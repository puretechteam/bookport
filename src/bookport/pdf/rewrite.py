"""Rewriting PDFs: pages kept, put in another order and turned; several PDFs joined into one; a PDF split
into several; and PDFs made smaller (pictures saved again at a lower resolution, the same picture or font
stored once, streams packed tighter, small objects in compressed object streams).

A PDF that is locked only by permissions (it opens without a password, but may not be edited or printed) can
be rewritten too: the result is locked again, with an empty user password, a random owner password, the same
permission bits (the fewest, when several are joined) and AES-256. A PDF that needs a password stays refused.

The objects a page needs are copied into a new file, renumbered; nothing is drawn again, so text stays
text. Bookmarks are rebuilt to point at the pages where they now are (those for pages taken out go; those
that open a web page or run an action stay), and the document's details (title, author...), links, form
fields and its other settings are kept. Links and bookmarks that jump to a place by its name jump to the page
itself, as names repeat across files. Page labels (i, ii, 1, 2...) follow their pages; the tag tree
(accessibility structure) is kept when the pages are all kept in their order, and left out otherwise. A page
taken out on its own (split, or some pages kept) takes only the pictures and fonts it draws, not every one
its file shares between pages.

Text can be laid over pages unseen (render mode 3): what OCR read on a scanned page, so the PDF can be
searched and its text copied. The page is drawn as before, the text over it in its own content stream.

    pdfs = edit([data], pages=[(2, 0), (0, 1)])   # the third page, then the first turned a quarter right
    pdfs = edit([a, b])                            # a and b joined
    pdfs = edit([data], split=1)                   # one PDF per page
    small = compress(data, dpi=150)
    found = edit([data], text={0: [[("Hello", 72, 700, 120, 712)]]})[0]  # page 1 searchable for "Hello"
"""

from __future__ import annotations

import bisect
import hashlib
import io
import zlib

from .. import limits
from ..errors import BookportError
from . import objects as out_objects
from .crypt import Encryptor
from .parser import IMAGE_FILTERS, Document, Name, PdfString, Ref, Stream, content_tokens, named_dests
from .textlayer import Fonts, _num, text_ops

DROP_FROM_CATALOG = ("Type", "Pages", "Outlines", "PageLabels", "StructTreeRoot", "MarkInfo")
KEPT_IN_ORDER = ("StructTreeRoot", "MarkInfo")  # kept when every page is, in its order: their page references hold
MOST_OUTLINE = 100_000
QUALITY = 75
TEXT_FONTS = "OCR"  # the text layer's fonts in a page's resources: /OCR0, /OCR1...


class _Source:
    """One PDF read for rewriting."""

    def __init__(self, data: bytes, number: int):
        self.doc = Document(data)  # a PDF that needs a password to open is refused here (DRMError)
        self.number = number
        self.pages = self.doc.pages()
        self.page_nums = {p["_ref"].num for p in self.pages if isinstance(p.get("_ref"), Ref)}
        self.dests = named_dests(self.doc)

    @property
    def permissions(self) -> int | None:
        """The /P of a PDF that opens with the empty password (a "permissions only" one), else None."""
        return self.doc.crypt.permissions if self.doc.crypt is not None else None

    def plain_raw(self, stm: Stream) -> bytes:
        """A stream's data, still in its filters, but no longer encrypted."""
        crypt = self.doc.crypt
        if crypt is not None and stm.num and stm.dict.get("Type") != "XRef":
            return crypt.decrypt(stm.raw, stm.num, stm.gen, stream=True)
        return stm.raw


def _dest(src: _Source, value):
    """A destination as [page ref, view...], from an explicit array, a name, or a GoTo action; None if
    it isn't one."""
    doc = src.doc
    value = doc.resolve(value)
    if isinstance(value, dict) and "S" in value:  # an action
        if str(value.get("S")) != "GoTo":
            return None
        value = doc.resolve(value.get("D"))
    if isinstance(value, (Name, PdfString, bytes, str)) and not isinstance(value, list):
        key = bytes(value).decode("latin-1") if isinstance(value, bytes) else str(value)
        value = doc.resolve(src.dests.get(key))
        if isinstance(value, dict):
            value = doc.resolve(value.get("D"))
    if isinstance(value, list) and value and isinstance(value[0], Ref):
        return value
    return None


