"""PostScript and EPS to SVG, for figures in books (EPUB, HTML) that can't hold a PDF.

The program runs as it does for PDF output (topdf.run), into a document that keeps every object it is given as a
Python value instead of writing it out. A page's content stream is then read back and drawn as SVG 1.1 (ISO 32000-1
chapter 8 for what each operator means, the SVG 1.1 specification for how it is written):

- paths filled with their rule, strokes with the matrix they were stroked under (line widths and dashes are in
  user space), clips as ``clipPath``, colours as sRGB (CMYK by the simple complement, as the PDF side does);
- pictures as PNG data (JPEG pictures as they are), image masks in the colour they were painted with;
- axial and radial shadings as gradients (with a plain colour after the ``url()`` for readers without them),
  the other shading types drawn as pictures; tiling patterns as ``pattern`` elements;
- text as glyph outlines, so it looks right without its fonts: Type 1 charstrings, CFF (Type 2) charstrings,
  TrueType ``glyf`` (Type 42) and Type 3 glyphs (what their BuildGlyph painted). The standard 14 fonts, named and
  not in the file, become ``<text>`` in a similar family (there are no outlines for them here).

An SVG holds one page: the first, or the one asked for."""

from __future__ import annotations

import base64
import math
import struct
import zlib
from xml.sax.saxutils import escape, quoteattr

from ..errors import BookportError
from ..images import Raster, encode_png
from ..pdf.objects import Ref, Writer
from ..pdf.parser import content_tokens
from . import fonts as FT
from .device import Document
from .outlines import Type1Outlines, Type2Outlines, truetype

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
MAX_RASTER = 400  # the long side of a shading drawn as a picture, in pixels
MAX_TRIANGLES = 40000
MAX_DEPTH = 8  # patterns and Type 3 glyphs drawn inside each other


# ---- the document that records instead of writing

class _Recorder(Writer):
    """A PDF writer that keeps each object as the Python value it was given (streams: their dict and bytes)."""

    def __init__(self):
        super().__init__()
        self.values: dict[int, object] = {}
        self.datas: dict[int, bytes] = {}

    def put(self, ref: Ref, value) -> Ref:
        self.values[ref.num] = value
        return ref

    def stream(self, data: bytes, extra: dict | None = None, compress: bool = True, ref: Ref | None = None) -> Ref:
        ref = ref or self.reserve()
        self.values[ref.num] = dict(extra or {})
        self.datas[ref.num] = bytes(data)
        return ref


class SvgDocument(Document):
    """Where the program's pages go for SVG output: (content stream, resources, size) for each."""

    def __init__(self):
        super().__init__()
        self.w = _Recorder()
        self.pages_ref = self.w.reserve()
        self.svg_pages: list[tuple[bytes, dict, tuple[float, float]]] = []

    def add_page(self, content, size) -> None:
        self.svg_pages.append((content.data(), content.resources(), size))
        self.pages.append(None)


def convert(data: bytes, eps: bool | None = None, notes: list[str] | None = None, page: int = 0) -> bytes:
    from . import topdf
    doc = SvgDocument()
    topdf.run(data, eps, doc)
    pages = doc.svg_pages
    if not 0 <= page < len(pages):
        raise BookportError(f"There is no page {page + 1}: the PostScript program drew {len(pages)}")
    if len(pages) > 1:
        doc.note(f"An SVG holds one page: page {page + 1} of the {len(pages)} the program drew was kept")
    out = _Svg(doc).page(*pages[page], title=doc.info.get("Title"))
    if notes is not None:
        notes.extend(doc.notes)
    return out.encode("utf-8")


# ---- small helpers

def mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def invert(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if not det or not math.isfinite(det):
        return None
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def apply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def scale_of(m) -> float:
    return abs(m[0] * m[3] - m[1] * m[2]) ** 0.5


def _fmt(v: float, places: int) -> str:
    if not math.isfinite(v):
        v = 0.0
    s = f"{v:.{places}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "", "-") else s


def _places(scale: float) -> int:
    """Decimals for coordinates whose unit is ``scale`` points: about a thousandth of a point."""
    if scale <= 0 or not math.isfinite(scale):
        return 3
    return max(0, min(8, 3 + math.ceil(-math.log10(scale))))


def _matrix(m, places: int = 6) -> str:
    return "matrix(" + " ".join(_fmt(v, places if i < 4 else max(places - 3, 3)) for i, v in enumerate(m)) + ")"


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)


def _cmyk(c, m, y, k):
    return (1 - min(1.0, c + k), 1 - min(1.0, m + k), 1 - min(1.0, y + k))


def path_d(segs, m=None, places: int = 3) -> str:
    out = []
    for s in segs:
        op = s[0]
        if op == "h":
            out.append("Z")
            continue
        vals = s[1:]
        if m is not None:
            pts = []
            for k in range(0, len(vals), 2):
                pts += apply(m, vals[k], vals[k + 1])
            vals = pts
        out.append({"m": "M", "l": "L", "c": "C"}[op] + " ".join(_fmt(v, places) for v in vals))
    return "".join(out)


def paint_attrs(**paints: str | None) -> str:
    """fill="..." and stroke="..." attributes. A gradient or pattern goes in the style attribute, with its plain
    colour as the attribute: readers without paint servers (MuPDF's SVG, say) then draw that colour, not black."""
    attrs, style = [], []
    for name, p in paints.items():
        if p is None:
            continue
        if p.startswith("url("):
            url, _, plain = p.partition(" ")
            attrs.append(f' {name}="{plain or "none"}"')
            style.append(f"{name}:{url}")
        else:
            attrs.append(f' {name}="{p}"')
    if style:
        attrs.append(f' style="{";".join(style)}"')
    return "".join(attrs)


def _rect(x0, y0, x1, y1) -> list:
    return [("m", x0, y0), ("l", x1, y0), ("l", x1, y1), ("l", x0, y1), ("h",)]


_FAMILIES = {"Times": "'Times New Roman', Times, 'Nimbus Roman', serif",
             "Helvetica": "Helvetica, Arial, 'Nimbus Sans', 'Liberation Sans', sans-serif",
             "Courier": "'Courier New', Courier, 'Nimbus Mono PS', monospace",
             "Symbol": "Symbol, 'Standard Symbols PS', serif",
             "ZapfDingbats": "'Zapf Dingbats', ZapfDingbats, 'D050000L', sans-serif"}


# ---- the drawing state

class _GS:
    __slots__ = ("cap", "ctm", "dash", "fill", "font", "groups", "join", "lw", "miter", "phase", "stroke", "tr")

    def __init__(self):
        self.ctm = IDENTITY
        self.fill = ("rgb", (0.0, 0.0, 0.0))
        self.stroke = ("rgb", (0.0, 0.0, 0.0))
        self.lw, self.cap, self.join, self.miter = 1.0, 0, 0, 10.0
        self.dash, self.phase = [], 0.0
        self.font = None
        self.tr = 0
        self.groups = 0

    def copy(self) -> _GS:
        g = _GS.__new__(_GS)
        for k in _GS.__slots__:
            setattr(g, k, getattr(self, k))
        g.groups = 0
        return g


class _Ctx:
    """One content stream being drawn: the page, a pattern cell or a Type 3 glyph."""

    def __init__(self, res: dict, area, scale: float, mask: bool, depth: int):
        self.res = res or {}
        self.area = area  # what "everywhere" is here (for shadings painted with sh)
        self.scale = scale  # points per unit of this stream's default space
        self.mask = mask  # colours are the painter's (a cached Type 3 glyph, an uncoloured pattern)
        self.depth = depth


