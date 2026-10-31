"""Comics and image lists: CBZ, CBR (stored RAR), a single image or a folder of images.

Each image becomes one fixed-layout page (with a viewport, so EPUB, KF8 and PDF keep the page
shape); sub-folders become chapters in the TOC. ComicInfo.xml metadata is read when present,
including right-to-left reading for manga.
"""

from __future__ import annotations

import io
import posixpath
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import limits, markup
from ..book import IMAGE_EXT, Book, Metadata, TocEntry, sniff_image
from ..errors import BookportError, DRMError

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def read(path) -> Book:
    p = Path(path)
    if p.is_dir():
        files = sorted((f for f in p.rglob("*") if f.is_file()), key=lambda f: natural_key(f.relative_to(p).as_posix()))
        entries = [(f.relative_to(p).as_posix(), f.read_bytes) for f in files]
        return build(entries, p.name)
    data = p.read_bytes()
    if sniff_image(data):
        return build([(p.name, lambda: data)], p.stem)
    if data[:4] == b"PK\x03\x04" or data[:4] == b"PK\x05\x06":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise BookportError(f"Damaged comic ZIP: {e}") from None
        infos = [i for i in z.infolist() if not i.is_dir()]
        if any(i.flag_bits & 1 for i in infos):
            raise DRMError("comic", "its pages are encrypted (password protected)")
        entries = [(i.filename, (lambda i=i: limits.zip_read(z, i))) for i in infos]
        return build(entries, p.stem)
    if data[:7] == b"Rar!\x1a\x07\x00" or data[:8] == b"Rar!\x1a\x07\x01\x00":
        return build(list(read_stored_rar(data)), p.stem)
    raise BookportError("Not a comic book archive (CBZ, or CBR with stored pages)")


def build(entries, title: str) -> Book:
    book = Book(Metadata(title=title))
    book.source_format = "cbz"
    book.fixed_layout = True
    pages = []
    info = None
    for name, get in sorted(entries, key=lambda e: natural_key(e[0])):
        base = posixpath.basename(name).lower()
        if base == "comicinfo.xml":
            info = get()
            continue
        if base.startswith(".") or "__macosx" in name.lower() or not base.endswith(IMAGE_SUFFIXES):
            continue
        data = get()
        mt = sniff_image(data)
        if mt is None:
            continue
        pages.append((name, data, mt))
    if not pages:
        raise BookportError("This comic has no pages (no images found)")
    if info:
        _comic_info(book, info)
    from .epub import image_size
    folders: dict[str, str] = {}
    toc: list[TocEntry] = []
    for n, (name, data, mt) in enumerate(pages, 1):
        img = book.add(f"images/page{n:04d}{IMAGE_EXT.get(mt, '.jpg')}", data, mt).href
        w, h = image_size(data) or (1072, 1448)
        root, body = markup.new_document(f"Page {n}", book.metadata.language)
        head = root.find("head")
        ET.SubElement(head, "meta", {"name": "viewport", "content": f"width={w}, height={h}"})
        ET.SubElement(head, "style").text = (f"html, body {{ margin: 0; padding: 0; width: {w}px; height: {h}px }} "
                                             "img { display: block; width: 100%; height: 100% }")
        div = ET.SubElement(body, "div")
        ET.SubElement(div, "img", {"src": f"../{img}", "alt": f"Page {n}", "width": str(w), "height": str(h)})
        href = book.add_document(f"pages/page{n:04d}.xhtml", root).href
        folder = posixpath.dirname(name)
        if folder and folder not in folders:
            folders[folder] = href
            toc.append(TocEntry(posixpath.basename(folder).replace("_", " "), href))
        if n == 1:
            book.cover = img
    book.toc = toc if len(toc) > 1 else [TocEntry(book.metadata.title or "Start", book.spine[0])]
    return book


