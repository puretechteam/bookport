"""Markdown: a CommonMark-style reader (with GitHub tables, strikethrough and Pandoc's ``$``-TeX math)
and a writer.

From Pandoc's Markdown the reader also takes footnotes (``[^id]`` with ``[^id]:`` definitions, and
inline ``^[...]``), definition lists, simple and grid tables, fenced divs (``::: class``) and
``H~2~O`` / ``x^2^``; the writer writes notes as ``[^n]``."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, Metadata, is_external, relative, resolve
from ..limits import safe_path
from ..textwriter import is_note, is_noteref

# --- reading -----------------------------------------------------------------------------------

_HR = re.compile(r"^ {0,3}([-*_])(\s*\1){2,}\s*$")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*(\{[^{}`]*\}|[^`\s]*)[^`]*$")
_BULLET = re.compile(r"^( {0,3})([-*+])( +|\t|$)(.*)$")
_ORDERED = re.compile(r"^( {0,3})(\(?(?:\d{1,9}|#|[ivxlcdm]{1,8}|[IVXLCDM]{1,8}|[a-zA-Z]))([.)])( +|\t|$)(.*)$")
_ROMAN = re.compile(r"^m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$")


def _ordered(line: str) -> re.Match | None:
    """A numbered list item, with Pandoc's fancy markers: 1. 1) (1) #. a. A) i. (iv) ...; "B. Russell"
    (a capital letter and a period) needs two spaces after it to be one, as in Pandoc."""
    m = _ORDERED.match(line)
    if m is None:
        return None
    mark, delim = m.group(2).lstrip("("), m.group(3)
    if m.group(2).startswith("(") and delim != ")":
        return None
    if mark.isalpha() and len(mark) > 1 and not _ROMAN.match(mark.lower()):
        return None
    if len(mark) == 1 and mark.isupper() and delim == "." and len(m.group(4)) < 2 and m.group(5):
        return None
    return m


def _marker_kind(mark: str, first: bool = True) -> tuple[str, int]:
    """(list type: "1", "a", "A", "i", "I"; the number) for a list marker."""
    mark = mark.lstrip("(")
    if mark.isdigit():
        return "1", int(mark)
    if mark == "#":
        return "1", 1
    if (len(mark) > 1 or mark in "iI") and _ROMAN.match(mark.lower()):
        values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
        total, prev = 0, 0
        for ch in reversed(mark.lower()):
            v = values[ch]
            total += -v if v < prev else v
            prev = max(prev, v)
        return ("i" if mark.islower() else "I"), total
    return ("a" if mark.islower() else "A"), ord(mark.lower()) - 96


def _same_style(m: re.Match, kind: str, style: tuple) -> bool:
    """Does this item go on the list (same delimiter, same kind of number)? #. fits any; in a lettered
    list, i and v are letters."""
    if (m.group(2)[:1] == "(", m.group(3)) != style:
        return False
    mark = m.group(2).lstrip("(")
    if mark == "#":
        return True
    if kind in ("a", "A") and len(mark) == 1:
        return mark.isupper() == (kind == "A") and mark.isalpha()
    if kind in ("i", "I") and mark.isalpha():  # in a roman list, v and x are numbers too
        return mark.isupper() == (kind == "I") and bool(_ROMAN.match(mark.lower()))
    return _marker_kind(mark)[0] == kind


_QUOTE = re.compile(r"^ {0,3}> ?(.*)$")
_REFDEF = re.compile(r"""^ {0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?(?:\s+(?:"([^"]*)"|'([^']*)'|\(([^)]*)\)))?\s*$""")
_HTML_BLOCK = re.compile(r"^ {0,3}<(/?)(address|article|aside|blockquote|body|center|details|dialog|dd|div|dl|dt|"
                         r"fieldset|figcaption|figure|footer|form|h[1-6]|header|hr|html|li|main|nav|ol|p|pre|section|"
                         r"table|tbody|td|tfoot|th|thead|tr|ul|!--|script|style)(\s|/?>|$)", re.IGNORECASE)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
# Pandoc's Markdown
_DIV_OPEN = re.compile(r"^ {0,3}:{3,}\s*(\{[^{}\n]*\}|[\w-]+)\s*:*\s*$")
_DIV_CLOSE = re.compile(r"^ {0,3}:{3,}\s*$")
_TAGS_LINE = re.compile(r"^\s*(?:<[^<>]+>\s*)+$")
_LINE_BLOCK = re.compile(r"^\|(?: (.*)|$)")
_DEF_ITEM = re.compile(r"^( {0,3}[:~](?: +|\t))(\S.*)$")
_DASHES = re.compile(r"^\s*-+(?:\s+-+)*\s*$")
_COLUMNS = re.compile(r"^\s*-+(?:\s+-+)+\s*$")  # two or more runs of dashes: a table's columns
_ONE_DASH_RUN = re.compile(r"^\s*-{3,}\s*$")
_GRID_RULE = re.compile(r"^\s*\+[-=:]+(?:\+[-=:]+)*\+\s*$")
_FOOTNOTE_DEF = re.compile(r"^ {0,3}\[\^([^\]\s]{1,100})\]:[ \t]?(.*)$")
_FOOTNOTE_REF = re.compile(r"\[\^([^\]\s]{1,100})\]")
_INLINE_NOTE = re.compile(r"\^\[((?:[^\[\]\n]|\[[^\[\]\n]*\]){1,2000})\]")


def _footnote_defs(text: str) -> tuple[str, dict[str, list[str]]]:
    """Pandoc footnote definitions (``[^id]: text``, more paragraphs indented by four spaces) taken out of
    the text: (the rest, {id: their lines})."""
    defs: dict[str, list[str]] = {}
    out: list[str] = []
    lines = text.split("\n")
    i, n = 0, len(lines)
    fence = None
    while i < n:
        line = lines[i]
        fm = _FENCE.match(line)
        if fm and (fence is None or line.strip().startswith(fence)):
            fence = None if fence else fm.group(2)[:3]
        m = None if fence else _FOOTNOTE_DEF.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        body = [m.group(2)]
        i += 1
        while i < n:
            nxt = lines[i]
            if not nxt.strip():
                if i + 1 < n and lines[i + 1].startswith(("    ", "\t")):
                    body.append("")
                    i += 1
                    continue
                break
            if _FOOTNOTE_DEF.match(nxt):
                break
            body.append(nxt[4:] if nxt.startswith("    ") else nxt.removeprefix("\t"))
            i += 1
        defs.setdefault(m.group(1), body)
    return "\n".join(out), defs


_SPAN = re.compile(r"\[([^\[\]]{0,1000})\]\{([^{}\n]{0,300})\}")
# <scheme:...> autolinks, for the URI schemes in common use (<text:p> stays an XML tag)
_AUTOLINK = re.compile(r"<((?:https?|ftps?|mailto|file|info|urn|tel|data|irc|ircs|news|nntp|ssh|sftp|git|svn|xmpp|"
                       r"magnet|doi|geo|sips?|wss?|ldap|telnet|gopher|feed|spotify|slack|zoommtg|skype):[^<>\s]+)>",
                       re.IGNORECASE)
