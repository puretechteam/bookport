"""Comics and image lists: CBZ, CBR (stored RAR), a single image or a folder of images.

Each image becomes one fixed-layout page (with a viewport, so EPUB, KF8 and PDF keep the page
shape); sub-folders become chapters in the TOC. ComicInfo.xml metadata is read when present,
including right-to-left reading for manga.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import __version__, limits, markup
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
    marks = _comic_info(book, info) if info else []
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
    bookmarked = [TocEntry(title, book.spine[i]) for i, title in marks if i < len(book.spine)]
    if bookmarked:
        toc = bookmarked
    book.toc = toc if len(toc) > 1 or bookmarked else [TocEntry(book.metadata.title or "Start", book.spine[0])]
    return book


def _comic_info(book: Book, data: bytes) -> list[tuple[int, str]]:
    try:
        root = markup.parse_xml(data, "ComicInfo.xml")
    except ET.ParseError:
        return []

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
        month, day = markup.whole(t("Month")), markup.whole(t("Day"))
        md.date = t("Year") + (f"-{month:02d}" if month is not None else "") + (
            f"-{day:02d}" if day is not None and month is not None else "")
    if t("Manga").lower() == "yesandrighttoleft":
        book.direction = "rtl"
    # ComicRack's page bookmarks: (page index, chapter title)
    marks = []
    for page in root.iterfind("Pages/Page"):
        title = " ".join(page.get("Bookmark", "").split())
        index = markup.whole(page.get("Image", ""))
        if title and index is not None:
            marks.append((index, title))
    return marks


# --- writing ------------------------------------------------------------------------------------

WRITES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}  # the pictures comic readers show
TEXT_PAGE_WORDS = 40  # more words than this and a page is text, not a picture with a caption


def write(book: Book, path) -> None:
    """CBZ: each page's pictures in reading order (0001.jpg, 0002.png, ...) and a ComicInfo.xml with
    the title, series, writers, summary, publisher, genres, language, date, reading direction and
    the TOC as page bookmarks. Pages of text can't become pictures, so a book with them is refused."""
    from ..book import resolve
    pictures: list[str] = []
    first_picture: dict[str, int] = {}
    text_pages = []
    for href, _res in book.documents():
        body = book.tree(href).find("body")
        if body is None:
            continue
        if _words(body) > TEXT_PAGE_WORDS:
            text_pages.append(href)
            continue
        for el in body.iter():
            if el.tag not in ("img", "image"):
                continue
            target = resolve(href, el.get("src") or el.get("xlink:href") or el.get("href") or "")
            res = book.resources.get(target)
            if res is not None and res.media_type in WRITES and target not in pictures:
                first_picture.setdefault(href, len(pictures))
                pictures.append(target)
    if text_pages:
        raise BookportError(f"A comic book archive holds only pictures, and {len(text_pages)} page(s) of this book are "
                            "text: write it as PDF or EPUB instead")
    cover = book.resources.get(book.cover) if book.cover else None
    if cover is not None and cover.media_type in WRITES and book.cover not in pictures:
        pictures.insert(0, book.cover)
        first_picture = {k: v + 1 for k, v in first_picture.items()}
    if not pictures:
        raise BookportError("This book has no pictures to make a comic of")
    marks: dict[int, str] = {}
    for _depth, e in book.toc_walk():
        page = first_picture.get(e.href.split("#")[0])
        if page is not None and e.title.strip():
            marks.setdefault(page, " ".join(e.title.split()))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:  # pictures are compressed already
        for n, href in enumerate(pictures, 1):
            res = book.resources[href]
            z.writestr(f"{n:04d}{IMAGE_EXT[res.media_type]}", res.data)
        z.writestr("ComicInfo.xml", _comic_info_xml(book, len(pictures), marks), zipfile.ZIP_DEFLATED)
        z.comment = _comic_book_info(book)


