"""PostScript fixes from the 0.4 audit: save and restore undoing local VM, Type 3 glyph pictures, programs that used
to never end or to fill memory, operators that took the wrong operands, and PostScript files handed to
bookport.read and bookport.convert. Anything that used to hang runs in a child process with a time limit, so a
regression fails the test instead of stopping the suite."""

from __future__ import annotations

import re
import struct
import subprocess
import sys
import textwrap
import time
import zlib
from pathlib import Path

import pytest

import bookport
from bookport import limits, ps
from bookport.errors import BookportError
from bookport.ps import interp, ops_graphics, topdf, write, write_fonts, write_paint
from bookport.ps.files import PSFile, Stream

from .test_pdf import needs_pymupdf, pymupdf
from .test_ps_read import color, doc, pdf, says, white

ROOT = Path(__file__).resolve().parents[1]


def run(src: str) -> interp.Interp:
    """The interpreter after running src (for looking at its state)."""
    I = topdf.make(False, None, None)
    I.es.append(interp.FileRun(PSFile(Stream(src.encode("latin-1")), True)))
    I.run(0)
    return I


def streams(data: bytes) -> list[bytes]:
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        try:
            out.append(zlib.decompress(m.group(1)))
        except zlib.error:
            out.append(m.group(1))
    return out


def child(code: str, seconds: float = 30) -> str:
    """Run code in a fresh Python with bookport; its output. A time-out fails the test (it would have hung)."""
    try:
        r = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True,
                           timeout=seconds, cwd=str(ROOT), check=False)
    except subprocess.TimeoutExpired:
        pytest.fail(f"still running after {seconds} s")
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def ends(program: bytes, seconds: float = 30, steps: int = 200_000_000) -> str:
    """What converting program does ("ok" or the error), in a child process with a time limit (and a step budget
    of its own, so a regression of an endless program stays small)."""
    return child(f"""
        from bookport import ps
        from bookport.ps import interp
        interp.MAX_STEPS = {steps}
        try:
            ps.to_pdf({program!r})
            print("ok")
        except Exception as e:
            print(type(e).__name__, e)
        """, seconds)


# ---- save and restore (PLRM 3.7.3)

def test_restore_undoes_definitions_made_since_the_save():
    assert says("/a 1 def /s save def /a 2 def userdict /b 3 put s restore a = userdict /b known =").split() == \
        ["1", "false"]


def test_restore_of_an_outer_save_closes_the_inner_ones():
    out = says("/a 1 def save /b 2 def save /a 3 def exch restore a = userdict /b known = {restore} stopped = ").split()
    assert out == ["1", "false", "true"]  # back to before both (/a changed in the inner one only), inner one closed


def test_global_vm_and_strings_keep_their_changes():
    out = says("/g true setglobal 1 array false setglobal def /l 1 array def /st (abc) def "
               "save g 0 9 put l 0 9 put st 0 65 put globaldict /k 7 put restore "
               "g 0 get = l 0 get == st = globaldict /k get =").split()
    assert out == ["9", "null", "Abc", "7"]


def test_a_window_on_an_older_array_is_restored_too():
    assert says("/la [1 2 3] def save la 1 1 getinterval 0 9 put restore la 1 get =").strip() == "2"


def test_gcheck_knows_global_vm():
    assert says("true setglobal 1 dict false setglobal gcheck = 1 dict gcheck = 5 gcheck =").split() == \
        ["true", "false", "true"]


def test_what_restore_keeps_is_bounded_by_what_changed():
    """A loop of a million definitions inside a page's save keeps one old value per key, and none at all for a
    dictionary made inside the save."""
    I = run("save /i 0 def 0 1 100000 {/i exch def} for 10 dict begin 0 1 100000 {/j exch def} for")
    assert len(I.saves) == 1 and len(I.saves[0].journal) == 1


