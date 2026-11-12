"""What the 0.4 review of the markup readers and writers (and their shared helpers) found: each test
fails on the code before its fix."""

import threading
import time
import zipfile

import bookport
from bookport import css, flow, links, markup
from bookport.fmt import txt

from .conftest import png


def _within(seconds, fn):
    """Run fn, failing (not hanging) when it takes longer than ``seconds``."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed back to the test
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"still running after {seconds} s"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _page(folder, body, head="", name="index.html"):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(f"<html><head><title>T</title>{head}</head><body>{body}</body></html>", encoding="utf-8")
    return p


def _html_book(tmp_path, body, head="", **files):
    d = tmp_path / "src"
    for name, data in files.items():
        f = d / name.replace("__", "/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return bookport.read(_page(d, body, head))


def _round(tmp_path, book, ext):
    out = tmp_path / f"out.{ext}"
    book.write(out)
    return bookport.read(out), out


def _body_html(book):
    return "".join(markup.to_string(book.tree(h).find("body")) for h, _ in book.documents())


# --- the shared text writer ---------------------------------------------------------------------

def test_pictures_whose_names_clash_get_names_of_their_own(tmp_path):
    """a/x.png, b/2_x.png and c/x.png: the third picture's fallback name was taken, and the search looped forever."""
    book = _html_book(tmp_path, '<p><img src="a/x.png" alt="1"/><img src="b/2_x.png" alt="2"/><img src="c/x.png" alt="3"/></p>',
                      a__x_png=png(), **{"b__2_x.png": png(4, 4), "c__x.png": png(5, 5)})
    out = tmp_path / "o.rst"
    _within(10, lambda: book.write(out))
    text = out.read_text(encoding="utf-8")
    names = {line.split("::", 1)[1].strip() for line in text.splitlines() if "image::" in line}
    assert len(names) == 3, text


def test_pictures_never_overwrite_a_different_file_beside_the_text(tmp_path):
    book = _html_book(tmp_path, '<p><img src="cover.png" alt="c"/> text</p>', **{"cover.png": png()})
    dest = tmp_path / "dest"
    (dest / "images").mkdir(parents=True)
    (dest / "images" / "cover.png").write_bytes(b"someone else's picture")
    book.write(dest / "book.rst", pictures=True)
    assert (dest / "images" / "cover.png").read_bytes() == b"someone else's picture"
    text = (dest / "book.rst").read_text(encoding="utf-8")
    name = next(line.split("::", 1)[1].strip() for line in text.splitlines() if "image::" in line)
    assert name != "images/cover.png" and (dest / name).read_bytes() == png()
    book.write(dest / "again.rst", pictures=True)  # the same picture again: its file is reused, not copied a third time
    assert sorted(p.name for p in (dest / "images").iterdir()) == sorted(["cover.png", name.rsplit("/", 1)[1]])


def test_a_note_that_calls_itself_is_written_once(tmp_path):
    book = _html_book(tmp_path, '<p>See<a epub:type="noteref" class="noteref" href="#fn1">1</a>.</p>'
                                '<aside epub:type="footnote" id="fn1"><p>Note text <a epub:type="noteref" class="noteref" '
                                'href="#fn1">1</a></p></aside>')
    _back, out = _round(tmp_path, book, "rst")
    assert out.read_text(encoding="utf-8").count("Note text") == 1


def test_a_table_inside_a_cell_stays_in_that_cell(tmp_path):
    book = _html_book(tmp_path, "<table><tr><td>outer <table><tr><td>INNER</td></tr></table></td><td>B</td></tr></table>")
    for ext in ("rst", "org", "adoc", "md", "txt"):
        out = tmp_path / f"o.{ext}"
        book.write(out)
        text = out.read_text(encoding="utf-8-sig")
        assert text.count("INNER") == 1, (ext, text)
    blocks = flow.book_blocks(book)
    table = next(b for b in blocks if b.kind == "table")
    assert len(table.rows) == 1 and "INNER" in "".join(r.text for r in table.rows[0][0].runs)


def test_blank_lines_inside_code_blocks_are_kept(tmp_path):
    code = "def a():\n    pass\n\n\ndef b():\n    pass"
    book = _html_book(tmp_path, f"<pre><code>{code}</code></pre><p>after</p>")
    for ext in ("rst", "org", "adoc", "tex", "md", "wiki"):
        back, _ = _round(tmp_path, book, ext)
        pres = ["".join(p.itertext()) for h, _ in back.documents() for p in back.tree(h).iter("pre")]
        assert any(code in p for p in pres), (ext, pres)


def test_no_break_spaces_are_kept(tmp_path):
    book = _html_book(tmp_path, "<p>Section 3 and Fig. 2</p>")
    for ext in ("rst", "org", "adoc", "md", "tex", "textile"):
        back, _ = _round(tmp_path, book, ext)
        assert "Section 3" in back.text(), (ext, repr(back.text()))


