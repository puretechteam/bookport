"""Font and text operators (PLRM 5): findfont, definefont, makefont and the show family, written to the PDF as text
in the fonts of fonts.py, each glyph where PostScript would have drawn it."""

from __future__ import annotations

from struct import error as struct_error

from . import fonts as FT
from .device import Content
from .gstate import IDENTITY, apply, dapply, invert, mul
from .interp import Interp, pop_bool, pop_int, pop_num, pop_proc
from .objects import FontID, Name, PSArray, PSDict, PSError, PSString, XName, key

OPS: dict[str, object] = {}


def op(*names):
    def reg(fn):
        for n in names or (fn.__name__.rstrip("_"),):
            OPS[n] = fn
        return fn
    return reg


def _matrix(v) -> tuple:
    if type(v) is PSArray and v.length == 6:
        vals = v.list()
        if all(type(x) in (int, float) for x in vals):
            return tuple(float(x) for x in vals)
    raise PSError("invalidfont", "FontMatrix")


def _encoding_array(names: list[str]) -> PSArray:
    return PSArray([Name(n) for n in names])


def setup(I: Interp) -> None:
    I.std_fonts = {}
    I.encodings = {"StandardEncoding": _encoding_array(FT.STANDARD),
                   "ISOLatin1Encoding": _encoding_array(FT.ISOLATIN1)}
    for n, arr in I.encodings.items():
        I.systemdict.d[Name(n)] = arr
        I.resources.setdefault("Encoding", {})[Name(n)] = arr
    I.known_font = lambda name: str(name) in FT.CORE14  # findfont finds any name (drawn with a standard font)


def standard_font(I, name: str) -> PSDict:
    """A font dictionary for a font that isn't in the program: one of the standard 14 by its metrics."""
    got = I.std_fonts.get(name)
    if got is not None:
        return got
    base, exact = FT.standard_for(name)
    if not exact:
        I.doc.note(f"{name} isn't in the file, so it was drawn with {base}")
    a = FT.afm(base)
    if a.symbolic:
        enc = [".notdef"] * 256
        for c, n in a.builtin.items():
            if 0 <= c < 256:
                enc[c] = n
        encoding = _encoding_array(enc)
    else:
        encoding = I.encodings["StandardEncoding"]
    cs = PSDict({Name(n): None for n in a.widths})
    d = PSDict({Name("FontType"): 1, Name("FontName"): Name(name), Name("PaintType"): 0,
                Name("FontMatrix"): PSArray([0.001, 0.0, 0.0, 0.001, 0.0, 0.0]),
                Name("FontBBox"): PSArray(list(a.bbox)), Name("Encoding"): encoding, Name("CharStrings"): cs,
                Name("BookportStandard"): Name(base)}, 20)
    d.d[Name("FID")] = FontID(_program(I, d))
    I.std_fonts[name] = d
    return d


def _program(I, fd: PSDict):
    """What a font dictionary is, shared by fonts made from the same glyphs (a re-encoded copy, say)."""
    d = fd.d
    ft = d.get("FontType", 1)
    progs = I.font_programs
    if "BookportStandard" in d:
        base = str(d["BookportStandard"])
        k = ("std", base)
        if k not in progs:
            progs[k] = FT.Standard(fd, base)
        return progs[k]
    if ft == 1 and type(d.get("CharStrings")) is PSDict:
        k = ("t1", id(d["CharStrings"].d))
        if k not in progs:
            progs[k] = FT.Type1(fd)
            I.keep.append(d["CharStrings"])
        return progs[k]
    if ft == 42 and type(d.get("sfnts")) is PSArray:
        k = ("t42", id(d["sfnts"].items))
        if k not in progs:
            p = FT.Type42(fd)
            if p.font is None:
                I.doc.note(f"The TrueType font {p.name} couldn't be read, so its text was drawn with Helvetica")
                return _program(I, standard_font(I, "Helvetica"))
            progs[k] = p
            I.keep.append(d["sfnts"])
        return progs[k]
    if ft == 3:
        return FT.Type3(fd)
    if ft == 2 and isinstance(d.get("BookportCFF"), tuple):
        data, font = d["BookportCFF"]
        k = ("cff", id(data), font.name)
        if k not in progs:
            progs[k] = FT.CFF(fd, data, font)
            I.keep.append(data)
        return progs[k]
    if ft == 0:
        I.doc.note("Text in composite (Type 0, CJK) fonts was left out")
        return None
    name = str(d.get("FontName", "Font"))
    I.doc.note(f"{name} is a kind of font bookport doesn't read (type {ft}), so it was drawn with a standard font")
    return _program(I, standard_font(I, name))


