"""Ebook and office format details found in the 1.5 audit: what a write leaves in the caller's book,
what metadata survives a round trip, and odd but real input that used to fail a whole book."""

import xml.etree.ElementTree as ET

import pytest

import bookport
from bookport import Book, BookportError, Metadata, markup

from .conftest import gif_bytes, png, sample_book


def _one_page(title="T", body_html="<p>Words.</p>", images=()):
    book = Book(Metadata(title=title, language="en"))
    for href, data in images:
        book.add(href, data)
    root, body = markup.new_document(title, "en")
    for el in markup.parse_fragment(f"<div>{body_html}</div>")[1]:
        body.append(el)
    book.add_document("text/a.xhtml", root)
    return book


def test_writing_fb2_leaves_the_book_as_it_was(tmp_path):
    """FB2 holds only JPEG and PNG, so a GIF is converted; the converted copy used to replace the
    picture in the caller's book (a GIF there became a PNG)."""
    gif = gif_bytes()
    book = _one_page(body_html='<p><img src="../images/x.gif" alt=""/></p>', images=[("images/x.gif", gif)])
    book.write(tmp_path / "a.fb2")
    res = book.resources["images/x.gif"]
    assert res.media_type == "image/gif" and res.data == gif
    assert len(bookport.read(tmp_path / "a.fb2").images()) == 1


FB2_SECTIONS = ('<?xml version="1.0" encoding="utf-8"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
                '<description><title-info><book-title>S</book-title></title-info></description><body>'
                + "".join(f"<section><title><p>Part {n}</p></title><p>Text {n}.</p></section>" for n in range(1, 4))
                + "</body></FictionBook>")


def test_fb2_section_ids_are_the_same_every_time(tmp_path):
    """Sections without ids were named from id() of an object: two reads of one file differed."""
    p = tmp_path / "s.fb2"
    p.write_text(FB2_SECTIONS, encoding="utf-8")
    first, second = bookport.read(p), bookport.read(p)

    def ids(book):
        return [el.get("id") for d in book.spine for el in book.tree(d).iter() if el.get("id")]
    assert ids(first) == ids(second) and len(ids(first)) >= 3


@pytest.mark.parametrize("fmt", ["docx", "odt", "rtf"])
def test_office_metadata_round_trip(tmp_path, fmt):
    """Subjects came back twice (subject and keywords), the date became the day of writing, ODT kept two
    authors as one, and DOCX and ODT dropped the publisher, ISBN and series."""
    md = bookport.read(sample_book().write(tmp_path / f"s.{fmt}")).metadata
    assert md.subjects == ["Fiction", "Testing"]
    if fmt == "rtf":
        return
    assert md.authors == ["Ada Writer", "Bo Second"]
    assert md.date == "2021-05-04"
    assert md.publisher == "Pure Tech Press" and md.identifiers.get("isbn") == "9780000000002"
    assert md.series == "Test Series" and md.series_index == 2.0


@pytest.mark.parametrize("fmt", ["mobi", "azw3"])
def test_mobi_ids_come_back_as_written(tmp_path, fmt):
    """The book's own id (EXTH 112, "bookport:<uuid>") came back as a "source", and its copy in EXTH 113
    (where Kindles want an ASIN) as the ASIN."""
    ids = bookport.read(sample_book().write(tmp_path / f"s.{fmt}")).metadata.identifiers
    assert ids == {"isbn": "9780000000002", "uuid": "0b7f7a3e-1111-4222-8333-944455556666"}
    book = sample_book()
    book.metadata.identifiers["asin"] = "B00TEST"
    assert bookport.read(book.write(tmp_path / f"t.{fmt}")).metadata.identifiers["asin"] == "B00TEST"


