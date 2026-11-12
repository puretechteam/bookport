"""DocBook 4 and 5 XML: a reader (books, articles, chapters and sections) and a DocBook 5 writer.

Read: sections of every kind with their titles, info/bookinfo metadata, paragraphs, all list kinds,
program listings, quotes, admonitions, CALS and HTML tables, figures and media objects (local
pictures through ``safe_path``), inline markup, links, cross references, footnotes, MathML and TeX
equations, glossaries and bibliographies. XInclude pulls in files beside the document. The XML is
parsed with bookport's safe parser: no DTD or external entity is ever loaded, and the simple text
entities a DOCTYPE declares are used without letting one expand into another.
"""

from __future__ import annotations

import copy
import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, Metadata, is_external
from ..errors import BookportError
from ..limits import MAX_DEPTH, safe_path
from ..textwriter import Table, TextWriter, write_text
from .mediawiki import html_book, local_picture

MAX_INCLUDES = 500
MAX_INCLUDE_DEPTH = 8
MAX_ENTITY_TEXT = 16 * 1024 * 1024  # what declared entities may add to a document, all uses together

SECTIONS = {"book", "article", "set", "part", "chapter", "appendix", "preface", "section", "sect1", "sect2", "sect3",
            "sect4", "sect5", "simplesect", "refentry", "refsect1", "refsect2", "refsect3", "refsection", "reference",
            "glossary", "bibliography", "index", "colophon", "dedication", "acknowledgements", "partintro",
            "refsynopsisdiv", "glossdiv", "bibliodiv", "topic"}
DOC_ROOTS = {"book", "article", "set"}
ADMONITIONS = {"note", "warning", "tip", "caution", "important", "danger"}
VERBATIM = {"programlisting", "screen", "literallayout", "synopsis", "address", "funcsynopsis", "cmdsynopsis",
            "classsynopsis", "programlistingco", "screenco"}
LISTS = {"itemizedlist", "orderedlist", "variablelist", "simplelist", "procedure", "substeps", "stepalternatives",
         "glosslist", "segmentedlist", "calloutlist", "qandaset", "qandadiv", "bibliolist"}
BLOCKS = (SECTIONS | ADMONITIONS | VERBATIM | LISTS | {
    "para", "simpara", "formalpara", "blockquote", "epigraph", "table", "informaltable", "figure", "informalfigure",
    "mediaobject", "equation", "informalequation", "example", "informalexample", "sidebar", "bridgehead", "abstract",
    "biblioentry", "bibliomixed", "glossentry", "msgset", "task", "revhistory", "legalnotice", "highlights", "div",
    "tr", "html:table", "screenshot", "include", "mediaobjectco", "productionset", "remark-block", "blockinfo"})
SKIP = {"indexterm", "titleabbrev", "toc", "lot", "co", "coref", "areaspec", "remark", "beginpage", "info", "bookinfo",
        "articleinfo", "chapterinfo", "sectioninfo", "sect1info", "sect2info", "sect3info", "sect4info", "sect5info",
        "prefaceinfo", "appendixinfo", "partinfo", "refentryinfo", "refsect1info", "refmiscinfo", "setinfo",
        "glossaryinfo", "bibliographyinfo", "referenceinfo", "refsynopsisdivinfo", "subtitle", "attribution"}
EMPH = {"emphasis": "em", "citetitle": "cite", "firstterm": "em", "foreignphrase": "em", "wordasword": "em",
        "glossterm": "em", "replaceable": "var", "superscript": "sup", "subscript": "sub", "abbrev": "abbr",
        "acronym": "abbr", "strong": "strong", "emph": "em"}
CODE = {"literal", "code", "command", "filename", "option", "userinput", "computeroutput", "varname", "function",
        "parameter", "classname", "methodname", "envar", "constant", "type", "systemitem", "prompt", "property",
        "returnvalue", "sgmltag", "tag", "token", "markup", "interfacename", "exceptionname", "structname",
        "structfield", "symbol", "errorcode", "errorname", "errortype", "keycode", "keysym", "uri", "package",
        "database", "literallayout-inline", "ooclass", "ooexception", "oointerface", "modifier", "initializer",
        "varargs", "void", "msgtext", "option-inline", "termdef"}
PLAIN = {"phrase", "application", "productname", "productnumber", "trademark", "orgname", "personname", "author",
         "firstname", "surname", "givenname", "othername", "honorific", "lineage", "affiliation", "jobtitle",
         "citation", "citerefentry", "refentrytitle", "manvolnum", "date", "pubdate", "year", "holder", "edition",
         "guilabel", "guibutton", "guiicon", "guimenu", "guisubmenu", "guimenuitem", "interface", "keycap", "mousebutton",
         "accel", "shortcut", "hardware", "medialabel", "lineannotation", "wordasword", "span", "textobject",
         "mathphrase", "corpauthor", "street", "city", "state", "postcode", "country", "phone", "fax", "otheraddr",
         "pob", "contrib", "authorinitials", "editor", "publishername", "biblioid", "isbn", "issn", "pagenums",
         "volumenum", "issuenum", "seriesvolnums", "citebiblioid", "bibliocoverage", "bibliorelation", "bibliosource",
         "bibliomisc", "bibliomset", "biblioset", "authorgroup", "collab", "collabname", "publisher", "copyright",
         "title", "personblurb", "address-inline", "replaceable-plain", "org", "orgdiv", "shortaffil", "label"}


# --- parsing -------------------------------------------------------------------------------------

_ENTITY_DECL = re.compile(r"""<!ENTITY\s+(%\s+)?([\w.:-]+)\s+(?:"([^"]*)"|'([^']*)'|(SYSTEM|PUBLIC)\b[^>]*)\s*>""")
_FAKE_DOCTYPE = '<!DOCTYPE docbook PUBLIC "-//bookport//no DTD is read//EN" "none.dtd">'


_PREFIXES = {"mml": markup.MATHML_NS, "xlink": markup.XLINK_NS, "xi": "http://www.w3.org/2001/XInclude",
             "db": "http://docbook.org/ns/docbook", "html": markup.XHTML_NS}


def _feed(body: str, entities: dict[str, str]) -> ET.Element:
    parser = markup.xml_parser()
    parser.entity.update(entities)
    parser.feed(body)
    return parser.close()


