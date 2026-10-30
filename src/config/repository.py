"""Persistence helpers for settings, category groups and diagnostics."""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.config.ranges import Group, RangeConfig, RangeValidationError, group_rows

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None:
        return default
    return row["value"]


def get_setting_bool(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    return get_setting(conn, key, "1" if default else "0") == "1"


def get_setting_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(float(get_setting(conn, key, str(default))))
    except (TypeError, ValueError):
        return default


def get_setting_float(conn: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(conn, key, str(default)))
    except (TypeError, ValueError):
        return default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), _now()),
    )


def load_settings(conn: sqlite3.Connection) -> Dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def get_allowed_categories(conn: sqlite3.Connection) -> List[str]:
    raw = get_setting(conn, "allowed_categories", "[]")
    try:
        data = json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def set_allowed_categories(conn: sqlite3.Connection, group_ids: List[str]) -> None:
    set_setting(conn, "allowed_categories", json.dumps(sorted(set(group_ids))))


def get_click_target(conn: sqlite3.Connection) -> Dict:
    raw = get_setting(conn, "click_target", "{}")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def set_click_target(conn: sqlite3.Connection, payload: Dict) -> None:
    set_setting(conn, "click_target", json.dumps(payload))


def get_click_targets(conn: sqlite3.Connection) -> List[Dict]:
    """Return every calibrated click point, newest list first.

    The modern ``click_targets`` array is the source of truth.  A database that
    only holds the older single ``click_target`` payload still resolves to a
    one-point sequence, so pre-existing calibrations keep working unchanged.
    """
    raw = get_setting(conn, "click_targets", "[]")
    try:
        data = json.loads(raw)
        targets = [t for t in data if isinstance(t, dict) and "x" in t and "y" in t] \
            if isinstance(data, list) else []
    except (TypeError, ValueError):
        targets = []

    if targets:
        return targets

    legacy = get_click_target(conn)
    if legacy and "x" in legacy and "y" in legacy:
        return [legacy]
    return []


def set_click_targets(conn: sqlite3.Connection, payloads: List[Dict]) -> None:
    """Persist the whole click-point sequence (replacing the previous one)."""
    cleaned = []
    for payload in payloads:
        if isinstance(payload, dict) and "x" in payload and "y" in payload:
            cleaned.append({
                **payload,
                "x": int(payload["x"]),
                "y": int(payload["y"]),
            })
    set_setting(conn, "click_targets", json.dumps(cleaned))
    # The first point stays available through the legacy single-target key so
    # older readers (and older databases) keep seeing a valid calibration.
    if cleaned:
        set_click_target(conn, cleaned[0])
    else:
        set_setting(conn, "click_target", "{}")


def get_click_sequence_delay_ms(conn: sqlite3.Connection) -> int:
    """Pause between two consecutive points of a click sequence, in ms."""
    return max(0, min(10000, get_setting_int(conn, "click_sequence_delay_ms", 200)))


def set_click_sequence_delay_ms(conn: sqlite3.Connection, delay_ms: int) -> None:
    set_setting(conn, "click_sequence_delay_ms",
                str(max(0, min(10000, int(delay_ms)))))


# ---------------------------------------------------------------------------
# Category groups / numerical ranges
# ---------------------------------------------------------------------------

def load_groups(conn: sqlite3.Connection) -> List[Group]:
    rows = conn.execute(
        """SELECT group_id, ordinal, name, abbreviation, lower_bound, upper_bound
           FROM groups ORDER BY ordinal ASC"""
    ).fetchall()
    return [
        Group(
            group_id=r["group_id"],
            ordinal=r["ordinal"],
            name=r["name"],
            abbreviation=r["abbreviation"],
            lower_bound=r["lower_bound"],
            upper_bound=r["upper_bound"],
        )
        for r in rows
    ]


def current_range_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM range_config_versions WHERE is_current = 1"
    ).fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def load_range_config(conn: sqlite3.Connection) -> RangeConfig:
    return RangeConfig(load_groups(conn), version=current_range_version(conn))


