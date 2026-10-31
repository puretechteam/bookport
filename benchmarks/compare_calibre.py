"""Convert a corpus with Calibre's ebook-convert and with bookport, and compare the results.

    python benchmarks/compare_calibre.py CORPUS --to epub,mobi,pdf [--from epub,mobi] [--tools tools.json]
                                         [--json results.json] [--limit N]

For every source book and target format, both tools convert in a fresh process (so start-up
counts for both), one at a time and at low priority. Then each output is measured the same way:

- time (seconds) and peak memory of the whole process tree (a Windows job object);
- whether it reopens in bookport, in Calibre (ebook-convert to text) and in PyMuPDF (for the
  formats PyMuPDF reads);
- epubcheck errors (EPUB outputs, when --tools points at epubcheck and Java);
- word recall: the share of the source's words (as a multiset) found in the output, with both
  texts extracted by the same tool (bookport, and Calibre with --calibre-text). With
  --originals, a source made from a book in that folder (alice.pdf from alice.epub) is measured
  against the original book instead, so neither tool is scored against its own reading of a
  lossy intermediate file;
- TOC titles that match the source's (so invented headings don't count), images, metadata
  fields (title, authors, language, publisher, date, identifiers, description, subjects) and
  whether the cover survived.

Prints one line per conversion and a summary table per target format (median time and memory,
worst-book recall). Nothing is
written outside the temporary output folder unless --json or --keep is given.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

CALIBRE = shutil.which("ebook-convert") or "C:/Program Files/Calibre2/ebook-convert.exe"
CALIBRE_DIR = Path(CALIBRE).parent
PYMUPDF_READS = {"pdf", "epub", "mobi", "fb2", "cbz", "txt", "xps", "azw3"}
BOOKPORT_CMD = "import sys, bookport; bookport.convert(sys.argv[1], sys.argv[2])"
FIELDS = ("title", "authors", "language", "publisher", "date", "identifiers", "description", "subjects")


# --- running with time and peak memory ---------------------------------------------------------

class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def run_measured(cmd: list[str], timeout: float = 900) -> tuple[float, int, int, str]:
    """(seconds, peak bytes of the process tree, return code, stderr tail)."""
    flags = 0x00004000 if os.name == "nt" else 0  # BELOW_NORMAL_PRIORITY_CLASS
    start = time.perf_counter()
    if os.name == "nt":
        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags | 0x4)  # suspended
        k32.AssignProcessToJobObject(job, ctypes.c_void_p(int(p._handle)))
        _resume(p.pid)
        try:
            _, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            _, err = p.communicate()
        seconds = time.perf_counter() - start
        info = _ExtendedLimits()
        k32.QueryInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info), None)
        k32.CloseHandle(job)
        peak = info.PeakJobMemoryUsed
    else:  # pragma: no cover - Windows is the benchmark machine
        import resource
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _, err = p.communicate(timeout=timeout)
        seconds = time.perf_counter() - start
        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
    return seconds, peak, p.returncode, err.decode("utf-8", "replace")[-800:]


def _resume(pid: int) -> None:
    """Resume a process created suspended (so it's in the job before it starts)."""
    ntdll = ctypes.windll.ntdll
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    ntdll.NtResumeProcess(h)
    k32.CloseHandle(h)


# --- measuring an output -------------------------------------------------------------------------

def words_bookport(path: Path) -> collections.Counter | None:
    import bookport
    try:
        return collections.Counter(re.findall(r"\w+", bookport.read(path).text().lower()))
    except Exception:  # noqa: BLE001 - a book that doesn't open counts as no words
        return None


def words_calibre(path: Path, tmp: Path) -> collections.Counter | None:
    out = tmp / f"calibre-text-{path.stem}-{path.suffix[1:]}.txt"
    r = subprocess.run([CALIBRE, str(path), str(out)], capture_output=True, timeout=900, check=False)
    if r.returncode or not out.exists():
        return None
    text = out.read_text("utf-8", "replace")
    out.unlink()
    return collections.Counter(re.findall(r"\w+", text.lower()))


def recall(src: collections.Counter, out: collections.Counter | None) -> float:
    if out is None:
        return 0.0
    total = sum(src.values())
    return 100.0 * sum((src & out).values()) / total if total else 100.0


def pymupdf_opens(path: Path) -> bool | None:
    if path.suffix[1:].lower() not in PYMUPDF_READS:
        return None
    try:
        import pymupdf
    except ImportError:
        return None
    try:
        with pymupdf.open(path) as d:
            return d.page_count > 0 and bool("".join(d[i].get_text() for i in range(min(3, d.page_count))) or d.page_count)
    except Exception:  # noqa: BLE001
        return False


def epubcheck(path: Path, tools: dict) -> int | None:
    if path.suffix.lower() != ".epub" or not tools.get("java") or not tools.get("epubcheck"):
        return None
    r = subprocess.run([tools["java"], "-jar", tools["epubcheck"], "-q", str(path)], capture_output=True, timeout=900,
                       check=False)
    text = (r.stdout + r.stderr).decode("utf-8", "replace")
    return sum(1 for line in text.splitlines() if line.startswith(("ERROR", "FATAL")))


def describe(path: Path) -> dict:
    import bookport
    try:
        b = bookport.read(path)
    except Exception as e:  # noqa: BLE001
        return {"opens": False, "error": str(e)[:200]}
    md = b.metadata
    fields = {f: bool(getattr(md, f)) for f in FIELDS}
    titles = [_norm(e.title) for _d, e in b.toc_walk()]
    return {"opens": True, "toc": len(titles), "toc_titles": [t for t in titles if t], "images": len(b.images()),
            "cover": b.cover is not None, "fields": fields}


def _norm(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.lower()))


