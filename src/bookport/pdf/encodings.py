"""Character encodings for PDF text: the standard Latin encodings (ISO 32000-1, annex D) and
glyph names to Unicode."""

from __future__ import annotations

import re
import unicodedata

PDFDOC = {
    0x18: "˘", 0x19: "ˇ", 0x1A: "ˆ", 0x1B: "˙", 0x1C: "˝", 0x1D: "˛", 0x1E: "˚",
    0x1F: "˜", 0x80: "•", 0x81: "†", 0x82: "‡", 0x83: "…", 0x84: "—", 0x85: "–",
    0x86: "ƒ", 0x87: "⁄", 0x88: "‹", 0x89: "›", 0x8A: "−", 0x8B: "‰", 0x8C: "„",
    0x8D: "“", 0x8E: "”", 0x8F: "‘", 0x90: "’", 0x91: "‚", 0x92: "™", 0x93: "ﬁ",
    0x94: "ﬂ", 0x95: "Ł", 0x96: "Œ", 0x97: "Š", 0x98: "Ÿ", 0x99: "Ž", 0x9A: "ı",
    0x9B: "ł", 0x9C: "œ", 0x9D: "š", 0x9E: "ž", 0xA0: "€",
}

NAMES = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%", "ampersand": "&",
    "quotesingle": "'", "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-",
    "period": ".", "slash": "/", "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">",
    "question": "?", "at": "@", "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "quoteright": "’", "quoteleft": "‘", "quotedblleft": "“", "quotedblright": "”",
    "quotesinglbase": "‚", "quotedblbase": "„", "endash": "–", "emdash": "—", "bullet": "•",
    "ellipsis": "…", "dagger": "†", "daggerdbl": "‡", "guilsinglleft": "‹", "guilsinglright": "›",
    "guillemotleft": "«", "guillemotright": "»", "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl",
    "trademark": "™", "copyright": "©", "registered": "®", "degree": "°", "section": "§",
    "paragraph": "¶", "periodcentered": "·", "cent": "¢", "sterling": "£", "yen": "¥",
    "Euro": "€", "euro": "€", "florin": "ƒ", "perthousand": "‰", "OE": "Œ", "oe": "œ",
    "AE": "Æ", "ae": "æ", "Oslash": "Ø", "oslash": "ø", "germandbls": "ß", "dotlessi": "ı",
    "Lslash": "Ł", "lslash": "ł", "Eth": "Ð", "eth": "ð", "Thorn": "Þ", "thorn": "þ",
    "minus": "−", "multiply": "×", "divide": "÷", "plusminus": "±", "onehalf": "½",
    "onequarter": "¼", "threequarters": "¾", "ordfeminine": "ª", "ordmasculine": "º",
    "exclamdown": "¡", "questiondown": "¿", "brokenbar": "¦", "logicalnot": "¬", "macron": "¯",
    "acute": "´", "cedilla": "¸", "dieresis": "¨", "mu": "µ", "nbspace": " ",
    "nonbreakingspace": " ", "sfthyphen": "­", "softhyphen": "­", "ring": "˚", "circumflex": "ˆ",
    "tilde": "˜", "caron": "ˇ", "breve": "˘", "dotaccent": "˙", "hungarumlaut": "˝",
    "ogonek": "˛", "fraction": "⁄", "currency": "¤", "onesuperior": "¹", "twosuperior": "²",
    "threesuperior": "³", "dotlessj": "ȷ", "lozenge": "◊", "arrowright": "→", "arrowleft": "←",
    "notequal": "≠", "lessequal": "≤", "greaterequal": "≥", "infinity": "∞", "partialdiff": "∂",
    "summation": "∑", "product": "∏", "pi": "π", "integral": "∫", "Omega": "Ω", "radical": "√",
    "approxequal": "≈", "Delta": "∆", "longs": "ſ", "figuredash": "‒", "quotereversed": "‛",
    "periodsuperior": ".", "commasuperior": ",", "hyphensuperior": "-",
}
_ACCENTS = {"grave": "GRAVE", "acute": "ACUTE", "circumflex": "CIRCUMFLEX", "tilde": "TILDE", "dieresis": "DIAERESIS",
            "ring": "RING ABOVE", "cedilla": "CEDILLA", "caron": "CARON", "macron": "MACRON", "breve": "BREVE",
            "ogonek": "OGONEK", "dotaccent": "DOT ABOVE", "hungarumlaut": "DOUBLE ACUTE", "commaaccent": "COMMA BELOW",
            "slash": "STROKE"}
_GREEK = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega"


