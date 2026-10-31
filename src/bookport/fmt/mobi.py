"""Mobipocket and Kindle books: MOBI 6, KF8 (AZW3) and joint MOBI 6 + KF8 files. Read and write.

Reading follows the MobileRead wiki's description of the format: the PDB container, the
PalmDOC/MOBI headers and EXTH metadata, PalmDOC and HUFF/CDIC compressed text with trailing
entries, the NCX index for the TOC, filepos links and recindex images for MOBI 6, and for KF8
the FDST flows, skeleton and fragment indexes that rebuild the original XHTML files, with
kindle:pos, kindle:embed and kindle:flow links resolved. Encrypted books raise DRMError.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .. import limits, markup
from ..book import IMAGE_EXT, Book, Landmark, Metadata, TocEntry, relative, sniff_image
from ..errors import BookportError, DRMError
from ..mobi_index import read_index
from ..palm import HuffCdic, palmdoc_decompress, read_pdb, trailing_size

NULL = 0xFFFFFFFF

NUMERIC_EXTH = {115, 116, 121, 125, 131, 201, 202, 203, 204, 205, 206, 207, 401, 403, 404, 405, 406}

LOCALES = {9: "en", 7: "de", 12: "fr", 10: "es", 16: "it", 19: "nl", 22: "pt", 25: "ru", 17: "ja", 4: "zh", 18: "ko",
           29: "sv", 20: "no", 6: "da", 11: "fi", 21: "pl", 5: "cs", 14: "hu", 31: "tr", 8: "el", 13: "he", 1: "ar"}


@dataclass
class MobiHeader:
    offset: int  # record index of this header's record 0
    compression: int
    text_length: int
    text_records: int
    record_size: int
    encryption: int
    mobi_type: int
    encoding: str
    version: int
    first_non_book: int
    title: str
    locale: int
    first_image: int
    huff_offset: int
    huff_count: int
    exth_flags: int
    extra_flags: int
    ncx_index: int
    fdst: int = NULL
    fdst_count: int = 0
    frag_index: int = NULL
    skel_index: int = NULL
    guide_index: int = NULL
    exth: dict = field(default_factory=dict)
    header_length: int = 0


def parse_header(records: list[bytes], idx: int) -> MobiHeader:
    r0 = records[idx]
    if len(r0) < 16:
        raise BookportError("Damaged MOBI: record 0 is too short")
    compression, _, text_length, nrec, rsize, encryption = struct.unpack(">HHIHHH", r0[:14])
    if r0[16:20] != b"MOBI":
        # a plain PalmDOC book inside a .mobi/.prc
        return MobiHeader(idx, compression, text_length, nrec, rsize, encryption, 0, "cp1252", 0, NULL, "", 0, NULL,
                          0, 0, 0, 0, NULL)

    def u32(off):
        return struct.unpack(">I", r0[off:off + 4])[0] if off + 4 <= len(r0) else NULL

    hlen = u32(20)
    enc = u32(28)
    version = u32(36)
    name_off, name_len = u32(84), u32(88)
    h = MobiHeader(
        offset=idx, compression=compression, text_length=text_length, text_records=nrec, record_size=rsize,
        encryption=encryption, mobi_type=u32(24), encoding="utf-8" if enc == 65001 else "cp1252", version=version,
        first_non_book=u32(80), title="", locale=u32(92) & 0xFF if u32(92) != NULL else 0, first_image=u32(108),
        huff_offset=u32(112), huff_count=u32(116), exth_flags=u32(128),
        extra_flags=(u32(240) & 0xFFFF) if hlen >= 0xE4 and 16 + hlen >= 244 else 0,
        ncx_index=u32(244) if 16 + hlen >= 248 else NULL, header_length=hlen)
    if version >= 8 and 16 + hlen >= 264:
        h.fdst = u32(192)
        h.fdst_count = u32(196)
        h.frag_index = u32(248)
        h.skel_index = u32(252)
        h.guide_index = u32(260)
    if name_off != NULL and name_off < len(r0):
        h.title = r0[name_off:name_off + name_len].decode(h.encoding, "replace")
    if h.exth_flags & 0x40:
        h.exth = parse_exth(r0[16 + hlen:], h.encoding)
    return h


def parse_exth(data: bytes, encoding: str) -> dict:
    out: dict[int, list] = {}
    if data[:4] != b"EXTH":
        return out
    _hlen, count = struct.unpack(">II", data[4:12])
    pos = 12
    for _ in range(count):
        if pos + 8 > len(data):
            break
        kind, length = struct.unpack(">II", data[pos:pos + 8])
        if length < 8:
            break
        value = data[pos + 8:pos + length]
        if kind in NUMERIC_EXTH and len(value) == 4:
            value = struct.unpack(">I", value)[0]
        elif kind not in (209, 300):
            value = value.decode(encoding, "replace")
        out.setdefault(kind, []).append(value)
        pos += length
    return out


def kind(data: bytes) -> str:
    """'mobi' (MOBI 6, maybe with KF8 inside), 'azw3' (KF8 only), or 'pdb'."""
    pdb = read_pdb(data)
    if pdb.type + pdb.creator != b"BOOKMOBI":
        return "pdb"
    h = parse_header(pdb.records, 0)
    return "azw3" if h.version == 8 else "mobi"


def _text(records: list[bytes], h: MobiHeader) -> bytes:
    if h.encryption:
        raise DRMError("Kindle" if h.version >= 8 else "MOBI", "it is encrypted (Mobipocket/Amazon DRM)")
    first = h.offset + 1
    recs = records[first:first + h.text_records]
    if h.compression == 17480:
        huff = HuffCdic(records[h.offset + h.huff_offset], records[h.offset + h.huff_offset + 1:h.offset + h.huff_offset + h.huff_count])
        decomp = huff.decompress
    elif h.compression == 2:
        decomp = palmdoc_decompress
    elif h.compression == 1:
        def decomp(b):
            return b
    else:
        raise BookportError(f"Unknown MOBI compression {h.compression}")
    parts = []
    for r in recs:
        cut = trailing_size(r, h.extra_flags) if h.extra_flags else 0
        parts.append(decomp(r[:len(r) - cut] if cut else r))
    text = b"".join(parts)
    if h.text_length and len(text) > h.text_length:
        text = text[:h.text_length]
    return text


def _metadata(h: MobiHeader) -> Metadata:
    md = Metadata()
    ex = h.exth
    md.title = (ex.get(503) or [h.title])[0].strip() or h.title
    md.authors = []
    for a in ex.get(100, []):
        for part in re.split(r"\s*&\s*|\s*;\s*", a):
            if part.strip() and part.strip() not in md.authors:
                md.authors.append(part.strip())
    md.publisher = (ex.get(101) or [""])[0].strip()
    md.description = (ex.get(103) or [""])[0].strip()
    if ex.get(104):
        md.identifiers["isbn"] = ex[104][0].strip()
    md.subjects = [s.strip() for s in ex.get(105, []) if s.strip()]
    md.date = (ex.get(106) or [""])[0].strip()
    md.contributors = [c.strip() for c in ex.get(108, []) if c.strip() and "calibre" not in c.lower()]
    md.rights = (ex.get(109) or [""])[0].strip()
    if ex.get(113):
        md.identifiers["asin"] = ex[113][0].strip()
    if ex.get(112):
        src = ex[112][0].strip()
        if src.lower().startswith("calibre:"):
            md.identifiers.setdefault("uuid", src.split(":", 1)[1])
        elif src:
            md.identifiers.setdefault("source", src)
    md.language = (ex.get(524) or [""])[0].strip() or LOCALES.get(h.locale, "")
    return md


def read(path) -> Book:
    data = Path(path).read_bytes()
    return from_bytes(data)


def from_bytes(data: bytes) -> Book:
    if data[:4] == b"TPZ0" or data[:3] == b"TPZ":
        raise BookportError("This is a Topaz Kindle book, which bookport can't read")
    if data[:8] == b"\xeaDRMION\xee":
        raise DRMError("Kindle KFX", "it is a KFX-ZIP book with DRM")
    if data[:4] == b"CONT":
        raise BookportError("This is a KFX book, which bookport can't read yet")
    pdb = read_pdb(data)
    if pdb.type + pdb.creator not in (b"BOOKMOBI", b"TEXtREAd"):
        raise BookportError("Not a MOBI book")
    records = pdb.records
    h6 = parse_header(records, 0)
    kf8 = None
    if h6.version == 8:
        kf8 = h6
    elif 121 in h6.exth:
        boundary = h6.exth[121][0]
        if isinstance(boundary, int) and 0 < boundary < len(records):
            try:
                kf8 = parse_header(records, boundary)
                if kf8.version != 8:
                    kf8 = None
            except (BookportError, struct.error, IndexError):
                kf8 = None
    if h6.encryption or (kf8 and kf8.encryption):
        raise DRMError("Kindle" if kf8 else "MOBI", "it is encrypted (Mobipocket/Amazon DRM)")
    if kf8 is not None:
        try:
            book = _read_kf8(records, kf8, h6)
            book.source_format = "azw3"
            return book
        except DRMError:
            raise
        except (BookportError, struct.error, IndexError, KeyError, ValueError, zlib.error):
            if kf8 is h6:
                raise
    book = _read_mobi6(records, h6)
    book.source_format = "mobi"
    return book


def _resources(records: list[bytes], h: MobiHeader, base: int) -> list[bytes | None]:
    """Resource records from the first image index (relative to ``base``) onwards."""
    out = []
    if h.first_image == NULL:
        return out
    start = base + h.first_image
    for r in records[start:]:
        if r[:4] in (b"FLIS", b"FCIS", b"SRCS", b"DATP", b"RESC", b"BOUN", b"CRES", b"CONT", b"kind", b"\xe9\x8e\r\n",
                     b"FDST", b"INDX", b"CMET", b"PAGE", b"AUDI", b"VIDE"):
            if r[:4] in (b"BOUN", b"\xe9\x8e\r\n", b"CONT", b"FLIS", b"FCIS"):
                if r[:4] in (b"BOUN", b"\xe9\x8e\r\n"):
                    break
                out.append(None)
                continue
            out.append(None)
            continue
        if r[:4] == b"FONT":
            out.append(r)
            continue
        out.append(r if sniff_image(r) else None)
    return out


def _add_images(book: Book, resources: list) -> dict[int, str]:
    hrefs = {}
    for i, r in enumerate(resources, 1):
        if r is None:
            continue
        if r[:4] == b"FONT":
            font = _font(r)
            if font is not None:
                ext = ".otf" if font[:4] == b"OTTO" else ".ttf"
                hrefs[i] = book.add(f"fonts/font{i:04d}{ext}", font).href
            continue
        mt = sniff_image(r)
        if mt is None:
            continue
        hrefs[i] = book.add(f"images/image{i:04d}{IMAGE_EXT.get(mt, '.bin')}", r, mt).href
    return hrefs


def _font(rec: bytes) -> bytes | None:
    try:
        _usize, flags, dstart, xor_len, xor_start = struct.unpack(">IIIII", rec[4:24])
    except struct.error:
        return None
    data = bytearray(rec[dstart:])
    key = rec[xor_start:xor_start + xor_len] if flags & 0x2 else b""
    for i in range(min(1040, len(data)) if key else 0):
        data[i] ^= key[i % len(key)]
    if flags & 0x1:
        try:
            data = limits.inflate(bytes(data))
        except zlib.error:
            return None
    return bytes(data)


def _cover(book: Book, h: MobiHeader, images: dict[int, str]) -> None:
    off = h.exth.get(201, [None])[0]
    if isinstance(off, int) and off != NULL and off + 1 in images:
        book.cover = images[off + 1]
        return
    uri = h.exth.get(129, [""])[0]
    m = re.search(r"kindle:embed:([0-9A-V]+)", uri or "")
    if m and int(m.group(1), 32) in images:
        book.cover = images[int(m.group(1), 32)]


# --- MOBI 6 ----------------------------------------------------------------------------------------

_FILEPOS = re.compile(rb"""filepos\s*=\s*["']?0*(\d+)["']?""", re.IGNORECASE)
_RECINDEX = re.compile(rb"""recindex\s*=\s*["']?(\d+)["']?""", re.IGNORECASE)


def _read_mobi6(records: list[bytes], h: MobiHeader) -> Book:
    text = _text(records, h)
    book = Book(_metadata(h))
    if h.mobi_type == 0 and not book.metadata.title:
        book.metadata.title = h.title
    resources = _resources(records, h, 0)
    images = _add_images(book, resources)
    _cover(book, h, images)
    # TOC positions from the NCX index
    toc_flat = []
    if h.ncx_index != NULL:
        try:
            entries, cncx = read_index(records, h.ncx_index, h.encoding)
        except (BookportError, struct.error, IndexError):
            entries, cncx = [], {}
        for ident, tags in entries:
            pos = tags.get(1, [None])[0]
            label = cncx.get(tags.get(3, [None])[0], ident)
            depth = tags.get(4, [0])[0]
            if pos is not None:
                toc_flat.append((depth, label, pos, tags.get(21, [None])[0]))
    positions = {int(m.group(1)) for m in _FILEPOS.finditer(text)} | {p for _d, _l, p, _ in toc_flat}
    guide = re.search(rb"<guide>(.*?)</guide>", text, re.DOTALL | re.IGNORECASE)
    landmarks = []
    if guide:
        for ref in re.finditer(rb"<reference([^>]*)>", guide.group(1), re.IGNORECASE):
            attrs = ref.group(1)
            t = re.search(rb"""type\s*=\s*["']([^"']+)""", attrs)
            ti = re.search(rb"""title\s*=\s*["']([^"']*)""", attrs)
            fp = _FILEPOS.search(attrs)
            if t and fp:
                pos = int(fp.group(1))
                positions.add(pos)
                landmarks.append((t.group(1).decode("ascii", "replace").lower(),
                                  ti.group(1).decode(h.encoding, "replace") if ti else "", pos))
    html = _insert_anchors(text, positions)
    html = _RECINDEX.sub(lambda m: b'src="' + _img_src(images, int(m.group(1))) + b'"', html)
    html = re.sub(rb"<guide>.*?</guide>", b"", html, flags=re.DOTALL | re.IGNORECASE)
    # split at page breaks into chapter documents
    pieces = re.split(rb"<mbp:pagebreak\s*/?>", html, flags=re.IGNORECASE)
    pieces = [p for p in pieces if re.sub(rb"<[^>]+>|\s", b"", p) or re.search(rb"<img|id=\"filepos", p)]
    if not pieces:
        pieces = [html]
    anchor_doc: dict[int, str] = {}
    head_html = b""
    m = re.search(rb"<head[^>]*>(.*?)</head>", html, re.DOTALL | re.IGNORECASE)
    if m:
        head_html = m.group(1)
    styles = b"".join(re.findall(rb"<style[^>]*>.*?</style>", head_html, re.DOTALL | re.IGNORECASE))
    docs = []
    for n, piece in enumerate(pieces, 1):
        href = f"text/part{n:04d}.xhtml"
        piece = re.sub(rb"</?(html|body|head)[^>]*>", b"", piece, flags=re.IGNORECASE)
        piece = re.sub(rb"<head[^>]*>.*?</head>", b"", piece, flags=re.DOTALL | re.IGNORECASE)
        piece = re.sub(rb"<title>.*?</title>|<meta[^>]*>|<style[^>]*>.*?</style>", b"", piece, flags=re.DOTALL | re.IGNORECASE)
        for a in re.finditer(rb'id="filepos(\d+)"', piece):
            anchor_doc[int(a.group(1))] = href
        docs.append((href, piece))
    lang = book.metadata.language
    for href, piece in docs:
        src = (b'<html><head><meta charset="utf-8"/><title>' + markup.escape(book.metadata.title).encode("utf-8")
               + b"</title>" + styles + b"</head><body>" + piece + b"</body></html>")
        root = markup.parse(src.decode(h.encoding, "replace"))
        _mobi6_fix(root, href, anchor_doc)
        if lang:
            root.set("lang", lang)
        book.add_document(href, root)

    def target(pos):
        if pos in anchor_doc:
            return f"{anchor_doc[pos]}#filepos{pos}"
        before = [p for p in anchor_doc if p <= pos]
        return f"{anchor_doc[max(before)]}#filepos{max(before)}" if before else book.spine[0]

    if toc_flat:
        book.toc = _toc_tree([(d, TocEntry(label, target(pos)), parent, pos) for d, label, pos, parent in toc_flat])
    for kind_, title, pos in landmarks:
        from .epub import _guide_type
        book.landmarks.append(Landmark(_guide_type(kind_), title, target(pos)))
    if not book.toc:
        book.ensure_toc()
    return book


def _toc_tree(items) -> list[TocEntry]:
    """NCX entries to a tree: by parent links when the index has them, else by depth in order.

    ``items`` are (depth, entry, parent index or None, text position) in index order.
    """
    if any(p is not None for _d, _e, p, _pos in items):
        top, kids = [], {}
        for i, (_d, e, p, pos) in enumerate(items):
            if p is None or not 0 <= p < len(items) or p == i:
                top.append((pos, i, e))
            else:
                kids.setdefault(p, []).append((pos, i, e))
        for p, children in kids.items():
            items[p][1].children = [e for _pos, _i, e in sorted(children, key=lambda c: (c[0], c[1]))]
        return [e for _pos, _i, e in sorted(top, key=lambda c: (c[0], c[1]))]
    from ..toc import nest
    return nest([(d + 1, e) for d, e, _p, _pos in items])


def _img_src(images, idx):
    href = images.get(idx)
    return ("../" + href).encode("utf-8") if href else b""


def _insert_anchors(text: bytes, positions) -> bytes:
    """Put <a id="fileposN"/> at every linked byte offset, moved off any tag it would split."""
    out = []
    last = 0
    for pos in sorted(p for p in positions if 0 <= p <= len(text)):
        p = pos
        # if the offset falls inside a tag, move to the start of that tag
        lt = text.rfind(b"<", 0, p)
        gt = text.rfind(b">", 0, p)
        if lt > gt:
            p = lt
        p = max(p, last)
        out.append(text[last:p])
        out.append(b'<a id="filepos%d"></a>' % pos)
        last = p
    out.append(text[last:])
    return b"".join(out)


def _mobi6_fix(root: ET.Element, href: str, anchor_doc: dict) -> None:
    for el in root.iter():
        if el.tag == "a" and el.get("filepos") is not None:
            try:
                pos = int(el.attrib.pop("filepos"))
            except ValueError:
                continue
            doc = anchor_doc.get(pos)
            if doc is None:
                before = [p for p in anchor_doc if p <= pos]
                if before:
                    pos = max(before)
                    doc = anchor_doc[pos]
            if doc:
                el.set("href", relative(href, f"{doc}#filepos{pos}"))
        if el.tag == "img":
            for k in ("hirecindex", "lorecindex", "recindex"):
                el.attrib.pop(k, None)
            if not el.get("src"):
                el.set("src", "")
        if el.tag in ("mbp:nu", "nu"):
            el.tag = "span"
        if ":" in el.tag:
            el.tag = "span" if el.tag not in ("mbp:pagebreak",) else "div"
        for k in list(el.attrib):
            if k in ("height", "width") and el.tag in ("p", "div", "blockquote") and el.get(k, "").startswith("-"):
                del el.attrib[k]
        # MOBI uses height/width on blocks for spacing; turn them into CSS margins
        if el.tag in ("p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"):
            ws = []
            if el.get("width"):
                ws.append(f"text-indent: {_mobi_len(el.attrib.pop('width'))}")
            if el.get("height"):
                ws.append(f"margin-top: {_mobi_len(el.attrib.pop('height'))}")
            if ws:
                el.set("style", "; ".join(ws + ([el.get("style")] if el.get("style") else [])))


def _mobi_len(v: str) -> str:
    v = v.strip()
    if v.endswith(("em", "%", "pt")):
        return v
    try:
        return f"{float(v) / 20:g}em" if float(v) else "0"
    except ValueError:
        return "0"


# --- KF8 -----------------------------------------------------------------------------------------

def _b32(s: str) -> int:
    return int(s, 32)


def _read_kf8(records: list[bytes], h: MobiHeader, h6: MobiHeader) -> Book:
    base = h.offset
    text = _text(records, h)
    # flows
    flows = [text]
    if h.fdst != NULL and base + h.fdst < len(records):
        fd = records[base + h.fdst]
        if fd[:4] == b"FDST":
            count = struct.unpack(">I", fd[8:12])[0]
            bounds = [struct.unpack(">II", fd[12 + 8 * i:20 + 8 * i]) for i in range(count)]
            flows = [text[s:e] for s, e in bounds]
    book = Book(_metadata(h if h.exth else h6))
    if not book.metadata.title:
        book.metadata.title = h.title or h6.title
    resources = _resources(records, h, base)
    if not any(resources) and h6 is not h:
        resources = _resources(records, h6, 0)
    images = _add_images(book, resources)
    _cover(book, h, images)
    if book.cover is None and h6 is not h:
        _cover(book, h6, images)
    skel, _ = read_index(records, base + h.skel_index if h.skel_index != NULL else None, h.encoding)
    frags, _frag_cncx = read_index(records, base + h.frag_index if h.frag_index != NULL else None, h.encoding)
    raw = flows[0]
    files = []  # (number, start, html bytes, [(fragment number, absolute insert offset within file, length)])
    frag_i = 0
    frag_table = []  # per fragment: (file index, insert position in the file's final text, length)
    for fnum, (ident, tags) in enumerate(skel):
        count = tags.get(1, [0])[0]
        start, length = tags.get(6, [0, 0])[:2]
        skeleton = raw[start:start + length]
        baseptr = start + length
        placed = []
        for _ in range(count):
            if frag_i >= len(frags):
                break
            fident, ftags = frags[frag_i]
            insert = int(fident) if fident.isdigit() else ftags.get(6, [0])[0]
            flen = ftags.get(6, [0, 0])[1] if len(ftags.get(6, [])) > 1 else 0
            data = raw[baseptr:baseptr + flen]
            ipos = insert - start
            # earlier fragments in this file shift later insert points
            skeleton = skeleton[:ipos] + data + skeleton[ipos:]
            placed.append((frag_i, ipos, flen))
            baseptr += flen
            frag_i += 1
        files.append(skeleton)
        for fi, ipos, flen in placed:
            frag_table.append((fi, fnum, ipos, flen))
    if not skel:
        files = [raw]
    frag_pos = {fi: (fnum, ipos) for fi, fnum, ipos, _l in frag_table}
    # pass 1: give every kindle:pos target an id
    targets: dict[tuple[int, int], tuple[int, int]] = {}  # (fid, off) -> (file, offset)
    pos_re = re.compile(rb"kindle:pos:fid:([0-9A-V]{4}):off:([0-9A-V]{10})")
    nav_targets = []
    ncx_entries, ncx_cncx = [], {}
    if h.ncx_index != NULL:
        try:
            ncx_entries, ncx_cncx = read_index(records, base + h.ncx_index, h.encoding)
        except (BookportError, struct.error, IndexError):
            ncx_entries = []
    for _ident, tags in ncx_entries:
        if 6 in tags and len(tags[6]) >= 2:
            nav_targets.append((tags[6][0], tags[6][1]))
    guide_entries, guide_cncx = [], {}
    if h.guide_index != NULL:
        try:
            guide_entries, guide_cncx = read_index(records, base + h.guide_index, h.encoding)
        except (BookportError, struct.error, IndexError):
            guide_entries = []
    for _ident, tags in guide_entries:
        if 6 in tags and len(tags[6]) >= 2:
            nav_targets.append((tags[6][0], tags[6][1]))
    wanted: dict[int, set] = {}
    for fdata in files:
        pass
    all_pos = set(nav_targets)
    for fdata in files:
        for m in pos_re.finditer(fdata):
            all_pos.add((_b32(m.group(1).decode()), _b32(m.group(2).decode())))
    for fid, off in all_pos:
        if fid in frag_pos:
            fnum, ipos = frag_pos[fid]
            targets[(fid, off)] = (fnum, ipos + off)
            wanted.setdefault(fnum, set()).add(ipos + off)
        elif fid < len(files):
            targets[(fid, off)] = (fid, off)
            wanted.setdefault(fid, set()).add(off)
    # insert ids at target offsets (on the tag found there)
    id_at: dict[tuple[int, int], str] = {}
    new_files = []
    for fnum, fdata in enumerate(files):
        offs = sorted(wanted.get(fnum, ()), reverse=True)
        fdata = bytearray(fdata)
        for off in offs:
            off = min(off, len(fdata))
            lt = fdata.find(b"<", off)
            if lt < 0:
                continue
            gt = fdata.find(b">", lt)
            tag = bytes(fdata[lt:gt])
            if tag.startswith((b"</", b"<!", b"<?")):
                anchor = b'<a id="pos%d_%d"></a>' % (fnum, off)
                fdata[lt:lt] = anchor
                id_at[(fnum, off)] = f"pos{fnum}_{off}"
                continue
            m = re.search(rb"""\sid\s*=\s*["']([^"']+)["']""", tag)
            if m:
                id_at[(fnum, off)] = m.group(1).decode(h.encoding, "replace")
            else:
                ident = f"pos{fnum}_{off}"
                name_end = lt + 1
                while name_end < gt and fdata[name_end] not in b" \t\r\n/>":
                    name_end += 1
                fdata[name_end:name_end] = f' id="{ident}"'.encode()
                id_at[(fnum, off)] = ident
        new_files.append(bytes(fdata))
    files = new_files
    hrefs = [f"text/part{n:04d}.xhtml" for n in range(len(files))]

    def pos_link(fid, off, from_href):
        t = targets.get((fid, off))
        if t is None:
            return ""
        fnum, foff = t
        ident = id_at.get((fnum, foff))
        target = hrefs[fnum] + (f"#{ident}" if ident and foff > 0 else "")
        return relative(from_href, target) if from_href else target

    css_hrefs = {}
    for i, flow in enumerate(flows[1:], 1):
        if flow.lstrip().startswith(b"<svg") or b"<svg" in flow[:200]:
            svg_href = f"images/flow{i:04d}.svg"
            flow = re.sub(rb"kindle:embed:([0-9A-V]{4})(\?mime=[\w/+-]+)?",
                          lambda m, base=svg_href: _embed(images, _b32(m.group(1).decode()), base).encode(), flow)
            css_hrefs[i] = book.add(svg_href, flow, "image/svg+xml").href
        else:
            css_hrefs[i] = book.add(f"styles/flow{i:04d}.css", _fix_css(flow, images), "text/css").href
    for fnum, fdata in enumerate(files):
        href = hrefs[fnum]
        fdata = pos_re.sub(lambda m, h=href: pos_link(_b32(m.group(1).decode()), _b32(m.group(2).decode()), h).encode(),
                           fdata)
        fdata = re.sub(rb"kindle:embed:([0-9A-V]{4})(\?mime=[\w/+-]+)?",
                       lambda m, h=href: _embed(images, _b32(m.group(1).decode()), h).encode(), fdata)
        fdata = re.sub(rb"kindle:flow:([0-9A-V]{4})(\?mime=[\w/+-]+)?",
                       lambda m, h=href: (relative(h, css_hrefs[_b32(m.group(1).decode())])
                                          if _b32(m.group(1).decode()) in css_hrefs else "").encode(), fdata)
        root = markup.parse(fdata.decode(h.encoding, "replace"))
        for el in root.iter():
            for k in ("aid", "data-AmznRemoved", "data-amznremoved", "data-AmznRemoved-M8", "data-amznremoved-m8",
                      "data-AmznPageBreak"):
                el.attrib.pop(k, None)
        book.add_document(href, root)
    # inline SVG flows referenced through <img src="...svg"> are fine as images
    book.toc = []
    if ncx_entries:
        items = []
        for ident, tags in ncx_entries:
            label = ncx_cncx.get(tags.get(3, [None])[0], ident)
            if 6 in tags and len(tags[6]) >= 2:
                href = pos_link(tags[6][0], tags[6][1], "")
            else:
                href = hrefs[0]
            items.append((tags.get(4, [0])[0], TocEntry(label, href), tags.get(21, [None])[0], tags.get(1, [0])[0]))
        book.toc = _toc_tree(items)
    from .epub import _guide_type
    for ident, tags in guide_entries:
        if 6 in tags and len(tags[6]) >= 2:
            title = guide_cncx.get(tags.get(1, [None])[0], ident)
            book.landmarks.append(Landmark(_guide_type(ident), title, pos_link(tags[6][0], tags[6][1], "")))
    if book.cover is None:
        lm = next((lm for lm in book.landmarks if lm.type == "cover"), None)
        if lm is not None:
            from .epub import _only_image
            book.cover = _only_image(book, lm.href.split("#")[0])
    if not book.toc:
        book.ensure_toc()
    if h.exth.get(527, [""])[0] == "rtl":
        book.direction = "rtl"
    book.fixed_layout = (h.exth.get(122, [""])[0] or "").lower() == "true"
    return book


def _embed(images, idx, href) -> str:
    target = images.get(idx)
    return relative(href, target) if target else ""


def _fix_css(flow: bytes, images) -> bytes:
    return re.sub(rb"kindle:embed:([0-9A-V]{4})(\?mime=[\w/+-]+)?",
                  lambda m: ("../" + images[_b32(m.group(1).decode())]).encode() if _b32(m.group(1).decode()) in images else b"",
                  flow)


def write(book: Book, path, fmt: str = "mobi", compress: bool = True, fonts: bool = True) -> None:
    from . import mobi_write
    mobi_write.write(book, path, fmt=fmt, compress=compress, fonts=fonts)
