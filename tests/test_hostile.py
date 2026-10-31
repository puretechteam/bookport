"""Hostile files: each limit and safety check, with a file built here that broke bookport before it.

Files that used to hang or eat memory are read in a child process with a time limit, and report
how long they took and how much memory they used at most.
"""

import json
import os
import stat
import struct
import subprocess
import sys
import textwrap
import types
import zipfile
import zlib
from pathlib import Path

import pytest

import bookport
from bookport import BookportError, DRMError, limits, markup
from bookport.__main__ import main

from .conftest import png
from .test_pdf import BASE, make_pdf, stream
from .test_rare import _chm

MB = 1024 * 1024
SRC = str(Path(bookport.__file__).resolve().parent.parent)


def bounded(code: str, timeout: float = 30, trace: bool = True) -> dict:
    """Run ``code`` (which calls ``report(fn)``) in a fresh Python; returns
    {"error", "bookport", "message", "seconds", "peak_mb"} or {"error": "timeout"}.
    ``trace=False`` leaves tracemalloc off (it slows parsing several times over), for tests that
    only time the run; peak_mb is then 0."""
    prelude = textwrap.dedent(f"""
        import json, struct, sys, time, tracemalloc, zlib
        sys.path.insert(0, {SRC!r})
        sys.setrecursionlimit(1000)
        import bookport
        def report(fn):
            if {trace!r}:
                tracemalloc.start()
            t = time.perf_counter()
            out = {{"error": None, "bookport": True, "message": ""}}
            try:
                fn()
            except BaseException as e:
                out = {{"error": type(e).__name__, "bookport": isinstance(e, bookport.BookportError), "message": str(e)[:300]}}
            out["seconds"] = time.perf_counter() - t
            out["peak_mb"] = tracemalloc.get_traced_memory()[1] / 2**20
            print(json.dumps(out))
    """)
    try:
        p = subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)], capture_output=True, text=True, check=False,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "bookport": False, "seconds": timeout, "peak_mb": 0}
    lines = p.stdout.strip().splitlines()
    if not lines:
        return {"error": "crash", "bookport": False, "message": p.stderr[-500:], "seconds": 0, "peak_mb": 0}
    return json.loads(lines[-1])


def read_bounded(path, timeout: float = 30) -> dict:
    return bounded(f"report(lambda: bookport.read({str(path)!r}))", timeout)


def assert_quick_refusal_or_read(r: dict, seconds: float = 10, peak_mb: float = 300):
    assert r["error"] is None or r["bookport"], r
    assert r["seconds"] < seconds and r["peak_mb"] < peak_mb, r


@pytest.fixture
def small_limits(monkeypatch):
    """1 MB per part and 2 MB per book, so bombs can be small."""
    monkeypatch.setattr(limits, "MAX_PART", MB)
    monkeypatch.setattr(limits, "MAX_BOOK", 2 * MB)


BOMB = zlib.compress(b"\0" * (3 * MB), 9)  # 3 KB that unpacks to 3 MB


def _zip(path, files: dict, stored=()):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(zipfile.ZipInfo(name), data, zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED)
    return path


CONTAINER = (b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             b'<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
             b'</container>')


def _opf(items=b"", spine=b'<itemref idref="t"/>', head=b"", extra=b""):
    return (b'<?xml version="1.0"?>' + head + b'<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
            b'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
            b'<dc:identifier id="id">x</dc:identifier></metadata><manifest>'
            b'<item id="t" href="t.xhtml" media-type="application/xhtml+xml"/>' + items + b"</manifest><spine" + extra +
            b">" + spine + b"</spine></package>")


XHTML = b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body><p>Some words.</p></body></html>'


def _epub(path, files=None, opf=None):
    base = {"mimetype": b"application/epub+zip", "META-INF/container.xml": CONTAINER, "content.opf": opf or _opf(),
            "t.xhtml": XHTML}
    base.update(files or {})
    return _zip(path, base, stored=("mimetype",))


# --- 1. local files only from the book's folder and one level up --------------------------------

