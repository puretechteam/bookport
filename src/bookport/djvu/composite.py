"""Drawing a DjVu page: its layers (mask, background, foreground colours) put together the way
DjVu viewers do. The background, usually stored at a third of the page's resolution or less, is
scaled up with the same bilinear interpolation as DjVuLibre's viewers; pixels of the mask take the
foreground colour (a palette colour per symbol, a scaled-up foreground picture, or black).
"""

from __future__ import annotations

import struct

from ..errors import BookportError
from . import bzz, iw44, jb2, mmr
from .doc import Page


class Layers:
    """A page's decoded layers: ``mask`` (a G4 mask as packed rows), ``bg``/``fg`` IW44
    pictures as (width, height, mode, pixels), ``colors``/``blit_colors`` from an FGbz palette."""

    def __init__(self, page: Page):
        self.page = page
        info = page.info
        self.width, self.height = info.width, info.height
        self.jb2: jb2.Jb2 | None = None
        self.mask: list[int] | None = None
        self.bg = None
        self.fg = None
        self.colors: list[tuple[int, int, int]] = []
        self.blit_colors: list[int] | None = None
        self.bg_jpeg: bytes | None = None


def layers(page: Page, background: bool = True) -> Layers:
    """Decode the page's layers (``background=False`` leaves the background out)."""
    lay = Layers(page)
    if page.kind != b"DJVU":
        lay.bg = iw44.decode(page.all(page.kind))
        lay.width, lay.height = lay.bg[0], lay.bg[1]
        return lay
    w, h = lay.width, lay.height
    sjbz = page.first(b"Sjbz")
    if sjbz is not None:
        dictionary = None
        djbz = page.first(b"Djbz")
        if djbz is not None:
            key = ("djbz", djbz[2])
            dictionary = page.doc.cache.get(key)
            if dictionary is None:
                dictionary = page.doc.cache[key] = jb2.decode(djbz[1], None, True)
        lay.jb2 = jb2.decode(sjbz[1], dictionary)
        if (lay.jb2.width, lay.jb2.height) != (w, h):
            raise BookportError(f"Damaged DjVu file: page {page.index + 1}'s mask isn't the size of the page")
    else:
        smmr = page.first(b"Smmr")
        if smmr is not None:
            mw, mh, rows = mmr.decode_chunk(smmr[1])
            if (mw, mh) != (w, h):
                raise BookportError(f"Damaged DjVu file: page {page.index + 1}'s mask isn't the size of the page")
            lay.mask = jb2.packed(rows, w)
    fgbz = page.first(b"FGbz")
    if fgbz is not None and lay.jb2 is not None:
        lay.colors, lay.blit_colors = palette(fgbz[1])
    fg44 = page.all(b"FG44")
    if fg44 and not lay.colors:
        lay.fg = iw44.decode(fg44)
    if background:
        bg44 = page.all(b"BG44")
        if bg44:
            lay.bg = iw44.decode(bg44)
        else:
            jp = page.first(b"BGjp")
            if jp is not None:
                lay.bg_jpeg = jp[1]
    return lay


def palette(data: bytes) -> tuple[list[tuple[int, int, int]], list[int] | None]:
    """An FGbz chunk: the colours (r, g, b) and, when it has them, the colour of each symbol placement."""
    if len(data) < 3 or data[0] & 0x7F:
        raise BookportError("Damaged DjVu file: a colour palette is damaged")
    n = struct.unpack_from(">H", data, 1)[0]
    if 3 + 3 * n > len(data):
        raise BookportError("Damaged DjVu file: a colour palette is cut short")
    colors = [(data[3 + 3 * i + 2], data[3 + 3 * i + 1], data[3 + 3 * i]) for i in range(n)]
    per_blit = None
    pos = 3 + 3 * n
    if data[0] & 0x80 and pos + 3 <= len(data):
        count = int.from_bytes(data[pos:pos + 3], "big")
        raw = bzz.decode(data[pos + 3:], limit=2 * count + 16)
        if len(raw) < 2 * count:
            raise BookportError("Damaged DjVu file: a colour palette is cut short")
        # a colour past the palette (some encoders write a few) takes the first colour; DjVuLibre
        # refuses the whole page instead
        per_blit = [c if c < n else 0 for c in struct.unpack_from(f">{count}H", raw)]
    return colors, per_blit


