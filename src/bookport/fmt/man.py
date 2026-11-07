"""man pages (roff with the man macros): a reader and a writer.

Read: .TH, sections, paragraphs (.PP .LP .P .HP), tagged and indented paragraphs as definition,
bullet and numbered lists (.TP .TQ .IP), relative indents (.RS/.RE), the font macros and escapes,
special characters, no-fill and example blocks, links (.UR/.UE, .MT/.ME), tbl tables (.TS/.TE),
simple conditionals and string definitions, and ``.so`` includes through ``safe_path``. eqn and pic
blocks, macro definitions and unknown requests are kept as text and named in ``book.lost``.

Write: a man page with .TH from the title, .SH/.SS sections, .TP for definition lists, .IP lists,
.EX blocks for code and tbl tables.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup, mathml
from ..book import Book, Metadata
from ..limits import safe_path
from ..textwriter import Table, TextWriter, write_text
from .mediawiki import attr, esc, html_book

MAX_SO_DEPTH = 8
MAX_LINES = 2_000_000
MAX_RS = 32

# roff special characters (\(xx and \[name]) that man pages use
CHARS = {
    "em": "\u2014", "en": "\u2013", "hy": "-", "mi": "\u2212", "-": "-", "bu": "\u2022", "aq": "'", "dq": '"',
    "lq": "\u201c", "rq": "\u201d", "oq": "\u2018", "cq": "\u2019", "co": "\u00a9", "rg": "\u00ae", "tm": "\u2122",
    "pl": "+", "mu": "\u00d7", "di": "\u00f7", "+-": "\u00b1", "<=": "\u2264", ">=": "\u2265", "!=": "\u2260",
    "==": "\u2261", "->": "\u2192", "<-": "\u2190", "<>": "\u2194", "rA": "\u21d2", "lA": "\u21d0", "ua": "\u2191",
    "da": "\u2193", "de": "\u00b0", "sc": "\u00a7", "ps": "\u00b6", "ti": "~", "ha": "^", "rs": "\\", "sl": "/",
    "ba": "|", "br": "|", "or": "|", "ga": "`", "aa": "\u00b4", "12": "\u00bd", "14": "\u00bc", "34": "\u00be",
    "ss": "\u00df", "Fo": "\u00ab", "Fc": "\u00bb", "fo": "\u2039", "fc": "\u203a", "fi": "fi", "fl": "fl",
    "ff": "ff", "Fi": "ffi", "Fl": "ffl", "dg": "\u2020", "dd": "\u2021", "ct": "\u00a2", "Po": "\u00a3",
    "Ye": "\u00a5", "Eu": "\u20ac", "eu": "\u20ac", "at": "@", "sh": "#", "Do": "$", "lB": "[", "rB": "]",
    "lC": "{", "rC": "}", "la": "\u27e8", "ra": "\u27e9", "ru": "_", "ul": "_", "bv": "|", "pc": "\u00b7",
    "md": "\u22c5", "no": "\u00ac", "AN": "\u2227", "OR": "\u2228", "if": "\u221e", "sr": "\u221a", "*a": "\u03b1",
    "*b": "\u03b2", "*g": "\u03b3", "*d": "\u03b4", "*e": "\u03b5", "*l": "\u03bb", "*m": "\u03bc", "*p": "\u03c0",
    "*s": "\u03c3", "*t": "\u03c4", "*f": "\u03c6", "*w": "\u03c9", "*D": "\u0394", "*S": "\u03a3", "*W": "\u03a9",
    "OK": "\u2713", "sq": "\u25a1", "ci": "\u25cb", "lz": "\u25ca", "nb": "\u2284", "ap": "\u223c", "~~": "\u2248",
    "fm": "\u2032", "sd": "\u2033", "ae": "\u00e6", "AE": "\u00c6", "o/": "\u00f8", "O/": "\u00d8", "'e": "\u00e9",
    "'E": "\u00c9", "`e": "\u00e8", ":a": "\u00e4", ":o": "\u00f6", ":u": "\u00fc", ":A": "\u00c4", ":O": "\u00d6",
    ":U": "\u00dc", "'a": "\u00e1", "`a": "\u00e0", "^a": "\u00e2", "'i": "\u00ed", "'o": "\u00f3", "'u": "\u00fa",
    ",c": "\u00e7", "~n": "\u00f1", "~N": "\u00d1", "%0": "\u2030", "es": "\u2205", "mo": "\u2208", "pd": "\u2202",
    "is": "\u222b", "sum": "\u2211", "product": "\u220f", "tno": "\u00ac", "hA": "\u21d4", "uA": "\u21d1",
    "dA": "\u21d3", "an": "\u23af", "radicalex": "\u203e", "rn": "\u203e", "bq": "\u201e", "Bq": "\u201e",
}
# the man macros' predefined strings (\*(lq ...)
STRINGS = {"lq": "\u201c", "rq": "\u201d", "R": "\u00ae", "Tm": "\u2122", "S": "", "HF": "", "Aq": "'", "aq": "'"}
FONT_TAGS = {"B": ("<strong>", "</strong>"), "I": ("<em>", "</em>"), "BI": ("<strong><em>", "</em></strong>"),
             "CW": ("<code>", "</code>")}
_FONT_NAMES = {"B": "B", "I": "I", "R": "R", "BI": "BI", "IB": "BI", "CW": "CW", "C": "CW", "CR": "CW", "CB": "B",
               "CI": "I", "TT": "CW", "CO": "CW", "TB": "B", "TI": "I", "TR": "R", "HB": "B", "HI": "I", "HR": "R",
               "1": "R", "2": "I", "3": "B", "4": "BI", "L": "CW", "M": "CW", "V": "CW"}
_BULLETS = {"\u2022", "*", "-", "\u2212", "o", "+", "\u00b7", "\u2013"}
_IGNORED = {"ad", "na", "in", "ti", "ne", "hy", "nh", "ll", "ps", "vs", "fam", "cs", "ss", "ta", "PD", "IX", "nr",
            "UC", "AT", "hw", "pl", "po", "lt", "mk", "rt", "ev", "fl", "rr", "rm", "rn", "tr", "lf", "cp", "do",
            "mso", "pc", "ns", "rs", "bp", "pn", "wh", "ch", "em", "dt", "it", "itc", "kern", "lg", "uf", "cflags", "nroff", "troff", "warn", "ab", "tm", "tm1", "tmc", "sy", "pso", "open", "close", "write",
            "cu", "ul", "bd", "fp", "ftr", "fschar", "char", "schar", "shc", "hc", "hla", "hlm", "hpf", "hym",
            "hys", "linetabs", "nm", "nn", "sizes", "ls", "ce", "rj", "fc", "lc", "tc", "c2", "cc", "ec", "eo",
            "ig-end", "hcode", "trf", "als", "aln", "chop", "substring", "length", "index", "box", "boxa", "di",
            "da", "sv", "os", "vpt", "brp", "spreadwarn", "special", "fspecial", "PT", "BT", "UN", "LINE", "MR-dummy",
            "PU", "pdfmark", "pdfhref"}


def _unq(s: str) -> list[str]:
    """A request's arguments: space separated, "double quotes" group, "" inside quotes is one quote."""
    args: list[str] = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break
        if s[i] == '"':
            i += 1
            buf = []
            while i < n:
                if s[i] == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                if s[i] == "\\" and i + 1 < n:
                    buf.append(s[i:i + 2])
                    i += 2
                    continue
                buf.append(s[i])
                i += 1
            args.append("".join(buf))
        else:
            j = i
            while j < n and s[j] not in " \t":
                j += 2 if s[j] == "\\" else 1
            args.append(s[i:j])
            i = j
    return args