def test_odd_digits_in_numbers_do_not_stop_a_writer(tmp_path):
    """"²" passes str.isdigit() but int() refuses it: one such attribute made the whole export fail."""
    bodies = ['<ol start="²"><li>a</li></ol>', '<table><tr><td colspan="²">a</td></tr></table>',
              '<p style="font-weight: ²">a</p>', '<ol><li value="²">a</li></ol>',
              '<table><tr><td rowspan="²">a</td></tr></table>']
    for k, body in enumerate(bodies):
        book = _html_book(tmp_path / str(k), body)
        for ext in ("txt", "md", "rst", "docx", "org"):
            book.write(tmp_path / str(k) / f"o.{ext}")


def test_a_roman_list_starting_at_a_huge_number_is_quick(tmp_path):
    book = _html_book(tmp_path, '<ol type="i" start="100000000000000"><li>x</li></ol>')
    _within(10, lambda: book.write(tmp_path / "o.txt"))


# --- styles ---------------------------------------------------------------------------------------

def _styles(body, sheet):
    root = markup.parse(f"<html><head></head><body>{body}</body></html>")
    return root, css.compute_styles(root, sheet)


def test_nth_child_with_a_plus_and_attribute_selectors_with_tilde_apply():
    root, st = _styles('<ul><li>a</li><li>b</li><li class="x y">c</li></ul>',
                       "li:nth-child(2n+1) { font-weight: bold } li[class~=y] { font-style: italic }"
                       " li:nth-child(2n + 0) { color: red }")
    lis = list(root.iter("li"))
    assert [s.get("font-weight") for s in map(st.get, lis)] == ["bold", None, "bold"]
    assert st[lis[2]].get("font-style") == "italic" and st[lis[1]].get("color") == "red"


def test_a_malformed_nth_child_is_ignored_not_fatal(tmp_path):
    root, st = _styles("<p>a</p><p>b</p>", "p:nth-child(an) { color: red } p:nth-child(2n+) { color: blue } p { margin: 0 }")
    assert all(st[p].get("color") is None for p in root.iter("p"))
    book = _html_book(tmp_path, "<p>a</p>", "<style>p:nth-child(an) { color: red }</style>")
    book.write(tmp_path / "o.txt")


def test_selectors_nested_very_deep_are_refused_not_fatal(tmp_path):
    sel = ":not(" * 1200 + "p" + ")" * 1200
    media = "@media screen {" * 1200 + "p{color:red}" + "}" * 1200
    css.parse_stylesheet(sel + "{color:red}")
    css.parse_stylesheet(media)
    book = _html_book(tmp_path, "<p>x</p>", f"<style>{sel}{{color:red}} {media}</style>")
    book.write(tmp_path / "o.txt")


def test_a_long_descendant_selector_on_a_deep_page_is_quick():
    """Each ancestor was tried again for every way of matching the rest: about C(60, 5) tries per element."""
    body = "<div>" * 60 + "<p>x</p>" + "</div>" * 60
    root = markup.parse(f"<html><body>{body}</body></html>")
    t = time.perf_counter()
    st = css.compute_styles(root, "span div div div div div p { color: red } div div div div div p { color: blue }")
    assert time.perf_counter() - t < 2.0
    assert st[next(root.iter("p"))].get("color") == "blue"


def test_sibling_rules_on_a_long_chapter_take_linear_time(tmp_path):
    """p + p and :first-child listed every sibling for every paragraph: 8,000 paragraphs took 3 s, not 0.3."""
    def run(n, sheet):
        root = markup.parse("<html><body>" + "<p>para</p>" * n + "<p class='last'>end</p></body></html>")
        t = time.perf_counter()
        st = css.compute_styles(root, sheet)
        return time.perf_counter() - t, st, root
    plain, _, _ = run(8000, "p { margin: 0 }")
    sibling, st, root = run(8000, "p + p { text-indent: 1em } p:first-child { color: red } p:last-child { color: blue }"
                                  " p:nth-of-type(2) { font-style: italic } p ~ p.last { font-weight: bold }")
    ps = list(root.iter("p"))
    assert st[ps[0]].get("text-indent") is None and st[ps[1]].get("text-indent") == "1em"
    assert st[ps[0]].get("color") == "red" and st[ps[-1]].get("color") == "blue"
    assert st[ps[1]].get("font-style") == "italic" and st[ps[-1]].get("font-weight") == "bold"
    assert sibling < plain * 4 + 0.2, (plain, sibling)


def test_unclosed_css_comments_are_quick():
    t = time.perf_counter()
    css.parse_stylesheet("/* a " * 20000 + "p { color: red }")
    assert time.perf_counter() - t < 1.0


def test_url_with_a_long_run_of_spaces_is_quick():
    t = time.perf_counter()
    assert links.css_urls("url(" + " " * 4000) == []
    assert links.css_urls('a { background: url( "x y.png" ) } b { background: url(z.png) }') == ["x y.png", "z.png"]
    assert time.perf_counter() - t < 1.0


# --- parsing and plain text -------------------------------------------------------------------------

def test_unclosed_cdata_and_doctype_runs_are_quick():
    t = time.perf_counter()
    markup.parse("<p>" + "<![CDATA[ x" * 8000 + "</p>")
    markup.has_internal_subset("<html><body>" + "<!DOCTYPE a" * 10000)
    assert time.perf_counter() - t < 2.0
    assert markup.has_internal_subset('<!DOCTYPE html PUBLIC "a" "b" [ <!ENTITY x "y"> ]><html/>')
    assert markup.text_of(markup.parse("<p>a<![CDATA[<b>]]>c</p>").find("body")) == "a<b>c"


