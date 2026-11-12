"""IW44 (DjVu v3 specification, appendix 1): the wavelet-coded colour and greyscale layers of a page
(BG44 background, FG44 foreground, TH44 thumbnails, and BM44/PM44 image files).

``decode(chunks)`` reads every chunk of one layer in order (each adds slices of precision) and
gives (width, height, mode, pixels): mode "L" or "RGB", rows top first.
"""

from __future__ import annotations

import struct

from .. import limits
from ..errors import BookportError
from .zp import ZP, EndOfData

# bucket ranges of the ten bands, and the initial step sizes (specification tables 2 and 4)
BANDS = ((0, 1), (1, 1), (2, 1), (3, 1), (4, 4), (8, 4), (12, 4), (16, 16), (32, 16), (48, 16))
QUANT = (0x004000, 0x008000, 0x008000, 0x010000, 0x010000, 0x010000, 0x020000, 0x020000, 0x020000, 0x040000,
         0x040000, 0x040000, 0x080000, 0x040000, 0x040000, 0x080000)
ZERO, ACTIVE, NEW, UNK = 1, 2, 4, 8
MAX_SLICES = 4096


def _zigzag() -> tuple[list[int], list[int]]:
    rows, cols = [], []
    for i in range(1024):
        r = c = 0
        for k in range(5):  # row from bits 1, 3, 5, 7, 9 and column from 0, 2, 4, 6, 8, most significant first
            r |= ((i >> (2 * k + 1)) & 1) << (4 - k)
            c |= ((i >> (2 * k)) & 1) << (4 - k)
        rows.append(r)
        cols.append(c)
    return rows, cols


ZIG_ROW, ZIG_COL = _zigzag()


