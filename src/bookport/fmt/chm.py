"""Compiled HTML Help (CHM): read.

The container: an ITSF header, an ITSP directory of PMGL chunks (name, section, offset, length
per file), section 0 stored as is and section 1 ("MSCompressed") in LZX with a reset table.
The book: pages in the order of the .hhc contents file (a sitemap of nested lists, which also
gives the TOC), then any other pages; images and stylesheets as resources; the title from #SYSTEM.
"""

from __future__ import annotations

import posixpath
import struct
from pathlib import Path
from urllib.parse import unquote

from .. import limits, markup
from ..book import Book, Metadata, TocEntry, guess_type, is_external, normalize_href, relative, resolve
from ..errors import BookportError
from ..lzx import FRAME, LzxDecoder

RESET_TABLE = "::DataSpace/Storage/MSCompressed/Transform/{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}/InstanceData/ResetTable"
CONTROL = "::DataSpace/Storage/MSCompressed/ControlData"
CONTENT = "::DataSpace/Storage/MSCompressed/Content"


# Windows ANSI code pages by primary language (the low 10 bits of an LCID); everything else is cp1252
_CODEPAGES = {0x11: "cp932", 0x12: "cp949", 0x19: "cp1251", 0x22: "cp1251", 0x02: "cp1251", 0x23: "cp1251",
              0x2F: "cp1251", 0x08: "cp1253", 0x1F: "cp1254", 0x0D: "cp1255", 0x01: "cp1256", 0x29: "cp1256",
              0x20: "cp1256", 0x25: "cp1257", 0x26: "cp1257", 0x27: "cp1257", 0x2A: "cp1258", 0x1E: "cp874",
              0x05: "cp1250", 0x15: "cp1250", 0x0E: "cp1250", 0x1B: "cp1250", 0x24: "cp1250", 0x18: "cp1250",
              0x1C: "cp1250", 0x1A: "cp1250"}


def codepage(lcid: int) -> str:
    """The code page for a Windows language id (the one in a CHM's #SYSTEM)."""
    lang = lcid & 0x3FF
    if lang == 0x04:  # Chinese: traditional for Taiwan, Hong Kong and Macao, simplified otherwise
        return "cp950" if lcid in (0x0404, 0x0C04, 0x1404) else "gbk"
    if lcid in (0x0C1A, 0x1C1A, 0x201A):  # Serbian and Bosnian in Cyrillic
        return "cp1251"
    return _CODEPAGES.get(lang, "cp1252")


def _encint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    return value, pos


