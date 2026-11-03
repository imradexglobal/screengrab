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
from src.learning.manual_brain import (
    KIND_MOVEMENT,
    KIND_RANGE,
    MAX_PATTERN_LENGTH,
    MIN_PATTERN_LENGTH,
    MOVEMENT_TOKENS,
    ManualBrain,
    ManualObservation,
    PatternDefinition,
    STAT_MOVEMENT,
    STAT_MOVEMENT_RANGE,
    STAT_RANGE,
    STAT_RANGE_MOVEMENT,
    movement_between,
    normalise_movement,
)
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
# Manual pattern definitions
# ---------------------------------------------------------------------------

def _clean_definition_tokens(conn: sqlite3.Connection, kind: str,
                             tokens: Sequence[str]) -> Tuple[str, ...]:
    """Validate and canonicalise the tokens of one pattern definition."""
    kind = str(kind or "").strip().lower()
    if kind not in (KIND_RANGE, KIND_MOVEMENT):
        raise ValueError("pattern kind must be 'range' or 'movement'")

    cleaned: List[str] = []
    for token in tokens:
        text = str(token or "").strip()
        if not text:
            raise ValueError("pattern tokens cannot be empty")
        cleaned.append(normalise_movement(text) if kind == KIND_MOVEMENT else text)

    if not (MIN_PATTERN_LENGTH <= len(cleaned) <= MAX_PATTERN_LENGTH):
        raise ValueError(
            f"a pattern needs between {MIN_PATTERN_LENGTH} and "
            f"{MAX_PATTERN_LENGTH} tokens")

    if kind == KIND_RANGE:
        known = {row["group_id"] for row in conn.execute(
            "SELECT group_id FROM groups")}
        unknown = [token for token in cleaned
                   if token not in known and token != "?"]
        if unknown:
            raise ValueError(
                "unknown range token(s): " + ", ".join(sorted(set(unknown))))

    return tuple(cleaned)


def _definition_from_row(row: sqlite3.Row) -> PatternDefinition:
    tokens = tuple(str(row["tokens"] or "").split(","))
    if tokens == ("",):
        tokens = ()
    expected_kind = row["expected_kind"] if "expected_kind" in row.keys() else None
    expected_value = row["expected_value"] if "expected_value" in row.keys() else None
    return PatternDefinition(
        pattern_id=int(row["pattern_id"]),
        kind=str(row["kind"]),
        tokens=tokens,
        label=str(row["label"] or ""),
        enabled=bool(row["enabled"]),
        expected_kind=str(expected_kind) if expected_kind else None,
        expected_value=str(expected_value) if expected_value else None,
    )


