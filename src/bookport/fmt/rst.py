"""reStructuredText: a reader (docutils markup plus the Sphinx directives and roles) and a writer.

The reader follows the docutils specification: section titles take their level from the order
their adornment styles first appear; footnotes, citations, substitutions and hyperlink targets
are resolved after the whole file is read. Sphinx documentation (object descriptions, version
notes, cross-reference roles, toctree) reads as Sphinx shows it. Includes go through
``safe_path``; directives and roles it doesn't know keep their text and are named in ``book.lost``.

Also here: ``build_book``, which the Org and AsciiDoc readers share (one document per top-level
section for big files, links fixed across documents, local pictures pulled in).
"""

from __future__ import annotations

import copy
import csv
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, Metadata, is_external, relative, resolve
from ..limits import MAX_DEPTH, safe_path
from ..textwriter import Table, TextWriter, is_noteref, write_text

MAX_INCLUDE_DEPTH = 8
MAX_INCLUDE_BYTES = 64 * 1024 * 1024
MAX_SUB_ROUNDS = 6  # substitutions inside substitutions
MAX_SUB_NODES = 200_000  # what substitutions may add to one document, all told (elements, and text per 64 characters)
MAX_INCLUDES = 500  # files one document may include, all told (MAX_INCLUDE_BYTES is the limit for their sizes too)

# --- shared by the markup readers ---------------------------------------------------------------


def make_id(name: str, taken: set[str]) -> str:
    """An id like docutils and Pandoc make ("Built-in Functions" -> "built-in-functions"), unique in ``taken``."""
    base = re.sub(r"[^\w]+", "-", unicodedata.normalize("NFKC", name).lower()).strip("-_") or "section"
    if base[0].isdigit():
        base = "s-" + base
    i, n = base, 1
    while i in taken:
        i = f"{base}-{n}"
        n += 1
    taken.add(i)
    return i


def node_append(parent: ET.Element, nodes) -> None:
    """Append a list of strings and elements (inline content) to ``parent``."""
    for x in nodes:
        if isinstance(x, str):
            if x:
                markup.append_text(parent, x)
        else:
            parent.append(x)


def el(tag: str, nodes=None, **attrs) -> ET.Element:
    e = ET.Element(tag, {k.rstrip("_").replace("__", ":").replace("_", "-"): v for k, v in attrs.items() if v is not None})
    if nodes is not None:
        node_append(e, nodes if isinstance(nodes, list) else [nodes])
    return e


def replace_el(parent: ET.Element, old: ET.Element, nodes) -> None:
    """Put ``nodes`` (strings and elements) where ``old`` is, keeping old's tail."""
    idx = list(parent).index(old)
    tail = old.tail or ""
    parent.remove(old)
    prev = parent[idx - 1] if idx > 0 else None
    for x in [*nodes, tail]:
        if isinstance(x, str):
            if not x:
                continue
            if prev is None:
                parent.text = (parent.text or "") + x
            else:
                prev.tail = (prev.tail or "") + x
        else:
            x.tail = None
            parent.insert(idx, x)
            idx += 1
            prev = x


def _splice(parent: ET.Element, reps: dict) -> None:
    """replace_el for many children of one parent at once: {child: nodes to put where it is}."""
    kids: list[ET.Element] = []
    text = [parent.text or ""]

    def add(x) -> None:
        if isinstance(x, str):
            if x:
                if kids:
                    kids[-1].tail = (kids[-1].tail or "") + x
                else:
                    text[0] += x
        else:
            x.tail = None
            kids.append(x)

    for c in list(parent):
        tail = c.tail or ""
        if c in reps:
            for x in reps[c]:
                add(x)
        else:
            add(c)
        add(tail)
    parent[:] = kids
    parent.text = text[0] or None


def unwrap(parent: ET.Element, e: ET.Element) -> None:
    """Replace ``e`` with its content."""
    nodes: list = [e.text or ""]
    for c in list(e):
        tail = c.tail or ""
        c.tail = None
        nodes += [c, tail]
    replace_el(parent, e, nodes)


def pull_images(book: Book, root: ET.Element, href: str, base_dir, top, lost: list[str]) -> None:
    """Local pictures into the book (from the document's folder or one level up, through safe_path)."""
    if base_dir is None:
        return
    for img in root.iter("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:") or (is_external(src) and not src.lower().startswith("file:")):
            continue
        from urllib.parse import unquote
        name = unquote(src.split("#")[0].split("?")[0])
        p = safe_path(base_dir, name, top)
        if p is None:
            note = f"A picture outside the document's folder was left out: {src}"
            if note not in lost:
                lost.append(note)
            continue
        try:
            data = p.read_bytes() if p.is_file() and p.stat().st_size <= MAX_INCLUDE_BYTES else None
        except OSError:
            data = None
        if data is None:
            continue
        existing = next((h for h, r in book.resources.items() if r.data == data), None)
        if existing is None:
            existing = book.add("images/" + re.sub(r"[^\w.-]", "_", p.name), data).href
        img.set("src", relative(href, existing))


def build_book(body: ET.Element, title: str, fmt: str, base_dir=None, top=None, lost: list[str] | None = None,
               metadata: Metadata | None = None, target: int = 60_000) -> Book:
    """A book from one long ``<body>``: split into documents at top-level headings when it's big, with
    links between the parts fixed, local pictures pulled in and the TOC built from the headings."""
    md = metadata or Metadata()
    md.title = md.title or title
    md.language = md.language or "en"
    book = Book(md)
    book.source_format = fmt
    book.lost = lost if lost is not None else []
    chunks = _split(body, target)
    hrefs = [f"text/part{n:04d}.xhtml" for n in range(1, len(chunks) + 1)]
    where: dict[str, str] = {}
    for href, chunk in zip(hrefs, chunks):
        for c in chunk:
            for e in c.iter():
                i = e.get("id")
                if i:
                    where.setdefault(i, href)
    for href, chunk in zip(hrefs, chunks):
        root, b = markup.new_document(md.title, md.language)
        b.extend(chunk)
        if len(chunks) > 1:
            for a in b.iter("a"):
                link = a.get("href", "")
                if link.startswith("#") and where.get(link[1:], href) != href:
                    a.set("href", relative(href, where[link[1:]]) + link)
        pull_images(book, root, href, base_dir, top, book.lost)
        book.add_document(href, root)
    if not book.spine:
        root, b = markup.new_document(md.title, md.language)
        ET.SubElement(b, "p")
        book.add_document("text/part0001.xhtml", root)
    book.ensure_toc()
    return book


def _split(body: ET.Element, target: int) -> list[list[ET.Element]]:
    children = list(body)
    total = sum(len(markup.text_of(c)) for c in children)
    if total <= target:
        return [children] if children else []
    levels = [int(c.tag[1]) for c in children if c.tag in ("h1", "h2")]
    if not levels:
        return [children]
    top = min(levels) if levels.count(min(levels)) > 1 else max(levels)
    chunks: list[list[ET.Element]] = []
    cur: list[ET.Element] = []
    size = 0
    for c in children:
        if c.tag in ("h1", "h2") and int(c.tag[1]) <= top and cur and size > target // 10:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += len(markup.text_of(c))
    if cur:
        chunks.append(cur)
    return chunks


def read_include(base_dir, name: str, top, lost: list[str], seen: list[Path], what: str = "An include",
                 count: dict | None = None) -> str | None:
    """The text of a file a document includes, or None (refused, missing, too big or a loop: said in ``lost``).
    ``count`` ({"files": 0, "bytes": 0}, one for the whole document) bounds the includes all told: depth
    alone let a few small files that each include the next ten times pull in 10^8 copies."""
    p = safe_path(base_dir, name, top) if base_dir is not None else None
    if p is None:
        lost.append(f"{what} outside the document's folder was left out: {name}")
        return None
    if p in seen or len(seen) >= MAX_INCLUDE_DEPTH:
        lost.append(f"{what} that includes itself (or nests more than {MAX_INCLUDE_DEPTH} deep) was left out: {name}")
        return None
    try:
        if not p.is_file() or p.stat().st_size > MAX_INCLUDE_BYTES:
            lost.append(f"{what} that isn't there was left out: {name}")
            return None
        if count is not None:
            count["files"] = count.get("files", 0) + 1
            count["bytes"] = count.get("bytes", 0) + p.stat().st_size
            if count["files"] > MAX_INCLUDES or count["bytes"] > MAX_INCLUDE_BYTES:
                if "The document includes more files than bookport reads; the rest were left out" not in lost:
                    lost.append("The document includes more files than bookport reads; the rest were left out")
                return None
        return markup.decode(p.read_bytes())
    except OSError:
        lost.append(f"{what} that can't be read was left out: {name}")
        return None


# --- reading -----------------------------------------------------------------------------------

_ADORN = re.compile(r"^([!-/:-@\[-`{-~])\1*\s*$")
_BULLET = re.compile(r"^([-*+•‣⁃])(?: +|$)")
_ENUM = re.compile(r"^(?:(\d+|#|[A-Za-z]|[ivxlcdm]+|[IVXLCDM]+)([.)])|\((\d+|#|[A-Za-z]|[ivxlcdm]+|[IVXLCDM]+)\))(?: +|$)")
_FIELD = re.compile(r"^:(?![\s:])((?:[^:\\`]|\\.|:(?!\s|$))+?)(?<!\\):(?:\s+|$)")
_OPTION_ONE = r"(?:--?[\w][\w-]*|/[\w]+|\+[\w])(?:(?:=| )(?![\s-])(?:<[^<>]+>|[^\s,]+))?"
_OPTION = re.compile(rf"^({_OPTION_ONE}(?:, {_OPTION_ONE})*)(?:  +(.*)|\s*$)")
_LINE_BLOCK = re.compile(r"^\|(?: +|$)")
_GRID_TOP = re.compile(r"^\+(?:-+\+)+\s*$")
_SIMPLE_TOP = re.compile(r"^=+(?: +=+)+\s*$")
_EXPLICIT = re.compile(r"^\.\.(?: +|$)")
_FOOTNOTE = re.compile(r"^\[([^\]\s]+)\](?:\s+(.*)|\s*$)")
_TARGET = re.compile(r"^_(`(?:[^`\\]|\\.)+`|(?:[^:\\`]|\\.)+|_):(?:\s+(.*)|\s*$)")
_SUBDEF = re.compile(r"^\|((?:[^|\\]|\\.)+)\|\s+([\w][\w:.+-]*?)::(?:\s+(.*)|\s*$)")
_DIRECTIVE = re.compile(r"^([\w][\w:.+-]*?)::(?:\s+(.*)|\s*$)")
_OPT_LINE = re.compile(r"^:([^:\s][^:]*?):(?:\s+(.*)|\s*$)")
_ATTRIBUTION = re.compile(r"^(?:--|---|—|―)(?: +)(?=\S)")

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
_SYMBOLS = "*†‡§¶#♠♥♦♣"

# inline markup: candidates, then the start/end rules of the specification decide
_INL = re.compile(r"""
 (?P<esc>\\(?:.|\n|$))
|(?P<lit>``)
|(?P<strong>\*\*)
|(?P<emph>\*)
|(?P<role>:[A-Za-z0-9](?:[\w.+-]|:(?!:)){0,64}?:`)
|(?P<interp>`)
|(?P<tgt>_`)
|(?P<sub>\|)
|(?P<fn>\[(?:[0-9]+|\#[\w.:+-]*|\*|[A-Za-z][\w.:+-]*)\]__?(?!\w))
|(?P<uri>(?<![\w.+-])(?:https?|ftp|sftp|mailto|news|irc|ssh|git|telnet|file):(?:[^\s<>\[\]`'"|\\]|\[[^\s\]]*\])+)
|(?P<mail>(?<![\w.+@-])[\w][\w.+-]*@[\w-]+(?:\.[\w-]+)+)
|(?P<ref>(?<![\w\\.:+-])[^\W_]+(?:[-_.:+][^\W_]+){0,32}__?(?!\w))
""", re.VERBOSE | re.DOTALL)
# role names and reference names are bounded, and a name can't start right after one of its own
# separators: unbounded, each ":" of ":a:a:a..." (or "-" of "a-a-a...") began a scan to the end of the line
_START_PRE = set("-:/'\"<([{") | set("‘“«‹‚„–—")
_END_POST = set("-.,:;!?\\/'\")]}>") | set("’”»›–—…")
_PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">", "'": "'", '"': '"', "‘": "’", "“": "”",
          "«": "»"}
_ROLE_SUFFIX = re.compile(r"__?|:[A-Za-z0-9](?:[\w.+-]|:(?!:))*?:")