@pytest.fixture
def shelf(tmp_path):
    """outer/secret.png (two levels above the book: refused), outer/shelf/up.png (one up: allowed),
    outer/shelf/book/pics/own.png (the book's own folder)."""
    outer = tmp_path / "outer"
    folder = outer / "shelf" / "book"
    (folder / "pics").mkdir(parents=True)
    secret = png(9, 9, (1, 2, 3))
    (outer / "secret.png").write_bytes(secret)
    (outer / "other.html").write_text("<html><body><p>SECRET WORDS</p></body></html>")
    (outer / "shelf" / "up.png").write_bytes(png(10, 10, (4, 5, 6)))
    (folder / "pics" / "own.png").write_bytes(png(11, 11, (7, 8, 9)))
    return types.SimpleNamespace(outer=outer, folder=folder, secret=secret, abs=(outer / "secret.png").as_posix(),
                                 own=(folder / "pics" / "own.png").read_bytes(), up=(outer / "shelf" / "up.png").read_bytes())


def _datas(book):
    return [r.data for r in book.resources.values()]


def test_safe_path_rule(tmp_path):
    base = tmp_path / "a" / "b"
    base.mkdir(parents=True)
    for bad in ("\\\\server\\share\\x.png", "//server/share/x.png", "C:\\x.png", "C:x.png", "file:///C:/x.png", "file:x.png",
                "/etc/passwd", "../../x.png", "..\\..\\x.png", "..%2F..%2Fx.png", "a\0b", ""):
        assert limits.safe_path(base, bad) is None, bad
    assert limits.safe_path(base, "../x.png") == (tmp_path / "a" / "x.png").resolve()
    assert limits.safe_path(base, "pics/x.png#frag") == (base / "pics" / "x.png").resolve()
    assert limits.safe_path(base, "sub/../../x.png") == (tmp_path / "a" / "x.png").resolve()


def test_html_reads_only_nearby_files(shelf):
    (shelf.folder / "s.css").write_text(f"body {{ background: url(../../secret.png) }} h1 {{ background: url({shelf.abs}) }}")
    page = shelf.folder / "page.html"
    page.write_text(f"""<html><head><link rel="stylesheet" href="s.css"/>
        <link rel="stylesheet" href="{shelf.abs.replace("secret.png", "x.css")}"/>
        <style>p {{ background: url("{shelf.abs}") }} div {{ background: url(../../secret.png) }}</style></head>
        <body><h1>Title</h1><p><img src="pics/own.png"/><img src="../up.png"/><img src="../../secret.png"/>
        <img src="{shelf.abs}"/><img src="file:///{shelf.abs}"/><img src="..%2F..%2Fsecret.png"/></p>
        <p><a href="{(shelf.outer / "other.html").as_posix()}">x</a> <a href="../../other.html">y</a></p></body></html>""")
    book = bookport.read(page)
    datas = _datas(book)
    assert shelf.secret not in datas
    assert "SECRET WORDS" not in book.text()
    assert shelf.own in datas and shelf.up in datas


def test_markdown_reads_only_nearby_files(shelf):
    md = shelf.folder / "book.md"
    md.write_text(f"# T\n\n![a](../../secret.png) ![b]({shelf.abs}) ![c](pics/own.png) ![d](../up.png)\n")
    datas = _datas(bookport.read(md))
    assert shelf.secret not in datas and shelf.own in datas and shelf.up in datas


def test_pml_reads_only_nearby_files(shelf):
    (shelf.folder / "book_img").mkdir()
    (shelf.folder / "book_img" / "own.png").write_bytes(shelf.own)
    pml = shelf.folder / "book.pml"
    pml.write_text(f'\\p\\m="../../secret.png"\\m="{shelf.abs}"\\m="own.png"Words.\n', encoding="cp1252")
    datas = _datas(bookport.read(pml))
    assert shelf.secret not in datas and shelf.own in datas


