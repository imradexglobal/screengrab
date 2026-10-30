"""Database initialisation, schema versioning and migration for Screen Monitor.

Schema history
--------------
v1  settings, regions, detections (UNIQUE on region_id+canonical_value),
    scan_sessions.  ``detections`` is kept verbatim after migration so that
    existing test suites and any external tooling that reads it keep working.

v2  Adds the tables required by the upgraded application:

    readings               - the single source of truth for valid readings
                             (no UNIQUE constraint: "save every reading" must
                             be able to store repeated values)
    groups                 - editable numerical categories (the nine defaults)
    range_config_versions  - versioned snapshots of the range configuration
    pattern_stats          - derived learning counters (versioned by model)
    predictions            - one row per prediction event, with the confidence
                             and evidence *as they were at creation time*
    click_audit            - every dispatch attempt, blocked or not
    model_versions         - derived-model lifecycle (BUILDING/VALID/FAILED)
    diagnostics            - rejected readings, capture/OCR errors, etc.
    schema_meta            - schema version + one-shot migration markers

v3  Adds ``region_b_readings``: Region B may now be activated and its
    readings are stored in their *own* table, which mirrors ``readings``
    column for column.  Region B data is store-only - it never enters the
    Region A history, the learning model, the statistics or the automation
    gate, and it can never be read back into ``readings``.

Nothing in v1 or v2 is dropped, truncated or rewritten.  Legacy ``detections``
rows are *copied* into ``readings`` (source='migrated') preserving their
original region identifier in ``legacy_region_id``.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

#: Fixed identifiers of the two region slots (spec section 5).
REGION_A_ID = "region-A"
REGION_B_ID = "region-B"


def _resolve_db_path() -> Path:
    """Resolve the production database location.

    Priority:
      1. ``SCREEN_MONITOR_DB`` environment variable (used by tests/tools).
      2. When frozen (packaged .exe) a stable writable per-user directory
         ``%APPDATA%\\ScreenMonitor\\screen_monitor.db`` so that an
         application upgrade never overwrites user data.
      3. Development: the repository root.
    """
    env = os.environ.get("SCREEN_MONITOR_DB")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "ScreenMonitor" / "screen_monitor.db"
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "screen_monitor.db"


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = _resolve_db_path()
APP_DATA_DIR = DB_PATH.parent


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Get a configured SQLite connection with WAL mode and proper settings.

    ``check_same_thread=False`` is required because the monitoring engine shares
    one connection between the GUI thread and its background scan thread.  All
    access to that shared connection is serialised by the engine's model lock,
    and WAL plus a 30s busy timeout keep concurrent readers safe.
    """
    db_path = Path(db_path)
    if db_path.parent:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    if not _table_exists(conn, "schema_meta"):
        return None
    cur = conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# v1 tables (kept byte-for-byte compatible)
# ---------------------------------------------------------------------------

