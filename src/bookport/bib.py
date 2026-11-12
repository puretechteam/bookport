"""Bibliographies: BibTeX and BibLaTeX files read into entries, and the citations in a book filled in.

A reader marks each citation as ``<span class="citation" data-cites="key1 key2">`` (the text inside is
replaced) with, when it matters:

- ``data-cite-mode``: ``normal`` (the default: "(Vaswani et al. 2017)" or "[1]"), ``textual``
  ("Vaswani et al. (2017)"), ``author``, ``year``, ``bare`` (no parentheses), ``bare-year``,
  ``nocite`` (listed, not shown);
- ``data-cite-prefix`` / ``data-cite-suffix``: notes before and after the whole group ("see", "p. 3");
- ``data-cite-notes``: per key, ``prefix|suffix`` pairs separated by ``;`` (Pandoc Markdown's
  ``[see @a, p. 3; @b]``).

The reference list goes where the document had one, a ``<div class="references">`` with no children
(``\\bibliography``, ``\\printbibliography``, Markdown's ``#refs``), or at the end of the book. Each
work is a ``<div class="csl-entry" id="ref-key">``. Two styles: ``author-year`` (Chicago-like, the list
sorted by author) and ``numeric`` ([1], the list in citation order).

Entries can also come ready-made (a ``.bbl`` file's ``\\bibitem``s, which arXiv papers ship): an
:class:`Entry` with ``element`` set is placed as it is, and cited by its ``label``.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import markup

# --- LaTeX text: accents and symbols (shared with the LaTeX reader) ---------------------------------

ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304", ".": "\u0307",
           "u": "\u0306", "v": "\u030c", "H": "\u030b", "c": "\u0327", "k": "\u0328", "d": "\u0323", "b": "\u0331",
           "r": "\u030a", "t": "\u0361", "G": "\u030f", "U": "\u030e"}

SYMBOLS = {
    "ss": "ß", "SS": "SS", "o": "ø", "O": "Ø", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "aa": "å", "AA": "Å",
    "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ", "dh": "ð", "DH": "Ð", "th": "þ", "TH": "Þ", "ng": "ŋ", "NG": "Ŋ",
    "dj": "đ", "DJ": "Đ", "copyright": "©", "textcopyright": "©", "textregistered": "®", "texttrademark": "™",
    "pounds": "£", "textsterling": "£", "euro": "€", "texteuro": "€", "EUR": "€", "textyen": "¥", "S": "§", "P": "¶",
    "textsection": "§", "textparagraph": "¶", "dag": "†", "ddag": "‡", "textdagger": "†", "textdaggerdbl": "‡",
    "dots": "…", "ldots": "…", "textellipsis": "…", "textbullet": "•", "textperiodcentered": "·",
    "textendash": "–", "textemdash": "—", "textquoteleft": "‘", "textquoteright": "’", "textquotedblleft": "“",
    "textquotedblright": "”", "textquotedbl": '"', "textquotesingle": "'", "guillemotleft": "«",
    "guillemotright": "»", "guillemetleft": "«", "guillemetright": "»", "guilsinglleft": "‹", "guilsinglright": "›",
    "quotedblbase": "„", "quotesinglbase": "‚", "textless": "<", "textgreater": ">", "textbackslash": "\\",
    "textbar": "|", "textasciitilde": "~", "textasciicircum": "^", "textunderscore": "_", "textbraceleft": "{",
    "textbraceright": "}", "textdollar": "$", "textdegree": "°", "degree": "°", "textonehalf": "½",
    "textonequarter": "¼", "textthreequarters": "¾", "textmu": "µ", "texttimes": "×", "textdiv": "÷", "textpm": "±",
    "textexclamdown": "¡", "textquestiondown": "¿", "LaTeX": "LaTeX", "TeX": "TeX", "LaTeXe": "LaTeX2e",
    "BibTeX": "BibTeX", "XeTeX": "XeTeX", "LuaTeX": "LuaTeX", "checkmark": "✓", "textcelsius": "℃",
    "textvisiblespace": "␣", "slash": "/", "nobreakspace": "\u00a0", "textnumero": "№", "textordfeminine": "ª",
    "textordmasculine": "º", "textcent": "¢", "textflorin": "ƒ", "textlira": "₤", "textwon": "₩", "textbaht": "฿",
    "textestimated": "℮", "textpertenthousand": "‱", "textperthousand": "‰", "textmusicalnote": "♪",
    "textleftarrow": "←", "textrightarrow": "→", "textuparrow": "↑", "textdownarrow": "↓", "textopenbullet": "◦",
    "textbigcircle": "◯", "textreferencemark": "※", "textinterrobang": "‽", "textdiscount": "⁒",
    "textlquill": "⁅", "textrquill": "⁆", "textcircledP": "℗", "textservicemark": "℠", "textohm": "Ω",
    "textmho": "℧", "textlnot": "¬", "textbrokenbar": "¦", "textcurrency": "¤", "textasteriskcentered": "∗",
    "textminus": "−", "textfractionsolidus": "⁄", "textsuperscript1": "¹", "textonesuperior": "¹",
    "texttwosuperior": "²", "textthreesuperior": "³", "textacutedbl": "˝", "textasciiacute": "´",
    "textasciibreve": "˘", "textasciicaron": "ˇ", "textasciidieresis": "¨", "textasciimacron": "¯",
    "quad": "\u2003", "qquad": "\u2003\u2003", "enspace": "\u2002", "thinspace": "\u2009", "enskip": "\u2002",
    "space": " ", " ": " ", ",": "\u2009", ";": "\u2005", ":": "\u2005", ">": "\u2005", "!": "", "/": "", "-": "",
    "@": "", "&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}", "textasciigrave": "`",
    "lq": "‘", "rq": "’", "i@": "ı",
}

# Greek letters and the commonest symbols, for text that is math in a bibliography field
MATH_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ",
    "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
    "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "ell": "ℓ", "infty": "∞", "pm": "±", "mp": "∓", "times": "×", "cdot": "·", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "sim": "∼", "simeq": "≃", "equiv": "≡", "in": "∈", "to": "→",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏",
    "int": "∫", "sqrt": "√", "circ": "∘", "star": "⋆", "ast": "∗", "prime": "′", "hbar": "ℏ", "emptyset": "∅",
    "langle": "⟨", "rangle": "⟩", "mid": "∣", "log": "log", "exp": "exp", "sin": "sin", "cos": "cos", "max": "max",
    "min": "min", "lim": "lim", "ln": "ln",
}

DASHES = re.compile(r"---|--|``|''|`|'|!`|\?`")
_DASH_MAP = {"---": "—", "--": "–", "``": "“", "''": "”", "`": "‘", "'": "’", "!`": "¡", "?`": "¿"}


def ligatures(s: str) -> str:
    """TeX's text ligatures: dashes and quotes."""
    if "-" not in s and "`" not in s and "'" not in s:
        return s
    return DASHES.sub(lambda m: _DASH_MAP[m.group(0)], s)


