"""PostScript and EPS as a book: bookport.ps runs the program into a PDF, which is then read like any PDF (its text,
pictures and pages). For PostScript to PDF itself, with every page kept as it was drawn, use ``bookport.ps.to_pdf``
(``bookport.convert`` to a .pdf does)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..book import Book


def read(path) -> Book:
    from .. import ps
    from ..pdf.reader import read_pdf
    p = Path(path)
    notes: list[str] = []
    pdf = ps.to_pdf(p, notes=notes)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix=".bookport-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf)
        book = read_pdf(Path(tmp))
    finally:
        os.unlink(tmp)
    book.source_format = "eps" if p.suffix.lower() in (".eps", ".epsf", ".epsi") else "ps"
    book.lost += notes
    if not book.metadata.title:
        book.metadata.title = p.stem
    return book
