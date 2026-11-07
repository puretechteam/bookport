"""OpenDocument text (ODT, ODF 1.2/1.3): read and write.

Reading: headings by outline level, paragraphs and spans with their automatic and named styles
(bold, italic, underline, strike-through, super/subscript, monospace), lists, tables, images in
frames, formulas (embedded MathML objects), links, bookmarks, footnotes and endnotes, and the
document's meta.xml.

Writing: a package LibreOffice and Word open (mimetype first and stored, manifest, content, styles,
meta), with real heading, list, table, code, quotation and footnote styles, pictures under
Pictures/, internal and external links, footnotes, and equations as formula objects (the MathML
in "Object N/content.xml", as LibreOffice stores them).
"""

from __future__ import annotations

import datetime
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import limits, markup, mathml
from ..book import Book, Metadata, relative, sniff_image
from ..errors import BookportError, DRMError
from ..textwriter import Table, TextWriter

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
        self.last_pre: ET.Element | None = None

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
                prev = parent[-2] if len(parent) > 1 else None
                if el2.tag == "pre" and prev is not None and prev.tag == "pre" and prev is self.last_pre \
                        and not (prev.tail or "").strip():
                    # one code block is a run of preformatted paragraphs, a line each
                    parent.remove(el2)
                    markup.append_text(prev, "\n" + (el2.text or ""))
                    prev.extend(list(el2))
                    el2 = prev
                if el2.tag == "pre":
                    self.last_pre = el2
                if el2.tag == "p" and not len(el2) and not (el2.text or "").strip():
                    el2.text = " "
                # a formula alone in its paragraph is a display equation
                maths = [m for m in el2 if m.tag == "math"]
                if len(maths) == 1 and len(el2) == 1 and not (el2.text or "").strip() and not (maths[0].tail or "").strip():
                    maths[0].set("display", "block")
                self.size += len(markup.text_of(el2))
            elif tag == q("text", "list"):
                sname = c.get(q("text", "style-name")) or list_style
                kind = self.list_styles.get(sname, {}).get(depth, "ul")
                lst = ET.SubElement(parent, kind)
                first = c.find(q("text", "list-item"))
                start = first.get(q("text", "start-value"), "") if first is not None else ""
                if kind == "ol" and start.isdigit() and start != "1":
                    lst.set("start", start)
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
                head = {id(r) for h in c.iter(q("table", "table-header-rows")) for r in h.iter(q("table", "table-row"))}
                for row in c.iter(q("table", "table-row")):
                    tr = ET.SubElement(table, "tr")
                    for cell in row:
                        if cell.tag == q("table", "table-cell"):
                            td = ET.SubElement(tr, "th" if id(row) in head else "td")
                            for attr, name in (("number-columns-spanned", "colspan"), ("number-rows-spanned", "rowspan")):
                                span = cell.get(q("table", attr))
                                if span and span.isdigit() and int(span) > 1:
                                    td.set(name, span)
                            self.blocks(cell, td)
                            # a cell with one paragraph needn't keep its <p>
                            if len(td) == 1 and td[0].tag == "p" and not (td.text or "").strip():
                                p = td[0]
                                td.remove(p)
                                td.text = p.text
                                td.extend(list(p))
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
        obj = frame.find(q("draw", "object"))
        if obj is not None:  # a formula (its draw:image, if any, is only a picture of it)
            self.formula(obj, parent)
            return
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

    def formula(self, obj, parent):
        href = obj.get(q("xlink", "href"), "")
        name = href.removeprefix("./").strip("/")
        if not name or name.startswith(("/", "..")) or "://" in name:
            return
        try:
            root = markup.parse_xml(limits.zip_read(self.z, f"{name}/content.xml"), f"{name}/content.xml")
        except (KeyError, ET.ParseError):
            return
        if root.tag.rsplit("}", 1)[-1] != "math":
            return
        for el in root.iter():
            if isinstance(el.tag, str):
                el.tag = el.tag.rsplit("}", 1)[-1]
            el.attrib = {k.rsplit("}", 1)[-1]: v for k, v in el.attrib.items()}
        root.set("display", "inline")
        parent.append(root)

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


