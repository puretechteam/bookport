"""Write Kindle books: KF8 (AZW3), and joint MOBI 6 + KF8 files (.mobi, .azw).

The KF8 part keeps the book's XHTML and CSS: each document becomes a skeleton (the page
with its container element emptied) plus fragments of up to 8 KB, with links rewritten to
kindle:pos, images and fonts to kindle:embed and stylesheets to kindle:flow, and NCX, skeleton,
fragment and guide indexes. The MOBI 6 part, for older Kindles, is simple HTML made from the
book's styled blocks (bold, italic, sizes, alignment, indents, lists, tables, images), with
filepos links and an NCX index. Text is PalmDOC compressed in 4096-byte records.
"""

from __future__ import annotations

import bisect
import random
import re
import struct
import xml.etree.ElementTree as ET
import zlib

from .. import markup
from ..book import CSS, Book, is_external, resolve
from ..errors import BookportError
from ..mobi_index import CNCX, build_index
from ..palm import palmdoc_compress, pdb_bytes

RECORD_SIZE = 4096
CHUNK = 8192
B32 = "0123456789ABCDEFGHIJKLMNOPQRSTUV"
LANG_CODES = {"en": 9, "de": 7, "fr": 12, "es": 10, "it": 16, "nl": 19, "pt": 22, "ru": 25, "ja": 17, "zh": 4, "ko": 18,
              "sv": 29, "no": 20, "nb": 20, "da": 6, "fi": 11, "pl": 21, "cs": 5, "hu": 14, "tr": 31, "el": 8, "he": 13,
              "ar": 1, "ca": 3, "la": 0}

FLIS = bytes.fromhex("464c4953000000080041000000000000ffffffff000100030000000300000001ffffffff")


def b32(n: int, width: int) -> str:
    out = ""
    while n:
        n, r = divmod(n, 32)
        out = B32[r] + out
    return out.rjust(width, "0")


def fcis(text_length: int) -> bytes:
    return (b"FCIS" + struct.pack(">IIII", 0x14, 0x10, 2, 0) + struct.pack(">I", text_length)
            + struct.pack(">IIIII", 0, 0x28, 0, 0x28, 8) + struct.pack(">HHI", 1, 1, 0))


# --- text records -------------------------------------------------------------------------------

def text_records(text: bytes, compress: bool = True) -> list[bytes]:
    """4096-byte text records, each with a multibyte-overlap trailing entry (extra flags = 1)."""
    out = []
    for start in range(0, len(text), RECORD_SIZE):
        chunk = text[start:start + RECORD_SIZE]
        end = start + len(chunk)
        # bytes of the next record that finish a UTF-8 character split at the boundary
        overlap = b""
        if end < len(text):
            k = len(chunk) - 1
            back = 0
            while k >= 0 and back < 3 and (chunk[k] & 0xC0) == 0x80:
                k -= 1
                back += 1
            if k >= 0 and chunk[k] >= 0xC0:
                need = 2 if chunk[k] < 0xE0 else 3 if chunk[k] < 0xF0 else 4
                missing = need - (back + 1)
                if missing > 0:
                    overlap = text[end:end + missing]
        body = palmdoc_compress(chunk) if compress else chunk
        out.append(body + overlap + bytes([len(overlap)]))
    return out


# --- EXTH and record 0 ------------------------------------------------------------------------------

def _exth(items: list[tuple[int, bytes | int | str]]) -> bytes:
    body = bytearray()
    n = 0
    for kind, value in items:
        if value is None or value == "" or value == b"":
            continue
        if isinstance(value, int):
            value = struct.pack(">I", value)
        elif isinstance(value, str):
            value = value.encode("utf-8")
        body += struct.pack(">II", kind, len(value) + 8) + value
        n += 1
    pad = (4 - len(body) % 4) % 4
    return b"EXTH" + struct.pack(">II", len(body) + 12 + pad, n) + bytes(body) + b"\0" * pad


