"""Markdown: a CommonMark-style reader (with GitHub tables and strikethrough) and a writer."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup
from ..book import Book, Metadata, is_external, relative, resolve
from ..limits import safe_path

# --- reading -----------------------------------------------------------------------------------

_HR = re.compile(r"^ {0,3}([-*_])(\s*\1){2,}\s*$")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)[^`]*$")
_BULLET = re.compile(r"^( {0,3})([-*+])( +|\t|$)(.*)$")
_ORDERED = re.compile(r"^( {0,3})(\d{1,9})([.)])( +|\t|$)(.*)$")
_QUOTE = re.compile(r"^ {0,3}> ?(.*)$")
_REFDEF = re.compile(r"""^ {0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?(?:\s+(?:"([^"]*)"|'([^']*)'|\(([^)]*)\)))?\s*$""")
_HTML_BLOCK = re.compile(r"^ {0,3}<(/?)(address|article|aside|blockquote|body|center|details|dialog|dd|div|dl|dt|"
                         r"fieldset|figcaption|figure|footer|form|h[1-6]|header|hr|html|li|main|nav|ol|p|pre|section|"
                         r"table|tbody|td|tfoot|th|thead|tr|ul|!--|script|style)(\s|/?>|$)", re.IGNORECASE)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


class _Parser:
    def __init__(self, text: str):
        self.refs: dict[str, tuple[str, str]] = {}
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
        return self.blocks(self.lines)

    # --- blocks -------------------------------------------------------------------------------
    def blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        i, n = 0, len(lines)
        para: list[str] = []

        def end_para():
            if para:
                out.append("<p>" + self.inline("\n".join(s.strip() for s in para)) + "</p>")
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
                cls = f' class="language-{html.escape(info)}"' if info else ""
                out.append(f"<pre><code{cls}>" + html.escape("\n".join(body)) + "</code></pre>")
                continue
            m = _ATX.match(line)
            if m:
                end_para()
                level = len(m.group(1))
                out.append(f"<h{level}>{self.inline((m.group(2) or '').strip())}</h{level}>")
                i += 1
                continue
            if para and _SETEXT.match(line):
                level = 1 if line.strip()[0] == "=" else 2
                text = "\n".join(s.strip() for s in para)
                para.clear()
                out.append(f"<h{level}>{self.inline(text)}</h{level}>")
                i += 1
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
            if (_BULLET.match(line) or _ORDERED.match(line)) and (
                    not para or _BULLET.match(line) or _ORDERED.match(line).group(2) == "1"):
                end_para()
                i = self._list(lines, i, out)
                continue
            if line.startswith("    ") and not para:
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
                out.append("\n".join(body))
                continue
            if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
                end_para()
                i = self._table(lines, i, out)
                continue
            para.append(line)
            i += 1
        end_para()
        return "\n".join(out)

    def _list(self, lines, i, out):
        n = len(lines)
        first = _BULLET.match(lines[i]) or _ORDERED.match(lines[i])
        ordered = first.re is _ORDERED
        start = int(first.group(2)) if ordered else 1
        items: list[list[str]] = []
        loose = False
        while i < n:
            m = (_ORDERED if ordered else _BULLET).match(lines[i])
            if not m:
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
                    if i + 1 < n and (_ORDERED if ordered else _BULLET).match(lines[i + 1]):
                        loose = True
                        i += 1
                    break
                if line.startswith((" " * marker_w, "    ")):
                    item.append(line[min(marker_w, len(line) - len(line.lstrip())):])
                    i += 1
                    continue
                if (_BULLET.match(line) or _ORDERED.match(line)) or _HR.match(line) or _ATX.match(line) or _QUOTE.match(line):
                    break
                item.append(line)  # lazy continuation
                i += 1
            items.append(item)
        tag = "ol" if ordered else "ul"
        attr = f' start="{start}"' if ordered and start != 1 else ""
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

        # code spans first: nothing inside them is markup
        text = re.sub(r"(`+)(.{1,1000}?)(?<!`)\1(?!`)", lambda m: keep("<code>" + html.escape(m.group(2).strip()) + "</code>"),
                      text, flags=re.DOTALL)
        text = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", lambda m: keep(html.escape(m.group(1))), text)
        text = re.sub(r"<((?:https?|ftp|mailto):[^>\s]+)>", lambda m: keep(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>'), text)
        text = re.sub(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w:.-]*(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*\s*/?>",
                      lambda m: keep(m.group(0)), text)
        text = re.sub(r"&(#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);", lambda m: keep(m.group(0)), text)
        text = html.escape(text, quote=False)

        def link(m, image):
            label, dest = m.group(1), m.group(2).strip()
            title = ""
            tm = re.match(r"^<?([^\s>]*)>?\s+(?:\"([^\"]*)\"|'([^']*)')$", dest)
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
            text = re.sub(r"\[([^\[\]]*)\]\(([^()\s]*(?:\([^()]*\)[^()\s]*)*(?:\s+\"[^\"]*\"|\s+'[^']*')?)\)",
                          lambda m: link(_unq(m), False), text)
            text = re.sub(r"!\[([^\[\]]+)\](?:\[([^\[\]]*)\])?", lambda m: reflink(m, True), text)
            text = re.sub(r"\[([^\[\]]+)\](?:\[([^\[\]]*)\])?", lambda m: reflink(m, False), text)
        # spans are bounded (1,000 characters), so unclosed markers can't make the search quadratic
        text = re.sub(r"(\*\*|__)(?=\S)(.{1,1000}?)(?<=\S)\1", r"<strong>\2</strong>", text, flags=re.DOTALL)
        text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.{1,1000}?)(?<=[^\s*])\*(?!\*)", r"<em>\1</em>", text, flags=re.DOTALL)
        text = re.sub(r"(?<![\w_])_(?=[^\s_])(.{1,1000}?)(?<=[^\s_])_(?![\w_])", r"<em>\1</em>", text, flags=re.DOTALL)
        text = re.sub(r"~~(?=\S)(.{1,1000}?)(?<=\S)~~", r"<del>\1</del>", text, flags=re.DOTALL)
        text = re.sub(r"( {2,}|\\)\n", "<br/>\n", text)
        text = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)
        # placeholders can nest (code inside a link label)
        text = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)
        return text

    def _link(self, label, dest, title, image, keep):
        dest = html.unescape(dest)
        t = f' title="{html.escape(html.unescape(title))}"' if title else ""
        if image:
            return keep(f'<img src="{html.escape(dest)}" alt="{html.escape(html.unescape(_strip_tags(label)))}"{t}/>')
        return f'<a href="{html.escape(dest)}"{t}>{label}</a>'


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
    # one document per top-level heading, so big files are split into chapters
    chunks = _split_chapters(text)
    for n, chunk in enumerate(chunks, 1):
        body_html = to_html(chunk)
        root = markup.parse(f"<html><head><title>{html.escape(book.metadata.title)}</title></head><body>{body_html}</body></html>")
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

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>])")


def _esc(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


class _Writer:
    def __init__(self, book: Book, image_dir: str = "images"):
        self.book = book
        self.image_dir = image_dir
        self.targets: set[str] = set()
        self.used_images: dict[str, str] = {}
        for href, _ in book.documents():
            for el in book.tree(href).iter("a"):
                link = el.get("href", "")
                if link and not is_external(link):
                    t = resolve(href, link)
                    frag = link.split("#", 1)[1] if "#" in link else ""
                    self.targets.add(f"{t}#{frag}" if frag else t)

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
            if tag in markup.BLOCK or tag in ("li", "figure", "nav", "section"):
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
    text = "\n\n".join(parts) + "\n"
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, w.used_images


def _yaml(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r"[:#\"'{}\[\],&*!|>%@`]", s) else s


def write(book: Book, path, **_) -> None:
    text, _images = book_to_markdown(book)
    Path(path).write_text(text, encoding="utf-8", newline="\n")