def test_htmlz_cover_stays_inside_the_archive(shelf, tmp_path):
    opf = (b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata '
           b'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata><guide>'
           b'<reference type="cover" title="Cover" href="' + shelf.abs.encode() + b'"/></guide></package>')
    p = _zip(tmp_path / "x.htmlz", {"index.html": b"<html><body><p>Words.</p></body></html>", "metadata.opf": opf})
    book = bookport.read(p)
    assert shelf.secret not in _datas(book)


# --- 2. any damage is a BookportError --------------------------------------------------------------

DAMAGED = {
    "x.lit": b"ITOLITLS\x01\x00\x00\x00\x28\x00\x00\x00",
    "x.chm": b"ITSF\x03\x00\x00\x00",
    "x.lrf": b"L\0R\0F\0\0\0" + b"\0" * 20,
    "x.snb": b"SNBP000B" + b"\0" * 30,
    "x.rb": b"\xb0\x0c\xb0\x0c" + b"\0" * 12,
    "x.mobi": b"\0" * 60 + b"BOOKMOBI" + b"\0" * 8 + b"\0\x01" + b"\0\0\0\x60",
    "x.pdb": b"\0" * 60 + b"PNRdPPrs" + b"\0" * 8 + b"\0\x01" + b"\0\0\0\x50",
    "x.pdf": b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\ntrailer << /Root 1 0 R >>",
    "x.docx": b"",
}


@pytest.mark.parametrize("name", sorted(DAMAGED))
def test_damaged_files_raise_bookport_error(tmp_path, name):
    p = tmp_path / name
    if name == "x.docx":
        _zip(p, {})  # an empty ZIP
    else:
        p.write_bytes(DAMAGED[name])
    with pytest.raises(BookportError):
        bookport.read(p)


def _encrypted_member_zip(path):
    _zip(path, {"mimetype": b"application/epub+zip", "a.txt": b"x"}, stored=("mimetype", "a.txt"))
    data = bytearray(path.read_bytes())
    data[6] |= 1  # local header: encrypted
    cd = data.find(b"PK\x01\x02")
    data[cd + 8] |= 1  # central directory: encrypted
    path.write_bytes(bytes(data))
    return path


def test_detect_never_raises_anything_else(tmp_path):
    p = _encrypted_member_zip(tmp_path / "x.epub")
    try:
        bookport.detect(p)
    except BookportError:
        pass


def test_cli_prints_a_message_not_a_traceback(tmp_path, capsys):
    bad = tmp_path / "bad.lit"
    bad.write_bytes(DAMAGED["x.lit"])
    assert main([str(bad), str(tmp_path / "out.epub")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("bookport: ") and "Traceback" not in err


# --- 3. bounded decompression ----------------------------------------------------------------------

def test_pdf_flate_bomb(tmp_path, small_limits):
    objs = dict(BASE)
    objs[4] = stream(BOMB, b"/Filter /FlateDecode")
    p = tmp_path / "bomb.pdf"
    p.write_bytes(make_pdf(objs))
    with pytest.raises(BookportError, match="unpacks to more than"):
        bookport.read(p)


def test_mobi_font_bomb(small_limits):
    from bookport.fmt import mobi
    rec = b"FONT" + struct.pack(">IIIII", 3 * MB, 1, 24, 0, 0) + BOMB
    with pytest.raises(BookportError):
        mobi._font(rec)


def test_mobi_font_with_a_short_key():
    from bookport.fmt import mobi
    # a 16-byte XOR key that the record cuts to 2 bytes
    rec = b"FONT" + struct.pack(">IIIII", 10, 2, 24, 16, 32) + b"0123456789"
    assert mobi._font(rec) is not None


def test_rb_bomb(small_limits):
    from bookport.fmt import rb
    with pytest.raises(BookportError):
        rb._inflate(struct.pack("<II", 1, 0) + struct.pack("<I", len(BOMB)) + BOMB)


def test_snb_bomb(small_limits):
    from bookport.fmt import snb
    data = b"SNBP000B" + b"\0" * 12 + struct.pack(">IIIII", 1, 0, len(BOMB), 0, 0) + b"\0" * 4 + BOMB + b"\0" * 16
    with pytest.raises(BookportError, match="unpacks to more than"):
        snb._files(data)


def test_bz2_bomb(small_limits):
    import bz2
    with pytest.raises(BookportError):
        limits.bunzip(bz2.compress(b"\0" * (3 * MB)))


def test_ztxt_and_ereader_bombs(small_limits):
    from bookport.fmt import pdb
    ztxt = types.SimpleNamespace(records=[struct.pack(">HHI", 1, 1, 0) + b"\0" * 8, BOMB])
    with pytest.raises(BookportError):
        pdb._ztxt(ztxt, "t")
    ereader = types.SimpleNamespace(records=[struct.pack(">H", 10) + b"\0" * 130, BOMB])
    with pytest.raises(BookportError):
        pdb._ereader(ereader, "t")


def test_lrf_bomb(small_limits):
    from bookport.fmt import lrf
    obj = lrf.LrfObject(1, 1, b"\0" * 8)
    obj.stream, obj.stream_flags = b"\0\0\0\0" + BOMB, 0x100
    with pytest.raises(BookportError):
        obj.data()


def _png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _png(w, h, idat):
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) +
            _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b""))