def test_a_lone_surrogate_in_the_text_does_not_fail_the_book(tmp_path):
    src = tmp_path / "a.tex"
    src.write_text('\\documentclass{article}\\begin{document}A \\char"D800 B\\end{document}', encoding="utf-8")
    book = bookport.read(src)
    assert "A" in book.text() and "B" in book.text()
    book.write(tmp_path / "a.epub")


def test_markdown_detection_in_plain_text_is_quick():
    t = time.perf_counter()
    txt.looks_like_markdown("[" * 40000)
    assert time.perf_counter() - t < 0.5


# --- cleaning for EPUB ------------------------------------------------------------------------------

def _epub_pages(path):
    z = zipfile.ZipFile(path)
    return {n.rsplit("/", 1)[-1]: z.read(n).decode("utf-8") for n in z.namelist() if n.endswith(".xhtml")}


def test_a_style_element_in_the_body_keeps_the_text_in_order(tmp_path):
    book = _html_book(tmp_path, "<div>Intro <b>bold</b><style>p{color:red}</style> rest of sentence.</div>")
    back, _ = _round(tmp_path, book, "epub")
    assert "Intro bold rest of sentence." in back.text()


def test_links_to_renamed_ids_on_later_pages_keep_their_anchor(tmp_path):
    """Ids like "fn:1" (Python-Markdown's footnotes) and "1st" are renamed; links from earlier pages lost the anchor."""
    d = tmp_path / "src"
    _page(d, '<h1>One</h1><p><a href="two.html#fn:1">to note</a> <a href="two.html#1st">to 1st</a></p>', name="one.html")
    _page(d, '<h1>Two</h1><p id="1st">first</p><p id="fn:1">the note</p><p><a href="#fn:1">same page</a></p>',
          name="two.html")
    book = bookport.read(d / "one.html")
    book.write(tmp_path / "o.epub")
    pages = _epub_pages(tmp_path / "o.epub")
    assert 'href="two.xhtml#fn_1"' in pages["one.xhtml"] and 'href="two.xhtml#id_1st"' in pages["one.xhtml"]
    assert 'href="#fn_1"' in pages["two.xhtml"] or 'href="two.xhtml#fn_1"' in pages["two.xhtml"]


def test_a_huge_or_odd_colspan_does_not_stop_the_epub(tmp_path):
    book = _html_book(tmp_path, '<table><tr><td colspan="1e999">a</td><td rowspan="99999999">b</td></tr></table>')
    book.write(tmp_path / "o.epub")
    page = next(iter(_epub_pages(tmp_path / "o.epub").values()))
    assert 'colspan="1e999"' not in page and 'rowspan="99999999"' not in page


def test_many_repeated_ids_are_renamed_quickly(tmp_path):
    book = _html_book(tmp_path, '<p id="a">x</p>' * 8000)
    t = time.perf_counter()
    book.write(tmp_path / "o.epub")
    assert time.perf_counter() - t < 2.5


# --- HTML -------------------------------------------------------------------------------------------

def test_a_page_with_thousands_of_links_to_one_page_is_read_quickly(tmp_path):
    d = tmp_path / "src"
    _page(d, "<p>b</p>", name="b.html")
    p = _page(d, '<a href="b.html">b</a>' * 4000, name="a.html")
    t = time.perf_counter()
    book = bookport.read(p)
    assert time.perf_counter() - t < 1.5
    assert len(list(book.documents())) == 2


def test_a_charset_line_does_not_drop_the_first_rule():
    """'@charset "utf-8"; p {...}': what followed the @charset's ";" was taken for its prelude and dropped."""
    rules = css.parse_stylesheet('@charset "utf-8";\np { color: red }\n@import url(a.css); h1 { color: blue }')
    assert [r.decls["color"][0] for r in rules] == ["red", "blue"]


def test_links_to_a_repeated_id_stay_with_its_first_use(tmp_path):
    book = _html_book(tmp_path, '<p id="a">first</p><p id="a">second</p><p><a href="#a">to a</a></p>')
    book.write(tmp_path / "o.epub")
    page = _epub_pages(tmp_path / "o.epub")["index.xhtml"]
    assert '#a">to a' in page and 'id="a"' in page and 'id="a_1"' in page  # not #a_1, the repeat


# --- the markup readers: hostile input ---------------------------------------------------------------

def _read_text(tmp_path, ext, text, **files):
    d = tmp_path / f"in_{ext}"
    d.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (d / name).write_text(data, encoding="utf-8")
    src = d / f"doc.{ext}"
    src.write_text(text, encoding="utf-8")
    return bookport.read(src)


def _quick(seconds, fn):
    t = time.perf_counter()
    out = _within(max(seconds * 4, 10), fn)
    assert time.perf_counter() - t < seconds
    return out


def test_textile_runs_of_marks_are_quick(tmp_path):
    """"(" * 50 after a block signature, or "<>" * 26 in a cell, took more than 20 s: overlapping choices."""
    _quick(2, lambda: _read_text(tmp_path, "textile", "p" + "(" * 60 + "x. text\n\n*" + "(" * 60 + "x item\n\n|"
                                 + "<>" * 40 + "x|\n"))
    book = _read_text(tmp_path, "textile", "p(((. padded\n\n|<>. justified|\n")
    assert "padded" in book.text() and "justified" in book.text()


