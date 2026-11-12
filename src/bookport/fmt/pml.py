"""Palm Markup Language (PML, the eReader format's text) and zipped PML (PMLZ): read and write.

PML is plain text with backslash codes: \\p page break, \\x and \\X0-\\X4 chapter titles,
\\c and \\r centred and right-aligned lines, \\i \\u \\o \\B \\k \\Sp \\Sb style toggles, \\l and \\s
font sizes, \\t and \\T="n%" indents, \\w rules, \\m images, \\q links, \\Q anchors, \\Fn and \\Sd
footnote and sidebar links with <footnote>/<sidebar> bodies, and \\a / \\U character codes.
"""

from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import limits, markup
from ..book import Book, Metadata, TocEntry, is_external, relative
from ..errors import BookportError
from ..limits import safe_path

_TOKEN = re.compile(r'\\(?:(X[0-4])|(C[0-4])="([^"]*)"|(Sp|Sb|Sd|Fn)(?:="([^"]*)")?|([TwmqQ])="([^"]*)"|(a)(\d{3})|(U)([0-9A-Fa-f]{4})'
                    r"|([pxcrtiuovnslbBkIq\\-]))")


# --- reading -----------------------------------------------------------------------------------

def _plain(s: str) -> str:
    """Text with its character codes turned into characters and every other code dropped (for note bodies)."""
    out, pos, high = [], 0, None
    for m in _TOKEN.finditer(s):
        out.append(s[pos:m.start()])
        pos = m.end()
        g = m.groups()
        if g[7]:
            try:
                out.append(bytes([int(g[8])]).decode("cp1252"))
            except (ValueError, UnicodeDecodeError):
                pass
        elif g[9]:
            cp = int(g[10], 16)
            if 0xD800 <= cp < 0xDC00:
                high = cp
                continue
            if 0xDC00 <= cp < 0xE000:
                if high is not None:
                    out.append(chr(0x10000 + ((high - 0xD800) << 10) + (cp - 0xDC00)))
            else:
                out.append(chr(cp))
        elif g[11] == "\\":
            out.append("\\")
        high = None
    out.append(s[pos:])
    return "".join(out)


def read(path) -> Book:
    p = Path(path)
    data = p.read_bytes()
    if data[:4] == b"PK\x03\x04":
        return _read_pmlz(data, p.stem)
    return pml_to_book(_decode(data), p.stem, images={}, base_dir=p.parent)


def _decode(data: bytes) -> str:
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def _read_pmlz(data: bytes, title: str) -> Book:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BookportError(f"Damaged PMLZ: {e}") from None
    with z:
        pml = next((n for n in z.namelist() if n.lower().endswith(".pml")), None)
        if pml is None:
            raise BookportError("This PMLZ has no .pml file in it")
        images = {posixpath.basename(n): limits.zip_read(z, n) for n in z.namelist()
                  if n.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))}
        book = pml_to_book(_decode(limits.zip_read(z, pml)), posixpath.splitext(posixpath.basename(pml))[0], images)
        opf = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
        if opf:
            import tempfile

            from .zipped import apply_opf
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "metadata.opf"
                p.write_bytes(limits.zip_read(z, opf))
                apply_opf(book, p)
    book.source_format = "pmlz"
    return book


