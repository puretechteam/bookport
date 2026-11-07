"""Org (Emacs Org mode): a reader that exports like Org's own HTML exporter, and a writer.

Headlines (TODO keywords, priorities, tags, COMMENT and :noexport: trees), ``#+OPTIONS``,
emphasis with Org's border rules, lists, tables, blocks, links (internal ones resolved the way Org
does: targets, names, headline text, custom ids), footnotes, LaTeX fragments, macros, citations.
``#+INCLUDE`` and ``#+SETUPFILE`` read local files through ``safe_path``.
"""

from __future__ import annotations

import functools
import html.entities
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, Metadata
from ..limits import MAX_DEPTH, safe_path
from ..textwriter import Table, write_text
from .rst import _BR, _C, _O, MarkupWriter, build_book, el, make_id, node_append, read_include, text_width, unwrap

MAX_MACRO_ROUNDS = 8
MAX_MACRO_GROWTH = 1_000_000
MAX_INLINE_DEPTH = 16

_HEADLINE = re.compile(r"^(\*+) +(.*?)\s*$")
_SCRIPT = re.compile(r"\*|[+-]?[\w.,\\]*[^\W_]")
_TAGS = re.compile(r"\s+(:(?:[\w@#%]+:)+)\s*$")
_PRIORITY = re.compile(r"^\[#([A-Za-z0-9])\]\s*")
_KEYWORD = re.compile(r"^\s*#\+([^\s:]+):(?:\s+(.*)|\s*$)")
_BEGIN = re.compile(r"^\s*#\+begin_(\S+)(?:\s+(.*?))?\s*$", re.IGNORECASE)
_DRAWER = re.compile(r"^\s*:([\w-]+):\s*$")
_END_DRAWER = re.compile(r"^\s*:end:\s*$", re.IGNORECASE)
_LIST = re.compile(r"^( *)([-+*]|\d+[.)]|[A-Za-z][.)])(?: +|$)")
_FIXED = re.compile(r"^\s*:(?: |$)")
_HR = re.compile(r"^\s*-{5,}\s*$")
_COMMENT = re.compile(r"^\s*#(?: |$)")
_FNDEF = re.compile(r"^\[fn:([^\]\s:]+)\]\s*(.*)$")
_PLANNING = re.compile(r"^\s*(?:(?:SCHEDULED|DEADLINE|CLOSED):|CLOCK:)")
_LATEX_ENV = re.compile(r"^\s*\\begin\{([A-Za-z*]+)\}")
_CHECKBOX = re.compile(r"^\[([ Xx-])\](?: +|$)")
_COUNTER = re.compile(r"^\[@(\d+|[A-Za-z])\]\s*")
_DESC = re.compile(r"^(.*?)\s+::(?:\s+|$)")
_COOKIE = re.compile(r"^<([lcr]?)(\d*)>$")
_MATH_ENVS = {"equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "eqnarray",
              "eqnarray*", "displaymath", "math", "flalign", "flalign*", "alignat", "alignat*"}
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tif", ".tiff", ".xpm", ".pbm")
_EMPH_PRE = set("-('\"{\u2018\u201c")
_EMPH_POST = set("-.,:!?;'\")}\\[\u2019\u201d")
_EMPH_TAGS = {"*": "strong", "/": "em", "_": "u", "+": "del", "=": "code", "~": "code"}
_TEX_ENTITIES = {"to": "\u2192", "leftarrow": "\u2190", "rightarrow": "\u2192", "Rightarrow": "\u21d2",
                 "Leftarrow": "\u21d0", "leftrightarrow": "\u2194", "infty": "\u221e", "pm": "\u00b1", "cdot": "\u22c5",
                 "ldots": "\u2026", "dots": "\u2026", "ell": "\u2113", "neq": "\u2260", "leq": "\u2264", "geq": "\u2265",
                 "approx": "\u2248", "in": "\u2208", "notin": "\u2209", "sum": "\u2211", "prod": "\u220f",
                 "partial": "\u2202", "nabla": "\u2207", "times": "\u00d7", "div": "\u00f7", "vert": "|", "vbar": "|",
                 "slash": "/", "backslash": "\\", "space": " ", "nbsp": "\u00a0", "shy": "\u00ad", "textbackslash": "\\",
                 "dollar": "$", "ast": "*", "under": "_", "hellip": "\u2026", "dash": "\u2013", "mdash": "\u2014",
                 "ndash": "\u2013", "S": "\u00a7", "P": "\u00b6", "textdegree": "\u00b0", "checkmark": "\u2713",
                 "smiley": "\u263a", "star": "\u22c6", "Box": "\u25a1", "Diamond": "\u25c7", "laquo": "\u00ab",
                 "raquo": "\u00bb", "deg": "\u00b0", "EUR": "\u20ac", "euro": "\u20ac", "pound": "\u00a3"}

_INL = re.compile(r"""
 (?P<snip>@@[\w-]+:)
|(?P<src>(?<![\w])src_[\w+-]+(?:\[[^\]\n]{0,200}\])?\{)
|(?P<lb>\\\\[ \t]*(?:\n|$))
|(?P<math1>\\\()
|(?P<math2>\\\[)
|(?P<dd>\$\$)
|(?P<d>\$)
|(?P<ent>\\[A-Za-z]+(?:\{\})?)
|(?P<shy>\\-)
|(?P<fn>\[fn:)
|(?P<cite>\[cite(?:/[\w/-]+)?:)
|(?P<link>\[\[)
|(?P<radio><<<)
|(?P<target><<)
|(?P<angle><(?:https?|ftp|mailto|file|doi|news):)
|(?P<ts>[<\[]\d{4}-\d{2}-\d{2}(?:\ [^\]>\n]{0,40})?[>\]](?:--[<\[]\d{4}-\d{2}-\d{2}(?:\ [^\]>\n]{0,40})?[>\]])?)
|(?P<url>(?<![\w/])(?:https?|ftp|mailto|doi|news|irc):[^\s()<>\[\]"']+)
|(?P<emph>[*/_=~+])
|(?P<sup>(?<=\S)\^)
|(?P<dash>---?|\.\.\.)
""", re.VERBOSE)


def _entity(name: str) -> str | None:
    return _TEX_ENTITIES.get(name) or html.entities.html5.get(name + ";")