def accent(mark: str, base: str) -> str:
    """A TeX accent command (``\\'``, ``\\c``, ...) on the first letter of ``base``."""
    comb = ACCENTS.get(mark)
    if comb is None:
        return base
    if not base:
        return unicodedata.normalize("NFC", " " + comb) if mark not in "'`^\"~" else {
            "'": "´", "`": "`", "^": "^", '"': "¨", "~": "~"}[mark]
    first = {"ı": "i", "ȷ": "j"}.get(base[0], base[0])
    return unicodedata.normalize("NFC", first + comb) + base[1:]


_DETEX = re.compile(r"\\([A-Za-z@]+)\*?\s*|\\(.)|([{}$~])|([^\\{}$~]+)", re.DOTALL)
_KEEP_ARG = {"emph", "textit", "textbf", "textsc", "textrm", "textsf", "texttt", "textup", "textsl", "textnormal",
             "mbox", "text", "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathcal", "mathbb", "url", "uline",
             "underline", "textsuperscript", "textsubscript", "ensuremath", "bibinfo", "bibfield", "MakeUppercase",
             "MakeLowercase", "NoCaseChange", "protect", "relax", "bf", "it", "em", "sc", "rm", "tt", "sl", "sf",
             "bfseries", "itshape", "scshape", "normalfont", "upshape", "mdseries", "nolinkurl", "path", "hbox"}