def _metadata_exth(book: Book, cover_index: int | None, extra: list) -> list:
    md = book.metadata
    items: list = []
    for a in md.authors:
        items.append((100, a))
    items.append((101, md.publisher))
    if md.description:
        items.append((103, md.description))
    if md.identifiers.get("isbn"):
        items.append((104, md.identifiers["isbn"]))
    for s in md.subjects:
        items.append((105, s))
    items.append((106, md.date))
    for c in md.contributors:
        items.append((108, c))
    items.append((109, md.rights))
    uid = md.uid()
    items.append((112, f"bookport:{uid}"))
    items.append((113, md.identifiers.get("asin") or uid))
    items.append((501, "EBOK"))
    items.append((503, md.title))
    items.append((524, md.language or "en"))
    if book.direction == "rtl":
        items.append((527, "rtl"))
    if book.fixed_layout:
        items.append((122, "true"))
    if cover_index is not None:
        items.append((201, cover_index))
        items.append((202, cover_index))
        items.append((203, 0))
    items.extend(extra)
    return items


def record0(book: Book, *, version: int, text_length: int, n_text: int, first_non_book: int, first_image: int,
            exth_items: list, ncx: int, fcis_i: int, flis_i: int, last_content: int = 0, fdst: int = 0, fdst_count: int = 0,
            frag: int = 0xFFFFFFFF, skel: int = 0xFFFFFFFF, guide: int = 0xFFFFFFFF, uid: int = 0,
            compression: int = 2) -> bytes:
    title = (book.metadata.title or "Untitled").encode("utf-8")
    hlen = 0x108 if version == 8 else 0xE8
    lang = (book.metadata.language or "en").split("-")[0].lower()
    locale = LANG_CODES.get(lang, 9)
    exth = _exth(exth_items)
    name_off = 16 + hlen + len(exth)
    h = bytearray(struct.pack(">HHIHHHH", compression, 0, text_length, n_text, RECORD_SIZE, 0, 0))
    h += b"MOBI" + struct.pack(">IIII", hlen, 2, 65001, uid)
    h += struct.pack(">I", version)
    h += struct.pack(">I", 0xFFFFFFFF) * 10  # orthographic .. extra index 5
    h += struct.pack(">IIIIIIII", first_non_book, name_off, len(title), locale, 0, 0, version, first_image)
    h += struct.pack(">IIII", 0, 0, 0, 0)  # huffman
    h += struct.pack(">I", 0x50)  # EXTH present
    h += b"\0" * 32
    h += struct.pack(">I", 0xFFFFFFFF)
    h += struct.pack(">IIII", 0xFFFFFFFF, 0xFFFFFFFF, 0, 0)  # no DRM
    h += b"\0" * 8
    if version == 8:
        h += struct.pack(">II", fdst, fdst_count)
    else:
        h += struct.pack(">HHI", 1, last_content, 1)
    h += struct.pack(">IIII", fcis_i, 1, flis_i, 1)
    h += b"\0" * 8
    h += struct.pack(">IIII", 0xFFFFFFFF, 0, 0xFFFFFFFF, 0xFFFFFFFF)
    h += struct.pack(">I", 1)  # extra data flags: multibyte overlap
    h += struct.pack(">I", ncx)
    if version == 8:
        h += struct.pack(">IIII", frag, skel, 0xFFFFFFFF, guide)
        h += struct.pack(">IIII", 0xFFFFFFFF, 0, 0xFFFFFFFF, 0)
    assert len(h) == 16 + hlen, len(h)
    out = bytes(h) + exth + title + b"\0\0"
    out += b"\0" * ((4 - len(out) % 4) % 4)
    return out


# --- resources ----------------------------------------------------------------------------------------

class Resources:
    """Images and fonts in record order; kindle:embed indexes are 1-based."""

    def __init__(self, book: Book, fonts: bool):
        self.book = book
        self.order: list[str] = []
        self.records: list[bytes] = []
        for href, res in book.resources.items():
            if res.is_image and res.media_type in ("image/jpeg", "image/png", "image/gif"):
                self._add(href, res.data)
            elif res.is_image and res.media_type in ("image/webp", "image/bmp", "image/tiff"):
                from ..images import to_png
                png = to_png(res.data)
                if png is not None:
                    self._add(href, png)
            elif fonts and res.media_type in ("font/ttf", "font/otf", "application/x-font-ttf", "application/vnd.ms-opentype",
                                              "application/font-sfnt", "font/sfnt"):
                self._add(href, _font_record(res.data))

    def _add(self, href, data):
        self.order.append(href)
        self.records.append(data)

    def index(self, href: str) -> int | None:
        try:
            return self.order.index(href) + 1
        except ValueError:
            return None


