"""Find sensitive text on PDF pages and map each match back to page rectangles.

PDF text is often split into many small spans, especially on tax forms where
each field is placed separately. Instead of matching span by span, every
character on the page is grouped into visual rows by baseline, the rows are
joined into one string per page, and regexes run over that string. Each
character keeps its bounding box, so a match can be turned back into the
rectangles to black out.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import pymupdf

from .config import Config

# No clipping to the visible page: text hidden outside the crop box still
# leaks through copy/paste and text extraction, so it must be found too.
# MuPDF's synthetic spaces are inhibited; page_text() inserts its own.
TEXT_FLAGS = (
    (pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_MEDIABOX_CLIP)
    | pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_INHIBIT_SPACES
)

# Characters normalized before matching (1:1, so indices stay aligned).
_NORMALIZE = str.maketrans({
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
})


@dataclass
class Char:
    c: str
    bbox: pymupdf.Rect | None  # None for separators we inserted ourselves
    row: int
    baseline: float | None = None  # None for non-horizontal text
    size: float = 0.0


@dataclass
class Match:
    id: int
    page: int  # 0-based
    kind: str
    text: str
    context: str
    rects: list[pymupdf.Rect] = field(default_factory=list)  # black boxes drawn
    cut_rects: list[pymupdf.Rect] = field(default_factory=list)  # text removal areas


@dataclass
class PageText:
    text: str
    chars: list[Char]


def page_text(page: pymupdf.Page) -> PageText:
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    horizontal = []  # (char, bbox, size, origin)
    other_lines = []
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            chars = [
                (ch["c"], pymupdf.Rect(ch["bbox"]), span["size"], ch["origin"])
                for span in line["spans"]
                for ch in span["chars"]
            ]
            if not chars:
                continue
            dx, dy = line["dir"]
            if abs(dy) < 1e-3 and dx > 0:
                horizontal.extend(chars)
            else:
                other_lines.append([(c, b, s, None) for c, b, s, _ in chars])

    # Group horizontal characters into rows that share a baseline.
    horizontal.sort(key=lambda c: (c[3][1], c[3][0]))
    rows: list[list] = []
    row_y = None
    for ch in horizontal:
        y = ch[3][1]
        if rows and abs(y - row_y) <= max(1.0, 0.3 * ch[2]):
            rows[-1].append(ch)
        else:
            rows.append([ch])
            row_y = y
    for row in rows:
        row.sort(key=lambda c: c[3][0])
        _fix_degenerate_boxes(row)
    for line in other_lines:
        for i, (c, b, s, o) in enumerate(line):
            if b.is_empty:  # rotated: no baseline to rebuild from; pad it
                line[i] = (c, pymupdf.Rect(b.x0 - 0.3 * s, b.y0 - 0.3 * s,
                                           b.x1 + 0.3 * s, b.y1 + 0.3 * s), s, o)
    rows.extend(other_lines)

    out: list[Char] = []
    for r, row in enumerate(rows):
        if out:
            out.append(Char("\n", None, r))
        prev = None
        for c, bbox, size, origin in row:
            if prev is not None and not prev[0].isspace() and not c.isspace():
                gap = bbox.x0 - prev[1].x1
                both_digits = prev[0].isdigit() and c.isdigit()
                if gap > 3 * size:
                    # Far apart: different form columns. A tab keeps number
                    # patterns (which only allow spaces) from joining them.
                    out.append(Char("\t", None, r))
                elif gap > 0.25 * size and not (both_digits and gap < 1.5 * size):
                    # Visible gap with no space character. Digits in comb
                    # fields (one box per digit) are left joined.
                    out.append(Char(" ", None, r))
            baseline = origin[1] if origin is not None else None
            out.append(Char(c.translate(_NORMALIZE), bbox, r, baseline, size))
            prev = (c, bbox)

    return PageText("".join(ch.c for ch in out), out)


def _fix_degenerate_boxes(row: list) -> None:
    """Rebuild zero-width or zero-height character boxes in a sorted row.

    Some tax software (CCH, for one) embeds fonts with zero glyph widths and
    positions every glyph explicitly, so MuPDF reports boxes with x0 == x1.
    Such boxes break gap detection and give nothing to redact. The width is
    taken from the distance to the next character, the height from the
    font size.
    """
    for i, (c, b, size, origin) in enumerate(row):
        x0, y0, x1, y1 = b
        if b.width < 0.01 * size:
            nxt = row[i + 1][3][0] if i + 1 < len(row) else None
            step = nxt - origin[0] if nxt is not None else 0
            x0 = origin[0]
            x1 = x0 + (step if 0 < step <= 1.2 * size else 0.6 * size)
        if b.height < 0.01 * size:
            y0, y1 = origin[1] - 0.8 * size, origin[1] + 0.2 * size
        if (x0, y0, x1, y1) != tuple(b):
            row[i] = (c, pymupdf.Rect(x0, y0, x1, y1), size, origin)


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def find_matches(doc: pymupdf.Document, cfg: Config) -> list[Match]:
    patterns = cfg.compiled_patterns()
    allowed = {_norm(a) for a in cfg.allow}
    matches: list[Match] = []

    for pno, page in enumerate(doc):
        pt = page_text(page)
        spans = []  # (start, end, kind)
        for kind, rx, keep in patterns:
            for m in rx.finditer(pt.text):
                if m.end() > m.start() and (keep is None or keep(m)):
                    spans.append((m.start(), m.end(), kind))

        # Earliest start first, longest first; drop spans inside a kept span.
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        kept = []
        for s in spans:
            if any(k[0] <= s[0] and s[1] <= k[1] for k in kept):
                continue
            kept.append(s)

        for start, end, kind in kept:
            text = pt.text[start:end]
            if _norm(text) in allowed:
                continue
            rects, cut_rects = _rects_for(pt.chars[start:end], pt.chars)
            if not rects:
                # Never drop a match silently: it would be neither shown nor
                # redacted. Degenerate boxes are rebuilt upstream, so this is a bug.
                raise RuntimeError(
                    f"page {pno + 1}: match {kind!r} has no location on the page")
            ctx = pt.text[max(0, start - 30):end + 30]
            ctx = re.sub(r"\s+", " ", ctx).strip()
            matches.append(Match(0, pno, kind, text, ctx, rects, cut_rects))

    for i, m in enumerate(matches, 1):
        m.id = i
    return matches


def _rects_for(
    chars: list[Char], page_chars: list[Char]
) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
    """Return (cover, cut) rectangles, one of each per row of the match.

    MuPDF removes every glyph whose box touches a redaction area, and its
    glyph boxes span the font's full line height, so on tightly spaced forms
    a box around one row also wipes letters on the rows above and below.
    The cut rectangle is therefore a band through the row, trimmed clear of
    other rows' glyph boxes; the cover rectangle is the visible black box,
    sized to the glyphs, and is drawn separately after the text is removed.
    """
    rows: dict[int, list[Char]] = {}
    for ch in chars:
        if ch.bbox is not None and not ch.bbox.is_empty:
            rows.setdefault(ch.row, []).append(ch)

    covers, cuts = [], []
    for row, row_chars in rows.items():
        full = pymupdf.Rect(row_chars[0].bbox)
        for ch in row_chars[1:]:
            full |= ch.bbox
        baseline = row_chars[0].baseline
        if baseline is None:  # rotated text: no trimming
            covers.append(full)
            cuts.append(pymupdf.Rect(full))
            continue

        size = max(ch.size for ch in row_chars)
        cover = pymupdf.Rect(full.x0, max(full.y0, baseline - 0.85 * size),
                             full.x1, min(full.y1, baseline + 0.25 * size))
        # Band from cap height to just under the baseline, then shrunk away
        # from any other row's glyph box that overlaps it horizontally.
        top, bottom = baseline - 0.7 * size, baseline + 0.1 * size
        mid = baseline - 0.35 * size
        for other in page_chars:
            b = other.bbox
            if (other.row == row or b is None or b.is_empty
                    or b.x1 <= full.x0 or b.x0 >= full.x1):
                continue
            if b.y1 <= mid and b.y1 > top:
                top = b.y1 + 0.01
            elif b.y0 >= mid and b.y0 < bottom:
                bottom = b.y0 - 0.01
        if bottom - top < 0.1 * size:
            # Overlapping rows: removing this text without touching the
            # neighbours is impossible. Remove it anyway; verification and
            # the preview will show the collateral damage.
            top, bottom = mid - 0.05 * size, mid + 0.05 * size
        # Pull the sides in a little too: a band that merely touches the next
        # glyph removes it, and a removed space shifts the rest of the line.
        inset = min(0.5, 0.2 * min(ch.bbox.width for ch in row_chars))
        covers.append(cover)
        cuts.append(pymupdf.Rect(full.x0 + inset, top, full.x1 - inset, bottom))
    return covers, cuts


def char_class(c: str) -> str:
    """'digit', 'ascii', 'space', 'control', 'private', 'unmapped' or 'other'."""
    if c.isdigit():
        return "digit"
    if c.isspace():
        return "space"
    if c == "\ufffd":
        return "unmapped"
    cat = unicodedata.category(c)
    if cat == "Cc":
        return "control"
    if cat == "Co":
        return "private"
    return "ascii" if c.isascii() else "other"


_MASK = {"digit": "#", "control": "\u25a1", "private": "\u25c7",
         "unmapped": "\ufffd", "other": "\u00a4"}


def mask_text(text: str) -> str:
    """Hide digits and anything that might be a digit in a custom encoding."""
    return "".join(_MASK.get(char_class(c), c) for c in text)


UNREADABLE = {"control", "private", "unmapped"}


def page_warnings(doc: pymupdf.Document) -> list[str]:
    """Flag pages whose content the text-based detector cannot see."""
    warnings = []
    for pno, page in enumerate(doc):
        fonts: dict[str, int] = {}
        raw = page.get_text("rawdict", flags=TEXT_FLAGS)
        for block in raw["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    bad = sum(char_class(ch["c"]) in UNREADABLE for ch in span["chars"])
                    if bad:
                        fonts[span["font"]] = fonts.get(span["font"], 0) + bad
        if fonts:
            warnings.append(
                f"page {pno + 1} has {sum(fonts.values())} character(s) with no "
                f"readable text mapping (font {', '.join(sorted(fonts))}); numbers "
                f"in that font can't be detected. Run 'redacter inspect' near them"
            )

        images = page.get_image_info()
        if not images:
            continue
        text_len = len(page.get_text("text", flags=TEXT_FLAGS).strip())
        area = abs(page.rect)
        covered = sum(abs(pymupdf.Rect(i["bbox"]) & page.rect) for i in images)
        if text_len < 50 and covered > 0.5 * area:
            warnings.append(
                f"page {pno + 1} looks scanned (mostly image, little text); "
                f"it was NOT checked for PII"
            )
        else:
            warnings.append(
                f"page {pno + 1} has {len(images)} image(s); images are not "
                f"checked for PII, so look at them in the preview"
            )
    return warnings
