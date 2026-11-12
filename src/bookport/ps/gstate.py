"""The graphics state (PLRM 4.3), matrices and paths. Paths are kept in device space (the PDF page's default space,
in points), as PostScript does, so a path built under one CTM and painted under another comes out right."""

from __future__ import annotations

import math

from .objects import PSError

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(m, n):
    """m then n (row vectors: p * m * n)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def invert(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-300:
        raise PSError("undefinedresult", "the matrix can't be inverted")
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def dapply(m, x, y):
    return (m[0] * x + m[2] * y, m[1] * x + m[3] * y)


# ---- colour spaces

class ColorSpace:
    """A colour space: its family, how many components, the PostScript array it came from, and for Indexed,
    Separation, DeviceN and Pattern what they lean on."""
    __slots__ = ("base", "cache", "family", "hival", "lookup", "n", "names", "obj", "tint")

    def __init__(self, family: str, n: int, obj=None):
        self.family, self.n, self.obj = family, n, obj
        self.base = None
        self.hival = 0
        self.lookup = None
        self.tint = None
        self.names = None
        self.cache: dict = {}

    def initial(self) -> list:
        if self.family == "DeviceCMYK":
            return [0.0, 0.0, 0.0, 1.0]
        if self.family in ("Separation", "DeviceN"):
            return [1.0] * self.n
        if self.family == "Indexed":
            return [0]
        if self.family == "Pattern":
            return [None]
        if self.family.startswith("CIEBased"):
            return [0.0] * self.n
        return [0.0] * self.n


GRAY = ColorSpace("DeviceGray", 1)
RGB = ColorSpace("DeviceRGB", 3)
CMYK = ColorSpace("DeviceCMYK", 4)


class GState:
    __slots__ = (
        "cap",
        "char_path",
        "clip",
        "color",
        "cp",
        "ctm",
        "dash",
        "dash_offset",
        "dev",
        "extra",
        "flat",
        "font",
        "join",
        "lw",
        "miter",
        "path",
        "pattern",
        "saved_by",
        "space",
        "start",
        "stroke_adjust",
        "stroked",
    )

    def __init__(self, ctm=IDENTITY):
        self.ctm = ctm
        self.path: list = []
        self.cp = None
        self.start = None
        self.clip: tuple = ()
        self.space = GRAY
        self.color = [0.0]
        self.pattern = None
        self.lw = 1.0
        self.cap = 0
        self.join = 0
        self.miter = 10.0
        self.dash: list = []
        self.dash_offset = 0.0
        self.flat = 1.0
        self.font = None
        self.stroke_adjust = False
        self.extra: dict = {}
        self.saved_by = None
        self.char_path = False
        self.stroked = None  # strokepath: the stroke settings the path stands for
        self.dev = None  # where painting goes: the page, a glyph or pattern being captured, or nowhere (nulldevice)

    def copy(self) -> GState:
        g = GState.__new__(GState)
        for s in GState.__slots__:
            setattr(g, s, getattr(self, s))
        g.path = list(self.path)
        g.color = list(self.color)
        g.saved_by = None
        return g


# ---- path geometry

def arc_segments(cx, cy, r, a1, a2, clockwise: bool):
    """Bezier curves for an arc (user space), each at most 90 degrees: [(x0, y0), (x1, y1, x2, y2, x3, y3)...].
    The end angle is moved by turns of 360 until it lies past the start (PLRM arc, arcn); a sweep of more than a
    turn is kept to one turn plus what ends it at the same angle (1e30 degrees would otherwise never end)."""
    total = a2 - a1
    if clockwise:
        total = -(-total % 360.0) if total > 0 else (total if total > -360.0 else -360.0 - (-total % 360.0))
    else:
        total = total % 360.0 if total < 0 else (total if total < 360.0 else 360.0 + total % 360.0)
    pieces = max(1, math.ceil(abs(total) / 90.0 - 1e-9))
    step = math.radians(total / pieces)
    k = 4.0 / 3.0 * math.tan(step / 4.0)
    t = math.radians(a1)
    x0, y0 = cx + r * math.cos(t), cy + r * math.sin(t)
    out = [(x0, y0)]
    for _ in range(pieces):
        c0, s0 = math.cos(t), math.sin(t)
        t2 = t + step
        c1, s1 = math.cos(t2), math.sin(t2)
        out.append((cx + r * (c0 - k * s0), cy + r * (s0 + k * c0),
                    cx + r * (c1 + k * s1), cy + r * (s1 - k * c1),
                    cx + r * c1, cy + r * s1))
        t = t2
    return out


def flatten_curve(x0, y0, x1, y1, x2, y2, x3, y3, tol: float = 0.25, depth: int = 0) -> list:
    d = abs((x1 - x3) * (y0 - y3) - (y1 - y3) * (x0 - x3)) + abs((x2 - x3) * (y0 - y3) - (y2 - y3) * (x0 - x3))
    span = math.hypot(x3 - x0, y3 - y0) or 1e-9
    if depth >= 12 or d / span <= tol:
        return [(x3, y3)]
    ax, ay = (x0 + x1) / 2, (y0 + y1) / 2
    bx, by = (x1 + x2) / 2, (y1 + y2) / 2
    cx, cy = (x2 + x3) / 2, (y2 + y3) / 2
    dx, dy = (ax + bx) / 2, (ay + by) / 2
    ex, ey = (bx + cx) / 2, (by + cy) / 2
    fx, fy = (dx + ex) / 2, (dy + ey) / 2
    return flatten_curve(x0, y0, ax, ay, dx, dy, fx, fy, tol, depth + 1) + \
        flatten_curve(fx, fy, ex, ey, cx, cy, x3, y3, tol, depth + 1)


def flatten(path: list, tol: float = 0.25) -> list:
    out = []
    px = py = 0.0
    for seg in path:
        if seg[0] == "t":
            out.append(seg)
        elif seg[0] == "c":
            for x, y in flatten_curve(px, py, *seg[1:], tol=tol):
                out.append(("l", x, y))
            px, py = seg[5], seg[6]
        else:
            out.append(seg)
            if seg[0] != "h":
                px, py = seg[1], seg[2]
    return out


def bbox(path: list):
    xs, ys = [], []
    for seg in path:
        if seg[0] == "t":  # glyphs from charpath: their boxes
            xs += [seg[3][0], seg[3][2]]
            ys += [seg[3][1], seg[3][3]]
        elif seg[0] != "h":
            xs.extend(seg[1::2])
            ys.extend(seg[2::2])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def transform_path(path: list, m) -> list:
    a, b, c, d, e, f = m
    out = []
    for seg in path:
        op = seg[0]
        if op == "h":
            out.append(seg)
        elif op == "t":
            continue
        elif op == "c":
            x1, y1, x2, y2, x3, y3 = seg[1:]
            out.append(("c", a * x1 + c * y1 + e, b * x1 + d * y1 + f, a * x2 + c * y2 + e, b * x2 + d * y2 + f,
                        a * x3 + c * y3 + e, b * x3 + d * y3 + f))
        else:
            x, y = seg[1], seg[2]
            out.append((op, a * x + c * y + e, b * x + d * y + f))
    return out


def reverse(path: list) -> list:
    """Each subpath drawn the other way round (reversepath)."""
    subs, cur = [], []
    for seg in path:
        if seg[0] == "t":
            continue
        if seg[0] == "m" and cur:
            subs.append(cur)
            cur = []
        cur.append(seg)
    if cur:
        subs.append(cur)
    out = []
    for sub in subs:
        closed = sub[-1][0] == "h"
        segs = [s for s in sub if s[0] != "h"]
        if not segs:
            continue
        pts = []  # (end point, segment kind, controls)
        px, py = segs[0][1], segs[0][2]
        nodes = [(px, py, None)]
        for s in segs[1:]:
            if s[0] == "c":
                nodes.append((s[5], s[6], (s[1], s[2], s[3], s[4])))
            else:
                nodes.append((s[1], s[2], None))
        x, y, _ = nodes[-1]
        out.append(("m", x, y))
        for i in range(len(nodes) - 1, 0, -1):
            ctrl = nodes[i][2]
            tx, ty = nodes[i - 1][0], nodes[i - 1][1]
            if ctrl:
                out.append(("c", ctrl[2], ctrl[3], ctrl[0], ctrl[1], tx, ty))
            else:
                out.append(("l", tx, ty))
        if closed:
            out.append(("h",))
        del pts
    return out
