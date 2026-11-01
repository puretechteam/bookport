"""Palm OS bitmaps (BitmapType), as Plucker stores its images: decoding to a Raster.

Written from Palm OS's description of the bitmap format. A bitmap is a big-endian header (width,
height, bytes per row, flags, bits per pixel, version), then an optional colour table, then the
pixels, optionally compressed; several bitmaps of different depths may be chained, and the
deepest one is used.

- Versions 0 and 1: a 16-byte header, 1 to 8 bits per pixel; version 1 with 255 bits per pixel
  is the empty bitmap that comes before high-density ones.
- Version 2: adds a transparent index, a compression type and 16-bit direct colour (RGB 565,
  with an 8-byte direct-colour block after the header).
- Version 3: a header whose size it gives itself, a pixel format (indexed, or RGB 565 big or
  little endian), a 32-bit transparent value and the next bitmap's offset in bytes.
- Compression: scan line (each row against the one above: per 8 bytes, a byte of flags marks
  the bytes that changed and follow), RLE (count and value pairs) and PackBits (in 16-bit units
  for 16-bit pixels). Compressed data starts with its size, 2 bytes (4 in version 3).
- Colours: 1, 2 and 4 bits are greys from white (0) to black; 8 bits use the colour table, or
  Palm's standard 256-colour palette.
"""

from __future__ import annotations

import struct

from . import limits
from .errors import BookportError
from .images import Raster

COMPRESSED, HAS_COLOR_TABLE, HAS_TRANSPARENCY, DIRECT_COLOR = 0x8000, 0x4000, 0x2000, 0x0400
SCANLINE, RLE, PACKBITS, NONE = 0, 1, 2, 0xFF