class _Converter:
    def __init__(self, book: Book, images: dict, base_dir):
        self.book = book
        self.images = images
        self.base_dir = base_dir
        self.image_hrefs: dict[str, str] = {}
        self.docs: list[tuple[ET.Element, ET.Element]] = []
        self.toc: list[tuple[int, str, int, str]] = []  # (level, title, doc index, id)
        self.anchor_docs: dict[str, int] = {}
        self.new_doc()

    def new_doc(self):
        self.docs.append(markup.new_document(self.book.metadata.title, self.book.metadata.language))

    @property
    def body(self):
        return self.docs[-1][1]

    def image(self, name: str) -> str | None:
        name = name.strip()
        if name in self.image_hrefs:
            return self.image_hrefs[name]
        data = self.images.get(name) or self.images.get(posixpath.basename(name))
        if data is None and self.base_dir is not None:
            # the book's folder, one level up, or a "<title>_img" folder beside it
            folders = [self.base_dir, self.base_dir / f"{self.book.metadata.title}_img",
                       *sorted(self.base_dir.glob("*_img"))]
            for folder in folders:
                candidate = safe_path(folder, name, root=self.base_dir.parent)
                if candidate is not None and candidate.is_file():
                    data = candidate.read_bytes()
                    break
        if data is None:
            return None
        href = self.book.add("images/" + re.sub(r"[^\w.-]", "_", posixpath.basename(name)), data).href
        self.image_hrefs[name] = href
        return href

    def run(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # <footnote id="x">...</footnote> and <sidebar> bodies are collected for a notes page
        notes = []

        def take(m):
            notes.append((m.group(1), m.group(2), m.group(3)))
            return ""

        text = re.sub(r'<(footnote|sidebar)\s+id="([^"]*)"\s*>(.*?)</\1>', take, text, flags=re.DOTALL | re.IGNORECASE)
        state = {"i": False, "u": False, "o": False, "B": False, "k": False, "Sp": False, "Sb": False, "l": False,
                 "s": False, "v": False}
        block = {"c": False, "r": False, "t": False}
        heading = None  # (tag, level)
        para: ET.Element | None = None
        link: ET.Element | None = None
        indent = ""

        def current():
            nonlocal para
            if para is None:
                if heading is not None:
                    tag = f"h{min(heading[1] + 1, 6)}"
                    para = ET.SubElement(self.body, tag)
                else:
                    para = ET.SubElement(self.body, "p")
                style = []
                if block["c"]:
                    style.append("text-align: center")
                elif block["r"]:
                    style.append("text-align: right")
                if block["t"]:
                    style.append("margin-left: 5%")
                if indent:
                    style.append(f"text-indent: {indent}")
                if style:
                    para.set("style", "; ".join(style))
            target = link if link is not None else para
            for flag, tag in (("B", "b"), ("i", "i"), ("u", "u"), ("o", "s"), ("Sp", "sup"), ("Sb", "sub"), ("l", "big"),
                              ("s", "small")):
                if state[flag]:
                    target = ET.SubElement(target, tag)
            if state["k"]:
                target = ET.SubElement(target, "span", {"style": "font-variant: small-caps"})
            return target

        def add_text(s):
            if not s or state["v"]:
                return
            markup.append_text(current(), s)

        def end_para():
            nonlocal para, indent, link
            if para is not None and not (para.text or len(para)) and para.tag == "p":
                para.text = "\u00a0"
            para = None
            indent = ""
            link = None  # a link never runs past the end of its paragraph

        pos = 0
        last_anchor = [-1, ""]
        high = None  # the first half of a surrogate pair: \U has 4 digits, so an emoji is \UD83D\UDE00
        for m in _TOKEN.finditer(text):
            chunk = text[pos:m.start()]
            if chunk or not m.group(10):
                high = None
            pos = m.end()
            self._text_lines(chunk, add_text, end_para if heading is None else (lambda: add_text(" ")))
            x_level, c_level, c_title, s_code, s_arg, k_code, k_arg, a_code, a_num, u_code, u_hex, simple = m.groups()
            if x_level or simple == "x":
                level = int(x_level[1]) if x_level else 0
                if heading is None:
                    end_para()
                    if (simple == "x" or level == 0) and self._has_content():
                        self.new_doc()
                    heading = ("h", level)
                    hid = f"ch{len(self.toc) + 1}"
                    current().set("id", hid)
                    self.toc.append((level, "", len(self.docs) - 1, hid))
                else:
                    h = para
                    if h is not None and self.toc:
                        lvl, _t, di, hid = self.toc[-1]
                        title = " ".join(markup.text_of(h).split())
                        self.toc[-1] = (lvl, title, di, hid)
                    heading = None
                    end_para()
                continue
            if c_level:
                # a \C entry right after an anchor points at that anchor
                anchor_id = last_anchor[1] if last_anchor[0] == m.start() else ""
                self.toc.append((int(c_level[1]), c_title, len(self.docs) - 1, anchor_id))
                continue
            if s_code:
                if s_code in ("Sp", "Sb"):
                    state[s_code] = not state[s_code]
                elif s_code in ("Fn", "Sd"):
                    if s_arg is not None:
                        link = ET.SubElement(current(), "a", {"href": f"#note_{_safe(s_arg)}", "class": "noteref"})
                    else:
                        link = None
                continue
            if k_code:
                if k_code == "T":
                    indent = k_arg
                elif k_code == "w":
                    end_para()
                    ET.SubElement(self.body, "hr", {"style": f"width: {k_arg}"} if k_arg else {})
                elif k_code == "m":
                    href = self.image(k_arg)
                    if href:
                        ET.SubElement(current(), "img", {"src": href, "alt": ""})
                elif k_code == "q":
                    if link is None:
                        target = k_arg.removeprefix("#")
                        attrs = {"href": k_arg if is_external(k_arg) else f"#a_{_safe(target)}"}
                        link = ET.SubElement(current(), "a", attrs)
                    else:
                        link = None
                elif k_code == "Q":
                    ET.SubElement(current(), "a", {"id": f"a_{_safe(k_arg)}"})
                    self.anchor_docs[f"a_{_safe(k_arg)}"] = len(self.docs) - 1
                    last_anchor[:] = [m.end(), f"a_{_safe(k_arg)}"]
                continue
            if a_code:
                try:
                    add_text(bytes([int(a_num)]).decode("cp1252"))
                except (ValueError, UnicodeDecodeError):
                    pass
                continue
            if u_code:
                cp = int(u_hex, 16)
                if 0xD800 <= cp < 0xDC00:
                    high = cp
                elif 0xDC00 <= cp < 0xE000:
                    if high is not None:
                        add_text(chr(0x10000 + ((high - 0xD800) << 10) + (cp - 0xDC00)))
                    high = None  # a lone half is dropped: it isn't a character
                else:
                    add_text(chr(cp))
                continue
            if simple == "p":
                end_para()
                if self._has_content():
                    self.new_doc()
            elif simple in ("c", "r", "t"):
                if heading is None:  # inside a chapter title, alignment codes don't split it
                    end_para()
                block[simple] = not block[simple]
            elif simple in ("i", "u", "o", "B", "b", "k", "v", "l", "s"):
                key = "B" if simple == "b" else simple
                state[key] = not state[key]
            elif simple == "n":
                state["l"] = state["s"] = False
            elif simple == "\\":
                add_text("\\")
            elif simple == "-":
                pass
            elif simple == "q":
                link = None  # the bare \q that closes a link
        self._text_lines(text[pos:], add_text, end_para if heading is None else (lambda: add_text(" ")))
        end_para()
        if notes:
            root, body = markup.new_document("Notes", self.book.metadata.language)
            for kind, nid, content in notes:
                aside = ET.SubElement(body, "aside", {"id": f"note_{_safe(nid)}", "epub:type": kind.lower()})
                p = ET.SubElement(aside, "p")
                p.text = _plain(" ".join(content.split()))
            self.docs.append((root, body))

    def _has_content(self):
        return len(self.body) > 0

    @staticmethod
    def _text_lines(chunk, add_text, end_para):
        parts = chunk.split("\n")
        for k, part in enumerate(parts):
            if k:
                end_para()
            add_text(part)

    def finish(self):
        hrefs = [f"text/part{n:04d}.xhtml" for n in range(1, len(self.docs) + 1)]
        targets = {}
        for i, (root, _b) in enumerate(self.docs):
            for el in root.iter():
                if el.get("id"):
                    targets[el.get("id")] = hrefs[i]
        for href, (root, body) in zip(hrefs, self.docs):
            for el in root.iter():
                if el.tag == "img" and el.get("src"):
                    el.set("src", relative(href, el.get("src")))
                if el.tag == "a" and el.get("href", "").startswith("#"):
                    frag = el.get("href")[1:]
                    if frag in targets:
                        el.set("href", relative(href, f"{targets[frag]}#{frag}"))
            if len(body) or body.text:
                self.book.add_document(href, root)
        flat = []
        from ..toc import nest
        for level, title, di, hid in self.toc:
            if not title:
                continue
            href = hrefs[di] + (f"#{hid}" if hid else "")
            if hrefs[di] in self.book.resources:
                flat.append((level + 1, TocEntry(title, href)))
        self.book.toc = nest(flat) if flat else []


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)