def _fid_prog(I, font: PSDict):
    fid = font.d.get("FID")
    if type(fid) is not FontID:
        raise PSError("invalidfont")
    return fid.prog


@op()
def definefont(I):
    font = I.os.pop()
    k = key(I.os.pop())
    if type(font) is not PSDict:
        raise PSError("typecheck")
    d = font.d
    if "FontMatrix" not in d or ("Encoding" not in d and d.get("FontType") != 0):
        if d.get("FontType") in (9, 10, 11, 32, 2):
            pass
        else:
            raise PSError("invalidfont")
    fid = d.get("FID")
    if type(fid) is not FontID:
        d[Name("FID")] = FontID(_program(I, font))
    I.fonts.d[k] = font
    I.resources.setdefault("Font", {})[k] = font
    I.os.append(font)


@op()
def undefinefont(I):
    k = key(I.os.pop())
    I.fonts.d.pop(k, None)


def find_font(I, k) -> PSDict:
    k = key(k)
    f = I.fonts.d.get(k)
    if type(f) is PSDict:
        return f
    f = I.resources.get("Font", {}).get(k)
    if type(f) is PSDict:
        return f
    name = str(k) if isinstance(k, str) else "Helvetica"
    f = standard_font(I, name)
    I.fonts.d[Name(name)] = f
    return f


@op()
def findfont(I):
    I.os.append(find_font(I, I.os.pop()))


def _copy_font(font: PSDict, m) -> PSDict:
    d = PSDict(dict(font.d), font.maxlength)
    d.d[Name("FontMatrix")] = PSArray(list(m))
    return d


@op()
def scalefont(I):
    s = float(pop_num(I))
    font = I.os.pop()
    if type(font) is not PSDict:
        raise PSError("typecheck")
    I.os.append(_copy_font(font, mul(_matrix(font.d.get("FontMatrix")), (s, 0.0, 0.0, s, 0.0, 0.0))))


@op()
def makefont(I):
    m = _matrix(I.os.pop())
    font = I.os.pop()
    if type(font) is not PSDict:
        raise PSError("typecheck")
    I.os.append(_copy_font(font, mul(_matrix(font.d.get("FontMatrix")), m)))


@op()
def setfont(I):
    font = I.os.pop()
    if type(font) is not PSDict:
        raise PSError("typecheck")
    I.gs.font = font


@op("currentfont", "rootfont")
def currentfont(I):
    I.os.append(I.gs.font if I.gs.font is not None else find_font(I, Name("Courier")))


@op()
def selectfont(I):
    v = I.os.pop()
    font = find_font(I, I.os.pop())
    if type(v) is PSArray:
        m = mul(_matrix(font.d.get("FontMatrix")), _matrix(v))
    else:
        s = float(v)
        m = mul(_matrix(font.d.get("FontMatrix")), (s, 0.0, 0.0, s, 0.0, 0.0))
    I.gs.font = _copy_font(font, m)


@op()
def setcachedevice(I):
    vals = [float(pop_num(I)) for _ in range(6)][::-1]
    cap = getattr(I, "capture", None)
    if cap is not None:
        cap["width"] = (vals[0], vals[1])
        cap["box"] = tuple(vals[2:])
        if I.gs.dev is cap["content"]:
            cap["content"].mask = True


@op()
def setcachedevice2(I):
    vals = [float(pop_num(I)) for _ in range(10)][::-1]
    cap = getattr(I, "capture", None)
    if cap is not None:
        cap["width"] = (vals[0], vals[1])
        cap["box"] = tuple(vals[2:6])
        if I.gs.dev is cap["content"]:
            cap["content"].mask = True