# --- writing -------------------------------------------------------------------------------------

MIMETYPE = "application/vnd.oasis.opendocument.text"
_ROOT_NS = " ".join(f'xmlns:{p}="{u}"' for p, u in NS.items() if p != "manifest") + \
    ' xmlns:math="http://www.w3.org/1998/Math/MathML" xmlns:loext="urn:org:documentfoundation:names:experimental:office:' \
    'xmlns:loext:1.0"'
_BLOCK_START = ("<text:p", "<text:h", "<text:list", "<table:table", "<text:section")
_PX_CM = 2.54 / 96
_MAX_W_CM, _MAX_H_CM = 17.0, 24.0


def _x(s: str) -> str:
    return markup._esc_text(markup.clean_text(s))


def _xa(s: str) -> str:
    return markup._esc_attr(markup.clean_text(s))


def _spaces(text: str) -> str:
    """Text where every space counts (code): runs of spaces as <text:s>, tabs as <text:tab>."""
    out = []
    for k, part in enumerate(re.split(r"( {2,}|^ | $|\t)", text)):
        if not part:
            continue
        if k % 2 == 0:
            out.append(_x(part))
        elif part == "\t":
            out.append("<text:tab/>")
        else:
            out.append("<text:s/>" if len(part) == 1 else f'<text:s text:c="{len(part)}"/>')
    return "".join(out)


# LibreOffice Math (checked with 25.x) drops an accent marked accent="true" on <mover>, and shows a
# combining accent over nothing; a spacing accent as an over-script looks right
_SPACING_ACCENTS = {chr(0x302): "ˆ", chr(0x303): "˜", chr(0x307): "˙", chr(0x308): "¨", chr(0x30C): "ˇ", chr(0x306): "˘",
                    chr(0x301): "´", chr(0x300): "`", chr(0x30A): "˚", chr(0x20D7): "→", chr(0x20DB): "⋯", "‾": "¯"}


def _for_office(el: ET.Element) -> ET.Element:
    """A copy of an equation for a formula object: spacing accents, and no invisible
    function-application operators (LibreOffice sets function names itself)."""
    import copy
    math = copy.deepcopy(el)
    math.tag = "math"
    math.tail = None  # the words after it belong to the paragraph
    for parent in list(math.iter()):
        for c in list(parent):
            if c.tag == "mo" and c.text == mathml.APPLY:
                parent.remove(c)
            elif c.tag == "mo" and c.text in _SPACING_ACCENTS and parent.tag == "mover":
                c.text = _SPACING_ACCENTS[c.text]
                c.attrib.pop("accent", None)
    return math