_ESC = re.compile(r"\\(?:\((..)|\[([^\]]{0,40})\]|f(?:\[([^\]]{0,20})\]|\((..)|(.))|\*(?:\((..)|\[([^\]]{0,40})\]|(.))"
                  r"|[nN](?:\((..)|\[([^\]]{0,40})\]|'([^']{0,20})'|(.))|s(?:[+-]?\d|\([+-]?\d\d|\[[^\]]{0,20}\]|'[^']{0,20}')"
                  r"|[hvwloxbLDZXRSHkmMVYz](?:'[^']{0,200}'|\[[^\]]{0,200}\]|\(..|.)?|(.))", re.DOTALL)


class _Man:
    def __init__(self, path: Path):
        self.base = path.parent
        self.top = path.parent.parent
        self.lost: list[str] = []
        self.unknown: dict[str, int] = {}
        self.strings = dict(STRINGS)
        self.meta = Metadata()
        self.blocks: list[list] = [["body", []]]  # open containers: [kind, parts, extra]
        self.para: list[tuple] = []  # inline pieces of the paragraph being read: ("t", font, text) / ("h", html)
        self.font = "R"
        self.prev_font = "R"
        self.nofill: list[str] | None = None  # lines of a .nf/.EX block
        self.pending: str = ""  # "term" after .TP: the next line is the tag
        self.next_font: str | None = None  # .B with no text: the font of the next line
        self.link: str | None = None
        self.so_stack: list[Path] = [path.resolve()]
        self.lines_read = 0
        self.skip_until: str | None = None  # inside .ig: the line that ends it
        self.macros: dict[str, list[str]] = {}
        self.expanding = 0
        self.expanded = 0
        self.depth = 0  # pages read for table cells
        self.cond_skip = 0  # inside a false conditional's \{ ... \} block: how deep
        self.last_if = True

    # --- input ------------------------------------------------------------------------------------
    def lines(self, text: str, base: Path, depth: int = 0):
        """Input lines, with escaped newlines joined and .so files read in place."""
        raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        i, n = 0, len(raw)
        while i < n:
            line = raw[i]
            i += 1
            while line.endswith("\\") and not line.endswith("\\\\") and i < n:
                line = line[:-1] + raw[i]
                i += 1
            self.lines_read += 1
            if self.lines_read > MAX_LINES:
                self.lost.append("The page is longer than bookport reads; the rest was left out")
                return
            m = re.match(r"^[.']\s*so\s+(\S+)", line)
            if m:
                yield from self.include(m.group(1), base, depth)
                continue
            yield line

    def include(self, name: str, base: Path, depth: int):
        # man-pages name included files from the top of the manual (man3/x.3), others from the page's folder
        candidates = [base, base.parent]
        target = None
        for folder in candidates:
            p = safe_path(folder, name, self.top)
            if p is not None and p.is_file():
                target = p
                break
        if target is None:
            refused = all(safe_path(folder, name, self.top) is None for folder in candidates)
            self.lost.append(f"An include outside the document's folder was left out: {name}" if refused
                             else f"An included file isn't there: {name}")
            return
        if depth >= MAX_SO_DEPTH or target in self.so_stack:
            self.lost.append(f"An include that would repeat itself (or nests too deep) was left out: {name}")
            return
        try:
            data = target.read_bytes()
        except OSError:
            self.lost.append(f"An included file isn't there: {name}")
            return
        self.so_stack.append(target)
        try:
            yield from self.lines(markup.decode(data), target.parent, depth + 1)
        finally:
            self.so_stack.pop()

    # --- the page ---------------------------------------------------------------------------------
    def read(self, text: str) -> str:
        it = self.lines(text, self.base)
        for line in it:
            if self.skip_until is not None:
                if line.strip() in (self.skip_until, ".."):
                    self.skip_until = None
                continue
            if self.cond_skip:
                self.cond_skip = max(self.cond_skip + line.count("\\{") - line.count("\\}"), 0)
                continue
            if line[:1] in (".", "'"):
                self.control(line, it)
            elif self.nofill is not None:
                self.nofill.append(self.inline_html(line))
            elif not line.strip():
                self.end_para()
            elif line.strip() != "\\}":
                self.text_line(line)
        self.end_para()
        self.end_nofill()
        while len(self.blocks) > 1:
            self.close()
        if self.unknown:
            names = ", ".join(sorted(k for k in self.unknown if k))
            if names:
                self.lost.append(f"roff requests bookport doesn't run were left out (their text kept): {names}")
        return "".join(self.blocks[0][1])

    def control(self, line: str, it, depth: int = 0) -> None:
        """A control line: comments, conditionals, macro definitions, then the requests and macros."""
        m = re.match(r"^[.']\s*(\S+)\s*(.*)$", line)
        if not m:
            return  # a lone dot is an empty request
        name, rest = m.group(1), m.group(2)
        if name.startswith(('\\"', "\\#")):
            return
        if name in ("if", "ie", "el"):
            if name == "el":
                ok = not self.last_if
            else:
                ok, rest = _condition(rest)
                if name == "ie":
                    self.last_if = ok
            rest = rest.strip()
            if not ok:
                self.cond_skip = max(rest.count("\\{") - rest.count("\\}"), 0)
                return
            rest = rest.replace("\\{", "").replace("\\}", "").strip()
            if rest and depth < 8:
                if rest[:1] in (".", "'"):
                    self.control(rest, it, depth + 1)
                elif self.nofill is not None:
                    self.nofill.append(self.inline_html(rest))
                else:
                    self.text_line(rest)
            return
        if name == "ig":
            self.skip_until = "." + rest.split()[0] if rest.strip() else ".."
            return
        if name in ("de", "de1", "am", "am1"):
            words = rest.split()
            end = "." + words[1] if len(words) > 1 else ".."
            body = []
            for src in it:
                if src.strip() == end or end != ".." and src.strip() == "..":
                    break
                if len(body) < 10_000:
                    body.append(src)
            if words:
                self.macros[words[0]] = (self.macros.get(words[0], []) if name.startswith("am") else []) + body
            return
        if name in self.macros:
            self.expand(name, rest, it, depth)
            return
        self.request(line, it)

    def expand(self, name: str, rest: str, it, depth: int) -> None:
        """Run a macro the page defined: its lines, with \\$1... replaced by the arguments."""
        if self.expanding >= 8 or self.expanded > 200_000:
            self.lost_once("Macros that call themselves too deeply were stopped")
            return
        args = _unq(rest)

        def arg(m: re.Match) -> str:
            key = m.group(1)
            if key in ("*", "@"):
                return " ".join(args)
            k = int(key)
            return args[k - 1] if 0 < k <= len(args) else ""

        self.expanding += 1
        try:
            for src in self.macros[name]:
                self.expanded += 1
                line = re.sub(r"\\\\?\$([1-9*@])", arg, src).replace("\\\\", "\\")
                if line[:1] in (".", "'"):
                    self.control(line, it, depth + 1)
                elif self.nofill is not None:
                    self.nofill.append(self.inline_html(line))
                elif line.strip():
                    self.text_line(line)
        finally:
            self.expanding -= 1

    def count_unknown(self, name: str) -> None:
        self.unknown[name] = self.unknown.get(name, 0) + 1

    # --- requests ---------------------------------------------------------------------------------
    def request(self, line: str, it) -> None:
        m = re.match(r"^[.']\s*(\S+)\s*(.*)$", line)
        if not m:
            return
        name, rest = m.group(1), m.group(2)
        rest = re.sub(r'(?<!\\)\\".*$', "", rest)
        args = _unq(rest)
        if name in ("SH", "SS"):
            self.end_para()
            self.end_nofill()
            while len(self.blocks) > 1:
                self.close()
            text = " ".join(args)
            if not text:
                text = self._next_text(it)
            level = 1 if name == "SH" else 2
            self.font = "R"
            self.blocks[0][1].append(f"<h{level}>{self.inline_html(text)}</h{level}>")
            return
        if name == "TH":
            if args:
                title = self.plain(args[0])
                self.meta.title = f"{title}({self.plain(args[1])})" if len(args) > 1 and args[1] else title
            if len(args) > 2 and args[2]:
                self.meta.date = self.plain(args[2])
            return
        if name in ("PP", "LP", "P", "HP"):
            if self.nofill is not None:
                self.nofill.append("")  # inside .nf a paragraph macro only leaves a blank line
                return
            self.end_para()
            self.to_container()
            self.font = "R"
            return
        if name in ("TP", "TQ"):
            self.end_para()
            self.end_nofill()
            if name == "TP":
                self.to_list("dl")
            else:
                while self.blocks[-1][0] not in ("dl", "body", "rs", "dd", "li"):
                    self.close()
                if self.blocks[-1][0] == "dd":
                    self.close()
            self.pending = "term"
            return
        if name == "IP":
            self.end_para()
            self.end_nofill()
            tag = self.plain(args[0]).strip() if args else ""
            if not tag:
                if self.blocks[-1][0] not in ("dd", "li"):
                    self.to_container()
                return
            number = re.fullmatch(r"\(?(\d{1,4}|[a-zA-Z]|[ivxlcIVXLC]{1,6})[.)]", tag) or re.fullmatch(r"\d{1,4}", tag)
            kind = "ul" if tag in _BULLETS else "ol" if number else "dl"
            self.to_list(kind, tag)
            if kind == "dl":
                self.blocks[-1][1].append(f"<dt>{self.inline_html(args[0])}</dt>")
                self.open("dd")
            else:
                self.open("li")
            return
        if name == "RS":
            self.end_para()
            self.end_nofill()
            if sum(1 for b in self.blocks if b[0] == "rs") < MAX_RS:
                self.open("rs")
            return
        if name == "RE":
            self.end_para()
            self.end_nofill()
            if any(b[0] == "rs" for b in self.blocks):
                while self.blocks[-1][0] != "rs":
                    self.close()
                self.close()
            return
        if name in ("nf", "EX"):
            self.end_para()
            if self.nofill is None:
                self.nofill = []
            return
        if name in ("fi", "EE"):
            self.end_nofill()
            return
        if name == "br":
            if self.nofill is None:
                self.para.append(("h", "<br/>"))
            return
        if name == "sp":
            if self.nofill is not None:
                self.nofill.append("")
            else:
                self.end_para()
            return
        if name in ("B", "I", "SM", "SB", "BI", "IB", "BR", "RB", "IR", "RI"):
            if not args:
                self.next_font = {"B": "B", "I": "I", "SB": "B", "SM": "R"}.get(name)
                return
            self.text_line(_font_macro(name, args), keep_font=True)
            return
        if name == "ft":
            self.font = _FONT_NAMES.get(args[0] if args else "R", "R") if args and args[0] != "P" else self.prev_font
            return
        if name in ("UR", "MT"):
            self.link = ("mailto:" if name == "MT" else "") + (self.plain(args[0]) if args else "")
            if self.para and self.para[-1][0] != "c":
                self.para.append(("t", self.font, " "))
            self.para.append(("a", self.link))
            return
        if name in ("UE", "ME"):
            if self.link is not None:
                self.para.append(("/a", self.link))
                self.link = None
            if args:
                self.text_line(" ".join(args).lstrip(), joined=True)
            return
        if name == "TS":
            self.end_para()
            self.end_nofill()
            self.add_block(self.table(it))
            return
        if name in ("EQ", "PS", "G1"):
            end = {"EQ": "EN", "PS": "PE", "G1": "G2"}[name]
            body = []
            for src in it:
                if re.match(rf"^[.']\s*{end}\b", src):
                    break
                body.append(src)
            what = {"EQ": "eqn equations", "PS": "pic drawings", "G1": "grap graphs"}[name]
            self.lost_once(f"{what} were kept as their source text: bookport doesn't draw them")
            self.end_para()
            self.add_block(f'<pre class="raw" data-format="{name.lower()}">{esc(chr(10).join(body))}</pre>')
            return
        if name in ("ds", "ds1", "as"):
            if args:
                value = rest[len(rest) - len(rest.lstrip()):].strip()
                value = value[len(args[0]):].lstrip()
                value = value.removeprefix('"')
                self.strings[args[0]] = (self.strings.get(args[0], "") if name == "as" else "") + value
            return
        if name in ("SY", "OP", "YS", "SYNOPSIS"):
            if name == "SY":
                self.end_para()
                self.text_line(f"\\fB{rest}\\fR")
            elif name == "OP" and args:
                self.text_line("[\\fB" + args[0] + "\\fR" + (" \\fI" + " ".join(args[1:]) + "\\fR" if len(args) > 1 else "") + "]")
            elif name == "YS":
                self.end_para()
            return
        if name in ("MR",):
            if args:
                self.text_line(f"\\fI{args[0]}\\fR({args[1] if len(args) > 1 else ''}){''.join(args[2:])}")
            return
        if name in ("Dd", "Dt", "Os", "Sh", "Nm", "Nd", "Pp", "Fl", "Ar", "Op", "It", "Bl", "El"):
            self.lost_once("This page uses the mdoc macros (BSD), which bookport doesn't read: their text was kept")
            self.text_line(rest)
            return
        if name in _IGNORED or name.startswith("\\"):
            return
        self.count_unknown("." + name)
        if rest.strip():
            self.text_line(rest)

    def _next_text(self, it) -> str:
        for line in it:
            if line[:1] in (".", "'"):
                m = re.match(r"^[.']\s*(B|I|BR|IR|SM)\s+(.*)$", line)
                if m:
                    return " ".join(_unq(m.group(2)))
                continue
            return line
        return ""

    # --- containers -------------------------------------------------------------------------------
    def open(self, kind: str, extra: str = "") -> None:
        self.blocks.append([kind, [], extra])

    def close(self) -> None:
        kind, parts, extra = self.blocks.pop()
        inner = "".join(parts)
        parent = self.blocks[-1]
        if kind == "rs":
            html_ = inner if parent[0] in ("dd", "li") or not inner else f"<blockquote>{inner}</blockquote>"
        elif kind == "ol" and extra:
            html_ = f'<ol start="{extra}">{inner}</ol>' if extra != "1" else f"<ol>{inner}</ol>"
        else:
            html_ = f"<{kind}>{inner}</{kind}>" if inner or kind in ("dd", "li") else ""
        parent[1].append(html_)

    def to_container(self) -> None:
        """Back to the innermost container paragraphs go in (a .PP ends tagged paragraphs and lists)."""
        while self.blocks[-1][0] not in ("body", "rs"):
            self.close()

    def to_list(self, kind: str, tag: str = "") -> None:
        """Into a list of this kind at this indent: the open one, or a new one."""
        while True:
            top = self.blocks[-1][0]
            if top in ("dd", "li"):
                self.close()
                continue
            if top == kind:
                return
            if top in ("dl", "ul", "ol"):
                self.close()
                continue
            break
        start = ""
        if kind == "ol":
            m = re.search(r"\d+", tag)
            start = m.group(0) if m else "1"
        self.open(kind, start)

    def add_block(self, html_: str) -> None:
        if self.blocks[-1][0] in ("dl", "ul", "ol"):
            self.to_container()
        self.blocks[-1][1].append(html_)

    # --- text -------------------------------------------------------------------------------------
    def text_line(self, line: str, keep_font: bool = False, joined: bool = False) -> None:
        if self.next_font:
            line = f"\\f{_font_esc(self.next_font)}{line}\\fR"
            self.next_font = None
        if self.nofill is not None:
            self.nofill.append(self.inline_html(line))
            return
        if self.pending == "term":
            self.pending = ""
            self.blocks[-1][1].append(f"<dt>{self.inline_html(line)}</dt>")
            self.open("dd")
            return
        if self.blocks[-1][0] in ("dl", "ul", "ol"):
            self.open("dd" if self.blocks[-1][0] == "dl" else "li")
        if self.para and not joined and self.para[-1][0] not in ("c", "a"):
            if line.startswith((" ", "\t")):
                self.para.append(("h", "<br/>"))
            else:
                self.para.append(("t", self.font, " "))
        if self.para and self.para[-1][0] == "c":
            self.para.pop()
        self.para.extend(self.pieces(line))

    def pieces(self, line: str) -> list[tuple]:
        """A line's text and font changes, escapes resolved."""
        out: list[tuple] = []
        pos = 0
        for m in _ESC.finditer(line):
            if m.start() > pos:
                out.append(("t", self.font, line[pos:m.start()]))
            pos = m.end()
            s = m.group(0)
            two, bracket = m.group(1), m.group(2)
            if two is not None or bracket is not None:
                out.append(("t", self.font, self.char(two if two is not None else bracket)))
                continue
            if s[1] == "f":
                f = m.group(3) if m.group(3) is not None else m.group(4) or m.group(5) or "R"
                if f in ("P", ""):
                    self.font, self.prev_font = self.prev_font, self.font
                else:
                    self.prev_font, self.font = self.font, _FONT_NAMES.get(f, "R")
                continue
            if s[1] == "*":
                name = m.group(6) or m.group(7) or m.group(8) or ""
                value = self.strings.get(name.split()[0] if name else "", "")
                if value:
                    out.extend(self.pieces(value) if "\\" in value and len(value) < 1000 else [("t", self.font, value)])
                continue
            if s[1] in "nN":
                if s[1] == "N":
                    code = m.group(11) or ""
                    if code.isdigit() and int(code) < 0x110000:
                        out.append(("t", self.font, chr(int(code))))
                continue
            c = m.group(13)
            if c is None:
                continue  # sizes, motions and other typesetting escapes
            if c in ("e", "\\"):
                out.append(("t", self.font, "\\"))
            elif c in ("-", ".", "'", "`", "/"):
                out.append(("t", self.font, {"'": "\u00b4", "`": "`"}.get(c, c)))
            elif c in (" ", "~", "0"):
                out.append(("t", self.font, "\u00a0"))
            elif c == "t":
                out.append(("t", self.font, "\t"))
            elif c == "c":
                out.append(("c",))
            elif c == '"':
                break  # a comment: the rest of the line
            elif c == "#":
                break
            elif c in ("&", "|", "^", "%", ":", ",", ")", "!", "{", "}", "a", "d", "u", "r", "p", "E", "\n", "j", "q"):
                continue
            else:
                out.append(("t", self.font, c))
        else:
            if pos < len(line):
                out.append(("t", self.font, line[pos:]))
            return out
        return out

    def char(self, name: str) -> str:
        if name in CHARS:
            return CHARS[name]
        m = re.fullmatch(r"u([0-9A-Fa-f]{4,6})(?:_[0-9A-Fa-f]{4,6})*", name)
        if m and int(m.group(1), 16) < 0x110000:
            return "".join(chr(int(h, 16)) for h in name[1:].split("_"))
        m = re.fullmatch(r"char(\d{1,6})", name)
        if m and int(m.group(1)) < 0x110000:
            return chr(int(m.group(1)))
        self.lost_once(f"Special characters bookport doesn't know were kept as their names, like \\[{name}]")
        return f"[{name}]"

    def plain(self, text: str) -> str:
        saved = self.font
        s = "".join(p[2] for p in self.pieces(text) if p[0] == "t")
        self.font = saved
        return s

    def inline_html(self, text: str) -> str:
        saved = self.font
        html_ = _render(self.pieces(text))
        self.font = saved
        return html_

    def end_para(self) -> None:
        pieces = self.para
        self.para = []
        if self.link is not None:
            pieces.append(("/a", self.link))
            self.link = None
        if not any(p[0] == "t" and p[2].strip() or p[0] in ("a", "h") and p[1] != "<br/>" for p in pieces):
            return
        html_ = _render(pieces).strip()
        html_ = html_.removesuffix("<br/>")
        if self.blocks[-1][0] in ("dl", "ul", "ol"):
            self.open("dd" if self.blocks[-1][0] == "dl" else "li")
        self.blocks[-1][1].append(f"<p>{html_}</p>")

    def end_nofill(self) -> None:
        if self.nofill is None:
            return
        lines = self.nofill
        self.nofill = None
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines:
            self.add_block("<pre>" + "\n".join(lines) + "</pre>")

    def lost_once(self, msg: str) -> None:
        if msg not in self.lost:
            self.lost.append(msg)

    # --- tbl --------------------------------------------------------------------------------------
    def table(self, it) -> str:
        lines = []
        for line in it:
            if re.match(r"^[.']\s*TE\b", line):
                break
            lines.append(line)
        tab = "\t"
        i = 0
        if lines and lines[0].rstrip().endswith(";"):
            m = re.search(r"tab\s*\((.)\)", lines[0])
            if m:
                tab = m.group(1)
            i = 1
        formats, i = _formats(lines, i)
        rows: list[list[str]] = []
        ruled_head = False
        while i < len(lines):
            line = lines[i]
            i += 1
            if re.match(r"^[.']\s*T&", line):
                more, i = _formats(lines, i)
                formats = formats[:len(rows)] + more
                continue
            if line.strip() in ("_", "=", "-"):
                if len(rows) == 1:
                    ruled_head = True  # a rule under the first row: it's the heading
                continue
            if line[:1] in (".", "'"):
                continue
            cells: list[str] = []
            rest: str | None = line
            while rest is not None:
                k = rest.find(tab)
                cell, after = (rest, None) if k < 0 else (rest[:k], rest[k + 1:])
                if cell.startswith("T{"):
                    block = [cell[2:]] if cell[2:].strip() else []
                    while i < len(lines) and not lines[i].startswith("T}"):
                        block.append(lines[i])
                        i += 1
                    end = lines[i] if i < len(lines) else "T}"
                    i += 1
                    cells.append("\n".join(block))
                    tail = end[2:]
                    rest = tail[len(tab):] if tail.startswith(tab) else None
                else:
                    cells.append(cell)
                    rest = after
            rows.append(cells)
        out = []
        for r, row in enumerate(rows):
            fmt = formats[min(r, len(formats) - 1)] if formats else []
            head = r == 0 and len(rows) > 1 and (ruled_head or bool(fmt) and all(_bold(f) or f[:1] in "sS^" for f in fmt))
            cells_html = []
            for c, text in enumerate(row):
                spec = fmt[c] if c < len(fmt) else "l"
                if spec[:1] in ("s", "S", "^"):
                    continue
                span = 1
                while c + span < len(fmt) and fmt[c + span][:1] in ("s", "S"):
                    span += 1
                tag = "th" if head else "td"
                a = f' colspan="{span}"' if span > 1 else ""
                align = {"r": "right", "c": "center"}.get(spec[:1].lower(), "")
                if align:
                    a += f' style="text-align: {align}"'
                cells_html.append(f"<{tag}{a}>{self.cell(text)}</{tag}>")
            if cells_html:
                row_html = "<tr>" + "".join(cells_html) + "</tr>"
                out.append(f"<thead>{row_html}</thead>" if head else row_html)
        return "<table>" + "".join(out) + "</table>" if out else ""

    def cell(self, text: str) -> str:
        if text.strip() in ("_", "=", "\\_", "\\^"):
            return ""
        if self.depth < 4 and re.search(r"(?m)^[.'](PP|LP|P|IP|TP|nf|EX|RS|TS|sp)\b", text):
            # a text block with paragraphs or a listing in it: read it as a little page
            sub = _Man(self.base / "cell")
            sub.top, sub.strings, sub.macros, sub.lost, sub.depth = self.top, self.strings, self.macros, self.lost, self.depth + 1
            inner = sub.read(text)
            if inner.startswith("<p>") and inner.endswith("</p>") and inner.count("<p>") == 1:
                inner = inner[3:-4]
            return inner
        saved = self.font
        parts: list[tuple] = []
        for k, line in enumerate(text.split("\n")):
            if line[:1] in (".", "'"):
                m = re.match(r"^[.']\s*(B|I|BR|IR|RB|RI|BI|IB|SM|SB)\s+(.*)$", line)
                if m:
                    line = _font_macro(m.group(1), _unq(m.group(2)))
                elif re.match(r"^[.']\s*br\b", line):
                    parts.append(("h", "<br/>"))
                    continue
                else:
                    continue
            if k and parts:
                parts.append(("t", self.font, " "))
            parts.extend(self.pieces(line))
        self.font = saved
        return _render(parts).strip()


