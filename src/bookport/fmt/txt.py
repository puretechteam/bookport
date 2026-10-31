"""Plain text: read (paragraphs, verse, chapter headings detected) and write."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import markup
from ..book import Book, Metadata

CHAPTER = re.compile(
    r"^\s*(chapter|chap\.|book|part|section|letter|volume|vol\.|act|scene|canto|stave|prologue|epilogue|preface|"
    r"introduction|foreword|afterword|appendix|contents|glossary|index|conclusion|interlude|postscript)\b",
    re.IGNORECASE)
ROMAN_LINE = re.compile(r"^\s*([IVXLCDM]+|\d+)\.?\s*$")
CJK_CHAPTER = re.compile(r"^\s*第[〇一二三四五六七八九十百千零\d]+[回章节卷部篇]")
SPLIT_SIZE = 120_000  # characters per document, so readers aren't handed one huge file


def looks_like_markdown(text: str) -> bool:
    sample = text[:200_000]
    lines = sample.split("\n")
    # only signs plain text doesn't have: ATX headings, links and code fences
    # (asterisk rules and bullets are common in plain text, e.g. "*** START ***" or "* * *")
    score = 0
    score += sum(1 for line in lines if re.match(r"^#{1,6}\s+\S", line)) * 3
    score += len(re.findall(r"\[[^\]\n]+\]\([^)\s]+\)", sample)) * 2
    score += len(re.findall(r"^```", sample, re.MULTILINE)) * 3
    return score >= max(6, len(lines) // 300)


def read(path, encoding: str | None = None, formatting: str = "auto") -> Book:
    data = Path(path).read_bytes()
    text = markup.decode(data, encoding or "utf-8") if encoding is None else data.decode(encoding, "replace")
    title = Path(path).stem.replace("_", " ")
    return book_from_text(text, title, formatting)


def book_from_text(text: str, title: str = "", formatting: str = "auto") -> Book:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if formatting == "markdown" or formatting == "auto" and looks_like_markdown(text):
        from .markdown import book_from_markdown
        return book_from_markdown(text, title)
    book = Book(Metadata(title=title))
    book.source_format = "txt"
    _guess_metadata(book, text)
    blocks = _blocks(text)
    _emit(book, blocks)
    book.ensure_toc()
    return book


def _guess_metadata(book: Book, text: str) -> None:
    head = text[:5000]
    m = re.search(r"^Title:\s*(.+)$", head, re.MULTILINE)
    if m:
        book.metadata.title = m.group(1).strip()
    m = re.search(r"^Author:\s*(.+)$", head, re.MULTILINE)
    if m:
        book.metadata.authors = [a.strip() for a in re.split(r"\s+and\s+|;", m.group(1).strip()) if a.strip()]
    m = re.search(r"^Language:\s*(.+)$", head, re.MULTILINE)
    if m:
        lang = {"english": "en", "french": "fr", "german": "de", "spanish": "es", "italian": "it", "chinese": "zh",
                "japanese": "ja", "russian": "ru", "portuguese": "pt", "dutch": "nl", "latin": "la"}.get(
            m.group(1).strip().lower())
        if lang:
            book.metadata.language = lang
    if not book.metadata.language:
        book.metadata.language = guess_language(text)


def guess_language(text: str) -> str:
    """For formats that don't record a language: Chinese when the text is mostly CJK, else English."""
    cjk = len(re.findall(r"[一-鿿]", text[:20000]))
    return "zh" if cjk > 1000 else "en"


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """Split text into ("h"|"p"|"verse"|"pre", lines) blocks."""
    lines = text.split("\n")
    has_blank = sum(1 for line in lines if not line.strip()) > len(lines) * 0.02 and len(lines) > 3 or len(lines) <= 3
    raw: list[tuple[int, list[str]]] = []  # (blank lines before, lines)
    if has_blank:
        cur: list[str] = []
        blanks = 0
        for line in lines:
            if line.strip():
                if not cur:
                    raw.append((blanks, cur))
                cur.append(line.rstrip())
                blanks = 0
            else:
                if cur:
                    cur = []
                blanks += 1
    else:
        indented = sum(1 for line in lines if re.match(r"^(\s{2,}|\t)\S", line))
        if indented > len(lines) * 0.05:
            # "print" style: a new paragraph starts at each indented line
            cur = []
            for line in lines:
                if not line.strip():
                    continue
                if re.match(r"^(\s{2,}|\t)\S", line) and cur:
                    raw.append((0, cur))
                    cur = []
                cur.append(line.strip())
            if cur:
                raw.append((0, cur))
        else:
            raw = [(0, [line.rstrip()]) for line in lines if line.strip()]
    lengths = sorted(len(line) for _, block in raw for line in block)
    wrap = lengths[int(len(lengths) * 0.95)] if lengths else 80
    out = []
    for blanks, block in raw:
        out.append(_classify(block, blanks, wrap))
    return out


