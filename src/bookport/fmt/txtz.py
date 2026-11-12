"""TXTZ: a ZIP holding index.txt (Markdown, so it stays readable as text), its images and a metadata.opf."""

from __future__ import annotations

import posixpath
import zipfile

from ..book import Book
from ..errors import BookportError
from . import markdown, txt
from .zipped import apply_opf, decode_file, extracted, metadata_opf, text_files


def read(path) -> Book:
    with extracted(path) as root:
        texts = text_files(root, (".txt", ".text", ".md", ".markdown", ".textile"))
        if not texts:
            raise BookportError("This TXTZ file has no text file in it")
        p = texts[0]
        text = decode_file(p)
        if (p.suffix.lower() in (".md", ".markdown") or text.startswith("---\n") or txt.looks_like_markdown(text)
                or "![" in text):
            book = markdown.book_from_markdown(text, p.stem, base_dir=p.parent, top=root)
        else:
            book = txt.book_from_text(text, p.stem, formatting="plain")
        book.source_format = "txtz"
        opfs = text_files(root, (".opf",))
        if opfs:
            apply_opf(book, opfs[0], root)
    return book


def write(book: Book, path, **_) -> None:
    text, images = markdown.book_to_markdown(book)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:  # straight to the file, not in memory first
        z.writestr("index.txt", text)
        for href, name in images.items():
            if href in book.resources:
                z.writestr(name, book.resources[href].data)
        cover_name = None
        cov = book.cover_image()
        if cov is not None:
            cover_name = "cover" + posixpath.splitext(cov.href)[1]
            z.writestr(cover_name, cov.data)
        z.writestr("metadata.opf", metadata_opf(book, cover_name))
