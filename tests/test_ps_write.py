"""PDF to PostScript and EPS (bookport.ps.write): each test PDF is drawn by PyMuPDF and its PostScript by Ghostscript
(used only as a black box, on temporary copies), and the two pictures are compared with SSIM at 150 dpi. Tests of
the file's structure, glyph placement, transparency and hostile input run without Ghostscript."""

import io
import itertools
import lzma
import os
import random
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

import pytest

from bookport import BookportError, ps
from bookport.pdf.objects import Name as N
from bookport.pdf.objects import Writer
from bookport.ps import write

pymupdf = pytest.importorskip("pymupdf")
np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


def _gs():
    for c in (os.environ.get("BOOKPORT_GS"), shutil.which("gswin64c"), shutil.which("gs"),
              Path(os.environ.get("LOCALAPPDATA", "")) / "ConvertAll" / "engines" / "ghostscript" / "bin" / "gswin64c.exe"):
        if c and Path(c).is_file():
            return str(c)
    return None


GS = _gs()
needs_gs = pytest.mark.skipif(GS is None, reason="Ghostscript isn't installed (BOOKPORT_GS)")
FONTS = Path(__file__).parents[1] / "src" / "bookport" / "fonts"
DPI = 150


# --- drawing and comparing -------------------------------------------------------------------------------------

def pdf_pictures(pdf: bytes) -> list:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    out = []
    for p in doc:
        pix = p.get_pixmap(dpi=DPI, alpha=False)
        out.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3))
    return out