def test_mediawiki_long_runs_in_links_pictures_and_headings_are_quick(tmp_path):
    _quick(2, lambda: _read_text(tmp_path, "wiki", "[http://a.com" + " " * 50000 + "\n\n" + "[http://a.com " * 5000 + "\n\n"
                                 "[[File:a.png|" + "1" * 20000 + "]]\n"))
    book = _quick(2, lambda: _read_text(tmp_path, "wiki", "== A ==\ntext\n" * 10000))
    ids = [e.get("id") for h, _ in book.documents() for e in book.tree(h).iter("h2")]
    assert len(set(ids)) == len(ids) == 10000
    book = _read_text(tmp_path, "wiki", "[http://a.com   the  label ] and [[File:a.png|200x100px|cap]]")
    assert "the  label" in book.text() or "the label" in book.text()


def test_roff_strings_and_macros_that_multiply_are_bounded(tmp_path):
    lines = [".TH X 1", ".ds a " + "x" * 10]
    for k, name in enumerate("bcdefghij"):
        lines.append(f".ds {name} " + f"\\*{'abcdefghij'[k]}" * 10)
    lines += [".SH NAME", "\\*j"]
    book = _quick(5, lambda: _read_text(tmp_path, "man", "\n".join(lines) + "\n"))
    assert len(book.text()) < 20_000_000
    book = _read_text(tmp_path, "man", ".TH X 1\n.ds a \\*a\n.ds b hello\n.ds b \\*b world\n.SH NAME\n\\*a \\*b\n")
    assert "hello world" in book.text()
    long = "y" * 10000
    macro = [".TH X 1", ".de m1", long, "..", ".de m2"] + [".m1"] * 1000 + ["..", ".de m3"] + [".m2"] * 300 + ["..", ".m3"]
    book = _quick(10, lambda: _read_text(tmp_path, "man", "\n".join(macro) + "\n"))
    assert len(book.text()) < 40_000_000


def test_latex_environments_that_end_themselves_stop(tmp_path):
    book = _quick(10, lambda: _read_text(tmp_path, "tex", "\\newenvironment{foo}{}{\\end{foo}}\\begin{foo}x\\end{foo} after"
                                         "\n\n\\newenvironment{p}{}{\\end{q}}\\newenvironment{q}{}{\\end{p}}"
                                         "\\begin{p}y\\end{p} end"))
    assert "after" in book.text() and any("expanded too many" in s for s in book.lost)


def test_latex_doubling_macro_and_huge_column_specs_are_quick(tmp_path):
    _quick(8, lambda: _read_text(tmp_path, "tex", "\\def\\a#1{\\a{#1#1}}\\a{x} after"))
    book = _quick(3, lambda: _read_text(tmp_path, "tex", "\\begin{tabular}{*{100}{*{100}{*{100}{*{100}{c}}}}}a & b\\\\"
                                        "\\end{tabular}"))
    assert "a" in book.text()
    from bookport.fmt import latex
    assert len(latex._colspec("*{100}{*{100}{*{100}{c}}}")) <= 1000  # 0.3.0: a list of a million columns


def test_latex_hostile_options_labels_and_ids_are_quick(tmp_path):
    _quick(2, lambda: _read_text(tmp_path, "tex", "\\begin{minted}" + " " * 80000 + "\n"))
    _quick(2, lambda: _read_text(tmp_path, "tex", "\\begin{thebibliography}{9}\\bibitem[" + "(" * 40000
                                 + "]{k} x\\end{thebibliography}"))
    _quick(4, lambda: _read_text(tmp_path, "tex", "\\hypertarget{a}{}" * 8000))


def test_latex_odd_numbers_in_options_do_not_fail_the_book(tmp_path):
    (tmp_path / "in_tex").mkdir(exist_ok=True)
    (tmp_path / "in_tex" / "a.png").write_bytes(png())
    book = _read_text(tmp_path, "tex", "\\includegraphics[width=.\\textwidth]{a.png} \\includegraphics[width=1.2.3\\textwidth]{a.png}"
                      "\\begin{tabular}{ll}\\multirow{1e400}{*}{x} & y\\end{tabular} kept")
    assert "kept" in book.text()


def test_latex_label_inside_a_section_title_names_that_section(tmp_path):
    book = _read_text(tmp_path, "tex", "\\documentclass{article}\\begin{document}\\section{Intro}x\\section{Title\\label{x}} "
                                       "See \\ref{x}.\\end{document}")
    assert "See 2." in " ".join(book.text().split())


def test_latex_writer_leaves_ordinary_divs_alone_and_keeps_unnumbered_theorems(tmp_path):
    book = _html_book(tmp_path, '<div class="calibre1"><p><em>Hope</em> is the thing with feathers.</p></div>'
                                '<div class="center"><p><strong>Note</strong> centred.</p></div>')
    out = tmp_path / "o.tex"
    book.write(out)
    tex = out.read_text(encoding="utf-8")
    assert "newtheorem" not in tex and "\\begin{calibre" not in tex and "\\begin{center}\\label" not in tex
    src = tmp_path / "t.tex"
    src.write_text("\\documentclass{article}\\usepackage{amsthm}\\newtheorem*{rem}{Remark}\\newtheorem{thm}{Theorem}"
                   "\\begin{document}\\begin{rem}Plain.\\end{rem}\\begin{thm}Numbered.\\end{thm}\\end{document}",
                   encoding="utf-8")
    back, _ = _round(tmp_path, bookport.read(src), "tex")
    text = " ".join(back.text().split())
    assert "Remark." in text and "Remark 1" not in text and "Theorem 1" in text


