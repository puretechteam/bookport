"""Typeset a Book into PDF pages: an own layout engine over the book's styled blocks.

Paragraphs are broken into lines (greedy, with justification capped so spacing never gapes),
headings stay with the lines after them, lists hang their markers, tables get measured columns,
images are scaled to the page, and chapters start on new pages. Fonts are embedded as TrueType
subsets (CIDFontType2 with Identity-H) with ToUnicode maps, so the text can be searched,
selected and extracted. Links, a bookmark outline from the TOC, page numbers and the book's
metadata are included.
"""

from __future__ import annotations

import datetime
import hashlib
import lzma
import os
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .. import flow as flowmod
from ..book import Book, is_external
from ..errors import BookportError
from . import ttf
from .objects import Name, Ref, Writer

PAGE_SIZES = {"phone": (252.0, 448.0), "ereader": (259.0, 346.0), "a5": (419.53, 595.28), "a4": (595.28, 841.89),
              "letter": (612.0, 792.0)}
PAGE_ALIASES = {"e-reader": "ereader", "ereader6": "ereader", "e-reader 6\"": "ereader", "6in": "ereader", "kindle": "ereader",
                "6": "ereader", "mobile": "phone", "us letter": "letter"}
MARGINS = {"phone": (16, 14, 18, 14), "ereader": (16, 16, 18, 16), "a5": (42, 40, 48, 40), "a4": (64, 64, 70, 64),
           "letter": (60, 66, 66, 66)}  # top, right, bottom, left
TEXT_SIZES = {"phone": (8.5, 10.0, 12.0), "ereader": (8.5, 10.0, 12.0), "a5": (9.5, 11.0, 13.0), "a4": (10.0, 12.0, 14.0),
              "letter": (10.0, 12.0, 14.0)}
FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
# stored xz-compressed, without the tables bookport doesn't read (tools/slim_fonts.py): 0.86 MB, not 2.4
FONT_FILES = {"regular": "NotoSerif-Regular.ttf.xz", "bold": "NotoSerif-Bold.ttf.xz",
              "italic": "NotoSerif-Italic.ttf.xz", "bolditalic": "NotoSerif-BoldItalic.ttf.xz",
              "mono": "NotoSansMono-Regular.ttf.xz"}
