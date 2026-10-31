"""Which formats bookport reads and writes, format detection, and the read/write/convert entry points."""

from __future__ import annotations

import importlib
import os
import struct
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

from . import limits
from .book import Book, TocEntry
from .errors import BookportError

# what a damaged file makes the readers raise; read() turns these into BookportError
DAMAGE = (struct.error, LookupError, ValueError, ArithmeticError, EOFError, zlib.error, zipfile.BadZipFile, ET.ParseError,
          RecursionError, MemoryError, NotImplementedError)

# ext: (module, name, readable, writable); KFX and CB7 are listed so they are refused by name
FORMATS: dict[str, tuple[str, str, bool, bool]] = {
    "epub": ("epub", "EPUB", True, True),
    "kepub": ("epub", "Kobo EPUB", True, False),
    "mobi": ("mobi", "Mobipocket / Kindle (MOBI)", True, True),
    "azw": ("mobi", "Kindle (AZW)", True, True),
    "azw3": ("mobi", "Kindle KF8 (AZW3)", True, True),
    "kfx": ("kfx", "Kindle KFX", False, False),
    "prc": ("mobi", "Mobipocket (PRC)", True, False),
    "pdb": ("pdb", "Palm database ebook (PalmDOC, eReader, Plucker, zTXT)", True, True),
    "fb2": ("fb2", "FictionBook 2", True, True),
    "fbz": ("fb2", "FictionBook 2, zipped", True, True),
    "lit": ("lit", "Microsoft Reader (LIT)", True, True),
    "lrf": ("lrf", "Sony BroadBand eBook (LRF)", True, True),
    "chm": ("chm", "Compiled HTML Help (CHM)", True, False),
    "html": ("html", "HTML", True, True),
    "htm": ("html", "HTML", True, False),
    "xhtml": ("html", "XHTML", True, False),
    "htmlz": ("htmlz", "Zipped HTML (HTMLZ)", True, True),
    "txt": ("txt", "Plain text", True, True),
    "text": ("txt", "Plain text", True, False),
    "txtz": ("txtz", "Zipped text (TXTZ)", True, True),
    "md": ("markdown", "Markdown", True, True),
    "markdown": ("markdown", "Markdown", True, False),
    "docx": ("docx", "Word document (DOCX)", True, True),
    "odt": ("odt", "OpenDocument text (ODT)", True, False),
    "rtf": ("rtf", "Rich Text Format", True, True),
    "pdf": ("pdf", "PDF", True, True),
    "cbz": ("comic", "Comic book ZIP", True, False),
    "cbr": ("comic", "Comic book RAR (stored pages)", True, False),
    "cb7": ("comic", "Comic book 7z", False, False),
    "rb": ("rb", "Rocket eBook (RB)", True, True),
    "tcr": ("tcr", "Psion TCR", True, True),
    "snb": ("snb", "Shanda Bambook (SNB)", True, True),
    "pml": ("pml", "Palm Markup Language", True, True),
    "pmlz": ("pml", "Zipped PML (PMLZ)", True, True),
    "oebzip": ("epub", "OEB zip", True, False),
    "jpg": ("comic", "JPEG image (one page)", True, False),
    "jpeg": ("comic", "JPEG image (one page)", True, False),
    "png": ("comic", "PNG image (one page)", True, False),
    "gif": ("comic", "GIF image (one page)", True, False),
    "webp": ("comic", "WebP image (one page)", True, False),
}


def formats() -> dict[str, dict]:
    """{extension: {"name", "read", "write"}} for every format bookport knows."""
    return {ext: {"name": name, "read": r, "write": w} for ext, (_m, name, r, w) in FORMATS.items() if r or w}


def _module(name: str):
    return importlib.import_module(f"bookport.fmt.{name}")