def test_rst_substitutions_and_includes_that_multiply_are_bounded(tmp_path):
    defs = [".. |s0| replace:: " + "x" * 20]
    for k in range(1, 6):
        defs.append(f".. |s{k}| replace:: " + f"|s{k - 1}| " * 20)
    book = _quick(10, lambda: _read_text(tmp_path, "rst", "Text |s5| end\n\n" + "\n".join(defs) + "\n"))
    assert len(book.text()) < 5_000_000 and "end" in book.text()
    files = {f"f{k}.rst": "".join(f".. include:: f{k + 1}.rst\n\n" for _ in range(4)) for k in range(8)}
    files["f8.rst"] = "leaf\n"
    book = _quick(10, lambda: _read_text(tmp_path, "rst", ".. include:: f0.rst\n", **files))
    assert any("more files than bookport reads" in s for s in book.lost)


def test_org_macros_and_includes_that_multiply_are_bounded(tmp_path):
    macros = ["#+MACRO: m0 " + "x" * 60] + [f"#+MACRO: m{k} " + f"{{{{{{m{k - 1}}}}}}}" * 10 for k in range(1, 8)]
    book = _quick(10, lambda: _read_text(tmp_path, "org", "\n".join(macros) + "\n\nText {{{m7}}} end\n"))
    assert len(book.text()) < 10_000_000
    files = {f"f{k}.org": "".join(f"#+INCLUDE: \"f{k + 1}.org\"\n" for _ in range(6)) for k in range(6)}
    files["f6.org"] = "leaf\n"
    book = _quick(10, lambda: _read_text(tmp_path, "org", "#+INCLUDE: \"f0.org\"\n", **files))
    assert any("more files than bookport reads" in s for s in book.lost)


def test_org_and_asciidoc_titles_with_long_space_runs_are_quick(tmp_path):
    _quick(1.5, lambda: _read_text(tmp_path, "org", "* a" + " " * 40000 + "b :t:\n- x" + " " * 40000 + "y :: z\n"))
    _quick(2, lambda: _read_text(tmp_path, "adoc", "== a" + " " * 20000 + "b\n"))
    book = _read_text(tmp_path, "org", "* Title   :tag1:tag2:\ntext\n")
    assert "Title" in book.text() and "Title   :tag1" not in book.text()
    book = _read_text(tmp_path, "adoc", "= Doc\n\n== Section Two ==\n\ntext\n")
    assert "Section Two" in book.text() and "==" not in book.text()


def test_asciidoc_attributes_and_table_repeats_that_multiply_are_bounded(tmp_path):
    lines = [":a0: " + "x" * 10] + [f":a{k}: " + f"{{a{k - 1}}}" * 10 for k in range(1, 10)]
    book = _quick(5, lambda: _read_text(tmp_path, "adoc", "\n".join(lines) + "\n\nText {a9} end\n"))
    assert len(book.text()) < 2_000_000
    book = _quick(5, lambda: _read_text(tmp_path, "adoc", "|===\n1000*.500+|x\n|===\n"))
    rows = sum(1 for h, _ in book.documents() for _ in book.tree(h).iter("tr"))
    assert rows <= 10_500


def test_asciidoc_odd_numbers_do_not_fail_the_book(tmp_path):
    book = _read_text(tmp_path, "adoc", "include::part.adoc[lines=1..x]\n\nkept\n", **{"part.adoc": "a\nb\n"})
    assert "kept" in book.text()
    book = _read_text(tmp_path, "adoc", ":c: {counter:n:\u00b2}\n\nkept {c}\n")
    assert "kept" in book.text()


# --- the markup readers and writers: what a round trip changed ---------------------------------------

def test_org_emphasis_like_text_and_headline_words_survive_a_round_trip(tmp_path):
    book = _html_book(tmp_path, "<p>rated *5* stars, edit /etc/hosts and /var/log/ now, a =b= c</p>"
                                "<h1>COMMENT on things</h1><p>kept</p><h1>TODO list</h1><h1>Ratio :a:b:</h1>"
                                "<h1>[#A] grade</h1>")
    back, _ = _round(tmp_path, book, "org")
    text = back.text().replace("\u200b", "")
    for s in ("rated *5* stars", "/etc/hosts and /var/log/ now", "a =b= c", "COMMENT on things", "kept", "TODO list",
              "Ratio :a:b:", "[#A] grade"):
        assert s in text, s
    assert "<strong>" not in _body_html(back) and "<em>" not in _body_html(back)


def test_org_drawer_without_end_keeps_the_headlines_after_it(tmp_path):
    book = _read_text(tmp_path, "org", "* H\n:PROPERTIES:\n:ID: x\n* Next\ntext after\n")
    assert "Next" in book.text() and "text after" in book.text()


