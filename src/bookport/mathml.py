"""Math: TeX and Typst math to MathML and back, and MathML to Word's OMML and back.

A book carries every equation as MathML (``<math display="inline|block">``), wrapped in
``<semantics>`` with the presentation markup first and, when it came from TeX, an
``<annotation encoding="application/x-tex">`` with the original, so a TeX writer gives back
exactly what was read.

API (all take and return ``xml.etree.ElementTree`` elements with plain local names):

- ``from_tex(tex, display=False) -> Element``
- ``to_tex(math) -> str`` (no ``$`` delimiters)
- ``from_typst(src, display=False) -> Element`` / ``to_typst(math) -> str``
- ``to_omml(math) -> Element`` (``m:oMath``, or ``m:oMathPara`` for display) / ``from_omml(el) -> Element``

The presentation markup is what browsers and EPUB readers render (MathML Core): letters in
``mi``, numbers in ``mn``, operators in ``mo``, styled letters as Unicode mathematical
alphanumerics plus ``mathvariant``, ``\\sin`` as ``<mrow><mi>sin</mi><mo>&#x2061;</mo></mrow>``,
``\\left( .. \\right)`` as an ``mrow`` between stretchy ``mo`` fences. Unknown TeX commands stay
as written in ``<mtext class="raw">``. Hostile input is bounded: nesting, length and macro
expansion are capped, and a formula past them is kept as text.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET

TEX = "application/x-tex"
TYPST = "application/x-typst"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
APPLY = chr(0x2061)  # function application, invisible
NBSP = chr(0xA0)
MAX_LEN = 100_000  # characters of TeX/Typst per formula
MAX_NEST = 60  # groups, arguments and environments inside each other
MAX_EXPAND = 2_000  # macro expansions per formula
MAX_TOKENS = 200_000


class _TooComplex(Exception):
    pass


def _el(tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    e = ET.Element(tag, {k.replace("_", "-"): v for k, v in attrs.items()})
    if text is not None:
        e.text = text
    return e


def _row(items: list[ET.Element]) -> ET.Element:
    """One element for a list: itself when single, else an mrow."""
    if len(items) == 1:
        return items[0]
    r = ET.Element("mrow")
    r.extend(items)
    return r


def _wrap(items: list[ET.Element], source: str, encoding: str, display: bool) -> ET.Element:
    math = ET.Element("math", {"display": "block" if display else "inline"})
    sem = ET.SubElement(math, "semantics")
    sem.append(_row(items) if items else _el("mrow"))
    ann = ET.SubElement(sem, "annotation", {"encoding": encoding})
    ann.text = source
    return math


def annotation(math: ET.Element, encoding: str = TEX) -> str | None:
    for el in math.iter():
        if _local(el.tag) == "annotation" and el.get("encoding") == encoding and el.text is not None:
            return el.text
    return None


def presentation(math: ET.Element) -> list[ET.Element]:
    """The presentation children of a <math> (inside <semantics> when there is one)."""
    kids = _kids(math)
    if len(kids) == 1 and _local(kids[0].tag) == "semantics":
        first = next((c for c in _kids(kids[0]) if _local(c.tag) not in ("annotation", "annotation-xml")), None)
        return [first] if first is not None else []
    return [c for c in kids if _local(c.tag) not in ("annotation", "annotation-xml")]


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _whole(value) -> int | None:
    """A whole number in ASCII digits (up to 9), or None ("²" and 5,000 digits both make int() fail)."""
    s = value or ""
    return int(s) if s.isascii() and s.isdigit() and len(s) < 10 else None


def _kids(el: ET.Element) -> list[ET.Element]:
    return [c for c in el if isinstance(c.tag, str)]


# --- styled letters (mathvariant <-> Unicode mathematical alphanumerics) ---------------------------

_LATIN = {"bold": 0x1D400, "italic": 0x1D434, "bold-italic": 0x1D468, "script": 0x1D49C, "bold-script": 0x1D4D0,
          "fraktur": 0x1D504, "double-struck": 0x1D538, "bold-fraktur": 0x1D56C, "sans-serif": 0x1D5A0,
          "bold-sans-serif": 0x1D5D4, "sans-serif-italic": 0x1D608, "sans-serif-bold-italic": 0x1D63C,
          "monospace": 0x1D670}
_DIGITS = {"bold": 0x1D7CE, "double-struck": 0x1D7D8, "sans-serif": 0x1D7E2, "bold-sans-serif": 0x1D7EC,
           "monospace": 0x1D7F6}
_GREEK = {"bold": 0x1D6A8, "italic": 0x1D6E2, "bold-italic": 0x1D71C, "bold-sans-serif": 0x1D756,
          "sans-serif-bold-italic": 0x1D790}
_HOLES = {0x1D455: 0x210E, 0x1D49D: 0x212C, 0x1D4A0: 0x2130, 0x1D4A1: 0x2131, 0x1D4A3: 0x210B, 0x1D4A4: 0x2110,
          0x1D4A7: 0x2112, 0x1D4A8: 0x2133, 0x1D4AD: 0x211B, 0x1D4BA: 0x212F, 0x1D4BC: 0x210A, 0x1D4C4: 0x2134,
          0x1D506: 0x212D, 0x1D50B: 0x210C, 0x1D50C: 0x2111, 0x1D515: 0x211C, 0x1D51D: 0x2128, 0x1D53A: 0x2102,
          0x1D53F: 0x210D, 0x1D545: 0x2115, 0x1D547: 0x2119, 0x1D548: 0x211A, 0x1D549: 0x211D, 0x1D551: 0x2124}
_GREEK_ORDER = ([chr(c) if c != 0x3A2 else chr(0x3F4) for c in range(0x391, 0x3AA)] + [chr(0x2207)]
                + [chr(c) for c in range(0x3B1, 0x3CA)] + [chr(c) for c in (0x2202, 0x3F5, 0x3D1, 0x3F0, 0x3D5, 0x3F1, 0x3D6)])
_STYLED: dict[tuple[str, str], str] = {}
_PLAIN: dict[str, tuple[str, str]] = {}


def _build_styles() -> None:
    letters = [chr(c) for c in range(65, 91)] + [chr(c) for c in range(97, 123)]
    for variant, start in _LATIN.items():
        for k, ch in enumerate(letters):
            _STYLED[(variant, ch)] = chr(_HOLES.get(start + k, start + k))
    for variant, start in _DIGITS.items():
        for k in range(10):
            _STYLED[(variant, str(k))] = chr(start + k)
    for variant, start in _GREEK.items():
        for k, ch in enumerate(_GREEK_ORDER):
            _STYLED[(variant, ch)] = chr(start + k)
    for (variant, ch), s in _STYLED.items():
        _PLAIN.setdefault(s, (ch, variant))


_build_styles()


def styled(text: str, variant: str) -> str:
    return "".join(_STYLED.get((variant, ch), ch) for ch in text)


def plain(text: str) -> tuple[str, str]:
    """(text with styled letters made plain, their variant or "")."""
    out, variant = [], ""
    for ch in text:
        got = _PLAIN.get(ch)
        if got:
            out.append(got[0])
            variant = variant or got[1]
        else:
            out.append(ch)
    return "".join(out), variant


# --- TeX tables ------------------------------------------------------------------------------------

def _table(spec: str) -> dict[str, str]:
    items = spec.split()
    return {items[k]: items[k + 1] for k in range(0, len(items) - 1, 2)}


# identifiers (mi)
TEX_MI = _table("""
alpha α beta β gamma γ delta δ epsilon ϵ varepsilon ε zeta ζ eta η theta θ vartheta ϑ iota ι kappa κ varkappa ϰ
lambda λ mu μ nu ν xi ξ omicron ο pi π varpi ϖ rho ρ varrho ϱ sigma σ varsigma ς tau τ upsilon υ phi ϕ varphi φ
chi χ psi ψ omega ω digamma ϝ
infty ∞ partial ∂ nabla ∇ emptyset ∅ varnothing ∅ hbar ℏ hslash ℏ ell ℓ wp ℘ Re ℜ Im ℑ aleph ℵ beth ℶ gimel ℷ
daleth ℸ imath ı jmath ȷ top ⊤ bot ⊥ angle ∠ measuredangle ∡ sphericalangle ∢ triangle △ ldots … dots … cdots ⋯
vdots ⋮ ddots ⋱ dotsc … dotso … dotsb ⋯ dotsi ⋯ dotsm ⋯ Box □ square □ blacksquare ■ Diamond ◇ lozenge ◊
blacklozenge ⧫ diamondsuit ♢ heartsuit ♡ clubsuit ♣ spadesuit ♠ flat ♭ natural ♮ sharp ♯ checkmark ✓ surd √ mho ℧
eth ð complement ∁ Finv Ⅎ Game ⅁ circledS Ⓢ bigstar ★ degree ° blacktriangle ▲
blacktriangledown ▼ triangledown ▽ varclubsuit ♧ maltese ✠ yen ¥ pounds £ copyright © S § P ¶
""")
TEX_UPPER_GREEK = _table("Gamma Γ Delta Δ Theta Θ Lambda Λ Xi Ξ Pi Π Sigma Σ Upsilon Υ Phi Φ Psi Ψ Omega Ω")
TEX_VAR_GREEK = {"var" + k: v for k, v in TEX_UPPER_GREEK.items()}  # \varGamma: italic capitals

# operators, relations, arrows, fences (mo)
TEX_MO = _table("""
pm ± mp ∓ times × div ÷ cdot ⋅ ast ∗ star ⋆ circ ∘ bullet ∙ oplus ⊕ ominus ⊖ otimes ⊗ oslash ⊘ odot ⊙ cap ∩ cup ∪
sqcap ⊓ sqcup ⊔ vee ∨ lor ∨ wedge ∧ land ∧ setminus ∖ smallsetminus ∖ wr ≀ diamond ⋄ bigtriangleup △
bigtriangledown ▽ triangleleft ◃ triangleright ▹ lhd ⊲ rhd ⊳ unlhd ⊴ unrhd ⊵ uplus ⊎ amalg ⨿ dagger † ddagger ‡
ltimes ⋉ rtimes ⋊ leftthreetimes ⋋ rightthreetimes ⋌ boxplus ⊞ boxminus ⊟ boxtimes ⊠ boxdot ⊡ dotplus ∔
divideontimes ⋇ cdotp ⋅ centerdot ⋅ intercal ⊺ barwedge ⌅ veebar ⊻ curlywedge ⋏ curlyvee ⋎ circledast ⊛
circledcirc ⊚ circleddash ⊝ Cap ⋒ Cup ⋓ doublebarwedge ⩞ dag † ddag ‡ prime ′ backprime ‵ forall ∀ exists ∃ nexists ∄ neg ¬ lnot ¬
le ≤ leq ≤ ge ≥ geq ≥ neq ≠ ne ≠ equiv ≡ approx ≈ approxeq ≊ sim ∼ simeq ≃ cong ≅ propto ∝ varpropto ∝ ll ≪ gg ≫
lll ⋘ ggg ⋙ subset ⊂ supset ⊃ subseteq ⊆ supseteq ⊇ subsetneq ⊊ supsetneq ⊋ subseteqq ⫅ supseteqq ⫆ sqsubset ⊏
sqsupset ⊐ sqsubseteq ⊑ sqsupseteq ⊒ in ∈ ni ∋ owns ∋ notin ∉ perp ⟂ parallel ∥ nparallel ∦ mid ∣ nmid ∤
vdash ⊢ dashv ⊣ models ⊨ vDash ⊨ Vdash ⊩ Vvdash ⊪ nvdash ⊬ nvDash ⊭ prec ≺ succ ≻ preceq ⪯ succeq ⪰
precsim ≾ succsim ≿ asymp ≍ doteq ≐ doteqdot ≑ bowtie ⋈ Join ⋈ smile ⌣ frown ⌢ leqslant ⩽ geqslant ⩾
lesssim ≲ gtrsim ≳ lessgtr ≶ gtrless ≷ lesseqgtr ⋚ gtreqless ⋛ coloneqq ≔ coloneq ≔ eqqcolon ≕ eqcolon ≕
triangleq ≜ eqcirc ≖ circeq ≗ nleq ≰ ngeq ≱ nless ≮ ngtr ≯ nsim ≁ ncong ≇ nsubseteq ⊈ nsupseteq ⊉
therefore ∴ because ∵ lessdot ⋖ gtrdot ⋗ Subset ⋐ Supset ⋑ vartriangleleft ⊲ vartriangleright ⊳
trianglelefteq ⊴ trianglerighteq ⊵ ntriangleleft ⋪ ntriangleright ⋫ nprec ⊀ nsucc ⊁ between ≬ pitchfork ⋔
backsim ∽ backsimeq ⋍ risingdotseq ≓ fallingdotseq ≒ thicksim ∼ thickapprox ≈ lneq ⪇ gneq ⪈ lneqq ≨ gneqq ≩
leqq ≦ geqq ≧ eqslantless ⪕ eqslantgtr ⪖ shortmid ∣ shortparallel ∥ multimap ⊸ dotdiv ∸ divides ∣
to → rightarrow → leftarrow ← gets ← leftrightarrow ↔ Rightarrow ⇒ Leftarrow ⇐ Leftrightarrow ⇔ implies ⟹
impliedby ⟸ iff ⟺ longrightarrow ⟶ longleftarrow ⟵ longleftrightarrow ⟷ Longrightarrow ⟹ Longleftarrow ⟸
Longleftrightarrow ⟺ mapsto ↦ longmapsto ⟼ uparrow ↑ downarrow ↓ updownarrow ↕ Uparrow ⇑ Downarrow ⇓
Updownarrow ⇕ nearrow ↗ searrow ↘ swarrow ↙ nwarrow ↖ hookrightarrow ↪ hookleftarrow ↩ rightharpoonup ⇀
rightharpoondown ⇁ leftharpoonup ↼ leftharpoondown ↽ rightleftharpoons ⇌ leftrightharpoons ⇋ leadsto ⇝
rightsquigarrow ⇝ leftrightsquigarrow ↭ twoheadrightarrow ↠ twoheadleftarrow ↞ rightrightarrows ⇉
leftleftarrows ⇇ rightleftarrows ⇄ leftrightarrows ⇆ curvearrowright ↷ curvearrowleft ↶ circlearrowright ↻
circlearrowleft ↺ Lsh ↰ Rsh ↱ upuparrows ⇈ downdownarrows ⇊ nrightarrow ↛ nleftarrow ↚ nRightarrow ⇏
nLeftarrow ⇍ nleftrightarrow ↮ nLeftrightarrow ⇎ restriction ↾ upharpoonright ↾ upharpoonleft ↿
downharpoonright ⇂ downharpoonleft ⇃ rightarrowtail ↣ leftarrowtail ↢ looparrowright ↬ looparrowleft ↫
Rrightarrow ⇛ Lleftarrow ⇚ dashrightarrow ⇢ dashleftarrow ⇠ colon : ldotp . vert | Vert ‖ backslash \\
lbrace { rbrace } lbrack [ rbrack ] langle ⟨ rangle ⟩ lceil ⌈ rceil ⌉ lfloor ⌊ rfloor ⌋ lvert | rvert |
lVert ‖ rVert ‖ lgroup ⟮ rgroup ⟯ ulcorner ⌜ urcorner ⌝ llcorner ⌞ lrcorner ⌟ llbracket ⟦ rrbracket ⟧
""")
TEX_MO.update({"{": "{", "}": "}", "|": "‖", "#": "#", "%": "%", "&": "&", "$": "$", "_": "_"})

TEX_BIG = _table("""
sum ∑ prod ∏ coprod ∐ bigcup ⋃ bigcap ⋂ bigsqcup ⨆ bigvee ⋁ bigwedge ⋀ bigoplus ⨁ bigotimes ⨂ bigodot ⨀
biguplus ⨄ int ∫ iint ∬ iiint ∭ iiiint ⨌ oint ∮ oiint ∯ oiiint ∰ intop ∫ smallint ∫
""")
INTEGRALS = set("∫∬∭⨌∮∯∰")  # scripts at the side even in display
BIG_CHARS = set(TEX_BIG.values())

FUNCTIONS = set("""arccos arcsin arctan arg cos cosh cot coth csc deg det dim exp gcd hom inf ker lg lim liminf limsup ln
log max min Pr sec sin sinh sup tan tanh injlim projlim""".split())
LIMIT_FUNCTIONS = {"det", "gcd", "inf", "lim", "liminf", "limsup", "max", "min", "Pr", "sup", "injlim", "projlim"}

SPACES = {",": "0.167em", "thinspace": "0.167em", ":": "0.222em", ">": "0.222em", "medspace": "0.222em",
          ";": "0.278em", "thickspace": "0.278em", "!": "-0.167em", "negthinspace": "-0.167em",
          "negmedspace": "-0.222em", "negthickspace": "-0.278em", "quad": "1em", "qquad": "2em", " ": "0.333em",
          "enspace": "0.5em", "space": "0.333em", "nobreakspace": "0.333em"}

# accents and over/under marks: command -> (character, "over"|"under")
ACCENTS = {"hat": (chr(0x302), "over"), "widehat": (chr(0x302), "over"), "tilde": (chr(0x303), "over"),
           "widetilde": (chr(0x303), "over"), "check": (chr(0x30C), "over"), "widecheck": (chr(0x30C), "over"),
           "breve": (chr(0x306), "over"), "acute": (chr(0x301), "over"), "grave": (chr(0x300), "over"),
           "dot": (chr(0x307), "over"), "ddot": (chr(0x308), "over"), "dddot": (chr(0x20DB), "over"),
           "ddddot": (chr(0x20DC), "over"), "mathring": (chr(0x30A), "over"), "vec": ("→", "over"),
           "bar": ("‾", "over"), "overline": ("¯", "over"), "underline": ("_", "under"),
           "overrightarrow": ("→", "over"), "overleftarrow": ("←", "over"), "overleftrightarrow": ("↔", "over"),
           "underrightarrow": ("→", "under"), "underleftarrow": ("←", "under"), "underleftrightarrow": ("↔", "under"),
           "utilde": (chr(0x330), "under"), "overbrace": ("⏞", "over"), "underbrace": ("⏟", "under"),
           "overbracket": ("⎴", "over"), "underbracket": ("⎵", "under"), "overparen": ("⏜", "over"),
           "underparen": ("⏝", "under")}
BRACES = {"⏞", "⏟", "⎴", "⎵", "⏜", "⏝"}

FONTS = {"mathbb": "double-struck", "Bbb": "double-struck", "mathcal": "script", "mathscr": "script",
         "mathfrak": "fraktur", "mathbf": "bold", "mathrm": "normal", "mathit": "italic", "mathsf": "sans-serif",
         "mathtt": "monospace", "boldsymbol": "bold-italic", "bm": "bold-italic", "mathbfit": "bold-italic",
         "mathsfit": "sans-serif-italic", "mathbfsf": "bold-sans-serif", "mathnormal": "", "pmb": "bold-italic",
         "mathbffrak": "bold-fraktur", "mathbfcal": "bold-script", "mathbfscr": "bold-script", "symbf": "bold",
         "symrm": "normal", "symit": "italic", "symbb": "double-struck", "symcal": "script", "symsf": "sans-serif",
         "symtt": "monospace", "symbfit": "bold-italic", "mathup": "normal", "symup": "normal"}
OLD_FONTS = {"rm": "normal", "bf": "bold", "it": "italic", "cal": "script", "sf": "sans-serif", "tt": "monospace",
             "mit": "italic", "frak": "fraktur", "boldmath": "bold-italic"}
# size switches: the rest of the group in that size (relative to the text around it)
SIZES = {"tiny": "50%", "scriptsize": "70%", "footnotesize": "80%", "small": "90%", "normalsize": "100%",
         "large": "120%", "Large": "144%", "LARGE": "173%", "huge": "207%", "Huge": "249%"}
SIZE_TEX = {v: k for k, v in SIZES.items()}
TEXT_CMDS = {"text": "", "textrm": "", "textnormal": "", "mbox": "", "hbox": "", "textup": "", "textmd": "",
             "textbf": "bold", "textit": "italic", "textsl": "italic", "textsf": "sans-serif", "texttt": "monospace",
             "emph": "italic"}
STYLES = {"displaystyle": ("displaystyle", "true"), "textstyle": ("displaystyle", "false"),
          "scriptstyle": ("scriptlevel", "1"), "scriptscriptstyle": ("scriptlevel", "2")}
BIG_SIZES = {"big": "1.2em", "Big": "1.623em", "bigg": "2.047em", "Bigg": "2.470em"}
BIG_SIZES.update({k + s: v for k, v in list(BIG_SIZES.items()) for s in ("l", "r", "m")})
OPEN = set("([{⟨⌈⌊⟮⟦⌜⌞")
CLOSE = set(")]}⟩⌉⌋⟯⟧⌝⌟")
BARS = {"|", "‖", "∥", "∣"}
NOOPS = {"nonumber", "notag", "displaybreak", "allowbreak", "nolimits", "limits", "qedhere", "hline", "hdashline",
         "strut", "mathstrut", "relax", "protect", "par", "noindent", "centering", "hfill", "hfil", "vfill", "medskip",
         "smallskip", "bigskip", "nobreak", "newline", "linebreak", "pagebreak", "begingroup", "endgroup",
         "displaylimits", "global", "nolinebreak", "toprule", "midrule", "bottomrule"}
SKIP_ARG = {"label", "tag", "tag*", "cline", "vspace", "vspace*", "hypertarget", "index", "eqref", "ref", "cite",
            "footnote", "cmidrule"}
MATRIX_FENCES = {"pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"), "vmatrix": ("∣", "∣"),
                 "Vmatrix": ("∥", "∥"), "cases": ("{", ""), "dcases": ("{", ""), "rcases": ("", "}"),
                 "pmatrix*": ("(", ")"), "bmatrix*": ("[", "]"), "psmallmatrix": ("(", ")"),
                 "bsmallmatrix": ("[", "]"), "numcases": ("{", ""), "drcases": ("", "}")}
ALIGNED = {"aligned", "align", "align*", "alignat", "alignat*", "alignedat", "split", "eqnarray", "eqnarray*",
           "flalign", "flalign*", "xalignat", "xxalignat"}
PLAIN_ENVS = {"equation", "equation*", "displaymath", "math", "empheq"}
NEGATED = {"=": "≠", "<": "≮", ">": "≯", "∈": "∉", "⊂": "⊄", "⊃": "⊅", "⊆": "⊈", "⊇": "⊉", "≤": "≰", "≥": "≱",
           "∼": "≁", "≅": "≇", "≡": "≢", "≈": "≉", "∣": "∤", "∥": "∦", "∃": "∄", "→": "↛", "←": "↚", "↔": "↮",
           "⇒": "⇏", "⇐": "⇍", "⇔": "⇎", "≺": "⊀", "≻": "⊁", "⊢": "⊬", "⊨": "⊭", "≃": "≄", "∋": "∌"}

# characters that are identifiers rather than operators when read from text (OMML runs, Typst)
MI_CHARS = {c for c in TEX_MI.values() if not c.isalpha()}

_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|%[^\n]*|\s+|.", re.DOTALL)


def _mo(text: str, **attrs: str) -> ET.Element:
    if text in OPEN or text in CLOSE or text in BARS:
        attrs.setdefault("stretchy", "false")
        if text in OPEN:
            attrs.setdefault("form", "prefix")
        elif text in CLOSE:
            attrs.setdefault("form", "postfix")
    return _el("mo", text, **attrs)


def _mspace(width: str) -> ET.Element:
    return _el("mspace", width=width)


def _func(name: str) -> ET.Element:
    return _row([_el("mi", name, mathvariant="normal"), _el("mo", APPLY)])


def _fence(open_: str, items: list[ET.Element], close: str) -> ET.Element:
    r = _el("mrow")
    if open_:
        r.append(_el("mo", open_, stretchy="true", form="prefix"))
    r.extend(items)
    if close:
        r.append(_el("mo", close, stretchy="true", form="postfix"))
    return r


def _mtable(rows: list[list[list[ET.Element]]], aligns: list[str], alternate: bool = False,
            displaystyle: bool = False) -> ET.Element:
    width = max((len(r) for r in rows), default=0)
    table = _el("mtable")
    if displaystyle:
        table.set("displaystyle", "true")
    for r in rows:
        tr = ET.SubElement(table, "mtr")
        for k in range(width):
            a = aligns[k % len(aligns)] if alternate else aligns[min(k, len(aligns) - 1)]
            td = ET.SubElement(tr, "mtd", {"columnalign": a, "style": f"text-align: {a}"})
            td.extend(r[k] if k < len(r) else [])
    return table


# --- TeX to MathML ----------------------------------------------------------------------------------

STOPS = frozenset({"}", "&", "\\\\", "\\cr", "\\end", "\\right", "\\middle"})
ROW_BREAKS = ("\\\\", "\\cr")
DIMEN = r"[-+]?\d*\.?\d+\s*(?:em|ex|pt|mm|cm|in|mu|bp|pc|sp)"


class _TexParser:
    def __init__(self, tex: str, display: bool, macros: dict | None = None, depth: int = 0, font: str = ""):
        self.toks = [t for t in _TOKEN.findall(tex) if not t.startswith("%")]
        if len(self.toks) > MAX_TOKENS:
            raise _TooComplex
        self.i = 0
        self.display = display
        self.macros = macros or {}
        self.expanded = 0
        self.depth = depth
        self.font = font  # mathvariant for letters, set by \mathbf, \rm, ...
        self.kind: dict[int, str] = {}  # id(element) -> "display" | "always" | "side": where its scripts go
        self.keep: list[ET.Element] = []  # keeps the ids above in use

    def sub(self, tex: str, font: str | None = None) -> list[ET.Element]:
        p = _TexParser(tex, self.display, self.macros, self.depth + 1, self.font if font is None else font)
        p._nest()
        return p.body()

    # tokens
    def peek(self, spaces: bool = False) -> str | None:
        while self.i < len(self.toks):
            t = self.toks[self.i]
            if not spaces and t.isspace():
                self.i += 1
                continue
            if t in self.macros:
                self._expand(t)
                continue
            return t
        return None

    def take(self) -> str | None:
        t = self.peek()
        if t is not None:
            self.i += 1
        return t

    def _expand(self, name: str) -> None:
        self.expanded += 1
        if self.expanded > MAX_EXPAND or len(self.toks) > MAX_TOKENS:
            raise _TooComplex
        nargs, body = self.macros[name]
        self.i += 1
        args = [self._raw_arg() for _ in range(nargs)]
        out: list[str] = []
        btoks = [t for t in _TOKEN.findall(body) if not t.startswith("%")]
        k = 0
        while k < len(btoks):
            t = btoks[k]
            if t == "#" and k + 1 < len(btoks) and btoks[k + 1].isdecimal():
                n = int(btoks[k + 1]) - 1
                out += args[n] if 0 <= n < len(args) else []
                k += 2
                continue
            out.append(t)
            k += 1
        self.toks[self.i:self.i] = out

    def _raw_arg(self) -> list[str]:
        while self.i < len(self.toks) and self.toks[self.i].isspace():
            self.i += 1
        if self.i >= len(self.toks):
            return []
        t = self.toks[self.i]
        self.i += 1
        if t != "{":
            return [t]
        depth, out = 1, []
        while self.i < len(self.toks):
            t = self.toks[self.i]
            self.i += 1
            if t == "{":
                depth += 1
            elif t == "}":
                depth -= 1
                if not depth:
                    break
            out.append(t)
        return out

    def raw_group(self) -> str:
        """The text of the next {group} (or single token), unparsed."""
        self.peek()
        return "".join(self._raw_arg())

    def opt(self, only: str | None = None) -> str | None:
        """An optional [argument]'s raw text, when there is one (and it matches ``only``, a regex)."""
        if self.peek() != "[":
            return None
        start = self.i
        self.i += 1
        depth, out = 0, []
        while self.i < len(self.toks):
            t = self.toks[self.i]
            self.i += 1
            if t == "{":
                depth += 1
            elif t == "}":
                depth -= 1
            elif t == "]" and depth <= 0:
                break
            out.append(t)
        text = "".join(out)
        if only is not None and not re.fullmatch(only, text.strip()):
            self.i = start
            return None
        return text

    def _nest(self) -> None:
        if self.depth > MAX_NEST:
            raise _TooComplex

    # structure
    def body(self) -> list[ET.Element]:
        rows = self.table_rows(None)
        if len(rows) == 1 and len(rows[0]) == 1:
            return rows[0][0]
        if any(len(r) > 1 for r in rows):
            return [_mtable(rows, ["right", "left"], alternate=True, displaystyle=True)]
        return [_mtable(rows, ["center"], displaystyle=True)]

    def table_rows(self, end: str | None) -> list[list[list[ET.Element]]]:
        rows: list[list[list[ET.Element]]] = [[]]
        pending: list[ET.Element] = []
        while True:
            cell = self.row()
            t = self.peek()
            if t is not None and t != "&" and t not in ROW_BREAKS and t != end and not (t == "\\end" and end == "}"):
                self.i += 1  # a stray }, \right, \middle or \end: dropped, the cell goes on
                if t in ("\\right", "\\middle"):
                    self.delim()
                elif t == "\\end":
                    self.raw_group()
                pending += cell
                continue
            rows[-1].append(pending + cell)
            pending = []
            if t is None or t == end or t == "\\end":
                break
            self.i += 1
            if t in ROW_BREAKS:
                self.opt(DIMEN)
                rows.append([])
        if len(rows) > 1 and rows[-1] in ([[]], []):
            rows.pop()
        return rows

    def row(self) -> list[ET.Element]:
        out: list[ET.Element] = []
        infix: tuple[str, list[ET.Element]] | None = None
        while True:
            t = self.peek()
            if t is None or t in STOPS:
                break
            if t in ("^", "_", "'"):
                out.append(self.scripts(_el("mrow")))
                continue
            self.i += 1
            name = t[1:] if t.startswith("\\") and len(t) > 1 else ""
            if name in ("over", "choose", "atop", "above", "brace", "brack"):
                if name == "above":
                    self._dimension()
                infix = (name, out)
                out = []
                continue
            if name in STYLES:
                attr, val = STYLES[name]
                st = _el("mstyle", **{attr: val})
                st.extend(self.row())
                out.append(st)
                break
            if name in OLD_FONTS:
                self.font = OLD_FONTS[name]
                continue
            if name in SIZES:
                st = _el("mstyle", mathsize=SIZES[name])
                st.extend(self.row())
                out.append(st)
                break
            if name == "color":
                st = _el("mstyle", mathcolor=self.raw_group().strip())
                st.extend(self.row())
                out.append(st)
                break
            el = self.atom(t)
            if el is not None:
                out.append(self.scripts(el))
        if infix is None:
            return out
        name, left = infix
        frac = _el("mfrac")
        frac.extend([_row(left) if left else _el("mrow"), _row(out) if out else _el("mrow")])
        if name in ("choose", "atop", "brace", "brack"):
            frac.set("linethickness", "0")
        if name in ("choose", "brace", "brack"):
            o, c = {"choose": ("(", ")"), "brace": ("{", "}"), "brack": ("[", "]")}[name]
            return [_fence(o, [frac], c)]
        return [frac]

    def group(self) -> ET.Element:
        """After a "{": the group's contents as one element."""
        self.depth += 1
        self._nest()
        saved = self.font
        items: list[ET.Element] = []
        while True:
            items += self.row()
            t = self.peek()
            if t is None:
                break
            self.i += 1
            if t == "}":
                break
            if t in ("\\right", "\\middle"):
                self.delim()
            elif t == "\\end":
                self.raw_group()
        self.font = saved
        self.depth -= 1
        return _row(items) if items else _el("mrow")

    def arg(self) -> ET.Element:
        self.depth += 1
        self._nest()
        try:
            t = self.peek()
            if t is None or t in STOPS:
                return _el("mrow")
            self.i += 1
            if t == "{":
                return self.group()
            if t.isdigit() and t.isascii():
                return self.token("mn", t)
            el = self.atom(t)
            return el if el is not None else _el("mrow")
        finally:
            self.depth -= 1

    def scripts(self, base: ET.Element) -> ET.Element:
        sub = sup = None
        primes = 0
        limits = None
        while True:
            t = self.peek()
            if t == "\\limits":
                self.i += 1
                limits = True
            elif t == "\\nolimits":
                self.i += 1
                limits = False
            elif t == "'" and sup is None:
                self.i += 1
                primes += 1
            elif t == "^" and sup is None:
                self.i += 1
                sup = self.arg()
            elif t == "_" and sub is None:
                self.i += 1
                sub = self.arg()
            else:
                break
        if primes:
            p = _el("mo", {1: "′", 2: "″", 3: "‴", 4: "⁗"}.get(primes, "′" * primes))
            sup = p if sup is None else _row([p, sup])
        if sub is None and sup is None:
            return base
        kind = self.kind.get(id(base), "")
        under = limits if limits is not None else kind == "always" or kind == "display" and self.display
        if under:
            tag = "munderover" if sub is not None and sup is not None else "munder" if sub is not None else "mover"
        else:
            tag = "msubsup" if sub is not None and sup is not None else "msub" if sub is not None else "msup"
        el = _el(tag)
        el.append(base)
        el.extend([x for x in (sub, sup) if x is not None])
        return el

    def mark(self, el: ET.Element, kind: str) -> ET.Element:
        self.keep.append(el)
        self.kind[id(el)] = kind
        return el

    def token(self, tag: str, text: str) -> ET.Element:
        font = self.font
        if font == "normal":
            return _el("mi", text, mathvariant="normal") if tag == "mi" else _el(tag, text)
        if font:
            s = styled(text, font)
            if s != text:
                return _el(tag, s, mathvariant=font)
        return _el(tag, text)

    def atom(self, t: str) -> ET.Element | None:
        if not t.startswith("\\") or len(t) == 1:
            return self.char(t)
        name = t[1:]
        if name in TEX_MI:
            ch = TEX_MI[name]
            return self.token("mi", ch) if self.font and ch not in ("…", "⋯", "⋮", "⋱") else _el("mi", ch)
        if name in TEX_UPPER_GREEK:
            ch = TEX_UPPER_GREEK[name]
            return self.token("mi", ch) if self.font and self.font != "normal" else _el("mi", ch, mathvariant="normal")
        if name in TEX_VAR_GREEK:
            return _el("mi", TEX_VAR_GREEK[name])
        if name in TEX_BIG:
            ch = TEX_BIG[name]
            return self.mark(_el("mo", ch), "side" if ch in INTEGRALS else "display")
        if name in TEX_MO:
            return _mo(TEX_MO[name])
        if name in FUNCTIONS:
            el = _func(name)
            return self.mark(el, "display") if name in LIMIT_FUNCTIONS else el
        if name in SPACES:
            return _mspace(SPACES[name])
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            if name == "cfrac":
                self.opt()
            frac = _el("mfrac")
            frac.extend([self.arg(), self.arg()])
            return _style(frac, "true") if name in ("dfrac", "cfrac") else _style(frac, "false") if name == "tfrac" else frac
        if name in ("binom", "dbinom", "tbinom"):
            frac = _el("mfrac", linethickness="0")
            frac.extend([self.arg(), self.arg()])
            el = _fence("(", [frac], ")")
            return _style(el, "true") if name == "dbinom" else _style(el, "false") if name == "tbinom" else el
        if name == "sqrt":
            index = self.opt()
            body = self.arg()
            if index is not None and index.strip():
                el = _el("mroot")
                el.extend([body, _row(self.sub(index))])
                return el
            el = _el("msqrt")
            el.append(body)
            return el
        if name in ACCENTS:
            ch, where = ACCENTS[name]
            el = _el("mover" if where == "over" else "munder")
            el.extend([self.arg(), _el("mo", ch, accent="true")])
            return self.mark(el, "always") if ch in BRACES else el
        if name in ("operatorname", "operatornamewithlimits", "DeclareMathOperator"):
            star = self.peek() == "*"
            if star:
                self.i += 1
            text = re.sub(r"\\[,;:! ]|\\mathrm|\s+|[{}]", "", self.raw_group())
            el = _func(text or "?")
            return self.mark(el, "display") if star or name == "operatornamewithlimits" else el
        if name in FONTS:
            group = self.raw_group()
            if FONTS[name] == "normal" and re.fullmatch(r"[A-Za-z]{2,}", group.strip()):
                return _el("mi", group.strip(), mathvariant="normal")
            items = self.sub(group, FONTS[name])
            return _row(items) if items else _el("mrow")
        if name in TEXT_CMDS:
            return self.text(self.raw_group(), TEXT_CMDS[name])
        if name == "textsc":
            el = self.text(self.raw_group(), "")
            for mt in el.iter("mtext"):
                mt.set("style", "font-variant: small-caps")
            return el
        if name == "left":
            return self.fenced()
        if name in BIG_SIZES:
            d = self.delim()
            if not d:
                return None
            form = "prefix" if name.endswith("l") or d in OPEN else "postfix" if name.endswith("r") or d in CLOSE else ""
            attrs = {"minsize": BIG_SIZES[name], "maxsize": BIG_SIZES[name], "stretchy": "true"}
            if form:
                attrs["form"] = form
            return _el("mo", d, **attrs)
        if name == "begin":
            return self.environment()
        if name in ("boxed", "fbox", "framebox"):
            el = _el("menclose", notation="box")
            el.append(self.text(self.raw_group(), "") if name != "boxed" else self.arg())
            return el
        if name in ("cancel", "bcancel", "xcancel", "sout"):
            el = _el("menclose", notation={"cancel": "updiagonalstrike", "bcancel": "downdiagonalstrike",
                                           "xcancel": "updiagonalstrike downdiagonalstrike",
                                           "sout": "horizontalstrike"}[name])
            el.append(self.arg())
            return el
        if name in ("phantom", "hphantom", "vphantom"):
            el = _el("mphantom")
            el.append(self.arg())
            return el
        if name in ("textcolor", "colorbox"):
            color = self.raw_group().strip()
            el = _el("mstyle", **({"mathcolor": color} if name == "textcolor" else {"mathbackground": color}))
            el.append(self.arg())
            return el
        if name in ("overset", "stackrel", "underset"):
            mark = self.arg()
            el = _el("munder" if name == "underset" else "mover")
            el.extend([self.arg(), mark])
            return el
        if name in X_ARROWS:
            below = self.opt()
            above = self.arg()
            if below:
                el = _el("munderover")
                el.extend([_el("mo", X_ARROWS[name]), _row(self.sub(below)), above])
            else:
                el = _el("mover")
                el.extend([_el("mo", X_ARROWS[name]), above])
            return el
        if name == "not":
            nxt = self.take()
            el = self.atom(nxt) if nxt is not None else None
            if el is not None and el.tag == "mo" and el.text:
                el.text = NEGATED.get(el.text) or el.text + chr(0x338)
                return el
            return _row([_el("mo", "/"), el]) if el is not None else _el("mo", "/")
        if name in ("mathrel", "mathbin", "mathop", "mathord", "mathopen", "mathclose", "mathpunct", "mathinner"):
            el = self.arg()
            if name in ("mathrel", "mathbin", "mathopen", "mathclose", "mathpunct") and el.tag in ("mi", "mn"):
                el.tag = "mo"
                el.attrib.pop("mathvariant", None)
            return self.mark(el, "display") if name == "mathop" else el
        if name == "bmod":
            return _row([_mspace("0.222em"), _func("mod"), _mspace("0.222em")])
        if name == "mod":
            return _row([_mspace("1em"), _func("mod"), _mspace("0.333em")])
        if name in ("pmod", "pod"):
            a = self.arg()
            inner = [_func("mod"), _mspace("0.333em"), a] if name == "pmod" else [a]
            return _row([_mspace("1em"), _mo("("), *inner, _mo(")")])
        if name in ("hspace", "hspace*", "hskip", "kern", "mkern", "mskip", "mspace"):
            if self.peek() == "*":
                self.i += 1
            dim = self.raw_group() if self.peek() == "{" else self._dimension()
            return _mspace(_em(dim))
        if name in ("substack", "subarray"):
            if name == "subarray":
                self.raw_group()
            if self.take() != "{":
                return None
            self.depth += 1
            self._nest()
            rows = self.table_rows("}")
            if self.peek() == "}":
                self.i += 1
            self.depth -= 1
            return _mtable(rows, ["center"])
        if name in SKIP_ARG:
            if self.peek() == "*":
                self.i += 1
            self.opt()
            self.raw_group()
            return None
        if name in ("smash", "ensuremath"):
            if name == "smash":
                self.opt()
            return self.arg()
        if name in NOOPS:
            return None
        if name == "sideset":
            left, right = self.arg(), self.arg()
            return _row([left, self.arg(), right])
        if name == "middle":
            d = self.delim()
            return _el("mo", d, stretchy="true", form="infix") if d else None
        if name == "right":
            d = self.delim()
            return _el("mo", d, stretchy="true", form="postfix") if d else None
        if name in ("dots", "dotsc"):
            return _el("mi", "…")
        if name.isspace():
            return _mspace("0.333em")  # a backslash before a line break is a space
        if len(name) == 1 and not name.isalpha():
            return _mo(name)
        # unknown: kept as written; its braced arguments follow as math
        return _el("mtext", t, **{"class": "raw"})

    def _dimension(self) -> str:
        out = []
        while True:
            t = self.peek()
            if t is None or not re.fullmatch(r"[-+.\d]|[a-z]+", t):
                break
            out.append(t)
            self.i += 1
            if t.isalpha():
                break
        return "".join(out)

    def char(self, t: str) -> ET.Element | None:
        if t.isascii() and t.isalpha():
            return self.token("mi", t)
        if t.isascii() and t.isdigit():
            num = [t]
            while True:
                nxt = self.peek(spaces=True)
                if nxt is not None and nxt.isascii() and nxt.isdigit():
                    num.append(nxt)
                    self.i += 1
                elif nxt == "." and self.i + 1 < len(self.toks) and self.toks[self.i + 1].isascii() \
                        and self.toks[self.i + 1].isdigit():
                    num.append(".")
                    self.i += 1
                else:
                    break
            return self.token("mn", "".join(num))
        if t == "{":
            return self.group()
        if t == "~":
            return _mspace("0.333em")
        if t.isspace():
            return None
        if t in MI_CHARS:
            return _el("mi", t)
        if t.isalpha():
            return self.token("mi", t)
        if t.isdigit():
            return _el("mn", t)
        if t == "-":
            return _el("mo", "−")
        if t in BIG_CHARS:
            return self.mark(_el("mo", t), "side" if t in INTEGRALS else "display")
        return _mo(t)

    def delim(self) -> str:
        t = self.take()
        if t is None or t == ".":
            return ""
        if t.startswith("\\") and len(t) > 1:
            name = t[1:]
            if name in ("{", "}"):
                return name
            return "‖" if name == "|" else TEX_MO.get(name, TEX_MI.get(name, ""))
        return {"<": "⟨", ">": "⟩"}.get(t, t)

    def fenced(self) -> ET.Element:
        self.depth += 1
        self._nest()
        items: list[ET.Element] = []
        open_ = self.delim()
        close = ""
        while True:
            items += self.row()
            t = self.peek()
            if t == "\\middle":
                self.i += 1
                d = self.delim()
                if d:
                    items.append(_el("mo", d, stretchy="true", form="infix"))
                continue
            if t == "\\right":
                self.i += 1
                close = self.delim()
            break
        self.depth -= 1
        return _fence(open_, items, close)

    def environment(self) -> ET.Element | None:
        name = self.raw_group().strip()
        self.depth += 1
        self._nest()
        spec = ""
        if name in ("array", "subarray", "alignat", "alignat*", "alignedat", "tabular"):
            self.opt(r"[tbc]")
            spec = self.raw_group()
        elif name in ("matrix*", "pmatrix*", "bmatrix*", "aligned", "gathered", "split", "alignedat"):
            self.opt(r"[tbclr]")
        rows = self.table_rows("\\end")
        if self.peek() == "\\end":
            self.i += 1
            self.raw_group()
        self.depth -= 1
        if name in PLAIN_ENVS:
            if len(rows) == 1:
                items = [c for cell in rows[0] for c in cell]
                return _row(items) if items else None
            return _mtable(rows, ["center"], displaystyle=True)
        if name in ("array", "subarray", "tabular"):
            cols = [{"l": "left", "c": "center", "r": "right", "p": "left"}[c]
                    for c in re.sub(r"\{[^{}]*\}", "", spec) if c in "lcrp"] or ["center"]
            table = _mtable(rows, cols)
        elif name in ("cases", "dcases", "rcases", "numcases", "drcases"):
            table = _mtable(rows, ["left", "left"])
        elif name in ALIGNED:
            table = _mtable(rows, ["right", "left"], alternate=True, displaystyle=True)
        elif name in ("gathered", "gather", "gather*", "multline", "multline*"):
            table = _mtable(rows, ["center"], displaystyle=True)
        else:
            table = _mtable(rows, ["center"])
        fences = MATRIX_FENCES.get(name)
        return _fence(fences[0], [table], fences[1]) if fences else table

    def text(self, raw: str, variant: str) -> ET.Element:
        parts = re.split(r"(?<!\\)\$", raw)
        items: list[ET.Element] = []
        for k, part in enumerate(parts):
            if k % 2:
                items += self.sub(part, "")
            elif part:
                txt = re.sub(r"^ | $", NBSP, re.sub(r"\s+", " ", _text_unescape(part)))
                if txt:
                    items.append(_el("mtext", styled(txt, variant), mathvariant=variant) if variant else _el("mtext", txt))
        return _row(items) if items else _el("mtext", "")


