"""PostScript and EPS to PDF (bookport.ps.to_pdf): the interpreter's operators, then what pages come out, checked by
drawing the PDF with PyMuPDF (an independent reader) and, where Ghostscript is installed, against Ghostscript's own
PDF of the same program (run as a black box, like Calibre in the other tests)."""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from bookport import ps
from bookport.errors import BookportError
from bookport.limits import LimitError
from bookport.ps import files, interp, topdf
from bookport.ps.files import PSFile, Stream

from .test_pdf import needs_pymupdf, pymupdf

GS = os.environ.get("BOOKPORT_GS") or next(
    (p for p in (shutil.which("gswin64c"), shutil.which("gs"),
                 os.path.expandvars(r"%LOCALAPPDATA%\ConvertAll\engines\ghostscript\bin\gswin64c.exe"))
     if p and Path(p).is_file()), None)
needs_gs = pytest.mark.skipif(GS is None, reason="Ghostscript isn't installed (it's only compared against)")


def says(src: str | bytes) -> str:
    """What a program prints (=, ==, print)."""
    I = topdf.make(False, None, None)
    I.es.append(interp.FileRun(PSFile(Stream(src.encode("latin-1") if isinstance(src, str) else src), True)))
    I.run(0)
    return I.out.decode("latin-1")


def lines(src: str) -> list[str]:
    return says(src).split()


def pdf(src: str | bytes, **kw) -> bytes:
    return ps.to_pdf(src.encode("latin-1") if isinstance(src, str) else src, **kw)


def doc(src, **kw):
    return pymupdf.open(stream=pdf(src, **kw), filetype="pdf")


def color(d, x: float, y: float, page: int = 0, dpi: int = 72) -> tuple:
    """The colour at (x, y) in PostScript's coordinates (from the bottom left)."""
    p = d[page]
    pix = p.get_pixmap(dpi=dpi, alpha=False)
    k = dpi / 72
    return pix.pixel(int(x * k), int((p.rect.height - y) * k))


def red(c) -> bool:
    return c[0] > 200 and c[1] < 60 and c[2] < 60


def white(c) -> bool:
    return min(c) > 235


# ---- the language

def test_arithmetic_and_types():
    assert lines("1 2 add = 7 2 idiv = -7 2 idiv = -7 3 mod = 7 2 div = 2 3 mul = 2.5 3 mul = 10 sqrt 10 sqrt mul ="
                 " 4 neg = 3.7 truncate = 3.5 round = -3.5 round = 2147483647 1 add type =") == \
        ["3", "3", "-3", "-1", "3.5", "6", "7.5", "10.0", "-4", "3.0", "4.0", "-3.0", "realtype"]
    assert lines("0 1 atan = 1 0 atan = 30 sin 100 mul round = 2 10 exp = 100 log = 16#ff = 8#17 = 2#1010 =") == \
        ["0.0", "90.0", "50.0", "1024.0", "2.0", "255", "15", "10"]
    assert lines("5 3 bitshift = 5 -1 bitshift = 12 10 and = 12 10 or = 12 10 xor = true not = 5 not =") == \
        ["40", "2", "8", "14", "6", "false", "-6"]


def test_stacks_and_control():
    assert lines("1 2 3 3 1 roll count = = = = 1 2 3 2 copy count = clear 5 1 1 10 {add} for =") == \
        ["3", "2", "1", "3", "5", "60"]
    assert lines("0 5 {1 add} repeat = 0 {1 add dup 7 eq {exit} if} loop = [1 2 3] {} forall add add =") == \
        ["5", "7", "6"]
    assert lines("{1 0 div} stopped = $error /errorname get = {(x) cvn load} stopped =") == \
        ["true", "undefinedresult", "true"]
    # errors inside operators are PostScript errors a program can catch, by their PLRM names
    assert lines("{add} stopped = $error /errorname get = clear {1 (a) add} stopped clear $error /errorname get = "
                 "clear {-1 array} stopped clear $error /errorname get =") == \
        ["true", "stackunderflow", "typecheck", "rangecheck"]
    assert lines("mark 1 2 counttomark = cleartomark count = {stop} stopped { (caught) = } if") == \
        ["2", "0", "caught"]
    assert lines("/f { dup 1 le { pop 1 } { dup 1 sub f mul } ifelse } def 10 f =") == ["3628800"]


def test_strings_arrays_dictionaries():
    assert lines("(abc) 1 get = (abc) 1 1 getinterval =") == ["98", "b"]
    out = says("(hello world) (o w) search pop print ( ) print print ( ) print print")
    assert out == "hell o w orld"  # search leaves post, match, pre (and true) on the stack
    assert lines("/s (abcdef) def s 2 (XY) putinterval s = [1 2 3] dup 1 9 put == << /a 1 /b 2 >> dup /a get =") == \
        ["abXYef", "[1", "9", "3]", "1"]
    assert lines("5 dict begin /k 3 def currentdict /k known = /k load = end /k where =") == ["true", "3", "false"]
    assert lines("123 10 string cvs = 3.25 10 string cvs = (42) cvi 1 add = (2.5) cvr 2 mul = /nm 10 string cvs =") \
        == ["123", "3.25", "43", "5.0", "nm"]
    assert lines("(  12 foo) token pop exch pop = 255 16 10 string cvrs = [1 [2 (s)] /n {x 1}] ==") == \
        ["12", "FF", "[1", "[2", "(s)]", "/n", "{x", "1}]"]


