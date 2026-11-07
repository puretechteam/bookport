"""BibTeX/BibLaTeX reading and citations filled into a book."""

import time
import xml.etree.ElementTree as ET

from bookport import Book, Metadata, bib, markup

BIB = r"""
@string{neurips = "Advances in Neural Information Processing Systems"}
@comment{this whole entry is ignored @article{nope, title={No}}}
@preamble{"\newcommand{\noop}[1]{}"}

@inproceedings{vaswani2017attention,
  title = {Attention Is All You Need},
  author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and Uszkoreit, Jakob and others},
  booktitle = neurips # " 30",
  pages = {5998--6008},
  year = 2017
}

@article{devlin,
  author = "Jacob Devlin and Ming-Wei Chang",
  title = "{BERT}: Pre-training of Deep Bidirectional Transformers",
  journal = {NAACL},
  volume = {1}, number = {2},
  year = {2019},
  month = jun,
  doi = {10.18653/v1/N19-1423}
}

@book{knuth,
  author = {Donald E. Knuth},
  title = {The {\TeX}book},
  publisher = {Addison-Wesley},
  address = {Reading, MA},
  year = {1984}
}

@misc{goedel,
  author = {G{\"o}del, Kurt and de la Vall{\'e}e Poussin, Charles and van der Waals, J. D.},
  title = {{\"U}ber formal unentscheidbare S{\"a}tze --- and more},
  year = {1931},
  url = {https://example.org/g%C3%B6del}
}

@article{knuth2,
  author = {Knuth, Donald E.},
  title = {Literate Programming},
  journal = {The Computer Journal},
  year = {1984}
}
"""


def _book(*spans: str, placeholder: bool = False) -> Book:
    root, body = markup.new_document("t")
    p = ET.SubElement(body, "p")
    for s in spans:
        p.append(ET.fromstring(s))
    if placeholder:
        ET.SubElement(body, "div", {"class": "references", "id": "refs"})
        ET.SubElement(body, "p").text = "after"
    b = Book(Metadata(title="t"))
    b.add_document("text/a.xhtml", root)
    return b


def _texts(book: Book) -> list[str]:
    return [markup.text_of(s) for s in book.tree("text/a.xhtml").iter("span") if "citation" in s.get("class", "")]


def test_parse_strings_concatenation_comments_and_fields():
    e = bib.parse(BIB)
    assert set(e) == {"vaswani2017attention", "devlin", "knuth", "goedel", "knuth2"}
    v = e["vaswani2017attention"]
    assert v.type == "inproceedings"
    assert v.get("booktitle") == "Advances in Neural Information Processing Systems 30"
    assert v.get("pages") == "5998–6008" and v.get("year") == "2017"
    assert e["devlin"].get("title") == "BERT: Pre-training of Deep Bidirectional Transformers"
    assert e["devlin"].get("month") == "June"
    assert e["knuth"].get("title") == "The TeXbook"
    assert e["goedel"].get("title") == "Über formal unentscheidbare Sätze — and more"


def test_names_in_every_form():
    names = bib.parse(BIB)["goedel"].names()
    assert [(n.first, n.von, n.last) for n in names] == [
        ("Kurt", "", "Gödel"), ("Charles", "de la", "Vallée Poussin"), ("J. D.", "van der", "Waals")]
    n = bib.parse_names("Jacob Devlin and Ming-Wei Chang and {World Health Organization} and others")
    assert [x.last for x in n[:3]] == ["Devlin", "Chang", "World Health Organization"] and n[3].others
    jr = bib.parse_names("King, Jr, Martin Luther")[0]
    assert (jr.first, jr.last, jr.jr) == ("Martin Luther", "King", "Jr")
    assert bib.parse_names("Ludwig van Beethoven")[0].family() == "van Beethoven"


def test_detex_accents_symbols_and_math():
    assert bib.detex(r"Erd{\H o}s and {\v{C}}ech, \c{c}a, {\ss}, \aa, \o, \'{\i}") == "Erdős and Čech, ça, ß, å, ø, í"
    assert bib.detex(r"The $\alpha$-test -- ``quoted'' \emph{text}~here") == "The α-test – “quoted” text here"


