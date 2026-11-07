"""DjVu, read in pure Python from the DjVu v3 specification: no DjVuLibre.

    from bookport import djvu
    doc = djvu.open("scan.djvu")                 # pages, outline, text
    pic = djvu.render("scan.djvu", 0)            # Picture: width, height, mode ("1", "L", "RGB"), data
    djvu.to_pdf("scan.djvu", "scan.pdf")         # layers kept, searchable text, bookmarks
    djvu.to_text("scan.djvu")                    # the hidden text, as djvutxt prints it

Single-page files, bundled documents and indirect ones (pages as files in the same folder) are
read; masks (JB2 with shared dictionaries, or G4), wavelet layers (IW44), palettes, hidden text,
outlines and links. The optional bookport-native package decodes many times faster.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .. import limits
from ..errors import BookportError
from ._accel import accelerated
from .doc import Document

__all__ = ["Document", "Picture", "accelerated", "open", "render", "to_pdf", "to_text"]


def open(path: str | os.PathLike) -> Document:
    """Read a DjVu document's structure (pages are decoded when they're drawn)."""
    path = Path(path)
    if not path.is_file():
        raise BookportError(f"No such file: {path}")
    with limits.reading():
        try:
            return Document(path)
        except BookportError:
            raise
        except (struct.error, IndexError, ValueError, OverflowError, MemoryError, RecursionError) as e:
            raise BookportError(f"Damaged DjVu file: {path.name} ({type(e).__name__})") from None


@dataclass
class Picture:
    """A drawn page: mode "1" is packed 1-bit rows (1 = black, as in PBM), "L" grey, "RGB" colour;
    rows top first, no padding beyond whole bytes for "1"."""
    width: int
    height: int
    mode: str
    data: bytes

    def png(self) -> bytes:
        """The picture as a PNG (1-bit, grey or colour)."""
        return _png(self)


def _doc(src) -> Document:
    return src if isinstance(src, Document) else open(src)


def render(src, page: int = 0, scale: float = 1.0) -> Picture:
    """Draw page ``page`` (0 is the first) the way DjVu viewers do, turned as the page says, at
    ``scale`` times its full resolution."""
    doc = _doc(src)
    if not 0 <= page < len(doc.pages):
        raise BookportError(f"This DjVu document has no page {page + 1} (it has {len(doc.pages)})")
    if not 0 < scale <= 4:
        raise BookportError("scale must be more than 0 and at most 4")
    from . import composite as r
    with limits.reading():
        try:
            p = doc.pages[page]
            w, h, mode, data = r.compose(p)
            pic = Picture(w, h, mode, data)
            if scale != 1.0:
                pic = _scale(pic, scale)
            return _rotate(pic, p.info.rotation)
        except BookportError:
            raise
        except (struct.error, IndexError, ValueError, OverflowError, MemoryError, RecursionError) as e:
            raise BookportError(f"Damaged DjVu file: page {page + 1} ({type(e).__name__})") from None


def to_pdf(src, dst: str | os.PathLike | None = None) -> bytes | Path:
    """Write the document as a PDF to ``dst`` (or return the PDF's bytes when there is no dst)."""
    from . import topdf
    doc = _doc(src)
    with limits.reading():
        try:
            data, _lost = topdf.convert(doc)
        except BookportError:
            raise
        except (struct.error, IndexError, ValueError, OverflowError, MemoryError, RecursionError) as e:
            raise BookportError(f"Damaged DjVu file: {doc.path.name} ({type(e).__name__})") from None
    if dst is None:
        return data
    dst = Path(dst)
    tmp = dst.with_name(f".bookport-{dst.name}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dst


def to_text(src) -> str:
    """The hidden text of every page as djvutxt prints it: each page with text ends in a newline
    and a form feed; pages without a text layer are skipped. Empty when there's no text layer."""
    from . import text
    doc = _doc(src)
    from .topdf import threads

    def one(p):
        try:
            return text.plain(p)
        except BookportError:
            return None

    with limits.reading():
        n = threads()
        if n > 1 and len(doc.pages) > 8:  # the compiled BZZ decoder lets go of Python's lock
            import contextvars
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(n) as pool:
                texts = list(pool.map(lambda p: contextvars.copy_context().run(one, p), doc.pages))
        else:
            texts = [one(p) for p in doc.pages]
    return b"".join(t + b"\n\f" for t in texts if t is not None).decode("utf-8", "replace")


def _scale(pic: Picture, scale: float) -> Picture:
    """Shrink by averaging (a black-and-white page turns grey) or grow by repeating pixels."""
    w2 = max(1, round(pic.width * scale))
    h2 = max(1, round(pic.height * scale))
    if pic.mode == "1":
        nb = (pic.width + 7) // 8
        rows = []
        for y in range(pic.height):
            r = pic.data[y * nb:(y + 1) * nb]
            rows.append(bytes(0 if (r[x >> 3] >> (7 - (x & 7))) & 1 else 255 for x in range(pic.width)))
        pic = Picture(pic.width, pic.height, "L", b"".join(rows))
    nc = 3 if pic.mode == "RGB" else 1
    out = bytearray(w2 * h2 * nc)
    xs = [(x * pic.width // w2, max(x * pic.width // w2 + 1, (x + 1) * pic.width // w2)) for x in range(w2)]
    for y in range(h2):
        y0 = y * pic.height // h2
        y1 = max(y0 + 1, (y + 1) * pic.height // h2)
        for x, (x0, x1) in enumerate(xs):
            n = (y1 - y0) * (x1 - x0)
            for c in range(nc):
                s = 0
                for yy in range(y0, y1):
                    base = yy * pic.width * nc + c
                    s += sum(pic.data[base + xx * nc] for xx in range(x0, x1))
                out[(y * w2 + x) * nc + c] = (s + n // 2) // n
    return Picture(w2, h2, pic.mode, bytes(out))


def _rotate(pic: Picture, degrees: int) -> Picture:
    """Turn counter-clockwise by 90, 180 or 270 degrees."""
    if not degrees:
        return pic
    w, h = pic.width, pic.height
    if pic.mode == "1":
        nb = (w + 7) // 8
        px = [[(pic.data[y * nb + (x >> 3)] >> (7 - (x & 7))) & 1 for x in range(w)] for y in range(h)]
    else:
        nc = 3 if pic.mode == "RGB" else 1
        px = [[pic.data[(y * w + x) * nc:(y * w + x + 1) * nc] for x in range(w)] for y in range(h)]
    if degrees == 180:
        out = [row[::-1] for row in px[::-1]]
    elif degrees == 90:  # counter-clockwise: the right column becomes the top row
        out = [[px[y][x] for y in range(h)] for x in range(w - 1, -1, -1)]
    else:
        out = [[px[y][x] for y in range(h - 1, -1, -1)] for x in range(w)]
    nh, nw = len(out), len(out[0]) if out else 0
    if pic.mode == "1":
        data = b"".join(bytes(sum(bit << (7 - k) for k, bit in enumerate(row[i:i + 8])) for i in range(0, nw, 8))
                        for row in out)
    else:
        data = b"".join(b"".join(row) for row in out)
    return Picture(nw, nh, pic.mode, data)


def _png(pic: Picture) -> bytes:
    if pic.mode == "1":
        depth, ctype = 1, 0
        nb = (pic.width + 7) // 8
        inv = bytes(255 - b for b in range(256))  # PNG grey 0 is black; DjVu's 1 is black
        rows = [pic.data[y * nb:(y + 1) * nb].translate(inv) for y in range(pic.height)]
    else:
        depth, ctype = 8, 2 if pic.mode == "RGB" else 0
        rl = pic.width * (3 if pic.mode == "RGB" else 1)
        rows = [pic.data[y * rl:(y + 1) * rl] for y in range(pic.height)]
    raw = b"".join(b"\0" + r for r in rows)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", pic.width, pic.height, depth, ctype, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