def detex(s: str) -> str:
    """LaTeX in a bibliography field (or a label) as plain text: accents, symbols, dashes, quotes,
    braces and font commands gone. Linear: one pass over the text."""
    if "\\" not in s and "{" not in s and "$" not in s and "~" not in s and "-" not in s and "`" not in s \
            and "'" not in s:
        return " ".join(s.split())
    out: list[str] = []
    pending_accent = ""
    for m in _DETEX.finditer(s):
        name, char, special, text = m.group(1), m.group(2), m.group(3), m.group(4)
        piece = ""
        if name is not None:
            if name in ACCENTS and len(name) == 1:
                pending_accent = name
                continue
            if name in SYMBOLS:
                piece = SYMBOLS[name]
            elif name in MATH_SYMBOLS:
                piece = MATH_SYMBOLS[name]
            elif name in ("and",):
                piece = " and "
            elif name in ("newblock", "par", "\\", "newline"):
                piece = " "
            else:
                continue  # a command with its argument kept (\emph{x} -> x) or dropped (\relax)
        elif char is not None:
            if char in ACCENTS:
                pending_accent = char
                continue
            piece = SYMBOLS.get(char, char if char not in "\\\n" else " ")
        elif special is not None:
            piece = "\u00a0" if special == "~" else ""
            if pending_accent and special in "{$":
                continue  # the accent applies to what's inside the braces
        else:
            piece = ligatures(text)
        if pending_accent:
            stripped = piece.lstrip()
            if stripped or piece == "":
                piece = accent(pending_accent, stripped)
                pending_accent = ""
        out.append(piece)
    return " ".join("".join(out).replace("\u00a0", " ").split())


# --- names -------------------------------------------------------------------------------------------

@dataclass
class Name:
    first: str = ""
    von: str = ""
    last: str = ""
    jr: str = ""
    others: bool = False  # "and others": et al.

    def family(self) -> str:
        return f"{self.von} {self.last}".strip()

    def full(self) -> str:
        out = " ".join(p for p in (self.first, self.von, self.last) if p)
        return f"{out}, {self.jr}" if self.jr else out

    def inverted(self) -> str:
        fam = self.family() + (f", {self.jr}" if self.jr else "")
        return f"{fam}, {self.first}" if self.first else fam


def _split_depth0(s: str, sep_re: re.Pattern) -> list[str]:
    """Split ``s`` where ``sep_re`` matches outside braces."""
    parts, depth, start, i = [], 0, 0, 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            m = sep_re.match(s, i)
            if m and m.end() > i:
                parts.append(s[start:i])
                start = i = m.end()
                continue
        i += 1
    parts.append(s[start:])
    return parts


_AND = re.compile(r"\s+and\s+", re.IGNORECASE)
_COMMA = re.compile(r",")
_SPACE = re.compile(r"[\s~]+")


def _is_lower_word(word: str) -> bool:
    """BibTeX's test for a "von" word: its first letter outside braces is lower case."""
    depth = 0
    i = 0
    while i < len(word):
        c = word[i]
        if c == "{":
            depth += 1
            if depth == 1 and word[i + 1:i + 2] == "\\":  # {\"o}: the letter after the command decides
                m = re.match(r"\\[A-Za-z]+\s*|\\.", word[i + 1:])
                rest = word[i + 1 + (m.end() if m else 1):].lstrip("{")
                return bool(rest) and rest[0].islower()
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and c.isalpha():
            return c.islower()
        i += 1
    return False


