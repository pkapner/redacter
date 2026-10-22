from pathlib import Path

import pymupdf
import pytest

from redacter.cli import main
from redacter.config import Config, load_config, term_regex
from redacter.detect import find_matches
from redacter.redact import open_pdf

NAME = "Jane Q. Doe"
SSN = "123-45-6789"


def make_return(path: Path) -> Path:
    """A fake 1040-style page with PII placed the awkward ways forms do it."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Form 1040 U.S. Individual Income Tax Return 2025", fontsize=12)
    page.insert_text((50, 100), "Your first name and middle initial", fontsize=7)
    page.insert_text((50, 112), "JANE Q", fontsize=10)
    page.insert_text((200, 112), "DOE", fontsize=10)  # separate span
    page.insert_text((400, 112), SSN, fontsize=10)
    page.insert_text((50, 140), "Home address: 123 Main Street, Springfield, IL 62704", fontsize=10)
    # Comb field: one digit per box
    for i, d in enumerate("987654321"):
        page.insert_text((50 + i * 12, 170), d, fontsize=10)
    page.insert_text((50, 200), "Wages 1   85,000.", fontsize=10)
    page.insert_text((50, 230), "Routing number 021000021  Account 000123456789", fontsize=10)
    page.insert_text((50, 260), "Phone (555) 867-5309  jane.doe@example.com", fontsize=10)
    page.insert_text((50, 290), "Employer EIN 12-3456789", fontsize=10)
    page.insert_text((50, 320), "Do not redact this line", fontsize=10)

    # Fillable form field holding an SSN (lives in a widget, not page text)
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.field_name = "spouse_ssn"
    w.rect = pymupdf.Rect(50, 340, 250, 360)
    w.field_value = "222-33-4444"
    page.add_widget(w)

    doc.set_metadata({"author": NAME, "title": f"Return for {NAME}"})
    doc.set_toc([[1, f"{NAME} 1040", 1]])
    doc.save(path)
    return path


@pytest.fixture
def pdf(tmp_path):
    return make_return(tmp_path / "return.pdf")


@pytest.fixture
def config(tmp_path):
    p = tmp_path / "redact.toml"
    p.write_text(
        'terms = ["Jane Q. Doe", "Jane Q", "Doe", "123 Main Street", "Springfield, IL 62704"]\n'
    )
    return p


def all_text(path: Path) -> str:
    doc = pymupdf.open(path)
    return "\n".join(p.get_text() for p in doc)


def test_detects_expected_kinds(pdf, config):
    doc = open_pdf(pdf)
    found = {(m.kind, m.text) for m in find_matches(doc, load_config(config))}
    texts = {t for _, t in found}
    assert ("ssn", SSN) in found
    assert ("ssn", "222-33-4444") in found  # from the form widget
    assert ("ein", "12-3456789") in found
    assert ("nine_digit", "987654321") in found  # comb field
    assert ("nine_digit", "021000021") in found
    assert ("account_number", "000123456789") in found
    assert ("email", "jane.doe@example.com") in found
    assert any(k == "phone" for k, _ in found)
    assert "123 Main Street" in texts
    assert not any("85,000" in t for t in texts)


def test_apply_removes_pii_and_keeps_other_text(pdf, config, tmp_path):
    out = tmp_path / "out.pdf"
    assert main(["apply", str(pdf), "-c", str(config), "-o", str(out)]) == 0
    text = all_text(out)
    for secret in [SSN, "222-33-4444", "12-3456789", "021000021", "000123456789",
                   "867-5309", "jane.doe@example.com", "Main Street", "JANE", "DOE"]:
        assert secret not in text, secret
    assert "Do not redact this line" in text
    assert "85,000." in text
    assert "Form 1040" in text

    doc = pymupdf.open(out)
    assert not any(doc.metadata.values()) or NAME not in str(doc.metadata)
    assert doc.get_toc() == []
    assert not list(doc[0].widgets())
    raw = Path(out).read_bytes()
    assert b"Jane" not in raw and b"Doe" not in raw


def test_skip_keeps_chosen_match(pdf, config, tmp_path, capsys):
    doc = open_pdf(pdf)
    matches = find_matches(doc, load_config(config))
    ein = next(m for m in matches if m.kind == "ein")
    out = tmp_path / "out.pdf"
    assert main(["apply", str(pdf), "-c", str(config), "-o", str(out),
                 "--skip", str(ein.id)]) == 0
    text = all_text(out)
    assert "12-3456789" in text
    assert SSN not in text


def test_allow_list(pdf, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('allow = ["12-3456789"]\n')
    doc = open_pdf(pdf)
    texts = {m.text for m in find_matches(doc, load_config(cfg))}
    assert "12-3456789" not in texts
    assert SSN in texts


def test_manual_box(pdf, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[boxes]]\npage = 1\nrect = [40, 305, 300, 325]\n')
    out = tmp_path / "out.pdf"
    assert main(["apply", str(pdf), "-c", str(cfg), "-o", str(out)]) == 0
    assert "Do not redact this line" not in all_text(out)


def test_scan_changes_nothing_and_writes_review_pdf(pdf, config, tmp_path, capsys):
    before = pdf.read_bytes()
    review = tmp_path / "review.pdf"
    assert main(["scan", str(pdf), "-c", str(config), "--review", str(review),
                 "--skip", "1"]) == 0
    assert pdf.read_bytes() == before
    assert SSN in capsys.readouterr().out
    doc = pymupdf.open(review)
    assert doc.page_count == 1
    assert "REVIEW COPY" in doc[0].get_text()
    page = doc[0]
    annots = list(page.annots())
    assert sum(a.type[1] == "Square" for a in annots) >= 10
    assert any("#1 keep" in (a.info.get("content") or "") for a in annots)


def test_refuses_to_overwrite(pdf, config, tmp_path):
    out = tmp_path / "out.pdf"
    out.write_bytes(b"x")
    assert main(["apply", str(pdf), "-c", str(config), "-o", str(out)]) == 1
    assert main(["apply", str(pdf), "-c", str(config), "-o", str(pdf)]) == 1


def test_unknown_skip_id(pdf, config, tmp_path):
    assert main(["apply", str(pdf), "-c", str(config), "-o", str(tmp_path / "o.pdf"),
                 "--skip", "999"]) == 1


def test_term_regex_is_forgiving():
    rx = term_regex("Jane Q. Doe")
    assert rx.search("name: JANE Q DOE here")
    assert rx.search("Jane Q.  Doe")
    assert not rx.search("JaneQDoe")
    assert not term_regex("Doe").search("Doesn't")


def test_bad_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[patterns]\nnope = true\n")
    with pytest.raises(Exception, match="unknown pattern"):
        load_config(p)


def test_ssn_does_not_match_inside_longer_numbers():
    cfg = Config()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "ref 1123-45-67890 amount 1,234,567.89", fontsize=10)
    assert [m for m in find_matches(doc, cfg) if m.kind == "ssn"] == []


@pytest.mark.parametrize("cell,group_gap", [(14, 12), (18, 10), (22, 20), (16, 40)])
def test_ssn_in_per_digit_boxes(tmp_path, cell, group_gap):
    # Form SSN fields often put each digit in its own cell, three groups
    # separated by dividers; spacing varies from form to form.
    src = tmp_path / "in.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((300, 160), "Your social security number", fontsize=7)
    x = 300
    for group in ["120", "02", "5000"]:
        for d in group:
            page.insert_text((x, 170), d, fontsize=8)
            x += cell
        x += group_gap
    page.insert_text((300, 200), "Amounts 1,234 567 890 and 1 2 3 4 5 6 7 8 9 0", fontsize=8)
    doc.save(src)

    found = [m for m in find_matches(open_pdf(src), Config())]
    assert len(found) == 1 and found[0].kind in ("ssn", "spaced_digits")
    assert found[0].text.replace(" ", "").replace("\t", "") == "120025000"

    out = tmp_path / "out.pdf"
    assert main(["apply", str(src), "-o", str(out)]) == 0
    text = all_text(out)
    assert not any(d in text.split("number")[1].split("Amounts")[0] for d in "0125")
    assert "1,234 567 890" in text


def test_init_writes_private_config(tmp_path):
    p = tmp_path / "redact.toml"
    assert main(["init", str(p)]) == 0
    assert p.stat().st_mode & 0o077 == 0
    load_config(p)  # example config parses


def test_text_after_redaction_does_not_move(tmp_path):
    src = tmp_path / "in.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    y = 100
    for t in ["Line above gjpqy 4,500.", f"SSN {SSN} Wages", "Line below TTTT 7,200."]:
        page.insert_text((50, y), t, fontsize=10)
        y += 10.5  # tighter than the font's line height
    doc.save(src)

    def words(path):
        return {w[4]: round(w[0], 1) for w in pymupdf.open(path)[0].get_text("words")}

    out = tmp_path / "out.pdf"
    assert main(["apply", str(src), "-o", str(out)]) == 0
    before, after = words(src), words(out)
    assert SSN not in after
    for w in ["Line", "above", "gjpqy", "4,500.", "below", "TTTT", "7,200.", "Wages"]:
        assert after.get(w) == before[w], w


def test_inspect_masks_digits(pdf, capsys):
    assert main(["inspect", str(pdf), "first name"]) == 0
    out = capsys.readouterr().out
    spans = [line.split("pt: ", 1)[1] for line in out.splitlines() if "pt: " in line]
    assert "JANE Q" in spans and "###-##-####" in spans
    assert not any(c.isdigit() for span in spans for c in span)


def test_mask_text_hides_unreadable_chars():
    from redacter.detect import mask_text
    assert mask_text("A1\x13\ue001\ufffd\u00e9") == "A#\u25a1\u25c7\ufffd\u00a4"


def make_zero_width_font_pdf(path: Path) -> Path:
    """Glyphs from a font whose widths are all 0, each placed by a TJ offset,
    as CCH tax software does. MuPDF reports zero-width character boxes."""
    doc = pymupdf.open()
    page = doc.new_page()
    font = doc.get_new_xref()
    doc.update_object(font, "<</Type/Font/Subtype/Type1/BaseFont/Courier/FirstChar 32"
                      "/LastChar 126/Widths[" + " ".join(["0"] * 95) + "]>>")
    res = doc.get_new_xref()
    doc.update_object(res, f"<</Font<</ZW {font} 0 R>>>>")
    doc.xref_set_key(page.xref, "Resources", f"{res} 0 R")

    def show(x, y, s):
        glyphs = " ".join(f"({c}) -600" for c in s)
        return f"BT /ZW 10 Tf {x} {y} Td [{glyphs}] TJ ET "

    body = (show(50, 720, "031176110") + show(130, 720, "36057570935")
            + show(50, 690, "123 45 6789") + show(50, 670, "LILY KAPNER")
            + show(50, 650, "Keep this"))
    contents = doc.get_new_xref()
    doc.update_object(contents, "<<>>")
    doc.update_stream(contents, body.encode())
    doc.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
    doc.save(path)
    return path


def test_zero_width_font_glyphs(tmp_path):
    src = make_zero_width_font_pdf(tmp_path / "in.pdf")
    cfg = tmp_path / "c.toml"
    cfg.write_text('terms = ["Lily Kapner"]\n')
    kinds = {m.kind for m in find_matches(open_pdf(src), load_config(cfg))}
    assert kinds == {"nine_digit", "account_number", "ssn", "term"}

    out = tmp_path / "out.pdf"
    assert main(["apply", str(src), "-c", str(cfg), "-o", str(out)]) == 0
    text = "".join(all_text(out).split())  # default extraction spaces every glyph
    assert "Keepthis" in text
    for secret in ["031176110", "36057570935", "6789", "KAPNER"]:
        assert secret not in text


@pytest.mark.parametrize("divider_dy", [0, 3, -3])
def test_ssn_with_divider_glyphs_between_boxes(tmp_path, divider_dy):
    # CCH draws the dotted lines between SSN boxes as large symbol-font
    # glyphs on the digits' baseline: 123 4"5 6"789 without special handling.
    doc = pymupdf.open()
    page = doc.new_page()
    zw = doc.get_new_xref()
    doc.update_object(zw, "<</Type/Font/Subtype/Type1/BaseFont/Courier/FirstChar 32"
                      "/LastChar 126/Widths[" + " ".join(["0"] * 95) + "]>>")
    sym = doc.get_new_xref()
    doc.update_object(sym, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    res = doc.get_new_xref()
    doc.update_object(res, f"<</Font<</ZW {zw} 0 R/SY {sym} 0 R>>>>")
    doc.xref_set_key(page.xref, "Resources", f"{res} 0 R")
    glyphs = " ".join(f"({c}) -600" for c in "123 45 6789")
    body = (f"BT /ZW 9.8 Tf 475 700 Td [{glyphs}] TJ ET "
            f"BT /SY 19.9 Tf 501 {700 + divider_dy} Td (\") Tj ET "
            f"BT /SY 19.9 Tf 522 {700 + divider_dy} Td (\") Tj ET "
            "BT /SY 7 Tf 472 715 Td (Your social security number) Tj ET")
    contents = doc.get_new_xref()
    doc.update_object(contents, "<<>>")
    doc.update_stream(contents, body.encode())
    doc.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
    src = tmp_path / "in.pdf"
    doc.save(src)

    found = find_matches(open_pdf(src), Config())
    assert len(found) == 1 and found[0].kind in ("ssn", "spaced_digits")

    out = tmp_path / "out.pdf"
    assert main(["apply", str(src), "-o", str(out)]) == 0
    text = "".join(all_text(out).split())
    assert "Yoursocialsecuritynumber" in text
    assert not any(d in text for d in "12346789")


def test_adjacent_routing_and_account_numbers(tmp_path):
    # CCH's direct-deposit table: zero-width font, digits 6.9pt apart, the
    # account number starting 2.4 digit-widths after the routing number.
    doc = pymupdf.open()
    page = doc.new_page()
    zw = doc.get_new_xref()
    doc.update_object(zw, "<</Type/Font/Subtype/Type1/BaseFont/Courier/FirstChar 32"
                      "/LastChar 126/Widths[" + " ".join(["0"] * 95) + "]>>")
    res = doc.get_new_xref()
    doc.update_object(res, f"<</Font<</ZW {zw} 0 R>>>>")
    doc.xref_set_key(page.xref, "Resources", f"{res} 0 R")
    step = -round(6.9 / 9.8 * 1000)

    def show(x, y, s):
        return f"BT /ZW 9.8 Tf {x} {y} Td [" + " ".join(f"({c}) {step}" for c in s) + "] TJ ET "

    body = ""
    for y in (700, 676, 652):
        body += show(400, y, "021000021") + show(472, y, "12345678901")
    contents = doc.get_new_xref()
    doc.update_object(contents, "<<>>")
    doc.update_stream(contents, body.encode())
    doc.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
    src = tmp_path / "in.pdf"
    doc.save(src)

    found = sorted((m.kind, m.text) for m in find_matches(open_pdf(src), Config()))
    assert found == [("account_number", "12345678901")] * 3 + [("nine_digit", "021000021")] * 3


def test_value_found_once_is_redacted_everywhere(tmp_path):
    src = tmp_path / "in.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 100), "Preparer phone 212-472-5638", fontsize=10)
    page.insert_text((50, 130), "Designee Phone no.2124725638 PIN 4", fontsize=10)
    page.insert_text((50, 160), "Office (212) 472 5638", fontsize=10)
    page.insert_text((50, 190), "New York, NY 10075  Total 1,007,500. Rate 0.1234567890", fontsize=10)
    page.insert_text((50, 220), "Second page header NY10075", fontsize=10)
    doc.save(src)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[custom]\nzip = '(?<!\\d)10075(?:-?\\d{4})?(?!\\d)'\n")

    out = tmp_path / "out.pdf"
    assert main(["apply", str(src), "-c", str(cfg), "-o", str(out)]) == 0
    text = all_text(out)
    for gone in ["5638", "2124725638", "10075"]:
        assert gone not in text.replace(",", "").replace("1007500", ""), gone
    for kept in ["Preparer phone", "Phone no.", "1,007,500.", "0.1234567890"]:
        assert kept in text, kept


def test_config_round_trip(tmp_path):
    from redacter.config import Box, save_config
    cfg = Config()
    cfg.terms = ["Jane Q. Doe", "O'Brien", "Doe, Jane"]
    cfg.allow = ["800-829-1040"]
    cfg.patterns["date"] = True
    cfg.custom = {"apartment": r"(?:#|apt\.?)\s*2F\b", "q": "it's"}
    cfg.boxes = [Box(2, (10.0, 20.0, 30.5, 40.0))]
    p = tmp_path / "sub" / "redact.toml"
    save_config(cfg, p)
    assert p.stat().st_mode & 0o077 == 0
    back = load_config(p)
    assert (back.terms, back.allow, back.patterns, back.custom) == (
        cfg.terms, cfg.allow, cfg.patterns, cfg.custom)
    assert back.boxes[0].page == 2 and back.boxes[0].rect == (10.0, 20.0, 30.5, 40.0)


def test_identifying_metadata_is_stripped_everywhere(tmp_path):
    """Names hidden in every non-content store of a PDF, including inside a
    JPEG's EXIF/XMP/comment blocks, must all be gone after redaction."""
    import re
    import struct

    def seg(marker, body):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    jpg = pix.tobytes("jpeg")
    jpg = (jpg[:2] + seg(0xE1, b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08 SecretExif")
           + seg(0xE1, b"http://ns.adobe.com/xap/1.0/\x00 SecretXmp")
           + seg(0xFE, b"SecretComment") + jpg[2:])

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 80), "Plain page text", fontsize=11)
    page.insert_image(pymupdf.Rect(50, 100, 150, 200), stream=jpg)
    doc.set_metadata({"author": "SecretInfo"})
    doc.set_xml_metadata("<x:xmpmeta>SecretCatalogXmp</x:xmpmeta>")
    cat = doc.pdf_catalog()
    pm = doc.get_new_xref()
    doc.update_object(pm, "<<>>")
    doc.update_stream(pm, b"<x:xmpmeta>SecretPageXmp</x:xmpmeta>")
    doc.xref_set_key(page.xref, "Metadata", f"{pm} 0 R")
    doc.xref_set_key(page.xref, "LastModified", "(SecretLastMod)")
    doc.xref_set_key(cat, "StructTreeRoot", "<</Type/StructTreeRoot/K <</S/P/Alt (SecretAlt)>> >>")
    doc.xref_set_key(cat, "PageLabels", "<</Nums [0 <</P (SecretLabel)>>]>>")
    doc.xref_set_key(cat, "Names", "<</Dests <</Names [(SecretDest) [3 0 R /Fit]]>>>>")
    src = tmp_path / "in.pdf"
    doc.save(src)
    src_id = pymupdf.open(src).xref_get_key(-1, "ID")[1]

    out = tmp_path / "out.pdf"
    assert main(["apply", str(src), "-o", str(out)]) == 0
    res = pymupdf.open(out)
    blob = res.tobytes(expand=255) + b"".join(
        res.xref_stream_raw(x) for x in range(1, res.xref_length()) if res.xref_is_stream(x))
    assert re.findall(rb"Secret\w*", blob) == []
    assert b"MuPDF" not in out.read_bytes()
    assert res.xref_get_key(-1, "ID")[1][:34] != src_id[:34]
    assert "Plain page text" in res[0].get_text()
    pix = res[0].get_pixmap(clip=pymupdf.Rect(60, 110, 140, 190))
    assert 190 < sum(pix.samples) / len(pix.samples) < 210  # image intact