_TEX_INLINE = re.compile(r"\\[A-Za-z]{2,30}\*?(?:\[[^\[\]\n]{0,200}\])?(?:\{[^{}\n]{0,500}\}){1,4}")
_TEX_BEGIN = re.compile(r"^\\begin\{([A-Za-z*]{1,30})\}")
_SMART_PIECES = re.compile("(<[^<>]*>|\x00\\d+\x00)")
_OPENS = r"(^|[\s(\[{/\N{EM DASH}\N{EN DASH}\N{LEFT DOUBLE QUOTATION MARK}\N{LEFT SINGLE QUOTATION MARK}\x00-])"


def _code_span(code: str, attrs: str | None) -> str:
    raw = re.fullmatch(r"\{\s*=([\w-]+)\s*\}", attrs or "")
    if raw:
        fmt = raw.group(1).lower()
        return code if fmt == "html" else f'<span class="raw" data-format="{html.escape(fmt)}">{html.escape(code)}</span>'
    return "<code>" + html.escape(code.strip()) + "</code>"


def _span(inner: str, spec: str, keep) -> str:
    attrs = _div_attrs("{" + spec + "}")
    classes = re.findall(r"\.([\w-]+)", spec)
    if "underline" in classes or "ul" in classes:
        return keep("<u>") + inner + keep("</u>")
    if "mark" in classes:
        return keep("<mark>") + inner + keep("</mark>")
    return keep(f"<span{attrs}>") + inner + keep("</span>")


def _smart(text: str) -> str:
    """Pandoc's typography on reading: curly quotes and apostrophes, -- and --- dashes, ... (never inside
    a tag; a tag between a quote and its word doesn't change which way the quote turns)."""
    tags: list[str] = []

    def hide(m: re.Match) -> str:
        tags.append(m.group(0))
        return f"\x01{len(tags) - 1}\x01"

    text = re.sub(r"<[^<>]*>", hide, text)
    text = text.replace("---", "\N{EM DASH}").replace("--", "\N{EN DASH}").replace("...", "\N{HORIZONTAL ELLIPSIS}")
    if '"' in text:
        text = re.sub(_OPENS + '"(?=\\S)', "\\1\N{LEFT DOUBLE QUOTATION MARK}", text)
        text = text.replace('"', "\N{RIGHT DOUBLE QUOTATION MARK}")
    if "'" in text:
        text = re.sub(r"(?<=[^\W_])'(?=[^\W_])", "\N{RIGHT SINGLE QUOTATION MARK}", text)
        text = re.sub(_OPENS + "'(?=\\S)", "\\1\N{LEFT SINGLE QUOTATION MARK}", text)
        text = text.replace("'", "\N{RIGHT SINGLE QUOTATION MARK}")
    return re.sub("\x01(\\d+)\x01", lambda m: tags[int(m.group(1))], text)


def _heading_attrs(text: str) -> tuple[str, str]:
    """``Title {#id .class}`` -> ("Title", ' id="id" class="class"')."""
    m = re.search(r"\s*\{([^{}]*)\}\s*$", text)
    if not m or not re.fullmatch(r"\s*(?:[#.][\w:.-]+|-|[\w-]+=(?:\"[^\"]*\"|\S+))(?:\s+(?:[#.][\w:.-]+|-|[\w-]+=(?:\"[^\"]*\"|\S+)))*\s*",
                                  m.group(1)):
        return text, ""
    return text[:m.start()], _div_attrs("{" + m.group(1) + "}")


def _div_attrs(spec: str) -> str:
    """``::: name`` or ``::: {#id .class key=value}`` as HTML attributes."""
    if not spec.startswith("{"):
        return f' class="{html.escape(spec)}"'
    classes = re.findall(r"\.([\w-]+)", spec)
    ident = re.search(r"#([\w-]+)", spec)
    out = f' id="{html.escape(ident.group(1))}"' if ident else ""
    return out + (f' class="{html.escape(" ".join(classes))}"' if classes else "")


class _Notes:
    """Pandoc footnotes across a book's chapters: the definitions, and the numbers given out so far."""

    def __init__(self, defs: dict[str, list[str]] | None = None):
        self.defs = defs or {}
        self.ids: dict[str, int] = {}  # footnote label -> its number
        self.count = 0
        self.where: dict[int, str] = {}  # note number -> the chapter file it is written in
        self.current = ""  # the chapter file being read


