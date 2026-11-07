"""ODT writing: the package, its parts, what bookport reads back, and (when installed) what LibreOffice keeps."""

import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

import bookport
from bookport import markup, mathml
from bookport.fmt import odt

from .conftest import png

T = odt.NS["text"]
TABLE = odt.NS["table"]
DRAW = odt.NS["draw"]
MANIFEST = odt.NS["manifest"]

HTML = """<html><head><title>Features</title></head><body>
<h1 id="top">Features</h1>
<p>Plain <em>emphasis</em>, <strong>strong</strong>, <code>a  code</code>, <del>gone</del>, H<sub>2</sub>O, x<sup>2</sup>,
a <a href="https://example.org/">link</a> and an <a href="#tbl">internal link</a>. Inline
<math><msup><mi>e</mi><mrow><mi>i</mi><mi>π</mi></mrow></msup><mo>+</mo><mn>1</mn><mo>=</mo><mn>0</mn></math> here.</p>
<p><math display="block"><mfrac><mi>a</mi><mi>b</mi></mfrac></math></p>
<h2>Lists</h2>
<ul><li>First</li><li>Second<ul><li>Nested</li></ul></li></ul>
<ol start="3"><li>Three</li><li>Four</li></ol>
<h2 id="tbl">Table</h2>
<table><caption>A table</caption><thead><tr><th>Name</th><th>Value</th><th>Note</th></tr></thead>
<tbody><tr><td>alpha</td><td>1</td><td rowspan="2">two rows</td></tr><tr><td>beta</td><td>2</td></tr>
<tr><td colspan="2">wide</td><td>end</td></tr></tbody></table>
<pre><code>for i in range(3):
    print(i)   # spaced</code></pre>
<blockquote><p>Quoted.</p></blockquote>
<p>A note.<a class="noteref" epub:type="noteref" href="#fn1"><sup>1</sup></a></p>
<p><img src="fig.png" alt="red box"/></p>
<aside epub:type="footnote" id="fn1"><p>The footnote text.</p></aside>
</body></html>"""


@pytest.fixture
def written(tmp_path):
    (tmp_path / "fig.png").write_bytes(png())
    src = tmp_path / "f.html"
    src.write_text(HTML, encoding="utf-8")
    book = bookport.read(src)
    out = tmp_path / "f.odt"
    book.write(out)
    return book, out


def test_package(written):
    _, out = written
    data = out.read_bytes()
    assert data[30:38] == b"mimetype" and data[38:38 + 39] == b"application/vnd.oasis.opendocument.text"
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
        names = set(z.namelist())
        manifest = ET.fromstring(z.read("META-INF/manifest.xml"))
        listed = {e.get(f"{{{MANIFEST}}}full-path") for e in manifest}
        assert {"/", "content.xml", "styles.xml", "meta.xml"} <= listed
        for name in listed - {"/"}:
            assert name in names or name.endswith("/"), name
        for name in names - {"mimetype", "META-INF/manifest.xml"}:
            assert name in listed, name
        for part in ("content.xml", "styles.xml", "meta.xml"):
            ET.fromstring(z.read(part))  # well-formed
        pictures = [n for n in names if n.startswith("Pictures/")]
        assert len(pictures) == 1 and z.read(pictures[0]) == png()
        objects = sorted(n for n in names if n.startswith("Object ") and n.endswith("/content.xml"))
        assert objects == ["Object 1/content.xml", "Object 2/content.xml"]
        for n in objects:
            m = ET.fromstring(z.read(n))  # one well-formed MathML document each (no text after it)
            assert m.tag == "{http://www.w3.org/1998/Math/MathML}math"
        content = ET.fromstring(z.read("content.xml"))
        meta = z.read("meta.xml").decode()
    assert "<dc:title>Features</dc:title>" in meta
    heads = [(h.get(f"{{{T}}}outline-level"), "".join(h.itertext())) for h in content.iter(f"{{{T}}}h")]
    assert heads == [("1", "Features"), ("2", "Lists"), ("2", "Table")]
    assert len(list(content.iter(f"{{{T}}}list"))) == 3
    assert content.find(f".//{{{T}}}list-item[@{{{T}}}start-value='3']") is not None
    table = content.find(f".//{{{TABLE}}}table")
    rows = table.findall(f".//{{{TABLE}}}table-row")
    assert [len(r) for r in rows] == [3, 3, 3, 3]  # cells plus covered cells fill every row
    assert table.find(f".//{{{TABLE}}}table-cell[@{{{TABLE}}}number-rows-spanned='2']") is not None
    assert table.find(f".//{{{TABLE}}}table-cell[@{{{TABLE}}}number-columns-spanned='2']") is not None
    assert len(content.findall(f".//{{{T}}}note")) == 1
    assert len(content.findall(f".//{{{DRAW}}}object")) == 2
    code = [p for p in content.iter(f"{{{T}}}p") if p.get(f"{{{T}}}style-name") == "Preformatted_20_Text"]
    assert len(code) == 2 and code[1].find(f"{{{T}}}s").get(f"{{{T}}}c") == "4"
    links = [a.get("{http://www.w3.org/1999/xlink}href") for a in content.iter(f"{{{T}}}a")]
    assert links[0] == "https://example.org/" and links[1].startswith("#")
    bookmarks = {b.get(f"{{{T}}}name") for b in content.iter(f"{{{T}}}bookmark")}
    assert links[1][1:] in bookmarks


