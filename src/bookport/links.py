"""Find and rewrite the links between a book's files (XHTML attributes, inline styles, CSS)."""

from __future__ import annotations

import posixpath
import re

from . import markup
from .book import CSS, Book, is_external, normalize_href, relative, resolve

LINK_ATTRS = ("href", "src", "xlink:href", "poster", "data")
_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)
_IMPORT = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.IGNORECASE)


def css_urls(text: str) -> list[str]:
    return [m.group(2) for m in _URL.finditer(text)] + [m.group(2) for m in _IMPORT.finditer(text)]


def rewrite_css(text: str, fn) -> str:
    """Rewrite every url() and @import in CSS; ``fn(link) -> new link or None (drop)``."""
    def url(m):
        new = fn(m.group(2).strip())
        return "none" if new is None else f'url("{new}")'

    def imp(m):
        new = fn(m.group(2))
        return "" if new is None else f'@import "{new}"'

    return _IMPORT.sub(imp, _URL.sub(url, text))


def rename(book: Book, mapping: dict[str, str]) -> None:
    """Rename resources (old href -> new href) and rewrite every reference to them."""
    if not mapping:
        return
    old_resources = book.resources
    # documents are parsed against their old location
    trees = {h: book.tree(h) for h, r in old_resources.items() if r.is_document}

    def target(base_old, link):
        if not link or is_external(link) or link.startswith(("data:", "#")):
            return None
        return resolve(base_old, link), link.split("#", 1)[1] if "#" in link else None

    new_resources = {}
    for old, res in old_resources.items():
        new = mapping.get(old, old)
        res.href = new
        new_resources[new] = res

    for old, res in old_resources.items():
        new_self = mapping.get(old, old)
        if res.is_document:
            root = trees[old]
            for el in root.iter():
                for attr in LINK_ATTRS:
                    v = el.get(attr)
                    if v is None:
                        continue
                    t = target(old, v)
                    if t is None:
                        continue
                    path, frag = t
                    dest = mapping.get(path, path)
                    el.set(attr, relative(new_self, dest + (f"#{frag}" if frag else "")))
                if el.get("srcset"):
                    parts = []
                    for part in el.get("srcset").split(","):
                        bits = part.split()
                        if bits and not is_external(bits[0]):
                            path = resolve(old, bits[0])
                            bits[0] = relative(new_self, mapping.get(path, path))
                        parts.append(" ".join(bits))
                    el.set("srcset", ", ".join(parts))
                style = el.get("style")
                if style and "url(" in style:
                    el.set("style", rewrite_css(style, lambda u, o=old, n=new_self: _css_link(o, n, u, mapping)))
                if el.tag == "style" and el.text:
                    el.text = rewrite_css(el.text, lambda u, o=old, n=new_self: _css_link(o, n, u, mapping))
            res.data = markup.serialize(root)
            book._trees[new_self] = (res.data, root)
        elif res.media_type == CSS:
            text = markup.decode(res.data)
            res.data = rewrite_css(text, lambda u, o=old, n=new_self: _css_link(o, n, u, mapping)).encode("utf-8")
    for old in list(book._trees):
        if old in mapping and mapping[old] != old and old not in new_resources:
            book._trees.pop(old, None)
    book.resources = new_resources
    book.spine = [mapping.get(h, h) for h in book.spine]

    def fix(href):
        doc, _, frag = href.partition("#")
        doc = mapping.get(doc, doc)
        return doc + (f"#{frag}" if frag else "")

    for _d, e in [x for t in book.toc for x in t.walk()]:
        e.href = fix(e.href)
    for lm in book.landmarks:
        lm.href = fix(lm.href)
    book.page_list = [(label, fix(h)) for label, h in book.page_list]
    if book.cover:
        book.cover = mapping.get(book.cover, book.cover)


def _css_link(old_base, new_base, link, mapping):
    if is_external(link) or link.startswith("data:"):
        return link
    path = resolve(old_base, link)
    return relative(new_base, mapping.get(path, path))


_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def safe_names(book: Book, reserved=("mimetype", "META-INF")) -> dict[str, str]:
    """A mapping that renames hrefs with unsafe characters, odd extensions or case clashes."""
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    from .book import IMAGE_EXT
    for href, res in book.resources.items():
        parts = href.split("/")
        clean = []
        for p in parts:
            if not _SAFE.match(p):
                base, ext = posixpath.splitext(p)
                base = re.sub(r"[^A-Za-z0-9_.-]", "_", base).strip("._") or "file"
                ext = re.sub(r"[^A-Za-z0-9]", "", ext)
                p = base + (f".{ext}" if ext else "")
            clean.append(p)
        if clean[0] in reserved:
            clean[0] = "_" + clean[0]
        new = "/".join(clean)
        base, ext = posixpath.splitext(new)
        if res.is_document and ext.lower() not in (".xhtml", ".html", ".htm") or res.is_document and ext.lower() in (".html", ".htm"):
            new = base + ".xhtml"
        elif res.is_image and res.media_type in IMAGE_EXT and ext.lower() not in (".jpg", ".jpeg", ".png", ".gif",
                                                                                     ".svg", ".webp", ".bmp", ".tif",
                                                                                     ".tiff"):
            new = base + IMAGE_EXT[res.media_type]
        elif res.media_type == CSS and ext.lower() != ".css":
            new = base + ".css"
        new = normalize_href(new)
        key = new.lower()
        if key in taken:
            b, e = posixpath.splitext(new)
            n = 1
            while f"{b}_{n}{e}".lower() in taken:
                n += 1
            new = f"{b}_{n}{e}"
            key = new.lower()
        taken.add(key)
        if new != href:
            mapping[href] = new
    return mapping
