"""Tests for the user-defined pattern brain.

The application must never invent patterns: every learned row has to come from
a ``pattern_definitions`` row the user added.  These tests cover movement
classification, range and movement patterns, the cross-outcome tables and the
round trip through "Recalculate App Brain".
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.config.repository import current_model_version, model_is_stale
from src.database.init_db import REGION_A_ID, initialise_database
from src.learning import repository as learn_repo
from src.learning.manual_brain import (
    KIND_MOVEMENT,
    KIND_RANGE,
    ManualBrain,
    PatternDefinition,
    STAT_MOVEMENT,
    STAT_MOVEMENT_RANGE,
    STAT_RANGE,
    STAT_RANGE_MOVEMENT,
    movement_between,
)
from src.learning.rebuild import recalculate_app_brain

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def temp_db():
    tmp = tempfile.mktemp(suffix=".db")
    conn = initialise_database(tmp)
    try:
        yield conn
    finally:
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            path = tmp + suffix
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _seed(conn, values, groups):
    rows = []
    for i, (value, group) in enumerate(zip(values, groups)):
        canonical = f"{value:.2f}"
        ts = (_BASE + timedelta(seconds=i)).isoformat()
        rows.append((REGION_A_ID, canonical, value, canonical, 99.0, group,
                     1, ts, ts, "session-test"))
    conn.executemany(
        """INSERT INTO readings
           (region_id, canonical_value, numeric_value, original_text,
            ocr_confidence, group_id, range_config_version,
            capture_timestamp, processing_timestamp, session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def _observe(brain, groups, values):
    for i, (group, value) in enumerate(zip(groups, values)):
        brain.observe(group, value, now=(_BASE + timedelta(seconds=i)).isoformat())


def _row_map(brain):
    return {
        (row["pattern_kind"], row["candidate_group"]): row
        for row in brain.all_stat_rows()
    }


class TestMovement:
    @pytest.mark.parametrize("previous,current,expected", [
        (1.00, 1.01, "UP"),
        (1.01, 1.00, "DOWN"),
        (1.00, 1.004, "FLAT"),   # same at two decimal places
        (1.00, 0.995, "FLAT"),
        (0.00, 0.01, "UP"),
    ])
    def test_two_decimal_classification(self, previous, current, expected):
        assert movement_between(previous, current) == expected


class TestRangePattern:
    def test_learns_next_range_and_next_movement(self):
        definition = PatternDefinition(1, KIND_RANGE, ("g1", "g2"))
        brain = ManualBrain([definition])
        groups = ["g1", "g2", "g1", "g2", "g1"]
        values = [1.00, 2.00, 1.00, 2.00, 1.00]
        _observe(brain, groups, values)

        rows = _row_map(brain)
        assert rows[(STAT_RANGE, "g1")]["opportunities"] == 2
        # Every g1 -> g2 ending was followed by a DOWN move (2.00 -> 1.00).
        assert rows[(STAT_RANGE_MOVEMENT, "DOWN")]["opportunities"] == 2
        # The pattern is defined by the user, not discovered.
        assert all(row["definition_id"] == 1
                   for row in brain.all_stat_rows())

    def test_no_definition_means_no_learning(self):
        brain = ManualBrain([])
        _observe(brain, ["g1", "g2", "g1", "g2"],
                 [1.00, 2.00, 1.00, 2.00])
        assert brain.all_stat_rows() == []
        assert brain.range_engine.pending is None

    def test_unrelated_sequence_is_not_learned(self):
        definition = PatternDefinition(1, KIND_RANGE, ("g1", "g2"))
        brain = ManualBrain([definition])
        _observe(brain, ["g1", "g3", "g1", "g3"],
                 [1.00, 1.60, 1.00, 1.60])
        assert brain.all_stat_rows() == []

    def test_disabled_definition_is_not_learned(self):
        definition = PatternDefinition(
            1, KIND_RANGE, ("g1", "g2"), enabled=False)
        brain = ManualBrain([definition])
        _observe(brain, ["g1", "g2", "g1", "g2"],
                 [1.00, 2.00, 1.00, 2.00])
        assert brain.all_stat_rows() == []


class TestMovementPattern:
    def test_learns_next_movement_and_next_range(self):
        definition = PatternDefinition(1, KIND_MOVEMENT, ("UP", "DOWN"))
        brain = ManualBrain([definition])
        groups = ["g1", "g2", "g1", "g2", "g1"]
        values = [1.00, 2.00, 1.00, 2.00, 1.00]
        _observe(brain, groups, values)

        rows = _row_map(brain)
        assert rows[(STAT_MOVEMENT, "UP")]["opportunities"] == 1
        # The UP, DOWN movement pattern ended at the third reading, whose next
        # range was g2.
        assert rows[(STAT_MOVEMENT_RANGE, "g2")]["opportunities"] == 1


class TestManualPatternRoundTrip:
    def test_rebuild_without_patterns_publishes_an_empty_valid_model(self, temp_db):
        conn = temp_db
        _seed(conn, [1.10, 1.30, 1.10, 1.30],
              ["g1", "g2", "g1", "g2"])

        result = recalculate_app_brain(conn)

        assert result["ok"] is True
        assert result["patterns"] == 0
        assert model_is_stale(conn) is False
        assert current_model_version(conn) == result["model_version"]
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM pattern_stats"
        ).fetchone()["n"] == 0

    def test_definitions_drive_outcomes_after_rebuild(self, temp_db):
        conn = temp_db
        _seed(conn, [1.10, 1.30] * 10, ["g1", "g2"] * 10)
        pattern_id = learn_repo.create_pattern_definition(
            conn, KIND_RANGE, ("g1", "g2"), "alternating")

        result = recalculate_app_brain(conn)

        assert result["ok"] is True
        summaries = learn_repo.manual_pattern_summaries(
            conn, result["model_version"])
        assert len(summaries) == 1
        assert summaries[0]["pattern_id"] == pattern_id
        assert summaries[0]["matches"] > 0

        outcomes = learn_repo.pattern_outcomes(
            conn, result["model_version"], pattern_id)
        assert any(row["candidate"] == "g1"
                   for row in outcomes["next_ranges"])
        assert any(row["candidate"] in ("UP", "DOWN")
                   for row in outcomes["next_movements"])

    def test_deleting_a_pattern_removes_only_its_derived_rows(self, temp_db):
        conn = temp_db
        _seed(conn, [1.10, 1.30] * 10, ["g1", "g2"] * 10)
        first = learn_repo.create_pattern_definition(
            conn, KIND_RANGE, ("g1", "g2"), "first")
        second = learn_repo.create_pattern_definition(
            conn, KIND_RANGE, ("g2", "g1"), "second")
        recalculate_app_brain(conn)

        assert learn_repo.delete_pattern_definition(conn, first) is True

        assert learn_repo.get_pattern_definition(conn, first) is None
        assert learn_repo.get_pattern_definition(conn, second) is not None
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM pattern_stats WHERE definition_id = ?",
            (first,),
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM readings"
        ).fetchone()["n"] == 20
