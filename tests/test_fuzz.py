"""Mutation fuzzing over every reader: damaged books may only raise BookportError, quickly and in bounded memory.

A sample book is written to every writable format (plus a CHM, two Plucker files, one with pictures, and a comic), then
each file is mutated the same way every run (a fixed seed): flipped bytes, big numbers in header
fields, truncation, repeated and deleted ranges, and for ZIP-based formats the same damage to a
member inside the archive. Four child processes read the mutants; each read is timed and its peak
memory traced. Takes under a minute.
"""

import io
import json
import os
import random
import struct
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import bookport

from .conftest import png, sample_book
from .test_office import _odt
from .test_rare import _bitmap, _chm, _pixel_rows, _plucker, _plucker_pages

SRC = str(Path(bookport.__file__).resolve().parent.parent)
WRITE = ["epub", "mobi", "azw3", "fb2", "fbz", "html", "htmlz", "txt", "txtz", "md", "docx", "rtf", "pdf", "pdb", "pml",
         "pmlz", "tcr", "rb", "snb", "lrf", "lit"]
ZIPPED = {"epub", "fbz", "htmlz", "txtz", "docx", "pmlz", "cbz", "odt"}
MUTANTS = 50
WORKERS = 4
SECONDS = 10  # per file, with memory tracing on
PEAK_MB = 256  # per file

WORKER = textwrap.dedent("""
    import json, sys, time, tracemalloc
    sys.path.insert(0, sys.argv[1])
    import bookport
    tracemalloc.start()
    for line in sys.stdin:
        path, fmt = json.loads(line)
        print(json.dumps({"start": path}), flush=True)
        tracemalloc.reset_peak()
        t = time.perf_counter()
        error = None
        try:
            bookport.read(path, fmt)
        except bookport.BookportError:
            pass
        except BaseException as e:
            error = f"{type(e).__name__}: {str(e)[:200]}"
        if fmt in ("chm", "lit") and error is None:
            try:
                bookport.unpack(path, fmt)
            except bookport.BookportError:
                pass
            except BaseException as e:
                error = f"unpack: {type(e).__name__}: {str(e)[:200]}"
        print(json.dumps({"path": path, "error": error, "seconds": time.perf_counter() - t,
                          "peak_mb": tracemalloc.get_traced_memory()[1] / 2**20}), flush=True)
""")


def _seeds(tmp: Path) -> dict[str, bytes]:
    book = sample_book()
    out = {}
    for fmt in WRITE:
        p = book.write(tmp / f"seed.{fmt}")
        out[fmt] = p.read_bytes()
    out["chm"] = _chm({"/a.htm": b"<html><head><title>A</title></head><body><p>Alpha <a href='b.htm'>b</a></p></body></html>",
                       "/b.htm": b"<html><body><p>Beta <img src='i.png'></p></body></html>", "/i.png": png()})
    out["plucker"] = _plucker([[b"First paragraph.", b"Second one."], [b"Another page."]])
    pic = _bitmap(13, 5, 4, _pixel_rows([[x % 16 for x in range(13)]] * 5, 13, 4), compression="scanline")
    text = b"A picture \x00\x1a\x00\x05 and a grid \x00\x1a\x00\x06."
    out["plucker-images"] = _plucker_pages([struct.pack(">HHHBB", 2, 1, len(text), 0, 0) + struct.pack(">HH", len(text), 0) + text,
                                            struct.pack(">HHHBB", 5, 0, len(pic), 2, 0) + pic,
                                            struct.pack(">HHHBB", 6, 0, 8, 15, 0) + struct.pack(">HHHH", 2, 1, 5, 5)])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("001.png", png())
        z.writestr("002.png", png(20, 20))
        z.writestr("ComicInfo.xml", "<ComicInfo><Title>C</Title></ComicInfo>")
    out["cbz"] = buf.getvalue()
    out["odt"] = _odt(tmp).read_bytes()
    return out


BIG = [b"\xff\xff\xff\xff", b"\x7f\xff\xff\xff", b"\0\0\0\0", b"\x80\0\0\0", b"\0\0\x10\0"]


def _mutate(data: bytes, rng: random.Random) -> bytes:
    b = bytearray(data)
    n = len(b)
    kind = rng.randrange(6)
    if n < 8:
        return bytes(b) + bytes(rng.randrange(256) for _ in range(8))
    if kind == 0:  # flip a few bytes
        for _ in range(rng.randint(1, 8)):
            b[rng.randrange(n)] = rng.randrange(256)
    elif kind == 1:  # a big number in a header field
        pos = rng.randrange(min(n - 4, 512))
        b[pos:pos + 4] = rng.choice(BIG)
    elif kind == 2:  # a big number anywhere
        pos = rng.randrange(n - 4)
        b[pos:pos + 4] = rng.choice(BIG)
    elif kind == 3:  # cut it short
        del b[rng.randrange(1, n):]
    elif kind == 4:  # repeat a range
        i = rng.randrange(n)
        j = min(n, i + rng.randint(1, 256))
        b[i:i] = b[i:j] * rng.randint(2, 20)
    else:  # drop a range
        i = rng.randrange(n)
        del b[i:i + rng.randint(1, 256)]
    return bytes(b)


def _mutate_member(data: bytes, rng: random.Random) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    infos = [i for i in src.infolist() if not i.is_dir()]
    victim = rng.choice([i for i in infos if i.filename != "mimetype"] or infos)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in infos:
            body = src.read(info)
            if info is victim:
                body = _mutate(body, rng)
            z.writestr(info.filename, body, zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def test_mutated_books_only_raise_bookport_error(tmp_path):
    rng = random.Random(20261030)
    jobs = []
    for fmt, data in _seeds(tmp_path).items():
        read_as = {"plucker": "pdb", "plucker-images": "pdb", "md": "md"}.get(fmt, fmt)
        for k in range(MUTANTS):
            mutant = _mutate_member(data, rng) if fmt in ZIPPED and k % 2 else _mutate(data, rng)
            p = tmp_path / f"m{k:02d}.{fmt}"
            p.write_bytes(mutant)
            jobs.append((str(p), read_as))
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, SRC], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONHASHSEED": "0"})
             for _ in range(WORKERS)]
    results, hung = [], []
    for w, proc in enumerate(procs):
        mine = "".join(json.dumps(j) + "\n" for j in jobs[w::WORKERS])
        try:
            out, _err = proc.communicate(mine, timeout=150)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _err = proc.communicate()
            starts = [json.loads(x)["start"] for x in out.splitlines() if '"start"' in x]
            hung.append(starts[-1] if starts else "?")
        results += [json.loads(x) for x in out.splitlines() if '"path"' in x]
    assert not hung, f"reading hung: {hung}"
    bad = [r for r in results if r["error"]]
    slow = [r for r in results if r["seconds"] > SECONDS or r["peak_mb"] > PEAK_MB]
    assert len(results) == len(jobs), f"{len(jobs) - len(results)} reads crashed the worker"
    assert not bad, "\n".join(f"{Path(r['path']).name}: {r['error']}" for r in bad)
    assert not slow, "\n".join(f"{Path(r['path']).name}: {r['seconds']:.1f} s, {r['peak_mb']:.0f} MB" for r in slow)