class _Parser:
    def __init__(self, text: str, notes: _Notes | None = None):
        self.refs: dict[str, tuple[str, str]] = {}
        self.depth = 0
        if notes is None:
            text, defs = _footnote_defs(text)
            notes = _Notes(defs)
        self.notes = notes
        self.asides: list[str] = []  # the notes first called in this text, written after it
        lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ").split("\n")
        self.lines = self._collect_refs(lines)

    def _collect_refs(self, lines):
        out = []
        in_fence = False
        for line in lines:
            if _FENCE.match(line):
                in_fence = not in_fence
            m = None if in_fence else _REFDEF.match(line)
            if m:
                title = m.group(3) or m.group(4) or m.group(5) or ""
                self.refs.setdefault(m.group(1).strip().lower(), (m.group(2), title))
            else:
                out.append(line)
        return out

    def html(self) -> str:
        body = self.blocks(self.lines)
        return body + "".join(self.asides)

    # --- blocks -------------------------------------------------------------------------------
    def blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        i, n = 0, len(lines)
        para: list[str] = []

        def end_para():
            if para:
                inner = self.inline("\n".join(s.strip() for s in para))
                # an image alone in its paragraph is a figure, its alt text the caption (Pandoc's implicit_figures)
                fig = re.fullmatch(r'\s*<img [^>]*\balt="([^"]+)"[^>]*/>\s*', inner)
                out.append(f"<figure>{inner.strip()}<figcaption>{fig.group(1)}</figcaption></figure>" if fig
                           else "<p>" + inner + "</p>")
                para.clear()

        while i < n:
            line = lines[i]
            if not line.strip():
                end_para()
                i += 1
                continue
            m = _FENCE.match(line)
            if m:
                end_para()
                fence, info = m.group(2), m.group(3)
                indent = len(m.group(1))
                body = []
                i += 1
                while i < n and not re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$", lines[i]):
                    body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
                    i += 1
                i += 1
                raw = re.fullmatch(r"\{\s*=([\w-]+)\s*\}", info)
                if raw:  # ```{=latex}: kept for writers of that format (HTML goes into the book as it is)
                    fmt = raw.group(1).lower()
                    out.append("\n".join(body) if fmt == "html" else
                               f'<pre class="raw" data-format="{html.escape(fmt)}">' + html.escape("\n".join(body)) + "</pre>")
                    continue
                if info.startswith("{"):
                    classes = re.findall(r"\.([\w+#-]+)", info)
                    info = classes[0] if classes else ""
                cls = f' class="language-{html.escape(info)}"' if info else ""
                out.append(f"<pre><code{cls}>" + html.escape("\n".join(body)) + "</code></pre>")
                continue
            m = _ATX.match(line)
            if m:
                end_para()
                level = len(m.group(1))
                text, attrs = _heading_attrs((m.group(2) or "").strip())
                out.append(f"<h{level}{attrs}>{self.inline(text)}</h{level}>")
                i += 1
                continue
            if para and _SETEXT.match(line):
                level = 1 if line.strip()[0] == "=" else 2
                text, attrs = _heading_attrs("\n".join(s.strip() for s in para))
                para.clear()
                out.append(f"<h{level}{attrs}>{self.inline(text)}</h{level}>")
                i += 1
                continue
            if not para and (_ONE_DASH_RUN.match(line) or _COLUMNS.match(line)) and self.depth < 32:
                j = self._multiline_table(lines, i, out) if _ONE_DASH_RUN.match(line) else self._headless_table(lines, i, out)
                if j > i:
                    i = j
                    continue
            if _HR.match(line):
                end_para()
                out.append("<hr/>")
                i += 1
                continue
            if _QUOTE.match(line):
                end_para()
                inner = []
                while i < n and lines[i].strip():
                    q = _QUOTE.match(lines[i])
                    inner.append(q.group(1) if q else lines[i])
                    i += 1
                    if i < n and not lines[i].strip() and i + 1 < n and _QUOTE.match(lines[i + 1]):
                        inner.append("")
                        i += 1
                out.append("<blockquote>" + self.blocks(inner) + "</blockquote>")
                continue
            if (_BULLET.match(line) or _ordered(line)) and (
                    not para or _BULLET.match(line) or _ordered(line).group(2) == "1"):
                end_para()
                i = self._list(lines, i, out)
                continue
            if line.startswith("    ") and not para and not (i + 1 < n and _COLUMNS.match(lines[i + 1])):
                body = []
                while i < n and (lines[i].startswith("    ") or not lines[i].strip()):
                    body.append(lines[i][4:])
                    i += 1
                while body and not body[-1].strip():
                    body.pop()
                out.append("<pre><code>" + html.escape("\n".join(body)) + "</code></pre>")
                continue
            if _HTML_BLOCK.match(line):
                end_para()
                body = []
                while i < n and lines[i].strip():
                    body.append(lines[i])
                    i += 1
                out.append(self._html_block(body))
                continue
            m = _TEX_BEGIN.match(line)
            if m and not para:
                # a TeX environment (Pandoc's raw_tex): kept for LaTeX writers, shown as text by others
                end = f"\\end{{{m.group(1)}}}"
                body = []
                while i < n:
                    body.append(lines[i])
                    i += 1
                    if end in body[-1]:
                        break
                out.append('<pre class="raw" data-format="latex">' + html.escape("\n".join(body)) + "</pre>")
                continue
            if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
                end_para()
                i = self._table(lines, i, out)
                continue
            # Pandoc's Markdown: fenced divs, definition lists, simple and grid tables
            if not para and self.depth < 32:
                j = self._pandoc_block(lines, i, out)
                if j > i:
                    i = j
                    continue
            para.append(line)
            i += 1
        end_para()
        return "\n".join(out)

    def _pandoc_block(self, lines: list[str], i: int, out: list[str]) -> int:
        """A fenced div, definition list, simple table or grid table starting at line i; the line after it
        (or i, when there is none)."""
        line, n = lines[i], len(lines)
        m = _DIV_OPEN.match(line)
        if m:
            depth, j, inner = 1, i + 1, []
            while j < n:
                if _DIV_OPEN.match(lines[j]):
                    depth += 1
                elif _DIV_CLOSE.match(lines[j]):
                    depth -= 1
                    if not depth:
                        break
                inner.append(lines[j])
                j += 1
            out.append(f"<div{_div_attrs(m.group(1))}>{self._nested(inner)}</div>")
            return j + 1
        nxt = lines[i + 1] if i + 1 < n else ""
        after = lines[i + 2] if i + 2 < n else ""
        if _LINE_BLOCK.match(line):
            return self._line_block(lines, i, out)
        if not line.startswith(" ") and (_DEF_ITEM.match(nxt) or not nxt.strip() and _DEF_ITEM.match(after)):
            return self._deflist(lines, i, out)
        if _GRID_RULE.match(line) and i + 1 < n and lines[i + 1].lstrip().startswith("|"):
            return self._grid_table(lines, i, out)
        if _COLUMNS.match(nxt) and "|" not in line:
            return self._simple_table(lines, i, out, headless=False)
        return i

    def _line_block(self, lines: list[str], i: int, out: list[str]) -> int:
        """``| `` lines: line breaks and leading spaces kept; a line starting with a space continues the one before."""
        rows: list[str] = []
        n = len(lines)
        while i < n:
            m = _LINE_BLOCK.match(lines[i])
            if m:
                text = m.group(1) or ""
                rows.append(re.sub(r"^ +", lambda s: "\xa0" * len(s.group(0)), text))
            elif rows and lines[i].startswith(" ") and lines[i].strip():
                rows[-1] += " " + lines[i].strip()
            else:
                break
            i += 1
        out.append('<div class="line-block">' + "<br/>".join(self.inline(r) for r in rows) + "</div>")
        return i

    def _html_block(self, body: list[str]) -> str:
        """Raw HTML kept as written; the lines between its tags are Markdown (Pandoc's markdown_in_html_blocks)."""
        if re.match(r"^ {0,3}<(?:pre|script|style|textarea|!--)", body[0], re.IGNORECASE) or len(body) < 3:
            return "\n".join(body)
        out: list[str] = []
        segment: list[str] = []

        def flush():
            if segment:
                inner = self._nested(segment)
                if inner.startswith("<p>") and inner.endswith("</p>") and inner.count("<p>") == 1:
                    inner = inner[3:-4]
                out.append(inner)
                segment.clear()

        for line in body:
            if _TAGS_LINE.match(line):
                flush()
                out.append(line)
            else:
                segment.append(line)
        flush()
        return "\n".join(out)

    def _nested(self, lines: list[str]) -> str:
        self.depth += 1
        try:
            return self.blocks(lines)
        finally:
            self.depth -= 1

    def _deflist(self, lines: list[str], i: int, out: list[str]) -> int:
        n = len(lines)
        parts = ["<dl>"]
        while i < n:
            term = lines[i]
            j = i + 1
            if j < n and not lines[j].strip():
                j += 1
            if not term.strip() or j >= n or not _DEF_ITEM.match(lines[j]):
                break
            parts.append(f"<dt>{self.inline(term.strip())}</dt>")
            tight = j == i + 1
            while j < n and _DEF_ITEM.match(lines[j]):
                m = _DEF_ITEM.match(lines[j])
                indent = len(m.group(0)) - len(m.group(2))
                body = [m.group(2)]
                j += 1
                while j < n:
                    if not lines[j].strip():
                        if j + 1 < n and lines[j + 1].startswith(" " * min(indent, 4)) and lines[j + 1].strip() \
                                and not _DEF_ITEM.match(lines[j + 1]):
                            body.append("")
                            j += 1
                            continue
                        break
                    if _DEF_ITEM.match(lines[j]):
                        break
                    body.append(lines[j][min(indent, len(lines[j]) - len(lines[j].lstrip())):])
                    j += 1
                inner = self._nested(body)
                if tight and inner.startswith("<p>") and inner.endswith("</p>") and inner.count("<p>") == 1:
                    inner = inner[3:-4]
                parts.append(f"<dd>{inner}</dd>")
                if j < n and not lines[j].strip() and j + 1 < n and _DEF_ITEM.match(lines[j + 1]):
                    j += 1
            i = j
            if i < n and not lines[i].strip():
                k = i + 1
                if k < n and lines[k].strip() and not lines[k].startswith(" ") and (
                        k + 1 < n and (_DEF_ITEM.match(lines[k + 1]) or not lines[k + 1].strip() and k + 2 < n
                                       and _DEF_ITEM.match(lines[k + 2]))):
                    i = k
                    continue
            break
        parts.append("</dl>")
        out.append("".join(parts))
        return i

    def _simple_table(self, lines: list[str], i: int, out: list[str], headless: bool) -> int:
        """A heading line over runs of dashes, then one row per line up to a blank line."""
        n = len(lines)
        cols = [(m.start(), m.end()) for m in re.finditer(r"-+", lines[i + 1])]
        j = i + 2
        rows = []
        while j < n and lines[j].strip():
            rows.append([lines[j]])
            j += 1
        return self._lined_table(lines, j, out, cols, [lines[i]], rows)

    def _multiline_table(self, lines: list[str], i: int, out: list[str]) -> int:
        """A row of dashes, heading lines, runs of dashes, rows of several lines with blank lines between,
        and a closing row of dashes."""
        n = len(lines)
        k = i + 1
        while k < n and lines[k].strip() and not _COLUMNS.match(lines[k]):
            k += 1
        if k >= n or not _COLUMNS.match(lines[k]) or k == i + 1:
            return i
        end = k + 1
        while end < n and not _ONE_DASH_RUN.match(lines[end]):
            end += 1
        if end >= n:
            return i
        cols = [(m.start(), m.end()) for m in re.finditer(r"-+", lines[k])]
        rows: list[list[str]] = []
        cur: list[str] = []
        for line in lines[k + 1:end]:
            if line.strip():
                cur.append(line)
            elif cur:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        return self._lined_table(lines, end + 1, out, cols, lines[i + 1:k], rows)

    def _headless_table(self, lines: list[str], i: int, out: list[str]) -> int:
        """A table without headings: runs of dashes, rows (one line each, or several with blank lines
        between), and the same runs of dashes again."""
        n = len(lines)
        end = i + 1
        while end < n and not _COLUMNS.match(lines[end]):
            if not lines[end].strip() and (end + 1 >= n or not lines[end + 1].strip()):
                return i  # two blank lines: no closing dashes, so not a table
            end += 1
        if end >= n or end == i + 1:
            return i
        body = lines[i + 1:end]
        rows: list[list[str]] = []
        if any(not line.strip() for line in body):
            cur: list[str] = []
            for line in body:
                if line.strip():
                    cur.append(line)
                elif cur:
                    rows.append(cur)
                    cur = []
            if cur:
                rows.append(cur)
        else:
            rows = [[line] for line in body]
        cols = [(m.start(), m.end()) for m in re.finditer(r"-+", lines[i])]
        return self._lined_table(lines, end + 1, out, cols, [], rows)

    def _lined_table(self, lines, j, out, cols, head_lines, rows) -> int:
        """Write a simple or multiline table: cells cut at the dashes' columns; alignment from where the
        heading sits over its dashes (flush right, flush left, both: default, neither: centered)."""

        def split(line: str) -> list[str]:
            cells = []
            for k, (a, _b) in enumerate(cols):
                start = a if k else 0
                end = cols[k + 1][0] if k + 1 < len(cols) else len(line)
                cells.append(line[start:end].strip())
            return cells

        def join(group: list[str]) -> list[str]:
            per = [split(line) for line in group]
            return [" ".join(c[k] for c in per if c[k]) for k in range(len(cols))]

        head = head_lines[0] if head_lines else ""
        aligns = []
        for a, b in cols:
            text = head[a:b]
            left = bool(text[:1].strip())
            right = len(text) == b - a and bool(text[-1:].strip())
            aligns.append("" if not text.strip() or left and right else "right" if right else "left" if left else "center")

        def row_html(cells, tag):
            out_cells = []
            for k, c in enumerate(cells):
                style = f' style="text-align: {aligns[k]}"' if aligns[k] else ""
                out_cells.append(f"<{tag}{style}>{self.inline(c)}</{tag}>")
            return "<tr>" + "".join(out_cells) + "</tr>"

        caption, j = self._caption(lines, j)
        parts = ["<table>"]
        if caption:
            parts.append(f"<caption>{caption}</caption>")
        heads = join(head_lines) if head_lines else []
        if any(heads):
            parts.append("<thead>" + row_html(heads, "th") + "</thead>")
        parts.append("<tbody>" + "".join(row_html(join(r), "td") for r in rows) + "</tbody></table>")
        out.append("".join(parts))
        return j

    def _caption(self, lines: list[str], j: int) -> tuple[str, int]:
        """A table's caption: ``: text`` or ``Table: text`` after it, past one blank line."""
        k = j
        if k < len(lines) and not lines[k].strip():
            k += 1
        m = re.match(r"^\s*(?:Table)?:\s+(\S.*)$", lines[k]) if k < len(lines) else None
        if not m:
            return "", j
        text = [m.group(1)]
        k += 1
        while k < len(lines) and lines[k].strip():
            text.append(lines[k].strip())
            k += 1
        return self.inline(" ".join(text)), k

    def _grid_table(self, lines: list[str], i: int, out: list[str]) -> int:
        """+---+---+ tables: cells may run over several lines; a +===+ rule ends the header."""
        n = len(lines)
        top = lines[i].strip()
        bounds = [k for k, c in enumerate(top) if c == "+"]
        rows: list[tuple[bool, list[list[str]]]] = []
        cur: list[list[str]] = [[] for _ in bounds[:-1]]
        head_rows = 0
        j = i + 1
        while j < n and lines[j].strip()[:1] in ("|", "+"):
            s = lines[j].strip()
            if _GRID_RULE.match(s):
                if any(any(c.strip() for c in cell) for cell in cur):
                    rows.append((False, cur))
                if "=" in s:
                    head_rows = len(rows)
                cur = [[] for _ in bounds[:-1]]
            else:
                for k in range(len(bounds) - 1):
                    cur[k].append(s[bounds[k] + 1:bounds[k + 1]] if len(s) > bounds[k] else "")
            j += 1
        parts = ["<table>"]
        for r, (_, cells) in enumerate(rows):
            tag = "th" if r < head_rows else "td"
            html_cells = []
            for cell in cells:
                inner = self._nested([c.rstrip()[1:] if c.startswith(" ") else c.rstrip() for c in cell])
                if inner.startswith("<p>") and inner.endswith("</p>") and inner.count("<p>") == 1:
                    inner = inner[3:-4]
                html_cells.append(f"<{tag}>{inner}</{tag}>")
            row = "<tr>" + "".join(html_cells) + "</tr>"
            parts.append(f"<thead>{row}</thead>" if r < head_rows else row)
        parts.append("</table>")
        caption, j = self._caption(lines, j)
        if caption:
            parts.insert(1, f"<caption>{caption}</caption>")
        out.append("".join(parts))
        return j

    def _list(self, lines, i, out):
        n = len(lines)
        first = _BULLET.match(lines[i]) or _ordered(lines[i])
        ordered = first.re is _ORDERED
        kind, start = _marker_kind(first.group(2)) if ordered else ("", 1)
        style = (first.group(2)[:1] == "(", first.group(3)) if ordered else None
        items: list[list[str]] = []
        loose = False
        while i < n:
            m = _ordered(lines[i]) if ordered else _BULLET.match(lines[i])
            # a marker of another kind or style starts a new list, as in Pandoc
            if not m or ordered and items and not _same_style(m, kind, style):
                break
            if ordered:
                content, marker_w = m.group(5), len(m.group(1)) + len(m.group(2)) + 1 + max(1, len(m.group(4)))
            else:
                content, marker_w = m.group(4), len(m.group(1)) + 1 + max(1, len(m.group(3)))
            item = [content]
            i += 1
            while i < n:
                line = lines[i]
                if not line.strip():
                    # a blank line inside the list: continue if the next line is indented or another item
                    if i + 1 < n and (lines[i + 1].startswith(" " * min(marker_w, 4)) and lines[i + 1].strip()):
                        item.append("")
                        loose = True
                        i += 1
                        continue
                    nm = (_ordered(lines[i + 1]) if ordered else _BULLET.match(lines[i + 1])) if i + 1 < n else None
                    if nm and (not ordered or _same_style(nm, kind, style)):  # a blank line between items: loose
                        loose = True
                        i += 1
                    break
                if line.startswith((" " * marker_w, "    ")):
                    rest = line[min(marker_w, len(line) - len(line.lstrip())):]
                    if (_BULLET.match(rest) or _ordered(rest)) and item[-1].strip() and \
                            not (_BULLET.match(item[-1]) or _ordered(item[-1])):
                        item.append("")  # a sublist right under the item's text
                    item.append(rest)
                    i += 1
                    continue
                if (_BULLET.match(line) or _ordered(line)) or _HR.match(line) or _ATX.match(line) or _QUOTE.match(line):
                    break
                item.append(line)  # lazy continuation
                i += 1
            items.append(item)
        tag = "ol" if ordered else "ul"
        attr = f' start="{start}"' if ordered and start != 1 else ""
        if ordered and kind != "1":
            attr += f' type="{kind}"'  # a. A. i. I. lists keep their numbering style
        parts = [f"<{tag}{attr}>"]
        for item in items:
            inner = self.blocks(item)
            if not loose and inner.startswith("<p>"):
                inner = re.sub(r"^<p>(.*?)</p>", r"\1", inner, count=1, flags=re.DOTALL)
            parts.append(f"<li>{inner}</li>")
        parts.append(f"</{tag}>")
        out.append("".join(parts))
        return i

    def _table(self, lines, i, out):
        def cells(line):
            line = line.strip()
            line = line.removeprefix("|")
            if line.endswith("|") and not line.endswith("\\|"):
                line = line[:-1]
            return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]

        head = cells(lines[i])
        aligns = []
        for c in cells(lines[i + 1]):
            left, right = c.startswith(":"), c.endswith(":")
            aligns.append("center" if left and right else "right" if right else "left" if left else "")
        i += 2
        rows = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i]:
            rows.append(cells(lines[i]))
            i += 1

        def cell(tag, text, k):
            a = aligns[k] if k < len(aligns) else ""
            style = f' style="text-align: {a}"' if a else ""
            return f"<{tag}{style}>{self.inline(text)}</{tag}>"

        parts = ["<table><thead><tr>"] + [cell("th", c, k) for k, c in enumerate(head)] + ["</tr></thead><tbody>"]
        for r in rows:
            parts.append("<tr>" + "".join(cell("td", r[k] if k < len(r) else "", k) for k in range(len(head))) + "</tr>")
        parts.append("</tbody></table>")
        out.append("".join(parts))
        return i

    # --- inlines ------------------------------------------------------------------------------
    def inline(self, text: str) -> str:
        placeholders: list[str] = []

        def keep(s):
            placeholders.append(s)
            return f"\x00{len(placeholders) - 1}\x00"

        # code spans first: nothing inside them is markup (`x`{=html} is raw HTML, `x`{=tex} raw TeX)
        text = re.sub(r"(`+)(.{1,1000}?)(?<!`)\1(?!`)(\{[^{}\n]{0,200}\})?", lambda m: keep(_code_span(m.group(2), m.group(3))),
                      text, flags=re.DOTALL)
        # TeX math, as Pandoc reads it: $$display$$, and $inline$ where the opening $ has no space
        # after it and the closing one no space before it nor a digit after it ("$5 and $10" is text)
        text = _DISPLAY_MATH.sub(lambda m: keep(_math(m.group(1), True)), text)
        text = _INLINE_MATH.sub(lambda m: keep(_math(m.group(1), False)), text)
        text = re.sub(r"\\ ", lambda m: keep("\N{NO-BREAK SPACE}"), text)  # Pandoc: an escaped space doesn't break
        text = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", lambda m: keep(html.escape(m.group(1))), text)
        # Pandoc footnotes: [^id] calls of notes defined in the text, and ^[inline notes]
        if "[^" in text and self.notes.defs:
            text = _FOOTNOTE_REF.sub(lambda m: keep(self._note_call(m.group(1), None)) if m.group(1) in self.notes.defs
                                     else m.group(0), text)
        if "^[" in text:
            text = _INLINE_NOTE.sub(lambda m: keep(self._note_call(None, m.group(1))), text)
        text = _AUTOLINK.sub(lambda m: keep(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>'), text)
        text = re.sub(r"<([\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8})>",
                      lambda m: keep(f'<a href="mailto:{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>'), text)
        # raw TeX, as Pandoc's raw_tex reads it: a command that takes arguments (\cite[..]{..}, \textbf{..})
        if "\\" in text:
            text = _TEX_INLINE.sub(lambda m: keep(f'<span class="raw" data-format="latex">{html.escape(m.group(0))}</span>'),
                                   text)
        text = re.sub(r"<!--.{0,2000}?-->|</?[A-Za-z][A-Za-z0-9:-]*(?:\s+[A-Za-z_:][\w:.-]*(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*\s*/?>",
                      lambda m: keep(m.group(0)), text, flags=re.DOTALL)
        text = re.sub(r"&(#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);", lambda m: keep(m.group(0)), text)
        text = html.escape(text, quote=False)

        def link(m, image):
            label, dest = m.group(1), m.group(2).strip()
            title = ""
            tm = re.match(r"^<?([^\s>]*)>?\s+(?:\"(.*)\"|'(.*)')$", dest, re.DOTALL)
            if tm:
                dest, title = tm.group(1), tm.group(2) or tm.group(3) or ""
            elif dest.startswith("<") and dest.endswith(">"):
                dest = dest[1:-1]
            return self._link(label, dest, title, image, keep)

        def reflink(m, image):
            label = m.group(1)
            ref = (m.group(2) or label).strip().lower()
            if ref not in self.refs:
                return m.group(0)
            dest, title = self.refs[ref]
            return self._link(label, dest, title, image, keep)

        for _ in range(3):  # nested brackets (an image inside a link)
            text = re.sub(r"!\[([^\[\]]*)\]\(([^()\s]*(?:\s+&quot;[^&]*&quot;)?(?:\s+\"[^\"]*\")?)\)",
                          lambda m: link(_unq(m), True), text)
            text = re.sub(r"\[([^\[\]]*)\]\(([^()\s]*(?:\([^()]*\)[^()\s]*)*(?:\s+\"[^\n]{0,500}?\"(?=\))|\s+'[^']*')?)\)",
                          lambda m: link(_unq(m), False), text)
            text = re.sub(r"!\[([^\[\]]+)\](?:\[([^\[\]]*)\])?", lambda m: reflink(m, True), text)
            text = re.sub(r"\[([^\[\]]+)\](?:\[([^\[\]]*)\])?", lambda m: reflink(m, False), text)
            # Pandoc's bracketed spans: [text]{#id .class}
            if "]{" in text:
                text = _SPAN.sub(lambda m: _span(m.group(1), m.group(2), keep), text)
        # spans are bounded (1,000 characters), so unclosed markers can't make the search quadratic
        text = re.sub(r"(\*\*|__)(?=\S)(.{1,1000}?)(?<=\S)\1", r"<strong>\2</strong>", text, flags=re.DOTALL)
        text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.{1,1000}?)(?<=[^\s*])\*(?!\*)", r"<em>\1</em>", text, flags=re.DOTALL)
        text = re.sub(r"(?<![\w_])_(?=[^\s_])(.{1,1000}?)(?<=[^\s_])_(?![\w_])", r"<em>\1</em>", text, flags=re.DOTALL)
        text = re.sub(r"~~(?=\S)(.{1,1000}?)(?<=\S)~~", r"<del>\1</del>", text, flags=re.DOTALL)
        if "^" in text or "~" in text:  # Pandoc's H~2~O and x^2^ (no spaces inside), never inside a tag
            text = re.sub(r"(<[^<>]*>)|(?<!~)~((?:[^\s~<>]|<[^<>]*>){1,200}?)~(?!~)|\^((?:[^\s^<>]|<[^<>]*>){1,200}?)\^",
                          lambda m: m.group(1) or (f"<sub>{m.group(2)}</sub>" if m.group(2) else f"<sup>{m.group(3)}</sup>"),
                          text)
        text = re.sub(r"( {2,}|\\)\n", "<br/>\n", text)
        if any(c in text for c in "\"'-."):
            text = _smart(text)
        text = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)
        # placeholders can nest (code inside a link label)
        text = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)
        return text

    def _note_call(self, label: str | None, inline: str | None) -> str:
        """The call of a footnote; the note itself is written after this chapter's text."""
        notes = self.notes
        if label is not None and label in notes.ids:
            num = notes.ids[label]
        else:
            notes.count += 1
            num = notes.count
            if label is not None:
                notes.ids[label] = num
            if inline is not None:
                body = f"<p>{self.inline(inline)}</p>"
            else:
                body = self._nested(notes.defs.get(label or "", [])) if self.depth < 32 else ""
            notes.where[num] = notes.current
            self.asides.append(f'<aside epub:type="footnote" id="fn{num}">{body or "<p></p>"}</aside>')
        doc = notes.where.get(num, notes.current)
        target = f"#fn{num}" if doc == notes.current else f"{doc}#fn{num}"  # a note in an earlier chapter
        return f'<a class="noteref" epub:type="noteref" href="{target}"><sup>{num}</sup></a>'

    def _link(self, label, dest, title, image, keep):
        dest = html.unescape(dest)
        t = f' title="{html.escape(html.unescape(title))}"' if title else ""
        if image:
            return keep(f'<img src="{html.escape(dest)}" alt="{html.escape(html.unescape(_strip_tags(label)))}"{t}/>')
        return f'<a href="{html.escape(dest)}"{t}>{label}</a>'


