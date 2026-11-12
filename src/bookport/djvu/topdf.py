"""DjVu to PDF that keeps DjVu's layers: the mask as a 1-bit image (CCITT G4 or Flate, whichever
is smaller) painted in its foreground colours over the background, which stays at the reduced
resolution DjVu stores it at (JPEG, or Flate when that is smaller). Pure black-and-white pages are
just the mask. The hidden text becomes an invisible text layer on the words (so the PDF can be
searched and copied from), the outline becomes bookmarks, links stay links, and page sizes come
from each page's resolution.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import struct
import zlib

from .. import limits
from ..errors import BookportError
from ..pdf.objects import Name, Ref, Writer
from ..pdf.textlayer import Fonts as _Fonts
from ..pdf.textlayer import _num
from ..pdf.textlayer import text_ops as _text_ops
from . import anno, composite, jpeg, mmr, text
from .doc import Document, Page

QUALITY = 75  # JPEG quality of reduced backgrounds (smoothed further by the viewer's scaling)
PHOTO_QUALITY = 85  # and of full-resolution pictures


def _mask_image(packed: bytes, width: int, height: int) -> tuple[bytes, dict]:
    """A 1-bit stencil, painted where the mask is black: JBIG2 (with bookport-native), CCITT G4 or
    Flate, whichever is smallest."""
    from ._accel import native
    d = {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": width, "Height": height, "ImageMask": True,
         "BitsPerComponent": 1}
    # JBIG2 and G4 both decode black as 0, which a stencil paints by default
    options = []
    if native is not None:
        options.append((native.jbig2_encode(packed, width, height), {"Filter": Name("JBIG2Decode")}))
    if not options or len(options[0][0]) * 24 > len(packed):  # G4 can only win on unusual (busy) masks
        options.append((mmr.encode_packed(packed, width, height),
                        {"Filter": Name("CCITTFaxDecode"), "DecodeParms": {"K": -1, "Columns": width, "Rows": height}}))
    if min(len(o[0]) for o in options) * 12 > len(packed):  # Flate wins only on halftones and dithering
        options.append((zlib.compress(packed, 6), {"Filter": Name("FlateDecode"), "Decode": [1, 0]}))
    data, extra = min(options, key=lambda o: len(o[0]))
    d.update(extra)
    return data, d


def _picture(img, quality: int, interpolate: bool) -> tuple[bytes, dict]:
    """A colour or grey picture: JPEG, or Flate when that's smaller (flat or tiny pictures)."""
    width, height, mode, px = img
    cs = Name("DeviceRGB" if mode == "RGB" else "DeviceGray")
    d = {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": width, "Height": height, "ColorSpace": cs,
         "BitsPerComponent": 8}
    if interpolate:
        d["Interpolate"] = True
    jp = jpeg.encode(width, height, mode, px, quality)
    flat = zlib.compress(px, 1)  # a quick look: only nearly flat pictures are worth Flate's best effort
    if len(flat) < 2 * len(jp):
        flat = zlib.compress(px, 9)
    if len(jp) < len(flat):
        d["Filter"] = Name("DCTDecode")
        return jp, d
    d["Filter"] = Name("FlateDecode")
    return flat, d


class _Prepared:
    """One page's pictures, drawing operators and words, worked out apart from the PDF writer (so
    pages can be prepared side by side)."""

    def __init__(self):
        self.images: list[tuple[str, bytes, dict, str | None]] = []  # name, data, dict, name of its mask
        self.ops: list[bytes] = []
        self.lost: str | None = None
        self.lines: list = []


def _prepare(page: Page) -> _Prepared:
    out = _Prepared()
    info = page.info
    W, H = info.width, info.height
    try:
        lay = composite.layers(page)
        if lay.bg is not None:
            reduced = (lay.bg[0], lay.bg[1]) != (W, H)
            data, d = _picture(lay.bg, QUALITY if reduced else PHOTO_QUALITY, reduced)
            out.images.append(("B", data, d, None))
            out.ops.append(b"q %d 0 0 %d 0 0 cm /B Do Q\n" % (W, H))
        elif lay.bg_jpeg is not None:
            out.images.append(("B", lay.bg_jpeg, _jpeg_dict(lay.bg_jpeg), None))
            out.ops.append(b"q %d 0 0 %d 0 0 cm /B Do Q\n" % (W, H))
        for i, (color, rows) in enumerate(composite.color_masks(lay)):
            if not rows.strip(bytes(1)):
                continue
            name = f"M{i}"
            data, d = _mask_image(rows, W, H)
            if color is None:  # the foreground picture's colours, through the mask
                out.images.append((name + "m", data, d, None))
                data, d = _picture(lay.fg, QUALITY, False)
                out.images.append((name, data, d, name + "m"))
                out.ops.append(b"q %d 0 0 %d 0 0 cm /%s Do Q\n" % (W, H, name.encode()))
            else:
                out.images.append((name, data, d, None))
                r, g, b = color
                out.ops.append(b"q %s %s %s rg %d 0 0 %d 0 0 cm /%s Do Q\n" % (
                    _num(r / 255), _num(g / 255), _num(b / 255), W, H, name.encode()))
    except BookportError as e:
        out.lost = f"Page {page.index + 1} couldn't be drawn and was left blank: {e}"
    except (struct.error, IndexError, ValueError, OverflowError, MemoryError, RecursionError) as e:
        out.lost = f"Page {page.index + 1} couldn't be drawn and was left blank: it's damaged ({type(e).__name__})"
        out.images, out.ops = [], []
    try:
        out.lines = text.lines(page)
    except (BookportError, struct.error, IndexError, ValueError, OverflowError, RecursionError):
        out.lines = []
    page.doc.cache.pop(("text", page.index), None)
    return out