def create_pattern_definition(conn: sqlite3.Connection, kind: str,
                              tokens: Sequence[str],
                              label: str = "",
                              expected_kind: Optional[str] = None,
                              expected_value: Optional[str] = None) -> int:
    """Add one user-defined pattern and return its id."""
    cleaned = _clean_definition_tokens(conn, kind, tokens)
    now = _now()
    try:
        cursor = conn.execute(
            """INSERT INTO pattern_definitions
               (kind, tokens, label, enabled, expected_kind, expected_value, created_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
            (str(kind).strip().lower(), ",".join(cleaned),
             str(label or "").strip(),
             str(expected_kind).strip().lower() if expected_kind else None,
             str(expected_value).strip() if expected_value else None,
             now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("that pattern already exists") from exc
    conn.commit()
    return int(cursor.lastrowid)


def list_pattern_definitions(conn: sqlite3.Connection) -> List[PatternDefinition]:
    rows = conn.execute(
        """SELECT pattern_id, kind, tokens, label, enabled, expected_kind, expected_value
           FROM pattern_definitions ORDER BY pattern_id ASC"""
    ).fetchall()
    return [_definition_from_row(row) for row in rows]


def enabled_pattern_definitions(conn: sqlite3.Connection) -> List[PatternDefinition]:
    rows = conn.execute(
        """SELECT pattern_id, kind, tokens, label, enabled, expected_kind, expected_value
           FROM pattern_definitions WHERE enabled = 1 ORDER BY pattern_id ASC"""
    ).fetchall()
    return [_definition_from_row(row) for row in rows]


def get_pattern_definition(conn: sqlite3.Connection,
                           pattern_id: int) -> Optional[PatternDefinition]:
    row = conn.execute(
        """SELECT pattern_id, kind, tokens, label, enabled, expected_kind, expected_value
           FROM pattern_definitions WHERE pattern_id = ?""",
        (int(pattern_id),),
    ).fetchone()
    return _definition_from_row(row) if row is not None else None


def set_pattern_enabled(conn: sqlite3.Connection, pattern_id: int,
                        enabled: bool) -> bool:
    cursor = conn.execute(
        "UPDATE pattern_definitions SET enabled = ?, updated_at = ? "
        "WHERE pattern_id = ?",
        (1 if enabled else 0, _now(), int(pattern_id)),
    )
    conn.commit()
    return bool(cursor.rowcount)


def update_pattern_expected(conn: sqlite3.Connection, pattern_id: int,
                            expected_kind: Optional[str],
                            expected_value: Optional[str]) -> bool:
    cursor = conn.execute(
        "UPDATE pattern_definitions SET expected_kind = ?, expected_value = ?, updated_at = ? WHERE pattern_id = ?",
        (str(expected_kind).strip().lower() if expected_kind else None,
         str(expected_value).strip() if expected_value else None,
         _now(), int(pattern_id)),
    )
    conn.commit()
    return bool(cursor.rowcount)


def delete_pattern_definition(conn: sqlite3.Connection, pattern_id: int) -> bool:
    """Delete a definition and the derived rows that belong to it."""
    definition_id = int(pattern_id)
    conn.execute("DELETE FROM pattern_stats WHERE definition_id = ?",
                 (definition_id,))
    conn.execute("DELETE FROM predictions WHERE definition_id = ?",
                 (definition_id,))
    cursor = conn.execute("DELETE FROM pattern_definitions WHERE pattern_id = ?",
                          (definition_id,))
    conn.commit()
    return bool(cursor.rowcount)


# ---------------------------------------------------------------------------
# Manual brain rehydration
# ---------------------------------------------------------------------------

def _load_stat(engine: LearningEngine, row: sqlite3.Row,
               recent_window: int) -> None:
    stat = PatternStat(
        pattern_length=row["pattern_length"],
        pattern_sequence=row["pattern_sequence"],
        candidate_group=row["candidate_group"],
        opportunities=row["opportunities"],
        resolved=row["resolved"],
        correct=row["correct"],
        incorrect=row["incorrect"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        pattern_kind=row["pattern_kind"],
        definition_id=row["definition_id"],
    )
    stat.recent = deque(
        (bool(x) for x in json.loads(row["recent_outcomes"] or "[]")),
        maxlen=recent_window,
    )
    engine.stats[(stat.pattern_length, stat.pattern_sequence,
                  stat.candidate_group)] = stat


def load_manual_brain(
    conn: sqlite3.Connection,
    model_version: int,
    definitions: Sequence[PatternDefinition],
    recent_window: int = DEFAULT_RECENT_WINDOW,
    region_id: str = "region-A",
) -> ManualBrain:
    """Rehydrate a :class:`ManualBrain` from the published derived model."""
    brain = ManualBrain(definitions, recent_window=recent_window)

    rows = conn.execute(
        """SELECT pattern_length, pattern_sequence, candidate_group,
                  opportunities, resolved, correct, incorrect, recent_outcomes,
                  first_seen, last_seen, pattern_kind, definition_id
           FROM pattern_stats
           WHERE model_version = ? AND definition_id IS NOT NULL""",
        (int(model_version),),
    ).fetchall()
    for row in rows:
        kind = row["pattern_kind"]
        if kind == STAT_RANGE:
            _load_stat(brain.range_engine, row, recent_window)
        elif kind == STAT_MOVEMENT:
            _load_stat(brain.movement_engine, row, recent_window)
        elif kind == STAT_RANGE_MOVEMENT:
            counts = brain.range_movement_counts.setdefault(
                int(row["definition_id"]), {})
            counts[str(row["candidate_group"])] = int(row["opportunities"] or 0)
        elif kind == STAT_MOVEMENT_RANGE:
            counts = brain.movement_range_counts.setdefault(
                int(row["definition_id"]), {})
            counts[str(row["candidate_group"])] = int(row["opportunities"] or 0)

    # Rebuild just enough of the chronological tail for pattern lookups.
    range_len = max((len(d.tokens) for d in definitions
                     if d.kind == KIND_RANGE and d.enabled), default=0)
    movement_len = max((len(d.tokens) for d in definitions
                        if d.kind == KIND_MOVEMENT and d.enabled), default=0)
    tail_n = max(range_len, movement_len + 1, 2)
    tail = conn.execute(
        """SELECT group_id, numeric_value, capture_timestamp
           FROM readings WHERE region_id = ?
           ORDER BY reading_id DESC LIMIT ?""",
        (region_id, tail_n),
    ).fetchall()
    tail = list(reversed(tail))

    if range_len:
        groups = [r["group_id"] for r in tail if r["group_id"] is not None]
        brain.range_engine.sequence = groups[-range_len:]

    if movement_len:
        values = [r["numeric_value"] for r in tail
                  if r["numeric_value"] is not None]
        movements = [
            movement_between(values[i - 1], values[i])
            for i in range(1, len(values))
        ]
        brain.movement_engine.sequence = movements[-movement_len:]

    last = tail[-1] if tail else None
    brain.restore_position(
        last["numeric_value"] if last is not None else None,
        last["capture_timestamp"] if last is not None else None,
    )
    brain.prime_pending()

    # Restore the outstanding range prediction so its outcome can resolve.
    pending = conn.execute(
        """SELECT prediction_id, pattern_length, pattern_sequence,
                  candidate_group, confidence, resolved_count, correct_count,
                  recent_accuracy, created_at, basis_reading_id,
                  expected_reading_id, pattern_kind, definition_id,
                  next_movement, next_movement_confidence
           FROM predictions
           WHERE model_version = ? AND resolved_at IS NULL
             AND definition_id IS NOT NULL AND pattern_kind = ?
           ORDER BY prediction_id DESC LIMIT 1""",
        (int(model_version), STAT_RANGE),
    ).fetchone()
    if pending is not None:
        brain.range_engine.set_pending(Prediction(
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
            pattern_kind=pending["pattern_kind"],
            definition_id=pending["definition_id"],
            next_movement=pending["next_movement"],
            next_movement_confidence=pending["next_movement_confidence"],
        ))

    # Movement forecasts are informational and never persisted, so recompute
    # the pending one from the restored movement sequence.
    brain.refresh_movement_prediction(
        now=last["capture_timestamp"] if last is not None else None)
    return brain


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
                  first_seen, last_seen, pattern_kind, definition_id
           FROM pattern_stats
           WHERE model_version = ? AND definition_id IS NOT NULL
             AND pattern_kind = ?""",
        (model_version, STAT_RANGE),
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
            pattern_kind=r["pattern_kind"],
            definition_id=r["definition_id"],
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
                  created_at, basis_reading_id, expected_reading_id,
                  pattern_kind, definition_id
           FROM predictions
           WHERE model_version = ? AND resolved_at IS NULL
             AND definition_id IS NOT NULL AND pattern_kind = ?
           ORDER BY prediction_id DESC LIMIT 1""",
        (model_version, STAT_RANGE),
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
            pattern_kind=pending["pattern_kind"],
            definition_id=pending["definition_id"],
        ))
    return engine


def pending_prediction_id(conn: sqlite3.Connection, model_version: int) -> Optional[int]:
    row = conn.execute(
        """SELECT prediction_id FROM predictions
           WHERE model_version = ? AND resolved_at IS NULL
             AND definition_id IS NOT NULL
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
                first_seen, last_seen, pattern_kind, definition_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_version, pattern_length, pattern_sequence, candidate_group)
               DO UPDATE SET
                   opportunities = excluded.opportunities,
                   resolved = excluded.resolved,
                   correct = excluded.correct,
                   incorrect = excluded.incorrect,
                   recent_outcomes = excluded.recent_outcomes,
                   last_seen = excluded.last_seen,
                   pattern_kind = excluded.pattern_kind,
                   definition_id = excluded.definition_id""",
            (
                model_version, row["pattern_length"], row["pattern_sequence"],
                row["candidate_group"], row["opportunities"], row["resolved"],
                row["correct"], row["incorrect"],
                json.dumps(row.get("recent_outcomes", [])),
                row.get("first_seen"), row.get("last_seen"),
                row.get("pattern_kind", STAT_RANGE), row.get("definition_id"),
            ),
        )


def insert_prediction(conn: sqlite3.Connection, model_version: int,
                      region_id: str, prediction: Prediction) -> int:
    cursor = conn.execute(
        """INSERT INTO predictions
           (model_version, created_at, region_id, pattern_length, pattern_sequence,
            candidate_group, confidence, resolved_count, correct_count,
            recent_accuracy, eligible, basis_reading_id, expected_reading_id,
            pattern_kind, definition_id, next_movement, next_movement_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
        (
            model_version, prediction.created_at or _now(), region_id,
            prediction.pattern_length, prediction.pattern_sequence,
            prediction.candidate_group, prediction.confidence,
            prediction.resolved_count, prediction.correct_count,
            prediction.recent_accuracy, prediction.basis_reading_id,
            prediction.expected_reading_id, prediction.pattern_kind,
            prediction.definition_id, prediction.next_movement,
            prediction.next_movement_confidence,
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


def apply_manual_learning(
    conn: sqlite3.Connection,
    model_version: int,
    region_id: str,
    observation: ManualObservation,
    pending_id: Optional[int],
) -> Tuple[bool, Optional[int]]:
    """Persist one incremental manual-brain step.

    Both the primary statistics (range + movement) and the cross-outcome rows
    are upserted.  Only the range prediction is persisted, because only it can
    drive a click; the movement forecast is attached to that same row.

    Returns ``(resolved_previous, new_prediction_id)``.
    """
    resolved_previous = False
    result = observation.range_result
    if result.resolved is not None and pending_id is not None:
        resolve_prediction(conn, pending_id, result.resolved)
        resolved_previous = True

    rows: List[Dict] = []
    if result.updated_stats:
        rows.extend(result.updated_stats)
    if observation.movement_result is not None and \
            observation.movement_result.updated_stats:
        rows.extend(observation.movement_result.updated_stats)
    if observation.cross_stats:
        rows.extend(observation.cross_stats)
    if rows:
        upsert_pattern_stats(conn, model_version, rows)

    new_id = None
    if observation.prediction is not None:
        new_id = insert_prediction(
            conn, model_version, region_id, observation.prediction)

    conn.commit()
    return resolved_previous, new_id


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

    where = "model_version = ? AND definition_id IS NOT NULL"
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
    """Number of manual pattern rows, optionally narrowed to some groups."""
    extra, params = _group_filter(group_ids)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM pattern_stats "
        f"WHERE model_version = ? AND definition_id IS NOT NULL{extra}",
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
            WHERE model_version = ? AND definition_id IS NOT NULL
              AND resolved_at IS NOT NULL{extra}
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
                       only_unresolved: bool = False,
                       model_version: Optional[int] = None) -> List[sqlite3.Row]:
    """Newest predictions, always restricted to manual patterns.

    ``model_version`` keeps the Learning page on the currently published model
    so legacy auto-generated predictions can never resurface.
    """
    where = ["definition_id IS NOT NULL"]
    params: List = []
    if model_version is not None:
        where.append("model_version = ?")
        params.append(int(model_version))
    if only_unresolved:
        where.append("resolved_at IS NULL")
    return conn.execute(
        f"""SELECT * FROM predictions WHERE {' AND '.join(where)}
            ORDER BY prediction_id DESC LIMIT ?""",
        [*params, limit],
    ).fetchall()


def top_patterns_from_db(conn: sqlite3.Connection, model_version: int,
                         limit: int = 100) -> List[sqlite3.Row]:
    return conn.execute(
        """SELECT pattern_length, pattern_sequence, candidate_group, opportunities,
                  resolved, correct, incorrect, recent_outcomes,
                  pattern_kind, definition_id
           FROM pattern_stats
           WHERE model_version = ? AND definition_id IS NOT NULL
           ORDER BY opportunities DESC, pattern_length ASC LIMIT ?""",
        (model_version, limit),
    ).fetchall()


def manual_pattern_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pattern_definitions").fetchone()
    return int(row["n"] or 0)


def manual_pattern_summaries(conn: sqlite3.Connection,
                             model_version: int) -> List[Dict]:
    """One summary row per user-defined pattern for the Learning page."""
    definitions = list_pattern_definitions(conn)
    rows = conn.execute(
        """SELECT definition_id, pattern_kind, candidate_group,
                  opportunities, resolved, correct, incorrect
           FROM pattern_stats
           WHERE model_version = ? AND definition_id IS NOT NULL""",
        (int(model_version),),
    ).fetchall()

    grouped: Dict[int, Dict[str, Dict[str, Dict]]] = {}
    for row in rows:
        definition_id = int(row["definition_id"])
        kind = str(row["pattern_kind"])
        candidate = str(row["candidate_group"])
        grouped.setdefault(definition_id, {}).setdefault(kind, {})[candidate] = {
            "candidate": candidate,
            "opportunities": int(row["opportunities"] or 0),
            "resolved": int(row["resolved"] or 0),
            "correct": int(row["correct"] or 0),
            "incorrect": int(row["incorrect"] or 0),
        }

    summaries: List[Dict] = []
    for definition in definitions:
        buckets = grouped.get(definition.pattern_id, {})
        if definition.kind == KIND_RANGE:
            primary_kind, other_kind = STAT_RANGE, STAT_RANGE_MOVEMENT
        else:
            primary_kind, other_kind = STAT_MOVEMENT, STAT_MOVEMENT_RANGE

        primary = list(buckets.get(primary_kind, {}).values())
        other = list(buckets.get(other_kind, {}).values())
        matches = sum(item["opportunities"] for item in primary)
        resolved = sum(item["resolved"] for item in primary)
        correct = sum(item["correct"] for item in primary)
        incorrect = sum(item["incorrect"] for item in primary)
        best_primary = max(primary, key=lambda x: x["opportunities"],
                           default=None)
        best_other = max(other, key=lambda x: x["opportunities"], default=None)

        summaries.append({
            "pattern_id": definition.pattern_id,
            "kind": definition.kind,
            "tokens": list(definition.tokens),
            "sequence": definition.sequence_text,
            "label": definition.label,
            "enabled": definition.enabled,
            "length": definition.length,
            "expected_kind": definition.expected_kind,
            "expected_value": definition.expected_value,
            "matches": matches,
            "expected_hits": 0,
            "expected_misses": 0,
            "resolved": resolved,
            "correct": correct,
            "incorrect": incorrect,
            "accuracy": (100.0 * correct / resolved) if resolved else None,
            "best_next_range": (
                (best_primary if definition.kind == KIND_RANGE else best_other)
                or {}).get("candidate"),
            "best_next_movement": (
                (best_primary if definition.kind == KIND_MOVEMENT else best_other)
                or {}).get("candidate"),
        })
    return summaries


def pattern_outcomes(conn: sqlite3.Connection, model_version: int,
                     definition_id: int) -> Dict[str, List[Dict]]:
    """Next-range and next-movement distributions for one definition."""
    definition = get_pattern_definition(conn, definition_id)
    if definition is None:
        return {"next_ranges": [], "next_movements": []}

    if definition.kind == KIND_RANGE:
        range_kind, movement_kind = STAT_RANGE, STAT_RANGE_MOVEMENT
    else:
        range_kind, movement_kind = STAT_MOVEMENT_RANGE, STAT_MOVEMENT

    rows = conn.execute(
        """SELECT pattern_kind, candidate_group, opportunities, resolved,
                  correct, incorrect
           FROM pattern_stats
           WHERE model_version = ? AND definition_id = ?
           ORDER BY opportunities DESC, candidate_group ASC""",
        (int(model_version), int(definition_id)),
    ).fetchall()

    def bucket(kind: str) -> List[Dict]:
        out: List[Dict] = []
        for row in rows:
            if row["pattern_kind"] != kind:
                continue
            resolved = int(row["resolved"] or 0)
            correct = int(row["correct"] or 0)
            out.append({
                "candidate": str(row["candidate_group"]),
                "opportunities": int(row["opportunities"] or 0),
                "resolved": resolved,
                "correct": correct,
                "incorrect": int(row["incorrect"] or 0),
                "accuracy": (100.0 * correct / resolved) if resolved else None,
            })
        return out

    return {
        "next_ranges": bucket(range_kind),
        "next_movements": bucket(movement_kind),
    }


def recent_click_audit(conn: sqlite3.Connection, limit: int = 200) -> List[sqlite3.Row]:
    return conn.execute(
        """SELECT audit_id, created_at, prediction_id, outcome, block_code, detail,
                  target_x, target_y, dry_run, region_a_count
           FROM click_audit ORDER BY audit_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
