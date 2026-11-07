"""man pages: reading roff with the man macros (fonts, lists, tbl, includes, conditionals) and writing one."""

import time

import bookport
from bookport import markup

PAGE = r""".\" A comment line
.TH LS 1 "2024-01-02" "GNU" "User Commands"
.SH NAME
ls \- list directory contents
.SH SYNOPSIS
.nf
.B ls
[\fIOPTION\fR]... [\fIFILE\fR]...
.PP
.BR more (1)
.fi
.SH DESCRIPTION
List information about the FILEs (the current directory by default).
Sort entries \fBalphabetically\fP if none of
.BR \-cftuvSUX " nor " \-\-sort
is specified.\(em with \(lqquotes\(rq and \[u00E9]t\[char233] \e back\-slash.
.PP
Second paragraph \f(CWcode\fR and \f[BI]both\f[].
.SS Options
.TP
.B \-a, \-\-all
do not ignore entries starting with .
.TP
.BI \-\-block\-size= SIZE
scale sizes by SIZE
.RS
.PP
More about it.
.RE
.TQ
.B \-B
second tag
.PP
.IP \(bu 2
first bullet
.IP \(bu 2
second bullet
.IP 1. 4
numbered
.IP 2. 4
next
.PP
See
.UR https://www.gnu.org/software/coreutils/
the site
.UE .
Mail
.MT bug@example.org
.ME .
.RS
.PP
Indented block.
.RE
.EX
int main(void)
{
    return 0;
}
.EE
.SH "SEE ALSO"
.BR dir (1)
"""


