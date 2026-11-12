"""HTML: read a page (and the local pages, images and styles it links to); write one self-contained page."""

from __future__ import annotations

import base64
import os
import re
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from urllib.parse import unquote

from .. import markup
from ..book import CSS, XHTML, Book, Metadata, guess_type, is_external, relative, resolve, sniff_image
from ..limits import safe_path
from ..links import css_urls

MAX_FILES = 2000


def read(path, follow_links: bool = True, max_depth: int = 5, root=None) -> Book:
    """Pages, pictures and styles the page links to come from its folder and one level up only
    (or from inside ``root``, when given: an unpacked archive)."""
    start = Path(path).resolve()
    base = start.parent
    top = Path(root).resolve() if root is not None else base.parent
    book = Book(Metadata(title=start.stem.replace("_", " ")))
    book.source_format = "html"
    hrefs: dict[Path, str] = {}

    def href_for(p: Path) -> str:
        if p in hrefs:
            return hrefs[p]
        try:
            rel = p.relative_to(base).as_posix()
        except ValueError:
            rel = f"outside/{len(hrefs)}_{p.name}"
        if p.suffix.lower() in (".html", ".htm", ".xhtml", ".shtml", ".php", ".asp", ".aspx"):
            rel = os.path.splitext(rel)[0] + ".xhtml"
        hrefs[p] = book.unique_href(rel.replace(" ", "_"))
        return hrefs[p]

    found: dict[tuple[Path, str], Path | None] = {}

    def locate(folder: Path, link: str) -> Path | None:
        # each link is looked up on disk once: a page linking to one page thousands of times did it each time
        key = (folder, link.split("#", 1)[0])
        if key not in found:
            found[key] = safe_path(folder, link, top)
        return found[key]

    queue = deque([(start, 0)])
    queued: set[Path] = {start}
    docs: list[tuple[Path, ET.Element]] = []
    while queue and len(docs) < MAX_FILES:
        p, depth = queue.popleft()
        try:
            data = p.read_bytes()
        except OSError:
            continue
        root = markup.parse(data)
        docs.append((p, root))
        if not follow_links or depth >= max_depth:
            continue
        for a in root.iter("a"):
            link = a.get("href", "")
            if not link or is_external(link) or link.startswith("#"):
                continue
            target = locate(p.parent, link)
            if target is not None and target not in queued and target.suffix.lower() in (".html", ".htm", ".xhtml",
                                                                                          ".shtml") and target.is_file():
                queued.add(target)
                queue.append((target, depth + 1))
    # documents in the order found; resources they use
    for p, root in docs:
        href = href_for(p)
        hrefs[p] = href
    for p, root in docs:
        href = hrefs[p]
        _collect_resources(book, root, p, href, href_for, locate)
        _rewrite_doc_links(root, p, href, hrefs, locate)
        footnotes(root)
        book.add_document(href, root, spine=True)
    _metadata_from_head(book, docs[0][1] if docs else None)
    book.ensure_toc()
    return book


_NOTE_REF = {"footnote-ref", "footnote-reference", "noteref", "fnref"}
_NOTE_BACK = {"footnote-back", "footnote-backref", "fn-backref", "footnote-backlink"}


def footnotes(root: ET.Element) -> None:
    """Footnotes as Pandoc, Python-Markdown, docutils and most site generators write them (calls marked
    footnote-ref or role="doc-noteref", the notes as list items in a section or div of class "footnotes"
    or role="doc-endnotes") become the book's: noteref links and ``<aside epub:type="footnote">``."""
    parent = {c: p for p in root.iter() for c in p}
    for a in root.iter("a"):
        cls = set(a.get("class", "").split())
        if cls & _NOTE_REF or a.get("role") == "doc-noteref":
            a.set("class", " ".join(sorted((cls - _NOTE_REF) | {"noteref"})))
            a.set("epub:type", "noteref")
    holders = [el for el in root.iter() if el.tag in ("section", "div", "aside", "ol")
               and (el.get("role") == "doc-endnotes" or {"footnotes", "footnote"} & set(el.get("class", "").split()))]
    done: set[int] = set()
    for holder in holders:
        if any(id(anc) in done for anc in _ancestors(holder, parent)) or holder not in parent:
            continue
        done.add(id(holder))
        items = [li for li in holder.iter("li") if li.get("id")]
        if not items:
            continue
        asides = []
        for li in items:
            for a in list(li.iter("a")):
                if set(a.get("class", "").split()) & _NOTE_BACK or a.get("role") == "doc-backlink" or \
                        (a.get("href", "").startswith("#") and (a.text or "").strip() in ("↩", "↩︎", "^")):
                    _drop(a, {c: p for p in li.iter() for c in p})
            aside = ET.Element("aside", {"epub:type": "footnote", "id": li.get("id", "")})
            aside.text = li.text
            aside.extend(list(li))
            asides.append(aside)
        up = parent[holder]
        at = list(up).index(holder)
        tail = holder.tail
        up.remove(holder)
        for k, aside in enumerate(asides):
            up.insert(at + k, aside)
        if tail and tail.strip():
            asides[-1].tail = tail


