"""Open, redact, sanitize, save, and verify PDFs."""

from __future__ import annotations

import getpass
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .config import Box, Config
from .detect import Match, Progress, find_matches, page_text, value_regex

FILL = (0, 0, 0)


class RedactError(Exception):
    pass


class PasswordNeeded(RedactError):
    """The PDF is encrypted and no password (or a wrong one) was given."""


def open_pdf(path: Path, password: str | None = None,
             prompt: bool = True) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise RedactError(f"cannot open {path}: {e}")
    if not doc.is_pdf:
        raise RedactError(f"{path} is not a PDF")
    if doc.needs_pass:
        if password is None:
            if not prompt:
                raise PasswordNeeded(f"{path.name} is password protected")
            password = getpass.getpass(f"Password for {path.name}: ")
        if not doc.authenticate(password):
            raise PasswordNeeded(f"wrong password for {path.name}")
    _drop_broken_objects(doc)
    # Flatten form fields and annotations into page content. Filled-in values
    # of fillable forms live in widgets, not page text: without this they are
    # invisible to detection and would survive redaction.
    doc.bake(annots=True, widgets=True)
    return doc


def _drop_broken_objects(doc: pymupdf.Document) -> None:
    """Replace objects the file lists but MuPDF can't load with empty ones.

    Real-world PDFs often have such entries ("cannot find object in xref").
    Viewers ignore them, but anything that walks every object (including
    PyMuPDF's scrub) stops with an error. They hold nothing readable, so
    emptying them changes nothing visible.
    """
    for xref in range(1, doc.xref_length()):
        try:
            doc.xref_object(xref)
        except Exception:
            doc.update_object(xref, "<<>>")  # "null" doesn't replace a broken entry


def redact_document(
    doc: pymupdf.Document,
    matches: list[Match],
    cfg: Config,
    skip: set[int],
    boxes: list[Box] = (),
    progress: Progress | None = None,
) -> list[Match]:
    """Black out matches (minus skipped IDs) and manual boxes, then sanitize.

    Manual boxes are the config's plus any given in boxes. Returns the
    matches that were redacted.
    """
    applied = [m for m in matches if m.id not in skip]
    cuts: dict[int, set[tuple]] = {}
    covers: dict[int, set[tuple]] = {}
    for m in applied:
        cuts.setdefault(m.page, set()).update(tuple(r) for r in m.cut_rects)
        covers.setdefault(m.page, set()).update(tuple(r) for r in m.rects)
    for pno, rects in cuts.items():
        page = doc[pno]
        for r in sorted(rects):
            page.add_redact_annot(pymupdf.Rect(r), fill=FILL, cross_out=False)

    for box in [*cfg.boxes, *boxes]:
        if box.page > doc.page_count:
            raise RedactError(
                f"box on page {box.page}, but the PDF has {doc.page_count} pages"
            )
        doc[box.page - 1].add_redact_annot(pymupdf.Rect(box.rect), fill=FILL, cross_out=False)

    for page in doc:
        if progress:
            progress("Redacting pages", page.number, doc.page_count)
        # Remove text under the boxes, blank out covered image pixels, and
        # drop vector graphics fully inside a box.
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
        rects = covers.get(page.number)
        if rects:  # all black boxes of a page in one drawing
            shape = page.new_shape()
            for r in sorted(rects):
                shape.draw_rect(pymupdf.Rect(r))
            shape.finish(color=None, fill=FILL, width=0)
            shape.commit(overlay=True)

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
    for key in CATALOG_STRIP:
        doc.xref_set_key(cat, key, "null")
    doc.xref_set_key(-1, "Info", "null")
    for xref in readable_xrefs(doc):
        if not doc.xref_is_stream(xref) and doc.xref_object(xref).lstrip()[:2] != "<<":
            continue
        # Per-object metadata: XMP on pages, images, fonts; app private data.
        for key in OBJECT_STRIP:
            if doc.xref_get_key(xref, key)[0] != "null":
                doc.xref_set_key(xref, key, "null")
        if doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode":
            raw = doc.xref_stream_raw(xref)
            clean = strip_jpeg_metadata(raw)
            if clean != raw:
                doc.update_stream(xref, clean, compress=False)
                doc.xref_set_key(xref, "Filter", "/DCTDecode")
    # A fresh file ID: the original's ID would link this copy to the
    # unredacted file it came from.
    doc.xref_set_key(-1, "ID", f"[<{secrets.token_hex(16)}><{secrets.token_hex(16)}>]")