def toc_matched(source: list[str], output: list[str]) -> int:
    """Source TOC titles found in the output's TOC (equal, or one containing the other), each output entry used once.
    Counting entries alone would reward tools that invent headings."""
    left = collections.Counter(output)
    found = 0
    for t in source:
        if left[t] > 0:
            left[t] -= 1
            found += 1
            continue
        hit = next((o for o, n in left.items() if n > 0 and len(min(o, t, key=len)) >= 3 and (t in o or o in t)), None)
        if hit is not None:
            left[hit] -= 1
            found += 1
    return found


def measure(path: Path, src_words, tools, tmp, calibre_text) -> dict:
    d = describe(path)
    d["size"] = path.stat().st_size
    d["recall_bp"] = recall(src_words["bp"], words_bookport(path)) if d.get("opens") else 0.0
    cal = words_calibre(path, tmp) if calibre_text or path.suffix.lower() != ".pdf" else None
    d["calibre_opens"] = cal is not None
    if calibre_text:
        d["recall_cal"] = recall(src_words["cal"], cal)
    d["pymupdf"] = pymupdf_opens(path)
    d["epubcheck"] = epubcheck(path, tools)
    return d


# --- driver --------------------------------------------------------------------------------------

def install_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--to", required=True, help="target formats, comma separated")
    ap.add_argument("--from", dest="sources", default="", help="only sources with these extensions")
    ap.add_argument("--tools", type=Path, help="tools.json from fetch_corpus.py (java, epubcheck)")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--calibre-text", action="store_true", help="also measure word recall with Calibre's text output")
    ap.add_argument("--keep", type=Path, help="keep the outputs in this folder")
    ap.add_argument("--originals", type=Path, help="folder of the EPUBs the sources were made from (for recall)")
    ap.add_argument("--bookport-options", default="", help="JSON options passed to bookport (e.g. for PDF)")
    a = ap.parse_args(argv)
    tools = json.loads(a.tools.read_text()) if a.tools else {}
    targets = [t.strip().lower() for t in a.to.split(",") if t.strip()]
    sources = sorted(p for p in a.corpus.iterdir() if p.is_file() and (
        not a.sources or p.suffix[1:].lower() in a.sources.split(",")))
    if a.limit:
        sources = sources[:a.limit]
    results = []
    tmp = Path(tempfile.mkdtemp(prefix="bp-compare-"))
    keep = a.keep
    bp_cmd = BOOKPORT_CMD
    if a.bookport_options:
        bp_cmd = ("import sys, json, bookport; bookport.convert(sys.argv[1], sys.argv[2], **json.loads(sys.argv[3]))")
    try:
        for src in sources:
            ref = src
            if a.originals and (a.originals / f"{src.name.split('.')[0]}.epub").exists():
                ref = a.originals / f"{src.name.split('.')[0]}.epub"
            src_words = {"bp": words_bookport(ref) or collections.Counter()}
            if a.calibre_text:
                src_words["cal"] = words_calibre(ref, tmp) or collections.Counter()
            src_info = describe(src)
            for fmt in targets:
                if fmt == src.suffix[1:].lower():
                    continue
                row = {"source": src.name, "to": fmt, "source_info": src_info}
                for tool in ("calibre", "bookport"):
                    out = (keep or tmp) / f"{src.stem}.{src.suffix[1:]}.{tool}.{fmt}"
                    if out.exists():
                        out.unlink()
                    if tool == "calibre":
                        cmd = [CALIBRE, str(src), str(out)]
                    else:
                        cmd = [sys.executable, "-c", bp_cmd, str(src), str(out)] + ([a.bookport_options] if a.bookport_options else [])
                    seconds, peak, code, err = run_measured(cmd)
                    r = {"seconds": round(seconds, 3), "peak_mb": round(peak / 2**20, 1), "ok": code == 0 and out.exists()}
                    if r["ok"]:
                        r.update(measure(out, src_words, tools, tmp, a.calibre_text))
                    else:
                        r["error"] = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
                    row[tool] = r
                results.append(row)
                c, b = row["calibre"], row["bookport"]
                print(f"{src.name[:30]:30} -> {fmt:5} | calibre {c['seconds']:6.2f}s {c['peak_mb']:6.0f}MB "
                      f"rec {c.get('recall_bp', 0):5.1f} toc {c.get('toc', '-')} img {c.get('images', '-')} "
                      f"ec {c.get('epubcheck')} | bookport {b['seconds']:6.2f}s {b['peak_mb']:6.0f}MB "
                      f"rec {b.get('recall_bp', 0):5.1f} toc {b.get('toc', '-')} img {b.get('images', '-')} "
                      f"ec {b.get('epubcheck')}" + ("" if b["ok"] else f" FAIL {b.get('error')}"), flush=True)
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
    summary(results, a.calibre_text)
    if a.json:
        import bookport
        a.json.write_text(json.dumps({
            "install_mb": {"calibre": round(install_size(CALIBRE_DIR) / 2**20, 1),
                           "bookport": round(install_size(Path(bookport.__file__).parent) / 2**20, 2)},
            "results": results}, indent=1))
    return 0


