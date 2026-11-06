"""Optical character recognition for scanned pages, via Apple's Vision.

A scanned page is a picture of text, usually with an invisible text layer
added by the scanner's OCR. That layer's word positions are often off, so
redaction boxes placed from it miss the visible words. Instead, pages that
are mostly image are rendered and read here, giving boxes taken from the
pixels themselves.

Vision is built into macOS (the engine behind Live Text); it is reached
through PyObjC. Elsewhere, or without PyObjC, no engine is available and
scanned pages fall back to their text layer, with a warning.

An engine takes a PNG of the page and returns its lines of text, each as a
list of (character, box) pairs. Boxes are (x0, y0, x1, y1) as fractions of
the image size with the origin at the top left, or None (e.g. for spaces).
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Callable

import pymupdf

Line = list[tuple[str, tuple[float, float, float, float] | None]]
Engine = Callable[[bytes], list[Line]]

OCR_DPI = 300
MODES = ("auto", "always", "off")

_engine: Engine | None = None
_engine_checked = False


def set_engine(engine: Engine | None) -> None:
    """Use engine for OCR (None: back to the platform default)."""
    global _engine, _engine_checked
    _engine, _engine_checked = engine, engine is not None


def engine() -> Engine | None:
    """The OCR engine to use, or None if this platform has none."""
    global _engine, _engine_checked
    if not _engine_checked:
        _engine_checked = True
        if sys.platform == "darwin" and importlib.util.find_spec("Vision") is not None:
            _engine = vision_engine
    return _engine


def vision_engine(png: bytes) -> list[Line]:
    """Read text with macOS Vision, with a box for every character."""
    import Foundation
    import Vision

    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        Foundation.NSData.dataWithBytes_length_(png, len(png)), None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    # Language correction "fixes" numbers and names into dictionary words.
    request.setUsesLanguageCorrection_(False)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {error}")

    lines: list[Line] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string())
        line: Line = []
        offset = 0  # in UTF-16 units, as NSString counts
        for ch in text:
            width = 2 if ord(ch) > 0xFFFF else 1
            box = None
            if not ch.isspace():
                rect_obs, _err = candidate.boundingBoxForRange_error_(
                    Foundation.NSMakeRange(offset, width), None)
                if rect_obs is not None:
                    r = rect_obs.boundingBox()
                    # Vision: normalized, origin bottom-left; ours: top-left
                    box = (r.origin.x, 1 - r.origin.y - r.size.height,
                           r.origin.x + r.size.width, 1 - r.origin.y)
            line.append((ch, box))
            offset += width
        lines.append(line)
    return lines


def page_is_scanned(page: pymupdf.Page) -> bool:
    """True for a page that is mostly image whose text, if any, is invisible
    (an OCR layer) or nearly absent: what's seen is a picture of the page."""
    area = abs(page.rect)
    if not area:
        return False
    covered = sum(abs(pymupdf.Rect(i["bbox"]) & page.rect) for i in page.get_image_info())
    if covered < 0.5 * area:
        return False
    visible = invisible = 0
    for span in page.get_texttrace():
        n = len(span["chars"])
        # type 3 is "neither fill nor stroke": invisible text
        if span.get("type") == 3 or span.get("opacity", 1) == 0:
            invisible += n
        else:
            visible += n
    return visible < 50 or invisible > visible


def wants_ocr(page: pymupdf.Page, mode: str) -> bool:
    if mode == "off":
        return False
    return mode == "always" or page_is_scanned(page)


def render_png(page: pymupdf.Page, dpi: int = OCR_DPI) -> bytes:
    zoom = dpi / 72
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
