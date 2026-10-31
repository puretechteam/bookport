"""The rare formats: PDB (PalmDOC, zTXT, eReader, Plucker), PML/PMLZ, TCR, RB, SNB, LRF, LIT, CHM."""

import struct
import zlib

import pytest

import bookport
from bookport import lzx, markup
from bookport.fmt import tcr
from bookport.palm import palmdoc_compress, pdb_bytes

from .conftest import EBOOK_CONVERT, needs_calibre, png, recall, run, toc_titles

WRITES = [("pdb", {}), ("pdb", {"format": "doc"}), ("pdb", {"format": "ztxt"}), ("pml", {}), ("pmlz", {}), ("tcr", {}),
          ("rb", {}), ("snb", {}), ("lrf", {}), ("lit", {})]


@pytest.mark.parametrize(("fmt", "opts"), WRITES, ids=[f"{f}-{o.get('format', '')}" for f, o in WRITES])
def test_round_trip(book, tmp_path, fmt, opts):
    out = tmp_path / f"b.{fmt}"
    book.write(out, fmt, **opts)
    back = bookport.read(out)
    # alt text has nowhere to go in these formats
    assert recall(book, back) > 0.97, fmt
    titles = toc_titles(back)
    for t in ("Chapter One", "Chapter Two", "Chapter Three"):
        assert any(t in x for x in titles), (fmt, titles)
    if fmt in ("pdb", "snb", "lrf", "rb", "pmlz", "lit") and not opts:
        assert back.metadata.title == "The Sample Book"
    if fmt in ("pmlz", "snb", "lrf", "rb", "lit") or fmt == "pdb" and not opts:
        assert back.images(), fmt


@needs_calibre
@pytest.mark.parametrize(("fmt", "opts"), WRITES, ids=[f"{f}-{o.get('format', '')}" for f, o in WRITES])
def test_calibre_reads_ours(book, tmp_path, fmt, opts):
    out = tmp_path / f"b.{fmt}"
    book.write(out, fmt, **opts)
    epub = tmp_path / "c.epub"
    run(EBOOK_CONVERT, out, epub)
    back = bookport.read(epub)
    assert recall(book, back) > 0.9, fmt


def _styled_book():
    b = bookport.Book()
    b.metadata.title = "Styled"
    b.cover = b.add("images/cover.png", png(), "image/png").href
    b.add("images/cell.png", png(20, 20, (0, 90, 200)), "image/png")
    long = " ".join(f"w{i}" for i in range(9000))  # a 50,000-character paragraph
    root = markup.parse("<html><body><h1>One</h1><p>From Verus<sup>1</sup> I learned good morals, and H<sub>2</sub>O "
                        "is water.</p><table><tr><td>cell text</td><td><img src='../images/cell.png' alt=''/></td></tr>"
                        f"</table><p>{long}</p></body></html>")
    b.add_document("text/one.xhtml", root)
    return b


@needs_calibre
@pytest.mark.parametrize(("fmt", "opts"), WRITES, ids=[f"{f}-{o.get('format', '')}" for f, o in WRITES])
def test_calibre_keeps_text_after_sup_and_sub(tmp_path, fmt, opts):
    book = _styled_book()
    out = tmp_path / f"b.{fmt}"
    book.write(out, fmt, **opts)
    epub = tmp_path / "c.epub"
    run(EBOOK_CONVERT, out, epub)
    assert recall(book, bookport.read(epub)) > 0.999, fmt


@pytest.mark.parametrize("fmt", ["lrf", "snb"])
def test_table_cell_images_and_cover_page_survive(tmp_path, fmt):
    book = _styled_book()
    out = tmp_path / f"b.{fmt}"
    book.write(out)
    back = bookport.read(out)
    sizes = sorted(len(r.data) for r in back.images())
    assert len(sizes) == 2, sizes  # the cover (not in the spine) and the picture in the table cell
    assert recall(book, back) > 0.999


@needs_calibre
@pytest.mark.parametrize(("fmt", "args"), [("pdb", ["--format", "ereader"]), ("pdb", ["--format", "doc"]),
                                           ("pdb", ["--format", "ztxt"]), ("pmlz", []), ("tcr", []), ("rb", []),
                                           ("snb", []), ("lrf", []), ("lit", [])])
def test_we_read_calibres(book, tmp_path, fmt, args):
    src = tmp_path / "s.epub"
    book.write(src)
    out = tmp_path / f"cal.{fmt}"
    run(EBOOK_CONVERT, src, out, *args)
    back = bookport.read(out)
    assert recall(book, back) > 0.9, (fmt, args)
    if fmt in ("lit", "snb", "lrf", "pmlz") or args == ["--format", "ereader"]:
        assert len(toc_titles(back)) >= 3, (fmt, toc_titles(back))
        if fmt != "pmlz":  # Calibre's PMLZ carries no metadata
            assert back.metadata.title == "The Sample Book"