def _font_record(data: bytes) -> bytes:
    comp = zlib.compress(data, 9)
    return b"FONT" + struct.pack(">IIIII", len(data), 1, 24, 0, 0) + comp


# --- KF8 -----------------------------------------------------------------------------------------------

_PLACE = re.compile(rb"kindle:pos:fid:([0-9A-V]{4}):off:([0-9A-V]{10})")


class KF8Builder:
    def __init__(self, book: Book, res: Resources):
        self.book = book
        self.res = res
        self.flows: list[bytes] = [b""]  # flow 0 is the text
        self.css_flow: dict[str, int] = {}
        self.targets: list[tuple[str, str]] = []  # placeholder index -> (doc href, id or "")
        self.target_index: dict[tuple[str, str], int] = {}

    def _target(self, doc: str, frag: str) -> str:
        key = (doc, frag)
        if key not in self.target_index:
            self.target_index[key] = len(self.targets)
            self.targets.append(key)
        n = self.target_index[key]
        # a placeholder of the final length: fid holds the target number, off is zero
        return f"kindle:pos:fid:{b32(n // (32**10), 4)}:off:{b32(n % (32**10), 10)}"

    def _css(self, href: str) -> int:
        if href not in self.css_flow:
            text = markup.decode(self.book.resources[href].data)
            text = self._css_urls(href, text)
            self.flows.append(text.encode("utf-8"))
            self.css_flow[href] = len(self.flows) - 1
        return self.css_flow[href]

    def _css_urls(self, base: str, text: str) -> str:
        from ..links import rewrite_css

        def fix(u):
            if is_external(u) or u.startswith("data:"):
                return u
            target = resolve(base, u)
            idx = self.res.index(target)
            if idx is None:
                return None
            mt = self.book.resources[target].media_type
            return f"kindle:embed:{b32(idx, 4)}?mime={mt}"

        text = re.sub(r"@import[^;]+;", "", text)
        return rewrite_css(text, fix)

    def prepare(self, href: str, root: ET.Element) -> ET.Element:
        for el in root.iter():
            for attr in ("href", "src", "xlink:href"):
                v = el.get(attr)
                if v is None or v.startswith("data:"):
                    continue
                if is_external(v):
                    continue
                if el.tag == "link":
                    target = resolve(href, v)
                    if target in self.book.resources and self.book.resources[target].media_type == CSS:
                        el.set(attr, f"kindle:flow:{b32(self._css(target), 4)}?mime=text/css")
                    continue
                path, _, frag = v.partition("#")
                target = resolve(href, path) if path else href
                res = self.book.resources.get(target)
                if res is None:
                    del el.attrib[attr]
                    continue
                if res.is_document:
                    el.set(attr, self._target(target, frag))
                elif res.media_type == "image/svg+xml":
                    # KF8 keeps SVG images as flows of their own
                    if target not in self.css_flow:
                        self.flows.append(res.data)
                        self.css_flow[target] = len(self.flows) - 1
                    el.set(attr, f"kindle:flow:{b32(self.css_flow[target], 4)}?mime=image/svg+xml")
                else:
                    idx = self.res.index(target)
                    if idx is None:
                        del el.attrib[attr]
                    else:
                        el.set(attr, f"kindle:embed:{b32(idx, 4)}?mime={res.media_type}")
            st = el.get("style")
            if st and "url(" in st:
                el.set("style", self._css_urls(href, st))
            if el.tag == "style" and el.text:
                el.text = self._css_urls(href, el.text)
        return root

    def build(self):
        """Lay out skeletons and fragments; returns (text, skel entries, frag entries, frag cncx, positions)."""
        book = self.book
        text = bytearray()
        skel_entries = []
        frag_entries = []
        frag_cncx = CNCX()
        frags = []  # (insert position, file number, raw start, data)
        id_pos: dict[tuple[str, str], tuple[int, int]] = {}  # (doc, id) -> (frag index, offset)
        aid_counter = [0]

        def aid():
            aid_counter[0] += 1
            return b32(aid_counter[0], 1)

        for fnum, (href, _res) in enumerate(book.documents()):
            root = self.prepare(href, _deep_copy(book.tree(href)))
            body = root.find("body")
            container = body
            while True:
                kids = [c for c in container if isinstance(c.tag, str)]
                if (len(kids) == 1 and kids[0].tag in ("div", "section", "article", "main") and not (container.text or "").strip()
                        and not (kids[0].tail or "").strip() and len(markup.to_string(kids[0])) > CHUNK):
                    container = kids[0]
                else:
                    break
            body_aid = aid()
            body.set("aid", body_aid)
            cont_aid = body_aid if container is body else aid()
            container.set("aid", cont_aid)
            # serialize the children as pieces, then empty the container for the skeleton
            pieces: list[bytes] = []
            if container.text:
                pieces.append(markup.escape(markup.clean_text(container.text)).encode("utf-8"))
            for child in container:
                out: list[str] = []
                markup._write(child, out, top=False, ns=True)
                pieces.append("".join(out).encode("utf-8"))
            for child in list(container):
                container.remove(child)
            container.text = None
            skeleton = markup.serialize(root)
            marker = f'aid="{cont_aid}"'.encode()
            at = skeleton.find(marker)
            insert_at = skeleton.find(b">", at) + 1
            if skeleton[insert_at - 2:insert_at] == b"/>":
                # an empty element serialized as <x/>: open it so fragments can go inside
                tag_start = skeleton.rfind(b"<", 0, at)
                name = re.match(rb"<([\w:-]+)", skeleton[tag_start:]).group(1)
                skeleton = skeleton[:insert_at - 2] + b"></" + name + b">" + skeleton[insert_at:]
                insert_at -= 1
            # group pieces into fragments of about CHUNK bytes
            groups: list[bytes] = []
            cur = b""
            for p in pieces:
                if cur and len(cur) + len(p) > CHUNK:
                    groups.append(cur)
                    cur = b""
                cur += p
            if cur or not groups:
                groups.append(cur or b" ")
            skel_start = len(text)
            text += skeleton
            insert = skel_start + insert_at
            cumul = 0
            selector = frag_cncx.add(f"P-//*[@aid='{cont_aid}']")
            first_frag = len(frags)
            for g in groups:
                fi = len(frags)
                frags.append((insert, fnum, g))
                frag_entries.append((f"{insert:010d}", {2: [selector], 3: [fnum], 4: [fi], 6: [cumul, len(g)]}))
                # ids inside this fragment
                for m in re.finditer(rb'\sid="([^"]+)"', g):
                    tag_start = g.rfind(b"<", 0, m.start())
                    key = (href, m.group(1).decode("utf-8"))
                    id_pos.setdefault(key, (fi, tag_start))
                insert += len(g)
                cumul += len(g)
            for g in groups:
                text += g
            for m in re.finditer(rb'\sid="([^"]+)"', skeleton):
                id_pos.setdefault((href, m.group(1).decode("utf-8")), (first_frag, 0))
            id_pos[(href, "")] = (first_frag, 0)
            skel_entries.append((f"SKEL{fnum:010d}", {1: [len(groups), len(groups)],
                                                       6: [skel_start, len(skeleton), skel_start, len(skeleton)]}))
        # patch placeholders with the real positions (same length, so nothing moves)
        def patch(m):
            n = int(m.group(1).decode(), 32) * 32**10 + int(m.group(2).decode(), 32)
            doc, frag = self.targets[n]
            fi, off = id_pos.get((doc, frag)) or id_pos.get((doc, "")) or (0, 0)
            return f"kindle:pos:fid:{b32(fi, 4)}:off:{b32(off, 10)}".encode()

        text = _PLACE.sub(patch, bytes(text))
        self.flows[0] = text
        positions = {k: (fi, off, frags[fi][0] + off) for k, (fi, off) in id_pos.items()}
        return skel_entries, frag_entries, frag_cncx, positions


