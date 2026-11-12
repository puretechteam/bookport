"""Command line: ``bookport SOURCE OUTPUT [options]``, ``bookport --info BOOK``, ``bookport --formats``."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, formats, read
from .errors import BookportError


def _say(text: str) -> None:
    """Print, as UTF-8 when the output is a pipe or a file (on Windows that would be the ANSI code page,
    which can't hold most titles: "Война и мир" ended in an error)."""
    out = sys.stdout
    if out is not None and not out.isatty() and hasattr(out, "buffer"):
        out.flush()
        out.buffer.write((text + "\n").encode("utf-8"))
        out.buffer.flush()
    else:
        print(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bookport", description="Convert ebooks between formats, in pure Python.")
    ap.add_argument("source", nargs="?", help="the book to read")
    ap.add_argument("output", nargs="?", help="the file to write; its extension picks the format")
    ap.add_argument("--to", dest="fmt", help="output format, when the extension doesn't say")
    ap.add_argument("--page-size", choices=("phone", "ereader", "a5", "a4", "letter"),
                    help="PDF: phone, ereader (6 inch), a5, a4 or letter (default a5)")
    ap.add_argument("--text-size", choices=("small", "normal", "large"), help="PDF: small, normal or large (default normal)")
    ap.add_argument("--epub-version", type=int, choices=(2, 3), help="EPUB: 2 or 3 (default 3)")
    ap.add_argument("--info", action="store_true",
                    help="print the book's metadata and contents as JSON (as UTF-8 when piped); with OUTPUT, convert too")
    ap.add_argument("--formats", action="store_true", help="list the formats bookport reads and writes")
    ap.add_argument("--version", action="version", version=f"bookport {__version__}")
    a = ap.parse_args(argv)
    if a.formats:
        for ext, info in formats().items():
            rw = ("read" if info["read"] else "") + (" + write" if info["write"] and info["read"] else "write" if info["write"] else "")
            print(f"{ext:9} {info['name']:55} {rw}")
        return 0
    if not a.source:
        ap.print_usage()
        return 2
    try:
        if a.output and not a.info and not (a.page_size or a.text_size or a.epub_version):
            from pathlib import Path

            from .registry import detect
            if (a.fmt or Path(a.output).suffix.lstrip(".")).lower() == "pdf" and detect(a.source) in ("ps", "eps"):
                from . import ps
                _say(str(ps.to_pdf(a.source, a.output)))  # every page as drawn, as bookport.convert does
                return 0
        book = read(a.source)
        if a.info or not a.output:
            md = book.metadata
            _say(json.dumps({
                "format": book.source_format, "title": md.title, "authors": md.authors, "language": md.language,
                "publisher": md.publisher, "date": md.date, "identifiers": md.identifiers, "subjects": md.subjects,
                "series": md.series, "series_index": md.series_index, "description": md.description,
                "documents": len(book.spine), "images": len(book.images()), "cover": book.cover,
                "words": book.word_count(),
                "toc": [{"depth": d, "title": e.title, "href": e.href} for d, e in book.toc_walk()],
            }, indent=1, ensure_ascii=False))
            if not a.output:
                return 0
        options = {}
        if a.page_size:
            options["page_size"] = a.page_size
        if a.text_size:
            options["text_size"] = a.text_size
        if a.epub_version:
            options["version"] = a.epub_version
        out = book.write(a.output, a.fmt, **options)
        _say(str(out))
        return 0
    except BookportError as e:
        print(f"bookport: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"bookport: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - a bug in bookport: a message, not a traceback
        print(f"bookport: unexpected error ({type(e).__name__}: {e}); please report it with the book that caused it",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
