"""Find sensitive text on PDF pages and map each match back to page rectangles.

PDF text is often split into many small spans, especially on tax forms where
each field is placed separately. Instead of matching span by span, every
character on the page is grouped into visual rows by baseline, the rows are
joined into one string per page, and regexes run over that string. Each
character keeps its bounding box, so a match can be turned back into the
rectangles to black out.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from .config import Config, term_regex

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
        dividers = _dividers(row) if row and row[0][3] is not None else set()
        gaps = [0.0] + [row[i][1].x0 - row[i - 1][1].x1 for i in range(1, len(row))]

        def digit_gap(i):  # gap before row[i] if it sits between two digits
            if 0 < i < len(row) and row[i - 1][0].isdigit() and row[i][0].isdigit():
                return gaps[i]
            return None

        for i, (c, bbox, size, origin) in enumerate(row):
            if i in dividers:
                # Read a box divider as a space; leave its outline out of
                # the match so it does not inflate the black box.
                out.append(Char(" ", None, r))
                prev = (" ", bbox, size)
                continue
            if prev is not None and not prev[0].isspace() and not c.isspace():
                gap = bbox.x0 - prev[1].x1
                both_digits = prev[0].isdigit() and c.isdigit()
                size_jump = max(size, prev[2]) > 1.2 * min(size, prev[2])
                if gap > 3 * size:
                    # Far apart: different form columns. A tab keeps number
                    # patterns (which only allow spaces) from joining them.
                    out.append(Char("\t", None, r))
                elif size_jump and gap > -0.1 * size:
                    # A font size change is a label meeting a value, e.g. the
                    # "Print" of a "Type or Print" label right against an
                    # address; without a break, "Print340 EAST..." would hide
                    # the address from terms.
                    out.append(Char(" ", None, r))
                elif both_digits:
                    # Digits in comb fields (one box per digit) are evenly
                    # spaced and stay joined. A gap clearly wider than the
                    # spacing of the digits around it separates two numbers,
                    # e.g. a routing number printed right before an account
                    # number, or the groups of an SSN.
                    around = [g for g in (digit_gap(i - 1), digit_gap(i + 1)) if g is not None]
                    if around:
                        split = gap > 1.5 * max(0.0, *around) + 0.15 * size
                    else:
                        split = gap >= 1.5 * size
                    if split:
                        out.append(Char(" ", None, r))
                elif gap > 0.25 * size:
                    # Visible gap with no space character.
                    out.append(Char(" ", None, r))
            baseline = origin[1] if origin is not None else None
            out.append(Char(c.translate(_NORMALIZE), bbox, r, baseline, size))
            prev = (c, bbox, size)

    return PageText("".join(ch.c for ch in out), out)


def _dividers(row: list) -> set[int]:
    """Indices of glyphs in a sorted row that are box dividers, not text.

    CCH draws the dotted lines between SSN boxes as big glyphs from a symbol
    font ('"' in CCHSymbolTwo) on the same baseline as the digits, which
    would otherwise read as 123 4"5 6"789. A divider is a non-alphanumeric
    glyph at least 1.5x the size of the alphanumerics on both sides of it.
    """
    alnum = [i for i, ch in enumerate(row) if ch[0].isalnum()]
    found = set()
    for i, (c, _b, size, _o) in enumerate(row):
        if c.isalnum() or c.isspace():
            continue
        left = [row[j][2] for j in alnum if j < i][-1:]
        right = [row[j][2] for j in alnum if j > i][:1]
        sides = left + right
        if sides and all(size >= 1.5 * s for s in sides):
            found.add(i)
    return found


def _fix_degenerate_boxes(row: list) -> None:
    """Rebuild zero-width or zero-height character boxes in a sorted row.

    Some tax software (CCH, for one) embeds fonts with zero glyph widths and
    positions every glyph explicitly, so MuPDF reports boxes with x0 == x1.
    Such boxes break gap detection and give nothing to redact. The width is
    the distance to the next character, capped at a typical glyph width
    (0.6 em, exact for the Courier-style fonts used for filled-in values) so
    that real spacing between two numbers still shows as a gap. The height
    comes from the font size.
    """
    for i, (c, b, size, origin) in enumerate(row):
        x0, y0, x1, y1 = b
        if b.width < 0.01 * size:
            nxt = row[i + 1][3][0] if i + 1 < len(row) else None
            step = nxt - origin[0] if nxt is not None else 0
            x0 = origin[0]
            x1 = x0 + (min(step, 0.6 * size) if step > 0 else 0.6 * size)
        if b.height < 0.01 * size:
            y0, y1 = origin[1] - 0.8 * size, origin[1] + 0.2 * size
        if (x0, y0, x1, y1) != tuple(b):
            row[i] = (c, pymupdf.Rect(x0, y0, x1, y1), size, origin)


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def match_key(m: Match) -> tuple:
    """Identity of a match that survives a rescan (IDs are renumbered)."""
    r = m.rects[0]
    return (m.page, " ".join(m.text.split()).casefold(), round(r.x0), round(r.y0))


def value_regex(text: str) -> re.Pattern | None:
    """Regex for other copies of an already-found value, in any formatting.

    A number matches with any spaces, dashes or parentheses between its
    digits (212-472-5638 also finds 2124725638 and (212) 472 5638), but not
    inside a longer number or an amount. Other values match as terms do.
    """
    if re.fullmatch(r"[\d\s\-()]+", text):
        digits = re.sub(r"\D", "", text)
        if len(digits) < 5:
            return None
        body = r"[ \-()]*".join(digits)
        return re.compile(rf"(?<![\d,$])(?<!\d\.){body}(?![\d,]|\.\d)")
    if not text.split():
        return None
    return term_regex(text)


Progress = Callable[[str, int, int], None]  # (what, done, total)


def find_matches(doc: pymupdf.Document, cfg: Config,
                 progress: Progress | None = None) -> list[Match]:
    patterns = cfg.compiled_patterns()
    allowed = {_norm(a) for a in cfg.allow}
    texts = []
    for pno, page in enumerate(doc):
        if progress:
            progress("Reading pages", pno, doc.page_count)
        texts.append(page_text(page))

    # First pass: every pattern on every page.
    found: list[list[tuple[int, int, str]]] = []
    for pt in texts:
        spans = []  # (start, end, kind)
        for kind, rx, keep in patterns:
            for m in rx.finditer(pt.text):
                if m.end() > m.start() and (keep is None or keep(m)):
                    spans.append((m.start(), m.end(), kind))
        found.append(spans)

    # Second pass: a value found anywhere is redacted everywhere, however it
    # is formatted there (a phone number found as 212-472-5638 on one line
    # is also removed where it appears as "no.2124725638").
    repeats: dict[str, tuple[re.Pattern, str]] = {}
    for pt, spans in zip(texts, found):
        for start, end, kind in spans:
            value = pt.text[start:end]
            if kind == "term" or _norm(value) in allowed:
                continue  # terms already match every occurrence
            key = re.sub(r"\D", "", value) or _norm(value)
            if key not in repeats and (rx := value_regex(value)):
                repeats[key] = (rx, kind)
    for pt, spans in zip(texts, found):
        for rx, kind in repeats.values():
            for m in rx.finditer(pt.text):
                spans.append((m.start(), m.end(), f"{kind} (repeat)"))

    matches: list[Match] = []
    for pno, (pt, spans) in enumerate(zip(texts, found)):

        # Earliest start first, longest first; drop spans inside a kept span.
        # Every kept span starts at or before s, so s lies inside one of them
        # exactly when the furthest kept end reaches s's end.
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        kept = []
        furthest = -1
        for s in spans:
            if s[1] <= furthest:
                continue
            kept.append(s)
            furthest = s[1]

        index = GlyphIndex(pt.chars) if kept else None

        for start, end, kind in kept:
            text = pt.text[start:end]
            if _norm(text) in allowed:
                continue
            rects, cut_rects = _rects_for(pt.chars[start:end], index)
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


class GlyphIndex:
    """Glyph boxes of a page sorted by top edge, for fast "what is near this
    band" queries (a page can hold thousands of glyphs and hundreds of
    matches, so scanning every glyph per match is far too slow)."""

    def __init__(self, chars: list[Char]):
        boxes = sorted(
            (b.y0, b.y1, b.x0, b.x1, ch.row)
            for ch in chars
            if (b := ch.bbox) is not None and b.x1 > b.x0 and b.y1 > b.y0
        )
        self.boxes = boxes
        self.tops = [b[0] for b in boxes]
        self.tallest = max((b[1] - b[0] for b in boxes), default=0.0)

    def near(self, top: float, bottom: float):
        """Boxes that may overlap the vertical band [top, bottom]."""
        lo = bisect.bisect_left(self.tops, top - self.tallest)
        hi = bisect.bisect_right(self.tops, bottom)
        return self.boxes[lo:hi]


def _rects_for(
    chars: list[Char], index: GlyphIndex
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
        for y0, y1, x0, x1, other_row in index.near(top, bottom):
            if other_row == row or x1 <= full.x0 or x0 >= full.x1:
                continue
            if y1 <= mid and y1 > top:
                top = y1 + 0.01
            elif y0 >= mid and y0 < bottom:
                bottom = y0 - 0.01
        if bottom - top < 0.1 * size:
            # Overlapping rows: removing this text without touching the
            # neighbours is impossible. Remove it anyway; verification and
            # the review copy will show the collateral damage.
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
                f"checked for PII, so look at them yourself"
            )
    return warnings
