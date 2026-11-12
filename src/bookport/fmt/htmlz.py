"""HTMLZ: a ZIP holding index.html, its stylesheet and images, and a metadata.opf."""

from __future__ import annotations

import posixpath
import zipfile

from .. import markup
from ..book import Book
from ..errors import BookportError
from . import html as html_fmt
from .zipped import apply_opf, extracted, metadata_opf, text_files


def read(path) -> Book:
    with extracted(path) as root:
        pages = text_files(root, (".html", ".htm", ".xhtml"))
        if not pages:
            raise BookportError("This HTMLZ file has no HTML page in it")
        book = html_fmt.read(pages[0], root=root)
        book.source_format = "htmlz"
        opfs = text_files(root, (".opf",))
        if opfs:
            apply_opf(book, opfs[0], root)
    return book


def write(book: Book, path, **_) -> None:
    page = html_fmt.single_page(book, inline_images=False)
    images: dict[str, str] = {}

    def free_name(href):
        base = posixpath.basename(href)
        name, k = "images/" + base, 1
        while name in images.values():  # two files with one name in different folders
            name, k = f"images/{k}_{base}", k + 1
        return name
    # every resource the page uses moves under images/ (or stays a stylesheet inline)
    used = []
    for el in page.iter():
        for attr in ("src", "xlink:href", "href", "poster"):
            v = el.get(attr)
            if v and v in book.resources and not book.resources[v].is_document:
                name = images.get(v)
                if name is None:
                    name = images[v] = free_name(v)
                    used.append(v)
                el.set(attr, name)
    style = page.find("head/style")
    css = style.text if style is not None else ""
    if style is not None:
        page.find("head").remove(style)
    import xml.etree.ElementTree as ET
    ET.SubElement(page.find("head"), "link", {"rel": "stylesheet", "type": "text/css", "href": "style.css"})
    for href in book.resources:
        if href in css and not book.resources[href].is_document and href not in images:
            images[href] = free_name(href)
    for href, name in images.items():
        css = css.replace(f'"{href}"', f'"{name}"')
    cover_name = None
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:  # straight to the file, not in memory first
        z.writestr("index.html", markup.serialize(page, xml_decl=False))
        z.writestr("style.css", css or "")
        for href, name in images.items():
            z.writestr(name, book.resources[href].data)
        cov = book.cover_image()
        if cov is not None:
            cover_name = "cover" + posixpath.splitext(cov.href)[1]
            z.writestr(cover_name, cov.data)
        z.writestr("metadata.opf", metadata_opf(book, cover_name))