def _outline(src: _Source) -> list:
    """The bookmarks as [(title, destination or None, colour, flags, children, action, open)]: action is
    one that isn't a jump to a page (a web address, a named action), open whether its children show."""
    doc = src.doc
    top = doc.resolve(doc.root.get("Outlines"))
    count = [0]

    def items(first, depth):
        got, node, seen = [], first, set()
        while isinstance(node, Ref) and node.num not in seen and depth < limits.MAX_TOC_DEPTH \
                and count[0] < MOST_OUTLINE:
            seen.add(node.num)
            count[0] += 1
            item = doc.resolve(node)
            if not isinstance(item, dict):
                break
            dest = _dest(src, item.get("Dest")) or _dest(src, item.get("A"))
            action = doc.resolve(item.get("A"))
            action = action if dest is None and isinstance(action, dict) and str(action.get("S")) != "GoTo" else None
            shown = doc.resolve(item.get("Count"))
            got.append((doc.resolve(item.get("Title")), dest, doc.resolve(item.get("C")), doc.resolve(item.get("F")),
                        items(item.get("First"), depth + 1), action, isinstance(shown, int) and shown > 0))
            node = item.get("Next")
        return got

    return items(top.get("First"), 0) if isinstance(top, dict) else []


class _Copier:
    """Copies one source's objects into a writer, renumbered: a page to the new page it became (or null when
    it isn't in this file), everything else once each."""

    def __init__(self, src: _Source, writer: out_objects.Writer, page_map: dict, squeeze: dict | None, shared: dict):
        self.src, self.w, self.page_map, self.squeeze, self.shared = src, writer, page_map, squeeze, shared
        self.map: dict[int, out_objects.Ref] = {}
        self.queue: list[int] = []
        self.masks: set[int] = set()  # soft masks: kept lossless
        self.limit_px = 0

    def ref(self, num: int):
        if num in self.page_map:
            return self.page_map[num]
        if num in self.src.page_nums:
            return None  # a page that isn't in this file
        r = self.map.get(num)
        if r is None:
            r = self.map[num] = self.w.reserve()
            self.queue.append(num)
        return r

    def value(self, v, depth: int = 0):
        if depth > limits.MAX_DEPTH:
            raise BookportError("Damaged PDF: objects nest too deep")
        if isinstance(v, Ref):
            return self.ref(v.num)
        if isinstance(v, Name):
            return out_objects.Name(v)
        if isinstance(v, PdfString):
            return bytes(v)
        if isinstance(v, dict):
            if isinstance(v.get("SMask"), Ref):
                self.masks.add(v["SMask"].num)
            if "Dest" in v or "D" in v:
                v = self._by_page(v)
            return {str(k): self.value(x, depth + 1) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x, depth + 1) for x in v]
        if isinstance(v, Stream):
            return None
        return v

    def _by_page(self, v: dict) -> dict:
        """A link (Dest) or GoTo action (D) that jumps to a place by its name, made to jump to the page itself:
        joined files often use the same names (LaTeX calls every first section section.1), and only the first
        file's names are kept."""
        resolve = self.src.doc.resolve
        for key in ("Dest", "D"):
            if key == "D" and str(resolve(v.get("S"))) != "GoTo":
                continue
            target = resolve(v.get(key))
            if isinstance(target, (str, bytes)):  # a name (a Name is a str, a PDF string bytes)
                dest = _dest(self.src, target)
                if dest is not None:
                    v = {**v, key: dest}
        return v

    def drain(self) -> None:
        while self.queue:
            num = self.queue.pop()
            obj = self.src.doc.get(num)
            ref = self.map[num]
            if isinstance(obj, Stream):
                self._stream(num, obj, ref)
            else:
                self.w.put(ref, self.value(obj))

    def _stream(self, num: int, stm: Stream, ref) -> None:
        d = {k: v for k, v in stm.dict.items() if k not in ("Length",)}
        raw = self.src.plain_raw(stm)
        if self.squeeze is not None:
            raw, d = self._smaller(num, stm, raw, d)
        body = self.value(d)
        key = None
        if self.squeeze is not None and not any(isinstance(v, Ref) for v in stm.dict.values()):
            key = hashlib.sha256(out_objects.serialize(body) + b"\0" + raw).digest()  # the same stream twice: once
            if key in self.shared:
                self.w.objects[ref.num] = b"null"
                self.alias(ref, self.shared[key])
                return
        self.w.stream(raw, body, compress=False, ref=ref)
        if key is not None:
            self.shared[key] = ref

    def alias(self, ref, to) -> None:
        """ref is the same stream as to: whatever points at ref points at to (fixed when the file is written)."""
        self.shared.setdefault("_aliases", {})[ref.num] = to.num

    # -- smaller streams

    def _smaller(self, num: int, stm: Stream, raw: bytes, d: dict):
        doc = self.src.doc
        if str(doc.resolve(d.get("Subtype"))) == "Image":
            got = self._smaller_picture(num, stm, raw, d)
            if got is not None:
                return got
            return raw, d
        filters = doc.stream_filters(stm)
        if filters in ([], ["FlateDecode"], ["Fl"]) and str(d.get("Type")) not in ("XRef", "ObjStm"):
            try:
                data = doc.decode(stm)
            except (BookportError, ValueError, zlib.error):
                return raw, d
            packed = zlib.compress(data, 9)
            if len(packed) < len(raw):
                d = {k: v for k, v in d.items() if k not in ("Filter", "DecodeParms", "DP")}
                d["Filter"] = Name("FlateDecode")
                return packed, d
        return raw, d

    def _smaller_picture(self, num: int, stm: Stream, raw: bytes, d: dict):
        """A picture saved again: no bigger than the page's size at the dpi asked, JPEG (a soft mask:
        lossless); None when it can't be or wouldn't be smaller."""
        try:
            from PIL import Image
        except ImportError:
            return None
        doc = self.src.doc
        filters = doc.stream_filters(stm)
        w, h, bits = doc.resolve(d.get("Width")), doc.resolve(d.get("Height")), doc.resolve(d.get("BitsPerComponent"))
        space = doc.resolve(d.get("ColorSpace"))
        if isinstance(space, list) and space and str(space[0]) == "ICCBased":
            icc = doc.resolve(space[1])
            n = doc.resolve(icc.dict.get("N")) if isinstance(icc, Stream) else None
            space = {1: "DeviceGray", 3: "DeviceRGB"}.get(n)
        mode = {"DeviceGray": "L", "DeviceRGB": "RGB"}.get(str(space)) if space is not None else None
        if (d.get("ImageMask") or bits != 8 or mode is None or "Decode" in d or not isinstance(w, int)
                or not isinstance(h, int) or w < 2 or h < 2 or any(f in IMAGE_FILTERS - {"DCTDecode", "DCT"} for f in filters)
                or len(filters) > 1 or "SMaskInData" in d):
            return None
        try:
            limits.check_pixels(w, h)
        except limits.LimitError:
            return None  # a poster or a map: copied as it is, not made smaller
        try:
            if filters in (["DCTDecode"], ["DCT"]):
                img = Image.open(io.BytesIO(raw))
                img.load()
                if img.mode != mode:
                    return None  # CMYK, or not what the PDF says it is: left as it is
            else:
                data = doc.decode(stm)
                if len(data) < w * h * len(mode):
                    return None
                img = Image.frombytes(mode, (w, h), data[:w * h * len(mode)])
        except (OSError, ValueError, BookportError):
            return None
        scale = min(1.0, self.limit_px / max(w, h)) if self.limit_px else 1.0
        if scale < 0.98:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        mask = num in self.masks
        if mask:
            buf.write(zlib.compress(img.tobytes(), 9))
        else:
            img.save(buf, "JPEG", quality=self.squeeze.get("quality", QUALITY), optimize=True)
        new = buf.getvalue()
        if len(new) >= len(raw) * 0.95:
            return None
        d = {k: v for k, v in d.items() if k not in ("Filter", "DecodeParms", "DP", "Width", "Height")}
        d.update(Width=img.width, Height=img.height, Filter=Name("FlateDecode" if mask else "DCTDecode"))
        return new, d


