"""The format-neutral book: metadata, a spine of XHTML documents, resources, TOC and landmarks."""

from __future__ import annotations

import mimetypes
import posixpath
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import markup

XHTML = "application/xhtml+xml"
CSS = "text/css"
NCX = "application/x-dtbncx+xml"

MEDIA_TYPES = {
    ".xhtml": XHTML, ".html": XHTML, ".htm": XHTML, ".xml": "application/xml",
    ".css": CSS, ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".svg": "image/svg+xml", ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".ncx": NCX, ".js": "application/javascript", ".mp3": "audio/mpeg", ".mp4": "video/mp4",
    ".smil": "application/smil+xml", ".txt": "text/plain", ".pls": "application/pls+xml",
}


def guess_type(href: str) -> str:
    ext = posixpath.splitext(href.lower())[1]
    return MEDIA_TYPES.get(ext) or mimetypes.guess_type(href)[0] or "application/octet-stream"


def sniff_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\0", b"MM\0*"):
        return "image/tiff"
    head = data[:512].lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048]):
        return "image/svg+xml"
    return None


IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/svg+xml": ".svg",
             "image/webp": ".webp", "image/bmp": ".bmp", "image/tiff": ".tif"}


@dataclass
class Resource:
    href: str
    media_type: str
    data: bytes
    id: str = ""
    properties: str = ""

    @property
    def is_document(self) -> bool:
        return self.media_type in (XHTML, "text/html")

    @property
    def is_image(self) -> bool:
        return self.media_type.startswith("image/")


@dataclass
class TocEntry:
    title: str
    href: str = ""
    children: list[TocEntry] = field(default_factory=list)

    def walk(self, depth: int = 0):
        yield depth, self
        for c in self.children:
            yield from c.walk(depth + 1)


@dataclass
class Landmark:
    type: str  # EPUB 3 structural semantics: cover, toc, bodymatter, titlepage, ...
    title: str
    href: str


@dataclass
class Metadata:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    language: str = ""
    identifiers: dict[str, str] = field(default_factory=dict)  # scheme (isbn, uuid, url, asin, ...) -> value
    publisher: str = ""
    date: str = ""  # ISO 8601, as precise as known
    description: str = ""  # may contain simple HTML
    subjects: list[str] = field(default_factory=list)
    series: str = ""
    series_index: float | None = None
    rights: str = ""
    contributors: list[str] = field(default_factory=list)
    title_sort: str = ""
    author_sort: str = ""

    def uid(self) -> str:
        """A stable unique id: the first identifier, else a new UUID (remembered)."""
        for scheme in ("uuid", "isbn", "doi", "url"):
            if self.identifiers.get(scheme):
                return self.identifiers[scheme]
        if self.identifiers:
            return next(iter(self.identifiers.values()))
        self.identifiers["uuid"] = str(uuid.uuid4())
        return self.identifiers["uuid"]


