"""Read a PDF into a Book: interpret page content to positioned text, then reflow it into
paragraphs and headings, with bold and italic runs, images, links, the outline as the TOC and the
document information as metadata. Running headers, footers and page numbers are dropped and
words hyphenated across lines are joined.
"""

from __future__ import annotations

import itertools
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .. import limits, markup
from ..book import Book, Metadata, TocEntry
from ..errors import BookportError, DRMError
from .encodings import BASE_ENCODINGS, STANDARD, glyph_to_unicode
from .parser import _CONTENT, Document, Ref, Stream, _literal, _name, content_tokens, text_string

# --- fonts ---------------------------------------------------------------------------------------

class CMap:
    """A ToUnicode CMap: code space ranges plus code -> text."""

    def __init__(self, data: bytes):
        self.ranges: list[tuple[int, bytes, bytes]] = []
        self.map: dict[bytes, str] = {}
        toks = content_tokens(data)
        i = 0
        n = len(toks)
        while i < n:
            k, v = toks[i]
            if k == "kw" and v == b"begincodespacerange":
                i += 1
                while i + 1 < n and not (toks[i][0] == "kw" and toks[i][1] == b"endcodespacerange"):
                    lo, hi = toks[i][1], toks[i + 1][1]
                    if isinstance(lo, bytes) and isinstance(hi, bytes):
                        self.ranges.append((len(lo), bytes(lo), bytes(hi)))
                    i += 2
            elif k == "kw" and v == b"beginbfchar":
                i += 1
                while i + 1 < n and not (toks[i][0] == "kw" and toks[i][1] == b"endbfchar"):
                    src, dst = toks[i][1], toks[i + 1][1]
                    if isinstance(src, bytes):
                        self.map[bytes(src)] = _utf16(dst)
                    i += 2
            elif k == "kw" and v == b"beginbfrange":
                i += 1
                while i + 2 < n and not (toks[i][0] == "kw" and toks[i][1] == b"endbfrange"):
                    lo, hi = toks[i][1], toks[i + 1][1]
                    if toks[i + 2][0] == "aopen":
                        j = i + 3
                        arr = []
                        while j < n and toks[j][0] != "aclose":
                            arr.append(toks[j][1])
                            j += 1
                        if isinstance(lo, bytes) and isinstance(hi, bytes):
                            a, b = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
                            for off, dst in enumerate(arr[:b - a + 1]):
                                self.map[(a + off).to_bytes(len(lo), "big")] = _utf16(dst)
                        i = j + 1
                        continue
                    dst = toks[i + 2][1]
                    if isinstance(lo, bytes) and isinstance(hi, bytes) and isinstance(dst, bytes):
                        a, b = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
                        base = bytes(dst)
                        start = int.from_bytes(base[-2:] or b"\0", "big")
                        prefix = base[:-2]
                        for off in range(min(b - a + 1, 65536)):
                            code = (a + off).to_bytes(len(lo), "big")
                            self.map[code] = _utf16(prefix + ((start + off) & 0xFFFF).to_bytes(2, "big"))
                    i += 3
            else:
                i += 1
        self.ranges.sort(key=lambda r: r[0])

    def code_length(self, data: bytes, pos: int, default: int) -> int:
        for length, lo, hi in self.ranges:
            chunk = data[pos:pos + length]
            if len(chunk) == length and lo <= chunk <= hi:
                return length
        return default


def _utf16(b) -> str:
    if not isinstance(b, (bytes, bytearray)):
        return ""
    b = bytes(b)
    if len(b) % 2:
        b = b"\0" + b
    try:
        return b.decode("utf-16-be")
    except UnicodeDecodeError:
        return b.decode("latin-1")


