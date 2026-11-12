"""The interpreter (PLRM 3.5): operand, dictionary and execution stacks and the loop that runs them.

Execution-stack frames: a procedure being run (Proc), a file or string being scanned (FileRun), the loop operators'
own frames (each knows its next step), stopped contexts, callbacks (eexec's ``end``) and sentinels, which mark
where a nested ``call`` (a BuildGlyph, an image's data procedure) returns to the Python code that made it."""

from __future__ import annotations

import os

from .. import limits
from ..errors import BookportError
from .files import PSFile, Stream
from .objects import GLOBAL, MARK, Name, Operator, PSArray, PSDict, PSError, PSString, XName, set_vm_stamp
from .scan import EOF, token

MAX_STEPS = 200_000_000  # objects run for one file (a few minutes of real work at most)
MAX_OSTACK = 1_000_000
MAX_ESTACK = 50_000
MAX_DSTACK = 10_000
MAX_CALLS = 64  # nested calls back into PostScript (glyphs drawing glyphs...)
MAX_ALIASES = 10_000  # a name standing for a name standing for a name... (/a /a cvx def a would never end)
DEBUG = bool(os.environ.get("BOOKPORT_PS_DEBUG"))  # Python errors in operators raised as they are
TRACE = bool(os.environ.get("BOOKPORT_PS_TRACE"))  # PostScript errors printed where they happen


class JobError(BookportError):
    """The PostScript program stopped with an error nothing caught."""

    def __init__(self, name: str, command: str, detail: str = ""):
        super().__init__(f"The PostScript program stopped with an error ({name} in {command})"
                         + (f": {detail}" if detail else ""))
        self.name, self.command, self.detail = name, command, detail


class Quit(Exception):
    """quit: the job ends normally."""


class _Unwind(Exception):
    """A stop going past a nested call: the Python code that made the call gives up, and the stop goes on."""


class Proc:
    __slots__ = ("end", "i", "items")

    def __init__(self, items: list, i: int, end: int):
        self.items, self.i, self.end = items, i, end


class FileRun:
    """A file being run, or an executable string (string: currentfile looks past those, PLRM 8.2)."""
    __slots__ = ("file", "string")

    def __init__(self, f: PSFile, string: bool = False):
        self.file = f
        self.string = string


class Loop:
    """The frames of for, repeat, loop and forall (exit pops up to one of these)."""
    __slots__ = ()


class Stopped:
    __slots__ = ()

    def step(self, interp: Interp) -> None:
        interp.es.pop()
        interp.os.append(False)


class Callback:
    __slots__ = ("fn",)

    def __init__(self, fn):
        self.fn = fn

    def step(self, interp: Interp) -> None:
        interp.es.pop()
        self.fn()


class Sentinel:
    __slots__ = ()

    def step(self, interp: Interp) -> None:  # never run: run() stops on reaching it
        interp.es.pop()


_MISSING = object()