def _comic_info(book: Book, data: bytes) -> None:
    try:
        root = markup.parse_xml(data, "ComicInfo.xml")
    except ET.ParseError:
        return

    def t(tag):
        el = root.find(tag)
        return " ".join((el.text or "").split()) if el is not None and el.text else ""

    md = book.metadata
    title = t("Title")
    series = t("Series")
    if title:
        md.title = title
    elif series:
        md.title = series + (f" {t('Number')}" if t("Number") else "")
    if series:
        md.series = series
        try:
            md.series_index = float(t("Number")) if t("Number") else None
        except ValueError:
            pass
    writers = [a.strip() for a in re.split(r"[,;]", t("Writer")) if a.strip()]
    md.authors = writers
    for role in ("Penciller", "Inker", "Colorist", "Letterer", "CoverArtist", "Editor"):
        md.contributors += [a.strip() for a in re.split(r"[,;]", t(role)) if a.strip()]
    md.description = t("Summary")
    md.publisher = t("Publisher")
    md.language = t("LanguageISO")
    md.subjects = [g.strip() for g in re.split(r"[,;]", t("Genre")) if g.strip()]
    if t("Year"):
        md.date = t("Year") + (f"-{int(t('Month')):02d}" if t("Month").isdigit() else "") + (
            f"-{int(t('Day')):02d}" if t("Day").isdigit() and t("Month").isdigit() else "")
    if t("Manga").lower() == "yesandrighttoleft":
        book.direction = "rtl"


# --- RAR, stored only (from RARLAB's technote) ------------------------------------------------

def read_stored_rar(data: bytes):
    """(name, getter) for each file in a RAR 4 or RAR 5 archive whose files are stored."""
    if data[:8] == b"Rar!\x1a\x07\x01\x00":
        yield from _rar5(data)
        return
    pos = 7
    n = len(data)
    while pos + 7 <= n:
        _crc, kind, flags, size = struct.unpack("<HBHH", data[pos:pos + 7])
        if size < 7:
            break
        add = 0
        if kind == 0x74:  # file header
            pack, _unp, _os, _fcrc, _time, _ver, method, name_len, _attr = struct.unpack("<IIBIIBBHI", data[pos + 7:pos + 32])
            hpos = pos + 32
            if flags & 0x100:
                hi_pack, _hi_unp = struct.unpack("<II", data[hpos:hpos + 8])
                pack |= hi_pack << 32
                hpos += 8
            raw_name = data[hpos:hpos + name_len]
            if flags & 0x200 and b"\0" in raw_name:
                name = raw_name.split(b"\0", 1)[0].decode("latin-1")
            else:
                name = raw_name.decode("utf-8" if flags & 0x200 else "cp437", "replace")
            if flags & 0x4:
                raise DRMError("comic", "its pages are encrypted (password protected)")
            start = pos + size
            if flags & 0xE0 != 0xE0:  # not a directory
                if method != 0x30:
                    raise BookportError("This CBR is compressed; bookport reads only CBR files with stored pages. "
                                        "Re-save it as CBZ first.")
                yield name.replace("\\", "/"), (lambda s=start, p=pack: data[s:s + p])
            add = pack
        elif flags & 0x8000 and kind != 0x74:
            add = struct.unpack("<I", data[pos + 7:pos + 11])[0]
        if kind == 0x7B:  # end of archive
            break
        pos += size + add


def _vint(data, pos):
    value = shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    return value, pos


def _rar5(data: bytes):
    pos = 8
    n = len(data)
    while pos + 4 < n:
        pos += 4  # header CRC32
        hsize, p = _vint(data, pos)
        hstart = p
        htype, p = _vint(data, p)
        hflags, p = _vint(data, p)
        dsize = 0
        if hflags & 1:
            _extra, p = _vint(data, p)
        if hflags & 2:
            dsize, p = _vint(data, p)
        if htype == 4:
            raise DRMError("comic", "the archive is encrypted (password protected)")
        if htype == 2:
            fflags, p = _vint(data, p)
            _unp, p = _vint(data, p)
            _attrs, p = _vint(data, p)
            if fflags & 2:
                p += 4
            if fflags & 4:
                p += 4
            comp, p = _vint(data, p)
            _host, p = _vint(data, p)
            nlen, p = _vint(data, p)
            name = data[p:p + nlen].decode("utf-8", "replace")
            method = (comp >> 7) & 7
            is_dir = fflags & 1
            start = hstart + hsize
            if not is_dir:
                if method != 0:
                    raise BookportError("This CBR is compressed; bookport reads only CBR files with stored pages. "
                                        "Re-save it as CBZ first.")
                yield name, (lambda s=start, d=dsize: data[s:s + d])
        if htype == 5:
            break
        pos = hstart + hsize + dsize
