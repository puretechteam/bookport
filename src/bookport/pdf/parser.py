"""Parsing PDF files (ISO 32000-1, chapter 7): objects, cross-reference tables and streams,
object streams, incremental updates, damaged-file repair, filters and decryption."""

from __future__ import annotations

import io
import itertools
import re
import zlib

from .. import limits
from ..errors import BookportError


class Name(str):
    pass


class Ref:
    __slots__ = ("gen", "num")

    def __init__(self, num, gen=0):
        self.num, self.gen = num, gen

    def __repr__(self):
        return f"Ref({self.num},{self.gen})"


class Stream:
    __slots__ = ("dict", "gen", "num", "raw")

    def __init__(self, d, raw, num=0, gen=0):
        self.dict, self.raw, self.num, self.gen = d, raw, num, gen

    def get(self, k, default=None):
        return self.dict.get(k, default)


class PdfString(bytes):
    pass


WS = b"\x00\t\n\x0c\r "
_TOKEN = re.compile(
    rb"(?P<ws>[\x00\t\n\x0c\r ]+|%[^\r\n]*)"
    rb"|(?P<num>[+-]?(?:\d+\.?\d*|\.\d+))(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])"
    rb"|(?P<name>/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*)"
    rb"|(?P<dopen><<)|(?P<dclose>>>)|(?P<aopen>\[)|(?P<aclose>\])"
    rb"|(?P<hex><[0-9A-Fa-f\x00\t\n\x0c\r ]*>)"
    rb"|(?P<str>\()"
    rb"|(?P<kw>[^\x00\t\n\x0c\r ()<>\[\]{}/%]+)"
    rb"|(?P<other>.)", re.DOTALL)