_V1_TABLES = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regions (
    region_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    monitor_id TEXT NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detections (
    detection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id TEXT NOT NULL,
    canonical_value TEXT NOT NULL,
    original_text TEXT,
    ocr_confidence REAL,
    screen_x INTEGER,
    screen_y INTEGER,
    monitor_id TEXT,
    capture_timestamp TEXT NOT NULL,
    processing_timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    UNIQUE (region_id, canonical_value)
);

CREATE TABLE IF NOT EXISTS scan_sessions (
    session_id TEXT PRIMARY KEY,
    start_timestamp TEXT NOT NULL,
    end_timestamp TEXT,
    scans_attempted INTEGER NOT NULL DEFAULT 0,
    scans_completed INTEGER NOT NULL DEFAULT 0,
    new_values_saved INTEGER NOT NULL DEFAULT 0,
    duplicates_skipped INTEGER NOT NULL DEFAULT 0,
    low_confidence_skipped INTEGER NOT NULL DEFAULT 0,
    invalid_candidates_skipped INTEGER NOT NULL DEFAULT 0,
    operational_errors INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_detections_region_timestamp
    ON detections (region_id, capture_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_detections_canonical
    ON detections (canonical_value);
CREATE INDEX IF NOT EXISTS idx_detections_capture_ts
    ON detections (capture_timestamp DESC);
"""

# ---------------------------------------------------------------------------
# v2 tables
# ---------------------------------------------------------------------------

_V2_TABLES = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id TEXT NOT NULL,
    canonical_value TEXT NOT NULL,
    numeric_value REAL NOT NULL,
    original_text TEXT,
    ocr_confidence REAL,
    quality_meta TEXT,
    group_id TEXT,
    range_config_version INTEGER,
    capture_timestamp TEXT NOT NULL,
    processing_timestamp TEXT NOT NULL,
    session_id TEXT,
    screen_x INTEGER,
    screen_y INTEGER,
    monitor_id TEXT,
    monitor_geometry TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    legacy_detection_id INTEGER,
    legacy_region_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_readings_region_ts
    ON readings (region_id, capture_timestamp);
CREATE INDEX IF NOT EXISTS idx_readings_region_id
    ON readings (region_id, reading_id);
CREATE INDEX IF NOT EXISTS idx_readings_group
    ON readings (group_id);
CREATE INDEX IF NOT EXISTS idx_readings_legacy
    ON readings (legacy_detection_id);

CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    name TEXT NOT NULL,
    abbreviation TEXT NOT NULL UNIQUE,
    lower_bound REAL NOT NULL,
    upper_bound REAL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS range_config_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    note TEXT,
    is_current INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS model_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 0,
    range_config_version INTEGER,
    algorithm_version TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS pattern_stats (
    pattern_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version INTEGER NOT NULL,
    pattern_length INTEGER NOT NULL,
    pattern_sequence TEXT NOT NULL,
    candidate_group TEXT NOT NULL,
    opportunities INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    correct INTEGER NOT NULL DEFAULT 0,
    incorrect INTEGER NOT NULL DEFAULT 0,
    recent_outcomes TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT,
    last_seen TEXT,
    UNIQUE (model_version, pattern_length, pattern_sequence, candidate_group)
);

CREATE INDEX IF NOT EXISTS idx_pattern_lookup
    ON pattern_stats (model_version, pattern_length, pattern_sequence);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    region_id TEXT NOT NULL,
    pattern_length INTEGER NOT NULL,
    pattern_sequence TEXT NOT NULL,
    candidate_group TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    resolved_count INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    recent_accuracy REAL,
    eligible INTEGER NOT NULL DEFAULT 0,
    basis_reading_id INTEGER,
    expected_reading_id INTEGER,
    actual_group TEXT,
    resolved_at TEXT,
    is_correct INTEGER,
    click_dispatched INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pred_unresolved
    ON predictions (model_version, resolved_at);
CREATE INDEX IF NOT EXISTS idx_pred_created
    ON predictions (created_at);

CREATE TABLE IF NOT EXISTS click_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    prediction_id INTEGER,
    outcome TEXT NOT NULL,
    block_code TEXT,
    detail TEXT,
    target_x INTEGER,
    target_y INTEGER,
    dry_run INTEGER NOT NULL DEFAULT 0,
    region_a_count INTEGER NOT NULL DEFAULT 0,
    session_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_click_audit_ts ON click_audit (created_at DESC);

CREATE TABLE IF NOT EXISTS diagnostics (
    diag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,
    detail TEXT,
    raw_text TEXT,
    region_id TEXT,
    session_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_diag_ts ON diagnostics (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_diag_category ON diagnostics (category);
"""

# ---------------------------------------------------------------------------
# v3 tables
# ---------------------------------------------------------------------------

_V3_TABLES = """
CREATE TABLE IF NOT EXISTS region_b_readings (
    reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id TEXT NOT NULL DEFAULT 'region-B',
    canonical_value TEXT NOT NULL,
    numeric_value REAL NOT NULL,
    original_text TEXT,
    ocr_confidence REAL,
    quality_meta TEXT,
    group_id TEXT,
    range_config_version INTEGER,
    capture_timestamp TEXT NOT NULL,
    processing_timestamp TEXT NOT NULL,
    session_id TEXT,
    screen_x INTEGER,
    screen_y INTEGER,
    monitor_id TEXT,
    monitor_geometry TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    legacy_detection_id INTEGER,
    legacy_region_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_region_b_ts
    ON region_b_readings (capture_timestamp);
CREATE INDEX IF NOT EXISTS idx_region_b_id
    ON region_b_readings (reading_id);
"""

#: Default application settings added in v2.
DEFAULT_SETTINGS: Dict[str, str] = {
    # v1 keys (unchanged defaults)
    "scan_interval": "1000",
    "min_ocr_confidence": "70",
    "monitoring_mode": "selected",
    # v2 keys
    "save_mode": "on_change",          # 'on_change' | 'every'
    "auto_click_enabled": "0",
    "auto_click_dry_run": "1",
    "allowed_categories": "[]",        # JSON array of group_id
    "confidence_threshold": "70",      # percent, strict '>'
    "min_evidence": "30",              # resolved predictions required
    "recent_window": "50",             # size of the recent-performance window
    "recent_min_accuracy": "60",       # percent
    "recent_safeguard_enabled": "1",
    "cooldown_seconds": "5",
    "click_target": "{}",              # JSON calibration payload (first point)
    "click_targets": "[]",             # JSON array of calibrated click points
    "click_sequence_delay_ms": "200",  # pause between two consecutive clicks
    "model_stale": "1",                # 1 until a successful recalculation
    "model_version": "0",
    "pattern_lengths": "4,5,6,7,8",
    "algorithm_version": "1.0",
    "rebuild_in_progress": "0",        # 1 while "Recalculate App Brain" runs
    "require_window_verification": "0",  # optional extra click precondition
}


def initialise_database(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Initialise/upgrade the database and return a configured connection.

    Migration is additive and idempotent: no table is dropped, truncated or
    rewritten.  Legacy ``detections`` rows are copied into ``readings`` once,
    with provenance preserved.
    """
    db_path = Path(db_path)
    if db_path.parent:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)

    # ---- v1 tables -------------------------------------------------------
    conn.executescript(_V1_TABLES)

    # ---- v2 tables -------------------------------------------------------
    conn.executescript(_V2_TABLES)

    # ---- v3 tables (Region B store-only readings) ------------------------
    conn.executescript(_V3_TABLES)

    # ---- defaults --------------------------------------------------------
    now = datetime.now(timezone.utc).isoformat()
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )

    # ---- default groups --------------------------------------------------
    _ensure_default_groups(conn)

    # ---- one-shot legacy migration --------------------------------------
    _migrate_legacy_detections(conn)

    # ---- schema marker ---------------------------------------------------
    _meta_set(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()
    return conn


def _ensure_default_groups(conn: sqlite3.Connection) -> None:
    """Insert the nine default numerical groups if the table is empty."""
    cur = conn.execute("SELECT COUNT(*) AS n FROM groups")
    if cur.fetchone()["n"] > 0:
        return

    # Imported lazily to avoid a circular import at module load time.
    from src.config.ranges import DEFAULT_GROUPS, group_rows

    now = datetime.now(timezone.utc).isoformat()
    for row in group_rows(DEFAULT_GROUPS):
        conn.execute(
            """INSERT OR IGNORE INTO groups
               (group_id, ordinal, name, abbreviation, lower_bound, upper_bound,
                is_default, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                row["group_id"], row["ordinal"], row["name"], row["abbreviation"],
                row["lower_bound"], row["upper_bound"], now, now,
            ),
        )
    conn.execute(
        "INSERT INTO range_config_versions (created_at, snapshot, note, is_current) "
        "VALUES (?, ?, ?, 1)",
        (now, json.dumps(group_rows(DEFAULT_GROUPS)), "initial defaults"),
    )


def _migrate_legacy_detections(conn: sqlite3.Connection) -> None:
    """Copy legacy ``detections`` rows into ``readings`` exactly once.

    The historical device did not have Region B, so every legacy reading is
    attributed to Region A; the original identifier is preserved in
    ``legacy_region_id`` so nothing is reinterpreted silently.
    """
    if _meta_get(conn, "legacy_detections_migrated") == "1":
        return
    if not _table_exists(conn, "detections"):
        _meta_set(conn, "legacy_detections_migrated", "1")
        return

    cur = conn.execute(
        """SELECT detection_id, region_id, canonical_value, original_text,
                  ocr_confidence, screen_x, screen_y, monitor_id,
                  capture_timestamp, processing_timestamp, session_id
           FROM detections
           WHERE detection_id NOT IN (
               SELECT legacy_detection_id FROM readings
               WHERE legacy_detection_id IS NOT NULL
           )"""
    )
    rows = cur.fetchall()
    migrated = 0
    for r in rows:
        canonical = r["canonical_value"]
        try:
            numeric = float(canonical)
        except (TypeError, ValueError):
            # Cannot be interpreted as a number - keep the row but refuse to
            # invent a numeric value.  It is preserved with numeric_value 0.0
            # and a diagnostic so it never silently inflates the gate.
            numeric = 0.0
            conn.execute(
                "INSERT INTO diagnostics (created_at, category, detail, raw_text, region_id) "
                "VALUES (?, 'MIGRATION_INVALID_NUMERIC', ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(),
                 f"legacy detection_id={r['detection_id']}", canonical, r["region_id"]),
            )
        conn.execute(
            """INSERT INTO readings
               (region_id, canonical_value, numeric_value, original_text,
                ocr_confidence, quality_meta, group_id, range_config_version,
                capture_timestamp, processing_timestamp, session_id,
                screen_x, screen_y, monitor_id, monitor_geometry, source,
                legacy_detection_id, legacy_region_id)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL,
                       'migrated', ?, ?)""",
            (
                REGION_A_ID if r["region_id"] != REGION_B_ID else REGION_B_ID,
                canonical, numeric, r["original_text"], r["ocr_confidence"],
                r["capture_timestamp"], r["processing_timestamp"], r["session_id"],
                r["screen_x"], r["screen_y"], r["monitor_id"],
                r["detection_id"], r["region_id"],
            ),
        )
        migrated += 1

    _meta_set(conn, "legacy_detections_migrated", "1")
    if migrated:
        logger.info("Migrated %d legacy detection rows into readings", migrated)


def count_valid_region_a_readings(conn: sqlite3.Connection) -> int:
    """Return the number of qualifying, persisted Region A readings.

    This is the *only* quantity that may satisfy the mandatory 1,000-record
    automation gate (spec section 12).  It counts persisted rows in
    ``readings`` for Region A; OCR attempts, rejected readings, diagnostics,
    predictions and Region B data are deliberately excluded.
    """
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE region_id = ?",
        (REGION_A_ID,),
    )
    return int(cur.fetchone()["n"])


def count_region_b_readings(conn: sqlite3.Connection) -> int:
    """Return how many readings Region B has stored in its own table.

    Region B is store-only: these rows never satisfy the automation gate, never
    feed learning or statistics and are never merged into ``readings``.
    """
    try:
        cur = conn.execute("SELECT COUNT(*) AS n FROM region_b_readings")
    except sqlite3.Error:  # pragma: no cover - pre-v3 database
        return 0
    return int(cur.fetchone()["n"])


def get_readings_ordered(conn: sqlite3.Connection,
                         region_id: str = REGION_A_ID) -> List[sqlite3.Row]:
    """Return all readings for a region in strict chronological order."""
    cur = conn.execute(
        """SELECT reading_id, region_id, canonical_value, numeric_value,
                  original_text, ocr_confidence, group_id, capture_timestamp
           FROM readings
           WHERE region_id = ?
           ORDER BY reading_id ASC""",
        (region_id,),
    )
    return cur.fetchall()
