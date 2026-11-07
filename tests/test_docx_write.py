"""DOCX writing: real Word lists, footnotes, code styles, table headers and captions, definition lists,
figures; and Markdown's implicit figures."""

import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

import bookport
from bookport import markup
from bookport.fmt.markdown import book_from_markdown

from .conftest import png

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

MD = """# Title

Text with `code`, a [relative link](/url/) and a note.[^1] Another note.[^2]

- one
- two
    - nested

1. first
2. second

Then:

7. seven
8. eight

- loose item

  with a second paragraph

- another

```python
x = 1
```

Term
:   Its definition.

| h1 | h2 |
|----|----|
| a  | b  |

![A red box](fig.png)

[^1]: The first note.
[^2]: The second note, with `code`.
"""


@pytest.fixture
def docx(tmp_path):
    (tmp_path / "fig.png").write_bytes(png())
    src = tmp_path / "s.md"
    src.write_text(MD, encoding="utf-8")
    book = bookport.read(src)
    out = tmp_path / "s.docx"
    book.write(out)
    with zipfile.ZipFile(out) as z:
        parts = {n: z.read(n).decode("utf-8") for n in z.namelist() if n.endswith((".xml", ".rels"))}
    return book, out, parts


def _paras(xml):
    return ET.fromstring(xml).iter(f"{{{W}}}p")


def _style(p):
    s = p.find(f"{{{W}}}pPr/{{{W}}}pStyle")
    return s.get(f"{{{W}}}val") if s is not None else ""


def _text(p):
    return "".join(t.text or "" for t in p.iter(f"{{{W}}}t"))


def test_package_parts(docx):
    _, _, parts = docx
    for name in ("word/document.xml", "word/numbering.xml", "word/footnotes.xml", "word/styles.xml", "word/settings.xml"):
        ET.fromstring(parts[name])
    assert "numbering.xml" in parts["word/_rels/document.xml.rels"]
    assert "footnotes.xml" in parts["word/_rels/document.xml.rels"]
    types = parts["[Content_Types].xml"]
    assert "numbering+xml" in types and "footnotes+xml" in types
    styles = parts["word/styles.xml"]
    for name in ("Source Code", "Verbatim Char", "Footnote Text", "Footnote Reference", "Table Caption",
                 "Image Caption", "Captioned Figure", "Definition Term", "Definition", "Compact"):
        assert f'w:val="{name}"' in styles, name


def test_lists(docx):
    _, _, parts = docx
    items = []
    for p in _paras(parts["word/document.xml"]):
        num = p.find(f"{{{W}}}pPr/{{{W}}}numPr")
        if num is not None:
            items.append((_text(p), num.find(f"{{{W}}}ilvl").get(f"{{{W}}}val"), num.find(f"{{{W}}}numId").get(f"{{{W}}}val"),
                          _style(p)))
    texts = [t for t, *_ in items]
    assert texts == ["one", "two", "nested", "first", "second", "seven", "eight", "loose item", "another"]
    assert [lvl for _, lvl, *_ in items[:3]] == ["0", "0", "1"]
    assert len({n for _, _, n, _ in items}) == 5  # every list is its own numbering instance
    assert items[0][3] == "Compact" and items[7][3] != "Compact"  # tight and loose lists
    numbering = ET.fromstring(parts["word/numbering.xml"])
    fmts = {a.get(f"{{{W}}}abstractNumId"): a.find(f"{{{W}}}lvl/{{{W}}}numFmt").get(f"{{{W}}}val")
            for a in numbering.iter(f"{{{W}}}abstractNum")}
    nums = {n.get(f"{{{W}}}numId"): n for n in numbering.iter(f"{{{W}}}num")}
    assert fmts[nums[items[0][2]].find(f"{{{W}}}abstractNumId").get(f"{{{W}}}val")] == "bullet"
    seven = nums[items[5][2]]
    assert fmts[seven.find(f"{{{W}}}abstractNumId").get(f"{{{W}}}val")] == "decimal"
    assert seven.find(f".//{{{W}}}startOverride").get(f"{{{W}}}val") == "7"


