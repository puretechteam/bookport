"""OpenDocument text (ODT, ODF 1.2/1.3): read.

Headings by outline level, paragraphs and spans with their automatic and named styles (bold,
italic, underline, strike-through, super/subscript, monospace), lists, tables, images in
frames, links, bookmarks, footnotes and endnotes, and the document's meta.xml.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from .. import limits, markup
from ..book import Book, Metadata, relative, sniff_image
from ..errors import BookportError, DRMError

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "dc": "http://purl.org/dc/elements/1.1/",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}


def q(prefix, tag):
    return f"{{{NS[prefix]}}}{tag}"


def read(path) -> Book:
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BookportError(f"Not an OpenDocument file: {e}") from None
    with z:
        try:
            manifest = limits.zip_read(z, "META-INF/manifest.xml")
        except KeyError:
            manifest = b""
        if b"encryption-data" in manifest:
            raise DRMError("OpenDocument", "the document is password protected")
        try:
            content = markup.parse_xml(limits.zip_read(z, "content.xml"), "content.xml")
        except KeyError:
            raise BookportError("Not an OpenDocument text: no content.xml") from None
        except ET.ParseError as e:
            raise BookportError(f"Damaged OpenDocument: {e}") from None
        styles_root = None
        try:
            styles_root = markup.parse_xml(limits.zip_read(z, "styles.xml"), "styles.xml")
        except (KeyError, ET.ParseError):
            pass
        book = Book(_meta(z))
        book.source_format = "odt"
        conv = _Converter(book, z, [content, styles_root])
        body = content.find(f"{q('office', 'body')}/{q('office', 'text')}")
        if body is None:
            raise BookportError("This OpenDocument file has no text body (is it a spreadsheet?)")
        conv.blocks(body, conv.body)
        conv.finish()
    if not book.metadata.title:
        h = next((e for href, _ in book.documents() for e in book.tree(href).iter() if e.tag in ("h1", "h2")), None)
        if h is not None:
            book.metadata.title = " ".join(markup.text_of(h).split())[:200]
    book.ensure_toc()
    return book


def _meta(z) -> Metadata:
    md = Metadata()
    try:
        root = markup.parse_xml(limits.zip_read(z, "meta.xml"), "meta.xml")
    except (KeyError, ET.ParseError):
        return md
    meta = root.find(q("office", "meta"))
    if meta is None:
        return md

    def t(prefix, tag):
        el = meta.find(q(prefix, tag))
        return " ".join((el.text or "").split()) if el is not None and el.text else ""

    md.title = t("dc", "title")
    creator = t("meta", "initial-creator") or t("dc", "creator")
    if creator:
        md.authors = [creator]
    md.description = t("dc", "description")
    if t("dc", "subject"):
        md.subjects.append(t("dc", "subject"))
    md.subjects += [" ".join((k.text or "").split()) for k in meta.findall(q("meta", "keyword")) if k.text]
    md.language = t("dc", "language")
    md.date = t("meta", "creation-date")[:10] or t("dc", "date")[:10]
    return md


class _Converter:
    def __init__(self, book, z, style_roots):
        self.book, self.z = book, z
        self.styles: dict[str, dict] = {}
        self.list_styles: dict[str, dict[int, str]] = {}
        for root in style_roots:
            if root is None:
                continue
            for s in root.iter(q("style", "style")):
                name = s.get(q("style", "name"))
                tp = s.find(q("style", "text-properties"))
                pp = s.find(q("style", "paragraph-properties"))
                self.styles[name] = {
                    "parent": s.get(q("style", "parent-style-name")),
                    "display": s.get(q("style", "display-name"), name or "").lower(),
                    "bold": (tp.get(q("fo", "font-weight")) in ("bold", "600", "700", "800", "900")) if tp is not None and tp.get(q("fo", "font-weight")) else None,
                    "italic": (tp.get(q("fo", "font-style")) in ("italic", "oblique")) if tp is not None and tp.get(q("fo", "font-style")) else None,
                    "underline": tp is not None and tp.get(q("style", "text-underline-style"), "none") != "none",
                    "strike": tp is not None and tp.get(q("style", "text-line-through-style"), "none") != "none",
                    "position": tp.get(q("style", "text-position"), "") if tp is not None else "",
                    "mono": tp is not None and any(m in (tp.get(q("style", "font-name"), "") + tp.get(q("fo", "font-family"), "")).lower()
                                                   for m in ("courier", "mono", "consolas")),
                    "align": pp.get(q("fo", "text-align"), "") if pp is not None else "",
                    "break": pp is not None and pp.get(q("fo", "break-before")) == "page",
                    "outline": s.get(q("style", "default-outline-level")),
                }
            for ls in root.iter(q("text", "list-style")):
                levels = {}
                for lv in ls:
                    level = int(lv.get(q("text", "level"), "1"))
                    levels[level] = "ol" if lv.tag == q("text", "list-level-style-number") else "ul"
                self.list_styles[ls.get(q("style", "name"))] = levels
        self.docs = []
        self.new_doc()
        self.images: dict[str, str] = {}
        self.notes: list[ET.Element] = []
        self.size = 0

    def new_doc(self):
        root, body = markup.new_document(self.book.metadata.title, self.book.metadata.language)
        self.docs.append((root, body))
        self.size = 0

    @property
    def body(self):
        return self.docs[-1][1]

    def prop(self, name, key):
        seen = 0
        while name and name in self.styles and seen < 20:
            v = self.styles[name][key]
            if v not in (None, "", False):
                return v
            name = self.styles[name]["parent"]
            seen += 1
        return None

    def blocks(self, el, parent, list_style=None, depth=1):
        for c in el:
            tag = c.tag
            if tag == q("text", "h"):
                level = c.get(q("text", "outline-level")) or "1"
                level = min(int(level) if level.isdigit() else 1, 6)
                if level == 1 and parent is self.body and self.size > 20000:
                    self.new_doc()
                    parent = self.body
                h = ET.SubElement(parent, f"h{level}")
                self.inline(c, h)
                self.size += len(markup.text_of(h))
            elif tag == q("text", "p"):
                sname = c.get(q("text", "style-name"))
                if self.prop(sname, "break") and parent is self.body and self.size > 20000:
                    self.new_doc()
                    parent = self.body
                display = (self.prop(sname, "display") or "").lower()
                outline = self.prop(sname, "outline")
                if display.startswith("heading") or outline:
                    m = re.search(r"(\d)", display)
                    level = int(m.group(1)) if m else int(outline or 1)
                    el2 = ET.SubElement(parent, f"h{min(level, 6)}")
                elif display in ("title",):
                    el2 = ET.SubElement(parent, "h1")
                elif "preformatted" in display or "code" in display:
                    el2 = ET.SubElement(parent, "pre")
                elif "quot" in display:
                    el2 = ET.SubElement(ET.SubElement(parent, "blockquote"), "p")
                else:
                    el2 = ET.SubElement(parent, "p")
                align = self.prop(sname, "align")
                if align in ("center", "end", "right", "justify"):
                    el2.set("style", f"text-align: {'right' if align == 'end' else align}")
                self.inline(c, el2)
                if el2.tag == "p" and not len(el2) and not (el2.text or "").strip():
                    el2.text = " "
                self.size += len(markup.text_of(el2))
            elif tag == q("text", "list"):
                sname = c.get(q("text", "style-name")) or list_style
                kind = self.list_styles.get(sname, {}).get(depth, "ul")
                lst = ET.SubElement(parent, kind)
                for item in c:
                    if item.tag in (q("text", "list-item"), q("text", "list-header")):
                        li = ET.SubElement(lst, "li")
                        self.blocks(item, li, sname, depth + 1)
                        # a single paragraph in an item needn't keep its <p>
                        if len(li) == 1 and li[0].tag == "p" and not li.text:
                            p = li[0]
                            li.remove(p)
                            li.text = p.text
                            for k in p:
                                li.append(k)
            elif tag == q("table", "table"):
                table = ET.SubElement(parent, "table")
                for row in c.iter(q("table", "table-row")):
                    tr = ET.SubElement(table, "tr")
                    for cell in row:
                        if cell.tag == q("table", "table-cell"):
                            td = ET.SubElement(tr, "td")
                            span = cell.get(q("table", "number-columns-spanned"))
                            if span and span.isdigit() and int(span) > 1:
                                td.set("colspan", span)
                            self.blocks(cell, td)
            elif tag in (q("text", "section"), q("text", "index-body"), q("text", "table-of-content"),
                         q("text", "alphabetical-index"), q("text", "illustration-index")):
                if tag in (q("text", "table-of-content"),):
                    continue  # the book gets its own TOC
                self.blocks(c, parent, list_style, depth)
            elif tag == q("draw", "frame"):
                self.frame(c, parent)

    def inline(self, el, target, style=None):
        markup.append_text(target, el.text or "")
        for c in el:
            tag = c.tag
            dest = target
            if tag == q("text", "span"):
                sname = c.get(q("text", "style-name"))
                dest = self._styled(target, sname)
                self.inline(c, dest)
            elif tag == q("text", "a"):
                href = c.get(q("xlink", "href"), "")
                if href.startswith("#"):
                    href = "#" + _safe(href[1:])
                a = ET.SubElement(target, "a", {"href": href} if href else {})
                self.inline(c, a)
            elif tag == q("text", "s"):
                n = c.get(q("text", "c"), "1")
                markup.append_text(target, " " * (int(n) if n.isdigit() else 1))
            elif tag == q("text", "tab"):
                markup.append_text(target, "\t")
            elif tag == q("text", "line-break"):
                ET.SubElement(target, "br")
            elif tag in (q("text", "bookmark"), q("text", "bookmark-start")):
                ET.SubElement(target, "a", {"id": _safe(c.get(q("text", "name"), ""))})
            elif tag == q("text", "note"):
                self.note(c, target)
            elif tag == q("draw", "frame"):
                self.frame(c, target)
            elif tag in (q("text", "soft-page-break"), q("text", "bookmark-end"), q("office", "annotation"),
                         q("office", "annotation-end")):
                pass
            else:
                self.inline(c, target)
            markup.append_text(target, c.tail or "")

    def _styled(self, target, sname):
        dest = target
        if self.prop(sname, "bold"):
            dest = ET.SubElement(dest, "b")
        if self.prop(sname, "italic"):
            dest = ET.SubElement(dest, "i")
        if self.prop(sname, "underline"):
            dest = ET.SubElement(dest, "u")
        if self.prop(sname, "strike"):
            dest = ET.SubElement(dest, "s")
        pos = self.prop(sname, "position") or ""
        if pos.startswith(("super", "33%")) or re.match(r"^\d+%", pos) and not pos.startswith("-"):
            if pos and not pos.startswith("0%"):
                dest = ET.SubElement(dest, "sup")
        elif pos.startswith(("sub", "-")):
            dest = ET.SubElement(dest, "sub")
        if self.prop(sname, "mono"):
            dest = ET.SubElement(dest, "code")
        if dest is target:
            dest = ET.SubElement(target, "span")
        return dest

    def frame(self, frame, parent):
        img = frame.find(q("draw", "image"))
        if img is None:
            box = frame.find(q("draw", "text-box"))
            if box is not None:
                self.blocks(box, parent)
            return
        href = img.get(q("xlink", "href"), "")
        if not href or href.startswith(("http:", "https:")):
            return
        bhref = self.images.get(href)
        if bhref is None:
            try:
                data = limits.zip_read(self.z, href.lstrip("./"))
            except KeyError:
                return
            if sniff_image(data) is None:
                return
            bhref = self.book.add("images/" + re.sub(r"[^\w.-]", "_", posixpath.basename(href)), data).href
            self.images[href] = bhref
        title = frame.find(q("svg", "title"))
        alt = (title.text or "") if title is not None else frame.get(q("draw", "name"), "")
        ET.SubElement(parent, "img", {"src": bhref, "alt": alt})

    def note(self, note, target):
        nid = _safe(note.get(q("text", "id")) or f"note{len(self.notes) + 1}")
        cit = note.find(q("text", "note-citation"))
        label = (cit.text or str(len(self.notes) + 1)) if cit is not None else str(len(self.notes) + 1)
        a = ET.SubElement(target, "a", {"href": f"#{nid}", "id": f"{nid}ref", "class": "noteref", "epub:type": "noteref"})
        ET.SubElement(a, "sup").text = label
        aside = ET.Element("aside", {"id": nid, "epub:type": "footnote"})
        body = note.find(q("text", "note-body"))
        if body is not None:
            self.blocks(body, aside)
        first = aside.find("p")
        back = ET.Element("a", {"href": f"#{nid}ref"})
        back.text = f"{label}. "
        if first is not None:
            back.tail = first.text
            first.text = None
            first.insert(0, back)
        self.notes.append(aside)

    def finish(self):
        hrefs = [f"text/part{n:04d}.xhtml" for n in range(1, len(self.docs) + 1)]
        docs = list(zip(hrefs, self.docs))
        if self.notes:
            root, body = markup.new_document("Notes", self.book.metadata.language)
            ET.SubElement(body, "h2").text = "Notes"
            for n in self.notes:
                body.append(n)
            docs.append(("text/notes.xhtml", (root, body)))
        targets = {}
        for href, (root, _b) in docs:
            for el in root.iter():
                if el.get("id"):
                    targets[el.get("id")] = href
        for href, (root, _b) in docs:
            for el in root.iter():
                if el.tag == "img" and el.get("src"):
                    el.set("src", relative(href, el.get("src")))
                if el.tag == "a" and el.get("href", "").startswith("#"):
                    frag = el.get("href")[1:]
                    if frag in targets:
                        el.set("href", relative(href, f"{targets[frag]}#{frag}"))
            self.book.add_document(href, root)


def _safe(name: str) -> str:
    s = re.sub(r"[^\w.-]", "_", name)
    return s if s and (s[0].isalpha() or s[0] == "_") else "n_" + s
