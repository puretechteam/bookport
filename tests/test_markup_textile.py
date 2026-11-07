"""Textile: reading block signatures, lists, tables, phrase modifiers, links and glyphs; writing it back."""

import time

import bookport
from bookport import markup
from bookport.fmt import textile

from .conftest import png


def _html(text: str, base=None) -> str:
    return textile.to_html(text, base)[0]


def _body(book) -> str:
    return "".join(book.resources[h].data.decode() for h, _ in book.documents())


def test_block_signatures_and_attributes():
    out = _html("h1. Title\n\nh2(intro#start){color:red}[en]. Second\n\np>. Right aligned\n\n"
                "bq.:https://cite.example Quoted text\n\nbc(python). x = 1\nif x:\n    y = 2\n\n"
                "pre. keep   spaces\n\n###. a comment\n\nnotextile. <b>raw</b> *kept*\n\nplain\nnext line")
    assert "<h1>Title</h1>" in out
    assert '<h2 class="intro" id="start" lang="en" style="color:red">Second</h2>' in out
    assert '<p style="text-align: right">Right aligned</p>' in out
    assert '<blockquote cite="https://cite.example"><p>Quoted text</p></blockquote>' in out
    assert '<pre><code class="language-python">x = 1\nif x:\n    y = 2</code></pre>' in out
    assert "<pre>keep   spaces</pre>" in out and "comment" not in out
    assert "<b>raw</b> *kept*" in out
    assert "<p>plain<br/>next line</p>" in out  # a line break in Textile is kept


def test_extended_blocks():
    out = _html("bc.. first\n\nsecond\n\np. after\n\nbq.. one\n\ntwo\n\nh3. Next")
    assert "<pre><code>first\n\nsecond</code></pre><p>after</p>" in out
    assert "<blockquote><p>one</p><p>two</p></blockquote><h3>Next</h3>" in out


def test_lists_and_definitions():
    out = _html("* one\n** nested\n*# mixed\n* two\n\n#3 three\n# four\n\n- coffee := Hot and black\n- milk :=\n"
                "White and\ncold =:")
    assert "<ul><li>one<ul><li>nested</li></ul><ol><li>mixed</li></ol></li><li>two</li></ul>" in out
    assert '<ol start="3"><li>three</li><li>four</li></ol>' in out
    assert "<dt>coffee</dt><dd>Hot and black</dd><dt>milk</dt><dd>White and<br/>cold</dd>" in out


def test_tables():
    out = _html("table(grid).\n|=. The caption\n|_. Name |_. Age |\n|\\2. both columns |\n|/2. tall | a |\n| b |\n"
                "|<. left |>. right |")
    assert '<table class="grid"><caption>The caption</caption>' in out
    assert "<thead><tr><th>Name</th><th>Age</th></tr></thead>" in out
    assert '<td colspan="2">both columns</td>' in out and '<td rowspan="2">tall</td>' in out
    assert '<td style="text-align: left">left</td><td style="text-align: right">right</td>' in out


def test_phrase_modifiers():
    out = _html("*strong* _em_ **bold** __italic__ ??cite?? -del- +ins+ x[^sup^] H[~2~]O @a*b@ %(note)span% "
                "in[*word*]s _*both*_ snake_case_name re-enter")
    for want in ("<strong>strong</strong>", "<em>em</em>", "<b>bold</b>", "<i>italic</i>", "<cite>cite</cite>",
                 "<del>del</del>", "<ins>ins</ins>", "<sup>sup</sup>", "<sub>2</sub>", "<code>a*b</code>",
                 '<span class="note">span</span>', "in<strong>word</strong>s", "<em><strong>both</strong></em>"):
        assert want in out, want
    assert "snake_case_name re-enter" in out


def test_links_images_footnotes_acronyms_glyphs(tmp_path):
    (tmp_path / "pic.png").write_bytes(png())
    text = ('A "link (a title)":https://x.example/a, ["bracketed":https://y.example]x, "$":https://z.example and '
            '"alias":ali.\n\n[ali]https://alias.example\n\n'
            "!pic.png(The alt)! !missing.png(Gone)! !pic.png!:https://to.example\n\n"
            "A note[1] and not[9].\n\nfn1. The footnote.\n\n"
            'ABC(A Big Corp) said "quoted" and it\'s -- (c) (r) (tm) 2 x 4 ... - fine\n\n==*not* bold==')
    p = tmp_path / "t.textile"
    p.write_text(text, encoding="utf-8")
    book = bookport.read(p)
    body = _body(book)
    assert '<a href="https://x.example/a" title="a title">link</a>,' in body
    assert '<a href="https://y.example">bracketed</a>x' in body
    assert '<a href="https://z.example">https://z.example</a>' in body
    assert '<a href="https://alias.example">alias</a>' in body
    assert body.count("<img") == 2 and len(book.images()) == 1 and 'alt="The alt"' in body and "Gone" in body
    assert '<a href="https://to.example"><img' in body
    assert '<a class="noteref" epub:type="noteref" href="#fn1"><sup>1</sup></a>' in body and "not[9]" in body
    assert '<aside epub:type="footnote" id="fn1"><p>The footnote.</p></aside>' in body
    assert '<abbr title="A Big Corp">ABC</abbr>' in body
    assert "\u201cquoted\u201d" in body and "it\u2019s" in body and "\u2014" in body
    assert "\u00a9 \u00ae \u2122 2\u00d74 \u2026 \u2013 fine" in body
    assert "*not* bold" in body
    assert any("aren't beside it" in m for m in book.lost)