def _deep_copy(el):
    import copy
    return copy.deepcopy(el)


def _ncx_entries(book: Book, pos_of):
    """NCX entries ordered by depth (all top-level first), with parent/child links."""
    flat = []  # (depth, title, position, fid tuple, parent index in flat)

    def walk(entries, depth, parent):
        for e in entries:
            doc, _, frag = e.href.partition("#")
            p = pos_of(doc, frag)
            if p is None:
                p = pos_of(doc, "")
            if p is None:
                walk(e.children, depth, parent)
                continue
            idx = len(flat)
            flat.append([depth, " ".join(e.title.split()) or "Untitled", p, parent])
            walk(e.children, depth + 1, idx)

    walk(book.toc, 0, -1)
    if not flat:
        return []
    order = sorted(range(len(flat)), key=lambda i: (flat[i][0], i))
    new_index = {old: new for new, old in enumerate(order)}
    entries = []
    for new, old in enumerate(order):
        depth, title, p, parent = flat[old]
        entries.append({"depth": depth, "title": title, "pos": p, "parent": new_index.get(parent, -1), "children": []})
    for i, e in enumerate(entries):
        if e["parent"] >= 0:
            entries[e["parent"]]["children"].append(i)
    return entries


def _ncx_index(entries, text_len: int, kf8: bool) -> list[bytes]:
    cncx = CNCX()
    positions = sorted({(e["pos"][2] if kf8 else e["pos"]) for e in entries})
    width = max(2, len(f"{len(entries) - 1:X}"))
    out = []
    for i, e in enumerate(entries):
        start = e["pos"][2] if kf8 else e["pos"]
        k = bisect.bisect_right(positions, start)  # the next entry's start (a scan of them all was quadratic)
        length = (positions[k] if k < len(positions) else text_len) - start
        tags = {1: [start], 2: [max(0, length)], 3: [cncx.add(e["title"])], 4: [e["depth"]]}
        if e["parent"] >= 0:
            tags[21] = [e["parent"]]
        if e["children"]:
            tags[22] = [e["children"][0]]
            tags[23] = [e["children"][-1]]
        if kf8:
            tags[6] = [e["pos"][0], e["pos"][1]]
        out.append((f"{i:0{width}X}", tags))
    tagx = [(1, 1, 1, 0), (2, 1, 2, 0), (3, 1, 4, 0), (4, 1, 8, 0), (21, 1, 16, 0), (22, 1, 32, 0), (23, 1, 64, 0)]
    if kf8:
        tagx.append((6, 2, 128, 0))
    tagx.append((0, 0, 0, 1))
    return build_index(out, tagx, 1, cncx.finish())