class Chm:
    def __init__(self, data: bytes):
        if data[:4] != b"ITSF":
            raise BookportError("Not a CHM file")
        self.data = data
        version = struct.unpack("<I", data[4:8])[0]
        dir_off, dir_len = struct.unpack("<QQ", data[72:88])
        self.content_off = struct.unpack("<Q", data[88:96])[0] if version >= 3 else dir_off + dir_len
        self.files: dict[str, tuple[int, int, int]] = {}
        self._read_directory(dir_off)
        self._section1: bytes | None = None
        self._lower: dict[str, str] | None = None

    def _read_directory(self, off: int):
        d = self.data
        if d[off:off + 4] != b"ITSP":
            raise BookportError("Damaged CHM: no directory")
        header_len = struct.unpack("<I", d[off + 8:off + 12])[0]
        chunk_size = struct.unpack("<I", d[off + 16:off + 20])[0]
        first, last = struct.unpack("<ii", d[off + 32:off + 40])
        n_chunks = struct.unpack("<I", d[off + 44:off + 48])[0]
        base = off + header_len
        if chunk_size < 20:
            raise BookportError("Damaged CHM: bad directory chunk size")
        n_chunks = min(n_chunks, max(0, len(d) - base) // chunk_size)
        last = min(last, n_chunks - 1)
        chunk_ids = range(n_chunks) if first < 0 else range(first, (last if last >= 0 else n_chunks - 1) + 1)
        for c in chunk_ids:
            chunk = d[base + c * chunk_size:base + (c + 1) * chunk_size]
            if chunk[:4] != b"PMGL":
                continue
            free = struct.unpack("<I", chunk[4:8])[0]
            end = chunk_size - free
            pos = 20
            while pos < end:
                n, pos = _encint(chunk, pos)
                if n == 0 or pos + n > len(chunk):
                    break
                name = chunk[pos:pos + n].decode("utf-8", "replace")
                pos += n
                section, pos = _encint(chunk, pos)
                offset, pos = _encint(chunk, pos)
                length, pos = _encint(chunk, pos)
                self.files[name] = (section, offset, length)

    def _section1_data(self) -> bytes:
        if self._section1 is None:
            content = self._raw(CONTENT)
            control = self._raw(CONTROL)
            table = self._raw(RESET_TABLE)
            if not content or len(control) < 24 or len(table) < 40:
                raise BookportError("Damaged CHM: no compressed section data")
            version, reset_interval, window = struct.unpack("<III", control[8:20])
            if version == 2:
                reset_interval *= 0x8000
                window *= 0x8000
            window_bits = max(15, (window - 1).bit_length())
            n_entries, _entry_size, header_len = struct.unpack("<III", table[4:16])
            total, _comp_len, block = struct.unpack("<QQQ", table[16:40])
            n_entries = min(n_entries, max(0, len(table) - header_len) // 8)
            total = min(total, limits.MAX_PART)
            entries = [struct.unpack("<Q", table[header_len + 8 * i:header_len + 8 * i + 8])[0] for i in range(n_entries)]
            frames_per_reset = max(1, reset_interval // (block or FRAME))
            dec = LzxDecoder(window_bits)
            out = bytearray()
            k = 0
            while len(out) < total and k < len(entries):
                start = entries[k]
                nxt = k + frames_per_reset
                end = entries[nxt] if nxt < len(entries) else len(content)
                want = min(frames_per_reset * FRAME, total - len(out))
                dec.reset()
                piece = dec.decompress(content[start:end], want)
                limits.charge(len(piece))
                out += piece
                k = nxt
            self._section1 = bytes(out)
        return self._section1

    def _raw(self, name: str) -> bytes:
        entry = self.files.get(name)
        if entry is None or entry[0] != 0:
            return b""
        _s, off, length = entry
        start = self.content_off + off
        return self.data[start:start + length]

    def read(self, name: str) -> bytes | None:
        entry = self.files.get(name)
        if entry is None:
            if self._lower is None:
                self._lower = {k.lower(): k for k in self.files}
            real = self._lower.get(name.lower())
            if real is None:
                return None
            entry = self.files[real]
        section, off, length = entry
        if section == 0:
            start = self.content_off + off
            return self.data[start:start + length]
        data = self._section1_data()
        return data[off:off + length]


def read(path) -> Book:
    chm = Chm(Path(path).read_bytes())
    stem = Path(path).stem
    book = Book(Metadata(title=stem))
    book.source_format = "chm"
    system = chm.read("/#SYSTEM") or b""
    entries = []
    pos = 4
    while pos + 4 <= len(system):
        code, length = struct.unpack("<HH", system[pos:pos + 4])
        entries.append((code, system[pos + 4:pos + 4 + length]))
        pos += 4 + length
    lcid = next((struct.unpack("<I", v[:4])[0] for c, v in entries if c == 4 and len(v) >= 4), 0)
    cp = codepage(lcid)  # the text of a CHM is in its language's ANSI code page
    hhc_name = default = ""
    for code, raw in entries:
        value = raw.split(b"\0", 1)[0].decode(cp, "replace")
        if code == 3 and value:
            book.metadata.title = value
        elif code == 2:
            default = value
        elif code == 0:
            hhc_name = value
    names = [n for n in chm.files if n.startswith("/") and not n.startswith(("/#", "/$")) and not n.endswith("/")]
    if not hhc_name:
        hhc_name = next((n[1:] for n in names if n.lower().endswith(".hhc")), "")
    pages = [n for n in names if n.lower().endswith((".htm", ".html", ".xhtml"))]
    # resources first, so pages can point at them
    hrefs = {}
    for n in names:
        if n in pages or n.lower().endswith((".hhc", ".hhk")):
            continue
        mt = guess_type(n)
        if mt.startswith("image/") or mt == "text/css":
            data = chm.read(n)
            if data:
                hrefs[n] = book.add(normalize_href(n), data, mt, unique=False).href
    order: dict[str, None] = {}  # pages in TOC order, each once
    toc: list[TocEntry] = []
    if hhc_name:
        hhc = chm.read("/" + hhc_name.lstrip("/"))
        if hhc:
            toc = _sitemap(markup.decode(hhc, cp), posixpath.dirname("/" + hhc_name.lstrip("/")))
            for _d, e in [x for t in toc for x in t.walk()]:
                page = e.href.split("#")[0]
                if page:
                    order.setdefault(page)
    if default and "/" + default.lstrip("/") not in order:
        order = {"/" + default.lstrip("/"): None, **order}
    page_set = {p.lower(): p for p in pages}
    spine = list(dict.fromkeys(page_set[p.lower()] for p in order if p.lower() in page_set))
    in_spine = set(spine)
    spine += sorted(p for p in pages if p not in in_spine)
    added, raw_pages = {}, {}
    for name in spine:
        data = chm.read(name)
        if data is None:
            continue
        href = normalize_href(name)
        if not href.lower().endswith(".xhtml"):
            href = posixpath.splitext(href)[0] + ".xhtml"
        added[name], raw_pages[name] = href, data
    # names in a CHM aren't case-sensitive: <a href="intro.htm"> finds /Pages/Intro.htm
    added_lower = {k.lower(): k for k in reversed(added)}
    hrefs_lower = {k.lower(): v for k, v in reversed(hrefs.items())}
    for name, href in added.items():
        root = markup.parse(markup.decode(raw_pages.pop(name), cp))
        base = normalize_href(name)
        for el in root.iter():
            for attr in ("href", "src"):
                v = el.get(attr)
                if not v or is_external(v) or v.startswith(("#", "javascript:")):
                    continue
                link, _, frag = unquote(v).partition("#")
                link = link.split("::/")[-1] if "::/" in link else link
                target = ("/" + resolve(base, link)).lower()
                if target in added_lower:
                    el.set(attr, relative(href, added[added_lower[target]] + (f"#{frag}" if frag else "")))
                elif target in hrefs_lower:
                    el.set(attr, relative(href, hrefs_lower[target]))
        if book.metadata.title == stem:
            t = root.find("head/title")
            if t is not None and (t.text or "").strip():
                book.metadata.title = " ".join(t.text.split())
        book.add_document(href, root)

    def fix(entries):
        out = []
        for e in entries:
            page, _, frag = e.href.partition("#")
            kids = fix(e.children)
            match = added_lower.get(page.lower())
            if match is not None:
                out.append(TocEntry(e.title, added[match] + (f"#{frag}" if frag else ""), kids))
            elif kids:
                out.append(TocEntry(e.title, kids[0].href, kids))
        return out

    book.toc = fix(toc)
    if not book.spine:
        raise BookportError("This CHM file has no pages")
    if not book.toc:
        book.ensure_toc()
    return book


def _sitemap(html: str, base_dir: str) -> list[TocEntry]:
    """The nested <UL><LI><OBJECT type="text/sitemap"> lists of an .hhc file."""
    root = markup.parse(html)
    body = root.find("body")

    def items(ul):
        out = []
        last = None
        for child in ul:
            if child.tag == "li":
                obj = child.find(".//object")
                if obj is not None:
                    params = {p.get("name", "").lower(): p.get("value", "") for p in obj.iter("param")}
                    local = params.get("local", "")
                    name = params.get("name", "") or local
                    href = ""
                    if local and not is_external(local):
                        local = local.split("::/")[-1]
                        href = "/" + normalize_href(posixpath.join(base_dir.lstrip("/"), unquote(local)))
                    last = TocEntry(name, href)
                    out.append(last)
                for sub in child:
                    if sub.tag == "ul" and last is not None:
                        last.children.extend(items(sub))
            elif child.tag == "ul" and last is not None:
                last.children.extend(items(child))
        return out

    first = next((u for u in body.iter("ul")), None) if body is not None else None
    return items(first) if first is not None else []


__all__ = ["Chm", "read"]
