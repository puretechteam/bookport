"""Parse HTML and XHTML into ElementTree elements, and write them back as XHTML.

Elements use plain local names ("p", "svg", "image"); namespaced attributes keep their usual
prefixes ("xml:lang", "epub:type", "xlink:href"). Well-formed XHTML goes through expat, with the
HTML entities predefined; anything else goes through a forgiving HTML parser that closes tags
the way browsers do.
"""

from __future__ import annotations

import html
import html.entities
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from .errors import BookportError
from .limits import MAX_DEPTH

XHTML_NS = "http://www.w3.org/1999/xhtml"
SVG_NS = "http://www.w3.org/2000/svg"
MATHML_NS = "http://www.w3.org/1998/Math/MathML"
XLINK_NS = "http://www.w3.org/1999/xlink"
EPUB_NS = "http://www.idpf.org/2007/ops"
XML_NS = "http://www.w3.org/XML/1998/namespace"

PREFIXES = {XLINK_NS: "xlink", EPUB_NS: "epub", XML_NS: "xml"}

VOID = frozenset(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"])
BLOCK = frozenset(
    ["address", "article", "aside", "blockquote", "body", "center", "dd", "details", "dialog", "dir", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "html", "li", "main", "menu", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "caption"]
)
# Tags whose start closes an open <p>
CLOSES_P = BLOCK - {"body", "html", "li", "dd", "dt", "td", "th", "tr", "tbody", "thead", "tfoot", "caption"}
RAW_TEXT = frozenset({"script", "style"})

# html.parser lowercases names; SVG needs its camelCase ones back (the HTML5 spec's adjustment tables)
SVG_TAGS = {t.lower(): t for t in ["altGlyph", "altGlyphDef", "altGlyphItem", "animateColor", "animateMotion", "animateTransform", "clipPath", "feBlend", "feColorMatrix", "feComponentTransfer", "feComposite", "feConvolveMatrix", "feDiffuseLighting", "feDisplacementMap", "feDistantLight", "feDropShadow", "feFlood", "feFuncA", "feFuncB", "feFuncG", "feFuncR", "feGaussianBlur", "feImage", "feMerge", "feMergeNode", "feMorphology", "feOffset", "fePointLight", "feSpecularLighting", "feSpotLight", "feTile", "feTurbulence", "foreignObject", "glyphRef", "linearGradient", "radialGradient", "textPath"]}
SVG_ATTRS = {a.lower(): a for a in ["attributeName", "attributeType", "baseFrequency", "baseProfile", "calcMode", "clipPathUnits", "diffuseConstant", "edgeMode", "filterUnits", "glyphRef", "gradientTransform", "gradientUnits", "kernelMatrix", "kernelUnitLength", "keyPoints", "keySplines", "keyTimes", "lengthAdjust", "limitingConeAngle", "markerHeight", "markerUnits", "markerWidth", "maskContentUnits", "maskUnits", "numOctaves", "pathLength", "patternContentUnits", "patternTransform", "patternUnits", "pointsAtX", "pointsAtY", "pointsAtZ", "preserveAlpha", "preserveAspectRatio", "primitiveUnits", "refX", "refY", "repeatCount", "repeatDur", "requiredExtensions", "requiredFeatures", "specularConstant", "specularExponent", "spreadMethod", "startOffset", "stdDeviation", "stitchTiles", "surfaceScale", "systemLanguage", "tableValues", "targetX", "targetY", "textLength", "viewBox", "viewTarget", "xChannelSelector", "yChannelSelector", "zoomAndPan"]}

_ENTITIES = {k[:-1]: v for k, v in html.entities.html5.items() if k.endswith(";")}


def _local(name: str) -> str:
    if name[:1] == "{":
        ns, _, local = name[1:].partition("}")
        prefix = PREFIXES.get(ns)
        return f"{prefix}:{local}" if prefix else local
    return name


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str):
            el.tag = _local(el.tag)
        if el.attrib:
            el.attrib = {_local(k): v for k, v in el.attrib.items() if not k.startswith("xmlns")}
    # SVG written in lowercase by HTML tools gets its camelCase names back
    for svg in root.iter("svg"):
        for el in svg.iter():
            if isinstance(el.tag, str):
                el.tag = SVG_TAGS.get(el.tag, el.tag)
            if el.attrib:
                el.attrib = {SVG_ATTRS.get(k, k): v for k, v in el.attrib.items()}
    return root


