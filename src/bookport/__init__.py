"""bookport: read and write ebooks in every common and rare format, in pure Python.

    import bookport
    book = bookport.read("novel.mobi")          # -> Book
    book.write("novel.epub")                     # format from the extension
    bookport.convert("novel.fb2", "novel.pdf", page_size="a5", text_size="normal")
    bookport.formats()                           # {"epub": {"name": ..., "read": True, "write": True}, ...}
    bookport.unpack("help.chm")                  # [("index.html", b"..."), ...]: the files inside a CHM or LIT

or from a terminal: ``bookport novel.mobi novel.epub``.
"""

from __future__ import annotations

__version__ = "0.2.0"

from .book import Book, Landmark, Metadata, Resource, TocEntry
from .errors import BookportError, DRMError
from .registry import convert, detect, formats, read, unpack

__all__ = [
    "Book",
    "BookportError",
    "DRMError",
    "Landmark",
    "Metadata",
    "Resource",
    "TocEntry",
    "__version__",
    "convert",
    "detect",
    "formats",
    "read",
    "unpack",
]
