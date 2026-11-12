"""LaTeX: a reader (TeX tokens, macros, includes, sections, lists, tables, figures, math, theorems,
footnotes, cross-references and citations) and a writer that makes a document pdflatex compiles.

The reader is not TeX: it knows LaTeX's document commands and environments, expands the macros a
document defines (``\\newcommand``, ``\\def``, ...), and keeps the text of anything else. Numbers come
out as LaTeX gives them: sections, figures, tables, equations, theorems and footnotes, so "see
Section 3" and ``\\ref`` agree.
"""

from __future__ import annotations

import itertools
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date
from pathlib import Path

from .. import bib, markup, mathml
from ..bib import ACCENTS, SYMBOLS, accent, ligatures
from ..book import Book, Metadata, is_external, relative, resolve, sniff_image
from ..errors import BookportError
from ..limits import LimitError, safe_path
from ..textwriter import Cell, Table, TextWriter, is_noteref, write_text

MAX_NEST = 60  # arguments inside arguments read one in another; deeper ones are read flat
MAX_EXPANSIONS = 200_000  # macro expansions for one document
MAX_EXPANDED = 4_000_000  # tokens macros may produce for one document
MAX_COLUMNS = 1000  # columns a tabular's spec may ask for
MAX_INCLUDE_DEPTH = 16
MAX_INCLUDES = 1000
MAX_SOURCE = 64 * 1024 * 1024  # characters read for one document, includes and all
MAX_MATH = 100_000  # tokens in one formula

# --- tokens ------------------------------------------------------------------------------------------
# A token is (kind, value): ("cs", name), ("tx", text), ("sp", " "), ("par", None), single characters
# with their own meaning as (char, char) for { } $ & # ^ _ ~, ("verb", code), ("venv", (env, lang, code)),
# ("url", (command, url)), ("param", n) in macro bodies and ("end", object) markers.

_TOK = re.compile(r"\\([A-Za-z@]+)|\\(.)|(%[^\n]*\n?)|([ \t\r\n\f]+)|([^\\{}$&#^_~%\[\] \t\r\n\f]+)|(.)", re.DOTALL)
_AFTER_CS = re.compile(r"[ \t]*(\n[ \t]*)?")
_VERB_ENVS = ("verbatim", "verbatim*", "Verbatim", "Verbatim*", "BVerbatim", "LVerbatim", "lstlisting", "minted",
              "alltt", "comment", "spverbatim", "code", "lstlisting*", "filecontents", "filecontents*", "pyglist",
              "sourcecode", "listing*")
_VERB_BEGIN = re.compile(r"[ \t]*\{(" + "|".join(re.escape(e) for e in _VERB_ENVS) + r")\}")
_URL_CMDS = {"url", "href", "nolinkurl", "path", "burl"}
_INCLUDES = {"input", "include", "subfile", "import", "subimport", "includefrom", "subincludefrom"}

SP = ("sp", " ")
PAR = ("par", None)
OPEN = ("{", "{")
CLOSE = ("}", "}")
LBRACK = ("tx", "[")
RBRACK = ("tx", "]")
SPECIAL = {"{": OPEN, "}": CLOSE, "$": ("$", "$"), "&": ("&", "&"), "#": ("#", "#"), "^": ("^", "^"),
           "_": ("_", "_"), "~": ("~", "~"), "[": LBRACK, "]": RBRACK}


def detok(toks) -> str:
    """Tokens back to LaTeX source."""
    out: list[str] = []
    letter = False
    for k, v in toks:
        if k == "cs":
            out.append("\\" + v)
            letter = v[:1].isalpha() or v[:1] == "@"
            continue
        if k == "tx":
            if letter and (v[:1].isalpha() or v[:1] == "@"):
                out.append(" ")
            out.append(v)
        elif k == "sp":
            out.append(" ")
        elif k == "par":
            out.append("\n\n")
        elif k in "{}$&#^_~":
            out.append(k)
        elif k == "param":
            out.append(f"#{v}")
        elif k == "verb":
            d = next((c for c in "|!+@=/" if c not in v), "|")
            out.append(f"\\verb{d}{v}{d}")
        elif k == "venv":
            out.append(f"\\begin{{{v[0]}}}\n{v[2]}\n\\end{{{v[0]}}}")
        elif k == "url":
            out.append(f"\\{v[0]}{{{v[1]}}}")
        letter = False
    return "".join(out)


class _Stream:
    """Tokens to read, as a stack of lists: macro expansions and arguments go on top."""

    __slots__ = ("stack",)

    def __init__(self):
        self.stack: list[list] = []

    def push(self, toks: list) -> None:
        if toks:
            self.stack.append([toks, 0])

    def next(self):
        st = self.stack
        while st:
            top = st[-1]
            i = top[1]
            if i < len(top[0]):
                top[1] = i + 1
                return top[0][i]
            st.pop()
        return None

    def peek(self):
        st = self.stack
        while st:
            top = st[-1]
            if top[1] < len(top[0]):
                return top[0][top[1]]
            st.pop()
        return None

    def unread(self, t) -> None:
        st = self.stack
        if st and st[-1][1] > 0 and st[-1][0][st[-1][1] - 1] is t:
            st[-1][1] -= 1
        else:
            st.append([[t], 0])


class _Frame:
    """Where content goes: a block frame opens paragraphs as text arrives, an inline one takes it as is."""

    __slots__ = ("block", "el", "kind", "para")

    def __init__(self, el: ET.Element, block: bool, kind: str = ""):
        self.el, self.block, self.para, self.kind = el, block, None, kind


class _Macro:
    __slots__ = ("body", "default", "delims", "nargs")

    def __init__(self, nargs: int, default, body: list, delims=None):
        self.nargs, self.default, self.body, self.delims = nargs, default, body, delims


def _params(body: list) -> list:
    """``#1`` in a macro body as ("param", 1); ``##`` as a plain ``#``."""
    out = []
    i, n = 0, len(body)
    while i < n:
        t = body[i]
        if t[0] == "#" and i + 1 < n:
            nxt = body[i + 1]
            if nxt[0] == "tx" and nxt[1][:1].isdigit():
                out.append(("param", int(nxt[1][0])))
                if len(nxt[1]) > 1:
                    out.append(("tx", nxt[1][1:]))
                i += 2
                continue
            if nxt[0] == "#":
                out.append(SPECIAL["#"])
                i += 2
                continue
        out.append(t)
        i += 1
    return out


def _subst(body: list, args: list[list]) -> list:
    out = []
    for t in body:
        if t[0] == "param":
            if 1 <= t[1] <= len(args):
                out.extend(args[t[1] - 1])
        else:
            out.append(t)
    return out