def test_footnotes_and_code(docx):
    _, _, parts = docx
    doc = parts["word/document.xml"]
    assert len(re.findall(r"<w:footnoteReference w:id=\"\d+\"/>", doc)) == 2
    assert "The first note" not in doc  # the notes left the text
    notes = ET.fromstring(parts["word/footnotes.xml"])
    real = [n for n in notes.iter(f"{{{W}}}footnote") if not n.get(f"{{{W}}}type")]
    assert [_text(n).strip() for n in real] == ["The first note.", "The second note, with code."]
    assert all(n.find(f".//{{{W}}}footnoteRef") is not None for n in real)
    assert 'w:val="VerbatimChar"' in parts["word/footnotes.xml"]
    styles = [_style(p) for p in _paras(doc)]
    assert "SourceCode" in styles and "DefinitionTerm" in styles and "Definition" in styles
    assert "CaptionedFigure" in styles and "ImageCaption" in styles
    assert 'w:rStyle w:val="VerbatimChar"' in doc
    rels = parts["word/_rels/document.xml.rels"]
    assert 'Target="/url/" TargetMode="External"' in rels  # a link to outside the book stays a link


def test_tables(tmp_path):
    src = tmp_path / "t.html"
    src.write_text("<html><body><table><caption>The caption</caption><thead><tr><th>A</th><th>B</th><th>C</th></tr></thead>"
                   "<tbody><tr><td rowspan='2'>tall</td><td>1</td><td>2</td></tr><tr><td colspan='2'>wide</td></tr></tbody>"
                   "</table></body></html>", encoding="utf-8")
    out = tmp_path / "t.docx"
    bookport.read(src).write(out)
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode()
    root = ET.fromstring(doc)
    caption = next(p for p in root.iter(f"{{{W}}}p") if _text(p) == "The caption")
    assert _style(caption) == "TableCaption"
    rows = root.findall(f".//{{{W}}}tr")
    assert rows[0].find(f"{{{W}}}trPr/{{{W}}}tblHeader") is not None and rows[1].find(f"{{{W}}}trPr") is None
    assert [len(r.findall(f"{{{W}}}tc")) for r in rows] == [3, 3, 2]
    assert rows[1].find(f".//{{{W}}}vMerge").get(f"{{{W}}}val") == "restart"
    assert rows[2].find(f".//{{{W}}}vMerge") is not None and rows[2].find(f".//{{{W}}}gridSpan") is not None
    back = bookport.read(out)
    table = back.tree(back.spine[0]).find(".//table")
    assert [len(tr) for tr in table.iter("tr")] == [3, 3, 1]  # the tall cell covers the last row's first


def test_reads_back(docx):
    _, out, _ = docx
    back = bookport.read(out)
    text = "".join(markup.to_string(back.tree(h).find("body")) for h, _ in back.documents())
    assert len(re.findall(r"<li[ >]", text)) == 9
    assert text.count('epub:type="noteref"') == 2
    assert re.search(r'<ol start="7">', text)
    assert "<pre>" in text and "<code>code</code>" in text


def test_markdown_implicit_figures(tmp_path):
    book = book_from_markdown("Text.\n\n![A caption](fig.png)\n\nInline ![x](fig.png) image.\n\n![](fig.png)\n", "f",
                              images={"fig.png": png()})
    root = book.tree(book.spine[0])
    figs = root.findall(".//figure")
    assert len(figs) == 1 and markup.text_of(figs[0].find("figcaption")) == "A caption"
    out = tmp_path / "f.md"
    book.write(out)
    assert "![A caption](images/fig.png)\n" in out.read_text(encoding="utf-8")
    assert len(bookport.read(out).tree(book.spine[0]).findall(".//figure")) == 1


SOFFICE = shutil.which("soffice") or next((p for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",)
                                           if Path(p).is_file()), None)


