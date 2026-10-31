"""Palm database (PDB) containers and the compression schemes found inside them.

- PDB: the 78-byte header, the record list, reading and writing records.
- PalmDOC compression: LZ77 with a 2047-byte window plus space+character pairs.
- HUFF/CDIC: Mobipocket's Huffman dictionary compression (decompression only).
- Mobipocket's variable-width integers (forward and backward encoded).
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from .errors import BookportError

PDB_EPOCH = 2082844800  # seconds from 1904-01-01 to 1970-01-01


@dataclass
class Pdb:
    name: bytes
    type: bytes
    creator: bytes
    records: list[bytes]
    attributes: int = 0
    version: int = 0
    ctime: int = 0
    mtime: int = 0
    uid_seed: int = 0


def read_pdb(data: bytes) -> Pdb:
    if len(data) < 78:
        raise BookportError("Not a Palm database: the file is too short")
    name = data[:32].split(b"\0", 1)[0]
    attrs, version, ctime, mtime = struct.unpack(">HHII", data[32:44])
    kind, creator = data[60:64], data[64:68]
    uid_seed = struct.unpack(">I", data[68:72])[0]
    count = struct.unpack(">H", data[76:78])[0]
    if 78 + count * 8 > len(data):
        raise BookportError("Damaged Palm database: the record list runs past the end of the file")
    offsets = [struct.unpack(">I", data[78 + i * 8:82 + i * 8])[0] for i in range(count)]
    records = []
    for i, off in enumerate(offsets):
        end = offsets[i + 1] if i + 1 < count else len(data)
        if off > len(data) or end < off:
            end = off = min(off, len(data))
        records.append(data[off:min(end, len(data))])
    return Pdb(name, kind, creator, records, attrs, version, ctime, mtime, uid_seed)


def pdb_bytes(name: str | bytes, kind: bytes, creator: bytes, records: list[bytes], uid_seed: int | None = None) -> bytes:
    if isinstance(name, str):
        name = _pdb_name(name)
    now = int(time.time()) + PDB_EPOCH
    n = len(records)
    header = bytearray(name[:31].ljust(32, b"\0"))
    header += struct.pack(">HHIIIIII", 0, 0, now, now, 0, 0, 0, 0)
    header += kind[:4].ljust(4, b"\0") + creator[:4].ljust(4, b"\0")
    header += struct.pack(">IIH", uid_seed if uid_seed is not None else (2 * n - 1), 0, n)
    offset = 78 + 8 * n + 2
    for i, r in enumerate(records):
        header += struct.pack(">IBBH", offset, 0, (2 * i >> 16) & 0xFF, (2 * i) & 0xFFFF)
        offset += len(r)
    header += b"\0\0"
    return bytes(header) + b"".join(records)


def _pdb_name(title: str) -> bytes:
    import re
    import unicodedata
    ascii_name = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9 _-]", "", ascii_name).strip().replace(" ", "_") or "book"
    if len(ascii_name) > 31:
        ascii_name = ascii_name[:14] + "-" + ascii_name[-16:]
    return ascii_name.encode("ascii")


# --- PalmDOC LZ77 ---------------------------------------------------------------------------------

def palmdoc_decompress(src: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        i += 1
        if c == 0 or 9 <= c <= 0x7F:
            out.append(c)
        elif c <= 8:
            out += src[i:i + c]
            i += c
        elif c >= 0xC0:
            out.append(0x20)
            out.append(c ^ 0x80)
        else:
            if i >= n:
                break
            pair = (c << 8) | src[i]
            i += 1
            dist = (pair >> 3) & 0x7FF
            length = (pair & 7) + 3
            if dist == 0 or dist > len(out):
                continue  # damaged: skip the reference rather than fail
            start = len(out) - dist
            if dist >= length:
                out += out[start:start + length]
            else:
                for k in range(length):
                    out.append(out[start + k])
    return bytes(out)


def palmdoc_compress(src: bytes) -> bytes:
    """PalmDOC LZ77: greedy longest match over 3-byte hash chains (window 2047, lengths 3-10)."""
    out = bytearray()
    append = out.append
    n = len(src)
    head = {}
    get = head.get
    prev = [-1] * n
    i = 0
    last3 = n - 2
    while i < n:
        if i < last3:
            key = src[i:i + 3]
            j = get(key, -1)
            prev[i] = j
            head[key] = i
            if j >= 0 and i - j <= 2047:
                lo = i - 2047
                limit = min(10, n - i)
                best = 0
                bestj = j
                steps = 0
                while j >= 0 and j >= lo and steps < 8:
                    length = 3
                    while length < limit and src[j + length] == src[i + length]:
                        length += 1
                    if length > best:
                        best, bestj = length, j
                        if length == limit:
                            break
                    j = prev[j]
                    steps += 1
                code = 0x8000 | ((i - bestj) << 3) | (best - 3)
                append(code >> 8)
                append(code & 0xFF)
                end = i + best
                k = i + 1
                stop = min(last3, end)
                while k < stop:
                    kk = src[k:k + 3]
                    prev[k] = get(kk, -1)
                    head[kk] = k
                    k += 1
                i = end
                continue
        c = src[i]
        if c == 0x20 and i + 1 < n and 0x40 <= src[i + 1] <= 0x7F:
            append(src[i + 1] ^ 0x80)
            if i + 1 < last3:
                kk = src[i + 1:i + 4]
                prev[i + 1] = get(kk, -1)
                head[kk] = i + 1
            i += 2
            continue
        if c == 0 or 9 <= c <= 0x7F:
            append(c)
            i += 1
            continue
        j = i + 1
        while j < n and j - i < 8:
            d = src[j]
            if d == 0 or 9 <= d <= 0x7F:
                break
            j += 1
        append(j - i)
        out += src[i:j]
        k = i + 1
        while k < j and k < last3:
            kk = src[k:k + 3]
            prev[k] = get(kk, -1)
            head[kk] = k
            k += 1
        i = j
    return bytes(out)


# --- HUFF/CDIC ------------------------------------------------------------------------------------

HUFF_RECORD_LIMIT = 1024 * 1024  # text records hold 4 KB; a phrase is shorter still


class HuffCdic:
    def __init__(self, huff: bytes, cdics: list[bytes]):
        if huff[:4] != b"HUFF" or len(huff) < 24:
            raise BookportError("Damaged MOBI: bad HUFF record")
        off1, off2 = struct.unpack(">II", huff[8:16])
        self.dict1 = []
        for v in struct.unpack(">256I", huff[off1:off1 + 1024]):
            codelen, term, maxcode = v & 0x1F, v & 0x80, v >> 8
            if codelen == 0:
                raise BookportError("Damaged MOBI: bad HUFF code table")
            self.dict1.append((codelen, term, ((maxcode + 1) << (32 - codelen)) - 1))
        d2 = struct.unpack(">64I", huff[off2:off2 + 256])
        self.mincode = [0] * 33
        self.maxcode = [0] * 33
        for codelen in range(1, 33):
            self.mincode[codelen] = d2[(codelen - 1) * 2] << (32 - codelen)
            self.maxcode[codelen] = ((d2[(codelen - 1) * 2 + 1] + 1) << (32 - codelen)) - 1
        self.dictionary: list = []
        for cdic in cdics:
            if cdic[:4] != b"CDIC":
                raise BookportError("Damaged MOBI: bad CDIC record")
            phrases, bits = struct.unpack(">II", cdic[8:16])
            n = min(1 << bits, phrases - len(self.dictionary))
            offsets = struct.unpack(f">{n}H", cdic[16:16 + n * 2])
            for off in offsets:
                blen = struct.unpack(">H", cdic[16 + off:18 + off])[0]
                self.dictionary.append([cdic[18 + off:18 + off + (blen & 0x7FFF)], bool(blen & 0x8000)])

    def decompress(self, data: bytes, depth: int = 0) -> bytes:
        if depth > 32:
            raise BookportError("Damaged MOBI: HUFF/CDIC recursion too deep")
        bitsleft = len(data) * 8
        data = data + b"\0" * 8
        pos = 0
        x = struct.unpack(">Q", data[0:8])[0]
        n = 32
        out = bytearray()
        dict1, mincode, maxcode_t, dictionary = self.dict1, self.mincode, self.maxcode, self.dictionary
        while True:
            if n <= 0:
                pos += 4
                x = struct.unpack(">Q", data[pos:pos + 8])[0]
                n += 32
            code = (x >> n) & 0xFFFFFFFF
            codelen, term, maxcode = dict1[code >> 24]
            if not term:
                while codelen < 32 and code < mincode[codelen]:
                    codelen += 1
                maxcode = maxcode_t[codelen]
            n -= codelen
            bitsleft -= codelen
            if bitsleft < 0:
                break
            r = (maxcode - code) >> (32 - codelen)
            if r >= len(dictionary):
                raise BookportError("Damaged MOBI: HUFF code out of range")
            entry = dictionary[r]
            piece, done = entry
            if not done:
                piece = self.decompress(piece, depth + 1)
                entry[0], entry[1] = piece, True
            out += piece
            if len(out) > HUFF_RECORD_LIMIT:
                raise BookportError("Damaged MOBI: a HUFF/CDIC record unpacks to more than 1 MB")
        return bytes(out)


# --- variable-width integers ----------------------------------------------------------------------

def vwi_forward(data: bytes, pos: int) -> tuple[int, int]:
    """Forward-encoded VWI: 7 bits per byte, the last byte has the high bit set. (value, bytes used)."""
    value = 0
    for k in range(pos, min(pos + 5, len(data))):
        b = data[k]
        value = (value << 7) | (b & 0x7F)
        if b & 0x80:
            return value, k - pos + 1
    return value, min(5, len(data) - pos)


def encode_vwi_forward(value: int) -> bytes:
    out = [value & 0x7F | 0x80]
    value >>= 7
    while value:
        out.append(value & 0x7F)
        value >>= 7
    return bytes(reversed(out))


def encode_vwi_backward(value: int) -> bytes:
    """Backward-encoded VWI (trailing entries): the first byte has the high bit set."""
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(value & 0x7F)
        value >>= 7
    out = list(reversed(out))
    out[0] |= 0x80
    return bytes(out)


def trailing_size(record: bytes, flags: int) -> int:
    """Bytes of trailing entries at the end of a text record (MOBI 'extra data flags')."""
    size = 0
    test = flags >> 1
    n = len(record)
    while test:
        if test & 1:
            value = shift = 0
            k = n - size
            while k > 0:
                b = record[k - 1]
                value |= (b & 0x7F) << shift
                shift += 7
                k -= 1
                if b & 0x80 or shift >= 28:
                    break
            size += value
        test >>= 1
    if flags & 1 and n - size - 1 >= 0:
        size += (record[n - size - 1] & 3) + 1
    return min(size, n)