class Interp:
    def __init__(self):
        set_vm_stamp(0)  # first: what this interpreter makes is local VM outside any save (whatever ran before)
        self.os: list = []
        self.es: list = []
        self.ds: list[PSDict] = []
        self.dd: list[dict] = []
        self.out = bytearray()  # what the program prints
        self.steps = 0
        self.calls = 0
        self.systemdict = PSDict(name="systemdict")
        self.globaldict = PSDict(name="globaldict")
        self.userdict = PSDict(name="userdict")
        self.errordict = PSDict(name="errordict")
        self.dollar_error = PSDict(name="$error")
        self.statusdict = PSDict(name="statusdict")
        self.internal = PSDict(name="internaldict")
        self.fonts = PSDict(name="FontDirectory")
        self.resources: dict[str, dict] = {}
        self.global_mode = False
        self.packing = False
        self.save_level = 0
        self.saves: list = []  # the open saves, outermost first
        self.vm_serial = 0
        self.seed = 0  # rand's
        self.notes: list[str] = []
        sd = self.systemdict.d
        for n, v in (("systemdict", self.systemdict), ("globaldict", self.globaldict), ("userdict", self.userdict),
                     ("errordict", self.errordict), ("$error", self.dollar_error), ("statusdict", self.statusdict),
                     ("FontDirectory", self.fonts), ("GlobalFontDirectory", self.fonts)):
            sd[Name(n)] = v
        self.dollar_error.d.update({Name("newerror"): False, Name("errorname"): None, Name("command"): None,
                                    Name("ostack"): None, Name("estack"): None, Name("dstack"): None,
                                    Name("recordstacks"): False, Name("binary"): False})
        for d in (self.systemdict, self.globaldict, self.userdict):
            self.ds.append(d)
            self.dd.append(d.d)
        self.systemdict.vm = self.globaldict.vm = GLOBAL

    # ---- changes to local VM, kept for restore (PLRM 3.7.3)
    def stamp(self) -> None:
        """Say which VM new objects go to (after setglobal, save or restore)."""
        set_vm_stamp(GLOBAL if self.global_mode else (self.saves[-1].serial if self.saves else 0))

    def dput(self, c: PSDict, k, v) -> None:
        """c[k] = v, noting the old value for restore when c is local and older than the innermost save."""
        saves = self.saves
        if saves and GLOBAL < c.vm < saves[-1].serial:
            d = c.d
            j = saves[-1].journal
            jk = (id(d), k)
            if jk not in j:
                j[jk] = (d, k, d.get(k, _MISSING))
        c.d[k] = v

    def dremove(self, c: PSDict, k) -> None:
        saves = self.saves
        if saves and GLOBAL < c.vm < saves[-1].serial and k in c.d:
            d = c.d
            j = saves[-1].journal
            jk = (id(d), k)
            if jk not in j:
                j[jk] = (d, k, d[k])
        c.d.pop(k, None)

    def aput(self, a: PSArray, i: int, vals: list) -> None:
        """a[i:i+len(vals)] = vals (within a's window), noting old values for restore as dput does."""
        items = a.items
        at = a.start + i
        saves = self.saves
        if saves and GLOBAL < a.vm < saves[-1].serial:
            j = saves[-1].journal
            base = id(items)
            for n in range(at, at + len(vals)):
                jk = (base, n)
                if jk not in j:
                    j[jk] = (items, n, items[n])
        items[at:at + len(vals)] = vals

    def undo_to(self, s) -> None:
        """restore: put back what changed in local VM since save s, closing s and every save inside it."""
        while self.saves:
            t = self.saves.pop()
            for c, k, old in t.journal.values():
                if old is _MISSING:
                    c.pop(k, None)
                else:
                    c[k] = old
            t.journal = {}
            t.valid = False
            if t is s:
                break
        self.save_level = len(self.saves)
        self.stamp()

    # ---- the dictionary stack
    def define(self, name: str, fn) -> None:
        self.systemdict.d[Name(name)] = Operator(name, fn)

    def lookup(self, name):
        for d in reversed(self.dd):
            v = d.get(name, _MISSING)
            if v is not _MISSING:
                return v
        raise PSError("undefined", str(name))

    def where(self, name) -> PSDict | None:
        for d in reversed(self.ds):
            if name in d.d:
                return d
        return None

    def begin(self, d: PSDict) -> None:
        if len(self.ds) >= MAX_DSTACK:
            raise PSError("dictstackoverflow")
        self.ds.append(d)
        self.dd.append(d.d)

    def end(self) -> None:
        if len(self.ds) <= 3:
            raise PSError("dictstackunderflow")
        self.ds.pop()
        self.dd.pop()

    # ---- running
    def push_exec(self, obj) -> None:
        """Run obj next (exec)."""
        t = type(obj)
        es = self.es
        if len(es) >= MAX_ESTACK:
            raise PSError("execstackoverflow")
        if t is PSArray and obj.x:
            if obj.length:
                es.append(Proc(obj.items, obj.start, obj.start + obj.length))
        elif t is XName or t is Operator:
            es.append(Proc([obj], 0, 1))
        elif t is PSString and obj.x:
            es.append(FileRun(PSFile(Stream(obj.bytes(), "string"), True), True))
        elif t is PSFile and obj.x:
            es.append(FileRun(obj))
        else:
            self.os.append(obj)

    def run(self, base: int = 0) -> None:
        """Run until the execution stack is back to ``base`` frames."""
        es, os_, lookup, dd = self.es, self.os, self.lookup, self.dd
        missing = _MISSING
        obj = None
        while len(es) > base:
            try:
                while len(es) > base:
                    f = es[-1]
                    ft = type(f)
                    if ft is Proc:
                        i = f.i
                        if i >= f.end:
                            es.pop()
                            continue
                        obj = f.items[i]
                        f.i = i + 1
                        if i + 1 >= f.end:  # its last object: the frame goes now (a tail call reuses it below)
                            es.pop()
                            spare = f
                        else:
                            spare = None
                    elif ft is FileRun:
                        spare = None
                        obj = token(f.file.stream, lookup)
                        if obj is EOF:
                            es.pop()
                            continue
                    else:  # a loop's next turn (counted too: an empty loop body still takes time)
                        self.steps += 1
                        if self.steps > MAX_STEPS:
                            raise limits.LimitError("This PostScript program runs too long, so bookport stopped it")
                        f.step(self)
                        if len(os_) > MAX_OSTACK:  # for's counter, with an empty body
                            raise PSError("stackoverflow")
                        continue
                    self.steps += 1
                    if self.steps > MAX_STEPS:
                        raise limits.LimitError("This PostScript program runs too long, so bookport stopped it")
                    t = type(obj)
                    if t is XName:
                        k = len(dd) - 1  # lookup(), inline (and making no iterator): most of what runs is a name
                        while k >= 0:
                            v = dd[k].get(obj, missing)
                            if v is not missing:
                                break
                            k -= 1
                        else:
                            raise PSError("undefined", str(obj))
                        tv = type(v)
                        if tv is XName:  # a name standing for another executable name
                            hops = 0
                            while tv is XName:
                                hops += 1
                                if hops > MAX_ALIASES:
                                    raise PSError("limitcheck", f"{obj} stands for itself")
                                v = lookup(v)
                                tv = type(v)
                            self.steps += hops
                        if tv is Operator:
                            v.fn(self)
                            if len(os_) > MAX_OSTACK:  # dup, copy, aload... (literals are checked below)
                                raise PSError("stackoverflow")
                        elif tv is PSArray:
                            if v.x:
                                if v.length:
                                    if len(es) >= MAX_ESTACK:
                                        raise PSError("execstackoverflow")
                                    if spare is not None:  # nothing else holds the frame just finished
                                        spare.items, spare.i, spare.end = v.items, v.start, v.start + v.length
                                        es.append(spare)
                                    else:
                                        es.append(Proc(v.items, v.start, v.start + v.length))
                            else:
                                os_.append(v)
                        elif (tv is PSString or tv is PSFile) and v.x:
                            self.push_exec(v)
                        else:
                            os_.append(v)
                    elif t is Operator:
                        obj.fn(self)
                        if len(os_) > MAX_OSTACK:
                            raise PSError("stackoverflow")
                    elif t is PSString and obj.x:
                        self.push_exec(obj)
                    else:
                        os_.append(obj)
                        if len(os_) > MAX_OSTACK:
                            raise PSError("stackoverflow")
            except PSError as e:
                self.error(e, obj, base)
            except (IndexError, TypeError, ValueError, ZeroDivisionError, OverflowError, KeyError,
                    AttributeError) as e:
                if DEBUG:
                    raise
                name = {IndexError: "stackunderflow", ZeroDivisionError: "undefinedresult",
                        OverflowError: "undefinedresult", ValueError: "rangecheck", KeyError: "undefined"}.get(
                    type(e), "typecheck")
                self.error(PSError(name, f"{type(e).__name__}: {e}"), obj, base)
            except _Unwind:
                self.stop(base)

    def call(self, proc) -> None:
        """Run proc now, from inside an operator, and come back."""
        if self.calls >= MAX_CALLS:
            raise PSError("limitcheck", "calls nested too deep")
        mark = Sentinel()
        self.es.append(mark)
        depth = len(self.es)
        self.calls += 1
        try:
            self.push_exec(proc)
            self.run(depth)
        finally:
            self.calls -= 1
        if self.es and self.es[-1] is mark:
            self.es.pop()

    def error(self, e: PSError, command, base: int) -> None:
        cmd = command if not isinstance(command, (PSArray, PSDict)) else None
        self.dollar_error.d.update({Name("newerror"): True, Name("errorname"): Name(e.name),
                                    Name("command"): cmd})
        self.last_error = (e.name, _describe(command), e.detail)
        if TRACE:
            f = self.currentfile()
            st = f.stream if f is not None else None
            where = st.buf[max(0, st.pos - 160):st.pos + 40] if st is not None else b""
            print(f"PS error {e.name} in {_describe(command)} ({e.detail}) at {st.pos if st else '-'}:\n"
                  f"  ...{bytes(where)!r}\n  stack top: {[repr(x)[:60] for x in self.os[-6:]]}")
        handler = self.errordict.d.get(e.name)
        if type(handler) is PSArray and handler.x:
            self.os.append(command)
            self.push_exec(handler)
            return
        self.stop(base)

    def stop(self, base: int) -> None:
        es = self.es
        while len(es) > base:
            f = es.pop()
            if type(f) is Stopped:
                self.os.append(True)
                return
        if base == 0:
            name, cmd, detail = getattr(self, "last_error", ("stop", "stop", ""))
            raise JobError(name, cmd, detail)
        if es and type(es[-1]) is Sentinel:
            es.pop()
        raise _Unwind

    def currentfile(self) -> PSFile | None:
        for f in reversed(self.es):
            if type(f) is FileRun and not f.string:
                return f.file
        return None


def _describe(obj) -> str:
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, Operator):
        return obj.name
    return type(obj).__name__


def pop_num(I: Interp):
    v = I.os.pop()
    if type(v) is int or type(v) is float:
        return v
    I.os.append(v)
    raise PSError("typecheck")


def pop_int(I: Interp) -> int:
    v = I.os.pop()
    if type(v) is int:
        return v
    I.os.append(v)
    raise PSError("typecheck")


def pop_bool(I: Interp) -> bool:
    v = I.os.pop()
    if type(v) is bool:
        return v
    I.os.append(v)
    raise PSError("typecheck")


def pop_type(I: Interp, t):
    v = I.os.pop()
    if type(v) is t:
        return v
    I.os.append(v)
    raise PSError("typecheck")


def pop_proc(I: Interp) -> PSArray:
    v = I.os.pop()
    if type(v) is PSArray:
        return v
    I.os.append(v)
    raise PSError("typecheck")


def count_to_mark(os_: list) -> int:
    for i in range(len(os_) - 1, -1, -1):
        if os_[i] is MARK:
            return len(os_) - 1 - i
    raise PSError("unmatchedmark")
