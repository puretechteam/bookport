"""Rocket eBook (RB, NuvoMedia): read and write.

An RB file has a 296-byte header ("NUVO" magic, the table of contents' offset and the file size)
and a table of entries (name, length, offset, flags). Flags 2 marks the info page (KEY=value
lines), 8 a deflated entry (zlib, in 4096-byte pieces, with a piece count and sizes up front).
Content is HTML and images.
"""

from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

from .. import limits, markup
from ..book import Book, Metadata, relative, sniff_image
from ..errors import BookportError

MAGIC = b"\xb0\x0c\xb0\x0c\x02\x00NUVO\x00\x00\x00\x00"
TOC_OFFSET = 296


def read(path) -> Book:
    data = Path(path).read_bytes()
    if not data.startswith(MAGIC[:4]):
        raise BookportError("Not a Rocket eBook (RB) file")
    toc = struct.unpack("<I", data[24:28])[0]
    if toc + 4 > len(data):
        raise BookportError("Damaged RB file: bad table of contents offset")
    count = struct.unpack("<I", data[toc:toc + 4])[0]
    entries = {}
    order = []
    info = {}
    for i in range(min(count, 10000)):
        e = data[toc + 4 + 44 * i:toc + 48 + 44 * i]
        if len(e) < 44:
            break
        name = e[:32].split(b"\0", 1)[0].decode("cp1252", "replace")
        length, offset, flags = struct.unpack("<III", e[32:44])
        raw = data[offset:offset + length]
        if flags & 8:
            raw = _inflate(raw)
        if flags & 2:
            for line in raw.decode("cp1252", "replace").splitlines():
                k, sep, v = line.partition("=")
                if sep:
                    info[k.strip().upper()] = v.strip()
            continue
        entries[name] = raw
        order.append(name)
    book = Book(Metadata(title=info.get("TITLE", Path(path).stem), authors=[a.strip() for a in
                                                                           info.get("AUTHOR", "").split(";") if a.strip()]))
    book.source_format = "rb"
    md = book.metadata
    md.publisher, md.rights = info.get("PUBLISHER", ""), info.get("COPYRIGHT", "")
    if info.get("ISBN"):
        md.identifiers["isbn"] = info["ISBN"]
    images = {}
    for name in order:
        if sniff_image(entries[name]):
            images[name] = book.add("images/" + re.sub(r"[^\w.-]", "_", name), entries[name]).href
    pages = [n for n in order if n.lower().endswith((".html", ".htm"))]
    page_href = {name.lower(): f"text/part{k:04d}.xhtml" for k, name in enumerate(pages, 1)}
    for name in pages:
        root = markup.parse(markup.decode(entries[name], "cp1252"))
        href = page_href[name.lower()]
        for el in root.iter("img"):
            src = el.get("src", "")
            if src in images:
                el.set("src", relative(href, images[src]))
        for el in root.iter("a"):  # pages are renamed, so links between them are too
            page, sep, frag = el.get("href", "").partition("#")
            if page.lower() in page_href:
                el.set("href", relative(href, page_href[page.lower()]) + sep + frag)
        book.add_document(href, root)
    if not book.spine:
        raise BookportError("This RB file has no pages")
    first = images.get(next((n for n in order if n.lower().startswith("cover")), ""), None)
    book.cover = first
    book.ensure_toc()
    return book


def _inflate(raw: bytes) -> bytes:
    if len(raw) < 8:
        return b""
    count, _size = struct.unpack("<II", raw[:8])
    sizes = struct.unpack(f"<{count}I", raw[8:8 + 4 * count]) if 8 + 4 * count <= len(raw) else ()
    pos = 8 + 4 * count
    out = []
    total = 0
    for s in sizes:
        try:
            piece = limits.inflate(raw[pos:pos + s], limit=limits.MAX_PART - total)
        except zlib.error:
            break
        out.append(piece)
        total += len(piece)
        pos += s
    return b"".join(out)


