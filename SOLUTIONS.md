# Screen Monitor - Issue Diagnosis & Resolution

## Why the App Window Did Not Appear

When running the batch files (`run.bat`, `run_app.bat`, etc.), a command prompt window opened and either closed, sat idle, or showed errors without displaying an application window.

This happened due to multiple compounding issues:

1. **No GUI Window Was Instantiated in `src/main.py`**:
   `src/main.py` initialized `QApplication` and called `app.exec()`, but never created or showed an instance of `MainWindow` (`# Simple console-based demonstration - in full app, this would show the GUI`). It entered the Qt event loop with zero visible windows, leaving the process blocking indefinitely in the console.

2. **Syntax Errors in `src/ui/main_window.py`**:
   `src/ui/main_window.py` contained syntax errors (e.g., `for i in reversed range(...)`), missing PySide6 imports (`QAbstractItemView`, `QRadioButton`), and invalid relative imports (`from ...src.database.init_db`).

3. **`ModuleNotFoundError: No module named 'src'` on Launch**:
   When launching via Python without `src` explicitly in `sys.path`, `src` could not be resolved. Batch scripts had formatting issues (`%~dp0%` with trailing `%`, unquoted `C:\Program Files`, or missing `PYTHONPATH`).

4. **Engine & OCR Initialization**:
   - `sqlite3` was used but not imported in `src/monitor/engine.py`.
   - `rapidocr-paddle` lacked the underlying `paddle` package; replaced with `rapidocr-onnxruntime` which runs lightweight and fast without paddle dependencies.
   - Database path resolution was hardened to handle permission constraints cleanly.

---

## What Was Fixed

1. **`src/main.py`**:
   - Automatically injects the repository root into `sys.path` dynamically.
   - Instantiates `MainWindow`, calls `window.show()`, and starts the Qt event loop.

2. **`src/ui/main_window.py`**:
   - Fixed all syntax errors, import paths, and component bindings.
   - Built a complete, modern dashboard with:
     - Real-time monitoring controls (Start, Pause, Resume, Stop).
     - Live statistics and filter/suppression counters.
     - Live detection feed updating dynamically as numbers are read.
     - Full region management (Add, Edit, Delete, Test OCR on selected region).
     - Detections history table with filtering and CSV export.
     - Settings panel for scan intervals, OCR confidence threshold, and monitoring mode.

3. **Batch Launchers (`run.bat`, `run_app.bat`, `launch.bat`, `launch_gui.bat`, `fix_path.bat`)**:
   - Unified to use a robust Python detection routine (checks `.venv`, system Python 3.12, system `python`, and `py` launcher).
   - Sets correct `PYTHONPATH` and maintains pause on failure so errors are never lost.

4. **OCR & Database Engine**:
   - Added `import sqlite3` in `src/monitor/engine.py`.
   - Installed `rapidocr-onnxruntime` and updated `src/ocr/engine.py` to support onnxruntime seamlessly.
   - Fixed confidence percentage scaling so detections match thresholds accurately.
   - Supported both full-screen scan mode and custom region monitoring.

---

## How to Run the App

Double-click `run.bat` in the project root:
```batch
run.bat
```
Alternatively:
```powershell
python src\main.py
```

## Automated Test Verification

All 29 automated tests pass:
```bash
python -m pytest
```
- Database duplicate suppression & WAL mode tests: 6 passed
- Numeric normalization (integers, decimals, currency, negative, etc.): 12 passed
- OCR engine detection & confidence filtering: 8 passed
- GUI window, dashboard, and settings initialization: 3 passed