class _OdtWriter(TextWriter):
    def __init__(self, book: Book):
        super().__init__(book)
        self.pictures: dict[str, tuple[str, bytes, str]] = {}  # book href -> (name in the package, data, media type)
        self.objects: list[tuple[str, str]] = []  # (object folder, MathML document)
        self.note_n = 0
        self.table_n = 0
        self.frame_n = 0

    def join_blocks(self, parts: list[str]) -> str:
        return "".join(p for p in parts if p.strip())

    def join_document(self, parts: list[str]) -> str:
        return "".join(parts)

    def escape(self, s: str) -> str:
        return _x(s)

    def bookmark(self, key: str | None) -> str:
        return f'<text:bookmark text:name="{_xa(key)}"/>' if key else ""

    def para(self, inner: str, style: str = "Text_20_body") -> str:
        """``inner`` as it is when it's already block markup, else in a paragraph of ``style``."""
        inner = inner.strip()
        if inner.startswith(_BLOCK_START):
            return inner
        return f'<text:p text:style-name="{style}">{inner}</text:p>'

    # blocks
    def paragraph(self, text: str, anchor: str | None = None) -> str:
        return f'<text:p text:style-name="Text_20_body">{self.bookmark(anchor)}{text}</text:p>'

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        return (f'<text:h text:style-name="Heading_20_{level}" text:outline-level="{level}">'
                f"{self.bookmark(anchor)}{text}</text:h>")

    def rule(self) -> str:
        return '<text:p text:style-name="Horizontal_20_Line"/>'

    def code_block(self, code: str, lang: str) -> str:
        return "".join(f'<text:p text:style-name="Preformatted_20_Text">{_spaces(line)}</text:p>'
                       for line in code.split("\n"))

    def raw_block(self, text: str, fmt: str) -> str:
        return self.code_block(text, "")

    def quote(self, inner: str) -> str:
        return re.sub(r'<text:p text:style-name="Text_20_body"', '<text:p text:style-name="Quotations"', inner)

    def list_item(self, li: ET.Element) -> str:
        return self.blocks(li) if self.has_blocks(li) else self.para(self.inlines(li), "List_20_Contents")

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        out = [f'<text:list text:style-name="{"L2" if ordered else "L1"}">']
        for k, item in enumerate(items):
            first = f' text:start-value="{start}"' if ordered and k == 0 and start != 1 else ""
            out.append(f"<text:list-item{first}>{self.para(item, 'List_20_Contents') if item.strip() else '<text:p/>'}"
                       "</text:list-item>")
        out.append("</text:list>")
        return "".join(out)

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        out = []
        for term, defs in pairs:
            out.append(self.para(term, "Definition_20_Term"))
            out += [self.para(d, "Definition_20_Description") for d in defs]
        return "".join(out)

    def table(self, table: Table) -> str:
        self.table_n += 1
        width = max(1, table.width)
        out = [(f'<table:table table:name="Table{self.table_n}" table:style-name="Table">'
                f'<table:table-column table:number-columns-repeated="{width}"/>')]
        taken: set[tuple[int, int]] = set()  # cells a span reaches into
        for r, row in enumerate(table.rows):
            if r == 0 and table.header_rows:
                out.append("<table:table-header-rows>")
            out.append("<table:table-row>")
            col = 0
            cells = iter(row)
            while col < width:
                if (r, col) in taken:
                    out.append("<table:covered-table-cell/>")
                    col += 1
                    continue
                cell = next(cells, None)
                if cell is None:
                    out.append('<table:table-cell office:value-type="string"><text:p/></table:table-cell>')
                    col += 1
                    continue
                span = min(cell.colspan, width - col)
                spans = (f' table:number-columns-spanned="{span}"' if span > 1 else "") + \
                    (f' table:number-rows-spanned="{cell.rowspan}"' if cell.rowspan > 1 else "")
                style = "Table_20_Heading" if cell.header or r < table.header_rows else "Table_20_Contents"
                body = self.para(cell.text, style) if cell.text.strip() else f'<text:p text:style-name="{style}"/>'
                out.append(f'<table:table-cell office:value-type="string"{spans}>{body}</table:table-cell>')
                for dr in range(cell.rowspan):
                    for dc in range(span):
                        if dr or dc:
                            taken.add((r + dr, col + dc))
                for _ in range(span - 1):
                    out.append("<table:covered-table-cell/>")
                col += span
            out.append("</table:table-row>")
            if r + 1 == table.header_rows:
                out.append("</table:table-header-rows>")
        out.append("</table:table>")
        if table.caption:
            out.append(f'<text:p text:style-name="Table_20_Caption">{table.caption}</text:p>')
        return "".join(out)

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        frame = self.image(src, alt)
        out = f'<text:p text:style-name="Figure">{self.bookmark(anchor)}{frame}</text:p>'
        if caption:
            out += f'<text:p text:style-name="Caption">{caption}</text:p>'
        return out

    def math_block(self, el: ET.Element) -> str:
        return f'<text:p text:style-name="Formula">{self.formula(el)}</text:p>'

    def math_inline(self, el: ET.Element) -> str:
        return self.formula(el)

    def formula(self, el: ET.Element) -> str:
        name = f"Object {len(self.objects) + 1}"
        doc = markup.to_string(_for_office(el)).replace("<math", f'<math xmlns="{markup.MATHML_NS}"', 1)
        self.objects.append((name, '<?xml version="1.0" encoding="UTF-8"?>\n' + doc))
        desc = " ".join(mathml.to_tex(el).split())
        return (f'<draw:frame draw:style-name="fr_math" draw:name="{name}" text:anchor-type="as-char" draw:z-index="0">'
                f'<draw:object xlink:href="./{name}" xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/>'
                f"<svg:desc>{_x(desc)}</svg:desc></draw:frame>")

    # inline
    def footnote(self, body: str, note_id: str) -> str:
        self.note_n += 1
        body = re.sub(r'<text:p text:style-name="[^"]*"', '<text:p text:style-name="Footnote"', self.para(body, "Footnote"))
        return (f'<text:note text:id="ftn{self.note_n}" text:note-class="footnote"><text:note-citation>{self.note_n}'
                f"</text:note-citation><text:note-body>{body}</text:note-body></text:note>")

    def line_break(self) -> str:
        return "<text:line-break/>"

    def inline_anchor(self, key: str) -> str:
        return self.bookmark(key)

    def _span(self, style: str, s: str) -> str:
        return f'<text:span text:style-name="{style}">{s}</text:span>'

    def emph(self, s: str) -> str:
        return self._span("Emphasis", s)

    def strong(self, s: str) -> str:
        return self._span("Strong_20_Emphasis", s)

    def strike(self, s: str) -> str:
        return self._span("Strikeout", s)

    def underline(self, s: str) -> str:
        return self._span("Underline", s)

    def sup(self, s: str) -> str:
        return self._span("Superscript", s)

    def sub(self, s: str) -> str:
        return self._span("Subscript", s)

    def smallcaps(self, s: str) -> str:
        return self._span("Small_20_Caps", s)

    def code(self, s: str) -> str:
        return self._span("Source_20_Text", _spaces(s))

    def raw_inline(self, text: str, fmt: str) -> str:
        return self.code(text)

    def link(self, target: str, text: str, external: bool) -> str:
        href = target if external else "#" + target
        return f'<text:a xlink:type="simple" xlink:href="{_xa(href)}">{text or _x(target)}</text:a>'

    def image(self, src: str, alt: str) -> str:
        target = next((t for t, n in self.used_images.items() if n == src), None)
        got = self.picture(target) if target else None
        if got is None:
            return _x(alt)
        name, w, h = got
        self.frame_n += 1
        return (f'<draw:frame draw:style-name="fr_img" draw:name="Image{self.frame_n}" text:anchor-type="as-char" '
                f'svg:width="{w:.3f}cm" svg:height="{h:.3f}cm" draw:z-index="0"><draw:image xlink:href="{_xa(name)}" '
                f'xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/><svg:title>{_x(alt)}</svg:title>'
                "</draw:frame>")

    def picture(self, href: str) -> tuple[str, float, float] | None:
        res = self.book.resources.get(href)
        if res is None or not res.is_image:
            return None
        if href not in self.pictures:
            data, mt = res.data, res.media_type
            if mt not in ("image/png", "image/jpeg", "image/gif", "image/svg+xml"):
                from ..images import to_png
                png = to_png(data)
                if png is None:
                    return None
                data, mt = png, "image/png"
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/svg+xml": ".svg"}[mt]
            base = re.sub(r"[^\w.-]", "_", posixpath.splitext(posixpath.basename(href))[0]) or "image"
            name = f"Pictures/{len(self.pictures) + 1:03d}_{base}{ext}"
            self.pictures[href] = (name, data, mt)
        name, data, _mt = self.pictures[href]
        from .epub import image_size
        w_px, h_px = image_size(data) or (300, 200)
        w, h = max(w_px, 1) * _PX_CM, max(h_px, 1) * _PX_CM
        scale = min(1.0, _MAX_W_CM / w, _MAX_H_CM / h)
        return name, w * scale, h * scale