def parse_names(s: str) -> list[Name]:
    names = []
    for part in _split_depth0(s.strip(), _AND):
        part = part.strip()
        if not part:
            continue
        if part.lower() == "others":
            names.append(Name(others=True))
            continue
        commas = [p.strip() for p in _split_depth0(part, _COMMA)]
        if len(commas) >= 2:
            words = [w for w in _split_depth0(commas[0], _SPACE) if w]
            k = 0
            while k < len(words) - 1 and _is_lower_word(words[k]):
                k += 1
            von, last = words[:k], words[k:]
            jr = commas[1] if len(commas) >= 3 else ""
            first = commas[2] if len(commas) >= 3 else commas[1]
            names.append(Name(detex(first), detex(" ".join(von)), detex(" ".join(last)), detex(jr)))
            continue
        words = [w for w in _split_depth0(part, _SPACE) if w]
        if len(words) == 1:
            names.append(Name(last=detex(words[0])))
            continue
        lower = [k for k in range(len(words) - 1) if _is_lower_word(words[k])]
        if lower:
            a, b = lower[0], lower[-1] + 1
            names.append(Name(detex(" ".join(words[:a])), detex(" ".join(words[a:b])), detex(" ".join(words[b:]))))
        else:
            names.append(Name(detex(" ".join(words[:-1])), "", detex(words[-1])))
    return names


# --- entries -----------------------------------------------------------------------------------------

MONTHS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April", "may": "May", "jun": "June",
          "jul": "July", "aug": "August", "sep": "September", "oct": "October", "nov": "November", "dec": "December"}


@dataclass
class Entry:
    key: str
    type: str = "misc"
    fields: dict[str, str] = field(default_factory=dict)  # name (lower case) -> value, LaTeX as written
    element: ET.Element | None = None  # a ready-made entry (from a .bbl), placed as it is
    label: str = ""  # a ready-made entry's own label: "[1]", "[Vas17]"
    short: str = ""  # author(s) for citing: "Vaswani et al."
    year: str = ""

    def get(self, name: str) -> str:
        return detex(self.fields.get(name, ""))

    def names(self, role: str = "author") -> list[Name]:
        raw = self.fields.get(role, "")
        return parse_names(raw) if raw else []


MAX_BIB = 20 * 1024 * 1024  # characters of .bib read for one book
MAX_VALUE = 65_536  # characters of one field or @string value


class _BibParser:
    """A BibTeX/BibLaTeX scanner: one pass, no backtracking."""

    def __init__(self, text: str, strings: dict[str, str]):
        self.s = text
        self.i = 0
        self.strings = strings

    def ws(self):
        s, n = self.s, len(self.s)
        while self.i < n and s[self.i] in " \t\r\n":
            self.i += 1

    def ident(self) -> str:
        m = re.compile(r"[^\s\"#%'(),={}]+").match(self.s, self.i)
        if not m:
            return ""
        self.i = m.end()
        return m.group(0)

    def braced(self, close_c: str) -> str:
        """The text up to the matching close (self.i just after the opening one); braces nest."""
        s, n = self.s, len(self.s)
        depth, start = 0, self.i
        while self.i < n:
            c = s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0 and close_c == "}":
                    out = s[start:self.i]
                    self.i += 1
                    return out
                depth -= 1
                depth = max(depth, 0)
            elif c == close_c and depth == 0:
                out = s[start:self.i]
                self.i += 1
                return out
            self.i += 1
        return s[start:]

    def value(self) -> str:
        parts: list[str] = []
        size = 0
        while True:
            self.ws()
            if self.i >= len(self.s):
                break
            c = self.s[self.i]
            if c == "{":
                self.i += 1
                parts.append(self.braced("}"))
            elif c == '"':
                self.i += 1
                parts.append(self.braced('"'))
            else:
                word = self.ident()
                if not word:
                    break
                low = word.lower()
                parts.append(word if word.isdigit() else self.strings.get(low) or MONTHS.get(low) or word)
            # bounded as it is built: @string{a = a # a} doubled with every line (24 lines made a
            # 33-million-character title)
            size += len(parts[-1])
            if size > MAX_VALUE:
                parts[-1] = parts[-1][:max(0, len(parts[-1]) - (size - MAX_VALUE))]
                size = MAX_VALUE
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == "#":
                self.i += 1
                continue
            break
        return "".join(parts)

    def entries(self):
        s, n = self.s, len(self.s)
        while True:
            at = s.find("@", self.i)
            if at < 0:
                return
            self.i = at + 1
            self.ws()
            kind = self.ident().lower()
            self.ws()
            if not kind or self.i >= n or s[self.i] not in "{(":
                continue
            close = "}" if s[self.i] == "{" else ")"
            self.i += 1
            if kind in ("comment", "preamble"):
                self.braced(close)
                continue
            if kind == "string":
                self.ws()
                name = self.ident().lower()
                self.ws()
                if self.i < n and s[self.i] == "=":
                    self.i += 1
                    self.strings[name] = self.value()
                self.ws()
                if self.i < n and s[self.i] == close:
                    self.i += 1
                continue
            self.ws()
            m = re.compile(r"[^,\s{}()]*").match(s, self.i)
            key = m.group(0) if m else ""
            self.i = m.end() if m else self.i
            fields: dict[str, str] = {}
            while self.i < n:
                self.ws()
                if self.i < n and s[self.i] == ",":
                    self.i += 1
                    continue
                if self.i >= n or s[self.i] == close:
                    self.i += 1
                    break
                if s[self.i] == "@":  # a damaged entry: the next one starts
                    break
                name = self.ident().lower()
                if not name:
                    self.i += 1
                    continue
                self.ws()
                if self.i < n and s[self.i] == "=":
                    self.i += 1
                    fields[name] = self.value()
            if key:
                yield Entry(key, kind or "misc", fields)