def _parse(sources: list[bytes]) -> list[_Source]:
    if not sources:
        raise BookportError("No PDF given")
    with limits.reading():
        return [_Source(data, i) for i, data in enumerate(sources)]


def edit(sources: list[bytes], pages: list[tuple[int, int]] | None = None, split: int | None = None,
         dpi: int | None = None, quality: int = QUALITY, text: dict | None = None) -> list[bytes]:
    """New PDFs from sources (joined, in order). pages: (page index, quarter turns clockwise) counted across
    all the sources together, in the order wanted (None: every page as it is). split: PDFs of this many pages
    each (None: one). dpi: made smaller (pictures at most this many pixels per inch of the largest page).
    text: invisible text to lay over pages, page index (as in pages) -> lines, each a list of
    (word, x0, y0, x1, y1) in points on the page as it shows: turned as the page itself says (not as pages
    turns it), from the bottom left of its visible area."""
    try:
        srcs = _parse(sources)
    except BookportError:
        raise
    except (ValueError, TypeError, IndexError, KeyError, AttributeError, RecursionError, zlib.error) as e:
        raise BookportError(f"Damaged PDF: {e}") from None
    every = [(src, page) for src in srcs for page in src.pages]
    if not every:
        raise BookportError("This PDF has no pages")
    plan = pages if pages is not None else [(i, 0) for i in range(len(every))]
    for i, _ in plan:
        if not 0 <= i < len(every):
            raise BookportError(f"There's no page {i + 1}: {'these PDFs have' if len(srcs) > 1 else 'this PDF has'} "
                                f"{len(every)} page{'s' if len(every) != 1 else ''}")
    if not plan:
        raise BookportError("No pages left: a PDF needs one at least")
    size = split if split and split > 0 else len(plan)
    squeeze = {"quality": quality} if dpi else None
    try:
        with limits.reading():
            return [_write(srcs, every, plan[k:k + size], squeeze, dpi, text or {}) for k in range(0, len(plan), size)]
    except BookportError:
        raise
    except (ValueError, TypeError, IndexError, KeyError, AttributeError, RecursionError, zlib.error) as e:
        raise BookportError(f"Damaged PDF: {e}") from None


