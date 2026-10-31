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


def serialize(v) -> bytes:
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
        return b"[" + b" ".join(serialize(x) for x in v) + b"]"
    if isinstance(v, dict):
        return b"<<" + b"".join(_name(k) + b" " + serialize(x) for k, x in v.items()) + b">>"
    raise TypeError(f"can't write {type(v)} to PDF")


class Writer:
    def __init__(self):
        self.objects: list[bytes | None] = [None]

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
        d["Length"] = len(data)
        body = serialize(d) + b"\nstream\n" + data + b"\nendstream"
        ref = ref or self.reserve()
        self.objects[ref.num] = body
        return ref

    def finish(self, root: Ref, info: Ref | None = None, file_id: bytes = b"") -> bytes:
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
