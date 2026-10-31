import os
import random
import struct

import pytest

import bookport
from bookport import DRMError
from bookport.fmt import mobi
from bookport.mobi_index import CNCX, build_index, read_index
from bookport.palm import (
    HuffCdic,
    encode_vwi_backward,
    encode_vwi_forward,
    palmdoc_compress,
    palmdoc_decompress,
    pdb_bytes,
    read_pdb,
    trailing_size,
    vwi_forward,
)

from .conftest import EBOOK_CONVERT, needs_calibre, recall, run, toc_titles


@pytest.mark.parametrize("data", [b"", b"a", b"hello world " * 400, os.urandom(4096), bytes(range(256)) * 16,
                                  "Grüße aus Köln, 紅樓夢, ελληνικά ".encode() * 120, b"\x00\x01\x08\x09\x80\xff" * 300])
def test_palmdoc_round_trip(data):
    assert palmdoc_decompress(palmdoc_compress(data)) == data


def test_palmdoc_compresses_text():
    text = (b"It is a truth universally acknowledged, that a single man in possession of a good fortune, "
            b"must be in want of a wife. ") * 30
    assert len(palmdoc_compress(text[:4096])) < 0.5 * 4096


def test_palmdoc_decoder_survives_garbage():
    rng = random.Random(1)
    for _ in range(200):
        palmdoc_decompress(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 300))))


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 16383, 16384, 2**21, 2**28 - 1])
def test_vwi(value):
    enc = encode_vwi_forward(value)
    assert vwi_forward(enc + b"junk", 0) == (value, len(enc))


@pytest.mark.parametrize("payload", [0, 1, 50, 126, 127, 200, 5000])
def test_trailing_entry(payload):
    size = payload + 1
    while payload + len(encode_vwi_backward(size)) != size:
        size += 1
    rec = b"TEXT" + b"x" * payload + encode_vwi_backward(size)
    assert trailing_size(rec, 0b10) == size


def test_trailing_multibyte():
    rec = b"text\xe2\x80" + b"\x02"  # two overlap bytes, then their count
    assert trailing_size(rec, 1) == 3


def test_index_round_trip():
    cncx = CNCX()
    entries = [(f"{i:02X}", {1: [i * 100], 2: [100], 3: [cncx.add(f"Chapter {i}")], 4: [0]}) for i in range(300)]
    tagx = [(1, 1, 1, 0), (2, 1, 2, 0), (3, 1, 4, 0), (4, 1, 8, 0), (0, 0, 0, 1)]
    records = build_index(entries, tagx, 1, cncx.finish())
    back, strings = read_index(records, 0)
    assert len(back) == 300
    assert back[5] == ("05", {1: [500], 2: [100], 3: [entries[5][1][3][0]], 4: [0]})
    assert strings[back[299][1][3][0]] == "Chapter 299"


def test_index_multi_value_tags():
    entries = [("SKEL0000000000", {1: [3, 3], 6: [0, 500, 0, 500]}), ("SKEL0000000001", {1: [1, 1], 6: [900, 70, 900, 70]})]
    records = build_index(entries, [(1, 1, 3, 0), (6, 2, 12, 0), (0, 0, 0, 1)], 1)
    assert read_index(records, 0)[0] == entries


def test_pdb_container():
    data = pdb_bytes("A Title: with/odd chars", b"BOOK", b"MOBI", [b"one", b"two", b"three"])
    pdb = read_pdb(data)
    assert pdb.records == [b"one", b"two", b"three"]
    assert pdb.type == b"BOOK" and pdb.creator == b"MOBI"
    assert b"/" not in pdb.name


