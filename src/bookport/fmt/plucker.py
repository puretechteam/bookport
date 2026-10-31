"""Plucker (DataPlkr Palm databases): read.

Record 0 lists special records (the home page first). Each data record starts with an 8-byte
header (uid, paragraph count, size, type, flags); text records (types 0 and 1) then carry one
(size, attributes) pair per paragraph and the text, PalmDOC-compressed (version 1) or zlib
(version 2). Inside the text a NUL starts a function code whose low 3 bits give the number of
argument bytes: links to other records, italic, underline, strike-through, styles, line breaks.
Images are Palm bitmaps, which bookport doesn't decode, so they're left out.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
import zlib

from .. import limits, markup
from ..book import Book, Metadata, relative
from ..errors import BookportError, DRMError
from ..palm import palmdoc_decompress

TEXT, TEXT_COMPRESSED = 0, 1


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
            texts[uid] = b"\x00\x38".join(pieces) if sum(sizes) == len(body) or pos <= len(body) else body
        elif kind == 10:  # metadata
            meta = _metadata(rec[8:])
    book = Book(Metadata(title=meta.get("title") or title))
    book.source_format = "pdb"
    if meta.get("author"):
        book.metadata.authors = [meta["author"]]
    order = ([home] if home in texts else []) + sorted(u for u in texts if u != home)
    hrefs = {uid: f"text/page{uid:05d}.xhtml" for uid in order}
    for uid in order:
        root, body = markup.new_document(book.metadata.title, "")
        _text(texts[uid], body, hrefs, hrefs[uid])
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


def _text(data: bytes, body, hrefs, own_href) -> None:
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
