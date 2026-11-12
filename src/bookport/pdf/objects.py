"""Writing PDF objects (ISO 32000-1, chapter 7): values, streams, cross-reference table, trailer."""

from __future__ import annotations

import zlib


class Name(str):
    """A PDF name (written /Like/This)."""


class Ref:
    __slots__ = ("num",)

    def __init__(self, num: int):
        self.num = num

    def __repr__(self):
        return f"Ref({self.num})"


_NAME_OK = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.+*:")


def _name(s: str) -> bytes:
    out = bytearray(b"/")
    for b in s.encode("utf-8"):
        out += bytes([b]) if b in _NAME_OK else b"#%02X" % b
    return bytes(out)


def text_string(s: str) -> bytes:
    """A text string: PDFDocEncoding when it's plain ASCII, else UTF-16BE with a BOM (as hex)."""
    if all(32 <= ord(c) < 127 for c in s):
        return b"(" + s.encode("ascii").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"
    return b"<FEFF" + s.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _text_bytes(s: str) -> bytes:
    """A text string's bytes as text_string spells them."""
    return s.encode("ascii") if all(32 <= ord(c) < 127 for c in s) else bytes([0xFE, 0xFF]) + s.encode("utf-16-be")


def serialize(v, enc=None) -> bytes:
    """v as PDF text. enc: a function protecting a string's bytes (the file is encrypted), which every string
    in v goes through."""
    if enc is not None and isinstance(v, (str, bytes, bytearray)) and not isinstance(v, Name):
        data = _text_bytes(v) if isinstance(v, str) else bytes(v)
        return b"<" + enc(data).hex().encode("ascii") + b">"
    if isinstance(v, Ref):
        return b"%d 0 R" % v.num
    if isinstance(v, Name):
        return _name(v)
    if isinstance(v, bool):
        return b"true" if v else b"false"
    if isinstance(v, int):
        return b"%d" % v
    if isinstance(v, float):
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return (s if s not in ("-0", "") else "0").encode("ascii")
    if v is None:
        return b"null"
    if isinstance(v, str):
        return text_string(v)
    if isinstance(v, (bytes, bytearray)):
        return b"<" + bytes(v).hex().encode("ascii") + b">"
    if isinstance(v, (list, tuple)):
        return b"[" + b" ".join(serialize(x, enc) for x in v) + b"]"
    if isinstance(v, dict):
        return b"<<" + b"".join(_name(k) + b" " + serialize(x, enc) for k, x in v.items()) + b">>"
    raise TypeError(f"can't write {type(v)} to PDF")


PACK = 200  # objects in each object stream (compact files)


class Writer:
    def __init__(self, encryptor=None):
        """encryptor: a crypt.Encryptor, when the file is to be protected (compact files only)."""
        self.encryptor = encryptor
        self.objects: list[bytes | None] = [None]
        self.streams: set[int] = set()  # objects that are streams (they can't go in an object stream)

    def reserve(self) -> Ref:
        self.objects.append(None)
        return Ref(len(self.objects) - 1)

    def put(self, ref: Ref, value) -> Ref:
        self.objects[ref.num] = serialize(value)
        return ref

    def add(self, value) -> Ref:
        return self.put(self.reserve(), value)

    def stream(self, data: bytes, extra: dict | None = None, compress: bool = True, ref: Ref | None = None) -> Ref:
        d = dict(extra or {})
        if compress and "Filter" not in d:
            data = zlib.compress(data, 6)
            d["Filter"] = Name("FlateDecode")
        enc = self.encryptor.encrypt if self.encryptor else None
        if enc:
            data = enc(data)
        d["Length"] = len(data)
        body = serialize(d, enc) + b"\nstream\n" + data + b"\nendstream"
        ref = ref or self.reserve()
        self.objects[ref.num] = body
        self.streams.add(ref.num)
        return ref

    def finish(self, root: Ref, info: Ref | None = None, file_id: bytes = b"", compact: bool = False) -> bytes:
        """The file. compact: every object that isn't a stream packed into compressed object streams, with a
        cross-reference stream (PDF 1.5 and later readers), much smaller for files of many small objects."""
        if compact:
            return self._finish_compact(root, info, file_id)
        if self.encryptor:
            raise ValueError("an encrypted file is written compact")
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * len(self.objects)
        for num in range(1, len(self.objects)):
            body = self.objects[num]
            if body is None:
                body = b"null"
            offsets[num] = len(out)
            out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % len(self.objects)
        for num in range(1, len(self.objects)):
            out += b"%010d 00000 n \n" % offsets[num]
        trailer = {"Size": len(self.objects), "Root": root}
        if info is not None:
            trailer["Info"] = info
        if file_id:
            trailer["ID"] = [file_id, file_id]
        out += b"trailer\n" + serialize(trailer) + b"\nstartxref\n%d\n%%%%EOF\n" % xref
        return bytes(out)

    def _finish_compact(self, root: Ref, info: Ref | None, file_id: bytes) -> bytes:
        loose = [n for n in range(1, len(self.objects)) if n not in self.streams]
        entries: dict[int, tuple[int, int, int]] = {}  # num -> (type, offset or object stream, index)
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        for num in sorted(self.streams):
            entries[num] = (1, len(out), 0)
            out += b"%d 0 obj\n" % num + self.objects[num] + b"\nendobj\n"
        encrypt = None
        if self.encryptor:  # the Encrypt dictionary: its own object, not packed and not itself encrypted
            encrypt = Ref(len(self.objects))
            self.objects.append(None)
            entries[encrypt.num] = (1, len(out), 0)
            out += b"%d 0 obj\n" % encrypt.num + serialize(self.encryptor.dictionary()) + b"\nendobj\n"
        for start in range(0, len(loose), PACK):
            group = loose[start:start + PACK]
            stm = len(self.objects)
            self.objects.append(None)
            head, body = bytearray(), bytearray()
            for i, num in enumerate(group):
                head += b"%d %d " % (num, len(body))
                body += (self.objects[num] or b"null") + b"\n"
                entries[num] = (2, stm, i)
            data = zlib.compress(bytes(head) + bytes(body), 9)
            if self.encryptor:  # the strings inside are protected with the stream, not one by one
                data = self.encryptor.encrypt(data)
            d = {"Type": Name("ObjStm"), "N": len(group), "First": len(head), "Filter": Name("FlateDecode"),
                 "Length": len(data)}
            entries[stm] = (1, len(out), 0)
            out += b"%d 0 obj\n" % stm + serialize(d) + b"\nstream\n" + data + b"\nendstream\nendobj\n"
        xref = len(self.objects)
        entries[xref] = (1, len(out), 0)
        size = xref + 1
        rows = bytearray(b"\x00\x00\x00\x00\x00\xff\xff")  # object 0: free
        for num in range(1, size):
            kind, a, b = entries.get(num, (0, 0, 0))
            rows += bytes([kind]) + a.to_bytes(4, "big") + b.to_bytes(2, "big")
        data = zlib.compress(bytes(rows), 9)
        d = {"Type": Name("XRef"), "Size": size, "W": [1, 4, 2], "Root": root, "Filter": Name("FlateDecode"),
             "Length": len(data)}
        if info is not None:
            d["Info"] = info
        if encrypt is not None:
            d["Encrypt"] = encrypt
        if file_id:
            d["ID"] = [file_id, file_id]
        out += b"%d 0 obj\n" % xref + serialize(d) + b"\nstream\n" + data + b"\nendstream\nendobj\n"
        out += b"startxref\n%d\n%%%%EOF\n" % entries[xref][1]
        return bytes(out)
