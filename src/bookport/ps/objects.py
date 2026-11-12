"""PostScript objects (PLRM 3.3): Python numbers, booleans and None (null) stand for themselves; names, strings,
arrays and dictionaries are the classes here, so they can carry the executable attribute and share storage the
way PostScript does (getinterval, dup)."""

from __future__ import annotations

import threading

# Which VM a new array or dictionary is made in (PLRM 3.7): GLOBAL, or the serial number of the innermost save
# open when it was made (0: none). restore undoes changes only to local objects older than the save it goes back to.
# Per thread, as each conversion runs its own interpreter.
GLOBAL = -1
_vm = threading.local()


def vm_stamp() -> int:
    return getattr(_vm, "stamp", 0)


def set_vm_stamp(stamp: int) -> None:
    _vm.stamp = stamp


class PSError(Exception):
    """A PostScript error, by its PLRM name (typecheck, undefined, rangecheck...)."""

    def __init__(self, name: str, detail: str = ""):
        super().__init__(name)
        self.name = name
        self.detail = detail


class Name(str):
    """A literal name (/like this)."""
    __slots__ = ()


class XName(str):
    """An executable name (like this)."""
    __slots__ = ()


class Mark:
    __slots__ = ()

    def __repr__(self):
        return "-mark-"


MARK = Mark()


class PSString:
    """A string: a window (start, length) on a shared bytearray, so getinterval and putinterval share storage."""
    __slots__ = ("buf", "length", "readonly", "start", "x")

    def __init__(self, buf: bytearray, start: int = 0, length: int | None = None, x: bool = False):
        self.buf = buf
        self.start = start
        self.length = len(buf) - start if length is None else length
        self.x = x
        self.readonly = False

    @classmethod
    def of(cls, data: bytes, x: bool = False) -> PSString:
        return cls(bytearray(data), 0, len(data), x)

    def bytes(self) -> bytes:
        if self.start == 0 and self.length == len(self.buf):
            return bytes(self.buf)
        return bytes(self.buf[self.start:self.start + self.length])

    def text(self) -> str:
        return self.bytes().decode("latin-1")

    def __repr__(self):
        return f"PSString({self.bytes()[:40]!r})"


class PSArray:
    """An array or procedure (x): a window on a shared list. vm: where the list was made (see GLOBAL); a window
    on an existing list (getinterval) passes the list's own."""
    __slots__ = ("items", "length", "readonly", "start", "vm", "x")

    def __init__(self, items: list, start: int = 0, length: int | None = None, x: bool = False, vm: int | None = None):
        self.items = items
        self.start = start
        self.length = len(items) - start if length is None else length
        self.x = x
        self.readonly = False
        self.vm = getattr(_vm, "stamp", 0) if vm is None else vm

    def list(self) -> list:
        if self.start == 0 and self.length == len(self.items):
            return self.items
        return self.items[self.start:self.start + self.length]

    def __repr__(self):
        return f"PSArray({'x ' if self.x else ''}{self.length})"


class PSDict:
    __slots__ = ("d", "maxlength", "name", "readonly", "vm")

    def __init__(self, d: dict | None = None, maxlength: int = 0, name: str = ""):
        self.d = {} if d is None else d
        self.maxlength = maxlength
        self.readonly = False
        self.name = name
        self.vm = getattr(_vm, "stamp", 0)

    def __repr__(self):
        return f"PSDict({self.name or len(self.d)})"


class Operator:
    __slots__ = ("fn", "name")

    def __init__(self, name: str, fn):
        self.name = name
        self.fn = fn

    def __repr__(self):
        return f"--{self.name}--"


class FontID:
    """A font's FID: its copies (scalefont, makefont) share it, and with it what the font is (fonts.Program)."""
    __slots__ = ("n", "prog")
    _next = 0

    def __init__(self, prog=None):
        FontID._next += 1
        self.n = FontID._next
        self.prog = prog


class Save:
    """A save: the graphics state, and the old values of what local VM objects older than it have had changed since
    (journal: (id(container), key) -> (container, key, old value), the first change only), for restore to put back."""
    __slots__ = ("gstate", "journal", "level", "serial", "valid")

    def __init__(self, gstate, level: int, serial: int = 0):
        self.gstate = gstate
        self.level = level
        self.serial = serial
        self.journal: dict = {}
        self.valid = True


class GStateObj:
    """A gstate object (Level 2 gstate / currentgstate / setgstate)."""
    __slots__ = ("gs",)

    def __init__(self, gs):
        self.gs = gs


def key(k):
    """A dictionary key: strings and names are the same key (as names)."""
    if isinstance(k, PSString):
        return Name(k.text())
    if type(k) is XName:
        return Name(k)
    return k


TYPE_NAMES = {
    bool: "booleantype", int: "integertype", float: "realtype", Name: "nametype", XName: "nametype",
    PSString: "stringtype", PSArray: "arraytype", PSDict: "dicttype", Operator: "operatortype",
    Mark: "marktype", type(None): "nulltype", FontID: "fonttype", Save: "savetype", GStateObj: "gstatetype",
}


def type_name(v) -> str:
    t = TYPE_NAMES.get(type(v))
    if t is not None:
        return t
    return "filetype" if hasattr(v, "readbyte") else "nulltype"


def is_number(v) -> bool:
    return (type(v) is int or type(v) is float)


def literal(v):
    """The object with the literal attribute (cvlit)."""
    if type(v) is XName:
        return Name(v)
    if type(v) in (PSArray, PSString):
        c = type(v).__new__(type(v))
        for s in type(v).__slots__:
            setattr(c, s, getattr(v, s))
        c.x = False
        return c
    if hasattr(v, "readbyte"):
        return v.copy_attr(False)
    return v


def executable(v):
    """The object with the executable attribute (cvx)."""
    if type(v) is Name:
        return XName(v)
    if type(v) in (PSArray, PSString):
        c = type(v).__new__(type(v))
        for s in type(v).__slots__:
            setattr(c, s, getattr(v, s))
        c.x = True
        return c
    if hasattr(v, "readbyte"):
        return v.copy_attr(True)
    return v


def is_exec(v) -> bool:
    t = type(v)
    if t is XName or t is Operator:
        return True
    if t is PSArray or t is PSString:
        return v.x
    if hasattr(v, "readbyte"):
        return v.x
    return False
