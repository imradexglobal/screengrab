"""Recalculate App Brain - rebuild the derived model from raw readings.

Guarantees (spec sections 14 and 17):

* Raw readings, raw OCR text, timestamps, stable reading ids, capture metadata,
  settings, range-configuration history, click rules/coordinates and the click
  audit log are **never** deleted or rewritten.
* Numeric readings are reclassified against the *current* valid ranges.
* Eligible Region A readings are replayed strictly chronologically; each
  historical prediction is generated before the next actual outcome is exposed,
  so no future information can leak into a prediction's confidence.
* All derived writes happen in a single transaction tagged with a new model
  version.  On any failure the transaction is rolled back, the previous
  published model remains intact, nothing partial is published and automated
  clicking stays blocked.
* Historical click actions are never re-executed.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from src.config.repository import (
    get_setting,
    load_range_config,
    mark_model_valid,
    model_is_stale,
    set_setting,
)
from src.database.init_db import REGION_A_ID, count_valid_region_a_readings
from src.learning import repository as learn_repo
from src.learning.patterns import (
    DEFAULT_PATTERN_LENGTHS,
    DEFAULT_RECENT_WINDOW,
    LearningEngine,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def recalculate_app_brain(
    conn: sqlite3.Connection,
    algorithm_version: str = "1.0",
    pattern_lengths: Sequence[int] = DEFAULT_PATTERN_LENGTHS,
    recent_window: int = DEFAULT_RECENT_WINDOW,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict:
    """Rebuild all derived learning data atomically.

    Returns a result dict: ``{"ok": bool, "model_version": int|None,
    "source_count": int, "predictions": int, "patterns": int, "error": str}``.
    """
    def report(frac: float, msg: str) -> None:
        if progress:
            try:
                progress(frac, msg)
            except Exception:  # pragma: no cover - UI callback must not break us
                pass

    # 1) Immediately block new automated clicks and flag the rebuild.
    set_setting(conn, "rebuild_in_progress", "1")
    conn.commit()
    report(0.02, "Automated clicking blocked for recalculation")

    range_version = _current_range_version(conn)
    config = load_range_config(conn)
    abbreviation = config.abbreviation

    version = _begin_model_version(conn, range_version, algorithm_version)
    source_count = 0
    n_predictions = 0
    n_patterns = 0

    try:
        # 2) Consistent snapshot of the raw readings (chronological).
        snapshot = conn.execute(
            """SELECT reading_id, numeric_value, capture_timestamp
               FROM readings WHERE region_id = ?
               ORDER BY reading_id ASC""",
            (REGION_A_ID,),
        ).fetchall()
        source_count = len(snapshot)
        report(0.05, f"Snapshot: {source_count} Region A readings")

        # 3) Reclassify every reading with the current ranges (derived field).
        classified: List[tuple] = []
        groups: List[str] = []
        timestamps: List[str] = []
        reading_ids: List[int] = []
        for row in snapshot:
            gid = config.classify(row["numeric_value"])
            classified.append((gid, range_version, row["reading_id"]))
            if gid is not None:
                groups.append(gid)
                timestamps.append(row["capture_timestamp"])
                reading_ids.append(row["reading_id"])

        report(0.20, f"Reclassified {source_count} readings")

        # 4) Chronological replay - a prediction is always created before the
        #    next actual outcome is observed.
        engine = LearningEngine(
            abbreviation_fn=abbreviation,
            pattern_lengths=pattern_lengths,
            recent_window=recent_window,
        )
        predictions = []
        total = max(len(groups), 1)
        for idx, gid in enumerate(groups):
            result = engine.observe(
                gid,
                now=timestamps[idx],
                basis_reading_id=reading_ids[idx],
                expected_reading_id=(reading_ids[idx + 1]
                                     if idx + 1 < len(reading_ids) else None),
            )
            if result.prediction is not None:
                predictions.append(result.prediction)
            if idx % 500 == 0:
                report(0.20 + 0.6 * (idx / total), f"Replaying {idx}/{total}")

        report(0.85, f"Replayed {len(groups)} classified readings")

        stat_rows = [s.to_row() for s in engine.stats.values()]

        # 5) Validate before publishing anything.
        if source_count > 0 and len(groups) == 0:
            raise ValueError(
                "No reading falls inside the configured ranges; refusing to "
                "publish an empty model."
            )

        # 6) Atomic publication of the derived data.
        conn.commit()  # close any implicit transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "UPDATE readings SET group_id = ?, range_config_version = ? "
                "WHERE reading_id = ?",
                classified,
            )
            conn.executemany(
                """INSERT INTO pattern_stats
                   (model_version, pattern_length, pattern_sequence, candidate_group,
                    opportunities, resolved, correct, incorrect, recent_outcomes,
                    first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        version, r["pattern_length"], r["pattern_sequence"],
                        r["candidate_group"], r["opportunities"], r["resolved"],
                        r["correct"], r["incorrect"],
                        json.dumps(r.get("recent_outcomes", [])),
                        r.get("first_seen"), r.get("last_seen"),
                    )
                    for r in stat_rows
                ],
            )
            conn.executemany(
                """INSERT INTO predictions
                   (model_version, created_at, region_id, pattern_length,
                    pattern_sequence, candidate_group, confidence, resolved_count,
                    correct_count, recent_accuracy, eligible, basis_reading_id,
                    expected_reading_id, actual_group, resolved_at, is_correct)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                [
                    (
                        version, p.created_at or _now(), REGION_A_ID,
                        p.pattern_length, p.pattern_sequence, p.candidate_group,
                        p.confidence, p.resolved_count, p.correct_count,
                        p.recent_accuracy, p.basis_reading_id,
                        p.expected_reading_id, p.actual_group, p.resolved_at,
                        None if p.is_correct is None else (1 if p.is_correct else 0),
                    )
                    for p in predictions
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        n_patterns = len(stat_rows)
        n_predictions = len(predictions)
        report(0.95, f"Published model v{version}")

    except Exception as exc:  # noqa: BLE001 - any failure must keep the old model
        logger.error("Recalculation failed: %s", exc, exc_info=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        _finish_model_version(conn, version, "FAILED", source_count, str(exc))
        set_setting(conn, "rebuild_in_progress", "0")
        set_setting(conn, "model_stale", "1")
        set_setting(conn, "auto_click_enabled", "0")
        conn.commit()
        return {
            "ok": False, "model_version": None, "source_count": source_count,
            "predictions": 0, "patterns": 0, "error": str(exc),
        }

    # 7) Publish the new model version and re-enable normal operation.
    _finish_model_version(conn, version, "VALID", source_count,
                          f"{n_patterns} patterns, {n_predictions} predictions")
    mark_model_valid(conn, version)
    set_setting(conn, "rebuild_in_progress", "0")
    conn.commit()
    report(1.0, f"Recalculation complete (model v{version})")

    return {
        "ok": True, "model_version": version, "source_count": source_count,
        "predictions": n_predictions, "patterns": n_patterns, "error": "",
    }


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _current_range_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM range_config_versions WHERE is_current = 1"
    ).fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _begin_model_version(conn: sqlite3.Connection, range_version: int,
                         algorithm_version: str) -> int:
    cursor = conn.execute(
        """INSERT INTO model_versions
           (created_at, status, source_count, range_config_version, algorithm_version)
           VALUES (?, 'BUILDING', 0, ?, ?)""",
        (_now(), range_version, algorithm_version),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _finish_model_version(conn: sqlite3.Connection, version: int, status: str,
                          source_count: int, detail: str = "") -> None:
    conn.execute(
        """UPDATE model_versions
           SET completed_at = ?, status = ?, source_count = ?, detail = ?
           WHERE version = ?""",
        (_now(), status, source_count, detail, version),
    )
    conn.commit()


def rebuild_required(conn: sqlite3.Connection) -> bool:
    """True when the derived model must be recalculated before clicking."""
    if model_is_stale(conn):
        return True
    if get_setting(conn, "model_version", "0") in ("", "0"):
        return True
    return False


def catch_up_readings(conn: sqlite3.Connection, after_reading_id: int,
                      region_id: str = REGION_A_ID) -> List[sqlite3.Row]:
    """Readings stored after the rebuild snapshot, in chronological order.

    Used for the controlled catch-up process so that readings arriving during a
    recalculation are folded into the freshly published model deterministically.
    """
    return conn.execute(
        """SELECT reading_id, group_id, capture_timestamp FROM readings
           WHERE region_id = ? AND reading_id > ?
           ORDER BY reading_id ASC""",
        (region_id, after_reading_id),
    ).fetchall()
