"""Fonts: what a PostScript font dictionary becomes in the PDF.

- Type 1 fonts defined in the program are rebuilt from their dictionaries (only the glyphs used, all Subrs) and
  embedded as FontFile; widths come from each charstring's hsbw/sbw (Type 1 Font Format, chapter 6).
- Type 42 fonts (TrueType in PostScript) are embedded as CID TrueType subsets, the CIDs being glyph numbers.
- Type 3 fonts become PDF Type 3 fonts: each glyph's BuildGlyph/BuildChar is run once and what it paints is
  kept as its CharProc.
- Fonts asked for by name and not in the program are the PDF standard 14, with Adobe's Core 14 metrics (the
  AFM files in ``afm/``); others are drawn with the nearest of those, with a note.

PDF text space: Type 1 and standard fonts are 1/1000 units (their glyph spaces are written as 0.001), TrueType
1/1000 em, Type 3 the font's own FontMatrix. The text matrix makes up the rest of each font's matrix."""

from __future__ import annotations

import hashlib
import struct
from functools import cache
from importlib import resources

from ..pdf import ttf
from ..pdf.encodings import glyph_to_unicode
from ..pdf.objects import Name as PdfName
from ..pdf.objects import Ref
from .cff import _ASCII
from .files import decrypt, encrypt
from .objects import Name, Operator, PSArray, PSDict, PSString, XName


def _encoding(upper: dict[int, str], ascii_changes: dict[int, str] | None = None) -> list[str]:
    enc = [".notdef"] * 256
    for i, n in enumerate(_ASCII):
        enc[32 + i] = n
    for c, n in (ascii_changes or {}).items():
        enc[c] = n
    for c, n in upper.items():
        enc[c] = n
    return enc


STANDARD = _encoding({
    0xA1: "exclamdown", 0xA2: "cent", 0xA3: "sterling", 0xA4: "fraction", 0xA5: "yen", 0xA6: "florin",
    0xA7: "section", 0xA8: "currency", 0xA9: "quotesingle", 0xAA: "quotedblleft", 0xAB: "guillemotleft",
    0xAC: "guilsinglleft", 0xAD: "guilsinglright", 0xAE: "fi", 0xAF: "fl", 0xB1: "endash", 0xB2: "dagger",
    0xB3: "daggerdbl", 0xB4: "periodcentered", 0xB6: "paragraph", 0xB7: "bullet", 0xB8: "quotesinglbase",
    0xB9: "quotedblbase", 0xBA: "quotedblright", 0xBB: "guillemotright", 0xBC: "ellipsis", 0xBD: "perthousand",
    0xBF: "questiondown", 0xC1: "grave", 0xC2: "acute", 0xC3: "circumflex", 0xC4: "tilde", 0xC5: "macron",
    0xC6: "breve", 0xC7: "dotaccent", 0xC8: "dieresis", 0xCA: "ring", 0xCB: "cedilla", 0xCD: "hungarumlaut",
    0xCE: "ogonek", 0xCF: "caron", 0xD0: "emdash", 0xE1: "AE", 0xE3: "ordfeminine", 0xE8: "Lslash", 0xE9: "Oslash",
    0xEA: "OE", 0xEB: "ordmasculine", 0xF1: "ae", 0xF5: "dotlessi", 0xF8: "lslash", 0xF9: "oslash", 0xFA: "oe",
    0xFB: "germandbls"})

