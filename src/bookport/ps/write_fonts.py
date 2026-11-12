"""Fonts for the PDF to PostScript writer, from the Type 1 Font Format book, Adobe TN 5012 (Type 42), TN 5176
(CFF), TN 5177 (Type 2 charstrings), TN 5014 (CID fonts) and ISO 32000-1, 9.5 to 9.10.

- Type 1 programs (FontFile) go in as they are, renamed, and a copy is re-encoded to the PDF's encoding.
- CFF (FontFile3, also inside OpenType) is turned into Type 1: each Type 2 charstring is run to its outline and
  written again as a Type 1 charstring (hints left out); CID-keyed CFF is split into Type 1 fonts of 256 glyphs.
- TrueType (FontFile2) becomes Type 42, with only the glyphs the document shows (the others emptied, so glyph
  numbers stay); a CID TrueType font is split into Type 42 fonts of 256 glyphs that share one sfnts array.
- Type 3 fonts become PostScript Type 3 fonts (their glyph procedures are translated by the page writer).
- Fonts the PDF doesn't carry are drawn with the nearest of the standard fonts every PostScript printer has.

Text is shown with the PDF's own widths (xshow), so a font's own metrics never move a glyph.
"""

from __future__ import annotations

import re
import struct

from ..pdf.encodings import glyph_to_unicode
from ..pdf.parser import Document, Stream, content_tokens
from ..pdf.ttf import _components
from . import write_tables as tables
from .files import encrypt as _encrypt
from .write_paint import arr, hexstr, name, num, nums, wrap

BASE_NAMES = {"StandardEncoding": tables.STANDARD, "WinAnsiEncoding": tables.WINANSI,
              "MacRomanEncoding": tables.MACROMAN, "PDFDocEncoding": tables.WINANSI}
STANDARD_14 = {"Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic", "Helvetica", "Helvetica-Bold",
               "Helvetica-Oblique", "Helvetica-BoldOblique", "Courier", "Courier-Bold", "Courier-Oblique",
               "Courier-BoldOblique", "Symbol", "ZapfDingbats"}
ALIASES = {"Arial": "Helvetica", "ArialMT": "Helvetica", "Arial-BoldMT": "Helvetica-Bold",
           "Arial-ItalicMT": "Helvetica-Oblique", "Arial-BoldItalicMT": "Helvetica-BoldOblique",
           "TimesNewRoman": "Times-Roman", "TimesNewRomanPSMT": "Times-Roman",
           "TimesNewRoman-Bold": "Times-Bold", "TimesNewRomanPS-BoldMT": "Times-Bold",
           "TimesNewRoman-Italic": "Times-Italic", "TimesNewRomanPS-ItalicMT": "Times-Italic",
           "TimesNewRoman-BoldItalic": "Times-BoldItalic", "TimesNewRomanPS-BoldItalicMT": "Times-BoldItalic",
           "CourierNew": "Courier", "CourierNewPSMT": "Courier", "CourierNew-Bold": "Courier-Bold",
           "CourierNewPS-BoldMT": "Courier-Bold", "CourierNew-Italic": "Courier-Oblique",
           "CourierNewPS-ItalicMT": "Courier-Oblique", "CourierNewPS-BoldItalicMT": "Courier-BoldOblique",
           "Helvetica-Italic": "Helvetica-Oblique", "Helvetica-BoldItalic": "Helvetica-BoldOblique"}
SPACE_NAMES = {"space", "uni0020"}


def _plain(name_: str) -> str:
    """A font name without its subset tag (ABCDEF+)."""
    return re.sub(r"^[A-Z]{6}\+", "", name_)


def standard_for(base: str, flags: int, weight, angle) -> str:
    """The standard font nearest to a font the PDF names but doesn't carry."""
    plain = _plain(base)
    if plain in STANDARD_14:
        return plain
    if plain in ALIASES:
        return ALIASES[plain]
    compact = plain.replace(",", "-").replace(" ", "")
    if compact in ALIASES:
        return ALIASES[compact]
    low = plain.lower()
    if "symbol" in low and not flags & 32:
        return "Symbol"
    if "dingbat" in low or "zapfd" in low:
        return "ZapfDingbats"
    bold = bool(re.search(r"bold|black|heavy|semibold|demi", low)) or (isinstance(weight, (int, float)) and weight >= 600) \
        or bool(flags & 262144)
    italic = bool(re.search(r"italic|oblique|slant", low)) or bool(flags & 64) or \
        (isinstance(angle, (int, float)) and angle != 0)
    if re.search(r"courier|mono|consol|typewriter|code", low) or flags & 1:
        family = "Courier"
    elif re.search(r"times|roman|serif|georgia|garamond|cambria|book|palatino|minion|baskerville|caslon|century", low) \
            and "sans" not in low or flags & 2 and "sans" not in low:
        family = "Times"
    else:
        family = "Helvetica"
    if family == "Times":
        return {(0, 0): "Times-Roman", (1, 0): "Times-Bold", (0, 1): "Times-Italic", (1, 1): "Times-BoldItalic"}[
            (int(bold), int(italic))]
    style = {(0, 0): "", (1, 0): "-Bold", (0, 1): "-Oblique", (1, 1): "-BoldOblique"}[(int(bold), int(italic))]
    return family + style


# --- Type 1 ------------------------------------------------------------------------------------------------



def _t1num(v: int) -> bytes:
    if -107 <= v <= 107:
        return bytes([v + 139])
    if 108 <= v <= 1131:
        v -= 108
        return bytes([(v >> 8) + 247, v & 0xFF])
    if -1131 <= v <= -108:
        v = -v - 108
        return bytes([(v >> 8) + 251, v & 0xFF])
    return b"\xff" + struct.pack(">i", v)


