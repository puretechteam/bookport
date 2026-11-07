"""JB2 (DjVu v3 specification, appendix 2): the bitonal mask of a page (Sjbz) and the symbol
dictionaries pages share (Djbz).

A JB2 stream is a list of records coded with the Z'-coder: symbols coded pixel by pixel (directly,
or as a refinement of a library symbol), copies of library symbols, and where each goes on the
page. ``decode`` gives a ``Jb2`` (its symbols and where they're placed); ``mask`` draws it as
1-bit rows.

Coordinates follow the specification: rows count from the bottom of the page. Symbols are kept
with their rows top first.
"""

from __future__ import annotations

from .. import limits
from ..errors import BookportError
from .zp import ZP, EndOfData

BIGPOSITIVE = 262142
BIGNEGATIVE = -262143
MAX_CELLS = 4_000_000  # number-coder contexts (an encoder resets them after 20,000)
MAX_RECORDS = 4_000_000

(START, NEW_MARK, NEW_LIB_ONLY, NEW_IMAGE_ONLY, REFINE, REFINE_LIB_ONLY, REFINE_IMAGE_ONLY, COPY, NON_MARK,
 DICT_OR_RESET, COMMENT, END) = range(12)

# the integer contexts
(C_COMMENT_BYTE, C_COMMENT_LEN, C_RECORD, C_MATCH, C_ABS_X, C_ABS_Y, C_SIZE_X, C_SIZE_Y, C_IMAGE_SIZE, C_INHERIT,
 C_REL_X_CUR, C_REL_X_LAST, C_REL_Y_CUR, C_REL_Y_LAST, C_REL_W, C_REL_H) = range(16)

PAD_L, PAD_R = 2, 3  # zero columns kept around symbol rows for the context templates


class Shape:
    """One symbol: width, height, rows (top first, each with PAD_L/PAD_R zero columns) and the
    box around its black pixels, (left, bottom, right, top), rows counted from its bottom."""

    __slots__ = ("_ints", "box", "h", "rows", "w")

    def __init__(self, w: int, h: int, rows: list[bytearray]):
        self.w, self.h, self.rows = w, h, rows
        self._ints = None
        right = -1
        top = -1
        left = 0
        bottom = 0
        inked = [t for t in range(h) if any(rows[t])]
        if inked:
            top = h - 1 - inked[0]
            bottom = h - 1 - inked[-1]
            cols = bytearray(w + PAD_L + PAD_R)
            for t in inked:
                r = rows[t]
                for c in range(len(r)):
                    if r[c]:
                        cols[c] = 1
            left = cols.index(1) - PAD_L
            right = len(cols) - 1 - cols[::-1].index(1) - PAD_L
        self.box = (left, bottom, right, top)

    def ints(self) -> list[int]:
        """Each row as an int, bit c for column c."""
        if self._ints is None:
            self._ints = [_row_int(r, self.w) for r in self.rows]
        return self._ints


def _row_int(r: bytearray, w: int) -> int:
    # bytes of 0/1 -> an int with bit c set for column c
    return int(bytes(r[PAD_L:PAD_L + w]).translate(_DIGITS)[::-1] or b"0", 2)


_DIGITS = bytes([48, 49]) + bytes(254)


class Jb2:
    """A decoded JB2 image or dictionary."""

    def __init__(self):
        self.width = 0
        self.height = 0
        self.shapes: list[Shape] = []
        self.blits: list[tuple[int, int, int]] = []  # (left, bottom, shape number)
        self.inherited = 0  # shapes that came from the dictionary
        self.truncated = False  # the data ran out before its end record

    @property
    def blit_count(self) -> int:
        return len(self.blits)

    @property
    def shape_count(self) -> int:
        return len(self.shapes)

    def mask(self, which: list[int] | None = None) -> bytes:
        """The placements (all, or those listed by number) as packed rows, top first, 1 = black."""
        blits = self.blits if which is None else [self.blits[i] for i in which if i < len(self.blits)]
        return packed(rows(self, blits), self.width)


class _NumCoder:
    __slots__ = ("cells", "left", "right", "roots")

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.cells = bytearray(1)
        self.left = [0]
        self.right = [0]
        self.roots = [0] * 16