def _roman(n: int) -> str:
    if n <= 0 or n >= 4000:
        return str(n)
    out = ""
    for v, s in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                 (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out += s
            n -= v
    return out


def _alpha(n: int) -> str:
    if n <= 0:
        return str(n)
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(ord("a") + r) + out
    return out


# --- what the reader knows -------------------------------------------------------------------------

SECTIONS = {"part": -1, "chapter": 0, "section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4,
            "subparagraph": 5, "addchap": 0, "addsec": 1, "addpart": -1}
_SEC_ORDER = ["part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"]
BOOK_CLASSES = {"book", "report", "memoir", "scrbook", "scrreprt", "amsbook", "thesis", "ucthesis", "mitthesis",
                "Thesis", "elegantbook", "tufte-book", "krantz", "svmono"}

INLINE = {"emph": ("em", None), "textit": ("em", None), "textsl": ("em", None), "mathit": ("em", None),
          "textbf": ("strong", None), "mathbf": ("strong", None), "bm": ("strong", None),
          "texttt": ("code", None), "textsc": ("span", "smallcaps"), "underline": ("u", None), "uline": ("u", None),
          "uuline": ("u", None), "uwave": ("u", None), "ul": ("u", None), "sout": ("del", None), "st": ("del", None),
          "xout": ("del", None), "textst": ("del", None), "textsuperscript": ("sup", None),
          "textsubscript": ("sub", None), "textup": ("span", None), "textrm": ("span", None),
          "textsf": ("span", None), "textnormal": ("span", None), "textmd": ("span", None), "mathrm": ("span", None),
          "mathsf": ("span", None), "mathtt": ("code", None), "textbfit": ("strong", None), "hl": ("mark", None),
          "lowercase": ("span", None), "uppercase": ("span", None), "MakeUppercase": ("span", None),
          "MakeLowercase": ("span", None), "MakeTextUppercase": ("span", None), "textnormalfont": ("span", None),
          "keyword": ("strong", None), "code": ("code", None)}
SWITCHES = {"bf": "b", "bfseries": "b", "it": "i", "itshape": "i", "em": "i", "sl": "i", "slshape": "i",
            "tt": "tt", "ttfamily": "tt", "sc": "sc", "scshape": "sc", "rm": "", "rmfamily": "", "normalfont": "",
            "upshape": "-i", "mdseries": "-b", "sf": "", "sffamily": "", "textstyle": None, "boldmath": None}
_STYLE_TAGS = {"b": ("strong", None), "i": ("em", None), "tt": ("code", None), "sc": ("span", "smallcaps")}

# commands with nothing to show: skipped with their arguments (s: a star, o: an optional, m: a mandatory one)
IGNORE = {
    "vspace": "sm", "hspace": "sm", "addvspace": "m", "setlength": "mm", "addtolength": "mm", "settowidth": "mm",
    "settoheight": "mm", "settodepth": "mm", "addtocounter": "mm", "stepcounter": "m", "refstepcounter": "m",
    "newcounter": "mo", "newlength": "m", "setlist": "om", "definecolor": "ommm", "colorlet": "mm", "color": "om",
    "pagecolor": "om", "pagestyle": "m", "thispagestyle": "m", "pagenumbering": "m", "markboth": "mm",
    "markright": "m", "bibliographystyle": "m", "documentclass": "om", "PassOptionsToPackage": "mm",
    "PassOptionsToClass": "mm", "usetikzlibrary": "m", "usepgfplotslibrary": "m", "pgfplotsset": "m",
    "tikzset": "m", "tikzstyle": "m", "hypersetup": "m", "geometry": "m", "lstset": "m", "setminted": "om",
    "captionsetup": "om", "sisetup": "m", "crefname": "mmm", "Crefname": "mmm", "crefformat": "mm",
    "Crefformat": "mm", "creflabelformat": "mm", "floatname": "mm", "floatstyle": "m", "restylefloat": "m",
    "DeclareCaptionFont": "mm", "DeclareCaptionFormat": "mm", "DeclareCaptionLabelFormat": "mm",
    "DeclareCaptionLabelSeparator": "mm", "DeclareCaptionType": "omo", "hyphenation": "m", "index": "m",
    "glossary": "m", "linespread": "m", "fontsize": "mm", "addcontentsline": "mmm", "enlargethispage": "sm",
    "pdfbookmark": "omm", "bookmark": "om", "phantom": "m", "hphantom": "m", "vphantom": "m",
    "includeonly": "m", "excludecomment": "m", "includecomment": "m", "setcopyright": "m", "copyrightyear": "m",
    "acmYear": "m", "acmDOI": "m", "acmISBN": "m", "acmConference": "ommm", "acmBooktitle": "m",
    "acmPrice": "m", "acmSubmissionID": "m", "received": "om", "ccsdesc": "om", "settopmatter": "m",
    "pdfinfo": "m", "pdfoutput": "", "graphicspath": "m", "DeclareGraphicsExtensions": "m",
    "newfloat": "mmmo", "floatplacement": "mm", "allowdisplaybreaks": "o", "numberwithin": "omm",
    "counterwithin": "smm", "counterwithout": "smm", "setcitestyle": "m", "bibpunct": "ommmmmm",
    "renewbibmacro": "smm", "DeclareFieldFormat": "omm", "ExecuteBibliographyOptions": "om", "AtBeginDocument": "m",
    "AtEndDocument": "m", "AtBeginEnvironment": "mm", "AtEndEnvironment": "mm", "pretocmd": "mmmm",
    "apptocmd": "mmmm", "patchcmd": "mmmmm", "newsavebox": "m", "sbox": "mm", "savebox": "moom", "usebox": "m",
    "makeatletter": "", "makeatother": "", "linenumbers": "o", "nolinenumbers": "", "doublespacing": "",
    "singlespacing": "", "onehalfspacing": "", "setstretch": "m", "titleformat": "smommo", "titlespacing": "smmmmo",
    "titlelabel": "m", "fancyhead": "om", "fancyfoot": "om", "fancyhf": "om", "lhead": "om", "chead": "om",
    "rhead": "om", "lfoot": "om", "cfoot": "om", "rfoot": "om", "setsecnumdepth": "m", "settocdepth": "m",
    "newtcolorbox": "ommom", "tcbuselibrary": "m", "tcbset": "m", "newmdenv": "om", "mdfsetup": "m",
    "theoremstyle": "m", "newtheoremstyle": "mmmmmmmmm", "swapnumbers": "", "qedhere": "", "newacronym": "ommm",
    "acrodef": "mom", "DeclareAcronym": "mm", "loadglsentries": "m", "makeglossaries": "", "printglossaries": "",
    "printglossary": "o", "nomenclature": "omm", "printnomenclature": "o", "makenomenclature": "",
    "IEEEoverridecommandlockouts": "", "IEEEpeerreviewmaketitle": "", "IEEEtriggeratref": "m",
    "affiliation": "om", "institute": "m", "email": "m", "keywords": "m", "icmltitlerunning": "m",
    "icmlsetsymbol": "mm", "icmlaffiliation": "mm", "icmlcorrespondingauthor": "mm", "icmlkeywords": "m",
    "printAffiliationsAndNotice": "m", "runningtitle": "m", "runningauthor": "m", "shorttitle": "m",
    "shortauthors": "m", "authorrunning": "m", "titlerunning": "m", "orcid": "m", "orcidID": "m", "corres": "m",
    "subjclass": "om", "dedicatory": "m", "translator": "m", "address": "om", "curraddr": "om", "urladdr": "om",
    "jot": "", "abovedisplayskip": "", "belowdisplayskip": "", "arraystretch": "", "tabcolsep": "",
    "newgeometry": "m", "restoregeometry": "", "setkeys": "mm", "pgfmathsetmacro": "mm", "pgfplotstableread": "mm",
    "newrobustcmd": "smom", "setbeamertemplate": "mom", "usetheme": "om", "usecolortheme": "om",
    "setbeamercolor": "mm", "setbeamerfont": "mm", "frametitle": "m", "framesubtitle": "m", "pause": "",
    "selectlanguage": "m", "foreignlanguage": "", "otherlanguage": "", 
    "DeclareUnicodeCharacter": "mm", "newunicodechar": "mm", "SetUnicodeOption": "m",
    "fontencoding": "m", "fontfamily": "m", "fontseries": "m", "fontshape": "m", "usefont": "mmmm",
    "setmainfont": "om", "setsansfont": "om", "setmonofont": "om", "setmathfont": "om", "newfontfamily": "mom",
    "babelfont": "omom", "FloatBarrier": "", "balance": "", "onecolumn": "", "twocolumn": "o",
    "clearpage": "", "newpage": "", "cleardoublepage": "", "pagebreak": "o", "nopagebreak": "o",
    "nolinebreak": "o", "smallskip": "", "medskip": "", "bigskip": "", "vfill": "", "hfill": "", "hfil": "",
    "vfil": "", "null": "", "relax": "", "protect": "", "leavevmode": "", "strut": "", "normalsize": "",
    "small": "", "footnotesize": "", "scriptsize": "", "tiny": "", "large": "", "Large": "", "LARGE": "",
    "huge": "", "Huge": "", "sloppy": "", "fussy": "", "frenchspacing": "", "nonfrenchspacing": "",
    "tableofcontents": "", "listoffigures": "", "listoftables": "", "printindex": "", "makeindex": "",
    "xspace": "", "ignorespaces": "", "unskip": "", "allowbreak": "", "break": "", "nobreak": "", "noindent": "",
    "indent": "", "centering": "", "raggedright": "", "raggedleft": "", "RaggedRight": "", "frontmatter": "",
    "mainmatter": "", "backmatter": "", "iclrfinalcopy": "", "bigbreak": "", "filbreak": "", "goodbreak": "",
    "nointerlineskip": "", "offinterlineskip": "", "vss": "", "hss": "", "arraybackslash": "", "normalcolor": "",
    "unboldmath": "", "displaystyle": "", "scriptstyle": "", "phantomsection": "", "fi": "", "or": "",
    "endinput": "", "listfiles": "", "nonstopmode": "", "batchmode": "", "scrollmode": "", "errorstopmode": "",
    "hbadness": "", "vbadness": "", "tolerance": "", "emergencystretch": "", "widowpenalty": "",
    "clubpenalty": "", "raggedbottom": "", "flushbottom": "", "hrulefill": "", "dotfill": "",
    "ttlinesep": "", "selectfont": "", "aftergroup": "", "global": "", "long": "", "outer": "", "expandafter": "",
    "noexpand": "", "csname": "", "endcsname": "", "string": "", "the": "", "number": "", "value": "m",
    "theequation": "", "thefigure": "", "thetable": "", "thesection": "", "thepage": "", "footnotesep": "",
    "mathversion": "m", "abovecaptionskip": "", "belowcaptionskip": "", "textfloatsep": "", "floatsep": "",
    "intextsep": "", "dbltextfloatsep": "", "parindent": "", "parskip": "", "baselineskip": "", "textwidth": "",
    "linewidth": "", "columnwidth": "", "textheight": "", "hsize": "", "vsize": "", "topmargin": "",
    "oddsidemargin": "", "evensidemargin": "", "headheight": "", "headsep": "", "footskip": "", "voffset": "",
    "hoffset": "", "marginparwidth": "", "marginparsep": "", "paperwidth": "", "paperheight": "",
    "pdfpagewidth": "", "pdfpageheight": "", "itemsep": "", "topsep": "", "parsep": "", "partopsep": "",
    "labelsep": "", "leftmargin": "", "rightmargin": "", "listparindent": "", "itemindent": "",
    "labelwidth": "", "fboxsep": "", "fboxrule": "", "arrayrulewidth": "", "doublerulesep": "",
    "extrarowheight": "", "heavyrulewidth": "", "lightrulewidth": "", "cmidrulewidth": "", "belowrulesep": "",
    "aboverulesep": "", "bibsep": "", "bibhang": "", "bibfont": "", "citestyle": "m", 
    "hyphenpenalty": "", "exhyphenpenalty": "", "looseness": "", "pretolerance": "",
    "sectionfont": "m", "subsectionfont": "m", "maxdeadcycles": "", "IEEEauthorrefmark": "m",
    "IEEEpubid": "m", "IEEEpubidadjcol": "", "IEEEspecialpapernotice": "m", "IEEEaftertitletext": "m",
    "sethlcolor": "m", "soulregister": "mm", "mathtoolsset": "m",
    "newsiamremark": "mm", "newsiamthm": "mm", "headers": "mm", "ead": "om", "cortext": "om", "fntext": "om",
    "tnotetext": "om", "journal": "m", "volume": "m", "pubyear": "m", "firstpage": "m",
    "lastpage": "m", "articlenumber": "m", "doi": "m", "issuenumber": "m", "useunder": "mmm",
}

# boxes and colours: their arguments skipped, their content kept where it is
TRANSPARENT = {"textcolor": "om", "colorbox": "om", "fcolorbox": "omm", "fbox": "", "framebox": "oo",
               "makebox": "oo", "mbox": "", "parbox": "ooom", "raisebox": "moo", "scalebox": "mo", "resizebox": "smm",
               "rotatebox": "om", "adjustbox": "m", "hbox": "", "vbox": "", "vtop": "", "centerline": "",
               "boxed": "", "shortstack": "o", "makecell": "o", "thead": "o", "Centerstack": "o", "stackanchor": "o",
               "textls": "o", "foreignlanguage": "om", "tcbox": "o",
               "smash": "o", "IEEEauthorblockA": "", "IEEEauthorblockN": "", "titlebox": "",
               "acrshort": "", "acrlong": "", "acrfull": "", "gls": "", "Gls": "", "glspl": "",
               "acs": "", "acl": "", "acf": "", "ac": "", "acp": "", "Ac": "", "affil": "o", "inst": "",
               "thanksref": "", "tnoteref": "", "fnref": "", "cormark": "o", "fnmark": "o", "textlatin": "",
               "textenglish": "", "textgreek": "", "textrussian": "", "foreignquote": "m", "hyphenquote": "m",
               "rlap": "", "llap": "", "clap": "", "mathrlap": "", "mathllap": "",
               "unexpanded": "", "detokenize": "", 
               "sidenote": "", "tablefootnote": "", "titlenote": "", "subtitle": "", "IEEEPARstart": "m"}

LISTS = {"itemize": "ul", "enumerate": "ol", "description": "dl", "compactitem": "ul", "compactenum": "ol",
         "compactdesc": "dl", "inparaenum": "ol", "inparaitem": "ul", "asparaenum": "ol", "asparaitem": "ul",
         "asparadesc": "dl", "itemize*": "ul", "enumerate*": "ol", "description*": "dl", "list": "ul",
         "tablenotes": "ul", "outline": "ul", "trivlist": "ul", "itemizeb": "ul", "dinglist": "ul",
         "checklist": "ul", "todolist": "ul", "steps": "ol", "questions": "ol", "parts": "ol", "choices": "ol",
         "algorithmic@": "ol"}
QUOTES = {"quote", "quotation", "verse", "displayquote", "quoting", "fquote", "shadequote"}
TRANSPARENT_ENVS = {
    "center": "", "flushleft": "", "flushright": "", "minipage": "oom", "raggedright": "", "RaggedRight": "",
    "singlespace": "", "singlespacing": "", "doublespace": "", "onehalfspace": "", "spacing": "m", "small": "",
    "footnotesize": "", "scriptsize": "", "tiny": "", "large": "", "Large": "", "LARGE": "", "huge": "",
    "Huge": "", "normalsize": "", "sloppypar": "", "multicols": "mo", "multicols*": "mo", "adjustbox": "m",
    "landscape": "", "samepage": "", "titlepage": "", "threeparttable": "o", "subequations": "",
    "appendices": "", "frame": "oom", "block": "m", "alertblock": "m", "exampleblock": "m", "columns": "o",
    "column": "om", "algorithm": "o", "algorithm*": "o", "algorithmic": "o", "algorithm2e": "o",
    "keywords": "", "IEEEkeywords": "", "acks": "", "ack": "", "acknowledgments": "", "acknowledgements": "",
    "tcolorbox": "o", "mdframed": "o", "framed": "", "shaded": "", "leftbar": "", "boxedminipage": "om",
    "otherlanguage": "m", "otherlanguage*": "m", "english": "", "CJK": "omm", "CJK*": "omm", "savenotes": "",
    "fullwidth": "", "widepage": "", "addmargin": "om", "scope": "o", 
    "tabbing": "", "bfseries": "", "itshape": "", "ttfamily": "", "linenomath": "",
    "linenomath*": "", "nolinenumbers": "", "multienum": "", "wide": "", "fullpage": "",
    "frontmatter": "", "abstract*": "", "IEEEbiography": "om", "IEEEbiographynophoto": "m", "biography": "om",
    "highlights": "", "graphicalabstract": "", "credits": "", "boxtext": "", "note": "o",
    "appendix": "", "lrbox": "m", "textblock": "mo",
    "textblock*": "mo", "overpic": "om", "notes": "", 
}
DRAWINGS = {"tikzpicture", "pgfpicture", "picture", "axis", "semilogyaxis", "semilogxaxis", "loglogaxis",
            "groupplot", "forest", "circuitikz", "pspicture", "tikzcd", "xy", "dot2tex", "asy", "asydef",
            "pgfonlayer", "sequencediagram", "umlseqdiag", "gnuplot", "mpost", "feynman", "fmffile",
            "polaraxis", "ternaryaxis", "tikzfigure"}
FIGURES = {"figure", "figure*", "wrapfigure", "wrapfigure*", "SCfigure", "SCfigure*", "sidewaysfigure",
           "subfigure", "subfigure*", "floatingfigure", "marginfigure", "listing"}
TABLE_FLOATS = {"table", "table*", "sidewaystable", "sidewaystable*", "wraptable", "wraptable*", "SCtable",
                "subtable", "subtable*", "margintable", "floatingtable"}
TABULARS = {"tabular", "tabular*", "tabularx", "tabulary", "longtable", "longtable*", "xltabular", "supertabular",
            "supertabular*", "xtabular", "xtabular*", "mpxtabular", "tabu", "longtabu", "NiceTabular", "NiceTabular*",
            "tblr", "longtblr", "talltblr", "TAB", "ctabular"}
MATH_ENVS = {"equation": 1, "equation*": 0, "align": 2, "align*": 0, "gather": 2, "gather*": 0, "multline": 1,
             "multline*": 0, "flalign": 2, "flalign*": 0, "alignat": 2, "alignat*": 0, "eqnarray": 2,
             "eqnarray*": 0, "displaymath": 0, "dmath": 1, "dmath*": 0, "math": -1, "IEEEeqnarray": 2,
             "IEEEeqnarray*": 0, "xalignat": 2, "xalignat*": 0, "xxalignat": 0, "darray": 0, "dgroup": 0,
             "dgroup*": 0, "empheq": 1, "eqn": 1, "mathpar": 0}
_MATH_DROP = {"small", "footnotesize", "scriptsize", "tiny", "normalsize", "large", "Large", "LARGE", "huge", "Huge",
              "normalfont", "selectfont", "nolinebreak", "allowbreak", "relax", "protect", "xspace"}
_ROW_ENVS = {"aligned", "alignedat", "gathered", "split", "cases", "cases*", "dcases", "rcases", "array", "matrix",
             "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix", "subarray", 
             "multlined", "pmatrix*", "bmatrix*", "matrix*", "numcases", "subnumcases", "tabular", "aligned*",
             "lgathered", "rgathered", "drcases", "psmallmatrix", "bsmallmatrix", "vsmallmatrix", "CD"}
_TEX_CONDITIONALS = {"if", "ifx", "ifnum", "ifdim", "ifodd", "ifvmode", "ifhmode", "ifmmode", "ifinner", "ifvoid",
                     "ifhbox", "ifvbox", "ifeof", "iftrue", "iffalse", "ifcase", "ifcat", "ifdefined", "ifcsname",
                     "iffontchar", "ifincsname", "ifpdf", "ifxetex", "ifluatex", "ifpdftex", "ifCLASSOPTIONcompsoc",
                     "ifCLASSOPTIONonecolumn", "ifCLASSINFOpdf", "ifarxiv", "ifdraft", "ifpreprint", "iffinal",
                     "ifcomments", "ifanonymous", "ifsubmission", "ifshowcomments"}
# names a document may not redefine: bookport reads them as structure
PROTECTED = set(SECTIONS) | {"item", "caption", "label", "ref", "eqref", "cite", "citep", "citet", "footnote",
                             "includegraphics", "begin", "end", "maketitle", "input", "include", "bibliography",
                             "bibitem", "url", "href", "emph", "textbf", "textit", "texttt", "par", "\\", "appendix",
                             "newcommand", "renewcommand", "def", "let", "title", "author", "date", "thanks",
                             "printbibliography", "multicolumn", "multirow", "hline", "toprule", "midrule",
                             "bottomrule", "tableofcontents", "footnotemark", "footnotetext", "autoref", "cref",
                             "Cref", "pageref", "newline", "verb", "frac", "sqrt", "left", "right"}
PROTECTED_ENVS = {"document", "abstract", "itemize", "enumerate", "description", "figure", "figure*", "table",
                  "table*", "tabular", "longtable", "equation", "equation*", "align", "align*", "thebibliography",
                  "verbatim", "quote", "quotation", "center", "proof", "minipage", "gather", "multline", "eqnarray",
                  "array", "cases", "matrix", "pmatrix", "bmatrix", "split", "aligned", "subfigure", "subtable"}
CITES = {"cite": "normal", "citep": "normal", "parencite": "normal", "autocite": "normal", "Cite": "normal",
         "Citep": "normal", "Parencite": "normal", "Autocite": "normal", "supercite": "normal", "footcite": "normal",
         "fullcite": "normal", "smartcite": "normal", "Smartcite": "normal", "cites": "normal", "parencites": "normal",
         "citet": "textual", "Citet": "textual", "textcite": "textual", "Textcite": "textual", "citeauthor": "author",
         "Citeauthor": "author", "citeyear": "bare-year", "citeyearpar": "year", "citealt": "bare",
         "Citealt": "bare", "citealp": "bare", "Citealp": "bare", "citenum": "bare", "nocite": "nocite",
         "citetitle": "author", "citeurl": "bare", "footfullcite": "normal", "citepalias": "normal",
         "citetalias": "textual", "textcites": "textual", "citeA": "textual", "citeNP": "bare", "citeN": "textual",
         "shortcite": "normal", "citeasnoun": "textual", "newcite": "textual", "citeposs": "textual", "possessivecite": "textual", "citeads": "normal"}
REFS = {"ref": "ref", "eqref": "eqref", "autoref": "autoref", "Autoref": "autoref", "cref": "cref", "Cref": "Cref",
        "cpageref": "ref", "Cpageref": "ref", "pageref": "ref", "vref": "ref", "Vref": "ref", "nameref": "nameref",
        "Nameref": "nameref", "labelcref": "ref", "subref": "ref", "fullref": "autoref", "crefrange": "cref",
        "Crefrange": "Cref", "vpageref": "ref", "thmref": "autoref", "prettyref": "autoref", "zcref": "cref",
        "zref": "ref", "ref*": "ref"}
_REF_NAMES = {"part": ("Part", "part"), "chapter": ("Chapter", "chapter"), "section": ("Section", "section"),
              "subsection": ("Section", "section"), "subsubsection": ("Section", "section"),
              "paragraph": ("Paragraph", "paragraph"), "subparagraph": ("Paragraph", "paragraph"),
              "appendix": ("Appendix", "appendix"), "figure": ("Figure", "fig."), "table": ("Table", "table"),
              "equation": ("Equation", "eq."), "item": ("Item", "item"), "footnote": ("Footnote", "footnote"),
              "subfigure": ("Figure", "fig."), "subtable": ("Table", "table"), "listing": ("Listing", "listing"), "algorithm": ("Algorithm", "alg.")}
LANGS = {"english": "en", "american": "en-US", "british": "en-GB", "UKenglish": "en-GB", "USenglish": "en-US",
         "german": "de", "ngerman": "de", "austrian": "de-AT", "french": "fr", "francais": "fr", "spanish": "es",
         "italian": "it", "portuguese": "pt", "brazil": "pt-BR", "brazilian": "pt-BR", "russian": "ru", "dutch": "nl",
         "polish": "pl", "czech": "cs", "slovak": "sk", "swedish": "sv", "danish": "da", "norsk": "nb",
         "norwegian": "nb", "nynorsk": "nn", "finnish": "fi", "greek": "el", "turkish": "tr", "hungarian": "hu",
         "romanian": "ro", "croatian": "hr", "serbian": "sr", "ukrainian": "uk", "catalan": "ca", "hebrew": "he",
         "arabic": "ar", "japanese": "ja", "chinese": "zh", "korean": "ko", "estonian": "et", "latvian": "lv",
         "lithuanian": "lt", "slovene": "sl", "icelandic": "is", "irish": "ga", "welsh": "cy", "basque": "eu",
         "galician": "gl", "vietnamese": "vi", "indonesian": "id", "bahasa": "id", "latin": "la"}
NUMERIC_BST = {"plain", "unsrt", "abbrv", "ieeetr", "ieeetran", "alpha", "siam", "acm", "amsplain", "amsalpha",
               "unsrtnat", "splncs", "splncs03", "splncs04", "ieee", "ieee_fullname", "naturemag", "nature",
               "apsrev", "apsrev4-1", "apsrev4-2", "jhep", "utphys", "elsarticle-num", "vancouver", "abbrvurl",
               "plainurl", "unsrturl", "ACM-Reference-Format-num", "spmpsci", "spphys", "aip", "aipnum4-1",
               "sty/splncs04", "h-physrev", "JHEP", "ws-ijmpa", "revtex", "phjcp", "pnas2011", "science"}


class _Table:
    def __init__(self, depth: int, aligns: list[str], name: str):
        self.depth = depth
        self.aligns = aligns
        self.name = name
        self.rows: list[list[ET.Element]] = []
        self.row: list[ET.Element] = []
        self.cell: ET.Element | None = None
        self.frame: _Frame | None = None
        self.head: list[list[ET.Element]] | None = None
        self.foot: list[list[ET.Element]] | None = None
        self.rule_after: set[int] = set()  # rows followed by \hline/\midrule
        self.caption: ET.Element | None = None
        self.label: tuple | None = None


class _List:
    def __init__(self, el: ET.Element, kind: str, level: int, frame: _Frame, enum_level: int):
        self.el, self.kind, self.level, self.frame, self.count = el, kind, level, frame, 0
        self.enum_level = enum_level
        self.item_frame: _Frame | None = None
        self.type = ""


class _Float:
    def __init__(self, el: ET.Element, kind: str, frame: _Frame):
        self.el, self.kind, self.frame = el, kind, frame
        self.number = ""
        self.sub = 0  # subfigures so far
        self.captioned = False
        self.pending: list[str] = []  # labels of subfigures, numbered once this float's caption is known


# --- the reader --------------------------------------------------------------------------------------

class _Reader:
    def __init__(self, book: Book, base_dir: Path | None, top: Path | None, main: Path | None):
        self.book = book
        self.base_dir, self.top, self.main = base_dir, top, main
        self.macros: dict[str, _Macro] = {}
        self.envdefs: dict[str, tuple[int, list | None, list, list]] = {}
        self.aliases: dict[str, str] = {}
        self.theorems: dict[str, dict] = {}
        self.theorem_style = "plain"
        self.newifs: dict[str, bool] = {}
        self.stream = _Stream()
        self.expansions = 0
        self.expanded = 0
        self.expansion_capped = False
        self.include_stack: list[Path] = []
        self.includes = 0
        self.source_chars = 0
        self.unknown_cmds: dict[str, int] = {}
        self.unknown_envs: dict[str, int] = {}
        self.lost: list[str] = []
        self.root, self.body = markup.new_document("")
        self.sink = ET.Element("div")
        self.preamble = True
        self.frames: list[_Frame] = [_Frame(self.sink, True, "sink")]
        self.style: tuple = ()
        self.code = 0
        self.wrap_of: dict[int, tuple[tuple, ET.Element]] = {}
        self.depth = 0
        self.env_stack: list[str] = []
        self.lists: list[_List] = []
        self.tables: list[_Table] = []
        self.floats: list[_Float] = []
        self.counters: dict[str, int] = defaultdict(int)
        self.docclass = "article"
        self.chapters = False
        self.secnumdepth: int | None = None
        self.appendix = False
        self.labels: dict[str, tuple[str, str, str, ET.Element]] = {}
        self.refs: list[tuple[ET.Element, list[str], str]] = []
        self.label_target: tuple[ET.Element, str, str] | None = None
        self.headings: list[tuple[ET.Element, int]] = []
        self.notes: dict[str, ET.Element] = {}
        self.note_seq = 0
        self.marks: list[str] = []
        self.title: list | None = None
        self.authors: list | None = None
        self.date: list | None = None
        self.abstract_tokens: list | None = None
        self.title_done = False
        self.bib_files: list[Path] = []
        self.bib_names: list[str] = []
        self.ready: dict[str, bib.Entry] = {}
        self.bib_style = ""
        self.natbib: str | None = None  # natbib's options when loaded
        self.biblatex: str | None = None
        self.bibitems = 0
        self.graphics_paths = [""]
        self.pictures_missing: list[str] = []
        self.pictures_vector: list[str] = []
        self.pictures_refused: list[str] = []
        self.drawings = 0
        self.ids: set[str] = set()
        self.id_next: dict[str, int] = {}
        self.lang = ""
        self.keywords: list[str] = []
        self.column_types: dict[str, str] = {}
        self.cell_breaks = 0
        self.theorem_names: dict[str, str] = {}
        self.bib_placed = False
        self.bib_div: ET.Element | None = None
        self.bib_item: _Frame | None = None
        self.bst = ""
        self.natbib_opts = ""
        self.sty_done: set[str] = set()
        self.delimited: dict[str, tuple[list, list]] = {}
        self.handlers = self._handlers()

    # --- tokenizing and including ------------------------------------------------------------------
    def tokenize(self, text: str, where: Path | None = None, depth: int = 0) -> list:
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.source_chars += len(text)
        out: list = []
        append = out.append
        line_start = True
        pos, n = 0, len(text)
        match = _TOK.match
        while pos < n:
            m = match(text, pos)
            pos = m.end()
            g = m.lastindex
            if g == 5:
                append(("tx", m.group(5)))
                line_start = False
            elif g == 4:
                nl = m.group(4).count("\n")
                if (nl >= 1 and line_start) or nl >= 2:
                    append(PAR)
                elif not line_start:
                    append(SP)
                line_start = False
            elif g == 1:
                name = m.group(1)
                if name in ("verb", "Verb", "lstinline", "mintinline", "spverb"):
                    pos = self._inline_verb(text, pos, name, out)
                    line_start = False
                    continue
                if name == "begin":
                    vm = _VERB_BEGIN.match(text, pos)
                    if vm:
                        pos = self._verb_env(text, vm, out)
                        line_start = False
                        continue
                if name in _URL_CMDS:
                    p = pos
                    while p < n and text[p] in " \t\n":
                        p += 1
                    if p < n and text[p] == "{":
                        end = _brace_end(text, p)
                        append(("url", (name, text[p + 1:end])))
                        pos = end + 1
                        line_start = False
                        continue
                if name in _INCLUDES:
                    got = self._include(text, pos, name, where, depth)
                    if got is not None:
                        pos, toks = got
                        out.extend(toks)
                        line_start = False
                        continue
                append(("cs", name))
                sm = _AFTER_CS.match(text, pos)
                pos = sm.end()
                line_start = sm.group(1) is not None
            elif g == 3:
                line_start = m.group(3).endswith("\n")
            elif g == 2:
                c = m.group(2)
                append(("cs", " " if c in "\n\r\t" else c))
                line_start = False
            else:
                c = m.group(6)
                t = SPECIAL.get(c)
                if t is not None:
                    append(t)
                line_start = False
        return out

    def _inline_verb(self, text: str, pos: int, name: str, out: list) -> int:
        n = len(text)
        if text.startswith("*", pos):
            pos += 1
        if name in ("lstinline", "mintinline"):
            if name == "mintinline" and pos < n and text[pos] == "{":  # \mintinline{lang}{code} or |code|
                pos = _brace_end(text, pos) + 1
            if pos < n and text[pos] == "[":
                close = text.find("]", pos)
                pos = close + 1 if close > 0 else pos
            if pos < n and text[pos] == "{":
                end = _brace_end(text, pos)
                out.append(("verb", text[pos + 1:end]))
                return end + 1
        if pos >= n:
            return pos
        d = text[pos]
        end = text.find(d, pos + 1)
        nl = text.find("\n", pos + 1)
        if d.isalpha() or d.isspace() or end < 0 or (0 <= nl < end):
            out.append(("cs", name))
            return pos
        out.append(("verb", text[pos + 1:end]))
        return end + 1

    def _verb_env(self, text: str, vm: re.Match, out: list) -> int:
        name = vm.group(1)
        pos = vm.end()
        lang = ""
        if name == "minted":
            # spaces once only before the options (two \s* side by side split long runs every way)
            om = re.compile(r"\s*(?:\[[^\]\n]*\]\s*)?\{([^}\n]*)\}").match(text, pos)
            if om:
                lang, pos = om.group(1), om.end()
        elif text.startswith("[", pos):
            close = text.find("]", pos)
            nl = text.find("\n", pos)
            if close > 0 and (nl < 0 or close < nl or text.count("[", pos, close) == 1):
                opts = text[pos + 1:close]
                lm = re.search(r"language\s*=\s*\{?([\w+#-]+)", opts)
                lang = lm.group(1) if lm else ""
                pos = close + 1
        end_tag = f"\\end{{{name}}}"
        end = text.find(end_tag, pos)
        body = text[pos:end if end >= 0 else len(text)]
        nl = body.find("\n")
        if nl >= 0 and not body[:nl].strip():
            body = body[nl + 1:]
        body = body.removesuffix("\n")
        if name != "comment":
            out.append(("venv", (name, lang.lower(), body)))
        return end + len(end_tag) if end >= 0 else len(text)

    def _include(self, text: str, pos: int, name: str, where: Path | None, depth: int):
        n = len(text)
        p = pos
        while p < n and text[p] in " \t":
            p += 1
        folder = ""
        if name in ("import", "subimport", "includefrom", "subincludefrom"):
            if p >= n or text[p] != "{":
                return None
            end = _brace_end(text, p)
            folder = text[p + 1:end].strip()
            p = end + 1
            while p < n and text[p] in " \t\n":
                p += 1
        if p < n and text[p] == "{":
            end = _brace_end(text, p)
            fname = text[p + 1:end].strip()
            p = end + 1
        elif name == "input":
            m = re.compile(r"[^\s{}%\\]+").match(text, p)
            if not m:
                return None
            fname, p = m.group(0), m.end()
        else:
            return None
        if not fname or "#" in fname or "\\" in fname:
            return None
        if name in ("subimport", "subincludefrom") and where is not None and self.base_dir is not None:
            try:
                rel = where.parent.relative_to(self.base_dir.resolve()).as_posix()
            except ValueError:
                rel = ""
            folder = (rel + "/" + folder) if rel and rel != "." else folder
        target = (folder.rstrip("/") + "/" + fname) if folder else fname
        if name in ("input",) and target.endswith((".sty", ".cls")):
            return p, []
        toks = self.include_tokens(target, depth, body_only=name == "subfile")
        return p, toks

    def include_tokens(self, target: str, depth: int, body_only: bool = False, quiet: bool = False) -> list:
        if self.base_dir is None:
            return []
        shown = target.replace("\\", "/")
        if depth >= MAX_INCLUDE_DEPTH or self.includes >= MAX_INCLUDES:
            self.lost.append(f"Included files nest too deeply; this one was left out: {shown}")
            return []
        cands = [target] if target.lower().endswith((".tex", ".ltx", ".latex", ".bbl", ".sty")) else [target + ".tex", target]
        refused = True
        for c in cands:
            p = safe_path(self.base_dir, c, self.top)
            if p is None:
                continue
            refused = False
            if not p.is_file():
                continue
            if p in self.include_stack:
                self.lost.append(f"An include loop was stopped: {shown} includes itself")
                return []
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if self.source_chars + len(data) > MAX_SOURCE:
                self.lost.append(f"The document is too long with its includes; this one was left out: {shown}")
                return []
            self.includes += 1
            text = markup.decode(data).lstrip("\ufeff")
            if body_only or "\\documentclass" in text[:4000]:
                b = text.find("\\begin{document}")
                if b >= 0:
                    e = text.find("\\end{document}", b)
                    text = text[b + len("\\begin{document}"):e if e >= 0 else len(text)]
            self.include_stack.append(p)
            try:
                return self.tokenize(text, p, depth + 1)
            finally:
                self.include_stack.pop()
        if quiet:
            return []
        if refused:
            self.lost.append(f"An include outside the document's folder was left out: {shown}")
        else:
            self.lost.append(f"An included file was not found: {shown}")
        return []

    # --- reading tokens -------------------------------------------------------------------------------
    def next(self):
        """The next token, user macros expanded."""
        while True:
            t = self.stream.next()
            if t is None or t[0] != "cs":
                return t
            m = self.macros.get(t[1])
            if m is None or self.expansion_capped:
                if self.delimited and t[1] in self.delimited and not self.expansion_capped:
                    self.expand_delimited(t[1])
                    continue
                return t
            self.expand(m)

    def _expansion_stop(self) -> None:
        if not self.expansion_capped:
            self.lost.append("Macros expanded too many times (a macro that calls itself?); the rest were left as they are")
        self.expansion_capped = True

    def expand(self, m: _Macro) -> None:
        self.expansions += 1
        if self.expansions > MAX_EXPANSIONS or self.expanded > MAX_EXPANDED:
            self._expansion_stop()
            return
        args: list[list] = []
        if m.nargs:
            if m.default is not None:
                opt = self.read_optional()
                args.append(opt if opt is not None else m.default)
            while len(args) < m.nargs:
                args.append(self.read_arg())
            body = _subst(m.body, args)
        else:
            body = m.body
        self.expanded += len(body)
        self.stream.push(body)

    def skip_spaces(self) -> None:
        st = self.stream
        while True:
            t = st.peek()
            if t is None or t[0] != "sp":
                return
            st.next()

    def read_arg(self) -> list:
        """One macro argument, unexpanded: a {group}'s tokens or a single token."""
        st = self.stream
        while True:
            t = st.next()
            if t is None:
                return []
            if t[0] not in ("sp", "par"):
                break
        k = t[0]
        if k == "{":
            return self._until_close()
        if k == "end":
            st.unread(t)
            return []
        if k == "tx" and len(t[1]) > 1:
            st.push([("tx", t[1][1:])])
            return [("tx", t[1][0])]
        if k == "}":
            st.unread(t)
            return []
        return [t]

    def _until_close(self) -> list:
        st = self.stream
        out = []
        depth = 0
        while True:
            t = st.next()
            if t is None:
                return out
            k = t[0]
            if k == "{":
                depth += 1
            elif k == "}":
                if depth == 0:
                    return out
                depth -= 1
            elif k == "end":
                st.unread(t)
                return out
            out.append(t)

    def read_optional(self, skip_space: bool = True) -> list | None:
        st = self.stream
        skipped = False
        if skip_space:
            while True:
                t = st.peek()
                if t is None or t[0] != "sp":
                    break
                st.next()
                skipped = True
        t = st.peek()
        if t is not LBRACK and t != LBRACK:
            if skipped:
                st.push([SP])
            return None
        st.next()
        out = []
        depth = 0
        count = 0
        while True:
            t = st.next()
            if t is None:
                return out
            k = t[0]
            if k == "{":
                depth += 1
            elif k == "}":
                depth -= 1
                if depth < 0:
                    st.unread(t)
                    return out
            elif k == "end":
                st.unread(t)
                return out
            elif depth == 0 and k == "tx" and t[1] == "]":
                return out
            count += 1
            if count > 100_000:
                return out
            out.append(t)

    def star(self) -> bool:
        t = self.stream.peek()
        if t is not None and t[0] == "tx" and t[1].startswith("*"):
            self.stream.next()
            if len(t[1]) > 1:
                self.stream.push([("tx", t[1][1:])])
            return True
        return False

    def skip_args(self, spec: str) -> None:
        for c in spec:
            if c == "s":
                self.star()
            elif c == "o":
                self.read_optional()
            elif c == "m":
                self.read_arg()

    def arg_text(self, toks: list | None = None) -> str:
        """An argument as plain source text (labels, file names, keys)."""
        return detok(self.read_arg() if toks is None else toks).strip()

    def plain(self, toks: list) -> str:
        """An argument's text as it reads: rendered into a scratch element."""
        el = ET.Element("span")
        self.into(el, toks, block=False)
        return _visible(el)

    def read_env_name(self) -> str:
        return detok(self.read_arg()).strip()

    # --- output --------------------------------------------------------------------------------------
    def push_frame(self, el: ET.Element, block: bool, kind: str = "") -> _Frame:
        f = _Frame(el, block, kind)
        self.frames.append(f)
        return f

    def pop_frame(self, f: _Frame) -> None:
        if f not in self.frames:
            return
        while self.frames:
            top = self.frames.pop()
            if top.block:
                self.close_para(top)
            if top is f:
                return

    def close_para(self, f: _Frame) -> None:
        p = f.para
        if p is None:
            return
        f.para = None
        if len(p) == 0 and not (p.text or "").strip() and len(f.el) and f.el[-1] is p:
            del f.el[-1]

    def inline_parent(self) -> ET.Element:
        f = self.frames[-1]
        if f.kind == "list":
            self.item(implicit=True)
            f = self.frames[-1]
        if f.block:
            if f.para is None:
                f.para = ET.SubElement(f.el, "p")
            parent = f.para
        else:
            parent = f.el
        if self.style:
            parent = self._styled(parent)
        return parent

    def _styled(self, parent: ET.Element) -> ET.Element:
        style = self.style
        if len(parent):
            last = parent[-1]
            w = self.wrap_of.get(id(last))
            if w is not None and w[0] == style and not last.tail:
                return w[1]
        outer = inner = None
        for s in style:
            tag, cls = _STYLE_TAGS[s]
            el = ET.SubElement(inner if inner is not None else parent, tag, {"class": cls} if cls else {})
            if outer is None:
                outer = el
            inner = el
        self.wrap_of[id(outer)] = (style, inner)
        return inner

    def text(self, s: str, lig: bool = True) -> None:
        if not s:
            return
        if not self.code and lig:
            s = ligatures(s)
        f = self.frames[-1]
        if (f.block and f.para is None or f.kind == "list") and not s.strip():
            return
        markup.append_text(self.inline_parent(), s)

    def add_inline(self, el: ET.Element) -> ET.Element:
        self.inline_parent().append(el)
        return el

    def add_block(self, tag: str, attrs: dict | None = None) -> ET.Element:
        f = self.frames[-1]
        if f.kind == "list":
            self.item(implicit=True)
            f = self.frames[-1]
        if f.block:
            self.close_para(f)
            return ET.SubElement(f.el, tag, attrs or {})
        saved, self.style = self.style, ()
        try:
            return ET.SubElement(self.inline_parent(), tag, attrs or {})
        finally:
            self.style = saved

    def into(self, el: ET.Element, toks: list, block: bool = False, kind: str = "") -> None:
        """Read ``toks`` into ``el`` (an inline or block container)."""
        if self.depth >= MAX_NEST:
            markup.append_text(el, " ".join(_flat_text(toks).split()))
            return
        f = self.push_frame(el, block, kind)
        saved = self.style
        self.style = ()
        marker = ("end", object())
        self.stream.push([*toks, marker])
        try:
            self.run(stop=marker)
        finally:
            self.style = saved
            self.pop_frame(f)

    def here(self, toks: list) -> None:
        """Read ``toks`` where we are, as a group."""
        if not toks:
            return
        if self.depth >= MAX_NEST:
            self.text(" ".join(_flat_text(toks).split()))
            return
        marker = ("end", object())
        saved = self.style
        self.stream.push([*toks, marker])
        try:
            self.run(stop=marker)
        finally:
            self.style = saved

    def new_id(self, key: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_.-]", "-", key) or "x"
        if not re.match(r"[A-Za-z_]", base):
            base = "l-" + base
        i, n = base, self.id_next.get(base, 1)  # on from the last number given, not from 1 each time
        while i in self.ids:
            n += 1
            i = f"{base}-{n}"
        self.id_next[base] = n
        self.ids.add(i)
        return i

    # --- the main loop ------------------------------------------------------------------------------
    def run(self, stop=None, env: str | None = None) -> None:
        self.depth += 1
        handlers = self.handlers
        try:
            while True:
                t = self.next()
                if t is None:
                    return
                k = t[0]
                if k == "tx":
                    # the text tokens that follow in the same list, added in one go: a macro that doubles
                    # its argument made millions of one-letter tokens, each joined to the text so far
                    st = self.stream.stack
                    if st:
                        lst, i = st[-1]
                        j = i
                        while j < len(lst) and lst[j][0] == "tx":
                            j += 1
                        if j > i:
                            st[-1][1] = j
                            self.text(t[1] + "".join(x[1] for x in lst[i:j]))
                            continue
                    self.text(t[1])
                elif k == "sp":
                    f = self.frames[-1]
                    if not (f.block and f.para is None):
                        self.text(" ")
                elif k == "cs":
                    name = t[1]
                    if name == "end":
                        envname = self.read_env_name()
                        if envname == env:
                            return
                        if self._end_other(envname):
                            return
                        continue
                    h = handlers.get(name) or handlers.get(self.aliases.get(name, ""))
                    if h is not None:
                        h(name)
                    elif name in SYMBOLS:
                        self.text(SYMBOLS[name], lig=False)  # \textasciigrave is a backtick, not an opening quote
                    elif name in IGNORE:
                        self.skip_args(IGNORE[name])
                    elif name in TRANSPARENT:
                        self.skip_args(TRANSPARENT[name])
                        self.here(self.read_arg())
                    elif name.startswith("if") and (name in _TEX_CONDITIONALS or name in self.newifs or "@" in name):
                        self.conditional(name)
                    elif name.endswith(("true", "false")) and ("if" + name[:-4] in self.newifs
                                                              or "if" + name[:-5] in self.newifs):
                        if name.endswith("true"):
                            self.newifs["if" + name[:-4]] = True
                        else:
                            self.newifs["if" + name[:-5]] = False
                    else:
                        self.unknown(name)
                elif k == "{":
                    self.group()
                elif k == "}":
                    if stop == "}":
                        return
                elif k == "par":
                    f = self.frames[-1]
                    if f.block:
                        self.close_para(f)
                    else:
                        self.text(" ")
                elif k == "$":
                    self.dollar()
                elif k == "end":
                    if t is stop:
                        return
                elif k == "&":
                    self.ampersand()
                elif k == "~":
                    self.text("\u00a0")
                elif k == "verb":
                    self.code += 1
                    c = ET.Element("code")
                    c.text = t[1]
                    self.add_inline(c)
                    self.code -= 1
                elif k == "venv":
                    self.code_block(t[1][2], t[1][1], t[1][0])
                elif k == "url":
                    self.url(t[1])
                elif k in ("^", "_", "#"):
                    self.text(k)
        finally:
            self.depth -= 1

    def _end_other(self, envname: str) -> bool:
        """``\\end`` of something that isn't the environment being read: True to stop reading this one."""
        d = self.envdefs.get(envname)
        if d is not None and envname not in PROTECTED_ENVS:
            # counted like any expansion: an environment whose end code ends itself looped forever
            self.expansions += 1
            if self.expansions > MAX_EXPANSIONS or self.expanded > MAX_EXPANDED:
                self._expansion_stop()
                return False
            self.expanded += len(d[3])
            self.stream.push(d[3])
            return False
        if envname == "document":
            self.stream.stack.clear()
            return True
        if envname in self.env_stack:  # an inner environment was never closed: close it here
            self.stream.push([("cs", "end"), OPEN, ("tx", envname), CLOSE])
            return True
        return False

    def group(self) -> None:
        if self.depth >= MAX_NEST:
            return
        saved = self.style
        self.run(stop="}")
        self.style = saved

    # --- handlers ------------------------------------------------------------------------------------
    def _handlers(self) -> dict:
        h = {
            "begin": self.begin, "par": self.par, "\\": self.newline, "newline": self.newline,
            "linebreak": self.newline, "item": self.item_cmd, "label": self.label, "caption": self.caption,
            "captionof": self.caption, "subcaption": self.caption, "footnote": self.footnote,
            "footnotemark": self.footnotemark, "footnotetext": self.footnotetext, "thanks": self.footnote,
            "marginpar": self.marginpar, "todo": self.marginpar, "includegraphics": self.includegraphics,
            "includesvg": self.includegraphics, "epsfig": self.includegraphics, "epsfbox": self.includegraphics,
            "psfig": self.includegraphics, "includepdf": self.includegraphics, "maketitle": self.maketitle,
            "title": self.set_title, "author": self.set_author, "date": self.set_date, "abstract": self.abstract_cmd,
            "newcommand": self.newcommand, "renewcommand": self.newcommand, "providecommand": self.newcommand,
            "DeclareRobustCommand": self.newcommand, "newcommandx": self.newcommand, "def": self.define,
            "gdef": self.define, "edef": self.define, "xdef": self.define, "let": self.let,
            "DeclareMathOperator": self.mathoperator, "newenvironment": self.newenvironment,
            "renewenvironment": self.newenvironment, "newtheorem": self.newtheorem,
            "declaretheorem": self.declaretheorem, "theoremstyle": self.theoremstyle, "newif": self.newif, "newmdenv": self.boxenv, "newtcolorbox": self.boxenv,
            "NewDocumentCommand": self.documentcommand, "RenewDocumentCommand": self.documentcommand,
            "ProvideDocumentCommand": self.documentcommand, "DeclareDocumentCommand": self.documentcommand,
            "NewDocumentEnvironment": self.documentenvironment, "RenewDocumentEnvironment": self.documentenvironment,
            "appendix": self.appendix_cmd, "setcounter": self.setcounter, "graphicspath": self.graphicspath,
            "newcolumntype": self.newcolumntype, "hline": self.rule_cmd, "toprule": self.rule_cmd,
            "midrule": self.rule_cmd, "bottomrule": self.rule_cmd, "cline": self.rule_cmd, "cmidrule": self.rule_cmd,
            "hhline": self.rule_cmd, "specialrule": self.rule_cmd, "addlinespace": self.rule_cmd,
            "Xhline": self.rule_cmd, "hdashline": self.rule_cmd, "cdashline": self.rule_cmd,
            "morecmidrules": self.rule_cmd,
            "tabularnewline": self.newline, "endhead": self.endhead, "endfirsthead": self.endhead,
            "endfoot": self.endhead, "endlastfoot": self.endhead, "multicolumn": self.multicolumn,
            "multirow": self.multirow, "[": self.display_bracket,
            "(": self.inline_paren, "ensuremath": self.ensuremath, "bibliography": self.bibliography,
            "addbibresource": self.addbibresource, 
            "printbibliography": self.printbibliography, "bibitem": self.bibitem, "newblock": self.space_cmd,
            "usepackage": self.usepackage, "RequirePackage": self.usepackage, "documentclass": self.documentclass,
            "today": self.today, "rule": self.rule, "hyperref": self.hyperref, "hyperlink": self.hyperlink,
            "hypertarget": self.hypertarget, "texorpdfstring": self.texorpdfstring, "enquote": self.enquote,
            "lstinputlisting": self.inputlisting,
            "inputminted": self.inputlisting, "verbatiminput": self.inputlisting, "VerbatimInput": self.inputlisting,
            "subfloat": self.subfloat, "subcaptionbox": self.subcaptionbox, "else": self.else_branch,
            "ifthenelse": self.ifthenelse, "iftoggle": self.iftoggle, "IfFileExists": self.iffileexists,
            "string": self.string_cmd, "char": self.char_cmd, "symbol": self.char_cmd, "unichar": self.char_cmd,
            "vskip": self.skip_dimen, "hskip": self.skip_dimen, "kern": self.skip_dimen, "mskip": self.skip_dimen,
            "hrule": self.hrule, "and": self.space_cmd, "And": self.space_cmd,
            "AND": self.space_cmd, "qed": self.qed, "qedsymbol": self.qed, "QED": self.qed,
            "lettrine": self.lettrine, "bibliographystyle": self.bibliographystyle,
            "PassOptionsToPackage": self.passoptions, "xspace": self.xspace, "textquotedbl": self.symbol, "space": self.symbol, 
            
            "keywords": self.keywords_cmd, 
            "selectlanguage": self.selectlanguage, "obeylines": self.noop, 
            
            "frac": self.text_frac, "sqrt": self.text_sqrt, 
        }
        for name in SECTIONS:
            h[name] = self.section
        for name in INLINE:
            h[name] = self.inline_cmd
        for name in SWITCHES:
            h[name] = self.switch
        for name in CITES:
            h[name] = self.cite
        for name in REFS:
            h[name] = self.ref
        for name in ACCENTS:
            h[name] = self.accent_cmd
        return h

    def noop(self, name: str) -> None:
        return

    def space_cmd(self, name: str) -> None:
        self.text(" ")

    def symbol(self, name: str) -> None:
        self.text({"textquotedbl": '"', "space": " "}.get(name, ""))

    def par(self, name: str) -> None:
        f = self.frames[-1]
        if f.block:
            self.close_para(f)

    def newline(self, name: str) -> None:
        self.star()
        t = self.stream.peek()
        if t is LBRACK or t == LBRACK:
            self.read_optional(skip_space=False)
        tb = self.tables[-1] if self.tables else None
        if tb is not None and tb.depth == self.depth and self.frames[-1] is tb.frame and not self.cell_breaks:
            self.end_row(tb)
            return
        f = self.frames[-1]
        if f.block and f.para is None:
            return
        if f.kind == "list":
            return
        self.add_inline(ET.Element("br"))

    def unknown(self, name: str) -> None:
        if name in self.macros:  # past the expansion limit
            return
        self.unknown_cmds[name] = self.unknown_cmds.get(name, 0) + 1
        self.star()
        while self.read_optional(skip_space=False) is not None:
            pass
        while True:
            t = self.stream.peek()
            if t is None or t[0] != "{":
                return
            self.stream.next()
            self.group()

    def begin(self, name: str) -> None:
        env = self.read_env_name()
        d = self.envdefs.get(env)
        if d is not None and env not in PROTECTED_ENVS:
            nargs, default, b, _e = d
            args = []
            if nargs:
                if default is not None:
                    opt = self.read_optional()
                    args.append(opt if opt is not None else default)
                while len(args) < nargs:
                    args.append(self.read_arg())
            self.expansions += 1
            if self.expansions > MAX_EXPANSIONS or self.expanded > MAX_EXPANDED:
                self._expansion_stop()
                return
            body = _subst(b, args)
            self.expanded += len(body)
            self.stream.push(body)
            return
        if env == "document":
            self.start_document()
            return
        if self.depth >= MAX_NEST and env not in MATH_ENVS:
            return  # nested too deeply: its content is read where it is, its \end ignored
        self.env_stack.append(env)
        try:
            self.environment(env)
        finally:
            if self.env_stack and self.env_stack[-1] == env:
                self.env_stack.pop()
            elif env in self.env_stack:
                self.env_stack.remove(env)

    def environment(self, env: str) -> None:
        if env in MATH_ENVS:
            self.math_env(env)
        elif env in LISTS:
            self.list_env(env)
        elif env in TABULARS:
            self.tabular(env)
        elif env in FIGURES:
            self.float_env(env, "figure")
        elif env in TABLE_FLOATS:
            self.float_env(env, "table")
        elif env in QUOTES:
            el = self.add_block("blockquote")
            self.block_env(el, env, "quote")
        elif env == "abstract":
            el = self.add_block("div", {"class": "abstract"})
            ET.SubElement(el, "div", {"class": "abstract-title"}).text = "Abstract"
            self.block_env(el, env, "abstract")
        elif env == "thebibliography":
            self.thebibliography(env)
        elif env == "proof":
            self.proof(env)
        elif env in self.theorems:
            self.theorem(env)
        elif env in DRAWINGS:
            self.skip_env(env)
            self.drawings += 1
        elif env in TRANSPARENT_ENVS:
            self.skip_args(TRANSPARENT_ENVS[env])
            f = self.frames[-1]
            if f.block:
                self.close_para(f)
            saved = self.style
            self.run(env=env)
            self.style = saved
            f = self.frames[-1]
            if f.block:
                self.close_para(f)
        elif env in _FALLBACK_THEOREMS:
            self.theorems[env] = {"title": _FALLBACK_THEOREMS[env], "counter": env, "within": None,
                                  "numbered": True, "style": "plain" if env in _PLAIN_THEOREMS else "definition"}
            self.theorem(env)
        else:
            self.unknown_envs[env] = self.unknown_envs.get(env, 0) + 1
            f = self.frames[-1]
            if f.block:
                self.close_para(f)
            self.run(env=env)

    def block_env(self, el: ET.Element, env: str, kind: str) -> None:
        f = self.push_frame(el, True, kind)
        saved = self.style
        try:
            self.run(env=env)
        finally:
            self.style = saved
            self.pop_frame(f)

    def skip_env(self, env: str) -> None:
        """Skip to ``\\end{env}`` without reading (TikZ drawings and the like)."""
        st = self.stream
        nest = 0
        while True:
            t = st.next()
            if t is None:
                return
            if t[0] == "cs" and t[1] in ("begin", "end"):
                name = detok(self.read_arg()).strip()
                if name == env:
                    if t[1] == "begin":
                        nest += 1
                    elif nest == 0:
                        return
                    else:
                        nest -= 1

    def start_document(self) -> None:
        self.preamble = False
        self.frames = [_Frame(self.body, True, "body")]

    # --- sections ------------------------------------------------------------------------------------
    def section(self, name: str) -> None:
        star = self.star()
        self.read_optional()
        toks = self.read_arg()
        level = SECTIONS[name]
        if name in ("addchap", "addsec", "addpart"):
            star = True
            name = {"addchap": "chapter", "addsec": "section", "addpart": "part"}[name]
        number = ""
        depth = self.secnumdepth if self.secnumdepth is not None else (2 if self.chapters else 3)
        if not star:
            number = self.step_section(name)
            if level > depth:
                number = ""
        h = self.add_block("h1", {"class": "unnumbered"} if not number else {})
        if number:
            span = ET.SubElement(h, "span", {"class": "header-section-number"})
            span.text = number
            span.tail = " "
        kind = "appendix" if self.appendix and name == ("chapter" if self.chapters else "section") else name
        self.headings.append((h, level))
        self.label_target = (h, number or self._current_sec(), kind)  # before the text: \section{Title\label{x}}
        self.into(h, toks)
        self.label_target = (h, number or self._current_sec(), kind)

    def step_section(self, name: str) -> str:
        i = _SEC_ORDER.index(name)
        self.counters[name] += 1
        for lower in _SEC_ORDER[i + 1:]:
            self.counters[lower] = 0
        for th in self.theorems.values():
            if th.get("within") == name:
                self.counters["thm:" + th["counter"]] = 0
        if name == "chapter":
            for c in ("equation", "figure", "table"):
                self.counters[c] = 0
        if name == "part":
            return _roman(self.counters["part"])
        return self._sec_number(name)

    def _sec_number(self, name: str) -> str:
        i = _SEC_ORDER.index(name)
        start = 1 if self.chapters else 2
        parts = []
        for j in range(start, i + 1):
            n = _SEC_ORDER[j]
            v = self.counters[n]
            parts.append(_alpha(v).upper() if self.appendix and j == start else str(v))
        return ".".join(parts)

    def _current_sec(self) -> str:
        for n in reversed(_SEC_ORDER[1:]):
            if self.counters[n]:
                return self._sec_number(n)
        return ""

    def _prefix(self) -> str:
        if self.chapters and self.counters["chapter"]:
            c = self.counters["chapter"]
            return (_alpha(c).upper() if self.appendix else str(c)) + "."
        return ""

    def appendix_cmd(self, name: str) -> None:
        self.appendix = True
        self.counters["chapter" if self.chapters else "section"] = 0

    def setcounter(self, name: str) -> None:
        counter = self.arg_text()
        value = self.arg_text()
        try:
            v = int(value)
        except ValueError:
            return
        if counter == "secnumdepth":
            self.secnumdepth = v
        elif counter in ("enumi", "enumii", "enumiii", "enumiv") and self.lists:
            lst = self.lists[-1]
            if lst.el.tag == "ol":
                lst.el.set("start", str(v + 1))
                lst.count = v
        elif counter in _SEC_ORDER or counter in ("equation", "figure", "table", "footnote"):
            self.counters[counter] = v

    # --- inline commands -----------------------------------------------------------------------------
    def inline_cmd(self, name: str) -> None:
        tag, cls = INLINE[name]
        toks = self.read_arg()
        if tag == "span" and not cls:
            self.here(toks)
            return
        el = ET.Element(tag, {"class": cls} if cls else {})
        self.add_inline(el)
        if tag == "code":
            self.code += 1
        try:
            self.into(el, toks)
        finally:
            if tag == "code":
                self.code -= 1

    def switch(self, name: str) -> None:
        s = SWITCHES[name]
        if s is None:
            return
        if s == "":
            self.style = ()
        elif s.startswith("-"):
            self.style = tuple(x for x in self.style if x != s[1:])
        elif s not in self.style:
            self.style = (*self.style, s)

    def accent_cmd(self, name: str) -> None:
        toks = self.read_arg()
        base = ""
        for k, v in toks:
            if k == "tx":
                base += v
            elif k == "cs":
                base += SYMBOLS.get(v, "")
        self.text(accent(name, base))

    def today(self, name: str) -> None:
        d = date.today()  # noqa: DTZ011 - \today is the local date, as LaTeX prints it
        self.text(f"{d:%B} {d.day}, {d.year}")

    def enquote(self, name: str) -> None:
        self.star()
        self.text("“")
        self.here(self.read_arg())
        self.text("”")

    def texorpdfstring(self, name: str) -> None:
        toks = self.read_arg()
        self.read_arg()
        self.here(toks)

    def string_cmd(self, name: str) -> None:
        t = self.stream.next()
        if t is not None and t[0] == "cs":
            self.text("\\" + t[1])
        elif t is not None and t[0] == "tx":
            self.text(t[1][:1])
            if len(t[1]) > 1:
                self.stream.push([("tx", t[1][1:])])

    def char_cmd(self, name: str) -> None:
        if name == "char":
            t = self.stream.next()
            if t is None:
                return
            s = detok([t])
            if s.startswith("`"):
                self.text(s[1:2])
                return
            m = re.match(r"(\d+)|\"([0-9A-Fa-f]+)|'([0-7]+)", s)
            if m:
                v = int(m.group(1)) if m.group(1) else int(m.group(2), 16) if m.group(2) else int(m.group(3), 8)
                if 0 < v < 0x110000:
                    self.text(chr(v))
                    rest = s[m.end():]
                    if rest:
                        self.text(rest)
            return
        s = self.arg_text()
        m = re.match(r"\s*(?:\"([0-9A-Fa-f]+)|(\d+)|'([0-7]+))", s)
        if m:
            v = int(m.group(1), 16) if m.group(1) else int(m.group(2)) if m.group(2) else int(m.group(3), 8)
            if 0 < v < 0x110000:
                self.text(chr(v))

    def skip_dimen(self, name: str) -> None:
        """``\\vskip 2mm plus 1fil`` and the like: the length after the command."""
        st = self.stream
        self.skip_spaces()
        for _ in range(8):
            t = st.peek()
            if t is None:
                return
            if t[0] == "tx" and re.match(r"^[-+]?[\d.]*(?:pt|mm|cm|in|em|ex|bp|pc|dd|cc|sp|mu|fil+|true|plus|minus|=)?$",
                                         t[1]) or t[0] == "cs" and t[1] in ("baselineskip", "textwidth", "linewidth", "columnwidth", "textheight",
                                           "parskip", "fill", "stretch", "jot", "medskipamount",
                                           "smallskipamount", "bigskipamount", "parindent", "fontdimen"):
                st.next()
            elif t[0] == "sp":
                st.next()
                nxt = st.peek()
                if not (nxt is not None and nxt[0] == "tx" and nxt[1] in ("plus", "minus")):
                    return
            else:
                return

    def rule(self, name: str) -> None:
        self.read_optional()
        width = self.arg_text()
        height = self.arg_text()
        if re.match(r"^\s*0+(?:\.0*)?\s*(?:pt|mm|cm|in|em|ex|bp)?\s*$", width) or \
                re.match(r"^\s*0+(?:\.0*)?\s*(?:pt|mm|cm|in|em|ex|bp)?\s*$", height):
            return
        f = self.frames[-1]
        if f.block and (f.para is None or (len(f.para) == 0 and not (f.para.text or "").strip())):
            self.add_block("hr")

    def hrule(self, name: str) -> None:
        f = self.frames[-1]
        if f.block and f.para is None:
            self.add_block("hr")

    def qed(self, name: str) -> None:
        self.text("\u00a0□")

    def bibliographystyle(self, name: str) -> None:
        self.bst = self.arg_text()

    def passoptions(self, name: str) -> None:
        opts = self.arg_text()
        if self.arg_text() == "natbib":
            self.natbib_opts += "," + opts

    def xspace(self, name: str) -> None:
        """A space unless punctuation or the end of a group follows."""
        t = self.stream.peek()
        if t is not None and (t[0] == "tx" and t[1][:1] not in ".,;:!?)]'/-" or t[0] in ("cs", "$", "~")) \
                and not (t[0] == "cs" and t[1] in ("footnote", "\\", "/", " ", "xspace")):
            self.text(" ")

    def lettrine(self, name: str) -> None:
        self.read_optional()
        self.here(self.read_arg())
        self.here(self.read_arg())

    def text_frac(self, name: str) -> None:
        a, b = self.read_arg(), self.read_arg()
        self.here(a)
        self.text("/")
        self.here(b)

    def text_sqrt(self, name: str) -> None:
        self.read_optional()
        self.text("√")
        self.here(self.read_arg())

    def keywords_cmd(self, name: str) -> None:
        toks = self.read_arg()
        if self.preamble:
            self.keywords = [k.strip() for k in re.split(r"[,;]|\\and|\\sep", detok(toks)) if k.strip()]
            return
        self.here(toks)

    def selectlanguage(self, name: str) -> None:
        lang = self.arg_text()
        if not self.lang and lang in LANGS:
            self.lang = LANGS[lang]

    def marginpar(self, name: str) -> None:
        self.read_optional()
        toks = self.read_arg()
        span = self.add_inline(ET.Element("span", {"class": "marginpar"}))
        span.text = " ["
        inner = ET.SubElement(span, "span")
        self.into(inner, toks)
        inner.tail = "] "

    # --- conditionals ------------------------------------------------------------------------------
    def conditional(self, name: str) -> None:
        if name == "iffalse":
            truth = False
        elif name in self.newifs:
            truth = self.newifs[name]
        else:
            truth = True
            st = self.stream
            if name == "ifx":
                st.next()
                st.next()
            elif name in ("ifdefined", "ifcat", "if"):
                st.next()
                if name != "ifdefined":
                    st.next()
            elif name == "ifcsname":
                for _ in range(1000):
                    t = st.next()
                    if t is None or t == ("cs", "endcsname"):
                        break
        if not truth:
            self.skip_branch(else_ok=True)

    def else_branch(self, name: str) -> None:
        # reached \else while reading a true branch: skip to \fi
        self.skip_branch(else_ok=False)

    def skip_branch(self, else_ok: bool) -> None:
        st = self.stream
        nest = 0
        while True:
            t = st.next()
            if t is None:
                return
            if t[0] != "cs":
                continue
            n = t[1]
            if n.startswith("if") and (n in _TEX_CONDITIONALS or n in self.newifs or "@" in n):
                nest += 1
            elif n == "fi":
                if nest == 0:
                    return
                nest -= 1
            elif n == "else" and nest == 0 and else_ok:
                return

    def newif(self, name: str) -> None:
        t = self.stream.next()
        if t is not None and t[0] == "cs" and t[1].startswith("if"):
            self.newifs[t[1]] = False

    def ifthenelse(self, name: str) -> None:
        self.read_arg()
        yes = self.read_arg()
        self.read_arg()
        self.here(yes)

    def iftoggle(self, name: str) -> None:
        self.read_arg()
        yes = self.read_arg()
        self.read_arg()
        self.here(yes)

    def iffileexists(self, name: str) -> None:
        self.read_arg()
        self.read_arg()
        self.here(self.read_arg())

    # --- definitions ---------------------------------------------------------------------------------
    def _defined_name(self) -> str | None:
        self.skip_spaces()
        t = self.stream.next()
        if t is None:
            return None
        if t[0] == "{":
            toks = self._until_close()
            cs = next((x for x in toks if x[0] == "cs"), None)
            return cs[1] if cs else None
        return t[1] if t[0] == "cs" else None

    def _may_define(self, name: str, kind: str) -> bool:
        if name in PROTECTED:
            return False
        if self.include_stack and self.include_stack[-1].suffix.lower() == ".sty":
            return "@" not in name and not any(name in t for t in (self.handlers, IGNORE, TRANSPARENT, SYMBOLS))
        return not (kind == "provide" and (name in self.macros or name in self.handlers))

    def newcommand(self, name: str) -> None:
        self.star()
        cname = self._defined_name()
        nopt = self.read_optional()
        nargs = 0
        if nopt is not None:
            try:
                nargs = max(0, min(9, int(detok(nopt).strip() or 0)))
            except ValueError:
                nargs = 0
        default = self.read_optional()
        body = self.read_arg()
        if cname and self._may_define(cname, "provide" if name == "providecommand" else "new"):
            self.macros[cname] = _Macro(nargs, default, _params(body))

    def define(self, name: str) -> None:
        t = self.stream.next()
        if t is None or t[0] != "cs":
            return
        cname = t[1]
        params = []
        st = self.stream
        while True:
            p = st.next()
            if p is None or len(params) > 1000:
                return
            if p[0] == "{":
                break
            params.append(p)
        body = _params(self._until_close())
        if not self._may_define(cname, "new"):
            return
        conv = _params(params)
        self.delimited.pop(cname, None)
        if all(p[0] == "param" for p in conv):
            self.macros[cname] = _Macro(len(conv), None, body)
            return
        # one parameter read up to a delimiter (\def\foo#1.{...}); other shapes are left as unknown commands
        self.macros.pop(cname, None)
        if len(conv) >= 2 and conv[0][0] == "param" and conv[1][0] != "param" and \
                sum(1 for p in conv if p[0] == "param") == 1:
            self.delimited[cname] = (conv[1], body)

    def expand_delimited(self, name: str) -> None:
        """A ``\\def\\foo#1<delimiter>{...}`` macro: the argument runs to the delimiter."""
        delim, body = self.delimited[name]
        self.expansions += 1
        if self.expansions > MAX_EXPANSIONS or self.expanded > MAX_EXPANDED:
            self.expansion_capped = True
            self.lost.append("Macros expanded too many times (a macro that calls itself?); the rest were left as they are")
            return
        st = self.stream
        arg: list = []
        depth = 0
        while len(arg) < 100_000:
            t = st.next()
            if t is None:
                break
            if t[0] == "end":
                st.unread(t)
                break
            if depth == 0:
                if delim[0] == "tx" and t[0] == "tx" and delim[1][:1] and delim[1][0] in t[1]:
                    i = t[1].index(delim[1][0])
                    if i:
                        arg.append(("tx", t[1][:i]))
                    if t[1][i + 1:]:
                        st.push([("tx", t[1][i + 1:])])
                    break
                if t == delim:
                    break
            if t[0] == "{":
                depth += 1
            elif t[0] == "}":
                depth -= 1
            arg.append(t)
        if len(arg) >= 2 and arg[0][0] == "{" and arg[-1][0] == "}":
            arg = arg[1:-1]
        out = _subst(body, [arg])
        self.expanded += len(out)
        st.push(out)


    def let(self, name: str) -> None:
        st = self.stream
        t = st.next()
        if t is None or t[0] != "cs":
            return
        nxt = st.next()
        while nxt is not None and (nxt[0] == "sp" or nxt == ("tx", "=")):
            nxt = st.next()
        if nxt is not None and nxt[0] == "tx" and nxt[1].startswith("="):
            rest = nxt[1][1:]
            nxt = ("tx", rest) if rest else st.next()
        if nxt is None or not self._may_define(t[1], "new"):
            return
        if nxt[0] == "cs":
            if nxt[1] in self.macros:
                self.macros[t[1]] = self.macros[nxt[1]]
            else:
                self.macros.pop(t[1], None)
                self.aliases[t[1]] = nxt[1]
        else:
            self.macros[t[1]] = _Macro(0, None, [nxt])

    def mathoperator(self, name: str) -> None:
        star = self.star()
        cname = self._defined_name()
        body = self.read_arg()
        if cname and self._may_define(cname, "new"):
            self.macros[cname] = _Macro(0, None, [("cs", "operatorname"), *([("tx", "*")] if star else []),
                                                  OPEN, *body, CLOSE])

    def newenvironment(self, name: str) -> None:
        self.star()
        env = self.arg_text()
        nopt = self.read_optional()
        nargs = 0
        if nopt is not None:
            try:
                nargs = max(0, min(9, int(detok(nopt).strip() or 0)))
            except ValueError:
                nargs = 0
        default = self.read_optional()
        b = self.read_arg()
        e = self.read_arg()
        if env and env not in PROTECTED_ENVS and not (self.include_stack and self.include_stack[-1].suffix == ".sty"
                                                      and (env in MATH_ENVS or env in LISTS or env in TABULARS)):
            self.envdefs[env] = (nargs, default, _params(b), _params(e))

    def documentcommand(self, name: str) -> None:
        cname = self._defined_name()
        spec = detok(self.read_arg())
        body = self.read_arg()
        args = re.findall(r"[mo]|O\{[^}]*\}|s|t.", spec)
        nargs = len(args)
        default = None
        if args and args[0].startswith(("o", "O")):
            default = self.tokenize(args[0][2:-1]) if args[0].startswith("O") else []
        if cname and self._may_define(cname, "new"):
            self.macros[cname] = _Macro(nargs, default, _params(body))

    def documentenvironment(self, name: str) -> None:
        env = self.arg_text()
        spec = detok(self.read_arg())
        b = self.read_arg()
        e = self.read_arg()
        nargs = len(re.findall(r"[mo]|O\{[^}]*\}", spec))
        if env and env not in PROTECTED_ENVS:
            self.envdefs[env] = (nargs, None, _params(b), _params(e))

    def newtheorem(self, name: str) -> None:
        star = self.star()
        env = self.arg_text()
        shared = self.read_optional()
        title = self.read_arg()
        within = self.read_optional()
        if not env:
            return
        counter = detok(shared).strip() if shared else env
        if shared and counter in self.theorems:
            counter = self.theorems[counter]["counter"]
        self.theorems[env] = {"title": title, "counter": counter, "numbered": not star,
                              "within": detok(within).strip() if within else (
                                  self.theorems.get(counter, {}).get("within") if shared else None),
                              "style": self.theorem_style}

    def declaretheorem(self, name: str) -> None:
        opts = detok(self.read_optional() or [])
        env = self.arg_text()
        m = re.search(r"name\s*=\s*\{?([^,}]+)", opts)
        title = m.group(1).strip() if m else env.capitalize()
        sib = re.search(r"sibling\s*=\s*([\w*]+)", opts)
        within = re.search(r"(?:numberwithin|parent)\s*=\s*(\w+)", opts)
        counter = sib.group(1) if sib else env
        if sib and counter in self.theorems:
            counter = self.theorems[counter]["counter"]
        self.theorems[env] = {"title": self.tokenize(title), "counter": counter,
                              "numbered": "numbered=no" not in opts.replace(" ", ""),
                              "within": within.group(1) if within else None,
                              "style": "definition" if "definition" in opts else self.theorem_style}

    def boxenv(self, name: str) -> None:
        """A framed box defined with newmdenv or newtcolorbox: read as its content."""
        self.read_optional()
        env = self.arg_text()
        if name == "newtcolorbox":
            self.read_optional()
            self.read_optional()
            self.read_arg()
        if env and env not in PROTECTED_ENVS:
            self.envdefs[env] = (0, None, [], [])

    def theoremstyle(self, name: str) -> None:
        self.theorem_style = self.arg_text() or "plain"

    def newcolumntype(self, name: str) -> None:
        col = self.arg_text()
        self.read_optional()
        spec = self.arg_text()
        if len(col) == 1:
            self.column_types[col] = spec

    def graphicspath(self, name: str) -> None:
        raw = self.arg_text()
        paths = re.findall(r"\{([^{}]*)\}", raw) or [raw]
        self.graphics_paths = [""] + [p.strip().rstrip("/") + "/" for p in paths if p.strip()]

    def usepackage(self, name: str) -> None:
        opts = self.read_optional()
        names = [n.strip() for n in self.arg_text().split(",") if n.strip()]
        opts_s = detok(opts) if opts else ""
        for pkg in names:
            if pkg == "babel" and opts_s:
                last = [o.strip() for o in opts_s.split(",") if o.strip()]
                main = next((o.split("=", 1)[1] for o in last if o.startswith("main=")), last[-1] if last else "")
                if main in LANGS:
                    self.lang = LANGS[main]
            elif pkg == "polyglossia":
                pass
            elif pkg == "natbib":
                self.natbib = (self.natbib or "") + "," + opts_s + self.natbib_opts
            elif pkg == "biblatex":
                self.biblatex = opts_s
            elif self.base_dir is not None and not self.preamble_sty_done(pkg):
                self._local_sty(pkg)

    def preamble_sty_done(self, pkg: str) -> bool:
        if pkg in self.sty_done:
            return True
        self.sty_done.add(pkg)
        return False

    def _local_sty(self, pkg: str) -> None:
        """A package that sits beside the document: its macro definitions are read, nothing else."""
        if self.base_dir is None or len(self.include_stack) >= 2:
            return
        p = safe_path(self.base_dir, pkg + ".sty", self.top)
        if p is None or not p.is_file():
            return
        try:
            if p.stat().st_size > 512 * 1024:
                return
        except OSError:
            return
        toks = self.include_tokens(pkg + ".sty", len(self.include_stack), quiet=True)
        if not toks:
            return
        # only the definitions are taken: a package's own TeX programming is never run
        saved_frames, saved_style = self.frames, self.style
        self.frames = [_Frame(ET.Element("div"), True, "sink")]
        self.include_stack.append(p)
        marker = ("end", object())
        self.stream.push([*toks, marker])
        definers = {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand", "def", "gdef",
                    "DeclareMathOperator", "newenvironment", "renewenvironment", "newtheorem", "theoremstyle", "newif",
                    "RequirePackage", "PassOptionsToPackage", "bibliographystyle",
                    "NewDocumentCommand", "RenewDocumentCommand", "ProvideDocumentCommand", "DeclareDocumentCommand"}
        try:
            while True:
                t = self.stream.next()
                if t is None or t is marker:
                    break
                if t[0] == "cs" and t[1] in definers:
                    self.handlers[t[1]](t[1])
        finally:
            self.include_stack.pop()
            self.frames, self.style = saved_frames, saved_style

    def documentclass(self, name: str) -> None:
        opts = self.read_optional()
        cls = self.arg_text()
        self.docclass = cls
        self.chapters = cls in BOOK_CLASSES
        if opts:
            for o in detok(opts).split(","):
                if o.strip() in LANGS and not self.lang:
                    self.lang = LANGS[o.strip()]

    # --- references ----------------------------------------------------------------------------------
    def label(self, name: str) -> None:
        key = self.arg_text()
        if not key or key in self.labels:
            return
        target = self.label_target
        if self.lists and self.lists[-1].item_frame is not None and self.frames and \
                any(f is self.lists[-1].item_frame for f in self.frames) and \
                (target is None or target[2] != "figure"):
            lst = self.lists[-1]
            if lst.el.tag == "ol" and (target is None or not _inside(target[0], lst.el)):
                li = lst.item_frame.el
                target = (li, self._item_label(lst, lst.count), "item")
        if target is None:
            anchor = self.add_inline(ET.Element("span"))
            i = self.new_id(key)
            anchor.set("id", i)
            self.labels[key] = (i, "", "", anchor)
            return
        el, number, kind = target
        if el.get("id") is None:
            i = self.new_id(key)
            el.set("id", i)
        else:
            i = self.new_id(key)
            span = ET.Element("span", {"id": i})
            el.insert(0, span)
        el.set("data-label", el.get("data-label", key) if el.get("data-label") else key)
        self.labels[key] = (i, number, kind, el)
        if kind.startswith("sub"):
            parent = next((f for f in reversed(self.floats) if not f.kind.startswith("sub")), None)
            if parent is not None:
                parent.pending.append(key)

    def ref(self, name: str) -> None:
        self.star()
        kind = REFS[name]
        keys = [k.strip() for k in self.arg_text().split(",") if k.strip()]
        if name in ("crefrange", "Crefrange"):
            keys += [k.strip() for k in self.arg_text().split(",") if k.strip()]
        a = ET.Element("a", {"class": "ref", "data-reference-type": kind, "data-reference": ",".join(keys)})
        a.text = "??"
        self.add_inline(a)
        self.refs.append((a, keys, kind))

    def hyperref(self, name: str) -> None:
        opt = self.read_optional()
        if opt is None:  # \hyperref{url}{category}{name}{text}
            url = self.arg_text()
            self.read_arg()
            self.read_arg()
            a = self.add_inline(ET.Element("a", {"href": url}))
            self.into(a, self.read_arg())
            return
        key = detok(opt).strip()
        a = self.add_inline(ET.Element("a", {"class": "ref", "data-reference-type": "hyperref",
                                             "data-reference": key}))
        self.into(a, self.read_arg())
        self.refs.append((a, [key], "hyperref"))

    def hyperlink(self, name: str) -> None:
        key = self.arg_text()
        a = self.add_inline(ET.Element("a", {"class": "ref", "data-reference-type": "hyperlink",
                                             "data-reference": key}))
        self.into(a, self.read_arg())
        self.refs.append((a, ["\0" + key], "hyperref"))

    def hypertarget(self, name: str) -> None:
        key = self.arg_text()
        span = self.add_inline(ET.Element("span"))
        i = self.new_id(key)
        span.set("id", i)
        self.labels.setdefault("\0" + key, (i, "", "", span))
        self.into(span, self.read_arg())

    # --- notes ---------------------------------------------------------------------------------------
    def footnote(self, name: str) -> None:
        opt = self.read_optional() if name != "thanks" else None
        toks = self.read_arg()
        shown = self._note_number(opt)
        aside = self._note_call(shown)
        self._note_text(aside, toks)

    def _note_number(self, opt: list | None) -> str:
        """The number a note shows: its optional argument (``\\thefootnote`` is the current one), else the next."""
        if opt is not None:
            s = detok(opt).strip()
            if "thefootnote" in s or "value{footnote}" in s:
                return str(self.counters["footnote"])
            if s:
                return re.sub(r"\\[A-Za-z@]+\s*", "", s).strip("{} ") or s
        self.counters["footnote"] += 1
        return str(self.counters["footnote"])

    def _note_text(self, aside: ET.Element, toks: list) -> None:
        saved_target, saved_lists, saved_tables = self.label_target, self.lists, self.tables
        self.lists, self.tables = [], []
        self.label_target = (aside, aside.get("data-shown", ""), "footnote")  # a label in a note gives its number
        try:
            self.into(aside, toks, block=True, kind="note")
        finally:
            self.label_target, self.lists, self.tables = saved_target, saved_lists, saved_tables

    def _note_call(self, shown: str, nid: str | None = None) -> ET.Element:
        """A note call showing ``shown``: to the note ``nid`` when given, else to a new note (returned)."""
        new = nid is None
        if new:
            self.note_seq += 1
            nid = f"fn{self.note_seq}"
            self.ids.add(nid)
        a = ET.Element("a", {"class": "noteref", "epub:type": "noteref", "href": f"#{nid}"})
        if new:
            a.set("id", f"fnref{self.note_seq}")
        ET.SubElement(a, "sup").text = shown
        saved, self.style = self.style, ()
        self.add_inline(a)
        self.style = saved
        if not new:
            return self.notes[nid]
        aside = ET.Element("aside", {"epub:type": "footnote", "id": nid, "data-shown": shown})
        self.notes[nid] = aside
        return aside

    def footnotemark(self, name: str) -> None:
        opt = self.read_optional()
        shown = self._note_number(opt)
        if opt is not None:
            # \footnotemark[1]: the note numbered 1; \footnotemark[\thefootnote]: a mark whose text comes later
            # (\footnotetext), so it joins a waiting mark with that number, not an older note
            waiting = "thefootnote" in detok(opt)
            for nid, aside in reversed(self.notes.items()):
                if aside.get("data-shown") == shown and (not waiting or nid in self.marks):
                    self._note_call(shown, nid)
                    return
        aside = self._note_call(shown)
        self.marks.append(aside.get("id"))

    def footnotetext(self, name: str) -> None:
        opt = self.read_optional()
        toks = self.read_arg()
        aside = None
        if opt is not None:
            shown = self._note_number(opt)
            aside = next((a for a in reversed(self.notes.values()) if a.get("data-shown") == shown and not len(a)), None)
            if aside is not None and aside.get("id") in self.marks:
                self.marks.remove(aside.get("id"))
        elif self.marks:
            aside = self.notes.get(self.marks.pop(0))
        if aside is None:  # text for a mark that never came: kept, as a note of its own
            self.note_seq += 1
            aside = ET.Element("aside", {"epub:type": "footnote", "id": f"fn{self.note_seq}"})
            self.notes[aside.get("id")] = aside
        self._note_text(aside, toks)

    # --- lists ---------------------------------------------------------------------------------------
    def list_env(self, env: str) -> None:
        tag = LISTS[env]
        opts = None
        if env == "list":
            label = self.read_arg()
            self.read_arg()
            tag = "ol" if any(t[0] == "cs" and t[1] in ("arabic", "roman", "alph", "Alph", "Roman", "theenumi")
                              for t in label) else "ul"
        else:
            opts = self.read_optional()
        level = sum(1 for lst in self.lists if lst.el.tag == "ol") + 1 if tag == "ol" else 0
        el = self.add_block(tag)
        f = self.push_frame(el, False, "list")
        lst = _List(el, tag, len(self.lists) + 1, f, level)
        if tag == "ol":
            o = detok(opts).strip() if opts else ""
            typ = ""
            m = re.search(r"\\(alph|Alph|roman|Roman|arabic)\*?", o)
            if m:
                typ = {"alph": "a", "Alph": "A", "roman": "i", "Roman": "I", "arabic": "1"}[m.group(1)]
            elif o and "=" not in o:
                m2 = re.search(r"[aAiI1]", o)
                typ = m2.group(0) if m2 else ""
            elif level >= 2 and not o:
                typ = {2: "a", 3: "i", 4: "A"}.get(level, "")
            if typ and typ != "1":
                el.set("type", typ)
            lst.type = typ or "1"
            sm = re.search(r"start\s*=\s*(-?\d+)", o)
            if sm:
                el.set("start", sm.group(1))
                lst.count = int(sm.group(1)) - 1
        self.lists.append(lst)
        saved = self.style
        try:
            self.run(env=env)
        finally:
            self.style = saved
            if lst in self.lists:
                self.lists.remove(lst)
            self.pop_frame(f)

    def item_cmd(self, name: str) -> None:
        self.item(implicit=False)

    def item(self, implicit: bool) -> None:
        if not self.lists:
            if not implicit:
                opt = self.read_optional()
                if opt:
                    self.here(opt)
                    self.text(" ")
            return
        lst = self.lists[-1]
        opt = None if implicit else self.read_optional()
        # close the item before
        if lst.item_frame is not None:
            self.pop_frame(lst.item_frame)
            lst.item_frame = None
        if lst.frame not in self.frames:
            self.frames.append(lst.frame)
        else:
            while self.frames[-1] is not lst.frame:
                self.pop_frame(self.frames[-1])
        lst.count += 1
        if lst.el.tag == "dl":
            if opt is not None:
                dt = ET.SubElement(lst.el, "dt")
                self.into(dt, opt)
            dd = ET.SubElement(lst.el, "dd")
            lst.item_frame = self.push_frame(dd, True, "item")
        else:
            li = ET.SubElement(lst.el, "li")
            lst.item_frame = self.push_frame(li, True, "item")
            if opt is not None:
                self.here(opt)
                self.text(" ")
        self.label_target = (lst.item_frame.el, self._item_label(lst, lst.count), "item")

    def _item_label(self, lst: _List, n: int) -> str:
        t = lst.type or "1"
        if t == "a":
            return _alpha(n)
        if t == "A":
            return _alpha(n).upper()
        if t == "i":
            return _roman(n).lower()
        if t == "I":
            return _roman(n)
        return str(n)

    # --- tables --------------------------------------------------------------------------------------
    def tabular(self, env: str) -> None:
        if env in ("tabular*", "tabularx", "tabulary", "xltabular", "tabu", "longtabu", "NiceTabular*", "TAB"):
            if env in ("tabu", "longtabu"):
                t = self.stream.peek()
                if t is not None and t[0] == "tx" and t[1] in ("to", "spread"):
                    self.stream.next()
                    self.read_arg()
            else:
                self.read_arg()
        self.read_optional()
        spec = self.arg_text()
        if env in ("tblr", "longtblr", "talltblr"):
            m = re.search(r"colspec\s*=\s*\{(.*)\}", spec)
            spec = m.group(1) if m else ("" if "=" in spec else spec)
        aligns = _colspec(spec, self.column_types)
        tb = _Table(self.depth + 1, aligns, env)
        self.tables.append(tb)
        self.new_row(tb)
        saved_lists = self.lists
        self.lists = []
        saved = self.style
        try:
            self.run(env=env)
        finally:
            self.style = saved
            self.lists = saved_lists
            self.end_cell(tb)
            if tb.row and (len(tb.row) > 1 or _has_content(tb.row[0])):
                tb.rows.append(tb.row)
            tb.row = []
            self.tables.remove(tb)
        table = self.build_table(tb)
        if table is None:
            return
        if tb.caption is not None:
            table.insert(0, tb.caption)
        self.place_table(table)

    def place_table(self, table: ET.Element) -> None:
        f = self.frames[-1]
        if f.block:
            self.close_para(f)
            f.el.append(table)
        elif f.kind == "list":
            self.item(implicit=True)
            self.frames[-1].el.append(table)
        else:
            self.inline_parent().append(table)

    def new_row(self, tb: _Table) -> None:
        tb.row = []
        self.new_cell(tb)

    def new_cell(self, tb: _Table) -> None:
        tb.cell = ET.Element("td")
        tb.row.append(tb.cell)
        tb.frame = self.push_frame(tb.cell, False, "cell")

    def end_cell(self, tb: _Table) -> None:
        if tb.frame is not None:
            self.pop_frame(tb.frame)
            tb.frame = None

    def end_row(self, tb: _Table) -> None:
        self.end_cell(tb)
        if tb.row and (len(tb.row) > 1 or _has_content(tb.row[0])):
            tb.rows.append(tb.row)
        self.new_row(tb)

    def ampersand(self) -> None:
        tb = self.tables[-1] if self.tables else None
        if tb is not None and tb.depth == self.depth and self.frames[-1] is tb.frame:
            self.end_cell(tb)
            self.new_cell(tb)
        elif self.env_stack and self.env_stack[-1] == "tabbing":
            self.text(" ")
        else:
            self.text("&")

    def rule_cmd(self, name: str) -> None:
        if name == "cline" or name == "cdashline":
            self.read_arg()
        elif name == "cmidrule":
            self.read_optional()
            t = self.stream.peek()
            if t is not None and t[0] == "tx" and t[1].startswith("("):
                while True:
                    t = self.stream.next()
                    if t is None or (t[0] == "tx" and ")" in t[1]):
                        if t is not None and t[1].index(")") + 1 < len(t[1]):
                            self.stream.push([("tx", t[1][t[1].index(")") + 1:])])
                        break
            self.read_arg()
        elif name == "specialrule":
            self.skip_args("mmm")
        elif name in ("addlinespace", "toprule", "midrule", "bottomrule", "Xhline"):
            if name == "Xhline":
                self.read_arg()
            else:
                self.read_optional()
        elif name == "hhline":
            self.read_arg()
        tb = self.tables[-1] if self.tables else None
        if tb is not None and name not in ("addlinespace", "toprule"):
            tb.rule_after.add(len(tb.rows))

    def endhead(self, name: str) -> None:
        tb = self.tables[-1] if self.tables else None
        if tb is None:
            return
        self.end_cell(tb)
        if tb.row and (len(tb.row) > 1 or _has_content(tb.row[0])):
            tb.rows.append(tb.row)
        rows, tb.rows = tb.rows, []
        if name == "endfirsthead":
            tb.head = rows
        elif name == "endhead":
            if tb.head is None:
                tb.head = rows
        elif tb.foot is None:
            tb.foot = rows
        tb.rule_after = set()
        self.new_row(tb)

    def multicolumn(self, name: str) -> None:
        n = self.arg_text()
        spec = self.arg_text()
        toks = self.read_arg()
        tb = self.tables[-1] if self.tables else None
        if tb is not None and tb.cell is not None and self.frames[-1] is tb.frame:
            try:
                span = max(1, min(int(n), 100))
            except ValueError:
                span = 1
            if span > 1:
                tb.cell.set("colspan", str(span))
            al = _colspec(spec, self.column_types)
            if al and al[0]:
                tb.cell.set("style", f"text-align: {al[0]}")
            self.cell_breaks += 1
            try:
                self.here(toks)
            finally:
                self.cell_breaks -= 1
        else:
            self.here(toks)

    def multirow(self, name: str) -> None:
        self.read_optional()
        n = self.arg_text()
        self.read_optional()
        self.read_arg()  # width, or *
        self.read_optional()
        toks = self.read_arg()
        tb = self.tables[-1] if self.tables else None
        if tb is not None and tb.cell is not None and self.frames[-1] is tb.frame:
            try:
                span = min(abs(int(float(n))), 65534)
            except (ValueError, OverflowError):  # "x", "nan", "1e400"
                span = 1
            if span > 1:
                tb.cell.set("rowspan", str(min(span, 500)))
        self.cell_breaks += 1
        try:
            self.here(toks)
        finally:
            self.cell_breaks -= 1

    def build_table(self, tb: _Table) -> ET.Element | None:
        head = tb.head
        rows = tb.rows
        if head is None and len(rows) > 1 and 1 in tb.rule_after:
            head, rows = rows[:1], rows[1:]
        if not rows and not head:
            return None
        table = ET.Element("table")
        width = len(tb.aligns)
        covered: dict[int, int] = {}  # column -> rows still covered by a rowspan

        def emit(group: ET.Element, row: list[ET.Element], header: bool):
            tr = ET.SubElement(group, "tr")
            col = 0
            for cell in row:
                while covered.get(col, 0) > 0 and not _has_content(cell):
                    covered[col] -= 1
                    cell = None
                    col += 1
                    break
                if cell is None:
                    continue
                if header:
                    cell.tag = "th"
                span = int(cell.get("colspan", "1"))
                if cell.get("style") is None and col < width and tb.aligns[col]:
                    cell.set("style", f"text-align: {tb.aligns[col]}")
                rs = int(cell.get("rowspan", "1"))
                if rs > 1:
                    for c in range(col, col + span):
                        covered[c] = rs - 1
                tr.append(cell)
                col += span
            for c in list(covered):
                if c >= col and covered[c] > 0:
                    covered[c] -= 1

        if head:
            thead = ET.SubElement(table, "thead")
            for r in head:
                emit(thead, r, True)
        tbody = ET.SubElement(table, "tbody")
        for r in rows:
            emit(tbody, r, False)
        if tb.foot:
            tfoot = ET.SubElement(table, "tfoot")
            for r in tb.foot:
                emit(tfoot, r, False)
        return table

    # --- floats --------------------------------------------------------------------------------------
    def float_env(self, env: str, kind: str) -> None:
        sub = env.startswith("sub")
        if env in ("wrapfigure", "wrapfigure*", "wraptable", "wraptable*"):
            self.read_optional()
            self.read_arg()
            self.read_optional()
            self.read_arg()
        elif sub:
            self.read_optional()
            self.read_arg()
        else:
            self.read_optional()
        parent = self.floats[-1] if self.floats else None
        el = self.add_block("figure" if kind == "figure" or env == "listing" else "div",
                            {"class": "table-float"} if kind == "table" and env != "listing" else {})
        f = self.push_frame(el, True, "float")
        fl = _Float(el, "listing" if env == "listing" else ("sub" + kind if sub else kind), f)
        fl.number = ""
        if sub and parent is not None:
            parent.sub += 1
            fl.number = (parent.number or "") + _alpha(parent.sub)
        self.floats.append(fl)
        saved_target = self.label_target
        saved = self.style
        try:
            self.run(env=env)
        finally:
            self.style = saved
            self.floats.remove(fl)
            self.pop_frame(f)
            self.label_target = saved_target
        self.finish_float(fl)

    def caption(self, name: str) -> None:
        kind_override = None
        if name == "captionof":
            self.star()
            kind_override = self.arg_text()
        else:
            self.star()
        self.read_optional()
        toks = self.read_arg()
        fl = self.floats[-1] if self.floats else None
        tb = self.tables[-1] if self.tables else None
        kind = kind_override or (fl.kind if fl else "figure")
        if tb is not None and tb.name.startswith(("longtable", "xltabular", "supertabular", "longtabu")) and \
                (fl is None or not _inside(tb.cell, fl.el) if tb.cell is not None else True):
            # longtable's own caption
            self.counters["table"] += 1
            number = self._prefix() + str(self.counters["table"])
            cap = ET.Element("caption")
            span = ET.SubElement(cap, "span", {"class": "table-number"})
            span.text = f"Table {number}: "
            self.label_target = (cap, number, "table")  # before the text: \caption{\label{x} ...}
            self.into(cap, toks)
            tb.caption = cap
            self.label_target = (cap, number, "table")
            return
        if kind.startswith("sub") and fl is not None:
            number = fl.number or "a"
            prefix = f"({number[-1]}) "
        elif kind in ("figure", "table", "listing"):
            self.counters[kind] += 1
            number = self._prefix() + str(self.counters[kind])
            prefix = f"{kind.capitalize()} {number}: "
            if fl is not None and not fl.kind.startswith("sub"):
                fl.number = number
        else:
            number, prefix = "", ""
        if fl is None:
            cap = self.add_block("p", {"class": "caption"})
        else:
            f = self.frames[-1]
            if f.block:
                self.close_para(f)
            cap = ET.SubElement(fl.el, "figcaption")
            fl.captioned = True
        if prefix:
            span = ET.SubElement(cap, "span", {"class": f"{kind.replace('sub', '')}-number"})
            span.text = prefix
        target = fl.el if fl is not None else cap
        self.label_target = (target, number, kind)  # before the text: \caption{\label{x} ...}
        self.into(cap, toks)
        self.label_target = (target, number, kind)

    def subfloat(self, name: str) -> None:
        cap = self.read_optional()
        self.read_optional()
        toks = self.read_arg()
        parent = self.floats[-1] if self.floats else None
        el = self.add_block("figure")
        f = self.push_frame(el, True, "float")
        fl = _Float(el, "subfigure", f)
        if parent is not None:
            parent.sub += 1
            fl.number = (parent.number or "") + _alpha(parent.sub)
        self.floats.append(fl)
        try:
            self.here(toks)
            if cap:
                self.close_para(f)
                c = ET.SubElement(el, "figcaption")
                ET.SubElement(c, "span", {"class": "figure-number"}).text = f"({_alpha(parent.sub if parent else 1)}) "
                self.into(c, cap)
        finally:
            self.floats.remove(fl)
            self.pop_frame(f)
        self.finish_float(fl)

    def subcaptionbox(self, name: str) -> None:
        self.star()
        self.read_optional()
        cap = self.read_arg()
        self.read_optional()
        self.read_optional()
        toks = self.read_arg()
        self.stream.push([("cs", "subfloat"), LBRACK, *cap, RBRACK, OPEN, *toks, CLOSE])

    def finish_float(self, fl: _Float) -> None:
        el = fl.el
        for key in fl.pending:
            i, num, kind, target = self.labels[key]
            self.labels[key] = (i, fl.number + num, kind, target)
        # pictures sit straight in the figure, not in paragraphs of their own
        for p in [c for c in el if c.tag == "p"]:
            if _only_pictures(p):
                i = list(el).index(p)
                el.remove(p)
                kids = list(p)
                if p.text and p.text.strip():
                    continue
                for k, c in enumerate(kids):
                    c.tail = None
                    el.insert(i + k, c)
        tables = [c for c in el.iter("table")]
        caps = [c for c in el if c.tag == "figcaption"]
        if fl.kind == "table" and len(tables) == 1 and len(caps) <= 1:
            table = tables[0]
            if caps:
                cap = caps[0]
                el.remove(cap)
                cap.tag = "caption"
                table.insert(0, cap)
            for a in ("id", "data-label"):
                if el.get(a) is not None and table.get(a) is None:
                    table.set(a, el.get(a))
            self._unwrap(el)
        elif fl.kind == "table" and not tables or fl.kind == "table":
            el.tag = "figure"
            el.attrib.pop("class", None)
        elif fl.kind == "figure" and not el.findall(".//img") and len(tables) == 1 and len(caps) <= 1:
            # a table put in a figure environment
            table = tables[0]
            if caps:
                el.remove(caps[0])
                caps[0].tag = "caption"
                table.insert(0, caps[0])
            if el.get("id") is not None and table.get("id") is None:
                table.set("id", el.get("id"))
                table.set("data-label", el.get("data-label", ""))
            self._unwrap(el)

    def _unwrap(self, el: ET.Element) -> None:
        """Put ``el``'s children where it is."""
        parent = self._parent_of(el)
        if parent is None:
            return
        i = list(parent).index(el)
        parent.remove(el)
        kids = list(el)
        for k, c in enumerate(kids):
            parent.insert(i + k, c)
        if el.tail and el.tail.strip():
            markup.append_text(parent if not kids else kids[-1], el.tail)

    def _parent_of(self, el: ET.Element) -> ET.Element | None:
        for f in reversed(self.frames):
            if el in list(f.el):
                return f.el
            if f.para is not None and el in list(f.para):
                return f.para
        for p in self.body.iter():
            if el in list(p):
                return p
        return None

    # --- pictures ------------------------------------------------------------------------------------
    def includegraphics(self, name: str) -> None:
        self.star()
        opts = self.read_optional()
        self.read_optional()
        raw = self.arg_text()
        if name in ("epsfig", "psfig"):  # \epsfig{file=name,width=...}
            m = re.search(r"(?:file|figure)\s*=\s*([^,}]+)", raw or "")
            raw = m.group(1) if m else ""
        src = raw.strip().strip('"').replace("{", "").replace("}", "")
        if not src:
            return
        img = ET.Element("img", {"src": src, "alt": ""})
        o = detok(opts) if opts else ""
        wm = re.search(r"width\s*=\s*(\d*\.?\d*)\s*\\(?:textwidth|linewidth|columnwidth|hsize)", o)
        if wm and wm.group(1) != ".":  # "width=.\textwidth" (or 1.2.3) is no number: the picture keeps its size
            frac = float(wm.group(1) or 1)
            img.set("style", f"width: {min(100, round(frac * 100))}%")
        self.place_picture(img, src)
        self.add_inline(img)

    def place_picture(self, img: ET.Element, src: str) -> None:
        if self.base_dir is None or is_external(src):
            return
        name = src.replace("\\", "/")
        has_ext = bool(re.search(r"\.(png|jpe?g|gif|svg|pdf|eps|ps|bmp|tiff?|webp)$", name, re.IGNORECASE))
        exts = [""] if has_ext else [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".pdf", ".eps", ".ps",
                                     ".PNG", ".JPG", ".PDF", ".EPS", ""]
        found = None
        refused = False
        for prefix in self.graphics_paths:
            for ext in exts:
                p = safe_path(self.base_dir, prefix + name + ext, self.top)
                if p is None:
                    refused = True
                    continue
                if p.is_file():
                    found = p
                    break
            if found:
                break
        if found is None:
            (self.pictures_refused if refused else self.pictures_missing).append(src)
            return
        try:
            data = found.read_bytes()
        except OSError:
            self.pictures_missing.append(src)
            return
        rel = found.name
        img.set("src", rel)
        if sniff_image(data) is None and _postscript(found.name, data):
            svg = self.eps_figure(rel, data)
            if svg is not None:  # drawn as SVG, which books can hold
                found, data = found.with_name(found.stem + ".svg"), svg
        if sniff_image(data) is None:
            self.pictures_vector.append(rel)
            img.set("alt", rel)
            try:
                img.set("data-original", found.relative_to(self.base_dir).as_posix())
            except ValueError:
                img.set("data-original", rel)
            return
        existing = next((h for h, r in self.book.resources.items() if r.data == data), None)
        if existing is None:
            existing = self.book.add("images/" + re.sub(r"[^\w.-]", "_", found.name), data).href
        img.set("src", "../" + existing)

    def eps_figure(self, rel: str, data: bytes) -> bytes | None:
        """An EPS or PostScript figure as SVG (text as glyph outlines), or None when it can't be drawn."""
        from .. import ps
        notes: list[str] = []
        try:
            svg = ps.to_svg(data, notes=notes)
        except LimitError:
            raise
        except BookportError as e:
            self.lost.append(f"The PostScript figure {rel} couldn't be drawn: {e}")
            return None
        self.lost += [f"{rel}: {n}" for n in notes]
        return svg

    def inputlisting(self, name: str) -> None:
        opts = self.read_optional()
        lang = ""
        if name == "inputminted":
            lang = self.arg_text()
        fname = self.arg_text()
        if opts:
            lm = re.search(r"language\s*=\s*\{?([\w+#-]+)", detok(opts))
            lang = lm.group(1) if lm else lang
        if self.base_dir is None or not fname:
            return
        p = safe_path(self.base_dir, fname, self.top)
        if p is None:
            self.lost.append(f"A listing outside the document's folder was left out: {fname}")
            return
        try:
            data = p.read_bytes()[:4 * 1024 * 1024]
        except OSError:
            self.lost.append(f"A listing file was not found: {fname}")
            return
        self.code_block(markup.decode(data).rstrip("\n"), lang.lower(), "lstlisting")

    def code_block(self, code: str, lang: str, env: str) -> None:
        pre = self.add_block("pre")
        c = ET.SubElement(pre, "code", {"class": f"language-{lang}"} if lang else {})
        c.text = code

    def url(self, v: tuple[str, str]) -> None:
        cmd, raw = v
        url = re.sub(r"\\([#%&_~$^{}])", r"\1", "".join(raw.split()))
        if cmd in ("nolinkurl", "path"):
            c = ET.Element("code")
            c.text = raw
            self.add_inline(c)
            return
        a = ET.Element("a", {"href": url})
        self.add_inline(a)
        if cmd == "href":
            self.into(a, self.read_arg())
        else:
            a.text = url  # shown as the address it is: \url{x/\%7E} showed the backslash, and got more each round trip
            a.set("class", "uri")

    # --- math ----------------------------------------------------------------------------------------
    def dollar(self) -> None:
        t = self.stream.peek()
        if t is not None and t[0] == "$":
            self.stream.next()
            out, _orig, _rows = self.collect_math("$$")
            self.display(out, None, [], "")
            return
        out, _orig, _rows = self.collect_math("$")
        self.inline_math(out)

    def display_bracket(self, name: str) -> None:
        out, _orig, _rows = self.collect_math("]")
        self.display(out, None, [], "")

    def inline_paren(self, name: str) -> None:
        out, _o, _r = self.collect_math(")")
        self.inline_math(out)

    def ensuremath(self, name: str) -> None:
        toks = self.read_arg()
        self.inline_math(detok(toks))

    def inline_math(self, tex: str) -> None:
        tex = tex.strip()
        if not tex:
            return
        self.add_inline(mathml.from_tex(tex, False))

    def collect_math(self, close: str, env: str | None = None):
        """The formula up to its end: (TeX for MathML, TeX as written, rows). Each row is
        [numbered, labels, tag, has_content]."""
        out: list = []
        orig: list = []
        rows = [[True, [], None, False]]
        depth = 0
        nest = 0
        inner = 0
        count = 0
        st = self.stream
        while True:
            t = self.next()
            if t is None:
                break
            count += 1
            if count > MAX_MATH:
                continue
            k, v = t
            if k == "{":
                depth += 1
            elif k == "}":
                depth -= 1
            elif k == "$":
                if depth <= 0:
                    if close == "$":
                        break
                    if close == "$$":
                        nxt = st.peek()
                        if nxt is not None and nxt[0] == "$":
                            st.next()
                        break
            elif k == "end":
                st.unread(t)
                break
            elif k == "par" and close in ("$", ")"):
                break
            elif k == "cs":
                if v == "]" and close == "]" and depth <= 0:
                    break
                if v == ")" and close == ")" and depth <= 0:
                    break
                if v in ("begin", "end"):
                    arg = self.read_arg()
                    name = detok(arg).strip()
                    if v == "end" and env is not None and name == env and nest == 0:
                        break
                    if name == env:
                        nest += 1 if v == "begin" else -1
                    if name in _ROW_ENVS or name.rstrip("*") in _ROW_ENVS:
                        inner += 1 if v == "begin" else -1
                    piece = [t, OPEN, ("tx", name), CLOSE]
                    out.extend(piece)
                    orig.extend(piece)
                    rows[-1][3] = True
                    continue
                if v == "label":
                    toks = self.read_arg()
                    rows[-1][1].append(detok(toks).strip())
                    orig.extend([t, OPEN, *toks, CLOSE])
                    continue
                if v in ("nonumber", "notag"):
                    rows[-1][0] = False
                    orig.append(t)
                    continue
                if v == "tag":
                    star = self.star()
                    toks = self.read_arg()
                    rows[-1][2] = (detok(toks).strip(), star)
                    orig.extend([t, OPEN, *toks, CLOSE])
                    continue
                if v == "\\" and depth <= 0 and inner <= 0:
                    rows.append([True, [], None, False])
                    out.append(t)
                    orig.append(t)
                    continue
                if v == "ensuremath" or v in _MATH_DROP:
                    orig.append(t)
                    continue
                if v == "textsc":
                    out.append(("cs", "text"))
                    orig.append(t)
                    continue
                if v in ("intertext", "shortintertext"):
                    pass
            if k not in ("sp", "par"):
                rows[-1][3] = True
            if k == "par":
                t = SP
            out.append(t)
            orig.append(t)
        return detok(out), detok(orig), rows

    def math_env(self, env: str) -> None:
        kind = MATH_ENVS[env]
        if kind == -1:  # \begin{math}: inline
            out, _o, _r = self.collect_math("env", env)
            self.inline_math(out)
            return
        extra = ""
        if env.startswith(("alignat", "xalignat", "xxalignat", "IEEEeqnarray")):
            extra = "{" + self.arg_text() + "}"
        elif env == "empheq":
            self.read_optional()
            self.read_arg()
        out, orig, rows = self.collect_math("env", env)
        base = env.rstrip("*")
        if kind == 2 or base in ("align", "gather", "flalign", "alignat", "eqnarray", "IEEEeqnarray", "xalignat"):
            inner = {"gather": "gathered", "alignat": "alignedat", "xalignat": "alignedat"}.get(base, "aligned")
            body = out.strip()
            while body.endswith("\\\\"):
                body = body[:-2].rstrip()
            if base == "eqnarray":
                tex = "\\begin{aligned}" + body + "\\end{aligned}"
            else:
                tex = f"\\begin{{{inner}}}{extra}{body}\\end{{{inner}}}"
        elif base == "multline":
            tex = "\\begin{gathered}" + out.strip() + "\\end{gathered}"
        else:
            tex = out
        numbered = kind > 0
        latex = f"\\begin{{{env}}}{extra}{orig}\\end{{{env}}}"
        self.display(tex, rows, [] if numbered else None, latex, multi=kind == 2, single=base == "multline")

    def display(self, tex: str, rows, numbered, latex: str, multi: bool = False, single: bool = False) -> None:
        tex = tex.strip()
        if not tex:
            return
        div = self.add_block("div", {"class": "equation"})
        math = mathml.from_tex(tex, True)
        div.append(math)
        if latex:
            div.set("data-latex", latex)
        numbers = []
        labels: list[tuple[str, str]] = []
        if rows is not None:
            use = rows if multi else [rows[-1] if single else _merge_rows(rows)]
            for r in use:
                is_numbered, row_labels, tag, has = r
                if tag is not None:
                    shown = tag[0] if tag[1] else f"({tag[0]})"
                    num = tag[0]
                elif numbered is not None and is_numbered and (has or not multi):
                    self.counters["equation"] += 1
                    num = self._prefix() + str(self.counters["equation"])
                    shown = f"({num})"
                else:
                    num, shown = "", ""
                if shown:
                    numbers.append(shown)
                for lab in row_labels:
                    labels.append((lab, num))
        if numbers:
            _number_math(math, numbers)
        for lab, num in labels:
            if lab in self.labels:
                continue
            i = self.new_id(lab)
            if div.get("id") is None:
                div.set("id", i)
                div.set("data-label", lab)
                self.labels[lab] = (i, num, "equation", div)
            else:
                span = ET.Element("span", {"id": i})
                div.insert(0, span)
                self.labels[lab] = (i, num, "equation", span)

    # --- theorems and proofs -------------------------------------------------------------------------
    def theorem(self, env: str) -> None:
        th = self.theorems[env]
        opt = self.read_optional()
        number = ""
        if th["numbered"]:
            key = "thm:" + th["counter"]
            self.counters[key] += 1
            prefix = ""
            if th.get("within"):
                w = th["within"]
                prefix = (self._sec_number(w) if w in _SEC_ORDER and w != "part" else str(self.counters[w])) + "."
            number = prefix + str(self.counters[key])
        # data-theorem marks it for the writer, which then knows it from any other div with one class
        div = self.add_block("div", {"class": env.rstrip("*"), "data-theorem": "numbered" if number else "unnumbered"})
        f = self.push_frame(div, True, "theorem")
        p = ET.SubElement(div, "p")
        f.para = p
        head = ET.SubElement(p, "strong" if th["style"] != "remark" else "em")
        title = th["title"]
        title_el = ET.Element("span")
        self.into(title_el, title if isinstance(title, list) else self.tokenize(str(title)))
        head.text = markup.text_of(title_el).strip() + (f" {number}" if number else "")
        if opt:
            head.tail = " ("
            note = ET.SubElement(p, "span")
            self.into(note, opt)
            note.tail = "). "
        else:
            head.tail = ". "
        saved_target = self.label_target
        self.label_target = (div, number, th["title"] if isinstance(th["title"], str) else env)
        self.theorem_names[env] = markup.text_of(title_el).strip()
        saved = self.style
        self.style = ("i",) if th["style"] == "plain" else ()
        try:
            self.run(env=env)
        finally:
            self.style = saved
            self.pop_frame(f)
            self.label_target = saved_target

    def proof(self, env: str) -> None:
        opt = self.read_optional()
        div = self.add_block("div", {"class": "proof"})
        f = self.push_frame(div, True, "proof")
        p = ET.SubElement(div, "p")
        f.para = p
        head = ET.SubElement(p, "em")
        if opt:
            self.into(head, opt)
            markup.append_text(head, ".")
        else:
            head.text = "Proof."
        head.tail = " "
        saved = self.style
        try:
            self.run(env=env)
        finally:
            self.style = saved
            last = None
            for c in div:
                if c.tag == "p":
                    last = c
            if last is not None:
                _rstrip_end(last)
                markup.append_text(last, "\u00a0□")
            self.pop_frame(f)

    # --- title ---------------------------------------------------------------------------------------
    def set_title(self, name: str) -> None:
        self.read_optional()
        self.title = self.read_arg()

    def set_author(self, name: str) -> None:
        self.read_optional()
        self.authors = (self.authors or []) + ([("cs", "and")] if self.authors else []) + self.read_arg()

    def set_date(self, name: str) -> None:
        self.read_optional()
        self.date = self.read_arg()

    def abstract_cmd(self, name: str) -> None:
        toks = self.read_arg()
        if self.preamble:
            self.abstract_tokens = toks
            return
        el = self.add_block("div", {"class": "abstract"})
        ET.SubElement(el, "div", {"class": "abstract-title"}).text = "Abstract"
        self.into(el, toks, block=True)

    def maketitle(self, name: str) -> None:
        if self.title_done or self.preamble:
            return
        self.title_done = True
        header = self.add_block("header", {"id": "title-block-header"})
        self.ids.add("title-block-header")
        self._title_block(header)

    def _title_block(self, header: ET.Element) -> None:
        if self.title:
            h = ET.SubElement(header, "h1", {"class": "title"})
            self.into(h, self.title)
            self.book.metadata.title = _plain(h)
        if self.authors:
            names = []
            for part in _split_authors(self.authors):
                if not any(t[0] not in ("sp", "par") for t in part):
                    continue
                p = ET.SubElement(header, "p", {"class": "author"})
                self.into(p, part)
                if len(p) == 0 and not (p.text or "").strip():
                    header.remove(p)
                    continue
                first = _first_line(p)
                if first:
                    names.append(first)
            self.book.metadata.authors = names[:50]
        if self.date:
            p = ET.SubElement(header, "p", {"class": "date"})
            self.into(p, self.date)
            self.book.metadata.date = _plain(p)
        if self.abstract_tokens:
            el = ET.SubElement(header, "div", {"class": "abstract"})
            ET.SubElement(el, "div", {"class": "abstract-title"}).text = "Abstract"
            self.into(el, self.abstract_tokens, block=True)

    # --- citations -----------------------------------------------------------------------------------
    def cite(self, name: str) -> None:
        self.star()
        first = self.read_optional()
        second = self.read_optional()
        keys = [k.strip() for k in self.arg_text().split(",") if k.strip()]
        if name.endswith("cites"):  # biblatex's \cites[pre][post]{a}[pre][post]{b}...: every key group
            while True:
                t = self.stream.peek()
                if t is None or not (t[0] == "{" or t == LBRACK):
                    break
                self.read_optional()
                self.read_optional()
                keys += [k.strip() for k in self.arg_text().split(",") if k.strip()]
        mode = CITES[name]
        pre, post = "", ""
        if second is not None:
            pre, post = self.plain(first or []), self.plain(second)
        elif first is not None:
            post = self.plain(first)
        span = ET.Element("span", {"class": "citation", "data-cites": " ".join(keys)})
        if mode != "normal":
            span.set("data-cite-mode", mode)
        if pre:
            span.set("data-cite-prefix", pre)
        if post:
            span.set("data-cite-suffix", post)
        span.text = "[" + ", ".join(keys) + "]" if mode != "nocite" else ""
        self.add_inline(span)

    def bibliography(self, name: str) -> None:
        names = [n.strip() for n in self.arg_text().split(",") if n.strip()]
        self.bib_names += names
        found = self._find_bibs(names)
        if found:
            self.bib_files += found
            self._bib_place()
            return
        bbl = self._bbl()
        if bbl:
            self.stream.push(bbl)
            return
        if names:
            self.lost.append("The bibliography file wasn't found, so citations show their keys: "
                             + ", ".join(n if n.endswith(".bib") else n + ".bib" for n in names))

    def _find_bibs(self, names: list[str]) -> list[Path]:
        out = []
        if self.base_dir is None:
            return out
        for n in names:
            cand = n if n.lower().endswith(".bib") else n + ".bib"
            p = safe_path(self.base_dir, cand, self.top)
            if p is None:
                self.lost.append(f"A bibliography outside the document's folder was left out: {cand}")
                continue
            if p.is_file() and p not in out:
                out.append(p)
        return out

    def _bbl(self) -> list:
        if self.base_dir is None or self.main is None or self.bibitems:
            return []
        p = safe_path(self.base_dir, self.main.stem + ".bbl", self.top)
        if p is None or not p.is_file():
            return []
        return self.include_tokens(self.main.stem + ".bbl", 0, quiet=True)

    def addbibresource(self, name: str) -> None:
        self.read_optional()
        n = self.arg_text()
        found = self._find_bibs([n])
        self.bib_files += [f for f in found if f not in self.bib_files]
        if not found and n:
            self.bib_names.append(n)

    def printbibliography(self, name: str) -> None:
        self.read_optional()
        if self.bib_files:
            self._bib_place()
            return
        bbl = self._bbl()
        if bbl:
            self.stream.push(bbl)
        elif self.bib_names:
            self.lost.append("The bibliography file wasn't found, so citations show their keys: "
                             + ", ".join(self.bib_names))

    def _bib_place(self) -> None:
        if self.bib_placed:
            return
        self.bib_placed = True
        h = self.add_block("h1", {"class": "unnumbered"})
        h.text = "Bibliography" if self.chapters else "References"
        self.headings.append((h, 0 if self.chapters else 1))
        self.add_block("div", {"class": "references", "id": "refs", "data-bib": "here"})

    def thebibliography(self, env: str) -> None:
        self.read_arg()
        h = self.add_block("h1", {"class": "unnumbered"})
        h.text = "Bibliography" if self.chapters else "References"
        self.headings.append((h, 0 if self.chapters else 1))
        self.bib_placed = True
        div = self.add_block("div", {"class": "references", "id": "refs"})
        self.ids.add("refs")
        f = self.push_frame(div, False, "bib")
        self.bib_div = div
        saved = self.style
        try:
            self.run(env=env)
        finally:
            self.style = saved
            self.pop_frame(f)
            if self.bib_item is not None:
                self.pop_frame(self.bib_item)
                self.bib_item = None

    def bibitem(self, name: str) -> None:
        opt = self.read_optional()
        key = self.arg_text()
        div = self.bib_div
        if div is None:
            return
        if self.bib_item is not None:
            self.pop_frame(self.bib_item)
        self.bibitems += 1
        entry = ET.SubElement(div, "div", {"class": "csl-entry", "id": self.new_id(bib.ref_id(key)), "data-key": key})
        e = bib.Entry(key, element=entry)
        numeric = self._cite_style() == "numeric"
        if opt is not None:
            label = " ".join(self.plain(opt).split())
            m = re.match(r"^([^()]*)\(([^()]*)\)(.*)$", label, re.DOTALL)  # [Author(Year)...]: no backtracking over "("
            if m and not numeric:
                e.short, e.year = m.group(1).strip(), m.group(2).strip()
                entry.set("data-short", e.short)
                entry.set("data-year", e.year)
            elif m:
                e.label = str(self.bibitems)
            else:
                e.label = label
        else:
            e.label = str(self.bibitems)
        if e.label:
            entry.text = f"[{e.label}] "
        self.ready.setdefault(key, e)
        self.bib_item = self.push_frame(entry, False, "bibitem")

    def _cite_style(self) -> str:
        if self.bib_style:
            return self.bib_style
        bst = self.bst
        if self.natbib is not None:
            style = "numeric" if re.search(r"\bnumbers\b", self.natbib) or bst.lower() in NUMERIC_BST - {"unsrtnat"} \
                else "author-year"
        elif self.biblatex is not None:
            style = "author-year" if re.search(r"style\s*=\s*(authoryear|apa|chicago|ext-authoryear)", self.biblatex) \
                else "numeric"
        elif bst:
            low = bst.lower()
            style = "numeric" if low in {s.lower() for s in NUMERIC_BST} or "num" in low else "author-year"
        else:
            style = "numeric"
        self.bib_style = style
        return style

    # --- finishing -----------------------------------------------------------------------------------
    def finish(self) -> None:
        while len(self.frames) > 1:
            self.pop_frame(self.frames[-1])
        if self.frames:
            self.close_para(self.frames[0])
        if not self.title_done and self.title:
            header = ET.Element("header", {"id": "title-block-header"})
            self.frames = [_Frame(header, True, "title")]
            self._title_block(header)
            self.body.insert(0, header)
        # headings: the top level used becomes h1
        levels = [lv for _, lv in self.headings]
        shift = 1 - min(levels) if levels and min(levels) < 1 else 0
        for h, lv in self.headings:
            h.tag = f"h{max(1, min(6, lv + shift))}"
        _tidy(self.body)


_FALLBACK_THEOREMS = {"theorem": "Theorem", "lemma": "Lemma", "corollary": "Corollary", "proposition": "Proposition",
                      "definition": "Definition", "remark": "Remark", "example": "Example", "conjecture": "Conjecture",
                      "claim": "Claim", "assumption": "Assumption", "observation": "Observation", "fact": "Fact",
                      "problem": "Problem", "exercise": "Exercise", "question": "Question", 
                      "hypothesis": "Hypothesis", "property": "Property", "notation": "Notation"}
_PLAIN_THEOREMS = {"theorem", "lemma", "corollary", "proposition", "conjecture", "claim", "fact", "hypothesis",
                   "property", "observation", "assumption"}


def _brace_end(text: str, p: int) -> int:
    """The index of the brace closing the one at ``p`` (or the end of the text)."""
    depth = 0
    n = len(text)
    i = p
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _flat_text(toks: list) -> str:
    out = []
    for k, v in toks:
        if k == "tx":
            out.append(v)
        elif k in ("sp", "par"):
            out.append(" ")
        elif k == "cs" and v in SYMBOLS:
            out.append(SYMBOLS[v])
        elif k == "verb":
            out.append(v)
    return "".join(out)


def _colspec(spec: str, custom: dict[str, str] | None = None, depth: int = 0) -> list[str]:
    out: list[str] = []
    i, n = 0, len(spec)
    names = {"l": "left", "c": "center", "r": "right", "X": "", "S": "center", "L": "left", "C": "center",
             "R": "right", "J": "", "Q": "", "Y": "center", "Z": "center"}
    while i < n:
        c = spec[i]
        if c == "{":
            i = _brace_end(spec, i) + 1
            continue
        if c in "pmbw" and i + 1 < n and spec[i + 1] == "{":
            out.append("left" if c == "p" else "")
            i = _brace_end(spec, i + 1) + 1
            if c == "w" and i < n and spec[i] == "{":
                i = _brace_end(spec, i) + 1
            continue
        if c in "@!<>" and i + 1 < n and spec[i + 1] == "{":
            i = _brace_end(spec, i + 1) + 1
            continue
        if c == "*" and i + 1 < n and spec[i + 1] == "{":
            e1 = _brace_end(spec, i + 1)
            count = spec[i + 2:e1].strip()
            if e1 + 1 < n and spec[e1 + 1] == "{":
                e2 = _brace_end(spec, e1 + 1)
                inner = spec[e1 + 2:e2]
                try:
                    k = max(0, min(int(count), 100))
                except ValueError:
                    k = 1
                if depth < 4:
                    part = _colspec(inner, custom, depth + 1)
                    # repeats inside repeats multiply: bounded, *{100}{*{100}{...}} asked for 10^8 columns
                    out += part * min(k, (MAX_COLUMNS - len(out)) // max(1, len(part)) + 1)
                    if len(out) >= MAX_COLUMNS:
                        return out[:MAX_COLUMNS]
                i = e2 + 1
                continue
            i = e1 + 1
            continue
        if c == "\\":
            m = re.compile(r"\\[A-Za-z@]+|\\.").match(spec, i)
            i = m.end() if m else i + 1
            continue
        if custom and c in custom and depth < 4:
            out += _colspec(custom[c], custom, depth + 1) or [""]
            i += 1
            continue
        if c in names:
            out.append(names[c])
        elif c.isalpha():
            out.append("")
        i += 1
    return out


def _has_content(el: ET.Element | None) -> bool:
    if el is None:
        return False
    return bool((el.text or "").strip()) or len(el) > 0 or el.get("colspan") is not None


def _only_pictures(p: ET.Element) -> bool:
    if (p.text or "").strip():
        return False
    if not len(p):
        return False
    for c in p:
        if c.tag not in ("img", "br") and not (c.tag == "span" and not markup.text_of(c).strip()):
            return False
        if (c.tail or "").strip():
            return False
    return any(c.tag == "img" for c in p)


def _inside(el: ET.Element | None, root: ET.Element) -> bool:
    if el is None:
        return False
    return any(x is el for x in root.iter())


def _merge_rows(rows: list) -> list:
    numbered = all(r[0] for r in rows)
    labels = [lab for r in rows for lab in r[1]]
    tag = next((r[2] for r in rows if r[2] is not None), None)
    return [numbered, labels, tag, True]


def _number_math(math: ET.Element, numbers: list[str]) -> None:
    """Equation numbers shown at the end of the formula (inside the MathML, so text counts stay the text)."""
    sem = math.find("semantics")
    host = sem if sem is not None else math
    pres = host[0] if len(host) and host[0].tag != "annotation" else None
    if pres is None:
        pres = ET.Element("mrow")
        host.insert(0, pres)
    elif pres.tag != "mrow":
        row = ET.Element("mrow")
        host.remove(pres)
        row.append(pres)
        host.insert(0, row)
        pres = row
    ET.SubElement(pres, "mspace", {"width": "2em"})
    ET.SubElement(pres, "mtext", {"class": "eqno"}).text = " ".join(numbers)


def _split_authors(toks: list) -> list[list]:
    parts: list[list] = [[]]
    depth = 0
    for t in toks:
        if t[0] == "{":
            depth += 1
        elif t[0] == "}":
            depth -= 1
        if depth == 0 and t[0] == "cs" and t[1] in ("and", "And", "AND"):
            parts.append([])
            continue
        parts[-1].append(t)
    return parts


def _visible(el: ET.Element, stop_at_br: bool = False) -> str:
    """An element's text as a reader sees it: no note calls, no TeX annotations, line breaks as spaces
    (or the end, with ``stop_at_br``)."""
    out: list[str] = [el.text or ""]
    stack: list[tuple[ET.Element, bool]] = [(c, False) for c in reversed(el)]
    while stack:
        c, leaving = stack.pop()
        if leaving:
            out.append(c.tail or "")
            continue
        if c.tag == "br":
            if stop_at_br:
                break
            out.append(" ")
        elif not (c.tag == "annotation" or c.tag == "a" and "noteref" in c.get("class", "")):
            out.append(c.text or "")
            stack.append((c, True))
            stack.extend((k, False) for k in reversed(c))
            continue
        out.append(c.tail or "")
    return " ".join("".join(out).split())


def _plain(el: ET.Element) -> str:
    """An element's text without its note calls."""
    return _visible(el)


def _first_line(p: ET.Element) -> str:
    return _visible(p, stop_at_br=True).strip(" ,")


_WS = re.compile(r"[ \t\r\n]+")
_EDGES = markup.BLOCK | {"figure", "figcaption", "caption", "aside", "header", "br"}


def _tidy(root: ET.Element) -> None:
    """Runs of white space as one space (not in code or math), none at the edges of blocks or line
    breaks, none doubled across element boundaries; no empty paragraphs."""
    state = {"space": True, "last": None}

    def put(el: ET.Element, attr: str) -> None:
        s = getattr(el, attr)
        if not s:
            return
        s = _WS.sub(" ", s)
        if state["space"]:
            s = s.lstrip(" ")
        setattr(el, attr, s or None)
        if s:
            state["space"] = s.endswith(" ")
            state["last"] = (el, attr)

    def edge() -> None:
        last = state["last"]
        if last is not None:
            s = getattr(last[0], last[1])
            if s:
                setattr(last[0], last[1], s.rstrip(" ") or None)
        state["last"] = None
        state["space"] = True

    stack: list[tuple[ET.Element, bool]] = [(root, False)]
    while stack:
        el, leaving = stack.pop()
        tag = el.tag
        block = tag in _EDGES or (tag == "math" and el.get("display") == "block")
        if not leaving:
            if block:
                edge()
            if tag in ("pre", "math", "code"):
                if not block:
                    state["space"], state["last"] = False, None
                stack.append((el, True))
                continue
            put(el, "text")
            stack.append((el, True))
            stack.extend((c, False) for c in reversed(el) if isinstance(c.tag, str))
        else:
            if block:
                edge()
            if el is not root:
                put(el, "tail")
    edge()
    for parent in root.iter():
        for c in [c for c in parent if c.tag == "p" and not len(c) and not (c.text or "").strip()]:
            if not (c.tail and c.tail.strip()):
                parent.remove(c)


# --- reading -----------------------------------------------------------------------------------------

def read(path) -> Book:
    p = Path(path)
    text = markup.decode(p.read_bytes())
    return book_from_latex(text, p.stem.replace("_", " "), base_dir=p.parent, main=p)


def book_from_latex(text: str, title: str = "", base_dir: Path | None = None, top: Path | None = None,
                    main: Path | None = None) -> Book:
    """A book from LaTeX source; files it names (includes, pictures, .bib) come from ``base_dir`` and one
    level up (``top``, when given, is the outermost folder allowed instead)."""
    text = text.lstrip("\ufeff")
    book = Book(Metadata(title=""))
    book.source_format = "tex"
    r = _Reader(book, base_dir.resolve() if base_dir is not None else None,
                top if top is not None else (base_dir.resolve().parent if base_dir is not None else None), main)
    if main is not None:
        try:
            r.include_stack.append(main.resolve())
        except OSError:
            pass
    toks = r.tokenize(text)
    if "\\begin{document}" not in text:
        r.start_document()
    r.stream.push(toks)
    r.run()
    r.finish()
    if not book.metadata.title:
        book.metadata.title = title
    book.metadata.language = r.lang or "en"
    book.metadata.subjects = r.keywords
    ab = r.body.find(".//div[@class='abstract']")
    if ab is not None:
        book.metadata.description = " ".join(
            "".join(markup.text_of(c) for c in ab if c.get("class") != "abstract-title").split())[:4000]
    _split(book, r)
    _resolve_refs(book, r)
    entries: dict[str, bib.Entry] = {}
    if r.bib_files:
        entries = bib.load(r.bib_files)
    for k, e in r.ready.items():
        entries[k] = e
    if entries:
        bib.apply(book, entries, style=r._cite_style(), title=False)
    _lost(book, r)
    book.ensure_toc()
    return book


def _split(book: Book, r: _Reader) -> None:
    """One document, or one per top-level heading when the text is long."""
    body = r.body
    kids = list(body)
    chunks: list[list[ET.Element]] = [[]]
    size = sum(len(markup.text_of(c)) for c in kids)
    if size > 120_000:
        top = min((int(h.tag[1]) for h, _ in r.headings), default=1)
        run = 0
        for c in kids:
            if c.tag == f"h{top}" and run > 20_000 and chunks[-1]:
                chunks.append([])
                run = 0
            chunks[-1].append(c)
            run += len(markup.text_of(c))
    else:
        chunks = [kids]
    title = book.metadata.title or "Untitled"
    for n, chunk in enumerate(chunks, 1):
        root, new_body = markup.new_document(title, r.lang or "en")
        for c in chunk:
            new_body.append(c)
        # the notes called from this part go at its end
        for a in list(new_body.iter("a")):
            if "noteref" in a.get("class", "") and a.get("href", "").startswith("#"):
                aside = r.notes.pop(a.get("href")[1:], None)
                if aside is not None:
                    aside.attrib.pop("data-shown", None)
                    _tidy(aside)
                    new_body.append(aside)
                    # notes inside notes
                    for a2 in list(aside.iter("a")):
                        if "noteref" in a2.get("class", "") and a2.get("href", "")[1:] in r.notes:
                            inner = r.notes.pop(a2.get("href")[1:])
                            inner.attrib.pop("data-shown", None)
                            _tidy(inner)
                            new_body.append(inner)
        book.add_document(f"text/part{n:04d}.xhtml", root)
    for nid, aside in r.notes.items():  # notes never called (\footnotetext without a mark)
        del nid
        if len(aside) and book.spine:
            last = book.tree(book.spine[-1]).find("body")
            aside.attrib.pop("data-shown", None)
            last.append(aside)
    r.notes.clear()
    if not book.spine:
        root, b = markup.new_document(title)
        ET.SubElement(b, "p")
        book.add_document("text/part0001.xhtml", root)


def _resolve_refs(book: Book, r: _Reader) -> None:
    where: dict[str, str] = {}
    for href, _ in book.documents():
        for el in book.tree(href).iter():
            i = el.get("id")
            if i:
                where.setdefault(i, href)
    doc_of: dict[int, str] = {}
    for href, _ in book.documents():
        for el in book.tree(href).iter("a"):
            doc_of[id(el)] = href
    names = r.theorem_names
    for a, keys, kind in r.refs:
        found = [r.labels.get(k) for k in keys]
        here = doc_of.get(id(a), "")
        first = next((f for f in found if f is not None), None)
        if first is None:
            a.attrib.pop("href", None)
            a.tag = "span"
            if kind in ("hyperref", "nameref"):
                continue
            a.text = "??"
            continue
        target = where.get(first[0], here)
        a.set("href", relative(here, f"{target}#{first[0]}") if target != here else f"#{first[0]}")
        if kind == "hyperref":
            continue
        nums = [f[1] if f is not None else "??" for f in found]
        if kind == "nameref":
            el = first[3]
            text = _plain(el)
            num = el.find("span[@class='header-section-number']")
            if num is not None and num.text:
                text = text[len(num.text):].strip()
            a.text = text
            for c in list(a):
                a.remove(c)
            continue
        if kind == "eqref":
            a.text = ", ".join(f"({n})" for n in nums)
        elif kind in ("autoref", "cref", "Cref"):
            k = first[2]
            pair = _REF_NAMES.get(k) or ((names.get(k) or k.capitalize(), (names.get(k) or k).lower())
                                         if k else ("", ""))
            word = pair[0] if kind != "cref" else pair[1]
            if len(nums) > 1 and word:
                word = word + ("s" if not word.endswith(".") else "")
            if k == "equation" and kind != "autoref":
                shown = ", ".join(f"({n})" for n in nums)
            else:
                shown = ", ".join(nums) if len(nums) < 3 else ", ".join(nums[:-1]) + " and " + nums[-1]
            a.text = f"{word}\u00a0{shown}" if word else shown
        else:
            a.text = ", ".join(nums) if any(nums) else "??"
    for href, _ in book.documents():
        book.set_tree(href, book.tree(href))


def _postscript(name: str, data: bytes) -> bool:
    """An EPS or PostScript figure (by its name, or a DOS EPS header or %! at its start)."""
    return (name.lower().endswith((".eps", ".ps", ".epsi", ".epsf")) or data[:4] == bytes((0xC5, 0xD0, 0xD3, 0xC6))
            or data.lstrip()[:2] == b"%!")


def _lost(book: Book, r: _Reader) -> None:
    lost = list(dict.fromkeys(r.lost))
    if r.unknown_cmds:
        names = sorted(r.unknown_cmds, key=lambda k: -r.unknown_cmds[k])
        shown = ", ".join("\\" + n for n in names[:30]) + (f" and {len(names) - 30} more" if len(names) > 30 else "")
        lost.append(f"LaTeX commands bookport doesn't know were left out, their text kept: {shown}")
    if r.unknown_envs:
        names = sorted(r.unknown_envs)
        lost.append("LaTeX environments bookport doesn't know, kept as plain text: " + ", ".join(names[:30]))
    if r.drawings:
        lost.append(f"Drawings made with TikZ or the picture environment can't be converted and were left out: "
                    f"{r.drawings}")
    if r.pictures_vector:
        lost.append("Pictures in PDF form (or PostScript that couldn't be drawn) can't go into a book; their places "
                    "and captions are kept: "
                    + ", ".join(dict.fromkeys(r.pictures_vector)))
    if r.pictures_missing:
        lost.append("Pictures not found: " + ", ".join(dict.fromkeys(r.pictures_missing)))
    if r.pictures_refused:
        lost.append("Pictures outside the document's folder were left out: " + ", ".join(dict.fromkeys(r.pictures_refused)))
    book.lost.extend(lost)


# --- writing -----------------------------------------------------------------------------------------

_ESCAPES = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&",
            "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "<": r"\textless{}",
            ">": r"\textgreater{}", "|": r"\textbar{}", "[": "{[}", "]": "{]}", "\u00a0": "~", "\u00ad": r"\-",
            "\u200b": "", "\u2003": r"\quad{}", "\u2009": r"\,", "\u2002": r"\enspace{}", "…": r"\ldots{}",
            "™": r"\texttrademark{}", "€": r"\texteuro{}", "−": "-", "→": r"\(\rightarrow\)",
            "←": r"\(\leftarrow\)", "⇒": r"\(\Rightarrow\)", "↔": r"\(\leftrightarrow\)", "≤": r"\(\leq\)",
            "≥": r"\(\geq\)", "≠": r"\(\neq\)", "≈": r"\(\approx\)", "±": r"\(\pm\)", "×": r"\(\times\)",
            "÷": r"\(\div\)", "∞": r"\(\infty\)", "∈": r"\(\in\)", "∑": r"\(\sum\)", "√": r"\(\surd\)",
            "∂": r"\(\partial\)", "∇": r"\(\nabla\)", "∼": r"\(\sim\)", "≡": r"\(\equiv\)", "□": r"\(\square\)",
            "∎": r"\(\blacksquare\)", "✓": r"\(\checkmark\)", "•": r"\textbullet{}", "′": r"\('\)",
            "°": r"\textdegree{}", "µ": r"\textmu{}", "ℓ": r"\(\ell\)", "⟨": r"\(\langle\)", "⟩": r"\(\rangle\)",
            "∗": r"\(\ast\)", "·": r"\textperiodcentered{}", "∅": r"\(\emptyset\)", "∀": r"\(\forall\)",
            "∃": r"\(\exists\)", "∩": r"\(\cap\)", "∪": r"\(\cup\)", "⊂": r"\(\subset\)", "⊆": r"\(\subseteq\)",
            "↑": r"\(\uparrow\)", "↓": r"\(\downarrow\)", "∝": r"\(\propto\)", "⋅": r"\(\cdot\)",
            "\u2028": r"\newline{}", "\u2011": "-", "\u2010": "-", "\ufeff": "", "\u200e": "", "\u200f": ""}
_GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "varepsilon", "ϵ": "epsilon", "ζ": "zeta",
          "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi",
          "π": "pi", "ρ": "rho", "σ": "sigma", "ς": "varsigma", "τ": "tau", "υ": "upsilon", "φ": "varphi",
          "ϕ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega", "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta",
          "Λ": "Lambda", "Ξ": "Xi", "Π": "Pi", "Σ": "Sigma", "Υ": "Upsilon", "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega"}