@op()
def setcharwidth(I):
    wy = float(pop_num(I))
    wx = float(pop_num(I))
    cap = getattr(I, "capture", None)
    if cap is not None:
        cap["width"] = (wx, wy)
        cap["box"] = None


# ---- glyphs

def _encoding(font: PSDict) -> list:
    enc = font.d.get("Encoding")
    return enc.list() if type(enc) is PSArray else []


def _glyph_name(enc: list, code: int) -> str:
    if code < len(enc):
        n = enc[code]
        if type(n) in (Name, XName):
            return str(n)
        if type(n) is PSString:
            return n.text()
    return ".notdef"


def _type3_glyph(I, font: PSDict, prog: FT.Type3, code: int, name: str) -> None:
    """Run a Type 3 font's BuildGlyph (or BuildChar) once for a glyph and keep what it paints."""
    k = code if prog.by_char else name
    if k in prog.glyphs:
        return
    prog.glyphs[k] = (None, 0.0, 0.0, None)  # (a glyph drawing itself stops here)
    content = Content(I.doc, shared=prog.names)
    content.fill_key = content.stroke_key = None
    saved = I.gs
    g = saved.copy()
    g.ctm, g.path, g.cp, g.start, g.clip, g.dev = IDENTITY, [], None, None, (), content
    g.font = font
    old_cap = getattr(I, "capture", None)
    cap = {"content": content, "width": (0.0, 0.0), "box": None}
    I.capture = cap
    I.gstack.append(saved)
    I.gs = g
    depth = len(I.gstack)
    try:
        I.os.append(font)
        build = font.d.get("BuildGlyph")
        if build is not None and not prog.by_char:
            I.os.append(Name(name))
        else:
            build = font.d.get("BuildChar")
            I.os.append(code)
        if build is None:
            raise PSError("invalidfont", "no BuildChar")
        I.call(build)
    finally:
        del I.gstack[depth - 1:]
        I.gs = saved
        I.capture = old_cap
    prog.glyphs[k] = (content, cap["width"][0], cap["width"][1], cap["box"])


def _glyph(I, font: PSDict, prog, code: int, name: str):
    """(pdf font, code bytes, glyph-space width) for one glyph, noting it in its PDF font."""
    doc = I.doc
    if prog.kind == "type42":
        gid = prog.gid(name)
        pf = prog.pdf_fonts.get("cid")
        if pf is None:
            pf = prog.pdf_fonts["cid"] = FT.CIDFont(doc, prog)
        wx, wy = prog.width(font, name, code)
        pf.use(gid, name, wx)
        return pf, gid.to_bytes(2, "big"), (wx, wy)
    if prog.kind == "type3":
        _type3_glyph(I, font, prog, code, name)
    wx, wy = prog.width(font, name, code)
    enc = font.d.get("Encoding")
    k = id(enc.items) if type(enc) is PSArray else 0
    variants = prog.pdf_fonts.setdefault(k, [])
    if type(enc) is PSArray:
        I.keep.append(enc)
    for pf in variants:
        if pf.use(code, name, wx):
            return pf, bytes([code]), (wx, wy)
    pf = FT.SimpleFont(doc, prog)
    variants.append(pf)
    pf.use(code, name, wx)
    return pf, bytes([code]), (wx, wy)


def _current(I):
    font = I.gs.font
    if font is None:
        font = find_font(I, Name("Courier"))
        I.gs.font = font
    prog = _fid_prog(I, font)
    return font, prog


def _codes(I, font: PSDict, s: PSString) -> list[tuple[int, str]]:
    enc = _encoding(font)
    return [(c, _glyph_name(enc, c)) for c in s.bytes()]