def parse(text: str, strings: dict[str, str] | None = None) -> dict[str, Entry]:
    """Entries of a BibTeX/BibLaTeX text, by key (the first of a repeated key wins)."""
    out: dict[str, Entry] = {}
    for e in _BibParser(text[:MAX_BIB], strings if strings is not None else {}).entries():
        out.setdefault(e.key, e)
    for e in out.values():  # crossref: missing fields come from the parent
        parent = out.get(e.fields.get("crossref", ""))
        if parent is not None and parent is not e:
            for k, v in parent.fields.items():
                e.fields.setdefault(k, v)
            if "title" in parent.fields:
                e.fields.setdefault("booktitle", parent.fields["title"])
    return out


def load(paths) -> dict[str, Entry]:
    """Entries of one or more .bib files (paths already checked with ``limits.safe_path``)."""
    strings: dict[str, str] = {}
    out: dict[str, Entry] = {}
    budget = MAX_BIB
    for p in paths:
        try:
            text = markup.decode(Path(p).read_bytes()[:budget])
        except OSError:
            continue
        budget -= len(text)
        for k, e in parse(text, strings).items():
            out.setdefault(k, e)
        if budget <= 0:
            break
    return out


# --- citing ------------------------------------------------------------------------------------------

def _short_names(names: list[Name]) -> str:
    fam = [n.family() for n in names if not n.others]
    if not fam:
        return ""
    if len(fam) >= 4 or any(n.others for n in names) and len(fam) >= 1:
        return f"{fam[0]} et al."
    if len(fam) == 1:
        return fam[0]
    if len(fam) == 2:
        return f"{fam[0]} and {fam[1]}"
    return f"{fam[0]}, {fam[1]}, and {fam[2]}"


def _year(e: Entry) -> str:
    y = e.get("year") or e.get("date")[:4]
    m = re.search(r"\d{4}", y)
    return m.group(0) if m else (y or "n.d.")


def _cite_info(e: Entry) -> tuple[str, str]:
    if e.short or e.year:
        return e.short, e.year
    names = e.names("author") or e.names("editor")
    short = _short_names(names) or e.get("organization") or e.get("institution") or e.get("title") or e.key
    return short, _year(e)


@dataclass
class _Cite:
    keys: list[str]
    mode: str = "normal"
    prefix: str = ""
    suffix: str = ""
    notes: list[tuple[str, str]] = field(default_factory=list)


def _cite_of(span: ET.Element) -> _Cite:
    keys = span.get("data-cites", "").split()
    c = _Cite(keys, span.get("data-cite-mode", "normal") or "normal", span.get("data-cite-prefix", ""),
              span.get("data-cite-suffix", ""))
    raw = span.get("data-cite-notes")
    if raw:
        for pair in raw.split(";"):
            pre, _, post = pair.partition("|")
            c.notes.append((pre.strip(), post.strip()))
    return c


