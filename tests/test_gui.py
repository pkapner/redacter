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