def test_a_save_left_open_by_an_earlier_program_changes_nothing_after():
    """Each interpreter starts in local VM outside any save, whatever ran before it in the same thread (a
    program that ended inside two saves made the next conversion's userdict look newer than its saves)."""
    run("save save")
    assert says("/a 1 def /s save def /a 2 def s restore a =").strip() == "1"


@needs_pymupdf
def test_an_eps_figure_redefining_showpage_keeps_the_pages_after_it():
    """The EPS-inclusion wrapper (BeginEPSF/EndEPSF, dvips specials): the figure's own showpage is defined away
    inside a save, and restore brings the real one back. It used to stay, so the pages after it ran together."""
    d = doc("%!PS\n/s save def userdict begin /showpage {} def end s restore\n"
            "0 0 moveto 100 100 lineto stroke showpage\n0 0 moveto 200 100 lineto stroke showpage\n")
    assert len(d) == 2


# ---- Type 3 fonts

@needs_pymupdf
def test_type3_glyphs_each_draw_their_own_picture():
    """Two bitmap glyphs (as in dvips PK fonts): their pictures used to share one resource name, so both drew
    the last one."""
    d = doc(b"""%!PS
/F 10 dict def F begin /FontType 3 def /FontMatrix [1 0 0 1 0 0] def /FontBBox [0 0 1 1] def
/Encoding 256 array def 0 1 255 {Encoding exch /.notdef put} for Encoding 65 /A put Encoding 66 /B put
/BuildChar { 1 0 0 0 1 1 setcachedevice exch pop 65 eq { 1 1 true [1 0 0 1 0 0] <80> imagemask }
  { 1 1 true [1 0 0 1 0 0] <00> imagemask } ifelse } def end
/T F definefont pop /T findfont 100 scalefont setfont 100 100 moveto (AB) show showpage
""")
    assert not white(color(d, 150, 150)) and white(color(d, 250, 150))


# ---- programs that never ended, or filled memory

def test_a_name_standing_for_itself_is_stopped():
    assert ends(b"%!PS\n/a /a cvx def a\n").startswith("JobError")


def test_cvrs_checks_its_radix():
    assert ends(b"%!PS\n16 1 10 string cvrs\n").startswith("JobError")
    assert says("{16 37 10 string cvrs} stopped = $error /errorname get = 255 16 10 string cvrs =").split() == \
        ["true", "rangecheck", "FF"]


def test_arcs_with_huge_angles_end():
    assert ends(b"%!PS\n0 0 moveto 0 0 10 0 1e30 arcn 0 0 10 1e30 0 arc stroke showpage\n") == "ok"
    from bookport.ps.gstate import arc_segments
    assert len(arc_segments(0, 0, 10, 0, 1e30, False)) <= 9  # one turn and a bit: at most 8 quarter curves
    assert len(arc_segments(0, 0, 10, 0, 90, False)) == 2


def test_the_operand_stack_has_a_limit_for_every_operator():
    """dup, copy and for's counter used to push past the limit of a million (only literals were checked): 30
    doublings would be 8 GB; 22 here, 4 million entries."""
    assert "stackoverflow" in ends(b"%!PS\n1 22 {count copy} repeat\n")
    assert "stackoverflow" in ends(b"%!PS\n1 {dup} loop\n", steps=5_000_000)
    assert "stackoverflow" in ends(b"%!PS\n0 1 3000000 {} for\n")


def test_user_objects_have_a_limit():
    t = time.perf_counter()
    out = says("{2000000 0 defineuserobject} stopped = {-1 0 defineuserobject} stopped = "
               "3 (x) defineuserobject 3 execuserobject = {-1 execuserobject} stopped =").split()
    assert out == ["true", "true", "x", "true"] and time.perf_counter() - t < 5


def test_paths_of_curves_have_a_limit(monkeypatch):
    monkeypatch.setattr(ops_graphics, "MAX_PATH", 1000)
    assert says("{0 0 moveto 2000 {1 1 2 2 3 3 curveto} repeat} stopped = $error /errorname get =").split() == \
        ["true", "limitcheck"]