class Font:
    def __init__(self, doc: Document, fdict: dict):
        r = doc.resolve
        self.subtype = str(r(fdict.get("Subtype", "Type1")))
        self.name = str(r(fdict.get("BaseFont", "")))
        low = self.name.lower()
        desc = r(fdict.get("FontDescriptor")) or {}
        self.cid = self.subtype == "Type0"
        if self.cid:
            descendants = r(fdict.get("DescendantFonts")) or []
            d0 = r(descendants[0]) if isinstance(descendants, list) and descendants else {}
            d0 = d0 if isinstance(d0, dict) else {}
            desc = r(d0.get("FontDescriptor")) or desc
            self.dw = r(d0.get("DW", 1000)) or 1000
            self.widths = _cid_widths(r(d0.get("W")) or [], r)
        else:
            first = r(fdict.get("FirstChar", 0)) or 0
            ws = r(fdict.get("Widths")) or []
            self.widths = {first + i: (r(w) or 0) for i, w in enumerate(ws)}
            self.dw = (r(desc.get("MissingWidth", 0)) if isinstance(desc, dict) else 0) or (600 if "courier" in low else 500)
        flags = r(desc.get("Flags", 0)) if isinstance(desc, dict) else 0
        flags = flags if isinstance(flags, int) else 0
        weight = r(desc.get("FontWeight", 400)) if isinstance(desc, dict) else 400
        self.bold = bool(re.search(r"bold|black|heavy|semibold|demi", low)) or (isinstance(weight, (int, float)) and weight >= 600) \
            or bool(flags & 262144)
        self.italic = bool(re.search(r"italic|oblique", low)) or bool(flags & 64) or \
            (isinstance(desc, dict) and isinstance(r(desc.get("ItalicAngle", 0)), (int, float)) and r(desc.get("ItalicAngle", 0)) != 0)
        self.mono = bool(re.search(r"courier|mono|consol", low)) or bool(flags & 1)
        self.tounicode = None
        tu = r(fdict.get("ToUnicode"))
        if isinstance(tu, Stream):
            try:
                self.tounicode = CMap(doc.decode(tu))
            except (BookportError, ValueError, IndexError):
                self.tounicode = None
        self.encoding = self._encoding(doc, fdict)
        self.type3_scale = 0.001
        if self.subtype == "Type3":
            fm = r(fdict.get("FontMatrix")) or [0.001]
            self.type3_scale = float(r(fm[0])) if fm and isinstance(r(fm[0]), (int, float)) else 0.001
        self._fixed_two = self.cid and (self.tounicode is None or not self.tounicode.ranges or all(
            length == 2 for length, _lo, _hi in self.tounicode.ranges))
        self.vertical = False
        if self.cid:
            enc = r(fdict.get("Encoding"))
            self.vertical = isinstance(enc, str) and enc.endswith("-V")

    def _encoding(self, doc, fdict) -> dict[int, str]:
        r = doc.resolve
        enc = r(fdict.get("Encoding"))
        if self.cid:
            return {}
        symbolic = "symbol" in self.name.lower() or "dingbat" in self.name.lower()
        base = STANDARD if self.subtype == "Type1" and not symbolic else BASE_ENCODINGS["WinAnsiEncoding"]
        table = dict(base)
        if isinstance(enc, str):
            table = dict(BASE_ENCODINGS.get(enc, base))
        elif isinstance(enc, dict):
            be = r(enc.get("BaseEncoding"))
            if isinstance(be, str):
                table = dict(BASE_ENCODINGS.get(be, base))
            diffs = r(enc.get("Differences")) or []
            code = 0
            for item in diffs:
                item = r(item)
                if isinstance(item, int):
                    code = item
                elif isinstance(item, str):
                    u = glyph_to_unicode(str(item))
                    if u is not None:
                        table[code] = u
                    code += 1
        return table

    def decode(self, s: bytes) -> list[tuple[int, str, float]]:
        """(code, text, advance in thousandths of text space) for each character code."""
        out = []
        if self.cid:
            i = 0
            n = len(s)
            fixed = self._fixed_two
            while i < n:
                length = 2 if fixed else self.tounicode.code_length(s, i, 2) if self.tounicode and self.tounicode.ranges else 2
                raw = s[i:i + length]
                code = int.from_bytes(raw, "big")
                text = self.tounicode.map.get(raw) if self.tounicode else None
                if text is None:
                    text = chr(code) if 32 <= code < 0xD800 and not self.tounicode else ""
                out.append((code, text, self.widths.get(code, self.dw)))
                i += length
            return out
        for b in s:
            text = None
            if self.tounicode:
                text = self.tounicode.map.get(bytes([b]))
            if text is None:
                text = self.encoding.get(b, chr(b) if 32 <= b < 127 else "")
            w = self.widths.get(b, self.dw)
            if self.subtype == "Type3":
                w = w * self.type3_scale * 1000
            out.append((b, text, w))
        return out


def _cid_widths(arr, r) -> dict[int, float]:
    out = {}
    i = 0
    while i < len(arr):
        first = r(arr[i])
        nxt = r(arr[i + 1]) if i + 1 < len(arr) else None
        if isinstance(nxt, list):
            for k, w in enumerate(nxt):
                out[first + k] = r(w) or 0
            i += 2
        elif i + 2 < len(arr):
            last, w = nxt, r(arr[i + 2])
            if isinstance(first, int) and isinstance(last, int) and last - first < 70000:
                for c in range(first, last + 1):
                    out[c] = w or 0
            i += 3
        else:
            break
    return out


# --- content ---------------------------------------------------------------------------------------

@dataclass
class Span:
    x: float
    y: float
    x1: float
    size: float
    text: str
    bold: bool
    italic: bool
    mono: bool
    order: int


@dataclass
class Picture:
    x: float
    y: float
    w: float
    h: float
    key: object


@dataclass
class PageContent:
    width: float
    height: float
    spans: list[Span] = field(default_factory=list)
    pictures: list[Picture] = field(default_factory=list)
    links: list[tuple] = field(default_factory=list)