@pytest.mark.parametrize("version", [2, 3])
def test_epub_keeps_every_identifier_scheme(tmp_path, version):
    """EPUB 3 output wrote other schemes as bare values, which came back as one "id" (the ASIN was lost)."""
    book = sample_book()
    book.metadata.identifiers.update({"goodreads": "12345", "asin": "B00TEST", "url": "https://example.org/b"})
    ids = bookport.read(book.write(tmp_path / "s.epub", version=version)).metadata.identifiers
    assert ids == book.metadata.identifiers


def test_epub_title_with_a_decimal_display_seq(tmp_path):
    from .test_hostile import _epub, _opf
    head = (b'<dc:title id="t1">Main</dc:title><meta refines="#t1" property="display-seq">1.0</meta>')
    opf = _opf().replace(b"<dc:title>T</dc:title>", head).replace(b'version="2.0"', b'version="3.0"')
    book = bookport.read(_epub(tmp_path / "x.epub", opf=opf))
    assert book.metadata.title == "Main"


def test_lit_with_many_ids_writes_quickly(tmp_path):
    """Each id measured the whole output so far: 16,000 ids took 29 s to write, now 0.2 s."""
    import time
    book = Book(Metadata(title="Ids", language="en"))
    root, body = markup.new_document("Ids", "en")
    for i in range(8000):
        ET.SubElement(body, "p", {"id": f"p{i}"}).text = f"Paragraph {i} with a few words in it."
    book.add_document("text/a.xhtml", root)
    t = time.perf_counter()
    book.write(tmp_path / "a.lit")
    assert time.perf_counter() - t < 2.5
    assert "Paragraph 7999" in bookport.read(tmp_path / "a.lit").text()


def test_lit_odd_table_numbers(tmp_path):
    """colspan="²" passed isdigit() and then failed int(); a huge colspan overflowed chr()."""
    html = ('<table><tr><td colspan="²">a</td><td rowspan="99999999">b</td><td colspan="2">c</td></tr></table>'
            '<ol><li value="٣">x</li></ol>')
    book = _one_page(body_html=html)
    book.write(tmp_path / "a.lit")
    assert "c" in bookport.read(tmp_path / "a.lit").text()


@pytest.mark.parametrize("kind", ["doc", "ztxt"])
def test_palm_text_keeps_any_alphabet(tmp_path, kind):
    """PalmDOC and zTXT were written as cp1252 with "?" for the rest: "Война и мир" came back as "????? ? ???"."""
    book = _one_page(title="W", body_html="<p>Война и мир, 東京, and café.</p>")
    book.write(tmp_path / "a.pdb", format=kind)
    assert "Война и мир, 東京, and café." in bookport.read(tmp_path / "a.pdb").text()
    plain = _one_page(title="P", body_html="<p>A café.</p>")
    plain.write(tmp_path / "b.pdb", format=kind)
    assert "A café." in bookport.read(tmp_path / "b.pdb").text()


def test_lit_pages_with_one_name_in_two_folders(tmp_path, monkeypatch):
    """a/index.htm and b/index.htm both became text/index.xhtml: the second was renamed when added, but
    links and the table of contents still pointed at the first."""
    from bookport.fmt import lit
    book = Book(Metadata(title="Two", language="en"))
    for folder, words, link in (("a", "A words", '<a href="../b/index.xhtml#x">to b</a>'), ("b", "B words", "")):
        root, body = markup.new_document(folder, "en")
        for el in markup.parse_fragment(f'<div><p id="x">{words}</p><p>{link}</p></div>')[1]:
            body.append(el)
        book.add_document(f"{folder}/index.xhtml", root)
    p = book.write(tmp_path / "t.lit")
    real = lit._package

    def as_folders(data):  # what other LIT makers write: the source file's own path
        pkg = real(data)
        for n, e in enumerate(e for e in pkg.iter("item") if "document" in e.get("media-type", "")):
            e.set("href", f"\x02{'ab'[n]}/index.htm")
        return pkg
    monkeypatch.setattr(lit, "_package", as_folders)
    got = bookport.read(p)
    assert len(got.spine) == 2
    link = next(el.get("href") for el in got.tree(got.spine[0]).iter() if el.get("href"))
    target = link.partition("#")[0]
    assert "B words" in markup.serialize(got.tree("text/" + target)).decode("utf-8")