def compress(data: bytes, dpi: int = 150, quality: int = QUALITY) -> bytes:
    """data made smaller (see edit)."""
    return edit([data], dpi=dpi, quality=quality)[0]


def _write(srcs: list[_Source], every: list, plan: list[tuple[int, int]], squeeze: dict | None, dpi: int | None,
           text: dict) -> bytes:
    # a source that was locked (permissions only) keeps its restrictions: the result is locked again, with the
    # fewest permissions any of the sources allowed
    locked = [s.permissions for s in srcs if s.permissions is not None]
    p = -1
    for bits in locked:
        p &= bits
    w = out_objects.Writer(Encryptor(p) if locked else None)
    fonts = None
    pages_ref = w.reserve()
    shared: dict = {}
    copiers = {}
    page_map: dict[int, dict] = {s.number: {} for s in srcs}
    new_pages = []
    for i, _ in plan:  # each page's new number first, so references to it anywhere find it
        src, page = every[i]
        ref = w.reserve()
        new_pages.append(ref)
        old = page.get("_ref")
        if isinstance(old, Ref):
            page_map[src.number].setdefault(old.num, ref)
    for src in srcs:
        copiers[src.number] = _Copier(src, w, page_map[src.number], squeeze, shared)
    whole = {s.number for s in srcs if all(p["_ref"].num in page_map[s.number] for p in s.pages if isinstance(p.get("_ref"), Ref))}
    largest = 0.0
    for (i, quarters), ref in zip(plan, new_pages):
        src, page = every[i]
        box = src.doc.resolve(page.get("MediaBox"))
        if isinstance(box, list) and len(box) == 4 and all(isinstance(src.doc.resolve(v), (int, float)) for v in box):
            x0, y0, x1, y1 = (float(src.doc.resolve(v)) for v in box)
            largest = max(largest, abs(x1 - x0), abs(y1 - y0))
        cp = copiers[src.number]
        lines = text.get(i)
        # a page with text laid over it gets resources of its own (a copy, with the text's fonts in it)
        body = {k: v for k, v in page.items() if k not in ("Parent", "_ref", "Type") and not (lines and k == "Resources")}
        if src.number not in whole and not lines:
            own_only = _own_resources(src.doc, page)
            if own_only is not None:
                body["Resources"] = own_only
        own = src.doc.resolve(page.get("Rotate"))
        own = int(own) % 360 if isinstance(own, (int, float)) and int(own) % 90 == 0 else 0
        body["Rotate"] = (own + 90 * quarters) % 360
        new = cp.value(body)
        if lines:
            fonts = fonts or Fonts(w)
            _lay_text(w, cp, src, page, own, new, lines, fonts)
        new["Type"] = out_objects.Name("Page")
        new["Parent"] = pages_ref
        if not new["Rotate"]:
            del new["Rotate"]
        w.put(ref, new)
    limit_px = round(largest / 72 * dpi) if dpi and largest else 0
    for cp in copiers.values():
        cp.limit_px = limit_px
    w.put(pages_ref, {"Type": out_objects.Name("Pages"), "Kids": new_pages, "Count": len(new_pages)})
    first = copiers[srcs[0].number]
    in_order = len(srcs) == 1 and [i for i, _ in plan] == list(range(len(every)))
    catalog = {k: v for k, v in srcs[0].doc.root.items() if k not in DROP_FROM_CATALOG or in_order and k in KEPT_IN_ORDER}
    catalog = first.value(catalog)
    action = catalog.get("OpenAction")
    if isinstance(action, list) and (not action or action[0] is None):
        del catalog["OpenAction"]  # it opened on a page that's gone
    catalog["Type"] = out_objects.Name("Catalog")
    catalog["Pages"] = pages_ref
    if locked:  # AES-256 (revision 6) is Adobe's extension level 8 to PDF 1.7
        catalog["Extensions"] = {"ADBE": {"BaseVersion": out_objects.Name("1.7"), "ExtensionLevel": 8}}
    labels = _page_labels(srcs, every, plan, copiers)
    if labels:
        catalog["PageLabels"] = {"Nums": labels}
    outline = []
    for src in srcs:
        outline += _place_outline(_outline(src), page_map[src.number], copiers[src.number])
    if outline:
        catalog["Outlines"] = _write_outline(w, outline)
        catalog.setdefault("PageMode", out_objects.Name("UseOutlines"))
    root = w.add(catalog)
    info = srcs[0].doc.resolve(srcs[0].doc.trailer.get("Info"))
    info_ref = w.add(first.value(info)) if isinstance(info, dict) else None
    for cp in copiers.values():
        cp.drain()
    if fonts is not None:
        fonts.finish()
    aliases = shared.get("_aliases")
    if aliases:
        _repoint(w, aliases)
    file_id = hashlib.md5(b"".join(w.objects[n] or b"" for n in range(1, min(len(w.objects), 64)))).digest()
    return w.finish(root, info_ref, file_id, compact=True)