def test_png_bomb(small_limits):
    from bookport import images
    with pytest.raises(BookportError):
        images.decode_png(_png(1000, 1000, BOMB))


def _huff_cdic(levels: int):
    """HUFF/CDIC where byte 0 is a phrase of two copies of the next phrase, ``levels`` deep: 2**levels bytes."""
    v = (255 << 8) | 0x80 | 8  # every first byte: an 8-bit terminal code
    huff = b"HUFF" + struct.pack(">III", 24, 24, 24 + 1024) + b"\0" * 8 + struct.pack(">256I", *[v] * 256) + b"\0" * 256
    phrases = []
    for k in range(256):
        j = 255 - k  # byte j decodes to phrase k
        if j < levels:
            phrases.append(struct.pack(">H", 2) + bytes([j + 1, j + 1]))
        else:
            phrases.append(struct.pack(">H", 0x8000 | 1) + b"x")
    offsets, body = [], b""
    for ph in phrases:
        offsets.append(512 + len(body))
        body += ph
    cdic = b"CDIC" + struct.pack(">III", 16, 256, 8) + struct.pack(">256H", *offsets) + body
    return huff, cdic


def test_huff_cdic_record_cap():
    from bookport.palm import HuffCdic
    huff, cdic = _huff_cdic(24)  # 16 MB from one byte
    with pytest.raises(BookportError):
        HuffCdic(huff, [cdic]).decompress(b"\0")


def _lzx_endless() -> bytes:
    """A verbatim LZX block whose only main-tree code is "0": an 8-byte match. Past the end of the
    input, the zero bits it reads decode as matches forever."""
    pairs = [(0, 1), (1, 3), (0xFF, 8), (0xFF, 8), (0xFF, 8)]
    pre = [(1 if s in (0, 16) else 0, 4) for s in range(20)]  # pre-tree: 0 = "same", 16 = "+1"
    pairs += pre + [(0, 1)] * 256
    pairs += pre + [(1 if i == 262 else 0, 1) for i in range(256, 256 + 30 * 8)]
    pairs += pre + [(0, 1)] * 249
    buf = n = 0
    out = bytearray()
    for value, width in pairs:
        buf = (buf << width) | value
        n += width
        while n >= 16:
            out += struct.pack("<H", (buf >> (n - 16)) & 0xFFFF)
            n -= 16
    if n:
        out += struct.pack("<H", (buf << (16 - n)) & 0xFFFF)
    return bytes(out)


def test_lzx_stops_when_the_input_ends():
    r = bounded(f"""
        from bookport.lzx import LzxDecoder
        def go():
            out = LzxDecoder(15).decompress({_lzx_endless()!r}, 3_000_000)
            if len(out) > 100_000:
                raise RuntimeError(f"{{len(out):,}} bytes from {len(_lzx_endless())} bytes of input")
        report(go)
    """)
    assert_quick_refusal_or_read(r, seconds=5)


