"""PDF to PostScript (Level 3) and EPS, from ISO 32000-1 and Adobe's PostScript Language Reference (3rd ed.).

Each page's content is translated in Python, operator by operator, into PostScript that calls a short prolog
(m, l, c, f, S, q, Q...). Text keeps PDF's text state here and shows every string with the PDF's own glyph
widths (xshow), so glyphs land where the PDF puts them whatever the embedded font says. Fonts, Type 3 glyph
procedures and tiling patterns are written once, in the setup. Images are ASCII85 (the file stays 7-bit):
JPEG and fax data pass through, everything else is Flate-compressed samples.

PostScript has no transparency. A page that uses it is drawn as a picture by ``flatten`` when the caller
gives one (ConvertAll renders with PDFium), else drawn opaque with a note.
"""

from __future__ import annotations

import math
import re
import zlib

from .. import limits
from ..errors import BookportError
from ..pdf.parser import Document, Stream, content_tokens
from . import write_fonts as wf
from .write_image import Images
from .write_paint import (
    CMYK,
    DEVICE,
    GRAY,
    IDENTITY,
    RGB,
    ColorSpace,
    _f,
    a85,
    arr,
    colorspace,
    matrix,
    mul,
    name,
    num,
    nums,
    shading,
    string,
    wrap,
)
from .write_text import FontRec

FLATTEN_DPI = 300
MAX_DEPTH = 12  # forms inside forms (and patterns, glyphs) drawn at most this deep

PROLOG = """\
/bookportdict 120 dict def
bookportdict begin
/bdf {bind def} bind def
/m {moveto} bdf /l {lineto} bdf /c {curveto} bdf /h {closepath} bdf
/re {4 2 roll moveto 1 index 0 rlineto 0 exch rlineto neg 0 rlineto closepath} bdf
/f {fill} bdf /f* {eofill} bdf /S {stroke} bdf /n {newpath} bdf /W {clip} bdf /W* {eoclip} bdf
/q {gsave} bdf /Q {grestore} bdf /cm {concat} bdf
/g {setgray} bdf /rg {setrgbcolor} bdf /k {setcmykcolor} bdf
/w {setlinewidth} bdf /J {setlinecap} bdf /j {setlinejoin} bdf /M {setmiterlimit} bdf /d {setdash} bdf
/T {gsave concat makefont setfont 0 0 moveto xshow grestore} bdf
/Ty {gsave concat makefont setfont 0 0 moveto xyshow grestore} bdf
/Tp {/Tpm matrix currentmatrix def concat makefont setfont /Tpw exch def /Tpx 0 def /Tpi 0 def
 {Tpx 0 moveto ( ) dup 0 4 -1 roll put true charpath /Tpx Tpx Tpw Tpi get add def /Tpi Tpi 1 add def} forall
 Tpm setmatrix} bdf
/SAstep {begin i a length lt {a i get /i i 1 add def} {()} ifelse end} bdf
/mksrc {2 dict begin /a exch def /i 0 def currentdict end [exch /SAstep cvx] cvx} bdf
/mkpat {gsave setmatrix makepattern grestore} bdf
/BuildGlyph3 {exch /CharProcs get exch 2 copy known not {pop /.notdef} if get bookportdict begin exec end} bdf
/BuildChar3 {1 index /Encoding get exch get 1 index /BuildGlyph get exec} bdf
end
"""


# --- graphics state ----------------------------------------------------------------------------------------

class GS:
    __slots__ = ("fa", "fc", "fcs", "font", "ps_color", "rise", "sa", "sc", "scs", "size", "tc", "th", "tl", "tr", "tw")

    def __init__(self):
        self.fcs = self.scs = GRAY
        self.fc: list | tuple = [0.0]
        self.sc: list | tuple = [0.0]
        self.font: FontRec | None = None
        self.size = 0.0
        self.tc = self.tw = self.tl = self.rise = 0.0
        self.th = 1.0
        self.tr = 0
        self.ps_color: str | None = None
        self.fa = self.sa = 1.0

    def copy(self) -> GS:
        g = GS.__new__(GS)
        for k in GS.__slots__:
            setattr(g, k, getattr(self, k))
        return g


ABBREV = {"W": "Width", "H": "Height", "BPC": "BitsPerComponent", "CS": "ColorSpace", "D": "Decode",
          "DP": "DecodeParms", "F": "Filter", "IM": "ImageMask", "I": "Interpolate", "L": "Length"}
FILTER_ABBREV = {"AHx": "ASCIIHexDecode", "A85": "ASCII85Decode", "LZW": "LZWDecode", "Fl": "FlateDecode",
                 "RL": "RunLengthDecode", "CCF": "CCITTFaxDecode", "DCT": "DCTDecode"}


_BI_OR_SKIP = re.compile(rb"[(%]|(?:^|(?<=[\s\]\)>}]))BI(?=[\s/])")
_PAREN = re.compile(rb"[()\\]")
_EOL = re.compile(rb"[\r\n]")


def _find_bi(data: bytes, pos: int):
    """The next BI operator from pos, looking past strings and comments (a Tj of "take BI and ID cards" is text)."""
    while True:
        m = _BI_OR_SKIP.search(data, pos)
        if not m:
            return None
        c = data[m.start()]
        if c == 0x28:  # a string: to its matching ), escapes and nested ( ) included
            depth, i = 1, m.end()
            while depth:
                k = _PAREN.search(data, i)
                if not k:
                    return None
                ch = data[k.start()]
                if ch == 0x5C:
                    i = k.start() + 2
                    continue
                depth += 1 if ch == 0x28 else -1
                i = k.end()
            pos = i
        elif c == 0x25:  # a comment, to the end of its line
            k = _EOL.search(data, m.end())
            if not k:
                return None
            pos = k.end()
        else:
            return m


