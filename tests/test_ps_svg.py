"""PostScript and EPS to SVG (bookport.ps.to_svg). Each program is drawn twice, from its SVG and from its PDF
(bookport.ps.to_pdf, checked on its own in test_ps_read), and the two pictures are compared with SSIM.

PyMuPDF draws both. Its SVG reader has no clip paths, gradients or patterns, so pages that use them are drawn with
headless Microsoft Edge (Chromium, the engine of many reading apps) where it is installed (BOOKPORT_EDGE, or its
usual place), and skipped elsewhere. The structure tests run without either."""

from __future__ import annotations

import base64
import io
import lzma
import os
import random
import re
import subprocess
import xml.etree.ElementTree as ET
from importlib import resources
from pathlib import Path

import pytest

from bookport import ps
from bookport.errors import BookportError
from bookport.images import decode_png
from bookport.ps import files

from .test_pdf import needs_pymupdf, pymupdf
from .test_ps_read import CLOSE, END, HSBW, PROGRAMS, RLINETO, RMOVETO, _cs, _ssim, type1_font

EDGE = os.environ.get("BOOKPORT_EDGE") or next(
    (p for p in (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                 os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"))
     if Path(p).is_file()), None)
needs_edge = pytest.mark.skipif(EDGE is None, reason="Microsoft Edge isn't installed (it draws SVG clips and gradients)")
SVG = "{http://www.w3.org/2000/svg}"


def svg(src: str | bytes, **kw) -> bytes:
    return ps.to_svg(src.encode("latin-1") if isinstance(src, str) else src, **kw)


def gray(rgb):
    return rgb @ [0.299, 0.587, 0.114]


def mupdf(data: bytes, kind: str, zoom: float = 2.0):
    """The page as PyMuPDF draws it (RGB, as floats)."""
    import numpy as np
    pix = pymupdf.open(stream=data, filetype=kind)[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(float)


def edge(data: bytes, size: tuple[float, float], folder: Path):
    """The SVG as headless Edge draws it, at 192 dpi (2 device pixels per CSS pixel)."""
    import numpy as np
    from PIL import Image
    f = folder / "page.svg"
    f.write_bytes(data)
    png = folder / "page.png"
    w, h = round(size[0] * 96 / 72), round(size[1] * 96 / 72)
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    f"--user-data-dir={folder / 'profile'}", f"--screenshot={png}", f"--window-size={w},{h}",
                    "--default-background-color=ffffffff", f.as_uri()],
                   capture_output=True, timeout=120, check=False, creationflags=0x08000000 if os.name == "nt" else 0)
    return np.asarray(Image.open(png).convert("RGB")).astype(float)


def same_size(a, b):
    """The two pictures cut to the same size, then to where either has ink (white margins would make any two
    drawings look alike)."""
    import numpy as np
    assert abs(a.shape[0] - b.shape[0]) <= 2 and abs(a.shape[1] - b.shape[1]) <= 2, (a.shape, b.shape)
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a, b = a[:h, :w], b[:h, :w]
    ys, xs = np.where((gray(a) < 250) | (gray(b) < 250))
    if len(ys):
        y0, y1, x0, x1 = max(0, ys.min() - 8), ys.max() + 9, max(0, xs.min() - 8), xs.max() + 9
        a, b = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
    return a, b


