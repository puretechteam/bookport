"""What the 0.4 review of the PDF and DjVu code found: each test fails on the code before its fix."""

import random
import zlib

import pytest

import bookport
from bookport import limits
from bookport.errors import BookportError
from bookport.pdf import parser

from .test_pdf import BASE, _content, make_pdf, stream


def _read_text(tmp_path, objs, name="a.pdf", data=None):
    p = tmp_path / name
    p.write_bytes(data if data is not None else make_pdf(objs))
    return bookport.read(p).text()


# --- the parser ---------------------------------------------------------------------------------------

def test_a_cross_reference_table_with_every_offset_wrong_is_repaired(tmp_path):
    """A table whose offsets all miss their objects (a hand-edited file): each object is found by a scan."""
    import re
    objs = dict(BASE)
    objs[4] = stream(_content([b"Found where it really is."]))
    data = make_pdf(objs)
    head, sep, tail = data.partition(b"\nxref\n")
    tail = re.sub(rb"(\d{10}) 00000 n", lambda m: b"%010d 00000 n" % (int(m.group(1)) + 7), tail)
    assert "Found where it really is." in _read_text(tmp_path, None, data=head + sep + tail)


@pytest.mark.parametrize("parms", [b"[null]", b"7", b"[[1 2]]", b"[<< /Predictor 1 >>]"])
def test_decode_parms_that_isnt_a_dictionary_is_ignored(tmp_path, parms):
    objs = dict(BASE)
    text = _content([b"Odd parameters, plain text."])
    objs[4] = stream(zlib.compress(text), b"/Filter /FlateDecode /DecodeParms " + parms)
    assert "Odd parameters, plain text." in _read_text(tmp_path, objs)


def test_run_length_output_is_held_to_the_limits(monkeypatch):
    bomb = (b"\x81\x00" * 4000) + b"\x80"  # 128 bytes out for every 2 in
    monkeypatch.setattr(limits, "MAX_PART", 100_000)
    with pytest.raises(limits.LimitError):
        parser._rl(bomb)


def _rows(ft, colors, cols, rows, seed=1):
    rnd = random.Random(seed)
    row = colors * cols
    data = bytearray()
    for _ in range(rows):
        data.append(ft if ft is not None else rnd.randrange(5))
        data += rnd.randbytes(row)
    return bytes(data)


def _slow_predict(data, colors, cols):
    """PNG predictors as the PDF spec gives them, one byte at a time (what _predict did before)."""
    row, bpp = colors * cols, colors
    out, prev, pos = bytearray(), bytearray(row), 0
    while pos + 1 + row <= len(data) or pos + 1 < len(data):
        ft, line = data[pos], bytearray(data[pos + 1:pos + 1 + row])
        line += bytes(row - len(line))
        pos += 1 + row
        for i in range(row):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if ft == 1:
                line[i] = (line[i] + a) & 255
            elif ft == 2:
                line[i] = (line[i] + b) & 255
            elif ft == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line
        prev = line
    return bytes(out)


@pytest.mark.parametrize("pillow", [True, False])
@pytest.mark.parametrize("ft", [0, 1, 2, 3, 4, None])
def test_png_predictors_give_the_same_bytes_fast_or_slow(monkeypatch, pillow, ft):
    if pillow:
        pytest.importorskip("PIL")
    else:
        monkeypatch.setattr(parser, "_png_rows", lambda *a: None)
    for colors, cols, rows, cut in ((3, 300, 80, 0), (1, 700, 100, 5), (4, 90, 200, 1), (2, 500, 70, 0)):
        data = _rows(ft, colors, cols, rows)
        data = data[:len(data) - cut]
        parms = {"Predictor": 12, "Colors": colors, "BitsPerComponent": 8, "Columns": cols}
        assert parser._predict(data, parms) == _slow_predict(data, colors, cols)


def test_paeth_predicted_pictures_are_quick():
    import time
    pytest.importorskip("PIL")
    data = _rows(4, 3, 1000, 1000)
    start = time.perf_counter()
    parser._predict(data, {"Predictor": 15, "Colors": 3, "BitsPerComponent": 8, "Columns": 1000})
    assert time.perf_counter() - start < 0.5  # 1000 x 1000 RGB: 1.4 s byte by byte, 0.035 s now


# --- the reader -------------------------------------------------------------------------------------

def _two_columns(lines=5):
    ops = [b"BT /F1 11 Tf 14 TL 50 700 Td"]
    ops += [b"(Left column line %d of the text.) Tj T*" % k for k in range(lines)]
    ops += [b"ET BT /F1 11 Tf 14 TL 320 700 Td"]
    ops += [b"(Right column line %d of the text.) Tj T*" % k for k in range(lines)]
    ops.append(b"ET")
    objs = dict(BASE)
    objs[3] = BASE[3].replace(b"[0 0 300 400]", b"[0 0 612 792]")
    objs[4] = stream(b"\n".join(ops))
    return objs