def test_bind_and_procedures_built_while_reading():
    out = says("/add {sub} def /p {1 2 add} def /add systemdict /add get def p = "
               "/q {3 4 add} bind def /add {mul} def q =")
    assert out.split() == ["3", "7"]  # p looked add up when run; q was bound to the operator
    assert lines("/x 5 def {//x} 0 get =") == ["5"]


def test_files_and_filters():
    data = zlib.compress(b"inflated words")
    a85 = _a85(data)
    # data follows the operator that reads it; the program goes on right after the data's end mark
    out = says(f"currentfile /ASCII85Decode filter /FlateDecode filter 100 string readstring\n{a85}~>\n"
               "pop = (after the data) =")
    assert out.split("\n")[:2] == ["inflated words", "after the data"]
    assert says("currentfile 5 string readhexstring\n48 65 6c 6c 6f pop =") == "Hello\n"
    assert says("(abcXYZdef) 0 (XYZ) /SubFileDecode filter 20 string readstring pop =") == "abc\n"
    assert says("currentfile 0 (END) /SubFileDecode filter 50 string readstring\nsome textEND pop = (ok) =") == \
        "some text\nok\n"
    assert says("currentfile /RunLengthDecode filter 8 string readstring\n" + _hexish(_rle(b"aaaaabcd")) +
                " pop = (ok) =") == "aaaaabcd\nok\n"


def _hexish(raw: bytes) -> str:
    return raw.decode("latin-1")


def _a85(data: bytes) -> str:
    import base64
    return base64.a85encode(data).decode()


def _rle(data: bytes) -> bytes:
    out = bytearray()
    out += bytes([256 - 4]) + data[:1]  # 5 a's as a run
    rest = data[5:]
    out += bytes([len(rest) - 1]) + rest
    return bytes(out) + b"\x80"


def test_eexec_runs_encrypted_code_and_carries_on_after_it():
    plain = b"/secret (from eexec) def mark currentfile closefile "
    enc = files.encrypt(b"\x01\x02\x03\x04" + plain, 55665)
    assert says(b"currentfile eexec " + enc + b"\n0000000000\ncleartomark secret = (back) =") == "from eexec\nback\n"
    hexed = enc.hex().encode()
    assert says(b"currentfile eexec\n" + hexed + b"\n0000000000\ncleartomark secret =") == "from eexec\n"


def test_bytes_encrypted_after_closefile_are_skipped():
    """Read ahead: what's encrypted after the closefile (a font writer's line ends) never runs as program text."""
    plain = b"/secret (kept) def mark currentfile closefile\n\n\n\n"
    enc = files.encrypt(b"\x01\x02\x03\x04" + plain, 55665)
    out = says(b"currentfile eexec\n" + enc.hex().encode() + b"\n" + b"0" * 64 + b"\ncleartomark secret =")
    assert out == "kept\n"


def test_what_programs_test_for_answers_like_a_printer():
    # gcheck is true for simple objects (Ghostscript's ps2write prolog copies anything that says false)
    assert lines("5 gcheck = /n gcheck = (s) gcheck = [1] gcheck =") == ["true", "true", "false", "false"]
    # a font that isn't in the file isn't "there", though findfont still gives one (a standard font)
    assert lines("/Palatino-Roman /Font resourcestatus = /Helvetica /Font resourcestatus {pop pop true} if =") == \
        ["false", "true"]
    assert lines("/Palatino-Roman findfont /FontName get =") == ["Palatino-Roman"]


def test_save_restore_brings_the_graphics_state_back():
    assert lines("1 setlinewidth save 5 setlinewidth currentlinewidth = restore currentlinewidth =") == \
        ["5.0", "1.0"]
    assert lines("gsave 0.5 setgray grestore currentgray = 1 0 0 setrgbcolor currentgray =") == ["0.0", "0.3"]


def test_matrices():
    assert lines("72 72 scale 1 1 transform = = 100 50 translate 0 0 transform = = matrix currentmatrix ==") == \
        ["72.0", "72.0", "3600.0", "7200.0", "[72.0", "0.0", "0.0", "72.0", "7200.0", "3600.0]"]
    assert lines("10 20 moveto 30 40 rlineto currentpoint = = newpath 0 0 moveto 100 0 rlineto 0 50 rlineto pathbbox"
                 " = = = =") == ["60.0", "40.0", "50.0", "100.0", "0.0", "0.0"]