class _Citer:
    def __init__(self, entries: dict[str, Entry], style: str):
        self.entries = entries
        self.lower = {k.lower(): k for k in entries}
        self.style = style
        self.order: list[str] = []  # keys in citation order
        self.missing: list[str] = []
        self.suffix: dict[str, str] = {}  # key -> "a"/"b" when two works share author and year
        self.number: dict[str, int] = {}

    def find(self, key: str) -> str | None:
        if key in self.entries:
            return key
        return self.lower.get(key.lower())

    def see(self, key: str) -> None:
        k = self.find(key)
        if k is None:
            if key not in self.missing:
                self.missing.append(key)
        elif k not in self.number:
            self.order.append(k)
            self.number[k] = len(self.order)

    def listed(self) -> list[str]:
        keys = list(self.order)
        ready = [k for k in self.entries if self.entries[k].element is not None]
        if ready:  # a .bbl lists what it lists, in its order
            return ready
        if self.style == "numeric":
            return keys
        return sorted(keys, key=self._sort_key)

    def _sort_key(self, k: str):
        e = self.entries[k]
        names = e.names("author") or e.names("editor")
        who = " ".join(n.inverted() for n in names).lower() if names else (e.get("title") or k).lower()
        return (who, _year(e), e.get("title").lower())

    def disambiguate(self) -> None:
        if self.style == "numeric":
            return
        groups: dict[tuple[str, str], list[str]] = {}
        for k in sorted(self.order, key=self._sort_key):
            if self.entries[k].element is not None:
                continue
            groups.setdefault(_cite_info(self.entries[k]), []).append(k)
        for keys in groups.values():
            if len(keys) > 1:
                for n, k in enumerate(keys):
                    self.suffix[k] = chr(ord("a") + n % 26)

    def label(self, k: str) -> str:
        e = self.entries[k]
        if e.label:
            return e.label
        return str(self.number.get(k, "?"))

    def numeric(self, k: str) -> bool:
        e = self.entries[k]
        return self.style == "numeric" and not (e.short and e.year) or bool(e.label)

    def text(self, c: _Cite) -> str:
        found = [(key, self.find(key)) for key in c.keys]
        parts = []
        numeric = any(k is not None and self.numeric(k) for _, k in found)
        for n, (key, k) in enumerate(found):
            pre, post = c.notes[n] if n < len(c.notes) else ("", "")
            if k is None:
                body = f"{key}?"
            elif numeric:
                body = self.label(k).strip("[]")
            else:
                short, year = _cite_info(self.entries[k])
                year += self.suffix.get(k, "")
                if c.mode == "author":
                    body = short
                elif c.mode in ("year", "bare-year"):
                    body = year
                elif c.mode == "textual":
                    note = f", {post}" if post else ""
                    post = ""
                    body = f"{short} ({year}{note})"
                else:
                    body = f"{short} {year}" if c.mode != "bare" else f"{short}, {year}"
            parts.append(" ".join(x for x in (pre, body) if x) + (f", {post}" if post else ""))
        if c.mode == "author" and numeric:
            return "; ".join(self._author_only(k, key) for key, k in found)
        if numeric and c.mode == "textual":
            return "; ".join(f"{self._author_only(k, key)} [{p}]" for (key, k), p in zip(found, parts))
        inner = ("; " if not numeric else ", ").join(parts)
        if numeric and len(parts) > 1 and not c.notes:
            inner = _ranges([p for p in parts])
        inner = " ".join(x for x in (c.prefix, inner) if x) + (f", {c.suffix}" if c.suffix else "")
        if c.mode in ("textual", "author", "bare", "bare-year"):
            return inner
        if c.mode == "year":
            return f"({inner})"
        return f"[{inner}]" if numeric else f"({inner})"

    def _author_only(self, k: str | None, key: str) -> str:
        if k is None:
            return f"{key}?"
        return _cite_info(self.entries[k])[0] or key