for _g, _n in _GREEK.items():
    _ESCAPES[_g] = f"\\(\\{_n}\\)"
# symbols outside the text fonts stay as the characters they are, declared for pdflatex in the preamble
_UNICODE = {c: "\\ensuremath{" + v[2:-2] + "}" for c, v in _ESCAPES.items() if v.startswith("\\(")}
for _c in _UNICODE:
    del _ESCAPES[_c]
# a hyphen or quote before another gets {} so TeX makes no dash or quote ligature of them ("a---b" was
# written a-{}--b and read back with an en dash); a backtick is no quote mark here
_ESC_RE = re.compile("[" + re.escape("".join(_ESCAPES)) + "]|-(?=-)|'(?=')|`")
_REF_KINDS = {"ref": "ref", "eqref": "eqref", "autoref": "autoref", "cref": "cref", "Cref": "Cref",
              "nameref": "nameref"}


def escape(s: str) -> str:
    """Text for LaTeX: the special characters, and the ligatures TeX would make (-- `` '')."""
    return _ESC_RE.sub(lambda m: _ESCAPES.get(m.group(0)) or ("\\textasciigrave{}" if m.group(0) == "`" else
                                                              m.group(0) + "{}"), s)


def _url(s: str) -> str:
    """A URL inside \\href{} or \\url{}: hyperref takes it as is, except these."""
    return re.sub(r"([#%\\{}])", r"\\\1", s).replace("\n", "")


