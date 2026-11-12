"""LaTeX: reading (tokens, macros, includes, structure, numbers, math, citations) and writing."""

import re
import time
import xml.etree.ElementTree as ET

import pytest

import bookport
from bookport import markup
from bookport.fmt import latex
from bookport.images import Raster, encode_png


def _read(tmp_path, text: str, name: str = "doc.tex", **files) -> bookport.Book:
    d = tmp_path / "paper"
    d.mkdir(exist_ok=True)
    for fname, data in files.items():
        p = d / fname.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data, encoding="utf-8")
    src = d / name
    src.write_text(text, encoding="utf-8")
    return bookport.read(src, "tex")


def _body(book) -> ET.Element:
    root = ET.Element("all")
    for href, _ in book.documents():
        root.extend(list(book.tree(href).find("body")))
    return root


def _text(el) -> str:
    """Text with runs of ordinary white space as one space (no-break spaces kept)."""
    return re.sub(r"[ \t\r\n]+", " ", "".join(t for t in _texts(el))).strip()


def _texts(el):
    if el.tag == "annotation":
        return
    yield el.text or ""
    for c in el:
        yield from _texts(c)
        yield c.tail or ""


def _doc(body: str, preamble: str = "") -> str:
    return "\\documentclass{article}\n" + preamble + "\n\\begin{document}\n" + body + "\n\\end{document}\n"


PNG = encode_png(Raster(2, 2, "RGB", bytes(12)))


# --- tokens and text -----------------------------------------------------------------------------------

def test_paragraphs_comments_quotes_dashes_and_accents(tmp_path):
    book = _read(tmp_path, _doc(
        "One line\nsame paragraph. % a comment\nStill%\nglued here.\n\n"
        "``Quoted'' -- en, --- em, `single'. Caf\\'e, na\\\"{\\i}ve, \\c{c}a, {\\L}ukasz, \\v Cech~x.\n"
        "%only a comment\n\nThird, 100\\% \\& \\$5 \\#1 a\\_b \\{x\\} \\textbackslash{} \\ldots"))
    ps = _body(book).findall("p")
    assert [_text(p) for p in ps] == [
        "One line same paragraph. Stillglued here.",
        "“Quoted” – en, — em, ‘single’. Café, naïve, ça, Łukasz, Čech\u00a0x.",
        "Third, 100% & $5 #1 a_b {x} \\ …"]


def test_verbatim_verb_listings_and_comment_environment(tmp_path):
    book = _read(tmp_path, _doc(
        "Use \\verb|a%b{}| and \\verb+x\\y+ and \\lstinline[language=C]{int x;}.\n"
        "\\begin{verbatim}\n  if (a) { b%c }\n\\end{verbatim}\n"
        "\\begin{lstlisting}[language=Python]\nprint('%')\n\\end{lstlisting}\n"
        "\\begin{minted}{rust}\nfn main() {}\n\\end{minted}\n"
        "\\begin{comment}\nhidden \\section{No}\n\\end{comment}\nAfter."))
    body = _body(book)
    assert [c.text for c in body.iter("code") if c.text][:3] == ["a%b{}", "x\\y", "int x;"]
    pres = body.findall("pre")
    assert [markup.text_of(p) for p in pres] == ["  if (a) { b%c }", "print('%')", "fn main() {}"]
    assert pres[1].find("code").get("class") == "language-python" and pres[2].find("code").get("class") == "language-rust"
    assert body.find("h1") is None and "hidden" not in _text(body) and _text(body).endswith("After.")


# --- macros ---------------------------------------------------------------------------------------------

def test_newcommand_def_optional_arguments_and_operators(tmp_path):
    book = _read(tmp_path, _doc(
        "\\name{} met \\greet{Bob}, \\greet[Hi]{Ann}. \\twice{ab} $\\R \\argmin_x \\norm{v}$ \\other.",
        "\\newcommand{\\name}{Alice}\n\\newcommand\\greet[2][Hello]{#1, #2!}\n\\def\\twice#1{#1#1}\n"
        "\\providecommand{\\name}{Not used}\n\\newcommand*{\\R}{\\mathbb{R}}\n\\DeclareMathOperator*{\\argmin}{arg\\,min}\n"
        "\\newcommand{\\norm}[1]{\\lVert #1\\rVert}\n\\let\\other\\name\n\\renewcommand{\\section}{broken}"))
    p = _body(book).find("p")
    assert _text(p).startswith("Alice met Hello, Bob!, Hi, Ann!. abab")
    tex = p.find(".//annotation").text
    assert "\\mathbb{R}" in tex and "\\operatorname*{arg\\,min}" in tex and "\\lVert v\\rVert" in tex
    assert _text(p).endswith("Alice.")


