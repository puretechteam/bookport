"""CCITT Group 4 (ITU-T T.6, "MMR") for bitonal rows: decoding Smmr mask chunks, and encoding
masks for PDF's CCITTFaxDecode filter (usually half the size of Flate on scanned text).

Rows are ints with bit c set for a black pixel in column c, top row first.
"""

from __future__ import annotations

import struct
from bisect import bisect_right

from ..errors import BookportError

_WHITE = """00110101 000111 0111 1000 1011 1100 1110 1111 10011 10100 00111 01000 001000 000011 110100 110101
101010 101011 0100111 0001100 0001000 0010111 0000011 0000100 0101000 0101011 0010011 0100100 0011000 00000010
00000011 00011010 00011011 00010010 00010011 00010100 00010101 00010110 00010111 00101000 00101001 00101010
00101011 00101100 00101101 00000100 00000101 00001010 00001011 01010010 01010011 01010100 01010101 00100100
00100101 01011000 01011001 01011010 01011011 01001010 01001011 00110010 00110011 00110100"""
_WHITE_MAKEUP = """11011 10010 010111 0110111 00110110 00110111 01100100 01100101 01101000 01100111 011001100 011001101
011010010 011010011 011010100 011010101 011010110 011010111 011011000 011011001 011011010 011011011 010011000
010011001 010011010 011000 010011011"""
_BLACK = """0000110111 010 11 10 011 0011 0010 00011 000101 000100 0000100 0000101 0000111 00000100 00000111 000011000
0000010111 0000011000 0000001000 00001100111 00001101000 00001101100 00000110111 00000101000 00000010111
00000011000 000011001010 000011001011 000011001100 000011001101 000001101000 000001101001 000001101010
000001101011 000011010010 000011010011 000011010100 000011010101 000011010110 000011010111 000001101100
000001101101 000011011010 000011011011 000001010100 000001010101 000001010110 000001010111 000001100100
000001100101 000001010010 000001010011 000000100100 000000110111 000000111000 000000100111 000000101000
000001011000 000001011001 000000101011 000000101100 000001011010 000001100110 000001100111"""
_BLACK_MAKEUP = """0000001111 000011001000 000011001001 000001011011 000000110011 000000110100 000000110101 0000001101100
0000001101101 0000001001010 0000001001011 0000001001100 0000001001101 0000001110010 0000001110011 0000001110100
0000001110101 0000001110110 0000001110111 0000001010010 0000001010011 0000001010100 0000001010101 0000001011010
0000001011011 0000001100100 0000001100101"""
_EXT_MAKEUP = """00000001000 00000001100 00000001101 000000010010 000000010011 000000010100 000000010101 000000010110
000000010111 000000011100 000000011101 000000011110 000000011111"""


def _table(term: str, makeup: str) -> dict[int, tuple[int, int]]:
    """run length -> (code, bits)"""
    out = {}
    for n, c in enumerate(term.split()):
        out[n] = (int(c, 2), len(c))
    for n, c in enumerate(makeup.split()):
        out[64 * (n + 1)] = (int(c, 2), len(c))
    for n, c in enumerate(_EXT_MAKEUP.split()):
        out[1792 + 64 * n] = (int(c, 2), len(c))
    return out


RUNS = (_table(_WHITE, _WHITE_MAKEUP), _table(_BLACK, _BLACK_MAKEUP))  # [colour][length]
DECODE = tuple({(bits, code): n for n, (code, bits) in t.items()} for t in RUNS)
# vertical modes: a1 - b1 -> (code, bits)
VERTICAL = {0: (1, 1), 1: (3, 3), -1: (2, 3), 2: (3, 6), -2: (2, 6), 3: (3, 7), -3: (2, 7)}
PASS = (1, 4)
HORIZONTAL = (1, 3)
MAX_WIDTH = 0xFFFF


def changes(row: int, width: int) -> list[int]:
    """Columns where the colour changes from the column before (white before the first)."""
    d = (row ^ (row << 1)) & ((1 << width) - 1)
    out = []
    while d:
        low = d & -d
        out.append(low.bit_length() - 1)
        d ^= low
    return out