class _LatexWriter(TextWriter):
    def __init__(self, book: Book):
        super().__init__(book)
        self.uses: set[str] = set()
        self.in_arg = 0  # inside a command's argument (footnote, caption, cell): no verbatim, no floats
        self.list_depth = 0
        self.quote_depth = 0
        self.labels: dict[str, str] = {}  # "doc#id" -> the label the source used
        self.header: dict[str, str] = {}
        self.numbered = False
        self.style = "author-year"
        self.entries: list[tuple[str, str, str, str]] = []  # key, text, short, year
        self.theorems: dict[str, str] = {}  # environment -> its name ("Theorem")
        self.table_anchor: str | None = None
        self.ref_style: str | None = None
        self.in_cell = 0
        self.unicode_used: list[str] = []
        self.notes_written: set[str] = set()
        self.call_counts: dict[str, int] | None = None
        self.originals: set[str] = set()  # pictures named as the LaTeX source had them (not in the book)
        self.skip: set[int] = set()
        for href, _ in book.documents():
            for el in book.tree(href).iter():
                if el.get("data-label") and el.get("id"):
                    self.labels[f"{href}#{el.get('id')}"] = el.get("data-label")
                if el.tag == "span" and el.get("class") == "header-section-number":
                    self.numbered = True
                kids = list(el)
                for a, b in itertools.pairwise(kids):
                    # thebibliography prints its own heading
                    if a.tag in ("h1", "h2", "h3") and b.tag == "div" and "references" in b.get("class", "").split():
                        self.skip.add(id(a))

    # --- keys ---------------------------------------------------------------------------------------
    def anchor_key(self, el: ET.Element) -> str | None:
        i = el.get("id")
        if i and el.get("data-label"):  # a label the source had: kept even when nothing refers to it
            return el.get("data-label")
        if i and f"{self.href}#{i}" in self.targets:
            return self.labels.get(f"{self.href}#{i}") or super().anchor_key(el)
        return None

    def link_key(self, link: str) -> str:
        t = resolve(self.href, link)
        frag = link.split("#", 1)[1] if "#" in link else ""
        return self.labels.get(f"{t}#{frag}") or super().link_key(link)

    # --- document -----------------------------------------------------------------------------------
    def write_book(self) -> str:
        body = super().write_book()
        everything = body + "".join(self.header.values()) + self.book.metadata.title + "".join(self.book.metadata.authors)
        self.unicode_used = sorted({c for c in everything if c in _UNICODE})
        return self.document_head() + body + self.document_tail()

    def preamble(self) -> str:
        return ""

    def postamble(self) -> str:
        if not self.entries:
            return ""
        widest = max((len(str(i + 1)) for i in range(len(self.entries))), default=1)
        lines = [f"\\begin{{thebibliography}}{{{'9' * widest}}}"]
        for key, text, short, year in self.entries:
            label = ""
            if self.style == "author-year" and short:
                label = "[{" + escape(short) + "(" + escape(year) + ")}]"
            lines.append(f"\\bibitem{label}{{{key}}}\n{text}")
        lines.append("\\end{thebibliography}")
        return "\n\n".join(lines)

    def document_head(self) -> str:
        md = self.book.metadata
        lines = ["\\documentclass{article}", "\\usepackage[T1]{fontenc}", "\\usepackage[utf8]{inputenc}",
                 "\\usepackage{lmodern}", "\\usepackage{textcomp}", "\\usepackage{amsmath,amssymb}"]
        lines += [f"\\DeclareUnicodeCharacter{{{ord(c):04X}}}{{{_UNICODE[c]}}}" for c in self.unicode_used]
        if "graphics" in self.uses:
            lines.append("\\usepackage{graphicx}")
            lines += ["\\makeatletter",
                      "\\def\\bpmaxwidth{\\ifdim\\Gin@nat@width>\\linewidth\\linewidth\\else\\Gin@nat@width\\fi}",
                      "\\def\\bpmaxheight{\\ifdim\\Gin@nat@height>0.8\\textheight0.8\\textheight\\else\\Gin@nat@height\\fi}",
                      "\\makeatother"]
        if "table" in self.uses:
            lines += ["\\usepackage{longtable,booktabs,array}"]
        if "multirow" in self.uses:
            lines.append("\\usepackage{multirow}")
        if "ulem" in self.uses:
            lines.append("\\usepackage[normalem]{ulem}")
        if "cite" in self.uses or self.entries:
            lines.append("\\usepackage[numbers]{natbib}" if self.style == "numeric" else "\\usepackage{natbib}")
        if "theorem" in self.uses:
            lines.append("\\usepackage{amsthm}")
            for env, name in self.theorems.items():
                star = "*" if env.endswith("*") else ""  # \newtheorem*{remark*}{Remark}: like align*, a name of its own
                lines.append(f"\\newtheorem{star}{{{env}}}{{{escape(name)}}}")
        lines += ["\\usepackage[hidelinks]{hyperref}"]
        if not self.numbered:
            lines.append("\\setcounter{secnumdepth}{-2}")
        lines.append("\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{6pt plus 2pt minus 1pt}")
        title = self.header.get("title") or ""
        if not title and md.title and self.book.source_format != "tex" and md.title != self.first_heading():
            title = escape(md.title)  # a title the text doesn't show itself (LaTeX sources have their own block)
        if title:
            lines.append(f"\\title{{{title}}}")
        authors = self.header.get("author") or " \\and ".join(escape(a) for a in md.authors)
        lines.append(f"\\author{{{authors}}}")
        date_ = self.header.get("date") or escape(md.date or "")
        lines.append(f"\\date{{{date_}}}")
        lines += ["", "\\begin{document}", ""]
        if title:
            lines.append("\\maketitle")
            lines.append("")
        if self.header.get("abstract"):
            lines += ["\\begin{abstract}", self.header["abstract"], "\\end{abstract}", ""]
        return "\n".join(lines) + "\n"

    def first_heading(self) -> str:
        for href, _ in self.book.documents():
            for el in self.book.tree(href).iter():
                if el.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    return " ".join(markup.text_of(el).split())
        return ""

    def document_tail(self) -> str:
        return "\n\\end{document}\n"

    # --- blocks -------------------------------------------------------------------------------------
    def block(self, el: ET.Element) -> str:
        tag = el.tag
        cls = el.get("class", "").split()
        if id(el) in self.skip:
            return ""
        if tag == "header" and el.get("id") == "title-block-header":
            return self.title_block(el)
        if tag == "div" and "equation" in cls:
            return self.equation(el)
        if tag == "div" and ("abstract" in cls):
            inner = self.blocks(_without(el, "abstract-title"))
            if not self.header.get("abstract") and self.href == next(iter(self.book.spine), ""):
                self.header["abstract"] = inner
                return ""
            return self.env("abstract", inner)
        if tag == "div" and "proof" in cls:
            self.uses.add("theorem")
            return self.env("proof", self.blocks(_drop_lead(el, "em", "□")))
        if tag == "div" and "references" in cls:
            return self.references(el)
        if tag == "div" and "table-float" in cls:
            return self.blocks(el)
        # a theorem only when the LaTeX reader made it one, or its class names one: a book's
        # <div class="calibre1"><p><em>Hope</em> is...</p></div> is a paragraph, not \newtheorem{calibre}
        if tag == "div" and len(cls) == 1 and (el.get("data-theorem") or cls[0] in _FALLBACK_THEOREMS):
            th = _theorem_head(el)
            if th is not None:
                return self.theorem(el, cls[0], *th)
        if tag == "figure":
            imgs = el.findall(".//img")
            if len(imgs) != 1 or el.find("figure") is not None or el.find(".//table") is not None:
                return self.multi_figure(el)
        if tag == "table":
            self.table_anchor = self.anchor_key(el)
        return super().block(el)

    def theorem(self, el: ET.Element, cls: str, name: str, note: ET.Element | None, body: ET.Element,
                numbered: bool = True) -> str:
        env = re.sub(r"[^A-Za-z]", "", cls) or "theorem"
        if el.get("data-theorem") == "unnumbered" or not numbered:
            env += "*"  # \newtheorem*: "Remark." reads back as "Remark.", not "Remark 1."
        self.uses.add("theorem")
        if env not in self.theorems:
            self.theorems[env] = name
        arg = ""
        if note is not None:
            arg = "[" + " ".join(self.inlines(note).split()) + "]"
        anchor = self.anchor_key(el)
        inner = self.blocks(body)
        return self.env(env, (f"\\label{{{anchor}}}\n" if anchor else "") + inner, arg)

    def title_block(self, el: ET.Element) -> str:
        h = el.find("h1")
        if h is not None:
            self.header["title"] = _thanks(" ".join(self.inlines(h).split()))
        authors = [_thanks(self.inlines(p).strip()) for p in el.findall("p") if "author" in p.get("class", "")]
        if authors:
            self.header["author"] = " \\and ".join(a.replace("\\\\\n", "\\\\ ") for a in authors)
        d = next((p for p in el.findall("p") if "date" in p.get("class", "")), None)
        if d is not None:
            self.header["date"] = _thanks(self.inlines(d).strip())
        ab = el.find("div[@class='abstract']")
        if ab is not None:
            self.header["abstract"] = self.blocks(_without(ab, "abstract-title"))
        return ""

    def env(self, name: str, inner: str, arg: str = "") -> str:
        return f"\\begin{{{name}}}{arg}\n{inner.strip()}\n\\end{{{name}}}"

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        text = re.sub(r"^(?:\\\\\s*)+|(?:\\\\\s*)+$", "", text.strip())
        if not text:
            return self.anchor_line(anchor) if anchor else ""
        return (self.anchor_line(anchor) + text) if anchor else text

    def anchor_line(self, key: str | None) -> str:
        return f"\\phantomsection\\label{{{key}}}" if key else ""

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        cmd = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph", 5: "subparagraph"}.get(level,
                                                                                                        "subparagraph")
        text = re.sub(r"\\\\\s*", " ", text).strip()
        star = ""
        if self.numbered:
            m = re.match(r"^\\bpnum\{[^}]*\}\s*", text)
            if m:
                text = text[m.end():]
            else:
                star = "*"
        text = text.replace("\\bpnum{", "{")
        short = ""
        if "\\footnote" in text:
            short = "[" + re.sub(r"\\protect\\footnote\{.*", "", text, flags=re.DOTALL).strip() + "]"
        label = f"\\label{{{anchor}}}" if anchor else ""
        return f"\\{cmd}{star}{short}{{{text}}}{label}"

    def inline(self, el: ET.Element) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        cls = el.get("class", "").split() if tag else []
        if tag == "span" and "header-section-number" in cls:
            return "\\bpnum{" + markup.text_of(el) + "}"
        if tag == "span" and ({"figure-number", "table-number", "listing-number", "eqno"} & set(cls)):
            return ""
        if tag == "a" and "ref" in cls and el.get("href") and el.get("data-reference-type") in _REF_KINDS:
            return f"\\{_REF_KINDS[el.get('data-reference-type', '')]}{{{self.link_key(el.get('href', ''))}}}"
        if tag == "span" and "citation" in cls and el.get("data-cites"):
            return self.cite(el)
        if tag in ("code", "kbd", "samp", "tt") and any(isinstance(c.tag, str) for c in el):
            # typewriter text with formulas, links or notes in it: kept, not flattened to its letters
            return "\\texttt{" + self.inlines(el) + "}"
        link = el.get("href", "") if tag == "a" else ""
        if link and not is_external(link) and not link.startswith(("#", "mailto:")) and \
                resolve(self.href, link) not in self.book.resources and "noteref" not in cls:
            # a link to a file that isn't part of the book: kept as written
            anchor = self.anchor_key(el)
            return (self.inline_anchor(anchor) if anchor else "") + f"\\href{{{_url(link)}}}{{{self.inlines(el).strip()}}}"
        return super().inline(el)

    def cite(self, el: ET.Element) -> str:
        self.uses.add("cite")
        keys = ",".join(el.get("data-cites", "").split())
        mode = el.get("data-cite-mode", "normal")
        pre, post = el.get("data-cite-prefix", ""), el.get("data-cite-suffix", "")
        notes = (f"[{escape(pre)}][{escape(post)}]" if pre else f"[{escape(post)}]" if post else "")
        if mode == "nocite":
            return f"\\nocite{{{keys}}}"
        cmd = {"textual": "citet", "author": "citeauthor", "year": "citeyearpar", "bare-year": "citeyear",
               "bare": "citealp"}.get(mode, "citep")
        if self.cite_numeric() and cmd not in ("citeauthor", "citealp"):
            cmd = "cite"
            notes = f"[{escape(post)}]" if post else ""
        return f"\\{cmd}{notes}{{{keys}}}"

    def cite_numeric(self) -> bool:
        if self.ref_style is None:
            self.ref_style = "author-year"
            for href, _ in self.book.documents():
                for div in self.book.tree(href).iter("div"):
                    if "references" in div.get("class", "").split():
                        self.ref_style = div.get("data-style") or (
                            "numeric" if any((e.text or "").startswith("[") for e in div) else "author-year")
                        break
        return self.ref_style == "numeric"

    def code_block(self, code: str, lang: str) -> str:
        if self.in_arg or "\\end{verbatim}" in code:
            lines = code.split("\n")
            out = " \\\\\n".join("\\texttt{" + escape(line).replace(" ", "~") + "}" if line else "~" for line in lines)
            return "{\\ttfamily\n" + out + "\\par}"
        return "\\begin{verbatim}\n" + code + "\n\\end{verbatim}"

    def raw_block(self, text: str, fmt: str) -> str:
        if fmt in ("latex", "tex"):
            return text
        return self.code_block(text, "")

    def quote(self, inner: str) -> str:
        return self.env("quote", inner) if inner.strip() else ""

    def blocks(self, el: ET.Element) -> str:
        if el.tag == "blockquote":
            self.quote_depth += 1
            try:
                return super().blocks(el)
            finally:
                self.quote_depth -= 1
        return super().blocks(el)

    def list_item(self, li: ET.Element) -> str:
        self.list_depth += 1
        try:
            anchor = self.anchor_key(li)
            return (f"\\label{{{anchor}}} " if anchor else "") + super().list_item(li)
        finally:
            self.list_depth -= 1

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        if not items:
            return ""
        if self.list_depth >= 4 or self.list_depth + self.quote_depth >= 6:
            # LaTeX nests lists four deep (six with quotes): deeper ones become paragraphs
            return "\n\n".join((f"{start + i}. " if ordered else "-- ") + it for i, it in enumerate(items))
        env = "enumerate" if ordered else "itemize"
        head = ""
        if ordered and start != 1:
            counter = ["enumi", "enumii", "enumiii", "enumiv"][min(self.list_depth, 3)]
            head = f"\n\\setcounter{{{counter}}}{{{start - 1}}}"
        body = "\n".join("\\item " + (it if it.strip() else "~") for it in items)
        return f"\\begin{{{env}}}{head}\n{body}\n\\end{{{env}}}"

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        if not pairs:
            return ""
        items = []
        for term, defs in pairs:
            items.append(f"\\item[{{{term}}}] " + ("\n\n".join(defs) if defs else "~"))
        return "\\begin{description}\n" + "\n".join(items) + "\n\\end{description}"

    def rule(self) -> str:
        return "\\begin{center}\\rule{0.5\\linewidth}{0.5pt}\\end{center}"

    def read_table(self, el: ET.Element) -> Table:
        self.in_arg += 1
        self.in_cell += 1
        try:
            return super().read_table(el)
        finally:
            self.in_arg -= 1
            self.in_cell -= 1

    def table(self, table: Table) -> str:
        self.uses.add("table")
        width = table.width
        if not width:
            return ""
        grid = _grid(table)
        multiline = any("\n" in c.text or "\\newline" in c.text for r in table.rows for c in r)
        aligns = []
        for k in range(width):
            col = [c for r in table.rows for c in r if c.colspan == 1 and getattr(c, "_col", -1) == k]
            a = next((c.align for c in col if c.align), "")
            aligns.append(a)
        if multiline or width > 6:
            frac = 1 / width
            spec = f"p{{{frac * 0.94:.3f}\\linewidth}}" * width
        else:
            spec = "".join({"center": "c", "right": "r"}.get(a, "l") for a in aligns)
        anchor, self.table_anchor = self.table_anchor, None
        nested = self.in_arg > 0 or self.list_depth > 0
        env = "tabular" if nested else "longtable"
        lines = []
        if env == "longtable":
            lines.append(f"\\begin{{longtable}}[]{{@{{}}{spec}@{{}}}}")
            if table.caption or anchor:
                label = f"\\label{{{anchor}}}" if anchor else ""
                lines.append(f"\\caption{{{table.caption}}}{label}\\tabularnewline")
        else:
            lines.append(f"\\begin{{tabular}}{{@{{}}{spec}@{{}}}}")
        lines.append("\\toprule")
        for i, row in enumerate(grid):
            cells = []
            for c in row:
                if c is None:
                    cells.append("")
                    continue
                text = c.text.replace("\n\n", " \\newline ").replace("\n", " ")
                if c.rowspan > 1:
                    self.uses.add("multirow")
                    text = f"\\multirow{{{c.rowspan}}}{{*}}{{{text}}}"
                if c.colspan > 1:
                    al = {"center": "c", "right": "r"}.get(c.align, "l")
                    text = f"\\multicolumn{{{c.colspan}}}{{{al}}}{{{text}}}"
                cells.append(text)
            lines.append(" & ".join(cells) + " \\\\")
            if i + 1 == table.header_rows:
                lines.append("\\midrule")
                if env == "longtable":
                    lines.append("\\endhead")
        lines.append("\\bottomrule")
        lines.append(f"\\end{{{env}}}")
        out = "\n".join(lines)
        if env == "tabular" and table.caption:
            out = f"{table.caption}\n\n{out}"
        if env == "tabular" and anchor:
            out = self.anchor_line(anchor) + out
        return out

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        pic = self.picture(src, alt)
        if self.in_arg or self.list_depth or self.quote_depth:
            parts = [f"\\begin{{center}}\n{pic}"]
            if caption:
                parts.append(f"\n\n{caption}")
            parts.append("\n\\end{center}")
            return (self.anchor_line(anchor) if anchor else "") + "".join(parts)
        out = ["\\begin{figure}[htbp]", "\\centering", pic]
        if caption:
            out.append(f"\\caption{{{caption}}}")
        if anchor:
            out.append(f"\\label{{{anchor}}}")
        out.append("\\end{figure}")
        return "\n".join(out)

    def multi_figure(self, el: ET.Element) -> str:
        cap = el.find("figcaption")
        caption = self.inlines(cap).strip() if cap is not None else ""
        self.in_arg += 1
        try:
            inner = self.blocks(_without_tag(el, "figcaption"))
        finally:
            self.in_arg -= 1
        anchor = self.anchor_key(el)
        if self.list_depth or self.quote_depth or self.in_arg:
            return inner + (f"\n\n{caption}" if caption else "")
        out = ["\\begin{figure}[htbp]", "\\centering", inner]
        if caption:
            out.append(f"\\caption{{{caption}}}")
        if anchor:
            out.append(f"\\label{{{anchor}}}")
        out.append("\\end{figure}")
        return "\n".join(out)

    def image_name(self, el: ET.Element) -> str:
        orig = el.get("data-original")
        if orig and resolve(self.href, el.get("src", "")) not in self.book.resources:
            # a PDF/EPS figure of a LaTeX source: not in the book, so named as the source had it
            self.originals.add(orig)
            return orig
        return super().image_name(el)

    def picture(self, src: str, alt: str) -> str:
        if not src:
            return escape(alt)
        if is_external(src):
            return f"\\url{{{_url(src)}}}"
        if src in self.originals:
            self.uses.add("graphics")
            return f"\\includegraphics[width=\\bpmaxwidth,height=\\bpmaxheight,keepaspectratio]{{{src}}}"
        target = None
        for k, v in self.used_images.items():
            if v == src:
                target = k
        if target is not None and target not in self.book.resources:
            return f"\\fbox{{{escape(alt or src.rsplit('/', 1)[-1])}}}"
        self.uses.add("graphics")
        return f"\\includegraphics[width=\\bpmaxwidth,height=\\bpmaxheight,keepaspectratio]{{{src}}}"

    def image(self, src: str, alt: str) -> str:
        return self.picture(src, alt)

    def equation(self, el: ET.Element) -> str:
        latex = el.get("data-latex")
        if latex:
            return latex
        math = el.find("math")
        if math is None:
            return self.blocks(el)
        tex = mathml.to_tex(math)
        anchor = self.anchor_key(el)
        if anchor:
            return f"\\begin{{equation}}\\label{{{anchor}}}\n{tex}\n\\end{{equation}}"
        return self.math_tex(tex)

    def math_tex(self, tex: str) -> str:
        if tex.startswith("\\begin{") and re.match(r"\\begin\{(align|gather|multline|flalign|alignat|eqnarray|equation)",
                                                     tex):
            return tex
        return f"\\[\n{tex}\n\\]"

    def math_block(self, el: ET.Element) -> str:
        return self.math_tex(mathml.to_tex(el))

    def math_inline(self, el: ET.Element) -> str:
        if el.get("display") == "block":
            return "\n" + self.math_tex(mathml.to_tex(el)) + "\n"
        return f"\\({mathml.to_tex(el)}\\)"

    def references(self, el: ET.Element) -> str:
        self.style = el.get("data-style") or ("numeric" if any((e.text or "").startswith("[") for e in el) else
                                             "author-year")
        for e in el:
            if e.tag != "div":
                continue
            key = e.get("data-key") or (e.get("id") or "").removeprefix("ref-")
            text = self.inlines(e).strip()
            text = re.sub(r"^\{\[\}[^{}]*\{\]\}\s*", "", text)
            self.entries.append((key or f"ref{len(self.entries) + 1}", text, e.get("data-short", ""),
                                 e.get("data-year", "")))
        return ""

    def citation(self, keys: list[str], shown: str) -> str:
        self.uses.add("cite")
        return f"\\cite{{{','.join(keys)}}}"

    def footnote(self, body: str, note_id: str) -> str:
        key = re.sub(r"[^\w-]", "_", f"note-{self.href.rsplit('/', 1)[-1].rsplit('.', 1)[0]}-{note_id}")
        if key in self.notes_written:
            return f"\\textsuperscript{{\\ref{{{key}}}}}"  # the same note called again: its number, once more
        self.notes_written.add(key)
        return f"\\footnote{{\\label{{{key}}}{body.strip()}}}" if self.repeated(note_id) else f"\\footnote{{{body.strip()}}}"

    def repeated(self, note_id: str) -> bool:
        if self.call_counts is None:
            self.call_counts = {}
            for href, _ in self.book.documents():
                for a in self.book.tree(href).iter("a"):
                    if is_noteref(a) and "#" in a.get("href", ""):
                        k = a.get("href", "").split("#", 1)[1]
                        self.call_counts[k] = self.call_counts.get(k, 0) + 1
        return self.call_counts.get(note_id, 0) > 1

    def note_call(self, el: ET.Element) -> str:
        link = el.get("href", "")
        doc = resolve(self.href, link.split("#")[0]) if link.split("#")[0] else self.href
        key = re.sub(r"[^\w-]", "_", f"note-{doc.rsplit('/', 1)[-1].rsplit('.', 1)[0]}-{link.split('#', 1)[-1]}")
        if key in self.notes_written:
            return f"\\textsuperscript{{\\ref{{{key}}}}}"
        self.in_arg += 1
        try:
            return super().note_call(el)
        finally:
            self.in_arg -= 1

    def line_break(self) -> str:
        return "\\newline " if self.in_cell else "\\\\\n"

    def inline_anchor(self, key: str) -> str:
        return f"\\phantomsection\\label{{{key}}}"

    def escape(self, s: str) -> str:
        return escape(s)

    def emph(self, s: str) -> str:
        return f"\\emph{{{s}}}"

    def strong(self, s: str) -> str:
        return f"\\textbf{{{s}}}"

    def strike(self, s: str) -> str:
        self.uses.add("ulem")
        return f"\\sout{{{s}}}"

    def underline(self, s: str) -> str:
        self.uses.add("ulem")
        return f"\\uline{{{s}}}"

    def sup(self, s: str) -> str:
        return f"\\textsuperscript{{{s}}}"

    def sub(self, s: str) -> str:
        return f"\\textsubscript{{{s}}}"

    def smallcaps(self, s: str) -> str:
        return f"\\textsc{{{s}}}"

    def code(self, s: str) -> str:
        return "\\texttt{" + escape(s).replace("  ", " ~") + "}"

    def raw_inline(self, text: str, fmt: str) -> str:
        return text if fmt in ("latex", "tex") else self.code(text)

    def link(self, target: str, text: str, external: bool) -> str:
        if external:
            plain = re.sub(r"\\(?:url|nolinkurl)\{(.*)\}", r"\1", text)
            if not text or plain.replace("\\", "") in (target, target.removeprefix("mailto:")) or text == escape(target):
                return f"\\url{{{_url(target)}}}"
            return f"\\href{{{_url(target)}}}{{{text}}}"
        return f"\\hyperref[{target}]{{{text}}}" if text else f"\\ref{{{target}}}"


