"""Pictures for the PDF to PostScript writer: image XObjects and inline images as PostScript image
dictionaries (ImageType 1, 3 for masks, 4 for colour keys), in device colour spaces or Indexed over one."""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

from .. import limits
from ..pdf.parser import Document, Stream
from .write_paint import CMYK, DEVICE, GRAY, RGB, ColorSpace, _f, a85, arr, colorspace, hexstr

if TYPE_CHECKING:
    from .write import Content, Writer


class Images:
    """Image XObjects and inline images as PostScript image dictionaries."""

    CHUNK = 60_000
    SHARE = 2048  # an image smaller than this is simply written again where it's used again

    def __init__(self, w: Writer):
        self.w = w
        self.doc = w.doc

    def write(self, c: Content, d: dict, stm: Stream):
        doc = self.doc
        r = doc.resolve
        width, height = r(d.get("Width")), r(d.get("Height"))
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            return
        limits.check_pixels(width, height)
        mask = r(d.get("ImageMask")) is True
        filters = [str(r(f)) for f in (r(d.get("Filter")) if isinstance(r(d.get("Filter")), list)
                                       else [r(d.get("Filter"))] if d.get("Filter") is not None else [])]
        last = filters[-1] if filters else None
        if last in ("JPXDecode", "JBIG2Decode"):
            self.w.unshowable(f"pictures in {'JPEG 2000' if last == 'JPXDecode' else 'JBIG2'}")
            return
        if r(d.get("SMask")) is not None and not isinstance(r(d.get("SMask")), str):
            self.w.transparent()
        bpc = r(d.get("BitsPerComponent"))
        bpc = 1 if mask else bpc if isinstance(bpc, int) and bpc in (1, 2, 4, 8, 16) else 8
        decode = [_f(r(v)) for v in r(d.get("Decode"))] if isinstance(r(d.get("Decode")), list) else None
        interp = r(d.get("Interpolate")) is True
        raw = doc.decode(stm)
        share = stm.num or None
        parms = r(d.get("DecodeParms"))
        if isinstance(parms, list):
            parms = r(parms[-1]) if parms else None
        if mask:
            c.set_color(fill=True)
            if last not in ("DCTDecode", "CCITTFaxDecode"):
                raw = _padded(raw, (width + 7) // 8 * height)
            dec = decode if decode and len(decode) == 2 else [0, 1]
            self._emit(c, width, height, 1, dec, None, raw, last, parms, interp, "imagemask", key=share)
            return
        cs = colorspace(doc, d.get("ColorSpace"), c.res)
        if cs.kind == "pattern":
            return
        if last in ("DCTDecode", "CCITTFaxDecode") and cs.kind not in DEVICE and cs.kind != "indexed":
            cs = {1: GRAY, 3: RGB, 4: CMYK}.get(cs.n, RGB) if last == "DCTDecode" else GRAY
        samples_ok = last not in ("DCTDecode", "CCITTFaxDecode")
        if samples_ok:  # missing samples are zeros, as PDF viewers draw them
            raw = _padded(raw, (width * cs.n * bpc + 7) // 8 * height)
        if samples_ok and bpc == 16:
            raw = raw[0::2]
            bpc = 8
        space, dec, raw = self._space(cs, bpc, decode, raw, width, height, samples_ok)
        if space is None:
            return
        smask = r(d.get("SMask"))
        mk = r(d.get("Mask"))
        if isinstance(mk, list) and samples_ok:
            ranges = [int(_f(r(v))) for v in mk]
            self._emit(c, width, height, bpc, dec, space, raw, last, parms, interp, "image", colorkey=ranges, key=share)
            return
        if isinstance(mk, Stream) and r(mk.dict.get("ImageMask")) is True:
            self._masked(c, width, height, bpc, dec, space, raw, last, parms, interp, mk, key=share)
            return
        if isinstance(smask, Stream):
            self._masked(c, width, height, bpc, dec, space, raw, last, parms, interp, smask, soft=True, key=share)
            return
        self._emit(c, width, height, bpc, dec, space, raw, last, parms, interp, "image", key=share)

    def _space(self, cs: ColorSpace, bpc: int, decode, raw: bytes, width: int, height: int, samples_ok: bool):
        """(PostScript colour space, Decode, samples) for an image in cs."""
        if cs.kind in DEVICE:
            dec = decode if decode and len(decode) == 2 * cs.n else [0, 1] * cs.n
            return "/" + DEVICE[cs.kind][0], dec, raw
        maxv = (1 << bpc) - 1
        if cs.kind == "indexed":
            base = cs.base or RGB
            if base.kind in DEVICE:
                table = cs.table[:(cs.hival + 1) * base.n]
                table += bytes((cs.hival + 1) * base.n - len(table))
                bname = DEVICE[base.kind][0]
            else:
                rows = [cs.convert([i]) for i in range(cs.hival + 1)]
                table = bytes(max(0, min(255, round(v * 255))) for row in rows for v in row)
                bname = DEVICE[base.device][0]
            dec = decode if decode and len(decode) == 2 else [0, maxv]
            return f"[/Indexed /{bname} {cs.hival} {hexstr(table)}]", dec, raw
        if not samples_ok:
            return None, None, raw
        # one component (Separation, an Indexed-like lookup): a lookup table over the device space
        dec = decode if decode and len(decode) == 2 * cs.n else [0, 1] * cs.n
        if cs.n == 1 and bpc <= 8:
            rows = []
            for i in range(maxv + 1):
                v = dec[0] + i * (dec[1] - dec[0]) / maxv
                rows.append(cs.convert([v]))
            table = bytes(max(0, min(255, round(x * 255))) for row in rows for x in row)
            return f"[/Indexed /{DEVICE[cs.device][0]} {maxv} {hexstr(table)}]", [0, maxv], raw
        # several components (DeviceN, Lab): each pixel converted
        n = cs.n
        out_n = DEVICE[cs.device][1]
        row_bytes = (width * n * bpc + 7) // 8
        out = bytearray()
        cache: dict = {}
        for y in range(height):
            row = raw[y * row_bytes:(y + 1) * row_bytes]
            vals = _unpack(row, bpc, width * n)
            for x in range(width):
                px = tuple(vals[x * n:(x + 1) * n])
                got = cache.get(px)
                if got is None:
                    comps = [dec[2 * i] + px[i] * (dec[2 * i + 1] - dec[2 * i]) / maxv for i in range(n)]
                    got = bytes(max(0, min(255, round(v * 255))) for v in cs.convert(comps))
                    if len(cache) < 100_000:
                        cache[px] = got
                out += got
        return "/" + DEVICE[cs.device][0], [0, 1] * out_n, bytes(out)

    def _source(self, c: Content, data: bytes, last: str | None, parms, key=None) -> tuple[str, bytes]:
        """(DataSource expression, data to follow in the file or b"" when it's in the expression). An image the
        document uses more than once (``key``: its object number) is kept in the setup and read from there."""
        if last == "DCTDecode":
            payload, filt = data, " /DCTDecode filter"
        elif last == "CCITTFaxDecode":
            payload, filt = data, " " + _ccitt(self.doc, parms) + " /CCITTFaxDecode filter"
        else:
            payload, filt = zlib.compress(data, 6), " /FlateDecode filter"
        if key is not None and self.w.image_uses.get(key, 0) > 1 and len(payload) > self.SHARE:
            pname = self.w.shared_images.get(key)
            if pname is None:
                pname = self.w.shared_images[key] = f"Im{key}"
                self.w.setup.append(f"/{pname} [" + "\n".join("<~" + a85(ch) for ch in _chunks(payload, self.CHUNK))
                                    + "] def")
            return f"{pname} mksrc" + filt, b""
        if c.inline:
            return "currentfile /ASCII85Decode filter" + filt, payload
        strings = "\n".join("<~" + a85(ch) for ch in _chunks(payload, self.CHUNK))
        return "[" + strings + "] mksrc" + filt, b""

    def _emit(self, c: Content, width, height, bpc, decode, space, raw, last, parms, interp, op, colorkey=None,
              key=None):
        src, payload = self._source(c, raw, last, parms, key)
        parts = [f"/ImageType {4 if colorkey else 1} /Width {width} /Height {height} /BitsPerComponent {bpc}",
                 f"/Decode {arr(decode)} /ImageMatrix [{width} 0 0 {-height} 0 {height}]"]
        if interp:
            parts.append("/Interpolate true")
        if colorkey:
            parts.append("/MaskColor " + arr(colorkey))
        parts.append("/DataSource " + src)
        if space is not None:
            c.emit(space + " setcolorspace")
            c.gs.ps_color = None
        c.emit("<< " + "\n".join(parts) + " >> " + op)
        if payload:
            c.emit(a85(payload))

    def _masked(self, c: Content, width, height, bpc, decode, space, raw, last, parms, interp, mstm: Stream,
                soft: bool = False, key=None):
        doc = self.doc
        r = doc.resolve
        md = mstm.dict
        mw, mh = r(md.get("Width")), r(md.get("Height"))
        if not isinstance(mw, int) or not isinstance(mh, int) or mw <= 0 or mh <= 0:
            self._emit(c, width, height, bpc, decode, space, raw, last, parms, interp, "image")
            return
        limits.check_pixels(mw, mh)
        mraw = doc.decode(mstm)
        mfilters = doc.stream_filters(mstm)
        if mfilters and mfilters[-1] in ("JPXDecode", "JBIG2Decode", "DCTDecode", "CCITTFaxDecode"):
            self._emit(c, width, height, bpc, decode, space, raw, last, parms, interp, "image")
            return
        if soft:
            # PostScript has no soft masks: shown where the mask is at least half on (a note says so)
            mbpc = r(md.get("BitsPerComponent")) or 8
            vals = _unpack(mraw, mbpc if mbpc in (1, 2, 4, 8, 16) else 8, mw * mh) if mbpc != 8 else mraw
            half = ((1 << (mbpc if mbpc in (1, 2, 4, 8) else 8)) - 1) / 2
            bits = bytearray()
            row_bytes = (mw + 7) // 8
            for y in range(mh):
                row = bytearray(row_bytes)
                for x in range(mw):
                    k = y * mw + x
                    v = vals[k] if k < len(vals) else 0
                    if v > half:
                        row[x >> 3] |= 0x80 >> (x & 7)
                bits += row
            mraw = bytes(bits)
            mdec = [1, 0]  # 1 = shown
        else:
            mraw = _padded(mraw, (mw + 7) // 8 * mh)
            md_dec = r(md.get("Decode"))
            mdec = [1, 0] if isinstance(md_dec, list) and md_dec and _f(r(md_dec[0])) == 1 else [0, 1]
        src, payload = self._source(c, raw, last, parms, key)
        chunks = [zlib.compress(mraw, 6)]
        mstrings = "\n".join("<~" + a85(ch) for ch in _chunks(chunks[0], self.CHUNK))
        data_dict = (f"<< /ImageType 1 /Width {width} /Height {height} /BitsPerComponent {bpc} /Decode {arr(decode)} "
                     f"/ImageMatrix [{width} 0 0 {-height} 0 {height}]" + (" /Interpolate true" if interp else "")
                     + f"\n/DataSource {src} >>")
        mask_dict = (f"<< /ImageType 1 /Width {mw} /Height {mh} /BitsPerComponent 1 /Decode {arr(mdec)} "
                     f"/ImageMatrix [{mw} 0 0 {-mh} 0 {mh}]\n/DataSource [{mstrings}] mksrc /FlateDecode filter >>")
        if space is not None:
            c.emit(space + " setcolorspace")
            c.gs.ps_color = None
        c.emit(f"<< /ImageType 3 /InterleaveType 3 /DataDict {data_dict}\n/MaskDict {mask_dict} >> image")
        if payload:
            c.emit(a85(payload))
        if soft:
            self.w.note("Pictures with soft edges or see-through parts are shown with hard edges")




def _padded(data: bytes, size: int) -> bytes:
    return data + bytes(size - len(data)) if len(data) < size else data


def _chunks(data: bytes, n: int) -> list[bytes]:
    return [data[i:i + n] for i in range(0, len(data), n)] or [b""]


def _unpack(row: bytes, bpc: int, count: int) -> list[int]:
    if bpc == 8:
        return list(row[:count])
    if bpc == 16:
        return [int.from_bytes(row[2 * i:2 * i + 2], "big") >> 8 for i in range(count)]
    out = []
    mask = (1 << bpc) - 1
    per = 8 // bpc
    for b in row:
        for k in range(per):
            out.append((b >> (8 - bpc * (k + 1))) & mask)
            if len(out) >= count:
                return out
    return out + [0] * (count - len(out))


def _ccitt(doc: Document, parms) -> str:
    p = parms if isinstance(parms, dict) else {}
    parts = []
    for k in ("K", "Columns", "Rows", "EncodedByteAlign", "EndOfLine", "EndOfBlock", "BlackIs1", "DamagedRowsBeforeError"):
        v = doc.resolve(p.get(k))
        if isinstance(v, bool):
            parts.append(f"/{k} {'true' if v else 'false'}")
        elif isinstance(v, int):
            parts.append(f"/{k} {v}")
    return "<< " + " ".join(parts) + " >>"