class _Codec:
    """The coefficients of one colour component and the state of its decoding."""

    def __init__(self, nblocks: int):
        self.blocks = [[None] * 64 for _ in range(nblocks)]
        self.lo = [QUANT[0], QUANT[1], QUANT[2], QUANT[3]] + [QUANT[4]] * 4 + [QUANT[5]] * 4 + [QUANT[6]] * 4
        self.hi = [0] + list(QUANT[7:16])
        self.band = 0
        self.bit = 0
        self.ctx_start = bytearray(32)
        self.ctx_bucket = bytearray(80)
        self.ctx_mant = bytearray(1)
        self.ctx_root = bytearray(1)

    def slice(self, zp: ZP) -> bool:
        if self.bit < 0:
            return False
        band = self.band
        if band == 0:
            zero = [not (0 < t < 0x8000) for t in self.lo]
            if not all(zero):
                for blk in self.blocks:
                    self._band0(zp, blk, zero)
        elif 0 < self.hi[band] < 0x8000:
            first, n = BANDS[band]
            for blk in self.blocks:
                self._bucket_band(zp, blk, band, first, n)
        self.band += 1
        if self.band == 10:
            self.band = 0
            self.bit += 1
            self.lo = [t >> 1 for t in self.lo]
            self.hi = [t >> 1 for t in self.hi]
            if self.hi[9] == 0:
                self.bit = -1
        return True

    def _band0(self, zp: ZP, blk: list, zero: list[bool]) -> None:
        coef = blk[0]
        lo = self.lo
        if coef is None:
            state = [ZERO if z else UNK for z in zero]
        else:
            state = [ZERO if z else (ACTIVE if c else UNK) for z, c in zip(zero, coef)]
        bstate = 0
        for s in state:
            bstate |= s
        # one bucket in band 0: its decision is made whenever a coefficient may become active
        if bstate & UNK and zp.decode(self.ctx_bucket, (4 if bstate & ACTIVE else 0)):
            if coef is None:
                coef = blk[0] = [0] * 16
            self._activate(zp, coef, state, lo, bstate & ACTIVE)
        if bstate & ACTIVE:
            self._refine(zp, coef, state, lo)

    def _bucket_band(self, zp: ZP, blk: list, band: int, first: int, n: int) -> None:
        decode = zp.decode
        bucket_state = []
        states = []
        bstate = 0
        for b in range(first, first + n):
            coef = blk[b]
            if coef is None:
                st = None
                bs = UNK
            else:
                st = [ACTIVE if c else UNK for c in coef]
                bs = 0
                for s in st:
                    bs |= s
            states.append(st)
            bucket_state.append(bs)
            bstate |= bs
        thres = self.hi[band]
        if n < 16 or bstate & ACTIVE:
            go = True
        elif bstate & UNK:
            go = decode(self.ctx_root, 0)
        else:
            go = False
        if go:
            act = 4 if bstate & ACTIVE else 0
            ctx = self.ctx_bucket
            base = band * 8
            for j in range(n):
                if bucket_state[j] & UNK:
                    # context: how many of the four parent coefficients are set (at most 3)
                    k = (first + j) << 2
                    parent = blk[k >> 4]
                    c = 0
                    if parent is not None:
                        k &= 15
                        c = (parent[k] != 0) + (parent[k + 1] != 0) + (parent[k + 2] != 0)
                        if c < 3 and parent[k + 3]:
                            c += 1
                    if decode(ctx, base + (c | act)):
                        bucket_state[j] |= NEW
            for j in range(n):
                if bucket_state[j] & NEW:
                    b = first + j
                    coef = blk[b]
                    if coef is None:
                        coef = blk[b] = [0] * 16
                        states[j] = [UNK] * 16
                    self._activate(zp, coef, states[j], thres, bucket_state[j] & ACTIVE)
        if bstate & ACTIVE:
            for j in range(n):
                if bucket_state[j] & ACTIVE:
                    self._refine(zp, blk[first + j], states[j], thres)

    def _activate(self, zp: ZP, coef: list[int], state: list[int], thres, active: int) -> None:
        """Decode which unknown coefficients of a bucket become non-zero, and their signs."""
        decode = zp.decode
        ctx = self.ctx_start
        act = 8 if active else 0
        gotcha = 0
        for s in state:
            if s & UNK:
                gotcha += 1
        for i in range(16):
            if state[i] & UNK:
                t = thres[i] if isinstance(thres, list) else thres
                if decode(ctx, (min(7, gotcha)) | act):
                    state[i] |= NEW
                    half = t >> 1
                    v = t + half - (half >> 2)
                    coef[i] = _s16(-v) if zp.iw_passthru() else _s16(v)
                    gotcha = 0
                elif gotcha > 0:
                    gotcha -= 1

    def _refine(self, zp: ZP, coef: list[int], state: list[int], thres) -> None:
        """One more bit of precision for each coefficient that was already non-zero."""
        decode = zp.decode
        for i in range(16):
            if state[i] & ACTIVE:
                t = thres[i] if isinstance(thres, list) else thres
                c = coef[i]
                v = -c if c < 0 else c
                if v <= 3 * t:
                    v += t >> 2
                    if decode(self.ctx_mant, 0):
                        v += t >> 1
                    else:
                        v = v - t + (t >> 1)
                elif zp.iw_passthru():
                    v += t >> 1
                else:
                    v = v - t + (t >> 1)
                coef[i] = _s16(v if c > 0 else -v)