def test_a_short_string_repeated_as_a_huge_picture_is_charged(monkeypatch):
    """(x) as the data of a 3000 x 3000 colour picture: 27 MB from 3 bytes, now counted against the budget."""
    monkeypatch.setattr(limits, "MAX_BOOK", 10 * 1024 * 1024)
    with pytest.raises(BookportError):
        pdf("%!PS\n3000 3000 8 [3000 0 0 3000 0 0] (x) false 3 colorimage showpage\n")


def test_type4_functions_keep_to_their_stack_and_32_bit_shifts():
    class Doc:
        @staticmethod
        def resolve(v):
            return v
    # 1, doubled 7 times (128 numbers), then 0.75 on top: past the limit of 100 the function stops where it is,
    # so its result is the 1 (31 doublings used to take gigabytes)
    f = write_paint.Calculator(Doc(), {"Domain": [0, 1], "Range": [0, 1]},
                               b"{ pop 1 " + b" ".join(b"%d copy" % (1 << k) for k in range(7)) + b" 0.75 }")
    assert f([0.5]) == [1.0]
    g = write_paint.Calculator(Doc(), {"Domain": [0, 1], "Range": [0, 1]}, b"{ pop 1 1 copy 2 copy 0.75 }")
    assert g([0.5]) == [0.75]  # (under the limit, it runs to the end)
    import tracemalloc
    tracemalloc.start()
    try:
        assert write_paint._shift(1, 2_000_000_000) == 0  # (it made a 250 MB number on the way)
        assert says("1 2000000000 bitshift =").split() == ["0"]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 50 * 2**20
    assert write_paint._shift(1, 31) == -(1 << 31) and write_paint._shift(-1, -28) == 15
    assert says("1 31 bitshift = 256 -4 bitshift =").split() == ["-2147483648", "16"]


def test_a_cmap_claiming_billions_of_codes_is_cut_short():
    """Overlapping ranges, each claiming 70,000 codes: 1,000 of them took 70 million dictionary writes (and a
    hostile font's 100,000, 7 billion); past the budget the rest is left out."""
    groups = 1000
    sub = struct.pack(">HHIII", 12, 0, 16 + 12 * groups, 0, groups) + struct.pack(">III", 0, 0x10FFFF, 1) * groups
    data = struct.pack(">HHHHI", 0, 1, 3, 10, 12) + sub
    t = time.perf_counter()
    m = write_fonts.cmaps(data)[(3, 10)]
    assert time.perf_counter() - t < 2 and m[65] == 66


def test_a_truetype_glyph_made_of_itself_is_left_out():
    out = child("""
        import struct, time
        from bookport.ps import outlines
        comps = b"".join(struct.pack(">HHbb", 0x20 | 0x02, 1, 0, 0) for _ in range(7)) + struct.pack(">HHbb", 2, 1, 0, 0)
        glyph1 = struct.pack(">hhhhh", -1, 0, 0, 10, 10) + comps   # 8 components, each glyph 1 itself
        glyf = b"\\0" * 10 + glyph1
        loca = struct.pack(">III", 0, 10, len(glyf))
        class Font:
            num_glyphs = 2
            long_loca = True
            @staticmethod
            def table(tag):
                return {"glyf": glyf, "loca": loca}[tag]
        t = time.perf_counter()
        outlines.truetype(Font, 1)
        print(round(time.perf_counter() - t, 2))
        """)
    assert float(out) < 5


def test_a_shading_of_huge_triangles_is_drawn_in_bounded_time():
    out = child("""
        import time
        from bookport.ps import tosvg
        tris = [((0, 0, (0.0,)), (400, 0, (1.0,)), (0, 400, (0.5,)))] * 40_000
        t = time.perf_counter()
        r = tosvg._rasterize(tris, (0, 0, 400, 400), 400, 400, lambda v: (v[0], v[0], v[0]))
        print(round(time.perf_counter() - t, 1), r.pixels[3])
        """)
    seconds, alpha = out.split()
    assert float(seconds) < 30 and alpha == "255"  # still drawn (the first triangles), just not 40,000 times