class _Org:
    def __init__(self, base_dir, top, lost: list[str]):
        self.base_dirs = [base_dir]
        self.top = top
        self.lost = lost
        self.lost_kinds: set[str] = set()
        self.meta: dict[str, str] = {}
        self.options: dict[str, str] = {}
        self.macros: dict[str, str] = {}
        self.link_abbrevs: dict[str, str] = {}
        self.todo: set[str] = {"TODO"}
        self.done: set[str] = {"DONE"}
        self.exclude = {"noexport"}
        self.select: set[str] = set()
        self._first_headline = 0
        self.footdefs: dict[str, list[str]] = {}
        self.inline_notes = 0
        self.ids: set[str] = set()
        self.headings: list[tuple[str, ET.Element]] = []  # (plain title, heading)
        self.custom_ids: dict[str, str] = {}
        self.names: dict[str, str] = {}  # targets and #+NAME -> id
        self.pending: dict[str, str] = {}  # affiliated keywords for the next element
        self.numbers: list[int] = []
        self.toc_spots: list[tuple[ET.Element, int]] = []
        self.bibliography: list[str] = []
        self.macro_n: dict[str, int] = {}
        self.seen: list[Path] = []

    def drop(self, kind: str, what: str) -> None:
        if kind not in self.lost_kinds:
            self.lost_kinds.add(kind)
            self.lost.append(what)

    def opt(self, name: str, default: str) -> str:
        return self.options.get(name, default)

    # --- before parsing: includes, settings, footnote definitions -------------------------------
    def expand_includes(self, lines: list[str], base, depth: int = 0) -> list[str]:
        out: list[str] = []
        block = None
        for line in lines:
            if block is None:
                m = _BEGIN.match(line)
                if m:
                    block = m.group(1).lower()
            elif re.match(rf"^\s*#\+end_{re.escape(block)}\s*$", line, re.IGNORECASE):
                block = None
            m = _KEYWORD.match(line) if block is None and "#+" in line[:12] else None
            if m and m.group(1).lower() == "include":
                out += self.include(m.group(2) or "", base, depth)
            elif m and m.group(1).lower() == "setupfile":
                self.setupfile((m.group(2) or "").strip().strip('"'), base, depth)
            else:
                out.append(line)
        return out

    def include(self, spec: str, base, depth: int) -> list[str]:
        m = re.match(r'^\s*(?:"([^"]+)"|(\S+))\s*(.*)$', spec)
        if not m:
            return []
        name, rest = m.group(1) or m.group(2), m.group(3)
        if depth >= 8:
            self.lost.append(f"An include nested too deep was left out: {name}")
            return []
        target = name.split("::")[0]
        text = read_include(base, target, self.top, self.lost, self.seen)
        if text is None:
            return []
        lines = _lines(text)
        lm = re.search(r':lines\s+"(\d*)-(\d*)"', rest)
        if lm:
            a = int(lm.group(1)) - 1 if lm.group(1) else 0
            b = int(lm.group(2)) - 1 if lm.group(2) else len(lines)
            lines = lines[a:b]
        kind = rest.split()[0].lower() if rest.split() and not rest.startswith(":") else ""
        if kind in ("src", "example", "export", "quote", "verse", "center", "comment"):
            arg = rest.split(None, 2)[1] if kind in ("src", "export") and len(rest.split()) > 1 else ""
            arg = "" if arg.startswith(":") else arg
            return [f"#+begin_{kind} {arg}".rstrip(), *[("," + s if re.match(r"^\s*(\*|#\+)", s) else s) for s in lines],
                    f"#+end_{kind}"]
        mm = re.search(r":minlevel\s+(\d+)", rest)
        if mm:
            want = int(mm.group(1))
            levels = [len(h.group(1)) for s in lines if (h := _HEADLINE.match(s)) and h.group(2) is not None]
            shift = want - min(levels) if levels else 0
            if shift > 0:
                lines = [("*" * shift + s) if _HEADLINE.match(s) and s.startswith("*") else s for s in lines]
        p = safe_path(base, target, self.top)
        self.seen.append(p)
        try:
            return self.expand_includes(lines, p.parent, depth + 1)
        finally:
            self.seen.pop()

    def setupfile(self, name: str, base, depth: int) -> None:
        if re.match(r"^[a-z]+://", name):
            self.lost.append(f"A setup file on the web wasn't fetched: {name}")
            return
        text = read_include(base, name, self.top, self.lost, self.seen, "A setup file")
        if text is not None and depth < 8:
            self.settings(_lines(text))

    def settings(self, lines: list[str]) -> None:
        block = None
        for line in lines:
            if block is None:
                m = _BEGIN.match(line)
                if m:
                    block = m.group(1).lower()
                    continue
            elif re.match(rf"^\s*#\+end_{re.escape(block)}\s*$", line, re.IGNORECASE):
                block = None
                continue
            if block is not None or "#+" not in line[:12]:
                continue
            m = _KEYWORD.match(line)
            if not m:
                continue
            key, value = m.group(1).lower(), (m.group(2) or "").strip()
            if key == "macro":
                name, _, body = value.partition(" ")
                self.macros[name.lower()] = body.strip()
            elif key == "link":
                name, _, url = value.partition(" ")
                self.link_abbrevs[name] = url.strip()
            elif key in ("todo", "seq_todo", "typ_todo"):
                words = [w.split("(")[0] for w in value.split()]
                if "|" in words:
                    k = words.index("|")
                    self.todo |= set(words[:k])
                    self.done |= set(words[k + 1:])
                elif words:
                    self.todo |= set(words[:-1])
                    self.done.add(words[-1])
            elif key == "options":
                for m2 in re.finditer(r"(\S+?):(\([^)]*\)|\S+)", value):
                    self.options[m2.group(1)] = m2.group(2)
            elif key == "exclude_tags":
                self.exclude = set(value.split())
            elif key == "select_tags":
                self.select = set(value.split())
            elif key in ("title", "subtitle", "author", "date", "email", "language", "description", "keywords"):
                self.meta[key] = (self.meta.get(key, "") + " " + value).strip() if key == "title" else value
            elif key in ("bibliography", "bibliography_file"):
                self.bibliography.append(value.strip().strip('"'))

    def collect_footnotes(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        block = None
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if block is None:
                m = _BEGIN.match(line)
                if m:
                    block = m.group(1).lower()
            elif re.match(rf"^\s*#\+end_{re.escape(block)}\s*$", line, re.IGNORECASE):
                block = None
            m = _FNDEF.match(line) if block is None and line.startswith("[fn:") else None
            if not m:
                out.append(line)
                i += 1
                continue
            body = [m.group(2)]
            i += 1
            blanks = 0
            inner = None
            while i < n:
                s = lines[i]
                if inner is None:
                    bm = _BEGIN.match(s)
                    if bm:
                        inner = bm.group(1).lower()
                    elif _FNDEF.match(s) and s.startswith("[fn:") or _HEADLINE.match(s) and s.startswith("*") \
                            and _HEADLINE.match(s).group(2) is not None:
                        break
                elif re.match(rf"^\s*#\+end_{re.escape(inner)}\s*$", s, re.IGNORECASE):
                    inner = None
                if not s.strip():
                    blanks += 1
                    if blanks >= 2 and inner is None:
                        break
                else:
                    blanks = 0
                body.append(s)
                i += 1
            self.footdefs.setdefault(m.group(1), body)
            out.append("")
        return out

    # --- blocks ------------------------------------------------------------------------------
    def emit(self, parent: ET.Element, e: ET.Element) -> ET.Element:
        name = self.pending.pop("name", None)
        if name:
            if not e.get("id"):
                e.set("id", make_id(name, self.ids))
            self.names.setdefault(name.lower(), e.get("id"))
        cap = self.pending.pop("caption", None)
        if cap is not None and e.tag == "table":
            e.insert(0, el("caption", self.inline(cap)))
        elif cap is not None and e.tag in ("pre", "div", "blockquote"):
            parent.append(el("p", self.inline(cap), class_="caption"))
        self.pending.clear()
        parent.append(e)
        return e

    def document(self, lines: list[str], body: ET.Element) -> None:
        """The whole file: headlines (with the subtrees to leave out) and the sections between them.

        As in Org's exporter, headlines deeper than the ``H`` option (3) become items of a list."""
        i, n = 0, len(lines)
        h_opt = self.opt("H", "3")
        deepest = int(h_opt) if h_opt.isdigit() else 3
        selected = self._selected(lines)
        skip_level = 0
        skip_own = False
        chunk: list[str] = []
        container = body
        stack: list[tuple[int, ET.Element, ET.Element]] = []  # headlines as list items: (level, list, item)
        while i < n:
            line = lines[i]
            m = _HEADLINE.match(line) if line[:1] == "*" else None
            if m:
                level = len(m.group(1))
                if skip_level and level > skip_level:
                    i += 1
                    continue
                skip_level = 0
                if not skip_own:
                    self.blocks(chunk, container, 0)
                chunk = []
                at = i
                i, skip, h = self.headline(lines, i, level, m.group(2) or "")
                skip_own = selected is not None and at not in selected
                if skip:
                    skip_level = level
                    continue
                if h is None or skip_own:
                    continue
                if level <= deepest:
                    stack.clear()
                    container = body
                    body.append(h)
                    continue
                while stack and stack[-1][0] > level:
                    stack.pop()
                if stack and stack[-1][0] == level:
                    lst = stack[-1][1]
                else:
                    holder = stack[-1][2] if stack else body
                    lst = ET.SubElement(holder, "ol" if self.opt("num", "t") != "nil" else "ul")
                h.tag = "p"
                item = ET.SubElement(lst, "li")
                item.append(h)
                if stack and stack[-1][0] == level:
                    stack[-1] = (level, lst, item)
                else:
                    stack.append((level, lst, item))
                container = item
                continue
            if not skip_level and (selected is None or not skip_own) and not (selected is not None and not stack
                                                                              and container is body and not body.findall("*")
                                                                              and i < self._first_headline):
                chunk.append(line)
            i += 1
        if not skip_own:
            self.blocks(chunk, container, 0)

    def _selected(self, lines: list[str]) -> set[int] | None:
        """With SELECT_TAGS in use: the headlines to keep (tagged ones, their subtrees and ancestors)."""
        self._first_headline = len(lines)
        if not self.select:
            return None
        heads: list[tuple[int, int, set[str]]] = []
        for k, line in enumerate(lines):
            m = _HEADLINE.match(line) if line[:1] == "*" else None
            if m:
                tm = _TAGS.search(m.group(2) or "")
                heads.append((k, len(m.group(1)), set(tm.group(1).split(":")) - {""} if tm else set()))
        if not any(tags & self.select for _k, _l, tags in heads):
            return None
        self._first_headline = heads[0][0] if heads else len(lines)
        keep: set[int] = set()
        path: list[tuple[int, int]] = []
        inside = 0  # level of a selected headline whose subtree we're in
        for k, level, tags in heads:
            while path and path[-1][1] >= level:
                path.pop()
            if inside and level <= inside:
                inside = 0
            if inside or tags & self.select:
                keep.add(k)
                keep.update(p for p, _l in path)
                if not inside:
                    inside = level
            path.append((k, level))
        return keep

    def headline(self, lines: list[str], i: int, level: int, text: str) -> tuple[int, bool, ET.Element | None]:
        i += 1
        props: dict[str, str] = {}
        n = len(lines)
        # planning lines and the property drawer, right under the headline
        while i < n and _PLANNING.match(lines[i]):
            i += 1
        if i < n and re.match(r"^\s*:PROPERTIES:\s*$", lines[i], re.IGNORECASE):
            i += 1
            while i < n and not _END_DRAWER.match(lines[i]):
                pm = re.match(r"^\s*:([^\s:]+):\s*(.*)$", lines[i])
                if pm:
                    props[pm.group(1).upper()] = pm.group(2).strip()
                i += 1
            i += 1
        words = text.split(None, 1)
        todo = ""
        if words and (words[0] in self.todo or words[0] in self.done):
            todo, text = words[0], words[1] if len(words) > 1 else ""
        if text.startswith("COMMENT") and (len(text) == 7 or text[7] == " "):
            return i, True, None
        pri = _PRIORITY.match(text)
        if pri:
            text = text[pri.end():]
        tags: list[str] = []
        tm = _TAGS.search(text)
        if tm:
            tags = [t for t in tm.group(1).split(":") if t]
            text = text[:tm.start()]
        if any(t in self.exclude for t in tags):
            return i, True, None
        if text.strip() == "Footnotes" and not todo:
            # Org's footnote section: its definitions were taken out already; with nothing else in it, it goes
            k = i
            while k < n and not lines[k].strip():
                k += 1
            if k >= n or (lines[k][:1] == "*" and _HEADLINE.match(lines[k])):
                return i, False, None
        h = el(f"h{min(level, 6)}")
        if self.opt("num", "nil") not in ("nil", "") and (not self.opt("num", "t").isdigit() or level <= int(self.opt("num", "t"))):
            while len(self.numbers) < level:
                self.numbers.append(0)
            del self.numbers[level:]
            self.numbers[level - 1] += 1
            h.append(el("span", ".".join(str(x) for x in self.numbers) + " ", class_="section-number"))
        if todo and self.opt("todo", "t") != "nil":
            h.append(el("span", todo, class_=f"todo {todo}" if todo in self.todo else f"done {todo}"))
            h[-1].tail = " "
        if pri and self.opt("pri", "nil") != "nil":
            node_append(h, [f"[#{pri.group(1)}] "])
        node_append(h, self.inline(text.strip()))
        if tags and self.opt("tags", "t") not in ("nil", "not-in-toc"):
            for t in tags:
                node_append(h, ["\u00a0"])
                h.append(el("span", t, class_="tag"))
        title_only = " ".join(_text_without(h, ("section-number", "todo", "done", "tag")).split())
        hid = props.get("CUSTOM_ID") or ""
        h.set("id", make_id(hid or title_only or "section", self.ids) if not hid else hid)
        self.ids.add(h.get("id"))
        if hid:
            self.custom_ids[hid] = h.get("id")
        if props.get("ID"):
            self.custom_ids.setdefault("id:" + props["ID"], h.get("id"))
        self.headings.append((title_only, h))
        return i, False, h

    def blocks(self, lines: list[str], parent: ET.Element, depth: int) -> None:
        if depth > MAX_DEPTH:
            parent.append(el("pre", "\n".join(lines)))
            return
        i, n = 0, len(lines)
        para: list[str] = []

        def end_para():
            if para:
                text = "\n".join(s.strip() for s in para)
                para.clear()
                self.paragraph(text, parent)

        while i < n:
            line = lines[i]
            s = line.lstrip()
            if not s:
                end_para()
                self.pending.clear()
                i += 1
                continue
            c = s[0]
            if c == "#":
                if s.startswith("#+"):
                    bm = _BEGIN.match(line)
                    if bm:
                        end_para()
                        i = self.block(lines, i, bm.group(1).lower(), bm.group(2) or "", parent, depth)
                        continue
                    km = _KEYWORD.match(line)
                    if km:
                        end_para()
                        self.keyword(km.group(1).lower(), (km.group(2) or "").strip(), parent)
                        i += 1
                        continue
                elif _COMMENT.match(line):
                    end_para()
                    i += 1
                    continue
            if c == "|" and self.opt("|", "t") != "nil":
                end_para()
                j = i
                while j < n and lines[j].lstrip().startswith("|"):
                    j += 1
                self.table(lines[i:j], parent)
                i = j
                continue
            if c == "+" and s.startswith("+-") and s.rstrip().endswith("+"):
                end_para()
                j = i
                while j < n and lines[j].lstrip()[:1] in ("+", "|"):
                    j += 1
                self.emit(parent, el("pre", "\n".join(x.strip() for x in lines[i:j]), class_="table"))
                i = j
                continue
            if c == ":":
                if _FIXED.match(line):
                    end_para()
                    j = i
                    out = []
                    while j < n and _FIXED.match(lines[j]):
                        out.append(lines[j].lstrip()[2:])
                        j += 1
                    if self.opt(":", "t") != "nil":
                        self.emit(parent, el("pre", el("code", "\n".join(out))))
                    i = j
                    continue
                dm = _DRAWER.match(line)
                if dm:
                    j = i + 1
                    while j < n and not _END_DRAWER.match(lines[j]) and not (lines[j].startswith("*") and _HEADLINE.match(lines[j])):
                        j += 1
                    if j < n and _END_DRAWER.match(lines[j]):
                        end_para()
                        name = dm.group(1)
                        if name.upper() not in ("PROPERTIES", "LOGBOOK") and self.opt("d", "t") != "nil":
                            div = el("div", class_=f"{name} drawer")
                            self.blocks(_dedent(lines[i + 1:j]), div, depth + 1)
                            self.emit(parent, div)
                        i = j + 1
                        continue
            if c == "-" and _HR.match(line):
                end_para()
                self.emit(parent, el("hr"))
                i += 1
                continue
            if c == "\\" and _LATEX_ENV.match(line):
                end_para()
                i = self.latex_env(lines, i, parent)
                continue
            if _PLANNING.match(line):
                end_para()
                i += 1
                continue
            lm = _LIST.match(line)
            if lm and (lm.group(2) != "*" or lm.group(1)) and (not lm.group(2)[0].isalpha()):
                end_para()
                i = self.plain_list(lines, i, parent, depth)
                continue
            para.append(line)
            i += 1
        end_para()

    def paragraph(self, text: str, parent: ET.Element) -> None:
        nodes = self.inline(text)
        caption = self.pending.get("caption")
        only = [x for x in nodes if not (isinstance(x, str) and not x.strip())]
        if len(only) == 1 and not isinstance(only[0], str) and only[0].tag == "img" and caption is not None:
            self.pending.pop("caption")
            fig = el("figure", only[0])
            fig.append(el("figcaption", self.inline(caption)))
            self.emit(parent, fig)
            return
        if len(only) == 1 and not isinstance(only[0], str) and only[0].tag == "math" and only[0].get("display") == "block":
            self.emit(parent, el("p", only[0]))
            return
        self.emit(parent, el("p", nodes))

    def keyword(self, key: str, value: str, parent: ET.Element) -> None:
        if key in ("caption", "name", "label", "results", "header", "plot") or key.startswith("attr_"):
            if key in ("caption", "name"):
                self.pending[key] = value
            elif key == "label":
                self.pending["name"] = value
            return
        if key == "html":
            lead, elems = markup.parse_fragment(value)
            if lead.strip():
                self.emit(parent, el("p", lead.strip()))
            for e in elems:
                e.tail = None
                self.emit(parent, e)
        elif key in ("latex", "tex", "beamer"):
            self.emit(parent, el("pre", value, class_="raw", data_format="latex"))
        elif key == "toc":
            words = value.split()
            if words and words[0].lower() == "headlines":
                depth = int(words[1]) if len(words) > 1 and words[1].isdigit() else 99
                div = el("div", class_="table-of-contents")
                self.toc_spots.append((div, depth))
                parent.append(div)
        elif key == "print_bibliography":
            div = el("div", class_="references")
            parent.append(div)
        elif key in ("texinfo", "odt", "man", "md", "ascii", "texinfo_head"):
            self.drop("kw:" + key, f"Org #+{key.upper()}: lines are for that exporter only and were left out")

    def block(self, lines: list[str], i: int, kind: str, args: str, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        j = i + 1
        end = re.compile(rf"^\s*#\+end_{re.escape(kind)}\s*$", re.IGNORECASE)
        nest = 0
        verbatim = kind in ("src", "example", "export", "comment", "html", "latex")
        while j < n:
            if end.match(lines[j]):
                if not nest:
                    break
                nest -= 1
            elif not verbatim and re.match(rf"^\s*#\+begin_{re.escape(kind)}(?:\s|$)", lines[j], re.IGNORECASE):
                nest += 1
            j += 1
        if j >= n:
            # no end line: Org reads the line as a paragraph
            self.paragraph(lines[i].strip(), parent)
            return i + 1
        raw = lines[i + 1:j]
        content = [re.sub(r"^(\s*),(\*|,\*|#\+|,#\+)", r"\1\2", s) for s in raw]
        nxt = j + 1
        if kind == "src":
            words = args.split()
            lang = words[0] if words and not words[0].startswith((":", "-")) else ""
            exports = re.search(r":exports\s+(\w+)", args)
            if exports and exports.group(1) in ("none", "results"):
                self.pending.clear()
                return nxt
            code = "\n".join(_dedent(content))
            self.emit(parent, el("pre", el("code", code, class_=f"language-{lang}" if lang else None)))
        elif kind == "example":
            self.emit(parent, el("pre", el("code", "\n".join(_dedent(content)))))
        elif kind == "quote":
            q = el("blockquote")
            self.blocks(content, q, depth + 1)
            self.emit(parent, q)
        elif kind == "verse":
            div = el("div", class_="line-block verse")
            for k, s in enumerate(content):
                if k:
                    div.append(el("br"))
                lead = len(s) - len(s.lstrip(" "))
                node_append(div, ["\u00a0" * lead, *self.inline(s.strip())])
            self.emit(parent, div)
        elif kind == "center":
            div = el("div", class_="center")
            self.blocks(content, div, depth + 1)
            self.emit(parent, div)
        elif kind == "comment":
            self.pending.clear()
        elif kind in ("export", "html", "latex"):
            fmt = kind if kind != "export" else args.split()[0].lower() if args.split() else ""
            text = "\n".join(content)
            if fmt == "html":
                lead, elems = markup.parse_fragment(text)
                if lead.strip():
                    self.emit(parent, el("p", lead.strip()))
                for e in elems:
                    e.tail = None
                    self.emit(parent, e)
            elif fmt in ("latex", "tex", "beamer"):
                self.emit(parent, el("pre", text, class_="raw", data_format="latex"))
            else:
                self.drop("export:" + fmt, f"Org export blocks for {fmt or 'an unnamed exporter'} were left out "
                          "(they're for that exporter only)")
        else:
            div = el("div", class_=re.sub(r"[^\w-]", "-", kind))
            self.blocks(content, div, depth + 1)
            self.emit(parent, div)
        return nxt

    def latex_env(self, lines: list[str], i: int, parent: ET.Element) -> int:
        name = _LATEX_ENV.match(lines[i]).group(1)
        end = "\\end{" + name + "}"
        j = i
        n = len(lines)
        while j < n and end not in lines[j]:
            j += 1
        if j >= n:
            self.paragraph(lines[i].strip(), parent)
            return i + 1
        text = "\n".join(s.strip() for s in lines[i:j + 1])
        if name in _MATH_ENVS:
            self.emit(parent, el("p", mathml.from_tex(text, display=True)))
        else:
            self.emit(parent, el("pre", text, class_="raw", data_format="latex"))
        return j + 1

    def plain_list(self, lines: list[str], i: int, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        first = _LIST.match(lines[i])
        indent = len(first.group(1))
        ordered = first.group(2)[0].isdigit()
        items: list[tuple[str, list[str], bool]] = []  # (first line rest, body lines, loose)
        start = None
        while i < n:
            m = _LIST.match(lines[i])
            if not m or len(m.group(1)) != indent or m.group(2)[0].isdigit() != ordered or \
                    (m.group(2) == "*" and not m.group(1)):
                break
            rest = lines[i][m.end():]
            j = i + 1
            blanks = 0
            body: list[str] = []
            while j < n:
                s = lines[j]
                if not s.strip():
                    blanks += 1
                    if blanks >= 2:
                        break
                    body.append("")
                    j += 1
                    continue
                if len(s) - len(s.lstrip(" ")) > indent:
                    blanks = 0
                    body.append(s)
                    j += 1
                    continue
                break
            blank_after = bool(body and not body[-1].strip())
            while body and not body[-1].strip():
                body.pop()
            items.append((rest, _dedent(body), blank_after))
            i = j
            if blanks >= 2:
                break
        is_desc = not ordered and bool(_DESC.match(_CHECKBOX.sub("", items[0][0])))
        lst = el("dl" if is_desc else "ol" if ordered else "ul")
        for k, (rest, body, blank_after) in enumerate(items):
            cm = _COUNTER.match(rest)
            if cm:
                rest = rest[cm.end():]
                if k == 0 and ordered and cm.group(1).isdigit():
                    start = int(cm.group(1))
            box = _CHECKBOX.match(rest)
            mark = ""
            if box:
                rest = rest[box.end():]
                mark = {" ": "\u2610 ", "-": "\u2610 "}.get(box.group(1), "\u2612 ")
            if is_desc:
                dm = _DESC.match(rest)
                term, rest = (dm.group(1), rest[dm.end():]) if dm else ("", rest)
                lst.append(el("dt", [mark, *self.inline(term)] if mark else self.inline(term)))
                item = ET.SubElement(lst, "dd")
                mark = ""
            else:
                item = ET.SubElement(lst, "li")
            item_lines = [rest.strip(), *body]
            self.blocks(item_lines, item, depth + 1)
            if mark:
                first_p = item[0] if len(item) and item[0].tag == "p" else None
                if first_p is not None:
                    first_p.text = mark + (first_p.text or "")
                else:
                    item.text = mark
            if len(item) and item[0].tag == "p" and not item[0].get("id") and not (item.text or "").strip() \
                    and not _loose(item_lines, blank_after and k + 1 < len(items)):
                unwrap(item, item[0])
        if start is not None and start != 1:
            lst.set("start", str(start))
        self.emit(parent, lst)
        return i

    def table(self, rows: list[str], parent: ET.Element) -> None:
        parsed: list[list[str] | None] = []  # None: a rule
        for r in rows:
            r = r.strip()
            if r.startswith("|-"):
                parsed.append(None)
                continue
            cells = r[1:]
            cells = cells.removesuffix("|")
            parsed.append([c.strip() for c in cells.split("|")])
        aligns: dict[int, str] = {}
        body_rows: list[list[str] | None] = []
        for row in parsed:
            if row is not None and any(row) and all(_COOKIE.match(c) for c in row if c):
                for k, c in enumerate(row):
                    m = _COOKIE.match(c)
                    if m and m.group(1):
                        aligns[k] = {"l": "left", "c": "center", "r": "right"}[m.group(1)]
                continue
            if row is not None and row and row[0] == "/":
                continue
            body_rows.append(row)
        while body_rows and body_rows[0] is None:
            body_rows.pop(0)
        while body_rows and body_rows[-1] is None:
            body_rows.pop()
        head_n = 0
        if None in body_rows:
            k = body_rows.index(None)
            if any(r is not None for r in body_rows[k:]):
                head_n = k
        width = max((len(r) for r in body_rows if r is not None), default=0)
        table = el("table")
        thead = el("thead") if head_n else None
        tbody = el("tbody")
        for k, row in enumerate(body_rows):
            if row is None:
                continue
            in_head = k < head_n
            tr = ET.SubElement(thead if in_head else tbody, "tr")
            for c in range(width):
                cell = el("th" if in_head else "td", self.inline(row[c]) if c < len(row) else None)
                if c in aligns:
                    cell.set("style", f"text-align: {aligns[c]}")
                tr.append(cell)
        if thead is not None:
            table.append(thead)
        table.append(tbody)
        self.emit(parent, table)

    # --- inline ------------------------------------------------------------------------------------
    def expand_macros(self, text: str) -> str:
        if "{{{" not in text:
            return text
        start_len = len(text)
        for _ in range(MAX_MACRO_ROUNDS):
            new = re.sub(r"\{\{\{([\w-]+)(?:\((.{0,4000}?)\))?\}\}\}", self._macro, text, flags=re.DOTALL)
            if new == text:
                break
            text = new
            if len(text) > start_len + MAX_MACRO_GROWTH:
                self.drop("macro-growth", "Org macros that grow without end were left unexpanded")
                break
        return text

    def _macro(self, m) -> str:
        name = m.group(1).lower()
        args = _macro_args(m.group(2)) if m.group(2) is not None else []
        if name in ("title", "author", "date", "email", "subtitle"):
            return self.meta.get(name, "")
        if name == "n":
            key = args[0] if args else ""
            self.macro_n[key] = self.macro_n.get(key, 0) + 1
            return str(self.macro_n[key])
        if name == "keyword":
            return self.meta.get(args[0].lower(), "") if args else ""
        if name in ("input-file", "modification-time", "time", "property"):
            return ""
        body = self.macros.get(name)
        if body is None:
            self.drop("macro:" + name, f"Org macro {{{{{{{name}}}}}}} isn't defined in the file; its text was kept")
            if name in ("kbd", "key"):
                return "".join(f"~{a}~" if "~" not in a else a for a in args) or ""
            if name == "var":
                return " ".join(f"/{a}/" for a in args)
            return ", ".join(args) if args else m.group(0).replace("{{{", "{\u200b{{")
        if body.startswith("(eval"):
            self.drop("macro-eval", "Org macros that run Emacs Lisp aren't run; their arguments were kept")
            return " ".join(args)

        def arg(am):
            k = int(am.group(1)) - 1
            return args[k] if 0 <= k < len(args) else ""
        return re.sub(r"\$(\d)", arg, body)

    def inline(self, text: str, depth: int = 0) -> list:
        if depth == 0:
            text = self.expand_macros(text)
        out: list = []
        buf: list[str] = []
        pos, n = 0, len(text)
        ends: dict[str, list[int]] = {}
        sup_mode = self.opt("^", "t")

        def flush():
            if buf:
                out.append("".join(buf))
                buf.clear()

        def emph_end(ch: str, start: int) -> int:
            lst = ends.get(ch)
            if lst is None:
                lst = []
                k = text.find(ch, 1)
                while k > 0:
                    if not text[k - 1].isspace() and (k + 1 >= n or text[k + 1].isspace() or text[k + 1] in _EMPH_POST):
                        lst.append(k)
                    k = text.find(ch, k + 1)
                ends[ch] = lst
            from bisect import bisect_left
            idx = bisect_left(lst, start)
            return lst[idx] if idx < len(lst) else -1

        def bracket_end(start: int) -> int:
            """The ] that closes the [ just before ``start`` (nested brackets counted), or -1."""
            return _pairs(text, "[", "]").get(start - 1, -1)

        while pos < n:
            m = _INL.search(text, pos)
            if not m:
                buf.append(text[pos:])
                break
            s, e, kind = m.start(), m.end(), m.lastgroup
            buf.append(text[pos:s])
            pos = e
            tok = m.group()
            if kind == "emph":
                ch = tok
                before = text[s - 1] if s else " "
                ok_start = (s == 0 or before.isspace() or before in _EMPH_PRE) and e < n and not text[e].isspace()
                if self.opt("*", "t") != "nil" and ok_start:
                    k = emph_end(ch, e + 1)
                    if k > 0 and text.count("\n", e, k) <= 1:
                        inner = text[e:k]
                        flush()
                        if ch in "=~":
                            out.append(el("code", inner, class_="verbatim" if ch == "=" else None))
                        elif depth < MAX_INLINE_DEPTH:
                            out.append(el(_EMPH_TAGS[ch], self.inline(inner, depth + 1)))
                        else:
                            out.append(el(_EMPH_TAGS[ch], inner))
                        pos = k + 1
                        continue
                if ch == "_" and s > 0 and not before.isspace() and sup_mode != "nil":
                    got = self._subsup(text, e, sup_mode, depth)
                    if got:
                        flush()
                        node, pos = got
                        out.append(el("sub", node))
                        continue
                buf.append(tok)
                continue
            if kind == "sup":
                got = self._subsup(text, e, sup_mode, depth) if sup_mode != "nil" else None
                if got:
                    flush()
                    node, pos = got
                    out.append(el("sup", node))
                else:
                    buf.append(tok)
                continue
            if kind == "dash":
                buf.append({"---": "\u2014", "--": "\u2013", "...": "\u2026"}[tok] if self.opt("-", "t") != "nil" else tok)
                continue
            if kind == "shy":
                buf.append("\u00ad" if self.opt("-", "t") != "nil" else tok)
                continue
            if kind == "lb":
                flush()
                out.append(el("br"))
                out.append("\n")
                continue
            if kind == "ent":
                name = tok[1:].removesuffix("{}")
                ch = _entity(name) if self.opt("e", "t") != "nil" else None
                buf.append(ch if ch is not None else tok)
                continue
            if kind in ("math1", "math2", "dd", "d"):
                got = self._math(text, s, e, kind)
                if got:
                    flush()
                    node, pos = got
                    out.append(node)
                else:
                    buf.append(tok)
                continue
            if kind == "snip":
                k = text.find("@@", e)
                if k < 0:
                    buf.append(tok)
                    continue
                backend = tok[2:-1].lower()
                value = text[e:k]
                pos = k + 2
                flush()
                if backend == "html":
                    lead, elems = markup.parse_fragment(value)
                    out.append(lead)
                    out += elems
                elif backend in ("latex", "tex", "beamer"):
                    out.append(el("span", value, class_="raw", data_format="latex"))
                else:
                    self.drop("snip:" + backend, f"Org export snippets for {backend} were left out "
                              "(they're for that exporter only)")
                continue
            if kind == "src":
                k = _brace_end(text, e)
                if k < 0:
                    buf.append(tok)
                    continue
                lang = re.match(r"src_([\w+-]+)", tok).group(1)
                flush()
                out.append(el("code", text[e:k], class_=f"language-{lang}"))
                pos = k + 1
                continue
            if kind == "fn":
                k = bracket_end(s + 1)
                if k < 0:
                    buf.append(tok)
                    continue
                inner = text[e:k]
                pos = k + 1
                flush()
                out.append(self.footnote_ref(inner))
                continue
            if kind == "cite":
                k = bracket_end(s + 1)
                if k < 0:
                    buf.append(tok)
                    continue
                pos = k + 1
                flush()
                out.append(self.citation(tok[6:-1], text[e:k]))
                continue
            if kind == "link":
                got = self._link(text, s, depth)
                if got:
                    flush()
                    node, pos = got
                    out.append(node)
                else:
                    buf.append(tok)
                continue
            if kind == "radio" or kind == "target":
                close = ">>>" if kind == "radio" else ">>"
                k = text.find(close, e, e + 200)
                name = text[e:k] if k > 0 else ""
                if k < 0 or not name.strip() or "\n" in name or "<" in name or ">" in name:
                    buf.append(tok)
                    continue
                pos = k + len(close)
                flush()
                tid = make_id(name, self.ids)
                self.names.setdefault(name.lower(), tid)
                out.append(el("span", name if kind == "radio" else "", id=tid))
                continue
            if kind == "angle":
                k = text.find(">", e, e + 2000)
                if k < 0:
                    buf.append(tok)
                    continue
                url = " ".join(text[s + 1:k].split())
                pos = k + 1
                flush()
                out.append(self._href(url, [url]))
                continue
            if kind == "url":
                url = tok
                while url and url[-1] in ".,;:!?)" and not (url[-1] == ")" and url.count("(") >= url.count(")")):
                    url = url[:-1]
                pos = s + len(url)
                flush()
                out.append(self._href(url, [url]))
                continue
            if kind == "ts":
                flush()
                out.append(el("span", tok, class_="timestamp"))
                continue
            buf.append(tok)
        flush()
        return out

    def _subsup(self, text: str, e: int, mode: str, depth: int):
        if e < len(text) and text[e] == "{":
            k = _brace_end(text, e + 1)
            if k < 0:
                return None
            return self.inline(text[e + 1:k], depth + 1) if depth < MAX_INLINE_DEPTH else text[e + 1:k], k + 1
        if mode == "{}":
            return None
        m = _SCRIPT.match(text, e)
        if not m or not m.group():
            return None
        return m.group(), m.end()

    def _math(self, text: str, s: int, e: int, kind: str):
        n = len(text)
        if kind in ("math1", "math2"):
            close = "\\)" if kind == "math1" else "\\]"
            k = text.find(close, e, e + 20000)
            if k < 0:
                return None
            return mathml.from_tex(text[e:k].strip(), display=kind == "math2"), k + 2
        if kind == "dd":
            k = text.find("$$", e, e + 20000)
            if k < 0:
                return None
            return mathml.from_tex(text[e:k].strip(), display=True), k + 2
        # $...$: Org's rules (not after a $; no space inside the ends; a space, punctuation or the end after)
        if s > 0 and text[s - 1] == "$" or e >= n or text[e] in " \t\n,;.$":
            return None
        k = e
        limit = min(n, e + 5000)
        while True:
            k = text.find("$", k + 1 if k > e else e, limit)
            if k < 0:
                return None
            if text[k - 1] not in " \t\n,.$" and (k + 1 >= n or text[k + 1] in " \t\n-.,?;:'\")"):
                break
            if text[k - 1] in " \t\n" and k == e:
                return None
        body = text[e:k]
        if body.count("\n") > 2:
            return None
        return mathml.from_tex(body), k + 1

    def _link(self, text: str, s: int, depth: int):
        n = len(text)
        k = s + 2
        stop = min(n, s + 8000)
        while k < stop and text[k] != "]":
            if text[k] == "\\" and k + 1 < n:
                k += 2
                continue
            if text[k] == "[":
                return None
            k += 1
        if k >= stop:
            return None
        target = re.sub(r"\\([\[\]\\])", r"\1", " ".join(text[s + 2:k].split()))
        if k + 1 < n and text[k + 1] == "]":
            desc = None
            end = k + 2
        elif k + 1 < n and text[k + 1] == "[":
            d = text.find("]]", k + 2, stop)
            if d < 0:
                return None
            desc = text[k + 2:d]
            end = d + 2
        else:
            return None
        if desc is None:
            if self._is_image(target):
                return el("img", src=_file_path(target), alt=""), end
            shown = [target.removeprefix("file:").removeprefix("*") if not target.startswith("#") else target[1:]]
            if target.startswith(("http", "mailto:", "ftp:")):
                shown = [target]
        else:
            if self._is_image(desc.strip()):
                shown = [el("img", src=_file_path(desc.strip()), alt="")]
            else:
                shown = self.inline(desc, depth + 1) if depth < MAX_INLINE_DEPTH else [desc]
        return self._href(target, shown), end

    @staticmethod
    def _is_image(target: str) -> bool:
        t = target.removeprefix("file:").lower().split("::")[0]
        if t.startswith(("http://", "https://")):
            return t.endswith(_IMAGE_EXT)
        return t.endswith(_IMAGE_EXT) and (target.startswith("file:") or ":" not in t[:6] and not t.startswith("#")
                                            and not t.startswith("*"))

    def _href(self, target: str, shown: list) -> ET.Element:
        abbrev, colon, rest = target.partition(":")
        if colon and abbrev in self.link_abbrevs:
            url = self.link_abbrevs[abbrev]
            target = url.replace("%s", rest) if "%s" in url else url + rest
        low = target.lower()
        if re.match(r"^(?:https?|ftp|mailto|news|irc):", low):
            return el("a", shown, href=target)
        if low.startswith("doi:"):
            return el("a", shown, href="https://doi.org/" + target[4:])
        if low.startswith("info:"):
            manual, _, node = target[5:].partition("::")
            manual, _, n2 = manual.partition("#")
            node = node or n2
            url = f"https://www.gnu.org/software/emacs/manual/html_mono/{manual}.html"
            if node and node != "Top":
                url += "#" + re.sub(r"[^\w-]", "-", node.strip())
            return el("a", shown, href=url)
        if low.startswith(("elisp:", "shell:", "help:", "attachment:", "docview:", "bbdb:", "rmail:", "gnus:",
                           "mhe:", "irc:", "eww:", "man:", "woman:", "w3m:", "elfeed:", "notmuch:")):
            return el("span", shown)
        if low.startswith("file:") or "/" in target or target.startswith("."):
            path = _file_path(target)
            if path.lower().endswith(".org"):
                path = path[:-4] + ".html"
            return el("a", shown, href=path)
        # internal: resolved after the whole file is read
        return el("a", shown, data_org_link=target)

    def footnote_ref(self, inner: str) -> ET.Element:
        label, colon, definition = inner.partition(":")
        if not colon and not label:
            label = ""
        if colon:
            if not label:
                self.inline_notes += 1
                label = f"\0anon{self.inline_notes}"
            self.footdefs.setdefault(label, [definition])
        ref = el("a", data_org_fn=label)
        return ref

    def citation(self, style: str, inner: str) -> ET.Element:
        """[cite/style:global prefix; pre @key suf; ...; global suffix], marked up as bookport.bib reads it."""
        parts = [p.strip() for p in inner.split(";")]
        prefix = parts.pop(0) if parts and "@" not in parts[0] else ""
        suffix = parts.pop() if parts and "@" not in parts[-1] else ""
        keys, notes = [], []
        for part in parts:
            m = re.match(r"^(.*?)-?@([\w:.#$%&+?<>~/-]+)(.*)$", part, re.DOTALL)
            if m:
                keys.append(m.group(2).rstrip(".,:"))
                notes.append(f"{' '.join(m.group(1).split())}|{' '.join(m.group(3).split())}")
        mode = {"t": "textual", "text": "textual", "a": "author", "author": "author", "na": "year", "noauthor": "year",
                "n": "nocite", "nocite": "nocite", "b": "bare", "bare": "bare"}.get(style.lstrip("/").split("/")[0], "normal")
        span = el("span", "(" + " ".join(inner.replace("@", "").split()) + ")", class_="citation",
                  data_cites=" ".join(keys))
        if mode != "normal":
            span.set("data-cite-mode", mode)
        if prefix:
            span.set("data-cite-prefix", prefix)
        if suffix:
            span.set("data-cite-suffix", suffix)
        if any(n != "|" for n in notes):
            span.set("data-cite-notes", ";".join(notes))
        return span

    # --- after the whole file is read -------------------------------------------------------------
    def resolve(self, body: ET.Element) -> None:
        titles: dict[str, str] = {}
        for title, h in self.headings:
            titles.setdefault(" ".join(title.split()).lower(), h.get("id"))
        parents = {c: p for p in body.iter() for c in p}
        for a in list(body.iter("a")):
            target = a.attrib.pop("data-org-link", None)
            if target is None:
                continue
            hid = None
            if target.startswith("*"):
                hid = titles.get(" ".join(target[1:].split()).lower())
            elif target.startswith("#"):
                # a custom id, or (as GitHub reads Org) a headline's own id
                hid = self.custom_ids.get(target[1:]) or (target[1:] if target[1:] in self.ids else None)
            elif target.startswith("id:"):
                hid = self.custom_ids.get(target)
            else:
                key = " ".join(target.split()).lower()
                hid = self.names.get(key) or titles.get(key)
            if hid:
                a.set("href", "#" + hid)
            elif a in parents:
                unwrap(parents[a], a)
        self._notes(body, parents)
        for div, depth in self.toc_spots:
            heads = [(int(h.tag[1]), t, h) for t, h in self.headings]
            if not heads:
                continue
            top = min(lv for lv, _t, _h in heads)
            root = el("ul")
            stack = [(top, root)]
            for level, _t, h in heads:
                if level - top + 1 > depth:
                    continue
                while level < stack[-1][0] and len(stack) > 1:
                    stack.pop()
                ul = stack[-1][1]
                while level > stack[-1][0]:
                    last = ul[-1] if len(ul) else ET.SubElement(ul, "li")
                    ul = ET.SubElement(last, "ul")
                    stack.append((stack[-1][0] + 1, ul))
                ET.SubElement(ul, "li").append(el("a", " ".join(markup.text_of(h).split()), href="#" + h.get("id")))
            div.append(root)

    def _notes(self, body: ET.Element, parents: dict) -> None:
        number: dict[str, int] = {}
        placed: dict[str, ET.Element] = {}
        refs = [a for a in body.iter("a") if a.get("data-org-fn") is not None]
        k = 0
        while k < len(refs):
            a = refs[k]
            k += 1
            label = a.attrib.pop("data-org-fn")
            if label not in self.footdefs:
                a.tag = "span"
                a.text = f"[fn:{label}]"
                continue
            if label not in number:
                number[label] = len(number) + 1
                aside = el("aside", epub__type="footnote", id=f"fn{number[label]}")
                lines = self.footdefs[label]
                if len(lines) == 1:
                    aside.append(el("p", self.inline(lines[0].strip())))
                else:
                    self.blocks(lines, aside, 1)
                placed[label] = aside
                # the note goes right after the top-level block that first calls it
                top = a
                while parents.get(top) is not None and parents[top] is not body:
                    top = parents[top]
                idx = list(body).index(top) + 1 if top in parents else len(body)
                while idx < len(body) and body[idx].tag == "aside":
                    idx += 1
                body.insert(idx, aside)
                parents[aside] = body
                for c in aside.iter():
                    for cc in c:
                        parents[cc] = c
                inner = [x for x in aside.iter("a") if x.get("data-org-fn") is not None]
                refs[k:k] = inner
            a.set("class", "noteref")
            a.set("epub:type", "noteref")
            a.set("href", f"#fn{number[label]}")
            a.append(el("sup", str(number[label])))
        unused = [lab for lab in self.footdefs if lab not in number and not lab.startswith("\0")]
        if unused:
            self.lost.append(f"{len(unused)} footnote definition(s) nothing refers to were left out, as Org's exporter "
                             f"does: {', '.join(unused[:5])}")


def _loose(lines: list[str], blank_after: bool) -> bool:
    """Whether an item's first paragraph is followed by a blank line inside the list (then it keeps its <p>)."""
    k = 0
    while k < len(lines) and (not lines[k].strip() or lines[k].lstrip().startswith("#")):
        k += 1
    while k < len(lines) and lines[k].strip() and not (k and _LIST.match(lines[k])):
        k += 1
    if k < len(lines):
        return not lines[k].strip()
    return blank_after


def _brace_end(text: str, start: int) -> int:
    """The } that closes the { just before ``start``, or -1."""
    return _pairs(text, "{", "}").get(start - 1, -1)


@functools.lru_cache(maxsize=16)
def _pairs(text: str, opener: str, closer: str) -> dict[int, int]:
    """Where each bracket of a text closes (backslash escapes skipped): one pass, so looking up is free."""
    out: dict[int, int] = {}
    stack: list[int] = []
    k, n = 0, len(text)
    while k < n:
        ch = text[k]
        if ch == "\\":
            k += 2
            continue
        if ch == opener:
            if len(stack) < 10000:
                stack.append(k)
        elif ch == closer and stack:
            out[stack.pop()] = k
        k += 1
    return out


def _macro_args(s: str) -> list[str]:
    out, cur, k = [], [], 0
    while k < len(s):
        ch = s[k]
        if ch == "\\" and k + 1 < len(s) and s[k + 1] in ",\\":
            cur.append(s[k + 1])
            k += 2
            continue
        if ch == ",":
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        k += 1
    out.append("".join(cur).strip())
    return out


def _file_path(target: str) -> str:
    t = target.removeprefix("file:")
    t = t.split("::")[0]
    return t.removeprefix("./")


def _text_without(h: ET.Element, classes: tuple[str, ...]) -> str:
    out = [h.text or ""]
    for c in h:
        if not any(x in classes for x in c.get("class", "").split()):
            out.append(markup.text_of(c))
        out.append(c.tail or "")
    return "".join(out)


def _lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    return [s.expandtabs(8).rstrip() for s in text.split("\n")]


def _dedent(lines: list[str]) -> list[str]:
    widths = [len(s) - len(s.lstrip(" ")) for s in lines if s.strip()]
    k = min(widths) if widths else 0
    return [s[k:] for s in lines] if k else list(lines)


def to_body(text: str, base_dir=None, top=None, lost: list[str] | None = None) -> tuple[ET.Element, dict, list[str]]:
    """Org text to a ``<body>`` element; also returns the document keywords and the bibliography files."""
    p = _Org(base_dir, top, lost if lost is not None else [])
    lines = _lines(text)
    lines = p.expand_includes(lines, base_dir)
    p.settings(lines)
    lines = p.collect_footnotes(lines)
    body = ET.Element("body")
    p.document(lines, body)
    p.resolve(body)
    return body, p.meta, p.bibliography


def read(path) -> Book:
    path = Path(path)
    text = markup.decode(path.read_bytes())
    lost: list[str] = []
    body, meta, bibs = to_body(text, path.parent, path.parent.parent, lost)
    md = Metadata(title=meta.get("title", "") or path.stem.replace("_", " "))
    if meta.get("author"):
        md.authors = [a.strip() for a in re.split(r"\s*(?:,|;| and )\s*", meta["author"]) if a.strip()]
    if meta.get("date"):
        md.date = meta["date"]
    if meta.get("language"):
        md.language = meta["language"]
    if meta.get("description"):
        md.description = meta["description"]
    book = build_book(body, md.title, "org", path.parent, path.parent.parent, lost, md)
    _bibliography(book, bibs, path.parent, lost)
    return book


def _bibliography(book: Book, names: list[str], base: Path, lost: list[str]) -> None:
    """Fill in the citations from the #+bibliography files (read through safe_path)."""
    from .. import bib
    paths = []
    for name in names:
        p = safe_path(base, name, base.parent)
        if p is None:
            lost.append(f"A bibliography outside the document's folder was left out: {name}")
        elif not p.is_file():
            lost.append(f"A bibliography file that isn't there was left out: {name}")
        else:
            paths.append(p)
    entries = bib.load(paths) if paths else {}
    if entries:
        bib.apply(book, entries)


# --- writing -----------------------------------------------------------------------------------

_ZW = "\u200b"  # Org's own way to keep text from being read as markup


class OrgWriter(MarkupWriter):
    inline_anchors = True

    def __init__(self, book: Book):
        super().__init__(book)
        self.note_bodies: list[tuple[str, str]] = []

    def header(self) -> str:
        md = self.book.metadata
        head = [f"#+title: {md.title}"] if md.title else []
        if md.authors:
            head.append(f"#+author: {', '.join(md.authors)}")
        if md.date:
            head.append(f"#+date: {md.date}")
        if md.language:
            head.append(f"#+language: {md.language}")
        if any(self.book.tree(h).find(f".//{t}") is not None for h, _ in self.book.documents() for t in ("h4", "h5", "h6")):
            head.append("#+options: H:6")  # Org makes headlines below level 3 list items unless told otherwise
        return "\n".join(head)

    def escape(self, s: str) -> str:
        # a zero-width space after a would-be marker keeps Org from reading text as markup
        for ch in "*/_=~+":
            if s.count(ch) >= 2:  # only a pair can be markup
                s = re.sub(rf"(?<![^\s\-('\"{{]){re.escape(ch)}(?=\S)", ch + _ZW, s)  # where Org could open markup
        s = re.sub(r"(?<=\S)([_^])(?=[^\W_]|\{)", "\\1" + _ZW, s)
        s = s.replace("[[", "[" + _ZW + "[").replace("[fn:", "[" + _ZW + "fn:").replace("{{{", "{" + _ZW + "{{")
        s = s.replace("@@", "@" + _ZW + "@").replace("<<", "<" + _ZW + "<").replace("[cite", "[" + _ZW + "cite")
        s = re.sub(r"\\(?=[A-Za-z(\[\\])", "\\\\" + _ZW, s)
        return s.replace("$", "\\dollar{}")

    def _markers(self, text: str) -> str:
        """Markers around emphasis become nothing, or a zero-width space where Org's border rules need one."""
        if _O not in text and _C not in text:
            return text
        out = []
        n = len(text)
        for k, ch in enumerate(text):
            if ch == _O:
                prev = next((text[j] for j in range(k - 1, -1, -1) if text[j] not in (_O, _C)), " ")
                if not (prev.isspace() or prev in _EMPH_PRE):
                    out.append(_ZW)
            elif ch == _C:
                nxt = next((text[j] for j in range(k + 1, n) if text[j] not in (_O, _C)), " ")
                if not (nxt.isspace() or nxt in _EMPH_POST):
                    out.append(_ZW)
            else:
                out.append(ch)
        return "".join(out)

    def _wrap(self, mark: str, s: str) -> str:
        return f"{_O}{mark}{s}{mark}{_C}"

    def _target(self, anchor: str | None) -> str:
        return f"<<{anchor}>>" if anchor else ""

    def doc_anchor(self, key: str) -> str:
        return f"<<{key}>>\n\n"

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        text = self._markers(text.strip())
        out = []
        for s in text.split("\n"):
            s = s.strip()
            if re.match(r"^(?:\*+ |[-+] |\d+[.)](?: |$)|#|:(?: |$)|:\w+:$|\||-{5,}$|\[fn:|\\begin\{)", s):
                s = _ZW + s
            out.append(s)
        return self._target(anchor) + "\n".join(out).replace(_BR, "\\\\\n")

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        text = self._markers(text).replace(_BR, " ").replace("\n", " ").strip() or _ZW
        out = "*" * level + " " + text
        if anchor:
            out += f"\n:PROPERTIES:\n:CUSTOM_ID: {anchor}\n:END:"
        return out

    def block(self, el_: ET.Element) -> str:
        cls = el_.get("class", "").split()
        anchor = self.anchor_key(el_)
        name = f"#+name: {anchor}\n" if anchor and el_.tag not in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "figure") else ""
        if el_.tag == "div" and "line-block" in cls:
            lines = self._markers(self.inlines(el_)).split(_BR)
            return name + "#+begin_verse\n" + "\n".join(s.strip().replace("\u00a0", " ") for s in lines) + "\n#+end_verse"
        if el_.tag == "div" and "admonition" in cls:
            kind = next((c for c in cls if c != "admonition"), "note")
            title = el_.find("p")
            inner = el("div")
            for c in el_:
                if not (c is title and "admonition-title" in c.get("class", "")):
                    inner.append(c)
            return name + f"#+begin_{kind}\n{self.blocks(inner)}\n#+end_{kind}"
        if el_.tag == "dl":
            self._dl_loose = any(c.tag == "dd" and any(x.tag == "p" for x in c) for c in el_)
        return name + super().block(el_)

    def rule(self) -> str:
        return "-----"

    def code_block(self, code: str, lang: str) -> str:
        code = re.sub(r"^(\s*)(\*|#\+)", r"\1,\2", code, flags=re.MULTILINE)
        if lang:
            return f"#+begin_src {lang}\n{code}\n#+end_src"
        return f"#+begin_example\n{code}\n#+end_example"

    def raw_block(self, text: str, fmt: str) -> str:
        if fmt in ("html", "latex", "tex"):
            return f"#+begin_export {'latex' if fmt == 'tex' else fmt}\n{text}\n#+end_export"
        return self.code_block(text, "")

    def quote(self, inner: str) -> str:
        return f"#+begin_quote\n{inner}\n#+end_quote"

    def list_item(self, li: ET.Element) -> str:
        # items whose text is a paragraph make a loose list (blank lines between the items)
        return ("\x05" if any(c.tag == "p" for c in li) else "") + super().list_item(li)

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        loose = any(i.startswith("\x05") for i in items)
        items = [i.removeprefix("\x05") for i in items]
        out = []
        for k, item in enumerate(items):
            marker = f"{start + k}." if ordered else "-"
            counter = f"[@{start}] " if ordered and k == 0 and start != 1 else ""
            item = re.sub(r"\n\n(?=(?:[-+]|\d+[.)]) )", "\n", self._markers(item).strip("\n"))  # a nested list goes right under
            lines = (item or _ZW).split("\n")
            pad = " " * (len(marker) + 1)
            out.append(f"{marker} {counter}{lines[0]}" + "".join("\n" + (pad + s if s.strip() else "") for s in lines[1:]))
        return ("\n\n" if loose or any("\n\n" in o for o in out) else "\n").join(out)

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        loose, self._dl_loose = getattr(self, "_dl_loose", False), False
        out = []
        for term, defs in pairs:
            term = self._markers(term).replace(_BR, " ").replace("\n", " ").strip().replace("::", ":" + _ZW + ":") or _ZW
            lines = "\n\n".join(self._markers(d) for d in defs if d.strip()).split("\n")
            out.append(f"- {term} :: {lines[0]}".rstrip() + "".join("\n" + ("  " + s if s.strip() else "") for s in lines[1:]))
        return ("\n\n" if loose or any("\n\n" in o for o in out) else "\n").join(out)

    def table(self, table: Table) -> str:
        if not table.rows:
            return ""
        width = table.width
        rows = []
        for r in table.rows:
            cells = []
            for c in r:
                cells.append(" ".join(self._markers(c.text).replace(_BR, " ").split()).replace("|", "\\vert{}"))
                cells += [""] * (c.colspan - 1)
            rows.append(cells + [""] * (width - len(cells)))
        widths = [max(text_width(r[k]) for r in rows) for k in range(width)]
        lines = ["| " + " | ".join(c + " " * (widths[k] - text_width(c)) for k, c in enumerate(r)) + " |" for r in rows]
        if table.header_rows and table.header_rows < len(lines):
            lines.insert(table.header_rows, "|" + "+".join("-" * (w + 2) for w in widths) + "|")
        cap = f"#+caption: {self._markers(table.caption)}\n" if table.caption else ""
        return cap + "\n".join(lines)

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        out = f"#+caption: {self._markers(caption).replace(_BR, ' ')}\n" if caption else ""
        if anchor:
            out += f"#+name: {anchor}\n"
        return out + self.image(src, alt)

    def math_block(self, el_: ET.Element) -> str:
        tex = mathml.to_tex(el_)
        return tex if tex.startswith("\\begin{") else "\\[\n" + tex + "\n\\]"

    def math_inline(self, el_: ET.Element) -> str:
        return "\\(" + mathml.to_tex(el_) + "\\)"

    def footnote(self, body: str, note_id: str) -> str:
        label = str(len(self.note_bodies) + 1)
        self.note_bodies.append((label, body))
        return f"[fn:{label}]"

    def postamble(self) -> str:
        return "\n\n".join(f"[fn:{label}] " + self._markers(b).strip() for label, b in self.note_bodies)

    def citation(self, keys: list[str], shown: str) -> str:
        return "[cite:" + ";".join("@" + k for k in keys) + "]"

    def line_break(self) -> str:
        return _BR

    def inline_anchor(self, key: str) -> str:
        return f"<<{key}>>"

    def emph(self, s: str) -> str:
        return self._wrap("/", s)

    def strong(self, s: str) -> str:
        return self._wrap("*", s)

    def strike(self, s: str) -> str:
        return self._wrap("+", s)

    def underline(self, s: str) -> str:
        return self._wrap("_", s)

    def sup(self, s: str) -> str:
        return f"^{{{s}}}"

    def sub(self, s: str) -> str:
        return f"_{{{s}}}"

    def code(self, s: str) -> str:
        s = " ".join(s.split("\n"))
        if not s.strip():
            return s
        mark = "~" if "~" not in s else "="
        if mark in s or s != s.strip():
            return self._wrap("=", s.replace("=", _ZW + "=" + _ZW))
        return self._wrap(mark, s)

    def raw_inline(self, text: str, fmt: str) -> str:
        return f"@@{fmt}:{text}@@" if fmt in ("html", "latex") else self.code(text)

    def link(self, target: str, text: str, external: bool) -> str:
        text = text.strip().replace("]]", "]" + _ZW + "]")
        if text.endswith("]"):
            text += _ZW
        t = target.replace("[", "%5B").replace("]", "%5D")
        if not external:
            if target not in self.anchored:
                return text
            t = ("#" + t) if t in self.heading_keys else t
        elif not re.match(r"^[a-z][\w+.-]*:", t):
            t = "file:" + t
        if not text or text.replace(_ZW, "") == target:
            return f"[[{t}]]"
        return f"[[{t}][{text}]]"

    def image(self, src: str, alt: str) -> str:
        if not src:
            return alt
        return f"[[{src if '://' in src else 'file:' + src}]]"


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    return write_text(OrgWriter(book), path, pictures)