def _lay_text(w: out_objects.Writer, cp: _Copier, src: _Source, page: dict, turn: int, new: dict, lines: list,
              fonts: Fonts) -> None:
    """Invisible words over the page new (as copied), given on the page as it shows (turned by turn)."""
    doc = src.doc
    x0, y0, x1, y1 = _box(doc, page.get("CropBox")) or _box(doc, page.get("MediaBox")) or (0.0, 0.0, 612.0, 792.0)
    wd, ht = x1 - x0, y1 - y0
    # from the page as it shows to the page's own space: turned back, then moved to the visible area's corner
    cm = {0: (1, 0, 0, 1, x0, y0), 90: (0, 1, -1, 0, x0 + wd, y0), 180: (-1, 0, 0, -1, x0 + wd, y0 + ht),
          270: (0, -1, 1, 0, x0, y0 + ht)}[turn]
    used: set[int] = set()
    ops = text_ops(lines, fonts, used, TEXT_FONTS)
    if not ops:
        return
    layer = b"Q\nq\n" + b" ".join(_num(v) for v in cm) + b" cm\n" + ops + b"Q\n"
    contents = new.get("Contents")
    old = contents if isinstance(contents, list) else [] if contents is None else [contents]
    # the page's own drawing kept whole between q and Q, so whatever state it leaves can't move the text
    new["Contents"] = [w.stream(b"q\n"), *old, w.stream(layer)]
    own = doc.resolve(page.get("Resources"))
    own = own if isinstance(own, dict) else {}
    res = cp.value({k: v for k, v in own.items() if str(k) != "Font"})
    font = doc.resolve(own.get("Font"))  # the page's fonts, as a dictionary of its own (it may be shared)
    font = cp.value(font) if isinstance(font, dict) else {}
    for f in sorted(used):
        font[f"{TEXT_FONTS}{f}"] = fonts.refs[f]
    res["Font"] = font
    new["Resources"] = res