def test_newenvironment_and_conditionals(tmp_path):
    book = _read(tmp_path, _doc(
        "\\begin{note}[Tip]careful\\end{note}\n\\iffalse hidden \\fi shown \\ifdraft draft\\else final\\fi.",
        "\\newenvironment{note}[1][Note]{\\textbf{#1:} }{ (end)}\n\\newif\\ifdraft"))
    assert _text(_body(book)) == "Tip: careful (end) shown final."


def test_macro_bombs_are_capped(tmp_path):
    t = time.perf_counter()
    book = _read(tmp_path, _doc("\\a x \\b y", "\\def\\a{\\a\\a}\n\\def\\b{bbbbbbbbbb\\b\\b}"))
    assert time.perf_counter() - t < 20
    assert any("expanded too many times" in line for line in book.lost)
    assert "y" in _text(_body(book))


# --- includes -------------------------------------------------------------------------------------------

def test_input_include_subfile_and_loops(tmp_path):
    book = _read(tmp_path, _doc(
        "\\input{sec/intro}\n\\include{loop}\n\\subfile{sub.tex}\n\\input macros\n\\mymacro"),
        sec__intro="\\section{Intro}\nFrom an include.", loop="Loop text \\input{loop}",
        **{"sub.tex": "\\documentclass[doc.tex]{subfiles}\n\\begin{document}\nSub body.\n\\end{document}",
           "macros.tex": "\\newcommand{\\mymacro}{Defined in a file.}"})
    body = _body(book)
    assert _text(body.find("h1")) == "1 Intro"
    assert "From an include." in _text(body) and "Sub body." in _text(body) and "Defined in a file." in _text(body)
    assert _text(body).count("Loop text") == 1
    assert any("include loop" in line for line in book.lost)


def test_includes_outside_the_folder_are_refused(tmp_path):
    secret = tmp_path / "secret.tex"
    secret.write_text("TOP SECRET", encoding="utf-8")
    (tmp_path / "up.tex").write_text("one level up is fine", encoding="utf-8")
    book = _read(tmp_path, _doc(
        "\\input{../../secret}\\input{" + str(secret).replace("\\", "/") + "}\\input{file:///etc/passwd}"
        "\\include{C:/Windows/win}\\input{../up}\\includegraphics{../../secret.png}\\lstinputlisting{/etc/passwd}"))
    text = _text(_body(book))
    assert "TOP SECRET" not in text and "one level up is fine" in text
    assert sum("outside the document's folder" in line for line in book.lost) >= 4


# --- structure and numbers ------------------------------------------------------------------------------

def test_title_block_abstract_sections_and_numbers(tmp_path):
    book = _read(tmp_path, _doc(
        "\\maketitle\n\\begin{abstract}We study.\\end{abstract}\n"
        "\\section{Intro}\\label{sec:intro}\nSee \\ref{sec:m}, \\autoref{sec:m}, \\cref{sub} and \\nameref{sec:m}.\n"
        "\\section[short]{Method}\\label{sec:m}\\subsection{Part}\\label{sub}\\subsubsection{Deep}\\paragraph{Para}\n"
        "\\section*{Thanks}\\appendix\\section{Extra}\\label{app}See \\ref{app}.",
        "\\title{A Study\\thanks{Funded.}}\\author{Ann One\\\\ Uni \\and Bo Two}\\date{May 2020}"))
    body = _body(book)
    header = body.find("header")
    assert _text(header.find("h1")) == "A Study1" and header.find("h1").get("class") == "title"
    assert [p.get("class") for p in header.findall("p")] == ["author", "author", "date"]
    assert book.metadata.title == "A Study" and book.metadata.authors == ["Ann One", "Bo Two"]
    assert book.metadata.date == "May 2020" and book.metadata.description == "We study."
    assert _text(body.find("div[@class='abstract']/p")) == "We study."
    heads = [(h.tag, _text(h)) for h in body.iter() if re.match(r"h\d$", h.tag) and h.get("class") != "title"]
    assert heads == [("h1", "1 Intro"), ("h1", "2 Method"), ("h2", "2.1 Part"), ("h3", "2.1.1 Deep"),
                     ("h4", "Para"), ("h1", "Thanks"), ("h1", "A Extra")]
    refs = [a for a in body.iter("a") if a.get("class") == "ref"]
    assert [_text(a) for a in refs] == ["2", "Section\u00a02", "section\u00a02.1", "Method", "A"]
    assert refs[0].get("href") == "#sec-m" and body.find(".//*[@id='sec-m']").tag == "h1"


