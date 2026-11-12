"""Palm database ebooks: PalmDOC, zTXT and eReader (read and write), Plucker (read).

The kind is in the PDB header's type and creator: TEXtREAd (PalmDOC: PalmDOC-compressed
text), zTXTGPlm (zTXT: zlib text with random-access flushes), PNRdPPrs (eReader: PML text with
chapter, link and image records, zlib or PalmDOC compressed; the DRM variants are refused) and
DataPlkr (Plucker: text and image records with function codes).
"""

from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

from .. import limits
from ..book import Book
from ..errors import BookportError, DRMError
from ..palm import palmdoc_compress, palmdoc_decompress, pdb_bytes, read_pdb


def read(path) -> Book:
    data = Path(path).read_bytes()
    pdb = read_pdb(data)
    kind = pdb.type + pdb.creator
    name = pdb.name.decode("cp1252", "replace").strip("\0").replace("_", " ")
    if kind == b"TEXtREAd":
        book = _palmdoc(pdb, name)
    elif kind == b"zTXTGPlm":
        book = _ztxt(pdb, name)
    elif kind == b"PNRdPPrs":
        book = _ereader(pdb, name)
    elif kind == b"DataPlkr":
        from .plucker import read_plucker
        book = read_plucker(pdb, name)
    elif kind == b"BOOKMOBI":
        from .mobi import from_bytes
        return from_bytes(data)
    else:
        raise BookportError(f"Unknown Palm ebook type {kind.decode('latin-1', 'replace')!r}")
    if not book.metadata.language:
        from .txt import guess_language
        book.metadata.language = guess_language(book.text())
    return book


def _text_book(text: str, title: str, fmt: str) -> Book:
    from .txt import book_from_text
    book = book_from_text(text, title, formatting="plain")
    book.source_format = fmt
    if not book.metadata.title:
        book.metadata.title = title
    return book


def _palmdoc(pdb, title) -> Book:
    r0 = pdb.records[0]
    compression, _, length, count = struct.unpack(">HHIH", r0[:10])
    if compression not in (1, 2):
        raise BookportError(f"Unknown PalmDOC compression {compression}")
    recs = pdb.records[1:1 + count]
    raw = b"".join(palmdoc_decompress(r) if compression == 2 else r for r in recs)[:length or None]
    text = raw.decode("utf-8") if _is_utf8(raw) else raw.decode("cp1252", "replace")
    return _text_book(text, title, "pdb")


def _is_utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
        return any(c >= 0x80 for c in b)
    except UnicodeDecodeError:
        return False


def _ztxt(pdb, title) -> Book:
    r0 = pdb.records[0]
    _version, count, length = struct.unpack(">HHI", r0[:8])
    d = zlib.decompressobj()
    out = bytearray()
    for r in pdb.records[1:1 + count]:
        try:
            out += d.decompress(r, limits.MAX_PART + 1 - len(out))
        except zlib.error:
            break
        limits.grow(out)
    limits.charge(len(out))
    out = bytes(out[:length or None])
    text = out.decode("utf-8") if _is_utf8(out) else out.decode("cp1252", "replace")
    return _text_book(text, title, "pdb")