X_ARROWS = {"xrightarrow": "→", "xleftarrow": "←", "xRightarrow": "⇒", "xLeftarrow": "⇐", "xleftrightarrow": "↔",
            "xLeftrightarrow": "⇔", "xmapsto": "↦", "xhookrightarrow": "↪", "xhookleftarrow": "↩",
            "xlongequal": "=", "xtwoheadrightarrow": "↠"}


def _style(el: ET.Element, display: str) -> ET.Element:
    st = _el("mstyle", displaystyle=display)
    st.append(el)
    return st


def _em(dim: str) -> str:
    m = re.match(r"\s*([-+]?\d*\.?\d+)\s*(em|ex|pt|mu|mm|cm|in|px)?", dim or "")
    if not m:
        return "0.333em"
    v = float(m.group(1)) * {"em": 1, "ex": 0.43, "pt": 0.1, "mu": 1 / 18, "mm": 0.284, "cm": 2.84, "in": 7.2,
                             "px": 0.075}[m.group(2) or "em"]
    return f"{v:.3f}".rstrip("0").rstrip(".") + "em"


_TEXT_ESC = {"\\%": "%", "\\&": "&", "\\_": "_", "\\#": "#", "\\$": "$", "\\{": "{", "\\}": "}", "\\ ": " ",
             "~": NBSP, "\\textbackslash": "\\", "---": "—", "--": "–", "``": "“", "''": "”", "\\,": " "}