@pytest.mark.parametrize("fmt", ["epub", "cbz", "docx", "odt", "fbz", "pmlz", "htmlz"])
def test_zip_member_over_the_limit(tmp_path, small_limits, fmt):
    big = b"\0" * (3 * MB)
    files = {
        "epub": {"t.xhtml": big},
        "cbz": {"001.png": png(), "002.png": big},
        "docx": {"[Content_Types].xml": b"<Types/>", "word/document.xml": big},
        "odt": {"mimetype": b"application/vnd.oasis.opendocument.text", "content.xml": big},
        "fbz": {"book.fb2": big},
        "pmlz": {"book.pml": big},
        "htmlz": {"index.html": big},
    }[fmt]
    p = tmp_path / f"x.{fmt}"
    _epub(p, files) if fmt == "epub" else _zip(p, files, stored=("mimetype",))
    with pytest.raises(BookportError, match="unpacks to more than"):
        bookport.read(p)


def test_zip_member_running_past_the_end(tmp_path):
    """A member whose stated compressed size is bigger than the whole file: Python before 3.12 set
    aside that many bytes before finding out (a 3 KB file asked for 257 MB on 3.10)."""
    p = tmp_path / "x.htmlz"
    _zip(p, {"index.html": b"<html><body><p>Words.</p></body></html>", "style.css": b"p { color: red }"})
    data = bytearray(p.read_bytes())
    cd = data.rindex(b"PK\x01\x02")  # the last member's central directory entry
    data[cd + 20:cd + 24] = struct.pack("<I", 200 * MB)  # its compressed size
    p.write_bytes(bytes(data))
    r = bounded(f"""
        import zipfile
        if "_end_offset" in zipfile.ZipInfo.__slots__:  # Python 3.12 added its own overlap check: test without it
            _orig = zipfile.ZipFile._RealGetContents
            def _as_before_312(self):
                _orig(self)
                for zi in self.filelist:
                    zi._end_offset = None
            zipfile.ZipFile._RealGetContents = _as_before_312
        report(lambda: bookport.read({str(p)!r}, "htmlz"))
    """)
    assert r["error"] is None or r["bookport"], r
    assert r["peak_mb"] < 20, r


def test_book_budget(tmp_path, small_limits):
    items = b"".join(b'<item id="i%d" href="i%d.css" media-type="text/css"/>' % (k, k) for k in range(4))
    files = {f"i{k}.css": b" " * (800 * 1024) for k in range(4)}
    p = _epub(tmp_path / "x.epub", files, opf=_opf(items))
    with pytest.raises(BookportError, match="This book unpacks to more than"):
        bookport.read(p)


def test_detect_reads_little_of_a_big_mimetype(tmp_path):
    import tracemalloc
    p = _zip(tmp_path / "x.zip", {"mimetype": b"a" * (8 * MB), "x.txt": b"words"})
    tracemalloc.start()
    bookport.detect(p)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < MB, peak


# --- 4. hangs --------------------------------------------------------------------------------------

def test_chm_zero_chunk_size(tmp_path):
    data = bytearray(_chm({"/a.htm": b"<html><body><p>x</p></body></html>"}))
    itsp = data.find(b"ITSP")
    data[itsp + 16:itsp + 20] = struct.pack("<I", 0)  # chunk size
    data[itsp + 32:itsp + 40] = struct.pack("<ii", -1, -1)
    data[itsp + 44:itsp + 48] = struct.pack("<I", 0x7FFFFFFF)  # chunks
    p = tmp_path / "x.chm"
    p.write_bytes(bytes(data))
    assert_quick_refusal_or_read(read_bounded(p))


def test_chm_huge_reset_table(tmp_path):
    rt = struct.pack("<IIII", 2, 0x7FFFFFFF, 8, 40) + struct.pack("<QQQ", 1 << 40, 10, 0x8000)
    control = struct.pack("<IIIIII", 6, 0x5A54534C, 2, 2, 2, 0)
    files = {"/a.htm": b"x", "::DataSpace/Storage/MSCompressed/Content": b"\0" * 64,
             "::DataSpace/Storage/MSCompressed/ControlData": control,
             "::DataSpace/Storage/MSCompressed/Transform/{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}/InstanceData/ResetTable": rt}
    data = _chm(files)
    # point the page at section 1
    data = data.replace(b"\x06/a.htm\x00", b"\x06/a.htm\x01", 1)
    p = tmp_path / "x.chm"
    p.write_bytes(data)
    assert_quick_refusal_or_read(read_bounded(p))


