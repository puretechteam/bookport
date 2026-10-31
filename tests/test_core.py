"""The shared machinery: HTML parsing and writing, CSS, styled blocks, Markdown, plain-text heuristics."""

import xml.etree.ElementTree as ET

from bookport import css, flow, markup
from bookport.fmt import markdown, txt


def test_html_parser_closes_tags_like_browsers():
    root = markup.parse("<p>one<p>two<ul><li>a<li>b</ul><table><tr><td>x<td>y</table>")
    body = root.find("body")
    assert [e.tag for e in body] == ["p", "p", "ul", "table"]
    assert [li.text for li in body.find("ul")] == ["a", "b"]
    assert [td.text for td in body.iter("td")] == ["x", "y"]


def test_xhtml_with_entities_and_namespaces():
    root = markup.parse('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                        '<head><title>t</title></head><body><p epub:type="x">a&nbsp;b &mdash; c</p>'
                        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><image xlink:href="i.png" '
                        'xmlns:xlink="http://www.w3.org/1999/xlink"/></svg></body></html>')
    p = root.find("body/p")
    assert p.get("epub:type") == "x" and p.text == "a b — c"
    assert root.find(".//image").get("xlink:href") == "i.png"
    out = markup.serialize(root).decode()
    assert 'xmlns:epub="http://www.idpf.org/2007/ops"' in out and 'xmlns="http://www.w3.org/2000/svg"' in out
    assert "viewBox" in out


def test_html_parser_keeps_svg_case():
    root = markup.parse("<html><body><svg viewbox='0 0 2 2'><lineargradient/></svg></body></html>")
    svg = root.find(".//svg")
    assert svg.get("viewBox") == "0 0 2 2" and svg.find("linearGradient") is not None


def test_decode_charsets():
    assert markup.decode(b"\xef\xbb\xbfcaf\xc3\xa9") == "café"
    assert markup.decode('<meta charset="windows-1252"><p>caf\xe9'.encode("cp1252")).endswith("café")
    assert markup.decode(b"caf\xe9") == "café"  # not UTF-8: falls back to Windows-1252


def test_css_cascade():
    root = markup.parse('<html><head></head><body><div class="a"><p id="x" class="b">t</p><p style="font-weight:bold">u</p>'
                        '</div></body></html>')
    styles = css.compute_styles(root, "div.a p { color: red } #x { color: blue !important } .b { color: green }"
                                      "p + p { font-style: italic } @media print { p { color: black } }"
                                      "@supports (display: grid) { .b { text-indent: 2em } }")
    ps = list(root.iter("p"))
    assert styles[ps[0]]["color"] == "blue" and styles[ps[0]]["text-indent"] == "2em"
    assert styles[ps[1]]["color"] == "red" and styles[ps[1]]["font-style"] == "italic"
    assert css.is_bold(styles[ps[1]])


def test_css_lengths():
    assert css.length("2em", 12) == 24
    assert css.length("50%", 12, percent_of=100) == 50
    assert css.length("12px") == 9
    assert css.font_size("larger", 10) == 12


def test_flow_blocks():
    root = markup.parse("<html><body><h2 id='h'>Head</h2><p>Some <b>bold</b> and <i>it</i><br/>next</p>"
                        "<ol start='3'><li>three</li><li>four</li></ol><div style='display:none'>hidden</div>"
                        "<p style='page-break-before: always'>new page</p></body></html>")
    blocks = flow.document_blocks(None, "d.xhtml", root)
    kinds = [b.kind for b in blocks]
    assert kinds == ["h", "p", "li", "li", "p"]
    assert blocks[0].anchors == ["d.xhtml#h"] and blocks[0].level == 2
    runs = blocks[1].runs
    assert [r.text for r in runs] == ["Some ", "bold", " and ", "it", "\n", "next"]
    assert runs[1].bold and runs[3].italic
    assert [b.marker for b in blocks[2:4]] == ["3.", "4."]
    assert blocks[4].page_break and "hidden" not in "".join(b.text() for b in blocks)


def test_markdown_reader():
    html = markdown.to_html("# Title\n\nSome *em* and **strong** and `code` and [a link](http://x.org \"t\").\n\n"
                            "- one\n- two\n  - nested\n\n1. first\n2. second\n\n> quoted\n\n```py\nx = 1 < 2\n```\n\n"
                            "| a | b |\n|---|:-:|\n| 1 | 2 |\n\n---\n\n[ref]: http://ref.example\n\nSee [ref].\n")
    root = markup.parse(f"<html><body>{html}</body></html>")
    body = root.find("body")
    assert body.find("h1").text == "Title"
    assert body.find(".//em").text == "em" and body.find(".//strong").text == "strong"
    assert body.find(".//code").text == "code"
    assert any(a.get("href") == "http://x.org" for a in body.iter("a"))
    assert body.find("ul/li/ul/li") is not None and body.find("ol") is not None
    assert body.find("blockquote") is not None
    assert body.find("pre/code").text == "x = 1 < 2"
    assert [c.text for c in body.iter("td")] == ["1", "2"]
    assert body.find("hr") is not None
    assert any(a.get("href") == "http://ref.example" for a in body.iter("a"))


