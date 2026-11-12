"""DjVu documents: the IFF85 container, the page list (one page, a bundled FORM:DJVM with its
DIRM directory, or an indirect document whose pages are files beside it), INCL includes, and
each page's INFO chunk."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .. import limits
from ..errors import BookportError

MAX_CHUNKS = 100_000  # chunks in one FORM
MAX_INCLUDE_DEPTH = 8
PAGE_FORMS = (b"DJVU", b"BM44", b"PM44")


def damaged(what: str) -> BookportError:
    return BookportError(f"Damaged DjVu file: {what}")


def chunks(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """The chunks between start and end: (id, data start, data end); a FORM's data starts with its type."""
    out = []
    pos = start
    end = min(end, len(data))
    while pos + 8 <= end:
        cid = bytes(data[pos:pos + 4])
        size = struct.unpack_from(">I", data, pos + 4)[0]
        body = pos + 8
        if body + size > end:
            size = end - body  # a cut-off last chunk keeps what's there
        out.append((cid, body, body + size))
        if len(out) > MAX_CHUNKS:
            raise damaged("it has too many chunks")
        pos = body + size + (size & 1)
    return out


def form(data: bytes, pos: int = 0) -> tuple[bytes, int, int]:
    """(type, start, end) of the FORM chunk at pos (after an optional AT&T magic)."""
    if data[pos:pos + 4] == b"AT&T":
        pos += 4
    if data[pos:pos + 4] != b"FORM" or pos + 12 > len(data):
        raise BookportError("Not a DjVu file")
    size = struct.unpack_from(">I", data, pos + 4)[0]
    end = min(pos + 8 + size, len(data))
    return bytes(data[pos + 8:pos + 12]), pos + 12, end


@dataclass
class Info:
    width: int
    height: int
    dpi: int = 300
    gamma: float = 2.2
    rotation: int = 0  # degrees counter-clockwise: 0, 90, 180 or 270
    version: int = 0


def parse_info(b: bytes) -> Info:
    if len(b) < 4:
        raise damaged("a page has no size")
    w, h = struct.unpack_from(">HH", b, 0)
    info = Info(w, h)
    if len(b) >= 6:
        info.version = b[4] | (b[5] << 8)
    if len(b) >= 8:
        dpi = b[6] | (b[7] << 8)
        info.dpi = dpi if 25 <= dpi <= 6000 else 300
    if len(b) >= 9:
        info.gamma = b[8] / 10 if 3 <= b[8] <= 50 else 2.2
    if len(b) >= 10:
        info.rotation = {6: 90, 2: 180, 5: 270}.get(b[9] & 7, 0)
    return info


@dataclass
class Component:
    id: str
    kind: int  # 0 include, 1 page, 2 thumbnails, 3 shared annotations
    name: str = ""
    title: str = ""
    offset: int = 0
    size: int = 0


@dataclass
class Page:
    """One page: its chunks in file order, with INCL'd components' chunks put where they're included."""
    doc: Document
    index: int
    id: str
    title: str
    kind: bytes  # DJVU, BM44 or PM44
    data: bytes
    own: list[tuple[bytes, int, int]]
    _info: Info | None = None
    _chunks: list[tuple[bytes, bytes, str]] | None = None
    cache: dict = field(default_factory=dict)

    @property
    def info(self) -> Info:
        if self._info is None:
            if self.kind == b"DJVU":
                b = next((self.data[s:e] for cid, s, e in self.own if cid == b"INFO"), None)
                if b is None:
                    raise damaged(f"page {self.index + 1} has no INFO chunk")
                self._info = parse_info(b)
            else:  # an IW44 image file: its size is in the first chunk's header
                b = next((self.data[s:e] for cid, s, e in self.own if cid in (b"BM44", b"PM44")), b"")
                if len(b) < 9 or b[0] != 0:
                    raise damaged("an IW44 image has no header")
                self._info = Info(*struct.unpack_from(">HH", b, 4), dpi=100)
        return self._info

    def chunks(self) -> list[tuple[bytes, bytes, str]]:
        """(id, data, component id) of every chunk on the page, includes expanded."""
        if self._chunks is None:
            out: list[tuple[bytes, bytes, str]] = []
            self._expand(self.data, self.own, self.id, out, 0, set())
            self._chunks = out
        return self._chunks

    def _expand(self, data, own, cid_name, out, depth, seen) -> None:
        for cid, s, e in own:
            if cid == b"INCL":
                target = bytes(data[s:e]).decode("utf-8", "replace").strip("\0 \r\n\t")
                if depth >= MAX_INCLUDE_DEPTH or target in seen:
                    continue
                inc = self.doc.component(target)
                if inc is None:
                    continue
                idata, ichunks = inc[0], inc[1]
                self._expand(idata, ichunks, target, out, depth + 1, seen | {target})
            else:
                out.append((cid, bytes(data[s:e]), cid_name))

    def all(self, cid: bytes) -> list[bytes]:
        return [d for c, d, _ in self.chunks() if c == cid]

    def first(self, *cids: bytes) -> tuple[bytes, bytes, str] | None:
        return next((c for c in self.chunks() if c[0] in cids), None)


