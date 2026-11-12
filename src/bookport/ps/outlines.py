"""Glyph outlines, for drawing text without its fonts (the SVG output): Type 1 charstrings (Adobe's Type 1 Font
Format, chapter 6, with the flex and hint othersubrs of chapter 8), Type 2 charstrings (Adobe TN 5177) and TrueType
``glyf`` outlines (Apple's TrueType Reference Manual).

Each reader gives a path in the font's glyph space, as the graphics state keeps paths: ("m", x, y), ("l", x, y),
("c", x1, y1, x2, y2, x3, y3) and ("h",)."""

from __future__ import annotations

import struct

from .files import decrypt

MAX_DEPTH = 10  # subroutine calls (and accented glyphs) nested deeper than this are damage
MAX_OPS = 20000  # operators in one glyph
MAX_COMPONENTS = 2000  # glyphs put together for one TrueType composite, all levels counted


class _Path:
    def __init__(self, dx: float = 0.0, dy: float = 0.0):
        self.segs: list = []
        self.dx, self.dy = dx, dy
        self.open = False

    def move(self, x, y):
        if self.open:
            self.segs.append(("h",))
        self.segs.append(("m", x + self.dx, y + self.dy))
        self.open = True

    def line(self, x, y):
        if not self.open:
            self.move(x, y)
            return
        self.segs.append(("l", x + self.dx, y + self.dy))

    def curve(self, x1, y1, x2, y2, x3, y3):
        if not self.open:
            self.move(x1, y1)
        d, e = self.dx, self.dy
        self.segs.append(("c", x1 + d, y1 + e, x2 + d, y2 + e, x3 + d, y3 + e))

    def close(self):
        if self.open:
            self.segs.append(("h",))
            self.open = False

    def done(self) -> list:
        self.close()
        return self.segs


# ---- Type 1

