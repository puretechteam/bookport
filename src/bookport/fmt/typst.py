"""Typst: a reader for Typst markup and a writer.

Reading covers the markup (headings, emphasis, raw text and blocks, lists, enums, term lists,
math, labels and references, line breaks, escapes, comments) and the common element functions
(link, image, figure, table and grid, footnote, quote, heading, emph/strong/strike/..., raw,
lorem, cite, ref, bibliography, include). It does not run Typst's scripting language: #set, #show,
#import and functions it doesn't know stay as raw Typst (and are listed in ``book.lost``); simple
``#let name = value`` variables are substituted.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, Metadata
from ..limits import MAX_DEPTH, safe_path
from ..textwriter import Table, TextWriter, write_text

MAX_INCLUDE = 8
MAX_EXPANSION = 2_000_000  # characters substituted from #let variables and includes, per book

_LOREM_TEXT = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et "
               "dolore magnam aliquam quaerat voluptatem. Ut enim aeque doleamus animo, cum corpore dolemus, fieri tamen "
               "permagna accessio potest, si aliquod aeternum et infinitum impendere malum nobis opinemur. Quod idem licet "
               "transferre in voluptatem, ut postea variari voluptas distinguique possit, augeri amplificarique non "
               "possit. At etiam Athenis, ut e patre audiebam facete et urbane Stoicos irridente, statua est in quo a "
               "nobis philosophia defensa et collaudata est, cum id, quod maxime placeat, facere possimus, omnis voluptas "
               "assumenda est, omnis dolor repellendus.")
_LOREM = _LOREM_TEXT.split()

_IDENT = re.compile(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*")
_LABEL = re.compile(r"<([\w:.-]+)>")
_URL = re.compile(r"https?://[^\s<>\[\]()\"']*[^\s<>\[\]()\"'.,;:!?*_]")
# DOTALL: an item's logical line runs on when a bracket opened on it does (`- a #[` ... `]`)
_LIST = re.compile(r"^( *)(-|\+|\d+\.)(?: +|$)(.*)$", re.DOTALL)
_TERM = re.compile(r"^( *)/ +([^:\n]*):[ \t]*(.*)$", re.DOTALL)
_HEADING = re.compile(r"^ *(=+)(?: +|$)(.*)$")
_RAW_FENCE = re.compile(r"^ *(`{3,})([\w+-]*)(.*)$")
_INLINE_STYLE = {"emph": "em", "strong": "strong", "strike": "del", "underline": "u", "overline": "span",
                 "super": "sup", "sub": "sub", "highlight": "mark", "smallcaps": "span"}
_PASS = {"align", "block", "box", "pad", "text", "par", "columns", "rect", "square", "circle", "ellipse", "place",
         "move", "scale", "rotate", "stack", "skew", "repeat", "hide", "layout", "measure", "context", "show",
         "set", "page", "colbreak", "upper", "lower", "lang", "metadata", "html.elem", "html.div", "html.span"}


def _match(src: str, i: int) -> int:
    """The index just after the bracket that closes the one at ``src[i]`` (or the end): strings
    and comments in code, escapes, raw text and math in markup are skipped, not counted."""
    closes = {"(": ")", "[": "]", "{": "}"}
    if src[i] not in closes:
        return i + 1
    stack = [src[i]]
    j, n = i + 1, len(src)
    while j < n and stack:
        ch = src[j]
        code = stack[-1] in "({"
        if code and ch == '"':
            j += 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
        elif ch == "/" and src.startswith("//", j) and (j == 0 or src[j - 1] != ":"):
            j = src.find("\n", j)
            j = n if j < 0 else j - 1
        elif ch == "/" and src.startswith("/*", j):
            k = src.find("*/", j + 2)
            j = n if k < 0 else k + 1
        elif not code and ch == "\\":
            j += 1
        elif not code and ch == "`":
            m = re.match(r"`+", src[j:])
            k = src.find(m.group(0), j + len(m.group(0)))
            j = n if k < 0 else k + len(m.group(0)) - 1
        elif not code and ch == "$":
            k = j + 1
            while k < n and src[k] != "$":
                k += 2 if src[k] == "\\" else 1
            j = k
        elif ch in closes:
            stack.append(ch)
            if len(stack) > MAX_DEPTH * 4:
                return n
        elif ch == closes[stack[-1]]:
            stack.pop()
        j += 1
    return min(j, n)


def _expr_end(src: str, i: int) -> int:
    """After the code expression starting at ``src[i] == "#"``: a name, then its (args) and [content]s."""
    j = i + 1
    if j < len(src) and src[j] in "([{":
        return _match(src, j)
    m = _IDENT.match(src, j)
    if not m:
        return j
    j = m.end()
    while j < len(src) and src[j] in "([":
        j = _match(src, j)
    return j


def _split_args(text: str) -> list[str]:
    """Top-level comma-separated parts of an argument list (without its parentheses)."""
    parts, depth, cur, j, n = [], 0, [], 0, len(text)
    while j < n:
        ch = text[j]
        if ch == '"':
            k = j + 1
            while k < n and text[k] != '"':
                k += 2 if text[k] == "\\" else 1
            cur.append(text[j:k + 1])
            j = k + 1
            continue
        if ch in "([{":
            k = _match(text, j)
            cur.append(text[j:k])
            j = k
            continue
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        j += 1
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


class _Reader:
    def __init__(self, book: Book, base: Path | None, top: Path | None):
        self.book = book
        self.base = base
        self.top = top
        self.vars: dict[str, str] = {}
        self.labels: set[str] = set()
        self.notes: list[str] = []
        self.lost: dict[str, str] = {}
        self.depth = 0
        self.budget = MAX_EXPANSION
        self.includes: list[Path] = []
        self.title = ""
        self.authors: list[str] = []
        self.bib_files: list[Path] = []

    def lose(self, kind: str, message: str) -> None:
        self.lost.setdefault(kind, message)

    # --- blocks ------------------------------------------------------------------------------
    def blocks(self, src: str) -> str:
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                return "<p>" + html.escape(src) + "</p>"
            return self._blocks(src)
        finally:
            self.depth -= 1

    def _blocks(self, src: str) -> str:
        out: list[str] = []
        para: list[str] = []
        i, n = 0, len(src)

        def end_para():
            text = "".join(para).strip()
            para.clear()
            if not text:
                return
            inner = self.inline(text)
            if not inner.strip():
                return
            m = re.fullmatch(r"\s*(<math [^>]*display=\"block\".*</math>)\s*", inner, re.DOTALL)
            out.append(f"<p>{m.group(1) if m else inner.strip()}</p>")

        while i < n:
            eol = src.find("\n", i)
            eol = n if eol < 0 else eol
            line = src[i:eol]
            stripped = line.strip()
            if not stripped:
                end_para()
                i = eol + 1
                continue
            m = _HEADING.match(line)
            if m and not stripped.startswith("=>"):
                end_para()
                j = self._line_end(src, i)
                text = src[i:j].lstrip()[len(m.group(1)):].strip()
                label = ""
                lm = re.search(r"\s*<([\w:.-]+)>\s*$", text)
                if lm:
                    label, text = lm.group(1), text[:lm.start()]
                level = min(len(m.group(1)), 6)
                idattr = f' id="{html.escape(label)}"' if label else ""
                out.append(f"<h{level}{idattr}>{self.inline(text)}</h{level}>")
                i = j + 1
                continue
            m = _RAW_FENCE.match(line)
            if m:
                end_para()
                fence = m.group(1)
                close = src.find(fence, i + len(line) - len(line.lstrip()) + len(fence))
                body = src[eol + 1:close if close >= 0 else n]
                if close >= 0 and close < eol:  # ```lang text``` on one line
                    body = src[i:close].lstrip()[len(fence) + len(m.group(2)):]
                lang = f' class="language-{html.escape(m.group(2))}"' if m.group(2) else ""
                out.append(f"<pre><code{lang}>{html.escape(_dedent(body).strip(chr(10)).rstrip())}</code></pre>")
                if close < 0:
                    i = n
                else:
                    nl = src.find("\n", close)
                    i = n if nl < 0 else nl + 1
                continue
            if _LIST.match(line) or _TERM.match(line):
                end_para()
                j = self._list(src, i, out)
                if j > i:
                    i = j
                    continue
                # not a list after all: read on as a paragraph line, so the loop always moves on
            if stripped.startswith("#"):
                j = len(line) - len(line.lstrip()) + i
                statement = re.match(r"#(set|show|import|let|include)\b", src[j:j + 9])
                if statement and para and statement.group(1) != "include":
                    k = self._statement_end(src, j)
                    para.append(src[i:k] + "\n")
                    i = k + 1
                    continue
                k = self._statement_end(src, j) if statement else _expr_end(src, j)
                nl = src.find("\n", k)
                rest = src[k:nl if nl >= 0 else n]
                block = self.block_code(src[j:k], rest)
                if block is not None:
                    end_para()
                    out.append(block)
                    i = n if nl < 0 else nl + 1
                    if rest.strip() and not _LABEL.fullmatch(rest.strip()):
                        para.append(rest + "\n")
                    continue
            j = self._line_end(src, i)
            para.append(src[i:j] + "\n")
            i = j + 1
        end_para()
        return "\n".join(out)

    def _statement_end(self, src: str, i: int) -> int:
        """The end of a #set/#show/#let/#import/#include statement: its line, with brackets run on."""
        n = len(src)
        j = i + 1
        while j < n and src[j] != "\n":
            ch = src[j]
            if ch in "([{":
                j = _match(src, j)
                continue
            if ch == '"':
                k = j + 1
                while k < n and src[k] not in '"\n':
                    k += 2 if src[k] == "\\" else 1
                j = k + 1
                continue
            if ch == "/" and src.startswith("//", j):
                break
            j += 1
        return min(j, n)

    def _line_end(self, src: str, i: int) -> int:
        """The end of the logical line from ``i``: a bracket, raw span or math opened on it may run on."""
        n = len(src)
        j = i
        while j < n and src[j] != "\n":
            ch = src[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "#":
                j = max(_expr_end(src, j), j + 1)
                continue
            if ch == "$":
                k = j + 1
                while k < n and src[k] != "$":
                    k += 2 if src[k] == "\\" else 1
                j = k + 1
                continue
            if ch == "`":
                m = re.match(r"`+", src[j:])
                k = src.find(m.group(0), j + len(m.group(0)))
                j = j + len(m.group(0)) if k < 0 else k + len(m.group(0))
                continue
            if ch == "/" and src.startswith("/*", j):
                k = src.find("*/", j + 2)
                j = n if k < 0 else k + 2
                continue
            if ch == "/" and src.startswith("//", j) and (j == 0 or src[j - 1] != ":"):
                k = src.find("\n", j)
                return n if k < 0 else k
            j += 1
        return min(j, n)

    def _list(self, src: str, i: int, out: list[str]) -> int:
        """A run of list, enum or term items from ``i``; nested by indentation."""
        n = len(src)
        items: list[tuple[str, str, str, str]] = []  # (kind, number or term, body)
        first = _LIST.match(src[i:src.find("\n", i) if src.find("\n", i) >= 0 else n]) or \
            _TERM.match(src[i:src.find("\n", i) if src.find("\n", i) >= 0 else n])
        indent = len(first.group(1))
        while i < n:
            eol = self._line_end(src, i)
            line = src[i:eol]
            m = _LIST.match(line)
            t = _TERM.match(line) if not m else None
            if not (m or t) or len((m or t).group(1)) != indent:
                break
            if m:
                marker, body = m.group(2), [m.group(3)]
                kind = "ul" if marker == "-" else "ol"
                extra = marker[:-1] if marker[0].isdigit() else ""
            else:
                kind, extra, body = "dl", t.group(2), [t.group(3)]
            i = eol + 1
            # continuation: lines indented deeper (blank lines between them allowed)
            while i < n:
                eol = self._line_end(src, i)
                line = src[i:eol]
                if not line.strip():
                    k = eol + 1
                    nxt = src[k:src.find("\n", k) if src.find("\n", k) >= 0 else n]
                    if nxt.strip() and len(nxt) - len(nxt.lstrip()) > indent:
                        body.append("")
                        i = k
                        continue
                    break
                if len(line) - len(line.lstrip()) > indent:
                    body.append(line[indent + 2:] if line[:indent + 2].strip() == "" else line.lstrip())
                    i = eol + 1
                    continue
                break
            items.append((kind, extra, "\n".join(body)))
            if i < n and not src[i:src.find("\n", i) if src.find("\n", i) >= 0 else n].strip():
                # a blank line ends the list unless another item of it follows
                k = src.find("\n", i)
                k = n if k < 0 else k + 1
                nxt = src[k:src.find("\n", k) if src.find("\n", k) >= 0 else n]
                mm = _LIST.match(nxt) or _TERM.match(nxt)
                if mm and len(mm.group(1)) == indent:
                    i = k
                    continue
                break
        k = 0
        while k < len(items):
            kind = items[k][0]
            group = []
            while k < len(items) and items[k][0] == kind:
                group.append(items[k])
                k += 1
            if kind == "dl":
                out.append("<dl>" + "".join(f"<dt>{self.inline(term)}</dt><dd>{self.item(body)}</dd>"
                                            for _, term, body in group) + "</dl>")
            else:
                start = group[0][1]
                attr = f' start="{start}"' if kind == "ol" and start and start != "1" else ""
                out.append(f"<{kind}{attr}>" + "".join(f"<li>{self.item(body)}</li>" for _, _, body in group) + f"</{kind}>")
        return i

    def item(self, body: str) -> str:
        inner = self.blocks(body)
        m = re.fullmatch(r"<p>(.*)</p>", inner, re.DOTALL)
        return m.group(1) if m and "<p>" not in m.group(1) else inner

    def statement(self, expr: str, block: bool) -> str:
        """#set, #show, #import, #let or #include: kept as raw Typst (a #let of a plain value is used)."""
        kind = expr[1:].split(None, 1)[0].split("(")[0]
        if kind == "include":
            return self.include(expr) if block else ""
        if kind == "let" and self.let(expr):
            return ""
        if kind == "set":
            self.set_rule(expr)
        if kind == "let":
            self.lose("let", "Typst #let definitions that compute something were kept as text, not run.")
        else:
            self.lose(kind, f"Typst #{kind} rules were kept as text, not applied.")
        tag = "pre" if block else "span"
        return f'<{tag} class="raw" data-format="typst">{html.escape(expr.strip())}</{tag}>'

    def block_code(self, expr: str, rest: str) -> str | None:
        """A #expression standing at the start of a line: its block, or None when it's inline."""
        if re.match(r"#(set|show|import|let|include)\b", expr):
            return self.statement(expr, True)
        name = (_IDENT.match(expr, 1) or [""])[0]
        if name in ("figure", "table", "grid", "quote", "heading", "image", "outline", "bibliography", "pagebreak",
                    "list", "enum", "terms", "line", "v", "align", "block", "columns", "raw", "pad", "par", "place",
                    "math.equation", "rect", "parbreak", "colbreak", "stack"):
            label = _LABEL.match(rest.strip())
            got = self.call(expr, block=True)
            if label and got.startswith("<") and not got.startswith("<pre"):
                got = re.sub(r"^<(\w+)", lambda mm: f'<{mm.group(1)} id="{html.escape(label.group(1))}"', got, count=1)
            return got if got.strip() else ""
        return None

    def let(self, expr: str) -> bool:
        """Takes in a #let of a plain value (text, content, number); False for anything else."""
        m = re.match(r"#let\s+([A-Za-z_][\w-]*)\s*=\s*(.*)$", expr.strip(), re.DOTALL)
        if not m:
            return False
        value = m.group(2).strip()
        if re.fullmatch(r'"(?:[^"\\]|\\.)*"', value):
            self.vars[m.group(1)] = html.escape(_unquote(value))
        elif value.startswith("[") and _match(value, 0) == len(value):
            self.vars[m.group(1)] = self.inline(value[1:-1])
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            self.vars[m.group(1)] = value
        else:
            return False
        return True

    def set_rule(self, expr: str) -> None:
        m = re.match(r"#set\s+document\((.*)\)", expr.strip(), re.DOTALL)
        if not m:
            return
        for part in _split_args(m.group(1)):
            k, _, v = part.partition(":")
            v = v.strip()
            if k.strip() == "title":
                self.title = _unquote(v) if v.startswith('"') else _strip_tags(self.inline(v[1:-1])) if v.startswith("[") else ""
            elif k.strip() == "author":
                self.authors = [_unquote(x) for x in re.findall(r'"(?:[^"\\]|\\.)*"', v)]

    def include(self, expr: str) -> str:
        m = re.search(r'"((?:[^"\\]|\\.)*)"', expr)
        if not m or self.base is None:
            return ""
        name = m.group(1)
        p = safe_path(self.base, name, self.top)
        if p is None or not p.is_file():
            self.lose(f"include:{name}", f"An include outside the document's folder (or missing) was left out: {name}")
            return ""
        if p.resolve() in self.includes or len(self.includes) >= MAX_INCLUDE:
            self.lose("include-loop", f"An include that includes itself (or nests too deep) was left out: {name}")
            return ""
        try:
            text = markup.decode(p.read_bytes())
        except OSError:
            return ""
        self.budget -= len(text)
        if self.budget < 0:
            self.lose("include-size", "Includes past the size limit were left out.")
            return ""
        self.includes.append(p.resolve())
        try:
            return self.blocks(text.replace("\r\n", "\n"))
        finally:
            self.includes.pop()

    # --- calls ---------------------------------------------------------------------------------
    def call(self, expr: str, block: bool = False) -> str:
        """HTML for a #expression (``expr`` starts with "#")."""
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                return html.escape(expr)
            return self._call(expr, block)
        finally:
            self.depth -= 1

    def _call(self, expr: str, block: bool) -> str:
        body = expr.removeprefix("#")
        if body.startswith("[") and body.endswith("]"):
            return self.blocks(body[1:-1]) if block else self.inline(body[1:-1])
        m = _IDENT.match(body)
        if not m:
            return self.raw(expr)
        name = m.group(0)
        rest = body[m.end():]
        if not rest:
            if name in self.vars:
                self.budget -= len(self.vars[name])
                return self.vars[name] if self.budget > 0 else ""
            if name in ("linebreak", "parbreak"):
                return "<br/>"
            return self.raw(expr)
        args: list[str] = []
        content: list[str] = []
        j = 0
        tail = ""
        while j < len(rest):
            if rest[j] not in "([":  # the call ends here (`#f(x) + 1`, `#name rest`): the rest is text
                tail = html.escape(rest[j:])
                break
            k = _match(rest, j)
            part = rest[j:k]
            if part.startswith("("):
                args += _split_args(part[1:-1])
            else:
                content.append(part[1:-1])
            j = k
        pos = [a for a in args if not re.match(r"[A-Za-z_][\w-]*\s*:(?!:)", a)]
        named = {a.split(":", 1)[0].strip(): a.split(":", 1)[1].strip() for a in args if a not in pos}
        values = pos + [f"[{c}]" for c in content]
        return self.element(name, values, named, expr, block) + tail

    def value(self, v: str, block: bool = False) -> str:
        """HTML for an argument value: a string, [content], $math$, a nested call, a number."""
        v = v.strip()
        if v.startswith('"'):
            return html.escape(_unquote(v))
        if v.startswith("[") and v.endswith("]"):
            return self.blocks(v[1:-1]) if block else self.inline(v[1:-1])
        if v.startswith("$") and v.endswith("$") and len(v) > 1:
            return self.math(v[1:-1])
        if _IDENT.match(v):
            return self.call("#" + v, block)
        return html.escape(v)

    def element(self, name: str, values: list[str], named: dict[str, str], expr: str, block: bool) -> str:
        first = values[0] if values else ""
        if name in _INLINE_STYLE:
            tag = _INLINE_STYLE[name]
            cls = ' class="smallcaps"' if name == "smallcaps" else ""
            return f"<{tag}{cls}>{self.value(first)}</{tag}>"
        if name == "link":
            dest = first.strip()
            label = _LABEL.fullmatch(dest)
            href = "#" + label.group(1) if label else _unquote(dest) if dest.startswith('"') else ""
            text = self.value(values[1]) if len(values) > 1 else html.escape(href.lstrip("#"))
            return f'<a href="{html.escape(href)}">{text}</a>' if href else text
        if name == "footnote":
            n = len(self.notes) + 1
            self.notes.append(f'<aside epub:type="footnote" id="fn{n}">{self.blocks(_content(first))}</aside>')
            return f'<a class="noteref" epub:type="noteref" href="#fn{n}"><sup>{n}</sup></a>'
        if name == "image":
            src = _unquote(first) if first.startswith('"') else ""
            alt = _unquote(named.get("alt", '""')) if named.get("alt", "").startswith('"') else ""
            img = f'<img src="{html.escape(src)}" alt="{html.escape(alt)}"/>'
            return f"<p>{img}</p>" if block else img
        if name == "figure":
            inner = self.value(first, block=True) if first else ""
            inner = re.sub(r"^<p>(<img[^>]*/>)</p>$", r"\1", inner.strip())
            cap = named.get("caption", "")
            caption = f"<figcaption>{self.value(cap)}</figcaption>" if cap else ""
            return f"<figure>{inner}{caption}</figure>"
        if name in ("table", "grid"):
            return self.table(values, named)
        if name == "heading":
            level = named.get("level") or named.get("depth") or "1"
            level = min(max(int(level), 1), 6) if level.isdigit() else 1
            return f"<h{level}>{self.value(first)}</h{level}>"
        if name == "quote":
            inner = self.value(first, block=True)
            if named.get("block") == "true" or block:
                att = named.get("attribution")
                att_html = f"<p>— {self.value(att)}</p>" if att else ""
                return f"<blockquote>{inner if inner.startswith('<') else f'<p>{inner}</p>'}{att_html}</blockquote>"
            return f"“{self.value(first)}”"
        if name == "raw":
            text = _unquote(first) if first.startswith('"') else first
            lang = _unquote(named["lang"]) if named.get("lang", "").startswith('"') else ""
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            if named.get("block") == "true" or block:
                return f"<pre><code{cls}>{html.escape(text)}</code></pre>"
            return f"<code>{html.escape(text)}</code>"
        if name in ("list", "enum", "terms"):
            tag = {"list": "ul", "enum": "ol", "terms": "dl"}[name]
            if name == "terms":
                return "<dl>" + "".join(self._term(v) for v in values) + "</dl>"
            return f"<{tag}>" + "".join(f"<li>{self.value(_item_of(v))}</li>" for v in values) + f"</{tag}>"
        if name == "lorem":
            k = int(first) if first.isdigit() else 0
            k = min(k, 100_000)
            words = [_LOREM[i % len(_LOREM)] for i in range(k)]
            return html.escape(" ".join(words))
        if name == "cite":
            keys = [m.group(1) for m in _LABEL.finditer(" ".join(values))]
            return self.citation(keys)
        if name == "ref":
            label = _LABEL.fullmatch(first.strip())
            return f'<a href="#{html.escape(label.group(1))}">{html.escape(label.group(1))}</a>' if label else ""
        if name == "bibliography":
            names = [_unquote(v) for v in values if v.startswith('"')]
            if values and values[0].startswith("("):
                names += [_unquote(x) for x in _split_args(values[0][1:-1]) if x.startswith('"')]
            title = named.get("title", "")
            title = "Bibliography" if not title else "" if title == "none" else self.value(title)
            return self.references(names, title)
        if name == "outline":
            self.lose("outline", "A Typst outline (table of contents) was left out: the book has its own.")
            return ""
        if name in ("pagebreak", "v", "h", "colbreak", "line"):
            return "<hr/>" if name == "line" and block else (" " if name == "h" else "")
        if name == "linebreak":
            return "<br/>"
        if name == "math.equation":
            return self.value(first)
        if name in _PASS or name.split(".")[0] in ("text", "par", "page", "html"):
            if not values:
                return ""
            last = values[-1]
            return self.value(last, block=block) if last.startswith("[") or _IDENT.match(last.strip()) else ""
        return self.raw(expr)

    def _term(self, v: str) -> str:
        m = re.match(r"terms\.item\(\s*(\[.*\])\s*,\s*(\[.*\])\s*\)$", v.strip(), re.DOTALL)
        if m:
            return f"<dt>{self.value(m.group(1))}</dt><dd>{self.value(m.group(2))}</dd>"
        return f"<dt>{self.value(v)}</dt><dd></dd>"

    def table(self, values: list[str], named: dict[str, str]) -> str:
        cols = named.get("columns", "1").strip()
        if cols.isdigit():
            width = max(1, min(int(cols), 1000))
        elif cols.startswith("("):
            width = max(1, len(_split_args(cols[1:-1])))
        else:
            width = 1
        cells: list[tuple[str, str, int, int]] = []  # (tag, html, colspan, rowspan)
        for v in values:
            v = v.strip()
            if v.startswith("..") or not v:
                self.lose("spread", "Typst table cells made by code (..spread) were kept as text.")
                cells.append(("td", self.raw(v), 1, 1))
                continue
            head = re.match(r"(?:table|grid)\.header\((.*)\)$", v, re.DOTALL)
            if head:
                cells += [("th", *self._cell(x)) for x in _split_args(head.group(1)) if not re.match(r"\w+\s*:", x)]
                continue
            if re.match(r"(?:table|grid)\.(?:hline|vline|footer)\b", v):
                continue
            cells.append(("td", *self._cell(v)))
        rows: list[list[str]] = []
        cur: list[str] = []
        used = 0
        for tag, inner, span, rspan in cells:
            attrs = (f' colspan="{span}"' if span > 1 else "") + (f' rowspan="{rspan}"' if rspan > 1 else "")
            cur.append(f"<{tag}{attrs}>{inner}</{tag}>")
            used += span
            if used >= width:
                rows.append(cur)
                cur, used = [], 0
        if cur:
            rows.append(cur)
        return "<table>" + "".join(f"<tr>{''.join(r)}</tr>" for r in rows) + "</table>"

    def _cell(self, v: str) -> tuple[str, int, int]:
        m = re.match(r"(?:table|grid)\.cell\((.*?)\)\s*(\[.*\])$", v, re.DOTALL)
        if m:
            named = dict(re.findall(r"(colspan|rowspan)\s*:\s*(\d+)", m.group(1)))
            return self.value(m.group(2)), int(named.get("colspan", 1)), int(named.get("rowspan", 1))
        return self.value(v), 1, 1

    def citation(self, keys: list[str]) -> str:
        if not keys:
            return ""
        shown = html.escape("; ".join(keys))
        return f'<span class="citation" data-cites="{html.escape(" ".join(keys))}">[{shown}]</span>'

    def references(self, names: list[str], title: str) -> str:
        """Where the reference list goes; the .bib files (in the document's folder only) are read at the end."""
        for name in names:
            p = safe_path(self.base, name, self.top) if self.base is not None else None
            if p is None or not p.is_file():
                self.lose(f"bib:{name}", f"A bibliography file outside the document's folder (or missing) was left out: {name}")
                continue
            self.bib_files.append(p)
        heading = f"<h1>{title}</h1>" if title else ""
        return heading + '<div class="references" data-bib="here"></div>'

    def raw(self, expr: str) -> str:
        name = (_IDENT.match(expr, 1) or [expr[:20]])[0]
        self.lose(f"fn:{name}", f"Typst code that bookport doesn't run was kept as text: #{name}")
        return f'<span class="raw" data-format="typst">{html.escape(expr)}</span>'

    def math(self, src: str) -> str:
        display = len(src) > 1 and src[0].isspace() and src[-1].isspace()
        return markup.to_string(mathml.from_typst(src, display))

    # --- inline --------------------------------------------------------------------------------
    def inline(self, text: str) -> str:
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                return html.escape(text)
            return self._inline(text)
        finally:
            self.depth -= 1

    def _inline(self, text: str) -> str:
        out: list[str] = []
        open_: list[str] = []  # "strong" / "em" open now
        quote = {"\"": False, "'": False}
        i, n = 0, len(text)

        def toggle(tag):
            if tag in open_:
                while open_:
                    t = open_.pop()
                    out.append(f"</{t}>")
                    if t == tag:
                        break
            else:
                open_.append(tag)
                out.append(f"<{tag}>")

        while i < n:
            ch = text[i]
            if ch == "\\":
                nxt = text[i + 1] if i + 1 < n else ""
                if nxt in ("", "\n", " ", "\t"):
                    out.append("<br/>")
                    i += 2
                    continue
                m = re.match(r"\\u\{([0-9A-Fa-f]{1,6})\}", text[i:])
                if m:
                    cp = int(m.group(1), 16)
                    out.append(html.escape(chr(cp)) if cp <= 0x10FFFF and not 0xD800 <= cp < 0xE000 else "")
                    i += m.end()
                    continue
                out.append(html.escape(nxt))
                i += 2
                continue
            if ch == "/" and text.startswith("//", i) and (i == 0 or text[i - 1] != ":"):
                k = text.find("\n", i)
                i = n if k < 0 else k
                continue
            if ch == "/" and text.startswith("/*", i):
                k = text.find("*/", i + 2)
                i = n if k < 0 else k + 2
                continue
            if ch == "`":
                m = re.match(r"`+", text[i:])
                ticks = m.group(0)
                k = text.find(ticks, i + len(ticks))
                if k < 0:
                    out.append(html.escape(ticks))
                    i += len(ticks)
                    continue
                body = text[i + len(ticks):k]
                if len(ticks) >= 3:
                    lm = re.match(r"([\w+-]*)\s", body)
                    lang = lm.group(1) if lm else ""
                    body = body[lm.end():] if lm else body
                    cls = f' class="language-{html.escape(lang)}"' if lang else ""
                    out.append(f"<code{cls}>{html.escape(body.strip())}</code>")
                else:
                    out.append(f"<code>{html.escape(body)}</code>")
                i = k + len(ticks)
                continue
            if ch == "$":
                k = i + 1
                while k < n and text[k] != "$":
                    k += 2 if text[k] == "\\" else 1
                out.append(self.math(text[i + 1:k]))
                i = k + 1
                continue
            if ch == "#" and re.match(r"#(set|show|import|let|include)\b", text[i:i + 9]):
                k = self._statement_end(text, i)
                out.append(self.statement(text[i:k], False))
                i = k
                continue
            if ch == "#" and i + 1 < n and (text[i + 1] in "([{" or text[i + 1].isalpha() or text[i + 1] == "_"):
                k = _expr_end(text, i)
                out.append(self.call(text[i:k]))
                i = k
                if i < n and text[i] == ";":
                    i += 1
                continue
            if ch == "*" and (open_ and open_[-1] == "strong" or "strong" not in open_):
                toggle("strong")
                i += 1
                continue
            if ch == "_":
                before = text[i - 1] if i else " "
                after = text[i + 1] if i + 1 < n else " "
                if "em" in open_ and not after.isalnum() or "em" not in open_ and not before.isalnum():
                    toggle("em")
                    i += 1
                    continue
            if ch == "<":
                m = _LABEL.match(text, i)
                if m:
                    out.append(f'<span id="{html.escape(m.group(1))}"></span>')
                    i = m.end()
                    continue
            if ch == "@":
                m = re.match(r"@([\w:.-]*[\w])", text[i:])
                if m and (i == 0 or not text[i - 1].isalnum()):
                    key = m.group(1)
                    sup = re.match(r"\[([^\]]*)\]", text[i + m.end():])
                    if key in self.labels:
                        shown = self.inline(sup.group(1)) if sup else html.escape(key)
                        out.append(f'<a href="#{html.escape(key)}">{shown}</a>')
                    else:
                        out.append(self.citation([key]))
                    i += m.end() + (sup.end() if sup else 0)
                    continue
            if ch in "hf" and _URL.match(text, i) and (i == 0 or not text[i - 1].isalnum()):
                m = _URL.match(text, i)
                url = m.group(0)
                out.append(f'<a href="{html.escape(url)}">{html.escape(url)}</a>')
                i = m.end()
                continue
            if ch == "~":
                out.append("\u00a0")
                i += 1
                continue
            if ch == "-" and text.startswith("---", i):
                out.append("—")
                i += 3
                continue
            if ch == "-" and text.startswith("--", i):
                out.append("–")
                i += 2
                continue
            if ch == "-" and text.startswith("-?", i):
                out.append("\u00ad")
                i += 2
                continue
            if ch == "." and text.startswith("...", i):
                out.append("…")
                i += 3
                continue
            if ch in quote:
                before = text[i - 1] if i else " "
                if ch == "'" and before.isalnum():
                    out.append("’")
                else:
                    out.append(("”" if ch == '"' else "’") if quote[ch] else ("“" if ch == '"' else "‘"))
                    quote[ch] = not quote[ch]
                i += 1
                continue
            if ch == "\n":
                out.append(" ")
                i += 1
                continue
            out.append(html.escape(ch))
            i += 1
        while open_:
            out.append(f"</{open_.pop()}>")
        return "".join(out)


def _content(v: str) -> str:
    v = v.strip()
    return v[1:-1] if v.startswith("[") and v.endswith("]") else _unquote(v) if v.startswith('"') else v


def _item_of(v: str) -> str:
    m = re.match(r"(?:list|enum)\.item\(.*?\)\s*(\[.*\])$", v.strip(), re.DOTALL)
    return m.group(1) if m else v


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1]
    return re.sub(r"\\(u\{[0-9A-Fa-f]{1,6}\}|.)", lambda m: _unescape(m.group(1)), s)