def build_kf8(book: Book, res: Resources):
    """KF8 text records and index records (relative layout handled by the caller)."""
    kb = KF8Builder(book, res)
    skel, frags, frag_cncx, positions = kb.build()
    flow_bounds = []
    text = bytearray()
    for flow in kb.flows:
        flow_bounds.append((len(text), len(text) + len(flow)))
        text += flow
    text = bytes(text)

    def pos_of(doc, frag):
        return positions.get((doc, frag))

    ncx_entries = _ncx_entries(book, pos_of)
    ncx = _ncx_index(ncx_entries, len(kb.flows[0]), kf8=True) if ncx_entries else []
    frag_tagx = [(2, 1, 1, 0), (3, 1, 2, 0), (4, 1, 4, 0), (6, 2, 8, 0), (0, 0, 0, 1)]
    skel_tagx = [(1, 1, 3, 0), (6, 2, 12, 0), (0, 0, 0, 1)]
    frag_recs = build_index(frags, frag_tagx, 1, frag_cncx.finish())
    skel_recs = build_index(skel, skel_tagx, 1)
    guide_recs = []
    guide = []
    gcncx = CNCX()
    for lm in book.landmarks:
        doc, _, frag = lm.href.partition("#")
        p = pos_of(doc, frag) or pos_of(doc, "")
        if p is None:
            continue
        kind = {"bodymatter": "text", "titlepage": "title-page"}.get(lm.type, lm.type)
        guide.append((kind, {1: [gcncx.add(lm.title or kind)], 6: [p[0], p[1]]}))
    if guide:
        seen = set()
        guide = sorted([g for g in guide if not (g[0] in seen or seen.add(g[0]))], key=lambda g: g[0])
        guide_recs = build_index(guide, [(1, 1, 1, 0), (6, 2, 2, 0), (0, 0, 0, 1)], 1, gcncx.finish())
    fdst = b"FDST" + struct.pack(">II", 12, len(flow_bounds)) + b"".join(struct.pack(">II", s, e) for s, e in flow_bounds)
    start_pos = None
    body = next((lm for lm in book.landmarks if lm.type == "bodymatter"), None)
    if body is not None:
        doc, _, frag = body.href.partition("#")
        p = pos_of(doc, frag) or pos_of(doc, "")
        if p is not None:
            start_pos = p[2]
    return text, frag_recs, skel_recs, guide_recs, ncx, fdst, len(flow_bounds), start_pos


