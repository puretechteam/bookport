"""PostScript files and filters (PLRM 3.8, 3.13): a stream is a byte buffer and a read position. The file being run
is one buffer; a filter decodes its source up to its end-of-data mark the first time it's read, leaving the source
exactly after that mark (so what follows image data is read as program again). eexec decrypts as it goes, and when
it's closed the source carries on right after the last byte it decrypted."""

from __future__ import annotations

import zlib
from array import array

from .. import limits
from .objects import PSError

WS = b"\x00\t\n\x0c\r "
HEX = b"0123456789abcdefABCDEF"
_HEX_VAL = {c: int(chr(c), 16) for c in HEX}


class Stream:
    """A readable stream over ``buf`` from ``pos``; ``more()`` adds to buf (False when there's no more)."""

    def __init__(self, data: bytes = b"", name: str = ""):
        self.buf = data
        self.pos = 0
        self.closed = False
        self.name = name

    def more(self) -> bool:
        return False

    def all(self) -> None:
        while self.more():
            pass

    def readbyte(self) -> int:
        if self.pos >= len(self.buf) and not self._fill(1):
            return -1
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def _fill(self, n: int) -> bool:
        """At least n bytes after pos, or as many as there are."""
        while len(self.buf) - self.pos < n:
            if self.closed or not self.more():
                return len(self.buf) > self.pos
        return True

    def read(self, n: int) -> bytes:
        self._fill(n)
        out = self.buf[self.pos:self.pos + n]
        self.pos += len(out)
        return bytes(out)

    def rest(self) -> bytes:
        self.all()
        out = self.buf[self.pos:]
        self.pos = len(self.buf)
        return bytes(out)

    def close(self) -> None:
        self.closed = True
        self.pos = len(self.buf)

    def available(self) -> int:
        return len(self.buf) - self.pos


class WriteStream(Stream):
    """%stdout and friends: what's written is kept (as print output) and can't be read."""

    def __init__(self, sink: bytearray, name: str = "%stdout"):
        super().__init__(b"", name)
        self.sink = sink

    def write(self, data: bytes) -> None:
        if len(self.sink) < 1_000_000:
            self.sink += data


class PSFile:
    """A file object: a stream and the executable attribute (cvx makes a new object on the same stream)."""
    __slots__ = ("stream", "x")

    def __init__(self, stream: Stream, x: bool = False):
        self.stream = stream
        self.x = x

    def readbyte(self) -> int:
        return self.stream.readbyte()

    def copy_attr(self, x: bool) -> PSFile:
        return PSFile(self.stream, x)

    def __repr__(self):
        return f"PSFile({self.stream.name})"


# ---- decoders: each takes the source stream, consumes it through its end-of-data mark and returns the bytes

def _hex(src: Stream) -> bytes:
    src.all()
    buf, start = src.buf, src.pos
    end = buf.find(b">", start)
    stop = len(buf) if end < 0 else end
    digits = bytes(buf[start:stop]).translate(None, WS)
    src.pos = len(buf) if end < 0 else end + 1
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        raise PSError("ioerror", "bad hex data") from None


def a85(text: bytes) -> bytes:
    out = bytearray()
    group = []
    for c in text:
        if c in WS:
            continue
        if c == 0x7A and not group:  # z
            out += b"\0\0\0\0"
            continue
        if not 33 <= c <= 117:
            raise PSError("ioerror", "bad ASCII85 data")
        group.append(c - 33)
        if len(group) == 5:
            n = 0
            for d in group:
                n = n * 85 + d
            if n > 0xFFFFFFFF:
                raise PSError("ioerror", "bad ASCII85 data")
            out += n.to_bytes(4, "big")
            group = []
    if group:
        if len(group) == 1:
            raise PSError("ioerror", "bad ASCII85 data")
        k = len(group)
        n = 0
        for d in group + [84] * (5 - k):
            n = n * 85 + d
        out += (n & 0xFFFFFFFF).to_bytes(4, "big")[:k - 1]
        limits.grow(out)
    return bytes(out)


def _a85(src: Stream) -> bytes:
    src.all()
    buf, start = src.buf, src.pos
    end = buf.find(b"~>", start)
    stop = len(buf) if end < 0 else end
    src.pos = len(buf) if end < 0 else end + 2
    return a85(buf[start:stop])


def _rl(src: Stream) -> bytes:
    src.all()
    buf, i = src.buf, src.pos
    out = bytearray()
    n = len(buf)
    while i < n:
        c = buf[i]
        i += 1
        if c == 128:
            break
        if c < 128:
            out += buf[i:i + c + 1]
            i += c + 1
        else:
            if i >= n:
                break
            out += bytes([buf[i]]) * (257 - c)
            i += 1
        if len(out) > limits.MAX_PART:
            limits.grow(out)
    src.pos = min(i, n)
    return bytes(out)


