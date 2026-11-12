"""Mobipocket INDX indexes: read and write (TAGX tag tables, IDXT, CNCX string records).

Used for the NCX (table of contents) in MOBI 6 and KF8, and for KF8's skeleton, fragment and
guide indexes.
"""

from __future__ import annotations

import struct

from .errors import BookportError
from .palm import encode_vwi_forward, vwi_forward


def _bits(x: int) -> int:
    return x.bit_count() if hasattr(x, "bit_count") else bin(x).count("1")


def parse_tagx(rec: bytes, start: int):
    if rec[start:start + 4] != b"TAGX":
        raise BookportError("Damaged MOBI index: no TAGX table")
    length, control_bytes = struct.unpack(">II", rec[start + 4:start + 12])
    end = min(start + length, len(rec))
    tags = [tuple(rec[i:i + 4]) for i in range(start + 12, end - 3, 4)]
    return control_bytes, tags


def _entry_tags(data: bytes, pos: int, control_bytes: int, tagx) -> dict[int, list[int]]:
    cbytes = data[pos:pos + control_bytes]
    pos += control_bytes
    plan = []
    ci = 0
    for tag, per_entry, mask, end in tagx:
        if end == 1:
            ci += 1
            continue
        if ci >= len(cbytes):
            break
        value = cbytes[ci] & mask
        if not value:
            continue
        if value == mask:
            if _bits(mask) > 1:
                nbytes, used = vwi_forward(data, pos)
                pos += used
                plan.append((tag, None, nbytes, per_entry))
            else:
                plan.append((tag, 1, None, per_entry))
        else:
            m = mask
            while m & 1 == 0:
                m >>= 1
                value >>= 1
            plan.append((tag, value, None, per_entry))
    out: dict[int, list[int]] = {}
    for tag, count, nbytes, per_entry in plan:
        values = []
        if count is not None:
            for _ in range(count * per_entry):
                if pos >= len(data):  # past the entry: a damaged count would add zeros for ever
                    break
                v, used = vwi_forward(data, pos)
                pos += used
                values.append(v)
        else:
            total = 0
            while total < nbytes and pos < len(data):
                v, used = vwi_forward(data, pos)
                pos += used
                total += used
                values.append(v)
        out[tag] = values
    return out


def read_index(records: list[bytes], first: int, encoding: str = "utf-8"):
    """Entries of the index whose header record is ``records[first]``.

    Returns (entries, cncx) where entries is a list of (ident string, {tag: [values]}) and cncx
    maps an offset to its string.
    """
    if first is None or first >= len(records) or first < 0:
        return [], {}
    head = records[first]
    if head[:4] != b"INDX":
        raise BookportError("Damaged MOBI: index record isn't INDX")
    header_len = struct.unpack(">I", head[4:8])[0]
    nrecords = struct.unpack(">I", head[24:28])[0]
    ncncx = struct.unpack(">I", head[52:56])[0]
    control_bytes, tagx = parse_tagx(head, header_len)
    entries = []
    for k in range(1, nrecords + 1):
        if first + k >= len(records):
            break
        rec = records[first + k]
        if rec[:4] != b"INDX":
            break
        idxt = struct.unpack(">I", rec[20:24])[0]
        count = struct.unpack(">I", rec[24:28])[0]
        if rec[idxt:idxt + 4] != b"IDXT":
            raise BookportError("Damaged MOBI index: no IDXT")
        offsets = [struct.unpack(">H", rec[idxt + 4 + 2 * i:idxt + 6 + 2 * i])[0] for i in range(count)]
        offsets.append(idxt)
        for i in range(count):
            start, end = offsets[i], offsets[i + 1]
            n = rec[start]
            ident = rec[start + 1:start + 1 + n]
            tags = _entry_tags(rec[:end], start + 1 + n, control_bytes, tagx)
            entries.append((ident.decode(encoding, "replace"), tags))
    cncx = {}
    for k in range(ncncx):
        idx = first + nrecords + 1 + k
        if idx >= len(records):
            break
        rec = records[idx]
        pos = 0
        while pos < len(rec):
            start = pos
            length, used = vwi_forward(rec, pos)
            pos += used
            if length == 0 and used == 1 and rec[start] == 0:
                break
            cncx[k * 0x10000 + start] = rec[pos:pos + length].decode(encoding, "replace")
            pos += length
    return entries, cncx


# --- writing -----------------------------------------------------------------------------------------

class CNCX:
    """String records for an index: offsets are (record << 16) | position."""

    def __init__(self):
        self.records: list[bytearray] = [bytearray()]
        self.offsets: dict[str, int] = {}

    def add(self, s: str) -> int:
        if s in self.offsets:
            return self.offsets[s]
        data = s.encode("utf-8")[:500]
        blob = encode_vwi_forward(len(data)) + data
        if len(self.records[-1]) + len(blob) > 0xFBF8:
            self.records.append(bytearray())
        rec = self.records[-1]
        off = (len(self.records) - 1) * 0x10000 + len(rec)
        rec += blob
        self.offsets[s] = off
        return off

    def finish(self) -> list[bytes]:
        out = []
        for r in self.records:
            if r:
                r = bytes(r) + b"\0" * ((4 - len(r) % 4) % 4)
                out.append(r)
        return out