class Book:
    """A book in memory.

    ``resources`` maps an href (a POSIX path inside the book, like ``text/ch1.xhtml``) to its
    Resource; ``spine`` lists document hrefs in reading order; TOC and landmark hrefs may carry
    ``#fragment``s. ``cover`` is the href of the cover image, if there is one.
    """

    def __init__(self, metadata: Metadata | None = None):
        self.metadata = metadata or Metadata()
        self.resources: dict[str, Resource] = {}
        self.spine: list[str] = []
        self.toc: list[TocEntry] = []
        self.landmarks: list[Landmark] = []
        self.page_list: list[tuple[str, str]] = []
        self.cover: str | None = None
        self.fixed_layout = False
        self.direction = ""  # "rtl" for right-to-left page progression
        self.source_format = ""
        self._trees: dict[str, tuple[bytes, ET.Element]] = {}

    def __repr__(self) -> str:
        m = self.metadata
        return (f"<Book {m.title!r} by {', '.join(m.authors) or '?'}: {len(self.spine)} documents, "
                f"{sum(r.is_image for r in self.resources.values())} images, {sum(1 for _ in self.toc_walk())} TOC entries>")

    # --- resources -----------------------------------------------------------------------------
    def add(self, href: str, data: bytes | str, media_type: str | None = None, *, spine: bool = False,
            properties: str = "", unique: bool = True) -> Resource:
        if isinstance(data, str):
            data = data.encode("utf-8")
        href = normalize_href(href)
        if unique:
            href = self.unique_href(href)
        mt = media_type or (sniff_image(data) if guess_type(href).startswith("image/") else None) or guess_type(href)
        if mt == "text/html":
            mt = XHTML
        res = Resource(href, mt, data, properties=properties)
        self.resources[href] = res
        if spine:
            self.spine.append(href)
        return res

    def unique_href(self, href: str) -> str:
        if href not in self.resources:
            return href
        base, ext = posixpath.splitext(href)
        n = 1
        while f"{base}_{n}{ext}" in self.resources:
            n += 1
        return f"{base}_{n}{ext}"

    def add_document(self, href: str, root: ET.Element, *, spine: bool = True) -> Resource:
        res = self.add(href, markup.serialize(root), XHTML, spine=spine)
        self._trees[res.href] = (res.data, root)
        return res

    def remove(self, href: str) -> None:
        self.resources.pop(href, None)
        self.spine = [h for h in self.spine if h != href]
        self._trees.pop(href, None)

    def documents(self):
        """(href, Resource) for spine documents, in reading order."""
        for href in self.spine:
            res = self.resources.get(href)
            if res is not None and res.is_document:
                yield href, res

    def images(self) -> list[Resource]:
        return [r for r in self.resources.values() if r.is_image]

    def stylesheets(self) -> list[Resource]:
        return [r for r in self.resources.values() if r.media_type == CSS]

    def tree(self, href: str) -> ET.Element:
        """The parsed document (cached until its data changes). Edit it, then call set_tree."""
        res = self.resources[href]
        cached = self._trees.get(href)
        if cached is not None and cached[0] is res.data:
            return cached[1]
        root = markup.parse(res.data)
        self._trees[href] = (res.data, root)
        return root

    def set_tree(self, href: str, root: ET.Element) -> None:
        res = self.resources[href]
        res.data = markup.serialize(root)
        self._trees[href] = (res.data, root)

    def css_for(self, href: str, root: ET.Element | None = None) -> str:
        """All CSS that applies to a document: linked stylesheets, then <style> blocks, in order."""
        root = root if root is not None else self.tree(href)
        parts = []
        head = root.find("head")
        if head is None:
            return ""
        for el in head:
            if el.tag == "link" and "stylesheet" in el.get("rel", "").lower():
                target = resolve(href, el.get("href", ""))
                res = self.resources.get(target)
                if res is not None:
                    parts.append(markup.decode(res.data))
            elif el.tag == "style":
                parts.append(el.text or "")
        return "\n".join(parts)

    # --- TOC -----------------------------------------------------------------------------------
    def toc_walk(self):
        for e in self.toc:
            yield from e.walk()

    def ensure_toc(self) -> None:
        """Build a TOC from headings when the book has none (one entry per document at least)."""
        if self.toc:
            return
        from .toc import toc_from_headings
        self.toc = toc_from_headings(self)

    # --- text ----------------------------------------------------------------------------------
    def text(self) -> str:
        from .text import book_text
        return book_text(self)

    def word_count(self) -> int:
        return len(re.findall(r"\w+", self.text()))

    def copy(self) -> Book:
        """A deep copy: changing the copy (cleaning, renaming) never touches this book."""
        import copy as _copy
        b = Book(_copy.deepcopy(self.metadata))
        for href, res in self.resources.items():
            r = b.add(href, res.data, res.media_type, unique=False)
            r.properties, r.id = res.properties, res.id
        b.spine = [h for h in self.spine if h in b.resources]
        b.toc = _copy.deepcopy(self.toc)
        b.landmarks = _copy.deepcopy(self.landmarks)
        b.page_list = list(self.page_list)
        b.cover = self.cover if self.cover in b.resources else None
        b.fixed_layout, b.direction, b.source_format = self.fixed_layout, self.direction, self.source_format
        return b

    # --- output --------------------------------------------------------------------------------
    def write(self, path, fmt: str | None = None, **options) -> Path:
        from .registry import write_book
        return write_book(self, path, fmt, **options)

    def cover_image(self) -> Resource | None:
        if self.cover and self.cover in self.resources:
            return self.resources[self.cover]
        return None


def normalize_href(href: str) -> str:
    href = href.replace("\\", "/")
    href = posixpath.normpath(href).lstrip("/")
    while href.startswith("../"):
        href = href[3:]
    return "" if href == "." else href


def resolve(base_href: str, link: str) -> str:
    """Resolve a relative link (no fragment) against the document that contains it."""
    from urllib.parse import unquote
    link = unquote(link.split("#", 1)[0].split("?", 1)[0])
    if not link:
        return base_href
    if link.startswith("/"):
        return normalize_href(link)
    return normalize_href(posixpath.join(posixpath.dirname(base_href), link))


def relative(from_href: str, to_href: str) -> str:
    """The relative link from one document to another href (which may have a #fragment)."""
    from urllib.parse import quote
    target, _, frag = to_href.partition("#")
    if not target or target == from_href:
        rel = posixpath.basename(target) if target else ""
    else:
        rel = posixpath.relpath(target, posixpath.dirname(from_href) or ".")
    rel = quote(rel, safe="/._-~!$&'()*+,;=:@")
    return rel + (f"#{frag}" if frag else "")


def is_external(link: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", link)) and not link.lower().startswith("file:")