def test_errors_without_a_catch_stop_the_conversion():
    with pytest.raises(BookportError, match="undefined"):
        pdf("%!PS\nnosuchthing\n")
    with pytest.raises(BookportError, match="stackunderflow"):
        pdf("%!PS\nadd\n")


def test_errors_after_pages_keep_the_pages():
    notes = []
    out = pdf("%!PS\n0 0 100 100 rectfill showpage oops\n", notes=notes)
    assert out.startswith(b"%PDF") and any("stopped early" in n for n in notes)


def test_a_program_that_never_ends_is_stopped(monkeypatch):
    monkeypatch.setattr(interp, "MAX_STEPS", 200_000)
    with pytest.raises(LimitError):
        pdf("%!PS\n{1 pop} loop\n")
    with pytest.raises(LimitError):
        pdf("%!PS\n/f {f} def f\n")
    with pytest.raises(LimitError):  # an empty loop body still counts
        pdf("%!PS\n1000000000 {} repeat\n")


def test_what_a_program_allocates_has_a_budget():
    with pytest.raises(LimitError):
        pdf("%!PS\n0 1 200 { pop 60000000 string pop } for\n")


@needs_pymupdf
def test_pages_after_a_restore_are_drawn():
    """save, draw, showpage, restore: the graphics state brought back still paints on the next page."""
    d = doc("%!PS\n/s save def 1 0 0 setrgbcolor 0 0 100 100 rectfill showpage s restore\n"
            "/s save def 0 0 1 setrgbcolor 0 0 100 100 rectfill showpage s restore\n")
    assert d.page_count == 2
    c = color(d, 50, 50, 1)
    assert red(color(d, 50, 50, 0)) and c[2] > 200 and c[0] < 60  # blue on page 2, not left blank


def test_hostile_programs_fail_cleanly():
    with pytest.raises(BookportError):
        pdf("%!PS\n2000000000 string\n")
    with pytest.raises(BookportError):
        pdf("%!PS\n" + "{" * 20000 + "\n")
    with pytest.raises(BookportError, match="invalidfileaccess"):
        pdf("%!PS\n(C:/Windows/win.ini) (r) file\n")
    with pytest.raises(BookportError, match="invalidfileaccess"):
        pdf("%!PS\n(x.ps) run\n")
    with pytest.raises(BookportError):
        pdf(b"\x89PNG not postscript at all")


# ---- pages

@needs_pymupdf
def test_paths_fill_stroke_and_colours():
    d = doc("%!PS\n1 0 0 setrgbcolor 100 100 200 150 rectfill\n0 0 1 setrgbcolor 10 setlinewidth "
            "newpath 400 400 moveto 500 400 lineto stroke\n0 1 0 0 setcmykcolor 100 500 50 50 rectfill\n"
            "0 1 1 sethsbcolor 300 600 50 50 rectfill showpage")
    assert d.page_count == 1 and d[0].rect.width == 612 and d[0].rect.height == 792
    assert red(color(d, 150, 150)) and white(color(d, 90, 90))
    assert color(d, 450, 400)[2] > 200 and color(d, 450, 403)[0] < 60  # the line is 10 wide
    assert white(color(d, 450, 410))
    c = color(d, 125, 525)
    assert c[1] < 100 and c[0] > 200  # magenta
    assert red(color(d, 325, 625))  # hue 0


@needs_pymupdf
def test_stroke_width_follows_the_matrix_it_was_stroked_under():
    d = doc("%!PS\n10 1 scale 1 setlinewidth newpath 10 300 moveto 10 500 lineto stroke showpage")
    # 1 unit wide in x scaled by 10: the line covers 95..105 in device space
    assert color(d, 98, 400)[0] < 60 and color(d, 103, 400)[0] < 60 and white(color(d, 110, 400))


@needs_pymupdf
def test_dashes_caps_and_arcs():
    d = doc("%!PS\n4 setlinewidth [20 20] 0 setdash newpath 100 100 moveto 300 100 lineto stroke\n"
            "[] 0 setdash 1 0 0 setrgbcolor newpath 300 400 100 0 180 arc closepath fill\n"
            "newpath 100 600 moveto 200 600 200 700 30 arcto 4 {pop} repeat 200 700 lineto stroke showpage")
    assert color(d, 110, 100)[0] < 60 and white(color(d, 130, 100))  # dash, gap
    assert red(color(d, 300, 450)) and white(color(d, 300, 350))  # the upper half disc


@needs_pymupdf
def test_clipping_and_initclip():
    d = doc("%!PS\ngsave 100 100 200 200 rectclip 1 0 0 setrgbcolor 0 0 612 792 rectfill grestore\n"
            "newpath 350 350 moveto 550 350 lineto 550 550 lineto 350 550 lineto closepath "
            "400 400 moveto 500 400 lineto 500 500 lineto 400 500 lineto closepath eoclip\n"
            "0 0 1 setrgbcolor 0 0 612 792 rectfill initclip 0 1 0 setrgbcolor 20 700 30 30 rectfill showpage")
    assert red(color(d, 200, 200)) and white(color(d, 50, 50)) and white(color(d, 320, 200))
    assert color(d, 370, 370)[2] > 200 and white(color(d, 450, 450))  # the hole of the even-odd clip
    c = color(d, 35, 715)
    assert c[1] > 100 and c[0] < 60 and c[2] < 60  # green: after initclip it's painted (not left white)