class _Bits:
    __slots__ = ("acc", "n", "out")

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, code: int, bits: int) -> None:
        self.acc = (self.acc << bits) | code
        self.n += bits
        if self.n >= 32:
            extra = self.n & 7
            self.out += (self.acc >> extra).to_bytes((self.n - extra) // 8, "big")
            self.acc &= (1 << extra) - 1
            self.n = extra

    def run(self, color: int, n: int) -> None:
        table = RUNS[color]
        while n >= 2560:
            self.put(*table[2560])
            n -= 2560
        if n >= 64:
            self.put(*table[n & ~63])
            n &= 63
        self.put(*table[n])

    def done(self) -> bytes:
        if self.n:
            self.out += (self.acc << (8 - self.n % 8 if self.n % 8 else 0)).to_bytes((self.n + 7) // 8, "big")
        return bytes(self.out)


def encode_packed(data: bytes, width: int, height: int) -> bytes:
    """Group 4 data for packed rows (column 0 in the top bit, 1 = black)."""
    from ._accel import native
    if native is not None:
        return native.g4_encode(data, width, height)
    from .jb2 import unpacked
    return encode(unpacked(data, width, height), width)


def encode(rows: list[int], width: int) -> bytes:
    """Group 4 data for rows (bit c = black pixel in column c), with an end-of-block code."""
    out = _Bits()
    put, run = out.put, out.run
    ref: list[int] = []
    for row in rows:
        cur = changes(row, width) if row else []
        a0 = -1
        col = 0
        j = 0  # index in cur of the first change after a0
        ncur = len(cur)
        nref = len(ref)
        while True:
            a1 = cur[j] if j < ncur else width
            k = bisect_right(ref, a0)
            if (k & 1) != col:
                k += 1
            b1 = ref[k] if k < nref else width
            b2 = ref[k + 1] if k + 1 < nref else width
            if b2 < a1:
                put(*PASS)
                a0 = b2
                continue
            d = a1 - b1
            if -3 <= d <= 3:
                put(*VERTICAL[d])
                a0 = a1
                col ^= 1
                j += 1
            else:
                a2 = cur[j + 1] if j + 1 < ncur else width
                put(*HORIZONTAL)
                run(col, a1 - max(a0, 0))
                run(col ^ 1, a2 - a1)
                a0 = a2
                j += 2
            if a0 >= width:
                break
        ref = cur
    put(1, 12)
    put(1, 12)
    return out.done()


class _Reader:
    __slots__ = ("data", "end", "pos")

    def __init__(self, data: bytes, start: int = 0, end: int | None = None):
        self.data = data
        self.pos = start * 8
        self.end = (len(data) if end is None else end) * 8

    def bit(self) -> int:
        p = self.pos
        if p >= self.end:
            raise BookportError("Damaged DjVu file: MMR mask data ends too soon")
        self.pos = p + 1
        return (self.data[p >> 3] >> (7 - (p & 7))) & 1

    def mode(self) -> str | int:
        code = 0
        for n in range(1, 8):
            code = (code << 1) | self.bit()
            if n == 1 and code == 1:
                return 0
            if n == 3:
                if code == 1:
                    return "h"
                if code in (2, 3):
                    return -1 if code == 2 else 1
            if n == 4 and code == 1:
                return "p"
            if n == 6 and code in (2, 3):
                return -2 if code == 2 else 2
            if n == 7 and code in (2, 3):
                return -3 if code == 2 else 3
        raise BookportError("Damaged DjVu file: MMR mask data has an unknown code")

    def run(self, color: int) -> int:
        table = DECODE[color]
        total = 0
        while True:
            code = 0
            for n in range(1, 14):
                code = (code << 1) | self.bit()
                got = table.get((n, code))
                if got is not None:
                    break
            else:
                raise BookportError("Damaged DjVu file: MMR mask data has an unknown run")
            total += got
            if got < 64:
                return total
            if total > MAX_WIDTH * 2:
                raise BookportError("Damaged DjVu file: an MMR run is too long")


def decode(data: bytes, width: int, height: int, start: int = 0, end: int | None = None) -> list[int]:
    """Rows of Group 4 data (rows past the data's end are left white)."""
    r = _Reader(data, start, end)
    rows: list[int] = []
    ref: list[int] = []
    for _ in range(height):
        cur: list[int] = []
        a0 = -1
        col = 0
        try:
            while a0 < width:
                k = bisect_right(ref, a0)
                if (k & 1) != col:
                    k += 1
                b1 = ref[k] if k < len(ref) else width
                b2 = ref[k + 1] if k + 1 < len(ref) else width
                m = r.mode()
                if m == "p":
                    a0 = b2
                elif m == "h":
                    a1 = min(width, max(a0, 0) + r.run(col))
                    a2 = min(width, a1 + r.run(col ^ 1))
                    cur += [a1, a2]
                    a0 = a2
                else:
                    a1 = b1 + m
                    if a1 < 0 or a1 > width or (cur and a1 < cur[-1]):
                        raise BookportError("Damaged DjVu file: MMR mask data points outside its row")
                    cur.append(a1)
                    a0 = a1
                    col ^= 1
        except BookportError:
            if not rows and not cur:
                raise
            rows.append(_row(cur, width))
            break
        rows.append(_row(cur, width))
        ref = [c for c in cur if c < width]
    rows += [0] * (height - len(rows))
    return rows


def _row(cur: list[int], width: int) -> int:
    """Changing elements back to a row int."""
    row = 0
    for i in range(0, len(cur), 2):
        a = min(cur[i], width)
        b = min(cur[i + 1], width) if i + 1 < len(cur) else width
        if b > a:
            row |= ((1 << (b - a)) - 1) << a
    return row


def decode_chunk(data: bytes) -> tuple[int, int, list[int]]:
    """An Smmr chunk: (width, height, rows)."""
    if len(data) < 8 or data[:3] != b"MMR":
        raise BookportError("Damaged DjVu file: an MMR mask has no header")
    flags = data[3]
    w, h = struct.unpack_from(">HH", data, 4)
    if not w or not h:
        raise BookportError("Damaged DjVu file: an MMR mask has no size")
    from .. import limits
    limits.check_pixels(w, h)
    if flags & 2:  # in stripes, each coded on its own
        if len(data) < 10:
            raise BookportError("Damaged DjVu file: an MMR mask is cut short")
        per = struct.unpack_from(">H", data, 8)[0] or h
        pos = 10
        rows: list[int] = []
        while len(rows) < h and pos + 4 <= len(data):
            n = struct.unpack_from(">I", data, pos)[0]
            pos += 4
            want = min(per, h - len(rows))
            rows += decode(data, w, want, pos, min(len(data), pos + n))
            pos += n
        rows += [0] * (h - len(rows))
    else:
        rows = decode(data, w, h, 8)
    if flags & 1:
        full = (1 << w) - 1
        rows = [r ^ full for r in rows]
    return w, h, rows
