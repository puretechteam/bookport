"""Trims the bundled Noto fonts to what bookport reads, and stores them xz-compressed:

    python tools/slim_fonts.py SOURCE_DIR    # the five .ttf files from notofonts' release

GSUB (bookport doesn't substitute glyphs) is left out and the post table is cut to its header
(format 3: no glyph names; bookport's subsetter writes format 3 anyway). Every other table is
copied as it is. Needs fontTools (bookport's dev extra)."""

import io
import lzma
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

FONTS = Path(__file__).resolve().parent.parent / "src" / "bookport" / "fonts"


def main() -> None:
    src = Path(sys.argv[1])
    for ttf in sorted(src.glob("Noto*.ttf")):
        font = TTFont(ttf, recalcTimestamp=False)
        if "GSUB" in font:
            del font["GSUB"]
        font["post"].formatType = 3.0
        out = FONTS / (ttf.name + ".xz")
        buf = io.BytesIO()  # in memory: nothing is written next to the fonts but the .xz
        font.save(buf)
        out.write_bytes(lzma.compress(buf.getvalue(), preset=9 | lzma.PRESET_EXTREME))
        print(f"{ttf.name}: {ttf.stat().st_size:,} -> {out.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
