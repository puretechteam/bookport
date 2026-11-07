"""AsciiDoc (Asciidoctor): reading (preprocessor, blocks, lists, tables, inline, hostile files) and writing."""

import time
import xml.etree.ElementTree as ET

import pytest

import bookport
from bookport import markup
from bookport.fmt import asciidoc


def _read(tmp_path, text: str, name: str = "doc.adoc", **files) -> bookport.Book:
    d = tmp_path / "docs"
    d.mkdir(exist_ok=True)
    for fname, data in files.items():
        p = d / fname.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data) if isinstance(data, bytes) else p.write_text(data, encoding="utf-8")
    src = d / name
    src.write_text(text, encoding="utf-8")
    return bookport.read(src, "adoc")


def _body(book) -> ET.Element:
    body = ET.Element("body")
    for href, _ in book.documents():
        body.extend(list(book.tree(href).find("body")))
    return body


def _html(book) -> str:
    return "".join(markup.to_string(c) for c in _body(book))


def _count(book, tag) -> int:
    return sum(1 for _ in _body(book).iter(tag))


def test_header_attributes_and_sections(tmp_path):
    text = ("= Doc Title\nJane Doe <j@d.org>; Joe Bloggs\nv1.0, 2024-05-01\n:product: Widget\n:idprefix: id_\n\n"
            "== One {product}\n\ntext about {product} and {missing}\n\n=== Two\n\n[[custom]]\n== Three\n")
    book = _read(tmp_path, text)
    assert book.metadata.title == "Doc Title" and book.metadata.authors == ["Jane Doe", "Joe Bloggs"]
    heads = [(e.tag, e.get("id"), markup.text_of(e)) for e in _body(book) if e.tag.startswith("h")]
    assert heads == [("h1", "id_one_widget", "One Widget"), ("h2", "id_two", "Two"), ("h1", "custom", "Three")]
    assert "about Widget and {missing}" in _html(book)


def test_conditionals_and_counters(tmp_path):
    text = (":flag:\n:level: 3\n\nifdef::flag[]\nshown\nendif::[]\nifndef::flag[]\nhidden\nendif::[]\n"
            "ifdef::nope,flag[either]\nifdef::nope+flag[both]\nifeval::[{level} > 2]\nbig\nendif::[]\n"
            "ifeval::[\"{level}\" == \"1\"]\nsmall\nendif::[]\n\nItem {counter:n} and {counter:n}.\n")
    html = _html(_read(tmp_path, text))
    assert "shown" in html and "hidden" not in html and "either" in html and "both" not in html
    assert "big" in html and "small" not in html and "Item 1 and 2." in html


def test_inline_formatting_constrained_and_unconstrained(tmp_path):
    text = ("*bold* _em_ `mono` #mark# ^sup^ ~sub~ **un**constrained __it__alic ``co``de a*b*c "
            "[.underline]#u# \\*escaped* \"`curly`\" (C) -> a -- b wasn't +*pass*+\n")
    html = _html(_read(tmp_path, text))
    for piece in ("<strong>bold</strong>", "<em>em</em>", "<code>mono</code>", "<mark>mark</mark>", "<sup>sup</sup>",
                  "<sub>sub</sub>", "<strong>un</strong>constrained", "<em>it</em>alic", "<code>co</code>de", "a*b*c",
                  '<u class="underline">u</u>', "*escaped*", "“curly”", "©", "→", "a — b",
                  "wasn’t", "*pass*"):
        assert piece in html, piece


def test_links_xrefs_anchors_and_footnotes(tmp_path):
    text = (":url: https://u.org\n\n[[top]]\n== Top\n\nhttps://a.org[A] https://b.org {url}[U] link:file.pdf[F] "
            "mailto:x@y.org[mail] <<top>> <<top,There>> xref:top[X] <<other.adoc#sec,O>> [[here]]anchor\n"
            "footnote:n1[First.] footnote:n1[] footnote:[Second.]\n")
    body = _body(_read(tmp_path, text))
    links = [(a.get("href"), markup.text_of(a)) for a in body.iter("a") if a.get("class") != "noteref"]
    assert links == [("https://a.org", "A"), ("https://b.org", "https://b.org"), ("https://u.org", "U"),
                     ("file.pdf", "F"), ("mailto:x@y.org", "mail"), ("#top", "Top"), ("#top", "There"), ("#top", "X"),
                     ("other.html#sec", "O")]
    calls = [markup.text_of(a) for a in body.iter("a") if a.get("class") == "noteref"]
    assert calls == ["1", "1", "2"] and [markup.text_of(a) for a in body.iter("aside")] == ["First.", "Second."]
    assert body.find(".//span[@id='here']") is not None


