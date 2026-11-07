"""Typst: reading markup and element functions, writing, and hostile files."""

import re
import time

import pytest

import bookport
from bookport import markup, mathml
from bookport.fmt.markdown import book_from_markdown

from .conftest import png

DOC = """#set document(title: "A Typst sample", author: ("Ann", "Bo"))
#set heading(numbering: "1.")
#let project = "bookport"

= Introduction <intro>
This is *strong* and _emphasised_ text with `raw code` and a link to
https://typst.app. Our project is #project. See @intro and @knuth84.

The sum $sum_(i=1)^n i = (n(n+1))/2$ is well known, and

$ integral_0^1 x^2 dif x = 1/3 $

- First item
- Second item
  - nested item
+ One
+ Two

/ Term: its description

```python
print("hello")
```

#figure(
  image("fig.png", alt: "red box"),
  caption: [A red box.],
) <fig>

#table(
  columns: 2,
  table.header([*Name*], [*Value*]),
  [a], [1],
  table.cell(colspan: 2)[wide],
)

A footnote here#footnote[The note text.] and a quote:

#quote(block: true, attribution: [Someone])[Words worth quoting.]

== Escapes and words
\\* not strong \\_ \\# \\$ 5 -- 10 --- done... A line \\
break. #emph[Emphasis] #strike[gone] H#sub[2]O x#super[2] #link("https://example.org")[a link].
// a comment
Text /* inline comment */ continues.

3. Three
4. Four

#lorem(12)
"""


