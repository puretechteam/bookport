"""Word documents (DOCX, ECMA-376 / Office Open XML): read and write.

Reading turns paragraphs, runs, styles (headings from style names and outline levels), numbered
and bulleted lists, tables, images, hyperlinks, bookmarks, footnotes, endnotes and equations
(Office Math, as MathML) into XHTML.
Writing makes a document Word, LibreOffice and Google Docs open, with real heading styles (so
the navigation pane works), lists, tables, images, internal and external links, equations as
Office Math (editable in Word) and the book's properties.
"""

from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import limits, markup, mathml
from ..book import Book, Metadata, is_external, relative, sniff_image
from ..errors import BookportError

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
V = "urn:schemas-microsoft-com:vml"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
OMML = mathml.M
ET.register_namespace("m", OMML)


def w(tag):
    return f"{{{W}}}{tag}"


def _wval(el, tag, attr="val"):
    if el is None:
        return None
    c = el.find(w(tag))
    if c is None:
        return None
    return c.get(w(attr), "")


def _on(el, tag) -> bool | None:
    """A w:b / w:i style toggle: None when absent, else its value."""
    if el is None:
        return None
    c = el.find(w(tag))
    if c is None:
        return None
    v = c.get(w("val"), "true").lower()
    return v not in ("0", "false", "off", "none")


# --- reading -----------------------------------------------------------------------------------

class _Styles:
    def __init__(self, root):
        self.styles = {}
        self.default_para = None
        if root is None:
            return
        for s in root.findall(w("style")):
            sid = s.get(w("styleId"), "")
            name = (_wval(s, "name") or sid).lower()
            self.styles[sid] = {
                "name": name, "type": s.get(w("type"), ""), "based": _wval(s, "basedOn"),
                "bold": _on(s.find(w("rPr")), "b"), "italic": _on(s.find(w("rPr")), "i"),
                "outline": _wval(s.find(w("pPr")), "outlineLvl"), "jc": _wval(s.find(w("pPr")), "jc"),
                "mono": self._mono(s.find(w("rPr"))),
            }
            if s.get(w("type")) == "paragraph" and s.get(w("default")) in ("1", "true"):
                self.default_para = sid

    @staticmethod
    def _mono(rpr):
        if rpr is None:
            return None
        f = rpr.find(w("rFonts"))
        if f is None:
            return None
        name = (f.get(w("ascii")) or f.get(w("hAnsi")) or "").lower()
        return any(m in name for m in ("courier", "consolas", "mono", "menlo", "lucida console")) or None

    def chain(self, sid):
        seen = 0
        while sid and sid in self.styles and seen < 20:
            yield self.styles[sid]
            sid = self.styles[sid]["based"]
            seen += 1

    def prop(self, sid, key):
        for s in self.chain(sid):
            if s[key] is not None:
                return s[key]
        return None

    def heading(self, sid) -> int:
        for s in self.chain(sid):
            name = s["name"]
            m = re.match(r"^(heading|überschrift|titre|titolo|encabezado|kop)\s*(\d)$", name)
            if m:
                return min(int(m.group(2)), 6)
            if name in ("title",):
                return 1
            lvl = markup.whole(s["outline"])
            if lvl is not None and lvl < 9:
                return min(lvl + 1, 6)
        return 0

    def kind(self, sid) -> str:
        for s in self.chain(sid):
            name = s["name"]
            if name in ("quote", "intense quote", "block text"):
                return "quote"
            if name in ("subtitle",):
                return "subtitle"
            if "code" in name or name in ("html preformatted", "plain text"):
                return "code"
        return ""


class _Numbering:
    def __init__(self, root):
        self.abstract = {}
        self.nums = {}
        self.starts: dict[tuple[str, int], str] = {}  # (numId, level) -> a list's own start number
        if root is None:
            return
        for a in root.findall(w("abstractNum")):
            levels = {}
            for lvl in a.findall(w("lvl")):
                levels[int(lvl.get(w("ilvl"), "0"))] = (_wval(lvl, "numFmt") or "bullet", _wval(lvl, "start") or "1")
            self.abstract[a.get(w("abstractNumId"))] = levels
        for n in root.findall(w("num")):
            self.nums[n.get(w("numId"))] = _wval(n, "abstractNumId")
            for o in n.findall(w("lvlOverride")):
                start = _wval(o, "startOverride")
                ilvl = markup.whole(o.get(w("ilvl"), "0"))
                if start and ilvl is not None:
                    self.starts[(n.get(w("numId")), ilvl)] = start

    def kind(self, num_id, level) -> tuple[str, int]:
        levels = self.abstract.get(self.nums.get(num_id), {})
        fmt, start = levels.get(level, ("bullet", "1"))
        start = self.starts.get((num_id, level), start)
        tag = "ul" if fmt in ("bullet", "none", "") else "ol"
        try:
            return tag, int(start)
        except ValueError:
            return tag, 1


WP_NS = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"


def _shape_position(shape) -> tuple[float, float] | None:
    """(top, left) of a floating shape in points, from its DrawingML anchor or its VML style."""
    v = shape.find(f".//{WP_NS}positionV/{WP_NS}posOffset")
    h = shape.find(f".//{WP_NS}positionH/{WP_NS}posOffset")
    if v is not None and h is not None:
        try:
            return int(v.text or 0) / 12700, int(h.text or 0) / 12700
        except ValueError:
            return None
    for el in shape.iter():
        style = el.get("style", "") if el.tag.startswith(f"{{{V}}}") else ""
        top = re.search(r"margin-top:\s*(-?[\d.]+)pt", style)
        left = re.search(r"margin-left:\s*(-?[\d.]+)pt", style)
        if top and left:
            return float(top.group(1)), float(left.group(1))
    return None


def read(path) -> Book:
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BookportError(f"Not a Word document: {e}") from None
    with z:
        return _read(z)


def _xml(z, name):
    try:
        data = limits.zip_read(z, name)
    except KeyError:
        return None
    try:
        return markup.parse_xml(data, f"{name} in this Word document")
    except ET.ParseError as e:
        raise BookportError(f"Damaged Word document: {name} ({e})") from None


def _rels(z, part):
    d, base = posixpath.split(part)
    root = _xml(z, posixpath.join(d, "_rels", base + ".rels"))
    out = {}
    if root is None:
        return out
    for r in root:
        target = r.get("Target", "")
        if r.get("TargetMode") != "External":
            target = posixpath.normpath(posixpath.join(d, target)) if not target.startswith("/") else target.lstrip("/")
        out[r.get("Id")] = (r.get("Type", "").rsplit("/", 1)[-1], target, r.get("TargetMode") == "External")
    return out


def _main_part(z) -> str:
    root = _xml(z, "_rels/.rels")
    if root is not None:
        for r in root:
            if r.get("Type", "").endswith("/officeDocument"):
                return r.get("Target", "word/document.xml").lstrip("/")
    return "word/document.xml"