_LATIN1_UPPER = ["space", "exclamdown", "cent", "sterling", "currency", "yen", "brokenbar", "section", "dieresis", "copyright", "ordfeminine", "guillemotleft", "logicalnot", "hyphen", "registered", "macron", "degree", "plusminus", "twosuperior", "threesuperior", "acute", "mu", "paragraph", "periodcentered", "cedilla", "onesuperior", "ordmasculine", "guillemotright", "onequarter", "onehalf", "threequarters", "questiondown", "Agrave", "Aacute", "Acircumflex", "Atilde", "Adieresis", "Aring", "AE", "Ccedilla", "Egrave", "Eacute", "Ecircumflex", "Edieresis", "Igrave", "Iacute", "Icircumflex", "Idieresis", "Eth", "Ntilde", "Ograve", "Oacute", "Ocircumflex", "Otilde", "Odieresis", "multiply", "Oslash", "Ugrave", "Uacute", "Ucircumflex", "Udieresis", "Yacute", "Thorn", "germandbls", "agrave", "aacute", "acircumflex", "atilde", "adieresis", "aring", "ae", "ccedilla", "egrave", "eacute", "ecircumflex", "edieresis", "igrave", "iacute", "icircumflex", "idieresis", "eth", "ntilde", "ograve", "oacute", "ocircumflex", "otilde", "odieresis", "divide", "oslash", "ugrave", "uacute", "ucircumflex", "udieresis", "yacute", "thorn", "ydieresis"]
ISOLATIN1 = _encoding(
    {**{0xA0 + i: n for i, n in enumerate(_LATIN1_UPPER)},
     0x90: "dotlessi", 0x91: "grave", 0x92: "acute", 0x93: "circumflex", 0x94: "tilde", 0x95: "macron",
     0x96: "breve", 0x97: "dotaccent", 0x98: "dieresis", 0x9A: "ring", 0x9B: "cedilla", 0x9D: "hungarumlaut",
     0x9E: "ogonek", 0x9F: "caron"}, {0x2D: "minus"})


def to_unicode(name: str) -> str | None:
    u = glyph_to_unicode(name)
    if u is None and name.startswith("uni") and len(name) >= 7:
        try:
            return chr(int(name[3:7], 16))
        except ValueError:
            return None
    if u is None and name.startswith("u") and 5 <= len(name) <= 7:
        try:
            return chr(int(name[1:], 16))
        except ValueError:
            return None
    return u


# ---- Adobe's Core 14 metrics

class AFM:
    def __init__(self, name: str):
        self.name = name
        self.widths: dict[str, float] = {}
        self.boxes: dict[str, tuple] = {}
        self.builtin: dict[int, str] = {}
        self.bbox = (0, -200, 1000, 800)
        self.cap, self.asc, self.desc, self.italic, self.fixed, self.stdvw = 700, 750, -250, 0.0, False, 80
        self.symbolic = name in ("Symbol", "ZapfDingbats")
        text = resources.files(__package__).joinpath("afm", f"{name}.afm").read_text("latin-1")
        for line in text.splitlines():
            if line.startswith("C "):
                parts = dict(p.strip().split(" ", 1) for p in line.split(";") if p.strip() and " " in p.strip())
                n = parts.get("N")
                if n is None:
                    continue
                self.widths[n] = float(parts.get("WX", "0"))
                if "B" in parts:
                    self.boxes[n] = tuple(float(v) for v in parts["B"].split())
                c = int(parts.get("C", "-1"))
                if c >= 0:
                    self.builtin[c] = n
                continue
            key, _, val = line.partition(" ")
            if key == "FontBBox":
                self.bbox = tuple(float(v) for v in val.split())
            elif key == "CapHeight":
                self.cap = float(val)
            elif key == "Ascender":
                self.asc = float(val)
            elif key == "Descender":
                self.desc = float(val)
            elif key == "ItalicAngle":
                self.italic = float(val)
            elif key == "IsFixedPitch":
                self.fixed = val.strip() == "true"
            elif key == "StdVW":
                self.stdvw = float(val)


CORE14 = ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique", "Helvetica", "Helvetica-Bold",
          "Helvetica-Oblique", "Helvetica-BoldOblique", "Times-Roman", "Times-Bold", "Times-Italic",
          "Times-BoldItalic", "Symbol", "ZapfDingbats")


@cache
def afm(name: str) -> AFM:
    return AFM(name)


