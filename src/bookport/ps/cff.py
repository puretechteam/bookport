"""CFF font sets (Adobe TN 5176) as PostScript gets them through the FontSetInit procset's StartData: read enough to
know each font's glyph names, encoding and widths (Type 2 charstrings, TN 5177), and keep the bytes to embed as
the PDF's FontFile3 (Type1C)."""

from __future__ import annotations

import struct

_ASCII = ["space", "exclam", "quotedbl", "numbersign", "dollar", "percent", "ampersand", "quoteright", "parenleft", "parenright", "asterisk", "plus", "comma", "hyphen", "period", "slash", "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "colon", "semicolon", "less", "equal", "greater", "question", "at", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "bracketleft", "backslash", "bracketright", "asciicircum", "underscore", "quoteleft", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "braceleft", "bar", "braceright", "asciitilde"]
_LATIN = ["exclamdown", "cent", "sterling", "fraction", "yen", "florin", "section", "currency", "quotesingle", "quotedblleft", "guillemotleft", "guilsinglleft", "guilsinglright", "fi", "fl", "endash", "dagger", "daggerdbl", "periodcentered", "paragraph", "bullet", "quotesinglbase", "quotedblbase", "quotedblright", "guillemotright", "ellipsis", "perthousand", "questiondown", "grave", "acute", "circumflex", "tilde", "macron", "breve", "dotaccent", "dieresis", "ring", "cedilla", "hungarumlaut", "ogonek", "caron", "emdash", "AE", "ordfeminine", "Lslash", "Oslash", "OE", "ordmasculine", "ae", "dotlessi", "lslash", "oslash", "oe", "germandbls", "onesuperior", "logicalnot", "mu", "trademark", "Eth", "onehalf", "plusminus", "Thorn", "onequarter", "divide", "brokenbar", "degree", "thorn", "threequarters", "twosuperior", "registered", "minus", "eth", "multiply", "threesuperior", "copyright", "Aacute", "Acircumflex", "Adieresis", "Agrave", "Aring", "Atilde", "Ccedilla", "Eacute", "Ecircumflex", "Edieresis", "Egrave", "Iacute", "Icircumflex", "Idieresis", "Igrave", "Ntilde", "Oacute", "Ocircumflex", "Odieresis", "Ograve", "Otilde", "Scaron", "Uacute", "Ucircumflex", "Udieresis", "Ugrave", "Yacute", "Ydieresis", "Zcaron", "aacute", "acircumflex", "adieresis", "agrave", "aring", "atilde", "ccedilla", "eacute", "ecircumflex", "edieresis", "egrave", "iacute", "icircumflex", "idieresis", "igrave", "ntilde", "oacute", "ocircumflex", "odieresis", "ograve", "otilde", "scaron", "uacute", "ucircumflex", "udieresis", "ugrave", "yacute", "ydieresis", "zcaron"]
_EXPERT = ("exclamsmall Hungarumlautsmall dollaroldstyle dollarsuperior ampersandsmall Acutesmall parenleftsuperior "
           "parenrightsuperior twodotenleader onedotenleader zerooldstyle oneoldstyle twooldstyle threeoldstyle "
           "fouroldstyle fiveoldstyle sixoldstyle sevenoldstyle eightoldstyle nineoldstyle commasuperior "
           "threequartersemdash periodsuperior questionsmall asuperior bsuperior centsuperior dsuperior esuperior "
           "isuperior lsuperior msuperior nsuperior osuperior rsuperior ssuperior tsuperior ff ffi ffl "
           "parenleftinferior parenrightinferior Circumflexsmall hyphensuperior Gravesmall "
           + " ".join(c + "small" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ") +
           " colonmonetary onefitted rupiah Tildesmall exclamdownsmall centoldstyle Lslashsmall Scaronsmall "
           "Zcaronsmall Dieresissmall Brevesmall Caronsmall Dotaccentsmall Macronsmall figuredash hypheninferior "
           "Ogoneksmall Ringsmall Cedillasmall questiondownsmall oneeighth threeeighths fiveeighths seveneighths "
           "onethird twothirds zerosuperior foursuperior fivesuperior sixsuperior sevensuperior eightsuperior "
           "ninesuperior zeroinferior oneinferior twoinferior threeinferior fourinferior fiveinferior sixinferior "
           "seveninferior eightinferior nineinferior centinferior dollarinferior periodinferior commainferior "
           "Agravesmall Aacutesmall Acircumflexsmall Atildesmall Adieresissmall Aringsmall AEsmall Ccedillasmall "
           "Egravesmall Eacutesmall Ecircumflexsmall Edieresissmall Igravesmall Iacutesmall Icircumflexsmall "
           "Idieresissmall Ethsmall Ntildesmall Ogravesmall Oacutesmall Ocircumflexsmall Otildesmall Odieresissmall "
           "OEsmall Oslashsmall Ugravesmall Uacutesmall Ucircumflexsmall Udieresissmall Yacutesmall Thornsmall "
           "Ydieresissmall 001.000 001.001 001.002 001.003 Black Bold Book Light Medium Regular Roman "
           "Semibold").split()
STANDARD_STRINGS = [".notdef", *_ASCII, *_LATIN, *_EXPERT]


class CFFError(ValueError):
    pass


def _index(data: bytes, pos: int) -> tuple[list[bytes], int]:
    if pos + 2 > len(data):
        raise CFFError("INDEX past the end")
    count = struct.unpack(">H", data[pos:pos + 2])[0]
    if count == 0:
        return [], pos + 2
    size = data[pos + 2]
    if not 1 <= size <= 4:
        raise CFFError("bad INDEX offset size")
    base = pos + 3
    offs = [int.from_bytes(data[base + i * size:base + (i + 1) * size], "big") for i in range(count + 1)]
    start = base + (count + 1) * size - 1
    items = [data[start + offs[i]:start + offs[i + 1]] for i in range(count)]
    return items, start + offs[-1]


def _real(data: bytes, i: int) -> tuple[float, int]:
    s = ""
    while i < len(data):
        b = data[i]
        i += 1
        for nib in (b >> 4, b & 15):
            if nib == 15:
                try:
                    return float(s or "0"), i
                except ValueError:
                    return 0.0, i
            s += "0123456789.EE?-?"[nib] if nib != 12 else "E-"
    return 0.0, i


def _dict(data: bytes) -> dict:
    out, ops = {}, []
    i = 0
    while i < len(data):
        b = data[i]
        if b <= 21:
            if b == 12:
                key = 1200 + data[i + 1] if i + 1 < len(data) else 1200
                i += 2
            else:
                key = b
                i += 1
            out[key] = ops
            ops = []
        elif b == 28:
            ops.append(struct.unpack(">h", data[i + 1:i + 3])[0])
            i += 3
        elif b == 29:
            ops.append(struct.unpack(">i", data[i + 1:i + 5])[0])
            i += 5
        elif b == 30:
            v, i = _real(data, i + 1)
            ops.append(v)
        elif 32 <= b <= 246:
            ops.append(b - 139)
            i += 1
        elif 247 <= b <= 250:
            ops.append((b - 247) * 256 + data[i + 1] + 108)
            i += 2
        elif 251 <= b <= 254:
            ops.append(-(b - 251) * 256 - data[i + 1] - 108)
            i += 2
        else:
            i += 1
    return out


class Font:
    """One font of a set: its name, glyph names (by glyph id), code->name encoding and the Private values."""

    def __init__(self, data: bytes, name: str, top: dict, strings: list[bytes], gsubrs: list[bytes]):
        self.data, self.name = data, name
        self.matrix = tuple(float(v) for v in top.get(1207, [0.001, 0, 0, 0.001, 0, 0]))
        self.bbox = [float(v) for v in top.get(5, [0, 0, 0, 0])]
        self.cid = 1230 in top
        cs_off = top.get(17, [0])[0]
        self.charstrings, _ = _index(data, cs_off)
        n = len(self.charstrings)
        self.strings = strings
        self.names = self._charset(top.get(15, [0])[0], n)
        self.encoding = {} if self.cid else self._encoding(top.get(16, [0])[0])
        size, off = (top.get(18) or [0, 0])[:2] if len(top.get(18, [])) >= 2 else (0, 0)
        priv = _dict(data[off:off + size]) if size else {}
        self.default_width = float((priv.get(20) or [0])[0])
        self.nominal_width = float((priv.get(21) or [0])[0])
        self.gsubrs = gsubrs
        self.subrs = _index(data, off + int(priv[19][0]))[0] if priv.get(19) else []

    def sid(self, s: int) -> str:
        if s < len(STANDARD_STRINGS):
            return STANDARD_STRINGS[s]
        k = s - len(STANDARD_STRINGS)
        return self.strings[k].decode("latin-1") if k < len(self.strings) else f"sid{s}"

    def _charset(self, off: int, n: int) -> list[str]:
        if off in (0, 1, 2) or self.cid and off < 3:
            return [self.sid(i) if not self.cid else f"cid{i}" for i in range(n)] if off == 0 else \
                [".notdef"] + [f"g{i}" for i in range(1, n)]
        d = self.data
        fmt = d[off]
        names = [".notdef"]
        i = off + 1
        while len(names) < n and i < len(d):
            if fmt == 0:
                v = struct.unpack(">H", d[i:i + 2])[0]
                names.append(f"cid{v}" if self.cid else self.sid(v))
                i += 2
            else:
                first = struct.unpack(">H", d[i:i + 2])[0]
                left = d[i + 2] if fmt == 1 else struct.unpack(">H", d[i + 2:i + 4])[0]
                i += 3 if fmt == 1 else 4
                for k in range(left + 1):
                    if len(names) >= n:
                        break
                    names.append(f"cid{first + k}" if self.cid else self.sid(first + k))
        while len(names) < n:
            names.append(f"g{len(names)}")
        return names

    def _encoding(self, off: int) -> dict[int, str]:
        from .fonts import STANDARD
        if off in (0, 1):
            names = set(self.names)
            return {c: n for c, n in enumerate(STANDARD) if n in names and n != ".notdef"}
        d = self.data
        fmt = d[off]
        enc: dict[int, str] = {}
        i = off + 1
        if fmt & 0x7F == 0:
            k = d[i]
            for g in range(1, k + 1):
                if g < len(self.names):
                    enc[d[i + g]] = self.names[g]
            i += 1 + k
        else:
            nr = d[i]
            i += 1
            g = 1
            for _ in range(nr):
                first, left = d[i], d[i + 1]
                i += 2
                for c in range(first, first + left + 1):
                    if g < len(self.names):
                        enc[c] = self.names[g]
                    g += 1
        if fmt & 0x80:
            ns = d[i]
            i += 1
            for _ in range(ns):
                code, s = d[i], struct.unpack(">H", d[i + 1:i + 3])[0]
                enc[code] = self.sid(s)
                i += 3
        return enc

    def width(self, gid: int) -> float:
        """The advance from the charstring's optional first number (TN 5177, 3.1)."""
        if not 0 <= gid < len(self.charstrings):
            return self.default_width
        stack: list = []
        got = self._first_op(self.charstrings[gid], stack, 0)
        if got is None:
            return self.default_width
        b = got
        if b in (1, 3, 18, 23, 19, 20):  # stems and masks: pairs
            odd = len(stack) % 2 == 1
        elif b == 21:
            odd = len(stack) > 2
        elif b in (4, 22):
            odd = len(stack) > 1
        elif b == 14:
            odd = len(stack) in (1, 5)
        else:
            return self.default_width
        return self.nominal_width + stack[0] if odd and stack else self.default_width

    @staticmethod
    def _bias(subrs: list) -> int:
        n = len(subrs)
        return 107 if n < 1240 else 1131 if n < 33900 else 32768

    def _first_op(self, cs: bytes, stack: list, depth: int):
        """Run a charstring up to its first operator that isn't a subroutine call; that operator (or None)."""
        if depth > 10:
            return None
        i = 0
        while i < len(cs):
            b = cs[i]
            if b in (10, 29):  # callsubr, callgsubr
                subrs = self.subrs if b == 10 else self.gsubrs
                if not stack:
                    return None
                k = int(stack.pop()) + self._bias(subrs)
                if not 0 <= k < len(subrs):
                    return None
                got = self._first_op(subrs[k], stack, depth + 1)
                if got != 11:  # the subroutine didn't just return
                    return got
                i += 1
                continue
            if b == 11:  # return
                return 11
            if b >= 32 or b == 28:
                if b == 28:
                    stack.append(struct.unpack(">h", cs[i + 1:i + 3])[0])
                    i += 3
                elif b <= 246:
                    stack.append(b - 139)
                    i += 1
                elif b <= 250:
                    stack.append((b - 247) * 256 + cs[i + 1] + 108)
                    i += 2
                elif b <= 254:
                    stack.append(-(b - 251) * 256 - cs[i + 1] - 108)
                    i += 2
                else:
                    stack.append(struct.unpack(">i", cs[i + 1:i + 5])[0] / 65536)
                    i += 5
                if len(stack) > 48:
                    return None
                continue
            return b
        return None


def read(data: bytes) -> list[Font]:
    if len(data) < 4 or data[0] != 1:
        raise CFFError("not a CFF font set")
    pos = data[2]
    names, pos = _index(data, pos)
    tops, pos = _index(data, pos)
    strings, pos = _index(data, pos)
    gsubrs, pos = _index(data, pos)
    out = []
    for name, top in zip(names, tops):
        if not name or name[0] == 0:
            continue
        out.append(Font(data, name.decode("latin-1"), _dict(top), strings, gsubrs))
    return out