def _doctype_span(text: str) -> tuple[int, int] | None:
    """Where the DOCTYPE declaration is, with its internal subset (quotes and comments respected)."""
    start = text.find("<!DOCTYPE")
    if start < 0:
        return None
    i, n = start + 9, len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c in "\"'":
            end = text.find(c, i + 1)
            i = n if end < 0 else end + 1
            continue
        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            i = n if end < 0 else end + 3
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == ">" and depth <= 0:
            return start, i + 1
        i += 1
    return start, n


def parse(data: bytes | str, what: str, lost: list[str]) -> ET.Element:
    """A DocBook document as a tree with plain local names. Its DOCTYPE is replaced by one that names no
    DTD (nothing is ever loaded); the plain-text entities it declared are known to the parser."""
    text = markup.decode(data) if isinstance(data, bytes) else data
    entities: dict[str, str] = {}
    span = _doctype_span(text)
    if span:
        decl = text[span[0]:span[1]]
        if "[" in decl:
            for m in _ENTITY_DECL.finditer(decl):
                name = m.group(2)
                if m.group(1):
                    continue  # parameter entities only shape the DTD
                if m.group(5):
                    entities.setdefault(name, "")
                    lost.append(f"An external entity was left out: {name}")
                    continue
                value = m.group(3) if m.group(3) is not None else m.group(4)
                entities.setdefault(name, value)
            # an entity's text may use character references and the predefined entities, never other declared ones
            for name, value in entities.items():
                value = re.sub(r"<[^>]*>", "", value)
                entities[name] = html.unescape(value) if "&" in value else value
            grow = sum(text.count(f"&{k};") * len(v) for k, v in entities.items())
            if grow > MAX_ENTITY_TEXT:
                raise BookportError(f"{what} declares entities that would add {grow // (1024 * 1024):,} MB of text, "
                                    "more than bookport reads")
        body = text[:span[0]] + _FAKE_DOCTYPE + text[span[1]:]
    else:
        decl_end = 0
        m = re.match(r"\s*<\?xml[^>]*\?>", text)
        if m:
            decl_end = m.end()
        body = text[:decl_end] + _FAKE_DOCTYPE + text[decl_end:]
    markup.check_xml(body, None, what)
    try:
        root = _feed(body, entities)
    except ET.ParseError as e:
        # DocBook 4 files often use mml: or xlink: without declaring them; declare them on the root and try again
        m = re.search(r"<(?!\?|!)[\w.:-]+", body)
        if "unbound prefix" not in str(e) or m is None:
            raise BookportError(f"{what} isn't well-formed XML ({e})") from None
        extra = "".join(f' xmlns:{p}="{ns}"' for p, ns in _PREFIXES.items() if f"xmlns:{p}=" not in body[:m.end() + 4096])
        try:
            root = _feed(body[:m.end()] + extra + body[m.end():], entities)
        except ET.ParseError as e2:
            raise BookportError(f"{what} isn't well-formed XML ({e2})") from None
    markup.check_xml("", root, what)
    return markup._strip_ns(root)


def _rstrip(el: ET.Element) -> None:
    """No white space at the end of an element's text."""
    if len(el):
        if el[-1].tail:
            el[-1].tail = el[-1].tail.rstrip() or None
    elif el.text:
        el.text = el.text.rstrip()


def _rows(part: ET.Element) -> list[list[ET.Element]]:
    """The rows of a table head or body: CALS rows, HTML rows, or cells written straight into it."""
    rows: list[list[ET.Element]] = []
    loose: list[ET.Element] = []
    for child in part:
        if local(child) in ("row", "tr"):
            rows.append(list(child))
        elif local(child) in ("entry", "entrytbl", "td", "th"):
            loose.append(child)
    return ([loose] if loose else []) + rows


def local(el: ET.Element) -> str:
    return el.tag if isinstance(el.tag, str) else ""