class Layer:
    """One IW44 layer being decoded, chunk by chunk."""

    def __init__(self):
        self.width = self.height = 0
        self.codecs: list[_Codec] = []
        self.serial = 0
        self.slices = 0
        self.delay = 0
        self.half = False
        self.color = False

    def add(self, data: bytes) -> None:
        if len(data) < 2:
            raise BookportError("Damaged DjVu file: an IW44 chunk is too short")
        serial, nslices = data[0], data[1]
        pos = 2
        if serial != self.serial:
            raise BookportError("Damaged DjVu file: IW44 chunks are out of order")
        if serial == 0:
            if len(data) < 9:
                raise BookportError("Damaged DjVu file: an IW44 chunk is too short")
            major, minor = data[2], data[3]
            if major & 0x7F > 1:
                raise BookportError("This DjVu file uses a newer IW44 version that bookport can't read")
            w, h = struct.unpack_from(">HH", data, 4)
            pos = 8
            delay = 0
            if minor >= 2:
                delay = data[8] & 0x7F
                self.half = not (data[8] & 0x80)
                pos = 9
            if not w or not h:
                raise BookportError("Damaged DjVu file: an IW44 image has no size")
            limits.check_pixels(w, h)
            self.width, self.height = w, h
            self.color = not (major & 0x80)
            self.delay = delay
            nb = ((w + 31) // 32) * ((h + 31) // 32)
            self.codecs = [_Codec(nb) for _ in range(3 if self.color else 1)]
        elif not self.codecs:
            raise BookportError("Damaged DjVu file: an IW44 chunk comes before the first one")
        self.serial += 1
        y = self.codecs[0]
        try:
            zp = ZP(data, pos)
            for _ in range(nslices):
                if self.slices >= MAX_SLICES:
                    break
                more = y.slice(zp)
                if self.color and self.slices >= self.delay:
                    more = self.codecs[1].slice(zp) | more
                    more = self.codecs[2].slice(zp) | more
                self.slices += 1
                if not more:
                    break
        except EndOfData:  # what was decoded so far stands, as in the reference decoder
            pass

    def image(self) -> tuple[int, int, str, bytes]:
        w, h = self.width, self.height
        if not self.codecs:
            raise BookportError("Damaged DjVu file: an IW44 layer has no data")
        planes = [_reconstruct(c, w, h, 2 if (i and self.half) else 1) for i, c in enumerate(self.codecs)]
        if len(planes) == 1:
            out = bytearray()
            for row in reversed(planes[0]):
                out += bytes(_GRAY[(v + 32 >> 6) + 512] for v in row[:w])
            return w, h, "L", bytes(out)
        out = bytearray(w * h * 3)
        pos = 0
        for yr, br, rr in zip(reversed(planes[0]), reversed(planes[1]), reversed(planes[2])):
            ys = [_CLAMP[(v + 32 >> 6) + 512] for v in yr[:w]]
            cb = [_CLAMP[(v + 32 >> 6) + 512] for v in br[:w]]
            cr = [_CLAMP[(v + 32 >> 6) + 512] for v in rr[:w]]
            t2 = [r + (r >> 1) for r in cr]
            t3 = [y + 128 - (b >> 2) for y, b in zip(ys, cb)]
            out[pos:pos + 3 * w:3] = bytes(_BYTE[y + 128 + t + 512] for y, t in zip(ys, t2))
            out[pos + 1:pos + 3 * w:3] = bytes(_BYTE[t - (u >> 1) + 512] for t, u in zip(t3, t2))
            out[pos + 2:pos + 3 * w:3] = bytes(_BYTE[t + (b << 1) + 512] for t, b in zip(t3, cb))
            pos += 3 * w
        return w, h, "RGB", bytes(out)


# fixed-point (6 fraction bits) to a signed byte, and to the grey level of a greyscale image
_CLAMP = [max(-128, min(127, v)) for v in range(-512, 513)]
_GRAY = [127 - c for c in _CLAMP]
_BYTE = [max(0, min(255, v)) for v in range(-512, 1024)]


def _s16(v: int) -> int:
    """v stored in a 16-bit sample, as the reference decoder keeps coefficients (it wraps)."""
    return ((v + 0x8000) & 0xFFFF) - 0x8000


def _short(row: list[int]) -> list[int]:
    if row and (max(row) > 0x7FFF or min(row) < -0x8000):
        return [((v + 0x8000) & 0xFFFF) - 0x8000 for v in row]
    return row


def _reconstruct(codec: _Codec, w: int, h: int, end: int) -> list[list[int]]:
    """The component's samples (rows bottom first, 6 fraction bits), down to scale ``end``."""
    bw = (w + 31) & ~31
    bh = (h + 31) & ~31
    rows = [[0] * bw for _ in range(bh)]
    nbx = bw // 32
    zr, zc = ZIG_ROW, ZIG_COL
    for n, blk in enumerate(codec.blocks):
        y0 = (n // nbx) * 32
        x0 = (n % nbx) * 32
        for b in range(64):
            coef = blk[b]
            if coef is None:
                continue
            base = b << 4
            for i in range(16):
                v = coef[i]
                if v:
                    rows[y0 + zr[base + i]][x0 + zc[base + i]] = v
    s = 16
    while s >= end:
        _vertical(rows, w, h, s)
        _horizontal(rows, w, h, s)
        s >>= 1
    if end == 2:  # half-resolution chrominance: each sample covers 2 x 2 pixels
        for y in range(0, h, 2):
            row = rows[y]
            for x in range(0, w, 2):
                row[x + 1] = row[x]
            if y + 1 < bh:
                rows[y + 1] = row
    return rows[:h]


def _vertical(rows: list[list[int]], w: int, h: int, s: int) -> None:
    kmax = (h - 1) // s
    vec = [rows[k * s][0:w:s] for k in range(kmax + 1)]
    zero = [0] * len(vec[0])

    def at(k):
        return vec[k] if 0 <= k <= kmax else zero

    for k in range(0, kmax + 1, 2):  # lifting: even rows from the odd ones around them
        vec[k] = [e - ((9 * (a + b) - c - d + 16) >> 5)
                  for e, a, b, c, d in zip(vec[k], at(k - 1), at(k + 1), at(k - 3), at(k + 3))]
        vec[k] = _short(vec[k])
    for k in range(1, kmax + 1, 2):  # prediction: odd rows from the even ones around them
        if k >= 3 and k + 3 <= kmax:
            vec[k] = [o + ((9 * (a + b) - c - d + 8) >> 4)
                      for o, a, b, c, d in zip(vec[k], vec[k - 1], vec[k + 1], vec[k - 3], vec[k + 3])]
            vec[k] = _short(vec[k])
        elif k + 1 <= kmax:
            vec[k] = [o + ((a + b + 1) >> 1) for o, a, b in zip(vec[k], vec[k - 1], vec[k + 1])]
            vec[k] = _short(vec[k])
        else:
            vec[k] = [o + a for o, a in zip(vec[k], vec[k - 1])]
            vec[k] = _short(vec[k])
    for k in range(kmax + 1):
        rows[k * s][0:w:s] = vec[k]


def _horizontal(rows: list[list[int]], w: int, h: int, s: int) -> None:
    kmax = (w - 1) // s
    for y in range(0, h, s):
        row = rows[y]
        x = row[0:w:s]
        ev = x[0::2]
        od = x[1::2]
        p = [0, 0] + od + [0, 0]
        ev = [e - ((9 * (a + b) - c - d + 16) >> 5) for e, a, b, c, d in zip(ev, p[1:], p[2:], p, p[3:])]
        ev = _short(ev)
        no = len(od)
        if no:
            q = [0] + ev + [0, 0]  # q[j + 1] = ev[j]
            new = [o + ((9 * (a + b) - c - d + 8) >> 4) for o, a, b, c, d in zip(od, q[1:], q[2:], q, q[3:])]
            # the odd samples near either end are linear interpolations instead
            last = (kmax - 4) // 2  # the last one with two even neighbours on each side
            for j in [0] + list(range(max(1, last + 1), no)):
                if 2 * j + 2 <= kmax:
                    new[j] = od[j] + ((ev[j] + ev[j + 1] + 1) >> 1)
                else:
                    new[j] = od[j] + ev[j]
            od = _short(new)
        x[0::2] = ev
        x[1::2] = od
        row[0:w:s] = x


def decode(chunks: list[bytes]) -> tuple[int, int, str, bytes]:
    """(width, height, "L" or "RGB", pixels top row first) of an IW44 layer's chunks."""
    from ._accel import native
    if native is not None:
        return native.iw44_decode([bytes(c) for c in chunks])
    layer = Layer()
    for c in chunks:
        layer.add(c)
    return layer.image()