def _mul(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


class Interpreter:
    def __init__(self, doc: Document):
        self.doc = doc
        self.fonts: dict[int, Font] = {}
        self.order = 0
        self.ops = 0  # content tokens run for the current page, forms included
        self.forms: dict[int, bytes] = {}  # decoded form XObjects, which pages and forms share
        self.form_bytes = 0
        self.active: set[int] = set()  # the forms being drawn right now (a form can't draw itself)

    def font(self, ref_or_dict) -> Font | None:
        key = ref_or_dict.num if isinstance(ref_or_dict, Ref) else id(ref_or_dict)
        if key not in self.fonts:
            d = self.doc.resolve(ref_or_dict)
            self.fonts[key] = Font(self.doc, d) if isinstance(d, dict) else None
        return self.fonts[key]

    def page(self, page: dict) -> PageContent:
        r = self.doc.resolve
        box = r(page.get("CropBox")) or r(page.get("MediaBox")) or [0, 0, 612, 792]
        try:
            x0, y0, x1, y1 = (float(r(v)) for v in box[:4])
        except (TypeError, ValueError):
            x0, y0, x1, y1 = 0, 0, 612, 792
        pc = PageContent(abs(x1 - x0), abs(y1 - y0))
        self.ops = 0
        contents = r(page.get("Contents"))
        streams = contents if isinstance(contents, list) else [contents]
        data = b"\n".join(self.doc.decode(r(s)) for s in streams if isinstance(r(s), Stream))
        resources = r(page.get("Resources")) or {}
        base = (1, 0, 0, 1, -min(x0, x1), -min(y0, y1))
        self.run(data, resources, base, pc, 0)
        for a in r(page.get("Annots")) or []:
            a = r(a)
            if isinstance(a, dict) and r(a.get("Subtype")) == "Link":
                action = r(a.get("A")) or {}
                uri = r(action.get("URI")) if isinstance(action, dict) else None
                rect = r(a.get("Rect"))
                if uri and isinstance(rect, list) and len(rect) == 4:
                    try:
                        rx = [float(r(v)) for v in rect]
                        pc.links.append((min(rx[0], rx[2]) + base[4], min(rx[1], rx[3]) + base[5],
                                         max(rx[0], rx[2]) + base[4], max(rx[1], rx[3]) + base[5],
                                         text_string(uri) if isinstance(uri, bytes) else str(uri)))
                    except (TypeError, ValueError):
                        pass
        return pc

    def run(self, data: bytes, resources: dict, ctm, pc: PageContent, depth: int):
        if depth > 8:
            return
        r = self.doc.resolve
        fonts = r(resources.get("Font")) or {}
        xobjects = r(resources.get("XObject")) or {}
        stack = []
        gs = []
        tm = lm = (1, 0, 0, 1, 0, 0)
        font = None
        size = 12.0
        tc = tw = 0.0
        th = 1.0
        tl = 0.0
        rise = 0.0
        in_text = False

        def show(s: bytes):
            nonlocal tm
            if font is None:
                return
            glyphs = font.decode(bytes(s))
            text = []
            m = _mul(tm, ctm)
            start = m[4], m[5]
            eff = size * (abs(m[3]) if m[3] else (abs(m[1]) or 1)) if not m[1] and not m[2] else size * (m[0] ** 2 + m[1] ** 2) ** 0.5
            adv_total = 0.0
            for code, t, w in glyphs:
                adv = (w / 1000.0 * size + tc + (tw if code == 32 and not font.cid else 0)) * th
                adv_total += adv
                text.append(t)
            tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + adv_total * tm[0], tm[5] + adv_total * tm[1])
            end = _mul(tm, ctm)
            txt = "".join(text)
            if txt:
                self.order += 1
                y = start[1] + rise * m[3]
                pc.spans.append(Span(start[0], y, end[4], eff, txt, font.bold, font.italic, font.mono, self.order))

        push = stack.append
        toks = _CONTENT.findall(data)
        self.ops += len(toks)
        if self.ops > limits.MAX_PDF_OPERATORS:
            return  # the rest of an endlessly repeating page is dropped
        for tok in toks:
            c = tok[0]
            # operands, classified by their first byte (the tokenizer and interpreter in one loop)
            if 48 <= c <= 57 or c == 45 or c == 46 or c == 43:
                try:
                    push(int(tok))
                except ValueError:
                    try:
                        push(float(tok))
                    except ValueError:
                        pass
                continue
            if c == 0x2F:
                push(_name(tok))
                continue
            if c == 0x28:
                push(_literal(tok, 1)[0])
                continue
            if c == 0x3C:
                if tok == b"<<":
                    push("<<")
                else:
                    h = tok[1:-1].translate(None, b" \t\r\n\x0c\x00")
                    try:
                        push(bytes.fromhex((h + b"0" if len(h) % 2 else h).decode("ascii")))
                    except ValueError:
                        push(b"")
                continue
            if c == 0x5B:
                push("[")
                continue
            if c == 0x5D:
                arr = []
                while stack and stack[-1] != "[":
                    arr.append(stack.pop())
                if stack:
                    stack.pop()
                push(list(reversed(arr)))
                continue
            if c == 0x3E:
                items = []
                while stack and stack[-1] != "<<":
                    items.append(stack.pop())
                if stack:
                    stack.pop()
                items.reverse()
                push({str(items[k]): items[k + 1] for k in range(0, len(items) - 1, 2)})
                continue
            if c == 0x25 or c in (0x28, 0x29):
                continue
            if tok in (b"true", b"false", b"null"):
                push(tok == b"true")
                continue
            op = tok
            try:
                if op == b"BT":
                    tm = lm = (1, 0, 0, 1, 0, 0)
                    in_text = True
                elif op == b"ET":
                    in_text = False
                elif op == b"Tf" and len(stack) >= 2:
                    name, size = stack[-2], stack[-1]
                    size = float(size) if isinstance(size, (int, float)) else 12.0
                    fref = fonts.get(str(name)) if isinstance(fonts, dict) else None
                    font = self.font(fref) if fref is not None else None
                elif op == b"Tm" and len(stack) >= 6:
                    tm = lm = tuple(float(v) for v in stack[-6:])
                elif op == b"Td" and len(stack) >= 2:
                    tx, ty = float(stack[-2]), float(stack[-1])
                    lm = (lm[0], lm[1], lm[2], lm[3], lm[4] + tx * lm[0] + ty * lm[2], lm[5] + tx * lm[1] + ty * lm[3])
                    tm = lm
                elif op == b"TD" and len(stack) >= 2:
                    tx, ty = float(stack[-2]), float(stack[-1])
                    tl = -ty
                    lm = (lm[0], lm[1], lm[2], lm[3], lm[4] + tx * lm[0] + ty * lm[2], lm[5] + tx * lm[1] + ty * lm[3])
                    tm = lm
                elif op == b"T*":
                    lm = (lm[0], lm[1], lm[2], lm[3], lm[4] - tl * lm[2], lm[5] - tl * lm[3])
                    tm = lm
                elif op == b"TL" and stack:
                    tl = float(stack[-1])
                elif op == b"Tc" and stack:
                    tc = float(stack[-1])
                elif op == b"Tw" and stack:
                    tw = float(stack[-1])
                elif op == b"Tz" and stack:
                    th = float(stack[-1]) / 100.0
                elif op == b"Ts" and stack:
                    rise = float(stack[-1])
                elif op == b"Tj" and stack and isinstance(stack[-1], bytes):
                    show(stack[-1])
                elif op == b"'" and stack:
                    lm = (lm[0], lm[1], lm[2], lm[3], lm[4] - tl * lm[2], lm[5] - tl * lm[3])
                    tm = lm
                    if isinstance(stack[-1], bytes):
                        show(stack[-1])
                elif op == b'"' and len(stack) >= 3:
                    tw, tc = float(stack[-3]), float(stack[-2])
                    lm = (lm[0], lm[1], lm[2], lm[3], lm[4] - tl * lm[2], lm[5] - tl * lm[3])
                    tm = lm
                    if isinstance(stack[-1], bytes):
                        show(stack[-1])
                elif op == b"TJ" and stack and isinstance(stack[-1], list):
                    for item in stack[-1]:
                        if isinstance(item, bytes):
                            show(item)
                        elif isinstance(item, (int, float)):
                            adv = -item / 1000.0 * size * th
                            if adv > size * 0.2 and font is not None:
                                # a big kern inside TJ is how many writers draw a space
                                show_space = True
                            else:
                                show_space = False
                            tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + adv * tm[0], tm[5] + adv * tm[1])
                            if show_space:
                                self.order += 1
                elif op == b"cm" and len(stack) >= 6:
                    ctm = _mul(tuple(float(v) for v in stack[-6:]), ctm)
                elif op == b"q":
                    gs.append((ctm, font, size, tc, tw, th, tl, rise))
                elif op == b"Q" and gs:
                    ctm, font, size, tc, tw, th, tl, rise = gs.pop()
                elif op == b"Do" and stack and isinstance(xobjects, dict) and self.ops <= limits.MAX_PDF_OPERATORS:
                    self.ops += 50  # drawing a form or picture costs more than one token
                    name = str(stack[-1])
                    xref = xobjects.get(name)
                    xo = r(xref)
                    if isinstance(xo, Stream):
                        sub = r(xo.dict.get("Subtype"))
                        if sub == "Image":
                            w, h = (ctm[0] ** 2 + ctm[1] ** 2) ** 0.5, (ctm[2] ** 2 + ctm[3] ** 2) ** 0.5
                            pc.pictures.append(Picture(ctm[4], ctm[5], w, h, xref if isinstance(xref, Ref) else xo))
                        elif sub == "Form":
                            fm = r(xo.dict.get("Matrix")) or [1, 0, 0, 1, 0, 0]
                            try:
                                fmt = tuple(float(r(v)) for v in fm[:6])
                            except (TypeError, ValueError):
                                fmt = (1, 0, 0, 1, 0, 0)
                            res = r(xo.dict.get("Resources")) or resources
                            key = xref.num if isinstance(xref, Ref) else id(xo)
                            body = None if key in self.active else self.forms.get(key)
                            if body is None and key not in self.active:
                                body = self.doc.decode(xo)
                                if self.form_bytes + len(body) <= 64 * 1024 * 1024:
                                    self.forms[key] = body
                                    self.form_bytes += len(body)
                            if body is not None:
                                self.active.add(key)
                                try:
                                    self.run(body, res, _mul(fmt, ctm), pc, depth + 1)
                                finally:
                                    self.active.discard(key)
                elif op == b"BI":
                    pass
            except (TypeError, ValueError, ZeroDivisionError, IndexError):
                pass
            stack.clear()
        del in_text


