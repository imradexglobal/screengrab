# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Screen Monitor Windows desktop application.

Build with:

    python -m PyInstaller --noconfirm --clean ScreenMonitor.spec

The result is a directory ``dist\\ScreenMonitor`` containing the EXE and DLLs.
This "one-directory" layout is less likely to be blocked by Smart App Control
than a "one-file" executable, as it does not extract to %TEMP% at runtime.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
hiddenimports = []

# --- UI: arrow pictograms referenced by APP_STYLESHEET ----------------------
datas += [
    ("src/ui/assets/arrow_up.svg", "src/ui/assets"),
    ("src/ui/assets/arrow_down.svg", "src/ui/assets"),
]

# --- OCR engine: bundled models + layout configuration ---------------------
datas += collect_data_files("rapidocr_onnxruntime")
hiddenimports += collect_submodules("rapidocr_onnxruntime")

# --- ONNX Runtime: native execution providers are loaded dynamically -------
hiddenimports += collect_submodules("onnxruntime")

# --- Explicitly imported at runtime, invisible to static analysis ----------
hiddenimports += [
    "sqlite3",
    "mss",
    "numpy",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "src.automation.clicker",
    "src.capture.screen_capture",
    "src.config.ranges",
    "src.config.repository",
    "src.database.init_db",
    "src.learning.patterns",
    "src.learning.rebuild",
    "src.learning.repository",
    "src.monitor.engine",
    "src.monitor.pipeline",
    "src.monitor.region_manager",
    "src.ocr.engine",
    "src.ui.main_window",
]

a = Analysis(
    ["launch.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "pytest",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScreenMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ScreenMonitor",
)