@pytest.mark.parametrize(("section", "error"), [("EbEncryptDS1", bookport.DRMError), ("Unknown12345", BookportError)])
def test_lit_section_names(tmp_path, section, error):
    """Every section other than MSCompressed was called DRM, a damaged name too."""
    p = sample_book().write(tmp_path / "a.lit")
    data = p.read_bytes()
    assert data.count("MSCompressed".encode("utf-16-le")) == 1
    p.write_bytes(data.replace("MSCompressed".encode("utf-16-le"), section.encode("utf-16-le")))
    with pytest.raises(error) as e:
        bookport.read(p)
    assert (e.type is bookport.DRMError) == (error is bookport.DRMError)


def test_lit_stray_byte_in_a_page(tmp_path, monkeypatch):
    from bookport.fmt import lit
    p = sample_book().write(tmp_path / "a.lit")
    real = lit.tokens

    def damaged(data):
        return real(data.replace(b"It was a ", b"It was a \xff", 1))
    monkeypatch.setattr(lit, "tokens", damaged)
    assert "bright" in bookport.read(p).text()


def test_chm_links_ignore_case_and_text_uses_the_language_code_page(tmp_path):
    """CHM names aren't case-sensitive, but pictures and styles were matched exactly (<img src="PICS/A.PNG">
    for /pics/a.png was left pointing nowhere); and every CHM was read as cp1252, so Russian, Japanese or
    Chinese help came out garbled."""
    import struct

    from .test_rare import _chm
    title = "Справка".encode("cp1251")
    system = (struct.pack("<I", 3) + struct.pack("<HH", 4, 4) + struct.pack("<I", 0x0419)
              + struct.pack("<HH", 3, len(title)) + title)
    page = ("<html><body><p>Привет, <a href='pages/intro.htm#x'>дальше</a> <img src='PICS/A.PNG' alt=''/></p>"
            "</body></html>").encode("cp1251")
    p = tmp_path / "h.chm"
    p.write_bytes(_chm({"/a.htm": page, "/Pages/Intro.htm": b"<html><body><p id='x'>Second</p></body></html>",
                        "/pics/a.png": png(), "/#SYSTEM": system}))
    b = bookport.read(p)
    assert b.metadata.title == "Справка" and "Привет" in b.text()
    first = next(h for h in b.spine if "Привет" in markup.serialize(b.tree(h)).decode("utf-8"))
    assert any(a.get("href", "").lower().endswith("intro.xhtml#x") for a in b.tree(first).iter("a"))
    assert [i.get("src") for i in b.tree(first).iter("img")] == ["pics/a.png"]


def test_chm_code_pages():
    from bookport.fmt.chm import codepage
    assert [codepage(x) for x in (0x0409, 0x0411, 0x0804, 0x0404, 0x0419, 0x0C1A, 0x081A, 0x0415)] == [
        "cp1252", "cp932", "gbk", "cp950", "cp1251", "cp1251", "cp1250", "cp1250"]


def test_pml_emoji_codes_and_note_bodies(tmp_path):
    r"""\UD83D\UDE00 (an emoji as a surrogate pair, which 4-digit \U codes need) failed the whole book, and
    note bodies lost their character codes ("caf\a233" became "caf233")."""
    src = (r'\x Notes\x' "\n\n" r'Smile \UD83D\UDE00 and a lone \UD800 half.\Fn="n1"1\Fn' "\n"
           r'<footnote id="n1">caf\a233 and caf\U00E9 \iit\i</footnote>' "\n")
    p = tmp_path / "a.pml"
    p.write_text(src, encoding="cp1252")
    text = bookport.read(p).text()
    assert "Smile \U0001F600 and a lone half." in text
    assert "café and café it" in text
    book = _one_page(body_html="<p>Smile \U0001F600 again.</p>")
    book.write(tmp_path / "b.pml")
    assert "Smile \U0001F600 again." in bookport.read(tmp_path / "b.pml").text()


