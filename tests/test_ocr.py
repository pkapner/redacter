"""Scanned pages: OCR boxes are used instead of a misplaced text layer.

Vision is macOS-only, so these tests plug in a stand-in engine that knows
where the text really is and, like real OCR, only reports characters
whose pixels are still visible.
"""

import pymupdf
import pytest

from redacter import ocr
from redacter.cli import main
from redacter.config import Config
from redacter.detect import find_matches, page_warnings
from redacter.redact import open_pdf

LINES = [
    (72, 100, "Policyholder: Mary Major"),
    (72, 130, "Policy number: LTC1234567"),
    (72, 160, "SSN 123-45-6789"),
    (72, 190, "Premium before increase: $1,212.88"),
]
SECRETS = ["Mary Major", "LTC1234567", "123-45-6789"]


def make_scan(path):
    """An image of LINES, plus an invisible OCR layer that is too narrow
    (smaller font at the same origins), as on the user's scanned letter."""
    src = pymupdf.open()
    sp = src.new_page(width=612, height=792)
    for x, y, t in LINES:
        sp.insert_text((x, y), t, fontsize=14)
    truth = []
    for x, y, t in LINES:
        rd = sp.get_text("rawdict", clip=pymupdf.Rect(0, y - 16, 612, y + 6))
        chars = [(c["c"], pymupdf.Rect(c["bbox"])) for b in rd["blocks"]
                 for ln in b["lines"] for s in ln["spans"] for c in s["chars"]]
        truth.append(chars)
    pix = sp.get_pixmap(matrix=pymupdf.Matrix(2, 2))

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, pixmap=pix)
    for x, y, t in LINES:
        page.insert_text((x, y), t, fontsize=7, render_mode=3)  # misplaced layer
    doc.save(path)
    return truth, sp.rect


def fake_engine(truth, page_rect):
    def engine(png):
        img = pymupdf.Pixmap(png)
        sx, sy = img.width / page_rect.width, img.height / page_rect.height
        lines = []
        for chars in truth:
            line = []
            for c, r in chars:
                if c.isspace():
                    line.append((c, None))
                    continue
                inner = r + (r.width * 0.2, r.height * 0.25, -r.width * 0.2, -r.height * 0.25)
                dark = total = 0
                for fx in range(5):
                    for fy in range(5):
                        x = int((inner.x0 + inner.width * fx / 4) * sx)
                        y = int((inner.y0 + inner.height * fy / 4) * sy)
                        if 0 <= x < img.width and 0 <= y < img.height:
                            total += 1
                            dark += sum(img.pixel(x, y)[:3]) < 300
                if total and dark / total > 0.9:
                    continue  # under a black box: unreadable
                line.append((c, (r.x0 / page_rect.width, r.y0 / page_rect.height,
                                 r.x1 / page_rect.width, r.y1 / page_rect.height)))
            lines.append(line)
        return lines
    return engine


@pytest.fixture
def scan(tmp_path):
    path = tmp_path / "scan.pdf"
    truth, rect = make_scan(path)
    ocr.set_engine(fake_engine(truth, rect))
    yield path, truth
    ocr.set_engine(None)


def config(mode="auto"):
    cfg = Config()
    cfg.terms = ["Mary Major", "LTC1234567"]
    cfg.ocr = mode
    return cfg


def true_rect(truth, text):
    for chars in truth:
        s = "".join(c for c, _ in chars)
        i = s.find(text)
        if i >= 0:
            r = pymupdf.Rect(chars[i][1])
            for _c, b in chars[i:i + len(text)]:
                r |= b
            return r
    raise KeyError(text)


def test_scanned_page_detected():
    doc = pymupdf.open()
    page = doc.new_page()
    assert not ocr.page_is_scanned(page)


def test_matches_use_ocr_boxes_not_the_misplaced_layer(scan):
    path, truth = scan
    doc = open_pdf(path)
    assert ocr.page_is_scanned(doc[0])
    found = {m.text: m for m in find_matches(doc, config())}
    for secret in SECRETS:
        m = found[secret]
        covered = pymupdf.Rect(m.rects[0])
        for r in m.rects[1:]:
            covered |= r
        assert covered.contains(true_rect(truth, secret)), secret
    assert any("read with OCR" in w for w in page_warnings(doc, config()))


def test_redacted_scan_has_no_pii_in_image_or_text(scan, tmp_path):
    path, truth = scan
    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text('terms = ["Mary Major", "LTC1234567"]\n')
    out = tmp_path / "out.pdf"
    assert main(["apply", str(path), "-c", str(cfgfile), "-o", str(out)]) == 0

    res = pymupdf.open(out)
    page = res[0]
    text = page.get_text()
    assert not any(s.split()[-1] in text for s in SECRETS)  # old OCR layer gone
    # the image pixels where each secret really was are wiped
    img = pymupdf.Pixmap(res, page.get_images()[0][0])
    sx, sy = img.width / page.rect.width, img.height / page.rect.height
    for secret in SECRETS:
        r = true_rect(truth, secret)
        dark = 0
        for fx in range(10):
            for fy in range(4):
                x = int((r.x0 + r.width * (fx + 0.5) / 10) * sx)
                y = int((r.y0 + r.height * (fy + 0.5) / 4) * sy)
                dark += sum(img.pixel(x, y)[:3]) < 300
        assert dark == 0, secret
    # and the rest of the letter is still there
    r = true_rect(truth, "Premium")
    x, y = int(r.x0 * sx), int(r.y0 * sy)
    assert any(sum(img.pixel(x + dx, y + dy)[:3]) < 300
               for dx in range(int(r.width * sx)) for dy in range(int(r.height * sy)))


def test_ocr_off_falls_back_to_text_layer(scan):
    path, _truth = scan
    doc = open_pdf(path)
    cfg = config("off")
    found = {m.text for m in find_matches(doc, cfg)}
    assert "123-45-6789" in found  # from the invisible layer
    assert any("OCR is off" in w for w in page_warnings(doc, cfg))
