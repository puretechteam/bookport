"""The PostScript scanner (PLRM 3.2): bytes to objects. Procedures are built here (deferred execution); //name is
looked up as it's read. After a name or number one whitespace character is consumed (CR LF counts as one), so
``currentfile ... readstring`` starts on the data's first byte."""

from __future__ import annotations

import re

from .files import Stream, a85
from .objects import Name, PSArray, PSError, PSString, XName

EOF = object()

_SPACE = re.compile(rb"[\x00\t\n\x0c\r ]*")
_TOKEN = re.compile(
    rb"(?:"
    rb"(%[^\r\n]*)"  # 1 comment
    rb"|([^\x00\t\n\x0c\r ()<>\[\]{}/%]+)"  # 2 number or name
    rb"|(//?)([^\x00\t\n\x0c\r ()<>\[\]{}/%]*)"  # 3, 4 literal or immediate name
    rb"|(<<|>>|<~|[\[\]{}()<>]))"  # 5 delimiter
)
_INT = re.compile(rb"[+-]?\d+\Z")
_REAL = re.compile(rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?\Z")
_RADIX = re.compile(rb"(\d{1,2})#([0-9A-Za-z]+)\Z")
_NUMSTART = frozenset(b"+-.0123456789")
_STR_SPECIAL = re.compile(rb"[()\\\r]")
_ESC = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, ord("\\"): 92, ord("("): 40, ord(")"): 41}
_WS = b"\x00\t\n\x0c\r "
_NAMES: dict[bytes, XName] = {}
_LITS: dict[bytes, Name] = {}
MAX_NAME = 65535


def number(tok: bytes):
    if tok[0] in _NUMSTART:
        if _INT.match(tok):
            v = int(tok)
            return v if -2147483648 <= v <= 2147483647 else float(v)
        if _REAL.match(tok):
            return float(tok)
    m = _RADIX.match(tok)
    if m and 2 <= int(m.group(1)) <= 36:
        try:
            v = int(m.group(2), int(m.group(1)))
        except ValueError:
            return None
        v &= 0xFFFFFFFF
        return v - (1 << 32) if v >= 1 << 31 else v
    return None


def xname(tok: bytes) -> XName:
    n = _NAMES.get(tok)
    if n is None:
        if len(tok) > MAX_NAME:
            raise PSError("limitcheck", "name too long")
        n = XName(tok.decode("latin-1"))
        if len(_NAMES) < 200_000:
            _NAMES[tok] = n
    return n


def lname(tok: bytes) -> Name:
    n = _LITS.get(tok)
    if n is None:
        if len(tok) > MAX_NAME:
            raise PSError("limitcheck", "name too long")
        n = Name(tok.decode("latin-1"))
        if len(_LITS) < 200_000:
            _LITS[tok] = n
    return n


def _eat_space(st: Stream, i: int) -> int:
    buf = st.buf
    if i >= len(buf) and not st.more():
        return i
    buf = st.buf
    if i < len(buf) and buf[i] in _WS:
        if buf[i] == 13:
            if i + 1 >= len(buf):
                st.more()
                buf = st.buf
            if i + 1 < len(buf) and buf[i + 1] == 10:
                return i + 2
        return i + 1
    return i


def _until(st: Stream, marker: bytes, i: int) -> int:
    while True:
        j = st.buf.find(marker, i)
        if j >= 0:
            return j
        if not st.more():
            raise PSError("syntaxerror", "unterminated string")


def _literal(st: Stream, i: int) -> tuple[bytes, int]:
    out = bytearray()
    depth = 1
    while True:
        buf = st.buf
        m = _STR_SPECIAL.search(buf, i)
        if m is None:
            out += buf[i:]
            i = len(buf)
            if not st.more():
                raise PSError("syntaxerror", "unterminated string")
            continue
        j = m.start()
        out += buf[i:j]
        c = buf[j]
        i = j + 1
        if c == 40:
            depth += 1
            out.append(40)
        elif c == 41:
            depth -= 1
            if depth == 0:
                return bytes(out), i
            out.append(41)
        elif c == 13:  # CR or CR LF in a string is a newline
            if i >= len(buf) and st.more():
                buf = st.buf
            if i < len(buf) and buf[i] == 10:
                i += 1
            out.append(10)
        else:  # backslash
            if i >= len(buf) and not st.more():
                raise PSError("syntaxerror", "unterminated string")
            buf = st.buf
            e = buf[i]
            i += 1
            if e in _ESC:
                out.append(_ESC[e])
            elif 48 <= e <= 55:
                v = e - 48
                for _ in range(2):
                    if i >= len(buf) and st.more():
                        buf = st.buf
                    if i < len(buf) and 48 <= buf[i] <= 55:
                        v = v * 8 + buf[i] - 48
                        i += 1
                    else:
                        break
                out.append(v & 0xFF)
            elif e == 13:
                if i >= len(buf) and st.more():
                    buf = st.buf
                if i < len(buf) and buf[i] == 10:
                    i += 1
            elif e == 10:
                pass
            else:
                out.append(e)


def token(st: Stream, lookup=None):
    """The next object from the stream (EOF at its end). ``lookup(name)`` evaluates //name."""
    stack: list[list] = []
    while True:
        buf = st.buf
        st.pos = _SPACE.match(buf, st.pos).end()
        if st.pos >= len(buf):
            if st.closed or not st.more():
                if stack:
                    raise PSError("syntaxerror", "unterminated procedure")
                return EOF
            continue
        m = _TOKEN.match(buf, st.pos)
        if m is None:
            raise PSError("syntaxerror", f"unexpected byte {buf[st.pos]:#x}")
        if m.end() == len(buf) and (m.group(2) or m.group(3) or m.group(1)) and not st.closed and st.more():
            continue  # the token may go on in the next chunk
        g = m.lastindex
        if g == 1:
            st.pos = m.end()
            continue
        if g == 2:
            tok = m.group(2)
            v = number(tok)
            obj = xname(tok) if v is None else v
            st.pos = _eat_space(st, m.end())
        elif g == 4 or g == 3:
            slashes, tok = m.group(3), m.group(4)
            st.pos = _eat_space(st, m.end())
            if len(slashes) == 2:
                if lookup is None:
                    raise PSError("undefined", tok.decode("latin-1"))
                obj = lookup(xname(tok))
            else:
                obj = lname(tok)
        else:
            d = m.group(5)
            i = m.end()
            if d == b"(":
                data, i = _literal(st, i)
                obj = PSString.of(data)
                st.pos = i
            elif d == b"<":
                j = _until(st, b">", i)
                digits = bytes(st.buf[i:j]).translate(None, _WS)
                if len(digits) % 2:
                    digits += b"0"
                try:
                    obj = PSString.of(bytes.fromhex(digits.decode("ascii")))
                except ValueError:
                    raise PSError("syntaxerror", "bad hex string") from None
                st.pos = j + 1
            elif d == b"<~":
                j = _until(st, b"~>", i)
                obj = PSString.of(a85(st.buf[i:j]))
                st.pos = j + 2
            elif d == b"{":
                st.pos = i
                stack.append([])
                if len(stack) > 10_000:
                    raise PSError("limitcheck", "procedures nested too deep")
                continue
            elif d == b"}":
                st.pos = i
                if not stack:
                    raise PSError("syntaxerror", "unmatched }")
                obj = PSArray(stack.pop(), x=True)
            elif d in (b"[", b"]", b"<<", b">>"):
                obj = xname(d)
                st.pos = i
            else:
                raise PSError("syntaxerror", f"unexpected {d.decode('latin-1')}")
        if stack:
            stack[-1].append(obj)
            continue
        return obj