def _without(el: ET.Element, cls: str) -> ET.Element:
    copy = ET.Element(el.tag, el.attrib)
    copy.text = el.text
    for c in el:
        if cls not in c.get("class", "").split():
            copy.append(c)
    return copy


def _without_tag(el: ET.Element, tag: str) -> ET.Element:
    copy = ET.Element(el.tag, el.attrib)
    copy.text = el.text
    for c in el:
        if c.tag != tag:
            copy.append(c)
    return copy


def _drop_lead(el: ET.Element, tag: str, tail_mark: str) -> ET.Element:
    """A proof without the "Proof." the reader added (LaTeX adds its own) and the closing box."""
    copy = ET.Element(el.tag, el.attrib)
    copy.text = el.text
    kids = list(el)
    for k, c in enumerate(kids):
        if k == 0 and c.tag == "p" and len(c) and c[0].tag == tag:
            p = ET.Element("p", c.attrib)
            p.text = (c[0].tail or "").lstrip()
            for x in list(c)[1:]:
                p.append(x)
            c = p
        copy.append(c)
    for p in reversed([c for c in copy if c.tag == "p"]):
        if len(p) and p[-1].tail and p[-1].tail.rstrip().endswith(tail_mark):
            p[-1].tail = p[-1].tail.rstrip()[:-1].rstrip("\u00a0 ")
        elif not len(p) and p.text and p.text.rstrip().endswith(tail_mark):
            p.text = p.text.rstrip()[:-1].rstrip("\u00a0 ")
        break
    return copy


