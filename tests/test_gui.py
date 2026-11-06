"""Smoke tests for the desktop app, run headless (QT_QPA_PLATFORM=offscreen)."""

import os
from pathlib import Path

import pymupdf
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from redacter.gui import MainWindow  # noqa: E402

from test_redacter import SSN, all_text, make_return  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "redact.toml"
    cfg.write_text('terms = ["Jane Q", "Doe", "123 Main Street"]\n')
    w = MainWindow(config_path=cfg)
    w.show()
    yield w
    w.close()


def test_load_toggle_box_and_redact(win, tmp_path):
    src = make_return(tmp_path / "return.pdf")
    assert win.load_pdf(src)
    win.wait_idle()
    assert len(win.matches) >= 10
    assert win.tree.topLevelItemCount() == len(win.matches)

    # keep the EIN by clicking it on the page
    ein = next(m for m in win.matches if m.kind == "ein")
    win.go_page(ein.page)
    center = win.page_view.to_view(ein.rects[0]).center().toPoint()
    QTest.mouseClick(win.page_view, Qt.LeftButton, pos=center)
    assert ein.id in win.kept_ids()

    # draw a box over "Do not redact this line"
    win.set_mode("box")
    a = win.page_view.to_view(pymupdf.Rect(40, 305, 300, 325))
    QTest.mousePress(win.page_view, Qt.LeftButton, pos=a.topLeft().toPoint())
    QTest.mouseMove(win.page_view, a.bottomRight().toPoint())
    QTest.mouseRelease(win.page_view, Qt.LeftButton, pos=a.bottomRight().toPoint())
    assert len(win.boxes) == 1

    out = tmp_path / "out.pdf"
    win.redact_to(out)
    win.wait_idle()
    text = all_text(out)
    assert SSN not in text and "JANE" not in text
    assert "12-3456789" in text            # kept
    assert "Do not redact this line" not in text  # boxed
    assert win.result_file.path == out
    assert win.showing_output


def test_kept_choice_survives_rescan(win, tmp_path):
    src = make_return(tmp_path / "return.pdf")
    win.load_pdf(src)
    win.wait_idle()
    ssn = next(m for m in win.matches if m.text == SSN)
    win.toggle_match(ssn.id)
    win.terms_edit.appendPlainText("Springfield")
    assert win.apply_settings()
    win.wait_idle()
    assert any(m.text == SSN and m.id in win.kept_ids() for m in win.matches)
    assert "Springfield" in Path(win.config_path).read_text()


def test_review_pdf(win, tmp_path, monkeypatch):
    src = make_return(tmp_path / "return.pdf")
    win.load_pdf(src)
    win.wait_idle()
    review = tmp_path / "review.pdf"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(review), ""))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    win.save_review()
    assert "REVIEW COPY" in pymupdf.open(review)[0].get_text()


def test_bad_custom_regex_is_reported(win, monkeypatch):
    seen = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a: seen.append(a[2]))
    win.custom_edit.setPlainText("broken = (unclosed")
    assert not win.apply_settings()
    assert "bad regex" in seen[0]


def test_state_labels_toggle_from_list(win, tmp_path):
    win.load_pdf(make_return(tmp_path / "return.pdf"))
    win.wait_idle()
    item = win.tree.topLevelItem(0)
    assert item.text(0) == "Redact"
    win._tree_item_clicked(item, 0)
    assert item.text(0) == "Keep"
    assert win.matches[0].id in win.kept_ids()


def test_multiple_files_keep_their_own_choices_and_batch(win, tmp_path):
    srcs = [make_return(tmp_path / f"return{i}.pdf") for i in range(3)]
    assert win.add_files(srcs)
    win.wait_idle()
    assert win.file_list.count() == 3 and win.current == 0

    # keep the EIN in file 0, then review file 1 and come back
    ein = next(m for m in win.matches if m.kind == "ein")
    win.toggle_match(ein.id)
    assert win.activate(1)
    win.wait_idle()
    assert win.kept_ids() == set()
    assert win.activate(0)
    assert ein.id in win.kept_ids()   # cached, no rescan needed

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    win.redact_all_to(out_dir)
    win.wait_idle(120000)
    outs = sorted(out_dir.glob("*.redacted.pdf"))
    assert [p.name for p in outs] == [f"return{i}.redacted.pdf" for i in range(3)]
    assert "12-3456789" in all_text(outs[0])        # kept in file 0 only
    assert "12-3456789" not in all_text(outs[1])
    assert all(SSN not in all_text(p) for p in outs)
    assert all(e.output is not None and not e.failed for e in win.entries)
    assert len(win.result_file.paths) == 3