def _columns(spec: str) -> list[str]:
    """A tbl format line's column keys, each with its modifiers: "rb lw(9n) c" -> ["rb", "lw(9n)", "c"]."""
    cols: list[str] = []
    i, n = 0, len(spec)
    while i < n:
        c = spec[i]
        if c.lower() in "lrcnas^_=":
            cols.append(c)
            i += 1
        elif c in "fF" and cols:
            m = re.match(r"[fF](\([^\s]{2}|\[[^\]]*\]|[A-Z][A-Z]?|\d)", spec[i:])
            step = m.end() if m else 1
            cols[-1] += spec[i:i + step]
            i += step
        elif c in "wWpPvVmM" and cols:
            m = re.match(r".(\([^)]*\)|[\d.]+[a-z]?)?", spec[i:])
            cols[-1] += m.group(0)
            i += m.end()
        elif c.isspace() or c in ",|":
            i += 1
        else:
            if cols:
                cols[-1] += c
            i += 1
    return cols


def _bold(spec: str) -> bool:
    return bool(re.search(r"[bB]|[fF]B|[fF]\(?B", spec[1:]))


def _formats(lines: list[str], i: int) -> tuple[list[list[str]], int]:
    """tbl format lines (one per row, the last ending with a period), from line i on."""
    formats = []
    while i < len(lines):
        spec = lines[i].strip()
        i += 1
        formats.append(_columns(spec.rstrip(".")))
        if spec.endswith("."):
            break
    return formats, i


