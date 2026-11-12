"""Write Microsoft Reader (LIT) files.

The layout mirrors what LIT readers expect: an ITOLITLS header and piece table, a secondary
header (CAOL and ITSF blocks), one IFCM/AOLL directory chunk, an entry-count chunk, section 0
(stored files: package, manifest, stylesheets, images, anchor tables) and section 1
("MSCompressed": the documents, as LZX made of stored blocks, reset every frame). Documents and
the package are written in LIT's tokenized XML (see lit.py). No DRM is applied.
"""

from __future__ import annotations

import posixpath
import re
import struct
import time
import uuid
import xml.etree.ElementTree as ET

from ..book import CSS, Book, is_external, resolve
from ..lzx import FRAME, compress_stored
from .lit import FLAG_CLOSE, FLAG_OPEN, HTML_TAGS

FLAG_BLOCK, FLAG_HEAD = 4, 8
LZX_GUID = bytes.fromhex("c607900a7640d31187890000f8105754")
LZX_GUID_TEXT = "{0A9007C6-4076-11D3-8789-0000F8105754}"
TAG_NUMBERS = {name: k for k, name in enumerate(HTML_TAGS) if name}
BLOCK_TAGS = frozenset(["address", "blockquote", "body", "caption", "center", "dd", "div", "dl", "dt", "h1", "h2", "h3",
                        "h4", "h5", "h6", "hr", "html", "li", "ol", "p", "pre", "table", "tbody", "td", "tfoot", "th",
                        "thead", "tr", "ul"])
HEAD_TAGS = frozenset(["head", "title", "meta", "link", "style", "base"])
OPF = {"package": 1, "dc:Title": 2, "dc:Creator": 3, "manifest": 16, "item": 17, "spine": 18, "itemref": 19, "metadata": 20,
       "dc-metadata": 21, "dc:Subject": 22, "dc:Description": 23, "dc:Publisher": 24, "dc:Contributor": 25, "dc:Date": 26,
       "dc:Identifier": 29, "dc:Language": 31, "dc:Rights": 34, "x-metadata": 35, "meta": 36, "tours": 37, "tour": 38,
       "site": 39, "guide": 40, "reference": 41}
OPF_ATTR = {"guid": 0x03, "version": 0x04, "id": 0x06, "href": 0x07, "media-type": 0x08, "idref": 0x0A, "xmlns:dc": 0x0B,
            "xmlns:oebpackage": 0x0C, "role": 0x0D, "file-as": 0x0E, "scheme": 0x10, "title": 0x11, "type": 0x12,
            "unique-identifier": 0x13, "name": 0x14, "content": 0x15}


def _encint(v: int) -> bytes:
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    return bytes(reversed(out))


def _small_number(v: str) -> bool:
    """A plain number a token can hold ("²" passes isdigit() but isn't one)."""
    return v.isascii() and v.isdigit() and int(v) < 0xFFFE


class _Tokens:
    """Builds tokenized XML as a list of code points (encoded to UTF-8 at the end)."""

    def __init__(self):
        self.out: list[str] = []
        self.anchors: list[tuple[str, int]] = []
        self._counted = 0  # pieces of out already in _bytes
        self._bytes = 0

    def chars(self, s: str):
        self.out.append(s.replace("\x00", ""))

    def string(self, s: str):
        self.out.append(chr(len(s) + 1) + s)

    def number(self, n: int):
        self.out.append(chr(0xFFFF) + chr(n + 1))

    def open(self, flags: int, tag, attrs: list[tuple[object, object]]):
        self.out.append("\x00" + chr(flags))
        if isinstance(tag, str):
            self.out.append(chr(0x8000))
            self.string(tag)
        else:
            self.out.append(chr(tag))
        for aid, value in attrs:
            if isinstance(aid, str):
                self.out.append(chr(0x8000))
                self.string(aid)
            else:
                self.out.append(chr(aid))
            if isinstance(value, int):
                self.number(value)
            else:
                self.string(value)
        self.out.append("\x00")

    def close(self, flags: int):
        self.out.append("\x00" + chr(flags) + "\x00")

    def size(self) -> int:
        """Bytes so far, counting only what was added since the last call (an id in every paragraph made
        writing quadratic: 4,000 ids took 1.9 s)."""
        new = self.out[self._counted:]
        self._bytes += sum(len(s.encode("utf-8", "surrogatepass")) for s in new)
        self._counted = len(self.out)
        return self._bytes

    def data(self) -> bytes:
        return "".join(self.out).encode("utf-8", "surrogatepass")


