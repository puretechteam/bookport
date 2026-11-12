"""Fonts as a PDF uses them, for the PDF to PostScript writer: which PostScript font and which byte each
character code becomes, its width, and the font's definition for the setup (see write_fonts for the programs)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..pdf.parser import Stream
from . import write_fonts as wf
from .write_paint import _f, hexstr, matrix, name, wrap

if TYPE_CHECKING:
    from .write import Writer

AFM = Path(__file__).with_name("afm")


_AFM_CACHE: dict[str, dict[str, float] | None] = {}


def afm_widths(font: str) -> dict[str, float] | None:
    """Glyph widths of a standard font from Adobe's metrics, when bookport carries them."""
    if font not in _AFM_CACHE:
        path = AFM / f"{font}.afm"
        widths = None
        if path.is_file():
            widths = {}
            for m in re.finditer(r"^C\s+-?\d+\s*;\s*WX\s+([\d.]+)\s*;\s*N\s+(\S+)", path.read_text("latin-1"), re.MULTILINE):
                widths[m.group(2)] = float(m.group(1))
        _AFM_CACHE[font] = widths
    return _AFM_CACHE[font]


class FontRec:
    """A PDF font as written to PostScript: its kind, how codes become PostScript fonts and slots, widths."""

    def __init__(self, w: Writer, key: str, fd: dict):
        self.w = w
        doc = w.doc
        r = doc.resolve
        self.ps = key
        self.subtype = str(r(fd.get("Subtype", "Type1")))
        self.base = str(r(fd.get("BaseFont", "")) or "")
        self.composite = self.subtype == "Type0"
        self.vertical = False
        self.type3_scale = 0.001
        self.has_widths = True
        self.used: set = set()
        self.slots: dict = {}
        self.subs: list[list] = []
        self.kind = "std"
        self.std = "Helvetica"
        self.names: list[str] = [".notdef"] * 256
        self.tt: wf.TrueType | None = None
        self.cff: wf.CFF | None = None
        self.program: str | None = None
        self.code_gid: dict[int, int] = {}
        self.cid_gid: dict[int, int] | None = None
        self.tounicode: dict[bytes, str] = {}
        self.gname: dict = {}
        self.taken: set[str] = set()
        self.open_sub: dict[int, int] = {}
        self.sub_group: list[int] = []
        self.tt_names: dict[int, str] = {}
        desc = r(fd.get("FontDescriptor")) or {}
        if self.composite:
            descendants = r(fd.get("DescendantFonts")) or []
            d0 = r(descendants[0]) if isinstance(descendants, list) and descendants else {}
            d0 = d0 if isinstance(d0, dict) else {}
            self.cidtype = str(r(d0.get("Subtype", "CIDFontType2")))
            desc = r(d0.get("FontDescriptor")) or {}
            self.dw = _f(r(d0.get("DW", 1000)), 1000)
            self.cwidths = wf.cid_widths(doc, d0.get("W"))
            enc = r(fd.get("Encoding"))
            if isinstance(enc, Stream):
                self.cmap = wf.CidMap(doc.decode(enc))
            else:
                self.cmap = wf.CidMap()
                if isinstance(enc, str) and not enc.startswith("Identity"):
                    w.note(f"The font {wf._plain(self.base) or 'in the PDF'} uses the {enc} character map, which "
                           "bookport doesn't carry, so its text may show the wrong glyphs")
                self.vertical = isinstance(enc, str) and enc.endswith("-V")
            c2g = r(d0.get("CIDToGIDMap"))
            if isinstance(c2g, Stream):
                raw = doc.decode(c2g)
                self.cid_gid = {i: int.from_bytes(raw[2 * i:2 * i + 2], "big") for i in range(len(raw) // 2)}
        else:
            first = r(fd.get("FirstChar", 0))
            first = first if isinstance(first, int) else 0
            ws = r(fd.get("Widths"))
            self.widths = {}
            if isinstance(ws, list):
                for i, v in enumerate(ws):
                    v = r(v)
                    self.widths[first + i] = _f(v)
            else:
                self.has_widths = False
            self.missing = _f(r(desc.get("MissingWidth", 0))) if isinstance(desc, dict) else 0.0
            if self.subtype == "Type3":
                fm = matrix(doc, fd.get("FontMatrix"), (0.001, 0, 0, 0.001, 0, 0))
                self.type3_scale = fm[0]
        self.desc = desc if isinstance(desc, dict) else {}
        tu = r(fd.get("ToUnicode"))
        if isinstance(tu, Stream):
            from ..pdf.reader import CMap
            try:
                self.tounicode = CMap(doc.decode(tu)).map
            except (ValueError, IndexError, TypeError):
                self.tounicode = {}
        self._choose(fd)

    # how the font is written
    def _choose(self, fd: dict):
        doc = self.w.doc
        r = doc.resolve
        desc = self.desc
        flags = r(desc.get("Flags", 0))
        self.flags = flags if isinstance(flags, int) else 0
        if self.subtype == "Type3":
            self.kind = "t3"
            self.fd = fd
            self._simple_names(fd, builtin=[".notdef"] * 256)
            return
        ff, ff2, ff3 = (wf.stream_bytes(doc, desc.get(k)) for k in ("FontFile", "FontFile2", "FontFile3"))
        try:
            if ff and not self.composite:
                prog = wf.type1_program(ff, "/" + self.ps + "orig")
                if prog:
                    self.program, _old = prog
                    self.kind = "t1"
                    self._simple_names(fd, builtin=wf.builtin_encoding(ff))
                    return
            if ff3:
                cff = wf.cff_of(ff3, "")
                if cff is None and ff3[:4] in (b"\x00\x01\x00\x00", b"true"):
                    ff2 = ff3
                elif cff is not None:
                    self.cff = wf.CFF(cff)
                    self.kind = "cff"
                    if not self.composite:
                        self._simple_names(fd, builtin=self.cff.encoding or list(wf.tables.STANDARD))
                    return
            if ff2:
                self.tt = wf.TrueType(ff2)
                self.kind = "tt"
                if not self.composite:
                    self._tt_codes(fd)
                return
        except (ValueError, IndexError, KeyError, wf.struct.error):
            self.w.note(f"The font {wf._plain(self.base) or 'in the PDF'} is damaged, so its text uses a standard font")
        # not embedded (or unusable): a standard font
        self.kind = "std"
        self.std = wf.standard_for(self.base, self.flags, r(desc.get("FontWeight")), r(desc.get("ItalicAngle")))
        plain = wf._plain(self.base)
        if plain != self.std and plain not in ("", self.std):
            self.w.note(f"The font {plain} isn't in the PDF, so its text uses {self.std}")
        symbolic = self.std in ("Symbol", "ZapfDingbats")
        if not self.composite:
            self._simple_names(fd, builtin=None if symbolic else list(wf.tables.STANDARD))
            if not self.has_widths:
                afm = afm_widths(self.std)
                if afm:
                    self.widths = {c: afm.get(n, 0.0) for c, n in enumerate(self.names) if n != ".notdef"}
                    self.has_widths = True

    def _simple_names(self, fd: dict, builtin: list[str] | None):
        doc = self.w.doc
        enc = doc.resolve(fd.get("Encoding"))
        self.builtin = builtin
        if isinstance(enc, str):
            self.names = list(wf.BASE_NAMES.get(enc, wf.tables.STANDARD))
            self.reencode = True
        elif isinstance(enc, dict):
            be = doc.resolve(enc.get("BaseEncoding"))
            base = wf.BASE_NAMES.get(be) if isinstance(be, str) else None
            self.names = list(base or builtin or wf.tables.STANDARD)
            for code, g in wf.differences(doc, enc).items():
                self.names[code] = g
            self.reencode = True
        else:
            self.names = list(builtin or wf.tables.STANDARD)
            self.reencode = builtin is not None and self.kind != "t1"

    def _tt_codes(self, fd: dict):
        """Simple TrueType: each code to a glyph (ISO 32000-1, 9.6.6.4)."""
        doc = self.w.doc
        tt = self.tt
        cm = tt.cmaps
        enc = doc.resolve(fd.get("Encoding"))
        names: dict[int, str] = {}
        if isinstance(enc, str):
            names = dict(enumerate(wf.BASE_NAMES.get(enc, wf.tables.WINANSI)))
        elif isinstance(enc, dict):
            be = doc.resolve(enc.get("BaseEncoding"))
            names = dict(enumerate(wf.BASE_NAMES.get(be, wf.tables.STANDARD))) if isinstance(be, str) else {}
            names.update(wf.differences(doc, enc))
        self.tt_names = names
        symbolic = bool(self.flags & 4) or not names
        uni = cm.get((3, 1)) or cm.get((3, 10)) or cm.get((0, 3)) or cm.get((0, 4))
        sym = cm.get((3, 0))
        mac = cm.get((1, 0))
        for code in range(256):
            gid = 0
            nm = names.get(code)
            if nm and nm != ".notdef" and uni and not symbolic:
                u = wf.glyph_to_unicode(nm)
                if u:
                    gid = uni.get(ord(u), 0)
            if not gid and sym:
                gid = sym.get(code) or sym.get(0xF000 + code) or sym.get(0xF100 + code) or sym.get(0xF200 + code) or 0
            if not gid and mac:
                gid = mac.get(code, 0)
            if not gid and uni and nm:
                u = wf.glyph_to_unicode(nm) if nm != ".notdef" else None
                if u:
                    gid = uni.get(ord(u), 0)
                elif symbolic:
                    gid = uni.get(code, 0)
            if not gid and not cm:
                gid = code
            if gid:
                self.code_gid[code] = gid

    # showing text
    def width(self, code: int, cid: int | None = None) -> float:
        if self.composite:
            return self.cwidths.get(cid if cid is not None else code, self.dw) / 1000
        w = self.widths.get(code, self.missing) if self.has_widths else (600 if "Courier" in self.std else 500)
        return w * self.type3_scale if self.subtype == "Type3" else w / 1000

    def glyphs(self, s: bytes) -> list[tuple[str, int, float, bool]]:
        """(PostScript font name, byte to show, advance in text space per unit of size, is a single-byte space)."""
        out = []
        if self.composite:
            for raw, cid in self.cmap.codes(s):
                key = self._glyph_key(cid, raw)
                slot = self.slots.get(key)
                if slot is None:
                    group = self.cff.fdselect[key] if self.kind == "cff" and self.cff.cid and \
                        0 <= key < len(self.cff.fdselect) else 0
                    k = self.open_sub.get(group)
                    if k is None or len(self.subs[k]) >= 256:
                        self.subs.append([None])  # slot 0 stays .notdef
                        self.sub_group.append(group)
                        k = self.open_sub[group] = len(self.subs) - 1
                    self.subs[k].append(key)
                    self.gname[key] = self._name_for(key, raw)
                    slot = self.slots[key] = (k, len(self.subs[k]) - 1)
                sub, byte = slot
                out.append((f"{self.ps}_{sub}", byte, self.width(0, cid), len(raw) == 1 and raw == b" "))
            return out
        for b in s:
            self.used.add(b)
            out.append((self.ps, b, self.width(b), b == 32))
        return out

    def _name_for(self, key, raw: bytes | None, base: str | None = None) -> str:
        """A glyph name that says which character it is (so text stays extractable), unique in the font."""
        u = self.tounicode.get(raw) if raw is not None else None
        if u and len(u) <= 4 and not base:
            base = "_".join(wf.unicode_name_cached(ch) for ch in u)
        if not base and self.kind == "cff" and not self.cff.cid and isinstance(key, int) and key < len(self.cff.charset):
            base = self.cff.sid(self.cff.charset[key])
        if isinstance(key, str):
            base = base or key
        if not base or base == ".notdef":
            base = f"g{key}"
        nm = base if base not in self.taken else f"{base}.g{key}"
        self.taken.add(nm)
        return nm

    def _glyph_key(self, cid: int, raw: bytes):
        if self.kind == "tt":
            gid = self.cid_gid.get(cid, 0) if self.cid_gid is not None else cid
            return gid
        if self.kind == "cff":
            cff = self.cff
            return cff.by_cid.get(cid, 0) if cff.cid else cid
        u = self.tounicode.get(raw, "")
        return wf.unicode_name_cached(u) if u else ".notdef"

    # writing the font
    def define(self) -> str:
        if self.composite:
            return "".join(self._define_sub(k, keys) for k, keys in enumerate(self.subs))
        if self.kind == "t1":
            return self.program + self._reencode(f"/{self.ps}orig", self.names, copy_builtin=not self.reencode)
        if self.kind == "cff":
            return self._cff_simple()
        if self.kind == "tt":
            gids = {self.code_gid[c] for c in self.used if c in self.code_gid}
            enc = [".notdef"] * 256
            charstrings: dict[str, int] = {}
            by_gid: dict[int, str] = {}
            for c in sorted(self.used):
                gid = self.code_gid.get(c)
                if gid is None:
                    continue
                if gid not in by_gid:
                    pdf_name = self.tt_names.get(c)
                    by_gid[gid] = self._name_for(gid, bytes([c]), pdf_name if pdf_name and pdf_name != ".notdef" else None)
                    charstrings[by_gid[gid]] = gid
                enc[c] = by_gid[gid]
            sf = self.tt.sfnts(gids)
            sfnts_name = f"{self.ps}sf"
            return (f"/{sfnts_name} [" + "\n".join(hexstr(s) for s in sf) + "] def\n"
                    + wf.type42_font("/" + self.ps, sfnts_name, self.tt.bbox, enc, charstrings))
        if self.kind == "t3":
            return self.w.type3(self)
        return self._reencode("/" + self.std, self.names if self.reencode else None)

    def _reencode(self, src: str, names: list[str] | None, copy_builtin: bool = False) -> str:
        head = f"{src} findfont dup length 1 add dict begin {{1 index /FID ne {{def}} {{pop pop}} ifelse}} forall\n"
        if names is not None and not copy_builtin:
            enc = "/Encoding [" + wrap([name(n) for n in names]) + "] def\n"
        else:
            enc = ""
        return head + enc + f"/FontName /{self.ps} def currentdict end /{self.ps} exch definefont pop\n"

    def _cff_simple(self) -> str:
        cff = self.cff
        wanted = {}
        for code in self.used:
            g = self.names[code]
            gid = cff.names.get(g)
            if gid is None and cff.encoding and not self.reencode:
                gid = cff.names.get(cff.encoding[code])
            if gid is not None:
                wanted[g] = gid
        charstrings = {}
        for g, gid in wanted.items():
            width, path = cff.outline(gid)
            charstrings[g] = wf.type1_charstring(width, path)
        enc = [n if n in wanted else ".notdef" for n in self.names]
        return wf.type1_font("/" + self.ps, cff.matrix, cff.bbox, enc, charstrings)

    def _define_sub(self, k: int, keys: list) -> str:
        fontname = f"/{self.ps}_{k}"
        if self.kind == "tt":
            if k == 0:
                every = {g for sub in self.subs for g in sub if isinstance(g, int)}
                head = f"/{self.ps}sf [" + "\n".join(hexstr(s) for s in self.tt.sfnts(every)) + "] def\n"
            else:
                head = ""
            enc = [".notdef"] + [self.gname[g] for g in keys[1:]]
            enc += [".notdef"] * (256 - len(enc))
            return head + wf.type42_font(fontname, f"{self.ps}sf", self.tt.bbox, enc,
                                         {self.gname[g]: g for g in keys[1:]})
        if self.kind == "cff":
            cff = self.cff
            charstrings = {}
            enc = [".notdef"] * 256
            matrix = cff.glyph_matrix(keys[1]) if len(keys) > 1 else cff.matrix  # (a subfont holds one FD's glyphs)
            for slot, gid in enumerate(keys[1:], start=1):
                width, path = cff.outline(gid)
                g = self.gname[gid]
                enc[slot] = g
                charstrings[g] = wf.type1_charstring(width, path)
            return wf.type1_font(fontname, matrix, cff.bbox, enc, charstrings)
        # not embedded: glyph names in a standard font
        names = [".notdef"] + list(keys[1:])
        names += [".notdef"] * (256 - len(names))
        src = "/" + wf.standard_for(self.base, self.flags, None, None)
        return (f"{src} findfont dup length 1 add dict begin {{1 index /FID ne {{def}} {{pop pop}} ifelse}} forall\n"
                "/Encoding [" + wrap([name(n) for n in names]) + "] def\n"
                f"/FontName {fontname} def currentdict end {fontname} exch definefont pop\n")

    def ps_names(self) -> list[str]:
        return [f"{self.ps}_{k}" for k in range(len(self.subs))] if self.composite else [self.ps]