_DISPLAY_MATH = re.compile(r"(?<![\\$])\$\$((?:[^$\\]|\\.|\$(?!\$)){1,20000}?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<![\\$])\$(?=[^\s$])((?:[^$\\]|\\.){1,10000}?)(?<=[^\s\\])\$(?![\d$])", re.DOTALL)


def _math(tex: str, display: bool) -> str:
    return markup.to_string(mathml.from_tex(tex.strip(), display))


def _unq(m):
    return m


def _strip_tags(s):
    return re.sub(r"<[^>]+>|\x00\d+\x00", "", s)


def to_html(text: str) -> str:
    """Markdown to an HTML fragment."""
    return _Parser(text).html()


def read(path) -> Book:
    p = Path(path)
    text = markup.decode(p.read_bytes())
    book = book_from_markdown(text, p.stem.replace("_", " "), base_dir=p.parent)
    return book


def book_from_markdown(text: str, title: str = "", base_dir: Path | None = None, images: dict | None = None,
                       top: Path | None = None) -> Book:
    """A book from Markdown; local images are taken from the ``images`` {name: bytes} map, or from
    ``base_dir`` and one level up (``top``, when given, is the outermost folder allowed instead)."""
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    meta, text = _front_matter(text)
    book = Book(Metadata(title=meta.get("title", title)))
    book.source_format = "md"
    if meta.get("author"):
        book.metadata.authors = [a.strip() for a in re.split(r"\s*(?:,|;| and )\s*", meta["author"]) if a.strip()]
    book.metadata.language = meta.get("lang", meta.get("language", "en"))
    if meta.get("date"):
        book.metadata.date = meta["date"]
    # one document per top-level heading, so big files are split into chapters; footnotes are defined
    # anywhere, so their definitions are taken out of the whole text first
    text, defs = _footnote_defs(text)
    notes = _Notes(defs)
    chunks = _split_chapters(text)
    for n, chunk in enumerate(chunks, 1):
        notes.current = f"part{n:04d}.xhtml"
        body_html = _Parser(chunk, notes).html()
        root = markup.parse(f'<html xmlns:epub="{markup.EPUB_NS}"><head><title>{html.escape(book.metadata.title)}</title>'
                            f"</head><body>{body_html}</body></html>")
        href = f"text/part{n:04d}.xhtml"
        _pull_images(book, root, href, base_dir, images or {}, top=top)
        book.add_document(href, root)
    if not book.spine:
        root, body = markup.new_document(book.metadata.title)
        ET.SubElement(body, "p")
        book.add_document("text/part0001.xhtml", root)
    if not meta.get("title"):
        first = book.tree(book.spine[0]).find(".//h1")
        if first is not None and markup.text_of(first).strip():
            book.metadata.title = " ".join(markup.text_of(first).split())
    book.ensure_toc()
    return book