class _Decoder:
    def __init__(self, data: bytes, dictionary: Jb2 | None, is_dict: bool):
        self.zp = ZP(data)
        self.num = _NumCoder()
        self.flags = bytearray(2)  # eventual refinement, offset type
        self.direct = bytearray(1024)
        self.cross = bytearray(2048)
        self.dictionary = dictionary
        self.is_dict = is_dict
        self.out = Jb2()
        self.lib: list[int] = []  # library index -> shape number
        self.started = False
        self.budget = 0

    # -- integers -----------------------------------------------------------------------------
    def code_num(self, low: int, high: int, which: int) -> int:
        nc = self.num
        decode = self.zp.decode
        cells, left, right = nc.cells, nc.left, nc.right
        arr, at = nc.roots, which
        cutoff = 0
        phase = 1
        rng = -1
        negative = False
        while rng != 1:
            cell = arr[at]
            if not cell:
                cell = len(cells)
                if cell >= MAX_CELLS:
                    raise BookportError("Damaged DjVu file: a JB2 stream uses too many contexts")
                cells.append(0)
                left.append(0)
                right.append(0)
                arr[at] = cell
            if low >= cutoff:
                decision = 1
            elif high >= cutoff:
                decision = decode(cells, cell)
            else:
                decision = 0
            arr = right if decision else left
            at = cell
            if phase == 1:
                negative = not decision
                if negative:
                    low, high = -high - 1, -low - 1
                phase = 2
                cutoff = 1
            elif phase == 2:
                if not decision:
                    phase = 3
                    rng = (cutoff + 1) >> 1
                    cutoff = 0 if rng == 1 else cutoff - (rng >> 1)
                else:
                    cutoff += cutoff + 1
            else:
                rng >>= 1
                if rng != 1:
                    cutoff += (rng >> 1) if decision else -(rng >> 1)
                elif not decision:
                    cutoff -= 1
        return -cutoff - 1 if negative else cutoff

    # -- bitmaps ------------------------------------------------------------------------------
    def _new_rows(self, w: int, h: int) -> list[bytearray]:
        if w > 0xFFFF or h > 0xFFFF:
            raise BookportError("Damaged DjVu file: a JB2 symbol is too large")
        self.budget -= w * h
        if self.budget < 0:
            raise BookportError("Damaged DjVu file: a JB2 stream draws more than its page holds")
        return [bytearray(w + PAD_L + PAD_R) for _ in range(h)]

    def direct_bitmap(self, w: int, h: int) -> list[bytearray]:
        rows = self._new_rows(w, h)
        zp = self.zp
        decode = zp.decode
        ctx = self.direct
        zero = bytearray(w + PAD_L + PAD_R)
        up2 = up1 = zero
        for t in range(h):
            up0 = rows[t]
            c = ((up2[1] << 9) | (up2[2] << 8) | (up2[3] << 7) | (up1[0] << 6) | (up1[1] << 5) | (up1[2] << 4)
                 | (up1[3] << 3) | (up1[4] << 2))
            for x in range(w):
                n = decode(ctx, c)
                if n:
                    up0[x + 2] = 1
                c = ((c << 1) & 0x37A) | (up1[x + 5] << 2) | (up2[x + 4] << 7) | n
            up2, up1 = up1, up0
        return rows

    def cross_bitmap(self, w: int, h: int, ref: Shape) -> list[bytearray]:
        rows = self._new_rows(w, h)
        zp = self.zp
        decode = zp.decode
        ctx = self.cross
        left, bottom, right, top = ref.box
        # align the centers of the two symbols (rows counted from the bottom, as the specification does)
        xd2c = (w // 2 - w + 1) - ((right - left + 1) // 2 - right)
        yd2c = (h // 2 - h + 1) - ((top - bottom + 1) // 2 - top)
        ydiff = ref.h - h - yd2c  # top-first row of the reference beside each new row
        width = w + PAD_L + PAD_R
        zero = bytearray(width)
        aligned: dict[int, bytearray] = {}

        def refrow(r: int) -> bytearray:
            got = aligned.get(r)
            if got is None:
                if 0 <= r < ref.h:
                    src = ref.rows[r]
                    got = bytearray(width)
                    # column c of the new symbol lies over column c + xd2c of the reference
                    lo = max(-PAD_L, -PAD_L - xd2c)
                    hi = min(w + PAD_R, ref.w + PAD_R - xd2c)
                    if hi > lo:
                        got[lo + PAD_L:hi + PAD_L] = src[lo + xd2c + PAD_L:hi + xd2c + PAD_L]
                else:
                    got = zero
                aligned[r] = got
            return got

        up1 = zero
        for t in range(h):
            up0 = rows[t]
            r = t + ydiff
            xup1, xup0, xdn1 = refrow(r - 1), refrow(r), refrow(r + 1)
            c = ((up1[1] << 10) | (up1[2] << 9) | (up1[3] << 8) | (xup1[2] << 6) | (xup0[1] << 5)
                 | (xup0[2] << 4) | (xup0[3] << 3) | (xdn1[1] << 2) | (xdn1[2] << 1) | xdn1[3])
            for x in range(w):
                n = decode(ctx, c)
                if n:
                    up0[x + 2] = 1
                c = (((c << 1) & 0x636) | (up1[x + 4] << 8) | (xup1[x + 3] << 6) | (xup0[x + 4] << 3) | xdn1[x + 4]
                     | (n << 7))
            up1 = up0
            aligned.pop(r - 1, None)
        return rows

    # -- locations ----------------------------------------------------------------------------
    def start_image(self, w: int, h: int) -> None:
        self.last_left = w + 1
        self.last_row_left = 0
        self.last_row_bottom = h
        self.last_right = 0
        self.last_bottom = h
        self.short = [h, h, h]
        self.short_pos = 0
        self.started = True

    def relative_location(self, rows: int, cols: int) -> tuple[int, int]:
        if not self.started:
            raise BookportError("Damaged DjVu file: a JB2 symbol comes before the start of the image")
        if self.zp.decode(self.flags, 1):  # a new line
            left = self.last_row_left + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_X_LAST)
            top = self.last_row_bottom + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_Y_LAST)
            right = left + cols - 1
            bottom = top - rows + 1
            self.last_left = self.last_row_left = left
            self.last_right = right
            self.last_bottom = self.last_row_bottom = bottom
            self.short = [bottom, bottom, bottom]
            self.short_pos = 0
        else:  # on the same line
            left = self.last_right + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_X_CUR)
            bottom = self.last_bottom + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_Y_CUR)
            right = left + cols - 1
            self.last_left = left
            self.last_right = right
            self.short_pos = (self.short_pos + 1) % 3
            self.short[self.short_pos] = bottom
            self.last_bottom = sorted(self.short)[1]  # the median of the last three bottoms
        return left - 1, bottom - 1

    # -- records ------------------------------------------------------------------------------
    def add_shape(self, w: int, h: int, rows: list[bytearray]) -> int:
        out = self.out
        out.shapes.append(Shape(w, h, rows))
        return len(out.shapes) - 1

    def match(self) -> int:
        if not self.lib:
            raise BookportError("Damaged DjVu file: a JB2 symbol refers to an empty library")
        return self.lib[self.code_num(0, len(self.lib) - 1, C_MATCH)]

    def run(self) -> Jb2:
        out = self.out
        try:
            self._records()
        except EndOfData:  # the data ran out: what was placed so far stands, as in the reference decoder
            out.truncated = True
        if not self.started:
            raise BookportError("Damaged DjVu file: a JB2 stream has no start record")
        return out

    def _records(self) -> None:
        out = self.out
        code_num = self.code_num
        image = not self.is_dict
        for _ in range(MAX_RECORDS):
            kind = code_num(START, END, C_RECORD)
            if kind == START:
                if self.started:
                    raise BookportError("Damaged DjVu file: a JB2 stream starts twice")
                w = code_num(0, BIGPOSITIVE, C_IMAGE_SIZE)
                h = code_num(0, BIGPOSITIVE, C_IMAGE_SIZE)
                if image:
                    if not w or not h:
                        raise BookportError("Damaged DjVu file: a JB2 image has no size")
                    limits.check_pixels(w, h)
                elif w or h:
                    raise BookportError("Damaged DjVu file: a JB2 dictionary has an image size")
                out.width, out.height = w, h
                self.budget = 4 * w * h + 4_000_000 if image else limits.MAX_PIXELS
                self.zp.decode(self.flags, 0)  # the eventual-refinement flag (never set)
                self.start_image(w, h)
                if self.dictionary is not None and out.inherited:
                    out.shapes = list(self.dictionary.shapes[:out.inherited])
                    self.lib = list(range(out.inherited))
            elif kind == DICT_OR_RESET:
                if self.started:
                    self.num.reset()
                    continue
                n = code_num(0, BIGPOSITIVE, C_INHERIT)
                if n and (self.dictionary is None or len(self.dictionary.shapes) < n):
                    raise BookportError("Damaged DjVu file: a page needs a shape dictionary it doesn't have")
                out.inherited = n
            elif kind == END:
                break
            elif kind == COMMENT:
                for _ in range(code_num(0, BIGPOSITIVE, C_COMMENT_LEN)):
                    code_num(0, 255, C_COMMENT_BYTE)
            elif not self.started:
                raise BookportError("Damaged DjVu file: a JB2 symbol comes before the start of the image")
            elif kind in (NEW_MARK, NEW_LIB_ONLY, NEW_IMAGE_ONLY, NON_MARK):
                if not image and kind != NEW_LIB_ONLY:
                    raise BookportError("Damaged DjVu file: a JB2 dictionary holds an image record")
                w = code_num(0, BIGPOSITIVE, C_SIZE_X)
                h = code_num(0, BIGPOSITIVE, C_SIZE_Y)
                rows = self.direct_bitmap(w, h)
                if kind == NON_MARK:
                    left = code_num(1, out.width, C_ABS_X)
                    top = code_num(1, out.height, C_ABS_Y)
                    place = (left - 1, top - h)
                elif kind != NEW_LIB_ONLY:
                    place = self.relative_location(h, w)
                shape = self.add_shape(w, h, rows)
                if kind in (NEW_MARK, NEW_LIB_ONLY):
                    self.lib.append(shape)
                if kind != NEW_LIB_ONLY:
                    out.blits.append((place[0], place[1], shape))
            elif kind in (REFINE, REFINE_LIB_ONLY, REFINE_IMAGE_ONLY):
                if not image and kind != REFINE_LIB_ONLY:
                    raise BookportError("Damaged DjVu file: a JB2 dictionary holds an image record")
                ref = out.shapes[self.match()]
                left, bottom, right, top = ref.box
                w = right - left + 1 + code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_W)
                h = top - bottom + 1 + code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_H)
                if w < 0 or h < 0:
                    raise BookportError("Damaged DjVu file: a JB2 symbol has a negative size")
                rows = self.cross_bitmap(w, h, ref)
                if kind != REFINE_LIB_ONLY:
                    place = self.relative_location(h, w)
                shape = self.add_shape(w, h, rows)
                if kind != REFINE_IMAGE_ONLY:
                    self.lib.append(shape)
                if kind != REFINE_LIB_ONLY:
                    out.blits.append((place[0], place[1], shape))
            elif kind == COPY:
                if not image:
                    raise BookportError("Damaged DjVu file: a JB2 dictionary holds an image record")
                shape = self.match()
                left, bottom, right, top = out.shapes[shape].box
                x, y = self.relative_location(top - bottom + 1, right - left + 1)
                out.blits.append((x - left, y - bottom, shape))
        else:
            raise BookportError("Damaged DjVu file: a JB2 stream has too many records")


