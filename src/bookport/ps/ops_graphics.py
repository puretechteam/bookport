"""Graphics operators (PLRM 8.1: graphics state, coordinates, paths, painting, images, patterns, forms, the page
device) and how what they paint becomes PDF."""

from __future__ import annotations

import colorsys
import math

from .. import limits
from ..pdf.objects import Name as PdfName
from . import files as F
from .device import Content, nums
from .gstate import (
    CMYK,
    GRAY,
    IDENTITY,
    RGB,
    ColorSpace,
    GState,
    apply,
    arc_segments,
    bbox,
    dapply,
    flatten,
    invert,
    mul,
    reverse,
    transform_path,
)
from .interp import Interp, pop_bool, pop_int, pop_num, pop_proc
from .objects import GStateObj, Name, PSArray, PSDict, PSError, PSString, XName, key

OPS: dict[str, object] = {}
MAX_PATH = 5_000_000  # segments in the current path (moveto, lineto and curves alike)


def op(*names):
    def reg(fn):
        for n in names or (fn.__name__.rstrip("_"),):
            OPS[n] = fn
        return fn
    return reg


def _nums(arr) -> list:
    if type(arr) is not PSArray:
        raise PSError("typecheck")
    out = arr.list()
    for v in out:
        if type(v) is not int and type(v) is not float:
            raise PSError("typecheck")
    return [float(v) for v in out]


def _pop_matrix(I) -> tuple:
    m = I.os.pop()
    vals = _nums(m)
    if len(vals) != 6:
        raise PSError("rangecheck")
    return tuple(vals), m


def _put_matrix(m: PSArray, vals) -> PSArray:
    if type(m) is not PSArray or m.length != 6:
        raise PSError("typecheck")
    m.items[m.start:m.start + 6] = [float(v) for v in vals]
    return m


def _pop_point(I):
    y = pop_num(I)
    x = pop_num(I)
    return x, y


# ---- graphics state

@op()
def gsave(I):
    if len(I.gstack) > 10_000:
        raise PSError("limitcheck")
    I.gstack.append(I.gs.copy())


@op()
def grestore(I):
    if I.gstack:
        g = I.gstack[-1]
        if g.saved_by is not None:  # a save's gstate stays (grestore doesn't go past it)
            I.gs = g.copy()
            I.gs.saved_by = None
            return
        I.gs = I.gstack.pop()


@op()
def grestoreall(I):
    while I.gstack:
        g = I.gstack[-1]
        if g.saved_by is not None:
            I.gs = g.copy()
            I.gs.saved_by = None
            return
        I.gs = I.gstack.pop()


def init_graphics(I, gs: GState) -> None:
    gs.ctm = I.default_ctm
    gs.path, gs.cp, gs.start = [], None, None
    gs.clip = ()
    gs.space, gs.color, gs.pattern = GRAY, [0.0], None
    gs.lw, gs.cap, gs.join, gs.miter = 1.0, 0, 0, 10.0
    gs.dash, gs.dash_offset, gs.flat = [], 0.0, 1.0
    gs.stroked = None


@op()
def initgraphics(I):
    init_graphics(I, I.gs)


@op("gstate")
def gstate_(I):
    I.os.append(GStateObj(I.gs.copy()))


@op()
def currentgstate(I):
    g = I.os.pop()
    if type(g) is not GStateObj:
        raise PSError("typecheck")
    g.gs = I.gs.copy()
    I.os.append(g)


@op()
def setgstate(I):
    g = I.os.pop()
    if type(g) is not GStateObj:
        raise PSError("typecheck")
    I.gs = g.gs.copy()


@op()
def setlinewidth(I):
    I.gs.lw = abs(float(pop_num(I)))


@op()
def currentlinewidth(I):
    I.os.append(I.gs.lw)


@op()
def setlinecap(I):
    v = pop_int(I)
    if not 0 <= v <= 2:
        raise PSError("rangecheck")
    I.gs.cap = v


@op()
def currentlinecap(I):
    I.os.append(I.gs.cap)


@op()
def setlinejoin(I):
    v = pop_int(I)
    if not 0 <= v <= 2:
        raise PSError("rangecheck")
    I.gs.join = v


@op()
def currentlinejoin(I):
    I.os.append(I.gs.join)


@op()
def setmiterlimit(I):
    v = float(pop_num(I))
    if v < 1:
        raise PSError("rangecheck")
    I.gs.miter = v


@op()
def currentmiterlimit(I):
    I.os.append(I.gs.miter)


@op()
def setdash(I):
    off = float(pop_num(I))
    arr = _nums(I.os.pop())
    if arr and (any(v < 0 for v in arr) or not any(arr)):
        raise PSError("rangecheck")
    I.gs.dash, I.gs.dash_offset = arr, off


@op()
def currentdash(I):
    I.os.append(PSArray(list(I.gs.dash)))
    I.os.append(I.gs.dash_offset)


@op()
def setflat(I):
    I.gs.flat = min(100.0, max(0.2, float(pop_num(I))))


@op()
def currentflat(I):
    I.os.append(I.gs.flat)


@op()
def setstrokeadjust(I):
    I.gs.stroke_adjust = pop_bool(I)


@op()
def currentstrokeadjust(I):
    I.os.append(I.gs.stroke_adjust)


def _keep(name: str, n: int, default):
    """Settings PDF output has no use for (transfer, screens...): kept so current* gives them back."""
    def set_(I):
        vals = [I.os.pop() for _ in range(n)][::-1]
        I.gs.extra[name] = vals
    def cur(I):
        vals = I.gs.extra.get(name)
        I.os.extend(vals if vals is not None else default())
    return set_, cur


for _n, _k, _d in (("transfer", 1, lambda: [PSArray([], x=True)]),
                   ("colortransfer", 4, lambda: [PSArray([], x=True)] * 4),
                   ("blackgeneration", 1, lambda: [PSArray([], x=True)]),
                   ("undercolorremoval", 1, lambda: [PSArray([], x=True)]),
                   ("screen", 3, lambda: [60.0, 0.0, PSArray([XName("pop"), XName("pop"), 0], x=True)]),
                   ("colorscreen", 12, lambda: [60.0, 0.0, PSArray([], x=True)] * 4),
                   ("halftone", 1, lambda: [PSDict({Name("HalftoneType"): 1})]),
                   ("overprint", 1, lambda: [False]),
                   ("smoothness", 1, lambda: [0.02]),
                   ("colorrendering", 1, lambda: [PSDict({Name("ColorRenderingType"): 1})]),
                   ("overprintmode", 1, lambda: [0])):
    OPS["set" + _n], OPS["current" + _n] = _keep(_n, _k, _d)


@op()
def findcolorrendering(I):
    I.os.pop()
    I.os.append(Name("DefaultColorRendering"))
    I.os.append(False)


# ---- colour

