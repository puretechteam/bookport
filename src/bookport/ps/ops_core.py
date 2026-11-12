"""The operators that aren't graphics (PLRM 8.1): stack, arithmetic, relational, control, types and conversion,
arrays, strings, dictionaries, files, VM, resources and the misc ones programs test for."""

from __future__ import annotations

import math
import time

from .. import limits
from . import files as F
from .interp import (
    MAX_ESTACK,
    Callback,
    FileRun,
    Interp,
    Loop,
    Proc,
    Quit,
    Sentinel,
    Stopped,
    count_to_mark,
    pop_bool,
    pop_int,
    pop_num,
    pop_proc,
)
from .objects import (
    GLOBAL,
    MARK,
    FontID,
    GStateObj,
    Mark,
    Name,
    Operator,
    PSArray,
    PSDict,
    PSError,
    PSString,
    Save,
    XName,
    executable,
    is_exec,
    key,
    literal,
    type_name,
)
from .scan import EOF, number, token

MAX_ARRAY = 2_000_000
MAX_STRING = 64 * 1024 * 1024
INT_MIN, INT_MAX = -2147483648, 2147483647
OPS: dict[str, object] = {}


def op(*names):
    def reg(fn):
        for n in names or (fn.__name__.rstrip("_"),):
            OPS[n] = fn
        return fn
    return reg


def _int(v):
    return v if INT_MIN <= v <= INT_MAX else float(v)


# ---- operand stack

@op("pop")
def _pop(I):
    I.os.pop()


@op()
def exch(I):
    s = I.os
    if len(s) < 2:
        raise PSError("stackunderflow")
    s[-1], s[-2] = s[-2], s[-1]


@op()
def dup(I):
    I.os.append(I.os[-1])


@op()
def index(I):
    n = pop_int(I)
    if n < 0 or n >= len(I.os):
        raise PSError("rangecheck")
    I.os.append(I.os[-1 - n])


@op()
def roll(I):
    j = pop_int(I)
    n = pop_int(I)
    s = I.os
    if n < 0:
        raise PSError("rangecheck")
    if n > len(s):
        raise PSError("stackunderflow")
    if n == 0:
        return
    j %= n
    if j:
        part = s[-n:]
        s[-n:] = part[-j:] + part[:-j]


@op()
def clear(I):
    I.os.clear()


@op()
def count(I):
    I.os.append(len(I.os))


@op("mark", "[", "<<")
def mark(I):
    I.os.append(MARK)


@op()
def cleartomark(I):
    n = count_to_mark(I.os)
    del I.os[len(I.os) - n - 1:]


@op()
def counttomark(I):
    I.os.append(count_to_mark(I.os))


@op()
def copy(I):
    top = I.os.pop()
    s = I.os
    if type(top) is int:
        if top < 0 or top > len(s):
            raise PSError("rangecheck")
        if top:
            s.extend(s[-top:])
        return
    src = s.pop()
    if type(src) is PSArray and type(top) is PSArray:
        if src.length > top.length:
            raise PSError("rangecheck")
        I.aput(top, 0, src.list()[:])
        s.append(PSArray(top.items, top.start, src.length, top.x, top.vm))
    elif type(src) is PSString and type(top) is PSString:
        if src.length > top.length:
            raise PSError("rangecheck")
        top.buf[top.start:top.start + src.length] = src.bytes()
        s.append(PSString(top.buf, top.start, src.length, top.x))
    elif type(src) is PSDict and type(top) is PSDict:
        for k, v in list(src.d.items()):
            I.dput(top, k, v)
        s.append(top)
    elif type(src) is GStateObj and type(top) is GStateObj:
        top.gs = src.gs.copy()
        s.append(top)
    else:
        raise PSError("typecheck")


# ---- arithmetic

def _two(I):
    b = pop_num(I)
    a = pop_num(I)
    return a, b


@op()
def add(I):
    s = I.os
    b, a = s[-1], s[-2]
    if type(a) is int and type(b) is int:
        s.pop()
        s[-1] = _int(a + b)
    elif type(a) in (int, float) and type(b) in (int, float):
        s.pop()
        s[-1] = float(a) + b
    else:
        raise PSError("typecheck")


@op()
def sub(I):
    s = I.os
    b, a = s[-1], s[-2]
    if type(a) is int and type(b) is int:
        s.pop()
        s[-1] = _int(a - b)
    elif type(a) in (int, float) and type(b) in (int, float):
        s.pop()
        s[-1] = float(a) - b
    else:
        raise PSError("typecheck")


@op()
def mul(I):
    s = I.os
    b, a = s[-1], s[-2]
    if type(a) is int and type(b) is int:
        s.pop()
        s[-1] = _int(a * b)
    elif type(a) in (int, float) and type(b) in (int, float):
        s.pop()
        s[-1] = float(a) * b
    else:
        raise PSError("typecheck")


@op()
def div(I):
    a, b = _two(I)
    if b == 0:
        raise PSError("undefinedresult")
    I.os.append(a / b)


@op()
def idiv(I):
    b = pop_int(I)
    a = pop_int(I)
    if b == 0:
        raise PSError("undefinedresult")
    q = abs(a) // abs(b)
    I.os.append(_int(q if (a >= 0) == (b >= 0) else -q))


@op()
def mod(I):
    b = pop_int(I)
    a = pop_int(I)
    if b == 0:
        raise PSError("undefinedresult")
    r = abs(a) % abs(b)
    I.os.append(r if a >= 0 else -r)


@op("abs")
def abs_(I):
    a = pop_num(I)
    I.os.append(_int(abs(a)) if type(a) is int else abs(a))


@op()
def neg(I):
    a = pop_num(I)
    I.os.append(_int(-a) if type(a) is int else -a)


def _rounder(fn):
    def r(I):
        a = pop_num(I)
        I.os.append(a if type(a) is int else float(fn(a)))
    return r