def type1_charstring(width: float, path: list) -> bytes:
    """A Type 1 charstring (not yet encrypted) for an outline: path items ("m", x, y), ("l", x, y),
    ("c", x1, y1, x2, y2, x3, y3), in glyph units; the side bearing point is the origin."""
    out = bytearray(_t1num(0) + _t1num(round(width)) + b"\x0d")  # hsbw
    cx = cy = 0
    open_ = False
    for item in path:
        op = item[0]
        pts = [round(v) for v in item[1:]]
        if op == "m":
            if open_:
                out += b"\x09"
            out += _t1num(pts[0] - cx) + _t1num(pts[1] - cy) + b"\x15"
            cx, cy = pts
            open_ = True
        elif op == "l":
            out += _t1num(pts[0] - cx) + _t1num(pts[1] - cy) + b"\x05"
            cx, cy = pts
        else:
            out += (_t1num(pts[0] - cx) + _t1num(pts[1] - cy) + _t1num(pts[2] - pts[0]) + _t1num(pts[3] - pts[1])
                    + _t1num(pts[4] - pts[2]) + _t1num(pts[5] - pts[3]) + b"\x08")
            cx, cy = pts[4], pts[5]
    if open_:
        out += b"\x09"
    out += b"\x0e"
    return bytes(out)


def type1_font(fontname: str, matrix: list[float], bbox: list[float], encoding: list[str],
               charstrings: dict[str, bytes]) -> str:
    """A Type 1 font program (eexec part in hex), defining ``fontname``."""
    charstrings = dict(charstrings)
    charstrings.setdefault(".notdef", type1_charstring(0, []))
    enc = ["/Encoding 256 array 0 1 255 {1 index exch /.notdef put} for"]
    for code, g in enumerate(encoding[:256]):
        if g and g != ".notdef":
            enc.append(f"dup {code} {name(g)} put")
    clear = "\n".join([
        f"%!FontType1-1.0: {fontname.removeprefix('/')} 001.000",
        "12 dict begin",
        f"/FontName {name(fontname)} def",
        "/PaintType 0 def /FontType 1 def",
        f"/FontMatrix {arr(matrix)} readonly def",
        f"/FontBBox {{{nums(bbox)}}} readonly def",
        *enc, "readonly def",
        "currentdict end",
        "currentfile eexec"]) + "\n"
    body = bytearray(b"dup /Private 8 dict dup begin\n"
                     b"/RD {string currentfile exch readstring pop} executeonly def\n"
                     b"/ND {noaccess def} executeonly def\n/NP {noaccess put} executeonly def\n"
                     b"/BlueValues [] def /MinFeature {16 16} def /password 5839 def /lenIV 4 def\n"
                     b"2 index /CharStrings %d dict dup begin\n" % (len(charstrings) + 1))
    for g, cs in charstrings.items():
        enc_cs = _encrypt(b"\0\0\0\0" + cs, 4330)
        body += name(g).encode("latin-1") + b" %d RD " % len(enc_cs) + enc_cs + b" ND\n"
    body += (b"end\nend\nreadonly put\nnoaccess put\ndup /FontName get exch definefont pop\n"
             b"mark currentfile closefile\n")
    secret = _encrypt(b"\0\0\0\0" + bytes(body), 55665).hex()
    lines = "\n".join(secret[i:i + 128] for i in range(0, len(secret), 128))
    return clear + lines + "\n" + ("0" * 64 + "\n") * 8 + "cleartomark\n"


def type1_program(data: bytes, rename: str) -> tuple[str, str] | None:
    """An embedded Type 1 program (FontFile), 7-bit clean and renamed: (text, its old name)."""
    if data[:1] == b"\x80":  # PFB segments
        out = bytearray()
        i = 0
        while i + 6 <= len(data) and data[i] == 0x80 and data[i + 1] in (1, 2):
            n = struct.unpack("<I", data[i + 2:i + 6])[0]
            out += data[i + 6:i + 6 + n]
            i += 6 + n
        data = bytes(out)
    m = re.search(rb"currentfile\s+eexec\s*?(\r\n|\r|\n|\s)", data)
    if not m:
        return None
    clear, rest = data[:m.end()], data[m.end():]
    while rest[:1] in (b"\r", b"\n", b" ", b"\t"):
        rest = rest[1:]
    end = re.search(rb"0{64}|cleartomark", rest)
    secret = rest[:end.start()] if end else rest
    if not re.fullmatch(rb"[0-9A-Fa-f\s]+", secret[:64] or b"x"):
        secret = secret.hex().encode("ascii")
    else:
        secret = re.sub(rb"\s", b"", secret)
    nm = re.search(rb"/FontName\s*/([^\s/\[\]{}()<>%]+)", clear)
    old = nm.group(1).decode("latin-1") if nm else ""
    clear = re.sub(rb"(/FontName\s*)/[^\s/\[\]{}()<>%]+", lambda mm: mm.group(1) + rename.encode("latin-1"), clear,
                   count=1)
    text = bytes(c if c < 128 else 0x3F for c in clear).decode("latin-1")
    text = re.sub(r"^(%%EOF|%%Trailer)", r"%\1", text, flags=re.MULTILINE)  # no DSC lines from inside the font
    hexed = secret.decode("ascii")
    lines = "\n".join(hexed[i:i + 128] for i in range(0, len(hexed), 128))
    return text.rstrip("\r\n ") + "\n" + lines + "\n" + ("0" * 64 + "\n") * 8 + "cleartomark\n", old


