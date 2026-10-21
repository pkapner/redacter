"""Smoke tests for the desktop app, run headless (QT_QPA_PLATFORM=offscreen)."""

import os
from pathlib import Path

import pymupdf
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
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
    assert win.redact_to(out)
    text = all_text(out)
    assert SSN not in text and "JANE" not in text
    assert "12-3456789" in text            # kept
    assert "Do not redact this line" not in text  # boxed
    assert win.result_file.path == out
    assert win.showing_output


def test_kept_choice_survives_rescan(win, tmp_path):
    src = make_return(tmp_path / "return.pdf")
    win.load_pdf(src)
    ssn = next(m for m in win.matches if m.text == SSN)
    win.toggle_match(ssn.id)
    win.terms_edit.appendPlainText("Springfield")
    assert win.apply_settings()
    assert any(m.text == SSN and m.id in win.kept_ids() for m in win.matches)
    assert "Springfield" in Path(win.config_path).read_text()


def test_review_pdf(win, tmp_path, monkeypatch):
    src = make_return(tmp_path / "return.pdf")
    win.load_pdf(src)
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