# --- reflow -----------------------------------------------------------------------------------------

@dataclass
class TextLine:
    y: float
    x0: float
    x1: float
    size: float
    runs: list  # (text, bold, italic, mono)
    page: int

    @property
    def text(self):
        return "".join(t for t, *_ in self.runs)


def lines_of(pc: PageContent, page_index: int) -> list[TextLine]:
    spans = sorted(pc.spans, key=lambda s: s.order)
    lines: list[list[Span]] = []
    for s in spans:
        if not s.text.strip() and s.text != " ":
            continue
        placed = False
        if lines:
            last = lines[-1]
            ref = last[-1]
            if abs(s.y - ref.y) <= max(ref.size, s.size) * 0.35 and s.x >= ref.x - ref.size * 0.5:
                last.append(s)
                placed = True
        if not placed:
            lines.append([s])
    out = []
    for group in lines:
        group.sort(key=lambda s: s.x)
        runs = []
        prev = None
        for s in group:
            text = s.text
            if prev is not None:
                gap = s.x - prev.x1
                if gap > max(prev.size, s.size) * 0.18 and not prev.text.endswith(" ") and not text.startswith(" "):
                    text = " " + text
            key = (s.bold, s.italic, s.mono)
            if runs and runs[-1][1:] == key:
                runs[-1] = (runs[-1][0] + text,) + key
            else:
                runs.append((text,) + key)
            prev = s
        size = Counter(round(s.size, 1) for s in group for _ in s.text).most_common(1)[0][0]
        out.append(TextLine(group[0].y, group[0].x, group[-1].x1, size, runs, page_index))
    out.sort(key=lambda ln: -ln.y)
    return out