FONT_MACROS = {"B": ("B",), "I": ("I",), "SM": ("R",), "SB": ("B",), "BI": ("B", "I"), "IB": ("I", "B"),
               "BR": ("B", "R"), "RB": ("R", "B"), "IR": ("I", "R"), "RI": ("R", "I")}


def _font_macro(name: str, args: list[str]) -> str:
    """.B, .BR and the rest as one line of text with font escapes (the two-font ones alternate per word)."""
    fonts = FONT_MACROS[name]
    words = args if len(fonts) > 1 else [" ".join(args)]
    return "".join(f"\\f{_font_esc(fonts[k % len(fonts)])}{w}" for k, w in enumerate(words)) + "\\fR"


def _font_esc(f: str) -> str:
    return f if len(f) == 1 else f"[{f}]"


def _condition(rest: str) -> tuple[bool, str]:
    """A simple .if condition: n (nroff: true), t (false), \\n(.g (groff: true), !-negation; others false."""
    rest = rest.lstrip()
    neg = rest.startswith("!")
    if neg:
        rest = rest[1:]
    m = re.match(r"(n|t|o|e|v|\\n\(\.g|\\n\[\.g\]|'[^']*'[^']*'|\"[^\"]*\"[^\"]*\"|\S+)\s*(.*)$", rest, re.DOTALL)
    if not m:
        return False, ""
    cond, body = m.group(1), m.group(2)
    if cond in ("n", "o"):
        ok = True
    elif cond in ("t", "e", "v"):
        ok = False
    elif cond in ("\\n(.g", "\\n[.g]"):
        ok = True
    elif cond[:1] in ("'", '"'):
        q = cond[0]
        a, _, b = cond[1:-1].partition(q)
        ok = a == b
    else:
        ok = False
    return ok != neg, body