def test_tcr_compression():
    text = ("It was the best of times, it was the worst of times. " * 200).encode()
    data = tcr.compress(text)
    assert len(data) < len(text) / 2
    assert data.startswith(b"!!8-Bit!!")
    pos = 9
    entries = []
    for _ in range(256):
        n = data[pos]
        entries.append(data[pos + 1:pos + 1 + n])
        pos += 1 + n
    assert b"".join(entries[b] for b in data[pos:]) == text


@pytest.mark.parametrize("size", [0, 1, 1000, 32768, 70001])
def test_lzx_stored_round_trip(size):
    import random
    data = bytes(random.Random(size).randrange(256) for _ in range(size))
    comp = lzx.compress_stored(data)
    dec = lzx.LzxDecoder(16)
    assert dec.decompress(comp, len(data)) == data


def _plucker(text_pages: list[list[bytes]]) -> bytes:
    """A small Plucker file: record 0 points home at uid 2; each page's paragraphs, PalmDOC compressed."""
    recs = [struct.pack(">HHHHH", 1, 1, 1, 0, 2)]
    for k, paras in enumerate(text_pages):
        table = b"".join(struct.pack(">HH", len(p), 0) for p in paras)
        body = b"".join(paras)
        recs.append(struct.pack(">HHHBB", 2 + k, len(paras), len(body), 1, 0) + table + palmdoc_compress(body))
    return pdb_bytes("Plucker test", b"Data", b"Plkr", recs)


def test_plucker(tmp_path):
    p = tmp_path / "p.pdb"
    p.write_bytes(_plucker([[b"Home page", b"See \x00\x0a\x00\x03the next page\x00\x08 and \x00\x40italics\x00\x48."],
                            [b"Second page text."]]))
    b = bookport.read(p)
    text = b.text()
    for w in ("Home page", "the next page", "italics", "Second page text"):
        assert w in text
    first = b.tree(b.spine[0])
    assert first.find(".//i").text == "italics"
    assert any(a.get("href", "").endswith("page00003.xhtml") for a in first.iter("a"))


def _chm(files: dict[str, bytes]) -> bytes:
    """A CHM with every file stored uncompressed in section 0."""
    def encint(v):
        out = [v & 0x7F]
        v >>= 7
        while v:
            out.append(0x80 | (v & 0x7F))
            v >>= 7
        return bytes(reversed(out))
    entries = b""
    content = b""
    for name, data in sorted(files.items()):
        nb = name.encode()
        entries += encint(len(nb)) + nb + encint(0) + encint(len(content)) + encint(len(data))
        content += data
    chunk_size = 4096
    chunk = b"PMGL" + struct.pack("<IIii", chunk_size - 20 - len(entries), 0, -1, -1) + entries
    chunk = chunk.ljust(chunk_size, b"\0")
    itsp = b"ITSP" + struct.pack("<IIIIIiiiiiI", 1, 0x54, 10, chunk_size, 2, 1, -1, 0, 0, -1, 1) + b"\0" * (0x54 - 48)
    directory = itsp + chunk
    header_len = 0x60
    dir_off = header_len
    content_off = dir_off + len(directory)
    itsf = b"ITSF" + struct.pack("<III", 3, header_len, 1) + b"\0" * 40 + struct.pack("<QQQQQ", 0, 0, dir_off, len(directory),
                                                                                      content_off)
    return itsf.ljust(header_len, b"\0") + directory + content


def test_chm_uncompressed(tmp_path):
    hhc = (b'<html><body><ul><li><object type="text/sitemap"><param name="Name" value="Start"><param name="Local" '
           b'value="a.htm"></object><ul><li><object type="text/sitemap"><param name="Name" value="Next">'
           b'<param name="Local" value="b.htm"></object></ul></ul></body></html>')
    system = struct.pack("<I", 3) + struct.pack("<HH", 3, 9) + b"Help Book" + struct.pack("<HH", 0, 7) + b"toc.hhc"
    p = tmp_path / "h.chm"
    p.write_bytes(_chm({"/a.htm": b"<html><body><h1>Start</h1><p>First <a href='b.htm'>link</a></p></body></html>",
                        "/b.htm": b"<html><body><p>Second page</p></body></html>", "/toc.hhc": hhc, "/#SYSTEM": system}))
    b = bookport.read(p)
    assert b.metadata.title == "Help Book"
    assert toc_titles(b) == ["Start", "Next"]
    assert "Second page" in b.text()
    assert any(a.get("href") == "b.xhtml" for a in b.tree(b.spine[0]).iter("a"))


def _many_chapters(n):
    b = bookport.Book()
    b.metadata.title = "Many"
    for i in range(n):
        root = markup.parse(f"<html><body><h1>Part {i}</h1><p>word{i} text</p></body></html>")
        res = b.add_document(f"chapter-{i:04d}.xhtml", root)
        b.toc.append(bookport.TocEntry(f"Part {i}", res.href))
    return b


def test_lit_directory_spans_chunks(tmp_path):
    # 400 documents make about 1,600 directory entries: several AOLL chunks and an AOLI index
    book = _many_chapters(400)
    out = tmp_path / "many.lit"
    book.write(out)
    assert out.read_bytes().count(b"AOLL") > 3 and b"AOLI" in out.read_bytes()
    back = bookport.read(out)
    assert len(back.spine) == 400 and len(toc_titles(back)) == 400
    assert recall(book, back) == 1.0