OPS["ceiling"] = _rounder(math.ceil)
OPS["floor"] = _rounder(math.floor)
OPS["round"] = _rounder(lambda a: math.floor(a + 0.5))
OPS["truncate"] = _rounder(math.trunc)


@op()
def sqrt(I):
    a = pop_num(I)
    if a < 0:
        raise PSError("rangecheck")
    I.os.append(math.sqrt(a))


@op()
def atan(I):
    num, den = _two(I)
    if num == 0 and den == 0:
        raise PSError("undefinedresult")
    I.os.append(math.degrees(math.atan2(num, den)) % 360.0)


@op()
def cos(I):
    I.os.append(math.cos(math.radians(pop_num(I))))


@op()
def sin(I):
    I.os.append(math.sin(math.radians(pop_num(I))))


@op("exp")
def exp_(I):
    base, e = _two(I)
    if base < 0 and e != int(e):
        raise PSError("undefinedresult")
    try:
        I.os.append(float(base) ** e)
    except (OverflowError, ZeroDivisionError):
        raise PSError("undefinedresult") from None


@op()
def ln(I):
    a = pop_num(I)
    if a <= 0:
        raise PSError("rangecheck")
    I.os.append(math.log(a))


@op()
def log(I):
    a = pop_num(I)
    if a <= 0:
        raise PSError("rangecheck")
    I.os.append(math.log10(a))


@op()
def rand(I):
    """The seed is the interpreter's own, so the same file converts the same way every time."""
    I.seed = (I.seed * 1103515245 + 12345) & 0x7FFFFFFF
    I.os.append(I.seed)


@op()
def srand(I):
    I.seed = pop_int(I) & 0x7FFFFFFF


@op()
def rrand(I):
    I.os.append(I.seed)


# ---- relational, boolean, bitwise

def _same(a, b) -> bool:
    ta, tb = type(a), type(b)
    if ta in (int, float) and tb in (int, float):
        return a == b
    if ta in (PSString, Name, XName) and tb in (PSString, Name, XName):
        return (a.text() if ta is PSString else a) == (b.text() if tb is PSString else b)
    if ta is bool or tb is bool:
        return ta is tb and a == b
    if ta is PSArray and tb is PSArray:
        return a.items is b.items and a.start == b.start and a.length == b.length
    if ta is PSDict and tb is PSDict:
        return a.d is b.d
    if ta is Operator and tb is Operator:
        return a.fn is b.fn
    if hasattr(a, "stream") and hasattr(b, "stream"):
        return a.stream is b.stream
    return a is b


@op()
def eq(I):
    b = I.os.pop()
    a = I.os.pop()
    I.os.append(_same(a, b))


@op()
def ne(I):
    b = I.os.pop()
    a = I.os.pop()
    I.os.append(not _same(a, b))


def _order(fn):
    def r(I):
        b = I.os.pop()
        a = I.os.pop()
        if type(a) in (int, float) and type(b) in (int, float):
            I.os.append(fn(a, b))
        elif type(a) is PSString and type(b) is PSString:
            I.os.append(fn(a.bytes(), b.bytes()))
        else:
            raise PSError("typecheck")
    return r


OPS["ge"] = _order(lambda a, b: a >= b)
OPS["gt"] = _order(lambda a, b: a > b)
OPS["le"] = _order(lambda a, b: a <= b)
OPS["lt"] = _order(lambda a, b: a < b)


def _logic(fb, fi):
    def r(I):
        b = I.os.pop()
        a = I.os.pop()
        if type(a) is bool and type(b) is bool:
            I.os.append(fb(a, b))
        elif type(a) is int and type(b) is int:
            v = fi(a, b) & 0xFFFFFFFF
            I.os.append(v - (1 << 32) if v >= 1 << 31 else v)
        else:
            raise PSError("typecheck")
    return r


OPS["and"] = _logic(lambda a, b: a and b, lambda a, b: a & b)
OPS["or"] = _logic(lambda a, b: a or b, lambda a, b: a | b)
OPS["xor"] = _logic(lambda a, b: a != b, lambda a, b: a ^ b)


@op("not")
def not_(I):
    a = I.os.pop()
    if type(a) is bool:
        I.os.append(not a)
    elif type(a) is int:
        I.os.append(~a)
    else:
        raise PSError("typecheck")


@op()
def bitshift(I):
    shift = pop_int(I)
    a = pop_int(I) & 0xFFFFFFFF
    if not -32 < shift < 32:  # every bit shifted out (and no huge number made on the way)
        shift, a = 0, 0
    v = (a << shift if shift >= 0 else a >> -shift) & 0xFFFFFFFF
    I.os.append(v - (1 << 32) if v >= 1 << 31 else v)


@op("true")
def true_(I):
    I.os.append(True)


@op("false")
def false_(I):
    I.os.append(False)


@op("null")
def null_(I):
    I.os.append(None)


# ---- control

@op("exec")
def exec_(I):
    I.push_exec(I.os.pop())


@op("if")
def if_(I):
    proc = pop_proc(I)
    if pop_bool(I):
        I.push_exec(proc)


@op()
def ifelse(I):
    no = pop_proc(I)
    yes = pop_proc(I)
    I.push_exec(yes if pop_bool(I) else no)


def _proc_frame(proc: PSArray):
    return Proc(proc.items, proc.start, proc.start + proc.length)


class ForLoop(Loop):
    __slots__ = ("i", "inc", "limit", "proc", "real")

    def step(self, I):
        i, inc = self.i, self.inc
        if (inc > 0 and i > self.limit) or (inc < 0 and i < self.limit):
            I.es.pop()
            return
        I.os.append(float(i) if self.real else i)
        self.i = i + inc
        if self.proc.length:
            I.es.append(_proc_frame(self.proc))