# Sphinx cross-reference roles shown as code, by their name without the domain ("py:func" -> "func")
_CODE_ROLES = {"func", "meth", "class", "mod", "exc", "attr", "data", "const", "obj", "type", "member", "macro",
               "var", "enum", "enumerator", "expr", "texpr", "struct", "union", "envvar", "option", "keyword",
               "token", "opcode", "pdbcmd", "monitoring-event", "c-api", "cdata", "cfunc", "cmacro", "ctype",
               "cmember", "func-type", "concept", "any", "confval", "event", "directive", "role", "code", "literal",
               "file", "samp", "regexp", "makevar"}
_ADMONITIONS = {"note": "Note", "warning": "Warning", "tip": "Tip", "hint": "Hint", "important": "Important",
                "caution": "Caution", "danger": "Danger", "error": "Error", "attention": "Attention",
                "seealso": "See also", "todo": "Todo", "impl-detail": "CPython implementation detail"}
_VERSIONS = {"versionadded": ("Added in version {}", "added"), "versionchanged": ("Changed in version {}", "changed"),
             "deprecated": ("Deprecated since version {}", "deprecated"),
             "versionremoved": ("Removed in version {}", "removed")}
# Sphinx object descriptions -> what Sphinx shows before the signature
_OBJECTS = {"function": "", "method": "", "class": "class ", "exception": "exception ", "attribute": "", "data": "",
            "decorator": "@", "decoratormethod": "@", "classmethod": "classmethod ", "staticmethod": "static ",
            "abstractmethod": "abstractmethod ", "coroutinefunction": "coroutine ", "coroutinemethod": "coroutine ",
            "awaitablefunction": "awaitable ", "awaitablemethod": "awaitable ", "property": "property ",
            "type": "type ", "describe": "", "object": "", "option": "", "cmdoption": "", "envvar": "",
            "opcode": "", "pdbcommand": "", "monitoring-event": "", "confval": "", "macro": "", "member": "",
            "var": "", "struct": "struct ", "union": "union ", "enum": "enum ", "enumerator": "", "directive": "..",
            "role": "", "event": "", "productionlist-token": ""}
# Sphinx and docutils directives with nothing to show in a book
_SILENT = {"index", "highlight", "currentmodule", "module", "sectionauthor", "moduleauthor", "codeauthor", "program",
           "tabularcolumns", "meta", "testsetup", "testcleanup", "default-domain", "sectnum", "section-numbering",
           "target-notes", "header", "footer", "role", "default-role", "title", "py:currentmodule",
           "py:module", "c:namespace", "c:namespace-push", "c:namespace-pop", "cpp:namespace", "highlightlang",
           "autosummary", "nitpick-ignore"}
_ONLY_TRUE = {"html", "epub", "builder_html", "builder_epub", "format_html", "singlehtml", "builder_singlehtml"}


def _unescape(s: str) -> str:
    """Backslash escapes: an escaped space or newline disappears, any other escaped character is kept."""
    if "\\" not in s:
        return s
    return re.sub(r"\\(.)", lambda m: "" if m.group(1).isspace() else m.group(1), s, flags=re.DOTALL).replace("\\", "")


def _norm(name: str) -> str:
    return " ".join(_unescape(name).split()).lower()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _roman(s: str) -> int | None:
    s = s.lower()
    if not s or any(c not in _ROMAN for c in s):
        return None
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _ROMAN[a]
        total += -v if b != " " and _ROMAN[b] > v else v
    return total


def _ok_start(text: str, s: str, e: int) -> bool:
    """The start-string rule: start of text, whitespace or an opening punctuation before; no space after."""
    before = text[s - 1] if s > 0 else " "
    after = text[e] if e < len(text) else " "
    if after.isspace():
        return False
    if not (before.isspace() or before in _START_PRE or unicodedata.category(before) in ("Ps", "Pi", "Pd", "Po")):
        return False
    return _PAIRS.get(before) != after


def _ok_end(text: str, e: int) -> bool:
    after = text[e] if e < len(text) else " "
    return after.isspace() or after in _END_POST or unicodedata.category(after) in ("Pe", "Pf", "Pd", "Po", "Pi")