def _read(z) -> Book:
    if "EncryptedPackage" in z.namelist():
        from ..errors import DRMError
        raise DRMError("Word", "the document is password protected")
    part = _main_part(z)
    doc = _xml(z, part)
    if doc is None:
        raise BookportError("Not a Word document: no word/document.xml")
    rels = _rels(z, part)
    by_type = {}
    for kind, target, _ext in rels.values():
        by_type.setdefault(kind, target)
    styles = _Styles(_xml(z, by_type.get("styles", "word/styles.xml")))
    numbering = _Numbering(_xml(z, by_type.get("numbering", "word/numbering.xml")))
    book = Book(_core(z))
    book.source_format = "docx"
    conv = _Converter(book, z, rels, styles, numbering)
    body = doc.find(w("body"))
    if body is None:
        raise BookportError("Damaged Word document: no body")
    conv.body(body)
    for kind in ("footnotes", "endnotes"):
        target = by_type.get(kind)
        if target:
            notes = _xml(z, target)
            if notes is not None:
                conv.notes(kind, notes, _rels(z, target))
    conv.finish()
    if not book.metadata.title:
        first = next((e for href, _ in book.documents() for e in book.tree(href).iter() if e.tag in ("h1", "h2")), None)
        if first is not None:
            book.metadata.title = " ".join(markup.text_of(first).split())[:200]
    book.ensure_toc()
    return book


def _core(z) -> Metadata:
    md = Metadata()
    root = _xml(z, "docProps/core.xml")
    if root is None:
        return md

    def t(ns, tag):
        el = root.find(f"{{{ns}}}{tag}")
        return " ".join((el.text or "").split()) if el is not None and el.text else ""

    md.title = t(DC, "title")
    creator = t(DC, "creator")
    if creator:
        md.authors = [a.strip() for a in re.split(r";|\band\b", creator) if a.strip()]
    md.description = t(DC, "description")
    subject = t(DC, "subject")
    if subject:
        md.subjects.append(subject)
    kw = t(CP, "keywords")
    if kw:
        md.subjects += [k.strip() for k in re.split(r"[,;]", kw) if k.strip()]
    md.subjects = list(dict.fromkeys(md.subjects))  # the subject is usually among the keywords too
    md.language = t(DC, "language")
    md.date = t(DCTERMS, "created")[:10]
    ident = t(DC, "identifier")
    if ident:
        md.identifiers["id"] = ident
    custom = _xml(z, "docProps/custom.xml")  # what core.xml has no place for: publisher, ISBN, series
    if custom is not None:
        props = {}
        for prop in custom:
            value = next((" ".join((v.text or "").split()) for v in prop), "")
            if prop.get("name") and value:
                props[prop.get("name")] = value
        md.publisher = props.get("Publisher", "")
        if props.get("ISBN"):
            md.identifiers["isbn"] = props["ISBN"]
        md.series = props.get("Series", "")
        try:
            md.series_index = float(props["Series index"]) if props.get("Series index") else None
        except ValueError:
            pass
    return md


CUSTOM_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"  # the property set Office uses for custom properties


def _custom_props(md) -> str:
    """docProps/custom.xml for the metadata core.xml can't hold, or "" when there is none."""
    props = [("Publisher", md.publisher), ("ISBN", md.identifiers.get("isbn", "")), ("Series", md.series),
             ("Series index", f"{md.series_index:g}" if md.series and md.series_index is not None else "")]
    body = "".join(f'<property fmtid="{CUSTOM_FMTID}" pid="{pid}" name="{name}"><vt:lpwstr>{markup._esc_text(value)}</vt:lpwstr>'
                   "</property>" for pid, (name, value) in enumerate(((n, v) for n, v in props if v), 2))
    if not body:
        return ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://schemas.openxmlformats.org/'
            'officeDocument/2006/custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'docPropsVTypes">{body}</Properties>')