# --- MOBI 6 --------------------------------------------------------------------------------------------

def build_mobi6(book: Book, res: Resources):
    """MOBI 6 HTML from the book's blocks: (text bytes, NCX records, start reading offset)."""
    from ..flow import book_blocks
    blocks = book_blocks(book)
    out = bytearray()
    anchors: dict[str, int] = {}
    links: list[tuple[int, str]] = []  # (offset of the 10 placeholder digits, target)
    head = (b"<html><head><guide>" + b"%GUIDE%" + b"</guide></head><body>")
    out += head
    quote_depth = 0

    def w(s: str):
        out.extend(s.encode("utf-8"))

    for b in blocks:
        if b.page_break and len(out) > len(head):
            w("<mbp:pagebreak/>")
        want_quote = (1 if b.quote else 0)
        while quote_depth < want_quote:
            w("<blockquote>")
            quote_depth += 1
        while quote_depth > want_quote:
            w("</blockquote>")
            quote_depth -= 1
        pos = len(out)
        for a in b.anchors:
            anchors.setdefault(a, pos)
            anchors.setdefault(a.split("#")[0], pos)
        if b.kind == "hr":
            w("<hr/>")
            continue
        if b.kind == "image":
            idx = res.index(b.image)
            if idx is not None:
                alt = f' alt="{markup._esc_attr(b.alt)}"' if b.alt else ""
                w(f'<p align="center"><img recindex="{idx:05d}"{alt} /></p>')
            continue
        if b.kind == "table":
            w('<table border="1">')
            for row in b.rows:
                w("<tr>")
                for c in row:
                    tag = "th" if c.header else "td"
                    span = f' colspan="{c.colspan}"' if c.colspan > 1 else ""
                    w(f"<{tag}{span}>")
                    _runs(c.runs, w, out, anchors, links, res)
                    w(f"</{tag}>")
                w("</tr>")
            w("</table>")
            continue
        if b.kind == "h":
            tag = f"h{min(max(b.level, 1), 6)}"
        else:
            tag = "p"
        attrs = []
        align = b.align if b.align in ("center", "right", "justify", "left") else ""
        if b.kind == "h" and not align:
            align = ""
        if align:
            attrs.append(f'align="{align}"')
        if b.kind == "p" and abs(b.indent) > 0.01:
            attrs.append(f'width="{b.indent:.2g}em"')
        if b.margin_top >= 0.5:
            attrs.append(f'height="{min(b.margin_top, 5):.2g}em"')
        indent = b.margin_left + (b.depth * 1.5 if b.marker else 0)
        open_bq = int(min(indent / 2.5, 4)) if not b.quote else 0
        w("<blockquote>" * open_bq)
        w(f"<{tag}{' ' + ' '.join(attrs) if attrs else ''}>")
        if b.size and abs(b.size - 1) > 0.15 and b.kind != "h":
            size = 2 if b.size < 0.9 else 4 if b.size < 1.4 else 5
            w(f'<font size="{size}">')
            close_font = True
        else:
            close_font = False
        if b.marker:
            w(markup.escape(b.marker) + " ")
        if b.kind == "pre":
            w("<tt>")
        _runs(b.runs, w, out, anchors, links, res, pre=b.kind == "pre")
        if b.kind == "pre":
            w("</tt>")
        if close_font:
            w("</font>")
        w(f"</{tag}>")
        w("</blockquote>" * open_bq)
    while quote_depth:
        w("</blockquote>")
        quote_depth -= 1
    w("</body></html>")
    # the guide goes in the head
    guide_parts = []
    for lm in book.landmarks:
        doc = lm.href
        if lm.type in ("toc", "bodymatter", "cover", "titlepage") and (doc in anchors or doc.split("#")[0] in anchors):
            kind = {"bodymatter": "text", "titlepage": "title-page"}.get(lm.type, lm.type)
            guide_parts.append((kind, lm.title or kind, doc))
    guide_html = bytearray()
    guide_links = []
    for kind, title, doc in guide_parts:
        guide_html += f'<reference type="{kind}" title="{markup._esc_attr(title)}" filepos='.encode()
        guide_links.append((len(guide_html), doc))
        guide_html += b"0000000000 />"
    gi = out.find(b"%GUIDE%")
    shift = len(guide_html) - len(b"%GUIDE%")
    out[gi:gi + 7] = guide_html
    links = [(p + shift, t) for p, t in links]
    anchors = {k: v + shift for k, v in anchors.items()}
    links += [(gi + p, t) for p, t in guide_links]
    for p, target in links:
        dest = anchors.get(target)
        if dest is None:
            dest = anchors.get(target.split("#")[0], 0)
        out[p:p + 10] = b"%010d" % dest
    text = bytes(out)

    def pos_of(doc, frag):
        key = f"{doc}#{frag}" if frag else doc
        return anchors.get(key)

    entries = _ncx_entries(book, pos_of)
    ncx = _ncx_index(entries, len(text), kf8=False) if entries else []
    start = None
    body = next((lm for lm in book.landmarks if lm.type == "bodymatter"), None)
    if body is not None:
        start = anchors.get(body.href) or anchors.get(body.href.split("#")[0])
    return text, ncx, start