def _space_of(I, obj) -> ColorSpace:
    """A colour space from its name or array (setcolorspace's operand)."""
    if type(obj) in (Name, XName):
        fam, parts = str(obj), [obj]
    elif type(obj) is PSArray and obj.length:
        parts = obj.list()
        fam = str(parts[0])
    else:
        raise PSError("typecheck")
    if fam == "DeviceGray" or fam == "CalGray":
        return GRAY if fam == "DeviceGray" and type(obj) is not PSArray else ColorSpace("DeviceGray", 1, obj)
    if fam in ("DeviceRGB", "CalRGB", "DevicePixel"):
        return RGB if type(obj) is not PSArray else ColorSpace("DeviceRGB", 3, obj)
    if fam == "DeviceCMYK":
        return CMYK if type(obj) is not PSArray else ColorSpace("DeviceCMYK", 4, obj)
    if fam.startswith("CIEBased"):
        n = {"CIEBasedA": 1, "CIEBasedABC": 3, "CIEBasedDEF": 3, "CIEBasedDEFG": 4}.get(fam, 3)
        I.doc.note("Calibrated (CIE) colours were drawn as plain device colours")
        cs = ColorSpace(fam, n, obj)
        return cs
    if fam == "ICCBased":
        d = parts[1] if len(parts) > 1 and type(parts[1]) is PSDict else PSDict()
        n = d.d.get("N", 3)
        cs = ColorSpace({1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(n, "DeviceRGB"), n, obj)
        return cs
    if fam == "Indexed":
        if len(parts) < 4:
            raise PSError("rangecheck")
        cs = ColorSpace("Indexed", 1, obj)
        cs.base = _space_of(I, parts[1])
        cs.hival = int(parts[2])
        look = parts[3]
        if type(look) is PSString:
            cs.lookup = look.bytes()
        elif type(look) is PSArray:
            cs.tint = look
        else:
            raise PSError("typecheck")
        return cs
    if fam in ("Separation", "DeviceN"):
        if len(parts) < 4:
            raise PSError("rangecheck")
        names = parts[1]
        n = names.length if type(names) is PSArray else 1
        cs = ColorSpace(fam, n, obj)
        cs.names = [str(x) for x in names.list()] if type(names) is PSArray else [str(names)]
        cs.base = _space_of(I, parts[2])
        cs.tint = parts[3]
        return cs
    if fam == "Pattern":
        cs = ColorSpace("Pattern", 1, obj)
        if len(parts) > 1:
            cs.base = _space_of(I, parts[1])
        return cs
    raise PSError("undefined", f"colour space {fam}")


def _set_space(I, cs: ColorSpace) -> None:
    I.gs.space = cs
    I.gs.color = cs.initial()
    I.gs.pattern = None


def _clamp(v) -> float:
    v = float(v)
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def device_color(I, cs: ColorSpace, comps: list, depth: int = 0):
    """A colour as ("g"|"rgb"|"cmyk", components), running tint transforms and lookups as it needs."""
    fam = cs.family
    if depth > 8:
        raise PSError("limitcheck")
    if fam == "DeviceGray" or fam == "CIEBasedA":
        return ("g", (_clamp(comps[0]),))
    if fam in ("DeviceRGB", "CIEBasedABC", "CIEBasedDEF"):
        return ("rgb", tuple(_clamp(c) for c in comps[:3]))
    if fam in ("DeviceCMYK", "CIEBasedDEFG"):
        return ("cmyk", tuple(_clamp(c) for c in comps[:4]))
    if fam == "Indexed":
        i = max(0, min(cs.hival, int(comps[0])))
        k = ("i", i)
        if k in cs.cache:
            return cs.cache[k]
        if cs.lookup is not None:
            n = cs.base.n
            raw = cs.lookup[i * n:(i + 1) * n]
            vals = [b / 255.0 for b in raw.ljust(n, b"\0")]
        else:
            I.os.append(i)
            I.call(cs.tint)
            vals = [float(I.os.pop()) for _ in range(cs.base.n)][::-1]
        out = device_color(I, cs.base, vals, depth + 1)
        cs.cache[k] = out
        return out
    if fam in ("Separation", "DeviceN"):
        k = tuple(round(float(c), 4) for c in comps)
        out = cs.cache.get(k)
        if out is not None:
            return out
        if cs.names == ["All"]:
            out = ("g", (1.0 - _clamp(comps[0]),))
        elif cs.names == ["None"]:
            out = None
        else:
            I.os.extend(float(c) for c in comps)
            I.call(cs.tint)
            vals = [I.os.pop() for _ in range(cs.base.n)][::-1]
            out = device_color(I, cs.base, vals, depth + 1)
        if len(cs.cache) < 100_000:
            cs.cache[k] = out
        return out
    if fam == "Pattern":
        return None
    return ("g", (0.0,))


def ink(gs: GState):
    """Where marks in the current colour go: the device, or nowhere for the colourant None (a Separation or DeviceN
    space of only None paints nothing at all, PLRM 4.8.4)."""
    cs = gs.space
    if cs.family in ("Separation", "DeviceN") and cs.names and all(n == "None" for n in cs.names):
        return None
    return gs.dev


def color_key(I, gs: GState):
    cs = gs.space
    if cs.family == "Pattern":
        pat = gs.pattern
        if pat is None:
            return ("g", (0.0,))
        ref, paint_type = pat
        if paint_type == 2 and cs.base is not None:
            base = device_color(I, cs.base, gs.color[:cs.base.n])
            return ("pattern", ref, base)
        return ("pattern", ref, None)
    return device_color(I, cs, gs.color)


def _gray_of(key) -> float:
    if key is None or key[0] == "pattern":
        return 0.0
    if key[0] == "g":
        return key[1][0]
    if key[0] == "rgb":
        r, g, b = key[1]
        return 0.3 * r + 0.59 * g + 0.11 * b
    c, m, y, k = key[1]
    return 1.0 - min(1.0, 0.3 * c + 0.59 * m + 0.11 * y + k)


def _rgb_of(key) -> tuple:
    if key is None or key[0] == "pattern":
        return (0.0, 0.0, 0.0)
    if key[0] == "g":
        return (key[1][0],) * 3
    if key[0] == "rgb":
        return key[1]
    c, m, y, k = key[1]
    return (1 - min(1, c + k), 1 - min(1, m + k), 1 - min(1, y + k))


@op()
def setgray(I):
    v = _clamp(pop_num(I))
    I.gs.space, I.gs.color, I.gs.pattern = GRAY, [v], None


@op()
def currentgray(I):
    I.os.append(_gray_of(color_key(I, I.gs)))


@op()
def setrgbcolor(I):
    b = pop_num(I)
    g = pop_num(I)
    r = pop_num(I)
    I.gs.space, I.gs.color, I.gs.pattern = RGB, [_clamp(r), _clamp(g), _clamp(b)], None


@op()
def currentrgbcolor(I):
    I.os.extend(_rgb_of(color_key(I, I.gs)))


@op()
def sethsbcolor(I):
    b = pop_num(I)
    s = pop_num(I)
    h = pop_num(I)
    I.gs.space, I.gs.color, I.gs.pattern = RGB, list(colorsys.hsv_to_rgb(_clamp(h), _clamp(s), _clamp(b))), None


@op()
def currenthsbcolor(I):
    I.os.extend(colorsys.rgb_to_hsv(*_rgb_of(color_key(I, I.gs))))


@op()
def setcmykcolor(I):
    vals = [_clamp(pop_num(I)) for _ in range(4)][::-1]
    I.gs.space, I.gs.color, I.gs.pattern = CMYK, vals, None


@op()
def currentcmykcolor(I):
    key_ = color_key(I, I.gs)
    if key_ and key_[0] == "cmyk":
        I.os.extend(key_[1])
        return
    r, g, b = _rgb_of(key_)
    c, m, y = 1 - r, 1 - g, 1 - b
    k = min(c, m, y)
    I.os.extend([c - k, m - k, y - k, k])


@op()
def setcolorspace(I):
    _set_space(I, _space_of(I, I.os.pop()))


@op()
def currentcolorspace(I):
    cs = I.gs.space
    I.os.append(cs.obj if type(cs.obj) is PSArray else PSArray([Name(cs.family)]))


@op()
def setcolor(I):
    cs = I.gs.space
    if cs.family == "Pattern":
        pat = I.os.pop()
        _use_pattern(I, pat)
        if cs.base is not None:
            I.gs.color = [pop_num(I) for _ in range(cs.base.n)][::-1]
        return
    vals = [pop_num(I) for _ in range(cs.n)][::-1]
    I.gs.color = vals


@op()
def currentcolor(I):
    I.os.extend(I.gs.color if I.gs.space.family != "Pattern" else [I.gs.extra.get("pattern_dict")])


@op()
def setpattern(I):
    pat = I.os.pop()
    if type(pat) is not PSDict:
        raise PSError("typecheck")
    paint_type = pat.d.get("PaintType", 1)
    if paint_type == 2:  # comp1 ... compn pattern setpattern (PLRM 8.2): its colour comes off the stack
        base = I.gs.space if I.gs.space.family != "Pattern" else (I.gs.space.base or GRAY)
        try:
            comps = [pop_num(I) for _ in range(base.n)][::-1]
        except (PSError, IndexError):
            I.os.append(pat)
            raise
        cs = ColorSpace("Pattern", 1, PSArray([Name("Pattern"), base.obj or Name(base.family)]))
        cs.base = base
        I.gs.space = cs
        I.gs.color = list(comps)
    else:
        I.gs.space = ColorSpace("Pattern", 1, PSArray([Name("Pattern")]))
        I.gs.color = [None]
    _use_pattern(I, pat)


def _use_pattern(I, pat) -> None:
    if type(pat) is not PSDict:
        raise PSError("typecheck")
    impl = pat.d.get("Implementation")
    if not isinstance(impl, tuple):
        raise PSError("undefined", "pattern not made with makepattern")
    I.gs.pattern = impl
    I.gs.extra["pattern_dict"] = pat


# ---- coordinates

@op()
def matrix(I):
    I.os.append(PSArray(list(IDENTITY)))


@op()
def initmatrix(I):
    I.gs.ctm = I.default_ctm


@op()
def identmatrix(I):
    I.os.append(_put_matrix(I.os.pop(), IDENTITY))


@op()
def defaultmatrix(I):
    I.os.append(_put_matrix(I.os.pop(), I.default_ctm))


@op()
def currentmatrix(I):
    I.os.append(_put_matrix(I.os.pop(), I.gs.ctm))


@op()
def setmatrix(I):
    I.gs.ctm = _pop_matrix(I)[0]


def _maybe_matrix(I):
    """The optional matrix operand of translate, scale, rotate, transform..."""
    top = I.os[-1]
    if type(top) is PSArray:
        return _pop_matrix(I)
    return None, None


@op()
def translate(I):
    _, arr = _maybe_matrix(I)
    tx, ty = _pop_point(I)
    t = (1.0, 0.0, 0.0, 1.0, float(tx), float(ty))
    if arr is not None:
        I.os.append(_put_matrix(arr, t))
    else:
        I.gs.ctm = mul(t, I.gs.ctm)


@op()
def scale(I):
    _, arr = _maybe_matrix(I)
    sx, sy = _pop_point(I)
    t = (float(sx), 0.0, 0.0, float(sy), 0.0, 0.0)
    if arr is not None:
        I.os.append(_put_matrix(arr, t))
    else:
        I.gs.ctm = mul(t, I.gs.ctm)


@op()
def rotate(I):
    _, arr = _maybe_matrix(I)
    a = math.radians(pop_num(I))
    c, s = math.cos(a), math.sin(a)
    if abs(c) < 1e-15:
        c = 0.0
    if abs(s) < 1e-15:
        s = 0.0
    t = (c, s, -s, c, 0.0, 0.0)
    if arr is not None:
        I.os.append(_put_matrix(arr, t))
    else:
        I.gs.ctm = mul(t, I.gs.ctm)


@op()
def concat(I):
    m = _pop_matrix(I)[0]
    I.gs.ctm = mul(m, I.gs.ctm)


@op()
def concatmatrix(I):
    out = I.os.pop()
    m2 = _pop_matrix(I)[0]
    m1 = _pop_matrix(I)[0]
    I.os.append(_put_matrix(out, mul(m1, m2)))


@op()
def invertmatrix(I):
    out = I.os.pop()
    m = _pop_matrix(I)[0]
    I.os.append(_put_matrix(out, invert(m)))


def _transformer(fn, inverse: bool):
    def r(I):
        m, _ = _maybe_matrix(I)
        x, y = _pop_point(I)
        m = m or I.gs.ctm
        if inverse:
            m = invert(m)
        I.os.extend(fn(m, x, y))
    return r


OPS["transform"] = _transformer(apply, False)
OPS["dtransform"] = _transformer(dapply, False)
OPS["itransform"] = _transformer(apply, True)
OPS["idtransform"] = _transformer(dapply, True)


# ---- paths

@op()
def newpath(I):
    gs = I.gs
    gs.path, gs.cp, gs.start, gs.stroked = [], None, None, None


@op()
def currentpoint(I):
    cp = I.gs.cp
    if cp is None:
        raise PSError("nocurrentpoint")
    I.os.extend(apply(invert(I.gs.ctm), *cp))


def _moveto(gs, x, y) -> None:
    if gs.path and gs.path[-1][0] == "m":
        gs.path[-1] = ("m", x, y)
    else:
        if len(gs.path) > MAX_PATH:
            raise PSError("limitcheck")
        gs.path.append(("m", x, y))
    gs.cp = gs.start = (x, y)


def _lineto(gs, x, y) -> None:
    if gs.cp is None:
        raise PSError("nocurrentpoint")
    if len(gs.path) > MAX_PATH:
        raise PSError("limitcheck")
    gs.path.append(("l", x, y))
    gs.cp = (x, y)


def _curveto(gs, x1, y1, x2, y2, x3, y3) -> None:
    if gs.cp is None:
        raise PSError("nocurrentpoint")
    if len(gs.path) > MAX_PATH:
        raise PSError("limitcheck")
    gs.path.append(("c", x1, y1, x2, y2, x3, y3))
    gs.cp = (x3, y3)


@op()
def moveto(I):
    x, y = _pop_point(I)
    _moveto(I.gs, *apply(I.gs.ctm, x, y))


@op()
def rmoveto(I):
    dx, dy = _pop_point(I)
    if I.gs.cp is None:
        raise PSError("nocurrentpoint")
    ddx, ddy = dapply(I.gs.ctm, dx, dy)
    _moveto(I.gs, I.gs.cp[0] + ddx, I.gs.cp[1] + ddy)


@op()
def lineto(I):
    x, y = _pop_point(I)
    _lineto(I.gs, *apply(I.gs.ctm, x, y))


@op()
def rlineto(I):
    dx, dy = _pop_point(I)
    if I.gs.cp is None:
        raise PSError("nocurrentpoint")
    ddx, ddy = dapply(I.gs.ctm, dx, dy)
    _lineto(I.gs, I.gs.cp[0] + ddx, I.gs.cp[1] + ddy)


@op()
def curveto(I):
    y3, x3 = pop_num(I), pop_num(I)
    y2, x2 = pop_num(I), pop_num(I)
    y1, x1 = pop_num(I), pop_num(I)
    m = I.gs.ctm
    _curveto(I.gs, *apply(m, x1, y1), *apply(m, x2, y2), *apply(m, x3, y3))


@op()
def rcurveto(I):
    y3, x3 = pop_num(I), pop_num(I)
    y2, x2 = pop_num(I), pop_num(I)
    y1, x1 = pop_num(I), pop_num(I)
    cp = I.gs.cp
    if cp is None:
        raise PSError("nocurrentpoint")
    m = I.gs.ctm
    pts = []
    for x, y in ((x1, y1), (x2, y2), (x3, y3)):
        dx, dy = dapply(m, x, y)
        pts += [cp[0] + dx, cp[1] + dy]
    _curveto(I.gs, *pts)


@op()
def closepath(I):
    gs = I.gs
    if gs.path and gs.path[-1][0] not in ("h",) and gs.start is not None:
        gs.path.append(("h",))
        gs.cp = gs.start


def _arc(I, clockwise: bool) -> None:
    a2 = pop_num(I)
    a1 = pop_num(I)
    r = pop_num(I)
    cx, cy = _pop_point(I)
    segs = arc_segments(float(cx), float(cy), float(r), float(a1), float(a2), clockwise)
    m = I.gs.ctm
    x0, y0 = apply(m, *segs[0])
    if I.gs.cp is None:
        _moveto(I.gs, x0, y0)
    else:
        _lineto(I.gs, x0, y0)
    for s in segs[1:]:
        _curveto(I.gs, *apply(m, s[0], s[1]), *apply(m, s[2], s[3]), *apply(m, s[4], s[5]))


@op()
def arc(I):
    _arc(I, False)


@op()
def arcn(I):
    _arc(I, True)


def _arct(I, push: bool) -> None:
    r = float(pop_num(I))
    x2, y2 = _pop_point(I)
    x1, y1 = _pop_point(I)
    gs = I.gs
    if gs.cp is None:
        raise PSError("nocurrentpoint")
    inv = invert(gs.ctm)
    x0, y0 = apply(inv, *gs.cp)
    v1x, v1y = x0 - x1, y0 - y1
    v2x, v2y = x2 - x1, y2 - y1
    l1, l2 = math.hypot(v1x, v1y), math.hypot(v2x, v2y)
    cross = v1x * v2y - v1y * v2x
    if l1 == 0 or l2 == 0 or abs(cross) < 1e-12 or r == 0:
        _lineto(gs, *apply(gs.ctm, x1, y1))
        if push:
            I.os.extend([float(x1), float(y1), float(x1), float(y1)])
        return
    cosang = (v1x * v2x + v1y * v2y) / (l1 * l2)
    ang = math.acos(max(-1.0, min(1.0, cosang)))
    d = r / math.tan(ang / 2)
    t1 = (x1 + v1x / l1 * d, y1 + v1y / l1 * d)
    t2 = (x1 + v2x / l2 * d, y1 + v2y / l2 * d)
    _lineto(gs, *apply(gs.ctm, *t1))
    # the arc from t1 to t2: a Bezier with the usual handle length for its sweep
    sweep = math.pi - ang
    k = 4.0 / 3.0 * math.tan(sweep / 4) * r
    c1 = (t1[0] - v1x / l1 * k, t1[1] - v1y / l1 * k)
    c2 = (t2[0] - v2x / l2 * k, t2[1] - v2y / l2 * k)
    m = gs.ctm
    _curveto(gs, *apply(m, *c1), *apply(m, *c2), *apply(m, *t2))
    if push:
        I.os.extend([t1[0], t1[1], t2[0], t2[1]])


@op()
def arct(I):
    _arct(I, False)


@op()
def arcto(I):
    _arct(I, True)


@op()
def flattenpath(I):
    I.gs.path = flatten(I.gs.path, max(0.05, I.gs.flat * 0.25))


@op()
def reversepath(I):
    I.gs.path = reverse(I.gs.path)
    if I.gs.path:
        last = [s for s in I.gs.path if s[0] != "h"]
        if last:
            I.gs.cp = (last[-1][-2], last[-1][-1])


@op()
def strokepath(I):
    """The outline of the stroke isn't computed: the path is kept and marked, so a fill of it strokes it."""
    gs = I.gs
    if gs.path:
        gs.stroked = (gs.ctm, gs.lw, gs.cap, gs.join, gs.miter, list(gs.dash), gs.dash_offset)


@op()
def clippath(I):
    gs = I.gs
    if gs.clip:
        gs.path = list(gs.clip[-1][0])
    else:
        w, h = I.page_size
        gs.path = [("m", 0.0, 0.0), ("l", w, 0.0), ("l", w, h), ("l", 0.0, h), ("h",)]
    gs.cp = gs.start = (0.0, 0.0)


@op()
def pathbbox(I):
    gs = I.gs
    if gs.cp is None:
        raise PSError("nocurrentpoint")
    b = bbox(gs.path) or (gs.cp[0], gs.cp[1], gs.cp[0], gs.cp[1])
    inv = invert(gs.ctm)
    pts = [apply(inv, b[0], b[1]), apply(inv, b[2], b[1]), apply(inv, b[0], b[3]), apply(inv, b[2], b[3])]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    I.os.extend([min(xs), min(ys), max(xs), max(ys)])


@op()
def pathforall(I):
    close = pop_proc(I)
    curve = pop_proc(I)
    line = pop_proc(I)
    move = pop_proc(I)
    inv = invert(I.gs.ctm)
    for seg in list(I.gs.path):
        if seg[0] == "m":
            I.os.extend(apply(inv, seg[1], seg[2]))
            I.call(move)
        elif seg[0] == "l":
            I.os.extend(apply(inv, seg[1], seg[2]))
            I.call(line)
        elif seg[0] == "c":
            I.os.extend([*apply(inv, seg[1], seg[2]), *apply(inv, seg[3], seg[4]), *apply(inv, seg[5], seg[6])])
            I.call(curve)
        elif seg[0] == "h":
            I.call(close)


@op()
def initclip(I):
    I.gs.clip = ()


def _clip(I, evenodd: bool) -> None:
    gs = I.gs
    path = gs.path
    if gs.stroked:
        I.doc.note("A stroke used as a clip was clipped to its path's inside instead")
    gs.clip = gs.clip + ((list(path), evenodd),)


@op()
def clip(I):
    _clip(I, False)


@op()
def eoclip(I):
    _clip(I, True)


def _rects(I):
    """rectfill/rectstroke/rectclip operands: x y w h, an array of them, or an encoded number string."""
    top = I.os[-1]
    if type(top) is PSArray:
        vals = _nums(I.os.pop())
    elif type(top) is PSString:
        raise PSError("typecheck", "encoded number strings aren't read")
    else:
        h = pop_num(I)
        w = pop_num(I)
        y = pop_num(I)
        x = pop_num(I)
        vals = [x, y, w, h]
    return [vals[i:i + 4] for i in range(0, len(vals) - 3, 4)]


def _rect_path(m, rects) -> list:
    path = []
    for x, y, w, h in rects:
        path.append(("m", *apply(m, x, y)))
        path.append(("l", *apply(m, x + w, y)))
        path.append(("l", *apply(m, x + w, y + h)))
        path.append(("l", *apply(m, x, y + h)))
        path.append(("h",))
    return path


@op()
def rectclip(I):
    gs = I.gs
    gs.clip = gs.clip + ((_rect_path(gs.ctm, _rects(I)), False),)
    gs.path, gs.cp, gs.start = [], None, None


@op()
def setbbox(I):
    del I.os[-4:]


# ---- painting

def stroke_style(gs) -> bytes:
    parts = []
    if gs.lw != 1:
        parts.append(nums(gs.lw) + b" w")
    if gs.cap:
        parts.append(b"%d J" % gs.cap)
    if gs.join:
        parts.append(b"%d j" % gs.join)
    if gs.miter != 10:
        parts.append(nums(gs.miter) + b" M")
    if gs.dash:
        parts.append(b"[" + nums(*gs.dash) + b"] " + nums(gs.dash_offset) + b" d")
    return b" ".join(parts)


def _glyphs(I, gs, path, mode: int) -> list:
    """Paint the charpath glyphs in a path as text (mode 0 fill, 1 stroke); the rest of the path is returned."""
    if not any(s[0] == "t" for s in path):
        return path
    dev = ink(gs)
    if dev is not None:
        style = b""
        if mode == 1:
            a, b, c, d = gs.ctm[:4]
            style = nums(gs.lw * abs(a * d - b * c) ** 0.5) + b" w"
        for s in path:
            if s[0] == "t":
                dev.text(s[1], s[2], gs.clip, color_key(I, gs), mode, style)
    return [s for s in path if s[0] != "t"]


def _do_stroke(I, gs, path, ctm=None, style=None) -> None:
    dev = ink(gs)
    if dev is None or not path:
        return
    path = _glyphs(I, gs, path, 1)
    if not path:
        return
    ctm = ctm or gs.ctm
    try:
        inv = invert(ctm)
    except PSError:
        return
    user = transform_path(path, inv)
    dev.stroke(user, ctm, style if style is not None else stroke_style(gs), gs.clip, color_key(I, gs))


def _do_fill(I, evenodd: bool) -> None:
    gs = I.gs
    if gs.stroked:  # strokepath then fill: the stroke it stands for
        ctm, lw, cap, join, miter, dash, off = gs.stroked
        g = gs.copy()
        g.lw, g.cap, g.join, g.miter, g.dash, g.dash_offset = lw, cap, join, miter, dash, off
        _do_stroke(I, gs, gs.path, ctm, stroke_style(g))
    elif ink(gs) is not None:
        path = _glyphs(I, gs, gs.path, 0)
        gs.dev.fill(path, evenodd, gs.clip, color_key(I, gs))
    gs.path, gs.cp, gs.start, gs.stroked = [], None, None, None


@op()
def fill(I):
    _do_fill(I, False)


@op()
def eofill(I):
    _do_fill(I, True)


@op()
def stroke(I):
    gs = I.gs
    if gs.stroked:
        _do_fill(I, False)
        return
    _do_stroke(I, gs, gs.path)
    gs.path, gs.cp, gs.start = [], None, None


@op()
def rectfill(I):
    gs = I.gs
    path = _rect_path(gs.ctm, _rects(I))
    if ink(gs) is not None:
        gs.dev.fill(path, False, gs.clip, color_key(I, gs))


@op()
def rectstroke(I):
    gs = I.gs
    m = None
    if type(I.os[-1]) is PSArray and I.os[-1].length == 6 and len(I.os) >= 2 and \
            (type(I.os[-2]) is PSArray or type(I.os[-5] if len(I.os) >= 5 else None) in (int, float)):
        m = _pop_matrix(I)[0]
    rects = _rects(I)
    ctm = mul(m, gs.ctm) if m else gs.ctm
    _do_stroke(I, gs, _rect_path(ctm, rects), ctm)


@op("infill", "ineofill", "instroke", "inufill", "inueofill", "inustroke")
def _hit(I):
    v = I.os.pop()
    if type(v) is not PSArray:
        I.os.pop()
    I.os.append(False)


@op()
def erasepage(I):
    if I.gs.dev is I.page:
        I.page.reset()


# ---- user paths (PLRM 4.6)

_UPATH_OPS = {"setbbox": 4, "moveto": 2, "rmoveto": 2, "lineto": 2, "rlineto": 2, "curveto": 6, "rcurveto": 6,
              "arc": 5, "arcn": 5, "arct": 5, "closepath": 0, "ucache": 0}


def _run_upath(I, up) -> None:
    if type(up) is not PSArray:
        raise PSError("typecheck")
    items = up.list()
    if items and type(items[0]) is PSString:
        raise PSError("typecheck", "encoded user paths aren't read")
    newpath(I)
    args = []
    for v in items:
        if type(v) in (int, float):
            args.append(v)
            continue
        name = str(v) if type(v) in (Name, XName) else getattr(v, "name", "")
        if name not in _UPATH_OPS:
            raise PSError("typecheck")
        I.os.extend(args)
        args = []
        if name not in ("ucache",):
            OPS[name](I)


@op()
def uappend(I):
    up = I.os.pop()
    keep = I.gs.path
    _run_upath(I, up)
    I.gs.path = keep + I.gs.path


def _with_upath(fn, takes_matrix: bool = False):
    """ufill, ueofill, ustroke: userpath op; ustroke alone also takes userpath matrix ustroke (PLRM 8.2)."""
    def r(I):
        m = None
        top = I.os[-1]
        if takes_matrix and len(I.os) >= 2 and type(top) is PSArray and not top.x and top.length == 6 and \
                type(I.os[-2]) is PSArray and all(type(v) in (int, float) for v in top.list()):
            m = _pop_matrix(I)[0]
        up = I.os.pop()
        gsave(I)
        _run_upath(I, up)
        if m:
            I.gs.ctm = mul(m, I.gs.ctm)
        fn(I)
        grestore(I)
    return r


OPS["ufill"] = _with_upath(lambda I: _do_fill(I, False))
OPS["ueofill"] = _with_upath(lambda I: _do_fill(I, True))
OPS["ustroke"] = _with_upath(lambda I: stroke(I), takes_matrix=True)


@op()
def ucache(I):
    pass


@op()
def upath(I):
    pop_bool(I)
    inv = invert(I.gs.ctm)
    items = []
    b = bbox(I.gs.path)
    if b:
        x0, y0 = apply(inv, b[0], b[1])
        x1, y1 = apply(inv, b[2], b[3])
        items += [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), XName("setbbox")]
    for seg in I.gs.path:
        if seg[0] == "t":  # charpath's glyphs: text, not path pieces a user path can hold
            continue
        if seg[0] == "h":
            items.append(XName("closepath"))
        else:
            for i in range(1, len(seg), 2):
                items += list(apply(inv, seg[i], seg[i + 1]))
            items.append(XName({"m": "moveto", "l": "lineto", "c": "curveto"}[seg[0]]))
    I.os.append(PSArray(items, x=True))


