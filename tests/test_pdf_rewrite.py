"""Rewriting PDFs: pages chosen, ordered and turned; joined; split; compressed. Checked with bookport's own
reader and with PyMuPDF (an independent one) where it's installed."""

import io
import zlib

import pytest

from bookport.errors import BookportError
from bookport.pdf import parser, rewrite

from .test_pdf import BASE, _content, _rc4_encrypt_pdf, make_pdf, needs_pymupdf, pymupdf, stream


def _pages(data: bytes) -> list[dict]:
    return parser.Document(data).pages()


def _texts(data: bytes) -> list[str]:
    doc = parser.Document(data)
    out = []
    for page in doc.pages():
        contents = doc.resolve(page.get("Contents"))
        out.append(doc.decode(contents).decode("latin-1") if isinstance(contents, parser.Stream) else "")
    return out


def _numbered(n: int, extra_catalog: bytes = b"", extra: dict | None = None) -> bytes:
    """A PDF of n pages, each saying its number, with a bookmark on every page."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R /Outlines 50 0 R " + extra_catalog + b">>",
            5: BASE[5], 9: BASE[9]}
    kids, marks = [], []
    for i in range(n):
        page, content, mark = 100 + i, 200 + i, 300 + i
        kids.append(b"%d 0 R" % page)
        objs[page] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents %d 0 R "
                      b"/Resources << /Font << /F1 5 0 R >> >> >>" % content)
        objs[content] = stream(_content([b"Page %d" % (i + 1)]))
        prev = b"/Prev %d 0 R " % (mark - 1) if i else b""
        nxt = b"/Next %d 0 R " % (mark + 1) if i + 1 < n else b""
        objs[mark] = b"<< /Title (Mark %d) /Parent 50 0 R /Dest [%d 0 R /Fit] %s%s>>" % (i + 1, page, prev, nxt)
        marks.append(mark)
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % n
    objs[50] = b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (marks[0], marks[-1], n)
    objs.update(extra or {})
    return make_pdf(objs)


def _outline(data: bytes) -> list[tuple[str, int]]:
    """Each bookmark's title and the page it opens (1-based), by bookport's own reader."""
    doc = parser.Document(data)
    nums = [p["_ref"].num for p in doc.pages()]
    top = doc.resolve(doc.root.get("Outlines"))
    got, node = [], top.get("First") if isinstance(top, dict) else None
    while isinstance(node, parser.Ref):
        item = doc.resolve(node)
        dest = doc.resolve(item.get("Dest"))
        got.append((parser.text_string(doc.resolve(item["Title"])), nums.index(dest[0].num) + 1 if dest else None))
        node = item.get("Next")
    return got


def test_pages_chosen_ordered_and_turned():
    src = _numbered(5)
    out = rewrite.edit([src], pages=[(4, 0), (0, 1), (2, 2), (2, 3)])
    assert len(out) == 1
    pages = _pages(out[0])
    assert [f"Page {n}" in t for n, t in zip((5, 1, 3, 3), _texts(out[0]))] == [True] * 4  # page 3 twice
    assert [p.get("Rotate", 0) for p in pages] == [0, 90, 180, 270]
    assert _outline(out[0]) == [("Mark 1", 2), ("Mark 3", 3), ("Mark 5", 1)]  # 2 and 4 went with their pages
    info = parser.Document(out[0]).info()
    assert parser.text_string(info["Title"]) == "Hand Made"  # the document's details stay


def test_a_turn_adds_to_the_page_s_own_and_to_what_it_inherits():
    src = _numbered(2, extra={2: b"<< /Type /Pages /Kids [100 0 R 101 0 R] /Count 2 /Rotate 90 >>"})
    pages = _pages(rewrite.edit([src], pages=[(0, 0), (1, 1)])[0])
    assert [p.get("Rotate", 0) for p in pages] == [90, 180]


def test_joined_in_order_with_every_bookmark():
    a, b = _numbered(2), _numbered(3)
    out = rewrite.edit([a, b])[0]
    assert len(_pages(out)) == 5
    assert ["Page 2" in t for t in _texts(out)] == [False, True, False, True, False]
    assert _outline(out) == [("Mark 1", 1), ("Mark 2", 2), ("Mark 1", 3), ("Mark 2", 4), ("Mark 3", 5)]
    plan = rewrite.edit([a, b], pages=[(4, 0), (0, 0)])[0]  # pages counted across both
    assert ["Page 3" in t for t in _texts(plan)] == [True, False]


def test_split_with_only_each_part_s_own_objects():
    big_font = stream(b"\x00" * 200_000, b"/Length1 200000")  # a font file only page 3 uses
    extra = {401: big_font, 102: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 202 0 R "
                                  b"/Resources << /Font << /F1 5 0 R /F2 << /Type /Font /FontFile2 401 0 R >> >> >> >>"),
             202: stream(b"BT /F2 12 Tf 40 360 Td (Page 3) Tj ET")}  # page 3 draws with it
    parts = rewrite.edit([_numbered(5, extra=extra)], split=2)
    assert [len(_pages(p)) for p in parts] == [2, 2, 1]
    assert [_outline(p) for p in parts] == [[("Mark 1", 1), ("Mark 2", 2)], [("Mark 3", 1), ("Mark 4", 2)],
                                           [("Mark 5", 1)]]
    assert len(parts[1]) > 200_000 > len(parts[0])  # the big font went only where its page is


def test_compact_files_have_object_streams_and_open_everywhere():
    out = rewrite.edit([_numbered(30)])[0]
    assert b"/ObjStm" in out and b"/XRef" in out
    assert len(_pages(out)) == 30


@needs_pymupdf
def test_pymupdf_reads_the_results(book, tmp_path):
    path = tmp_path / "b.pdf"
    book.write(path)
    data = path.read_bytes()
    with pymupdf.open(stream=data) as d:
        pages, toc = d.page_count, d.get_toc()
    out = rewrite.edit([data, data], pages=None)[0]
    with pymupdf.open(stream=out) as d:
        assert d.page_count == 2 * pages and len(d.get_toc()) == 2 * len(toc)
        assert d.metadata["title"] == "The Sample Book"
        assert "cabbage" in "".join(p.get_text() for p in d)  # still text, not pictures
    turned = rewrite.edit([data], pages=[(0, 1)])[0]
    with pymupdf.open(stream=turned) as d:
        assert d.page_count == 1 and d[0].rotation == 90


def _photo_pdf(size=(1700, 2200), page=(612, 792), quality=95, mask=False) -> bytes:
    from PIL import Image
    img = Image.radial_gradient("L").resize(size).convert("RGB")
    img.paste((200, 60, 40), (size[0] // 4, size[1] // 4, size[0] // 2, size[1] // 2))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    jpeg = buf.getvalue()
    sm = b""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents 4 0 R /Resources << /XObject << /Im 6 0 R >> >> >>"
               % page,
            4: stream(b"q %d 0 0 %d 0 0 cm /Im Do Q" % page), 9: BASE[9]}
    if mask:
        alpha = zlib.compress(Image.linear_gradient("L").resize(size).tobytes())
        objs[7] = stream(alpha, b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                                b"/BitsPerComponent 8 /Filter /FlateDecode" % size)
        sm = b"/SMask 7 0 R"
    objs[6] = stream(jpeg, b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                           b"/BitsPerComponent 8 /Filter /DCTDecode %s" % (*size, sm))
    return make_pdf(objs)


def _image(data: bytes, name="Im"):
    doc = parser.Document(data)
    res = doc.resolve(doc.pages()[0]["Resources"])
    return doc, doc.resolve(doc.resolve(res["XObject"])[name])


def test_compress_saves_pictures_at_the_resolution_asked():
    pytest.importorskip("PIL")
    src = _photo_pdf()  # 1700 x 2200 on a letter page: 200 dpi
    small = rewrite.compress(src, dpi=150)
    _, img = _image(small)
    assert (img.dict["Width"], img.dict["Height"]) == (1275, 1650)  # the page's 11 inches at 150 dpi, its shape kept
    assert len(small) < len(src) / 3
    same = rewrite.compress(src, dpi=300)  # already below that: kept as it is unless it saves much
    _, img = _image(same)
    assert img.dict["Width"] in (1700,)


def test_compress_keeps_a_soft_mask_lossless():
    pytest.importorskip("PIL")
    small = rewrite.compress(_photo_pdf(mask=True), dpi=100)
    doc, img = _image(small)
    mask = doc.resolve(img.dict["SMask"])
    assert str(mask.dict["Filter"]) == "FlateDecode" and str(img.dict["Filter"]) == "DCTDecode"


def test_the_same_picture_twice_is_stored_once():
    pytest.importorskip("PIL")
    src = _photo_pdf()
    doc = parser.Document(src)
    raw = doc.get(6).raw
    twin = stream(raw, b"/Type /XObject /Subtype /Image /Width 1700 /Height 2200 /ColorSpace /DeviceRGB "
                       b"/BitsPerComponent 8 /Filter /DCTDecode")
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /XObject << /A 6 0 R /B 8 0 R >> >> >>",
            4: stream(b"/A Do /B Do"), 6: twin, 8: twin, 9: BASE[9]}
    out = rewrite.compress(make_pdf(objs), dpi=300)
    doc, _ = _image(out, "A")
    res = doc.resolve(doc.pages()[0]["Resources"])
    xo = doc.resolve(res["XObject"])
    assert xo["A"].num == xo["B"].num  # one copy, both names pointing at it
    assert len(out) < len(raw) * 1.5


def test_wrong_requests_and_protected_pdfs_are_said_plainly():
    src = _numbered(3)
    with pytest.raises(BookportError, match="There's no page 5: this PDF has 3 pages"):
        rewrite.edit([src], pages=[(4, 0)])
    with pytest.raises(BookportError, match="No pages left"):
        rewrite.edit([src], pages=[])
    objs = dict(BASE)
    objs[4] = stream(_content([b"Open, but with permissions."]))
    enc, fid = _rc4_encrypt_pdf(objs, user=b"letmein")
    locked = make_pdf(enc, trailer_extra=b"/Encrypt 8 0 R /ID [<%s> <%s>]" % (fid.hex().encode(), fid.hex().encode()))
    with pytest.raises(BookportError, match="password"):
        rewrite.edit([locked])  # a real password is never got around by rewriting it
    for junk in (b"%PDF-1.4\n" + b"\x00" * 50, b"not a pdf", src[: len(src) // 3]):
        try:
            rewrite.edit([junk])
        except BookportError:
            pass  # a damaged file is said in words; nothing else may come out of it


def test_a_bookmark_by_name_follows_its_page():
    names = b"/Names << /Dests << /Names [(two) [101 0 R /Fit]] >> >>"
    extra = {300: b"<< /Title (Named) /Parent 50 0 R /Dest (two) >>",
             50: b"<< /Type /Outlines /First 300 0 R /Last 300 0 R /Count 1 >>"}
    out = rewrite.edit([_numbered(3, extra_catalog=names, extra=extra)], pages=[(2, 0), (1, 0)])[0]
    assert _outline(out) == [("Named", 2)]


# ---- an invisible text layer (what OCR read on a scanned page)

WORDS = [[("Hello", 50.0, 100.0, 120.0, 120.0), ("łódź", 130.0, 100.0, 190.0, 120.0)]]


def _layer(data: bytes, index: int) -> tuple[dict, list[bytes]]:
    """A page's resources and its content streams, decoded, by bookport's own reader."""
    doc = parser.Document(data)
    page = doc.pages()[index]
    contents = doc.resolve(page.get("Contents"))
    parts = contents if isinstance(contents, list) else [contents]
    return doc.resolve(page.get("Resources")), [doc.decode(doc.resolve(p)) for p in parts]


def test_text_is_laid_over_a_page_unseen_and_the_page_is_drawn_as_before():
    out = rewrite.edit([_numbered(2)], text={1: WORDS})[0]
    res, streams = _layer(out, 1)
    assert len(streams) == 3 and streams[0] == b"q\n" and b"Page 2" in streams[1]  # the page as it was, kept whole
    assert streams[2].startswith(b"Q\nq\n1 0 0 1 0 0 cm\nBT 3 Tr")                 # then the words, in render mode 3
    doc = parser.Document(out)
    fonts = doc.resolve(res["Font"])
    assert sorted(fonts) == ["F1", "OCR0"]  # the page's own font, and the layer's
    other, first = _layer(out, 0)
    assert sorted(doc.resolve(other["Font"])) == ["F1"] and len(first) == 1  # the page without text is as it was


def test_the_layer_follows_the_pages_own_turn_and_crop():
    """Words are given on the page as it shows: a page turned a quarter (/Rotate 90) or cropped gets the matrix
    that puts them back on the page's own space."""
    turned = _numbered(1, extra={100: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Rotate 90 "
                                      b"/Contents 200 0 R /Resources << /Font << /F1 5 0 R >> >> >>"})
    _, streams = _layer(rewrite.edit([turned], text={0: WORDS})[0], 0)
    assert b"0 1 -1 0 300 0 cm" in streams[-1]
    cropped = _numbered(1, extra={100: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /CropBox [10 20 290 380] "
                                       b"/Contents 200 0 R /Resources << /Font << /F1 5 0 R >> >> >>"})
    _, streams = _layer(rewrite.edit([cropped], text={0: WORDS})[0], 0)
    assert b"1 0 0 1 10 20 cm" in streams[-1]
    # turning the page with pages= turns the words with it: they stay where they lie on its own space
    _, streams = _layer(rewrite.edit([turned], pages=[(0, 1)], text={0: WORDS})[0], 0)
    assert b"0 1 -1 0 300 0 cm" in streams[-1]


