"""PostScript and EPS, in pure Python from Adobe's PostScript Language Reference (3rd edition) and the PDF 1.7
specification: no Ghostscript.

    from bookport import ps
    ps.to_pdf("figure.eps", "figure.pdf")        # PostScript or EPS -> PDF (an EPS page is its bounding box)
    ps.to_svg("figure.eps", "figure.svg")        # PostScript or EPS -> SVG (one page; text as glyph outlines)
    ps.from_pdf("paper.pdf", "paper.ps")         # PDF -> PostScript (Level 3)
    ps.from_pdf("chart.pdf", "chart.eps", eps=True)  # one page as EPS

All take a path or the file's bytes, write ``dst`` when given (else return the bytes) and add a line to
``notes`` for anything they couldn't keep as it was (a font drawn with another one, say).
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import limits
from ..errors import BookportError

__all__ = ["from_pdf", "to_pdf", "to_svg"]

DAMAGE = (IndexError, KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError, MemoryError, RecursionError)


def _data(src) -> tuple[bytes, str]:
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src), "file"
    path = Path(src)
    if not path.is_file():
        raise BookportError(f"No such file: {path}")
    return path.read_bytes(), path.name


def _save(data: bytes, dst) -> bytes | Path:
    if dst is None:
        return data
    dst = Path(dst)
    tmp = dst.with_name(f".bookport-{dst.name}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dst


def to_pdf(src, dst: str | os.PathLike | None = None, *, eps: bool | None = None,
           notes: list[str] | None = None) -> bytes | Path:
    """PostScript or EPS as a PDF. ``eps``: the page is the drawing's bounding box (None: as the file says)."""
    from . import topdf
    data, name = _data(src)
    with limits.reading():
        try:
            pdf = topdf.convert(data, eps=eps, notes=notes)
        except BookportError:
            raise
        except DAMAGE as e:
            raise BookportError(f"Damaged PostScript file: {name} ({type(e).__name__})") from None
    return _save(pdf, dst)


def to_svg(src, dst: str | os.PathLike | None = None, notes: list[str] | None = None, *, eps: bool | None = None,
           page: int = 0) -> bytes | Path:
    """PostScript or EPS as SVG (one page, ``page``), for figures in books: text is drawn as glyph outlines, so it
    looks right without its fonts. ``eps``: the page is the drawing's bounding box (None: as the file says)."""
    from . import tosvg
    data, name = _data(src)
    with limits.reading():
        try:
            svg = tosvg.convert(data, eps=eps, notes=notes, page=page)
        except BookportError:
            raise
        except DAMAGE as e:
            raise BookportError(f"Damaged PostScript file: {name} ({type(e).__name__})") from None
    return _save(svg, dst)


def from_pdf(src, dst: str | os.PathLike | None = None, *, eps: bool = False, page: int = 0,
             flatten=None, notes: list[str] | None = None) -> bytes | Path:
    """A PDF as PostScript (every page) or, with ``eps``, page ``page`` as EPS.

    ``flatten(index, dpi) -> (width, height, rgb_bytes)``, when given, draws a page that uses transparency
    (which PostScript doesn't have) as a picture; without it such a page is drawn opaque, with a note."""
    from . import write
    data, name = _data(src)
    with limits.reading():
        try:
            out = write.convert(data, eps=eps, page=page, flatten=flatten, notes=notes)
        except BookportError:
            raise
        except DAMAGE as e:
            raise BookportError(f"Damaged PDF: {name} ({type(e).__name__})") from None
    return _save(out, dst)
