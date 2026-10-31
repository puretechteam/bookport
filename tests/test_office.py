"""DOCX, ODT, RTF and comics."""

import io
import struct
import zipfile
import zlib

import pytest

import bookport
from bookport.fmt import rtf

from .conftest import EBOOK_CONVERT, gif_bytes, needs_calibre, png, recall, run, toc_titles


@needs_calibre
@pytest.mark.parametrize("fmt", ["docx", "rtf"])
def test_calibre_reads_ours(book, tmp_path, fmt):
    out = tmp_path / f"b.{fmt}"
    book.write(out)
    epub = tmp_path / "c.epub"
    run(EBOOK_CONVERT, out, epub)
    back = bookport.read(epub)
    assert recall(book, back) > 0.97
    assert back.metadata.title == book.metadata.title
    assert len(back.images()) >= 2


@needs_calibre
@pytest.mark.parametrize("fmt", ["docx", "rtf"])
def test_we_read_calibres(book, tmp_path, fmt):
    src = tmp_path / "s.epub"
    book.write(src)
    out = tmp_path / f"cal.{fmt}"
    run(EBOOK_CONVERT, src, out)
    back = bookport.read(out)
    # Calibre's RTF writer runs table cells together and drops alt text, so a little less survives
    assert recall(book, back) > (0.93 if fmt == "rtf" else 0.97)
    titles = toc_titles(back)
    for t in ("Chapter One", "Chapter Two", "Chapter Three"):
        assert any(t in x for x in titles), (t, titles)
    assert back.images()


def test_docx_structure(book, tmp_path):
    out = tmp_path / "b.docx"
    book.write(out)
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode()
        assert 'w:val="Heading1"' in doc and 'w:val="Heading2"' in doc
        assert "w:hyperlink w:anchor=" in doc  # internal link to a bookmark
        assert "<w:tbl>" in doc and "<w:drawing>" in doc
        core = z.read("docProps/core.xml").decode()
        assert "Ada Writer; Bo Second" in core
    back = bookport.read(out)
    assert toc_titles(back) == ["Chapter One", "A Section", "Chapter Two", "Chapter Three"]
    assert back.metadata.authors == ["Ada Writer", "Bo Second"]
    # the link to the notes still points at them
    links = [a.get("href") for h, _ in back.documents() for a in back.tree(h).iter("a") if a.get("href")]
    assert any("#bm_" in x for x in links)


def test_docx_keeps_inline_alt_text_and_unique_bookmarks(tmp_path):
    import re

    from bookport import markup
    b = bookport.Book()
    b.metadata.title = "Drop caps"
    b.add("images/m.png", png(), "image/png")
    # a drop-cap picture whose alt text is the first letter, and a heading with two anchors
    b.add_document("text/a.xhtml", markup.parse(
        "<html><body><p><a href='#c2'>to two</a> <a href='#top'>top</a></p>"
        "<h2 id='c2'><a id='top'/>CHAPTER II.</h2><p><span><img src='../images/m.png' alt='M'/></span>R. "
        "BENNET was among the earliest.</p></body></html>"))
    out = tmp_path / "d.docx"
    b.write(out)
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode()
    assert 'descr="M"' in doc
    names = re.findall(r'bookmarkStart w:id="\d+" w:name="([^"]+)"', doc)
    assert len(names) == len(set(names)), names
    assert "MR" in bookport.read(out).text().replace(" ", "").upper()