def test_equations_are_numbered_like_latex(tmp_path):
    book = _read(tmp_path, _doc(
        "Text $a^2$ and \\(b\\).\n\\begin{equation}\\label{e:one} E=mc^2 \\end{equation}\n"
        "\\begin{align} x &= 1 \\label{e:two}\\\\ y &= 2 \\nonumber \\\\ z &= 3 \\label{e:three} \\end{align}\n"
        "\\begin{equation*} \\text{none} \\end{equation*} $$d$$ \\[e\\]\n"
        "\\begin{gather} p \\tag{$*$}\\label{e:star} \\end{gather}\n"
        "\\begin{eqnarray} a &=& b \\end{eqnarray}\n"
        "See \\eqref{e:one}, \\ref{e:two}, \\eqref{e:three}, \\eqref{e:star}, \\eqref{nope}."))
    body = _body(book)
    inline = [m for m in body.iter("math") if m.get("display") == "inline"]
    assert len(inline) == 2
    eqs = body.findall("div[@class='equation']")
    assert len(eqs) == 7
    nums = [" ".join(m.text for m in e.iter("mtext") if m.get("class") == "eqno") for e in eqs]
    assert nums == ["(1)", "(2) (3)", "", "", "", "($*$)", "(4)"]
    refs = [_text(a) for a in body.iter() if a.get("class") == "ref"]
    assert refs == ["(1)", "2", "(3)", "($*$)", "??"]
    assert eqs[0].get("data-latex").startswith("\\begin{equation}\\label{e:one}")
    assert latex.mathml.annotation(eqs[1].find("math")).startswith("\\begin{aligned}")


def test_lists_items_and_labels(tmp_path):
    book = _read(tmp_path, _doc(
        "\\begin{itemize}\\item one\\item[$\\star$] two\n\\begin{itemize}\\item nested\\end{itemize}\\end{itemize}\n"
        "\\begin{enumerate}[(a)]\\setcounter{enumi}{2}\\item c\\label{it:c}\\item d\\end{enumerate}\n"
        "\\begin{enumerate}[label=\\roman*.]\\item i\\end{enumerate}\n"
        "\\begin{description}\\item[Term] Meaning.\\item[\\emph{T2}] More.\\end{description} See \\ref{it:c}."))
    body = _body(book)
    ul = body.find("ul")
    assert [_text(li.find("p")) for li in ul.findall("li")] == ["one", "⋆ two"] and _text(ul.find("li/ul/li")) == "nested"
    ols = body.findall("ol")
    assert ols[0].get("type") == "a" and ols[0].get("start") == "3" and ols[1].get("type") == "i"
    assert [_text(x) for x in body.find("dl")] == ["Term", "Meaning.", "T2", "More."]
    assert body.find("dl/dt/em").text == "T2"
    assert _text(next(a for a in body.iter("a") if a.get("class") == "ref")) == "c"