def _unescape(e: str) -> str:
    if e.startswith("u{"):
        cp = int(e[2:-1], 16)
        return chr(cp) if cp <= 0x10FFFF and not 0xD800 <= cp < 0xE000 else ""
    return {"n": "\n", "t": "\t", "r": "\r"}.get(e, e)


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def _dedent(s: str) -> str:
    lines = s.split("\n")
    indents = [len(x) - len(x.lstrip()) for x in lines if x.strip()]
    cut = min(indents, default=0)
    return "\n".join(x[cut:] for x in lines)


def read(path) -> Book:
    p = Path(path)
    text = markup.decode(p.read_bytes()).replace("\r\n", "\n").lstrip("\ufeff")
    return book_from_typst(text, p.stem.replace("_", " "), base_dir=p.parent, source=p)


def book_from_typst(text: str, title: str = "", base_dir: Path | None = None, top: Path | None = None,
                    source: Path | None = None) -> Book:
    from .markdown import _pull_images
    book = Book(Metadata(title=title))
    book.source_format = "typ"
    book.metadata.language = "en"
    r = _Reader(book, base_dir, top if top is not None else (base_dir.parent if base_dir is not None else None))
    r.labels = set(_LABEL.findall(text))
    if source is not None:
        r.includes.append(Path(source).resolve())
    body = r.blocks(text)
    if r.notes:
        body += "\n" + "\n".join(r.notes)
    root = markup.parse(f"<html><head><title>{html.escape(r.title or title)}</title></head><body>{body}</body></html>")
    href = "text/part0001.xhtml"
    _pull_images(book, root, href, base_dir, {}, top=r.top)
    book.add_document(href, root)
    if r.title:
        book.metadata.title = r.title
    else:
        first = root.find(".//h1")
        if first is not None and markup.text_of(first).strip():
            book.metadata.title = " ".join(markup.text_of(first).split())
    if r.authors:
        book.metadata.authors = r.authors
    book.lost += list(r.lost.values())
    if r.bib_files:
        from .. import bib
        bib.apply(book, bib.load(r.bib_files), title=False)
    book.ensure_toc()
    return book


