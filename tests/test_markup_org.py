"""Org: reading (headlines, options, markup, lists, tables, blocks, links, notes, includes, hostile files) and writing."""

import time
import xml.etree.ElementTree as ET

import pytest

import bookport
from bookport import markup
from bookport.fmt import org


def _read(tmp_path, text: str, name: str = "doc.org", **files) -> bookport.Book:
    d = tmp_path / "docs"
    d.mkdir(exist_ok=True)
    for fname, data in files.items():
        p = d / fname.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data) if isinstance(data, bytes) else p.write_text(data, encoding="utf-8")
    src = d / name
    src.write_text(text, encoding="utf-8")
    return bookport.read(src, "org")


def _body(book) -> ET.Element:
    body = ET.Element("body")
    for href, _ in book.documents():
        body.extend(list(book.tree(href).find("body")))
    return body


def _html(book) -> str:
    return "".join(markup.to_string(c) for c in _body(book))


def _count(book, tag) -> int:
    return sum(1 for _ in _body(book).iter(tag))


def test_keywords_headlines_todo_priority_tags(tmp_path):
    text = ("#+TITLE: The Title\n#+AUTHOR: A. Writer\n#+DATE: 2024\n#+TODO: TODO WAIT | DONE GONE\n\n"
            "* TODO [#A] First :work:home:\n** WAIT Second\n*** GONE Third\n* COMMENT Hidden\nsecret\n"
            "* Skipped :noexport:\nsecret too\n* Last\n")
    book = _read(tmp_path, text)
    assert book.metadata.title == "The Title" and book.metadata.authors == ["A. Writer"]
    heads = [e for e in _body(book) if e.tag.startswith("h")]
    assert [e.tag for e in heads] == ["h1", "h2", "h3", "h1"]
    assert markup.text_of(heads[0]) == "TODO First\u00a0work\u00a0home"
    assert heads[1].find("span").get("class") == "todo WAIT" and heads[2].find("span").get("class") == "done GONE"
    assert "secret" not in _html(book) and "[#A]" not in _html(book)


def test_headlines_deeper_than_h_become_list_items(tmp_path):
    body = _body(_read(tmp_path, "#+OPTIONS: H:2\n* One\n** Two\n*** Three\ntext\n*** Four\n"))
    assert [e.tag for e in body] == ["h1", "h2", "ol"]
    assert [markup.text_of(li.find("p")) for li in body.find("ol")] == ["Three", "Four"]


def test_options_num_and_subscripts(tmp_path):
    html = _html(_read(tmp_path, "#+OPTIONS: num:t ^:{}\n* A\n** B\na_b a_{b} x^2 x^{2}\n"))
    assert "1 </span>A" in html and "1.1 </span>B" in html
    assert "a_b a<sub>b</sub> x^2 x<sup>2</sup>" in html
    html = _html(_read(tmp_path, "#+OPTIONS: ^:nil\na_{b}\n"))
    assert "<sub>" not in html


def test_emphasis_border_rules(tmp_path):
    text = ("*bold* /it/ _un_ =verb= ~code~ +strike+ a*b*c *nested /it/* =no *bold* here= 3/4 and 1/2\n"
            "*across\nlines* *not\n\nclosed*\n")
    html = _html(_read(tmp_path, text))
    for piece in ("<strong>bold</strong>", "<em>it</em>", "<u>un</u>", '<code class="verbatim">verb</code>',
                  "<code>code</code>", "<del>strike</del>", "a*b*c", "<strong>nested <em>it</em></strong>",
                  '<code class="verbatim">no *bold* here</code>', "3/4 and 1/2", "<strong>across\nlines</strong>"):
        assert piece in html, piece
    assert "*not" in html and "closed*" in html


def test_entities_special_strings_line_break(tmp_path):
    html = _html(_read(tmp_path, "\\alpha \\to \\nbsp{} -- --- ... \\- \\unknown\nend\\\\\nnext\n"))
    assert "\u03b1 \u2192 \u00a0 \u2013 \u2014 \u2026 \u00ad \\unknown" in html and "<br/>" in html


def test_lists_ordered_checkbox_description_counter(tmp_path):
    text = "- a\n- [X] done\n- [ ] todo\n  1. [@3] three\n  2. four\n\n- term :: definition\n  more\n"
    body = _body(_read(tmp_path, text))
    ul = body.find("ul")
    assert [markup.text_of(li).split("\n")[0][:6] for li in ul] == ["a", "\u2612 done", "\u2610 todo", "term :"]
    assert ul.find(".//ol").get("start") == "3"
    dl = _body(_read(tmp_path, "- term :: definition\n- other :: two\n")).find("dl")
    assert [markup.text_of(e) for e in dl] == ["term", "definition", "other", "two"]


def test_table_rules_alignment_cookies_caption(tmp_path):
    text = "#+CAPTION: Cap\n#+NAME: tab\n| <r> | <l> |\n| a | b |\n|---+---|\n| 1 | 2 |\n| 3 |\n"
    t = _body(_read(tmp_path, text)).find("table")
    assert t.get("id") == "tab" and t.find("caption").text == "Cap"
    assert [c.text for c in t.iter("th")] == ["a", "b"] and [c.text for c in t.iter("td")] == ["1", "2", "3", None]
    assert t.find(".//th").get("style") == "text-align: right"


