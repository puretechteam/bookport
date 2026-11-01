"""PDF: the typesetter's output, and the reader on PDFs made by bookport, by hand and by others."""

import hashlib
import io
import os
import re
import struct
import zlib

import pytest

import bookport
from bookport.pdf import crypt, parser
from bookport.pdf.typeset import PAGE_SIZES, typeset

from .conftest import EBOOK_CONVERT, needs_calibre, recall, run, toc_titles

try:
    import pymupdf
except ImportError:  # optional: an independent reader to check our output against
    pymupdf = None
needs_pymupdf = pytest.mark.skipif(pymupdf is None, reason="PyMuPDF isn't installed")


def test_pdf_round_trip(book, tmp_path):
    out = tmp_path / "b.pdf"
    book.write(out)
    data = out.read_bytes()
    assert data.startswith(b"%PDF-1.7") and data.rstrip().endswith(b"%%EOF")
    back = bookport.read(out)
    assert recall(book, back) > 0.98  # a page has no place for alt text
    assert toc_titles(back) == toc_titles(book)
    assert back.metadata.title == "The Sample Book"
    assert back.metadata.authors == ["Ada Writer", "Bo Second"]
    assert back.cover is not None and len(back.images()) >= 2


def test_fonts_are_embedded_subsets(book):
    data = typeset(book)
    doc = parser.Document(data)
    fonts = set()
    for obj_num in doc.xref:
        o = doc.get(obj_num)
        if isinstance(o, dict) and o.get("Type") == "FontDescriptor":
            assert "FontFile2" in o
            name = str(o["FontName"])
            assert re.fullmatch(r"[A-Z]{6}\+\S+", name)
            fonts.add(name.split("+")[1])
    assert "NotoSerif-Regular" in fonts and "NotoSerif-Bold" in fonts and "NotoSerif-Italic" in fonts
    # subsets are small: far below the full font files
    sizes = [len(doc.decode(doc.get(n))) for n in doc.xref if isinstance(doc.get(n), parser.Stream)
             and "Length1" in doc.get(n).dict]
    assert sizes and max(sizes) < 60_000


@pytest.mark.parametrize("size", list(PAGE_SIZES))
def test_page_sizes(book, size):
    doc = parser.Document(typeset(book, size))
    page = doc.pages()[0]
    w, h = PAGE_SIZES[size]
    box = [float(v) for v in page["MediaBox"]]
    assert abs(box[2] - w) < 0.5 and abs(box[3] - h) < 0.5


def test_text_size_changes_layout(book):
    import xml.etree.ElementTree as ET

    from bookport import markup
    root, body = markup.new_document("Long", "en")
    for i in range(200):
        ET.SubElement(body, "p").text = f"Paragraph {i}, long enough to wrap across a few lines of text on a small page."
    book.add_document("text/long.xhtml", root)
    counts = {ts: len(parser.Document(typeset(book, "ereader", ts)).pages()) for ts in ("small", "normal", "large")}
    assert counts["small"] < counts["normal"] < counts["large"]


def test_bad_options(book, tmp_path):
    with pytest.raises(bookport.BookportError):
        book.write(tmp_path / "x.pdf", page_size="tabloid")
    with pytest.raises(bookport.BookportError):
        book.write(tmp_path / "x.pdf", text_size="huge")


@needs_pymupdf
def test_pymupdf_reads_our_pdf(book, tmp_path):
    out = tmp_path / "b.pdf"
    book.write(out, page_size="a5")
    with pymupdf.open(out) as d:
        text = "".join(p.get_text() for p in d)
        for word in ("bright", "Winston", "cabbage", "Ann", "notes"):
            assert word in text
        toc = d.get_toc()
        assert [t[1] for t in toc] == toc_titles(book)
        assert d.metadata["title"] == "The Sample Book"
        links = [link for p in d for link in p.get_links()]
        assert any(link.get("kind") == pymupdf.LINK_NAMED or link.get("page", -1) >= 0 for link in links)


@needs_calibre
def test_calibre_reads_our_pdf(book, tmp_path):
    out = tmp_path / "b.pdf"
    book.write(out)
    txt = tmp_path / "b.txt"
    run(EBOOK_CONVERT, out, txt)
    text = txt.read_text("utf-8", "replace")
    for word in ("bright", "Winston", "cabbage", "notes"):
        assert word in text


# --- the reader on hand-made PDFs ---------------------------------------------------------------

