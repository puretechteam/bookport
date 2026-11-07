"""The hidden text layer (TXTa, TXTz): the page's text and its zones (page, column, region,
paragraph, line, word, character), each a box in page pixels counted from the bottom left."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..errors import BookportError
from . import bzz

PAGE, COLUMN, REGION, PARAGRAPH, LINE, WORD, CHARACTER = range(1, 8)
MAX_ZONE_DEPTH = 32
SEPARATORS = " \t\r\n\x0b\x0c\x1d\x1e\x1f\0"


@dataclass
class Zone:
    kind: int
    x: int  # left
    y: int  # bottom
    w: int
    h: int
    start: int  # byte range of its text
    length: int
    children: list[Zone] = field(default_factory=list)


@dataclass
class PageText:
    text: bytes  # UTF-8, with DjVu's separators
    zone: Zone | None

    def zone_text(self, z: Zone) -> str:
        return self.text[max(0, z.start):max(0, z.start + z.length)].decode("utf-8", "replace")


def parse(data: bytes, compressed: bool) -> PageText:
    if compressed:
        data = bzz.decode(data)
    if len(data) < 3:
        return PageText(b"", None)
    n = int.from_bytes(data[:3], "big")
    text = bytes(data[3:3 + n])
    pos = 3 + n
    if pos >= len(data):  # no zones: just the text
        return PageText(text, None)
    pos += 1  # version
    root = None
    try:
        root = _zones(data, pos)
    except (struct.error, IndexError, BookportError):  # damaged zones: the text alone stays
        pass
    return PageText(text, root)


def _read(data: bytes, pos: int):
    kind = data[pos]
    x, y, w, h, start = (v - 0x8000 for v in struct.unpack_from(">5H", data, pos + 1))
    length = int.from_bytes(data[pos + 11:pos + 14], "big")
    n = int.from_bytes(data[pos + 14:pos + 17], "big")
    if pos + 17 > len(data):
        raise IndexError
    return Zone(kind, x, y, w, h, start, length), n, pos + 17


def _zones(data: bytes, pos: int) -> Zone:
    root, n, pos = _read(data, pos)
    # (zone, children still to read, previous child)
    stack: list[list] = [[root, n, None]]
    count = 1
    limit = len(data) // 17 + 1
    while stack:
        top = stack[-1]
        parent, left, prev = top
        if left <= 0:
            stack.pop()
            continue
        z, n, pos = _read(data, pos)
        count += 1
        if count > limit:
            raise BookportError("Damaged DjVu file: a text layer has too many zones")
        if prev is not None:
            if z.kind in (PAGE, PARAGRAPH, LINE):
                z.x += prev.x
                z.y = prev.y - (z.y + z.h)
            else:
                z.x += prev.x + prev.w
                z.y += prev.y
            z.start += prev.start + prev.length
        else:
            z.x += parent.x
            z.y = parent.y + parent.h - (z.y + z.h)
            z.start += parent.start
        parent.children.append(z)
        top[1] = left - 1
        top[2] = z
        if n:
            if len(stack) >= MAX_ZONE_DEPTH:
                raise BookportError("Damaged DjVu file: a text layer nests too deeply")
            stack.append([z, n, None])
    return root


def page_text(page) -> PageText | None:
    got = page.first(b"TXTz", b"TXTa")
    if got is None:
        return None
    key = ("text", page.index)
    pt = page.doc.cache.get(key)
    if pt is None:
        pt = parse(got[1], got[0] == b"TXTz")
        page.doc.cache[key] = pt
    return pt


def plain(page) -> bytes | None:
    """The page's text as djvutxt prints it (None for a page without a text layer)."""
    got = page.first(b"TXTz", b"TXTa")
    if got is None:
        return None
    data = bzz.decode(got[1]) if got[0] == b"TXTz" else got[1]
    n = int.from_bytes(data[:3], "big")
    t = bytes(data[3:3 + n])
    if 3 + n + 18 <= len(data):  # only the page zone's own range is needed, not the zone tree
        try:
            root = _read(data, 3 + n + 1)[0]
            t = t[max(0, root.start):max(0, root.start + root.length)]
        except (struct.error, IndexError):
            pass
    # encoders often end the text with a NUL, where djvutxt (a C program) stops
    return t.split(bytes(1), 1)[0]


def lines(page) -> list[list[tuple[str, float, float, float, float]]]:
    """The words of a page by line, each (text, left, bottom, right, top) in page pixels from the
    bottom left. Lines without word zones are split at spaces, in proportion to their letters."""
    pt = page_text(page)
    if pt is None:
        return []
    out: list[list] = []
    if pt.zone is None:
        info = page.info
        texts = [t for t in pt.text.decode("utf-8", "replace").splitlines() if t.strip()]
        step = info.height / max(len(texts) + 2, 60)
        for i, t in enumerate(texts):
            top = info.height - step * (i + 1)
            out.append(_split(t, 0, top - step, info.width, top))
        return out
    stack = [pt.zone]
    order = []
    while stack:  # depth first, in reading order
        z = stack.pop()
        if z.kind >= LINE or not z.children:
            order.append(z)
        else:
            stack.extend(reversed(z.children))
    for z in order:
        if z.kind == LINE or (z.kind < LINE and not z.children):
            words = [c for c in z.children if c.kind >= WORD]
            if words:
                line = []
                for wz in words:
                    t = pt.zone_text(wz).strip(SEPARATORS)
                    if t:
                        line.append((t, wz.x, wz.y, wz.x + wz.w, wz.y + wz.h))
                if line:
                    out.append(line)
            else:
                t = pt.zone_text(z).strip(SEPARATORS)
                if t:
                    out.append(_split(t, z.x, z.y, z.x + z.w, z.y + z.h))
        elif z.kind == WORD:  # a word straight under a paragraph or region
            t = pt.zone_text(z).strip(SEPARATORS)
            if t:
                out.append([(t, z.x, z.y, z.x + z.w, z.y + z.h)])
    return out


def _split(t: str, x0: float, y0: float, x1: float, y1: float) -> list:
    words = t.split()
    total = sum(len(w) for w in words) + max(0, len(words) - 1)
    per = (x1 - x0) / max(total, 1)
    out = []
    x = x0
    for w in words:
        out.append((w, x, y0, x + per * len(w), y1))
        x += per * (len(w) + 1)
    return out