def test_asciidoc_code_spans_and_escaped_attributes_survive_a_round_trip(tmp_path):
    book = _html_book(tmp_path, "<p><code>a_b(x[i])</code> <code>x-&gt;y</code> <code>{x}</code> "
                                "<code>*not bold*</code></p><p>The {sp} and {doctitle} words</p>")
    back, _ = _round(tmp_path, book, "adoc")
    codes = [markup.text_of(c) for h, _ in back.documents() for c in back.tree(h).iter("code")]
    assert codes == ["a_b(x[i])", "x->y", "{x}", "*not bold*"]
    assert "The {sp} and {doctitle} words" in back.text()
    book = _read_text(tmp_path, "adoc", ":author: Jane\n\nThe \\{author} field, `+{author}+` and `+lit*eral+`\n")
    assert "The {author} field" in book.text() and "lit*eral" in book.text()


def test_asciidoc_section_title_is_read_once(tmp_path):
    book = _read_text(tmp_path, "adoc", "= D\n\n== Ch {counter:c}\n\n== Ch {counter:c}\n\n== Ch {counter:c}\n\n"
                                        "== Title footnote:[note]\n\ntext\n")
    heads = [" ".join(markup.text_of(e).split()) for h, _ in book.documents() for e in book.tree(h).iter()
             if e.tag in ("h1", "h2")]
    assert [x for x in heads if x.startswith("Ch")] == ["Ch 1", "Ch 2", "Ch 3"]
    asides = [e for h, _ in book.documents() for e in book.tree(h).iter("aside")]
    assert len(asides) == 1



def test_markdown_reference_links_reach_every_chapter(tmp_path):
    chapter = "\n\n".join(["Some text for this chapter. " * 20] * 120)  # past the size a chapter is split at
    text = f"# One\n\n[the site][s] here\n\n{chapter}\n\n# Two\n\n[the site][s] again\n\n{chapter}\n\n[s]: https://example.com\n"
    book = _read_text(tmp_path, "md", text)
    assert len(list(book.documents())) >= 2
    links_ = [a.get("href") for h, _ in book.documents() for a in book.tree(h).iter("a")]
    assert links_.count("https://example.com") == 2 and "[the site][s]" not in book.text()


def test_markdown_hard_line_breaks_are_read_and_written(tmp_path):
    book = _read_text(tmp_path, "md", "line one  \nline two\\\nline three\n")
    assert sum(1 for h, _ in book.documents() for _ in book.tree(h).iter("br")) == 2
    html_book = _html_book(tmp_path, "<p>a<br/>b</p>")
    back, _ = _round(tmp_path, html_book, "md")
    assert sum(1 for h, _ in back.documents() for _ in back.tree(h).iter("br")) == 1


def test_markdown_long_lines_of_marks_are_quick(tmp_path):
    n = 20000
    _quick(3, lambda: _read_text(tmp_path, "md", "# a" + " " * n + "b\n\n```" + "a" * n + "`\n\n``` " + " " * n + "`\n\n"
                                 "a|b\n" + " " * n + "x\n\n:::foo" + " " * (2 * n) + "x\n\nend" + " " * n + "x\n"))
    book = _read_text(tmp_path, "md", "## Title ##\n\n```python\ncode\n```\n\n| a | b |\n|:--|--:|\n| 1 | 2 |\n")
    assert "Title" in book.text() and "##" not in book.text()
    assert sum(1 for h, _ in book.documents() for _ in book.tree(h).iter("table")) == 1


def test_docbook_question_groups_are_kept(tmp_path):
    src = ('<article xmlns="http://docbook.org/ns/docbook" version="5.0"><title>T</title><qandaset><qandadiv>'
           "<title>Group</title><qandaentry><question><para>Why?</para></question><answer><para>Because.</para>"
           "</answer></qandaentry></qandadiv></qandaset></article>")
    book = _read_text(tmp_path, "dbk", src)
    text = book.text()
    assert "Group" in text and "Why?" in text and "Because." in text


def test_docbook_writer_writes_the_text_after_an_equation_once(tmp_path):
    src = tmp_path / "m.tex"
    src.write_text("\\documentclass{article}\\begin{document}a $x$ TAIL words\\end{document}", encoding="utf-8")
    out = tmp_path / "o.dbk"
    bookport.read(src).write(out)
    assert out.read_text(encoding="utf-8").count("TAIL words") == 1


def test_rst_literal_blocks_and_quotes_inside_list_items(tmp_path):
    book = _read_text(tmp_path, "rst", "- Install::\n\n      pip install x\n\n- a\n\n    quoted\n\n1. Run this::\n\n       make all\n")
    pres = [markup.text_of(p) for h, _ in book.documents() for p in book.tree(h).iter("pre")]
    assert pres == ["pip install x", "make all"]
    assert sum(1 for h, _ in book.documents() for _ in book.tree(h).iter("blockquote")) == 1


def test_rst_long_runs_of_role_and_reference_marks_are_quick(tmp_path):
    _quick(3, lambda: _read_text(tmp_path, "rst", ":a" * 10000 + "\n\n" + "a-" * 10000 + "\n\n" + "a." * 10000 + "\n\n"
                                 + ":a" * 5000 + ":\n"))
    book = _read_text(tmp_path, "rst", "See :py:class:`Foo` and some-ref_ here.\n\n.. _some-ref: https://example.com\n")
    assert [a.get("href") for h, _ in book.documents() for a in book.tree(h).iter("a")] == ["https://example.com"]


