"""A baseline JPEG encoder (ITU-T T.81), for the background pictures of DjVu pages written to
PDF: greyscale, or colour with 4:2:0 chroma, the standard Huffman tables of annex K and the
usual quality scaling of the quantization tables."""

from __future__ import annotations

import math
import struct

_LUMA_Q = (16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56,
           14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
           49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99)
_CHROMA_Q = (17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99, 24, 26, 56, 99, 99, 99, 99, 99,
             47, 66, 99, 99, 99, 99, 99, 99) + (99,) * 32

# the standard Huffman tables (T.81 annex K.3): code counts by length 1..16, then the values
_DC_LUMA = ((0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0), tuple(range(12)))
_DC_CHROMA = ((0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0), tuple(range(12)))
_AC_LUMA = ((0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D), bytes.fromhex(
    "01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435"
    "363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798"
    "999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4"
    "f5f6f7f8f9fa"))
_AC_CHROMA = ((0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77), bytes.fromhex(
    "000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a"
    "35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a9293949596"
    "9798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4"
    "f5f6f7f8f9fa"))


def _zigzag() -> list[int]:
    order = sorted((x + y, (y if (x + y) % 2 else x), y * 8 + x) for y in range(8) for x in range(8))
    return [i for _, _, i in order]


ZIGZAG = _zigzag()  # zigzag position -> index in the block, row by row