def test_admonitions_and_delimited_blocks(tmp_path):
    text = ("NOTE: Paragraph note.\n\n[TIP]\n====\nBlock tip.\n====\n\n.Code\n[source,ruby]\n----\nputs 1\n----\n\n"
            "....\nliteral\n....\n\n.Ex\n====\nexample\n====\n\n****\nsidebar\n****\n\n[quote, Ann, Book]\n____\nQuoted.\n____\n\n"
            "[verse]\n____\nline one\n  line two\n____\n\n++++\n<b>raw</b>\n++++\n\n--\nopen\n--\n\n////\ncomment\n////\n"
            "// line comment\n\n  indented literal\n\n'''\n\n```python\nx = 1\n```\n")
    body = _body(_read(tmp_path, text))
    html = "".join(markup.to_string(c) for c in body)
    assert html.count('<div class="admonition') == 2
    assert [markup.text_of(e) for e in body.iter("pre")] == ["puts 1", "literal", "indented literal", "x = 1"]
    assert body.find(".//code[@class='language-ruby']") is not None and "Example 1. Ex" in html
    assert '<div class="sidebar"><p>sidebar</p></div>' in html and "— Ann" in html and "<cite>Book</cite>" in html
    assert 'class="verse"' in html and "<b>raw</b>" in html and "comment" not in html and "<hr/>" in html


def test_lists_nesting_by_marker_checklists_continuation(tmp_path):
    text = ("* one\n** two\n*** three\n* four\n+\nattached para\n+\n----\ncode\n----\n- [x] done\n\n. first\n. second\n.. inner\n\n"
            "3. three\n4. four\n\nterm:: def\nother::\n  indented def\n\n<1> callout\n")
    body = _body(_read(tmp_path, text))
    top = body.find("ul")
    assert len(top.findall("li")) == 2 and top.find("li/ul/li/ul/li/p").text == "three"
    assert [markup.text_of(p) for p in top.findall("li")[1]][:2] == ["four", "attached para"]
    assert top.find("li[2]/pre") is not None
    assert body.find(".//ol/li/ol/li/p").text == "inner"
    assert [e.get("start") for e in body.iter("ol")][2] == "3"
    dl = body.find(".//dl")
    assert [markup.text_of(e) for e in dl] == ["term", "def", "other", "indented def"]


def test_tables_specs_spans_header_csv(tmp_path):
    text = ('.Cap\n[cols="1,2a,>1",options="header"]\n|===\n|A |B |C\n\n|1\n|* x\n* y\n|3\n2+|span |z\n.2+|tall |q |r\n|s |t\n|===\n\n'
            ',===\nk,v\n"a,b",c\n,===\n\n|===\n|implicit |header\n\n|1 |2\n|===\n')
    tables = _body(_read(tmp_path, text)).findall("table")
    t = tables[0]
    assert t.find("caption").text == "Table 1. Cap" and [markup.text_of(c) for c in t.iter("th")] == ["A", "B", "C"]
    assert t.find(".//td/ul") is not None
    spans = [(c.get("colspan"), c.get("rowspan")) for c in t.iter("td") if c.get("colspan") or c.get("rowspan")]
    assert spans == [("2", None), (None, "2")]
    assert [markup.text_of(c) for c in tables[1].iter("td")] == ["k", "v", "a,b", "c"]
    assert [markup.text_of(c) for c in tables[2].iter("th")] == ["implicit", "header"]


def test_images_figures_and_math(tmp_path):
    from .conftest import png
    text = (":imagesdir: img\n\n.A figure\nimage::pic.png[Alt]\n\nInline image:pic.png[] here.\n\n"
            "latexmath:[x^2] and stem:[y] and\n\n[latexmath]\n++++\na = b\n++++\n")
    book = _read(tmp_path, text, **{"img__pic.png": png()})
    body = _body(book)
    assert body.find(".//figcaption").text == "Figure 1. A figure" and body.find(".//figure/img").get("alt") == "Alt"
    assert len(book.images()) == 1 and _count(book, "img") == 2
    assert [m.get("display") for m in body.iter("math")] == ["inline", "inline", "block"]
    assert any("AsciiMath" in line for line in book.lost)