def _render(pieces: list[tuple]) -> str:
    out: list[str] = []
    font = "R"
    for p in pieces:
        kind = p[0]
        if kind == "t":
            if p[1] != font:
                if font in FONT_TAGS:
                    out.append(FONT_TAGS[font][1])
                font = p[1]
                if font in FONT_TAGS:
                    out.append(FONT_TAGS[font][0])
            out.append(esc(p[2]))
        else:
            if font in FONT_TAGS:
                out.append(FONT_TAGS[font][1])
                font = "R"
            if kind == "h":
                out.append(p[1])
            elif kind == "a":
                out.append(f'<a href="{attr(p[1])}">')
            elif kind == "/a":
                out.append("</a>")
    if font in FONT_TAGS:
        out.append(FONT_TAGS[font][1])
    s = "".join(out)
    # an empty link (.UR url / .UE with no text) shows its address
    s = re.sub(r'<a href="([^"]*)">(\s*)</a>', lambda m: f'<a href="{m.group(1)}">{html.unescape(m.group(1)).removeprefix("mailto:")}</a>', s)
    s = re.sub(r"<(strong|em|code)>(\s*)</\1>", r"\2", s)
    return s


def read(path) -> Book:
    p = Path(path)
    m = _Man(p)
    body = m.read(markup.decode(p.read_bytes()))
    return html_book(body, p.name, "man", p.parent, m.lost, m.meta)