def test_a_page_in_two_columns_reads_one_column_then_the_other(tmp_path):
    text = _read_text(tmp_path, _two_columns())
    left = [text.index(f"Left column line {k}") for k in range(5)]
    right = [text.index(f"Right column line {k}") for k in range(5)]
    assert left == sorted(left) and right == sorted(right) and max(left) < min(right), text


def test_a_title_across_both_columns_comes_first(tmp_path):
    objs = _two_columns()
    objs[4] = stream(b"BT /F1 18 Tf 150 750 Td (A Title Across The Page) Tj ET\n" + objs[4].split(b"stream\n", 1)[1]
                     .rsplit(b"\nendstream", 1)[0])
    text = _read_text(tmp_path, objs)
    assert text.index("A Title Across") < text.index("Left column line 0") < text.index("Right column line 0")


def test_one_column_with_labels_beside_it_stays_in_line_order(tmp_path):
    """Short labels down one side (a table of contents' page numbers drawn last) are no column."""
    ops = [b"BT /F1 11 Tf 14 TL 50 700 Td"] + [b"(Chapter number %d of the book) Tj T*" % k for k in range(6)]
    ops += [b"ET BT /F1 11 Tf 14 TL 500 700 Td"] + [b"(%d) Tj T*" % (k * 10 + 1) for k in range(6)] + [b"ET"]
    objs = dict(BASE)
    objs[3] = BASE[3].replace(b"[0 0 300 400]", b"[0 0 612 792]")
    objs[4] = stream(b"\n".join(ops))
    text = _read_text(tmp_path, objs)
    assert text.index("Chapter number 0") < text.index("Chapter number 1")
    assert text.index("Chapter number 1") < text.index("11")  # beside its line, as before


def test_inline_image_data_isnt_read_as_operators(tmp_path):
    rnd = random.Random(7)
    for k in range(40):  # 18 of these 40 seeds used to turn the text around
        pixels = rnd.randbytes(40 * 40 * 3)
        content = (b"BT /F1 12 Tf 40 360 Td (Before) Tj ET\n"
                   b"q 40 0 0 40 40 300 cm BI /W 40 /H 40 /BPC 8 /CS /RGB ID " + pixels + b"\nEI Q\n"
                   b"BT /F1 12 Tf 40 200 Td (After) Tj ET")
        objs = dict(BASE)
        objs[4] = stream(content)
        text = _read_text(tmp_path, objs, name=f"i{k}.pdf")
        assert "Before" in text and "After" in text and text.index("Before") < text.index("After"), (k, text)


def test_inline_image_with_a_filter_ends_at_its_ei(tmp_path):
    content = (b"BT /F1 12 Tf 40 360 Td (Before) Tj ET\n"
               b"BI /W 4 /H 4 /F /AHx ID 00ff00ff00ff00ff00ff00ff00ff00ff> EI\n"
               b"BT /F1 12 Tf 40 200 Td (After) Tj ET")
    objs = dict(BASE)
    objs[4] = stream(content)
    text = _read_text(tmp_path, objs)
    assert text.index("Before") < text.index("After")


def test_a_small_tounicode_map_of_huge_ranges_reads_quickly(tmp_path):
    import time
    lines = b"\n".join(b"<%04X0000> <%04XFFFF> <0041>" % (i, i) for i in range(300))
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n1 begincodespacerange <00000000> <FFFFFFFF>"
            b" endcodespacerange\n300 beginbfrange\n" + lines + b"\nendbfrange endcmap end end")
    objs = dict(BASE)
    objs[5] = (b"<< /Type /Font /Subtype /Type0 /BaseFont /X /Encoding /Identity-H /DescendantFonts [6 0 R] "
               b"/ToUnicode 7 0 R >>")
    objs[6] = (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /X /DW 500 "
               b"/W [" + b" ".join(b"%d %d 600" % (i * 60000, i * 60000 + 59999) for i in range(900)) + b"] >>")
    objs[7] = stream(cmap)
    objs[4] = stream(b"BT /F1 12 Tf 40 360 Td <0005000100050002> Tj ET")
    start = time.perf_counter()
    text = _read_text(tmp_path, objs)
    took = time.perf_counter() - start
    assert "BC" in text  # 0x0005_0001 -> 'A' + 1, 0x0005_0002 -> 'A' + 2
    assert took < 2.0, took  # was 53 s for the map and 2 s for the widths


