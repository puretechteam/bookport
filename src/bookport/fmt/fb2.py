"""FictionBook 2 (FB2) and zipped FB2: read and write.

Follows the FictionBook 2.0/2.1 schema: <description> (title-info, document-info,
publish-info), <body> sections with titles, epigraphs, poems, cites, tables and notes bodies,
and images stored base64 in <binary> elements.
"""

from __future__ import annotations

import base64
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import ClassVar

from .. import limits, markup
from ..book import IMAGE_EXT, Book, Metadata, TocEntry, is_external, relative, resolve, sniff_image
from ..errors import BookportError

FB2_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK = "http://www.w3.org/1999/xlink"


# --- reading -----------------------------------------------------------------------------------

def read(path) -> Book:
    p = Path(path)
    data = p.read_bytes()
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                name = next((n for n in z.namelist() if n.lower().endswith(".fb2")), None)
                if name is None:
                    raise BookportError("This zipped FB2 has no .fb2 file in it")
                data = limits.zip_read(z, name)
        except zipfile.BadZipFile as e:
            raise BookportError(f"Damaged FB2 ZIP: {e}") from None
    return from_bytes(data)


def _parse(data: bytes) -> ET.Element:
    text = markup.decode(data)
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    markup.check_xml(text, None, "This FB2 file")
    parser = markup.xml_parser()
    try:
        parser.feed(text)
        root = parser.close()
    except ET.ParseError:
        # undeclared prefixes or stray ampersands are common in FB2 found in the wild
        fixed = re.sub(r"&(?!#?\w+;)", "&amp;", text)
        fixed = re.sub(r"\b(l|xlink):href=", "href=", fixed)
        try:
            parser = markup.xml_parser()
            parser.feed(fixed)
            root = parser.close()
        except ET.ParseError as e:
            raise BookportError(f"Damaged FB2 file: {e}") from None
    markup.check_xml("", root, "This FB2 file")
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        if el.attrib:
            el.attrib = {(k.split("}", 1)[1] if "}" in k else k): v for k, v in el.attrib.items()}
    return root