# --- writing -------------------------------------------------------------------------------------

_NAMED_OUT = {"\u2014": "\\(em", "\u2013": "\\(en", "\u201c": "\\(lq", "\u201d": "\\(rq", "\u2018": "\\(oq",
              "\u2019": "\\(cq", "\u2022": "\\(bu", "\u00a0": "\\ ", "\u00a9": "\\(co", "\u00ae": "\\(rg",
              "\u2122": "\\(tm", "\u00b0": "\\(de"}


def _roff(s: str, code: bool = False) -> str:
    s = s.replace("\\", "\\e").replace("-", "\\-")
    out = []
    for ch in s:
        if ord(ch) < 128:
            out.append(ch)
        elif ch in _NAMED_OUT and not code:
            out.append(_NAMED_OUT[ch])
        else:
            out.append(f"\\[u{ord(ch):04X}]")
    return "".join(out)


def _quoted(s: str) -> str:
    return '"' + _roff(s).replace('"', "\\(dq") + '"'


def _lines(text: str) -> str:
    """Text lines: none may start with a control character (the writer's own requests are marked with
    a \x01 in front) or with a space (that would break the line)."""
    out = []
    for line in text.split("\n"):
        if line[:1] == "\x01":
            out.append(line[1:])
        elif line[:1] in (".", "'"):
            out.append("\\&" + line)
        elif line[:1] == " ":
            out.append("\\ " + line.lstrip(" "))
        else:
            out.append(line)
    return "\n".join(out)