def builtin_encoding(program: bytes) -> list[str]:
    """The encoding a Type 1 program sets up for itself (StandardEncoding unless it lists its own)."""
    head = program[:program.find(b"eexec")] if b"eexec" in program else program[:20000]
    if re.search(rb"/Encoding\s+StandardEncoding", head):
        return list(tables.STANDARD)
    out = [".notdef"] * 256
    found = False
    for m in re.finditer(rb"dup\s+(\d+)\s*/([^\s/\[\]{}()<>%]+)\s+put", head):
        code = int(m.group(1))
        if code < 256:
            out[code] = m.group(2).decode("latin-1")
            found = True
    return out if found else list(tables.STANDARD)


# --- CFF ---------------------------------------------------------------------------------------------------

class CFFError(ValueError):
    pass


def _index(data: bytes, pos: int) -> tuple[list[bytes], int]:
    if pos + 2 > len(data):
        raise CFFError("index")
    count = struct.unpack(">H", data[pos:pos + 2])[0]
    if count == 0:
        return [], pos + 2
    size = data[pos + 2]
    if not 1 <= size <= 4:
        raise CFFError("offsize")
    offs = []
    p = pos + 3
    for _ in range(count + 1):
        offs.append(int.from_bytes(data[p:p + size], "big"))
        p += size
    base = p - 1
    items = [data[base + offs[i]:base + offs[i + 1]] for i in range(count)]
    return items, base + offs[-1]


def _dict(data: bytes) -> dict:
    out: dict = {}
    ops: list = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b <= 21:
            if b == 12:
                key = (12, data[i + 1] if i + 1 < n else 0)
                i += 2
            else:
                key = b
                i += 1
            out[key] = ops
            ops = []
        elif b == 28:
            ops.append(struct.unpack(">h", data[i + 1:i + 3])[0])
            i += 3
        elif b == 29:
            ops.append(struct.unpack(">i", data[i + 1:i + 5])[0])
            i += 5
        elif b == 30:
            s = ""
            i += 1
            done = False
            while i < n and not done:
                for nib in (data[i] >> 4, data[i] & 15):
                    if nib == 15:
                        done = True
                        break
                    s += "0123456789.E" [nib] if nib < 12 else ("E-" if nib == 12 else "-")
                i += 1
            try:
                ops.append(float(s))
            except ValueError:
                ops.append(0.0)
        elif 32 <= b <= 246:
            ops.append(b - 139)
            i += 1
        elif 247 <= b <= 250:
            ops.append((b - 247) * 256 + data[i + 1] + 108)
            i += 2
        elif 251 <= b <= 254:
            ops.append(-(b - 251) * 256 - data[i + 1] - 108)
            i += 2
        else:
            i += 1
    return out


def _bias(n: int) -> int:
    return 107 if n < 1240 else 1131 if n < 33900 else 32768


class _Private:
    def __init__(self, data: bytes, pd: dict):
        self.default = float((pd.get(20) or [0])[0])
        self.nominal = float((pd.get(21) or [0])[0])
        self.subrs: list[bytes] = []