def _labels(src: _Source) -> list[tuple[int, dict]] | None:
    """The page label ranges of a file: (first page index, its label dictionary), in order; None without."""
    doc = src.doc
    tree = doc.resolve(doc.root.get("PageLabels"))
    if not isinstance(tree, dict):
        return None
    found: dict[int, dict] = {}
    stack, seen = [tree], set()
    while stack and len(found) < MOST_OUTLINE:
        node = stack.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        nums = doc.resolve(node.get("Nums"))
        if isinstance(nums, list):
            for i in range(0, len(nums) - 1, 2):
                key, label = doc.resolve(nums[i]), doc.resolve(nums[i + 1])
                if isinstance(key, int) and key >= 0 and isinstance(label, dict):
                    found[key] = label
        kids = doc.resolve(node.get("Kids"))
        if isinstance(kids, list):
            stack.extend(doc.resolve(k) for k in kids)
    return sorted(found.items()) or None


def _page_labels(srcs: list[_Source], every: list, plan: list, copiers: dict) -> list:
    """/Nums for the new file: each page keeps the label it had (iv stays iv, 12 stays 12); a file without labels
    numbers its pages 1, 2, 3... Nothing when no file has labels."""
    ranges = {s.number: _labels(s) for s in srcs}
    if not any(ranges.values()):
        return []
    local = {}
    for s in srcs:
        for k, p in enumerate(s.pages):
            local[id(p)] = k
    out: list = []
    before = None  # (source, style, prefix, number) of the page before
    for new_index, (i, _) in enumerate(plan):
        src, page = every[i]
        k = local[id(page)]
        style, prefix, number = "D", None, k + 1
        got = ranges[src.number]
        if got:
            at = bisect.bisect_right([r[0] for r in got], k) - 1
            if at < 0:
                style = None  # before the first range: no label
            else:
                start, label = got[at]
                doc = src.doc
                style = doc.resolve(label.get("S"))
                style = str(style) if isinstance(style, str) else None
                prefix = doc.resolve(label.get("P"))
                prefix = bytes(prefix) if isinstance(prefix, bytes) else None
                first = doc.resolve(label.get("St"))
                number = (first if isinstance(first, int) and first > 0 else 1) + k - start
        now = (src.number, style, prefix, number)
        if before is None or now[:3] != before[:3] or number != before[3] + 1:
            entry: dict = {}
            if style is not None:
                entry["S"] = out_objects.Name(style)
            if prefix is not None:
                entry["P"] = prefix
            if number != 1:
                entry["St"] = number
            out += [new_index, entry]
        before = now
    return out