def test_cli_info_through_a_pipe_is_utf8(tmp_path):
    """On Windows a pipe used the ANSI code page, which can't hold "Война и мир 東京": --info ended in an error."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    book = _one_page(title="Война и мир 東京")
    src = book.write(tmp_path / "w.epub")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONPATH"] = str(Path(bookport.__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "bookport", str(src), "--info"], capture_output=True, env=env, timeout=120,
                       check=False)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.decode("utf-8"))["title"] == "Война и мир 東京"
    r = subprocess.run([sys.executable, "-m", "bookport", str(src), str(tmp_path / "w.fb2"), "--info"], capture_output=True,
                       env=env, timeout=120, check=False)
    assert r.returncode == 0 and (tmp_path / "w.fb2").exists()  # --info with an output converts as well


def test_writer_errors_are_bookport_errors(tmp_path):
    """A 120 KB description overflowed LRF's 16-bit info size (a bare struct.error); EPUB version "3" was a TypeError."""
    import random
    book = sample_book()
    rnd = random.Random(1)
    book.metadata.description = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(400_000))
    book.write(tmp_path / "a.lrf")
    md = bookport.read(tmp_path / "a.lrf").metadata
    assert md.title == "The Sample Book" and 1000 < len(md.description) < 400_000
    sample_book().write(tmp_path / "a.epub", version="3")
    with pytest.raises(BookportError, match="use 2 or 3"):
        sample_book().write(tmp_path / "b.epub", version=4)
    assert not (tmp_path / "b.epub").exists()


def test_a_write_that_fails_inside_a_writer_is_a_bookport_error(tmp_path, monkeypatch):
    import struct

    from bookport.fmt import fb2

    def broken(*a, **k):
        raise struct.error("'H' format requires 0 <= number <= 65535")
    monkeypatch.setattr(fb2, "to_bytes", broken)
    with pytest.raises(BookportError, match="Can't write this book as FB2"):
        sample_book().write(tmp_path / "a.fb2")
    assert list(tmp_path.iterdir()) == []


def test_mobi6_head_stays_out_of_the_first_chapter(tmp_path, monkeypatch):
    """The <html>/<head> tags were stripped before the head itself, so its leftovers (a <link>, stray text)
    landed in chapter one; and <header> was taken for <head>."""
    from bookport.fmt import mobi
    from bookport.palm import read_pdb
    out = sample_book().write(tmp_path / "a.mobi")
    pdb = read_pdb(out.read_bytes())
    h6 = mobi.parse_header(pdb.records, 0)
    html = (b'<html><head><title>x</title><link rel="stylesheet" href="s.css"/>Stray head words</head>'
            b"<body><header><p>Top line</p></header><p>Body words</p></body></html>")
    monkeypatch.setattr(mobi, "_text", lambda records, h: html)
    text = mobi._read_mobi6(pdb.records, h6).text()
    assert "Top line" in text and "Body words" in text and "Stray head words" not in text