def _page(w: Writer, page: Page, prep: _Prepared, page_ref: Ref, parent: Ref, fonts: _Fonts, doc: Document,
          page_refs: list[Ref], lost: list[str]) -> None:
    info = page.info
    W, H = info.width, info.height
    # a hair under the page's exact size, so that at its own resolution, or fitted to its width, a
    # reader lands its pixels on whole device pixels (a page that rounds up gets a row more to fill,
    # and every pixel is stretched into it, which blurs the text); the height a hair more, since
    # fitting to the width scales it by the width's rounding
    across, down = 1 - 2e-6, 1 - 4e-6
    pw = W * 72 / info.dpi * (down if info.rotation % 180 else across)
    ph = H * 72 / info.dpi * (across if info.rotation % 180 else down)
    # and the pictures a hair under the page, centred: a reader rounds their edges outwards
    sx, sy = pw / W * (1 - 2e-6), ph / H * (1 - 2e-6)
    refs: dict[str, Ref] = {}
    xobjects: dict[str, Ref] = {}
    for name, data, d, mask in prep.images:
        if mask is not None:
            d = {**d, "Mask": refs[mask]}
        refs[name] = w.stream(data, d, compress=False)
        if not name.endswith("m"):
            xobjects[name] = refs[name]
    if prep.lost:
        lost.append(prep.lost)
    used: set[int] = set()
    ops = [b"q %.10g 0 0 %.10g %.6f %.6f cm\n" % (sx, sy, (pw - W * sx) / 2, (ph - H * sy) / 2), *prep.ops, _text_ops(prep.lines, fonts, used), b"Q\n"]
    resources: dict = {}
    if xobjects:
        resources["XObject"] = xobjects
    if used:
        resources["Font"] = {f"T{f}": fonts.refs[f] for f in sorted(used)}
    content = w.stream(b"".join(ops))
    pd = {"Type": Name("Page"), "Parent": parent, "MediaBox": [0, 0, pw, ph],
          "Resources": resources, "Contents": content}
    if info.rotation:
        pd["Rotate"] = (360 - info.rotation) % 360
    annots = _links(w, page, doc, page_refs, sx, sy)
    if annots:
        pd["Annots"] = annots
    w.put(page_ref, pd)


def _round(v: float) -> float:
    return round(v, 3)


def _jpeg_dict(data: bytes) -> dict:
    from ..pdf.images import _jpeg_info
    got = _jpeg_info(data)
    if got is None:
        raise BookportError("Damaged DjVu file: a JPEG background is damaged")
    width, height, comps, bits, _adobe = got
    return {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": width, "Height": height,
            "ColorSpace": Name({1: "DeviceGray", 4: "DeviceCMYK"}.get(comps, "DeviceRGB")), "BitsPerComponent": bits,
            "Filter": Name("DCTDecode"), "Interpolate": True}


def _links(w: Writer, page: Page, doc: Document, page_refs: list[Ref], sx: float, sy: float) -> list[Ref]:
    try:
        ann = anno.page_annotations(page)
    except BookportError:
        return []
    info = page.info
    out = []
    for link in ann.links:
        x0, y0, x1, y1 = _unrotate(link.rect, info)
        rect = [_round(x0 * sx), _round(y0 * sy), _round(x1 * sx), _round(y1 * sy)]
        d = {"Type": Name("Annot"), "Subtype": Name("Link"), "Rect": rect, "Border": [0, 0, 0]}
        if link.url.startswith("#"):
            target = anno.target(doc, link.url, page.index)
            if target is None:
                continue
            d["Dest"] = [page_refs[target], Name("Fit")]
        else:
            d["A"] = {"S": Name("URI"), "URI": link.url}
        if link.comment:
            d["Contents"] = link.comment
        out.append(w.add(d))
    return out