def _own_resources(doc: Document, page: dict) -> dict | None:
    """The page's resources with only the pictures, forms and fonts its drawing names (a file's pages often share
    one dictionary of every picture in it); None when that can't be told or nothing would go."""
    res = doc.resolve(page.get("Resources"))
    if not isinstance(res, dict):
        return None
    xobjects, fonts = doc.resolve(res.get("XObject")), doc.resolve(res.get("Font"))
    if not isinstance(xobjects, dict) and not isinstance(fonts, dict):
        return None
    from .reader import _without_inline_images
    contents = doc.resolve(page.get("Contents"))
    todo = []
    try:
        for c in contents if isinstance(contents, list) else [contents]:
            c = doc.resolve(c)
            if isinstance(c, Stream):
                todo.append(doc.decode(c))
    except (BookportError, ValueError, zlib.error):
        return None
    used: set[str] = set()
    forms = 0
    while todo:
        toks = content_tokens(_without_inline_images(todo.pop()))
        for k, (kind, v) in enumerate(toks):
            if kind != "kw" or v not in (b"Do", b"Tf"):
                continue
            name = toks[k - 1][1] if v == b"Do" else toks[k - 2][1] if k >= 2 else None
            if not isinstance(name, Name) or name in used:
                continue
            used.add(str(name))
            xo = doc.resolve(xobjects.get(str(name))) if v == b"Do" and isinstance(xobjects, dict) else None
            if isinstance(xo, Stream) and str(xo.dict.get("Subtype")) == "Form" and "Resources" not in xo.dict:
                forms += 1  # a form without resources of its own draws with the page's
                if forms > 1000:
                    return None
                try:
                    todo.append(doc.decode(xo))
                except (BookportError, ValueError, zlib.error):
                    return None
    kept = dict(res)
    dropped = False
    for key, table in (("XObject", xobjects), ("Font", fonts)):
        if isinstance(table, dict):
            mine = {n: ref for n, ref in table.items() if n in used}
            dropped = dropped or len(mine) < len(table)
            kept[key] = mine
    return kept if dropped else None


def _box(doc: Document, value) -> tuple[float, float, float, float] | None:
    box = doc.resolve(value)
    if not isinstance(box, list) or len(box) != 4:
        return None
    nums = [doc.resolve(v) for v in box]
    if not all(isinstance(v, (int, float)) for v in nums):
        return None
    a, b, c, d = (float(v) for v in nums)
    return min(a, c), min(b, d), max(a, c), max(b, d)


def _place_outline(items: list, page_map: dict, cp: _Copier) -> list:
    """Bookmarks pointing at the new pages; one whose page is gone goes (its own bookmarks move up)."""
    placed = []
    for title, dest, colour, flags, children, action, open_ in items:
        kids = _place_outline(children, page_map, cp)
        target = page_map.get(dest[0].num) if dest else None
        if target is None and not kids and action is None:
            continue
        place = None if target is None else [target, *(cp.value(v) for v in dest[1:])]
        placed.append((title, place, colour, flags, kids, cp.value(action) if action is not None else None, open_))
    return placed


def _write_outline(w: out_objects.Writer, items: list) -> out_objects.Ref:
    top = w.reserve()

    def level(parent, entries):
        refs = [w.reserve() for _ in entries]
        total = 0
        for k, (ref, (title, dest, colour, flags, kids, action, open_)) in enumerate(zip(refs, entries)):
            node = {"Title": bytes(title) if isinstance(title, bytes) else str(title or ""), "Parent": parent}
            if dest is not None:
                node["Dest"] = dest
            elif action is not None:
                node["A"] = action
            if k:
                node["Prev"] = refs[k - 1]
            if k + 1 < len(refs):
                node["Next"] = refs[k + 1]
            if isinstance(colour, list) and len(colour) == 3:
                node["C"] = colour
            if isinstance(flags, int) and flags:
                node["F"] = flags
            if kids:
                first, last, count = level(ref, kids)
                node.update(First=first, Last=last, Count=count if open_ else -count)
            w.put(ref, node)
            total += 1
        return refs[0], refs[-1], total

    first, last, count = level(top, items)
    w.put(top, {"Type": out_objects.Name("Outlines"), "First": first, "Last": last, "Count": count})
    return top


def _repoint(w: out_objects.Writer, aliases: dict[int, int]) -> None:
    """Point every reference to a stream stored twice at the one copy kept (the text of each object, which
    is all it is by now: "12 0 R" becomes "7 0 R")."""
    import re
    pattern = re.compile(rb"(?<![\d.])(\d+) 0 R\b")

    def fix(m):
        num = int(m.group(1))
        return b"%d 0 R" % aliases.get(num, num)
    for n in range(1, len(w.objects)):
        body = w.objects[n]
        if body is None or n in aliases:
            continue
        if n in w.streams:
            head, sep, rest = body.partition(b"\nstream\n")
            w.objects[n] = pattern.sub(fix, head) + sep + rest
        else:
            w.objects[n] = pattern.sub(fix, body)