# ---- operators that took the wrong things

def test_a_translated_font_matrix_moves_the_text():
    data = pdf("%!PS\n/Helvetica findfont [10 0 0 10 0 50] makefont setfont 100 100 moveto (A) show showpage\n")
    assert any(b"10 0 0 10 100 150 Tm" in s for s in streams(data))


def test_an_uncoloured_pattern_takes_its_colour_from_the_stack():
    src = ("<< /PatternType 1 /PaintType 2 /TilingType 1 /BBox [0 0 10 10] /XStep 10 /YStep 10 "
           "/PaintProc { pop 0 0 5 5 rectfill } >> matrix makepattern /P exch def "
           "/DeviceRGB setcolorspace 1 0 0 P setpattern 0 0 100 100 rectfill ")
    assert says(src + "count =").strip() == "0"
    assert any(b"1 0 0 /P1 scn" in s for s in streams(pdf("%!PS\n" + src + "showpage\n")))


def test_currentfile_looks_past_strings_being_run():
    assert says("(currentfile 3 string readstring pop =) cvx exec ABC\n").strip() == "ABC"


def test_twelve_bit_rows_start_on_a_byte():
    assert ops_graphics._unpack12(bytes.fromhex("FFF0ABC0"), 2, 1, 2) == bytes.fromhex("FFAB")
    assert ops_graphics._unpack12(bytes.fromhex("123456789A"), 5, 3, 1) == bytes.fromhex("124578")


@needs_pymupdf
def test_the_colourant_none_paints_nothing():
    d = doc("%!PS\n0 0 1 setrgbcolor [/Separation /None /DeviceGray {pop 0}] setcolorspace 1 setcolor "
            "0 0 100 100 rectfill 0 0 moveto 100 100 lineto 10 setlinewidth stroke "
            "/Helvetica findfont 50 scalefont setfont 10 10 moveto (XX) show showpage\n")
    assert white(color(d, 50, 50)) and white(color(d, 20, 25)) and d[0].get_text().strip() == ""


def test_rand_starts_from_the_same_seed_each_conversion():
    src = b"%!PS\n0 1 20 {pop rand 600 mod rand 600 mod moveto 5 5 rlineto} for stroke showpage\n"
    assert ps.to_pdf(src) == ps.to_pdf(src)


def test_ufill_takes_no_matrix_and_user_paths_skip_glyphs():
    assert says("[1 2] {0 0 moveto 10 0 lineto 10 10 lineto closepath} ufill count =").strip() == "1"
    out = says("/Helvetica findfont 10 scalefont setfont 0 0 moveto (A) false charpath true upath length 0 gt = "
               "/c 0 def {pop pop} {pop pop} {6 {pop} repeat} {/c c 1 add def} pathforall c =").split()
    assert out == ["true", "0"]


def test_readline_says_false_at_the_end_of_the_file():
    assert says("{currentfile 10 string readline exch = =} exec\nabc").split() == ["abc", "false"]
    assert says("{currentfile 10 string readline exch = =} exec\nabc\n").split() == ["abc", "true"]


