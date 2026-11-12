"""A base for writers of plain-text markup languages (reStructuredText, Org, AsciiDoc, LaTeX, ...).

It walks a book's XHTML the way every such writer needs to, and leaves the syntax to a subclass:
headings, paragraphs, lists, tables, code, quotes, definition lists, notes, math, links and
pictures arrive as calls with their content already written out.

What the readers put in the XHTML, and the writers expect (the book's conventions):

- headings ``h1``-``h6``; paragraphs ``p``; ``pre`` (with ``<code class="language-x">`` inside for
  a language) for code blocks; ``blockquote``; ``ul``/``ol`` (``start``) with ``li``; ``dl``
  with ``dt``/``dd``; ``hr``; ``table`` with ``caption``, ``thead``/``tbody``/``tr`` and
  ``th``/``td`` (``colspan``/``rowspan``); ``figure`` with ``img`` and ``figcaption``.
- inline: ``em``, ``strong``, ``code``, ``del``, ``u``, ``sup``, ``sub``, ``span class="smallcaps"``,
  ``a href``, ``img src alt``, ``br``.
- notes: the call is ``<a class="noteref" epub:type="noteref" href="#fnN"><sup>n</sup></a>`` and the
  note itself ``<aside epub:type="footnote" id="fnN">`` anywhere in the book (often a last
  "Notes" document). Writers put the note's text where the call is, or collect them.
- math: MathML ``<math display="block|inline">``, normally ``<semantics>`` with the presentation
  markup first and ``<annotation encoding="application/x-tex">`` holding the TeX it came from
  (see :mod:`bookport.mathml`). A block equation sits alone in a ``p`` or ``div``.
- citations: ``<span class="citation" data-cites="key1 key2">`` around the text shown for them;
  the reference list is ``<div class="references">`` with one ``<div class="csl-entry" id="ref-key">``
  per work (see :mod:`bookport.bib`).
- anything a reader could not turn into structure stays as text, in ``<span class="raw"
  data-format="latex">`` (inline) or ``<pre class="raw" data-format="latex">`` (block), so a writer
  for the same language can give it back unchanged and others show it as text.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import markup
from .book import Book, is_external, resolve

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
INLINE_EMPH = {"em": "emph", "i": "emph", "cite": "emph", "dfn": "emph", "var": "emph",
               "strong": "strong", "b": "strong", "del": "strike", "s": "strike", "strike": "strike",
               "u": "underline", "ins": "underline", "sup": "sup", "sub": "sub"}
CODE = {"code", "kbd", "samp", "tt"}
SKIP = {"script", "style", "head", "title"}
_SPACES = re.compile(r"[ \t\r\n\f]+")


@dataclass
class Cell:
    text: str
    header: bool = False
    colspan: int = 1
    rowspan: int = 1
    align: str = ""


@dataclass
class Table:
    rows: list[list[Cell]] = field(default_factory=list)
    caption: str = ""
    header_rows: int = 0  # how many leading rows are headings

    @property
    def width(self) -> int:
        return max((sum(c.colspan for c in r) for r in self.rows), default=0)


def is_note(el: ET.Element) -> bool:
    return el.tag == "aside" and el.get("epub:type", "") in ("footnote", "endnote", "rearnote", "note")


def is_noteref(el: ET.Element) -> bool:
    return el.tag == "a" and ("noteref" in el.get("class", "").split() or el.get("epub:type") == "noteref")


class TextWriter:
    """Subclass and override the ``*_block``/inline methods; call :meth:`write_book`."""

    image_dir = "images"
    collapse_space = True  # inline whitespace runs become one space

    def __init__(self, book: Book):
        self.book = book
        self.href = ""
        self.used_images: dict[str, str] = {}  # image href in the book -> name used in the text
        self._names: set[str] = set()  # the names in used_images
        self.out_dir = None  # the folder the text goes to (write_text sets it), so pictures don't overwrite files there
        self._writing_notes: set[int] = set()
        self.notes: dict[str, ET.Element] = {}  # "doc.xhtml#id" -> aside
        self.targets: set[str] = set()
        for href, _ in book.documents():
            root = book.tree(href)
            for el in root.iter():
                if el.tag == "aside" and is_note(el) and el.get("id"):
                    self.notes[f"{href}#{el.get('id')}"] = el
                elif el.tag == "a":
                    link = el.get("href", "")
                    if link and not is_external(link) and not is_noteref(el):
                        t = resolve(href, link)
                        frag = link.split("#", 1)[1] if "#" in link else ""
                        self.targets.add(f"{t}#{frag}" if frag else t)

    # --- driving ---------------------------------------------------------------------------------
    def write_book(self) -> str:
        parts = [self.preamble()]
        for href, _ in self.book.documents():
            self.href = href
            body = self.book.tree(href).find("body")
            if body is None:
                continue
            text = self.blocks(body)
            if text.strip():
                parts.append(text)
        parts.append(self.postamble())
        return self.join_document([p for p in parts if p and p.strip()])

    def join_document(self, parts: list[str]) -> str:
        # blank lines inside a part (a code block's) stay as they are
        return "\n\n".join(p.strip("\n") for p in parts).strip("\n") + "\n"

    def preamble(self) -> str:
        return ""

    def postamble(self) -> str:
        return ""

    def anchor_key(self, el: ET.Element) -> str | None:
        """The link target this element is, when something in the book links to it."""
        i = el.get("id")
        if i and f"{self.href}#{i}" in self.targets:
            return re.sub(r"[^\w-]", "_", f"{self.href.rsplit('/', 1)[-1].rsplit('.', 1)[0]}-{i}")
        return None

    def link_key(self, link: str) -> str:
        t = resolve(self.href, link)
        frag = link.split("#", 1)[1] if "#" in link else ""
        if not frag:
            return re.sub(r"[^\w-]", "_", t.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        return re.sub(r"[^\w-]", "_", f"{t.rsplit('/', 1)[-1].rsplit('.', 1)[0]}-{frag}")

    # --- blocks ------------------------------------------------------------------------------------
    def blocks(self, el: ET.Element) -> str:
        parts: list[str] = []
        inline: list[str] = []

        def flush():
            s = "".join(inline).strip()
            inline.clear()
            if s:
                parts.append(self.paragraph(s))

        if el.text and el.text.strip():
            inline.append(self.text(el.text))
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            if not tag or tag in SKIP:
                pass
            elif is_note(child):
                pass  # written where they're called
            elif tag in markup.BLOCK or tag in ("figure", "nav", "section", "aside", "math") and self._is_block(child):
                flush()
                block = self.block(child)
                if block and block.strip():
                    parts.append(block)
            else:
                inline.append(self.inline(child))
            if child.tail and (child.tail.strip() or inline):
                inline.append(self.text(child.tail))
        flush()
        return self.join_blocks(parts)

    def join_blocks(self, parts: list[str]) -> str:
        return "\n\n".join(p.strip("\n") for p in parts if p.strip())

    def _is_block(self, el: ET.Element) -> bool:
        return el.tag != "math" or el.get("display") == "block"

    def has_blocks(self, el: ET.Element) -> bool:
        return any(isinstance(c.tag, str) and (c.tag in markup.BLOCK or c.tag == "figure") for c in el)

    def block(self, el: ET.Element) -> str:
        tag = el.tag
        anchor = self.anchor_key(el)
        if tag in HEADINGS:
            return self.heading(HEADINGS[tag], " ".join(self.inlines(el).split()), anchor)
        if tag == "p":
            only = [c for c in el if isinstance(c.tag, str)]
            if len(only) == 1 and only[0].tag == "math" and only[0].get("display") == "block" \
                    and not (el.text or "").strip() and not (only[0].tail or "").strip():
                return self.math_block(only[0])
            return self.paragraph(self.inlines(el).strip(), anchor)
        if tag == "math":
            return self.math_block(el)
        if tag == "hr":
            return self.rule()
        if tag == "pre":
            if "raw" in el.get("class", "").split():
                return self.raw_block(markup.text_of(el), el.get("data-format", ""))
            code = el.find("code")
            lang = ""
            for c in ((code.get("class", "") if code is not None else "") + " " + el.get("class", "")).split():
                if c.startswith("language-"):
                    lang = c[9:]
            return self.code_block(markup.text_of(el).rstrip("\n"), lang)
        if tag == "blockquote":
            return self.quote(self.blocks(el))
        if tag in ("ul", "ol"):
            items = [self.list_item(li) for li in el if li.tag == "li"]
            return self.list_block(items, tag == "ol", number(el.get("start"), 1, -10**6, 10**6))
        if tag == "dl":
            pairs: list[tuple[str, list[str]]] = []
            for c in el:
                if c.tag == "dt":
                    pairs.append((" ".join(self.inlines(c).split()), []))
                elif c.tag == "dd":
                    if not pairs:
                        pairs.append(("", []))
                    pairs[-1][1].append(self.blocks(c) if self.has_blocks(c) else self.inlines(c).strip())
            return self.definition_list(pairs)
        if tag == "table":
            return self.table(self.read_table(el))
        if tag == "figure":
            img = el.find(".//img")
            cap = el.find("figcaption")
            caption = self.inlines(cap).strip() if cap is not None else ""
            if img is not None:
                return self.figure(self.image_name(img), img.get("alt", ""), caption, anchor)
            return self.blocks(el)
        if tag == "div" and "references" in el.get("class", "").split():
            return self.references(el)
        return self.blocks(el)

    def list_item(self, li: ET.Element) -> str:
        return self.blocks(li) if self.has_blocks(li) else self.inlines(li).strip()

    def read_table(self, el: ET.Element) -> Table:
        t = Table()
        cap = el.find("caption")
        if cap is not None:
            t.caption = " ".join(self.inlines(cap).split())
        for tr in table_rows(el):
            row = []
            for c in tr:
                if c.tag not in ("td", "th"):
                    continue
                align = c.get("align", "") or (re.search(r"text-align:\s*(\w+)", c.get("style", "")) or [None, ""])[1]
                text = self.blocks(c) if self.has_blocks(c) else self.inlines(c)
                row.append(Cell(" ".join(text.split()) if "\n" not in text.strip() else text.strip(),
                                c.tag == "th", number(c.get("colspan"), 1, 1, 50), number(c.get("rowspan"), 1, 1, 500),
                                align))
            t.rows.append(row)
        head = el.find("thead")
        if head is not None:
            t.header_rows = len(head.findall("tr"))
        elif t.rows and t.rows[0] and all(c.header for c in t.rows[0]):
            t.header_rows = 1
        return t

    # --- inline ------------------------------------------------------------------------------------
    def inlines(self, el: ET.Element) -> str:
        out = [self.text(el.text or "")]
        for c in el:
            if isinstance(c.tag, str) and not is_note(c):
                out.append(self.inline(c))
            if c.tail:
                out.append(self.text(c.tail))
        return "".join(out)

    def text(self, s: str) -> str:
        if self.collapse_space:
            s = _SPACES.sub(" ", s)  # not \s: no-break and thin spaces are text
        return self.escape(s)

    def inline(self, el: ET.Element) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        if not tag or tag in SKIP:
            return ""
        anchor = self.anchor_key(el)
        prefix = self.inline_anchor(anchor) if anchor else ""
        if tag == "br":
            return self.line_break()
        if tag == "math":
            return prefix + self.math_inline(el)
        if tag in INLINE_EMPH:
            inner = self.inlines(el)
            if not inner.strip():
                return prefix + inner
            lead, core, trail = _split_space(inner)
            return prefix + lead + getattr(self, INLINE_EMPH[tag])(core) + trail
        if tag in CODE:
            return prefix + self.code(markup.text_of(el))
        if tag == "span":
            classes = el.get("class", "").split()
            if "raw" in classes:
                return prefix + self.raw_inline(markup.text_of(el), el.get("data-format", ""))
            if "citation" in classes and el.get("data-cites"):
                return prefix + self.citation(el.get("data-cites", "").split(), self.inlines(el))
            if "smallcaps" in classes or "font-variant: small-caps" in el.get("style", ""):
                return prefix + self.smallcaps(self.inlines(el))
            return prefix + self.inlines(el)
        if tag == "a":
            if is_noteref(el):
                return self.note_call(el)
            link = el.get("href", "")
            inner = self.inlines(el)
            if not link:
                return prefix + inner
            if is_external(link) or link.startswith("mailto:"):
                return prefix + self.link(link, inner.strip(), external=True)
            return prefix + self.link(self.link_key(link), inner.strip(), external=False)
        if tag in ("img", "image"):
            name = self.image_name(el)
            return prefix + (self.image(name, el.get("alt", "")) if name else self.text(el.get("alt", "")))
        if tag == "svg":
            img = el.find(".//image")
            return self.inline(img) if img is not None else ""
        if tag in markup.BLOCK:
            return " " + " ".join(self.blocks(el).split()) + " "
        return prefix + self.inlines(el)

    def note_call(self, el: ET.Element) -> str:
        link = el.get("href", "")
        note = self.notes.get(f"{resolve(self.href, link.split('#')[0]) if link.split('#')[0] else self.href}#"
                              f"{link.split('#', 1)[1] if '#' in link else ''}")
        if note is None or id(note) in self._writing_notes:  # a note that calls itself, or one calling it
            return self.inlines(el)
        saved, self.href = self.href, self._href_of(note)
        self._writing_notes.add(id(note))
        try:
            body = self.blocks(note)
        finally:
            self.href = saved
            self._writing_notes.discard(id(note))
        return self.footnote(_drop_backlink(body), note.get("id", ""))

    def _href_of(self, note: ET.Element) -> str:
        for key, n in self.notes.items():
            if n is note:
                return key.split("#")[0]
        return self.href

    def image_name(self, el: ET.Element) -> str:
        """The name a picture gets beside the text (``images/x.png``), or its URL when it's not in the book."""
        src = el.get("src") or el.get("xlink:href") or el.get("href") or ""
        if not src:
            return ""
        if is_external(src) or src.startswith("data:"):
            return src if not src.startswith("data:") else ""
        target = resolve(self.href, src)
        name = self.used_images.get(target)
        if name is None:
            base = re.sub(r"[^\w.-]", "_", target.split("/")[-1])
            if not base.strip("."):  # "." or "..": never a name that leads out of the pictures' folder
                base = "picture"
            name, n = f"{self.image_dir}/{base}", 0
            while name in self._names or self._taken_on_disk(name, target):
                n += 1
                name = f"{self.image_dir}/{n}_{base}"
            self.used_images[target] = name
            self._names.add(name)
        return name

    def _taken_on_disk(self, name: str, target: str) -> bool:
        """A different file already has this name in the folder the text is written to: never overwrite it."""
        if self.out_dir is None:
            return False
        path = self.out_dir / name
        if not path.exists():
            return False
        res = self.book.resources.get(target)
        try:
            return res is None or not path.is_file() or path.stat().st_size != len(res.data) or path.read_bytes() != res.data
        except OSError:
            return True

    # --- what subclasses write ----------------------------------------------------------------------
    def escape(self, s: str) -> str:
        return s

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        return text

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        return text

    def rule(self) -> str:
        return "----"

    def code_block(self, code: str, lang: str) -> str:
        return code

    def raw_block(self, text: str, fmt: str) -> str:
        return self.code_block(text, "")

    def quote(self, inner: str) -> str:
        return inner

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        return "\n".join(items)

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        return "\n\n".join(t + "\n" + "\n".join(d) for t, d in pairs)

    def table(self, table: Table) -> str:
        return "\n".join(" | ".join(c.text for c in r) for r in table.rows)

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        return self.paragraph(self.image(src, alt or caption)) if src else caption

    def references(self, el: ET.Element) -> str:
        return self.blocks(el)

    def math_block(self, el: ET.Element) -> str:
        from . import mathml
        return mathml.to_tex(el)

    def math_inline(self, el: ET.Element) -> str:
        from . import mathml
        return mathml.to_tex(el)

    def footnote(self, body: str, note_id: str) -> str:
        return f" ({body.strip()})"

    def citation(self, keys: list[str], shown: str) -> str:
        return shown

    def line_break(self) -> str:
        return "\n"

    def inline_anchor(self, key: str) -> str:
        return ""

    def emph(self, s: str) -> str:
        return s

    def strong(self, s: str) -> str:
        return s

    def strike(self, s: str) -> str:
        return s

    def underline(self, s: str) -> str:
        return s

    def sup(self, s: str) -> str:
        return s

    def sub(self, s: str) -> str:
        return s

    def smallcaps(self, s: str) -> str:
        return s

    def code(self, s: str) -> str:
        return s

    def raw_inline(self, text: str, fmt: str) -> str:
        return self.code(text)

    def link(self, target: str, text: str, external: bool) -> str:
        return text

    def image(self, src: str, alt: str) -> str:
        return alt


