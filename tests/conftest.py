import collections
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from bookport import Book, Metadata, TocEntry, markup
from bookport.images import Raster, encode_png


def _first(*candidates):
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


EBOOK_CONVERT = _first(shutil.which("ebook-convert"), "C:/Program Files/Calibre2/ebook-convert.exe")
needs_calibre = pytest.mark.skipif(EBOOK_CONVERT is None, reason="calibre isn't installed")

# epubcheck: set BOOKPORT_TOOLS to the tools.json written by benchmarks/fetch_corpus.py --tools
_TOOLS = {}
if os.environ.get("BOOKPORT_TOOLS") and Path(os.environ["BOOKPORT_TOOLS"]).is_file():
    _TOOLS = json.loads(Path(os.environ["BOOKPORT_TOOLS"]).read_text())
EPUBCHECK = _TOOLS.get("epubcheck") if _TOOLS.get("epubcheck") and Path(_TOOLS["epubcheck"]).is_file() else None
JAVA = _TOOLS.get("java")
needs_epubcheck = pytest.mark.skipif(EPUBCHECK is None, reason="epubcheck isn't set up (BOOKPORT_TOOLS)")

# a folder of real books (benchmarks/fetch_corpus.py), for the slower corpus tests
CORPUS = Path(os.environ["BOOKPORT_CORPUS"]) if os.environ.get("BOOKPORT_CORPUS") else None
needs_corpus = pytest.mark.skipif(CORPUS is None or not CORPUS.is_dir(), reason="BOOKPORT_CORPUS isn't set")


def png(w=40, h=30, color=(200, 30, 30)) -> bytes:
    return encode_png(Raster(w, h, "RGB", bytes(color) * (w * h)))


def gif_bytes(w=8, h=6) -> bytes:
    """A GIF with a two-colour checkerboard, LZW coded without compression (a clear code every
    254 codes keeps the code size at 9 bits)."""
    import struct
    pixels = [(x + y) % 2 for y in range(h) for x in range(w)]
    codes = [256]
    for k, p in enumerate(pixels):
        if k and k % 254 == 0:
            codes.append(256)
        codes.append(p)
    codes.append(257)
    bits = acc = 0
    data = bytearray()
    for c in codes:
        acc |= c << bits
        bits += 9
        while bits >= 8:
            data.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        data.append(acc & 0xFF)
    blocks = b"".join(bytes([len(data[i:i + 255])]) + bytes(data[i:i + 255]) for i in range(0, len(data), 255))
    palette = bytes([255, 0, 0, 0, 0, 255]) + bytes(3 * 254)
    return (b"GIF89a" + struct.pack("<HHBBB", w, h, 0xF7, 0, 0) + palette
            + b"\x2c" + struct.pack("<HHHHB", 0, 0, w, h, 0) + b"\x08" + blocks + b"\x00\x3b")


CHAPTERS = [
    ("Chapter One", ["It was a <b>bright</b> cold day in <i>April</i>, and the clocks were striking thirteen.",
                     "Winston Smith, his chin nuzzled into his breast, slipped quickly through the glass doors.",
                     'See also <a href="ch3.xhtml#notes">the notes</a> at the end.']),
    ("Chapter Two", ["The hallway smelt of boiled cabbage and old rag mats.",
                     "At one end of it a coloured poster, too large for indoor display, had been tacked to the wall."]),
    ("Chapter Three", ["Outside, even through the shut window-pane, the world looked cold.",
                       "Down in the street little eddies of wind were whirling dust and torn paper into spirals."]),
]


def sample_book(images=True) -> Book:
    md = Metadata(title="The Sample Book", authors=["Ada Writer", "Bo Second"], language="en",
                  identifiers={"uuid": "0b7f7a3e-1111-4222-8333-944455556666", "isbn": "9780000000002"},
                  publisher="Pure Tech Press", date="2021-05-04", description="A book for testing every format.",
                  subjects=["Fiction", "Testing"], series="Test Series", series_index=2.0)
    book = Book(md)
    book.add("styles/main.css", "p { text-indent: 1.5em; margin: 0 } h1 { text-align: center } .note { font-size: small }")
    if images:
        book.cover = book.add("images/cover.png", png(60, 90, (20, 60, 160))).href
        book.add("images/fig.png", png())
    for n, (title, paras) in enumerate(CHAPTERS, 1):
        root, body = markup.new_document(title, "en", css_hrefs=["../styles/main.css"])
        h = ET.SubElement(body, "h1", {"id": f"c{n}"})
        h.text = title
        for p in paras:
            body.append(markup.parse_fragment(f"<p>{p}</p>")[1][0])
        if n == 1:
            ul = ET.SubElement(body, "ul")
            for item in ("first item", "second item"):
                ET.SubElement(ul, "li").text = item
            table = ET.SubElement(body, "table")
            for row in (("Name", "Age"), ("Ann", "31")):
                tr = ET.SubElement(table, "tr")
                for cell in row:
                    ET.SubElement(tr, "td").text = cell
            h2 = ET.SubElement(body, "h2", {"id": "c1s1"})
            h2.text = "A Section"
            ET.SubElement(body, "p").text = "Text inside the section of chapter one."
            if images:
                div = ET.SubElement(body, "div")
                ET.SubElement(div, "img", {"src": "../images/fig.png", "alt": "a figure"})
        if n == 3:
            p = ET.SubElement(body, "p", {"id": "notes", "class": "note"})
            p.text = "These are the notes."
        book.add_document(f"text/ch{n}.xhtml", root)
    book.toc = [TocEntry("Chapter One", "text/ch1.xhtml#c1", [TocEntry("A Section", "text/ch1.xhtml#c1s1")]),
                TocEntry("Chapter Two", "text/ch2.xhtml#c2"),
                TocEntry("Chapter Three", "text/ch3.xhtml#c3")]
    return book


def words(text: str) -> collections.Counter:
    return collections.Counter(re.findall(r"\w+", text.lower()))


def recall(a: Book, b: Book) -> float:
    wa, wb = words(a.text()), words(b.text())
    return sum((wa & wb).values()) / max(1, sum(wa.values()))


def toc_titles(book: Book) -> list[str]:
    return [" ".join(e.title.split()) for _d, e in book.toc_walk()]


def run(*args, ok=True) -> subprocess.CompletedProcess:
    r = subprocess.run([str(a) for a in args], capture_output=True, timeout=600, check=False)
    if ok:
        assert r.returncode == 0, (args, r.stdout.decode(errors="replace")[-2000:], r.stderr.decode(errors="replace")[-2000:])
    return r


def epubcheck_errors(path: Path) -> list[str]:
    r = run(JAVA or "java", "-jar", EPUBCHECK, "-q", path, ok=False)
    text = (r.stdout + r.stderr).decode("utf-8", "replace")
    return [line for line in text.splitlines() if line.startswith(("ERROR", "FATAL"))]


@pytest.fixture
def book() -> Book:
    return sample_book()