def _ranges(nums: list[str]) -> str:
    """"1, 2, 3, 5" -> "1–3, 5" when every label is a number."""
    if not all(n.isdigit() for n in nums):
        return ", ".join(nums)
    vals = sorted({int(n) for n in nums})
    out, i = [], 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[j] + 1:
            j += 1
        out.append(f"{vals[i]}–{vals[j]}" if j - i >= 2 else ", ".join(str(v) for v in vals[i:j + 1]))
        i = j + 1
    return ", ".join(out)


# --- the reference list ------------------------------------------------------------------------------

def _join_names(names: list[Name]) -> str:
    real = [n for n in names if not n.others]
    if not real:
        return ""
    shown = [real[0].inverted()] + [n.full() for n in real[1:]]
    if len(real) > 10:
        shown = shown[:7]
        return ", ".join(shown) + ", et al."
    tail = ", et al." if any(n.others for n in names) else ""
    if len(shown) == 1:
        return shown[0] + tail
    if tail:
        return ", ".join(shown) + tail
    return ", ".join(shown[:-1]) + (", and " if len(shown) > 2 else " and ") + shown[-1]


def _end(s: str) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".?!" else s + "."


def render(e: Entry, year_suffix: str = "") -> list[tuple[str, str]]:
    """One entry as (text, kind) runs: kind "" (plain), "em" (italic), "a" (a link)."""
    runs: list[tuple[str, str]] = []
    t = e.type.lower()
    authors = e.names("author")
    editors = e.names("editor")
    who = _join_names(authors) or (_join_names(editors) + (", ed." if len(editors) == 1 else ", eds.") if editors else "")
    who = who or e.get("organization") or e.get("institution")
    title = e.get("title")
    if e.fields.get("subtitle"):
        title = f"{title}: {e.get('subtitle')}"
    year = _year(e) + year_suffix
    if who:
        runs.append((_end(who) + " ", ""))
    runs.append((_end(year) + " ", ""))
    whole = t in ("book", "mvbook", "proceedings", "collection", "manual", "booklet", "phdthesis", "mastersthesis",
                  "thesis", "report") and t not in ("phdthesis", "mastersthesis", "thesis", "report", "techreport")
    if title:
        if whole:
            runs.append((_end(title), "em"))
            runs.append((" ", ""))
        else:
            runs.append(("“" + _end(title) + "” ", ""))
    journal = e.get("journal") or e.get("journaltitle")
    if journal:
        runs.append((journal, "em"))
        vol = e.get("volume")
        num = e.get("number") or e.get("issue")
        pages = e.get("pages").replace("--", "–")
        tail = (f" {vol}" if vol else "") + (f" ({num})" if num else "") + (f": {pages}" if pages else "")
        # the space after the journal's name: _end strips it ("Nature5 (2)")
        runs.append(((" " if not tail.startswith(":") else "") + _end(tail) + " " if tail else ". ", ""))
    elif e.get("booktitle"):
        runs.append(("In ", ""))
        runs.append((e.get("booktitle"), "em"))
        pages = e.get("pages").replace("--", "–")
        runs.append(((f", {pages}" if pages else "") + ". ", ""))
    if t in ("phdthesis", "mastersthesis", "thesis"):
        kind = e.get("type") or ("PhD thesis" if t == "phdthesis" else "Master's thesis" if t == "mastersthesis" else "Thesis")
        runs.append((_end(", ".join(x for x in (kind, e.get("school") or e.get("institution")) if x)) + " ", ""))
    elif t in ("techreport", "report"):
        kind = e.get("type") or "Technical report"
        num = e.get("number")
        runs.append((_end(", ".join(x for x in (f"{kind} {num}".strip(), e.get("institution")) if x)) + " ", ""))
    pub = ": ".join(x for x in (e.get("address") or e.get("location"), e.get("publisher")) if x)
    if pub and not journal:
        runs.append((_end(pub) + " ", ""))
    for extra in ("howpublished", "note"):
        v = e.get(extra)
        if v and not re.match(r"^\\?url|^https?:", v):
            runs.append((_end(v) + " ", ""))
    eprint = e.get("eprint")
    if eprint and (e.get("archiveprefix") or e.get("eprinttype") or "").lower() in ("arxiv", "") \
            and not e.get("journal"):
        if not e.fields.get("doi") and not e.fields.get("url"):
            runs.append((f"https://arxiv.org/abs/{eprint}", "a"))  # the paper's arXiv page
        else:
            runs.append((f"arXiv:{eprint}. ", ""))
    doi = e.fields.get("doi", "").strip()
    url = e.fields.get("url", "").strip()
    if doi:
        link = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        runs.append((link, "a"))
    elif url:
        runs.append((url, "a"))
    if runs and runs[-1][1] == "":
        runs[-1] = (runs[-1][0].rstrip(), "")
    return [r for r in runs if r[0]]


