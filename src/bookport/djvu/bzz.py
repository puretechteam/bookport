"""BZZ (DjVu v3 specification, appendix 4): the general-purpose compressor of DIRM, NAVM, ANTz,
TXTz and FGbz data. A Burrows-Wheeler transform whose move-to-front list is kept in order of
estimated symbol frequency, coded with the Z'-coder."""

from __future__ import annotations

from .. import limits
from ..errors import BookportError
from .zp import ZP, EndOfData

MAX_BLOCK = 4096 * 1024  # the largest block an encoder writes


def decode(data: bytes, limit: int | None = None) -> bytes:
    """The bytes a BZZ stream holds (at most ``limit``, MAX_PART by default)."""
    from ._accel import native
    if native is not None:
        return native.bzz_decode(bytes(data), limits.MAX_PART if limit is None else limit)
    return decode_py(data, limit)


def decode_py(data: bytes, limit: int | None = None) -> bytes:
    limit = limits.MAX_PART if limit is None else limit
    ctx = bytearray(300)
    out = bytearray()
    try:
        zp = ZP(data)
        while True:
            size = _raw(zp, 24)
            if not size:
                break
            if size > MAX_BLOCK:
                raise BookportError("Damaged DjVu file: a compressed block is too large")
            if len(out) + size - 1 > limit:
                limits.grow(len(out) + size - 1, limit)
            out += _block(zp, ctx, size)
    except EndOfData:
        raise BookportError("Damaged DjVu file: compressed data ends too soon") from None
    limits.charge(len(out))
    return bytes(out)


def _raw(zp: ZP, bits: int) -> int:
    n = 1
    m = 1 << bits
    while n < m:
        n = (n << 1) | zp.passthru()
    return n - m


def _block(zp: ZP, ctx: bytearray, size: int) -> bytearray:
    decode = zp.decode
    fshift = 0
    if zp.passthru():
        fshift = 2 if zp.passthru() else 1
    mtf = list(range(256))
    freq = [0, 0, 0, 0]
    fadd = 4
    mtfno = 3
    marker = -1
    data = bytearray(size)
    for i in range(size):
        c = min(2, mtfno)
        if decode(ctx, c):
            mtfno = 0
        elif decode(ctx, c + 3):
            mtfno = 1
        else:
            mtfno = 256
            base = 6
            for bits in range(1, 8):
                if decode(ctx, base):
                    n = 1
                    top = 1 << bits
                    while n < top:
                        n = (n << 1) | decode(ctx, base + n)
                    mtfno = n
                    break
                base += 1 << bits
            if mtfno == 256:  # the end-of-block marker
                data[i] = 0
                marker = i
                continue
        sym = mtf[mtfno]
        data[i] = sym
        # move the symbol forward by its estimated frequency
        fadd += fadd >> fshift
        if fadd > 0x10000000:
            fadd >>= 24
            freq = [f >> 24 for f in freq]
        fc = fadd + (freq[mtfno] if mtfno < 4 else 0)
        k = mtfno
        while k > 3:
            mtf[k] = mtf[k - 1]
            k -= 1
        while k > 0 and fc >= freq[k - 1]:
            mtf[k] = mtf[k - 1]
            freq[k] = freq[k - 1]
            k -= 1
        mtf[k] = sym
        freq[k] = fc
    if not 0 <= marker < size:
        raise BookportError("Damaged DjVu file: a compressed block has no end marker")
    return _unbwt(data, marker)


def _unbwt(data: bytearray, marker: int) -> bytearray:
    size = len(data)
    count = [0] * 256
    posn = [0] * size
    for i, c in enumerate(data):
        if i != marker:
            posn[i] = count[c]
            count[c] += 1
    last = 1
    for c in range(256):
        last, count[c] = last + count[c], last
    out = bytearray(size - 1)
    k = 0
    for last in range(size - 2, -1, -1):
        c = data[k]
        out[last] = c
        k = count[c] + posn[k]
        if k >= size:
            raise BookportError("Damaged DjVu file: a compressed block doesn't decode")
    if k != marker:
        raise BookportError("Damaged DjVu file: a compressed block doesn't decode")
    return out