@needs_pymupdf
def test_masked_pictures_keep_indexes_and_decode_arrays():
    """ImageType 3 with the mask as each pixel's first sample (InterleaveType 1): an Indexed picture's indexes
    used to be scaled to 0..255 (another colour), and the picture's Decode was dropped."""
    def one(space, bpc, decode, sample):
        d = doc(f"""%!PS
0 1 0 setrgbcolor 0 0 200 200 rectfill {space} setcolorspace
<< /ImageType 3 /InterleaveType 1
   /DataDict << /ImageType 1 /Width 1 /Height 1 /BitsPerComponent {bpc} /Decode {decode} /ImageMatrix [1 0 0 1 0 0]
                /DataSource <{sample}> >>
   /MaskDict << /ImageType 1 /Width 1 /Height 1 /BitsPerComponent {bpc} /Decode [0 1] /ImageMatrix [1 0 0 1 0 0] >>
>> 100 100 scale image showpage
""")
        return color(d, 50, 50)
    assert one("[/Indexed /DeviceRGB 3 <000000 FF0000 0000FF FFFFFF>]", 2, "[0 3]", "10")[:3] == (255, 0, 0)  # mask 0 (paint), index 1
    assert white(one("/DeviceGray", 8, "[1 0]", "0000"))


@needs_pymupdf
def test_type42_metrics_move_the_text_as_the_pdf_draws_it():
    import lzma
    from importlib import resources

    from bookport.pdf import ttf
    data = lzma.decompress(resources.files("bookport").joinpath("fonts/NotoSerif-Regular.ttf.xz").read_bytes())
    f = ttf.load(data)
    gids = {n: f.cmap[ord(c)] for n, c in (("H", "H"), ("i", "i"))}
    chunks = [data[i:i + 60000] for i in range(0, len(data), 60000)]
    chunks = [c + (b"\0" if len(c) % 2 else b"") for c in chunks]
    sfnts = b" ".join(b"<" + c.hex().encode() + b">" for c in chunks)
    src = (b"%!PS\n/Noto 12 dict begin /FontType 42 def /FontName /NotoSerif def /FontMatrix [1 0 0 1 0 0] def "
           b"/FontBBox [0 0 1 1] def /PaintType 0 def /Encoding 256 array 0 1 255 {1 index exch /.notdef put} for "
           b"dup 72 /H put dup 105 /i put def /CharStrings 3 dict dup begin /.notdef 0 def " +
           b" ".join(b"/%s %d def" % (n.encode(), g) for n, g in gids.items()) +
           b" end def /Metrics 1 dict dup /H 2 put def /sfnts [" + sfnts + b"] def currentdict end definefont pop\n"
           b"/Noto findfont 60 scalefont setfont 100 400 moveto (Hi) show showpage")
    d = pymupdf.open(stream=pdf(src), filetype="pdf")
    chars = [c for b in d[0].get_text("rawdict")["blocks"] for ln in b["lines"] for s in ln["spans"] for c in s["chars"]]
    assert abs(chars[1]["origin"][0] - (100 + 2 * 60)) < 1  # i where the H's Metrics width of 2 em puts it


def test_inline_images_are_found_outside_strings_only():
    parts = write._split_inline(b"BT (take BI and ID cards) Tj ET")
    assert len(parts) == 1 and (("v", b"take BI and ID cards") in parts[0])
    parts = write._split_inline(b"q (a\\) BI) Tj % BI in a comment\nBI /W 1 /H 1 /BPC 8 /CS /G ID x EI Q")
    assert [type(p).__name__ for p in parts] == ["list", "tuple", "list"]


def test_readonly_gives_a_read_only_copy():
    assert says("/a [1 2] def a readonly wcheck = a wcheck = /s (ab) def s readonly pop s wcheck =").split() == \
        ["false", "true", "true"]


def test_the_default_spot_function_runs_as_a_procedure():
    assert says("0.5 0.5 currentscreen exch pop exch pop exec count =").split() == ["1"]


def test_a_loop_counts_each_turn_once():
    """loop counted its turns twice against the step budget (so it got half the budget other loops get)."""
    I = run("/n 0 def {/n n 1 add def n 400 ge {exit} if} loop")
    per_turn = 1 + 10  # the loop's own step, and the ten objects of its body
    assert I.steps <= 400 * per_turn + 50