def _comic_book_info(book: Book) -> bytes:
    """The same metadata as ComicBookInfo 1.0 JSON in the ZIP comment, which calibre and other
    library tools read instead of ComicInfo.xml (a ZIP comment holds at most 65,535 bytes)."""
    md = book.metadata
    info: dict = {"title": md.title, "series": md.series, "publisher": md.publisher, "comments": md.description,
                  "language": md.language, "genre": ", ".join(md.subjects), "tags": md.subjects,
                  "credits": [{"person": a, "role": "Writer", "primary": True} for a in md.authors]}
    if md.series and md.series_index is not None:
        info["issue"] = f"{md.series_index:g}"
    date = re.match(r"(\d{4})(?:-(\d\d))?", md.date or "")
    if date:
        info["publicationYear"] = int(date[1])
        if date[2]:
            info["publicationMonth"] = int(date[2])
    info = {k: v for k, v in info.items() if v}
    data = json.dumps({"appID": f"bookport/{__version__}", "ComicBookInfo/1.0": info}, ensure_ascii=False).encode()
    if len(data) > 0xFFFF:  # a very long summary: leave it out
        info.pop("comments", None)
        data = json.dumps({"appID": f"bookport/{__version__}", "ComicBookInfo/1.0": info}, ensure_ascii=False).encode()
    return data if len(data) <= 0xFFFF else b""


def _words(body: ET.Element) -> int:
    n = 0
    for el in body.iter():
        if el.tag not in ("style", "script"):
            n += len(re.findall(r"\w+", el.text or ""))
        if el is not body:
            n += len(re.findall(r"\w+", el.tail or ""))
    return n


def _comic_info_xml(book: Book, pages: int, marks: dict[int, str]) -> bytes:
    md = book.metadata
    root = ET.Element("ComicInfo")

    def add(tag, value):
        if value:
            ET.SubElement(root, tag).text = str(value)

    # in the order ComicInfo's schema lists them
    add("Title", md.title)
    add("Series", md.series)
    if md.series and md.series_index is not None:
        add("Number", f"{md.series_index:g}")
    add("Summary", md.description)
    date = re.match(r"(\d{4})(?:-(\d\d)(?:-(\d\d))?)?", md.date or "")
    if date:
        add("Year", int(date[1]))
        add("Month", int(date[2]) if date[2] else "")
        add("Day", int(date[3]) if date[3] else "")
    add("Writer", ", ".join(md.authors))
    add("Publisher", md.publisher)
    add("Genre", ", ".join(md.subjects))
    add("PageCount", pages)
    add("LanguageISO", md.language)
    if book.direction == "rtl":
        add("Manga", "YesAndRightToLeft")
    if marks:
        listing = ET.SubElement(root, "Pages")
        for index, title in sorted(marks.items()):
            ET.SubElement(listing, "Page", {"Image": str(index), "Bookmark": title})
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")


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


def _rar5_encrypted(data: bytes, pos: int, end: int) -> bool:
    """Whether a RAR 5 file header's extra area (``pos`` to ``end``) has a file encryption record (type 1):
    a password on the files only, which leaves the archive's headers readable."""
    while 0 <= pos < end:
        size, q = _vint(data, pos)  # the size counts from the record's type on
        kind, _ = _vint(data, q)
        if kind == 1:
            return True
        if size == 0:
            break
        pos = q + size
    return False


def _rar5(data: bytes):
    pos = 8
    n = len(data)
    while pos + 4 < n:
        pos += 4  # header CRC32
        hsize, p = _vint(data, pos)
        hstart = p
        htype, p = _vint(data, p)
        hflags, p = _vint(data, p)
        dsize = extra = 0
        if hflags & 1:
            extra, p = _vint(data, p)
        if hflags & 2:
            dsize, p = _vint(data, p)
        if htype == 4:
            raise DRMError("comic", "the archive is encrypted (password protected)")
        if htype == 2 and extra and _rar5_encrypted(data, hstart + hsize - extra, hstart + hsize):
            raise DRMError("comic", "its pages are encrypted (password protected)")
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
