"""Psion TCR: text compressed with a 256-entry dictionary ("!!8-Bit!!" header). Read and write.

Each byte of the data stands for one dictionary entry (a string of up to 255 bytes). The writer
builds its dictionary by repeatedly replacing the most common pair of codes with a new code
(byte-pair encoding) until all 256 codes are used.
"""

from __future__ import annotations

import itertools
from collections import Counter
from pathlib import Path

from ..book import Book
from ..errors import BookportError

MAGIC = b"!!8-Bit!!"


def read(path) -> Book:
    data = Path(path).read_bytes()
    if not data.startswith(MAGIC):
        raise BookportError("Not a TCR file")
    pos = len(MAGIC)
    entries = []
    for _ in range(256):
        if pos >= len(data):
            raise BookportError("Damaged TCR file: the dictionary is cut short")
        n = data[pos]
        entries.append(data[pos + 1:pos + 1 + n])
        pos += 1 + n
    raw = b"".join(entries[b] for b in data[pos:])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", "replace")
    from .txt import book_from_text
    book = book_from_text(text, Path(path).stem, formatting="plain")
    book.source_format = "tcr"
    return book


def compress(raw: bytes) -> bytes:
    """TCR file contents for ``raw`` bytes."""
    seq = list(raw)
    entries: dict[int, bytes] = {b: bytes([b]) for b in set(seq)}
    free = [c for c in range(256) if c not in entries]
    # byte-pair passes over the whole text; each pass replaces the top pairs that don't overlap
    while free and len(seq) > 1:
        pairs = Counter(itertools.pairwise(seq))
        if not pairs:
            break
        top = [p for p, n in pairs.most_common(len(free)) if n > 2 and len(entries[p[0]]) + len(entries[p[1]]) <= 255]
        if not top:
            break
        codes = {}
        for p in top:
            if not free:
                break
            c = free.pop(0)
            codes[p] = c
            entries[c] = entries[p[0]] + entries[p[1]]
        out = []
        i = 0
        n = len(seq)
        while i < n:
            if i + 1 < n:
                c = codes.get((seq[i], seq[i + 1]))
                if c is not None:
                    out.append(c)
                    i += 2
                    continue
            out.append(seq[i])
            i += 1
        seq = out
    table = b"".join(bytes([len(entries.get(c, b""))]) + entries.get(c, b"") for c in range(256))
    return MAGIC + table + bytes(seq)


def write(book: Book, path, encoding: str = "utf-8-sig", **_) -> None:
    """UTF-8 with a byte-order mark by default: Calibre reads TCR as UTF-8, and the mark stops it guessing."""
    from .txt import book_to_text
    text = book_to_text(book).replace("\n", "\r\n")
    Path(path).write_bytes(compress(text.encode(encoding, "replace")))
