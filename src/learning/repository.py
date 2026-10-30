"""Persistence glue for readings and the derived learning model.

Keeps the pure :mod:`src.learning.patterns` engine free of SQL while making the
incremental path and the full "Recalculate App Brain" replay write exactly the
same derived data.
"""

import json
import logging
import sqlite3
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.database.init_db import REGION_B_ID
from src.learning.patterns import (
    DEFAULT_PATTERN_LENGTHS,
    DEFAULT_RECENT_WINDOW,
    LearningEngine,
    ObservationResult,
    PatternStat,
    Prediction,
)

logger = logging.getLogger(__name__)

#: Store-only table that receives Region B readings.  It mirrors ``readings``
#: column for column so the same row shape is written for both regions, but the
#: two tables are never merged: Region B data is only ever *stored*.
REGION_B_TABLE = "region_b_readings"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

_READING_COLUMNS = (
    "region_id, canonical_value, numeric_value, original_text, ocr_confidence, "
    "quality_meta, group_id, range_config_version, capture_timestamp, "
    "processing_timestamp, session_id, screen_x, screen_y, monitor_id, "
    "monitor_geometry, source"
)


def _insert_reading(conn: sqlite3.Connection, table: str, **fields) -> int:
    """Shared writer for ``readings`` and ``region_b_readings``.

    ``table`` is always one of the two constants defined in this module - it is
    never taken from user input.
    """
    if table not in ("readings", REGION_B_TABLE):
        raise ValueError(f"unsupported reading table: {table}")
    capture_timestamp = fields.get("capture_timestamp") or _now()
    processing_timestamp = fields.get("processing_timestamp") or _now()
    quality_meta = fields.get("quality_meta")
    cursor = conn.execute(
        f"""INSERT INTO {table}
           ({_READING_COLUMNS})
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fields["region_id"], fields["canonical_value"],
            float(fields["numeric_value"]), fields.get("original_text"),
            fields.get("ocr_confidence"),
            json.dumps(quality_meta) if quality_meta else None,
            fields.get("group_id"), fields.get("range_config_version"),
            capture_timestamp, processing_timestamp, fields.get("session_id"),
            fields.get("screen_x"), fields.get("screen_y"),
            fields.get("monitor_id"), fields.get("monitor_geometry"),
            fields.get("source", "live"),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_reading(
    conn: sqlite3.Connection,
    *,
    region_id: str,
    canonical_value: str,
    numeric_value: float,
    original_text: Optional[str],
    ocr_confidence: Optional[float],
    group_id: Optional[str],
    range_config_version: Optional[int],
    capture_timestamp: Optional[str] = None,
    processing_timestamp: Optional[str] = None,
    session_id: Optional[str] = None,
    screen_x: Optional[int] = None,
    screen_y: Optional[int] = None,
    monitor_id: Optional[str] = None,
    monitor_geometry: Optional[str] = None,
    quality_meta: Optional[Dict] = None,
    source: str = "live",
) -> int:
    """Persist one accepted reading and return its stable ``reading_id``."""
    return _insert_reading(
        conn, "readings",
        region_id=region_id,
        canonical_value=canonical_value,
        numeric_value=numeric_value,
        original_text=original_text,
        ocr_confidence=ocr_confidence,
        group_id=group_id,
        range_config_version=range_config_version,
        capture_timestamp=capture_timestamp,
        processing_timestamp=processing_timestamp,
        session_id=session_id,
        screen_x=screen_x,
        screen_y=screen_y,
        monitor_id=monitor_id,
        monitor_geometry=monitor_geometry,
        quality_meta=quality_meta,
        source=source,
    )


def insert_region_b_reading(
    conn: sqlite3.Connection,
    *,
    canonical_value: str,
    numeric_value: float,
    original_text: Optional[str] = None,
    ocr_confidence: Optional[float] = None,
    group_id: Optional[str] = None,
    range_config_version: Optional[int] = None,
    capture_timestamp: Optional[str] = None,
    processing_timestamp: Optional[str] = None,
    session_id: Optional[str] = None,
    screen_x: Optional[int] = None,
    screen_y: Optional[int] = None,
    monitor_id: Optional[str] = None,
    monitor_geometry: Optional[str] = None,
    quality_meta: Optional[Dict] = None,
    source: str = "live",
    region_id: str = "region-B",
) -> int:
    """Store one Region B reading in the store-only ``region_b_readings`` table.

    Region B data is written and kept, exactly like a Region A row in shape,
    but it never reaches ``readings`` so it can never influence the gate,
    learning, predictions, statistics or history.
    """
    return _insert_reading(
        conn, REGION_B_TABLE,
        region_id=region_id,
        canonical_value=canonical_value,
        numeric_value=numeric_value,
        original_text=original_text,
        ocr_confidence=ocr_confidence,
        group_id=group_id,
        range_config_version=range_config_version,
        capture_timestamp=capture_timestamp,
        processing_timestamp=processing_timestamp,
        session_id=session_id,
        screen_x=screen_x,
        screen_y=screen_y,
        monitor_id=monitor_id,
        monitor_geometry=monitor_geometry,
        quality_meta=quality_meta,
        source=source,
    )


def last_reading(conn: sqlite3.Connection, region_id: str) -> Optional[sqlite3.Row]:
    if region_id == REGION_B_ID:
        return last_region_b_reading(conn)
    return conn.execute(
        """SELECT reading_id, canonical_value, numeric_value, group_id,
                  capture_timestamp
           FROM readings WHERE region_id = ?
           ORDER BY reading_id DESC LIMIT 1""",
        (region_id,),
    ).fetchone()


def last_region_b_reading(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Most recent Region B row (used for duplicate suppression on slot B)."""
    try:
        return conn.execute(
            """SELECT reading_id, canonical_value, numeric_value, group_id,
                      capture_timestamp
               FROM region_b_readings
               ORDER BY reading_id DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - pre-v3 database
        return None


def recent_region_b_readings(conn: sqlite3.Connection, limit: int = 200,
                             offset: int = 0) -> List[sqlite3.Row]:
    """Newest-first page through the store-only Region B table."""
    try:
        return conn.execute(
            """SELECT * FROM region_b_readings
               ORDER BY reading_id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
    except sqlite3.Error:  # pragma: no cover - pre-v3 database
        return []


def recent_readings(conn: sqlite3.Connection, limit: int = 200,
                    offset: int = 0, region_id: Optional[str] = None
                    ) -> List[sqlite3.Row]:
    """Page through readings, newest first."""
    if region_id:
        return conn.execute(
            """SELECT r.*, g.name AS group_name, g.abbreviation AS group_abbrev
               FROM readings r LEFT JOIN groups g ON r.group_id = g.group_id
               WHERE r.region_id = ?
               ORDER BY r.reading_id DESC LIMIT ? OFFSET ?""",
            (region_id, limit, offset),
        ).fetchall()
    return conn.execute(
        """SELECT r.*, g.name AS group_name, g.abbreviation AS group_abbrev
           FROM readings r LEFT JOIN groups g ON r.group_id = g.group_id
           ORDER BY r.reading_id DESC LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()


def delete_readings(conn: sqlite3.Connection, reading_ids: Sequence[int]) -> int:
    """Delete the given raw readings by id and return how many rows were removed.

    Only the readings the user explicitly selected are touched.  The derived
    model is flagged stale by the caller, because it was built from data that no
    longer exists.
    """
    ids = [int(reading_id) for reading_id in reading_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = conn.execute(
        f"DELETE FROM readings WHERE reading_id IN ({placeholders})", ids)
    conn.commit()
    return int(cursor.rowcount or 0)


def reading_group_sequence(conn: sqlite3.Connection, region_id: str,
                           model_agnostic: bool = True) -> List[sqlite3.Row]:
    """Chronological ``(reading_id, group_id, capture_timestamp)`` tuples.

    Used by the rebuild replay and to bootstrap the incremental engine.
    """
    return conn.execute(
        """SELECT reading_id, group_id, capture_timestamp
           FROM readings
           WHERE region_id = ? AND group_id IS NOT NULL
           ORDER BY reading_id ASC""",
        (region_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Incremental learning persistence
# ---------------------------------------------------------------------------

def load_engine_state(
    conn: sqlite3.Connection,
    model_version: int,
    abbreviation_fn: Callable[[str], str],
    pattern_lengths: Sequence[int] = DEFAULT_PATTERN_LENGTHS,
    recent_window: int = DEFAULT_RECENT_WINDOW,
    region_id: str = "region-A",
) -> LearningEngine:
    """Rehydrate a :class:`LearningEngine` from the published derived model.

    Only the tail of the category sequence is needed because candidate
    selection looks back at most ``max(pattern_lengths)`` categories.
    """
    engine = LearningEngine(
        abbreviation_fn=abbreviation_fn,
        pattern_lengths=pattern_lengths,
        recent_window=recent_window,
    )

    rows = conn.execute(
        """SELECT pattern_length, pattern_sequence, candidate_group,
                  opportunities, resolved, correct, incorrect, recent_outcomes,
                  first_seen, last_seen
           FROM pattern_stats WHERE model_version = ?""",
        (model_version,),
    ).fetchall()
    for r in rows:
        stat = PatternStat(
            pattern_length=r["pattern_length"],
            pattern_sequence=r["pattern_sequence"],
            candidate_group=r["candidate_group"],
            opportunities=r["opportunities"],
            resolved=r["resolved"],
            correct=r["correct"],
            incorrect=r["incorrect"],
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
        )
        stat.recent = deque(
            (bool(x) for x in json.loads(r["recent_outcomes"] or "[]")),
            maxlen=recent_window,
        )
        engine.stats[(stat.pattern_length, stat.pattern_sequence,
                      stat.candidate_group)] = stat

    # Rebuild just enough of the chronological tail for pattern lookups.
    max_len = max(pattern_lengths) if pattern_lengths else 0
    tail = conn.execute(
        """SELECT group_id, capture_timestamp FROM readings
           WHERE region_id = ? AND group_id IS NOT NULL
           ORDER BY reading_id DESC LIMIT ?""",
        (region_id, max(max_len, 1)),
    ).fetchall()
    engine.sequence = [r["group_id"] for r in reversed(tail)]
    if tail:
        # So the first reading after a restart can still be recognised as too
        # far removed from the previous one (gap-based sequence break).
        engine.restore_last_observed(tail[0]["capture_timestamp"])

    # Restore the outstanding prediction so its outcome can be resolved.
    pending = conn.execute(
        """SELECT prediction_id, pattern_length, pattern_sequence, candidate_group,
                  confidence, resolved_count, correct_count, recent_accuracy,
                  created_at, basis_reading_id, expected_reading_id
           FROM predictions
           WHERE model_version = ? AND resolved_at IS NULL
           ORDER BY prediction_id DESC LIMIT 1""",
        (model_version,),
    ).fetchone()
    if pending is not None:
        engine.set_pending(Prediction(
            pattern_length=pending["pattern_length"],
            pattern_sequence=pending["pattern_sequence"],
            candidate_group=pending["candidate_group"],
            confidence=pending["confidence"],
            resolved_count=pending["resolved_count"],
            correct_count=pending["correct_count"],
            recent_accuracy=pending["recent_accuracy"],
            created_at=pending["created_at"],
            basis_reading_id=pending["basis_reading_id"],
            expected_reading_id=pending["expected_reading_id"],
        ))
    return engine


def pending_prediction_id(conn: sqlite3.Connection, model_version: int) -> Optional[int]:
    row = conn.execute(
        """SELECT prediction_id FROM predictions
           WHERE model_version = ? AND resolved_at IS NULL
           ORDER BY prediction_id DESC LIMIT 1""",
        (model_version,),
    ).fetchone()
    return int(row["prediction_id"]) if row else None


def upsert_pattern_stats(conn: sqlite3.Connection, model_version: int,
                         rows: Sequence[Dict]) -> None:
    """Insert-or-update the given pattern counters for a model version."""
    for row in rows:
        conn.execute(
            """INSERT INTO pattern_stats
               (model_version, pattern_length, pattern_sequence, candidate_group,
                opportunities, resolved, correct, incorrect, recent_outcomes,
                first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_version, pattern_length, pattern_sequence, candidate_group)
               DO UPDATE SET
                   opportunities = excluded.opportunities,
                   resolved = excluded.resolved,
                   correct = excluded.correct,
                   incorrect = excluded.incorrect,
                   recent_outcomes = excluded.recent_outcomes,
                   last_seen = excluded.last_seen""",
            (
                model_version, row["pattern_length"], row["pattern_sequence"],
                row["candidate_group"], row["opportunities"], row["resolved"],
                row["correct"], row["incorrect"],
                json.dumps(row.get("recent_outcomes", [])),
                row.get("first_seen"), row.get("last_seen"),
            ),
        )


def insert_prediction(conn: sqlite3.Connection, model_version: int,
                      region_id: str, prediction: Prediction) -> int:
    cursor = conn.execute(
        """INSERT INTO predictions
           (model_version, created_at, region_id, pattern_length, pattern_sequence,
            candidate_group, confidence, resolved_count, correct_count,
            recent_accuracy, eligible, basis_reading_id, expected_reading_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            model_version, prediction.created_at or _now(), region_id,
            prediction.pattern_length, prediction.pattern_sequence,
            prediction.candidate_group, prediction.confidence,
            prediction.resolved_count, prediction.correct_count,
            prediction.recent_accuracy, prediction.basis_reading_id,
            prediction.expected_reading_id,
        ),
    )
    return int(cursor.lastrowid)


def resolve_prediction(conn: sqlite3.Connection, prediction_id: int,
                       prediction: Prediction) -> None:
    conn.execute(
        """UPDATE predictions
           SET actual_group = ?, resolved_at = ?, is_correct = ?
           WHERE prediction_id = ?""",
        (
            prediction.actual_group,
            prediction.resolved_at or _now(),
            1 if prediction.is_correct else 0,
            prediction_id,
        ),
    )


def apply_learning(
    conn: sqlite3.Connection,
    model_version: int,
    region_id: str,
    result: ObservationResult,
    pending_id: Optional[int],
) -> Tuple[bool, Optional[int]]:
    """Persist one incremental learning step.

    Returns ``(resolved_previous, new_prediction_id)``.
    """
    resolved_previous = False
    if result.resolved is not None and pending_id is not None:
        resolve_prediction(conn, pending_id, result.resolved)
        resolved_previous = True

    if result.updated_stats:
        upsert_pattern_stats(conn, model_version, result.updated_stats)

    new_id = None
    if result.prediction is not None:
        new_id = insert_prediction(conn, model_version, region_id, result.prediction)

    conn.commit()
    return resolved_previous, new_id


# ---------------------------------------------------------------------------
# Queries used by the UI
# ---------------------------------------------------------------------------

def prediction_summary(conn: sqlite3.Connection, model_version: int,
                       group_ids: Optional[Sequence[str]] = None) -> Dict:
    """Aggregate prediction quality for a model version.

    ``group_ids`` narrows the aggregation to those candidate groups, which is
    what the Learning page uses for its "allowed categories only" panel.  An
    empty ``group_ids`` sequence yields a zeroed summary instead of matching
    nothing by accident.
    """
    if group_ids is not None and not list(group_ids):
        return {
            "total": 0, "resolved": 0, "correct": 0, "incorrect": 0,
            "unresolved": 0, "accuracy": None,
        }

    where = "model_version = ?"
    params: List = [model_version]
    if group_ids is not None:
        ids = [str(g) for g in group_ids]
        where += f" AND candidate_group IN ({','.join('?' for _ in ids)})"
        params.extend(ids)

    row = conn.execute(
        f"""SELECT COUNT(*) AS total,
                  SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                  SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) AS correct,
                  SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) AS incorrect
           FROM predictions WHERE {where}""",
        params,
    ).fetchone()
    total = row["total"] or 0
    resolved = row["resolved"] or 0
    correct = row["correct"] or 0
    incorrect = row["incorrect"] or 0
    return {
        "total": total,
        "resolved": resolved,
        "correct": correct,
        "incorrect": incorrect,
        "unresolved": total - resolved,
        "accuracy": (100.0 * correct / resolved) if resolved else None,
    }


def _group_filter(group_ids: Optional[Sequence[str]]) -> Tuple[str, List]:
    """``(" AND col IN (?,?)", [...])`` fragment for an optional group list."""
    if group_ids is None:
        return "", []
    ids = [str(g) for g in group_ids]
    if not ids:
        return " AND 1 = 0", []
    return f" AND candidate_group IN ({','.join('?' for _ in ids)})", ids


def pattern_count(conn: sqlite3.Connection, model_version: int,
                  group_ids: Optional[Sequence[str]] = None) -> int:
    """Number of learned pattern rows, optionally narrowed to some groups."""
    extra, params = _group_filter(group_ids)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM pattern_stats "
        f"WHERE model_version = ?{extra}",
        [model_version, *params],
    ).fetchone()
    return int(row["n"] or 0)


def region_a_reading_count(conn: sqlite3.Connection,
                           group_ids: Optional[Sequence[str]] = None) -> int:
    """Region A readings, optionally narrowed to the allowed groups only."""
    where = "WHERE region_id = 'region-A'"
    params: List = []
    if group_ids is not None:
        ids = [str(g) for g in group_ids]
        if not ids:
            return 0
        where += f" AND group_id IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    row = conn.execute(f"SELECT COUNT(*) AS n FROM readings {where}", params).fetchone()
    return int(row["n"] or 0)


def recent_resolved_accuracy(conn: sqlite3.Connection, model_version: int,
                             limit: int = 50,
                             group_ids: Optional[Sequence[str]] = None) -> Dict:
    """Accuracy over the most recent ``limit`` *resolved* predictions."""
    extra, params = _group_filter(group_ids)
    rows = conn.execute(
        f"""SELECT is_correct FROM predictions
            WHERE model_version = ? AND resolved_at IS NOT NULL{extra}
            ORDER BY prediction_id DESC LIMIT ?""",
        [model_version, *params, max(1, int(limit))],
    ).fetchall()
    if not rows:
        return {"count": 0, "correct": 0, "accuracy": None}
    correct = sum(1 for r in rows if int(r["is_correct"] or 0) == 1)
    return {
        "count": len(rows),
        "correct": correct,
        "accuracy": 100.0 * correct / len(rows),
    }


def recent_predictions(conn: sqlite3.Connection, limit: int = 100,
                       only_unresolved: bool = False) -> List[sqlite3.Row]:
    if only_unresolved:
        return conn.execute(
            """SELECT * FROM predictions WHERE resolved_at IS NULL
               ORDER BY prediction_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM predictions ORDER BY prediction_id DESC LIMIT ?", (limit,)
    ).fetchall()


def top_patterns_from_db(conn: sqlite3.Connection, model_version: int,
                         limit: int = 100) -> List[sqlite3.Row]:
    return conn.execute(
        """SELECT pattern_length, pattern_sequence, candidate_group, opportunities,
                  resolved, correct, incorrect, recent_outcomes
           FROM pattern_stats
           WHERE model_version = ?
           ORDER BY opportunities DESC, pattern_length ASC LIMIT ?""",
        (model_version, limit),
    ).fetchall()


def recent_click_audit(conn: sqlite3.Connection, limit: int = 200) -> List[sqlite3.Row]:
    return conn.execute(
        """SELECT audit_id, created_at, prediction_id, outcome, block_code, detail,
                  target_x, target_y, dry_run, region_a_count
           FROM click_audit ORDER BY audit_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
