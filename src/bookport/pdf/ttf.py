"""TrueType fonts: parse metrics and character maps, and subset glyf fonts for embedding in PDF.

Written from Apple's TrueType Reference Manual and the OpenType specification: the table
directory (and TTC collections), head, hhea, hmtx, maxp, cmap (formats 4 and 12), loca, glyf
(simple and composite glyphs), OS/2, post and name.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..errors import BookportError


@dataclass
class Font:
    data: bytes
    tables: dict[str, tuple[int, int]]
    units: int = 1000
    ascent: int = 800
    descent: int = -200
    line_gap: int = 0
    cap_height: int = 700
    x_height: int = 500
    bbox: tuple = (0, -200, 1000, 800)
    italic_angle: float = 0.0
    fixed: bool = False
    bold: bool = False
    num_glyphs: int = 0
    long_loca: bool = False
    advances: list[int] = field(default_factory=list)
    cmap: dict[int, int] = field(default_factory=dict)
    name: str = "Font"
    embeddable: bool = True
    _width_cache: dict = field(default_factory=dict)

    def table(self, tag: str) -> bytes:
        off, length = self.tables[tag]
        return self.data[off:off + length]

    def glyph(self, ch: str) -> int:
        return self.cmap.get(ord(ch), 0)

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def advance(self, gid: int) -> int:
        adv = self.advances
        return adv[gid] if gid < len(adv) else (adv[-1] if adv else self.units // 2)

    def width(self, text: str, size: float) -> float:
        """Advance width of ``text`` in points at ``size``."""
        cache = self._width_cache
        total = 0
        for ch in text:
            w = cache.get(ch)
            if w is None:
                w = cache[ch] = self.advance(self.cmap.get(ord(ch), 0))
            total += w
        return total * size / self.units


def load(data: bytes, index: int = 0) -> Font:
    if data[:4] == b"ttcf":
        count = struct.unpack(">I", data[8:12])[0]
        if index >= count:
            raise BookportError("Font collection index out of range")
        offset = struct.unpack(">I", data[12 + 4 * index:16 + 4 * index])[0]
    else:
        offset = 0
    sfnt = data[offset:offset + 4]
    if sfnt not in (b"\x00\x01\x00\x00", b"true"):
        raise BookportError("Only TrueType (glyf) fonts can be embedded")
    num = struct.unpack(">H", data[offset + 4:offset + 6])[0]
    tables = {}
    for i in range(num):
        rec = data[offset + 12 + 16 * i:offset + 28 + 16 * i]
        tag = rec[:4].decode("latin-1")
        _check, off, length = struct.unpack(">III", rec[4:16])
        tables[tag] = (off, length)
    f = Font(data, tables)
    for t in ("head", "hhea", "maxp", "hmtx", "cmap", "loca", "glyf"):
        if t not in tables:
            raise BookportError(f"Font has no {t} table")
    head = f.table("head")
    f.units = struct.unpack(">H", head[18:20])[0] or 1000
    f.bbox = struct.unpack(">hhhh", head[36:44])
    mac_style = struct.unpack(">H", head[44:46])[0]
    f.long_loca = struct.unpack(">h", head[50:52])[0] == 1
    hhea = f.table("hhea")
    f.ascent, f.descent, f.line_gap = struct.unpack(">hhh", hhea[4:10])
    n_hmetrics = struct.unpack(">H", hhea[34:36])[0]
    f.num_glyphs = struct.unpack(">H", f.table("maxp")[4:6])[0]
    hmtx = f.table("hmtx")
    adv = [struct.unpack(">H", hmtx[4 * i:4 * i + 2])[0] for i in range(n_hmetrics)]
    last = adv[-1] if adv else f.units // 2
    f.advances = adv + [last] * (f.num_glyphs - n_hmetrics)
    f.bold = bool(mac_style & 1)
    if "OS/2" in tables:
        os2 = f.table("OS/2")
        weight = struct.unpack(">H", os2[4:6])[0]
        fs_type = struct.unpack(">H", os2[8:10])[0]
        f.embeddable = fs_type & 15 != 2  # "restricted license" fonts can't be embedded
        f.bold = f.bold or weight >= 600
        if len(os2) >= 72:
            asc, desc, _gap = struct.unpack(">hhh", os2[68:74])
            if asc:
                f.ascent, f.descent = asc, desc
        if len(os2) >= 90:
            f.x_height, f.cap_height = struct.unpack(">hh", os2[86:90])
    if "post" in tables:
        post = f.table("post")
        whole, frac = struct.unpack(">hH", post[4:8])
        f.italic_angle = whole + frac / 65536
        f.fixed = struct.unpack(">I", post[12:16])[0] != 0
    f.cmap = _cmap(f.table("cmap"))
    f.name = _ps_name(f) or "Font"
    return f


def _cmap(data: bytes) -> dict[int, int]:
    n = struct.unpack(">H", data[2:4])[0]
    best = None
    for i in range(n):
        pid, eid, off = struct.unpack(">HHI", data[4 + 8 * i:12 + 8 * i])
        fmt = struct.unpack(">H", data[off:off + 2])[0]
        rank = {(3, 10): 0, (0, 4): 1, (0, 6): 1, (3, 1): 2, (0, 3): 3, (0, 1): 4, (0, 0): 5}.get((pid, eid), 9)
        if fmt in (4, 12) and (best is None or rank < best[0]):
            best = (rank, off, fmt)
    if best is None:
        return {}
    _r, off, fmt = best
    out: dict[int, int] = {}
    if fmt == 4:
        seg2 = struct.unpack(">H", data[off + 6:off + 8])[0]
        segs = seg2 // 2
        ends = struct.unpack(f">{segs}H", data[off + 14:off + 14 + seg2])
        starts = struct.unpack(f">{segs}H", data[off + 16 + seg2:off + 16 + 2 * seg2])
        deltas = struct.unpack(f">{segs}h", data[off + 16 + 2 * seg2:off + 16 + 3 * seg2])
        ro_pos = off + 16 + 3 * seg2
        ranges = struct.unpack(f">{segs}H", data[ro_pos:ro_pos + seg2])
        for k in range(segs):
            start, end, delta, ro = starts[k], ends[k], deltas[k], ranges[k]
            if start == 0xFFFF:
                continue
            for c in range(start, end + 1):
                if ro == 0:
                    g = (c + delta) & 0xFFFF
                else:
                    gpos = ro_pos + 2 * k + ro + 2 * (c - start)
                    g = struct.unpack(">H", data[gpos:gpos + 2])[0] if gpos + 2 <= len(data) else 0
                    if g:
                        g = (g + delta) & 0xFFFF
                if g:
                    out[c] = g
    else:
        ngroups = struct.unpack(">I", data[off + 12:off + 16])[0]
        for k in range(ngroups):
            start, end, gid = struct.unpack(">III", data[off + 16 + 12 * k:off + 28 + 12 * k])
            for c in range(start, min(end, 0x10FFFF) + 1):
                out[c] = gid + (c - start)
    return out


def _ps_name(f: Font) -> str:
    if "name" not in f.tables:
        return ""
    data = f.table("name")
    count, string_off = struct.unpack(">HH", data[2:6])
    for i in range(count):
        pid, _eid, _lang, nid, length, off = struct.unpack(">HHHHHH", data[6 + 12 * i:18 + 12 * i])
        if nid == 6:
            raw = data[string_off + off:string_off + off + length]
            try:
                name = raw.decode("utf-16-be") if pid in (0, 3) else raw.decode("latin-1")
            except UnicodeDecodeError:
                continue
            return "".join(c for c in name if c.isalnum() or c in "-_")
    return ""


# --- subsetting --------------------------------------------------------------------------------------

def _glyph_range(f: Font, loca: bytes, gid: int) -> tuple[int, int]:
    if f.long_loca:
        a, b = struct.unpack(">II", loca[4 * gid:4 * gid + 8])
    else:
        a, b = struct.unpack(">HH", loca[2 * gid:2 * gid + 4])
        a, b = a * 2, b * 2
    return a, b


def _components(glyph: bytes) -> list[tuple[int, int]]:
    """(byte offset of the glyph index, glyph index) for each component of a composite glyph."""
    out = []
    if len(glyph) < 10 or struct.unpack(">h", glyph[:2])[0] >= 0:
        return out
    pos = 10
    while pos + 4 <= len(glyph):
        flags, gid = struct.unpack(">HH", glyph[pos:pos + 4])
        out.append((pos + 2, gid))
        pos += 4
        pos += 4 if flags & 0x0001 else 2  # ARG_1_AND_2_ARE_WORDS
        if flags & 0x0008:
            pos += 2  # WE_HAVE_A_SCALE
        elif flags & 0x0040:
            pos += 4  # X_AND_Y_SCALE
        elif flags & 0x0080:
            pos += 8  # TWO_BY_TWO
        if not flags & 0x0020:  # MORE_COMPONENTS
            break
    return out


def _checksum(data: bytes) -> int:
    data = data + b"\0" * ((4 - len(data) % 4) % 4)
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def subset(f: Font, gids) -> tuple[bytes, dict[int, int]]:
    """A TrueType font with only ``gids`` (and their components), renumbered from 1.

    Returns (font bytes, {old glyph id: new glyph id}); glyph 0 (.notdef) stays 0.
    """
    loca = f.table("loca")
    glyf_off, _glyf_len = f.tables["glyf"]
    data = f.data
    wanted = {0} | {g for g in gids if 0 <= g < f.num_glyphs}
    stack = list(wanted)
    while stack:
        g = stack.pop()
        a, b = _glyph_range(f, loca, g)
        for _pos, comp in _components(data[glyf_off + a:glyf_off + b]):
            if comp not in wanted and comp < f.num_glyphs:
                wanted.add(comp)
                stack.append(comp)
    order = sorted(wanted)
    mapping = {old: new for new, old in enumerate(order)}
    glyf = bytearray()
    offsets = []
    for old in order:
        a, b = _glyph_range(f, loca, old)
        g = bytearray(data[glyf_off + a:glyf_off + b])
        for pos, comp in _components(bytes(g)):
            g[pos:pos + 2] = struct.pack(">H", mapping.get(comp, 0))
        offsets.append(len(glyf))
        glyf += g
        glyf += b"\0" * ((4 - len(glyf) % 4) % 4)
    offsets.append(len(glyf))
    loca_new = struct.pack(f">{len(offsets)}I", *offsets)
    hmtx_new = b"".join(struct.pack(">Hh", f.advance(old), _lsb(f, old)) for old in order)
    head = bytearray(f.table("head"))
    head[8:12] = b"\0\0\0\0"  # checkSumAdjustment, set below
    head[50:52] = struct.pack(">h", 1)  # long loca
    hhea = bytearray(f.table("hhea"))
    hhea[34:36] = struct.pack(">H", len(order))
    maxp = bytearray(f.table("maxp"))
    maxp[4:6] = struct.pack(">H", len(order))
    post = struct.pack(">IIhhIIIII", 0x00030000, struct.unpack(">I", f.table("post")[4:8])[0] if "post" in f.tables else 0,
                       0, 0, 1 if f.fixed else 0, 0, 0, 0, 0)
    tables = {"head": bytes(head), "hhea": bytes(hhea), "maxp": bytes(maxp), "hmtx": hmtx_new, "loca": loca_new,
              "glyf": bytes(glyf), "post": post}
    for extra in ("cvt ", "fpgm", "prep"):
        if extra in f.tables:
            tables[extra] = f.table(extra)
    out = _assemble(tables)
    adj = (0xB1B0AFBA - _checksum(out)) & 0xFFFFFFFF
    # patch checkSumAdjustment in the head table copy
    directory = _directory(out)
    hoff = directory["head"][0]
    out = out[:hoff + 8] + struct.pack(">I", adj) + out[hoff + 12:]
    return out, mapping


def _lsb(f: Font, gid: int) -> int:
    hmtx = f.table("hmtx")
    n_h = struct.unpack(">H", f.table("hhea")[34:36])[0]
    if gid < n_h:
        return struct.unpack(">h", hmtx[4 * gid + 2:4 * gid + 4])[0]
    k = 4 * n_h + 2 * (gid - n_h)
    return struct.unpack(">h", hmtx[k:k + 2])[0] if k + 2 <= len(hmtx) else 0


def _assemble(tables: dict[str, bytes]) -> bytes:
    tags = sorted(tables)
    n = len(tags)
    entry_selector = max(0, n.bit_length() - 1)
    search_range = (1 << entry_selector) * 16
    header = struct.pack(">IHHHH", 0x00010000, n, search_range, entry_selector, n * 16 - search_range)
    offset = 12 + 16 * n
    dir_entries = b""
    body = b""
    for tag in tags:
        t = tables[tag]
        dir_entries += tag.encode("latin-1") + struct.pack(">III", _checksum(t), offset + len(body), len(t))
        body += t + b"\0" * ((4 - len(t) % 4) % 4)
    return header + dir_entries + body


def _directory(data: bytes) -> dict[str, tuple[int, int]]:
    n = struct.unpack(">H", data[4:6])[0]
    out = {}
    for i in range(n):
        rec = data[12 + 16 * i:28 + 16 * i]
        out[rec[:4].decode("latin-1")] = struct.unpack(">II", rec[8:16])
    return out