def make_pdf(objects: dict[int, bytes], root: int = 1, xref_stream=False, trailer_extra=b"") -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"
    size = max(objects) + 1
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += b"%010d 00000 n \n" % offsets[num] if num in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info 9 0 R %s >>\nstartxref\n%d\n%%%%EOF\n" % (size, root, trailer_extra, start)
    return bytes(out)


def stream(content: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s >>\nstream\n" % (len(content), extra) + content + b"\nendstream"


BASE = {
    1: b"<< /Type /Catalog /Pages 2 0 R >>",
    2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    9: b"<< /Title (Hand Made) /Author (A. Maker) >>",
}


def _content(lines):
    ops = [b"BT /F1 12 Tf 40 360 Td 14 TL"]
    for line in lines:
        ops.append(b"(" + line + b") Tj T*")
    ops.append(b"ET")
    return b"\n".join(ops)


def test_reader_simple(tmp_path):
    objs = dict(BASE)
    objs[4] = stream(_content([b"Hello world, caf\\351 ", b"and a second line."]))
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(objs))
    b = bookport.read(p)
    assert "Hello world, café and a second line." in b.text()
    assert b.metadata.title == "Hand Made" and b.metadata.authors == ["A. Maker"]


def test_reader_filters(tmp_path):
    text = _content([b"Filtered text survives."])
    a85 = b"<~" + _a85(zlib.compress(text)) + b"~>"
    objs = dict(BASE)
    objs[4] = stream(a85, b"/Filter [/ASCII85Decode /FlateDecode]")
    p = tmp_path / "f.pdf"
    p.write_bytes(make_pdf(objs))
    assert "Filtered text survives." in bookport.read(p).text()
    assert parser._lzw(bytes.fromhex("800B6050220C0C8501")) == b"-----A---B"  # the example in ISO 32000-1, 7.4.4.2
    assert parser._rl(b"\x02abc\xfdz\x80") == b"abczzzz"
    assert parser._ahx(b"48 65 6c6c6f>") == b"Hello"


def _a85(data: bytes) -> bytes:
    import base64
    return base64.a85encode(data)


def test_reader_repairs_broken_xref(tmp_path):
    objs = dict(BASE)
    objs[4] = stream(_content([b"Still readable."]))
    data = make_pdf(objs)
    data = re.sub(rb"startxref\n\d+", b"startxref\n999999", data)
    data = re.sub(rb"\d{10} 00000 n", b"0000000001 00000 n", data)
    p = tmp_path / "broken.pdf"
    p.write_bytes(data)
    assert "Still readable." in bookport.read(p).text()


def test_reader_object_and_xref_streams(tmp_path):
    # objects 3 and 5 live in an object stream; the cross-reference table is itself a stream
    content = _content([b"From an object stream."])
    obj3 = BASE[3]
    obj5 = BASE[5]
    head = b"3 0 5 %d " % (len(obj3) + 1)
    objstm_body = head + obj3 + b" " + obj5
    pieces = {1: BASE[1], 2: BASE[2], 4: stream(content), 9: BASE[9],
              6: stream(zlib.compress(objstm_body), b"/Type /ObjStm /N 2 /First %d /Filter /FlateDecode" % len(head))}
    out = bytearray(b"%PDF-1.5\n")
    offsets = {}
    for num in sorted(pieces):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + pieces[num] + b"\nendobj\n"
    rows = [b"\x00" + struct.pack(">I", 0) + b"\xff"]
    for num in range(1, 8):
        if num in (3, 5):
            rows.append(b"\x02" + struct.pack(">I", 6) + bytes([0 if num == 3 else 1]))
        elif num in offsets:
            rows.append(b"\x01" + struct.pack(">I", offsets[num]) + b"\x00")
        else:
            rows.append(b"\x00" + struct.pack(">I", 0) + b"\x00")
    xref_off = len(out)
    rows.append(b"\x01" + struct.pack(">I", xref_off) + b"\x00")
    body = zlib.compress(b"".join(rows))
    out += b"7 0 obj\n" + stream(body, b"/Type /XRef /Size 8 /W [1 4 1] /Root 1 0 R /Info 9 0 R /Filter /FlateDecode") + b"\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref_off
    p = tmp_path / "objstm.pdf"
    p.write_bytes(bytes(out))
    assert "From an object stream." in bookport.read(p).text()