def write(book: Book, path, **_) -> None:
    w = _OdtWriter(book)
    body = w.write_book()
    md = book.metadata
    content = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-content {_ROOT_NS} office:version="1.3">'
               f"{_AUTOMATIC}<office:body><office:text>{body or '<text:p/>'}</office:text></office:body>"
               "</office:document-content>")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    meta = [f"<meta:generator>bookport</meta:generator><dc:title>{_x(md.title)}</dc:title>"]
    if md.authors:
        meta.append(f"<meta:initial-creator>{_x('; '.join(md.authors))}</meta:initial-creator>"
                    f"<dc:creator>{_x('; '.join(md.authors))}</dc:creator>")
    if md.language:
        meta.append(f"<dc:language>{_x(md.language)}</dc:language>")
    if md.description:
        meta.append(f"<dc:description>{_x(re.sub(r'<[^>]+>', '', md.description))}</dc:description>")
    if md.subjects:
        meta.append(f"<dc:subject>{_x(md.subjects[0])}</dc:subject>")
        meta += [f"<meta:keyword>{_x(s)}</meta:keyword>" for s in md.subjects]
    meta.append(f"<meta:creation-date>{now}</meta:creation-date><dc:date>{now}</dc:date>")
    meta_xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-meta {_ROOT_NS} office:version="1.3">'
                f"<office:meta>{''.join(meta)}</office:meta></office:document-meta>")
    styles = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-styles {_ROOT_NS} office:version="1.3">'
              f"{_STYLES.replace('%LANG%', _xa((md.language or 'en').split('-')[0]))}</office:document-styles>")
    entries = [("/", MIMETYPE), ("content.xml", "text/xml"), ("styles.xml", "text/xml"), ("meta.xml", "text/xml")]
    entries += [(name, mt) for name, _data, mt in w.pictures.values()]
    for name, _doc in w.objects:
        entries += [(f"{name}/", "application/vnd.oasis.opendocument.formula"), (f"{name}/content.xml", "text/xml")]
    manifest = ('<?xml version="1.0" encoding="UTF-8"?>\n<manifest:manifest xmlns:manifest="'
                + NS["manifest"] + '" manifest:version="1.3">'
                + "".join(f'<manifest:file-entry manifest:full-path="{_xa(p)}"'
                          + (' manifest:version="1.3"' if p == "/" else "") + f' manifest:media-type="{mt}"/>'
                          for p, mt in entries)
                + "</manifest:manifest>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE, compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/manifest.xml", manifest)
        z.writestr("content.xml", content)
        z.writestr("styles.xml", styles)
        z.writestr("meta.xml", meta_xml)
        for name, data, _mt in w.pictures.values():
            z.writestr(name, data)
        for name, doc in w.objects:
            z.writestr(f"{name}/content.xml", doc)
    Path(path).write_bytes(buf.getvalue())


