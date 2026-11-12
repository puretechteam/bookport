"""Invisible text over a page (render mode 3): what a scanned page says, laid over the words in the picture,
so the PDF can be searched and its text copied. The words are in Helvetica, a standard font that isn't
embedded, each stretched over its own box; any character gets a one-byte code in one of as many fonts
as it takes, with a ToUnicode map saying what each code is."""

from __future__ import annotations

from .objects import Name, Ref, Writer


def _num(v: float) -> bytes:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return (s if s not in ("-0", "") else "0").encode()


def to_unicode(text: dict[int, str], width: int) -> bytes:
    """A ToUnicode CMap: each code (width bytes) to its text."""
    lines = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
             b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def", b"1 begincodespacerange",
             b"<%s> <%s>" % (b"00" * width, b"FF" * width), b"endcodespacerange"]
    items = sorted(text.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(b"%d beginbfchar" % len(chunk))
        for code, chars in chunk:
            lines.append(b"<%0*X> <%s>" % (2 * width, code, chars.encode("utf-16-be").hex().upper().encode()))
        lines.append(b"endbfchar")
    lines += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(lines)


class Fonts:
    """Invisible-text fonts: Helvetica (a standard font, not embedded) with 1-byte codes given to
    characters as they come, and a ToUnicode map so the text can be found and copied."""

    PER_FONT = 224

    def __init__(self, w: Writer):
        self.w = w
        self.refs: list[Ref] = []
        self.chars: list[dict[str, int]] = []
        self.where: dict[str, tuple[int, int]] = {}

    def code(self, ch: str) -> tuple[int, int]:
        got = self.where.get(ch)
        if got is None:
            if 32 <= ord(ch) < 127 and (not self.chars or ch not in self.chars[0]) and self._free(0, ord(ch)):
                got = (0, ord(ch))  # plain ASCII keeps its own code in the first font
            else:
                got = self._next()
            self.where[ch] = got
            self.chars[got[0]][ch] = got[1]
        return got

    def _free(self, font: int, code: int) -> bool:
        if not self.chars:
            self._new()
        return code not in self.chars[font].values()

    def _next(self) -> tuple[int, int]:
        if not self.chars:
            self._new()
        for f in range(len(self.chars)):
            used = set(self.chars[f].values())
            if len(used) >= self.PER_FONT + (95 if f == 0 else 0):
                continue
            for c in list(range(161, 256)) + list(range(1, 32)) + list(range(127, 161)) + list(range(32, 127)):
                if c not in used and (f > 0 or not 32 <= c < 127):
                    return f, c
        self._new()
        return len(self.chars) - 1, 161

    def _new(self) -> None:
        self.chars.append({})
        self.refs.append(self.w.reserve())

    def finish(self) -> None:
        for ref, chars in zip(self.refs, self.chars):
            if not chars:
                self.w.put(ref, {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")})
                continue
            codes = sorted(chars.values())
            first, last = codes[0], codes[-1]
            diffs: list = []
            by_code = {c: ch for ch, c in chars.items()}
            prev = None
            for c in codes:
                if prev is None or c != prev + 1:
                    diffs.append(c)
                diffs.append(Name(f"g{c}"))
                prev = c
            tu = self.w.stream(to_unicode(by_code, 1))
            self.w.put(ref, {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
                             "FirstChar": first, "LastChar": last, "Widths": [500] * (last - first + 1),
                             "Encoding": {"Type": Name("Encoding"), "Differences": diffs}, "ToUnicode": tu})


def text_ops(lines, fonts: Fonts, used: set[int], prefix: str = "T") -> bytes:
    """Invisible words (render mode 3), each stretched over its box: lines of (word, x0, y0, x1, y1).
    The fonts are named prefix + their number in the page's resources; used gets the numbers used."""
    if not lines:
        return b""
    out = [b"BT 3 Tr\n"]
    cur_font = None
    cur_size = None
    for line in lines:
        for k, (word, x0, y0, x1, y1) in enumerate(line):
            h = max(1.0, y1 - y0)
            word_chars = word + (" " if k + 1 < len(line) else "")
            codes = [fonts.code(ch) for ch in word_chars]
            n = len(word)
            sx = max(0.01, (x1 - x0) / (n * 0.5 * h)) if n else 1.0
            # split the word where its characters fall in different fonts
            parts: list[tuple[int, bytearray]] = []
            for f, c in codes:
                if not parts or parts[-1][0] != f:
                    parts.append((f, bytearray()))
                parts[-1][1].append(c)
            out.append(b"%s 0 0 1 %s %s Tm\n" % (_num(sx), _num(x0), _num(y0 + 0.21 * h)))
            for f, data in parts:
                used.add(f)
                if (f, h) != (cur_font, cur_size):
                    out.append(b"/%s%d %s Tf\n" % (prefix.encode(), f, _num(h)))
                    cur_font, cur_size = f, h
                out.append(b"<" + data.hex().encode() + b"> Tj\n")
    out.append(b"ET\n")
    return b"".join(out)
