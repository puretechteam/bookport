"""Pandoc's Markdown in the Markdown reader and writer, and Pandoc-style footnotes in the HTML reader."""

import time

import bookport
from bookport import markup
from bookport.fmt import html as html_fmt
from bookport.fmt import markdown


def _body(book) -> str:
    return "".join(book.resources[h].data.decode() for h, _ in book.documents())


def test_footnotes_defined_inline_and_reused():
    out = markdown.to_html("A[^a] and B^[Inline *note*.] and A again[^a]; [^nope] stays.\n\n"
                           "[^a]: First line\n    continued.\n\n    Second paragraph.\n")
    assert out.count('href="#fn1"') == 2 and out.count('class="noteref"') == 3
    assert "[^nope]" in out
    assert '<aside epub:type="footnote" id="fn1"><p>First line\ncontinued.</p>\n<p>Second paragraph.</p></aside>' in out
    assert '<aside epub:type="footnote" id="fn2"><p>Inline <em>note</em>.</p></aside>' in out


def test_footnotes_across_chapters(tmp_path):
    big = "x " * 4000
    p = tmp_path / "a.md"
    p.write_text(f"# One\n\n{big}\n\nCall[^n].\n\n# Two\n\n{big}\n\nAgain[^n].\n\n[^n]: The note.\n", encoding="utf-8")
    book = bookport.read(p)
    assert len(book.spine) == 2
    second = book.resources[book.spine[1]].data.decode()
    assert 'href="part0001.xhtml#fn1"' in second  # the note lives with its first call


def test_definition_lists():
    out = markdown.to_html("Apple\n:   A red fruit.\n:   A company.\n\nOrange\n\n:   An orange fruit.\n\n"
                           "    With a second paragraph.\n")
    assert "<dt>Apple</dt><dd>A red fruit.</dd><dd>A company.</dd>" in out
    assert "<dt>Orange</dt><dd><p>An orange fruit.</p>\n<p>With a second paragraph.</p></dd>" in out


def test_fenced_divs_nest():
    out = markdown.to_html("::: warning\nOuter.\n\n:::: {#x .note .big}\nInner *text*.\n::::\n:::\n")
    assert '<div class="warning"><p>Outer.</p>\n<div id="x" class="note big"><p>Inner <em>text</em>.</p></div></div>' in out


def test_simple_multiline_headless_and_grid_tables():
    simple = markdown.to_html("  Right Left    Center  Default\n------- ------ -------- -------\n     12 12        12    12\n\n"
                              ": The caption.\n")
    assert "<caption>The caption.</caption>" in simple
    assert '<th style="text-align: right">Right</th><th style="text-align: left">Left</th>' in simple
    assert '<th style="text-align: center">Center</th><th>Default</th>' in simple
    multi = markdown.to_html("----------------------\nHead   Other\nline   more\n------ ---------\nCell   spans\n"
                             "       two lines\n\nSecond row\n----------------------\n")
    assert ">Head line</th>" in multi and ">spans two lines</td>" in multi and multi.count("<tr>") == 3
    headless = markdown.to_html("----- -----\n12    34\n56    78\n----- -----\n")
    assert headless.count("<td>") == 4 and "<th" not in headless
    grid = markdown.to_html("+-----+------+\n| A   | B    |\n+=====+======+\n| one | two  |\n| 1   |      |\n+-----+------+\n")
    assert "<thead><tr><th>A</th><th>B</th></tr></thead>" in grid and "<td>one\n1</td>" in grid


def test_sub_and_superscript_not_in_links():
    out = markdown.to_html("H~2~O and x^2^, ~~gone~~, [l](http://e.example/~a/~b) and a ~ b ~ c.")
    assert "H<sub>2</sub>O" in out and "x<sup>2</sup>" in out and "<del>gone</del>" in out
    assert 'href="http://e.example/~a/~b"' in out and "a ~ b ~ c" in out