@op("for")
def for_(I):
    proc = pop_proc(I)
    limit = pop_num(I)
    inc = pop_num(I)
    init = pop_num(I)
    f = ForLoop()
    f.i, f.inc, f.limit, f.proc = init, inc, limit, proc
    f.real = not (type(init) is int and type(inc) is int)
    if f.real:
        f.i = float(init)
    if inc == 0:
        f.limit = math.inf if (init <= limit) else -math.inf
        f.inc = 0
        if init > limit:
            return
    I.es.append(f)


class RepeatLoop(Loop):
    __slots__ = ("n", "proc")

    def step(self, I):
        if self.n <= 0:
            I.es.pop()
            return
        self.n -= 1
        if self.proc.length:  # (each turn counts against the step budget in Interp.run, empty or not)
            I.es.append(_proc_frame(self.proc))


@op()
def repeat(I):
    proc = pop_proc(I)
    n = pop_int(I)
    if n < 0:
        raise PSError("rangecheck")
    f = RepeatLoop()
    f.n, f.proc = n, proc
    I.es.append(f)


class ForeverLoop(Loop):
    __slots__ = ("proc",)

    def step(self, I):
        if self.proc.length:  # (counted by Interp.run, like every loop's turns)
            I.es.append(_proc_frame(self.proc))


@op()
def loop(I):
    f = ForeverLoop()
    f.proc = pop_proc(I)
    I.es.append(f)


class ForallLoop(Loop):
    __slots__ = ("i", "pairs", "proc", "values")

    def step(self, I):
        if self.i >= len(self.values):
            I.es.pop()
            return
        v = self.values[self.i]
        self.i += 1
        if self.pairs:
            I.os.append(v[0])
            I.os.append(v[1])
        else:
            I.os.append(v)
        if self.proc.length:
            I.es.append(_proc_frame(self.proc))


def _dict_key_out(k):
    return Name(k) if type(k) is str or type(k) is XName else k


@op()
def forall(I):
    proc = pop_proc(I)
    obj = I.os.pop()
    f = ForallLoop()
    f.proc, f.i, f.pairs = proc, 0, False
    t = type(obj)
    if t is PSArray:
        f.values = list(obj.list())
    elif t is PSString:
        f.values = list(obj.bytes())
    elif t is PSDict:
        f.values = [(_dict_key_out(k), v) for k, v in obj.d.items()]
        f.pairs = True
    else:
        raise PSError("typecheck")
    I.es.append(f)


@op("exit")
def exit_(I):
    es = I.es
    while es:
        f = es[-1]
        if isinstance(f, Loop):
            es.pop()
            return
        if type(f) in (Stopped, Sentinel, FileRun):
            raise PSError("invalidexit")
        es.pop()
    raise PSError("invalidexit")


@op()
def stop(I):
    from .interp import _Unwind
    raise _Unwind


@op()
def stopped(I):
    obj = I.os.pop()
    if len(I.es) >= MAX_ESTACK:
        raise PSError("execstackoverflow")
    I.es.append(Stopped())
    I.push_exec(obj)


@op()
def countexecstack(I):
    I.os.append(len(I.es))


@op()
def execstack(I):
    arr = I.os.pop()
    items = [None] * len(I.es)
    if arr.length < len(items):
        raise PSError("rangecheck")
    I.aput(arr, 0, items)
    I.os.append(PSArray(arr.items, arr.start, len(items), False, arr.vm))


@op("quit")
def quit_(I):
    raise Quit


@op()
def start(I):
    pass


# ---- types, attributes, conversion

@op("type")
def type_(I):
    I.os.append(XName(type_name(I.os.pop())))


@op()
def cvlit(I):
    I.os.append(literal(I.os.pop()))


@op()
def cvx(I):
    I.os.append(executable(I.os.pop()))


@op()
def xcheck(I):
    I.os.append(is_exec(I.os.pop()))


@op("executeonly", "noaccess", "readonly")
def readonly(I):
    """An array or string comes back as a read-only copy of the object (the original, and other copies sharing its
    values, keep their access); a dictionary's access is the dictionary's own (PLRM 3.3.2)."""
    v = I.os[-1]
    t = type(v)
    if t is PSArray or t is PSString:
        c = t.__new__(t)
        for s in t.__slots__:
            setattr(c, s, getattr(v, s))
        c.readonly = True
        I.os[-1] = c
    elif hasattr(v, "readonly"):
        v.readonly = True


@op()
def rcheck(I):
    I.os.pop()
    I.os.append(True)


@op()
def wcheck(I):
    v = I.os.pop()
    I.os.append(not getattr(v, "readonly", False))


@op()
def cvi(I):
    v = I.os.pop()
    if type(v) is PSString:
        n = number(v.bytes().strip()) if v.bytes().strip() else None
        if n is None:
            raise PSError("syntaxerror")
        v = n
    if type(v) is int:
        I.os.append(v)
    elif type(v) is float:
        if math.isnan(v) or not INT_MIN <= v <= INT_MAX:
            raise PSError("rangecheck")
        I.os.append(int(v))
    else:
        raise PSError("typecheck")


@op()
def cvr(I):
    v = I.os.pop()
    if type(v) is PSString:
        n = number(v.bytes().strip()) if v.bytes().strip() else None
        if n is None:
            raise PSError("syntaxerror")
        v = n
    if type(v) in (int, float):
        I.os.append(float(v))
    else:
        raise PSError("typecheck")


@op()
def cvn(I):
    s = I.os.pop()
    if type(s) is not PSString:
        raise PSError("typecheck")
    I.os.append(XName(s.text()) if s.x else Name(s.text()))


def real_text(v: float) -> str:
    if math.isinf(v) or math.isnan(v):
        return "inf" if v > 0 else "-inf" if v < 0 else "nan"
    if v == int(v) and abs(v) < 1e7:
        return f"{v:.1f}"
    s = f"{v:.6g}"
    if "e" in s:
        m, e = s.split("e")
        return f"{m}e{int(e):+03d}"
    return s if "." in s else s + ".0"