def _runs(runs, w, out, anchors, links, res, pre=False):
    for r in runs:
        pos = len(out)
        for a in r.anchors:
            anchors.setdefault(a, pos)
        if r.image:
            idx = res.index(r.image)
            if idx is not None:
                w(f'<img recindex="{idx:05d}" />')
            continue
        if r.text == "\n":
            w("<br/>")
            continue
        text = markup.escape(markup.clean_text(r.text))
        text = text.replace("\n", "<br/>")
        if pre:
            text = text.replace("  ", "&nbsp; ")
        opens, closes = [], []
        for flag, tag in ((r.bold, "b"), (r.italic, "i"), (r.underline, "u"), (r.strike, "s"), (r.sup, "sup"),
                          (r.sub, "sub"), (r.mono and not pre, "tt")):
            if flag:
                opens.append(f"<{tag}>")
                closes.insert(0, f"</{tag}>")
        if r.size and abs(r.size - 1) > 0.15 and not (r.sup or r.sub):
            size = 2 if r.size < 0.9 else 4 if r.size < 1.4 else 5
            opens.insert(0, f'<font size="{size}">')
            closes.append("</font>")
        if r.href:
            if is_external(r.href):
                opens.insert(0, f'<a href="{markup._esc_attr(r.href)}">')
                closes.append("</a>")
            else:
                w("<a filepos=")
                links.append((len(out), r.href))
                w("0000000000>")
                closes.append("</a>")
        w("".join(opens) + text + "".join(closes))


# --- the file ------------------------------------------------------------------------------------------

def _prepare(book: Book) -> Book:
    from .. import clean
    from ..toc import fix_links
    b = book.copy()
    clean.clean_book(b)
    fix_links(b)
    if not b.toc:
        b.ensure_toc()
    if not b.metadata.title:
        b.metadata.title = "Untitled"
    if not b.spine:
        root, body = markup.new_document(b.metadata.title)
        ET.SubElement(body, "p").text = b.metadata.title
        b.add_document("text/part0001.xhtml", root)
    # a cover page made only for EPUB isn't needed: Kindles show the cover from metadata
    return b


