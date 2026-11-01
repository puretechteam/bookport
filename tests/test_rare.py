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


def test_unpack_chm(tmp_path):
    p = tmp_path / "h.chm"
    files = {"/a.htm": b"<html><body><p>First</p></body></html>", "/pics/": b"", "/pics/i.png": png(), "/#SYSTEM": b"\3\0\0\0",
             "/empty.txt": b"", "::DataSpace/NameList": b"\0\0"}
    p.write_bytes(_chm(files))
    assert bookport.unpack(p) == [("#SYSTEM", b"\3\0\0\0"), ("a.htm", files["/a.htm"]), ("empty.txt", b""),
                                  ("pics/i.png", png())]


def test_unpack_lit(tmp_path):
    book = _many_chapters(3)
    book.add("images/pic.png", png(), "image/png")
    out = tmp_path / "b.lit"
    book.write(out)
    files = dict(bookport.unpack(out))
    assert png() in files.values()  # pictures are stored as they are
    assert files.get("meta")  # the package, tokenized
    contents = [name for name in files if name.endswith("/content")]
    assert len(contents) >= 3 and all(files[n] for n in contents)  # pages, from the LZX section
    assert not any(n.startswith(("/", "::")) or n.endswith("/") for n in files)


def test_unpack_refuses_drm_and_other_formats(tmp_path, monkeypatch):
    from bookport.fmt.lit import Lit
    out = tmp_path / "b.lit"
    _many_chapters(1).write(out)
    monkeypatch.setattr(Lit, "_names", lambda self: ["Uncompressed", "EbEncryptDS"])  # the pages in an encrypted section
    with pytest.raises(bookport.DRMError):
        bookport.unpack(out)
    epub = _many_chapters(1).write(tmp_path / "b.epub")
    with pytest.raises(bookport.BookportError, match="unpacks CHM and LIT"):
        bookport.unpack(epub)
    with pytest.raises(bookport.BookportError, match="No such file"):
        bookport.unpack(tmp_path / "missing.chm")


# --- Palm bitmaps (Plucker images), built from the format description --------------------------

def _bitmap(width, height, depth, rows: list[bytes], version=2, flags=0, compression=None, palette=None,
            transparent=0, direct=None, next_off=0):
    """One Palm bitmap; rows are the packed pixel rows (padded to row_bytes); compression None is
    stored, else "scanline", "rle" or "packbits"."""
    row_bytes = len(rows[0])
    raw = b"".join(rows)
    if palette is not None:
        flags |= 0x4000
    if compression is not None:
        flags |= 0x8000
        kind = {"scanline": 0, "rle": 1, "packbits": 2}[compression]
        packed = {"scanline": _scanline, "rle": _rle, "packbits": _packbits}[compression](raw, row_bytes, depth)
        size = 4 if version >= 3 else 2
        body = (len(packed) + size).to_bytes(size, "big") + packed
    else:
        kind = 0xFF if version >= 2 else 0
        body = raw
    if version >= 3:
        head = struct.pack(">HHHHBBBBBBHII", width, height, row_bytes, flags, depth, 3, 24, 2 if depth == 16 else 0, 0,
                           kind, 72, transparent, next_off)
    else:
        head = struct.pack(">HHHHBBHBBH", width, height, row_bytes, flags, depth, version, next_off // 4, transparent,
                           kind if version == 2 else 0, 0)
    table = b""
    if palette is not None:
        table = struct.pack(">H", len(palette)) + b"".join(bytes([k, *c]) for k, c in enumerate(palette))
    extra = b""
    if direct is not None:
        extra = bytes([5, 6, 5, 0, 0, *direct])
    return head + table + extra + body


def _scanline(raw, row_bytes, _depth):
    out = bytearray()
    prev = bytes(row_bytes)
    for y in range(0, len(raw), row_bytes):
        row = raw[y:y + row_bytes]
        for start in range(0, row_bytes, 8):
            flags, lits = 0, b""
            for k in range(start, min(start + 8, row_bytes)):
                if row[k] != prev[k]:
                    flags |= 0x80 >> (k - start)
                    lits += bytes([row[k]])
            out += bytes([flags]) + lits
        prev = row
    return bytes(out)


def _rle(raw, _row_bytes, _depth):
    out = bytearray()
    i = 0
    while i < len(raw):
        n = 1
        while i + n < len(raw) and raw[i + n] == raw[i] and n < 255:
            n += 1
        out += bytes([n, raw[i]])
        i += n
    return bytes(out)