def _front_matter(text):
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n(---|\.\.\.)\s*\n", text, re.DOTALL)
    if m:
        for line in m.group(1).split("\n"):
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip().lower()] = v.strip().strip("\"'")
        text = text[m.end():]
    else:
        # Pandoc title block: % title / % author / % date
        lines = text.split("\n", 3)
        if lines and lines[0].startswith("% "):
            meta["title"] = lines[0][2:].strip()
            if len(lines) > 1 and lines[1].startswith("% "):
                meta["author"] = lines[1][2:].strip()
            if len(lines) > 2 and lines[2].startswith("% "):
                meta["date"] = lines[2][2:].strip()
            text = "\n".join(line for line in text.split("\n") if not line.startswith("% ") or text.index(line) > 200)
    return meta, text


def _split_chapters(text, target=60_000):
    lines = text.split("\n")
    chunks, cur, size, fence = [], [], 0, False
    for line in lines:
        if _FENCE.match(line):
            fence = not fence
        if not fence and re.match(r"^#{1,2}\s", line) and size > target // 10 and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()] or [text]


def _pull_images(book, root, href, base_dir, images, top=None):
    for el in root.iter("img"):
        src = el.get("src", "")
        if not src or is_external(src) or src.startswith("data:"):
            continue
        from urllib.parse import unquote
        name = unquote(src.split("#")[0].split("?")[0])
        data = images.get(name) or images.get(name.lstrip("./"))
        if data is None and base_dir is not None:
            p = safe_path(base_dir, name, top)
            try:
                data = p.read_bytes() if p is not None and p.is_file() else None
            except OSError:
                data = None
        if data is None:
            continue
        img_href = "images/" + re.sub(r"[^\w.-]", "_", name.replace("\\", "/").split("/")[-1])
        existing = next((h for h, r in book.resources.items() if r.data == data), None)
        if existing is None:
            existing = book.add(img_href, data).href
        el.set("src", relative(href, existing))


