# Screen Monitor — Installation, Backup and Recovery

Version: **1.0.0** (schema v2)
Target: Windows 10/11 desktop, distributed as a single `.exe`.

---

## 1. Installation

### 1.1 End user (packaged build)

1. Copy `dist\ScreenMonitor.exe` to any permanent folder, for example
   `C:\Tools\ScreenMonitor\`. Do **not** run it from inside a zip file or from a
   temporary extraction folder — the app writes a small per-user DB path on
   first launch and Windows can delete temp folders at any time.
2. Double-click `ScreenMonitor.exe`. No installer is required and no registry
   keys are written.
3. First launch creates the database (schema v2, 14 tables, 20 default settings,
   the nine default numerical groups) at:

   ```
   %APPDATA%\ScreenMonitor\screen_monitor.db
   ```

   This is a stable, per-user, writable location that a later upgrade of the
   `.exe` never overwrites.
4. Optional: right-click the exe → *Send to* → *Desktop (create shortcut)*.

**Requirements**

| Item | Detail |
|---|---|
| OS | Windows 10 or 11, 64-bit |
| Runtime | None — Python, PySide6, RapidOCR and onnxruntime are bundled |
| Disk | ~150 MB for the exe, plus the database (a few MB) |
| Permissions | Normal user. Screen-capture of the desktop is done with `mss`; no admin rights needed |
| Display | Any DPI. Coordinates are stored in virtual-desktop logical space and re-validated against the current display signature |

The app is fully offline. OCR runs locally through RapidOCR/ONNX; nothing is
sent to the network.

### 1.2 Developer (from source)

```powershell
python -m pip install -r requirements.txt
python launch.py
```

Python 3.12 is required. In development mode the database resolves to
`<repo>\screen_monitor.db` instead of `%APPDATA%`.

Set `SCREEN_MONITOR_DB` to any path to override the database location — this is
also how the automated test suite isolates itself:

```powershell
$env:SCREEN_MONITOR_DB = "$PWD\_scratch\smoke.db"
python launch.py
```

### 1.3 Building the executable

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean ScreenMonitor.spec
```

