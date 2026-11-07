"""Compare bookport's markup readers and writers with Pandoc, structure by structure, and time both.

    python benchmarks/compare_pandoc.py read  <fmt> <files or folders...>   [--json out.json]
    python benchmarks/compare_pandoc.py write <fmt> <files or folders...>   [--json out.json]

``read``: each file is read by Pandoc (to HTML) and by bookport (to its XHTML); the two are counted
the same way (headings, paragraphs, list items, tables, cells, pictures, links, code blocks, notes,
equations, words) and the counts compared.

``write``: each file (any format both read; Markdown and HTML work best) is written as <fmt> by
bookport, then Pandoc reads that back: what Pandoc finds in our output is compared with what
Pandoc finds in the original, so the output is checked by the program it replaces.

Times: Pandoc's are for the pandoc process (start-up included: that is what ConvertAll paid, twice
per conversion); bookport's are in-process, best of 3. Pandoc must be on PATH (it is only ever a
yardstick here; bookport never runs it)."""

from __future__ import annotations

import argparse
import html.parser
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import bookport

PANDOC_NAME = {"tex": "latex", "rst": "rst", "org": "org", "adoc": "asciidoc", "wiki": "mediawiki", "typ": "typst",
               "dbk": "docbook", "man": "man", "textile": "textile", "md": "markdown", "html": "html",
               "docx": "docx", "odt": "odt", "epub": "epub"}
WRITE_NAME = PANDOC_NAME | {"adoc": "asciidoc", "md": "gfm"}
EXTS = {".tex": "tex", ".latex": "tex", ".rst": "rst", ".org": "org", ".adoc": "adoc", ".asciidoc": "adoc",
        ".asc": "adoc", ".wiki": "wiki", ".mediawiki": "wiki", ".typ": "typ", ".dbk": "dbk", ".docbook": "dbk",
        ".xml": "dbk", ".man": "man", ".textile": "textile", ".md": "md", ".markdown": "md", ".html": "html",
        ".docx": "docx", ".odt": "odt", ".epub": "epub"} | {f".{n}": "man" for n in range(1, 10)}  # ls.1, printf.3
KEYS = ["headings", "paragraphs", "items", "tables", "cells", "images", "links", "code", "notes", "math", "words"]


VOID = ("br", "img", "hr", "meta", "link", "input", "col", "wbr", "source", "area", "base", "embed", "param", "track")