@needs_pymupdf
def test_pages_sizes_and_eps_boxes():
    d = doc("%!PS\n<< /PageSize [300 200] >> setpagedevice 0 0 10 10 rectfill showpage\n"
            "<< /PageSize [200 300] >> setpagedevice 0 0 10 10 rectfill showpage\n")
    assert d.page_count == 2
    assert (d[0].rect.width, d[0].rect.height) == (300, 200) and (d[1].rect.width, d[1].rect.height) == (200, 300)
    eps = ("%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 100 200 300 260\n%%HiResBoundingBox: 100 200 300 260.5\n"
           "%%EndComments\n1 0 0 setrgbcolor 100 200 100 60 rectfill\n")
    d = doc(eps)
    assert d.page_count == 1 and (d[0].rect.width, d[0].rect.height) == (200, 60.5)
    assert red(color(d, 50, 30)) and white(color(d, 150, 30))
    atend = "%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: (atend)\n0 0 50 50 rectfill\n%%Trailer\n%%BoundingBox: 0 0 50 40\n"
    assert doc(atend)[0].rect.height == 40


@needs_pymupdf
def test_dos_eps_wrapper_and_a_printer_job_prefix():
    body = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 80 40\n1 0 0 setrgbcolor 0 0 80 40 rectfill\n"
    head = b"\xc5\xd0\xd3\xc6" + struct.pack("<IIIIIIH", 30, len(body), 0, 0, 0, 0, 0xFFFF)
    notes = []
    d = pymupdf.open(stream=pdf(head + body + b"TIFF preview bytes", notes=notes), filetype="pdf")
    assert notes == []  # the preview after the PostScript isn't run as program text
    assert (d[0].rect.width, d[0].rect.height) == (80, 40) and red(color(d, 40, 20))
    d = pymupdf.open(stream=pdf(b"\x1b%-12345X@PJL ENTER LANGUAGE=POSTSCRIPT\r\n" + body), filetype="pdf")
    assert red(color(d, 40, 20))


@needs_pymupdf
def test_standard_fonts_text_and_its_width():
    src = ("%!PS\n/Helvetica findfont 20 scalefont setfont 100 700 moveto (Hello world) show\n"
           "/Times-Roman findfont 30 scalefont setfont (Centered) stringwidth pop 2 div 306 exch sub 600 moveto "
           "(Centered) show\n/Courier findfont 12 scalefont setfont 72 500 moveto (abc) show currentpoint pop = "
           "showpage")
    assert says(src.replace("showpage", "")).strip() == "93.6"  # 72 + 3 Courier glyphs of 600/1000 x 12
    d = doc(src)
    text = d[0].get_text()
    assert "Hello world" in text and "Centered" in text and "abc" in text
    words = {w[4]: w for w in d[0].get_text("words")}
    left, right = words["Centered"][0], words["Centered"][2]
    assert abs((left + right) / 2 - 306) < 3  # stringwidth used Times' real widths
    fonts = {f[3] for f in d[0].get_fonts()}
    assert {"Helvetica", "Times-Roman", "Courier"} <= fonts


@needs_pymupdf
def test_reencoded_fonts_and_spacing_operators():
    src = ("%!PS\n/Times-Roman findfont dup length dict begin {1 index /FID ne {def} {pop pop} ifelse} forall "
           "/Encoding ISOLatin1Encoding def currentdict end /Times-Latin1 exch definefont pop\n"
           "/Times-Latin1 findfont 24 scalefont setfont 72 700 moveto (caf\\351 na\\357ve) show\n"
           "72 650 moveto 10 0 (spaced) ashow 72 600 moveto 20 0 32 (a b c) widthshow\n"
           "72 550 moveto (xyz) [30 30 30] xshow showpage")
    d = doc(src)
    t = d[0].get_text()
    assert "café naïve" in t
    chars = [c for b in d[0].get_text("rawdict")["blocks"] for ln in b["lines"] for s in ln["spans"]
             for c in s["chars"]]
    xs = {c["c"]: c["origin"][0] for c in chars if abs(c["origin"][1] - (792 - 550)) < 1}
    assert abs(xs["y"] - xs["x"] - 30) < 0.01 and abs(xs["z"] - xs["y"] - 30) < 0.01