def test_blocks(tmp_path):
    text = ("#+BEGIN_SRC python :exports code\nprint(1)\n,* not a headline\n#+END_SRC\n\n#+begin_src sh :exports none\nhidden\n"
            "#+end_src\n\n#+BEGIN_EXAMPLE\nex\n#+END_EXAMPLE\n\n: fixed\n: width\n\n#+begin_quote\nq\n#+begin_quote\nnested\n"
            "#+end_quote\n#+end_quote\n\n#+BEGIN_VERSE\none\n  two\n#+END_VERSE\n\n#+BEGIN_CENTER\nc\n#+END_CENTER\n\n"
            "#+BEGIN_EXPORT html\n<b>raw</b>\n#+END_EXPORT\n\n#+begin_export latex\n\\foo\n#+end_export\n\n"
            "#+begin_comment\nno\n#+end_comment\n\n#+begin_note\nspecial\n#+end_note\n\n-----\n")
    body = _body(_read(tmp_path, text))
    pres = [markup.text_of(e) for e in body.iter("pre")]
    assert pres == ["print(1)\n* not a headline", "ex", "fixed\nwidth", "\\foo"]
    assert body.find(".//code").get("class") == "language-python"
    html = "".join(markup.to_string(c) for c in body)
    assert "hidden" not in html and ">no<" not in html
    assert len(list(body.iter("blockquote"))) == 2 and 'class="line-block verse"' in html
    assert "<b>raw</b>" in html and '<div class="note"><p>special</p></div>' in html and "<hr/>" in html


def test_drawers_planning_properties(tmp_path):
    text = ("* H\nSCHEDULED: <2024-01-01 Mon>\n:PROPERTIES:\n:CUSTOM_ID: myid\n:END:\n:LOGBOOK:\nlog\n:END:\n"
            ":NOTES:\nkept\n:END:\ntext\n")
    body = _body(_read(tmp_path, text))
    assert body.find("h1").get("id") == "myid"
    html = "".join(markup.to_string(c) for c in body)
    assert "SCHEDULED" not in html and "log" not in html and "kept" in html


def test_links_internal_and_external_and_images(tmp_path):
    text = ("* Intro\n:PROPERTIES:\n:CUSTOM_ID: cid\n:END:\n<<tgt>>here\n#+NAME: named\n| x |\n\n"
            "[[*Intro][h]] [[#cid][c]] [[tgt][t]] [[named][n]] [[Intro]] [[https://x.org][X]] https://y.org\n"
            "[[file:other.org][o]] [[doi:10.1/x][d]] [[info:emacs::Top][i]] [[missing][gone]] [[./p.png]]\n"
            "#+LINK: gh https://github.com/%s\n[[gh:org/repo][g]]\n")
    links = {markup.text_of(a): a.get("href") for a in _body(_read(tmp_path, text)).iter("a")}
    assert links == {"h": "#cid", "c": "#cid", "t": "#tgt", "n": "#named", "Intro": "#cid", "X": "https://x.org",
                     "https://y.org": "https://y.org", "o": "other.html", "d": "https://doi.org/10.1/x",
                     "i": "https://www.gnu.org/software/emacs/manual/html_mono/emacs.html", "g": "https://github.com/org/repo"}


def test_figure_from_caption_and_image_link(tmp_path):
    from .conftest import png
    book = _read(tmp_path, "#+CAPTION: Pic\n[[./p.png]]\n", **{"p.png": png()})
    fig = _body(book).find("figure")
    assert fig.find("figcaption").text == "Pic" and fig.find("img").get("src").startswith("../images/")
    assert len(book.images()) == 1


def test_footnotes_reference_inline_and_section(tmp_path):
    text = ("Text[fn:a] and[fn::inline] and[fn:b: named inline] again[fn:a].\n\n* Footnotes\n\n[fn:a] Note a.\n\n"
            "[fn:unused] never called\n")
    book = _read(tmp_path, text)
    body = _body(book)
    calls = [markup.text_of(a) for a in body.iter("a") if a.get("class") == "noteref"]
    assert calls == ["1", "2", "3", "1"]
    assert [markup.text_of(a) for a in body.iter("aside")] == ["Note a.", "inline", "named inline"]
    assert not body.findall("h1") and any("nothing refers to" in line for line in book.lost)


def test_math_fragments(tmp_path):
    text = "$x$ and \\(y\\) and $$z$$ and \\[w\\] but $5 and $6 not\n\n\\begin{equation}\na=b\n\\end{equation}\n"
    maths = list(_body(_read(tmp_path, text)).iter("math"))
    assert [m.get("display") for m in maths] == ["inline", "inline", "block", "block", "block"]
    assert "$5 and $6" in _html(_read(tmp_path, text))