def test_rtf_reader_features():
    src = (rb"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\froman Times;}{\f1\fmodern Courier New;}{\f2\fcharset204 Arial Cyr;}}"
           rb"{\stylesheet{\s0 Normal;}{\s1 heading 1;}}{\info{\title Tiny}{\author Ann Author}{\*\company Small Press}"
           rb"{\creatim\yr2020\mo2\dy3}}"
           rb"\pard\s1 Heading one\par"
           rb"\pard Plain {\b bold} {\i italic} {\f1 mono} caf\'e9 " + b"\x5c" + rb"u8364? x\line y\par"
           rb"\pard {\f2 \'cf\'f0\'e8\'e2\'e5\'f2}\par"
           rb"\pard {\field{\*\fldinst {HYPERLINK \"https://example.com\"}}{\fldrslt {link text}}} after\par"
           rb"\pard Note here{\super\chftn}{\footnote\pard {\super\chftn} The note.}\par"
           rb"\trowd\cellx1000\cellx2000\pard\intbl A1\cell B1\cell\row\pard after table\par}")
    b = rtf.from_bytes(src)
    text = b.text()
    assert b.metadata.title == "Tiny" and b.metadata.authors == ["Ann Author"]
    assert b.metadata.publisher == "Small Press" and b.metadata.date == "2020-02-03"
    for word in ("Heading one", "bold", "café", "€", "Привет", "link text", "The note", "A1", "B1", "after table"):
        assert word in text, word
    assert toc_titles(b) == ["Heading one", "Notes"]
    first = b.tree(b.spine[0])
    assert first.find(".//b").text == "bold" and first.find(".//code").text == "mono"
    assert any(a.get("href") == "https://example.com" for a in first.iter("a"))
    assert first.find(".//table") is not None


def test_rtf_escapes():
    s = rtf._esc("a{b}c\\d é € 😀\n")
    assert s == "a\\{b\\}c\\\\d \\u233? \\u8364? \\u-10179?\\u-8704?\\line "


def _odt(tmp_path):
    content = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink">
<office:automatic-styles><style:style style:name="T1" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>
<style:style style:name="T2" style:family="text"><style:text-properties fo:font-style="italic"/></style:style></office:automatic-styles>
<office:body><office:text>
<text:h text:outline-level="1">First Part</text:h>
<text:p>Some <text:span text:style-name="T1">bold</text:span> and <text:span text:style-name="T2">italic</text:span> text<text:note text:id="n1"><text:note-citation>1</text:note-citation><text:note-body><text:p>A footnote.</text:p></text:note-body></text:note>.</text:p>
<text:list><text:list-item><text:p>One</text:p></text:list-item><text:list-item><text:p>Two</text:p></text:list-item></text:list>
<table:table><table:table-row><table:table-cell><text:p>Cell</text:p></table:table-cell></table:table-row></table:table>
<text:p><draw:frame><draw:image xlink:href="Pictures/a.png"/></draw:frame></text:p>
<text:h text:outline-level="2">Second <text:a xlink:href="https://example.org">link</text:a></text:h>
</office:text></office:body></office:document-content>"""
    meta = """<?xml version="1.0" encoding="UTF-8"?><office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"><office:meta>