def test_rst_odd_numbers_in_options_and_labels_do_not_fail_the_book(tmp_path):
    texts = [".. [\u00b2] note\n\nkept\n", ".. contents::\n   :depth: \u00b2\n\nkept\n", ":pep:`\u00b2`\n\nkept\n",
             ".. csv-table::\n   :header: a, b\n   :header-rows: x\n\n   1, 2\n\nkept\n",
             ".. |d| date:: %Q\n\nkept |d|\n", ".. literalinclude:: part.txt\n   :start-line: --5\n   :lines: \u00b2\n\nkept\n"]
    for k, text in enumerate(texts):
        book = _read_text(tmp_path / str(k), "rst", text, **{"part.txt": "a\nb\n"})
        assert "kept" in book.text(), text


def test_bibliography_journal_and_volume_have_a_space_between(tmp_path):
    from bookport import bib
    e = bib.parse("@article{k, author={Doe, Jane}, title={T}, journal={Nature}, volume={5}, number={2}, pages={1--9},"
                  " year={2020}}")["k"]
    text = "".join(t for t, _kind in bib.render(e))
    assert "Nature 5 (2): 1–9." in text, text
    e = bib.parse("@article{k, author={Doe, Jane}, title={T}, journal={Nature}, pages={1--9}, year={2020}}")["k"]
    assert "Nature: 1–9." in "".join(t for t, _kind in bib.render(e))


def test_bibliography_strings_that_double_are_bounded():
    from bookport import bib
    text = '@string{a = "xy"}\n' + "@string{a = a # a}\n" * 40 + "@book{k, title = a # a # a, year = 2000}\n"
    t = time.perf_counter()
    e = bib.parse(text)["k"]
    assert time.perf_counter() - t < 2 and len(e.fields["title"]) <= 65_536


def test_typst_dollar_inside_a_math_string_does_not_end_the_math(tmp_path):
    book = _read_text(tmp_path, "typ", 'A $"costs $5"$ b.\n\nNext paragraph *bold*.\n')
    body = _body_html(book)
    assert "<strong>bold</strong>" in body and body.count("<math") == 1


def test_typst_lorem_is_counted_against_the_size_limit(tmp_path):
    book = _quick(5, lambda: _read_text(tmp_path, "typ", "#lorem(100000)\n" * 40))
    assert len(book.text()) < 20_000_000


def test_typst_inline_raw_with_a_language_at_a_line_start_keeps_the_rest(tmp_path):
    book = _read_text(tmp_path, "typ", "```py print(1)``` and `x` after\n")
    assert "and" in book.text() and "after" in book.text() and "print(1)" in book.text()


def test_tex_text_keeps_escaped_braces_and_backslashes():
    from bookport import mathml
    assert [e.text for e in mathml.from_tex(r"\text{\{x\}}").iter("mtext")] == ["{x}"]
    assert [e.text for e in mathml.from_tex(r"\text{a\textbackslash{}b}").iter("mtext")] == ["a\\b"]


def test_latex_dashes_backticks_and_url_text_survive_a_round_trip(tmp_path):
    book = _html_book(tmp_path, "<p>a---b g----h `tick` ?` !`</p><p><a href='http://y.org/%7E'>http://y.org/%7E</a></p>")
    back, _out = _round(tmp_path, book, "tex")
    text = back.text()
    assert "a---b" in text and "g----h" in text and "`tick`" in text and "?`" in text, text
    src = tmp_path / "u.tex"
    src.write_text("\\documentclass{article}\\usepackage{hyperref}\\begin{document}\\url{http://y.org/\\%7E}\\end{document}",
                   encoding="utf-8")
    assert "http://y.org/%7E" in bookport.read(src).text()


def test_latex_biblatex_multi_citations_keep_every_key(tmp_path):
    book = _read_text(tmp_path, "tex", "\\documentclass{article}\\begin{document}\\cites{a}{b} next "
                                       "\\parencites[see][12]{c}[3]{d} end\\end{document}")
    cites = [s.get("data-cites") for h, _ in book.documents() for s in book.tree(h).iter("span")
             if "citation" in s.get("class", "")]
    assert cites == ["a b", "c d"] and "b next" not in book.text() and "next" in book.text()


def test_man_table_cell_spans_keep_the_cell_after_them(tmp_path):
    book = _html_book(tmp_path, '<table><tr><td colspan="2">A</td><td>B</td></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>')
    back, _ = _round(tmp_path, book, "man")
    assert "B" in back.text() and "A" in back.text()


def test_mediawiki_link_targets_survive_a_round_trip(tmp_path):
    book = _html_book(tmp_path, "<p><a href='#d'>to d</a></p><h2 id='c'>Two</h2><p id='d'>Para d</p>")
    back, _ = _round(tmp_path, book, "wiki")
    ids = {e.get("id") for h, _ in back.documents() for e in back.tree(h).iter() if e.get("id")}
    hrefs = [a.get("href") for h, _ in back.documents() for a in back.tree(h).iter("a")]
    assert any(h and h.lstrip("#").split("#")[-1] in ids for h in hrefs), (hrefs, ids)