def _grid(table: Table) -> list[list[Cell | None]]:
    """Rows with the cells a rowspan covers left empty (None), as LaTeX wants them."""
    out: list[list[Cell | None]] = []
    pending: dict[int, int] = {}
    for row in table.rows:
        line: list[Cell | None] = []
        col = 0
        it = iter(row)
        cell = next(it, None)
        while cell is not None or any(pending.get(c, 0) > 0 for c in range(col, col + 1)):
            if pending.get(col, 0) > 0:
                pending[col] -= 1
                line.append(None)
                col += 1
                continue
            if cell is None:
                break
            cell._col = col  # type: ignore[attr-defined]
            line.append(cell)
            if cell.rowspan > 1:
                for c in range(col, col + cell.colspan):
                    pending[c] = cell.rowspan - 1
            for _ in range(cell.colspan - 1):
                pass
            col += cell.colspan
            cell = next(it, None)
        width = table.width
        filled = sum((c.colspan if c is not None else 1) for c in line)
        while filled < width:
            if pending.get(col, 0) > 0:
                pending[col] -= 1
            line.append(None)
            filled += 1
            col += 1
        out.append(line)
    return out


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    """Write ``book`` as a LaTeX document; with ``pictures``, the pictures it uses go beside it (images/)."""
    return write_text(_LatexWriter(book), path, pictures)