def text_of(v) -> bytes:
    """What cvs and = write for v."""
    t = type(v)
    if t is int:
        return str(v).encode()
    if t is float:
        return real_text(v).encode()
    if t is bool:
        return b"true" if v else b"false"
    if t is PSString:
        return v.bytes()
    if t is Name or t is XName:
        return v.encode("latin-1", "replace")
    if t is Operator:
        return v.name.encode("latin-1", "replace")
    return b"--nostringval--"


@op()
def cvs(I):
    s = I.os.pop()
    v = I.os.pop()
    data = text_of(v)
    if type(s) is not PSString:
        raise PSError("typecheck")
    if len(data) > s.length:
        raise PSError("rangecheck")
    s.buf[s.start:s.start + len(data)] = data
    I.os.append(PSString(s.buf, s.start, len(data)))


@op()
def cvrs(I):
    s = I.os.pop()
    radix = pop_int(I)
    v = pop_num(I)
    if not 2 <= radix <= 36:
        raise PSError("rangecheck", f"radix {radix}")
    if radix == 10:
        data = text_of(v)
    else:
        n = int(v) & 0xFFFFFFFF
        digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        out = ""
        while True:
            out = digits[n % radix] + out
            n //= radix
            if not n:
                break
        data = out.encode()
    if len(data) > s.length:
        raise PSError("rangecheck")
    s.buf[s.start:s.start + len(data)] = data
    I.os.append(PSString(s.buf, s.start, len(data)))


# ---- arrays, strings, dictionaries (the polymorphic ones)

@op()
def array(I):
    n = pop_int(I)
    if not 0 <= n <= MAX_ARRAY:
        raise PSError("rangecheck" if n < 0 else "limitcheck")
    limits.charge(8 * n)  # everything a program makes counts against the file's budget
    I.os.append(PSArray([None] * n))


@op("]")
def endarray(I):
    n = count_to_mark(I.os)
    items = I.os[len(I.os) - n:]
    del I.os[len(I.os) - n - 1:]
    I.os.append(PSArray(items))


@op()
def packedarray(I):
    n = pop_int(I)
    if n < 0 or n > len(I.os):
        raise PSError("rangecheck")
    items = I.os[len(I.os) - n:] if n else []
    del I.os[len(I.os) - n:]
    a = PSArray(items)
    a.readonly = True
    I.os.append(a)


@op()
def setpacking(I):
    I.packing = pop_bool(I)


@op()
def currentpacking(I):
    I.os.append(I.packing)


@op()
def aload(I):
    a = I.os.pop()
    if type(a) is not PSArray:
        raise PSError("typecheck")
    I.os.extend(a.list())
    I.os.append(a)


@op()
def astore(I):
    a = I.os.pop()
    if type(a) is not PSArray:
        raise PSError("typecheck")
    n = a.length
    if n > len(I.os):
        raise PSError("stackunderflow")
    vals = I.os[len(I.os) - n:] if n else []
    del I.os[len(I.os) - n:]
    I.aput(a, 0, vals)
    I.os.append(a)


@op()
def string(I):
    n = pop_int(I)
    if not 0 <= n <= MAX_STRING:
        raise PSError("rangecheck" if n < 0 else "limitcheck")
    limits.charge(n)
    I.os.append(PSString(bytearray(n)))


@op()
def length(I):
    v = I.os.pop()
    t = type(v)
    if t is PSArray or t is PSString:
        I.os.append(v.length)
    elif t is PSDict:
        I.os.append(len(v.d))
    elif t is Name or t is XName:
        I.os.append(len(v))
    else:
        raise PSError("typecheck")


@op()
def get(I):
    k = I.os.pop()
    obj = I.os.pop()
    t = type(obj)
    if t is PSArray:
        if type(k) is not int:
            raise PSError("typecheck")
        if not 0 <= k < obj.length:
            raise PSError("rangecheck")
        I.os.append(obj.items[obj.start + k])
    elif t is PSString:
        if type(k) is not int:
            raise PSError("typecheck")
        if not 0 <= k < obj.length:
            raise PSError("rangecheck")
        I.os.append(obj.buf[obj.start + k])
    elif t is PSDict:
        v = obj.d.get(key(k), _NONE)
        if v is _NONE:
            raise PSError("undefined", str(k))
        I.os.append(v)
    else:
        raise PSError("typecheck")


_NONE = object()


@op()
def put(I):
    v = I.os.pop()
    k = I.os.pop()
    obj = I.os.pop()
    t = type(obj)
    if t is PSArray:
        if type(k) is not int:
            raise PSError("typecheck")
        if not 0 <= k < obj.length:
            raise PSError("rangecheck")
        I.aput(obj, k, [v])
    elif t is PSString:
        if type(k) is not int or type(v) is not int:
            raise PSError("typecheck")
        if not 0 <= k < obj.length:
            raise PSError("rangecheck")
        obj.buf[obj.start + k] = v & 0xFF
    elif t is PSDict:
        I.dput(obj, key(k), v)
    else:
        raise PSError("typecheck")


@op()
def getinterval(I):
    n = pop_int(I)
    i = pop_int(I)
    obj = I.os.pop()
    if type(obj) not in (PSArray, PSString):
        raise PSError("typecheck")
    if i < 0 or n < 0 or i + n > obj.length:
        raise PSError("rangecheck")
    if type(obj) is PSArray:
        I.os.append(PSArray(obj.items, obj.start + i, n, obj.x, obj.vm))
    else:
        I.os.append(PSString(obj.buf, obj.start + i, n, obj.x))