def test_strip_jpeg_metadata_keeps_decoding_blocks():
    from redacter.redact import strip_jpeg_metadata
    jfif = b"\xff\xe0\x00\x07JFIF\x00"
    icc = b"\xff\xe2\x00\x10ICC_PROFILE\x00\x01\x01"
    adobe = b"\xff\xee\x00\x04ab"
    exif = b"\xff\xe1\x00\x08Exif\x00\x00"
    com = b"\xff\xfe\x00\x05hi!"
    tail = b"\xff\xdb\x00\x03x\xff\xda\x00\x02SCANDATA\xff\xd9"
    data = b"\xff\xd8" + jfif + exif + icc + com + adobe + tail
    assert strip_jpeg_metadata(data) == b"\xff\xd8" + jfif + icc + adobe + tail
    assert strip_jpeg_metadata(b"not a jpeg") == b"not a jpeg"


def test_batch_runs_in_parallel_processes(tmp_path):
    from redacter.batch import BatchJob, output_paths, run_batch
    srcs = [make_return(tmp_path / f"r{i}.pdf") for i in range(3)]
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    srcs.append(tmp_path / "bad.pdf")
    outs = output_paths(srcs, tmp_path)
    jobs = [BatchJob(i, s, o) for i, (s, o) in enumerate(zip(srcs, outs))]
    seen = []
    results = run_batch(jobs, Config(), lambda *a: seen.append(a), workers=2)
    assert [r.ok for r in results] == [True, True, True, False]
    assert "cannot open" in results[3].error
    assert all(SSN not in all_text(o) for o in outs[:3])
    assert seen and seen[-1][1] == 4