def test_docx_alternate_content_without_warnings(tmp_path):
    """`find(Fallback) or find(Choice)` tested an element's truth, which Python 3.12 warns about (and means
    "has children", so an empty Fallback silently fell through)."""
    import warnings
    import zipfile

    from bookport.fmt.docx import MC
    src = sample_book().write(tmp_path / "a.docx")
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n) for n in z.namelist()}
    alt = (f'<mc:AlternateContent xmlns:mc="{MC}"><mc:Choice Requires="w14"><w:p><w:r><w:t>New way</w:t></w:r></w:p>'
           '</mc:Choice><mc:Fallback><w:p><w:r><w:t>Old way</w:t></w:r></w:p></mc:Fallback></mc:AlternateContent>'
           f'<mc:AlternateContent xmlns:mc="{MC}"><mc:Choice Requires="w14"><w:p><w:r><w:t>Only choice</w:t></w:r></w:p>'
           '</mc:Choice><mc:Fallback/></mc:AlternateContent>')
    files["word/document.xml"] = files["word/document.xml"].replace(b"<w:body>", b"<w:body>" + alt.encode(), 1)
    p = tmp_path / "b.docx"
    with zipfile.ZipFile(p, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        text = bookport.read(p).text()
    assert "Old way" in text and "New way" not in text and "Only choice" in text


def _rb(files):
    """A Rocket eBook from (name, bytes, flags) entries, stored as they are."""
    import struct

    from bookport.fmt.rb import MAGIC, TOC_OFFSET
    toc = struct.pack("<I", len(files))
    body = b""
    offset = TOC_OFFSET + 4 + 44 * len(files)
    for name, raw, flags in files:
        toc += name.encode()[:31].ljust(32, b"\0") + struct.pack("<III", len(raw), offset + len(body), flags)
        body += raw
    header = (MAGIC + b"\0" * (24 - len(MAGIC)) + struct.pack("<II", TOC_OFFSET, 0)).ljust(TOC_OFFSET, b"\0")
    return header + toc + body


def test_rb_links_between_pages_and_one_line_titles(tmp_path):
    """Pages are renamed text/partNNNN.xhtml, but links between them weren't; and a line break in a title
    wrote a key of its own into the info block."""
    p = tmp_path / "two.rb"
    p.write_bytes(_rb([("info.info", b"TYPE=2\nTITLE=Two\n", 2),
                       ("one.html", b'<html><body><p>First <a href="Two.html#end">on</a></p></body></html>', 0),
                       ("two.html", b'<html><body><p id="end">Second</p></body></html>', 0)]))
    book = bookport.read(p)
    link = next(a.get("href") for a in book.tree(book.spine[0]).iter("a"))
    assert link == "part0002.xhtml#end"
    odd = _one_page(title="Line one\nBODY=other.html")
    odd.write(tmp_path / "o.rb")
    back = bookport.read(tmp_path / "o.rb")
    assert back.metadata.title == "Line one BODY=other.html" and "Words." in back.text()


def _unfilter_reference(raw, width, height, bpp, row_bytes):
    """The PNG spec's filters, byte by byte (the code bookport had before 0.4)."""
    out = bytearray()
    prev = bytearray(row_bytes)
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + row_bytes])
        pos += 1 + row_bytes
        for i in range(row_bytes):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            line[i] = (line[i] + (0, a, b, (a + b) >> 1, a if pa <= pb and pa <= pc else b if pb <= pc else c)[ftype]) & 0xFF
        out += line
        prev = line
    return out


@pytest.mark.parametrize("pillow", [True, False])
@pytest.mark.parametrize("bpp", [1, 2, 3, 4, 6, 8])
def test_png_unfilter_matches_the_spec(bpp, pillow, monkeypatch):
    """Both ways of undoing PNG filters (Pillow's, in C, for 8-bit samples; Python's otherwise) give the
    spec's bytes. Paeth on 800x600 RGB took 0.33 s in Python."""
    import random

    from bookport.images import _unfilter
    from bookport.pdf import parser
    if not pillow:
        monkeypatch.setattr(parser, "_png_rows", lambda *a: None)
    rnd = random.Random(bpp)
    w, h = 61, 17
    raw = b"".join(bytes([rnd.randrange(5)]) + rnd.randbytes(w * bpp) for _ in range(h))
    assert _unfilter(raw, w, h, bpp, w * bpp) == _unfilter_reference(raw, w, h, bpp, w * bpp)