class CFF:
    """A CFF font (the first in its FontSet): glyph names (or CIDs), outlines and widths."""

    def __init__(self, data: bytes):
        if len(data) < 4:
            raise CFFError("short")
        self.data = data
        hdr = data[2]
        _names, p = _index(data, hdr)
        tops, p = _index(data, p)
        strings, p = _index(data, p)
        self.gsubrs, _ = _index(data, p)
        self.strings = strings
        if not tops:
            raise CFFError("no top dict")
        top = _dict(tops[0])
        self.top = top
        self.matrix = [float(v) for v in top.get((12, 7), [0.001, 0, 0, 0.001, 0, 0])]
        self.bbox = [float(v) for v in top.get(5, [0, 0, 0, 0])]
        cs_off = (top.get(17) or [0])[0]
        self.charstrings, _ = _index(data, cs_off)
        self.n = len(self.charstrings)
        self.cid = (12, 30) in top
        self.privates: list[_Private] = []
        self.fd_matrices: list[list[float]] = []
        self.fdselect: list[int] = [0] * self.n
        if self.cid:
            fda_off = (top.get((12, 36)) or [0])[0]
            fds, _ = _index(data, fda_off)
            for fd in fds:
                fdd = _dict(fd)
                self.privates.append(self._private(fdd.get(18)))
                self.fd_matrices.append([float(v) for v in fdd.get((12, 7), [])])
            sel_off = (top.get((12, 37)) or [0])[0]
            self.fdselect = self._fdselect(sel_off)
        else:
            self.privates.append(self._private(top.get(18)))
        self.charset = self._charset((top.get(15) or [0])[0])
        self.names = {} if self.cid else {self.sid(s): g for g, s in enumerate(self.charset)}
        self.by_cid = {s: g for g, s in enumerate(self.charset)} if self.cid else {}
        self.encoding = None if self.cid else self._encoding((top.get(16) or [0])[0])
        self._outlines: dict[int, tuple[float, list]] = {}

    def _private(self, entry) -> _Private:
        if not entry or len(entry) < 2:
            return _Private(b"", {})
        size, off = int(entry[0]), int(entry[1])
        raw = self.data[off:off + size]
        pd = _dict(raw)
        priv = _Private(raw, pd)
        if 19 in pd:
            priv.subrs, _ = _index(self.data, off + int(pd[19][0]))
        return priv

    def sid(self, s: int) -> str:
        if s < 391:
            return tables.CFF_STRINGS[s]
        k = s - 391
        return self.strings[k].decode("latin-1") if k < len(self.strings) else f"sid{s}"

    def _charset(self, off: int) -> list[int]:
        n = self.n
        if off in (0, 1, 2):
            return list(range(n))
        d = self.data
        fmt = d[off]
        out = [0]
        p = off + 1
        if fmt == 0:
            for _ in range(n - 1):
                out.append(struct.unpack(">H", d[p:p + 2])[0])
                p += 2
        else:
            while len(out) < n and p < len(d):
                first = struct.unpack(">H", d[p:p + 2])[0]
                left = d[p + 2] if fmt == 1 else struct.unpack(">H", d[p + 2:p + 4])[0]
                p += 3 if fmt == 1 else 4
                out.extend(range(first, first + left + 1))
        return (out + [0] * n)[:n]

    def _fdselect(self, off: int) -> list[int]:
        d = self.data
        n = self.n
        if not off or off >= len(d):
            return [0] * n
        fmt = d[off]
        if fmt == 0:
            return list(d[off + 1:off + 1 + n]) + [0] * max(0, n - len(d[off + 1:off + 1 + n]))
        out = [0] * n
        count = struct.unpack(">H", d[off + 1:off + 3])[0]
        p = off + 3
        for k in range(count):
            first, fd = struct.unpack(">HB", d[p:p + 3])
            nxt = struct.unpack(">H", d[p + 3:p + 5])[0] if k < count else n
            for g in range(first, min(nxt, n)):
                out[g] = fd
            p += 3
        return out

    def _encoding(self, off: int) -> list[str]:
        if off in (0, 1):
            return list(tables.STANDARD)  # (the Expert encoding is never used by PDF fonts in practice)
        d = self.data
        out = [".notdef"] * 256
        fmt = d[off]
        p = off + 1
        names = [self.sid(s) for s in self.charset]
        if fmt & 0x7F == 0:
            count = d[p]
            for g in range(1, count + 1):
                code = d[p + g]
                if g < len(names):
                    out[code] = names[g]
            p += 1 + count
        else:
            ranges = d[p]
            p += 1
            g = 1
            for _ in range(ranges):
                first, left = d[p], d[p + 1]
                p += 2
                for code in range(first, min(first + left + 1, 256)):
                    if g < len(names):
                        out[code] = names[g]
                    g += 1
        if fmt & 0x80:
            sups = d[p]
            p += 1
            for _ in range(sups):
                code, s = struct.unpack(">BH", d[p:p + 3])
                out[code] = self.sid(s)
                p += 3
        return out

    def glyph_matrix(self, gid: int) -> list[float]:
        if self.cid and self.fd_matrices:
            fd = self.fdselect[gid] if gid < len(self.fdselect) else 0
            m = self.fd_matrices[fd] if fd < len(self.fd_matrices) else []
            if len(m) == 6:
                return _mul(m, self.matrix)
        return self.matrix

    def outline(self, gid: int) -> tuple[float, list]:
        """(advance width, path) of glyph gid in its own units."""
        if gid in self._outlines:
            return self._outlines[gid]
        res = (0.0, [])
        if 0 <= gid < self.n:
            fd = self.fdselect[gid] if self.cid and gid < len(self.fdselect) else 0
            priv = self.privates[fd] if fd < len(self.privates) else _Private(b"", {})
            try:
                res = _Type2(self, priv).run(self.charstrings[gid])
            except (IndexError, ValueError, struct.error, ZeroDivisionError, RecursionError):
                res = (priv.default, [])
        if len(self._outlines) < 70_000:
            self._outlines[gid] = res
        return res