def detect(path: str | os.PathLike) -> str:
    """The format of a file, from its content first and its extension second."""
    p = Path(path)
    name = p.name.lower()
    try:
        with open(p, "rb") as f:
            head = f.read(4096)
    except OSError as e:
        raise BookportError(f"Can't open {p}: {e}") from None
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if name.endswith(".fb2.zip"):
        return "fbz"
    if head[:4] == b"PK\x03\x04" or head[:4] == b"PK\x05\x06":
        return _zip_kind(p, ext)
    if _pdf_header(head):
        return "pdf"
    if head[:8] == b"ITOLITLS":
        return "lit"
    if head[:4] == b"ITSF":
        return "chm"
    if head[:6] == b"L\0R\0F\0":
        return "lrf"
    if head[:4] == b"SNBP":
        return "snb"
    if head[:9] == b"!!8-Bit!!":
        return "tcr"
    if head[:4] == b"\xb0\x0c\xb0\x0c":
        return "rb"
    if head[:5] in (b"{\\rtf", b"{\\urt"):
        return "rtf"
    if head[:6] == b"Rar!\x1a\x07":
        return "cbr"
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or head[:6] in (b"GIF87a", b"GIF89a"):
        return ext if ext in ("jpg", "jpeg", "png", "gif") else "png" if head[:4] == b"\x89PNG" else "jpg"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        raise BookportError(f"{p.name} is an old Microsoft Office file (or a password-protected one), "
                            "which bookport can't read")
    if len(head) >= 68:
        kind = head[60:68]
        if kind == b"BOOKMOBI":
            if ext in ("azw3", "azw", "prc"):
                return ext
            return "azw3" if _is_kf8_only(p) else "mobi"
        if kind[:4] in (b"TEXt", b"PNRd", b"Data", b"zTXT", b"BVok", b"ToGo", b"PPrs", b"SDoc") or kind in (b"TEXtREAd", b"PNRdPPrs", b"DataPlkr", b"zTXTGPlm"):
            return "pdb"
    if ext in FORMATS:
        return {"htm": "html", "xhtml": "html", "markdown": "md", "text": "txt", "kepub": "epub"}.get(ext, ext)
    low = head.lower()
    if b"<fictionbook" in low:
        return "fb2"
    if b"<html" in low or b"<!doctype html" in low:
        return "html"
    return "txt"


def _pdf_header(head: bytes) -> bool:
    """%PDF- at the start, or after a binary header (MacBinary, for one), never inside text."""
    at = head.find(b"%PDF-", 0, 1024)
    if at < 0:
        return False
    return at == 0 or any(b < 9 or 13 < b < 32 for b in head[:at])


def _zip_kind(p: Path, ext: str) -> str:
    try:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            lower = [n.lower() for n in names]
            if "mimetype" in names and z.getinfo("mimetype").file_size <= 256:
                mt = limits.zip_read(z, "mimetype").strip()
                if mt == b"application/epub+zip":
                    return "epub"
                if mt == b"application/vnd.oasis.opendocument.text":
                    return "odt"
            if "[content_types].xml" in lower and any(n.startswith("word/") for n in lower):
                return "docx"
            if "meta-inf/container.xml" in lower:
                return "epub"
            if any(n.endswith(".fb2") for n in lower):
                return "fbz"
            if any(n.endswith(".pml") for n in lower):
                return "pmlz"
            if ext in ("htmlz", "txtz", "cbz", "epub", "docx", "odt", "kepub", "oebzip"):
                return {"kepub": "epub", "oebzip": "epub"}.get(ext, ext)
            if any(n.endswith((".html", ".htm", ".xhtml")) for n in lower):
                return "htmlz"
            if any(n.endswith((".txt", ".md", ".text")) for n in lower):
                return "txtz"
            if lower and all(n.endswith(("/", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".xml", ".txt", ".json"))
                             for n in lower):
                return "cbz"
    except (OSError, RuntimeError, *DAMAGE):
        raise BookportError(f"Damaged ZIP file: {p.name}") from None
    return ext or "epub"


def _is_kf8_only(p: Path) -> bool:
    from .fmt import mobi
    try:
        return mobi.kind(p.read_bytes()) == "azw3"
    except Exception:  # noqa: BLE001 - detection never fails on a strange file
        return False


def read(path: str | os.PathLike, fmt: str | None = None, **options) -> Book:
    """Read a book from any supported format."""
    path = Path(path)
    if path.is_dir():
        fmt = fmt or "cbz"  # a folder of images
    elif not path.is_file():
        raise BookportError(f"No such file: {path}")
    fmt = (fmt or detect(path)).lower().lstrip(".")
    entry = FORMATS.get(fmt)
    if entry is None or not entry[2]:
        raise BookportError(f"bookport can't read {fmt.upper()} files")
    module = _module(entry[0])
    with limits.reading():
        try:
            book = module.read(path, **options) if options else module.read(path)
            if not book.source_format:
                book.source_format = fmt
            _finish(book, path)
        except BookportError:
            raise
        except DAMAGE as e:
            what = ("uses more memory than bookport allows" if isinstance(e, MemoryError) else
                    "nests too deeply" if isinstance(e, RecursionError) else "is damaged")
            raise BookportError(f"Can't read {path.name}: the {entry[1]} file {what} "
                                f"({type(e).__name__}: {str(e)[:120] or 'no details'})") from e
    return book