class _Converter:
    def __init__(self, book, z, rels, styles, numbering):
        self.book, self.z, self.rels, self.styles, self.numbering = book, z, rels, styles, numbering
        self.docs: list[tuple[ET.Element, ET.Element]] = []
        self.new_doc()
        self.images: dict[str, str] = {}
        self.list_stack: list[tuple[str, int, ET.Element]] = []  # (numId, level, list element)
        self.bookmarks: dict[str, int] = {}  # bookmark name -> document index
        self.note_refs: list[tuple[str, str, ET.Element]] = []
        self.notes_doc = None
        self.size = 0
        self.field = None
        # text boxes (and floating pictures) waiting for their paragraph to end: (position, order, kind, element)
        self.boxes: list[tuple[tuple[float, float] | None, int, str, ET.Element]] = []
        self.box_depth = 0

    def new_doc(self):
        root, body = markup.new_document(self.book.metadata.title, self.book.metadata.language)
        self.docs.append((root, body))
        self.list_stack = []
        self.size = 0

    @property
    def body_el(self):
        return self.docs[-1][1]

    # --- blocks -------------------------------------------------------------------------------
    def body(self, body):
        for el in body:
            self.block(el, self.body_el)

    def block(self, el, parent):
        tag = el.tag
        if tag == w("p"):
            self.paragraph(el, parent)
        elif tag == w("tbl"):
            self.end_lists()
            self.table(el, parent)
        elif tag in (w("sdt"),):
            content = el.find(w("sdtContent"))
            if content is not None:
                for c in content:
                    self.block(c, parent)
        elif tag in (w("customXml"), w("smartTag"), w("ins")):
            for c in el:
                self.block(c, parent)
        elif tag == f"{{{MC}}}AlternateContent":
            fallback = el.find(f"{{{MC}}}Fallback")  # (an element's truth is its child count, and warned about)
            choice = fallback if fallback is not None and len(fallback) else el.find(f"{{{MC}}}Choice")
            if choice is not None:
                for c in choice:
                    self.block(c, parent)

    def paragraph(self, p, parent):
        ppr = p.find(w("pPr"))
        sid = _wval(ppr, "pStyle") or self.styles.default_para
        level = self.styles.heading(sid)
        outline = _wval(ppr, "outlineLvl")
        outline_n = markup.whole(outline)
        if not level and outline_n is not None and outline_n < 6:
            level = outline_n + 1
        kind = self.styles.kind(sid)
        num = ppr.find(w("numPr")) if ppr is not None else None
        page_break = ppr is not None and _on(ppr, "pageBreakBefore")
        if level == 1 and parent is self.body_el and self.size > 20000 or page_break and self.size > 20000:
            self.new_doc()
            parent = self.body_el
        if num is not None and not level:
            num_id = _wval(num, "numId")
            ilvl = int(_wval(num, "ilvl") or 0)
            if num_id and num_id != "0":
                container = self.list_item(num_id, ilvl, parent)
                el = ET.SubElement(container, "li")
                self.runs(p, el, sid)
                if self.boxes:
                    self.flush_boxes(el, el)
                self.size += len(markup.text_of(el))
                return
        self.end_lists()
        if level:
            el = ET.SubElement(parent, f"h{level}")
        elif kind == "code":
            el = ET.SubElement(parent, "pre")
        elif kind == "quote":
            el = ET.SubElement(ET.SubElement(parent, "blockquote"), "p")
        else:
            el = ET.SubElement(parent, "p")
            if kind == "subtitle":
                el.set("class", "subtitle")
        jc = _wval(ppr, "jc") or self.styles.prop(sid, "jc")
        style = []
        if jc in ("center", "right", "both", "distribute", "end"):
            style.append(f"text-align: {'justify' if jc in ('both', 'distribute') else 'right' if jc == 'end' else jc}")
        ind = ppr.find(w("ind")) if ppr is not None else None
        if ind is not None:
            left = ind.get(w("left")) or ind.get(w("start"))
            first = ind.get(w("firstLine"))
            hang = ind.get(w("hanging"))
            if (markup.whole(left) or 0) > 0:
                style.append(f"margin-left: {markup.whole(left) / 20:g}pt")
            if (markup.whole(first) or 0) > 0:
                style.append(f"text-indent: {markup.whole(first) / 20:g}pt")
            elif (markup.whole(hang) or 0) > 0:
                style.append(f"text-indent: -{markup.whole(hang) / 20:g}pt")
        if style:
            el.set("style", "; ".join(style))
        self.runs(p, el, sid)
        if self.boxes:
            self.flush_boxes(el, parent)
            if el.tag == "p" and not len(el) and not (el.text or "").strip() and el in list(parent):
                parent.remove(el)
            return
        if el.tag == "p" and not len(el) and not (el.text or "").strip():
            el.text = " "  # keep empty paragraphs as spacing
        self.size += len(markup.text_of(el))

    def take_boxes(self, shape) -> bool:
        """Keep the text boxes of a drawing (LibreOffice opens each line of a PDF as one) for after
        the paragraph they're anchored in; True when it had any."""
        boxes = list(shape.iter(w("txbxContent")))
        if not boxes:
            return False
        inner = {id(b2) for b in boxes for b2 in b.iter(w("txbxContent")) if b2 is not b}
        pos = _shape_position(shape)
        for b in boxes:
            if id(b) not in inner:
                self.boxes.append((pos, len(self.boxes), "box", b))
        return True

    def take_floating(self, shape) -> bool:
        """A floating picture (anchored, not in the line) waits too, so it takes its place among
        the text boxes; True when the shape is one."""
        drawing = shape.find(f".//{w('drawing')}") if shape.tag != w("drawing") else shape
        if drawing is None or drawing.find(f"{WP_NS}anchor") is None:
            return False
        self.boxes.append((_shape_position(shape), len(self.boxes), "picture", shape))
        return True

    def flush_boxes(self, anchor, parent):
        """The text boxes anchored in a paragraph, in reading order (top to bottom, left to right),
        as paragraphs after it; an anchor paragraph with nothing else in it goes."""
        boxes, self.boxes = self.boxes, []
        if not any(kind == "box" for _, _, kind, _ in boxes):  # floating pictures only: they stay in the paragraph
            for _, _, _, shape in boxes:
                self.image(shape, anchor)
            return
        if all(pos is not None for pos, _, _, _ in boxes):
            boxes.sort(key=lambda b: (b[0][0], b[0][1], b[1]))
        if anchor.tag == "p" and not len(anchor) and not (anchor.text or "").strip() and anchor in list(parent):
            parent.remove(anchor)
        self.box_depth += 1
        try:
            for _, _, kind, box in boxes:
                if kind == "picture":
                    self.image(box, ET.SubElement(parent, "p"))
                    continue
                if self.box_depth > 8:  # boxes in boxes in boxes...: their text, flat
                    ET.SubElement(parent, "p").text = " ".join("".join(t.text or "" for t in box.iter(w("t"))).split())
                    continue
                for c in box:
                    self.block(c, parent)
        finally:
            self.box_depth -= 1

    def list_item(self, num_id, ilvl, parent):
        tag, start = self.numbering.kind(num_id, ilvl)
        while self.list_stack and (self.list_stack[-1][1] > ilvl or self.list_stack[-1][1] == ilvl and self.list_stack[-1][0] != num_id):
            self.list_stack.pop()
        if not self.list_stack or self.list_stack[-1][1] < ilvl:
            if self.list_stack:
                holder = self.list_stack[-1][2]
                last = holder[-1] if len(holder) else ET.SubElement(holder, "li")
            else:
                holder = None
                last = parent
            lst = ET.SubElement(last, tag)
            if tag == "ol" and start != 1:
                lst.set("start", str(start))
            self.list_stack.append((num_id, ilvl, lst))
        return self.list_stack[-1][2]

    def end_lists(self):
        self.list_stack = []

    def table(self, tbl, parent):
        table = ET.SubElement(parent, "table")
        for tr in tbl.findall(w("tr")):
            row = ET.SubElement(table, "tr")
            for tc in tr.findall(w("tc")):
                tcpr = tc.find(w("tcPr"))
                vmerge = tcpr.find(w("vMerge")) if tcpr is not None else None
                if vmerge is not None and vmerge.get(w("val")) not in ("restart",):
                    continue  # continuation of a vertically merged cell
                cell = ET.SubElement(row, "td")
                span = _wval(tcpr, "gridSpan")
                if (markup.whole(span) or 0) > 1:
                    cell.set("colspan", str(markup.whole(span)))
                saved = self.list_stack
                self.list_stack = []
                for c in tc:
                    self.block(c, cell)
                self.list_stack = saved
                # a cell with one paragraph needn't keep the <p>
                if len(cell) == 1 and cell[0].tag == "p" and not cell.text:
                    p = cell[0]
                    cell.remove(p)
                    cell.text = p.text
                    for k in p:
                        cell.append(k)

    # --- runs ---------------------------------------------------------------------------------
    def runs(self, p, target, sid):
        base_bold = bool(self.styles.prop(sid, "bold"))
        base_italic = bool(self.styles.prop(sid, "italic"))
        self._inline(p, target, sid, base_bold, base_italic, link=None)

    def _inline(self, el, target, sid, bold, italic, link):
        for c in el:
            tag = c.tag
            if tag == w("r"):
                self.run(c, target, sid, bold, italic, link)
            elif tag == w("hyperlink"):
                rid = c.get(f"{{{R}}}id")
                anchor = c.get(w("anchor"))
                href = None
                if rid and rid in self.rels:
                    href = self.rels[rid][1]
                elif anchor:
                    href = f"#bm_{_safe(anchor)}"
                a = ET.SubElement(target, "a", {"href": href}) if href else target
                self._inline(c, a, sid, bold, italic, link=href)
            elif tag == w("bookmarkStart"):
                name = c.get(w("name"), "")
                if name and not name.startswith("_GoBack"):
                    ET.SubElement(target, "a", {"id": f"bm_{_safe(name)}"})
                    self.bookmarks[f"bm_{_safe(name)}"] = len(self.docs) - 1
            elif tag in (w("ins"), w("smartTag"), w("customXml"), w("fldSimple"), w("sdt"), w("sdtContent"),
                         f"{{{MC}}}AlternateContent", f"{{{MC}}}Fallback"):
                if tag == w("fldSimple"):
                    m = re.search(r'HYPERLINK\s+"([^"]+)"', c.get(w("instr"), ""))
                    if m:
                        a = ET.SubElement(target, "a", {"href": m.group(1)})
                        self._inline(c, a, sid, bold, italic, m.group(1))
                        continue
                if tag == f"{{{MC}}}AlternateContent":
                    alt = c.find(f"{{{MC}}}Fallback")
                    if alt is None:
                        alt = c.find(f"{{{MC}}}Choice")
                    if alt is not None:
                        self._inline(alt, target, sid, bold, italic, link)
                    continue
                self._inline(c, target, sid, bold, italic, link)
            elif tag in (f"{{{OMML}}}oMath", f"{{{OMML}}}oMathPara"):
                target.append(mathml.from_omml(c))
            elif tag == w("del") or tag == w("moveFrom"):
                continue

    def run(self, r, target, sid, bold, italic, link):
        rpr = r.find(w("rPr"))
        rstyle = _wval(rpr, "rStyle")
        b = _on(rpr, "b")
        b = b if b is not None else (self.styles.prop(rstyle, "bold") if rstyle else None)
        i = _on(rpr, "i")
        i = i if i is not None else (self.styles.prop(rstyle, "italic") if rstyle else None)
        b = bold if b is None else b
        i = italic if i is None else i
        u = _wval(rpr, "u")
        strike = _on(rpr, "strike") or _on(rpr, "dstrike")
        va = _wval(rpr, "vertAlign")
        mono = self.styles._mono(rpr) or (self.styles.prop(rstyle, "mono") if rstyle else None)
        caps = _on(rpr, "smallCaps")
        hidden = _on(rpr, "vanish")
        if hidden:
            return
        # field codes: HYPERLINK in a complex field turns the following text into a link
        for c in r:
            if c.tag == w("fldChar"):
                kind = c.get(w("fldCharType"))
                if kind == "begin":
                    self.field = {"instr": "", "result": False}
                elif kind == "separate" and self.field is not None:
                    self.field["result"] = True
                elif kind == "end":
                    self.field = None
        if self.field is not None and not self.field["result"]:
            for c in r.findall(w("instrText")):
                self.field["instr"] += c.text or ""
            return
        dest = target
        if self.field is not None and self.field["result"]:
            m = re.search(r'HYPERLINK\s+(?:\\l\s+)?"([^"]+)"', self.field["instr"])
            if m and link is None:
                href = m.group(1) if "\\l" not in self.field["instr"] else f"#bm_{_safe(m.group(1))}"
                dest = ET.SubElement(target, "a", {"href": href})
        wrappers = []
        if b:
            wrappers.append("b")
        if i:
            wrappers.append("i")
        if u and u != "none":
            wrappers.append("u")
        if strike:
            wrappers.append("s")
        if va == "superscript":
            wrappers.append("sup")
        elif va == "subscript":
            wrappers.append("sub")
        if mono:
            wrappers.append("code")
        for t in wrappers:
            dest = ET.SubElement(dest, t)
        if caps:
            dest = ET.SubElement(dest, "span", {"style": "font-variant: small-caps"})
        for c in r:
            tag = c.tag
            if tag == w("t"):
                markup.append_text(dest, c.text or "")
            elif tag == w("tab"):
                markup.append_text(dest, "\t")
            elif tag in (w("br"), w("cr")):
                if c.get(w("type")) == "page":
                    continue
                ET.SubElement(dest, "br")
            elif tag == w("noBreakHyphen"):
                markup.append_text(dest, "‑")
            elif tag == w("softHyphen"):
                markup.append_text(dest, "­")
            elif tag == w("sym"):
                ch = c.get(w("char"), "")
                try:
                    markup.append_text(dest, chr(int(ch[2:] if ch.upper().startswith("F0") else ch, 16)))
                except ValueError:
                    pass
            elif tag == w("drawing") or tag == w("pict") or tag == w("object"):
                if not self.take_boxes(c) and not self.take_floating(c):
                    self.image(c, dest)
            elif tag in (w("footnoteReference"), w("endnoteReference")):
                kind = "footnote" if tag == w("footnoteReference") else "endnote"
                nid = c.get(w("id"), "")
                a = ET.SubElement(dest, "a", {"href": f"#{kind}{nid}", "id": f"{kind}ref{nid}", "class": "noteref",
                                              "epub:type": "noteref"})
                ET.SubElement(a, "sup").text = str(len(self.note_refs) + 1)
                self.note_refs.append((kind, nid, a))
            elif tag == f"{{{MC}}}AlternateContent":
                # one of the two copies only: the shape (with its text boxes) or, failing that, the fallback
                branches = [x for x in (c.find(f"{{{MC}}}Choice"), c.find(f"{{{MC}}}Fallback")) if x is not None]
                boxed = next((x for x in branches if x.find(f".//{w('txbxContent')}") is not None), None)
                if boxed is not None:
                    for k in boxed:
                        if k.tag in (w("drawing"), w("pict")):
                            self.take_boxes(k)
                    continue
                fb = c.find(f"{{{MC}}}Fallback")
                if fb is None:
                    fb = c.find(f"{{{MC}}}Choice")
                if fb is not None:
                    for k in fb:
                        if k.tag in (w("drawing"), w("pict")) and not self.take_floating(c):
                            self.image(k, dest)

    def image(self, el, target):
        rid = None
        for blip in el.iter(f"{{{A}}}blip"):
            rid = blip.get(f"{{{R}}}embed") or blip.get(f"{{{R}}}link")
            break
        if rid is None:
            for im in el.iter(f"{{{V}}}imagedata"):
                rid = im.get(f"{{{R}}}id")
                break
        if rid is None or rid not in self.rels:
            return
        _kind, part, external = self.rels[rid]
        if external:
            return
        href = self.images.get(part)
        if href is None:
            try:
                data = limits.zip_read(self.z, part)
            except KeyError:
                return
            if sniff_image(data) is None:
                return
            href = self.book.add("images/" + re.sub(r"[^\w.-]", "_", posixpath.basename(part)), data).href
            self.images[part] = href
        alt = ""
        for d in el.iter(f"{{{WP}}}docPr"):
            alt = d.get("descr") or d.get("title") or ""
            break
        size = ""
        for ext in el.iter(f"{{{WP}}}extent"):
            try:
                width_px = int(ext.get("cx", "0")) / 9525
                if width_px:
                    size = f"width: {min(width_px, 2000):.0f}px"
            except ValueError:
                pass
            break
        attrs = {"src": href, "alt": alt}
        if size:
            attrs["style"] = f"{size}; max-width: 100%"
        ET.SubElement(target, "img", attrs)

    # --- notes --------------------------------------------------------------------------------
    def notes(self, kind, root, rels):
        tagname = "footnote" if kind == "footnotes" else "endnote"
        wanted = {nid for k, nid, _a in self.note_refs if k == tagname}
        if not wanted:
            return
        if self.notes_doc is None:
            self.notes_doc = markup.new_document("Notes", self.book.metadata.language)
            ET.SubElement(self.notes_doc[1], "h2").text = "Notes"
        body = self.notes_doc[1]
        saved = self.rels
        self.rels = {**self.rels, **rels}
        for note in root.findall(w(tagname)):
            nid = note.get(w("id"), "")
            if nid not in wanted:
                continue
            aside = ET.SubElement(body, "aside", {"id": f"{tagname}{nid}", "epub:type": tagname})
            for p in note:
                self.block(p, aside)
            first = aside.find("p")
            if first is not None:
                back = ET.Element("a", {"href": f"#{tagname}ref{nid}"})
                n = next((k + 1 for k, (kk, ni, _a) in enumerate(self.note_refs) if kk == tagname and ni == nid), "")
                back.text = f"{n}. "
                back.tail = first.text
                first.text = None
                first.insert(0, back)
        self.rels = saved

    def finish(self):
        book = self.book
        hrefs = []
        for n, (root, body) in enumerate(self.docs, 1):
            hrefs.append(f"text/part{n:04d}.xhtml")
        notes_href = None
        if self.notes_doc is not None:
            notes_href = "text/notes.xhtml"
        # bookmark and note links across documents
        targets = {}
        for i, (root, _b) in enumerate(self.docs):
            for el in root.iter():
                if el.get("id"):
                    targets[el.get("id")] = hrefs[i]
        if self.notes_doc is not None:
            for el in self.notes_doc[0].iter():
                if el.get("id"):
                    targets[el.get("id")] = notes_href
        all_docs = list(zip(hrefs, self.docs)) + ([(notes_href, self.notes_doc)] if self.notes_doc else [])
        for href, (root, _b) in all_docs:
            for el in root.iter():
                if el.tag == "img" and el.get("src") and not el.get("src").startswith(("..", "http")):
                    el.set("src", relative(href, el.get("src")))
                if el.tag == "a" and el.get("href", "").startswith("#"):
                    frag = el.get("href")[1:]
                    if frag in targets:
                        el.set("href", relative(href, f"{targets[frag]}#{frag}"))
            book.add_document(href, root)


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)