def _list_levels(bullet: bool) -> str:
    out = []
    for n in range(1, 11):
        indent = f'<style:list-level-properties text:list-level-position-and-space-mode="label-alignment">' \
                 f'<style:list-level-label-alignment text:label-followed-by="listtab" fo:text-indent="-0.635cm" ' \
                 f'fo:margin-left="{0.635 * n + 0.635:.3f}cm"/></style:list-level-properties>'
        if bullet:
            char = "•◦▪"[(n - 1) % 3]
            out.append(f'<text:list-level-style-bullet text:level="{n}" text:bullet-char="{char}">{indent}'
                       "</text:list-level-style-bullet>")
        else:
            out.append(f'<text:list-level-style-number text:level="{n}" style:num-suffix="." style:num-format="1">'
                       f"{indent}</text:list-level-style-number>")
    return "".join(out)


_AUTOMATIC = (
    "<office:automatic-styles>"
    '<style:style style:name="fr_math" style:family="graphic" style:parent-style-name="Formula">'
    '<style:graphic-properties style:vertical-pos="middle" style:vertical-rel="text" draw:ole-draw-aspect="1"/>'
    "</style:style>"
    '<style:style style:name="fr_img" style:family="graphic" style:parent-style-name="Graphics">'
    '<style:graphic-properties style:vertical-pos="top" style:vertical-rel="baseline"/></style:style>'
    f'<text:list-style style:name="L1">{_list_levels(True)}</text:list-style>'
    f'<text:list-style style:name="L2">{_list_levels(False)}</text:list-style>'
    "</office:automatic-styles>")