class _Writer:
    def __init__(self, book: Book):
        self.book = book
        self.ids: dict[str, str] = {}
        self.alias: dict[str, str] = {}  # an SVG that only wraps a picture -> that picture
        taken = set()
        for href in book.resources:
            base = re.sub(r"[^\w.-]", "_", posixpath.basename(href)) or "item"
            iid = base
            n = 1
            while iid in taken:
                iid = f"{n}_{base}"
                n += 1
            self.ids[href] = iid
            taken.add(iid)

    def link(self, doc_href: str, value: str) -> str | None:
        if is_external(value):
            return "\x03" + value
        path, _, frag = value.partition("#")
        target = resolve(doc_href, path) if path else doc_href
        target = self.alias.get(target, target)
        iid = self.ids.get(target)
        if iid is None:
            return None
        return "\x02" + iid + (f"#{frag}" if frag else "")

    def document(self, href: str, root: ET.Element) -> tuple[bytes, list[tuple[str, int]]]:
        t = _Tokens()
        anchors: list[tuple[str, int]] = []

        def walk(el, in_head):
            tag = el.tag if isinstance(el.tag, str) else None
            if tag is None:
                return
            head = in_head or tag in HEAD_TAGS or tag == "head"
            number = TAG_NUMBERS.get(tag)
            flags = FLAG_OPEN | (FLAG_BLOCK if tag in BLOCK_TAGS or head else 0) | (FLAG_HEAD if head else 0)
            empty = not len(el) and not el.text and tag in ("br", "hr", "img", "meta", "link", "a", "col")
            if empty:
                flags |= FLAG_CLOSE
            attrs = []
            for k, v in el.attrib.items():
                if k == "id":
                    anchors.append((v, t.size()))
                    attrs.append((0x83EB, v))
                elif k == "class":
                    attrs.append((0x83EA, v))
                elif k in ("lang", "xml:lang"):
                    if k == "lang":
                        attrs.append((0x9392, v))
                elif k == "title" and tag not in ("link",):
                    attrs.append((0x8046, v))
                elif k == "dir":
                    attrs.append((0x93FE, v))
                elif tag == "a" and k == "href":
                    target = self.link(href, v)
                    if target:
                        attrs.append((0x01, target))
                elif tag == "img" and k == "src":
                    target = self.link(href, v)
                    if target:
                        attrs.append((0x3EC, target))
                elif tag == "img" and k == "alt":
                    attrs.append((0x3EB, v))
                elif tag == "link" and k == "href":
                    target = self.link(href, v)
                    if target:
                        attrs.append((0x3EE, target))
                elif tag == "link" and k in ("rel", "type"):
                    attrs.append((0x3EF if k == "rel" else 0x3F1, v))
                elif tag in ("td", "th") and k in ("colspan", "rowspan") and _small_number(v):
                    attrs.append((0x7D3 if k == "colspan" else 0x7D2, int(v)))
                elif tag == "li" and k == "value" and _small_number(v):
                    attrs.append((0x3EA, int(v)))
                elif k in ("style", "width", "height", "start", "type", "name", "colspan", "rowspan", "align"):
                    attrs.append((k, v))
            if tag == "meta":
                return  # charset declarations aren't needed: the content is UTF-8
            t.open(flags, number if number is not None else tag, attrs)
            if empty:
                return
            if el.text:
                t.chars(el.text)
            for child in el:
                walk(child, head)
                if child.tail:
                    t.chars(child.tail)
            t.close(FLAG_CLOSE | (flags & (FLAG_BLOCK | FLAG_HEAD)))

        walk(root, False)
        return t.data(), anchors

    def package(self, docs: list[str], others: list[str], css: list[str], images: list[str], toc) -> bytes:
        md = self.book.metadata
        t = _Tokens()

        def el(name, attrs=(), text=None, empty=False):
            a = [(OPF_ATTR[k], v) for k, v in attrs]
            if empty:
                t.open(FLAG_OPEN | FLAG_CLOSE, OPF[name], a)
                return
            t.open(FLAG_OPEN, OPF[name], a)
            if text is not None:
                t.chars(text)
                t.close(FLAG_CLOSE)

        uid = md.uid()
        el("package", [("unique-identifier", "uuid_id"), ("version", 1), ("guid", "{" + str(uuid.uuid4()).upper() + "}")])
        el("metadata")
        el("dc-metadata", [("xmlns:dc", "http://purl.org/dc/elements/1.1/"),
                           ("xmlns:oebpackage", "http://openebook.org/namespaces/oeb-package/1.0/")])
        lang = {"en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "ja": "jpn", "zh": "zho"}.get(
            (md.language or "en").split("-")[0], md.language or "eng")
        el("dc:Language", text=lang)
        el("dc:Title", text=md.title or "Untitled")
        for a in md.authors:
            el("dc:Creator", [("file-as", md.author_sort or a), ("role", "aut")], text=a)
        if md.description:
            el("dc:Description", text=re.sub(r"<[^>]+>", "", md.description))
        if md.publisher:
            el("dc:Publisher", text=md.publisher)
        if md.identifiers.get("isbn"):
            el("dc:Identifier", [("scheme", "ISBN")], text=md.identifiers["isbn"])
        el("dc:Identifier", [("id", "uuid_id"), ("scheme", "uuid")], text=uid)
        for s in md.subjects:
            el("dc:Subject", text=s)
        if md.date:
            el("dc:Date", text=md.date)
        if md.rights:
            el("dc:Rights", text=md.rights)
        t.close(FLAG_CLOSE)  # dc-metadata
        el("x-metadata")
        if self.book.cover and self.book.cover in self.ids:
            el("meta", [("name", "cover"), ("content", self.ids[self.book.cover])], empty=True)
        t.close(FLAG_CLOSE)  # x-metadata
        t.close(FLAG_CLOSE)  # metadata
        el("manifest")
        for href in docs + others:
            el("item", [("id", self.ids[href]), ("href", "\x02" + self.ids[href]), ("media-type", "text/x-oeb1-document")],
               empty=True)
        for href in css:
            el("item", [("id", self.ids[href]), ("href", "\x02" + self.ids[href]), ("media-type", "text/x-oeb1-css")], empty=True)
        for href in images:
            el("item", [("id", self.ids[href]), ("href", "\x02" + self.ids[href]),
                        ("media-type", self.book.resources[href].media_type)], empty=True)
        t.close(FLAG_CLOSE)
        el("spine")
        for href in docs:
            el("itemref", [("idref", self.ids[href])], empty=True)
        t.close(FLAG_CLOSE)
        if toc:
            el("tours")
            el("tour", [("id", "chaptertour"), ("title", "Chapter Tour")])
            for title, target in toc:
                el("site", [("title", title), ("href", target)], empty=True)
            t.close(FLAG_CLOSE)
            t.close(FLAG_CLOSE)
        guide = []
        if self.book.cover and self.book.cover in self.ids:
            guide.append(("cover", "\x02" + self.ids[self.book.cover], "Cover image"))
        if guide:
            el("guide")
            for kind, target, title in guide:
                el("reference", [("type", kind), ("href", target), ("title", title)], empty=True)
            t.close(FLAG_CLOSE)
        t.close(FLAG_CLOSE)  # package
        return t.data()