_NAME_ESC = re.compile(rb"#([0-9A-Fa-f]{2})")
_STR_ESC = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f", ord("("): b"(",
            ord(")"): b")", ord("\\"): b"\\"}


def _literal(data: bytes, pos: int) -> tuple[bytes, int]:
    """A literal string starting after its '('. Returns (bytes, position after ')')."""
    out = bytearray()
    depth = 1
    n = len(data)
    i = pos
    while i < n:
        c = data[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                break
            e = data[i]
            if e in _STR_ESC:
                out += _STR_ESC[e]
                i += 1
            elif 0x30 <= e <= 0x37:
                j = i
                while j < n and j < i + 3 and 0x30 <= data[j] <= 0x37:
                    j += 1
                out.append(int(data[i:j], 8) & 0xFF)
                i = j
            elif e in (0x0D, 0x0A):
                i += 1
                if e == 0x0D and i < n and data[i] == 0x0A:
                    i += 1
            else:
                out.append(e)
                i += 1
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out.append(c)
        i += 1
    return bytes(out), n


def _name(raw: bytes) -> Name:
    raw = raw[1:]
    if b"#" in raw:
        raw = _NAME_ESC.sub(lambda m: bytes([int(m.group(1), 16)]), raw)
    return Name(raw.decode("latin-1"))


def tokens(data: bytes, pos: int = 0, end: int | None = None):
    """Yield (kind, value, end position) for each token."""
    end = len(data) if end is None else end
    match = _TOKEN.match
    while pos < end:
        m = match(data, pos)
        if m is None:
            break
        kind = m.lastgroup
        pos = m.end()
        if kind == "ws":
            continue
        raw = m.group(kind)
        if kind == "num":
            yield "v", (int(raw) if b"." not in raw else float(raw)), pos
        elif kind == "name":
            yield "v", _name(raw), pos
        elif kind == "str":
            s, pos = _literal(data, pos)
            yield "v", PdfString(s), pos
        elif kind == "hex":
            h = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
            if len(h) % 2:
                h += b"0"
            yield "v", PdfString(bytes.fromhex(h.decode("ascii"))), pos
        elif kind in ("dopen", "dclose", "aopen", "aclose"):
            yield kind, None, pos
        elif kind == "kw":
            if raw == b"true":
                yield "v", True, pos
            elif raw == b"false":
                yield "v", False, pos
            elif raw == b"null":
                yield "v", None, pos
            else:
                yield "kw", raw, pos
        else:
            yield "other", raw, pos


_CONTENT = re.compile(
    rb"\((?:[^()\\]|\\.|\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\))*\)"  # literal strings, nested up to 3 deep
    rb"|<<|>>|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s()<>\[\]{}/%]*|%[^\r\n]*|[^\s()<>\[\]{}/%]+|[()]", re.DOTALL)
_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)\Z")
_KEYWORDS = {b"true": True, b"false": False, b"null": None}


def content_tokens(data: bytes) -> list[tuple[str, object]]:
    """Fast tokens for content streams and CMaps: [(kind, value)], kinds as in ``tokens``."""
    out = []
    append = out.append
    number = _NUMBER.match
    for tok in _CONTENT.findall(data):
        c = tok[0]
        if c == 0x28:  # (
            s, _ = _literal(tok, 1)
            append(("v", PdfString(s)))
        elif c == 0x2F:  # /
            append(("v", _name(tok)))
        elif c == 0x3C:  # <
            if tok == b"<<":
                append(("dopen", None))
            else:
                h = re.sub(rb"[^0-9A-Fa-f]", b"", tok)
                if len(h) % 2:
                    h += b"0"
                append(("v", PdfString(bytes.fromhex(h.decode("ascii")))))
        elif c == 0x3E:
            append(("dclose", None))
        elif c == 0x5B:
            append(("aopen", None))
        elif c == 0x5D:
            append(("aclose", None))
        elif c == 0x25:  # % comment
            continue
        elif (48 <= c <= 57 or c in (43, 45, 46)) and number(tok):
            append(("v", int(tok) if b"." not in tok else float(tok)))
        elif tok in _KEYWORDS:
            append(("v", _KEYWORDS[tok]))
        else:
            append(("kw", tok))
    return out


def parse_value(data: bytes, pos: int):
    """One object at ``pos`` (dict, array, number, name, string, reference). Returns (value, end)."""
    stack: list = []
    gen = tokens(data, pos)
    for kind, value, end in gen:
        if kind == "v":
            item = value
        elif kind == "dopen":
            stack.append({})
            stack.append("dict-marker")
            continue
        elif kind == "aopen":
            stack.append([])
            stack.append("array-marker")
            continue
        elif kind == "aclose":
            items = []
            while stack and stack[-1] != "array-marker":
                items.append(stack.pop())
            if stack:
                stack.pop()
                arr = stack.pop()
                arr.extend(reversed(items))
                item = arr
            else:
                item = None
        elif kind == "dclose":
            items = []
            while stack and stack[-1] != "dict-marker":
                items.append(stack.pop())
            items.reverse()
            if stack:
                stack.pop()
                d = stack.pop()
                for k in range(0, len(items) - 1, 2):
                    if isinstance(items[k], Name):
                        d[str(items[k])] = items[k + 1]
                item = d
            else:
                item = None
        elif kind == "kw" and value == b"R":
            # num gen R
            if len(stack) >= 2 and isinstance(stack[-1], int) and isinstance(stack[-2], int):
                g = stack.pop()
                n = stack.pop()
                item = Ref(n, g)
            else:
                continue
        elif kind == "kw":
            if not stack:
                return None, end
            continue
        else:
            continue
        if not stack:
            # a number may be the start of a reference: look ahead
            if isinstance(item, int):
                look = list(_peek(data, end, 2))
                if len(look) == 2 and look[0][0] == "v" and isinstance(look[0][1], int) and look[1][0] == "kw" \
                        and look[1][1] == b"R":
                    return Ref(item, look[0][1]), look[1][2]
            return item, end
        stack.append(item)
    return (stack[0] if stack and not isinstance(stack[0], str) else None), len(data)


def _peek(data, pos, n):
    for k, tok in enumerate(tokens(data, pos)):
        if k >= n:
            break
        yield tok


# --- filters -------------------------------------------------------------------------------------

def _flate(data: bytes) -> bytes:
    # damaged streams give what decodes before the damage; some writers leave out the zlib header
    out = limits.inflate(data, partial=True)
    return out or limits.inflate(data[2:], -15, partial=True)


def _predict(data: bytes, parms: dict) -> bytes:
    pred = parms.get("Predictor", 1)
    if not isinstance(pred, int) or pred < 2:
        return data
    colors = parms.get("Colors", 1) or 1
    bpc = parms.get("BitsPerComponent", 8) or 8
    cols = parms.get("Columns", 1) or 1
    if not all(isinstance(v, int) for v in (colors, bpc, cols)) or not (0 < colors <= 32 and bpc in (1, 2, 4, 8, 16)
                                                                         and 0 < cols <= 1 << 20):
        raise BookportError("Damaged PDF: bad predictor parameters")
    bpp = max(1, colors * bpc // 8)
    row = (colors * bpc * cols + 7) // 8
    if pred == 2:
        out = bytearray(data)
        for r in range(0, len(out), row):
            for i in range(r + bpp, min(r + row, len(out))):
                out[i] = (out[i] + out[i - bpp]) & 0xFF
        return bytes(out)
    if bpc == 8 and colors <= 4 and len(data) >= 1 << 16:
        got = _png_rows(data, colors, cols, row)
        if got is not None:
            return got
    out = bytearray()
    prev = bytearray(row)
    hi = int.from_bytes(b"\x80" * row, "big")  # for Up: bytewise sums of two rows as one big-integer addition
    lo = hi ^ ((1 << (8 * row)) - 1)
    pos = 0
    while pos + 1 + row <= len(data) or pos + 1 < len(data):
        ft = data[pos]
        line = bytearray(data[pos + 1:pos + 1 + row])
        line += bytes(row - len(line))
        pos += 1 + row
        if ft == 1:
            for k in range(bpp):  # each byte plus the one bpp before it: running sums, one per byte of a pixel
                line[k::bpp] = bytes(map(_BYTE, itertools.accumulate(line[k::bpp])))
        elif ft == 2:
            x, y = int.from_bytes(line, "big"), int.from_bytes(prev, "big")
            line = bytearray((((x & lo) + (y & lo)) ^ ((x ^ y) & hi)).to_bytes(row, "big"))
        elif ft == 3:
            for i in range(row):
                line[i] = (line[i] + (((line[i - bpp] if i >= bpp else 0) + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(row):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out += line
        prev = line
    return bytes(out)


_BYTE = (255).__and__


def _png_rows(data: bytes, colors: int, cols: int, row: int) -> bytes | None:
    """PNG-predicted rows (8 bits a sample) undone by Pillow, in C (about 25 times faster than the loop in
    _predict); None without Pillow, or for rows it won't take (a filter type past 4)."""
    try:
        import warnings

        from PIL import Image
    except ImportError:
        return None
    rows, left = divmod(len(data), row + 1)
    if left > 1:  # a short last row is read as if the rest were zeros, as _predict reads it
        data = data + bytes(row + 1 - left)
        rows += 1
    elif left:
        data = data[:-1]
    if not rows:
        return None
    mode, kind = {1: ("L", 0), 2: ("LA", 4), 3: ("RGB", 2), 4: ("RGBA", 6)}[colors]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + tag + body + zlib.crc32(tag + body).to_bytes(4, "big")
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", cols.to_bytes(4, "big") + rows.to_bytes(4, "big") + bytes((8, kind, 0, 0, 0)))
           + chunk(b"IDAT", zlib.compress(data, 0)) + chunk(b"IEND", b""))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # a big picture is no bomb here: its bytes are already in memory
            img = Image.open(io.BytesIO(png))
            img.load()
        if img.mode != mode:
            return None
        return img.tobytes()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _lzw(data: bytes, early: int = 1) -> bytes:
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    size = 9
    prev = None
    buf = nbits = 0
    for byte in data:
        buf = (buf << 8) | byte
        nbits += 8
        while nbits >= size:
            code = (buf >> (nbits - size)) & ((1 << size) - 1)
            nbits -= size
            if code == 256:
                table = table[:258]
                size = 9
                prev = None
                continue
            if code == 257:
                return bytes(out)
            if prev is None:
                entry = table[code] if code < len(table) else b""
            elif code < len(table):
                entry = table[code]
                table.append(prev + entry[:1])
            else:
                entry = prev + prev[:1]
                table.append(entry)
            out += entry
            if len(out) > limits.MAX_PART:
                limits.grow(out)
            prev = entry
            if len(table) + early >= (1 << size) and size < 12:
                size += 1
            if len(table) >= 4096:  # a full table waits for a clear code; it never grows past 12 bits
                prev = None
    limits.charge(len(out))
    return bytes(out)


def _a85(data: bytes) -> bytes:
    data = re.sub(rb"\s", b"", data)
    if data.startswith(b"<~"):
        data = data[2:]
    end = data.find(b"~>")
    if end >= 0:
        data = data[:end]
    out = bytearray()
    group = []
    for c in data:
        if c == ord("z") and not group:
            out += b"\0\0\0\0"
            continue
        if not 33 <= c <= 117:
            continue
        group.append(c - 33)
        if len(group) == 5:
            v = 0
            for g in group:
                v = v * 85 + g
            out += (v & 0xFFFFFFFF).to_bytes(4, "big")
            group = []
    if group:
        n = len(group)
        group += [84] * (5 - n)
        v = 0
        for g in group:
            v = v * 85 + g
        out += (v & 0xFFFFFFFF).to_bytes(4, "big")[:n - 1]
    return bytes(out)


def _ahx(data: bytes) -> bytes:
    data = re.sub(rb"[^0-9A-Fa-f>]", b"", data).split(b">")[0]
    if len(data) % 2:
        data += b"0"
    return bytes.fromhex(data.decode("ascii"))


def _rl(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        n = data[i]
        if n == 128:
            break
        if n < 128:
            out += data[i + 1:i + 2 + n]
            i += 2 + n
        else:
            out += data[i + 1:i + 2] * (257 - n)
            i += 2
        if len(out) > limits.MAX_PART:  # up to 64 times its size: held to the limits as LZW and Flate are
            limits.grow(out)
    limits.charge(len(out))
    return bytes(out)


IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"}


# --- the document ------------------------------------------------------------------------------------

class Document:
    def __init__(self, data: bytes):
        if b"%PDF" not in data[:1024]:
            raise BookportError("Not a PDF file")
        self.data = data
        self.xref: dict[int, tuple] = {}
        self.trailer: dict = {}
        self.cache: dict[int, object] = {}
        self.crypt = None
        self._objstm_cache: dict[int, list] = {}
        self._scanned: dict[int, int] | None = None  # object number -> offset, from a scan, made when needed
        try:
            self._read_xref()
        except (BookportError, ValueError, IndexError, KeyError, TypeError):
            self.xref, self.trailer = {}, {}
        if not self.xref or "Root" not in self.trailer:
            self._rebuild()
        enc = self.trailer.get("Encrypt")
        if enc is not None:
            from .crypt import Decryptor
            ids = self.resolve(self.trailer.get("ID")) or [b""]
            first = bytes(self.resolve(ids[0])) if ids else b""
            self.crypt = None  # the Encrypt dictionary itself is never encrypted
            encd = self.resolve(enc)
            self.crypt = Decryptor(encd, first, self.resolve)
            self._enc_num = enc.num if isinstance(enc, Ref) else -1
            self.cache.clear()

    # --- cross reference ----------------------------------------------------------------------
    def _read_xref(self):
        data = self.data
        idx = data.rfind(b"startxref")
        if idx < 0:
            raise BookportError("no startxref")
        m = re.match(rb"startxref\s+(\d+)", data[idx:idx + 40])
        if not m:
            raise BookportError("bad startxref")
        offset = int(m.group(1))
        seen = set()
        while offset and offset not in seen and offset < len(data):
            seen.add(offset)
            trailer = self._xref_section(offset)
            for k, v in trailer.items():
                self.trailer.setdefault(k, v)
            if isinstance(trailer.get("XRefStm"), int):
                try:
                    self._xref_section(trailer["XRefStm"])
                except (BookportError, ValueError, IndexError, KeyError):
                    pass
            prev = trailer.get("Prev")
            offset = prev if isinstance(prev, int) else 0

    def _xref_section(self, offset: int) -> dict:
        data = self.data
        pos = offset
        while pos < len(data) and data[pos] in WS:
            pos += 1
        if data.startswith(b"xref", pos):
            pos += 4
            while True:
                m = re.compile(rb"\s*(\d+)\s+(\d+)\s*").match(data, pos)
                if not m:
                    break
                start, count = int(m.group(1)), int(m.group(2))
                pos = m.end()
                for k in range(count):
                    entry = data[pos:pos + 20]
                    em = re.match(rb"(\d{10})\s(\d{5})\s([nf])", entry)
                    if not em:
                        em2 = re.compile(rb"\s*(\d+)\s+(\d+)\s+([nf])").match(data, pos)
                        if not em2:
                            break
                        off, g, t = int(em2.group(1)), int(em2.group(2)), em2.group(3)
                        pos = em2.end()
                    else:
                        off, g, t = int(em.group(1)), int(em.group(2)), em.group(3)
                        pos += 20
                    if t == b"n" and start + k not in self.xref and off > 0:
                        self.xref[start + k] = (1, off, g)
                    elif t == b"f":
                        self.xref.setdefault(start + k, (0, 0, 0))
            tpos = data.find(b"trailer", pos)
            if tpos < 0:
                return {}
            trailer, _ = parse_value(data, tpos + 7)
            return trailer if isinstance(trailer, dict) else {}
        # a cross-reference stream
        obj = self._parse_indirect_at(pos)
        if not isinstance(obj, Stream) or obj.dict.get("Type") != "XRef":
            raise BookportError("bad xref")
        d = obj.dict
        raw = self.decode(obj)
        w = d.get("W", [1, 2, 1])
        index = d.get("Index", [0, d.get("Size", 0)])
        if not isinstance(w, list) or len(w) != 3 or not all(isinstance(x, int) and 0 <= x <= 8 for x in w) \
                or not isinstance(index, list) or not all(isinstance(x, int) and x >= 0 for x in index):
            raise BookportError("bad xref stream")
        entry = sum(w)
        if entry == 0:
            raise BookportError("bad xref stream")
        p = 0
        for i in range(0, len(index) - 1, 2):
            start, count = index[i], index[i + 1]
            for k in range(min(count, (len(raw) - p) // entry + 1)):
                fields = []
                for width in w:
                    v = int.from_bytes(raw[p:p + width], "big") if width else None
                    fields.append(v)
                    p += width
                t = fields[0] if w[0] else 1
                num = start + k
                if num in self.xref:
                    continue
                if t == 1:
                    self.xref[num] = (1, fields[1], fields[2] or 0)
                elif t == 2:
                    self.xref[num] = (2, fields[1], fields[2])
                else:
                    self.xref[num] = (0, 0, 0)
                if p > len(raw) + entry:
                    break
        return d

    def _rebuild(self):
        """Scan the file for 'n g obj' to rebuild a damaged cross-reference table."""
        self.xref = {}
        for m in re.finditer(rb"(?<![\d])(\d+)\s+(\d+)\s+obj\b", self.data):
            self.xref[int(m.group(1))] = (1, m.start(), int(m.group(2)))
        trailer = {}
        for m in re.finditer(rb"trailer", self.data):
            t, _ = parse_value(self.data, m.end())
            if isinstance(t, dict):
                trailer.update(t)
        if "Root" not in trailer:
            for num in self.xref:
                obj = self.get(num)
                if isinstance(obj, dict) and obj.get("Type") == "Catalog":
                    trailer["Root"] = Ref(num, 0)
                    break
                if isinstance(obj, Stream) and obj.dict.get("Type") == "XRef" and "Root" in obj.dict:
                    trailer.update(obj.dict)
        if "Root" not in trailer:
            raise BookportError("Damaged PDF: no document catalog")
        self.trailer = trailer

    # --- objects ------------------------------------------------------------------------------
    def _parse_indirect_at(self, pos: int, expect: int | None = None):
        """The object at pos; with expect, a BookportError unless it's that object (a wrong offset)."""
        data = self.data
        m = re.compile(rb"\s*(\d+)\s+(\d+)\s+obj").match(data, pos)
        if not m:
            raise BookportError("bad object")
        num, gen = int(m.group(1)), int(m.group(2))
        if expect is not None and num != expect:
            raise BookportError("bad object")
        value, end = parse_value(data, m.end())
        m2 = re.compile(rb"\s*stream(\r\n|\n|\r)?").match(data, end)
        if m2 and isinstance(value, dict):
            start = m2.end()
            length = value.get("Length")
            if isinstance(length, Ref):
                length = self._length(length)
            if not isinstance(length, int) or length < 0 or start + length > len(data) or \
                    not re.match(rb"\s*endstream", data[start + length:start + length + 20]):
                stop = data.find(b"endstream", start)
                length = (stop if stop >= 0 else len(data)) - start
                while length > 0 and data[start + length - 1] in b"\r\n":
                    length -= 1
            raw = data[start:start + length]
            return Stream(value, raw, num, gen)
        if self.crypt is not None:
            value = self._decrypt_strings(value, num, gen)
        return value

    def _length(self, ref: Ref):
        entry = self.xref.get(ref.num)
        if entry and entry[0] == 1:
            try:
                return self._at(entry[1], ref.num)
            except BookportError:
                return None
        return self.get(ref.num)

    def _decrypt_strings(self, v, num, gen):
        if isinstance(v, PdfString):
            return PdfString(self.crypt.decrypt(bytes(v), num, gen, stream=False))
        if isinstance(v, list):
            return [self._decrypt_strings(x, num, gen) for x in v]
        if isinstance(v, dict):
            return {k: self._decrypt_strings(x, num, gen) for k, x in v.items()}
        return v

    def get(self, num: int):
        if num in self.cache:
            return self.cache[num]
        entry = self.xref.get(num)
        value = None
        if entry is not None:
            try:
                if entry[0] == 1:
                    value = self._at(entry[1], num)
                elif entry[0] == 2:
                    value = self._from_objstm(entry[1], entry[2])
            except limits.LimitError:
                raise
            except (BookportError, ValueError, IndexError, RecursionError):
                value = None
        if isinstance(value, Stream) and self.crypt is not None and num != getattr(self, "_enc_num", -1):
            value.dict = self._decrypt_strings(value.dict, value.num, value.gen)
        self.cache[num] = value
        return value

    def _at(self, offset: int, num: int):
        """Object num at offset, or where a scan of the file finds it when the offset is wrong (junk before
        %PDF, a file edited by hand: every offset off by the same amount)."""
        try:
            return self._parse_indirect_at(offset, num)
        except BookportError:
            if self._scanned is None:
                self._scanned = {int(m.group(1)): m.start()
                                 for m in re.finditer(rb"(?<![\d])(\d+)\s+(\d+)\s+obj\b", self.data)}
            found = self._scanned.get(num)
            if found is None or found == offset:
                raise
            return self._parse_indirect_at(found, num)

    def _from_objstm(self, stm_num: int, index: int):
        objs = self._objstm_cache.get(stm_num)
        if objs is None:
            stm = self.get(stm_num)
            if not isinstance(stm, Stream):
                return None
            data = self.decode(stm)
            n = stm.dict.get("N", 0)
            first = stm.dict.get("First", 0)
            if not isinstance(n, int) or not isinstance(first, int) or first < 0:
                return None
            head = []
            toks = [v for k, v, _e in tokens(data, 0, first) if k == "v"]
            for i in range(0, min(len(toks), 2 * n), 2):
                head.append((toks[i], toks[i + 1]))
            objs = []
            for _onum, off in head:
                if not isinstance(off, int) or off < 0:
                    objs.append(None)
                    continue
                v, _ = parse_value(data, first + off)
                objs.append(v)
            self._objstm_cache[stm_num] = objs
        return objs[index] if index < len(objs) else None

    def resolve(self, v, depth=0):
        while isinstance(v, Ref) and depth < 32:
            v = self.get(v.num)
            depth += 1
        return v

    # --- streams ------------------------------------------------------------------------------
    def decode(self, stm: Stream) -> bytes:
        data = stm.raw
        if self.crypt is not None and stm.num and stm.dict.get("Type") != "XRef":
            data = self.crypt.decrypt(data, stm.num, stm.gen, stream=True)
        filters = self.resolve(stm.dict.get("Filter"))
        parms = self.resolve(stm.dict.get("DecodeParms") or stm.dict.get("DP"))
        if filters is None:
            return data
        if not isinstance(filters, list):
            filters = [filters]
            parms = [parms]
        elif not isinstance(parms, list):
            parms = [parms] * len(filters)
        for f, p in zip(filters, parms + [None] * (len(filters) - len(parms)) if parms else [None] * len(filters)):
            f = str(self.resolve(f))
            p = self.resolve(p)
            if isinstance(p, list) and len(p) == 1:  # [<< ... >>] for a single filter
                p = self.resolve(p[0])
            p = {k: self.resolve(v) for k, v in p.items()} if isinstance(p, dict) else {}
            if f in ("FlateDecode", "Fl"):
                data = _predict(_flate(data), p)
            elif f in ("LZWDecode", "LZW"):
                data = _predict(_lzw(data, p.get("EarlyChange", 1)), p)
            elif f in ("ASCII85Decode", "A85"):
                data = _a85(data)
            elif f in ("ASCIIHexDecode", "AHx"):
                data = _ahx(data)
            elif f in ("RunLengthDecode", "RL"):
                data = _rl(data)
            elif f == "Crypt":
                continue
            else:
                return data  # a picture's own filter (DCT, JPX, CCITT, JBIG2), or one this reader doesn't know
        return data

    def stream_filters(self, stm: Stream) -> list[str]:
        f = self.resolve(stm.dict.get("Filter"))
        if f is None:
            return []
        return [str(self.resolve(x)) for x in (f if isinstance(f, list) else [f])]

    # --- structure ----------------------------------------------------------------------------
    @property
    def root(self) -> dict:
        r = self.resolve(self.trailer.get("Root"))
        if not isinstance(r, dict):
            raise BookportError("Damaged PDF: no document catalog")
        return r

    def info(self) -> dict:
        i = self.resolve(self.trailer.get("Info"))
        return i if isinstance(i, dict) else {}

    def pages(self) -> list[dict]:
        out = []
        seen = set()

        def walk(node_ref, inherited, depth):
            node = self.resolve(node_ref)
            if not isinstance(node, dict) or depth > 64:
                return
            key = node_ref.num if isinstance(node_ref, Ref) else id(node)
            if key in seen:
                return
            seen.add(key)
            inh = dict(inherited)
            for k in ("Resources", "MediaBox", "CropBox", "Rotate"):
                if k in node:
                    inh[k] = node[k]
            kids = self.resolve(node.get("Kids"))
            if node.get("Type") == "Pages" or isinstance(kids, list) and node.get("Type") != "Page":
                for kid in kids or []:
                    walk(kid, inh, depth + 1)
            else:
                page = dict(inh)
                page.update(node)
                page["_ref"] = node_ref
                out.append(page)

        walk(self.root.get("Pages"), {}, 0)
        return out


def named_dests(doc: Document, most: int = 100_000) -> dict:
    """Every named destination: the catalog's Dests dictionary and the Names tree's Dests, name (as str,
    a PDF string read as Latin-1) -> value."""
    found: dict = {}
    root = doc.root
    old = doc.resolve(root.get("Dests"))
    if isinstance(old, dict):
        for k, v in old.items():
            found[str(k)] = v
    names = doc.resolve(root.get("Names"))
    tree = doc.resolve(names.get("Dests")) if isinstance(names, dict) else None
    stack, seen = [tree], set()
    while stack and len(found) < most:
        node = stack.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        pairs = doc.resolve(node.get("Names"))
        if isinstance(pairs, list):
            for i in range(0, len(pairs) - 1, 2):
                key = doc.resolve(pairs[i])
                found[bytes(key).decode("latin-1") if isinstance(key, bytes) else str(key)] = pairs[i + 1]
        kids = doc.resolve(node.get("Kids"))
        if isinstance(kids, list):
            stack.extend(doc.resolve(k) for k in kids)
    return found


def text_string(v) -> str:
    """A PDF text string (PDFDocEncoding, UTF-16 with BOM, or UTF-8 with BOM) as str."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    b = bytes(v)
    if b[:2] == b"\xfe\xff":
        return b[2:].decode("utf-16-be", "replace")
    if b[:2] == b"\xff\xfe":
        return b[2:].decode("utf-16-le", "replace")
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "replace")
    from .encodings import PDFDOC
    return "".join(PDFDOC.get(c, chr(c)) for c in b)
