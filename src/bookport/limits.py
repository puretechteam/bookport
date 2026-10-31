"""Limits that keep a hostile book from using unbounded memory or time, and the rule for local files.

Every limit is far above what a real book needs; going over one raises BookportError.

- One stream, record, section or ZIP member unpacks to at most ``MAX_PART`` bytes, and everything a
  book unpacks to, together, to at most ``MAX_BOOK`` (a running budget kept for each ``read()``).
- Pictures have at most ``MAX_PIXELS`` pixels; documents nest at most ``MAX_DEPTH`` elements deep;
  tables of contents at most ``MAX_TOC_DEPTH`` levels and ``MAX_TOC_ENTRIES`` entries.
- Local files a book links to (pictures, styles, covers, other pages) come only from the book's own
  folder and one level up (``safe_path``): never an absolute, network, ``file:`` or further-out path.
"""

from __future__ import annotations

import bz2
import re
import zipfile
import zlib
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import unquote

from .errors import BookportError

MB = 1024 * 1024
MAX_BOOK = 2048 * MB
MAX_PART = 512 * MB
MAX_PIXELS = 100_000_000  # 10,000 x 10,000
MAX_DEPTH = 128
MAX_TOC_DEPTH = 32
MAX_TOC_ENTRIES = 100_000
MAX_PDF_OPERATORS = 1_000_000  # content tokens run for one PDF page, forms included (a drawing costs 50)

class LimitError(BookportError):
    """A book over one of these limits (a BookportError, so callers need nothing new)."""


_budget: ContextVar[list[int] | None] = ContextVar("bookport_budget", default=None)


@contextmanager
def reading():
    """A fresh unpacking budget for one book (nested reads share the outer one)."""
    if _budget.get() is not None:
        yield
        return
    token = _budget.set([0])
    try:
        yield
    finally:
        _budget.reset(token)


def charge(n: int) -> None:
    """Count ``n`` unpacked bytes against the current book's budget."""
    used = _budget.get()
    if used is None:
        return
    used[0] += n
    if used[0] > MAX_BOOK:
        raise LimitError(f"This book unpacks to more than {MAX_BOOK // MB:,} MB, so bookport won't read it")


def _too_big(limit: int) -> LimitError:
    return LimitError(f"Part of this book unpacks to more than {limit // MB:,} MB, so bookport won't read it")


def inflate(data: bytes, wbits: int = 15, limit: int | None = None, partial: bool = False) -> bytes:
    """zlib.decompress with a size limit. ``partial`` returns what decodes before damage or a
    missing end instead of raising zlib.error."""
    limit = MAX_PART if limit is None else limit
    d = zlib.decompressobj(wbits)
    out = bytearray()
    step = 65536 if partial else max(1, len(data))
    try:
        for i in range(0, len(data), step):
            chunk = data[i:i + step]
            while chunk and not d.eof:
                out += d.decompress(chunk, limit + 1 - len(out))
                if len(out) > limit:
                    raise _too_big(limit)
                chunk = d.unconsumed_tail
            if d.eof:
                break
    except zlib.error:
        if not partial:
            raise
    if not partial and not d.eof:
        raise zlib.error("Error -5 while decompressing data: incomplete or truncated stream")
    charge(len(out))
    return bytes(out)


def bunzip(data: bytes, limit: int | None = None) -> bytes:
    """bz2.decompress (one stream) with a size limit."""
    limit = MAX_PART if limit is None else limit
    d = bz2.BZ2Decompressor()
    out = bytearray(d.decompress(data, limit + 1))
    while not d.eof and not d.needs_input and len(out) <= limit:
        out += d.decompress(b"", limit + 1 - len(out))
    if len(out) > limit:
        raise _too_big(limit)
    if not d.eof:
        raise ValueError("Compressed data ended before the end-of-stream marker was reached")
    charge(len(out))
    return bytes(out)


def grow(out: bytes | bytearray | int, limit: int | None = None) -> None:
    """Raise once a decoder's output (or its size) passes the limit for one part."""
    limit = MAX_PART if limit is None else limit
    if (out if isinstance(out, int) else len(out)) > limit:
        raise _too_big(limit)


def zip_read(z: zipfile.ZipFile, member, limit: int | None = None) -> bytes:
    """One ZIP member, refused when its stated size is over the limit or the book's budget.
    (zipfile never returns more than the stated size.) KeyError when it isn't there."""
    limit = MAX_PART if limit is None else limit
    info = member if isinstance(member, zipfile.ZipInfo) else z.getinfo(member)
    if info.file_size > limit:
        raise _too_big(limit)
    # A member can't hold more bytes than the file has. Python before 3.12 doesn't check, and reading
    # sets aside the stated compressed size first: a 3 KB file could ask for hundreds of MB.
    if z.fp is not None:
        pos = z.fp.tell()
        end = z.fp.seek(0, 2)
        z.fp.seek(pos)
        if info.header_offset + info.compress_size > end:
            raise BookportError(f"Damaged ZIP file: {info.filename} runs past the end of the file")
    charge(info.file_size)
    try:
        return z.read(info)
    except RuntimeError as e:  # an encrypted member
        raise BookportError(f"Can't read {info.filename} in this ZIP file: {e}") from None
    except (NotImplementedError, zipfile.BadZipFile, EOFError, OSError, zlib.error) as e:
        raise BookportError(f"Damaged ZIP file: can't read {info.filename} ({e})") from None


def check_pixels(width: int, height: int) -> None:
    if width * height > MAX_PIXELS:
        raise LimitError(f"A picture in this book is {width:,} x {height:,} pixels, more than bookport reads")


_NOT_LOCAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|[\\/])")  # a scheme or drive (file:, C:), or a rooted path


def safe_path(base, link: str, root=None) -> Path | None:
    """The local file ``link`` (relative to the folder ``base``) names, or None when it isn't allowed.

    Allowed: a relative link that stays inside ``root`` (by default one level above ``base``).
    Refused: absolute paths, drive letters, network (UNC) paths, ``file:`` and other schemes, and
    anything that resolves (symbolic links included) outside ``root``.
    """
    link = unquote(link.split("#")[0].split("?")[0]).strip()
    if not link or "\0" in link or _NOT_LOCAL.match(link):
        return None
    try:
        base = Path(base).resolve()
        top = (base.parent if root is None else Path(root)).resolve()
        target = (base / link).resolve()
        target.relative_to(top)
    except (ValueError, OSError):
        return None
    return target
