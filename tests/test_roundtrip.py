"""Write the sample book to every format, read it back, and check what each format can keep."""

import pytest

import bookport
from bookport import formats

from .conftest import CHAPTERS, epubcheck_errors, needs_epubcheck, recall, toc_titles

# what each output format keeps: TOC (all, top or none), images, cover, metadata fields
KEEPS = {
    "epub": {"toc": "all", "images": True, "cover": True, "meta": ("title", "authors", "language", "publisher", "date",
                                                                   "description", "subjects", "series")},
    "mobi": {"toc": "all", "images": True, "cover": True, "meta": ("title", "authors", "language", "publisher", "date",
                                                                   "description", "subjects")},
    "azw": {"toc": "all", "images": True, "cover": True, "meta": ("title", "authors", "language", "publisher")},
    "azw3": {"toc": "all", "images": True, "cover": True, "meta": ("title", "authors", "language", "publisher", "date",
                                                                   "description", "subjects")},
    "fb2": {"toc": "all", "images": True, "cover": True, "meta": ("title", "authors", "language", "publisher", "date",
                                                                  "description", "series")},
    "fbz": {"toc": "all", "images": True, "cover": True, "meta": ("title", "authors", "language")},
    "htmlz": {"toc": "headings", "images": True, "cover": True, "meta": ("title", "authors", "language", "publisher",
                                                                         "date", "description", "subjects", "series")},
    "txtz": {"toc": "headings", "images": True, "cover": True, "meta": ("title", "authors", "language", "publisher",
                                                                        "date", "description", "subjects", "series")},
    "html": {"toc": "headings", "images": True, "cover": False, "meta": ("title", "authors", "language", "description")},
    "md": {"toc": "headings", "images": False, "cover": False, "meta": ("title", "authors", "language", "date")},
    "txt": {"toc": "none", "images": False, "cover": False, "meta": ()},
    "docx": {"toc": "headings", "images": True, "cover": True, "meta": ("title", "authors", "language", "description",
                                                                        "subjects")},
    "rtf": {"toc": "headings", "images": True, "cover": True, "meta": ("title", "authors", "publisher", "date",
                                                                       "description", "subjects")},
}


def _writable():
    return [ext for ext, info in formats().items() if info["write"]]


def test_formats_lists_reads_and_writes():
    f = formats()
    for ext in ("epub", "mobi", "azw3", "fb2", "html", "htmlz", "txt", "txtz", "md"):
        assert f[ext]["read"] and f[ext]["write"], ext
    assert all(set(v) == {"name", "read", "write"} for v in f.values())


@pytest.mark.parametrize("fmt", [k for k in KEEPS])
def test_round_trip(book, tmp_path, fmt):
    suffix = ".fb2.zip" if fmt == "fbz" else f".{fmt}"
    out = tmp_path / f"book{suffix}"
    book.write(out, fmt)
    assert out.stat().st_size > 0
    back = bookport.read(out)
    keeps = KEEPS[fmt]
    assert recall(book, back) >= 0.99, fmt
    titles = toc_titles(back)
    if keeps["toc"] == "all":
        assert titles == toc_titles(book)
    elif keeps["toc"] == "headings":
        for title, _ in CHAPTERS:
            assert title in titles
    if keeps["images"]:
        assert len(back.images()) >= 1
    if keeps["cover"]:
        assert back.cover is not None
        assert back.resources[back.cover].data[:4] == b"\x89PNG"
    md, md2 = book.metadata, back.metadata
    for field in keeps["meta"]:
        assert getattr(md2, field), (fmt, field)
        if field in ("title", "authors", "language", "publisher", "series"):
            assert getattr(md2, field) == getattr(md, field), (fmt, field)


def test_every_writable_format_writes(book, tmp_path):
    for fmt in _writable():
        if fmt in ("fbz",):
            continue
        out = tmp_path / f"b.{fmt}"
        if fmt == "cbz":  # a comic archive holds only pictures: a book of text is refused (test_office has comics)
            with pytest.raises(bookport.BookportError, match="only pictures"):
                book.write(out, fmt)
            continue
        book.write(out, fmt)
        assert out.stat().st_size > 0, fmt


def test_convert(book, tmp_path):
    src = tmp_path / "a.epub"
    book.write(src)
    dst = bookport.convert(src, tmp_path / "a.fb2")
    assert bookport.read(dst).metadata.title == "The Sample Book"