Output: `dist\ScreenMonitor.exe` (one-file, windowed, UPX disabled).
Build-time intermediates are written to `build\` and can be deleted at any
time; they are never shipped and contain no user data.

---

## 2. What the app does on first run

1. Creates/opens the database and applies the schema-2 migration.
2. Copies any legacy `detections` rows into `readings` **once**, preserving the
   original region identifier in `legacy_region_id`. Legacy rows are never
   deleted or rewritten. The one-shot marker is the
   `legacy_detections_migrated` row in `schema_meta`.
3. Seeds the two region slots (`region-A` active, `region-B` reserved) and the
   nine default numerical groups.
4. Starts with **auto-click disabled and dry-run enabled**. Real clicking has to
   be turned on deliberately in *Automation*.

---

## 3. Backup

Two supported methods. Both produce a consistent, restorable copy.

### 3.1 From inside the app (recommended)

*History* page → **Backup Database** → choose a destination `.db` file.
This uses SQLite's online backup API, so it is safe to run while monitoring is
active and it includes everything committed so far.

### 3.2 From the file system

1. Stop the app (or at least stop monitoring from the *Dashboard*).
2. Copy all three files together — the database uses WAL mode:

   ```
   %APPDATA%\ScreenMonitor\screen_monitor.db
   %APPDATA%\ScreenMonitor\screen_monitor.db-wal
   %APPDATA%\ScreenMonitor\screen_monitor.db-shm
   ```

   The `.db` file alone is usually sufficient after a clean shutdown (the WAL is
   checkpointed on close), but copying all three is always safe.

The repository also keeps an offline checkpoint of the pre-upgrade database at
`backups\screen_monitor_pre_phase1.db`.

### 3.3 CSV export

*History* page → **Export CSV** streams the whole readings table to a CSV file
in batches (10 columns: Reading ID, Canonical Value, Original Text, Group,
Region, Confidence (%), Capture Timestamp, Processing Timestamp, Session ID,
Source). Useful for analysis and as an independent, human-readable archive.

---

## 4. Recovery

### 4.1 Restore a backup

1. Close the app completely (check Task Manager for a lingering
   `ScreenMonitor.exe`).
2. Move the current files aside rather than deleting them:

   ```powershell
   $dir = "$env:APPDATA\ScreenMonitor"
   New-Item -ItemType Directory -Force "$dir\broken" | Out-Null
   Move-Item "$dir\screen_monitor.db*" "$dir\broken\"
   ```
3. Copy the backup in as `screen_monitor.db` (drop any stale `-wal`/`-shm`
   files next to it).
4. Start the app. It will detect schema v2 and run the normal startup path.

### 4.2 If the database will not open

The app refuses to delete or reset the database. Instead:

1. Read the error shown at startup / in the log.
2. Inspect the file read-only:

   ```powershell
   python -c "import sqlite3;c=sqlite3.connect(r'%APPDATA%\ScreenMonitor\screen_monitor.db');print(c.execute('PRAGMA integrity_check').fetchone())"
   ```
3. If SQLite reports corruption, try to salvage before restoring:

   ```powershell
   sqlite3 screen_monitor.db ".recover" | sqlite3 salvaged.db
   ```

   (Requires the `sqlite3` CLI.) Then use `salvaged.db` as in §4.1.
4. If the file is unreadable, restore the most recent backup from §3. If no
   backup exists, rename the database aside, start the app to get a fresh empty
   schema-v2 database, and re-import historical data from a CSV export.

### 4.3 Rolling back to the previous build

1. Keep the previous `ScreenMonitor.exe` (rename it, e.g.
   `ScreenMonitor-1.0.0.exe`) before deploying a new one.
2. Schema-2 changes are **additive only**: no v1 table is dropped, truncated or
   rewritten, and `detections` is preserved verbatim. Rolling the exe back to a
   v1 build therefore still works against the same file.
3. If you need a byte-for-byte rollback, restore the pre-upgrade checkpoint from
   `backups\`.

### 4.4 "Recalculate App Brain" went wrong

Rebuilds are published atomically: a new model version is only marked `VALID`
after the chronological replay finishes. If a rebuild fails it is marked
`FAILED` and the previously valid model stays in place, so no data is lost.
Raw readings and click history are never modified by a rebuild — re-run
*Recalculate App Brain* from the *Learning* page to try again.

---

## 5. Upgrade

1. Close the running app.
2. Replace `ScreenMonitor.exe` with the new build (keep the old one renamed as
   described in §4.3).
3. Start the app. The database in `%APPDATA%` is reused as-is; only additive
   migrations run.
4. Take a backup (§3) before and after every upgrade.

---

## 6. Uninstall

The app is portable. To remove it:

1. Close the app.
2. Delete the folder containing `ScreenMonitor.exe`.
3. User data is **not** removed automatically. To delete it as well, remove
   `%APPDATA%\ScreenMonitor\` — this destroys all readings, groups, learned
   patterns and click history, so export a CSV and/or take a backup first.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Nothing is saved | Reading outcomes are shown on the *Dashboard* and the raw rejections on *Activity* → diagnostics. `OCR_FAILURE` means the OCR engine errored — it is never stored as a zero. Check the region actually contains the number. |
| "Auto-click locked: N / 1,000 valid Region A readings." | By design. The 1,000-record Region A gate is mandatory and non-configurable; it is enforced inside the automation execution path. Collect more readings. |
| Clicks blocked with `DISPLAY_CALIBRATION_CHANGED` | The display layout or DPI changed since the click target was calibrated. Re-calibrate the target on the *Automation* page. |
| Clicks blocked with `INVALID_TARGET_COORDINATES` | No click target has been set, or it lies outside the current virtual desktop. Use *Use Current Cursor Position* to set one. |
| Region B does nothing | Region B is a reserved, inactive slot by design; it never feeds readings, learning or clicks. |
| Window opens but the tray icon is missing | The app can run minimised to the tray; if the tray is unavailable, closing the window exits the app. |
| Where is the log? | Logging goes to stdout for the dev build. Launch from a console (`launch.bat`) to see it. |

---

## 8. Data locations summary

| Artifact | Location |
|---|---|
| Database (packaged build) | `%APPDATA%\ScreenMonitor\screen_monitor.db` |
| Database (dev build) | `<repo>\screen_monitor.db` |
| Database override | `%SCREEN_MONITOR_DB%` |
| Pre-upgrade checkpoint | `backups\screen_monitor_pre_phase1.db` |
| Packaged executable | `dist\ScreenMonitor.exe` |
| Build intermediates (disposable) | `build\` |