def test_author_year_citations_and_list():
    book = _book('<span class="citation" data-cites="vaswani2017attention">x</span>',
                 '<span class="citation" data-cites="devlin knuth" data-cite-mode="textual">x</span>',
                 '<span class="citation" data-cites="vaswani2017attention" data-cite-suffix="p. 3" '
                 'data-cite-prefix="see">x</span>',
                 '<span class="citation" data-cites="goedel" data-cite-mode="author">x</span>',
                 '<span class="citation" data-cites="knuth knuth2">x</span>',
                 '<span class="citation" data-cites="missing">x</span>', placeholder=True)
    n = bib.apply(book, bib.parse(BIB), "author-year")
    assert n == 6
    assert _texts(book)[:5] == ["(Vaswani et al. 2017)", "Devlin and Chang (2019); Knuth (1984b)",
                                "(see Vaswani et al. 2017, p. 3)", "Gödel, de la Vallée Poussin, and van der Waals",
                                "(Knuth 1984b; Knuth 1984a)"]
    assert _texts(book)[5] == "(missing?)"
    assert any("missing" in line for line in book.lost)
    body = book.tree("text/a.xhtml").find("body")
    refs = body.find("div[@class='references']")
    assert refs is not None and body[-1].text == "after"  # the list went where the placeholder was
    ids = [d.get("id") for d in refs]
    assert ids == ["ref-devlin", "ref-goedel", "ref-knuth2", "ref-knuth", "ref-vaswani2017attention"]  # by author
    vas = markup.text_of(refs[-1])
    assert vas.startswith("Vaswani, Ashish, Noam Shazeer, Niki Parmar, Jakob Uszkoreit, et al. 2017.")
    assert "“Attention Is All You Need.”" in vas and "5998–6008" in vas
    assert refs[0].find("a").get("href") == "https://doi.org/10.18653/v1/N19-1423"
    assert refs[3].find("em").text == "The TeXbook."


def test_numeric_style_orders_by_first_citation_and_compresses_ranges():
    book = _book('<span class="citation" data-cites="knuth">x</span>',
                 '<span class="citation" data-cites="devlin goedel vaswani2017attention knuth">x</span>')
    bib.apply(book, bib.parse(BIB), "numeric")
    assert _texts(book) == ["[1]", "[1–4]"]
    refs = book.tree("text/a.xhtml").find(".//div[@class='references']")
    assert [d.get("id") for d in refs] == ["ref-knuth", "ref-devlin", "ref-goedel", "ref-vaswani2017attention"]
    assert refs[0].text.startswith("[1] ")
    assert book.tree("text/a.xhtml").find("body/h1").text == "References"  # appended at the end, with a heading


def test_ready_made_entries_from_a_bbl():
    root, body = markup.new_document("t")
    p = ET.SubElement(body, "p")
    p.append(ET.fromstring('<span class="citation" data-cites="a b">x</span>'))
    refs = ET.SubElement(body, "div", {"class": "references"})
    ea = ET.SubElement(refs, "div", {"class": "csl-entry", "id": "ref-a"})
    ea.text = "Smith. A paper."
    eb = ET.SubElement(refs, "div", {"class": "csl-entry", "id": "ref-b"})
    book = Book(Metadata(title="t"))
    book.add_document("text/a.xhtml", root)
    entries = {"a": bib.Entry("a", element=ea, short="Smith", year="2001"),
               "b": bib.Entry("b", element=eb, short="Jones and Lee", year="1999")}
    bib.apply(book, entries, "author-year")
    assert _texts(book) == ["(Smith 2001; Jones and Lee 1999)"]
    assert len(book.tree("text/a.xhtml").find(".//div[@class='references']")) == 2  # nothing added
    entries = {"a": bib.Entry("a", element=ea, label="7"), "b": bib.Entry("b", element=eb, label="Jon99")}
    book2 = _book('<span class="citation" data-cites="a b">x</span>')
    bib.apply(book2, entries, "author-year")
    assert _texts(book2) == ["[7, Jon99]"]


def test_load_reads_files_and_survives_damage(tmp_path):
    good = tmp_path / "a.bib"
    good.write_text(BIB, encoding="utf-8")
    bad = tmp_path / "b.bib"
    bad.write_text("@article{x, title = {never closed\n@book{y, title={Fine}, year=2000}\n@misc{", encoding="utf-8")
    entries = bib.load([good, bad, tmp_path / "missing.bib"])
    assert "vaswani2017attention" in entries and "x" in entries
    assert bib.load([]) == {}


def test_hostile_bibliographies_are_fast():
    deep = "@misc{deep, title = " + "{" * 200_000 + "x" + "}" * 200_000 + "}"
    many = "".join(f"@misc{{k{i}, author={{A{i} and B}}, title={{T}}, year={{2000}}}}\n" for i in range(20_000))
    unclosed = '@misc{q, title = "' + "a" * 500_000
    t = time.perf_counter()
    assert "deep" in bib.parse(deep)
    assert len(bib.parse(many)) == 20_000
    assert "q" in bib.parse(unclosed)
    assert bib.parse("@" * 100_000 + "{" * 1000) == {}
    assert time.perf_counter() - t < 10