def _codes(spec) -> dict[int, tuple[int, int]]:
    counts, values = spec
    out = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            out[values[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return out


def _quant(base, quality: int) -> list[int]:
    quality = max(1, min(100, quality))
    scale = 5000 // quality if quality < 50 else 200 - 2 * quality
    return [max(1, min(255, (q * scale + 50) // 100)) for q in base]


# the 1-D DCT as a matrix: _COS[u][x] = C(u)/2 cos((2x+1)u pi/16)
_COS = [[(math.sqrt(0.5) if u == 0 else 1.0) / 2 * math.cos((2 * x + 1) * u * math.pi / 16) for x in range(8)]
        for u in range(8)]


def _dct(block: list[float]) -> list[float]:
    rows = []
    for y in range(8):
        r = block[y * 8:y * 8 + 8]
        rows.append([c[0] * r[0] + c[1] * r[1] + c[2] * r[2] + c[3] * r[3] + c[4] * r[4] + c[5] * r[5] + c[6] * r[6]
                     + c[7] * r[7] for c in _COS])
    out = [0.0] * 64
    for u in range(8):
        col = [rows[y][u] for y in range(8)]
        for v in range(8):
            c = _COS[v]
            out[v * 8 + u] = (c[0] * col[0] + c[1] * col[1] + c[2] * col[2] + c[3] * col[3] + c[4] * col[4]
                              + c[5] * col[5] + c[6] * col[6] + c[7] * col[7])
    return out


class _Writer:
    __slots__ = ("acc", "n", "out")

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, code: int, bits: int) -> None:
        self.acc = (self.acc << bits) | code
        self.n += bits
        while self.n >= 8:
            self.n -= 8
            b = (self.acc >> self.n) & 0xFF
            self.out.append(b)
            if b == 0xFF:
                self.out.append(0)
        self.acc &= (1 << self.n) - 1

    def flush(self) -> bytes:
        if self.n:
            self.put((1 << (8 - self.n)) - 1, 8 - self.n)
        return bytes(self.out)


def _plane(px: bytes, w: int, h: int, nc: int, c: int, pw: int, ph: int) -> list[list[float]]:
    """One channel, padded to pw x ph by repeating the last column and row."""
    rows = []
    for y in range(ph):
        yy = min(y, h - 1)
        row = list(px[yy * w * nc + c:(yy + 1) * w * nc:nc])
        rows.append(row + [row[-1]] * (pw - w))
    return rows


def encode(width: int, height: int, mode: str, px: bytes, quality: int = 75) -> bytes:
    """A baseline JPEG of the pixels (mode "L" or "RGB", rows top first)."""
    from ._accel import native
    if native is not None:
        return native.jpeg_encode(width, height, 3 if mode == "RGB" else 1, px, quality)
    color = mode == "RGB"
    unit = 16 if color else 8
    pw = (width + unit - 1) // unit * unit
    ph = (height + unit - 1) // unit * unit
    ql = _quant(_LUMA_Q, quality)
    qc = _quant(_CHROMA_Q, quality)
    if color:
        r, g, b = (_plane(px, width, height, 3, c, pw, ph) for c in range(3))
        ys = [[0.299 * a + 0.587 * bb + 0.114 * cc - 128 for a, bb, cc in zip(ra, ga, ba)] for ra, ga, ba in zip(r, g, b)]
        cb_full = [[-0.168736 * a - 0.331264 * bb + 0.5 * cc for a, bb, cc in zip(ra, ga, ba)] for ra, ga, ba in zip(r, g, b)]
        cr_full = [[0.5 * a - 0.418688 * bb - 0.081312 * cc for a, bb, cc in zip(ra, ga, ba)] for ra, ga, ba in zip(r, g, b)]
        cbs = [[(p[2 * x] + p[2 * x + 1] + q[2 * x] + q[2 * x + 1]) / 4 for x in range(pw // 2)]
               for p, q in zip(cb_full[0::2], cb_full[1::2])]
        crs = [[(p[2 * x] + p[2 * x + 1] + q[2 * x] + q[2 * x + 1]) / 4 for x in range(pw // 2)]
               for p, q in zip(cr_full[0::2], cr_full[1::2])]
    else:
        ys = [[v - 128 for v in row] for row in _plane(px, width, height, 1, 0, pw, ph)]
    dcl, acl = _codes(_DC_LUMA), _codes(_AC_LUMA)
    dcc, acc = _codes(_DC_CHROMA), _codes(_AC_CHROMA)
    w = _Writer()
    pred = [0, 0, 0]

    def block(plane, bx, by, q, dc, ac, k):
        data = []
        for y in range(by, by + 8):
            data += plane[y][bx:bx + 8]
        f = _dct(data)
        zz = [round(f[ZIGZAG[i]] / q[ZIGZAG[i]]) for i in range(64)]
        diff = zz[0] - pred[k]
        pred[k] = zz[0]
        size = abs(diff).bit_length()
        w.put(*dc[size])
        if size:
            w.put(diff if diff > 0 else diff + (1 << size) - 1, size)
        run = 0
        for i in range(1, 64):
            v = zz[i]
            if not v:
                run += 1
                continue
            while run > 15:
                w.put(*ac[0xF0])
                run -= 16
            size = abs(v).bit_length()
            w.put(*ac[(run << 4) | size])
            w.put(v if v > 0 else v + (1 << size) - 1, size)
            run = 0
        if run:
            w.put(*ac[0])

    for my in range(0, ph, unit):
        for mx in range(0, pw, unit):
            if color:
                for dy in (0, 8):
                    for dx in (0, 8):
                        block(ys, mx + dx, my + dy, ql, dcl, acl, 0)
                block(cbs, mx // 2, my // 2, qc, dcc, acc, 1)
                block(crs, mx // 2, my // 2, qc, dcc, acc, 2)
            else:
                block(ys, mx, my, ql, dcl, acl, 0)
    return _headers(width, height, color, ql, qc) + w.flush() + b"\xff\xd9"


def _headers(width: int, height: int, color: bool, ql: list[int], qc: list[int]) -> bytes:
    out = bytearray(b"\xff\xd8")
    out += b"\xff\xe0" + struct.pack(">H5sBBBHHBB", 16, b"JFIF\0", 1, 1, 0, 1, 1, 0, 0)
    tables = [(0, ql)] + ([(1, qc)] if color else [])
    for tid, q in tables:
        out += b"\xff\xdb" + struct.pack(">HB", 67, tid) + bytes(q[ZIGZAG[i]] for i in range(64))
    comps = ((1, 0x22, 0), (2, 0x11, 1), (3, 0x11, 1)) if color else ((1, 0x11, 0),)
    out += b"\xff\xc0" + struct.pack(">HBHHB", 8 + 3 * len(comps), 8, height, width, len(comps))
    for c in comps:
        out += bytes(c)
    specs = [(0x00, _DC_LUMA), (0x10, _AC_LUMA)] + ([(0x01, _DC_CHROMA), (0x11, _AC_CHROMA)] if color else [])
    for tc, (counts, values) in specs:
        out += b"\xff\xc4" + struct.pack(">HB", 3 + 16 + len(values), tc) + bytes(counts) + bytes(values)
    sel = ((1, 0x00), (2, 0x11), (3, 0x11)) if color else ((1, 0x00),)
    out += b"\xff\xda" + struct.pack(">HB", 6 + 2 * len(sel), len(sel))
    for s in sel:
        out += bytes(s)
    out += b"\x00\x3f\x00"
    return bytes(out)