def _ereader(pdb, title) -> Book:
    from .pml import pml_to_book
    r = pdb.records
    r0 = r[0]
    if len(r0) == 202 or len(r0) == 116:
        raise DRMError("eReader", "it is a protected (Palm Digital Media) book")
    compression = struct.unpack(">H", r0[:2])[0]
    if compression in (260, 272):
        raise DRMError("eReader", "its text is encrypted")
    if compression not in (1, 2, 10):
        raise BookportError(f"Unknown eReader compression {compression}")
    encoding = struct.unpack(">H", r0[6:8])[0]
    non_text = struct.unpack(">H", r0[12:14])[0]
    n_images = struct.unpack(">H", r0[20:22])[0]
    image_start = struct.unpack(">H", r0[40:42])[0] if len(r0) >= 42 else 0
    meta_start = struct.unpack(">H", r0[44:46])[0] if len(r0) >= 46 else 0
    parts = []
    total = 0
    for rec in r[1:non_text or len(r)]:
        try:
            part = (limits.inflate(rec, limit=limits.MAX_PART - total) if compression == 10 else
                    palmdoc_decompress(rec) if compression == 2 else rec)
        except zlib.error:
            break
        parts.append(part)
        total += len(part)
        limits.grow(total)
    text = b"".join(parts)
    # the chapter index (offset into the text, title) becomes anchors plus \C entries for the TOC
    n_chapters = struct.unpack(">H", r0[14:16])[0] if len(r0) >= 16 else 0
    chapter_start = struct.unpack(">H", r0[32:34])[0] if len(r0) >= 34 else 0
    marks = []
    if chapter_start and n_chapters and chapter_start + n_chapters <= len(r):
        for k, rec in enumerate(r[chapter_start:chapter_start + n_chapters]):
            if len(rec) < 5:
                continue
            offset = struct.unpack(">I", rec[:4])[0]
            raw = rec[4:].split(b"\0", 1)[0]
            stripped = raw.lstrip(b"\n")
            level = min((len(stripped) - len(stripped.lstrip(b" "))) // 2, 4)
            title = stripped.strip().decode("utf-8" if encoding == 65001 else "cp1252", "replace")
            if title and 0 <= offset <= len(text):
                marks.append((offset, k, level, title))
    if marks and not re.search(rb"\\X[1-4]", text) and len(re.findall(rb"\\X0", text)) // 2 < len(marks) // 2:
        pieces = []
        last = 0
        for offset, k, level, title in sorted(marks):
            pieces.append(text[last:offset])
            enc = "utf-8" if encoding == 65001 else "cp1252"
            pieces.append(b'\\Q="toc%d"\\C%d="%s"' % (k, level, title.replace('"', "'").encode(enc, "replace")))
            last = offset
        pieces.append(text[last:])
        text = b"".join(pieces)
    pml = text.decode("utf-8" if encoding == 65001 else "cp1252", "replace")
    images = {}
    for rec in r[image_start:image_start + n_images] if image_start else []:
        if rec[:4] in (b"PNG ", b"JPEG", b"GIF "):
            name = rec[4:36].split(b"\0", 1)[0].decode("cp1252", "replace")
            for magic in (b"\x89PNG", b"\xff\xd8\xff", b"GIF8"):
                at = rec.find(magic, 36)
                if at >= 0:
                    images[name] = rec[at:]
                    break
    book = pml_to_book(pml, title, images)
    book.source_format = "pdb"
    if meta_start and meta_start < len(r):
        fields = r[meta_start].split(b"\0")
        names = ["title", "author", "rights", "publisher", "isbn"]
        values = {k: fields[i].decode("cp1252", "replace").strip() for i, k in enumerate(names) if i < len(fields)}
        md = book.metadata
        md.title = values.get("title") or md.title
        if values.get("author"):
            md.authors = [a.strip() for a in values["author"].replace("&", ";").split(";") if a.strip()]
        md.rights = values.get("rights", "")
        md.publisher = values.get("publisher", "")
        if values.get("isbn"):
            md.identifiers["isbn"] = values["isbn"]
    if images:
        first = next(iter(images))
        if first.lower().startswith("cover"):
            book.cover = next((h for h, res in book.resources.items() if res.data == images[first]), None)
    return book


# --- writing -----------------------------------------------------------------------------------

def write(book: Book, path, fmt: str = "pdb", format: str = "ereader", **_) -> None:
    kind = (format or "ereader").lower()
    if kind in ("doc", "palmdoc", "text"):
        data = _write_palmdoc(book)
    elif kind == "ztxt":
        data = _write_ztxt(book)
    elif kind == "ereader":
        data = _write_ereader(book)
    else:
        raise BookportError(f"Unknown PDB format {format!r}: use ereader, doc or ztxt")
    Path(path).write_bytes(data)


def _plain_text(book: Book) -> bytes:
    from .txt import book_to_text
    text = book_to_text(book).replace("\n", "\r\n")
    try:
        return text.encode("cp1252")  # what older Palm readers expect
    except UnicodeEncodeError:
        return text.encode("utf-8")  # anything else (Cyrillic, Greek, CJK): the readers here detect UTF-8


def _write_palmdoc(book: Book) -> bytes:
    text = _plain_text(book)
    recs = [palmdoc_compress(text[i:i + 4096]) for i in range(0, len(text), 4096)]
    r0 = struct.pack(">HHIHHHH", 2, 0, len(text), len(recs), 4096, 0, 0)
    return pdb_bytes(book.metadata.title or "book", b"TEXt", b"REAd", [r0] + recs)


def _write_ztxt(book: Book) -> bytes:
    text = _plain_text(book)
    c = zlib.compressobj(9)
    recs = []
    for i in range(0, len(text), 8192):
        recs.append(c.compress(text[i:i + 8192]) + c.flush(zlib.Z_FULL_FLUSH))  # each record starts cleanly
    if recs:
        recs[-1] += c.flush()
    crc = zlib.crc32(b"".join(recs)) & 0xFFFFFFFF
    r0 = struct.pack(">HHIHHHHHBBI", 0x012C, len(recs), len(text), 8192, 0, 0, 0, 0, 1, 0, crc) + b"\0" * 8
    return pdb_bytes(book.metadata.title or "book", b"zTXT", b"GPlm", [r0] + recs)


def _write_ereader(book: Book) -> bytes:
    from ..images import to_png
    from .pml import book_to_pml
    pml, images = book_to_pml(book, ascii_only=True)
    text = pml.encode("cp1252", "replace")
    # text in pieces of about 8 KB, cut at line breaks
    chunks = []
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + 8192)
        if end < len(text):
            nl = text.rfind(b"\n", pos, end)
            if nl > pos:
                end = nl + 1
        chunks.append(text[pos:end])
        pos = end
    text_recs = [zlib.compress(c, 9) for c in chunks]
    # chapter index: offset into the text and title, for each \X heading
    chapters = []
    for m in re.finditer(rb"\\X([0-4])(.*?)\\X\1", text, re.DOTALL):
        title = re.sub(rb"\\(?:a\d{3}|U[0-9A-Fa-f]{4}|Q=\"[^\"]*\"|[A-Za-z]{1,2}(?:=\"[^\"]*\")?)", b"", m.group(2)).strip()
        chapters.append(struct.pack(">I", m.start()) + b"  " * int(m.group(1)) + title[:200] + b"\0")
    links = []
    for m in re.finditer(rb'\\Q="([^"]+)"', text):
        links.append(struct.pack(">I", m.start()) + m.group(1)[:64] + b"\0")
    image_recs = []
    for href, name in images.items():
        data = book.resources[href].data
        png = data if data[:8] == b"\x89PNG\r\n\x1a\n" else to_png(data)
        if png is None:
            continue
        image_recs.append(b"PNG " + name.encode("ascii")[:31].ljust(32, b"\0") + b"\0" * 26 + png)
    md = book.metadata
    meta = b"\0".join(x.encode("cp1252", "replace") for x in (md.title, "; ".join(md.authors), md.rights, md.publisher,
                                                              md.identifiers.get("isbn", ""))) + b"\0"
    n_text = len(text_recs)
    chapter_start = 1 + n_text
    link_start = chapter_start + len(chapters)
    image_start = link_start + len(links)
    meta_rec = image_start + len(image_recs)
    last = meta_rec + 1
    fields = [10, 0, 0, 25152, 0, 0, chapter_start, len(chapters), 0, 0, len(image_recs), len(links), 1, 0, 0, 0,
              chapter_start, 2560, last, last, image_start, link_start, meta_rec, 0, last, last, last]
    r0 = struct.pack(f">{len(fields)}H", *fields).ljust(132, b"\0")
    tail = b"\x1f\xff\x1f\xff" + b" \x00" * 20 + struct.pack(">H", n_text)
    records = [r0] + text_recs + chapters + links + image_recs + [meta, tail, b"MeTaInFo\0"]
    return pdb_bytes(md.title or "book", b"PNRd", b"PPrs", records)