def _lit_header(npieces=2, chunk_size=0, nchunks=0x7FFFFFFF):
    hlen = 40
    ifcm = b"IFCM" + struct.pack("<II", 1, chunk_size) + b"\0" * 12 + struct.pack("<I", nchunks) + b"\0" * 4
    pieces_at = hlen
    dir_at = pieces_at + 16 * 2
    head = b"ITOLITLS" + struct.pack("<III", 1, hlen, npieces) + b"\0" * (hlen - 20)
    pieces = struct.pack("<IIII", 0, 0, 0, 0) + struct.pack("<IIII", dir_at, 0, len(ifcm), 0)
    return head + pieces + ifcm


@pytest.mark.parametrize("kind", ["chunks", "pieces"])
def test_lit_huge_counts(tmp_path, kind):
    data = _lit_header() if kind == "chunks" else _lit_header(npieces=0x7FFFFFFF, chunk_size=64, nchunks=1)
    p = tmp_path / "x.lit"
    p.write_bytes(data)
    assert_quick_refusal_or_read(read_bounded(p))


def test_mobi_tagx_length():
    r = bounded("""
        from bookport.mobi_index import parse_tagx
        report(lambda: parse_tagx(b"TAGX" + struct.pack(">II", 0xFFFFFFF0, 1) + b"\\x01\\x01\\x01\\x00", 0))
    """)
    assert_quick_refusal_or_read(r, seconds=5)


def _pdf_file(tmp_path, objs, name="x.pdf", **kw):
    p = tmp_path / name
    p.write_bytes(make_pdf(objs, **kw))
    return p


def test_pdf_xref_stream_with_zero_widths(tmp_path):
    xref = stream(b"", b"/Type /XRef /W [0 0 0] /Index [0 2000000000] /Size 2000000000 /Root 1 0 R")
    data = b"%PDF-1.5\n1 0 obj << /Type /Catalog >> endobj\n"
    start = len(data) + 1
    data += b"\n" + b"2 0 obj " + xref + b" endobj\nstartxref\n%d\n%%%%EOF\n" % start
    p = tmp_path / "x.pdf"
    p.write_bytes(data)
    assert_quick_refusal_or_read(read_bounded(p))


def test_pdf_form_that_draws_itself(tmp_path):
    objs = dict(BASE)
    objs[3] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 4 0 R "
               b"/Resources << /XObject << /Fm1 6 0 R >> >> >>")
    objs[4] = stream(b"/Fm1 Do")
    objs[6] = stream(b"/Fm1 Do " * 50, b"/Type /XObject /Subtype /Form /BBox [0 0 1 1] "
                                        b"/Resources << /XObject << /Fm1 6 0 R >> >>")
    assert_quick_refusal_or_read(read_bounded(_pdf_file(tmp_path, objs)))


def test_pdf_forms_that_fan_out(tmp_path):
    objs = dict(BASE)
    objs[3] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 4 0 R "
               b"/Resources << /XObject << /Fm 10 0 R >> >> >>")
    objs[4] = stream(b"/Fm Do")
    for k in range(10, 16):  # each form draws the next 40 times: 40**6 draws
        nxt = b"/Resources << /XObject << /Fm %d 0 R >> >>" % (k + 1) if k < 15 else b""
        objs[k] = stream(b"/Fm Do " * 40 if k < 15 else b"0 0 m", b"/Type /XObject /Subtype /Form " + nxt)
    assert_quick_refusal_or_read(read_bounded(_pdf_file(tmp_path, objs)))


def test_pdf_predictor_columns(tmp_path):
    objs = dict(BASE)
    objs[4] = stream(zlib.compress(b"\2" * 100), b"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 1000000000 /Colors 3 >>")
    assert_quick_refusal_or_read(read_bounded(_pdf_file(tmp_path, objs)))


