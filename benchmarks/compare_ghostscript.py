"""PostScript/EPS to PDF: bookport against Ghostscript, run as a black box on scratch copies.

    python benchmarks/compare_ghostscript.py <folder of .ps/.eps> --gs <gswin64c.exe> [--json out.json]

For each file both PDFs are drawn by the same renderer (PyMuPDF) at 150 dpi and compared page by page (SSIM over
7x7 windows, PSNR), and their text compared word by word. Times are the better of taking turns three times."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookport import ps

DPI = 150
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def render(pdf: bytes, dpi: int = DPI) -> list[np.ndarray]:
    import pymupdf
    out = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
            out.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).astype(np.float64))
    return out


def words(pdf: bytes) -> list[str]:
    import pymupdf
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return " ".join(p.get_text() for p in doc).split()


def _box(a: np.ndarray, k: int) -> np.ndarray:
    c = np.cumsum(np.cumsum(np.pad(a, ((1, 0), (1, 0))), 0), 1)
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def ssim(a: np.ndarray, b: np.ndarray, k: int = 7) -> float:
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a, b = a[:h, :w], b[:h, :w]
    if h < k or w < k:
        return 1.0 if np.array_equal(a, b) else 0.0
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = _box(a, k), _box(b, k)
    va = _box(a * a, k) - ma * ma
    vb = _box(b * b, k) - mb * mb
    cov = _box(a * b, k) - ma * mb
    s = ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))
    return float(s.mean())


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    mse = float(((a[:h, :w] - b[:h, :w]) ** 2).mean())
    return 99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def ghostscript(gs: str, src: Path, eps: bool, work: Path) -> tuple[bytes, float]:
    local = work / ("in.eps" if eps else "in.ps")
    shutil.copyfile(src, local)
    out = work / "gs.pdf"
    args = [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", f"-sOutputFile={out.name}"]
    if eps:
        args.append("-dEPSCrop")
    t = time.perf_counter()
    subprocess.run([*args, local.name], cwd=work, capture_output=True, timeout=600, creationflags=NO_WINDOW,
                   check=False)
    took = time.perf_counter() - t
    return (out.read_bytes() if out.exists() else b""), took


def compare(src: Path, gs: str, work: Path) -> dict:
    eps = src.suffix.lower() in (".eps", ".epsf", ".epsi")
    ours_t, gs_t = [], []
    ours = gs_pdf = b""
    notes: list[str] = []
    error = ""
    for _ in range(3):  # taking turns, best of three
        gs_pdf, t = ghostscript(gs, src, eps, work)
        gs_t.append(t)
        t = time.perf_counter()
        try:
            notes = []
            ours = ps.to_pdf(src, eps=eps or None, notes=notes)
        except Exception as e:  # noqa: BLE001 (the report says what went wrong)
            error = f"{type(e).__name__}: {e}"
            ours = b""
        ours_t.append(time.perf_counter() - t)
        if error:
            break
    row = {"file": src.name, "kb": round(src.stat().st_size / 1024, 1), "ours_s": round(min(ours_t), 3),
           "gs_s": round(min(gs_t), 3), "error": error, "notes": notes, "ours_kb": round(len(ours) / 1024, 1),
           "gs_kb": round(len(gs_pdf) / 1024, 1)}
    if not ours or not gs_pdf:
        return row
    a, b = render(gs_pdf), render(ours)
    row["pages"] = (len(a), len(b))
    row["ssim"] = [round(ssim(x, y), 4) for x, y in zip(a, b)]
    row["psnr"] = [round(psnr(x, y), 1) for x, y in zip(a, b)]
    row["size_match"] = all(x.shape == y.shape for x, y in zip(a, b))
    wa, wb = words(gs_pdf), words(ours)
    if wa:
        from collections import Counter
        ca, cb = Counter(wa), Counter(wb)
        row["word_recall"] = round(sum((ca & cb).values()) / sum(ca.values()), 4)
    return row


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--gs", required=True)
    p.add_argument("--json")
    p.add_argument("--only")
    a = p.parse_args()
    rows = []
    with tempfile.TemporaryDirectory(prefix="bookport-gs-") as tmp:
        for src in sorted(Path(a.folder).iterdir()):
            if src.suffix.lower() not in (".ps", ".eps", ".epsf", ".epsi") or (a.only and a.only not in src.name):
                continue
            row = compare(src, a.gs, Path(tmp))
            rows.append(row)
            ss = row.get("ssim") or []
            print(f"{row['file']:<34} pages {row.get('pages')} ssim min {min(ss) if ss else '-'} "
                  f"words {row.get('word_recall', '-')} ours {row['ours_s']}s gs {row['gs_s']}s "
                  f"{row['error'][:80]}", flush=True)
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