def _classify(block, blanks, wrap):
    first = block[0].strip()
    if len(block) <= 2 and len(" ".join(b.strip() for b in block)) < 70:
        joined = " ".join(b.strip() for b in block)
        letters = [c for c in joined if c.isalpha()]
        upper = letters and all(not c.islower() for c in letters) and len(letters) >= 2
        if CHAPTER.match(first) and not joined.endswith((",", ";")) or CJK_CHAPTER.match(first) \
                or (upper and blanks >= 2 and not joined.endswith(",")) or (ROMAN_LINE.match(first) and blanks >= 2):
            return ("h", [b.strip() for b in block])
        # a short line set apart by two blank lines, not ending like a sentence, is a heading
        if blanks >= 2 and letters and not joined.endswith((".", ",", ";", ":", "!", "?", '"', "'", "”", "’", ")")):
            return ("h", [b.strip() for b in block])
    if all(line.startswith(("    ", "\t")) for line in block) and len(block) > 1:
        return ("pre", block)
    if len(block) > 1 and wrap > 30:
        short = sum(1 for line in block[:-1] if len(line) < wrap * 0.6)
        if short > (len(block) - 1) * 0.5:
            return ("verse", [line.strip() for line in block])
    return ("p", [line.strip() for line in block])


def _emit(book: Book, blocks) -> None:
    lang = book.metadata.language
    n = 0
    root, body = markup.new_document(book.metadata.title, lang)
    size = 0

    def flush():
        nonlocal root, body, size, n
        if len(body):
            n += 1
            book.add_document(f"text/part{n:04d}.xhtml", root)
        root, body = markup.new_document(book.metadata.title, lang)
        size = 0

    for kind, lines in blocks:
        if kind == "h" and (CHAPTER.match(lines[0]) or CJK_CHAPTER.match(lines[0])) and size > 2000 or size > SPLIT_SIZE and kind != "verse":
            flush()
        if kind == "h":
            h = ET.SubElement(body, "h2")
            _lines_into(h, lines)
        elif kind == "pre":
            pre = ET.SubElement(body, "pre")
            indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
            pre.text = "\n".join(line[indent:] for line in lines)
        elif kind == "verse":
            p = ET.SubElement(body, "p", {"class": "verse"})
            _lines_into(p, lines)
        else:
            p = ET.SubElement(body, "p")
            p.text = " ".join(lines)
        size += sum(len(line) for line in lines)
    flush()
    if not book.spine:
        root, body = markup.new_document(book.metadata.title, lang)
        ET.SubElement(body, "p")
        book.add_document("text/part0001.xhtml", root)
    book.add("styles/txt.css", "p { margin: 0 0 .8em 0 } p.verse { margin-left: 2em } h2 { text-align: center }"
             " pre { white-space: pre-wrap }", "text/css")
    for href, _ in list(book.documents()):
        tree = book.tree(href)
        ET.SubElement(tree.find("head"), "link", {"rel": "stylesheet", "type": "text/css", "href": "../styles/txt.css"})
        book.set_tree(href, tree)


def _lines_into(el, lines):
    el.text = lines[0]
    for line in lines[1:]:
        br = ET.SubElement(el, "br")
        br.tail = line


# --- writing -----------------------------------------------------------------------------------

NEWLINES = {"unix": "\n", "windows": "\r\n", "mac": "\r", "system": "\r\n" if __import__("os").name == "nt" else "\n"}


def book_to_text(book: Book, wrap: int = 0, toc: bool = False) -> str:
    from ..flow import book_blocks
    out: list[str] = []
    if toc and book.toc:
        out.append("Contents")
        for depth, e in book.toc_walk():
            out.append("  " * depth + e.title)
        out.append("")
    for b in book_blocks(book, page_breaks=False):
        text = block_text(b)
        if not text.strip():
            continue
        if b.kind == "h" and out:
            out.append("")  # two blank lines before a heading set it apart from the text
            if out[-2:-1] != [""]:
                out.append("")
        if wrap and b.kind not in ("pre", "table"):
            import textwrap
            text = "\n".join(textwrap.fill(line, wrap, break_long_words=False, break_on_hyphens=False) or ""
                             for line in text.split("\n"))
        out.append(text)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def block_text(b) -> str:
    if b.kind == "table":
        return "\n".join("\t".join(" ".join("".join(r.text for r in c.runs).split()) for c in row) for row in b.rows)
    if b.kind == "hr":
        return "* * *"
    if b.kind == "image":
        return f"[{b.alt}]" if b.alt and not b.alt.lower().startswith("cover") else ""
    text = "".join(r.text if not r.image else (f"[{''}]" if False else "") for r in b.runs)
    if b.kind != "pre":
        text = "\n".join(line.strip() for line in text.split("\n"))
    prefix = ""
    if b.marker:
        prefix = "  " * max(0, b.depth - 1) + b.marker + " "
    elif b.quote:
        prefix = "    "
    return prefix + text


def write(book: Book, path, newline: str = "unix", wrap: int = 0, encoding: str = "utf-8-sig", toc: bool = False, **_):
    """UTF-8 with a byte-order mark by default, so readers that guess the encoding (Calibre does) don't."""
    text = book_to_text(book, wrap=wrap, toc=toc)
    nl = NEWLINES.get(newline, newline if newline in ("\n", "\r\n", "\r") else "\n")
    if nl != "\n":
        text = text.replace("\n", nl)
    with open(path, "w", encoding=encoding, errors="replace", newline="") as f:
        f.write(text)
