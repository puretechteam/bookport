"""reStructuredText: reading (docutils and Sphinx markup, includes, hostile files) and writing."""

import time
import xml.etree.ElementTree as ET

import pytest

import bookport
from bookport import markup
from bookport.fmt import rst


def _read(tmp_path, text: str, name: str = "doc.rst", **files) -> bookport.Book:
    d = tmp_path / "docs"
    d.mkdir(exist_ok=True)
    for fname, data in files.items():
        p = d / fname.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data) if isinstance(data, bytes) else p.write_text(data, encoding="utf-8")
    src = d / name
    src.write_text(text, encoding="utf-8")
    return bookport.read(src, "rst")


def _body(book) -> ET.Element:
    body = ET.Element("body")
    for href, _ in book.documents():
        body.extend(list(book.tree(href).find("body")))
    return body


def _html(book) -> str:
    return "".join(markup.to_string(c) for c in _body(book))


def _count(book, tag) -> int:
    return sum(1 for _ in _body(book).iter(tag))


def test_sections_take_levels_in_the_order_styles_appear(tmp_path):
    book = _read(tmp_path, "=====\nTitle\n=====\n\nOne\n---\n\ntext\n\nTwo\n~~~\n\nThree\n-----\n\nmore\n")
    heads = [(e.tag, markup.text_of(e)) for e in _body(book) if e.tag.startswith("h")]
    assert heads == [("h1", "Title"), ("h2", "One"), ("h3", "Two"), ("h2", "Three")]
    assert book.metadata.title == "Title"


def test_short_underline_is_a_paragraph_and_transition(tmp_path):
    html = _html(_read(tmp_path, "Some long title\n==\n\nA\n\n----------\n\nB\n"))
    assert "<h1" not in html and "<hr/>" in html


def test_inline_markup_and_start_end_rules(tmp_path):
    html = _html(_read(tmp_path, "*em* **strong** ``lit`` 2*3*4 a*b ``x*y`` :sub:`2` :sup:`3` \\*not*\n"))
    assert "<em>em</em>" in html and "<strong>strong</strong>" in html and "<code>lit</code>" in html
    assert "2*3*4 a*b" in html and "<code>x*y</code>" in html
    assert "<sub>2</sub>" in html and "<sup>3</sup>" in html and "*not*" in html


def test_lists_all_enumerators_and_auto(tmp_path):
    book = _read(tmp_path, "- a\n- b\n\n#. one\n#. two\n\n3) three\n4) four\n\n(a) x\n(b) y\n\nii. two\niii. three\n")
    lists = [e for e in _body(book) if e.tag in ("ul", "ol")]
    assert [e.tag for e in lists] == ["ul", "ol", "ol", "ol", "ol"]
    assert lists[2].get("start") == "3" and lists[3].get("type") == "a"
    assert lists[4].get("type") == "i" and lists[4].get("start") == "2"
    assert _count(book, "li") == 10 and _count(book, "p") == 0  # simple lists: no <p> in items


def test_a_capital_initial_is_not_a_list(tmp_path):
    html = _html(_read(tmp_path, "A. Einstein wrote\nthis text.\n"))
    assert "<ol" not in html and "A. Einstein" in html


def test_definition_field_and_option_lists(tmp_path):
    book = _read(tmp_path, "term : classifier\n   Definition.\n\n:Field: value\n\n-a        all\n--long=X  long one\n")
    dls = [e for e in _body(book) if e.tag == "dl"]
    assert len(dls) == 3
    assert "classifier" in markup.text_of(dls[0].find("dt"))
    assert markup.text_of(dls[2]).startswith("-aall")


def test_literal_blocks_doctest_and_line_blocks(tmp_path):
    text = "Code::\n\n    x = 1\n      y\n\nQuoted ::\n\n> a\n> b\n\n>>> 1 + 1\n2\n\n| line one\n|    indented\n"
    book = _read(tmp_path, text)
    pres = [markup.text_of(e) for e in _body(book).iter("pre")]
    assert pres == ["x = 1\n  y", "> a\n> b", ">>> 1 + 1\n2"]
    html = _html(book)
    assert "<p>Code:</p>" in html and "<p>Quoted</p>" in html
    assert 'class="line-block"' in html and "<br/>" in html


