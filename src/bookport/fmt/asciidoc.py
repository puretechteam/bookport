"""AsciiDoc (the Asciidoctor dialect): a reader and a writer.

The reader follows Asciidoctor: a preprocessor first (``include::`` through ``safe_path`` with
``lines=``/``tag=``/``leveloffset=``, ``ifdef``/``ifndef``/``ifeval``, attribute entries), then
blocks (sections, delimited blocks, lists nested by marker, tables with column specs and spans,
admonitions, block titles with "Figure 1." style captions) and inline text (constrained and
unconstrained formatting, macros, cross references, footnotes, attribute references, replacements).
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, Metadata
from ..limits import MAX_DEPTH, safe_path
from ..textwriter import Table, write_text
from .org import _pairs
from .rst import _BR, MarkupWriter, build_book, el, node_append, read_include, unwrap

MAX_INCLUDE_DEPTH = 8
MAX_INLINE_DEPTH = 16
MAX_ATTR_GROWTH = 1_000_000
MAX_ATTR_VALUE = 65_536  # one attribute's value
MAX_TABLE_CELLS = 200_000  # grid places one table's spans and repeats may fill
MAX_TABLE_ROWS = 10_000
_ESCAPED_BRACE = ""  # \{ while attributes are put in (a private-use character, given back as "{")

_ATTR_ENTRY = re.compile(r"^:(!?)([\w][\w-]*)(!?):(?:[ \t]+(.*)|[ \t]*$)")
_SECTION_LINE = re.compile(r"^(={1,6}|#{1,6})[ \t]+(\S.*)$")


def _literal_code(inner: str) -> str:
    """Code between backticks: `+text+` (Asciidoctor's literal monospace) and ``pass:[text]`` as their text."""
    if len(inner) >= 2 and inner[0] == "+" and inner[-1] == "+":
        return inner[1:-1]
    if inner.startswith("pass:[") and inner.endswith("]"):
        return inner[6:-1].replace("\\]", "]")
    return inner


class _Section:
    """What a section title line matched: group(1) the marks, group(2) the title (closing marks taken off)."""

    __slots__ = ("_groups",)

    def __init__(self, groups: tuple):
        self._groups = groups

    def group(self, k: int) -> str:
        return self._groups[k]


def _section(line: str) -> _Section | None:
    # the closing marks are taken off here, not by the regex: "(\S.*?)(?:[ \t]+\1)?[ \t]*$" tried every
    # place in a long run of spaces (20,000 of them in one title took 20 s)
    m = _SECTION_LINE.match(line)
    if m is None:
        return None
    marks, title = m.group(1), m.group(2).rstrip()
    if title.endswith(marks) and title[:-len(marks)].endswith((" ", "\t")):
        title = title[:-len(marks)].rstrip()
    return _Section((line, marks, title))
_DELIM = re.compile(r"^(-{4,}|\.{4,}|={4,}|\*{4,}|_{4,}|\+{4,}|/{4,}|--|`{3}[\w+-]*|[|,:!]={3,})[ \t]*$")
_BLOCK_ATTR = re.compile(r"^\[(?:[^\[\]]|\[[^\[\]]*\])*\]$")
_ANCHOR_LINE = re.compile(r"^\[\[([\w:.-][^,\]]*)(?:,\s*([^\]]+))?\]\]$")
_TITLE = re.compile(r"^\.([^\s.].*)$")
_ADMONITION = re.compile(r"^(NOTE|TIP|IMPORTANT|CAUTION|WARNING):[ \t]+(.*)$", re.DOTALL)
_UL = re.compile(r"^[ \t]*(-|\*{1,5})[ \t]+(.*)$")
_OL = re.compile(r"^[ \t]*(\.{1,5}|\d+\.|[a-zA-Z]\.|[ivxIVX]+\))[ \t]+(.*)$")
_DL = re.compile(r"^[ \t]*(?!//)(\S.*?)(:{2,4}|;;)(?:[ \t]+(.*)|[ \t]*$)")
_COLIST = re.compile(r"^<(\d+|\.)>[ \t]+(.*)$")
_MACRO_BLOCK = re.compile(r"^(image|video|audio|toc)::([^\[\s]*)\[(.*)\]$")
_CHECK = re.compile(r"^\[([ xX*])\][ \t]+")
_COND = re.compile(r"^(\\)?(ifdef|ifndef|ifeval|endif)::([^\[]*)\[(.*)\]$")
_INCLUDE = re.compile(r"^(\\)?include::([^\[]+)\[(.*)\]$")
_ATTR_REF = re.compile(r"(\\)?\{([\w][\w-]*(?::[\w-]+(?::[^}]*)?)?)\}")
_ADMONITIONS = {"NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important", "CAUTION": "Caution", "WARNING": "Warning"}
_CELL_SPEC = re.compile(r"(?:^|(?<=\s))((?:\d+\*)?(?:\d*(?:\.\d+)?\+)?(?:[<^>])?(?:\.[<^>])?[adehlmsv]?)$")

_BUILTIN = {"empty": "", "blank": "", "sp": " ", "nbsp": "\u00a0", "zwsp": "\u200b", "wj": "\u2060", "apos": "'",
            "quot": '"', "lsquo": "\u2018", "rsquo": "\u2019", "ldquo": "\u201c", "rdquo": "\u201d",
            "deg": "\u00b0", "plus": "+", "brvbar": "\u00a6", "vbar": "|", "amp": "&", "lt": "<", "gt": ">",
            "startsb": "[", "endsb": "]", "caret": "^", "asterisk": "*", "tilde": "~", "backslash": "\\",
            "backtick": "`", "two-colons": "::", "two-semicolons": ";;", "cpp": "C++", "pp": "++", "cxx": "C++",
            "idprefix": "_", "idseparator": "_", "figure-caption": "Figure", "table-caption": "Table",
            "example-caption": "Example", "appendix-caption": "Appendix", "note-caption": "Note",
            "tip-caption": "Tip", "important-caption": "Important", "caution-caption": "Caution",
            "warning-caption": "Warning", "toc-title": "Table of Contents", "backend": "html5", "basebackend": "html",
            "backend-html5": "", "basebackend-html": "", "doctype": "article", "asciidoctor": "",
            "asciidoctor-version": "2.0.23", "safe-mode-name": "safe", "embedded": "", "outfilesuffix": ".html",
            "attribute-missing": "skip", "user-home": "."}

_INL = re.compile(r"""
 (?P<esc>\\(?=[*_`#^~+{\[<(=.-]|(?:https?|ftp|irc|mailto|link|image|kbd|btn|menu|footnote|footnoteref|xref|anchor|stem|latexmath|asciimath|pass|indexterm2?|icon):))
|(?P<pass3>\+\+\+)
|(?P<pass2>\+\+)
|(?P<pass1>\+)
|(?P<lb>[ \t]\+[ \t]*(?:\n|$))
|(?P<macro>(?<![\w:/])(?:link|mailto|image|kbd|btn|menu|footnote|footnoteref|xref|anchor|stem|latexmath|asciimath|pass|indexterm2?|icon):(?=[^\s\[]*\[))
|(?P<url>(?<![\w"'/=])(?:https?|ftp|irc|file)://[^\s\[\]<>"]+)
|(?P<angle><(?:https?|ftp|irc|mailto):[^\s>]+>)
|(?P<xref><<)
|(?P<anchor3>\[\[\[)
|(?P<anchor2>\[\[)
|(?P<role>\[[.#%][\w.#%-]*\](?=[*_`#]))
|(?P<idx3>\(\(\()
|(?P<idx2>\(\()
|(?P<quote>["']`)
|(?P<mark2>\*\*|__|``|\#\#)
|(?P<mark1>[*_`\#])
|(?P<supsub>[\^~])
|(?P<repl>\((?:C|R|TM)\)|--|\.\.\.|->|=>|<-|<=)
|(?P<apos>(?<=\w)'(?=\w))
|(?P<mail>(?<![\w.+-])[\w][\w.+-]*@[\w-]+(?:\.[\w-]+)+)
""", re.VERBOSE)
_MARK_TAG = {"*": "strong", "_": "em", "`": "code", "#": "mark"}
_REPL = {"(C)": "\u00a9", "(R)": "\u00ae", "(TM)": "\u2122", "...": "\u2026", "->": "\u2192", "=>": "\u21d2",
         "<-": "\u2190", "<=": "\u21d0"}


def _attrlist(text: str) -> tuple[list[str], dict[str, str]]:
    """Positional and named attributes of a block attribute line or a macro's brackets."""
    pos: list[str] = []
    named: dict[str, str] = {}
    for m in re.finditer(r'\s*(?:([\w-]+)\s*=\s*)?("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^,]*)\s*(?:,|$)', text):
        if m.start() == len(text) and m.group(0) == "":
            break
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].replace('\\"', '"').replace("\\'", "'")
        if key:
            named[key.lower()] = value
        else:
            pos.append(value)
        if m.end() >= len(text):
            break
    return pos, named