@pytest.mark.skipif(SOFFICE is None, reason="LibreOffice isn't installed")
def test_libreoffice_keeps_structure(docx, tmp_path):
    """LibreOffice opens the file the way Word does and saves it again: lists, notes and tables survive."""
    _, out, _ = docx
    lo = tmp_path / "lo"
    lo.mkdir()
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([SOFFICE, "--headless", "--norestore", "--nolockcheck", "--nodefault",
                        f"-env:UserInstallation={Path(profile).as_uri()}", "--convert-to", "docx:MS Word 2007 XML",
                        "--outdir", str(lo), str(out)], capture_output=True, timeout=300, check=False)
    resaved = lo / out.name
    assert resaved.is_file()

    def counts(path):
        b = bookport.read(path)
        text = "".join(markup.to_string(b.tree(h).find("body")) for h, _ in b.documents())
        return {t: len(re.findall(rf"<{t}[ />]", text)) for t in ("h1", "li", "table", "td", "img", "pre", "aside")}

    assert counts(resaved) == counts(out)


def _box(text, top, left, picture=False):
    """A floating text box as LibreOffice writes it: the shape, and a VML copy for older readers."""
    wp = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    pos = (f'<wp:positionH relativeFrom="column"><wp:posOffset>{left * 12700}</wp:posOffset></wp:positionH>'
           f'<wp:positionV relativeFrom="paragraph"><wp:posOffset>{top * 12700}</wp:posOffset></wp:positionV>')
    if picture:
        body = ('<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData><pic:pic '
                'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:blipFill><a:blip r:embed="rId9"/>'
                '</pic:blipFill></pic:pic></a:graphicData></a:graphic>')
        vml = f'<v:shape style="margin-left:{left}pt;margin-top:{top}pt"><v:imagedata r:id="rId9"/></v:shape>'
    else:
        content = f"<w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent>"
        body = ('<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData><wps:wsp '
                f'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"><wps:txbx>{content}</wps:txbx>'
                "</wps:wsp></a:graphicData></a:graphic>")
        vml = f'<v:shape style="margin-left:{left}pt;margin-top:{top}pt"><v:textbox>{content}</v:textbox></v:shape>'
    return (f'<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor xmlns:wp="{wp}">{pos}{body}'
            f"</wp:anchor></w:drawing></mc:Choice><mc:Fallback><w:pict>{vml}</w:pict></mc:Fallback></mc:AlternateContent></w:r>")


def test_reads_text_boxes(tmp_path):
    """LibreOffice opens a PDF as one text box per line: their text comes in, once, in reading order."""
    body = ("<w:p><w:r><w:t>Before.</w:t></w:r></w:p><w:p>" + _box("second line", 40, 72) + _box("", 80, 72, picture=True)
            + _box("first line", 20, 72) + _box("same row, right", 40, 300) + "</w:p><w:p><w:r><w:t>After.</w:t></w:r></w:p>")
    doc = (f'<w:document xmlns:w="{W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
           'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:v="urn:schemas-microsoft-com:vml">'
           f"<w:body>{body}</w:body></w:document>")
    p = tmp_path / "boxes.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                   'Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                   'Target="media/p.png"/></Relationships>')
        z.writestr("word/media/p.png", png())
    book = bookport.read(p)
    body = book.tree(book.spine[0]).find("body")
    shown = [markup.text_of(e).strip() or (e.find("img") is not None and "[picture]") for e in body if e.tag == "p"]
    assert shown == ["Before.", "first line", "second line", "same row, right", "[picture]", "After."]
    assert len(book.images()) == 1


@pytest.mark.skipif(SOFFICE is None, reason="LibreOffice isn't installed")
def test_libreoffice_pdf_import(tmp_path):
    """A two-page PDF opened by LibreOffice and saved as DOCX (ConvertAll's PDF -> markup route) keeps its text."""
    book = bookport.Book()
    book.metadata.title = "Two pages"
    for n in (1, 2):
        root, b = markup.new_document(f"Page {n}")
        ET.SubElement(b, "p").text = f"Sample text, page {n}"
        book.add_document(f"text/p{n}.xhtml", root)
    pdf = tmp_path / "two.pdf"
    book.write(pdf)
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([SOFFICE, "--headless", "--norestore", "--nolockcheck", "--nodefault",
                        f"-env:UserInstallation={Path(profile).as_uri()}", "--infilter=writer_pdf_import",
                        "--convert-to", "docx", "--outdir", str(tmp_path), str(pdf)], capture_output=True, timeout=300,
                       check=False)
    text = bookport.read(tmp_path / "two.docx").text()
    assert text.index("page 1") < text.index("page 2")