def test_block_quote_with_attribution(tmp_path):
    html = _html(_read(tmp_path, "Para.\n\n    Wise words.\n\n    -- Someone\n"))
    assert "<blockquote><p>Wise words.</p><p class=\"attribution\">\u2014 Someone</p></blockquote>" in html


def test_grid_table_with_spans_and_header(tmp_path):
    text = ("+-----+-----+-----+\n| H1  | H2  | H3  |\n+=====+=====+=====+\n| a   | b         |\n+-----+-----+-----+\n"
            "| c   | d   | e   |\n+     +-----+-----+\n|     | f   | g   |\n+-----+-----+-----+\n")
    t = _body(_read(tmp_path, text)).find("table")
    assert len(t.findall("thead/tr/th")) == 3
    cells = t.findall("tbody/tr/td")
    assert [markup.text_of(c) for c in cells] == ["a", "b", "c", "d", "e", "f", "g"]
    assert cells[1].get("colspan") == "2" and cells[2].get("rowspan") == "2"


def test_simple_list_and_csv_tables(tmp_path):
    text = ("=====  =====\nA      B\n=====  =====\n1      two\n       lines\n3      4\n=====  =====\n\n"
            ".. list-table:: Cap\n   :header-rows: 1\n\n   * - x\n     - y\n   * - 1\n     - 2\n\n"
            '.. csv-table:: CSV\n   :header: "p", "q"\n\n   1, "2,3"\n')
    tables = _body(_read(tmp_path, text)).findall("table")
    assert len(tables) == 3
    assert [markup.text_of(c) for c in tables[0].iter("td")] == ["1", "two\nlines", "3", "4"]
    assert tables[1].find("caption").text == "Cap" and len(tables[1].findall("thead/tr/th")) == 2
    assert [markup.text_of(c) for c in tables[2].iter("td")] == ["1", "2,3"]


def test_hyperlinks_named_anonymous_embedded_internal(tmp_path):
    text = ("See Python_, `the docs`_, `anon`__, `inline <http://i.org>`_, `alias <Python_>`__,\n"
            "Target_ and `Section Two`_.\n\n.. _Python: http://python.org\n.. _the docs: http://docs.org\n"
            "__ http://anon.org\n\n.. _target:\n\nSection Two\n===========\n")
    hrefs = [a.get("href") for a in _body(_read(tmp_path, text)).iter("a")]
    assert hrefs == ["http://python.org", "http://docs.org", "http://anon.org", "http://i.org", "http://python.org",
                     "#target", "#target"]


def test_footnotes_numbered_auto_labelled_symbol_and_citations(tmp_path):
    text = ("A [1]_ B [#]_ C [#lab]_ D [*]_ E [#lab]_ F [CIT]_.\n\n.. [1] one\n.. [#] auto\n.. [#lab] labelled\n"
            ".. [*] star\n.. [CIT] A work.\n")
    body = _body(_read(tmp_path, text))
    calls = [a for a in body.iter("a") if a.get("class") == "noteref"]
    assert [markup.text_of(a) for a in calls] == ["1", "2", "3", "*", "3"]
    asides = {a.get("id"): markup.text_of(a) for a in body.iter("aside")}
    assert asides["fn2"] == "auto" and asides["fn3"] == "labelled"
    cite = [a for a in body.iter("a") if a.get("href", "").startswith("#cite")]
    assert markup.text_of(cite[0]) == "[CIT]" and body.find(".//dl[@class='citations']") is not None


def test_substitutions_replace_image_unicode(tmp_path):
    text = "|name| and |pic| and |tm| and |pic|_.\n\n.. |name| replace:: *Python*\n.. |pic| image:: p.png\n" \
           ".. |tm| unicode:: U+2122\n.. _pic: http://pic.org\n"
    html = _html(_read(tmp_path, text, **{"p.png": b"\x89PNG\r\n\x1a\nfake"}))
    assert "<em>Python</em>" in html and "\u2122" in html
    assert html.count("<img") == 2 and '<a href="http://pic.org"><img' in html