def _cs(*items) -> bytes:
    """A Type 1 charstring (numbers and command codes), encrypted with 4 lead bytes."""
    out = bytearray()
    for it in items:
        if isinstance(it, tuple):
            out += bytes(it)
        elif -107 <= it <= 107:
            out.append(it + 139)
        elif 108 <= it <= 1131:
            v = it - 108
            out += bytes([v // 256 + 247, v % 256])
        elif -1131 <= it <= -108:
            v = -it - 108
            out += bytes([v // 256 + 251, v % 256])
        else:
            out += b"\xff" + struct.pack(">i", it)
    return files.encrypt(b"\0\0\0\0" + bytes(out), 4330)


HSBW, RLINETO, RMOVETO, CLOSE, END = (13,), (5,), (21,), (9,), (14,)


def type1_font(name: str = "TestBoxes", hexed: bool = False) -> bytes:
    """A Type 1 font of two glyphs: A, a 500 unit square, 700 wide; B, a triangle, 600 wide."""
    glyphs = {".notdef": _cs(0, 500, HSBW, END),
              "A": _cs(0, 700, HSBW, 100, 0, RMOVETO, 500, 0, RLINETO, 0, 500, RLINETO, -500, 0, RLINETO, CLOSE,
                       END),
              "B": _cs(0, 600, HSBW, 50, 0, RMOVETO, 500, 0, RLINETO, -250, 500, RLINETO, CLOSE, END)}
    priv = bytearray(b"dup /Private 8 dict dup begin /RD {string currentfile exch readstring pop} executeonly def "
                     b"/ND {noaccess def} executeonly def /NP {noaccess put} executeonly def /lenIV 4 def "
                     b"/BlueValues [] def /MinFeature {16 16} def /password 5839 def /Subrs 0 array ND\n"
                     b"2 index /CharStrings 3 dict dup begin\n")
    for n, cs in glyphs.items():
        priv += b"/" + n.encode() + b" %d RD " % len(cs) + cs + b" ND\n"
    priv += b"end end readonly put noaccess put dup /FontName get exch definefont pop mark currentfile closefile\n"
    enc = files.encrypt(b"\x10\x20\x30\x40" + bytes(priv), 55665)
    if hexed:
        enc = b"\n".join(enc.hex().encode()[i:i + 64] for i in range(0, len(enc) * 2, 64))
    head = (b"%!FontType1-1.0: " + name.encode() + b"\n10 dict begin /FontName /" + name.encode() + b" def "
            b"/FontType 1 def /PaintType 0 def /FontMatrix [0.001 0 0 0.001 0 0] readonly def "
            b"/FontBBox {0 0 700 600} readonly def /Encoding 256 array 0 1 255 {1 index exch /.notdef put} for "
            b"dup 65 /A put dup 66 /B put readonly def currentdict end currentfile eexec\n")
    return head + enc + b"\n" + (b"0" * 64 + b"\n") * 8 + b"cleartomark\n"


@needs_pymupdf
@pytest.mark.parametrize("hexed", [False, True])
def test_type1_fonts_in_the_program_are_embedded(hexed):
    src = type1_font(hexed=hexed) + (b"/TestBoxes findfont 100 scalefont setfont 100 500 moveto (AB) show "
                                     b"(ABA) stringwidth pop = showpage")
    assert says(src).strip() == "200.0"  # 70 + 60 + 70
    d = pymupdf.open(stream=pdf(src), filetype="pdf")
    assert color(d, 135, 525)[0] < 60  # inside the A square (100 + 10 .. 60)
    assert white(color(d, 105, 525))  # left of it
    assert color(d, 200, 505)[0] < 60  # B starts at 170: its triangle's base
    fonts = d[0].get_fonts()
    assert fonts and fonts[0][1] == "pfa" or fonts[0][2] == "Type1"
    ff = [x for x in range(1, d.xref_length()) if "/FontFile " in d.xref_object(x)]
    assert ff, "the font program is embedded"


def _cff_font() -> bytes:
    """A CFF font set (one font, TestCFF: A a square 700 wide, B a triangle 600 wide), built with fontTools."""
    fb = pytest.importorskip("fontTools.fontBuilder")
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.ttLib import TTFont
    b = fb.FontBuilder(1000, isTTF=False)
    b.setupGlyphOrder([".notdef", "A", "B"])
    b.setupCharacterMap({65: "A", 66: "B"})
    shapes = {".notdef": (500, []), "A": (700, [(100, 0), (600, 0), (600, 500), (100, 500)]),
              "B": (600, [(50, 0), (550, 0), (300, 500)])}
    charstrings = {}
    for name, (width, pts) in shapes.items():
        pen = T2CharStringPen(width, None)
        if pts:
            pen.moveTo(pts[0])
            for p in pts[1:]:
                pen.lineTo(p)
            pen.closePath()
        charstrings[name] = pen.getCharString()
    b.setupCFF("TestCFF", {"FullName": "TestCFF"}, charstrings, {})
    b.setupHorizontalMetrics({n: (w, 0) for n, (w, _) in shapes.items()})
    b.setupHorizontalHeader(ascent=800, descent=-200)
    b.setupNameTable({"familyName": "TestCFF", "styleName": "Regular"})
    b.setupOS2()
    b.setupPost()
    buf = io.BytesIO()
    b.save(buf)
    return TTFont(io.BytesIO(buf.getvalue())).reader["CFF "]


@needs_pymupdf
def test_cff_font_sets_through_startdata():
    data = _cff_font()
    src = (b"%!PS\n/FontSetInit /ProcSet findresource begin\n/TestCFF " + str(len(data)).encode() + b" StartData " +
           data +
           b"\n/TestCFF findfont 100 scalefont setfont 100 500 moveto (AB) show (ABA) stringwidth pop = showpage")
    assert says(src).strip() == "200.0"  # widths from the Type 2 charstrings: 70 + 60 + 70
    d = pymupdf.open(stream=pdf(src), filetype="pdf")
    assert color(d, 135, 525)[0] < 60 and white(color(d, 105, 525)) and color(d, 200, 505)[0] < 60
    assert any("/FontFile3" in d.xref_object(x) for x in range(1, d.xref_length()))
    assert d[0].get_text().strip() == "AB"
    assert lines(src.replace(b"showpage", b"") + b" currentdict /StartData known =")[-1] == "false"  # its begin ended


@pytest.mark.skipif(not Path("C:/Windows/Fonts/MiriamLibre-Regular.otf").is_file(), reason="needs Windows' Miriam Libre")
def test_cff_widths_follow_subroutines():
    """Real CFF fonts put the width in shared subroutines: every glyph's advance as fontTools reads it."""
    ttlib = pytest.importorskip("fontTools.ttLib")
    from bookport.ps import cff
    f = ttlib.TTFont("C:/Windows/Fonts/MiriamLibre-Regular.otf")
    font = cff.read(f.reader["CFF "])[0]
    order = f.getGlyphOrder()
    assert font.names == order
    assert [font.width(i) for i in range(len(order))] == [float(f["hmtx"][n][0]) for n in order]


@needs_pymupdf
def test_type42_fonts_are_embedded_as_truetype():
    import lzma
    from importlib import resources

    from bookport.pdf import ttf
    data = lzma.decompress(resources.files("bookport").joinpath("fonts/NotoSerif-Regular.ttf.xz").read_bytes())
    f = ttf.load(data)
    gids = {n: f.cmap[ord(c)] for n, c in (("H", "H"), ("i", "i"), ("exclam", "!"))}
    chunks = [data[i:i + 60000] for i in range(0, len(data), 60000)]
    chunks = [c + (b"\0" if len(c) % 2 else b"") for c in chunks]  # (strings are even: a padding byte is ignored)
    sfnts = b" ".join(b"<" + c.hex().encode() + b">" for c in chunks)
    src = (b"%!PS\n/Noto 12 dict begin /FontType 42 def /FontName /NotoSerif def /FontMatrix [1 0 0 1 0 0] def "
           b"/FontBBox [0 0 1 1] def /PaintType 0 def /Encoding 256 array 0 1 255 {1 index exch /.notdef put} for "
           b"dup 72 /H put dup 105 /i put dup 33 /exclam put def /CharStrings 4 dict dup begin /.notdef 0 def " +
           b" ".join(b"/%s %d def" % (n.encode(), g) for n, g in gids.items()) +
           b" end def /sfnts [" + sfnts + b"] def currentdict end definefont pop\n"
           b"/Noto findfont 60 scalefont setfont 100 400 moveto (Hi!) show (H) stringwidth pop = showpage")
    width = f.advance(gids["H"]) / f.units * 60
    assert abs(float(says(src)) - width) < 0.01
    d = pymupdf.open(stream=pdf(src), filetype="pdf")
    assert d[0].get_text().strip() == "Hi!"
    ff = [x for x in range(1, d.xref_length()) if d.xref_get_key(x, "FontFile2")[0] == "xref"]
    sub = d.xref_stream(int(d.xref_get_key(ff[0], "FontFile2")[1].split()[0]))
    assert sub[:4] == b"\0\1\0\0" and b"glyf" in sub[:200] and len(sub) > 300  # a TrueType subset, not an empty one
    x0, y0, x1, y1 = d[0].get_text("words")[0][:4]
    assert abs(x0 - 100) < 8 and abs((y1 + y0) / 2 - (792 - 420)) < 30  # placed where it was shown
    pix = d[0].get_pixmap(dpi=72, clip=(x0, y0, x1, y1))
    assert min(pix.samples) < 60  # and drawn (the glyphs are in the embedded font)


@needs_pymupdf
def test_type3_fonts_with_bitmap_glyphs():
    src = ("%!PS\n/T3 8 dict begin /FontType 3 def /FontMatrix [1 0 0 1 0 0] def /FontBBox [0 0 1 1] def "
           "/Encoding 256 array 0 1 255 {1 index exch /.notdef put} for dup 88 /box put def "
           "/BuildChar { pop pop 1 0 0 0 1 1 setcachedevice 8 8 true [8 0 0 8 0 0] {<FF818181818181FF>} imagemask } "
           "def currentdict end definefont pop\n/T3 findfont 80 scalefont setfont 100 400 moveto (XX) show "
           "currentpoint pop = showpage")
    assert says(src.replace("showpage", "")).strip() == "260.0"
    d = doc(src)
    assert color(d, 102, 402)[0] < 80  # the frame of the first box
    assert white(color(d, 140, 440))  # its inside
    assert color(d, 182, 402)[0] < 80  # the second box


@needs_pymupdf
def test_charpath_clips_to_glyphs():
    d = doc("%!PS\n/Helvetica-Bold findfont 200 scalefont setfont 100 300 moveto (IL) true charpath clip "
            "1 0 0 setrgbcolor 0 0 612 792 rectfill showpage")
    assert red(color(d, 125, 400)) and white(color(d, 300, 650)) and white(color(d, 50, 50))


@needs_pymupdf
def test_pictures_with_filters():
    rgb = bytes([255, 0, 0] * 16 + [0, 0, 255] * 16)  # 4x8: top half red, bottom blue
    a85 = _a85(zlib.compress(rgb))
    d = doc(f"%!PS\n100 100 translate 200 200 scale 4 8 8 [4 0 0 -8 0 8] currentfile /ASCII85Decode filter "
            f"/FlateDecode filter false 3 colorimage\n{a85}~>\nshowpage")
    assert red(color(d, 200, 250)) and color(d, 200, 150)[2] > 200
    gray = "00 40 80 c0 ff ff ff ff"
    d = doc(f"%!PS\n100 100 translate 400 100 scale 8 1 8 [8 0 0 1 0 0] {{<{gray}>}} image showpage")
    assert color(d, 110, 150)[0] < 20 and color(d, 480, 150)[0] > 240


@needs_pymupdf
def test_jpeg_pictures_go_into_the_pdf_as_they_are():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 200, 0)).save(buf, "JPEG", quality=95)
    jpeg = buf.getvalue()
    src = (f"%!PS\n100 100 translate 100 100 scale << /ImageType 1 /Width 16 /Height 16 /BitsPerComponent 8 "
           f"/Decode [0 1 0 1 0 1] /ImageMatrix [16 0 0 -16 0 16] /DataSource currentfile /ASCII85Decode filter "
           f"/DCTDecode filter >> /DeviceRGB setcolorspace image\n{_a85(jpeg)}~>\n(still reading) = showpage")
    out = pdf(src)
    assert jpeg in out  # the very same JPEG bytes
    d = pymupdf.open(stream=out, filetype="pdf")
    c = color(d, 150, 150)
    assert c[1] > 150 and c[0] < 80


@needs_pymupdf
def test_image_masks_indexed_and_separation_colours():
    d = doc("%!PS\n1 0 0 setrgbcolor 100 100 translate 160 160 scale 8 8 true [8 0 0 8 0 0] "
            "{<FF00FF00FF00FF00>} imagemask showpage")
    assert red(color(d, 150, 110)) and white(color(d, 150, 130))  # rows alternate
    d = doc("%!PS\n[/Indexed /DeviceRGB 1 <FF0000 0000FF>] setcolorspace 1 setcolor 100 100 50 50 rectfill "
            "[/Separation /Spot /DeviceRGB {dup 0 exch}] setcolorspace 1 setcolor 300 100 50 50 rectfill showpage")
    assert color(d, 125, 125)[2] > 200 and color(d, 125, 125)[0] < 50
    c = color(d, 325, 125)
    assert c[0] > 200 and c[1] < 50 and c[2] > 200  # the tint transform gave (1 0 1)


@needs_pymupdf
def test_shadings_and_patterns():
    d = doc("%!PS\ngsave 0 0 400 100 rectclip << /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 400 0] "
            "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> /Extend [true true] >> "
            "shfill grestore\n"
            "<< /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 20 20] /XStep 20 /YStep 20 "
            "/PaintProc { pop 1 0 0 setrgbcolor 0 0 10 10 rectfill } >> matrix makepattern setpattern "
            "0 200 200 200 rectfill showpage")
    assert red(color(d, 5, 50)) and color(d, 395, 50)[2] > 200
    assert red(color(d, 5, 205)) and white(color(d, 15, 215))


@needs_pymupdf
def test_forms_and_pdfmark_title():
    d = doc("%!PS\n[ /Title (Report) /Author (Pure Tech) /DOCINFO pdfmark\n"
            "/F << /FormType 1 /BBox [0 0 50 50] /Matrix [1 0 0 1 0 0] /PaintProc { pop 1 0 0 setrgbcolor "
            "0 0 100 100 rectfill } >> def 100 100 translate F execform showpage")
    assert d.metadata["title"] == "Report" and d.metadata["author"] == "Pure Tech"
    assert red(color(d, 125, 125)) and white(color(d, 175, 175))  # clipped to the form's box


@needs_pymupdf
def test_title_from_the_dsc_comments():
    d = doc("%!PS-Adobe-3.0\n%%Title: (A chart)\n0 0 10 10 rectfill showpage")
    assert d.metadata["title"] == "A chart"


@needs_pymupdf
def test_nothing_drawn_is_one_blank_page():
    assert doc("%!PS\n% nothing\n").page_count == 1


# ---- against Ghostscript (black box): the same programs, drawn the same way

def _ssim(a, b) -> float:
    import numpy as np
    k = 7
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a, b = a[:h, :w].astype(float), b[:h, :w].astype(float)

    def box(x):
        c = np.cumsum(np.cumsum(np.pad(x, ((1, 0), (1, 0))), 0), 1)
        return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    ma, mb = box(a), box(b)
    va, vb, cov = box(a * a) - ma * ma, box(b * b) - mb * mb, box(a * b) - ma * mb
    c1, c2 = 6.5025, 58.5225
    return float((((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))).mean())


def _gray(pdf_bytes: bytes):
    import numpy as np
    d = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for p in d:
        pix = p.get_pixmap(dpi=100, colorspace=pymupdf.csGRAY, alpha=False)
        out.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width))
    return out


