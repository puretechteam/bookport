"""Math: TeX, Typst and Office Math (OMML) to MathML and back, and math in Markdown, HTML, EPUB and DOCX."""

import io
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

import pytest

import bookport
from bookport import markup, mathml
from bookport.fmt.markdown import book_from_markdown

M = mathml.M


# --- a presentation tree to compare (mrows are grouping only; attributes besides mathvariant and
# linethickness don't change what is shown; OMML can't tell <mtext class="raw"> from <mtext>) -----

def _norm_list(kids):
    out = []
    for c in kids:
        t = c.tag
        if t in ("annotation", "annotation-xml"):
            continue
        if t == "mtable":
            rows = list(c)
            if len(rows) == 1 and len(rows[0]) == 1:
                out += _norm_list(list(rows[0][0]))
                continue
        if t in ("mrow", "mstyle", "mpadded", "semantics"):
            out += _norm_list(list(c)[:1] if t == "semantics" else list(c))
            continue
        out.append(_norm(c))
    return out


def _arg(c):
    items = _norm_list([c])
    return items[0] if len(items) == 1 else ("mrow", *items)


def _norm(el):
    t = el.tag
    if t == "math":
        return ("math", *_norm_list(mathml.presentation(el)))
    if t in ("mi", "mn", "mo", "mtext"):
        text = unicodedata.normalize("NFC", el.text or "")
        mv = el.get("mathvariant", "")
        if t == "mi" and mv == "normal" and len(text) > 1:
            mv = ""
        return (t, text, mv)
    if t == "mspace":
        return ("mspace", el.get("width", ""))
    if t == "mfrac":
        return ("mfrac", el.get("linethickness", ""), *(_arg(k) for k in el))
    if t in ("msqrt", "mphantom", "mtd", "menclose"):
        return (t, *_norm_list(list(el)))
    if t in ("mtable", "mtr"):
        return (t, *(_norm(k) for k in el))
    return (t, *(_arg(k) for k in el))


def _bare(math):
    """The MathML without its annotations, so to_tex/to_typst write from the presentation."""
    m = ET.fromstring(ET.tostring(math))
    for sem in m.iter("semantics"):
        for a in [k for k in sem if k.tag == "annotation"]:
            sem.remove(a)
    return m


def tags(math):
    return [e.tag for e in math.iter()]


FORMULAS = [
    r"x^2 + y_i", r"\frac{a}{b} + \dfrac{1}{2} - \tfrac12", r"\sqrt{x} \sqrt[3]{y}", r"\sum_{i=1}^n a_i",
    r"\int_0^1 f(x)\,dx", r"\lim_{n \to \infty} \frac{1}{n} = 0", r"\left( \frac{a}{b} \right)^2",
    r"\left\langle x \middle| y \right\rangle", r"\binom{n}{k}", r"\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}",
    r"\begin{bmatrix} a \\ b \end{bmatrix}", r"|x| = \begin{cases} x & x \ge 0 \\ -x & \text{otherwise} \end{cases}",
    r"\begin{aligned} a &= b + c \\ &= d \end{aligned}", r"\hat{x} \tilde{y} \vec{v} \bar{z} \dot{a} \overline{AB}",
    r"\mathbb{R}^n \mathcal{L} \mathfrak{g} \mathbf{x} \boldsymbol{\alpha} \mathrm{d}t \mathrm{const}",
    r"\alpha \beta \Gamma \varepsilon \phi \varphi \infty \partial \nabla", r"a \le b \ne c \in A \subseteq B \to C",
    r"\sin x + \log_2 y + \operatorname{argmax}_x f", r"f'(x) + g''(t) + \theta_0'", r"\underbrace{a + b}_{n}",
    r"\overbrace{x y}^{k}", r"\text{if } x > 0", r"\not= \not\in", r"\boxed{E = mc^2}", r"{a \over b} {n \choose k}",
    r"x_{i,j}^{(k)}", r"\{1, 2, 3\}", r"\| v \|_2", r"\mathbf{2110 \pm 50}", r"1.5 \times 10^{-8}",
    r"\begin{array}{lcr} a & b & c \\ d & e & f \end{array}", r"\xrightarrow{f} \overset{a}{=} \underset{b}{\sim}",
    r"\cancel{x} \phantom{y}", r"P(A \mid B) = \frac{P(B \mid A) P(A)}{P(B)}",
]