def _t(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def from_bytes(data: bytes) -> Book:
    root = _parse(data)
    if root.tag != "FictionBook":
        raise BookportError("Not a FictionBook file")
    book = Book(Metadata())
    book.source_format = "fb2"
    desc = root.find("description")
    binaries = {}
    for b in root.findall("binary"):
        bid = b.get("id", "")
        try:
            raw = base64.b64decode("".join((b.text or "").split()), validate=False)
        except ValueError:
            continue
        mt = sniff_image(raw) or b.get("content-type", "image/jpeg")
        name = re.sub(r"[^\w.-]", "_", bid) or f"image{len(binaries)}"
        if not name.lower().endswith(tuple(IMAGE_EXT.values())) and mt in IMAGE_EXT:
            name += IMAGE_EXT[mt]
        binaries[bid] = book.add(f"images/{name}", raw, mt).href
    _read_description(book, desc, binaries)
    bodies = root.findall("body")
    if not bodies:
        raise BookportError("This FB2 file has no <body>")
    conv = _Converter(book, binaries)
    for body in bodies:
        conv.body(body)
    conv.fix_links()
    book.toc = conv.toc
    if not book.toc:
        book.ensure_toc()
    return book


def _read_description(book, desc, binaries):
    if desc is None:
        return
    md = book.metadata
    ti = desc.find("title-info")
    if ti is not None:
        md.title = _t(ti.find("book-title"))
        for a in ti.findall("author"):
            name = " ".join(x for x in (_t(a.find("first-name")), _t(a.find("middle-name")), _t(a.find("last-name"))) if x)
            name = name or _t(a.find("nickname"))
            if name:
                md.authors.append(name)
        md.language = _t(ti.find("lang"))
        ann = ti.find("annotation")
        if ann is not None:
            md.description = "".join(f"<p>{markup.escape(_t(p))}</p>" for p in ann.iter("p")) or _t(ann)
        md.subjects = [_t(g) for g in ti.findall("genre") if _t(g)]
        kw = _t(ti.find("keywords"))
        if kw:
            md.subjects += [k.strip() for k in kw.split(",") if k.strip() and k.strip() not in md.subjects]
        date = ti.find("date")
        if date is not None:
            md.date = date.get("value") or _t(date)
        seq = ti.find("sequence")
        if seq is not None and seq.get("name"):
            md.series = seq.get("name")
            try:
                md.series_index = float(seq.get("number")) if seq.get("number") else None
            except ValueError:
                pass
        cover = ti.find("coverpage/image")
        if cover is not None:
            ref = (cover.get("href") or "").lstrip("#")
            if ref in binaries:
                book.cover = binaries[ref]
        for t in ti.findall("translator"):
            name = " ".join(x for x in (_t(t.find("first-name")), _t(t.find("last-name"))) if x)
            if name:
                md.contributors.append(name)
    pi = desc.find("publish-info")
    if pi is not None:
        md.publisher = _t(pi.find("publisher"))
        isbn = _t(pi.find("isbn"))
        if isbn:
            md.identifiers["isbn"] = isbn
        year = _t(pi.find("year"))
        if year and not md.date:
            md.date = year
    di = desc.find("document-info")
    if di is not None:
        doc_id = _t(di.find("id"))
        if doc_id:
            md.identifiers.setdefault("fb2-id", doc_id)


class _Converter:
    """FB2 body XML to XHTML documents (one per top-level section)."""

    def __init__(self, book, binaries):
        self.book = book
        self.binaries = binaries
        self.toc: list[TocEntry] = []
        self.ids: dict[str, str] = {}  # fb2 id -> document href
        self.n = 0
        self.css_href = book.add("styles/fb2.css", FB2_CSS, "text/css").href

    def new_doc(self):
        self.n += 1
        href = f"text/part{self.n:04d}.xhtml"
        root, body = markup.new_document(self.book.metadata.title, self.book.metadata.language,
                                         css_hrefs=[relative(href, self.css_href)])
        return href, root, body

    def body(self, body):
        name = body.get("name", "")
        notes = name in ("notes", "comments")
        sections = body.findall("section")
        title = body.find("title")
        if notes or not sections:
            href, root, html_body = self.new_doc()
            if notes:
                html_body.set("class", "notes")
            for child in body:
                self.block(child, html_body, href, 1, None)
            self.book.add_document(href, root)
            if notes:
                t = _t(title) or ("Notes" if name == "notes" else "Comments")
                self.toc.append(TocEntry(t, href))
            return
        # text before the first section (the book title, epigraphs) gets its own page
        lead = [c for c in body if c.tag != "section"]
        if lead:
            href, root, html_body = self.new_doc()
            for child in lead:
                self.block(child, html_body, href, 1, None)
            self.book.add_document(href, root)
        for sec in sections:
            href, root, html_body = self.new_doc()
            entry = self.section(sec, html_body, href, 1)
            self.book.add_document(href, root)
            if entry is not None:
                self.toc.append(entry)

    def section(self, sec, parent, href, depth):
        div = ET.SubElement(parent, "div", {"class": "section"})
        sid = sec.get("id")
        if sid:
            div.set("id", self._id(sid, href))
        entry = None
        for child in sec:
            if child.tag == "title":
                h = ET.SubElement(div, f"h{min(depth, 6)}")
                if not div.get("id"):
                    div.set("id", f"s{len(self.ids) + id(div) % 100000}")
                    self.ids[div.get("id")] = href
                first = True
                for p in child:
                    if p.tag == "p":
                        if not first:
                            ET.SubElement(h, "br")
                        self.inline_into(p, h, href)
                        first = False
                    elif p.tag == "empty-line":
                        pass
                title = " ".join(markup.text_of(h).split())
                entry = TocEntry(title, f"{href}#{div.get('id')}")
            elif child.tag == "section":
                sub = self.section(child, div, href, depth + 1)
                if sub is not None:
                    if entry is None:
                        entry = TocEntry("", f"{href}#{div.get('id')}" if div.get("id") else href)
                    entry.children.append(sub)
            else:
                self.block(child, div, href, depth, None)
        if entry is not None and not entry.title:
            if entry.children:
                kids = entry.children
                return kids[0] if len(kids) == 1 else TocEntry(kids[0].title, entry.href, kids)
            return None
        return entry

    def _id(self, fid, href):
        safe = re.sub(r"[^\w.-]", "_", fid)
        if not safe or not (safe[0].isalpha() or safe[0] == "_"):
            safe = "n_" + safe
        self.ids[fid] = href
        self.ids[safe] = href
        return safe

    def block(self, el, parent, href, depth, cls):
        tag = el.tag
        if tag == "p":
            p = ET.SubElement(parent, "p")
            if cls:
                p.set("class", cls)
            if el.get("id"):
                p.set("id", self._id(el.get("id"), href))
            self.inline_into(el, p, href)
        elif tag == "empty-line":
            ET.SubElement(parent, "p", {"class": "empty-line"}).text = " "
        elif tag == "subtitle":
            p = ET.SubElement(parent, "p", {"class": "subtitle"})
            if el.get("id"):
                p.set("id", self._id(el.get("id"), href))
            self.inline_into(el, p, href)
        elif tag == "image":
            self.image(el, parent, block=True)
        elif tag in ("epigraph", "cite", "annotation"):
            kind = {"epigraph": "epigraph", "cite": "cite", "annotation": "annotation"}[tag]
            div = ET.SubElement(parent, "blockquote" if tag == "cite" else "div", {"class": kind})
            if el.get("id"):
                div.set("id", self._id(el.get("id"), href))
            for c in el:
                if c.tag == "text-author":
                    p = ET.SubElement(div, "p", {"class": "text-author"})
                    self.inline_into(c, p, href)
                else:
                    self.block(c, div, href, depth, None)
        elif tag == "poem":
            div = ET.SubElement(parent, "div", {"class": "poem"})
            if el.get("id"):
                div.set("id", self._id(el.get("id"), href))
            for c in el:
                if c.tag == "title":
                    h = ET.SubElement(div, "p", {"class": "poem-title"})
                    self.inline_into(c.find("p") if c.find("p") is not None else c, h, href)
                elif c.tag == "stanza":
                    st = ET.SubElement(div, "div", {"class": "stanza"})
                    for v in c:
                        if v.tag == "v":
                            p = ET.SubElement(st, "p", {"class": "v"})
                            self.inline_into(v, p, href)
                        elif v.tag in ("title", "subtitle"):
                            p = ET.SubElement(st, "p", {"class": "subtitle"})
                            self.inline_into(v.find("p") if v.find("p") is not None else v, p, href)
                elif c.tag == "text-author":
                    p = ET.SubElement(div, "p", {"class": "text-author"})
                    self.inline_into(c, p, href)
                elif c.tag == "date":
                    p = ET.SubElement(div, "p", {"class": "date"})
                    p.text = _t(c)
                else:
                    self.block(c, div, href, depth, None)
        elif tag == "table":
            table = ET.SubElement(parent, "table")
            for tr in el:
                if tr.tag != "tr":
                    continue
                row = ET.SubElement(table, "tr")
                for cell in tr:
                    if cell.tag in ("td", "th"):
                        c = ET.SubElement(row, cell.tag)
                        for a in ("colspan", "rowspan"):
                            if cell.get(a):
                                c.set(a, cell.get(a))
                        self.inline_into(cell, c, href)
        elif tag == "section":
            self.section(el, parent, href, depth + 1)
        elif tag == "title":
            h = ET.SubElement(parent, f"h{min(depth, 6)}")
            for p in el:
                if p.tag == "p":
                    if len(h) or h.text:
                        ET.SubElement(h, "br")
                    self.inline_into(p, h, href)
        elif tag == "text-author":
            p = ET.SubElement(parent, "p", {"class": "text-author"})
            self.inline_into(el, p, href)

    def image(self, el, parent, block):
        ref = (el.get("href") or "").lstrip("#")
        src = self.binaries.get(ref)
        if src is None:
            return
        img = ET.Element("img", {"src": "../" + src if not src.startswith("../") else src, "alt": el.get("alt", "")})
        if block:
            div = ET.SubElement(parent, "div", {"class": "image"})
            div.append(img)
            if el.get("id"):
                div.set("id", re.sub(r"[^\w.-]", "_", el.get("id")))
        else:
            parent.append(img)

    INLINE: ClassVar[dict] = {"emphasis": "em", "strong": "strong", "strikethrough": "del", "sub": "sub", "sup": "sup", "code": "code",
              "style": "span"}

    def inline_into(self, el, target, href):
        markup.append_text(target, el.text or "")
        for c in el:
            tag = c.tag
            if tag in self.INLINE:
                new = ET.SubElement(target, self.INLINE[tag])
                if tag == "style" and c.get("name"):
                    new.set("class", re.sub(r"[^\w-]", "_", c.get("name")))
                self.inline_into(c, new, href)
            elif tag == "a":
                link = c.get("href", "")
                a = ET.SubElement(target, "a", {"href": link})
                if c.get("type") == "note":
                    a.set("class", "noteref")
                    a.set("epub:type", "noteref")
                self.inline_into(c, a, href)
            elif tag == "image":
                self.image(c, target, block=False)
            else:
                self.inline_into(c, target, href)
            markup.append_text(target, c.tail or "")

    def fix_links(self):
        """FB2 links are '#id' across the whole file; point them at the right document."""
        for href, res in list(self.book.resources.items()):
            if not res.is_document:
                continue
            root = self.book.tree(href)
            changed = False
            for a in root.iter("a"):
                link = a.get("href", "")
                if link.startswith("#"):
                    fid = link[1:]
                    doc = self.ids.get(fid)
                    safe = re.sub(r"[^\w.-]", "_", fid)
                    if safe and not (safe[0].isalpha() or safe[0] == "_"):
                        safe = "n_" + safe
                    if doc:
                        a.set("href", relative(href, f"{doc}#{safe}"))
                        changed = True
            if changed:
                self.book.set_tree(href, root)


FB2_CSS = """
p { margin: 0; text-indent: 1.5em; text-align: justify }
h1, h2, h3, h4, h5, h6 { text-align: center; text-indent: 0 }
.subtitle, .poem-title { text-align: center; text-indent: 0; font-weight: bold; margin: 1em 0 }
.epigraph { margin: 1em 0 1em 30%; font-style: italic }
.cite { margin: 1em 2em }
.poem { margin: 1em 0 1em 2em }
.stanza { margin: 0 0 1em 0 }
.v { text-indent: 0; text-align: left }
.text-author { text-align: right; font-style: italic; text-indent: 0 }
.empty-line { text-indent: 0 }
.image { text-align: center; text-indent: 0; margin: 1em 0 }
.image img { max-width: 100% }
.notes p { text-indent: 0 }
""".strip()


# --- writing -----------------------------------------------------------------------------------

class _Writer:
    def __init__(self, book: Book):
        self.book = book
        self.binaries: dict[str, tuple[str, str]] = {}  # href -> (id, content type)
        self.anchor_ids: dict[str, str] = {}
        self.targets = self._link_targets()

    def _link_targets(self):
        targets = set()
        for href, _ in self.book.documents():
            for a in self.book.tree(href).iter("a"):
                link = a.get("href", "")
                if link and not is_external(link):
                    t = resolve(href, link)
                    frag = link.split("#", 1)[1] if "#" in link else ""
                    targets.add(f"{t}#{frag}" if frag else t)
        return targets

    def fid(self, key: str) -> str:
        if key not in self.anchor_ids:
            self.anchor_ids[key] = f"a{len(self.anchor_ids) + 1}"
        return self.anchor_ids[key]

    def image_id(self, href: str) -> str | None:
        res = self.book.resources.get(href)
        if res is None or not res.is_image:
            return None
        if href not in self.binaries:
            data, mt = res.data, res.media_type
            if mt not in ("image/jpeg", "image/png"):
                from ..images import to_png
                converted = to_png(data)
                if converted is None:
                    return None
                data, mt = converted, "image/png"
                self.book.resources[href] = type(res)(res.href, mt, data)
            base = re.sub(r"[^\w.-]", "_", href.split("/")[-1])
            if mt == "image/png" and not base.lower().endswith(".png"):
                base = base.rsplit(".", 1)[0] + ".png"
            bid = base if not base[0].isdigit() else "img" + base
            while bid in {v[0] for v in self.binaries.values()}:
                bid = f"{len(self.binaries)}_{bid}"
            self.binaries[href] = (bid, mt)
        return self.binaries[href][0]


def write(book: Book, path, fmt: str = "fb2", **_) -> None:
    data = to_bytes(book)
    if fmt == "fbz" or str(path).lower().endswith(".zip"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            name = re.sub(r'[\\/:*?"<>|]', "_", book.metadata.title or "book")[:80] + ".fb2"
            z.writestr(name, data)
        data = buf.getvalue()
    Path(path).write_bytes(data)


def to_bytes(book: Book) -> bytes:
    from ..flow import book_blocks
    w = _Writer(book)
    md = book.metadata
    blocks = book_blocks(book)
    out: list[str] = []
    esc = markup._esc_text
    out.append('<?xml version="1.0" encoding="utf-8"?>\n')
    out.append(f'<FictionBook xmlns="{FB2_NS}" xmlns:l="{XLINK}">\n<description>\n<title-info>\n')
    genres = [s for s in md.subjects if re.fullmatch(r"[a-z_]+", s)] or ["prose"]
    for g in genres[:5]:
        out.append(f"<genre>{esc(g)}</genre>\n")
    for a in md.authors or ["Unknown"]:
        out.append("<author>" + _author_xml(a) + "</author>\n")
    out.append(f"<book-title>{esc(md.title or 'Untitled')}</book-title>\n")
    if md.description:
        desc = re.sub(r"<[^>]+>", "\n", md.description)
        paras = [" ".join(p.split()) for p in desc.split("\n") if p.strip()]
        out.append("<annotation>" + "".join(f"<p>{esc(p)}</p>" for p in paras) + "</annotation>\n")
    keywords = [s for s in md.subjects if s not in genres]
    if keywords:
        out.append(f"<keywords>{esc(', '.join(keywords))}</keywords>\n")
    if md.date:
        year = md.date[:10]
        out.append(f'<date value="{esc(year)}">{esc(md.date[:4])}</date>\n')
    if book.cover:
        cid = w.image_id(book.cover)
        if cid:
            out.append(f'<coverpage><image l:href="#{cid}"/></coverpage>\n')
    out.append(f"<lang>{esc((md.language or 'en').split('-')[0])}</lang>\n")
    if md.series:
        num = f' number="{int(md.series_index)}"' if md.series_index is not None and md.series_index == int(md.series_index) else ""
        out.append(f'<sequence name="{markup._esc_attr(md.series)}"{num}/>\n')
    out.append("</title-info>\n<document-info>\n<author><nickname>bookport</nickname></author>\n")
    out.append("<program-used>bookport</program-used>\n")
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    out.append(f'<date value="{today}">{today}</date>\n')
    out.append(f"<id>{esc(md.uid())}</id>\n<version>1.0</version>\n</document-info>\n")
    if md.publisher or md.identifiers.get("isbn"):
        out.append("<publish-info>\n")
        if md.publisher:
            out.append(f"<publisher>{esc(md.publisher)}</publisher>\n")
        if md.date[:4].isdigit():
            out.append(f"<year>{md.date[:4]}</year>\n")
        if md.identifiers.get("isbn"):
            out.append(f"<isbn>{esc(md.identifiers['isbn'])}</isbn>\n")
        out.append("</publish-info>\n")
    out.append("</description>\n<body>\n")
    out.append(f"<title><p>{esc(md.title or 'Untitled')}</p></title>\n")
    out.append(_sections(blocks, w, book))
    out.append("</body>\n")
    for href, (bid, mt) in w.binaries.items():
        data = book.resources[href].data
        out.append(f'<binary id="{markup._esc_attr(bid)}" content-type="{mt}">')
        b64 = base64.b64encode(data).decode("ascii")
        out.append("\n".join(b64[i:i + 76] for i in range(0, len(b64), 76)))
        out.append("</binary>\n")
    out.append("</FictionBook>\n")
    return markup.clean_text("".join(out)).encode("utf-8")


def _author_xml(name: str) -> str:
    esc = markup._esc_text
    parts = name.split()
    if len(parts) == 1:
        return f"<nickname>{esc(name)}</nickname>"
    first, last = parts[0], parts[-1]
    middle = " ".join(parts[1:-1])
    return f"<first-name>{esc(first)}</first-name>" + (f"<middle-name>{esc(middle)}</middle-name>" if middle else "") + \
        f"<last-name>{esc(last)}</last-name>"


def _sections(blocks, w: _Writer, book: Book) -> str:
    """Blocks to nested <section>s: a heading opens a section at its level."""
    out: list[str] = []
    stack: list[int] = []  # heading levels of open sections
    esc = markup._esc_text
    open_any = False
    # every link target in a block shares the block's one id
    for b in blocks:
        hits = [a for a in b.anchors if a in w.targets]
        if hits:
            first = w.fid(hits[0])
            for a in hits[1:]:
                w.anchor_ids[a] = first

    def open_section(level, anchors):
        nonlocal open_any
        while stack and stack[-1] >= level:
            out.append("</section>\n")
            stack.pop()
        sid = next((w.fid(a) for a in anchors if a in w.targets or a in toc_at), None)
        out.append(f'<section id="{sid}">\n' if sid else "<section>\n")
        stack.append(level)
        open_any = True

    # sections follow the book's TOC when it has one: each entry opens a section at its depth
    toc_at: dict[str, tuple[int, str]] = {}
    for depth, e in book.toc_walk():
        toc_at.setdefault(e.href, (depth + 1, " ".join(e.title.split())))
    used_toc = set()
    for b in blocks:
        hit = next((a for a in b.anchors if a in toc_at and a not in used_toc), None) if toc_at else None
        if hit is not None:
            used_toc.add(hit)
            level, title = toc_at[hit]
            for a in b.anchors:
                if a in toc_at:
                    used_toc.add(a)
            open_section(level, b.anchors + [hit])
            if b.kind == "h" and " ".join(b.text().split()) == title:
                lines = _split_lines(b.runs)
                out.append("<title>" + "".join(f"<p>{_inline(r, w, book)}</p>" for r in lines if _inline(r, w, book).strip())
                           + "</title>\n")
                continue
            out.append(f"<title><p>{esc(title or 'Untitled')}</p></title>\n")
        elif not toc_at and b.kind == "h" and b.level <= 3:
            open_section(b.level, b.anchors)
            lines = _split_lines(b.runs)
            out.append("<title>" + "".join(f"<p>{_inline(r, w, book)}</p>" for r in lines if _inline(r, w, book).strip())
                       + "</title>\n")
            continue
        elif not open_any:
            open_section(1, b.anchors)
        aid = next((w.fid(a) for a in b.anchors if a in w.targets), None)
        id_attr = f' id="{aid}"' if aid else ""
        if b.kind == "image":
            iid = w.image_id(b.image)
            if iid:
                alt = f' alt="{markup._esc_attr(b.alt)}"' if b.alt else ""
                out.append(f'<image l:href="#{iid}"{alt}{id_attr}/>\n')
            continue
        if b.kind == "hr":
            out.append("<empty-line/>\n")
            continue
        if b.kind == "table":
            out.append(f"<table{id_attr}>")
            for row in b.rows:
                out.append("<tr>" + "".join(
                    f"<{'th' if c.header else 'td'}>{_inline(c.runs, w, book)}</{'th' if c.header else 'td'}>" for c in row)
                    + "</tr>")
            out.append("</table>\n")
            continue
        if b.kind == "h":
            out.append(f"<subtitle{id_attr}>{_inline(b.runs, w, book)}</subtitle>\n")
            continue
        lines = _split_lines(b.runs)
        prefix = esc(b.marker + " ") if b.marker else ""
        if b.kind == "pre" or len(lines) > 1 and b.kind != "li":
            for k, line in enumerate(lines):
                text = _inline(line, w, book)
                out.append(f"<p{id_attr if k == 0 else ''}>{text}</p>\n" if text.strip() else "<empty-line/>\n")
            continue
        text = _inline(b.runs, w, book)
        if b.quote:
            out.append(f"<cite><p{id_attr}>{prefix}{text}</p></cite>\n")
        else:
            out.append(f"<p{id_attr}>{prefix}{text}</p>\n")
    while stack:
        out.append("</section>\n")
        stack.pop()
    if not open_any:
        out.append("<section><p/></section>\n")
    return "".join(out)


def _split_lines(runs):
    lines, cur = [], []
    for r in runs:
        if r.text == "\n" and not r.image:
            lines.append(cur)
            cur = []
            continue
        if "\n" in r.text and not r.image:
            parts = r.text.split("\n")
            for k, part in enumerate(parts):
                if k:
                    lines.append(cur)
                    cur = []
                if part:
                    cur.append(type(r)(**{**r.__dict__, "text": part}))
            continue
        cur.append(r)
    lines.append(cur)
    return [line for line in lines if line] or [[]]


def _inline(runs, w: _Writer, book: Book) -> str:
    out = []
    esc = markup._esc_text
    for r in runs:
        if r.image:
            iid = w.image_id(r.image)
            if iid:
                out.append(f'<image l:href="#{iid}"/>')
            continue
        text = esc(r.text.replace("\n", " "))
        if not text:
            continue
        if r.mono:
            text = f"<code>{text}</code>"
        if r.sup:
            text = f"<sup>{text}</sup>"
        elif r.sub:
            text = f"<sub>{text}</sub>"
        if r.strike:
            text = f"<strikethrough>{text}</strikethrough>"
        if r.italic:
            text = f"<emphasis>{text}</emphasis>"
        if r.bold:
            text = f"<strong>{text}</strong>"
        if r.href:
            if is_external(r.href):
                text = f'<a l:href="{markup._esc_attr(r.href)}">{text}</a>'
            elif r.href in w.targets:
                text = f'<a l:href="#{w.fid(r.href)}">{text}</a>'
        out.append(text)
    return "".join(out)