def readable_xrefs(doc: pymupdf.Document):
    """Object numbers MuPDF can actually load.

    Real-world PDFs often list objects in their index that are missing or
    corrupt ("cannot find object in xref"). Those can't be read, and since
    the output is rewritten from readable objects only, they never reach it.
    """
    for xref in range(1, doc.xref_length()):
        try:
            doc.xref_object(xref)
        except Exception:
            continue
        yield xref


# Catalog entries that can hold names or text outside the page content.
CATALOG_STRIP = (
    "AcroForm", "Outlines", "PieceInfo", "OpenAction", "AA", "Info", "Metadata",
    "StructTreeRoot", "MarkInfo", "PageLabels", "Names", "Dests", "Threads",
    "SpiderInfo", "Collection", "Legal", "Perms", "URI",
)
OBJECT_STRIP = ("Metadata", "PieceInfo", "LastModified")


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Drop EXIF/XMP (APP1), comments and other APPn blocks from a JPEG.

    Keeps what decoding needs: JFIF (APP0), the ICC colour profile (APP2
    "ICC_PROFILE") and Adobe colour info (APP14). Returns data unchanged if
    it doesn't parse as a JPEG.
    """
    if data[:2] != b"\xff\xd8":
        return data
    out = bytearray(data[:2])
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return data
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xDA:  # start of scan: the image data follows
            out += data[i:]
            return bytes(out)
        length = int.from_bytes(data[i + 2:i + 4], "big")
        segment = data[i:i + 2 + length]
        if len(segment) < 2 + length:
            return data
        is_app = 0xE0 <= marker <= 0xEF
        keep = (
            not is_app and marker != 0xFE
            or marker == 0xE0
            or marker == 0xEE
            or marker == 0xE2 and segment[4:16] == b"ICC_PROFILE\x00"
        )
        if keep:
            out += segment
        i += 2 + length
    return data


@dataclass
class RedactResult:
    applied: list[Match]
    skipped: list[Match]
    boxes: int
    verification: "Verification"


def redact_file(
    src: Path,
    out: Path,
    cfg: Config,
    matches: list[Match] | None = None,
    skip: set[int] = frozenset(),
    boxes: list[Box] = (),
    password: str | None = None,
    progress: Progress | None = None,
) -> RedactResult:
    """Write a redacted, verified copy of src to out.

    matches defaults to a fresh scan; pass the list a caller already showed
    the user so its choices (skip IDs) apply. If verification fails, out is
    deleted and the result lists the leaks.
    """
    if out.resolve() == src.resolve():
        raise RedactError("output must differ from input")
    doc = open_pdf(src, password, prompt=False)
    if matches is None:
        matches = find_matches(doc, cfg, progress)
    applied = redact_document(doc, matches, cfg, skip, boxes, progress)
    skipped = [m for m in matches if m.id in skip]
    if progress:
        progress("Saving", 0, 1)
    save(doc, out)
    doc.close()
    result = verify(out, cfg, applied, skipped, progress)
    if not result.ok:
        out.unlink()
    return RedactResult(applied, skipped, len(cfg.boxes) + len(boxes), result)


def save(doc: pymupdf.Document, out: Path) -> None:
    # Full rewrite (never incremental) with unused objects garbage-collected,
    # so the original text does not survive in an older revision of the file.
    tmp = out.with_name(out.name + ".tmp")
    doc.save(tmp, garbage=4, deflate=True, clean=True, incremental=False)
    # MuPDF writes a "% Written by MuPDF x.y" comment into the header. Blank
    # it with spaces of the same length, so every byte offset stays valid.
    with open(tmp, "r+b") as f:
        head = f.read(256)
        blanked = re.sub(rb"% Written by MuPDF[^\r\n]*",
                         lambda m: b"%" + b" " * (len(m.group()) - 1), head)
        if blanked != head:
            f.seek(0)
            f.write(blanked)
    os.replace(tmp, out)


@dataclass
class Verification:
    leaks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.leaks


def verify(out: Path, cfg: Config, applied: list[Match], skipped: list[Match],
           progress: Progress | None = None) -> Verification:
    """Re-open the output and check nothing that should be gone is still there."""
    result = Verification()
    doc = pymupdf.open(out)
    skipped_texts = {_compact(m.text) for m in skipped}
    leaks: dict[str, None] = {}  # ordered set: one line per distinct leak

    def step(what, done=0, total=1):
        if progress:
            progress(what, done, total)

    # Each distinct redacted value once: the same name or number is often
    # matched dozens of times across a return.
    values: dict[str, str] = {}
    for m in applied:
        key = _compact(m.text)
        if key and key not in skipped_texts:
            values.setdefault(key, m.text)

    # 1. Detection finds nothing new (other than matches the user kept).
    verify_progress = (lambda w, d, t: step("Verifying: re-reading pages", d, t)) if progress else None
    for m in find_matches(doc, cfg, verify_progress):
        if _compact(m.text) not in skipped_texts:
            leaks[f"page {m.page + 1}: {m.kind} still detected: {m.text!r}"] = None

    # 2. No redacted value survives anywhere in the extracted text, in any
    #    formatting (same digit boundaries as detection, so 10075 is not
    #    reported inside an amount like 1,007,500).
    step("Verifying: searching text")
    pages = [page_text(p).text for p in doc]
    for text_value in values.values():
        rx = value_regex(text_value)
        if rx is None:
            continue
        for pno, text in enumerate(pages):
            if hit := rx.search(text):
                ctx = re.sub(r"\s+", " ", text[max(0, hit.start() - 25):hit.end() + 25])
                leaks[f"page {pno + 1}: redacted value {text_value!r} still present: ...{ctx}..."] = None

    # 3. No redacted string survives in the file's raw bytes: object
    #    definitions (metadata, form remnants, stray strings) and decompressed
    #    page drawing commands. Image and font data are skipped (binary, and
    #    image metadata is checked in step 4). Numbers under 9 digits are
    #    skipped too: they turn up by chance in the file's own structure.
    step("Verifying: searching file bytes")
    needles: dict[bytes, str] = {}
    for text_value in values.values():
        if len(text_value.strip()) < 4:
            continue
        digits = re.sub(r"\D", "", text_value)
        if re.fullmatch(r"[\d\s\-()]+", text_value) and len(digits) < 9:
            continue
        for needle in _raw_needles(text_value):
            needles.setdefault(needle, text_value)
    if needles:
        rx = re.compile(b"|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True)))
        for hit in set(rx.findall(_searchable_bytes(doc))):
            leaks[f"raw file bytes still contain {needles[hit]!r}"] = None

    # 4. No metadata stores survive anywhere in the file.
    step("Verifying: checking metadata")
    for issue in metadata_leftovers(doc):
        leaks[issue] = None

    result.leaks = list(leaks)
    doc.close()
    return result


def _searchable_bytes(doc: pymupdf.Document) -> bytes:
    """The file's objects and non-binary streams, decompressed, lowercased."""
    parts = [doc.pdf_trailer().encode("latin-1", "replace")]
    for xref in readable_xrefs(doc):
        obj = doc.xref_object(xref, compressed=False)
        parts.append(obj.encode("latin-1", "replace"))
        if not doc.xref_is_stream(xref):
            continue
        binary = (
            doc.xref_get_key(xref, "Subtype")[1] in ("/Image", "/Type1C", "/CIDFontType0C", "/OpenType")
            or any(doc.xref_get_key(xref, k)[0] != "null" for k in ("Length1", "Length2", "Length3"))
        )
        if not binary:
            try:
                parts.append(doc.xref_stream(xref) or b"")
            except Exception:  # undecodable stream: search it as stored
                parts.append(doc.xref_stream_raw(xref) or b"")
    return b"\n".join(parts).lower()


