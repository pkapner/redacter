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


def test_scan_changes_nothing_and_previews(pdf, config, tmp_path, capsys):
    before = pdf.read_bytes()
    prev = tmp_path / "prev"
    assert main(["scan", str(pdf), "-c", str(config), "--preview", str(prev)]) == 0
    assert pdf.read_bytes() == before
    assert SSN in capsys.readouterr().out
    assert (prev / "page-001.png").exists()


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