def _packbits(raw, _row_bytes, depth):
    unit = 2 if depth == 16 else 1
    units = [raw[i:i + unit] for i in range(0, len(raw), unit)]
    out = bytearray()
    i = 0
    while i < len(units):
        n = 1
        while i + n < len(units) and units[i + n] == units[i] and n < 128:
            n += 1
        if n > 1:  # a repeat: 257 - count
            out += bytes([257 - n]) + units[i]
            i += n
        else:  # literals up to the next repeat
            j = i
            while j < len(units) and j - i < 128 and not (j + 1 < len(units) and units[j + 1] == units[j]):
                j += 1
            j = max(j, i + 1)
            out += bytes([j - i - 1]) + b"".join(units[i:j])
            i = j
    return bytes(out)


def _pixel_rows(values, width, depth):
    """Pack rows of pixel values MSB first, rows padded to an even number of bytes."""
    rows = []
    row_bytes = ((width * depth + 15) // 16) * 2
    for row in values:
        bits = 0
        for v in row:
            bits = bits << depth | v
        bits <<= row_bytes * 8 - width * depth
        rows.append(bits.to_bytes(row_bytes, "big"))
    return rows


def _rgb(r):
    return [tuple(r.pixels[k:k + len(r.mode)]) for k in range(0, len(r.pixels), len(r.mode))]


def test_palm_bitmap_greys_at_every_depth_and_compression():
    from bookport.palmbitmap import decode
    for depth in (1, 2, 4):
        top = (1 << depth) - 1
        values = [[(x + y) % (top + 1) for x in range(13)] for y in range(5)]
        want = [(255 - v * 255 // top,) * 3 for row in values for v in row]
        for version in (0, 1, 2):
            for compression in (None, "scanline") if version < 2 else (None, "scanline", "rle", "packbits"):
                data = _bitmap(13, 5, depth, _pixel_rows(values, 13, depth), version=version, compression=compression)
                r = decode(data)
                assert (r.width, r.height, r.mode) == (13, 5, "RGB")
                assert _rgb(r) == want, (depth, version, compression)


def test_palm_bitmap_colour_table_transparency_and_16_bits():
    from bookport.palmbitmap import decode
    palette = [(250, 0, 0), (0, 250, 0), (0, 0, 250), (9, 9, 9)]
    values = [[x % 4 for x in range(7)] for _ in range(3)]
    data = _bitmap(7, 3, 8, _pixel_rows(values, 7, 8), compression="rle", palette=palette, flags=0x2000, transparent=3)
    r = decode(data)
    assert r.mode == "RGBA"
    assert _rgb(r)[:4] == [(250, 0, 0, 255), (0, 250, 0, 255), (0, 0, 250, 255), (9, 9, 9, 0)]
    # 16-bit RGB 565: pure red, green, blue and white, packed in 16-bit units
    px = [0xF800, 0x07E0, 0x001F, 0xFFFF] * 2
    rows = [b"".join(struct.pack(">H", v) for v in px)] * 4
    for compression in (None, "packbits", "scanline"):
        r = decode(_bitmap(8, 4, 16, rows, flags=0x0400, compression=compression, direct=(1, 2, 3)))
        assert _rgb(r)[:4] == [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)], compression
    v3 = decode(_bitmap(8, 4, 16, rows, version=3, compression="packbits"))
    assert _rgb(v3)[:4] == [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]


def test_palm_bitmap_chain_uses_the_deepest_and_the_system_palette():
    from bookport.palmbitmap import SYSTEM_PALETTE, decode
    low = _bitmap(4, 2, 1, _pixel_rows([[1, 0, 1, 0]] * 2, 4, 1), version=1)
    low = low[:10] + struct.pack(">H", (len(low) + 16) // 4) + low[12:]  # next: after the empty bitmap that follows
    dummy = struct.pack(">HHHHBBHI", 0, 0, 0, 0, 255, 1, 0, 0)
    deep = _bitmap(4, 2, 8, _pixel_rows([[0, 5, 215, 230]] * 2, 4, 8), version=3)
    r = decode(low + dummy + deep)
    assert (r.width, r.height) == (4, 2)
    assert _rgb(r)[:4] == [(255, 255, 255), (255, 0, 255), (0, 0, 0), (0, 128, 128)]
    assert SYSTEM_PALETTE[1] == (255, 204, 255) and SYSTEM_PALETTE[6] == (255, 255, 204)


def test_palm_bitmap_damage_raises_bookport_error():
    from bookport.palmbitmap import decode
    good = _bitmap(13, 5, 4, _pixel_rows([[1] * 13] * 5, 13, 4), compression="scanline")
    v3 = _bitmap(4, 2, 8, _pixel_rows([[1] * 4] * 2, 4, 8), version=3)
    for bad in (good[:6], good[:4] + b"\0\0" + good[6:], b"\0" * 16, struct.pack(">HHHHBB", 9, 9, 1, 0, 8, 2) + b"\0" * 6,
                v3[:18]):
        with pytest.raises(bookport.BookportError):
            decode(bad)
    assert decode(good[:-3]).width == 13  # cut short: what is there, the rest blank


def _plucker_pages(records):
    """A Plucker file from raw data records (each with its 8-byte header); home is uid 2."""
    return pdb_bytes("Plucker test", b"Data", b"Plkr", [struct.pack(">HHHHH", 1, 1, 1, 0, 2), *records])


def test_plucker_images(tmp_path):
    red = [(200, 10, 10)]
    tile = _bitmap(3, 2, 8, _pixel_rows([[0, 0, 0]] * 2, 3, 8), palette=red)
    blue = _bitmap(2, 2, 8, _pixel_rows([[0, 0]] * 2, 2, 8), palette=[(10, 10, 200)])
    text = b"A picture: \x00\x1a\x00\x05 and a big one \x00\x5c\x00\x07\x00\x06 here."
    recs = [struct.pack(">HHHBB", 2, 1, len(text), 0, 0) + struct.pack(">HH", len(text), 0) + text,
            struct.pack(">HHHBB", 5, 0, len(tile), 2, 0) + tile,
            struct.pack(">HHHBB", 6, 0, len(blue), 3, 0) + palmdoc_compress(blue),
            struct.pack(">HHHBB", 8, 0, len(blue), 2, 0) + blue,
            struct.pack(">HHHBB", 7, 0, 8, 15, 0) + struct.pack(">HHHH", 2, 1, 5, 8)]  # a 2x1 grid: tile, then blue
    p = tmp_path / "pics.pdb"
    p.write_bytes(_plucker_pages(recs))
    b = bookport.read(p)
    imgs = [img.get("src") for img in b.tree(b.spine[0]).iter("img")]
    assert len(imgs) == 2
    from bookport.images import decode_png
    first = decode_png(b.resources["images/image00005.png"].data)
    assert (first.width, first.height) == (3, 2) and _rgb(first)[0][:3] == (200, 10, 10)
    shown = decode_png(b.resources["images/image00006.png"].data)  # 0x5C shows the second image in the text
    assert (shown.width, shown.height) == (2, 2) and _rgb(shown)[0][:3] == (10, 10, 200)
    assert "A picture:" in b.text() and "here." in b.text()
    # the composite, when a page shows it
    text2 = b"Grid \x00\x1a\x00\x07"
    recs[0] = struct.pack(">HHHBB", 2, 1, len(text2), 0, 0) + struct.pack(">HH", len(text2), 0) + text2
    p.write_bytes(_plucker_pages(recs))
    b = bookport.read(p)
    grid = decode_png(b.resources["images/image00007.png"].data)
    assert (grid.width, grid.height) == (5, 2)
    px = _rgb(grid)
    assert px[0][:3] == (200, 10, 10) and px[3][:3] == (10, 10, 200)


@pytest.mark.parametrize("fmt, kept", [
    ("rb", {"publisher": "Pure Tech Press", "isbn": "9780000000002"}),
    ("snb", {"publisher": "Pure Tech Press", "date": "2021-05-04", "language": "en", "description": "A book for testing every format."}),
    ("lrf", {"publisher": "Pure Tech Press", "date": "2021-05-04", "language": "en", "subjects": ["Fiction", "Testing"],
             "description": "A book for testing every format."}),
])
def test_rare_writers_keep_the_metadata_their_format_holds(book, tmp_path, fmt, kept):
    out = tmp_path / f"m.{fmt}"
    book.write(out)
    md = bookport.read(out).metadata
    got = {k: md.identifiers.get("isbn") if k == "isbn" else getattr(md, k) for k in kept}
    assert got == kept


def test_palm_bitmap_16_bit_transparent_colour():
    # version 2 gives the transparent colour as 8-bit RGB; pixels are 5-6-5, so it's matched in 5-6-5
    from bookport.palmbitmap import decode
    key = (0x80 >> 3) << 11 | (0x40 >> 2) << 5 | 0xC0 >> 3
    rows = [struct.pack(">HH", key, 0xFFFF)] * 2
    r = decode(_bitmap(2, 2, 16, rows, flags=0x0400 | 0x2000, direct=(0x80, 0x40, 0xC0)))
    assert r.mode == "RGBA" and [p[3] for p in _rgb(r)] == [0, 255, 0, 255]