class Type1Outlines:
    """Outlines of a Type 1 font (fonts.Type1): charstrings decrypted with lenIV, Subrs, seac accents."""

    def __init__(self, charstrings: dict, private: dict, standard: list[str], len_iv: int = 4):
        self.charstrings = charstrings
        self.len_iv = len_iv  # fonts.Type1 worked it out from the Private dictionary
        subrs = private.get("Subrs")
        self.subrs = subrs.list() if hasattr(subrs, "list") else []
        self.standard = standard
        self._subr_cache: dict[int, bytes] = {}

    def _clear(self, s) -> bytes:
        data = s.bytes() if hasattr(s, "bytes") else bytes(s)
        return decrypt(data, 4330, self.len_iv) if self.len_iv >= 0 else data

    def _subr(self, i: int) -> bytes | None:
        got = self._subr_cache.get(i)
        if got is None:
            if not 0 <= i < len(self.subrs) or not hasattr(self.subrs[i], "bytes"):
                return None
            got = self._subr_cache[i] = self._clear(self.subrs[i])
        return got

    def glyph(self, name: str) -> list:
        return self._glyph(name, _Path(), 0)

    def _glyph(self, name: str, path: _Path, depth: int) -> list:
        cs = self.charstrings.get(name)
        if cs is None or not hasattr(cs, "bytes"):
            cs = self.charstrings.get(".notdef")
            if cs is None or not hasattr(cs, "bytes"):
                return []
        st = _T1State(path)
        try:
            self._run(self._clear(cs), st, 0)
        except _Stop:
            pass
        except (IndexError, struct.error, ZeroDivisionError, OverflowError):
            pass
        if st.seac and depth < 2:
            asb, adx, ady, bchar, achar = st.seac
            base = self.standard[bchar] if 0 <= bchar < 256 else ".notdef"
            accent = self.standard[achar] if 0 <= achar < 256 else ".notdef"
            out = _Path()
            self._glyph(base, out, depth + 1)
            # the accent's origin goes at (adx - asb, ady) from the base's: its own hsbw puts its sidebearing back
            acc = _Path(adx - asb + st.sbx, ady)
            self._glyph(accent, acc, depth + 1)
            return out.done() + acc.done()
        return path.done()

    def _run(self, data: bytes, st: _T1State, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise _Stop
        i, n = 0, len(data)
        stack = st.stack
        while i < n:
            v = data[i]
            if v >= 32:
                if v <= 246:
                    stack.append(v - 139)
                    i += 1
                elif v <= 250:
                    stack.append((v - 247) * 256 + data[i + 1] + 108)
                    i += 2
                elif v <= 254:
                    stack.append(-(v - 251) * 256 - data[i + 1] - 108)
                    i += 2
                else:
                    stack.append(struct.unpack(">i", data[i + 1:i + 5])[0])
                    i += 5
                if len(stack) > 64:
                    raise _Stop
                continue
            st.ops += 1
            if st.ops > MAX_OPS:
                raise _Stop
            if v == 12:
                op = 1200 + data[i + 1] if i + 1 < n else 1200
                i += 2
            else:
                op = v
                i += 1
            if op == 10:  # callsubr
                k = int(stack.pop())
                sub = self._subr(k)
                if sub is not None:
                    self._run(sub, st, depth + 1)
                continue
            if op == 11:  # return
                return
            self._op(op, st)


    def _op(self, op: int, st: _T1State) -> None:
        s = st.stack
        p = st.path
        if op == 13:  # hsbw
            st.sbx = float(s[0]) if s else 0.0
            st.x, st.y = st.sbx, 0.0
        elif op == 1207:  # sbw
            st.sbx = float(s[0]) if s else 0.0
            st.x, st.y = st.sbx, float(s[1]) if len(s) > 1 else 0.0
        elif op in (21, 22, 4):  # rmoveto, hmoveto, vmoveto
            dx, dy = (s[0], s[1]) if op == 21 else (s[0], 0) if op == 22 else (0, s[0])
            st.x += dx
            st.y += dy
            if st.flex is not None:
                st.flex.append((st.x, st.y))
            else:
                p.move(st.x, st.y)
        elif op in (5, 6, 7):  # rlineto, hlineto, vlineto
            dx, dy = (s[0], s[1]) if op == 5 else (s[0], 0) if op == 6 else (0, s[0])
            st.x += dx
            st.y += dy
            p.line(st.x, st.y)
        elif op in (8, 30, 31):  # rrcurveto, vhcurveto, hvcurveto
            if op == 8:
                d = s[:6]
            elif op == 30:
                d = [0, s[0], s[1], s[2], s[3], 0]
            else:
                d = [s[0], 0, s[1], s[2], 0, s[3]]
            x1, y1 = st.x + d[0], st.y + d[1]
            x2, y2 = x1 + d[2], y1 + d[3]
            st.x, st.y = x2 + d[4], y2 + d[5]
            p.curve(x1, y1, x2, y2, st.x, st.y)
        elif op == 9:  # closepath
            p.close()
        elif op == 14:  # endchar
            s.clear()
            raise _Stop
        elif op == 1206:  # seac
            if len(s) >= 5:
                st.seac = (float(s[-5]), float(s[-4]), float(s[-3]), int(s[-2]), int(s[-1]))
            s.clear()
            raise _Stop
        elif op == 1212:  # div
            b = s.pop()
            a = s.pop()
            s.append(a / b if b else 0)
            return
        elif op == 1216:  # callothersubr
            other = int(s.pop())
            k = int(s.pop())
            args = [s.pop() for _ in range(min(k, len(s)))][::-1]
            if other == 1:  # flex starts: the moves that follow are its points
                st.flex = []
            elif other == 0 and st.flex is not None:  # flex ends: a reference point and two curves
                pts = st.flex
                st.flex = None
                if len(pts) >= 7:
                    p.curve(*pts[1], *pts[2], *pts[3])
                    p.curve(*pts[4], *pts[5], *pts[6])
                    st.x, st.y = pts[6]
                st.ps = [args[2], args[1]] if len(args) >= 3 else []
            elif other == 3:  # hint replacement: answered with subr 3, which does nothing
                st.ps = [3]
            else:
                st.ps = args[::-1]
            return
        elif op == 1217:  # pop
            s.append(st.ps.pop() if st.ps else 0)
            return
        elif op == 1233:  # setcurrentpoint
            if len(s) >= 2:
                st.x, st.y = float(s[-2]), float(s[-1])
        s.clear()


class _T1State:
    __slots__ = ("flex", "ops", "path", "ps", "sbx", "seac", "stack", "x", "y")

    def __init__(self, path: _Path):
        self.path = path
        self.stack: list = []
        self.ps: list = []
        self.x = self.y = self.sbx = 0.0
        self.flex: list | None = None
        self.seac = None
        self.ops = 0


class _Stop(Exception):  # the end of a glyph, not an error
    pass


# ---- Type 2 (CFF)

def _bias(subrs: list) -> int:
    n = len(subrs)
    return 107 if n < 1240 else 1131 if n < 33900 else 32768


class Type2Outlines:
    """Outlines of a CFF font (cff.Font): Type 2 charstrings with local and global subroutines."""

    def __init__(self, font, standard: list[str]):
        self.font = font
        self.standard = standard
        self.gids = {n: i for i, n in enumerate(font.names)}

    def glyph(self, gid: int) -> list:
        return self._glyph(gid, _Path(), 0)

    def _glyph(self, gid: int, path: _Path, depth: int) -> list:
        cs = self.font.charstrings
        if not 0 <= gid < len(cs):
            gid = 0
            if not cs:
                return []
        st = _T2State(path)
        try:
            self._run(cs[gid], st, 0)
        except _Stop:
            pass
        except (IndexError, struct.error, ZeroDivisionError, OverflowError, ValueError):
            pass
        if st.seac and depth < 2:
            adx, ady, bchar, achar = st.seac
            out = _Path()
            base = self.gids.get(self.standard[bchar] if 0 <= bchar < 256 else "", 0)
            accent = self.gids.get(self.standard[achar] if 0 <= achar < 256 else "", 0)
            self._glyph(base, out, depth + 1)
            acc = _Path(adx, ady)
            self._glyph(accent, acc, depth + 1)
            return out.done() + acc.done()
        return path.done()

    def _run(self, data: bytes, st: _T2State, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise _Stop
        s = st.stack
        i, n = 0, len(data)
        while i < n:
            v = data[i]
            if v >= 32 or v == 28:
                if v == 28:
                    s.append(struct.unpack(">h", data[i + 1:i + 3])[0])
                    i += 3
                elif v <= 246:
                    s.append(v - 139)
                    i += 1
                elif v <= 250:
                    s.append((v - 247) * 256 + data[i + 1] + 108)
                    i += 2
                elif v <= 254:
                    s.append(-(v - 251) * 256 - data[i + 1] - 108)
                    i += 2
                else:
                    s.append(struct.unpack(">i", data[i + 1:i + 5])[0] / 65536)
                    i += 5
                if len(s) > 96:
                    raise _Stop
                continue
            st.ops += 1
            if st.ops > MAX_OPS:
                raise _Stop
            if v == 12:
                op = 1200 + data[i + 1] if i + 1 < n else 1200
                i += 2
            else:
                op = v
                i += 1
            if op in (10, 29):  # callsubr, callgsubr
                subrs = self.font.subrs if op == 10 else self.font.gsubrs
                k = int(s.pop()) + _bias(subrs)
                if 0 <= k < len(subrs):
                    self._run(subrs[k], st, depth + 1)
                continue
            if op == 11:  # return
                return
            if op in (19, 20):  # hintmask, cntrmask: the stems so far (an implicit vstem) give the mask's size
                self._width(st, len(s) % 2 == 1)
                st.stems += len(s) // 2
                s.clear()
                i += (st.stems + 7) // 8
                continue
            self._op(op, st)

    @staticmethod
    def _width(st: _T2State, extra: bool) -> None:
        if not st.have_width:
            st.have_width = True
            if extra and st.stack:
                st.stack.pop(0)

    def _op(self, op: int, st: _T2State) -> None:
        s = st.stack
        p = st.path
        if op in (1, 3, 18, 23):  # hstem, vstem, hstemhm, vstemhm
            self._width(st, len(s) % 2 == 1)
            st.stems += len(s) // 2
        elif op == 21:  # rmoveto
            self._width(st, len(s) > 2)
            st.x += s[-2]
            st.y += s[-1]
            p.move(st.x, st.y)
        elif op in (22, 4):  # hmoveto, vmoveto
            self._width(st, len(s) > 1)
            if op == 22:
                st.x += s[-1]
            else:
                st.y += s[-1]
            p.move(st.x, st.y)
        elif op == 14:  # endchar (with four numbers: an accented glyph, as seac)
            self._width(st, len(s) in (1, 5))
            if len(s) >= 4:
                st.seac = (float(s[-4]), float(s[-3]), int(s[-2]), int(s[-1]))
            s.clear()
            raise _Stop
        elif op == 5:  # rlineto
            for k in range(0, len(s) - 1, 2):
                st.x += s[k]
                st.y += s[k + 1]
                p.line(st.x, st.y)
        elif op in (6, 7):  # hlineto, vlineto: alternating
            horiz = op == 6
            for d in s:
                if horiz:
                    st.x += d
                else:
                    st.y += d
                p.line(st.x, st.y)
                horiz = not horiz
        elif op == 8:  # rrcurveto
            for k in range(0, len(s) - 5, 6):
                self._curve(st, *s[k:k + 6])
        elif op == 24:  # rcurveline
            k = 0
            while k + 6 <= len(s) - 2:
                self._curve(st, *s[k:k + 6])
                k += 6
            if k + 2 <= len(s):
                st.x += s[k]
                st.y += s[k + 1]
                p.line(st.x, st.y)
        elif op == 25:  # rlinecurve
            k = 0
            while k + 2 <= len(s) - 6:
                st.x += s[k]
                st.y += s[k + 1]
                p.line(st.x, st.y)
                k += 2
            if k + 6 <= len(s):
                self._curve(st, *s[k:k + 6])
        elif op == 26:  # vvcurveto
            k = 0
            dx1 = 0
            if len(s) % 4 == 1:
                dx1 = s[0]
                k = 1
            while k + 4 <= len(s):
                self._curve(st, dx1, s[k], s[k + 1], s[k + 2], 0, s[k + 3])
                dx1 = 0
                k += 4
        elif op == 27:  # hhcurveto
            k = 0
            dy1 = 0
            if len(s) % 4 == 1:
                dy1 = s[0]
                k = 1
            while k + 4 <= len(s):
                self._curve(st, s[k], dy1, s[k + 1], s[k + 2], s[k + 3], 0)
                dy1 = 0
                k += 4
        elif op in (30, 31):  # vhcurveto, hvcurveto: alternating, the last with an optional extra number
            horiz = op == 31
            k = 0
            while k + 4 <= len(s):
                last = len(s) - k == 5
                f = s[k + 4] if last else 0
                if horiz:
                    self._curve(st, s[k], 0, s[k + 1], s[k + 2], f, s[k + 3])
                else:
                    self._curve(st, 0, s[k], s[k + 1], s[k + 2], s[k + 3], f)
                horiz = not horiz
                k += 5 if last else 4
        elif op == 1235:  # flex
            if len(s) >= 12:
                self._curve(st, *s[0:6])
                self._curve(st, *s[6:12])
        elif op == 1234:  # hflex: dx1 dx2 dy2 dx3 dx4 dx5 dx6, back to the starting height
            if len(s) >= 7:
                self._curve(st, s[0], 0, s[1], s[2], s[3], 0)
                self._curve(st, s[4], 0, s[5], -s[2], s[6], 0)
        elif op == 1236:  # hflex1: dx1 dy1 dx2 dy2 dx3 dx4 dx5 dy5 dx6
            if len(s) >= 9:
                self._curve(st, s[0], s[1], s[2], s[3], s[4], 0)
                self._curve(st, s[5], 0, s[6], s[7], s[8], -(s[1] + s[3] + s[7]))
        elif op == 1237:  # flex1: five points, the sixth along the longer direction
            if len(s) >= 11:
                dx = sum(s[0:10:2])
                dy = sum(s[1:10:2])
                self._curve(st, *s[0:6])
                if abs(dx) > abs(dy):
                    self._curve(st, s[6], s[7], s[8], s[9], s[10], -dy)
                else:
                    self._curve(st, s[6], s[7], s[8], s[9], -dx, s[10])
        elif op >= 1200:
            self._arith(op, st)
            return
        s.clear()

    @staticmethod
    def _curve(st: _T2State, a, b, c, d, e, f) -> None:
        x1, y1 = st.x + a, st.y + b
        x2, y2 = x1 + c, y1 + d
        st.x, st.y = x2 + e, y2 + f
        st.path.curve(x1, y1, x2, y2, st.x, st.y)

    @staticmethod
    def _arith(op: int, st: _T2State) -> None:
        s = st.stack
        if op == 1209:
            s.append(abs(s.pop()))
        elif op == 1210:
            b = s.pop()
            s.append(s.pop() + b)
        elif op == 1211:
            b = s.pop()
            s.append(s.pop() - b)
        elif op == 1212:
            b = s.pop()
            a = s.pop()
            s.append(a / b if b else 0)
        elif op == 1214:
            s.append(-s.pop())
        elif op == 1224:
            b = s.pop()
            s.append(s.pop() * b)
        elif op == 1226:
            s.append(abs(s.pop()) ** 0.5)
        elif op == 1218:
            s.pop()
        elif op == 1227:
            s.append(s[-1])
        elif op == 1228:
            s[-1], s[-2] = s[-2], s[-1]
        elif op == 1229:
            k = int(s.pop())
            s.append(s[-1 - k] if 0 <= k < len(s) else s[-1])
        elif op == 1230:
            j = int(s.pop())
            k = int(s.pop())
            if 0 < k <= len(s):
                part = s[-k:]
                j %= k
                s[-k:] = part[-j:] + part[:-j] if j else part
        elif op == 1220:  # put
            i = int(s.pop())
            v = s.pop()
            if 0 <= i < 32:
                st.transient[i] = v
        elif op == 1221:  # get
            i = int(s.pop())
            s.append(st.transient.get(i, 0))
        elif op == 1203:
            b = s.pop()
            s.append(1 if s.pop() and b else 0)
        elif op == 1204:
            b = s.pop()
            s.append(1 if s.pop() or b else 0)
        elif op == 1205:
            s.append(0 if s.pop() else 1)
        elif op == 1215:
            b = s.pop()
            s.append(1 if s.pop() == b else 0)
        elif op == 1222:  # ifelse
            v2 = s.pop()
            v1 = s.pop()
            s2 = s.pop()
            s1 = s.pop()
            s.append(s1 if v1 <= v2 else s2)
        elif op == 1223:  # random: (0, 1], any number will do
            s.append(0.5)
        else:
            s.clear()


class _T2State:
    __slots__ = ("have_width", "ops", "path", "seac", "stack", "stems", "transient", "x", "y")

    def __init__(self, path: _Path):
        self.path = path
        self.stack: list = []
        self.x = self.y = 0.0
        self.stems = 0
        self.have_width = False
        self.seac = None
        self.ops = 0
        self.transient: dict = {}


# ---- TrueType

def truetype(font, gid: int, depth: int = 0, _within: frozenset = frozenset(), _left: list | None = None) -> list:
    """A TrueType glyph's outline (quadratic pieces as cubic curves), in font units; composites put together.
    A component that is the glyph itself (or one it's part of) is left out, and so is everything past
    MAX_COMPONENTS: N components each made of N more, ten deep, would otherwise be N**10 glyphs."""
    try:
        glyf = font.table("glyf")
        loca = font.table("loca")
    except KeyError:
        return []
    if not 0 <= gid < font.num_glyphs:
        gid = 0
    try:
        if font.long_loca:
            a, b = struct.unpack(">II", loca[4 * gid:4 * gid + 8])
        else:
            a, b = struct.unpack(">HH", loca[2 * gid:2 * gid + 4])
            a, b = a * 2, b * 2
    except struct.error:
        return []
    g = glyf[a:b]
    if len(g) < 10:
        return []
    try:
        ncont = struct.unpack(">h", g[:2])[0]
        if ncont >= 0:
            return _simple(g, ncont)
        return _composite(font, g, depth, _within | {gid}, [MAX_COMPONENTS] if _left is None else _left)
    except (IndexError, struct.error):
        return []


def _simple(g: bytes, ncont: int) -> list:
    ends = list(struct.unpack(f">{ncont}H", g[10:10 + 2 * ncont]))
    if not ends:
        return []
    npts = ends[-1] + 1
    pos = 10 + 2 * ncont
    ilen = struct.unpack(">H", g[pos:pos + 2])[0]
    pos += 2 + ilen
    flags = []
    while len(flags) < npts:
        f = g[pos]
        pos += 1
        flags.append(f)
        if f & 8:
            r = g[pos]
            pos += 1
            flags.extend([f] * r)
    flags = flags[:npts]
    xs, ys = [], []
    for coords, short, same in ((xs, 2, 16), (ys, 4, 32)):
        v = 0
        for f in flags:
            if f & short:
                d = g[pos]
                pos += 1
                v += d if f & same else -d
            elif not f & same:
                v += struct.unpack(">h", g[pos:pos + 2])[0]
                pos += 2
            coords.append(v)
    out = []
    start = 0
    for end in ends:
        pts = [(xs[k], ys[k], flags[k] & 1) for k in range(start, end + 1)]
        start = end + 1
        out += _contour(pts)
    return out


def _contour(pts: list) -> list:
    if not pts:
        return []
    n = len(pts)
    first = next((k for k in range(n) if pts[k][2]), None)
    if first is None:  # all off the curve: start between the first two
        x0 = (pts[0][0] + pts[1 % n][0]) / 2
        y0 = (pts[0][1] + pts[1 % n][1]) / 2
        order = pts[1:] + pts[:1]
    else:
        x0, y0 = pts[first][0], pts[first][1]
        order = pts[first + 1:] + pts[:first]
    out = [("m", x0, y0)]
    cx, cy = x0, y0
    ctrl = None
    for x, y, on in [*order, (x0, y0, 1)]:
        if on:
            if ctrl is None:
                out.append(("l", x, y))
            else:
                out.append(_quad(cx, cy, ctrl[0], ctrl[1], x, y))
                ctrl = None
            cx, cy = x, y
        else:
            if ctrl is not None:
                mx, my = (ctrl[0] + x) / 2, (ctrl[1] + y) / 2
                out.append(_quad(cx, cy, ctrl[0], ctrl[1], mx, my))
                cx, cy = mx, my
            ctrl = (x, y)
    out.append(("h",))
    return out


def _quad(x0, y0, qx, qy, x, y):
    return ("c", x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0), x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y), x, y)