def _split_inline(data: bytes) -> list:
    """Content split into token lists and inline images: [tokens, (dict, image data), tokens, ...]."""
    out: list = []
    pos = 0
    while True:
        m = _find_bi(data, pos)
        if not m:
            out.append(content_tokens(data[pos:]))
            return out
        out.append(content_tokens(data[pos:m.start()]))
        idm = re.compile(rb"\sID(?:\r\n|[\s])").search(data, m.end())
        if not idm:
            return out
        head = content_tokens(data[m.end():idm.start() + 1])
        d = _tokens_dict(head)
        start = idm.end()
        filt = d.get("Filter") or d.get("F")
        width, height = d.get("Width", d.get("W", 0)), d.get("Height", d.get("H", 0))
        end = -1
        if not filt and isinstance(width, int) and isinstance(height, int):
            bpc = d.get("BitsPerComponent", d.get("BPC", 1 if d.get("ImageMask") or d.get("IM") else 8))
            comps = {"G": 1, "DeviceGray": 1, "RGB": 3, "DeviceRGB": 3, "CMYK": 4, "DeviceCMYK": 4}.get(
                str(d.get("ColorSpace", d.get("CS", "G"))), 1)
            if d.get("ImageMask") or d.get("IM"):
                comps = 1
            if isinstance(bpc, int):
                length = ((width * comps * bpc + 7) // 8) * height
                if re.match(rb"\s*EI(?:\s|$)", data[start + length:start + length + 6]):
                    end = start + length
        if end < 0:
            em = re.compile(rb"\sEI(?=\s|$)").search(data, start)
            end = em.start() if em else len(data)
        out.append((d, data[start:end]))
        em2 = re.compile(rb"EI").search(data, end)
        pos = em2.end() if em2 else len(data)


def _tokens_dict(toks) -> dict:
    vals = _values(toks)
    d = {}
    for i in range(0, len(vals) - 1, 2):
        if isinstance(vals[i], str):
            d[str(vals[i])] = vals[i + 1]
    return d


def _values(toks) -> list:
    stack: list = []
    for kind, v in toks:
        if kind == "v":
            stack.append(v)
        elif kind == "aopen":
            stack.append(_MARK_A)
        elif kind == "dopen":
            stack.append(_MARK_D)
        elif kind == "aclose":
            items = []
            while stack and stack[-1] is not _MARK_A:
                items.append(stack.pop())
            if stack:
                stack.pop()
            stack.append(items[::-1])
        elif kind == "dclose":
            items = []
            while stack and stack[-1] is not _MARK_D:
                items.append(stack.pop())
            if stack:
                stack.pop()
            items.reverse()
            stack.append({str(items[i]): items[i + 1] for i in range(0, len(items) - 1, 2) if isinstance(items[i], str)})
        elif kind == "kw":
            stack.append(v.decode("latin-1"))
    return stack


_MARK_A, _MARK_D = object(), object()


class Content:
    """Translates one content stream (a page, form, pattern cell or glyph) into PostScript lines."""

    def __init__(self, w: Writer, out: list[str], resources: dict, gs: GS, inline: bool, depth: int,
                 glyph: bool = False):
        self.w, self.out, self.res, self.gs = w, out, resources if isinstance(resources, dict) else {}, gs
        self.doc = w.doc
        self.inline = inline  # image data may follow in the file (not inside a procedure)
        self.depth = depth
        self.glyph = glyph
        self.cached_glyph = False  # after d1: colour and pictures don't apply
        self.stack: list[GS] = []
        self.cur = (0.0, 0.0)
        self.start = (0.0, 0.0)
        self.clip: str | None = None
        self.tm = self.tlm = IDENTITY
        self.text_clip = False
        self.hidden = 0
        self.mc: list[bool] = []
        self.base: str | None = None

    def emit(self, s: str):
        self.out.append(s)

    # the content loop
    def run(self, data: bytes):
        if b"scn" in data or b"SCN" in data:
            self.base = f"B{self.w.uid()}"
            self.emit(f"/{self.base} matrix currentmatrix def")
        for part in _split_inline(data):
            if isinstance(part, tuple):
                if not self.hidden:
                    self.inline_image(*part)
                continue
            self._run_tokens(part)
        while self.stack:
            self.emit("Q")
            self.gs = self.stack.pop()

    def _run_tokens(self, toks):
        w = self.w
        operands: list = []
        marks: list = []
        for kind, v in toks:
            if kind == "v":
                operands.append(v)
                continue
            if kind == "aopen" or kind == "dopen":
                marks.append((kind, len(operands)))
                continue
            if kind == "aclose" or kind == "dclose":
                if marks:
                    mk, at = marks.pop()
                    items = operands[at:]
                    del operands[at:]
                    if mk == "aopen":
                        operands.append(items)
                    else:
                        operands.append({str(items[i]): items[i + 1] for i in range(0, len(items) - 1, 2)
                                         if isinstance(items[i], str)})
                continue
            if kind != "kw":
                continue
            w.ops += 1
            if w.ops > limits.MAX_PDF_OPERATORS:
                if not w.cut:
                    w.cut = True
                    w.note(f"A page has more than {limits.MAX_PDF_OPERATORS:,} drawing steps; the rest of it was left out")
                return
            handler = OPS.get(v)
            if handler is not None:
                try:
                    handler(self, operands)
                except (IndexError, TypeError, ValueError, KeyError, ZeroDivisionError, OverflowError):
                    pass  # an operator with operands it can't use draws nothing (as in a viewer)
            operands.clear()

    # numbers from operands
    @staticmethod
    def _nums(ops, n):
        vals = [_f(x) for x in ops[-n:]]
        if len(vals) < n:
            raise IndexError("operands")
        return vals

    # graphics state
    def op_q(self, ops):
        self.stack.append(self.gs.copy())
        self.emit("q")

    def op_Q(self, ops):
        if self.stack:
            self.gs = self.stack.pop()
            self.emit("Q")

    def op_cm(self, ops):
        m = self._nums(ops, 6)
        self.emit(arr(m) + " cm")

    def op_w(self, ops):
        self.emit(num(self._nums(ops, 1)[0]) + " w")

    def op_J(self, ops):
        self.emit(str(int(self._nums(ops, 1)[0]) % 3) + " J")

    def op_j(self, ops):
        self.emit(str(int(self._nums(ops, 1)[0]) % 3) + " j")

    def op_M(self, ops):
        self.emit(num(max(1.0, self._nums(ops, 1)[0])) + " M")

    def op_d(self, ops):
        dash, phase = ops[-2], ops[-1]
        if isinstance(dash, list):
            vals = [_f(self.doc.resolve(x)) for x in dash]
            if vals and all(v == 0 for v in vals):
                vals = []
            self.emit(arr(vals) + " " + num(phase) + " d")

    def op_gs(self, ops):
        res = self.doc.resolve(self.res.get("ExtGState")) or {}
        g = self.doc.resolve(res.get(str(ops[-1]))) if isinstance(res, dict) else None
        if not isinstance(g, dict):
            return
        r = self.doc.resolve
        for k, v in g.items():
            v = r(v)
            if k == "LW":
                self.emit(num(_f(v)) + " w")
            elif k == "LC" and isinstance(v, int):
                self.emit(f"{v % 3} J")
            elif k == "LJ" and isinstance(v, int):
                self.emit(f"{v % 3} j")
            elif k == "ML":
                self.emit(num(max(1.0, _f(v, 10))) + " M")
            elif k == "D" and isinstance(v, list) and len(v) == 2 and isinstance(r(v[0]), list):
                self.op_d([r(v[0]), _f(r(v[1]))])
            elif k == "Font" and isinstance(v, list) and len(v) == 2:
                self.gs.font = self.w.font(v[0])
                self.gs.size = _f(r(v[1]))
            elif k in ("CA", "ca"):
                a = _f(v, 1.0)
                if k == "CA":
                    self.gs.sa = a
                else:
                    self.gs.fa = a
                if a < 0.999:
                    self.w.transparent()
            elif k == "BM":
                bm = r(v[0]) if isinstance(v, list) and v else v
                if bm not in ("Normal", "Compatible", None):
                    self.w.transparent()
            elif k == "SMask" and isinstance(v, dict):
                self.w.transparent()

    # paths
    def op_m(self, ops):
        x, y = self._nums(ops, 2)
        self.cur = self.start = (x, y)
        self.emit(f"{num(x)} {num(y)} m")

    def op_l(self, ops):
        x, y = self._nums(ops, 2)
        self.cur = (x, y)
        self.emit(f"{num(x)} {num(y)} l")

    def op_c(self, ops):
        v = self._nums(ops, 6)
        self.cur = (v[4], v[5])
        self.emit(nums(v) + " c")

    def op_v(self, ops):
        v = self._nums(ops, 4)
        x0, y0 = self.cur
        self.cur = (v[2], v[3])
        self.emit(nums([x0, y0, *v]) + " c")

    def op_y(self, ops):
        v = self._nums(ops, 4)
        self.cur = (v[2], v[3])
        self.emit(nums([*v, v[2], v[3]]) + " c")

    def op_h(self, ops):
        self.cur = self.start
        self.emit("h")

    def op_re(self, ops):
        x, y, wd, ht = self._nums(ops, 4)
        self.cur = self.start = (x, y)
        self.emit(f"{num(x)} {num(y)} {num(wd)} {num(ht)} re")

    def op_W(self, ops):
        self.clip = "W"

    def op_Wstar(self, ops):
        self.clip = "W*"

    def paint(self, fill: str | None, stroke: bool, close: bool = False):
        if close:
            self.emit("h")
        gs = self.gs
        clip = self.clip
        self.clip = None
        hidden = self.hidden
        do_fill = fill and not hidden and gs.fa > 0 and not (gs.fcs.kind == "separation" and gs.fcs.none)
        do_stroke = stroke and not hidden and gs.sa > 0 and not (gs.scs.kind == "separation" and gs.scs.none)
        keep = clip is not None
        if do_fill:
            self.set_color(fill=True)
            self.emit(("q " + fill + " Q") if (do_stroke or keep) else fill)
        if do_stroke:
            self.set_color(fill=False)
            self.emit("q S Q" if keep else "S")
        if keep:
            self.emit(clip + " n")
        elif not do_stroke and not do_fill:
            self.emit("n")

    def op_S(self, ops):
        self.paint(None, True)

    def op_s(self, ops):
        self.paint(None, True, close=True)

    def op_f(self, ops):
        self.paint("f", False)

    def op_fstar(self, ops):
        self.paint("f*", False)

    def op_B(self, ops):
        self.paint("f", True)

    def op_Bstar(self, ops):
        self.paint("f*", True)

    def op_b(self, ops):
        self.paint("f", True, close=True)

    def op_bstar(self, ops):
        self.paint("f*", True, close=True)

    def op_n(self, ops):
        self.paint(None, False)

    # colour
    def set_color(self, fill: bool):
        if self.cached_glyph:
            return
        gs = self.gs
        cs, comps = (gs.fcs, gs.fc) if fill else (gs.scs, gs.sc)
        if cs.kind == "pattern":
            key = self.w.pattern_color(self, cs, comps)
            if key is None:
                key = "0 g"
        else:
            try:
                key = cs.ps(list(comps))
            except (IndexError, TypeError, ValueError, ZeroDivisionError, OverflowError):
                key = "0 g"
        if key != gs.ps_color:
            self.emit(key)
            gs.ps_color = key

    def _space(self, ops, fill: bool):
        cs = colorspace(self.doc, ops[-1], self.res)
        if fill:
            self.gs.fcs, self.gs.fc = cs, cs.initial()
        else:
            self.gs.scs, self.gs.sc = cs, cs.initial()

    def _comps(self, ops, fill: bool):
        gs = self.gs
        cs = gs.fcs if fill else gs.scs
        if cs.kind == "pattern":
            pat = ops[-1] if ops and isinstance(ops[-1], str) else None
            comps = ("pattern", pat, [_f(x) for x in ops[:-1] if isinstance(x, (int, float))])
        else:
            comps = [_f(x) for x in ops if isinstance(x, (int, float))][-cs.n:] if cs.n else []
        if fill:
            gs.fc = comps
        else:
            gs.sc = comps

    def op_cs(self, ops):
        self._space(ops, True)

    def op_CS(self, ops):
        self._space(ops, False)

    def op_sc(self, ops):
        self._comps(ops, True)

    def op_SC(self, ops):
        self._comps(ops, False)

    def _device(self, ops, cs: ColorSpace, fill: bool):
        vals = self._nums(ops, cs.n)
        if fill:
            self.gs.fcs, self.gs.fc = cs, vals
        else:
            self.gs.scs, self.gs.sc = cs, vals

    def op_g(self, ops):
        self._device(ops, GRAY, True)

    def op_G(self, ops):
        self._device(ops, GRAY, False)

    def op_rg(self, ops):
        self._device(ops, RGB, True)

    def op_RG(self, ops):
        self._device(ops, RGB, False)

    def op_k(self, ops):
        self._device(ops, CMYK, True)

    def op_K(self, ops):
        self._device(ops, CMYK, False)

    # shading
    def op_sh(self, ops):
        if self.hidden or self.cached_glyph:
            return
        shs = self.doc.resolve(self.res.get("Shading")) or {}
        sh = shs.get(str(ops[-1])) if isinstance(shs, dict) else None
        if sh is None:
            return
        got = shading(self.doc, sh, self.res, self.inline)
        if got is None:
            self.w.note("A shading couldn't be written to PostScript and was left out")
            return
        text, data = got
        self.emit(text + " shfill")
        if data:
            self.emit(a85(data))
        self.gs.ps_color = None  # (shfill doesn't change the colour; kept simple and safe)

    # text
    def op_BT(self, ops):
        self.tm = self.tlm = IDENTITY
        self.text_clip = False

    def op_ET(self, ops):
        if self.text_clip:
            self.emit("W n")
            self.text_clip = False

    def op_Tf(self, ops):
        fonts = self.doc.resolve(self.res.get("Font")) or {}
        ref = fonts.get(str(ops[-2])) if isinstance(fonts, dict) else None
        self.gs.font = self.w.font(ref) if ref is not None else None
        self.gs.size = _f(ops[-1])

    def op_Tc(self, ops):
        self.gs.tc = self._nums(ops, 1)[0]

    def op_Tw(self, ops):
        self.gs.tw = self._nums(ops, 1)[0]

    def op_Tz(self, ops):
        self.gs.th = self._nums(ops, 1)[0] / 100

    def op_TL(self, ops):
        self.gs.tl = self._nums(ops, 1)[0]

    def op_Ts(self, ops):
        self.gs.rise = self._nums(ops, 1)[0]

    def op_Tr(self, ops):
        self.gs.tr = int(self._nums(ops, 1)[0]) % 8

    def op_Td(self, ops):
        tx, ty = self._nums(ops, 2)
        self.tlm = self.tm = mul((1, 0, 0, 1, tx, ty), self.tlm)

    def op_TD(self, ops):
        tx, ty = self._nums(ops, 2)
        self.gs.tl = -ty
        self.op_Td([tx, ty])

    def op_Tm(self, ops):
        self.tlm = self.tm = tuple(self._nums(ops, 6))

    def op_Tstar(self, ops):
        self.op_Td([0, -self.gs.tl])

    def op_Tj(self, ops):
        s = ops[-1]
        if isinstance(s, bytes):
            self.show([s])

    def op_TJ(self, ops):
        a = ops[-1]
        if isinstance(a, list):
            self.show(a)

    def op_quote(self, ops):
        self.op_Tstar([])
        self.op_Tj(ops)

    def op_dquote(self, ops):
        self.gs.tw, self.gs.tc = _f(ops[-3]), _f(ops[-2])
        self.op_Tstar([])
        self.op_Tj(ops)

    def show(self, items: list):
        gs = self.gs
        font = gs.font
        if font is None:
            return
        size, th = gs.size, gs.th
        runs: list[list] = []  # [ps font, start x, bytes, widths]
        x = 0.0
        vertical = font.vertical
        for item in items:
            if isinstance(item, (int, float)):
                adj = -_f(item) / 1000 * size * (1 if vertical else th)
                if runs and runs[-1][3]:
                    runs[-1][3][-1] += adj
                x += adj
                continue
            if not isinstance(item, bytes):
                continue
            for psf, byte, wd, space in font.glyphs(bytes(item)):
                if vertical:
                    adv = wd * 0 + size + gs.tc + (gs.tw if space else 0)  # (one em down a line of vertical text)
                    adv = -adv
                else:
                    adv = (wd * size + gs.tc + (gs.tw if space else 0)) * th
                if not runs or runs[-1][0] != psf:
                    runs.append([psf, x, bytearray(), []])
                runs[-1][2].append(byte)
                runs[-1][3].append(adv)
                x += adv
        if not self.hidden and gs.tr != 3 and runs and size:
            self._emit_runs(runs, vertical)
        if vertical:
            self.tm = mul((1, 0, 0, 1, 0, x), self.tm)
        else:
            self.tm = mul((1, 0, 0, 1, x, 0), self.tm)

    def _emit_runs(self, runs, vertical: bool):
        gs = self.gs
        size, th, rise = gs.size, gs.th, gs.rise
        fm = [size * th, 0, 0, size, 0, rise]
        mode = gs.tr
        if self.gs.font is not None and self.gs.font.kind == "t3":
            mode = 7 if mode == 7 else 0  # a Type 3 glyph draws itself; viewers ignore the other modes
        if mode in (0, 2, 4, 6) and not self.cached_glyph:
            self.set_color(fill=True)
        for psf, x0, codes, widths in runs:
            if not codes:
                continue
            if vertical:
                tm = mul((1, 0, 0, 1, -size * 0.5, x0 - size * 0.88), self.tm)
                ws = "[" + wrap([f"0 {num(v)}" for v in widths]) + "]"
                op = "Ty"
            else:
                tm = mul((1, 0, 0, 1, x0, 0), self.tm)
                ws = arr(widths)
                op = "T"
            args = f"{string(bytes(codes))} {ws} {psf} {arr(fm)} {arr(tm)}"
            if mode == 0:
                self.emit(f"{args} {op}")
                continue
            if mode == 7:
                self.emit(f"{args} Tp")
                self.text_clip = True
                continue
            if mode in (1, 2, 5, 6):
                if mode in (2, 6):
                    self.set_color(fill=True)
                    self.emit(f"q n {args} Tp f Q")
                self.set_color(fill=False)
                self.emit(f"q n {args} Tp S Q")
            if mode == 4:
                self.emit(f"{args} {op}")
            if mode >= 4:
                self.emit(f"{args} Tp")
                self.text_clip = True

    # Type 3 glyphs
    def op_d0(self, ops):
        wx, wy = self._nums(ops, 2)
        self.emit(f"{num(wx)} {num(wy)} setcharwidth")

    def op_d1(self, ops):
        v = self._nums(ops, 6)
        self.emit(nums(v) + " setcachedevice")
        self.cached_glyph = True

    # marked content and optional content
    def op_BMC(self, ops):
        self.mc.append(False)

    def op_BDC(self, ops):
        hide = False
        if len(ops) >= 2 and ops[-2] == "OC":
            props = ops[-1]
            if isinstance(props, str):
                pr = self.doc.resolve(self.res.get("Properties")) or {}
                props = pr.get(props) if isinstance(pr, dict) else None
            hide = not self.w.visible(props)
        self.mc.append(hide)
        if hide:
            self.hidden += 1

    def op_EMC(self, ops):
        if self.mc and self.mc.pop():
            self.hidden -= 1

    # XObjects
    def op_Do(self, ops):
        if self.hidden:
            return
        xo = self.doc.resolve(self.res.get("XObject")) or {}
        ref = xo.get(str(ops[-1])) if isinstance(xo, dict) else None
        stm = self.doc.resolve(ref)
        if not isinstance(stm, Stream):
            return
        if not self.w.visible(stm.dict.get("OC")):
            return
        sub = self.doc.resolve(stm.dict.get("Subtype"))
        if sub == "Image":
            if not self.cached_glyph or self.doc.resolve(stm.dict.get("ImageMask")) is True:
                self.image(stm.dict, stm, None)
        elif sub == "Form":
            self.form(stm)

    def form(self, stm: Stream, outer: tuple | None = None):
        w = self.w
        key = stm.num or id(stm)
        if key in w.active or self.depth >= MAX_DEPTH:
            return
        d = stm.dict
        r = self.doc.resolve
        fmatrix = matrix(self.doc, d.get("Matrix"))
        bbox = r(d.get("BBox"))
        self.emit("q")
        if outer is not None:
            self.emit(arr(outer) + " cm")
        if fmatrix != IDENTITY:
            self.emit(arr(fmatrix) + " cm")
        if isinstance(bbox, list) and len(bbox) == 4:
            x0, y0, x1, y1 = (_f(r(v)) for v in bbox)
            self.emit(f"{num(min(x0, x1))} {num(min(y0, y1))} {num(abs(x1 - x0))} {num(abs(y1 - y0))} rectclip")
        res = r(d.get("Resources"))
        if not isinstance(res, dict):
            res = self.res
        gs = self.gs.copy()
        child = Content(w, self.out, res, gs, self.inline, self.depth + 1, glyph=self.glyph)
        child.cached_glyph = self.cached_glyph
        child.hidden = self.hidden
        w.active.add(key)
        try:
            child.run(self.doc.decode(stm))
        finally:
            w.active.discard(key)
        self.emit("Q")

    # pictures
    def inline_image(self, d: dict, data: bytes):
        full = {}
        for k, v in d.items():
            k = ABBREV.get(k, k)
            if k == "Filter":
                v = [FILTER_ABBREV.get(str(x), str(x)) for x in v] if isinstance(v, list) else FILTER_ABBREV.get(str(v), str(v))
            elif k == "ColorSpace" and isinstance(v, str):
                v = {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK", "I": "Indexed"}.get(v, v)
            elif k == "ColorSpace" and isinstance(v, list) and v and v[0] in ("I", "Indexed"):
                base = {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK"}.get(str(v[1]), v[1]) if len(v) > 1 else "DeviceRGB"
                v = ["Indexed", base, *v[2:]]
            full[k] = v
        if self.cached_glyph and full.get("ImageMask") is not True:
            return
        self.image(full, Stream(full, data), None)

    def image(self, d: dict, stm: Stream, _unused):
        self.w.images.write(self, d, stm)


# --- operators ---------------------------------------------------------------------------------------------

OPS = {
    b"q": Content.op_q, b"Q": Content.op_Q, b"cm": Content.op_cm, b"w": Content.op_w, b"J": Content.op_J,
    b"j": Content.op_j, b"M": Content.op_M, b"d": Content.op_d, b"gs": Content.op_gs,
    b"m": Content.op_m, b"l": Content.op_l, b"c": Content.op_c, b"v": Content.op_v, b"y": Content.op_y,
    b"h": Content.op_h, b"re": Content.op_re, b"W": Content.op_W, b"W*": Content.op_Wstar,
    b"S": Content.op_S, b"s": Content.op_s, b"f": Content.op_f, b"F": Content.op_f, b"f*": Content.op_fstar,
    b"B": Content.op_B, b"B*": Content.op_Bstar, b"b": Content.op_b, b"b*": Content.op_bstar, b"n": Content.op_n,
    b"cs": Content.op_cs, b"CS": Content.op_CS, b"sc": Content.op_sc, b"scn": Content.op_sc, b"SC": Content.op_SC,
    b"SCN": Content.op_SC, b"g": Content.op_g, b"G": Content.op_G, b"rg": Content.op_rg, b"RG": Content.op_RG,
    b"k": Content.op_k, b"K": Content.op_K, b"sh": Content.op_sh,
    b"BT": Content.op_BT, b"ET": Content.op_ET, b"Tf": Content.op_Tf, b"Tc": Content.op_Tc, b"Tw": Content.op_Tw,
    b"Tz": Content.op_Tz, b"TL": Content.op_TL, b"Ts": Content.op_Ts, b"Tr": Content.op_Tr, b"Td": Content.op_Td,
    b"TD": Content.op_TD, b"Tm": Content.op_Tm, b"T*": Content.op_Tstar, b"Tj": Content.op_Tj, b"TJ": Content.op_TJ,
    b"'": Content.op_quote, b'"': Content.op_dquote, b"d0": Content.op_d0, b"d1": Content.op_d1,
    b"BMC": Content.op_BMC, b"BDC": Content.op_BDC, b"EMC": Content.op_EMC, b"Do": Content.op_Do,
}


# --- the document ------------------------------------------------------------------------------------------

class Writer:
    def __init__(self, doc: Document, notes: list[str] | None, flatten):
        self.doc = doc
        self.notes = notes
        self.flatten = flatten
        self.fonts: dict = {}
        self.font_order: list[FontRec] = []
        self.setup: list[str] = []
        self.patterns: dict = {}
        self.images = Images(self)
        self.image_uses: dict[int, int] = {}
        self.shared_images: dict[int, str] = {}
        self._uid = 0
        self._noted: set[str] = set()
        self.active: set = set()
        self.ops = 0
        self.cut = False
        self.page_transparent = False
        self.page_unshowable: str | None = None
        self.off = self._optional_content()

    def count_images(self, pages: list[dict]) -> None:
        """How many times each image XObject is drawn in the document (forms and appearances included)."""
        r = self.doc.resolve
        forms: dict = {}
        budget = [limits.MAX_PDF_OPERATORS]

        def scan(data: bytes, res, depth: int, inside: frozenset):
            if depth > MAX_DEPTH or not isinstance(res, dict) or budget[0] <= 0:
                return
            xo = r(res.get("XObject"))
            if not isinstance(xo, dict):
                return
            prev = None
            toks = content_tokens(data)
            budget[0] -= len(toks)
            for kind, v in toks:
                if kind == "kw" and v == b"Do" and isinstance(prev, str):
                    ref = xo.get(prev)
                    stm = r(ref)
                    num = getattr(ref, "num", None)
                    if isinstance(stm, Stream) and num is not None:
                        sub = r(stm.dict.get("Subtype"))
                        if sub == "Image":
                            self.image_uses[num] = self.image_uses.get(num, 0) + 1
                        elif sub == "Form" and num not in inside:
                            if num not in forms:
                                forms[num] = self.doc.decode(stm)
                            fres = r(stm.dict.get("Resources"))
                            scan(forms[num], fres if isinstance(fres, dict) else res, depth + 1, inside | {num})
                prev = v if kind == "v" else None

        for page in pages:
            contents = r(page.get("Contents"))
            streams = contents if isinstance(contents, list) else [contents]
            data = b"\n".join(self.doc.decode(r(x)) for x in streams if isinstance(r(x), Stream))
            scan(data, r(page.get("Resources")), 0, frozenset())

    def uid(self) -> int:
        self._uid += 1
        return self._uid

    def note(self, text: str):
        if text not in self._noted:
            self._noted.add(text)
            if self.notes is not None:
                self.notes.append(text)

    def transparent(self):
        self.page_transparent = True

    def unshowable(self, what: str):
        if self.flatten is not None:
            self.page_unshowable = what
        else:
            self.note(f"This PDF has {what}, which PostScript can't decode, so they were left out")

    # optional content
    def _optional_content(self) -> set:
        r = self.doc.resolve
        try:
            ocp = r(self.doc.root.get("OCProperties"))
        except BookportError:
            return set()
        off = set()
        if isinstance(ocp, dict):
            d = r(ocp.get("D"))
            if isinstance(d, dict):
                for x in r(d.get("OFF")) or []:
                    off.add(x.num if hasattr(x, "num") else id(x))
        return off

    def visible(self, oc) -> bool:
        if oc is None or not self.off:
            return True
        r = self.doc.resolve
        key = oc.num if hasattr(oc, "num") else None
        d = r(oc)
        if not isinstance(d, dict):
            return True
        if d.get("Type") == "OCMD" or "OCGs" in d:
            groups = r(d.get("OCGs"))
            groups = groups if isinstance(groups, list) else [d.get("OCGs")] if d.get("OCGs") is not None else []
            on = [(g.num if hasattr(g, "num") else id(g)) not in self.off for g in groups]
            policy = r(d.get("P")) or "AnyOn"
            if not on:
                return True
            return {"AllOn": all(on), "AnyOff": not all(on), "AllOff": not any(on)}.get(policy, any(on))
        return key not in self.off

    # fonts
    def font(self, ref) -> FontRec | None:
        key = ref.num if hasattr(ref, "num") else id(ref)
        if key not in self.fonts:
            fd = self.doc.resolve(ref)
            if not isinstance(fd, dict):
                self.fonts[key] = None
            else:
                rec = FontRec(self, f"bpF{len(self.font_order) + 1}", fd)
                self.fonts[key] = rec
                self.font_order.append(rec)
        return self.fonts[key]

    def type3(self, font: FontRec) -> str:
        doc = self.doc
        r = doc.resolve
        fd = font.fd
        procs = r(fd.get("CharProcs")) or {}
        res = r(fd.get("Resources"))
        res = res if isinstance(res, dict) else font.page_res if hasattr(font, "page_res") else {}
        fm = matrix(doc, fd.get("FontMatrix"), (0.001, 0, 0, 0.001, 0, 0))
        bbox = [_f(r(v)) for v in r(fd.get("FontBBox"))] if isinstance(r(fd.get("FontBBox")), list) else [0, 0, 0, 0]
        if len(bbox) != 4:
            bbox = [0, 0, 0, 0]
        names = sorted({font.names[c] for c in font.used if font.names[c] in procs})
        lines = [f"/{font.ps} 9 dict dup begin", "/FontType 3 def", f"/FontMatrix {arr(fm)} def",
                 f"/FontBBox {arr(bbox)} def",
                 "/Encoding [" + wrap([name(font.names[c] if font.names[c] in procs else ".notdef")
                                       for c in range(256)]) + "] def",
                 f"/CharProcs {len(names) + 1} dict dup begin", "/.notdef {0 0 setcharwidth} def"]
        for g in names:
            stm = r(procs.get(g))
            if not isinstance(stm, Stream):
                continue
            body: list[str] = []
            key = stm.num or id(stm)
            if key in self.active:
                continue
            self.active.add(key)
            try:
                ct = Content(self, body, res, GS(), inline=False, depth=1, glyph=True)
                data = doc.decode(stm)
                if not re.search(rb"\bd[01]\b", data[:400]):
                    code = next((c for c in font.used if font.names[c] == g), 0)
                    body.append(f"{num(font.widths.get(code, 0))} 0 setcharwidth")
                ct.run(data)
            finally:
                self.active.discard(key)
            lines.append(f"{name(g)} {{\n" + "\n".join(body) + "\n} bind def")
        lines += ["end def", "/BuildGlyph {BuildGlyph3} def /BuildChar {BuildChar3} def", "end",
                  f"/{font.ps} exch definefont pop"]
        return "\n".join(lines) + "\n"

    # patterns
    def pattern_color(self, c: Content, cs: ColorSpace, comps) -> str | None:
        if not isinstance(comps, tuple) or comps[0] != "pattern" or comps[1] is None:
            return None
        doc = self.doc
        r = doc.resolve
        pats = r(c.res.get("Pattern")) or {}
        ref = pats.get(comps[1]) if isinstance(pats, dict) else None
        pat = r(ref)
        if pat is None:
            return None
        d = pat.dict if isinstance(pat, Stream) else pat
        if not isinstance(d, dict):
            return None
        key = ref.num if hasattr(ref, "num") else id(pat)
        proto = self.patterns.get(key)
        if proto is None:
            proto = self._pattern(pat, d, c)
            self.patterns[key] = proto
        if not proto:
            return None
        pname, uncolored = proto
        pmatrix = matrix(doc, d.get("Matrix"))
        base = c.base or "B0"
        use = f"{pname} {arr(pmatrix)} {base} mkpat setpattern"
        if uncolored:
            under = cs.base or RGB
            vals = under.convert(comps[2]) if comps[2] else [0.0] * DEVICE[under.device][1]
            return f"/{DEVICE[under.device][0]} setcolorspace {nums(round(v, 4) for v in vals)} {use}"
        return use

    def _pattern(self, pat, d: dict, c: Content):
        doc = self.doc
        r = doc.resolve
        kind = r(d.get("PatternType"))
        pname = f"P{self.uid()}"
        if kind == 2:
            got = shading(doc, d.get("Shading"), c.res, inline=False)
            if got is None:
                self.note("A shading couldn't be written to PostScript and was left out")
                return False
            if r(d.get("ExtGState")):
                eg = r(d.get("ExtGState"))
                if isinstance(eg, dict) and (_f(r(eg.get("ca")), 1) < 0.999 or _f(r(eg.get("CA")), 1) < 0.999):
                    self.transparent()
            self.setup.append(f"/{pname} << /PatternType 2 /Shading {got[0]} >> def")
            return pname, False
        if kind != 1 or not isinstance(pat, Stream):
            return False
        bbox = [_f(r(v)) for v in r(d.get("BBox"))] if isinstance(r(d.get("BBox")), list) else [0, 0, 1, 1]
        xs, ys = _f(r(d.get("XStep")), 1) or 1, _f(r(d.get("YStep")), 1) or 1
        paint = r(d.get("PaintType"))
        tiling = r(d.get("TilingType"))
        res = r(d.get("Resources"))
        res = res if isinstance(res, dict) else c.res
        body: list[str] = []
        key = pat.num or id(pat)
        if key in self.active:
            return False
        self.active.add(key)
        try:
            ct = Content(self, body, res, GS(), inline=False, depth=c.depth + 1)
            if paint == 2:
                ct.cached_glyph = True  # an uncoloured pattern's cell takes its colour from where it's used
            ct.run(doc.decode(pat))
        finally:
            self.active.discard(key)
        self.setup.append(f"/{pname} << /PatternType 1 /PaintType {2 if paint == 2 else 1} "
                          f"/TilingType {tiling if tiling in (1, 2, 3) else 1} /BBox {arr(bbox)} /XStep {num(xs)} "
                          f"/YStep {num(ys)}\n/PaintProc {{pop bookportdict begin\n" + "\n".join(body) + "\nend} >> def")
        return pname, paint == 2

    # pages
    def page_ps(self, index: int, page: dict, eps: bool) -> tuple[str, tuple[float, float]]:
        doc = self.doc
        r = doc.resolve
        x0, y0, x1, y1 = self.box(page)
        rot = r(page.get("Rotate"))
        rot = (rot if isinstance(rot, int) else 0) % 360 // 90 * 90
        wd, ht = x1 - x0, y1 - y0
        shown = (ht, wd) if rot in (90, 270) else (wd, ht)
        turn = {0: (1, 0, 0, 1, -x0, -y0), 90: (0, -1, 1, 0, -y0, x1), 180: (-1, 0, 0, -1, x1, y1),
                270: (0, 1, -1, 0, y1, -x0)}[rot]
        self.page_transparent = False
        self.page_unshowable = None
        self.ops = 0
        self.cut = False
        body: list[str] = []
        kept = (len(self.notes) if self.notes is not None else 0, set(self._noted))
        self._page_body(body, page, turn, (x0, y0, wd, ht))
        if (self.page_transparent or self.page_unshowable) and self.flatten is not None:
            pic = self._flat(index, shown)
            if pic is not None:
                if self.notes is not None:  # what the page's drawing noted no longer applies to its picture
                    del self.notes[kept[0]:]
                self._noted = kept[1]
                why = "transparency" if self.page_transparent else self.page_unshowable
                self.note(f"Page {index + 1} uses {why}, so it was drawn as a picture")
                return pic, shown
        elif self.page_transparent:
            self.note("Some pages use transparency, which PostScript doesn't have; they were drawn without it")
        return "\n".join(body), shown

    def _page_body(self, body: list[str], page: dict, turn, box):
        doc = self.doc
        r = doc.resolve
        res = r(page.get("Resources"))
        res = res if isinstance(res, dict) else {}
        body.append(arr(turn) + " cm")
        body.append(f"{num(box[0])} {num(box[1])} {num(box[2])} {num(box[3])} rectclip")
        body.append("/B0 matrix currentmatrix def")
        contents = r(page.get("Contents"))
        streams = contents if isinstance(contents, list) else [contents]
        data = b"\n".join(doc.decode(r(s)) for s in streams if isinstance(r(s), Stream))
        gs = GS()
        ct = Content(self, body, res, gs, inline=True, depth=0)
        ct.base = "B0"
        body.append("q")
        ct.run(data)
        body.append("Q")
        for a in r(page.get("Annots")) or []:
            self._annot(body, r(a), res)

    def _annot(self, body: list[str], a, page_res):
        doc = self.doc
        r = doc.resolve
        if not isinstance(a, dict) or r(a.get("Subtype")) == "Popup":
            return
        flags = r(a.get("F"))
        flags = flags if isinstance(flags, int) else 0
        if flags & (2 | 32):  # Hidden, NoView
            return
        if not self.visible(a.get("OC")):
            return
        ap = r(a.get("AP"))
        normal = r(ap.get("N")) if isinstance(ap, dict) else None
        if isinstance(normal, dict) and not isinstance(normal, Stream):
            state = r(a.get("AS"))
            normal = r(normal.get(str(state))) if state is not None else None
        if not isinstance(normal, Stream):
            return
        rect = r(a.get("Rect"))
        if not isinstance(rect, list) or len(rect) != 4:
            return
        rx0, ry0, rx1, ry1 = (_f(r(v)) for v in rect)
        rx0, rx1 = min(rx0, rx1), max(rx0, rx1)
        ry0, ry1 = min(ry0, ry1), max(ry0, ry1)
        bbox = r(normal.dict.get("BBox"))
        if not isinstance(bbox, list) or len(bbox) != 4:
            return
        m = matrix(doc, normal.dict.get("Matrix"))
        bx = [_f(r(v)) for v in bbox]
        pts = [(bx[0], bx[1]), (bx[2], bx[1]), (bx[0], bx[3]), (bx[2], bx[3])]
        tp = [(m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]) for x, y in pts]
        tx0, tx1 = min(p[0] for p in tp), max(p[0] for p in tp)
        ty0, ty1 = min(p[1] for p in tp), max(p[1] for p in tp)
        sx = (rx1 - rx0) / (tx1 - tx0) if tx1 > tx0 else 1
        sy = (ry1 - ry0) / (ty1 - ty0) if ty1 > ty0 else 1
        fit = (sx, 0, 0, sy, rx0 - tx0 * sx, ry0 - ty0 * sy)
        ct = Content(self, body, page_res, GS(), inline=True, depth=0)
        ct.base = "B0"
        ct.form(normal, outer=fit)

    def box(self, page: dict) -> tuple[float, float, float, float]:
        r = self.doc.resolve

        def rect(v):
            v = r(v)
            if isinstance(v, list) and len(v) == 4:
                vals = [_f(r(x)) for x in v]
                a, b = sorted(vals[0::2]), sorted(vals[1::2])
                if a[1] > a[0] and b[1] > b[0]:
                    return a[0], b[0], a[1], b[1]
            return None
        media = rect(page.get("MediaBox")) or (0.0, 0.0, 612.0, 792.0)
        crop = rect(page.get("CropBox"))
        if crop:
            x0, y0 = max(crop[0], media[0]), max(crop[1], media[1])
            x1, y1 = min(crop[2], media[2]), min(crop[3], media[3])
            if x1 > x0 and y1 > y0:
                return x0, y0, x1, y1
        return media

    def _flat(self, index: int, shown: tuple[float, float]) -> str | None:
        try:
            got = self.flatten(index, FLATTEN_DPI)
        except Exception:
            return None
        if not got:
            return None
        pw, ph, rgb = got
        if not isinstance(pw, int) or not isinstance(ph, int) or pw <= 0 or ph <= 0 or len(rgb) < pw * ph * 3:
            return None
        jpeg = _jpeg(pw, ph, rgb)
        if jpeg is not None:
            src, payload = "currentfile /ASCII85Decode filter /DCTDecode filter", jpeg
        else:
            src, payload = "currentfile /ASCII85Decode filter /FlateDecode filter", zlib.compress(rgb[:pw * ph * 3], 6)
        return (f"q [{num(shown[0])} 0 0 {num(shown[1])} 0 0] cm /DeviceRGB setcolorspace\n"
                f"<< /ImageType 1 /Width {pw} /Height {ph} /BitsPerComponent 8 /Decode [0 1 0 1 0 1] "
                f"/ImageMatrix [{pw} 0 0 {-ph} 0 {ph}] /Interpolate true\n/DataSource {src} >> image\n"
                + a85(payload) + "\nQ")


def _jpeg(w: int, h: int, rgb: bytes) -> bytes | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    import io
    buf = io.BytesIO()
    Image.frombytes("RGB", (w, h), bytes(rgb[:w * h * 3])).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def convert(data: bytes, eps: bool = False, page: int = 0, flatten=None, notes: list[str] | None = None) -> bytes:
    """A PDF as PostScript (every page) or as EPS (page ``page``)."""
    doc = Document(data)
    pages = doc.pages()
    if not pages:
        raise BookportError("This PDF has no pages")
    if eps:
        if not 0 <= page < len(pages):
            raise BookportError(f"There's no page {page + 1}: this PDF has {len(pages)} page{'s' if len(pages) != 1 else ''}")
        chosen = [(page, pages[page])]
    else:
        chosen = list(enumerate(pages))
    w = Writer(doc, notes, flatten)
    if len(chosen) > 1:
        w.count_images([p for _, p in chosen])
    bodies = []
    for index, p in chosen:
        bodies.append(w.page_ps(index, p, eps))
    fonts = []
    for rec in w.font_order:
        try:
            text = rec.define()
        except (ValueError, IndexError, KeyError, wf.struct.error, ZeroDivisionError):
            w.note(f"The font {wf._plain(rec.base) or 'in the PDF'} couldn't be written, so its text uses Helvetica")
            text = (f"/Helvetica findfont dup length dict begin {{1 index /FID ne {{def}} {{pop pop}} ifelse}} forall "
                    f"currentdict end /{rec.ps} exch definefont pop\n") if not rec.composite else "".join(
                f"/Helvetica findfont /{n} exch definefont pop\n" for n in rec.ps_names())
        names = rec.ps_names()
        fonts.append(f"%%BeginResource: font {names[0]}\n" + text + "%%EndResource\n"
                     + "bookportdict begin " + " ".join(f"/{n} /{n} findfont def" for n in names) + " end")
    return _assemble(w, doc, bodies, fonts, eps)


def _info(doc: Document, key: str) -> str:
    from ..pdf.parser import text_string
    v = doc.resolve(doc.info().get(key))
    if not isinstance(v, (bytes, str)):
        return ""
    s = text_string(v)
    return "".join(ch if 32 <= ord(ch) < 127 and ch not in "()\\" else "?" for ch in s)[:120]


def _assemble(w: Writer, doc: Document, bodies, fonts, eps: bool) -> bytes:
    maxw = max(s[0] for _, s in bodies)
    maxh = max(s[1] for _, s in bodies)
    first = bodies[0][1]
    out = ["%!PS-Adobe-3.0" + (" EPSF-3.0" if eps else "")]
    title = _info(doc, "Title")
    bb = (first if eps else (maxw, maxh))
    out += ["%%Creator: bookport", f"%%Title: {title}" if title else "%%Title: (untitled)",
            f"%%BoundingBox: 0 0 {math.ceil(bb[0] - 1e-6)} {math.ceil(bb[1] - 1e-6)}",
            f"%%HiResBoundingBox: 0 0 {num(round(bb[0], 3))} {num(round(bb[1], 3))}",
            "%%LanguageLevel: 3", "%%DocumentData: Clean7Bit"]
    if not eps:
        out += [f"%%Pages: {len(bodies)}", f"%%DocumentMedia: Default {num(first[0])} {num(first[1])} 0 () ()"]
    else:
        out.append("%%Pages: 1")
    out += ["%%EndComments", "%%BeginProlog", "%%BeginResource: procset bookport 1.0 0", PROLOG.rstrip(),
            "%%EndResource", "%%EndProlog", "%%BeginSetup"]
    out += fonts
    if w.setup:
        out += ["bookportdict begin", *w.setup, "end"]
    out.append("%%EndSetup")
    for k, (body, size) in enumerate(bodies, start=1):
        out += [f"%%Page: {k} {k}", f"%%PageBoundingBox: 0 0 {math.ceil(size[0] - 1e-6)} {math.ceil(size[1] - 1e-6)}"]
        if not eps:
            out += ["%%BeginPageSetup", f"<< /PageSize [{num(size[0])} {num(size[1])}] >> setpagedevice",
                    "%%EndPageSetup"]
        out += ["bookportdict begin /bpsave save def", body, "bpsave restore end", "showpage", "%%PageTrailer"]
    out += ["%%Trailer", "%%EOF", ""]
    return "\n".join(out).encode("latin-1", "replace")
