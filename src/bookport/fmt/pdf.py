"""PDF: write with bookport's own typesetter; read with its own parser, reflowing pages to text."""

from __future__ import annotations

from pathlib import Path

from ..book import Book


def write(book: Book, path, page_size: str | None = None, text_size: str | None = None, **_) -> None:
    from ..pdf.typeset import typeset
    Path(path).write_bytes(typeset(book, page_size, text_size))


def read(path) -> Book:
    from ..pdf.reader import read_pdf
    return read_pdf(Path(path))