def text_of(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return " ".join("".join(t for t in _itertext(el)).split())


def _itertext(el: ET.Element):
    """Text without index terms, footnotes and remarks."""
    stack = [(el, False)]
    while stack:
        node, tail = stack.pop()
        if tail:
            if node.tail:
                yield node.tail
            continue
        if node is not el:
            stack.append((node, True))
        if local(node) in ("indexterm", "footnote", "remark", "co", "anchor"):
            continue
        if node.text:
            yield node.text
        for child in reversed(node):
            stack.append((child, False))


def _title(el: ET.Element) -> ET.Element | None:
    t = el.find("title")
    if t is None:
        for info in el:
            if local(info).endswith("info") and info.find("title") is not None:
                return info.find("title")
    return t


def _id(el: ET.Element) -> str:
    return el.get("xml:id") or el.get("id") or ""


# --- reading -------------------------------------------------------------------------------------

class _Reader:
    def __init__(self, path: Path):
        self.base = path.parent
        self.top = path.parent.parent
        self.lost: list[str] = []
        self.unknown: set[str] = set()
        self.ids: dict[str, ET.Element] = {}
        self.notes: list[ET.Element] = []
        self.note_ids: dict[str, tuple[str, int]] = {}
        self.missing_pictures = 0
        self.quotes = 0
        self.includes = 0
        self.include_stack: list[Path] = [path.resolve()]

    # --- includes ---------------------------------------------------------------------------------
    def expand_includes(self, root: ET.Element, base: Path, depth: int = 0) -> None:
        """Replace each xi:include with what it names (files beside the document only)."""
        stack = [root]
        while stack:
            el = stack.pop()
            for i, child in reversed(list(enumerate(el))):  # from the end, so the positions stay right
                if local(child) != "include" or not child.get("href"):
                    stack.append(child)
                    continue
                new = self._include(child, base, depth)
                tail = child.tail or ""
                prev = el[i - 1] if i else None
                el.remove(child)
                if isinstance(new, ET.Element):
                    new.tail = tail
                    el.insert(i, new)
                    continue
                s = (new or "") + tail
                if prev is None:
                    el.text = (el.text or "") + s
                else:
                    prev.tail = (prev.tail or "") + s

    def _include(self, el: ET.Element, base: Path, depth: int) -> ET.Element | str | None:
        href = el.get("href", "")
        p = safe_path(base, href, self.top)
        if p is None:
            self.lost.append(f"An include outside the document's folder was left out: {href}")
            return self._fallback(el)
        if depth >= MAX_INCLUDE_DEPTH or p in self.include_stack or self.includes >= MAX_INCLUDES:
            self.lost.append(f"An include that would repeat itself (or nests too deep) was left out: {href}")
            return None
        try:
            data = p.read_bytes()
        except OSError:
            self.lost.append(f"An included file isn't there: {href}")
            return self._fallback(el)
        self.includes += 1
        if el.get("parse") == "text":
            return markup.decode(data)
        try:
            sub = parse(data, f"The included file {href}", self.lost)
        except BookportError as e:
            self.lost.append(str(e))
            return None
        self.include_stack.append(p)
        try:
            self.expand_includes(sub, p.parent, depth + 1)
        finally:
            self.include_stack.pop()
        return sub

    def _fallback(self, el: ET.Element) -> ET.Element | None:
        fb = next((c for c in el if local(c) == "fallback"), None)
        if fb is None:
            return None
        div = ET.Element("phrase")
        div.text = fb.text
        div.extend(list(fb))
        return div

    # --- the document --------------------------------------------------------------------------------
    def metadata(self, root: ET.Element) -> Metadata:
        md = Metadata()
        info = None
        for child in root:
            if local(child) in ("info", "bookinfo", "articleinfo", "chapterinfo", "setinfo", "partinfo", "refentryinfo"):
                info = child
                break
        title = _title(root)
        md.title = text_of(title)
        md.language = root.get("xml:lang") or root.get("lang") or ""
        if info is None:
            return md
        for a in info.iter():
            tag = local(a)
            if tag in ("author", "editor", "corpauthor", "othercredit"):
                name = self.person(a)
                if name:
                    (md.authors if tag in ("author", "corpauthor") else md.contributors).append(name)
        for tag in ("pubdate", "date"):
            d = info.find(f".//{tag}")
            if d is not None and text_of(d) and not md.date:
                md.date = text_of(d)
        pub = info.find(".//publishername")
        if pub is not None:
            md.publisher = text_of(pub)
        cr = info.find(".//copyright")
        if cr is not None:
            years = ", ".join(text_of(y) for y in cr.findall("year"))
            holders = ", ".join(text_of(h) for h in cr.findall("holder"))
            md.rights = " ".join(s for s in ("Copyright", years, holders) if s) if years or holders else ""
        ab = info.find("abstract")
        if ab is not None:
            md.description = text_of(ab)
        md.subjects = [text_of(k) for k in info.iter() if local(k) in ("keyword", "subjectterm") and text_of(k)]
        for tag in ("isbn", "issn"):
            v = info.find(f".//{tag}")
            if v is not None and text_of(v):
                md.identifiers[tag] = text_of(v)
        for b in info.iter("biblioid"):
            if b.get("class") and text_of(b):
                md.identifiers[b.get("class", "").lower()] = text_of(b)
        return md

    def person(self, a: ET.Element) -> str:
        pn = a.find("personname")
        src = pn if pn is not None else a
        parts = [text_of(src.find(t)) for t in ("honorific", "firstname", "givenname", "othername", "surname", "lineage")]
        name = " ".join(p for p in parts if p)
        if not name:
            org = src.find("orgname")
            name = text_of(org) if org is not None else text_of(src) if local(a) == "corpauthor" or pn is not None else ""
        return name

    def convert(self, root: ET.Element) -> str:
        for el in root.iter():
            i = _id(el)
            if i:
                self.ids.setdefault(i, el)
        body = ET.Element("body")
        if local(root) in DOC_ROOTS:
            self.children(root, body, 0)
        else:
            self.block(root, body, 0)
        if self.notes:
            body.extend(self.notes)
        if self.unknown:
            self.lost.append("DocBook elements bookport doesn't know were kept as plain text: "
                             + ", ".join(f"<{t}>" for t in sorted(self.unknown)))
        if self.missing_pictures:
            self.lost.append(f"{self.missing_pictures} picture{'s' if self.missing_pictures != 1 else ''} named in the "
                             "document aren't beside it, so only their descriptions were kept")
        out: list[str] = []
        for c in body:
            out.append(markup.to_string(c))
        return "".join(out)

    # --- blocks ------------------------------------------------------------------------------------
    def children(self, el: ET.Element, out: ET.Element, level: int, skip_title: bool = True) -> None:
        """The block content of an element: loose text and inline children gather into paragraphs."""
        para: ET.Element | None = None

        def inline_target() -> ET.Element:
            nonlocal para
            if para is None:
                para = ET.SubElement(out, "p")
            return para

        if el.text and el.text.strip():
            markup.append_text(inline_target(), el.text.lstrip())
        for child in el:
            tag = local(child)
            if tag in BLOCKS or tag in ("glossdef", "answer", "question", "listitem", "step"):
                if para is not None and para.text and not len(para):
                    para.text = para.text.rstrip()
                para = None
                self.block(child, out, level)
            elif tag and tag not in SKIP and not (skip_title and tag == "title"):
                self.inline(child, inline_target())
            if child.tail and (child.tail.strip() or para is not None):
                markup.append_text(inline_target(), child.tail if para is not None else child.tail.lstrip())
        if para is not None and not len(para) and not (para.text or "").strip():
            out.remove(para)

    def block(self, el: ET.Element, out: ET.Element, level: int, depth: int = 0) -> None:
        tag = local(el)
        if depth > MAX_DEPTH:
            markup.append_text(out, text_of(el))
            return
        eid = _id(el)
        if tag in SECTIONS:
            title = _title(el)
            if tag == "refentry" and title is None:
                title = el.find(".//refentrytitle")
            if title is not None or tag not in ("partintro", "index", "glossdiv", "bibliodiv"):
                h = ET.SubElement(out, f"h{min(level + 1, 6)}")
                if title is not None:
                    self.inline_children(title, h)
                elif tag in ("glossary", "bibliography", "index", "dedication", "colophon", "acknowledgements"):
                    h.text = tag.capitalize()
                if eid:
                    h.set("id", eid)
                if not len(h) and not (h.text or "").strip():
                    out.remove(h)
            if tag == "refentry":
                self.refentry(el, out, level + 1)
                return
            self.children(el, out, level + 1)
            return
        if tag in ("para", "simpara"):
            self.para(el, out, eid)
            return
        if tag == "formalpara":
            p = ET.SubElement(out, "p")
            title = el.find("title")
            if title is not None:
                s = ET.SubElement(p, "strong")
                self.inline_children(title, s)
                s.tail = " "
            inner = el.find("para")
            if inner is not None:
                self.inline_children(inner, p)
            if eid:
                p.set("id", eid)
            return
        if tag in ("literallayout", "address") and el.get("class") != "monospaced":
            # line breaks kept, but not code: a block of lines
            p = ET.SubElement(out, "div", {"class": "line-block"})
            if eid:
                p.set("id", eid)
            text = "".join(_itertext(el)).strip("\n")
            for k, line in enumerate(text.split("\n")):
                if k:
                    ET.SubElement(p, "br")
                markup.append_text(p, re.sub(r"^ +", lambda m: "\xa0" * len(m.group(0)), line))
            return
        if tag in VERBATIM:
            pre = ET.SubElement(out, "pre")
            code = ET.SubElement(pre, "code")
            lang = el.get("language", "")
            if lang:
                code.set("class", "language-" + re.sub(r"[^\w+#.-]", "", lang))
            code.text = "".join(_itertext(el)).strip("\n")
            if eid:
                pre.set("id", eid)
            return
        if tag in ("blockquote", "epigraph"):
            self.titled(el, out)
            bq = ET.SubElement(out, "blockquote")
            if eid:
                bq.set("id", eid)
            self.children(el, bq, level)
            attribution = el.find("attribution")
            if attribution is not None:
                p = ET.SubElement(bq, "p")
                p.text = "\N{EM DASH} "
                self.inline_children(attribution, p)
            return
        if tag in ADMONITIONS or tag in ("sidebar", "abstract", "example", "informalexample", "highlights", "legalnotice",
                                         "task", "screenshot"):
            div = ET.SubElement(out, "div", {"class": tag})
            if eid:
                div.set("id", eid)
            title = _title(el)
            if title is not None or tag in ADMONITIONS:
                t = ET.SubElement(div, "div", {"class": "title"})
                s = ET.SubElement(t, "strong")
                if title is not None:
                    self.inline_children(title, s)
                else:
                    s.text = tag.capitalize()
            self.children(el, div, level)
            return
        if tag in LISTS:
            self.list(el, out, level, depth)
            return
        if tag in ("table", "informaltable"):
            self.table(el, out, depth)
            return
        if tag in ("figure", "informalfigure", "mediaobject", "screenshot", "mediaobjectco"):
            self.figure(el, out)
            return
        if tag in ("equation", "informalequation"):
            maths = [c for c in el if local(c) == "math"] or [self.math(el, True)]
            maths = [m for m in maths if m is not None]
            if maths:
                p = ET.SubElement(out, "p")
                if eid:
                    p.set("id", eid)
                for m in maths:
                    m = copy.deepcopy(m) if m in el else m
                    m.tail = None
                    m.set("display", "block")
                    p.append(m)
            return
        if tag == "bridgehead":
            renders = {"sect1": 1, "sect2": 2, "sect3": 3, "sect4": 4, "sect5": 5}.get(el.get("renderas", ""), level + 1)
            h = ET.SubElement(out, f"h{min(max(renders, 1), 6)}")
            self.inline_children(el, h)
            if eid:
                h.set("id", eid)
            return
        if tag in ("biblioentry", "bibliomixed"):
            p = ET.SubElement(out, "p")
            if eid:
                p.set("id", eid)
            if tag == "bibliomixed":
                self.inline_children(el, p)
            else:
                p.text = ", ".join(t for t in (text_of(c) for c in el if local(c) not in SKIP) if t)
            return
        if tag == "glossentry":
            dl = ET.SubElement(out, "dl")
            self.glossentry(el, dl)
            return
        if tag in ("tr", "include"):
            return
        if tag in ("listitem", "step", "glossdef", "answer", "question", "div", "blockinfo", "revhistory", "msgset",
                   "productionset", "remark-block"):
            self.children(el, out, level)
            return
        self.unknown.add(tag)
        self.children(el, out, level)

    def titled(self, el: ET.Element, out: ET.Element) -> None:
        """A title of a list, quote or example, as a short bold paragraph before it."""
        title = el.find("title")
        if title is not None and text_of(title):
            p = ET.SubElement(out, "p")
            s = ET.SubElement(p, "strong")
            self.inline_children(title, s)

    def para(self, el: ET.Element, out: ET.Element, eid: str) -> None:
        """A paragraph, split around the lists, listings and tables DocBook allows inside one."""
        p = ET.SubElement(out, "p")
        if eid:
            p.set("id", eid)
        if el.text:
            markup.append_text(p, el.text)
        for child in el:
            tag = local(child)
            if tag in BLOCKS and tag != "simplelist" or tag == "simplelist" and child.get("type") != "inline":
                if not len(p) and not (p.text or "").strip():
                    out.remove(p)
                else:
                    _rstrip(p)
                self.block(child, out, 0)
                p = ET.SubElement(out, "p")
                if child.tail and child.tail.strip():
                    p.text = child.tail.lstrip()
                continue
            self.inline(child, p)
            if child.tail:
                markup.append_text(p, child.tail)
        if not len(p) and not (p.text or "").strip():
            out.remove(p)
        else:
            p.text = (p.text or "").lstrip() or None
            _rstrip(p)

    def list(self, el: ET.Element, out: ET.Element, level: int, depth: int) -> None:
        tag = local(el)
        self.titled(el, out)
        eid = _id(el)
        if tag in ("variablelist", "glosslist", "qandaset", "qandadiv", "segmentedlist"):
            dl = ET.SubElement(out, "dl")
            dls = [dl]
            if eid:
                dl.set("id", eid)
            for entry in el:
                kind = local(entry)
                if kind == "qandadiv":  # a group of questions with its own title: its entries, then on
                    self.list(entry, out, level, depth + 1)
                    dl = ET.SubElement(out, "dl")
                    dls.append(dl)
                elif kind == "varlistentry":
                    dt = ET.SubElement(dl, "dt")
                    terms = entry.findall("term")
                    for k, term in enumerate(terms):
                        if k:
                            markup.append_text(dt, ", ")
                        self.inline_children(term, dt)
                    if _id(entry):
                        dt.set("id", _id(entry))
                    for item in entry.findall("listitem"):
                        self.children(item, ET.SubElement(dl, "dd"), level)
                elif kind == "glossentry":
                    self.glossentry(entry, dl)
                elif kind == "qandaentry":
                    q, a = entry.find("question"), entry.find("answer")
                    if q is not None:
                        dt = ET.SubElement(dl, "dt")
                        dt.text = " ".join(text_of(c) for c in q if local(c) not in ("label",))
                    if a is not None:
                        self.children(a, ET.SubElement(dl, "dd"), level)
                elif kind == "seglistitem":
                    titles = [text_of(t) for t in el.findall("segtitle")]
                    for k, seg in enumerate(entry.findall("seg")):
                        dt = ET.SubElement(dl, "dt")
                        dt.text = titles[k] if k < len(titles) else ""
                        dd = ET.SubElement(dl, "dd")
                        self.inline_children(seg, dd)
            for d in dls:
                if not len(d) and not d.get("id"):
                    out.remove(d)
            return
        if tag == "simplelist":
            members = el.findall("member")
            if el.get("type") == "inline":
                p = ET.SubElement(out, "p")
                for k, m in enumerate(members):
                    if k:
                        markup.append_text(p, ", ")
                    self.inline_children(m, p)
                return
            ul = ET.SubElement(out, "ul")
            for m in members:
                self.inline_children(m, ET.SubElement(ul, "li"))
            return
        ordered = tag in ("orderedlist", "procedure", "substeps", "calloutlist")
        lst = ET.SubElement(out, "ol" if ordered else "ul")
        if eid:
            lst.set("id", eid)
        start = el.get("startingnumber", "")
        if ordered and start.isdecimal() and start != "1":
            lst.set("start", start)
        for item in el:
            if local(item) in ("listitem", "step", "callout", "biblioentry", "bibliomixed"):
                li = ET.SubElement(lst, "li")
                if _id(item):
                    li.set("id", _id(item))
                if local(item) in ("biblioentry", "bibliomixed"):
                    self.block(item, li, level, depth + 1)
                else:
                    self.children(item, li, level)
                if el.get("spacing") == "compact" and len(li) == 1 and li[0].tag == "p" and not li[0].get("id"):
                    p = li[0]  # a tight list: items without paragraphs
                    li.remove(p)
                    li.text = p.text
                    li.extend(list(p))

    def glossentry(self, entry: ET.Element, dl: ET.Element) -> None:
        dt = ET.SubElement(dl, "dt")
        term = entry.find("glossterm")
        if term is not None:
            self.inline_children(term, dt)
        if _id(entry):
            dt.set("id", _id(entry))
        for d in entry:
            kind = local(d)
            if kind == "glossdef":
                self.children(d, ET.SubElement(dl, "dd"), 0)
            elif kind == "glosssee":
                dd = ET.SubElement(dl, "dd")
                dd.text = "See " + (text_of(d) or self._xref_text(d.get("otherterm", "")))

    def refentry(self, el: ET.Element, out: ET.Element, level: int) -> None:
        name = el.find(".//refnamediv")
        if name is not None:
            h = ET.SubElement(out, f"h{min(level + 1, 6)}")
            h.text = "Name"
            p = ET.SubElement(out, "p")
            p.text = ", ".join(text_of(n) for n in name.findall("refname"))
            purpose = name.find("refpurpose")
            if purpose is not None:
                p.text += " \N{EM DASH} "
                self.inline_children(purpose, p)
        for child in el:
            if local(child) in ("refmeta", "refnamediv") or local(child) in SKIP:
                continue
            if local(child) == "refsynopsisdiv" and _title(child) is None:
                h = ET.SubElement(out, f"h{min(level + 1, 6)}")
                h.text = "Synopsis"
                self.children(child, out, level + 1)
                continue
            self.block(child, out, level)

    # --- tables ------------------------------------------------------------------------------------
    def table(self, el: ET.Element, out: ET.Element, depth: int) -> None:
        table = ET.SubElement(out, "table")
        if _id(el):
            table.set("id", _id(el))
        title = el.find("title")
        if title is None:
            title = el.find("caption")
        if title is not None:
            self.inline_children(title, ET.SubElement(table, "caption"))
        groups = el.findall("tgroup")
        if not groups:  # an HTML table
            for part in el:
                kind = local(part)
                if kind in ("thead", "tbody", "tfoot"):
                    sect = ET.SubElement(table, kind)
                    for tr in part.findall("tr"):
                        self.html_row(tr, sect, depth)
                elif kind == "tr":
                    self.html_row(part, table, depth)
            return
        for group in groups:
            cols: dict[str, int] = {}
            n = 0
            for spec in group.findall("colspec"):
                n = markup.whole(spec.get("colnum")) or n + 1
                if spec.get("colname"):
                    cols[spec.get("colname", "")] = n
            spans = {s.get("spanname", ""): (s.get("namest", ""), s.get("nameend", "")) for s in group.findall("spanspec")}
            for kind in ("thead", "tbody", "tfoot"):
                part = group.find(kind)
                if part is None:
                    continue
                sect = ET.SubElement(table, kind)
                for row in _rows(part):
                    tr = ET.SubElement(sect, "tr")
                    for entry in row:
                        if local(entry) not in ("entry", "entrytbl", "td", "th"):
                            continue
                        cell = ET.SubElement(tr, "th" if kind == "thead" or local(entry) == "th" else "td")
                        st, en = entry.get("namest", ""), entry.get("nameend", "")
                        if entry.get("spanname") in spans:
                            st, en = spans[entry.get("spanname", "")]
                        if st in cols and en in cols and cols[en] > cols[st]:
                            cell.set("colspan", str(min(cols[en] - cols[st] + 1, 50)))
                        more = entry.get("morerows", "")
                        if (markup.whole(more) or 0) > 0:
                            cell.set("rowspan", str(min(markup.whole(more) + 1, 500)))
                        align = entry.get("align", "")
                        if align in ("left", "right", "center", "justify"):
                            cell.set("style", f"text-align: {align}")
                        if local(entry) == "entrytbl" and depth < 16:
                            self.table(entry, cell, depth + 1)
                        else:
                            self.cell(entry, cell)

    def html_row(self, tr: ET.Element, parent: ET.Element, depth: int) -> None:
        row = ET.SubElement(parent, "tr")
        for c in tr:
            if local(c) in ("td", "th"):
                cell = ET.SubElement(row, local(c))
                for a in ("colspan", "rowspan"):
                    if (c.get(a) or "").isdecimal():
                        cell.set(a, c.get(a, ""))
                self.cell(c, cell)

    def cell(self, entry: ET.Element, cell: ET.Element) -> None:
        if any(local(c) in BLOCKS for c in entry):
            self.children(entry, cell, 0)
        else:
            self.inline_children(entry, cell)

    # --- pictures and math ---------------------------------------------------------------------------
    def figure(self, el: ET.Element, out: ET.Element) -> None:
        objects = [el] if local(el) in ("mediaobject", "inlinemediaobject") else list(el.iter("mediaobject")) or [el]
        title = el.find("title")
        if title is None:
            title = el.find("caption")
            if title is None and objects:
                title = objects[0].find("caption")
        fig = ET.SubElement(out, "figure")
        if _id(el):
            fig.set("id", _id(el))
        for obj in objects:
            img = self.image(obj)
            if img is not None:
                fig.append(img)
        if title is not None and text_of(title):
            cap = ET.SubElement(fig, "figcaption")
            if local(title) == "caption":
                cap.text = text_of(title)
            else:
                self.inline_children(title, cap)
        if not len(fig):
            out.remove(fig)
        elif fig.find("img") is None:
            alt = " ".join(o.get("alt", "") for o in objects if o.get("alt"))
            if alt:
                p = ET.SubElement(fig, "figcaption")
                p.text = alt

    def image(self, obj: ET.Element) -> ET.Element | None:
        """The best picture a media object offers, or its text when the picture isn't here."""
        best = None
        for io in obj.iter("imageobject"):
            data = io.find("imagedata")
            if data is None or not data.get("fileref"):
                continue
            ref = data.get("fileref", "")
            ext = ref.rsplit(".", 1)[-1].lower()
            score = 2 if ext in ("png", "jpg", "jpeg", "gif", "svg", "webp") else 1
            score += 1 if io.get("role") in ("html", "epub", "xhtml") else 0
            if best is None or score > best[0]:
                best = (score, ref)
        alt = " ".join(text_of(t) for t in obj.iter("textobject") if t.find("phrase") is not None or text_of(t))
        alt = alt or text_of(obj.find("alt"))
        if best is None:
            if alt:
                img = ET.Element("span")
                img.text = alt
                return img
            return None
        ref = best[1]
        if is_external(ref) and not ref.startswith("file:"):
            return ET.Element("img", {"src": ref, "alt": alt})
        src = local_picture(self.base, ref, self.lost)
        if src is None:
            self.missing_pictures += 1
            if alt:
                obj.set("alt", alt)
            return None
        return ET.Element("img", {"src": src, "alt": alt})

    def math(self, el: ET.Element, display: bool) -> ET.Element | None:
        m = next((c for c in el.iter() if local(c) == "math"), None)
        if m is not None:
            m = copy.deepcopy(m)
            m.tail = None
            m.set("display", "block" if display else "inline")
            return m
        for c in el.iter():
            if local(c) in ("alt", "tex", "texmath", "mathphrase") and (local(c) != "mathphrase" or c.get("role") in ("tex", "latex")):
                tex = text_of(c)
                tex = re.sub(r"^\s*(\$\$?|\\\(|\\\[)|(\$\$?|\\\)|\\\])\s*$", "", tex).strip()
                if tex:
                    return mathml.from_tex(tex, display=display)
        phrase = el.find("mathphrase")
        if phrase is not None:
            span = ET.Element("span", {"class": "math"})
            self.inline_children(phrase, span)
            return span
        return None

    # --- inline --------------------------------------------------------------------------------------
    def inline_children(self, el: ET.Element, out: ET.Element) -> None:
        if el.text:
            markup.append_text(out, el.text)
        for child in el:
            self.inline(child, out)
            if child.tail:
                markup.append_text(out, child.tail)

    def inline(self, el: ET.Element, out: ET.Element, depth: int = 0) -> None:
        tag = local(el)
        if not tag or tag in SKIP:
            return
        if depth > MAX_DEPTH:
            markup.append_text(out, text_of(el))
            return
        eid = _id(el)
        if tag in EMPH:
            new = EMPH[tag]
            if tag == "emphasis":
                role = el.get("role", "").lower()
                new = {"bold": "strong", "strong": "strong", "underline": "u", "strikethrough": "del",
                       "smallcaps": "span"}.get(role, "em")
            node = ET.SubElement(out, new)
            if new == "span":
                node.set("class", "smallcaps")
            self.inline_children(el, node)
        elif tag in CODE:
            node = ET.SubElement(out, "code")
            node.text = "".join(_itertext(el))
        elif tag in ("keycombo", "menuchoice"):
            parts = [text_of(c) for c in el if local(c) not in SKIP and text_of(c)]
            markup.append_text(out, ("+" if tag == "keycombo" else " > ").join(parts) or text_of(el))
        elif tag == "quote":
            self.quotes += 1
            inner = self.quotes % 2 == 0  # quotes inside quotes take single marks
            markup.append_text(out, "\N{LEFT SINGLE QUOTATION MARK}" if inner else "\N{LEFT DOUBLE QUOTATION MARK}")
            self.inline_children(el, out)
            markup.append_text(out, "\N{RIGHT SINGLE QUOTATION MARK}" if inner else "\N{RIGHT DOUBLE QUOTATION MARK}")
            self.quotes -= 1
        elif tag == "citerefentry":
            self.inline_children(el.find("refentrytitle") if el.find("refentrytitle") is not None else el, out)
            vol = el.find("manvolnum")
            if vol is not None:
                markup.append_text(out, f"({text_of(vol)})")
        elif tag == "trademark":
            self.inline_children(el, out)
            markup.append_text(out, "\N{REGISTERED SIGN}" if el.get("class") == "registered" else "\N{TRADE MARK SIGN}")
        elif tag in ("ulink", "link", "uri", "email", "olink"):
            url = el.get("url") or el.get("xlink:href") or ""
            if tag == "email":
                url = "mailto:" + text_of(el)
            if tag == "link" and el.get("linkend"):
                url = "#" + el.get("linkend", "")
            if tag == "uri" and not url:
                url = text_of(el)
            if not url or tag == "olink":
                self.inline_children(el, out)
                return
            a = ET.SubElement(out, "a", {"href": url})
            self.inline_children(el, a)
            if not len(a) and not (a.text or "").strip():
                a.text = self._xref_text(el.get("linkend", "")) if el.get("linkend") else url.removeprefix("mailto:")
        elif tag == "xref":
            target = el.get("linkend", "")
            a = ET.SubElement(out, "a", {"href": "#" + target})
            a.text = self._xref_text(target, el.get("endterm", ""))
        elif tag == "anchor":
            if eid:
                ET.SubElement(out, "a", {"id": eid})
            return
        elif tag == "footnote":
            self.footnote(el, out)
            return
        elif tag == "footnoteref":
            known = self.note_ids.get(el.get("linkend", ""))
            if known:
                a = ET.SubElement(out, "a", {"class": "noteref", "epub:type": "noteref", "href": f"#{known[0]}"})
                ET.SubElement(a, "sup").text = str(known[1])
            return
        elif tag == "inlineequation":
            m = self.math(el, False)
            if m is not None:
                out.append(m)
            return
        elif tag in ("inlinemediaobject", "inlinegraphic", "graphic"):
            if tag != "inlinemediaobject":
                obj = ET.Element("mediaobject")
                io = ET.SubElement(obj, "imageobject")
                ET.SubElement(io, "imagedata", {"fileref": el.get("fileref", "")})
                el = obj
            img = self.image(el)
            if img is not None:
                out.append(img)
            elif el.get("alt"):
                markup.append_text(out, el.get("alt", ""))
            return
        elif tag == "citation":
            markup.append_text(out, "[")
            self.inline_children(el, out)
            markup.append_text(out, "]")
        elif tag in ("author", "editor", "personname", "othercredit"):
            markup.append_text(out, self.person(el) or text_of(el))
        elif tag in PLAIN or tag == "title":
            if eid:
                ET.SubElement(out, "a", {"id": eid})
            self.inline_children(el, out)
        elif tag in BLOCKS or tag in ("listitem", "entry", "member"):
            markup.append_text(out, " " + text_of(el) + " ")
        else:
            self.unknown.add(tag)
            self.inline_children(el, out)

    def footnote(self, el: ET.Element, out: ET.Element) -> None:
        num = len(self.notes) + 1
        nid = _id(el) or f"fn{num}"
        self.note_ids[nid] = (nid, num)
        a = ET.SubElement(out, "a", {"class": "noteref", "epub:type": "noteref", "href": f"#{nid}"})
        ET.SubElement(a, "sup").text = str(num)
        aside = ET.Element("aside", {"epub:type": "footnote", "id": nid})
        self.notes.append(aside)
        self.children(el, aside, 0)

    def _xref_text(self, target: str, endterm: str = "") -> str:
        if endterm and endterm in self.ids:
            return text_of(self.ids[endterm])
        el = self.ids.get(target)
        if el is None:
            return target
        if el.get("xreflabel"):
            return el.get("xreflabel", "")
        title = _title(el)
        if title is not None and text_of(title):
            return text_of(title)
        if local(el) in ("cmdsynopsis", "funcsynopsis"):
            name = el.find(".//command") if local(el) == "cmdsynopsis" else el.find(".//function")
            if name is not None:
                return text_of(name)
        if local(el) in ("varlistentry",):
            return text_of(el.find("term"))
        if local(el) == "glossentry":
            return text_of(el.find("glossterm"))
        return text_of(el)[:80] or target


def read(path) -> Book:
    p = Path(path)
    r = _Reader(p)
    root = parse(p.read_bytes(), f"The DocBook file {p.name}", r.lost)
    r.expand_includes(root, p.parent)
    if markup.too_deep(root):
        raise BookportError(f"The DocBook file {p.name} nests elements more than {MAX_DEPTH} deep")
    md = r.metadata(root)
    body = r.convert(root)
    return html_book(body, p.stem.replace("_", " "), "dbk", p.parent, r.lost, md)


# --- writing -------------------------------------------------------------------------------------

_BLOCK_START = re.compile(r"<(?:para|programlisting|itemizedlist|orderedlist|variablelist|blockquote|informaltable|table|"
                          r"figure|informalfigure|bridgehead|literallayout|screen|note|equation|informalequation|"
                          r"simpara|section|mediaobject)\b")
_NESTED = {"li", "dd", "td", "th", "blockquote", "aside", "figure", "dt"}


def _xml(s: str) -> str:
    return markup.clean_text(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xattr(s: str) -> str:
    return _xml(s).replace('"', "&quot;")


class _Writer(TextWriter):
    def __init__(self, book: Book):
        super().__init__(book)
        self.nested = 0
        self.tight: list[bool] = []

    def blocks(self, el: ET.Element) -> str:
        inside = el.tag in _NESTED
        self.nested += inside
        try:
            return super().blocks(el)
        finally:
            self.nested -= inside

    def write_book(self) -> str:
        text = super().write_book()
        # headings become nested <section>s
        out: list[str] = []
        open_levels: list[int] = []
        pos = 0
        for m in re.finditer(r"\x00S(\d)\x00", text):
            out.append(text[pos:m.start()])
            level = int(m.group(1))
            while open_levels and open_levels[-1] >= level:
                open_levels.pop()
                out.append("</section>\n")
            open_levels.append(level)
            out.append("<section")
            pos = m.end()
        out.append(text[pos:])
        body = "".join(out).rstrip("\n")
        tail = "\n".join("</section>" for _ in open_levels)
        return body.replace("\x00END\x00", (tail + "\n" if tail else "") + "</article>") + "\n"

    def join_document(self, parts: list[str]) -> str:
        return "\n".join(p.strip("\n") for p in parts)

    def preamble(self) -> str:
        md = self.book.metadata
        info = [f"<title>{_xml(md.title or 'Untitled')}</title>"]
        for a in md.authors:
            info.append(f"<author><personname>{_xml(a)}</personname></author>")
        if md.date:
            info.append(f"<date>{_xml(md.date)}</date>")
        if md.publisher:
            info.append(f"<publisher><publishername>{_xml(md.publisher)}</publishername></publisher>")
        if md.description:
            info.append(f"<abstract><para>{_xml(re.sub(r'<[^>]*>', '', md.description))}</para></abstract>")
        lang = f' xml:lang="{_xattr(md.language)}"' if md.language else ""
        return ('<?xml version="1.0" encoding="utf-8"?>\n'
                '<article xmlns="http://docbook.org/ns/docbook" xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'xmlns:mml="http://www.w3.org/1998/Math/MathML" version="5.0"{lang}>\n'
                "<info>\n" + "\n".join(info) + "\n</info>")

    def postamble(self) -> str:
        return "\x00END\x00"

    def escape(self, s: str) -> str:
        return _xml(s)

    def _id(self, anchor: str | None) -> str:
        return f' xml:id="{anchor}"' if anchor else ""

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        return f"<para{self._id(anchor)}>{text}</para>"

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        if self.nested:
            return f"<bridgehead{self._id(anchor)}>{text}</bridgehead>"
        return f"\x00S{min(max(level, 1), 6)}\x00{self._id(anchor)}>\n<title>{text}</title>"

    def rule(self) -> str:
        return ""

    def code_block(self, code: str, lang: str) -> str:
        attr = f' language="{_xattr(lang)}"' if lang else ""
        return f"<programlisting{attr}>{_xml(code)}</programlisting>"

    def raw_block(self, text: str, fmt: str) -> str:
        return text if fmt in ("docbook", "dbk") else self.code_block(text, "")

    def raw_inline(self, text: str, fmt: str) -> str:
        return text if fmt in ("docbook", "dbk") else self.code(text)

    def quote(self, inner: str) -> str:
        return f"<blockquote>\n{self._blocks(inner)}\n</blockquote>"

    def _blocks(self, s: str) -> str:
        """Content that must be blocks: loose inline text goes in a paragraph."""
        s = s.strip()
        if not s:
            return "<para></para>"
        return s if _BLOCK_START.match(s) else self.paragraph(s)

    def list_item(self, li: ET.Element) -> str:
        return self.blocks(li) if self.has_blocks(li) else self.paragraph(self.inlines(li).strip())

    def block(self, el: ET.Element) -> str:
        if el.tag in ("ul", "ol"):
            # a list whose items are one line of text each is a compact one
            self.tight.append(all(not self.has_blocks(li) for li in el if li.tag == "li"))
            try:
                return super().block(el)
            finally:
                self.tight.pop()
        return super().block(el)

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        body = "\n".join(f"<listitem>{self._blocks(it)}</listitem>" for it in items)
        spacing = ' spacing="compact"' if self.tight and self.tight[-1] else ""
        if ordered:
            attr = f' startingnumber="{start}"' if start != 1 else ""
            return f"<orderedlist{attr}{spacing}>\n{body}\n</orderedlist>"
        return f"<itemizedlist{spacing}>\n{body}\n</itemizedlist>"

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        out = ["<variablelist>"]
        for term, defs in pairs:
            out.append(f"<varlistentry><term>{term}</term><listitem>"
                       + "".join(self._blocks(d) for d in defs or [""]) + "</listitem></varlistentry>")
        out.append("</variablelist>")
        return "\n".join(out)

    def table(self, table: Table) -> str:
        width = max(table.width, 1)
        busy: dict[tuple[int, int], bool] = {}
        rows_out = []
        for r, row in enumerate(table.rows):
            col = 0
            cells = []
            for c in row:
                while busy.get((r, col)):
                    col += 1
                attrs = ""
                if c.colspan > 1:
                    attrs += f' namest="c{col + 1}" nameend="c{min(col + c.colspan, width)}"'
                if c.rowspan > 1:
                    attrs += f' morerows="{c.rowspan - 1}"'
                if c.align in ("left", "right", "center", "justify"):
                    attrs += f' align="{c.align}"'
                for dr in range(c.rowspan):
                    for dc in range(c.colspan):
                        busy[(r + dr, col + dc)] = True
                col += c.colspan
                text = c.text
                cells.append(f"<entry{attrs}>{self._blocks(text) if chr(10) in text else text}</entry>")
            rows_out.append("<row>" + "".join(cells) + "</row>")
        specs = "".join(f'<colspec colname="c{k + 1}"/>' for k in range(width))
        head = "".join(rows_out[:table.header_rows])
        body = "".join(rows_out[table.header_rows:]) or "<row>" + "<entry></entry>" * width + "</row>"
        group = (f'<tgroup cols="{width}">{specs}' + (f"<thead>{head}</thead>" if head else "")
                 + f"<tbody>{body}</tbody></tgroup>")
        if table.caption:
            return f"<table>\n<title>{table.caption}</title>\n{group}\n</table>"
        return f"<informaltable>\n{group}\n</informaltable>"

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        if not src:
            return self.paragraph(caption) if caption else ""
        obj = self._media("mediaobject", src, alt or caption)
        if caption:
            return f"<figure{self._id(anchor)}>\n<title>{caption}</title>\n{obj}\n</figure>"
        return f"<informalfigure{self._id(anchor)}>\n{obj}\n</informalfigure>"

    def _media(self, tag: str, src: str, alt: str) -> str:
        text = f"<textobject><phrase>{_xml(re.sub(r'<[^>]*>', '', alt))}</phrase></textobject>" if alt else ""
        return f'<{tag}><imageobject><imagedata fileref="{_xattr(src)}"/></imageobject>{text}</{tag}>'

    def image(self, src: str, alt: str) -> str:
        return self._media("inlinemediaobject", src, alt)

    def _mathml(self, el: ET.Element) -> str:
        m = copy.deepcopy(el)
        m.tail = None  # the text after the equation is written after it, once
        for node in m.iter():
            if isinstance(node.tag, str):
                node.tag = "mml:" + node.tag
            node.attrib.pop("xmlns", None)
        m.attrib.pop("display", None)
        return markup.to_string(m)

    def math_block(self, el: ET.Element) -> str:
        return f"<informalequation>{self._mathml(el)}</informalequation>"

    def math_inline(self, el: ET.Element) -> str:
        return f"<inlineequation>{self._mathml(el)}</inlineequation>"

    def footnote(self, body: str, note_id: str) -> str:
        return f"<footnote>{self._blocks(body)}</footnote>"

    def line_break(self) -> str:
        return "\n"

    def inline_anchor(self, key: str) -> str:
        return f'<anchor xml:id="{key}"/>'

    def emph(self, s: str) -> str:
        return f"<emphasis>{s}</emphasis>"

    def strong(self, s: str) -> str:
        return f'<emphasis role="strong">{s}</emphasis>'

    def strike(self, s: str) -> str:
        return f'<emphasis role="strikethrough">{s}</emphasis>'

    def underline(self, s: str) -> str:
        return f'<emphasis role="underline">{s}</emphasis>'

    def sup(self, s: str) -> str:
        return f"<superscript>{s}</superscript>"

    def sub(self, s: str) -> str:
        return f"<subscript>{s}</subscript>"

    def smallcaps(self, s: str) -> str:
        return f'<emphasis role="smallcaps">{s}</emphasis>'

    def code(self, s: str) -> str:
        return f"<literal>{_xml(s)}</literal>"

    def link(self, target: str, text: str, external: bool) -> str:
        if external:
            return f'<link xlink:href="{_xattr(target)}">{text}</link>'
        return f'<link linkend="{target}">{text}</link>'

    def citation(self, keys: list[str], shown: str) -> str:
        return f"<citation>{shown}</citation>" if shown.strip() else ""


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    return write_text(_Writer(book), path, pictures)