PROGRAMS = {
    "shapes": "%!PS\n1 0 0 setrgbcolor 72 72 200 100 rectfill 0 0 1 setrgbcolor 6 setlinewidth [12 6] 0 setdash "
              "newpath 300 300 80 0 300 arc closepath stroke showpage",
    "text": "%!PS\n/Helvetica findfont 18 scalefont setfont 72 700 moveto (The quick brown fox) show "
            "/Times-Italic findfont 30 scalefont setfont 72 600 moveto 2 0 (jumps over) ashow "
            "gsave 300 300 translate 45 rotate /Courier-Bold findfont 24 scalefont setfont 0 0 moveto (rotated) show "
            "grestore showpage",
    "clips": "%!PS\nnewpath 306 396 150 0 360 arc clip 0 0 1 setrgbcolor 0 0 612 792 rectfill "
             "1 1 0 setrgbcolor 0 20 612 { 0 moveto 0 792 rlineto 8 setlinewidth stroke } for showpage",
    "shading": "%!PS\n<< /ShadingType 3 /ColorSpace /DeviceRGB /Coords [306 396 0 306 396 200] "
               "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 1 0] /C1 [0 0.5 0] /N 1 >> >> shfill showpage",
    "pattern": "%!PS\n<< /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 30 30] /XStep 30 /YStep 30 "
               "/PaintProc { pop 0 0 1 setrgbcolor 15 15 10 0 360 arc fill } >> matrix makepattern setpattern "
               "100 100 400 400 rectfill showpage",
    "charpath": "%!PS\n/Times-Bold findfont 150 scalefont setfont 60 400 moveto (Clip) true charpath clip "
                "0 10 600 { 0 moveto 600 600 rlineto 4 setlinewidth stroke } for showpage",
}