def test_from_tex_structure():
    m = mathml.from_tex(r"x^2", False)
    assert m.tag == "math" and m.get("display") == "inline"
    assert mathml.annotation(m) == "x^2"
    assert tags(m)[:4] == ["math", "semantics", "msup", "mi"]
    # big operators take their limits above and below in display only
    assert "munderover" in tags(mathml.from_tex(r"\sum_{i=1}^n i", True))
    assert "msubsup" in tags(mathml.from_tex(r"\sum_{i=1}^n i", False))
    assert "msubsup" in tags(mathml.from_tex(r"\int_0^1 f", True))  # integrals keep theirs at the side
    assert "munder" in tags(mathml.from_tex(r"\lim_{x\to 0} f", True))
    frac = mathml.from_tex(r"\binom{n}{k}").find(".//mfrac")
    assert frac.get("linethickness") == "0"
    table = mathml.from_tex(r"\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}")
    assert len(table.findall(".//mtr")) == 2 and len(table.findall(".//mtd")) == 4
    fences = [o.text for o in table.iter("mo") if o.get("stretchy") == "true"]
    assert fences == ["(", ")"]
    bb = mathml.from_tex(r"\mathbb{R}").find(".//mi")
    assert bb.text == "ℝ" and bb.get("mathvariant") == "double-struck"
    sin = mathml.from_tex(r"\sin x").find(".//mrow")[0]
    assert [c.text for c in sin] == ["sin", mathml.APPLY]
    assert mathml.from_tex(r"\not\in").find(".//mo").text == "∉"
    assert mathml.from_tex(r"a - b").findall(".//mo")[0].text == "−"


def test_unknown_commands_kept_raw():
    m = mathml.from_tex(r"\mbf{x} + \foo")
    raw = [e.text for e in m.iter("mtext") if e.get("class") == "raw"]
    assert raw == ["\\mbf", "\\foo"]
    assert m.find(".//mi").text == "x"  # its argument is still math


def test_macros_expand():
    m = mathml.from_tex(r"\R^n \norm{v}", macros={"\\R": (0, r"\mathbb{R}"), "\\norm": (1, r"\left\| #1 \right\|")})
    assert m.find(".//mi").text == "ℝ"
    assert [o.text for o in m.iter("mo") if o.get("stretchy") == "true"] == ["‖", "‖"]


@pytest.mark.parametrize("tex", FORMULAS)
@pytest.mark.parametrize("display", [False, True])
def test_round_trips(tex, display):
    m = mathml.from_tex(tex, display)
    ref = _norm(m)
    assert not any(e.get("class") == "raw" for e in m.iter("mtext")), tex
    # MathML -> TeX -> MathML
    back = mathml.to_tex(_bare(m))
    assert _norm(mathml.from_tex(back, display)) == ref, back
    # MathML -> OMML -> MathML (through serialized XML, as in a .docx)
    om = ET.fromstring(ET.tostring(mathml.to_omml(_bare(m))))
    assert om.tag == f"{{{M}}}{'oMathPara' if display else 'oMath'}"
    assert _norm(mathml.from_omml(om)) == ref
    # MathML -> Typst -> MathML (Typst math has no box: \boxed keeps its contents only)
    typ = mathml.to_typst(_bare(m))
    if "boxed" not in tex:
        assert _norm(mathml.from_typst(typ, display)) == ref, typ


def test_to_tex_prefers_annotation():
    m = mathml.from_tex(r"\frac  {a}{b}")
    assert mathml.to_tex(m) == r"\frac  {a}{b}"
    assert mathml.to_tex(_bare(m)) == r"\frac{a}{b}"


def test_mathml_from_html_to_tex():
    html = ('<math><mrow><mi>x</mi><mo>=</mo><mfrac><mrow><mo>−</mo><mi>b</mi><mo>±</mo><msqrt><msup><mi>b</mi><mn>2</mn>'
            '</msup><mo>−</mo><mn>4</mn><mi>a</mi><mi>c</mi></msqrt></mrow><mrow><mn>2</mn><mi>a</mi></mrow></mfrac></mrow></math>')
    tex = mathml.to_tex(ET.fromstring(html))
    assert tex == r"x = \frac{- b \pm \sqrt{b^2 - 4 a c}}{2 a}"


