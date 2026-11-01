"""Sony BroadBand eBook (LRF, BBeB): read and write.

An LRF file is a header (signature, root object, object index, a zlib-packed XML info block and
an optional thumbnail) followed by objects. Each object is a run of tags: 16-bit codes 0xF5xx
with fixed or length-prefixed parameters, between 0xF500 (start: id, type) and 0xF501 (end).
Objects link to each other: the book (BookAtr) to its page tree, pages to blocks, blocks to
text and image objects, and each to a shared attribute object (PageAtr, BlockAtr, TextAtr).
Text streams are UTF-16 strings (0xF5CC) between paragraph tags (0xF5A1/0xF5A2), with italic
(0xF581/0xF582), line breaks (0xF5D2), font changes and link buttons. A TOC object lists
(page, object, title) entries.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from .. import limits, markup
from ..book import Book, Metadata, TocEntry, sniff_image
from ..errors import BookportError

SIGNATURE = b"L\x00R\x00F\x00\x00\x00"
STR = -1  # a length-prefixed string parameter
LIST = -2  # a count-prefixed list of 32-bit ids
TAG_SIZES = {
    0x00: 6, 0x01: 0, 0x02: 4, 0x03: 4, 0x04: 4, 0x05: 0, 0x06: 0, 0x07: 4, 0x08: 4, 0x09: 4, 0x0A: 4, 0x0B: LIST,
    0x0D: STR, 0x0E: 2, 0x11: 2, 0x12: 2, 0x13: 2, 0x14: 2, 0x15: 2, 0x16: STR, 0x17: 4, 0x18: 4, 0x19: 2, 0x1A: 2,
    0x1B: 2, 0x1C: 2, 0x1D: 2, 0x1E: 2, 0x21: 2, 0x22: 2, 0x23: 2, 0x24: 2, 0x25: 2, 0x26: 2, 0x27: 2, 0x28: 2,
    0x29: 6, 0x2A: 2, 0x2B: 2, 0x2C: 2, 0x2D: 4, 0x2E: 2, 0x31: 2, 0x32: 2, 0x33: 2, 0x34: 4, 0x35: 2, 0x36: 2,
    0x37: 4, 0x38: 2, 0x39: 2, 0x3A: 2, 0x3B: 2, 0x3C: 2, 0x3D: 2, 0x3E: 2, 0x41: 2, 0x42: 2, 0x44: 4, 0x45: 4,
    0x46: 2, 0x47: 2, 0x48: 2, 0x49: 8, 0x4A: 8, 0x4B: 4, 0x4C: 4, 0x4D: 0, 0x4E: 12, 0x51: 2, 0x52: 2, 0x53: 4,
    0x54: 2, 0x55: STR, 0x56: 4, 0x57: 2, 0x58: 2, 0x59: STR, 0x5A: STR, 0x5B: 4, 0x5C: LIST, 0x5D: STR, 0x5E: 2,
    0x60: 2, 0x61: 2, 0x62: 0, 0x63: 0, 0x64: 0, 0x65: 0, 0x66: 0, 0x67: 0, 0x68: 0, 0x69: 0, 0x6A: 0, 0x6B: 0,
    0x6C: 8, 0x6D: 4, 0x6E: 0, 0x71: 0, 0x72: 0, 0x73: 10, 0x75: 2, 0x76: 2, 0x77: 2, 0x78: 4, 0x79: 2, 0x7A: 2,
    0x7B: 4, 0x7C: 4, 0x81: 0, 0x82: 0, 0xA1: 4, 0xA2: 0, 0xA5: STR, 0xA6: 0, 0xA7: 4, 0xA8: 0, 0xA9: 0, 0xAA: 0,
    0xAB: 0, 0xAC: 0, 0xAD: 0, 0xAE: 0, 0xB1: 0, 0xB2: 0, 0xB3: 0, 0xB4: 0, 0xB5: 0, 0xB6: 0, 0xB7: 0, 0xB8: 0,
    0xB9: 0, 0xBA: 0, 0xBB: 0, 0xBC: 0, 0xBD: 0, 0xBE: 0, 0xC1: 0, 0xC2: 0, 0xC3: 2, 0xC4: 0, 0xC5: 2, 0xC6: 2,
    0xC7: 0, 0xC8: 2, 0xC9: 0, 0xCA: 2, 0xCB: 0, 0xCC: STR, 0xD1: 12, 0xD2: 0, 0xD4: 2, 0xD5: 0, 0xD6: 0, 0xD7: 2,
    0xD8: 0, 0xD9: 0, 0xDA: 2, 0xDB: 2, 0xDC: 2, 0xDD: 2, 0xF1: 2, 0xF2: 4, 0xF3: 4, 0xF4: 2, 0xF5: 4, 0xF6: 4,
    0xF7: 4, 0xF8: 4,
}
IMAGE_TYPES = {0x11: "image/jpeg", 0x12: "image/png", 0x13: "image/bmp", 0x14: "image/gif"}


def tags(data: bytes, pos: int = 0, end: int | None = None):
    """Yield (tag low byte, parameter bytes, position after) for each tag; streams come through
    0x04 (size) and 0x05 (the stream itself as the parameter)."""
    end = len(data) if end is None else end
    stream_size = 0
    while pos + 2 <= end:
        code = struct.unpack("<H", data[pos:pos + 2])[0]
        pos += 2
        if code >> 8 != 0xF5:
            continue  # not on a tag: step forward
        low = code & 0xFF
        if low == 0x05:
            param = data[pos:pos + stream_size]
            pos += stream_size
            yield low, param, pos
            continue
        size = TAG_SIZES.get(low)
        if size is None:
            size = 0
        if size == STR:
            n = struct.unpack("<H", data[pos:pos + 2])[0]
            param = data[pos + 2:pos + 2 + n]
            pos += 2 + n
        elif size == LIST:
            n = struct.unpack("<H", data[pos:pos + 2])[0]
            param = data[pos + 2:pos + 2 + 4 * n]
            pos += 2 + 4 * n
        else:
            param = data[pos:pos + size]
            pos += size
        if low == 0x04:
            stream_size = struct.unpack("<I", param)[0] if len(param) == 4 else 0
        yield low, param, pos


class LrfObject:
    def __init__(self, oid: int, otype: int, data: bytes):
        self.id, self.type = oid, otype
        self.tags: list[tuple[int, bytes]] = []
        self.links: list[int] = []
        self.stream = b""
        self.stream_flags = 0
        for low, param, _p in tags(data, 8):
            if low == 0x01:
                break
            if low == 0x03 and len(param) == 4:
                self.links.append(struct.unpack("<I", param)[0])
            elif low == 0x54 and len(param) == 2:
                self.stream_flags = struct.unpack("<H", param)[0]
            elif low == 0x05:
                self.stream = param
            self.tags.append((low, param))

    def get(self, low: int) -> bytes | None:
        return next((p for t, p in self.tags if t == low), None)

    def data(self) -> bytes:
        s = self.stream
        if self.stream_flags & 0x100 and len(s) > 4:
            try:
                s = limits.inflate(s[4:])
            except zlib.error:
                return b""
        return s


def _objects(data: bytes) -> tuple[dict[int, LrfObject], int, dict]:
    if not data.startswith(SIGNATURE[:6]):
        raise BookportError("Not an LRF file")
    root, count, index = struct.unpack("<IQQ", data[12:32])
    if index + 16 * count > len(data):
        raise BookportError("Damaged LRF file: the object index runs past the end")
    objs = {}
    for i in range(count):
        _oid, off, size, _r = struct.unpack("<IIII", data[index + 16 * i:index + 16 * i + 16])
        chunk = data[off:off + size]
        if len(chunk) < 8 or chunk[:2] != b"\x00\xf5":
            continue
        _t, oid2, otype = struct.unpack("<HIH", chunk[:8])
        objs[oid2] = LrfObject(oid2, otype, chunk)
    info = {}
    try:
        info_len = struct.unpack("<H", data[0x4C:0x4E])[0]
        xml = limits.inflate(data[0x58:0x54 + info_len]) if info_len > 4 else b""
        root_el = markup.parse_xml(xml.lstrip(b"\xef\xbb\xbf"), "This LRF file's info block") if xml else None
        if root_el is not None:
            for el in root_el.iter():
                if el.text and el.text.strip():
                    info.setdefault(el.tag, el.text.strip())
                if el.get("reading"):
                    info.setdefault(el.tag + "_reading", el.get("reading"))
    except (zlib.error, ET.ParseError, struct.error):
        pass
    return objs, root, info


def read(path) -> Book:
    data = Path(path).read_bytes()
    objs, root, info = _objects(data)
    book = Book(Metadata(title=info.get("Title", Path(path).stem)))
    book.source_format = "lrf"
    md = book.metadata
    if info.get("Author"):
        md.authors = [a.strip() for a in re.split(r"\s*(?:&|;| and )\s*", info["Author"]) if a.strip()]
    md.publisher = info.get("Publisher", "") if info.get("Publisher") != "Unknown" else ""
    md.language = info.get("Language", "")
    if info.get("Category"):
        md.subjects = [c.strip() for c in info["Category"].split(",") if c.strip()]
    if re.match(r"\d{4}(-\d\d){0,2}$", info.get("CreationDate", "")):
        md.date = info["CreationDate"]
    if info.get("FreeText"):
        md.description = info["FreeText"]
    if info.get("BookID"):
        md.identifiers["lrf"] = info["BookID"]
    book_atr = objs.get(root)
    tree_id = struct.unpack("<I", book_atr.get(0x7B))[0] if book_atr and book_atr.get(0x7B) else None
    tree = objs.get(tree_id)
    page_ids = []
    if tree is not None and tree.get(0x5C):
        p = tree.get(0x5C)
        page_ids = list(struct.unpack(f"<{len(p) // 4}I", p))
    if not page_ids:
        page_ids = [o.id for o in objs.values() if o.type == 0x02]
    images: dict[int, str] = {}

    def image(img_id):
        if img_id in images:
            return images[img_id]
        img = objs.get(img_id)
        if img is None:
            return None
        ref = img.get(0x4C)
        stream = objs.get(struct.unpack("<I", ref)[0]) if ref else img
        if stream is None:
            return None
        raw = stream.stream
        mt = sniff_image(raw) or IMAGE_TYPES.get(stream.stream_flags & 0xFF)
        if mt is None or not sniff_image(raw):
            return None
        from ..book import IMAGE_EXT
        images[img_id] = book.add(f"images/image{len(images) + 1:04d}{IMAGE_EXT.get(mt, '.bin')}", raw, mt).href
        return images[img_id]

    anchor_of: dict[tuple[int, int], str] = {}
    for n, pid in enumerate(page_ids, 1):
        page = objs.get(pid)
        if page is None:
            continue
        href = f"text/part{n:04d}.xhtml"
        root_el, body = markup.new_document(md.title, md.language)
        ET.SubElement(body, "a", {"id": f"page{pid}"})
        anchor_of[(pid, 0)] = f"{href}#page{pid}"
        for low, param, _p in tags(page.data()):
            if low != 0x03 or len(param) != 4:
                continue
            block = objs.get(struct.unpack("<I", param)[0])
            if block is None:
                continue
            ET.SubElement(body, "a", {"id": f"obj{block.id}"})
            anchor_of[(pid, block.id)] = f"{href}#obj{block.id}"
            targets = [block] + [objs[i] for i in _stream_links(block) if i in objs]
            for t in targets:
                if t.type == 0x0A:
                    anchor_of[(pid, t.id)] = f"{href}#obj{block.id}"
                    _text(t.data(), body, objs, image)
                elif t.type == 0x0C:
                    src = image(t.id)
                    if src:
                        div = ET.SubElement(body, "div")
                        ET.SubElement(div, "img", {"src": "../" + src, "alt": ""})
                elif t.type == 0x0D:
                    for i in _stream_links(t):
                        o = objs.get(i)
                        if o is not None and o.type == 0x0C:
                            src = image(o.id)
                            if src:
                                div = ET.SubElement(body, "div")
                                ET.SubElement(div, "img", {"src": "../" + src, "alt": ""})
                        elif o is not None and o.type == 0x06:
                            for k in _stream_links(o):
                                inner = objs.get(k)
                                if inner is not None and inner.type == 0x0A:
                                    _text(inner.data(), body, objs, image)
        book.add_document(href, root_el)
    toc_obj = next((o for o in objs.values() if o.type == 0x1E), None)
    if toc_obj is not None:
        raw = toc_obj.data() if toc_obj.stream_flags & 0x100 else toc_obj.stream
        try:
            count = struct.unpack("<I", raw[:4])[0]
            offs = struct.unpack(f"<{count}I", raw[4:4 + 4 * count])
            base = 4 + 4 * count
            for o in offs:
                pid, oid, n = struct.unpack("<IIH", raw[base + o:base + o + 10])
                title = raw[base + o + 10:base + o + 10 + n].decode("utf-16-le", "replace")
                href = anchor_of.get((pid, oid)) or anchor_of.get((pid, 0))
                if href:
                    book.toc.append(TocEntry(title.strip() or "Untitled", href))
        except struct.error:
            pass
    if not book.spine:
        raise BookportError("This LRF file has no pages")
    if not book.toc:
        book.ensure_toc()
    imgs = book.images()
    if imgs:
        book.cover = imgs[0].href if len(book.spine) and book.spine[0] in book.resources and \
            imgs[0].href in markup.serialize(book.tree(book.spine[0])).decode("utf-8", "replace") else None
    return book


def _stream_links(obj: LrfObject) -> list[int]:
    out = []
    for low, param, _p in tags(obj.data()):
        if low == 0x03 and len(param) == 4:
            out.append(struct.unpack("<I", param)[0])
        elif low == 0x49 and len(param) == 8:  # a canvas puts an object at (x, y)
            out.append(struct.unpack("<I", param[4:8])[0])
    return out


def _text(data: bytes, body, objs, image) -> None:
    para = None
    styles = {"i": False, "b": False, "sup": False, "sub": False}
    link = None

    def target():
        nonlocal para
        if para is None:
            para = ET.SubElement(body, "p")
        t = link if link is not None else para
        for k, tag in (("b", "b"), ("i", "i"), ("sup", "sup"), ("sub", "sub")):
            if styles[k]:
                t = ET.SubElement(t, tag)
        return t

    pending_br = False
    for low, param, _p in tags(data):
        if pending_br and low != 0xA2:
            ET.SubElement(target(), "br")
        pending_br = False
        if low == 0xA1:
            para = ET.SubElement(body, "p")
        elif low == 0xA2:
            if para is not None and not (para.text or len(para)):
                para.text = " "
            para = None
            link = None
        elif low == 0xCC:
            markup.append_text(target(), param.decode("utf-16-le", "replace"))
        elif low == 0xD2:
            pending_br = True
            continue
        elif low == 0x81:
            styles["i"] = True
        elif low == 0x82:
            styles["i"] = False
        elif low == 0x15 and len(param) == 2:
            styles["b"] = struct.unpack("<H", param)[0] >= 600
        elif low in (0xB7, 0xB9, 0xB1, 0xB3):  # B7/B9 as Sony and Calibre write them, B1/B3 from bookport 0.1 drafts
            styles["sup" if low in (0xB7, 0xB1) else "sub"] = True
        elif low in (0xB8, 0xBA, 0xB2, 0xB4):
            styles["sup" if low in (0xB8, 0xB2) else "sub"] = False
        elif low == 0xA7:
            link = ET.SubElement(target(), "span")
        elif low == 0xA8:
            link = None
        elif low == 0xD1 and len(param) == 12:
            _w, _h, img_id = struct.unpack("<HHI", param[:8])
            src = image(img_id)
            if src:
                ET.SubElement(target(), "img", {"src": "../" + src, "alt": ""})


# --- writing -----------------------------------------------------------------------------------

def _tag(low: int, param: bytes = b"") -> bytes:
    return struct.pack("<H", 0xF500 | low) + param


def _u16(low, v):
    return _tag(low, struct.pack("<H", v & 0xFFFF))


def _u32(low, v):
    return _tag(low, struct.pack("<I", v))


def _string(low, s: str) -> bytes:
    raw = s.encode("utf-16-le")
    if len(raw) <= 0xFFFE:
        return _tag(low, struct.pack("<H", len(raw)) + raw)
    # the length is 16 bits: long text goes out as several strings, never splitting a surrogate pair
    out = b""
    while raw:
        cut = min(len(raw), 0xFFFE)
        if cut < len(raw) and 0xD8 <= raw[cut - 1] <= 0xDB:
            cut -= 2
        out += _tag(low, struct.pack("<H", cut) + raw[:cut])
        raw = raw[cut:]
    return out


class _Writer:
    def __init__(self):
        self.objects: dict[int, bytes] = {}
        self.next_id = 1

    def reserve(self) -> int:
        oid = self.next_id
        self.next_id += 1
        return oid

    def put(self, oid: int, otype: int, body: bytes):
        self.objects[oid] = _tag(0x00, struct.pack("<IH", oid, otype)) + body + _tag(0x01)

    def stream(self, payload: bytes, flags: int, compress: bool) -> bytes:
        if compress:
            payload = struct.pack("<I", len(payload)) + zlib.compress(payload, 9)
            flags |= 0x100
        return _u16(0x54, flags) + _u32(0x04, len(payload)) + _tag(0x05) + payload + _tag(0x06)


FONT = "Dutch801 Rm BT Roman"


def write(book: Book, path, **_) -> None:
    from ..flow import Block, book_blocks
    from ..images import to_png
    w = _Writer()
    md = book.metadata
    root_id = w.reserve()
    tree_id = w.reserve()
    page_atr = w.reserve()
    text_atr = w.reserve()
    block_atr = w.reserve()
    # shared attributes: a 600 x 800 screen with margins, 10 pt text
    w.put(page_atr, 0x05, b"".join([_u16(0x21, 20), _u16(0x22, 0), _u16(0x23, 0), _u16(0x24, 20), _u16(0x25, 740),
                                     _u16(0x26, 560), _u16(0x27, 0), _u16(0x28, 0), _u16(0x35, 0x34), _u16(0x2B, 0),
                                     _u16(0x2A, 1)]))
    w.put(text_atr, 0x0B, b"".join([_u16(0x11, 100), _u16(0x12, 0xFFF6), _u16(0x13, 0), _u16(0x14, 0), _u16(0x15, 400),
                                     _string(0x16, FONT), _u32(0x17, 0), _u32(0x18, 0xFF), _u16(0x19, 25), _u16(0x1A, 0),
                                     _u16(0x1B, 120), _u16(0x1C, 12), _u16(0x1D, 0), _u16(0x1E, 0), _u16(0x3C, 1),
                                     _u16(0x3D, 1)]))
    w.put(block_atr, 0x07, b"".join([_u16(0x31, 560), _u16(0x32, 740), _u16(0x33, 0x12), _u16(0x35, 0x34),
                                      _u16(0x36, 0), _u32(0x37, 0), _u16(0x38, 0), _u16(0x39, 0), _u16(0x3A, 0),
                                      _u32(0x34, 0xFF)]))
    images: dict[str, int] = {}

    def image(href):
        if href in images:
            return images[href]
        res = book.resources.get(href)
        if res is None or not res.is_image:
            return None
        data = res.data
        kind = {"image/jpeg": 0x11, "image/png": 0x12, "image/gif": 0x14}.get(res.media_type)
        if kind is None:
            data = to_png(data)
            kind = 0x12
            if data is None:
                return None
        from .epub import image_size
        wpx, hpx = image_size(data) or (300, 400)
        sid = w.reserve()
        w.put(sid, 0x11, w.stream(data, kind, compress=False))
        iid = w.reserve()
        w.put(iid, 0x0C, _tag(0x4A, struct.pack("<HHHH", 0, 0, wpx, hpx)) + _tag(0x4B, struct.pack("<HH", wpx, hpx))
              + _u32(0x4C, sid))
        images[href] = (iid, wpx, hpx)
        return images[href]

    # one page per top-level TOC entry (else per document)
    starts = {}
    for depth, e in book.toc_walk():
        starts.setdefault(e.href, (depth, " ".join(e.title.split())))
    pages: list[list] = [[]]
    toc_marks: list[tuple[int, str, int]] = []
    blocks_all = book_blocks(book)
    if book.cover is not None and next((x.image for x in blocks_all[:3] if x.kind == "image"), None) != book.cover:
        # the book doesn't open on its cover (Standard Ebooks keep it out of the spine): give it a page
        blocks_all.insert(0, Block(kind="image", image=book.cover))
        if len(blocks_all) > 1:
            blocks_all[1].page_break = True
    for b in blocks_all:
        hit = next((starts.pop(a) for a in b.anchors if a in starts), None)
        if (b.page_break or (hit is not None and hit[0] == 0)) and pages[-1]:
            pages.append([])
        if hit is not None:
            toc_marks.append((len(pages) - 1, hit[1], len(pages[-1])))
        pages[-1].append(b)
    page_ids = []
    text_ids = []
    for blocks in pages:
        stream = bytearray()
        for b in blocks:
            stream += _paragraph(b, image)
        if not stream:
            stream += _tag(0xA1, b"\0\0\0\0") + _tag(0xA2)
        tid = w.reserve()
        w.put(tid, 0x0A, _u32(0x03, text_atr) + w.stream(bytes(stream), 0, compress=True))
        bid = w.reserve()
        w.put(bid, 0x06, _u32(0x03, block_atr) + w.stream(_u32(0x03, tid), 0, compress=False))
        pid = w.reserve()
        page_ids.append(pid)
        text_ids.append(bid)
        w.put(pid, 0x02, _u32(0x03, page_atr) + _u32(0x7C, tree_id) + w.stream(_u32(0x03, bid), 0, compress=False))
    w.put(tree_id, 0x01, _tag(0x5C, struct.pack("<H", len(page_ids)) + b"".join(struct.pack("<I", p) for p in page_ids)))
    # the TOC object
    entries = b""
    offsets = []
    for page_index, title, _k in toc_marks:
        offsets.append(len(entries))
        raw = title.encode("utf-16-le")
        entries += struct.pack("<IIH", page_ids[page_index], text_ids[page_index], len(raw)) + raw
    toc_id = w.reserve()
    toc_payload = struct.pack("<I", len(offsets)) + b"".join(struct.pack("<I", o) for o in offsets) + entries
    w.put(toc_id, 0x1E, w.stream(toc_payload, 0x51, compress=False))
    w.put(root_id, 0x1C, b"".join([_u32(0x7B, tree_id), _u16(0x76, 0), _u16(0x77, 1), _u16(0x79, 2), _u16(0x7A, 0x10),
                                    _u16(0xDA, 2), _u16(0x75, 1), _u32(0x78, 0), _string(0x16, FONT)]))
    # header, info and thumbnail
    info = ('﻿<?xml version="1.0" encoding="utf-8"?>\n<Info version="1.1">\n<BookInfo>\n'
            f"<Title reading=\"\">{markup.escape(md.title)}</Title>\n"
            f"<Author reading=\"{markup._esc_attr(md.author_sort or '')}\">{markup.escape(' & '.join(md.authors))}</Author>\n"
            f"<BookID>{markup.escape(md.uid().replace('-', '')[:32])}</BookID>\n"
            f"<Publisher reading=\"\">{markup.escape(md.publisher)}</Publisher>\n<Label reading=\"\" />\n"
            f"<Category>{markup.escape(', '.join(md.subjects))}</Category>\n<Classification />\n"
            f"<FreeText>{markup.escape(re.sub(r'<[^>]+>', '', md.description))}</FreeText>\n</BookInfo>\n<DocInfo>\n"
            f"<Language>{markup.escape(md.language or 'en')}</Language>\n<Creator />\n"
            f"<CreationDate>{markup.escape(md.date[:10])}</CreationDate>\n<Producer>bookport</Producer>\n"
            "<Page>0</Page>\n</DocInfo>\n</Info>\n").encode()
    info_z = struct.pack("<I", len(info)) + zlib.compress(info, 9)
    thumb = b""
    thumb_type = 0
    cover = book.cover_image()
    if cover is not None and cover.media_type in ("image/jpeg", "image/png", "image/gif"):
        thumb = cover.data
        thumb_type = {"image/jpeg": 0x11, "image/png": 0x12, "image/gif": 0x14}[cover.media_type]
    header_len = 0x58
    pos = 0x54 + len(info_z) + len(thumb)
    pos += (16 - pos % 16) % 16
    body = bytearray()
    index = []
    for oid in sorted(w.objects):
        index.append((oid, pos + len(body), len(w.objects[oid])))
        body += w.objects[oid]
    index_off = pos + len(body)
    header = bytearray(SIGNATURE)
    header += struct.pack("<HH", 1000, 0x0A00)
    header += struct.pack("<IQQ", root_id, len(index), index_off)
    header += struct.pack("<IHHHHHB", 0, 1, 1660, 0, 600, 800, 24)
    header = header.ljust(0x44, b"\0")
    toc_off = next(off for oid, off, _s in index if oid == toc_id)
    header += struct.pack("<II", toc_id, toc_off)
    header += struct.pack("<HHI", len(info_z), thumb_type, len(thumb))
    del header_len
    out = bytes(header) + info_z + thumb
    out += b"\0" * (pos - len(out))
    out += bytes(body)
    out += b"".join(struct.pack("<IIII", oid, off, size, 0) for oid, off, size in index)
    Path(path).write_bytes(out)


def _paragraph(b, image) -> bytes:
    out = bytearray()
    if b.kind == "image":
        got = image(b.image)
        if got:
            iid, wpx, hpx = got
            scale = min(1.0, 560 / wpx if wpx else 1, 700 / hpx if hpx else 1)
            out += _tag(0xA1, b"\0\0\0\0") + _tag(0xD1, struct.pack("<HHII", int(wpx * scale), int(hpx * scale), iid, 4))
            out += _tag(0xD2) + _tag(0xA2)
        return bytes(out)
    if b.kind == "hr":
        return _tag(0xA1, b"\0\0\0\0") + _string(0xCC, "* * *") + _tag(0xA2)
    if b.kind == "table":
        runs = []
        for row in b.rows:
            runs.append(" | ".join("".join(r.text for r in c.runs) for c in row))
        out += b"".join(_tag(0xA1, b"\0\0\0\0") + _string(0xCC, line) + _tag(0xA2) for line in runs)
        for src in b.images():
            got = image(src)
            if got:
                iid, wpx, hpx = got
                out += _tag(0xA1, b"\0\0\0\0") + _tag(0xD1, struct.pack("<HHII", min(wpx, 560), min(hpx, 700), iid, 4))
                out += _tag(0xD2) + _tag(0xA2)
        return bytes(out)
    out += _tag(0xA1, b"\0\0\0\0")
    if b.kind == "h":
        out += _u16(0x11, max(110, min(220, int(100 * (b.size or 1.4)))))
        out += _u16(0x15, 700)
    if b.marker:
        out += _string(0xCC, b.marker + " ")
    for r in b.runs:
        if r.image:
            got = image(r.image)
            if got:
                iid, wpx, hpx = got
                out += _tag(0xD1, struct.pack("<HHII", min(wpx, 560), min(hpx, 700), iid, 4))
            continue
        if not r.text:
            continue
        if r.italic:
            out += _tag(0x81)
        if r.bold and b.kind != "h":
            out += _u16(0x15, 700)
        if r.sup:
            out += _tag(0xB7)
        elif r.sub:
            out += _tag(0xB9)
        parts = r.text.split("\n")
        for k, part in enumerate(parts):
            if k:
                out += _tag(0xD2)
            if part:
                out += _string(0xCC, part)
        if r.sup:
            out += _tag(0xB8)
        elif r.sub:
            out += _tag(0xBA)
        if r.bold and b.kind != "h":
            out += _u16(0x15, 400)
        if r.italic:
            out += _tag(0x82)
    if b.kind == "h":
        out += _u16(0x15, 400) + _u16(0x11, 100)
    out += _tag(0xD2) + _tag(0xA2)  # readers expect a line break to close each paragraph
    return bytes(out)