def _rc4_encrypt_pdf(objs, user=b"", owner=b"owner", revision=3):
    """Encrypt string-free content streams with the standard handler, revision 2 or 3 (RC4)."""
    file_id = hashlib.md5(b"bookport test").digest()
    length = 16 if revision == 3 else 5
    pad = crypt.PAD
    # O (algorithm 3)
    okey = hashlib.md5((owner + pad)[:32]).digest()
    if revision == 3:
        for _ in range(50):
            okey = hashlib.md5(okey).digest()
    okey = okey[:length]
    o = crypt.rc4(okey, (user + pad)[:32])
    if revision == 3:
        for i in range(1, 20):
            o = crypt.rc4(bytes(b ^ i for b in okey), o)
    p_val = -4
    key = hashlib.md5((user + pad)[:32] + o + struct.pack("<i", p_val) + file_id).digest()
    if revision == 3:
        for _ in range(50):
            key = hashlib.md5(key[:length]).digest()
    key = key[:length]
    if revision == 2:
        u = crypt.rc4(key, pad)
    else:
        u = crypt.rc4(key, hashlib.md5(pad + file_id).digest())
        for i in range(1, 20):
            u = crypt.rc4(bytes(b ^ i for b in key), u)
        u += b"\0" * 16
    out = {}
    for num, body in objs.items():
        m = re.match(rb"(<< /Length )(\d+)( .*?>>\nstream\n)(.*)(\nendstream)$", body, re.DOTALL)
        if m:
            okey_obj = hashlib.md5(key + struct.pack("<I", num)[:3] + b"\0\0").digest()[:min(length + 5, 16)]
            data = crypt.rc4(okey_obj, m.group(4))
            body = m.group(1) + str(len(data)).encode() + m.group(3) + data + m.group(5)
        out[num] = body
    out[8] = (b"<< /Filter /Standard /V %d /R %d /Length %d /P %d /O <%s> /U <%s> >>"
              % (2 if revision == 3 else 1, revision, length * 8, p_val, o.hex().encode(), u.hex().encode()))
    return out, file_id


@pytest.mark.parametrize("revision", [2, 3])
def test_reader_rc4_empty_user_password(tmp_path, revision):
    objs = dict(BASE)
    objs[4] = stream(_content([b"Secret but open."]))
    enc, fid = _rc4_encrypt_pdf(objs, revision=revision)
    p = tmp_path / "enc.pdf"
    p.write_bytes(make_pdf(enc, trailer_extra=b"/Encrypt 8 0 R /ID [<%s> <%s>]" % (fid.hex().encode(), fid.hex().encode())))
    assert "Secret but open." in bookport.read(p).text()


def test_reader_refuses_password(tmp_path):
    objs = dict(BASE)
    objs[4] = stream(_content([b"Locked."]))
    enc, fid = _rc4_encrypt_pdf(objs, user=b"letmein")
    p = tmp_path / "locked.pdf"
    p.write_bytes(make_pdf(enc, trailer_extra=b"/Encrypt 8 0 R /ID [<%s> <%s>]" % (fid.hex().encode(), fid.hex().encode())))
    with pytest.raises(bookport.DRMError):
        bookport.read(p)


@needs_pymupdf
@pytest.mark.parametrize("method", ["PDF_ENCRYPT_AES_128", "PDF_ENCRYPT_AES_256", "PDF_ENCRYPT_RC4_128"])
def test_reader_pymupdf_encryption(book, tmp_path, method):
    src = tmp_path / "plain.pdf"
    book.write(src)
    enc = tmp_path / "enc.pdf"
    with pymupdf.open(src) as d:
        d.save(enc, encryption=getattr(pymupdf, method), owner_pw="owner", user_pw="", permissions=0)
    assert recall(book, bookport.read(enc)) > 0.98
    locked = tmp_path / "locked.pdf"
    with pymupdf.open(src) as d:
        d.save(locked, encryption=getattr(pymupdf, method), owner_pw="owner", user_pw="user")
    with pytest.raises(bookport.DRMError):
        bookport.read(locked)


@needs_pymupdf
def test_reader_object_streams_from_pymupdf(book, tmp_path):
    src = tmp_path / "plain.pdf"
    book.write(src)
    packed = tmp_path / "packed.pdf"
    with pymupdf.open(src) as d:
        d.save(packed, garbage=4, deflate=True, use_objstms=1)
    back = bookport.read(packed)
    assert recall(book, back) > 0.98  # a page has no place for alt text
    assert toc_titles(back) == toc_titles(book)


def test_hyphen_rules():
    from bookport.pdf.reader import HYPHEN_JOIN, resolve_hyphens
    items = [["p", [("a remark-" + HYPHEN_JOIN, False, False, False), ("able thing, quite remarkable", False, False, False)], 0],
             ["p", [("the waistcoat-" + HYPHEN_JOIN + "pocket", False, False, False)], 0]]
    resolve_hyphens(items)
    assert items[0][1][0][0] + items[0][1][1][0] == "a remarkable thing, quite remarkable"
    assert items[1][1][0][0] == "the waistcoat-pocket"


