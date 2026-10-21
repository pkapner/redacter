"""Desktop app: drop a PDF, review what will be redacted, redact it.

Run with `redacter-gui` (or `redacter gui`). Needs the gui extra:
pip install -e '.[gui]'
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from PySide6.QtCore import (
    QEvent, QEventLoop, QMimeData, QPoint, QRect, QRectF, QSettings, QSize, QStandardPaths,
    Qt, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QBrush, QColor, QDesktopServices, QDrag, QFont, QImage,
    QKeySequence, QPainter, QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QStatusBar, QTabWidget, QToolBar, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .config import BUILTIN_PATTERNS, Box, Config, ConfigError, load_config, save_config
from .batch import BatchJob, output_paths, run_batch
from .detect import Match, find_matches, match_key, page_text, page_warnings
from .redact import PasswordNeeded, RedactError, open_pdf, redact_file, write_review_pdf

PATTERN_HELP = {
    "ssn": "SSN / ITIN with dashes or spaces (123-45-6789)",
    "ein": "Employer ID number (12-3456789)",
    "nine_digit": "Any 9-digit number (undashed SSN, routing number)",
    "account_number": "10-17 digit numbers (bank accounts)",
    "phone": "Phone numbers",
    "email": "Email addresses",
    "spaced_digits": "9 digits in separate boxes (1 2 3 | 4 5 | 6 7 8 9)",
    "date": "Dates like 01/02/1980 (off by default: forms are full of dates)",
}

RED = QColor(220, 30, 30)
GREEN = QColor(20, 150, 60)
BLUE = QColor(40, 90, 230)


def default_config_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    return Path(base or Path.home() / ".config" / "redacter") / "redact.toml"


@dataclass
class FileEntry:
    """One PDF in the file list and the review choices made for it."""

    path: Path
    password: str | None = None
    matches: list[Match] | None = None
    scanned_version: int = -1     # settings version the matches came from
    warnings: list[str] = field(default_factory=list)
    kept_keys: set[tuple] = field(default_factory=set)
    boxes: list[Box] = field(default_factory=list)
    page_no: int = 0
    output: Path | None = None
    status: str = "not scanned"
    failed: bool = False


# --------------------------------------------------------------------------
# Page view


class PageView(QWidget):
    """One rendered page with redaction overlays and mouse tools."""

    matchClicked = Signal(int)          # match id
    boxDrawn = Signal(object)           # pymupdf.Rect, unrotated page coords
    boxRemoveRequested = Signal(int)    # index into self.boxes
    termRegionSelected = Signal(object)  # pymupdf.Rect, unrotated page coords

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.page: pymupdf.Page | None = None
        self.zoom = 1.25
        self.image: QImage | None = None
        self.matches: list[Match] = []
        self.kept: set[int] = set()
        self.boxes: list[Box] = []       # boxes on this page
        self.box_indices: list[int] = []  # their indices in the window's list
        self.highlight: int | None = None
        self.overlays = True
        self.mode = "select"
        self._drag_start: QPoint | None = None
        self._drag_now: QPoint | None = None

    # geometry ------------------------------------------------------------

    def set_page(self, page: pymupdf.Page | None, overlays: bool = True):
        self.page = page
        self.overlays = overlays
        self.render()

    def render(self):
        if self.page is None:
            self.image = None
            self.setFixedSize(QSize(0, 0))
            self.update()
            return
        dpr = self.devicePixelRatioF() or 1.0
        z = self.zoom * dpr
        pix = self.page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        img.setDevicePixelRatio(dpr)
        self.image = img
        self.setFixedSize(QSize(round(pix.width / dpr), round(pix.height / dpr)))
        self.update()

    def to_view(self, r: pymupdf.Rect) -> QRectF:
        d = r * self.page.rotation_matrix
        d.normalize()
        z = self.zoom
        return QRectF(d.x0 * z, d.y0 * z, d.width * z, d.height * z)

    def to_page(self, a: QPoint, b: QPoint) -> pymupdf.Rect:
        z = self.zoom
        r = pymupdf.Rect(min(a.x(), b.x()) / z, min(a.y(), b.y()) / z,
                         max(a.x(), b.x()) / z, max(a.y(), b.y()) / z)
        r = r * self.page.derotation_matrix
        r.normalize()
        return r

    def _match_at(self, pos: QPoint) -> Match | None:
        for m in self.matches:
            for r in m.rects:
                if self.to_view(r).adjusted(-2, -2, 2, 2).contains(pos):
                    return m
        return None

    def _box_at(self, pos: QPoint) -> int | None:
        for b, idx in zip(self.boxes, self.box_indices):
            if self.to_view(pymupdf.Rect(b.rect)).contains(pos):
                return idx
        return None

    # painting ------------------------------------------------------------

    def paintEvent(self, _event):
        if self.image is None:
            return
        p = QPainter(self)
        p.drawImage(0, 0, self.image)
        if not self.overlays:
            return
        p.setRenderHint(QPainter.Antialiasing)
        for m in self.matches:
            color = GREEN if m.id in self.kept else RED
            fill = QColor(color)
            fill.setAlpha(45)
            pen = QPen(color, 3 if m.id == self.highlight else 1.5)
            for r in m.rects:
                rv = self.to_view(r).adjusted(-1.5, -1.5, 1.5, 1.5)
                p.fillRect(rv, fill)
                p.setPen(pen)
                p.drawRect(rv)
        for b in self.boxes:
            rv = self.to_view(pymupdf.Rect(b.rect))
            p.fillRect(rv, QColor(0, 0, 0, 120))
            p.setPen(QPen(BLUE, 1.5, Qt.DashLine))
            p.drawRect(rv)
        if self._drag_start and self._drag_now:
            color = BLUE if self.mode == "box" else QColor(230, 140, 0)
            p.setPen(QPen(color, 1.5, Qt.DashLine))
            fill = QColor(color)
            fill.setAlpha(40)
            r = QRect(self._drag_start, self._drag_now).normalized()
            p.fillRect(r, fill)
            p.drawRect(r)

    # mouse ---------------------------------------------------------------

    def mousePressEvent(self, e):
        if self.page is None or not self.overlays:
            return
        if e.button() == Qt.RightButton:
            idx = self._box_at(e.position().toPoint())
            if idx is not None:
                menu = QMenu(self)
                remove = menu.addAction("Remove box")
                if menu.exec(e.globalPosition().toPoint()) == remove:
                    self.boxRemoveRequested.emit(idx)
            return
        if e.button() == Qt.LeftButton:
            self._drag_start = e.position().toPoint()
            self._drag_now = None

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if self._drag_start is not None and self.mode in ("box", "term"):
            self._drag_now = pos
            self.update()
            return
        if self.page is not None and self.overlays:
            m = self._match_at(pos)
            if m is not None:
                self.setCursor(Qt.PointingHandCursor)
                state = "kept (click to redact)" if m.id in self.kept else "will be redacted (click to keep)"
                self.setToolTip(f"#{m.id} {m.kind}: {m.text}\n{state}")
                return
            self.setToolTip("")
            self.setCursor(Qt.CrossCursor if self.mode in ("box", "term") else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self._drag_start is None:
            return
        start, end = self._drag_start, e.position().toPoint()
        self._drag_start = self._drag_now = None
        self.update()
        dragged = (end - start).manhattanLength() > 4
        if dragged and self.mode == "box":
            self.boxDrawn.emit(self.to_page(start, end))
        elif dragged and self.mode == "term":
            self.termRegionSelected.emit(self.to_page(start, end))
        elif not dragged:
            m = self._match_at(end)
            if m is not None:
                self.matchClicked.emit(m.id)


# --------------------------------------------------------------------------
# Background work


class Worker(QThread):
    """Runs fn(progress) off the UI thread so the window stays responsive.

    The window is disabled while a worker runs, so the UI thread never uses
    PyMuPDF at the same time (it is not safe to use from two threads at once).
    """

    progressed = Signal(str, int, int)
    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            result = self.fn(lambda what, done, total: self.progressed.emit(what, done, total))
        except Exception as e:  # reported to the user by the window
            self.failed.emit(e)
        else:
            self.succeeded.emit(result)


# --------------------------------------------------------------------------
# Small widgets


class DropZone(QLabel):
    def __init__(self):
        super().__init__("Drop a PDF here\n\nor use Open… (⌘O)\n\n"
                         "Tip: drop your redact.toml here too, to use your saved terms.")
        self.setAlignment(Qt.AlignCenter)
        f = self.font()
        f.setPointSize(f.pointSize() + 6)
        self.setFont(f)
        self.setStyleSheet(
            "QLabel { border: 3px dashed #9a9a9a; border-radius: 18px; color: #777;"
            " margin: 40px; padding: 40px; }")


class DraggableFile(QLabel):
    """Shows output file(s); drag them out to Finder (or anywhere that takes files)."""

    def __init__(self):
        super().__init__()
        self.paths: list[Path] = []
        self._press: QPoint | None = None
        self.setCursor(Qt.OpenHandCursor)
        self.setStyleSheet(
            "QLabel { background: #eef6ee; border: 1px solid #7fb27f; border-radius: 6px;"
            " padding: 6px 10px; font-weight: bold; color: #1d4d1d; }")
        self.setToolTip("Drag to Finder, Mail, or anywhere else")

    @property
    def path(self) -> Path | None:
        return self.paths[0] if len(self.paths) == 1 else None

    def set_path(self, path: Path):
        self.set_paths([path])

    def set_paths(self, paths: list[Path]):
        self.paths = list(paths)
        label = self.paths[0].name if len(self.paths) == 1 else f"{len(self.paths)} redacted PDFs"
        self.setText(f"📄  {label}   ⇢ drag me")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseMoveEvent(self, e):
        if self._press is None or not self.paths:
            return
        if (e.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in self.paths])
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)
        self._press = None
        if not any(p.exists() for p in self.paths):  # moved away by the drop target
            self.window().output_moved(self.paths)


# --------------------------------------------------------------------------
# Main window


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None):
        super().__init__()
        self.setWindowTitle("Redacter")
        self.setAcceptDrops(True)
        self.resize(1300, 900)

        self.settings = QSettings("redacter", "redacter")
        saved = self.settings.value("config_path", "")
        self.config_path = Path(config_path or saved or default_config_path())
        self.cfg = Config()

        self.entries: list[FileEntry] = []
        self.current: int | None = None   # index into entries
        self.cfg_version = 0              # bumped when settings change
        self.src: Path | None = None
        self.password: str | None = None
        self.doc: pymupdf.Document | None = None       # baked original, for display
        self.out_doc: pymupdf.Document | None = None   # redacted output, for display
        self.output: Path | None = None
        self.matches: list[Match] = []
        self.kept_keys: set[tuple] = set()
        self.boxes: list[Box] = []
        self.page_no = 0
        self.showing_output = False
        self.worker: Worker | None = None
        self.progress_dialog: QProgressDialog | None = None

        self._build_ui()
        self._load_config(self.config_path, quiet=True)
        self._update_enabled()

    # UI construction -----------------------------------------------------

    def _build_ui(self):
        tb = QToolBar("Main")
        self.toolbar = tb
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        self.act_open = QAction("Open…", self, shortcut=QKeySequence.Open, triggered=self.choose_pdf)
        tb.addAction(self.act_open)
        tb.addSeparator()

        self.mode_group = QActionGroup(self)
        self.mode_actions = {}
        for mode, label, tip in [
            ("select", "Select", "Click a highlighted match to keep it or redact it"),
            ("box", "Draw box", "Drag a rectangle to black out anything (e.g. a signature)"),
            ("term", "Add term", "Drag across text to redact it everywhere in the document"),
        ]:
            a = QAction(label, self, checkable=True, toolTip=tip)
            a.triggered.connect(lambda _=False, m=mode: self.set_mode(m))
            self.mode_group.addAction(a)
            tb.addAction(a)
            self.mode_actions[mode] = a
        self.mode_actions["select"].setChecked(True)
        tb.addSeparator()

        self.act_prev = QAction("◀", self, shortcut=QKeySequence(Qt.Key_PageUp),
                                toolTip="Previous page", triggered=lambda: self.go_page(self.page_no - 1))
        self.act_next = QAction("▶", self, shortcut=QKeySequence(Qt.Key_PageDown),
                                toolTip="Next page", triggered=lambda: self.go_page(self.page_no + 1))
        self.page_label = QLabel("  –  ")
        tb.addAction(self.act_prev)
        tb.addWidget(self.page_label)
        tb.addAction(self.act_next)
        self.act_zoom_out = QAction("−", self, shortcut=QKeySequence.ZoomOut,
                                    toolTip="Zoom out", triggered=lambda: self.set_zoom(0.8))
        self.act_zoom_in = QAction("+", self, shortcut=QKeySequence.ZoomIn,
                                   toolTip="Zoom in", triggered=lambda: self.set_zoom(1.25))
        tb.addAction(self.act_zoom_out)
        tb.addAction(self.act_zoom_in)
        tb.addSeparator()

        self.act_view = QAction("Show redacted result", self, checkable=True,
                                triggered=self.toggle_output_view)
        tb.addAction(self.act_view)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.act_review = QAction("Save review PDF…", self, triggered=self.save_review)
        tb.addAction(self.act_review)
        self.btn_redact = QPushButton("Redact…")
        self.btn_redact.setStyleSheet(
            "QPushButton { background: #c62828; color: white; font-weight: bold;"
            " padding: 6px 18px; border-radius: 5px; }"
            "QPushButton:disabled { background: #bbb; }")
        self.btn_redact.clicked.connect(self.redact)
        tb.addWidget(self.btn_redact)
        self.btn_redact_all = QPushButton("Redact all files…")
        self.btn_redact_all.setStyleSheet(
            "QPushButton { background: #8e1c1c; color: white; font-weight: bold;"
            " padding: 6px 14px; border-radius: 5px; margin-left: 6px; }"
            "QPushButton:disabled { background: #bbb; }")
        self.btn_redact_all.clicked.connect(self.redact_all)
        tb.addWidget(self.btn_redact_all)

        # center: drop zone or page
        self.page_view = PageView()
        self.page_view.matchClicked.connect(self.toggle_match)
        self.page_view.boxDrawn.connect(self.add_box)
        self.page_view.boxRemoveRequested.connect(self.remove_box)
        self.page_view.termRegionSelected.connect(self.add_term_from_region)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.page_view)
        self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.scroll.setStyleSheet("QScrollArea { background: #5a5a5a; }")
        self.stack = QStackedWidget()
        self.stack.addWidget(DropZone())
        self.stack.addWidget(self.scroll)

        # left: matches + settings
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_matches_tab(), "Matches")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.tabs.setMinimumWidth(380)

        files_panel = QWidget()
        fl = QVBoxLayout(files_panel)
        fl.setContentsMargins(0, 0, 0, 0)
        self.files_label = QLabel("<b>Files</b>")
        fl.addWidget(self.files_label)
        self.file_list = QListWidget()
        self.file_list.setToolTip("Click a file to review it. Right-click to remove it.")
        self.file_list.currentRowChanged.connect(self._file_row_changed)
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._file_menu)
        fl.addWidget(self.file_list)
        self.files_panel = files_panel
        files_panel.hide()

        left = QSplitter(Qt.Vertical)
        left.addWidget(files_panel)
        left.addWidget(self.tabs)
        left.setStretchFactor(1, 1)
        left.setSizes([150, 700])

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        split.setSizes([430, 870])

        # bottom: result
        self.result_bar = QFrame()
        self.result_bar.setFrameShape(QFrame.StyledPanel)
        rl = QHBoxLayout(self.result_bar)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.result_file = DraggableFile()
        self.btn_open_out = QPushButton("Open", clicked=self.open_output)
        self.btn_reveal = QPushButton("Show in Finder", clicked=self.reveal_output)
        rl.addWidget(self.result_label, 1)
        rl.addWidget(self.result_file)
        rl.addWidget(self.btn_open_out)
        rl.addWidget(self.btn_reveal)
        self.result_bar.hide()

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.addWidget(split, 1)
        cl.addWidget(self.result_bar)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

    def _build_matches_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.summary = QLabel("Drop a PDF to start.")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Action", "Page", "Type", "Text"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.itemClicked.connect(self._tree_item_clicked)
        self.tree.currentItemChanged.connect(self._tree_current_changed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        lay.addWidget(self.tree, 1)

        row = QHBoxLayout()
        row.addWidget(QPushButton("Redact all", clicked=lambda: self.set_all_kept(False)))
        row.addWidget(QPushButton("Keep all", clicked=lambda: self.set_all_kept(True)))
        lay.addLayout(row)

        lay.addWidget(QLabel("Warnings:"))
        self.warnings = QListWidget()
        self.warnings.setMaximumHeight(130)
        self.warnings.setWordWrap(True)
        lay.addWidget(self.warnings)
        return w

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        path_row = QHBoxLayout()
        self.config_label = QLabel()
        self.config_label.setWordWrap(True)
        self.config_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_row.addWidget(self.config_label, 1)
        path_row.addWidget(QPushButton("Open…", clicked=self.choose_config))
        path_row.addWidget(QPushButton("Save as…", clicked=self.save_config_as))
        lay.addLayout(path_row)

        lay.addWidget(self._wrapped("<b>Terms</b> — names, addresses, employers, one per "
                                    "line. Case and punctuation don't matter."))
        self.terms_edit = QPlainTextEdit()
        lay.addWidget(self.terms_edit, 3)

        lay.addWidget(self._wrapped("<b>Never redact</b> — exact values to leave alone, one per line"))
        self.allow_edit = QPlainTextEdit()
        self.allow_edit.setMaximumHeight(70)
        lay.addWidget(self.allow_edit)

        lay.addWidget(QLabel("<b>Detectors</b>"))
        grid = QGridLayout()
        self.pattern_boxes: dict[str, QCheckBox] = {}
        for i, name in enumerate(BUILTIN_PATTERNS):
            cb = QCheckBox(PATTERN_HELP.get(name, name))
            self.pattern_boxes[name] = cb
            grid.addWidget(cb, i, 0)
        lay.addLayout(grid)

        lay.addWidget(self._wrapped("<b>Custom patterns</b> — <tt>name = regex</tt>, one per line"))
        self.custom_edit = QPlainTextEdit()
        self.custom_edit.setMaximumHeight(90)
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.Monospace)
        self.custom_edit.setFont(mono)
        lay.addWidget(self.custom_edit)

        self.btn_apply_settings = QPushButton("Save settings and rescan", clicked=self.apply_settings)
        lay.addWidget(self.btn_apply_settings)
        return w

    @staticmethod
    def _wrapped(html: str) -> QLabel:
        label = QLabel(html)
        label.setWordWrap(True)
        return label

    # config --------------------------------------------------------------

    def _load_config(self, path: Path, quiet: bool = False) -> bool:
        try:
            cfg = load_config(path) if path.exists() else Config()
        except ConfigError as e:
            if not quiet:
                QMessageBox.warning(self, "Config error", str(e))
            self.statusBar().showMessage(f"Config error: {e}")
            return False
        self.cfg = cfg
        self.config_path = path
        self.settings.setValue("config_path", str(path))
        self._config_to_form()
        return True

    def _config_to_form(self):
        exists = self.config_path.exists()
        self.config_label.setText(
            f"Config: <tt>{self.config_path}</tt>" + ("" if exists else " <i>(new)</i>"))
        self.terms_edit.setPlainText("\n".join(self.cfg.terms))
        self.allow_edit.setPlainText("\n".join(self.cfg.allow))
        for name, cb in self.pattern_boxes.items():
            cb.setChecked(self.cfg.patterns.get(name, False))
        self.custom_edit.setPlainText("\n".join(f"{k} = {v}" for k, v in self.cfg.custom.items()))

    def _form_to_config(self) -> Config:
        cfg = Config()
        cfg.terms = [t.strip() for t in self.terms_edit.toPlainText().splitlines() if t.strip()]
        cfg.allow = [a.strip() for a in self.allow_edit.toPlainText().splitlines() if a.strip()]
        cfg.patterns = {name: cb.isChecked() for name, cb in self.pattern_boxes.items()}
        for n, line in enumerate(self.custom_edit.toPlainText().splitlines(), 1):
            if not line.strip():
                continue
            name, sep, rx = line.partition("=")
            name, rx = name.strip(), rx.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name) or not rx:
                raise ConfigError(f"custom pattern line {n}: write it as name = regex")
            try:
                re.compile(rx)
            except re.error as e:
                raise ConfigError(f"custom pattern {name!r}: bad regex: {e}")
            cfg.custom[name] = rx
        cfg.boxes = list(self.cfg.boxes)
        return cfg

    def apply_settings(self) -> bool:
        try:
            cfg = self._form_to_config()
            # validates terms too
            cfg.compiled_patterns()
        except ConfigError as e:
            QMessageBox.warning(self, "Settings", str(e))
            return False
        self.cfg = cfg
        save_config(cfg, self.config_path)
        self._config_to_form()
        self.statusBar().showMessage(f"Saved {self.config_path}", 4000)
        self.cfg_version += 1  # every file's matches are now out of date
        if self.src:
            self.rescan()
        return True

    def choose_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open config", str(self.config_path.parent), "Redacter config (*.toml)")
        if path and self._load_config(Path(path)) and self.src:
            self.cfg_version += 1
            self.rescan()

    def save_config_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save config as", str(self.config_path), "Redacter config (*.toml)")
        if path:
            self.config_path = Path(path)
            self.apply_settings()

    # loading -------------------------------------------------------------

    def dragEnterEvent(self, e):
        if any(u.toLocalFile().lower().endswith((".pdf", ".toml")) for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        tomls = [p for p in paths if p.suffix.lower() == ".toml"]
        pdfs = [p for p in paths if p.suffix.lower() == ".pdf"]
        # a dropped folder: take the PDFs inside it
        for d in (p for p in paths if p.is_dir()):
            pdfs += sorted(q for q in d.iterdir() if q.suffix.lower() == ".pdf")
        if tomls and self._load_config(tomls[0]):
            self.statusBar().showMessage(f"Using config {tomls[0]}", 4000)
            if self.src and not pdfs:
                self.cfg_version += 1
                self.rescan()
        if pdfs:
            self.add_files(pdfs)

    def choose_pdf(self):
        start = str(self.src.parent) if self.src else str(Path.home())
        paths, _ = QFileDialog.getOpenFileNames(self, "Open PDFs", start, "PDF files (*.pdf)")
        if paths:
            self.add_files([Path(p) for p in paths])

    def load_pdf(self, path: Path, password: str | None = None) -> bool:
        """Add one PDF and show it."""
        return self.add_files([path], password)

    def add_files(self, paths: list[Path], password: str | None = None) -> bool:
        """Add PDFs to the file list (skipping ones already there); show the first."""
        if self.worker is not None:
            return False
        known = {e.path.resolve(): i for i, e in enumerate(self.entries)}
        first = None
        for p in paths:
            key = p.resolve()
            if key in known:
                first = known[key] if first is None else first
                continue
            self.entries.append(FileEntry(p, password))
            known[key] = len(self.entries) - 1
            first = len(self.entries) - 1 if first is None else first
        self._refresh_file_list()
        if len(paths) > 1:
            self.statusBar().showMessage(
                f"{len(self.entries)} files. Review any of them, or use Redact all files…", 6000)
        return first is not None and self.activate(first)

    def activate(self, index: int) -> bool:
        """Show entries[index], restoring the choices made for it."""
        if self.worker is not None:
            return False
        entry = self.entries[index]
        try:
            doc = open_pdf(entry.path, entry.password, prompt=False)
        except PasswordNeeded as e:
            pw, ok = QInputDialog.getText(self, "Password", f"{e}. Password:", QLineEdit.Password)
            if not ok:
                self._select_row(self.current)
                return False
            entry.password = pw
            return self.activate(index)
        except RedactError as e:
            entry.status, entry.failed = f"can't open: {e}", True
            self._refresh_file_list()
            QMessageBox.warning(self, "Can't open", str(e))
            return False
        self._store_current()
        self._close_docs()
        self.current = index
        self.src, self.password, self.doc = entry.path, entry.password, doc
        # shared objects: edits made while reviewing land in the entry
        self.kept_keys = entry.kept_keys
        self.boxes = entry.boxes
        self.page_no = entry.page_no
        self.showing_output = False
        self.act_view.setChecked(False)
        self.result_bar.hide()
        if entry.output is not None and entry.output.exists() and not entry.failed:
            self.output = entry.output
            self.out_doc = pymupdf.open(entry.output)
            self._show_saved(entry.output, entry.status)
        self.setWindowTitle(f"Redacter — {entry.path.name}")
        self.stack.setCurrentIndex(1)
        self._select_row(index)
        if entry.matches is not None and entry.scanned_version == self.cfg_version:
            self._scan_done((entry.matches, list(entry.warnings)))
        else:
            self.rescan()
        return True

    def _store_current(self):
        if self.current is not None and self.current < len(self.entries):
            self.entries[self.current].page_no = self.page_no

    def _select_row(self, row):
        self.file_list.blockSignals(True)
        self.file_list.setCurrentRow(-1 if row is None else row)
        self.file_list.blockSignals(False)

    def _file_row_changed(self, row: int):
        if 0 <= row < len(self.entries) and row != self.current:
            self.activate(row)

    def _refresh_file_list(self):
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for e in self.entries:
            self.file_list.addItem(f"{e.path.name}  —  {e.status}")
            item = self.file_list.item(self.file_list.count() - 1)
            item.setToolTip(str(e.path))
            if e.failed:
                item.setForeground(QBrush(RED))
            elif e.output is not None:
                item.setForeground(QBrush(GREEN))
        if self.current is not None:
            self.file_list.setCurrentRow(self.current)
        self.file_list.blockSignals(False)
        n = len(self.entries)
        self.files_label.setText(f"<b>Files</b> ({n})")
        self.files_panel.setVisible(n > 0)
        self._update_enabled()

    def _file_menu(self, pos):
        item = self.file_list.itemAt(pos)
        if item is None or self.worker is not None:
            return
        row = self.file_list.row(item)
        menu = QMenu(self)
        remove = menu.addAction("Remove from list")
        if menu.exec(self.file_list.viewport().mapToGlobal(pos)) == remove:
            self.remove_file(row)

    def remove_file(self, row: int):
        del self.entries[row]
        if self.current == row:
            self.current = None
            self._close_docs()
            self.matches = []
            if self.entries:
                self._refresh_file_list()
                self.activate(min(row, len(self.entries) - 1))
                return
            self.src = self.doc = None
            self.kept_keys, self.boxes = set(), []
            self.tree.clear()
            self.warnings.clear()
            self.result_bar.hide()
            self.summary.setText("Drop a PDF to start.")
            self.setWindowTitle("Redacter")
            self.stack.setCurrentIndex(0)
        elif self.current is not None and self.current > row:
            self.current -= 1
        self._refresh_file_list()

    def _close_docs(self):
        self.page_view.set_page(None)
        self._shown = None
        for d in (self.doc, self.out_doc):
            if d is not None:
                d.close()
        self.doc = self.out_doc = None
        self.output = None

    def rescan(self):
        if self.doc is None:
            return
        doc, cfg = self.doc, self.cfg

        def work(progress):
            matches = find_matches(doc, cfg, progress)
            progress("Checking pages for images", 0, 1)
            return matches, page_warnings(doc)

        self.run_task("Scanning", work, self._scan_done)

    def _scan_done(self, result):
        self.matches, warnings = result
        if self.current is not None:
            entry = self.entries[self.current]
            entry.matches, entry.warnings = self.matches, list(warnings)
            entry.scanned_version = self.cfg_version
            if entry.output is None and not entry.failed:
                entry.status = f"{len(self.matches)} matches"
            self._refresh_file_list()
        if not self.cfg.terms:
            warnings.insert(0, "No terms set, so names and addresses will NOT be "
                               "redacted. Add them in Settings.")
        self.warnings.clear()
        self.warnings.addItems(warnings or ["None"])
        self._invalidate_output()
        self._fill_tree()
        self.go_page(self.page_no)
        self.statusBar().showMessage(f"Found {len(self.matches)} matches", 4000)

    # matches & boxes -----------------------------------------------------

    def kept_ids(self) -> set[int]:
        return {m.id for m in self.matches if match_key(m) in self.kept_keys}

    def _fill_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        kept = self.kept_ids()
        for m in self.matches:
            it = QTreeWidgetItem(["", str(m.page + 1), m.kind, " ".join(m.text.split())])
            it.setData(0, Qt.UserRole, ("match", m.id))
            self._show_state(it, m.id in kept)
            it.setToolTip(3, m.context)
            self.tree.addTopLevelItem(it)
        for i, b in enumerate(self.boxes):
            it = QTreeWidgetItem(["Box", str(b.page), "manual box", "right-click to remove"])
            it.setData(0, Qt.UserRole, ("box", i))
            it.setForeground(0, QBrush(BLUE))
            it.setForeground(2, QBrush(BLUE))
            self.tree.addTopLevelItem(it)
        for col in range(3):
            self.tree.resizeColumnToContents(col)
        self.tree.blockSignals(False)
        self._update_summary()

    def _update_summary(self):
        kept = len(self.kept_ids())
        n = len(self.matches)
        boxes = len(self.boxes) + len(self.cfg.boxes)
        self.summary.setText(
            f"<b>{n - kept}</b> of {n} matches will be redacted"
            + (f", <b>{kept}</b> kept" if kept else "")
            + (f", plus <b>{boxes}</b> box(es)" if boxes else "")
            + ".<br><span style='color:#888'>Click a match on the page, or its Redact/Keep "
              "label here, to switch it. Use Draw box / Add term for anything missed.</span>")
        self._update_enabled()

    @staticmethod
    def _show_state(item: QTreeWidgetItem, kept: bool):
        # A coloured word rather than a checkbox: checkboxes in item views
        # are nearly invisible in macOS dark mode.
        item.setText(0, "Keep" if kept else "Redact")
        item.setForeground(0, QBrush(GREEN if kept else RED))
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)
        item.setToolTip(0, "Click to " + ("redact this" if kept else "keep this unredacted"))

    def _tree_item_clicked(self, item: QTreeWidgetItem, col: int):
        kind, ident = item.data(0, Qt.UserRole)
        if kind != "match" or col != 0:
            return
        key = match_key(self._match(ident))
        kept = key not in self.kept_keys
        (self.kept_keys.add if kept else self.kept_keys.discard)(key)
        self._show_state(item, kept)
        self._invalidate_output()
        self._update_summary()
        self._refresh_page()

    def _tree_current_changed(self, item, _prev):
        if item is None:
            return
        kind, ident = item.data(0, Qt.UserRole)
        if kind == "match":
            m = self._match(ident)
            self.page_view.highlight = m.id
            self.go_page(m.page)
            self.scroll.ensureVisible(*self._center_of(m.rects[0]), 200, 200)
        else:
            self.go_page(self.boxes[ident].page - 1)

    def _center_of(self, r: pymupdf.Rect):
        v = self.page_view.to_view(r)
        return int(v.center().x()), int(v.center().y())

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        kind, ident = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        if kind == "box":
            if menu.addAction("Remove box") == menu.exec(self.tree.viewport().mapToGlobal(pos)):
                self.remove_box(ident)
        else:
            m = self._match(ident)
            allow = menu.addAction(f"Never redact “{' '.join(m.text.split())}”")
            if menu.exec(self.tree.viewport().mapToGlobal(pos)) == allow:
                self.allow_edit.appendPlainText(" ".join(m.text.split()))
                self.apply_settings()

    def _match(self, ident: int) -> Match:
        return next(m for m in self.matches if m.id == ident)

    def toggle_match(self, ident: int):
        key = match_key(self._match(ident))
        if key in self.kept_keys:
            self.kept_keys.remove(key)
        else:
            self.kept_keys.add(key)
        self._invalidate_output()
        self._fill_tree()
        self._refresh_page()

    def set_all_kept(self, kept: bool):
        self.kept_keys.clear()
        if kept:
            self.kept_keys.update(match_key(m) for m in self.matches)
        self._invalidate_output()
        self._fill_tree()
        self._refresh_page()

    def add_box(self, rect: pymupdf.Rect):
        if rect.width < 2 or rect.height < 2:
            return
        self.boxes.append(Box(self.page_no + 1, tuple(round(v, 1) for v in rect)))
        self._invalidate_output()
        self._fill_tree()
        self._refresh_page()

    def remove_box(self, index: int):
        if index < 0:
            QMessageBox.information(
                self, "Box from config",
                f"This box is set in {self.config_path.name}; remove it there.")
            return
        del self.boxes[index]
        self._invalidate_output()
        self._fill_tree()
        self._refresh_page()

    def add_term_from_region(self, rect: pymupdf.Rect):
        pt = page_text(self.doc[self.page_no])
        inside = {i for i, ch in enumerate(pt.chars)
                  if ch.bbox is not None and rect.contains((ch.bbox.tl + ch.bbox.br) / 2)}
        picked = ""
        if inside:
            lo, hi = min(inside), max(inside)
            # keep the selected characters plus the separators between them
            picked = "".join(ch.c for i, ch in enumerate(pt.chars[lo:hi + 1], lo)
                             if i in inside or ch.bbox is None)
        picked = " ".join(picked.split())
        text, ok = QInputDialog.getText(
            self, "Add term",
            "Redact this text everywhere in every document\n(saved to your settings):",
            QLineEdit.Normal, picked)
        text = " ".join(text.split())
        if ok and text:
            if text.casefold() not in {t.casefold() for t in self.cfg.terms}:
                self.terms_edit.appendPlainText(text)
            self.apply_settings()

    # navigation & display ------------------------------------------------

    def set_mode(self, mode: str):
        self.page_view.mode = mode
        self.mode_actions[mode].setChecked(True)

    def set_zoom(self, factor: float):
        self.page_view.zoom = max(0.4, min(4.0, self.page_view.zoom * factor))
        self._refresh_page(rerender=True)

    def go_page(self, n: int):
        doc = self._display_doc()
        if doc is None:
            return
        self.page_no = max(0, min(doc.page_count - 1, n))
        self._refresh_page(rerender=True)

    def _display_doc(self):
        return self.out_doc if self.showing_output and self.out_doc else self.doc

    def _refresh_page(self, rerender: bool = False):
        doc = self._display_doc()
        if doc is None:
            return
        pv = self.page_view
        shown = (id(doc), self.page_no)
        if rerender or getattr(self, "_shown", None) != shown:
            pv.set_page(doc[self.page_no], overlays=not self.showing_output)
            self._shown = shown
        pv.matches = [m for m in self.matches if m.page == self.page_no]
        pv.kept = self.kept_ids()
        pv.boxes = [b for b in self.boxes if b.page == self.page_no + 1] + \
                   [b for b in self.cfg.boxes if b.page == self.page_no + 1]
        pv.box_indices = [i for i, b in enumerate(self.boxes) if b.page == self.page_no + 1] + \
                         [-1] * sum(b.page == self.page_no + 1 for b in self.cfg.boxes)
        pv.update()
        on_page = len(pv.matches)
        self.page_label.setText(
            f"  Page {self.page_no + 1} / {doc.page_count}"
            + ("  (redacted)" if self.showing_output else f"  · {on_page} here") + "  ")
        self._update_enabled()

    def toggle_output_view(self, on: bool):
        self.showing_output = on and self.out_doc is not None
        self._refresh_page(rerender=True)

    def _invalidate_output(self):
        """The redacted file no longer reflects the current choices."""
        if self.out_doc is not None:
            self.out_doc.close()
            self.out_doc = None
        if self.showing_output:
            self.showing_output = False
            self.act_view.setChecked(False)
        if self.output is not None and self.result_bar.isVisible():
            self.result_label.setText(
                "<span style='color:#a15c00'>You changed something since redacting. "
                "Redact again to update the file.</span>")

    def _update_enabled(self):
        loaded = self.doc is not None
        for a in (self.act_prev, self.act_next, self.act_zoom_in, self.act_zoom_out,
                  self.act_review, *self.mode_actions.values()):
            a.setEnabled(loaded)
        self.btn_redact.setEnabled(loaded)
        self.btn_redact_all.setEnabled(len(self.entries) > 1)
        self.btn_redact_all.setVisible(len(self.entries) > 1)
        self.act_view.setEnabled(self.out_doc is not None)

    # outputs -------------------------------------------------------------

    def default_output(self) -> Path:
        return self.src.with_name(self.src.stem + ".redacted.pdf")

    def redact(self):
        if self.src is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save redacted PDF", str(self.default_output()), "PDF files (*.pdf)")
        if path:
            self.redact_to(Path(path))

    def redact_to(self, out: Path):
        """Redact to out in the background; the result shows when done."""
        if out.suffix.lower() != ".pdf":
            out = out.with_suffix(".pdf")
        if self.out_doc is not None:  # may be the file being overwritten
            self.out_doc.close()
            self.out_doc = None
            self.showing_output = False
        args = (self.src, out, self.cfg, self.matches, self.kept_ids(),
                list(self.boxes), self.password)

        def work(progress):
            return redact_file(*args, progress=progress)

        self.run_task("Redacting", work, lambda result: self._redact_done(out, result))

    def _redact_done(self, out: Path, result):
        self.result_bar.show()
        v = result.verification
        entry = self.entries[self.current] if self.current is not None else None
        if entry is not None:
            entry.failed = not v.ok
            entry.output = out if v.ok else None
            entry.status = "✓ redacted" if v.ok else "✗ verification failed"
            self._refresh_file_list()
        if not v.ok:
            self.result_file.hide()
            self.btn_open_out.hide()
            self.btn_reveal.hide()
            self.result_label.setText(
                "<b style='color:#c62828'>Verification failed — nothing was saved.</b> "
                "Some redacted values are still in the file:<br>"
                + "<br>".join(f"• {leak}" for leak in v.leaks[:8])
                + ("<br>…" if len(v.leaks) > 8 else ""))
            self.statusBar().showMessage("Verification failed", 6000)
            return

        self.output = out
        self.out_doc = pymupdf.open(out)
        kept = len(result.skipped)
        self._show_saved(
            out, f"{len(result.applied)} matches"
            + (f" and {result.boxes} box(es)" if result.boxes else "")
            + " removed" + (f", {kept} kept" if kept else ""))
        self.act_view.setEnabled(True)
        self.act_view.setChecked(True)
        self.toggle_output_view(True)
        self.statusBar().showMessage(f"Saved {out}", 6000)

    def _show_saved(self, out: Path, detail: str):
        self.result_bar.show()
        self.result_file.set_path(out)
        for w in (self.result_file, self.btn_open_out, self.btn_reveal):
            w.show()
        self.result_label.setText(
            f"<b style='color:#2e7d32'>✓ Redacted and verified.</b> {detail}. "
            "Check pages with images yourself before sharing.")

    def redact_all(self):
        if not self.entries:
            return
        start = str(self.entries[0].path.parent)
        folder = QFileDialog.getExistingDirectory(
            self, "Save redacted PDFs into folder", start)
        if folder:
            self.redact_all_to(Path(folder))

    def redact_all_to(self, folder: Path):
        """Redact every file in the list into folder, in parallel processes."""
        self._store_current()
        outs = output_paths([e.path for e in self.entries], folder)
        jobs = [BatchJob(i, e.path, out, e.password, set(e.kept_keys), list(e.boxes))
                for i, (e, out) in enumerate(zip(self.entries, outs))]
        cfg = self.cfg
        if self.out_doc is not None:  # its file may be overwritten
            self.out_doc.close()
            self.out_doc = None
            self.showing_output = False
        self.run_task("Redacting all files", lambda progress: run_batch(jobs, cfg, progress),
                      lambda results: self._batch_done(folder, results))

    def _batch_done(self, folder: Path, results):
        good = []
        problems = []
        for r in results:
            entry = self.entries[r.index]
            entry.failed = not r.ok
            if r.ok:
                entry.output = r.out
                entry.status = f"✓ redacted ({r.applied} removed)"
                good.append(r.out)
            else:
                entry.output = None
                reason = r.error or "verification failed: " + "; ".join(r.leaks[:2])
                entry.status = f"✗ {reason}"
                problems.append(f"• {entry.path.name}: {reason}")
        self._refresh_file_list()
        self.result_bar.show()
        self.output = folder
        if good:
            self.result_file.set_paths(good)
            self.result_file.show()
        else:
            self.result_file.hide()
        self.btn_open_out.hide()
        self.btn_reveal.setVisible(bool(good))
        head = (f"<b style='color:#2e7d32'>✓ {len(good)} of {len(results)} files redacted "
                f"and verified</b> into {folder}." if good else
                "<b style='color:#c62828'>No files were redacted.</b>")
        if problems:
            head += "<br><b style='color:#c62828'>Not redacted:</b><br>" + "<br>".join(problems[:6])
            if len(problems) > 6:
                head += "<br>…"
        self.result_label.setText(head + "<br>Check pages with images yourself before sharing.")
        cur = self.entries[self.current] if self.current is not None else None
        if cur is not None and cur.output is not None:
            self.out_doc = pymupdf.open(cur.output)
            self.act_view.setEnabled(True)
        self.statusBar().showMessage(f"Batch done: {len(good)} of {len(results)} redacted", 8000)

    # background tasks ----------------------------------------------------

    def run_task(self, title: str, fn, on_done):
        """Run fn(progress) in a worker with a progress dialog, then on_done."""
        if self.worker is not None:
            return
        self.centralWidget().setEnabled(False)
        self.toolbar.setEnabled(False)
        dlg = QProgressDialog(f"{title}…", None, 0, 0, self)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(360)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        self.progress_dialog = dlg

        def progressed(what, done, total):
            dlg.setMaximum(max(total, 1) if total > 1 else 0)  # 0 = busy bar
            dlg.setValue(done)
            dlg.setLabelText(f"{what}… {done + 1} of {total}" if total > 1 else f"{what}…")
            self.statusBar().showMessage(dlg.labelText())

        def finish():
            dlg.close()
            self.progress_dialog = None
            self.worker = None
            self.centralWidget().setEnabled(True)
            self.toolbar.setEnabled(True)
            self._update_enabled()

        def succeeded(result):
            finish()
            on_done(result)

        def failed(error):
            finish()
            QMessageBox.warning(self, f"{title} failed", str(error))
            self.statusBar().showMessage(f"{title} failed", 6000)

        worker = Worker(fn)
        worker.progressed.connect(progressed)
        worker.succeeded.connect(succeeded)
        worker.failed.connect(failed)
        worker.finished.connect(worker.deleteLater)
        self.worker = worker
        worker.start()

    def wait_idle(self, timeout_ms: int = 60000) -> None:
        """Block (processing events) until the current task is done."""
        loop = QEventLoop()
        timer = QTimer()
        timer.timeout.connect(lambda: loop.quit() if self.worker is None else None)
        timer.start(20)
        QTimer.singleShot(timeout_ms, loop.quit)
        if self.worker is not None:
            loop.exec()
        timer.stop()

    def save_review(self):
        if self.src is None:
            return
        default = self.src.with_name(self.src.stem + ".review.pdf")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save review copy (NOT redacted)", str(default), "PDF files (*.pdf)")
        if not path:
            return
        out = Path(path)
        if out.resolve() == self.src.resolve():
            QMessageBox.warning(self, "Review copy", "Choose a different file than the original.")
            return
        write_review_pdf(self.src, out, self.matches, self.kept_ids(),
                         [*self.cfg.boxes, *self.boxes], self.password)
        QMessageBox.information(
            self, "Review copy saved",
            f"Saved {out.name}.\n\nIt shows everything UNREDACTED with the planned "
            "redactions outlined (red = redact, green = keep, blue = box). "
            "Delete it when you're done.")

    def open_output(self):
        if self.output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output)))

    def reveal_output(self):
        if not self.output:
            return
        if self.output.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output)))
            return
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(self.output)], check=False)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output.parent)))

    def output_moved(self, old: list[Path]):
        self.result_label.setText("Moved the redacted PDF(s) out of the folder.")
        self.btn_open_out.hide()
        self.btn_reveal.hide()

    def closeEvent(self, e):
        if self.worker is not None:  # let it finish; its document is in use
            self.wait_idle()
        self._close_docs()
        super().closeEvent(e)


class App(QApplication):
    """Opens PDFs that macOS hands over (dropped on the Dock icon, Open With)."""

    window: MainWindow | None = None
    pending: list[Path] = []

    def event(self, e):
        if e.type() == QEvent.FileOpen:
            path = Path(e.file())
            if self.window is not None:
                self.window.load_pdf(path)
            else:
                self.pending.append(path)
            return True
        return super().event(e)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    app = QApplication.instance() or App(argv)
    app.setApplicationName("Redacter")
    win = MainWindow()
    win.show()
    if isinstance(app, App):
        app.window = win
        for path in app.pending:
            win.load_pdf(path)
    pdfs = [Path(a) for a in argv[1:] if a.lower().endswith(".pdf")]
    if pdfs:
        win.load_pdf(pdfs[0])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