def show_glyphs(I, glyphs: list[tuple[int, str]], spacing=None, emit: bool = True, path: bool = False):
    """Place glyphs from the current point. spacing(i, code, (dx, dy)) gives each glyph's displacement in user
    space instead of (or added to) its width: returns (dx, dy, replace)."""
    gs = I.gs
    if gs.cp is None:
        raise PSError("nocurrentpoint")
    font, prog = _current(I)
    if prog is None:  # a composite font: the text is skipped, the point moves on by nothing
        return
    fm = _matrix(font.d.get("FontMatrix"))
    to_user = fm
    to_dev = mul(fm, gs.ctm)
    x, y = gs.cp
    placed = []
    for i, (code, name) in enumerate(glyphs):
        pf, data, (wx, wy) = _glyph(I, font, prog, code, name)
        placed.append((pf, data, x, y, wx))
        ux, uy = dapply(to_user, wx, wy)
        if spacing is not None:
            ax, ay, replace = spacing(i, code, (ux, uy))
            ux, uy = (ax, ay) if replace else (ux + ax, uy + ay)
        dx, dy = dapply(gs.ctm, ux, uy)
        x, y = x + dx, y + dy
    gs.cp = (x, y)
    if not placed:
        return
    runs_by_font = _runs(prog, fm, gs.ctm, placed)
    if path:
        for pf, runs in runs_by_font:
            box = _box(font, to_dev, placed)
            gs.path.append(("t", pf.ref, runs, box))
        return
    from .ops_graphics import color_key, ink
    if emit and ink(gs) is not None:
        color = color_key(I, gs)
        mode = 1 if font.d.get("PaintType") == 2 else 0
        style = b""
        if mode == 1:
            sw = font.d.get("StrokeWidth", 0)
            style = b"%.4f w" % float(sw if type(sw) in (int, float) else 0)
        for pf, runs in runs_by_font:
            gs.dev.text(pf.ref, runs, gs.clip, color, mode, style)


def _box(font, to_dev, placed):
    bb = font.d.get("FontBBox")
    vals = [float(v) for v in bb.list()] if type(bb) is PSArray and bb.length == 4 else [0, 0, 1000, 1000]
    if vals == [0, 0, 0, 0]:
        vals = [0, -200, 1000, 800]
    xs, ys = [], []
    for _pf, _d, x, y, wx in placed:
        for gx, gy in ((vals[0], vals[1]), (vals[2], vals[3]), (vals[0], vals[3]), (vals[2], vals[1])):
            dx, dy = dapply(to_dev, gx, gy)
            xs.append(x + dx)
            ys.append(y + dy)
    return min(xs), min(ys), max(xs), max(ys)


def _runs(prog, fm, ctm, placed):
    """TJ runs, one text matrix per stretch the PDF's own advances can follow (adjusting along the baseline).
    The glyphs' origin is the current point moved by the font matrix's own translation (makefont with an offset,
    for raised text), so lin is taken without the CTM's translation and its offset added to each run's origin."""
    lin = mul(mul(invert(prog.pdf_matrix), fm), (ctm[0], ctm[1], ctm[2], ctm[3], 0.0, 0.0))
    try:
        inv = invert((lin[0], lin[1], lin[2], lin[3], 0.0, 0.0))
    except PSError:
        return []
    out = []  # [(pdf font, runs)]
    runs = None
    cur_pf = None
    pen = (0.0, 0.0)
    origin = (0.0, 0.0)
    for pf, data, x, y, wx in placed:
        if pf is not cur_pf:
            runs = []
            out.append((pf, runs))
            cur_pf = pf
            items = None
        tx, ty = apply(inv, x - origin[0], y - origin[1])
        if items is not None:
            dx, dy = tx - pen[0], ty - pen[1]
            if abs(dy) > 1e-4:
                items = None
            elif abs(dx) > 1e-5:
                items.append(-dx * 1000.0)
        if items is None:
            origin = (x, y)
            tx = ty = 0.0
            items = []
            runs.append(((lin[0], lin[1], lin[2], lin[3], x + lin[4], y + lin[5]), items))
        if items and isinstance(items[-1], bytes):
            items[-1] = items[-1] + data
        else:
            items.append(data)
        adv = dapply(prog.pdf_matrix, wx, 0.0)[0]
        pen = (tx + adv, ty)
    return out