def _composite(font, g: bytes, depth: int, within: frozenset, left: list) -> list:
    if depth > MAX_DEPTH:
        return []
    out = []
    pos = 10
    while pos + 4 <= len(g):
        flags, gid = struct.unpack(">HH", g[pos:pos + 4])
        pos += 4
        if flags & 1:
            a1, a2 = struct.unpack(">hh" if flags & 2 else ">HH", g[pos:pos + 4])
            pos += 4
        else:
            a1, a2 = struct.unpack(">bb" if flags & 2 else ">BB", g[pos:pos + 2])
            pos += 2
        a, b, c, d = 1.0, 0.0, 0.0, 1.0
        if flags & 0x0008:
            a = d = struct.unpack(">h", g[pos:pos + 2])[0] / 16384
            pos += 2
        elif flags & 0x0040:
            a, d = (v / 16384 for v in struct.unpack(">hh", g[pos:pos + 4]))
            pos += 4
        elif flags & 0x0080:
            a, b, c, d = (v / 16384 for v in struct.unpack(">hhhh", g[pos:pos + 8]))
            pos += 8
        dx, dy = (a1, a2) if flags & 2 else (0, 0)  # point matching (rare) isn't done: placed at the origin
        left[0] -= 1
        if left[0] < 0:
            break
        for seg in (truetype(font, gid, depth + 1, within, left) if gid not in within else ()):
            if seg[0] == "h":
                out.append(seg)
                continue
            vals = seg[1:]
            moved = []
            for k in range(0, len(vals), 2):
                x, y = vals[k], vals[k + 1]
                moved += [a * x + c * y + dx, b * x + d * y + dy]
            out.append((seg[0], *moved))
        if not flags & 0x0020:
            break
    return out


__all__ = ["Type1Outlines", "Type2Outlines", "truetype"]
