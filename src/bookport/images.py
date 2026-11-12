"""Image decoding and re-encoding without dependencies (Pillow is used when it's installed).

Decoders for PNG, GIF and BMP give raw pixels; a PNG encoder writes them back. That covers
what ebook formats need: FB2 and MOBI take only JPEG/PNG/GIF, PDF wants raw samples.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from itertools import accumulate

from . import limits
from .errors import BookportError

try:  # optional
    import PIL.Image as _PIL
except ImportError:  # pragma: no cover - depends on the environment
    _PIL = None


@dataclass
class Raster:
    width: int
    height: int
    mode: str  # "L", "LA", "RGB", "RGBA"
    pixels: bytes  # rows top to bottom, no padding

    @property
    def channels(self) -> int:
        return len(self.mode)


# --- PNG ---------------------------------------------------------------------------------------

def _chunks(data):
    pos = 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        yield kind, data[pos + 8:pos + 8 + length]
        pos += 12 + length


def png_info(data: bytes) -> dict:
    ihdr = next(c for k, c in _chunks(data) if k == b"IHDR")
    w, h, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", ihdr)
    return {"width": w, "height": h, "depth": depth, "color": ctype, "interlace": interlace}


_AND255 = (255).__and__


def _unfilter(raw: bytes, width: int, height: int, bpp: int, row_bytes: int) -> bytearray:
    if row_bytes == width * bpp and bpp <= 4 and len(raw) >= height * (row_bytes + 1):  # 8-bit samples
        from .pdf.parser import _png_rows  # Pillow undoes the filters in C, when it's installed
        fast = _png_rows(raw[:height * (row_bytes + 1)], bpp, width, row_bytes)
        if fast is not None and len(fast) == height * row_bytes:
            return bytearray(fast)
    out = bytearray(height * row_bytes)
    prev = bytes(row_bytes)
    high = int.from_bytes(b"\x80" * row_bytes, "little")  # the top bit of every byte
    low = ((1 << (8 * row_bytes)) - 1) ^ high
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        line = raw[pos + 1:pos + 1 + row_bytes]
        pos += 1 + row_bytes
        if ftype == 1:  # Sub: a running sum of each channel (in C: accumulate and map)
            line = bytearray(line)
            for c in range(bpp):
                line[c::bpp] = bytes(map(_AND255, accumulate(line[c::bpp])))
        elif ftype == 2:  # Up: every byte added to the one above, at once, as one big number with no carries
            a, b = int.from_bytes(line, "little"), int.from_bytes(prev, "little")
            line = ((a & low) + (b & low) ^ ((a ^ b) & high)).to_bytes(row_bytes, "little")
        elif ftype == 3:
            line = list(line)
            up = prev
            for i in range(bpp):
                line[i] = (line[i] + (up[i] >> 1)) & 0xFF
            for i in range(bpp, row_bytes):
                line[i] = (line[i] + ((line[i - bpp] + up[i]) >> 1)) & 0xFF
        elif ftype == 4:
            line = list(line)
            up = prev
            for i in range(bpp):
                line[i] = (line[i] + up[i]) & 0xFF  # no left or upper left: Paeth picks the byte above
            for i in range(bpp, row_bytes):
                a = line[i - bpp]
                b = up[i]
                c = up[i - bpp]
                pa = b - c  # |p - a| with p = a + b - c
                pb = a - c
                pc = pa + pb
                if pa < 0:
                    pa = -pa
                if pb < 0:
                    pb = -pb
                if pc < 0:
                    pc = -pc
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        line = bytes(line)
        out[y * row_bytes:(y + 1) * row_bytes] = line
        prev = line
    return out


def decode_png(data: bytes) -> Raster:
    info = png_info(data)
    w, h, depth, ctype = info["width"], info["height"], info["depth"], info["color"]
    limits.check_pixels(w, h)
    palette = trns = None
    idat = []
    for kind, chunk in _chunks(data):
        if kind == b"PLTE":
            palette = chunk
        elif kind == b"tRNS":
            trns = chunk
        elif kind == b"IDAT":
            idat.append(chunk)
    raw = limits.inflate(b"".join(idat), limit=min(limits.MAX_PART, h * ((w * 64 + 7) // 8 + 8) + 1024))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    if info["interlace"]:
        return _decode_adam7(raw, w, h, depth, ctype, channels, palette, trns)
    bits = depth * channels
    row_bytes = (w * bits + 7) // 8
    bpp = max(1, bits // 8)
    px = _unfilter(raw, w, h, bpp, row_bytes)
    return _expand(px, w, h, depth, ctype, channels, palette, trns, row_bytes)


def _expand(px, w, h, depth, ctype, channels, palette, trns, row_bytes) -> Raster:
    if depth == 16:
        px = bytes(px[0::2])
        row_bytes //= 2
        depth = 8
    if depth < 8:
        out = bytearray(w * h)
        per = 8 // depth
        mask = (1 << depth) - 1
        scale = 255 // mask if ctype == 0 else 1
        for y in range(h):
            row = px[y * row_bytes:(y + 1) * row_bytes]
            for x in range(w):
                byte = row[x // per]
                shift = 8 - depth * (x % per + 1)
                out[y * w + x] = ((byte >> shift) & mask) * scale
        px = out
    else:
        px = bytes(px)
    if ctype == 3:
        pal = palette or b""
        alpha = trns or b""
        if alpha:
            lut = [pal[i * 3:i * 3 + 3] + bytes([alpha[i] if i < len(alpha) else 255]) for i in range(256)]
            lut = [x if len(x) == 4 else b"\0\0\0\xff" for x in lut]
            return Raster(w, h, "RGBA", b"".join(lut[i] for i in px))
        lut = [pal[i * 3:i * 3 + 3] if len(pal) >= i * 3 + 3 else b"\0\0\0" for i in range(256)]
        return Raster(w, h, "RGB", b"".join(lut[i] for i in px))
    mode = {0: "L", 2: "RGB", 4: "LA", 6: "RGBA"}[ctype]
    return Raster(w, h, mode, px)


def _decode_adam7(raw, w, h, depth, ctype, channels, palette, trns) -> Raster:
    passes = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    bits = depth * channels
    bpp = max(1, bits // 8)
    full = None
    pos = 0
    for x0, y0, dx, dy in passes:
        pw = (w - x0 + dx - 1) // dx
        ph = (h - y0 + dy - 1) // dy
        if pw <= 0 or ph <= 0:
            continue
        row_bytes = (pw * bits + 7) // 8
        size = ph * (row_bytes + 1)
        px = _unfilter(raw[pos:pos + size], pw, ph, bpp, row_bytes)
        pos += size
        sub = _expand(px, pw, ph, depth, ctype, channels, palette, trns, row_bytes)
        if full is None:
            full = bytearray(w * h * sub.channels)
            mode = sub.mode
        c = sub.channels
        for y in range(ph):
            for x in range(pw):
                src = (y * pw + x) * c
                dst = ((y0 + y * dy) * w + x0 + x * dx) * c
                full[dst:dst + c] = sub.pixels[src:src + c]
    return Raster(w, h, mode, bytes(full))


def encode_png(r: Raster, level: int = 6) -> bytes:
    ctype = {"L": 0, "LA": 4, "RGB": 2, "RGBA": 6}[r.mode]
    stride = r.width * r.channels
    raw = b"".join(b"\0" + r.pixels[y * stride:(y + 1) * stride] for y in range(r.height))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", r.width, r.height, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b""))


# --- GIF ---------------------------------------------------------------------------------------

def _lzw_decode(data: bytes, min_size: int, npixels: int) -> bytearray:
    clear = 1 << min_size
    end = clear + 1
    size = min_size + 1
    table = [bytes([i]) for i in range(clear)] + [b"", b""]
    out = bytearray()
    prev = None
    bitbuf = nbits = 0
    for byte in data:
        bitbuf |= byte << nbits
        nbits += 8
        while nbits >= size:
            code = bitbuf & ((1 << size) - 1)
            bitbuf >>= size
            nbits -= size
            if code == clear:
                table = table[:clear + 2]
                size = min_size + 1
                prev = None
                continue
            if code == end:
                return out
            if prev is None:
                entry = table[code] if code < len(table) else b""
                out += entry
                prev = entry
                continue
            if code < len(table):
                entry = table[code]
                table.append(prev + entry[:1])
            else:
                entry = prev + prev[:1]
                table.append(entry)
            out += entry
            prev = entry
            if len(table) == (1 << size) and size < 12:
                size += 1
            if len(out) >= npixels:
                return out
    return out


def decode_gif(data: bytes) -> Raster:
    """The first frame of a GIF, as RGBA (or RGB when it has no transparency)."""
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("not a GIF")
    w, h, flags, _bg, _aspect = struct.unpack("<HHBBB", data[6:13])
    limits.check_pixels(w, h)
    pos = 13
    gct = None
    if flags & 0x80:
        n = 3 * (2 << (flags & 7))
        gct = data[pos:pos + n]
        pos += n
    transparent = None
    while pos < len(data):
        block = data[pos]
        pos += 1
        if block == 0x21:
            label = data[pos]
            pos += 1
            if label == 0xF9 and data[pos] >= 4:
                packed = data[pos + 1]
                if packed & 1:
                    transparent = data[pos + 4]
            while pos < len(data) and data[pos]:
                pos += data[pos] + 1
            pos += 1
        elif block == 0x2C:
            ix, iy, iw, ih, iflags = struct.unpack("<HHHHB", data[pos:pos + 9])
            pos += 9
            table = gct
            if iflags & 0x80:
                n = 3 * (2 << (iflags & 7))
                table = data[pos:pos + n]
                pos += n
            min_size = data[pos]
            pos += 1
            chunks = []
            while pos < len(data) and data[pos]:
                chunks.append(data[pos + 1:pos + 1 + data[pos]])
                pos += data[pos] + 1
            indices = _lzw_decode(b"".join(chunks), min_size, iw * ih)
            indices += bytes(iw * ih - len(indices))
            if iflags & 0x40:  # interlaced rows
                order = list(range(0, ih, 8)) + list(range(4, ih, 8)) + list(range(2, ih, 4)) + list(range(1, ih, 2))
                rows = [indices[i * iw:(i + 1) * iw] for i in range(ih)]
                fixed = [b""] * ih
                for k, y in enumerate(order):
                    fixed[y] = rows[k]
                indices = bytearray(b"".join(fixed))
            table = table or bytes(range(256)) * 3
            alpha = transparent is not None
            lut = []
            for i in range(256):
                rgb = table[i * 3:i * 3 + 3] if len(table) >= i * 3 + 3 else b"\0\0\0"
                lut.append(rgb + (b"\0" if i == transparent else b"\xff") if alpha else rgb)
            c = 4 if alpha else 3
            canvas = bytearray((b"\0\0\0\0" if alpha else b"\xff\xff\xff") * (w * h))
            for y in range(min(ih, h - iy)):
                row = b"".join(lut[i] for i in indices[y * iw:(y + 1) * iw])
                start = ((iy + y) * w + ix) * c
                width = min(iw, w - ix) * c
                canvas[start:start + width] = row[:width]
            return Raster(w, h, "RGBA" if alpha else "RGB", bytes(canvas))
        elif block == 0x3B:
            break
        else:
            break
    raise ValueError("GIF has no image")


# --- BMP ---------------------------------------------------------------------------------------

def decode_bmp(data: bytes) -> Raster:
    if data[:2] != b"BM":
        raise ValueError("not a BMP")
    offset = struct.unpack("<I", data[10:14])[0]
    header = struct.unpack("<I", data[14:18])[0]
    w, h, _planes, bpp, comp = struct.unpack("<iiHHI", data[18:34])
    if comp not in (0, 3):
        raise ValueError("compressed BMP")
    top_down = h < 0
    h = abs(h)
    limits.check_pixels(abs(w), h)
    palette = []
    if bpp <= 8:
        ncolors = struct.unpack("<I", data[46:50])[0] or (1 << bpp)
        pal_start = 14 + header
        for i in range(ncolors):
            b, g, r, _ = data[pal_start + i * 4:pal_start + i * 4 + 4]
            palette.append(bytes((r, g, b)))
    stride = ((w * bpp + 31) // 32) * 4
    out = bytearray()
    for y in range(h):
        row_y = y if top_down else h - 1 - y
        row = data[offset + row_y * stride:offset + (row_y + 1) * stride]
        if bpp == 24:
            for x in range(w):
                b, g, r = row[x * 3:x * 3 + 3]
                out += bytes((r, g, b))
        elif bpp == 32:
            for x in range(w):
                b, g, r, _a = row[x * 4:x * 4 + 4]
                out += bytes((r, g, b))
        elif bpp in (1, 4, 8):
            per = 8 // bpp
            mask = (1 << bpp) - 1
            for x in range(w):
                byte = row[x // per]
                idx = (byte >> (8 - bpp * (x % per + 1))) & mask
                out += palette[idx] if idx < len(palette) else b"\0\0\0"
        else:
            raise ValueError(f"{bpp}-bit BMP")
    return Raster(w, h, "RGB", bytes(out))


# --- conversions -------------------------------------------------------------------------------

def decode(data: bytes) -> Raster | None:
    """Raw pixels for PNG/GIF/BMP (and anything Pillow reads, when installed). JPEG needs Pillow."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return decode_png(data)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return decode_gif(data)
        if data[:2] == b"BM":
            return decode_bmp(data)
    except (ValueError, struct.error, zlib.error, IndexError, KeyError, StopIteration, BookportError):
        pass  # not an image bookport can convert (too big, or damaged)
    if _PIL is not None:
        import io
        try:
            im = _PIL.open(io.BytesIO(data))
            im.load()
            if im.mode not in ("L", "LA", "RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.mode or "transparency" in im.info else "RGB")
            return Raster(im.width, im.height, im.mode, im.tobytes())
        except Exception:
            return None
    return None


def to_png(data: bytes) -> bytes | None:
    """Any decodable image as PNG; None when it can't be decoded."""
    r = decode(data)
    return encode_png(r) if r is not None else None


def flatten_alpha(r: Raster, background=(255, 255, 255)) -> tuple[Raster, bytes | None]:
    """Split alpha off: (color raster, alpha bytes or None)."""
    if r.mode in ("L", "RGB"):
        return r, None
    px = r.pixels
    n = r.width * r.height
    if r.mode == "LA":
        color = bytes(px[0::2])
        alpha = bytes(px[1::2])
        return Raster(r.width, r.height, "L", color), alpha
    color = bytearray(n * 3)
    color[0::3] = px[0::4]
    color[1::3] = px[1::4]
    color[2::3] = px[2::4]
    alpha = px[3::4]
    if all(a == 255 for a in alpha[:: max(1, len(alpha) // 4096)]) and alpha.count(255) == len(alpha):
        return Raster(r.width, r.height, "RGB", bytes(color)), None
    return Raster(r.width, r.height, "RGB", bytes(color)), bytes(alpha)
