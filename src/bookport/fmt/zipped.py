"""Shared pieces for HTMLZ and TXTZ: safe extraction and the metadata.opf they carry."""

from __future__ import annotations

import posixpath
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from .. import limits, markup
from ..book import Book
from ..errors import BookportError
from ..limits import safe_path


@contextmanager
def extracted(path):
    """Extract a ZIP into a temporary folder, skipping names that would land outside it."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BookportError(f"Damaged ZIP file: {e}") from None
    with z, tempfile.TemporaryDirectory(prefix="bookport-") as tmp:
        root = Path(tmp)
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            norm = posixpath.normpath(name)
            if info.is_dir() or norm.startswith(("../", "/")) or norm == ".." or ":" in norm:
                continue
            dest = root / norm
            data = limits.zip_read(z, info)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)  # fails where a file has the folder's name
                dest.write_bytes(data)
            except OSError as e:
                raise BookportError(f"Can't unpack {name} from this ZIP file ({e})") from None
        yield root


def apply_opf(book: Book, opf_path: Path, root: Path | None = None) -> None:
    """Merge the metadata (and cover) from a metadata.opf next to the content; the cover must be
    inside ``root`` (the unpacked archive; by default the OPF's own folder)."""
    from .epub import _find, _local, _read_metadata, _xml
    try:
        opf = _xml(opf_path.read_bytes())
    except (OSError, BookportError):
        return
    meta_el = _find(opf, "metadata")
    md = _read_metadata(meta_el)
    for field in ("title", "language", "publisher", "date", "description", "rights", "series", "title_sort", "author_sort"):
        if getattr(md, field):
            setattr(book.metadata, field, getattr(md, field))
    if md.series_index is not None:
        book.metadata.series_index = md.series_index
    for field in ("authors", "subjects", "contributors"):
        if getattr(md, field):
            setattr(book.metadata, field, getattr(md, field))
    book.metadata.identifiers.update(md.identifiers)
    guide = _find(opf, "guide")
    if guide is not None:
        for ref in guide:
            if _local(ref.tag) == "reference" and ref.get("type") == "cover":
                p = safe_path(opf_path.parent, ref.get("href", ""), root or opf_path.parent)
                if p is not None and p.is_file():
                    data = p.read_bytes()
                    existing = next((h for h, r in book.resources.items() if r.data == data), None)
                    book.cover = existing or book.add("images/cover" + p.suffix.lower(), data).href


def metadata_opf(book: Book, cover_name: str | None = None) -> bytes:
    """A metadata-only OPF 2 package, as HTMLZ and TXTZ files carry."""
    from .epub import DC_NS, OPF_NS, _esc, _w3cdtf
    md = book.metadata
    out = ['<?xml version="1.0" encoding="utf-8"?>\n',
           f'<package xmlns="{OPF_NS}" version="2.0" unique-identifier="uid">\n',
           f'  <metadata xmlns:dc="{DC_NS}" xmlns:opf="{OPF_NS}">\n',
           f'    <dc:identifier id="uid" opf:scheme="uuid">{_esc(md.uid())}</dc:identifier>\n',
           f"    <dc:title>{_esc(md.title)}</dc:title>\n"]
    for a in md.authors:
        out.append(f'    <dc:creator opf:role="aut">{_esc(a)}</dc:creator>\n')
    out.append(f"    <dc:language>{_esc(md.language or 'en')}</dc:language>\n")
    for scheme, value in md.identifiers.items():
        if value != md.uid():
            out.append(f'    <dc:identifier opf:scheme="{_esc(scheme)}">{_esc(value)}</dc:identifier>\n')
    if md.publisher:
        out.append(f"    <dc:publisher>{_esc(md.publisher)}</dc:publisher>\n")
    if md.date and _w3cdtf(md.date):
        out.append(f"    <dc:date>{_esc(_w3cdtf(md.date))}</dc:date>\n")
    if md.description:
        out.append(f"    <dc:description>{_esc(md.description)}</dc:description>\n")
    for s in md.subjects:
        out.append(f"    <dc:subject>{_esc(s)}</dc:subject>\n")
    if md.rights:
        out.append(f"    <dc:rights>{_esc(md.rights)}</dc:rights>\n")
    if md.series:
        out.append(f'    <meta name="calibre:series" content="{_esc(md.series)}"/>\n')
        if md.series_index is not None:
            out.append(f'    <meta name="calibre:series_index" content="{md.series_index:g}"/>\n')
    out.append("  </metadata>\n")
    if cover_name:
        out.append(f'  <guide>\n    <reference type="cover" title="Cover" href="{_esc(cover_name)}"/>\n  </guide>\n')
    out.append("</package>\n")
    return "".join(out).encode("utf-8")


def text_files(z_root: Path, exts) -> list[Path]:
    return sorted((p for p in z_root.rglob("*") if p.is_file() and p.suffix.lower() in exts),
                  key=lambda p: (len(p.relative_to(z_root).parts), p.name.lower() not in ("index.html", "index.txt"), p.name))


def decode_file(p: Path) -> str:
    return markup.decode(p.read_bytes())