def write_text(writer: TextWriter, path, pictures: bool = False) -> list[str]:
    """Write a book's text with ``writer`` to ``path`` (UTF-8, ``\\n`` line ends). With ``pictures``,
    the pictures the text refers to (``images/x.png``) are saved beside it, so the result opens with
    them; returns the names saved. Writers' ``write(book, path, pictures=False, **_)`` call this."""
    from pathlib import Path
    path = Path(path)
    if pictures:
        writer.out_dir = path.parent
    path.write_text(writer.write_book(), encoding="utf-8", newline="\n")
    saved = []
    if pictures:
        for href, name in writer.used_images.items():
            res = writer.book.resources.get(href)
            if res is None or "://" in name or name.startswith("data:"):
                continue
            target = path.parent / name
            if not target.exists():  # one that exists holds these very bytes (image_name made sure)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(res.data)
            saved.append(name)
    return saved


def table_rows(table: ET.Element) -> list[ET.Element]:
    """The rows of this table, not of tables inside its cells."""
    rows = []
    for c in table:
        if c.tag == "tr":
            rows.append(c)
        elif c.tag in ("thead", "tbody", "tfoot"):
            rows.extend(r for r in c if r.tag == "tr")
    return rows


def number(value, default: int, lo: int, hi: int) -> int:
    """An attribute's whole number (colspan, start), clamped; ``default`` when it isn't one ("²", "1e999", "")."""
    s = (value or "").strip()
    sign = -1 if s[:1] == "-" else 1
    s = s.lstrip("+-")
    if not (s.isascii() and s.isdigit()) or len(s) > 12:
        return default if not (s.isascii() and s.isdigit()) else (hi if sign > 0 else lo)
    return max(lo, min(hi, sign * int(s)))


def _split_space(s: str) -> tuple[str, str, str]:
    core = s.strip()
    if not core:
        return s, "", ""
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    return lead, core, trail


def _drop_backlink(text: str) -> str:
    # notes read from EPUB/DOCX often start with their own number or a "↩" link back
    return re.sub(r"^\s*(?:\[?\d+\]?\.?|↩︎?)\s+", "", text).replace("↩︎", "").replace("↩", "").strip()