_TEXT_ACCENTS = {'"': 0x308, "'": 0x301, "`": 0x300, "^": 0x302, "~": 0x303, "=": 0x304, ".": 0x307, "c": 0x327,
                 "v": 0x30C, "u": 0x306, "H": 0x30B, "r": 0x30A, "k": 0x328}


_HIDE_BRACES = str.maketrans({"{": "\x01", "}": "\x02", "\\": "\x03"})
_SHOW_BRACES = str.maketrans({"\x01": "{", "\x02": "}", "\x03": "\\"})


def _text_unescape(s: str) -> str:
    s = re.sub(r"\\([\"'`^~=.]|[cvuHrk](?![A-Za-z]))\s*\{?([A-Za-z])\}?",
               lambda m: unicodedata.normalize("NFC", m.group(2) + chr(_TEXT_ACCENTS[m.group(1)])), s)
    s = re.sub(r"\\[;:!>]", " ", s)
    # what the escapes stand for is kept apart until the bare braces are gone: \{x\} came out as "x"
    s = re.sub(r"\\textbackslash\b|\\[%&_#${} ,]|---|--|``|''|~",
               lambda m: _TEXT_ESC.get(m.group(0), m.group(0)).translate(_HIDE_BRACES), s)
    s = re.sub(r"\\[A-Za-z]+\*?\s*", "", s)  # \emph{x} inside text keeps x
    return re.sub(r"(?<!\\)[{}]", "", s).translate(_SHOW_BRACES)


def from_tex(tex: str, display: bool = False, macros: dict | None = None) -> ET.Element:
    """TeX math to MathML. ``macros``: {"\\\\name": (number of arguments, body)} expanded while reading
    (expansion is capped, so a macro that calls itself ends as text)."""
    if len(tex) > MAX_LEN:
        return _wrap([_el("mtext", tex[:MAX_LEN], **{"class": "raw"})], tex, TEX, display)
    try:
        items = _TexParser(tex, display, macros).body()
    except (_TooComplex, RecursionError):
        items = [_el("mtext", tex, **{"class": "raw"})]
    return _wrap(items, tex, TEX, display)


# --- MathML to TeX ----------------------------------------------------------------------------------