# --- writing -----------------------------------------------------------------------------------

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>$])")


def _esc(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


class _Writer:
    def __init__(self, book: Book, image_dir: str = "images"):
        self.book = book
        self.image_dir = image_dir
        self.targets: set[str] = set()
        self.used_images: dict[str, str] = {}
        self.notes: dict[str, tuple[str, ET.Element]] = {}  # "doc#id" -> (doc, aside): written as [^n]
        self.note_num: dict[str, int] = {}
        self.note_texts: list[str] = []
        for href, _ in book.documents():
            for el in book.tree(href).iter():
                if el.tag == "aside" and is_note(el) and el.get("id"):
                    self.notes[f"{href}#{el.get('id')}"] = (href, el)
                if el.tag != "a":
                    continue
                link = el.get("href", "")
                if link and not is_external(link) and not is_noteref(el):
                    t = resolve(href, link)
                    frag = link.split("#", 1)[1] if "#" in link else ""
                    self.targets.add(f"{t}#{frag}" if frag else t)

    def note(self, el) -> str:
        """A footnote call as [^n]; the note is written at the end of the text."""
        link = el.get("href", "")
        doc, _, frag = link.partition("#")
        key = f"{resolve(self.href, doc) if doc else self.href}#{frag}"
        if key not in self.notes:
            return self.inline_children(el)
        if key not in self.note_num:
            self.note_num[key] = len(self.note_texts) + 1
            self.note_texts.append("")
            saved, (self.href, aside) = self.href, self.notes[key]
            try:
                body = self.blocks(aside, 0)
            finally:
                self.href = saved
            body = re.sub(r"^\s*(?:\[?\d+\]?\.?|↩︎?)\s+", "", body).replace("↩︎", "").replace("↩", "").strip()
            self.note_texts[self.note_num[key] - 1] = body
        return f"[^{self.note_num[key]}]"

    def footnotes(self) -> str:
        out = []
        for n, body in enumerate(self.note_texts, 1):
            lines = body.split("\n")
            out.append(f"[^{n}]: " + lines[0] + "".join("\n" + ("    " + ln if ln else "") for ln in lines[1:]))
        return "\n\n".join(out)

    def anchor_name(self, key: str) -> str:
        return re.sub(r"[^\w-]", "_", key.replace(".xhtml", "").replace("/", "_").replace("#", "-"))

    def document(self, href: str) -> str:
        self.href = href
        root = self.book.tree(href)
        body = root.find("body")
        out = []
        if href in self.targets:
            out.append(f'<a id="{self.anchor_name(href)}"></a>\n')
        out.append(self.blocks(body, 0))
        return "".join(out).strip("\n")

    def blocks(self, el, depth) -> str:
        parts: list[str] = []
        inline: list[str] = []

        def flush():
            s = "".join(inline).strip()
            inline.clear()
            if s:
                parts.append(_wrap_lines(s))

        if el.text and el.text.strip():
            inline.append(self._text(el.text))
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            if tag == "aside" and is_note(child):
                pass  # written as [^n] where it is called
            elif tag in markup.BLOCK or tag in ("li", "figure", "nav", "section"):
                flush()
                block = self.block(child, depth)
                if block:
                    parts.append(block)
            else:
                inline.append(self.inline_el(child))
            if child.tail and child.tail.strip() or child.tail and inline:
                inline.append(self._text(child.tail))
        flush()
        return "\n\n".join(p for p in parts if p.strip())

    def _anchors(self, el) -> str:
        i = el.get("id")
        if i and f"{self.href}#{i}" in self.targets:
            return f'<a id="{self.anchor_name(self.href + "#" + i)}"></a>'
        return ""

    def block(self, el, depth) -> str:
        tag = el.tag
        anchor = self._anchors(el)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = " ".join(self.inline_children(el).split())
            return f"{'#' * int(tag[1])} {anchor}{text}" if text else anchor
        if tag == "p":
            text = self.inline_children(el).strip()
            return anchor + _wrap_lines(text) if text else anchor
        if tag == "hr":
            return "* * *"
        if tag == "pre":
            code = markup.text_of(el)
            fence = "~~~" if "```" in code else "```"
            return f"{anchor}{fence}\n{code.rstrip()}\n{fence}"
        if tag == "blockquote":
            inner = self.blocks(el, depth)
            return anchor + "\n".join("> " + line if line else ">" for line in inner.split("\n"))
        if tag in ("ul", "ol"):
            items = []
            n = int(el.get("start", "1")) if el.get("start", "1").isdigit() else 1
            for li in el:
                if li.tag != "li":
                    continue
                marker = f"{n}." if tag == "ol" else "-"
                n += 1
                inner = self.blocks(li, depth + 1) if any(isinstance(c.tag, str) and c.tag in markup.BLOCK for c in li) \
                    else self.inline_children(li).strip()
                pad = " " * (len(marker) + 1)
                lines = inner.split("\n")
                items.append(f"{marker} " + lines[0] + "".join("\n" + (pad + line if line else "") for line in lines[1:]))
            return anchor + "\n".join(items)
        if tag == "table":
            return anchor + self.table(el)
        if tag == "dl":
            parts = []
            for c in el:
                if c.tag == "dt":
                    parts.append(f"**{self.inline_children(c).strip()}**")
                elif c.tag == "dd":
                    parts.append(": " + self.inline_children(c).strip())
            return anchor + "\n\n".join(parts)
        if tag in ("img",):
            return self.inline_el(el)
        if tag == "figure" and len(list(el.iter("img"))) == 1:
            # one picture: written alone in its paragraph, its caption as the alt text, it reads back as a figure
            img = el.find(".//img")
            cap = el.find("figcaption")
            caption = " ".join(markup.text_of(cap).split()) if cap is not None else ""
            if caption:
                img = ET.Element("img", {**img.attrib, "alt": caption})
            return anchor + self.inline_el(img)
        return anchor + self.blocks(el, depth)

    def table(self, el) -> str:
        rows = []
        for tr in el.iter("tr"):
            row = []
            for c in tr:
                if c.tag in ("td", "th"):
                    row.append(" ".join(self.inline_children(c).split()).replace("|", "\\|"))
                    span = c.get("colspan", "1")
                    row += [""] * (min(int(span), 50) - 1 if span.isdigit() and int(span) > 1 else 0)
            rows.append(row)
        caption = el.find("caption")
        caption = _wrap_lines(self.inline_children(caption).strip()) + "\n\n" if caption is not None else ""
        if not rows:
            return caption
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return caption + "\n".join(out)

    def inline_children(self, el) -> str:
        out = [self._text(el.text or "")]
        for c in el:
            out.append(self.inline_el(c))
            out.append(self._text(c.tail or ""))
        return "".join(out)

    def _text(self, s: str) -> str:
        return _esc(re.sub(r"\s+", " ", s))

    def inline_el(self, el) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        anchor = self._anchors(el)
        if tag in ("script", "style", "head"):
            return ""
        if tag == "br":
            return "  \n"
        if tag == "math":
            tex = " ".join(re.sub(r"(?<!\\)%[^\n]*", "", mathml.to_tex(el)).split())
            if not tex:
                return ""
            return anchor + (f"$${tex}$$" if el.get("display") == "block" else f"${tex}$")
        if tag in ("em", "i", "cite", "dfn", "var"):
            inner = self.inline_children(el)
            return anchor + (f"*{inner.strip()}*" if inner.strip() else inner)
        if tag in ("strong", "b"):
            inner = self.inline_children(el)
            return anchor + (f"**{inner.strip()}**" if inner.strip() else inner)
        if tag in ("del", "s", "strike"):
            inner = self.inline_children(el)
            return anchor + (f"~~{inner.strip()}~~" if inner.strip() else inner)
        if tag in ("code", "kbd", "samp", "tt"):
            code = markup.text_of(el)
            tick = "``" if "`" in code else "`"
            return anchor + f"{tick}{code}{tick}"
        if tag == "a" and is_noteref(el):
            return anchor + self.note(el)
        if tag == "a":
            inner = self.inline_children(el)
            link = el.get("href", "")
            if not link:
                return anchor + inner
            if not is_external(link):
                t = resolve(self.href, link)
                frag = link.split("#", 1)[1] if "#" in link else ""
                key = f"{t}#{frag}" if frag else t
                link = "#" + self.anchor_name(key)
            return anchor + f"[{inner.strip()}]({link})"
        if tag in ("img", "image"):
            src = el.get("src") or el.get("xlink:href") or ""
            if not src or is_external(src):
                return ""
            target = resolve(self.href, src)
            name = self.used_images.get(target)
            if name is None:
                base = re.sub(r"[^\w.-]", "_", target.split("/")[-1])
                name = f"{self.image_dir}/{base}"
                while name in self.used_images.values():
                    name = f"{self.image_dir}/{len(self.used_images)}_{base}"
                self.used_images[target] = name
            alt = el.get("alt", "")
            return anchor + f"![{_esc(alt)}]({name})"
        if tag == "sup":
            return anchor + f"<sup>{self.inline_children(el)}</sup>"
        if tag == "sub":
            return anchor + f"<sub>{self.inline_children(el)}</sub>"
        if tag == "svg":
            img = el.find(".//image")
            return self.inline_el(img) if img is not None else ""
        if tag in markup.BLOCK:
            return " " + self.blocks(el, 0) + " "
        return anchor + self.inline_children(el)


# text at the start of a line that a reader would take for a list, heading or setext underline
_LINE_START = re.compile(r"^(?:(\d{1,9})([.)])|([-+#=]))(?=\s|$)")


def _wrap_lines(s: str) -> str:
    return "\n".join(_LINE_START.sub(lambda m: f"{m[1]}\\{m[2]}" if m[1] else f"\\{m[3]}", line.strip())
                     for line in s.split("\n"))


def book_to_markdown(book: Book, image_dir: str = "images") -> tuple[str, dict[str, str]]:
    """(Markdown text, {image href: name used in the text})."""
    w = _Writer(book, image_dir)
    parts = []
    md = book.metadata
    front = ["---", f"title: {_yaml(md.title)}"]
    if md.authors:
        front.append(f"author: {_yaml(', '.join(md.authors))}")
    if md.language:
        front.append(f"lang: {md.language}")
    if md.date:
        front.append(f"date: {_yaml(md.date)}")
    front.append("---")
    parts.append("\n".join(front))
    for href, _ in book.documents():
        doc = w.document(href)
        if doc.strip():
            parts.append(doc)
    if w.note_texts:
        parts.append(w.footnotes())
    text = "\n\n".join(parts) + "\n"
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, w.used_images


def _yaml(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r"[:#\"'{}\[\],&*!|>%@`]", s) else s


def write(book: Book, path, **_) -> None:
    text, _images = book_to_markdown(book)
    Path(path).write_text(text, encoding="utf-8", newline="\n")