class Document:
    """A DjVu document read from a file: ``pages``, and ``outline_data`` (the NAVM chunk, if any)."""

    def __init__(self, path, data: bytes | None = None):
        self.path = Path(path)
        self.data = self.path.read_bytes() if data is None else data
        self.components: list[Component] = []
        self.pages: list[Page] = []
        self.outline_data: bytes | None = None
        self.bundled = True
        self.lost: list[str] = []
        self._loaded: dict[str, tuple[bytes, list] | None] = {}
        self._by_id: tuple | None = None  # (how many components, by ID, by name or title), made on the first look-up
        self.cache: dict = {}
        self._read()

    def _read(self) -> None:
        data = self.data
        kind, start, end = form(data)
        if kind in PAGE_FORMS:
            self.pages.append(Page(self, 0, self.path.name, "", kind, data, chunks(data, start, end)))
            return
        if kind != b"DJVM":
            raise BookportError(f"This DjVu file holds no pages (FORM:{kind.decode('latin-1')})")
        top = chunks(data, start, end)
        dirm = next(((s, e) for cid, s, e in top if cid == b"DIRM"), None)
        if dirm is None:
            raise damaged("it has no page directory")
        self._dirm(data[dirm[0]:dirm[1]])
        self.outline_data = next((bytes(data[s:e]) for cid, s, e in top if cid == b"NAVM"), None)
        for comp in self.components:
            if comp.kind != 1:
                continue
            got = self.component(comp.id)
            if got is None:
                self.lost.append(f"Page {len(self.pages) + 1} ({comp.id}) is missing, so it was left out")
                continue
            pdata, own, pkind = got[0], got[1], got[2]
            if pkind not in PAGE_FORMS:
                continue
            self.pages.append(Page(self, len(self.pages), comp.id, comp.title, pkind, pdata, own))
        if not self.pages:
            raise BookportError("This DjVu document has no pages")

    def _dirm(self, d: bytes) -> None:
        from . import bzz
        if len(d) < 3:
            raise damaged("its page directory is too short")
        self.bundled = bool(d[0] & 0x80)
        n = struct.unpack_from(">H", d, 1)[0]
        pos = 3
        offsets = []
        if self.bundled:
            if pos + 4 * n > len(d):
                raise damaged("its page directory is cut short")
            offsets = list(struct.unpack_from(f">{n}I", d, pos))
            pos += 4 * n
        z = bzz.decode(d[pos:], limit=64 * limits.MB)
        need = 4 * n
        if len(z) < need:
            raise damaged("its page directory is cut short")
        sizes = [int.from_bytes(z[3 * i:3 * i + 3], "big") for i in range(n)]
        flags = z[3 * n:4 * n]
        strings = z[4 * n:].split(b"\0")
        si = 0

        def take() -> str:
            nonlocal si
            s = strings[si] if si < len(strings) else b""
            si += 1
            return s.decode("utf-8", "replace")

        for i in range(n):
            cid = take()
            name = take() if flags[i] & 0x80 else cid
            title = take() if flags[i] & 0x40 else ""
            self.components.append(Component(cid, flags[i] & 0x3F, name, title, offsets[i] if offsets else 0, sizes[i]))

    def component(self, cid: str):
        """(data, chunks, form type) of a component by ID, or None when it can't be had."""
        if cid in self._loaded:
            return self._loaded[cid]
        if self._by_id is None or self._by_id[0] != len(self.components):  # each search was through every one
            by_id: dict[str, Component] = {}
            by_name: dict[str, Component] = {}
            for c in self.components:
                by_id.setdefault(c.id, c)
                by_name.setdefault(c.name, c)
                by_name.setdefault(c.title, c)
            self._by_id = (len(self.components), by_id, by_name)
        comp = self._by_id[1].get(cid) or self._by_id[2].get(cid)
        got = None
        if comp is not None:
            try:
                if self.bundled:
                    if comp.offset + 12 <= len(self.data):
                        kind, start, end = form(self.data, comp.offset)
                        got = (self.data, chunks(self.data, start, end), kind)
                else:
                    got = self._sibling(comp)
            except BookportError:
                got = None
        self._loaded[cid] = got
        return got

    def _sibling(self, comp: Component):
        folder = self.path.parent
        target = limits.safe_path(folder, comp.name or comp.id, root=folder)
        if target is None:
            self.lost.append(f"A page file outside the document's folder was left out: {comp.name or comp.id}")
            return None
        if not target.is_file():
            return None
        data = target.read_bytes()
        limits.charge(len(data))
        kind, start, end = form(data)
        return data, chunks(data, start, end), kind