def test_a_full_lzw_table_stays_full():
    """Past 4,096 entries (with no clear code) the table used to keep growing with entries nothing can name."""
    import tracemalloc

    from bookport.ps import files
    codes, size, prev = [65], 258, b"A"  # A, then each code the next new entry: AA, AAA, ...
    while size < 4096:
        codes.append(size)
        prev = prev + prev[:1]
        size += 1
    codes += [4095] * 6000 + [257]  # each a 3.8 KB entry: the output, and before, as much again in the table
    bits, width, table = [], 9, 258
    for i, c in enumerate(codes):
        bits.append(format(c, f"0{width}b"))
        if i and table < 4096:
            table += 1
        elif i == 0:
            table = 258
        if table + 1 >= (1 << width) and width < 12:
            width += 1
    s = "".join(bits)
    data = int(s + "0" * (-len(s) % 8), 2).to_bytes((len(s) + 7) // 8, "big")
    tracemalloc.start()
    out = files._lzw(Stream(data))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert out.startswith(b"A") and len(out) > 6000 * 3800
    assert peak < 2.6 * len(out)  # the output while it's built and as bytes (2x), not a third copy in the table


# ---- PostScript handed to bookport.read and bookport.convert

def _eps(tmp_path) -> Path:
    p = tmp_path / "figure.eps"
    p.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 300 100\n%%Title: A figure\n"
                  b"/Helvetica findfont 24 scalefont setfont 10 40 moveto (Hello PostScript) show showpage\n")
    return p


def test_postscript_is_detected_and_read_as_a_book(tmp_path):
    p = _eps(tmp_path)
    assert bookport.detect(p) == "eps"
    q = tmp_path / "job.ps"
    q.write_bytes(b"%!PS\n/Helvetica findfont 12 scalefont setfont 72 700 moveto (A page) show showpage\n")
    assert bookport.detect(q) == "ps"
    book = bookport.read(p)  # used to come back as a text book of the program itself
    assert book.source_format == "eps"
    assert "Hello PostScript" in book.text() and "findfont" not in book.text()
    assert bookport.formats()["eps"]["read"] and not bookport.formats()["eps"]["write"]


def test_postscript_to_pdf_keeps_the_pages_as_drawn(tmp_path):
    p = _eps(tmp_path)
    out = bookport.convert(p, tmp_path / "figure.pdf")
    assert out.read_bytes() == ps.to_pdf(p)  # straight through bookport.ps, not reflowed as a book
    assert bookport.convert(p, tmp_path / "figure.epub").stat().st_size > 0
    from bookport.__main__ import main
    assert main([str(p), str(tmp_path / "cli.pdf")]) == 0  # the command line too
    assert (tmp_path / "cli.pdf").read_bytes() == out.read_bytes()


def test_postscript_with_a_printer_job_or_dos_header_is_detected(tmp_path):
    a = tmp_path / "job"
    a.write_bytes(b"\x1b%-12345X@PJL ENTER LANGUAGE = POSTSCRIPT\n%!PS-Adobe-3.0\nshowpage\n")
    assert bookport.detect(a) == "ps"
    b = tmp_path / "dos.eps"
    b.write_bytes(b"\xc5\xd0\xd3\xc6" + struct.pack("<IIIIIIH", 30, 20, 0, 0, 0, 0, 0xFFFF)
                  + b"%!PS-Adobe-3.0 EPSF-3.0\nshowpage\n")
    assert bookport.detect(b) == "eps"
    c = tmp_path / "figure"  # no extension to go by: the header says it
    c.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n")
    assert bookport.detect(c) == "eps"


def test_a_tex_file_with_an_editors_magic_comment_is_not_postscript(tmp_path):
    a = tmp_path / "paper.tex"
    a.write_bytes(b"%!TEX program = xelatex\n\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n")
    assert bookport.detect(a) == "tex"
    assert "Hello" in bookport.read(a).text()
    b = tmp_path / "figure.tex"  # but a PostScript header is PostScript whatever the name
    b.write_bytes(b"%!PS-Adobe-3.0\nshowpage\n")
    assert bookport.detect(b) == "ps"