def test_from_omml_word_constructs():
    om = ET.fromstring(
        f'<m:oMathPara xmlns:m="{M}"><m:oMath>'
        '<m:func><m:fName><m:r><m:rPr><m:sty m:val="p"/></m:rPr><m:t>sin</m:t></m:r></m:fName><m:e><m:r><m:t>x</m:t></m:r></m:e></m:func>'
        '<m:nary><m:sub><m:r><m:t>0</m:t></m:r></m:sub><m:sup><m:r><m:t>1</m:t></m:r></m:sup><m:e><m:r><m:t>f</m:t></m:r></m:e></m:nary>'
        '<m:d><m:e><m:r><m:t>a+b</m:t></m:r></m:e></m:d>'
        '<m:eqArr><m:e><m:r><m:t>x&amp;=1</m:t></m:r></m:e><m:e><m:r><m:t>y&amp;=2</m:t></m:r></m:e></m:eqArr>'
        '</m:oMath></m:oMathPara>')
    m = mathml.from_omml(om)
    assert m.get("display") == "block"
    assert mathml.annotation(m) == r"\sin x\int_0^1 f\left(a + b\right)\begin{aligned} x & = 1 \\ y & = 2 \end{aligned}"
    nary = m.find(".//msubsup")
    assert nary[0].text == "∫"  # m:nary without m:chr is an integral


def test_typst_math():
    m = mathml.from_typst("sum_(i=1)^n x_i / 2", True)
    assert tags(m)[2] == "mrow" and m.find(".//munderover") is not None and m.find(".//mfrac") is not None
    m = mathml.from_typst("mat(1, 2; 3, 4) vec(a, b) cases(x & x > 0, 0 & \"else\")")
    assert len(m.findall(".//mtable")) == 3
    assert m.findall(".//mtable")[0].find("mtr") is not None and len(m.findall(".//mtable")[0]) == 2
    assert mathml.from_typst("RR").find(".//mi").text == "ℝ"
    assert mathml.from_typst('"text"').find(".//mtext").text == "text"
    assert mathml.to_typst(mathml.from_tex(r"\frac{a}{b} + \alpha_i")) == "frac(a, b) + alpha_i"
    assert mathml.to_typst(mathml.from_typst("x^2 + 1")) == "x^2 + 1"  # the source when it has one


# --- hostile input ----------------------------------------------------------------------------------

@pytest.mark.parametrize("tex", ["{" * 5000 + "x" + "}" * 5000, r"\frac" * 3000 + "ab", r"\sqrt" * 4000 + "x",
                                 r"\left(" * 3000 + "x", "x^" * 5000, r"\begin{matrix}" * 2000, "{" * 50000,
                                 r"\text{" * 3000, "\\" * 10000, "$" * 1000 + "&" * 1000], ids=[str(k) for k in range(10)])
def test_hostile_tex(tex):
    t = time.perf_counter()
    m = mathml.from_tex(tex, True)
    assert time.perf_counter() - t < 5
    ET.tostring(m)  # serializes: no runaway depth
    mathml.to_tex(_bare(m))
    ET.tostring(mathml.to_omml(_bare(m)))
    mathml.to_typst(_bare(m))


def test_hostile_macros():
    t = time.perf_counter()
    m = mathml.from_tex(r"\a", macros={"\\a": (0, r"\a\a")})  # doubles itself forever
    assert m.find(".//mtext").get("class") == "raw"
    m = mathml.from_tex(r"\b", macros={"\\b": (0, r"\b x")})
    assert m.find(".//mtext").get("class") == "raw"
    assert time.perf_counter() - t < 5


def test_hostile_long_and_deep():
    m = mathml.from_tex("x" * (mathml.MAX_LEN + 10))
    assert m.find(".//mtext").get("class") == "raw"
    for src in ["(" * 5000 + "x", "frac(" * 3000, "x_" * 5000, '"' * 3001, "#(" * 3000]:
        m = mathml.from_typst(src, True)
        ET.tostring(m)
    deep = ET.Element("math")
    cur = deep
    for _ in range(3000):
        cur = ET.SubElement(cur, "mrow")
    ET.SubElement(cur, "mi").text = "x"
    mathml.to_tex(deep)
    mathml.to_typst(deep)
    om = mathml.to_omml(deep)
    mathml.from_omml(om)


# --- math in the formats -------------------------------------------------------------------------

def _maths(book):
    return [m for href, _ in book.documents() for m in book.tree(href).iter("math")]


def test_markdown_reads_dollar_math():
    book = book_from_markdown("Inline $x^2$ and $$\\sum_i a_i$$ here.\n\nPrices $5 and $10 stay text. So do $ x $, "
                              "\\$y\\$ and `$z$`.\n\n$$\n\\frac{a}{b}\n$$\n", "m")
    maths = _maths(book)
    assert [m.get("display") for m in maths] == ["inline", "block", "block"]
    assert [mathml.annotation(m) for m in maths] == ["x^2", "\\sum_i a_i", "\\frac{a}{b}"]
    text = markup.text_of(book.tree(book.spine[0]).find("body"))
    assert "$5 and $10" in text and "$ x $" in text and "$y$" in text and "$z$" in text


