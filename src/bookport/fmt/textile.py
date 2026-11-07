"""Textile (as described on textile-lang.com): a reader and a writer.

Read: block signatures (h1-h6, p, bq, bc, pre, fnN, notextile, ### comments) with their attributes
``(class#id){style}[lang]`` and alignment, extended blocks (``..``), lists (* # and mixed, with
start numbers), definition lists (``- term := definition``), tables (headers ``_.``, spans ``\\2``
``/3``, alignment), phrase modifiers, links and link aliases, images, footnotes, acronyms,
``==notextile==``, HTML passthrough, and the glyph rules (quotes, dashes, ellipsis, (c) (r) (tm), x).
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, is_external
from ..textwriter import Table, TextWriter, write_text
from .mediawiki import attr, clean_input, esc, html_book, local_picture, note_aside, note_call

MAX_NEST = 16

_ATTRS = r"(?:\([^()\n]{0,80}\)|\{[^{}\n]{0,200}\}|\[[^\[\]\n]{0,40}\]|<>|[<>=~^-]|\(+|\)+){0,8}"
_SIG = re.compile(rf"^(h[1-6]|p|bq|bc|pre|fn\d{{1,4}}\^?|###|notextile|clear|div)({_ATTRS})(\.\.?)(?::(\S+))?(?: (.*)|$)")
_INLINE_ATTRS = r"(?:\([^()\s][^()\n]{0,80}\)|\{[^{}\n]{0,200}\}|\[[^\[\]\n]{0,40}\]){0,3}"
_TAGS_ONLY = re.compile(r"^\s*(?:</?(?:div|section|aside|article|header|footer|nav|main|figure|figcaption|table|thead|"
                        r"tbody|tfoot|tr|td|th|caption|colgroup|col|ul|ol|li|dl|dt|dd|blockquote|p|hr|br|details|"
                        r"summary|center|address|hgroup)\b[^<>]*>\s*)+$", re.IGNORECASE)
_RULE = re.compile(r"^(?:-{4,}|\*{3,}|_{3,})\s*$")
_LIST = re.compile(rf"^([*#]+)([_\d]{{0,4}})({_ATTRS}) (.*)$")
_DEF = re.compile(r"^- (.+?) :=(?: (.*)|$)")
_ALIAS = re.compile(r"^\[([^\[\]\n]{1,100})\](\S+)\s*$")
_TABLE_SIG = re.compile(rf"^table({_ATTRS})\.\s*$")
_ROW_ATTRS = re.compile(rf"^({_ATTRS})\. ?(?=\|)")
_HTML_BLOCK = re.compile(r"^<(/?)(div|table|ul|ol|dl|pre|blockquote|h[1-6]|p|notextile|hr|figure|section|address|"
                         r"center|form|aside|header|footer|nav|article|details)\b", re.IGNORECASE)
_INLINE_TAGS = {"b", "i", "u", "s", "strike", "del", "ins", "sup", "sub", "small", "big", "span", "code", "tt", "kbd",
                "var", "samp", "abbr", "acronym", "cite", "q", "em", "strong", "mark", "dfn", "bdi", "bdo", "br", "a",
                "img", "wbr"}
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)((?:[\s/][^<>]*)?)>")
_SPLIT = re.compile("\x00(\\d+)\x00")
_PHRASES = [("**", "b"), ("__", "i"), ("??", "cite"), ("*", "strong"), ("_", "em"), ("-", "del"), ("+", "ins"),
            ("^", "sup"), ("~", "sub"), ("%", "span")]
_URL_END = re.compile(r"[.,;:!?'\")\]]+$")


def _parse_attrs(s: str) -> dict[str, str]:
    """``(class#id){style}[lang]`` and alignment marks, as HTML attributes."""
    out: dict[str, str] = {}
    styles = []
    for m in re.finditer(r"\(([^()]*)\)|\{([^{}]*)\}|\[([^\[\]]*)\]|(<>|[<>=])", s or ""):
        if m.group(1) is not None:
            cls, _, ident = m.group(1).partition("#")
            if cls.strip():
                out["class"] = cls.strip()
            if ident.strip():
                out["id"] = re.sub(r"[^\w.:-]", "", ident.strip())
        elif m.group(2) is not None:
            styles.append(m.group(2).strip().rstrip(";"))
        elif m.group(3) is not None:
            out["lang"] = m.group(3).strip()
        elif m.group(4):
            styles.append("text-align: " + {"<": "left", ">": "right", "=": "center", "<>": "justify"}[m.group(4)])
    if styles:
        out["style"] = "; ".join(s for s in styles if s)
    return out


def _attr_html(a: dict[str, str], keep=("class", "id", "style", "lang")) -> str:
    return "".join(f' {k}="{attr(v)}"' for k, v in a.items() if k in keep and v)


class _Textile:
    def __init__(self, base_dir: Path | None):
        self.base = base_dir
        self.slots: list[str] = []
        self.aliases: dict[str, str] = {}
        self.notes: dict[str, str] = {}  # footnote number -> its html
        self.note_order: list[str] = []
        self.defined: set[str] = set()  # the footnote numbers that have a fnN. block
        self.lost: list[str] = []
        self.missing = 0

    def keep(self, html_: str) -> str:
        self.slots.append(html_)
        return f"\x00{len(self.slots) - 1}\x00"

    # --- blocks ---------------------------------------------------------------------------------
    def blocks(self, text: str, depth: int = 0) -> str:
        lines = text.split("\n")
        out: list[str] = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            m = _SIG.match(line)
            if m:
                i = self.signature(m, lines, i, out, depth)
                continue
            if _TABLE_SIG.match(line) or line.lstrip().startswith("|") or _ROW_ATTRS.match(line):
                i = self.table(lines, i, out)
                continue
            if _LIST.match(line):
                i = self.list(lines, i, out)
                continue
            if _DEF.match(line):
                i = self.deflist(lines, i, out)
                continue
            h = _HTML_BLOCK.match(line)
            if h:
                i = self.html_block(lines, i, out, h.group(2).lower(), depth)
                continue
            if _RULE.match(line):
                out.append("<hr/>")
                i += 1
                continue
            body = []
            while i < n and lines[i].strip():
                # a list, table or HTML block may start right under a paragraph's last line
                if body and (_LIST.match(lines[i]) or _HTML_BLOCK.match(lines[i]) or lines[i].startswith("|")):
                    break
                body.append(lines[i])
                i += 1
            out.append(f"<p>{self.inline(chr(10).join(body))}</p>")
        return "".join(out)

    def signature(self, m: re.Match, lines: list[str], i: int, out: list[str], depth: int) -> int:
        kind, attrs, dots, cite, first = m.group(1), _parse_attrs(m.group(2)), m.group(3), m.group(4), m.group(5) or ""
        extended = dots == ".."
        body = [first]
        i += 1
        n = len(lines)
        while i < n:
            if not lines[i].strip():
                if not extended:
                    break
                # an extended block runs on over blank lines, up to the next signature
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j >= n or _SIG.match(lines[j]):
                    i = j
                    break
                body.extend(lines[i:j])
                i = j
                continue
            if extended and _SIG.match(lines[i]):
                break  # an extended block ends at the next signature
            body.append(lines[i])
            i += 1
        text = "\n".join(body)
        a = _attr_html(attrs)
        if kind == "###":
            return i
        if kind in ("bc", "pre"):
            lang = attrs.get("class", "") if kind == "bc" else ""
            lang = lang.removeprefix("language-").split()[0] if lang else ""
            code = text.strip("\n")
            cls = f' class="language-{attr(lang)}"' if lang else ""
            out.append(f'<pre{_attr_html(attrs, ("id",))}><code{cls}>{esc(code)}</code></pre>' if kind == "bc"
                       else f"<pre{a}>{esc(code)}</pre>")
            return i
        if kind == "notextile":
            out.append(text)
            return i
        paras = [p for p in re.split(r"\n\s*\n", text)] if extended else [text]
        if kind.startswith("fn"):
            num = kind[2:].rstrip("^")
            self.notes[num] = "".join(f"<p>{self.inline(p)}</p>" for p in paras if p.strip())
            return i
        if kind[0] == "h" and kind != "hr":
            out.append(f"<{kind}{a}>{self.inline(text)}</{kind}>")
            return i
        inner = "".join(f"<p{a if kind == 'p' else ''}>{self.inline(p)}</p>" for p in paras if p.strip())
        if kind == "bq":
            cite_attr = f' cite="{attr(cite)}"' if cite else ""
            out.append(f"<blockquote{a}{cite_attr}>{inner}</blockquote>")
        elif kind == "div":
            out.append(f"<div{a}>{inner}</div>")
        else:
            out.append(inner)
        return i

    def list(self, lines: list[str], i: int, out: list[str]) -> int:
        stack: list[str] = []  # the marks of the open lists
        parts: list[str] = []
        n = len(lines)
        while i < n:
            m = _LIST.match(lines[i])
            if not m:
                if lines[i].strip() and not _SIG.match(lines[i]) and parts and not lines[i].startswith(("|", "- ")):
                    parts.append("<br/>" + self.inline(lines[i]))  # a line that continues the item
                    i += 1
                    continue
                break
            marks, start, text = m.group(1)[:MAX_NEST], m.group(2), m.group(4)
            depth = len(marks)
            while len(stack) > depth or stack and stack[-1] != marks[len(stack) - 1]:
                c = stack.pop()
                parts.append(f"</li></{'ol' if c == '#' else 'ul'}>")
            if len(stack) == depth:
                parts.append("</li><li>")
            else:
                while len(stack) < depth:
                    c = marks[len(stack)]
                    tag = "ol" if c == "#" else "ul"
                    st = f' start="{int(start)}"' if tag == "ol" and start.isdigit() and len(stack) == depth - 1 else ""
                    parts.append(f"<{tag}{st}><li>")
                    stack.append(c)
            parts.append(self.inline(text))
            i += 1
        while stack:
            c = stack.pop()
            parts.append(f"</li></{'ol' if c == '#' else 'ul'}>")
        out.append("".join(parts))
        return i

    def deflist(self, lines: list[str], i: int, out: list[str]) -> int:
        parts = ["<dl>"]
        n = len(lines)
        while i < n:
            m = _DEF.match(lines[i])
            if not m:
                break
            parts.append(f"<dt>{self.inline(m.group(1).strip())}</dt>")
            i += 1
            definition = m.group(2)
            if definition is not None and definition.strip().endswith("=:"):
                parts.append(f"<dd>{self.inline(definition.strip()[:-2].strip())}</dd>")
                continue
            body = [definition or ""]
            if definition is None or not definition.strip() or i < n and not _DEF.match(lines[i]) and lines[i].strip():
                while i < n and not _DEF.match(lines[i]) and lines[i].strip() != "=:" and (lines[i].strip() or definition is None):
                    if lines[i].rstrip().endswith("=:"):
                        body.append(lines[i].rstrip()[:-2])
                        i += 1
                        break
                    body.append(lines[i])
                    i += 1
                if i < n and lines[i].strip() == "=:":
                    i += 1
            text = "\n".join(body).strip()
            paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
            parts.append("<dd>" + ("".join(f"<p>{self.inline(p)}</p>" for p in paras) if len(paras) > 1
                                   else self.inline(text)) + "</dd>")
        parts.append("</dl>")
        out.append("".join(parts))
        return i

    def table(self, lines: list[str], i: int, out: list[str]) -> int:
        n = len(lines)
        tattrs = ""
        m = _TABLE_SIG.match(lines[i])
        if m:
            tattrs = _attr_html(_parse_attrs(m.group(1)), ("class", "id"))
            i += 1
        rows: list[str] = []
        head_rows: list[str] = []
        caption = ""
        pending = ""
        while i < n:
            line = lines[i].strip()
            if not line:
                break
            rm = _ROW_ATTRS.match(line)
            if rm:
                line = line[rm.end():]
            if not line.startswith("|"):
                break
            i += 1
            if line.startswith("|=.") and not rows and not head_rows:
                caption = self.inline(line[3:].strip().rstrip("|").strip())
                continue
            # a row may go on over more lines until it ends with |
            while not line.rstrip().endswith("|") and i < n and lines[i].strip():
                line += "\n" + lines[i]
                i += 1
            line = pending + line
            cells = _cells(line.strip()[1:])
            row = []
            all_head = True
            for cell in cells:
                cm = re.match(r"^((?:_|\\\d+|/\d+|[<>=^~]|<>|\([^()]*\)|\{[^{}]*\}|\[[^\[\]]*\])*)\. (.*)$", cell, re.DOTALL)
                spec, content = (cm.group(1), cm.group(2)) if cm else ("", cell)
                head = "_" in spec
                all_head &= head
                a = []
                cs = re.search(r"\\(\d+)", spec)
                rs = re.search(r"/(\d+)", spec)
                if cs and int(cs.group(1)) > 1:
                    a.append(f' colspan="{min(int(cs.group(1)), 50)}"')
                if rs and int(rs.group(1)) > 1:
                    a.append(f' rowspan="{min(int(rs.group(1)), 500)}"')
                al = re.search(r"<>|[<>=]", re.sub(r"\([^()]*\)|\{[^{}]*\}|\[[^\[\]]*\]", "", spec))
                if al:
                    a.append(f' style="text-align: {({"<": "left", ">": "right", "=": "center", "<>": "justify"})[al.group(0)]}"')
                tag = "th" if head else "td"
                row.append(f"<{tag}{''.join(a)}>{self.inline(content.strip())}</{tag}>")
            html_row = "<tr>" + "".join(row) + "</tr>"
            if all_head and not rows and row:
                head_rows.append(html_row)
            else:
                rows.append(html_row)
        table = [f"<table{tattrs}>"]
        if caption:
            table.append(f"<caption>{caption}</caption>")
        if head_rows:
            table.append("<thead>" + "".join(head_rows) + "</thead>")
        if rows:
            table.append("<tbody>" + "".join(rows) + "</tbody>")
        table.append("</table>")
        out.append("".join(table))
        return i

    def html_block(self, lines: list[str], i: int, out: list[str], tag: str, nest: int = 0) -> int:
        n = len(lines)
        body = []
        if tag in ("pre", "notextile"):
            end = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
            while i < n:
                body.append(lines[i])
                i += 1
                if end.search(body[-1]):
                    break
            text = "\n".join(body)
            if tag == "pre":
                text = re.sub(r"(<pre\b[^>]*>)\n", r"\1", text, count=1)  # as HTML does, a first line break is no line
            if tag == "notextile":
                text = re.sub(r"</?notextile\s*>", "", text, flags=re.IGNORECASE).strip("\n")
                if text and not _HTML_BLOCK.match(text):
                    text = f"<p>{text}</p>"  # protected text is still a paragraph
            out.append(text)
            return i
        # the element runs to its own end tag (blank lines included); a lone end tag just closes
        tags = re.compile(rf"<(/?){tag}\b[^>]*?(/?)>", re.IGNORECASE)
        depth = 0
        while i < n:
            line = lines[i]
            if not line.strip() and depth <= 0:
                break
            body.append(line)
            i += 1
            for m in tags.finditer(line):
                depth += -1 if m.group(1) else 0 if m.group(2) or tag == "hr" else 1
            if depth <= 0 and body:
                break
        # the HTML stays as written; lines of only tags pass through, the Textile between them is read as
        # blocks (or as inline text when it starts on a line with a tag, like <li>text)
        segment: list[str] = []

        def flush() -> None:
            if "".join(segment).strip():
                text = "\n".join(segment).strip("\n")
                if text.lstrip().startswith("<") or nest >= MAX_NEST:
                    out.append("\n".join(self.inline(c, raw_tags=True) for c in re.split(r"\n\s*\n", text)))
                else:
                    out.append(self.blocks(text, nest + 1))
            segment.clear()

        opened = 0
        for line in body:
            if _TAGS_ONLY.match(line):
                flush()
                change = len(re.findall(r"<[A-Za-z]", line)) - len(re.findall(r"</", line))
                if change <= 0 or opened + change <= 4 * MAX_NEST:  # past that, more opening tags are left out
                    opened += change
                    out.append(line.strip())
            else:
                segment.append(line)
        flush()
        return i

    # --- inline ---------------------------------------------------------------------------------
    def inline(self, text: str, raw_tags: bool = False, depth: int = 0) -> str:
        if depth > MAX_NEST:
            return esc(text)
        keep = self.keep
        if "==" in text:
            text = re.sub(r"(?<!=)==(?!=)(.{1,2000}?)(?<!=)==(?!=)", lambda m: keep(esc(m.group(1))), text, flags=re.DOTALL)
        if "<notextile>" in text:
            text = re.sub(r"<notextile>(.*?)</notextile>", lambda m: keep(m.group(1)), text, flags=re.DOTALL)
        if "@" in text:
            text = re.sub(r"(?<![\w@])@(?!\s)([^@\n]{1,1000}?)(?<!\s)@(?![\w@])", lambda m: keep(f"<code>{esc(m.group(1))}</code>"),
                          text)
            text = re.sub(r"\[@([^@\n]{1,1000}?)@\]", lambda m: keep(f"<code>{esc(m.group(1))}</code>"), text)
        if "<" in text:
            text = _TAG.sub(lambda m: self._tag(m, raw_tags), text)
        if "!" in text:
            text = re.sub(rf"!({_ATTRS})([^\s!()]+)(?:\s+\d+x\d+)?(?:\(([^()]*)\))?!(?::(\S+))?", self._image, text)
        if '"' in text:
            text = re.sub(r'\["([^"\n]{1,500})":([^\]\s]+)\]', lambda m: self._link(m.group(1), m.group(2), depth), text)
            text = re.sub(r'"([^"\n]{1,500})":(\S+)', lambda m: self._link_trail(m, depth), text)
        if "[" in text:
            text = re.sub(r"(?<=\S)\[(\d{1,4})\]", self._noteref, text)
        text = re.sub(r"\b([A-Z][A-Z0-9]{1,15})\(([^()\n]{1,200})\)", lambda m: keep(
            f'<abbr title="{attr(m.group(2))}">{esc(m.group(1))}</abbr>'), text)
        for mark, tag in _PHRASES:
            if mark in text:
                text = self._phrase(text, mark, tag, depth)
        text = _glyphs(text)
        text = text.replace("\n", "\x00br\x00")
        out = []
        for k, part in enumerate(re.split("\x00(\\d+|br)\x00", text)):
            if k % 2 == 0:
                out.append(esc(html.unescape(part)) if "&" in part else esc(part))
            elif part == "br":
                out.append("<br/>")
            else:
                out.append(self.slots[int(part)])
        s = "".join(out)
        # placeholders made while reading a placeholder (formatting inside a link) are filled in too
        for _ in range(4):
            if "\x00" not in s:
                break
            s = _SPLIT.sub(lambda m: self.slots[int(m.group(1))], s)
        return s

    def _tag(self, m: re.Match, raw_tags: bool) -> str:
        name = m.group(2).lower()
        if raw_tags or name in _INLINE_TAGS:
            return self.keep(m.group(0))
        return m.group(0)

    def _phrase(self, text: str, mark: str, tag: str, depth: int) -> str:
        q = re.escape(mark)
        inner = rf"({_INLINE_ATTRS})(?=\S)(.{{1,1000}}?)(?<=\S)"

        def sub(m: re.Match) -> str:
            a = _attr_html(_parse_attrs(m.group(1))) if m.group(1) else ""
            content = m.group(2)
            body = self.inline(content, depth=depth + 1) if tag not in ("code",) else esc(content)
            return self.keep(f"<{tag}{a}>{body}</{tag}>")

        text = re.sub(rf"\[{q}{inner}{q}\]", sub, text, flags=re.DOTALL)
        if mark == "-":
            pattern = rf"(?<![^\W_])(?<!-)-(?![\s\-]){inner}-(?![^\W_])(?!-)"
        else:
            pattern = rf"(?<![^\W_])(?<!{q}){q}(?!\s){inner}{q}(?![^\W_])(?!{q})"
        return re.sub(pattern, sub, text, flags=re.DOTALL)

    def _image(self, m: re.Match) -> str:
        a, src, alt, link = _parse_attrs(m.group(1)), m.group(2), m.group(3) or "", m.group(4)
        local = None if is_external(src) else local_picture(self.base, html.unescape(src), self.lost)
        if local is None and not is_external(src):
            self.missing += 1
            return self.keep(esc(alt)) if alt else ""
        img = f'<img src="{attr(local or src)}" alt="{attr(alt)}"{_attr_html(a, ("class", "id"))}/>'
        if link:
            trail = _URL_END.search(link)
            tail = trail.group(0) if trail else ""
            link = link[:len(link) - len(tail)]
            return self.keep(f'<a href="{attr(self.aliases.get(link, link))}">{img}</a>') + tail
        return self.keep(img)

    def _link_trail(self, m: re.Match, depth: int) -> str:
        url = m.group(2)
        trail = ""
        note = re.search(r"\[\d{1,4}\]$", url)
        if note:  # a footnote call right after the address
            trail, url = note.group(0), url[:note.start()]
        # punctuation after the address ends the sentence, not the link (a closing parenthesis only if unopened)
        while url and url[-1] in ".,;:!?'\"]" or url.endswith(")") and url.count("(") < url.count(")"):
            trail = url[-1] + trail
            url = url[:-1]
        return self._link(m.group(1), url, depth) + trail

    def _link(self, text: str, url: str, depth: int) -> str:
        title = ""
        tm = re.match(r"^(.*?)\s*\(([^()]*)\)$", text, re.DOTALL)
        if tm and tm.group(1):
            text, title = tm.group(1), tm.group(2)
        url = self.aliases.get(url, url)
        am = re.match(_INLINE_ATTRS, text)
        cls = ""
        if am and am.group(0) and text[am.end():]:
            cls = _attr_html(_parse_attrs(am.group(0)), ("class", "id", "lang"))  # "(class)text":url
            text = text[am.end():]
        if text == "$":
            text = html.unescape(url).removeprefix("mailto:")  # "$":url shows the address
        t = f' title="{attr(title)}"' if title else ""
        return self.keep(f'<a href="{attr(html.unescape(url))}"{t}{cls}>{self.inline(text, depth=depth + 1)}</a>')

    def _noteref(self, m: re.Match) -> str:
        num = m.group(1)
        if num not in self.defined:
            return m.group(0)  # brackets with a number but no fnN. block are just text
        if num not in self.note_order:
            self.note_order.append(num)
        return self.keep(note_call(int(num), f"fn{num}"))


def _cells(row: str) -> list[str]:
    """A table row's cells (the row starts after its first |), | inside @code@ or ==...== kept."""
    cells, cur, i, n = [], [], 0, len(row)
    while i < n:
        c = row[i]
        if c in "@=" and (c != "=" or row.startswith("==", i)):
            close = row.find("==" if c == "=" else "@", i + (2 if c == "=" else 1))
            if close > 0:
                end = close + (2 if c == "=" else 1)
                cur.append(row[i:end])
                i = end
                continue
        if c == "|":
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        cells.append("".join(cur))
    return cells


def _glyphs(text: str) -> str:
    """Textile's typographic conversions, on text outside placeholders."""
    if "--" in text:
        text = re.sub(r"(?<=\S)--(?=\S)|(?<=\s)--(?=\s)", "\N{EM DASH}", text)
    text = re.sub(r"(?<=\s)-(?=\s)", "\N{EN DASH}", text)
    text = text.replace("...", "\N{HORIZONTAL ELLIPSIS}")
    if "(" in text:
        text = re.sub(r"\((?:c|C)\)", "\N{COPYRIGHT SIGN}", text)
        text = re.sub(r"\((?:r|R)\)", "\N{REGISTERED SIGN}", text)
        text = re.sub(r"\((?:tm|TM)\)", "\N{TRADE MARK SIGN}", text)
    text = re.sub(r"(?<=\d) ?x ?(?=\d)", "\N{MULTIPLICATION SIGN}", text)
    if '"' in text:
        text = re.sub(r'(^|[\s(\[{\x00-])"(?=\S)', "\\1\N{LEFT DOUBLE QUOTATION MARK}", text)
        text = text.replace('"', "\N{RIGHT DOUBLE QUOTATION MARK}")
    if "'" in text:
        text = re.sub(r"(^|[\s(\[{\x00-])'(?=\S)", "\\1\N{LEFT SINGLE QUOTATION MARK}", text)
        text = text.replace("'", "\N{RIGHT SINGLE QUOTATION MARK}")
    return text


def to_html(text: str, base_dir: Path | None = None) -> tuple[str, list[str]]:
    t = _Textile(base_dir)
    lines = []
    for line in clean_input(text).split("\n"):
        m = _ALIAS.match(line)
        if m and not line.startswith("[[") and re.match(r"^[a-zA-Z][\w+.-]*:|^[./#]", m.group(2)):
            t.aliases[m.group(1)] = m.group(2)
            continue
        lines.append(line)
    t.defined = {m.group(1) for m in re.finditer(r"(?m)^fn(\d{1,4})", "\n".join(lines))}
    body = t.blocks("\n".join(lines))
    for num in t.note_order:
        if num in t.notes:
            body += note_aside(f"fn{num}", t.notes[num])
    for num, inner in t.notes.items():
        if num not in t.note_order:
            body += note_aside(f"fn{num}", inner)  # a footnote nothing calls is still text of the page
    if t.missing:
        t.lost.append(f"{t.missing} picture{'s' if t.missing != 1 else ''} named in the text aren't beside it, "
                      "so only their descriptions were kept")
    return body, t.lost


def read(path) -> Book:
    p = Path(path)
    body, lost = to_html(markup.decode(p.read_bytes()), p.parent)
    book = html_book(body, p.stem.replace("_", " "), "textile", p.parent, lost)
    first = book.tree(book.spine[0]).find(".//h1")
    if first is not None and markup.text_of(first).strip():
        book.metadata.title = " ".join(markup.text_of(first).split())
    return book


# --- writing -------------------------------------------------------------------------------------

_LINE_START = re.compile(r"^(?:[*#]+ |- |\||h[1-6]\.|p\.|bq\.|bc\.|pre\.|fn\d|###\.|notextile\.|table\.|<)")


class _Writer(TextWriter):
    def __init__(self, book: Book):
        super().__init__(book)
        self.note_texts: list[str] = []

    def escape(self, s: str) -> str:
        # only what Textile would read as markup is escaped: an entity, a tag, the phrase and link marks
        s = re.sub(r"&(?=#?\w+;)", "&amp;", s)
        s = re.sub(r"<(?=[A-Za-z/!?])", "&lt;", s)
        s = re.sub(r"[*_^~@%!|\[\]=+]", lambda m: f"&#{ord(m.group(0))};", s)
        s = re.sub(r"(?<![\w-])-(?=\S)|(?<=\S)-(?![\w-])", "&#45;", s)
        return s.replace('":', "&#34;:")  # only "text": could start a link

    def join_document(self, parts: list[str]) -> str:
        text = super().join_document(parts)
        if self.note_texts:
            text += "\n" + "\n\n".join(f"fn{k}. {body}" for k, body in enumerate(self.note_texts, 1)) + "\n"
        return text

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        text = text.strip()
        if anchor:
            return f"p(#{anchor}). {text}"
        return f"p. {text}" if _LINE_START.match(text) else text

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        return f"h{min(max(level, 1), 6)}{f'(#{anchor})' if anchor else ''}. {text}"

    def rule(self) -> str:
        return "<hr />"

    def code_block(self, code: str, lang: str) -> str:
        if "\n\n" in code or code.startswith(" ") or not code.strip():
            # a bc. block would end at the blank line; in <pre> only what reads as a tag or an entity is escaped
            code = re.sub(r"<(?=[A-Za-z/!?])", "&lt;", re.sub(r"&(?=#?\w+;)", "&amp;", code))
            return f"<pre>\n{code}\n</pre>"
        return f"bc{f'({lang})' if lang else ''}. {code}"

    def raw_block(self, text: str, fmt: str) -> str:
        return text if fmt == "textile" else self.code_block(text, "")

    def raw_inline(self, text: str, fmt: str) -> str:
        return text if fmt == "textile" else self.code(text)

    def quote(self, inner: str) -> str:
        paras = [p for p in inner.split("\n\n") if p.strip()]
        if len(paras) == 1 and not _LINE_START.match(paras[0]) and "\n" not in paras[0]:
            return f"bq. {paras[0]}"
        # more than one paragraph: an HTML quote with Textile inside (bq.. is not read everywhere)
        return "<blockquote>\n\n" + inner + "\n\n</blockquote>"

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        mark = "#" if ordered else "*"
        out = []
        for k, it in enumerate(items):
            lines = [ln for ln in it.split("\n") if ln.strip()]
            first, more = (lines[0] if lines else ""), lines[1:]
            lead = f"{mark}{start}" if ordered and k == 0 and start != 1 else mark
            if first[:1] in "*#" and re.match(r"^[*#]+\d* ", first):  # an item that is only a nested list
                out.append(f"{lead} ")
                more = lines
            else:
                out.append(f"{lead} {first}".rstrip())
            for line in more:
                if re.match(r"^[*#]+\d* ", line):
                    out.append(mark + line)
                else:
                    out[-1] += "<br />" + line
        return "\n".join(out)

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        out = []
        for term, defs in pairs:
            d = "\n\n".join(defs)
            if "\n" in d:
                out.append(f"- {term} :=\n{d} =:")
            else:
                out.append(f"- {term} := {d}")
        return "\n".join(out)

    def table(self, table: Table) -> str:
        out = []
        if table.caption:
            out.append(f"p. {table.caption}\n")
        for r, row in enumerate(table.rows):
            cells = []
            for c in row:
                spec = "_" if c.header or r < table.header_rows else ""
                if c.colspan > 1:
                    spec += f"\\{c.colspan}"
                if c.rowspan > 1:
                    spec += f"/{c.rowspan}"
                spec += {"left": "<", "right": ">", "center": "="}.get(c.align, "")
                text = " ".join(c.text.split("\n")).strip() if "\n" not in c.text.strip() else "<br />".join(
                    ln.strip() for ln in c.text.strip().split("\n") if ln.strip())
                cells.append(f"{spec}. {text}" if spec else f" {text}")
            out.append("|" + "|".join(cells) + "|")
        return "\n".join(out)

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        img = self.image(src, alt or caption) if src else ""
        return img + (f"\n{caption}" if caption else "")

    def image(self, src: str, alt: str) -> str:
        alt = re.sub(r"[()!]", "", alt)
        return f"!{src}({alt})!" if alt else f"!{src}!"

    def math_block(self, el: ET.Element) -> str:
        return f"==$${mathml.to_tex(el)}$$=="

    def math_inline(self, el: ET.Element) -> str:
        return f"==${mathml.to_tex(el)}$=="

    def footnote(self, body: str, note_id: str) -> str:
        self.note_texts.append(" ".join(body.split()))
        return f"[{len(self.note_texts)}]"

    def line_break(self) -> str:
        return "\n"

    def emph(self, s: str) -> str:
        return f"_{s}_"

    def strong(self, s: str) -> str:
        return f"*{s}*"

    def strike(self, s: str) -> str:
        return f"-{s}-"

    def underline(self, s: str) -> str:
        return f"+{s}+"

    def sup(self, s: str) -> str:
        return f"[^{s}^]"  # bracketed: these sit inside words (x[^2^], H[~2~]O)

    def sub(self, s: str) -> str:
        return f"[~{s}~]"

    def smallcaps(self, s: str) -> str:
        return f"%{{font-variant:small-caps}}{s}%"

    def code(self, s: str) -> str:
        if "@" in s or "\n" in s:
            return f"<code>{html.escape(s, quote=False)}</code>"
        return f"@{s}@"

    def inline_anchor(self, key: str) -> str:
        return f'<a id="{key}"></a>'

    def link(self, target: str, text: str, external: bool) -> str:
        url = target if external else f"#{target}"
        return f'"{(text or url).replace(chr(34), "&#34;")}":{url}'


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    return write_text(_Writer(book), path, pictures)
