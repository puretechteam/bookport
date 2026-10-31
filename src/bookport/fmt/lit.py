"""Microsoft Reader (LIT): read (writing is in lit_write.py).

Container: an ITOLITLS header with a piece table; an IFCM directory of AOLL chunks (name,
section, offset, length per file); section 0 stored, section 1 ("MSCompressed") in LZX with a
reset table; sections 2 and 3 encrypted. The package (/meta) and each document's /content are
tokenized XML: NUL, a flag byte (open, close, block, head), a tag number, attributes as numbers
with length-prefixed string values (or numbers after U+FFFF), then NUL; text in between. Tag
numbers for documents follow the alphabetical list of HTML tags; the package uses OEB 1.0
element numbers. Books whose content is encrypted (any DRM level) raise DRMError.
"""

from __future__ import annotations

import posixpath
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import limits, markup
from ..book import CSS, IMAGE_EXT, Book, Metadata, TocEntry, relative, sniff_image
from ..errors import BookportError, DRMError
from ..lzx import FRAME, LzxDecoder

HTML_TAGS = (
    [None, None, None] + ["a", "acronym", "address", "applet", "area", "b", "base", "basefont", "bdo", "bgsound", "big", "blink", "blockquote", "body", "br", "button", "caption", "center", "cite", "code", "col", "colgroup", "comment", "custom", "dd", "del", "dfn", "dir", "div", "dl", "dt", "em", "embed", "fieldset", "font", "form", "frame", "frameset", "unknown41", "h1", "h2", "h3", "h4", "h5", "h6", "head", "hr", "html", "i", "iframe", "img", "input", "ins", "kbd", "label", "legend", "li", "link", "listing", "map", "marquee", "menu", "meta", "nextid", "nobr", "noembed", "noframes", "noscript", "object", "ol", "option", "p", "param", "plaintext", "pre", "q", "rp", "rt", "ruby", "s", "samp", "script", "select", "small", "span", "strike", "strong", "style", "sub", "sup", "table", "tbody", "tc", "td", "textarea", "tfoot", "th", "thead", "title", "tr", "tt", "u", "ul", "var", "wbr", "xmp"])
GLOBAL_ATTRS = {0x3EA: "class", 0x3EB: "id", 0x46: "title", 0x1392: "lang", 0x13FE: "dir", 0x01: "name"}
TAG_ATTRS = {
    "a": {0x01: "href"}, "img": {0x3EC: "src", 0x3EB: "alt", 0x3EA: "align"}, "link": {0x3EE: "href", 0x3EF: "rel", 0x3F1: "type"},
    "meta": {0x3EA: "http-equiv", 0x3EB: "content"}, "td": {0x7D3: "colspan", 0x7D2: "rowspan"},
    "th": {0x7D3: "colspan", 0x7D2: "rowspan"}, "li": {0x3EA: "value"}, "ol": {0x3EA: "start"},
}
OPF_TAGS = {1: "package", 2: "dc:Title", 3: "dc:Creator", 16: "manifest", 17: "item", 18: "spine", 19: "itemref",
            20: "metadata", 21: "dc-metadata", 22: "dc:Subject", 23: "dc:Description", 24: "dc:Publisher",
            25: "dc:Contributor", 26: "dc:Date", 27: "dc:Type", 28: "dc:Format", 29: "dc:Identifier", 30: "dc:Source",
            31: "dc:Language", 32: "dc:Relation", 33: "dc:Coverage", 34: "dc:Rights", 35: "x-metadata", 36: "meta",
            37: "tours", 38: "tour", 39: "site", 40: "guide", 41: "reference"}
OPF_ATTRS = {0x03: "guid", 0x06: "id", 0x07: "href", 0x08: "media-type", 0x0A: "idref", 0x0B: "xmlns:dc", 0x0C: "xmlns:oeb",
             0x0D: "role", 0x0E: "file-as", 0x10: "scheme", 0x11: "title", 0x12: "type", 0x13: "unique-identifier",
             0x14: "name", 0x15: "content"}
FLAG_OPEN, FLAG_CLOSE = 1, 2


def _encint(b: bytes, p: int) -> tuple[int, int]:
    v = 0
    while p < len(b):
        c = b[p]
        p += 1
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            break
    return v, p


