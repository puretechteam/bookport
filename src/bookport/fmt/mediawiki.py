"""MediaWiki markup (the wikitext of a page, as Wikipedia stores it): a reader and a writer.

Reading follows the markup described on mediawiki.org: headings, bold and italic quote runs, lists,
indents and definition lists, tables, pre blocks, internal and external links, pictures, ``<ref>``
footnotes, ``<math>``, ``<nowiki>``, ``<syntaxhighlight>`` and the HTML tags MediaWiki allows.
Templates (``{{...}}``) are not expanded: like Pandoc, the reader leaves them out and says how many
in ``book.lost``. Pictures named in ``[[File:...]]`` are taken from beside the page when they are
there; otherwise their captions are kept as text.

The helpers ``html_book``, ``note_call``, ``math_html`` and ``esc`` are shared by the other
markup readers in this package (Textile, man).
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, Metadata, is_external, resolve
from ..limits import safe_path
from ..textwriter import Table, TextWriter, write_text
from .markdown import _pull_images

MAX_NEST = 24  # quotes, refs, tables and lists inside each other, deeper than any real page
MAX_LIST = 48


# --- helpers shared by the markup readers ------------------------------------------------------

def esc(s: str) -> str:
    return html.escape(s, quote=False)


def attr(s: str) -> str:
    return html.escape(s, quote=True)


def note_call(num: int, nid: str) -> str:
    return f'<a class="noteref" epub:type="noteref" href="#{attr(nid)}"><sup>{num}</sup></a>'


def note_aside(nid: str, body: str) -> str:
    return f'<aside epub:type="footnote" id="{attr(nid)}">{body}</aside>'


def math_html(tex: str, display: bool) -> str:
    return markup.to_string(mathml.from_tex(tex, display=display))


_BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_input(text: str) -> str:
    """Line ends as \\n, no NUL or other control characters (they can't go in XHTML)."""
    return _BAD_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\N{BYTE ORDER MARK}"))


def html_book(body: str, title: str, fmt: str, base_dir: Path | None, lost: list[str] | None = None,
              meta: Metadata | None = None) -> Book:
    """A one-document book from an XHTML body (notes as asides inside it); local pictures pulled in."""
    md = meta or Metadata()
    md.title = md.title or title
    md.language = md.language or "en"
    book = Book(md)
    book.source_format = fmt
    if not body.strip():
        body = "<p></p>"
    doc = (f'<html xmlns:epub="{markup.EPUB_NS}"><head><title>{esc(md.title)}</title></head>'
           f"<body>{body}</body></html>")
    root = markup.parse(doc)
    href = "text/part0001.xhtml"
    if base_dir is not None:
        _pull_images(book, root, href, base_dir, {})
    book.add_document(href, root)
    book.lost.extend(lost or [])
    book.ensure_toc()
    return book


def local_picture(base_dir: Path | None, name: str, lost: list[str]) -> str | None:
    """The link to use for a picture beside the document, or None when it isn't there (or isn't allowed)."""
    if base_dir is None or not name or is_external(name) or name.startswith("data:"):
        return None
    tried = [name]
    if " " in name:
        tried.append(name.replace(" ", "_"))
    elif "_" in name:
        tried.append(name.replace("_", " "))
    for cand in tried:
        p = safe_path(base_dir, cand)
        if p is None:
            msg = f"A picture outside the document's folder was left out: {name}"
            if msg not in lost:
                lost.append(msg)
            return None
        try:
            if p.is_file():
                return cand
        except OSError:
            return None
    return None


# --- reading -----------------------------------------------------------------------------------

_MARK = re.compile("\x00(\\d+)\x00")
_SPLIT_MARK = re.compile("\x00(\\d+)\x00")
# tags whose content isn't wikitext (or is parsed on its own), and HTML blocks that nest
_EXT = ("nowiki|pre|math|chem|ce|syntaxhighlight|source|ref|references|gallery|poem|blockquote|table|ul|ol|dl|"
        "score|timeline|graph|mapframe|maplink|templatedata|imagemap|hiero|inputbox|categorytree|templatestyles|"
        "section|indicator|includeonly|charinsert|langconvert|phonos|ipa")
_OPEN = re.compile(rf"<!--|<({_EXT})(?=[\s/>])([^>]*?)(/?)>", re.IGNORECASE)
_NESTING = {"blockquote", "table", "ul", "ol", "dl"}
_UNKNOWN_EXT = {"score", "timeline", "graph", "mapframe", "maplink", "imagemap", "hiero", "inputbox",
                "categorytree", "langconvert", "phonos"}
_SILENT = {"templatestyles", "section", "indicator", "includeonly", "templatedata"}
_BLOCK_KINDS = {"pre", "source", "gallery", "poem", "bq", "html", "raw", "fig", "mathb"}
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))""")
_MAGIC = re.compile(r"__(?:NOTOC|FORCETOC|TOC|NOEDITSECTION|NEWSECTIONLINK|NONEWSECTIONLINK|NOGALLERY|HIDDENCAT|"
                    r"INDEX|NOINDEX|STATICREDIRECT|NOTITLECONVERT|NOTC|NOCONTENTCONVERT|NOCC|DISAMBIG|"
                    r"EXPECTUNUSEDCATEGORY|EXPECTUNUSEDTEMPLATE|NOGLOBAL|ARCHIVEDTALK|NOTALK|NOEXTERNALLANGLINKS)__")
_BRACES = re.compile(r"\{{2,}|\}{2,}")
_ILINK = re.compile(r"\[\[([^\[\]\n]*)\]\]([a-z]+)?")
_ELINK = re.compile(r"\[((?:https?:|ftp:|ftps:|mailto:|news:|irc:|ircs:|gopher:|sftp:|ssh:|telnet:|git:|svn:|//)"
                    r"[^\s\[\]<>\"\x00]+)(?:[ \t]+([^\]\n]*))?\]", re.IGNORECASE)
_BARE = re.compile(r"\b(?:https?|ftp)://[^\s<>\[\]{}\"|\x00-\x08]+")
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)((?:[\s/][^<>]*)?)>")
_BLOCKISH = re.compile(r"(?:</?(?:div|center|p)\b[^<>]*>|<hr\b[^<>]*>)[ \t]*", re.IGNORECASE)
_INLINE_TAGS = {"b", "i", "u", "s", "strike", "del", "ins", "sup", "sub", "small", "big", "span", "code", "tt", "kbd",
                "var", "samp", "abbr", "cite", "q", "em", "strong", "font", "mark", "dfn", "bdi", "bdo", "ruby", "rt",
                "rp", "rb", "time", "data", "wbr", "br"}
_KEEP_ATTRS = {"title", "lang", "dir", "class", "style"}
_FILE_NS = {"file", "image", "media", "archivo", "datei", "fichier", "bild", "imagen", "immagine", "ficheiro",
            "plik", "bestand", "arquivo", "fil", "файл", "ファイル", "文件", "画像"}
_IMG_OPTS = re.compile(r"^(?:thumb|thumbnail|frame|framed|frameless|border|left|right|center|centre|none|upright"
                       r"|baseline|middle|sub|super|text-top|text-bottom|top|bottom|loop|muted|\d*x?\d+\s*px"
                       r"|(?:upright|link|alt|page|class|lang|start|end|thumbtime|alt)\s*=.*)$", re.IGNORECASE)
_STRUCT = re.compile(r"<(/?)(table|thead|tbody|tfoot|tr|td|th|caption|ul|ol|li|dl|dt|dd)\b([^<>]*)>", re.IGNORECASE)
_CELLISH = {"td", "th", "li", "dt", "dd", "caption"}


def _attrs(s: str) -> dict[str, str]:
    out = {}
    for m in _ATTR.finditer(s or ""):
        v = m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4)
        out[m.group(1).lower()] = html.unescape(v)
    return out


def _cell_attrs(a: dict[str, str]) -> str:
    out = []
    for k in ("colspan", "rowspan"):
        v = a.get(k, "").strip()
        if v.isdigit() and int(v) > 1:
            out.append(f' {k}="{min(int(v), 500)}"')
    align = a.get("align", "").strip().lower()
    m = re.search(r"text-align\s*:\s*(left|right|center|justify)", a.get("style", ""), re.IGNORECASE)
    align = align or (m.group(1).lower() if m else "")
    if align in ("left", "right", "center", "justify"):
        out.append(f' style="text-align: {align}"')
    return "".join(out)


class _Wiki:
    def __init__(self, base_dir: Path | None):
        self.base_dir = base_dir
        self.slots: list[tuple] = []  # placeholder payloads: (kind, raw text, *data)
        self.defs: dict[str, str] = {}  # named <ref> contents
        self.note_ids: dict[str, tuple[str, int]] = {}  # ref name -> (aside id, number)
        self.notes: list[tuple[str, str]] = []
        self.templates = 0
        self.lost: list[str] = []
        self.missing_pictures = 0
        self.unknown: dict[str, int] = {}
        self.ext_count = 0
        self.in_note = 0
        self.ids: set[str] = set()

    def keep(self, *slot) -> str:
        self.slots.append(slot)
        return f"\x00{len(self.slots) - 1}\x00"

    # --- preparing: comments, tags, templates, links -------------------------------------------
    def prepare(self, text: str) -> str:
        text = self.extract(text)
        if "{{" in text:
            text = text.replace("{{!}}", "|").replace("{{=}}", "=").replace("{{!!}}", "||")
            text = self.strip_templates(text)
        if "__" in text:
            text = _MAGIC.sub("", text)
        text = _BLOCKISH.sub(lambda m: "\n----\n" if m.group(0)[1:3].lower() == "hr" else "\n\n", text)
        if "[" in text:
            text = self.links(text)
        return text

    def extract(self, text: str) -> str:
        """Comments out; extension tags and HTML blocks into placeholders (their content is kept raw)."""
        out: list[str] = []
        pos = 0
        n = len(text)
        noclose: set[str] = set()
        pairs: dict[str, dict[int, tuple[int, int]]] = {}
        while pos < n:
            m = _OPEN.search(text, pos)
            if m is None:
                break
            start = m.start()
            if m.group(0) == "<!--":
                end = text.find("-->", start + 4)
                end = n if end < 0 else end + 3
                # a comment alone on its line takes the line with it
                ls = text.rfind("\n", 0, start) + 1
                le = text.find("\n", end)
                le = n if le < 0 else le
                if ls >= pos and not text[ls:start].strip(" \t") and not text[end:le].strip(" \t") and le < n:
                    out.append(text[pos:ls])
                    pos = le + 1
                else:
                    out.append(text[pos:start])
                    pos = end
                continue
            name = m.group(1).lower()
            attrs = m.group(2) or ""
            out.append(text[pos:start])
            if m.group(3) or attrs.rstrip().endswith("/"):
                out.append(self.ext(name, _attrs(attrs), None, m.group(0)))
                pos = m.end()
                continue
            if name in _NESTING:
                table = pairs.get(name)
                if table is None:
                    table = pairs[name] = _pair_tags(text, name)
                span = table.get(start)
                if span is None:
                    out.append(m.group(0))
                    pos = m.end()
                    continue
                inner_end, end = span
            else:
                close = None if name in noclose else re.compile(rf"</{name}\s*>", re.IGNORECASE).search(text, m.end())
                if close is None:
                    noclose.add(name)
                    out.append(m.group(0))  # an unclosed tag is shown as text, as MediaWiki does
                    pos = m.end()
                    continue
                inner_end, end = close.start(), close.end()
            out.append(self.ext(name, _attrs(attrs), text[m.end():inner_end], text[start:end]))
            pos = end
        out.append(text[pos:])
        return "".join(out)

    def ext(self, name: str, a: dict[str, str], content: str | None, raw: str) -> str:
        if name == "ref":
            ref_name = a.get("name", "").strip()
            if content is not None and ref_name and content.strip():
                self.defs.setdefault(ref_name, content)
            return self.keep("ref", raw, ref_name, content)
        if name == "references":
            if content:
                for m in re.finditer(r"<ref\b([^>]*?)>(.*?)</ref\s*>", content, re.IGNORECASE | re.DOTALL):
                    ref_name = _attrs(m.group(1)).get("name", "").strip()
                    if ref_name:
                        self.defs.setdefault(ref_name, m.group(2))
            return ""
        if name in _SILENT or content is None and name not in ("nowiki", "pre"):
            return ""
        content = content or ""
        if name == "nowiki":
            return self.keep("nowiki", raw, content)
        if name == "pre":
            return self.keep("pre", raw, content)
        if name in ("math", "chem", "ce"):
            tex = content.strip() if name == "math" else "\\ce{" + content.strip() + "}"
            return self.keep("mathb" if a.get("display", "").lower() == "block" else "math", raw, tex)
        if name in ("syntaxhighlight", "source"):
            lang = re.sub(r"[^\w+#.-]", "", a.get("lang", "")).lower()
            if "inline" in raw[:raw.find(">")].lower().split():
                return self.keep("code", raw, content)
            return self.keep("source", raw, lang, content.strip("\n"))
        if name in ("gallery", "poem", "blockquote"):
            return self.keep({"gallery": "gallery", "poem": "poem", "blockquote": "bq"}[name], raw, content)
        if name in ("table", "ul", "ol", "dl"):
            return self.keep("html", raw, name, a, content)
        if name == "charinsert":
            return content
        self.unknown[name] = self.unknown.get(name, 0) + 1
        return self.keep("raw", raw, name, content)

    def strip_templates(self, text: str) -> str:
        """Leave out every {{template}} and {{{parameter}}} (nested ones go with the outer one).

        Brace runs pair up as MediaWiki's preprocessor pairs them: a closing run takes three braces
        from an open run that has three or more and two otherwise, innermost first."""
        stack: list[list[int]] = []  # [start of the run, braces still open]
        spans: list[tuple[int, int]] = []
        for m in _BRACES.finditer(text):
            run = len(m.group(0))
            if m.group(0)[0] == "{":
                stack.append([m.start(), run])
                continue
            pos = m.start()
            while run >= 2 and stack:
                top = stack[-1]
                take = 3 if top[1] >= 3 and run >= 3 else 2
                top[1] -= take
                pos += take
                run -= take
                spans.append((top[0] + top[1], pos))
                if top[1] < 2:
                    stack.pop()
        if not spans:
            return text
        spans.sort()
        out: list[str] = []
        pos = 0
        for start, end in spans:
            if start < pos:
                continue  # inside one already left out
            self.templates += 1
            out.append(text[pos:start])
            if text[start - 1:start] == "'" and text[end:end + 1] == "'":
                out.append(self.keep("tag", "", ""))  # '''{{x}}''' stays two runs, not one of six
            pos = end
            # a template at the start of a line doesn't leave its line starting with a space (a pre block)
            if (start == 0 or text[start - 1] == "\n") and text[end:end + 1] in (" ", "\t"):
                while pos < len(text) and text[pos] in " \t":
                    pos += 1
        out.append(text[pos:])
        return "".join(out)

    def links(self, text: str) -> str:
        text = _ELINK.sub(lambda m: self.keep("ext", m.group(0), m.group(1), m.group(2)), text)
        for _ in range(8):  # innermost first: links inside picture captions
            if "[[" not in text:
                break
            new = _ILINK.sub(self._ilink, text)
            if new == text:
                break
            text = new
        return text

    def _ilink(self, m: re.Match) -> str:
        inner, trail = m.group(1), m.group(2) or ""
        target, pipe, label = inner.partition("|")
        target = target.strip()
        forced = target.startswith(":")
        if forced:
            target = target[1:].strip()
        if not target:
            return m.group(0)
        ns, colon, _rest = target.partition(":")
        ns = ns.strip().lower()
        if colon and not forced:
            if ns == "category":
                return trail
            if ns in _FILE_NS:
                params = label.split("|") if pipe else []
                boxed = any(p.strip().lower() in ("thumb", "thumbnail", "frame", "framed") for p in params)
                return self.keep("fig" if boxed else "file", m.group(0), _rest.strip(), params, trail)
        if pipe and not label.strip():
            label = re.sub(r"\s*\([^()]*\)$", "", target.partition(":")[2] if colon else target) or target
        return self.keep("link", m.group(0), target, label if pipe else None, trail)

    # --- blocks ---------------------------------------------------------------------------------
    def blocks(self, text: str, depth: int) -> str:
        lines = text.split("\n")
        out: list[str] = []
        para: list[str] = []
        i, n = 0, len(lines)

        def end_para():
            if para:
                out.append(self.paragraph("\n".join(para), depth))
                para.clear()

        while i < n:
            line = lines[i]
            if not line.strip():
                end_para()
                i += 1
                continue
            stripped = line.lstrip(" \t")
            if stripped.startswith("{|") and depth < MAX_NEST:
                end_para()
                table, i, rest = self.table(lines, i, depth)
                out.append(table)
                if rest.strip():
                    i -= 1
                    lines[i] = rest
                continue
            c = line[0]
            if c == "=":
                h = self.heading(line, depth)
                if h:
                    end_para()
                    out.append(h)
                    i += 1
                    continue
            if line.startswith("----"):
                end_para()
                out.append("<hr/>")
                rest = line.lstrip("-")
                if rest.strip():
                    lines[i] = rest.lstrip(" ")
                    continue
                i += 1
                continue
            if c in "*#:;":
                end_para()
                i = self.list(lines, i, out, depth)
                continue
            if c == " " and not self._block_line(stripped):
                end_para()
                pre = []
                while i < n and lines[i][:1] == " " and lines[i].strip() and not self._block_line(lines[i].lstrip(" \t")) \
                        and not lines[i].lstrip(" \t").startswith("{|"):
                    pre.append(self.inline(lines[i][1:], depth))
                    i += 1
                out.append("<pre>" + "\n".join(pre) + "</pre>")
                continue
            para.append(line)
            i += 1
        end_para()
        return "".join(out)

    def _block_line(self, s: str) -> bool:
        """A line that only holds a block (a pre, a table...) isn't a space-indented pre block."""
        m = _MARK.match(s)
        return bool(m) and self.slots[int(m.group(1))][0] in _BLOCK_KINDS and not s[m.end():].strip()

    def heading(self, line: str, depth: int) -> str:
        s = line.rstrip()
        a = len(s) - len(s.lstrip("="))
        b = len(s) - len(s.rstrip("="))
        if not a or not b or a == len(s):
            return ""
        level = min(a, b, 6)
        text = s[level:len(s) - level].strip()
        inner = self.inline(text, depth)
        plain = html.unescape(re.sub(r"<[^>]*>", "", inner)).strip()
        hid = re.sub(r"\s+", "_", plain) or "section"
        base, k = hid, 2
        while hid in self.ids:
            hid = f"{base}_{k}"
            k += 1
        self.ids.add(hid)
        return f'<h{level} id="{attr(hid)}">{inner}</h{level}>'

    def paragraph(self, text: str, depth: int) -> str:
        """A paragraph, split around the blocks inside it (a display equation, a picture box...)."""
        if "\x00" not in text:
            return f"<p>{self.inline(text, depth)}</p>" if text.strip() else ""
        out: list[str] = []
        cur: list[str] = []
        parts = _SPLIT_MARK.split(text)
        for k, part in enumerate(parts):
            if k % 2 == 0:
                cur.append(part)
                continue
            slot = self.slots[int(part)]
            if slot[0] in _BLOCK_KINDS:
                self._para(out, "".join(cur), depth)
                cur = []
                out.append(self.render(slot, depth))
            else:
                cur.append(f"\x00{part}\x00")
        self._para(out, "".join(cur), depth)
        return "".join(out)

    def _para(self, out: list[str], text: str, depth: int) -> None:
        if text.strip():
            inner = self.inline(text.strip(), depth)
            if inner.strip():
                out.append(f"<p>{inner}</p>")

    def list(self, lines: list[str], i: int, out: list[str], depth: int) -> int:
        stack: list[list[str]] = []  # [char] per open level
        parts: list[str] = []
        emit = parts.append
        n = len(lines)
        while i < n and lines[i][:1] in ("*", "#", ":", ";"):
            line = lines[i]
            m = re.match(r"[*#:;]+", line)
            prefix, text = m.group(0)[:MAX_LIST], line[min(m.end(), MAX_LIST):]
            items = [(prefix, text)]
            if prefix[-1] == ";":
                k = _def_colon(text)
                if k >= 0:
                    items = [(prefix, text[:k]), (prefix[:-1] + ":", text[k + 1:])]
            for pre, body in items:
                common = 0
                while common < len(pre) and common < len(stack) and _same(pre[common], stack[common][0]):
                    common += 1
                while len(stack) > common:
                    c = stack.pop()[0]
                    emit(f"</{_item(c)}></{_list(c)}>")
                if common == len(pre):
                    c = stack[-1][0]
                    emit(f"</{_item(c)}><{_item(pre[-1])}>")
                    stack[-1][0] = pre[-1]
                else:
                    for c in pre[common:]:
                        emit(f"<{_list(c)}><{_item(c)}>")
                        stack.append([c])
                emit(self.inline(body.strip(), depth))
            i += 1
        while stack:
            c = stack.pop()[0]
            emit(f"</{_item(c)}></{_list(c)}>")
        out.append("".join(parts))
        return i

    def table(self, lines: list[str], i: int, depth: int) -> tuple[str, int, str]:
        n = len(lines)
        i += 1
        caption: list[str] | None = None
        rows: list[list[list]] = []
        row: list[list] | None = None
        cell: list | None = None
        fostered: list[str] = []
        nest = 0
        rest = ""
        while i < n:
            raw = lines[i]
            line = raw.strip()
            i += 1
            if nest:
                cell[2].append(raw)
                if line.startswith("{|"):
                    nest += 1
                elif line.startswith("|}"):
                    nest -= 1
                continue
            if line.startswith("|}"):
                rest = line[2:]
                break
            if line.startswith("{|"):
                if cell is None:
                    row = row if row is not None else []
                    if not rows or rows[-1] is not row:
                        rows.append(row)
                    cell = ["td", {}, []]
                    row.append(cell)
                cell[2].append(raw)
                nest = 1
                continue
            if line.startswith("|+"):
                a, content = _split_attr(line[2:])
                caption = [content]
                cell = None
                continue
            if line.startswith("|-"):
                row = []
                rows.append(row)
                cell = None
                continue
            if line[:1] in ("|", "!"):
                head = line[0] == "!"
                body = line[1:]
                chunks = re.split(r"!!|\|\|", body) if head else body.split("||")
                if row is None:
                    row = []
                    rows.append(row)
                for chunk in chunks:
                    a, content = _split_attr(chunk)
                    cell = ["th" if head else "td", a, [content]]
                    row.append(cell)
                continue
            if cell is not None:
                cell[2].append(raw)
            elif caption is not None and not rows:
                caption.append(raw)
            else:
                fostered.append(raw)
        out = []
        if fostered and "".join(fostered).strip():
            out.append(self.blocks("\n".join(fostered), depth + 1))
        out.append("<table>")
        if caption is not None and "".join(caption).strip():
            out.append(f"<caption>{self.inline(' '.join(s.strip() for s in caption), depth)}</caption>")
        rows = [r for r in rows if r]
        body = rows
        if rows and all(c[0] == "th" for c in rows[0]):
            out.append("<thead>" + self._row(rows[0], depth) + "</thead>")
            body = rows[1:]
        if body:
            out.append("<tbody>" + "".join(self._row(r, depth) for r in body) + "</tbody>")
        out.append("</table>")
        return "".join(out), i, rest

    def _row(self, cells: list[list], depth: int) -> str:
        out = ["<tr>"]
        for tag, a, content in cells:
            out.append(f"<{tag}{_cell_attrs(a)}>{self.cell(chr(10).join(content), depth)}</{tag}>")
        out.append("</tr>")
        return "".join(out)

    def cell(self, text: str, depth: int) -> str:
        """A table cell: inline text, or blocks when it goes on over more lines. Its first line follows
        the cell's marker, so it never starts a list, heading or pre block."""
        first, _, rest = text.rstrip("\n").partition("\n")
        first = first.strip(" \t")
        if not rest.strip():
            return self.inline(first, depth)
        if first and (first[0] in "*#:;=" or first.startswith(("{|", "----"))):
            inner = self.paragraph(first, depth) + self.blocks(rest, depth + 1)
        else:
            inner = self.blocks(first + "\n" + rest, depth + 1)
        if first and inner.startswith("<p>"):
            end = inner.find("</p>")
            return inner[3:end] + inner[end + 4:]  # the text after the marker isn't a paragraph of its own
        return inner

    # --- inline ---------------------------------------------------------------------------------
    def inline(self, text: str, depth: int) -> str:
        if not text:
            return ""
        if "<" in text:
            text = _TAG.sub(self._html_tag, text)
        if "''" in text:
            text = "\n".join(_quotes(line) if "''" in line else line for line in text.split("\n"))
        if "://" in text:
            text = _BARE.sub(self._bare, text)
        return self.finish(text, depth)

    def finish(self, text: str, depth: int) -> str:
        parts = _SPLIT_MARK.split(text)
        out = []
        for k, part in enumerate(parts):
            if k % 2 == 0:
                if part:
                    s = esc(html.unescape(part)) if "&" in part else esc(part)
                    if "\x01" in s or "\x02" in s or "\x03" in s or "\x04" in s:
                        s = s.replace("\x01", "<i>").replace("\x02", "</i>").replace("\x03", "<b>").replace("\x04", "</b>")
                    out.append(s)
            else:
                out.append(self.render(self.slots[int(part)], depth))
        return "".join(out)

    def _html_tag(self, m: re.Match) -> str:
        name = m.group(2).lower()
        if name not in _INLINE_TAGS:
            return m.group(0)
        if name in ("br", "wbr"):
            return self.keep("tag", m.group(0), "<br/>" if name == "br" else "")
        tag = {"font": "span", "big": "span", "strike": "del", "tt": "code"}.get(name, name)
        if m.group(1):
            return self.keep("tag", m.group(0), f"</{tag}>")
        a = _attrs(m.group(3))
        extra = "".join(f' {k}="{attr(v)}"' for k, v in a.items() if k in _KEEP_ATTRS)
        if m.group(3).rstrip().endswith("/"):
            return self.keep("tag", m.group(0), "")
        return self.keep("tag", m.group(0), f"<{tag}{extra}>")

    def _bare(self, m: re.Match) -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?'\x01\x02\x03\x04" or url.endswith(")") and "(" not in url:
            trail = url[-1] + trail
            url = url[:-1]
        return self.keep("ext", url, url, "\x00bare") + trail

    # --- placeholders -----------------------------------------------------------------------------
    def render(self, slot: tuple, depth: int) -> str:
        kind = slot[0]
        if depth > MAX_NEST:
            return esc(slot[1])
        if kind == "tag":
            return slot[2]
        if kind == "link":
            _, _raw, target, label, trail = slot
            text = self.inline(label, depth + 1) if label is not None else esc(html.unescape(target))
            page, _, frag = html.unescape(target).partition("#")
            href = page.strip().replace(" ", "_") + ("#" + frag.strip().replace(" ", "_") if frag else "")
            return f'<a href="{attr(href)}">{text}{esc(trail)}</a>'
        if kind == "ext":
            _, _raw, url, label = slot
            url = html.unescape(url)
            if url.startswith("//"):
                url = "https:" + url
            if label == "\x00bare":
                return f'<a href="{attr(url)}">{esc(url)}</a>'
            if label is None or not label.strip():
                self.ext_count += 1
                return f'<a href="{attr(url)}">[{self.ext_count}]</a>'
            return f'<a href="{attr(url)}">{self.inline(label.strip(), depth + 1)}</a>'
        if kind in ("file", "fig"):
            return self.file(slot[2], slot[3], slot[4], depth, boxed=kind == "fig")
        if kind == "nowiki":
            return esc(html.unescape(slot[2]))
        if kind == "code":
            return f"<code>{esc(slot[2])}</code>"
        if kind == "math":
            return math_html(slot[2], False)
        if kind == "mathb":
            return f"<p>{math_html(slot[2], True)}</p>"
        if kind == "ref":
            return self.ref(slot[2], slot[3], depth)
        if kind == "pre":
            return f"<pre>{esc(html.unescape(slot[2].strip(chr(10))))}</pre>"
        if kind == "source":
            lang = slot[2]
            cls = f' class="language-{attr(lang)}"' if lang else ""
            return f"<pre><code{cls}>{esc(slot[3])}</code></pre>"
        if kind == "bq":
            return f"<blockquote>{self.blocks(self.prepare(slot[2]), depth + 1)}</blockquote>"
        if kind == "poem":
            return self.poem(slot[2], depth)
        if kind == "gallery":
            return self.gallery(slot[2], depth)
        if kind == "html":
            return self.html_block(slot[2], slot[3], slot[4], depth)
        if kind == "raw":
            return f'<pre class="raw" data-format="mediawiki">{esc(slot[1])}</pre>'
        return esc(slot[1])

    def ref(self, name: str, content: str | None, depth: int) -> str:
        if self.in_note:
            self.lost_once("A footnote inside another footnote was left out")
            return ""
        if name and name in self.note_ids:
            nid, num = self.note_ids[name]
            return note_call(num, nid)
        body = content if content is not None and content.strip() else self.defs.get(name) if name else content
        if body is None:
            self.lost_once("A footnote that is used but never written was left out")
            return ""
        num = len(self.notes) + 1
        nid = f"fn{num}"
        if name:
            self.note_ids[name] = (nid, num)
        slot = len(self.notes)
        self.notes.append((nid, ""))
        self.in_note += 1
        try:
            inner = self.blocks(self.prepare(body.strip()), depth + 1)
        finally:
            self.in_note -= 1
        self.notes[slot] = (nid, inner or "<p></p>")
        return note_call(num, nid)

    def lost_once(self, msg: str) -> None:
        if msg not in self.lost:
            self.lost.append(msg)

    def file(self, name: str, params: list[str], trail: str, depth: int, boxed: bool) -> str:
        opts, caption, alt = set(), None, ""
        for p in params:
            s = p.strip()
            if _IMG_OPTS.match(s):
                key = s.split("=", 1)[0].strip().lower()
                if key == "alt" and "=" in s:
                    alt = s.split("=", 1)[1].strip()
                opts.add(key)
            else:
                caption = p
        boxed = boxed or bool(opts & {"thumb", "thumbnail", "frame", "framed"})
        cap = self.inline(caption.strip(), depth + 1) if caption is not None else ""
        src = local_picture(self.base_dir, html.unescape(name), self.lost)
        if src is None:
            self.missing_pictures += 1
            if boxed:
                return f"<figure><figcaption>{cap}</figcaption></figure>" if cap else ""
            return cap + esc(trail)
        alt_text = alt or html.unescape(re.sub(r"<[^>]*>", "", cap))
        img = f'<img src="{attr(src)}" alt="{attr(alt_text)}"/>'
        if boxed:
            return f"<figure>{img}<figcaption>{cap}</figcaption></figure>" if cap else f"<figure>{img}</figure>"
        return img + esc(trail)

    def gallery(self, content: str, depth: int) -> str:
        out = []
        for line in self.prepare(content).split("\n"):
            line = line.strip()
            if not line:
                continue
            name, _, caption = line.partition("|")
            ns, colon, rest = name.partition(":")
            if colon and ns.strip().lower() in _FILE_NS:
                name = rest
            out.append(self.file(name.strip(), [caption] if caption else [], "", depth, boxed=True))
        return "".join(out)

    def poem(self, content: str, depth: int) -> str:
        text = self.prepare(content.strip("\n"))
        out = []
        for stanza in re.split(r"\n\s*\n", text):
            lines = [self.inline(re.sub(r"^ +", lambda m: "\xa0" * len(m.group(0)), line), depth)
                     for line in stanza.split("\n")]
            if any(s.strip() for s in lines):
                out.append("<p>" + "<br/>".join(lines) + "</p>")
        return "".join(out)

    def html_block(self, tag: str, a: dict[str, str], content: str, depth: int) -> str:
        """An HTML table or list written in the page: its tags kept, the text between them read as wikitext."""
        start = ""
        if tag == "ol" and a.get("start", "").strip().lstrip("-").isdigit():
            start = f' start="{int(a["start"].strip())}"'
        out = [f"<{tag}{start}>"]
        text = self.prepare(content)
        pos = 0
        inside: list[str] = [tag]
        for m in _STRUCT.finditer(text):
            self._html_text(text[pos:m.start()], inside, out, depth)
            pos = m.end()
            name = m.group(2).lower()
            if m.group(1):
                if name in inside:
                    while inside:
                        top = inside.pop()
                        out.append(f"</{top}>")
                        if top == name:
                            break
                continue
            if len(inside) >= MAX_NEST:
                continue
            if name in ("td", "th", "li", "dt", "dd") and inside and inside[-1] == name:
                out.append(f"</{inside.pop()}>")
            extra = _cell_attrs(_attrs(m.group(3))) if name in ("td", "th") else ""
            if name == "ol":
                s = _attrs(m.group(3)).get("start", "").strip()
                extra = f' start="{int(s)}"' if s.lstrip("-").isdigit() else ""
            out.append(f"<{name}{extra}>")
            inside.append(name)
        self._html_text(text[pos:], inside, out, depth)
        while inside:
            out.append(f"</{inside.pop()}>")
        return "".join(out)

    def _html_text(self, s: str, inside: list[str], out: list[str], depth: int) -> None:
        if not s.strip():
            return
        s = "\n".join(line.lstrip(" \t") for line in s.split("\n"))  # no pre blocks inside HTML blocks
        if inside and inside[-1] in _CELLISH:
            out.append(self.cell(s, depth))
        elif inside and inside[-1] in ("ul", "ol"):
            # a wikitext list of the same kind inside an HTML list gives it its items; loose text is one more item
            inner, tag = self.cell(s, depth), inside[-1]
            if inner.startswith(f"<{tag}>") and inner.endswith(f"</{tag}>") and inner.count(f"<{tag}>") == 1:
                out.append(inner[len(tag) + 2:-len(tag) - 3])
            else:
                out.append(f"<li>{inner}</li>")
        else:
            out.append(self.blocks(s, depth + 1))

    def summary(self) -> list[str]:
        lost = list(self.lost)
        if self.templates:
            lost.append(f"{self.templates} MediaWiki template{'s' if self.templates != 1 else ''} ({{{{...}}}}) "
                        "left out: bookport doesn't expand templates")
        if self.missing_pictures:
            lost.append(f"{self.missing_pictures} picture{'s' if self.missing_pictures != 1 else ''} named in the page "
                        "aren't beside it, so only the captions were kept")
        for name, count in sorted(self.unknown.items()):
            lost.append(f"{count} <{name}> block{'s' if count != 1 else ''} kept as text: bookport doesn't draw them")
        return lost