def _para_style(name: str, display: str, parent: str = "Standard", para: str = "", text: str = "",
                extra: str = "") -> str:
    return (f'<style:style style:name="{name}" style:display-name="{display}" style:family="paragraph" '
            f'style:parent-style-name="{parent}"{extra}>'
            + (f"<style:paragraph-properties {para}/>" if para else "")
            + (f"<style:text-properties {text}/>" if text else "") + "</style:style>")


def _text_style(name: str, display: str, props: str) -> str:
    return (f'<style:style style:name="{name}" style:display-name="{display}" style:family="text">'
            f"<style:text-properties {props}/></style:style>")


_HEADING_SIZES = {1: "130%", 2: "115%", 3: "101%", 4: "95%", 5: "85%", 6: "85%"}
_MONO = 'style:font-name="Liberation Mono" fo:font-family="\'Liberation Mono\'" style:font-family-generic="modern" ' \
        'style:font-pitch="fixed"'
_STYLES = (
    '<office:font-face-decls><style:font-face style:name="Liberation Serif" svg:font-family="\'Liberation Serif\'" '
    'style:font-family-generic="roman" style:font-pitch="variable"/><style:font-face style:name="Liberation Sans" '
    'svg:font-family="\'Liberation Sans\'" style:font-family-generic="swiss" style:font-pitch="variable"/>'
    '<style:font-face style:name="Liberation Mono" svg:font-family="\'Liberation Mono\'" '
    'style:font-family-generic="modern" style:font-pitch="fixed"/></office:font-face-decls>'
    "<office:styles>"
    '<style:default-style style:family="paragraph"><style:paragraph-properties style:writing-mode="page"/>'
    '<style:text-properties style:font-name="Liberation Serif" fo:font-size="12pt" fo:language="%LANG%"/>'
    "</style:default-style>"
    '<style:style style:name="Standard" style:family="paragraph" style:class="text"/>'
    + _para_style("Text_20_body", "Text body", para='fo:margin-top="0cm" fo:margin-bottom="0.247cm" fo:line-height="115%"')
    + _para_style("Heading", "Heading", "Standard", 'fo:margin-top="0.423cm" fo:margin-bottom="0.212cm" fo:keep-with-next="always"',
                  'style:font-name="Liberation Sans" fo:font-size="14pt"', ' style:next-style-name="Text_20_body"')
    + "".join(_para_style(f"Heading_20_{n}", f"Heading {n}", "Heading", "",
                          f'fo:font-size="{_HEADING_SIZES[n]}" fo:font-weight="bold"'
                          + (' fo:font-style="italic"' if n >= 5 else ""),
                          f' style:default-outline-level="{n}" style:next-style-name="Text_20_body"') for n in range(1, 7))
    + _para_style("Title", "Title", "Heading", 'fo:text-align="center"', 'fo:font-size="28pt" fo:font-weight="bold"')
    + _para_style("Quotations", "Quotations", "Standard", 'fo:margin-left="1cm" fo:margin-right="1cm" fo:margin-top="0cm" '
                  'fo:margin-bottom="0.283cm"')
    + _para_style("Preformatted_20_Text", "Preformatted Text", "Standard", 'fo:margin-top="0cm" fo:margin-bottom="0cm"',
                  _MONO + ' fo:font-size="10pt"')
    + _para_style("List_20_Contents", "List Contents", "Text_20_body")
    + _para_style("Definition_20_Term", "Definition Term", "Text_20_body", 'fo:margin-bottom="0cm"', 'fo:font-weight="bold"')
    + _para_style("Definition_20_Description", "Definition Description", "Text_20_body", 'fo:margin-left="1cm"')
    + _para_style("Table_20_Contents", "Table Contents", "Standard")
    + _para_style("Table_20_Heading", "Table Heading", "Table_20_Contents", 'fo:text-align="center"', 'fo:font-weight="bold"')
    + _para_style("Caption", "Caption", "Standard", 'fo:margin-top="0.212cm" fo:margin-bottom="0.212cm"',
                  'fo:font-size="10pt" fo:font-style="italic"')
    + _para_style("Table_20_Caption", "Table Caption", "Caption")
    + _para_style("Figure", "Figure", "Standard", 'fo:text-align="center"')
    + _para_style("Formula", "Formula", "Standard", 'fo:text-align="center"')
    + _para_style("Footnote", "Footnote", "Standard", "", 'fo:font-size="10pt"')
    + _para_style("Horizontal_20_Line", "Horizontal Line", "Standard", 'fo:margin-bottom="0.5cm" fo:border-bottom="0.06pt solid '
                  '#808080" fo:padding="0cm"', 'fo:font-size="6pt"')
    + _text_style("Emphasis", "Emphasis", 'fo:font-style="italic"')
    + _text_style("Strong_20_Emphasis", "Strong Emphasis", 'fo:font-weight="bold"')
    + _text_style("Source_20_Text", "Source Text", _MONO)
    + _text_style("Strikeout", "Strikeout", 'style:text-line-through-style="solid" style:text-line-through-type="single"')
    + _text_style("Underline", "Underline", 'style:text-underline-style="solid" style:text-underline-width="auto" '
                  'style:text-underline-color="font-color"')
    + _text_style("Superscript", "Superscript", 'style:text-position="super 58%"')
    + _text_style("Subscript", "Subscript", 'style:text-position="sub 58%"')
    + _text_style("Small_20_Caps", "Small Caps", 'fo:font-variant="small-caps"')
    + _text_style("Internet_20_link", "Internet link", 'fo:color="#000080" style:text-underline-style="solid" '
                  'style:text-underline-width="auto" style:text-underline-color="font-color"')
    + '<style:style style:name="Graphics" style:family="graphic"><style:graphic-properties text:anchor-type="as-char" '
      'svg:y="0cm" style:vertical-pos="top" style:vertical-rel="baseline"/></style:style>'
      '<style:style style:name="Formula" style:family="graphic"><style:graphic-properties text:anchor-type="as-char" '
      'svg:y="0cm" style:vertical-pos="middle" style:vertical-rel="text"/></style:style>'
      '<style:style style:name="Table" style:family="table"><style:table-properties style:width="17cm" '
      'table:align="margins"/></style:style>'
      '<text:outline-style style:name="Outline">'
    + "".join(f'<text:outline-level-style text:level="{n}" style:num-format=""/>' for n in range(1, 11))
    + "</text:outline-style>"
      '<text:notes-configuration text:note-class="footnote" style:num-format="1" text:start-value="0" '
      'text:footnotes-position="page" text:start-numbering-at="document"/>'
      "</office:styles>"
      '<office:automatic-styles><style:page-layout style:name="pm1"><style:page-layout-properties fo:page-width="21cm" '
      'fo:page-height="29.7cm" style:print-orientation="portrait" fo:margin-top="2cm" fo:margin-bottom="2cm" '
      'fo:margin-left="2cm" fo:margin-right="2cm"/></style:page-layout></office:automatic-styles>'
      '<office:master-styles><style:master-page style:name="Standard" style:page-layout-name="pm1"/>'
      "</office:master-styles>")