def _read(tmp_path, text, name="doc.typ"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    (tmp_path / "fig.png").write_bytes(png())
    return bookport.read(p)


def _body(book):
    return "".join(markup.to_string(book.tree(h).find("body")) for h, _ in book.documents())


def test_reads_markup(tmp_path):
    book = _read(tmp_path, DOC)
    assert bookport.detect(tmp_path / "doc.typ") == "typ"
    assert book.metadata.title == "A Typst sample"
    assert book.metadata.authors == ["Ann", "Bo"]
    root = book.tree(book.spine[0])
    assert [markup.text_of(h) for h in root.iter("h1")] == ["Introduction"]
    assert root.find(".//h1").get("id") == "intro"
    assert markup.text_of(root.find(".//strong")) == "strong"
    assert markup.text_of(root.find(".//em")) == "emphasised"
    assert root.find(".//p/code").text == "raw code"
    links = [a.get("href") for a in root.iter("a") if a.get("href")]
    assert "https://typst.app" in links and "#intro" in links and "https://example.org" in links
    assert "Our project is bookport." in markup.text_of(root)
    cite = root.find(".//span[@class='citation']")
    assert cite.get("data-cites") == "knuth84"
    maths = list(root.iter("math"))
    assert [m.get("display") for m in maths] == ["inline", "block"]
    assert mathml.annotation(maths[0], mathml.TYPST) == "sum_(i=1)^n i = (n(n+1))/2"
    assert maths[0].find(".//munderover") is None and maths[0].find(".//msubsup") is not None
    ul = root.find(".//ul")
    assert [markup.text_of(li).split()[0] for li in ul.findall("li")] == ["First", "Second"]
    assert ul.find("li/ul/li").text == "nested item"
    ols = root.findall(".//ol")
    assert len(ols[0]) == 2 and ols[1].get("start") == "3"
    assert root.find(".//dt").text == "Term"
    assert root.find(".//pre/code").get("class") == "language-python"
    fig = root.find(".//figure")
    assert fig.get("id") == "fig" and fig.find("img").get("alt") == "red box"
    assert markup.text_of(fig.find("figcaption")) == "A red box."
    assert book.images()  # the picture came in
    table = root.find(".//table")
    assert len(table.findall(".//th")) == 2 and table.find(".//td[@colspan='2']").text == "wide"
    assert root.find(".//a[@class='noteref']") is not None
    assert markup.text_of(root.find(".//aside")).strip() == "The note text."
    assert root.find(".//blockquote") is not None
    text = " ".join(markup.text_of(root).split())
    assert "* not strong _ # $ 5 – 10 — done…" in text
    assert "comment" not in text and "Text continues." in text
    assert root.find(".//sub").text == "2" and root.find(".//del").text == "gone"
    assert re.search(r"Lorem( \w+,?){11}", text)
    # set and show rules stay as Typst, and the book says so
    raw = [markup.text_of(p) for p in root.iter("pre") if p.get("data-format") == "typst"]
    assert raw[0].startswith("#set document(") and "#set heading(numbering: \"1.\")" in raw
    assert any("#set" in x for x in book.lost)


def test_unknown_code_kept(tmp_path):
    book = _read(tmp_path, "Value: #calc.pow(2, 3) and #mystery[x].\n\n#let f(x) = x + 1\n")
    root = book.tree(book.spine[0])
    raw = [markup.text_of(s) for s in root.iter("span") if s.get("data-format") == "typst"]
    assert raw == ["#calc.pow(2, 3)", "#mystery[x]"]
    assert any("calc.pow" in x for x in book.lost) and any("#let" in x for x in book.lost)


def test_include(tmp_path):
    (tmp_path / "part.typ").write_text("== Included\nFrom the other file.\n", encoding="utf-8")
    book = _read(tmp_path, '= Main\n#include "part.typ"\n#include "../../outside.typ"\n#include "doc.typ"\n')
    root = book.tree(book.spine[0])
    assert [markup.text_of(h) for h in root.iter("h2")] == ["Included"]
    assert any("outside.typ" in x for x in book.lost)
    assert any("includes itself" in x for x in book.lost)


@pytest.mark.parametrize("text", ["#[" * 5000, "#emph[" * 3000 + "x", "(" * 20000, "*_" * 20000, "$" * 10001,
                                  "- " * 5000 + "x", "#table(" * 2000, "\\" * 10000, "`" * 5001, "#" * 5000,
                                  "= " * 5000, "#let a = [#a]\n#a #a #a\n", '#include "doc.typ"\n'],
                         ids=[str(k) for k in range(13)])
def test_hostile(tmp_path, text):
    t = time.perf_counter()
    book = _read(tmp_path, text)
    assert time.perf_counter() - t < 10
    book.write(tmp_path / "out.typ")
    book.write(tmp_path / "out.epub")


def _counts(book):
    root_text = re.sub(r'<pre class="raw" data-format="typst">.*?</pre>', "", _body(book), flags=re.DOTALL)
    return {t: len(re.findall(rf"<{t}[ />]", root_text)) for t in ("h1", "h2", "p", "li", "table", "td", "th", "img",
                                                                    "math", "pre", "dt", "aside", "strong", "em")}


def test_round_trip(tmp_path):
    book = _read(tmp_path, DOC)
    out = tmp_path / "out" / "again.typ"
    out.parent.mkdir()
    from bookport.fmt import typst
    saved = typst.write(book, out, pictures=True)
    assert saved == ["images/fig.png"] and (out.parent / "images" / "fig.png").read_bytes() == png()
    again = bookport.read(out)
    assert _counts(again) == _counts(book)
    assert again.metadata.title == book.metadata.title
    assert [mathml.annotation(m, mathml.TYPST) for m in again.tree(again.spine[0]).iter("math")] == \
        [mathml.annotation(m, mathml.TYPST) for m in book.tree(book.spine[0]).iter("math")]
    assert _words(again) == _words(book)


def test_writes_from_markdown(tmp_path):
    src = ("# Title\n\nSome *emph*, **strong**, `code`, a [link](https://example.org) and specials: * _ # $ @ < [ ] ~ "
           "// -- ... \"quoted\".\n\n- a\n- b\n\n1. x\n2. y\n\n| h1 | h2 |\n|---|---|\n| c | d |\n\n"
           "Inline $\\frac{a}{b}$ and\n\n$$\\sum_{i=1}^n i$$\n\n```js\nlet x = 1;\n```\n\n> quoted\n")
    book = book_from_markdown(src, "t")
    out = tmp_path / "t.typ"
    book.write(out)
    text = out.read_text(encoding="utf-8")
    assert "$frac(a, b)$" in text and "$ sum_(i = 1)^n i $" in text and "```js" in text
    assert "#emph[emph]" in text and "#link(\"https://example.org\")[link]" in text
    again = bookport.read(out)
    a, b = _counts(book), _counts(again)
    assert a == b
    def straight(s):  # Typst and the Markdown reader may each make quotes curly
        return s.replace("“", '"').replace("”", '"')

    assert straight(_words(again)) == straight(_words(book))


def _words(book):
    """The text outside equations and kept-as-Typst code, without spacing."""
    body = markup.parse(markup.to_string(book.tree(book.spine[0]))).find("body")
    for parent in list(body.iter()):
        for c in list(parent):
            if c.tag == "math" or c.get("data-format") == "typst":
                tail = c.tail
                parent.remove(c)
                markup.append_text(parent, tail or "")
    return "".join(markup.text_of(body).split())


def test_bibliography(tmp_path):
    (tmp_path / "refs.bib").write_text("@book{knuth84, author = {Donald E. Knuth}, title = {The TeXbook}, year = {1984}}\n",
                                       encoding="utf-8")
    book = _read(tmp_path, '= Doc\nAs @knuth84 shows, and @missing.\n\n#bibliography("refs.bib")\n#bibliography("../x.bib")\n')
    root = book.tree(book.spine[0])
    cites = [markup.text_of(s) for s in root.iter("span") if s.get("class") == "citation"]
    assert cites == ["(Knuth 1984)", "(missing?)"]
    assert root.find(".//div[@id='ref-knuth84']") is not None
    assert any("missing" in x for x in book.lost) and any("x.bib" in x for x in book.lost)


def test_list_item_with_a_block_running_on(tmp_path):
    """A list item whose #[...] runs over several lines (Typst's own test suite) used to hang."""
    t = time.perf_counter()
    body = _body(_read(tmp_path, "- Level 1\n  - Level #[\n2 through content block\n]\n"))
    assert time.perf_counter() - t < 5
    assert re.search(r"<ul><li><p>Level 1</p>\s*<ul><li>Level\s+2 through content block</li></ul></li></ul>", body)


def test_call_followed_by_text(tmp_path):
    """`#name` followed by more than its arguments (`#x + 1`) keeps the rest as text, not a crash."""
    body = _body(_read(tmp_path, "#let x = 5\nThe value is #x + 1 here.\n"))
    assert "here." in body


def test_call_stops_at_what_is_not_an_argument():
    """`#f(x) rest` once ended in KeyError: ' ' (a bracket was looked up for the character after the arguments)."""
    from bookport.book import Book
    from bookport.fmt.typst import _Reader
    reader = _Reader(Book(), None, None)
    assert "rest &amp; more" in reader._call("#strong(x) rest & more", False)