def test_html_passthrough_with_textile_inside():
    out = _html('<div class="box">\n\nh2. Inside\n\nSome *text*.\n\n</div>\n\nAfter <span>inline</span> <foo>.')
    assert '<div class="box">' in out and "<h2>Inside</h2>" in out and "<strong>text</strong>" in out
    assert "</div>" in out and "<span>inline</span> &lt;foo&gt;." in out


def test_hostile_input():
    t = time.perf_counter()
    _html("*" * 50_000)
    _html("_" * 50_000 + "x")
    _html('"' * 50_000 + ":")
    _html("[" * 30_000 + "]" * 30_000)
    _html("!" * 50_000)
    _html("<div>\n" * 20_000)
    _html("|" * 50_000)
    _html("*" * 5000 + " deep")
    _html("bc.. x\n\n" * 5000)
    assert time.perf_counter() - t < 20


def test_hostile_nesting_stays_parseable(tmp_path):
    p = tmp_path / "deep.textile"
    p.write_text("*" * 400 + " deep\n\n" + "<div>\n\n" * 300 + "text", encoding="utf-8")
    book = bookport.read(p)
    assert "deep" in book.text() and not markup.too_deep(book.tree(book.spine[0]))


# --- writing ---------------------------------------------------------------------------------------

def _book_from_html(body: str):
    root = markup.parse(f'<html xmlns:epub="{markup.EPUB_NS}"><head><title>T</title></head><body>{body}</body></html>')
    book = bookport.Book(bookport.Metadata(title="T"))
    book.add_document("text/a.xhtml", root)
    return book


def test_write_textile(tmp_path):
    out = tmp_path / "out.textile"
    _book_from_html('<h2 id="x">Head</h2><p>Some <em>em</em> <strong>st</strong> <code>c</code> <del>d</del> '
                    'H<sub>2</sub>O x<sup>2</sup> a*b_c <a href="https://e.example">link</a>'
                    '<a class="noteref" epub:type="noteref" href="#n1">1</a></p><p>* not a list</p>'
                    "<ul><li>one<ul><li>two</li></ul></li></ul><ol start=\"4\"><li>four</li></ol>"
                    "<dl><dt>T</dt><dd>D</dd></dl><pre><code class='language-py'>x = 1</code></pre>"
                    "<pre>a\n\nb &lt; c</pre><blockquote><p>q</p></blockquote>"
                    '<table><tr><th>H</th><th colspan="2">W</th></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>'
                    '<p><img src="i.png" alt="pic"/></p>'
                    '<aside epub:type="footnote" id="n1"><p>Note.</p></aside>').write(out, "textile")
    text = out.read_text(encoding="utf-8")
    assert "h2. Head" in text
    assert "Some _em_ *st* @c@ -d- H[~2~]O x[^2^] a&#42;b&#95;c \"link\":https://e.example[1]" in text
    assert "&#42; not a list" in text
    assert "* one\n** two" in text and "#4 four" in text and "- T := D" in text
    assert "bc(py). x = 1" in text and "<pre>\na\n\nb < c\n</pre>" in text and "bq. q" in text
    assert "|_. H|_\\2. W|\n| 1| 2| 3|" in text
    assert "!images/i.png(pic)!" in text and text.rstrip().endswith("fn1. Note.")


def test_round_trip(tmp_path):
    src = ("h1. Title\n\nSome _em_ and *strong* with \"a link\":https://x.example.[1]\n\n* one\n** two\n# three\n\n"
           "|_. a |_. b |\n| 1 | 2 |\n\nbc. code here\n\nfn1. A note.\n")
    p = tmp_path / "a.textile"
    p.write_text(src, encoding="utf-8")
    first = bookport.read(p)
    out = tmp_path / "b.textile"
    first.write(out)
    second = bookport.read(out)
    assert second.text().split() == first.text().split()
    for tag in ("h1", "em", "strong", "a href", "li", "th", "td", "pre", "aside"):
        assert _body(first).count(f"<{tag}") == _body(second).count(f"<{tag}"), tag


def test_a_table_column_in_the_wrong_place_is_no_crash(tmp_path):
    """Fuzzing found it: raw `<table><tr><col>` ended in ValueError (the column had already been moved)."""
    src = tmp_path / "a.textile"
    src.write_text("<table><tr><col>", encoding="utf-8")
    bookport.read(src, "textile").write(tmp_path / "a.html", "html")