def standard_for(name: str) -> tuple[str, bool]:
    """The standard font a font name is drawn with, and whether that's the font itself."""
    if name in CORE14:
        return name, True
    low = name.lower().replace(" ", "").replace("-", "").replace("_", "")
    for alias, core in (("timesroman", "Times-Roman"), ("timesnewromanpsmt", "Times-Roman"),
                        ("arialmt", "Helvetica"), ("helveticaneue", "Helvetica"), ("couriernewpsmt", "Courier")):
        if low == alias:
            return core, False
    if "symbol" in low and "dingbat" not in low:
        return "Symbol", low == "symbol"
    if "dingbat" in low:
        return "ZapfDingbats", False
    bold = any(w in low for w in ("bold", "black", "heavy", "demi", "semibold"))
    italic = any(w in low for w in ("italic", "oblique", "slanted", "kursiv"))
    if any(w in low for w in ("courier", "mono", "consol", "typewriter", "code", "fixed")):
        family = "Courier"
    elif any(w in low for w in ("times", "roman", "serif", "garamond", "georgia", "palatino", "book", "century",
                                "schoolbook", "minion", "caslon", "baskerville", "bodoni", "cambria", "chancery",
                                "goudy", "utopia", "charter", "cmr", "nimbusrom")) and "sans" not in low:
        family = "Times"
    else:
        family = "Helvetica"
    if family == "Times":
        style = {(False, False): "Roman", (True, False): "Bold", (False, True): "Italic", (True, True): "BoldItalic"}
        return f"Times-{style[bold, italic]}", False
    style = {(False, False): "", (True, False): "-Bold", (False, True): "-Oblique", (True, True): "-BoldOblique"}
    return family + style[bold, italic], False


# ---- PostScript syntax for rebuilt fonts

def ps_syntax(v, depth: int = 0) -> bytes:
    t = type(v)
    if depth > 32:
        return b"null"
    if t is bool:
        return b"true" if v else b"false"
    if t is int:
        return b"%d" % v
    if t is float:
        return (b"%.8f" % v).rstrip(b"0").rstrip(b".") or b"0"
    if t is Name:
        return b"/" + v.encode("latin-1", "replace")
    if t is XName:
        return v.encode("latin-1", "replace")
    if t is Operator:
        return v.name.encode("latin-1", "replace")
    if t is PSString:
        return b"<" + v.bytes().hex().encode() + b">"
    if t is PSArray:
        inner = b" ".join(ps_syntax(x, depth + 1) for x in v.list())
        return (b"{" + inner + b"}") if v.x else (b"[" + inner + b"]")
    return b"null"


# ---- Type 1 charstrings

def _cs_numbers(data: bytes):
    """Type 1 charstring tokens: numbers and (op,) tuples."""
    i, n = 0, len(data)
    while i < n:
        v = data[i]
        if v >= 32:
            if v <= 246:
                yield v - 139
                i += 1
            elif v <= 250:
                yield (v - 247) * 256 + data[i + 1] + 108 if i + 1 < n else 0
                i += 2
            elif v <= 254:
                yield -(v - 251) * 256 - (data[i + 1] if i + 1 < n else 0) - 108
                i += 2
            else:
                yield struct.unpack(">i", data[i + 1:i + 5].ljust(4, b"\0"))[0]
                i += 5
        elif v == 12:
            yield (12, data[i + 1] if i + 1 < n else 0)
            i += 2
        else:
            yield (v,)
            i += 1


def charstring_info(cs: bytes, len_iv: int) -> tuple[float, float, tuple | None]:
    """A Type 1 glyph's width (wx, wy) and, for an accented glyph made with seac, its two StandardEncoding codes."""
    data = decrypt(cs, 4330, len_iv) if len_iv >= 0 else cs
    stack: list = []
    wx = wy = 0.0
    seac = None
    for tok in _cs_numbers(data):
        if isinstance(tok, tuple):
            if tok == (13,):  # hsbw
                if len(stack) >= 2:
                    wx = float(stack[1])
                return wx, wy, _find_seac(data)
            if tok == (12, 7):  # sbw
                if len(stack) >= 4:
                    wx, wy = float(stack[2]), float(stack[3])
                return wx, wy, _find_seac(data)
            if tok == (12, 12) and len(stack) >= 2:  # div
                b = stack.pop()
                a = stack.pop()
                stack.append(a / b if b else 0)
                continue
            if tok == (12, 6) and len(stack) >= 5:  # seac before a width (not allowed, but harmless)
                seac = (int(stack[3]), int(stack[4]))
            stack = []
            continue
        stack.append(tok)
        if len(stack) > 48:
            break
    return wx, wy, seac