def decode(data: bytes, dictionary=None, is_dict: bool = False):
    """Decode an Sjbz (image) or Djbz (``is_dict``) chunk; ``dictionary`` is the Djbz it inherits
    from. Either way the result has width, height, truncated, blit_count and mask()."""
    from ._accel import native
    if native is not None:
        return native.jb2_decode(bytes(data), dictionary, is_dict)
    return decode_py(data, dictionary, is_dict)


def decode_py(data: bytes, dictionary: Jb2 | None = None, is_dict: bool = False) -> Jb2:
    return _Decoder(data, dictionary, is_dict).run()


def rows(jb2: Jb2, blits=None) -> list[int]:
    """The image as rows top first, each an int with bit c set for a black pixel in column c.
    ``blits`` picks which placements to draw (all of them by default)."""
    w, h = jb2.width, jb2.height
    rows = [0] * h
    shapes = jb2.shapes
    for left, bottom, s in (jb2.blits if blits is None else blits):
        shape = shapes[s]
        sh = shape.h
        top = h - bottom - sh  # page row of the symbol's top row
        ints = shape.ints()
        t0 = max(0, -top)
        t1 = min(sh, h - top)
        if left >= 0:
            for t in range(t0, t1):
                if ints[t]:
                    rows[top + t] |= ints[t] << left
        else:
            for t in range(t0, t1):
                if ints[t]:
                    rows[top + t] |= ints[t] >> -left
    full = (1 << w) - 1
    return [r & full for r in rows]


# bit-reversed bytes: ints keep column c in bit c, packed rows keep column 0 in the top bit
REVERSE = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))


def packed(rows: list[int], w: int) -> bytes:
    """Rows of ints as packed 1-bit rows (column 0 in the top bit, 1 = black), as PBM and PDF want."""
    n = (w + 7) // 8
    return b"".join(r.to_bytes(n, "little") for r in rows).translate(REVERSE)


def unpacked(data: bytes, w: int, h: int) -> list[int]:
    """Packed rows back to ints (bit c for column c)."""
    n = (w + 7) // 8
    full = (1 << w) - 1
    t = data.translate(REVERSE)
    return [int.from_bytes(t[y * n:(y + 1) * n], "little") & full for y in range(h)]