def _pair_tags(text: str, name: str) -> dict[int, tuple[int, int]]:
    """{start of an opening tag: (start of its closing tag, end of it)} for one nesting tag name."""
    stack: list[int] = []
    out: dict[int, tuple[int, int]] = {}
    for m in re.finditer(rf"<(/?){name}(?=[\s/>])[^>]*>", text, re.IGNORECASE):
        if m.group(1):
            if stack:
                start = stack.pop()
                out[start] = (m.start(), m.end())
        elif not m.group(0).endswith("/>"):
            stack.append(m.start())
    return out


def _same(a: str, b: str) -> bool:
    return a == b or a in ";:" and b in ";:"


def _item(c: str) -> str:
    return "li" if c in "*#" else "dt" if c == ";" else "dd"


def _list(c: str) -> str:
    return "ul" if c == "*" else "ol" if c == "#" else "dl"


def _def_colon(text: str) -> int:
    """Where ``;term : definition`` splits: the first colon outside a URL."""
    k = text.find(":")
    while k >= 0 and text[k + 1:k + 3] == "//":
        k = text.find(":", k + 1)
    return k


def _split_attr(chunk: str) -> tuple[dict[str, str], str]:
    """``attrs | content`` in a table cell: the first single pipe ends the attributes."""
    k = chunk.find("|")
    if k < 0:
        return {}, chunk
    return _attrs(chunk[:k]), chunk[k + 1:]


