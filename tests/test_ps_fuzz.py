"""Mutation fuzzing of the PostScript reader: damaged programs may only raise BookportError, quickly and in bounded
memory. Seeds are the test programs (shapes, text, a Type 1 font, pictures through filters, a DOS EPS); each is
damaged the same way every run (a fixed seed): flipped bytes, cuts, repeats, and PostScript tokens dropped in at
random (an extra brace, a huge number, a loop, eexec, restore...). Two child processes read the mutants."""

import json
import random
import struct
import subprocess
import sys
import textwrap
import zlib
from pathlib import Path

import bookport

from .test_ps_read import PROGRAMS, _a85, type1_font

SRC = str(Path(bookport.__file__).resolve().parent.parent)
MUTANTS = 60  # per seed
WORKERS = 2
SECONDS = 10
PEAK_MB = 256
TOKENS = [b"{", b"}", b"[", b"]", b"<<", b">>", b"(", b")", b"<", b"~>", b"1e308", b"-2147483648", b"2147483647",
          b"999999 array", b"99999999 string", b"{} loop", b"exec", b"currentfile", b"eexec", b"closefile", b"restore",
          b"save", b"showpage", b"clip", b"0 0 moveto", b"1000000 {} repeat", b"/x {x} def x", b"stop", b"exit",
          b"definefont", b"findfont", b"setfont", b"show", b"image", b"imagemask", b"shfill", b"makepattern",
          b"setcolorspace", b"0 div", b"dup", b"pop", b"count", b"roll", b"bind", b"//add", b"%%BoundingBox: 0 0 9 9"]

WORKER = textwrap.dedent("""
    import json, sys, time, tracemalloc
    sys.path.insert(0, sys.argv[1])
    import bookport
    from bookport import ps
    from bookport.ps import interp
    interp.MAX_STEPS = 2_000_000
    tracemalloc.start()
    for line in sys.stdin:
        path = json.loads(line)
        print(json.dumps({"start": path}), flush=True)
        tracemalloc.reset_peak()
        t = time.perf_counter()
        error = None
        try:
            ps.to_pdf(path)
        except bookport.BookportError:
            pass
        except BaseException as e:
            error = f"{type(e).__name__}: {str(e)[:200]}"
        print(json.dumps({"path": path, "error": error, "seconds": time.perf_counter() - t,
                          "peak_mb": tracemalloc.get_traced_memory()[1] / 2**20}), flush=True)
""")


def _seeds() -> dict[str, bytes]:
    out = {k: v.encode() for k, v in PROGRAMS.items()}
    out["type1"] = type1_font() + b"/TestBoxes findfont 50 scalefont setfont 100 100 moveto (ABBA) show showpage\n"
    rgb = bytes([255, 0, 0] * 16 + [0, 0, 255] * 16)
    out["picture"] = (b"%!PS\n100 100 translate 200 200 scale 4 8 8 [4 0 0 -8 0 8] currentfile /ASCII85Decode filter "
                      b"/FlateDecode filter false 3 colorimage\n" + _a85(zlib.compress(rgb)).encode() + b"~>\nshowpage")
    body = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 80 40\n1 0 0 setrgbcolor 0 0 80 40 rectfill\n"
    out["dos-eps"] = b"\xc5\xd0\xd3\xc6" + struct.pack("<IIIIIIH", 30, len(body), 0, 0, 0, 0, 0xFFFF) + body
    return out


def _mutate(data: bytes, rng: random.Random) -> bytes:
    b = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        kind = rng.randrange(5)
        if not b:
            break
        i = rng.randrange(len(b))
        if kind == 0:
            b[i] = rng.randrange(256)
        elif kind == 1:
            del b[i:]
        elif kind == 2:
            j = min(len(b), i + rng.randint(1, 64))
            b[i:i] = b[i:j] * rng.randint(2, 20)
        elif kind == 3:
            del b[i:i + rng.randint(1, 32)]
        else:
            b[i:i] = b" " + rng.choice(TOKENS) + b" "
    return bytes(b)


def test_damaged_programs_only_raise_bookport_errors(tmp_path):
    rng = random.Random(1588)
    paths = []
    for name, data in _seeds().items():
        for k in range(MUTANTS):
            p = tmp_path / f"{name}-{k}.ps"
            p.write_bytes(_mutate(data, rng))
            paths.append(str(p))
    chunks = [paths[i::WORKERS] for i in range(WORKERS)]
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, SRC], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True) for _ in chunks]
    results = []
    for proc, chunk in zip(procs, chunks):
        out, _ = proc.communicate("\n".join(json.dumps(p) for p in chunk) + "\n", timeout=SECONDS * len(chunk))
        results += [json.loads(line) for line in out.splitlines() if '"path"' in line]
    assert len(results) == len(paths)
    bad = [r for r in results if r["error"]]
    slow = [r for r in results if r["seconds"] > SECONDS]
    big = [r for r in results if r["peak_mb"] > PEAK_MB]
    assert not bad, bad[:5]
    assert not slow, slow[:5]
    assert not big, big[:5]