def test_lit_svg_cover_points_at_its_picture(tmp_path):
    # Microsoft Reader has no SVG; a Kindle-style SVG cover page must still show the picture
    book = _many_chapters(1)
    book.add("images/cover.png", png(), "image/png")
    book.add("images/cover.svg", '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
             '<image xlink:href="cover.png" width="40" height="30"/></svg>', "image/svg+xml")
    root = markup.parse('<html><body><img src="../images/cover.svg"/></body></html>')
    book.add_document("text/cover.xhtml", root)
    book.spine.insert(0, book.spine.pop())
    out = tmp_path / "c.lit"
    book.write(out)
    back = bookport.read(out)
    srcs = [img.get("src") for href in back.spine for img in back.tree(href).iter("img")]
    assert srcs and srcs[0].endswith("cover.png")
    assert [r.media_type for r in back.images()] == ["image/png"]


@needs_calibre
def test_calibre_reads_our_long_lit(tmp_path):
    book = _many_chapters(400)
    out = tmp_path / "many.lit"
    book.write(out)
    epub = tmp_path / "c.epub"
    run(EBOOK_CONVERT, out, epub)
    back = bookport.read(epub)
    assert recall(book, back) == 1.0 and len(toc_titles(back)) == 400


@needs_calibre
def test_calibre_keeps_lit_files_with_the_same_name(tmp_path):
    book = bookport.Book()
    book.metadata.title = "Same names"
    for folder in ("howto", "library"):
        root = markup.parse(f"<html><body><h1>{folder}</h1><p>only{folder} words here</p></body></html>")
        book.add_document(f"{folder}/intro.html", root)
    out = tmp_path / "same.lit"
    book.write(out)
    epub = tmp_path / "c.epub"
    run(EBOOK_CONVERT, out, epub)
    assert recall(book, bookport.read(epub)) == 1.0


def test_lit_drm_refused(tmp_path):
    # a LIT whose content lives in the encrypted section is refused
    from bookport.fmt.lit import Lit
    lit = Lit.__new__(Lit)
    lit.section_names = ["Uncompressed", "MSCompressed", "EbEncryptDS"]
    lit._sections = {}
    with pytest.raises(bookport.DRMError):
        lit.section(2)


def test_ereader_drm_refused(tmp_path):
    r0 = struct.pack(">H", 260) + b"\0" * 130
    p = tmp_path / "drm.pdb"
    p.write_bytes(pdb_bytes("drm", b"PNRd", b"PPrs", [r0, zlib.compress(b"text")]))
    with pytest.raises(bookport.DRMError):
        bookport.read(p)


def _quotes_book():
    book = _many_chapters(1)
    root = markup.parse("<html><body><p>See under Rule\u00a05: “it’s the writer’s café”.</p></body></html>")
    book.add_document("text/quotes.xhtml", root)
    return book


@pytest.mark.parametrize("fmt", ["txt", "tcr"])
def test_txt_and_tcr_are_utf8_with_a_byte_order_mark(tmp_path, fmt):
    out = tmp_path / f"q.{fmt}"
    _quotes_book().write(out)
    if fmt == "txt":
        assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    text = bookport.read(out).text()
    assert "\ufeff" not in text and "Rule\u00a05" in text and "it’s the writer’s café" in text


@needs_calibre
@pytest.mark.parametrize("fmt", ["txt", "tcr"])
def test_calibre_reads_our_quotes(tmp_path, fmt):
    # Calibre reads TCR as UTF-8 and guesses for TXT; Windows-1252 quotes came out as junk
    out = tmp_path / f"q.{fmt}"
    _quotes_book().write(out)
    txt_out = tmp_path / "c.txt"
    run(EBOOK_CONVERT, out, txt_out)
    assert "it’s the writer’s café" in txt_out.read_text("utf-8")


def test_snb_has_a_chapter_for_nested_toc_entries(tmp_path):
    book = _many_chapters(2)
    root = markup.parse("<html><body><h1>Stories</h1><h2 id='s1'>I</h2><p>one</p><h2 id='s2'>II</h2><p>two</p>"
                        "</body></html>")
    res = book.add_document("stories.xhtml", root)
    book.toc.append(bookport.TocEntry("Stories", res.href, [bookport.TocEntry("I", res.href + "#s1"),
                                                            bookport.TocEntry("II", res.href + "#s2")]))
    out = tmp_path / "n.snb"
    book.write(out)
    assert toc_titles(bookport.read(out)) == ["Part 0", "Part 1", "Stories", "I", "II"]


def test_tcr_keeps_text_outside_windows_1252(tmp_path):
    book = _many_chapters(1)
    root = markup.parse("<html><body><p>τὰ πρὸς ἑαυτόν and café</p></body></html>")
    book.add_document("text/greek.xhtml", root)
    out = tmp_path / "g.tcr"
    book.write(out)
    assert "τὰ πρὸς ἑαυτόν" in bookport.read(out).text()