def test_a_layer_goes_with_its_page_through_a_split_and_compress():
    parts = rewrite.edit([_numbered(3)], split=1, dpi=150, text={2: WORDS})
    assert [b"/OCR0" in b"".join(_layer(p, 0)[1]) for p in parts] == [False, False, True]


@needs_pymupdf
def test_another_reader_finds_the_words_where_they_were_put():
    out = rewrite.edit([_numbered(1)], text={0: WORDS})[0]
    with pymupdf.open(stream=out) as d:
        page = d[0]
        text = page.get_text()
        words = {w[4]: w[:4] for w in page.get_text("words")}
    assert "Page 1" in text and "Hello" in text and "łódź" in text  # its own text, and the layer's, every letter
    x0, y0, x1, y1 = words["Hello"]
    # PyMuPDF counts from the top, and boxes a line by Helvetica's ascent: a little taller than the box given
    assert abs(x0 - 50) < 2 and abs(x1 - 120) < 3 and 400 - 130 < y0 < y1 < 400 - 95


# -- permissions-only PDFs (they open with no password, but may not be edited or printed)

def _permissions_only(n: int = 3, method: str = "PDF_ENCRYPT_AES_256", perms: int | None = None, user: str = ""):
    """_numbered(n), locked by PyMuPDF with the permissions asked (print and copy by default)."""
    d = pymupdf.open(stream=_numbered(n), filetype="pdf")
    if perms is None:
        perms = pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_COPY
    data = d.write(encryption=getattr(pymupdf, method), owner_pw="owner", user_pw=user, permissions=perms)
    d.close()
    return data