# --- writing -----------------------------------------------------------------------------------

_SPECIAL = re.compile(r"([\\*_`$#<@\[\]~])|(/(?=[/*]))|(-(?=-))|(\.(?=\.\.))|('|\")")
_LINE_START = re.compile(r"^(\s*)(=+|-|\+|/ |\d+\.)(?=\s|$)", re.MULTILINE)


class _TypstWriter(TextWriter):
    def __init__(self, book: Book):
        super().__init__(book)
        # the labels that will exist: links to anything else are written as plain text
        self.keys: set[str] = set()
        for target in self.targets:
            if "#" in target:
                doc, frag = target.split("#", 1)
                self.keys.add(re.sub(r"[^\w-]", "_", f"{doc.rsplit('/', 1)[-1].rsplit('.', 1)[0]}-{frag}"))

    def escape(self, s: str) -> str:
        return _SPECIAL.sub(lambda m: "\\" + m.group(0), s)

    def preamble(self) -> str:
        md = self.book.metadata
        for href, _ in self.book.documents():
            for pre in self.book.tree(href).iter("pre"):
                if pre.get("data-format") == "typst" and markup.text_of(pre).lstrip().startswith("#set document"):
                    return ""  # read from Typst: its own #set document comes back as it was
        args = []
        if md.title:
            args.append(f"title: {_quote(md.title)}")
        if md.authors:
            args.append("author: (" + ", ".join(_quote(a) for a in md.authors) + ("," if len(md.authors) == 1 else "") + ")")
        return f"#set document({', '.join(args)})" if args else ""

    def _start(self, text: str) -> str:
        return _LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2), text)

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        text = self._start(re.sub(r"\n(?!\\)", "\n", text.strip()))
        return text + (f" <{anchor}>" if anchor and anchor in self.keys else "")

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        return "=" * level + " " + text + (f" <{anchor}>" if anchor and anchor in self.keys else "")

    def rule(self) -> str:
        return "#line(length: 100%)"

    def code_block(self, code: str, lang: str) -> str:
        fence = "```"
        while fence in code:
            fence += "`"
        return f"{fence}{lang}\n{code}\n{fence}"

    def raw_block(self, text: str, fmt: str) -> str:
        return text if fmt == "typst" else self.code_block(text, "")

    def quote(self, inner: str) -> str:
        return "#quote(block: true)[\n" + inner + "\n]"

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        out = []
        for k, item in enumerate(items):
            marker = f"{start}." if ordered and k == 0 and start != 1 else "+" if ordered else "-"
            lines = item.split("\n")
            pad = " " * (len(marker) + 1)
            out.append(f"{marker} " + lines[0] + "".join("\n" + (pad + x if x else "") for x in lines[1:]))
        return "\n".join(out)

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        out = []
        for term, defs in pairs:
            body = "\n\n".join(defs)
            lines = body.split("\n")
            out.append(f"/ {term.replace(':', chr(92) + ':')}: " + lines[0] + "".join("\n  " + x if x else "\n" for x in lines[1:]))
        return "\n".join(out)

    def table(self, table: Table) -> str:
        cells = []
        for r, row in enumerate(table.rows):
            row_cells = []
            for c in row:
                body = "[" + c.text.replace("\n\n", " \\ ") + "]"
                if c.colspan > 1 or c.rowspan > 1:
                    spans = ", ".join(x for x in (f"colspan: {c.colspan}" if c.colspan > 1 else "",
                                                  f"rowspan: {c.rowspan}" if c.rowspan > 1 else "") if x)
                    body = f"table.cell({spans}){body}"
                row_cells.append(body)
            if r < table.header_rows:
                cells.append("table.header(" + ", ".join(row_cells) + ")")
            else:
                cells += row_cells
        tbl = f"table(\n  columns: {max(1, table.width)},\n  " + ",\n  ".join(cells) + ",\n)"
        if table.caption:
            return f"#figure(\n  {tbl},\n  caption: [{table.caption}],\n)"
        return "#" + tbl

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        img = f"image({_quote(src)}" + (f", alt: {_quote(alt)}" if alt else "") + ")"
        out = f"#figure(\n  {img},\n" + (f"  caption: [{caption}],\n" if caption else "") + ")"
        return out + (f" <{anchor}>" if anchor and anchor in self.keys else "")

    def math_block(self, el: ET.Element) -> str:
        return "$ " + mathml.to_typst(el) + " $"

    def math_inline(self, el: ET.Element) -> str:
        return "$" + mathml.to_typst(el).strip() + "$"

    def footnote(self, body: str, note_id: str) -> str:
        return f"#footnote[{body.strip()}]"

    def citation(self, keys: list[str], shown: str) -> str:
        good = [k for k in keys if re.fullmatch(r"[\w:.-]*\w", k)]
        return " ".join("@" + k for k in good) if good else shown

    def line_break(self) -> str:
        return " \\\n"

    def inline_anchor(self, key: str) -> str:
        return f"#box[] <{key}>" if key in self.keys else ""

    def emph(self, s: str) -> str:
        return f"#emph[{s}]"

    def strong(self, s: str) -> str:
        return f"#strong[{s}]"

    def strike(self, s: str) -> str:
        return f"#strike[{s}]"

    def underline(self, s: str) -> str:
        return f"#underline[{s}]"

    def sup(self, s: str) -> str:
        return f"#super[{s}]"

    def sub(self, s: str) -> str:
        return f"#sub[{s}]"

    def smallcaps(self, s: str) -> str:
        return f"#smallcaps[{s}]"

    def code(self, s: str) -> str:
        s = " ".join(s.split("\n"))
        if "`" in s:
            return f"#raw({_quote(s)})"
        return f"`{s}`"

    def raw_inline(self, text: str, fmt: str) -> str:
        return text if fmt == "typst" else self.code(text)

    def link(self, target: str, text: str, external: bool) -> str:
        if external:
            if text == self.escape(target) or not text:
                return f"#link({_quote(target)})"
            return f"#link({_quote(target)})[{text}]"
        if target not in self.keys:
            return text
        return f"#link(<{target}>)[{text}]"

    def image(self, src: str, alt: str) -> str:
        if not src:
            return self.escape(alt)
        return f"#box(image({_quote(src)}" + (f", alt: {_quote(alt)}" if alt else "") + "))"


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    return write_text(_TypstWriter(book), path, pictures)
