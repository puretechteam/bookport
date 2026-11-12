"""Images as PDF XObjects: JPEG passed through as is, PNG passed through when it can be
(non-interlaced, no alpha), otherwise decoded to samples with a soft mask for transparency."""

from __future__ import annotations

import struct
import zlib

from .. import images as imgmod
from .objects import Name, Ref, Writer


def _jpeg_info(data: bytes):
    i = 2
    adobe = False
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xEE and data[i + 4:i + 9] == b"Adobe":
            adobe = True
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            bits = data[i + 4]
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            comps = data[i + 9]
            return w, h, comps, bits, adobe
        i += 2 + length
    return None


def embed(writer: Writer, data: bytes) -> tuple[Ref, int, int] | None:
    if data[:3] == b"\xff\xd8\xff":
        info = _jpeg_info(data)
        if info is None:
            return None
        w, h, comps, bits, adobe = info
        cs = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(comps)
        if cs is None:
            return None
        d = {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": w, "Height": h, "ColorSpace": Name(cs),
             "BitsPerComponent": bits, "Filter": Name("DCTDecode")}
        if comps == 4 and adobe:
            d["Decode"] = [1, 0, 1, 0, 1, 0, 1, 0]
        return writer.stream(data, d, compress=False), w, h
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            info = imgmod.png_info(data)
        except (StopIteration, struct.error):
            return None
        chunks = {k for k, _ in imgmod._chunks(data)}
        if not info["interlace"] and info["color"] in (0, 2) and info["depth"] in (8, 16) and b"tRNS" not in chunks:
            idat = b"".join(c for k, c in imgmod._chunks(data) if k == b"IDAT")
            colors = 1 if info["color"] == 0 else 3
            d = {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": info["width"], "Height": info["height"],
                 "ColorSpace": Name("DeviceGray" if colors == 1 else "DeviceRGB"), "BitsPerComponent": info["depth"],
                 "Filter": Name("FlateDecode"),
                 "DecodeParms": {"Predictor": 15, "Colors": colors, "BitsPerComponent": info["depth"],
                                 "Columns": info["width"]}}
            return writer.stream(idat, d, compress=False), info["width"], info["height"]
    try:
        r = imgmod.decode(data)
    except Exception:
        r = None
    if r is None:
        return None
    color, alpha = imgmod.flatten_alpha(r)
    d = {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": r.width, "Height": r.height,
         "ColorSpace": Name("DeviceGray" if color.mode == "L" else "DeviceRGB"), "BitsPerComponent": 8}
    if alpha is not None:
        d["SMask"] = writer.stream(zlib.compress(alpha, 6), {"Type": Name("XObject"), "Subtype": Name("Image"),
                                                              "Width": r.width, "Height": r.height,
                                                              "ColorSpace": Name("DeviceGray"), "BitsPerComponent": 8,
                                                              "Filter": Name("FlateDecode")}, compress=False)
    return writer.stream(color.pixels, d), r.width, r.height