def _rstrip_end(el: ET.Element) -> None:
    """No white space at the end of ``el``'s text."""
    if len(el):
        if el[-1].tail:
            el[-1].tail = el[-1].tail.rstrip()
    elif el.text:
        el.text = el.text.rstrip()


def _thanks(s: str) -> str:
    """Notes in the title block are \\thanks there."""
    return s.replace("\\footnote{", "\\thanks{")


_THEOREM_HEAD = re.compile(r"^([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?)(?:\s+([0-9A-Z][\w.]*))?$")


def _theorem_head(el: ET.Element):
    """A theorem-like block (``<div class="lemma"><p><strong>Lemma 2</strong>. ...``): (name, note, body)."""
    first = el[0] if len(el) else None
    if first is None or first.tag != "p" or (first.text or "").strip() or not len(first):
        return None
    head = first[0]
    if head.tag not in ("strong", "em") or len(head):
        return None
    m = _THEOREM_HEAD.match((head.text or "").strip())
    if not m or m.group(1) == "Proof":
        return None
    kids = list(first)[1:]
    note = None
    tail = (head.tail or "").lstrip()
    if tail.startswith("(") and kids and kids[0].tag == "span" and (kids[0].tail or "").lstrip().startswith(")"):
        note = kids[0]
        tail = (kids[0].tail or "").lstrip()[1:]
        kids = kids[1:]
    tail = tail.lstrip(".: ")
    body = ET.Element("div")
    p = ET.SubElement(body, "p")
    p.text = tail
    for k in kids:
        p.append(k)
    for c in list(el)[1:]:
        body.append(c)
    for q in body:  # the reader's italics for the body: LaTeX sets the body in italics itself
        inner = [c for c in q if isinstance(c.tag, str)]
        if q.tag == "p" and len(inner) == 1 and inner[0].tag == "em" and not (q.text or "").strip()                 and not (inner[0].tail or "").strip():
            em = inner[0]
            q.remove(em)
            q.text = (q.text or "") + (em.text or "")
            for c in em:
                q.append(c)
            if len(q):
                q[-1].tail = (q[-1].tail or "") + (em.tail or "")
            else:
                q.text += em.tail or ""
    return m.group(1), note, body, m.group(2) is not None