@op()
def show(I):
    s = I.os.pop()
    if type(s) is not PSString:
        raise PSError("typecheck")
    font, prog = _current(I)
    if prog is None:
        return
    show_glyphs(I, _codes(I, font, s))


@op()
def ashow(I):
    s = I.os.pop()
    ay = float(pop_num(I))
    ax = float(pop_num(I))
    font, prog = _current(I)
    if prog is None:
        return
    show_glyphs(I, _codes(I, font, s), lambda i, c, w: (ax, ay, False))


@op()
def widthshow(I):
    s = I.os.pop()
    char = pop_int(I)
    cy = float(pop_num(I))
    cx = float(pop_num(I))
    font, prog = _current(I)
    if prog is None:
        return
    show_glyphs(I, _codes(I, font, s), lambda i, c, w: ((cx, cy, False) if c == char else (0.0, 0.0, False)))


@op()
def awidthshow(I):
    s = I.os.pop()
    ay = float(pop_num(I))
    ax = float(pop_num(I))
    char = pop_int(I)
    cy = float(pop_num(I))
    cx = float(pop_num(I))
    font, prog = _current(I)
    if prog is None:
        return
    show_glyphs(I, _codes(I, font, s),
                lambda i, c, w: ((cx + ax, cy + ay, False) if c == char else (ax, ay, False)))


def _numarray(v) -> list[float]:
    if type(v) is PSArray:
        return [float(x) for x in v.list() if type(x) in (int, float)]
    if type(v) is PSString:
        data = v.bytes()
        if len(data) >= 4 and data[0] == 149:  # an encoded number string (PLRM 3.14.5)
            import struct
            rep = data[1]
            n = struct.unpack(">H" if rep < 128 else "<H", data[2:4])[0]
            body = data[4:]
            out = []
            r = rep % 128
            if r < 32:
                fmt, size, scale = (">i" if rep < 128 else "<i"), 4, 2.0 ** -r
            elif r < 48:
                fmt, size, scale = (">h" if rep < 128 else "<h"), 2, 2.0 ** -(r - 32)
            else:
                fmt, size, scale = (">f" if rep < 128 else "<f"), 4, 1.0
            for i in range(n):
                chunk = body[i * size:(i + 1) * size]
                if len(chunk) < size:
                    break
                out.append(struct.unpack(fmt, chunk)[0] * scale)
            return out
    raise PSError("typecheck")


def _xyshow(I, kind: str):
    nums = _numarray(I.os.pop())
    s = I.os.pop()
    if type(s) is not PSString:
        raise PSError("typecheck")
    font, prog = _current(I)
    if prog is None:
        return
    step = 2 if kind == "xy" else 1

    def spacing(i, c, w):
        j = i * step
        if kind == "xy":
            return (nums[j] if j < len(nums) else 0.0, nums[j + 1] if j + 1 < len(nums) else 0.0, True)
        v = nums[j] if j < len(nums) else 0.0
        return (v, 0.0, True) if kind == "x" else (0.0, v, True)
    show_glyphs(I, _codes(I, font, s), spacing)


OPS["xshow"] = lambda I: _xyshow(I, "x")
OPS["yshow"] = lambda I: _xyshow(I, "y")
OPS["xyshow"] = lambda I: _xyshow(I, "xy")


@op()
def kshow(I):
    s = I.os.pop()
    proc = pop_proc(I)
    font, prog = _current(I)
    if prog is None:
        return
    data = s.bytes()
    for i, c in enumerate(data):
        font, prog = _current(I)
        show_glyphs(I, [(c, _glyph_name(_encoding(font), c))])
        if i + 1 < len(data):
            I.os.extend([c, data[i + 1]])
            I.call(proc)


@op()
def cshow(I):
    s = I.os.pop()
    proc = pop_proc(I)
    font, prog = _current(I)
    for c in s.bytes():
        font, prog = _current(I)
        name = _glyph_name(_encoding(font), c)
        wx, wy = prog.width(font, name, c) if prog is not None else (0.0, 0.0)
        if prog is not None and prog.kind == "type3":
            _type3_glyph(I, font, prog, c, name)
            wx, wy = prog.width(font, name, c)
        ux, uy = dapply(_matrix(font.d.get("FontMatrix")), wx, wy)
        I.os.extend([c, ux, uy])
        I.call(proc)