def test_failed_write_leaves_nothing(book, tmp_path, monkeypatch):
    from bookport.fmt import fb2

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(fb2, "to_bytes", boom)
    with pytest.raises(RuntimeError):
        book.write(tmp_path / "x.fb2")
    assert list(tmp_path.iterdir()) == []


def test_unknown_format(book, tmp_path):
    with pytest.raises(bookport.BookportError):
        book.write(tmp_path / "x.xyz")
    p = tmp_path / "missing.epub"
    with pytest.raises(bookport.BookportError):
        bookport.read(p)


def test_internal_links_survive(book, tmp_path):
    for fmt in ("epub", "azw3", "mobi", "fb2", "htmlz"):
        out = tmp_path / f"l.{fmt}"
        book.write(out)
        back = bookport.read(out)
        found = False
        for href, _ in back.documents():
            for a in back.tree(href).iter("a"):
                if "the notes" in "".join(a.itertext()) and a.get("href"):
                    found = True
        assert found, fmt


def _tricky_epub(book, tmp_path):
    # what LIT and PDF sources bring: a guide entry that points at an image, two TOC entries on one target
    book.landmarks.append(bookport.Landmark("cover", "Cover image", next(iter(book.images())).href))
    book.toc.append(bookport.TocEntry("Again", book.toc[0].href))
    out = tmp_path / "t.epub"
    book.write(out)
    return out


def test_epub_guide_and_ncx_stay_valid(book, tmp_path):
    import re
    import zipfile
    z = zipfile.ZipFile(_tricky_epub(book, tmp_path))
    opf = z.read("OEBPS/content.opf").decode()
    assert not re.search(r'<reference [^>]*href="images/', opf)
    ncx = z.read("OEBPS/toc.ncx").decode()
    orders = {}
    for order, src in re.findall(r'playOrder="(\d+)">.*?<content src="([^"]+)"', ncx, re.DOTALL):
        assert orders.setdefault(src, order) == order, src


@needs_epubcheck
def test_epubcheck_passes(book, tmp_path):
    assert epubcheck_errors(_tricky_epub(book, tmp_path)) == []


TABLES = ('<table><col class="a"><col class="b"><tbody><tr><th>Release:</th><td>3.10</td></tr></tbody></table>'
          '<table><tr><td>loose row</td></tr><tbody><tr><td>in a body</td></tr></tbody><tr><td>loose again</td></tr></table>')


def _tables_after_cleaning(tmp_path):
    src = tmp_path / "t.html"
    src.write_text(f"<html><body><p>Tables:</p>{TABLES}</body></html>", encoding="utf-8")
    out = bookport.read(src).write(tmp_path / "t.epub")
    back = bookport.read(out)
    return out, [t for href in back.spine for t in back.tree(href).iter("table")]


def test_bare_cols_and_loose_rows_make_a_valid_table(tmp_path):
    # Sphinx (Python's documentation) puts <col> straight in <table>; they were wrapped in rows and cells
    _out, (first, second) = _tables_after_cleaning(tmp_path)
    assert [c.tag for c in first] == ["colgroup", "tbody"]
    assert [c.get("class") for c in first.find("colgroup")] == ["a", "b"]
    assert [c.tag for c in second] == ["tbody", "tbody", "tbody"]  # rows never sit beside a tbody
    assert "".join(second.itertext()) == "loose rowin a bodyloose again"


@needs_epubcheck
def test_bare_cols_and_loose_rows_pass_epubcheck(tmp_path):
    out, _tables = _tables_after_cleaning(tmp_path)
    assert epubcheck_errors(out) == []


def test_a_stray_percent_in_a_web_link_is_escaped(tmp_path):
    # Python's documentation links to "...r5rs-Z-H-9.html#%_sec_6.2", which isn't a valid URL in an EPUB
    src = tmp_path / "l.html"
    src.write_text('<html><body><p><a href="https://example.org/r5rs.html#%_sec_6.2">R5RS</a> '
                   '<a href="https://example.org/a%20b?q=100%25">ok</a></p></body></html>', encoding="utf-8")
    back = bookport.read(bookport.read(src).write(tmp_path / "l.epub"))
    hrefs = [a.get("href") for h in back.spine for a in back.tree(h).iter("a")]
    assert hrefs == ["https://example.org/r5rs.html#%25_sec_6.2", "https://example.org/a%20b?q=100%25"]