# ---- images (PLRM 4.10)

def _source_bytes(I, src, need: int) -> bytes:
    """``need`` bytes from a data source: a string (used over and over), a procedure or a file."""
    if type(src) is PSString:
        data = src.bytes()
        if not data:
            return b""
        full, rest = divmod(need, len(data))
        return data * full + data[:rest]
    if type(src) is PSArray:
        out = bytearray()
        while len(out) < need:
            I.call(src)
            s = I.os.pop()
            if type(s) is not PSString:
                raise PSError("typecheck")
            if not s.length:
                break
            out += s.bytes()
            if len(out) > limits.MAX_PART:
                raise limits.LimitError("A picture in this file is too big for bookport")
        return bytes(out[:need])
    if isinstance(src, F.PSFile):
        return src.stream.read(need)
    raise PSError("typecheck")


def _passthrough(src):
    """A JPEG or fax stream an image reads straight from: placed in the PDF as it is."""
    if isinstance(src, F.PSFile):
        st = src.stream
        if isinstance(st, F.DCTStream) and not st.done and not st.taken:
            return "DCTDecode", st
        from .ops_core import CCITTStream
        if isinstance(st, CCITTStream) and not st.done and not st.taken:
            return "CCITTFaxDecode", st
    return None


def _image_matrix(w: int, h: int, im, ctm):
    return mul(mul((float(w), 0.0, 0.0, -float(h), 0.0, float(h)), invert(im)), ctm)