def test_roles_including_sphinx(tmp_path):
    text = (":emphasis:`e` :strong:`s` :literal:`l` :code:`c` :math:`x^2` :func:`len` :meth:`~a.b.c` "
            ":class:`Title <x.Y>` :pep:`8` :rfc:`2822` :term:`thing` :kbd:`C-x` :file:`a/{b}` :weird:`kept`\n")
    book = _read(tmp_path, text)
    html = _html(book)
    for piece in ("<em>e</em>", "<strong>s</strong>", "<code>l</code>", "<code>len()</code>", "<code>c()</code>",
                  "<code>Title</code>", "PEP 8", "RFC 2822", "<kbd>C-x</kbd>", "<code>a/b</code>", "kept"):
        assert piece in html, piece
    assert html.count("<math") == 1
    assert any("weird" in line for line in book.lost)


def test_directives_admonitions_code_math_figure_topic(tmp_path):
    text = (".. note:: Be careful.\n\n.. warning::\n\n   Really.\n\n.. code-block:: python\n   :linenos:\n\n   x = 1\n\n"
            ".. math::\n\n   a = b\n\n   c = d\n\n.. figure:: f.png\n   :alt: Alt\n\n   The caption.\n\n"
            ".. topic:: Topic\n\n   Body.\n\n.. rubric:: Rubric\n\n.. versionadded:: 3.2\n   Explained.\n\n"
            ".. deprecated:: 3.4\n\n.. raw:: html\n\n   <b>bold</b>\n\n.. raw:: latex\n\n   \\foo\n")
    body = _body(_read(tmp_path, text))
    html = "".join(markup.to_string(c) for c in body)
    assert html.count('<div class="admonition') == 2 and "admonition-title\">Note<" in html
    assert body.find(".//code[@class='language-python']").text == "x = 1"
    assert len([m for m in body.iter("math") if m.get("display") == "block"]) == 2
    assert body.find(".//figure/figcaption").text == "The caption." and body.find(".//figure/img").get("alt") == "Alt"
    assert "Added in version 3.2: " in html and "Deprecated since version 3.4." in html
    assert "<b>bold</b>" in html and 'data-format="latex"' in html


def test_sphinx_objects_toctree_only_index_and_unknown(tmp_path):
    text = (".. index:: single: x\n\n.. function:: spam(a, b)\n   :noindex:\n\n   Does spam.\n\n"
            ".. class:: Egg\n\n   An egg.\n\n.. toctree::\n   :maxdepth: 2\n\n   intro\n   Guide <guide>\n\n"
            ".. only:: latex\n\n   Hidden.\n\n.. only:: html\n\n   Shown.\n\n.. mystery:: arg\n\n   Mystery text.\n")
    book = _read(tmp_path, text)
    body = _body(book)
    dts = [markup.text_of(d) for d in body.iter("dt")]
    assert dts == ["spam(a, b)", "class Egg"]
    assert [a.get("href") for a in body.find(".//ul[@class='toctree']").iter("a")] == ["intro.html", "guide.html"]
    html = _html(book)
    assert "Hidden" not in html and "Shown" in html and "single: x" not in html
    assert "Mystery text." in html and "arg" in html
    assert book.lost == ["reStructuredText directive 'mystery' isn't supported; its text was kept"]


def test_contents_directive_lists_sections(tmp_path):
    body = _body(_read(tmp_path, ".. contents::\n\nOne\n===\n\nTwo\n===\n\nSub\n---\n"))
    links = [a.get("href") for a in body.find(".//div[@class='contents topic']").iter("a")]
    assert links == ["#one", "#two", "#sub"]


def test_include_with_lines_and_literal(tmp_path):
    text = ".. include:: part.rst\n   :start-line: 1\n   :end-line: 4\n\n.. include:: code.py\n   :literal:\n"
    book = _read(tmp_path, text, **{"part.rst": "skipped\nKept *one*.\n\nKept two.\nskipped too\n", "code.py": "x = 1\n"})
    html = _html(book)
    assert "skipped" not in html and "<em>one</em>" in html and "Kept two" in html
    assert "<pre><code>x = 1</code></pre>" in html


