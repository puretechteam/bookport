"""DjVu: decoding against DjVuLibre's own output (hashes of ddjvu's pixels and djvutxt's text,
taken from DjVuLibre 3.5.29 for the samples in tests/djvu/), PDF output read back with PyMuPDF,
the Book reader, the codecs one by one, and damaged and hostile files.

The samples were made with DjVuLibre's encoders (cjb2, c44, cpaldjvu, djvumake, djvm, djvused)
from generated pictures. With bookport-native installed the tests run the compiled decoders;
BOOKPORT_PURE=1 runs them on the Python ones, and test_native_matches_python compares the two.
"""

from __future__ import annotations

import hashlib
import random
import struct
import time
from pathlib import Path

import pytest

import bookport
from bookport import BookportError, djvu
from bookport.djvu import _accel, anno, bzz, composite, iw44, jb2, jpeg, mmr, text
from bookport.djvu.doc import Component, Document

HERE = Path(__file__).parent / "djvu"
DJVULIBRE_DOCS = Path(r"C:\Program Files (x86)\DjVuLibre\doc")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


# (file, page): (mode, width, height, sha256 of ddjvu's pixels)
RENDERS = {
    ("bw_odd.djvu", 0): ("1", 1001, 777, "d786b67f884c3bd8"),
    ("book.djvu", 0): ("1", 850, 1100, "7447f4d0bede5d5e"),
    ("book.djvu", 1): ("1", 850, 1100, "7b9c80af48ce3369"),
    ("book.djvu", 2): ("RGB", 403, 301, "c7672762ba387edd"),
    ("book.djvu", 3): ("1", 850, 1100, "515709f463429310"),
    ("book.djvu", 4): ("1", 1100, 850, "5e42d04fdb8966de"),  # turned 90 degrees
    ("compound.djvu", 0): ("RGB", 900, 1200, "1a9ba0e8d305c9b7"),
    ("gray_prog.djvu", 0): ("L", 257, 190, "1d3818006835867e"),
    ("photo_half.djvu", 0): ("RGB", 403, 301, "5918f82e567c8803"),
    ("mmr.djvu", 0): ("1", 1001, 777, "d786b67f884c3bd8"),  # the same page as bw_odd, as a striped G4 mask
    ("text.djvu", 0): ("1", 1275, 1650, "04a092d8abdb78d9"),
    ("indirect/index.djvu", 0): ("1", 850, 1100, "7447f4d0bede5d5e"),
    ("indirect/index.djvu", 2): ("RGB", 403, 301, "c7672762ba387edd"),
    ("indirect/index.djvu", 4): ("1", 1100, 850, "5e42d04fdb8966de"),
}
TEXTS = {"book.djvu": "a160068c52b3dee5", "text.djvu": "9ba190bcfdd2c6ae", "indirect/index.djvu": "a160068c52b3dee5",
         "bw_odd.djvu": sha(b""), "compound.djvu": sha(b"")}
# DjVuLibre's own documents (a shared JB2 dictionary, FGbz palettes, FG44 and BG44 layers)
DOCS = {("djvu3spec.djvu", 0): ("1", "bf333edd6d95a269", "e87c92a051b6ad5a"),
        ("lizard2002.djvu", 0): ("RGB", "dde399819a24cca2", "b1ae676f0621a05d"),
        ("lizard2007.djvu", 1): ("RGB", "c443f4be3b34ae4d", "0e9f7fcf96be83f5"),
        ("djvulibre-book-ru.djvu", 1): ("RGB", "a3b9c7dfc5a88956", "abe1703e242845e2")}


@pytest.mark.parametrize("name,page", sorted(RENDERS))
def test_render_matches_djvulibre(name, page):
    mode, w, h, digest = RENDERS[(name, page)]
    pic = djvu.render(HERE / name, page)
    assert (pic.mode, pic.width, pic.height) == (mode, w, h)
    assert sha(pic.data) == digest


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_text_matches_djvutxt(name):
    assert sha(djvu.to_text(HERE / name).encode("utf-8")) == TEXTS[name]


@pytest.mark.skipif(not DJVULIBRE_DOCS.is_dir(), reason="DjVuLibre's documents aren't installed")
@pytest.mark.parametrize("name,page", sorted(DOCS))
def test_djvulibre_documents(name, page):
    mode, digest, text_digest = DOCS[(name, page)]
    pic = djvu.render(DJVULIBRE_DOCS / name, page)
    assert pic.mode == mode and sha(pic.data) == digest
    assert sha(djvu.to_text(DJVULIBRE_DOCS / name).encode("utf-8")) == text_digest