def test_tables_multicolumn_multirow_booktabs_and_longtable(tmp_path):
    book = _read(tmp_path, _doc(
        "\\begin{table}[t]\\centering\\caption{Results}\\label{tab:r}\n"
        "\\begin{tabular}{l|cr@{}}\\toprule\nModel & \\multicolumn{2}{c}{Score} \\\\ \\midrule\n"
        "\\multirow{2}{*}{A} & 1 & 2 \\\\\n & 3 & 4 \\\\ \\cmidrule(lr){2-3} B & \\textbf{5} & 6\\\\\\bottomrule\n"
        "\\end{tabular}\\end{table}\n"
        "\\begin{longtable}{ll}\\caption{Long}\\\\ H1 & H2 \\\\ \\hline\\endfirsthead H1 & H2\\\\\\endhead a & b\\\\\n"
        "\\end{longtable}\nTable~\\ref{tab:r}."))
    body = _body(book)
    t1, t2 = body.findall("table")
    assert _text(t1.find("caption")) == "Table 1: Results" and t1.get("id") == "tab-r"
    assert [[(c.tag, _text(c), c.get("colspan"), c.get("rowspan")) for c in tr] for tr in t1.iter("tr")] == [
        [("th", "Model", None, None), ("th", "Score", "2", None)],
        [("td", "A", None, "2"), ("td", "1", None, None), ("td", "2", None, None)],
        [("td", "3", None, None), ("td", "4", None, None)],
        [("td", "B", None, None), ("td", "5", None, None), ("td", "6", None, None)]]
    assert t1.find(".//td").get("style") == "text-align: left"
    assert _text(t2.find("caption")) == "Table 2: Long"
    assert [[_text(c) for c in tr] for tr in t2.iter("tr")] == [["H1", "H2"], ["a", "b"]]
    assert _text(next(a for a in body.iter("a") if a.get("class") == "ref")) == "1"


def test_figures_pictures_and_subfigures(tmp_path):
    book = _read(tmp_path, _doc(
        "\\graphicspath{{figs/}}\n\\begin{figure}[h]\\centering\\includegraphics[width=0.5\\linewidth]{plot}"
        "\\caption{A plot.}\\label{fig:p}\\end{figure}\n"
        "\\begin{figure}\\begin{subfigure}{0.4\\textwidth}\\includegraphics{vec.pdf}\\caption{Left}\\label{fig:l}"
        "\\end{subfigure}\\begin{subfigure}{0.4\\textwidth}\\includegraphics{nothere}\\caption{Right}\\end{subfigure}"
        "\\caption{Both.}\\end{figure} See \\ref{fig:p} and \\ref{fig:l}."),
        **{"figs/plot.png": PNG, "vec.pdf": b"%PDF-1.4 vector"})
    body = _body(book)
    figs = body.findall("figure")
    assert len(figs) == 2
    img = figs[0].find("img")
    assert img.get("src") == "../images/plot.png" and img.get("style") == "width: 50%"
    assert _text(figs[0].find("figcaption")) == "Figure 1: A plot." and figs[0].get("id") == "fig-p"
    assert len(book.images()) == 1
    subs = figs[1].findall("figure")
    assert [_text(s.find("figcaption")) for s in subs] == ["(a) Left", "(b) Right"]
    assert _text(figs[1].findall("figcaption")[-1]) == "Figure 2: Both."
    assert [_text(a) for a in body.iter("a") if a.get("class") == "ref"] == ["1", "2a"]
    assert any("PDF form" in line and "vec.pdf" in line for line in book.lost)
    assert any("not found" in line and "nothere" in line for line in book.lost)


EPS = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 200 100\n%%EndComments\n"
       b"1 0 0 setrgbcolor 10 10 180 80 rectfill 0 setgray /Helvetica findfont 20 scalefont setfont "
       b"20 40 moveto (Plot) show\n%%EOF\n")