def test_macros_and_unknown_macros(tmp_path):
    text = "#+TITLE: T\n#+MACRO: greet Hello $1 and $2!\n{{{greet(A, B\\, C)}}} {{{title}}} {{{kbd(C-x)}}} {{{n}}}{{{n}}}\n"
    book = _read(tmp_path, text)
    html = _html(book)
    assert "Hello A and B, C!" in html and " T " in html and "<code>C-x</code>" in html and "12" in html
    assert any("kbd" in line for line in book.lost)


def test_citations_with_bibliography(tmp_path):
    bib = "@book{doe99, author={John Doe}, title={A Book}, year={1999}, publisher={P}}\n"
    text = "#+bibliography: refs.bib\nSee [cite:@doe99] and [cite/t:see @doe99 p. 3].\n\n#+print_bibliography:\n"
    body = _body(_read(tmp_path, text, **{"refs.bib": bib}))
    cites = [markup.text_of(s) for s in body.iter("span") if s.get("class") == "citation"]
    assert cites == ["(Doe 1999)", "see Doe (1999, p. 3)"]
    assert body.find(".//div[@class='csl-entry']") is not None


def test_select_tags(tmp_path):
    text = "#+SELECT_TAGS: yes\nintro\n* A\n** B :yes:\n*** C\n** D\n* E\n"
    heads = [markup.text_of(h).split("\u00a0")[0] for h in _body(_read(tmp_path, text)) if h.tag.startswith("h")]
    assert heads == ["A", "B", "C"]


def test_include_lines_src_minlevel_and_hostile(tmp_path):
    (tmp_path / "secret.org").write_text("SECRET", encoding="utf-8")
    text = ('#+INCLUDE: "part.org" :minlevel 2\n#+INCLUDE: "code.py" src python :lines "2-3"\n'
            '#+INCLUDE: "../../secret.org"\n#+INCLUDE: "/etc/passwd"\n#+INCLUDE: "loop.org"\n#+SETUPFILE: "setup.org"\n'
            "{{{m}}}\n")
    book = _read(tmp_path, text, **{"part.org": "* Part\nbody\n", "code.py": "a\nb\nc\n", "loop.org": '#+INCLUDE: "loop.org"\n',
                                    "setup.org": "#+MACRO: m from setup\n"})
    body = _body(book)
    assert body.find("h2") is not None and markup.text_of(body.find(".//pre")) == "b"
    html = _html(book)
    assert "SECRET" not in html and "from setup" in html
    assert sum("outside the document's folder" in line for line in book.lost) == 2
    assert any("includes itself" in line for line in book.lost)


def test_hostile_inputs_are_fast(tmp_path):
    texts = ["*a " * 30000, "/" * 50000, "[[" * 20000, "[fn:" * 10000, "$" * 40000,
             "".join(" " * k + "- x\n" for k in range(500)),
             "#+MACRO: a $1$1$1$1\n" + "{{{a(" * 30 + "x" + ")}}}" * 30,
             "#+MACRO: b {{{b}}}{{{b}}}\n{{{b}}}\n", "* " * 5000, "|" * 30000]
    t = time.perf_counter()
    for text in texts:
        assert _read(tmp_path, text).spine
    assert time.perf_counter() - t < 15


def test_reads_back_what_it_writes(tmp_path):
    src = ("#+TITLE: T\n\n* Head\nA *b* /c/ =d= [[https://x.org][link]] note[fn:1].\n\n- one\n- two\n  1. nested\n\n"
           "- term :: def\n\n| a | b |\n|---+---|\n| 1 | 2 |\n\n#+begin_src python\nx = 1\n#+end_src\n\n#+begin_quote\nq\n"
           "#+end_quote\n\n** Sub\n\\(x^2\\) and a_{b}\n\n[fn:1] The note.\n")
    book = _read(tmp_path, src)
    out = tmp_path / "out.org"
    org.write(book, out)
    again = bookport.read(out, "org")
    assert again.metadata.title == "T"
    for tag in ("h1", "h2", "p", "li", "dt", "table", "td", "th", "pre", "em", "strong", "code", "a", "aside",
                "blockquote", "math", "sub"):
        assert _count(again, tag) == _count(book, tag), tag


def test_writer_keeps_plain_text_plain(tmp_path):
    b = bookport.Book(bookport.Metadata(title="T"))
    root, body = markup.new_document("T")
    ET.SubElement(body, "p").text = "* not a headline, 2*3*4, a_b, $5 and $6, [[x]], \\alpha"
    ET.SubElement(body, "p").text = "- not a list"
    b.add_document("text/a.xhtml", root)
    out = tmp_path / "w.org"
    org.write(b, out)
    again = [markup.text_of(p).replace("\u200b", "") for p in _body(bookport.read(out, "org")).iter("p")]
    assert again == ["* not a headline, 2*3*4, a_b, $5 and $6, [[x]], \\alpha", "- not a list"]


@pytest.mark.parametrize("text", ["", "*", "* ", "#+", "#+BEGIN_SRC", "[fn:", "|", "- ", ":END:", "\\begin{x}"])
def test_odd_tiny_inputs_read(tmp_path, text):
    assert _read(tmp_path, text).spine
