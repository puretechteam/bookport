"""EPUB 2 and 3 (IDPF/W3C): read and write.

Reading follows the OPF (metadata, manifest, spine, guide), the EPUB 3 navigation document or
the NCX for the TOC, landmarks and page list, and undoes IDPF and Adobe font obfuscation.
Books with any other encryption (Adobe ADEPT, Readium LCP, Apple FairPlay, ...) raise DRMError.

Writing produces EPUB 3.3 with an NCX as well (so EPUB 2 readers get a TOC), or pure EPUB 2
with ``version=2``. Documents go through clean.py first, so the result passes epubcheck.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import quote, unquote

from .. import limits, markup
from ..book import CSS, NCX, XHTML, Book, Landmark, Metadata, TocEntry, guess_type, normalize_href, relative, resolve
from ..errors import BookportError, DRMError

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
IDPF_FONT = "http://www.idpf.org/2008/embedding"
ADOBE_FONT = "http://ns.adobe.com/pdf/enc#RC"


def _xml(data: bytes) -> ET.Element:
    what = "An XML file in this EPUB"
    markup.check_xml(data, None, what)
    parser = markup.xml_parser()
    try:
        parser.feed(markup.decode(data).lstrip("﻿").encode("utf-8") if not isinstance(data, str) else data)
        root = parser.close()
    except ET.ParseError:
        # some OPFs have stray bytes or undeclared prefixes; try the forgiving way
        text = markup.decode(data)
        text = re.sub(r"<\?xml[^>]*\?>", "", text)
        text = re.sub(r"\s(\w+):(\w+)=", r" \1_\2=", text) if "xmlns" not in text else text
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as e:
            raise BookportError(f"Damaged EPUB: an XML file can't be read ({e})") from None
    markup.check_xml("", root, what)
    return markup._strip_ns(root)


def _local(tag: str) -> str:
    return tag.split(":", 1)[-1] if isinstance(tag, str) else ""


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _find(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return c
    return None


# --- reading -----------------------------------------------------------------------------------

def read(path_or_file) -> Book:
    try:
        z = zipfile.ZipFile(path_or_file)
    except (zipfile.BadZipFile, OSError) as e:
        raise BookportError(f"Not an EPUB (not a ZIP file): {e}") from None
    with z:
        return read_zip(z)


def read_zip(z: zipfile.ZipFile) -> Book:
    names = {n: n for n in z.namelist()}
    lower = {n.lower(): n for n in names}

    def get(name):
        real = names.get(name) or lower.get(name.lower())
        if real is None:
            real = names.get(unquote(name)) or lower.get(unquote(name).lower())
        if real is None:
            raise KeyError(name)
        return limits.zip_read(z, real)

    _check_drm(z, lower, get)
    opf_path = _find_opf(names, lower, get)
    opf = _xml(get(opf_path))
    base = posixpath.dirname(opf_path)
    book = Book()
    book.source_format = "epub"
    meta_el = _find(opf, "metadata")
    manifest_el = _find(opf, "manifest")
    spine_el = _find(opf, "spine")
    if manifest_el is None or spine_el is None:
        raise BookportError("Damaged EPUB: the package file has no manifest or spine")
    obfuscated = _obfuscated(lower, get)
    unique_id_value = _unique_identifier(opf, meta_el)

    def to_href(zip_path):
        rel = posixpath.relpath(zip_path, base) if base else zip_path
        return normalize_href(zip_path if rel.startswith("..") else rel)

    items = {}
    nav_href = ncx_href = None
    for item in _children(manifest_el, "item"):
        iid, href, mt = item.get("id", ""), item.get("href", ""), item.get("media-type", "")
        if not href:
            continue
        zip_path = normalize_href(posixpath.join(base, unquote(href.split("#")[0])))
        try:
            data = get(zip_path)
        except KeyError:
            continue
        if zip_path in obfuscated:
            data = _deobfuscate(data, obfuscated[zip_path], unique_id_value, meta_el)
        props = item.get("properties", "")
        bhref = to_href(zip_path)
        mt = mt.lower().strip() or guess_type(bhref)
        if mt in ("text/html", "application/xml") and bhref.lower().endswith((".xhtml", ".html", ".htm")):
            mt = XHTML
        if mt == "text/x-oeb1-document":
            mt = XHTML
        if mt == "text/x-oeb1-css":
            mt = CSS
        if mt == NCX:
            ncx_href = bhref
        if "nav" in props.split():
            nav_href = bhref
        res = book.add(bhref, data, mt, unique=False)
        res.id = iid
        res.properties = " ".join(p for p in props.split() if p not in ("nav", "cover-image"))
        items[iid] = (bhref, props)
    spine_hrefs = []
    for ref in _children(spine_el, "itemref"):
        it = items.get(ref.get("idref", ""))
        if it and book.resources[it[0]].is_document:
            spine_hrefs.append(it[0])
    book.spine = list(dict.fromkeys(spine_hrefs))
    ppd = spine_el.get("page-progression-direction", "")
    book.direction = ppd if ppd in ("rtl", "ltr") else ""
    if ncx_href is None:
        toc_id = spine_el.get("toc")
        if toc_id in items:
            ncx_href = items[toc_id][0]
    book.metadata = _read_metadata(meta_el)
    book.fixed_layout = any(_local(m.tag) == "meta" and m.get("property") == "rendition:layout"
                            and (m.text or "").strip() == "pre-paginated" for m in meta_el.iter()) if meta_el is not None else False
    if not book.fixed_layout and "com.apple.ibooks.display-options.xml" in "".join(lower):
        try:
            opts = get(lower["meta-inf/com.apple.ibooks.display-options.xml"])
            book.fixed_layout = b"fixed-layout" in opts and b"true" in opts
        except KeyError:
            pass
    # TOC
    if nav_href:
        _read_nav(book, nav_href)
    if not book.toc and ncx_href:
        _read_ncx(book, ncx_href)
    guide = _find(opf, "guide")
    if not book.landmarks and guide is not None:
        for ref in _children(guide, "reference"):
            h = ref.get("href", "")
            if h:
                target = to_href(normalize_href(posixpath.join(base, unquote(h.split("#")[0]))))
                frag = h.split("#", 1)[1] if "#" in h else ""
                if target in book.resources:
                    book.landmarks.append(Landmark(_guide_type(ref.get("type", "")), ref.get("title", ""),
                                                   target + (f"#{frag}" if frag else "")))
    book.cover = _find_cover(book, meta_el, items)
    # the nav document is regenerated on write; keep it only if it's part of the reading order
    if nav_href and nav_href not in book.spine:
        book.remove(nav_href)
    if ncx_href:
        book.remove(ncx_href)
    for href in list(book.resources):
        if href.lower().endswith(".opf"):
            book.remove(href)
    return book


def _check_drm(z, lower, get):
    if "meta-inf/rights.xml" in lower:
        rights = get(lower["meta-inf/rights.xml"])
        if b"adept" in rights.lower():  # a rights.xml alone (a licence, say) isn't DRM
            raise DRMError("EPUB", "it uses Adobe Digital Editions (ADEPT) DRM")
    if "meta-inf/license.lcpl" in lower:
        raise DRMError("EPUB", "it uses Readium LCP DRM")
    if "meta-inf/sinf.xml" in lower:
        raise DRMError("EPUB", "it uses Apple FairPlay DRM")
    if "meta-inf/encryption.xml" in lower:
        enc = _xml(get(lower["meta-inf/encryption.xml"]))
        for data in enc.iter():
            if _local(data.tag) != "EncryptedData":
                continue
            method = _find(data, "EncryptionMethod")
            alg = method.get("Algorithm", "") if method is not None else ""
            if alg not in (IDPF_FONT, ADOBE_FONT):
                raise DRMError("EPUB", "its files are encrypted")


def _obfuscated(lower, get) -> dict:
    out = {}
    if "meta-inf/encryption.xml" not in lower:
        return out
    enc = _xml(get(lower["meta-inf/encryption.xml"]))
    for data in enc.iter():
        if _local(data.tag) != "EncryptedData":
            continue
        method = _find(data, "EncryptionMethod")
        ref = _find(data, "CipherReference")
        if method is not None and ref is not None:
            out[normalize_href(unquote(ref.get("URI", "")))] = method.get("Algorithm", "")
    return out


def _unique_identifier(opf, meta_el):
    uid_id = opf.get("unique-identifier")
    if meta_el is None:
        return ""
    for el in meta_el:
        if _local(el.tag) == "identifier" and (uid_id is None or el.get("id") == uid_id):
            return (el.text or "").strip()
    return ""


def _deobfuscate(data: bytes, alg: str, uid: str, meta_el) -> bytes:
    if alg == IDPF_FONT:
        key = hashlib.sha1("".join(uid.split()).encode("utf-8")).digest()
        n = 1040
    elif alg == ADOBE_FONT:
        # Adobe's key: the 16 bytes of the urn:uuid identifier
        ident = ""
        for el in meta_el.iter() if meta_el is not None else ():
            t = (el.text or "").strip()
            if _local(el.tag) == "identifier" and "uuid" in t.lower():
                ident = t
                break
        hexs = re.sub(r"[^0-9a-fA-F]", "", ident.split(":")[-1])
        if len(hexs) < 32:
            return data
        key = bytes.fromhex(hexs[:32])
        n = 1024
    else:
        return data
    head = bytes(b ^ key[i % len(key)] for i, b in enumerate(data[:n]))
    return head + data[n:]


def _find_opf(names, lower, get) -> str:
    try:
        container = _xml(get("META-INF/container.xml"))
        for rf in container.iter():
            if _local(rf.tag) == "rootfile" and rf.get("full-path"):
                p = normalize_href(unquote(rf.get("full-path")))
                if p in names or p.lower() in lower:
                    return names.get(p) or lower[p.lower()]
    except KeyError:
        pass
    for n in names:
        if n.lower().endswith(".opf"):
            return n
    raise BookportError("Not an EPUB: no package (.opf) file")


def _guide_type(t: str) -> str:
    t = t.lower().strip()
    return {"text": "bodymatter", "start": "bodymatter", "title-page": "titlepage", "copyright-page": "copyright-page",
            "toc": "toc", "cover": "cover", "index": "index", "glossary": "glossary", "acknowledgements": "acknowledgments",
            "bibliography": "bibliography", "colophon": "colophon", "dedication": "dedication", "epigraph": "epigraph",
            "foreword": "foreword", "preface": "preface", "notes": "endnotes", "loi": "loi", "lot": "lot"}.get(t, t or "bodymatter")


def _read_metadata(meta_el) -> Metadata:
    md = Metadata()
    if meta_el is None:
        return md
    # EPUB 2 sometimes nests dc-metadata
    els = [e for e in meta_el.iter() if e is not meta_el]
    refines: dict[str, dict[str, str]] = {}
    for e in els:
        if _local(e.tag) == "meta" and e.get("refines"):
            ref = refines.setdefault(e.get("refines").lstrip("#"), {})
            prop = e.get("property", "")
            value = (e.text or "").strip()
            if prop == "role":
                ref["roles"] = ref.get("roles", []) + [value.lower()]
            ref.setdefault(prop, value)
    titles = []
    for e in els:
        name = _local(e.tag)
        text = " ".join((e.text or "").split()) if name != "description" else (e.text or "").strip()
        rid = e.get("id", "")
        ref = refines.get(rid, {})
        if name == "title" and text:
            seq = re.match(r"\s*\d+", ref.get("display-seq", ""))  # "1.0" and the like are seen too
            titles.append((ref.get("title-type", "main"), int(seq[0]) if seq else 0, text))
            if ref.get("file-as"):
                md.title_sort = ref["file-as"]
            if e.get("file-as") or e.get("opf:file-as"):
                md.title_sort = e.get("file-as") or e.get("opf:file-as")
        elif name in ("creator", "contributor") and text:
            roles = [r.lower() for r in (e.get("role"), e.get("opf:role")) if r] or ref.get("roles", [])
            if name == "creator" and (not roles or "aut" in roles):
                md.authors.append(text)
                fa = e.get("file-as") or e.get("opf:file-as") or ref.get("file-as")
                if fa and not md.author_sort:
                    md.author_sort = fa
            else:
                md.contributors.append(text)
        elif name == "language" and text and not md.language:
            md.language = text
        elif name == "identifier" and text:
            scheme = (e.get("scheme") or e.get("opf:scheme") or ref.get("identifier-type", "")).lower()
            value = text
            low = text.lower()
            if low.startswith("urn:uuid:"):
                scheme, value = "uuid", text[9:]
            elif low.startswith("urn:isbn:"):
                scheme, value = "isbn", text[9:]
            elif low.startswith("isbn:"):
                scheme, value = "isbn", text[5:]
            elif low.startswith(("urn:doi:", "doi:")):
                scheme, value = "doi", text.split(":", 2)[-1]
            elif low.startswith(("http://", "https://")) and not scheme:
                scheme = "url"
            elif (not scheme and (named := re.fullmatch(r"([a-z][a-z0-9-]{1,30}):(?!//)(.+)", text, re.IGNORECASE))
                    and named[1].lower() not in ("urn", "http", "https")):
                scheme, value = named[1].lower(), named[2]  # "asin:B00X": how EPUB 3 output keeps the scheme
            elif not scheme:
                scheme = "uuid" if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", low) else (
                    "isbn" if re.fullmatch(r"(97[89])?\d{9}[\dx]", low.replace("-", "")) else "id")
            if scheme not in md.identifiers:
                md.identifiers[scheme] = value
        elif name == "publisher" and text and not md.publisher:
            md.publisher = text
        elif name == "date" and text:
            ev = (e.get("event") or e.get("opf:event") or "").lower()
            if not md.date or ev in ("publication", "original-publication"):
                md.date = text
        elif name == "description" and text and not md.description:
            md.description = text
        elif name == "subject" and text:
            md.subjects.append(text)
        elif name == "rights" and text and not md.rights:
            md.rights = text
        elif name == "meta":
            prop, nm = e.get("property", ""), e.get("name", "")
            if nm == "calibre:series" and e.get("content"):
                md.series = e.get("content")
            elif nm == "calibre:series_index" and e.get("content"):
                try:
                    md.series_index = float(e.get("content"))
                except ValueError:
                    pass
            elif prop == "belongs-to-collection" and text and not e.get("refines"):
                if ref.get("collection-type", "series") == "series" and not md.series:
                    md.series = text
                    try:
                        md.series_index = float(ref.get("group-position", "")) if ref.get("group-position") else None
                    except ValueError:
                        pass
            elif nm == "calibre:title_sort" and e.get("content"):
                md.title_sort = e.get("content")
    if titles:
        mains = [t for t in titles if t[0] == "main"] or titles
        md.title = min(mains, key=lambda t: t[1])[2]
        subs = [t[2] for t in titles if t[0] == "subtitle"]
        if subs and subs[0] not in md.title:
            md.title = f"{md.title}: {subs[0]}"
    return md


def _read_nav(book: Book, nav_href: str) -> None:
    root = book.tree(nav_href)
    for nav in root.iter("nav"):
        kind = nav.get("epub:type", "") or nav.get("role", "")
        ol = next((c for c in nav.iter() if c.tag == "ol"), None)
        if ol is None:
            continue
        if "toc" in kind.split() or "doc-toc" in kind:
            book.toc = _nav_list(ol, nav_href, book)
        elif "landmarks" in kind:
            for a in ol.iter("a"):
                h = a.get("href", "")
                if h:
                    book.landmarks.append(Landmark(a.get("epub:type", "").split(" ")[0] or "bodymatter",
                                                   " ".join(markup.text_of(a).split()), _abs(nav_href, h)))
        elif "page-list" in kind or "doc-pagelist" in kind:
            for a in ol.iter("a"):
                if a.get("href"):
                    book.page_list.append((" ".join(markup.text_of(a).split()), _abs(nav_href, a.get("href"))))


def _abs(base: str, link: str) -> str:
    target = resolve(base, link)
    frag = link.split("#", 1)[1] if "#" in link else ""
    return target + (f"#{unquote(frag)}" if frag else "")


def _nav_list(ol, nav_href, book) -> list[TocEntry]:
    out = []
    for li in [c for c in ol if c.tag == "li"]:
        a = next((c for c in li if c.tag in ("a", "span")), None)
        if a is None:
            a = next((c for c in li.iter() if c.tag in ("a", "span")), None)
        title = " ".join(markup.text_of(a).split()) if a is not None else ""
        if not title and a is not None:
            img = a.find(".//img")
            title = img.get("alt", "") if img is not None else ""
        href = _abs(nav_href, a.get("href")) if a is not None and a.get("href") else ""
        sub = next((c for c in li if c.tag == "ol"), None)
        children = _nav_list(sub, nav_href, book) if sub is not None else []
        if title or children:
            out.append(TocEntry(title, href, children))
    return out


def _read_ncx(book: Book, ncx_href: str) -> None:
    root = _xml(book.resources[ncx_href].data)
    nav_map = _find(root, "navMap")

    def points(el):
        out = []
        for np in _children(el, "navPoint"):
            label = _find(np, "text")
            content = _find(np, "content")
            title = " ".join((label.text or "").split()) if label is not None else ""
            href = _abs(ncx_href, content.get("src", "")) if content is not None and content.get("src") else ""
            out.append(TocEntry(title, href, points(np)))
        return out

    if nav_map is not None:
        book.toc = points(nav_map)
    page_list = _find(root, "pageList")
    if page_list is not None and not book.page_list:
        for pt in _children(page_list, "pageTarget"):
            label = _find(pt, "text")
            content = _find(pt, "content")
            if content is not None and content.get("src"):
                book.page_list.append(((label.text or "").strip() if label is not None else pt.get("value", ""),
                                       _abs(ncx_href, content.get("src"))))


def _find_cover(book, meta_el, items):
    for iid, (href, props) in items.items():
        if "cover-image" in props.split() and book.resources[href].is_image:
            return href
    if meta_el is not None:
        for m in meta_el.iter():
            if _local(m.tag) == "meta" and m.get("name") == "cover":
                c = m.get("content", "")
                if c in items and book.resources[items[c][0]].is_image:
                    return items[c][0]
                # some books put the href instead of the id
                for iid, (href, _p) in items.items():
                    if href.endswith(c) and book.resources[href].is_image:
                        return href
    for iid, (href, _p) in items.items():
        if "cover" in iid.lower() and book.resources[href].is_image:
            return href
    # the cover page from the guide or landmarks: its only image
    for lm in book.landmarks:
        if lm.type == "cover":
            doc = lm.href.split("#")[0]
            if doc in book.resources and book.resources[doc].is_document:
                img = _only_image(book, doc)
                if img:
                    return img
            elif doc in book.resources and book.resources[doc].is_image:
                return doc
    return None


def _only_image(book, doc):
    root = book.tree(doc)
    for el in root.iter():
        src = el.get("src") if el.tag == "img" else (el.get("xlink:href") or el.get("href")) if el.tag == "image" else None
        if src:
            target = resolve(doc, src)
            if target in book.resources and book.resources[target].is_image:
                return target
    return None


# --- writing -----------------------------------------------------------------------------------

def write(book: Book, path_or_file, version: int = 3, cover_page: bool = True, **_opts) -> None:
    """Write ``book`` as an EPUB. ``version`` is 3 (default, with an NCX too) or 2."""
    if str(version).strip() not in ("2", "3"):
        raise BookportError(f"EPUB version {version!r}: use 2 or 3")
    version = int(str(version).strip())
    from .. import clean, links
    book = prepare(book, cover_page=cover_page)
    clean.clean_book(book)
    links.rename(book, links.safe_names(book, reserved=("mimetype", "META-INF", "content.opf", "toc.ncx",
                                                         "nav.xhtml")))
    from ..toc import fix_links
    fix_links(book)
    if not book.toc:
        book.ensure_toc()
    md = book.metadata
    uid = md.uid()
    modified = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = _manifest_ids(book)
    nav_href = "nav.xhtml" if "nav.xhtml" not in book.resources else book.unique_href("nav.xhtml")
    ncx_href = "toc.ncx" if "toc.ncx" not in book.resources else book.unique_href("toc.ncx")
    # straight to the file: building it in memory first held the whole book twice
    with zipfile.ZipFile(path_or_file, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(zipfile.ZipInfo("mimetype", date_time=(2020, 1, 1, 0, 0, 0)), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        props = {}
        for href, res in book.resources.items():
            if res.is_document:
                props[href] = _doc_properties(res.data)
        if version >= 3:
            z.writestr(f"OEBPS/{nav_href}", nav_document(book, nav_href))
        z.writestr(f"OEBPS/{ncx_href}", ncx_document(book, uid, ncx_href))
        z.writestr("OEBPS/content.opf", opf_document(book, version, uid, modified, ids, props, nav_href, ncx_href))
        for href, res in book.resources.items():
            compress = zipfile.ZIP_STORED if res.media_type in ("image/jpeg", "image/png", "image/gif") else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(f"OEBPS/{href}", date_time=(2020, 1, 1, 0, 0, 0)), res.data, compress_type=compress)


CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def prepare(book: Book, cover_page: bool = True) -> Book:
    """A copy of the book ready for packaging: a cover page, a non-empty spine, a title."""
    b = book.copy()
    # landmarks and the guide may only point at content documents (LIT guides point at cover images)
    b.landmarks = [lm for lm in b.landmarks if (r := b.resources.get(lm.href.split("#")[0])) is not None and r.is_document]
    if not b.metadata.title:
        b.metadata.title = "Untitled"
    if not b.metadata.language:
        b.metadata.language = "en"
    if not b.spine:
        root, body = markup.new_document(b.metadata.title, b.metadata.language)
        ET.SubElement(body, "h1").text = b.metadata.title
        b.add_document("text/title.xhtml", root)
    if cover_page and b.cover and not _starts_with_cover(b):
        href = b.unique_href("cover.xhtml")
        root = cover_document(b, href)
        b.add(href, markup.serialize(root), XHTML)
        b.spine.insert(0, href)
        b.landmarks = [lm for lm in b.landmarks if lm.type != "cover"]
        b.landmarks.insert(0, Landmark("cover", "Cover", href))
    return b


def _starts_with_cover(book: Book) -> bool:
    first = book.spine[0]
    if any(lm.type == "cover" and lm.href.split("#")[0] == first for lm in book.landmarks):
        return True
    root = book.tree(first)
    words = len(markup.text_of(root.find("body")).split()) if root.find("body") is not None else 0
    return words < 10 and _only_image(book, first) == book.cover


def cover_document(book: Book, href: str) -> ET.Element:
    cover = book.resources[book.cover]
    w, h = image_size(cover.data) or (600, 800)
    root, body = markup.new_document("Cover", book.metadata.language)
    style = ET.SubElement(root.find("head"), "style")
    style.text = "html, body { margin: 0; padding: 0; height: 100%; text-align: center } svg { height: 100%; width: 100% }"
    body.set("epub:type", "cover")
    div = ET.SubElement(body, "div")
    svg = ET.SubElement(div, "svg", {"version": "1.1", "width": "100%", "height": "100%",
                                     "viewBox": f"0 0 {w} {h}", "preserveAspectRatio": "xMidYMid meet"})
    ET.SubElement(svg, "image", {"width": str(w), "height": str(h), "xlink:href": relative(href, book.cover)})
    return root


def image_size(data: bytes):
    """(width, height) of a JPEG, PNG, GIF, WebP or BMP, without decoding it."""
    import struct
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return w, abs(h)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + length
        if data.lstrip()[:5] in (b"<svg ", b"<?xml") or b"<svg" in data[:1024]:
            m = re.search(rb'viewBox="\s*[-\d.]+[ ,]+[-\d.]+[ ,]+([\d.]+)[ ,]+([\d.]+)', data[:4096])
            if m:
                return int(float(m.group(1))), int(float(m.group(2)))
    except (struct.error, IndexError):
        return None
    return None


def _doc_properties(data: bytes) -> set:
    props = set()
    if b"<svg" in data:
        props.add("svg")
    if b"<math" in data:
        props.add("mathml")
    if b"<script" in data:
        props.add("scripted")
    return props


def _manifest_ids(book: Book) -> dict:
    ids = {}
    used = set()
    for href in book.resources:
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", posixpath.basename(href))
        if not base or not (base[0].isalpha() or base[0] == "_"):
            base = "id_" + base
        i, n = base, 1
        while i in used or i in ("nav", "ncx", "uid"):
            i = f"{base}_{n}"
            n += 1
        used.add(i)
        ids[href] = i
    return ids


def _esc(s: str) -> str:
    return markup._esc_attr(markup.clean_text(s))


def _w3cdtf(date: str) -> str | None:
    d = date.strip()
    m = re.match(r"^(\d{4})(-\d{2})?(-\d{2})?", d)
    if not m:
        return None
    full = re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$", d)
    if full:
        return d
    year, mon, day = m.group(1), m.group(2) or "", m.group(3) or ""
    if year == "0000" or year < "0100":
        return None
    if mon and not (1 <= int(mon[1:]) <= 12):
        mon = day = ""
    if day and not (1 <= int(day[1:]) <= 31):
        day = ""
    return year + mon + (day if mon else "")


def opf_document(book: Book, version, uid, modified, ids, props, nav_href, ncx_href) -> bytes:
    md = book.metadata
    v3 = version >= 3
    out = ['<?xml version="1.0" encoding="utf-8"?>\n']
    pkg_attrs = f'xmlns="{OPF_NS}" version="{"3.0" if v3 else "2.0"}" unique-identifier="uid"'
    if v3:
        pkg_attrs += f' xml:lang="{_esc(md.language)}"'
        if book.fixed_layout:
            pkg_attrs += ' prefix="rendition: http://www.idpf.org/vocab/rendition/#"'
    out.append(f"<package {pkg_attrs}>\n")
    out.append(f'  <metadata xmlns:dc="{DC_NS}" xmlns:opf="{OPF_NS}">\n')
    scheme_uid = next((k for k, v in md.identifiers.items() if v == uid), "uuid")
    uid_text = f"urn:uuid:{uid}" if scheme_uid == "uuid" and not uid.startswith("urn:") else (
        f"urn:isbn:{uid}" if scheme_uid == "isbn" else uid)
    out.append(f'    <dc:identifier id="uid">{_esc(uid_text)}</dc:identifier>\n')
    for scheme, value in md.identifiers.items():
        if value == uid:
            continue
        if v3:
            text = f"urn:isbn:{value}" if scheme == "isbn" else (f"urn:uuid:{value}" if scheme == "uuid" else (
                value if scheme in ("url", "id") or value.lower().startswith(("urn:", "doi:")) else f"{scheme}:{value}"))
            out.append(f"    <dc:identifier>{_esc(text)}</dc:identifier>\n")
        else:
            out.append(f'    <dc:identifier opf:scheme="{_esc(scheme)}">{_esc(value)}</dc:identifier>\n')
    out.append(f"    <dc:title id=\"title\">{_esc(md.title)}</dc:title>\n")
    if v3 and md.title_sort:
        out.append(f'    <meta refines="#title" property="file-as">{_esc(md.title_sort)}</meta>\n')
    out.append(f"    <dc:language>{_esc(md.language)}</dc:language>\n")
    for i, a in enumerate(md.authors):
        if v3:
            out.append(f'    <dc:creator id="creator{i}">{_esc(a)}</dc:creator>\n')
            out.append(f'    <meta refines="#creator{i}" property="role" scheme="marc:relators">aut</meta>\n')
            if i == 0 and md.author_sort:
                out.append(f'    <meta refines="#creator{i}" property="file-as">{_esc(md.author_sort)}</meta>\n')
        else:
            fa = f' opf:file-as="{_esc(md.author_sort)}"' if i == 0 and md.author_sort else ""
            out.append(f'    <dc:creator opf:role="aut"{fa}>{_esc(a)}</dc:creator>\n')
    for c in md.contributors:
        out.append(f"    <dc:contributor>{_esc(c)}</dc:contributor>\n")
    if md.publisher:
        out.append(f"    <dc:publisher>{_esc(md.publisher)}</dc:publisher>\n")
    date = _w3cdtf(md.date) if md.date else None
    if date:
        out.append(f"    <dc:date>{_esc(date)}</dc:date>\n")
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
        if v3:
            out.append(f'    <meta property="belongs-to-collection" id="series">{_esc(md.series)}</meta>\n')
            out.append('    <meta refines="#series" property="collection-type">series</meta>\n')
            if md.series_index is not None:
                out.append(f'    <meta refines="#series" property="group-position">{md.series_index:g}</meta>\n')
    if book.cover:
        out.append(f'    <meta name="cover" content="{ids[book.cover]}"/>\n')
    if v3:
        out.append(f'    <meta property="dcterms:modified">{modified}</meta>\n')
        if book.fixed_layout:
            out.append('    <meta property="rendition:layout">pre-paginated</meta>\n')
            out.append('    <meta property="rendition:spread">none</meta>\n')
    out.append("  </metadata>\n  <manifest>\n")
    if v3:
        out.append(f'    <item id="nav" href="{quote(nav_href)}" media-type="application/xhtml+xml" properties="nav"/>\n')
    out.append(f'    <item id="ncx" href="{quote(ncx_href)}" media-type="{NCX}"/>\n')
    for href, res in book.resources.items():
        p = set()
        if v3:
            p |= props.get(href, set())
            if href == book.cover:
                p.add("cover-image")
            p |= {x for x in res.properties.split() if x in ("svg", "mathml", "scripted", "remote-resources", "switch")}
        attr = f' properties="{" ".join(sorted(p))}"' if p else ""
        mt = res.media_type
        if mt == "text/html":
            mt = XHTML
        out.append(f'    <item id="{ids[href]}" href="{_esc(quote(href))}" media-type="{_esc(mt)}"{attr}/>\n')
    ppd = f' page-progression-direction="{book.direction}"' if book.direction in ("rtl", "ltr") and v3 else ""
    out.append(f'  </manifest>\n  <spine toc="ncx"{ppd}>\n')
    for href in book.spine:
        if href in ids:
            out.append(f'    <itemref idref="{ids[href]}"/>\n')
    out.append("  </spine>\n")
    if book.landmarks or not v3:
        out.append("  <guide>\n")
        guide_types = {"bodymatter": "text", "titlepage": "title-page", "endnotes": "notes", "acknowledgments": "acknowledgements"}
        seen = set()
        for lm in book.landmarks:
            if lm.href.split("#")[0] in book.resources and lm.type not in seen:
                seen.add(lm.type)
                t = guide_types.get(lm.type, lm.type)
                out.append(f'    <reference type="{_esc(t)}" title="{_esc(lm.title or t)}" href="{_esc(_q(lm.href))}"/>\n')
        if not v3 and not book.landmarks and book.spine:
            out.append(f'    <reference type="text" title="Start" href="{_esc(_q(book.spine[0]))}"/>\n')
        out.append("  </guide>\n")
    out.append("</package>\n")
    text = "".join(out)
    if text.count("<guide>\n  </guide>"):
        text = text.replace("  <guide>\n  </guide>\n", "")
    return text.encode("utf-8")


def _q(href: str) -> str:
    doc, _, frag = href.partition("#")
    return quote(doc) + (f"#{quote(frag)}" if frag else "")


def nav_document(book: Book, nav_href: str) -> bytes:
    md = book.metadata
    root = ET.Element("html", {"lang": md.language, "xml:lang": md.language})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "meta", {"charset": "utf-8"})
    ET.SubElement(head, "title").text = md.title or "Contents"
    ET.SubElement(head, "style").text = "nav#landmarks, nav#page-list { display: none } ol { list-style-type: none }"
    body = ET.SubElement(root, "body")
    nav = ET.SubElement(body, "nav", {"epub:type": "toc", "id": "toc", "role": "doc-toc"})
    ET.SubElement(nav, "h1").text = "Contents"

    def add(parent, entries):
        ol = ET.SubElement(parent, "ol")
        for e in entries:
            li = ET.SubElement(ol, "li")
            title = " ".join((e.title or "").split()) or "Untitled"
            if e.href:
                a = ET.SubElement(li, "a", {"href": relative(nav_href, e.href)})
            else:
                a = ET.SubElement(li, "span")
            a.text = title
            if e.children:
                add(li, e.children)
        return ol

    entries = book.toc or [TocEntry(md.title or "Start", book.spine[0])]
    add(nav, entries)
    lms = [lm for lm in book.landmarks if lm.href.split("#")[0] in book.resources]
    if book.spine and not any(lm.type == "bodymatter" for lm in lms):
        start = next((h for h in book.spine if not any(lm.href.split("#")[0] == h and lm.type == "cover" for lm in lms)),
                     book.spine[0])
        lms.append(Landmark("bodymatter", "Start", start))
    if lms:
        nav2 = ET.SubElement(body, "nav", {"epub:type": "landmarks", "id": "landmarks", "hidden": "hidden"})
        ET.SubElement(nav2, "h2").text = "Landmarks"
        ol = ET.SubElement(nav2, "ol")
        seen = set()
        for lm in lms:
            key = (lm.type, lm.href)
            if key in seen:
                continue
            seen.add(key)
            li = ET.SubElement(ol, "li")
            a = ET.SubElement(li, "a", {"epub:type": lm.type, "href": relative(nav_href, lm.href)})
            a.text = lm.title or lm.type.title()
    pages = [(label, h) for label, h in book.page_list if h.split("#")[0] in book.resources]
    if pages:
        nav3 = ET.SubElement(body, "nav", {"epub:type": "page-list", "id": "page-list", "hidden": "hidden"})
        ol = ET.SubElement(nav3, "ol")
        for label, h in pages:
            li = ET.SubElement(ol, "li")
            ET.SubElement(li, "a", {"href": relative(nav_href, h)}).text = label or "?"
    return markup.serialize(root)


def ncx_document(book: Book, uid: str, ncx_href: str) -> bytes:
    md = book.metadata
    flat = [x for e in book.toc for x in e.walk()]
    depth = max((d for d, _ in flat), default=0) + 1
    out = ['<?xml version="1.0" encoding="utf-8"?>\n',
           f'<ncx xmlns="{NCX_NS}" version="2005-1" xml:lang="{_esc(md.language)}">\n  <head>\n',
           f'    <meta name="dtb:uid" content="{_esc(_ncx_uid(book, uid))}"/>\n',
           f'    <meta name="dtb:depth" content="{depth}"/>\n',
           '    <meta name="dtb:totalPageCount" content="0"/>\n    <meta name="dtb:maxPageNumber" content="0"/>\n',
           f"  </head>\n  <docTitle><text>{_esc(md.title)}</text></docTitle>\n"]
    for a in md.authors[:1]:
        out.append(f"  <docAuthor><text>{_esc(a)}</text></docAuthor>\n")
    out.append("  <navMap>\n")
    counter = [0]
    orders: dict[str, int] = {}  # entries with the same target must share a playOrder

    def order(href):
        if href not in orders:
            orders[href] = len(orders) + 1
        return orders[href]

    entries = book.toc or [TocEntry(md.title or "Start", book.spine[0])]

    def point(e, indent):
        counter[0] += 1
        n = counter[0]
        href = e.href or (e.children[0].href if e.children else book.spine[0])
        pad = "  " * indent
        out.append(f'{pad}<navPoint id="np{n}" playOrder="{order(href)}">\n{pad}  <navLabel><text>{_esc(" ".join((e.title or "Untitled").split()) or "Untitled")}</text></navLabel>\n')
        out.append(f'{pad}  <content src="{_esc(relative(ncx_href, href))}"/>\n')
        for c in e.children:
            point(c, indent + 1)
        out.append(f"{pad}</navPoint>\n")

    for e in entries:
        point(e, 2)
    out.append("  </navMap>\n")
    pages = [(label, h) for label, h in book.page_list if h.split("#")[0] in book.resources]
    if pages:
        out.append("  <pageList>\n    <navLabel><text>Pages</text></navLabel>\n")
        for i, (label, h) in enumerate(pages, 1):
            value = f' value="{label}"' if label.isdigit() else ""
            ptype = "normal" if label.isdigit() else "front"
            out.append(f'    <pageTarget id="p{i}" type="{ptype}"{value} playOrder="{order(h)}"><navLabel><text>'
                       f'{_esc(label or str(i))}</text></navLabel><content src="{_esc(relative(ncx_href, h))}"/></pageTarget>\n')
        out.append("  </pageList>\n")
    out.append("</ncx>\n")
    return "".join(out).encode("utf-8")


def _ncx_uid(book, uid):
    scheme = next((k for k, v in book.metadata.identifiers.items() if v == uid), "uuid")
    if scheme == "uuid" and not uid.startswith("urn:"):
        return f"urn:uuid:{uid}"
    if scheme == "isbn":
        return f"urn:isbn:{uid}"
    return uid