def test_markdown_writer_keeps_spaces_at_emphasis_edges_and_anchors_off_blocks(tmp_path):
    book = _html_book(tmp_path, "<p>word<em> italic</em> and <b>bold </b>after</p><p><a href='#L'>list</a> "
                                "<a href='#C'>code</a></p><ul id='L'><li>one</li><li>two</li></ul>"
                                "<pre id='C'>code here</pre><p>end</p>")
    back, _ = _round(tmp_path, book, "md")
    body = _body_html(back)
    assert "word <em>italic</em> and <strong>bold</strong> after" in " ".join(body.split())
    assert sum(1 for h, _ in back.documents() for _ in back.tree(h).iter("li")) == 2
    assert [markup.text_of(p) for h, _ in back.documents() for p in back.tree(h).iter("pre")] == ["code here"]
    assert "end" in back.text()


def test_odd_digits_do_not_fail_the_other_markup_readers(tmp_path):
    cases = {"wiki": '{|\n| colspan="\u00b2" | a\n|}\n\nkept\n',
             "dbk": '<article xmlns="http://docbook.org/ns/docbook" version="5.0"><informaltable><tgroup cols="1">'
                    '<colspec colnum="\u00b2"/><tbody><row><entry morerows="\u00b2">a</entry></row></tbody></tgroup>'
                    "</informaltable><para>kept</para></article>",
             "man": ".TH X 1\n.SH A\n\\N'\u00b2' kept\n",
             "org": "#+OPTIONS: H:\u00b2 num:\u00b2\n#+TOC: headlines \u00b2\n* A\nkept\n",
             "typ": "#heading(level: \u00b2)[A]\n\nkept\n"}
    for ext, text in cases.items():
        assert "kept" in _read_text(tmp_path, ext, text).text(), ext


def test_odd_and_huge_digits_do_not_fail_the_office_readers_or_the_cleaner(tmp_path):
    """"²" passes str.isdigit() and 5,000 digits pass it too, but int() refuses both: one such attribute made the
    whole book unreadable (ODT, DOCX, DocBook, MediaWiki) or the EPUB unwritable (an HTML table's border)."""
    big = "9" * 5000
    ns = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"')
    for k, bad in enumerate(("²", big)):
        for name, body in (("s", f'<text:p>a<text:s text:c="{bad}"/>kept</text:p>'),
                           ("h", f'<text:h text:outline-level="{bad}">kept</text:h>'),
                           ("c", (f'<table:table><table:table-row><table:table-cell table:number-columns-spanned="{bad}">'
                                  "<text:p>kept</text:p></table:table-cell></table:table-row></table:table>"))):
            p = tmp_path / f"{name}{k}.odt"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
                z.writestr("content.xml", f'<?xml version="1.0"?><office:document-content {ns}><office:body>'
                                          f"<office:text>{body}</office:text></office:body></office:document-content>")
            assert "kept" in bookport.read(p).text(), (name, k)
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        for name, body in (("o", f'<w:p><w:pPr><w:outlineLvl w:val="{bad}"/></w:pPr><w:r><w:t>kept</w:t></w:r></w:p>'),
                           ("i", f'<w:p><w:pPr><w:ind w:left="{bad}" w:firstLine="{bad}"/></w:pPr><w:r><w:t>kept</w:t></w:r></w:p>'),
                           ("g", (f'<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="{bad}"/></w:tcPr><w:p><w:r><w:t>kept'
                                  "</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"))):
            p = tmp_path / f"{name}{k}.docx"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                           'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/>'
                           '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                           '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                           'wordprocessingml.document.main+xml"/></Types>')
                z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                           'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                           'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
                z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
            assert "kept" in bookport.read(p).text(), (name, k)
        wiki = _read_text(tmp_path, "wiki", f'<ol start="{bad}"><li>a</li></ol>\n{{|\n| colspan="{bad}" | a\n|}}\n\nkept\n')
        assert "kept" in wiki.text(), ("wiki", k)
        dbk = _read_text(tmp_path, "dbk", '<article xmlns="http://docbook.org/ns/docbook" version="5.0"><informaltable>'
                         f'<tgroup cols="1"><colspec colnum="{bad}"/><tbody><row><entry morerows="{bad}">a</entry></row>'
                         "</tbody></tgroup></informaltable><para>kept</para></article>")
        assert "kept" in dbk.text(), ("dbk", k)
        html = _page(tmp_path / f"html{k}", f'<table border="{bad}"><tr><td>kept</td></tr></table>')
        out = bookport.convert(html, tmp_path / f"html{k}.epub")
        assert "kept" in bookport.read(out).text(), ("html", k)


def test_huge_digits_do_not_fail_org_and_typst(tmp_path):
    big = "9" * 5000
    org = f"#+OPTIONS: H:{big} num:{big}\n#+TOC: headlines {big}\n* A\n1. [@{big}] item\n\nkept\n"
    typ = f"#heading(level: {big})[A]\n\n#table(columns: {big})[x]\n\nkept\n"
    for ext, text in (("org", org), ("typ", typ)):
        assert "kept" in _read_text(tmp_path, ext, text).text(), ext