def reduction(width: int, height: int, w: int, h: int) -> int:
    """By how much a layer of w x h is reduced from the page's width x height (1 to 12), or 0."""
    for red in range(1, 13):
        if (width + red - 1) // red == w and (height + red - 1) // red == h:
            return red
    return 0


def color_masks(lay: Layers) -> list[tuple[tuple[int, int, int] | None, list[int]]]:
    """(colour, packed mask) to paint in order: one per palette colour used, or one mask whose
    colour comes from the foreground picture (None) or is black."""
    if lay.jb2 is None:
        return [((0, 0, 0) if lay.fg is None else None, lay.mask)] if lay.mask is not None else []
    if lay.colors:
        per = lay.blit_colors
        if per is None:
            return [(lay.colors[0], lay.jb2.mask())]
        groups: dict[int, list[int]] = {}
        for i in range(lay.jb2.blit_count):
            groups.setdefault(per[i] if i < len(per) else 0, []).append(i)
        return [(lay.colors[c], lay.jb2.mask(groups[c])) for c in sorted(groups)]
    return [((0, 0, 0) if lay.fg is None else None, lay.jb2.mask())]


# --- scaling like DjVuLibre's viewers: bilinear, 4 fraction bits ---------------------------------

def _coords(inmax: int, outmax: int, inn: int, out: int) -> list[int]:
    length = inn * 16
    y = (length + out) // (2 * out) - 8
    z = out // 2
    lim = (inmax - 1) * 16
    res = []
    for _ in range(outmax):
        res.append(min(lim, y))
        z += length
        y += z // out
        z %= out
    return res


_INTERP = [[(j * i + 8) >> 4 for j in range(-255, 256)] for i in range(16)]


def upscale(img, width: int, height: int, red: int) -> bytes:
    """A layer reduced by ``red`` scaled up to width x height (rows top first, same mode), as
    DjVuLibre's viewers scale the background."""
    from ._accel import native
    w, h, mode, px = img
    if native is not None:
        return native.upscale(w, h, 3 if mode == "RGB" else 1, px, width, height, red)
    nc = 3 if mode == "RGB" else 1
    rowlen = w * nc
    vco = _coords(h, height, 1, red)
    hco = _coords(w, width, 1, red)
    pairs = [((n >> 4) + 1, n & 15) for n in hco]
    out = bytearray(width * height * nc)
    cache: dict[tuple[int, int], bytes] = {}
    for y_bu in range(height):  # rows counted from the bottom, as the scaler does
        fy = vco[y_bu]
        lo = max(fy >> 4, 0)
        hi = min((fy >> 4) + 1, h - 1)
        frac = fy & 15
        key = (lo, frac if hi != lo else 0)
        row = cache.get(key)
        if row is None:
            lower = px[(h - 1 - lo) * rowlen:(h - lo) * rowlen]
            if hi != lo and frac:
                upper = px[(h - 1 - hi) * rowlen:(h - hi) * rowlen]
                t = _INTERP[frac]
                line = bytes(a + t[b - a + 255] for a, b in zip(lower, upper))
            else:
                line = lower
            row = bytearray(width * nc)
            for c in range(nc):
                ch = line[c::nc]
                ch = ch[:1] + ch + ch[-1:]
                row[c::nc] = bytes(ch[i] + _INTERP[f][ch[i + 1] - ch[i] + 255] if f else ch[i] for i, f in pairs)
            row = bytes(row)
            if len(cache) > 64:
                cache.clear()
            cache[key] = row
        t_row = height - 1 - y_bu
        out[t_row * width * nc:(t_row + 1) * width * nc] = row
    return bytes(out)