def _ancestors(el: ET.Element, parent: dict):
    while el in parent:
        el = parent[el]
        yield el


def _drop(el: ET.Element, parent: dict) -> None:
    """Remove an element, keeping its tail text."""
    up = parent.get(el)
    if up is None:
        return
    kids = list(up)
    i = kids.index(el)
    if el.tail:
        if i:
            kids[i - 1].tail = (kids[i - 1].tail or "") + el.tail
        else:
            up.text = (up.text or "") + el.tail
    up.remove(el)


def _collect_resources(book, root, p: Path, href, href_for, locate):
    for el in root.iter():
        for attr in ("src", "xlink:href", "poster"):
            v = el.get(attr)
            if v and not is_external(v) and not v.startswith("data:"):
                el.set(attr, _add_file(book, p, v, href, href_for, locate))
            elif v and v.startswith("data:") and el.tag in ("img", "image"):
                res = _data_uri(book, v)
                if res is not None:
                    el.set(attr, relative(href, res))
        if el.tag == "link" and "stylesheet" in el.get("rel", "").lower():
            v = el.get("href", "")
            if v and not is_external(v):
                el.set("href", _add_file(book, p, v, href, href_for, locate, css=True))
        if el.tag == "style" and el.text:
            for u in css_urls(el.text):
                if not is_external(u) and not u.startswith("data:"):
                    new = _add_file(book, p, u, href, href_for, locate)
                    el.text = el.text.replace(u, new)


def _data_uri(book, uri):
    m = re.match(r"data:([\w/+.-]+)?(;base64)?,(.*)", uri, re.DOTALL)
    if not m:
        return None
    try:
        data = base64.b64decode(m.group(3)) if m.group(2) else unquote(m.group(3)).encode("latin-1")
    except (ValueError, UnicodeEncodeError):
        return None
    mt = sniff_image(data) or m.group(1) or "application/octet-stream"
    from ..book import IMAGE_EXT
    return book.add(f"images/inline{len(book.resources)}{IMAGE_EXT.get(mt, '.bin')}", data, mt).href


def _add_file(book, page: Path, link: str, href, href_for, locate, css=False) -> str:
    target = locate(page.parent, link)
    if target is None or not target.is_file():
        return link
    rhref = href_for(target)
    if rhref not in book.resources:
        data = target.read_bytes()
        mt = guess_type(rhref)
        if css or mt == CSS:
            text = markup.decode(data)
            for u in css_urls(text):
                if not is_external(u) and not u.startswith("data:"):
                    sub = locate(target.parent, u)
                    if sub is not None and sub.is_file():
                        sub_href = href_for(sub)
                        if sub_href not in book.resources:
                            book.add(sub_href, sub.read_bytes(), unique=False)
                        text = text.replace(u, relative(rhref, sub_href))
            data = text.encode("utf-8")
            mt = CSS
        book.add(rhref, data, mt, unique=False)
    return relative(href, rhref)


def _rewrite_doc_links(root, p: Path, href, hrefs, locate):
    for a in root.iter("a"):
        link = a.get("href", "")
        if not link or is_external(link) or link.startswith("#"):
            continue
        frag = link.partition("#")[2]
        target = locate(p.parent, link)
        if target in hrefs:
            a.set("href", relative(href, hrefs[target] + (f"#{frag}" if frag else "")))