def test_writer_writes_footnotes_as_pandoc_does(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("Text[^a] and[^b].\n\n[^a]: One.\n\n    Two.\n[^b]: Three.\n", encoding="utf-8")
    out = tmp_path / "b.md"
    bookport.read(p).write(out)
    text = out.read_text(encoding="utf-8")
    assert "Text[^1] and[^2]." in text and "[^1]: One.\n\n    Two." in text and "[^2]: Three." in text
    again = bookport.read(out)
    assert _body(again).count('class="noteref"') == 2 and "Two." in again.text()


def test_html_reader_takes_pandoc_footnotes(tmp_path):
    page = ('<html><body><p>Call<a href="#fn1" class="footnote-ref" id="fnref1" role="doc-noteref"><sup>1</sup></a>.</p>'
            '<section id="footnotes" class="footnotes footnotes-end-of-document" role="doc-endnotes"><hr />'
            '<ol><li id="fn1"><p>The note.<a href="#fnref1" class="footnote-back" role="doc-backlink">↩︎</a></p>'
            "</li></ol></section></body></html>")
    p = tmp_path / "p.html"
    p.write_text(page, encoding="utf-8")
    body = _body(bookport.read(p))
    assert 'epub:type="noteref"' in body and '<aside epub:type="footnote" id="fn1"><p>The note.</p></aside>' in body
    assert "<section" not in body and "↩" not in body
    root = markup.parse('<html><body><p>x<sup id="fnref:1"><a class="footnote-ref" href="#fn:1">1</a></sup></p>'
                        '<div class="footnote"><ol><li id="fn:1"><p>Py-Markdown note '
                        '<a class="footnote-backref" href="#fnref:1">&#8617;</a></p></li></ol></div></body></html>')
    html_fmt.footnotes(root)
    s = markup.to_string(root)
    assert '<aside epub:type="footnote" id="fn:1"><p>Py-Markdown note </p></aside>' in s and "noteref" in s


def test_hostile_markdown():
    t = time.perf_counter()
    markdown.to_html("::: a\n" * 5000)
    markdown.to_html("[^a]" * 20_000 + "\n\n[^a]: x [^a]\n")
    markdown.to_html("^[" * 20_000)
    markdown.to_html("--- ---\nx\n" * 5000)
    markdown.to_html("---\nh\n--- ---\n" * 3000)
    markdown.to_html("t\n: d\n" * 5000)
    markdown.to_html("+--+\n|" * 5000)
    assert time.perf_counter() - t < 20


def test_fancy_list_markers():
    out = markdown.to_html("#. one\n#. two\n\n(2) begins with 2\n(3) and now 3\n\niv. roman, from 4\nv.  next\n\n"
                           "A.  Upper\nB.  next\n\nc) lower from c\nd) next\n\nB. Russell was a philosopher.\n")
    assert "<ol><li>one</li><li>two</li></ol>" in out
    assert '<ol start="2"><li>begins with 2</li><li>and now 3</li></ol>' in out
    assert '<ol start="4" type="i"><li>roman, from 4</li><li>next</li></ol>' in out
    assert '<ol type="A"><li>Upper</li><li>next</li></ol>' in out
    assert '<ol start="3" type="a"><li>lower from c</li><li>next</li></ol>' in out
    assert "<p>B. Russell was a philosopher.</p>" in out  # one space after a capital and a period: not a list


def test_sublist_without_blank_line():
    out = markdown.to_html("A.  Upper Alpha\n    I.  Upper Roman.\n        (6) Decimal from 6\n").replace("\n", "")
    assert ('<ol type="A"><li>Upper Alpha<ol type="I"><li>Upper Roman.<ol start="6"><li>Decimal from 6</li></ol>'
            "</li></ol></li></ol>") in out


def test_line_blocks():
    out = markdown.to_html("| The limerick packs\n|    In space that is\n|  But the good\n   ones I've seen\n|\n")
    assert out.startswith('<div class="line-block">The limerick packs<br/>\xa0\xa0\xa0In space that is<br/>'
                          "\xa0But the good ones I\u2019ve seen<br/></div>")


def test_attributes_raw_and_autolinks():
    out = markdown.to_html("# Intro {#start .x}\n\nSetext {#s2}\n------\n\n"
                           "[small]{.smallcaps} [marked]{#m .note} [u]{.underline} `<b>x</b>`{=html} `\\x`{=tex} "
                           "`y`{.python} \\cite[22]{smith} <info:a/b> <me@example.org> <text:p>\n\n"
                           "```{=latex}\n\\begin{x}\n```\n\n``` {.haskell .literate}\nx = 1\n```\n\n"
                           "\\begin{tabular}{ll}\na & b\n\\end{tabular}\n")
    assert '<h1 id="start" class="x">Intro</h1>' in out and '<h2 id="s2">Setext</h2>' in out
    assert '<span class="smallcaps">small</span>' in out and '<span id="m" class="note">marked</span>' in out
    assert "<u>u</u>" in out and "<b>x</b>" in out and '<span class="raw" data-format="tex">\\x</span>' in out
    assert "<code>y</code>" in out and '<span class="raw" data-format="latex">\\cite[22]{smith}</span>' in out
    assert '<a href="info:a/b">info:a/b</a>' in out and '<a href="mailto:me@example.org">' in out and "<text:p>" in out
    assert '<pre class="raw" data-format="latex">\\begin{x}</pre>' in out
    assert '<pre><code class="language-haskell">x = 1</code></pre>' in out
    assert '<pre class="raw" data-format="latex">\\begin{tabular}{ll}\na &amp; b\n\\end{tabular}</pre>' in out


def test_smart_punctuation_as_pandoc_reads_it():
    out = markdown.to_html("\"Hello,\" said the spider. \"'Shelob' is my name.\" Don't -- or --- wait... "
                           "Some 'quoted `code`' and a \"[quoted link](/u)\". `don't -- touch` [l](/a--b \"t 'q'\")")
    assert ("\u201cHello,\u201d said the spider. \u201c\u2018Shelob\u2019 is my name.\u201d Don\u2019t \u2013 or "
            "\u2014 wait\u2026") in out
    assert "\u2018quoted <code>code</code>\u2019" in out and '\u201c<a href="/u">quoted link</a>\u201d' in out
    assert "<code>don&#x27;t -- touch</code>" in out and 'href="/a--b"' in out  # code keeps straight quotes


def test_markdown_inside_html_blocks_and_escaped_space():
    out = markdown.to_html("<table>\n<tr>\n<td>\nThis is *emphasized*\n</td>\n</tr>\n</table>\n\na^hello\\ there^ x")
    assert "<td>\nThis is <em>emphasized</em>\n</td>" in out
    assert "a<sup>hello\xa0there</sup> x" in out