def test_cid_widths_and_ranges_are_looked_up_where_they_are():
    from bookport.pdf.reader import CMap, _cid_widths
    w = _cid_widths([10, [100, 200], 50, 60, 700, 5, 7, 300], lambda v: v)
    assert [w.get(c, 0) for c in (10, 11, 12, 50, 55, 60, 61, 5, 7, 8)] == [100, 200, 0, 700, 700, 700, 0, 300, 300, 0]
    m = CMap(b"1 begincodespacerange <00> <FF> endcodespacerange 2 beginbfrange <41> <43> <0061> <20> <21> <00E9> "
             b"endbfrange 1 beginbfchar <44> <0078> endbfchar")
    assert [m.get(bytes([c])) for c in (0x41, 0x43, 0x44, 0x20, 0x21, 0x22)] == ["a", "c", "x", "é", "ê", None]
    only_ranges = CMap(b"1 begincodespacerange <00> <FF> endcodespacerange 1 beginbfrange <41> <43> <0061> endbfrange")
    assert only_ranges.map and only_ranges.map.get(b"B") == "b" and only_ranges.map.get(b"Z", "") == ""  # as a dict


def test_a_page_over_the_operator_budget_keeps_what_comes_before(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_PDF_OPERATORS", 500)
    content = b"BT /F1 12 Tf 40 360 Td (Kept at the start.) Tj ET\n" + b"0 0 m " * 2000
    objs = dict(BASE)
    objs[4] = stream(content)
    assert "Kept at the start." in _read_text(tmp_path, objs)  # the whole stream used to be dropped


def test_one_damaged_picture_is_left_out_not_the_book(tmp_path):
    objs = dict(BASE)
    objs[3] = BASE[3].replace(b"/Font << /F1 5 0 R >>", b"/Font << /F1 5 0 R >> /XObject << /Im 6 0 R >>")
    objs[4] = stream(b"BT /F1 12 Tf 40 360 Td (Words beside a broken picture.) Tj ET q 200 0 0 200 40 40 cm /Im Do Q")
    objs[6] = stream(b"\x00" * 64, b"/Type /XObject /Subtype /Image /Width 8 /Height 8 /BitsPerComponent 8 "
                                   b"/ColorSpace [/ICCBased] ")
    assert "Words beside a broken picture." in _read_text(tmp_path, objs)


def test_a_page_with_one_line_isnt_counted_twice_as_a_running_header():
    """A line on 2 of 6 pages is no running header (that takes 3 pages and 30%); on a page with one line it was
    counted as both a first and a last line, 4 times, and dropped."""
    from bookport.pdf.reader import TextLine, _strip_furniture
    pages = [[TextLine(780, 50, 300, 10, [("A short page at the top", False, False, False)], i)] for i in range(2)]
    pages += [[TextLine(400, 50, 300, 10, [(f"Body {i}", False, False, False)], i)] for i in range(2, 6)]
    _strip_furniture(pages, [792] * 6)
    assert pages[0] and pages[1]


# --- the PDF writer -------------------------------------------------------------------------------

def _book(body_html: str, images: dict | None = None):
    import xml.etree.ElementTree as ET

    from bookport import Book, Metadata, markup
    book = Book(Metadata(title="T", language="en"))
    for href, data in (images or {}).items():
        book.add(href, data)
    root, body = markup.new_document("T", "en")
    for el in markup.parse_fragment(f"<div>{body_html}</div>")[1]:
        body.append(el)
    book.add_document("text/a.xhtml", root)
    ET.SubElement(body, "span")
    return book


def _lines(pdf: bytes, page: int = 0) -> list[str]:
    """The text of each line of a page, as bookport's reader finds it (spaces kept)."""
    from bookport.pdf.reader import Interpreter
    doc = parser.Document(pdf)
    pc = Interpreter(doc).page(doc.pages()[page])
    rows: dict = {}
    for s in sorted(pc.spans, key=lambda s: s.order):
        rows.setdefault(round(s.y), []).append(s)
    return ["".join(s.text for s in sorted(row, key=lambda s: s.x)) for _y, row in sorted(rows.items(), key=lambda r: -r[0])]


def _fonts_used_are_in_resources(pdf: bytes) -> None:
    import re
    doc = parser.Document(pdf)
    for i, page in enumerate(doc.pages()):
        content = doc.decode(doc.resolve(page["Contents"]))
        have = set(doc.resolve(doc.resolve(page["Resources"]).get("Font")) or {})
        used = {m.decode() for m in re.findall(rb"/(\w+) [\d.]+ Tf", content)}
        assert used <= have, (i, used, have)


def test_preformatted_text_keeps_its_indent_and_every_space():
    from bookport.pdf.typeset import typeset
    pdf = typeset(_book("<pre>def f():\n        return 1\nx  =  2</pre>"))
    lines = [ln for ln in _lines(pdf) if ln.strip() and not ln.strip().isdigit()]
    assert lines[:3] == ["def f():", "        return 1", "x  =  2"], lines


def test_a_book_of_pictures_or_bold_only_names_the_fonts_its_pages_use():
    from bookport.pdf.typeset import typeset

    from .conftest import png
    pictures = _book('<p><img src="../images/a.png"/></p><p style="page-break-before: always"><img src="../images/b.png"/></p>',
                     {"images/a.png": png(400, 600), "images/b.png": png(400, 600, (0, 90, 0))})
    _fonts_used_are_in_resources(typeset(pictures))
    _fonts_used_are_in_resources(typeset(_book("<p><b>Only bold words here.</b></p>")))


@pytest.mark.parametrize("kind, mark", [("square", "▪"), ("circle", "◦"), ("disc", "•")])
def test_list_markers_are_drawn_in_a_font_that_has_them(tmp_path, kind, mark):
    from bookport.pdf.typeset import typeset
    pdf = typeset(_book(f'<ul style="list-style-type: {kind}"><li>An item</li></ul>'))
    p = tmp_path / "l.pdf"
    p.write_bytes(pdf)
    text = "".join(_lines(pdf))
    assert mark in text and "�" not in text, text


def test_a_line_taller_than_the_page_leaves_no_blank_page():
    """A picture in a line, as tall as a page holds: with the line spacing the line is taller than the page, and
    at the top of a page it used to start another (blank) one first."""
    from bookport.pdf.typeset import typeset

    from .conftest import png
    book = _book('<p><img src="../images/tall.png"/> beside it</p>', {"images/tall.png": png(1000, 2600)})
    doc = parser.Document(typeset(book))
    texts = [doc.decode(doc.resolve(p["Contents"])) for p in doc.pages()]
    assert len(texts) == 1 and b" Do" in texts[0], [len(t) for t in texts]


def test_a_table_row_that_goes_on_to_the_next_page_draws_nothing_under_the_margin():
    import re

    from bookport.pdf.typeset import Typesetter
    cell = "<br/>".join(f"Line {k} of a long cell" for k in range(120))
    lowest = []
    for n in range(28, 48):  # the row starts lower and lower down the first page
        book = _book("".join(f"<p>Filler paragraph {k}.</p>" for k in range(n)) + f"<table><tr><td>{cell}</td></tr></table>")
        from bookport import clean
        b = book.copy()
        clean.clean_book(b)
        ts = Typesetter(b)
        doc = parser.Document(ts.run())
        for page in doc.pages():
            content = doc.decode(doc.resolve(page["Contents"]))
            for m in re.finditer(rb"1 0 0 1 [\d.]+ ([\d.]+) Tm\n(?!<)", content):  # line text, not the page number
                lowest.append(float(m.group(1)))
    assert min(lowest) >= ts.mb - 4, min(lowest)


# --- rewriting PDFs ---------------------------------------------------------------------------------

def _named_link_pdf(pages: int = 2) -> bytes:
    """pages pages, the first with a link to the named place (sec), which is the last page: as LaTeX names it."""
    from .test_pdf_rewrite import _numbered
    last = 100 + pages - 1
    extra = {
        100: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 200 0 R /Annots [600 0 R] "
              b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        600: b"<< /Type /Annot /Subtype /Link /Rect [0 0 100 20] /A << /S /GoTo /D (sec) >> >>",
        601: b"<< /Names [(sec) [%d 0 R /Fit]] >>" % last,
    }
    return _numbered(pages, extra_catalog=b"/Names << /Dests 601 0 R >> ", extra=extra)


def _link_targets(data: bytes) -> list:
    """For each link on each page: the page it jumps to (1-based), by bookport's own reader."""
    doc = parser.Document(data)
    nums = [p["_ref"].num for p in doc.pages()]
    from bookport.pdf.rewrite import _dest, _Source
    src = _Source(data, 0)
    out = []
    for p in doc.pages():
        for a in doc.resolve(p.get("Annots")) or []:
            d = _dest(src, doc.resolve(doc.resolve(a).get("A")))
            out.append(nums.index(d[0].num) + 1 if d else None)
    return out


def test_joined_files_links_by_name_go_to_their_own_file():
    from bookport.pdf import rewrite
    a, b = _named_link_pdf(2), _named_link_pdf(2)
    joined = rewrite.edit([a, b])[0]
    assert _link_targets(joined) == [2, 4]  # the second file's link went to page 2, in the first file


def test_page_labels_follow_their_pages():
    from bookport.pdf import rewrite

    from .test_pdf_rewrite import _numbered
    labels = b"/PageLabels << /Nums [0 << /S /r >> 2 << /S /D >>] >> "
    data = _numbered(5, extra_catalog=labels)

    def shown(pdf):
        doc = parser.Document(pdf)
        nums = doc.resolve(doc.root.get("PageLabels"))["Nums"]
        return [(nums[i], {k: str(v) if not isinstance(v, int) else v for k, v in doc.resolve(nums[i + 1]).items()})
                for i in range(0, len(nums), 2)]
    assert shown(rewrite.compress(data)) == [(0, {"S": "r"}), (2, {"S": "D"})]
    # pages 2, 4 and 5: ii, then 2 and 3
    assert shown(rewrite.edit([data], pages=[(1, 0), (3, 0), (4, 0)])[0]) == [(0, {"S": "r", "St": 2}), (1, {"S": "D", "St": 2})]
    # joined with a file without labels: that one's pages are 1, 2...
    assert shown(rewrite.edit([data, _numbered(2)])[0])[-1] == (5, {"S": "D"})


def test_the_tag_tree_stays_when_every_page_does():
    from bookport.pdf import rewrite

    from .test_pdf_rewrite import _numbered
    data = _numbered(2, extra_catalog=b"/StructTreeRoot 700 0 R /MarkInfo << /Marked true >> ",
                     extra={700: b"<< /Type /StructTreeRoot /K [701 0 R] >>",
                            701: b"<< /Type /StructElem /S /P /Pg 101 0 R /K 0 >>"})
    kept = parser.Document(rewrite.compress(data))
    tree = kept.resolve(kept.root.get("StructTreeRoot"))
    elem = kept.resolve(tree["K"][0])
    assert elem["Pg"].num == kept.pages()[1]["_ref"].num and kept.root.get("MarkInfo")
    turned = parser.Document(rewrite.edit([data], pages=[(1, 0), (0, 0)])[0])
    assert "StructTreeRoot" not in turned.root  # other pages or another order: the tree would be wrong


def test_one_huge_picture_is_left_as_it_is_when_making_a_pdf_smaller():
    from bookport.pdf import rewrite

    from .test_pdf_rewrite import _image, _photo_pdf
    pytest.importorskip("PIL")
    data = _photo_pdf(size=(300, 200))
    # the same file, but its picture says it's 12,000 x 9,000 (over the pixel limit)
    big = data.replace(b"/Width 300", b"/Width 12000").replace(b"/Height 200", b"/Height 9000")
    assert big != data
    out = rewrite.compress(big)
    assert parser.Document(out).pages() and _image(out) is not None


def test_a_page_taken_out_alone_takes_only_the_pictures_it_draws():
    from bookport.pdf import rewrite

    from .test_pdf_rewrite import _numbered
    pics = {701: stream(b"\x11" * 30_000, b"/Type /XObject /Subtype /Image /Width 100 /Height 100 /BitsPerComponent 8 "
                                         b"/ColorSpace /DeviceRGB"),
            702: stream(b"\x22" * 30_000, b"/Type /XObject /Subtype /Image /Width 100 /Height 100 /BitsPerComponent 8 "
                                         b"/ColorSpace /DeviceRGB"),
            700: b"<< /Font << /F1 5 0 R >> /XObject << /A 701 0 R /B 702 0 R >> >>",
            100: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 200 0 R /Resources 700 0 R >>",
            101: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 201 0 R /Resources 700 0 R >>",
            200: stream(b"q 100 0 0 100 0 0 cm /A Do Q BT /F1 12 Tf 40 360 Td (Page 1) Tj ET"),
            201: stream(b"q 100 0 0 100 0 0 cm /B Do Q BT /F1 12 Tf 40 360 Td (Page 2) Tj ET")}
    parts = rewrite.edit([_numbered(2, extra=pics)], split=1)
    for part, mine, other in ((parts[0], b"\x11", b"\x22"), (parts[1], b"\x22", b"\x11")):
        doc = parser.Document(part)
        images = [doc.decode(o) for n in doc.xref if isinstance(o := doc.get(n), parser.Stream)
                  and str(o.dict.get("Subtype")) == "Image"]
        assert len(images) == 1 and images[0][:1] == mine, len(images)
    whole = parser.Document(rewrite.edit([_numbered(2, extra=pics)])[0])  # every page kept: the shared dictionary as it was
    kept = [whole.get(n) for n in whole.xref]
    assert sum(isinstance(o, parser.Stream) and str(o.dict.get("Subtype")) == "Image" for o in kept) == 2


def test_anything_a_damaged_file_breaks_while_rewriting_is_said_as_damage(monkeypatch):
    from bookport.pdf import rewrite

    from .test_pdf_rewrite import _numbered

    def broken(*a, **k):
        raise TypeError("odd")
    monkeypatch.setattr(rewrite, "_outline", broken)
    with pytest.raises(BookportError, match="Damaged PDF"):
        rewrite.edit([_numbered(2)])


def test_bookmarks_to_web_pages_stay_and_open_ones_stay_open():
    from bookport.pdf import rewrite

    from .test_pdf_rewrite import _numbered
    extra = {50: b"<< /Type /Outlines /First 300 0 R /Last 302 0 R /Count 4 >>",
             300: b"<< /Title (Mark 1) /Parent 50 0 R /Dest [100 0 R /Fit] /Next 302 0 R /First 301 0 R /Last 301 0 R /Count 1 >>",
             301: b"<< /Title (Kid) /Parent 300 0 R /Dest [101 0 R /Fit] >>",
             302: b"<< /Title (Website) /Parent 50 0 R /Prev 300 0 R /A << /S /URI /URI (https://example.org) >> >>"}
    out = parser.Document(rewrite.edit([_numbered(2, extra=extra)])[0])
    top = out.resolve(out.root["Outlines"])
    first = out.resolve(top["First"])
    second = out.resolve(first["Next"])
    assert first["Count"] == 1  # open, as it was (every bookmark used to be written closed)
    assert parser.text_string(second["Title"]) == "Website" and bytes(out.resolve(second["A"])["URI"]) == b"https://example.org"


# --- encryption ------------------------------------------------------------------------------------

def test_aes_cbc_matches_the_nist_example_and_is_quick():
    import time

    from bookport.pdf import crypt
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")
    cipher = crypt.AES(key).cbc_encrypt(plain, iv)
    assert cipher.hex() == "7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2"  # SP 800-38A F.2.1
    assert crypt.AES(key).cbc_decrypt(cipher, iv) == plain
    data = random.Random(5).randbytes(256 * 1024)
    start = time.perf_counter()
    crypt.aes_decrypt(key, data)
    assert time.perf_counter() - start < 1.5  # 256 KB took 4.9 to 12.6 s byte by byte; 0.2 s now


def _aesv2_pdf(objs, with_length: bool):
    """Encrypted with AES-128 (V 4, R 4, the crypt filter AESV2), empty user password; with or without /Length."""
    import hashlib
    import re
    import struct

    from bookport.pdf import crypt
    file_id = hashlib.md5(b"aesv2 test").digest()
    pad = crypt.PAD
    okey = hashlib.md5(b"owner" + pad[:27]).digest()
    for _ in range(50):
        okey = hashlib.md5(okey).digest()
    o = crypt.rc4(okey, pad)
    for i in range(1, 20):
        o = crypt.rc4(bytes(b ^ i for b in okey), o)
    key = hashlib.md5(pad + o + struct.pack("<i", -4) + file_id).digest()
    for _ in range(50):
        key = hashlib.md5(key).digest()
    u = crypt.rc4(key, hashlib.md5(pad + file_id).digest())
    for i in range(1, 20):
        u = crypt.rc4(bytes(b ^ i for b in key), u)
    u += bytes(16)
    out = {}
    for num, body in objs.items():
        m = re.match(rb"(<< /Length )(\d+)( .*?>>\nstream\n)(.*)(\nendstream)$", body, re.DOTALL)
        if m:
            k = hashlib.md5(key + struct.pack("<I", num)[:3] + b"\0\0sAlT").digest()
            plain = m.group(4) + bytes([16 - len(m.group(4)) % 16]) * (16 - len(m.group(4)) % 16)
            iv = bytes(range(16))
            data = iv + crypt.AES(k).cbc_encrypt(plain, iv)
            body = m.group(1) + str(len(data)).encode() + m.group(3) + data + m.group(5)
        out[num] = body
    length = b"/Length 128 " if with_length else b""
    out[8] = (b"<< /Filter /Standard /V 4 /R 4 " + length + b"/P -4 /O <%s> /U <%s> /CF << /StdCF << /CFM /AESV2 "
              b"/AuthEvent /DocOpen >> >> /StmF /StdCF /StrF /Identity >>" % (o.hex().encode(), u.hex().encode()))
    return out, file_id


@pytest.mark.parametrize("with_length", [True, False])
def test_aes_128_without_a_length_opens(tmp_path, with_length):
    objs = dict(BASE)
    objs[4] = stream(_content([b"Encrypted, no password."]))
    enc, fid = _aesv2_pdf(objs, with_length)
    data = make_pdf(enc, trailer_extra=b"/Encrypt 8 0 R /ID [<%s> <%s>]" % (fid.hex().encode(), fid.hex().encode()))
    assert "Encrypted, no password." in _read_text(tmp_path, None, data=data)  # without /Length: "needs a password"


# --- DjVu -------------------------------------------------------------------------------------------

DJVU = __import__("pathlib").Path(__file__).parent / "djvu"


def test_text_read_on_threads_keeps_the_unpacking_budget(monkeypatch):
    from bookport import djvu
    from bookport.djvu import text, topdf
    doc = djvu.open(DJVU / "book.djvu")
    doc.pages = doc.pages * 2  # 10 pages: past the 8 that turn threads on
    seen = []

    def plain(page):
        seen.append(limits._budget.get())
        return b"x"
    monkeypatch.setattr(topdf, "threads", lambda: 2)
    monkeypatch.setattr(text, "plain", plain)
    assert djvu.to_text(doc).count("x") == 10
    assert len(seen) == 10 and all(b is not None for b in seen)  # copied on a worker, each page had none


def test_one_damaged_djvu_page_is_left_blank_not_the_whole_document(monkeypatch):
    import struct

    from bookport import djvu
    from bookport.djvu import composite
    real = composite.layers

    def layers(page):
        if page.index == 1:
            raise struct.error("unpack requires a buffer of 4 bytes")
        return real(page)
    monkeypatch.setattr(composite, "layers", layers)
    monkeypatch.setattr(composite, "color_masks", lambda lay: [])
    doc = djvu.open(DJVU / "book.djvu")
    pdf = parser.Document(djvu.to_pdf(doc))
    assert len(pdf.pages()) == 5


def _rotate_by_hand(pic, degrees):
    """The rotation as the spec says, pixel by pixel (what _rotate did before)."""
    w, h = pic.width, pic.height
    if pic.mode == "1":
        nb = (w + 7) // 8
        px = [[(pic.data[y * nb + (x >> 3)] >> (7 - (x & 7))) & 1 for x in range(w)] for y in range(h)]
    else:
        nc = 3 if pic.mode == "RGB" else 1
        px = [[pic.data[(y * w + x) * nc:(y * w + x + 1) * nc] for x in range(w)] for y in range(h)]
    if degrees == 180:
        out = [row[::-1] for row in px[::-1]]
    elif degrees == 90:
        out = [[px[y][x] for y in range(h)] for x in range(w - 1, -1, -1)]
    else:
        out = [[px[y][x] for y in range(h - 1, -1, -1)] for x in range(w)]
    if pic.mode == "1":
        return b"".join(bytes(sum(bit << (7 - k) for k, bit in enumerate(row[i:i + 8])) for i in range(0, len(row), 8))
                        for row in out)
    return b"".join(b"".join(row) for row in out)


def _scale_by_hand(pic, scale):
    """Box averaging, one output pixel at a time (what _scale did before)."""
    w2, h2 = max(1, round(pic.width * scale)), max(1, round(pic.height * scale))
    if pic.mode == "1":
        nb = (pic.width + 7) // 8
        data = bytes(0 if (pic.data[y * nb + (x >> 3)] >> (7 - (x & 7))) & 1 else 255
                     for y in range(pic.height) for x in range(pic.width))
    else:
        data = pic.data
    nc = 3 if pic.mode == "RGB" else 1
    out = bytearray()
    for y in range(h2):
        y0 = y * pic.height // h2
        y1 = max(y0 + 1, (y + 1) * pic.height // h2)
        for x in range(w2):
            x0 = x * pic.width // w2
            x1 = max(x0 + 1, (x + 1) * pic.width // w2)
            n = (y1 - y0) * (x1 - x0)
            for c in range(nc):
                s = sum(data[(yy * pic.width + xx) * nc + c] for yy in range(y0, y1) for xx in range(x0, x1))
                out.append((s + n // 2) // n)
    return bytes(out)


@pytest.mark.parametrize("mode", ["1", "L", "RGB"])
def test_djvu_pictures_turn_and_scale_as_pixel_by_pixel(mode):
    from bookport.djvu import Picture, _rotate, _scale
    rnd = random.Random(9)
    for w, h in ((1, 1), (7, 3), (8, 5), (13, 9), (33, 17)):
        nc = 3 if mode == "RGB" else 1
        if mode == "1":
            nb = (w + 7) // 8
            keep = (0xFF << (nb * 8 - w)) & 0xFF
            data = bytes(b & keep if i % nb == nb - 1 else b for i, b in enumerate(rnd.randbytes(nb * h)))
        else:
            data = rnd.randbytes(w * h * nc)
        pic = Picture(w, h, mode, data)
        for deg in (90, 180, 270):
            assert _rotate(pic, deg).data == _rotate_by_hand(pic, deg), (w, h, deg)
        for sc in (0.13, 0.5, 0.77, 1.6, 3.0):
            assert _scale(pic, sc).data == _scale_by_hand(pic, sc), (w, h, sc)


def test_djvu_rotation_is_quick():
    import time

    from bookport.djvu import Picture, _rotate
    pic = Picture(2000, 2000, "1", random.Random(1).randbytes(250 * 2000))
    start = time.perf_counter()
    _rotate(pic, 90)
    assert time.perf_counter() - start < 0.4  # 1.0 s pixel by pixel; 0.04 s now


def test_a_bundle_finds_each_page_without_searching_every_component():
    import time

    from bookport.djvu import doc as djvu_doc
    d = djvu_doc.Document.__new__(djvu_doc.Document)
    d.components = [djvu_doc.Component(f"p{k:05d}.djvu", 1, f"n{k}", f"t{k}", 0, 0) for k in range(10_000)]
    d._loaded, d.bundled, d.data, d._by_id = {}, True, b"", None
    start = time.perf_counter()
    for k in range(10_000):
        d.component(f"p{k:05d}.djvu")
    assert time.perf_counter() - start < 0.3  # 1.0 s searched one by one; 0.006 s now
    found = []
    d._loaded = {}
    d._sibling = found.append
    d.bundled = False
    for key in ("p00003.djvu", "n7", "t9"):
        d.component(key)
    assert [c.id for c in found] == ["p00003.djvu", "p00007.djvu", "p00009.djvu"]


# --- what the reader now reads ------------------------------------------------------------------------

def test_web_links_on_a_page_are_kept_round_their_words(tmp_path):
    from bookport import markup
    objs = dict(BASE)
    objs[3] = BASE[3].replace(b"/Resources", b"/Annots [6 0 R] /Resources")
    objs[4] = stream(b"BT /F1 12 Tf 40 360 Td (Read the ) Tj ET BT /F1 12 Tf 100 360 Td (manual) Tj ET "
                     b"BT /F1 12 Tf 150 360 Td ( online.) Tj ET")
    objs[6] = b"<< /Type /Annot /Subtype /Link /Rect [98 355 145 372] /A << /S /URI /URI (https://example.org/manual) >> >>"
    p = tmp_path / "l.pdf"
    p.write_bytes(make_pdf(objs))
    book = bookport.read(p)
    html = b"".join(markup.serialize(book.tree(h)) for h in book.spine).decode("utf-8")
    assert '<a href="https://example.org/manual">manual</a>' in html, html


def _g4_page(tmp_path, extra=b"", black_is_1=False):
    from bookport.djvu import mmr
    w, h = 64, 40
    rows = [((1 << 20) - 1) << 10 if 10 <= y < 30 else 0 for y in range(h)]  # a black bar: columns 10-29, rows 10-29
    data = mmr.encode(rows, w)
    parms = b"<< /K -1 /Columns 64 /Rows 40 %s>>" % (b"/BlackIs1 true " if black_is_1 else b"")
    objs = dict(BASE)
    objs[3] = BASE[3].replace(b"/Font << /F1 5 0 R >>", b"/Font << /F1 5 0 R >> /XObject << /Im 6 0 R >>")
    objs[4] = stream(b"q 280 0 0 380 10 10 cm /Im Do Q")
    objs[6] = stream(data, b"/Type /XObject /Subtype /Image /Width 64 /Height 40 /BitsPerComponent 1 /ColorSpace /DeviceGray "
                           b"/Filter /CCITTFaxDecode /DecodeParms " + parms + b" " + extra)
    p = tmp_path / "scan.pdf"
    p.write_bytes(make_pdf(objs))
    book = bookport.read(p)
    pics = [r for r in book.resources.values() if r.is_image]
    assert len(pics) == 1, "the scan's picture was dropped"
    import io

    from PIL import Image
    img = Image.open(io.BytesIO(pics[0].data)).convert("L")
    return img.getpixel((15, 15)), img.getpixel((5, 5))  # inside the bar, outside it


@pytest.mark.parametrize("extra, black_is_1, want", [(b"", False, (0, 255)), (b"/Decode [1 0]", False, (255, 0)),
                                                     (b"/Decode [1 0]", True, (0, 255))])
def test_a_scanned_page_in_ccitt_g4_keeps_its_picture(tmp_path, extra, black_is_1, want):
    pytest.importorskip("PIL")
    assert _g4_page(tmp_path, extra, black_is_1) == want


def _cid_pdf(tmp_path, font_file: bool, encoding=b"/Identity-H", codes=None):
    from bookport.pdf import ttf
    from bookport.pdf.typeset import font_bytes
    data = font_bytes("regular")
    font = ttf.load(data)
    codes = codes if codes is not None else b"".join(font.cmap[ord(c)].to_bytes(2, "big") for c in "Hi there")
    objs = dict(BASE)
    objs[5] = b"<< /Type /Font /Subtype /Type0 /BaseFont /NotoSerif /Encoding " + encoding + b" /DescendantFonts [6 0 R] >>"
    objs[6] = (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /NotoSerif /CIDToGIDMap /Identity /DW 500 "
               b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /FontDescriptor 7 0 R >>")
    objs[7] = b"<< /Type /FontDescriptor /FontName /NotoSerif " + (b"/FontFile2 10 0 R " if font_file else b"") + b">>"
    if font_file:
        objs[10] = stream(data)
    objs[4] = stream(b"BT /F1 12 Tf 40 360 Td <" + codes.hex().encode() + b"> Tj ET")
    return _read_text(tmp_path, objs)


def test_identity_h_text_without_tounicode_reads_through_the_fonts_own_cmap(tmp_path):
    assert "Hi there" in _cid_pdf(tmp_path, font_file=True)  # was the glyph numbers as characters


def test_identity_h_text_that_cant_be_told_gives_no_wrong_letters(tmp_path):
    text = _cid_pdf(tmp_path, font_file=False)
    assert not any(c.isalpha() for c in text), text


def test_unicode_cmaps_still_read_codes_as_characters(tmp_path):
    assert "Hi" in _cid_pdf(tmp_path, font_file=False, encoding=b"/UniGB-UCS2-H", codes="Hi".encode("utf-16-be"))