def test_eps_figures_become_svg_in_the_epub(tmp_path):
    import zipfile
    book = _read(tmp_path, _doc(
        "\\begin{figure}\\includegraphics[width=0.5\\linewidth]{plot}\\caption{A plot.}\\end{figure}"
        "\\begin{figure}\\epsfig{file=broken.eps}\\caption{Broken.}\\end{figure}"),
        **{"plot.eps": EPS, "broken.eps": b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 9 9\nnosuchop\n"})
    imgs = list(_body(book).iter("img"))
    assert imgs[0].get("src") == "../images/plot.svg" and imgs[0].get("style") == "width: 50%"
    res = book.resources["images/plot.svg"]
    assert res.media_type == "image/svg+xml"
    root = ET.fromstring(res.data)
    assert root.get("viewBox") == "0 0 200 100" and b'fill="#ff0000"' in res.data and b">Plot</text>" in res.data
    assert any("plot.eps" in line and "Helvetica" in line for line in book.lost)  # its standard font, noted
    assert any("broken.eps couldn't be drawn" in line for line in book.lost)
    assert imgs[1].get("alt") == "broken.eps"  # its place kept
    out = book.write(tmp_path / "paper.epub")
    with zipfile.ZipFile(out) as z:
        svgs = [n for n in z.namelist() if n.endswith("plot.svg")]
        assert svgs and z.read(svgs[0]) == res.data
        opf = next(z.read(n).decode() for n in z.namelist() if n.endswith(".opf"))
        assert 'media-type="image/svg+xml"' in opf
        pages = "".join(z.read(n).decode() for n in z.namelist() if n.endswith((".xhtml", ".html")))
        assert "plot.svg" in pages


def test_theorems_proofs_footnotes_and_quotes(tmp_path):
    book = _read(tmp_path, _doc(
        "\\section{S}\\begin{theorem}[Main]\\label{thm:m} All good. \\end{theorem}"
        "\\begin{lemma}Small.\\end{lemma}\\begin{defn}A word.\\end{defn}\\begin{rem}Note.\\end{rem}\n"
        "\\begin{proof}Easy.\\end{proof}\nText\\footnote{A note with \\emph{style}.} and\\footnote{Two.}\n"
        "\\begin{quote}Quoted.\\end{quote} By Theorem~\\ref{thm:m}.",
        "\\newtheorem{theorem}{Theorem}[section]\n\\newtheorem{lemma}[theorem]{Lemma}\n"
        "\\theoremstyle{definition}\\newtheorem{defn}{Definition}\\newtheorem*{rem}{Remark}"))
    body = _body(book)
    divs = {d.get("class"): d for d in body.iter("div")}
    assert _text(divs["theorem"]) == "Theorem 1.1 (Main). All good." and divs["theorem"].find("p/em") is not None
    assert _text(divs["lemma"]) == "Lemma 1.2. Small."
    assert _text(divs["defn"]) == "Definition 1. A word." and divs["defn"].find("p/em") is None
    assert _text(divs["rem"]) == "Remark. Note."
    assert _text(divs["proof"]) == "Proof. Easy.\u00a0□"
    calls = [a for a in body.iter("a") if a.get("class") == "noteref"]
    notes = [a for a in body.iter("aside")]
    assert [_text(a) for a in calls] == ["1", "2"] and [n.get("id") for n in notes] == ["fn1", "fn2"]
    assert _text(notes[0]) == "A note with style." and notes[0].find("p/em") is not None
    assert _text(body.find("blockquote")) == "Quoted."
    assert _text(next(a for a in body.iter("a") if a.get("class") == "ref")) == "1.1"


def test_links_formatting_and_unknown_commands(tmp_path):
    book = _read(tmp_path, _doc(
        "\\href{https://x.org/a\\#b}{X} \\url{https://y.org/%7Ez} \\emph{e} \\textbf{b} \\texttt{t--t} "
        "{\\bf bold \\it both} \\textsc{sc} \\underline{u} \\sout{s} \\textsuperscript{2}\n"
        "\\mystery[opt]{kept text}\\alsounknown \\vspace{1cm}\\definecolor{c}{rgb}{1,0,0}\\textcolor{red}{red}"))
    p = _body(book).find("p")
    links = p.findall("a")
    assert [(a.get("href"), _text(a)) for a in links] == [("https://x.org/a#b", "X"), ("https://y.org/%7Ez", "https://y.org/%7Ez")]
    assert p.find("code").text == "t--t"
    assert [_text(x) for x in p.findall("strong")] == ["b", "bold", "both"] and p.find("strong/em").text == "both"
    assert p.find("span[@class='smallcaps']").text == "sc" and p.find("u") is not None and p.find("del") is not None
    assert _text(p).endswith("kept textred") and "rgb" not in _text(p)
    lost = " ".join(book.lost)
    assert "\\mystery" in lost and "\\alsounknown" in lost and "\\vspace" not in lost


def test_citations_with_bib_bbl_and_inline_bibliography(tmp_path):
    bibtext = "@article{smith20, author={Smith, Ann and Lee, Bo}, title={A Paper}, journal={J}, year={2020}}\n"
    book = _read(tmp_path, _doc("As \\citet{smith20} show \\citep[see][p.~2]{smith20}. \\cite{gone}\n"
                                "\\bibliographystyle{plainnat}\\bibliography{refs}", "\\usepackage{natbib}"),
                 **{"refs.bib": bibtext})
    body = _body(book)
    cites = [_text(s) for s in body.iter("span") if s.get("class") == "citation"]
    assert cites == ["Smith and Lee (2020)", "(see Smith and Lee 2020, p. 2)", "(gone?)"]
    refs = body.find(".//div[@class='references']")
    assert [d.get("id") for d in refs] == ["ref-smith20"] and "“A Paper.”" in _text(refs)
    assert _text([h for h in body.iter("h1")][-1]) == "References"
    # no .bib, but the .bbl beside the paper (as arXiv ships them): natbib labels give author and year
    bbl = ("\\begin{thebibliography}{1}\n\\bibitem[{Vaswani et~al.(2017)Vaswani, Shazeer}]{vas}\nAshish Vaswani."
           "\n\\newblock Attention.\n\\bibitem[Kim(2014)]{kim} Yoon Kim. \\newblock CNNs.\n\\end{thebibliography}")
    book = _read(tmp_path, _doc("\\citep{vas,kim} and \\citet{kim}.\\bibliography{none}", "\\usepackage{natbib}"),
                 name="paper2.tex", **{"paper2.bbl": bbl})
    body = _body(book)
    assert [_text(s) for s in body.iter("span") if s.get("class") == "citation"] == [
        "(Vaswani et al. 2017; Kim 2014)", "Kim (2014)"]
    assert [_text(d) for d in body.find(".//div[@class='references']")] == ["Ashish Vaswani. Attention.",
                                                                          "Yoon Kim. CNNs."]
    # numeric, from the document's own thebibliography
    book = _read(tmp_path, _doc("See \\cite{b,a}.\n\\begin{thebibliography}{9}\\bibitem{a} First.\\bibitem{b} Second."
                                "\\end{thebibliography}"), name="paper3.tex")
    body = _body(book)
    assert [_text(s) for s in body.iter("span") if s.get("class") == "citation"] == ["[1, 2]"]
    assert [_text(d) for d in body.find(".//div[@class='references']")] == ["[1] First.", "[2] Second."]


def test_tikz_and_unknown_environments(tmp_path):
    book = _read(tmp_path, _doc("\\begin{tikzpicture}\\draw (0,0) -- (1,1) node {label};\\end{tikzpicture}"
                                "\\begin{mybox}inside\\end{mybox}"))
    assert _text(_body(book)) == "inside"
    assert any("TikZ" in line for line in book.lost) and any("mybox" in line for line in book.lost)


def test_fragment_without_preamble_and_language(tmp_path):
    book = _read(tmp_path, "\\section{Only}\nText.")
    assert [_text(x) for x in _body(book)] == ["1 Only", "Text."]
    book = _read(tmp_path, _doc("Hallo.", "\\usepackage[ngerman]{babel}"), name="de.tex")
    assert book.metadata.language == "de"


# --- hostile input --------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "{" * 50_000 + "deep" + "}" * 50_000,
    "\\textbf{" * 5_000 + "deep" + "}" * 5_000,
    "\\begin{itemize}\\item " * 3_000 + "deep",
    "\\begin{quote}" * 3_000 + "deep",
    "$" + "{" * 20_000 + "x",
    "\\begin{verbatim}" * 20_000,
    "\\verb" * 50_000,
    "\\iffalse " * 10_000 + "deep",
    "\\footnote{" * 3_000 + "deep",
    "\\begin{tabular}{" + "c" * 10_000 + "}" + "a&" * 50_000 + "\\end{tabular}",
    "\\multicolumn{999999999}{c}{x}\\multirow{-99999999}{*}{y}" * 100,
    "\\section{" * 2_000 + "x",
], ids=["braces", "textbf", "itemize", "quote", "math", "verbatim", "verb", "iffalse", "footnote", "tabular", "spans",
        "section"])
