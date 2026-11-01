"""The real-book corpus: public-domain books from Project Gutenberg and Standard Ebooks and Python's
documentation CHM, as benchmarks/fetch_corpus.py downloads them. Set BOOKPORT_CORPUS to that folder
(the one holding the .epub, .mobi, .azw3, .txt and .chm files) to run these; they take a few minutes.

    python benchmarks/fetch_corpus.py SCRATCH --tools
    BOOKPORT_CORPUS=SCRATCH/corpus BOOKPORT_TOOLS=SCRATCH/tools/tools.json pytest tests/test_corpus.py
"""

import shutil
import subprocess
from pathlib import Path

import pytest

import bookport

from .conftest import CORPUS, epubcheck_errors, needs_corpus, needs_epubcheck, recall, toc_titles

pytestmark = needs_corpus

BOOKS = sorted(CORPUS.glob("*.epub")) if CORPUS is not None and CORPUS.is_dir() else []
# every writable format but CBZ, which takes only books made of pictures
WRITES = sorted(f for f, v in bookport.formats().items() if v["write"] and f != "cbz")
# words a format may lose from the worst book: alt text, and text in pictures or tables some formats can't hold
RECALL = {"pdf": 0.97, "tcr": 0.97, "txt": 0.97, "txtz": 0.97, "md": 0.97, "pdb": 0.97, "rb": 0.97, "lrf": 0.97}
# Gutenberg's MOBI of Hong Lou Meng holds 115,664 of its EPUB's 129,768 words; calibre reads the same 115,664
KINDLE_RECALL = {"hong-lou-meng.mobi": 0.89}
# formats that carry a TOC bookport reads back
TOC = {"epub", "mobi", "azw3", "fb2", "docx", "pdf", "lit", "lrf", "snb", "html"}


def _first_copy(stem: str, ext: str) -> Path | None:
    p = CORPUS / f"{stem}.{ext}"
    return p if p.is_file() else None


def test_the_corpus_is_there():
    assert len(BOOKS) >= 10, f"expected the corpus's EPUBs in {CORPUS}"


@pytest.mark.parametrize("ext", ["epub", "mobi", "azw3", "txt"])
def test_every_book_reads(ext):
    files = sorted(CORPUS.glob(f"*.{ext}"))
    assert files
    for f in files:
        b = bookport.read(f)
        assert b.word_count() > 5000, f.name
        assert b.metadata.title, f.name
        assert b.spine, f.name


def test_kindle_copies_hold_the_epubs_words():
    checked = 0
    for epub in BOOKS:
        source = bookport.read(epub)
        for ext in ("mobi", "azw3"):
            kindle = _first_copy(epub.stem, ext)
            if kindle is None:
                continue
            # Gutenberg makes its Kindle files from the same text: the same words, a few links and notes aside
            assert recall(source, bookport.read(kindle)) > KINDLE_RECALL.get(kindle.name, 0.97), kindle.name
            checked += 1
    assert checked >= 10


@pytest.mark.parametrize("fmt", WRITES)
def test_every_book_round_trips(fmt, tmp_path):
    for epub in BOOKS:
        source = bookport.read(epub)
        out = source.write(tmp_path / f"{epub.stem}.{fmt}")
        back = bookport.read(out)
        assert recall(source, back) >= RECALL.get(fmt, 0.99), (epub.name, fmt, recall(source, back))
        if fmt in TOC and len(source.toc) > 1:
            assert len(toc_titles(back)) >= 0.9 * len(toc_titles(source)), (epub.name, fmt)
        out.unlink()


@needs_epubcheck
def test_epubs_made_from_every_input_pass_epubcheck(tmp_path):
    inputs = BOOKS + sorted(CORPUS.glob("*.mobi")) + sorted(CORPUS.glob("*.azw3")) + sorted(CORPUS.glob("*.txt"))
    for src in inputs:
        out = bookport.convert(src, tmp_path / f"{src.stem}-{src.suffix[1:]}.epub")
        assert epubcheck_errors(out) == [], src.name
        out.unlink()


def _chm() -> Path:
    chm = next(iter(sorted(CORPUS.glob("*.chm"))), None)
    if chm is None:
        pytest.skip("the corpus has no CHM")
    return chm


def test_the_chm_reads_with_its_whole_contents():
    b = bookport.read(_chm())
    assert len(b.spine) > 500
    assert len(toc_titles(b)) > 4000
    assert b.word_count() > 1_000_000


SEVEN_ZIP = shutil.which("7z") or next((p for p in ("C:/Program Files/7-Zip/7z.exe",) if Path(p).is_file()), None)


@pytest.mark.skipif(SEVEN_ZIP is None, reason="7-Zip isn't installed (it checks bookport.unpack)")
def test_unpacking_the_chm_gives_the_files_7zip_gives(tmp_path):
    chm = _chm()
    subprocess.run([SEVEN_ZIP, "x", "-y", f"-o{tmp_path / 'seven'}", str(chm)], check=True, capture_output=True)
    seven = {p.relative_to(tmp_path / "seven").as_posix(): p.read_bytes() for p in (tmp_path / "seven").rglob("*")
             if p.is_file()}
    ours = dict(bookport.unpack(chm))
    assert sorted(ours) == sorted(seven)
    assert [n for n in ours if ours[n] != seven[n]] == []
