"""The PDF side: a page (or glyph, pattern cell) is a content stream built as the program paints, and the document
gathers pages, pictures and fonts. Paths go out in device space; strokes and shadings are wrapped in ``q cm ... Q``
with the CTM they were painted under, since line widths, dashes and shadings live in user space.

PDF can only take a clip away with Q, so the clip is kept lazily: before each mark the content stream's clip is
brought to the graphics state's (closing q levels that clip too much, opening one with the clips that are new)."""

from __future__ import annotations

import hashlib
import zlib

from ..pdf.objects import Name, Ref, Writer
from .gstate import IDENTITY


def num(v: float) -> bytes:
    if v == int(v) and -1e9 < v < 1e9:
        return b"%d" % int(v)
    s = b"%.4f" % v
    s = s.rstrip(b"0").rstrip(b".")
    return s if s not in (b"-0", b"") else b"0"


def nums(*vs) -> bytes:
    return b" ".join(num(v) for v in vs)


def path_bytes(path: list) -> bytes:
    out = []
    for seg in path:
        op = seg[0]
        if op == "t":  # glyphs from charpath go out as text
            continue
        if op == "l":
            out.append(b"%s %s l" % (num(seg[1]), num(seg[2])))
        elif op == "c":
            out.append(nums(*seg[1:]) + b" c")
        elif op == "m":
            out.append(b"%s %s m" % (num(seg[1]), num(seg[2])))
        else:
            out.append(b"h")
    return b"\n".join(out)


def color_ops(key, stroke: bool) -> bytes:
    kind = key[0]
    if kind == "g":
        return num(key[1][0]) + (b" G" if stroke else b" g")
    if kind == "rgb":
        return nums(*key[1]) + (b" RG" if stroke else b" rg")
    if kind == "cmyk":
        return nums(*key[1]) + (b" K" if stroke else b" k")
    raise ValueError(kind)


class Content:
    """One content stream and the resources it uses."""

    def __init__(self, doc: Document, mask: bool = False, shared: tuple | None = None):
        self.doc = doc
        self.mask = mask  # a Type 3 glyph cached with setcachedevice: colours are the text's, not its own
        self.shared = shared  # (resources, names) shared by the glyphs of one Type 3 font, which has one Resources
        self.reset()

    def reset(self) -> None:
        """Empty again (the page after showpage or erasepage: graphics states saved earlier still point here)."""
        self.ops: list[bytes] = []
        if self.shared is not None:
            self.res, self._names = self.shared
        else:
            self.res: dict[str, dict[str, Ref]] = {}
            self._names: dict[tuple[str, int], str] = {}
        self.levels: list[tuple] = []  # (clip, fill, stroke) for each q opened
        self.clip: tuple = ()
        self.fill_key = ("g", (0,))
        self.stroke_key = ("g", (0,))
        self.marks = False

    # ---- resources
    def name(self, cat: str, ref: Ref, prefix: str) -> str:
        k = (cat, ref.num)
        n = self._names.get(k)
        if n is None:
            d = self.res.setdefault(cat, {})
            n = f"{prefix}{len(d) + 1}"
            d[n] = ref
            self._names[k] = n
        return n

    # ---- state
    def sync(self, clip: tuple) -> None:
        cur = self.clip
        if cur is clip:
            return
        while not _prefix(cur, clip):
            self.ops.append(b"Q")
            cur, self.fill_key, self.stroke_key = self.levels.pop()
        if len(cur) < len(clip):
            self.ops.append(b"q")
            self.levels.append((cur, self.fill_key, self.stroke_key))
            for path, evenodd in clip[len(cur):]:
                glyphs = [s for s in path if s[0] == "t"]
                shape = [s for s in path if s[0] != "t"]
                if glyphs and not any(s[0] in ("l", "c") for s in shape):
                    shape = []  # only the points charpath moved to: the glyphs are the clip
                if shape or not glyphs:  # (an empty path: nothing at all is inside)
                    self.ops.append((path_bytes(shape) if shape else b"0 0 m h") + (b" W* n" if evenodd else b" W n"))
                for s in glyphs:  # charpath then clip: the glyphs' outlines, by text render mode 7
                    self.ops.append(self._text_ops(s[1], s[2], 7, b""))
        self.clip = clip

    def _color(self, key, stroke: bool) -> bytes:
        if key[0] != "pattern":
            return color_ops(key, stroke)
        _, ref, base = key  # ("pattern", pattern, None or the colour of an uncoloured one)
        pname = self.name("Pattern", ref, "P").encode()
        if base is None:
            return b"/Pattern " + (b"CS /" if stroke else b"cs /") + pname + (b" SCN" if stroke else b" scn")
        family = {"g": "DeviceGray", "rgb": "DeviceRGB", "cmyk": "DeviceCMYK"}[base[0]]
        csname = self.name("ColorSpace", self.doc.pattern_space(family), "CS").encode()
        return (b"/" + csname + (b" CS " if stroke else b" cs ") + nums(*base[1]) + b" /" + pname
                + (b" SCN" if stroke else b" scn"))

    def set_fill(self, key) -> None:
        if key is None or self.mask:
            return
        if key != self.fill_key:
            self.ops.append(self._color(key, False))
            self.fill_key = key

    def set_stroke(self, key) -> None:
        if key is None or self.mask:
            return
        if key != self.stroke_key:
            self.ops.append(self._color(key, True))
            self.stroke_key = key

    # ---- marks
    def fill(self, path: list, evenodd: bool, clip: tuple, color) -> None:
        if not path:
            return
        self.sync(clip)
        self.set_fill(color)
        self.ops.append(path_bytes(path) + (b" f*" if evenodd else b" f"))
        self.marks = True

    def stroke(self, user_path: list, ctm, style: bytes, clip: tuple, color) -> None:
        if not user_path:
            return
        self.sync(clip)
        self.set_stroke(color)
        self.ops.append(b"q " + nums(*ctm) + b" cm " + style + b"\n" + path_bytes(user_path) + b" S Q")
        self.marks = True

    def image(self, xobj: Ref, matrix, clip: tuple, color=None) -> None:
        self.sync(clip)
        if color is not None:
            self.set_fill(color)
        n = self.name("XObject", xobj, "Im")
        self.ops.append(b"q " + nums(*matrix) + b" cm /" + n.encode() + b" Do Q")
        self.marks = True

    def shading(self, sh: Ref, ctm, clip: tuple) -> None:
        self.sync(clip)
        n = self.name("Shading", sh, "Sh")
        self.ops.append(b"q " + nums(*ctm) + b" cm /" + n.encode() + b" sh Q")
        self.marks = True

    def text(self, font: Ref, runs: list, clip: tuple, color, mode: int = 0, style: bytes = b"") -> None:
        """runs: [(text matrix, [bytes or number adjustments...])], each shown with a TJ."""
        self.sync(clip)
        if mode in (0, 2, 4, 6):
            self.set_fill(color)
        if mode in (1, 2, 5, 6):
            self.set_stroke(color)
        self.ops.append(self._text_ops(font, runs, mode, style))
        self.marks = True

    def _text_ops(self, font: Ref, runs: list, mode: int, style: bytes) -> bytes:
        n = self.name("Font", font, "F")
        out = [b"BT /" + n.encode() + b" 1 Tf"]
        if mode:
            out.append(b"%d Tr" % mode)
        if style:
            out.append(style)
        for tm, items in runs:
            parts = []
            for it in items:
                parts.append(num(it) if isinstance(it, (int, float)) else b"<" + it.hex().encode() + b">")
            out.append(nums(*tm) + b" Tm [" + b" ".join(parts) + b"] TJ")
        out.append(b"ET")
        return b"\n".join(out)

    def raw(self, data: bytes) -> None:
        self.ops.append(data)
        self.marks = True

    def data(self) -> bytes:
        tail = [b"Q"] * len(self.levels)
        return b"\n".join(self.ops + tail) + b"\n"

    def resources(self) -> dict:
        return {cat: {k: v for k, v in d.items()} for cat, d in self.res.items()}