def test_hostile_documents_finish_quickly(tmp_path, body):
    t = time.perf_counter()
    book = _read(tmp_path, _doc(body))
    assert time.perf_counter() - t < 30
    assert book.spine


# --- writing --------------------------------------------------------------------------------------------

def _write(book) -> str:
    return latex._LatexWriter(book).write_book()


def _balanced(text: str) -> bool:
    body = re.sub(r"\\begin\{verbatim\}.*?\\end\{verbatim\}", "", text, flags=re.DOTALL)
    body = re.sub(r"\\[{}%$&#_]", "", body)
    depth = 0
    for c in body:
        depth += {"{": 1, "}": -1}.get(c, 0)
        if depth < 0:
            return False
    envs = re.findall(r"\\(begin|end)\{([^}]*)\}", body)
    stack = []
    for kind, name in envs:
        if kind == "begin":
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return depth == 0 and not stack


def test_escaping():
    assert latex.escape("# $ % & _ { } ~ ^ \\ <x> [y] a--b ``q''") == (
        "\\# \\$ \\% \\& \\_ \\{ \\} \\textasciitilde{} \\textasciicircum{} \\textbackslash{} "
        "\\textless{}x\\textgreater{} {[}y{]} a-{}-b \\textasciigrave{}\\textasciigrave{}q'{}'")  # a backtick, not a quote
    assert latex.escape("α ≤ β\u00a0x™") == "α ≤ β~x\\texttrademark{}"