def _unpack12(data: bytes, row: int, per: int, h: int) -> bytes:
    """12-bit samples as 8-bit ones (their top bits), row by row: each row starts on a byte, so a row of an odd
    number of samples ends half way through its last byte."""
    out = bytearray()
    for r in range(h):
        line = data[r * row:(r + 1) * row] + b"\0"
        got = bytearray()
        for i in range(0, len(line) - 2, 3):
            got.append(line[i])
            got.append(((line[i + 1] & 0x0F) << 4) | (line[i + 2] >> 4))
        if len(got) < per and len(line) - 1 >= len(got) * 3 // 2 + 1:  # the odd last sample
            got.append(line[len(got) * 3 // 2])
        out += got[:per]
    return bytes(out)


def _samples(data: bytes, bpc: int, row: int, rows: int, ncomp: int, w: int) -> list:
    """Unpacked sample values, row by row (each row starts on a byte)."""
    out = []
    per = w * ncomp
    for r in range(rows):
        line = data[r * row:(r + 1) * row]
        if bpc == 8:
            out.extend(line[:per])
            continue
        bits = int.from_bytes(line, "big") if line else 0
        total = len(line) * 8
        mask = (1 << bpc) - 1
        for i in range(per):
            shift = total - (i + 1) * bpc
            out.append((bits >> shift) & mask if shift >= 0 else 0)
    return out


def pdf_space(I, cs: ColorSpace):
    """The PDF colour space for a PostScript one; Separation and DeviceN get a sampled tint function."""
    fam = cs.family
    if fam in ("DeviceGray", "CIEBasedA"):
        return PdfName("DeviceGray")
    if fam in ("DeviceRGB", "CIEBasedABC", "CIEBasedDEF"):
        return PdfName("DeviceRGB")
    if fam in ("DeviceCMYK", "CIEBasedDEFG"):
        return PdfName("DeviceCMYK")
    if fam == "Indexed":
        base = cs.base
        if base.family in ("DeviceGray", "DeviceRGB", "DeviceCMYK") and cs.lookup is not None:
            table = cs.lookup[:(cs.hival + 1) * base.n].ljust((cs.hival + 1) * base.n, b"\0")
            return [PdfName("Indexed"), PdfName(base.family), cs.hival, table]
        keys = [device_color(I, cs, [i]) for i in range(cs.hival + 1)]
        fam_out = _common_family(keys)
        table = bytearray()
        for k in keys:
            table += bytes(round(c * 255) for c in _as_family(k, fam_out))
        return [PdfName("Indexed"), PdfName(fam_out), cs.hival, bytes(table)]
    if fam in ("Separation", "DeviceN"):
        n = cs.n
        size = 256 if n == 1 else max(2, round(4096 ** (1.0 / n)))
        if n > 4:
            size = 2
        grid = []
        _grid(n, size, [], grid)
        keys = [device_color(I, cs, pt) for pt in grid]
        fam_out = _common_family(keys)
        out_n = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}[fam_out]
        samples = bytearray()
        for k in keys:
            samples += bytes(round(c * 255) for c in _as_family(k, fam_out))
        fn = I.doc.w.stream(bytes(samples), {"FunctionType": 0, "Domain": [0, 1] * n, "Range": [0, 1] * out_n,
                                             "Size": [size] * n, "BitsPerSample": 8})
        names = [PdfName(x) for x in cs.names]
        if fam == "Separation":
            return [PdfName("Separation"), names[0], PdfName(fam_out), fn]
        return [PdfName("DeviceN"), names, PdfName(fam_out), fn]
    raise PSError("typecheck", f"{fam} can't be used here")


def _grid(n, size, prefix, out) -> None:
    """Sample points in PDF sampled-function order (the first input varies fastest)."""
    total = size ** n
    for idx in range(total):
        pt, k = [], idx
        for _ in range(n):
            pt.append((k % size) / (size - 1))
            k //= size
        out.append(pt)


def _common_family(keys) -> str:
    fams = {k[0] for k in keys if k is not None}
    if fams <= {"g"}:
        return "DeviceGray"
    if "cmyk" in fams and not ({"rgb"} & fams):
        return "DeviceCMYK"
    return "DeviceRGB"


def _as_family(k, fam: str) -> tuple:
    if fam == "DeviceGray":
        return (_gray_of(k),)
    if fam == "DeviceRGB":
        return _rgb_of(k)
    if k is not None and k[0] == "cmyk":
        return k[1]
    g = _gray_of(k)
    return (0.0, 0.0, 0.0, 1.0 - g)


def _image_dict(d: PSDict) -> dict:
    return {str(k): v for k, v in d.d.items()}


def _decode_list(v, n: int, bpc: int, indexed: bool) -> list | None:
    if v is None:
        return None
    vals = _nums(v)
    default = [0.0, float((1 << bpc) - 1)] * n if indexed else [0.0, 1.0] * n
    return None if vals == default else vals


def _read_planes(I, sources, w, h, bpc, ncomp, multi: bool):
    row_bits = w * bpc * (1 if multi else ncomp)
    row = (row_bits + 7) // 8
    need = row * h
    limits.check_pixels(w, h)
    limits.charge(need * (len(sources) if multi else 1))  # what the picture unpacks to, whatever its source's size
    if multi:
        planes = [_source_bytes(I, s, need) for s in sources]
        planes = [p.ljust(need, b"\0") for p in planes]
        return planes, row
    data = _source_bytes(I, sources[0], need)
    return [data.ljust(need, b"\0")], row


def _interleave(planes, w, h, bpc, row) -> tuple[bytes, int]:
    n = len(planes)
    if n == 1:
        return planes[0], row
    if bpc == 8:
        out = bytearray(w * h * n)
        for c, p in enumerate(planes):
            for r in range(h):
                out[r * w * n + c:(r + 1) * w * n:n] = p[r * row:r * row + w]
        return bytes(out), w * n
    vals = [_samples(p, bpc, row, h, 1, w) for p in planes]
    out = bytearray()
    for i in range(w * h):
        for c in range(n):
            out.append(vals[c][i] * 255 // ((1 << bpc) - 1))
    return bytes(out), w * n


def place_image(I, w: int, h: int, bpc: int, cs: ColorSpace | None, decode, interpolate: bool, im, sources,
                multi: bool, mask_polarity=None, mask_ref=None, color_key_mask=None) -> None:
    gs = I.gs
    # a mask paints in the current colour; a picture in its own space (nothing for the colourant None).
    # Its data is read either way, so the program goes on where it should.
    dev = ink(gs) if cs is None else (None if cs.family in ("Separation", "DeviceN") and cs.names
                                      and all(n == "None" for n in cs.names) else gs.dev)
    if w <= 0 or h <= 0:
        return
    if bpc not in (1, 2, 4, 8, 12):
        raise PSError("rangecheck")
    ncomp = 1 if cs is None else cs.n
    d = {"Width": w, "Height": h}
    if interpolate:
        d["Interpolate"] = True
    jpeg = _passthrough(sources[0]) if len(sources) == 1 else None
    if jpeg is not None:
        kind, st = jpeg
        st.taken = True
        data = st.grab()
        st.done = True
        d["Filter"] = PdfName(kind)
        if kind == "CCITTFaxDecode":
            parms = {k: v for k, v in st.parms.items() if type(v) in (int, bool, float)}
            parms.setdefault("Columns", w)
            parms.setdefault("Rows", h)
            d["DecodeParms"] = parms
        compress = False
    else:
        planes, row = _read_planes(I, sources, w, h, bpc, ncomp, multi)
        data, row = _interleave(planes, w, h, bpc, row) if multi and ncomp > 1 else (planes[0], row)
        if multi and ncomp > 1:
            bpc = 8 if bpc != 8 and len(planes) > 1 else bpc
        if bpc == 12:
            data, bpc = _unpack12(data, row, w * ncomp, h), 8
        compress = True
    if cs is None:  # an image mask, or a mask for another image
        d["ImageMask"] = True
        d["BitsPerComponent"] = 1
        if mask_polarity:
            d["Decode"] = [1, 0]
    else:
        conv = cs.family in ("Separation", "DeviceN") or (cs.family == "Indexed" and cs.lookup is None) or \
            (cs.family == "Indexed" and cs.base.family not in ("DeviceGray", "DeviceRGB", "DeviceCMYK"))
        if conv and jpeg is None:
            data, out_fam = _convert_samples(I, cs, data, bpc, w, h, decode)
            d["ColorSpace"] = PdfName(out_fam)
            d["BitsPerComponent"] = 8
            decode = None
        else:
            d["ColorSpace"] = pdf_space(I, cs)
            d["BitsPerComponent"] = bpc if jpeg is None or kind != "DCTDecode" else 8
        dec = _decode_list(decode, ncomp, bpc, cs.family == "Indexed")
        if dec is not None:
            d["Decode"] = dec
        if mask_ref is not None:
            d["Mask"] = mask_ref
        elif color_key_mask is not None:
            d["Mask"] = color_key_mask
    if dev is None:
        return
    ref = I.doc.image(data, d, compress)
    m = _image_matrix(w, h, im, gs.ctm)
    dev.image(ref, m, gs.clip, color_key(I, gs) if cs is None else None)


def _convert_samples(I, cs, data, bpc, w, h, decode) -> tuple[bytes, str]:
    """Separation, DeviceN and computed Indexed pictures turned into device colours, a pixel value at a time."""
    n = cs.n
    row = (w * n * bpc + 7) // 8
    vals = _samples(data, bpc, row, h, n, w)
    top = (1 << bpc) - 1
    dec = _nums(decode) if decode is not None else ([0.0, float(top)] if cs.family == "Indexed" else [0.0, 1.0] * n)
    # each distinct sample value (one number, or n of them) turned into a colour, and into its bytes, once
    pixels = vals if n == 1 else [tuple(vals[i:i + n]) for i in range(0, len(vals), n)]
    cache: dict = {}
    for t in set(pixels):
        tt = (t,) if n == 1 else t
        cache[t] = device_color(I, cs, [dec[2 * c] + tt[c] * (dec[2 * c + 1] - dec[2 * c]) / top for c in range(n)])
    fam = _common_family(set(cache.values()))
    out_of = {t: bytes(round(c * 255) for c in _as_family(k, fam)) for t, k in cache.items()}
    return b"".join([out_of[t] for t in pixels]), fam


def _image_args_l1(I, ncomp_from_stack: bool):
    """width height bits matrix source(s) [multi ncomp] (Level 1 image and colorimage)."""
    if ncomp_from_stack:
        ncomp = pop_int(I)
        multi = pop_bool(I)
        srcs = [I.os.pop() for _ in range(ncomp if multi else 1)][::-1]
    else:
        ncomp, multi, srcs = 1, False, [I.os.pop()]
    im = _pop_matrix(I)[0]
    bpc = pop_int(I)
    h = pop_int(I)
    w = pop_int(I)
    return w, h, bpc, im, srcs, ncomp, multi


@op()
def image(I):
    if type(I.os[-1]) is PSDict:
        d = _image_dict(I.os.pop())
        t = d.get("ImageType", 1)
        if t == 3:
            _masked_image(I, d)
            return
        w, h = int(d["Width"]), int(d["Height"])
        bpc = int(d.get("BitsPerComponent", 8))
        im = tuple(_nums(d["ImageMatrix"]))
        multi = bool(d.get("MultipleDataSources", False))
        src = d["DataSource"]
        srcs = src.list() if multi and type(src) is PSArray else [src]
        ck = None
        if t == 4:
            mc = [int(v) for v in d["MaskColor"].list()]
            ck = mc if len(mc) == 2 * I.gs.space.n else [v for x in mc for v in (x, x)]
        place_image(I, w, h, bpc, I.gs.space, d.get("Decode"), bool(d.get("Interpolate")), im, srcs, multi,
                    color_key_mask=ck)
        return
    w, h, bpc, im, srcs, _, _ = _image_args_l1(I, False)
    place_image(I, w, h, bpc, GRAY, None, False, im, srcs, False)


@op()
def colorimage(I):
    w, h, bpc, im, srcs, ncomp, multi = _image_args_l1(I, True)
    cs = {1: GRAY, 3: RGB, 4: CMYK}.get(ncomp)
    if cs is None:
        raise PSError("rangecheck")
    place_image(I, w, h, bpc, cs, None, False, im, srcs, multi)


@op()
def imagemask(I):
    if type(I.os[-1]) is PSDict:
        d = _image_dict(I.os.pop())
        dec = d.get("Decode")
        polarity = dec is not None and _nums(dec)[:2] == [1.0, 0.0]
        place_image(I, int(d["Width"]), int(d["Height"]), 1, None, None, bool(d.get("Interpolate")),
                    tuple(_nums(d["ImageMatrix"])), [d["DataSource"]], False, mask_polarity=polarity)
        return
    src = I.os.pop()
    im = _pop_matrix(I)[0]
    polarity = pop_bool(I)
    h = pop_int(I)
    w = pop_int(I)
    place_image(I, w, h, 1, None, None, False, im, [src], False, mask_polarity=polarity)


def _masked_image(I, d: dict) -> None:
    """ImageType 3: a picture and a stencil saying where it's painted (InterleaveType 3, separate sources)."""
    data_d = _image_dict(d["DataDict"])
    mask_d = _image_dict(d["MaskDict"])
    inter = int(d.get("InterleaveType", 3))
    mw, mh = int(mask_d["Width"]), int(mask_d["Height"])
    w, h = int(data_d["Width"]), int(data_d["Height"])
    bpc = int(data_d.get("BitsPerComponent", 8))
    cs = I.gs.space
    if inter == 1:  # the mask is an extra first component of every pixel
        src = data_d["DataSource"]
        need = (w * (cs.n + 1) * bpc + 7) // 8 * h
        raw = _source_bytes(I, src, need).ljust(need, b"\0")
        vals = _samples(raw, bpc, (w * (cs.n + 1) * bpc + 7) // 8, h, cs.n + 1, w)
        top = (1 << bpc) - 1
        indexed = cs.family == "Indexed"  # indexes stay indexes (scaling them to 0..255 would pick other colours)
        mask_bits, colour = bytearray(), bytearray()
        for i in range(0, len(vals), cs.n + 1):
            mask_bits.append(vals[i])
            colour += bytes(min(255, v) if indexed else v * 255 // top for v in vals[i + 1:i + 1 + cs.n])
        decode = None  # the data's own Decode, for its samples as they now are (8 bits)
        ddec = data_d.get("Decode")
        if ddec is not None:
            dv = _nums(ddec)
            if indexed and len(dv) >= 2:
                dv = [dv[0], dv[0] + (dv[1] - dv[0]) * 255 / top]
            decode = PSArray([float(v) for v in dv])
        packed = _pack_bits([1 if v else 0 for v in mask_bits], w, h)
        dec = mask_d.get("Decode")
        md = {"ImageMask": True, "Width": w, "Height": h, "BitsPerComponent": 1}
        if dec is not None:
            md["Decode"] = [int(v) for v in _nums(dec)]
        mref = I.doc.image(packed, md)
        place_image(I, w, h, 8, cs, decode, bool(data_d.get("Interpolate")), tuple(_nums(data_d["ImageMatrix"])),
                    [PSString.of(bytes(colour))], False, mask_ref=mref)
        return
    if inter == 2:  # mask rows come before the data rows they cover
        src = data_d["DataSource"]
        mrow, drow = (mw + 7) // 8, (w * cs.n * bpc + 7) // 8
        # in blocks: a taller mask has mh/h of its rows before each data row, a shorter one each of its rows
        # before h/mh data rows
        if mh >= h:
            blocks, mper, dper = h, max(1, mh // h), 1
        else:
            blocks, mper, dper = mh, 1, max(1, h // max(1, mh))
        mask_data, data = bytearray(), bytearray()
        for _ in range(blocks):
            mask_data += _source_bytes(I, src, mrow * mper)
            data += _source_bytes(I, src, drow * dper)
        msrc, dsrc = PSString.of(bytes(mask_data)), PSString.of(bytes(data))
    else:
        msrc, dsrc = mask_d["DataSource"], data_d["DataSource"]
    planes, _ = _read_planes(I, [msrc], mw, mh, 1, 1, False)
    md = {"ImageMask": True, "Width": mw, "Height": mh, "BitsPerComponent": 1}
    dec = mask_d.get("Decode")
    if dec is not None:
        md["Decode"] = [int(v) for v in _nums(dec)]
    mref = I.doc.image(planes[0], md)
    place_image(I, w, h, bpc, cs, data_d.get("Decode"), bool(data_d.get("Interpolate")),
                tuple(_nums(data_d["ImageMatrix"])), [dsrc], False, mask_ref=mref)


def _pack_bits(bits: list, w: int, h: int) -> bytes:
    out = bytearray()
    for r in range(h):
        row = bits[r * w:(r + 1) * w]
        v = 0
        for i, b in enumerate(row):
            v = (v << 1) | b
            if i % 8 == 7:
                out.append(v)
                v = 0
        if w % 8:
            out.append(v << (8 - w % 8))
    return bytes(out)


# ---- shadings and patterns (PLRM 4.9)

def pdf_value(I, v, depth: int = 0):
    """A PostScript value as a PDF one (for shading and function dictionaries)."""
    t = type(v)
    if depth > 16:
        raise PSError("limitcheck")
    if t in (int, float, bool) or v is None:
        return v
    if t is Name or t is XName:
        return PdfName(v)
    if t is PSString:
        return v.bytes()
    if t is PSArray:
        return [pdf_value(I, x, depth + 1) for x in v.list()]
    if t is PSDict:
        if "FunctionType" in v.d:
            return pdf_function(I, v)
        return {str(k): pdf_value(I, x, depth + 1) for k, x in v.d.items()}
    return None


def pdf_function(I, f):
    if type(f) is PSArray and f.x:  # a procedure where a function goes: sampled
        return _sample_proc(I, f, 1, None)
    if type(f) is PSArray:
        return [pdf_function(I, x) for x in f.list()]
    d = {str(k): v for k, v in f.d.items()}
    ft = d.get("FunctionType")
    if ft == 0:
        src = d.get("DataSource")
        if isinstance(src, F.PSFile):
            data = src.stream.rest()
        elif type(src) is PSString:
            data = src.bytes()
        else:
            raise PSError("typecheck")
        extra = {k: pdf_value(I, v) for k, v in d.items() if k != "DataSource"}
        return I.doc.w.stream(data, extra)
    if ft in (2, 3):
        out = {}
        for k, v in d.items():
            out[k] = [pdf_function(I, x) for x in v.list()] if k == "Functions" else pdf_value(I, v)
        return out
    raise PSError("rangecheck", f"function type {ft}")


def _sample_proc(I, proc, n_in: int, n_out: int | None):
    samples = []
    size = 256
    vals = []
    for i in range(size):
        before = len(I.os)
        I.os.append(i / (size - 1))
        I.call(proc)
        got = I.os[before:]
        del I.os[before:]
        vals.append([_clamp(x) for x in got])
    n_out = len(vals[0]) if vals else 1
    for v in vals:
        samples += [round(x * 255) for x in v[:n_out]]
    return I.doc.w.stream(bytes(samples), {"FunctionType": 0, "Domain": [0, 1], "Range": [0, 1] * n_out,
                                           "Size": [size], "BitsPerSample": 8})


def pdf_shading(I, sh: PSDict):
    d = {str(k): v for k, v in sh.d.items()}
    st = d.get("ShadingType")
    if st not in range(1, 8):
        raise PSError("rangecheck", "shading type")
    cs = _space_of(I, d.get("ColorSpace", Name("DeviceGray")))
    out = {"ShadingType": st, "ColorSpace": pdf_space(I, cs)}
    for k in ("Background", "BBox", "AntiAlias", "Coords", "Domain", "Matrix", "Extend", "BitsPerCoordinate",
              "BitsPerComponent", "BitsPerFlag", "Decode", "VerticesPerRow"):
        if k in d:
            out[k] = pdf_value(I, d[k])
    if "Function" in d:
        out["Function"] = pdf_function(I, d["Function"])
    if st >= 4:
        src = d.get("DataSource")
        if type(src) is PSArray and not src.x:
            data, extra = _mesh_from_numbers(st, _nums(src), cs.n if "Function" not in d else 1,
                                             int(d.get("VerticesPerRow", 2)))
            out.update(extra)
        elif isinstance(src, F.PSFile):
            data = src.stream.rest()
        elif type(src) is PSString:
            data = src.bytes()
        else:
            raise PSError("typecheck")
        return I.doc.w.stream(data, out)
    return I.doc.w.add(out)


def _mesh_from_numbers(st: int, vals: list, ncomp: int, per_row: int):
    """A mesh given as an array of numbers, packed as a PDF mesh stream (32-bit coordinates, 16-bit colours)."""
    records = []  # (flag or None, [(x, y)...], [colour...])
    i = 0
    n = len(vals)
    while i < n:
        if st in (4, 6, 7):
            flag = int(vals[i])
            i += 1
        else:
            flag = None
        if st in (4, 5):
            npts, ncol = 1, 1
        else:
            full = 12 if st == 6 else 16
            npts = full if flag == 0 else full - 4
            ncol = 4 if flag == 0 else 2
        pts = [(vals[i + 2 * k], vals[i + 2 * k + 1]) for k in range(npts)]
        i += 2 * npts
        cols = [vals[i + c * ncomp:i + (c + 1) * ncomp] for c in range(ncol)]
        i += ncol * ncomp
        if i > n:
            break
        records.append((flag, pts, cols))
    xs = [p[0] for r in records for p in r[1]] or [0.0]
    ys = [p[1] for r in records for p in r[1]] or [0.0]
    cmins = [min(c[k] for r in records for c in r[2]) for k in range(ncomp)] if records else [0.0] * ncomp
    cmaxs = [max(c[k] for r in records for c in r[2]) for k in range(ncomp)] if records else [1.0] * ncomp
    x0, x1 = min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1
    y0, y1 = min(ys), max(ys) if max(ys) > min(ys) else min(ys) + 1
    dec = [x0, x1, y0, y1]
    for lo, hi in zip(cmins, cmaxs):
        dec += [lo, hi if hi > lo else lo + 1]
    out = bytearray()
    for flag, pts, cols in records:
        if flag is not None:
            out.append(flag & 3)
        for x, y in pts:
            out += int((x - x0) / (x1 - x0) * 0xFFFFFFFF).to_bytes(4, "big")
            out += int((y - y0) / (y1 - y0) * 0xFFFFFFFF).to_bytes(4, "big")
        for c in cols:
            for k in range(ncomp):
                lo, hi = dec[4 + 2 * k], dec[5 + 2 * k]
                out += int((c[k] - lo) / (hi - lo) * 0xFFFF).to_bytes(2, "big")
    extra = {"BitsPerCoordinate": 32, "BitsPerComponent": 16, "Decode": dec}
    if st in (4, 6, 7):
        extra["BitsPerFlag"] = 8
    if st == 5:
        extra["VerticesPerRow"] = per_row
    return bytes(out), extra


@op()
def shfill(I):
    sh = I.os.pop()
    if type(sh) is not PSDict:
        raise PSError("typecheck")
    gs = I.gs
    if gs.dev is None:
        return
    ref = pdf_shading(I, sh)
    gs.dev.shading(ref, gs.ctm, gs.clip)


@op()
def makepattern(I):
    m = _pop_matrix(I)[0]
    pat = I.os.pop()
    if type(pat) is not PSDict:
        raise PSError("typecheck")
    d = {str(k): v for k, v in pat.d.items()}
    pt = d.get("PatternType", 1)
    full = mul(m, I.gs.ctm)
    w = I.doc.w
    if pt == 2:
        ref = w.add({"PatternType": 2, "Shading": pdf_shading(I, d["Shading"]), "Matrix": list(full)})
        paint_type = 1
    else:
        paint_type = int(d.get("PaintType", 1))
        cell = Content(I.doc, mask=(paint_type == 2))
        saved = I.gs
        g = I.gs.copy()
        init_graphics(I, g)
        g.ctm = IDENTITY
        g.dev = cell
        g.font = saved.font
        I.gstack.append(saved)
        I.gs = g
        copy_ = PSDict(dict(pat.d), pat.maxlength)
        try:
            I.os.append(copy_)
            I.call(d["PaintProc"])
        finally:
            I.gs = saved
            if I.gstack and I.gstack[-1] is saved:
                I.gstack.pop()
        bb = _nums(d["BBox"])
        ref = w.stream(cell.data(), {"Type": PdfName("Pattern"), "PatternType": 1, "PaintType": paint_type,
                                     "TilingType": int(d.get("TilingType", 1)), "BBox": bb,
                                     "XStep": float(d.get("XStep", bb[2] - bb[0])),
                                     "YStep": float(d.get("YStep", bb[3] - bb[1])),
                                     "Matrix": list(full), "Resources": cell.resources()})
    out = PSDict(dict(pat.d), pat.maxlength)
    out.d[Name("Implementation")] = (ref, paint_type)
    out.d[Name("Matrix")] = PSArray(list(full))
    I.os.append(out)


@op()
def execform(I):
    form = I.os.pop()
    if type(form) is not PSDict:
        raise PSError("typecheck")
    d = {str(k): v for k, v in form.d.items()}
    gsave(I)
    try:
        if "Matrix" in d:
            I.gs.ctm = mul(tuple(_nums(d["Matrix"])), I.gs.ctm)
        if "BBox" in d:
            x0, y0, x1, y1 = _nums(d["BBox"])
            I.gs.clip = I.gs.clip + ((_rect_path(I.gs.ctm, [[x0, y0, x1 - x0, y1 - y0]]), False),)
        I.gs.path, I.gs.cp = [], None
        I.os.append(form)
        I.call(d["PaintProc"])
    finally:
        grestore(I)


# ---- the page device

def _page_size(I, d: dict):
    ps = d.get("PageSize")
    if type(ps) is PSArray and ps.length >= 2:
        w, h = (float(v) for v in ps.list()[:2])
        if w > 0 and h > 0:
            return w, h
    return None


@op()
def setpagedevice(I):
    d = I.os.pop()
    if type(d) is not PSDict:
        raise PSError("typecheck")
    for k, v in d.d.items():
        I.pagedevice.d[key(k)] = v
    size = _page_size(I, {str(k): v for k, v in d.d.items()})
    if size and not I.eps:
        I.page_size = size
    init_graphics(I, I.gs)


@op()
def currentpagedevice(I):
    d = PSDict(dict(I.pagedevice.d))
    d.d[Name("PageSize")] = PSArray([I.page_size[0], I.page_size[1]])
    I.os.append(d)


def _page_proc(I, name: str):
    p = I.pagedevice.d.get(name)
    return p if type(p) is PSArray and p.x else None


@op()
def showpage(I):
    end = _page_proc(I, "EndPage")
    emit = True
    if end is not None:
        I.os.extend([I.page_count, 0])
        I.call(end)
        emit = bool(I.os.pop())
    if emit:
        I.emit_page()
    I.page.reset()
    init_graphics(I, I.gs)
    begin = _page_proc(I, "BeginPage")
    if begin is not None:
        I.os.append(I.page_count)
        I.call(begin)


@op()
def copypage(I):
    I.emit_page(keep=True)


@op()
def nulldevice(I):
    I.gs.dev = None
    I.gs.ctm = IDENTITY
    I.gs.clip = ()


def _paper(w, h):
    def r(I):
        if not I.eps:
            I.page_size = (float(w), float(h))
    return r


PAPERS = {"letter": (612, 792), "lettersmall": (612, 792), "legal": (612, 1008), "a4": (595, 842),
          "a4small": (595, 842), "a3": (842, 1191), "a5": (420, 595), "b5": (516, 729), "note": (612, 792),
          "11x17": (792, 1224), "ledger": (1224, 792), "tabloid": (792, 1224)}


@op()
def setpageparams(I):
    pop_int(I)
    pop_num(I)
    h = pop_num(I)
    w = pop_num(I)
    if not I.eps:
        I.page_size = (float(w), float(h))


def register(I: Interp) -> None:
    for name, fn in OPS.items():
        if name.endswith("_ignore"):
            continue
        I.define(name, fn)
    for name, (w, h) in PAPERS.items():
        from .objects import Operator
        o = Operator(name, _paper(w, h))
        I.userdict.d[Name(name)] = o
        I.statusdict.d[Name(name)] = o
    I.statusdict.d[Name("setpageparams")] = I.systemdict.d.pop(Name("setpageparams"))
    for n, v in (("product", PSString.of(b"bookport")), ("revision", 1)):
        I.statusdict.d[Name(n)] = v