# glyf-outline system fonts that cover scripts the bundled fonts don't (CJK, symbols)
SYSTEM_FALLBACKS = [
    "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/msgothic.ttc", "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/seguisym.ttf", "C:/Windows/Fonts/arialuni.ttf", "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]

CJK = re.compile("[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef\u3000-\u303f]")


def page_size(name: str | None) -> tuple[str, tuple[float, float]]:
    key = (name or "a5").strip().lower()
    key = PAGE_ALIASES.get(key, key)
    if key not in PAGE_SIZES:
        raise BookportError(f"Unknown page size {name!r}: use phone, ereader, a5, a4 or letter")
    return key, PAGE_SIZES[key]


class Slot:
    """One embedded font: the glyphs used and their text, for subsetting and ToUnicode."""

    def __init__(self, name: str, font: ttf.Font):
        self.name = name
        self.font = font
        self.used: dict[int, str] = {}
        self.ref: Ref | None = None


def font_bytes(style: str) -> bytes:
    """A bundled font, as a TrueType file."""
    return lzma.decompress((FONT_DIR / FONT_FILES[style]).read_bytes())


class Fonts:
    def __init__(self):
        self.slots: dict[str, Slot] = {}
        self._fallbacks: list[Slot] | None = None
        self._cover: dict[str, Slot | None] = {}

    def get(self, style: str) -> Slot:
        if style not in self.slots:
            self.slots[style] = Slot(f"F{len(self.slots) + 1}", ttf.load(font_bytes(style)))
        return self.slots[style]

    def fallbacks(self) -> list[Slot]:
        if self._fallbacks is None:
            self._fallbacks = []
            extra = os.environ.get("BOOKPORT_FALLBACK_FONTS", "")
            for path in [p for p in extra.split(os.pathsep) if p] + SYSTEM_FALLBACKS:
                try:
                    if not os.path.isfile(path):
                        continue
                    font = ttf.load(Path(path).read_bytes())
                except (BookportError, OSError, struct.error, IndexError):
                    continue
                if font.embeddable:
                    slot = Slot(f"FB{len(self._fallbacks) + 1}", font)
                    self._fallbacks.append(slot)
        return self._fallbacks

    def for_char(self, ch: str, style: str) -> Slot:
        primary = self.get(style)
        if primary.font.has(ch) or ch in "\n\t":
            return primary
        regular = self.get("regular") if style != "mono" else primary
        if regular.font.has(ch):
            return regular
        key = ch
        if key not in self._cover:
            found = None
            for slot in self.fallbacks():
                if slot.font.has(ch):
                    found = slot
                    break
            if found is not None and found.name not in [s.name for s in self.slots.values()]:
                self.slots[found.name] = found
            self._cover[key] = found
        return self._cover[key] or primary


@dataclass
class Item:
    kind: str  # "text", "space", "image", "break"
    text: str = ""
    slot: Slot | None = None
    size: float = 10.0
    width: float = 0.0
    rise: float = 0.0
    color: tuple | None = None
    underline: bool = False
    strike: bool = False
    href: str = ""
    anchors: tuple = ()
    image: str = ""
    height: float = 0.0


@dataclass
class Line:
    items: list[Item]
    width: float
    spaces: int
    ascent: float
    descent: float
    last: bool = False
    indent: float = 0.0


@dataclass
class Page:
    ops: list[bytes] = field(default_factory=list)
    links: list[tuple] = field(default_factory=list)  # (x0, y0, x1, y1, href)
    number: int = 0


def _color(value: str):
    v = (value or "").strip().lower()
    named = {"black": None, "red": (0.8, 0, 0), "blue": (0, 0, 0.8), "green": (0, 0.5, 0), "gray": (0.5, 0.5, 0.5),
             "grey": (0.5, 0.5, 0.5), "maroon": (0.5, 0, 0), "navy": (0, 0, 0.5), "purple": (0.5, 0, 0.5)}
    if v in named:
        return named[v]
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        rgb = tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return None if max(rgb) < 0.15 else rgb
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", v)
    if m:
        rgb = tuple(min(255, int(x)) / 255 for x in m.groups())
        return None if max(rgb) < 0.15 else rgb
    return None


def _fmt(v: float) -> bytes:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return (s or "0").encode("ascii")


class Typesetter:
    def __init__(self, book: Book, page: str | None = None, text_size: str | None = None):
        self.book = book
        self.size_key, (self.pw, self.ph) = page_size(page)
        sizes = TEXT_SIZES[self.size_key]
        ts = (text_size or "normal").strip().lower()
        if ts not in ("small", "normal", "large"):
            raise BookportError(f"Unknown text size {text_size!r}: use small, normal or large")
        self.base = sizes[("small", "normal", "large").index(ts)]
        self.mt, self.mr, self.mb, self.ml = MARGINS[self.size_key]
        self.width = self.pw - self.ml - self.mr
        self.fonts = Fonts()
        self.pages: list[Page] = []
        self.y = 0.0
        self.anchors: dict[str, tuple[int, float]] = {}
        self.images: dict[str, tuple[str, Ref, int, int]] = {}
        self.writer = Writer()
        self.prev_bottom = 0.0
        self.number = 0

    # --- pages -------------------------------------------------------------------------------
    def new_page(self, numbered=True):
        p = Page()
        if numbered:
            self.number += 1
            p.number = self.number
        self.pages.append(p)
        self.y = self.ph - self.mt
        self.prev_bottom = 0.0
        return p

    @property
    def page(self) -> Page:
        return self.pages[-1]

    def room(self) -> float:
        return self.y - self.mb

    def ensure(self, height: float):
        if height > self.room() and self.y < self.ph - self.mt - 1:
            self.new_page()

    # --- items ---------------------------------------------------------------------------------
    def style_of(self, run: flowmod.Run) -> str:
        if run.mono:
            return "mono"
        if run.bold and run.italic:
            return "bolditalic"
        if run.bold:
            return "bold"
        if run.italic:
            return "italic"
        return "regular"

    def items(self, runs, size: float, mono=False, scale: float = 1.0) -> list[Item]:
        """Runs to items; a run's size is relative to the body text (``scale`` shrinks, e.g. in tables)."""
        out: list[Item] = []
        for r in runs:
            if r.image:
                out.append(self.image_item(r, size))
                continue
            style = "mono" if mono else self.style_of(r)
            rsize = self.base * scale * (r.size if r.size and 0.5 < r.size < 3.5 else 1.0) if r.size else size
            rise = 0.0
            if r.sup or r.sub:
                rise = rsize * (0.33 if r.sup else -0.15)
                rsize *= 0.7
            color = _color(r.color)
            href = r.href
            text = r.text.upper() if r.smallcaps else r.text
            first = True
            for part in re.split(r"(\n| +|\t)", text):
                if not part:
                    continue
                anchors = r.anchors if first else ()
                first = False
                if part == "\n":
                    out.append(Item("break", anchors=anchors))
                    continue
                if part[0] in " \t":
                    slot = self.fonts.get(style)
                    w = slot.font.width(" ", rsize) * len(part.replace("\t", "    "))
                    out.append(Item("space", " ", slot, rsize, w, rise, color, r.underline, r.strike, href, anchors))
                    continue
                # split by the font that has each character
                seg = ""
                seg_slot = None
                for ch in unicodedata.normalize("NFC", part):
                    slot = self.fonts.for_char(ch, style)
                    if seg and slot is not seg_slot:
                        out.append(Item("text", seg, seg_slot, rsize, seg_slot.font.width(seg, rsize), rise, color,
                                        r.underline, r.strike, href, anchors))
                        anchors = ()
                        seg = ""
                    seg_slot = slot
                    seg += ch
                if seg:
                    out.append(Item("text", seg, seg_slot, rsize, seg_slot.font.width(seg, rsize), rise, color,
                                    r.underline, r.strike, href, anchors))
        return out

    def image_item(self, run, size) -> Item:
        info = self.image(run.image)
        if info is None:
            return Item("text", "", self.fonts.get("regular"), size, 0, anchors=run.anchors)
        _name, _ref, w, h = info
        wpt, hpt = w * 0.75, h * 0.75
        max_h = size * 6 if hpt > size * 6 and wpt < self.width * 0.5 else self.ph - self.mt - self.mb - size
        scale = min(1.0, self.width / wpt if wpt else 1, max_h / hpt if hpt else 1)
        return Item("image", "", None, size, wpt * scale, 0, None, False, False, run.href, run.anchors, run.image,
                    hpt * scale)

    # --- line breaking ---------------------------------------------------------------------------
    def lines(self, items: list[Item], width: float, indent: float = 0.0, hang: float = 0.0) -> list[Line]:
        """Greedy line breaking; ``indent`` applies to the first line, ``hang`` to the rest."""
        words: list[tuple[list[Item], float, Item | None]] = []
        cur: list[Item] = []
        cur_w = 0.0
        for it in items:
            if it.kind == "space" or it.kind == "break":
                words.append((cur, cur_w, it))
                cur, cur_w = [], 0.0
            elif it.kind == "text" and CJK.search(it.text):
                # every CJK character is a break opportunity
                for k, ch in enumerate(it.text):
                    piece = Item("text", ch, it.slot, it.size, it.slot.font.width(ch, it.size), it.rise, it.color,
                                 it.underline, it.strike, it.href, it.anchors if k == 0 else ())
                    if CJK.match(ch):
                        if cur:
                            words.append((cur, cur_w, None))
                        cur, cur_w = [piece], piece.width
                        words.append((cur, cur_w, None))
                        cur, cur_w = [], 0.0
                    else:
                        cur.append(piece)
                        cur_w += piece.width
            elif it.kind == "text" and re.search(r"\w[-‐–—/]\w", it.text):
                # a line may also break after a hyphen, dash or slash inside a word
                parts = [p for p in re.split(r"(?<=[\w][-‐–—/])(?=\w)", it.text) if p]
                for k, part in enumerate(parts):
                    piece = Item("text", part, it.slot, it.size, it.slot.font.width(part, it.size), it.rise, it.color,
                                 it.underline, it.strike, it.href, it.anchors if k == 0 else ())
                    cur.append(piece)
                    cur_w += piece.width
                    if k + 1 < len(parts):
                        words.append((cur, cur_w, None))
                        cur, cur_w = [], 0.0
            else:
                cur.append(it)
                cur_w += it.width
        if cur:
            words.append((cur, cur_w, None))
        out: list[Line] = []
        line: list[Item] = []
        line_w = 0.0
        spaces = 0
        pending_space: Item | None = None
        avail = width - indent

        def finish(last):
            nonlocal line, line_w, spaces, avail
            while line and line[-1].kind == "space":
                line_w -= line.pop().width
                spaces -= 1
            asc = desc = 0.0
            for it in line:
                if it.kind == "image":
                    asc = max(asc, it.height)
                else:
                    asc = max(asc, it.size * 0.95 + max(0.0, it.rise))
                    desc = max(desc, it.size * 0.3 - min(0.0, it.rise))
            if not line:
                asc, desc = self.base * 0.95, self.base * 0.3
            out.append(Line(line, line_w, max(0, spaces), asc, desc, last, width - avail))
            line, line_w, spaces = [], 0.0, 0
            avail = width - hang

        for word, w, after in words:
            if word:
                extra = pending_space.width if pending_space is not None and line else 0.0
                if line and line_w + extra + w > avail + 0.01:
                    finish(False)
                    extra = 0.0
                    pending_space = None
                if w > avail + 0.01 and not line:
                    # a word longer than the line: break it between characters
                    for it in word:
                        if it.kind != "text" or it.width <= avail + 0.01:
                            if line and line_w + it.width > avail + 0.01:
                                finish(False)
                            line.append(it)
                            line_w += it.width
                            continue
                        chunk = ""
                        for ch in it.text:
                            cw = it.slot.font.width(chunk + ch, it.size)
                            if line_w + cw > avail + 0.01 and (chunk or line):
                                if chunk:
                                    line.append(Item("text", chunk, it.slot, it.size, it.slot.font.width(chunk, it.size),
                                                     it.rise, it.color, it.underline, it.strike, it.href, it.anchors))
                                    line_w += line[-1].width
                                finish(False)
                                chunk = ""
                            chunk += ch
                        if chunk:
                            line.append(Item("text", chunk, it.slot, it.size, it.slot.font.width(chunk, it.size),
                                             it.rise, it.color, it.underline, it.strike, it.href))
                            line_w += line[-1].width
                else:
                    if extra and pending_space is not None:
                        line.append(pending_space)
                        line_w += pending_space.width
                        spaces += 1
                    line.extend(word)
                    line_w += w
                pending_space = None
            if after is not None:
                if after.kind == "break":
                    if after.anchors:
                        line.append(Item("text", "", self.fonts.get("regular"), self.base, 0, anchors=after.anchors))
                    finish(True)
                    pending_space = None
                else:
                    pending_space = after if line else None
                    if not line and after.anchors:
                        pending_space = None
        if line or not out:
            finish(True)
        else:
            out[-1].last = True
        return out

    # --- drawing -------------------------------------------------------------------------------
    def draw_line(self, line: Line, x0: float, width: float, align: str, baseline: float):
        ops = self.page.ops
        slack = width - line.indent - line.width
        x = x0 + line.indent
        gap = 0.0
        if align == "center":
            x += max(0.0, slack) / 2
        elif align == "right":
            x += max(0.0, slack)
        elif align == "justify" and not line.last and line.spaces and slack > 0:
            space_w = next((it.width for it in line.items if it.kind == "space"), self.base * 0.25)
            gap = slack / line.spaces
            if gap > space_w * 2.5:
                gap = 0.0  # too loose: leave the line ragged rather than stretch it
        # one text object per line: glyphs advance the position themselves, so a TJ array with the
        # justification gaps as kerns places every word exactly where the layout put it
        text_ops: list[bytes] = []
        tj: list[bytes] = []
        state = {"font": None, "rise": 0.0, "color": None, "placed": False}

        def flush():
            if tj:
                text_ops.append(b"[" + b"".join(tj) + b"] TJ\n")
                tj.clear()

        for it in line.items:
            for a in it.anchors:
                self.anchors.setdefault(a, (len(self.pages) - 1, baseline + line.ascent))
            if it.kind == "image":
                info = self.images.get(it.image)
                if info is not None:
                    name = info[0]
                    ops.append(b"q %s 0 0 %s %s %s cm /%s Do Q\n" % (_fmt(it.width), _fmt(it.height), _fmt(x),
                                                                      _fmt(baseline), name.encode()))
                    if it.href:
                        self.page.links.append((x, baseline, x + it.width, baseline + it.height, it.href))
                x += it.width
                flush()
                state["placed"] = False  # text after the picture needs its position set again
                continue
            w = it.width + (gap if it.kind == "space" else 0.0)
            if it.text and it.slot is not None:
                key = (it.slot.name, it.size)
                if key != state["font"]:
                    flush()
                    text_ops.append(b"/%s %s Tf\n" % (it.slot.name.encode(), _fmt(it.size)))
                    state["font"] = key
                if it.rise != state["rise"]:
                    flush()
                    text_ops.append(b"%s Ts\n" % _fmt(it.rise))
                    state["rise"] = it.rise
                if it.color != state["color"]:
                    flush()
                    text_ops.append(b"%s %s %s rg\n" % tuple(_fmt(c) for c in it.color) if it.color else b"0 g\n")
                    state["color"] = it.color
                if not state["placed"]:
                    flush()
                    text_ops.append(b"1 0 0 1 %s %s Tm\n" % (_fmt(x), _fmt(baseline)))
                    state["placed"] = True
                glyphs = self.encode(it.slot, it.text)
                kerned = self.kerned(it.slot.font, it.text, glyphs) if len(it.text) > 1 else None
                for part in kerned or (b"<" + glyphs + b">",):
                    if part[:1] == b"<" and tj and tj[-1][:1] == b"<":
                        tj[-1] = tj[-1][:-1] + part[1:]  # one string for a run of glyphs with no kern between
                    else:
                        tj.append(part)
                if it.kind == "space" and gap:
                    tj.append(_fmt(-gap * 1000 / it.size))
            if it.underline or it.strike:
                thick = max(0.4, it.size * 0.05)
                yy = baseline + it.rise + (-it.size * 0.12 if it.underline else it.size * 0.28)
                ops.append(b"%s %s %s %s re f\n" % (_fmt(x), _fmt(yy), _fmt(w), _fmt(thick)))
            if it.href:
                self.page.links.append((x, baseline - it.size * 0.25, x + w, baseline + it.size * 0.9, it.href))
            x += w
        flush()
        if text_ops:
            ops.append(b"BT\n" + b"".join(text_ops) + b"ET\n")

    def encode(self, slot: Slot, text: str) -> bytes:
        font = slot.font
        out = []
        for ch in text:
            gid = font.cmap.get(ord(ch), 0)
            if gid not in slot.used:
                slot.used[gid] = ch if gid else "\ufffd"  # glyph 0 stands for every character the font lacks
            out.append(b"%04X" % gid)
        return b"".join(out)

    @staticmethod
    def kerned(font: ttf.Font, text: str, glyphs: bytes) -> list[bytes] | None:
        """TJ array elements for text encoded as glyphs: glyph strings with the font's kerning between
        pairs as numbers (thousandths of the font size, positive moving the next glyph left), as
        Font.width measures it; None when no pair in it is kerned."""
        kerning = font.kerning()
        if not kerning.subtables:
            return None
        done = kerning.runs.get(text, False)  # books repeat their words
        if done is not False:
            return done
        kern = kerning.pair
        cmap = font.cmap
        parts = None
        start = 0
        prev = cmap.get(ord(text[0]), 0)
        for k in range(1, len(text)):
            g = cmap.get(ord(text[k]), 0)
            adjust = kern(prev, g)
            if adjust:
                if parts is None:
                    parts = []
                parts += [b"<" + glyphs[4 * start:4 * k] + b">", _fmt(-adjust * 1000 / font.units)]
                start = k
            prev = g
        if parts is not None:
            parts.append(b"<" + glyphs[4 * start:] + b">")
        if len(kerning.runs) < 100_000:
            kerning.runs[text] = parts
        return parts

    # --- blocks ----------------------------------------------------------------------------------
    def paragraph(self, b: flowmod.Block, size: float, x0: float, width: float, mono=False):
        items = self.items(b.runs, size, mono=mono)
        if not items:
            for a in b.anchors:
                self.anchors.setdefault(a, (len(self.pages) - 1, self.y))
            return
        indent = b.indent * size if b.kind == "p" and b.indent > 0 else 0.0
        lines = self.lines(items, width, indent=indent)
        lh = b.line_height if 1.0 <= (b.line_height or 0) <= 2.5 else (1.2 if b.kind == "h" else 1.38)
        align = {"justify": "justify", "center": "center", "right": "right"}.get(b.align, "left")
        if b.kind == "pre":
            align = "left"
        need_first = (lines[0].ascent + lines[0].descent) * lh / 1.25
        if b.keep_with_next:
            need_first += (self.base * 1.38) * 3
        self.ensure(need_first)
        for a in b.anchors:
            self.anchors.setdefault(a, (len(self.pages) - 1, self.y))
        for k, line in enumerate(lines):
            height = max(line.ascent + line.descent, (line.ascent + line.descent) * lh / 1.25)
            if height > self.room():
                self.new_page()
            baseline = self.y - line.ascent - (height - line.ascent - line.descent) / 2
            self.draw_line(line, x0, width, align, baseline)
            self.y -= height

    def list_marker(self, b, size, x0):
        if not b.marker:
            return
        slot = self.fonts.get("regular")
        w = slot.font.width(b.marker, size)
        baseline = self.y - size * 0.95 - size * 0.1
        self.page.ops.append(b"BT /%s %s Tf 1 0 0 1 %s %s Tm <%s> Tj ET\n" % (
            slot.name.encode(), _fmt(size), _fmt(x0 - w - size * 0.4), _fmt(baseline), self.encode(slot, b.marker)))

    def image_block(self, b: flowmod.Block):
        info = self.image(b.image)
        if info is None:
            return
        _name, _ref, w, h = info
        wpt = b.width or w * 0.75
        hpt = b.height or (h * 0.75 * (wpt / (w * 0.75)) if w else h * 0.75)
        max_h = self.ph - self.mt - self.mb
        scale = min(1.0, self.width / wpt if wpt else 1, max_h / hpt if hpt else 1)
        wpt, hpt = wpt * scale, hpt * scale
        if hpt > self.room() >= hpt * 0.7:
            s = self.room() / hpt  # a little smaller rather than a half-empty page
            wpt, hpt = wpt * s, hpt * s
        if hpt > self.room():
            if self.y < self.ph - self.mt - 1:
                self.new_page()
            if hpt > self.room():
                s = self.room() / hpt
                wpt, hpt = wpt * s, hpt * s
        x = self.ml + (self.width - wpt) / 2
        for a in b.anchors:
            self.anchors.setdefault(a, (len(self.pages) - 1, self.y))
        self.page.ops.append(b"q %s 0 0 %s %s %s cm /%s Do Q\n" % (_fmt(wpt), _fmt(hpt), _fmt(x), _fmt(self.y - hpt),
                                                                    info[0].encode()))
        self.y -= hpt

    def hr(self):
        w = self.width * 0.3
        x = self.ml + (self.width - w) / 2
        y = self.y - self.base * 0.6
        self.page.ops.append(b"0.6 G 0.5 w %s %s m %s %s l S 0 G\n" % (_fmt(x), _fmt(y), _fmt(x + w), _fmt(y)))
        self.y -= self.base * 1.2

    def table(self, b: flowmod.Block):
        size = self.base * 0.9
        pad = 3.0
        ncols = max(sum(c.colspan for c in row) for row in b.rows)
        mins = [size * 2] * ncols
        maxs = [size * 2] * ncols
        cell_items = []
        for row in b.rows:
            row_items = []
            col = 0
            for c in row:
                items = self.items(c.runs, size, scale=0.9)
                if c.header:
                    for it in items:
                        if it.slot is not None and it.slot is self.fonts.get("regular"):
                            it.slot = self.fonts.get("bold")
                            it.width = it.slot.font.width(it.text, it.size) if it.kind == "text" else it.width
                natural = sum(it.width for it in items if it.kind != "break")
                longest = 0.0
                cur = 0.0
                for it in items:
                    if it.kind in ("space", "break"):
                        longest, cur = max(longest, cur), 0.0
                    else:
                        cur += it.width
                longest = max(longest, cur)
                if c.colspan == 1 and col < ncols:
                    mins[col] = max(mins[col], longest + 2 * pad)
                    maxs[col] = max(maxs[col], natural + 2 * pad)
                row_items.append((c, items, col))
                col += c.colspan
            cell_items.append(row_items)
        total_max, total_min = sum(maxs), sum(mins)
        if total_max <= self.width:
            widths = maxs
        elif total_min >= self.width:
            widths = [m * self.width / total_min for m in mins]
        else:
            extra = self.width - total_min
            span = (total_max - total_min) or 1
            widths = [mins[i] + extra * (maxs[i] - mins[i]) / span for i in range(ncols)]
        lefts = [self.ml]
        for wv in widths:
            lefts.append(lefts[-1] + wv)
        for a in b.anchors:
            self.anchors.setdefault(a, (len(self.pages) - 1, self.y))
        for row_items in cell_items:
            laid = []
            height = 0.0
            for c, items, col in row_items:
                w = sum(widths[col:col + c.colspan]) - 2 * pad
                lines = self.lines(items, max(w, size), 0.0) if items else []
                h = sum((ln.ascent + ln.descent) * 1.1 for ln in lines) + 2 * pad
                laid.append((c, lines, col, w))
                height = max(height, h)
            if height > self.room() and height <= self.ph - self.mt - self.mb:
                self.new_page()
            # a row taller than what's left of the page continues on the next page, line by line
            pending = [list(lines) for _c, lines, _col, _w in laid]
            while True:
                room = self.room() - 2 * pad
                top = self.y
                slice_h = 0.0
                for k, (c, _lines, col, w) in enumerate(laid):
                    x0 = lefts[col] + pad
                    y = top - pad
                    used = 0.0
                    while pending[k]:
                        ln = pending[k][0]
                        lh = (ln.ascent + ln.descent) * 1.1
                        if used + lh > room and used > 0:
                            break
                        self.draw_line(ln, x0, w, c.align if c.align in ("center", "right") else "left", y - ln.ascent)
                        y -= lh
                        used += lh
                        pending[k].pop(0)
                    slice_h = max(slice_h, used + 2 * pad)
                for c, _lines, col, _w in laid:
                    right = lefts[min(col + c.colspan, ncols)]
                    self.page.ops.append(b"0.55 G 0.5 w %s %s %s %s re S 0 G\n" % (
                        _fmt(lefts[col]), _fmt(top - slice_h), _fmt(right - lefts[col]), _fmt(slice_h)))
                self.y -= slice_h
                if not any(pending):
                    break
                self.new_page()
        self.y -= size * 0.5

    def cover_page(self):
        cover = self.book.cover_image()
        if cover is None:
            return False
        info = self.image(cover.href)
        if info is None:
            return False
        p = self.new_page(numbered=False)
        name, _ref, w, h = info
        scale = min(self.pw / w, self.ph / h) if w and h else 1
        dw, dh = w * scale, h * scale
        p.ops.append(b"q %s 0 0 %s %s %s cm /%s Do Q\n" % (_fmt(dw), _fmt(dh), _fmt((self.pw - dw) / 2), _fmt((self.ph - dh) / 2),
                                                            name.encode()))
        return True

    # --- images ----------------------------------------------------------------------------------
    def image(self, href: str):
        if href in self.images:
            return self.images[href]
        res = self.book.resources.get(href)
        if res is None or not res.is_image:
            self.images[href] = None
            return None
        from .images import embed
        got = embed(self.writer, res.data)
        if got is None:
            self.images[href] = None
            return None
        ref, w, h = got
        name = f"Im{len(self.images) + 1}"
        self.images[href] = (name, ref, w, h)
        return self.images[href]

    # --- the document ------------------------------------------------------------------------------
    def run(self) -> bytes:
        book = self.book
        blocks = flowmod.visible(flowmod.book_blocks(book))
        has_cover = self.cover_page()
        cover_href = book.cover
        self.new_page()
        first = True
        for b in blocks:
            if b.kind == "image" and b.image == cover_href and has_cover and first:
                for a in b.anchors:
                    self.anchors.setdefault(a, (0, self.ph))
                continue
            if b.page_break and not first and self.y < self.ph - self.mt - 1:
                self.new_page()
            first = False
            size = self.base * (b.size if b.size and 0.6 < b.size < 3.5 else 1.0)
            space_before = min(max(b.margin_top, 0.0), 3.0) * self.base
            gap = max(space_before, self.prev_bottom)
            if self.y < self.ph - self.mt - 1:
                self.y -= gap
            if b.kind == "hr":
                self.hr()
            elif b.kind == "image":
                self.image_block(b)
            elif b.kind == "table" and b.rows:
                self.table(b)
            else:
                indent_l = min(b.margin_left, 12.0) * self.base
                indent_r = min(b.margin_right, 8.0) * self.base
                if b.marker:
                    indent_l = max(indent_l, (b.depth or 1) * self.base * 1.6)
                if b.quote:
                    indent_l, indent_r = max(indent_l, self.base * 1.5), max(indent_r, self.base * 1.5)
                indent_l = min(indent_l, self.width * 0.4)
                indent_r = min(indent_r, self.width * 0.25)
                x0 = self.ml + indent_l
                width = self.width - indent_l - indent_r
                if b.marker:
                    self.ensure(size * 1.4)
                    self.list_marker(b, size, x0)
                self.paragraph(b, size, x0, width, mono=b.kind == "pre")
            self.prev_bottom = min(max(b.margin_bottom, 0.0), 3.0) * self.base
        return self.write()

    def write(self) -> bytes:
        w = self.writer
        md = self.book.metadata
        catalog = w.reserve()
        pages_ref = w.reserve()
        font_refs = {}
        for slot in self.fonts.slots.values():
            if slot.used:
                slot.ref = w.reserve()
                font_refs[slot.name] = slot.ref
        page_refs = [w.reserve() for _ in self.pages]
        dest_names = {}
        for key, (pi, y) in self.anchors.items():
            if 0 <= pi < len(page_refs):
                dest_names[key] = (f"d{len(dest_names) + 1}", pi, y)
        resources = {"Font": {name: ref for name, ref in font_refs.items()},
                     "XObject": {info[0]: info[1] for info in self.images.values() if info is not None},
                     "ProcSet": [Name("PDF"), Name("Text"), Name("ImageC"), Name("ImageB")]}
        res_ref = w.add(resources)
        num_slot = self.fonts.get("regular")
        for i, page in enumerate(self.pages):
            ops = list(page.ops)
            if page.number:
                label = str(page.number)
                size = self.base * 0.72
                x = (self.pw - num_slot.font.width(label, size)) / 2
                ops.append(b"BT /%s %s Tf 0.35 g 1 0 0 1 %s %s Tm <%s> Tj 0 g ET\n" % (
                    num_slot.name.encode(), _fmt(size), _fmt(x), _fmt(self.mb * 0.45), self.encode(num_slot, label)))
            content = w.stream(b"".join(ops))
            annots = []
            for x0, y0, x1, y1, href in page.links:
                rect = [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]
                if is_external(href):
                    annots.append(w.add({"Type": Name("Annot"), "Subtype": Name("Link"), "Rect": rect, "Border": [0, 0, 0],
                                         "A": {"S": Name("URI"), "URI": href}}))
                else:
                    target = dest_names.get(href) or dest_names.get(href.split("#")[0])
                    if target:
                        annots.append(w.add({"Type": Name("Annot"), "Subtype": Name("Link"), "Rect": rect,
                                             "Border": [0, 0, 0], "Dest": target[0]}))
            pd = {"Type": Name("Page"), "Parent": pages_ref, "MediaBox": [0, 0, round(self.pw, 2), round(self.ph, 2)],
                  "Contents": content, "Resources": res_ref}
            if annots:
                pd["Annots"] = annots
            w.put(page_refs[i], pd)
        # fonts last: now every used glyph is known
        for slot in self.fonts.slots.values():
            if slot.ref is not None:
                self.font_objects(slot)
        w.put(pages_ref, {"Type": Name("Pages"), "Kids": page_refs, "Count": len(page_refs)})
        cat = {"Type": Name("Catalog"), "Pages": pages_ref, "ViewerPreferences": {"DisplayDocTitle": True}}
        if md.language:
            cat["Lang"] = md.language
        if dest_names:
            names = sorted((n, pi, y) for n, pi, y in dest_names.values())
            arr = []
            for n, pi, y in names:
                arr += [n, [page_refs[pi], Name("XYZ"), 0, round(min(y + 2, self.ph), 2), None]]
            cat["Names"] = {"Dests": w.add({"Names": arr})}
        outline = self.outline(page_refs, dest_names)
        if outline is not None:
            cat["Outlines"] = outline
            cat["PageMode"] = Name("UseOutlines")
        w.put(catalog, cat)
        now = datetime.datetime.now(datetime.timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        info = {"Title": md.title or "Untitled", "Producer": "bookport", "Creator": "bookport", "CreationDate": now,
                "ModDate": now}
        if md.authors:
            info["Author"] = ", ".join(md.authors)
        if md.subjects:
            info["Keywords"] = ", ".join(md.subjects)
        if md.description:
            info["Subject"] = re.sub(r"<[^>]+>", "", md.description)[:2000]
        info_ref = w.add(info)
        fid = hashlib.md5((md.uid() + now).encode()).digest()
        return w.finish(catalog, info_ref, fid)

    def outline(self, page_refs, dest_names):
        entries = self.book.toc
        if not entries:
            return None
        w = self.writer
        root = w.reserve()

        def target(e):
            t = dest_names.get(e.href) or dest_names.get(e.href.split("#")[0])
            if t is None:
                return None
            _n, pi, y = t
            return [page_refs[pi], Name("XYZ"), 0, round(min(y + 2, self.ph), 2), None]

        def build(items, parent):
            refs = []
            kept = []
            for e in items:
                dest = target(e)
                kids = e.children
                if dest is None and not kids:
                    continue
                kept.append((e, dest))
                refs.append(w.reserve())
            total = 0
            for k, ((e, dest), ref) in enumerate(zip(kept, refs)):
                d = {"Title": " ".join(e.title.split()) or "Untitled", "Parent": parent}
                if dest is not None:
                    d["Dest"] = dest
                elif e.children:
                    first_dest = next((target(c) for _dd, c in e.walk() if target(c)), None)
                    if first_dest:
                        d["Dest"] = first_dest
                if k > 0:
                    d["Prev"] = refs[k - 1]
                if k + 1 < len(refs):
                    d["Next"] = refs[k + 1]
                child_refs, child_count = build(e.children, ref)
                if child_refs:
                    d["First"] = child_refs[0]
                    d["Last"] = child_refs[-1]
                    d["Count"] = -child_count if False else child_count
                w.put(ref, d)
                total += 1 + child_count
            return refs, total

        refs, count = build(entries, root)
        if not refs:
            return None
        w.put(root, {"Type": Name("Outlines"), "First": refs[0], "Last": refs[-1], "Count": count})
        return root

    def font_objects(self, slot: Slot):
        w = self.writer
        f = slot.font
        gids = sorted(slot.used)
        sub, mapping = ttf.subset(f, gids)
        file_ref = w.stream(sub, {"Length1": len(sub)})
        scale = 1000.0 / f.units
        max_cid = max(gids) if gids else 0
        cid_to_gid = bytearray(2 * (max_cid + 1))
        for old, new in mapping.items():
            if old <= max_cid:
                cid_to_gid[2 * old:2 * old + 2] = struct.pack(">H", new)
        c2g_ref = w.stream(bytes(cid_to_gid))
        widths = []
        run_start = None
        run = []
        for g in gids:
            adv = round(f.advance(g) * scale)
            if run_start is not None and g == run_start + len(run):
                run.append(adv)
            else:
                if run_start is not None:
                    widths += [run_start, run]
                run_start, run = g, [adv]
        if run_start is not None:
            widths += [run_start, run]
        tag = "".join(chr(65 + (b % 26)) for b in hashlib.md5(slot.name.encode() + bytes(str(gids), "ascii")).digest()[:6])
        base_name = f"{tag}+{f.name}"
        flags = 4 | (1 if f.fixed else 0) | (64 if f.italic_angle else 0) | (262144 if f.bold else 0)
        bbox = [int(v * scale) for v in f.bbox]
        descriptor = w.add({"Type": Name("FontDescriptor"), "FontName": Name(base_name), "Flags": flags, "FontBBox": bbox,
                            "ItalicAngle": round(f.italic_angle, 2), "Ascent": int(f.ascent * scale),
                            "Descent": int(f.descent * scale), "CapHeight": int((f.cap_height or f.ascent) * scale),
                            "StemV": 120 if f.bold else 80, "FontFile2": file_ref})
        cid_font = w.add({"Type": Name("Font"), "Subtype": Name("CIDFontType2"), "BaseFont": Name(base_name),
                          "CIDSystemInfo": {"Registry": "Adobe", "Ordering": "Identity", "Supplement": 0},
                          "FontDescriptor": descriptor, "DW": int(f.advance(0) * scale), "W": widths,
                          "CIDToGIDMap": c2g_ref})
        to_unicode = w.stream(_to_unicode(slot.used))
        w.put(slot.ref, {"Type": Name("Font"), "Subtype": Name("Type0"), "BaseFont": Name(base_name),
                         "Encoding": Name("Identity-H"), "DescendantFonts": [cid_font], "ToUnicode": to_unicode})


def _to_unicode(used: dict[int, str]) -> bytes:
    lines = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
             b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def",
             b"1 begincodespacerange", b"<0000> <FFFF>", b"endcodespacerange"]
    items = sorted(used.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(b"%d beginbfchar" % len(chunk))
        for gid, text in chunk:
            lines.append(b"<%04X> <%s>" % (gid, text.encode("utf-16-be").hex().upper().encode()))
        lines.append(b"endbfchar")
    lines += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(lines)


def typeset(book: Book, page_size: str | None = None, text_size: str | None = None) -> bytes:
    from .. import clean
    b = book.copy()
    clean.clean_book(b)
    if not b.toc:
        b.ensure_toc()
    return Typesetter(b, page_size, text_size).run()