def test_aes_vectors():
    a = crypt.AES(bytes(range(16)))
    assert a.encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff")).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    b = crypt.AES(bytes(range(32)))
    assert b.encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff")).hex() == "8ea2b7ca516745bfeafc49904b496089"
    assert b.decrypt_block(bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")).hex() == "00112233445566778899aabbccddeeff"


def test_reader_rejects_garbage(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4\nthis is not really a pdf at all\n%%EOF")
    with pytest.raises(bookport.BookportError):
        bookport.read(p)


def test_lines_with_numbers_inside_words_are_kept(tmp_path):
    # Every line here reads "word# word# ..." once digits are masked, the way running headers are found.
    # Only whole numbers (page numbers, "Chapter 12") may be masked, or a line at each page edge is lost.
    import random
    rnd = random.Random(7)
    paras = "".join("<p>" + " ".join(f"word{rnd.randrange(4000)}" for _ in range(rnd.randrange(30, 120))) + ".</p>"
                    for _ in range(300))
    src = tmp_path / "numbered.html"
    src.write_text(f"<html><head><title>Numbered</title></head><body>{paras}</body></html>", encoding="utf-8")
    book = bookport.read(src)
    out = tmp_path / "numbered.pdf"
    book.write(out)
    assert bookport.read(out).word_count() == book.word_count()


@needs_pymupdf
def test_running_headers_and_page_numbers_are_dropped(tmp_path):
    doc = pymupdf.open()
    for n in range(1, 9):
        page = doc.new_page(width=420, height=595)
        page.insert_text((40, 40), "Annual Report 2024", fontsize=9)
        for k in range(30):
            page.insert_text((40, 80 + k * 15), f"Body line {k} of page {n} with the year 1984 in it.", fontsize=10)
        page.insert_text((190, 570), str(n), fontsize=9)
    out = tmp_path / "report.pdf"
    doc.save(out)
    text = bookport.read(out).text()
    assert "Annual Report" not in text
    assert not re.search(r"(?m)^\s*\d+\s*$", text)
    assert text.count("Body line") == 8 * 30


def test_fallback_fonts_from_the_environment(tmp_path, monkeypatch):
    # The bundled serif has no arrows or maths symbols; BOOKPORT_FALLBACK_FONTS names fonts that do
    # (on Linux, where the Windows fallbacks don't exist, this is how CJK fonts are given).

    from bookport.pdf import typeset as ts
    src = tmp_path / "symbols.html"
    src.write_text("<html><body><p>Arrows ← → and ∀x ∃y.</p></body></html>", encoding="utf-8")
    book = bookport.read(src)
    monkeypatch.setattr(ts, "SYSTEM_FALLBACKS", [])
    font = tmp_path / "fonts" / "mono.ttf"
    font.parent.mkdir()
    font.write_bytes(ts.font_bytes("mono"))
    monkeypatch.setenv("BOOKPORT_FALLBACK_FONTS", os.pathsep.join([str(tmp_path / "missing.ttf"), str(font)]))
    out = tmp_path / "symbols.pdf"
    book.write(out)
    assert "← → and ∀x ∃y" in bookport.read(out).text()


def test_characters_no_font_has_read_back_as_unknown(tmp_path, monkeypatch):
    # They are drawn with the font's empty glyph 0; its text was whichever missing character came first.
    from bookport.pdf import typeset as ts
    src = tmp_path / "symbols.html"
    src.write_text("<html><body><p>Arrows ← → and ∀x ∃y.</p></body></html>", encoding="utf-8")
    monkeypatch.setattr(ts, "SYSTEM_FALLBACKS", [])
    monkeypatch.delenv("BOOKPORT_FALLBACK_FONTS", raising=False)
    out = tmp_path / "symbols.pdf"
    bookport.read(src).write(out)
    assert "Arrows \ufffd \ufffd and \ufffdx \ufffdy." in bookport.read(out).text()


def test_a_word_that_just_fits_is_not_split():
    # a table column is exactly as wide as its longest word, give or take a rounding error
    from bookport.pdf.typeset import Item, Typesetter
    t = Typesetter(bookport.Book())
    slot = t.fonts.get("bold")
    w = slot.font.width("PAGE", 9.9)
    for avail in (w, w - 1e-12, w * (1 - 1e-15)):
        lines = t.lines([Item("text", "PAGE", slot, 9.9, w)], avail)
        assert [[it.text for it in ln.items] for ln in lines] == [["PAGE"]], avail


def test_kerning_comes_from_the_fonts_own_table():
    # values from Noto Serif's GPOS 'kern' feature (all 8,836 printable ASCII pairs of the four serif
    # styles were checked against fontTools when this was written)
    from bookport.pdf import ttf
    from bookport.pdf.typeset import font_bytes
    serif = ttf.load(font_bytes("regular"))
    pairs = {p: serif.kerning().pair(serif.glyph(p[0]), serif.glyph(p[1])) for p in ("AV", "To", "r.", "ab", "f)")}
    assert pairs == {"AV": -80, "To": -50, "r.": -130, "ab": 0, "f)": 90}
    assert serif.width("AV", 10) == pytest.approx(serif.width("A", 10) + serif.width("V", 10) - 0.8)
    mono = ttf.load(font_bytes("mono"))
    assert mono.width("AV", 10) == mono.width("A", 10) + mono.width("V", 10)  # fixed pitch: never kerned


@needs_pymupdf
def test_kerned_glyphs_sit_where_the_layout_put_them(tmp_path):
    from bookport.pdf import ttf
    from bookport.pdf.typeset import font_bytes
    src = tmp_path / "k.html"
    src.write_text("<html><body><p>AVATAR Tower</p></body></html>", encoding="utf-8")
    out = tmp_path / "k.pdf"
    bookport.read(src).write(out)
    serif = ttf.load(font_bytes("regular"))
    chars = [c for b in pymupdf.open(out)[0].get_text("rawdict")["blocks"] for ln in b.get("lines", [])
             for s in ln["spans"] for c in s["chars"]]
    size = next(s["size"] for b in pymupdf.open(out)[0].get_text("rawdict")["blocks"] for ln in b.get("lines", [])
                for s in ln["spans"])
    text = "".join(c["c"] for c in chars)
    start = text.index("AVATAR")
    for k in range(start, start + 5):
        step = chars[k + 1]["origin"][0] - chars[k]["origin"][0]
        assert step == pytest.approx(serif.width(text[k:k + 2], size) - serif.width(text[k + 1], size), abs=0.02), text[k:k + 2]


def _fonttools_pair(subtables, first: str, second: str) -> int:
    """The first kern subtable's XAdvance for (first, second) glyph names, read by fontTools."""
    for st in subtables:
        if first not in st.Coverage.glyphs:
            continue
        if st.Format == 1:
            pairs = st.PairSet[st.Coverage.glyphs.index(first)].PairValueRecord
            rec = next((r for r in pairs if r.SecondGlyph == second), None)
            if rec is None:
                continue
            return getattr(rec.Value1, "XAdvance", 0) or 0
        c1 = st.ClassDef1.classDefs.get(first, 0)
        c2 = st.ClassDef2.classDefs.get(second, 0)
        return getattr(st.Class1Record[c1].Class2Record[c2].Value1, "XAdvance", 0) or 0
    return 0


@pytest.mark.parametrize("style", ["regular", "bold", "italic", "bolditalic"])
def test_kerning_matches_fonttools_for_every_ascii_pair(style):
    fonttools = pytest.importorskip("fontTools.ttLib")
    from bookport.pdf import ttf
    from bookport.pdf.typeset import font_bytes
    data = font_bytes(style)
    ours = ttf.load(data)
    font = fonttools.TTFont(io.BytesIO(data))
    gpos = font["GPOS"].table
    lookups = sorted({i for fr in gpos.FeatureList.FeatureRecord if fr.FeatureTag == "kern"
                      for i in fr.Feature.LookupListIndex})
    subtables = []
    for i in lookups:
        lookup = gpos.LookupList.Lookup[i]
        for st in lookup.SubTable:
            st = st.ExtSubTable if lookup.LookupType == 9 else st
            if getattr(st, "ValueFormat1", 0) & 4:  # only those that move the first glyph's advance
                subtables.append(st)
    cmap = font.getBestCmap()
    chars = [chr(c) for c in range(33, 127)]
    differ, checked = [], 0
    for a in chars:
        for b in chars:
            want = _fonttools_pair(subtables, cmap[ord(a)], cmap[ord(b)])
            got = ours.kerning().pair(ours.glyph(a), ours.glyph(b))
            checked += 1
            if got != want:
                differ.append((a + b, got, want))
    assert checked == 8836
    assert differ == []