def glyph_to_unicode(name: str) -> str | None:
    if not name:
        return None
    if name in NAMES:
        return NAMES[name]
    base = name.split(".", 1)[0]
    if "_" in base and base not in NAMES:
        parts = [glyph_to_unicode(p) for p in base.split("_")]
        return "".join(p for p in parts if p) if all(parts) else None
    if base in NAMES:
        return NAMES[base]
    if len(base) == 1:
        return base
    m = re.fullmatch(r"uni((?:[0-9A-F]{4})+)", base)
    if m:
        h = m.group(1)
        try:
            return "".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h), 4))
        except ValueError:
            return None
    m = re.fullmatch(r"u([0-9A-F]{4,6})", base)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return None
    m = re.fullmatch(r"([A-Za-z])(grave|acute|circumflex|tilde|dieresis|ring|cedilla|caron|macron|breve|ogonek|dotaccent|"
                     r"hungarumlaut|commaaccent|slash)", base)
    if m:
        letter, accent = m.groups()
        case = "CAPITAL" if letter.isupper() else "SMALL"
        try:
            return unicodedata.lookup(f"LATIN {case} LETTER {letter.upper()} WITH {_ACCENTS[accent]}")
        except KeyError:
            if accent == "commaaccent":
                try:
                    return unicodedata.lookup(f"LATIN {case} LETTER {letter.upper()} WITH CEDILLA")
                except KeyError:
                    return None
            return None
    low = base.lower()
    if low in _GREEK.split():
        try:
            return unicodedata.lookup(f"GREEK {'CAPITAL' if base[0].isupper() else 'SMALL'} LETTER {low.upper()}")
        except KeyError:
            return None
    return None  # g12, cid12, glyph12, index12: a glyph number says nothing about the character


def _build(names_by_code: dict[int, str], base: dict[int, str] | None = None) -> dict[int, str]:
    out = dict(base or {})
    for code, name in names_by_code.items():
        u = glyph_to_unicode(name)
        if u:
            out[code] = u
    return out


ASCII = {c: chr(c) for c in range(32, 127)}
STANDARD = dict(ASCII)
STANDARD.update({0x27: "’", 0x60: "‘"})
STANDARD.update(_build({
    0xA1: "exclamdown", 0xA2: "cent", 0xA3: "sterling", 0xA4: "fraction", 0xA5: "yen", 0xA6: "florin", 0xA7: "section",
    0xA8: "currency", 0xA9: "quotesingle", 0xAA: "quotedblleft", 0xAB: "guillemotleft", 0xAC: "guilsinglleft",
    0xAD: "guilsinglright", 0xAE: "fi", 0xAF: "fl", 0xB1: "endash", 0xB2: "dagger", 0xB3: "daggerdbl",
    0xB4: "periodcentered", 0xB6: "paragraph", 0xB7: "bullet", 0xB8: "quotesinglbase", 0xB9: "quotedblbase",
    0xBA: "quotedblright", 0xBB: "guillemotright", 0xBC: "ellipsis", 0xBD: "perthousand", 0xBF: "questiondown",
    0xC1: "grave", 0xC2: "acute", 0xC3: "circumflex", 0xC4: "tilde", 0xC5: "macron", 0xC6: "breve", 0xC7: "dotaccent",
    0xC8: "dieresis", 0xCA: "ring", 0xCB: "cedilla", 0xCD: "hungarumlaut", 0xCE: "ogonek", 0xCF: "caron",
    0xD0: "emdash", 0xE1: "AE", 0xE3: "ordfeminine", 0xE8: "Lslash", 0xE9: "Oslash", 0xEA: "OE", 0xEB: "ordmasculine",
    0xF1: "ae", 0xF5: "dotlessi", 0xF8: "lslash", 0xF9: "oslash", 0xFA: "oe", 0xFB: "germandbls"}))


def _codec(name: str) -> dict[int, str]:
    out = {}
    for c in range(32, 256):
        try:
            ch = bytes([c]).decode(name)
        except UnicodeDecodeError:
            continue
        if ch.isprintable() or ch == " ":
            out[c] = ch
    return out


WINANSI = _codec("cp1252")
WINANSI.update({0x7F: "•", 0x81: "•", 0x8D: "•", 0x8F: "•", 0x90: "•", 0x9D: "•",
                0xA0: " ", 0xAD: "-"})
MACROMAN = _codec("mac_roman")
PDFDOC_FULL = {c: PDFDOC.get(c, chr(c)) for c in range(32, 256) if c not in range(0x7F, 0xA0) or c in PDFDOC}
BASE_ENCODINGS = {"StandardEncoding": STANDARD, "WinAnsiEncoding": WINANSI, "MacRomanEncoding": MACROMAN,
                  "PDFDocEncoding": PDFDOC_FULL, "MacExpertEncoding": STANDARD}