def test_detect_and_formats():
    assert bookport.detect(HERE / "book.djvu") == "djvu"
    assert bookport.formats()["djvu"] == {"name": "DjVu", "read": True, "write": False}
    assert bookport.formats()["djv"]["read"]


def test_structure():
    for name in ("book.djvu", "indirect/index.djvu"):
        doc = djvu.open(HERE / name)
        assert len(doc.pages) == 5
        assert [p.info.dpi for p in doc.pages] == [100] * 5
        assert [p.info.rotation for p in doc.pages] == [0, 0, 0, 0, 90]
        assert doc.bundled == (name == "book.djvu")
    assert len(djvu.open(HERE / "bw_odd.djvu").pages) == 1


def test_outline_and_annotations():
    doc = djvu.open(HERE / "book.djvu")
    marks = anno.outline(doc.outline_data)
    assert [m.title for m in marks] == ["Chapter one", "Chapter two \u00e9", "Web"]
    assert [m.title for m in marks[0].children] == ["Section 1.1", "A photo"]
    assert marks[1].children[0].children[0].title == "Deeper"
    assert [anno.target(doc, m.url) for m in marks] == [0, 3, None]
    assert anno.target(doc, "#+1", 2) == 3 and anno.target(doc, "#99") is None
    a = anno.page_annotations(doc.pages[0])
    assert a.metadata == {"author": "Pure Tech", "title": "A Test Book"}
    assert [(link.url, link.rect) for link in a.links] == [("http://example.com/x", (100, 100, 400, 160)),
                                                           ("#4", (100, 300, 300, 350))]


def test_text_zones():
    page = djvu.open(HERE / "text.djvu").pages[0]
    pt = text.page_text(page)
    kinds = []
    stack = [pt.zone]
    while stack:
        z = stack.pop()
        kinds.append(z.kind)
        stack.extend(z.children)
    assert set(kinds) == {text.PAGE, text.COLUMN, text.REGION, text.PARAGRAPH, text.LINE, text.WORD, text.CHARACTER}
    lines = text.lines(page)
    words = [w for line in lines for w in line]
    assert len(lines) == 35
    assert [w[0] for w in words] == djvu.to_text(HERE / "text.djvu").split()
    for _w, x0, y0, x1, y1 in words:
        assert 0 <= x0 < x1 <= 1275 and 0 <= y0 < y1 <= 1650


