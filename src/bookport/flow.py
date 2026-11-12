"""Flatten styled XHTML into blocks of styled runs, for writers of non-HTML formats.

The text, RTF, DOCX, FB2, PML, LRF, eReader and PDF writers all consume the same thing: a
sequence of ``Block``s (paragraphs, headings, list items, preformatted text, images, tables,
rules) holding ``Run``s of text with a style. CSS decides what is bold, a heading or hidden.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

from . import css as cssmod
from .book import Book, is_external, resolve
from .textwriter import number, table_rows

_WS = re.compile(r"[ \t\n\r\f]+")
_INVISIBLE = re.compile("[­\u200b⁠﻿]")


@dataclass
class Run:
    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    sup: bool = False
    sub: bool = False
    mono: bool = False
    smallcaps: bool = False
    size: float = 1.0  # relative to the body text
    color: str = ""
    href: str = ""  # "doc.xhtml#id" inside the book, or an external URL
    image: str = ""  # inline image href
    alt: str = ""  # its alt text
    anchors: tuple = ()  # "doc.xhtml#id" targets that start here
    math: ET.Element | None = None  # an equation (MathML); ``text`` holds it as TeX for writers without math
    note: str = ""  # a footnote call: the key of the note it calls (writers that keep notes set data-note on the <a>)

    def same_style(self, o: Run) -> bool:
        return (self.bold, self.italic, self.underline, self.strike, self.sup, self.sub, self.mono, self.smallcaps,
                self.size, self.color, self.href) == (o.bold, o.italic, o.underline, o.strike, o.sup, o.sub,
                                                      o.mono, o.smallcaps, o.size, o.color, o.href)


@dataclass
class Cell:
    runs: list[Run]
    header: bool = False
    colspan: int = 1
    rowspan: int = 1
    align: str = ""


@dataclass
class Block:
    kind: str = "p"  # p, h, pre, li, image, table, hr
    runs: list[Run] = field(default_factory=list)
    level: int = 0  # heading level 1-6
    align: str = ""  # left, right, center, justify
    indent: float = 0.0  # first-line indent, em
    margin_left: float = 0.0  # em
    margin_right: float = 0.0
    margin_top: float = 0.0  # em
    margin_bottom: float = 0.0
    size: float = 1.0
    marker: str = ""  # list marker: "•", "3."
    depth: int = 0  # list nesting
    anchors: list[str] = field(default_factory=list)
    image: str = ""
    alt: str = ""
    width: float = 0.0  # image width hint, points (0 = natural)
    height: float = 0.0
    rows: list[list[Cell]] = field(default_factory=list)
    page_break: bool = False  # starts on a new page
    keep_with_next: bool = False
    doc: str = ""
    quote: bool = False  # inside a blockquote
    offscreen: bool = False  # moved off the page by CSS (text kept for screen readers): skip on paged output
    line_height: float = 0.0  # multiple of the font size; 0 = writer default
    list_key: int = 0  # the list a list item (or a paragraph inside one) belongs to; 0 outside lists
    list_type: str = ""  # its CSS list-style-type: disc, decimal, lower-alpha, ...
    list_start: int = 1  # the number its first item has
    role: str = ""  # dt, dd, caption (of a table), figcaption

    def text(self) -> str:
        if self.kind == "table":
            return "\n".join("\t".join("".join(r.text for r in c.runs) for c in row) for row in self.rows)
        return "".join(r.text for r in self.runs)

    def images(self) -> list[str]:
        """Every image in the block, table cells included."""
        runs = [r for row in self.rows for c in row for r in c.runs] if self.kind == "table" else self.runs
        return ([self.image] if self.image else []) + [r.image for r in runs if r.image]


ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
         (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]


def roman(n: int) -> str:
    if not 0 < n < 4000:  # as CSS does: out of the range Roman numerals cover, the number in digits
        return str(n)
    out = []
    for v, s in ROMAN:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def marker_for(style_type: str, n: int) -> str:
    t = (style_type or "disc").lower()
    if t == "none":
        return ""
    if t == "decimal" or t == "decimal-leading-zero":
        return f"{n:02d}." if t.endswith("zero") else f"{n}."
    if t == "lower-roman":
        return roman(n) + "."
    if t == "upper-roman":
        return roman(n).upper() + "."
    if t in ("lower-alpha", "lower-latin"):
        return _alpha(n) + "."
    if t in ("upper-alpha", "upper-latin"):
        return _alpha(n).upper() + "."
    if t == "circle":
        return "◦"
    if t == "square":
        return "▪"
    return "•"


def _alpha(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return s


class _Walker:
    def __init__(self, book: Book | None, href: str, root: ET.Element, extra_css: str = ""):
        self.book = book
        self.href = href
        author = book.css_for(href, root) if book is not None else ""
        self.styles = cssmod.compute_styles(root, author, extra_css)
        self.blocks: list[Block] = []
        self.cur: list[Run] = []
        self.pending_anchors: list[str] = []
        self.root = root
        body = root.find("body")
        self.base_size = cssmod.font_size(self.styles.get(body, {}).get("font-size"), 12.0) if body is not None else 12.0

    # --- helpers ------------------------------------------------------------------------------
    def _run_style(self, el, style, base: Run) -> Run:
        r = replace(base, text="", image="", anchors=())
        r.bold = cssmod.is_bold(style)
        r.italic = cssmod.is_italic(style)
        deco = style.get("text-decoration", "")
        if el.tag in ("u", "ins") or "underline" in deco:
            r.underline = True
        if el.tag in ("s", "strike", "del") or "line-through" in deco:
            r.strike = True
        va = style.get("vertical-align", "")
        if va == "super":
            r.sup, r.sub = True, False
        elif va == "sub":
            r.sub, r.sup = True, False
        r.mono = cssmod.is_mono(style)
        r.smallcaps = style.get("font-variant", "") == "small-caps"
        r.color = style.get("color", "")
        if el.tag == "a" and el.get("href"):
            link = el.get("href")
            if is_external(link):
                r.href = link
            elif link.startswith("#"):
                r.href = f"{self.href}{link}"
            else:
                target = resolve(self.href, link)
                frag = link.split("#", 1)[1] if "#" in link else ""
                r.href = target + (f"#{frag}" if frag else "")
                if self.book is not None and target not in self.book.resources:
                    r.href = link  # not in the book: a relative web address, kept as written
        return r

    def _size(self, el) -> float:
        style = self.styles.get(el, {})
        return self._abs_size(el, style) / self.base_size

    def _abs_size(self, el, style):
        # computed font sizes are resolved top-down and memoized
        cache = self.__dict__.setdefault("_sizes", {})
        if el in cache:
            return cache[el]
        parent = self._parent(el)
        psize = self._abs_size(parent, self.styles.get(parent, {})) if parent is not None else 12.0
        size = cssmod.font_size(style.get("font-size"), psize)
        cache[el] = size
        return size

    def _parent(self, el):
        pm = self.__dict__.get("_pm")
        if pm is None:
            pm = self._pm = {c: p for p in self.root.iter() for c in p}
        return pm.get(el)

    def add_text(self, text: str, run: Run, pre: bool) -> None:
        if not text:
            return
        if not pre:
            text = _WS.sub(" ", text)
        text = _INVISIBLE.sub("", text)
        if not text:
            return
        anchors = tuple(self.pending_anchors)
        self.pending_anchors = []
        if self.cur and not anchors and not self.cur[-1].image and self.cur[-1].math is None and not self.cur[-1].note \
                and self.cur[-1].same_style(run) and self.cur[-1].text != "\n":
            self.cur[-1].text += text
        else:
            self.cur.append(replace(run, text=text, anchors=anchors))

    def flush(self, ctx: dict) -> None:
        anchors = [a for r in self.cur for a in r.anchors]
        runs = _trim(self.cur)
        self.cur = []
        if not runs:
            # ids on whitespace-only content belong to whatever comes next
            self.pending_anchors = anchors + self.pending_anchors
            return
        kept = {a for r in runs for a in r.anchors}
        lost = [a for a in anchors if a not in kept]
        if lost:
            runs[0] = replace(runs[0], anchors=tuple(lost) + runs[0].anchors)
        b = self._new_block(ctx)
        pictures = [r for r in runs if r.image]
        if len(pictures) == 1 and not pictures[0].text and all(not r.text for r in runs if not r.image):
            # one picture, plus empty runs that only carry ids: an image block
            runs = [replace(pictures[0], anchors=tuple(a for r in runs for a in r.anchors))]
        b.runs = runs
        if len(runs) == 1 and runs[0].image and not runs[0].text:
            b.kind = "image"
            b.image = runs[0].image
            b.alt = ctx.get("alt", "")
            b.width, b.height = ctx.get("img_size", (0.0, 0.0))
        self.blocks.append(b)
        ctx["first"] = False

    def _new_block(self, ctx) -> Block:
        b = Block(kind=ctx.get("kind", "p"), level=ctx.get("level", 0), align=ctx.get("align", ""),
                  indent=ctx.get("indent", 0.0), margin_left=ctx.get("margin_left", 0.0),
                  margin_right=ctx.get("margin_right", 0.0),
                  margin_top=ctx.get("margin_top", 0.0) if ctx.get("first", True) else 0.0,
                  margin_bottom=ctx.get("margin_bottom", 0.0),
                  size=ctx.get("size", 1.0), depth=ctx.get("depth", 0), doc=self.href,
                  quote=ctx.get("quote", False), line_height=ctx.get("line_height", 0.0),
                  offscreen=ctx.get("offscreen", False), list_key=ctx.get("list_key", 0),
                  list_type=ctx.get("list_type", ""), list_start=ctx.get("list_start", 1), role=ctx.get("role", ""))
        if ctx.get("marker") and ctx.get("first", True):
            b.marker = ctx["marker"]
        if ctx.get("page_break") and ctx.get("first", True):
            b.page_break = True
            ctx["page_break"] = False
        if b.kind == "li" and not ctx.get("first", True):
            b.kind = "p"
        b.keep_with_next = b.kind == "h"
        anchors = [a for r in b.runs for a in r.anchors]
        b.anchors = anchors
        return b

    # --- walking ------------------------------------------------------------------------------
    def walk(self) -> list[Block]:
        body = self.root.find("body")
        if body is None:
            return []
        ctx = {"kind": "p", "first": True}
        if body.get("id"):
            self.pending_anchors.append(f"{self.href}#{body.get('id')}")
        self._children(body, self._run_style(body, self.styles.get(body, {}), Run()), ctx, pre=False)
        self.flush(ctx)
        if self.pending_anchors:
            if self.blocks:
                self.blocks[-1].anchors.extend(self.pending_anchors)
            else:
                self.blocks.append(Block(kind="p", anchors=list(self.pending_anchors), doc=self.href))
        for b in self.blocks:
            b.anchors = list(dict.fromkeys([a for r in b.runs for a in r.anchors] + b.anchors))
        return self.blocks

    def _children(self, el, run, ctx, pre):
        self.add_text(el.text or "", run, pre)
        for child in el:
            self._element(child, run, ctx, pre)
            self.add_text(child.tail or "", run, pre)

    def _element(self, el, parent_run, ctx, pre):
        if not isinstance(el.tag, str):
            return
        style = self.styles.get(el, {})
        display = style.get("display", "inline")
        if display == "none" or style.get("visibility") == "hidden" and el.tag != "a":
            if el.get("id"):
                self.pending_anchors.append(f"{self.href}#{el.get('id')}")
            return
        if el.get("id"):
            self.pending_anchors.append(f"{self.href}#{el.get('id')}")
        if el.tag == "a" and el.get("data-note"):
            anchors = tuple(self.pending_anchors)
            self.pending_anchors = []
            self.cur.append(Run(text="", note=el.get("data-note", ""), anchors=anchors))
            return
        if el.tag == "a" and el.get("name") and not el.get("id"):
            self.pending_anchors.append(f"{self.href}#{el.get('name')}")
        tag = el.tag
        run = self._run_style(el, style, parent_run)
        run.size = self._size(el)
        if tag == "br":
            anchors = tuple(self.pending_anchors)
            self.pending_anchors = []
            self.cur.append(replace(run, text="\n", image="", anchors=anchors))
            return
        if tag in ("img", "image") or (tag == "svg" and el.find(".//image") is not None and display != "inline"):
            self._image(el, run, ctx, style)
            return
        if tag == "svg":
            img = el.find(".//image")
            if img is not None:
                self._image(img, run, ctx, style)
            return
        if tag == "math":
            from .mathml import to_tex
            tex = " ".join((el.get("alttext") or to_tex(el)).split())
            if tex:
                anchors = tuple(self.pending_anchors)
                self.pending_anchors = []
                self.cur.append(replace(run, text=tex, anchors=anchors, math=el))
            return
        if tag in ("object", "video", "audio", "iframe", "script", "style", "noscript"):
            self.add_text(el.get("alttext") or el.get("alt") or "", run, pre)
            return
        if tag == "hr":
            self.flush(ctx)
            self.blocks.append(self._new_block({**ctx, "kind": "hr", "first": True, "marker": ""}))
            return
        if tag == "table":  # a table also when a style sheet shows it as a block (to let it scroll)
            self.flush(ctx)
            self._table(el, ctx)
            return
        if display in ("block", "list-item", "table", "table-row", "table-cell", "table-caption", "flex", "grid",
                       "table-row-group", "table-header-group", "table-footer-group") or tag in ("p", "div"):
            self._block(el, style, run, ctx, display, pre)
            return
        self._children(el, run, ctx, pre or style.get("white-space", "").startswith("pre"))

    def _block(self, el, style, run, ctx, display, pre):
        self.flush(ctx)
        size = self._abs_size(el, style)
        rel = size / self.base_size
        sub = dict(ctx)
        sub["first"] = True
        sub["size"] = rel
        sub.pop("alt", None)
        # a list item's marker goes on its first block, also when that is a paragraph inside it
        sub["marker"] = ctx.get("marker", "") if ctx.get("kind") == "li" and ctx.get("first", True) \
            and el.tag not in ("ul", "ol", "dl", "menu", "dir", "table") else ""
        if _offscreen(style):
            sub["offscreen"] = True
        tag = el.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            sub["kind"], sub["level"] = "h", int(tag[1])
        elif style.get("white-space", "").startswith("pre") or tag == "pre":
            sub["kind"] = "pre"
            pre = True
        elif display == "list-item" or tag == "li":
            sub["kind"] = "li"
            sub["depth"] = ctx.get("depth", 0)
            sub["marker"] = self._marker(el, style)
        elif sub.get("kind") not in ("h", "pre", "li") or sub.get("kind") == "li":
            sub["kind"] = "p"
        if tag in ("dt", "dd", "figcaption", "caption", "figure"):
            sub["role"] = tag
        if tag in ("ul", "ol", "menu", "dir"):
            self._list_n = getattr(self, "_list_n", 0) + 1
            sub["list_key"] = self._list_n
            kind = style.get("list-style-type") or ("decimal" if tag == "ol" else "disc")
            if tag == "ol" and el.get("type"):
                kind = {"1": "decimal", "a": "lower-alpha", "A": "upper-alpha", "i": "lower-roman",
                        "I": "upper-roman"}.get(el.get("type"), kind)
            sub["list_type"] = kind
            sub["list_start"] = number(el.get("start"), 1, -10**9, 10**9) if tag == "ol" else 1
            sub["role"] = ""
        if tag in ("ul", "ol", "dl", "menu", "dir"):
            sub["depth"] = ctx.get("depth", 0) + 1
            if tag == "ol":
                sub["counter"] = number(el.get("start"), 1, -10**9, 10**9) - 1
            else:
                sub["counter"] = 0
            self._list_counters = getattr(self, "_list_counters", {})
            self._list_counters[el] = sub["counter"]
        if tag == "blockquote":
            sub["quote"] = True
        align = style.get("text-align", "")
        sub["align"] = {"start": "left", "end": "right", "-webkit-center": "center"}.get(align, align)
        sub["indent"] = cssmod.length(style.get("text-indent"), size, percent_of=size * 30) / size if style.get("text-indent") else 0.0
        ml = cssmod.length(style.get("margin-left"), size, percent_of=size * 30) + cssmod.length(style.get("padding-left"), size, percent_of=size * 30)
        mr = cssmod.length(style.get("margin-right"), size, percent_of=size * 30)
        sub["margin_left"] = ctx.get("margin_left", 0.0) + ml / self.base_size
        sub["margin_right"] = ctx.get("margin_right", 0.0) + mr / self.base_size
        sub["margin_top"] = cssmod.length(style.get("margin-top"), size, percent_of=size * 30) / self.base_size
        sub["margin_bottom"] = cssmod.length(style.get("margin-bottom"), size, percent_of=size * 30) / self.base_size
        lh = style.get("line-height", "")
        if lh and lh != "normal":
            try:
                sub["line_height"] = float(lh)
            except ValueError:
                sub["line_height"] = cssmod.length(lh, size, percent_of=size) / size if size else 0.0
        # a pending break goes to the first block written inside this element, and only to it
        sub["page_break"] = ctx.pop("page_break", False) or style.get("break-before") in ("page", "always", "left", "right")
        self._children(el, run, sub, pre)
        self.flush(sub)
        if sub.get("page_break"):
            # nothing inside took the break: it carries to whatever comes next
            ctx["page_break"] = True
        if style.get("break-after") in ("page", "always", "left", "right"):
            ctx["page_break"] = True
            ctx["first"] = True
        if not sub.get("first") and self.blocks:
            # margin-bottom belongs to the last block written inside this element
            self.blocks[-1].margin_bottom = max(self.blocks[-1].margin_bottom, sub["margin_bottom"])
        ctx["first"] = ctx.get("first", True) and sub.get("first", True)

    def _marker(self, el, style):
        parent = self._parent(el)
        counters = getattr(self, "_list_counters", {})
        if parent is not None and parent in counters:
            if el.get("value"):
                counters[parent] = number(el.get("value"), counters[parent] + 1, -10**9, 10**9) - 1
            counters[parent] += 1
            n = counters[parent]
        else:
            n = 1
        ptype = style.get("list-style-type") or ("decimal" if parent is not None and parent.tag == "ol" else "disc")
        if parent is not None and parent.tag == "ol" and parent.get("type"):
            ptype = {"1": "decimal", "a": "lower-alpha", "A": "upper-alpha", "i": "lower-roman",
                     "I": "upper-roman"}.get(parent.get("type"), ptype)
        return marker_for(ptype, n)

    def _image(self, el, run, ctx, style):
        src = el.get("src") or el.get("xlink:href") or el.get("href") or ""
        if not src or is_external(src) and not src.startswith("data:"):
            alt = el.get("alt", "")
            self.add_text(alt, run, False)
            return
        href = resolve(self.href, src)
        if self.book is not None and href not in self.book.resources:
            self.add_text(el.get("alt", ""), run, False)
            return
        anchors = tuple(self.pending_anchors)
        self.pending_anchors = []
        self.cur.append(replace(run, text="", image=href, alt=el.get("alt", ""), anchors=anchors))
        w = cssmod.length(style.get("width") or el.get("width"), 12.0, percent_of=None, default=0.0)
        h = cssmod.length(style.get("height") or el.get("height"), 12.0, percent_of=None, default=0.0)
        if "%" in (style.get("width") or el.get("width") or ""):
            w = 0.0
        if "%" in (style.get("height") or el.get("height") or ""):
            h = 0.0
        ctx["img_size"] = (w, h)
        ctx["alt"] = el.get("alt", "")

    def _table(self, table, ctx):
        rows: list[list[Cell]] = []
        caption = table.find("caption")
        if caption is not None:
            self._block(caption, self.styles.get(caption, {}), Run(), ctx, "block", False)
        for tr in table_rows(table):  # a table inside a cell is that cell's text, not more rows of this one
            row = []
            for cell in tr:
                if cell.tag not in ("td", "th"):
                    continue
                saved, self.cur = self.cur, []
                saved_blocks = len(self.blocks)
                st = self.styles.get(cell, {})
                run = self._run_style(cell, st, Run())
                cctx = {"kind": "p", "first": True}
                self._children(cell, run, cctx, False)
                # nested blocks inside a cell collapse into its text, one line each
                inner = self.blocks[saved_blocks:]
                del self.blocks[saved_blocks:]
                runs = []
                for b in inner:
                    if b.kind == "table":
                        for r in b.rows:
                            for k, c in enumerate(r):
                                runs.extend(([Run(text=" ")] if k else []) + c.runs)
                            runs.append(Run(text="\n"))
                        continue
                    runs.extend(b.runs)
                    runs.append(Run(text="\n"))
                runs.extend(self.cur)
                self.cur = saved
                row.append(Cell(_trim(runs), header=cell.tag == "th", colspan=number(cell.get("colspan"), 1, 1, 1000),
                                rowspan=number(cell.get("rowspan"), 1, 1, 65534), align=st.get("text-align", "")))
            if row:
                rows.append(row)
        if rows:
            b = self._new_block({**ctx, "kind": "table", "first": True, "marker": ""})
            b.rows = rows
            b.anchors = list(self.pending_anchors)
            self.pending_anchors = []
            self.blocks.append(b)
            ctx["first"] = False


def _trim(runs: list[Run]) -> list[Run]:
    """Drop leading/trailing spaces (and empty runs) at the edges of a block; keep anchors."""
    runs = [r for r in runs if r.text or r.image or r.anchors or r.note]
    while runs and not runs[0].image and not runs[0].text.strip(" ") and not runs[0].anchors and not runs[0].note:
        runs.pop(0)
    while runs and not runs[-1].image and not runs[-1].text.strip(" \n") and not runs[-1].anchors and not runs[-1].note:
        runs.pop()
    if runs and not runs[0].image:
        runs[0] = replace(runs[0], text=runs[0].text.lstrip(" "))
    if runs and not runs[-1].image:
        runs[-1] = replace(runs[-1], text=runs[-1].text.rstrip(" "))
    # collapse a space after a line break
    for i in range(1, len(runs)):
        if runs[i - 1].text.endswith(("\n", " ")) and runs[i].text.startswith(" "):
            runs[i] = replace(runs[i], text=runs[i].text.lstrip(" "))
    if not any(r.text.strip() or r.image or r.note for r in runs):
        return []
    return runs


def document_blocks(book: Book | None, href: str, root: ET.Element | None = None, extra_css: str = "") -> list[Block]:
    if root is None:
        root = book.tree(href)
    return _Walker(book, href, root, extra_css).walk()



def _offscreen(style: dict) -> bool:
    """position: absolute with a large negative offset, the usual way to hide text visually but not from readers."""
    if style.get("position") not in ("absolute", "fixed"):
        return False
    for side in ("left", "top", "right"):
        v = style.get(side, "")
        if v.startswith("-"):
            try:
                if cssmod.length(v, 12.0, default=0.0) < -500:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def visible(blocks: list[Block]) -> list[Block]:
    """The blocks a page shows: off-screen ones dropped, their anchors and page breaks passed on to what follows."""
    out: list[Block] = []
    anchors: list[str] = []
    page_break = False
    for b in blocks:
        if b.offscreen:
            anchors += b.anchors
            page_break = page_break or b.page_break
            continue
        if anchors or page_break:
            b.anchors = anchors + b.anchors
            b.page_break = b.page_break or page_break
            anchors, page_break = [], False
        out.append(b)
    return out


def book_blocks(book: Book, extra_css: str = "", page_breaks: bool = True) -> list[Block]:
    """Every spine document's blocks; each document after the first starts on a new page."""
    out: list[Block] = []
    for i, (href, _res) in enumerate(book.documents()):
        blocks = document_blocks(book, href, extra_css=extra_css)
        if not blocks:
            out.append(Block(kind="p", doc=href, anchors=[href], page_break=page_breaks and i > 0))
            continue
        blocks[0].anchors.insert(0, href)
        if page_breaks and i > 0:
            blocks[0].page_break = True
        out.extend(blocks)
    return out