def score(a, b) -> float:
    """SSIM of the two pictures (on grey), the worst of their four quarters (so a local mistake isn't averaged
    away)."""
    a, b = gray(a), gray(b)
    h, w = a.shape
    if h < 64 or w < 64:
        return _ssim(a, b)
    return min(_ssim(a[ys, xs], b[ys, xs]) for ys in (slice(0, h // 2), slice(h // 2, h))
               for xs in (slice(0, w // 2), slice(w // 2, w)))


def colour_gap(a, b) -> float:
    """How far apart the colours are: the mean difference of the red, green and blue levels (0 to 255)."""
    return float(abs(a - b).mean())


# ---- fonts made for the tests

def t1_font(name: str, glyphs: dict, subrs: list | None = None, codes: dict | None = None) -> bytes:
    """A Type 1 font from charstrings (``_cs``), with Subrs and an encoding {code: glyph name}."""
    priv = bytearray(b"dup /Private 8 dict dup begin /RD {string currentfile exch readstring pop} executeonly def "
                     b"/ND {noaccess def} executeonly def /NP {noaccess put} executeonly def /lenIV 4 def "
                     b"/BlueValues [] def /MinFeature {16 16} def /password 5839 def\n")
    subrs = subrs or []
    priv += b"/Subrs %d array\n" % len(subrs)
    for i, s in enumerate(subrs):
        priv += b"dup %d %d RD " % (i, len(s)) + s + b" NP\n"
    priv += b"ND\n2 index /CharStrings %d dict dup begin\n" % len(glyphs)
    for n, cs in glyphs.items():
        priv += b"/" + n.encode() + b" %d RD " % len(cs) + cs + b" ND\n"
    priv += b"end end readonly put noaccess put dup /FontName get exch definefont pop mark currentfile closefile\n"
    enc = b" ".join(b"dup %d /%s put" % (c, n.encode()) for c, n in (codes or {}).items())
    head = (b"%!FontType1-1.0: " + name.encode() + b"\n10 dict begin /FontName /" + name.encode() + b" def "
            b"/FontType 1 def /PaintType 0 def /FontMatrix [0.001 0 0 0.001 0 0] readonly def "
            b"/FontBBox {0 -200 1000 900} readonly def /Encoding 256 array 0 1 255 {1 index exch /.notdef put} for "
            + enc + b" readonly def currentdict end currentfile eexec\n")
    return head + files.encrypt(b"\x10\x20\x30\x40" + bytes(priv), 55665) + b"\n" + (b"0" * 64 + b"\n") * 8 + \
        b"cleartomark\n"


CALLSUBR, RETURN, OTHERSUBR, POP, SETCP, HSTEM, SEAC = (10,), (11,), (12, 16), (12, 17), (12, 33), (1,), (12, 6)
FLEX_SUBRS = [_cs(3, 0, OTHERSUBR, POP, POP, SETCP, RETURN),  # Type 1 Font Format, 8.3: flex and hint replacement
              _cs(0, 1, OTHERSUBR, RETURN), _cs(0, 2, OTHERSUBR, RETURN), _cs(RETURN),
              _cs(1, 3, OTHERSUBR, POP, CALLSUBR, RETURN), _cs(0, 50, HSTEM, RETURN)]


def flex_font() -> bytes:
    """F: a box whose top is a flex (two curves dipping 50 units); its hints are replaced on the way."""
    pts = [(-250, -20), (200, 20), (-100, -50), (-100, 0), (-100, 0), (-100, 50), (-50, 0)]
    flex = [1, CALLSUBR]
    for dx, dy in pts:
        flex += [dx, dy, RMOVETO, 2, CALLSUBR]
    glyph = _cs(0, 600, HSBW, 5, 4, CALLSUBR, 50, 0, RMOVETO, 500, 0, RLINETO, 0, 200, RLINETO, *flex,
                50, 50, 200, 0, CALLSUBR, CLOSE, END)
    accent = _cs(30, 300, HSBW, 0, 600, RMOVETO, 100, 0, RLINETO, 0, 100, RLINETO, -100, 0, RLINETO, CLOSE, END)
    base = _cs(100, 700, HSBW, 0, 0, RMOVETO, 500, 0, RLINETO, 0, 500, RLINETO, -500, 0, RLINETO, CLOSE, END)
    composite = _cs(40, 700, HSBW, 10, 250, 50, 65, 194, SEAC)  # A with acute (StandardEncoding 65 and 194)
    return t1_font("TestFlex", {".notdef": _cs(0, 500, HSBW, END), "F": glyph, "A": base, "acute": accent,
                                "Aacute": composite}, FLEX_SUBRS, {70: "F", 65: "A", 88: "Aacute"})


def cff_font() -> bytes:
    """A CFF font set with curves (fontTools writes them with the shortest Type 2 operators) and a ring (O)."""
    fb = pytest.importorskip("fontTools.fontBuilder")
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.ttLib import TTFont
    b = fb.FontBuilder(1000, isTTF=False)
    b.setupGlyphOrder([".notdef", "O", "S"])
    b.setupCharacterMap({79: "O", 83: "S"})
    charstrings = {}
    for name, width in ((".notdef", 500), ("O", 800), ("S", 600)):
        pen = T2CharStringPen(width, None)
        if name == "O":
            for r, s in ((350, 1), (200, -1)):  # the hole runs the other way round
                k = 0.5523 * r
                pen.moveTo((400 + r, 350))
                pen.curveTo((400 + r, 350 + s * k), (400 + k, 350 + s * r), (400, 350 + s * r))
                pen.curveTo((400 - k, 350 + s * r), (400 - r, 350 + s * k), (400 - r, 350))
                pen.curveTo((400 - r, 350 - s * k), (400 - k, 350 - s * r), (400, 350 - s * r))
                pen.curveTo((400 + k, 350 - s * r), (400 + r, 350 - s * k), (400 + r, 350))
                pen.closePath()
        elif name == "S":
            pen.moveTo((500, 600))
            pen.curveTo((400, 720), (80, 700), (100, 520))
            pen.curveTo((120, 360), (480, 380), (500, 200))
            pen.curveTo((520, 20), (150, -20), (60, 120))
            pen.lineTo((120, 160))
            pen.curveTo((200, 60), (440, 80), (420, 210))
            pen.curveTo((400, 330), (60, 320), (40, 500))
            pen.curveTo((20, 760), (440, 780), (560, 640))
            pen.closePath()
        charstrings[name] = pen.getCharString()
    b.setupCFF("TestCurves", {"FullName": "TestCurves"}, charstrings, {})
    b.setupHorizontalMetrics({n: (w, 0) for n, w in ((".notdef", 500), ("O", 800), ("S", 600))})
    b.setupHorizontalHeader(ascent=800, descent=-200)
    b.setupNameTable({"familyName": "TestCurves", "styleName": "Regular"})
    b.setupOS2()
    b.setupPost()
    buf = io.BytesIO()
    b.save(buf)
    data = TTFont(io.BytesIO(buf.getvalue())).reader["CFF "]
    return (b"%!PS\n/FontSetInit /ProcSet findresource begin\n/TestCurves " + str(len(data)).encode()
            + b" StartData " + data + b"\n")


def type42_font(text: str) -> bytes:
    """Noto Serif (bookport's own) as a Type 42 font with the characters of ``text`` at their Latin-1 codes."""
    from bookport.pdf import ttf
    data = lzma.decompress(resources.files("bookport").joinpath("fonts/NotoSerif-Regular.ttf.xz").read_bytes())
    f = ttf.load(data)
    chunks = [data[i:i + 60000] for i in range(0, len(data), 60000)]
    sfnts = b" ".join(b"<" + c.hex().encode() + b">" for c in chunks)
    chars = sorted(set(text))
    return (b"%!PS\n/Noto 12 dict begin /FontType 42 def /FontName /NotoSerif def /FontMatrix [1 0 0 1 0 0] def "
            b"/FontBBox [0 0 1 1] def /PaintType 0 def /Encoding 256 array 0 1 255 {1 index exch /.notdef put} for "
            + b" ".join(b"dup %d /u%04X put" % (ord(c), ord(c)) for c in chars)
            + b" def /CharStrings 64 dict dup begin /.notdef 0 def "
            + b" ".join(b"/u%04X %d def" % (ord(c), f.cmap[ord(c)]) for c in chars)
            + b" end def /sfnts [" + sfnts + b"] def currentdict end definefont pop\n")


def _hex_image(w: int, h: int) -> str:
    """A smooth RGB picture (a colour ramp with a dark disc), as hex rows."""
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            inside = (x - w / 2) ** 2 + (y - h / 2) ** 2 < (w / 4) ** 2
            row += bytes((40, 40, 60) if inside else (255 * x // w, 255 * y // h, 160))
        rows.append(row.hex())
    return "\n".join(rows)


# ---- the programs, drawn by PyMuPDF from both outputs

# a picture and an image mask. At 32 points (drawn at 144 dpi) a picture's pixel is a device pixel: MuPDF's SVG
# reader smooths enlarged pictures, which its PDF side doesn't (Edge, below, draws them enlarged, and sharp)
PICTURES = (f"%!PS\n50 400 translate SIZE SIZE scale 64 64 8 [64 0 0 -64 0 64] {{<{_hex_image(64, 64)}>}} "
            "false 3 colorimage 0 0.5 0 setrgbcolor 1.2 0 translate 64 64 true [64 0 0 -64 0 64] {<"
            + ("FF00FF00FF00FF00" * 8 + "00FF00FF00FF00FF" * 8) * 4 + ">} imagemask showpage")

PROGS = {
    "paths": "%!PS\n0.2 0.4 0.8 setrgbcolor newpath 100 100 moveto 300 500 lineto 500 100 lineto closepath "
             "150 250 moveto 300 450 lineto 450 250 lineto closepath eofill "
             "0 1 1 0 setcmykcolor 50 600 200 100 rectfill 0.5 setgray 300 600 moveto 350 750 450 550 550 700 curveto "
             "550 600 lineto fill showpage",
    "strokes": "%!PS\n1 0 0 setrgbcolor 20 setlinewidth 1 setlinecap 1 setlinejoin 100 100 moveto 300 400 lineto "
               "500 100 lineto stroke gsave 300 600 translate 3 1 scale 0 0 1 setrgbcolor 4 setlinewidth [6 3] 0 setdash "
               "newpath 0 0 40 0 360 arc stroke grestore 0 1 0 setrgbcolor 0 setlinewidth 50 700 moveto 550 700 lineto "
               "stroke 2 setlinejoin 15 setlinewidth 0 setgray 50 200 moveto 80 300 lineto 110 200 lineto stroke "
               "2 setlinecap 10 setlinewidth 400 300 moveto 550 300 lineto stroke showpage",
    "type1": (type1_font() + b"/TestBoxes findfont 60 scalefont setfont 100 600 moveto (ABBA) show "
              b"gsave 300 300 translate 30 rotate 1 0 0 setrgbcolor 0 0 moveto (AB) show grestore showpage"),
    "type1_flex_seac": flex_font() + b"/TestFlex findfont 150 scalefont setfont 100 500 moveto (FXA) show showpage",
    "type3": ("%!PS\n/T3 8 dict begin /FontType 3 def /FontMatrix [0.001 0 0 0.001 0 0] def "
              "/FontBBox [0 0 1000 1000] def /Encoding 256 array 0 1 255 {1 index exch /.notdef put} for "
              "dup 88 /tri put dup 89 /sq put def /BuildChar { exch begin dup 88 eq { 1000 0 setcharwidth 0 0 1 "
              "setrgbcolor 0 0 moveto 900 0 lineto 450 900 lineto closepath fill } { 1000 0 0 0 800 800 "
              "setcachedevice 100 100 600 600 rectfill } ifelse pop end } def currentdict end definefont pop\n"
              "/T3 findfont 80 scalefont setfont 100 400 moveto 1 0 0 setrgbcolor (XYXY) show showpage"),
    "pictures": PICTURES.replace("SIZE", "32"),
}


def _fonts_cases():
    out = dict(PROGS)
    out["cff"] = None
    out["type42"] = None
    return sorted(out)


def _program(name: str) -> bytes:
    if name == "cff":
        return cff_font() + b"/TestCurves findfont 150 scalefont setfont 100 400 moveto (OSSO) show showpage"
    if name == "type42":
        text = "Hamburgefonstiv \xe9t\xe9"
        return (type42_font(text) + b"/Noto findfont 44 scalefont setfont 40 500 moveto ("
                + text.encode("latin-1") + b") show 0 0 1 setrgbcolor 40 300 moveto 2 0 (Hamburg) ashow showpage")
    src = PROGS[name]
    return src.encode("latin-1") if isinstance(src, str) else src


@needs_pymupdf
@pytest.mark.parametrize("name", _fonts_cases())
def test_svg_draws_like_the_pdf(name):
    src = _program(name)
    a, b = same_size(mupdf(ps.to_pdf(src), "pdf"), mupdf(svg(src), "svg"))
    got, gap = score(a, b), colour_gap(a, b)
    assert got > 0.97 and gap < 4, (name, got, gap)
    ink_a, ink_b = (gray(a) < 128).sum(), (gray(b) < 128).sum()
    assert abs(ink_a - ink_b) <= 0.05 * ink_a + 200, (name, ink_a, ink_b)  # as much ink


# ---- clips, gradients and patterns: drawn by Edge

EDGE_PROGS = {
    "clips": "%!PS\n100 100 400 400 rectclip newpath 300 300 150 0 360 arc eoclip 0.2 0.6 0.9 setrgbcolor "
             "0 0 612 792 rectfill gsave 200 200 100 100 rectclip 1 0 0 setrgbcolor 0 0 612 792 rectfill grestore "
             "0 setgray 0 0 moveto 612 792 lineto 5 setlinewidth stroke initclip\n"
             + type1_font().decode("latin-1") + "/TestBoxes findfont 150 scalefont setfont 60 560 moveto (ABAB) "
             "true charpath clip 0 10 600 { 0 moveto 600 600 rlineto 4 setlinewidth stroke } for showpage",
    "pictures": PICTURES.replace("SIZE", "256").replace("1.2 0 translate", "0 -1.2 translate"),
    "shadings": "%!PS\ngsave 0 400 306 392 rectclip << /ShadingType 2 /ColorSpace /DeviceRGB /Coords [50 0 250 0] "
                "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >> shfill grestore "
                "gsave 306 400 306 392 rectclip << /ShadingType 3 /ColorSpace /DeviceRGB /Coords [459 596 20 459 596 "
                "150] /Extend [true true] /Function << /FunctionType 2 /Domain [0 1] /C0 [1 1 0] /C1 [0 0.6 0.8] /N 1 "
                ">> >> shfill grestore gsave 0 0 306 400 rectclip << /ShadingType 2 /ColorSpace /DeviceGray /Coords "
                "[20 0 290 0] /Extend [true true] /Function << /FunctionType 3 /Domain [0 1] /Bounds [0.5] /Encode "
                "[0 1 1 0] /Functions [ << /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [1] /N 1 >> << /FunctionType 2 "
                "/Domain [0 1] /C0 [0.2] /C1 [1] /N 2 >> ] >> >> shfill grestore "
                "gsave 306 0 306 400 rectclip << /ShadingType 2 /ColorSpace [/Separation /Spot /DeviceRGB "
                "{dup 1 exch sub 0 exch}] /Coords [320 0 600 0] /Function << /FunctionType 2 /Domain [0 1] /C0 [0] "
                "/C1 [1] /N 1 >> >> shfill grestore showpage",
    "meshes": "%!PS\n<< /ShadingType 4 /ColorSpace /DeviceRGB /DataSource [0 50 450 1 0 0 0 290 450 0 1 0 0 170 750 "
              "0 0 1 1 290 750 1 1 0] >> shfill "
              "<< /ShadingType 6 /ColorSpace /DeviceRGB /DataSource [0 320 450 370 450 470 450 570 450 570 550 570 "
              "650 570 750 470 750 370 750 320 750 320 650 320 550 1 0 0 0 1 0 0 0 1 1 1 0] >> shfill "
              "gsave 100 100 translate << /ShadingType 1 /ColorSpace /DeviceRGB /Domain [0 1 0 1] /Matrix [400 0 0 "
              "250 0 0] /Function << /FunctionType 0 /Domain [0 1 0 1] /Range [0 1 0 1 0 1] /Size [2 2] "
              "/BitsPerSample 8 /DataSource <FF0000 00FF00 0000FF FFFF00> >> >> shfill grestore showpage",
    "patterns": PROGRAMS["pattern"].replace("100 100 400 400 rectfill", "50 450 250 250 rectfill")
    .replace("showpage", "")
    + "<< /PatternType 1 /PaintType 1 /TilingType 1 /BBox [10 10 40 40] /XStep 30 /YStep 30 /PaintProc { pop 1 0 0 "
      "setrgbcolor 20 20 moveto 30 20 lineto 25 38 lineto closepath fill 10 10 6 6 rectfill } >> [2 0 0 2 13 7] "
      "makepattern setpattern 330 450 250 250 rectfill "
      "<< /PatternType 1 /PaintType 2 /TilingType 1 /BBox [0 0 20 20] /XStep 25 /YStep 20 /PaintProc { pop 0 0 10 10 "
      "rectfill } >> [2 0 0 2 0 0] makepattern /P exch def [/Pattern /DeviceRGB] setcolorspace 1 0 0 P setcolor "
      "50 100 250 250 rectfill 0 0.5 0 P setcolor 330 100 250 250 rectfill showpage",
}


@needs_edge
@needs_pymupdf
@pytest.mark.parametrize("name", sorted(EDGE_PROGS))
def test_clips_gradients_and_patterns_draw_like_the_pdf_in_a_browser(name, tmp_path):
    src = EDGE_PROGS[name].encode("latin-1")
    out = svg(src)
    a, b = same_size(mupdf(ps.to_pdf(src), "pdf", 2.0 * 96 / 72), edge(out, (612, 792), tmp_path))
    got = score(a, b)
    assert got > 0.97, (name, got)


@needs_pymupdf
def test_readers_without_gradients_get_their_average_colour_not_black():
    src = PROGRAMS["shading"].encode()
    out = svg(src)
    assert b'style="fill:url(#' in out
    d = pymupdf.open(stream=out, filetype="svg")
    pix = d[0].get_pixmap(dpi=36, alpha=False)
    assert sum(pix.pixel(153, 198)) > 200  # the middle of the radial shading: a colour, not black (MuPDF's SVG)


# ---- what the SVG is made of

def _tree(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def test_an_eps_is_its_bounding_box_and_well_formed():
    out = svg("%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 100 200 400 350\n%%Title: (A figure)\n%%EndComments\n"
              "1 0 0 setrgbcolor 100 200 300 150 rectfill\n")
    root = _tree(out)
    assert root.get("viewBox") == "0 0 300 150" and root.get("width") == "300pt" and root.get("height") == "150pt"
    assert root.find(f"{SVG}title").text == "A figure"
    path = root.find(f".//{SVG}path")
    assert path.get("fill") == "#ff0000" and path.get("d") == "M0 0L300 0L300 150L0 150Z"


def test_text_is_glyph_outlines_and_standard_fonts_are_text():
    out = svg(type1_font() + b"/TestBoxes findfont 100 scalefont setfont 100 500 moveto (ABA) show showpage")
    root = _tree(out)
    assert root.find(f".//{SVG}text") is None
    glyphs = root.findall(f".//{SVG}defs/{SVG}path")
    assert len(glyphs) == 2  # A and B, once each
    uses = root.findall(f".//{SVG}use")
    assert len(uses) == 3
    assert uses[0].get("transform") == "matrix(0.1 0 0 0.1 100 500)"
    assert glyphs[0].get("d") == "M100 0L600 0L600 500L100 500Z"
    notes = []
    root = _tree(svg(PROGRAMS["text"], notes=notes))
    texts = root.findall(f".//{SVG}text")
    assert [t.text for t in texts] == ["The quick brown fox", "jumps over", "rotated"]
    assert "Helvetica" in texts[0].get("font-family") and texts[1].get("font-style") == "italic"
    assert texts[2].get("font-weight") == "bold" and "Courier" in texts[2].get("font-family")
    assert any("Times-Italic" in n and "standard font" in n for n in notes)


@needs_pymupdf
def test_standard_font_letters_sit_where_the_pdf_puts_them():
    """Drawn in a font the reader has, so not compared by picture: each letter's origin against the PDF's."""
    src = PROGRAMS["text"].encode()
    want = []
    for block in pymupdf.open(stream=ps.to_pdf(src), filetype="pdf")[0].get_text("rawdict")["blocks"]:
        for line in block["lines"]:
            for span in line["spans"]:
                want += [(c["c"], c["origin"]) for c in span["chars"]]
    got = []
    for t in _tree(svg(src)).iter(f"{SVG}text"):
        a, b, _c, _d, e, f = (float(v) for v in t.get("transform")[7:-1].split())
        for ch, x in zip(t.text, (float(v) for v in t.get("x").split())):
            got.append((ch, (a * x + e, 792 - (b * x + f))))  # (PyMuPDF's y runs down the page)
    assert [c for c, _ in got] == [c for c, _ in want]
    assert max(max(abs(p[0] - q[0]), abs(p[1] - q[1])) for (_, p), (_, q) in zip(got, want)) < 0.01


def test_clips_shadings_patterns_and_pictures_are_svg_elements():
    root = _tree(svg(EDGE_PROGS["clips"]))
    assert len(root.findall(f".//{SVG}clipPath")) >= 3
    assert root.find(f".//{SVG}clipPath/{SVG}path[@clip-rule='evenodd']") is not None
    assert len(root.findall(f".//{SVG}clipPath/{SVG}use")) == 4  # charpath then clip: the glyphs' outlines
    root = _tree(svg(PROGRAMS["charpath"]))
    assert root.find(f".//{SVG}clipPath/{SVG}text").text == "Clip"  # with a standard font, its text
    root = _tree(svg(EDGE_PROGS["shadings"]))
    assert len(root.findall(f".//{SVG}linearGradient")) == 3 and len(root.findall(f".//{SVG}radialGradient")) == 1
    notes = []
    root = _tree(svg(EDGE_PROGS["meshes"], notes=notes))
    assert len(root.findall(f".//{SVG}image")) == 3 and any("drawn as pictures" in n for n in notes)
    root = _tree(svg(EDGE_PROGS["patterns"]))
    pats = root.findall(f".//{SVG}pattern")
    assert len(pats) == 4  # the uncoloured one twice: in red and in green
    assert pats[1].find(f"{SVG}g").get("transform") == "translate(-10 -10)"  # the cell's box starts at 10 10
    out = svg(_program("pictures"))
    hrefs = re.findall(rb'xlink:href="data:image/png;base64,([^"]+)"', out)
    assert len(hrefs) == 2
    pic, mask = (decode_png(base64.b64decode(h)) for h in hrefs)
    assert (pic.width, pic.height, pic.mode) == (64, 64, "RGB") and pic.pixels[:3] == bytes((0, 0, 160))
    assert mask.mode == "RGBA" and mask.pixels[:4] == bytes((0, 128, 0, 255)) and mask.pixels[4 * 8:4 * 9] == \
        bytes((0, 128, 0, 0))  # painted where the mask's bits are 1 (imagemask with true)


def test_jpeg_pictures_go_in_as_they_are():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 200, 0)).save(buf, "JPEG", quality=95)
    jpeg = buf.getvalue()
    a85 = base64.a85encode(jpeg, adobe=False).decode()
    out = svg(f"%!PS\n100 100 translate 100 100 scale << /ImageType 1 /Width 16 /Height 16 /BitsPerComponent 8 "
              f"/Decode [0 1 0 1 0 1] /ImageMatrix [16 0 0 -16 0 16] /DataSource currentfile /ASCII85Decode filter "
              f"/DCTDecode filter >> /DeviceRGB setcolorspace image\n{a85}~>\nshowpage")
    assert b"data:image/jpeg;base64," + base64.b64encode(jpeg) in out


def test_pages_files_and_errors(tmp_path):
    two = "%!PS\n1 0 0 setrgbcolor 0 0 10 10 rectfill showpage 0 0 1 setrgbcolor 0 0 20 20 rectfill showpage"
    notes = []
    assert b'fill="#ff0000"' in svg(two, notes=notes) and any("page 1 of the 2" in n for n in notes)
    assert b'fill="#0000ff"' in svg(two, page=1)
    with pytest.raises(BookportError, match="no page 3"):
        svg(two, page=2)
    with pytest.raises(BookportError, match="isn't a PostScript file"):
        svg(b"\x00\x01 not at all")
    src = tmp_path / "fig.eps"
    src.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n0 0 10 10 rectfill\n")
    got = ps.to_svg(src, tmp_path / "fig.svg")
    assert got == tmp_path / "fig.svg" and got.read_bytes().startswith(b"<?xml")
    assert not list(tmp_path.glob(".bookport-*"))


def test_damaged_programs_fail_cleanly():
    """Mutants of programs that use every kind of mark: an SVG or a BookportError, nothing else."""
    rng = random.Random(7)
    seeds = [_program(n) for n in ("paths", "type1_flex_seac", "type3", "pictures")] + \
        [EDGE_PROGS[n].encode("latin-1") for n in ("shadings", "meshes", "patterns", "clips")]
    made = 0
    for _ in range(120):
        data = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 6)):
            i = rng.randrange(len(data))
            data[i] = rng.randrange(256) if rng.random() < 0.7 else data[rng.randrange(len(data))]
        try:
            out = ps.to_svg(bytes(data))
        except BookportError:
            continue
        ET.fromstring(out)  # well formed whatever went in
        made += 1
    assert made > 20


@needs_pymupdf
def test_accents_built_with_seac_sit_where_the_pdf_puts_them():
    src = flex_font() + b"/TestFlex findfont 100 scalefont setfont 100 500 moveto (X) show showpage"
    import numpy as np
    spots = []
    for kind, data in (("pdf", ps.to_pdf(src)), ("svg", svg(src))):
        a = gray(mupdf(data, kind, 1.0))
        ys, xs = np.where(a < 128)
        top = ys < ys.min() + 6
        spots.append((xs.min(), xs.max(), xs[top].min(), xs[top].max()))
    assert spots[0] == spots[1]  # the base and its accent, the same pixels