def _strip_furniture(pages: list[list[TextLine]], heights: list[float]) -> None:
    """Drop page numbers and running headers/footers. Lines are compared with whole numbers masked
    ("Chapter 12" and "Chapter 13" match); digits inside a word ("A320", "1880s") stay."""
    counts: Counter = Counter()
    for lines in pages:
        for ln in lines[:2] + lines[-2:]:
            counts[re.sub(r"\b\d+\b", "#", ln.text.strip().lower())] += 1
    n = len(pages)
    for i, lines in enumerate(pages):
        h = heights[i]
        keep = []
        for k, ln in enumerate(lines):
            t = ln.text.strip()
            edge = ln.y > h * 0.9 or ln.y < h * 0.1
            near_end = k < 2 or k >= len(lines) - 2
            norm = re.sub(r"\b\d+\b", "#", t.lower())
            if edge and near_end and (re.fullmatch(r"[\divxlcdm\s\-–—.]*", t.lower()) and len(t) < 12
                                      or n >= 6 and counts[norm] >= max(3, n * 0.3) and len(t) < 90):
                continue
            keep.append(ln)
        pages[i] = keep


def reflow(pages: list[list[TextLine]], pictures: dict[int, list], width: float):
    """Lines to paragraphs: ('p'|'h1'|'h2'|'h3', runs, page) and ('img', key, page) items."""
    all_lines = [ln for lines in pages for ln in lines]
    if not all_lines:
        return [("img", p.key, i) for i, pics in pictures.items() for p in pics]
    body = Counter()
    for ln in all_lines:
        body[round(ln.size, 1)] += len(ln.text)
    body_size = body.most_common(1)[0][0] or 10
    lefts = Counter(round(ln.x0) for ln in all_lines if abs(ln.size - body_size) < 0.6)
    left = lefts.most_common(1)[0][0] if lefts else 0
    rights = sorted(ln.x1 for ln in all_lines if abs(ln.size - body_size) < 0.6)
    right = rights[int(len(rights) * 0.9)] if rights else width
    gaps = []
    for lines in pages:
        for a, b in itertools.pairwise(lines):
            if abs(a.size - body_size) < 0.6 and abs(b.size - body_size) < 0.6 and 0 < a.y - b.y < body_size * 3:
                gaps.append(a.y - b.y)
    spacing = statistics.median(gaps) if gaps else body_size * 1.3
    heading_sizes = sorted({round(ln.size, 1) for ln in all_lines if ln.size >= body_size * 1.18}, reverse=True)
    level_of = {s: min(k + 1, 3) for k, s in enumerate(heading_sizes)}
    out = []
    for pi, lines in enumerate(pages):
        pics = sorted(pictures.get(pi, []), key=lambda p: -(p.y + p.h))
        para = None
        prev = None
        for ln in lines:
            while pics and pics[0].y + pics[0].h * 0.5 > ln.y:
                if para:
                    out.append(para)
                    para = None
                out.append(("img", pics.pop(0).key, pi))
            heading = round(ln.size, 1) in level_of
            new = para is None
            if not new:
                gap = prev.y - ln.y
                kind_now = para[0]
                if heading != (kind_now != "p") or gap > spacing * 1.45 or gap < -spacing:
                    new = True
                elif not heading and ln.x0 > left + body_size * 0.8 and prev.x0 <= left + body_size * 0.5:
                    new = True  # a first-line indent
                elif not heading and prev.x1 < right - body_size * 3 and re.search(r"[.!?:;\"'”’)]\s*$", prev.text):
                    new = True  # the last line ended short, at the end of a sentence
                elif ln.x0 > left + body_size * 3 and prev.x0 <= left + body_size:
                    new = True
            if new:
                if para:
                    out.append(para)
                kind = f"h{level_of[round(ln.size, 1)]}" if heading else "p"
                para = [kind, [], pi]
            else:
                _join(para[1], prev)
            para[1].extend(ln.runs)
            prev = ln
        if para:
            out.append(para)
        for p in pics:
            out.append(("img", p.key, pi))
    return out