class _Svg:
    def __init__(self, doc: SvgDocument):
        self.doc = doc
        self.values = doc.w.values
        self.datas = doc.w.datas
        self.fonts = {f.ref.num: f for f in doc.fonts}
        self.defs: list[str] = []
        self.n = 0
        self.glyph_ids: dict = {}
        self.outlines: dict = {}
        self.image_ids: dict = {}
        self.paint_ids: dict = {}

    def new_id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def get(self, v):
        for _ in range(16):
            if isinstance(v, Ref):
                v = self.values.get(v.num)
            else:
                break
        return v

    def stream_data(self, v) -> bytes:
        if not isinstance(v, Ref):
            return b""
        data = self.datas.get(v.num, b"")
        d = self.values.get(v.num) or {}
        filt = d.get("Filter") if isinstance(d, dict) else None
        filters = filt if isinstance(filt, list) else [filt] if filt else []
        for f in filters:
            if f == "FlateDecode":
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    return b""
            else:
                break
        return data

    # ---- the page

    def page(self, data: bytes, res: dict, size, title: str | None = None) -> str:
        w, h = size
        body: list[str] = []
        ctx = _Ctx(res, (0.0, 0.0, w, h), 1.0, False, 0)
        self.run(data, ctx, _GS(), body)
        head = ['<?xml version="1.0" encoding="UTF-8"?>',
                ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" '
                 f'width="{_fmt(w, 3)}pt" height="{_fmt(h, 3)}pt" viewBox="0 0 {_fmt(w, 3)} {_fmt(h, 3)}">')]
        if title:
            head.append(f"<title>{escape(title)}</title>")
        if self.defs:
            head.append("<defs>")
            head += self.defs
            head.append("</defs>")
        head.append(f'<g transform="matrix(1 0 0 -1 0 {_fmt(h, 3)})">')
        return "\n".join([*head, *body, "</g>", "</svg>", ""])

    # ---- content streams

    def run(self, data: bytes, ctx: _Ctx, gs: _GS, out: list[str]) -> None:
        stack: list[_GS] = []
        ops: list = []
        path: list = []
        cp = (0.0, 0.0)
        start = (0.0, 0.0)
        clip = None
        text = None  # [Tm, line matrix, clip elements] inside BT ... ET
        arrays: list[list] = []
        tc = tw = 0.0
        size = 1.0
        toks = content_tokens(data)
        for kind, v in toks:
            if kind == "v":
                (arrays[-1] if arrays else ops).append(v)
                continue
            if kind == "aopen":
                arrays.append([])
                continue
            if kind == "aclose":
                if arrays:
                    a = arrays.pop()
                    (arrays[-1] if arrays else ops).append(a)
                continue
            if kind in ("dopen", "dclose"):
                continue
            op = v.decode("latin-1", "replace")
            o = ops
            ops = []
            try:
                # path construction
                if op == "m":
                    cp = start = (float(o[0]), float(o[1]))
                    path.append(("m", *cp))
                elif op == "l":
                    cp = (float(o[0]), float(o[1]))
                    path.append(("l", *cp))
                elif op == "c":
                    vals = [float(x) for x in o[:6]]
                    path.append(("c", *vals))
                    cp = (vals[4], vals[5])
                elif op == "v":
                    vals = [float(x) for x in o[:4]]
                    path.append(("c", cp[0], cp[1], *vals))
                    cp = (vals[2], vals[3])
                elif op == "y":
                    vals = [float(x) for x in o[:4]]
                    path.append(("c", vals[0], vals[1], vals[2], vals[3], vals[2], vals[3]))
                    cp = (vals[2], vals[3])
                elif op == "h":
                    path.append(("h",))
                    cp = start
                elif op == "re":
                    x, y, rw, rh = (float(x) for x in o[:4])
                    path += _rect(x, y, x + rw, y + rh)
                    cp = start = (x, y)
                # painting
                elif op in ("f", "F", "f*", "S", "s", "B", "B*", "b", "b*", "n"):
                    if op in ("s", "b", "b*"):
                        path.append(("h",))
                    if op in ("f", "F", "f*", "B", "B*", "b", "b*"):
                        self.fill(path, op.endswith("*"), gs, ctx, out)
                    if op in ("S", "s", "B", "B*", "b", "b*"):
                        self.stroke(path, gs, ctx, out)
                    if clip is not None:
                        self.clip(path, clip, gs, ctx, out)
                        clip = None
                    path = []
                elif op in ("W", "W*"):
                    clip = op == "W*"
                # state
                elif op == "q":
                    stack.append(gs)
                    gs = gs.copy()
                elif op == "Q":
                    if stack:
                        out += ["</g>"] * gs.groups
                        gs = stack.pop()
                elif op == "cm":
                    gs.ctm = mul(tuple(float(x) for x in o[:6]), gs.ctm)
                elif op == "w":
                    gs.lw = float(o[0])
                elif op == "J":
                    gs.cap = int(o[0])
                elif op == "j":
                    gs.join = int(o[0])
                elif op == "M":
                    gs.miter = float(o[0])
                elif op == "d":
                    gs.dash = [float(x) for x in o[0]] if isinstance(o[0], list) else []
                    gs.phase = float(o[1]) if len(o) > 1 else 0.0
                # colour
                elif op in ("g", "G", "rg", "RG", "k", "K"):
                    if not ctx.mask:
                        vals = [float(x) for x in o]
                        rgb = (vals[0],) * 3 if op in ("g", "G") else tuple(vals[:3]) if op in ("rg", "RG") \
                            else _cmyk(*vals[:4])
                        if op.islower():
                            gs.fill = ("rgb", rgb)
                        else:
                            gs.stroke = ("rgb", rgb)
                elif op in ("cs", "CS"):
                    if not ctx.mask:
                        sp = self.color_space(o[0], ctx)
                        paint = ("space", sp)
                        if op == "cs":
                            gs.fill = paint
                        else:
                            gs.stroke = paint
                elif op in ("sc", "scn", "SC", "SCN"):
                    if not ctx.mask:
                        cur = gs.fill if op.islower() else gs.stroke
                        paint = self.set_color(cur, o, ctx)
                        if op.islower():
                            gs.fill = paint
                        else:
                            gs.stroke = paint
                # pictures and shadings
                elif op == "Do":
                    self.xobject(o[0], gs, ctx, out)
                elif op == "sh":
                    self.shfill(o[0], gs, ctx, out)
                # text
                elif op == "BT":
                    text = [IDENTITY, IDENTITY, []]
                elif op == "ET":
                    if text is not None and text[2]:
                        self.clip_with(text[2], out, gs)
                    text = None
                elif op == "Tf":
                    gs.font = o[0]
                    size = float(o[1])
                elif op == "Tr":
                    gs.tr = int(o[0])
                elif op == "Tc":
                    tc = float(o[0])
                elif op == "Tw":
                    tw = float(o[0])
                elif op == "Tm" and text is not None:
                    text[0] = text[1] = tuple(float(x) for x in o[:6])
                elif op in ("Td", "TD") and text is not None:
                    text[0] = text[1] = mul((1, 0, 0, 1, float(o[0]), float(o[1])), text[1])
                elif op in ("TJ", "Tj") and text is not None:
                    items = o[0] if op == "TJ" else [o[0]]
                    text[0] = self.show(items, text, gs, ctx, out, size, tc, tw)
            except (IndexError, TypeError, ValueError, ZeroDivisionError, OverflowError, KeyError):
                continue
        out += ["</g>"] * gs.groups
        for g in reversed(stack):
            out += ["</g>"] * g.groups

    # ---- colours

    def color_space(self, name, ctx: _Ctx):
        if name in ("DeviceGray", "DeviceRGB", "DeviceCMYK", "Pattern"):
            return name
        v = self.get((ctx.res.get("ColorSpace") or {}).get(name))
        return v if v is not None else "DeviceGray"

    def set_color(self, cur, o, ctx: _Ctx):
        sp = cur[1] if cur[0] == "space" else cur[2] if cur[0] == "pattern" else None
        nums = [float(x) for x in o if isinstance(x, (int, float)) and not isinstance(x, bool)]
        names = [x for x in o if isinstance(x, str)]
        is_pattern = sp == "Pattern" or (isinstance(sp, list) and sp and sp[0] == "Pattern")
        if is_pattern and names:
            ref = (ctx.res.get("Pattern") or {}).get(names[-1])
            base = None
            if isinstance(sp, list) and len(sp) > 1:
                base = self.to_rgb(self.get(sp[1]), nums)
            return ("pattern", ref, sp, base)
        if sp is None:
            sp = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(len(nums), "DeviceGray")
        return ("rgb", self.to_rgb(sp, nums))

    def to_rgb(self, space, comps) -> tuple:
        space = self.get(space)
        comps = [float(c) for c in comps]
        try:
            if isinstance(space, list) and space:
                kind = space[0]
                if kind == "Indexed":
                    base = self.get(space[1])
                    table = self.get(space[3])
                    if isinstance(space[3], Ref):
                        table = self.stream_data(space[3])
                    n = _ncomp(self, base)
                    i = max(0, min(int(space[2]), int(round(comps[0]) if comps else 0)))
                    vals = [b / 255 for b in bytes(table)[i * n:(i + 1) * n]]
                    return self.to_rgb(base, vals + [0.0] * (n - len(vals)))
                if kind in ("Separation", "DeviceN"):
                    fn = self.function(space[3])
                    return self.to_rgb(space[2], fn(comps))
                if kind == "ICCBased":
                    n = (self.get(space[1]) or {}).get("N", 3)
                    space = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(n, "DeviceRGB")
                elif kind in ("CalGray", "CalRGB", "Lab"):
                    space = "DeviceGray" if kind == "CalGray" else "DeviceRGB"
                else:
                    space = "DeviceGray"
            if space in ("DeviceRGB", "RGB"):
                c = comps + [0.0] * (3 - len(comps))
                return (c[0], c[1], c[2])
            if space in ("DeviceCMYK", "CMYK"):
                c = comps + [0.0] * (4 - len(comps))
                return _cmyk(*c[:4])
        except (IndexError, TypeError, ValueError):
            return (0.0, 0.0, 0.0)
        g = comps[0] if comps else 0.0
        return (g, g, g)

    # ---- functions (ISO 32000-1, 7.10): sampled, exponential, stitching

    def function(self, f, depth: int = 0):
        if depth > 8:
            return lambda xs: [0.0]
        if isinstance(f, list):
            fs = [self.function(x, depth + 1) for x in f]
            return lambda xs: [v for g in fs for v in g(xs)[:1]]
        d = self.get(f)
        if not isinstance(d, dict):
            return lambda xs: list(xs)
        ft = d.get("FunctionType")
        dom = [float(x) for x in d.get("Domain", [0, 1])]
        rng = [float(x) for x in d.get("Range", [])] if d.get("Range") else None

        def clip_out(vals):
            if rng:
                return [max(rng[2 * i], min(rng[2 * i + 1], v)) if 2 * i + 1 < len(rng) else v
                        for i, v in enumerate(vals)]
            return vals

        if ft == 2:
            c0 = [float(x) for x in d.get("C0", [0.0])]
            c1 = [float(x) for x in d.get("C1", [1.0])]
            n = float(d.get("N", 1))

            def exp(xs):
                x = max(dom[0], min(dom[1], xs[0] if xs else 0.0))
                try:
                    t = x ** n
                except (OverflowError, ZeroDivisionError, ValueError):
                    t = 0.0
                if isinstance(t, complex):
                    t = 0.0
                return clip_out([a + t * (b - a) for a, b in zip(c0, c1)])
            return exp
        if ft == 3:
            subs = [self.function(x, depth + 1) for x in d.get("Functions", [])]
            bounds = [float(x) for x in d.get("Bounds", [])]
            enc = [float(x) for x in d.get("Encode", [])]

            def stitch(xs):
                x = max(dom[0], min(dom[1], xs[0] if xs else 0.0))
                k = 0
                while k < len(bounds) and x >= bounds[k]:
                    k += 1
                k = min(k, len(subs) - 1)
                if k < 0:
                    return [0.0]
                lo = dom[0] if k == 0 else bounds[k - 1]
                hi = dom[1] if k >= len(bounds) else bounds[k]
                e0, e1 = (enc[2 * k], enc[2 * k + 1]) if 2 * k + 1 < len(enc) else (0.0, 1.0)
                t = e0 if hi == lo else e0 + (x - lo) * (e1 - e0) / (hi - lo)
                return clip_out(subs[k]([t]))
            return stitch
        if ft == 0:
            return self._sampled(d, self.stream_data(f) if isinstance(f, Ref) else b"", dom, rng or [0, 1])
        return lambda xs: [0.0] * max(1, len(rng or [0, 1]) // 2)

    @staticmethod
    def _sampled(d: dict, data: bytes, dom: list, rng: list):
        sizes = [max(1, int(x)) for x in d.get("Size", [2])]
        m = len(sizes)
        n = max(1, len(rng) // 2)
        bps = int(d.get("BitsPerSample", 8))
        enc = [float(x) for x in d.get("Encode", [])] or [v for s in sizes for v in (0.0, s - 1.0)]
        dec = [float(x) for x in d.get("Decode", [])] or rng
        top = (1 << bps) - 1 if bps < 64 else 1
        total = 1
        for s in sizes:
            total *= s
        cache: dict[int, list] = {}

        def sample(idx: int) -> list:
            got = cache.get(idx)
            if got is None:
                got = []
                for j in range(n):
                    bit = (idx * n + j) * bps
                    byte = bit >> 3
                    if bps == 8:
                        raw = data[byte] if byte < len(data) else 0
                    elif bps == 16:
                        raw = int.from_bytes(data[byte:byte + 2].ljust(2, b"\0"), "big")
                    else:
                        chunk = int.from_bytes(data[byte:byte + 5].ljust(5, b"\0"), "big")
                        raw = (chunk >> (40 - (bit & 7) - bps)) & top
                    got.append(raw)
                if len(cache) < 100000:
                    cache[idx] = got
            return got

        def fn(xs):
            pos = []
            for i in range(m):
                x = xs[i] if i < len(xs) else 0.0
                d0, d1 = dom[2 * i], dom[2 * i + 1] if 2 * i + 1 < len(dom) else 1.0
                x = max(d0, min(d1, x))
                e0, e1 = enc[2 * i], enc[2 * i + 1]
                e = e0 if d1 == d0 else e0 + (x - d0) * (e1 - e0) / (d1 - d0)
                pos.append(max(0.0, min(sizes[i] - 1.0, e)))
            out = [0.0] * n
            corners = 1 << min(m, 4)
            for c in range(corners):
                weight = 1.0
                idx = 0
                stride = 1
                for i in range(m):
                    lo = int(pos[i])
                    frac = pos[i] - lo
                    hi_bit = (c >> i) & 1 if i < 4 else 0
                    k = min(lo + hi_bit, sizes[i] - 1)
                    weight *= frac if hi_bit else 1.0 - frac
                    idx += k * stride
                    stride *= sizes[i]
                if weight == 0.0 or idx >= total:
                    continue
                s = sample(idx)
                for j in range(n):
                    out[j] += weight * s[j]
            res = []
            for j in range(n):
                d0, d1 = (dec[2 * j], dec[2 * j + 1]) if 2 * j + 1 < len(dec) else (0.0, 1.0)
                v = d0 + out[j] * (d1 - d0) / top
                r0, r1 = (rng[2 * j], rng[2 * j + 1]) if 2 * j + 1 < len(rng) else (0.0, 1.0)
                res.append(max(r0, min(r1, v)))
            return res
        return fn

    # ---- paint

    def paint(self, p, elem_ctm, ctx: _Ctx) -> str:
        """An SVG paint for a colour or pattern, for an element drawn under ``elem_ctm`` (in this stream's space)."""
        if p is None:
            return "none"
        if p[0] == "rgb":
            return _hex(p[1])
        if p[0] == "space":
            sp = p[1]
            if sp == "Pattern" or (isinstance(sp, list) and sp and sp[0] == "Pattern"):
                return "none"
            return _hex(self.to_rgb(sp, [0.0, 0.0, 0.0, 1.0] if sp == "DeviceCMYK" else [0.0]))
        if p[0] == "pattern":
            return self.pattern_paint(p[1], p[3], elem_ctm, ctx)
        return "#000000"

    def pattern_paint(self, ref, base, elem_ctm, ctx: _Ctx) -> str:
        d = self.get(ref)
        if not isinstance(d, dict):
            return "none"
        inv = invert(elem_ctm) or IDENTITY
        m = tuple(float(x) for x in d.get("Matrix", IDENTITY))
        m = mul(m, inv)
        key = (ref.num if isinstance(ref, Ref) else id(ref), base, m)
        got = self.paint_ids.get(key)
        if got is not None:
            return got
        if d.get("PatternType") == 2:
            got = self.gradient_paint(d.get("Shading"), m)
        else:
            got = self.tiling(ref, d, base, m, ctx)
        self.paint_ids[key] = got
        return got

    def tiling(self, ref, d: dict, base, m, ctx: _Ctx) -> str:
        if ctx.depth >= MAX_DEPTH:
            return _hex(base or (0.5, 0.5, 0.5))
        bb = [float(x) for x in d.get("BBox", [0, 0, 1, 1])]
        xs, ys = float(d.get("XStep", bb[2] - bb[0])), float(d.get("YStep", bb[3] - bb[1]))
        if not xs or not ys or len(bb) != 4:
            return "none"
        pid = self.new_id("p")
        inner: list[str] = []
        gs = _GS()
        if base is not None:
            gs.fill = gs.stroke = ("rgb", base)
        sub = _Ctx(d.get("Resources") or {}, tuple(bb), ctx.scale * scale_of(m), d.get("PaintType") == 2,
                   ctx.depth + 1)
        self.run(self.stream_data(ref), sub, gs, inner)
        self.defs.append(f'<pattern id="{pid}" patternUnits="userSpaceOnUse" x="{_fmt(bb[0], 4)}" '
                         f'y="{_fmt(bb[1], 4)}" width="{_fmt(abs(xs), 4)}" height="{_fmt(abs(ys), 4)}" '
                         f'patternTransform="{_matrix(m)}">')
        if bb[0] or bb[1]:  # (SVG draws a tile's contents from its corner)
            inner = [f'<g transform="translate({_fmt(-bb[0], 4)} {_fmt(-bb[1], 4)})">', *inner, "</g>"]
        self.defs += inner
        self.defs.append("</pattern>")
        return f"url(#{pid}) {_hex(base or self._average_of(inner))}"

    @staticmethod
    def _average_of(inner: list[str]) -> tuple:
        for line in inner:
            i = line.find('fill="#')
            if i >= 0:
                h = line[i + 7:i + 13]
                try:
                    return tuple(int(h[k:k + 2], 16) / 255 for k in (0, 2, 4))
                except ValueError:
                    break
        return (0.5, 0.5, 0.5)

    # ---- marks

    def fill(self, path, evenodd: bool, gs: _GS, ctx: _Ctx, out: list[str]) -> None:
        segs = path
        if not any(s[0] in ("l", "c") for s in segs):
            return
        if gs.fill[0] == "pattern":
            sh = self._shading_pattern(gs.fill[1])
            if sh is not None and self._needs_picture(sh):
                self._picture_fill(segs, evenodd, gs, ctx, out, sh)
                return
        places = _places(ctx.scale)
        rule = ' fill-rule="evenodd"' if evenodd else ""
        out.append(f'<path d="{path_d(segs, gs.ctm, places)}"{paint_attrs(fill=self.paint(gs.fill, IDENTITY, ctx))}'
                   f'{rule}/>')

    def stroke(self, path, gs: _GS, ctx: _Ctx, out: list[str]) -> None:
        if not path:
            return
        attrs = self.stroke_attrs(gs, ctx, gs.ctm, "none")
        places = _places(ctx.scale * scale_of(gs.ctm))
        tr = "" if gs.ctm == IDENTITY else f' transform="{_matrix(gs.ctm)}"'
        out.append(f'<path d="{path_d(path, None, places)}"{attrs}{tr}/>')

    def stroke_attrs(self, gs: _GS, ctx: _Ctx, ctm, fill: str) -> str:
        """Fill (given) and stroke attributes for a stroke under ``ctm``."""
        s = scale_of(ctm) * ctx.scale
        lw = gs.lw
        if lw <= 0:  # the thinnest line a device can draw: about a third of a point here
            lw = 0.3 / s if s else 0.3
        a = [paint_attrs(fill=fill, stroke=self.paint(gs.stroke, ctm, ctx)),
             f' stroke-width="{_fmt(lw, _places(s) + 1)}"']
        if gs.cap:
            a.append(' stroke-linecap="%s"' % ("round" if gs.cap == 1 else "square"))
        if gs.join:
            a.append(' stroke-linejoin="%s"' % ("round" if gs.join == 1 else "bevel"))
        if gs.miter != 4:
            a.append(f' stroke-miterlimit="{_fmt(max(1.0, gs.miter), 3)}"')
        if gs.dash and any(x > 0 for x in gs.dash):
            dash = [max(0.0, x) for x in gs.dash]
            if len(dash) % 2:
                dash *= 2
            a.append(f' stroke-dasharray="{" ".join(_fmt(x, 4) for x in dash)}"')
            if gs.phase:
                a.append(f' stroke-dashoffset="{_fmt(gs.phase, 4)}"')
        return "".join(a)

    def clip(self, path, evenodd: bool, gs: _GS, ctx: _Ctx, out: list[str]) -> None:
        places = _places(ctx.scale)
        segs = path if any(s[0] in ("l", "c") for s in path) else []
        rule = ' clip-rule="evenodd"' if evenodd else ""
        cid = self.new_id("c")
        d = path_d(segs, gs.ctm, places) if segs else "M0 0Z"
        self.defs.append(f'<clipPath id="{cid}"><path d="{d}"{rule}/></clipPath>')
        out.append(f'<g clip-path="url(#{cid})">')
        gs.groups += 1

    def clip_with(self, elements: list[str], out: list[str], gs: _GS) -> None:
        cid = self.new_id("c")
        self.defs.append(f'<clipPath id="{cid}">' + "".join(elements) + "</clipPath>")
        out.append(f'<g clip-path="url(#{cid})">')
        gs.groups += 1

    # ---- pictures

    def xobject(self, name, gs: _GS, ctx: _Ctx, out: list[str]) -> None:
        ref = (ctx.res.get("XObject") or {}).get(name)
        d = self.get(ref)
        if not isinstance(d, dict) or d.get("Subtype") != "Image":
            return
        color = gs.fill[1] if d.get("ImageMask") and gs.fill[0] == "rgb" else \
            (gs.fill[3] if d.get("ImageMask") and gs.fill[0] == "pattern" and gs.fill[3] else None)
        if d.get("ImageMask") and color is None:
            color = (0.0, 0.0, 0.0)
        key = (ref.num if isinstance(ref, Ref) else id(d), color)
        iid = self.image_ids.get(key)
        if iid is None:
            href = self.image_href(ref, d, color)
            if href is None:
                return
            iid = self.image_ids[key] = self.new_id("i")
            smooth = "" if d.get("Interpolate") else ' style="image-rendering:optimizeSpeed;image-rendering:pixelated"'
            self.defs.append(f'<image id="{iid}" width="1" height="1" preserveAspectRatio="none"{smooth} '
                             f'xlink:href="{href}"/>')
        m = mul((1.0, 0.0, 0.0, -1.0, 0.0, 1.0), gs.ctm)
        out.append(f'<use xlink:href="#{iid}" transform="{_matrix(m, 8)}"/>')

    def image_href(self, ref, d: dict, color) -> str | None:
        filt = d.get("Filter")
        filt = filt[-1] if isinstance(filt, list) and filt else filt
        w, h = int(d.get("Width", 0)), int(d.get("Height", 0))
        if w <= 0 or h <= 0:
            return None
        raw = self.datas.get(ref.num, b"") if isinstance(ref, Ref) else b""
        if filt == "DCTDecode":
            space = self.get(d.get("ColorSpace"))
            plain = space in ("DeviceGray", "DeviceRGB") and not d.get("Mask") and not d.get("Decode")
            if plain:
                return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
            pixels = _pil_decode(raw, "jpeg")
            if pixels is None:
                self.doc.note("A CMYK or masked JPEG picture was put in the SVG as it is (no Pillow to convert it)")
                return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
            mode, _pw, _ph, px = pixels
            samples, bpc, ncomp, space = px, 8, len(mode), {"L": "DeviceGray", "RGB": "DeviceRGB",
                                                              "CMYK": "DeviceCMYK"}.get(mode, "DeviceRGB")
            if mode == "CMYK" and d.get("Decode") and [float(x) for x in d["Decode"]][:2] == [1.0, 0.0]:
                samples = bytes(255 - b for b in samples)
            dec = None
        elif filt == "CCITTFaxDecode":
            samples = _ccitt(raw, d.get("DecodeParms") or {}, w, h)
            if samples is None:
                self.doc.note("A fax-coded picture was left out of the SVG (decoding it needs Pillow)")
                return None
            bpc, ncomp, space, dec = 1, 1, self.get(d.get("ColorSpace")) or "DeviceGray", d.get("Decode")
        else:
            samples = self.stream_data(ref) if isinstance(ref, Ref) else b""
            bpc = int(d.get("BitsPerComponent", 1 if d.get("ImageMask") else 8))
            space = self.get(d.get("ColorSpace")) or "DeviceGray"
            ncomp = 1 if d.get("ImageMask") else _ncomp(self, space)
            dec = d.get("Decode")
        if d.get("ImageMask"):
            vals = _unpack(samples, 1, w, h, 1)
            paint_on = 0 if not dec or float(dec[0]) == 0 else 1
            r, g, b = (round(c * 255) for c in color)
            px = bytearray()
            for v in vals:
                px += bytes((r, g, b, 255 if v == paint_on else 0))
            return _png_uri(Raster(w, h, "RGBA", bytes(px)))
        vals = _unpack(samples, bpc, w, h, ncomp)
        top = (1 << bpc) - 1
        decode = [float(x) for x in dec] if dec else None
        indexed = isinstance(space, list) and space and space[0] == "Indexed"
        if decode is None:
            decode = [0.0, float(top)] * ncomp if indexed else [0.0, 1.0] * ncomp
        alpha = None
        mask = d.get("Mask")
        if isinstance(mask, list):  # colour key: samples in these ranges aren't painted
            ranges = [int(x) for x in mask]
            alpha = bytearray()
            for i in range(0, len(vals), ncomp):
                hit = all(ranges[2 * c] <= vals[i + c] <= ranges[2 * c + 1] for c in range(min(ncomp, len(ranges) // 2)))
                alpha.append(0 if hit else 255)
        elif mask is not None:
            md = self.get(mask)
            if isinstance(md, dict):
                alpha = self._mask_alpha(mask, md, w, h)
        smask = d.get("SMask")
        if smask is not None and alpha is None:
            sd = self.get(smask)
            if isinstance(sd, dict):
                alpha = self._smask_alpha(smask, sd, w, h)
        cache: dict = {}
        px = bytearray()
        for i in range(0, len(vals), ncomp):
            t = tuple(vals[i:i + ncomp])
            rgb = cache.get(t)
            if rgb is None:
                comps = [decode[2 * c] + t[c] * (decode[2 * c + 1] - decode[2 * c]) / top if top else 0.0
                         for c in range(ncomp)]
                rgb = bytes(max(0, min(255, round(x * 255))) for x in self.to_rgb(space, comps))
                if len(cache) < 65536:
                    cache[t] = rgb
            px += rgb
        if alpha is not None:
            rgba = bytearray()
            for k in range(w * h):
                rgba += px[3 * k:3 * k + 3]
                rgba.append(alpha[k] if k < len(alpha) else 255)
            return _png_uri(Raster(w, h, "RGBA", bytes(rgba)))
        return _png_uri(Raster(w, h, "RGB", bytes(px)))

    def _mask_alpha(self, ref, md: dict, w: int, h: int) -> bytes:
        mw, mh = int(md.get("Width", 1)), int(md.get("Height", 1))
        bits = _unpack(self.stream_data(ref), 1, mw, mh, 1)
        dec = md.get("Decode")
        paint_on = 0 if not dec or float(dec[0]) == 0 else 1
        return _resample([255 if v == paint_on else 0 for v in bits], mw, mh, w, h)

    def _smask_alpha(self, ref, sd: dict, w: int, h: int) -> bytes:
        mw, mh = int(sd.get("Width", 1)), int(sd.get("Height", 1))
        bpc = int(sd.get("BitsPerComponent", 8))
        vals = _unpack(self.stream_data(ref), bpc, mw, mh, 1)
        top = (1 << bpc) - 1
        return _resample([v * 255 // top for v in vals], mw, mh, w, h)

    # ---- shadings

    def _shading_pattern(self, ref):
        d = self.get(ref)
        if isinstance(d, dict) and d.get("PatternType") == 2:
            return d
        return None

    def _needs_picture(self, pat: dict) -> bool:
        sh = self.get(pat.get("Shading"))
        return isinstance(sh, dict) and sh.get("ShadingType") not in (2, 3)

    def _picture_fill(self, segs, evenodd, gs: _GS, ctx: _Ctx, out: list[str], pat: dict) -> None:
        """A path painted with a mesh or function shading: the shading as a picture, clipped to the path."""
        self.clip(segs, evenodd, gs, ctx, out)
        m = tuple(float(x) for x in pat.get("Matrix", IDENTITY))
        self.shading_picture(pat.get("Shading"), m, ctx, out)
        out.append("</g>")
        gs.groups -= 1

    def shfill(self, name, gs: _GS, ctx: _Ctx, out: list[str]) -> None:
        ref = (ctx.res.get("Shading") or {}).get(name)
        sh = self.get(ref)
        if not isinstance(sh, dict):
            return
        st = sh.get("ShadingType")
        if st in (2, 3):
            region = self._region(sh, gs.ctm, ctx)
            if region is None:
                return
            fill = self.gradient_paint(ref, gs.ctm)
            out.append(f'<path d="{path_d(region, None, _places(ctx.scale))}"{paint_attrs(fill=fill)}/>')
        else:
            self.shading_picture(ref, gs.ctm, ctx, out)

    def _region(self, sh: dict, ctm, ctx: _Ctx):
        """Where an axial or radial shading paints, in this stream's space: the area, cut to the shading's
        extent where it doesn't extend."""
        x0, y0, x1, y1 = ctx.area
        bbox = sh.get("BBox")
        if bbox and len(bbox) == 4:
            b = [float(v) for v in bbox]
            pts = [apply(ctm, b[0], b[1]), apply(ctm, b[2], b[1]), apply(ctm, b[2], b[3]), apply(ctm, b[0], b[3])]
            return [("m", *pts[0]), ("l", *pts[1]), ("l", *pts[2]), ("l", *pts[3]), ("h",)]
        coords = [float(v) for v in sh.get("Coords", [])]
        ext = [bool(v) for v in sh.get("Extend", [False, False])] + [False, False]
        if sh.get("ShadingType") == 2 and len(coords) == 4 and not (ext[0] and ext[1]):
            ax, ay, bx, by = coords
            dx, dy = bx - ax, by - ay
            ln = math.hypot(dx, dy)
            if ln == 0:
                return None
            inv = invert(ctm)
            far = 0.0
            if inv is not None:
                for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
                    px, py = apply(inv, cx, cy)
                    far = max(far, math.hypot(px - ax, py - ay), math.hypot(px - bx, py - by))
            far = far * 2 + ln
            ux, uy = dx / ln, dy / ln
            nx, ny = -uy * far, ux * far
            s0 = (ax - ux * far, ay - uy * far) if ext[0] else (ax, ay)
            s1 = (bx + ux * far, by + uy * far) if ext[1] else (bx, by)
            pts = [(s0[0] + nx, s0[1] + ny), (s1[0] + nx, s1[1] + ny), (s1[0] - nx, s1[1] - ny),
                   (s0[0] - nx, s0[1] - ny)]
            pts = [apply(ctm, *p) for p in pts]
            return [("m", *pts[0]), ("l", *pts[1]), ("l", *pts[2]), ("l", *pts[3]), ("h",)]
        if sh.get("ShadingType") == 3 and len(coords) == 6 and not ext[1]:
            cx, cy, r = coords[3], coords[4], coords[5]
            if not ext[0] and coords[2] > r:
                cx, cy, r = coords[0], coords[1], coords[2]
            k = 0.5523
            circle = [("m", cx + r, cy), ("c", cx + r, cy + k * r, cx + k * r, cy + r, cx, cy + r),
                      ("c", cx - k * r, cy + r, cx - r, cy + k * r, cx - r, cy),
                      ("c", cx - r, cy - k * r, cx - k * r, cy - r, cx, cy - r),
                      ("c", cx + k * r, cy - r, cx + r, cy - k * r, cx + r, cy), ("h",)]
            return [(s[0], *[v for i in range(1, len(s), 2) for v in apply(ctm, s[i], s[i + 1])]) if s[0] != "h"
                    else s for s in circle]
        return _rect(x0, y0, x1, y1)

    def gradient_paint(self, ref, m) -> str:
        """An axial or radial shading as an SVG gradient whose space is ``m`` from the element's."""
        sh = self.get(ref)
        if not isinstance(sh, dict):
            return "none"
        st = sh.get("ShadingType")
        if st not in (2, 3):  # a mesh as a paint (a stroke, say): its average colour
            return _hex(self._mesh_average(ref, sh))
        coords = [float(v) for v in sh.get("Coords", [])]
        dom = [float(v) for v in sh.get("Domain", [0, 1])]
        fn = self.function(sh.get("Function")) if sh.get("Function") is not None else (lambda xs: xs)
        space = sh.get("ColorSpace")
        n = 17
        cols = [self.to_rgb(space, fn([dom[0] + k / (n - 1) * (dom[1] - dom[0])])) for k in range(n)]
        gid = self.new_id("s")
        if st == 2 and len(coords) == 4:
            offs = [k / (n - 1) for k in range(n)]
            head = (f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{_fmt(coords[0], 5)}" '
                    f'y1="{_fmt(coords[1], 5)}" x2="{_fmt(coords[2], 5)}" y2="{_fmt(coords[3], 5)}" '
                    f'gradientTransform="{_matrix(m)}">')
            tail = "</linearGradient>"
        elif st == 3 and len(coords) == 6:
            # SVG 1.1 has no start radius: the stops are spread from it to the end circle (and the other way round
            # when the start circle is the bigger one)
            r0, r1 = max(0.0, coords[2]), max(0.0, coords[5])
            big, small = (coords[3:6], coords[0:3]) if r1 >= r0 else (coords[0:3], coords[3:6])
            if r1 < r0:
                cols.reverse()
            rb = max(big[2], 1e-9)
            offs = [(small[2] + k / (n - 1) * (big[2] - small[2])) / rb for k in range(n)]
            head = (f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" cx="{_fmt(big[0], 5)}" '
                    f'cy="{_fmt(big[1], 5)}" r="{_fmt(rb, 5)}" fx="{_fmt(small[0], 5)}" '
                    f'fy="{_fmt(small[1], 5)}" gradientTransform="{_matrix(m)}">')
            tail = "</radialGradient>"
        else:
            return "none"
        stops = [f'<stop offset="{_fmt(max(0.0, min(1.0, o)), 4)}" stop-color="{_hex(c)}"/>' for o, c in zip(offs, cols)]
        self.defs.append(head + "".join(stops) + tail)
        avg = tuple(sum(c[i] for c in cols) / len(cols) for i in range(3))
        return f"url(#{gid}) {_hex(avg)}"

    def shading_picture(self, ref, ctm, ctx: _Ctx, out: list[str]) -> None:
        """A function-based or mesh shading drawn as a picture, placed with ``ctm`` (shading space -> stream)."""
        sh = self.get(ref)
        if not isinstance(sh, dict):
            return
        st = sh.get("ShadingType")
        if st in (2, 3):
            region = self._region(sh, ctm, ctx)
            if region is not None:
                out.append(f'<path d="{path_d(region, None, _places(ctx.scale))}"'
                           f'{paint_attrs(fill=self.gradient_paint(ref, ctm))}/>')
            return
        space = sh.get("ColorSpace")
        fn = self.function(sh.get("Function")) if sh.get("Function") is not None else None
        scale = scale_of(ctm) * ctx.scale
        if st == 1:
            dom = [float(v) for v in sh.get("Domain", [0, 1, 0, 1])]
            m = mul(tuple(float(v) for v in sh.get("Matrix", IDENTITY)), ctm)
            box = (dom[0], dom[2], dom[1], dom[3])
            size = max(abs(dom[1] - dom[0]), abs(dom[3] - dom[2])) * scale_of(m) * ctx.scale
            n = max(8, min(128, int(size)))
            px = bytearray()
            for row in range(n):
                y = dom[3] - (row + 0.5) / n * (dom[3] - dom[2])
                for col in range(n):
                    x = dom[0] + (col + 0.5) / n * (dom[1] - dom[0])
                    rgb = self.to_rgb(space, fn([x, y]) if fn else [0.0])
                    px += bytes(max(0, min(255, round(c * 255))) for c in rgb)
                    px.append(255)
            raster = Raster(n, n, "RGBA", bytes(px))
        else:
            tris = self.mesh_triangles(ref, sh)
            if not tris:
                return
            xs = [p[0] for t in tris for p in t]
            ys = [p[1] for t in tris for p in t]
            box = (min(xs), min(ys), max(xs), max(ys))
            bw, bh = box[2] - box[0], box[3] - box[1]
            if bw <= 0 or bh <= 0:
                return
            k = min(2.0 * scale, MAX_RASTER / max(bw, bh))
            pw, ph = max(2, min(MAX_RASTER, math.ceil(bw * k))), max(2, min(MAX_RASTER, math.ceil(bh * k)))
            table = None
            if fn is not None:
                table = [self.to_rgb(space, fn([i / 255])) for i in range(256)]
                tdom = [float(v) for v in sh.get("Domain", [0, 1])] if sh.get("Domain") else None
            else:
                tdom = None
            raster = _rasterize(tris, box, pw, ph, lambda c: self._mesh_rgb(c, space, table, tdom))
            m = ctm
        x0, y0, x1, y1 = box
        place = mul((x1 - x0, 0.0, 0.0, -(y1 - y0), x0, y1), m)
        out.append(f'<image width="1" height="1" preserveAspectRatio="none" transform="{_matrix(place, 8)}" '
                   f'xlink:href="{_png_uri(raster)}"/>')
        self.doc.note("Smooth shadings other than axial and radial were drawn as pictures in the SVG")

    def _mesh_rgb(self, comps, space, table, tdom):
        if table is not None:
            t = comps[0]
            if tdom:
                t = (t - tdom[0]) / (tdom[1] - tdom[0]) if tdom[1] != tdom[0] else 0.0
            return table[max(0, min(255, round(t * 255)))]
        return self.to_rgb(space, comps)

    def _mesh_average(self, ref, sh: dict) -> tuple:
        """The average colour of a function-based or mesh shading (for a stroke painted with one)."""
        space = sh.get("ColorSpace")
        fn = self.function(sh.get("Function")) if sh.get("Function") is not None else None
        if sh.get("ShadingType") == 1:
            dom = [float(v) for v in sh.get("Domain", [0, 1, 0, 1])]
            return self.to_rgb(space, fn([(dom[0] + dom[1]) / 2, (dom[2] + dom[3]) / 2]) if fn else [0.0])
        cols = [self.to_rgb(space, fn(v[2][:1]) if fn else v[2]) for t in self.mesh_triangles(ref, sh)[:500] for v in t]
        if not cols:
            return (0.5, 0.5, 0.5)
        return tuple(sum(c[i] for c in cols) / len(cols) for i in range(3))

    def mesh_triangles(self, ref, sh: dict) -> list:
        """The triangles of a mesh shading (types 4 to 7), each vertex (x, y, colour components)."""
        st = sh.get("ShadingType")
        data = self.stream_data(ref)
        bpc_xy = int(sh.get("BitsPerCoordinate", 16))
        bpc_c = int(sh.get("BitsPerComponent", 8))
        bpf = int(sh.get("BitsPerFlag", 8))
        dec = [float(v) for v in sh.get("Decode", [])]
        ncomp = 1 if sh.get("Function") is not None else _ncomp(self, sh.get("ColorSpace"))
        if len(dec) < 4 + 2 * ncomp:
            return []
        rd = _Bits(data)

        def coord(k):
            raw = rd.read(bpc_xy)
            return dec[2 * k] + raw * (dec[2 * k + 1] - dec[2 * k]) / ((1 << bpc_xy) - 1)

        def colour():
            top = (1 << bpc_c) - 1
            return [dec[4 + 2 * c] + rd.read(bpc_c) * (dec[5 + 2 * c] - dec[4 + 2 * c]) / top for c in range(ncomp)]

        def vertex():
            x = coord(0)
            y = coord(1)
            return (x, y, colour())

        tris: list = []
        try:
            if st == 4:
                va = vb = vc = None
                while rd.left() and len(tris) < MAX_TRIANGLES:
                    flag = rd.read(bpf)
                    v = vertex()
                    rd.align()
                    if flag == 0 or va is None:
                        va = v
                        rd.read(bpf)
                        vb = vertex()
                        rd.align()
                        rd.read(bpf)
                        vc = vertex()
                        rd.align()
                    elif flag == 1:
                        va, vb, vc = vb, vc, v
                    else:
                        vb, vc = vc, v
                    tris.append((va, vb, vc))
            elif st == 5:
                per = max(2, int(sh.get("VerticesPerRow", 2)))
                rows = []
                while rd.left():
                    row = []
                    for _ in range(per):
                        row.append(vertex())
                    rd.align()
                    rows.append(row)
                    if len(rows) * per > MAX_TRIANGLES:
                        break
                for r in range(len(rows) - 1):
                    for c in range(per - 1):
                        a, b, cc, d = rows[r][c], rows[r][c + 1], rows[r + 1][c], rows[r + 1][c + 1]
                        tris += [(a, b, cc), (b, d, cc)]
            elif st in (6, 7):
                full = 12 if st == 6 else 16
                prev = None
                while rd.left() and len(tris) < MAX_TRIANGLES:
                    flag = rd.read(bpf)
                    if flag == 0 or prev is None:
                        pts = [(coord(0), coord(1)) for _ in range(full)]
                        cols = [colour() for _ in range(4)]
                    else:
                        pp, pc = prev
                        edge = {1: ([3, 4, 5, 6], [1, 2]), 2: ([6, 7, 8, 9], [2, 3]), 3: ([9, 10, 11, 0], [3, 0])}
                        ip, ic = edge.get(flag, edge[1])
                        pts = [pp[i] for i in ip] + [(coord(0), coord(1)) for _ in range(full - 4)]
                        cols = [pc[i] for i in ic] + [colour() for _ in range(2)]
                    rd.align()
                    prev = (pts, cols)
                    tris += _patch_triangles(pts, cols, st == 7)
        except (IndexError, ValueError, ZeroDivisionError):
            pass
        return tris

    # ---- text

    def show(self, items, text, gs: _GS, ctx: _Ctx, out: list[str], size: float, tc: float, tw: float):
        font = self.fonts.get(self._font_ref(gs.font, ctx))
        tm = text[0]
        if font is None:
            return tm
        prog = font.prog
        two = isinstance(font, FT.CIDFont)
        mode = gs.tr
        tx = 0.0
        glyphs = []  # (code or gid, name, x in text space)
        for it in items:
            if isinstance(it, (int, float)) and not isinstance(it, bool):
                tx -= float(it) / 1000 * size
                continue
            if not isinstance(it, bytes):
                continue
            data = bytes(it)
            step = 2 if two else 1
            for i in range(0, len(data) - step + 1, step):
                code = int.from_bytes(data[i:i + step], "big")
                if two:
                    name = font.gids.get(code, "")
                    w0 = (prog.font.advance(code) / prog.font.units) if prog.font else 0.0
                else:
                    name = font.codes.get(code, ".notdef")
                    w0 = font.widths.get(code, 0.0) * prog.pdf_matrix[0]
                glyphs.append((code, name, tx))
                tx += w0 * size + tc + (tw if not two and code == 32 else 0.0)
        if glyphs and mode != 3:
            if prog.kind == "standard":
                self._text(glyphs, prog, tm, size, gs, ctx, out, mode, text)
            elif prog.kind == "type3":
                self._type3(glyphs, font, tm, size, gs, ctx, out, mode, text)
            else:
                self._outlines(glyphs, font, tm, size, gs, ctx, out, mode, text)
        return mul((1.0, 0.0, 0.0, 1.0, tx, 0.0), tm)

    @staticmethod
    def _font_ref(name, ctx: _Ctx):
        ref = (ctx.res.get("Font") or {}).get(name)
        return ref.num if isinstance(ref, Ref) else None

    def _glyph_matrix(self, font, prog, x: float, size: float, tm, ctm):
        if isinstance(font, FT.CIDFont):
            u = 1.0 / (prog.font.units if prog.font else 1000)
            gm = (u, 0.0, 0.0, u, 0.0, 0.0)
        else:
            gm = prog.pdf_matrix
        return mul(mul(mul(gm, (size, 0.0, 0.0, size, x, 0.0)), tm), ctm)

    def _outline(self, font, code: int, name: str):
        prog = font.prog
        key = (id(prog), code if isinstance(font, FT.CIDFont) else name)
        got = self.glyph_ids.get(key)
        if got is not None:
            return got
        segs: list = []
        if prog.kind == "type1":
            o = self.outlines.get(id(prog))
            if o is None:
                o = self.outlines[id(prog)] = Type1Outlines(prog.charstrings, prog.private, FT.STANDARD, prog.len_iv)
            segs = o.glyph(name)
        elif prog.kind == "cff":
            o = self.outlines.get(id(prog))
            if o is None:
                o = self.outlines[id(prog)] = Type2Outlines(prog.font, FT.STANDARD)
            segs = o.glyph(prog.gids.get(name, 0))
        elif prog.kind == "type42" and prog.font is not None:
            segs = truetype(prog.font, code)
        if not any(s[0] in ("l", "c") for s in segs):
            self.glyph_ids[key] = ""
            return ""
        gid = self.new_id("g")
        self.defs.append(f'<path id="{gid}" d="{path_d(segs, None, 2)}"/>')
        self.glyph_ids[key] = gid
        return gid

    def _outlines(self, glyphs, font, tm, size, gs: _GS, ctx: _Ctx, out, mode, text) -> None:
        uses = []
        clip_uses = []
        m0 = None
        for code, name, x in glyphs:
            gid = self._outline(font, code, name)
            if not gid:
                continue
            m = self._glyph_matrix(font, font.prog, x, size, tm, gs.ctm)
            m0 = m0 or m
            use = f'<use xlink:href="#{gid}" transform="{_matrix(m, 8)}"/>'
            uses.append(use)
            if mode >= 4:
                clip_uses.append(use)
        if not uses:
            return
        attrs = self._text_paint(mode, gs, ctx, m0)
        if attrs is not None:
            out.append(f"<g{attrs}>" + "".join(uses) + "</g>")
        text[2] += clip_uses

    def _text_paint(self, mode: int, gs: _GS, ctx: _Ctx, m) -> str | None:
        """Fill and stroke attributes for text in render mode ``mode`` whose glyphs are drawn under ``m``."""
        fill = mode in (0, 2, 4, 6)
        stroke = mode in (1, 2, 5, 6)
        if not fill and not stroke:
            return None
        fill_paint = self.paint(gs.fill, m, ctx) if fill else "none"
        if not stroke:
            return paint_attrs(fill=fill_paint)
        g = gs.copy()
        s = scale_of(m) or 1.0
        g.lw = (gs.lw if gs.lw > 0 else 0.3 / max(ctx.scale, 1e-9)) / s
        g.dash = [d / s for d in gs.dash]
        g.phase = gs.phase / s
        return self.stroke_attrs(g, _Ctx(ctx.res, ctx.area, ctx.scale * s, ctx.mask, ctx.depth), IDENTITY, fill_paint)

    def _text(self, glyphs, prog, tm, size, gs: _GS, ctx: _Ctx, out, mode, text) -> None:
        """A standard font not in the file: SVG text in a similar family, each glyph where the PDF puts it."""
        base = prog.base
        family = "Times" if base.startswith("Times") else base.split("-")[0]
        css = _FAMILIES.get(family, _FAMILIES["Helvetica"])
        bold = "Bold" in base
        italic = "Italic" in base or "Oblique" in base
        chars, xs = [], []
        for _code, name, x in glyphs:
            u = FT.to_unicode(name)
            if not u:
                continue
            chars.append(u[0])
            xs.append(x * 100 / size if size else 0.0)
        if not chars:
            return
        m = mul((size / 100, 0.0, 0.0, -size / 100, 0.0, 0.0), mul(tm, gs.ctm))
        style = (f' font-family={quoteattr(css)} font-size="100"' + (' font-weight="bold"' if bold else "")
                 + (' font-style="italic"' if italic else ""))
        el = (f'<text xml:space="preserve" transform="{_matrix(m, 8)}" x="{" ".join(_fmt(x, 2) for x in xs)}" y="0"'
              f'{style}')
        attrs = self._text_paint(mode, gs, ctx, m)
        if attrs is not None:
            out.append(el + attrs + ">" + escape("".join(chars)) + "</text>")
        if mode >= 4:
            text[2].append(el + ">" + escape("".join(chars)) + "</text>")
        self.doc.note(f"Text in {base}, a standard font not in the file, is SVG text in a similar font the reader has")

    def _type3(self, glyphs, font, tm, size, gs: _GS, ctx: _Ctx, out, mode, text) -> None:
        prog = font.prog
        if ctx.depth >= MAX_DEPTH:
            return
        bb = prog.fd.d.get("FontBBox")
        fbox = [float(v) for v in bb.list()] if hasattr(bb, "list") and bb.length == 4 else [0, 0, 1000, 1000]
        for code, name, x in glyphs:
            key = code if prog.by_char else name
            g = prog.glyphs.get(key)
            if not g or g[0] is None:
                continue
            content, _wx, _wy, box = g
            m = self._glyph_matrix(font, prog, x, size, tm, gs.ctm)
            ck = (id(prog), key, repr(gs.fill), repr(gs.stroke) if mode else None)
            gid = self.glyph_ids.get(ck)
            if gid is None:
                inner: list[str] = []
                g2 = _GS()
                g2.fill, g2.stroke = gs.fill, gs.stroke
                area = tuple(box) if box else tuple(fbox)
                sub = _Ctx(content.resources(), area, ctx.scale * scale_of(m), box is not None, ctx.depth + 1)
                self.run(content.data(), sub, g2, inner)
                gid = self.new_id("t")
                self.defs.append(f'<g id="{gid}">' + "".join(inner) + "</g>")
                self.glyph_ids[ck] = gid
            use = f'<use xlink:href="#{gid}" transform="{_matrix(m, 8)}"/>'
            if mode in (0, 1, 2, 4, 5, 6):
                out.append(use)
            if mode >= 4:
                text[2].append(use)


# ---- pictures and meshes

def _ncomp(svg: _Svg, space) -> int:
    space = svg.get(space)
    if isinstance(space, list) and space:
        k = space[0]
        if k in ("Indexed", "Separation"):
            return 1
        if k == "DeviceN":
            return len(svg.get(space[1]) or [1])
        if k == "ICCBased":
            return int((svg.get(space[1]) or {}).get("N", 3))
        if k in ("CalRGB", "Lab"):
            return 3
        if k == "CalGray":
            return 1
        if k == "Pattern":
            return 1
    return {"DeviceRGB": 3, "DeviceCMYK": 4, "RGB": 3, "CMYK": 4}.get(space, 1)


def _unpack(data: bytes, bpc: int, w: int, h: int, ncomp: int) -> list[int]:
    """Sample values row by row (rows start on a byte), short data padded with zeros."""
    row = (w * ncomp * bpc + 7) // 8
    need = row * h
    data = data[:need].ljust(need, b"\0")
    if bpc == 8:
        if row == w * ncomp:
            return list(data)
        return [v for r in range(h) for v in data[r * row:r * row + w * ncomp]]
    out = []
    n = w * ncomp
    if bpc == 16:
        for r in range(h):
            seg = data[r * row:r * row + 2 * n]
            out += [(seg[2 * i] << 8 | seg[2 * i + 1]) >> 8 for i in range(n)]
        return out
    mask = (1 << bpc) - 1
    for r in range(h):
        seg = data[r * row:(r + 1) * row]
        big = int.from_bytes(seg, "big")
        total = len(seg) * 8
        out += [(big >> (total - (i + 1) * bpc)) & mask for i in range(n)]
    return out


def _resample(vals: list, mw: int, mh: int, w: int, h: int) -> bytes:
    if (mw, mh) == (w, h):
        return bytes(vals)
    out = bytearray()
    for y in range(h):
        sy = min(mh - 1, y * mh // h)
        for x in range(w):
            out.append(vals[sy * mw + min(mw - 1, x * mw // w)])
    return bytes(out)


def _png_uri(r: Raster) -> str:
    return "data:image/png;base64," + base64.b64encode(encode_png(r)).decode("ascii")


def _pil_decode(data: bytes, kind: str):
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:  # noqa: BLE001 - a picture Pillow can't read is left out
        return None
    if im.mode not in ("L", "RGB", "CMYK"):
        im = im.convert("RGB")
    del kind
    return im.mode, im.width, im.height, im.tobytes()


def _ccitt(data: bytes, parms: dict, w: int, h: int) -> bytes | None:
    """A fax-coded picture's 1-bit rows, read by Pillow (as a one-strip TIFF), or None."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    k = int(parms.get("K", 0))
    cols = int(parms.get("Columns", w))
    rows = int(parms.get("Rows", h)) or h
    black1 = bool(parms.get("BlackIs1", False))
    comp = 4 if k < 0 else 3
    entries = [(256, 4, cols), (257, 4, rows), (258, 3, 1), (259, 3, comp), (262, 3, 1 if black1 else 0),
               (273, 4, 0), (277, 3, 1), (278, 4, rows), (279, 4, len(data))]
    if comp == 3:
        entries.append((292, 4, 1 if k > 0 else 0))
    entries.sort()
    ifd_off = 8
    data_off = ifd_off + 2 + 12 * len(entries) + 4
    ifd = struct.pack("<H", len(entries))
    for tag, typ, val in entries:
        if tag == 273:
            val = data_off
        ifd += struct.pack("<HHI", tag, typ, 1) + (struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val))
    tiff = b"II*\0" + struct.pack("<I", ifd_off) + ifd + b"\0\0\0\0" + data
    try:
        im = Image.open(io.BytesIO(tiff))
        im.load()
        im = im.convert("1")
    except Exception:  # noqa: BLE001 - a picture Pillow can't read is left out
        return None
    # PDF's samples: 0 is black unless BlackIs1 (Pillow gives 0 for black)
    raw = im.tobytes()
    return raw if not black1 else bytes(255 - b for b in raw)


class _Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0  # in bits

    def left(self) -> bool:
        return self.pos + 8 <= len(self.data) * 8

    def read(self, n: int) -> int:
        if n <= 0:
            return 0
        end = self.pos + n
        if end > len(self.data) * 8:
            raise IndexError
        start = self.pos >> 3
        stop = (end + 7) >> 3
        big = int.from_bytes(self.data[start:stop], "big")
        shift = stop * 8 - end
        self.pos = end
        return (big >> shift) & ((1 << n) - 1)

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7


def _bez(p0, p1, p2, p3, t):
    s = 1 - t
    return (s * s * s * p0[0] + 3 * s * s * t * p1[0] + 3 * s * t * t * p2[0] + t * t * t * p3[0],
            s * s * s * p0[1] + 3 * s * s * t * p1[1] + 3 * s * t * t * p2[1] + t * t * t * p3[1])


def _patch_triangles(pts: list, cols: list, tensor: bool, n: int = 10) -> list:
    """A Coons (or tensor-product) patch as a grid of triangles. The points go round the edge as in the stream:
    p00 p01 p02 p03 p13 p23 p33 p32 p31 p30 p20 p10 (then p11 p12 p22 p21 for a tensor patch)."""
    p = {}
    order = [(0, 0), (0, 1), (0, 2), (0, 3), (1, 3), (2, 3), (3, 3), (3, 2), (3, 1), (3, 0), (2, 0), (1, 0),
             (1, 1), (1, 2), (2, 2), (2, 1)]
    for (i, j), pt in zip(order, pts):
        p[i, j] = pt
    c00, c03, c33, c30 = cols  # the colours at p00, p03, p33, p30

    def surface(u, v):
        if tensor and len(pts) >= 16:
            bu = [(1 - u) ** 3, 3 * u * (1 - u) ** 2, 3 * u * u * (1 - u), u ** 3]
            bv = [(1 - v) ** 3, 3 * v * (1 - v) ** 2, 3 * v * v * (1 - v), v ** 3]
            x = sum(p[i, j][0] * bv[i] * bu[j] for i in range(4) for j in range(4))
            y = sum(p[i, j][1] * bv[i] * bu[j] for i in range(4) for j in range(4))
            return x, y
        c1 = _bez(p[0, 0], p[0, 1], p[0, 2], p[0, 3], u)
        c2 = _bez(p[3, 0], p[3, 1], p[3, 2], p[3, 3], u)
        d1 = _bez(p[0, 0], p[1, 0], p[2, 0], p[3, 0], v)
        d2 = _bez(p[0, 3], p[1, 3], p[2, 3], p[3, 3], v)
        out = []
        for k in range(2):
            sc = (1 - v) * c1[k] + v * c2[k]
            sd = (1 - u) * d1[k] + u * d2[k]
            sb = ((1 - v) * ((1 - u) * p[0, 0][k] + u * p[0, 3][k])
                  + v * ((1 - u) * p[3, 0][k] + u * p[3, 3][k]))
            out.append(sc + sd - sb)
        return out[0], out[1]

    def colour(u, v):
        return [(1 - v) * ((1 - u) * a + u * b) + v * ((1 - u) * d + u * c)
                for a, b, c, d in zip(c00, c03, c33, c30)]

    grid = [[(*surface(i / n, j / n), colour(i / n, j / n)) for i in range(n + 1)] for j in range(n + 1)]
    tris = []
    for j in range(n):
        for i in range(n):
            a, b, c, d = grid[j][i], grid[j][i + 1], grid[j + 1][i], grid[j + 1][i + 1]
            tris += [(a, b, c), (b, d, c)]
    return tris


def _rasterize(tris: list, box, pw: int, ph: int, to_rgb) -> Raster:
    """Gouraud-shaded triangles drawn into a picture of the box (row 0 at the top); uncovered pixels transparent.
    Pixels looked at are counted: a mesh covers its box about twice over, so past eight times the picture's size
    (thousands of triangles each as big as the box, from a hostile shading) the rest is left out."""
    x0, y0, x1, y1 = box
    sx, sy = pw / (x1 - x0), ph / (y1 - y0)
    comps = bytearray(pw * ph * 4)
    left = 8 * pw * ph + 4096
    for a, b, c in tris:
        pa = ((a[0] - x0) * sx, (y1 - a[1]) * sy)
        pb = ((b[0] - x0) * sx, (y1 - b[1]) * sy)
        pc = ((c[0] - x0) * sx, (y1 - c[1]) * sy)
        det = (pb[1] - pc[1]) * (pa[0] - pc[0]) + (pc[0] - pb[0]) * (pa[1] - pc[1])
        if abs(det) < 1e-12:
            continue
        ca, cb, cc = (to_rgb(v[2]) for v in (a, b, c))
        lo_x = max(0, int(min(pa[0], pb[0], pc[0])))
        hi_x = min(pw - 1, int(max(pa[0], pb[0], pc[0])))
        lo_y = max(0, int(min(pa[1], pb[1], pc[1])))
        hi_y = min(ph - 1, int(max(pa[1], pb[1], pc[1])))
        left -= max(0, hi_x - lo_x + 1) * max(0, hi_y - lo_y + 1)
        if left < 0:
            break
        eps = -1e-6
        for py in range(lo_y, hi_y + 1):
            cy = py + 0.5
            for px in range(lo_x, hi_x + 1):
                cx = px + 0.5
                wa = ((pb[1] - pc[1]) * (cx - pc[0]) + (pc[0] - pb[0]) * (cy - pc[1])) / det
                wb = ((pc[1] - pa[1]) * (cx - pc[0]) + (pa[0] - pc[0]) * (cy - pc[1])) / det
                wc = 1 - wa - wb
                if wa < eps or wb < eps or wc < eps:
                    continue
                k = (py * pw + px) * 4
                comps[k:k + 4] = bytes((max(0, min(255, round((wa * ca[0] + wb * cb[0] + wc * cc[0]) * 255))),
                                        max(0, min(255, round((wa * ca[1] + wb * cb[1] + wc * cc[1]) * 255))),
                                        max(0, min(255, round((wa * ca[2] + wb * cb[2] + wc * cc[2]) * 255))),
                                        255))
    return Raster(pw, ph, "RGBA", bytes(comps))


__all__ = ["SvgDocument", "convert"]