def _p(data: bytes) -> int:
    return parser.Document(data).crypt.permissions & 0xFFFFFFFF


@needs_pymupdf
@pytest.mark.parametrize("method", ["PDF_ENCRYPT_AES_256", "PDF_ENCRYPT_AES_128", "PDF_ENCRYPT_RC4_128"])
def test_permissions_only_pdf_is_split_joined_and_locked_again(method):
    src = _permissions_only(3, method)
    assert _p(src) != 0xFFFFFFFC and parser.Document(src).crypt is not None
    # split: one locked PDF a page, each reopening with the empty password through bookport's own reader
    parts = rewrite.edit([src], split=1)
    assert len(parts) == 3
    for i, part in enumerate(parts):
        crypt = parser.Document(part).crypt
        assert (crypt.r, crypt.v, crypt.aes) == (6, 5, True)  # AES-256, the standard handler's revision 6
        assert _p(part) == _p(src)  # the same /P
        assert _texts(part)[0].count(f"Page {i + 1}") == 1
    # join: pages of both, the same lock
    joined = rewrite.edit([src, src])[0]
    assert len(_pages(joined)) == 6 and _p(joined) == _p(src)
    assert [t.count("Page") for t in _texts(joined)] == [1] * 6
    # compress and edit go through the same door
    assert _p(rewrite.compress(src, dpi=100)) == _p(src)
    assert len(_pages(rewrite.edit([src], pages=[(2, 0), (0, 1)])[0])) == 2