def _prefix(cur: tuple, clip: tuple) -> bool:
    if len(cur) > len(clip):
        return False
    for a, b in zip(cur, clip):
        if a is not b:
            return False
    return True


class Document:
    """The PDF being made: pages in order, pictures (shared when they're the same), fonts written at the end."""

    def __init__(self):
        self.w = Writer()
        self.pages_ref = self.w.reserve()
        self.pages: list[Ref] = []
        self.images: dict[bytes, Ref] = {}
        self.fonts = []  # objects with finish(doc) (fonts.py)
        self.notes: list[str] = []
        self.info: dict[str, str] = {}
        self._pattern_spaces: dict[str, Ref] = {}

    def pattern_space(self, family: str) -> Ref:
        ref = self._pattern_spaces.get(family)
        if ref is None:
            ref = self._pattern_spaces[family] = self.w.add([Name("Pattern"), Name(family)])
        return ref

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def image(self, data: bytes, d: dict, compress: bool = True) -> Ref:
        h = hashlib.sha1(repr(sorted((k, repr(v)) for k, v in d.items())).encode() + data).digest()
        ref = self.images.get(h)
        if ref is None:
            extra = dict(d)
            if compress and "Filter" not in extra:
                packed = zlib.compress(data, 6)
                extra["Filter"] = Name("FlateDecode")
                ref = self.w.stream(packed, {"Type": Name("XObject"), "Subtype": Name("Image"), **extra}, compress=False)
            else:
                ref = self.w.stream(data, {"Type": Name("XObject"), "Subtype": Name("Image"), **extra}, compress=False)
            self.images[h] = ref
        return ref

    def add_page(self, content: Content, size: tuple[float, float]) -> None:
        stream = self.w.stream(content.data())
        page = {"Type": Name("Page"), "Parent": self.pages_ref,
                "MediaBox": [0, 0, _round(size[0]), _round(size[1])],
                "Resources": content.resources(), "Contents": stream}
        self.pages.append(self.w.add(page))

    def finish(self) -> bytes:
        for f in self.fonts:
            f.finish(self)
        w = self.w
        w.put(self.pages_ref, {"Type": Name("Pages"), "Kids": self.pages, "Count": len(self.pages)})
        root = w.add({"Type": Name("Catalog"), "Pages": self.pages_ref})
        info = {"Producer": "bookport"}
        info.update({k: v for k, v in self.info.items() if v})
        info_ref = w.add(info)
        fid = hashlib.md5(b"".join(o or b"" for o in w.objects[1:50])).digest()
        return w.finish(root, info_ref, file_id=fid, compact=True)


def _round(v: float):
    r = round(v, 3)
    return int(r) if r == int(r) else r


__all__ = ["IDENTITY", "Content", "Document", "num", "nums", "path_bytes"]