def metadata_leftovers(doc: pymupdf.Document) -> list[str]:
    """Places outside the page content that still hold identifying data."""
    issues = []
    if any(v for k, v in doc.metadata.items() if k not in ("format", "encryption")):
        issues.append("document info metadata is not empty")
    cat = doc.pdf_catalog()
    for key in CATALOG_STRIP:
        if doc.xref_get_key(cat, key)[0] not in ("null",):
            issues.append(f"document catalog still has /{key}")
    for xref in readable_xrefs(doc):
        for key in OBJECT_STRIP:
            if doc.xref_get_key(xref, key)[0] != "null":
                issues.append(f"object {xref} still has /{key}")
        if doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode":
            raw = doc.xref_stream_raw(xref)
            if strip_jpeg_metadata(raw) != raw:
                issues.append(f"image {xref} still has embedded JPEG metadata")
    return issues


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


REVIEW_RED = (0.85, 0, 0)
REVIEW_GREEN = (0, 0.55, 0)
REVIEW_BLUE = (0.1, 0.3, 0.9)


def write_review_pdf(
    src: Path,
    out: Path,
    matches: list[Match],
    skip: set[int] = frozenset(),
    boxes: list[Box] = (),
    password: str | None = None,
) -> None:
    """Write an UNREDACTED copy of src with every planned redaction outlined.

    Red outlines will be redacted, green ones are kept, blue are manual
    boxes; each match is labelled with its ID. Every page is stamped as a
    review copy, since it still contains all the PII.
    """
    doc = open_pdf(src, password, prompt=False)
    by_page: dict[int, list[Match]] = {}
    for m in matches:
        by_page.setdefault(m.page, []).append(m)
    box_pages: dict[int, list[Box]] = {}
    for b in boxes:
        box_pages.setdefault(b.page - 1, []).append(b)

    for pno, page in enumerate(doc):
        for m in by_page.get(pno, []):
            color = REVIEW_GREEN if m.id in skip else REVIEW_RED
            for r in m.rects:
                annot = page.add_rect_annot(r + (-1, -1, 1, 1))
                annot.set_colors(stroke=color)
                annot.set_border(width=1)
                annot.update()
            label = m.rects[0]
            annot = page.add_freetext_annot(
                pymupdf.Rect(label.x0, label.y0 - 8, label.x0 + 40, label.y0),
                f"#{m.id}" + (" keep" if m.id in skip else ""),
                fontsize=6, text_color=color, fill_color=None, border_width=0,
            )
        for b in box_pages.get(pno, []):
            annot = page.add_rect_annot(pymupdf.Rect(b.rect))
            annot.set_colors(stroke=REVIEW_BLUE)
            annot.set_border(width=1.5, dashes=[3, 2])
            annot.update()
        page.insert_text(
            (page.rect.x0 + 10, page.rect.y0 + 10),
            "REVIEW COPY - NOT REDACTED - contains personal information",
            fontsize=7, color=REVIEW_RED,
        )
    doc.set_metadata({"title": "REVIEW COPY - NOT REDACTED"})
    doc.save(out, garbage=3, deflate=True)
    os.chmod(out, 0o600)
    doc.close()