def test_hand_made_rc4_permissions_only_pdf_is_rewritten():
    objs = dict(BASE)
    objs[4] = stream(_content([b"Open, but with permissions."]))
    enc, fid = _rc4_encrypt_pdf(objs)  # /P -4: anything allowed, an owner password set
    src = make_pdf(enc, trailer_extra=b"/Encrypt 8 0 R /ID [<%s> <%s>]" % (fid.hex().encode(), fid.hex().encode()))
    out = rewrite.edit([src])[0]
    assert "Open, but with permissions." in _texts(out)[0]
    assert _p(out) == _p(src) == 0xFFFFFFFC
    assert b"Open, but with" not in out  # the content is not stored in the clear


@needs_pymupdf
def test_joined_permissions_are_the_fewest():
    a = _permissions_only(1, perms=pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_COPY)
    b = _permissions_only(1, perms=pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ANNOTATE)
    plain = _numbered(1)
    out = rewrite.edit([a, b, plain])[0]
    assert _p(out) == _p(a) & _p(b)
    assert _p(out) & ~_p(a) == 0 and _p(out) & ~_p(b) == 0
    # a plain PDF joined with a locked one: the result is locked
    assert parser.Document(rewrite.edit([plain, a])[0]).crypt is not None
    # nothing locked: nothing locked
    assert parser.Document(rewrite.edit([plain, plain])[0]).crypt is None


@needs_pymupdf
def test_rewritten_locked_pdf_opens_and_renders_elsewhere():
    src = _permissions_only(2)
    out = rewrite.edit([src])[0]
    with pymupdf.open(stream=out, filetype="pdf") as d:  # an independent reader, empty password
        assert not d.needs_pass and d.page_count == 2
        assert d.permissions & 0xFFFFFFFF == _p(src) or d.permissions == parser.Document(src).crypt.permissions
        assert "Page 1" in d[0].get_text()
        pix = d[0].get_pixmap(dpi=40)
        assert min(pix.samples) < 128  # the text is drawn: not a blank page
    try:
        import pypdfium2
    except ImportError:
        return
    page = pypdfium2.PdfDocument(out)[0]
    assert min(page.render(scale=0.5).to_pil().convert("L").tobytes()) < 128


@needs_pymupdf
def test_real_password_pdf_is_still_refused():
    src = _permissions_only(2, user="secret")
    with pytest.raises(BookportError):
        rewrite.edit([src])
    with pytest.raises(BookportError):
        rewrite.edit([_numbered(1), src])
    with pytest.raises(BookportError):
        rewrite.compress(src)