def _block_style(attrline: str) -> tuple[str, str, list[str], dict[str, str], set[str], list[str]]:
    """(style, id, positional, named, options, roles) of a block attribute line's content."""
    pos, named = _attrlist(attrline)
    style, bid, opts, roles = "", "", set(), []
    if pos:
        first = pos[0]
        m = re.match(r"^([^.#%]*)((?:[.#%][^.#%]*)*)$", first)
        if m:
            style = m.group(1).strip()
            for part in re.findall(r"[.#%][^.#%]*", m.group(2)):
                if part[0] == "#":
                    bid = part[1:]
                elif part[0] == ".":
                    roles.append(part[1:])
                else:
                    opts.add(part[1:])
    if named.get("id"):
        bid = named["id"]
    for o in re.split(r"[,\s]+", named.get("options", named.get("opts", ""))):
        if o:
            opts.add(o)
    if named.get("role"):
        roles += named["role"].split()
    return style, bid, pos, named, opts, roles


class _Adoc:
    def __init__(self, base_dir, top, lost: list[str], attrs: dict[str, str] | None = None):
        self.base_dir = base_dir
        self.top = top
        self.lost = lost
        self.lost_kinds: set[str] = set()
        self.attrs: dict[str, str] = dict(_BUILTIN)
        self.attrs.update(attrs or {})
        self.ids: set[str] = set()
        self.reftext: dict[str, str] = {}  # id -> text a cross reference shows
        self.notes: list[tuple[ET.Element, ET.Element]] = []  # (call, aside)
        self.note_ids: dict[str, tuple[int, ET.Element]] = {}
        self.counters: dict[str, int] = {}
        self.seen: list[Path] = []
        self.include_count: dict[str, int] = {}  # files and bytes included, all told
        self.level_shift = 0

    def drop(self, kind: str, what: str) -> None:
        if kind not in self.lost_kinds:
            self.lost_kinds.add(kind)
            self.lost.append(what)

    # --- preprocessor ----------------------------------------------------------------------------
    def preprocess(self, lines: list[str], base, depth: int = 0, offset: int = 0) -> list[str]:
        out: list[str] = []
        skipping: list[bool] = []  # one entry per open conditional: is its content left out?
        verbatim = None
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            i += 1
            m = _COND.match(line) if line[:2] in ("if", "en", "\\i", "\\e") else None
            if m:
                if m.group(1):
                    if not any(skipping):
                        out.append(line[1:])
                    continue
                kind, names, content = m.group(2), m.group(3), m.group(4)
                if kind == "endif":
                    if skipping:
                        skipping.pop()
                    continue
                if any(skipping):
                    if not content or kind == "ifeval":
                        skipping.append(True)
                    continue
                ok = self._condition(kind, names, content)
                if content and kind != "ifeval":
                    if ok:
                        out.append(content)
                    continue
                skipping.append(not ok)
                continue
            if any(skipping):
                continue
            if verbatim is None:
                dm = _DELIM.match(line)
                if dm and dm.group(1)[0] in "-.+/`" and dm.group(1) != "--":
                    verbatim = dm.group(1)
            elif line.rstrip() == verbatim or (verbatim.startswith("```") and line.rstrip() == "```"):
                verbatim = None
            m = _INCLUDE.match(line) if line[:1] in ("i", "\\") else None
            if m:
                if m.group(1):
                    out.append(line[1:])
                else:
                    out += self.include(m.group(2), m.group(3), base, depth, offset)
                continue
            if verbatim is None and line[:1] == ":":
                am = _ATTR_ENTRY.match(line)
                if am:
                    value = am.group(4) or ""
                    while value.endswith(" \\") and i < n:
                        value = value[:-2].rstrip() + " " + lines[i].strip()
                        i += 1
                    self.set_attr(am, value)
                    out.append(f":{am.group(1)}{am.group(2)}{am.group(3)}: {value}".rstrip())
                    continue
            if offset and verbatim is None:
                sm = _section(line)
                if sm and sm.group(1)[0] == "=":
                    lv = max(1, min(6, len(sm.group(1)) + offset))
                    line = "=" * lv + line[len(sm.group(1)):]
            out.append(line)
        return out

    def set_attr(self, m, value: str) -> None:
        name = m.group(2).lower()
        if m.group(1) or m.group(3):
            self.attrs.pop(name, None)
        else:
            value = self.subst(value) if "{" in value else value
            if len(value) > MAX_ATTR_VALUE:  # values made of values grew tenfold a line: 300 bytes made 10 MB
                self.drop("attr-size", "Attribute values longer than bookport keeps were cut short")
                value = value[:MAX_ATTR_VALUE]
            self.attrs[name] = value

    def _condition(self, kind: str, names: str, content: str) -> bool:
        if kind == "ifeval":
            return _ifeval(self.subst(content))
        if "+" in names:
            parts = [p.strip() for p in names.split("+") if p.strip()]
            res = all(p.lower() in self.attrs for p in parts)
        else:
            parts = [p.strip() for p in names.split(",") if p.strip()]
            res = any(p.lower() in self.attrs for p in parts)
        return res if kind == "ifdef" else not res

    def include(self, target: str, attrs: str, base, depth: int, offset: int) -> list[str]:
        target = self.subst(target)
        if re.match(r"^[a-z]+://", target):
            self.lost.append(f"An include from the web wasn't fetched: {target}")
            return []
        if "$" in target and not target.startswith((".", "/")):
            self.drop("antora:" + target.split("$")[0], f"Antora resource includes can't be found outside Antora: {target}")
            return []
        if depth >= MAX_INCLUDE_DEPTH:
            self.lost.append(f"An include nested more than {MAX_INCLUDE_DEPTH} deep was left out: {target}")
            return []
        text = read_include(base, target, self.top, self.lost, self.seen, count=self.include_count)
        if text is None:
            return []
        lines = _lines(text)
        _pos, named = _attrlist(attrs)
        if named.get("lines"):
            keep: list[str] = []
            for part in re.split(r"[;,]", named["lines"]):
                a, sep, b = part.strip().partition("..")
                b = b.strip()
                # "1..x" or "²" made int() fail, and with it the whole book
                if not re.fullmatch(r"-?\d{1,9}", a.strip(), re.ASCII) or b and not re.fullmatch(r"-?\d{1,9}", b, re.ASCII):
                    continue
                lo = int(a)
                hi = (len(lines) if not b or b == "-1" else int(b)) if sep else lo
                keep += lines[max(lo - 1, 0):hi]
            lines = keep
        tags = named.get("tag") or named.get("tags")
        if tags:
            lines = _tagged(lines, [t for t in re.split(r"[;,]", tags) if t])
        if markup.whole(named.get("indent", "")) is not None:
            k = markup.whole(named["indent"])
            ded = _dedent(lines)
            lines = [" " * k + s if s.strip() else s for s in ded]
        lo = named.get("leveloffset", "")
        new_offset = offset
        if re.fullmatch(r"[+-]?\d+", lo.strip()):
            new_offset = offset + int(lo) if lo.strip()[0] in "+-" else int(lo)
        p = safe_path(base, target, self.top)
        self.seen.append(p)
        try:
            return self.preprocess(lines, p.parent, depth + 1, new_offset)
        finally:
            self.seen.pop()

    def subst(self, text: str) -> str:
        """Attribute references (``{name}``); a missing one stays as written."""
        if "{" not in text:
            return text
        start = len(text)

        def rep(m):
            if m.group(1):  # \{name}: kept, also through the second pass below
                return _ESCAPED_BRACE + m.group(2) + "}"
            name = m.group(2)
            if ":" in name:
                kind, _, rest = name.partition(":")
                if kind in ("counter", "counter2"):
                    cname, _, init = rest.partition(":")
                    cur = self.counters.get(cname)
                    digits = init.isascii() and init.isdigit() and len(init) < 10
                    if cur is None:
                        v = int(init) if digits else (ord(init) if len(init) == 1 else 1)
                    else:
                        v = cur + 1
                    self.counters[cname] = v
                    shown = chr(min(v, 0x10FFFF)) if init and not digits else str(v)
                    self.attrs[cname] = shown
                    return shown if kind == "counter" else ""
                if kind == "set":
                    aname, _, val = rest.partition(":")
                    if aname.endswith("!"):
                        self.attrs.pop(aname[:-1].lower(), None)
                    else:
                        self.attrs[aname.lower()] = val
                    return ""
                return m.group(0)
            v = self.attrs.get(name.lower())
            return m.group(0) if v is None else v
        out = _ATTR_REF.sub(rep, text)
        if out != text and "{" in out and len(out) < start + MAX_ATTR_GROWTH:
            out = _ATTR_REF.sub(rep, out)  # an attribute's value can name another
        return out.replace(_ESCAPED_BRACE, "{")

    # --- blocks ------------------------------------------------------------------------------------
    def document(self, lines: list[str], body: ET.Element) -> None:
        i = self.header(lines)
        levels = [len(m.group(1)) for s in lines[i:] if s[:1] in "=#" and (m := _section(s))]
        self.level_shift = 1 if 1 in levels else 0  # book parts ("= Part") in the body: they are the h1s
        self.blocks(lines[i:], body, 0, top=True)

    def header(self, lines: list[str]) -> int:
        """The document header (title, author and revision lines, attribute entries); returns where the body starts."""
        i, n = 0, len(lines)
        while i < n and (not lines[i].strip() or lines[i].startswith("//") or _ATTR_ENTRY.match(lines[i])):
            am = _ATTR_ENTRY.match(lines[i])
            if am:
                self.set_attr(am, am.group(4) or "")
            i += 1
        k = i
        while k < n and (_ANCHOR_LINE.match(lines[k]) or _BLOCK_ATTR.match(lines[k])):
            k += 1
        m = _section(lines[k]) if k < n else None
        if not m or len(m.group(1)) != 1:
            return i
        title = m.group(2)
        self.attrs["doctitle"] = title
        i = k + 1
        if i < n and lines[i].strip() and not lines[i].startswith((":", "//")):
            authors = lines[i].strip()
            self.attrs["author"] = re.sub(r"\s*<[^>]*>", "", authors).split(";")[0].strip()
            self.attrs["authors"] = "; ".join(re.sub(r"\s*<[^>]*>", "", a).strip() for a in authors.split(";"))
            i += 1
            if i < n and lines[i].strip() and not lines[i].startswith((":", "//")):
                rev = lines[i].strip()
                rm = re.match(r"^v?([\d.]+)?,?\s*([^:]*)(?::\s*(.*))?$", rev)
                if rm and rm.group(2):
                    self.attrs["revdate"] = rm.group(2).strip()
                i += 1
        while i < n and lines[i].strip():
            am = _ATTR_ENTRY.match(lines[i])
            if am:
                self.set_attr(am, am.group(4) or "")
            i += 1
        return i

    def blocks(self, lines: list[str], parent: ET.Element, depth: int, top: bool = False) -> None:
        if depth > MAX_DEPTH:
            parent.append(el("pre", "\n".join(lines)))
            return
        i, n = 0, len(lines)
        meta: dict = {}
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            c = line[0]
            if c == "/" and line.startswith("//"):
                if line.startswith("////") and _DELIM.match(line):
                    j = i + 1
                    while j < n and lines[j].rstrip() != line.rstrip():
                        j += 1
                    i = j + 1
                    continue
                if not line.startswith("///"):
                    i += 1
                    continue
            if c == ":":
                am = _ATTR_ENTRY.match(line)
                if am:
                    self.set_attr(am, am.group(4) or "")
                    i += 1
                    continue
            if c == "[":
                am = _ANCHOR_LINE.match(line.rstrip())
                if am:
                    meta["id"] = am.group(1)
                    if am.group(2):
                        meta["reftext"] = am.group(2)
                    i += 1
                    continue
                if _BLOCK_ATTR.match(line.rstrip()) and not line.startswith("[["):
                    style, bid, pos, named, opts, _roles = _block_style(self.subst(line.rstrip()[1:-1]))
                    if style or not meta.get("style"):
                        meta["style"] = style
                    meta["pos"], meta["named"] = pos, named
                    meta.setdefault("opts", set()).update(opts)
                    if bid:
                        meta["id"] = bid
                    if named.get("reftext"):
                        meta["reftext"] = named["reftext"]
                    i += 1
                    continue
            if c == "." and _TITLE.match(line) and not _OL.match(line):
                meta["title"] = line[1:].strip()
                i += 1
                continue
            if c in "=#":
                sm = _section(line)
                if sm and not (c == "=" and _DELIM.match(line)):
                    self.section(sm, meta, parent)
                    meta = {}
                    i += 1
                    continue
            dm = _DELIM.match(line)
            if dm:
                i = self.delimited(lines, i, dm.group(1), meta, parent, depth)
                meta = {}
                continue
            if c in "itva" and "::" in line:
                mm = _MACRO_BLOCK.match(line.rstrip())
                if mm:
                    self.block_macro(mm, meta, parent)
                    meta = {}
                    i += 1
                    continue
            if line.rstrip() in ("'''", "---", "***", "- - -", "* * *"):
                self.emit(parent, el("hr"), meta)
                meta = {}
                i += 1
                continue
            if line.rstrip() == "<<<":
                meta = {}
                i += 1
                continue
            if self._item(line):
                k = self.list_block(lines, i, parent, depth, meta)
                if k > i:
                    meta = {}
                    i = k
                    continue
            if c in " \t" and not meta.get("style"):
                j = i
                while j < n and lines[j].strip():
                    j += 1
                self.emit(parent, el("pre", el("code", "\n".join(_dedent(lines[i:j])))), meta)
                meta = {}
                i = j
                continue
            j = i
            while j < n and lines[j].strip() and not (j > i and (_DELIM.match(lines[j]) or
                                                                   (_BLOCK_ATTR.match(lines[j].rstrip()) and lines[j][1:2] != "["))):
                if j > i and lines[j].startswith("//") and not lines[j].startswith("///"):
                    break
                j += 1
            para = [s for s in lines[i:j] if not (s.startswith("//") and not s.startswith("///"))]
            self.paragraph(para, meta, parent, depth)
            meta = {}
            i = j

    def emit(self, parent: ET.Element, e: ET.Element, meta: dict, title_ok: bool = True) -> ET.Element:
        if meta.get("id"):
            if not e.get("id"):
                e.set("id", meta["id"])
            self.ids.add(meta["id"])
            if meta.get("reftext"):
                self.reftext[meta["id"]] = meta["reftext"]
            elif meta.get("title"):
                self.reftext.setdefault(meta["id"], meta["title"])
        if title_ok and meta.get("title"):
            parent.append(el("p", self.inline(meta["title"]), class_="title"))
        parent.append(e)
        return e

    def caption(self, kind: str, title: str) -> list:
        """A block title with its caption ("Figure 3. ") when the document numbers that kind of block."""
        label = self.attrs.get(f"{kind}-caption")
        if label is None:
            return self.inline(title)
        self.counters[kind] = self.counters.get(kind, 0) + 1
        return [f"{label} {self.counters[kind]}. ", *self.inline(title)]

    def section(self, m, meta: dict, parent: ET.Element) -> None:
        marks, title = m.group(1), m.group(2)
        level = len(marks) - 1 + self.level_shift
        style = meta.get("style", "")
        h = el(f"h{max(1, min(level, 6))}")
        nodes = self.inline(title)
        # the id comes from these nodes: reading the title a second time ran {counter:...} and footnotes twice
        title_text = "".join(x if isinstance(x, str) else "".join(x.itertext()) + (x.tail or "") for x in nodes)
        if style == "appendix":
            self.counters["appendix"] = self.counters.get("appendix", 0) + 1
            nodes = [f"{self.attrs.get('appendix-caption', 'Appendix')} {chr(64 + min(self.counters['appendix'], 26))}: ",
                     *nodes]
        node_append(h, nodes)
        plain = " ".join(markup.text_of(h).split())
        hid = meta.get("id")
        if not hid:
            prefix = self.attrs.get("idprefix", "_")
            sep = self.attrs.get("idseparator", "_")
            base = re.sub(r"[^\w\s.-]+", "", " ".join(title_text.split()).lower())
            base = re.sub(r"[\s.-]+", sep or "_", base).strip(sep or "_")
            hid = prefix + base if base else prefix + "section"
            k, cand = 2, hid
            while cand in self.ids:
                cand = f"{hid}{sep}{k}"
                k += 1
            hid = cand
        h.set("id", hid)
        self.ids.add(hid)
        self.reftext.setdefault(hid, meta.get("reftext") or plain)
        parent.append(h)

    def paragraph(self, para: list[str], meta: dict, parent: ET.Element, depth: int) -> None:
        style = meta.get("style", "")
        text = "\n".join(s.strip() for s in para)
        am = _ADMONITION.match(text) if not style else None
        if am or style in _ADMONITIONS:
            kind = am.group(1) if am else style
            div = self._admonition(kind, meta)
            div.append(el("p", self.inline(am.group(2) if am else text)))
            self.emit(parent, div, meta, title_ok=False)
            return
        if style in ("source", "listing") or (style == "" and meta.get("pos") and meta["pos"][0] == ""
                                              and len(meta["pos"]) > 1):
            self.emit(parent, self._listing("\n".join(para), meta), meta)
            return
        if style == "literal":
            self.emit(parent, el("pre", el("code", "\n".join(_dedent(para)))), meta)
            return
        if style in ("quote", "verse"):
            q = self._quote(style, meta, ["\n".join(para)], depth)
            self.emit(parent, q, meta)
            return
        if style in ("stem", "latexmath", "asciimath"):
            self.emit(parent, el("p", self._math("\n".join(para), style, True)), meta)
            return
        if style == "pass":
            self._raw("\n".join(para), parent, meta)
            return
        if style in ("sidebar", "example", "abstract", "partintro"):
            div = el("div", class_=style)
            div.append(el("p", self.inline(text)))
            self.emit(parent, div, meta)
            return
        lines = [s.strip() for s in para]
        if "hardbreaks" in meta.get("opts", set()) or "hardbreaks" in self.attrs or "hardbreaks-option" in self.attrs:
            lines = [s + " +" if k < len(lines) - 1 and not s.endswith(" +") else s for k, s in enumerate(lines)]
        self.emit(parent, el("p", self.inline("\n".join(lines))), meta)

    def _admonition(self, kind: str, meta: dict) -> ET.Element:
        div = el("div", class_=f"admonition {kind.lower()}")
        div.append(el("p", self.attrs.get(f"{kind.lower()}-caption", _ADMONITIONS[kind]), class_="admonition-title"))
        if meta.get("title"):
            div.append(el("p", el("strong", self.inline(meta["title"])), class_="title"))
        return div

    def _listing(self, code: str, meta: dict) -> ET.Element:
        pos = meta.get("pos") or []
        lang = pos[1] if len(pos) > 1 and pos[0] in ("source", "") else meta.get("named", {}).get("language", "")
        if not lang and meta.get("style") == "source":
            lang = self.attrs.get("source-language", "")
        subs = meta.get("named", {}).get("subs", "")
        if "attributes" in subs:
            code = self.subst(code)
        return el("pre", el("code", code, class_=f"language-{lang.strip()}" if lang.strip() else None))

    def _quote(self, style: str, meta: dict, content: list[str], depth: int, block: bool = False) -> ET.Element:
        pos = meta.get("pos") or []
        attribution = meta.get("named", {}).get("attribution") or (pos[1] if len(pos) > 1 else "")
        citetitle = meta.get("named", {}).get("citetitle") or (pos[2] if len(pos) > 2 else "")
        if style == "verse":
            q = el("blockquote", class_="verse")
            div = el("div", class_="line-block")
            for k, s in enumerate("\n".join(content).strip("\n").split("\n")):
                if k:
                    div.append(el("br"))
                lead = len(s) - len(s.lstrip(" "))
                node_append(div, ["\u00a0" * lead, *self.inline(s.strip())])
            q.append(div)
        else:
            q = el("blockquote")
            if block:
                self.blocks(content, q, depth + 1)
            else:
                q.append(el("p", self.inline("\n".join(s.strip() for s in content))))
        if attribution or citetitle:
            who = [f"\u2014 {attribution}" if attribution else "\u2014"]
            if citetitle:
                who += [", " if attribution else " ", el("cite", self.inline(citetitle))]
            q.append(el("p", who, class_="attribution"))
        return q

    def _math(self, text: str, style: str, display: bool) -> ET.Element:
        kind = style if style != "stem" else (self.attrs.get("stem") or "asciimath")
        if kind not in ("latexmath", "tex"):
            self.drop("asciimath", "AsciiMath equations were read as TeX (bookport has no AsciiMath reader)")
        text = text.strip()
        if text.startswith("\\[") and text.endswith("\\]") or text.startswith("\\(") and text.endswith("\\)"):
            text = text[2:-2].strip()
        return mathml.from_tex(text, display=display)

    def _raw(self, text: str, parent: ET.Element, meta: dict) -> None:
        lead, elems = markup.parse_fragment(text)
        if lead.strip():
            self.emit(parent, el("p", lead.strip()), meta)
            meta = {}
        for e in elems:
            e.tail = None
            self.emit(parent, e, meta)
            meta = {}

    def block_macro(self, m, meta: dict, parent: ET.Element) -> None:
        kind, target, attrs = m.group(1), self.subst(m.group(2)), self.subst(m.group(3))
        pos, named = _attrlist(attrs)
        if kind == "toc":
            return
        if kind != "image":
            self.emit(parent, el("p", el("a", named.get("title") or target, href=target)), meta)
            return
        src = self._image_src(target)
        alt = named.get("alt") or (pos[0] if pos and pos[0] else _default_alt(target))
        img = el("img", src=src, alt=alt)
        node = el("a", img, href=named["link"]) if named.get("link") else img
        if meta.get("title"):
            fig = el("figure", node)
            fig.append(el("figcaption", self.caption("figure", meta["title"])))
            meta = {k: v for k, v in meta.items() if k != "title"}
            self.emit(parent, fig, meta)
        else:
            self.emit(parent, el("p", node), meta)

    def _image_src(self, target: str) -> str:
        if re.match(r"^[a-z]+:", target) or target.startswith("/"):
            return target
        d = self.attrs.get("imagesdir", "")
        return f"{d.rstrip('/')}/{target}" if d else target

    def delimited(self, lines: list[str], i: int, delim: str, meta: dict, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        close = "```" if delim.startswith("```") else delim
        j = i + 1
        while j < n and lines[j].rstrip() != close:
            j += 1
        content = lines[i + 1:j]
        nxt = j + 1
        style = meta.get("style", "")
        kind = delim[0]
        if delim.startswith("```"):
            lang = delim[3:].strip()
            self.emit(parent, el("pre", el("code", "\n".join(content), class_=f"language-{lang}" if lang else None)), meta)
        elif delim[1:2] == "=" and kind in "|,:!" and delim[1:].strip("=") == "":
            self.table(content, kind, meta, parent, depth)
        elif kind == "-" and delim != "--" or (kind == "-" and style in ("source", "listing")):
            if style in _ADMONITIONS:
                div = self._admonition(style, meta)
                self.blocks(content, div, depth + 1)
                self.emit(parent, div, meta, title_ok=False)
            else:
                self.emit(parent, self._listing("\n".join(content), meta), meta)
        elif kind == ".":
            if style in ("source", "listing"):
                self.emit(parent, self._listing("\n".join(content), meta), meta)
            else:
                self.emit(parent, el("pre", el("code", "\n".join(content))), meta)
        elif kind == "+":
            if style in ("stem", "latexmath", "asciimath"):
                self.emit(parent, el("p", self._math("\n".join(content), style, True)), meta)
            else:
                self._raw("\n".join(content), parent, meta)
        elif kind == "_" or (kind == "-" and style in ("quote", "verse")):
            self.emit(parent, self._quote(style or "quote", meta, content, depth, block=style != "verse"), meta)
        elif kind == "=" or kind == "*" or kind == "-":
            if style in _ADMONITIONS:
                div = self._admonition(style, meta)
                self.blocks(content, div, depth + 1)
                self.emit(parent, div, meta, title_ok=False)
            elif kind == "-" and style in ("stem", "latexmath", "asciimath"):
                self.emit(parent, el("p", self._math("\n".join(content), style, True)), meta)
            elif kind == "-" and style == "pass":
                self._raw("\n".join(content), parent, meta)
            else:
                cls = {"=": "example", "*": "sidebar"}.get(kind, style or "open")
                div = el("div", class_=cls)
                if meta.get("title"):
                    title = self.caption("example", meta["title"]) if kind == "=" else self.inline(meta["title"])
                    div.append(el("p", title, class_="title"))
                self.blocks(content, div, depth + 1)
                self.emit(parent, div, meta, title_ok=False)
        return nxt

    # --- lists ---------------------------------------------------------------------------------------
    def _item(self, line: str):
        """(key, kind, text, extra) when the line starts a list item."""
        m = _UL.match(line)
        if m:
            return m.group(1), "ul", m.group(2), None
        m = _OL.match(line)
        if m:
            mk = m.group(1)
            if mk[0] == ".":
                return mk, "ol", m.group(2), None
            key = "1." if mk[0].isdigit() else ("a." if mk[0].islower() else "A.") if mk.endswith(".") else "i)"
            return key, "ol", m.group(2), mk
        m = _COLIST.match(line)
        if m:
            return "<1>", "colist", m.group(2), None  # (never nested: a callout list follows its listing)
        if line[:1] not in (" ", "\t"):
            m = _DL.match(line)
            if m and not m.group(1).rstrip().endswith(":"):
                return m.group(2), "dl", m.group(3) or "", m.group(1).strip()
        return None

    def list_block(self, lines: list[str], i: int, parent: ET.Element, depth: int, meta: dict) -> int:
        n = len(lines)
        stack: list[list] = []  # [key, list element, current item]
        first = True
        while i < n:
            line = lines[i]
            got = self._item(line)
            if got:
                key, kind, text, extra = got
                idx = next((k for k in range(len(stack) - 1, -1, -1) if stack[k][0] == key), None)
                if idx is None and kind == "colist" and stack:
                    break
                if idx is not None:
                    del stack[idx + 1:]
                    lst = stack[-1][1]
                else:
                    if len(stack) >= 32:
                        break
                    lst = el("dl" if kind == "dl" else "ol" if kind in ("ol", "colist") else "ul")
                    if kind == "ol" and extra and extra[0].isdigit() and extra[:-1] != "1":
                        lst.set("start", extra[:-1])
                    elif kind == "ol" and extra and extra[0].isalpha():
                        lst.set("type", "a" if extra[0].islower() and not extra.endswith(")") else
                                "A" if extra.endswith(".") else "i")
                    if kind == "colist":
                        lst.set("class", "callout-list")
                    if stack:
                        stack[-1][2].append(lst)
                    elif first:
                        self.emit(parent, lst, meta)
                        first = False
                    else:
                        parent.append(lst)
                    stack.append([key, lst, None])
                i += 1
                # the item's own text: the lines that follow until a blank line, a "+" or another item
                more = []
                while i < n and lines[i].strip() and lines[i].strip() != "+" and not self._item(lines[i]) \
                        and not _DELIM.match(lines[i]) and not (_BLOCK_ATTR.match(lines[i].rstrip()) and lines[i][1:2] != "["):
                    if not (lines[i].startswith("//") and not lines[i].startswith("///")):
                        more.append(lines[i].strip())
                    i += 1
                if kind == "dl":
                    dt = el("dt", self.inline(extra))
                    lst.append(dt)
                    item = ET.SubElement(lst, "dd")
                    if not text.strip() and not more and i + 1 < n and not lines[i].strip() and lines[i + 1][:1] in " \t":
                        k = i + 1
                        while k < n and lines[k].strip():
                            more.append(lines[k].strip())
                            k += 1
                        i = k
                else:
                    item = ET.SubElement(lst, "li")
                body = "\n".join([text.strip(), *more]).strip()
                if kind == "ul":
                    cm = _CHECK.match(body)
                    if cm:
                        body = ("\u2612 " if cm.group(1) != " " else "\u2610 ") + body[cm.end():]
                if body:
                    item.append(el("p", self.inline(body)))
                stack[-1][2] = item
                continue
            if line.strip() == "+" and stack:
                # a list continuation: the next block belongs to the current item
                j = i + 1
                k = self._block_end(lines, j)
                self.blocks(lines[j:k], stack[-1][2], depth + 1)
                i = k
                continue
            if not line.strip():
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and self._item(lines[j]):
                    i = j
                    continue
                if j < n and lines[j][:1] in (" ", "\t") and stack:
                    k = j
                    while k < n and lines[k].strip():
                        k += 1
                    stack[-1][2].append(el("pre", el("code", "\n".join(_dedent(lines[j:k])))))
                    i = k
                    continue
                i = j
                break
            break
        return i

    def _block_end(self, lines: list[str], j: int) -> int:
        """Where the block that starts at line j (after its attribute and title lines) ends."""
        n = len(lines)
        while j < n and (_BLOCK_ATTR.match(lines[j].rstrip()) or _ANCHOR_LINE.match(lines[j].rstrip())
                         or (_TITLE.match(lines[j]) and not _OL.match(lines[j]))):
            j += 1
        if j < n:
            dm = _DELIM.match(lines[j])
            if dm:
                close = "```" if dm.group(1).startswith("```") else dm.group(1)
                k = j + 1
                while k < n and lines[k].rstrip() != close:
                    k += 1
                return min(k + 1, n)
        while j < n and lines[j].strip() and lines[j].strip() != "+" and not self._item(lines[j]):
            j += 1
        return j

    # --- tables ----------------------------------------------------------------------------------------
    def table(self, content: list[str], sep: str, meta: dict, parent: ET.Element, depth: int) -> None:
        named = meta.get("named", {})
        opts = meta.get("opts", set())
        content = [s for s in content if not (s.startswith("//") and not s.startswith("///"))]
        fmt = named.get("format", {"|": "psv", ",": "csv", ":": "dsv", "!": "psv"}[sep])
        specs = _col_specs(named.get("cols", ""))
        rows: list[list[dict]] = []
        if fmt in ("csv", "tsv", "dsv"):
            delim = named.get("separator") or {"csv": ",", "tsv": "\t", "dsv": ":"}[fmt]
            text = "\n".join(content)
            try:
                data = list(csv.reader(text.splitlines(), delimiter=delim[:1])) if fmt != "dsv" else \
                    [s.split(delim[:1]) for s in text.splitlines() if s.strip()]
            except csv.Error:
                data = [[s] for s in text.splitlines()]
            data = [r for r in data if r]
            width = len(specs) or (len(data[0]) if data else 0)
            cells = [{"text": c.strip(), "spec": ""} for r in data for c in (r + [""] * (width - len(r)))[:width]]
            implicit_header = False
        else:
            cell_sep = "!" if sep == "!" else named.get("separator", "|")[:1] or "|"
            cells, first_line_cells, implicit_header = _psv_cells(content, cell_sep)
            width = len(specs) or first_line_cells or 1
        if not specs:
            specs = [{"style": "", "align": ""} for _ in range(width)]
        # place the cells on the grid: spans, and rows filled by column count
        taken: dict[tuple[int, int], bool] = {}
        r = c = 0
        current: list[dict] = []
        for cell in cells:
            # repeats of tall cells fill rows of their own: "100*.500+|x" made 49,501 rows from 22 bytes
            if len(taken) > MAX_TABLE_CELLS or len(rows) > MAX_TABLE_ROWS:
                self.drop("table-size", "A table larger than bookport reads was cut short")
                break
            sm = re.match(r"^(?:(\d+)\*)?(?:(\d*)(?:\.(\d+))?\+)?([<^>])?(?:\.[<^>])?([adehlmsv])?$", cell["spec"])
            dup = int(sm.group(1)) if sm and sm.group(1) else 1
            cs = int(sm.group(2)) if sm and sm.group(2) else 1
            rs = int(sm.group(3)) if sm and sm.group(3) else 1
            style = sm.group(5) if sm and sm.group(5) else ""
            align = {"<": "left", "^": "center", ">": "right"}.get(sm.group(4) if sm else "", "")
            for _ in range(min(dup, 1000)):
                if len(taken) > MAX_TABLE_CELLS or len(rows) > MAX_TABLE_ROWS:
                    break
                while (r, c) in taken:
                    c += 1
                    if c >= width:
                        rows.append(current)
                        current = []
                        r, c = r + 1, 0
                cs2 = max(1, min(cs, width - c))
                current.append({**cell, "colspan": cs2, "rowspan": max(1, min(rs, 500)), "col": c, "style": style,
                                "align": align})
                for rr in range(r, r + max(1, min(rs, 500))):
                    for cc in range(c, c + cs2):
                        taken[(rr, cc)] = True
                c += cs2
                if c >= width:
                    rows.append(current)
                    current = []
                    r, c = r + 1, 0
        if current:
            rows.append(current)
        multiline = any("\n" in c["text"].strip() for row in rows for c in row)
        header = "header" in opts or "header-option" in named or (implicit_header and "noheader" not in opts)
        table = el("table")
        if meta.get("title"):
            table.append(el("caption", self.caption("table", meta["title"])))
        thead = el("thead") if header and rows else None
        tbody = el("tbody")
        for k, row in enumerate(rows):
            in_head = header and k == 0
            tr = ET.SubElement(thead if in_head else tbody, "tr")
            for cell in row:
                col = specs[cell["col"]] if cell["col"] < len(specs) else {"style": "", "align": ""}
                style = cell["style"] or (col["style"] if not in_head else "")
                tag = "th" if in_head or style == "h" else "td"
                td = ET.SubElement(tr, tag)
                if cell["colspan"] > 1:
                    td.set("colspan", str(cell["colspan"]))
                if cell["rowspan"] > 1:
                    td.set("rowspan", str(cell["rowspan"]))
                align = cell["align"] or col["align"]
                if align:
                    td.set("style", f"text-align: {align}")
                self._cell(td, cell["text"], style, depth)
        if thead is not None:
            table.append(thead)
        table.append(tbody)
        cells = [c for c in table.iter() if c.tag in ("td", "th")]
        if multiline or any(len(c) and c[0].tag in markup.BLOCK for c in cells):
            # some cells hold blocks or several lines: then every cell's text is a paragraph (Asciidoctor's
            # own output always has one)
            for c in cells:
                if not (len(c) and c[0].tag in markup.BLOCK) and (c.text or len(c)):
                    p = el("p")
                    p.text, c.text = c.text, None
                    p.extend(list(c))
                    for x in list(c):
                        c.remove(x)
                    c.append(p)
        self.emit(parent, table, {k: v for k, v in meta.items() if k != "title"})

    def _cell(self, td: ET.Element, text: str, style: str, depth: int) -> None:
        if style == "a":
            self.blocks(_lines(text), td, depth + 1)
            if len(td) == 1 and td[0].tag == "p" and not td[0].get("id"):
                unwrap(td, td[0])
            return
        if style == "l":
            td.append(el("pre", el("code", text)))
            return
        paras = [p.strip() for p in re.split(r"\n[ \t]*\n", text.strip()) if p.strip()]
        tag = {"m": "code", "e": "em", "s": "strong"}.get(style)
        made = []
        for p in paras:
            nodes = self.inline(p) if style != "m" else [p]
            made.append(el(tag, nodes) if tag else nodes)
        if len(made) == 1:
            node_append(td, made[0] if isinstance(made[0], list) else [made[0]])
        else:
            for m in made:
                td.append(el("p", m if isinstance(m, list) else [m]))

    # --- inline ------------------------------------------------------------------------------------
    def inline(self, text: str, depth: int = 0) -> list:
        if depth == 0:
            text = self.subst(text)
        out: list = []
        buf: list[str] = []
        pos, n = 0, len(text)
        ends: dict[str, list[int]] = {}

        def flush():
            if buf:
                out.append("".join(buf))
                buf.clear()

        def constrained_end(mark: str, start: int) -> int:
            lst = ends.get(mark)
            if lst is None:
                lst = []
                k = text.find(mark, 1)
                while k > 0:
                    after = text[k + 1] if k + 1 < n else " "
                    if not text[k - 1].isspace() and not (after.isalnum() or after == "_"):
                        lst.append(k)
                    k = text.find(mark, k + 1)
                ends[mark] = lst
            from bisect import bisect_left
            idx = bisect_left(lst, start)
            return lst[idx] if idx < len(lst) else -1

        def bracket(start: int) -> int:
            """The index of the ] that closes the [ at ``start - 1`` (escapes and nesting counted), or -1."""
            return _pairs(text, "[", "]").get(start - 1, -1)

        def sub(s: str) -> list:
            return self.inline(s, depth + 1) if depth < MAX_INLINE_DEPTH else [s]

        while pos < n:
            m = _INL.search(text, pos)
            if not m:
                buf.append(text[pos:])
                break
            s, e, kind = m.start(), m.end(), m.lastgroup
            buf.append(text[pos:s])
            pos = e
            tok = m.group()
            if kind == "esc":
                # the escaped markup is text: take the next token as it is
                m2 = _INL.match(text, e)
                if m2 and m2.lastgroup != "esc":
                    buf.append(m2.group())
                    pos = m2.end()
                continue
            if kind in ("pass3", "pass2", "pass1"):
                k = text.find(tok, e)
                if k < 0 or k == e or (kind == "pass1" and (s > 0 and text[s - 1].isalnum() or
                                                             (k + 1 < n and text[k + 1].isalnum()))):
                    buf.append(tok)
                    continue
                flush()
                if kind == "pass3":
                    lead, elems = markup.parse_fragment(text[e:k])
                    out.append(lead)
                    out += elems
                else:
                    out.append(text[e:k])
                pos = k + len(tok)
                continue
            if kind == "lb":
                flush()
                out.append(el("br"))
                out.append("\n")
                continue
            if kind == "macro":
                got = self._macro(text, s, e, tok[:-1], bracket, sub)
                if got is None:
                    buf.append(tok)
                    continue
                node, pos = got
                flush()
                if isinstance(node, list):
                    out += node
                elif node is not None:
                    out.append(node)
                continue
            if kind == "url":
                url = tok
                if e < n and text[e] == "[":
                    k = bracket(e + 1)
                    if k > 0:
                        label = text[e + 1:k]
                        flush()
                        out.append(self._link(url, label, sub))
                        pos = k + 1
                        continue
                while url and url[-1] in ".,;:!?)'" and not (url[-1] == ")" and url.count("(") >= url.count(")")):
                    url = url[:-1]
                pos = s + len(url)
                flush()
                out.append(el("a", url, href=url))
                continue
            if kind == "angle":
                flush()
                out.append(el("a", tok[1:-1], href=tok[1:-1]))
                continue
            if kind == "mail":
                flush()
                out.append(el("a", tok, href="mailto:" + tok))
                continue
            if kind == "xref":
                k = text.find(">>", e, e + 2000)
                if k < 0 or "\n\n" in text[e:k]:
                    buf.append(tok)
                    continue
                target, _, label = text[e:k].partition(",")
                if not target.strip() or " " in target.strip():
                    buf.append(tok)
                    continue
                flush()
                out.append(self._xref(target.strip(), label.strip(), sub))
                pos = k + 2
                continue
            if kind in ("anchor2", "anchor3"):
                close = "]]" if kind == "anchor2" else "]]]"
                k = text.find(close, e, e + 300)
                body = text[e:k] if k > 0 else ""
                am = re.fullmatch(r"([\w:.-][^,\]\s]*)(?:,\s*(.*))?", body)
                if k < 0 or not am:
                    buf.append(tok)
                    continue
                flush()
                aid = am.group(1)
                self.ids.add(aid)
                if am.group(2):
                    self.reftext[aid] = am.group(2)
                out.append(el("span", f"[{aid}]" if kind == "anchor3" else "", id=aid))
                pos = k + len(close)
                continue
            if kind in ("idx2", "idx3"):
                close = "))" if kind == "idx2" else ")))"
                k = text.find(close, e, e + 500)
                if k < 0:
                    buf.append(tok)
                    continue
                if kind == "idx2":
                    flush()
                    out += sub(text[e:k])
                pos = k + len(close)
                continue
            if kind == "role":
                markm = re.match(r"\*\*|__|``|##|[*_`#]", text[e:])
                mk = markm.group()
                if len(mk) == 2:
                    k = text.find(mk, e + 2)
                else:
                    k = constrained_end(mk, e + 2)
                if k < 0:
                    buf.append(tok)
                    continue
                inner = text[e + len(mk):k]
                tag = _MARK_TAG[mk[0]] if mk[0] != "#" else "span"
                attrs = tok[1:-1]
                node = el(tag, sub(inner) if mk[0] != "`" else [inner])
                roles = re.findall(r"\.([\w-]+)", attrs)
                idm = re.search(r"#([\w:-]+)", attrs)
                if roles:
                    node.set("class", " ".join(roles))
                    if "underline" in roles and tag == "span":
                        node.tag = "u"
                    elif "line-through" in roles and tag == "span":
                        node.tag = "del"
                if idm:
                    node.set("id", idm.group(1))
                    self.ids.add(idm.group(1))
                flush()
                out.append(node)
                pos = k + len(mk)
                continue
            if kind == "mark2":
                k = text.find(tok, e)
                if k < 0 or k == e:
                    buf.append(tok)
                    continue
                inner = text[e:k]
                flush()
                ch = tok[0]
                out.append(el(_MARK_TAG[ch], sub(inner) if ch != "`" else [_literal_code(inner)]))
                pos = k + 2
                continue
            if kind == "mark1":
                ch = tok
                before = text[s - 1] if s else " "
                if (s == 0 or not (before.isalnum() or before in "_;:}")) and e < n and not text[e].isspace():
                    k = constrained_end(ch, e + 1)
                    if k > 0:
                        inner = text[e:k]
                        if inner and not inner[-1].isspace():
                            flush()
                            out.append(el(_MARK_TAG[ch], sub(inner) if ch != "`" else [_literal_code(inner)]))
                            pos = k + 1
                            continue
                buf.append(tok)
                continue
            if kind == "supsub":
                k = text.find(tok, e, e + 200)
                if k > e and not any(x.isspace() for x in text[e:k]):
                    flush()
                    out.append(el("sup" if tok == "^" else "sub", sub(text[e:k])))
                    pos = k + 1
                    continue
                buf.append(tok)
                continue
            if kind == "quote":
                close = "`" + tok[0]
                k = text.find(close, e, e + 2000)
                if k < 0:
                    buf.append(tok)
                    continue
                flush()
                q = ("\u201c", "\u201d") if tok[0] == '"' else ("\u2018", "\u2019")
                out += [q[0], *sub(text[e:k]), q[1]]
                pos = k + 2
                continue
            if kind == "repl":
                if tok == "--":
                    before = text[s - 1] if s else " "
                    after = text[e] if e < n else " "
                    if before.isspace() and after.isspace():
                        buf.append("\u2014")
                        continue
                    if before.isalnum() and after.isalnum():
                        buf.append("\u2014\u200b")
                        continue
                    buf.append(tok)
                    continue
                buf.append(_REPL.get(tok, tok))
                continue
            if kind == "apos":
                buf.append("\u2019")
                continue
            buf.append(tok)
        flush()
        return out

    def _link(self, url: str, label: str, sub) -> ET.Element:
        label = label.strip()
        window = label.endswith("^")
        if window:
            label = label[:-1]
        if "=" in label and "," in label:
            pos, _named = _attrlist(label)
            label = pos[0] if pos else ""
        shown = sub(label) if label else [url.removeprefix("mailto:")]
        return el("a", shown, href=url)

    def _macro(self, text: str, s: int, e: int, name: str, bracket, sub):
        k = text.find("[", e, e + 2001)
        if k < 0 or k - e > 2000 or any(ch.isspace() for ch in text[e:k]):
            return None
        target = text[e:k]
        close = bracket(k + 1)
        if close < 0:
            return None
        inner = text[k + 1:close]
        end = close + 1
        if name == "link":
            href = target if not target.lower().endswith(".adoc") else target[:-5] + ".html"
            return self._link(href, inner, sub), end
        if name == "mailto":
            return self._link("mailto:" + target, inner.split(",")[0] if inner else "", sub), end
        if name == "image":
            pos, named = _attrlist(inner)
            alt = named.get("alt") or (pos[0] if pos and pos[0] else _default_alt(target))
            img = el("img", src=self._image_src(target), alt=alt)
            return (el("a", img, href=named["link"]) if named.get("link") else img), end
        if name == "kbd":
            keys = [x.strip() for x in re.split(r"(?<!\+)\+(?!\+$)|,", inner) if x.strip()] or [inner]
            nodes: list = []
            for j, key in enumerate(keys):
                if j:
                    nodes.append("+")
                nodes.append(el("kbd", key))
            return nodes, end
        if name == "btn":
            return el("strong", inner, class_="button"), end
        if name == "menu":
            items = [target, *[x.strip() for x in re.split(r"\s*>\s*", inner) if x.strip()]]
            return el("em", " \u25b8 ".join(items), class_="menuseq"), end
        if name in ("footnote", "footnoteref"):
            if name == "footnoteref":
                fid, _, body = inner.partition(",")
                return self._footnote(fid.strip(), body.strip(), sub), end
            return self._footnote(target, inner, sub), end
        if name == "xref":
            return self._xref(target, inner, sub), end
        if name == "anchor":
            self.ids.add(target)
            if inner:
                self.reftext[target] = inner
            return el("span", "", id=target), end
        if name in ("stem", "latexmath", "asciimath"):
            return self._math(inner.replace("\\]", "]"), name, False), end
        if name == "pass":
            return inner, end
        if name == "indexterm":
            return None, end
        if name == "indexterm2":
            return sub(inner), end
        if name == "icon":
            return f"[{target}]", end
        return None

    def _footnote(self, fid: str, body: str, sub) -> ET.Element:
        if fid and fid in self.note_ids and not body.strip():
            num, aside = self.note_ids[fid]
            call = el("a", el("sup", str(num)), class_="noteref", epub__type="noteref", href=f"#fn{num}")
            return call
        num = len(self.notes) + 1
        aside = el("aside", epub__type="footnote", id=f"fn{num}")
        aside.append(el("p", sub(body.replace("\\]", "]"))))
        call = el("a", el("sup", str(num)), class_="noteref", epub__type="noteref", href=f"#fn{num}")
        self.notes.append((call, aside))
        if fid:
            self.note_ids[fid] = (num, aside)
        return call

    def _xref(self, target: str, label: str, sub) -> ET.Element:
        path, hashmark, frag = target.partition("#")
        if not hashmark:
            if path.endswith((".adoc", ".asc", ".asciidoc")) or "/" in path:
                frag = ""
            else:
                frag, path = path, ""
        if path:
            path = re.sub(r"\.(adoc|asc|asciidoc)$", "", path)
        a = el("a", sub(label) if label else None, data_adoc_xref=frag, data_adoc_path=path)
        return a

    def resolve(self, body: ET.Element) -> None:
        parents = {c: p for p in body.iter() for c in p}
        for a in list(body.iter("a")):
            frag = a.attrib.pop("data-adoc-xref", None)
            if frag is None:
                continue
            path = a.attrib.pop("data-adoc-path", "")
            empty = not len(a) and not (a.text or "").strip()
            if frag in self.ids:
                a.set("href", "#" + frag)
                if empty:
                    a.text = self.reftext.get(frag) or f"[{frag}]"
            elif path:
                a.set("href", f"{path}.html" + (f"#{frag}" if frag else ""))
                if empty:
                    a.text = f"{path}.html" + (f"#{frag}" if frag else "")  # what Asciidoctor shows
            else:
                if empty:
                    a.text = f"[{frag}]"
                if a in parents:
                    unwrap(parents[a], a)
        # notes go right after the top-level block that first calls them
        for call, aside in self.notes:
            top = call
            while parents.get(top) is not None and parents[top] is not body:
                top = parents[top]
            if parents.get(top) is body:
                idx = list(body).index(top) + 1
                while idx < len(body) and body[idx].tag == "aside":
                    idx += 1
                body.insert(idx, aside)
            else:
                body.append(aside)
            parents[aside] = body
            for c in aside.iter():
                for cc in c:
                    parents[cc] = c


def _psv_cells(content: list[str], sep: str) -> tuple[list[dict], int, bool]:
    """Cells of a prefix-separated table ("|a |b"), the number on the first line, and whether that
    first line is an implicit header (alone, followed by a blank line)."""
    text = "\n".join(content)
    cells: list[dict] = []
    positions = []
    k = text.find(sep)
    while k >= 0:
        if not (k > 0 and text[k - 1] == "\\"):
            positions.append(k)
        k = text.find(sep, k + 1)
    if not positions:
        return [], 0, False
    starts = []  # (spec start, separator position, spec)
    for p in positions:
        m = _CELL_SPEC.search(text, max(0, p - 24), p)
        spec = m.group(1) if m and m.end() == p else ""
        starts.append((p - len(spec), p, spec))
    for j, (s0, p, spec) in enumerate(starts):
        end = starts[j + 1][0] if j + 1 < len(starts) else len(text)
        cells.append({"text": text[p + 1:end].replace("\\" + sep, sep).strip(" \t"), "spec": spec})
    first_line = content[0] if content else ""
    k = 0
    while k < len(content) and not content[k].strip():
        k += 1
    first_line = content[k] if k < len(content) else ""
    first_count = sum(1 for p in range(len(first_line)) if first_line[p] == sep and (p == 0 or first_line[p - 1] != "\\"))
    implicit = k + 1 < len(content) and not content[k + 1].strip() and first_count > 0
    return cells, first_count, implicit


def _col_specs(cols: str) -> list[dict]:
    if not cols.strip():
        return []
    if markup.whole(cols.strip()) is not None:
        return [{"style": "", "align": ""} for _ in range(min(markup.whole(cols.strip()), 1000))]
    out = []
    for part in re.split(r"[,;]", cols):
        part = part.strip()
        m = re.match(r"^(?:(\d+)\*)?([<^>])?(?:\.[<^>])?(\d+%?|~)?([adehlmsv])?$", part)
        if not m:
            out.append({"style": "", "align": ""})
            continue
        mult = int(m.group(1)) if m.group(1) else 1
        for _ in range(min(mult, 1000)):
            out.append({"style": m.group(4) or "", "align": {"<": "left", "^": "center", ">": "right"}.get(m.group(2) or "", "")})
    return out


def _tagged(lines: list[str], tags: list[str]) -> list[str]:
    """The lines between ``tag::name[]`` and ``end::name[]`` markers (``**``/``*``/``!name`` as Asciidoctor reads them)."""
    want = {t for t in tags if not t.startswith("!") and t not in ("*", "**")}
    exclude = {t[1:] for t in tags if t.startswith("!")}
    everything = "**" in tags or "*" in tags or (not want and exclude)
    out: list[str] = []
    active: list[str] = []
    for line in lines:
        m = re.search(r"\b(tag|end)::([\w-]+)\[\]", line)
        if m:
            if m.group(1) == "tag":
                active.append(m.group(2))
            elif m.group(2) in active:
                active.remove(m.group(2))
            continue
        if any(a in exclude for a in active):
            continue
        if everything or any(a in want for a in active):
            out.append(line)
    return out


def _ifeval(expr: str) -> bool:
    m = re.match(r"^\s*(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+?)\s*$", expr)
    if not m:
        return False

    def val(s: str):
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
            return s[1:-1]
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d*\.\d+", s):
            return float(s)
        return {"true": True, "false": False}.get(s, s)

    a, op, b = val(m.group(1)), m.group(2), val(m.group(3))
    try:
        return {"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
    except TypeError:
        return False


def _default_alt(target: str) -> str:
    name = target.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"[-_]+", " ", name)


def _lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    return [s.rstrip() for s in text.split("\n")]


def _dedent(lines: list[str]) -> list[str]:
    widths = [len(s) - len(s.lstrip(" \t")) for s in lines if s.strip()]
    k = min(widths) if widths else 0
    return [s[k:] for s in lines] if k else list(lines)


def to_body(text: str, base_dir=None, top=None, lost: list[str] | None = None) -> tuple[ET.Element, dict]:
    """AsciiDoc text to a ``<body>`` element; also returns the document attributes."""
    p = _Adoc(base_dir, top, lost if lost is not None else [])
    lines = p.preprocess(_lines(text), base_dir)
    p.attrs = dict(_BUILTIN)
    p.counters = {}
    body = ET.Element("body")
    p.document(lines, body)
    p.resolve(body)
    return body, p.attrs


def read(path) -> Book:
    path = Path(path)
    text = markup.decode(path.read_bytes())
    lost: list[str] = []
    body, attrs = to_body(text, path.parent, path.parent.parent, lost)
    md = Metadata(title=attrs.get("doctitle") or path.stem.replace("_", " "))
    if attrs.get("authors"):
        md.authors = [a.strip() for a in attrs["authors"].split(";") if a.strip()]
    if attrs.get("revdate"):
        md.date = attrs["revdate"]
    if attrs.get("lang"):
        md.language = attrs["lang"]
    if attrs.get("description"):
        md.description = attrs["description"]
    if attrs.get("doctitle") is None:
        first = body.find("h1")
        if first is not None and len(body.findall("h1")) == 1:
            md.title = " ".join(markup.text_of(first).split())
    return build_book(body, md.title, "adoc", path.parent, path.parent.parent, lost, md)


# --- writing -----------------------------------------------------------------------------------

_LINE_MARK = re.compile(r"^(?:[*.\-=#]+[ \t]|\d+\.[ \t]|[a-zA-Z]\.[ \t]|<\d+>[ \t]|//|:!?\w[\w-]*!?:|\[|'''|<<<|\+$"
                        r"|(?:NOTE|TIP|IMPORTANT|CAUTION|WARNING):|(?:-{4,}|\.{4,}|={4,}|\*{4,}|_{4,}|\+{4,}|--)$"
                        r"|(?:ifn?def|ifeval|endif|include)::|(?:image|video|audio|toc)::)")


class AsciiDocWriter(MarkupWriter):
    inline_anchors = True

    def __init__(self, book: Book):
        super().__init__(book)
        self._lists = 0

    def header(self) -> str:
        md = self.book.metadata
        head = [f"= {md.title}"] if md.title else []
        if md.title and md.authors:
            head.append("; ".join(md.authors))
        if md.language:
            head.append(f":lang: {md.language}")
        return "\n".join(head)

    def escape(self, s: str) -> str:
        # a backslash only where the text would otherwise be read as markup (elsewhere it would show)
        for ch in "*_`#":
            if s.count(ch) >= 2:
                s = re.sub(rf"(?<![\w\\]){re.escape(ch)}(?=\S)|{re.escape(ch * 2)}", lambda m: "\\" + m.group(), s)
        s = re.sub(r"(\^|~)(?=\S+?\1)", r"\\\1", s)
        s = re.sub(r"\{(?=[\w][\w-]*(?::[^}]*)?\})", r"\\{", s)
        s = re.sub(r"(\+\+\+|\+\+)", r"\\\1", s)
        s = re.sub(r"(?<!\w)\+(?=\S[^+]*\+(?!\w))", r"\\+", s)
        s = re.sub(r"(<<|\[\[|\(\(|\(C\)|\(R\)|\(TM\)|->|=>|<-|<=|(?<=\s)--(?=\s)|(?<=\w)--(?=\w))", r"\\\1", s)
        s = re.sub(r"\b((?:https?|ftp|irc|mailto|link|image|kbd|btn|menu|footnote|xref|anchor|stem|latexmath|pass):)",
                   r"\\\1", s)
        return re.sub(r" \+(?=\n|$)", " pass:[+]", s)

    def _wrap(self, mark: str, s: str) -> str:
        # unconstrained (doubled) marks work anywhere, even inside a word
        return f"{mark}{mark}{s}{mark}{mark}"

    def _anchor(self, anchor: str | None) -> str:
        return f"[[{anchor}]]\n" if anchor else ""

    def doc_anchor(self, key: str) -> str:
        return f"[[{key}]]\n"

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        lines = []
        for s in text.strip().split("\n"):
            s = s.strip()
            if _LINE_MARK.match(s) or re.search(r"(?::{2,4}|;;)(?:\s|$)", s):
                s = "{empty}" + s
            lines.append(s)
        return self._anchor(anchor) + "\n".join(lines).replace(_BR, " +\n")

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        text = text.replace(_BR, " ").replace("\n", " ").strip() or "{empty}"
        return self._anchor(anchor) + "=" * min(level + 1, 6) + " " + text

    def block(self, el_: ET.Element) -> str:
        cls = el_.get("class", "").split()
        anchor = self.anchor_key(el_)
        prefix = self._anchor(anchor) if anchor and el_.tag not in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "figure") \
            else ""
        if el_.tag in ("ul", "ol"):
            self._lists += 1
            try:
                return prefix + self._list(el_)
            finally:
                self._lists -= 1
        if el_.tag == "div" and "admonition" in cls:
            kind = next((c.upper() for c in cls if c.upper() in _ADMONITIONS), "NOTE")
            title = el_.find("p")
            inner = el("div")
            for c in el_:
                if not (c is title and "admonition-title" in c.get("class", "")):
                    inner.append(c)
            return prefix + f"[{kind}]\n====\n{self.blocks(inner)}\n===="
        if el_.tag == "div" and "line-block" in cls:
            lines = self.inlines(el_).split(_BR)
            return prefix + "[verse]\n____\n" + "\n".join(s.strip().replace(" ", " ") for s in lines) + "\n____"
        if el_.tag == "div" and "sidebar" in cls:
            return prefix + f"****\n{self.blocks(el_)}\n****"
        return prefix + super().block(el_)

    def _list(self, lst: ET.Element) -> str:
        ordered = lst.tag == "ol"
        mark = ("." if ordered else "*") * min(self._lists, 5)
        out = []
        for li in lst:
            if li.tag != "li":
                continue
            body = (self.list_item(li).strip("\n") or "{empty}").split("\n\n")
            text = f"{mark} {body[0]}"
            for part in body[1:]:
                nested = re.match(r"^(?:\[start=\d+\]\n)?[*.]+ ", part)
                text += ("\n" if nested else "\n+\n") + part
            out.append(text)
        start = lst.get("start", "1")
        prefix = f"[start={start}]\n" if ordered and start.isdigit() and start != "1" else ""
        return prefix + "\n".join(out)

    def rule(self) -> str:
        return "'''"

    def code_block(self, code: str, lang: str) -> str:
        fence = "----" if lang else "...."
        while fence in code.split("\n"):
            fence += fence[0]
        head = f"[source,{lang}]\n" if lang else ""
        return f"{head}{fence}\n{code}\n{fence}"

    def raw_block(self, text: str, fmt: str) -> str:
        return f"++++\n{text}\n++++" if fmt == "html" else self.code_block(text, "")

    def quote(self, inner: str) -> str:
        fence = "____"
        while fence in inner.split("\n"):
            fence += "_"
        return f"{fence}\n{inner}\n{fence}"

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        out = []
        for term, defs in pairs:
            term = term.replace(_BR, " ").replace("\n", " ").strip().replace("::", ":{empty}:") or "{empty}"
            parts = "\n\n".join(d for d in defs if d.strip()).split("\n\n") if defs else []
            text = f"{term}::"
            if parts and parts[0]:
                text += ("\n" if "\n" in parts[0] else " ") + parts[0]
                for part in parts[1:]:
                    text += "\n+\n" + part
            out.append(text)
        return "\n".join(out)

    def table(self, table: Table) -> str:
        if not table.rows:
            return ""
        lines = []
        if table.caption:
            lines.append("." + table.caption.replace(_BR, " "))
        opts = [f'cols="{table.width}*"']
        if table.header_rows:
            opts.append('options="header"')
        lines += ["[" + ",".join(opts) + "]", "|==="]
        for k, row in enumerate(table.rows):
            cells = []
            for c in row:
                spec = (f"{c.colspan}" if c.colspan > 1 else "") + (f".{c.rowspan}" if c.rowspan > 1 else "")
                spec += "+" if spec else ""
                text = c.text.replace("|", "{vbar}").replace(_BR, " +\n")
                if "\n\n" in text.strip():
                    spec += "a"
                cells.append(f"{spec}|{text}".rstrip())
            lines.append("\n".join(cells) if any("\n" in x for x in cells) else " ".join(cells))
            if k == 0 and table.header_rows:
                lines.append("")
        lines.append("|===")
        return "\n".join(lines)

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        out = self._anchor(anchor)
        if caption:
            out += "." + caption.replace(_BR, " ").replace("\n", " ") + "\n"
        return out + f"image::{src}[{_attr_text(alt)}]"

    def math_block(self, el_: ET.Element) -> str:
        return "[latexmath]\n++++\n" + mathml.to_tex(el_) + "\n++++"

    def math_inline(self, el_: ET.Element) -> str:
        return "latexmath:[" + mathml.to_tex(el_).replace("]", "\\]") + "]"

    def footnote(self, body: str, note_id: str) -> str:
        return "footnote:[" + " ".join(body.replace(_BR, " ").split()).replace("]", "\\]") + "]"

    def line_break(self) -> str:
        return _BR

    def inline_anchor(self, key: str) -> str:
        return f"[[{key}]]"

    def emph(self, s: str) -> str:
        return self._wrap("_", s)

    def strong(self, s: str) -> str:
        return self._wrap("*", s)

    def strike(self, s: str) -> str:
        return f"[.line-through]##{s}##"

    def underline(self, s: str) -> str:
        return f"[.underline]##{s}##"

    def sup(self, s: str) -> str:
        return f"^{s.replace(' ', '{sp}')}^" if s.strip() else s

    def sub(self, s: str) -> str:
        return f"~{s.replace(' ', '{sp}')}~" if s.strip() else s

    def code(self, s: str) -> str:
        s = " ".join(s.split("\n"))
        if not s.strip():
            return s
        # the reader takes the text between `` and `` as it stands (pass:[...] in there stayed "pass:[...]"):
        # only attribute references, which are put in before, need their \
        if "``" in s or s.startswith("`") or s.endswith("`"):
            return "``pass:[" + s.replace("]", "\\]") + "]``"
        return "``" + re.sub(r"\{(?=[\w][\w-]*(?::[^}]*)?\})", r"\\{", s) + "``"

    def raw_inline(self, text: str, fmt: str) -> str:
        return f"+++{text}+++" if fmt == "html" else self.code(text)

    def link(self, target: str, text: str, external: bool) -> str:
        text = text.strip()
        if not external:
            if target not in self.anchored:
                return text
            return f"<<{target},{text}>>" if text else f"<<{target}>>"
        if re.sub(r"\\(?=[\w{])", "", text) == target:
            text = ""
        text = text.replace("]", "\\]")
        if target.startswith("mailto:"):
            return f"mailto:{target[7:]}[{text}]"
        if re.match(r"^(?:https?|ftp|irc)://", target):
            return f"{target}[{text}]" if text and text != target else f"{target}[]"
        return f"link:{target.replace(' ', '%20')}[{text}]"

    def image(self, src: str, alt: str) -> str:
        return f"image:{src.replace(' ', '%20')}[{_attr_text(alt)}]" if src else alt


def _attr_text(s: str) -> str:
    s = " ".join(s.split()).replace("]", "\\]")
    return '"' + s.replace('"', '\\"') + '"' if "," in s or '"' in s or "=" in s else s


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    return write_text(AsciiDocWriter(book), path, pictures)