def _control_and_values(tagx, tags: dict[int, list[int]], control_bytes: int):
    cbytes = [0] * control_bytes
    values = bytearray()
    ci = 0
    for tag, per_entry, mask, end in tagx:
        if end == 1:
            ci += 1
            continue
        vals = tags.get(tag)
        if not vals:
            continue
        count = len(vals) // per_entry
        shift = (mask & -mask).bit_length() - 1
        if _bits(mask) == 1:
            cbytes[ci] |= mask
        elif count < (mask >> shift):
            cbytes[ci] |= (count << shift) & mask
        else:
            cbytes[ci] |= mask
            encoded = b"".join(encode_vwi_forward(v) for v in vals)
            values += encode_vwi_forward(len(encoded)) + encoded  # a byte count, then the values
            continue
        for v in vals:
            values += encode_vwi_forward(v)
    return bytes(cbytes), bytes(values)


def build_index(entries: list[tuple[str, dict[int, list[int]]]], tagx: list[tuple[int, int, int, int]],
                control_bytes: int, cncx: list[bytes] | None = None, encoding: int = 65001) -> list[bytes]:
    """INDX header record + data records (+ the given CNCX records)."""
    cncx = cncx or []
    tagx_bytes = b"TAGX" + struct.pack(">II", 12 + 4 * len(tagx), control_bytes) + b"".join(bytes(t) for t in tagx)
    # data records, each under ~ 64 KB with its IDXT
    data_records: list[tuple[bytes, str, int]] = []
    cur = bytearray()
    offsets: list[int] = []
    last_ident = ""
    count = 0

    def close():
        nonlocal cur, offsets, count
        if not offsets:
            return
        header_len = 192
        body = bytes(cur)
        idxt_pos = header_len + len(body)
        pad = (4 - idxt_pos % 4) % 4
        idxt_pos += pad
        idxt = b"IDXT" + b"".join(struct.pack(">H", header_len + o) for o in offsets)
        idxt += b"\0" * ((4 - len(idxt) % 4) % 4)
        head = bytearray(b"INDX" + struct.pack(">IIII", header_len, 1, 0, 0))
        head += struct.pack(">II", idxt_pos, len(offsets))
        head += struct.pack(">I", 0xFFFFFFFF) * 2
        head = head.ljust(header_len, b"\0")
        rec = bytes(head) + body + b"\0" * pad + idxt
        data_records.append((rec, last_ident, len(offsets)))
        cur = bytearray()
        offsets = []
        count = 0

    for ident, tags in entries:
        cb, vals = _control_and_values(tagx, tags, control_bytes)
        idb = ident.encode("utf-8")
        blob = bytes([len(idb)]) + idb + cb + vals
        if len(cur) + len(blob) + 2 * (len(offsets) + 1) > 0xFC00 - 200:
            close()
        offsets.append(len(cur))
        cur += blob
        last_ident = ident
        count += 1
    close()
    # header record: header, TAGX, then (last ident, count) per data record with its own IDXT
    geom = bytearray()
    geom_offsets = []
    for _rec, ident, n in data_records:
        geom_offsets.append(len(geom))
        idb = ident.encode("utf-8")
        geom += bytes([len(idb)]) + idb + struct.pack(">H", n)
    header_len = 192
    pre = header_len + len(tagx_bytes)
    pad1 = (4 - pre % 4) % 4
    geom_start = pre + pad1
    idxt_pos = geom_start + len(geom)
    pad2 = (4 - idxt_pos % 4) % 4
    idxt_pos += pad2
    idxt = b"IDXT" + b"".join(struct.pack(">H", geom_start + o) for o in geom_offsets)
    idxt += b"\0" * ((4 - len(idxt) % 4) % 4)
    head = bytearray(b"INDX" + struct.pack(">IIII", header_len, 0, 0, 0))
    head += struct.pack(">II", idxt_pos, len(data_records))
    head += struct.pack(">II", encoding, 0xFFFFFFFF)  # encoding, language
    head += struct.pack(">I", len(entries))  # total entries
    head += struct.pack(">III", 0, 0, 0)  # ORDT, LIGT, LIGT count
    head += struct.pack(">I", len(cncx))
    head = head.ljust(180, b"\0")
    head += struct.pack(">I", header_len)  # TAGX offset (as written by other tools)
    head = head.ljust(header_len, b"\0")
    header_record = bytes(head) + tagx_bytes + b"\0" * pad1 + bytes(geom) + b"\0" * pad2 + idxt
    return [header_record] + [r for r, _i, _n in data_records] + list(cncx)