def test_markdown_writes_math(tmp_path):
    src = "# T\n\nInline $x_1^2$ costs \\$5.\n\n$$\\int_0^1 f$$\n"
    out = tmp_path / "o.md"
    book_from_markdown(src, "m").write(out)
    text = out.read_text(encoding="utf-8")
    assert "$x_1^2$" in text and "$$\\int_0^1 f$$" in text and "\\$5" in text
    back = bookport.read(out)
    assert [mathml.annotation(m) for m in _maths(back)] == ["x_1^2", "\\int_0^1 f"]


def test_html_mathml_survives(tmp_path):
    src = tmp_path / "a.html"
    src.write_text('<html><body><p>See <math><msup><mi>x</mi><mn>2</mn></msup></math>.</p>'
                   '<p><math display="block"><mfrac><mi>a</mi><mi>b</mi></mfrac></math></p></body></html>', encoding="utf-8")
    book = bookport.read(src)
    assert len(_maths(book)) == 2
    out = tmp_path / "b.html"
    book.write(out)
    text = out.read_text(encoding="utf-8")
    assert text.count('xmlns="http://www.w3.org/1998/Math/MathML"') == 2
    back = bookport.read(out)
    assert [tags(m) for m in _maths(back)] == [tags(m) for m in _maths(book)]


def test_epub_declares_mathml(tmp_path):
    book = book_from_markdown("# T\n\nMath $x^2$ here.\n", "m")
    out = tmp_path / "m.epub"
    book.write(out)
    with zipfile.ZipFile(out) as z:
        opf = next(z.read(n).decode() for n in z.namelist() if n.endswith(".opf"))
    assert re.search(r'<item [^>]*properties="[^"]*mathml', opf)
    assert len(_maths(bookport.read(out))) == 1


def test_docx_writes_and_reads_omml(tmp_path):
    book = book_from_markdown("# T\n\nInline $\\alpha^2 + \\frac{1}{2}$ here.\n\n$$\\sum_{i=1}^n i = \\frac{n(n+1)}{2}$$\n", "m")
    out = tmp_path / "m.docx"
    book.write(out)
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode()
    root = ET.fromstring(doc)
    assert len(root.findall(f".//{{{M}}}oMathPara")) == 1
    assert len(root.findall(f".//{{{M}}}oMath")) == 2
    assert root.find(f".//{{{M}}}nary") is not None and root.find(f".//{{{M}}}f") is not None
    back = bookport.read(out)
    maths = _maths(back)
    assert [m.get("display") for m in maths] == ["inline", "block"]
    assert [_norm(m) for m in maths] == [_norm(m) for m in _maths(book)]


def test_docx_reads_word_math(tmp_path):
    body = (f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:m="{M}"><w:body>'
            '<w:p><w:r><w:t xml:space="preserve">Area </w:t></w:r><m:oMath><m:r><m:t>π</m:t></m:r><m:sSup><m:e><m:r><m:t>r</m:t>'
            '</m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup></m:oMath></w:p>'
            '<w:p><m:oMathPara><m:oMath><m:f><m:num><m:r><m:t>a</m:t></m:r></m:num><m:den><m:r><m:t>b</m:t></m:r></m:den></m:f>'
            '</m:oMath></m:oMathPara></w:p></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", body)
    p = tmp_path / "w.docx"
    p.write_bytes(buf.getvalue())
    maths = _maths(bookport.read(p))
    assert [mathml.annotation(m) for m in maths] == ["\\pi r^2", "\\frac{a}{b}"]
    assert [m.get("display") for m in maths] == ["inline", "block"]


def test_flow_gives_math_as_tex(tmp_path):
    book = book_from_markdown("Inline $x^2$ here.\n", "m")
    out = tmp_path / "m.txt"
    book.write(out)
    text = out.read_text(encoding="utf-8-sig")
    assert "x^2" in text and "x2x^2" not in text


def test_size_switches_and_small_caps():
    m = mathml.from_tex(r"a {\small b + c} \textsc{Name} d", True)
    st = m.find(".//mstyle")
    assert st.get("mathsize") == "90%" and [e.text for e in st] == ["b", "+", "c"]
    sc = [e for e in m.iter("mtext") if e.get("style") == "font-variant: small-caps"]
    assert [e.text for e in sc] == ["Name"]
    assert not any(e.get("class") == "raw" for e in m.iter("mtext"))
    back = mathml.to_tex(_bare(m))
    assert r"{\small b + c}" in back and r"\textsc{Name}" in back
    assert _norm(mathml.from_tex(back, True)) == _norm(m)
    assert mathml.from_tex(r"\Huge x").find(".//mstyle").get("mathsize") == "249%"