class _Rst:
    def __init__(self, base_dir, top, lost: list[str]):
        self.base_dirs = [base_dir]
        self.top = top
        self.lost = lost
        self.lost_kinds: set[str] = set()
        self.styles: list[tuple[str, bool]] = []
        self.ids: set[str] = set()
        self.targets: dict[str, tuple[str, str]] = {}  # name -> ("url"|"id"|"ref", value)
        self.anon_targets: list[tuple[str, str]] = []
        self.subs: dict[str, list] = {}
        self.pending: list[str] = []
        self.footnotes: list[tuple[str, ET.Element]] = []  # (label as written, aside)
        self.citations: dict[str, str] = {}  # name -> id
        self.default_role = "title-reference"
        self.roles: dict[str, tuple[str, str]] = {}  # custom role -> (base role, raw format)
        self.highlight = ""
        self.seen: list[Path] = []
        self.include_count: dict[str, int] = {}  # files and bytes included, all told
        self.meta: dict[str, str] = {}
        self.headings: dict[str, ET.Element] = {}  # id -> heading, for :ref: titles and contents
        self.contents: list[tuple[ET.Element, int, bool]] = []

    def drop(self, kind: str, what: str) -> None:
        if kind not in self.lost_kinds:
            self.lost_kinds.add(kind)
            self.lost.append(what)

    # --- block structure ---------------------------------------------------------------------
    def emit(self, parent: ET.Element, e: ET.Element, name: str | None = None) -> ET.Element:
        """Append a block; internal targets waiting for the next element, and its own name, point to it."""
        if self.pending or name:
            if not e.get("id"):
                e.set("id", make_id(self.pending[0] if self.pending else name, self.ids))
            for n in self.pending:
                self.targets.setdefault(n, ("id", e.get("id")))
            if name:
                self.targets.setdefault(_norm(name), ("id", e.get("id")))
            self.pending = []
        parent.append(e)
        return e

    def blocks(self, lines: list[str], parent: ET.Element, depth: int = 0, sections: bool = False) -> None:
        if depth > MAX_DEPTH:
            self.emit(parent, el("pre", "\n".join(lines)))
            return
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            c = line[0]
            if c == " ":
                j = i
                while j < n and (not lines[j].strip() or lines[j][0] == " "):
                    j += 1
                block = _dedent(lines[i:j])
                self.block_quote(block, parent, depth)
                i = j
                continue
            if c == "." and _EXPLICIT.match(line):
                i = self.explicit(lines, i, parent, depth, sections)
                continue
            if c == "_" and line.startswith("__ "):
                body, j = _indented_after(lines, i + 1)
                self.anon_targets.append(("url", "".join([line[3:].strip(), *body]).replace(" ", "")))
                i = j
                continue
            if c == ">" and (line.startswith(">>> ") or line.rstrip() == ">>>"):
                j = i
                while j < n and lines[j].strip():
                    j += 1
                self.emit(parent, el("pre", el("code", "\n".join(lines[i:j]), class_="language-pycon")))
                i = j
                continue
            if c == "|" and _LINE_BLOCK.match(line):
                i = self.line_block(lines, i, parent)
                continue
            if c == "+" and _GRID_TOP.match(line):
                j = i
                while j < n and lines[j].strip() and lines[j].lstrip()[:1] in ("+", "|"):
                    j += 1
                self.emit(parent, self.grid_table(lines[i:j], depth))
                i = j
                continue
            if c == "=" and _SIMPLE_TOP.match(line):
                i = self.simple_table(lines, i, parent, depth)
                continue
            if _ADORN.match(line) and len(line.strip()) > 1:
                t = self._overline(lines, i)
                if t is not None:
                    title, i = t
                    self.section(title, line.strip()[0], True, parent, sections)
                    continue
                if len(line.strip()) >= 4 and (i == 0 or not lines[i - 1].strip()) and (i + 1 >= n or not lines[i + 1].strip()):
                    if sections or depth == 0:
                        self.emit(parent, el("hr"))
                    i += 1
                    continue
            if c in "-*+•‣⁃" and _BULLET.match(line):
                i = self.bullet_list(lines, i, parent, depth)
                continue
            m = _ENUM.match(line) if (c.isalnum() or c in "#(") else None
            if m and self._enum_ok(lines, i, m):
                i = self.enum_list(lines, i, parent, depth)
                continue
            if c == ":" and _FIELD.match(line):
                i = self.field_list(lines, i, parent, depth)
                continue
            if c in "-/+" and _OPTION.match(line) and self._option_ok(lines, i):
                i = self.option_list(lines, i, parent, depth)
                continue
            # text: a section title, a definition list or a paragraph
            if i + 1 < n:
                nxt = lines[i + 1]
                if nxt and nxt[0] != " " and _ADORN.match(nxt):
                    u = nxt.rstrip()
                    if len(u) >= len(line.rstrip()) or len(u) >= 4:
                        if sections:
                            self.section(line.strip(), u[0], False, parent, sections)
                        else:
                            self.paragraph([line], parent)
                        i += 2
                        continue
                if nxt.strip() and nxt[0] == " ":
                    i = self.definition_list(lines, i, parent, depth)
                    continue
            i = self.paragraph_at(lines, i, parent, depth)

    def _overline(self, lines: list[str], i: int):
        """(title, next line index) when lines[i] is an overline with a title and matching underline."""
        if i < 0 or i + 2 >= len(lines):
            return None
        over, title, under = lines[i].rstrip(), lines[i + 1], lines[i + 2].rstrip()
        if not title.strip() or over != under or not _ADORN.match(under) or len(over) < len(title.strip()):
            return None
        return title.strip(), i + 3

    def section(self, title: str, char: str, over: bool, parent: ET.Element, sections: bool) -> None:
        style = (char, over)
        if style not in self.styles:
            self.styles.append(style)
        level = min(self.styles.index(style) + 1, 6)
        h = el(f"h{level}", self.inline(title))
        text = " ".join(markup.text_of(h).split())
        self.emit(parent, h, name=text)
        self.headings[h.get("id")] = h

    def paragraph_at(self, lines: list[str], i: int, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        j = i
        while j < n and lines[j].strip() and (j == i or lines[j][0] != " "):
            j += 1
        para = lines[i:j]
        literal = self.paragraph(para, parent)
        if literal:
            # a literal block follows: indented, or "quoted" (each line starting with the same punctuation)
            k = j
            while k < n and not lines[k].strip():
                k += 1
            if k < n and lines[k][0] == " ":
                e = k
                while e < n and (not lines[e].strip() or lines[e][0] == " "):
                    e += 1
                block = _dedent(lines[k:e])
                while block and not block[-1].strip():
                    block.pop()
                self.emit(parent, el("pre", el("code", "\n".join(block), class_=f"language-{self.highlight}"
                                                if self.highlight else None)))
                return e
            if k < n and lines[k][0] in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~":
                q = lines[k][0]
                e = k
                while e < n and lines[e][:1] == q:
                    e += 1
                self.emit(parent, el("pre", el("code", "\n".join(lines[k:e]))))
                return e
        return j

    def paragraph(self, para: list[str], parent: ET.Element) -> bool:
        """A paragraph; True when it ends with "::" (a literal block follows)."""
        text = "\n".join(s.strip() for s in para)
        literal = text.endswith("::") and not text.endswith("\\::")
        if literal:
            if text == "::":
                return True
            text = text[:-3] if text[-3:-2].isspace() else text[:-1]
        self.emit(parent, el("p", self.inline(text)))
        return literal

    def block_quote(self, lines: list[str], parent: ET.Element, depth: int) -> None:
        q = el("blockquote")
        self.blocks(lines, q, depth + 1)
        last = q[-1] if len(q) else None
        if last is not None and last.tag == "p" and last.text and _ATTRIBUTION.match(last.text) and len(q) > 1:
            last.set("class", "attribution")
            last.text = "— " + last.text[_ATTRIBUTION.match(last.text).end():]
        self.emit(parent, q)

    def line_block(self, lines: list[str], i: int, parent: ET.Element) -> int:
        n = len(lines)
        out: list[str] = []
        while i < n and _LINE_BLOCK.match(lines[i]):
            text = lines[i][2:] if len(lines[i]) > 1 else ""
            i += 1
            while i < n and lines[i].strip() and lines[i][0] == " ":
                text += " " + lines[i].strip()
                i += 1
            out.append(text)
        div = el("div", class_="line-block")
        for k, t in enumerate(out):
            if k:
                div.append(el("br"))
            lead = len(t) - len(t.lstrip(" "))
            node_append(div, [" " * lead, *self.inline(t.strip())])
        self.emit(parent, div)
        return i

    # --- lists ---------------------------------------------------------------------------------
    def _item(self, lines: list[str], i: int, text_start: int) -> tuple[list[str], int]:
        """A list item's lines (first line from ``text_start``, the rest dedented) and where it ends."""
        n = len(lines)
        first = lines[i][text_start:]
        j = i + 1
        while j < n and (not lines[j].strip() or lines[j][0] == " "):
            j += 1
        rest = lines[i + 1:j]
        while rest and not rest[-1].strip():
            rest.pop()
            j -= 1
        while j < n and not lines[j].strip():
            j += 1
        # the body lines up with the item's text: what is indented further is a literal block or a
        # quote inside the item ("- Install::" then the code), so only text_start columns come off
        if all(not s.strip() or len(s) - len(s.lstrip(" ")) >= text_start for s in rest):
            return [first, *[s[text_start:] for s in rest]], j
        return [first, *_dedent(rest)], j

    def _fill_item(self, item: ET.Element, body: list[str], depth: int) -> None:
        self.blocks(body, item, depth + 1)
        if item.tag == "dd" and len(item) == 1 and item[0].tag == "p" and not item[0].get("id"):
            unwrap(item, item[0])

    @staticmethod
    def compact(lst: ET.Element) -> ET.Element:
        """A list whose items are each one paragraph (and maybe a nested list) is "simple", as docutils
        says: its items hold the text without a <p>."""
        items = [li for li in lst if li.tag == "li"]
        if all(len(li) and li[0].tag == "p" and not li[0].get("id") and not (li.text or "").strip()
               and all(c.tag in ("ul", "ol") for c in li[1:]) for li in items):
            for li in items:
                unwrap(li, li[0])
        return lst

    def bullet_list(self, lines: list[str], i: int, parent: ET.Element, depth: int) -> int:
        char = lines[i][0]
        ul = el("ul")
        n = len(lines)
        while i < n and lines[i][:1] == char and _BULLET.match(lines[i]):
            m = _BULLET.match(lines[i])
            body, i = self._item(lines, i, m.end())
            li = ET.SubElement(ul, "li")
            self._fill_item(li, body, depth)
        self.emit(parent, self.compact(ul))
        return i

    @staticmethod
    def _enum_kind(m) -> tuple[str, str, str]:
        """(format, sequence, value) of an enumerator."""
        if m.group(1) is not None:
            fmt, v = "period" if m.group(2) == "." else "rparen", m.group(1)
        else:
            fmt, v = "parens", m.group(3)
        if v == "#":
            seq = "auto"
        elif v.isdigit():
            seq = "arabic"
        elif _roman(v) is not None and (len(v) > 1 or v in "iI"):
            seq = "lowerroman" if v.islower() else "upperroman"
        else:
            seq = "loweralpha" if v.islower() else "upperalpha"
        return fmt, seq, v

    def _enum_ok(self, lines: list[str], i: int, m) -> bool:
        if i + 1 >= len(lines):
            return True
        nxt = lines[i + 1]
        if not nxt.strip() or nxt[0] == " ":
            return True
        m2 = _ENUM.match(nxt)
        return bool(m2) and self._enum_kind(m2)[0] == self._enum_kind(m)[0]

    def enum_list(self, lines: list[str], i: int, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        m = _ENUM.match(lines[i])
        fmt, seq, v = self._enum_kind(m)
        ol = el("ol")
        start = (1 if seq == "auto" else _int(v, 1) if seq == "arabic" else _roman(v) if "roman" in seq
                 else ord(v.lower()) - 96)
        if start != 1:
            ol.set("start", str(start))
        typ = {"loweralpha": "a", "upperalpha": "A", "lowerroman": "i", "upperroman": "I"}.get(seq)
        if typ:
            ol.set("type", typ)
        first = True
        while i < n:
            m = _ENUM.match(lines[i])
            if not m:
                break
            f2, s2, _v = self._enum_kind(m)
            if not first and (f2 != fmt or (s2 != seq and s2 != "auto" and seq != "auto"
                                            and not ({s2, seq} <= {"loweralpha", "lowerroman"}
                                                     or {s2, seq} <= {"upperalpha", "upperroman"}))):
                break
            first = False
            body, i = self._item(lines, i, m.end())
            li = ET.SubElement(ol, "li")
            self._fill_item(li, body, depth)
        self.emit(parent, self.compact(ol))
        return i

    def field_list(self, lines: list[str], i: int, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        dl = el("dl", class_="field-list")
        while i < n and lines[i][:1] == ":":
            m = _FIELD.match(lines[i])
            if not m:
                break
            body, i = self._item(lines, i, m.end())
            name = m.group(1)
            dl.append(el("dt", self.inline(name)))
            dd = ET.SubElement(dl, "dd")
            self._fill_item(dd, body, depth)
            if parent.tag == "body" and (len(parent) == 0 or len(parent) == 1 and parent[0].tag in ("h1", "h2")):
                key = _norm(name)
                if key in ("author", "authors", "date", "version", "copyright", "organization"):
                    self.meta.setdefault(key, " ".join(markup.text_of(dd).split()))
        self.emit(parent, dl)
        return i

    def _option_ok(self, lines: list[str], i: int) -> bool:
        m = _OPTION.match(lines[i])
        if m.group(2):
            return True
        return i + 1 < len(lines) and lines[i + 1].strip() != "" and lines[i + 1][0] == " "

    def option_list(self, lines: list[str], i: int, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        dl = el("dl", class_="option-list")
        while i < n and lines[i][:1] in "-/+":
            m = _OPTION.match(lines[i])
            if not m or not self._option_ok(lines, i):
                break
            opts = m.group(1)
            start = m.start(2) if m.group(2) else len(lines[i])
            body, i = self._item(lines, i, start)
            if not body[0].strip():
                body = body[1:]
            dl.append(el("dt", el("code", opts)))
            dd = ET.SubElement(dl, "dd")
            self.blocks(body, dd, depth + 1)
        self.emit(parent, dl)
        return i

    def definition_list(self, lines: list[str], i: int, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        dl = el("dl")
        while i + 1 < n and lines[i].strip() and lines[i][0] != " " and lines[i + 1].strip() and lines[i + 1][0] == " ":
            term = lines[i].strip()
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j][0] == " "):
                j += 1
            body = _dedent(lines[i + 1:j])
            parts = re.split(r" +: +", term)
            dt = el("dt", self.inline(parts[0]))
            for cl in parts[1:]:
                dt.append(el("span", [" : ", *self.inline(cl)], class_="classifier"))
            dl.append(dt)
            dd = ET.SubElement(dl, "dd")
            self.blocks(body, dd, depth + 1)
            i = j
            while i < n and not lines[i].strip():
                i += 1
            if i < n and (_EXPLICIT.match(lines[i]) or _BULLET.match(lines[i]) or _ENUM.match(lines[i])):
                break
        self.emit(parent, dl)
        return i

    # --- tables ----------------------------------------------------------------------------------
    def _cell(self, cell: ET.Element, lines: list[str], depth: int) -> None:
        lines = _dedent([s.rstrip() for s in lines])
        while lines and not lines[0].strip():
            lines.pop(0)
        self.blocks(lines, cell, depth + 1)
        if len(cell) == 1 and cell[0].tag == "p" and not cell[0].get("id"):
            unwrap(cell, cell[0])

    def grid_table(self, lines: list[str], depth: int) -> ET.Element:
        lines = [s.rstrip() for s in lines]
        width = max(len(s) for s in lines)
        if width * len(lines) > 4_000_000:
            return el("pre", "\n".join(lines))
        g = [s.ljust(width) for s in lines]
        h = len(g)
        head = next((y for y in range(1, h) if g[y].startswith("+=")), None)
        cells: list[tuple[int, int, int, int]] = []
        todo, seen = [(0, 0)], set()
        while todo:
            y, x = todo.pop()
            if (y, x) in seen or y >= h - 1 or x >= width - 1:
                continue
            seen.add((y, x))
            found = _scan_cell(g, y, x)
            if found:
                y2, x2 = found
                cells.append((y, x, y2, x2))
                todo += [(y, x2), (y2, x)]
        if not cells:
            return el("pre", "\n".join(lines))
        rows_at = sorted({c[0] for c in cells} | {c[2] for c in cells})
        cols_at = sorted({c[1] for c in cells} | {c[3] for c in cells})
        table = el("table")
        thead = el("thead") if head else None
        tbody = el("tbody")
        by_row: dict[int, list] = {}
        for c in sorted(cells):
            by_row.setdefault(c[0], []).append(c)
        for y in sorted(by_row):
            in_head = head is not None and y < head
            tr = ET.SubElement(thead if in_head else tbody, "tr")
            for (y1, x1, y2, x2) in sorted(by_row[y], key=lambda c: c[1]):
                td = ET.SubElement(tr, "th" if in_head else "td")
                rs = rows_at.index(y2) - rows_at.index(y1)
                cs = cols_at.index(x2) - cols_at.index(x1)
                if rs > 1:
                    td.set("rowspan", str(rs))
                if cs > 1:
                    td.set("colspan", str(cs))
                self._cell(td, [g[k][x1 + 1:x2] for k in range(y1 + 1, y2)], depth)
        if thead is not None and len(thead):
            table.append(thead)
        table.append(tbody)
        return table

    def simple_table(self, lines: list[str], i: int, parent: ET.Element, depth: int) -> int:
        n = len(lines)
        border = lines[i].rstrip()
        cols = [(m.start(), m.end()) for m in re.finditer(r"=+", border)]
        j = i + 1
        rows: list[list[str]] = []
        head_rows = None
        while j < n:
            line = lines[j].rstrip()
            if _SIMPLE_TOP.match(line) or (line and set(line) <= {"=", " "} and line.startswith("=")):
                if j + 1 >= n or not lines[j + 1].strip():
                    j += 1
                    break
                if head_rows is None:
                    head_rows = len(rows)
                j += 1
                continue
            if line and set(line) <= {"-", " "}:  # column-span underlines: not kept
                j += 1
                continue
            if not line.strip():
                if rows:
                    rows[-1].append("")
                j += 1
                continue
            if line[:cols[0][1]].strip() or not rows:
                rows.append([line])
            else:
                rows[-1].append(line)
            j += 1
        table = el("table")
        thead = el("thead") if head_rows else None
        tbody = el("tbody")
        starts = [c[0] for c in cols]
        for r, row in enumerate(rows):
            tr = ET.SubElement(thead if head_rows and r < head_rows else tbody, "tr")
            for k, s in enumerate(starts):
                end = starts[k + 1] if k + 1 < len(starts) else None
                td = ET.SubElement(tr, "th" if head_rows and r < head_rows else "td")
                self._cell(td, [ln[s:end] for ln in row], depth)
        if thead is not None:
            table.append(thead)
        table.append(tbody)
        self.emit(parent, table)
        return j

    # --- explicit markup -------------------------------------------------------------------------
    def explicit(self, lines: list[str], i: int, parent: ET.Element, depth: int, sections: bool) -> int:
        first = lines[i][2:].strip()
        if not first and (i + 1 >= len(lines) or not lines[i + 1].strip()):
            return i + 1  # an empty comment: it ends what came before and takes nothing after it
        body, j = _indented_after(lines, i + 1)
        m = _FOOTNOTE.match(first)
        if m:
            self.footnote(m.group(1), [m.group(2) or "", *body], parent, depth)
            return j
        m = _TARGET.match(first)
        if m:
            self.target(m.group(1), [m.group(2) or "", *body])
            return j
        m = _SUBDEF.match(first)
        if m:
            self.subdef(m.group(1), m.group(2).lower(), m.group(3) or "", body, depth)
            return j
        m = _DIRECTIVE.match(first)
        if m:
            self.directive(m.group(1), m.group(2) or "", body, parent, depth, sections)
            return j
        return j  # a comment

    def footnote(self, label: str, body: list[str], parent: ET.Element, depth: int) -> None:
        if label.isdigit() or label[0] in "#*":
            aside = el("aside", epub__type="footnote")
            self.blocks(_dedent_first(body), aside, depth + 1)
            self.footnotes.append((label, aside))
            parent.append(aside)
            return
        # a citation: kept where it is, like docutils, as a definition list
        cid = make_id("cite-" + label, self.ids)
        self.citations[_norm(label)] = cid
        prev = parent[-1] if len(parent) else None
        dl = prev if prev is not None and prev.tag == "dl" and prev.get("class") == "citations" else None
        if dl is None:
            dl = self.emit(parent, el("dl", class_="citations"))
        dl.append(el("dt", f"[{label}]", id=cid))
        dd = ET.SubElement(dl, "dd")
        self.blocks(_dedent_first(body), dd, depth + 1)

    def target(self, name: str, body: list[str]) -> None:
        link = "".join(s.strip() for s in body)
        if name == "_":
            if link.endswith("_") and not link.endswith("\\_") and " " not in link.strip("`_"):
                self.anon_targets.append(("ref", _norm(link[:-1].strip("`"))))
            else:
                self.anon_targets.append(("url", _unescape(link)) if link else ("pending", ""))
            if not link:
                self.pending.append(f"\0anon{len(self.anon_targets)}")
            return
        name = _norm(name.strip("`"))
        if not link:
            self.pending.append(name)
            return
        if link.endswith("_") and not link.endswith("\\_") and " " not in link.strip("`_"):
            self.targets[name] = ("ref", _norm(link[:-1].strip("`")))
        else:
            self.targets[name] = ("url", _unescape(link))

    def subdef(self, name: str, kind: str, first: str, body: list[str], depth: int) -> None:
        args, opts, _content = _directive_parts(first, body)
        name = _unescape(name)
        if kind == "replace":
            nodes = self.inline(" ".join(s.strip() for s in [first, *body] if s.strip()))
        elif kind == "image":
            img = el("img", src=args.replace("\n", "").replace(" ", ""), alt=opts.get("alt", name))
            nodes = [el("a", img, href=opts["target"]) if opts.get("target") else img]
        elif kind == "unicode":
            nodes = [_unicode(" ".join([first, *body]))]
        elif kind == "date":
            import time
            try:  # a format the platform refuses ("%Q" on Windows) is shown as written
                nodes = [time.strftime(args.strip() or "%Y-%m-%d")]
            except ValueError:
                nodes = [args.strip()]
        else:
            holder = el("div")
            self.directive(kind, first, body, holder, depth, False)
            nodes = [el("span", list(holder[0])) if len(holder) and len(holder[0]) else markup.text_of(holder)]
        self.subs[name] = nodes

    # --- directives ------------------------------------------------------------------------------
    def directive(self, name: str, first: str, body: list[str], parent: ET.Element, depth: int, sections: bool) -> None:
        lname = name.lower()
        bare = lname.split(":", 1)[1] if lname.startswith(("py:", "std:", "rst:")) else lname
        if bare in _SILENT or lname in _SILENT:
            if bare == "highlight" or bare == "highlightlang":
                self.highlight = first.strip()
            elif bare == "default-role":
                self.default_role = first.strip() or "title-reference"
            elif bare == "role":
                m = re.match(r"^([\w.:+-]+)(?:\(([\w.:+-]+)\))?", first.strip())
                if m:
                    _a, opts, _c = _directive_parts("", body)
                    self.roles[m.group(1).lower()] = ((m.group(2) or "").lower(), opts.get("format", ""))
            elif bare == "title":
                self.meta["title"] = first.strip()
            return
        if bare in _ADMONITIONS or bare == "admonition":
            args, opts, content = _directive_parts(first, body, content_first=bare != "admonition")
            div = el("div", class_=f"admonition {bare}")
            title = self.inline(args.strip()) if bare == "admonition" else [_ADMONITIONS[bare]]
            div.append(el("p", title, class_="admonition-title"))
            self.blocks(content, div, depth + 1)
            self.emit(parent, div)
            return
        if bare in _VERSIONS or bare == "deprecated-removed":
            self.version_note(bare, first, body, parent, depth)
            return
        if ":" in lname and lname.split(":", 1)[0] in ("py", "c", "cpp", "js", "rst", "std", "math") or bare in _OBJECTS:
            self.object_description(lname, first, body, parent, depth)
            return
        handler = getattr(self, "d_" + bare.replace("-", "_"), None)
        if handler is not None:
            handler(first, body, parent, depth, sections)
            return
        # unknown: the arguments and the content are kept as text
        self.drop("directive:" + lname, f"reStructuredText directive '{name}' isn't supported; its text was kept")
        args, opts, content = _directive_parts(first, body)
        div = el("div", class_=re.sub(r"[^\w-]", "-", lname))
        if args.strip():
            div.append(el("p", self.inline(args.strip())))
        self.blocks(content, div, depth + 1)
        self.emit(parent, div)

    def version_note(self, kind: str, first: str, body: list[str], parent: ET.Element, depth: int) -> None:
        args, _opts, content = _directive_parts(first, body)
        parts = args.split(None, 2 if kind == "deprecated-removed" else 1)
        if kind == "deprecated-removed":
            ver = parts[0] if parts else ""
            rem = parts[1] if len(parts) > 1 else ""
            label, cls = f"Deprecated since version {ver}, will be removed in version {rem}", "deprecated"
            explain = parts[2] if len(parts) > 2 else ""
        else:
            label = _VERSIONS[kind][0].format(parts[0] if parts else "")
            cls = _VERSIONS[kind][1]
            explain = parts[1] if len(parts) > 1 else ""
        div = el("div", class_=kind)
        if explain.strip() or content:
            p = el("p", el("span", label + ": ", class_=f"versionmodified {cls}"))
            if explain.strip():
                node_append(p, self.inline(explain.strip()))
                div.append(p)
                self.blocks(content, div, depth + 1)
            else:
                self.blocks(content, div, depth + 1)
                first_p = div[0] if len(div) and div[0].tag == "p" else None
                if first_p is not None:
                    span = p[0]
                    span.tail = first_p.text
                    first_p.text = None
                    first_p.insert(0, span)
                else:
                    div.insert(0, p)
        else:
            div.append(el("p", el("span", label + ".", class_=f"versionmodified {cls}")))
        self.emit(parent, div)

    def object_description(self, name: str, first: str, body: list[str], parent: ET.Element, depth: int) -> None:
        args, opts, content = _directive_parts(first, body)
        bare = name.split(":", 1)[1] if ":" in name else name
        prefix = _OBJECTS.get(bare, "")
        dl = el("dl", class_=re.sub(r"[^\w-]", " ", name).strip())
        sigs = [s.strip() for s in re.split(r"(?<!\\)\n", args) if s.strip()] or [""]
        for sig in sigs:
            shown = prefix + _unescape(sig).replace("\\\n", "")
            if opts.get("type"):
                shown += ": " + opts["type"]
            if opts.get("value"):
                shown += " = " + opts["value"]
            dt = el("dt", el("code", shown))
            key = re.split(r"[\s(\[]", sig.strip(), maxsplit=1)[0]
            if key and "noindex" not in opts and "no-index" not in opts:
                dt.set("id", make_id(key, self.ids))
            dl.append(dt)
        dd = ET.SubElement(dl, "dd")
        self.blocks(content, dd, depth + 1)
        self.emit(parent, dl)

    def _code(self, code: str, lang: str, parent: ET.Element, caption: str = "") -> None:
        if caption:
            self.emit(parent, el("p", self.inline(caption), class_="code-caption"))
        lang = lang.strip()
        self.emit(parent, el("pre", el("code", code, class_=f"language-{lang}" if lang and lang != "none" else None)))

    def d_code(self, first, body, parent, depth, sections):
        args, opts, content = _directive_parts(first, body, max_args=1)
        while content and not content[-1].strip():
            content.pop()
        self._code("\n".join(content), args or self.highlight, parent, opts.get("caption", ""))

    d_code_block = d_sourcecode = d_code

    def d_doctest(self, first, body, parent, depth, sections):
        _a, _o, content = _directive_parts(first, body, max_args=1)
        self._code("\n".join(content).rstrip("\n"), "pycon", parent)

    def d_testcode(self, first, body, parent, depth, sections):
        _a, _o, content = _directive_parts(first, body, max_args=1)
        self._code("\n".join(content).rstrip("\n"), "python", parent)

    def d_testoutput(self, first, body, parent, depth, sections):
        _a, opts, content = _directive_parts(first, body, max_args=1)
        if "hide" not in opts:
            self._code("\n".join(content).rstrip("\n"), "", parent)

    def d_productionlist(self, first, body, parent, depth, sections):
        lines = [first, *body] if first.strip() else body
        self._code("\n".join(_dedent([s for s in lines if s.strip()])), "", parent)

    def d_parsed_literal(self, first, body, parent, depth, sections):
        _a, _o, content = _directive_parts(first, body)
        self.emit(parent, el("pre", self.inline("\n".join(content).rstrip("\n"))))

    def d_math(self, first, body, parent, depth, sections):
        args, opts, content = _directive_parts(first, body)
        # the argument is an equation of its own; blank lines separate the content's equations
        text = "\n\n".join([args, "\n".join(content)]) if args.strip() else "\n".join(content)
        for eq in re.split(r"\n\s*\n", text.strip()):
            if eq.strip():
                p = el("p", mathml.from_tex(eq.strip(), display=True))
                self.emit(parent, p, name=opts.get("label") or opts.get("name"))

    def d_raw(self, first, body, parent, depth, sections):
        args, opts, content = _directive_parts(first, body)
        fmt = args.split()[0].lower() if args.split() else ""
        text = "\n".join(content)
        if opts.get("file") or opts.get("url"):
            self.lost.append(f"A raw {fmt} file wasn't read: {opts.get('file') or opts.get('url')}")
            return
        if fmt == "html":
            lead, elems = markup.parse_fragment(text)
            if lead.strip():
                self.emit(parent, el("p", lead.strip()))
            for e in elems:
                e.tail = None
                self.emit(parent, e)
        else:
            self.emit(parent, el("pre", text, class_="raw", data_format=fmt))

    def d_image(self, first, body, parent, depth, sections):
        args, opts, _c = _directive_parts(first, body)
        img = el("img", src=_unescape(args.replace("\n", "").replace(" ", "")), alt=opts.get("alt", ""))
        node = el("a", img, href=opts["target"]) if opts.get("target") else img
        self.emit(parent, el("p", node), name=opts.get("name"))

    def d_figure(self, first, body, parent, depth, sections):
        args, opts, content = _directive_parts(first, body)
        fig = el("figure")
        holder = el("div")
        self.blocks(content, holder, depth + 1)
        caption = holder[0] if len(holder) and holder[0].tag == "p" else None
        cap_text = " ".join(markup.text_of(caption).split()) if caption is not None else ""
        img = el("img", src=_unescape(args.replace("\n", "").replace(" ", "")), alt=opts.get("alt", cap_text))
        fig.append(el("a", img, href=opts["target"]) if opts.get("target") else img)
        if caption is not None:
            fc = el("figcaption")
            node_append(fc, [caption.text or "", *list(caption)])
            fig.append(fc)
            holder.remove(caption)
        if len(holder):
            fig.append(el("div", list(holder), class_="legend"))
        self.emit(parent, fig, name=opts.get("name"))

    def d_topic(self, first, body, parent, depth, sections, kind="topic"):
        args, opts, content = _directive_parts(first, body)
        div = el("div", class_=kind)
        div.append(el("p", el("strong", self.inline(args.strip())), class_=f"{kind}-title"))
        if opts.get("subtitle"):
            div.append(el("p", self.inline(opts["subtitle"]), class_=f"{kind}-subtitle"))
        self.blocks(content, div, depth + 1)
        self.emit(parent, div)

    def d_sidebar(self, first, body, parent, depth, sections):
        self.d_topic(first, body, parent, depth, sections, kind="sidebar")

    def d_rubric(self, first, body, parent, depth, sections):
        args, _o, _c = _directive_parts(first, body)
        self.emit(parent, el("p", el("strong", self.inline(args.strip())), class_="rubric"))

    def d_centered(self, first, body, parent, depth, sections):
        args, _o, _c = _directive_parts(first, body)
        self.emit(parent, el("p", el("strong", self.inline(args.strip())), class_="centered"))

    def _wrapper(self, tag: str, cls: str, first, body, parent, depth, content_first=True):
        _a, _o, content = _directive_parts(first, body, content_first=content_first)
        div = el(tag, class_=cls)
        self.blocks(content, div, depth + 1)
        self.emit(parent, div)
        return div

    def d_epigraph(self, first, body, parent, depth, sections):
        self._wrapper("blockquote", "epigraph", first, body, parent, depth)

    def d_highlights(self, first, body, parent, depth, sections):
        self._wrapper("blockquote", "highlights", first, body, parent, depth)

    def d_pull_quote(self, first, body, parent, depth, sections):
        self._wrapper("blockquote", "pull-quote", first, body, parent, depth)

    def d_container(self, first, body, parent, depth, sections):
        self._wrapper("div", "container " + first.strip(), "", body, parent, depth, content_first=False)

    def d_compound(self, first, body, parent, depth, sections):
        self._wrapper("div", "compound", first, body, parent, depth)

    def d_hlist(self, first, body, parent, depth, sections):
        self._wrapper("div", "hlist", first, body, parent, depth)

    def d_glossary(self, first, body, parent, depth, sections):
        _a, _o, content = _directive_parts(first, body)
        div = el("div")
        self.blocks(content, div, depth + 1)
        for dl in div.iter("dl"):
            for dt in dl.findall("dt"):
                term = " ".join(markup.text_of(dt).split())
                if term and not dt.get("id"):
                    dt.set("id", make_id("term-" + term, self.ids))
                    self.targets.setdefault("term:" + term.lower(), ("id", dt.get("id")))
        for c in list(div):
            self.emit(parent, c)

    def d_ifconfig(self, first, body, parent, depth, sections):
        self._wrapper("div", "ifconfig", "", body, parent, depth, content_first=False)

    def d_line_block(self, first, body, parent, depth, sections):
        _a, _o, content = _directive_parts(first, body)
        self.line_block(["| " + s.rstrip() for s in content if s.strip()], 0, parent)

    def d_only(self, first, body, parent, depth, sections):
        args, _o, content = _directive_parts(first, body)
        if _only(args):
            self.blocks(content, parent, depth + 1, sections)

    def d_availability(self, first, body, parent, depth, sections):
        args, _o, content = _directive_parts(first, body)
        p = el("p", [el("strong", "Availability"), ": ", *self.inline(args.strip())], class_="availability")
        self.emit(parent, p)
        self.blocks(content, parent, depth + 1)

    def d_audit_event(self, first, body, parent, depth, sections):
        args, _o, content = _directive_parts(first, body)
        parts = args.split(None, 2)
        name = parts[0] if parts else ""
        call = [a.strip() for a in parts[1].split(",") if a.strip()] if len(parts) > 1 else []
        if content:
            div = el("div", class_="audit-hook")
            self.blocks(content, div, depth + 1)
            self.emit(parent, div)
            return
        words = ["Raises an ", el("a", "auditing event", href="https://docs.python.org/3/library/sys.html#auditing"),
                 " ", el("code", name)]
        if call:
            words += [" with argument" + ("s " if len(call) > 1 else " ")]
            for k, a in enumerate(call):
                words += [", " if k else "", el("code", a)]
        else:
            words += [" with no arguments"]
        self.emit(parent, el("p", [*words, "."], class_="audit-hook"))

    def d_toctree(self, first, body, parent, depth, sections):
        _a, opts, content = _directive_parts(first, body, content_first=False)
        if "hidden" in opts:
            return
        if opts.get("caption"):
            self.emit(parent, el("p", el("strong", opts["caption"]), class_="caption"))
        ul = el("ul", class_="toctree")
        for line in content:
            s = line.strip()
            if not s or s.startswith(":"):
                continue
            m = re.match(r"^(.*?)\s*<([^<>]+)>$", s)
            title, doc = (m.group(1), m.group(2)) if m else ("", s)
            if is_external(doc):
                href = doc
            elif doc == "self":
                continue
            else:
                href = doc.lstrip("/") + ".html"
            ET.SubElement(ul, "li").append(el("a", title or doc, href=href))
        if len(ul):
            self.emit(parent, ul)

    def d_contents(self, first, body, parent, depth, sections):
        args, opts, _c = _directive_parts(first, body)
        div = el("div", class_="contents topic")
        div.append(el("p", el("strong", args.strip() or "Contents"), class_="topic-title"))
        d = opts.get("depth", "")
        self.contents.append((div, _int(d, 99), "local" in opts))
        self.emit(parent, div)

    def d_include(self, first, body, parent, depth, sections):
        args, opts, _c = _directive_parts(first, body)
        name = args.strip()
        if name.startswith("<") and name.endswith(">"):
            self.drop("std-include", f"A docutils standard definitions file wasn't read: {name}")
            return
        text = read_include(self.base_dirs[-1], name, self.top, self.lost, self.seen, count=self.include_count)
        if text is None:
            return
        lines = _lines(text)
        lines = _slice_lines(lines, opts)
        if "literal" in opts or "code" in opts:
            self._code("\n".join(lines).rstrip("\n"), opts.get("code", ""), parent)
            return
        p = safe_path(self.base_dirs[-1], name, self.top)
        self.seen.append(p)
        self.base_dirs.append(p.parent)
        try:
            self.blocks(lines, parent, depth + 1, sections)
        finally:
            self.seen.pop()
            self.base_dirs.pop()

    def d_literalinclude(self, first, body, parent, depth, sections):
        args, opts, _c = _directive_parts(first, body)
        name = args.strip()
        text = read_include(self.base_dirs[-1], name, self.top, self.lost, self.seen, count=self.include_count)
        if text is None:
            return
        lines = _slice_lines(_lines(text), opts)
        if opts.get("lines"):
            keep: list[str] = []
            for part in opts["lines"].split(","):
                a, _, b = part.strip().partition("-")
                lo = _int(a, 1)
                hi = _int(b, len(lines)) if _ else lo
                keep += lines[lo - 1:hi]
            lines = keep
        if opts.get("pyobject"):
            self.drop("pyobject", "literalinclude :pyobject: isn't supported; the whole file was included")
        if opts.get("dedent", "").isdigit():
            k = _int(opts["dedent"], 0)
            lines = [s[k:] if s[:k].strip() == "" else s.lstrip() for s in lines]
        self._code("\n".join(lines).rstrip("\n"), opts.get("language", self.highlight), parent, opts.get("caption", ""))

    def d_table(self, first, body, parent, depth, sections):
        args, _o, content = _directive_parts(first, body)
        holder = el("div")
        self.blocks(content, holder, depth + 1)
        for c in list(holder):
            if c.tag == "table" and args.strip():
                c.insert(0, el("caption", self.inline(args.strip())))
            self.emit(parent, c)

    def d_list_table(self, first, body, parent, depth, sections):
        args, opts, content = _directive_parts(first, body)
        holder = el("div")
        self.blocks(content, holder, depth + 1)
        outer = holder.find("ul")
        rows: list[list[ET.Element]] = []
        if outer is not None:
            for li in outer.findall("li"):
                inner = li.find("ul")
                rows.append(inner.findall("li") if inner is not None else [])
        self.emit(parent, self._table_from(rows, args, opts))

    def d_csv_table(self, first, body, parent, depth, sections):
        args, opts, content = _directive_parts(first, body)
        text = "\n".join(content)
        if opts.get("file"):
            got = read_include(self.base_dirs[-1], opts["file"].strip(), self.top, self.lost, self.seen, "A table file", count=self.include_count)
            text = got or ""
        elif opts.get("url"):
            self.lost.append(f"A table from the web wasn't fetched: {opts['url']}")
        delim = {"tab": "\t", "space": " ", "comma": ","}.get(opts.get("delim", ","), opts.get("delim", ",")[:1] or ",")
        rdr = {"delimiter": delim, "quotechar": opts.get("quote", '"')[:1] or '"', "skipinitialspace": "keepspace" not in opts}
        if opts.get("escape"):
            rdr["escapechar"] = opts["escape"][:1]
            rdr["doublequote"] = False
        try:
            data = list(csv.reader(text.splitlines(), **rdr))
            header = list(csv.reader([opts["header"]], **rdr)) if opts.get("header") else []
        except csv.Error:
            data, header = [[s] for s in text.splitlines()], []
        rows = []
        for r in [*header, *data]:
            row = []
            for cell in r:
                td = el("td")
                self._cell(td, cell.split("\n"), depth)
                row.append(td)
            rows.append(row)
        if header:
            opts = {**opts, "header-rows": str(len(header) + _int(opts.get("header-rows", ""), 0))}
        self.emit(parent, self._table_from(rows, args, opts))

    def _table_from(self, rows: list[list[ET.Element]], title: str, opts: dict) -> ET.Element:
        table = el("table")
        if title.strip():
            table.append(el("caption", self.inline(title.strip())))
        hr = opts.get("header-rows", "0")
        heads = _int(hr, 0)
        thead = el("thead") if heads else None
        tbody = el("tbody")
        for r, row in enumerate(rows):
            tr = ET.SubElement(thead if r < heads else tbody, "tr")
            for cell in row:
                c = el("th" if r < heads else "td")
                c.text = cell.text
                c.extend(list(cell))
                if len(c) == 1 and c[0].tag == "p" and not (c.text or "").strip():
                    unwrap(c, c[0])
                tr.append(c)
        if thead is not None:
            table.append(thead)
        table.append(tbody)
        return table

    # --- inline ------------------------------------------------------------------------------------
    def inline(self, text: str) -> list:
        """Inline markup to a list of strings and elements."""
        out: list = []
        pos = 0
        n = len(text)
        ends: dict[str, list[int]] = {}
        buf: list[str] = []

        def end_of(token: str, start: int, escapable: bool = True) -> int:
            """The first place after ``start`` where ``token`` can end inline markup, or -1."""
            lst = ends.get(token)
            if lst is None:
                lst = []
                k = text.find(token)
                while k >= 0:
                    if k > 0 and not text[k - 1].isspace() and (not escapable or not _escaped(text, k)) \
                            and (token in "`|" or _ok_end(text, k + len(token))):
                        lst.append(k)
                    k = text.find(token, k + 1)
                ends[token] = lst
            from bisect import bisect_left
            idx = bisect_left(lst, start)
            return lst[idx] if idx < len(lst) else -1

        def flush():
            if buf:
                out.append("".join(buf))
                buf.clear()

        while pos < n:
            m = _INL.search(text, pos)
            if not m:
                buf.append(text[pos:])
                break
            s, e, kind = m.start(), m.end(), m.lastgroup
            buf.append(text[pos:s])
            pos = e
            if kind == "esc":
                ch = text[s + 1:s + 2]
                if ch and not ch.isspace():
                    buf.append(ch)
                continue
            if kind == "uri" or kind == "mail":
                url = m.group()
                if kind == "uri":
                    while url and url[-1] in ".,;:!?'\")]}" and not (url[-1] == ")" and url.count("(") >= url.count(")")):
                        url = url[:-1]
                    pos = s + len(url)
                    if url.rstrip(":").endswith(("http", "https", "ftp", "mailto")) or len(url) < 8 and "://" not in url:
                        buf.append(url)
                        continue
                flush()
                out.append(el("a", url, href=url if kind == "uri" else "mailto:" + url))
                continue
            if kind == "ref":
                word = m.group()
                anon = word.endswith("__")
                name = word[:-2] if anon else word[:-1]
                flush()
                out.append(el("a", name, data_rst_anon="1") if anon else el("a", name, data_rst_ref=_norm(name)))
                continue
            if kind == "fn":
                tok = m.group()
                if not _ok_start(text, s, s + 1):
                    buf.append(tok)
                    continue
                label = tok[1:tok.index("]")]
                flush()
                if label.isdigit() or label[0] in "#*":
                    out.append(el("a", data_rst_fn=label))
                else:
                    out.append(el("a", f"[{label}]", data_rst_cite=_norm(label)))
                continue
            if not _ok_start(text, s, e):
                buf.append(m.group())
                continue
            if kind == "lit":
                k = end_of("``", e, escapable=False)
                if k < 0:
                    buf.append(m.group())
                    continue
                flush()
                out.append(el("code", " ".join(text[e:k].split("\n"))))
                pos = k + 2
                continue
            if kind in ("strong", "emph"):
                tok = "**" if kind == "strong" else "*"
                k = end_of(tok, e)
                if k < 0:
                    buf.append(m.group())
                    continue
                flush()
                out.append(el("strong" if kind == "strong" else "em", _unescape(text[e:k])))
                pos = k + len(tok)
                continue
            if kind == "sub":
                k = end_of("|", e)
                for _ in range(64):
                    if k < 0:
                        break
                    sfx = 2 if text.startswith("__", k + 1) else 1 if text.startswith("_", k + 1) else 0
                    if _ok_end(text, k + 1 + sfx):
                        break
                    k = end_of("|", k + 1)
                if k < 0:
                    buf.append(m.group())
                    continue
                name = _unescape(text[e:k])
                after = text[k + 1:k + 3]
                pos = k + 1
                flush()
                node = el("span", data_rst_sub=" ".join(name.split()))
                if after.startswith("__"):
                    node = el("a", node, data_rst_anon="1")
                    pos += 2
                elif after.startswith("_"):
                    node = el("a", node, data_rst_ref=_norm(name))
                    pos += 1
                out.append(node)
                continue
            # `interpreted text`, `phrase reference`_, :role:`text`, `text`:role:, _`inline target`
            role = m.group()[1:-2] if kind == "role" else None
            k = e
            found = -1
            suffix = ""
            for _ in range(64):
                k = end_of("`", k)
                if k < 0:
                    break
                sm = _ROLE_SUFFIX.match(text, k + 1) if kind == "interp" else None
                if sm and _ok_end(text, k + 1 + len(sm.group())):
                    found, suffix = k, sm.group()
                    break
                if _ok_end(text, k + 1):
                    found = k
                    break
                k += 1
            if found < 0:
                buf.append(m.group())
                continue
            raw = text[e:found]
            pos = found + 1 + len(suffix)
            flush()
            if kind == "tgt":
                name = " ".join(_unescape(raw).split())
                span = el("span", name, id=make_id(name, self.ids))
                self.targets.setdefault(_norm(name), ("id", span.get("id")))
                out.append(span)
            elif suffix.startswith("_"):
                out.append(self.phrase_ref(raw, anon=suffix == "__"))
            else:
                if suffix.startswith(":"):
                    role = suffix[1:-1]
                out += self.role(role if role is not None else self.default_role, raw)
        flush()
        return out

    def phrase_ref(self, raw: str, anon: bool) -> ET.Element:
        m = re.match(r"^(.*?)\s*(?<!\\)<([^<>]*)>$", raw, re.DOTALL)
        if m:
            text, link = _unescape(m.group(1)).strip(), m.group(2)
            if link.endswith("_") and not link.endswith("\\_"):
                a = el("a", " ".join((text or link[:-1]).split()), data_rst_ref=_norm(link[:-1].strip("`")))
                return a
            url = _unescape("".join(link.split()))
            text = " ".join(text.split()) or url
            if not anon and m.group(1).strip():
                self.targets.setdefault(_norm(m.group(1)), ("url", url))
            return el("a", text, href=url)
        text = " ".join(_unescape(raw).split())
        return el("a", text, data_rst_anon="1") if anon else el("a", text, data_rst_ref=_norm(raw))

    def role(self, role: str, raw: str) -> list:
        name = role.lower()
        if name in self.roles:
            base, fmt = self.roles[name]
            if base == "raw" or fmt:
                return [el("span", _unescape(raw), class_="raw", data_format=fmt or "raw")]
            if not base:
                return [el("span", self.inline(raw), class_=name)]
            name = base
        bare = name.split(":", 1)[1] if ":" in name and name.split(":", 1)[0] in ("py", "c", "cpp", "js", "rst", "std", "math") else name
        text = _unescape(raw)
        if bare in ("emphasis", "title-reference", "title", "t", "dfn", "manpage", "mailheader", "mimetype", "newsgroup"):
            return [el("cite" if bare.startswith("t") else "em", text)]
        if bare in ("strong", "command", "program"):
            return [el("strong", text)]
        if bare in ("literal", "code"):
            return [el("code", raw if bare == "code" else text)]
        if bare in ("sub", "subscript"):
            return [el("sub", self.inline(raw))]
        if bare in ("sup", "superscript"):
            return [el("sup", self.inline(raw))]
        if bare == "math":
            return [mathml.from_tex(raw.replace("\\`", "`"))]
        if bare in ("kbd",):
            return [el("kbd", text)]
        if bare in ("abbr", "acronym", "ab", "ac"):
            m = re.match(r"^(.*?)\s*\((.*)\)$", text, re.DOTALL)
            return [el("abbr", m.group(1), title=m.group(2))] if m else [el("abbr", text)]
        if bare in ("pep", "pep-reference", "rfc", "rfc-reference"):
            num, _, frag = text.partition("#")
            num = num.strip()
            if not _digits(num):
                m = re.match(r"^(.*?)\s*<(\d+)(#[^>]*)?>$", text)
                if not m:
                    return [text]
                shown, num, frag = m.group(1), m.group(2), (m.group(3) or "#")[1:]
            else:
                shown = ""
            if bare.startswith("pep"):
                href, label = f"https://peps.python.org/pep-{int(num):04d}/", f"PEP {int(num)}"
            else:
                href, label = f"https://datatracker.ietf.org/doc/html/rfc{int(num)}.html", f"RFC {int(num)}"
            return [el("a", shown or label, href=href + (f"#{frag}" if frag else ""))]
        if bare in ("gh", "issue"):
            num = text.strip()
            return [el("a", f"gh-{num}" if bare == "gh" else f"bpo-{num}",
                       href=f"https://github.com/python/cpython/issues/{num}")] if num.isdigit() else [text]
        if bare in ("cve", "cwe"):
            num = text.strip()
            href = (f"https://www.cve.org/CVERecord?id=CVE-{num}" if bare == "cve"
                    else f"https://cwe.mitre.org/data/definitions/{num}.html")
            return [el("a", f"{bare.upper()}-{num}", href=href)]
        if bare == "source":
            m = re.match(r"^(.*?)\s*<([^<>]+)>$", text)
            shown, path = (m.group(1), m.group(2)) if m else (text, text)
            return [el("a", shown, href="https://github.com/python/cpython/tree/main/" + path.lstrip("/"))]
        explicit, target = _title_target(text)
        if bare == "ref" or bare == "numref":
            return [el("a", explicit or target, data_rst_label=_norm(target), data_rst_title="1" if explicit else "0")]
        if bare == "term":
            return [el("a", explicit or target, data_rst_term=target.lower())]
        if bare in ("doc", "download", "menuselection", "guilabel", "pypi"):
            shown = explicit or target
            if bare in ("menuselection", "guilabel"):
                shown = shown.replace("-->", "▸").replace("&&", "\0").replace("&", "").replace("\0", "&")
            return [el("em", shown) if bare in ("menuselection", "guilabel") else shown]
        if bare in ("file", "samp"):
            return [el("code", re.sub(r"(?<!\\)\{([^{}]*)\}", r"\1", text).replace("\\{", "{").replace("\\}", "}"))]
        if bare in _CODE_ROLES or ":" in name:
            shown = explicit
            if not shown:
                shown = target.lstrip("!")
                if shown.startswith("~"):
                    shown = re.split(r"[.:]", shown[1:])[-1]
                shown = shown.lstrip(".")
                if bare in ("func", "meth") and not shown.endswith(")"):
                    shown += "()"
            if bare not in _CODE_ROLES:
                self.drop("role:" + name, f"reStructuredText role ':{role}:' isn't known; its text was kept as code")
            return [el("code", shown)]
        self.drop("role:" + name, f"reStructuredText role ':{role}:' isn't supported; its text was kept")
        return [el("span", text, class_="raw", data_format="rst")] if name.startswith("raw") else [text]

    # --- after the whole file is read -------------------------------------------------------------
    def resolve(self, body: ET.Element) -> None:
        parents = {c: p for p in body.iter() for c in p}
        # substitutions (definitions can use others, a few rounds deep), within one budget for the
        # file: definitions that each use the next ten times multiplied the text every round
        budget = MAX_SUB_NODES
        sizes: dict[str, int] = {}
        for _ in range(MAX_SUB_ROUNDS):
            subs = [e for e in body.iter("span") if e.get("data-rst-sub") is not None]
            if not subs:
                break
            # each parent's children are rebuilt once a round (replace_el per use was quadratic in a
            # paragraph with many of them)
            by_parent: dict[ET.Element, dict[ET.Element, list]] = {}
            for e in subs:
                name = e.get("data-rst-sub")
                nodes = self.subs.get(name)
                if nodes is None:
                    nodes = next((v for k, v in self.subs.items() if k.lower() == name.lower()), None)
                p = parents.get(e)
                if p is None:
                    continue
                if nodes is not None:
                    if name not in sizes:
                        sizes[name] = sum(1 + len(x) // 64 if isinstance(x, str) else
                                          sum(1 + len(c.text or "") // 64 for c in x.iter()) for x in nodes)
                    budget -= sizes[name]
                    if budget < 0:
                        if budget + sizes[name] >= 0:  # said once
                            self.lost.append("Substitutions that grow without end were stopped; the rest are shown as |name|")
                        nodes = None
                if nodes is None:
                    by_parent.setdefault(p, {})[e] = [f"|{name}|"]
                    continue
                fresh = [copy.deepcopy(x) if not isinstance(x, str) else x for x in nodes]
                by_parent.setdefault(p, {})[e] = fresh
                for x in fresh:
                    if not isinstance(x, str):
                        parents[x] = p
                        for c in x.iter():
                            for cc in c:
                                parents[cc] = c
            for p, reps in by_parent.items():
                _splice(p, reps)
        left: dict[ET.Element, dict[ET.Element, list]] = {}
        for e in body.iter("span"):
            if e.get("data-rst-sub") is not None and e in parents:
                left.setdefault(parents[e], {})[e] = [f"|{e.get('data-rst-sub')}|"]
        for p, reps in left.items():
            _splice(p, reps)
        self._resolve_notes(body, parents)
        anon_list = []
        for k, t in enumerate(self.anon_targets, 1):
            if t[0] == "pending":
                t = self.targets.get(f"\0anon{k}")
            elif t[0] == "ref":
                t = self._lookup(t[1])
            anon_list.append(t)
        anon = iter(anon_list)
        for a in list(body.iter("a")):
            p = parents.get(a)
            if a.get("data-rst-anon") is not None:
                del a.attrib["data-rst-anon"]
                t = next(anon, None)
                self._point(a, t, p)
            elif a.get("data-rst-ref") is not None:
                name = a.attrib.pop("data-rst-ref")
                self._point(a, self._lookup(name), p)
            elif a.get("data-rst-label") is not None:
                name = a.attrib.pop("data-rst-label")
                explicit = a.attrib.pop("data-rst-title") == "1"
                t = self._lookup(name)
                if t and t[0] == "id" and not explicit and t[1] in self.headings:
                    a.text = " ".join(markup.text_of(self.headings[t[1]]).split())
                    for c in list(a):
                        a.remove(c)
                self._point(a, t, p)
            elif a.get("data-rst-term") is not None:
                t = self.targets.get("term:" + a.attrib.pop("data-rst-term"))
                self._point(a, t, p)
            elif a.get("data-rst-cite") is not None:
                cid = self.citations.get(a.attrib.pop("data-rst-cite"))
                self._point(a, ("id", cid) if cid else None, p)
        for div, depth, local in self.contents:
            self._fill_contents(body, div, depth, local)

    def _lookup(self, name: str):
        seen = 0
        t = self.targets.get(name)
        while t and t[0] == "ref" and seen < 10:
            t = self.targets.get(t[1])
            seen += 1
        return t if t and t[0] != "ref" else None

    def _point(self, a: ET.Element, t, parent) -> None:
        if t and t[0] == "url" and t[1]:
            a.set("href", t[1])
        elif t and t[0] == "id" and t[1]:
            a.set("href", "#" + t[1])
        elif parent is not None:
            unwrap(parent, a)

    def _resolve_notes(self, body: ET.Element, parents: dict) -> None:
        manual = {lab for lab, _ in self.footnotes if _digits(lab)}
        used = {int(x) for x in manual}
        by_label: dict[str, ET.Element] = {}
        auto: list[ET.Element] = []
        symbols: list[ET.Element] = []
        number: dict[int, str] = {}
        nxt = 1
        for k, (label, aside) in enumerate(self.footnotes):
            if _digits(label):
                shown = str(int(label))
            elif label.startswith("#"):
                while nxt in used:
                    nxt += 1
                used.add(nxt)
                shown = str(nxt)
                if len(label) > 1:
                    by_label["#" + _norm(label[1:])] = aside
                    self.targets.setdefault(_norm(label[1:]), ("id", f"fn{shown}"))
                else:
                    auto.append(aside)
            else:
                shown = _SYMBOLS[len(symbols) % len(_SYMBOLS)] * (len(symbols) // len(_SYMBOLS) + 1)
                symbols.append(aside)
            aside.set("id", f"fn{shown}" if shown.isdigit() else f"fn-s{len(symbols)}")
            aside.set("data-label", shown)
            number[k] = shown
            if _digits(label):
                by_label[str(int(label))] = aside
        auto_i = sym_i = 0
        for a in list(body.iter("a")):
            label = a.get("data-rst-fn")
            if label is None:
                continue
            del a.attrib["data-rst-fn"]
            if _digits(label):
                aside = by_label.get(str(int(label)))
            elif label == "#":
                aside = auto[auto_i] if auto_i < len(auto) else None
                auto_i += 1
            elif label.startswith("#"):
                aside = by_label.get("#" + _norm(label[1:]))
            else:
                aside = symbols[sym_i] if sym_i < len(symbols) else None
                sym_i += 1
            if aside is None:
                a.tag = "span"
                a.text = f"[{label}]"
                continue
            a.set("class", "noteref")
            a.set("epub:type", "noteref")
            a.set("href", "#" + aside.get("id"))
            a.append(el("sup", aside.get("data-label")))

    def _fill_contents(self, body: ET.Element, div: ET.Element, depth: int, local: bool) -> None:
        heads = []
        started = not local
        base_level = None
        for e in body:
            if e is div:
                started = True
                continue
            if started and e.tag in ("h1", "h2", "h3", "h4", "h5", "h6") and e.get("id"):
                level = int(e.tag[1])
                if local and base_level is not None and level <= base_level:
                    break
                heads.append((level, e))
            if local and not started and e.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                base_level = int(e.tag[1])
        if not heads:
            return
        top = min(lv for lv, _ in heads)
        root = el("ul")
        stack = [(top, root)]
        for level, h in heads:
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


def _escaped(text: str, k: int) -> bool:
    b = 0
    while k - b - 1 >= 0 and text[k - b - 1] == "\\":
        b += 1
    return b % 2 == 1


def _scan_cell(g: list[str], top: int, left: int):
    """The bottom-right corner of the grid-table cell whose top-left corner is (top, left)."""
    h, w = len(g), len(g[0])
    if g[top][left] != "+":
        return None
    for x in range(left + 1, w):
        ch = g[top][x]
        if ch == "+":
            if top + 1 < h and g[top + 1][x] in "|+":
                for y in range(top + 1, h):
                    c2 = g[y][x]
                    if c2 == "+":
                        if all(g[y][k] in "-=+" for k in range(left + 1, x)) and \
                                all(g[k][left] in "|+" for k in range(top + 1, y)):
                            return y, x
                    elif c2 != "|":
                        break
        elif ch not in "-=":
            return None
    return None


def _lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    return [s.expandtabs(8).rstrip().replace("\f", " ").replace("\v", " ") for s in text.split("\n")]


def _digits(s: str) -> bool:
    """Plain ASCII digits int() takes ("²" passes str.isdigit(), and failed the whole book in int())."""
    return s.isascii() and s.isdigit() and len(s) < 10


def _int(s: str, default):
    """A whole number written in an option or label (an optional sign, ASCII digits), else `default`."""
    s = (s or "").strip()
    return int(s) if re.fullmatch(r"[+-]?\d{1,9}", s, re.ASCII) else default


def _dedent(lines: list[str]) -> list[str]:
    widths = [_indent(s) for s in lines if s.strip()]
    k = min(widths) if widths else 0
    return [s[k:] for s in lines] if k else list(lines)


def _dedent_first(body: list[str]) -> list[str]:
    """A body whose first line is the text after a marker and the rest an indented block (already dedented)."""
    first, rest = body[0], body[1:]
    return [first.strip(), *rest] if first.strip() else rest


def _indented_after(lines: list[str], i: int) -> tuple[list[str], int]:
    n = len(lines)
    j = i
    while j < n and (not lines[j].strip() or lines[j][0] == " "):
        j += 1
    block = lines[i:j]
    while block and not block[-1].strip():
        block.pop()
    return _dedent(block), j


def _directive_parts(first: str, body: list[str], content_first: bool = False, max_args: int = 99):
    """(arguments text, {option: value}, content lines) of a directive block."""
    lines = list(body)
    k = 0
    args: list[str] = [first] if first.strip() else []
    if content_first:
        opts: dict[str, str] = {}
        while k < len(lines) and _OPT_LINE.match(lines[k]) and not args:
            m = _OPT_LINE.match(lines[k])
            opts[m.group(1).lower()] = (m.group(2) or "").strip()
            k += 1
        content = (args + lines[k:]) if args else lines[k:]
        while content and not content[0].strip():
            content.pop(0)
        return "", opts, content
    if max_args > 1:
        while k < len(lines) and lines[k].strip() and not _OPT_LINE.match(lines[k]):
            args.append(lines[k])
            k += 1
    opts = {}
    while k < len(lines) and lines[k].strip() and _OPT_LINE.match(lines[k]):
        m = _OPT_LINE.match(lines[k])
        name, value = m.group(1).lower(), (m.group(2) or "").strip()
        k += 1
        while k < len(lines) and lines[k].strip() and lines[k][0] == " ":
            value += " " + lines[k].strip()
            k += 1
        opts[name] = value
    content = lines[k:]
    while content and not content[0].strip():
        content.pop(0)
    while content and not content[-1].strip():
        content.pop()
    return "\n".join(s.strip() for s in args), opts, _dedent(content)


def _slice_lines(lines: list[str], opts: dict) -> list[str]:
    text_lines = lines
    if opts.get("start-line", "").lstrip("-").isdigit() or opts.get("end-line", "").lstrip("-").isdigit():
        a = _int(opts.get("start-line", ""), None)
        b = _int(opts.get("end-line", ""), None)
        text_lines = text_lines[a:b]
    for key, after in (("start-after", True), ("start-at", False)):
        if opts.get(key):
            for k, s in enumerate(text_lines):
                if opts[key] in s:
                    text_lines = text_lines[k + 1:] if after else text_lines[k:]
                    break
    for key, before in (("end-before", True), ("end-at", False)):
        if opts.get(key):
            for k, s in enumerate(text_lines):
                if opts[key] in s:
                    text_lines = text_lines[:k] if before else text_lines[:k + 1]
                    break
    return text_lines


def _title_target(text: str) -> tuple[str, str]:
    """("Title", "target") from "Title <target>", or ("", text)."""
    m = re.match(r"^(.*?)\s*<([^<>]+)>$", text, re.DOTALL)
    if m and m.group(1).strip():
        return " ".join(m.group(1).split()), m.group(2).strip()
    return "", " ".join(text.split())


def _unicode(spec: str) -> str:
    spec = spec.split(" .. ")[0]
    out = []
    for tok in spec.split():
        m = re.fullmatch(r"(?:0x|x|\\x|U\+|u|\\u|&#x)([0-9a-fA-F]+);?|&#(\d+);?|(\d+)", tok)
        try:
            if m and m.group(1):
                out.append(chr(int(m.group(1), 16)))
            elif m:
                out.append(chr(int(m.group(2) or m.group(3))))
            else:
                out.append(tok)
        except (ValueError, OverflowError):
            out.append(tok)
    return "".join(out)


def _only(expr: str) -> bool:
    """Sphinx ``only`` expressions, with the tags a book build has (html, epub) true."""
    tokens = re.findall(r"\(|\)|\w+", expr.lower())
    pos = 0

    def atom(d=0):
        nonlocal pos
        if pos >= len(tokens) or d > 50:
            return False
        t = tokens[pos]
        pos += 1
        if t == "not":
            return not atom(d + 1)
        if t == "(":
            v = disj(d + 1)
            pos += 1
            return v
        return t in _ONLY_TRUE

    def conj(d):
        nonlocal pos
        v = atom(d)
        while pos < len(tokens) and tokens[pos] == "and":
            pos += 1
            v = atom(d) and v
        return v

    def disj(d=0):
        nonlocal pos
        v = conj(d)
        while pos < len(tokens) and tokens[pos] == "or":
            pos += 1
            v = conj(d) or v
        return v

    return disj() if tokens else True


def to_body(text: str, base_dir=None, top=None, lost: list[str] | None = None) -> tuple[ET.Element, dict]:
    """reStructuredText to a ``<body>`` element; also returns the document's metadata fields."""
    p = _Rst(base_dir, top, lost if lost is not None else [])
    body = ET.Element("body")
    p.blocks(_lines(text), body, 0, sections=True)
    p.resolve(body)
    heads = [e for e in body if e.tag == "h1"]
    if "title" not in p.meta and len(heads) == 1:
        p.meta["title"] = " ".join(markup.text_of(heads[0]).split())
    return body, p.meta


def read(path) -> Book:
    path = Path(path)
    text = markup.decode(path.read_bytes())
    lost: list[str] = []
    body, meta = to_body(text, path.parent, path.parent.parent, lost)
    md = Metadata(title=meta.get("title", "") or path.stem.replace("_", " "))
    if meta.get("author") or meta.get("authors"):
        md.authors = [a.strip() for a in re.split(r"\s*[;,]\s*|\s+and\s+", meta.get("author") or meta["authors"]) if a.strip()]
    if meta.get("date"):
        md.date = meta["date"]
    return build_book(body, md.title, "rst", path.parent, path.parent.parent, lost, md)


# --- writing -----------------------------------------------------------------------------------

_O, _C = "\x02", "\x03"  # around inline markup until a block is put together (some languages need a space or \ there)
_BR = "\x01"  # a line break inside a paragraph
_ANCHORABLE = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "blockquote", "ul", "ol", "dl", "table", "figure", "div",
               "section", "hr", "nav", "article", "aside"}
_EAST_WIDE = ("W", "F")


def text_width(s: str) -> int:
    """Columns a line takes in a monospaced editor (wide East Asian characters take two)."""
    return sum(2 if unicodedata.east_asian_width(ch) in _EAST_WIDE else 0 if unicodedata.combining(ch) else 1 for ch in s)


class MarkupWriter(TextWriter):
    """What the rST, Org and AsciiDoc writers share: anchors for whole documents that are linked to, the
    set of anchors the text will have, and the markers around inline markup."""

    inline_anchors = False  # whether the language can put an anchor in the middle of text

    def __init__(self, book: Book):
        super().__init__(book)
        self.anchored: set[str] = set()
        self.heading_keys: set[str] = set()
        for href, _ in book.documents():
            stem = re.sub(r"[^\w-]", "_", href.rsplit("/", 1)[-1].rsplit(".", 1)[0])
            if href in self.targets:
                self.anchored.add(stem)
            for e in book.tree(href).iter():
                i = e.get("id")
                if i and f"{href}#{i}" in self.targets:
                    key = re.sub(r"[^\w-]", "_", f"{stem}-{i}")
                    if e.tag in _ANCHORABLE or (self.inline_anchors and e.tag not in markup.BLOCK):
                        self.anchored.add(key)
                    if e.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                        self.heading_keys.add(key)

    def header(self) -> str:
        return ""

    def inline(self, el_: ET.Element) -> str:
        link = el_.get("href", "") if el_.tag == "a" else ""
        if link and not is_external(link) and not link.startswith("#") and not is_noteref(el_) \
                and resolve(self.href, link) not in self.book.resources:
            # a link to a file outside the book stays a link to that file
            return self.link(link, self.inlines(el_).strip(), external=True)
        return super().inline(el_)

    def doc_anchor(self, key: str) -> str:
        return ""

    def finish(self, text: str) -> str:
        return text.replace(_BR, "\n").replace(_O, "").replace(_C, "")

    def write_book(self) -> str:
        parts = [self.header(), self.preamble()]
        for href, _ in self.book.documents():
            self.href = href
            body = self.book.tree(href).find("body")
            if body is None:
                continue
            text = self.blocks(body)
            if href in self.targets:
                stem = re.sub(r"[^\w-]", "_", href.rsplit("/", 1)[-1].rsplit(".", 1)[0])
                text = self.doc_anchor(stem) + text
            if text.strip():
                parts.append(text)
        parts.append(self.postamble())
        return self.finish(self.join_document([p for p in parts if p and p.strip()]))


_INLINE_SPECIAL = re.compile(r"([\\*`|])")
_LINE_START = re.compile(r"^(?:[-*+•‣⁃](?=\s|$)|\d+[.)](?=\s|$)|#\.(?=\s|$)|\(\w+\)(?=\s|$)"
                         r"|[a-zA-Z][.)](?=\s)|\.\.(?=\s|$)|:\w|>>>|__ )")
_MARKUP_TAGS = {"em", "i", "cite", "dfn", "var", "strong", "b", "del", "s", "strike", "u", "ins", "sup", "sub", "a",
                "code", "kbd", "samp", "tt"}


class RstWriter(MarkupWriter):
    def __init__(self, book: Book):
        super().__init__(book)
        self._nest = 0
        self._deferred: list[str] = []
        self.note_bodies: list[tuple[str, str]] = []
        self.subst: list[tuple[str, str, str, str]] = []  # name, src, alt, target

    # inline markup can't nest in reStructuredText: inside one, the rest is plain text
    def inline(self, el_: ET.Element) -> str:
        tag = el_.tag if isinstance(el_.tag, str) else ""
        if tag == "a" and not (el_.text or "").strip() and len(el_) == 1 and el_[0].tag == "img" \
                and not (el_[0].tail or "").strip() and el_.get("href") and not self._nest:
            name = self.image_name(el_[0])
            link = el_.get("href", "")
            target = link if is_external(link) else ""
            return self._subst_image(name, el_[0].get("alt", ""), target) if name else ""
        if tag not in _MARKUP_TAGS:
            return super().inline(el_)
        if self._nest:
            if tag == "a" and ("noteref" in el_.get("class", "") or el_.get("epub:type") == "noteref"):
                self._deferred.append(self.note_call(el_))
                return ""
            return self.text(markup.text_of(el_)) if tag in ("code", "kbd", "samp", "tt") else self.inlines(el_)
        self._nest += 1
        try:
            out = super().inline(el_)
        finally:
            self._nest -= 1
        if self._deferred:
            out += "".join(self._deferred)
            self._deferred = []
        return out

    def escape(self, s: str) -> str:
        s = _INLINE_SPECIAL.sub(r"\\\1", s)
        return re.sub(r"(?<=[\w\]])_(?=\W|$)", r"\\_", s)

    def _markers(self, text: str) -> str:
        """Markers around inline markup become nothing, or an escaped space where a word touches them."""
        if _O not in text and _C not in text:
            return text
        out = []
        n = len(text)
        for k, ch in enumerate(text):
            if ch == _O:
                prev = next((text[j] for j in range(k - 1, -1, -1) if text[j] not in (_O, _C)), " ")
                if not (prev.isspace() or prev in _START_PRE or unicodedata.category(prev) in ("Ps", "Pi", "Pd", "Po")):
                    out.append("\\ ")
            elif ch == _C:
                nxt = next((text[j] for j in range(k + 1, n) if text[j] not in (_O, _C)), " ")
                if not (nxt.isspace() or nxt in _END_POST or unicodedata.category(nxt) in ("Pe", "Pf", "Pd", "Po", "Pi")):
                    out.append("\\ ")
            else:
                out.append(ch)
        return "".join(out)

    def _wrap(self, start: str, s: str, end: str) -> str:
        return f"{_O}{start}{s}{end}{_C}"

    def _anchor(self, anchor: str | None) -> str:
        return f".. _{anchor}:\n\n" if anchor else ""

    def doc_anchor(self, key: str) -> str:
        return f".. _{key}:\n\n"

    def _subst_defs(self) -> str:
        if not self.subst:
            return ""
        out = []
        for name, src, alt, target in self.subst:
            d = f".. |{name}| image:: {src}"
            if alt:
                d += f"\n   :alt: {alt}"
            if target:
                d += f"\n   :target: {target}"
            out.append(d)
        self.subst = []
        return "\n\n" + "\n".join(out)

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        text = self._markers(text.strip())
        if _BR in text:
            lines = [s.strip() for s in text.split(_BR)]
            return self._anchor(anchor) + "\n".join("| " + s if s else "|" for s in lines) + self._subst_defs()
        text = re.sub(r"\s*\n\s*", " ", text)
        if _LINE_START.match(text) or (len(text) >= 4 and _ADORN.match(text)) or text.startswith("| ") or text == "|":
            text = "\\" + text
        if text.endswith("::"):
            text = text[:-1] + "\\:"
        return self._anchor(anchor) + text + self._subst_defs()

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        text = self._markers(text).replace(_BR, " ").replace("\n", " ").strip() or "\\ "
        char = "=-~^\"'`"[min(level, 7) - 1]
        return self._anchor(anchor) + f"{text}\n{char * max(text_width(text), 4)}" + self._subst_defs()

    def block(self, el_: ET.Element) -> str:
        cls = el_.get("class", "").split()
        anchor = self.anchor_key(el_)
        prefix = self._anchor(anchor) if anchor and el_.tag not in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "figure") \
            else ""
        if el_.tag == "div" and "admonition" in cls:
            kind = next((c for c in cls if c in _ADMONITIONS and c not in ("impl-detail", "seealso", "todo")), None)
            title = el_.find("p")
            inner = el("div")
            inner.text = el_.text
            for c in el_:
                if not (c is title and "admonition-title" in c.get("class", "")):
                    inner.append(copy.copy(c))
            body = self.blocks(inner) or "\\ "
            if kind:
                return prefix + f".. {kind}::\n\n" + _indent_text(body, 3)
            head = self._markers(self.inlines(title)).strip() if title is not None else "Note"
            head = head.replace("\n", " ")
            return prefix + f".. admonition:: {head}\n\n" + _indent_text(body, 3)
        if el_.tag == "div" and "line-block" in cls:
            return prefix + self.paragraph(self.inlines(el_).replace(" ", " "))
        return prefix + super().block(el_)

    def rule(self) -> str:
        return "----------"

    def code_block(self, code: str, lang: str) -> str:
        head = f".. code:: {lang}" if lang else "::"
        return head + "\n\n" + _indent_text(code or " ", 3)

    def raw_block(self, text: str, fmt: str) -> str:
        if fmt == "rst":
            return text
        if fmt in ("html", "latex", "tex"):
            return f".. raw:: {'latex' if fmt == 'tex' else fmt}\n\n" + _indent_text(text, 3)
        return self.code_block(text, "")

    def quote(self, inner: str) -> str:
        return "\x04" + _indent_text(inner, 4)

    def join_blocks(self, parts: list[str]) -> str:
        out: list[str] = []
        for p in parts:
            p = p.strip("\n")
            if not p.strip():
                continue
            if p.startswith("\x04"):
                p = p[1:]
                if out:
                    out.append("..")  # an empty comment, so the quote doesn't join what comes before
            out.append(p)
        return "\n\n".join(out)

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        out = []
        for k, item in enumerate(items):
            marker = f"{start + k}." if ordered else "-"
            body = self._markers(item).lstrip("\x04").strip("\n") or "\\ "
            out.append(marker + " " + _indent_text(body, len(marker) + 1).lstrip(" "))
        spaced = any("\n" in o for o in out)
        return ("\n\n" if spaced else "\n").join(out)

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        out = []
        for term, defs in pairs:
            term = self._markers(term).replace(_BR, " ").replace("\n", " ").strip() or "\\ "
            if _LINE_START.match(term):
                term = "\\" + term
            body = "\n\n".join(self._markers(d).lstrip("\x04") for d in defs if d.strip()) or "\\ "
            out.append(term + "\n" + _indent_text(body, 4))
        return "\n\n".join(out)

    def table(self, table: Table) -> str:
        if not table.rows:
            return ""
        text = _grid(table, lambda s: self._markers(s).replace(_BR, "\n").replace("\x04", ""))
        if table.caption:
            return f".. table:: {self._markers(table.caption)}\n\n" + _indent_text(text, 3)
        return text

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        if not src:
            return self.paragraph(caption, anchor)
        out = self._anchor(anchor) + f".. figure:: {src}"
        if alt:
            out += f"\n   :alt: {' '.join(alt.split())}"
        if caption:
            out += "\n\n" + _indent_text(self._markers(caption).replace(_BR, " "), 3)
        return out

    def math_block(self, el_: ET.Element) -> str:
        return ".. math::\n\n" + _indent_text(mathml.to_tex(el_), 3)

    def math_inline(self, el_: ET.Element) -> str:
        return self._wrap(":math:`", mathml.to_tex(el_).replace("`", "\\`"), "`")

    def footnote(self, body: str, note_id: str) -> str:
        label = f"n{len(self.note_bodies) + 1}"
        self.note_bodies.append((label, body))
        return f"{_O}[#{label}]_{_C}"

    def postamble(self) -> str:
        notes = "\n\n".join(f".. [#{label}] " + _indent_text(self._markers(b) or "\\ ", 3).lstrip(" ")
                            for label, b in self.note_bodies)
        return (notes + self._subst_defs()).strip("\n")

    def line_break(self) -> str:
        return _BR

    def emph(self, s: str) -> str:
        return self._wrap("*", s, "*")

    def strong(self, s: str) -> str:
        return self._wrap("**", s, "**")

    def sup(self, s: str) -> str:
        return self._wrap(":sup:`", s, "`")

    def sub(self, s: str) -> str:
        return self._wrap(":sub:`", s, "`")

    def code(self, s: str) -> str:
        s = " ".join(s.split("\n"))
        if not s.strip():
            return s
        if "``" in s or s.startswith(("`", " ")) or s.endswith(("`", " ")):
            return self._wrap(":literal:`", s.replace("\\", "\\\\").replace("`", "\\`"), "`")
        return self._wrap("``", s, "``")

    def raw_inline(self, text: str, fmt: str) -> str:
        return text if fmt == "rst" else self.code(text)

    def link(self, target: str, text: str, external: bool) -> str:
        text = text.strip()
        if external:
            if target.startswith("mailto:") and text in ("", target[7:]):
                return target[7:]
            if not text or text == target:
                return f"{_O}{target}{_C}" if re.match(r"^[a-z]+://", target) else self._wrap("`", f"<{target}>", "`__")
            return self._wrap("`", f"{text} <{target}>", "`__")
        if target not in self.anchored:
            return text
        return self._wrap("`", f"{text or target} <{target}_>", "`__")

    def _subst_image(self, src: str, alt: str, target: str = "") -> str:
        name = f"img{len(self.used_images)}_{len(self.subst) + 1}"
        self.subst.append((name, src, " ".join(alt.split()), target))
        return f"{_O}|{name}|{_C}"

    def image(self, src: str, alt: str) -> str:
        return self._subst_image(src, alt) if src else alt


def _indent_text(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + s if s.strip() else "" for s in text.split("\n"))


def _grid(table: Table, finish) -> str:
    """A grid table, spans kept."""
    width = table.width
    grid: list[list[tuple[int, int] | None]] = []
    cells: list[tuple[int, int, int, int, str]] = []  # row, col, rowspan, colspan, text
    for r, row in enumerate(table.rows):
        while len(grid) <= r:
            grid.append([None] * width)
        c = 0
        for cell in row:
            while c < width and grid[r][c] is not None:
                c += 1
            if c >= width:
                break
            cs = min(cell.colspan, width - c)
            rs = max(1, min(cell.rowspan, len(table.rows) - r))
            idx = len(cells)
            cells.append((r, c, rs, cs, finish(cell.text)))
            for rr in range(r, r + rs):
                while len(grid) <= rr:
                    grid.append([None] * width)
                for cc in range(c, c + cs):
                    grid[rr][cc] = (idx, rr - r)
            c += cs
    nrows = len(grid)
    for r in range(nrows):
        for c in range(width):
            if grid[r][c] is None:
                cells.append((r, c, 1, 1, ""))
                grid[r][c] = (len(cells) - 1, 0)
    lines_of = [t[4].split("\n") if t[4] else [""] for t in cells]
    colw = [3] * width
    for (_r, c, _rs, cs, _t), lines in zip(cells, lines_of):
        if cs == 1:
            colw[c] = max(colw[c], max(text_width(s) for s in lines) + 2)
    for (_r, c, _rs, cs, _t), lines in zip(cells, lines_of):
        need = max(text_width(s) for s in lines) + 2 - (sum(colw[c:c + cs]) + cs - 1)
        if cs > 1 and need > 0:
            colw[c + cs - 1] += need
    rowh = [1] * nrows
    for (r, _c, rs, _cs, _t), lines in zip(cells, lines_of):
        if rs == 1:
            rowh[r] = max(rowh[r], len(lines))
    for (r, _c, rs, _cs, _t), lines in zip(cells, lines_of):
        need = len(lines) - (sum(rowh[r:r + rs]) + rs - 1)
        if rs > 1 and need > 0:
            rowh[r + rs - 1] += need

    def border(r: int, char: str) -> str:
        """The line above row r (r == nrows: the bottom one)."""
        inner = 0 < r < nrows
        out = ["|" if inner and grid[r][0][1] > 0 else "+"]
        for c in range(width):
            spanning = inner and grid[r][c][1] > 0
            out.append(" " * colw[c] if spanning else char * colw[c])
            if c + 1 == width:
                out.append("|" if spanning else "+")
            elif inner:
                a, b, a_up, b_up = grid[r][c][0], grid[r][c + 1][0], grid[r - 1][c][0], grid[r - 1][c + 1][0]
                if a == b and a_up == b_up:
                    out.append(" " if a == a_up else char)
                elif a == a_up and b == b_up:
                    out.append("|")
                else:
                    out.append("+")
            else:
                row = grid[0] if r == 0 else grid[nrows - 1]
                out.append(char if row[c][0] == row[c + 1][0] else "+")
        return "".join(out)

    out_lines = [border(0, "-")]
    for r in range(nrows):
        for k in range(rowh[r]):
            line = ["|"]
            c = 0
            while c < width:
                idx, _off = grid[r][c]
                r0, _c0, _rs, cs, _t = cells[idx]
                w = sum(colw[c:c + cs]) + cs - 1
                line_no = sum(rowh[r0 + j] + 1 for j in range(r - r0)) + k
                lines = lines_of[idx]
                text = lines[line_no] if line_no < len(lines) else ""
                line.append(" " + text + " " * (w - 1 - text_width(text)) + "|")
                c += cs
            out_lines.append("".join(line))
        head = table.header_rows and r + 1 == table.header_rows and r + 1 < nrows
        out_lines.append(border(r + 1, "=" if head else "-"))
    return "\n".join(out_lines)


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    return write_text(RstWriter(book), path, pictures)