@op()
def putinterval(I):
    src = I.os.pop()
    i = pop_int(I)
    dst = I.os.pop()
    if type(dst) is PSArray and type(src) is PSArray:
        if i < 0 or i + src.length > dst.length:
            raise PSError("rangecheck")
        I.aput(dst, i, src.list()[:])
    elif type(dst) is PSString and type(src) is PSString:
        if i < 0 or i + src.length > dst.length:
            raise PSError("rangecheck")
        dst.buf[dst.start + i:dst.start + i + src.length] = src.bytes()
    else:
        raise PSError("typecheck")


@op()
def search(I):
    seek = I.os.pop()
    s = I.os.pop()
    if type(s) is not PSString or type(seek) is not PSString:
        raise PSError("typecheck")
    data = s.bytes()
    j = data.find(seek.bytes())
    if j < 0:
        I.os.append(s)
        I.os.append(False)
        return
    n = seek.length
    I.os.append(PSString(s.buf, s.start + j + n, s.length - j - n))
    I.os.append(PSString(s.buf, s.start + j, n))
    I.os.append(PSString(s.buf, s.start, j))
    I.os.append(True)


@op()
def anchorsearch(I):
    seek = I.os.pop()
    s = I.os.pop()
    if type(s) is not PSString or type(seek) is not PSString:
        raise PSError("typecheck")
    n = seek.length
    if s.bytes().startswith(seek.bytes()):
        I.os.append(PSString(s.buf, s.start + n, s.length - n))
        I.os.append(PSString(s.buf, s.start, n))
        I.os.append(True)
    else:
        I.os.append(s)
        I.os.append(False)


@op("token")
def token_(I):
    src = I.os.pop()
    if type(src) is PSString:
        st = F.Stream(src.bytes(), "string")
        obj = token(st, I.lookup)
        if obj is EOF:
            I.os.append(False)
            return
        I.os.append(PSString(src.buf, src.start + st.pos, src.length - st.pos))
        I.os.append(obj)
        I.os.append(True)
    elif isinstance(src, F.PSFile):
        obj = token(src.stream, I.lookup)
        if obj is EOF:
            I.os.append(False)
        else:
            I.os.append(obj)
            I.os.append(True)
    else:
        raise PSError("typecheck")


@op("dict")
def dict_(I):
    n = pop_int(I)
    if n < 0:
        raise PSError("rangecheck")
    I.os.append(PSDict(maxlength=n))


@op(">>")
def enddict(I):
    n = count_to_mark(I.os)
    if n % 2:
        raise PSError("rangecheck")
    vals = I.os[len(I.os) - n:]
    del I.os[len(I.os) - n - 1:]
    d = {}
    for i in range(0, n, 2):
        d[key(vals[i])] = vals[i + 1]
    I.os.append(PSDict(d, maxlength=len(d)))


@op()
def maxlength(I):
    d = I.os.pop()
    if type(d) is not PSDict:
        raise PSError("typecheck")
    I.os.append(max(d.maxlength, len(d.d)))


@op("begin")
def begin_(I):
    d = I.os.pop()
    if type(d) is not PSDict:
        I.os.append(d)
        raise PSError("typecheck")
    I.begin(d)


@op("end")
def end_(I):
    I.end()


@op("def")
def def_(I):
    v = I.os.pop()
    k = I.os.pop()
    I.dput(I.ds[-1], key(k), v)


@op()
def load(I):
    k = key(I.os.pop())
    I.os.append(I.lookup(k))


@op()
def store(I):
    v = I.os.pop()
    k = key(I.os.pop())
    d = I.where(k)
    I.dput(d if d is not None else I.ds[-1], k, v)


@op()
def known(I):
    k = key(I.os.pop())
    d = I.os.pop()
    if type(d) is not PSDict:
        raise PSError("typecheck")
    I.os.append(k in d.d)


@op()
def undef(I):
    k = key(I.os.pop())
    d = I.os.pop()
    if type(d) is not PSDict:
        raise PSError("typecheck")
    I.dremove(d, k)


@op()
def where(I):
    k = key(I.os.pop())
    d = I.where(k)
    if d is None:
        I.os.append(False)
    else:
        I.os.append(d)
        I.os.append(True)


@op()
def currentdict(I):
    I.os.append(I.ds[-1])


@op()
def countdictstack(I):
    I.os.append(len(I.ds))


@op()
def dictstack(I):
    arr = I.os.pop()
    if arr.length < len(I.ds):
        raise PSError("rangecheck")
    I.aput(arr, 0, list(I.ds))
    I.os.append(PSArray(arr.items, arr.start, len(I.ds), False, arr.vm))


@op()
def cleardictstack(I):
    del I.ds[3:]
    del I.dd[3:]


# ---- VM

@op()
def save(I):
    I.vm_serial += 1
    s = Save(I.gs.copy(), len(I.saves) + 1, I.vm_serial)
    I.saves.append(s)
    I.save_level = len(I.saves)
    I.stamp()
    I.gstack.append(I.gs.copy())
    I.gstack[-1].saved_by = s
    I.os.append(s)


@op()
def restore(I):
    """Back to a save: the graphics state, and every change made since to arrays and dictionaries in local VM
    (definitions in userdict, say: an EPS figure's own showpage goes with it). Global VM and strings keep
    their changes, as the PLRM has it. Saves made inside this one are closed too."""
    s = I.os.pop()
    if type(s) is not Save or not s.valid or not any(t is s for t in I.saves):
        raise PSError("invalidrestore")
    while I.gstack:  # grestoreall back to the save
        g = I.gstack.pop()
        if getattr(g, "saved_by", None) is s:
            break
    I.gs = s.gstate.copy()
    I.undo_to(s)


@op()
def vmstatus(I):
    I.os.extend([I.save_level, 100000, 100000000])


@op()
def setglobal(I):
    I.global_mode = pop_bool(I)
    I.stamp()


@op()
def currentglobal(I):
    I.os.append(I.global_mode)