# --- writing -----------------------------------------------------------------------------------

EMU_PER_PX = 9525
MAX_WIDTH_EMU = int(6.0 * 914400)  # 6 inches: a Letter or A4 page less its margins
MAX_HEIGHT_EMU = int(8.5 * 914400)


def _esc(s: str) -> str:
    return markup._esc_attr(markup.clean_text(s))


def _t(text: str) -> str:
    text = markup.clean_text(text)
    space = ' xml:space="preserve"' if text != text.strip() or "  " in text else ""
    return f"<w:t{space}>{markup._esc_text(text)}</w:t>"


class _DocxWriter:
    def __init__(self, book: Book, notes: dict[str, ET.Element] | None = None):
        self.book = book
        self.notes = notes or {}  # note key -> its <aside>, taken out of the text
        self.note_ids: dict[str, int] = {}
        self.lists: dict[int, tuple[int, str, int, int]] = {}  # list key -> (numId, format, start, level)
        self.targets: set[str] = set()
        self.rels: list[tuple[str, str, str, bool]] = []  # (id, type, target, external)
        self.media: dict[str, tuple[str, str]] = {}  # href -> (zip name, rel id)
        self.bookmarks: dict[str, str] = {}
        self.placed: set[str] = set()  # bookmarks already in the document (names must be unique)
        self.bm_id = 0
        self.ext_links: dict[str, str] = {}
        self.img_n = 0

    def rel(self, kind, target, external=False) -> str:
        rid = f"rId{len(self.rels) + 10}"
        self.rels.append((rid, kind, target, external))
        return rid

    def bookmark(self, key: str) -> str:
        if key not in self.bookmarks:
            self.bookmarks[key] = f"bm{len(self.bookmarks) + 1}"
        return self.bookmarks[key]

    def place(self, key: str) -> str:
        """The bookmark for ``key`` at this spot, once per document."""
        if key in self.placed:
            return ""
        self.placed.add(key)
        self.bm_id += 1
        return f'<w:bookmarkStart w:id="{self.bm_id}" w:name="{self.bookmark(key)}"/><w:bookmarkEnd w:id="{self.bm_id}"/>'

    def image(self, href: str):
        res = self.book.resources.get(href)
        if res is None or not res.is_image:
            return None
        if href not in self.media:  # converted once, however often the picture is used
            data, mt = res.data, res.media_type
            if mt not in ("image/jpeg", "image/png", "image/gif"):
                from ..images import to_png
                png = to_png(data)
                if png is None:
                    return None
                data, mt = png, "image/png"
            ext = {"image/jpeg": ".jpeg", "image/png": ".png", "image/gif": ".gif"}[mt]
            name = f"media/image{len(self.media) + 1}{ext}"
            rid = self.rel("http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", name)
            self.media[href] = (name, rid, data)
        name, rid, data = self.media[href]
        from .epub import image_size
        w_px, h_px = image_size(data) or (300, 300)
        cx, cy = w_px * EMU_PER_PX, h_px * EMU_PER_PX
        scale = min(1.0, MAX_WIDTH_EMU / cx if cx else 1, MAX_HEIGHT_EMU / cy if cy else 1)
        return rid, int(cx * scale), int(cy * scale)

    def drawing(self, href, alt="") -> str:
        got = self.image(href)
        if got is None:
            return ""
        rid, cx, cy = got
        self.img_n += 1
        n = self.img_n
        return (f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
                f'<wp:docPr id="{n}" name="Picture {n}" descr="{_esc(alt)}"/><wp:cNvGraphicFramePr>'
                f'<a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr><a:graphic><a:graphicData '
                f'uri="{PIC}"><pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" name="Picture {n}"/><pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr>'
                f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/>'
                f'</a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')

    def runs(self, runs, targets, code: bool = False) -> str:
        out = []
        for r in runs:
            for a in r.anchors:
                if a in targets:
                    out.append(self.place(a))
            if r.note:
                nid = self.note_id(r.note)
                if nid:
                    out.append(f'<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteReference w:id="{nid}"/>'
                               "</w:r>")
                continue
            if r.image:
                out.append(self.drawing(r.image, r.alt))
                continue
            if r.math is not None:
                om = mathml.to_omml(r.math)
                if om.tag == f"{{{OMML}}}oMathPara" and sum(1 for x in runs if x.text or x.image) > 1:
                    om = om[0]  # a display equation among words is set inline
                out.append(ET.tostring(om, encoding="unicode"))
                continue
            if not r.text:
                continue
            props = []
            if r.href:
                props.append('<w:rStyle w:val="Hyperlink"/>')
                if r.mono and not code:
                    props.append('<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:cs="Courier New"/>')
            elif r.mono and not code:
                props.append('<w:rStyle w:val="VerbatimChar"/>')
            if r.bold:
                props.append("<w:b/>")
            if r.italic:
                props.append("<w:i/>")
            if r.smallcaps:
                props.append("<w:smallCaps/>")
            if r.strike:
                props.append("<w:strike/>")
            if r.color and re.fullmatch(r"#[0-9a-fA-F]{6}", r.color) and r.color.lower() not in ("#000000",):
                props.append(f'<w:color w:val="{r.color[1:].upper()}"/>')
            if r.size and abs(r.size - 1) > 0.1 and not (r.sup or r.sub) and not code:
                props.append(f'<w:sz w:val="{max(8, min(144, round(24 * r.size)))}"/>')
            if r.underline:
                props.append('<w:u w:val="single"/>')
            if r.sup:
                props.append('<w:vertAlign w:val="superscript"/>')
            elif r.sub:
                props.append('<w:vertAlign w:val="subscript"/>')
            rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
            parts = r.text.split("\n")
            body = "<w:br/>".join(_t(p) for p in parts)
            run = f"<w:r>{rpr}{body}</w:r>"
            if r.href and not r.href.lower().startswith("file:"):
                # a web address, or a relative one that points outside the book
                if is_external(r.href) or r.href.split("#", 1)[0] not in self.book.resources:
                    rid = self.ext_links.get(r.href)
                    if rid is None:
                        rid = self.rel("http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                                       r.href, external=True)
                        self.ext_links[r.href] = rid
                    run = f'<w:hyperlink r:id="{rid}" w:history="1">{run}</w:hyperlink>'
                elif r.href in targets:
                    run = f'<w:hyperlink w:anchor="{self.bookmark(r.href)}" w:history="1">{run}</w:hyperlink>'
            out.append(run)
        return "".join(out)

    def note_id(self, key: str) -> int:
        """The footnote number for a note (by its key), 0 when the book has no such note."""
        if key not in self.notes:
            return 0
        if key not in self.note_ids:
            self.note_ids[key] = len(self.note_ids) + 1
        return self.note_ids[key]

    def list_num(self, b) -> tuple[int, int]:
        """(numId, level) for a list item block: one numbering instance per list."""
        level = max(0, min(8, b.depth - 1))
        if b.list_key not in self.lists:
            fmt = _LIST_FORMATS.get(b.list_type, "bullet")
            self.lists[b.list_key] = (len(self.lists) + 1, fmt, b.list_start, level)
        return self.lists[b.list_key][0], level

    def paragraph(self, b, targets, bms: str, first: bool, style: str = "") -> str:
        ppr = []
        item = bool(b.marker and b.list_key)
        if b.kind == "h":
            ppr.append(f'<w:pStyle w:val="Heading{min(max(b.level, 1), 6)}"/>')
        elif style:
            ppr.append(f'<w:pStyle w:val="{style}"/>')
        elif b.kind == "pre":
            ppr.append('<w:pStyle w:val="SourceCode"/>')
        elif b.role in ("caption", "figcaption"):
            ppr.append(f'<w:pStyle w:val="{"TableCaption" if b.role == "caption" else "ImageCaption"}"/>')
        elif b.role == "dt":
            ppr.append('<w:pStyle w:val="DefinitionTerm"/>')
        elif b.role == "dd":
            ppr.append('<w:pStyle w:val="Definition"/>')
        elif b.kind == "image" and b.role == "figure":
            ppr.append('<w:pStyle w:val="CaptionedFigure"/>')
        elif b.quote:
            ppr.append('<w:pStyle w:val="Quote"/>')
        elif item and b.kind == "li":
            ppr.append('<w:pStyle w:val="Compact"/>')  # an item that is one line of text, not paragraphs: a tight list
        if b.page_break and not first:
            ppr.append("<w:pageBreakBefore/>")
        if item:
            num, level = self.list_num(b)
            ppr.append(f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr>')
        align = {"center": "center", "right": "right", "justify": "both"}.get(b.align)
        if b.kind == "image":
            align = "center"
        if align:
            ppr.append(f'<w:jc w:val="{align}"/>')
        left = b.margin_left * 240 if b.margin_left else 0
        if b.list_key and not item:
            left = 720 * max(1, b.depth)  # a later paragraph of a list item lines up with its text
        elif b.marker and not b.list_key:
            left = 360 * b.depth + 360
        elif item or b.role or style:
            left = 0  # the numbering or the style sets the indent
        indent = ""
        if left > 0:
            indent += f' w:left="{int(left)}"'
        if b.marker and not item:
            indent += ' w:hanging="360"'
        elif b.indent > 0 and b.kind == "p" and not b.list_key:
            indent += f' w:firstLine="{int(b.indent * 240)}"'
        if indent:
            ppr.append(f"<w:ind{indent}/>")
        if b.kind == "p" and b.margin_top == 0 and b.indent > 0:
            ppr.append('<w:spacing w:before="0" w:after="0"/>')
        ppr_xml = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
        if b.kind == "image":
            content = self.drawing(b.image, b.alt)
        else:
            marker = "" if item or not b.marker else \
                f"<w:r><w:t xml:space=\"preserve\">{markup._esc_text(b.marker)}\t</w:t></w:r>"
            runs = b.runs
            if b.kind == "h":  # the heading style is bold already: bold runs would read as strong text
                from dataclasses import replace
                runs = [replace(r, bold=False) for r in runs]
            content = marker + self.runs(runs, targets, code=b.kind == "pre")
        return f"<w:p>{ppr_xml}{bms}{content}</w:p>"

    def document(self) -> str:
        from ..flow import book_blocks
        blocks = book_blocks(self.book)
        self.targets = targets = {r.href for b in blocks for r in b.runs if r.href and not is_external(r.href)}
        targets |= {r.href for b in blocks if b.kind == "table" for row in b.rows for c in row for r in c.runs
                    if r.href and not is_external(r.href)}
        body = []
        cover = self.book.cover_image()
        if cover is not None:
            first_img = next((b.image for b in blocks[:3] if b.kind == "image"), None)
            if first_img != self.book.cover:
                body.append(f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr>{self.drawing(self.book.cover, "Cover")}</w:p>')
                body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        for b in blocks:
            bms = ""
            for a in b.anchors:
                if a in targets:
                    bms += self.place(a)
            if b.kind == "table":
                body.append(self.table(b, targets, bms))
                continue
            if b.kind == "hr":
                body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="auto"/></w:pBdr>'
                            "</w:pPr></w:p>")
                continue
            body.append(self.paragraph(b, targets, bms, first=not body))
        body.append('<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
                    'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>')
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {_ROOT_NS}><w:body>'
                + "".join(body) + "</w:body></w:document>")

    def footnotes(self) -> str:
        """footnotes.xml: the separators Word expects, then every note that is called, in call order."""
        from ..flow import document_blocks
        out = [('<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr><w:spacing w:after="0" w:line="240" '
                'w:lineRule="auto"/></w:pPr><w:r><w:separator/></w:r></w:p></w:footnote>'
                '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:pPr><w:spacing w:after="0" w:line="240" '
                'w:lineRule="auto"/></w:pPr><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>')]
        done = 0
        keys = list(self.note_ids)
        while done < len(keys):  # a note may call another
            key = keys[done]
            done += 1
            href = key.split("#", 1)[0]
            root, body = markup.new_document()
            aside = self.notes[key]
            body.text = aside.text
            for c in list(aside):
                body.append(c)
            paras = []
            for b in document_blocks(self.book, href, root):
                if b.kind == "table":
                    paras.append(self.table(b, self.targets, ""))
                    continue
                if b.kind == "hr":
                    continue
                p = self.paragraph(b, self.targets, "", first=True, style="" if b.kind in ("pre", "li") else "FootnoteText")
                paras.append(p)
            ref = '<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/></w:r>' \
                  '<w:r><w:t xml:space="preserve"> </w:t></w:r>'
            if paras and paras[0].startswith("<w:p>"):
                head = re.match(r"<w:p>(<w:pPr>.*?</w:pPr>)?", paras[0])
                paras[0] = paras[0][:head.end()] + ref + paras[0][head.end():]
            else:
                paras.insert(0, f'<w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>{ref}</w:p>')
            out.append(f'<w:footnote w:id="{self.note_ids[key]}">{"".join(paras)}</w:footnote>')
            keys = list(self.note_ids)
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:footnotes {_ROOT_NS}>'
                + "".join(out) + "</w:footnotes>")

    def numbering(self) -> str:
        abstracts: dict[str, int] = {}
        for _num, fmt, _start, _level in self.lists.values():
            abstracts.setdefault(fmt, len(abstracts))
        out = []
        for fmt, aid in abstracts.items():
            levels = []
            for i in range(9):
                text = "•◦▪"[i % 3] if fmt == "bullet" else f"%{i + 1}."
                levels.append(f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/>'
                              f'<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{720 * (i + 1)}" w:hanging="360"/></w:pPr>'
                              "</w:lvl>")
            out.append(f'<w:abstractNum w:abstractNumId="{aid}"><w:multiLevelType w:val="hybridMultilevel"/>'
                       + "".join(levels) + "</w:abstractNum>")
        for num, fmt, start, level in self.lists.values():
            override = "" if fmt == "bullet" else \
                f'<w:lvlOverride w:ilvl="{level}"><w:startOverride w:val="{start}"/></w:lvlOverride>'
            out.append(f'<w:num w:numId="{num}"><w:abstractNumId w:val="{abstracts[fmt]}"/>{override}</w:num>')
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering xmlns:w="{W}">'
                + "".join(out) + "</w:numbering>")

    def table(self, b, targets, bms) -> str:
        cols = max(sum(c.colspan for c in row) for row in b.rows)
        width = int(9360 / max(1, cols))
        out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/><w:tblBorders>'
               + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
                         for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
               + '</w:tblBorders><w:tblLook w:val="0020" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" '
               'w:noHBand="0" w:noVBand="0"/></w:tblPr><w:tblGrid>' + f'<w:gridCol w:w="{width}"/>' * cols + "</w:tblGrid>"]
        header_rows = 0
        for row in b.rows:
            if row and all(c.header for c in row):
                header_rows += 1
            else:
                break
        merged: dict[int, tuple[int, int]] = {}  # column -> (rows still covered, columns spanned) by a rowspan above
        for k, row in enumerate(b.rows):
            out.append("<w:tr><w:trPr><w:tblHeader/></w:trPr>" if k < header_rows else "<w:tr>")
            col = 0
            cells = iter(row)
            first = True
            while col < cols:
                if col in merged:
                    left, span = merged[col]
                    gs = f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
                    out.append(f'<w:tc><w:tcPr><w:tcW w:w="{width * span}" w:type="dxa"/>{gs}<w:vMerge/></w:tcPr><w:p/></w:tc>')
                    if left <= 1:
                        del merged[col]
                    else:
                        merged[col] = (left - 1, span)
                    col += span
                    continue
                cell = next(cells, None)
                if cell is None:
                    out.append(f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr><w:p/></w:tc>')
                    col += 1
                    continue
                span = max(1, min(cell.colspan, cols - col))
                gs = f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
                vm = ""
                if cell.rowspan > 1 and k + 1 < len(b.rows):
                    vm = '<w:vMerge w:val="restart"/>'
                    merged[col] = (min(cell.rowspan, len(b.rows) - k) - 1, span)
                runs = self.runs(cell.runs, targets)
                if cell.header:
                    runs = runs.replace("<w:r><w:t", "<w:r><w:rPr><w:b/></w:rPr><w:t")
                prefix = bms if k == 0 and first else ""
                first = False
                out.append(f'<w:tc><w:tcPr><w:tcW w:w="{width * span}" w:type="dxa"/>{gs}{vm}</w:tcPr>'
                           f"<w:p>{prefix}{runs}</w:p></w:tc>")
                col += span
            out.append("</w:tr>")
        out.append("</w:tbl><w:p/>")
        return "".join(out)


_LIST_FORMATS = {"disc": "bullet", "circle": "bullet", "square": "bullet", "none": "bullet", "decimal": "decimal",
                 "decimal-leading-zero": "decimalZero", "lower-alpha": "lowerLetter", "lower-latin": "lowerLetter",
                 "upper-alpha": "upperLetter", "upper-latin": "upperLetter", "lower-roman": "lowerRoman",
                 "upper-roman": "upperRoman"}
_ROOT_NS = f'xmlns:w="{W}" xmlns:r="{R}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:pic="{PIC}" xmlns:m="{OMML}"'
_NOTE_BACKLINK = re.compile(r"^\s*(?:\[?\d+\]?[.)]?|↩︎?)\s*$")


def _keep_web_links(book: Book, before: bool) -> None:
    """Links to relative web addresses outside the book ("/about/") are dropped by the clean-up
    for EPUB; a Word document keeps them: park them before it and put them back after."""
    from ..book import resolve
    for href, _ in book.documents():
        root = book.tree(href)
        changed = False
        for a in root.iter("a"):
            if before:
                link = a.get("href", "")
                doc = link.split("#", 1)[0]
                if doc and not is_external(link) and not link.lower().startswith(("file:", "javascript:"))                         and resolve(href, doc) not in book.resources:
                    a.set("data-web-href", link)
                    changed = True
            elif a.get("data-web-href") is not None:
                a.set("href", a.attrib.pop("data-web-href"))
                changed = True
        if changed:
            book.set_tree(href, root)


def _take_notes(book: Book) -> dict[str, ET.Element]:
    """Move every footnote the book calls out of the text: {key: its <aside>}. The calls get a
    data-note attribute (the flow turns them into note runs); a "Notes" page left empty goes."""
    from ..book import resolve
    from ..textwriter import is_note, is_noteref
    asides: dict[str, tuple[str, ET.Element]] = {}
    for href, _ in book.documents():
        for el in book.tree(href).iter():
            if is_note(el) and el.get("id"):
                asides[f"{href}#{el.get('id')}"] = (href, el)
    used: dict[str, ET.Element] = {}
    for href, _ in book.documents():
        for a in book.tree(href).iter("a"):
            if not is_noteref(a):
                continue
            link = a.get("href", "")
            doc, _, frag = link.partition("#")
            key = f"{resolve(href, doc) if doc else href}#{frag}"
            if key in asides:
                a.set("data-note", key)
                used[key] = asides[key][1]
    touched = set()
    for key, aside in used.items():
        href = asides[key][0]
        root = book.tree(href)
        parent = next((p for p in root.iter() if aside in list(p)), None)
        if parent is not None:
            parent.remove(aside)
            touched.add(href)
        # the number or arrow some readers put at the start of a note, linking back to the call
        for a in list(aside.iter("a")):
            if _NOTE_BACKLINK.match(markup.text_of(a)) and a.get("href"):
                holder = next((p for p in aside.iter() if a in list(p)), None)
                if holder is not None:
                    tail = a.tail
                    idx = list(holder).index(a)
                    holder.remove(a)
                    if tail:
                        if idx == 0:
                            holder.text = (holder.text or "") + tail
                        else:
                            holder[idx - 1].tail = (holder[idx - 1].tail or "") + tail
    for href in touched:
        root = book.tree(href)
        body = root.find("body")
        if body is not None and not "".join(markup.text_of(c) for c in body if c.tag not in ("h1", "h2", "h3")).strip() \
                and not body.findall(".//img"):
            book.spine = [s for s in book.spine if s != href]
        else:
            book.set_tree(href, root)
    return used


STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia" w:eastAsia="SimSun" w:cs="Times New Roman"/>
<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="%LANG%"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:qFormat/><w:pPr><w:jc w:val="center"/><w:spacing w:before="480" w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="52"/></w:rPr></w:style>
%HEADINGS%
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:ind w:left="720" w:right="720"/></w:pPr><w:rPr><w:i/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="SourceCode"><w:name w:val="Source Code"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:cs="Courier New"/><w:sz w:val="20"/></w:rPr></w:style>
<w:style w:type="character" w:styleId="VerbatimChar"><w:name w:val="Verbatim Char"/><w:basedOn w:val="DefaultParagraphFont"/>
<w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:cs="Courier New"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="FootnoteText"><w:name w:val="Footnote Text"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:after="60"/></w:pPr><w:rPr><w:sz w:val="20"/></w:rPr></w:style>
<w:style w:type="character" w:styleId="FootnoteReference"><w:name w:val="Footnote Reference"/>
<w:basedOn w:val="DefaultParagraphFont"/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="TableCaption"><w:name w:val="Table Caption"/><w:basedOn w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="120"/></w:pPr><w:rPr><w:i/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="CaptionedFigure"><w:name w:val="Captioned Figure"/><w:basedOn w:val="Normal"/>
<w:pPr><w:keepNext/><w:jc w:val="center"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="ImageCaption"><w:name w:val="Image Caption"/><w:basedOn w:val="Normal"/>
<w:pPr><w:jc w:val="center"/></w:pPr><w:rPr><w:i/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Compact"><w:name w:val="Compact"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:before="36" w:after="36"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="DefinitionTerm"><w:name w:val="Definition Term"/><w:basedOn w:val="Normal"/>
<w:next w:val="Definition"/><w:pPr><w:keepNext/><w:spacing w:after="0"/></w:pPr><w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Definition"><w:name w:val="Definition"/><w:basedOn w:val="Normal"/>
<w:pPr><w:ind w:left="720"/></w:pPr></w:style>
<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/></w:style>
<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/><w:basedOn w:val="DefaultParagraphFont"/>
<w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>
<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/>
<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/>
<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/></w:style>
</w:styles>"""


def _headings():
    sizes = {1: 40, 2: 32, 3: 28, 4: 26, 5: 24, 6: 24}
    out = []
    for n in range(1, 7):
        italic = "<w:i/>" if n >= 5 else ""
        out.append(f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
                   f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/>'
                   f'<w:spacing w:before="{360 if n < 3 else 240}" w:after="120"/><w:outlineLvl w:val="{n - 1}"/></w:pPr>'
                   f'<w:rPr><w:b/>{italic}<w:sz w:val="{sizes[n]}"/></w:rPr></w:style>')
    return "\n".join(out)


def write(book: Book, path, **_) -> None:
    from .. import clean
    b = book.copy()
    _keep_web_links(b, True)
    clean.clean_book(b)
    _keep_web_links(b, False)
    wr = _DocxWriter(b, _take_notes(b))
    document = wr.document()
    footnotes = wr.footnotes() if wr.note_ids else ""
    numbering = wr.numbering() if wr.lists else ""
    md = b.metadata
    lang = md.language or "en"
    styles = STYLES.replace("%HEADINGS%", _headings()).replace("%LANG%", _esc(lang))
    rels = [("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles", "styles.xml", False),
            ("rId2", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings", "settings.xml", False)]
    if numbering:
        rels.append(("rId3", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering", "numbering.xml",
                     False))
    if footnotes:
        rels.append(("rId4", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes", "footnotes.xml",
                     False))
    note_rel_xml = "".join(
        f'<Relationship Id="{rid}" Type="{kind}" Target="{_esc(target)}"' + (' TargetMode="External"' if ext else "") + "/>"
        for rid, kind, target, ext in wr.rels)
    rels += wr.rels
    rel_xml = "".join(
        f'<Relationship Id="{rid}" Type="{kind}" Target="{_esc(target)}"' + (' TargetMode="External"' if ext else "") + "/>"
        for rid, kind, target, ext in rels)
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    created = md.date if re.fullmatch(r"\d{4}(-\d\d(-\d\d)?)?", md.date or "") else now  # the book's own date
    custom = _custom_props(md)
    core = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties xmlns:cp="{CP}" xmlns:dc="{DC}" '
            f'xmlns:dcterms="{DCTERMS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{markup._esc_text(md.title)}</dc:title><dc:creator>{markup._esc_text('; '.join(md.authors))}</dc:creator>"
            + (f"<dc:subject>{markup._esc_text(md.subjects[0])}</dc:subject>" if md.subjects else "")
            + (f"<cp:keywords>{markup._esc_text(', '.join(md.subjects))}</cp:keywords>" if md.subjects else "")
            + (f"<dc:description>{markup._esc_text(re.sub(r'<[^>]+>', '', md.description))}</dc:description>" if md.description else "")
            + f"<dc:language>{_esc(lang)}</dc:language>"
            + (f"<dc:identifier>{markup._esc_text(md.uid())}</dc:identifier>")
            + f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>'
            + f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://schemas.openxmlformats.org/'
           'officeDocument/2006/extended-properties"><Application>bookport</Application></Properties>')
    settings = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:settings xmlns:w="{W}">'
                '<w:defaultTabStop w:val="720"/>'
                + ('<w:footnotePr><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>' if footnotes else "")
                + "<w:compat/></w:settings>")
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/'
              'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
              'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
              '<Default Extension="png" ContentType="image/png"/><Default Extension="jpeg" ContentType="image/jpeg"/>'
              '<Default Extension="gif" ContentType="image/gif"/>'
              '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.'
              'openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/settings.xml" '
              'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
              '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties'
              '+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'extended-properties+xml"/>'
              + ('<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.numbering+xml"/>' if numbering else "")
              + ('<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.footnotes+xml"/>' if footnotes else "")
              + ('<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'custom-properties+xml"/>' if custom else "")
              + "</Types>")
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="' + REL + '">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                 'officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.'
                 'org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship '
                 'Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
                 'Target="docProps/app.xml"/>'
                 + ('<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                    'custom-properties" Target="docProps/custom.xml"/>' if custom else "")
                 + '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        if custom:
            z.writestr("docProps/custom.xml", custom)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/settings.xml", settings)
        z.writestr("word/_rels/document.xml.rels",
                   f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{REL}">{rel_xml}</Relationships>')
        if numbering:
            z.writestr("word/numbering.xml", numbering)
        if footnotes:
            z.writestr("word/footnotes.xml", footnotes)
            z.writestr("word/_rels/footnotes.xml.rels",
                       f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{REL}">{note_rel_xml}'
                       "</Relationships>")
        for name, _rid, data in wr.media.values():
            z.writestr(f"word/{name}", data)
    Path(path).write_bytes(buf.getvalue())