def gs_run(data: bytes, tmp: Path, *args, name="in.ps") -> subprocess.CompletedProcess:
    src = tmp / name
    src.write_bytes(data)
    return subprocess.run([GS, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", *args, str(src)], capture_output=True, text=True,
                          timeout=300, cwd=tmp, check=False)


def ps_pictures(data: bytes, tmp: Path, eps=False) -> list:
    r = gs_run(data, tmp, "-sDEVICE=png16m", f"-r{DPI}", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
               *(["-dEPSCrop"] if eps else []), f"-sOutputFile={tmp / 'p%03d.png'}", name="in.eps" if eps else "in.ps")
    assert "Error" not in r.stdout + r.stderr, (r.stdout + r.stderr)[:2000]
    return [np.asarray(Image.open(f).convert("RGB")) for f in sorted(tmp.glob("p*.png"))]


def gs_pdf(data: bytes, tmp: Path) -> bytes:
    """Ghostscript's PDF of our PostScript (to read the text back)."""
    gs_run(data, tmp, "-sDEVICE=pdfwrite", f"-sOutputFile={tmp / 'back.pdf'}")
    return (tmp / "back.pdf").read_bytes()


def ssim(a, b) -> float:
    """Mean SSIM (7x7 windows, on grey) of two pictures, cropped to the smaller one (they differ by a pixel at most)."""
    assert abs(a.shape[0] - b.shape[0]) <= 2 and abs(a.shape[1] - b.shape[1]) <= 2, (a.shape, b.shape)
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    x = a[:h, :w].astype(np.float64).mean(axis=2)
    y = b[:h, :w].astype(np.float64).mean(axis=2)
    k = 7

    def box(m):
        c = np.cumsum(np.cumsum(np.pad(m, ((1, 0), (1, 0))), 0), 1)
        return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    mx, my = box(x), box(y)
    vx, vy, cxy = box(x * x) - mx * mx, box(y * y) - my * my, box(x * y) - mx * my
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return float((((2 * mx * my + c1) * (2 * cxy + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2))).mean())


def scores(pdf: bytes, tmp: Path, eps=False, **kw) -> list[float]:
    data = write.convert(pdf, eps=eps, **kw)
    want, got = pdf_pictures(pdf), ps_pictures(data, tmp, eps)
    if eps:
        want = want[:1]
    assert len(want) == len(got)
    return [ssim(a, b) for a, b in zip(want, got)]


def text_of(pdf: bytes) -> str:
    return " ".join(" ".join(p.get_text() for p in pymupdf.open(stream=pdf, filetype="pdf")).split())


def char_origins(pdf: bytes) -> list:
    out = []
    for p in pymupdf.open(stream=pdf, filetype="pdf"):
        for b in p.get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    out += [(p.number, ch["c"], ch["origin"]) for ch in sp["chars"] if ch["c"].strip()]
    return out


# --- test PDFs -------------------------------------------------------------------------------------------------

def build(pages, catalog=None) -> bytes:
    """pages: dicts of content (bytes), resources (dict, or a function of the Writer), box, rotate, crop, annots."""
    w = Writer()
    root, tree = w.reserve(), w.reserve()
    kids = []
    for p in pages:
        res = p.get("resources", {})
        d = {"Type": N("Page"), "Parent": tree, "MediaBox": p.get("box", [0, 0, 400, 300]),
             "Resources": res(w) if callable(res) else res, "Contents": w.stream(p["content"])}
        for key, pdf_key in (("rotate", "Rotate"), ("crop", "CropBox")):
            if key in p:
                d[pdf_key] = p[key]
        if "annots" in p:
            d["Annots"] = p["annots"](w)
        kids.append(w.add(d))
    w.put(tree, {"Type": N("Pages"), "Kids": kids, "Count": len(kids)})
    cat = {"Type": N("Catalog"), "Pages": tree}
    if catalog:
        cat.update(catalog(w))
    w.put(root, cat)
    return w.finish(root)


def helvetica(w, base="Helvetica", **extra):
    return w.add({"Type": N("Font"), "Subtype": N("Type1"), "BaseFont": N(base), **extra})


def gradient(wd=48, ht=32):
    img = Image.new("RGB", (wd, ht))
    img.putdata([(x * 255 // wd, y * 255 // ht, 128) for y in range(ht) for x in range(wd)])
    return img


def image(w, img, **extra):
    cs = {"L": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK"}[img.mode]
    return w.stream(img.tobytes(), {"Type": N("XObject"), "Subtype": N("Image"), "Width": img.width,
                                    "Height": img.height, "BitsPerComponent": 8, "ColorSpace": N(cs), **extra})


def calc(w, prog: bytes, domain, rng):
    return w.stream(prog, {"FunctionType": 4, "Domain": domain, "Range": rng}, compress=False)


def paths_pdf():
    return build([{"content": b"q 200 150 m 300 150 l 300 250 l 100 250 l 100 50 l 200 50 l h W* n 0 0 1 rg 0 0 400 300 re f Q"
                              b" q 20 20 100 100 re W n 1 0 0 rg 0.2 g 50 50 m 150 50 l 150 150 l h 1 0 0 RG 4 w B Q"
                              b" 0 1 0 RG 6 w 1 J 1 j [10 6] 3 d 250 30 m 380 120 l S 0 0 0 1 k 300 200 60 40 re f"
                              b" 0.5 g 20 270 m 60 290 100 250 140 270 c 180 290 220 280 v 260 250 300 270 y S"}])


def shadings_pdf():
    def res(w):
        stitch = {"FunctionType": 3, "Domain": [0, 1], "Bounds": [0.5], "Encode": [0, 1, 0, 1],
                  "Functions": [{"FunctionType": 2, "Domain": [0, 1], "C0": [1, 0, 0], "C1": [0, 1, 0], "N": 1},
                                {"FunctionType": 2, "Domain": [0, 1], "C0": [0, 1, 0], "C1": [0, 0, 1], "N": 1}]}
        sampled = w.stream(bytes(v for i in range(16) for v in (i * 17, 255 - i * 17, 128)),
                           {"FunctionType": 0, "Domain": [0, 1], "Range": [0, 1, 0, 1, 0, 1], "Size": [16], "BitsPerSample": 8})
        spot = [N("Separation"), N("Spot"), N("DeviceCMYK"),
                calc(w, b"{ dup 0.8 mul exch dup 0 mul exch 0.3 mul 0 }", [0, 1], [0, 1] * 4)]
        devn = [N("DeviceN"), [N("A"), N("B")], N("DeviceRGB"), calc(w, b"{ 1 exch sub exch 0.5 }", [0, 1, 0, 1], [0, 1] * 3)]
        return {"Shading": {
            "A": {"ShadingType": 2, "ColorSpace": N("DeviceRGB"), "Coords": [20, 0, 190, 0], "Function": stitch,
                  "Extend": [True, True]},
            "R": {"ShadingType": 3, "ColorSpace": N("DeviceRGB"), "Coords": [295, 80, 5, 295, 80, 80], "Function": sampled,
                  "Extend": [False, True]},
            "D": {"ShadingType": 2, "ColorSpace": devn, "Coords": [20, 160, 190, 280],
                  "Function": calc(w, b"{ dup 0.5 mul exch dup mul 1 exch sub }", [0, 1], [0, 1, 0, 1])},
            "S": {"ShadingType": 2, "ColorSpace": spot, "Coords": [210, 0, 380, 0],
                  "Function": {"FunctionType": 2, "Domain": [0, 1], "C0": [0], "C1": [1], "N": 2}}}}
    return build([{"content": b"q 20 20 170 120 re W n /A sh Q q 210 20 170 120 re W n /R sh Q "
                              b"q 20 160 170 120 re W n /D sh Q q 210 160 170 120 re W n /S sh Q", "resources": res}])


def mesh_pdf():
    pts = [(0, 20, 20, 255, 0, 0), (0, 380, 20, 0, 255, 0), (0, 200, 280, 0, 0, 255), (1, 380, 280, 255, 255, 0)]
    data = b"".join(bytes([f]) + int(x * 65535 / 400).to_bytes(2, "big") + int(y * 65535 / 300).to_bytes(2, "big")
                    + bytes([r, g, b]) for f, x, y, r, g, b in pts)
    return build([{"content": b"/M sh", "resources": lambda w: {"Shading": {"M": w.stream(data, {
        "ShadingType": 4, "ColorSpace": N("DeviceRGB"), "BitsPerCoordinate": 16, "BitsPerComponent": 8, "BitsPerFlag": 8,
        "Decode": [0, 400, 0, 300, 0, 1, 0, 1, 0, 1]})}}}])


def patterns_pdf():
    def res(w):
        tile = {"Type": N("Pattern"), "PatternType": 1, "TilingType": 1, "BBox": [0, 0, 20, 20], "Resources": {}}
        p1 = w.stream(b"0 0 1 rg 0 0 10 10 re f 1 0 0 rg 10 10 10 10 re f",
                      {**tile, "PaintType": 1, "XStep": 20, "YStep": 20, "Matrix": [1, 0.3, 0, 1, 0, 0]})
        p2 = w.stream(b"0 0 m 20 10 l 0 20 l f", {**tile, "PaintType": 2, "XStep": 25, "YStep": 25})
        p3 = w.add({"Type": N("Pattern"), "PatternType": 2, "Matrix": [1, 0, 0, 1, 0, 160], "Shading": {
            "ShadingType": 2, "ColorSpace": N("DeviceRGB"), "Coords": [0, 0, 360, 0], "Extend": [True, True],
            "Function": {"FunctionType": 2, "Domain": [0, 1], "C0": [1, 1, 0], "C1": [0.5, 0, 0.5], "N": 1}}})
        return {"Pattern": {"P1": p1, "P2": p2, "P3": p3}, "ColorSpace": {"U": [N("Pattern"), N("DeviceRGB")]}}
    return build([{"box": [0, 0, 400, 320], "resources": res,
                   "content": b"/Pattern cs /P1 scn 20 20 170 120 re f /U cs 0.2 0.7 0.3 /P2 scn 210 20 170 120 re f "
                              b"/Pattern cs /P3 scn 20 160 360 120 re f /Pattern CS /P1 SCN 12 w 40 300 m 360 300 l S"}])


def forms_pdf():
    def res(w):
        fm = w.stream(b"0 0 1 RG 3 w 0 0 m 50 50 l S 1 0 0 rg 25 0 25 25 re f BT /F1 10 Tf 2 40 Td (Form) Tj ET",
                      {"Type": N("XObject"), "Subtype": N("Form"), "BBox": [0, 0, 40, 60],
                       "Resources": {"Font": {"F1": helvetica(w)}}})
        nest = w.stream(b"q 0.5 0 0 0.5 0 0 cm /Fm Do Q 0 g 0 0 10 10 re f",
                        {"Type": N("XObject"), "Subtype": N("Form"), "BBox": [0, 0, 400, 300],
                         "Matrix": [1, 0, 0, 1, 40, 200], "Resources": {"XObject": {"Fm": fm}}})
        return {"XObject": {"Fm": fm, "Nest": nest}}
    return build([{"content": b"q 1 0 0 1 20 20 cm /Fm Do Q q 2 0 0 2 150 40 cm /Fm Do Q "
                              b"q 0.7 0.7 -0.7 0.7 330 60 cm /Fm Do Q /Nest Do", "resources": res}])


def type3_pdf():
    square = b"1000 0 d0 1 0 0 rg 100 0 800 800 re f 0 0 1 rg 300 200 400 400 re f"
    bits = bytes([0xF0, 0xF0, 0x0F, 0x0F] * 2)
    stencil = b"1000 0 0 0 1000 1000 d1 q 1000 0 0 1000 0 0 cm BI /W 8 /H 8 /IM true /BPC 1 ID " + bits + b" EI Q"

    def res(w):
        return {"Font": {"T3": w.add({
            "Type": N("Font"), "Subtype": N("Type3"), "FontBBox": [0, 0, 1000, 1000], "FontMatrix": [0.001, 0, 0, 0.001, 0, 0],
            "CharProcs": {"sq": w.stream(square), "mk": w.stream(stencil, compress=False)},
            "Encoding": {"Type": N("Encoding"), "Differences": [97, N("sq"), N("mk")]},
            "FirstChar": 97, "LastChar": 98, "Widths": [1100, 1000], "Resources": {}})}}
    return build([{"content": b"0 0.5 0 rg BT /T3 40 Tf 20 200 Td (abab) Tj 0 -80 Td 2 Tr (ba) Tj ET", "resources": res}])


def images_pdf():
    img = gradient()

    def res(w):
        base = {"Type": N("XObject"), "Subtype": N("Image"), "Width": 48, "Height": 32, "BitsPerComponent": 8}
        pal = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 250, 200, 0])
        buf = io.BytesIO()
        img.convert("CMYK").save(buf, "JPEG", quality=92)
        stencil = w.stream(bytes([0xF0, 0x0F, 0x33, 0x55, 0xAA, 0x0F] * 32),
                           {**base, "BitsPerComponent": 1, "ImageMask": True})
        return {"XObject": {
            "Rgb": image(w, img), "Gray": image(w, img.convert("L")), "Cmyk": image(w, img.convert("CMYK")),
            "Idx": w.stream(bytes((x // 12 + y // 8) % 4 for y in range(32) for x in range(48)),
                            {**base, "ColorSpace": [N("Indexed"), N("DeviceRGB"), 3, pal]}),
            "Idx4": w.stream(bytes(((x // 12) << 4 | (x // 12 + 1) % 4) for y in range(32) for x in range(0, 48, 2)),
                             {**base, "BitsPerComponent": 4, "ColorSpace": [N("Indexed"), N("DeviceRGB"), 3, pal]}),
            "Sep": w.stream(bytes((x * 5) % 256 for y in range(32) for x in range(48)),
                            {**base, "ColorSpace": [N("Separation"), N("Green"), N("DeviceRGB"),
                                                    calc(w, b"{ dup 0 exch 0.6 mul 0 }", [0, 1], [0, 1] * 3)]}),
            "Jpg": w.stream(buf.getvalue(), {**base, "ColorSpace": N("DeviceCMYK"), "Filter": N("DCTDecode"),
                                             "Decode": [1, 0] * 4}, compress=False),
            "Mk": w.stream(bytes([0b10101010, 0b01010101, 0xFF, 0, 0xF0, 0x0F] * 32), {**base, "BitsPerComponent": 1,
                                                                                    "ImageMask": True}),
            "G16": w.stream(b"".join((x * 1300).to_bytes(2, "big") for y in range(32) for x in range(48)),
                            {**base, "BitsPerComponent": 16, "ColorSpace": N("DeviceGray")}),
            "Key": image(w, img, Mask=[0, 120, 0, 255, 0, 255]),
            "Stencil": image(w, img, Mask=stencil)}}
    names = [b"Rgb", b"Gray", b"Cmyk", b"Idx", b"Idx4", b"Sep", b"Jpg", b"Mk", b"G16", b"Key", b"Stencil"]
    content = b"1 0.9 0.8 rg 0 0 400 300 re f 0.6 0 0.6 rg "
    for i, nm in enumerate(names):
        content += b"q 80 0 0 60 %d %d cm /%s Do Q\n" % (15 + (i % 4) * 95, 210 - (i // 4) * 95, nm)
    return build([{"content": content, "resources": res}])


def inline_and_fax_pdf():
    img = Image.new("1", (64, 40), 1)
    img.putdata([0 if (x // 8 + y // 8) % 2 else 1 for y in range(40) for x in range(64)])
    buf = io.BytesIO()
    img.save(buf, "TIFF", compression="group4")
    tif = Image.open(io.BytesIO(buf.getvalue()))
    g4 = buf.getvalue()[tif.tag_v2[273][0]:tif.tag_v2[273][0] + tif.tag_v2[279][0]]
    raw = bytes(v for y in range(20) for x in range(30) for v in (x * 8, y * 12, 100))
    import zlib
    return build([{"resources": lambda w: {"XObject": {"Fax": w.stream(g4, {
        "Type": N("XObject"), "Subtype": N("Image"), "Width": 64, "Height": 40, "BitsPerComponent": 1,
        "ColorSpace": N("DeviceGray"), "Filter": N("CCITTFaxDecode"), "DecodeParms": {"K": -1, "Columns": 64, "Rows": 40}},
        compress=False)}},
        "content": b"q 160 0 0 100 20 180 cm /Fax Do Q\nq 150 0 0 100 220 180 cm BI /W 30 /H 20 /CS /RGB /BPC 8 ID " + raw
                   + b"\nEI Q\nq 150 0 0 100 220 40 cm BI /W 30 /H 20 /CS /RGB /BPC 8 /F /Fl ID " + zlib.compress(raw)
                   + b"\nEI Q\n"}])


def text_resources(w):
    return {"Font": {"F1": helvetica(w, Encoding=N("WinAnsiEncoding")), "F2": helvetica(w, "Times-Roman"),
                     "F3": helvetica(w, "Courier", Encoding=N("WinAnsiEncoding")), "F4": helvetica(w, "Helvetica-Bold")}}


def text_pdf_content() -> bytes:
    return (b"BT /F1 16 Tf 20 260 Td (Plain text) Tj 0 -24 Td 3 Tc (Char spaced) Tj 0 Tc 8 Tw 0 -24 Td (Word spaced)"
                   b" Tj 0 Tw 150 Tz 0 -24 Td (Scaled 150) Tj 100 Tz 0 -24 Td [(Kern) -500 (ed) 300 (TJ)] Tj 0 -24 Td 5 Ts"
                   b" (Rise) Tj 0 Ts 18 TL T* (Next line) Tj (more) ' 2 1 (quote) \" ET\n"
                   b"BT /F2 20 Tf 0.9 0.3 -0.3 0.9 250 120 Tm (Rotated Times) Tj ET\n"
                   b"BT /F3 14 Tf 250 260 Td (Courier \\(x\\) \\\\ \\351t\\351) Tj ET\n"
                   b"BT /F4 30 Tf 0 0 1 rg 1 0 0 RG 1.5 w 1 Tr 230 40 Td (Out) Tj 2 Tr 70 0 Td (Both) Tj 3 Tr (Gone) Tj ET\n"
                   b"BT 7 Tr /F4 48 Tf 230 180 Td (CLIP) Tj ET 220 170 160 50 re 0 1 0 rg f\n")


def text_pdf():
    return build([{"resources": text_resources, "content": text_pdf_content()}])


def rotated_pdf(rot):
    return build([{"rotate": rot, "crop": [5, 5, 395, 295],
                   "resources": lambda w: {"Font": {"F1": helvetica(w)}},
                   "content": b"1 0 0 rg 10 10 100 50 re f 0 0 1 rg 290 240 100 50 re f BT /F1 24 Tf 120 140 Td (Up) Tj ET"}])


def optional_pdf():
    holder = {}  # the two groups, made with the page and named again in the catalog

    def res(w):
        holder["on"], holder["off"] = w.add({"Type": N("OCG"), "Name": "On"}), w.add({"Type": N("OCG"), "Name": "Off"})
        hidden = w.stream(b"0 1 0 rg 0 0 50 50 re f", {"Type": N("XObject"), "Subtype": N("Form"), "BBox": [0, 0, 50, 50],
                                                       "Matrix": [1, 0, 0, 1, 200, 220], "OC": holder["off"]})
        return {"Properties": {"On": holder["on"], "Off": holder["off"]}, "XObject": {"Hid": hidden}}

    def cat(w):
        return {"OCProperties": {"OCGs": [holder["on"], holder["off"]], "D": {"OFF": [holder["off"]]}}}
    return build([{"resources": res, "content": b"/OC /On BDC 1 0 0 rg 20 20 150 100 re f EMC "
                                               b"/OC /Off BDC 0 0 1 rg 220 20 150 100 re f EMC /Hid Do"}], catalog=cat)


def annots_pdf():
    def annots(w):
        ap = w.stream(b"0.9 0.9 0 rg 0 0 100 30 re f 0 g BT /Helv 12 Tf 5 10 Td (Note) Tj ET",
                      {"Type": N("XObject"), "Subtype": N("Form"), "BBox": [0, 0, 100, 30],
                       "Resources": {"Font": {"Helv": helvetica(w)}}})
        hidden = w.stream(b"1 0 0 rg 0 0 10 10 re f", {"Type": N("XObject"), "Subtype": N("Form"), "BBox": [0, 0, 10, 10]})
        return [w.add({"Type": N("Annot"), "Subtype": N("FreeText"), "Rect": [50, 200, 250, 260], "F": 4, "AP": {"N": ap}}),
                w.add({"Type": N("Annot"), "Subtype": N("Square"), "Rect": [300, 50, 350, 100], "F": 2, "AP": {"N": hidden}})]
    return build([{"content": b"0 0 1 rg 20 20 40 40 re f", "annots": annots}])


def short_data_pdf():
    """Pictures whose data stops halfway: PDF viewers draw the rest as zeros (painted, for a mask)."""
    def res(w):
        base = {"Type": N("XObject"), "Subtype": N("Image"), "Width": 48, "Height": 32}
        return {"XObject": {"M": w.stream(bytes([0xAA, 0x55] * 48), {**base, "BitsPerComponent": 1, "ImageMask": True}),
                            "G": w.stream(bytes(range(0, 256, 3)) * 8, {**base, "BitsPerComponent": 8,
                                                                         "ColorSpace": N("DeviceGray")})}}
    return build([{"resources": res, "content": b"1 0 0 rg q 160 0 0 120 20 100 cm /M Do Q q 160 0 0 120 210 100 cm /G Do Q"}])


CASES = {  # name: (PDF, lowest SSIM accepted)
    "short_data": (short_data_pdf, 0.97),
    "paths": (paths_pdf, 0.99), "shadings": (shadings_pdf, 0.99), "mesh": (mesh_pdf, 0.98),
    "patterns": (patterns_pdf, 0.95), "forms": (forms_pdf, 0.99), "type3": (type3_pdf, 0.99),
    "images": (images_pdf, 0.96), "inline_and_fax": (inline_and_fax_pdf, 0.98), "text": (text_pdf, 0.98),
    "rotate90": (lambda: rotated_pdf(90), 0.99), "rotate180": (lambda: rotated_pdf(180), 0.99),
    "rotate270": (lambda: rotated_pdf(270), 0.99), "optional": (optional_pdf, 0.99), "annotations": (annots_pdf, 0.99),
}


# --- embedded fonts (made from bookport's Noto Serif with fontTools) -----------------------------------------------

TEXT = "Hello, embedded fonts 123!"


def _noto() -> bytes:
    return lzma.decompress((FONTS / "NotoSerif-Regular.ttf.xz").read_bytes())


def _ttf() -> bytes:
    subset = pytest.importorskip("fontTools.subset")
    from fontTools.ttLib import TTFont
    opts = subset.Options()
    opts.layout_features = []
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    ft = TTFont(io.BytesIO(_noto()))
    sub = subset.Subsetter(opts)
    sub.populate(text=TEXT + "AaBb")
    sub.subset(ft)
    out = io.BytesIO()
    ft.save(out)
    return out.getvalue()


def _otf() -> bytes:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.ttLib import TTFont
    src = TTFont(io.BytesIO(_ttf()))
    gs = src.getGlyphSet()
    order = src.getGlyphOrder()
    charstrings = {}
    for g in order:
        pen = T2CharStringPen(gs[g].width, gs)
        gs[g].draw(pen)
        charstrings[g] = pen.getCharString()
    fb = FontBuilder(src["head"].unitsPerEm, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(src.getBestCmap())
    fb.setupCFF("NotoTestCFF", {"FullName": "Noto Test CFF"}, charstrings, {})
    fb.setupHorizontalMetrics({g: tuple(src["hmtx"][g]) for g in order})
    fb.setupHorizontalHeader(ascent=1069, descent=-293)
    fb.setupNameTable({"familyName": "NotoTestCFF", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    out = io.BytesIO()
    fb.font.save(out)
    return out.getvalue()


def _bare_cff(otf: bytes) -> bytes:
    from fontTools.ttLib import TTFont
    ft = TTFont(io.BytesIO(otf))
    cff = ft["CFF "].cff
    cff.desubroutinize()
    out = io.BytesIO()
    cff.compile(out, ft)
    return out.getvalue()


def _cid_cff(otf: bytes) -> tuple[bytes, dict, dict]:
    """A CID-keyed CFF assembled by hand (TN 5176) from the OTF's charstrings: glyph i gets CID 1000 + 7i.
    Returns (cff, {character: cid}, {cid: width})."""
    from fontTools.ttLib import TTFont
    ft = TTFont(io.BytesIO(otf))
    cff = ft["CFF "].cff
    cff.desubroutinize()
    top = cff[cff.fontNames[0]]
    order = ft.getGlyphOrder()
    css = []
    for g in order:
        cs = top.CharStrings[g]
        cs.compile()
        css.append(cs.bytecode)
    cids = [0] + [1000 + 7 * i for i in range(1, len(order))]

    def int5(v):
        return b"\x1d" + struct.pack(">i", v)

    def index(items):
        offs = [1]
        for it in items:
            offs.append(offs[-1] + len(it))
        return struct.pack(">HB", len(items), 4) + b"".join(struct.pack(">I", o) for o in offs) + b"".join(items)
    private = bytes([139, 20, 139, 21])  # defaultWidthX 0, nominalWidthX 0
    header, names, strings, gsubrs = b"\x01\x00\x04\x04", index([b"NotoTestCID"]), index([b"Adobe", b"Identity"]), b"\0\0"
    charset = b"\x00" + b"".join(struct.pack(">H", c) for c in cids[1:])
    fdselect = b"\x03" + struct.pack(">HHBH", 1, 0, 0, len(order))
    offs = [0] * 5  # charset, fdselect, private, fdarray, charstrings: every int is 5 bytes, so two passes settle them
    for _ in range(2):
        topd = (int5(391) + int5(392) + int5(0) + b"\x0c\x1e" + int5(offs[0]) + b"\x0f" + int5(offs[4]) + b"\x11"
                + int5(offs[3]) + b"\x0c\x24" + int5(offs[1]) + b"\x0c\x25" + int5(max(cids) + 1) + b"\x0c\x22")
        pos = len(header) + len(names) + len(index([topd])) + len(strings) + len(gsubrs)
        offs[0] = pos
        offs[1] = pos = pos + len(charset)
        offs[2] = pos = pos + len(fdselect)
        fdarray = index([int5(len(private)) + int5(offs[2]) + b"\x12"])
        offs[3] = pos = pos + len(private)
        offs[4] = pos + len(fdarray)
    data = header + names + index([topd]) + strings + gsubrs + charset + fdselect + private + fdarray + index(css)
    cmap = ft.getBestCmap()
    upm = ft["head"].unitsPerEm
    by_char = {ch: cids[order.index(cmap[ord(ch)])] for ch in set(TEXT)}
    widths = {by_char[ch]: round(ft["hmtx"][cmap[ord(ch)]][0] * 1000 / upm) for ch in set(TEXT)}
    return data, by_char, widths


def _widths(font: bytes) -> list[int]:
    from fontTools.ttLib import TTFont
    ft = TTFont(io.BytesIO(font))
    cmap, upm = ft.getBestCmap(), ft["head"].unitsPerEm
    return [round(ft["hmtx"][cmap[c]][0] * 1000 / upm) if c in cmap else 0 for c in range(32, 127)]


def _font_page(font_of, content: bytes) -> bytes:
    return build([{"box": [0, 0, 400, 120], "content": content, "resources": lambda w: {"Font": {"F1": font_of(w)}}}])


def _descriptor(w, name, **ff):
    return w.add({"Type": N("FontDescriptor"), "FontName": N(name), "Flags": 32, "FontBBox": [-700, -400, 2800, 1100],
                  "ItalicAngle": 0, "Ascent": 1069, "Descent": -293, "CapHeight": 700, "StemV": 80, **ff})


SHOW = b"BT /F1 24 Tf 20 50 Td (" + TEXT.encode() + b") Tj ET"


def truetype_pdf():
    data = _ttf()
    return _font_page(lambda w: w.add({
        "Type": N("Font"), "Subtype": N("TrueType"), "BaseFont": N("NotoTT"), "FirstChar": 32, "LastChar": 126,
        "Widths": _widths(data), "Encoding": N("WinAnsiEncoding"),
        "FontDescriptor": _descriptor(w, "NotoTT", FontFile2=w.stream(data, {"Length1": len(data)}))}), SHOW)


def truetype_cid_pdf():
    doc = pymupdf.open()
    p = doc.new_page(width=400, height=120)
    p.insert_font(fontname="ns", fontbuffer=_noto())
    p.insert_text((20, 70), TEXT, fontname="ns", fontsize=24)
    return doc.tobytes(garbage=3, deflate=True)


def cff_pdf(bare: bool):
    from fontTools.ttLib import TTFont
    otf = _otf()
    cmap = TTFont(io.BytesIO(otf)).getBestCmap()
    data = _bare_cff(otf) if bare else otf
    return _font_page(lambda w: w.add({
        "Type": N("Font"), "Subtype": N("Type1"), "BaseFont": N("NotoTestCFF"), "FirstChar": 32, "LastChar": 126,
        "Widths": _widths(otf), "Encoding": {"Differences": [32] + [N(cmap.get(c, ".notdef")) for c in range(32, 127)]},
        "FontDescriptor": _descriptor(w, "NotoTestCFF", FontFile3=w.stream(data, {"Subtype": N("Type1C" if bare else "OpenType")}))}),
        SHOW)


def cff_cid_pdf():
    data, by_char, widths = _cid_cff(_otf())
    codes = b"".join(struct.pack(">H", by_char[ch]) for ch in TEXT)
    w_arr = [x for cid in sorted(widths) for x in (cid, [widths[cid]])]
    return _font_page(lambda w: w.add({
        "Type": N("Font"), "Subtype": N("Type0"), "BaseFont": N("NotoTestCID"), "Encoding": N("Identity-H"),
        "DescendantFonts": [w.add({"Type": N("Font"), "Subtype": N("CIDFontType0"), "BaseFont": N("NotoTestCID"),
                                   "CIDSystemInfo": {"Registry": "Adobe", "Ordering": "Identity", "Supplement": 0},
                                   "W": w_arr, "DW": 500, "FontDescriptor": _descriptor(
                                       w, "NotoTestCID", FontFile3=w.stream(data, {"Subtype": N("CIDFontType0C")}))})]}),
        b"BT /F1 24 Tf 20 50 Td <" + codes.hex().encode() + b"> Tj ET")


def type1_pdf():
    """A Type 1 font (from bookport's own CFF to Type 1 conversion) embedded as FontFile, binary eexec part."""
    from fontTools.ttLib import TTFont

    from bookport.ps import write_fonts as wf
    otf = _otf()
    cff = wf.CFF(_bare_cff(otf))
    cmap = TTFont(io.BytesIO(otf)).getBestCmap()
    names = [".notdef"] * 256
    charstrings = {}
    for c in range(32, 127):
        g = cmap.get(c)
        if g in cff.names:
            names[c] = g
            charstrings[g] = wf.type1_charstring(*cff.outline(cff.names[g]))
    text = wf.type1_font("/NotoTestT1", cff.matrix, cff.bbox, names, charstrings)
    clear, rest = text.split("currentfile eexec\n", 1)
    clear = (clear + "currentfile eexec\n").encode("latin-1")
    binary = bytes.fromhex(re.sub(r"\s", "", rest.split("\n" + "0" * 64)[0]))
    trailer = (("0" * 64 + "\n") * 8 + "cleartomark\n").encode()
    prog = clear + binary + trailer
    return _font_page(lambda w: w.add({
        "Type": N("Font"), "Subtype": N("Type1"), "BaseFont": N("NotoTestT1"), "FirstChar": 32, "LastChar": 126,
        "Widths": _widths(otf), "FontDescriptor": _descriptor(w, "NotoTestT1", FontFile=w.stream(
            prog, {"Length1": len(clear), "Length2": len(binary), "Length3": len(trailer)}))}), SHOW)


FONT_CASES = {  # name: (PDF, what the PostScript must carry)
    "truetype": (truetype_pdf, b"/FontType 42"), "truetype_cid": (truetype_cid_pdf, b"/FontType 42"),
    "cff": (lambda: cff_pdf(True), b"currentfile eexec"), "opentype_cff": (lambda: cff_pdf(False), b"currentfile eexec"),
    "cff_cid": (cff_cid_pdf, b"currentfile eexec"), "type1": (type1_pdf, b"currentfile eexec"),
}


# --- the tests ---------------------------------------------------------------------------------------------------

def test_dsc_structure_and_7_bit():
    pdf = build([{"content": b"0 0 1 rg 10 10 50 50 re f"}, {"content": b"1 0 0 rg 10 10 50 50 re f", "box": [0, 0, 200, 100]}])
    data = write.convert(images_pdf()) + write.convert(pdf)
    assert all(b < 128 for b in data)
    assert max(len(line) for line in data.split(b"\n")) <= 255
    text = write.convert(pdf).decode("ascii")
    assert text.startswith("%!PS-Adobe-3.0\n") and text.rstrip().endswith("%%EOF")
    assert "%%Pages: 2" in text and "%%BoundingBox: 0 0 400 300" in text and "%%LanguageLevel: 3" in text
    assert re.findall(r"^%%Page: (\d+) (\d+)$", text, re.MULTILINE) == [("1", "1"), ("2", "2")]
    assert text.count("%%PageBoundingBox: 0 0 200 100") == 1 and text.count("/PageSize [200 100]") == 1
    assert text.index("%%EndProlog") < text.index("%%BeginSetup") < text.index("%%Page: 1 1") < text.index("%%Trailer")


def test_eps_is_the_chosen_page_turned_and_cropped():
    pdf = build([{"content": b"0 g 0 0 10 10 re f"},
                 {"content": b"0 g 0 0 10 10 re f", "rotate": 90, "crop": [10, 20, 310, 220]}])
    text = write.convert(pdf, eps=True, page=1).decode("ascii")
    assert text.startswith("%!PS-Adobe-3.0 EPSF-3.0\n")
    assert "%%BoundingBox: 0 0 200 300" in text and "%%HiResBoundingBox: 0 0 200 300" in text  # turned: 200 wide
    assert "setpagedevice" not in text and text.count("%%Page:") == 1 and "%%Pages: 1" in text
    with pytest.raises(BookportError, match="no page 3"):
        write.convert(pdf, eps=True, page=2)


def test_public_api_writes_files_and_notes(tmp_path):
    src = tmp_path / "in.pdf"
    src.write_bytes(text_pdf())
    notes = []
    out = ps.from_pdf(src, tmp_path / "out.eps", eps=True, notes=notes)
    assert out == tmp_path / "out.eps" and out.read_bytes().startswith(b"%!PS-Adobe-3.0 EPSF-3.0")
    assert ps.from_pdf(src.read_bytes()).startswith(b"%!PS-Adobe-3.0\n")
    assert notes == []
    with pytest.raises(BookportError):
        ps.from_pdf(tmp_path / "missing.pdf")


@needs_gs
@pytest.mark.parametrize("case", list(CASES))
def test_draws_like_the_pdf(case, tmp_path):
    make, lowest = CASES[case]
    got = scores(make(), tmp_path)
    assert min(got) >= lowest, got


@needs_gs
def test_eps_draws_like_the_page(tmp_path):
    assert min(scores(rotated_pdf(90), tmp_path, eps=True)) >= 0.99


@needs_gs
@pytest.mark.parametrize("case", list(FONT_CASES))
def test_embedded_fonts_draw_like_the_pdf(case, tmp_path):
    make, carried = FONT_CASES[case]
    pdf = make()
    data = write.convert(pdf)
    assert carried in data
    assert min(scores(pdf, tmp_path)) >= 0.995


@needs_gs
@pytest.mark.parametrize("case", ["truetype", "truetype_cid", "cff", "opentype_cff", "type1"])
def test_text_stays_text(case, tmp_path):
    pdf = FONT_CASES[case][0]()
    assert text_of(gs_pdf(write.convert(pdf), tmp_path)) == text_of(pdf) == TEXT


@needs_gs
def test_every_glyph_lands_where_the_pdf_puts_it(tmp_path):
    """Character origins after PostScript and back (Ghostscript's PDF), against the source: within 0.05 pt.
    (Only filled text: outlined and clipping text is drawn as outlines in PostScript, so it doesn't read back.)"""
    plain = re.sub(rb"BT /F4.*?ET|BT 7 Tr.*?ET", b"", text_pdf_content(), flags=re.DOTALL)
    for pdf in (build([{"resources": text_resources, "content": plain}]), truetype_cid_pdf()):
        want, got = char_origins(pdf), char_origins(gs_pdf(write.convert(pdf), tmp_path))
        assert [c[:2] for c in got] == [c[:2] for c in want]
        worst = max(max(abs(a[2][0] - b[2][0]), abs(a[2][1] - b[2][1])) for a, b in zip(want, got))
        assert worst < 0.05, worst


@needs_gs
def test_the_pdf_widths_win_over_the_font_s(tmp_path):
    """A standard font with every width set to 700: glyphs follow the PDF's widths, not Helvetica's own."""
    pdf = build([{"resources": lambda w: {"Font": {"F1": helvetica(w, FirstChar=32, LastChar=126, Widths=[700] * 95)}},
                  "content": b"BT /F1 20 Tf 20 100 Td (iiii WWWW) Tj ET"}])
    xs = [c[2][0] for c in char_origins(gs_pdf(write.convert(pdf), tmp_path))]
    steps = [round(b - a, 3) for a, b in itertools.pairwise(xs)]
    assert steps[:3] == [14.0, 14.0, 14.0] and steps[-3:] == [14.0, 14.0, 14.0], steps  # 700/1000 x 20 pt


def test_glyphs_are_named_for_their_characters():
    data = write.convert(truetype_cid_pdf())
    for g in ("/H 43", "/e ", "/comma ", "/exclam "):
        assert g.encode() in data, g


def _transparent_pdf():
    doc = pymupdf.open()
    p = doc.new_page(width=400, height=300)
    p.draw_rect(pymupdf.Rect(20, 20, 200, 200), color=None, fill=(1, 0, 0))
    p.draw_rect(pymupdf.Rect(100, 100, 300, 280), color=None, fill=(0, 0, 1), fill_opacity=0.5)
    return doc.tobytes()


def test_transparency_is_drawn_as_a_picture_when_it_can_be(tmp_path):
    pdf = _transparent_pdf()

    def flatten(index, dpi):
        pix = pymupdf.open(stream=pdf, filetype="pdf")[index].get_pixmap(dpi=dpi, alpha=False)
        return pix.width, pix.height, pix.samples
    notes = []
    flat = write.convert(pdf, flatten=flatten, notes=notes)
    assert notes == ["Page 1 uses transparency, so it was drawn as a picture"]
    assert b"/Width 1667 /Height 1250" in flat  # 300 dpi
    notes = []
    opaque = write.convert(pdf, notes=notes)
    assert notes == ["Some pages use transparency, which PostScript doesn't have; they were drawn without it"]
    assert b"/Width 1667" not in opaque
    if GS:
        want = pdf_pictures(pdf)[0]
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        good = ssim(want, ps_pictures(flat, tmp_path / "a")[0])
        plain = ssim(want, ps_pictures(opaque, tmp_path / "b")[0])
        assert good >= 0.99 and good > plain, (good, plain)


def test_an_image_used_on_every_page_is_written_once():
    big = gradient(300, 200)

    def res(w):
        if "img" not in holder:
            holder["img"] = image(w, big)
        return {"XObject": {"I": holder["img"]}}
    holder = {}
    pdf = build([{"content": b"q 300 0 0 200 50 50 cm /I Do Q", "resources": res} for _ in range(5)])
    once = write.convert(build([{"content": b"q 300 0 0 200 50 50 cm /I Do Q", "resources": lambda w: {"XObject": {
        "I": image(w, big)}}}]))
    data = write.convert(pdf)
    assert data.count(b"Im") >= 5 and data.count(b"mksrc /FlateDecode filter") == 5
    assert len(data) < len(once) * 1.5, (len(data), len(once))


def test_encrypted_pdf_without_a_password(tmp_path):
    doc = pymupdf.open(stream=text_pdf(), filetype="pdf")
    enc = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="")
    assert b"/Encrypt" in enc
    assert b"Plain text" in write.convert(enc)


def test_damaged_input_raises_bookport_error():
    for bad in (b"", b"hello", b"%PDF-1.7\n", text_pdf()[:150],
                b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>"):
        with pytest.raises(BookportError):
            write.convert(bad)


def test_a_page_of_endless_drawing_stops_at_the_budget():
    pdf = build([{"content": b"0 0 m\n" * 1_100_000}])
    notes = []
    t = time.perf_counter()
    data = write.convert(pdf, notes=notes)
    assert time.perf_counter() - t < 30
    assert notes == ["A page has more than 1,000,000 drawing steps; the rest of it was left out"]
    assert data.count(b" m\n") <= 1_000_000


def test_forms_that_draw_themselves_stop():
    w = Writer()
    root, tree, me = w.reserve(), w.reserve(), w.reserve()
    w.stream(b"/X Do 0 0 7 7 re f", {"Type": N("XObject"), "Subtype": N("Form"), "BBox": [0, 0, 10, 10],
                                     "Resources": {"XObject": {"X": me}}}, ref=me)
    chain = None
    for _ in range(100):
        chain = w.stream(b"/C Do 0 0 3 3 re f", {"Type": N("XObject"), "Subtype": N("Form"), "BBox": [0, 0, 10, 10],
                                                 "Resources": {"XObject": {"C": chain}} if chain else {}})
    page = w.add({"Type": N("Page"), "Parent": tree, "MediaBox": [0, 0, 100, 100], "Contents": w.stream(b"/X Do /C Do"),
                  "Resources": {"XObject": {"X": me, "C": chain}}})
    w.put(tree, {"Type": N("Pages"), "Kids": [page], "Count": 1})
    w.put(root, {"Type": N("Catalog"), "Pages": tree})
    data = write.convert(w.finish(root))
    assert data.count(b"0 0 7 7 re") == 1 and data.count(b"0 0 3 3 re") == write.MAX_DEPTH


def test_mutated_pdfs_raise_only_bookport_errors():
    rng = random.Random(1)
    seeds = [paths_pdf(), forms_pdf(), images_pdf(), type3_pdf(), text_pdf(), patterns_pdf(), shadings_pdf()]
    outcomes = {"ok": 0, "error": 0}
    for _ in range(300):
        data = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 12)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        try:
            write.convert(bytes(data))
            outcomes["ok"] += 1
        except BookportError:
            outcomes["error"] += 1
    assert outcomes["ok"] > 50 and outcomes["error"] > 50, outcomes