@op()
def gcheck(I):
    """True for simple objects (they're in no VM) and for arrays and dictionaries made while setglobal was on.
    Strings, files, gstates and saves say false (their VM isn't tracked)."""
    v = I.os.pop()
    t = type(v)
    if t is PSArray or t is PSDict:
        I.os.append(v.vm == GLOBAL)
    else:
        I.os.append(t not in (PSString, GStateObj, Save) and not isinstance(v, F.PSFile))


@op("vmreclaim", "setvmthreshold", "setobjectformat", "setcachelimit", "setshared")
def _drop1(I):
    I.os.pop()


@op("currentobjectformat")
def currentobjectformat(I):
    I.os.append(0)


@op("setuserparams", "setsystemparams")
def _drop_params(I):
    I.os.pop()


@op("setdevparams")
def setdevparams(I):
    del I.os[-2:]


@op("currentuserparams", "currentsystemparams")
def currentuserparams(I):
    I.os.append(PSDict({Name("MaxDictStack"): 10000, Name("MaxOpStack"): 1000000,
                        Name("MaxExecStack"): MAX_ESTACK}))


@op("setcacheparams")
def setcacheparams(I):
    n = count_to_mark(I.os)
    del I.os[len(I.os) - n - 1:]


@op("currentcacheparams")
def currentcacheparams(I):
    I.os.extend([MARK, 100000, 1000, 1000])


@op("cachestatus")
def cachestatus(I):
    I.os.extend([0, 100000, 0, 1000, 0, 1000, 0])


@op("defineuserobject")
def defineuserobject(I):
    v = I.os.pop()
    i = pop_int(I)
    if not 0 <= i < MAX_ARRAY:
        raise PSError("rangecheck" if i < 0 else "limitcheck", f"user object {i}")
    uo = I.userdict.d.get(Name("UserObjects"))
    if type(uo) is not PSArray:
        uo = PSArray([])
    if uo.length <= i:  # a longer array in its place (PLRM: the old one's contents copied over)
        limits.charge((i + 1 - uo.length) * 8)
        uo = PSArray(uo.list() + [None] * (i + 1 - uo.length))
        I.dput(I.userdict, Name("UserObjects"), uo)
    I.aput(uo, i, [v])


@op("execuserobject")
def execuserobject(I):
    i = pop_int(I)
    uo = I.userdict.d.get(Name("UserObjects"))
    if type(uo) is not PSArray or i >= uo.length:
        raise PSError("undefined")
    if i < 0:
        raise PSError("rangecheck")
    I.push_exec(uo.items[uo.start + i])


@op("undefineuserobject")
def undefineuserobject(I):
    pop_int(I)


# ---- misc

@op()
def bind(I):
    proc = I.os[-1]
    if type(proc) is PSArray:
        _bind(I, proc, 0)


def _bind(I, proc: PSArray, depth: int) -> None:
    if depth > 64:
        return
    items = proc.items
    for i in range(proc.start, proc.start + proc.length):
        v = items[i]
        if type(v) is XName:
            d = I.where(v)
            if d is not None:
                w = d.d[v]
                if type(w) is Operator:
                    items[i] = w
        elif type(v) is PSArray and v.x and not v.readonly:
            _bind(I, v, depth + 1)
            v.readonly = True


@op()
def version(I):
    I.os.append(PSString.of(b"3010"))


@op()
def product(I):
    I.os.append(PSString.of(b"bookport"))


@op()
def revision(I):
    I.os.append(1)


@op()
def serialnumber(I):
    I.os.append(0)


@op()
def languagelevel(I):
    I.os.append(3)


@op()
def realtime(I):
    I.os.append(int(time.monotonic() * 1000) & 0x7FFFFFFF)


@op()
def usertime(I):
    I.os.append(int(time.process_time() * 1000) & 0x7FFFFFFF)


@op("echo")
def echo(I):
    I.os.pop()


@op("prompt", "flush", "executive")
def _nothing(I):
    pass


def write_out(I, data: bytes) -> None:
    if len(I.out) < 1_000_000:
        I.out += data


@op("print")
def print_(I):
    s = I.os.pop()
    if type(s) is not PSString:
        raise PSError("typecheck")
    write_out(I, s.bytes())


@op("=")
def eq_print(I):
    write_out(I, text_of(I.os.pop()) + b"\n")


def syntax_of(v, depth: int = 0) -> bytes:
    t = type(v)
    if t is PSString:
        return b"(" + v.bytes().replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"
    if t is Name:
        return b"/" + v.encode("latin-1", "replace")
    if t is PSArray:
        if depth > 8:
            return b"[...]"
        inner = b" ".join(syntax_of(x, depth + 1) for x in v.list()[:200])
        return (b"{" + inner + b"}") if v.x else (b"[" + inner + b"]")
    if t is PSDict:
        return b"-dict-"
    if t is Mark:
        return b"-mark-"
    if v is None:
        return b"null"
    if t is Operator:
        return b"--" + v.name.encode("latin-1", "replace") + b"--"
    if t is FontID:
        return b"-fontID-"
    if t is Save:
        return b"-save-"
    return text_of(v)


@op("==")
def eqeq(I):
    write_out(I, syntax_of(I.os.pop()) + b"\n")


@op("pstack")
def pstack(I):
    for v in reversed(I.os):
        write_out(I, syntax_of(v) + b"\n")


@op("stack")
def stack(I):
    for v in reversed(I.os):
        write_out(I, text_of(v) + b"\n")


@op()
def internaldict(I):
    pop_int(I)
    I.os.append(I.internal)


@op("handleerror")
def handleerror(I):
    pass


# ---- files

def _pop_file(I) -> F.PSFile:
    f = I.os.pop()
    if not isinstance(f, F.PSFile):
        raise PSError("typecheck")
    return f