def _mul(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


class _Type2:
    """Runs a Type 2 charstring to its outline (hints are counted, to skip hint masks, and dropped)."""

    STEPS = 50_000

    def __init__(self, cff: CFF, priv: _Private):
        self.cff, self.priv = cff, priv
        self.path: list = []
        self.x = self.y = 0.0
        self.stems = 0
        self.width: float | None = None
        self.st: list[float] = []
        self.steps = 0
        self.done = False
        self.trans: list[float] = [0.0] * 32

    def run(self, cs: bytes) -> tuple[float, list]:
        self._exec(cs, 0)
        w = self.priv.default if self.width is None else self.width
        return w, self.path

    def _w(self, extra: bool):
        if self.width is None:
            if extra and self.st:
                self.width = self.priv.nominal + self.st.pop(0)
            else:
                self.width = self.priv.default

    def _move(self, dx, dy):
        self.x += dx
        self.y += dy
        self.path.append(("m", self.x, self.y))

    def _line(self, dx, dy):
        self.x += dx
        self.y += dy
        self.path.append(("l", self.x, self.y))

    def _curve(self, a, b, c, d, e, f):
        x1, y1 = self.x + a, self.y + b
        x2, y2 = x1 + c, y1 + d
        self.x, self.y = x2 + e, y2 + f
        self.path.append(("c", x1, y1, x2, y2, self.x, self.y))

    def _exec(self, cs: bytes, depth: int):
        if depth > 10:
            raise ValueError("deep")
        st = self.st
        i, n = 0, len(cs)
        while i < n and not self.done:
            self.steps += 1
            if self.steps > self.STEPS:
                raise ValueError("long")
            b = cs[i]
            if b >= 32 or b == 28:
                if b == 28:
                    st.append(struct.unpack(">h", cs[i + 1:i + 3])[0])
                    i += 3
                elif b <= 246:
                    st.append(b - 139)
                    i += 1
                elif b <= 250:
                    st.append((b - 247) * 256 + cs[i + 1] + 108)
                    i += 2
                elif b <= 254:
                    st.append(-(b - 251) * 256 - cs[i + 1] - 108)
                    i += 2
                else:
                    st.append(struct.unpack(">i", cs[i + 1:i + 5])[0] / 65536)
                    i += 5
                continue
            i += 1
            if b in (1, 3, 18, 23):  # stems
                self._w(len(st) % 2 == 1)
                self.stems += len(st) // 2
                st.clear()
            elif b in (19, 20):  # hintmask, cntrmask
                self._w(len(st) % 2 == 1)
                self.stems += len(st) // 2
                st.clear()
                i += (self.stems + 7) // 8
            elif b == 21:
                self._w(len(st) > 2)
                self._move(st[-2], st[-1])
                st.clear()
            elif b == 22:
                self._w(len(st) > 1)
                self._move(st[-1], 0)
                st.clear()
            elif b == 4:
                self._w(len(st) > 1)
                self._move(0, st[-1])
                st.clear()
            elif b == 5:
                for k in range(0, len(st) - 1, 2):
                    self._line(st[k], st[k + 1])
                st.clear()
            elif b in (6, 7):
                horiz = b == 6
                for v in st:
                    self._line(v, 0) if horiz else self._line(0, v)
                    horiz = not horiz
                st.clear()
            elif b == 8:
                for k in range(0, len(st) - 5, 6):
                    self._curve(*st[k:k + 6])
                st.clear()
            elif b == 24:  # rcurveline
                k = 0
                while k + 6 <= len(st) - 2:
                    self._curve(*st[k:k + 6])
                    k += 6
                if k + 1 < len(st):
                    self._line(st[k], st[k + 1])
                st.clear()
            elif b == 25:  # rlinecurve
                k = 0
                while k + 2 <= len(st) - 6:
                    self._line(st[k], st[k + 1])
                    k += 2
                if k + 6 <= len(st):
                    self._curve(*st[k:k + 6])
                st.clear()
            elif b == 26:  # vvcurveto
                k = 0
                dx1 = 0.0
                if len(st) % 4 == 1:
                    dx1 = st[0]
                    k = 1
                while k + 4 <= len(st):
                    self._curve(dx1, st[k], st[k + 1], st[k + 2], 0, st[k + 3])
                    dx1 = 0.0
                    k += 4
                st.clear()
            elif b == 27:  # hhcurveto
                k = 0
                dy1 = 0.0
                if len(st) % 4 == 1:
                    dy1 = st[0]
                    k = 1
                while k + 4 <= len(st):
                    self._curve(st[k], dy1, st[k + 1], st[k + 2], st[k + 3], 0)
                    dy1 = 0.0
                    k += 4
                st.clear()
            elif b in (30, 31):  # vhcurveto, hvcurveto
                horiz = b == 31
                k = 0
                while k + 4 <= len(st):
                    last = len(st) - k == 5
                    if horiz:
                        self._curve(st[k], 0, st[k + 1], st[k + 2], st[k + 4] if last else 0, st[k + 3])
                    else:
                        self._curve(0, st[k], st[k + 1], st[k + 2], st[k + 3], st[k + 4] if last else 0)
                    k += 5 if last else 4
                    horiz = not horiz
                st.clear()
            elif b in (10, 29):  # callsubr, callgsubr
                subrs = self.priv.subrs if b == 10 else self.cff.gsubrs
                k = int(st.pop()) + _bias(len(subrs))
                if 0 <= k < len(subrs):
                    self._exec(subrs[k], depth + 1)
            elif b == 11:
                return
            elif b == 14:  # endchar
                self._w(len(st) in (1, 5))
                if len(st) >= 4:
                    self._seac(*st[-4:])
                st.clear()
                self.done = True
            elif b == 12:
                b2 = cs[i] if i < n else 0
                i += 1
                self._escape(b2)
            else:
                st.clear()

    def _seac(self, adx, ady, bchar, achar):
        names = self.cff.names
        base = names.get(tables.STANDARD[int(bchar) & 255])
        accent = names.get(tables.STANDARD[int(achar) & 255])
        if base is not None:
            self.path += self.cff.outline(base)[1]
        if accent is not None:
            self.path += [(p[0], *[v + (adx if j % 2 == 0 else ady) for j, v in enumerate(p[1:])])
                          for p in self.cff.outline(accent)[1]]

    def _escape(self, b2: int):
        st = self.st
        if b2 == 35 and len(st) >= 12:  # flex
            self._curve(*st[0:6])
            self._curve(*st[6:12])
        elif b2 == 34 and len(st) >= 7:  # hflex
            dx1, dx2, dy2, dx3, dx4, dx5, dx6 = st[:7]
            self._curve(dx1, 0, dx2, dy2, dx3, 0)
            self._curve(dx4, 0, dx5, -dy2, dx6, 0)
        elif b2 == 36 and len(st) >= 9:  # hflex1
            dx1, dy1, dx2, dy2, dx3, dx4, dx5, dy5, dx6 = st[:9]
            self._curve(dx1, dy1, dx2, dy2, dx3, 0)
            self._curve(dx4, 0, dx5, dy5, dx6, -(dy1 + dy2 + dy5))
        elif b2 == 37 and len(st) >= 11:  # flex1
            d = st[:11]
            dx = sum(d[0:10:2])
            dy = sum(d[1:10:2])
            self._curve(*d[0:6])
            if abs(dx) > abs(dy):
                self._curve(d[6], d[7], d[8], d[9], d[10], -dy)
            else:
                self._curve(d[6], d[7], d[8], d[9], -dx, d[10])
        elif b2 in _ARITH:
            _ARITH[b2](self)
            return
        st.clear()


def _arith(fn, n):
    def run(t2: _Type2):
        args = [t2.st.pop() for _ in range(n)][::-1] if len(t2.st) >= n else [0.0] * n
        t2.st.extend(fn(t2, *args))
    return run


_ARITH = {
    3: _arith(lambda t, a, b: [1 if a and b else 0], 2), 4: _arith(lambda t, a, b: [1 if a or b else 0], 2),
    5: _arith(lambda t, a: [0 if a else 1], 1), 9: _arith(lambda t, a: [abs(a)], 1),
    10: _arith(lambda t, a, b: [a + b], 2), 11: _arith(lambda t, a, b: [a - b], 2),
    12: _arith(lambda t, a, b: [a / b if b else 0], 2), 14: _arith(lambda t, a: [-a], 1),
    15: _arith(lambda t, a, b: [1 if a == b else 0], 2), 18: _arith(lambda t, a: [], 1),
    20: _arith(lambda t, a, i: (t.trans.__setitem__(int(i) % 32, a), [])[1], 2),
    21: _arith(lambda t, i: [t.trans[int(i) % 32]], 1),
    22: _arith(lambda t, s1, s2, v1, v2: [s1 if v1 <= v2 else s2], 4), 23: _arith(lambda t: [0.5], 0),
    24: _arith(lambda t, a, b: [a * b], 2), 26: _arith(lambda t, a: [abs(a) ** 0.5], 1),
    27: _arith(lambda t, a: [a, a], 1), 28: _arith(lambda t, a, b: [b, a], 2),
}


def cff_of(data: bytes, subtype: str) -> bytes | None:
    """The CFF data of a FontFile3 (bare CFF, or an OpenType font's CFF table)."""
    if data[:4] == b"OTTO":
        tabs = sfnt_tables(data)
        if "CFF " in tabs:
            off, ln = tabs["CFF "]
            return data[off:off + ln]
        return None
    return data if data[:1] == b"\x01" else None


# --- TrueType ----------------------------------------------------------------------------------------------

def sfnt_tables(data: bytes) -> dict[str, tuple[int, int]]:
    off = 0
    if data[:4] == b"ttcf":
        off = struct.unpack(">I", data[12:16])[0]
    num_ = struct.unpack(">H", data[off + 4:off + 6])[0]
    out = {}
    for i in range(min(num_, 256)):
        rec = data[off + 12 + 16 * i:off + 28 + 16 * i]
        if len(rec) < 16:
            break
        tag = rec[:4].decode("latin-1")
        _c, o, ln = struct.unpack(">III", rec[4:16])
        if o + ln <= len(data):
            out[tag] = (o, ln)
    return out


MAX_CMAP_ENTRIES = 0x110000  # codes mapped across all of a font's subtables (every Unicode code point once)


def cmaps(data: bytes) -> dict[tuple[int, int], dict[int, int]]:
    """Every cmap subtable of formats 0, 4, 6 and 12, by (platform, encoding). Overlapping ranges in a damaged or
    hostile font could claim billions of codes: past MAX_CMAP_ENTRIES in all, the rest is left out."""
    out: dict[tuple[int, int], dict[int, int]] = {}
    left = MAX_CMAP_ENTRIES
    if len(data) < 4:
        return out
    n = struct.unpack(">H", data[2:4])[0]
    for i in range(min(n, 64)):
        pid, eid, off = struct.unpack(">HHI", data[4 + 8 * i:12 + 8 * i])
        if off + 2 > len(data):
            continue
        fmt = struct.unpack(">H", data[off:off + 2])[0]
        m: dict[int, int] = {}
        try:
            if fmt == 0:
                m = {c: g for c, g in enumerate(data[off + 6:off + 262]) if g}
            elif fmt == 6:
                first, count = struct.unpack(">HH", data[off + 6:off + 10])
                gl = struct.unpack(f">{count}H", data[off + 10:off + 10 + 2 * count])
                m = {first + k: g for k, g in enumerate(gl) if g}
            elif fmt == 4:
                seg2 = struct.unpack(">H", data[off + 6:off + 8])[0]
                segs = seg2 // 2
                ends = struct.unpack(f">{segs}H", data[off + 14:off + 14 + seg2])
                starts = struct.unpack(f">{segs}H", data[off + 16 + seg2:off + 16 + 2 * seg2])
                deltas = struct.unpack(f">{segs}h", data[off + 16 + 2 * seg2:off + 16 + 3 * seg2])
                ro_pos = off + 16 + 3 * seg2
                ranges = struct.unpack(f">{segs}H", data[ro_pos:ro_pos + seg2])
                for k in range(segs):
                    s, e, dl, ro = starts[k], ends[k], deltas[k], ranges[k]
                    if s == 0xFFFF:
                        continue
                    left -= max(0, min(e, 0xFFFE) + 1 - s)
                    if left < 0:
                        break
                    for c in range(s, min(e, 0xFFFE) + 1):
                        if ro == 0:
                            g = (c + dl) & 0xFFFF
                        else:
                            gp = ro_pos + 2 * k + ro + 2 * (c - s)
                            g = struct.unpack(">H", data[gp:gp + 2])[0] if gp + 2 <= len(data) else 0
                            g = (g + dl) & 0xFFFF if g else 0
                        if g:
                            m[c] = g
            elif fmt == 12:
                ng = struct.unpack(">I", data[off + 12:off + 16])[0]
                for k in range(min(ng, 100_000)):
                    s, e, g0 = struct.unpack(">III", data[off + 16 + 12 * k:off + 28 + 12 * k])
                    left -= max(0, min(e, s + 70_000, 0x10FFFF) + 1 - s)
                    if left < 0:
                        break
                    for c in range(s, min(e, s + 70_000, 0x10FFFF) + 1):
                        m[c] = g0 + c - s
        except struct.error:
            continue
        out.setdefault((pid, eid), m)
    return out


class TrueType:
    """An embedded TrueType font, kept for Type 42: its tables, glyph count and character maps."""

    KEEP = ("head", "hhea", "hmtx", "loca", "glyf", "maxp", "cvt ", "fpgm", "prep")

    def __init__(self, data: bytes):
        self.data = data
        self.tables = sfnt_tables(data)
        for t in ("head", "hhea", "hmtx", "loca", "glyf", "maxp"):
            if t not in self.tables:
                raise ValueError(f"no {t}")
        head = self.table("head")
        self.units = struct.unpack(">H", head[18:20])[0] or 1000
        self.bbox = [v / self.units for v in struct.unpack(">hhhh", head[36:44])]
        self.long = struct.unpack(">h", head[50:52])[0] == 1
        self.n = struct.unpack(">H", self.table("maxp")[4:6])[0]
        self.cmaps = cmaps(self.table("cmap")) if "cmap" in self.tables else {}

    def table(self, tag: str) -> bytes:
        o, ln = self.tables[tag]
        return self.data[o:o + ln]

    def _loca(self) -> list[int]:
        loca = self.table("loca")
        n = self.n + 1
        if self.long:
            vals = list(struct.unpack(f">{min(n, len(loca) // 4)}I", loca[:4 * min(n, len(loca) // 4)]))
        else:
            vals = [v * 2 for v in struct.unpack(f">{min(n, len(loca) // 2)}H", loca[:2 * min(n, len(loca) // 2)])]
        while len(vals) < n:
            vals.append(vals[-1] if vals else 0)
        return vals

    def sfnts(self, used: set[int]) -> list[bytes]:
        """The font with only glyphs ``used`` (and their parts) kept, as Type 42 sfnts strings: each under
        65,535 bytes, even, broken at table or glyph boundaries."""
        loca = self._loca()
        glyf = self.table("glyf")
        keep = {0} | {g for g in used if 0 <= g < self.n}
        todo = list(keep)
        while todo:
            g = todo.pop()
            for _pos, comp in _components(glyf[loca[g]:loca[g + 1]]):
                if comp < self.n and comp not in keep:
                    keep.add(comp)
                    todo.append(comp)
        pieces: list[bytes] = []
        offsets = []
        size = 0
        for g in range(self.n):
            offsets.append(size)
            if g in keep:
                body = glyf[loca[g]:loca[g + 1]]
                body += b"\0" * (len(body) % 2)
                pieces.append(body)
                size += len(body)
            else:
                pieces.append(b"")
        offsets.append(size)
        new_glyf = b"".join(pieces)
        tabs = {t: self.table(t) for t in self.KEEP if t in self.tables}
        tabs["glyf"] = new_glyf
        tabs["loca"] = struct.pack(f">{len(offsets)}I", *offsets)
        head = bytearray(tabs["head"])
        head[50:52] = struct.pack(">h", 1)
        head[8:12] = b"\0\0\0\0"
        tabs["head"] = bytes(head)
        tags = sorted(tabs)
        nt = len(tags)
        sel = max(0, nt.bit_length() - 1)
        rng = (1 << sel) * 16
        header = struct.pack(">IHHHH", 0x00010000, nt, rng, sel, nt * 16 - rng)
        offset = 12 + 16 * nt
        dirs = b""
        starts = {}
        for t in tags:
            body = tabs[t]
            starts[t] = offset
            dirs += t.encode("latin-1") + struct.pack(">III", _checksum(body), offset, len(body))
            offset += len(body) + (-len(body)) % 4
        out = [header + dirs]
        for t in tags:
            body = tabs[t] + b"\0" * ((-len(tabs[t])) % 4)
            if t == "glyf" and len(body) > 65000:
                cur = bytearray()
                for piece in pieces:
                    if len(cur) + len(piece) > 65000 and cur:
                        out.append(bytes(cur))
                        cur = bytearray()
                    cur += piece
                cur += b"\0" * ((-len(new_glyf)) % 4)
                out.append(bytes(cur))
            else:
                out.append(body)
        merged: list[bytes] = []
        cur = b""
        for piece in out:
            if len(piece) > 65000:  # a table larger than a string: break it anywhere (only glyf must be whole glyphs)
                if cur:
                    merged.append(cur)
                    cur = b""
                for k in range(0, len(piece), 65000):
                    merged.append(piece[k:k + 65000])
                continue
            if len(cur) + len(piece) > 65000:
                merged.append(cur)
                cur = b""
            cur += piece
        if cur:
            merged.append(cur)
        return [m + b"\0" if len(m) % 2 else m for m in merged]


def _checksum(data: bytes) -> int:
    data = data + b"\0" * ((-len(data)) % 4)
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def type42_font(fontname: str, sfnts_ps: str, bbox: list[float], encoding: list[str], charstrings: dict[str, int]) -> str:
    enc = wrap([name(g) for g in encoding])
    cs = wrap([f"{name(g)} {gid} def" for g, gid in charstrings.items()])
    return (f"11 dict begin\n/FontName {name(fontname)} def /FontType 42 def /PaintType 0 def\n"
            f"/FontMatrix [1 0 0 1 0 0] def /FontBBox {arr(bbox)} def\n"
            f"/Encoding [{enc}] def\n"
            f"/CharStrings {len(charstrings) + 1} dict dup begin /.notdef 0 def {cs} end def\n"
            f"/sfnts {sfnts_ps} def\ncurrentdict end {name(fontname)} exch definefont pop\n")


# --- the CMaps of composite fonts --------------------------------------------------------------------------

class CidMap:
    """Character codes to CIDs: Identity (two bytes), or an embedded CMap's code space and CID ranges."""

    def __init__(self, data: bytes | None = None):
        self.ranges: list[tuple[int, bytes, bytes]] = []
        self.single: dict[bytes, int] = {}
        self.spans: list[tuple[int, int, int, int]] = []  # (length, low, high, first cid)
        if data:
            self._read(data)

    def _read(self, data: bytes):
        toks = content_tokens(data)
        i, n = 0, len(toks)
        while i < n:
            k, v = toks[i]
            if k == "kw" and v in (b"begincodespacerange", b"begincidrange", b"begincidchar"):
                block = v
                i += 1
                while i < n and not (toks[i][0] == "kw" and toks[i][1].startswith(b"end")):
                    if block == b"begincodespacerange" and i + 1 < n:
                        lo, hi = toks[i][1], toks[i + 1][1]
                        if isinstance(lo, bytes) and isinstance(hi, bytes):
                            self.ranges.append((len(lo), bytes(lo), bytes(hi)))
                        i += 2
                    elif block == b"begincidrange" and i + 2 < n:
                        lo, hi, cid = toks[i][1], toks[i + 1][1], toks[i + 2][1]
                        if isinstance(lo, bytes) and isinstance(hi, bytes) and isinstance(cid, int):
                            self.spans.append((len(lo), int.from_bytes(lo, "big"), int.from_bytes(hi, "big"), cid))
                        i += 3
                    elif block == b"begincidchar" and i + 1 < n:
                        src, cid = toks[i][1], toks[i + 1][1]
                        if isinstance(src, bytes) and isinstance(cid, int):
                            self.single[bytes(src)] = cid
                        i += 2
                    else:
                        i += 1
            i += 1
        self.ranges.sort(key=lambda r: r[0])

    def codes(self, s: bytes) -> list[tuple[bytes, int]]:
        """(code bytes, CID) for each character code in s."""
        out = []
        i, n = 0, len(s)
        while i < n:
            length = 2
            for ln, lo, hi in self.ranges:
                chunk = s[i:i + ln]
                if len(chunk) == ln and lo <= chunk <= hi:
                    length = ln
                    break
            raw = s[i:i + length]
            i += length
            if not self.spans and not self.single:
                out.append((raw, int.from_bytes(raw, "big")))
                continue
            cid = self.single.get(raw)
            if cid is None:
                v = int.from_bytes(raw, "big")
                for ln, lo, hi, first in self.spans:
                    if ln == len(raw) and lo <= v <= hi:
                        cid = first + v - lo
                        break
            out.append((raw, cid or 0))
        return out


def cid_widths(doc: Document, arr_) -> dict[int, float]:
    r = doc.resolve
    arr_ = r(arr_) or []
    out: dict[int, float] = {}
    i = 0
    while isinstance(arr_, list) and i < len(arr_):
        first = r(arr_[i])
        nxt = r(arr_[i + 1]) if i + 1 < len(arr_) else None
        if isinstance(nxt, list) and isinstance(first, int):
            for k, w in enumerate(nxt):
                w = r(w)
                out[first + k] = float(w) if isinstance(w, (int, float)) else 0.0
            i += 2
        elif i + 2 < len(arr_) and isinstance(first, int) and isinstance(nxt, int):
            w = r(arr_[i + 2])
            for c in range(first, min(nxt, first + 70_000) + 1):
                out[c] = float(w) if isinstance(w, (int, float)) else 0.0
            i += 3
        else:
            break
    return out


def differences(doc: Document, enc) -> dict[int, str]:
    out: dict[int, str] = {}
    diffs = doc.resolve(enc.get("Differences")) if isinstance(enc, dict) else None
    code = 0
    for item in diffs or []:
        item = doc.resolve(item)
        if isinstance(item, int):
            code = item
        elif isinstance(item, str):
            if 0 <= code < 256:
                out[code] = str(item)
            code += 1
    return out


def unicode_name(u: str) -> str:
    """A glyph name for a character, as the standard fonts name their glyphs."""
    if not u:
        return ".notdef"
    c = ord(u[0])
    for table in (tables.STANDARD, tables.WINANSI, tables.MACROMAN):
        for g in table:
            if g != ".notdef" and glyph_to_unicode(g) == u[0]:
                return g
    return f"uni{c:04X}"


_NAME_CACHE: dict[str, str] = {}


def unicode_name_cached(u: str) -> str:
    if u not in _NAME_CACHE:
        _NAME_CACHE[u] = unicode_name(u)
    return _NAME_CACHE[u]


def stream_bytes(doc: Document, v) -> bytes | None:
    v = doc.resolve(v)
    return doc.decode(v) if isinstance(v, Stream) else None


__all__ = [
    "BASE_NAMES",
    "CFF",
    "CidMap",
    "TrueType",
    "builtin_encoding",
    "cff_of",
    "cid_widths",
    "differences",
    "hexstr",
    "num",
    "standard_for",
    "type1_charstring",
    "type1_font",
    "type1_program",
    "type42_font",
    "unicode_name_cached",
]