@op()
def glyphshow(I):
    n = I.os.pop()
    font, prog = _current(I)
    if prog is None:
        return
    name = str(n) if type(n) in (Name, XName) else ".notdef"
    enc = _encoding(font)
    code = next((c for c in range(len(enc)) if _glyph_name(enc, c) == name), None)
    if code is None:  # a glyph the encoding doesn't reach: give it a code of its own in a PDF font
        code = 255
        k = id(font.d.get("Encoding").items) if type(font.d.get("Encoding")) is PSArray else 0
        used = {c for pf in prog.pdf_fonts.get(k, []) if hasattr(pf, "codes") for c in pf.codes}
        for c in range(255, 0, -1):
            if c not in used:
                code = c
                break
    show_glyphs(I, [(code, name)])


@op()
def stringwidth(I):
    s = I.os.pop()
    if type(s) is not PSString:
        raise PSError("typecheck")
    font, prog = _current(I)
    if prog is None:
        I.os.extend([0.0, 0.0])
        return
    fm = _matrix(font.d.get("FontMatrix"))
    tx = ty = 0.0
    for code, name in _codes(I, font, s):
        if prog.kind == "type3":
            _type3_glyph(I, font, prog, code, name)
        wx, wy = prog.width(font, name, code)
        ux, uy = dapply(fm, wx, wy)
        tx += ux
        ty += uy
    I.os.extend([tx, ty])


@op()
def charpath(I):
    pop_bool(I)
    s = I.os.pop()
    if type(s) is not PSString:
        raise PSError("typecheck")
    font, prog = _current(I)
    if prog is None:
        return
    show_glyphs(I, _codes(I, font, s), path=True)


def start_data(I):
    """FontSetInit's StartData: name length StartData, then that many bytes of CFF; each font in the set is
    defined (CID-keyed ones as CIDFont resources, which composite fonts would use)."""
    from . import cff
    n = pop_int(I)
    set_name = I.os.pop()
    f = I.currentfile()
    if f is None or n < 0:
        raise PSError("ioerror")
    data = f.stream.read(n)
    try:
        fonts = cff.read(data)
    except (cff.CFFError, IndexError, struct_error) as e:
        raise PSError("invalidfont", f"CFF: {e}") from None
    if len(fonts) > 1:
        I.doc.note("A CFF font set with several fonts was embedded whole for each of them")
    for font in fonts:
        if font.cid:
            I.resources.setdefault("CIDFont", {})[Name(font.name)] = PSDict({Name("CIDFontName"): Name(font.name)})
            continue
        enc = [".notdef"] * 256
        for c, nm in font.encoding.items():
            if 0 <= c < 256:
                enc[c] = nm
        d = PSDict({Name("FontType"): 2, Name("FontName"): Name(font.name), Name("PaintType"): 0,
                    Name("FontMatrix"): PSArray(list(font.matrix)), Name("FontBBox"): PSArray(list(font.bbox)),
                    Name("Encoding"): _encoding_array(enc), Name("Private"): PSDict(),
                    Name("CharStrings"): PSDict({Name(nm): i for i, nm in enumerate(font.names)}),
                    Name("BookportCFF"): (data, font)}, 20)
        I.os.extend([Name(font.name), d])
        definefont(I)
        I.os.pop()
    I.resources.setdefault("FontSet", {})[key(set_name)] = PSDict({Name("FontSetName"): set_name})
    if len(I.ds) > 3 and I.ds[-1].name == "FontSetInit":  # the procset's begin is closed here
        I.end()


def register(I: Interp) -> None:
    from .objects import Operator
    I.font_programs = {}
    I.keep = []  # objects whose id() keys a font program: kept alive so the id isn't reused
    setup(I)
    for name, fn in OPS.items():
        I.define(name, fn)
    I.resources.setdefault("ProcSet", {})[Name("FontSetInit")] = PSDict(
        {Name("StartData"): Operator("StartData", start_data)}, name="FontSetInit")