def _metadata_from_head(book, root):
    if root is None:
        return
    head = root.find("head")
    md = book.metadata
    title = head.find("title") if head is not None else None
    if title is not None and (title.text or "").strip():
        md.title = " ".join(title.text.split())
    lang = root.get("lang") or root.get("xml:lang")
    if lang:
        md.language = lang
    for meta in (head.iter("meta") if head is not None else ()):
        name = (meta.get("name") or meta.get("property") or "").lower()
        content = (meta.get("content") or "").strip()
        if not content:
            continue
        if name in ("author", "dc.creator", "dcterms.creator", "book:author"):
            md.authors.append(content)
        elif name in ("description", "dc.description", "og:description"):
            md.description = md.description or content
        elif name in ("dc.language", "language"):
            md.language = md.language or content
        elif name in ("dc.publisher", "publisher"):
            md.publisher = content
        elif name in ("dc.date", "dcterms.issued", "date"):
            md.date = content
        elif name in ("keywords", "dc.subject"):
            md.subjects += [k.strip() for k in content.split(",") if k.strip()]


# --- writing: one page, images inlined as data: URIs ----------------------------------------

def single_page(book: Book, inline_images: bool = True) -> ET.Element:
    """All spine documents merged into one XHTML page; internal links still work."""
    from .. import clean
    b = book.copy()
    clean.clean_book(b)
    md = b.metadata
    root, body = markup.new_document(md.title, md.language)
    head = root.find("head")
    for a in md.authors:
        ET.SubElement(head, "meta", {"name": "author", "content": a})
    if md.description:
        ET.SubElement(head, "meta", {"name": "description", "content": re.sub(r"<[^>]+>", "", md.description)})
    css_parts = []
    seen_css = set()
    for href, res in b.documents():
        doc = b.tree(href)
        for el in doc.find("head"):
            if el.tag == "link":
                target = resolve(href, el.get("href", ""))
                if target in b.resources and target not in seen_css:
                    seen_css.add(target)
                    css_parts.append(_inline_css_urls(b, target, markup.decode(b.resources[target].data), inline_images))
            elif el.tag == "style" and el.text:
                css_parts.append(_inline_css_urls(b, href, el.text, inline_images))
    if css_parts:
        ET.SubElement(head, "style").text = "\n".join(css_parts)
    ids = _doc_ids(b)
    for href, res in b.documents():
        doc = b.tree(href)
        section = ET.SubElement(body, "section", {"id": ids[href]})
        dbody = doc.find("body")
        prefix = ids[href] + "-"
        for el in dbody.iter():
            if el.get("id"):
                el.set("id", prefix + el.get("id"))
            for attr in ("href", "src", "xlink:href"):
                v = el.get(attr)
                if v is None or is_external(v) or v.startswith("data:"):
                    continue
                path, _, frag = v.partition("#")
                target = resolve(href, path) if path else href
                if target in ids:
                    el.set(attr, f"#{ids[target]}-{frag}" if frag else f"#{ids[target]}")
                elif target in b.resources:
                    r = b.resources[target]
                    if inline_images and r.is_image:
                        el.set(attr, f"data:{r.media_type};base64,{base64.b64encode(r.data).decode('ascii')}")
                    else:
                        el.set(attr, target)
        section.text = dbody.text
        for child in list(dbody):
            section.append(child)
    return root


def _doc_ids(book):
    ids = {}
    for n, (href, _) in enumerate(book.documents(), 1):
        ids[href] = f"doc{n}"
    return ids


def _inline_css_urls(book, base, text, inline):
    from ..links import rewrite_css

    def fix(u):
        if is_external(u) or u.startswith("data:"):
            return u
        target = resolve(base, u)
        r = book.resources.get(target)
        if r is None:
            return None
        if inline:
            return f"data:{r.media_type};base64,{base64.b64encode(r.data).decode('ascii')}"
        return target

    return rewrite_css(text, fix)


def write(book: Book, path, **_) -> None:
    root = single_page(book, inline_images=True)
    Path(path).write_bytes(markup.serialize(root, xml_decl=False))


__all__ = ["XHTML", "read", "single_page", "write"]