def save_range_config(conn: sqlite3.Connection, groups: List[Group],
                      note: str = "edited") -> int:
    """Persist a new validated range configuration and return its version.

    Existing readings are never rewritten here; only the configuration is
    versioned.  The derived model is flagged stale so that automated clicking
    stays blocked until a successful recalculation.
    """
    config = RangeConfig(groups)
    config.require_valid()  # raises RangeValidationError on bad input

    now = _now()
    # Retire the previous current version
    conn.execute("UPDATE range_config_versions SET is_current = 0 WHERE is_current = 1")

    for g in config.groups:
        conn.execute(
            """INSERT INTO groups
               (group_id, ordinal, name, abbreviation, lower_bound, upper_bound,
                is_default, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
               ON CONFLICT(group_id) DO UPDATE SET
                   ordinal = excluded.ordinal,
                   name = excluded.name,
                   abbreviation = excluded.abbreviation,
                   lower_bound = excluded.lower_bound,
                   upper_bound = excluded.upper_bound,
                   updated_at = excluded.updated_at""",
            (g.group_id, g.ordinal, g.name, g.abbreviation,
             g.lower_bound, g.upper_bound, now, now),
        )

    cursor = conn.execute(
        "INSERT INTO range_config_versions (created_at, snapshot, note, is_current) "
        "VALUES (?, ?, ?, 1)",
        (now, json.dumps(config.to_snapshot()), note),
    )
    version = int(cursor.lastrowid)

    # Removing a group leaves historical readings untouched: their group_id
    # simply becomes unresolvable, which the rebuild reclassifies.
    existing_ids = {g.group_id for g in config.groups}
    for row in conn.execute("SELECT group_id FROM groups").fetchall():
        if row["group_id"] not in existing_ids:
            conn.execute("DELETE FROM groups WHERE group_id = ?", (row["group_id"],))

    conn.commit()
    mark_model_stale(conn, f"range configuration changed (version {version})")
    return version


# ---------------------------------------------------------------------------
# Derived-model state
# ---------------------------------------------------------------------------

def mark_model_stale(conn: sqlite3.Connection, reason: str) -> None:
    """Flag the derived model as requiring recalculation and block clicking."""
    set_setting(conn, "model_stale", "1")
    set_setting(conn, "auto_click_enabled", "0")
    record_diagnostic(conn, "MODEL_STALE", reason)
    conn.commit()


def model_is_stale(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "model_stale", "1") == "1"


def current_model_version(conn: sqlite3.Connection) -> int:
    return get_setting_int(conn, "model_version", 0)


def mark_model_valid(conn: sqlite3.Connection, version: int) -> None:
    set_setting(conn, "model_version", str(version))
    set_setting(conn, "model_stale", "0")
    conn.commit()


def model_status(conn: sqlite3.Connection) -> str:
    """Return one of VALID / STALE / REBUILDING / FAILED."""
    row = conn.execute(
        "SELECT status FROM model_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row and row["status"] == "BUILDING":
        return "REBUILDING"
    if row and row["status"] == "FAILED" and model_is_stale(conn):
        return "FAILED"
    return "STALE" if model_is_stale(conn) else "VALID"


def begin_model_version(conn: sqlite3.Connection, range_version: int,
                        algorithm_version: str) -> int:
    cursor = conn.execute(
        """INSERT INTO model_versions
           (created_at, status, source_count, range_config_version, algorithm_version)
           VALUES (?, 'BUILDING', 0, ?, ?)""",
        (_now(), range_version, algorithm_version),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_model_version(conn: sqlite3.Connection, version: int, status: str,
                         source_count: int, detail: str = "") -> None:
    conn.execute(
        """UPDATE model_versions
           SET completed_at = ?, status = ?, source_count = ?, detail = ?
           WHERE version = ?""",
        (_now(), status, source_count, detail, version),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def record_diagnostic(conn: sqlite3.Connection, category: str, detail: str = "",
                      raw_text: Optional[str] = None,
                      region_id: Optional[str] = None,
                      session_id: Optional[str] = None) -> None:
    """Record a rejection, capture error or other diagnostic event.

    Diagnostics are stored separately from valid readings so that they can
    never be mistaken for readings nor influence the 1,000-record gate.
    """
    try:
        conn.execute(
            """INSERT INTO diagnostics
               (created_at, category, detail, raw_text, region_id, session_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_now(), category, detail, raw_text, region_id, session_id),
        )
        conn.commit()
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        logger.error("Failed to record diagnostic %s: %s", category, exc)


def recent_diagnostics(conn: sqlite3.Connection, limit: int = 200) -> List[sqlite3.Row]:
    return conn.execute(
        """SELECT diag_id, created_at, category, detail, raw_text, region_id
           FROM diagnostics ORDER BY diag_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