def test_to_pdf(tmp_path):
    fitz = pytest.importorskip("fitz")
    out = djvu.to_pdf(HERE / "book.djvu", tmp_path / "book.pdf")
    pdf = fitz.open(out)
    assert pdf.page_count == 5
    assert pdf[0].rect.width == pytest.approx(612, abs=0.01) and pdf[0].rect.height == pytest.approx(792, abs=0.01)  # 850 x 1100 pixels at 100 dpi
    assert pdf[4].rotation == 270  # DjVu turns counter-clockwise, PDF clockwise
    assert pdf.metadata["title"] == "A Test Book" and pdf.metadata["author"] == "Pure Tech"
    toc = [(level, title, page) for level, title, page, *_ in pdf.get_toc(simple=False)]
    assert toc[:4] == [(1, "Chapter one", 1), (2, "Section 1.1", 2), (2, "A photo", 3), (1, "Chapter two \u00e9", 4)]
    links = pdf[0].get_links()
    assert {link.get("uri") for link in links} >= {"http://example.com/x"}
    assert any(link.get("page") == 3 for link in links)
    # every word of the hidden text can be found, on its word
    ours = " ".join(pdf[i].get_text() for i in range(5)).split()
    assert ours == djvu.to_text(HERE / "book.djvu").split()
    # the mask comes back exactly at the page's own resolution
    pix = pdf[0].get_pixmap(matrix=fitz.Matrix(100 / 72, 100 / 72), colorspace=fitz.csGRAY)
    pic = djvu.render(HERE / "book.djvu", 0)
    nb = (pic.width + 7) // 8
    same = total = 0
    for y in range(0, pic.height, 3):
        for x in range(0, pic.width, 3):
            black = (pic.data[y * nb + x // 8] >> (7 - x % 8)) & 1
            same += black == (pix.samples[y * pix.stride + x] < 128)
            total += 1
    assert same / total > 0.999


@pytest.mark.parametrize("size", [(2550, 3301, 300), (1655, 2480, 300), (2421, 3469, 333), (1897, 3069, 600), (850, 1100, 100)])
@pytest.mark.parametrize("fit", [False, True], ids=["own resolution", "fitted to the width"])
def test_pdf_pixels_land_on_whole_pixels(monkeypatch, size, fit):
    """A page whose size doesn't come out in whole device pixels must not be stretched by a row,
    which blurs its text: such pages scored 22 dB against DjVuLibre's drawing of them."""
    pdfium = pytest.importorskip("pypdfium2")
    np = pytest.importorskip("numpy")
    from types import SimpleNamespace

    from bookport.djvu import doc as doc_module
    from bookport.djvu import topdf
    W, H, dpi = size
    stride = (W + 7) // 8
    rows = bytearray(stride * H)
    for y in range(0, H, 7):  # one-pixel lines of dots: a stretch of a row blurs them
        for x in range(0, W, 5):
            rows[y * stride + x // 8] |= 0x80 >> (x % 8)
    prep = topdf._Prepared()
    data, d = topdf._mask_image(bytes(rows), W, H)
    prep.images.append(("M0", data, d, None))
    prep.ops.append(b"q 0 g %d 0 0 %d 0 0 cm /M0 Do Q\n" % (W, H))
    monkeypatch.setattr(topdf, "_links", lambda *a: [])
    w = topdf.Writer()
    catalog, parent, ref = w.reserve(), w.reserve(), w.reserve()
    page = SimpleNamespace(info=doc_module.Info(width=W, height=H, dpi=dpi), index=0)
    fonts = topdf._Fonts(w)
    topdf._page(w, page, prep, ref, parent, fonts, None, [ref], [])
    fonts.finish()
    w.put(parent, {"Type": topdf.Name("Pages"), "Kids": [ref], "Count": 1})
    w.put(catalog, {"Type": topdf.Name("Catalog"), "Pages": parent})
    pdf = pdfium.PdfDocument(w.finish(catalog, w.add({"Producer": "test"}), bytes(16)))
    got = pdf[0].render(scale=(W - 1e-4) / pdf[0].get_width() if fit else dpi / 72, grayscale=True).to_numpy()
    assert got.shape[:2] == (H, W)
    bits = np.unpackbits(np.frombuffer(bytes(rows), np.uint8).reshape(H, -1), axis=1)[:, :W]
    assert np.mean(np.abs(got.reshape(H, W).astype(float) - (1 - bits.astype(float)) * 255)) < 1
    pdf.close()


def test_to_pdf_layers_and_size(tmp_path):
    fitz = pytest.importorskip("fitz")
    data = djvu.to_pdf(HERE / "compound.djvu")
    assert data[:5] == b"%PDF-"
    pdf = fitz.open(stream=data, filetype="pdf")
    images = pdf[0].get_images(full=True)
    assert len(images) >= 2  # the background, and the foreground picture (through the mask)
    assert len(data) < 100_000
    pix = pdf[0].get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
    assert (pix.width, pix.height) == (900, 1200)


def test_book(tmp_path):
    book = bookport.read(HERE / "book.djvu")
    assert book.source_format == "djvu" and book.fixed_layout
    assert book.metadata.title == "A Test Book" and book.metadata.authors == ["Pure Tech"]
    assert len(book.spine) == 5
    assert [e.title for e in book.toc] == ["Chapter one", "Chapter two \u00e9"]
    media = sorted(r.media_type for r in book.resources.values() if r.is_image)
    assert media == ["image/jpeg"] + ["image/png"] * 4
    book.write(tmp_path / "book.epub")
    book.write(tmp_path / "book.cbz")
    assert len(bookport.read(tmp_path / "book.cbz").spine) == 5


def test_picture_png():
    from bookport.images import decode_png
    pic = djvu.render(HERE / "bw_odd.djvu", 0)
    r = decode_png(pic.png())
    assert (r.width, r.height, r.mode) == (1001, 777, "L")
    nb = (pic.width + 7) // 8
    for y, x in ((100, 70), (400, 500), (700, 900)):
        black = (pic.data[y * nb + x // 8] >> (7 - x % 8)) & 1
        assert r.pixels[y * r.width + x] == (0 if black else 255)
    small = djvu.render(HERE / "gray_prog.djvu", 0, scale=0.5)
    assert (small.width, small.height, small.mode) == (128, 95, "L")


def test_palette_beyond_its_colours():
    """cpaldjvu wrote two symbols with colours past the palette; DjVuLibre refuses the page."""
    pic = djvu.render(HERE / "pal.djvu", 0)
    assert (pic.mode, pic.width, pic.height) == ("RGB", 600, 400)


def test_mmr_round_trip():
    rnd = random.Random(3)
    for w in (1, 7, 64, 333):
        rows = []
        for _ in range(40):
            r = 0
            for _ in range(rnd.randint(0, 12)):
                a = rnd.randrange(w)
                r |= ((1 << rnd.randint(1, w - a)) - 1) << a
            rows.append(r & ((1 << w) - 1))
        data = mmr.encode(rows, w)
        assert mmr.decode(data, w, len(rows)) == rows
        assert mmr.encode_packed(jb2.packed(rows, w), w, len(rows)) == data


def test_jpeg_encoder():
    pil = pytest.importorskip("PIL.Image")
    import io
    w, h, mode, px = iw44.decode(djvu.open(HERE / "photo_half.djvu").pages[0].all(b"BG44"))
    data = jpeg.encode(w, h, mode, px, 90)
    im = pil.open(io.BytesIO(data))
    assert im.size == (403, 301) and im.mode == "RGB"
    err = sum((a - b) ** 2 for a, b in zip(im.tobytes(), px)) / len(px)
    assert err < 100  # PSNR above 28 dB on a noisy picture


def test_native_matches_python():
    native = _accel.native
    if native is None:
        pytest.skip("bookport-native isn't installed")
    try:
        results = []
        for mode in (native, None):
            _accel.native = mode
            got = []
            for name in ("book.djvu", "compound.djvu", "photo_half.djvu", "gray_prog.djvu", "text.djvu"):
                doc = djvu.open(HERE / name)
                got += [djvu.render(doc, i).data for i in range(len(doc.pages))]
                got.append(djvu.to_text(HERE / name))
                page = doc.pages[0]
                for cid in (b"TXTz", b"ANTz"):
                    c = page.first(cid)
                    if c:
                        got.append(bzz.decode(c[1]))
                lay = composite.layers(page)
                if lay.jb2 is not None:
                    m = lay.jb2.mask()
                    got += [m, lay.jb2.mask([0, 2, 5]), mmr.encode_packed(m, lay.width, lay.height)]
                if lay.bg is not None:
                    got.append(jpeg.encode(*lay.bg, 75))
                    got.append(composite.upscale(lay.bg, lay.bg[0] * 3 - 1, lay.bg[1] * 3 - 2, 3))
            results.append(got)
        assert results[0] == results[1]
    finally:
        _accel.native = native


# --- damaged and hostile files -------------------------------------------------------------------

def _iff(kind: bytes, chunks: list[tuple[bytes, bytes]]) -> bytes:
    body = kind
    for cid, data in chunks:
        body += cid + struct.pack(">I", len(data)) + data + (b"\0" if len(data) & 1 else b"")
    return b"AT&TFORM" + struct.pack(">I", len(body)) + body


def _all_ways(path: Path, book: bool = True) -> None:
    """Everything a reader does with a file, which may only fail with BookportError."""
    try:
        doc = djvu.open(path)
    except BookportError:
        return
    for i in range(min(len(doc.pages), 3)):
        try:
            djvu.render(doc, i)
        except BookportError:
            pass
    for f in (djvu.to_text, djvu.to_pdf, bookport.read) if book else (djvu.to_text,):
        try:
            f(path)
        except BookportError:
            pass


def test_truncated(tmp_path):
    for name in ("book.djvu", "compound.djvu", "text.djvu"):
        data = (HERE / name).read_bytes()
        for cut in range(12, len(data), max(1, len(data) // 20)):
            p = tmp_path / "cut.djvu"
            p.write_bytes(data[:cut])
            _all_ways(p, name != "compound.djvu")


def test_mutated(tmp_path):
    rnd = random.Random(11)
    for name in ("bw_odd.djvu", "gray_prog.djvu", "text.djvu", "book.djvu"):
        data = (HERE / name).read_bytes()
        for _ in range(25):
            b = bytearray(data)
            for _ in range(rnd.randint(1, 8)):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
            p = tmp_path / "bad.djvu"
            p.write_bytes(bytes(b))
            t = time.perf_counter()
            _all_ways(p)
            assert time.perf_counter() - t < 60


def test_not_djvu(tmp_path):
    p = tmp_path / "x.djvu"
    for data in (b"", b"AT&T", b"AT&TFORM\0\0\0\4DJVM", _iff(b"DJVU", []), _iff(b"DJVU", [(b"INFO", b"\0")]),
                 _iff(b"XXXX", [(b"INFO", bytes(10))]), b"%PDF-1.4"):
        p.write_bytes(data)
        with pytest.raises(BookportError):
            doc = djvu.open(p)
            djvu.render(doc, 0)


def test_huge_page_refused(tmp_path):
    info = struct.pack(">HH", 65535, 65535) + bytes([24, 0, 100, 0, 22, 1])
    p = tmp_path / "huge.djvu"
    # a JB2 stream that is nothing but ZP-coded ones decodes to an absurd image size
    p.write_bytes(_iff(b"DJVU", [(b"INFO", info), (b"Sjbz", b"\xff" * 64)]))
    with pytest.raises(BookportError):
        djvu.render(p, 0)
    p.write_bytes(_iff(b"DJVU", [(b"INFO", info), (b"BG44", bytes([0, 1, 1, 2, 255, 255, 255, 255, 0x80]))]))
    with pytest.raises(BookportError):
        djvu.render(p, 0)


def test_text_ends_at_nul(tmp_path):
    """Encoders end the text with a NUL; djvutxt (a C program) stops there, and so do we."""
    info = struct.pack(">HH", 100, 100) + bytes([24, 0, 100, 0, 22, 1])
    txt = b"hello world" + bytes(1) + b"hidden"
    p = tmp_path / "t.djvu"
    p.write_bytes(_iff(b"DJVU", [(b"INFO", info), (b"TXTa", len(txt).to_bytes(3, "big") + txt)]))
    assert djvu.to_text(p) == "hello world\n\f"


def test_zp_runs_out_quietly():
    # an IW44 chunk cut to its header: the reference decoder keeps the (empty) picture
    w, h, mode, px = iw44.decode([bytes([0, 74, 0x81, 2, 0, 8, 0, 8, 0x80])])
    assert (w, h, mode) == (8, 8, "L") and set(px) == {127}
    with pytest.raises(BookportError):
        bzz.decode(b"\x12\x34")


def test_deep_and_long_structures():
    with pytest.raises(BookportError):
        anno.sexpr(b"(" * 10_000)
    assert anno.sexpr(b'(maparea "a\\"b" "" (rect 1 2 3 4)) (unclosed "x') == [
        ["maparea", 'a"b', "", ["rect", 1, 2, 3, 4]], ["unclosed", "x"]]
    # a zone tree that claims 2^24 children per level stops at the data's end
    zone = bytes([1]) + struct.pack(">5H", *([0x8000] * 5)) + b"\0\0\0" + b"\xff\xff\xff"
    data = b"\0\0\2hi" + b"\1" + zone * 40
    pt = text.parse(data, False)
    assert pt.text == b"hi"


def test_indirect_pages_stay_in_their_folder(tmp_path):
    doc = Document(HERE / "indirect" / "index.djvu")
    assert doc._sibling(Component("x", 1, "../book.djvu")) is None
    assert doc._sibling(Component("x", 1, "C:/Windows/win.ini")) is None
    assert any("outside the document's folder" in m for m in doc.lost)


def test_threads_give_the_same_pdf(monkeypatch):
    if _accel.native is None:
        pytest.skip("pages are prepared in threads only with bookport-native")
    import re

    def strip(d):
        return re.sub(rb"/CreationDate \([^)]*\)|/ID \[<[0-9a-f]+> <[0-9a-f]+>\]", b"", d)
    monkeypatch.setenv("BOOKPORT_THREADS", "1")
    one = strip(djvu.to_pdf(HERE / "book.djvu"))
    monkeypatch.setenv("BOOKPORT_THREADS", "4")
    assert strip(djvu.to_pdf(HERE / "book.djvu")) == one