def _med(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def summary(results: list[dict], calibre_text: bool) -> None:
    by_fmt = collections.defaultdict(list)
    for r in results:
        by_fmt[r["to"]].append(r)
    cols = ["ok", "median s", "peak MB", "epubcheck err", "reopen bp", "reopen calibre", "reopen pymupdf",
            "recall % (worst, bookport reads)", "recall % (worst, calibre reads)", "TOC titles kept", "images kept", "metadata kept", "cover kept"]
    print("\n| to | tool | " + " | ".join(cols) + " |")
    print("|" + "---|" * (len(cols) + 2))
    for fmt, rows in by_fmt.items():
        for tool in ("calibre", "bookport"):
            rs = [r[tool] for r in rows]
            src = [r["source_info"] for r in rows]
            n = len(rs)
            ok = sum(1 for x in rs if x["ok"])
            def frac(key, rs=rs):
                vals = [x.get(key) for x in rs if x.get(key) is not None]
                return f"{sum(1 for v in vals if v)}/{len(vals)}" if vals else "-"
            ec = [x.get("epubcheck") for x in rs if x.get("epubcheck") is not None]
            toc = sum(toc_matched(s.get("toc_titles", []), x.get("toc_titles", [])) for x, s in zip(rs, src))
            toc_src = sum(len(s.get("toc_titles", [])) for s in src)
            img = sum(min(x.get("images", 0), s.get("images", 0)) for x, s in zip(rs, src))
            img_src = sum(s.get("images", 0) for s in src)
            md_kept = md_src = 0
            for x, s in zip(rs, src):
                for f in FIELDS:
                    if s.get("fields", {}).get(f):
                        md_src += 1
                        md_kept += bool(x.get("fields", {}).get(f))
            cov = sum(1 for x, s in zip(rs, src) if s.get("cover") and x.get("cover"))
            cov_src = sum(1 for s in src if s.get("cover"))
            worst = [x.get("recall_bp", 0.0) if x["ok"] else 0.0 for x in rs]
            rec = min(worst) if worst else None
            worst_cal = [x.get("recall_cal", 0.0) if x["ok"] else 0.0 for x in rs] if calibre_text else []
            rec_cal = min(worst_cal) if worst_cal else None
            cells = [f"{ok}/{n}", f"{_med([x['seconds'] for x in rs]):.2f}", f"{_med([x['peak_mb'] for x in rs]):.0f}",
                     str(sum(ec)) if ec else "-", frac("opens"), frac("calibre_opens"), frac("pymupdf"),
                     f"{rec:.2f}" if rec is not None else "-", f"{rec_cal:.2f}" if rec_cal is not None else "-", f"{toc}/{toc_src}", f"{img}/{img_src}",
                     f"{md_kept}/{md_src}", f"{cov}/{cov_src}"]
            print(f"| {fmt} | {tool} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    sys.exit(main())