def _quotes(line: str) -> str:
    """MediaWiki's apostrophe runs: '' italic, ''' bold, ''''' both, with its rules for odd counts.
    Tags come out as \\x01 <i> \\x02 </i> \\x03 <b> \\x04 </b>."""
    parts = re.split(r"('{2,})", line)
    if len(parts) == 1:
        return line
    italics = bolds = 0
    for k in range(1, len(parts), 2):
        size = len(parts[k])
        if size == 4:
            parts[k - 1] += "'"
            parts[k] = "'''"
            size = 3
        elif size > 5:
            parts[k - 1] += "'" * (size - 5)
            parts[k] = "'''''"
            size = 5
        if size == 2:
            italics += 1
        elif size == 3:
            bolds += 1
        else:
            italics += 1
            bolds += 1
    if italics % 2 and bolds % 2:
        single = multi = space = None
        for k in range(1, len(parts), 2):
            if len(parts[k]) == 3:
                before = parts[k - 1]
                x1, x2 = before[-1:], before[-2:-1]
                if x1 == " ":
                    space = space or k
                elif x2 == " ":
                    single = k
                    break
                elif multi is None:
                    multi = k
        pick = single or multi or space
        if pick:
            parts[pick - 1] += "'"
            parts[pick] = "''"
    out: list[str] = []
    state = ""
    buf: list[str] = []
    for k, part in enumerate(parts):
        if k % 2 == 0:
            (buf if state == "both" else out).append(part)
            continue
        size = len(part)
        if size == 2:
            if state == "i":
                out.append("\x02")
                state = ""
            elif state == "bi":
                out.append("\x02")
                state = "b"
            elif state == "ib":
                out.append("\x04\x02\x03")
                state = "b"
            elif state == "both":
                out.append("\x03\x01" + "".join(buf) + "\x02")
                buf = []
                state = "b"
            else:
                out.append("\x01")
                state += "i"
        elif size == 3:
            if state == "b":
                out.append("\x04")
                state = ""
            elif state == "bi":
                out.append("\x02\x04\x01")
                state = "i"
            elif state == "ib":
                out.append("\x04")
                state = "i"
            elif state == "both":
                out.append("\x01\x03" + "".join(buf) + "\x04")
                buf = []
                state = "i"
            else:
                out.append("\x03")
                state += "b"
        else:
            if state == "b":
                out.append("\x04\x01")
                state = "i"
            elif state == "i":
                out.append("\x02\x03")
                state = "b"
            elif state == "bi":
                out.append("\x02\x04")
                state = ""
            elif state == "ib":
                out.append("\x04\x02")
                state = ""
            elif state == "both":
                out.append("\x01\x03" + "".join(buf) + "\x04\x02")
                buf = []
                state = ""
            else:
                buf = []
                state = "both"
    if state == "both":
        out.append("\x03\x01" + "".join(buf) + "\x02\x04")
    else:
        if state in ("b", "ib"):
            out.append("\x04")
        if state in ("i", "bi", "ib"):
            out.append("\x02")
        if state == "bi":
            out.append("\x04")
    return "".join(out)