def pml_to_book(text: str, title: str = "", images: dict | None = None, base_dir=None) -> Book:
    book = Book(Metadata(title=title))
    book.source_format = "pml"
    conv = _Converter(book, images or {}, base_dir)
    conv.run(text)
    conv.finish()
    if not book.spine:
        root, body = markup.new_document(title)
        ET.SubElement(body, "p")
        book.add_document("text/part0001.xhtml", root)
    if not book.toc:
        book.ensure_toc()
    return book


# --- writing -----------------------------------------------------------------------------------

def _esc(text: str, ascii_only: bool) -> str:
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append(" ")
        elif ord(ch) < 128 or not ascii_only:
            out.append(ch)
        else:
            try:
                b = ch.encode("cp1252")
                out.append(f"\\a{b[0]:03d}")
            except UnicodeEncodeError:
                n = ord(ch)
                if n < 0x10000:
                    out.append(f"\\U{n:04X}")
                else:  # \U takes 4 digits: past them, a surrogate pair
                    n -= 0x10000
                    out.append(f"\\U{0xD800 + (n >> 10):04X}\\U{0xDC00 + (n & 0x3FF):04X}")
    return "".join(out)


def book_to_pml(book: Book, ascii_only: bool = True, image_names: dict | None = None) -> tuple[str, dict[str, str]]:
    """PML text and {image href: file name} for the images it refers to."""
    from ..flow import book_blocks
    image_names = image_names if image_names is not None else {}
    targets = set()
    blocks = book_blocks(book)
    for b in blocks:
        for r in b.runs:
            if r.href and not is_external(r.href):
                targets.add(r.href)
    anchors: dict[str, str] = {}

    def anchor(key):
        if key not in anchors:
            anchors[key] = f"link{len(anchors) + 1}"
        return anchors[key]

    def image(href):
        if href not in image_names:
            res = book.resources.get(href)
            if res is None or not res.is_image:
                return None
            name = f"img{len(image_names) + 1:04d}.png"
            image_names[href] = name
        return image_names[href]

    toc_levels = {}
    for depth, e in book.toc_walk():
        toc_levels.setdefault(e.href, (min(depth, 4), " ".join(e.title.split())))
    lines: list[str] = []
    if book.cover:
        name = image(book.cover)
        if name:
            lines.append(f'\\m="{name}"')
            lines.append("\\p")
    for b in blocks:
        prefix = ""
        if b.page_break and lines and lines[-1] != "\\p":
            lines.append("\\p")
        for a in b.anchors:
            if a in targets:
                prefix += f'\\Q="{anchor(a)}"'
        toc_hit = next((toc_levels[a] for a in b.anchors if a in toc_levels), None)
        if b.kind == "image":
            name = image(b.image)
            if name:
                lines.append(prefix + f'\\c\\m="{name}"')
                lines.append("\\c")
            continue
        if b.kind == "hr":
            lines.append('\\w="50%"')
            continue
        if b.kind == "table":
            for row in b.rows:
                lines.append(prefix + "  ".join(_runs([r for c in row for r in c.runs + [_Space()]], image, anchor,
                                                      targets, ascii_only).split("\x00")))
                prefix = ""
            continue
        text = _runs(b.runs, image, anchor, targets, ascii_only).replace("\x00", " ")
        if b.marker:
            text = _esc(b.marker, ascii_only) + " " + text
        if b.kind == "h" or toc_hit is not None and b.kind != "p":
            level = toc_hit[0] if toc_hit is not None else min(max(b.level - 1, 0), 4)
            tag = f"\\X{level}"
            lines.append(prefix + f"{tag}{text}{tag}")
            continue
        if toc_hit is not None:
            lines.append(f'\\C{toc_hit[0]}="{_esc(toc_hit[1], ascii_only)}"')
        if b.align == "center":
            lines.append("\\c" + prefix + text)
            lines.append("\\c")
        elif b.align == "right":
            lines.append("\\r" + prefix + text)
            lines.append("\\r")
        elif b.quote or b.margin_left > 1:
            lines.append("\\t" + prefix + text + "\\t")
        else:
            lines.append(prefix + ('\\T="5%"' if b.indent > 0 else "") + text)
    return "\n".join(lines) + "\n", image_names