def _reverse(*tables: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in tables:
        for k, v in t.items():
            if len(k) > 1:
                out.setdefault(v, "\\" + k)
    return out


CHAR_TEX = _reverse(TEX_MO, TEX_MI, TEX_BIG, TEX_UPPER_GREEK) | {
    "≤": "\\leq", "≥": "\\geq", "≠": "\\neq", "→": "\\to", "←": "\\leftarrow", "∧": "\\wedge", "∨": "\\vee",
    "¬": "\\neg", "∣": "\\mid", "∋": "\\ni", "∖": "\\setminus", "⋅": "\\cdot", "∅": "\\emptyset", "ℏ": "\\hbar",
    "…": "\\ldots", "∼": "\\sim", "≈": "\\approx", "∝": "\\propto", "⋈": "\\bowtie", "{": "\\{", "}": "\\}",
    "‖": "\\|", "|": "|", "[": "[", "]": "]", "#": "\\#", "%": "\\%", "&": "\\&", "$": "\\$", "_": "\\_",
    "\\": "\\backslash", "−": "-", "∗": "\\ast", ":": ":", ".": ".", "△": "\\triangle", "∥": "\\parallel",
    "⟂": "\\perp", "⊥": "\\bot", "∫": "\\int", "∑": "\\sum", "∏": "\\prod", "□": "\\Box", "⟹": "\\implies",
    "⟺": "\\iff", "⇔": "\\Leftrightarrow", "⇒": "\\Rightarrow", "∙": "\\bullet", "†": "\\dagger", "‡": "\\ddagger",
    "⊲": "\\lhd", "⊳": "\\rhd", "∴": "\\therefore", "⋯": "\\cdots", "⋮": "\\vdots", "⋱": "\\ddots",
    "≔": "\\coloneqq", "ϵ": "\\epsilon", "ε": "\\varepsilon", "ϕ": "\\phi", "φ": "\\varphi", "↦": "\\mapsto",
    "⊨": "\\models", "◇": "\\Diamond", "∁": "\\complement", "⟨": "\\langle", "⟩": "\\rangle", "∠": "\\angle",
    "⊤": "\\top", "ℜ": "\\Re", "ℑ": "\\Im", "′": "\\prime", "∞": "\\infty", "~": "\\sim", "^": "\\hat{}",
    "<": "<", ">": ">", "=": "=", "+": "+", "/": "/", "*": "*", "!": "!", "?": "?", ",": ",", ";": ";",
    "'": "'", "(": "(", ")": ")", "@": "@", "`": "`", "\"": "\\text{\"}"}
ACCENT_TEX = {v[0]: k for k, v in ACCENTS.items() if v[1] == "over" and not k.startswith(("wide", "over"))}
ACCENT_TEX.update({"¯": "overline", "^": "hat", "ˆ": "hat", "~": "tilde", "˜": "tilde", "⃗": "vec", "˙": "dot",
                   "¨": "ddot", "´": "acute", "`": "grave", "˘": "breve", "ˇ": "check", chr(0x305): "bar",
                   chr(0x304): "bar", "⏞": "overbrace", "⎴": "overbracket", "⏜": "overparen", "←": "overleftarrow",
                   "↔": "overleftrightarrow", "→": "vec", "‾": "bar"})
UNDER_TEX = {"_": "underline", chr(0x332): "underline", "⏟": "underbrace", "⎵": "underbracket", "⏝": "underparen",
             "→": "underrightarrow", "←": "underleftarrow", "↔": "underleftrightarrow", chr(0x330): "utilde"}
VARIANT_TEX = {"double-struck": "mathbb", "script": "mathcal", "fraktur": "mathfrak", "bold": "mathbf",
               "normal": "mathrm", "italic": "mathit", "sans-serif": "mathsf", "monospace": "mathtt",
               "bold-italic": "boldsymbol", "bold-fraktur": "mathbffrak", "bold-script": "mathbfcal",
               "bold-sans-serif": "mathbfsf", "sans-serif-italic": "mathsfit", "sans-serif-bold-italic": "mathsfit"}
TEXT_VARIANT = {"bold": "textbf", "italic": "textit", "sans-serif": "textsf", "monospace": "texttt"}
WIDTH_TEX = {"0.167em": "\\,", "0.222em": "\\:", "0.278em": "\\;", "-0.167em": "\\!", "1em": "\\quad", "2em": "\\qquad",
             "0.333em": "\\ ", "0.5em": "\\enspace", "-0.222em": "\\negmedspace", "-0.278em": "\\negthickspace",
             "1.0em": "\\quad", "2.0em": "\\qquad"}
FENCE_ENV = {("(", ")"): "pmatrix", ("[", "]"): "bmatrix", ("{", "}"): "Bmatrix", ("|", "|"): "vmatrix",
             ("∣", "∣"): "vmatrix", ("‖", "‖"): "Vmatrix", ("∥", "∥"): "Vmatrix", ("{", ""): "cases", ("", "}"): "rcases"}
DELIM_TEX = {"(": "(", ")": ")", "[": "[", "]": "]", "{": "\\{", "}": "\\}", "|": "|", "∣": "|", "‖": "\\|", "∥": "\\|",
             "⟨": "\\langle", "⟩": "\\rangle", "⌈": "\\lceil", "⌉": "\\rceil", "⌊": "\\lfloor", "⌋": "\\rfloor",
             "/": "/", "\\": "\\backslash", "↑": "\\uparrow", "↓": "\\downarrow", "⟦": "\\llbracket", "⟧": "\\rrbracket",
             "⟮": "\\lgroup", "⟯": "\\rgroup", "": "."}
_SPACED = set("=<>+−±∓×÷⋅∘∗≤≥≠≡≈∼≃≅∝≪≫⊂⊃⊆⊇∈∉∋→←↔⇒⇐⇔⟹⟺⟶⟵↦∣∥∪∩∧∨⊕⊗≔≺≻⪯⪰⊢⊨")
_SPACED_TEX = {CHAR_TEX.get(c, c) for c in _SPACED} | {"-"}
_PRIMES = {"′": "'", "″": "''", "‴": "'''", "⁗": "''''"}
_ZERO = ("0", "0px", "0pt", "0em", "0.0em")


def _fence_parts(el: ET.Element):
    """For an mrow between stretchy fences: (open, [[items] per part], [separators], close); else None."""
    if _local(el.tag) != "mrow":
        return None
    kids = _kids(el)
    if not kids:
        return None

    def fence(k, form):
        return _local(k.tag) == "mo" and k.get("form") == form and (k.get("stretchy") == "true" or k.get("fence") == "true")

    o = fence(kids[0], "prefix")
    c = fence(kids[-1], "postfix") and (len(kids) > 1 or not o)
    if not o and not c:
        return None
    parts: list[list[ET.Element]] = [[]]
    seps: list[str] = []
    for k in kids[1 if o else 0:len(kids) - 1 if c else len(kids)]:
        if _local(k.tag) == "mo" and k.get("form") == "infix" and k.get("stretchy") == "true":
            seps.append(k.text or "")
            parts.append([])
        else:
            parts[-1].append(k)
    return (kids[0].text or "") if o else "", parts, seps, (kids[-1].text or "") if c else ""


def _func_name(el: ET.Element) -> str | None:
    if _local(el.tag) != "mrow":
        return None
    kids = _kids(el)
    if len(kids) == 2 and _local(kids[0].tag) == "mi" and _local(kids[1].tag) == "mo" and kids[1].text == APPLY:
        return kids[0].text or ""
    return None


def _scripts_of(tag: str, kids: list[ET.Element]):
    """(base, sub, sup, above/below) for a script element."""
    base = kids[0] if kids else _el("mrow")
    second = kids[1] if len(kids) > 1 else None
    third = kids[2] if len(kids) > 2 else None
    if tag in ("msub", "munder"):
        return base, second, None, tag == "munder"
    if tag in ("msup", "mover"):
        return base, None, second, tag == "mover"
    return base, second, third, tag == "munderover"


class _ToTex:
    def __init__(self, display: bool):
        self.display = display
        self.depth = 0

    def row(self, kids: list[ET.Element]) -> str:
        out: list[str] = []
        k = 0
        while k < len(kids):
            c = kids[k]
            tag = _local(c.tag)
            if tag == "mi" and len(c.text or "") > 1 and k + 1 < len(kids) and _local(kids[k + 1].tag) == "mo" \
                    and kids[k + 1].text == APPLY:
                out.append(self.function(c.text or "", False))
                k += 2
                continue
            if not (tag == "mo" and c.text == APPLY):
                out.append(self.tex(c))
            k += 1
        return _join(out)

    def function(self, name: str, limits: bool) -> str:
        if name in FUNCTIONS:
            return "\\" + name
        return ("\\operatorname*{" if limits else "\\operatorname{") + _text_escape(name) + "}"

    def arg(self, el: ET.Element | None) -> str:
        if el is None:
            return "{}"
        s = self.tex(el)
        return s if len(s) == 1 and s.isascii() and s.isalnum() else "{" + s + "}"

    def base(self, el: ET.Element) -> str:
        s = self.tex(el)
        tag = _local(el.tag)
        single = tag in ("mi", "mn", "mo", "mtext", "mfrac", "msqrt", "mroot", "menclose", "mphantom", "mtable") \
            or _fence_parts(el) is not None or _func_name(el) is not None \
            or tag == "mover" and self.accent(el, ACCENT_TEX) or tag == "munder" and self.accent(el, UNDER_TEX)
        return s if single and s and not (tag == "mi" and s.startswith("\\") and " " in s) else "{" + s + "}"

    def accent(self, el: ET.Element, table: dict[str, str]) -> str | None:
        kids = _kids(el)
        if len(kids) == 2 and _local(kids[1].tag) == "mo" and kids[1].text in table:
            name = table[kids[1].text or ""]
            if name in ("overbrace", "underbrace", "overbracket", "underbracket", "overparen", "underparen") \
                    or kids[1].get("accent") == "true" or kids[1].get("accentunder") == "true" \
                    or el.get("accent") == "true" or el.get("accentunder") == "true" \
                    or unicodedata.combining(kids[1].text or " "):
                return name
        return None

    def movable(self, base: ET.Element) -> tuple[str, bool] | None:
        """(TeX for a big operator or function base, takes limits by default)."""
        if _local(base.tag) == "mo" and base.text in BIG_CHARS:
            return CHAR_TEX.get(base.text or "", base.text or ""), (base.text or "") not in INTEGRALS
        name = _func_name(base)
        if name is not None:
            return self.function(name, False), name in LIMIT_FUNCTIONS
        return None

    def tex(self, el: ET.Element) -> str:
        self.depth += 1
        try:
            return self._tex(el) if self.depth < 200 else ""
        finally:
            self.depth -= 1

    def _tex(self, el: ET.Element) -> str:
        tag = _local(el.tag)
        kids = _kids(el)
        text = el.text or ""
        if tag == "math":
            return self.row(presentation(el))
        if tag == "semantics":
            return self.tex(kids[0]) if kids else ""
        if tag in ("annotation", "annotation-xml", "none", "mprescripts"):
            return ""
        if tag == "mi":
            return self.token(text, el.get("mathvariant", ""), ident=True)
        if tag == "mn":
            return self.token(text, el.get("mathvariant", ""), ident=False)
        if tag == "mo":
            return self.operator(text)
        if tag == "mtext":
            if "raw" in el.get("class", "").split():
                return text
            base, variant = plain(text)
            variant = el.get("mathvariant", "") or variant
            cmd = "textsc" if "small-caps" in el.get("style", "") else TEXT_VARIANT.get(variant, "text")
            return "\\" + cmd + "{" + _text_escape(base.replace(NBSP, " ")) + "}"
        if tag == "ms":
            return "\\text{\"" + _text_escape(text) + "\"}"
        if tag == "mspace":
            w = el.get("width", "")
            return WIDTH_TEX.get(w, f"\\hspace{{{w}}}" if w else "")
        if tag in ("mrow", "mstyle", "mpadded", "merror", "maction"):
            fp = _fence_parts(el)
            if fp is not None:
                return self.fenced(fp)
            name = _func_name(el)
            if name is not None:
                return self.function(name, False)
            if tag == "mstyle":
                return self.mstyle(el, kids)
            return self.row(kids)
        if tag == "mfrac":
            if len(kids) < 2:
                return self.row(kids)
            num, den = self.tex(kids[0]), self.tex(kids[1])
            if el.get("linethickness") in _ZERO:
                return "{" + num + " \\atop " + den + "}"
            return f"\\frac{{{num}}}{{{den}}}"
        if tag == "msqrt":
            return "\\sqrt{" + self.row(kids) + "}"
        if tag == "mroot":
            if len(kids) < 2:
                return "\\sqrt{" + self.row(kids) + "}"
            return f"\\sqrt[{self.tex(kids[1])}]{{{self.tex(kids[0])}}}"
        if tag in ("msub", "msup", "msubsup", "munder", "mover", "munderover"):
            return self.scripts(el, tag, kids)
        if tag == "mtable":
            return self.table(el, None)
        if tag == "menclose":
            n = el.get("notation", "box")
            cmd = "\\cancel" if "updiagonal" in n and "down" not in n else "\\bcancel" if "downdiagonal" in n \
                and "up" not in n else "\\xcancel" if "updiagonal" in n else "\\sout" if "horizontal" in n else "\\boxed"
            return cmd + "{" + self.row(kids) + "}"
        if tag == "mphantom":
            return "\\phantom{" + self.row(kids) + "}"
        if tag == "mmultiscripts":
            return self.multiscripts(kids)
        return self.row(kids)

    def mstyle(self, el: ET.Element, kids: list[ET.Element]) -> str:
        inner = self.row(kids)
        if el.get("mathcolor"):
            return "{\\color{" + el.get("mathcolor", "") + "}" + inner + "}"
        if el.get("mathsize") in SIZE_TEX:
            return "{\\" + SIZE_TEX[el.get("mathsize", "")] + " " + inner + "}"
        ds = el.get("displaystyle")
        if ds and len(kids) == 1 and _local(kids[0].tag) == "mfrac" and len(_kids(kids[0])) == 2 \
                and kids[0].get("linethickness") not in _ZERO:
            f = _kids(kids[0])
            return ("\\dfrac" if ds == "true" else "\\tfrac") + "{" + self.tex(f[0]) + "}{" + self.tex(f[1]) + "}"
        if ds and len(kids) == 1 and _fence_parts(kids[0]) is not None:
            fp = _fence_parts(kids[0])
            if fp and fp[0] == "(" and fp[3] == ")" and len(fp[1]) == 1 and len(fp[1][0]) == 1 \
                    and _local(fp[1][0][0].tag) == "mfrac" and fp[1][0][0].get("linethickness") in _ZERO:
                f = _kids(fp[1][0][0])
                if len(f) == 2:
                    return ("\\dbinom" if ds == "true" else "\\tbinom") + "{" + self.tex(f[0]) + "}{" + self.tex(f[1]) + "}"
        if ds:
            return "{\\" + ("displaystyle " if ds == "true" else "textstyle ") + inner + "}"
        if el.get("scriptlevel") in ("1", "2"):
            return "{\\" + ("scriptstyle " if el.get("scriptlevel") == "1" else "scriptscriptstyle ") + inner + "}"
        return inner

    def multiscripts(self, kids: list[ET.Element]) -> str:
        if not kids:
            return ""
        post: list[ET.Element] = []
        pre: list[ET.Element] = []
        target = post
        for k in kids[1:]:
            if _local(k.tag) == "mprescripts":
                target = pre
                continue
            target.append(k)
        out = ""
        for k in range(0, len(pre), 2):
            out += "{}" + self.script_pair(pre[k], pre[k + 1] if k + 1 < len(pre) else None)
        out += self.base(kids[0])
        for k in range(0, len(post), 2):
            out += self.script_pair(post[k], post[k + 1] if k + 1 < len(post) else None)
        return out

    def script_pair(self, sub: ET.Element, sup: ET.Element | None) -> str:
        out = "_" + self.arg(sub) if _local(sub.tag) != "none" else ""
        if sup is not None and _local(sup.tag) != "none":
            out += "^" + self.arg(sup)
        return out

    def token(self, text: str, variant: str, ident: bool) -> str:
        base, found = plain(text)
        variant = variant or found
        if ident and len(base) > 1 and base.isascii() and base.isalpha():
            return "\\" + (VARIANT_TEX.get(variant, "mathrm") if variant not in ("", "italic") else "mathrm") + "{" + base + "}"
        out = _cat([CHAR_TEX.get(ch, ch) for ch in base])
        if not variant:
            if ident and len(base) == 1 and base in TEX_UPPER_GREEK.values():
                return "\\var" + CHAR_TEX[base][1:]
            return out
        if variant == "normal":
            if len(base) == 1 and base in TEX_UPPER_GREEK.values():
                return out
            return "\\mathrm{" + out + "}" if ident else out
        if variant == "italic" and not found:
            return out
        cmd = VARIANT_TEX.get(variant)
        return "\\" + cmd + "{" + out + "}" if cmd else out

    def operator(self, text: str) -> str:
        if text in CHAR_TEX:
            return CHAR_TEX[text]
        if len(text) > 1 and text.isalpha():
            return self.function(text, False)
        if len(text) == 2 and text[1] == chr(0x338):
            return "\\not" + self.operator(text[0])
        return _cat([CHAR_TEX.get(ch, ch) for ch in text])

    def fenced(self, fp) -> str:
        o, parts, seps, c = fp
        if not seps and len(parts[0]) == 1:
            only = parts[0][0]
            if _local(only.tag) == "mtable":
                env = FENCE_ENV.get((o, c))
                if env and not (env == "cases" and not self._left_aligned(only)):
                    return self.table(only, env)
            if _local(only.tag) == "mfrac" and only.get("linethickness") in _ZERO and (o, c) == ("(", ")") \
                    and len(_kids(only)) == 2:
                f = _kids(only)
                return f"\\binom{{{self.tex(f[0])}}}{{{self.tex(f[1])}}}"
        out = ["\\left" + DELIM_TEX.get(o, o)]
        for k, part in enumerate(parts):
            if k:
                out.append("\\middle" + DELIM_TEX.get(seps[k - 1], seps[k - 1]))
            out.append(self.row(part))
        out.append("\\right" + DELIM_TEX.get(c, c))
        return _join(out)

    def _left_aligned(self, table: ET.Element) -> bool:
        return all(td.get("columnalign", "left") == "left" for tr in _kids(table) for td in _kids(tr))

    def scripts(self, el: ET.Element, tag: str, kids: list[ET.Element]) -> str:
        if not kids:
            return ""
        if tag in ("mover", "munder") and len(kids) == 2:
            acc = self.accent(el, ACCENT_TEX if tag == "mover" else UNDER_TEX)
            if acc:
                return "\\" + acc + "{" + self.tex(kids[0]) + "}"
            inner = _kids(kids[0])
            if _local(kids[0].tag) == tag and len(inner) == 2:
                acc = self.accent(kids[0], ACCENT_TEX if tag == "mover" else UNDER_TEX)
                if acc in ("overbrace", "overbracket", "overparen", "underbrace", "underbracket", "underparen"):
                    return "\\" + acc + "{" + self.tex(inner[0]) + "}" + ("^" if tag == "mover" else "_") + self.arg(kids[1])
        base, sub, sup, under = _scripts_of(tag, kids)
        mov = self.movable(base)
        if mov is not None:
            b, default = mov
            if under and not (default and self.display):
                b += "\\limits"
            elif not under and default and self.display:
                b += "\\nolimits"
        elif under:
            if tag == "mover":
                return "\\overset{" + self.tex(sup) + "}{" + self.tex(base) + "}"
            if tag == "munder":
                return "\\underset{" + self.tex(sub) + "}{" + self.tex(base) + "}"
            b = "\\mathop{" + self.tex(base) + "}\\limits"
        else:
            b = self.base(base)
        out = b
        if sub is not None:
            out += "_" + self.arg(sub)
        if sup is not None:
            prime = _PRIMES.get(sup.text or "") if _local(sup.tag) == "mo" and not under else None
            out += prime if prime else "^" + self.arg(sup)
        return out

    def table(self, el: ET.Element, env: str | None) -> str:
        trs = [tr for tr in _kids(el) if _local(tr.tag) in ("mtr", "mlabeledtr")]
        rows = [[self.row(_kids(td)) for td in _kids(tr) if _local(td.tag) == "mtd"] for tr in trs]
        aligns: list[str] = []
        for tr in trs:
            for k, td in enumerate(_kids(tr)):
                if len(aligns) <= k:
                    aligns.append(td.get("columnalign") or el.get("columnalign", "center").split()[0])
        if env is None:
            if len(aligns) > 1 and all(a == ("right" if k % 2 == 0 else "left") for k, a in enumerate(aligns)):
                env = "aligned"
            elif all(a == "center" for a in aligns):
                env = "gathered" if len(aligns) <= 1 and el.get("displaystyle") == "true" else "matrix"
            else:
                env = "array"
        body = " \\\\ ".join(" & ".join(r) for r in rows)
        if env == "array":
            spec = "".join({"left": "l", "right": "r"}.get(a, "c") for a in aligns) or "c"
            return f"\\begin{{array}}{{{spec}}} {body} \\end{{array}}"
        return f"\\begin{{{env}}} {body} \\end{{{env}}}"


def _join(parts: list[str]) -> str:
    out = ""
    for p in parts:
        if not p:
            continue
        if p in _SPACED_TEX:
            out += " " + p + " "
            continue
        if out and (re.search(r"\\[A-Za-z]+$", out) or out[-1].isalnum()) and p[0].isalnum():
            out += " "
        out += p
    return re.sub(r"  +", " ", out).strip()


def _cat(parts: list[str]) -> str:
    """The characters of one token: together, with a space only where a command word needs one."""
    out = ""
    for p in parts:
        if out and re.search(r"\\[A-Za-z]+$", out) and p[:1].isalpha():
            out += " "
        out += p
    return out


_TEXT_SPECIAL = {"\\": "\\textbackslash{}", "{": "\\{", "}": "\\}", "$": "\\$", "&": "\\&", "#": "\\#", "^": "\\^{}",
                 "_": "\\_", "%": "\\%", "~": "\\~{}"}


def _text_escape(s: str) -> str:
    return "".join(_TEXT_SPECIAL.get(ch, ch) for ch in s)


def to_tex(math: ET.Element) -> str:
    found = annotation(math)
    if found is not None:
        return found.strip()
    return to_tex_walk(math)


def to_tex_walk(math: ET.Element) -> str:
    """TeX written from the presentation markup alone (any annotation is ignored)."""
    conv = _ToTex(math.get("display") == "block" or math.get("mode") == "display")
    return conv.row(presentation(math)) if _local(math.tag) == "math" else conv.tex(math)


# --- OMML (Word's math) --------------------------------------------------------------------------

def _m(tag: str) -> str:
    return f"{{{M}}}{tag}"


def _mval(parent: ET.Element, tag: str, val: str) -> ET.Element:
    e = ET.SubElement(parent, _m(tag))
    e.set(_m("val"), val)
    return e


# mathvariant -> (m:scr, m:sty)
_VARIANT_OMML = {"normal": ("", "p"), "bold": ("", "b"), "italic": ("", "i"), "bold-italic": ("", "bi"),
                 "double-struck": ("double-struck", "p"), "script": ("script", "p"), "bold-script": ("script", "b"),
                 "fraktur": ("fraktur", "p"), "bold-fraktur": ("fraktur", "b"), "sans-serif": ("sans-serif", "p"),
                 "bold-sans-serif": ("sans-serif", "b"), "sans-serif-italic": ("sans-serif", "i"),
                 "sans-serif-bold-italic": ("sans-serif", "bi"), "monospace": ("monospace", "p")}
_OMML_VARIANT = {v: k for k, v in _VARIANT_OMML.items()}
SPACE_CHARS = {"0.167em": chr(0x2009), "0.222em": chr(0x205F), "0.278em": chr(0x2005), "0.333em": chr(0x2004),
               "0.5em": chr(0x2002), "1em": chr(0x2003), "2em": chr(0x2003) * 2, "-0.167em": chr(0x200A)}
_SPACE_WIDTH = {v: k for k, v in SPACE_CHARS.items()}
ALL_SPACES = set("".join(SPACE_CHARS.values()))
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _om_run(text: str, kind: str, variant: str = "") -> ET.Element:
    r = ET.Element(_m("r"))
    base, found = plain(text)
    variant = variant or found
    if kind == "mi" and not variant and len(base) > 1:
        variant = "normal"
    if kind == "mtext" or variant:
        rpr = ET.SubElement(r, _m("rPr"))
        scr, sty = _VARIANT_OMML.get(variant, ("", "p" if kind == "mtext" else ""))
        if kind == "mtext":
            ET.SubElement(rpr, _m("nor"))
        if scr:
            _mval(rpr, "scr", scr)
        if sty:
            _mval(rpr, "sty", sty)
    t = ET.SubElement(r, _m("t"))
    t.text = base
    if base != base.strip():
        t.set(_XML_SPACE, "preserve")
    return r


class _ToOmml:
    def __init__(self):
        self.depth = 0

    def arg(self, tag: str, el: ET.Element | list[ET.Element] | None) -> ET.Element:
        out = ET.Element(_m(tag))
        if el is not None:
            out.extend(self.row(el if isinstance(el, list) else [el]))
        return out

    def row(self, kids: list[ET.Element]) -> list[ET.Element]:
        self.depth += 1
        try:
            return self._row(list(kids)) if self.depth < 150 else []
        finally:
            self.depth -= 1

    def _row(self, kids: list[ET.Element]) -> list[ET.Element]:
        out: list[ET.Element] = []
        k = 0
        while k < len(kids):
            c = kids[k]
            tag = _local(c.tag)
            if tag in ("mrow", "mstyle", "mpadded", "merror", "maction") and _fence_parts(c) is None \
                    and _func_name(c) is None:
                kids[k:k + 1] = _kids(c)
                continue
            if tag == "semantics":
                kids[k:k + 1] = _kids(c)[:1]
                continue
            nary = self.nary_parts(c)
            if nary is not None:
                operand = kids[k + 1] if k + 1 < len(kids) else None
                out.append(self.nary(*nary, operand))
                k += 2 if operand is not None else 1
                continue
            name = _func_name(c)
            if name is not None and k + 1 < len(kids):
                f = ET.Element(_m("func"))
                ET.SubElement(f, _m("funcPr"))
                ET.SubElement(f, _m("fName")).append(_om_run(name, "mi", _kids(c)[0].get("mathvariant", "")))
                f.append(self.arg("e", kids[k + 1]))
                out.append(f)
                k += 2
                continue
            out.extend(self.el(c))
            k += 1
        return out

    def nary_parts(self, el: ET.Element):
        tag = _local(el.tag)
        kids = _kids(el)
        if tag == "mo" and (el.text or "") in BIG_CHARS:
            return el.text, None, None, False
        if tag in ("msub", "msup", "msubsup", "munder", "mover", "munderover") and kids \
                and _local(kids[0].tag) == "mo" and (kids[0].text or "") in BIG_CHARS:
            _, sub, sup, under = _scripts_of(tag, kids)
            return kids[0].text, sub, sup, under
        return None

    def nary(self, ch: str, sub, sup, under: bool, operand) -> ET.Element:
        n = ET.Element(_m("nary"))
        pr = ET.SubElement(n, _m("naryPr"))
        _mval(pr, "chr", ch)
        _mval(pr, "limLoc", "undOvr" if under else "subSup")
        if sub is None:
            _mval(pr, "subHide", "1")
        if sup is None:
            _mval(pr, "supHide", "1")
        n.extend([self.arg("sub", sub), self.arg("sup", sup), self.arg("e", operand)])
        return n

    def el(self, el: ET.Element) -> list[ET.Element]:
        tag = _local(el.tag)
        kids = _kids(el)
        text = el.text or ""
        if tag in ("mi", "mn", "mo", "ms"):
            return [_om_run(text, tag, el.get("mathvariant", ""))] if text else []
        if tag == "mtext":
            return [_om_run(text, "mtext", el.get("mathvariant", ""))] if text else []
        if tag == "mspace":
            ch = SPACE_CHARS.get(el.get("width", "").replace(".0em", "em"))
            if ch is None:
                return []
            r = ET.Element(_m("r"))
            ET.SubElement(r, _m("t")).text = ch
            return [r]
        if tag == "mrow":
            fp = _fence_parts(el)
            return [self.delimited(fp)] if fp is not None else self.row(kids)
        if tag == "mfrac":
            f = ET.Element(_m("f"))
            pr = ET.SubElement(f, _m("fPr"))
            if el.get("linethickness") in _ZERO:
                _mval(pr, "type", "noBar")
            elif el.get("bevelled") == "true":
                _mval(pr, "type", "skw")
            f.append(self.arg("num", kids[0] if kids else None))
            f.append(self.arg("den", kids[1] if len(kids) > 1 else None))
            return [f]
        if tag in ("msqrt", "mroot"):
            r = ET.Element(_m("rad"))
            pr = ET.SubElement(r, _m("radPr"))
            if tag == "msqrt":
                _mval(pr, "degHide", "1")
                r.extend([ET.Element(_m("deg")), self.arg("e", kids)])
            else:
                r.extend([self.arg("deg", kids[1] if len(kids) > 1 else None), self.arg("e", kids[0] if kids else None)])
            return [r]
        if tag in ("msub", "msup", "msubsup"):
            base, sub, sup, _ = _scripts_of(tag, kids)
            s = ET.Element(_m({"msub": "sSub", "msup": "sSup", "msubsup": "sSubSup"}[tag]))
            s.append(self.arg("e", base))
            if tag != "msup":
                s.append(self.arg("sub", sub))
            if tag != "msub":
                s.append(self.arg("sup", sup))
            return [s]
        if tag in ("mover", "munder", "munderover"):
            return [self.overunder(tag, kids)]
        if tag == "mtable":
            return [self.matrix(el)]
        if tag == "menclose":
            b = ET.Element(_m("borderBox"))
            notation = el.get("notation", "box")
            if "box" not in notation:
                pr = ET.SubElement(b, _m("borderBoxPr"))
                for side in ("hideTop", "hideBot", "hideLeft", "hideRight"):
                    _mval(pr, side, "1")
                for word, prop in (("updiagonalstrike", "strikeBLTR"), ("downdiagonalstrike", "strikeTLBR"),
                                   ("horizontalstrike", "strikeH")):
                    if word in notation:
                        _mval(pr, prop, "1")
            b.append(self.arg("e", kids))
            return [b]
        if tag == "mphantom":
            p = ET.Element(_m("phant"))
            p.append(self.arg("e", kids))
            return [p]
        if tag == "mmultiscripts" and kids:
            s = ET.Element(_m("sSubSup"))
            post = [k for k in kids[1:] if _local(k.tag) not in ("mprescripts", "none")]
            s.extend([self.arg("e", kids[0]), self.arg("sub", post[0] if post else None),
                      self.arg("sup", post[1] if len(post) > 1 else None)])
            return [s]
        if tag in ("annotation", "annotation-xml", "none", "mprescripts"):
            return []
        return self.row(kids)

    def delimited(self, fp) -> ET.Element:
        o, parts, seps, c = fp
        d = ET.Element(_m("d"))
        pr = ET.SubElement(d, _m("dPr"))
        _mval(pr, "begChr", o)
        if seps:
            _mval(pr, "sepChr", seps[0])
        _mval(pr, "endChr", c)
        for part in parts:
            d.append(self.arg("e", part))
        return d

    def overunder(self, tag: str, kids: list[ET.Element]) -> ET.Element:
        base = kids[0] if kids else None
        mark = kids[1] if len(kids) > 1 else None
        ch = (mark.text or "") if mark is not None and _local(mark.tag) == "mo" else None
        if tag == "mover" and ch is not None:
            if ch == "¯":
                return self.bar("top", base)
            if ch in BRACES:
                return self.group_chr(ch, "top", base)
            if mark.get("accent") == "true" or len(ch) == 1 and unicodedata.combining(ch):
                a = ET.Element(_m("acc"))
                _mval(ET.SubElement(a, _m("accPr")), "chr", ch)
                a.append(self.arg("e", base))
                return a
        if tag == "munder" and ch is not None:
            if ch in ("_", chr(0x332)):
                return self.bar("bot", base)
            if ch in BRACES:
                return self.group_chr(ch, "bot", base)
        if tag == "munderover":
            low = ET.Element(_m("limLow"))
            low.extend([self.arg("e", base), self.arg("lim", mark)])
            up = ET.Element(_m("limUpp"))
            ET.SubElement(up, _m("e")).append(low)
            up.append(self.arg("lim", kids[2] if len(kids) > 2 else None))
            return up
        lim = ET.Element(_m("limUpp" if tag == "mover" else "limLow"))
        lim.extend([self.arg("e", base), self.arg("lim", mark)])
        return lim

    def bar(self, pos: str, base) -> ET.Element:
        b = ET.Element(_m("bar"))
        _mval(ET.SubElement(b, _m("barPr")), "pos", pos)
        b.append(self.arg("e", base))
        return b

    def group_chr(self, ch: str, pos: str, base) -> ET.Element:
        g = ET.Element(_m("groupChr"))
        pr = ET.SubElement(g, _m("groupChrPr"))
        _mval(pr, "chr", ch)
        _mval(pr, "pos", pos)
        _mval(pr, "vertJc", "bot" if pos == "top" else "top")
        g.append(self.arg("e", base))
        return g

    def matrix(self, el: ET.Element) -> ET.Element:
        rows = [tr for tr in _kids(el) if _local(tr.tag) in ("mtr", "mlabeledtr")]
        width = max((len(_kids(tr)) for tr in rows), default=1) or 1
        mm = ET.Element(_m("m"))
        pr = ET.SubElement(mm, _m("mPr"))
        aligns = []
        for k in range(width):
            found = next((_kids(tr)[k].get("columnalign") for tr in rows
                          if k < len(_kids(tr)) and _kids(tr)[k].get("columnalign")), "")
            aligns.append(found or "center")
        mcs = ET.SubElement(pr, _m("mcs"))
        for a in aligns:
            mcpr = ET.SubElement(ET.SubElement(mcs, _m("mc")), _m("mcPr"))
            _mval(mcpr, "count", "1")
            _mval(mcpr, "mcJc", a)
        for tr in rows:
            mr = ET.SubElement(mm, _m("mr"))
            tds = _kids(tr)
            for k in range(width):
                mr.append(self.arg("e", _kids(tds[k]) if k < len(tds) else None))
        return mm


def to_omml(math: ET.Element) -> ET.Element:
    om = ET.Element(_m("oMath"))
    om.extend(_ToOmml().row(presentation(math) if _local(math.tag) == "math" else [math]))
    if math.get("display") == "block":
        para = ET.Element(_m("oMathPara"))
        para.append(om)
        return para
    return om


def _child(el: ET.Element | None, tag: str) -> ET.Element | None:
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == tag:
            return c
    return None


def _val(el: ET.Element | None, tag: str) -> str | None:
    c = _child(el, tag)
    if c is None:
        return None
    for k, v in c.attrib.items():
        if _local(k) == "val":
            return v
    return "1"


def _prop(el: ET.Element, pr: str, tag: str) -> str | None:
    return _val(_child(el, pr), tag)


def _on(v: str | None) -> bool:
    return v in ("1", "on", "true")


_OMML_PROPS = {"rPr", "ctrlPr", "fPr", "radPr", "sSubPr", "sSupPr", "sSubSupPr", "naryPr", "dPr", "accPr", "barPr",
               "groupChrPr", "limLowPr", "limUppPr", "funcPr", "mPr", "eqArrPr", "boxPr", "borderBoxPr", "phantPr",
               "sPrePr", "oMathParaPr", "argPr"}


class _FromOmml:
    def __init__(self):
        self.depth = 0

    def items(self, el: ET.Element | None) -> list[ET.Element]:
        if el is None:
            return []
        self.depth += 1
        try:
            if self.depth > 150:
                return []
            out: list[ET.Element] = []
            for c in el:
                out += self.el(c)
            return out
        finally:
            self.depth -= 1

    def one(self, el: ET.Element | None) -> ET.Element:
        items = self.items(el)
        return _row(items) if items else _el("mrow")

    def el(self, el: ET.Element) -> list[ET.Element]:
        tag = _local(el.tag)
        e = _child(el, "e")
        if tag == "r":
            return self.run(el)
        if tag in _OMML_PROPS:
            return []
        if tag == "f":
            frac = _el("mfrac")
            frac.extend([self.one(_child(el, "num")), self.one(_child(el, "den"))])
            typ = _prop(el, "fPr", "type")
            if typ == "noBar":
                frac.set("linethickness", "0")
            elif typ in ("skw", "lin"):
                frac.set("bevelled", "true")
            return [frac]
        if tag == "rad":
            deg = _child(el, "deg")
            if _on(_prop(el, "radPr", "degHide")) or not self.items(deg):
                r = _el("msqrt")
                r.append(self.one(e))
                return [r]
            r = _el("mroot")
            r.extend([self.one(e), self.one(deg)])
            return [r]
        if tag in ("sSub", "sSup", "sSubSup"):
            s = _el({"sSub": "msub", "sSup": "msup", "sSubSup": "msubsup"}[tag])
            s.append(self.one(e))
            if tag != "sSup":
                s.append(self.one(_child(el, "sub")))
            if tag != "sSub":
                s.append(self.one(_child(el, "sup")))
            return [s]
        if tag == "sPre":
            s = _el("mmultiscripts")
            s.extend([self.one(e), _el("mprescripts"), self.one(_child(el, "sub")), self.one(_child(el, "sup"))])
            return [s]
        if tag == "nary":
            return self.nary(el, e)
        if tag == "d":
            beg, end, sep = (_prop(el, "dPr", t) for t in ("begChr", "endChr", "sepChr"))
            parts = [self.items(x) for x in el if _local(x.tag) == "e"]
            items: list[ET.Element] = []
            for k, part in enumerate(parts):
                if k and (sep if sep is not None else "|"):
                    items.append(_el("mo", sep if sep is not None else "|", stretchy="true", form="infix"))
                items += part
            return [_fence("(" if beg is None else beg, items, ")" if end is None else end)]
        if tag == "m":
            mcs = _child(_child(el, "mPr"), "mcs")
            aligns = []
            for mc in list(mcs) if mcs is not None else []:
                if _local(mc.tag) == "mc":
                    count = _val(_child(mc, "mcPr"), "count") or "1"
                    aligns += [_val(_child(mc, "mcPr"), "mcJc") or "center"] * min(1 if _whole(count) is None else _whole(count), 1000)
            rows = [[self.items(x) for x in mr if _local(x.tag) == "e"] for mr in el if _local(mr.tag) == "mr"]
            return [_mtable(rows, aligns or ["center"])]
        if tag == "eqArr":
            rows = []
            for x in (c for c in el if _local(c.tag) == "e"):
                cells: list[list[ET.Element]] = [[]]
                for item in self.items(x):
                    if _local(item.tag) == "mo" and item.text == "&":
                        cells.append([])
                    else:
                        cells[-1].append(item)
                rows.append(cells)
            alternate = any(len(r) > 1 for r in rows)
            return [_mtable(rows, ["right", "left"] if alternate else ["center"], alternate, displaystyle=True)]
        if tag == "acc":
            ch = _prop(el, "accPr", "chr")
            o = _el("mover")
            o.extend([self.one(e), _el("mo", chr(0x302) if ch is None else ch, accent="true")])
            return [o]
        if tag == "bar":
            top = (_prop(el, "barPr", "pos") or "bot") == "top"
            o = _el("mover" if top else "munder")
            o.extend([self.one(e), _el("mo", "¯" if top else "_", accent="true")])
            return [o]
        if tag == "groupChr":
            ch = _prop(el, "groupChrPr", "chr") or "⏟"
            top = (_prop(el, "groupChrPr", "pos") or "bot") == "top"
            o = _el("mover" if top else "munder")
            o.extend([self.one(e), _el("mo", ch, accent="true")])
            return [o]
        if tag in ("limLow", "limUpp"):
            inner = [c for c in (e if e is not None else []) if _local(c.tag) not in _OMML_PROPS]
            if tag == "limUpp" and len(inner) == 1 and _local(inner[0].tag) == "limLow":
                low = inner[0]
                o = _el("munderover")
                o.extend([self.one(_child(low, "e")), self.one(_child(low, "lim")), self.one(_child(el, "lim"))])
                return [o]
            o = _el("munder" if tag == "limLow" else "mover")
            o.extend([self.one(e), self.one(_child(el, "lim"))])
            return [o]
        if tag == "func":
            name = self.items(_child(el, "fName"))
            body = self.items(e)
            return [_row([*name, _el("mo", APPLY)])] + ([_row(body)] if body else [])
        if tag == "borderBox":
            notes = [word for word, prop in (("updiagonalstrike", "strikeBLTR"), ("downdiagonalstrike", "strikeTLBR"),
                                             ("horizontalstrike", "strikeH")) if _on(_prop(el, "borderBoxPr", prop))]
            if not all(_on(_prop(el, "borderBoxPr", s)) for s in ("hideTop", "hideBot", "hideLeft", "hideRight")):
                notes.insert(0, "box")
            o = _el("menclose", notation=" ".join(notes) or "box")
            o.append(self.one(e))
            return [o]
        if tag == "phant":
            o = _el("mphantom")
            o.append(self.one(e))
            return [o]
        if tag == "t":
            return _tokens(el.text or "", "")
        return self.items(el)

    def nary(self, el: ET.Element, e: ET.Element | None) -> list[ET.Element]:
        ch = _prop(el, "naryPr", "chr")
        ch = "∫" if ch is None else ch
        loc = _prop(el, "naryPr", "limLoc") or ("subSup" if ch in INTEGRALS else "undOvr")
        sub = None if _on(_prop(el, "naryPr", "subHide")) else self.items(_child(el, "sub")) or None
        sup = None if _on(_prop(el, "naryPr", "supHide")) else self.items(_child(el, "sup")) or None
        head = _el("mo", ch)
        if sub or sup:
            under = loc == "undOvr"
            tag = ("munderover" if under else "msubsup") if sub and sup else \
                ("munder" if under else "msub") if sub else ("mover" if under else "msup")
            op = head
            head = _el(tag)
            head.append(op)
            head.extend([_row(x) for x in (sub, sup) if x])
        body = self.items(e)
        return [head] + ([_row(body)] if body else [])

    def run(self, r: ET.Element) -> list[ET.Element]:
        text = "".join(t.text or "" for t in r if _local(t.tag) == "t")
        if not text:
            return []
        rpr = _child(r, "rPr")
        nor = rpr is not None and _child(rpr, "nor") is not None
        scr = _val(rpr, "scr") or ""
        sty = _val(rpr, "sty") or ""
        variant = _OMML_VARIANT.get((scr, sty or ("p" if scr else "")), "") if scr or sty else ""
        if nor:
            variant = "" if variant == "normal" else variant
            return [_el("mtext", styled(text, variant), mathvariant=variant) if variant else _el("mtext", text)]
        return _tokens(text, variant)


def _tokens(text: str, variant: str) -> list[ET.Element]:
    """Math text split the way a reader takes it: digits together, letters one by one (or as one
    word when upright), spaces as mspace, the rest operators."""
    if text in _SPACE_WIDTH:
        return [_mspace(_SPACE_WIDTH[text])]
    out: list[ET.Element] = []
    k, n = 0, len(text)
    while k < n:
        ch = text[k]
        if ch.isascii() and ch.isdigit():
            j = k + 1
            while j < n and (text[j].isascii() and text[j].isdigit() or text[j] == "." and j + 1 < n
                             and text[j + 1].isascii() and text[j + 1].isdigit()):
                j += 1
            num = text[k:j]
            s = styled(num, variant) if variant not in ("", "normal", "italic") else num
            out.append(_el("mn", s, mathvariant=variant) if s != num else _el("mn", num))
            k = j
            continue
        if ch.isalpha() and ch not in MI_CHARS:
            if variant == "normal":
                j = k + 1
                while j < n and text[j].isalpha() and text[j] not in MI_CHARS:
                    j += 1
                out.append(_el("mi", text[k:j], mathvariant="normal"))
                k = j
                continue
            s = styled(ch, variant) if variant else ch
            out.append(_el("mi", s, mathvariant=variant) if s != ch else _el("mi", ch))
            k += 1
            continue
        if ch in ALL_SPACES:
            j = k + 1
            while j < n and text[j] in ALL_SPACES:
                j += 1
            out.append(_mspace(_SPACE_WIDTH.get(text[k:j], "0.167em")))
            k = j
            continue
        if not ch.isspace():
            out.append(_el("mi", ch) if ch in MI_CHARS else _el("mn", ch) if ch.isdigit() else _mo(ch))
        k += 1
    return out


def from_omml(el: ET.Element) -> ET.Element:
    """Word's m:oMath (inline) or m:oMathPara (display) to MathML, with a TeX annotation."""
    conv = _FromOmml()
    display = _local(el.tag) == "oMathPara"
    if display:
        maths = [c for c in el if _local(c.tag) == "oMath"]
        if len(maths) > 1:
            items = [_mtable([[conv.items(om)] for om in maths], ["center"], displaystyle=True)]
        else:
            items = conv.items(maths[0]) if maths else []
    else:
        items = conv.items(el)
    math = ET.Element("math", {"display": "block" if display else "inline"})
    sem = ET.SubElement(math, "semantics")
    sem.append(_row(items) if items else _el("mrow"))
    ET.SubElement(sem, "annotation", {"encoding": TEX}).text = to_tex_walk(math)
    return math


# --- Typst math -----------------------------------------------------------------------------------

TYPST_SYM = _table("""
alpha α beta β gamma γ delta δ epsilon ε epsilon.alt ϵ zeta ζ eta η theta θ theta.alt ϑ iota ι kappa κ kappa.alt ϰ
lambda λ mu μ nu ν xi ξ omicron ο pi π pi.alt ϖ rho ρ rho.alt ϱ sigma σ sigma.alt ς tau τ upsilon υ phi φ phi.alt ϕ
chi χ psi ψ omega ω digamma ϝ Alpha Α Beta Β Gamma Γ Delta Δ Epsilon Ε Zeta Ζ Eta Η Theta Θ Iota Ι Kappa Κ
Lambda Λ Mu Μ Nu Ν Xi Ξ Omicron Ο Pi Π Rho Ρ Sigma Σ Tau Τ Upsilon Υ Phi Φ Chi Χ Psi Ψ Omega Ω
infinity ∞ oo ∞ partial ∂ diff ∂ nabla ∇ gradient ∇ emptyset ∅ nothing ∅ ell ℓ planck.reduce ℏ hbar ℏ planck ℎ
aleph ℵ beth ℶ gimel ℷ top ⊤ bot ⊥ angle ∠ dots.h … dots.h.c ⋯ dots.c ⋯ dots.v ⋮ dots.down ⋱ dots … ellipsis …
square □ qed ∎ wp ℘ Re ℜ Im ℑ dotless.i ı dotless.j ȷ degree ° triangle △ triangle.stroked.t △ diamond ◇
lozenge ◊ star.filled ★ square.filled ■ checkmark ✓ complement ∁
plus + minus − times × div ÷ dot ⋅ dot.op ⋅ dot.c · ast ∗ ast.op ∗ star ⋆ star.op ⋆ compose ∘ circle.small ∘
bullet • plus.minus ± minus.plus ∓ plus.circle ⊕ minus.circle ⊖ times.circle ⊗ dot.circle ⊙ div.circle ⊘ union ∪
sect ∩ union.sq ⊔ sect.sq ⊓ union.plus ⊎ and ∧ or ∨ not ¬ without ∖ backslash \\ wreath ≀ times.l ⋉ times.r ⋊
dagger † dagger.double ‡ plus.square ⊞ minus.square ⊟ times.square ⊠ dot.square ⊡ xor ⊕
eq = eq.not ≠ lt < gt > lt.eq ≤ gt.eq ≥ lt.not ≮ gt.not ≯ lt.eq.not ≰ gt.eq.not ≱ lt.double ≪ gt.double ≫
lt.triple ⋘ gt.triple ⋙ lt.tilde ≲ gt.tilde ≳ lt.eq.slant ⩽ gt.eq.slant ⩾ approx ≈ approx.eq ≊ approx.not ≉
equiv ≡ equiv.not ≢ prop ∝ tilde.op ∼ tilde.eq ≃ tilde.equiv ≅ tilde.not ≁ tilde.equiv.not ≇ colon.eq ≔ eq.colon ≕
eq.def ≝ eq.delta ≜ eq.dots ≑ in ∈ in.not ∉ in.rev ∋ in.rev.not ∌ subset ⊂ subset.eq ⊆ subset.neq ⊊ subset.not ⊄
subset.eq.not ⊈ supset ⊃ supset.eq ⊇ supset.neq ⊋ supset.not ⊅ supset.eq.not ⊉ subset.sq ⊏ supset.sq ⊐
subset.eq.sq ⊑ supset.eq.sq ⊒ perp ⟂ parallel ∥ parallel.not ∦ divides ∣ divides.not ∤ mid ∣ tack.r ⊢ tack.l ⊣
tack.r.double ⊨ models ⊨ forces ⊩ prec ≺ succ ≻ prec.eq ⪯ succ.eq ⪰ prec.tilde ≾ succ.tilde ≿ asymp ≍ join ⨝
smile ⌣ frown ⌢ therefore ∴ because ∵ forall ∀ exists ∃ exists.not ∄ lt.tri ⊲ gt.tri ⊳ lt.tri.eq ⊴ gt.tri.eq ⊵
arrow.r → arrow.l ← arrow.l.r ↔ arrow.t ↑ arrow.b ↓ arrow.t.b ↕ arrow.r.double ⇒ arrow.l.double ⇐
arrow.l.r.double ⇔ arrow.t.double ⇑ arrow.b.double ⇓ arrow.r.long ⟶ arrow.l.long ⟵ arrow.l.r.long ⟷
arrow.r.double.long ⟹ arrow.l.double.long ⟸ arrow.l.r.double.long ⟺ arrow.r.bar ↦ arrow.r.long.bar ⟼
arrow.r.hook ↪ arrow.l.hook ↩ arrow.r.squiggly ⇝ arrow.r.twohead ↠ arrow.l.twohead ↞ arrow.r.tail ↣ arrow.tr ↗
arrow.br ↘ arrow.bl ↙ arrow.tl ↖ arrow.r.not ↛ arrow.l.not ↚ arrow.ccw ↺ arrow.cw ↻ harpoon.rt ⇀ harpoon.rb ⇁
harpoon.lt ↼ harpoon.lb ↽ harpoons.rtlb ⇌ harpoons.ltrb ⇋ arrows.rr ⇉ arrows.ll ⇇ arrows.rl ⇄ arrows.lr ⇆
arrow.r.double.not ⇏ arrow.l.r.not ↮ arrow.r.dashed ⇢ multimap ⊸
sum ∑ product ∏ product.co ∐ integral ∫ integral.double ∬ integral.triple ∭ integral.quad ⨌ integral.cont ∮
integral.surf ∯ integral.vol ∰ union.big ⋃ sect.big ⋂ union.sq.big ⨆ and.big ⋀ or.big ⋁ plus.circle.big ⨁
times.circle.big ⨂ dot.circle.big ⨀ union.plus.big ⨄
paren.l ( paren.r ) bracket.l [ bracket.r ] brace.l { brace.r } angle.l ⟨ angle.r ⟩ chevron.l ⟨ chevron.r ⟩
bar.v | bar.v.double ‖ bar.h ― floor.l ⌊ floor.r ⌋ ceil.l ⌈ ceil.r ⌉ bracket.l.double ⟦ bracket.r.double ⟧
corner.l.t ⌜ corner.r.t ⌝ corner.l.b ⌞ corner.r.b ⌟ prime ′ prime.double ″ prime.triple ‴ prime.rev ‵
hash # percent % amp & at @ colon : comma , semi ; slash / excl ! quest ? tilde ~ dollar $ dash.en – dash.em —
""")
TYPST_SYM.update({"bar": "|", "quote.double": "\""})
TYPST_STRUCK = {"RR": "ℝ", "NN": "ℕ", "ZZ": "ℤ", "QQ": "ℚ", "CC": "ℂ", "HH": "ℍ", "PP": "ℙ", "FF": "𝔽", "EE": "𝔼"}
TYPST_SPACES = {"thin": "0.167em", "med": "0.222em", "thick": "0.278em", "quad": "1em", "wide": "2em", "space": "0.333em",
                "space.thin": "0.167em", "space.med": "0.222em", "space.quad": "1em", "space.en": "0.5em",
                "space.nobreak": "0.333em"}
TYPST_SHORT = {"<==>": "⟺", "<-->": "⟷", "==>": "⟹", "<==": "⟸", "-->": "⟶", "<--": "⟵", "|->": "↦", "<=>": "⇔",
               "<->": "↔", "->>": "↠", "<<-": "↞", ">->": "↣", "~~>": "⟿", "<<<": "⋘", ">>>": "⋙", "::=": "⩴",
               "...": "…", "[|": "⟦", "|]": "⟧", "->": "→", "<-": "←", "=>": "⇒", "<=": "≤", ">=": "≥", "!=": "≠",
               ":=": "≔", "<<": "≪", ">>": "≫", "~>": "⇝", "||": "‖", "-": "−"}
TYPST_ACCENTS = {"hat": chr(0x302), "tilde": chr(0x303), "macron": "‾", "bar": "‾", "breve": chr(0x306),
                 "acute": chr(0x301), "grave": chr(0x300), "caron": chr(0x30C), "dot": chr(0x307),
                 "dot.double": chr(0x308), "diaer": chr(0x308), "dot.triple": chr(0x20DB), "dot.quad": chr(0x20DC),
                 "circle": chr(0x30A), "arrow": "→", "arrow.l": "←", "arrow.l.r": "↔", "harpoon": "⇀", "harpoon.lt": "↼"}
TYPST_OVERUNDER = {"overline": ("¯", "over"), "underline": ("_", "under"), "overbrace": ("⏞", "over"),
                   "underbrace": ("⏟", "under"), "overbracket": ("⎴", "over"), "underbracket": ("⎵", "under"),
                   "overparen": ("⏜", "over"), "underparen": ("⏝", "under")}
TYPST_FENCES = {"abs": ("|", "|"), "norm": ("‖", "‖"), "floor": ("⌊", "⌋"), "ceil": ("⌈", "⌉"), "round": ("⌊", "⌉")}
TYPST_VARIANTS = {"upright": "normal", "italic": "italic", "bold": "bold", "bb": "double-struck", "cal": "script",
                  "scr": "script", "frak": "fraktur", "sans": "sans-serif", "mono": "monospace", "serif": ""}
TYPST_FUNCTIONS = FUNCTIONS | {"lcm", "mod", "tr", "id", "im", "sgn", "arccot", "ctg", "tg", "csch", "sech"}
_COMBINE = {("bold", "italic"): "bold-italic", ("bold", "script"): "bold-script", ("bold", "fraktur"): "bold-fraktur",
            ("bold", "sans-serif"): "bold-sans-serif", ("italic", "sans-serif"): "sans-serif-italic",
            ("bold-italic", "sans-serif"): "sans-serif-bold-italic", ("italic", "bold-sans-serif"): "sans-serif-bold-italic",
            ("bold", "sans-serif-italic"): "sans-serif-bold-italic"}

_TYP = re.compile(r'"(?:[^"\\]|\\.)*"?|//[^\n]*|/\*(?:[^*]|\*(?!/))*(?:\*/)?|\\u\{[0-9A-Fa-f]{1,6}\}|\\(?=\s)|\\.|\\'
                  r"|#[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*|#[({\[]|\d+(?:\.\d+)?|[A-Za-z]+(?:\.[A-Za-z]+)*"
                  r"|<==>|<-->|==>|<==|-->|<--|\|->|<=>|<->|->>|<<-|>->|~~>|<<<|>>>|::=|\.\.\.|\[\||\|\]"
                  r"|->|<-|=>|<=|>=|!=|:=|<<|>>|~>|\|\||\s+|.", re.DOTALL)
_TYP_CLOSE = {"(": ")", "[": "]", "{": "}"}
_TYP_NAMED = {"delim", "gap", "augment", "size", "t", "b", "tl", "tr", "bl", "br", "limits", "align", "column-gap",
              "row-gap", "inverted", "cross", "angle", "stroke", "reverse", "length"}


def _variant_of(outer: str, inner: str) -> str:
    if not inner or inner == outer:
        return outer
    if not outer:
        return inner
    return _COMBINE.get((outer, inner)) or _COMBINE.get((inner, outer)) or outer


def _restyle(items: list[ET.Element], variant: str) -> None:
    for el in items:
        for t in el.iter():
            if _local(t.tag) in ("mi", "mn", "mtext") and t.text:
                base, cur = plain(t.text)
                cur = t.get("mathvariant", "") or cur
                v = _variant_of(variant, cur if cur != "normal" else "")
                if v == "normal":
                    if _local(t.tag) != "mn":
                        t.text = base
                        if _local(t.tag) == "mi":
                            t.set("mathvariant", "normal")
                        else:
                            t.attrib.pop("mathvariant", None)
                    continue
                s = styled(base, v) if v else base
                t.text = s
                if v and s != base:
                    t.set("mathvariant", v)
                else:
                    t.attrib.pop("mathvariant", None)


class _TypstParser:
    def __init__(self, src: str, display: bool, depth: int = 0):
        self.toks = [t for t in _TYP.findall(src) if not t.startswith(("//", "/*"))]
        if len(self.toks) > MAX_TOKENS:
            raise _TooComplex
        self.i = 0
        self.display = display
        self.depth = depth
        self.kind: dict[int, str] = {}
        self.keep: list[ET.Element] = []

    def peek(self, spaces: bool = False) -> str | None:
        while self.i < len(self.toks):
            t = self.toks[self.i]
            if not spaces and t.isspace():
                self.i += 1
                continue
            return t
        return None

    def mark(self, el: ET.Element, kind: str) -> ET.Element:
        self.keep.append(el)
        self.kind[id(el)] = kind
        return el

    def nest(self, step: int) -> None:
        self.depth += step
        if self.depth > MAX_NEST:
            raise _TooComplex

    def body(self) -> list[ET.Element]:
        rows: list[list[list[ET.Element]]] = [[[]]]
        while True:
            rows[-1][-1] += self.row(frozenset({"&", "\\"}))
            t = self.peek()
            if t is None:
                break
            self.i += 1
            if t == "&":
                rows[-1].append([])
            elif t == "\\":
                rows.append([[]])
        if len(rows) > 1 and rows[-1] == [[]]:
            rows.pop()
        if len(rows) == 1 and len(rows[0]) == 1:
            return rows[0][0]
        if any(len(r) > 1 for r in rows):
            return [_mtable(rows, ["right", "left"], alternate=True, displaystyle=True)]
        return [_mtable(rows, ["center"], displaystyle=True)]

    def row(self, stops: frozenset) -> list[ET.Element]:
        out: list[ET.Element] = []
        while True:
            t = self.peek()
            if t is None or t in stops:
                return out
            if t == "/" and out:
                self.i += 1
                num = out.pop()
                den = self.scripted(stops)
                frac = _el("mfrac")
                frac.extend([_unparen(num), _unparen(den) if den is not None else _el("mrow")])
                out.append(frac)
                continue
            el = self.scripted(stops)
            if el is not None:
                out.append(el)

    def scripted(self, stops: frozenset) -> ET.Element | None:
        t = self.peek()
        if t is None or t in stops:
            return None
        if t in ("_", "^"):
            base = _el("mrow")
        else:
            self.i += 1
            base = self.atom(t, stops)
            if base is None:
                return None
        sub = sup = None
        primes = 0
        while True:
            t = self.peek(spaces=True)
            if t == "'" and sup is None:
                self.i += 1
                primes += 1
            elif t in ("_", "^") and (sub if t == "_" else sup) is None:
                self.i += 1
                self.nest(1)
                nxt = self.peek()
                arg = None
                if nxt is not None and nxt not in stops:
                    self.i += 1
                    arg = self.atom(nxt, stops, script=True)
                self.nest(-1)
                arg = _unparen(arg) if arg is not None else _el("mrow")
                if t == "_":
                    sub = arg
                else:
                    sup = arg
            else:
                break
        if primes:
            p = _el("mo", {1: "′", 2: "″", 3: "‴", 4: "⁗"}.get(primes, "′" * primes))
            sup = p if sup is None else _row([p, sup])
        if sub is None and sup is None:
            return base
        kind = self.kind.get(id(base), "")
        under = kind == "always" or kind == "display" and self.display
        tag = ("munderover" if under else "msubsup") if sub is not None and sup is not None else \
            ("munder" if under else "msub") if sub is not None else ("mover" if under else "msup")
        el = _el(tag)
        el.append(base)
        el.extend([x for x in (sub, sup) if x is not None])
        return el

    def atom(self, t: str, stops: frozenset, script: bool = False) -> ET.Element | None:
        if t.isspace():
            return None
        if t.startswith('"'):
            body = re.sub(r"\\(.)", r"\1", t[1:-1] if len(t) > 1 and t.endswith('"') else t[1:])
            if not body:
                return _el("mrow")
            return _el("mtext", re.sub(r"^ | $", NBSP, body))
        if t[0].isdigit():
            return _el("mn", t)
        if t[0].isascii() and t[0].isalpha():
            return self.ident(t, stops)
        if t in _TYP_CLOSE:
            return self.group(t)
        if t.startswith("#"):
            return self.code(t)
        if t.startswith("\\u{"):
            return _tok(chr(min(int(t[3:-1], 16), 0x10FFFF)))
        if t.startswith("\\") and len(t) == 2:
            return _tok(t[1]) if not t[1].isspace() else None
        if t in TYPST_SHORT:
            return _el("mo", TYPST_SHORT[t])
        if t in BIG_CHARS:
            return self.mark(_el("mo", t), "side" if t in INTEGRALS else "display")
        return _tok(t)

    def code(self, t: str) -> ET.Element:
        text = t
        if t == "#h" and self.i < len(self.toks) and self.toks[self.i] == "(":
            arg = self.raw_balanced()
            m = re.fullmatch(r"\(\s*(-?\d*\.?\d+\s*(?:em|pt|mm|cm|in))\s*\)", arg)
            return _mspace(_em(m.group(1))) if m else _el("mtext", t + arg, **{"class": "raw"})
        if t in ("#(", "#{", "#["):
            close = _TYP_CLOSE[t[1]]
            depth = 1
            while self.i < len(self.toks) and depth:
                x = self.toks[self.i]
                self.i += 1
                text += x
                if x == t[1]:
                    depth += 1
                elif x == close:
                    depth -= 1
        elif self.i < len(self.toks) and self.toks[self.i] in ("(", "["):
            text += self.raw_balanced()
        return _el("mtext", text, **{"class": "raw"})

    def raw_balanced(self) -> str:
        """The raw text of the bracketed groups that follow (a code call's arguments)."""
        out = ""
        while self.i < len(self.toks) and self.toks[self.i] in ("(", "["):
            open_ = self.toks[self.i]
            depth = 0
            while self.i < len(self.toks):
                x = self.toks[self.i]
                self.i += 1
                out += x
                if x == open_:
                    depth += 1
                elif x == _TYP_CLOSE[open_]:
                    depth -= 1
                    if not depth:
                        break
        return out

    def group(self, open_: str) -> ET.Element:
        close = _TYP_CLOSE[open_]
        start = self.i
        self.nest(1)
        items = self.row(frozenset({close, "&", "\\"}) if open_ != "(" else frozenset({close}))
        self.nest(-1)
        if self.peek() == close:
            self.i += 1
            return _fence(open_, items, close)
        if not items and self.i == start:
            return _mo(open_)
        r = _el("mrow")
        r.append(_mo(open_))
        r.extend(items)
        return r

    def ident(self, name: str, stops: frozenset) -> ET.Element | None:
        call = self.i < len(self.toks) and self.toks[self.i] == "("
        if call:
            self.i += 1
            self.nest(1)
            try:
                return self.call(name)
            finally:
                self.nest(-1)
        if len(name) == 1:
            return _el("mi", name)
        if name in TYPST_SYM:
            return _tok(TYPST_SYM[name], capital_upright=True, mark=self)
        if name in TYPST_STRUCK:
            return _el("mi", TYPST_STRUCK[name], mathvariant="double-struck")
        if name in TYPST_SPACES:
            return _mspace(TYPST_SPACES[name])
        if name in TYPST_FUNCTIONS:
            el = _func(name)
            return self.mark(el, "display") if name in LIMIT_FUNCTIONS else el
        if "." in name:
            parts = name.split(".")
            for k in range(len(parts) - 1, 0, -1):
                head = ".".join(parts[:k])
                if head in TYPST_SYM or len(head) == 1:
                    first = _tok(TYPST_SYM[head], capital_upright=True) if head in TYPST_SYM else _el("mi", head)
                    rest = [x for p in parts[k:] for x in (_el("mo", "."), self.ident(p, stops)) if x is not None]
                    return _row([first, *rest])
        return _el("mi", name, mathvariant="normal")

    def args(self) -> tuple[list[list[list[ET.Element]]], dict[str, str | list[ET.Element]], list[int]]:
        """After "(": positional arguments (each a list of &-separated cells), named ones, and where
        each ";" (a matrix row break) came."""
        pos: list[list[list[ET.Element]]] = []
        named: dict[str, str | list[ET.Element]] = {}
        breaks: list[int] = []
        while True:
            t = self.peek()
            if t is None:
                break
            if t == ")":
                self.i += 1
                break
            if t in (",", ";"):
                self.i += 1
                if t == ";":
                    breaks.append(len(pos))
                continue
            if t in _TYP_NAMED and self.i + 1 < len(self.toks) and self.toks[self.i + 1] == ":":
                self.i += 2
                v = self.peek()
                if v is not None and v.startswith(('"', "#")):
                    self.i += 1
                    if v.startswith("#") and self.i < len(self.toks) and self.toks[self.i] in ("(", "["):
                        v += self.raw_balanced()
                    named[t] = v.strip('"')
                else:
                    named[t] = self.row(frozenset({",", ";", ")"}))
                continue
            cells: list[list[ET.Element]] = [[]]
            while True:
                cells[-1] += self.row(frozenset({",", ";", ")", "&"}))
                if self.peek() == "&":
                    self.i += 1
                    cells.append([])
                    continue
                break
            pos.append(cells)
        return pos, named, breaks

    def call(self, name: str) -> ET.Element:
        pos, named, breaks = self.args()
        flat = [[x for cell in a for x in cell] for a in pos]
        first = flat[0] if flat else []
        one = _row(first) if first else _el("mrow")
        if name == "frac" and len(flat) >= 2:
            f = _el("mfrac")
            f.extend([one, _row(flat[1])])
            return f
        if name == "binom" and len(flat) >= 2:
            f = _el("mfrac", linethickness="0")
            f.extend([one, _row(flat[1])])
            return _fence("(", [f], ")")
        if name == "sqrt":
            r = _el("msqrt")
            r.extend(first)
            return r
        if name == "root" and len(flat) >= 2:
            r = _el("mroot")
            r.extend([_row(flat[1]), one])
            return r
        if name in ("vec", "mat", "cases"):
            delim = named.get("delim", "{" if name == "cases" else "(")
            delim = delim if isinstance(delim, str) else "("
            o, c = {"(": ("(", ")"), "[": ("[", "]"), "{": ("{", "}"), "|": ("∣", "∣"), "||": ("∥", "∥"),
                    "#none": ("", ""), "none": ("", ""), "⟨": ("⟨", "⟩"), "<": ("⟨", "⟩")}.get(delim, ("(", ")"))
            if name == "cases":
                rows = [a for a in pos]
                table = _mtable(rows, ["left", "left"])
                rev = named.get("reverse") in ("#true", "true")
                return _fence("" if rev else o or "{", [table], ("}" if c in ("", ")") else c) if rev else "")
            if name == "vec":
                table = _mtable([[x] for x in flat], ["center"])
            else:
                rows, start = [], 0
                for b in [*breaks, len(flat)]:
                    if b > start or rows or b == len(flat):
                        rows.append(flat[start:b])
                    start = b
                rows = [r for r in rows if r] or [[]]
                table = _mtable(rows, ["center"])
            return _fence(o, [table], c) if o or c else table
        if name in TYPST_FENCES:
            o, c = TYPST_FENCES[name]
            return _fence(o, first, c)
        if name == "lr":
            items = first
            if items and _local(items[0].tag) == "mrow" and len(items) == 1 and _fence_parts(items[0]) is not None:
                return items[0]
            o = items[0].text if items and _local(items[0].tag) == "mo" and items[0].text in OPEN | BARS else ""
            c = items[-1].text if len(items) > (1 if o else 0) and _local(items[-1].tag) == "mo" \
                and items[-1].text in CLOSE | BARS else ""
            inner = items[1 if o else 0:len(items) - 1 if c else len(items)]
            return _fence(o or "", inner, c or "")
        if name == "mid":
            return _el("mo", first[0].text or "|", stretchy="true", form="infix") if first and first[0].text else _el("mo", "|")
        if name in TYPST_ACCENTS or name == "accent":
            ch = TYPST_ACCENTS.get(name, "")
            if name == "accent" and len(flat) > 1:
                ch = "".join(x.text or "" for x in flat[1])
                ch = TYPST_ACCENTS.get(ch, ch)
            el = _el("mover")
            el.extend([one, _el("mo", ch, accent="true")])
            return el
        if name in TYPST_OVERUNDER:
            ch, where = TYPST_OVERUNDER[name]
            el = _el("mover" if where == "over" else "munder")
            el.extend([one, _el("mo", ch, accent="true")])
            if len(flat) > 1 and ch not in ("¯", "_"):
                outer = _el(el.tag)
                outer.extend([el, _row(flat[1])])
                return outer
            return self.mark(el, "always") if ch in BRACES else el
        if name in TYPST_VARIANTS:
            if name == "upright" and len(first) == 1 and _local(first[0].tag) == "mtext":
                return _el("mi", first[0].text or "", mathvariant="normal")
            _restyle(first, TYPST_VARIANTS[name])
            return one
        if name == "op":
            text = "".join(x.text or "" for x in first)
            el = _func(text or "?")
            return self.mark(el, "display") if named.get("limits") in ("#true", "true") else el
        if name in ("display", "inline", "script", "sscript"):
            st = _el("mstyle", **({"displaystyle": "true" if name == "display" else "false"} if name in ("display", "inline")
                                  else {"scriptlevel": "1" if name == "script" else "2"}))
            st.extend(first)
            return st
        if name == "cancel":
            n = "updiagonalstrike downdiagonalstrike" if named.get("cross") in ("#true", "true") else \
                "downdiagonalstrike" if named.get("inverted") in ("#true", "true") else "updiagonalstrike"
            el = _el("menclose", notation=n)
            el.extend(first)
            return el
        if name in ("limits", "scripts"):
            return self.mark(one, "always" if name == "limits" else "side")
        if name == "attach":
            sub = named.get("b") or named.get("br")
            sup = named.get("t") or named.get("tr")
            sub = _row(sub) if isinstance(sub, list) and sub else None
            sup = _row(sup) if isinstance(sup, list) and sup else None
            if sub is None and sup is None:
                return one
            kind = self.kind.get(id(one), "")
            under = kind == "always" or kind == "display" and self.display
            tag = ("munderover" if under else "msubsup") if sub is not None and sup is not None else \
                ("munder" if under else "msub") if sub is not None else ("mover" if under else "msup")
            el = _el(tag)
            el.append(one)
            el.extend([x for x in (sub, sup) if x is not None])
            return el
        if name == "hide":
            el = _el("mphantom")
            el.extend(first)
            return el
        if name in ("class", "stretch", "box", "block", "text", "math.equation", "equation"):
            items = flat[-1] if flat else []
            return _row(items) if items else _el("mrow")
        # anything else reads as a function applied to its arguments: f(x, y)
        head = _el("mi", name) if len(name) == 1 else _func(name) if name in TYPST_FUNCTIONS else \
            _tok(TYPST_SYM[name], capital_upright=True) if name in TYPST_SYM else _el("mi", name, mathvariant="normal")
        inner: list[ET.Element] = []
        for k, a in enumerate(flat):
            if k:
                inner.append(_el("mo", ","))
            inner += a
        return _row([head, _fence("(", inner, ")")])


def _tok(ch: str, capital_upright: bool = False, mark: _TypstParser | None = None) -> ET.Element:
    if ch in BIG_CHARS:
        el = _el("mo", ch)
        return mark.mark(el, "side" if ch in INTEGRALS else "display") if mark is not None else el
    if ch in TEX_UPPER_GREEK.values() and capital_upright:
        return _el("mi", ch, mathvariant="normal")
    if ch in MI_CHARS or ch.isalpha():
        return _el("mi", ch)
    if ch.isdigit():
        return _el("mn", ch)
    return _mo(ch)


def _unparen(el: ET.Element) -> ET.Element:
    """A fraction's or script's argument loses the parentheses that grouped it."""
    fp = _fence_parts(el)
    if fp is not None and fp[0] == "(" and fp[3] == ")" and not fp[2]:
        return _row(fp[1][0]) if fp[1][0] else _el("mrow")
    return el


def from_typst(src: str, display: bool = False) -> ET.Element:
    if len(src) > MAX_LEN:
        return _wrap([_el("mtext", src[:MAX_LEN], **{"class": "raw"})], src, TYPST, display)
    try:
        items = _TypstParser(src, display).body()
    except (_TooComplex, RecursionError):
        items = [_el("mtext", src, **{"class": "raw"})]
    return _wrap(items, src.strip(), TYPST, display)


# Writing uses only names that have kept their meaning across Typst versions (some, like dot.circle,
# were renamed); every other symbol is written as its character, which Typst takes as it is.
_TYPST_STABLE = set("""alpha beta gamma delta epsilon epsilon.alt zeta eta theta theta.alt iota kappa lambda mu nu xi
omicron pi rho sigma tau upsilon phi phi.alt chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
infinity partial nabla ell aleph emptyset sum product integral integral.double integral.triple integral.cont union.big
sect.big arrow.r arrow.l arrow.l.r arrow.r.double arrow.l.double arrow.l.r.double arrow.r.bar arrow.t arrow.b lt.eq
gt.eq eq.not approx equiv in in.not subset subset.eq supset supset.eq union sect times div plus.minus minus.plus
dot.op forall exists not and or dots.h dots.c dots.v dots.down prime""".split())
TYPST_NAME = {v: k for k, v in reversed(list(TYPST_SYM.items())) if k in _TYPST_STABLE}
TYPST_NAME.update({"−": "-", "/": "\\/", "_": "\\_", "^": "\\^", "&": "\\&", "#": "\\#", "$": "\\$",
                   "\\": "backslash", "\"": "\\\"", "′": "prime", "∂": "partial", "∞": "infinity", "∅": "emptyset",
                   "…": "dots.h", "⋯": "dots.c", "ϵ": "epsilon.alt", "ε": "epsilon", "ϕ": "phi.alt", "φ": "phi",
                   "⋅": "dot.op", "×": "times", "→": "arrow.r", "∧": "and", "∨": "or", "¬": "not", "∇": "nabla"})
TYPST_NAME.update({v: k for k, v in TYPST_STRUCK.items() if k in ("RR", "NN", "ZZ", "QQ", "CC")})
TYPST_ACCENT_NAME = {v: k for k, v in reversed(list(TYPST_ACCENTS.items()))}
TYPST_ACCENT_NAME.update({"‾": "macron", chr(0x304): "macron", chr(0x305): "macron", "^": "hat", "~": "tilde",
                          "→": "arrow", "⃗": "arrow", "¨": "dot.double", "˙": "dot"})
VARIANT_TYPST = {"normal": "upright", "italic": "italic", "bold": "bold", "double-struck": "bb", "script": "cal",
                 "fraktur": "frak", "sans-serif": "sans", "monospace": "mono", "bold-italic": "bold(italic",
                 "bold-script": "bold(cal", "bold-fraktur": "bold(frak", "bold-sans-serif": "bold(sans",
                 "sans-serif-italic": "italic(sans", "sans-serif-bold-italic": "bold(italic(sans"}
WIDTH_TYPST = {"0.167em": "thin", "0.222em": "med", "0.278em": "thick", "1em": "quad", "1.0em": "quad", "2em": "wide",
               "2.0em": "wide", "0.333em": "space", "0.5em": "space.en", "-0.167em": "#h(-0.167em)",
               "-0.222em": "#h(-0.222em)", "-0.278em": "#h(-0.278em)"}


class _ToTypst:
    def __init__(self, display: bool):
        self.display = display
        self.depth = 0
        self.in_args = 0
        self.top_table = 0  # id() of a table that is the whole formula: written as lines with & and \\
        self.paired: set[int] = set()  # plain brackets written as is: they pair up in their row
        self.keep: list[ET.Element] = []

    def pair(self, kids: list[ET.Element]) -> None:
        """Typst groups ( ) [ ] { } that match, so only brackets that match in the same row stay bare;
        the others are escaped (a bracket with a script on it, half an interval)."""
        stack: list[ET.Element] = []
        for c in kids:
            if _local(c.tag) != "mo" or c.get("stretchy") == "true":
                continue
            t = c.text or ""
            if t in _TYP_CLOSE:
                stack.append(c)
            elif t in (")", "]", "}") and stack and _TYP_CLOSE[stack[-1].text or ""] == t:
                o = stack.pop()
                self.paired |= {id(o), id(c)}
                self.keep += [o, c]

    def row(self, kids: list[ET.Element]) -> str:
        self.pair(kids)
        out: list[str] = []
        k = 0
        while k < len(kids):
            c = kids[k]
            tag = _local(c.tag)
            if tag == "mi" and len(c.text or "") > 1 and k + 1 < len(kids) and _local(kids[k + 1].tag) == "mo" \
                    and kids[k + 1].text == APPLY:
                out.append(self.function(c.text or ""))
                k += 2
                continue
            if not (tag == "mo" and c.text == APPLY):
                out.append(self.typ(c))
            k += 1
        return self._balance(" ".join(p for p in out if p))

    def _balance(self, s: str) -> str:
        return s

    def function(self, name: str) -> str:
        return name if name in TYPST_FUNCTIONS else 'op("' + name.replace('"', '\\"') + '")'

    def typ(self, el: ET.Element) -> str:
        self.depth += 1
        try:
            return self._typ(el) if self.depth < 200 else ""
        finally:
            self.depth -= 1

    def call(self, name: str, *args: str) -> str:
        return name + "(" + ", ".join(args) + ")"

    def inner(self, el: ET.Element | list[ET.Element] | None) -> str:
        """An argument of a call, where commas need escaping."""
        self.in_args += 1
        try:
            if el is None:
                return ""
            return self.row(el) if isinstance(el, list) else self.typ(el)
        finally:
            self.in_args -= 1

    def script(self, el: ET.Element | None) -> str:
        if el is None:
            return "()"
        s = self.typ(el)
        if re.fullmatch(r"[A-Za-z]|\d+|[A-Za-z]+(?:\.[A-Za-z]+)*|[^\sA-Za-z\d()\\\"]", s) and s not in ("/", "_", "^"):
            return s
        return "(" + s + ")"

    def _typ(self, el: ET.Element) -> str:
        tag = _local(el.tag)
        kids = _kids(el)
        text = el.text or ""
        if tag == "math":
            return self.row(presentation(el))
        if tag == "semantics":
            return self.typ(kids[0]) if kids else ""
        if tag in ("annotation", "annotation-xml", "none", "mprescripts"):
            return ""
        if tag in ("mi", "mn"):
            return self.token(text, el.get("mathvariant", ""), tag == "mi")
        if tag == "mo":
            if text in ("(", ")", "[", "]", "{", "}") and id(el) not in self.paired:
                return "\\" + text
            return self.operator(text)
        if tag in ("mtext", "ms"):
            base, variant = plain(text)
            variant = el.get("mathvariant", "") or variant
            if variant and variant in VARIANT_TYPST and variant != "normal":
                wrap = VARIANT_TYPST[variant]
                return wrap + '("' + base.replace("\\", "\\\\").replace('"', '\\"') + '")' + ")" * wrap.count("(")
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if tag == "mspace":
            w = el.get("width", "")
            return WIDTH_TYPST.get(w, f"#h({w})" if re.fullmatch(r"-?\d*\.?\d+em", w) else "")
        if tag in ("mrow", "mstyle", "mpadded", "merror", "maction"):
            fp = _fence_parts(el)
            if fp is not None:
                return self.fenced(fp)
            name = _func_name(el)
            if name is not None:
                return self.function(name)
            inner = self.row(kids)
            if tag == "mstyle" and el.get("displaystyle") and inner:
                return self.call("display" if el.get("displaystyle") == "true" else "inline", self.inner(kids))
            return inner
        if tag == "mfrac":
            if len(kids) < 2:
                return self.row(kids)
            return self.call("frac", self.inner(kids[0]), self.inner(kids[1]))
        if tag == "msqrt":
            return self.call("sqrt", self.inner(kids))
        if tag == "mroot":
            if len(kids) < 2:
                return self.call("sqrt", self.inner(kids))
            return self.call("root", self.inner(kids[1]), self.inner(kids[0]))
        if tag in ("msub", "msup", "msubsup", "munder", "mover", "munderover"):
            return self.scripts(tag, kids)
        if tag == "mtable":
            return self.matrix(el, None, top=id(el) == self.top_table)
        if tag == "menclose":
            n = el.get("notation", "box")
            if "strike" in n:
                extra = ", cross: #true" if "up" in n and "down" in n else ", inverted: #true" if "down" in n else ""
                return "cancel(" + self.inner(kids) + extra + ")"
            return self.row(kids)  # Typst math has no box around a formula part
        if tag == "mphantom":
            return "hide(" + self.inner(kids) + ")"
        if tag == "mmultiscripts" and kids:
            return self.typ(kids[0])
        return self.row(kids)

    def token(self, text: str, variant: str, ident: bool) -> str:
        base, found = plain(text)
        variant = variant or found
        if ident and len(base) > 1 and base.isascii() and base.isalpha():
            word = 'upright("' + base + '")' if variant in ("", "normal") else None
            if word:
                return word
        if not ident and re.fullmatch(r"\d+(?:\.\d+)?", base) or len(base) == 1 and base.isascii() and base.isalpha():
            out = base
        else:
            out = " ".join(TYPST_NAME.get(ch, ch) for ch in base)
        if variant == "double-struck" and len(base) == 1 and TYPST_NAME.get(_STYLED.get(("double-struck", base), base), "")[:1].isupper():
            return TYPST_NAME[_STYLED.get(("double-struck", base), base)]  # RR, NN, ...
        capital = len(base) == 1 and base in TEX_UPPER_GREEK.values()
        if not variant:
            return "italic(" + out + ")" if ident and capital else out
        if variant == "normal":
            return out if capital or not ident or not base.isalpha() else "upright(" + out + ")"
        if variant == "italic" and not found:
            return out
        wrap = VARIANT_TYPST.get(variant)
        if not wrap:
            return out
        return wrap + "(" + out + ")" * (wrap.count("(") + 1)

    def operator(self, text: str) -> str:
        if text in (",", ";"):
            return "\\" + text if self.in_args else text  # a comma inside a call would split its arguments
        if text in TYPST_NAME:
            return TYPST_NAME[text]
        if len(text) > 1 and text.isalpha():
            return self.function(text)
        return " ".join(TYPST_NAME.get(ch, ch) for ch in text)

    def fenced(self, fp) -> str:
        o, parts, seps, c = fp
        if not seps and len(parts[0]) == 1:
            only = parts[0][0]
            if _local(only.tag) == "mtable":
                return self.matrix(only, (o, c), top=False)
            if _local(only.tag) == "mfrac" and only.get("linethickness") in _ZERO and (o, c) == ("(", ")") \
                    and len(_kids(only)) == 2:
                f = _kids(only)
                return self.call("binom", self.inner(f[0]), self.inner(f[1]))
        body = " mid(" + "".join(self.operator(s) for s in seps[:1]) + ") " if seps else ""
        if (o, c) in (("(", ")"), ("[", "]"), ("{", "}")):
            return o + (body.join(self.row(p) for p in parts) if seps else self.row(parts[0])) + c
        self.in_args += 1
        try:
            inner = body.join(self.row(p) for p in parts) if seps else self.row(parts[0])
        finally:
            self.in_args -= 1
        ends = [("\\" + x if x in "()[]{}" else self.operator(x)) if x else "" for x in (o, c)]
        return "lr(" + " ".join(x for x in (ends[0], inner, ends[1]) if x) + ")"

    def scripts(self, tag: str, kids: list[ET.Element]) -> str:
        if not kids:
            return ""
        if tag in ("mover", "munder") and len(kids) == 2 and _local(kids[1].tag) == "mo":
            ch = kids[1].text or ""
            if tag == "mover" and ch in TYPST_ACCENT_NAME and (kids[1].get("accent") == "true"
                                                                or unicodedata.combining(ch)):
                return self.call(TYPST_ACCENT_NAME[ch], self.inner(kids[0]))
            for name, (mark, where) in TYPST_OVERUNDER.items():
                if mark == ch and where == ("over" if tag == "mover" else "under"):
                    return self.call(name, self.inner(kids[0]))
        if tag in ("mover", "munder") and len(kids) == 2 and _local(kids[0].tag) == tag:
            inner = _kids(kids[0])
            if len(inner) == 2 and _local(inner[1].tag) == "mo":
                for name, (mark, where) in TYPST_OVERUNDER.items():
                    if mark == inner[1].text and mark in BRACES and where == ("over" if tag == "mover" else "under"):
                        return self.call(name, self.inner(inner[0]), self.inner(kids[1]))
        base, sub, sup, under = _scripts_of(tag, kids)
        movable = _local(base.tag) == "mo" and base.text in BIG_CHARS or _func_name(base) is not None
        b = self.typ(base)
        if movable:
            default = (base.text not in INTEGRALS) if _local(base.tag) == "mo" else _func_name(base) in LIMIT_FUNCTIONS
            if under and not (default and self.display):
                b = "limits(" + b + ")"
            elif not under and default and self.display:
                b = "scripts(" + b + ")"
        elif under:
            b = "limits(" + self.inner(base) + ")"
        elif _local(base.tag) == "mrow" and not _kids(base):
            b = '""'
        elif _local(base.tag) in ("mrow", "mstyle", "mpadded") and _fence_parts(base) is None and _func_name(base) is None \
                or _local(base.tag) in ("msub", "msup", "msubsup", "munder", "mover", "munderover", "mtable", "mspace") \
                and not (_local(base.tag) in ("mover", "munder") and b.endswith(")") and not b.startswith("limits")):
            args = [self.inner(base)]
            if sub is not None:
                args.append("b: " + self.inner(sub))
            if sup is not None:
                args.append("t: " + self.inner(sup))
            return "attach(" + ", ".join(args) + ")"
        if not b:
            b = '""'
        out = b
        if sub is not None:
            out += "_" + self.script(sub)
        if sup is not None:
            prime = {"′": "'", "″": "''", "‴": "'''"}.get(sup.text or "") if _local(sup.tag) == "mo" and not under else None
            out += prime if prime else "^" + self.script(sup)
        return out

    def matrix(self, el: ET.Element, fences: tuple[str, str] | None, top: bool) -> str:
        trs = [tr for tr in _kids(el) if _local(tr.tag) in ("mtr", "mlabeledtr")]
        cells = [[_kids(td) for td in _kids(tr)] for tr in trs]
        if fences is None and top:
            return " \\ ".join(" & ".join(self.row(c) for c in r) for r in cells)
        o, c = fences or ("", "")
        if o == "{" and c == "" or o == "" and c == "}":
            rows = ", ".join(" & ".join(self.inner(x) for x in r) for r in cells)
            return "cases(" + ("reverse: #true, " if o == "" else "") + rows + ")"
        delim = {("(", ")"): "", ("[", "]"): 'delim: "[", ', ("{", "}"): 'delim: "{", ', ("∣", "∣"): 'delim: "|", ',
                 ("|", "|"): 'delim: "|", ', ("∥", "∥"): 'delim: "||", ', ("‖", "‖"): 'delim: "||", ',
                 ("", ""): "delim: #none, "}.get((o, c))
        if delim is None:
            return "lr(" + self.operator(o) + " " + self.matrix(el, ("", ""), False) + " " + self.operator(c) + ")"
        if all(len(r) == 1 for r in cells) and delim == "" and len(cells) > 1:
            return "vec(" + ", ".join(self.inner(r[0]) for r in cells) + ")"
        return "mat(" + delim + "; ".join(", ".join(self.inner(x) for x in r) for r in cells) + ")"


def to_typst(math: ET.Element) -> str:
    found = annotation(math, TYPST)
    if found is not None:
        return found.strip()
    conv = _ToTypst(math.get("display") == "block")
    kids = presentation(math) if _local(math.tag) == "math" else [math]
    only = kids[0] if len(kids) == 1 else None
    while only is not None and _local(only.tag) in ("mrow", "mstyle") and len(_kids(only)) == 1:
        only = _kids(only)[0]
    if only is not None and _local(only.tag) == "mtable":
        conv.top_table = id(only)
    return conv.row(kids)