def _lzw(src: Stream, early: int = 1) -> bytes:
    src.all()
    data, start = src.buf, src.pos
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    width, bit, prev = 9, start * 8, None
    total = len(data) * 8
    while bit + width <= total:
        byte = bit >> 3
        chunk = int.from_bytes(data[byte:byte + 3].ljust(3, b"\0"), "big")
        code = (chunk >> (24 - (bit & 7) - width)) & ((1 << width) - 1)
        bit += width
        if code == 256:
            table = table[:258]
            width, prev = 9, None
            continue
        if code == 257:
            break
        if code < len(table):
            entry = table[code]
            if prev is not None and len(table) < 4096:  # a full table stays as it is until a clear code
                table.append(prev + entry[:1])
        elif prev is not None and code == len(table):
            entry = prev + prev[:1]
            table.append(entry)
        else:
            raise PSError("ioerror", "bad LZW data")
        out += entry
        if len(out) > limits.MAX_PART:
            limits.grow(out)
        prev = entry
        if len(table) + early >= (1 << width) and width < 12:
            width += 1
    src.pos = min(len(data), (bit + 7) >> 3)
    return bytes(out)


def _flate(src: Stream) -> bytes:
    src.all()
    d = zlib.decompressobj()
    data = src.buf[src.pos:]
    try:
        out = d.decompress(data, limits.MAX_PART + 1)
    except zlib.error:
        raise PSError("ioerror", "bad Flate data") from None
    if len(out) > limits.MAX_PART:
        limits.grow(out)
    src.pos = len(src.buf) - len(d.unused_data)
    return out


def jpeg_end(buf, start: int) -> int:
    """Where a JPEG starting at ``start`` ends (after its EOI marker), walking the marker segments."""
    i, n = start, len(buf)
    while i < n and buf[i] != 0xFF:
        i += 1  # leading junk (whitespace before the SOI)
    if buf[i:i + 2] != b"\xff\xd8":
        raise PSError("ioerror", "not JPEG data")
    i += 2
    while i + 1 < n:
        if buf[i] != 0xFF:
            i += 1
            continue
        m = buf[i + 1]
        if m == 0xD9:
            return i + 2
        if m in (0xFF, 0x00, 0x01) or 0xD0 <= m <= 0xD7:
            i += 1 if m == 0xFF else 2
            continue
        if i + 4 > n:
            break
        seg = (buf[i + 2] << 8) | buf[i + 3]
        i += 2 + seg
        if m == 0xDA:  # entropy-coded data follows: the next marker that isn't stuffing or a restart
            while i + 1 < n:
                if buf[i] == 0xFF and buf[i + 1] not in (0x00, 0xFF) and not 0xD0 <= buf[i + 1] <= 0xD7:
                    break
                i += 1
    return n


class Decoded(Stream):
    """A decoding filter: everything up to the source's end-of-data, decoded the first time it's read."""

    def __init__(self, source: Stream, decode, name: str = ""):
        super().__init__(b"", name)
        self.source = source
        self.decode = decode
        self.done = False

    def more(self) -> bool:
        if self.done:
            return False
        self.done = True
        data = self.decode(self.source)
        limits.charge(len(data))
        self.buf = self.buf + data  # positions stay valid (the scanner may hold one)
        return bool(data)


class DCTStream(Decoded):
    """DCTDecode: the JPEG is kept as it is (``jpeg``) for the PDF; its pixels only if something reads them."""

    def __init__(self, source: Stream):
        super().__init__(source, self._pixels, "DCTDecode")
        self.jpeg: bytes | None = None
        self.taken = False

    def grab(self) -> bytes:
        if self.jpeg is None:
            src = self.source
            src.all()
            end = jpeg_end(src.buf, src.pos)
            self.jpeg = bytes(src.buf[src.pos:end]).lstrip(WS)
            src.pos = end
        return self.jpeg

    def _pixels(self, _src) -> bytes:
        data = self.grab()
        try:
            import io

            from PIL import Image
        except ImportError:
            raise PSError("ioerror", "reading JPEG pixels needs Pillow") from None
        with Image.open(io.BytesIO(data)) as im:
            limits.check_pixels(*im.size)
            return im.tobytes()


class SubFile(Stream):
    """SubFileDecode: the source up to the count-th EODString (or count bytes when the string is empty)."""

    def __init__(self, source: Stream, count: int, eod: bytes):
        super().__init__(b"", "SubFileDecode")
        self.source, self.count, self.eod = source, count, eod
        self.done = False

    def more(self) -> bool:
        if self.done:
            return False
        self.done = True
        src = self.source
        src.all()
        buf, pos = src.buf, src.pos
        if not self.eod:  # a byte count (0: everything)
            stop = end = len(buf) if self.count == 0 else min(len(buf), pos + self.count)
        else:  # the first count EODStrings pass through; the next one ends the data (and is consumed)
            stop = end = len(buf)
            j = pos
            for _ in range(self.count + 1):
                k = buf.find(self.eod, j)
                if k < 0:
                    break
                j = k + len(self.eod)
            else:
                stop, end = k, j
        self.buf = bytes(buf[pos:stop])
        src.pos = end
        return bool(self.buf)


