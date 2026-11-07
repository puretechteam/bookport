"""MediaWiki: reading wikitext (with its quote, list, table, link and ref rules) and writing it back."""

import time

import pytest

import bookport
from bookport import markup
from bookport.fmt import mediawiki

from .conftest import png


def _html(text: str, base=None) -> str:
    return mediawiki.to_html(text, base)[0]


def _read(tmp_path, text: str, name="page.wiki"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return bookport.read(p)


def _body(book) -> str:
    return "".join(book.resources[h].data.decode() for h, _ in book.documents())


# --- reading ---------------------------------------------------------------------------------------

def test_headings_levels_and_ids():
    out = _html("= One =\n== Two ==\n===Three===\n====== Six ======\n======= Seven =======\n == not ==")
    assert '<h1 id="One">One</h1>' in out and '<h2 id="Two">Two</h2>' in out and "<h3" in out
    assert out.count("<h6") == 2  # seven = signs: a level-6 heading whose text keeps the extra =
    assert '<h6 id="=_Seven_=">= Seven =</h6>' in out
    assert "<pre>== not ==</pre>" in out  # a leading space makes a pre block


@pytest.mark.parametrize("src,want", [
    ("''i''", "<i>i</i>"),
    ("'''b'''", "<b>b</b>"),
    ("'''''bi'''''", "<i><b>bi</b></i>"),
    ("'''''i'' b'''", "<b><i>i</i> b</b>"),
    ("'''b ''i'''''", "<b>b <i>i</i></b>"),
    ("'''''b''' i''", "<i><b>b</b> i</i>"),
    ("''''four''''", "'<b>four'</b>"),
    ("l'''amour'' x", "l'<i>amour</i> x"),  # odd bold and odd italic: a single-letter word's ''' is ' + ''
    ("''open", "<i>open</i>"),  # closed at the end of the line
])
def test_apostrophe_runs(src, want):
    assert f"<p>{want}</p>" in _html(src)


def test_lists_mixed_nesting_and_definitions():
    out = _html("* a\n** b\n*# c\n* d\n# one\n#: more\n; term : def\n; t2\n: d2\n: d3")
    assert "<ul><li>a<ul><li>b</li></ul><ol><li>c</li></ol></li><li>d</li></ul>" in out
    assert "<ol><li>one<dl><dd>more</dd></dl></li></ol>" in out
    assert "<dl><dt>term</dt><dd>def</dd><dt>t2</dt><dd>d2</dd><dd>d3</dd></dl>" in out


def test_definition_colon_skips_urls():
    out = _html("; see http://example.com/x : the site")
    assert "<dd>the site</dd>" in out and 'href="http://example.com/x"' in out


def test_rule_pre_and_paragraphs():
    out = _html("top\n---- after\n\n line one\n line ''two''\n\nnext\nline")
    assert "<hr/><p>after</p>" in out
    assert "<pre>line one\nline <i>two</i></pre>" in out
    assert "<p>next\nline</p>" in out


def test_tables_attributes_spans_captions_inline_cells():
    src = """{| class="wikitable"
|+ The ''caption''
|-
! Name !! colspan="2" | Both
|-
| a || style="text-align: right" | b || c
|-
| rowspan="2" | tall
| multi
line
|}after"""
    out = _html(src)
    assert "<caption>The <i>caption</i></caption>" in out
    assert '<thead><tr><th>Name</th><th colspan="2">Both</th></tr></thead>' in out
    assert '<td style="text-align: right">b</td>' in out and '<td rowspan="2">tall</td>' in out
    assert "<td>multi\nline</td>" in out
    assert out.endswith("<p>after</p>")


def test_nested_table_and_blocks_in_cells():
    out = _html("{|\n| outer\n|\n{|\n| inner\n|}\n|-\n| x\n\n# one\n# two\n|}")
    assert out.count("<table>") == 2 and "<td>inner</td>" in out
    assert "<ol><li>one</li><li>two</li></ol>" in out
    # a cell's first line follows its marker: * there is text, not a list
    assert "<td>* not a list</td>" in _html("{|\n| * not a list\n|}")


def test_internal_links_trail_pipe_trick_and_categories():
    out = _html("[[Dog]]s [[Help:Contents|]] [[Foo (bar)|]] [[Page#Some part|text]] [[#Local]] "
                "[[Category:Hidden]] [[:Category:Shown]]")
    assert '<a href="Dog">Dogs</a>' in out
    assert '<a href="Help:Contents">Contents</a>' in out and '<a href="Foo_(bar)">Foo</a>' in out
    assert '<a href="Page#Some_part">text</a>' in out and '<a href="#Local">#Local</a>' in out
    assert "Hidden" not in out and '<a href="Category:Shown">Category:Shown</a>' in out


def test_external_and_bare_links():
    out = _html("[https://a.example/x ''A''] [https://b.example] [//c.example c] see https://d.example/p, (https://e.example/q)")
    assert '<a href="https://a.example/x"><i>A</i></a>' in out
    assert '<a href="https://b.example">[1]</a>' in out
    assert '<a href="https://c.example">c</a>' in out
    assert '<a href="https://d.example/p">https://d.example/p</a>,' in out
    assert '<a href="https://e.example/q">https://e.example/q</a>)' in out


def test_refs_named_reused_and_list_defined():
    out = _html("A<ref name=\"x\">First ''note''.</ref> B<ref name=\"x\"/> C<ref name=late/> D<ref>Plain</ref>\n"
                '<references>\n<ref name="late">Defined later</ref>\n</references>')
    assert out.count('href="#fn1"') == 2 and out.count('class="noteref"') == 4
    assert '<aside epub:type="footnote" id="fn1"><p>First <i>note</i>.</p></aside>' in out
    assert '<aside epub:type="footnote" id="fn2"><p>Defined later</p></aside>' in out
    assert '<aside epub:type="footnote" id="fn3"><p>Plain</p></aside>' in out


def test_math_code_nowiki_entities_and_html_tags():
    out = _html('E <math>x^2</math> and\n<math display="block">\\frac{a}{b}</math>\nmore '
                "<nowiki>''raw'' [[x]]</nowiki> &amp; &eacute; H<sub>2</sub>O <s>gone</s> <u>u</u> <br/>"
                '<span style="color:red">r</span> <foo>shown</foo>')
    assert out.count("<math") == 2 and 'display="block"' in out and "x^2" in out
    assert "''raw'' [[x]]" in out and "&amp; é" in out
    assert "H<sub>2</sub>O <s>gone</s> <u>u</u> <br/>" in out
    assert '<span style="color:red">r</span>' in out and "&lt;foo&gt;shown&lt;/foo&gt;" in out


def test_block_math_splits_its_paragraph():
    out = _html('Before\n<math display="block">a=b</math>\nafter')
    assert out.startswith("<p>Before</p><p><math") and out.endswith("<p>after</p>")


def test_code_blocks():
    out = _html('<syntaxhighlight lang="python">\nif x < 1:\n    pass\n</syntaxhighlight>\n'
                "<pre>a &lt; b\n  ''kept''</pre>\n<source lang=c>int x;</source> "
                '<syntaxhighlight lang="sh" inline>ls -l</syntaxhighlight>')
    assert '<pre><code class="language-python">if x &lt; 1:\n    pass</code></pre>' in out
    assert "<pre>a &lt; b\n  ''kept''</pre>" in out
    assert '<code class="language-c">int x;</code>' in out and "<code>ls -l</code>" in out


def test_templates_left_out_and_counted():
    body, lost = mediawiki.to_html("{{Infobox|a={{x|{{y}}}}|b=c}}\nText {{cite|z}} end {{{param}}}.\n'''{{bold}}''' ok")
    assert "Infobox" not in body and "cite" not in body and "param" not in body
    assert "<p>Text  end .\n<b></b> ok</p>" in body
    assert any(m.startswith("4 MediaWiki templates") for m in lost)


def test_magic_words_comments_and_blockquote():
    out = _html("__TOC__\nA<!-- hidden -->B\n<!-- whole line -->\nC\n<blockquote>\nQ ''one''\n\nQ two\n</blockquote>")
    assert "TOC" not in out and "hidden" not in out
    assert "<p>AB\nC</p>" in out
    assert "<blockquote><p>Q <i>one</i></p><p>Q two</p></blockquote>" in out


def test_html_lists_hold_wikitext():
    out = _html("<ol start=\"3\">\n<li>three ''x''</li>\n<li>four\n* sub</li>\n</ol>")
    assert out.startswith('<ol start="3"><li>three <i>x</i></li><li>four<ul><li>sub</li></ul></li></ol>')


def test_pictures_local_or_caption(tmp_path):
    (tmp_path / "Here.png").write_bytes(png())
    book = _read(tmp_path, "[[File:Here.png|thumb|A ''shown'' one]]\n\n[[File:Missing.jpg|thumb|Only caption]]\n\n"
                           "Inline [[Image:Here.png|20px|alt=An icon]] and [[File:../../x.png|thumb|out]].")
    body = _body(book)
    assert body.count("<img") == 2 and len(book.images()) == 1
    assert "<figcaption>A <i>shown</i> one</figcaption>" in body
    assert "<figure><figcaption>Only caption</figcaption></figure>" in body
    assert 'alt="An icon"' in body
    assert any("outside the document's folder" in m and "../../x.png" in m for m in book.lost)
    assert any("pictures named in the page" in m for m in book.lost)


def test_gallery_poem_and_unknown_tags():
    body, lost = mediawiki.to_html("<gallery>\nFile:A.jpg|First\nB.jpg|Second\n</gallery>\n"
                                   "<poem>\nline one\n  line two\n\nstanza</poem>\n<score>\\relative c' { c }</score>")
    assert "<figcaption>First</figcaption>" in body and "<figcaption>Second</figcaption>" in body
    assert "<p>line one<br/>\xa0\xa0line two</p><p>stanza</p>" in body
    assert '<pre class="raw" data-format="mediawiki">&lt;score&gt;' in body
    assert any("<score>" in m for m in lost)


def test_read_title_and_metadata(tmp_path):
    book = _read(tmp_path, "Some text.", name="Mona_Lisa.wiki")
    assert book.metadata.title == "Mona Lisa" and book.source_format == "wiki"
    assert book.metadata.language == "en"


# --- hostile input ---------------------------------------------------------------------------------

def _quick(text: str, limit: float = 5.0) -> str:
    t = time.perf_counter()
    out = _html(text)
    assert time.perf_counter() - t < limit
    return out


def test_hostile_nesting_and_unclosed_markup():
    _quick("{{" * 50_000 + "x")
    _quick("[[" * 50_000 + "x" + "]]" * 50_000)
    _quick("<ref>" * 20_000)
    _quick("<blockquote>" * 5_000 + "x" + "</blockquote>" * 5_000)
    _quick("{|\n|\n" * 3_000 + "x")
    _quick("*" * 10_000 + " deep")
    _quick("'''''" * 20_000)
    _quick("<!--" * 20_000)
    _quick("<nowiki>" * 20_000)
    _quick("[http://" * 20_000)


def test_hostile_deep_output_stays_parseable(tmp_path):
    book = _read(tmp_path, "<blockquote>" * 300 + "deep" + "</blockquote>" * 300 + "\n" + "#" * 500 + " x")
    assert "deep" in book.text()
    root = book.tree(book.spine[0])
    assert not markup.too_deep(root)


def test_hostile_ref_loops(tmp_path):
    out = _html('<ref name="a">see <ref name="a"/></ref> and <ref name="b"><ref name="b">x</ref></ref>')
    assert out.count('class="noteref"') >= 1


# --- writing ---------------------------------------------------------------------------------------

def _book_from_html(body: str):
    root = markup.parse(f'<html xmlns:epub="{markup.EPUB_NS}"><head><title>T</title></head><body>{body}</body></html>')
    book = bookport.Book(bookport.Metadata(title="T"))
    book.add_document("text/a.xhtml", root)
    return book


def _write(tmp_path, body: str) -> str:
    out = tmp_path / "out.wiki"
    _book_from_html(body).write(out, "wiki")
    return out.read_text(encoding="utf-8")


def test_write_blocks(tmp_path):
    text = _write(tmp_path, "<h1>Top</h1><h3>Sub</h3><p>Some <em>it</em>, <strong>bold</strong>, <code>a[1]</code>, "
                            "<del>x</del>, H<sub>2</sub>O.</p><hr/><pre><code class='language-python'>x = 1</code></pre>"
                            "<pre>a &lt; b</pre><blockquote><p>q</p></blockquote>")
    assert "= Top =" in text and "=== Sub ===" in text
    assert "Some ''it'', '''bold''', <code>a&#91;1&#93;</code>, <s>x</s>, H<sub>2</sub>O." in text
    assert '<syntaxhighlight lang="python">\nx = 1\n</syntaxhighlight>' in text
    assert "<pre>a < b</pre>" in text and "<blockquote>\nq\n</blockquote>" in text and "----" in text


def test_write_lists_tables_links_notes(tmp_path):
    text = _write(tmp_path, '<ul><li>a<ol><li>b</li></ol></li><li>c</li></ul><ol start="4"><li>four</li></ol>'
                            "<dl><dt>T</dt><dd>D</dd></dl>"
                            '<table><caption>Cap</caption><tr><th>H</th><th colspan="2">W</th></tr><tr><td>1</td><td>2</td>'
                            "<td>a|b</td></tr></table>"
                            '<p><a href="https://x.example">site</a> <a href="Other_page">other</a> '
                            '<img src="pic.png" alt="P"/> call<a class="noteref" epub:type="noteref" href="#n1">1</a></p>'
                            '<aside epub:type="footnote" id="n1"><p>The note.</p></aside>')
    assert "* a\n*# b\n* c" in text
    assert '<ol start="4">\n<li>four</li>\n</ol>' in text
    assert "; T\n: D" in text
    assert '{| class="wikitable"\n|+ Cap\n! H\n! colspan="2" | W\n|-\n| 1\n| 2\n| a&#124;b\n|}' in text
    assert "[https://x.example site]" in text and "[[Other page|other]]" in text
    assert "[[File:images/pic.png|P]]" in text
    assert "call<ref>The note.</ref>" in text and text.rstrip().endswith("<references />")


def test_write_escapes_wiki_syntax(tmp_path):
    text = _write(tmp_path, "<p>* not a list</p><p>a ''b'' [[c]] {{d}} ~~~~ x</p>")
    assert "<nowiki/>* not a list" in text
    assert "''" not in text.split("\n")[-2] and "[[" not in text and "{{" not in text


def test_round_trip(tmp_path):
    src = ("== Intro ==\nSome ''italic'' and '''bold''' text with a [[Target|link]].<ref>A note.</ref>\n\n"
           "* one\n** two\n# three\n\n{|\n! h1 !! h2\n|-\n| c1 || c2\n|}\n\n<math>x+1</math>\n")
    first = _read(tmp_path, src)
    out = tmp_path / "again.wiki"
    first.write(out)
    second = bookport.read(out)
    assert second.text().split() == first.text().split()
    for tag in ("h2", "i", "b", "li", "td", "th", "math", "aside"):
        assert _body(first).count(f"<{tag}") == _body(second).count(f"<{tag}"), tag