def _find_seac(data: bytes):
    stack: list = []
    for tok in _cs_numbers(data):
        if isinstance(tok, tuple):
            if tok == (12, 6) and len(stack) >= 5:
                return int(stack[-2]), int(stack[-1])
            if tok == (12, 12) and len(stack) >= 2:
                b = stack.pop()
                a = stack.pop()
                stack.append(a / b if b else 0)
                continue
            if tok not in ((10,), (12, 16), (12, 17)):
                stack = []
            continue
        stack.append(tok)
    return None


# ---- font programs: what a defined font is, shared by its scaled and re-encoded copies

def _d(fd: PSDict, k: str, default=None):
    return fd.d.get(k, default)


def _matrix(v) -> tuple:
    if type(v) is PSArray and v.length == 6:
        vals = v.list()
        if all(type(x) in (int, float) for x in vals):
            return tuple(float(x) for x in vals)
    return (0.001, 0.0, 0.0, 0.001, 0.0, 0.0)


class Program:
    kind = ""
    pdf_matrix = (0.001, 0.0, 0.0, 0.001, 0.0, 0.0)

    def __init__(self, fd: PSDict):
        self.name = str(_d(fd, "FontName", "Font"))
        self.base_matrix = _matrix(_d(fd, "FontMatrix"))
        self.pdf_fonts: dict = {}

    def width(self, fd: PSDict, name: str, code: int) -> tuple[float, float]:
        """The glyph's advance in glyph space, Metrics taken into account."""
        m = _d(fd, "Metrics")
        if type(m) is PSDict:
            v = m.d.get(name)
            if type(v) in (int, float):
                return float(v), 0.0
            if type(v) is PSArray:
                vals = [float(x) for x in v.list() if type(x) in (int, float)]
                if len(vals) == 2:
                    return vals[1], 0.0
                if len(vals) == 4:
                    return vals[2], vals[3]
        return self.own_width(name, code)

    def own_width(self, name: str, code: int) -> tuple[float, float]:
        return 0.0, 0.0


class Standard(Program):
    """A standard font (by name, not in the program)."""
    kind = "standard"

    def __init__(self, fd: PSDict, base: str):
        super().__init__(fd)
        self.base = base
        self.afm = afm(base)

    def own_width(self, name, code):
        w = self.afm.widths.get(name)
        if w is None:
            w = self.afm.widths.get(".notdef", self.afm.widths.get("space", 250.0))
        return w, 0.0


