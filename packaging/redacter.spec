# PyInstaller spec for Redacter.app. Build it with scripts/build-dmg.sh.
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

REPO = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[REPO],
    datas=[(os.path.join(REPO, "redacter", "redact.example.toml"), "redacter")],
    hiddenimports=collect_submodules("pymupdf") + ["redacter.gui", "redacter.batch"],
    # Only QtCore/QtGui/QtWidgets are used; keep the rest of Qt out.
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
              "PySide6.QtPdf", "PySide6.QtNetwork", "PySide6.QtSql"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Redacter",
          console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Redacter")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Redacter.app",
        bundle_identifier="local.redacter",
        info_plist={
            "CFBundleDisplayName": "Redacter",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            # Lets PDFs be dropped on the Dock icon / opened with Redacter.
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "PDF document",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["com.adobe.pdf"],
            }],
        },
    )
