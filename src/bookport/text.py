"""Plain text from XHTML: one paragraph per block, blank lines between, fast (no CSS)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .markup import BLOCK

_SKIP = frozenset({"head", "script", "style", "title", "template"})
_WS = re.compile(r"[ \t\r\n\f]+")
_INVISIBLE = re.compile("[­\u200b‌‍⁠﻿]")


def document_paragraphs(root: ET.Element) -> list[str]:
    body = root.find("body")
    if body is None:
        body = root
    paras: list[str] = []
    cur: list[str] = []

    def flush():
        s = "".join(cur)
        cur.clear()
        lines = [_WS.sub(" ", line).strip() for line in s.split("\n")]
        text = "\n".join(line for line in lines if line)
        if text:
            paras.append(text)

    def walk(el, pre):
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in _SKIP:
            return
        block = tag in BLOCK or tag == "li"
        if tag == "br":
            cur.append("\n")
        elif tag in ("img", "image"):
            alt = el.get("alt", "")
            if alt and not alt.lower().startswith(("cover", "image")):
                cur.append(alt)
        if block:
            flush()
        if tag == "pre":
            pre = True
        if el.text:
            cur.append(el.text)
        for child in el:
            walk(child, pre)
            if child.tail:
                cur.append(child.tail)
            if tag in ("td", "th") or (isinstance(child.tag, str) and child.tag in ("td", "th")):
                cur.append(" ")
        if block:
            if pre:
                # keep the line structure of preformatted text
                s = "".join(cur)
                cur.clear()
                if s.strip("\n"):
                    paras.append(s.strip("\n"))
            else:
                flush()

    walk(body, False)
    flush()
    return paras


def book_text(book) -> str:
    out = []
    for href, _res in book.documents():
        out.extend(document_paragraphs(book.tree(href)))
    return _INVISIBLE.sub("", "\n\n".join(out))


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())
