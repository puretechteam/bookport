"""Rich Text Format (RTF 1.9.1): read and write.

Reading handles code pages per font charset and Unicode escapes, character and paragraph
formatting, heading styles (by stylesheet name or outline level), tables, PNG/JPEG pictures,
HYPERLINK fields, bookmarks, footnotes and the info group. Writing produces RTF that Word,
WordPad and LibreOffice open, with heading styles, links, bookmarks, tables and pictures.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup
from ..book import Book, Metadata, is_external
from ..errors import BookportError

CHARSET_CP = {0: "cp1252", 1: "cp1252", 2: "cp1252", 77: "mac_roman", 128: "cp932", 129: "cp949", 130: "cp1361",
              134: "cp936", 136: "cp950", 161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255", 178: "cp1256",
              186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250", 255: "cp437"}
SKIP_DEST = {"fonttbl", "colortbl", "stylesheet", "info", "header", "headerl", "headerr", "headerf", "footer", "footerl",
             "footerr", "footerf", "object", "objdata", "themedata", "colorschememapping", "datastore", "latentstyles",
             "listtable", "listoverridetable", "rsidtbl", "generator", "xmlnstbl", "mmathPr", "nonshppict", "fldinst",
             "filetbl", "revtbl", "pgdsctbl", "private", "pnseclvl", "pntext", "pntxta", "pntxtb", "xe", "tc", "bkmkend",
             "atnid", "atnauthor", "annotation", "comment", "shpinst", "sp", "sn", "sv", "picprop", "blipuid", "docvar",
             "wgrffmtfilter", "pgdsc", "fchars", "lchars", "operator", "listpicture", "userprops", "ftnsep", "ftnsepc",
             "aftnsep", "aftnsepc", "template", "background", "formfield", "ffname", "ffdeftext"}
SPECIAL = {"emdash": "\u2014", "endash": "\u2013", "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c",
           "rdblquote": "\u201d", "bullet": "\u2022", "tab": "\t", "emspace": "\u2003", "enspace": "\u2002",
           "qmspace": "\u2005", "zwj": "\u200d", "zwnj": "\u200c", "ltrmark": "", "rtlmark": "", "chftn": ""}

_TOKEN = re.compile(rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}\r\n]+)|\r?\n|\r")


class _State:
    __slots__ = ("bold", "caps", "dest", "font", "hidden", "italic", "size", "strike", "sub", "sup", "uc", "underline")

    def __init__(self):
        self.bold = self.italic = self.underline = self.strike = self.sup = self.sub = self.hidden = self.caps = False
        self.font = 0
        self.uc = 1
        self.dest = None
        self.size = 24

    def copy(self):
        s = _State()
        for k in self.__slots__:
            setattr(s, k, getattr(self, k))
        return s

    def plain(self):
        self.bold = self.italic = self.underline = self.strike = self.sup = self.sub = self.hidden = self.caps = False
        self.size = 24


def read(path) -> Book:
    data = Path(path).read_bytes()
    if not data.lstrip()[:5] in (b"{\\rtf", b"{\\urt"):
        raise BookportError("Not an RTF file")
    return from_bytes(data, Path(path).stem)


def from_bytes(data: bytes, title: str = "") -> Book:
    p = _Parser(data)
    p.parse()
    book = Book(Metadata(title=p.info.get("title", "") or title))
    book.source_format = "rtf"
    md = book.metadata
    if p.info.get("author"):
        md.authors = [a.strip() for a in re.split(r";|\band\b", p.info["author"]) if a.strip()]
    if p.info.get("subject"):
        md.subjects.append(p.info["subject"])
    if p.info.get("keywords"):
        md.subjects += [k.strip() for k in re.split(r"[,;]", p.info["keywords"]) if k.strip()]
    md.description = p.info.get("doccomm", "")
    if p.info.get("company"):
        md.publisher = p.info["company"]
    md.date = p.info.get("creatim", "")
    md.language = p.lang
    _emit(book, p)
    book.ensure_toc()
    return book


class _Parser:
    def __init__(self, data: bytes):
        self.data = data
        self.fonts: dict[int, str] = {}  # font number -> code page
        self.mono_fonts: set[int] = set()
        self.styles: dict[int, str] = {}
        self.info: dict[str, str] = {}
        self.codepage = "cp1252"
        self.lang = ""
        self.paras: list[dict] = []  # {"runs": [(text, style)], "props": {...}}
        self.cur_runs: list = []
        self.pprops = self._new_pprops()
        self.images: list[tuple[bytes, str]] = []
        self.notes: list[list[dict]] = []
        self.table_rows: list = []
        self.cur_row: list = []
        self.cur_cell: list = []
        self.link_stack: list = []

    @staticmethod
    def _new_pprops():
        return {"align": "", "li": 0, "fi": 0, "style": 0, "outline": None, "intbl": False, "pagebreak": False}

    def parse(self):
        stack: list[_State] = []
        state = _State()
        pending_bytes = bytearray()
        skip = 0  # characters to skip after \u
        group_dest: list = []
        dest_text: list[str] = []
        font_entry = {"num": None, "charset": 0, "name": ""}
        pict = None
        style_entry = {"num": 0, "name": ""}
        footnote_depth = None
        field = None
        info_key = None
        star = False

        def flush_bytes():
            if pending_bytes:
                cp = self.fonts.get(state.font, self.codepage)
                try:
                    text = bytes(pending_bytes).decode(cp, "replace")
                except LookupError:
                    text = bytes(pending_bytes).decode("cp1252", "replace")
                pending_bytes.clear()
                emit(text)

        def emit(text):
            nonlocal skip
            if not text:
                return
            d = state.dest
            if d == "pict":
                return
            if d in ("fonttbl",):
                font_entry["name"] += text
                return
            if d == "stylesheet":
                style_entry["name"] += text
                return
            if d == "info_item":
                dest_text.append(text)
                return
            if d == "fldinst":
                field["instr"] += text
                return
            if d in ("bkmkstart", "sn", "sv"):
                dest_text.append(text)
                return
            if d is not None and d != "fldrslt" and d != "footnote" and d != "shppict":
                return
            if state.hidden:
                return
            self.cur_runs.append((text, (state.bold, state.italic, state.underline, state.strike, state.sup, state.sub,
                                         state.font in self.mono_fonts, state.caps, field["href"] if field and d == "fldrslt" else None)))

        for m in _TOKEN.finditer(self.data):
            word, param, hexbyte, sym, brace, text = m.groups()
            if word is None and hexbyte is None and sym is None and brace is None and text is None:
                continue  # a bare line break: RTF ignores it
            if text is not None:
                if skip:
                    n = min(skip, len(text))
                    skip -= n
                    text = text[n:]
                if text:
                    if state.dest == "pict" and pict is not None:
                        pict["hex"].append(text)
                        continue
                    pending_bytes.extend(text)
                continue
            if hexbyte is not None:
                if skip:
                    skip -= 1
                    continue
                if state.dest == "pict":
                    continue
                pending_bytes.append(int(hexbyte, 16))
                continue
            flush_bytes()
            if brace == b"{":
                stack.append(state)
                state = state.copy()
                group_dest.append(None)
                if state.dest == "stylesheet" and stack and stack[-1].dest == "stylesheet" and \
                        (len(stack) < 2 or stack[-2].dest != "stylesheet"):
                    # each group in the stylesheet is one style; \s0 may be left out
                    group_dest[-1] = "style"
                    style_entry = {"num": 0, "name": ""}
                continue
            if brace == b"}":
                ending = group_dest.pop() if group_dest else None
                if ending == "font" and font_entry["num"] is not None:
                    name = font_entry["name"].strip().rstrip(";")
                    self.fonts[font_entry["num"]] = CHARSET_CP.get(font_entry["charset"], self.codepage)
                    if any(k in name.lower() for k in ("courier", "mono", "consolas", "lucida console")):
                        self.mono_fonts.add(font_entry["num"])
                    font_entry = {"num": None, "charset": 0, "name": ""}
                elif ending == "style":
                    self.styles[style_entry["num"]] = style_entry["name"].strip().rstrip(";").lower()
                    style_entry = {"num": 0, "name": ""}
                elif ending == "info_item" and info_key:
                    self.info[info_key] = " ".join("".join(dest_text).split())
                    dest_text = []
                    info_key = None
                elif ending in ("sn", "sv") and pict is not None:
                    value = "".join(dest_text).strip()
                    dest_text = []
                    if ending == "sn":
                        pict["key"] = value
                    elif pict.get("key") == "wzDescription":
                        pict["alt"] = value
                elif ending == "pict" and pict is not None:
                    if pict["kind"]:
                        hexdata = re.sub(rb"[^0-9a-fA-F]", b"", b"".join(pict["hex"]))
                        try:
                            img = bytes.fromhex(hexdata.decode("ascii")) if not pict["bin"] else pict["bin"]
                        except ValueError:
                            img = b""
                        if img:
                            self.images.append((img, pict["kind"]))
                            self.cur_runs.append(("", ("img", len(self.images) - 1, pict["goal"], pict.get("alt", ""))))
                    pict = None
                elif ending == "bkmkstart":
                    name = "".join(dest_text).strip()
                    dest_text = []
                    if name:
                        self.cur_runs.append(("", ("anchor", name)))
                elif ending == "fldinst" and field is not None:
                    mm = re.search(r'HYPERLINK\s+(\\l\s+)?"?([^"\s]+)"?', field["instr"])
                    if mm:
                        field["href"] = ("#" if mm.group(1) else "") + mm.group(2)
                elif ending == "field":
                    field = self.link_stack.pop() if self.link_stack else None
                elif ending == "footnote" and footnote_depth is not None:
                    self._end_para()
                    note = self.paras[footnote_depth:]
                    del self.paras[footnote_depth:]
                    self.cur_runs = self._saved_runs
                    self.pprops = self._saved_props
                    self.notes.append(note)
                    self.cur_runs.append(("", ("noteref", len(self.notes))))
                    footnote_depth = None
                state = stack.pop() if stack else _State()
                continue
            if sym is not None:
                if skip:
                    skip -= 1
                    continue
                c = sym.decode("latin-1")
                if c in "\\{}":
                    pending_bytes.extend(sym)
                elif c == "~":
                    emit("\u00a0")
                elif c == "_":
                    emit("\u2011")
                elif c == "-":
                    pass
                elif c == "*":
                    star = True
                elif c in "\r\n":
                    self._end_para()
                continue
            w = word.decode("ascii")
            n = int(param) if param is not None else None
            if star:
                star = False
                # \* marks a destination to skip unless it's one we read
                if w not in ("shppict", "bkmkstart", "fldinst", "footnote", "pict", "company", "picprop") and state.dest != "stylesheet":
                    state.dest = "skip"
                    if group_dest:
                        group_dest[-1] = "skip"
                    continue
            if state.dest == "skip":
                continue
            if state.dest in ("picprop", "sn", "sv"):
                if w in ("sn", "sv") and state.dest == "picprop":
                    state.dest = w
                    group_dest[-1] = w
                    dest_text = []
                continue
            if state.dest == "pict":
                if w == "picprop":
                    state.dest = "picprop"
                elif w in ("pngblip", "jpegblip"):
                    pict["kind"] = "png" if w == "pngblip" else "jpeg"
                elif w in ("emfblip", "wmetafile", "macpict", "dibitmap", "wbitmap", "pmmetafile"):
                    pict["kind"] = None
                elif w in ("picwgoal", "pichgoal") and n:
                    pict["goal"] = pict["goal"] + ((w, n),)
                elif w == "bin" and n:
                    start = m.end()
                    pict["bin"] = self.data[start:start + n]
                continue
            if w == "u" and n is not None:
                flush_bytes()
                emit(chr(n + 65536 if n < 0 else n) if 0 <= (n + 65536 if n < 0 else n) < 0x110000 else "")
                skip = state.uc
                continue
            if w == "uc" and n is not None:
                state.uc = n
                continue
            if w in ("ansicpg",) and n:
                self.codepage = f"cp{n}"
                continue
            if w == "deflang" and n and not self.lang:
                self.lang = _LCID.get(n, "")
                continue
            if w in SKIP_DEST and state.dest in (None, "fldrslt", "footnote", "shppict", "fonttbl", "stylesheet", "info"):
                if w == "fonttbl":
                    state.dest = "fonttbl"
                elif w == "stylesheet":
                    state.dest = "stylesheet"
                elif w == "info":
                    state.dest = "info"
                elif w == "fldinst" and field is not None:
                    state.dest = "fldinst"
                    group_dest[-1] = "fldinst"
                else:
                    state.dest = "skip"
                    group_dest[-1] = "skip"
                continue
            if state.dest == "fonttbl":
                if w == "f" and n is not None:
                    font_entry["num"] = n
                    group_dest[-1] = "font"
                elif w == "fcharset" and n is not None:
                    font_entry["charset"] = n
                continue
            if state.dest == "stylesheet":
                if w == "s" and n is not None:
                    style_entry["num"] = n
                    group_dest[-1] = "style"
                elif w in ("cs", "ds", "ts"):
                    style_entry["num"] = -1
                    group_dest[-1] = "style"
                continue
            if state.dest == "info":
                if w in ("title", "author", "subject", "keywords", "doccomm", "company", "operator"):
                    state.dest = "info_item"
                    group_dest[-1] = "info_item"
                    info_key = w
                    dest_text = []
                elif w == "creatim":
                    state.dest = "info_date"
                    group_dest[-1] = "info_date"
                continue
            if state.dest == "info_date":
                if w in ("yr", "mo", "dy") and n is not None:
                    parts = self.info.get("_date", {})
                    parts[w] = n
                    self.info["_date"] = parts
                    if "yr" in parts:
                        self.info["creatim"] = f"{parts['yr']:04d}" + (f"-{parts.get('mo', 1):02d}-{parts.get('dy', 1):02d}" if "mo" in parts else "")
                continue
            # body controls
            if w == "pict":
                state.dest = "pict"
                group_dest[-1] = "pict"
                pict = {"kind": None, "hex": [], "goal": (), "bin": b""}
            elif w == "shppict":
                state.dest = state.dest or None
            elif w == "bkmkstart":
                state.dest = "bkmkstart"
                group_dest[-1] = "bkmkstart"
                dest_text = []
            elif w == "field":
                self.link_stack.append(field)
                field = {"instr": "", "href": None}
                group_dest[-1] = "field"
            elif w == "fldrslt":
                state.dest = "fldrslt"
            elif w == "footnote":
                self._saved_runs = self.cur_runs
                self._saved_props = self.pprops
                self.cur_runs = []
                self.pprops = self._new_pprops()
                footnote_depth = len(self.paras)
                state.dest = "footnote"
                group_dest[-1] = "footnote"
            elif w == "par" or w == "sect":
                self._end_para()
            elif w == "page":
                self._end_para()
                self.pprops["pagebreak"] = True
            elif w == "line":
                emit("\n")
            elif w in SPECIAL:
                emit(SPECIAL[w])
            elif w == "cell" or w == "nestcell":
                self._end_para(cell=True)
            elif w == "row" or w == "nestrow":
                self._end_row()
            elif w == "intbl":
                self.pprops["intbl"] = True
            elif w == "pard":
                keep_tbl = False
                self.pprops = {**self._new_pprops(), "intbl": keep_tbl, "pagebreak": self.pprops.get("pagebreak", False)}
            elif w == "plain":
                state.plain()
            elif w == "b":
                state.bold = n != 0
            elif w == "i":
                state.italic = n != 0
            elif w in ("ul", "uld", "uldb", "ulw", "uldash", "ulwave", "ulth"):
                state.underline = n != 0
            elif w == "ulnone":
                state.underline = False
            elif w in ("strike", "striked"):
                state.strike = n != 0
            elif w == "super":
                state.sup, state.sub = True, False
            elif w == "sub":
                state.sub, state.sup = True, False
            elif w == "nosupersub":
                state.sup = state.sub = False
            elif w in ("scaps", "caps"):
                state.caps = n != 0
            elif w == "v":
                state.hidden = n != 0
            elif w == "f" and n is not None:
                state.font = n
            elif w == "fs" and n is not None:
                state.size = n
            elif w in ("ql", "qr", "qc", "qj", "qd"):
                self.pprops["align"] = {"ql": "", "qr": "right", "qc": "center", "qj": "justify", "qd": "justify"}[w]
            elif w == "li" and n is not None:
                self.pprops["li"] = n
            elif w == "fi" and n is not None:
                self.pprops["fi"] = n
            elif w == "s" and n is not None:
                self.pprops["style"] = n
            elif w == "outlinelevel" and n is not None:
                self.pprops["outline"] = n
            elif w == "pagebb":
                self.pprops["pagebreak"] = True
            elif w == "listtext" or w == "pntext":
                pass
        flush_bytes()
        self._end_para()
        self._end_row(force=True)

    def _end_para(self, cell=False):
        runs = self.cur_runs
        self.cur_runs = []
        props = dict(self.pprops)
        if cell or props.get("intbl"):
            self.cur_cell.append({"runs": runs, "props": props})
            if cell:
                self.cur_row.append(self.cur_cell)
                self.cur_cell = []
            return
        if self.table_rows:
            self._flush_table()
        if not runs and not props.get("pagebreak"):
            self.paras.append({"runs": [], "props": props, "empty": True})
            return
        self.paras.append({"runs": runs, "props": props})
        self.pprops["pagebreak"] = False

    def _end_row(self, force=False):
        if self.cur_cell:
            self.cur_row.append(self.cur_cell)
            self.cur_cell = []
        if self.cur_row:
            self.table_rows.append(self.cur_row)
            self.cur_row = []
        if force and self.table_rows:
            self._flush_table()

    def _flush_table(self):
        self.paras.append({"table": self.table_rows, "props": {}})
        self.table_rows = []


_LCID = {1033: "en", 2057: "en-GB", 1031: "de", 1036: "fr", 1034: "es", 3082: "es", 1040: "it", 1043: "nl", 1046: "pt-BR",
         2070: "pt", 1049: "ru", 1041: "ja", 2052: "zh-CN", 1028: "zh-TW", 1042: "ko", 1053: "sv", 1044: "nb", 1030: "da",
         1035: "fi", 1045: "pl", 1029: "cs", 1038: "hu", 1055: "tr", 1032: "el", 1037: "he", 1025: "ar"}


def _emit(book: Book, p: _Parser) -> None:
    lang = book.metadata.language
    docs = []
    root, body = markup.new_document(book.metadata.title, lang)
    docs.append((root, body))
    size = 0
    image_hrefs = []
    for k, (data, kind) in enumerate(p.images):
        image_hrefs.append(book.add(f"images/image{k + 1}.{'png' if kind == 'png' else 'jpg'}", data).href)
    note_ids = []

    def heading_level(props):
        if props.get("outline") is not None and 0 <= props["outline"] < 6:
            return props["outline"] + 1
        name = p.styles.get(props.get("style", 0), "")
        m = re.match(r"^(heading|überschrift|titre|titolo)\s*(\d)$", name)
        if m:
            return min(int(m.group(2)), 6)
        if name == "title":
            return 1
        return 0

    def fill(el, runs, base="../"):
        cur_link = None
        link_el = None
        for text, style in runs:
            if style and style[0] == "img":
                idx, goal = style[1], dict(style[2])
                attrs = {"src": base + image_hrefs[idx], "alt": style[3] if len(style) > 3 else ""}
                if goal.get("picwgoal"):
                    attrs["style"] = f"width: {goal['picwgoal'] / 20:.0f}pt; max-width: 100%"
                ET.SubElement(el, "img", attrs)
                continue
            if style and style[0] == "anchor":
                ET.SubElement(el, "a", {"id": "bm_" + re.sub(r"[^\w.-]", "_", style[1])})
                continue
            if style and style[0] == "noteref":
                n = style[1]
                a = ET.SubElement(el, "a", {"href": f"notes.xhtml#note{n}", "id": f"noteref{n}", "class": "noteref",
                                            "epub:type": "noteref"})
                ET.SubElement(a, "sup").text = str(n)
                note_ids.append(n)
                continue
            bold, italic, underline, strike, sup, sub, mono, caps, href = style
            target = el
            if href:
                if href != cur_link:
                    link = href if not href.startswith("#") else "#bm_" + re.sub(r"[^\w.-]", "_", href[1:])
                    link_el = ET.SubElement(el, "a", {"href": link})
                    cur_link = href
                target = link_el
            else:
                cur_link = None
            for flag, tag in ((bold, "b"), (italic, "i"), (underline, "u"), (strike, "s"), (sup, "sup"), (sub, "sub"),
                              (mono, "code")):
                if flag:
                    target = ET.SubElement(target, tag)
            if caps:
                target = ET.SubElement(target, "span", {"style": "font-variant: small-caps"})
            parts = text.split("\n")
            markup.append_text(target, parts[0])
            for part in parts[1:]:
                br = ET.SubElement(target, "br")
                br.tail = part

    for para in p.paras:
        if "table" in para:
            table = ET.SubElement(body, "table")
            for row in para["table"]:
                tr = ET.SubElement(table, "tr")
                for cell in row:
                    td = ET.SubElement(tr, "td")
                    for k, cp in enumerate(cell):
                        if k:
                            ET.SubElement(td, "br")
                        fill(td, cp["runs"])
            continue
        props = para["props"]
        level = heading_level(props)
        if (props.get("pagebreak") or level == 1) and size > 20000:
            root, body = markup.new_document(book.metadata.title, lang)
            docs.append((root, body))
            size = 0
        if para.get("empty"):
            ET.SubElement(body, "p").text = "\u00a0"
            continue
        el = ET.SubElement(body, f"h{level}" if level else "p")
        style = []
        if props.get("align"):
            style.append(f"text-align: {props['align']}")
        if props.get("li", 0) > 0:
            style.append(f"margin-left: {props['li'] / 20:g}pt")
        if props.get("fi", 0):
            style.append(f"text-indent: {props['fi'] / 20:g}pt")
        if style:
            el.set("style", "; ".join(style))
        fill(el, para["runs"])
        size += len(markup.text_of(el))
    hrefs = [f"text/part{n:04d}.xhtml" for n in range(1, len(docs) + 1)]
    if p.notes:
        nroot, nbody = markup.new_document("Notes", lang)
        ET.SubElement(nbody, "h2").text = "Notes"
        for n, note in enumerate(p.notes, 1):
            aside = ET.SubElement(nbody, "aside", {"id": f"note{n}", "epub:type": "footnote"})
            for k, para in enumerate(note):
                pel = ET.SubElement(aside, "p")
                if k == 0:
                    back = ET.SubElement(pel, "a", {"href": f"{{doc}}#noteref{n}"})
                    back.text = f"{n}. "
                fill(pel, para.get("runs", []))
        docs.append((nroot, nbody))
        hrefs.append("text/notes.xhtml")
    # fix note and bookmark links across documents
    where = {}
    for href, (r, _b) in zip(hrefs, docs):
        for el in r.iter():
            if el.get("id"):
                where[el.get("id")] = href
    from ..book import relative
    for href, (r, _b) in zip(hrefs, docs):
        for el in r.iter("a"):
            link = el.get("href")
            if not link:
                continue
            if link.startswith(("{doc}#", "notes.xhtml#", "#")):
                frag = link.split("#", 1)[1]
                if frag in where:
                    el.set("href", relative(href, f"{where[frag]}#{frag}"))
        book.add_document(href, r)


# --- writing -----------------------------------------------------------------------------------

def _esc(text: str) -> str:
    out = []
    for ch in markup.clean_text(text):
        o = ord(ch)
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\line ")
        elif ch == "\t":
            out.append("\\tab ")
        elif ch == "\u00a0":
            out.append("\\~")
        elif o < 128:
            out.append(ch)
        elif o < 0x10000:
            out.append(f"\\u{o if o < 32768 else o - 65536}?")
        else:
            o -= 0x10000
            hi, lo = 0xD800 + (o >> 10), 0xDC00 + (o & 0x3FF)
            out.append(f"\\u{hi - 65536}?\\u{lo - 65536}?")
    return "".join(out)


def to_rtf(book: Book) -> str:
    from .. import clean
    from ..flow import book_blocks
    b = book.copy()
    clean.clean_book(b)
    blocks = book_blocks(b)
    targets = {r.href for bl in blocks for r in bl.runs if r.href and not is_external(r.href)}
    bms: dict[str, str] = {}

    def bm(key):
        if key not in bms:
            bms[key] = f"bm{len(bms) + 1}"
        return bms[key]

    md = b.metadata
    out = [r"{\rtf1\ansi\ansicpg1252\deff0\uc1",
           r"{\fonttbl{\f0\froman\fcharset0 Georgia;}{\f1\fswiss\fcharset0 Arial;}{\f2\fmodern\fcharset0 Courier New;}}",
           r"{\colortbl;\red0\green0\blue0;\red5\green99\blue193;}",
           r"{\stylesheet{\s0\f0\fs24 Normal;}"]
    sizes = {1: 40, 2: 32, 3: 28, 4: 26, 5: 24, 6: 24}
    for n in range(1, 7):
        out.append(rf"{{\s{n}\sbasedon0\snext0\outlinelevel{n - 1}\keepn\sb240\sa120\b\fs{sizes[n]} heading {n};}}")
    out.append(r"{\s7\sbasedon0\li720\ri720\i Quote;}{\s8\sbasedon0\f2\fs20 Code;}}")
    info = [rf"{{\title {_esc(md.title)}}}"]
    if md.authors:
        info.append(rf"{{\author {_esc('; '.join(md.authors))}}}")
    if md.subjects:
        info.append(rf"{{\subject {_esc(md.subjects[0])}}}{{\keywords {_esc(', '.join(md.subjects))}}}")
    if md.description:
        info.append(rf"{{\doccomm {_esc(re.sub(r'<[^>]+>', '', md.description))}}}")
    if md.publisher:
        info.append(rf"{{\company {_esc(md.publisher)}}}")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", md.date or "")
    if m:
        info.append(rf"{{\creatim\yr{int(m.group(1))}\mo{int(m.group(2))}\dy{int(m.group(3))}}}")
    out.append(r"{\info" + "".join(info) + "}")
    lang = {"en": 1033, "de": 1031, "fr": 1036, "es": 3082, "it": 1040, "nl": 1043, "pt": 2070, "ru": 1049, "ja": 1041,
            "zh": 2052, "ko": 1042}.get((md.language or "en").split("-")[0], 1033)
    out.append(rf"\deflang{lang}\paperw12240\paperh15840\margl1440\margr1440\margt1440\margb1440\widowctrl")

    def runs_rtf(runs):
        parts = []
        for r in runs:
            for a in r.anchors:
                if a in targets:
                    parts.append(rf"{{\*\bkmkstart {bm(a)}}}{{\*\bkmkend {bm(a)}}}")
            if r.image:
                parts.append(_pict(b, r.image))
                continue
            if not r.text:
                continue
            fmt = ""
            if r.bold:
                fmt += r"\b"
            if r.italic:
                fmt += r"\i"
            if r.underline:
                fmt += r"\ul"
            if r.strike:
                fmt += r"\strike"
            if r.sup:
                fmt += r"\super"
            elif r.sub:
                fmt += r"\sub"
            if r.mono:
                fmt += r"\f2"
            if r.smallcaps:
                fmt += r"\scaps"
            if r.size and abs(r.size - 1) > 0.1 and not (r.sup or r.sub):
                fmt += rf"\fs{max(8, min(144, round(24 * r.size)))}"
            text = "{" + fmt + (" " if fmt else "") + _esc(r.text) + "}"
            if r.href and (is_external(r.href) or r.href in targets):
                inst = f'HYPERLINK "{r.href}"' if is_external(r.href) else f'HYPERLINK \\\\l "{bm(r.href)}"'
                text = rf"{{\field{{\*\fldinst {{{_esc(inst)}}}}}{{\fldrslt {{\ul\cf2 {_esc(r.text)}}}}}}}"
            parts.append(text)
        return "".join(parts)

    first = True
    if b.cover is not None and next((x.image for x in blocks[:3] if x.kind == "image"), None) != b.cover:
        out.append(r"\pard\qc " + _pict(b, b.cover, "Cover") + r"\par\pard\page")
    for bl in blocks:
        anchors = "".join(rf"{{\*\bkmkstart {bm(a)}}}{{\*\bkmkend {bm(a)}}}" for a in bl.anchors if a in targets)
        if bl.kind == "table":
            cols = max(sum(c.colspan for c in row) for row in bl.rows)
            width = 9360 // max(1, cols)
            for row in bl.rows:
                edges = []
                x = 0
                for c in row:
                    x += width * c.colspan
                    edges.append(rf"\clbrdrt\brdrs\clbrdrl\brdrs\clbrdrb\brdrs\clbrdrr\brdrs\cellx{x}")
                bold_on, bold_off = "{" + "\\b ", "}"
                cells = "".join("\\pard\\intbl " + (bold_on if c.header else "") + runs_rtf(c.runs)
                                + (bold_off if c.header else "") + "\\cell " for c in row)
                out.append(r"\trowd\trgaph108" + "".join(edges) + anchors + cells + r"\row")
                anchors = ""
            out.append(r"\pard\par")
            continue
        props = r"\pard\plain"
        if bl.kind == "h":
            n = min(max(bl.level, 1), 6)
            props += rf"\s{n}\outlinelevel{n - 1}\keepn\sb240\sa120\b\fs{sizes[n]}"
        elif bl.kind == "pre":
            props += r"\s8\f2\fs20"
        elif bl.quote:
            props += r"\s7\li720\ri720"
        else:
            props += r"\fs24"
        if bl.page_break and not first:
            props += r"\pagebb"
        align = {"center": r"\qc", "right": r"\qr", "justify": r"\qj"}.get(bl.align, "")
        if bl.kind == "image":
            align = r"\qc"
        props += align
        left = int(bl.margin_left * 240) if bl.margin_left else 0
        if bl.marker:
            left = 360 * bl.depth + 360
            props += rf"\li{left}\fi-360"
        elif left > 0 and not bl.quote:
            props += rf"\li{left}"
        if bl.kind == "p" and bl.indent > 0 and not bl.marker:
            props += rf"\fi{int(bl.indent * 240)}"
        if bl.kind == "hr":
            out.append(r"\pard\brdrb\brdrs\brdrw10\brsp20\par")
            continue
        if bl.kind == "image":
            content = _pict(b, bl.image, bl.alt)
        else:
            content = (_esc(bl.marker) + r"\tab " if bl.marker else "") + runs_rtf(bl.runs)
        out.append(props + " " + anchors + content + r"\par")
        first = False
    out.append("}")
    return "\n".join(out)


def _pict(book: Book, href: str, alt: str = "") -> str:
    res = book.resources.get(href)
    if res is None:
        return ""
    data, mt = res.data, res.media_type
    if mt not in ("image/png", "image/jpeg"):
        from ..images import to_png
        data = to_png(data)
        if data is None:
            return ""
        mt = "image/png"
    from .epub import image_size
    w, h = image_size(data) or (300, 300)
    goal_w, goal_h = w * 15, h * 15  # pixels to twips at 96 dpi
    scale = min(1.0, 9360 / goal_w if goal_w else 1, 12000 / goal_h if goal_h else 1)
    hexdata = data.hex()
    lines = "\n".join(hexdata[i:i + 128] for i in range(0, len(hexdata), 128))
    kind = r"\pngblip" if mt == "image/png" else r"\jpegblip"
    prop = rf"{{\*\picprop{{\sp{{\sn wzDescription}}{{\sv {_esc(alt)}}}}}}}" if alt else ""
    return (rf"{{\pict{prop}{kind}\picw{w}\pich{h}\picwgoal{int(goal_w * scale)}\pichgoal{int(goal_h * scale)}"
            f"\n{lines}}}")


def write(book: Book, path, **_) -> None:
    Path(path).write_text(to_rtf(book), encoding="ascii", errors="replace", newline="\r\n")