def _svg_picture(book: Book, href: str, data: bytes) -> str | None:
    """The raster image an SVG shows, when it shows exactly one."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    found = [v for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "image"
             for k, v in el.attrib.items() if k.rsplit("}", 1)[-1] == "href"]
    if len(found) != 1 or is_external(found[0]):
        return None
    target = resolve(href, found[0])
    res = book.resources.get(target)
    return target if res is not None and res.media_type in ("image/jpeg", "image/png", "image/gif") else None


def write(book: Book, path, **_) -> None:
    from .. import clean
    from ..images import to_png
    b = book.copy()
    clean.clean_book(b)
    if not b.toc:
        b.ensure_toc()
    w = _Writer(b)
    docs = [h for h, _r in b.documents()]
    css = [h for h, r in b.resources.items() if r.media_type == CSS]
    images = []
    for h, r in list(b.resources.items()):
        if r.is_image:
            if r.media_type not in ("image/jpeg", "image/png", "image/gif"):
                png = to_png(r.data)
                if png is None:
                    # Reader has no SVG: point a cover-style wrapper at the picture inside it, drop the rest
                    picture = _svg_picture(b, h, r.data)
                    if picture:
                        w.alias[h] = picture
                    del w.ids[h]
                    continue
                r.data, r.media_type = png, "image/png"
            images.append(h)
    # the documents, tokenized, one after the other in section 1
    section1 = bytearray()
    files: dict[str, tuple[int, int, int] | None] = {}
    store = bytearray()  # section 0

    def put(name, data):
        files[name] = (0, len(store), len(data))
        store.extend(data)

    manifest_docs = []
    for href in docs:
        data, anchors = w.document(href, b.tree(href))
        iid = w.ids[href]
        offset = len(section1)
        section1 += data
        files[f"/data/{iid}/"] = None
        files[f"/data/{iid}/content"] = (1, offset, len(data))
        ahc = bytes([min(len(anchors), 255)]) + b"".join(
            bytes([len(a.encode("utf-8"))]) + a.encode("utf-8") + struct.pack("<I", off) for a, off in anchors[:255]
            if len(a.encode("utf-8")) < 256)
        put(f"/data/{iid}/ahc", ahc)
        put(f"/data/{iid}/aht", b"\0\0\0\0")
        manifest_docs.append((href, offset))
    for href in css:
        put(f"/data/{w.ids[href]}", b.resources[href].data)
    for href in images:
        put(f"/data/{w.ids[href]}", b.resources[href].data)
    toc = []
    for _d, e in b.toc_walk():
        target = w.link("", e.href) if e.href else None
        if target:
            toc.append((" ".join(e.title.split()) or "Untitled", target))
    put("/meta", w.package(docs, [], css, images, toc))

    def item(href, offset=0):
        iid = w.ids[href].encode("utf-8")
        p = iid  # unique, so files that share a name in different folders stay apart
        mt = b.resources[href].media_type.encode("ascii")
        return struct.pack("<I", offset) + bytes([len(iid)]) + iid + bytes([len(p)]) + p + bytes([len(mt)]) + mt + b"\0"

    manifest = b"\x01\\"
    for group in (manifest_docs, [], [(h, 0) for h in css], [(h, 0) for h in images]):
        manifest += struct.pack("<I", len(group)) + b"".join(item(h, off) for h, off in group)
    put("/manifest", manifest)
    put("/Version", b"\x08\x00\x01\x00")
    names = ["Uncompressed", "MSCompressed"]
    nl = b"".join(struct.pack("<H", len(n)) + n.encode("utf-16-le") + b"\0\0" for n in names)
    put("::DataSpace/NameList", struct.pack("<HH", (4 + len(nl)) // 2, len(names)) + nl)
    # section 1: stored LZX, reset at every frame
    raw = bytes(section1)
    content = bytearray()
    entries = []
    for i in range(0, max(len(raw), 1), FRAME):
        entries.append(len(content))
        content += compress_stored(raw[i:i + FRAME])
    base = "::DataSpace/Storage/MSCompressed"
    put(f"{base}/Content", bytes(content))
    put(f"{base}/ControlData", struct.pack("<IIIIIIII", 7, 0x43585A4C, 3, 1, 4, 2, 0, 0))
    put(f"{base}/SpanInfo", struct.pack("<Q", len(raw)))
    put(f"{base}/Transform/List", LZX_GUID)
    files[f"{base}/Transform/{LZX_GUID_TEXT}/InstanceData/"] = None
    table = struct.pack("<IIIIQQQ", 3, len(entries), 8, 0x28, len(raw), len(content), FRAME)
    table += b"".join(struct.pack("<Q", e) for e in entries)
    put(f"{base}/Transform/{LZX_GUID_TEXT}/InstanceData/ResetTable", table)
    files[f"::Transform/{LZX_GUID_TEXT}/"] = None
    files["/data/"] = None
    data = _container(files, bytes(store))
    with open(path, "wb") as f:
        f.write(data)


def _chunk(kind: bytes, size: int, number: int, header: bytes, entries: bytes, count: int) -> bytes:
    quick = struct.pack("<H", count)
    free = size - 8 - len(header) - len(entries)
    return (kind + struct.pack("<I", free) + header + entries).ljust(size - 2, b"\0") + quick


def _directory(names: list[str], files: dict) -> tuple[bytes, int, int, list[int]]:
    """The IFCM directory: AOLL chunks of entries chained in name order, plus an AOLI index when there are several."""
    size = 0x2000
    room = size - 48 - 2
    groups: list[list[tuple[str, bytes]]] = [[]]
    used = 0
    for n in names:
        nb = n.encode("utf-8")
        sec, off, length = files[n] if files[n] is not None else (0, 0, 0)
        entry = _encint(len(nb)) + nb + _encint(sec) + _encint(off) + _encint(length)
        if used + len(entry) > room:
            groups.append([])
            used = 0
        groups[-1].append((n, entry))
        used += len(entry)
    chunks = []
    first = 0
    last = len(groups) - 1
    for i, g in enumerate(groups):
        header = struct.pack("<QqqQQ", i, i - 1 if i else -1, i + 1 if i < last else -1, first, 1)
        chunks.append(_chunk(b"AOLL", size, i, header, b"".join(e for _n, e in g), len(g)))
        first += len(g)
    index = -1
    if len(groups) > 1:
        entries = b"".join(_encint(len(g[0][0].encode("utf-8"))) + g[0][0].encode("utf-8") + _encint(i) for i, g in enumerate(groups))
        if 16 + len(entries) + 2 > size:
            raise ValueError("too many files for a LIT directory")
        index = len(groups)
        chunks.append(_chunk(b"AOLI", size, index, struct.pack("<Q", index), entries, len(groups)))
    data = b"IFCM" + struct.pack("<IIIqII", 1, size, 0x100000, -1, len(chunks), 0) + b"".join(chunks)
    return data, index, last, [len(g) for g in groups]


def _container(files: dict, store: bytes) -> bytes:
    names = sorted(files, key=lambda n: n.encode("utf-8"))
    directory, index, last, counts = _directory(names, files)
    count_entries = b"".join(_encint(counts[i - 1] if i else 0) + _encint(c) + _encint(i) for i, c in enumerate(counts))
    if 48 + len(count_entries) + 2 > 0x200:
        raise ValueError("too many files for a LIT directory")
    count_chunk = _chunk(b"AOLL", 0x200, 0, struct.pack("<QqqQQ", 0, -1, -1, 0, 1), count_entries, len(counts))
    count_piece = b"IFCM" + struct.pack("<IIIqII", 1, 0x200, 0x20000, -1, 1, 0) + count_chunk
    guid1 = bytes.fromhex("c307900a7640d31187890000f8105754")
    guid2 = bytes.fromhex("c407900a7640d31187890000f8105754")
    header_len, n_pieces, sec_len = 40, 5, 232
    p0_off = header_len + 16 * n_pieces + sec_len
    sizes = [24, len(directory), len(count_piece), 16, 16]
    offsets = [p0_off]
    for s in sizes[:-1]:
        offsets.append(offsets[-1] + s)
    content_start = offsets[-1] + sizes[-1]
    total = content_start + len(store)
    header = b"ITOLITLS" + struct.pack("<IIII", 1, header_len, n_pieces, sec_len) + bytes.fromhex("c107900a7640d31187890000f8105754")
    pieces = b"".join(struct.pack("<IIII", o, 0, s, 0) for o, s in zip(offsets, sizes))
    words = [2, 0x98, index & 0xFFFFFFFF, 0xFFFFFFFF if index < 0 else 0, 0, 0, last, 0, 0, 0, 0x2000, 2, 0,
             2 if index >= 0 else 1, 0, 0, len(names), 0, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0, 0, 0, 0, 0, 0x200, 2, 0, 1, 0, 0,
             len(counts), 0, 0x100000, 0x20000, 0, 0, 0x4C4F4143, 2, 0x50, 0x31337, 0, 0x2000,
             0x200, 0x100000, 0x20000, 0, 0, 0, 0x46535449, 4, 0x20, 1, content_start, 0, int(time.time()) & 0xFFFFFFFF, 0x409]
    secondary = struct.pack(f"<{len(words)}I", *words)
    piece0 = struct.pack("<IIIIII", 0x1FE, 0, total, 0, 0, 0)
    out = header + pieces + secondary + piece0 + directory + count_piece + guid1 + guid2
    assert len(out) == content_start, (len(out), content_start)
    return out + store