def _system_palette() -> list[tuple[int, int, int]]:
    # the 216-colour cube (red slowest, green fastest, each from FF down to 00), ten greys, five
    # more colours, then black
    cube = [(255 - 51 * (i // 36), 255 - 51 * (i % 6), 255 - 51 * (i // 6 % 6)) for i in range(216)]
    greys = [(v, v, v) for v in (0x11, 0x22, 0x44, 0x55, 0x77, 0x88, 0xAA, 0xBB, 0xDD, 0xEE)]
    extra = [(0xC0, 0xC0, 0xC0), (0x80, 0, 0), (0x80, 0, 0x80), (0, 0x80, 0), (0, 0x80, 0x80)]
    return cube + greys + extra + [(0, 0, 0)] * (256 - 216 - 10 - 5)


SYSTEM_PALETTE = _system_palette()


def decode(data: bytes) -> Raster:
    """The deepest bitmap of a (possibly chained) Palm bitmap, as RGB or RGBA."""
    best = None
    pos = 0
    seen = set()
    try:
        while pos + 16 <= len(data) and pos not in seen and len(seen) < 16:
            seen.add(pos)
            head = _header(data, pos)
            if head["depth"] != 255 and (best is None or head["depth"] > best["depth"]):
                best = head
            if not head["next"]:
                break
            pos += head["next"]
        if best is None:
            raise BookportError("Damaged Palm bitmap: no picture in it")
        return _pixels(data, best)
    except (struct.error, IndexError, ValueError) as e:
        raise BookportError(f"Damaged Palm bitmap ({type(e).__name__})") from None


def _header(data: bytes, pos: int) -> dict:
    width, height, row_bytes, flags, depth, version = struct.unpack(">HHHHBB", data[pos:pos + 10])
    head = {"width": width, "height": height, "row_bytes": row_bytes, "flags": flags, "version": version,
            "depth": depth or 1, "transparent": None, "compression": SCANLINE, "format": 0, "big_size": False}
    if version == 1 and depth == 255:  # the empty bitmap before high-density ones: the next starts after it
        head["next"] = 16
        return head
    if version >= 3:
        size, pixel_format, _unused, compression, _density, transparent, nxt = struct.unpack(
            ">BBBBHII", data[pos + 10:pos + 24])
        head.update(compression=compression, format=pixel_format, next=nxt, big_size=True,
                    transparent=transparent if flags & HAS_TRANSPARENCY else None)
        body = pos + max(size, 24)
    else:
        nxt, transparent, compression = struct.unpack(">HBB", data[pos + 10:pos + 14])
        head["next"] = nxt * 4
        if version == 2:
            head["compression"] = compression
            if flags & HAS_TRANSPARENCY:
                head["transparent"] = transparent
        body = pos + 16
    if flags & HAS_COLOR_TABLE:
        count = struct.unpack(">H", data[body:body + 2])[0]
        table = data[body + 2:body + 2 + 4 * count]
        head["palette"] = [tuple(table[4 * k + 1:4 * k + 4]) for k in range(len(table) // 4)]
        body += 2 + 4 * count
    if version == 2 and flags & DIRECT_COLOR:
        if flags & HAS_TRANSPARENCY:
            r, g, b = data[body + 5:body + 8]
            head["transparent"] = (r >> 3) << 11 | (g >> 2) << 5 | b >> 3  # as the 5-6-5 pixels hold it
        body += 8
        head["format"] = 2
    head["body"] = body
    return head


def _pixels(data: bytes, h: dict) -> Raster:
    width, height, row_bytes, depth = h["width"], h["height"], h["row_bytes"], h["depth"]
    if not width or not height or depth not in (1, 2, 4, 8, 16) or row_bytes * 8 < width * depth:
        raise BookportError(f"Damaged Palm bitmap: {width}x{height} at {depth} bits, {row_bytes} bytes a row")
    limits.check_pixels(width, height)
    size = row_bytes * height
    body = h["body"]
    if h["flags"] & COMPRESSED and h["compression"] != NONE:
        n = 4 if h["big_size"] else 2
        length = int.from_bytes(data[body:body + n], "big")
        raw = _decompress(data[body + n:body + max(n, length)], h["compression"], row_bytes, size, depth)
    else:
        raw = data[body:body + size]
    raw = bytes(raw[:size]).ljust(size, b"\0")
    transparent = h["transparent"]
    if depth == 16:
        little = h["format"] == 3
        rgb = bytearray()
        alpha = bytearray()
        for y in range(height):
            row = raw[y * row_bytes:y * row_bytes + 2 * width]
            for x in range(0, 2 * width, 2):
                v = row[x + 1] << 8 | row[x] if little else row[x] << 8 | row[x + 1]
                c = ((v >> 11) * 255 // 31, (v >> 5 & 63) * 255 // 63, (v & 31) * 255 // 31)
                rgb += bytes(c)
                alpha.append(0 if v == transparent else 255)
        return _with_alpha(width, height, rgb, alpha, transparent is not None)
    if depth == 8:
        palette = h.get("palette") or SYSTEM_PALETTE
    else:
        top = (1 << depth) - 1
        palette = h.get("palette") or [(255 - v * 255 // top,) * 3 for v in range(top + 1)]
    palette = list(palette) + [(0, 0, 0)] * (256 - len(palette))
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    rgb = bytearray()
    alpha = bytearray()
    for y in range(height):
        row = raw[y * row_bytes:(y + 1) * row_bytes]
        for x in range(width):
            byte = row[x // per_byte]
            v = byte >> (8 - depth * (x % per_byte + 1)) & mask
            rgb += bytes(palette[v])
            alpha.append(0 if v == transparent else 255)
    return _with_alpha(width, height, rgb, alpha, transparent is not None)


def _with_alpha(width: int, height: int, rgb: bytearray, alpha: bytearray, transparent: bool) -> Raster:
    if not transparent or all(a == 255 for a in alpha):
        return Raster(width, height, "RGB", bytes(rgb))
    rgba = bytearray(4 * width * height)
    rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = rgb[0::3], rgb[1::3], rgb[2::3], alpha
    return Raster(width, height, "RGBA", bytes(rgba))


def _decompress(src: bytes, kind: int, row_bytes: int, size: int, depth: int) -> bytearray:
    out = bytearray()
    i, n = 0, len(src)
    if kind == SCANLINE:
        prev = bytearray(row_bytes)
        while len(out) < size and i < n:
            row = bytearray()
            for start in range(0, row_bytes, 8):
                if i >= n:
                    break
                flags = src[i]
                i += 1
                for k in range(start, min(start + 8, row_bytes)):
                    if flags & (0x80 >> (k - start)) and i < n:
                        row.append(src[i])
                        i += 1
                    else:
                        row.append(prev[k])
            row = row.ljust(row_bytes, b"\0")
            out += row
            prev = row
    elif kind == RLE:
        while len(out) < size and i + 1 < n:
            out += bytes([src[i + 1]]) * min(src[i], size - len(out))
            i += 2
    elif kind == PACKBITS:
        unit = 2 if depth == 16 else 1
        while len(out) < size and i < n:
            c = src[i]
            i += 1
            if c < 128:  # c + 1 units as they are
                out += src[i:i + (c + 1) * unit]
                i += (c + 1) * unit
            elif c > 128:  # the next unit, 257 - c times
                out += src[i:i + unit] * min(257 - c, (size - len(out)) // unit + 1)
                i += unit
    else:
        raise BookportError(f"Unknown Palm bitmap compression {kind}")
    return out