def write(book: Book, path, fmt: str = "mobi", compress: bool = True, fonts: bool = True, **_opts) -> None:
    if fmt not in ("mobi", "azw", "azw3"):
        raise BookportError(f"Unknown Kindle format {fmt}")
    b = _prepare(book)
    res = Resources(b, fonts=fonts)
    cover_index = None
    if b.cover is not None:
        idx = res.index(b.cover)
        cover_index = idx - 1 if idx is not None else None
    uid = random.Random(b.metadata.uid()).getrandbits(32)
    kf8_text, frag_recs, skel_recs, guide_recs, ncx8, fdst, nflows, start8 = build_kf8(b, res)
    kf8_texts = text_records(kf8_text, compress)
    records: list[bytes] = []
    if fmt == "azw3":
        # KF8 only: r0, text, padding, indexes, resources, FDST, FLIS, FCIS, EOF
        n_text = len(kf8_texts)
        i = 1 + n_text + 1
        frag_i = i
        i += len(frag_recs)
        skel_i = i
        i += len(skel_recs)
        guide_i = i if guide_recs else 0xFFFFFFFF
        i += len(guide_recs)
        ncx_i = i if ncx8 else 0xFFFFFFFF
        i += len(ncx8)
        first_image = i
        i += len(res.records)
        fdst_i, flis_i, fcis_i = i, i + 1, i + 2
        extra = [(125, len(res.records))]
        if cover_index is not None:
            extra.append((129, f"kindle:embed:{b32(cover_index + 1, 4)}"))
        if start8 is not None:
            extra.append((116, start8))
        exth = _metadata_exth(b, cover_index, extra)
        r0 = record0(b, version=8, text_length=len(kf8_text), n_text=n_text, first_non_book=frag_i,
                     first_image=first_image, exth_items=exth, ncx=ncx_i, fcis_i=fcis_i, flis_i=flis_i,
                     fdst=fdst_i, fdst_count=nflows, frag=frag_i, skel=skel_i, guide=guide_i, uid=uid, compression=2 if compress else 1)
        records = [r0] + kf8_texts + [b"\0\0"] + frag_recs + skel_recs + guide_recs + ncx8 + res.records + \
            [fdst, FLIS, fcis(len(kf8_text)), b"\xe9\x8e\r\n"]
    else:
        text6, ncx6, start6 = build_mobi6(b, res)
        texts6 = text_records(text6, compress)
        n6 = len(texts6)
        i = 1 + n6
        ncx6_i = i if ncx6 else 0xFFFFFFFF
        i += len(ncx6)
        first_image6 = i
        i += len(res.records)
        flis6, fcis6 = i, i + 1
        last_content = i - 1
        boundary_i = i + 2
        k0 = boundary_i + 1  # KF8 record 0
        # KF8 indexes are relative to its record 0
        j = 1 + len(kf8_texts) + 1
        frag_i = j
        j += len(frag_recs)
        skel_i = j
        j += len(skel_recs)
        guide_i = j if guide_recs else 0xFFFFFFFF
        j += len(guide_recs)
        ncx_i = j if ncx8 else 0xFFFFFFFF
        j += len(ncx8)
        fdst_i, flis_i, fcis_i = j, j + 1, j + 2
        extra6 = [(121, k0), (125, len(res.records))]
        if cover_index is not None:
            extra6.append((129, f"kindle:embed:{b32(cover_index + 1, 4)}"))
        if start6 is not None:
            extra6.append((116, start6))
        exth6 = _metadata_exth(b, cover_index, extra6)
        r0_6 = record0(b, version=6, text_length=len(text6), n_text=n6, first_non_book=1 + n6, first_image=first_image6,
                       exth_items=exth6, ncx=ncx6_i, fcis_i=fcis6, flis_i=flis6, last_content=last_content, uid=uid, compression=2 if compress else 1)
        extra8 = [(125, 0)]
        if cover_index is not None:
            extra8.append((129, f"kindle:embed:{b32(cover_index + 1, 4)}"))
        if start8 is not None:
            extra8.append((116, start8))
        exth8 = _metadata_exth(b, cover_index, extra8)
        r0_8 = record0(b, version=8, text_length=len(kf8_text), n_text=len(kf8_texts), first_non_book=frag_i,
                       first_image=fdst_i, exth_items=exth8, ncx=ncx_i, fcis_i=fcis_i, flis_i=flis_i, fdst=fdst_i,
                       fdst_count=nflows, frag=frag_i, skel=skel_i, guide=guide_i, uid=uid, compression=2 if compress else 1)
        records = [r0_6] + texts6 + ncx6 + res.records + [FLIS, fcis(len(text6)), b"BOUNDARY", r0_8] + kf8_texts + \
            [b"\0\0"] + frag_recs + skel_recs + guide_recs + ncx8 + [fdst, FLIS, fcis(len(kf8_text)), b"\xe9\x8e\r\n"]
    data = pdb_bytes(b.metadata.title or "book", b"BOOK", b"MOBI", records)
    with open(path, "wb") as f:
        f.write(data)
