"""Plucker (DataPlkr Palm databases): read.

Record 0 lists special records (the home page first). Each data record starts with an 8-byte
header (uid, paragraph count, size, type, flags); text records (types 0 and 1) then carry one
(size, attributes) pair per paragraph and the text, PalmDOC-compressed (version 1) or zlib
(version 2). Inside the text a NUL starts a function code whose low 3 bits give the number of
argument bytes: links to other records, italic, underline, strike-through, styles, line breaks.
Images (types 2 and 3) are Palm bitmaps, decoded to PNG; a composite image (type 15) is a grid of
them joined into one. The function codes 0x1A and 0x5C put an image in the text.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
import zlib

from .. import limits, markup, palmbitmap
from ..book import Book, Metadata, relative
from ..errors import BookportError, DRMError
from ..images import Raster, encode_png
from ..palm import palmdoc_decompress

TEXT, TEXT_COMPRESSED, IMAGE, IMAGE_COMPRESSED, COMPOSITE = 0, 1, 2, 3, 15


def read_plucker(pdb, title: str) -> Book:
    r = pdb.records
    if len(r[0]) < 6:
        raise BookportError("Damaged Plucker file")
    _uid, version, n_special = struct.unpack(">HHH", r[0][:6])
    if version not in (1, 2):
        raise DRMError("Plucker", "its records are encrypted") if version > 2 else BookportError("Unknown Plucker version")
    home = None
    for k in range(n_special):
        name, rid = struct.unpack(">HH", r[0][6 + 4 * k:10 + 4 * k])
        if name == 0:
            home = rid
    texts = {}
    meta = {}
    bitmaps: dict[int, Raster] = {}
    composites: dict[int, bytes] = {}
    for rec in r[1:]:
        if len(rec) < 8:
            continue
        uid, paras, size, kind, _flags = struct.unpack(">HHHBB", rec[:8])
        if kind in (TEXT, TEXT_COMPRESSED):
            body = rec[8 + 4 * paras:]
            if kind == TEXT_COMPRESSED:
                try:
                    body = palmdoc_decompress(body) if version == 1 else limits.inflate(body)
                except zlib.error:
                    continue
            body = body[:size] if size else body
            # each paragraph from the table becomes its own <p> (a NUL 0x38 is a line break)
            sizes = [struct.unpack(">H", rec[8 + 4 * k:10 + 4 * k])[0] for k in range(paras)]
            pieces, pos = [], 0
            for s in sizes:
                pieces.append(body[pos:pos + s])
                pos += s
            if pos < len(body):
                pieces.append(body[pos:])
            texts[uid] = b"\x00\x38".join(pieces) if pos <= len(body) else body
        elif kind in (IMAGE, IMAGE_COMPRESSED):
            body = rec[8:]
            try:
                if kind == IMAGE_COMPRESSED:
                    body = palmdoc_decompress(body) if version == 1 else limits.inflate(body)
                bitmaps[uid] = palmbitmap.decode(body)
            except limits.LimitError:
                raise
            except (BookportError, zlib.error, struct.error, IndexError, ValueError):
                continue  # a picture that can't be read is left out, as before
        elif kind == COMPOSITE:
            composites[uid] = rec[8:]
        elif kind == 10:  # metadata
            meta = _metadata(rec[8:])
    book = Book(Metadata(title=meta.get("title") or title))
    book.source_format = "pdb"
    if meta.get("author"):
        book.metadata.authors = [meta["author"]]
    for uid, grid in composites.items():
        joined = _composite(grid, bitmaps)
        if joined is not None:
            bitmaps[uid] = joined
    order = ([home] if home in texts else []) + sorted(u for u in texts if u != home)
    hrefs = {uid: f"text/page{uid:05d}.xhtml" for uid in order}
    images: dict[int, str] = {}  # added when a page first shows them

    def image(uid: int) -> str | None:
        if uid not in images and uid in bitmaps:
            images[uid] = book.add(f"images/image{uid:05d}.png", encode_png(bitmaps[uid]), "image/png").href
        return images.get(uid)

    for uid in order:
        root, body = markup.new_document(book.metadata.title, "")
        _text(texts[uid], body, hrefs, hrefs[uid], image)
        book.add_document(hrefs[uid], root)
    if not book.spine:
        raise BookportError("This Plucker file has no text")
    book.ensure_toc()
    return book


def _metadata(data: bytes) -> dict:
    out = {}
    try:
        count = struct.unpack(">H", data[:2])[0]
        pos = 2
        for _ in range(count):
            typ, length = struct.unpack(">HH", data[pos:pos + 4])
            value = data[pos + 4:pos + 4 + 2 * length]
            if typ == 4:
                out["author"] = value.split(b"\0")[0].decode("cp1252", "replace")
            elif typ == 5:
                out["title"] = value.split(b"\0")[0].decode("cp1252", "replace")
            pos += 4 + 2 * length
    except struct.error:
        pass
    return out


def _text(data: bytes, body, hrefs, own_href, image=lambda uid: None) -> None:
    para = ET.SubElement(body, "p")
    stack = [para]
    i, n = 0, len(data)
    buf = bytearray()

    def flush():
        if buf:
            markup.append_text(stack[-1], buf.decode("cp1252", "replace"))
            buf.clear()

    def open_(tag, attrs=None):
        flush()
        el = ET.SubElement(stack[-1], tag, attrs or {})
        stack.append(el)

    def close(tag):
        flush()
        for k in range(len(stack) - 1, 0, -1):
            if stack[k].tag == tag:
                del stack[k:]
                return

    while i < n:
        c = data[i]
        if c != 0:
            buf.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        code = data[i + 1]
        args = data[i + 2:i + 2 + (code & 7)]
        i += 2 + (code & 7)
        if code in (0x0A, 0x0C) and len(args) >= 2:
            uid = struct.unpack(">H", args[:2])[0]
            target = hrefs.get(uid)
            open_("a", {"href": relative(own_href, target)} if target else {})
        elif code == 0x08:
            close("a")
        elif code == 0x38:
            flush()
            para = ET.SubElement(body, "p")
            stack[:] = [para]
        elif code == 0x40:
            open_("i")
        elif code == 0x48:
            close("i")
        elif code == 0x60:
            open_("u")
        elif code == 0x68:
            close("u")
        elif code == 0x70:
            open_("s")
        elif code == 0x78:
            close("s")
        elif code == 0x11 and args:
            close("b")
            close("code")
            style = args[0]
            if style in (1, 2, 3, 4, 5, 6):
                flush()
                h = ET.SubElement(body, f"h{style}")
                stack[:] = [h]
            elif style == 7:
                open_("b")
            elif style == 8:
                open_("code")
        elif code == 0x33:
            flush()
            ET.SubElement(body, "hr")
        elif code in (0x1A, 0x5C) and len(args) >= 2:
            # 0x5C: a larger image, then the one shown in the text
            uids = [struct.unpack(">H", args[2:4])[0], struct.unpack(">H", args[:2])[0]] if code == 0x5C else [
                struct.unpack(">H", args[:2])[0]]
            src = next((h for h in map(image, uids) if h), None)
            if src:
                flush()
                ET.SubElement(stack[-1], "img", {"src": relative(own_href, src), "alt": ""})
        elif code in (0x83, 0x85) and len(args) >= 3:
            cp = struct.unpack(">H", args[1:3])[0] if code == 0x83 else struct.unpack(">I", args[1:5])[0]
            flush()
            try:
                markup.append_text(stack[-1], chr(cp))
            except ValueError:
                pass
            i += args[0] if args else 0  # skip the plain-text alternative that follows
    flush()
    for p in list(body):
        if p.tag == "p" and not len(p) and not (p.text or "").strip():
            body.remove(p)


def _composite(grid: bytes, bitmaps: dict[int, Raster]) -> Raster | None:
    """A composite image: columns, rows, then the record of each tile, row by row."""
    if len(grid) < 4:
        return None
    cols, rows = struct.unpack(">HH", grid[:4])
    uids = [struct.unpack(">H", grid[4 + 2 * k:6 + 2 * k])[0] for k in range(min(cols * rows, (len(grid) - 4) // 2))]
    if not cols or not rows or len(uids) < cols * rows or any(u not in bitmaps for u in uids):
        return None
    tiles = [[_rgba(bitmaps[uids[r * cols + c]]) for c in range(cols)] for r in range(rows)]
    widths = [t.width for t in tiles[0]]
    heights = [row[0].height for row in tiles]
    width, height = sum(widths), sum(heights)
    limits.check_pixels(width, height)
    out = bytearray(4 * width * height)
    y0 = 0
    for r, row in enumerate(tiles):
        x0 = 0
        for c, t in enumerate(row):
            w, h = min(t.width, widths[c]), min(t.height, heights[r])
            for y in range(h):
                start = 4 * ((y0 + y) * width + x0)
                out[start:start + 4 * w] = t.pixels[4 * y * t.width:4 * (y * t.width + w)]
            x0 += widths[c]
        y0 += heights[r]
    return Raster(width, height, "RGBA", bytes(out))


def _rgba(r: Raster) -> Raster:
    if r.mode == "RGBA":
        return r
    px = bytearray(4 * r.width * r.height)
    px[0::4], px[1::4], px[2::4] = r.pixels[0::3], r.pixels[1::3], r.pixels[2::3]
    px[3::4] = b"\xff" * (r.width * r.height)
    return Raster(r.width, r.height, "RGBA", bytes(px))