class Type1(Program):
    kind = "type1"

    def __init__(self, fd: PSDict):
        super().__init__(fd)
        self.fd = fd
        cs = _d(fd, "CharStrings")
        self.charstrings: dict = cs.d if type(cs) is PSDict else {}
        priv = _d(fd, "Private")
        self.private: dict = priv.d if type(priv) is PSDict else {}
        self.len_iv = int(self.private.get("lenIV", 4)) if type(self.private.get("lenIV", 4)) is int else 4
        self.info: dict = {}

    def glyph_info(self, name: str):
        got = self.info.get(name)
        if got is None:
            s = self.charstrings.get(name)
            got = charstring_info(s.bytes(), self.len_iv) if type(s) is PSString else (0.0, 0.0, None)
            self.info[name] = got
        return got

    def own_width(self, name, code):
        if name not in self.charstrings:
            name = ".notdef"
        wx, wy, _ = self.glyph_info(name)
        return wx, wy

    def program(self, names: set[str]) -> tuple[bytes, int, int, int]:
        """The font rebuilt with these glyphs (and what seac needs): (data, Length1, Length2, Length3)."""
        keep = {".notdef"} | {n for n in names if n in self.charstrings}
        for n in list(keep):
            seac = self.glyph_info(n)[2]
            if seac:
                for c in seac:
                    if 0 <= c < 256 and STANDARD[c] in self.charstrings:
                        keep.add(STANDARD[c])
        fname = self.name.encode("latin-1", "replace").replace(b" ", b"")
        bbox = _d(self.fd, "FontBBox")
        bb = [float(x) for x in bbox.list()] if type(bbox) is PSArray and bbox.length == 4 else [0, 0, 0, 0]
        clear = (b"%!FontType1-1.0: " + fname + b"\n12 dict begin\n/FontName /" + fname + b" def\n"
                 b"/PaintType " + b"%d" % int(_d(self.fd, "PaintType", 0) or 0) + b" def\n/FontType 1 def\n"
                 b"/FontMatrix [0.001 0 0 0.001 0 0] readonly def\n"
                 b"/FontBBox {" + b" ".join(ps_syntax(v) for v in bb) + b"} readonly def\n"
                 b"/Encoding StandardEncoding def\ncurrentdict end\ncurrentfile eexec\n")
        priv = [b"dup /Private 20 dict dup begin",
                b"/RD{string currentfile exch readstring pop}executeonly def",
                b"/ND{noaccess def}executeonly def", b"/NP{noaccess put}executeonly def",
                b"/MinFeature{16 16}def", b"/password 5839 def"]
        for key in ("BlueValues", "OtherBlues", "FamilyBlues", "FamilyOtherBlues", "BlueScale", "BlueShift",
                    "BlueFuzz", "StdHW", "StdVW", "StemSnapH", "StemSnapV", "ForceBold", "LanguageGroup",
                    "ExpansionFactor", "RndStemUp", "lenIV", "OtherSubrs"):
            if key in self.private:
                v = self.private[key]
                if key == "OtherSubrs" and type(v) is not PSArray:
                    continue
                priv.append(b"/" + key.encode() + b" " + ps_syntax(v) + b" def")
        body = bytearray(b"\n".join(priv) + b"\n")
        subrs = self.private.get("Subrs")
        if type(subrs) is PSArray:
            items = subrs.list()
            body += b"/Subrs %d array\n" % len(items)
            for i, s in enumerate(items):
                data = s.bytes() if type(s) is PSString else b""
                body += b"dup %d %d RD " % (i, len(data)) + data + b" NP\n"
            body += b"ND\n"
        body += b"2 index /CharStrings %d dict dup begin\n" % len(keep)
        for n in sorted(keep):
            s = self.charstrings.get(n)
            data = s.bytes() if type(s) is PSString else b""
            body += b"/" + n.encode("latin-1", "replace") + b" %d RD " % len(data) + data + b" ND\n"
        body += b"end\nend\nreadonly put\nnoaccess put\ndup /FontName get exch definefont pop\n" \
                b"mark currentfile closefile\n"
        lead = b"\x00\x00\x00\x00"
        enc = encrypt(lead + bytes(body), 55665)
        if all(c in b"0123456789abcdefABCDEF" for c in enc[:4]):
            enc = encrypt(b"\xff\xff\xff\xff" + bytes(body), 55665)
        trailer = (b"0" * 64 + b"\n") * 8 + b"cleartomark\n"
        return clear + enc + trailer, len(clear), len(enc), len(trailer)


class Type42(Program):
    kind = "type42"
    pdf_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def __init__(self, fd: PSDict):
        super().__init__(fd)
        sf = _d(fd, "sfnts")
        parts = [s.bytes() for s in sf.list() if type(s) is PSString] if type(sf) is PSArray else []
        self.font = _load_sfnt(parts)
        cs = _d(fd, "CharStrings")
        self.gids: dict = {}
        if type(cs) is PSDict:
            for k, v in cs.d.items():
                if type(v) is int:
                    self.gids[str(k)] = v
        self.used: dict[int, str] = {}

    def gid(self, name: str) -> int:
        g = self.gids.get(name, self.gids.get(".notdef", 0))
        return g if self.font and 0 <= g < self.font.num_glyphs else 0

    def own_width(self, name, code):
        f = self.font
        if f is None:
            return 0.5, 0.0
        return f.advance(self.gid(name)) / f.units, 0.0