@op("file")
def file_(I):
    I.os.pop()  # the access string: what's opened here is only ever read or written as it says
    name = I.os.pop()
    if type(name) is not PSString:
        raise PSError("typecheck")
    n = name.bytes()
    if n in (b"%stdout", b"%stderr"):
        I.os.append(F.PSFile(F.WriteStream(I.out, n.decode())))
    elif n in (b"%stdin", b"%lineedit", b"%statementedit"):
        I.os.append(F.PSFile(F.Stream(b"", n.decode())))
    else:
        raise PSError("invalidfileaccess", "bookport doesn't open files from PostScript")


@op()
def currentfile(I):
    f = I.currentfile()
    I.os.append(f.copy_attr(False) if f is not None else F.PSFile(F.Stream(b"", "%none")))


@op()
def closefile(I):
    f = _pop_file(I)
    f.stream.close()


@op()
def read(I):
    f = _pop_file(I)
    b = f.stream.readbyte()
    if b < 0:
        I.os.append(False)
    else:
        I.os.append(b)
        I.os.append(True)


@op()
def readstring(I):
    s = I.os.pop()
    f = _pop_file(I)
    if type(s) is not PSString:
        raise PSError("typecheck")
    data = f.stream.read(s.length)
    s.buf[s.start:s.start + len(data)] = data
    I.os.append(PSString(s.buf, s.start, len(data)))
    I.os.append(len(data) == s.length)


@op()
def readhexstring(I):
    s = I.os.pop()
    f = _pop_file(I)
    if type(s) is not PSString:
        raise PSError("typecheck")
    st = f.stream
    n = s.length
    out = bytearray()
    hexval = F._HEX_VAL
    pending = -1
    while len(out) < n:
        if st.pos >= len(st.buf) and not st._fill(1):
            break
        buf = st.buf
        i, end = st.pos, len(buf)
        while i < end and len(out) < n:
            v = hexval.get(buf[i])
            i += 1
            if v is None:
                continue
            if pending < 0:
                pending = v
            else:
                out.append((pending << 4) | v)
                pending = -1
        st.pos = i
    s.buf[s.start:s.start + len(out)] = out
    I.os.append(PSString(s.buf, s.start, len(out)))
    I.os.append(len(out) == n)


@op()
def readline(I):
    s = I.os.pop()
    f = _pop_file(I)
    st = f.stream
    out = bytearray()
    found = False
    while True:
        b = st.readbyte()
        if b < 0:
            break
        if b == 10:
            found = True
            break
        if b == 13:
            found = True
            nb = st.readbyte()
            if nb >= 0 and nb != 10:
                st.pos -= 1
            break
        out.append(b)
        if len(out) > s.length:
            raise PSError("rangecheck")
    s.buf[s.start:s.start + len(out)] = out
    I.os.append(PSString(s.buf, s.start, len(out)))
    I.os.append(found)  # false when the file ended before an end of line, even with a last line read (PLRM)


@op()
def bytesavailable(I):
    f = _pop_file(I)
    I.os.append(f.stream.available() if not f.stream.closed else -1)


@op()
def flushfile(I):
    f = _pop_file(I)
    if not isinstance(f.stream, F.WriteStream):
        f.stream.rest()


@op()
def resetfile(I):
    _pop_file(I)


@op()
def status(I):
    v = I.os.pop()
    if isinstance(v, F.PSFile):
        I.os.append(not v.stream.closed)
    else:
        I.os.append(False)


@op()
def fileposition(I):
    f = _pop_file(I)
    I.os.append(f.stream.pos)


@op()
def setfileposition(I):
    n = pop_int(I)
    f = _pop_file(I)
    if not 0 <= n <= len(f.stream.buf):
        raise PSError("ioerror")
    f.stream.pos = n


@op("write")
def write_(I):
    b = pop_int(I)
    f = _pop_file(I)
    if isinstance(f.stream, F.WriteStream):
        f.stream.write(bytes([b & 0xFF]))


@op()
def writestring(I):
    s = I.os.pop()
    f = _pop_file(I)
    if isinstance(f.stream, F.WriteStream) and type(s) is PSString:
        f.stream.write(s.bytes())


@op()
def writehexstring(I):
    s = I.os.pop()
    f = _pop_file(I)
    if isinstance(f.stream, F.WriteStream) and type(s) is PSString:
        f.stream.write(s.bytes().hex().encode())


@op("deletefile")
def deletefile(I):
    I.os.pop()
    raise PSError("invalidfileaccess")


@op()
def renamefile(I):
    I.os.pop()
    I.os.pop()
    raise PSError("invalidfileaccess")


@op()
def filenameforall(I):
    del I.os[-3:]


@op("run")
def run_(I):
    I.os.pop()
    raise PSError("invalidfileaccess", "bookport doesn't run other files")


def _source(I, src) -> F.Stream:
    if isinstance(src, F.PSFile):
        return src.stream
    if type(src) is PSString:
        return F.Stream(src.bytes(), "string")
    if type(src) is PSArray:
        return ProcStream(I, src)
    raise PSError("typecheck")


class ProcStream(F.Stream):
    """A procedure as a data source: each call gives the next string (an empty one ends it)."""

    def __init__(self, I, proc):
        super().__init__(b"", "procedure")
        self.I, self.proc, self.done = I, proc, False
        self.buf = bytearray()

    def more(self) -> bool:
        if self.done:
            return False
        self.I.call(self.proc)
        s = self.I.os.pop()
        if type(s) is not PSString:
            raise PSError("typecheck")
        if not s.length:
            self.done = True
            return False
        self.buf += s.bytes()
        return True