def _counts(book):
    text = "".join(markup.to_string(book.tree(h).find("body")) for h, _ in book.documents())
    return {t: len(re.findall(rf"<{t}[ />]", text)) for t in ("h1", "h2", "li", "table", "td", "th", "img", "math",
                                                              "pre", "aside", "blockquote", "a")}


def test_reads_back(written):
    book, out = written
    back = bookport.read(out)
    a, b = _counts(book), _counts(back)
    for k in ("h1", "li", "table", "img", "math", "pre", "aside", "blockquote"):
        assert a[k] == b[k], k
    assert b["h2"] == a["h2"] + 1  # the reader gathers the notes under a "Notes" heading
    assert a["td"] + a["th"] == b["td"] + b["th"]
    maths = [m for h, _ in back.documents() for m in back.tree(h).iter("math")]
    assert [m.get("display") for m in maths] == ["inline", "block"]
    assert mathml.to_tex(maths[1]) == r"\frac{a}{b}"
    text = " ".join(markup.text_of(back.tree(back.spine[0])).split())
    assert "Inline here." not in text and "here." in text
    pre = back.tree(back.spine[0]).find(".//pre")
    assert markup.text_of(pre) == "for i in range(3):\n    print(i)   # spaced"
    assert back.tree(back.spine[0]).find(".//ol").get("start") == "3"
    assert back.metadata.title == "Features"


def test_from_markdown_with_math(tmp_path):
    src = tmp_path / "m.md"
    src.write_text("# T\n\n$\\hat{x} = \\sum_{i=1}^n \\frac{a_i}{2}$ and\n\n$$\\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}$$\n",
                   encoding="utf-8")
    out = tmp_path / "m.odt"
    bookport.read(src).write(out)
    with zipfile.ZipFile(out) as z:
        first = z.read("Object 1/content.xml").decode()
    assert "\u2061" not in first and "ˆ" in first  # LibreOffice wants spacing accents, sets function names itself
    back = bookport.read(out)
    maths = [m for h, _ in back.documents() for m in back.tree(h).iter("math")]
    assert len(maths) == 2 and maths[1].find(".//mtable") is not None


def test_reads_libreoffice_formula_frames(tmp_path):
    """LibreOffice keeps a picture of each formula next to the object: the object is what counts."""
    content = (f'<office:document-content xmlns:office="{odt.NS["office"]}" xmlns:text="{T}" xmlns:draw="{DRAW}" '
               'xmlns:xlink="http://www.w3.org/1999/xlink"><office:body><office:text><text:p>Area '
               '<draw:frame><draw:object xlink:href="./Object 1"/><draw:image xlink:href="./ObjectReplacements/Object 1"/>'
               '</draw:frame> and <draw:frame><draw:object xlink:href="../../evil"/></draw:frame></text:p>'
               '</office:text></office:body></office:document-content>')
    p = tmp_path / "lo.odt"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", odt.MIMETYPE)
        z.writestr("content.xml", content)
        z.writestr("Object 1/content.xml", '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><mi>π</mi>'
                                           '<msup><mi>r</mi><mn>2</mn></msup></math>')
        z.writestr("ObjectReplacements/Object 1", b"\x00\x01")
    book = bookport.read(p)
    maths = [m for h, _ in book.documents() for m in book.tree(h).iter("math")]
    assert len(maths) == 1 and maths[0].get("display") == "inline"
    assert mathml.to_tex(maths[0]) == r"\pi r^2"


SOFFICE = shutil.which("soffice") or next((p for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",)
                                           if Path(p).is_file()), None)


@pytest.mark.skipif(SOFFICE is None, reason="LibreOffice isn't installed")
def test_libreoffice_keeps_everything(written, tmp_path):
    """LibreOffice opens the file and saves it again from what it understood: nothing is lost."""
    _, out = written
    lo = tmp_path / "lo"
    lo.mkdir()
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([SOFFICE, "--headless", "--norestore", "--nolockcheck", "--nodefault",
                        f"-env:UserInstallation={Path(profile).as_uri()}", "--convert-to", "odt", "--outdir", str(lo),
                        str(out)], capture_output=True, timeout=300, check=False)
    resaved = lo / out.name
    assert resaved.is_file()
    assert _counts(bookport.read(resaved)) == _counts(bookport.read(out))
    with zipfile.ZipFile(resaved) as z:
        starmath = re.search(r'StarMath 5.0">(.*?)<', z.read("Object 2/content.xml").decode()).group(1)
    assert "frac" in starmath  # LibreOffice Math understood the equation