def _load_sfnt(parts: list[bytes]):
    """A TrueType font from a Type 42 sfnts array: strings may carry a padding byte at their end."""
    for data in (b"".join(p[:-1] if len(p) % 2 else p for p in parts), b"".join(parts)):
        try:
            return _tolerant_load(data)
        except Exception:
            continue
    return None


def _tolerant_load(data: bytes):
    try:
        return ttf.load(data)
    except Exception:
        pass
    num = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for i in range(num):
        rec = data[12 + 16 * i:28 + 16 * i]
        off, length = struct.unpack(">II", rec[8:16])
        if off + length > len(data) + 3:
            raise ValueError("table outside the font")
        tables[rec[:4].decode("latin-1")] = (off, length)
    for t in ("head", "hhea", "maxp", "hmtx", "loca", "glyf"):
        if t not in tables:
            raise ValueError(f"no {t}")
    f = ttf.Font(data, tables)
    head = f.table("head")
    f.units = struct.unpack(">H", head[18:20])[0] or 1000
    f.bbox = struct.unpack(">hhhh", head[36:44])
    f.long_loca = struct.unpack(">h", head[50:52])[0] == 1
    hhea = f.table("hhea")
    f.ascent, f.descent, f.line_gap = struct.unpack(">hhh", hhea[4:10])
    n_h = struct.unpack(">H", hhea[34:36])[0]
    f.num_glyphs = struct.unpack(">H", f.table("maxp")[4:6])[0]
    hmtx = f.table("hmtx")
    adv = [struct.unpack(">H", hmtx[4 * i:4 * i + 2])[0] for i in range(min(n_h, len(hmtx) // 4))]
    f.advances = adv + [adv[-1] if adv else f.units // 2] * max(0, f.num_glyphs - len(adv))
    f.cmap = {}
    return f


class CFF(Program):
    """A font from a CFF font set (FontSetInit's StartData): FontType 2."""
    kind = "cff"

    def __init__(self, fd: PSDict, data: bytes, font):
        super().__init__(fd)
        self.fd, self.data, self.font = fd, data, font
        self.gids = {n: i for i, n in enumerate(font.names)}

    def own_width(self, name, code):
        return self.font.width(self.gids.get(name, 0)), 0.0


class Type3(Program):
    kind = "type3"

    def __init__(self, fd: PSDict):
        super().__init__(fd)
        self.fd = fd
        self.pdf_matrix = self.base_matrix
        self.glyphs: dict = {}  # name or code -> (content, wx, wy, bbox or None)
        self.by_char = "BuildGlyph" not in fd.d
        self.names: tuple = ({}, {})  # the glyphs' resources and their names: one Resources for the whole font

    def own_width(self, name, code):
        g = self.glyphs.get(code if self.by_char else name)
        return (g[1], g[2]) if g else (0.0, 0.0)


# ---- the PDF fonts

def tag(text: str) -> str:
    return "".join(chr(65 + (b % 26)) for b in hashlib.md5(text.encode("utf-8", "replace")).digest()[:6])


def unicode_map(entries: dict[int, str], two_byte: bool) -> bytes:
    lines = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
             b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def", b"1 begincodespacerange",
             b"<0000> <FFFF>" if two_byte else b"<00> <FF>", b"endcodespacerange"]
    items = sorted((c, t) for c, t in entries.items() if t)
    fmt = b"<%04X> <%s>" if two_byte else b"<%02X> <%s>"
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(b"%d beginbfchar" % len(chunk))
        for c, t in chunk:
            lines.append(fmt % (c, t.encode("utf-16-be").hex().upper().encode()))
        lines.append(b"endbfchar")
    lines += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(lines)


class SimpleFont:
    """A PDF font with one-byte codes: a Type 1 or standard font with an encoding, or a Type 3 font."""

    def __init__(self, doc, prog: Program):
        self.prog = prog
        self.ref: Ref = doc.w.reserve()
        self.codes: dict[int, str] = {}  # code -> glyph name
        self.widths: dict[int, float] = {}
        doc.fonts.append(self)

    def use(self, code: int, name: str, wx: float) -> bool:
        """Note a code; False when it's already used for another glyph (another PDF font is needed)."""
        have = self.codes.get(code)
        if have is not None and have != name:
            return False
        self.codes[code] = name
        self.widths[code] = wx
        return True

    def finish(self, doc) -> None:
        w = doc.w
        prog = self.prog
        if not self.codes:
            self.codes = {32: "space"}
            self.widths = {32: 0.0}
        codes = sorted(self.codes)
        first, last = codes[0], codes[-1]
        widths = [_r(self.widths.get(c, 0.0)) for c in range(first, last + 1)]
        diffs: list = []
        prev = None
        for c in codes:
            if prev is None or c != prev + 1:
                diffs.append(c)
            diffs.append(PdfName(self._glyph_name(c)))
            prev = c
        uni = {c: to_unicode(self.codes[c]) for c in codes}
        d = {"Type": PdfName("Font"), "FirstChar": first, "LastChar": last, "Widths": widths,
             "Encoding": {"Type": PdfName("Encoding"), "Differences": diffs}}
        if any(uni.values()):
            d["ToUnicode"] = w.stream(unicode_map(uni, False))
        if prog.kind == "standard":
            d.update({"Subtype": PdfName("Type1"), "BaseFont": PdfName(prog.base)})
        elif prog.kind in ("type1", "cff"):
            names = set(self.codes.values())
            if prog.kind == "type1":
                data, l1, l2, l3 = prog.program(names)
                ff_key, ff = "FontFile", w.stream(data, {"Length1": l1, "Length2": l2, "Length3": l3})
                base = f"{tag(prog.name + ' '.join(sorted(names)))}+{prog.name.replace(' ', '')}"
            else:  # the CFF as the program gave it (all its glyphs)
                ff_key, ff = "FontFile3", w.stream(prog.data, {"Subtype": PdfName("Type1C")})
                base = prog.name.replace(" ", "")
            bbox = _d(prog.fd, "FontBBox")
            bb = [float(x) for x in bbox.list()] if type(bbox) is PSArray and bbox.length == 4 else [0, 0, 0, 0]
            symbolic = any(n not in _LATIN_NAMES for n in names)
            desc = w.add({"Type": PdfName("FontDescriptor"), "FontName": PdfName(base),
                          "Flags": 4 if symbolic else 32, "FontBBox": [_r(v) for v in bb], "ItalicAngle": 0,
                          "Ascent": _r(bb[3]), "Descent": _r(bb[1]), "CapHeight": _r(bb[3]), "StemV": 80,
                          ff_key: ff})
            d.update({"Subtype": PdfName("Type1"), "BaseFont": PdfName(base), "FontDescriptor": desc})
        else:  # Type 3
            procs = {}
            res: dict = {}
            for c in codes:
                key = c if prog.by_char else self.codes[c]
                g = prog.glyphs.get(key)
                content, wx, wy, box = g if g else (None, 0.0, 0.0, None)
                head = (b"%s %s %s %s %s %s d1\n" % tuple(_n(v) for v in (wx, wy, *box)) if box
                        else b"%s %s d0\n" % (_n(wx), _n(wy)))
                body = content.data() if content is not None else b""
                procs[self._glyph_name(c)] = w.stream(head + body)
                if content is not None:
                    for cat, items in content.resources().items():
                        res.setdefault(cat, {}).update(items)
            bbox = _d(prog.fd, "FontBBox")
            bb = [float(x) for x in bbox.list()] if type(bbox) is PSArray and bbox.length == 4 else [0, 0, 0, 0]
            d.update({"Subtype": PdfName("Type3"), "FontBBox": [_r(v) for v in bb],
                      "FontMatrix": [_r(v, 8) for v in prog.base_matrix], "CharProcs": procs, "Resources": res})
        w.put(self.ref, d)

    def _glyph_name(self, c: int) -> str:
        if self.prog.kind == "type3" and self.prog.by_char:
            return f"c{c}"
        return self.codes[c]


_LATIN_NAMES = set(STANDARD) | set(ISOLATIN1)


class CIDFont:
    """A Type 42 font as a CID TrueType font: two-byte codes that are the font's own glyph numbers."""

    def __init__(self, doc, prog: Type42):
        self.prog = prog
        self.ref: Ref = doc.w.reserve()
        self.gids: dict[int, str] = {}
        self.advances: dict[int, float] = {}  # gid -> advance in glyph space (em units), as shown
        doc.fonts.append(self)

    def use(self, gid: int, name: str, wx: float | None = None) -> None:
        """Note a glyph and the advance the text moves by (the font's Metrics, when it has them, over hmtx)."""
        self.gids.setdefault(gid, name)
        if wx is not None:
            self.advances.setdefault(gid, wx)

    def finish(self, doc) -> None:
        w = doc.w
        prog = self.prog
        f = prog.font
        gids = sorted(self.gids) or [0]
        if f is None:
            w.put(self.ref, {"Type": PdfName("Font"), "Subtype": PdfName("Type1"), "BaseFont": PdfName("Helvetica")})
            return
        data, mapping = ttf.subset(f, gids)
        ff = w.stream(data, {"Length1": len(data)})
        top = max(gids)
        c2g = bytearray(2 * (top + 1))
        for old, new in mapping.items():
            if old <= top:
                c2g[2 * old:2 * old + 2] = struct.pack(">H", new)
        scale = 1000.0 / f.units
        widths = []
        run_start, run = None, []
        for g in gids:
            adv = _r(self.advances[g] * 1000.0 if g in self.advances else f.advance(g) * scale)
            if run_start is not None and g == run_start + len(run):
                run.append(adv)
            else:
                if run_start is not None:
                    widths += [run_start, run]
                run_start, run = g, [adv]
        if run_start is not None:
            widths += [run_start, run]
        name = prog.name.replace(" ", "")
        base = f"{tag(name + str(gids))}+{name}"
        desc = w.add({"Type": PdfName("FontDescriptor"), "FontName": PdfName(base), "Flags": 4,
                      "FontBBox": [int(v * scale) for v in f.bbox], "ItalicAngle": 0,
                      "Ascent": int(f.ascent * scale), "Descent": int(f.descent * scale),
                      "CapHeight": int(f.ascent * scale), "StemV": 80, "FontFile2": ff})
        cid = w.add({"Type": PdfName("Font"), "Subtype": PdfName("CIDFontType2"), "BaseFont": PdfName(base),
                     "CIDSystemInfo": {"Registry": "Adobe", "Ordering": "Identity", "Supplement": 0},
                     "FontDescriptor": desc, "W": widths, "CIDToGIDMap": w.stream(bytes(c2g))})
        rev = {g: chr(u) for u, g in f.cmap.items()} if f.cmap else {}
        uni = {g: to_unicode(n) or rev.get(g) for g, n in self.gids.items()}
        d = {"Type": PdfName("Font"), "Subtype": PdfName("Type0"), "BaseFont": PdfName(base),
             "Encoding": PdfName("Identity-H"), "DescendantFonts": [cid]}
        if any(uni.values()):
            d["ToUnicode"] = w.stream(unicode_map(uni, True))
        w.put(self.ref, d)


def _r(v: float, places: int = 4):
    r = round(float(v), places)
    return int(r) if r == int(r) else r


def _n(v: float) -> bytes:
    r = _r(v)
    return str(r).encode()


__all__ = ["CORE14", "ISOLATIN1", "STANDARD", "CIDFont", "SimpleFont", "Standard", "Type1", "Type3", "Type42",
           "afm", "standard_for"]