def _read(tmp_path, text: str, name="ls.1"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return bookport.read(p, "man")


def _body(book) -> str:
    return "".join(book.resources[h].data.decode() for h, _ in book.documents())


def test_title_sections_and_fonts(tmp_path):
    book = _read(tmp_path, PAGE)
    assert book.metadata.title == "LS(1)" and book.metadata.date == "2024-01-02"
    body = _body(book)
    assert ">NAME</h1><p>ls - list directory contents</p>" in body
    assert ">SEE ALSO</h1>" in body and ">Options</h2>" in body
    assert "<strong>alphabetically</strong> if none of <strong>-cftuvSUX</strong> nor <strong>--sort</strong>" in body
    assert "\u2014 with \u201cquotes\u201d and \u00e9t\u00e9 \\ back-slash." in body
    assert "<code>code</code>" in body and "<strong><em>both</em></strong>" in body


def test_nofill_synopsis_keeps_lines(tmp_path):
    body = _body(_read(tmp_path, PAGE))
    assert "<pre><strong>ls</strong>\n[<em>OPTION</em>]... [<em>FILE</em>]...\n\n<strong>more</strong>(1)</pre>" in body
    assert "<pre>int main(void)\n{\n    return 0;\n}</pre>" in body


def test_tagged_paragraphs_and_lists(tmp_path):
    body = _body(_read(tmp_path, PAGE))
    assert "<dl><dt><strong>-a, --all</strong></dt><dd><p>do not ignore entries starting with .</p></dd>" in body
    assert "<dt><strong>--block-size=</strong><em>SIZE</em></dt><dd><p>scale sizes by SIZE</p><p>More about it.</p></dd>" in body
    assert "<dt><strong>-B</strong></dt><dd><p>second tag</p></dd></dl>" in body
    assert "<ul><li><p>first bullet</p></li><li><p>second bullet</p></li></ul>" in body
    assert "<ol><li><p>numbered</p></li><li><p>next</p></li></ol>" in body


def test_links_and_indents(tmp_path):
    body = _body(_read(tmp_path, PAGE))
    assert 'See <a href="https://www.gnu.org/software/coreutils/">the site</a>. Mail' in body
    assert '<a href="mailto:bug@example.org">bug@example.org</a>.' in body
    assert "<blockquote><p>Indented block.</p></blockquote>" in body


def test_tbl_tables(tmp_path):
    text = """.TH T 1
.SH TABLES
.TS
allbox tab(@);
lb cb rb
l c r.
Name@Mid@Right
T{
A long
cell
T}@x@1
.T&
l s s.
spanning all three
.TE
.TS
tab(;);
rlcl.
a;b;c;d
_
1;2;3;4
.TE
"""
    body = _body(_read(tmp_path, text, "t.1"))
    assert "<thead><tr><th>Name</th><th style=\"text-align: center\">Mid</th>" in body
    assert "<td>A long cell</td>" in body and '<td colspan="3">spanning all three</td>' in body
    assert body.count("<table>") == 2 and "<th style=\"text-align: right\">a</th>" in body  # a rule under the first row makes it the heading


def test_includes_conditionals_strings_and_macros(tmp_path):
    (tmp_path / "man3").mkdir()
    (tmp_path / "man3" / "real.3").write_text(".TH REAL 3\n.SH NAME\nreal \\- the page\n", encoding="utf-8")
    (tmp_path / "man3" / "alias.3").write_text(".so man3/real.3\n", encoding="utf-8")
    book = bookport.read(tmp_path / "man3" / "alias.3", "man")
    assert "real - the page" in book.text() and book.metadata.title == "REAL(3)"
    text = r""".TH C 1
.ds Pn Product
.ie n .ds Q "nroff
.el .ds Q "troff
.if t \{\
Typeset only.
.\}
.de Ex
.B \\$1
said \\$2
..
Uses \*(Pn in \*Q mode.
.Ex Ada hello
.so ../../../outside.1
.so /etc/passwd
.xyz unknown macro text
"""
    book = _read(tmp_path, text, "c.1")
    body = _body(book)
    assert "Uses Product in nroff mode." in body and "Typeset only" not in body
    assert "<strong>Ada</strong> said hello" in body
    assert "unknown macro text" in body and any(".xyz" in m for m in book.lost)
    assert sum("outside the document's folder" in m for m in book.lost) == 2


def test_eqn_pic_kept_as_text(tmp_path):
    book = _read(tmp_path, ".TH E 1\n.EQ\nx sup 2\n.EN\nAfter.\n", "e.1")
    assert "x sup 2" in _body(book) and any("eqn" in m for m in book.lost)


def test_hostile_pages(tmp_path):
    t = time.perf_counter()
    # an include loop, a macro calling itself, deep .RS nesting, huge escapes
    (tmp_path / "a.1").write_text(".so b.1\n", encoding="utf-8")
    (tmp_path / "b.1").write_text(".so a.1\n", encoding="utf-8")
    book = bookport.read(tmp_path / "a.1", "man")
    assert any("repeat itself" in m for m in book.lost)
    _read(tmp_path, ".de L\n.L\n..\n.L\n", "loop.1")
    book = _read(tmp_path, ".RS\n" * 5000 + "deep\n" + ".RE\n" * 5000, "deep.1")
    assert "deep" in book.text() and not markup.too_deep(book.tree(book.spine[0]))
    _read(tmp_path, "\\f" * 100_000 + "\\(" * 50_000 + "\\[" * 50_000, "esc.1")
    _read(tmp_path, ".TS\n" + "l" * 10_000 + ".\n" + "x\t" * 10_000 + "\n.TE\n", "wide.1")
    _read(tmp_path, ".if n \\{\\\n" * 10_000, "cond.1")
    assert time.perf_counter() - t < 20


# --- writing ---------------------------------------------------------------------------------------

def _book_from_html(body: str, title="tool(8)"):
    root = markup.parse(f'<html xmlns:epub="{markup.EPUB_NS}"><head><title>T</title></head><body>{body}</body></html>')
    book = bookport.Book(bookport.Metadata(title=title, date="2024"))
    book.add_document("text/a.xhtml", root)
    return book


def test_write_man(tmp_path):
    out = tmp_path / "out.man"
    _book_from_html("<h1>Name</h1><p>tool - does <em>things</em> with <strong>-x</strong> and <code>a\\b</code>.</p>"
                    "<p>.starts with a dot<br/>'and a quote</p><h2>Sub</h2>"
                    '<dl><dt><strong>-v</strong></dt><dd><p>verbose</p><p>more</p></dd></dl>'
                    "<ul><li>one</li><li>two<ul><li>inner</li></ul></li></ul><ol start=\"3\"><li>three</li></ol>"
                    "<pre>if x:\n    .y = 1</pre><blockquote><p>quoted</p></blockquote>"
                    '<p><a href="https://x.example">site</a> and \u2014 caf\u00e9'
                    '<a class="noteref" epub:type="noteref" href="#n1">1</a></p>'
                    "<table><tr><th>H1</th><th>H2</th></tr><tr><td>a</td><td>b</td></tr></table>"
                    '<aside epub:type="footnote" id="n1"><p>A note.</p></aside>').write(out, "man")
    text = out.read_text(encoding="utf-8")
    assert text.startswith('.TH "TOOL" "8" "2024" "" ""\n')
    assert '.SH "Name"' in text and '.SS "Sub"' in text
    assert "tool \\- does \\f[I]things\\f[R] with \\f[B]\\-x\\f[R] and \\f[CR]a\\eb\\f[R]." in text
    assert "\\&.starts with a dot\n.br\n\\&'and a quote" in text
    assert ".TP\n\\f[B]\\-v\\f[R]\nverbose\n.RS\n.PP\nmore\n.RE" in text
    assert ".IP \\(bu 2\none\n.IP \\(bu 2\ntwo\n.RS 2\n.IP \\(bu 2\ninner\n.RE" in text
    assert '.IP "3." 4\nthree' in text
    assert ".EX\nif x:\n    .y = 1\n.EE" in text
    assert ".RS\n.PP\nquoted\n.RE" in text
    assert ".UR https://x.example\nsite\n.UE \\c" in text and "\\(em caf\\[u00E9]" in text
    assert "[1]" in text and ".SH NOTES\n.IP [1] 5\nA note." in text
    assert ".TS\nallbox;\nlb lb\nl l.\nT{\nH1\nT}\tT{\nH2\nT}" in text and ".TE" in text


def test_round_trip(tmp_path):
    first = _read(tmp_path, PAGE)
    out = tmp_path / "again.1"
    first.write(out, "man")
    second = bookport.read(out, "man")
    assert second.metadata.title == "LS(1)"
    assert second.text().split() == first.text().split()
    for tag in ("h1", "h2", "dt", "li", "pre", "a href"):  # (code blocks keep no fonts)
        assert _body(first).count(f"<{tag}") == _body(second).count(f"<{tag}"), tag


def test_detect_man_pages_by_content(tmp_path):
    p = tmp_path / "printf.3"
    p.write_text('.\\" comment\n.TH PRINTF 3\n.SH NAME\nprintf\n', encoding="utf-8")
    assert bookport.detect(p) == "man"
    q = tmp_path / "notes.1"
    q.write_text("Just text.\n", encoding="utf-8")
    assert bookport.detect(q) == "txt"
