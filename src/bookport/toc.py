"""Tables of contents: build one from headings, and keep TOC links pointing at real anchors."""

from __future__ import annotations

from .book import Book, TocEntry
from .markup import text_of

_HEADINGS = ("h1", "h2", "h3")


def _clean(s: str) -> str:
    return " ".join(s.split())


def toc_from_headings(book: Book, levels=_HEADINGS) -> list[TocEntry]:
    """A nested TOC from h1-h3 headings (adding ids where needed); else one entry per document."""
    entries: list[tuple[int, TocEntry]] = []
    used_ids: dict[str, set] = {}
    for n, (href, _res) in enumerate(book.documents(), 1):
        root = book.tree(href)
        ids = used_ids.setdefault(href, {e.get("id") for e in root.iter() if e.get("id")})
        changed = False
        found = False
        for el in root.iter():
            if el.tag in levels:
                title = _clean(text_of(el))
                if not title:
                    continue
                hid = el.get("id")
                if not hid:
                    hid = _new_id(ids, "toc")
                    el.set("id", hid)
                    changed = True
                entries.append((int(el.tag[1]), TocEntry(title[:200], f"{href}#{hid}")))
                found = True
        if changed:
            book.set_tree(href, root)
        if not found:
            title = root.find("head/title")
            name = _clean(title.text or "") if title is not None and title.text else ""
            if name and name != book.metadata.title:
                entries.append((1, TocEntry(name, href)))
    if not entries:
        return [TocEntry(book.metadata.title or "Start", href) for href, _ in list(book.documents())[:1]]
    return nest(entries)


def nest(flat: list[tuple[int, TocEntry]]) -> list[TocEntry]:
    """Turn (level, entry) pairs into a tree; levels need not be contiguous."""
    top: list[TocEntry] = []
    stack: list[tuple[int, TocEntry]] = []
    min_level = min(level for level, _ in flat) if flat else 1
    for level, entry in flat:
        level = level - min_level + 1
        while stack and stack[-1][0] >= level:
            stack.pop()
        (stack[-1][1].children if stack else top).append(entry)
        stack.append((level, entry))
    return top


def _new_id(ids: set, prefix: str) -> str:
    n = len(ids) + 1
    while f"{prefix}{n}" in ids:
        n += 1
    ids.add(f"{prefix}{n}")
    return f"{prefix}{n}"


def fix_links(book: Book) -> None:
    """Point TOC entries whose target is missing at the document (or drop them if that's gone)."""
    def ok(entry):
        doc, _, frag = entry.href.partition("#")
        if doc not in book.resources:
            return False
        if frag:
            root = book.tree(doc)
            if not any(e.get("id") == frag for e in root.iter()):
                entry.href = doc
        return True

    def walk(entries):
        out = []
        for e in entries:
            e.children = walk(e.children)
            if ok(e) or e.children:
                if not e.href and e.children:
                    e.href = e.children[0].href
                out.append(e)
        return out

    book.toc = walk(book.toc)

