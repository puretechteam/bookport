"""Make documents valid XHTML5 for EPUB: obsolete markup to CSS, broken links and nesting fixed.

This is what lets bookport write EPUBs that pass epubcheck with zero errors from HTML found in
the wild (old MOBI files, Word exports, hand-made HTML).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from . import markup
from .book import CSS, Book, is_external, relative, resolve
from .links import rewrite_css

OBSOLETE_TAGS = {
    "font": "span", "center": "div", "big": "span", "strike": "s", "tt": "code", "acronym": "abbr",
    "basefont": None, "dir": "ul", "applet": None, "frame": None, "frameset": None, "noframes": "div",
    "marquee": "span", "blink": "span", "nobr": "span", "spacer": None, "xmp": "pre", "listing": "pre",
    "plaintext": "pre", "isindex": None, "keygen": None, "menuitem": None, "rb": "span", "rtc": "span",
    "content": "span", "shadow": None, "element": "span", "image": "img", "layer": "div", "ilayer": "span",
    "multicol": "div", "nextid": None, "bgsound": None, "embed": None, "html:html": None,
}
TAG_STYLE = {"center": "text-align: center", "big": "font-size: larger", "tt": "", "nobr": "white-space: nowrap"}

# Attributes HTML5 allows on any element
GLOBAL_ATTRS = frozenset(["accesskey", "autocapitalize", "autofocus", "class", "contenteditable", "dir", "draggable", "enterkeyhint", "hidden", "id", "inert", "inputmode", "is", "itemid", "itemprop", "itemref", "itemscope", "itemtype", "lang", "nonce", "popover", "slot", "spellcheck", "style", "tabindex", "title", "translate", "xml:lang", "xml:space", "epub:type", "epub:prefix", "role"])
ELEMENT_ATTRS = {
    "a": {"href", "target", "download", "ping", "rel", "hreflang", "type", "referrerpolicy"},
    "area": {"alt", "coords", "shape", "href", "target", "download", "ping", "rel", "referrerpolicy"},
    "img": {"alt", "src", "srcset", "sizes", "crossorigin", "usemap", "ismap", "width", "height", "referrerpolicy",
            "decoding", "loading", "fetchpriority"},
    "ol": {"reversed", "start", "type"}, "li": {"value"}, "q": {"cite"}, "blockquote": {"cite"},
    "del": {"cite", "datetime"}, "ins": {"cite", "datetime"}, "time": {"datetime"}, "data": {"value"},
    "td": {"colspan", "rowspan", "headers"}, "th": {"colspan", "rowspan", "headers", "scope", "abbr"},
    "col": {"span"}, "colgroup": {"span"}, "table": set(), "meta": {"name", "content", "charset", "http-equiv"},
    "link": {"href", "rel", "type", "media", "hreflang", "sizes", "crossorigin", "title", "as"},
    "style": {"media", "type", "title"}, "abbr": set(), "dfn": set(), "bdo": set(),
    "iframe": {"src", "srcdoc", "name", "sandbox", "allow", "allowfullscreen", "width", "height", "referrerpolicy",
               "loading"},
    "object": {"data", "type", "name", "form", "width", "height"}, "param": {"name", "value"},
    "video": {"src", "crossorigin", "poster", "preload", "autoplay", "playsinline", "loop", "muted", "controls",
              "width", "height"},
    "audio": {"src", "crossorigin", "preload", "autoplay", "loop", "muted", "controls"},
    "source": {"src", "type", "srcset", "sizes", "media", "width", "height"}, "track": {"kind", "src", "srclang",
                                                                                         "label", "default"},
    "map": {"name"}, "label": {"for"}, "output": {"for", "form", "name"}, "progress": {"value", "max"},
    "meter": {"value", "min", "max", "low", "high", "optimum"},
    "form": {"accept-charset", "action", "autocomplete", "enctype", "method", "name", "novalidate", "target"},
    "input": {"accept", "alt", "autocomplete", "checked", "dirname", "disabled", "form", "list", "max", "maxlength",
              "min", "minlength", "multiple", "name", "pattern", "placeholder", "readonly", "required", "size", "src",
              "step", "type", "value", "width", "height"},
    "button": {"disabled", "form", "name", "type", "value"},
    "select": {"autocomplete", "disabled", "form", "multiple", "name", "required", "size"},
    "option": {"disabled", "label", "selected", "value"}, "optgroup": {"disabled", "label"},
    "textarea": {"cols", "rows", "name", "disabled", "readonly", "required", "placeholder", "maxlength", "wrap"},
    "details": {"open"}, "dialog": {"open"}, "html": {"xmlns", "lang", "xml:lang", "dir"},
    "base": {"href", "target"}, "script": {"src", "type", "async", "defer", "crossorigin", "charset"},
    "canvas": {"width", "height"}, "embed": {"src", "type", "width", "height"}, "body": set(),
    "head": set(), "title": set(), "svg": None, "math": None,
}
FLOW_ONLY = frozenset(["address", "article", "aside", "blockquote", "details", "dialog", "div", "dl", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol", "p", "pre", "section", "table", "ul", "center", "li", "dd", "dt", "tr", "td", "th", "tbody", "thead", "tfoot", "caption"])
PHRASING_PARENTS = frozenset(["p", "h1", "h2", "h3", "h4", "h5", "h6", "span", "a", "b", "i", "em", "strong", "small", "sub", "sup", "u", "s", "code", "abbr", "cite", "q", "dfn", "kbd", "samp", "var", "mark", "label", "bdi", "bdo", "time", "data", "ruby", "rt", "rp", "pre", "legend", "summary", "dt", "caption"])
_ID_OK = re.compile(r"^[A-Za-z_][\w.-]*$")
CORE_IMAGES = {"image/jpeg", "image/png", "image/gif", "image/svg+xml", "image/webp"}


def _ensure_style(el, css):
    if not css:
        return
    old = el.get("style", "").strip().rstrip(";")
    el.set("style", f"{old}; {css}" if old else css)


def _length_css(v):
    v = v.strip()
    if re.fullmatch(r"\d+(\.\d+)?", v):
        return f"{v}px"
    if re.fullmatch(r"\d+(\.\d+)?%", v):
        return v
    return None


def presentational_to_css(el):
    """Turn presentational attributes HTML5 dropped into inline CSS (or drop them)."""
    a = el.attrib
    tag = el.tag
    css = []
    if "align" in a:
        v = a.pop("align").lower()
        if tag in ("img", "object", "table", "iframe", "embed"):
            if v in ("left", "right"):
                css.append(f"float: {v}")
            elif v == "center" and tag == "table":
                css.append("margin-left: auto; margin-right: auto")
            elif v in ("middle", "top", "bottom"):
                css.append(f"vertical-align: {v}")
        elif v in ("left", "right", "center", "justify"):
            css.append(f"text-align: {v}")
    if "valign" in a:
        v = a.pop("valign").lower()
        if v in ("top", "middle", "bottom", "baseline"):
            css.append(f"vertical-align: {v}")
    if "bgcolor" in a:
        css.append(f"background-color: {a.pop('bgcolor')}")
    if "color" in a and tag in ("font", "basefont", "hr"):
        css.append(f"color: {a.pop('color')}")
    if tag in ("table", "td", "th", "col", "colgroup", "hr", "pre", "div", "p", "tr", "iframe", "object", "embed"):
        for dim in ("width", "height"):
            if dim in a and dim not in ELEMENT_ATTRS.get(tag, set()):
                length = _length_css(a.pop(dim))
                if length:
                    css.append(f"{dim}: {length}")
    if tag == "table":
        if "border" in a:
            b = a.pop("border").strip() or "1"
            if b.isdigit() and int(b) > 0:
                css.append("border-collapse: collapse")
                el.set("class", (el.get("class", "") + " bp-bordered").strip())
        if "cellpadding" in a:
            pad = _length_css(a.pop("cellpadding"))
            if pad:
                el.set("data-pad", pad)  # applied to cells below
        if "cellspacing" in a:
            sp = _length_css(a.pop("cellspacing"))
            if sp:
                css.append(f"border-spacing: {sp}")
        for k in ("frame", "rules", "summary", "datapagesize"):
            a.pop(k, None)
    if tag == "font":
        face, size = a.pop("face", None), a.pop("size", None)
        if face:
            css.append(f"font-family: {face}")
        if size:
            s = size.strip()
            sizes = {"1": "x-small", "2": "small", "3": "medium", "4": "large", "5": "x-large", "6": "xx-large",
                     "7": "xx-large", "-1": "smaller", "-2": "small", "+1": "larger", "+2": "x-large", "+3": "xx-large"}
            if s in sizes:
                css.append(f"font-size: {sizes[s]}")
    if tag == "hr":
        a.pop("noshade", None)
        a.pop("size", None)
    if tag == "br" and "clear" in a:
        v = a.pop("clear").lower()
        if v in ("left", "right", "both", "all"):
            css.append(f"clear: {'both' if v == 'all' else v}")
    if tag == "img":
        for k in ("hspace", "vspace"):
            v = a.pop(k, None)
            if v and v.isdigit():
                css.append(f"margin-{'left' if k == 'hspace' else 'top'}: {v}px")
        b = a.pop("border", None)
        if b and b.isdigit():
            css.append(f"border-width: {b}px")
        for dim in ("width", "height"):
            v = a.get(dim)
            if v is not None and not re.fullmatch(r"\d+", v.strip()):
                a.pop(dim)
                length = _length_css(v.strip())
                if length:
                    css.append(f"{dim}: {length}")
    if tag in ("ul", "li") and "type" in a:
        v = a.pop("type").lower()
        if v in ("disc", "circle", "square"):
            css.append(f"list-style-type: {v}")
    if tag in ("td", "th") and "nowrap" in a:
        a.pop("nowrap")
        css.append("white-space: nowrap")
    if tag in ("body",):
        for k in ("text", "link", "vlink", "alink", "background", "topmargin", "leftmargin", "marginwidth",
                  "marginheight"):
            a.pop(k, None)
    _ensure_style(el, "; ".join(css))


class Cleaner:
    """Cleans the documents of one book, knowing every resource and every id."""

    def __init__(self, book: Book):
        self.book = book
        self.ids: dict[str, set] = {}

    def run(self, keep_scripts: bool = False) -> None:
        book = self.book
        for href, res in list(book.resources.items()):
            if res.is_document:
                root = book.tree(href)
                self.structure(href, root, keep_scripts)
                self.ids[href] = {e.get("id") for e in root.iter() if e.get("id")}
        for href, res in list(book.resources.items()):
            if res.is_document:
                root = book.tree(href)
                self.links(href, root)
                book.set_tree(href, root)
            elif res.media_type == CSS:
                res.data = self.css(href, markup.decode(res.data)).encode("utf-8")

    # --- structure ----------------------------------------------------------------------------
    def structure(self, href, root, keep_scripts):
        head, body = root.find("head"), root.find("body")
        for k in list(root.attrib):
            if k not in ("lang", "xml:lang", "dir", "epub:prefix"):
                del root.attrib[k]
        lang = root.get("lang") or root.get("xml:lang") or self.book.metadata.language
        if lang:
            root.set("lang", lang)
            root.set("xml:lang", lang)
        # head: one charset meta, a non-empty title, stylesheets, styles
        title = head.find("title")
        if title is None:
            title = ET.Element("title")
            head.insert(0, title)
        if not (title.text or "").strip():
            h = next((e for e in body.iter() if e.tag in ("h1", "h2", "h3")), None)
            title.text = (" ".join(markup.text_of(h).split()) if h is not None else "") or self.book.metadata.title or "Untitled"
        title.text = " ".join(title.text.split())
        for t in head.findall("title")[1:]:
            head.remove(t)
        for el in list(head):
            if el.tag == "meta":
                if el.get("http-equiv") or el.get("charset") or not el.get("name") and not el.get("property") or el.get("property") and not el.get("name"):
                    head.remove(el)
            elif el.tag == "script" and not keep_scripts:
                head.remove(el)
            elif el.tag not in ("title", "meta", "link", "style", "script", "base"):
                head.remove(el)
                body.insert(0, el)
            elif el.tag == "base":
                head.remove(el)
        head.insert(0, ET.Element("meta", {"charset": "utf-8"}))
        # body styles move to <head>
        parent = {c: p for p in root.iter() for c in p}
        for st in [e for e in body.iter() if e.tag == "style"]:
            parent[st].remove(st)
            _keep_tail(parent[st], st)
            st.tail = None
            head.append(st)
        self._fix_tree(body, parent, keep_scripts)
        self._unique_ids(root)

    def _fix_tree(self, body, parent, keep_scripts):
        # 1. element renames, removals and attribute fixes (top-down)
        foreign = {}
        for el in list(body.iter()):
            if not isinstance(el.tag, str):
                continue
            tag = el.tag
            if ":" in tag:
                el.tag = tag = tag.split(":", 1)[1]
            if tag in ("svg", "math"):
                continue
            if self._in_foreign(el, parent, foreign):
                continue
            if tag in ("script", "noscript") and not keep_scripts or tag in ("head", "html", "body") and el is not body:
                _remove(el, parent, keep_children=tag in ("noscript", "html", "body", "head"))
                continue
            if tag in OBSOLETE_TAGS:
                new = OBSOLETE_TAGS[tag]
                if tag in ("font", "basefont"):
                    presentational_to_css(el)
                if new is None:
                    _remove(el, parent, keep_children=tag not in ("applet", "embed", "frameset", "frame", "bgsound",
                                                                 "isindex", "keygen", "spacer", "nextid"))
                    continue
                _ensure_style(el, TAG_STYLE.get(tag, ""))
                el.tag = new
                if tag == "image" and el.get("href") and not el.get("src"):
                        el.set("src", el.attrib.pop("href"))
            presentational_to_css(el)
            self._attrs(el)
        # 2. nesting: block elements inside phrasing elements
        foreign = {}
        for el in list(body.iter()):
            if el.tag in ("svg", "math") or self._in_foreign(el, parent, foreign):
                continue
            if el.tag in PHRASING_PARENTS and any(isinstance(c.tag, str) and c.tag in FLOW_ONLY for c in el.iter() if c is not el):
                if el.tag in ("p", "span", "pre", "summary", "legend", "caption"):
                    el.tag = "div"
                elif el.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    el.set("class", (el.get("class", "") + f" bp-{el.tag}").strip())
                    el.tag = "div"
                elif el.tag == "a":
                    pass  # <a> is transparent: allowed if its parent allows the content
                elif el.tag == "dt":
                    el.tag = "dd"
                else:
                    el.tag = "span"
                    # inline formatting wrapping blocks becomes a div with the same look
                    el.tag = "div"
        parent = {c: p for p in body.iter() for c in p}
        # 3. <a> inside <a>; lists hold only <li>; tables hold rows; empty headings are fine
        for el in list(body.iter()):
            if el.tag == "a":
                p = parent.get(el)
                while p is not None and p is not body:
                    if p.tag == "a":
                        el.tag = "span"
                        for k in ("href", "target", "rel", "download", "type", "hreflang"):
                            el.attrib.pop(k, None)
                        break
                    p = parent.get(p)
            elif el.tag in ("ul", "ol"):
                self._wrap_list(el)
            elif el.tag == "dl":
                for c in list(el):
                    if isinstance(c.tag, str) and c.tag not in ("dt", "dd", "div", "script", "template"):
                        c.tag = "dd" if c.tag not in FLOW_ONLY else c.tag
                        if c.tag not in ("dt", "dd"):
                            wrapper = ET.Element("dd")
                            i = list(el).index(c)
                            el.remove(c)
                            wrapper.append(c)
                            el.insert(i, wrapper)
                if el.text and el.text.strip():
                    dd = ET.Element("dd")
                    dd.text = el.text
                    el.text = None
                    el.insert(0, dd)
            elif el.tag == "table":
                self._fix_table(el)
            elif el.tag == "img":
                el.set("alt", el.get("alt", ""))
            elif el.tag == "li" and parent.get(el) is not None and parent[el].tag not in ("ul", "ol", "menu"):
                el.tag = "div"
                el.attrib.pop("value", None)
            elif el.tag == "li" and parent.get(el) is not None and parent[el].tag != "ol":
                el.attrib.pop("value", None)  # only items of an <ol> carry a number
            elif el.tag in ("dt", "dd") and parent.get(el) is not None and parent[el].tag not in ("dl", "div"):
                el.tag = "div"
            elif el.tag in ("td", "th", "tr", "tbody", "thead", "tfoot", "caption", "col", "colgroup"):
                p = parent.get(el)
                ok = {"td": ("tr",), "th": ("tr",), "tr": ("table", "tbody", "thead", "tfoot"),
                      "tbody": ("table",), "thead": ("table",), "tfoot": ("table",), "caption": ("table",),
                      "col": ("colgroup", "table"), "colgroup": ("table",)}[el.tag]
                if p is None or p.tag not in ok:
                    el.tag = "div" if el.tag not in ("col", "colgroup") else el.tag
                    if el.tag in ("col", "colgroup"):
                        _remove(el, parent, keep_children=False)
            elif el.tag == "figcaption" and parent.get(el) is not None and parent[el].tag != "figure":
                el.tag = "div"
            elif el.tag in ("rt", "rp") and parent.get(el) is not None and parent[el].tag != "ruby":
                el.tag = "span"
            elif el.tag in ("source", "track", "param", "option", "optgroup", "area", "legend", "summary"):
                p = parent.get(el)
                need = {"source": ("video", "audio", "picture"), "track": ("video", "audio"), "param": ("object",),
                        "option": ("select", "datalist", "optgroup"), "optgroup": ("select",), "area": ("map",),
                        "legend": ("fieldset",), "summary": ("details",)}[el.tag]
                if p is None or p.tag not in need:
                    if el.tag in ("legend", "summary", "option"):
                        el.tag = "span" if el.tag == "option" else "div"
                    else:
                        _remove(el, parent, keep_children=False)

    def _in_foreign(self, el, parent, seen):
        # seen: element -> it is an <svg>/<math> or inside one. The walk up stops at the first element
        # already known, so each element is climbed once instead of once per descendant.
        path, p, inside = [], parent.get(el), False
        while p is not None:
            if p in seen:
                inside = seen[p]
                break
            if p.tag in ("svg", "math"):
                inside = seen[p] = True
                break
            path.append(p)
            p = parent.get(p)
        for q in path:
            seen[q] = inside
        return inside

    def _attrs(self, el):
        allowed = ELEMENT_ATTRS.get(el.tag, set())
        for k in list(el.attrib):
            low = k.lower()
            if low.startswith("on") and not keep_event(low):
                del el.attrib[k]
            elif low.startswith(("data-", "aria-")) or k in GLOBAL_ATTRS or k in allowed:
                if low == "data-pad":
                    continue
                continue
            elif k == "name" and el.tag == "a":
                name = el.attrib.pop("name")
                if not el.get("id") and name:
                    el.set("id", name)
            elif k == "lang" or k == "xml:lang":
                continue
            else:
                del el.attrib[k]
        if el.get("dir") and el.get("dir").lower() not in ("ltr", "rtl", "auto"):
            del el.attrib["dir"]
        if "lang" in el.attrib or "xml:lang" in el.attrib:
            lang = el.get("lang") or el.get("xml:lang")
            if lang:
                el.set("lang", lang)
                el.set("xml:lang", lang)
            else:
                el.attrib.pop("lang", None)
                el.attrib.pop("xml:lang", None)
        if el.tag == "a" and el.get("target") and not el.get("href"):
            del el.attrib["target"]
        for k in ("colspan", "rowspan", "span", "start", "value"):
            v = el.get(k)
            if v is not None:
                try:
                    n = int(float(v.strip()))
                    if k in ("colspan", "span") and n < 1 or k == "rowspan" and n < 0:
                        raise ValueError
                    el.set(k, str(n))
                except ValueError:
                    del el.attrib[k]
        for k in ("width", "height"):
            v = el.get(k)
            if v is not None and not re.fullmatch(r"\d+", v.strip()):
                del el.attrib[k]
        if el.get("tabindex") is not None and not re.fullmatch(r"-?\d+", el.get("tabindex")):
            del el.attrib["tabindex"]
        if el.tag == "ol" and el.get("type") not in (None, "1", "a", "A", "i", "I"):
            del el.attrib["type"]
        if el.tag == "th" and el.get("scope") not in (None, "row", "col", "rowgroup", "colgroup"):
            del el.attrib["scope"]
        for flag in ("hidden", "reversed", "controls", "autoplay", "loop", "muted", "open", "checked", "selected",
                     "disabled", "readonly", "required", "multiple", "ismap", "allowfullscreen", "defer", "async"):
            if flag in el.attrib:
                el.set(flag, flag)
        if "class" in el.attrib and not el.get("class").strip():
            del el.attrib["class"]
        if "style" in el.attrib:
            st = clean_inline_style(el.get("style"))
            if st:
                el.set("style", st)
            else:
                del el.attrib["style"]
        if "epub:type" in el.attrib and not el.get("epub:type").strip():
            del el.attrib["epub:type"]

    def _wrap_list(self, el):
        if el.text and el.text.strip():
            li = ET.Element("li")
            li.text = el.text
            el.insert(0, li)
        el.text = None
        new_children = []
        pending = None
        for c in list(el):
            el.remove(c)
            if isinstance(c.tag, str) and c.tag in ("li", "script", "template"):
                if pending is not None:
                    pending = None
                new_children.append(c)
            else:
                if pending is None:
                    pending = ET.Element("li")
                    pending.set("style", "list-style-type: none")
                    new_children.append(pending)
                pending.append(c)
                if c.tail and c.tail.strip():
                    pass
                else:
                    pass
            if c.tail and c.tail.strip() and c.tag == "li":
                target = ET.Element("li")
                target.set("style", "list-style-type: none")
                target.text = c.tail
                c.tail = None
                new_children.append(target)
            elif c.tail and not c.tail.strip():
                c.tail = None
        for c in new_children:
            el.append(c)

    def _fix_table(self, table):
        pad = table.attrib.pop("data-pad", None)
        bordered = "bp-bordered" in table.get("class", "")
        if table.text and table.text.strip():
            cap = ET.Element("caption")
            cap.text = table.text
            table.insert(0, cap)
        table.text = None
        # only caption, colgroup, thead, tbody, tfoot, tr may sit in a table
        order = {"caption": 0, "colgroup": 1, "thead": 2}
        body_rows = []
        children = list(table)
        for c in children:
            table.remove(c)
        heads, tails, others = [], [], []
        for c in children:
            if not isinstance(c.tag, str):
                continue
            c.tail = None
            if c.tag in order:
                heads.append(c)
            elif c.tag == "col":  # a <col> straight in the table belongs to a colgroup, as HTML parsers make it
                if not heads or heads[-1].tag != "colgroup" or heads[-1].get("data-made") != "1":
                    heads.append(ET.Element("colgroup", {"data-made": "1"}))
                heads[-1].append(c)
            elif c.tag == "tfoot":
                tails.append(c)
            elif c.tag in ("tbody", "tr"):
                others.append(c)
            elif c.tag in ("td", "th"):
                if not others or others[-1].tag != "tr" or others[-1].get("data-made") != "1":
                    tr = ET.Element("tr", {"data-made": "1"})
                    others.append(tr)
                others[-1].append(c)
            else:
                cell = ET.Element("td")
                cell.append(c)
                tr = ET.Element("tr")
                tr.append(cell)
                others.append(tr)
        # rows may sit straight in a table or in row groups, not both: runs of loose rows get a tbody
        if any(c.tag == "tr" for c in others) and (any(c.tag == "tbody" for c in others) or tails or
                                                   any(c.tag == "thead" for c in heads)):
            grouped = []
            for c in others:
                if c.tag == "tr":
                    if not grouped or grouped[-1].get("data-made") != "1":
                        grouped.append(ET.Element("tbody", {"data-made": "1"}))
                    grouped[-1].append(c)
                else:
                    grouped.append(c)
            others = grouped
        # caption must come first
        heads.sort(key=lambda e: order[e.tag])
        for c in heads + others + tails:
            c.attrib.pop("data-made", None)
            table.append(c)
            body_rows.append(c)
        for tr in table.iter("tr"):
            tr.attrib.pop("data-made", None)
            if tr.text and tr.text.strip():
                td = ET.Element("td")
                td.text = tr.text
                tr.insert(0, td)
            tr.text = None
            for c in list(tr):
                if isinstance(c.tag, str) and c.tag not in ("td", "th", "script", "template"):
                    td = ET.Element("td")
                    i = list(tr).index(c)
                    tr.remove(c)
                    td.append(c)
                    tr.insert(i, td)
                if isinstance(c.tag, str) and c.tag in ("td", "th"):
                    if pad:
                        _ensure_style(c, f"padding: {pad}")
                    if bordered:
                        _ensure_style(c, "border: 1px solid")
                if c.tail and not c.tail.strip():
                    c.tail = None
        for sec in table:
            if sec.tag in ("tbody", "thead", "tfoot"):
                sec.text = None
                for c in list(sec):
                    if isinstance(c.tag, str) and c.tag != "tr":
                        tr = ET.Element("tr")
                        td = ET.SubElement(tr, "td")
                        i = list(sec).index(c)
                        sec.remove(c)
                        td.append(c)
                        sec.insert(i, tr)
        cls = table.get("class", "").replace("bp-bordered", "").strip()
        if cls:
            table.set("class", cls)
        else:
            table.attrib.pop("class", None)
        if bordered:
            _ensure_style(table, "border: 1px solid")

    def _unique_ids(self, root):
        seen = set()
        for el in root.iter():
            i = el.get("id")
            if i is None:
                continue
            new = i.strip()
            if not _ID_OK.match(new):
                new = re.sub(r"[^\w.-]", "_", new)
                if not new or not (new[0].isalpha() or new[0] == "_"):
                    new = "id_" + new
            base, n = new, 1
            while new in seen:
                new = f"{base}_{n}"
                n += 1
            seen.add(new)
            if new != i:
                el.set("id", new)
                el.set("data-old-id", i)

    # --- links --------------------------------------------------------------------------------
    def links(self, href, root):
        book = self.book
        old_ids = {}
        for el in root.iter():
            if el.get("data-old-id") is not None:
                old_ids.setdefault(el.attrib.pop("data-old-id"), el.get("id"))
        self._old_ids = getattr(self, "_old_ids", {})
        self._old_ids[href] = old_ids
        parent = {c: p for p in root.iter() for c in p}
        head = root.find("head")
        for el in list(root.iter()):
            if el.tag == "link":
                target = resolve(href, el.get("href", ""))
                res = book.resources.get(target)
                if res is None or res.media_type != CSS or is_external(el.get("href", "")):
                    parent[el].remove(el)
                    continue
                el.set("rel", "stylesheet")
                el.set("type", "text/css")
                for k in list(el.attrib):
                    if k not in ("href", "rel", "type", "media", "title"):
                        del el.attrib[k]
                continue
            if el.get("srcset") is not None:
                kept = []
                for part in el.get("srcset").split(","):
                    bits = part.split()
                    if bits and not is_external(bits[0]) and resolve(href, bits[0]) in book.resources:
                        kept.append(" ".join([relative(href, resolve(href, bits[0]))] + bits[1:]))
                if kept:
                    el.set("srcset", ", ".join(kept))
                else:
                    del el.attrib["srcset"]
            for attr in ("href", "src", "xlink:href", "poster", "data"):
                v = el.get(attr)
                if v is None:
                    continue
                v = v.strip()
                if attr == "href" and el.tag in ("a", "area"):
                    new = self._fix_anchor_link(href, v)
                    if new is None:
                        del el.attrib[attr]
                        for k in ("target", "rel", "download", "type", "hreflang"):
                            el.attrib.pop(k, None)
                    else:
                        el.set(attr, new)
                    continue
                if v.startswith("data:"):
                    continue
                if is_external(v) or not v:
                    self._drop_media(el, attr, parent)
                    continue
                target = resolve(href, v)
                res = book.resources.get(target)
                if res is None:
                    self._drop_media(el, attr, parent)
                    continue
                if el.tag == "img" and res.is_image and res.media_type not in CORE_IMAGES:
                    self._drop_media(el, attr, parent)
                    continue
                el.set(attr, relative(href, target))
            st = el.get("style")
            if st and "url(" in st:
                el.set("style", self.css(href, st))
            if el.tag == "style" and el.text:
                el.text = self.css(href, el.text)
        del head

    def _fix_anchor_link(self, href, v):
        if not v:
            return None
        if v.lower().startswith("javascript:"):
            return None
        if is_external(v):
            # a "%" that doesn't start an escape (#%_sec) makes the URL invalid: escape it
            return re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", v.replace(" ", "%20"))
        doc, _, frag = v.partition("#")
        target = resolve(href, doc) if doc else href
        if target not in self.book.resources:
            return None
        res = self.book.resources[target]
        if not res.is_document:
            return relative(href, target) if res.is_image and res.media_type in CORE_IMAGES else None
        if frag:
            from urllib.parse import unquote
            frag = unquote(frag)
            ids = self.ids.get(target, set())
            frag = self._old_ids.get(target, {}).get(frag, frag) if hasattr(self, "_old_ids") else frag
            if frag not in ids:
                frag = ""
        return relative(href, target + (f"#{frag}" if frag else ""))

    def _drop_media(self, el, attr, parent):
        if el.tag in ("img", "image", "embed", "iframe", "video", "audio", "source", "track", "object") or attr == "src":
            alt = el.get("alt", "")
            p = parent.get(el)
            if p is not None:
                idx = list(p).index(el)
                tail = (alt + (el.tail or "")) if alt else el.tail
                p.remove(el)
                if tail:
                    if idx > 0:
                        p[idx - 1].tail = (p[idx - 1].tail or "") + tail
                    else:
                        p.text = (p.text or "") + tail
        else:
            del el.attrib[attr]

    def css(self, href, text):
        book = self.book

        def fix(link):
            if link.startswith("data:"):
                return link
            if is_external(link):
                return None
            target = resolve(href, link)
            return relative(href, target) if target in book.resources else None

        text = rewrite_css(text, fix)
        text = re.sub(r"@namespace[^;]*;", "", text)
        # @font-face blocks whose src is gone break epubcheck
        text = re.sub(r"@font-face\s*\{[^}]*\}", lambda m: m.group(0) if "url(" in m.group(0) else "", text)
        return text


def keep_event(_name):
    return False


def clean_inline_style(style: str) -> str:
    parts = []
    for decl in style.split(";"):
        if ":" not in decl:
            continue
        k, _, v = decl.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k and v and re.fullmatch(r"-?[a-z][a-z0-9-]*", k):
            parts.append(f"{k}: {v}")
    return "; ".join(parts)


def _keep_tail(parent, el):
    if el.tail:
        idx = list(parent).index(el) if el in list(parent) else -1
        if idx > 0:
            parent[idx - 1].tail = (parent[idx - 1].tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail


def _remove(el, parent, keep_children: bool):
    p = parent.get(el)
    if p is None or el not in list(p):  # already moved by what was cleaned before it
        return
    idx = list(p).index(el)
    p.remove(el)
    if keep_children:
        pieces = []
        if el.text:
            pieces.append(el.text)
        kids = list(el)
        if idx > 0:
            prev = p[idx - 1]
            prev.tail = (prev.tail or "") + "".join(pieces)
        else:
            p.text = (p.text or "") + "".join(pieces)
        for k, child in enumerate(kids):
            p.insert(idx + k, child)
            parent[child] = p
        last_tail = el.tail or ""
        if kids:
            kids[-1].tail = (kids[-1].tail or "") + last_tail
        elif idx > 0:
            p[idx - 1].tail = (p[idx - 1].tail or "") + last_tail
        else:
            p.text = (p.text or "") + last_tail
    else:
        if el.tail:
            if idx > 0:
                p[idx - 1].tail = (p[idx - 1].tail or "") + el.tail
            else:
                p.text = (p.text or "") + el.tail


def clean_book(book: Book, keep_scripts: bool = False) -> None:
    Cleaner(book).run(keep_scripts)
