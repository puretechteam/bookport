"""Colour for the PDF to PostScript writer (ISO 32000-1, 8.6 and 8.7; PostScript Language Reference, 3rd ed.,
4.8 and 4.9): colour spaces turned into PostScript's device spaces, PDF functions evaluated (all four types) or
written as PostScript function dictionaries, and shadings written for shfill and makepattern.

PostScript can show DeviceGray, DeviceRGB and DeviceCMYK, Indexed over one of them and Patterns as they are.
Everything else (ICC and CIE-based spaces, Lab, Separation, DeviceN) is turned into the nearest device space
here, in Python: a spot colour prints as its alternate, which is what a composite printer does with it too.
"""

from __future__ import annotations

import math
import re

from .. import limits
from ..pdf.parser import Document, PdfString, Stream

STRING_MAX = 65535  # the longest PostScript string (Appendix B's implementation limit)


# --- writing PostScript values -----------------------------------------------------------------------------

def num(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(v):
        return "0"
    if v == int(v) and abs(v) < 1e9:
        return str(int(v))
    s = format(v, ".7g")
    if "e" in s:
        s = f"{v:.10f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "", "-") else s


def wrap(tokens, width: int = 200) -> str:
    """Tokens joined by spaces, broken into lines (DSC wants lines under 255 characters)."""
    lines, cur, n = [], [], 0
    for t in tokens:
        if n + len(t) + 1 > width and cur:
            lines.append(" ".join(cur))
            cur, n = [], 0
        cur.append(t)
        n += len(t) + 1
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


def nums(vs) -> str:
    return wrap([num(v) for v in vs])


def arr(vs) -> str:
    return "[" + nums(vs) + "]"


_PLAIN = set(range(32, 127)) - {0x28, 0x29, 0x5C}


def string(b: bytes, width: int = 200) -> str:
    """A PostScript literal string, 7-bit clean, broken across lines (backslash newline) when it's long."""
    out = ["("]
    run = 1
    for c in b:
        piece = chr(c) if c in _PLAIN else "\\" + chr(c) if c in (0x28, 0x29, 0x5C) else f"\\{c:03o}"
        if run + len(piece) > width:
            out.append("\\\n")
            run = 0
        out.append(piece)
        run += len(piece)
    out.append(")")
    return "".join(out)


def hexstr(b: bytes, width: int = 128) -> str:
    h = b.hex()
    return "<" + "\n".join(h[i:i + width] for i in range(0, len(h), width)) + ">"


def name(s: str) -> str:
    """/Name with PostScript's delimiters kept out (a name from a PDF can hold any byte)."""
    safe = re.sub(r"[^A-Za-z0-9_.+\-*!$&'|:;?@^`~]", "_", s.removeprefix("/"))
    return "/" + (safe or "_")


def a85(data: bytes, width: int = 75) -> str:
    """ASCII85 lines ending in ~>, as the ASCII85Decode filter reads them."""
    out = bytearray()
    n = len(data)
    full = n - n % 4
    for i in range(0, full, 4):
        v = int.from_bytes(data[i:i + 4], "big")
        if v == 0:
            out += b"z"
            continue
        c = bytearray(5)
        for k in range(4, -1, -1):
            v, c[k] = divmod(v, 85)
        out += bytes(x + 33 for x in c)
    rest = n - full
    if rest:
        v = int.from_bytes(data[full:] + b"\0" * (4 - rest), "big")
        c = bytearray(5)
        for k in range(4, -1, -1):
            v, c[k] = divmod(v, 85)
        out += bytes(x + 33 for x in c[:rest + 1])
    s = out.decode("ascii")
    lines = [s[i:i + width] for i in range(0, len(s), width)]
    # a line starting with % would read as a DSC comment to tools that scan the file
    lines = [(" " + ln) if ln.startswith("%") else ln for ln in lines]
    return "\n".join(lines) + "~>"


# --- functions ---------------------------------------------------------------------------------------------

def _f(v, default=0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(a, b) -> tuple:
    """Matrix product a x b (PostScript and PDF order: a applies first)."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def matrix(doc: Document, v, default=IDENTITY) -> tuple:
    v = doc.resolve(v)
    if isinstance(v, list) and len(v) == 6:
        return tuple(_f(doc.resolve(x)) for x in v)
    return default


def _floats(doc: Document, v) -> list[float]:
    v = doc.resolve(v)
    return [_f(doc.resolve(x)) for x in v] if isinstance(v, list) else []


def _interp(x, x0, x1, y0, y1):
    return y0 if x1 == x0 else y0 + (x - x0) * (y1 - y0) / (x1 - x0)


class Function:
    """A PDF function: ``f(inputs) -> outputs``, ``ps()`` the same as a PostScript function dictionary
    (None when PostScript has no such function, then it's sampled)."""

    n_in = 1
    n_out = 1
    domain: list[float]

    def __call__(self, xs: list[float]) -> list[float]:
        raise NotImplementedError

    def ps(self) -> str | None:
        return None


class Sampled(Function):
    def __init__(self, doc: Document, d: dict, data: bytes):
        self.domain = _floats(doc, d.get("Domain")) or [0.0, 1.0]
        self.range = _floats(doc, d.get("Range"))
        self.n_in, self.n_out = max(1, len(self.domain) // 2), max(1, len(self.range) // 2)
        size = doc.resolve(d.get("Size")) or []
        self.size = [max(1, min(int(_f(doc.resolve(s), 1)), 1 << 16)) for s in size][:self.n_in] or [2]
        while len(self.size) < self.n_in:
            self.size.append(2)
        self.bps = int(_f(doc.resolve(d.get("BitsPerSample")), 8))
        if self.bps not in (1, 2, 4, 8, 12, 16, 24, 32):
            self.bps = 8
        self.order = int(_f(doc.resolve(d.get("Order")), 1))
        self.encode = _floats(doc, d.get("Encode")) or [v for s in self.size for v in (0, s - 1)]
        self.decode = _floats(doc, d.get("Decode")) or list(self.range)
        self.data = data
        count = 1
        for s in self.size:
            count *= s
        self.count = count
        self.max = (1 << self.bps) - 1

    def _sample(self, index: int, j: int) -> float:
        k = (index * self.n_out + j) * self.bps
        byte = k >> 3
        if self.bps >= 8:
            nb = self.bps // 8
            raw = self.data[byte:byte + nb]
            v = int.from_bytes(raw, "big") if len(raw) == nb else 0
        else:
            b = self.data[byte] if byte < len(self.data) else 0
            v = (b >> (8 - self.bps - (k & 7))) & self.max
        lo, hi = self.decode[2 * j:2 * j + 2] if len(self.decode) >= 2 * j + 2 else (0.0, 1.0)
        return lo + v * (hi - lo) / self.max

    def __call__(self, xs):
        pos = []
        for i in range(self.n_in):
            x = xs[i] if i < len(xs) else 0.0
            d0, d1 = self.domain[2 * i:2 * i + 2] if len(self.domain) >= 2 * i + 2 else (0.0, 1.0)
            x = min(max(x, min(d0, d1)), max(d0, d1))
            e0, e1 = self.encode[2 * i:2 * i + 2] if len(self.encode) >= 2 * i + 2 else (0, self.size[i] - 1)
            e = min(max(_interp(x, d0, d1, e0, e1), 0), self.size[i] - 1)
            pos.append(e)
        out = []
        corners = min(self.n_in, 4)
        for j in range(self.n_out):
            total = 0.0
            for mask in range(1 << corners):
                weight, index, stride = 1.0, 0, 1
                for i in range(self.n_in):
                    e = pos[i]
                    lo = int(e)
                    frac = e - lo
                    if i < corners and mask >> i & 1:
                        idx, w = min(lo + 1, self.size[i] - 1), frac
                    elif i < corners:
                        idx, w = lo, 1 - frac
                    else:
                        idx, w = round(e), 1.0
                    weight *= w
                    index += idx * stride
                    stride *= self.size[i]
                if weight:
                    total += weight * self._sample(index, j)
            out.append(total)
        return _clip(out, self.range)

    def ps(self):
        need = (self.count * self.n_out * self.bps + 7) // 8
        if self.bps > 12 and self.bps != 16 and self.bps != 24 and self.bps != 32 or need > STRING_MAX:
            return None
        if self.bps in (24, 32):
            return None
        data = self.data[:need] + bytes(max(0, need - len(self.data)))
        return ("<< /FunctionType 0 /Domain " + arr(self.domain) + " /Range " + arr(self.range)
                + " /Size [" + " ".join(str(s) for s in self.size) + f"] /BitsPerSample {self.bps}"
                + (" /Order 3" if self.order == 3 else "") + " /Encode " + arr(self.encode)
                + " /Decode " + arr(self.decode) + " /DataSource " + hexstr(data) + " >>")


class Exponential(Function):
    def __init__(self, doc: Document, d: dict):
        self.domain = _floats(doc, d.get("Domain")) or [0.0, 1.0]
        self.range = _floats(doc, d.get("Range"))
        self.c0 = _floats(doc, d.get("C0")) or [0.0]
        self.c1 = _floats(doc, d.get("C1")) or [1.0]
        if len(self.c0) != len(self.c1):
            m = min(len(self.c0), len(self.c1)) or 1
            self.c0, self.c1 = (self.c0 + [0.0])[:m], (self.c1 + [1.0])[:m]
        self.n = _f(doc.resolve(d.get("N")), 1.0)
        self.n_out = len(self.c0)

    def __call__(self, xs):
        x = xs[0] if xs else 0.0
        x = min(max(x, min(self.domain[:2])), max(self.domain[:2]))
        try:
            p = x ** self.n if x > 0 or self.n == int(self.n) else 0.0
        except (OverflowError, ZeroDivisionError):
            p = 0.0
        if isinstance(p, complex):
            p = 0.0
        return _clip([a + p * (b - a) for a, b in zip(self.c0, self.c1)], self.range)

    def ps(self):
        return ("<< /FunctionType 2 /Domain " + arr(self.domain[:2]) + " /C0 " + arr(self.c0) + " /C1 "
                + arr(self.c1) + " /N " + num(self.n) + (" /Range " + arr(self.range) if self.range else "") + " >>")


class Stitching(Function):
    def __init__(self, doc: Document, d: dict, depth: int):
        self.domain = _floats(doc, d.get("Domain")) or [0.0, 1.0]
        self.range = _floats(doc, d.get("Range"))
        fns = doc.resolve(d.get("Functions")) or []
        self.fns = [function(doc, f, depth + 1) for f in fns] if isinstance(fns, list) else []
        self.fns = [f for f in self.fns if f is not None]
        self.bounds = _floats(doc, d.get("Bounds"))
        self.encode = _floats(doc, d.get("Encode"))
        self.n_out = self.fns[0].n_out if self.fns else 1

    def __call__(self, xs):
        if not self.fns:
            return [0.0] * self.n_out
        x = xs[0] if xs else 0.0
        d0, d1 = self.domain[0], self.domain[1] if len(self.domain) > 1 else 1.0
        x = min(max(x, d0), d1)
        k = 0
        while k < len(self.bounds) and k < len(self.fns) - 1 and x >= self.bounds[k]:
            k += 1
        lo = self.bounds[k - 1] if k > 0 else d0
        hi = self.bounds[k] if k < len(self.bounds) else d1
        e0, e1 = self.encode[2 * k:2 * k + 2] if len(self.encode) >= 2 * k + 2 else (0.0, 1.0)
        return _clip(self.fns[min(k, len(self.fns) - 1)]([_interp(x, lo, hi, e0, e1)]), self.range)

    def ps(self):
        subs = [f.ps() for f in self.fns]
        if not subs or any(s is None for s in subs):
            return None
        return ("<< /FunctionType 3 /Domain " + arr(self.domain[:2]) + " /Functions [" + " ".join(subs)
                + "] /Bounds " + arr(self.bounds) + " /Encode " + arr(self.encode)
                + (" /Range " + arr(self.range) if self.range else "") + " >>")


class Calculator(Function):
    """A PostScript calculator function (type 4), run by a small interpreter of its own operators."""

    MAX_STEPS = 10_000

    def __init__(self, doc: Document, d: dict, data: bytes):
        self.domain = _floats(doc, d.get("Domain")) or [0.0, 1.0]
        self.range = _floats(doc, d.get("Range"))
        self.n_in, self.n_out = max(1, len(self.domain) // 2), max(1, len(self.range) // 2)
        self.text = data
        toks = [t for t in re.findall(rb"[{}]|[^\s{}]+", data)][:20_000]
        self.prog, _ = self._parse(toks, 1) if toks and toks[0] == b"{" else ([], 0)

    def _parse(self, toks, i):
        out = []
        while i < len(toks):
            t = toks[i]
            if t == b"{":
                sub, i = self._parse(toks, i + 1)
                out.append(sub)
                continue
            if t == b"}":
                return out, i + 1
            try:
                out.append(float(t) if b"." in t or b"e" in t.lower() else int(t))
            except ValueError:
                out.append(t.decode("latin-1"))
            i += 1
        return out, i

    def __call__(self, xs):
        st: list = []
        for i in range(self.n_in):
            x = xs[i] if i < len(xs) else 0.0
            d0, d1 = self.domain[2 * i:2 * i + 2] if len(self.domain) >= 2 * i + 2 else (0.0, 1.0)
            st.append(min(max(x, min(d0, d1)), max(d0, d1)))
        steps = [0]
        try:
            self._run(self.prog, st, steps)
        except (IndexError, ValueError, TypeError, ZeroDivisionError, OverflowError, RecursionError):
            pass
        out = [_f(v) if not isinstance(v, bool) else float(v) for v in st[-self.n_out:]]
        out = [0.0] * (self.n_out - len(out)) + out
        return _clip(out, self.range)

    def _run(self, prog, st, steps):
        for item in prog:
            steps[0] += 1
            if steps[0] > self.MAX_STEPS:
                raise ValueError("too long")
            if len(st) > MAX_CALC_STACK:
                raise ValueError("stack too deep")
            if isinstance(item, list):
                st.append(item)
                continue
            if not isinstance(item, str):
                st.append(item)
                continue
            op = item
            if op == "if":
                proc, cond = st.pop(), st.pop()
                if cond is True:
                    self._run(proc, st, steps)
            elif op == "ifelse":
                p2, p1, cond = st.pop(), st.pop(), st.pop()
                self._run(p1 if cond is True else p2, st, steps)
            elif op in _CALC2:
                b, a = st.pop(), st.pop()
                st.append(_CALC2[op](a, b))
            elif op in _CALC1:
                st.append(_CALC1[op](st.pop()))
            elif op == "true":
                st.append(True)
            elif op == "false":
                st.append(False)
            elif op == "dup":
                st.append(st[-1])
            elif op == "pop":
                st.pop()
            elif op == "exch":
                st[-1], st[-2] = st[-2], st[-1]
            elif op == "copy":
                n = int(st.pop())
                if not 0 <= n <= len(st) or len(st) + n > MAX_CALC_STACK:
                    raise ValueError("copy")
                if n:
                    st.extend(st[-n:])
            elif op == "index":
                n = int(st.pop())
                if n < 0:
                    raise ValueError("index")
                st.append(st[-1 - n])
            elif op == "roll":
                j, n = int(st.pop()), int(st.pop())
                if n:
                    part = st[-n:]
                    j %= n
                    st[-n:] = part[-j:] + part[:-j] if j else part
            else:
                raise ValueError(op)

    def ps(self):
        return None  # PostScript's own function dictionaries have no type 4; it's sampled


MAX_CALC_STACK = 100  # a type 4 function's stack (ISO 32000 C.2: the limit is 100)


def _shift(a: int, b: int) -> int:
    """bitshift on 32-bit integers (shifting by 32 or more leaves nothing, so 1 2000000000 bitshift is no
    250 MB number)."""
    if not -32 < b < 32:
        return 0
    v = ((a & 0xFFFFFFFF) << b if b >= 0 else (a & 0xFFFFFFFF) >> -b) & 0xFFFFFFFF
    return v - (1 << 32) if v >= 1 << 31 else v


def _bool_or_int(f_int, f_bool):
    def op(a, b):
        if isinstance(a, bool) and isinstance(b, bool):
            return f_bool(a, b)
        return f_int(int(a), int(b))
    return op


def _idiv(a, b):
    q = abs(int(a)) // abs(int(b))
    return q if (a >= 0) == (b >= 0) else -q


def _mod(a, b):
    r = abs(int(a)) % abs(int(b))
    return r if a >= 0 else -r


_CALC2 = {
    "add": lambda a, b: a + b, "sub": lambda a, b: a - b, "mul": lambda a, b: a * b,
    "div": lambda a, b: a / b, "idiv": _idiv, "mod": _mod,
    "exp": lambda a, b: float(a) ** b if a > 0 or b == int(b) else 0.0,
    "atan": lambda a, b: math.degrees(math.atan2(a, b)) % 360,
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
    "and": _bool_or_int(lambda a, b: a & b, lambda a, b: a and b),
    "or": _bool_or_int(lambda a, b: a | b, lambda a, b: a or b),
    "xor": _bool_or_int(lambda a, b: a ^ b, lambda a, b: a != b),
    "bitshift": lambda a, b: _shift(int(a), int(b)),
}
_CALC1 = {
    "abs": abs, "neg": lambda a: -a, "ceiling": lambda a: float(math.ceil(a)), "floor": lambda a: float(math.floor(a)),
    "round": lambda a: float(math.floor(a + 0.5)), "truncate": lambda a: float(math.trunc(a)),
    "sqrt": lambda a: math.sqrt(a), "sin": lambda a: math.sin(math.radians(a)), "cos": lambda a: math.cos(math.radians(a)),
    "ln": lambda a: math.log(a), "log": lambda a: math.log10(a), "cvi": lambda a: int(a), "cvr": lambda a: float(a),
    "not": lambda a: (not a) if isinstance(a, bool) else ~int(a),
}


class Group(Function):
    """An array of one-output functions, used as one."""

    def __init__(self, fns: list[Function]):
        self.fns = fns
        self.domain = fns[0].domain if fns else [0.0, 1.0]
        self.n_in = fns[0].n_in if fns else 1
        self.n_out = sum(f.n_out for f in fns)

    def __call__(self, xs):
        return [v for f in self.fns for v in f(xs)]

    def ps(self):
        subs = [f.ps() for f in self.fns]
        return None if any(s is None for s in subs) else "[" + " ".join(subs) + "]"


def _clip(out, rng):
    if len(rng) >= 2 * len(out):
        return [min(max(v, rng[2 * i]), rng[2 * i + 1]) for i, v in enumerate(out)]
    return out


def function(doc: Document, obj, depth: int = 0) -> Function | None:
    obj = doc.resolve(obj)
    if depth > 8:
        return None
    if isinstance(obj, list):
        fns = [function(doc, f, depth + 1) for f in obj]
        return Group([f for f in fns if f is not None]) if fns and all(fns) else None
    d = obj.dict if isinstance(obj, Stream) else obj
    if not isinstance(d, dict):
        return None
    kind = doc.resolve(d.get("FunctionType"))
    try:
        if kind == 0 and isinstance(obj, Stream):
            return Sampled(doc, d, doc.decode(obj))
        if kind == 2:
            return Exponential(doc, d)
        if kind == 3:
            return Stitching(doc, d, depth)
        if kind == 4 and isinstance(obj, Stream):
            return Calculator(doc, d, doc.decode(obj))
    except limits.LimitError:
        raise
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        return None
    return None


def sampled_ps(fn, domain: list[float], n_out: int, steps: int | None = None) -> str:
    """fn (one or two inputs) sampled into a PostScript type 0 function, 8 bits a sample."""
    n_in = max(1, len(domain) // 2)
    if n_in == 1:
        steps = steps or 256
        d0, d1 = domain[0], domain[1]
        rows = [fn([d0 + (d1 - d0) * i / (steps - 1)]) for i in range(steps)]
        size = [steps]
    else:
        steps = steps or 48
        d0, d1, e0, e1 = domain[:4]
        rows = [fn([d0 + (d1 - d0) * i / (steps - 1), e0 + (e1 - e0) * j / (steps - 1)])
                for j in range(steps) for i in range(steps)]
        size = [steps, steps]
        domain = domain[:4]
    data = bytes(max(0, min(255, round(v * 255))) for row in rows for v in (list(row) + [0.0] * n_out)[:n_out])
    return ("<< /FunctionType 0 /Domain " + arr(domain) + " /Range " + arr([0, 1] * n_out) + " /Size ["
            + " ".join(str(s) for s in size) + "] /BitsPerSample 8 /DataSource " + hexstr(data) + " >>")


# --- colour spaces -----------------------------------------------------------------------------------------

DEVICE = {"gray": ("DeviceGray", 1), "rgb": ("DeviceRGB", 3), "cmyk": ("DeviceCMYK", 4)}
SET = {"gray": "g", "rgb": "rg", "cmyk": "k"}


class ColorSpace:
    """kind: gray, rgb, cmyk (PostScript shows these), indexed, separation (also DeviceN), lab or pattern."""

    def __init__(self, kind: str, n: int, **kw):
        self.kind, self.n = kind, n
        self.base: ColorSpace | None = kw.get("base")
        self.hival: int = kw.get("hival", 0)
        self.table: bytes = kw.get("table", b"")
        self.fn: Function | None = kw.get("fn")
        self.none: bool = kw.get("none", False)  # a separation called None: never painted
        self.white = kw.get("white", (0.9505, 1.0, 1.089))
        self.range = kw.get("range", [-100.0, 100.0, -100.0, 100.0])

    @property
    def device(self) -> str:
        """The device space this one is shown in: gray, rgb or cmyk."""
        if self.kind in DEVICE:
            return self.kind
        if self.kind == "lab":
            return "rgb"
        if self.base is not None:
            return self.base.device
        return "gray"

    def initial(self) -> list[float]:
        if self.kind == "cmyk":
            return [0.0, 0.0, 0.0, 1.0]
        if self.kind == "separation":
            return [1.0] * self.n
        if self.kind == "lab":
            return [0.0, 0.0, 0.0]
        return [0.0] * self.n

    def convert(self, comps: list[float]) -> list[float]:
        """comps as values of the device space (``device``), each 0 to 1."""
        comps = list(comps) + [0.0] * (self.n - len(comps))
        k = self.kind
        if k in DEVICE:
            return [min(max(v, 0.0), 1.0) for v in comps[:self.n]]
        if k == "indexed":
            i = max(0, min(self.hival, round(comps[0])))
            base = self.base
            m = base.n if base else 1
            raw = self.table[i * m:(i + 1) * m]
            vals = [b / 255 for b in raw] + [0.0] * (m - len(raw))
            if base is not None and base.kind not in DEVICE:
                # a lookup table holds the base space's components over its own ranges
                if base.kind == "lab":
                    r = base.range
                    vals = [vals[0] * 100, r[0] + vals[1] * (r[1] - r[0]), r[2] + vals[2] * (r[3] - r[2])]
                return base.convert(vals)
            return vals
        if k == "separation":
            alt = self.base or ColorSpace("gray", 1)
            if self.fn is None:
                return alt.convert(alt.initial()) if alt.kind != "gray" else [1.0 - min(max(comps[0], 0), 1)]
            return alt.convert(self.fn(comps[:self.n]))
        if k == "lab":
            return lab_to_rgb(comps, self.white)
        return [0.0]

    def ps(self, comps: list[float]) -> str:
        vals = self.convert(comps)
        return nums(round(v, 4) for v in vals) + " " + SET[self.device]


def lab_to_rgb(lab, white=(0.9505, 1.0, 1.089)) -> list[float]:
    L, a, b = (list(lab) + [0.0, 0.0, 0.0])[:3]
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200

    def g(t):
        return t ** 3 if t > 6 / 29 else 3 * (6 / 29) ** 2 * (t - 4 / 29)
    x, y, z = white[0] * g(fx), white[1] * g(fy), white[2] * g(fz)
    # XYZ (D65 white) to linear sRGB, then sRGB's curve
    r = 3.2406 * x - 1.5372 * y - 0.4986 * z
    gg = -0.9689 * x + 1.8758 * y + 0.0415 * z
    bb = 0.0557 * x - 0.2040 * y + 1.0570 * z

    def curve(c):
        c = min(max(c, 0.0), 1.0)
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return [curve(r), curve(gg), curve(bb)]


GRAY, RGB, CMYK = ColorSpace("gray", 1), ColorSpace("rgb", 3), ColorSpace("cmyk", 4)
NAMED = {"DeviceGray": GRAY, "G": GRAY, "CalGray": GRAY, "DeviceRGB": RGB, "RGB": RGB, "CalRGB": RGB,
         "DeviceCMYK": CMYK, "CMYK": CMYK}


def colorspace(doc: Document, obj, resources: dict | None, depth: int = 0) -> ColorSpace:
    """A PDF colour space (a name, a resource name or an array) as a ColorSpace."""
    r = doc.resolve
    obj = r(obj)
    if depth > 8:
        return GRAY
    if isinstance(obj, str):
        if obj in NAMED:
            return NAMED[obj]
        if obj == "Pattern":
            return ColorSpace("pattern", 0)
        spaces = r((resources or {}).get("ColorSpace")) or {}
        if isinstance(spaces, dict) and obj in spaces:
            return colorspace(doc, spaces[obj], None, depth + 1)
        return GRAY
    if not isinstance(obj, list) or not obj:
        return GRAY
    family = str(r(obj[0]))
    if family in NAMED and len(obj) == 1:
        return NAMED[family]
    if family in ("CalGray",):
        return GRAY
    if family in ("CalRGB",):
        return RGB
    if family == "ICCBased":
        stm = r(obj[1]) if len(obj) > 1 else None
        d = stm.dict if isinstance(stm, Stream) else {}
        n = r(d.get("N"))
        if n in (1, 3, 4):
            return {1: GRAY, 3: RGB, 4: CMYK}[n]
        alt = d.get("Alternate")
        return colorspace(doc, alt, resources, depth + 1) if alt is not None else RGB
    if family == "Lab":
        d = r(obj[1]) if len(obj) > 1 else {}
        d = d if isinstance(d, dict) else {}
        white = _floats(doc, d.get("WhitePoint")) or [0.9505, 1.0, 1.089]
        rng = _floats(doc, d.get("Range")) or [-100.0, 100.0, -100.0, 100.0]
        return ColorSpace("lab", 3, white=tuple((white + [1.0, 1.0, 1.0])[:3]), range=(rng + [0.0] * 4)[:4])
    if family in ("Indexed", "I"):
        if len(obj) < 4:
            return GRAY
        base = colorspace(doc, obj[1], resources, depth + 1)
        hival = max(0, min(255, int(_f(r(obj[2])))))
        look = r(obj[3])
        table = doc.decode(look) if isinstance(look, Stream) else bytes(look) if isinstance(look, (bytes, str)) else b""
        if isinstance(look, str) and not isinstance(look, PdfString):
            table = look.encode("latin-1", "replace")
        return ColorSpace("indexed", 1, base=base, hival=hival, table=bytes(table))
    if family in ("Separation", "DeviceN"):
        if len(obj) < 4:
            return GRAY
        names = r(obj[1])
        n = len(names) if family == "DeviceN" and isinstance(names, list) else 1
        alt = colorspace(doc, obj[2], resources, depth + 1)
        fn = function(doc, obj[3])
        none = family == "Separation" and names == "None" or \
            family == "DeviceN" and isinstance(names, list) and all(r(x) == "None" for x in names)
        if family == "Separation" and names == "All":
            return ColorSpace("separation", 1, base=GRAY, fn=_AllInk(), none=False)
        return ColorSpace("separation", n, base=alt, fn=fn, none=none)
    if family == "Pattern":
        under = colorspace(doc, obj[1], resources, depth + 1) if len(obj) > 1 else None
        return ColorSpace("pattern", under.n if under else 0, base=under)
    return GRAY


class _AllInk(Function):
    """The separation called All: a tint of every colorant, shown as grey."""

    def __init__(self):
        self.domain = [0.0, 1.0]

    def __call__(self, xs):
        return [1.0 - min(max(xs[0] if xs else 0.0, 0.0), 1.0)]


# --- shadings ----------------------------------------------------------------------------------------------

def shading(doc: Document, obj, resources: dict | None, inline: bool) -> tuple[str, bytes] | None:
    """A PDF shading as PostScript shading dictionary text, plus the mesh data to follow it in the file when
    ``inline`` (shfill then reads it from currentfile); None when it can't be shown."""
    r = doc.resolve
    obj = r(obj)
    d = obj.dict if isinstance(obj, Stream) else obj
    if not isinstance(d, dict):
        return None
    kind = r(d.get("ShadingType"))
    if kind not in (1, 2, 3, 4, 5, 6, 7):
        return None
    cs = colorspace(doc, d.get("ColorSpace"), resources)
    if cs.kind == "pattern" or cs.none:
        return None
    fn = function(doc, d.get("Function")) if d.get("Function") is not None else None
    parts = [f"/ShadingType {kind}"]
    out_cs = cs
    func_ps = None
    if fn is not None:
        if kind >= 4:
            dec = _floats(doc, d.get("Decode"))
            domain = dec[4:6] if len(dec) >= 6 else [0.0, 1.0]
        else:
            domain = _floats(doc, d.get("Domain")) or ([0.0, 1.0, 0.0, 1.0] if kind == 1 else [0.0, 1.0])
        if cs.kind in DEVICE:
            func_ps = fn.ps() or sampled_ps(fn, domain, cs.n)
        else:
            out_cs = DEVICE_CS[cs.device]
            func_ps = sampled_ps(lambda xs, f=fn, c=cs: c.convert(f(xs)), domain, out_cs.n)
    elif kind < 4 or cs.kind not in DEVICE:
        return None  # a shading needs its function; mesh vertices in a non-device space aren't drawn (rare)
    parts.append("/ColorSpace /" + DEVICE[out_cs.device][0])
    if func_ps:
        parts.append("/Function " + func_ps)
    bg = _floats(doc, d.get("Background"))
    if bg and len(bg) == cs.n:
        parts.append("/Background " + arr(cs.convert(bg)))
    bbox = _floats(doc, d.get("BBox"))
    if len(bbox) == 4:
        parts.append("/BBox " + arr(bbox))
    if r(d.get("AntiAlias")) is True:
        parts.append("/AntiAlias true")
    for key in ("Coords", "Domain", "Matrix", "Decode"):
        vals = _floats(doc, d.get(key))
        if vals:
            parts.append(f"/{key} " + arr(vals))
    ext = r(d.get("Extend"))
    if isinstance(ext, list) and len(ext) == 2:
        parts.append("/Extend [" + " ".join("true" if r(e) is True else "false" for e in ext) + "]")
    trailing = b""
    if kind >= 4:
        for key in ("BitsPerCoordinate", "BitsPerComponent", "BitsPerFlag", "VerticesPerRow"):
            v = r(d.get(key))
            if isinstance(v, int):
                parts.append(f"/{key} {v}")
        data = doc.decode(obj) if isinstance(obj, Stream) else b""
        if not data:
            return None
        if len(data) <= STRING_MAX:
            parts.append("/DataSource " + hexstr(data))
        elif inline:
            parts.append("/DataSource currentfile /ASCII85Decode filter")
            trailing = data
        else:
            return None
    return "<< " + " ".join(parts) + " >>", trailing


DEVICE_CS = {"gray": GRAY, "rgb": RGB, "cmyk": CMYK}