class _Count(html.parser.HTMLParser):
    """Footnotes count the same on both sides: Pandoc lists them as <li> items (one-paragraph notes without
    <p>) in <section class="footnotes">, bookport keeps them in <aside epub:type="footnote"> with <p>s;
    inside either, <p> and <li> aren't counted (links, code, math and words still are)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.c: Counter = Counter()
        self.words = 0
        self.skip = 0
        self.in_math = 0
        self.notes_depth = 0
        self.open: list[tuple[bool, bool]] = []  # per open element: starts a footnote region, starts math

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs, closed=True)

    def handle_starttag(self, tag, attrs, closed=False):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        region = (tag == "section" and ("footnotes" in cls or a.get("id") == "footnotes")) or \
                 (tag == "aside" and ((a.get("epub:type") or "") in ("footnote", "endnote") or a.get("role") == "doc-endnotes"))
        mathy = tag == "math" or (tag == "span" and "math" in cls)
        if not closed and tag not in VOID:
            self.open.append((region, mathy and not self.in_math))
            self.notes_depth += region
            self.in_math += mathy and not self.in_math
        if tag in ("script", "style", "head", "title", "annotation", "nav") and not closed:
            self.skip += 1
        if self.notes_depth and tag in ("p", "li"):
            pass
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.c["headings"] += 1
        elif tag == "p":
            self.c["paragraphs"] += 1
        elif tag in ("li", "dt"):
            self.c["items"] += 1
        elif tag == "table":
            self.c["tables"] += 1
        elif tag in ("td", "th"):
            self.c["cells"] += 1
        elif tag == "img":
            self.c["images"] += 1
        elif tag == "a" and a.get("href") and "noteref" not in cls and "footnote-ref" not in cls \
                and a.get("role") != "doc-noteref" and a.get("role") != "doc-backlink" and "footnote-back" not in cls \
                and a.get("aria-hidden") != "true":  # Pandoc's line-number anchors in highlighted code
            self.c["links"] += 1
        elif tag == "pre":
            self.c["code"] += 1
        elif mathy and not (tag == "math" and self.in_math > 1):
            self.c["math"] += 1
        if ("noteref" in cls or "footnote-ref" in cls or a.get("role") == "doc-noteref"
                or a.get("epub:type") == "noteref"):
            self.c["notes"] += 1

    def handle_endtag(self, tag):
        if tag not in VOID and self.open:
            region, mathy = self.open.pop()  # Pandoc's TeX fallback is a <span class="math">: it ends with its span
            self.notes_depth -= region
            self.in_math -= mathy
        if tag in ("script", "style", "head", "title", "annotation", "nav") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and not self.in_math:
            self.words += len(re.findall(r"\w+", data))


def count(html_text: str) -> dict:
    p = _Count()
    p.feed(html_text)
    d = {k: p.c[k] for k in KEYS if k != "words"}
    d["words"] = p.words
    return d


def pandoc(args: list[str], stdin: bytes | None = None) -> tuple[bytes, float]:
    t = time.perf_counter()
    out = subprocess.run(["pandoc", *args], input=stdin, capture_output=True, timeout=600, check=True).stdout
    return out, time.perf_counter() - t


# --plain: no syntax highlighting (Pandoc's highlighted code links every line to itself, which would
# count as links); --nosandbox: let Pandoc follow includes (AsciiDoc and LaTeX books need them)
EXTRA: list[str] = []
SANDBOX = ["--sandbox"]


def pandoc_html(path: Path, fmt: str) -> tuple[str, float]:
    out, took = pandoc([*SANDBOX, *EXTRA, "-f", PANDOC_NAME[fmt], "-t", "html", "--mathml", str(path)])
    return out.decode("utf-8", "replace"), took


def ours_html(path: Path, fmt: str) -> tuple[str, float]:
    best = None
    for _ in range(3):
        t = time.perf_counter()
        book = bookport.read(path, fmt)
        text = "".join(book.resources[h].data.decode("utf-8", "replace") for h, _ in book.documents())
        took = time.perf_counter() - t
        best = took if best is None else min(best, took)
    return text, best


def files(args: list[str]) -> list[Path]:
    out = []
    for a in args:
        p = Path(a)
        out += sorted(q for q in p.rglob("*") if q.suffix.lower() in EXTS) if p.is_dir() else [p]
    return out


def ratio(a: int, b: int) -> float:
    return 1.0 if a == b else min(a, b) / max(a, b) if max(a, b) else 1.0


def compare(a: dict, b: dict) -> dict:
    return {k: (a[k], b[k]) for k in KEYS if a[k] != b[k]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["read", "write"])
    ap.add_argument("fmt")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json")
    ap.add_argument("--plain", action="store_true", help="Pandoc without syntax highlighting")
    ap.add_argument("--nosandbox", action="store_true", help="let Pandoc read included files")
    a = ap.parse_args()
    if a.plain:
        EXTRA.append("--syntax-highlighting=none")
    if a.nosandbox:
        SANDBOX.clear()
    rows = []
    for path in files(a.paths):
        src_fmt = EXTS[path.suffix.lower()]
        try:
            if a.mode == "read":
                theirs, t_p = pandoc_html(path, a.fmt)
                ours, t_o = ours_html(path, a.fmt)
                ref, got = count(theirs), count(ours)
                # ConvertAll ran pandoc twice (to its JSON form, then on): the second start-up counts too
                _, t_p2 = pandoc(["-f", "html", "-t", "html"], theirs.encode())
                t_p += t_p2
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / f"out.{a.fmt}"
                    best = None
                    for _ in range(3):
                        t = time.perf_counter()
                        bookport.read(path, src_fmt).write(out, a.fmt)
                        took = time.perf_counter() - t
                        best = took if best is None else min(best, took)
                    t_o = best
                    t0 = time.perf_counter()
                    pandoc([*SANDBOX, *EXTRA, "-f", PANDOC_NAME[src_fmt], "-t", WRITE_NAME[a.fmt], "-o",
                            str(Path(tmp) / f"theirs.{a.fmt}"), str(path)])
                    t_p = time.perf_counter() - t0
                    ref = count(pandoc_html(path, src_fmt)[0])
                    got = count(pandoc_html(out, a.fmt)[0])
            diff = compare(ref, got)
            words = ratio(ref["words"], got["words"])
            row = {"file": str(path), "pandoc_s": round(t_p, 4), "bookport_s": round(t_o, 4),
                   "words_match": round(words, 4), "diff": diff, "pandoc": ref, "bookport": got}
        except Exception as exc:  # noqa: BLE001 - a failing file is a row
            row = {"file": str(path), "error": f"{type(exc).__name__}: {exc}"[:400]}
        rows.append(row)
        if "error" in row:
            print(f"ERROR {path.name}: {row['error']}")
        else:
            print(f"{path.name}: pandoc {row['pandoc_s']:.3f}s bookport {row['bookport_s']:.3f}s "
                  f"words {row['words_match']:.3f} diff {row['diff']}")
    ok = [r for r in rows if "error" not in r]
    if ok:
        faster = sum(r["bookport_s"] < r["pandoc_s"] for r in ok)
        exact = sum(not {k: v for k, v in r["diff"].items() if k != "words"} for r in ok)
        print(f"\n{len(ok)} files, {len(rows) - len(ok)} errors; bookport faster on {faster}; same structure on {exact}; "
              f"words within 1% on {sum(r['words_match'] >= 0.99 for r in ok)}")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    if not shutil.which("pandoc"):
        sys.exit("pandoc isn't on PATH")
    main()