def test_markdown_to_latex_is_a_complete_document(tmp_path):
    md = tmp_path / "a.md"
    png = tmp_path / "p.png"
    png.write_bytes(PNG)
    md.write_text("---\ntitle: My_Doc & Co\nauthor: Ann\n---\n\n# Head #1\n\nText with *em*, **b**, `co_de`, "
                  "~~gone~~, [link](https://x.org/?a=1&b=%20) and a note.\n\n- one\n- two\n  1. nested\n\n"
                  "| A | B |\n|---|--:|\n| 1 | 2 |\n\n```python\nx = {'a': 1}  # 100%\n```\n\n> quote\n\n![pic](p.png)\n",
                  encoding="utf-8")
    book = bookport.read(md, "md")
    text = _write(book)
    assert text.startswith("\\documentclass{article}") and text.rstrip().endswith("\\end{document}")
    assert "\\title{My\\_Doc \\& Co}" in text and "\\maketitle" in text
    assert "\\section{Head \\#1}" in text and "\\setcounter{secnumdepth}{-2}" in text
    assert "\\emph{em}" in text and "\\textbf{b}" in text and "\\texttt{co\\_de}" in text and "\\sout{gone}" in text
    assert "\\href{https://x.org/?a=1&b=\\%20}{link}" in text
    assert "\\begin{itemize}" in text and "\\begin{enumerate}" in text
    assert "\\begin{longtable}[]{@{}lr@{}}" in text and "\\usepackage{longtable,booktabs,array}" in text
    assert "\\begin{verbatim}\nx = {'a': 1}  # 100%\n\\end{verbatim}" in text
    assert "\\begin{quote}" in text and "\\includegraphics[" in text and "{images/p.png}" in text
    assert _balanced(text)


def test_latex_round_trip_keeps_structure(tmp_path):
    src = _doc(
        "\\maketitle\\begin{abstract}Abs.\\end{abstract}\\section{Intro}\\label{s:i}"
        "Para with $x_1$ and a note\\footnote{Note \\emph{here}.} and \\cite{k}.\n"
        "\\begin{equation}\\label{eq:a} a=b \\end{equation}\nSee \\eqref{eq:a} and Section~\\ref{s:i}.\n"
        "\\begin{theorem}\\label{t:1}True.\\end{theorem}\\begin{proof}Done.\\end{proof}\n"
        "\\begin{table}\\caption{T}\\label{tab:t}\\begin{tabular}{lc}a & b\\\\ \\multicolumn{2}{c}{c}\\end{tabular}"
        "\\end{table}\n\\begin{description}\\item[x] y\\end{description}\n"
        "\\begin{enumerate}\\item one\\label{it:1}\\end{enumerate}\n"
        "\\begin{thebibliography}{9}\\bibitem{k} K. Author. Title.\\end{thebibliography}",
        "\\title{Round \\& Trip}\\author{A \\and B}\\newtheorem{theorem}{Theorem}")
    book = _read(tmp_path, src)
    text = _write(book)
    assert _balanced(text)
    for piece in ("\\title{Round \\& Trip}", "\\author{A \\and B}", "\\begin{abstract}\nAbs.\n\\end{abstract}",
                  "\\section{Intro}\\label{s:i}", "\\footnote{Note \\emph{here}.}", "\\cite{k}",
                  "\\begin{equation}\\label{eq:a} a=b \\end{equation}", "\\eqref{eq:a}", "\\ref{s:i}",
                  "\\newtheorem{theorem}{Theorem}", "\\begin{theorem}\n\\label{t:1}\nTrue.\n\\end{theorem}",
                  "\\begin{proof}\nDone.\n\\end{proof}", "\\caption{T}\\label{tab:t}", "\\multicolumn{2}{c}{c}",
                  "\\item[{x}] y", "\\label{it:1} one", "\\begin{thebibliography}{9}", "\\bibitem{k}"):
        assert piece in text, piece
    out = tmp_path / "again" / "doc.tex"
    out.parent.mkdir()
    out.write_text(text, encoding="utf-8")
    again = bookport.read(out, "tex")
    a, b = _body(book), _body(again)
    for tag in ("h1", "p", "table", "td", "dt", "li", "aside", "math"):
        assert len(list(a.iter(tag))) == len(list(b.iter(tag))), tag
    assert [_text(x) for x in a.iter("span") if x.get("class") == "citation"] == \
        [_text(x) for x in b.iter("span") if x.get("class") == "citation"]


