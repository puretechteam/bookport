"""PostScript and EPS to PDF: unwrap the file (a DOS EPS header, a printer job's PJL), read its DSC header for the
bounding box and title, run it, and write each showpage as a PDF page. An EPS page is its bounding box."""

from __future__ import annotations

import re
import struct

from ..errors import BookportError
from . import ops_core, ops_graphics, ops_text
from .device import Content, Document
from .files import PSFile, Stream
from .gstate import IDENTITY, GState
from .interp import FileRun, Interp, JobError, Quit
from .objects import Name, PSDict

LETTER = (612.0, 792.0)
_BBOX = re.compile(rb"^%%(Hi[Rr]es)?BoundingBox:[ \t]*(\(atend\)|[-+0-9. \t]+)", re.MULTILINE)
_MEDIA = re.compile(rb"^%%DocumentMedia:[ \t]*\S+[ \t]+([0-9.]+)[ \t]+([0-9.]+)", re.MULTILINE)
_TITLE = re.compile(rb"^%%Title:[ \t]*(.*?)[ \t]*\r?$", re.MULTILINE)
_CREATOR = re.compile(rb"^%%Creator:[ \t]*(.*?)[ \t]*\r?$", re.MULTILINE)


def unwrap(data: bytes) -> bytes:
    """The PostScript in a file: inside a DOS EPS binary header, or after a PJL/Ctrl-D prefix."""
    if data[:4] == b"\xc5\xd0\xd3\xc6" and len(data) >= 30:
        start, length = struct.unpack("<II", data[4:12])
        body = data[start:start + length]
        if not body:
            raise BookportError("This EPS file's PostScript part is empty")
        return body
    i = data.find(b"%!")
    if 0 < i < 4096:
        return data[i:]
    return data


def dsc(data: bytes) -> dict:
    """What the DSC comments say: EPS or not, the bounding box, the paper and the title."""
    head = data[:65536]
    out = {"eps": bool(re.match(rb"%!PS-Adobe-\d\.\d\s+EPSF", head)), "bbox": None, "media": None,
           "title": None}
    boxes = {}
    for m in _BBOX.finditer(head):
        val = m.group(2)
        if val == b"(atend)":
            tail = data[-65536:]
            found = [t for t in _BBOX.finditer(tail) if t.group(1) == m.group(1) and t.group(2) != b"(atend)"]
            if not found:
                continue
            val = found[-1].group(2)
        try:
            nums = [float(v) for v in val.split()[:4]]
        except ValueError:
            continue
        if len(nums) == 4 and (m.group(1) or b"") not in boxes:
            boxes[m.group(1) or b""] = nums
    box = next((boxes[k] for k in boxes if k), None) or boxes.get(b"")
    if box and box[2] > box[0] and box[3] > box[1]:
        out["bbox"] = box
    m = _MEDIA.search(head)
    if m:
        try:
            w, h = float(m.group(1)), float(m.group(2))
            if w > 0 and h > 0:
                out["media"] = (w, h)
        except ValueError:
            pass
    m = _TITLE.search(head)
    if m:
        t = m.group(1).strip()
        if t.startswith(b"(") and t.endswith(b")"):
            t = t[1:-1]
        out["title"] = t.decode("latin-1", "replace").strip() or None
    return out


class _Job(Interp):
    def emit_page(self, keep: bool = False) -> None:
        self.page_count += 1
        size = self.page_size
        if keep:
            copy = Content(self.doc)
            copy.ops = list(self.page.ops)
            copy.res = {k: dict(v) for k, v in self.page.res.items()}
            copy.levels = list(self.page.levels)
            self.doc.add_page(copy, size)
        else:
            self.doc.add_page(self.page, size)


def make(eps: bool, box, media, doc: Document | None = None) -> _Job:
    I = _Job()
    I.doc = doc if doc is not None else Document()
    I.eps = eps
    I.page_count = 0
    if eps and box:
        x0, y0, x1, y1 = box
        I.page_size = (x1 - x0, y1 - y0)
        I.default_ctm = (1.0, 0.0, 0.0, 1.0, -x0, -y0)
    else:
        I.page_size = media or LETTER
        I.default_ctm = IDENTITY
    I.pagedevice = PSDict({Name("PageSize"): None}, name="pagedevice")
    I.gs = GState(I.default_ctm)
    I.gstack = []
    I.page = Content(I.doc)
    I.gs.dev = I.page
    ops_core.register(I)
    ops_graphics.register(I)
    ops_text.register(I)
    _pdfmark(I)
    return I


def _pdfmark(I: Interp) -> None:
    """pdfmark: the document's title and author kept; marks PDF output doesn't need are dropped."""
    from .interp import count_to_mark
    from .objects import PSString

    def pdfmark(I):
        kind = I.os.pop()
        n = count_to_mark(I.os)
        vals = I.os[len(I.os) - n:]
        del I.os[len(I.os) - n - 1:]
        if str(kind) == "DOCINFO":
            for i in range(0, len(vals) - 1, 2):
                k, v = str(vals[i]), vals[i + 1]
                if k in ("Title", "Author", "Subject", "Keywords", "Creator") and type(v) is PSString:
                    I.doc.info[k] = _text(v.bytes())
    I.define("pdfmark", pdfmark)


def _text(raw: bytes) -> str:
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be", "replace")
    return raw.decode("latin-1", "replace")


def convert(data: bytes, eps: bool | None = None, notes: list[str] | None = None) -> bytes:
    I = run(data, eps, Document())
    pdf = I.doc.finish()
    if notes is not None:
        notes.extend(I.doc.notes)
    return pdf


def run(data: bytes, eps: bool | None, doc: Document) -> _Job:
    """Run the program, its pages going to ``doc`` (a PDF, or what the SVG output records)."""
    data = unwrap(data)
    if not data.lstrip()[:2] == b"%!" and b"showpage" not in data[:1_000_000] and b"moveto" not in data[:1_000_000]:
        raise BookportError("This isn't a PostScript file")
    info = dsc(data)
    is_eps = info["eps"] if eps is None else eps
    if is_eps and info["bbox"] is None:
        is_eps = False
    I = make(is_eps, info["bbox"], info["media"], doc)
    if info["title"]:
        I.doc.info["Title"] = info["title"]
    main = PSFile(Stream(data, "%main"), True)
    I.es.append(FileRun(main))
    error = None
    try:
        I.run(0)
    except Quit:
        pass
    except JobError as e:
        error = e
    if I.page.marks or (not I.doc.pages and not error):
        I.emit_page()
    if error is not None:
        if not any(_has_marks(I)):
            raise error
        I.doc.note(f"The PostScript program stopped early with an error ({error.name} in {error.command}); "
                   "the pages it drew are kept")
    return I


def _has_marks(I):
    yield bool(I.doc.pages)