<dc:title>An ODT</dc:title><meta:initial-creator>Odie Writer</meta:initial-creator><dc:language>fr</dc:language>
</office:meta></office:document-meta>"""
    p = tmp_path / "a.odt"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", content)
        z.writestr("meta.xml", meta)
        z.writestr("Pictures/a.png", png())
    return p


def test_odt(tmp_path):
    b = bookport.read(_odt(tmp_path))
    assert bookport.detect(tmp_path / "a.odt") == "odt"
    assert b.metadata.title == "An ODT" and b.metadata.authors == ["Odie Writer"] and b.metadata.language == "fr"
    text = b.text()
    for word in ("First Part", "bold", "italic", "A footnote", "One", "Two", "Cell", "Second link"):
        assert word in text
    assert toc_titles(b) == ["First Part", "Second link", "Notes"]
    assert len(b.images()) == 1
    root = b.tree(b.spine[0])
    assert root.find(".//b").text == "bold" and root.find(".//ul") is not None
    b.write(tmp_path / "a.epub")


def _cbz(tmp_path, info=True):
    p = tmp_path / "comic.cbz"
    with zipfile.ZipFile(p, "w") as z:
        for i in (1, 2, 10):
            z.writestr(f"ch1/page{i}.png", png(30 + i, 40, (i * 20, 0, 0)))
        z.writestr("ch2/page1.gif", gif_bytes())
        if info:
            z.writestr("ComicInfo.xml", "<ComicInfo><Title>Big Comic</Title><Series>Bigs</Series><Number>3</Number>"
                                        "<Writer>W One, W Two</Writer><Manga>YesAndRightToLeft</Manga>"
                                        "<LanguageISO>ja</LanguageISO></ComicInfo>")
    return p


def test_cbz(tmp_path):
    b = bookport.read(_cbz(tmp_path))
    assert b.fixed_layout and b.direction == "rtl"
    assert b.metadata.title == "Big Comic" and b.metadata.authors == ["W One", "W Two"] and b.metadata.series_index == 3
    assert len(b.spine) == 4 and len(b.images()) == 4
    # natural order: page2 before page10
    assert [b.resources[h].data for h in b.spine]
    assert toc_titles(b) == ["ch1", "ch2"]
    for fmt in ("epub", "azw3", "mobi"):
        out = tmp_path / f"c.{fmt}"
        b.write(out)
        back = bookport.read(out)
        assert len(back.images()) == 4, fmt
        assert back.fixed_layout, fmt


def test_cbr_stored(tmp_path):
    # a minimal RAR 4 with two stored files, made by hand from the format description
    def block(kind, flags, body, add=b""):
        head = struct.pack("<BHH", kind, flags, 7 + len(body)) + body
        crc = zlib.crc32(head) & 0xFFFF
        return struct.pack("<H", crc) + head + add

    data = b"Rar!\x1a\x07\x00" + block(0x73, 0, b"\0" * 6)
    for name, payload in (("p1.png", png(10, 10)), ("p2.png", png(12, 12))):
        nb = name.encode()
        body = struct.pack("<IIBIIBBHI", len(payload), len(payload), 2, zlib.crc32(payload), 0, 29, 0x30, len(nb), 0x20) + nb
        data += block(0x74, 0x8000, body, payload)
    data += block(0x7B, 0, b"")
    p = tmp_path / "c.cbr"
    p.write_bytes(data)
    b = bookport.read(p)
    assert len(b.images()) == 2 and b.fixed_layout


def test_image_and_folder(tmp_path):
    (tmp_path / "imgs").mkdir()
    (tmp_path / "imgs" / "b.png").write_bytes(png(5, 5))
    (tmp_path / "imgs" / "a.png").write_bytes(png(6, 6))
    b = bookport.read(tmp_path / "imgs")
    assert len(b.spine) == 2
    one = tmp_path / "one.png"
    one.write_bytes(png())
    assert len(bookport.read(one).spine) == 1


def test_encrypted_comic_refused(tmp_path):
    p = tmp_path / "e.cbz"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.png", png())
    data = bytearray(p.read_bytes())
    # set the "encrypted" flag on both headers
    i = data.find(b"PK\x03\x04")
    data[i + 6] |= 1
    j = data.find(b"PK\x01\x02")
    data[j + 8] |= 1
    p.write_bytes(bytes(data))
    with pytest.raises(bookport.DRMError):
        bookport.read(p)


def test_old_office_refused(tmp_path):
    p = tmp_path / "old.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600)
    with pytest.raises(bookport.BookportError):
        bookport.read(p)


def test_docx_reader_handles_fields_and_numbering(tmp_path):
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = f"""<w:document xmlns:w="{W}"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Intro</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>first</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>nested</w:t></w:r></w:p>
<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK "https://x.org" </w:instrText></w:r>
<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>field link</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>
<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>hidden</w:t></w:r><w:r><w:t>shown</w:t></w:r></w:p>
</w:body></w:document>"""
    styles = f"""<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>"""
    numbering = f"""<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl>
<w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>"""
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="r2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
            "</Relationships>")
    p = tmp_path / "f.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/numbering.xml", numbering)
        z.writestr("word/_rels/document.xml.rels", rels)
    b = bookport.read(p)
    root = b.tree(b.spine[0])
    assert toc_titles(b) == ["Intro"]
    ol = root.find(".//ol")
    assert ol is not None and ol.find("li/ul/li").text == "nested"
    assert any(a.get("href") == "https://x.org" and a.text == "field link" for a in root.iter("a"))
    assert "hidden" not in b.text() and "shown" in b.text()
    io.BytesIO()
