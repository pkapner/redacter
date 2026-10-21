"""Open, redact, sanitize, save, and verify PDFs."""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .config import Config
from .detect import Match, find_matches, page_text, value_regex

FILL = (0, 0, 0)


class RedactError(Exception):
    pass


def open_pdf(path: Path, password: str | None = None) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise RedactError(f"cannot open {path}: {e}")
    if not doc.is_pdf:
        raise RedactError(f"{path} is not a PDF")
    if doc.needs_pass:
        if password is None:
            password = getpass.getpass(f"Password for {path.name}: ")
        if not doc.authenticate(password):
            raise RedactError(f"wrong password for {path}")
    # Flatten form fields and annotations into page content. Filled-in values
    # of fillable forms live in widgets, not page text: without this they are
    # invisible to detection and would survive redaction.
    doc.bake(annots=True, widgets=True)
    return doc


def redact_document(
    doc: pymupdf.Document,
    matches: list[Match],
    cfg: Config,
    skip: set[int],
) -> list[Match]:
    """Black out matches (minus skipped IDs) and manual boxes, then sanitize.

    Returns the matches that were redacted.
    """
    applied = [m for m in matches if m.id not in skip]
    covers: dict[int, list[pymupdf.Rect]] = {}
    for m in applied:
        page = doc[m.page]
        for r in m.cut_rects:
            page.add_redact_annot(r, fill=FILL, cross_out=False)
        covers.setdefault(m.page, []).extend(m.rects)

    for box in cfg.boxes:
        if box.page > doc.page_count:
            raise RedactError(
                f"box on page {box.page}, but the PDF has {doc.page_count} pages"
            )
        doc[box.page - 1].add_redact_annot(pymupdf.Rect(box.rect), fill=FILL, cross_out=False)

    for page in doc:
        # Remove text under the boxes, blank out covered image pixels, and
        # drop vector graphics fully inside a box.
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
        for r in covers.get(page.number, []):
            page.draw_rect(r, color=None, fill=FILL, overlay=True)

    sanitize(doc)
    return applied


def sanitize(doc: pymupdf.Document) -> None:
    """Strip places outside page content where PII hides."""
    doc.scrub(
        attached_files=True,
        clean_pages=True,
        embedded_files=True,
        hidden_text=True,
        javascript=True,
        metadata=True,
        redactions=True,
        remove_links=True,
        reset_fields=True,
        reset_responses=True,
        thumbnails=True,
        xml_metadata=True,
    )
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.set_toc([])  # bookmarks can carry names
    cat = doc.pdf_catalog()
    for key in ("AcroForm", "Outlines", "PieceInfo", "OpenAction", "AA"):
        doc.xref_set_key(cat, key, "null")
    for page in doc:
        doc.xref_set_key(page.xref, "PieceInfo", "null")


def save(doc: pymupdf.Document, out: Path) -> None:
    # Full rewrite (never incremental) with unused objects garbage-collected,
    # so the original text does not survive in an older revision of the file.
    tmp = out.with_name(out.name + ".tmp")
    doc.save(tmp, garbage=4, deflate=True, clean=True, incremental=False)
    os.replace(tmp, out)


@dataclass
class Verification:
    leaks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.leaks


def verify(out: Path, cfg: Config, applied: list[Match], skipped: list[Match]) -> Verification:
    """Re-open the output and check nothing that should be gone is still there."""
    result = Verification()
    doc = pymupdf.open(out)
    skipped_texts = {_compact(m.text) for m in skipped}

    leaks: dict[str, None] = {}  # ordered set: one line per distinct leak

    # 1. Detection finds nothing new (other than matches the user kept).
    for m in find_matches(doc, cfg):
        if _compact(m.text) not in skipped_texts:
            leaks[f"page {m.page + 1}: {m.kind} still detected: {m.text!r}"] = None

    # 2. No redacted value survives anywhere in the extracted text, in any
    #    formatting (same digit boundaries as detection, so 10075 is not
    #    reported inside an amount like 1,007,500).
    pages = [page_text(p).text for p in doc]
    for m in applied:
        if _compact(m.text) in skipped_texts or not (rx := value_regex(m.text)):
            continue
        for pno, text in enumerate(pages):
            if hit := rx.search(text):
                ctx = re.sub(r"\s+", " ", text[max(0, hit.start() - 25):hit.end() + 25])
                leaks[f"page {pno + 1}: redacted value {m.text!r} still present: ...{ctx}..."] = None

    # 3. No redacted string survives in the raw, decompressed file bytes
    #    (metadata, form remnants, stray objects). Numbers under 9 digits are
    #    skipped: they turn up by chance in the file's own structure.
    raw = doc.tobytes(garbage=0, expand=255).lower()
    for m in applied:
        if _compact(m.text) in skipped_texts or len(m.text.strip()) < 4:
            continue
        digits = re.sub(r"\D", "", m.text)
        if re.fullmatch(r"[\d\s\-()]+", m.text) and len(digits) < 9:
            continue
        for needle in _raw_needles(m.text):
            if needle in raw:
                leaks[f"raw file bytes still contain {m.text!r}"] = None
                break

    result.leaks = list(leaks)
    doc.close()
    return result


def _compact(s: str) -> str:
    return re.sub(r"[\s\-.,()]", "", s).casefold()


def _raw_needles(text: str) -> list[bytes]:
    variants = {text.strip(), " ".join(text.split())}
    needles = []
    for v in variants:
        v = v.lower()
        needles.append(v.encode("latin-1", errors="ignore"))
        needles.append(v.encode("utf-16-be"))
    return [n for n in needles if len(n) >= 4]


def render_preview(
    doc: pymupdf.Document,
    out_dir: Path,
    matches: list[Match] | None = None,
    skip: set[int] = frozenset(),
    zoom: float = 2.0,
) -> list[Path]:
    """Render each page to PNG. With matches, outline them (red) with IDs.

    Skipped matches are outlined in green so they are easy to tell apart.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    paths = []
    mat = pymupdf.Matrix(zoom, zoom)
    by_page: dict[int, list[Match]] = {}
    for m in matches or []:
        by_page.setdefault(m.page, []).append(m)

    for pno, page in enumerate(doc):
        shapes = []
        for m in by_page.get(pno, []):
            color = (0, 0.6, 0) if m.id in skip else (1, 0, 0)
            for r in m.rects:
                annot = page.add_rect_annot(r)
                annot.set_colors(stroke=color)
                annot.set_border(width=1)
                annot.update()
                shapes.append(annot)
            label = m.rects[0]
            annot = page.add_freetext_annot(
                pymupdf.Rect(label.x0, label.y0 - 9, label.x0 + 30, label.y0),
                f"#{m.id}", fontsize=7, text_color=color, fill_color=None,
            )
            shapes.append(annot)
        pix = page.get_pixmap(matrix=mat, annots=True)
        path = out_dir / f"page-{pno + 1:03d}.png"
        pix.save(path)
        os.chmod(path, 0o600)
        paths.append(path)
        for annot in shapes:
            page.delete_annot(annot)
    return paths