def test_mobi_table_of_contents_index_is_linear():
    """Each entry's length came from a scan of every entry: 10,000 entries took 1.8 s, now 0.1 s."""
    import time

    from bookport.fmt import mobi_write
    entries = [{"pos": i * 100, "title": f"Chapter {i}", "depth": 0, "parent": -1, "children": []} for i in range(10_000)]
    t = time.perf_counter()
    mobi_write._ncx_index(entries, 10_000 * 100 + 50, False)
    assert time.perf_counter() - t < 0.8


def test_kf8_only_book_named_mobi_is_found_from_its_header(tmp_path, monkeypatch):
    """Telling a KF8-only .mobi from a joint one read the whole file (then read() read it again); now the header."""
    from pathlib import Path
    kf8 = sample_book().write(tmp_path / "a.azw3")
    named = tmp_path / "b.mobi"
    named.write_bytes(kf8.read_bytes())
    joint = sample_book().write(tmp_path / "c.mobi")
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read the whole file"))
    assert bookport.detect(named) == "azw3" and bookport.detect(joint) == "mobi"


@pytest.mark.parametrize("ext", ["epub", "htmlz", "txtz"])
def test_zip_writers_dont_hold_the_book_twice(tmp_path, ext):
    """The ZIP was built in memory and then copied to the file: 40 MB of pictures cost 46 to 51 MB more,
    now 4 to 12 MB."""
    import os
    import tracemalloc
    book = Book(Metadata(title="Pics", language="en"))
    root, body = markup.new_document("Pics", "en")
    for i in range(8):
        book.add(f"images/p{i}.jpg", b"\xff\xd8\xff\xe0" + os.urandom(5 * 2**20), "image/jpeg")
        ET.SubElement(body, "img", {"src": f"../images/p{i}.jpg", "alt": ""})
    book.add_document("text/a.xhtml", root)
    tracemalloc.start()
    try:
        book.write(tmp_path / f"a.{ext}")
        peak = tracemalloc.get_traced_memory()[1] / 2**20
    finally:
        tracemalloc.stop()
    assert peak < 25
    assert len(bookport.read(tmp_path / f"a.{ext}").images()) == 8


def _rar5_stored(files, encrypted=False):
    """A RAR 5 archive with stored files, made from RARLAB's format description; with ``encrypted``, each
    file header carries a file encryption record (a password on the files, not the headers)."""
    def vint(v):
        out = bytearray()
        while True:
            b = v & 0x7F
            v >>= 7
            out.append(b | (0x80 if v else 0))
            if not v:
                return bytes(out)

    def header(kind, flags, rest, extra=b"", data=b""):
        fields = vint(kind) + vint(flags) + (vint(len(extra)) if flags & 1 else b"") + (vint(len(data)) if flags & 2 else b"")
        body = fields + rest + extra
        return b"\0\0\0\0" + vint(len(body)) + body + data

    out = b"Rar!\x1a\x07\x01\x00" + header(1, 0, vint(0))
    for name, payload in files:
        nb = name.encode()
        rest = vint(0) + vint(len(payload)) + vint(0x20) + vint(0) + vint(2) + vint(len(nb)) + nb
        extra = b""
        if encrypted:  # type 1, version 0, flags 0, KDF count 15, salt 16, IV 16
            record = vint(1) + vint(0) + vint(0) + bytes([15]) + bytes(32)
            extra = vint(len(record)) + record
        out += header(2, 2 | (1 if extra else 0), rest, extra, payload)
    return out + header(5, 0, vint(0))


def test_cbr5_with_a_password_on_the_pages_is_drm(tmp_path):
    """RAR 5 can encrypt the files but not the headers; that was read as "no pages" instead of a password."""
    pages = [("p1.png", png(10, 10)), ("p2.png", png(12, 12))]
    p = tmp_path / "a.cbr"
    p.write_bytes(_rar5_stored(pages))
    assert len(bookport.read(p).images()) == 2
    p.write_bytes(_rar5_stored(pages, encrypted=True))
    with pytest.raises(bookport.DRMError):
        bookport.read(p)