HYPHEN_JOIN = chr(0xE000)  # a private-use marker: "a hyphen ended this line"
NBSP = chr(0xA0)
_CJK_END = re.compile("[" + chr(0x2E80) + "-" + chr(0x9FFF) + chr(0xAC00) + "-" + chr(0xD7AF) + chr(0xF900) + "-"
                      + chr(0xFAFF) + chr(0xFF00) + "-" + chr(0xFFEF) + chr(0x3000) + "-" + chr(0x303F) + "]$")


def _join(runs: list, prev: TextLine) -> None:
    """Join a line to the previous with a space; a line-end hyphen is marked for later; no space
    between CJK characters."""
    if not runs:
        return
    text, *style = runs[-1]
    if re.search(r"\w-$", text):
        runs[-1] = (text + HYPHEN_JOIN,) + tuple(style)
    elif _CJK_END.search(text):
        return
    elif not text.endswith((" ", NBSP)):
        runs[-1] = (text + " ",) + tuple(style)


def resolve_hyphens(items: list) -> None:
    """Decide each line-end hyphen: join the halves when the joined word appears elsewhere in the
    book without a hyphen (typesetter hyphenation), keep the hyphen otherwise (a real compound)."""
    words: Counter = Counter()
    for it in items:
        if it[0] != "img":
            for text, *_ in it[1]:
                words.update(w.lower() for w in re.findall(r"\w+", text.replace(HYPHEN_JOIN, " ")))
    pattern = re.compile(r"(\w+)-" + HYPHEN_JOIN + r"(\x00?\s*)(\w+)")

    def fix(m):
        a, sep, b = m.group(1), m.group(2), m.group(3)
        if words.get((a + b).lower(), 0) > 0:
            return a + sep + b
        return a + "-" + sep + b

    for it in items:
        if it[0] == "img":
            continue
        runs = it[1]
        # a split word can span two runs (a style change at the line break)
        joined = "\x00".join(t for t, *_ in runs)
        if HYPHEN_JOIN not in joined:
            continue
        joined = pattern.sub(fix, joined).replace(HYPHEN_JOIN, "-")
        parts = joined.split("\x00")
        for k, part in enumerate(parts[:len(runs)]):
            runs[k] = (part,) + tuple(runs[k][1:])


# --- images ------------------------------------------------------------------------------------