def test_scanned_page_in_app_uses_ocr(win, tmp_path, monkeypatch):
    from redacter import ocr
    from test_ocr import fake_engine, make_scan, true_rect

    path = tmp_path / "scan.pdf"
    truth, rect = make_scan(path)
    ocr.set_engine(fake_engine(truth, rect))
    try:
        win.load_pdf(path)
        win.wait_idle()
        assert any(m.text == "123-45-6789" for m in win.matches)
        assert any("read with OCR" in win.warnings.item(i).text()
                   for i in range(win.warnings.count()))
        # "Add term" on a scanned page offers what OCR read there
        offered = []
        monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                            lambda *a, **k: (offered.append(a[4]), ("", False))[1])
        win.add_term_from_region(true_rect(truth, "Premium before") + (-2, -2, 2, 2))
        assert offered == ["Premium before"]
    finally:
        ocr.set_engine(None)


def make_pages(path, n):
    """n copies of the fake return's page in one PDF."""
    one = pymupdf.open(make_return(path.with_name("one.pdf")))
    doc = pymupdf.open()
    for _ in range(n):
        doc.insert_pdf(one)
    doc.save(path)
    return path


def test_rotated_page_is_redacted_and_saved_turned(win, tmp_path, monkeypatch):
    src = make_return(tmp_path / "return.pdf")
    win.load_pdf(src)
    win.wait_idle()
    before = len(win.matches)
    win.rotate_page(90)
    win.rotate_page(90)
    win.rotate_page(-90)
    assert win.entries[0].rotations == {0: 90}
    assert win.page_view.width() > win.page_view.height()  # shown landscape
    assert len(win.matches) == before  # text layer: nothing to re-read

    out = tmp_path / "out.pdf"
    win.redact_to(out)
    win.wait_idle()
    assert pymupdf.open(out)[0].rotation == 90
    text = all_text(out)
    assert SSN not in text and "JANE" not in text
    assert "Do not redact" in text

    review = tmp_path / "review.pdf"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(review), ""))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    win.save_review()
    assert pymupdf.open(review)[0].rotation == 90


def test_rotation_applies_in_batch(win, tmp_path):
    srcs = [make_return(tmp_path / f"return{i}.pdf") for i in range(2)]
    win.add_files(srcs)
    win.wait_idle()
    win.rotate_page(270)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    win.redact_all_to(out_dir)
    win.wait_idle(120000)
    assert pymupdf.open(out_dir / "return0.redacted.pdf")[0].rotation == 270
    assert pymupdf.open(out_dir / "return1.redacted.pdf")[0].rotation == 0


def test_rotating_scanned_page_reads_it_again(win, tmp_path):
    from redacter import ocr
    from test_ocr import fake_engine, make_scan

    path = tmp_path / "scan.pdf"
    truth, rect = make_scan(path)
    engine = fake_engine(truth, rect)
    calls = []
    ocr.set_engine(lambda png: (calls.append(1), engine(png))[1])
    try:
        win.load_pdf(path)
        win.wait_idle()
        assert len(calls) == 1
        win.rotate_page(180)
        win.wait_idle()
        assert len(calls) == 2
        assert win.page_texts[0].ocr
    finally:
        ocr.set_engine(None)


def test_continuous_scroll(win, tmp_path):
    win.resize(1000, 700)
    win.load_pdf(make_pages(tmp_path / "pages.pdf", 4))
    win.wait_idle()
    assert [v.isVisible() for v in win.views] == [True, False, False, False]

    win.set_continuous(True)
    assert all(v.isVisible() for v in win.views)
    win.go_page(2)
    assert win.page_no == 2
    bar = win.scroll.verticalScrollBar()
    assert bar.value() == win.views[2].y() - 12

    # scrolling moves the current page; a click picks its page for boxes
    bar.setValue(win.views[1].y())
    assert win.page_no == 1
    v = win.views[3]
    win.set_mode("box")
    a = v.to_view(pymupdf.Rect(40, 305, 300, 325))
    QTest.mousePress(v, Qt.LeftButton, pos=a.topLeft().toPoint())
    QTest.mouseMove(v, a.bottomRight().toPoint())
    QTest.mouseRelease(v, Qt.LeftButton, pos=a.bottomRight().toPoint())
    assert win.page_no == 3 and win.boxes[-1].page == 4

    # only pages near the viewport keep a rendered image
    bar.setValue(0)
    win.repaint()
    assert win.views[0].image is not None and win.views[3].image is None

    win.set_continuous(False)
    assert [v.isVisible() for v in win.views].count(True) == 1