def _deflate(raw: bytes) -> bytes:
    pieces = [zlib.compress(raw[i:i + 4096], 9) for i in range(0, len(raw), 4096)] or [zlib.compress(b"")]
    return struct.pack("<II", len(pieces), len(raw)) + b"".join(struct.pack("<I", len(p)) for p in pieces) + b"".join(pieces)


def write(book: Book, path, **_) -> None:
    from ..flow import book_blocks
    from ..images import to_png
    # Rocket readers know simple HTML: one page with b, i, u, headings, centred paragraphs and images
    names: dict[str, str] = {}
    parts = ["<HTML><HEAD><TITLE>" + markup.escape(book.metadata.title) + "</TITLE></HEAD><BODY>"]

    def image(href):
        if href not in names:
            res = book.resources.get(href)
            if res is None or not res.is_image:
                return None
            names[href] = f"{len(names)}.png"
        return names[href]

    if book.cover and image(book.cover):
        parts.append(f'<P ALIGN="CENTER"><IMG SRC="{names[book.cover]}"></P>')
    for b in book_blocks(book):
        if b.kind == "image":
            n = image(b.image)
            if n:
                parts.append(f'<P ALIGN="CENTER"><IMG SRC="{n}"></P>')
            continue
        if b.kind == "hr":
            parts.append("<HR>")
            continue
        if b.kind == "table":
            for row in b.rows:
                parts.append("<P>" + " | ".join(_runs(c.runs, image) for c in row) + "</P>")
            continue
        text = (markup.escape(b.marker) + " " if b.marker else "") + _runs(b.runs, image)
        if b.kind == "h":
            level = min(max(b.level, 1), 6)
            parts.append(f"<H{level}>{text}</H{level}>")
        else:
            align = ' ALIGN="CENTER"' if b.align == "center" else ' ALIGN="RIGHT"' if b.align == "right" else ""
            parts.append(f"<P{align}>{text}</P>")
    parts.append("</BODY></HTML>")
    html = "\n".join(parts).encode("cp1252", "xmlcharrefreplace")
    md = book.metadata
    # one line per key: a line break in the title would start a key of its own
    info = f"TYPE=2\nTITLE={' '.join(md.title.split())}\nAUTHOR={' '.join('; '.join(md.authors).split())}\n"
    # the other keys a Rocket eBook's info page has
    for key, value in (("PUBLISHER", md.publisher), ("COPYRIGHT", md.rights), ("ISBN", md.identifiers.get("isbn", ""))):
        if value:
            info += f"{key}={' '.join(value.split())}\n"
    info += "GENERATOR=bookport\nBODY=index.html\n"
    files = [("info.info", info.encode("cp1252", "replace"), 2), ("index.html", _deflate(html), 8)]
    for href, name in names.items():
        data = book.resources[href].data
        png = data if data[:8] == b"\x89PNG\r\n\x1a\n" else to_png(data)
        if png is not None:
            files.append((name, png, 0))
    offset = TOC_OFFSET + 4 + 44 * len(files)
    toc = struct.pack("<I", len(files))
    body = b""
    for name, raw, flags in files:
        toc += name.encode("cp1252")[:31].ljust(32, b"\0") + struct.pack("<III", len(raw), offset + len(body), flags)
        body += raw
    total = TOC_OFFSET + len(toc) + len(body)
    header = MAGIC + b"\0" * (24 - len(MAGIC)) + struct.pack("<II", TOC_OFFSET, total)
    header = header.ljust(TOC_OFFSET, b"\0")
    Path(path).write_bytes(header + toc + body)


def _runs(runs, image) -> str:
    out = []
    for r in runs:
        if r.image:
            n = image(r.image)
            if n:
                out.append(f'<IMG SRC="{n}">')
            continue
        t = markup.escape(r.text).replace("\n", "<BR>")
        if r.bold:
            t = f"<B>{t}</B>"
        if r.italic:
            t = f"<I>{t}</I>"
        if r.underline:
            t = f"<U>{t}</U>"
        if r.sup:
            t = f"<SUP>{t}</SUP>"
        if r.sub:
            t = f"<SUB>{t}</SUB>"
        out.append(t)
    return "".join(out)


__all__ = ["read", "write"]