class _Writer(TextWriter):
    def __init__(self, book: Book):
        super().__init__(book)
        self.fonts: list[str] = []
        self.note_bodies: list[str] = []

    def preamble(self) -> str:
        md = self.book.metadata
        title, section = md.title or "Untitled", ""
        m = re.fullmatch(r"\s*(.+?)\s*\((\w+)\)\s*", title)
        if m:
            title, section = m.group(1), m.group(2)
        return f'.TH {_quoted(title.upper() if m else title)} {_quoted(section)} {_quoted(md.date)} "" ""'

    def postamble(self) -> str:
        if not self.note_bodies:
            return ""
        out = [".SH NOTES"]
        for k, body in enumerate(self.note_bodies, 1):
            out.append(f".IP [{k}] 5\n{body}")
        return "\n".join(out)

    def join_document(self, parts: list[str]) -> str:
        text = "\n".join(p.strip("\n") for p in parts if p.strip())
        # a link or break inside a heading or table cell was folded into one line: there it is plain text
        text = re.sub(r"\\c ?\x01\.(?:UR|MT) (\S+) ?(.*?) ?\x01\.(?:UE|ME) \\c ?", lambda m: (m.group(2) or m.group(1)) + " ",
                      text)
        text = re.sub(r" ?\x01\.br ?", " ", text).replace("\x01", "")
        return re.sub(r"\n{2,}", "\n", text).strip("\n") + "\n"

    def escape(self, s: str) -> str:
        return _roff(s)

    def paragraph(self, text: str, anchor: str | None = None) -> str:
        return ".PP\n" + _lines(text.strip())

    def heading(self, level: int, text: str, anchor: str | None) -> str:
        text = text.replace('"', "\\(dq")
        return f'.SH "{text}"' if level <= 1 else f'.SS "{text}"'

    def rule(self) -> str:
        return ".PP\n.ce\n* * *"

    def code_block(self, code: str, lang: str) -> str:
        # no-fill keeps leading spaces; an empty line would be dropped, so it gets a zero-width \&
        lines = [("\\&" + ln if ln[:1] in (".", "'") else ln) or "\\&" for ln in _roff(code, code=True).split("\n")]
        return ".IP\n.EX\n" + "\n".join(lines) + "\n.EE"

    def raw_block(self, text: str, fmt: str) -> str:
        return text if fmt in ("man", "roff", "ms") else self.code_block(text, "")

    def raw_inline(self, text: str, fmt: str) -> str:
        return text if fmt in ("man", "roff") else self.code(text)

    def quote(self, inner: str) -> str:
        return f".RS\n{inner}\n.RE"

    def _item(self, mark: str, width: int, body: str) -> str:
        blocks = body.strip("\n")
        first, rest = blocks, ""
        first = first.removeprefix(".PP\n")
        m = re.search(r"\n\.(?:PP|IP|TP|RS|EX|TS|SH|SS|ce)\b", "\n" + first)
        if m and m.start() > 0:
            first, rest = ("\n" + first)[1:m.start()], ("\n" + first)[m.start() + 1:]
        elif m:
            first, rest = "", first
        out = f".IP {mark} {width}\n{_lines(first)}" if first else f".IP {mark} {width}"
        if rest.strip():
            out += f"\n.RS {width}\n{rest}\n.RE"
        return out

    def list_block(self, items: list[str], ordered: bool, start: int) -> str:
        out = []
        for k, it in enumerate(items):
            mark = f'"{start + k}."' if ordered else "\\(bu"
            out.append(self._item(mark, 4 if ordered else 2, it))
        return "\n".join(out)

    def list_item(self, li: ET.Element) -> str:
        return self.blocks(li) if self.has_blocks(li) else self.inlines(li).strip()

    def definition_list(self, pairs: list[tuple[str, list[str]]]) -> str:
        out = []
        for term, defs in pairs:
            body = "\n".join(d if d.startswith(".") else _lines(d) for d in defs)
            body = body.removeprefix(".PP\n")
            m = re.search(r"\n\.(?:PP|IP|TP|EX|TS|ce)\b", "\n" + body)
            if m and m.start() > 0:
                first, rest = ("\n" + body)[1:m.start()], ("\n" + body)[m.start() + 1:]
                body = first + "\n.RS\n" + rest + "\n.RE"
            elif m:
                body = ".RS\n" + body + "\n.RE"
            out.append(f".TP\n{_lines(term)}\n{body}".rstrip())
        return "\n".join(out)

    def table(self, table: Table) -> str:
        width = max(table.width, 1)
        fmt_rows = []
        data_rows = []
        busy: dict[tuple[int, int], bool] = {}
        for r, row in enumerate(table.rows):
            fmt = ["l"] * width
            cells = [""] * width
            col = 0
            for c in row:
                while col < width and busy.get((r, col)):
                    fmt[col] = "^"
                    col += 1
                if col >= width:
                    break
                bold = "b" if c.header or r < table.header_rows else ""
                fmt[col] = {"right": "r", "center": "c"}.get(c.align, "l") + bold
                cells[col] = "T{\n" + _lines(c.text.strip()) + "\nT}"
                for dc in range(1, c.colspan):
                    if col + dc < width:
                        fmt[col + dc] = "s"
                for dr in range(c.rowspan):
                    for dc in range(c.colspan):
                        busy[(r + dr, col + dc)] = True
                col += c.colspan
            while col < width:
                if busy.get((r, col)):
                    fmt[col] = "^"
                col += 1
            fmt_rows.append(" ".join(fmt))
            data_rows.append("\t".join(cell for cell, f in zip(cells, fmt, strict=True) if f not in ("s",)))
        out = [".PP", ".TS", "allbox;"]
        out += [f + ("." if k == len(fmt_rows) - 1 else "") for k, f in enumerate(fmt_rows)]
        out += data_rows
        out.append(".TE")
        cap = f".PP\n{_lines(table.caption)}\n" if table.caption else ""
        return cap + "\n".join(out)

    def figure(self, src: str, alt: str, caption: str, anchor: str | None) -> str:
        return ".PP\n" + _lines(f"[{caption or alt or 'picture'}]")

    def image(self, src: str, alt: str) -> str:
        return f"[{alt}]" if alt else ""

    def math_block(self, el: ET.Element) -> str:
        return ".PP\n" + _lines(_roff(mathml.to_tex(el)))

    def math_inline(self, el: ET.Element) -> str:
        return _roff(mathml.to_tex(el))

    def footnote(self, body: str, note_id: str) -> str:
        text = body.strip()
        text = text.removeprefix(".PP\n")
        self.note_bodies.append(text)
        return f"[{len(self.note_bodies)}]"

    def line_break(self) -> str:
        return "\n\x01.br\n"

    def inline(self, el: ET.Element) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        style = {"em": "I", "i": "I", "cite": "I", "dfn": "I", "var": "I", "u": "I", "ins": "I",
                 "strong": "B", "b": "B"}.get(tag)
        if style is None:
            return super().inline(el)
        outer = self._font()
        self.fonts.append(style)
        try:
            inner = self.inlines(el)
        finally:
            self.fonts.pop()
        if not inner.strip():
            return inner
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f"{lead}\\f[{self._font(style)}]{inner.strip()}\\f[{outer}]{trail}"

    def _font(self, extra: str = "") -> str:
        s = set(self.fonts) | ({extra} if extra else set())
        return "BI" if s >= {"B", "I"} else "B" if "B" in s else "I" if "I" in s else "R"

    def code(self, s: str) -> str:
        return f"\\f[CR]{_roff(s, code=True)}\\f[{self._font()}]"

    def link(self, target: str, text: str, external: bool) -> str:
        if not external:
            return text
        mail = target.startswith("mailto:")
        start, end = (".MT " + target[7:], ".ME") if mail else (".UR " + target, ".UE")
        shown = text if text and text not in (_roff(target), _roff(target[7:])) else ""
        return f"\\c\n\x01{start}\n" + (f"{shown}\n" if shown else "") + f"\x01{end} \\c\n"


def write(book: Book, path, pictures: bool = False, **_) -> list[str]:
    return write_text(_Writer(book), path, pictures)