def _entry_element(e: Entry, key_id: str, label: str, year_suffix: str) -> ET.Element:
    div = ET.Element("div", {"class": "csl-entry", "id": key_id, "data-key": e.key})
    if label:
        div.text = f"[{label}] "
    for text, kind in render(e, year_suffix):
        if kind == "em":
            ET.SubElement(div, "em").text = text
        elif kind == "a":
            a = ET.SubElement(div, "a", {"href": text})
            a.text = text
        else:
            markup.append_text(div, text)
    return div


def ref_id(key: str) -> str:
    return "ref-" + re.sub(r"[^\w.-]", "_", key)


def apply(book, entries: dict[str, Entry], style: str = "author-year", title: str | None = None) -> int:
    """Fill every citation in ``book`` from ``entries`` and add the reference list. Returns the number
    of citations filled. Keys without an entry show as "key?" and are named in ``book.lost``."""
    citer = _Citer(entries, style if style in ("author-year", "numeric") else "author-year")
    spans: list[tuple[str, ET.Element, _Cite]] = []
    nocite_all = False
    for href, _ in book.documents():
        for span in book.tree(href).iter("span"):
            if "citation" in span.get("class", "").split() and span.get("data-cites") is not None:
                c = _cite_of(span)
                spans.append((href, span, c))
                for k in c.keys:
                    if k == "*" and c.mode == "nocite":
                        nocite_all = True
                    else:
                        citer.see(k)
    if nocite_all:
        for k in entries:
            citer.see(k)
    citer.disambiguate()
    changed: set[str] = set()
    for href, span, c in spans:
        for child in list(span):
            span.remove(child)
        span.text = "" if c.mode == "nocite" else citer.text(c)
        changed.add(href)
    listed = citer.listed()
    # the list goes where the document asked for it, else at the end
    place = None
    for href, _ in book.documents():
        for div in book.tree(href).iter("div"):
            if "references" in div.get("class", "").split() and len(div) == 0:
                place = (href, div)
                break
            if "references" in div.get("class", "").split() and div.get("data-bib") == "here":
                place = (href, div)
                break
        if place:
            break
    if listed and any(entries[k].element is None for k in listed):
        if place is None:
            hrefs = [h for h, _ in book.documents()]
            if hrefs:
                body = book.tree(hrefs[-1]).find("body")
                if body is not None:
                    if title is not False:
                        ET.SubElement(body, "h1", {"class": "unnumbered"}).text = title or "References"
                    div = ET.SubElement(body, "div", {"class": "references", "id": "refs"})
                    place = (hrefs[-1], div)
        if place is not None:
            div = place[1]
            div.attrib.pop("data-bib", None)
            div.set("data-style", citer.style)
            for k in listed:
                e = entries[k]
                if e.element is not None:
                    continue
                label = str(citer.number[k]) if citer.style == "numeric" and k in citer.number else ""
                el = _entry_element(e, ref_id(k), label, citer.suffix.get(k, ""))
                short, year = _cite_info(e)
                el.set("data-short", short)
                el.set("data-year", year + citer.suffix.get(k, ""))
                div.append(el)
            changed.add(place[0])
    elif place is not None and len(place[1]) == 0:
        place[1].attrib.pop("data-bib", None)
    for href in changed:
        book.set_tree(href, book.tree(href))
    if citer.missing:
        shown = ", ".join(citer.missing[:20]) + (f" and {len(citer.missing) - 20} more" if len(citer.missing) > 20 else "")
        book.lost.append(f"Citations with no entry in the bibliography (shown as key?): {shown}")
    return len(spans)