class _Space:
    text = " "
    image = ""
    href = ""
    anchors = ()
    bold = italic = underline = strike = sup = sub = mono = smallcaps = False
    size = 1.0


def _runs(runs, image, anchor, targets, ascii_only) -> str:
    out = []
    for r in runs:
        for a in r.anchors:
            if a in targets:
                out.append(f'\\Q="{anchor(a)}"')
        if r.image:
            name = image(r.image)
            if name:
                out.append(f'\\m="{name}"')
            continue
        if not r.text:
            continue
        text = _esc(r.text, ascii_only)
        opens = ""
        for flag, code in ((r.bold, "\\B"), (r.italic, "\\i"), (r.underline, "\\u"), (r.strike, "\\o"),
                           (r.sup, "\\Sp"), (r.sub, "\\Sb"), (r.smallcaps, "\\k")):
            if flag:
                opens += code
        closes = "".join(reversed(re.findall(r"\\(?:Sp|Sb|[Biuok])", opens)))
        piece = opens + text + closes
        if r.href and not is_external(r.href) and r.href in targets:
            piece = f'\\q="#{anchor(r.href)}"{piece}\\q'
        out.append(piece)
    return "".join(out)


def write(book: Book, path, fmt: str = "pml", **_) -> None:
    from ..images import to_png
    # cp1252 with \a and \U codes for everything else: what eReader software and other PML readers expect
    text, images = book_to_pml(book, ascii_only=True)
    if fmt == "pmlz" or str(path).lower().endswith(".pmlz"):
        name = re.sub(r'[\\/:*?"<>|]', "_", book.metadata.title or "book")[:60] or "book"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{name}.pml", text.encode("cp1252", "replace"))
            for href, fname in images.items():
                data = book.resources[href].data
                png = data if data[:8] == b"\x89PNG\r\n\x1a\n" else to_png(data)
                if png is not None:
                    z.writestr(f"{name}_img/{fname}", png)
            from .zipped import metadata_opf
            z.writestr("metadata.opf", metadata_opf(book))
        Path(path).write_bytes(buf.getvalue())
        return
    Path(path).write_bytes(text.encode("cp1252", "replace"))