@needs_gs
@needs_pymupdf
@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_pages_look_like_ghostscripts(name, tmp_path):
    src = PROGRAMS[name].encode()
    (tmp_path / "in.ps").write_bytes(src)
    subprocess.run([GS, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-sOutputFile=gs.pdf", "in.ps"],
                   cwd=tmp_path, capture_output=True, timeout=120, check=True,
                   creationflags=0x08000000 if os.name == "nt" else 0)
    theirs, ours = _gray((tmp_path / "gs.pdf").read_bytes()), _gray(pdf(src))
    assert len(theirs) == len(ours)
    for a, b in zip(theirs, ours):
        assert a.shape == b.shape
        assert _ssim(a, b) > 0.97, name


# ---- round trips: PDF to PostScript (bookport's writer) and back (its reader)

@needs_pymupdf
def test_a_book_to_postscript_and_back_keeps_every_word_in_place(tmp_path):
    from collections import Counter

    from .conftest import sample_book
    src = sample_book().write(tmp_path / "book.pdf").read_bytes()
    back = ps.to_pdf(ps.from_pdf(src))
    a, b = pymupdf.open(stream=src, filetype="pdf"), pymupdf.open(stream=back, filetype="pdf")
    assert a.page_count == b.page_count
    wa = Counter(w[4] for p in a for w in p.get_text("words"))
    wb = Counter(w[4] for p in b for w in p.get_text("words"))
    assert wa == wb and sum(wa.values()) > 100
    for pa, pb in zip(a, b):  # each word where it was
        pos_a = sorted((w[4], round(w[0], 1), round(w[1], 1)) for w in pa.get_text("words"))
        pos_b = sorted((w[4], round(w[0], 1), round(w[1], 1)) for w in pb.get_text("words"))
        assert all(x[0] == y[0] and abs(x[1] - y[1]) < 0.5 and abs(x[2] - y[2]) < 0.5 for x, y in zip(pos_a, pos_b))
    ssims = [_ssim(x, y) for x, y in zip(_gray(src), _gray(back))]
    assert min(ssims) > 0.99, ssims