def to_html(text: str, base_dir: Path | None = None) -> tuple[str, list[str]]:
    """Wikitext to an XHTML body (notes as asides at the end) and what was left out."""
    w = _Wiki(base_dir)
    text = clean_input(text)
    if re.match(r"\s*#redirect\b", text, re.IGNORECASE):
        text = re.sub(r"^\s*#redirect\s*:?", "Redirect to:", text, count=1, flags=re.IGNORECASE)
    body = w.blocks(w.prepare(text), 0)
    body += "".join(note_aside(nid, inner) for nid, inner in w.notes)
    return body, w.summary()


def read(path) -> Book:
    p = Path(path)
    body, lost = to_html(markup.decode(p.read_bytes()), p.parent)
    return html_book(body, p.stem.replace("_", " "), "wiki", p.parent, lost)


# --- writing -----------------------------------------------------------------------------------

_LINE_SPECIAL = re.compile(r"^(?:[*#:; ]|=|----|\{\|)")


class _Writer(TextWriter):
    def __init__(self, book: Book):
        super().__init__(book)
        self.wrote_notes = False

    def escape(self, s: str) -> str:
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = s.replace("[", "&#91;").replace("]", "&#93;").replace("{", "&#123;").replace("}", "&#125;")
        s = s.replace("|", "&#124;").replace("~~~", "~~&#126;").replace("__", "_&#95;")
        if "'" in s:
            s = re.sub(r"'(?=')|^'|'$", "&#39;", s)
        if "://" in s:
            s = s.replace("://", "&#58;//")  # a bare URL in text would become a link
        return s

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        text = text.strip()
        if _LINE_SPECIAL.match(text):
            text = "<nowiki/>" + text
        return (f'<span id="{anchor}"></span>' if anchor else "") + text

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        marks = "=" * min(max(level, 1), 6)
        return f"{marks} {text} {marks}" + (f'\n<span id="{anchor}"></span>' if anchor else "")

    def rule(self) -> str:
        return "----"

    def code_block(self, code: str, lang: str) -> str:
        if lang:
            code = re.sub(r"</syntaxhighlight", "&lt;/syntaxhighlight", code, flags=re.IGNORECASE)
            lang = re.sub(r"[^\w+#.-]", "", lang)
            return f'<syntaxhighlight lang="{lang}">\n{code}\n</syntaxhighlight>'
        # <pre> keeps markup as text; only what would end it or read as an entity needs escaping
        code = re.sub(r"&(?=#?\w+;)", "&amp;", code)
        return "<pre>" + re.sub(r"<(?=/?pre\b|/?nowiki\b|!--)", "&lt;", code, flags=re.IGNORECASE) + "</pre>"

    def raw_block(self, text: str, fmt: str) -> str:
        return text if fmt in ("mediawiki", "wiki") else self.code_block(text, "")

    def raw_inline(self, text: str, fmt: str) -> str:
        return text if fmt in ("mediawiki", "wiki") else self.code(text)

    def quote(self, inner: str) -> str:
        return f"<blockquote>\n{inner}\n</blockquote>"

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        mark = "#" if ordered else "*"
        if start == 1 and all(_simple_item(it) for it in items):
            out = []
            for it in items:
                first, *more = [b for b in it.split("\n") if b.strip()] or [""]
                out.append(f"{mark} {first}".rstrip())
                out += [mark + line for line in more]
            return "\n".join(out)
        tag = "ol" if ordered else "ul"
        attrs = f' start="{start}"' if ordered and start != 1 else ""
        return f"<{tag}{attrs}>\n" + "\n".join(f"<li>{it}</li>" for it in items) + f"\n</{tag}>"

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        if all("\n" not in d for _, ds in pairs for d in ds):
            out = []
            for term, defs in pairs:
                out.append("; " + term.replace(":", "&#58;"))
                out += [": " + d for d in defs]
            return "\n".join(out)
        out = ["<dl>"]
        for term, defs in pairs:
            out.append(f"<dt>{term}</dt>")
            out += [f"<dd>{d}</dd>" for d in defs]
        return "\n".join(out + ["</dl>"])

    def table(self, table: Table) -> str:
        out = ['{| class="wikitable"']
        if table.caption:
            out.append(f"|+ {table.caption}")
        for n, row in enumerate(table.rows):
            if n:
                out.append("|-")
            for c in row:
                mark = "!" if c.header or n < table.header_rows else "|"
                a = (f' colspan="{c.colspan}"' if c.colspan > 1 else "") + (f' rowspan="{c.rowspan}"' if c.rowspan > 1 else "")
                if c.align in ("left", "right", "center"):
                    a += f' style="text-align: {c.align}"'
                text = c.text
                if "\n" in text:
                    out.append(f"{mark}{a + ' |' if a else ''}\n{text}")
                else:
                    out.append(f"{mark}{a + ' |' if a else ''} {text}".rstrip())
        out.append("|}")
        return "\n".join(out)

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        if not src:
            return caption
        if is_external(src):
            return f"[{src} {caption or alt or src}]"
        return f"[[File:{src}|thumb|{caption or alt}]]" if caption or alt else f"[[File:{src}|thumb]]"

    def image(self, src: str, alt: str) -> str:
        if is_external(src):
            return src
        return f"[[File:{src}|{alt}]]" if alt else f"[[File:{src}]]"

    def math_block(self, el: ET.Element) -> str:
        return f'<math display="block">{mathml.to_tex(el)}</math>'

    def math_inline(self, el: ET.Element) -> str:
        return f"<math>{mathml.to_tex(el)}</math>"

    def footnote(self, body: str, note_id: str) -> str:
        self.wrote_notes = True
        return f"<ref>{body.strip()}</ref>"

    def postamble(self) -> str:
        return "<references />" if self.wrote_notes else ""

    def line_break(self) -> str:
        return "<br />"

    def emph(self, s: str) -> str:
        return f"''{s}''"

    def strong(self, s: str) -> str:
        return f"'''{s}'''"

    def strike(self, s: str) -> str:
        return f"<s>{s}</s>"

    def underline(self, s: str) -> str:
        return f"<u>{s}</u>"

    def sup(self, s: str) -> str:
        return f"<sup>{s}</sup>"

    def sub(self, s: str) -> str:
        return f"<sub>{s}</sub>"

    def smallcaps(self, s: str) -> str:
        return f'<span style="font-variant:small-caps">{s}</span>'

    def code(self, s: str) -> str:
        return f"<code>{self.escape(s)}</code>"

    def inline_anchor(self, key: str) -> str:
        return f'<span id="{key}"></span>'

    def link(self, target: str, text: str, external: bool) -> str:
        if external:
            if target.startswith("mailto:") or text and text != target:
                return f"[{target} {text}]" if text else f"[{target}]"
            return target
        return f"[[#{target}|{text}]]" if text else f"[[#{target}]]"

    def inline(self, el: ET.Element) -> str:
        if el.tag == "a" and el.get("href") and not self._is_book_link(el.get("href", "")):
            # a link to another wiki page (what [[Page]] reads as): written back as one
            page = el.get("href", "")
            text = self.inlines(el).strip()
            target = page.replace("_", " ")
            if text == target:
                return f"[[{target}]]"
            return f"[[{target}|{text}]]" if text else f"[[{target}]]"
        return super().inline(el)

    def _is_book_link(self, link: str) -> bool:
        if is_external(link) or link.startswith(("#", "mailto:")) or "noteref" in link:
            return True
        return resolve(self.href, link) in self.book.resources


def _simple_item(text: str) -> bool:
    """An item MediaWiki's * and # lines can hold: one line of text, then nested * / # lists."""
    blocks = [b for b in text.split("\n\n") if b.strip()]
    if not blocks:
        return True
    first = blocks[0]
    if "\n" in first and not all(line[:1] in "*#:;" for line in first.split("\n")[1:]):
        return False
    if first[:1] in "*#:;" and len(blocks) == 1 and all(line[:1] in "*#:;" for line in first.split("\n")):
        return True
    return not first.startswith(("<", "{|", " ")) and all(
        all(line[:1] in "*#:;" for line in b.split("\n")) for b in blocks[1:])


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    return write_text(_Writer(book), path, pictures)
