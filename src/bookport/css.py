"""A small CSS engine: parse stylesheets, match selectors, compute inherited styles.

Enough of CSS 2.1 and Selectors 3 for ebooks: type, class, id, attribute and structural
selectors with all four combinators, specificity, !important, inline styles, the common
shorthands, and @media blocks for screen/all. Values stay strings; ``length`` turns one into
points.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

INHERITED = frozenset(["color", "font-family", "font-size", "font-style", "font-variant", "font-weight", "letter-spacing", "line-height", "list-style-type", "list-style-position", "text-align", "text-indent", "text-transform", "visibility", "white-space", "word-spacing", "direction", "hyphens"])

# Browser defaults for the elements books use (CSS 2.1 appendix D, trimmed)
UA_CSS = """
html, address, blockquote, body, dd, div, dl, dt, fieldset, form, frame, frameset, h1, h2, h3, h4, h5, h6,
noframes, ol, p, ul, center, dir, hr, menu, pre, article, aside, footer, header, hgroup, main, nav, section,
figure, figcaption, details, summary { display: block }
li { display: list-item }
head, script, style, title, meta, link, template { display: none }
table { display: table } tr { display: table-row } thead { display: table-header-group }
tbody { display: table-row-group } tfoot { display: table-footer-group } col { display: table-column }
colgroup { display: table-column-group } td, th { display: table-cell } caption { display: table-caption }
th { font-weight: bold; text-align: center }
h1 { font-size: 2em; margin: .67em 0 } h2 { font-size: 1.5em; margin: .75em 0 }
h3 { font-size: 1.17em; margin: .83em 0 } h4 { margin: 1.12em 0 }
h5 { font-size: .83em; margin: 1.5em 0 } h6 { font-size: .75em; margin: 1.67em 0 }
h1, h2, h3, h4, h5, h6, b, strong { font-weight: bold }
p, dl, ol, ul, fieldset, form, menu, dir { margin: 1em 0 }
blockquote, figure { margin: 1em 40px }
i, cite, em, var, address, dfn { font-style: italic }
pre, tt, code, kbd, samp { font-family: monospace }
pre { white-space: pre; margin: 1em 0 }
big { font-size: 1.17em } small, sub, sup { font-size: .83em }
sub { vertical-align: sub } sup { vertical-align: super }
s, strike, del { text-decoration: line-through }
u, ins { text-decoration: underline }
ol, ul, dir, menu, dd { margin-left: 40px }
ol { list-style-type: decimal } ul { list-style-type: disc }
ol ul, ul ol, ul ul, ol ol { margin-top: 0; margin-bottom: 0 }
center { text-align: center }
hr { border: 1px inset; margin: .5em 0 }
br { display: inline }
"""


@dataclass
class Rule:
    selector: list  # parsed compound chain, rightmost last
    specificity: tuple
    decls: dict  # prop -> (value, important)
    order: int


_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def parse_stylesheet(text: str, start_order: int = 0) -> list[Rule]:
    text = _COMMENT.sub("", text)
    rules: list[Rule] = []
    _parse_block(text, rules, start_order)
    return rules


def _parse_block(text, rules, order):
    i, n = 0, len(text)
    while i < n:
        j = text.find("{", i)
        if j < 0:
            break
        prelude = text[i:j].strip()
        end = _match_brace(text, j)
        body = text[j + 1:end]
        i = end + 1
        if prelude.startswith("@"):
            # an @-rule ending with ';' before the '{' (e.g. @import) belongs to the prelude
            semi = prelude.rfind(";")
            if semi >= 0:
                prelude = prelude[semi + 1:].strip()
            if not prelude.startswith("@"):
                pass
            else:
                kw = re.match(r"@[-\w]*", prelude).group(0).lower()  # "@supports(...)" has no space
                if kw == "@media":
                    media = prelude[6:].lower()
                    if not media.strip() or any(m in media for m in ("screen", "all")) and "amzn-mobi" not in media:
                        order = _parse_block(body, rules, order)
                elif kw == "@supports":
                    # every condition is taken as supported, except negated ones
                    if not re.match(r"@supports\s*not\b", prelude, re.IGNORECASE):
                        order = _parse_block(body, rules, order)
                continue
        if "@" in prelude and ";" in prelude:
            prelude = prelude[prelude.rfind(";") + 1:].strip()
        decls = parse_declarations(body)
        if not decls:
            continue
        for sel in _split_commas(prelude):
            parsed = parse_selector(sel)
            if parsed is None:
                continue
            chain, spec = parsed
            rules.append(Rule(chain, spec, decls, order))
            order += 1
    return order


def _match_brace(text, j):
    depth = 0
    for k in range(j, len(text)):
        c = text[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return k
    return len(text)


def _split_commas(s):
    parts, depth, cur = [], 0, []
    for c in s:
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def parse_declarations(body: str) -> dict:
    out: dict = {}
    for part in _split_semis(body):
        if ":" not in part:
            continue
        prop, _, value = part.partition(":")
        prop = prop.strip().lower()
        value = value.strip()
        important = False
        if value.lower().endswith("!important"):
            important = True
            value = value[:-10].strip()
        if not prop or not value:
            continue
        for p, v in expand(prop, value):
            out[p] = (v, important)
    return out


def _split_semis(s):
    parts, cur, quote, depth = [], [], None, 0
    for c in s:
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ";" and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    parts.append("".join(cur))
    return parts


def _box(values):
    v = values.split()
    if not v:
        return None
    if len(v) == 1:
        v = v * 4
    elif len(v) == 2:
        v = [v[0], v[1], v[0], v[1]]
    elif len(v) == 3:
        v = [v[0], v[1], v[2], v[1]]
    return v[:4]


_FONT_SIZES = re.compile(r"^(xx-small|x-small|small|medium|large|x-large|xx-large|smaller|larger|[\d.]+(px|pt|em|rem|%|ex|pc|in|cm|mm)?)(/\S+)?$")


def expand(prop: str, value: str):
    """Expand shorthands into longhands."""
    if prop in ("margin", "padding"):
        box = _box(value)
        if box:
            for side, v in zip(("top", "right", "bottom", "left"), box):
                yield f"{prop}-{side}", v
        return
    if prop == "font":
        parts = value.replace(", ", ",").split()
        for k, part in enumerate(parts):
            low = part.lower()
            if low in ("italic", "oblique"):
                yield "font-style", low
            elif low in ("bold", "bolder", "lighter") or low.isdigit():
                yield "font-weight", low
            elif low == "small-caps":
                yield "font-variant", low
            elif _FONT_SIZES.match(low):
                size, _, lh = low.partition("/")
                yield "font-size", size
                if lh:
                    yield "line-height", lh
                if k + 1 < len(parts):
                    yield "font-family", " ".join(parts[k + 1:]).replace(",", ", ")
                return
        return
    if prop == "page-break-before":
        yield "break-before", "page" if value in ("always", "left", "right") else value
        return
    if prop == "page-break-after":
        yield "break-after", "page" if value in ("always", "left", "right") else value
        return
    if prop == "page-break-inside":
        yield "break-inside", value
        return
    if prop == "text-decoration-line":
        yield "text-decoration", value
        return
    if prop == "list-style":
        for part in value.split():
            if part in ("inside", "outside"):
                yield "list-style-position", part
            elif not part.startswith("url("):
                yield "list-style-type", part
        return
    if prop == "background":
        for part in value.split():
            if part.startswith(("#", "rgb")) or part.isalpha():
                yield "background-color", part
                break
        return
    yield prop, value


# --- selectors --------------------------------------------------------------------------------

_SIMPLE = re.compile(
    r"""(?P<tag>\*|[A-Za-z_][\w-]*(?:\|[\w-]+)?)
      | \#(?P<id>[\w-]+)
      | \.(?P<cls>[\w-]+)
      | \[(?P<attr>[\w:|-]+)\s*(?:(?P<op>[~|^$*]?=)\s*(?P<val>"[^"]*"|'[^']*'|[^\]\s]+)\s*(?:i\s*)?)?\]
      | ::?(?P<pseudo>[\w-]+)(?:\((?P<arg>[^)]*)\))?
    """, re.VERBOSE)


def parse_selector(sel: str):
    """Returns ([(combinator, compound), ...], specificity) or None if unsupported."""
    sel = sel.strip()
    tokens = re.split(r"\s*([>+~])\s*|\s+", sel)
    chain = []
    comb = " "
    a = b = c = 0
    for tok in tokens:
        if tok is None or tok == "":
            continue
        if tok in (">", "+", "~"):
            comb = tok
            continue
        compound = _parse_compound(tok)
        if compound is None:
            return None
        chain.append((comb, compound))
        for kind, *_ in compound:
            if kind == "id":
                a += 1
            elif kind in ("cls", "attr", "pseudo", "not"):
                b += 1
            elif kind == "tag":
                c += 1
        comb = " "
    if not chain:
        return None
    return chain, (a, b, c)


_PSEUDO_OK = {"first-child", "last-child", "only-child", "first-of-type", "last-of-type", "link", "root", "empty"}


def _parse_compound(tok):
    out = []
    pos = 0
    while pos < len(tok):
        if tok.startswith(":not(", pos):
            end = tok.find(")", pos)
            inner = _parse_compound(tok[pos + 5:end])
            if inner is None or end < 0:
                return None
            out.append(("not", inner))
            pos = end + 1
            continue
        m = _SIMPLE.match(tok, pos)
        if not m or m.end() == pos:
            return None
        pos = m.end()
        if m.group("tag"):
            t = m.group("tag").split("|")[-1].lower()
            if t != "*":
                out.append(("tag", t))
        elif m.group("id"):
            out.append(("id", m.group("id")))
        elif m.group("cls"):
            out.append(("cls", m.group("cls")))
        elif m.group("attr"):
            val = m.group("val")
            if val and val[0] in "'\"":
                val = val[1:-1]
            out.append(("attr", m.group("attr").replace("|", ":"), m.group("op"), val))
        else:
            pseudo = m.group("pseudo").lower()
            if pseudo in ("before", "after", "first-letter", "first-line", "selection", "marker", "hover",
                          "focus", "active", "visited", "target"):
                return None  # generated content and dynamic states don't apply to a static book
            if pseudo == "nth-child" or pseudo == "nth-of-type":
                out.append(("nth", pseudo, (m.group("arg") or "").strip()))
            elif pseudo in _PSEUDO_OK:
                out.append(("pseudo", pseudo))
            else:
                return None
    return out


def _nth_match(expr, index):
    expr = expr.replace(" ", "").lower()
    if expr == "odd":
        expr = "2n+1"
    elif expr == "even":
        expr = "2n"
    if "n" not in expr:
        try:
            return index == int(expr)
        except ValueError:
            return False
    a, _, b = expr.partition("n")
    a = -1 if a == "-" else (1 if a in ("", "+") else int(a))
    b = int(b) if b else 0
    if a == 0:
        return index == b
    return (index - b) % a == 0 and (index - b) // a >= 0


class Matcher:
    """Selector matching for one document (holds the parent map)."""

    def __init__(self, root: ET.Element):
        self.parent = {c: p for p in root.iter() for c in p}
        self.root = root

    def classes(self, el):
        return el.get("class", "").split()

    def _siblings(self, el):
        p = self.parent.get(el)
        return [c for c in p if isinstance(c.tag, str)] if p is not None else [el]

    def compound(self, el, compound) -> bool:
        for item in compound:
            kind = item[0]
            if kind == "tag":
                if el.tag.lower() != item[1]:
                    return False
            elif kind == "id":
                if el.get("id") != item[1]:
                    return False
            elif kind == "cls":
                if item[1] not in self.classes(el):
                    return False
            elif kind == "attr":
                _, name, op, val = item
                have = el.get(name)
                if have is None:
                    return False
                if op == "=" and have != val:
                    return False
                if op == "~=" and val not in have.split():
                    return False
                if op == "|=" and not (have == val or have.startswith(val + "-")):
                    return False
                if op == "^=" and not have.startswith(val):
                    return False
                if op == "$=" and not have.endswith(val):
                    return False
                if op == "*=" and val not in have:
                    return False
            elif kind == "pseudo":
                name = item[1]
                if name == "root":
                    if el is not self.root:
                        return False
                elif name == "link":
                    if el.tag != "a" or "href" not in el.attrib:
                        return False
                elif name == "empty":
                    if len(el) or el.text:
                        return False
                else:
                    sibs = self._siblings(el)
                    if name.endswith("of-type"):
                        sibs = [s for s in sibs if s.tag == el.tag]
                    if name.startswith("first") and sibs[0] is not el:
                        return False
                    if name.startswith("last") and sibs[-1] is not el:
                        return False
                    if name == "only-child" and len(sibs) != 1:
                        return False
            elif kind == "nth":
                sibs = self._siblings(el)
                if item[1] == "nth-of-type":
                    sibs = [s for s in sibs if s.tag == el.tag]
                if not _nth_match(item[2], sibs.index(el) + 1):
                    return False
            elif kind == "not" and self.compound(el, item[1]):
                return False
        return True

    def matches(self, el, chain) -> bool:
        if not self.compound(el, chain[-1][1]):
            return False
        return self._match_from(el, chain, len(chain) - 1)

    def _match_from(self, el, chain, i):
        if i == 0:
            return True
        comb = chain[i][0]
        target = chain[i - 1][1]
        if comb == " ":
            p = self.parent.get(el)
            while p is not None:
                if self.compound(p, target) and self._match_from(p, chain, i - 1):
                    return True
                p = self.parent.get(p)
            return False
        if comb == ">":
            p = self.parent.get(el)
            return p is not None and self.compound(p, target) and self._match_from(p, chain, i - 1)
        sibs = self._siblings(el)
        k = sibs.index(el)
        if comb == "+":
            return k > 0 and self.compound(sibs[k - 1], target) and self._match_from(sibs[k - 1], chain, i - 1)
        for s in sibs[:k]:
            if self.compound(s, target) and self._match_from(s, chain, i - 1):
                return True
        return False


class Stylesheet:
    """Rules indexed by the key of their rightmost compound, for fast lookup."""

    def __init__(self, rules: list[Rule]):
        self.by_id: dict = {}
        self.by_cls: dict = {}
        self.by_tag: dict = {}
        self.other: list = []
        for r in rules:
            last = r.selector[-1][1]
            key = next((x for x in last if x[0] == "id"), None) or next((x for x in last if x[0] == "cls"), None) \
                or next((x for x in last if x[0] == "tag"), None)
            if key is None:
                self.other.append(r)
            elif key[0] == "id":
                self.by_id.setdefault(key[1], []).append(r)
            elif key[0] == "cls":
                self.by_cls.setdefault(key[1], []).append(r)
            else:
                self.by_tag.setdefault(key[1], []).append(r)

    def candidates(self, el):
        out = list(self.other)
        out += self.by_tag.get(el.tag.lower(), ())
        i = el.get("id")
        if i:
            out += self.by_id.get(i, ())
        for c in el.get("class", "").split():
            out += self.by_cls.get(c, ())
        return out


_UA = None


def ua_rules():
    global _UA
    if _UA is None:
        _UA = parse_stylesheet(UA_CSS, start_order=-100000)
    return _UA


def compute_styles(root: ET.Element, author_css: str = "", user_css: str = "") -> dict:
    """Map every element under ``root`` to its computed style (a dict of property -> string)."""
    ua = Stylesheet(ua_rules())
    author = Stylesheet(parse_stylesheet(author_css) + parse_stylesheet(user_css, start_order=10**6))
    m = Matcher(root)
    styles: dict = {}

    def visit(el, inherited):
        if not isinstance(el.tag, str):
            return
        style = dict(inherited)
        decl_list = []
        for sheet, origin in ((ua, 0), (author, 1)):
            for r in sheet.candidates(el):
                if m.matches(el, r.selector):
                    for p, (v, imp) in r.decls.items():
                        decl_list.append(((origin + (2 if imp else 0)), r.specificity, r.order, p, v))
        inline = el.get("style")
        if inline:
            for p, (v, imp) in parse_declarations(inline).items():
                decl_list.append((1 + (2 if imp else 0), (1, 0, 0, 0), 0, p, v))
        # non-inherited properties reset to their initial values
        for p in list(style):
            if p not in INHERITED:
                del style[p]
        decl_list.sort(key=lambda d: (d[0], d[1], d[2]))
        for *_, p, v in decl_list:
            if v == "inherit":
                if p in inherited:
                    style[p] = inherited[p]
                continue
            style[p] = v
        _presentational(el, style)
        styles[el] = style
        for child in el:
            visit(child, style)

    visit(root, {})
    return styles


def _presentational(el, style):
    """HTML attributes that act like CSS (align, <font>, width on images)."""
    tag = el.tag
    align = el.get("align")
    if align and "text-align" not in style and tag not in ("img", "table"):
        style["text-align"] = align.lower()
    if tag == "font":
        if el.get("face"):
            style.setdefault("font-family", el.get("face"))
        size = el.get("size")
        if size:
            sizes = {"1": "x-small", "2": "small", "3": "medium", "4": "large", "5": "x-large", "6": "xx-large",
                     "7": "xx-large"}
            style["font-size"] = sizes.get(size.strip().lstrip("+-"), "medium")
        if el.get("color"):
            style["color"] = el.get("color")


ABS_SIZES = {"xx-small": 7, "x-small": 7.5, "small": 10, "medium": 12, "large": 13.5, "x-large": 18, "xx-large": 24}
UNIT_PT = {"pt": 1.0, "px": 0.75, "in": 72.0, "cm": 72 / 2.54, "mm": 72 / 25.4, "pc": 12.0, "q": 72 / 101.6}


def length(value: str | None, font_size: float = 12.0, percent_of: float | None = None,
           root_size: float = 12.0, default: float = 0.0) -> float:
    """A CSS length in points. Percentages need ``percent_of``."""
    if value is None:
        return default
    v = value.strip().lower()
    if not v or v in ("auto", "none", "normal", "initial", "inherit"):
        return default
    m = re.match(r"^([+-]?[\d.]+)\s*([a-z%]*)", v)
    if not m:
        return default
    try:
        num = float(m.group(1))
    except ValueError:
        return default
    unit = m.group(2)
    if unit == "" and num == 0:
        return 0.0
    if unit == "em":
        return num * font_size
    if unit == "ex":
        return num * font_size * 0.5
    if unit == "rem":
        return num * root_size
    if unit == "%":
        return num / 100 * (percent_of if percent_of is not None else font_size)
    if unit in UNIT_PT:
        return num * UNIT_PT[unit]
    if unit == "":
        return num * 0.75  # unitless (quirks): pixels
    return default


def font_size(value: str | None, parent: float, root: float = 12.0) -> float:
    if not value:
        return parent
    v = value.strip().lower()
    if v in ABS_SIZES:
        return ABS_SIZES[v] * root / 12
    if v == "smaller":
        return parent / 1.2
    if v == "larger":
        return parent * 1.2
    return length(v, parent, percent_of=parent, root_size=root, default=parent) or parent


def is_bold(style) -> bool:
    w = style.get("font-weight", "normal").lower()
    return w in ("bold", "bolder") or (w.isdigit() and int(w) >= 600)


def is_italic(style) -> bool:
    return style.get("font-style", "normal").lower() in ("italic", "oblique")


def is_mono(style) -> bool:
    fam = style.get("font-family", "").lower()
    return "mono" in fam or "courier" in fam or "consol" in fam