class Lit:
    def __init__(self, data: bytes):
        if data[:8] != b"ITOLITLS":
            raise BookportError("Not a LIT file")
        self.data = data
        hlen, npieces = struct.unpack("<II", data[12:20])
        npieces = min(npieces, max(0, len(data) - hlen) // 16)
        pieces = [struct.unpack("<IIII", data[hlen + 16 * i:hlen + 16 * i + 16])[::2] for i in range(npieces)]
        if len(pieces) < 2:
            raise BookportError("Damaged LIT file: no directory")
        self.content_start = pieces[-1][0] + pieces[-1][1]
        self.files: dict[str, tuple[int, int, int]] = {}
        off, size = pieces[1]
        d = data[off:off + size]
        if d[:4] != b"IFCM":
            raise BookportError("Damaged LIT file: bad directory")
        chunk_size, nchunks = struct.unpack("<I", d[8:12])[0], struct.unpack("<I", d[24:28])[0]
        if chunk_size < 48:
            raise BookportError("Damaged LIT file: bad directory chunk size")
        nchunks = min(nchunks, max(0, len(d) - 32) // chunk_size)
        for c in range(nchunks):
            ch = d[32 + c * chunk_size:32 + (c + 1) * chunk_size]
            if ch[:4] != b"AOLL":
                continue
            quick = struct.unpack("<I", ch[4:8])[0]
            pos = 48
            while pos < chunk_size - quick:
                n, pos = _encint(ch, pos)
                if n == 0:
                    break
                name = ch[pos:pos + n].decode("utf-8", "replace")
                pos += n
                section, pos = _encint(ch, pos)
                offset, pos = _encint(ch, pos)
                length, pos = _encint(ch, pos)
                self.files[name] = (section, offset, length)
        if "/DRMStorage/Licenses/EUL" in self.files or "/DRMStorage/DRMBookplate" in self.files:
            raise DRMError("LIT", "it is an owner-exclusive or inscribed Microsoft Reader book")
        self._sections: dict[int, bytes] = {}
        self.section_names = self._names()

    def _names(self) -> list[str]:
        raw = self.raw("::DataSpace/NameList")
        out = []
        try:
            count = struct.unpack("<H", raw[2:4])[0]
            pos = 4
            for _ in range(count):
                n = struct.unpack("<H", raw[pos:pos + 2])[0]
                out.append(raw[pos + 2:pos + 2 + 2 * n].decode("utf-16-le"))
                pos += 2 + 2 * n + 2
        except (struct.error, UnicodeDecodeError):
            out = ["Uncompressed", "MSCompressed", "EbEncryptDS", "EbEncryptOnlyDS"]
        return out

    def raw(self, name: str) -> bytes:
        entry = self.files.get(name)
        if entry is None or entry[0] != 0:
            return b""
        start = self.content_start + entry[1]
        return self.data[start:start + entry[2]]

    def section(self, n: int) -> bytes:
        if n in self._sections:
            return self._sections[n]
        name = self.section_names[n] if n < len(self.section_names) else ""
        if name != "MSCompressed":
            raise DRMError("LIT", "its content is encrypted (Microsoft Reader DRM)")
        base = f"::DataSpace/Storage/{name}"
        content = self.raw(f"{base}/Content")
        control = self.raw(f"{base}/ControlData")
        table = self.raw(f"{base}/Transform/{{0A9007C6-4076-11D3-8789-0000F8105754}}/InstanceData/ResetTable")
        if not content or len(control) < 20 or len(table) < 40:
            raise BookportError("Damaged LIT file: no compressed section")
        _n, _magic, version, reset, window = struct.unpack("<IIIII", control[:20])
        frames_per_reset = reset if version >= 2 else max(1, reset // FRAME)
        window_bits = max(15, (window * FRAME - 1).bit_length()) if version >= 2 else max(15, (window - 1).bit_length())
        count = struct.unpack("<I", table[4:8])[0]
        header_len = struct.unpack("<I", table[12:16])[0]
        total = min(struct.unpack("<Q", table[16:24])[0], limits.MAX_PART)
        count = min(count, max(0, len(table) - header_len) // 8)
        entries = [struct.unpack("<Q", table[header_len + 8 * i:header_len + 8 * i + 8])[0] for i in range(count)]
        dec = LzxDecoder(window_bits)
        out = bytearray()
        k = 0
        while len(out) < total and k < len(entries):
            nxt = k + frames_per_reset
            end = entries[nxt] if nxt < len(entries) else len(content)
            dec.reset()
            piece = dec.decompress(content[entries[k]:end], min(frames_per_reset * FRAME, total - len(out)))
            limits.charge(len(piece))
            out += piece
            k = nxt
        self._sections[n] = bytes(out)
        return self._sections[n]

    def get(self, name: str) -> bytes | None:
        entry = self.files.get(name)
        if entry is None:
            return None
        section, off, length = entry
        if section == 0:
            return self.raw(name)
        return self.section(section)[off:off + length]


def tokens(data: bytes):
    """('open', flags, tag, [(attr, value)]), ('close', flags) and ('text', str) from tokenized XML."""
    text = data.decode("utf-8", "surrogatepass")
    i, n = 0, len(text)
    buf: list[str] = []
    while i < n:
        c = text[i]
        if c != "\x00":
            buf.append(c)
            i += 1
            continue
        if buf:
            yield ("text", "".join(buf))
            buf = []
        if i + 1 >= n:
            break
        flags = ord(text[i + 1])
        i += 2
        tag = None
        if flags & FLAG_OPEN:
            tag = ord(text[i]) if i < n else 0
            i += 1
            if tag == 0x8000 and i < n:
                ln = ord(text[i]) - 1
                tag = text[i + 1:i + 1 + ln]
                i += 1 + ln
        attrs = []
        while i < n and text[i] != "\x00":
            aid = ord(text[i])
            i += 1
            name = None
            if aid == 0x8000 and i < n:
                ln = ord(text[i]) - 1
                name = text[i + 1:i + 1 + ln]
                i += 1 + ln
            if i < n and text[i] == "￿":
                value = str(ord(text[i + 1]) - 1) if i + 1 < n else ""
                i += 2
            elif i < n:
                ln = ord(text[i]) - 1
                value = text[i + 1:i + 1 + ln]
                i += 1 + max(0, ln)
            else:
                value = ""
            attrs.append((name if name is not None else aid, value))
        i += 1
        if flags & FLAG_OPEN:
            yield ("open", flags, tag, attrs)
        elif flags & FLAG_CLOSE:
            yield ("close", flags)
    if buf:
        yield ("text", "".join(buf))


def _package(data: bytes) -> ET.Element:
    """The /meta package as an element tree (OEB 1.0 names)."""
    root = ET.Element("lit-package")
    stack = [root]
    for tok in tokens(data):
        if tok[0] == "open":
            _k, flags, tag, attrs = tok
            name = OPF_TAGS.get(tag, f"x{tag}") if isinstance(tag, int) else tag
            el = ET.SubElement(stack[-1], name.replace(":", "_"))
            for a, v in attrs:
                key = OPF_ATTRS.get(a, f"a{a}") if isinstance(a, int) else a
                el.set(key.replace(":", "_"), v)
            if not flags & FLAG_CLOSE:
                stack.append(el)
        elif tok[0] == "close":
            if len(stack) > 1:
                stack.pop()
        else:
            markup.append_text(stack[-1], tok[1])
    return root


def read(path) -> Book:
    lit = Lit(Path(path).read_bytes())
    meta = lit.get("/meta")
    if not meta:
        raise BookportError("Damaged LIT file: no package")
    pkg = _package(meta)
    book = Book(Metadata(title=Path(path).stem))
    book.source_format = "lit"
    md = book.metadata

    def texts(tag):
        return [" ".join((e.text or "").split()) for e in pkg.iter(tag) if (e.text or "").strip()]

    md.title = (texts("dc_Title") or [md.title])[0]
    for e in pkg.iter("dc_Creator"):
        if (e.text or "").strip() and e.get("role", "aut") in ("aut", ""):
            md.authors.append(" ".join(e.text.split()))
        elif (e.text or "").strip():
            md.contributors.append(" ".join(e.text.split()))
    md.contributors = [c for c in md.contributors if "calibre" not in c.lower()]
    md.publisher = (texts("dc_Publisher") or [""])[0]
    md.description = (texts("dc_Description") or [""])[0]
    md.subjects = texts("dc_Subject")
    md.date = (texts("dc_Date") or [""])[0]
    md.rights = (texts("dc_Rights") or [""])[0]
    lang = (texts("dc_Language") or [""])[0]
    md.language = {"eng": "en", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de", "spa": "es", "ita": "it",
                   "jpn": "ja", "zho": "zh", "chi": "zh"}.get(lang.lower(), lang)
    for e in pkg.iter("dc_Identifier"):
        if (e.text or "").strip():
            md.identifiers.setdefault((e.get("scheme") or "id").lower(), e.text.strip())
    for e in pkg.iter("meta"):
        name, content = e.get("name", ""), e.get("content", "")
        if name.endswith("}series"):
            md.series = content
        elif name.endswith("}series_index"):
            try:
                md.series_index = float(content) if "." in content else float(content) + 0
            except ValueError:
                pass
    # manifest: documents are /data/<id>/content, everything else /data/<id>
    items = {}
    for e in pkg.iter("item"):
        iid, href, mt = e.get("id", ""), e.get("href", "").lstrip("\x02"), e.get("media-type", "")
        items[iid] = (href or iid, mt)
    hrefs: dict[str, str] = {}
    for iid, (href, mt) in items.items():
        if "oeb1-document" in mt or mt in ("application/xhtml+xml", "text/html"):
            ext = ".xhtml"
            hrefs[iid] = "text/" + re.sub(r"[^\w.-]", "_", posixpath.splitext(posixpath.basename(href))[0] or iid) + ext
        elif "css" in mt:
            hrefs[iid] = "styles/" + re.sub(r"[^\w.-]", "_", iid) + ".css"
        else:
            data = lit.get(f"/data/{iid}")
            if data is None:
                continue
            kind = sniff_image(data)
            if kind is None:
                continue
            base = re.sub(r"[^\w.-]", "_", posixpath.splitext(posixpath.basename(href))[0] or iid)
            hrefs[iid] = book.add(f"images/{base}{IMAGE_EXT.get(kind, '.bin')}", data, kind).href
    for iid, (href, mt) in items.items():
        if "css" in mt and iid in hrefs:
            data = lit.get(f"/data/{iid}") or b""
            book.add(hrefs[iid], data, CSS, unique=False)
    spine_ids = [e.get("idref", "") for e in pkg.iter("itemref")]
    docs = list(dict.fromkeys(i for i in spine_ids if i in items and hrefs.get(i, "").startswith("text/")))
    docs += [i for i, (h, mt) in items.items() if i not in docs and "oeb1-document" in mt]
    for iid in docs:
        data = lit.get(f"/data/{iid}/content")
        if data is None:
            continue
        root = _document(data, hrefs[iid], hrefs, items)
        book.add_document(hrefs[iid], root)

    def target(ref: str) -> str:
        ref = ref.lstrip("\x02")
        iid, _, frag = ref.partition("#")
        h = hrefs.get(iid)
        return (h + (f"#{frag}" if frag else "")) if h else ""

    for tour in pkg.iter("tour"):
        for site in tour.iter("site"):
            h = target(site.get("href", ""))
            if h:
                book.toc.append(TocEntry(site.get("title", "") or "Untitled", h))
        if book.toc:
            break
    from ..book import Landmark
    from .epub import _guide_type
    for ref in pkg.iter("reference"):
        h = target(ref.get("href", ""))
        kind = ref.get("type", "")
        res = book.resources.get(h.split("#")[0]) if h else None
        if kind == "cover" and res is not None and res.is_image:
            book.cover = res.href
        elif h and kind:
            book.landmarks.append(Landmark(_guide_type(kind.replace("other.", "")), ref.get("title", ""), h))
    cover_meta = next((e.get("content") for e in pkg.iter("meta") if e.get("name") == "cover"), None)
    if book.cover is None and cover_meta in hrefs and book.resources.get(hrefs[cover_meta]) is not None:
        book.cover = hrefs[cover_meta]
    if not book.spine:
        raise BookportError("This LIT file has no documents")
    from ..toc import fix_links
    fix_links(book)
    if not book.toc:
        book.ensure_toc()
    return book


def _document(data: bytes, href: str, hrefs: dict, items: dict) -> ET.Element:
    root = ET.Element("html")
    stack = [root]
    for tok in tokens(data):
        if tok[0] == "open":
            _k, flags, tag, attrs = tok
            if isinstance(tag, int):
                name = HTML_TAGS[tag] if 0 <= tag < len(HTML_TAGS) and HTML_TAGS[tag] else "span"
            else:
                name = tag or "span"
            if name in ("html",) and stack[-1] is root and not len(root) and root.tag == "html":
                el = root
            else:
                el = ET.SubElement(stack[-1], name)
            for a, v in attrs:
                if isinstance(a, str):
                    if a in ("xml:lang", "lang"):
                        el.set("lang", v)
                    continue
                key = (a & 0x7FFF) if a & 0x8000 else None
                attr = GLOBAL_ATTRS.get(key) if key is not None else TAG_ATTRS.get(name, {}).get(a)
                if attr is None:
                    continue
                if attr in ("href", "src") and v.startswith("\x02"):
                    iid, _, frag = v[1:].partition("#")
                    t = hrefs.get(iid)
                    if t is None:
                        continue
                    v = relative(href, t + (f"#{frag}" if frag else ""))
                elif attr == "href" and v.startswith("\x03"):
                    v = v[1:]
                el.set(attr, v)
            if el is not root and not flags & FLAG_CLOSE:
                stack.append(el)
        elif tok[0] == "close":
            if len(stack) > 1:
                stack.pop()
        else:
            markup.append_text(stack[-1], tok[1])
    return markup.parse(markup.serialize(root))


def write(book: Book, path) -> None:
    from . import lit_write
    lit_write.write(book, path)