def test_output_paths_are_unique():
    from redacter.batch import output_paths
    outs = output_paths([Path("/a/x.pdf"), Path("/b/x.pdf"), Path("/c/y.pdf")], Path("/o"))
    assert [p.name for p in outs] == ["x.redacted.pdf", "x.redacted-2.pdf", "y.redacted.pdf"]


def test_label_touching_value_in_bigger_font():
    # CCH's "Type or Print" label sits right against the address it labels.
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((258, 118), "Print", fontsize=6.9, fontname="hebo")
    w = pymupdf.get_text_length("Print", fontname="hebo", fontsize=6.9)
    page.insert_text((258 + w + 1.0, 118), "340 EAST 80TH STREET APT 2F",
                     fontsize=9.8, fontname="cour")
    cfg = Config()
    cfg.terms = ["340 East 80th Street"]
    assert [(m.kind, m.text) for m in find_matches(doc, cfg)] == [("term", "340 EAST 80TH STREET")]


@pytest.mark.parametrize("referenced", [False, True])
def test_pdf_with_corrupt_object(tmp_path, referenced):
    # Real PDFs often list objects that can't be loaded ("cannot find
    # object in xref"); redaction must carry on regardless.
    import re
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 80), f"SSN {SSN}", fontsize=11)
    spare = doc.get_new_xref()
    doc.update_object(spare, "<</Spare 1>>")
    if referenced:
        doc.xref_set_key(page.xref, "Extra", f"{spare} 0 R")
    good = tmp_path / "good.pdf"
    doc.save(good, garbage=0, deflate=False)
    raw = good.read_bytes()
    assert re.search(rf"\n{spare} 0 obj".encode(), raw)
    src = tmp_path / "broken.pdf"
    src.write_bytes(raw.replace(f"\n{spare} 0 obj".encode(), b"\nXX 0 obj", 1))
    with pytest.raises(Exception):
        pymupdf.open(src).xref_object(spare)  # really broken

    out = tmp_path / "out.pdf"
    assert main(["apply", str(src), "-o", str(out)]) == 0
    assert SSN not in all_text(out)


def test_numeric_term_glued_to_letters(tmp_path):
    # The state and ZIP run together on CCH forms: "NEW YORK NY10075".
    src = tmp_path / "in.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 80), "refund. NEW YORK NY10075 Foreign country", fontsize=10)
    page.insert_text((50, 110), "ZIP 10075 and 100750 and 1,007,500.", fontsize=10)
    doc.save(src)
    cfg = tmp_path / "c.toml"
    cfg.write_text('terms = ["10075", "Doe"]\n')
    found = [m.text for m in find_matches(open_pdf(src), load_config(cfg))]
    assert found == ["10075", "10075"]          # not inside 100750 or 1,007,500

    out = tmp_path / "out.pdf"
    assert main(["apply", str(src), "-c", str(cfg), "-o", str(out)]) == 0
    text = all_text(out)
    assert "NY10075" not in text and "NEW YORK NY" in text
    assert "100750" in text and "1,007,500." in text
    assert not term_regex("Doe").search("Doesn't")
    assert term_regex("80th Street").search("340 E 80th Street")