def _unrotate(rect, info) -> tuple[int, int, int, int]:
    """Annotation boxes are given on the page as shown (rotated); PDF wants them on the page as stored."""
    x0, y0, x1, y1 = rect
    W, H = info.width, info.height
    if info.rotation == 90:  # turned counter-clockwise: shown (X, Y) is stored (Y, H - X)
        return y0, H - x1, y1, H - x0
    if info.rotation == 180:
        return W - x1, H - y1, W - x0, H - y0
    if info.rotation == 270:  # turned clockwise: shown (X, Y) is stored (W - Y, X)
        return W - y1, x0, W - y0, x1
    return x0, y0, x1, y1


def _outline(w: Writer, doc: Document, page_refs: list[Ref]) -> Ref | None:
    if not doc.outline_data:
        return None
    try:
        marks = anno.outline(doc.outline_data)
    except BookportError:
        return None
    if not marks:
        return None
    root = w.reserve()

    def build(items, parent) -> tuple[list[Ref], int]:
        refs = [w.reserve() for _ in items]
        total = 0
        for i, (m, ref) in enumerate(zip(items, refs)):
            d = {"Title": " ".join(m.title.split()) or "Untitled", "Parent": parent}
            t = anno.target(doc, m.url)
            if t is not None:
                d["Dest"] = [page_refs[t], Name("Fit")]
            elif m.url and not m.url.startswith("#"):
                d["A"] = {"S": Name("URI"), "URI": m.url}
            if i:
                d["Prev"] = refs[i - 1]
            if i + 1 < len(refs):
                d["Next"] = refs[i + 1]
            kids, count = build(m.children, ref)
            if kids:
                d["First"], d["Last"], d["Count"] = kids[0], kids[-1], count
            w.put(ref, d)
            total += 1 + count
        return refs, total

    refs, count = build(marks, root)
    w.put(root, {"Type": Name("Outlines"), "First": refs[0], "Last": refs[-1], "Count": count})
    return root


def threads() -> int:
    """How many pages are prepared at once: BOOKPORT_THREADS, else one less than the cores (at
    most 8). Only with bookport-native, whose decoders let go of Python's lock while they work."""
    from ._accel import native
    if native is None:
        return 1
    try:
        n = int(os.environ.get("BOOKPORT_THREADS", "0"))
    except ValueError:
        n = 0
    return max(1, n) if n > 0 else max(1, min(8, (os.cpu_count() or 2) - 1))


def _prepared(doc: Document):
    """Each page's _Prepared, in order; several at a time in threads when that helps."""
    n = threads()
    if n == 1 or len(doc.pages) < 2:
        for page in doc.pages:
            yield _prepare(page)
        return
    import contextvars
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(n) as pool:
        ahead: deque = deque()
        for page in doc.pages:  # at most 2n pages in flight (each holds its decoded pictures)
            ahead.append(pool.submit(contextvars.copy_context().run, _prepare, page))  # the same unpacking budget
            if len(ahead) >= 2 * n:
                yield ahead.popleft().result()
        while ahead:
            yield ahead.popleft().result()


def convert(doc: Document) -> tuple[bytes, list[str]]:
    """The PDF for a document, and what had to be left out (said for people)."""
    w = Writer()
    catalog = w.reserve()
    pages_ref = w.reserve()
    page_refs = [w.reserve() for _ in doc.pages]
    fonts = _Fonts(w)
    lost = list(doc.lost)
    for page, prep in zip(doc.pages, _prepared(doc)):
        _page(w, page, prep, page_refs[page.index], pages_ref, fonts, doc, page_refs, lost)
    fonts.finish()
    w.put(pages_ref, {"Type": Name("Pages"), "Kids": page_refs, "Count": len(page_refs)})
    cat = {"Type": Name("Catalog"), "Pages": pages_ref}
    outline = _outline(w, doc, page_refs)
    if outline is not None:
        cat["Outlines"] = outline
        cat["PageMode"] = Name("UseOutlines")
    w.put(catalog, cat)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    info = {"Producer": "bookport", "CreationDate": now}
    try:
        meta = anno.page_annotations(doc.pages[0]).metadata
    except BookportError:
        meta = {}
    for key, name in (("title", "Title"), ("author", "Author"), ("subject", "Subject"), ("keywords", "Keywords")):
        if meta.get(key):
            info[name] = meta[key]
    if "Title" in info:
        cat["ViewerPreferences"] = {"DisplayDocTitle": True}
        w.put(catalog, cat)
    info_ref = w.add(info)
    fid = hashlib.md5(doc.path.name.encode("utf-8", "replace") + now.encode()).digest()
    data = w.finish(catalog, info_ref, fid)
    limits.grow(len(data), limits.MAX_BOOK)
    return data, lost