class Eexec(Stream):
    """eexec decryption (Type 1 font format, chapter 7), hex or binary, a chunk at a time; ``close`` puts the
    source right after the last decrypted byte that was read."""

    CHUNK = 16384

    def __init__(self, source: Stream, r: int = 55665):
        super().__init__(bytearray(), "eexec")
        self.source = source
        self.r = r
        self.ends = array("q")  # the source position after each decrypted byte
        source._fill(4)
        i = source.pos
        while i < len(source.buf) and source.buf[i] in WS:
            i += 1
            if i >= len(source.buf):
                source.more()
        source.pos = i
        source._fill(4)
        head = source.buf[i:i + 4]
        self.hex = len(head) == 4 and all(c in HEX for c in head)
        self.skip = 4
        self.pending = -1

    def more(self) -> bool:
        if self.closed:
            return False
        src = self.source
        if src.pos >= len(src.buf) and not src.more():
            return False
        chunk_end = min(len(src.buf), src.pos + self.CHUNK)
        r, buf, ends = self.r, self.buf, self.ends
        data = src.buf
        added = 0
        i = src.pos
        if self.hex:
            pending = self.pending
            while i < chunk_end:
                c = data[i]
                i += 1
                v = _HEX_VAL.get(c)
                if v is None:
                    continue
                if pending < 0:
                    pending = v
                    continue
                c, pending = (pending << 4) | v, -1
                p = c ^ (r >> 8)
                r = ((c + r) * 52845 + 22719) & 0xFFFF
                if self.skip:
                    self.skip -= 1
                    continue
                buf.append(p)
                ends.append(i)
                added += 1
            self.pending = pending
        else:
            for i in range(src.pos, chunk_end):
                c = data[i]
                p = c ^ (r >> 8)
                r = ((c + r) * 52845 + 22719) & 0xFFFF
                if self.skip:
                    self.skip -= 1
                    continue
                buf.append(p)
                ends.append(i + 1)
                added += 1
            i = chunk_end
        src.pos = i
        self.r = r
        return added > 0 or src.pos < len(src.buf) or src.more()

    def close(self) -> None:
        """The source carries on after the encrypted part. Bytes encrypted after the closefile are skipped, as an
        interpreter reading ahead would (the zeros fonts put after it are there for that): in hex, every hex
        digit (the zeros too, which only cleartomark would have cleared); in binary, up to the zeros."""
        if not self.closed:
            src = self.source
            if self.pos > 0:
                src.pos = self.ends[self.pos - 1]
            src.all()
            buf, i = src.buf, src.pos
            if self.hex:  # whole words of hex digits (cleartomark starts with hex letters)
                while True:
                    while i < len(buf) and buf[i] in WS:
                        i += 1
                    j = i
                    while j < len(buf) and buf[j] not in WS:
                        j += 1
                    if j == i or not all(c in HEX for c in buf[i:j]):
                        break
                    i = j
            else:
                while i < len(buf) and buf[i] in WS:
                    i += 1
                if buf[i:i + 1] not in (b"0", b"c", b"%", b""):  # something encrypted is left: find the zeros
                    j = buf.find(b"0" * 32, i, i + 4096)
                    i = j if j >= 0 else i
            src.pos = i
            self.closed = True
            self.pos = len(self.buf)


def decrypt(data: bytes, r: int, skip: int) -> bytes:
    """Type 1 decryption of a whole string (charstrings: r 4330, skip lenIV)."""
    out = bytearray()
    for c in data:
        out.append(c ^ (r >> 8))
        r = ((c + r) * 52845 + 22719) & 0xFFFF
    return bytes(out[skip:]) if skip >= 0 else bytes(data)


def encrypt(data: bytes, r: int) -> bytes:
    out = bytearray()
    for p in data:
        c = p ^ (r >> 8)
        r = ((c + r) * 52845 + 22719) & 0xFFFF
        out.append(c)
    return bytes(out)


def predict(data: bytes, parms: dict) -> bytes:
    from ..pdf.parser import _predict
    return _predict(data, parms)


DECODERS = {
    "ASCIIHexDecode": _hex, "AHx": _hex, "ASCII85Decode": _a85, "A85": _a85, "RunLengthDecode": _rl, "RL": _rl,
    "FlateDecode": _flate, "Fl": _flate,
}