def blocks(img, width: int, height: int, red: int) -> bytes:
    """A layer reduced by ``red`` scaled up by repeating each pixel (how viewers colour the
    foreground: no interpolation), rows counted from the bottom as DjVu does."""
    from ._accel import native
    w, h, mode, px = img
    nc = 3 if mode == "RGB" else 1
    if native is not None:
        return native.blocks(w, h, nc, px, width, height, red)
    out = bytearray()
    last = -1
    line = b""
    for y in range(height):
        fy = h - 1 - ((height - 1 - y) // red)
        if fy != last:
            src = px[max(fy, 0) * w * nc:(max(fy, 0) + 1) * w * nc]
            line = b"".join(src[i:i + nc] * red for i in range(0, len(src), nc))[:width * nc]
            line += line[-nc:] * (width - len(line) // nc)
            last = fy
        out += line
    return bytes(out)


def _fill_mask(packed_row: bytes, nc: int) -> bytes:
    """A packed 1-bit row as a byte mask, 0xFF for each byte of each black pixel."""
    table = _EXPAND3 if nc == 3 else _EXPAND1
    return b"".join(table[b] for b in packed_row)


_EXPAND1 = [bytes(0xFF if b & (0x80 >> k) else 0 for k in range(8)) for b in range(256)]
_EXPAND3 = [bytes(v for v in e for _ in range(3)) for e in _EXPAND1]


def compose(page: Page) -> tuple[int, int, str, bytes]:
    """The page at full resolution: (width, height, mode, data) with mode "1" (packed rows,
    1 = black), "L" or "RGB", rows top first."""
    lay = layers(page)
    return compose_layers(lay)


def compose_layers(lay: Layers) -> tuple[int, int, str, bytes]:
    from ._accel import native
    W, H = lay.width, lay.height
    paints = color_masks(lay)
    if lay.bg is None and lay.bg_jpeg is None and (not paints or (len(paints) == 1 and paints[0][0] == (0, 0, 0))):
        return W, H, "1", paints[0][1] if paints else bytes((W + 7) // 8 * H)
    gray = (lay.bg is None or lay.bg[2] == "L") and lay.fg is None and all(
        c is not None and c[0] == c[1] == c[2] for c, _ in paints)
    nc = 1 if gray else 3
    if lay.bg is not None:
        bw, bh, bmode, bpx = lay.bg
        red = reduction(W, H, bw, bh)
        if not red:
            raise BookportError(f"Damaged DjVu file: page {lay.page.index + 1}'s background isn't a reduction of the page")
        if red == 1 and (bw, bh) == (W, H):
            base = bpx
        else:
            base = upscale(lay.bg, W, H, red)
        if nc == 3 and bmode == "L":
            base = bytes(v for v in base for _ in range(3))
    else:
        base = b"\xff" * (W * H * nc)
    if not paints:
        return W, H, "RGB" if nc == 3 else "L", base
    fgpx = None
    if lay.fg is not None:
        fw, fh, fmode, f = lay.fg
        red = reduction(W, H, fw, fh)
        if not red:
            raise BookportError(f"Damaged DjVu file: page {lay.page.index + 1}'s foreground isn't a reduction of the page")
        fgpx = blocks(lay.fg, W, H, red) if red > 1 else f
        if fmode == "L":
            fgpx = bytes(v for v in fgpx for _ in range(3))
    rowlen = W * nc
    if native is not None:
        return W, H, "RGB" if nc == 3 else "L", native.paint(W, H, nc, base, paints, fgpx)
    out = bytearray(base)
    nbytes = (W + 7) // 8
    for color, packed in paints:
        solid = None if color is None else int.from_bytes(bytes(color[:nc]) * W, "big")
        for y in range(H):
            if not any(packed[y * nbytes:(y + 1) * nbytes]):
                continue
            m = int.from_bytes(_fill_mask(packed[y * nbytes:(y + 1) * nbytes], nc)[:rowlen], "big")
            cur = int.from_bytes(out[y * rowlen:(y + 1) * rowlen], "big")
            fg = solid if solid is not None else int.from_bytes(fgpx[y * rowlen:(y + 1) * rowlen], "big")
            out[y * rowlen:(y + 1) * rowlen] = ((cur & ~m) | (fg & m)).to_bytes(rowlen, "big")
    return W, H, "RGB" if nc == 3 else "L", bytes(out)
