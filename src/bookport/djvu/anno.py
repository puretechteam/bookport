"""Annotations (ANTa, ANTz: links and document metadata, written as S-expressions) and the
document outline (NAVM bookmarks)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import BookportError
from . import bzz

MAX_DEPTH = 64
MAX_TOKENS = 1_000_000
_TOKEN = re.compile(rb'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"?|([^\s()"]+))', re.DOTALL)
_ESC = {b"n": b"\n", b"t": b"\t", b"r": b"\r", b"b": b"\b", b"f": b"\f", b"v": b"\v", b"a": b"\a"}


class Symbol(str):
    """A bare word in an annotation (rect, maparea, ...), as opposed to a string."""


def _unescape(s: bytes) -> str:
    def sub(m):
        c = m.group(1)
        if c[:1].isdigit():
            return bytes([int(c, 8) & 0xFF])
        return _ESC.get(c, c)
    return re.sub(rb"\\([0-7]{1,3}|.)", sub, s, flags=re.DOTALL).decode("utf-8", "replace")


def sexpr(data: bytes) -> list:
    """Every top-level expression in the text: lists, strings, Symbols and numbers."""
    top: list = []
    stack = [top]
    count = 0
    pos = 0
    n = len(data)
    while pos < n:
        m = _TOKEN.match(data, pos)
        if m is None or m.end() == pos:
            break
        pos = m.end()
        count += 1
        if count > MAX_TOKENS:
            raise BookportError("Damaged DjVu file: its annotations are too long")
        if m.group(1):
            if len(stack) > MAX_DEPTH:
                raise BookportError("Damaged DjVu file: its annotations nest too deeply")
            new: list = []
            stack[-1].append(new)
            stack.append(new)
        elif m.group(2):
            if len(stack) > 1:
                stack.pop()
        elif m.group(3) is not None:
            stack[-1].append(_unescape(m.group(3)))
        elif m.group(4):
            word = m.group(4).decode("utf-8", "replace")
            try:
                stack[-1].append(int(word))
            except ValueError:
                try:
                    stack[-1].append(float(word))
                except ValueError:
                    stack[-1].append(Symbol(word))
    return top


@dataclass
class Link:
    url: str  # an address, or "#page" / "#component id" inside the document
    comment: str
    rect: tuple[int, int, int, int]  # left, bottom, right, top in page pixels


@dataclass
class Annotations:
    metadata: dict[str, str] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)


def page_annotations(page) -> Annotations:
    """All annotation chunks of a page (shared ones included), merged."""
    out = Annotations()
    for cid, data, _ in page.chunks():
        if cid not in (b"ANTa", b"ANTz"):
            continue
        try:
            raw = bzz.decode(data, limit=64 * 1024 * 1024) if cid == b"ANTz" else data
        except BookportError:
            continue
        for e in sexpr(raw):
            if not isinstance(e, list) or not e or not isinstance(e[0], Symbol):
                continue
            head = e[0]
            if head == "metadata":
                for item in e[1:]:
                    if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], Symbol) and isinstance(item[1], str):
                        out.metadata.setdefault(str(item[0]).lower(), item[1])
            elif head == "maparea" and len(e) >= 4:
                link = _maparea(e)
                if link is not None:
                    out.links.append(link)
    return out


def _maparea(e: list) -> Link | None:
    url = e[1]
    if isinstance(url, list):
        url = url[1] if len(url) > 1 and isinstance(url[1], str) else ""
    if not isinstance(url, str) or not url:
        return None
    comment = e[2] if isinstance(e[2], str) else ""
    shape = e[3]
    if not isinstance(shape, list) or not shape:
        return None
    nums = [v for v in shape[1:] if isinstance(v, (int, float))]
    kind = shape[0]
    if kind in ("rect", "oval", "text") and len(nums) >= 4:
        x, y, w, h = (int(v) for v in nums[:4])
        return Link(url, comment, (x, y, x + w, y + h))
    if kind in ("poly", "line") and len(nums) >= 4:
        xs, ys = nums[0::2], nums[1::2]
        return Link(url, comment, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))))
    return None


@dataclass
class Bookmark:
    title: str
    url: str
    children: list[Bookmark] = field(default_factory=list)


def outline(data: bytes) -> list[Bookmark]:
    """The NAVM chunk's bookmarks, nested."""
    raw = bzz.decode(data, limit=64 * 1024 * 1024)
    if len(raw) < 2:
        return []
    total = int.from_bytes(raw[:2], "big")
    pos = 2
    top: list[Bookmark] = []
    # (list to add to, bookmarks still to read for it)
    stack: list[list] = [[top, 1 << 30]]
    for _ in range(total):
        while stack and stack[-1][1] <= 0:
            stack.pop()
        if not stack or pos + 4 > len(raw):
            break
        nchild = raw[pos]
        n = int.from_bytes(raw[pos + 1:pos + 4], "big")
        title = raw[pos + 4:pos + 4 + n].decode("utf-8", "replace")
        pos += 4 + n
        n = int.from_bytes(raw[pos:pos + 3], "big")
        url = raw[pos + 3:pos + 3 + n].decode("utf-8", "replace")
        pos += 3 + n
        b = Bookmark(title, url)
        stack[-1][0].append(b)
        stack[-1][1] -= 1
        if nchild:
            if len(stack) > MAX_DEPTH:
                raise BookportError("Damaged DjVu file: its outline nests too deeply")
            stack.append([b.children, nchild])
    return top


def target(doc, url: str, here: int = 0) -> int | None:
    """The page index a "#..." address names (a page number, +n/-n from ``here``, or a page's
    component ID), or None for an outside address or one that names nothing."""
    if not url.startswith("#"):
        return None
    ref = url[1:]
    if not ref:
        return None
    try:
        if ref[0] in "+-":
            n = here + int(ref)
        else:
            n = int(ref) - 1
        return n if 0 <= n < len(doc.pages) else None
    except ValueError:
        pass
    for p in doc.pages:
        if p.id == ref:
            return p.index
    for c in doc.components:
        if ref in (c.name, c.title):
            for p in doc.pages:
                if p.id == c.id:
                    return p.index
    return None