def _finish(book: Book, path: Path) -> None:
    book.toc = clamp_toc(book.toc)
    if not book.metadata.title:
        book.metadata.title = path.stem.replace("_", " ")
    if book.cover is None:
        book.cover = guess_cover(book)
    book.metadata.uid()


def clamp_toc(toc: list[TocEntry]) -> list[TocEntry]:
    """At most MAX_TOC_DEPTH levels (deeper entries move up to the last level) and MAX_TOC_ENTRIES entries."""
    left = limits.MAX_TOC_ENTRIES

    def walk(entries, depth):
        nonlocal left
        out = []
        for e in entries:
            if left <= 0:
                break
            left -= 1
            out.append(e)
            if depth < limits.MAX_TOC_DEPTH - 1:
                e.children = walk(e.children, depth + 1)
                continue
            stack, flat = list(reversed(e.children)), []
            while stack and left > 0:  # everything below, in reading order, as siblings
                c = stack.pop()
                stack.extend(reversed(c.children))
                c.children = []
                flat.append(c)
                left -= 1
            e.children = []
            out.extend(flat)
        return out

    return walk(toc, 0)


def guess_cover(book: Book) -> str | None:
    """For formats without a cover field: an image that comes before any text on the first page."""
    from .book import resolve
    for href, _res in list(book.documents())[:1]:
        body = book.tree(href).find("body")
        if body is None:
            return None
        for el in body.iter():
            if (el.text or "").strip() and el.tag not in ("style", "script"):
                return None
            if el.tag in ("img", "image"):
                src = el.get("src") or el.get("xlink:href") or el.get("href") or ""
                target = resolve(href, src)
                res = book.resources.get(target)
                return target if res is not None and res.is_image else None
            if el.tail and el.tail.strip():
                return None
    return None


def write_book(book: Book, path, fmt: str | None = None, **options) -> Path:
    path = Path(path)
    fmt = (fmt or path.suffix).lower().lstrip(".")
    if path.name.lower().endswith(".fb2.zip"):
        fmt = "fbz"
    entry = FORMATS.get(fmt)
    if entry is None or not entry[3]:
        raise BookportError(f"bookport can't write {fmt.upper() or 'files without an extension'}")
    module = _module(entry[0])
    allowed = write_options(fmt)
    unknown = sorted(set(options) - allowed)
    if unknown:
        known = ", ".join(sorted(allowed)) or "none"
        raise BookportError(f"Unknown option for {fmt.upper()}: {', '.join(unknown)} (options: {known})")
    path.parent.mkdir(parents=True, exist_ok=True)
    # write to a temporary name first, so a failed write never leaves half a file behind
    fd, tmp = tempfile.mkstemp(prefix=".bookport-", suffix="." + fmt, dir=path.parent)
    os.close(fd)
    try:
        module.write(book, tmp, fmt=fmt, **options) if _takes_fmt(module) else module.write(book, tmp, **options)
        os.chmod(tmp, 0o666 & ~_umask())  # mkstemp makes the file private; give it a new file's usual mode
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _umask() -> int:
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def write_options(fmt: str) -> set[str]:
    """The options a format's writer takes (its keyword parameters)."""
    import inspect
    entry = FORMATS.get(fmt)
    if entry is None or not entry[3]:
        return set()
    params = inspect.signature(_module(entry[0]).write).parameters
    return {n for n, p in params.items() if p.kind == p.KEYWORD_ONLY or (p.kind == p.POSITIONAL_OR_KEYWORD and
            p.default is not p.empty)} - {"fmt"}


def _takes_fmt(module) -> bool:
    import inspect
    try:
        params = inspect.signature(module.write).parameters
    except (TypeError, ValueError):
        return False
    return "fmt" in params


def convert(src, dst, fmt: str | None = None, **options) -> Path:
    """Read ``src`` and write it to ``dst`` (format from ``fmt`` or ``dst``'s extension)."""
    book = read(src)
    return write_book(book, dst, fmt, **options)
