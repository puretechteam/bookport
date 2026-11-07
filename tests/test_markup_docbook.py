"""DocBook 4 and 5: reading books, articles and their parts safely, and writing DocBook 5."""

import pytest

import bookport
from bookport import BookportError, markup

from .conftest import png

DB5 = 'xmlns="http://docbook.org/ns/docbook" xmlns:xlink="http://www.w3.org/1999/xlink" version="5.0"'


def _read(tmp_path, text: str, name="doc.xml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return bookport.read(p, "dbk")


def _body(book) -> str:
    return "".join(book.resources[h].data.decode() for h, _ in book.documents())


ARTICLE = f"""<?xml version="1.0" encoding="utf-8"?>
<article {DB5} xml:lang="en">
  <info>
    <title>A <emphasis>Test</emphasis> Article</title>
    <author><personname><firstname>Ada</firstname><surname>Lovelace</surname></personname></author>
    <author><orgname>Pure Tech</orgname></author>
    <date>2024-05-01</date>
    <abstract><para>About things.</para></abstract>
    <keywordset><keyword>tests</keyword></keywordset>
  </info>
  <section xml:id="intro">
    <title>Intro</title>
    <para>Plain <emphasis>em</emphasis>, <emphasis role="bold">bold</emphasis>, <literal>code</literal>,
      <command>ls</command>, <replaceable>file</replaceable>, x<superscript>2</superscript>,
      H<subscript>2</subscript>O, <quote>outer <quote>inner</quote></quote>
      <link xlink:href="https://example.com">a site</link>, <ulink url="https://old.example">old</ulink>,
      <email>me@example.com</email> and <xref linkend="later"/>.<indexterm><primary>hidden</primary></indexterm>
      A note<footnote><para>The note text.</para></footnote>.</para>
    <simpara>Simple.</simpara>
    <formalpara><title>Formal</title><para>body</para></formalpara>
    <section xml:id="later">
      <title>Later part</title>
      <itemizedlist spacing="compact"><listitem><para>one</para></listitem><listitem><para>two</para></listitem></itemizedlist>
      <orderedlist startingnumber="3"><listitem><para>three</para>
        <itemizedlist><listitem><para>nested</para></listitem></itemizedlist></listitem></orderedlist>
      <variablelist><varlistentry><term>T1</term><term>T2</term><listitem><para>D1</para></listitem>
        <listitem><para>D2</para></listitem></varlistentry></variablelist>
      <programlisting language="python">if a &lt; b:
    pass</programlisting>
      <screen>$ ls</screen>
      <literallayout>line one
  line two</literallayout>
      <blockquote><attribution>Someone</attribution><para>Quoted.</para></blockquote>
      <note><para>Careful.</para></note>
      <warning><title>Hot</title><para>Very.</para></warning>
      <para>Before the list
        <itemizedlist><listitem><para>inside a para</para></listitem></itemizedlist>
        after it.</para>
    </section>
  </section>
</article>"""


def test_article_metadata_and_sections(tmp_path):
    book = _read(tmp_path, ARTICLE)
    md = book.metadata
    assert md.title == "A Test Article" and md.authors == ["Ada Lovelace", "Pure Tech"]
    assert md.date == "2024-05-01" and md.description == "About things." and md.subjects == ["tests"]
    assert md.language == "en" and book.source_format == "dbk"
    body = _body(book)
    assert '<h1 id="intro">Intro</h1>' in body and '<h2 id="later">Later part</h2>' in body


def test_article_inline_markup(tmp_path):
    body = _body(_read(tmp_path, ARTICLE))
    assert "<em>em</em>" in body and "<strong>bold</strong>" in body and "<code>code</code>" in body
    assert "<code>ls</code>" in body and "<var>file</var>" in body and "x<sup>2</sup>" in body
    assert "H<sub>2</sub>O" in body
    assert "\u201couter \u2018inner\u2019\u201d" in body
    assert '<a href="https://example.com">a site</a>' in body and '<a href="https://old.example">old</a>' in body
    assert '<a href="mailto:me@example.com">me@example.com</a>' in body
    assert '<a href="#later">Later part</a>' in body and "hidden" not in body
    assert '<a class="noteref" epub:type="noteref" href="#fn1"><sup>1</sup></a>' in body
    assert '<aside epub:type="footnote" id="fn1"><p>The note text.</p></aside>' in body


def test_article_blocks(tmp_path):
    body = _body(_read(tmp_path, ARTICLE))
    assert "<p><strong>Formal</strong> body</p>" in body and "<p>Simple.</p>" in body
    assert "<ul><li>one</li><li>two</li></ul>" in body  # compact: no paragraphs in the items
    assert '<ol start="3"><li><p>three</p><ul><li><p>nested</p></li></ul></li></ol>' in body
    assert "<dl><dt>T1, T2</dt><dd><p>D1</p></dd><dd><p>D2</p></dd></dl>" in body
    assert '<pre><code class="language-python">if a &lt; b:\n    pass</code></pre>' in body
    assert "<pre><code>$ ls</code></pre>" in body
    assert '<div class="line-block">line one<br/>\xa0\xa0line two</div>' in body
    assert "<blockquote><p>Quoted.</p><p>\u2014 Someone</p></blockquote>" in body
    assert '<div class="note"><div class="title"><strong>Note</strong></div><p>Careful.</p></div>' in body
    assert "<strong>Hot</strong>" in body
    assert "<p>Before the list</p><ul><li><p>inside a para</p></li></ul><p>after it.</p>" in body


def test_book_chapters_cals_table_figure_math(tmp_path):
    (tmp_path / "pic.png").write_bytes(png())
    text = """<!DOCTYPE book PUBLIC "-//OASIS//DTD DocBook XML V4.5//EN" "http://www.oasis-open.org/docbook/xml/4.5/docbookx.dtd" [
<!ENTITY product "Widget&#8482;">
<!ENTITY % param "ignored">
]>
<book><bookinfo><title>The &product; Book</title><author><firstname>Bo</firstname><surname>Ok</surname></author>
<copyright><year>2020</year><holder>Pure Tech</holder></copyright></bookinfo>
<chapter id="c1"><title>One</title>
<para>Using &product; &amp; friends&nbsp;now.</para>
<table><title>Spans</title><tgroup cols="3">
<colspec colname="a"/><colspec colname="b"/><colspec colname="c"/>
<thead><row><entry namest="a" nameend="b">wide</entry><entry>c</entry></row></thead>
<tbody><row><entry morerows="1">tall</entry><entry><para>p1</para></entry><entry align="right">r</entry></row>
<row><entry>x</entry><entry>y</entry></row></tbody></tgroup></table>
<figure id="f1"><title>A picture</title><mediaobject>
<imageobject><imagedata fileref="pic.eps"/></imageobject>
<imageobject><imagedata fileref="pic.png"/></imageobject>
<textobject><phrase>alt words</phrase></textobject></mediaobject></figure>
<para>See <xref linkend="f1"/> and <link linkend="c1">chapter</link>.</para>
<informalequation><alt role="tex">E = mc^2</alt></informalequation>
<para>Inline <inlineequation><mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mi>x</mml:mi></mml:math></inlineequation>.</para>
</chapter>
<appendix><title>Extra</title><sect1><title>Deeper</title><para>Done.</para></sect1></appendix>
</book>"""
    book = _read(tmp_path, text)
    assert book.metadata.title == "The Widget\u2122 Book" and book.metadata.authors == ["Bo Ok"]
    assert book.metadata.rights == "Copyright 2020 Pure Tech"
    body = _body(book)
    assert "Using Widget\u2122 &amp; friends\xa0now." in body
    assert '<h1 id="c1">One</h1>' in body and ">Extra</h1>" in body and ">Deeper</h2>" in body
    assert "<caption>Spans</caption>" in body and '<th colspan="2">wide</th>' in body
    assert '<td rowspan="2">tall</td>' in body and "<td><p>p1</p></td>" in body
    assert '<td style="text-align: right">r</td>' in body
    assert len(book.images()) == 1 and 'alt="alt words"' in body and "<figcaption>A picture</figcaption>" in body
    assert '<a href="#f1">A picture</a>' in body and '<a href="#c1">chapter</a>' in body
    assert body.count("<math") == 2 and "E = mc^2" in body and 'display="block"' in body


def test_glossary_bibliography_refentry_procedure(tmp_path):
    text = f"""<article {DB5}><title>T</title>
<glossary><glossentry xml:id="g1"><glossterm>Term</glossterm><glossdef><para>Meaning.</para></glossdef></glossentry></glossary>
<bibliography><biblioentry><author><personname>A. Writer</personname></author><title>A Book</title>
<publisher><publishername>Pub</publishername></publisher></biblioentry></bibliography>
<procedure><title>Steps</title><step><para>First</para></step><step><para>Second</para>
<substeps><step><para>Sub</para></step></substeps></step></procedure>
<refentry><refmeta><refentrytitle>tool</refentrytitle><manvolnum>1</manvolnum></refmeta>
<refnamediv><refname>tool</refname><refpurpose>does things</refpurpose></refnamediv>
<refsect1><title>Options</title><para>See <citerefentry><refentrytitle>ls</refentrytitle><manvolnum>1</manvolnum></citerefentry>.</para></refsect1>
</refentry>
<unknownthing>kept text</unknownthing>
</article>"""
    book = _read(tmp_path, text)
    body = _body(book)
    assert '<dt id="g1"><em>Term</em></dt><dd><p>Meaning.</p></dd>' in body or "<dt id=\"g1\">Term</dt>" in body
    assert "A. Writer, A Book, Pub" in body
    assert "<p><strong>Steps</strong></p><ol><li><p>First</p></li><li><p>Second</p><ol><li><p>Sub</p></li></ol></li></ol>" in body
    assert "tool \u2014 does things" in body and "ls(1)" in body
    assert "kept text" in body and any("<unknownthing>" in m for m in book.lost)


def test_xinclude_inside_folder_only(tmp_path):
    (tmp_path / "part.xml").write_text(f'<section {DB5}><title>Included</title><para>From a file.</para></section>',
                                       encoding="utf-8")
    (tmp_path / "code.txt").write_text("print(1)", encoding="utf-8")
    (tmp_path / "loop.xml").write_text('<section xmlns:xi="http://www.w3.org/2001/XInclude"><title>L</title>'
                                       '<xi:include href="loop.xml"/></section>', encoding="utf-8")
    text = f"""<article {DB5} xmlns:xi="http://www.w3.org/2001/XInclude"><title>T</title>
<xi:include href="part.xml"/>
<programlisting><xi:include href="code.txt" parse="text"/></programlisting>
<xi:include href="../../outside.xml"/>
<xi:include href="C:/Windows/win.ini" parse="text"/>
<xi:include href="loop.xml"/>
</article>"""
    book = _read(tmp_path, text)
    body = _body(book)
    assert ">Included</h1>" in body and "From a file." in body and "print(1)" in body
    assert sum("outside the document's folder" in m for m in book.lost) == 2
    assert any("repeat itself" in m for m in book.lost)


def test_hostile_entities_and_depth(tmp_path):
    bomb = ('<!DOCTYPE a [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><article><para>&c;&c;&c;</para></article>')
    book = _read(tmp_path, bomb, "bomb.xml")
    assert len(book.text()) < 1000  # declared entities never expand into each other
    big = '<!DOCTYPE a [<!ENTITY x "' + "x" * 60_000 + '">]><article><para>' + "&x;" * 400 + "</para></article>"
    with pytest.raises(BookportError):
        _read(tmp_path, big, "big.xml")
    ext = '<!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><article><para>[&e;]</para></article>'
    book = _read(tmp_path, ext, "ext.xml")
    assert "[]" in book.text() and any("external entity" in m for m in book.lost)
    deep = "<article>" + "<section><title>t</title>" * 200 + "</section>" * 200 + "</article>"
    with pytest.raises(BookportError):
        _read(tmp_path, deep, "deep.xml")
    with pytest.raises(BookportError):
        _read(tmp_path, "<article><para>unclosed</article>", "bad.xml")


def test_undeclared_prefixes_are_forgiven(tmp_path):
    book = _read(tmp_path, "<article><para>x <inlineequation><mml:math><mml:mi>y</mml:mi></mml:math></inlineequation>"
                           "</para></article>")
    assert "<math" in _body(book)


# --- writing ---------------------------------------------------------------------------------------

def _book_from_html(body: str):
    root = markup.parse(f'<html xmlns:epub="{markup.EPUB_NS}"><head><title>T</title></head><body>{body}</body></html>')
    book = bookport.Book(bookport.Metadata(title="Title & More", authors=["Ann Author"], date="2024", language="en"))
    book.add_document("text/a.xhtml", root)
    return book


def test_write_docbook5(tmp_path):
    out = tmp_path / "out.dbk"
    _book_from_html('<h1 id="s1">One</h1><p>Text <em>e</em> <strong>s</strong> <code>c&lt;</code> '
                    '<a href="https://x.example">x</a> <a href="#s2">to two</a> H<sub>2</sub>O'
                    '<a class="noteref" epub:type="noteref" href="#n1">1</a></p>'
                    '<h2>Sub</h2><ul><li>a</li><li>b</li></ul><ol start="2"><li><p>p</p><p>q</p></li></ol>'
                    '<dl><dt>T</dt><dd>D</dd></dl><pre><code class="language-sh">ls &amp;&amp; cd</code></pre>'
                    '<blockquote><p>q</p></blockquote>'
                    '<table><caption>Cap</caption><thead><tr><th colspan="2">H</th></tr></thead>'
                    '<tbody><tr><td rowspan="2">r</td><td>1</td></tr><tr><td>2</td></tr></tbody></table>'
                    '<figure><img src="i.png" alt="alt"/><figcaption>Fig</figcaption></figure>'
                    '<h1 id="s2">Two</h1><p>End.</p>'
                    '<aside epub:type="footnote" id="n1"><p>Note.</p></aside>').write(out, "dbk")
    text = out.read_text(encoding="utf-8")
    root = markup.parse_xml(text.split("\n", 1)[1])  # well-formed
    assert root.tag.endswith("article")
    assert "<title>Title &amp; More</title>" in text and "<personname>Ann Author</personname>" in text
    assert '<section xml:id="a-s1">\n<title>One</title>' in text or "<title>One</title>" in text
    assert text.count("<section") == 3 and text.count("</section>") == 3
    assert '<emphasis>e</emphasis> <emphasis role="strong">s</emphasis> <literal>c&lt;</literal>' in text
    assert '<link xlink:href="https://x.example">x</link>' in text and "<link linkend=" in text
    assert "<footnote><para>Note.</para></footnote>" in text
    assert '<itemizedlist spacing="compact">' in text and '<orderedlist startingnumber="2">' in text
    assert "<variablelist>" in text and '<programlisting language="sh">ls &amp;&amp; cd</programlisting>' in text
    assert '<entry namest="c1" nameend="c2">H</entry>' in text and '<entry morerows="1">r</entry>' in text
    assert '<imagedata fileref="images/i.png"/>' in text and "<title>Fig</title>" in text


def test_round_trip(tmp_path):
    first = _read(tmp_path, ARTICLE)
    out = tmp_path / "again.dbk"
    first.write(out)
    second = bookport.read(out)
    assert second.metadata.title == first.metadata.title and second.metadata.authors == first.metadata.authors
    assert second.text().split() == first.text().split()
    for tag in ("h1", "h2", "li", "dt", "pre", "blockquote", "aside", "strong", "code"):
        assert _body(first).count(f"<{tag}") == _body(second).count(f"<{tag}"), tag