def test_hostile_includes_are_refused_and_said(tmp_path):
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")
    text = (".. include:: ../../secret.txt\n\n.. include:: C:/Windows/win.ini\n\n.. include:: file:///etc/passwd\n\n"
            ".. include:: loop.rst\n\n.. image:: ../../x.png\n")
    book = _read(tmp_path, text, **{"loop.rst": ".. include:: loop.rst\n\nLoop text.\n"})
    html = _html(book)
    assert "SECRET" not in html and html.count("Loop text.") == 1
    assert sum("outside the document's folder" in line for line in book.lost) == 4
    assert any("includes itself" in line for line in book.lost)


def test_hostile_nesting_and_backtracking_are_fast(tmp_path):
    deep = "".join(" " * k + "- item\n\n" for k in range(400))
    stars = "*a " * 20000 + "\n\n" + "`x " * 20000 + "\n\n" + "|" * 20000 + "\n"
    subs = "|a|\n\n" + "".join(f".. |{c}| replace:: |{d}| |{d}|\n" for c, d in zip("abcdefghij", "bcdefghijk"))
    t = time.perf_counter()
    for text in (deep, stars, subs):
        book = _read(tmp_path, text)
        assert book.spine
    assert time.perf_counter() - t < 10


def test_reads_back_what_it_writes(tmp_path):
    src = ("Title\n=====\n\nA *b* **c** ``d`` `link <http://x.org>`__ and note [#]_.\n\n.. [#] The note.\n\n"
           "- one\n- two\n\n  - nested\n\n1. a\n2. b\n\nterm\n    def\n\n::\n\n   code\n\n.. note:: careful\n\n"
           "+---+---+\n| a | b |\n+===+===+\n| 1 | 2 |\n+---+---+\n\n    quoted\n\nSub\n---\n\n| line\n| two\n")
    book = _read(tmp_path, src)
    out = tmp_path / "out.rst"
    rst.write(book, out)
    again = bookport.read(out, "rst")
    for tag in ("h1", "h2", "p", "li", "dt", "table", "td", "th", "pre", "em", "strong", "code", "a", "aside", "blockquote"):
        assert _count(again, tag) == _count(book, tag), tag


def test_writer_escapes_and_spaces_around_markup(tmp_path):
    b = bookport.Book(bookport.Metadata(title="T"))
    root, body = markup.new_document("T")
    p = ET.SubElement(body, "p")
    p.text = "un"
    em = ET.SubElement(p, "em")
    em.text = "believ"
    em.tail = "able *stars* and word_ and |bar| 1. not a list"
    ET.SubElement(body, "p").text = "1. not a list either"
    b.add_document("text/a.xhtml", root)
    out = tmp_path / "w.rst"
    rst.write(b, out)
    text = out.read_text(encoding="utf-8")
    assert "un\\ *believ*\\ able" in text and "\\1. not a list either" in text
    again = _body(bookport.read(out, "rst"))
    assert [markup.text_of(e) for e in again.iter("p")] == ["unbelievable *stars* and word_ and |bar| 1. not a list",
                                                            "1. not a list either"]


def test_write_saves_pictures_when_asked(tmp_path):
    from .conftest import png
    book = _read(tmp_path, ".. image:: pic.png\n", **{"pic.png": png()})
    out = tmp_path / "o" / "x.rst"
    out.parent.mkdir()
    saved = rst.write(book, out, pictures=True)
    assert saved == ["images/pic.png"] and (out.parent / "images" / "pic.png").read_bytes() == png()
    assert ".. |" in out.read_text(encoding="utf-8") or "image:: images/pic.png" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["", "\n\n", "..", "::", "|", "`", "**", ".. ::", "+--", "===", "- ", "#.", "[1]_"])
def test_odd_tiny_inputs_read(tmp_path, text):
    assert _read(tmp_path, text).spine
