"""Shanda Bambook (SNB): read and write.

Layout: a 44-byte header ("SNBP000B", file count, file-table and stream sizes), the zlib-packed
file table (attributes, name offset and size for each file, then the names), the binary stream
(images, stored), the text stream (bz2 in 32 KB blocks), and a tail (block offsets, then each
file's block and offset) found through the last 16 bytes. Inside, snbf/book.snbf holds the
metadata, snbf/toc.snbf the chapters, and each snbc/*.snbc a chapter of <text> and <img> lines.
"""

from __future__ import annotations

import bz2
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from .. import limits, markup
from ..book import Book, Metadata, TocEntry, relative, sniff_image
from ..errors import BookportError

MAGIC = b"SNBP000B"
BLOCK = 0x8000
BINARY, TEXT = 0x01000000, 0x41000000


def _files(data: bytes) -> dict[str, bytes]:
    if not data.startswith(MAGIC[:4]):
        raise BookportError("Not an SNB file")
    try:
        file_count, _vfat_size, vfat_comp, bin_size, plain_size = struct.unpack(">IIIII", data[20:40])
        vfat = limits.inflate(data[44:44 + vfat_comp])
        entries = [struct.unpack(">III", vfat[12 * i:12 * i + 12]) for i in range(file_count)]
        names = vfat[12 * file_count:].split(b"\0")
        tail_size, tail_off = struct.unpack(">II", data[-16:-8])
        tail = limits.inflate(data[tail_off:tail_off + tail_size])
    except (struct.error, zlib.error, IndexError) as e:
        raise BookportError(f"Damaged SNB file: {e}") from None
    n_bin = (bin_size + BLOCK - 1) // BLOCK
    n_plain = (plain_size + BLOCK - 1) // BLOCK
    n = n_bin + n_plain
    n = min(n, len(tail) // 4)
    blocks = [struct.unpack(">I", tail[4 * i:4 * i + 4])[0] for i in range(n)]
    ends = blocks[1:] + [tail_off]
    n_bin = min(n_bin, n)
    bin_data = b"".join(data[blocks[i]:ends[i]] for i in range(n_bin))[:bin_size]
    pieces = []
    total = 0
    for i in range(n_bin, n):
        block = data[blocks[i]:ends[i]]
        left = limits.MAX_PART - total
        try:
            piece = limits.bunzip(block, left)
        except (OSError, ValueError):
            try:
                piece = limits.inflate(block, limit=left) if block[:1] == b"x" else b""
            except zlib.error:
                piece = b""
        pieces.append(piece)
        total += len(piece)
    plain = b"".join(pieces)
    out = {}
    bpos = ppos = 0
    for (attr, _off, size), name in zip(entries, names):
        key = name.decode("utf-8", "replace")
        if attr & 0x40000000:
            out[key] = plain[ppos:ppos + size]
            ppos += size
        else:
            out[key] = bin_data[bpos:bpos + size]
            bpos += size
    return out


def read(path) -> Book:
    files = _files(Path(path).read_bytes())
    book = Book(Metadata(title=Path(path).stem))
    book.source_format = "snb"
    head = _xml(files.get("snbf/book.snbf", b""))
    if head is not None:
        def t(tag):
            el = head.find(f"head/{tag}")
            return (el.text or "").strip() if el is not None else ""
        md = book.metadata
        md.title = t("name") or md.title
        if t("author"):
            md.authors = [a.strip() for a in re.split(r"[;,]", t("author")) if a.strip()]
        lang = t("language").lower()
        md.language = {"eng": "en", "zho": "zh", "chs": "zh", "cht": "zh-TW", "fra": "fr", "deu": "de", "jpn": "ja"}.get(lang, lang)
        md.rights, md.publisher, md.description = t("rights"), t("publisher"), t("abstract")
        if re.match(r"\d{4}(-\d\d){0,2}$", t("created")):
            md.date = t("created")
        cover_name = t("cover")
    else:
        cover_name = ""
    images = {}
    for name, data in files.items():
        if name.startswith("snbc/images/") and sniff_image(data):
            images[name.split("/")[-1]] = book.add("images/" + re.sub(r"[^\w.-]", "_", name.split("/")[-1]), data).href
    toc = _xml(files.get("snbf/toc.snbf", b""))
    chapters = []
    if toc is not None:
        chapters = [(c.get("src", ""), (c.text or "").strip()) for c in toc.iter("chapter")]
    if not chapters:
        chapters = [(n.split("/")[-1], "") for n in sorted(files) if n.endswith(".snbc")]
    for k, (src, title) in enumerate(chapters, 1):
        data = files.get(f"snbc/{src}")
        if data is None:
            continue
        doc = _xml(data)
        href = f"text/part{k:04d}.xhtml"
        root, body = markup.new_document(title or book.metadata.title, book.metadata.language)
        if doc is not None:
            for el in doc.find("body") if doc.find("body") is not None else []:
                if el.tag == "text":
                    p = ET.SubElement(body, "p")
                    p.text = (el.text or "").strip().lstrip("　")
                elif el.tag == "img" and (el.text or "").strip() in images:
                    div = ET.SubElement(body, "div")
                    ET.SubElement(div, "img", {"src": relative(href, images[el.text.strip()]), "alt": ""})
        if not len(body):
            ET.SubElement(body, "h2").text = title
        book.add_document(href, root)
        if title:
            book.toc.append(TocEntry(title, href))
    if cover_name in images:
        book.cover = images[cover_name]
    if not book.spine:
        raise BookportError("This SNB file has no chapters")
    return book


def _xml(data: bytes):
    if not data:
        return None
    try:
        return markup.parse_xml(data, "This SNB file")
    except ET.ParseError:
        return None


def write(book: Book, path, **_) -> None:
    from ..flow import book_blocks
    from ..images import to_png
    md = book.metadata
    text_files: list[tuple[str, bytes]] = []
    bin_files: list[tuple[str, bytes]] = []
    image_names: dict[str, str] = {}

    def image(href):
        if href not in image_names:
            res = book.resources.get(href)
            if res is None or not res.is_image:
                return None
            data = res.data
            if res.media_type not in ("image/jpeg", "image/png"):
                data = to_png(data)
                if data is None:
                    return None
            name = f"image{len(image_names) + 1:04d}" + (".jpg" if data[:2] == b"\xff\xd8" else ".png")
            image_names[href] = name
            bin_files.append((f"snbc/images/{name}", data))
        return image_names[href]

    # one chapter per TOC entry, nested ones too since the SNB TOC is flat (else per document)
    chapters: list[tuple[str, list]] = []
    starts = {}
    for _depth, e in book.toc_walk():
        starts.setdefault(e.href, " ".join(e.title.split()))
    cur_title, cur = (md.title, [])
    for b in book_blocks(book):
        hit = next((starts.pop(a) for a in b.anchors if a in starts), None)
        if hit is not None and cur:
            chapters.append((cur_title, cur))
            cur_title, cur = hit, []
        elif hit is not None:
            cur_title = hit
        cur.append(b)
    if cur:
        chapters.append((cur_title, cur))
    toc_items = []
    for k, (title, blocks) in enumerate(chapters, 1):
        name = f"chapter{k:04d}.snbc"
        lines = [f"<snbc>\n  <head>\n    <title>{markup.escape(title)}</title>\n  </head>\n  <body>"]
        for b in blocks:
            if b.kind == "image":
                n = image(b.image)
                if n:
                    lines.append(f"    <img>{n}</img>")
                continue
            for src in b.images():
                n = image(src)
                if n:
                    lines.append(f"    <img>{n}</img>")
            text = b.text() if b.kind != "table" else b.text().replace("\t", "  ")
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    lines.append(f"    <text><![CDATA[　　{line.replace(']]>', ']] >')}]]></text>")
        lines.append("  </body>\n</snbc>\n")
        text_files.append((f"snbc/{name}", "\n".join(lines).encode("utf-8")))
        toc_items.append(f'    <chapter src="{name}">{markup.escape(title)}</chapter>')
    cover = image(book.cover) if book.cover else ""
    lang = {"en": "ENG", "zh": "ZHO", "fr": "FRA", "de": "DEU", "ja": "JPN"}.get((md.language or "en").split("-")[0], "ENG")
    book_snbf = ('<book-snbf version="1.0">\n  <head>\n'
                 f"    <name>{markup.escape(md.title)}</name>\n    <author>{markup.escape('; '.join(md.authors))}</author>\n"
                 f"    <language>{lang}</language>\n    <rights>{markup.escape(md.rights)}</rights>\n"
                 f"    <publisher>{markup.escape(md.publisher)}</publisher>\n    <generator>bookport</generator>\n"
                 f"    <created>{markup.escape(md.date)}</created>\n"
                 f"    <abstract>{markup.escape(re.sub(r'<[^>]+>', '', md.description))}</abstract>\n"
                 f"    <cover>{cover or ''}</cover>\n  </head>\n</book-snbf>\n")
    toc_snbf = (f"<toc-snbf>\n  <head>\n    <chapters>{len(toc_items)}</chapters>\n  </head>\n  <body>\n"
                + "\n".join(toc_items) + "\n  </body>\n</toc-snbf>\n")
    text_files += [("snbf/book.snbf", book_snbf.encode("utf-8")), ("snbf/toc.snbf", toc_snbf.encode("utf-8"))]
    Path(path).write_bytes(pack(bin_files, text_files))


def pack(bin_files: list[tuple[str, bytes]], text_files: list[tuple[str, bytes]]) -> bytes:
    files = [(n, d, BINARY) for n, d in bin_files] + [(n, d, TEXT) for n, d in text_files]
    names = b""
    table = b""
    for name, data, attr in files:
        table += struct.pack(">III", attr, len(names), len(data))
        names += name.encode("utf-8") + b"\0"
    vfat = table + names
    vfat_comp = zlib.compress(vfat, 9)
    bin_stream = b"".join(d for _n, d, a in files if a == BINARY)
    plain = b"".join(d for _n, d, a in files if a == TEXT)
    header_size = 44
    out = bytearray()
    blocks = []
    pos = header_size + len(vfat_comp)
    for i in range(0, len(bin_stream), BLOCK):
        blocks.append(pos + i)
    body = bytearray(bin_stream)
    for i in range(0, len(plain), BLOCK):
        blocks.append(header_size + len(vfat_comp) + len(body))
        body += bz2.compress(plain[i:i + BLOCK], 9)
    # where each file starts: (block number, offset in the block)
    starts = b""
    boff = poff = 0
    n_bin = (len(bin_stream) + BLOCK - 1) // BLOCK
    for _name, data, attr in files:
        if attr == BINARY:
            starts += struct.pack(">II", boff // BLOCK, boff % BLOCK)
            boff += len(data)
        else:
            starts += struct.pack(">II", n_bin + poff // BLOCK, poff % BLOCK)
            poff += len(data)
    tail = zlib.compress(b"".join(struct.pack(">I", b) for b in blocks) + starts, 9)
    tail_off = header_size + len(vfat_comp) + len(body)
    header = MAGIC + struct.pack(">IIIIIIIII", BLOCK, 0x00A3A3A3, 0, len(files), len(vfat), len(vfat_comp), len(bin_stream),
                                 len(plain), 0)
    out += header + vfat_comp + body + tail + struct.pack(">II", len(tail), tail_off) + MAGIC
    return bytes(out)