def extract_image(doc: Document, key) -> tuple[bytes, str] | None:
    xo = doc.resolve(key)
    if not isinstance(xo, Stream):
        return None
    r = doc.resolve
    d = xo.dict
    filters = doc.stream_filters(xo)
    w, h = r(d.get("Width")), r(d.get("Height"))
    if not isinstance(w, int) or not isinstance(h, int) or w < 8 or h < 8 or w * h > limits.MAX_PIXELS:
        return None
    if filters and filters[-1] in ("DCTDecode", "DCT"):
        data = doc.decode(xo)
        return (data, "image/jpeg") if data[:2] == b"\xff\xd8" else None
    if filters and filters[-1] in ("JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"):
        return None
    data = doc.decode(xo)
    bpc = r(d.get("BitsPerComponent", 8)) or 8
    cs = r(d.get("ColorSpace", "DeviceRGB"))
    palette = None
    ncomp = 3
    if isinstance(cs, list) and cs:
        kind = str(r(cs[0]))
        if kind == "Indexed" and len(cs) >= 4:
            base = r(cs[1])
            hival = r(cs[2])
            lookup = r(cs[3])
            if isinstance(lookup, Stream):
                lookup = doc.decode(lookup)
            base_n = 3
            if isinstance(base, list) and base and str(r(base[0])) == "ICCBased":
                icc = r(base[1])
                base_n = r(icc.dict.get("N", 3)) if isinstance(icc, Stream) else 3
            elif str(base) == "DeviceGray":
                base_n = 1
            elif str(base) == "DeviceCMYK":
                base_n = 4
            palette = (bytes(lookup or b""), base_n, hival)
            ncomp = 1
        elif kind == "ICCBased":
            icc = r(cs[1])
            ncomp = r(icc.dict.get("N", 3)) if isinstance(icc, Stream) else 3
        elif kind in ("CalRGB", "Lab"):
            ncomp = 3
        elif kind == "CalGray":
            ncomp = 1
        elif kind in ("DeviceN", "Separation"):
            return None
    elif cs in ("DeviceGray", "CalGray", "G"):
        ncomp = 1
    elif cs in ("DeviceCMYK", "CMYK"):
        ncomp = 4
    from ..images import Raster, encode_png
    if r(d.get("ImageMask")):
        return None
    if bpc not in (1, 2, 4, 8, 16) or ncomp not in (1, 3, 4):
        return None
    row = (w * ncomp * bpc + 7) // 8
    if len(data) < row * h:
        data += bytes(row * h - len(data))
    pixels = bytearray()
    for y in range(h):
        line = data[y * row:(y + 1) * row]
        if bpc == 8:
            pixels += line[:w * ncomp]
        elif bpc == 16:
            pixels += line[0:w * ncomp * 2:2]
        else:
            per = 8 // bpc
            mask = (1 << bpc) - 1
            vals = []
            for x in range(w * ncomp):
                vals.append((line[x // per] >> (8 - bpc * (x % per + 1))) & mask)
            if palette is None:
                vals = [v * 255 // mask for v in vals]
            pixels += bytes(vals)
    if palette is not None:
        lut, n, _hival = palette
        out = bytearray()
        for v in pixels:
            entry = lut[v * n:v * n + n]
            if n == 1:
                entry = entry * 3
            elif n == 4:
                entry = _cmyk(entry)
            out += entry.ljust(3, b"\0")
        return encode_png(Raster(w, h, "RGB", bytes(out))), "image/png"
    if ncomp == 4:
        out = bytearray()
        for i in range(0, len(pixels), 4):
            out += _cmyk(pixels[i:i + 4])
        return encode_png(Raster(w, h, "RGB", bytes(out))), "image/png"
    mode = "L" if ncomp == 1 else "RGB"
    return encode_png(Raster(w, h, mode, bytes(pixels[:w * h * (1 if ncomp == 1 else 3)]))), "image/png"


def _cmyk(px) -> bytes:
    if len(px) < 4:
        return b"\0\0\0"
    c, m, y, k = px[:4]
    return bytes((255 - min(255, c + k), 255 - min(255, m + k), 255 - min(255, y + k)))


# --- the book --------------------------------------------------------------------------------------

def _metadata(doc: Document) -> Metadata:
    md = Metadata()
    info = doc.info()
    r = doc.resolve
    md.title = text_string(r(info.get("Title"))).strip()
    author = text_string(r(info.get("Author"))).strip()
    if author:
        md.authors = [a.strip() for a in re.split(r";|,(?=\s*[A-Z][^,]*\s)|\band\b|&", author) if a.strip()]
    md.description = text_string(r(info.get("Subject"))).strip()
    kw = text_string(r(info.get("Keywords"))).strip()
    if kw:
        md.subjects = [k.strip() for k in re.split(r"[,;]", kw) if k.strip()]
    created = text_string(r(info.get("CreationDate")))
    m = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?", created)
    if m:
        md.date = m.group(1) + (f"-{m.group(2)}" if m.group(2) else "") + (f"-{m.group(3)}" if m.group(3) and m.group(2) else "")
    lang = r(doc.root.get("Lang"))
    if lang:
        md.language = text_string(lang)
    meta = r(doc.root.get("Metadata"))
    if isinstance(meta, Stream) and (not md.title or not md.authors):
        try:
            xmp = doc.decode(meta).decode("utf-8", "replace")
            if not md.title:
                t = re.search(r"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", xmp, re.DOTALL)
                if t:
                    md.title = markup.html.unescape(t.group(1)).strip() if hasattr(markup, "html") else t.group(1).strip()
            if not md.authors:
                a = re.search(r"<dc:creator>.*?<rdf:li[^>]*>(.*?)</rdf:li>", xmp, re.DOTALL)
                if a:
                    md.authors = [a.group(1).strip()]
        except (BookportError, ValueError):
            pass
    return md


def _outline(doc: Document, page_index: dict) -> list:
    r = doc.resolve
    outlines = r(doc.root.get("Outlines"))
    names = _named_dests(doc)
    out = []

    def dest_page(item):
        dest = r(item.get("Dest"))
        if dest is None:
            action = r(item.get("A"))
            if isinstance(action, dict) and r(action.get("S")) == "GoTo":
                dest = r(action.get("D"))
        if isinstance(dest, (bytes, str)):
            dest = names.get(bytes(dest) if isinstance(dest, bytes) else dest.encode("latin-1"))
            dest = r(dest)
            if isinstance(dest, dict):
                dest = r(dest.get("D"))
        if isinstance(dest, list) and dest:
            target = dest[0]
            if isinstance(target, Ref):
                pi = page_index.get(target.num)
                y = r(dest[3]) if len(dest) > 3 and str(r(dest[1])) == "XYZ" else None
                return pi, y if isinstance(y, (int, float)) else None
            if isinstance(target, int):
                return target, None
        return None, None

    def walk(first, depth, seen):
        items = []
        node_ref = first
        count = 0
        while node_ref is not None and count < 10000:
            count += 1
            key = node_ref.num if isinstance(node_ref, Ref) else id(node_ref)
            if key in seen:
                break
            seen.add(key)
            node = r(node_ref)
            if not isinstance(node, dict):
                break
            title = text_string(r(node.get("Title"))).strip()
            pi, y = dest_page(node)
            children = walk(node.get("First"), depth + 1, seen) if depth < 12 else []
            items.append((title, pi, y, children))
            node_ref = node.get("Next")
        return items

    if isinstance(outlines, dict):
        out = walk(outlines.get("First"), 0, set())
    return out


def _named_dests(doc: Document) -> dict[bytes, object]:
    r = doc.resolve
    out = {}
    names = r(doc.root.get("Names"))
    tree = r(names.get("Dests")) if isinstance(names, dict) else None

    def walk(node, depth):
        node = r(node)
        if not isinstance(node, dict) or depth > 20:
            return
        arr = r(node.get("Names")) or []
        for k in range(0, len(arr) - 1, 2):
            key = r(arr[k])
            if isinstance(key, bytes):
                out[bytes(key)] = arr[k + 1]
        for kid in r(node.get("Kids")) or []:
            walk(kid, depth + 1)

    if tree is not None:
        walk(tree, 0)
    old = r(doc.root.get("Dests"))
    if isinstance(old, dict):
        for k, v in old.items():
            out[k.encode("latin-1")] = v
    return out


def read_pdf(path: Path) -> Book:
    data = Path(path).read_bytes()
    try:
        doc = Document(data)
    except DRMError:
        raise
    except (BookportError, ValueError, IndexError, KeyError, TypeError, RecursionError) as e:
        raise BookportError(f"Damaged PDF: {e}") from None
    book = Book(_metadata(doc))
    book.source_format = "pdf"
    try:
        pages = doc.pages()
    except (BookportError, RecursionError) as e:
        raise BookportError(f"Damaged PDF: {e}") from None
    if not pages:
        raise BookportError("This PDF has no pages")
    interp = Interpreter(doc)
    contents = []
    for p in pages:
        try:
            contents.append(interp.page(p))
        except limits.LimitError:
            raise
        except (BookportError, RecursionError, ValueError, TypeError, IndexError, KeyError, AttributeError):
            contents.append(PageContent(612, 792))  # a damaged page reads as an empty one
    page_lines = [lines_of(pc, i) for i, pc in enumerate(contents)]
    _strip_furniture(page_lines, [pc.height for pc in contents])
    pictures = {i: [p for p in pc.pictures if p.w > 10 and p.h > 10] for i, pc in enumerate(contents)}
    width = statistics.median([pc.width for pc in contents])
    items = reflow(page_lines, pictures, width)
    resolve_hyphens(items)
    # images
    image_hrefs: dict[object, str | None] = {}

    def image_href(key):
        k = key.num if isinstance(key, Ref) else id(key)
        if k not in image_hrefs:
            got = extract_image(doc, key)
            image_hrefs[k] = None
            if got is not None:
                data_, mt = got
                ext = ".jpg" if mt == "image/jpeg" else ".png"
                image_hrefs[k] = book.add(f"images/image{len(image_hrefs):04d}{ext}", data_, mt).href
        return image_hrefs[k]

    page_index = {p["_ref"].num: i for i, p in enumerate(pages) if isinstance(p.get("_ref"), Ref)}
    outline = _outline(doc, page_index)
    # chapters start at top-level outline entries, or every 12 pages
    starts = sorted({pi for _t, pi, _y, _c in outline if isinstance(pi, int)})
    if len(starts) < 2:
        starts = list(range(0, len(pages), 12))
    if 0 not in starts:
        starts.insert(0, 0)
    doc_of_page = {}
    k = 0
    for pi in range(len(pages)):
        while k + 1 < len(starts) and pi >= starts[k + 1]:
            k += 1
        doc_of_page[pi] = k
    lang = book.metadata.language
    docs: dict[int, tuple] = {}
    page_anchor: dict[int, tuple[str, str]] = {}
    for item in items:
        pi = item[2]
        di = doc_of_page.get(pi, 0)
        if di not in docs:
            docs[di] = markup.new_document(book.metadata.title, lang)
        _root, body = docs[di]
        href = f"text/part{di + 1:04d}.xhtml"
        if pi not in page_anchor:
            ET.SubElement(body, "a", {"id": f"page{pi + 1}"})
            page_anchor[pi] = (href, f"page{pi + 1}")
        if item[0] == "img":
            src = image_href(item[1])
            if src:
                div = ET.SubElement(body, "div", {"class": "image"})
                ET.SubElement(div, "img", {"src": "../" + src, "alt": ""})
            continue
        kind, runs, _pi = item
        el = ET.SubElement(body, kind)
        for text, bold, italic, mono in runs:
            target = el
            if bold and kind == "p":
                target = ET.SubElement(target, "b")
            if italic:
                target = ET.SubElement(target, "i")
            if mono:
                target = ET.SubElement(target, "code")
            markup.append_text(target, text)
        _links(el, contents[pi])
    for di in sorted(docs):
        root, body = docs[di]
        book.add_document(f"text/part{di + 1:04d}.xhtml", root)
    if not book.spine:
        root, body = markup.new_document(book.metadata.title, lang)
        ET.SubElement(body, "p")
        book.add_document("text/part0001.xhtml", root)

    def entry(title, pi, children):
        target = page_anchor.get(pi) if isinstance(pi, int) else None
        if target is None and isinstance(pi, int):
            later = [p for p in page_anchor if p >= pi]
            target = page_anchor[min(later)] if later else None
        href = f"{target[0]}#{target[1]}" if target else ""
        kids = [entry(t, p, c) for t, p, _y, c in children]
        if not href and kids:
            href = kids[0].href
        return TocEntry(title or "Untitled", href, kids)

    book.toc = [entry(t, p, c) for t, p, _y, c in outline]
    book.toc = [e for e in book.toc if e.href]
    if not book.toc:
        book.ensure_toc()
    # a first page that's just a big picture is the cover
    first_items = [it for it in items if it[2] == 0]
    if first_items and first_items[0][0] == "img" and sum(1 for it in first_items if it[0] != "img") <= 2:
        k = first_items[0][1]
        href = image_hrefs.get(k.num if isinstance(k, Ref) else id(k))
        if href:
            book.cover = href
    if not book.metadata.title:
        h = next((it for it in items if it[0] in ("h1", "h2")), None)
        if h:
            book.metadata.title = "".join(t for t, *_ in h[1]).strip()[:200]
    return book


def _links(el, pc: PageContent) -> None:
    if not pc.links:
        return
    del el
