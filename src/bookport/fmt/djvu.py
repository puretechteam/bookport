"""DjVu: a fixed-layout book of page pictures (so DjVu converts to EPUB, CBZ, PDF ...), with the
outline as the table of contents and the title and authors from the document's metadata.

Black-and-white pages become 1-bit PNGs; pages with a picture background become JPEGs (a PNG of
a scanned colour page is many times larger); other pages PNGs. For DjVu to PDF with the layers,
text and bookmarks kept, use ``bookport.djvu.to_pdf``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup
from ..book import Book, Metadata, TocEntry
from ..errors import BookportError


def read(path) -> Book:
    from .. import djvu
    from ..djvu import anno, composite, jpeg
    p = Path(path)
    doc = djvu.Document(p)
    book = Book(Metadata(title=""))
    book.source_format = "djvu"
    book.fixed_layout = True
    book.lost += doc.lost
    try:
        meta = anno.page_annotations(doc.pages[0]).metadata
    except BookportError:
        meta = {}
    if meta.get("title"):
        book.metadata.title = meta["title"]
    if meta.get("author"):
        book.metadata.authors = [a.strip() for a in meta["author"].split(";") if a.strip()] or [meta["author"]]
    for key in ("publisher", "language"):
        if meta.get(key):
            setattr(book.metadata, key, meta[key])
    if meta.get("year"):
        book.metadata.date = meta["year"]
    hrefs: list[str | None] = []  # each page's document (None for one that couldn't be drawn)
    for page in doc.pages:
        n = page.index + 1
        try:
            lay = composite.layers(page)
            w, h, mode, data = composite.compose_layers(lay)
            pic = djvu._rotate(djvu.Picture(w, h, mode, data), page.info.rotation)
            if lay.bg is not None and mode != "1":
                img = book.add(f"images/page{n:04d}.jpg", jpeg.encode(pic.width, pic.height, pic.mode, pic.data, 85),
                               "image/jpeg").href
            else:
                img = book.add(f"images/page{n:04d}.png", pic.png(), "image/png").href
            w, h = pic.width, pic.height
        except BookportError as e:
            book.lost.append(f"Page {n} couldn't be drawn and was left out: {e}")
            hrefs.append(None)
            continue
        finally:
            doc.cache.pop(("text", page.index), None)
        root, body = markup.new_document(f"Page {n}", book.metadata.language)
        head = root.find("head")
        ET.SubElement(head, "meta", {"name": "viewport", "content": f"width={w}, height={h}"})
        ET.SubElement(head, "style").text = (f"html, body {{ margin: 0; padding: 0; width: {w}px; height: {h}px }} "
                                             "img { display: block; width: 100%; height: 100% }")
        div = ET.SubElement(body, "div")
        ET.SubElement(div, "img", {"src": f"../{img}", "alt": f"Page {n}", "width": str(w), "height": str(h)})
        hrefs.append(book.add_document(f"pages/page{n:04d}.xhtml", root).href)
        if book.cover is None:
            book.cover = img
    if not book.spine:
        raise BookportError("This DjVu document has no pages that could be drawn")
    book.toc = _toc(doc, hrefs)
    return book


def _toc(doc, hrefs: list[str | None]) -> list[TocEntry]:
    from ..djvu import anno
    marks = []
    if doc.outline_data:
        try:
            marks = anno.outline(doc.outline_data)
        except BookportError:
            marks = []

    def build(items) -> list[TocEntry]:
        out = []
        for m in items:
            t = anno.target(doc, m.url)
            kids = build(m.children)
            # a page that was left out points at the next one drawn
            href = next((h for h in hrefs[t:] if h), None) if t is not None else None
            href = href or (kids[0].href if kids else None)
            if href is None:
                continue
            out.append(TocEntry(" ".join(m.title.split()) or "Untitled", href, kids))
        return out

    toc = build(marks)
    return toc or [TocEntry("Start", next(h for h in hrefs if h))]