def test_pdf_huge_image(tmp_path):
    objs = dict(BASE)
    objs[3] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 4 0 R "
               b"/Resources << /XObject << /Im1 6 0 R >> >> >>")
    objs[4] = stream(b"q 100 0 0 100 0 0 cm /Im1 Do Q")
    objs[6] = stream(zlib.compress(b"\0" * 100), b"/Type /XObject /Subtype /Image /Width 60000 /Height 60000 "
                                                 b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode")
    assert_quick_refusal_or_read(read_bounded(_pdf_file(tmp_path, objs)))


def test_png_pixel_limit():
    r = bounded(f"""
        from bookport import images
        report(lambda: images.decode_png({_png(20000, 20000, zlib.compress(b"", 9))!r}))
    """)
    assert_quick_refusal_or_read(r)
    assert r["bookport"] and r["error"], r


def test_deeply_nested_html(tmp_path):
    p = tmp_path / "deep.html"
    p.write_text("<html><body>" + "<div>" * 100_000 + "<p>Deep words.</p>" + "</div>" * 100_000 + "</body></html>")
    r = bounded(f"""
        def go():
            book = bookport.read({str(p)!r})
            assert "Deep words." in book.text(), book.text()[:100]
            book.write({str(tmp_path / "deep.epub")!r})
        report(go)
    """, timeout=60, trace=False)
    assert r["error"] is None, r
    assert r["seconds"] < 15, r


def test_deeply_nested_xhtml_through_xml_parser():
    text = '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>' + "<div>" * 5000 + "x" + "</div>" * 5000
    root = markup.parse(text + "</body></html>")
    depth, stack = 0, [(root, 0)]
    while stack:
        el, d = stack.pop()
        depth = max(depth, d)
        stack.extend((c, d + 1) for c in el)
    assert depth <= limits.MAX_DEPTH + 2
    assert "x" in markup.text_of(root)


def test_serialize_is_not_recursive():
    import xml.etree.ElementTree as ET
    root = ET.Element("html")
    el = ET.SubElement(root, "body")
    for _ in range(5000):
        el = ET.SubElement(el, "span")
    el.text = "end"
    assert markup.serialize(root).endswith(b"end" + b"</span>" * 5000 + b"</body></html>")


def test_toc_depth_is_capped(tmp_path):
    def nav(level):
        if level == 50:
            return b""
        return (b'<navPoint id="n%d" playOrder="%d"><navLabel><text>L%d</text></navLabel><content src="t.xhtml"/>' %
                (level, level + 1, level)) + nav(level + 1) + b"</navPoint>"
    ncx = (b'<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/>'
           b"<docTitle><text>T</text></docTitle><navMap>" + nav(0) + b"</navMap></ncx>")
    p = _epub(tmp_path / "x.epub", {"toc.ncx": ncx},
              opf=_opf(b'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>', extra=b' toc="ncx"'))
    book = bookport.read(p)
    assert max(d for d, _e in book.toc_walk()) < limits.MAX_TOC_DEPTH
    assert len(list(book.toc_walk())) == 50
    book.write(tmp_path / "back.epub")


def test_markdown_emphasis_does_not_backtrack():
    r = bounded("""
        from bookport.fmt import markdown
        report(lambda: markdown.to_html("**a " * 30000 + "\\n\\n" + "_b " * 30000 + "\\n\\n" + "~~c " * 30000))
    """)
    assert r["error"] is None, r
    assert r["seconds"] < 5, r


def test_serialize_validates_names():
    import xml.etree.ElementTree as ET
    root = ET.Element("html")
    body = ET.SubElement(root, "body")
    p = ET.SubElement(body, 'p onclick="alert(1)"', {'x" onload="y': "1", "ok": "2", "1bad": "3"})
    p.text = "text"
    out = markup.serialize(root)
    parsed = ET.fromstring(out)
    assert b"onclick" not in out and b"onload" not in out
    assert "text" in "".join(parsed.itertext())


def test_xml_internal_subset_is_refused(tmp_path):
    root = markup.parse('<?xml version="1.0"?><!DOCTYPE html [<!ENTITY x "EXPANDED">]>'
                        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>&x;</p></body></html>')
    assert "EXPANDED" not in markup.text_of(root)
    lol = b'<!DOCTYPE package [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    p = _epub(tmp_path / "x.epub", opf=_opf(head=lol).replace(b"<dc:title>T", b"<dc:title>&b;"))
    with pytest.raises(BookportError):
        bookport.read(p)


# --- 5. bugs -------------------------------------------------------------------------------------

def test_chm_title_from_the_first_page(tmp_path):
    p = tmp_path / "help.chm"
    p.write_bytes(_chm({"/a.htm": b"<html><head><title>Real Title</title></head><body><p><a href='b.htm'>next</a></p>"
                                  b"</body></html>",
                        "/b.htm": b"<html><head><title>Second</title></head><body><p>two</p></body></html>"}))
    assert bookport.read(p).metadata.title == "Real Title"


def test_output_has_normal_permissions(book, tmp_path):
    normal = tmp_path / "normal.txt"
    normal.write_text("x")
    out = book.write(tmp_path / "out.txt")
    assert stat.S_IMODE(os.stat(out).st_mode) == stat.S_IMODE(os.stat(normal).st_mode)


def test_pdf_detected_only_at_the_start(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_bytes(b"Every PDF starts with %PDF-1.7 and a binary comment.\n")
    assert bookport.detect(txt) == "txt"
    mac = tmp_path / "old"
    mac.write_bytes(b"\0\x08old.pdf" + b"\0" * 118 + b"%PDF-1.3\n")
    assert bookport.detect(mac) == "pdf"


def test_lit_odd_package_entries(book, tmp_path, monkeypatch):
    from bookport.fmt import lit
    p = book.write(tmp_path / "x.lit")
    real = lit._package

    def odd(data):
        pkg = real(data)
        import xml.etree.ElementTree as ET
        img = next((e.get("id") for e in pkg.iter("item") if e.get("media-type", "").startswith("image/")), None)
        css = next((e.get("id") for e in pkg.iter("item") if "css" in e.get("media-type", "")), None)
        guide = next(pkg.iter("guide"), None)
        if guide is None:
            guide = ET.SubElement(pkg, "guide")
        ET.SubElement(guide, "reference", {"type": "cover", "href": f"\x02{img}#frag"})
        spine = next(pkg.iter("spine"))
        ET.SubElement(spine, "itemref", {"idref": css or "nothing"})
        return pkg

    monkeypatch.setattr(lit, "_package", odd)
    assert bookport.read(p).spine


def test_rights_xml_alone_is_not_drm(tmp_path):
    p = _epub(tmp_path / "x.epub", {"META-INF/rights.xml": b'<?xml version="1.0"?><rights><license>CC BY 4.0</license></rights>'})
    assert "Some words." in bookport.read(p).text()
    adept = _epub(tmp_path / "adept.epub", {"META-INF/rights.xml": b'<rights xmlns="http://ns.adobe.com/adept"/>'})
    with pytest.raises(DRMError):
        bookport.read(adept)


@pytest.mark.parametrize("flag", ["--page-size", "--text-size"])
def test_cli_checks_pdf_sizes(book, tmp_path, capsys, flag):
    src = book.write(tmp_path / "a.epub")
    with pytest.raises(SystemExit) as e:
        main([str(src), str(tmp_path / "a.pdf"), flag, "huge"])
    assert e.value.code == 2 and "invalid choice" in capsys.readouterr().err


def test_unknown_write_options_are_refused(book, tmp_path):
    with pytest.raises(BookportError, match="colour"):
        book.write(tmp_path / "a.epub", colour="red")
    with pytest.raises(BookportError, match="page_size"):
        book.write(tmp_path / "a.mobi", page_size="a4")
    book.write(tmp_path / "a.pdf", page_size="a4", text_size="large")
    book.write(tmp_path / "b.epub", version=2)
    book.write(tmp_path / "a.pdb", format="ztxt")