_DECL = re.compile(rb"^\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)", re.IGNORECASE)
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9._-]+)", re.IGNORECASE)


def decode(data: bytes | str, default: str = "utf-8") -> str:
    """Bytes to text, by BOM, XML declaration or <meta charset>, then UTF-8, then Windows-1252."""
    if isinstance(data, str):
        return data
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    head = data[:2048]
    m = _DECL.match(head) or _META_CHARSET.search(head)
    names = [m.group(1).decode("ascii").lower()] if m else []
    for enc in names + [default, "utf-8"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1252", "replace")


_XML_DECL_STR = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


def parse(data: bytes | str) -> ET.Element:
    """Parse an XHTML or HTML document; always returns an <html> element with <head> and <body>."""
    text = decode(data)
    root = _parse_xml(text) if "<html" in text[:4096].lower() else None
    if root is None:
        root = HtmlBuilder().build(text)
    return _ensure_shell(root)


def _parse_xml(text: str) -> ET.Element | None:
    if has_internal_subset(text):
        return None  # the HTML parser reads it without expanding the entities
    parser = xml_parser()
    body = _XML_DECL_STR.sub("", text, count=1)
    try:
        parser.feed(body)
        root = parser.close()
    except ET.ParseError:
        return None
    if too_deep(root):
        return None  # the HTML parser keeps the text and caps the nesting
    root = _strip_ns(root)
    return root if root.tag == "html" else None


# a DOCTYPE with an internal subset ("[...]") is the only way a document can declare entities
_INTERNAL_SUBSET = re.compile(r"""<!DOCTYPE(?:[^>\["']|"[^"]*"|'[^']*')*\[""", re.IGNORECASE)


def has_internal_subset(text: str | bytes) -> bool:
    if isinstance(text, bytes):
        text = text.decode("latin-1")
    return _INTERNAL_SUBSET.search(text) is not None


def xml_parser() -> ET.XMLParser:
    """An expat parser that knows the HTML entities (books use &nbsp; and friends in XML files)."""
    parser = ET.XMLParser()
    parser.entity.update(_ENTITIES)
    return parser


def too_deep(root: ET.Element, limit: int = MAX_DEPTH) -> bool:
    stack = [(root, 0)]
    pop, push = stack.pop, stack.append
    while stack:
        el, d = pop()
        if len(el):
            if d >= limit:
                return True
            for c in el:
                push((c, d + 1))
    return False


def check_xml(text: str | bytes, root: ET.Element | None, what: str) -> None:
    """Refuse XML data files that declare entities or nest deeper than MAX_DEPTH."""
    if has_internal_subset(text):
        raise BookportError(f"{what} declares its own entities (a DOCTYPE internal subset), which bookport refuses")
    if root is not None and too_deep(root):
        raise BookportError(f"{what} nests elements more than {MAX_DEPTH} deep")


def parse_xml(data: str | bytes, what: str = "An XML file") -> ET.Element:
    """Parse an XML data file (package, index, document part): HTML entities known, no internal
    subset, bounded nesting. Raises ET.ParseError when it isn't well-formed."""
    check_xml(data, None, what)
    parser = xml_parser()
    parser.feed(data)
    root = parser.close()
    check_xml("", root, what)
    return root


def parse_fragment(text: str) -> list:
    """Parse HTML that isn't a whole document; returns the list of top-level nodes inside <body>
    (strings for text, elements otherwise) as ``(leading text, [elements])``."""
    root = HtmlBuilder().build(f"<body>{text}</body>")
    body = _ensure_shell(root).find("body")
    return [body.text or "", list(body)]


def _ensure_shell(root: ET.Element) -> ET.Element:
    if root.tag != "html":
        html_el = ET.Element("html")
        body = ET.SubElement(html_el, "body")
        body.append(root)
        root = html_el
    head = root.find("head")
    if head is None:
        head = ET.Element("head")
        root.insert(0, head)
    if root.find("body") is None:
        body = ET.SubElement(root, "body")
        for child in list(root):
            if child is not head and child is not body:
                root.remove(child)
                body.append(child)
    return root


class HtmlBuilder(HTMLParser):
    """A forgiving HTML parser: implicit end tags, stray end tags ignored, entities decoded."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("html")
        self.stack = [self.root]
        self.in_svg = 0

    def build(self, text: str) -> ET.Element:
        text = _XML_DECL_STR.sub("", text, count=1)
        text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", lambda m: html.escape(m.group(1), quote=False), text, flags=re.DOTALL)
        self.feed(text)
        self.close()
        root = self.root
        # <html> start tags only merge their attributes into the root
        return root

    # helpers -------------------------------------------------------------
    @property
    def top(self) -> ET.Element:
        return self.stack[-1]

    def _append_text(self, text: str) -> None:
        top = self.top
        if len(top):
            last = top[-1]
            last.tail = (last.tail or "") + text
        else:
            top.text = (top.text or "") + text

    def _close_to(self, tag: str) -> bool:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return True
            if self.stack[i].tag in ("table", "body") and tag not in ("table", "body", "html"):
                return False
        return False

    def _open_in_scope(self, tags, stop=("table", "ul", "ol", "dl", "body")) -> str | None:
        for el in reversed(self.stack[1:]):
            if el.tag in tags:
                return el.tag
            if el.tag in stop:
                return None
        return None

    # HTMLParser callbacks ------------------------------------------------
    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag, attrs, closed):
        if ":" in tag:
            tag = tag.split(":", 1)[1]
        if self.in_svg or tag == "svg":
            tag = SVG_TAGS.get(tag, tag)
            attrs = [(SVG_ATTRS.get(k, k), v) for k, v in attrs]
        attrib = {k: ("" if v is None else v) for k, v in attrs if k and not k.startswith("xmlns")}
        if tag == "html":
            self.root.attrib.update(attrib)
            return
        if tag in ("head", "body"):
            el = self.root.find(tag)
            if el is None:
                if tag == "body" and self.root.find("head") is None:
                    ET.SubElement(self.root, "head")
                el = ET.SubElement(self.root, tag)
            el.attrib.update(attrib)
            self.stack = [self.root, el]
            return
        if len(self.stack) == 1:
            # content before <body>: head-only tags go in <head>, the rest starts the body
            if tag in ("title", "meta", "link", "style", "script", "base") and self.root.find("body") is None:
                head = self.root.find("head")
                if head is None:
                    head = ET.SubElement(self.root, "head")
                self.stack.append(head)
            else:
                self._start("body", [], closed=False)
        elif self.top.tag == "head" and tag not in ("title", "meta", "link", "style", "script", "base", "noscript"):
            self._start("body", [], closed=False)
        if not self.in_svg:
            if tag in CLOSES_P and self._open_in_scope({"p"}):
                self._close_to("p")
            if tag == "li" and self._open_in_scope({"li"}, stop=("ul", "ol", "table", "body")):
                self._close_to("li")
            if tag in ("dt", "dd") and self._open_in_scope({"dt", "dd"}, stop=("dl", "table", "body")):
                self._close_to(self._open_in_scope({"dt", "dd"}))
            if tag in ("td", "th") and self._open_in_scope({"td", "th"}, stop=("tr", "table")):
                self._close_to(self._open_in_scope({"td", "th"}, stop=("tr", "table")))
            if tag == "tr" and self._open_in_scope({"tr"}, stop=("table",)):
                self._close_to("tr")
            if tag in ("thead", "tbody", "tfoot") and self._open_in_scope({"thead", "tbody", "tfoot"}, stop=("table",)):
                self._close_to(self._open_in_scope({"thead", "tbody", "tfoot"}, stop=("table",)))
            if tag == "option" and self.top.tag == "option":
                self.stack.pop()
        el = ET.SubElement(self.top, tag, attrib)
        if tag == "svg":
            self.in_svg += 1
        if closed or (tag in VOID and not self.in_svg) or len(self.stack) > MAX_DEPTH:
            # past MAX_DEPTH levels an element stays empty and what it held goes on at this level
            if tag == "svg":
                self.in_svg -= 1
            return
        self.stack.append(el)

    def handle_endtag(self, tag):
        if ":" in tag:
            tag = tag.split(":", 1)[1]
        if self.in_svg:
            tag = SVG_TAGS.get(tag, tag)
        if tag in ("html", "body", "head"):
            if tag == "head" and self.top.tag == "head":
                self.stack.pop()
            return
        if tag in VOID and not self.in_svg:
            return
        if tag == "p" and not self._open_in_scope({"p"}):
            ET.SubElement(self.top, "p")  # a stray </p> makes an empty paragraph, as browsers do
            return
        if self._close_to(tag) and tag == "svg":
            self.in_svg = max(0, self.in_svg - 1)

    def handle_data(self, data):
        if len(self.stack) == 1:
            if not data.strip():
                return
            self._start("body", [], closed=False)
        elif self.top.tag == "head" and data.strip():
            self._start("body", [], closed=False)
        self._append_text(data)

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


# --- writing ----------------------------------------------------------------------------------

def _esc_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;").replace("\n", "&#10;")


_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def clean_text(s: str) -> str:
    return _XML_BAD.sub("", s)


def serialize(root: ET.Element, doctype: bool = True, xml_decl: bool = True) -> bytes:
    """Write an element tree (local names) as XHTML, UTF-8."""
    out: list[str] = []
    if xml_decl:
        out.append('<?xml version="1.0" encoding="utf-8"?>\n')
    if doctype and root.tag == "html":
        out.append("<!DOCTYPE html>\n")
    _write(root, out, top=True)
    return "".join(out).encode("utf-8")


def to_string(el: ET.Element) -> str:
    out: list[str] = []
    _write(el, out, top=False, ns=False)
    return "".join(out)


_NAME = re.compile(r"[^\W\d][\w.-]*(?::[^\W\d][\w.-]*)?\Z")
_names: dict[str, bool] = {}


def _ok_name(name: str) -> bool:
    """An XML name (with at most one prefix), so a tag or attribute read from a book can't break the markup."""
    ok = _names.get(name)
    if ok is None:
        ok = _names[name] = len(_names) < 100_000 and bool(_NAME.match(name)) and not name.lower().startswith("xmlns")
    return ok


def _write(root, out, top, ns=True, in_svg=False):
    # depth-first with an explicit stack, so deep trees can't hit Python's recursion limit
    stack = [(root, top, in_svg, None)]
    pop, push = stack.pop, stack.append
    while stack:
        el, is_top, in_svg, closing = pop()
        if closing is not None:
            out.append(closing)
            if el.tail and not is_top:
                out.append(_esc_text(clean_text(el.tail)))
            continue
        tag = el.tag
        if not isinstance(tag, str):  # comments / processing instructions
            if el.tail:
                out.append(_esc_text(clean_text(el.tail)))
            continue
        if not _ok_name(tag):
            tag = "span"
        attrs = {k: v for k, v in el.attrib.items() if _ok_name(k)}
        if ns:
            if is_top and tag == "html":
                attrs = {"xmlns": XHTML_NS, **attrs}
                used = {k.split(":", 1)[0] for e in el.iter() for k in e.attrib if ":" in k}
                if "epub" in used:
                    attrs["xmlns:epub"] = EPUB_NS
            if tag == "svg" and not in_svg:
                attrs["xmlns"] = SVG_NS
                if any(k.startswith("xlink:") for e in el.iter() for k in e.attrib):
                    attrs["xmlns:xlink"] = XLINK_NS
            elif tag == "math":
                attrs["xmlns"] = MATHML_NS
        out.append("<" + tag)
        for k, v in attrs.items():
            out.append(f' {k}="{_esc_attr(clean_text(str(v)))}"')
        children = len(el)
        text = el.text
        sub_svg = in_svg or tag == "svg"
        if tag in VOID and not children and not text and not in_svg or sub_svg and not children and not text:
            out.append("/>")
            if el.tail and not is_top:
                out.append(_esc_text(clean_text(el.tail)))
            continue
        out.append(">")
        if text:
            t = clean_text(text)
            out.append(t if tag in RAW_TEXT and "<" not in t and "&" not in t else _esc_text(t))
        push((el, is_top, in_svg, f"</{tag}>"))
        for child in reversed(el):
            push((child, False, sub_svg, None))


# --- small tree helpers ----------------------------------------------------------------------

def text_of(el: ET.Element) -> str:
    return "".join(el.itertext())


def new_document(title: str = "", lang: str = "", css_hrefs=()) -> tuple[ET.Element, ET.Element]:
    """An empty XHTML document; returns (html, body)."""
    root = ET.Element("html")
    if lang:
        root.set("lang", lang)
        root.set("xml:lang", lang)
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "meta", {"charset": "utf-8"})
    ET.SubElement(head, "title").text = title or "Untitled"
    for href in css_hrefs:
        ET.SubElement(head, "link", {"rel": "stylesheet", "type": "text/css", "href": href})
    body = ET.SubElement(root, "body")
    return root, body


def append_text(parent: ET.Element, text: str) -> None:
    if not text:
        return
    if len(parent):
        parent[-1].tail = (parent[-1].tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def escape(s: str) -> str:
    return _esc_text(s)