def test_huffcdic_decoding():
    # a dictionary of four 2-bit codes: 00 -> "ab", 01 -> "cd", 10 -> "e ", 11 -> "fg"
    dict1 = [2 | 0x80 | (3 << 8)] * 256
    dict2 = [0, 0] * 32
    dict2[2] = 0
    dict2[3] = 3
    huff = b"HUFF" + struct.pack(">IIIII", 24, 24, 24 + 1024, 0, 0)[:20] + struct.pack(">256I", *dict1) + struct.pack(">64I", *dict2)
    phrases = [b"ab", b"cd", b"e ", b"fg"]
    offs = []
    body = b""
    for p in phrases:
        offs.append(len(phrases) * 2 + len(body))
        body += struct.pack(">H", len(p) | 0x8000) + p
    cdic = b"CDIC" + struct.pack(">III", 16, len(phrases), 2) + struct.pack(">4H", *offs) + body
    h = HuffCdic(huff, [cdic])
    # with every code's max at 3, code c picks phrase 3 - c: bits 11 10 01 00 -> "ab" "cd" "e " "fg"
    assert h.decompress(bytes([0b11100100])) == b"abcde fg"


def _encrypted(book, tmp_path):
    out = tmp_path / "b.azw3"
    book.write(out)
    data = bytearray(out.read_bytes())
    pdb = read_pdb(bytes(data))
    first = struct.unpack(">I", data[78:82])[0]
    data[first + 12:first + 14] = struct.pack(">H", 2)
    del pdb
    p = tmp_path / "drm.azw3"
    p.write_bytes(bytes(data))
    return p


def test_drm_is_refused(book, tmp_path):
    with pytest.raises(DRMError) as e:
        bookport.read(_encrypted(book, tmp_path))
    assert "DRM" in str(e.value)


def test_kind_detection(book, tmp_path):
    book.write(tmp_path / "a.azw3")
    book.write(tmp_path / "a.mobi")
    assert mobi.kind((tmp_path / "a.azw3").read_bytes()) == "azw3"
    assert mobi.kind((tmp_path / "a.mobi").read_bytes()) == "mobi"
    assert bookport.detect(tmp_path / "a.azw3") == "azw3"


def test_joint_file_has_both_halves(book, tmp_path):
    out = tmp_path / "j.mobi"
    book.write(out)
    pdb = read_pdb(out.read_bytes())
    h6 = mobi.parse_header(pdb.records, 0)
    assert h6.version == 6 and 121 in h6.exth
    h8 = mobi.parse_header(pdb.records, h6.exth[121][0])
    assert h8.version == 8
    old = mobi._read_mobi6(pdb.records, h6)
    assert recall(book, old) > 0.99
    assert toc_titles(old) == toc_titles(book)
    assert old.cover is not None


def test_uncompressed_and_big_text(book, tmp_path):
    import xml.etree.ElementTree as ET

    from bookport import markup
    root, body = markup.new_document("Big", "en")
    for i in range(3000):
        ET.SubElement(body, "p").text = f"Paragraph {i} with some words, ünïcödé and 漢字 to split across records."
    book.add_document("text/big.xhtml", root)
    for compress in (True, False):
        out = tmp_path / f"big{compress}.azw3"
        book.write(out, compress=compress)
        back = bookport.read(out)
        assert recall(book, back) > 0.999


@needs_calibre
def test_calibre_reads_our_kindle_files(book, tmp_path):
    for fmt in ("mobi", "azw3"):
        out = tmp_path / f"c.{fmt}"
        book.write(out)
        epub = tmp_path / f"c.{fmt}.epub"
        run(EBOOK_CONVERT, out, epub)
        back = bookport.read(epub)
        assert recall(book, back) > 0.99, fmt
        assert toc_titles(back) == toc_titles(book), fmt
        assert back.cover is not None
        assert back.metadata.authors == book.metadata.authors


@needs_calibre
def test_we_read_calibres_kindle_files(book, tmp_path):
    src = tmp_path / "s.epub"
    book.write(src)
    for fmt, extra in (("mobi", ["--mobi-file-type", "both"]), ("mobi", ["--mobi-file-type", "old"]), ("azw3", [])):
        out = tmp_path / f"cal{len(extra)}.{fmt}"
        run(EBOOK_CONVERT, src, out, *extra)
        back = bookport.read(out)
        assert recall(book, back) > 0.98, (fmt, extra)
        assert [t for t in toc_titles(back) if t] [:3] == toc_titles(book)[:3] or len(toc_titles(back)) >= 3
        assert back.cover is not None
