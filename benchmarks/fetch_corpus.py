"""Download the comparison corpus and test tools into a scratch folder.

    python benchmarks/fetch_corpus.py SCRATCH [--tools]

Public-domain books from Project Gutenberg (EPUB 3, Kindle MOBI, KF8 AZW3, plain text, HTML) and
Standard Ebooks (EPUB 3, AZW3), a CHM help file (Python's documentation), and with --tools,
epubcheck and a portable Java 21 to run it. Everything lands in SCRATCH; files already there are
kept, so the script can be re-run. Nothing is written anywhere else.

Every file is checked against the SHA-256 it had when the README's numbers were measured. A file
that doesn't match isn't saved (the library may have regenerated it since); ``--allow-changed``
keeps it anyway and prints its new hash.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

UA = "bookport-corpus/0.1 (format testing; one request at a time)"

# Gutenberg ids: novels, an illustrated book, poetry, a reference work with tables, non-Latin text
GUTENBERG = {
    "pride-and-prejudice": 1342,
    "alice": 11,
    "moby-dick": 2701,
    "frankenstein": 84,
    "sherlock-holmes": 1661,
    "leaves-of-grass": 1322,
    "elements-of-style": 37134,
    "les-miserables-1": 17489,
    "hong-lou-meng": 24264,
    "time-machine": 35,
}
GUTENBERG_KINDS = {
    "epub": "epub3.images",
    "mobi": "kindle.images",
    "azw3": "kf8.images",
    "txt": "txt.utf-8",
}

STANDARD_EBOOKS = {
    "se-dracula": "bram-stoker/dracula",
    "se-poe-short-fiction": "edgar-allan-poe/short-fiction",
    "se-meditations": "marcus-aurelius/meditations/george-long",
}

CHM = {"python-3.10-docs.chm": "https://www.python.org/ftp/python/3.10.11/python31011.chm"}

EPUBCHECK = "https://github.com/w3c/epubcheck/releases/download/v5.3.0/epubcheck-5.3.0.zip"
JRE = ("https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/"
       "OpenJDK21U-jre_x64_windows_hotspot_21.0.13_9.zip")

# the files the published comparison used
SHA256 = {
    "alice.azw3": "d8211269aed55ded8302f289950936db8368b4d4d384c463b722034152f8f1d2",
    "alice.epub": "4f81245d31380ac1d6eafb9a321e3691369bc70985736ae4bccf849a4f784036",
    "alice.mobi": "1b4222ffdf1862a39d0fae6d42c588f28a34c12ad73cc21c2849fe2646cac3e1",
    "alice.txt": "01b38ea4c710a84bc18d0bd41271a5a1a92b94e97b2812f4dece97d4a694725e",
    "elements-of-style.azw3": "74c06639e564dc5dc9ce64fe16c35ae7ca0041b90d9a2782fc04fa74b5b5085e",
    "elements-of-style.epub": "0cff4f18dccb7e80b6d4a8cde3535ff462866df369d94343eaef0119dda8cb2c",
    "elements-of-style.mobi": "45931c19eb2623124329dd487c9a02d42d6665bbd5f0bdf3774f7d5699548845",
    "elements-of-style.txt": "bf5312eb0fa36837253ec84211cdab51deae01744e1de3ffef528aee43c27496",
    "frankenstein.azw3": "bb23e01816942b03234bc72e94d278217aaf45eba384113ec4b5d73922a66865",
    "frankenstein.epub": "0bfcc8cacdc72e132272844ed465c6198f1049a8d2b9e3f16806d02beee452e3",
    "frankenstein.mobi": "d907f385b28a2af24f6186a646fd366ede1b84c4c688afe93da3f705862e8e74",
    "frankenstein.txt": "2b97b7f8aad508649481d8dd4f6b54f9b965ddb3ba634e9151c815f537b49e42",
    "hong-lou-meng.azw3": "506c85caec8ff2ff9de01253fbb7fa078684b0d3ad8588817c25bef815f512d0",
    "hong-lou-meng.epub": "b387660f0a27441ab6f724b0e3a0da30d4776e90158b9aad9e1f36b518ef3d21",
    "hong-lou-meng.mobi": "e287b66374a364e016e411f4de2a8caf0420fe3a12e57ff71eb0c9865206567e",
    "hong-lou-meng.txt": "ff1526996bf4b81807651921a85e5c1c0f1d1d123c9fa4553057ba6a3ec72011",
    "leaves-of-grass.azw3": "26e34c98137b75c6050a04e995bf6f81b5d9278598185c49c955b25757be8445",
    "leaves-of-grass.epub": "82a7fbe96015c9e03ecab4480d3fa3f243d274b38210ea3b2b4175dac4b5c1d5",
    "leaves-of-grass.mobi": "1b5d00f47eed75e8e5db5acd9afeb1fe50c69732fdb723bde32fb06f4b5af062",
    "leaves-of-grass.txt": "77c066a8774e2b04d8bfbc570d9aa5131410355cd6062ab5962ac773240f3140",
    "les-miserables-1.azw3": "12466624856ef0e8e728e5c0e69793e97b7586bffaab7b0373a89a54169f62ae",
    "les-miserables-1.epub": "905772726fd1008b65e9a19bd7162f069b3be0bdadce2ef0a29f1f37472dab32",
    "les-miserables-1.mobi": "c1f49986a50a985507f24022f7f376d00ac846d459cf6722edc085a4a23cd74a",
    "les-miserables-1.txt": "a5de514ba7b9f2e1790e7e259c4e8b7a35ae1d29e4bf9a5f8767039c58b80503",
    "moby-dick.azw3": "6b13d77e7deedfaba25d218cfc62149be7fa85181d81276a25d07b0ef320991b",
    "moby-dick.epub": "1bc88e79ef7cf9d5325abe0071e6302265b708df56d3af07db124c81e84197f6",
    "moby-dick.mobi": "6c9ca39ed66a3967c7fdfb1d0e2c937e507938546f0d639ce018087fff9ef597",
    "moby-dick.txt": "b5e88b71a1cda068e9ab1d94344c9d932338ba6da1a66def97a1aecd9cfcc87d",
    "pride-and-prejudice.azw3": "f6d7254e848d577d5baf6fa1f157b22f799ba1769bcdac1fb76028d71ff47148",
    "pride-and-prejudice.epub": "fd519471687295a8b2492d9e413bc24bee21ade9808d8af29899a16e8599064e",
    "pride-and-prejudice.mobi": "159a164d43cdf2955ac64eb4cf020d8e0c58a998cb04a725e5cf14194301aba1",
    "pride-and-prejudice.txt": "3f6bb9d6f78e0293b56acd4714dd68cb7d6d1d293402031ce9d5a216bcaf9d75",
    "python-3.10-docs.chm": "e66d2ce1b8a1cd16ae31e6f0e60ad1ce5edcf7851890bdee9333bfc52eb95561",
    "se-dracula.azw3": "072466cb7e81dbb72709dac75e28d65c7f94e8580237717575b12f85d3690f4b",
    "se-dracula.epub": "7189e267ffd12aeb59eed09850d5ba0c4caa9ccefcffc3025b38d25563c138f3",
    "se-meditations.azw3": "ddb1334442bee7985d6f038708c8a67e69ad84ce5c284365369cf47336a5d491",
    "se-meditations.epub": "c2b77e9daa37694b978f78f9ae2d801fd263be533af187e8b36b7da28845e0c8",
    "se-poe-short-fiction.azw3": "8a07d8002269b612b1ed4b5edd2a283ed2ffc819bb7bcff9a04095873bdc9e5b",
    "se-poe-short-fiction.epub": "cf5bf1c5fae87049415c552eaae8951126de9324a5aee714a3c0458cdf3dbc38",
    "sherlock-holmes.azw3": "f106332c7aa9e36fcf8fa53d1245a2e1c9e0dbe0c48e265f01523550e8216ab7",
    "sherlock-holmes.epub": "310ddf29f2b98b870b0e970061d236c9ce7c65163e0e172c254a1db3d070d3f4",
    "sherlock-holmes.mobi": "a374b1c7ccd719d6d4d557ee8fcce694f09ed26bc82e2b9a830dba884c994b53",
    "sherlock-holmes.txt": "922e2a12ccb43a4c9544c260b2166c6ad2097aeb5957faeee113f173bb857cd0",
    "time-machine.azw3": "28c4105cd4269e75fae7a716da4fd6be5957422b59e9c85d00cd4101a3c1febb",
    "time-machine.epub": "f52cfd0c629d87db3ef91ced77ef5fd7e8fc8f3a997c73aede05e67091d14eec",
    "time-machine.mobi": "6b757fc91aed3b4bef4fefcb53a039562f699c76d76922cb697d97cdf7c8d938",
    "time-machine.txt": "2892e919000e17c83e1dac51b30f4675db50536b644d7579fe8a89bb399a9bdc",
    "epubcheck.zip": "6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5",
    "jre21.zip": "2332c0b4cff6bed722da3567c91d5e47568bcba863fa4945d0a8d6fc76158628",
}

ALLOW_CHANGED = False


def fetch(url: str, dest: Path) -> bool:
    want = SHA256.get(dest.name)
    if dest.exists() and dest.stat().st_size:
        return _matches(dest.read_bytes(), want, dest) or ALLOW_CHANGED
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=120) as r:
            data = r.read()
    except Exception as e:  # noqa: BLE001 - report and carry on with the rest of the corpus
        print(f"  failed {url}: {e}")
        return False
    if not _matches(data, want, dest) and not ALLOW_CHANGED:
        return False
    dest.write_bytes(data)
    print(f"  {dest.name}: {len(data):,} bytes")
    time.sleep(1.0)  # be polite to the mirrors
    return True


def _matches(data: bytes, want: str | None, dest: Path) -> bool:
    got = hashlib.sha256(data).hexdigest()
    if got == want:
        return True
    print(f"  {dest.name}: SHA-256 {got} isn't the pinned {want or '(none)'}"
          + ("; keeping it (--allow-changed)" if ALLOW_CHANGED else "; not used"))
    return False


def se_name(path: str) -> str:
    return path.replace("/", "_")


def books(root: Path) -> None:
    for name, gid in GUTENBERG.items():
        for ext, kind in GUTENBERG_KINDS.items():
            fetch(f"https://www.gutenberg.org/ebooks/{gid}.{kind}", root / f"{name}.{ext}")
    for name, path in STANDARD_EBOOKS.items():
        base = f"https://standardebooks.org/ebooks/{path}/downloads/{se_name(path)}"
        fetch(f"{base}.epub?source=download", root / f"{name}.epub")
        fetch(f"{base}.azw3?source=download", root / f"{name}.azw3")
    for name, url in CHM.items():
        fetch(url, root / name)


def unzip(data_path: Path, dest: Path) -> None:
    if dest.exists():
        return
    with zipfile.ZipFile(io.BytesIO(data_path.read_bytes())) as z:
        z.extractall(dest)


def tools(root: Path) -> None:
    if fetch(EPUBCHECK, root / "epubcheck.zip"):
        unzip(root / "epubcheck.zip", root / "epubcheck")
    if fetch(JRE, root / "jre21.zip"):
        unzip(root / "jre21.zip", root / "jre21")
    java = next((root / "jre21").glob("*/bin/java.exe"), None)
    jar = next((root / "epubcheck").glob("*/epubcheck.jar"), None)
    (root / "tools.json").write_text(json.dumps({"java": str(java), "epubcheck": str(jar)}, indent=1))
    print(f"java: {java}\nepubcheck: {jar}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scratch", type=Path)
    ap.add_argument("--tools", action="store_true", help="also fetch epubcheck and a portable Java 21")
    ap.add_argument("--no-books", action="store_true")
    ap.add_argument("--allow-changed", action="store_true", help="keep files whose SHA-256 differs from the pinned one")
    a = ap.parse_args(argv)
    global ALLOW_CHANGED
    ALLOW_CHANGED = a.allow_changed
    if not a.no_books:
        books(a.scratch / "corpus")
    if a.tools:
        tools(a.scratch / "tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