def test_include_lines_tags_leveloffset_and_hostile(tmp_path):
    (tmp_path / "secret.adoc").write_text("SECRET", encoding="utf-8")
    text = ("include::part.adoc[leveloffset=+1]\n\n[source]\n----\ninclude::code.rb[tag=keep]\n----\n\n"
            "include::code.rb[lines=1..1]\n\ninclude::../../secret.adoc[]\n\ninclude::/etc/passwd[]\n\n"
            "include::https://evil.org/x.adoc[]\n\ninclude::loop.adoc[]\n")
    book = _read(tmp_path, text, **{"part.adoc": "== Part\n\nbody\n", "loop.adoc": "include::loop.adoc[]\nloop\n",
                                    "code.rb": "first\n# tag::keep[]\nkept\n# end::keep[]\nlast\n"})
    body = _body(book)
    assert body.find("h2") is not None and markup.text_of(body.find(".//pre")) == "kept"
    html = _html(book)
    assert "first" in html and "SECRET" not in html and "last" not in html
    assert sum("outside the document's folder" in line for line in book.lost) == 2
    assert any("web" in line for line in book.lost) and any("includes itself" in line for line in book.lost)


def test_hostile_inputs_are_fast(tmp_path):
    texts = ["*a " * 30000, "footnote:[" * 10000, "<<" * 50000, "+++" * 20000, "**x" * 30000, "[[" * 30000,
             "".join("*" * (k % 5 + 1) + " x\n" for k in range(5000)), "|===\n" + "|a " * 50000 + "\n|===\n",
             ":a: {b}{b}\n:b: {a}{a}\n\n{a}\n", "ifdef::x[]\n" * 5000, "[" * 30000 + "]" * 30000,
             "." * 40000 + "\n", "include::doc.adoc[]\n"]
    t = time.perf_counter()
    for text in texts:
        assert _read(tmp_path, text).spine
    assert time.perf_counter() - t < 15


def test_reads_back_what_it_writes(tmp_path):
    src = ("= T\n\n== Head\n\nA *b* _c_ `d` https://x.org[link] note.footnote:[The note.]\n\n* one\n** nested\n* two\n\n"
           ". first\n. second\n\nterm:: def\n\n[cols=\"2*\",options=\"header\"]\n|===\n|a |b\n\n|1 |2\n|===\n\n"
           "[source,python]\n----\nx = 1\n----\n\n____\nquoted\n____\n\n=== Sub\n\nNOTE: careful\n")
    book = _read(tmp_path, src)
    out = tmp_path / "out.adoc"
    asciidoc.write(book, out)
    again = bookport.read(out, "adoc")
    assert again.metadata.title == "T"
    for tag in ("h1", "h2", "p", "li", "dt", "table", "td", "th", "pre", "em", "strong", "code", "a", "aside", "blockquote"):
        assert _count(again, tag) == _count(book, tag), tag


def test_writer_keeps_plain_text_plain(tmp_path):
    b = bookport.Book(bookport.Metadata(title="T"))
    root, body = markup.new_document("T")
    ET.SubElement(body, "p").text = "* not a list, 2*3*4 and a *pair*, {attr}, <<x>>, a -- b, (C), price $5 +"
    ET.SubElement(body, "p").text = "NOTE: not an admonition"
    b.add_document("text/a.xhtml", root)
    out = tmp_path / "w.adoc"
    asciidoc.write(b, out)
    again = [markup.text_of(p) for p in _body(bookport.read(out, "adoc")).iter("p")]
    assert again == ["* not a list, 2*3*4 and a *pair*, {attr}, <<x>>, a -- b, (C), price $5 +", "NOTE: not an admonition"]


@pytest.mark.parametrize("text", ["", "=", "= ", "----", "|===", "* ", "::", "+", "[", "include::[]", "ifdef::[]", "{"])
def test_odd_tiny_inputs_read(tmp_path, text):
    assert _read(tmp_path, text).spine