def test_markdown_round_trip(book, tmp_path):
    text, _images = markdown.book_to_markdown(book)
    assert text.startswith("---\ntitle: The Sample Book")
    assert "# Chapter One" in text and "**bright**" in text and "*April*" in text
    assert "| Name | Age |" in text and "- first item" in text
    back = markdown.book_from_markdown(text)
    assert back.metadata.authors == ["Ada Writer", "Bo Second"]


def test_txt_heuristics():
    text = ("Title: A Test\nAuthor: Some One\n\n\n\nCHAPTER I.\n\n\nIt was a dark night and the wind was\nhowling through "
            "the trees as it always did\nin that season of the year.\n\nA second paragraph.\n\n\nThe Next Part\n\n"
            "Roses are red,\nviolets blue,\nshort lines\nlike verse.\n")
    b = txt.book_from_text(text, "x")
    assert b.metadata.title == "A Test" and b.metadata.authors == ["Some One"]
    titles = [e.title for _d, e in b.toc_walk()]
    assert "CHAPTER I." in titles and "The Next Part" in titles
    root = b.tree(b.spine[-1])
    paras = [markup.text_of(p) for p in root.iter("p")]
    assert any(p.startswith("It was a dark night and the wind was howling") for p in paras)
    verse = next(p for p in root.iter("p") if "Roses" in markup.text_of(p))
    assert len(verse.findall("br")) == 3


def test_markdown_keeps_paragraphs_that_look_like_lists():
    import bookport
    b = bookport.Book()
    b.add_document("text/a.xhtml", markup.parse(
        "<html><body><p>1. In 1788 the King's advisers warned him.</p><p>2) Second.</p><p>- not a bullet</p>"
        "<p>+ plus</p><p># not a heading</p><p>=</p><p>3.14 is pi</p></body></html>"))
    text, _images = markdown.book_to_markdown(b)
    back = markdown.book_from_markdown(text).text()
    for s in ("1. In 1788", "2) Second.", "- not a bullet", "+ plus", "# not a heading", "3.14 is pi"):
        assert s in back, (s, text)
    assert "<ol" not in markdown.to_html(text) and "<ul" not in markdown.to_html(text)


def test_markdown_table_keeps_caption_and_spans():
    import bookport
    b = bookport.Book()
    b.add_document("text/a.xhtml", markup.parse(
        "<html><body><table><caption>Cut &amp; Comeagain, Tailors.<br/>To Peter Proffit</caption>"
        "<tr><td colspan='2'></td><th>Drs.</th></tr><tr><td>July 10.</td><td>To promenade</td><td>25</td></tr>"
        "</table></body></html>"))
    text, _images = markdown.book_to_markdown(b)
    back = markdown.book_from_markdown(text).text()
    assert "Cut & Comeagain, Tailors." in back and "To Peter Proffit" in back
    assert "|  |  | Drs. |" in text and "| July 10. | To promenade | 25 |" in text


def test_markdown_detection():
    assert txt.looks_like_markdown("# Title\n\nSee [x](y) and [z](w).\n\n## Two\n\n```\ncode\n```\n")
    assert not txt.looks_like_markdown("*** START OF THE BOOK ***\n\n* * *\n\nPlain text with *stars*.\n" * 20)


def test_new_document_helpers():
    root, body = markup.new_document("T", "fr", css_hrefs=["a.css"])
    ET.SubElement(body, "p").text = "x"
    out = markup.serialize(root).decode()
    assert 'lang="fr"' in out and '<link rel="stylesheet" type="text/css" href="a.css"/>' in out


def test_supports_without_space_and_offscreen_text():
    # Standard Ebooks: hidden for one old reader, shown again in @supports(...) and moved off-screen
    root = markup.parse('<html><body><section class="t"><h1 id="top">Meditations</h1><p>By Marcus Aurelius.</p>'
                        '<img src="t.png" alt=""/></section><p>Body text.</p></body></html>')
    sheet = (".t h1, .t p { display: none } @supports(font-size: 0){ .t h1, .t p { display: block } }"
             ".t h1, .t p { position: absolute; left: -999em }")
    blocks = flow.document_blocks(None, "d.xhtml", root, extra_css=sheet)
    assert [b.text() for b in blocks if b.offscreen] == ["Meditations", "By Marcus Aurelius."]
    shown = flow.visible(blocks)
    assert "Meditations" not in [b.text() for b in shown] and shown[0].anchors[:1] == ["d.xhtml#top"]


def test_picture_with_anchor_is_an_image_block(book):
    href = next(iter(book.images())).href
    root = markup.parse(f'<html><body><p><a id="x"></a><img src="../{href}" alt="A picture"/></p></body></html>')
    blocks = flow.document_blocks(book, "text/p.xhtml", root)
    assert blocks[0].kind == "image" and blocks[0].alt == "A picture" and "text/p.xhtml#x" in blocks[0].anchors