def test_writer_handles_deep_lists_code_in_notes_and_wide_tables():
    root, body = markup.new_document("t")
    parent = body
    for _ in range(7):
        li = ET.SubElement(ET.SubElement(parent, "ul"), "li")
        li.text = "level"
        parent = li
    p = ET.SubElement(body, "p")
    a = ET.SubElement(p, "a", {"class": "noteref", "epub:type": "noteref", "href": "#n1"})
    ET.SubElement(a, "sup").text = "1"
    aside = ET.SubElement(body, "aside", {"epub:type": "footnote", "id": "n1"})
    ET.SubElement(ET.SubElement(aside, "pre"), "code").text = "x = 1\n\\end{verbatim}"
    table = ET.SubElement(body, "table")
    tr = ET.SubElement(table, "tr")
    for k in range(9):
        td = ET.SubElement(tr, "td")
        ET.SubElement(td, "p").text = f"cell {k}"
        ET.SubElement(td, "p").text = "second"
    book = bookport.Book(bookport.Metadata(title="t"))
    book.add_document("text/a.xhtml", root)
    text = _write(book)
    assert text.count("\\begin{itemize}") == 4 and _balanced(text)
    assert "\\footnote{{\\ttfamily" in text and "\\end{verbatim}" not in text.split("\\footnote")[1]
    assert "p{0.104\\linewidth}" in text and "\\newline" in text


def test_write_saves_pictures_beside_the_text(tmp_path):
    book = _read(tmp_path, _doc("\\begin{figure}\\includegraphics{pic}\\caption{C}\\end{figure}"),
                 **{"pic.png": PNG})
    out = tmp_path / "out" / "doc.tex"
    out.parent.mkdir()
    saved = latex.write(book, out, pictures=True)
    assert saved == ["images/pic.png"] and (out.parent / "images" / "pic.png").read_bytes() == PNG
    assert "{images/pic.png}" in out.read_text(encoding="utf-8")
    assert latex.write(book, tmp_path / "out" / "plain.tex") == []


def test_footnote_marks_and_texts(tmp_path):
    book = _read(tmp_path, _doc(
        "\\maketitle A\\footnotemark{} and B\\footnotemark[\\thefootnote]. "
        "\\footnotetext{Shared note.} C\\footnotemark[1] D\\footnotetext[7]{Lone text.}",
        "\\title{T}\\author{Ann\\thanks{Equal.}}"))
    body = _body(book)
    calls = [(a.get("href"), _text(a)) for a in body.iter("a") if a.get("class") == "noteref"]
    assert calls == [("#fn1", "1"), ("#fn2", "2"), ("#fn2", "2"), ("#fn1", "1")]
    notes = {a.get("id"): _text(a) for a in body.iter("aside")}
    assert notes == {"fn1": "Equal.", "fn2": "Shared note.", "fn3": "Lone text."}
    assert all(a.get("data-shown") is None for a in body.iter("aside"))


def test_delimited_macros(tmp_path):
    book = _read(tmp_path, _doc("\\upto first part. rest \\until{a.b}\\stop after \\loop x.",
                                "\\def\\upto#1.{[#1]}\\def\\until#1\\stop{<#1>}\\def\\loop#1.{\\loop#1.}"))
    assert _text(_body(book).find("p")).startswith("[first part] rest <a.b>after")
    assert any("expanded too many times" in line for line in book.lost)