@op("filter")
def filter_(I):
    name = I.os.pop()
    if type(name) not in (Name, XName):
        raise PSError("typecheck")
    name = str(name)
    parms = {}
    if name.endswith("Encode"):
        target = I.os.pop()
        if I.os and type(I.os[-1]) is PSDict:
            I.os.pop()
        I.os.append(target if isinstance(target, F.PSFile) else F.PSFile(F.WriteStream(bytearray(), name)))
        return
    if name == "SubFileDecode":  # source [dict] count string /SubFileDecode filter
        eod = I.os.pop()
        cnt = pop_int(I)
        if type(eod) is not PSString:
            raise PSError("typecheck")
        if type(I.os[-1]) is PSDict:
            I.os.pop()
        I.os.append(F.PSFile(F.SubFile(_source(I, I.os.pop()), cnt, eod.bytes())))
        return
    if type(I.os[-1]) is PSDict:  # source dict /Name filter (a data source is never a dictionary)
        parms = {str(k): v for k, v in I.os.pop().d.items()}
    src = _source(I, I.os.pop())
    if name in F.DECODERS:
        st = F.Decoded(src, F.DECODERS[name], name)
    elif name in ("LZWDecode", "LZW"):
        early = parms.get("EarlyChange", 1)
        st = F.Decoded(src, lambda s: F._lzw(s, early), name)
    elif name in ("DCTDecode", "DCT"):
        st = F.DCTStream(src)
    elif name == "eexecDecode":
        st = F.Eexec(src)
    elif name in ("ReusableStreamDecode", "NullEncode"):
        st = F.Decoded(src, lambda s: s.rest(), name)
    elif name in ("CCITTFaxDecode", "CCF"):
        st = CCITTStream(src, parms)
    else:
        raise PSError("undefined", f"filter {name}")
    if parms.get("Predictor", 1) > 1 and name in ("FlateDecode", "Fl", "LZWDecode", "LZW"):
        inner = st
        st = F.Decoded(inner, lambda s: F.predict(s.rest(), parms), name)
    I.os.append(F.PSFile(st))


class CCITTStream(F.Decoded):
    """CCITTFaxDecode: the encoded bytes are kept (``fax``) so a picture can go into the PDF as it is; this needs
    the data to end where its source does (inside an ASCII85 or SubFileDecode filter, as programs write it)."""

    def __init__(self, source, parms):
        super().__init__(source, self._pixels, "CCITTFaxDecode")
        self.parms = parms
        self.fax: bytes | None = None
        self.taken = False

    def grab(self) -> bytes:
        if self.fax is None:
            if type(self.source) is F.Stream:
                raise PSError("ioerror", "fax pictures straight in the program aren't read")
            self.fax = self.source.rest()
        return self.fax

    def _pixels(self, _src) -> bytes:
        raise PSError("ioerror", "fax pictures are only placed, not read")


@op("eexec")
def eexec(I):
    src = I.os.pop()
    st = F.Eexec(_source(I, src))
    f = F.PSFile(st, True)
    I.begin(I.systemdict)

    def done():
        if len(I.ds) > 3 and I.ds[-1] is I.systemdict:
            I.end()
    I.es.append(Callback(done))
    I.es.append(FileRun(f))


# ---- resources (PLRM 3.9)

CATEGORIES = ("Font", "CIDFont", "CMap", "FontSet", "Encoding", "Form", "Pattern", "ProcSet", "ColorSpace",
              "Halftone", "ColorRendering", "IdiomSet", "InkParams", "TrapParams", "OutputDevice", "ControlLanguage",
              "Localization", "PDL", "HWOptions", "Filter", "ColorSpaceFamily", "Emulator", "IODevice",
              "ColorRenderingType", "FMapType", "FontType", "FormType", "HalftoneType", "ImageType", "PatternType",
              "FunctionType", "ShadingType", "CIDSystemInfo", "Category", "Generic")


def resource_dict(I, cat: str) -> dict:
    return I.resources.setdefault(cat, {})


@op()
def defineresource(I):
    cat = str(I.os.pop())
    inst = I.os.pop()
    k = key(I.os.pop())
    if cat == "Font":
        I.os.append(k)
        I.os.append(inst)
        I.lookup(Name("definefont")).fn(I)
        return
    resource_dict(I, cat)[k] = inst
    I.os.append(inst)


@op()
def undefineresource(I):
    cat = str(I.os.pop())
    k = key(I.os.pop())
    resource_dict(I, cat).pop(k, None)
    if cat == "Font":
        I.fonts.d.pop(k, None)


@op()
def findresource(I):
    cat = str(I.os.pop())
    k = key(I.os.pop())
    if cat == "Font":
        I.os.append(k)
        I.lookup(Name("findfont")).fn(I)
        return
    d = resource_dict(I, cat)
    if k in d:
        I.os.append(d[k])
        return
    if cat == "Category" and str(k) in CATEGORIES:
        I.os.append(PSDict(name=str(k)))
        return
    if cat == "ProcSet":
        I.os.append(PSDict(name=str(k)))
        return
    raise PSError("undefinedresource", f"{cat} {k}")


@op()
def resourcestatus(I):
    cat = str(I.os.pop())
    k = key(I.os.pop())
    have = k in resource_dict(I, cat) or (cat == "Font" and (k in I.fonts.d or I.known_font(k))) or \
        (cat == "Category" and str(k) in CATEGORIES)
    if have:
        I.os.extend([1, 0, True])
    else:
        I.os.append(False)


@op()
def resourceforall(I):
    cat = str(I.os.pop())
    scratch = I.os.pop()
    proc = pop_proc(I)
    template = I.os.pop()
    names = list(resource_dict(I, cat)) + (list(I.fonts.d) if cat == "Font" else [])
    pat = template.text() if type(template) is PSString else "*"
    import fnmatch
    vals = []
    for n in names:
        if fnmatch.fnmatchcase(str(n), pat):
            b = str(n).encode("latin-1", "replace")[:scratch.length]
            vals.append(PSString.of(b))
    f = ForallLoop()
    f.values, f.i, f.proc, f.pairs = vals, 0, proc, False
    I.es.append(f)


@op()
def findencoding(I):
    I.os.append(Name("Encoding"))
    findresource(I)


def register(I: Interp) -> None:
    for name, fn in OPS.items():
        I.define(name, fn)
