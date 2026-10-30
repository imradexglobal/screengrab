"""Tests for "Recalculate App Brain" (:mod:`src.learning.rebuild`).

Covers successful publication, idempotence, preservation of raw readings, the
failure path (atomicity + safety), and reload determinism.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.config.repository import (
    current_model_version,
    get_setting,
    load_range_config,
    model_is_stale,
    set_setting,
)
from src.database.init_db import REGION_A_ID, initialise_database
from src.learning import repository as learn_repo
from src.learning import rebuild as rebuild_mod
from src.learning.patterns import LearningEngine
from src.learning.rebuild import recalculate_app_brain

#: A numeric value firmly inside each default group.
_GROUP_VALUE = {
    "g1": 1.10, "g2": 1.30, "g3": 1.60, "g4": 2.10, "g5": 3.00,
    "g6": 6.00, "g7": 12.00, "g8": 30.00, "g9": 200.00,
}


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


def _seed(conn, groups):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i, gid in enumerate(groups):
        value = _GROUP_VALUE[gid]
        canonical = f"{value:.2f}"
        ts = (base + timedelta(seconds=i)).isoformat()
        rows.append((REGION_A_ID, canonical, value, canonical, 99.0, None, gid,
                     1, ts, ts, "session-test", 0, 0, "mon", None, "live"))
    conn.executemany(
        """INSERT INTO readings
           (region_id, canonical_value, numeric_value, original_text,
            ocr_confidence, quality_meta, group_id, range_config_version,
            capture_timestamp, processing_timestamp, session_id, screen_x,
            screen_y, monitor_id, monitor_geometry, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return [gid for gid in groups]


def _sequence(repeats=60):
    return ["g1", "g2", "g3", "g4"] * repeats


def _db_stats(conn, model_version):
    return {
        (r["pattern_length"], r["pattern_sequence"], r["candidate_group"]):
            (r["opportunities"], r["resolved"], r["correct"], r["incorrect"])
        for r in conn.execute(
            "SELECT pattern_length, pattern_sequence, candidate_group, "
            "opportunities, resolved, correct, incorrect FROM pattern_stats "
            "WHERE model_version = ? ORDER BY pattern_length, pattern_sequence, "
            "candidate_group",
            (model_version,))
    }


def _engine_stats(engine):
    return {
        (s.pattern_length, s.pattern_sequence, s.candidate_group):
            (s.opportunities, s.resolved, s.correct, s.incorrect)
        for s in engine.stats.values()
    }


def _readings_snapshot(conn):
    return [
        (r["reading_id"], r["canonical_value"], r["numeric_value"], r["group_id"])
        for r in conn.execute(
            "SELECT reading_id, canonical_value, numeric_value, group_id "
            "FROM readings ORDER BY reading_id")
    ]


class TestSuccessfulRecalculation:
    def test_publishes_a_new_valid_model(self, temp_db):
        conn = temp_db
        groups = _seed(conn, _sequence())

        result = recalculate_app_brain(conn)

        assert result["ok"] is True
        assert result["model_version"] is not None
        assert result["source_count"] == len(groups)
        assert model_is_stale(conn) is False
        assert get_setting(conn, "model_stale", "1") == "0"
        assert current_model_version(conn) == result["model_version"]

        patterns = conn.execute(
            "SELECT COUNT(*) AS n FROM pattern_stats WHERE model_version = ?",
            (result["model_version"],)).fetchone()["n"]
        assert patterns > 0

        # Every reading now carries a derived classification.
        unclassified = conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE group_id IS NULL"
        ).fetchone()["n"]
        assert unclassified == 0

    def test_recalculation_is_idempotent(self, temp_db):
        conn = temp_db
        _seed(conn, _sequence())

        first = recalculate_app_brain(conn)
        second = recalculate_app_brain(conn)

        assert first["ok"] and second["ok"]
        assert first["model_version"] != second["model_version"]
        assert _db_stats(conn, first["model_version"]) == \
               _db_stats(conn, second["model_version"])

    def test_raw_readings_are_preserved(self, temp_db):
        conn = temp_db
        groups = _seed(conn, _sequence())
        before = _readings_snapshot(conn)

        recalculate_app_brain(conn)

        after = _readings_snapshot(conn)
        assert len(after) == len(groups)
        # reading_id / canonical_value / numeric_value must be untouched.
        assert [(r[0], r[1], r[2]) for r in after] == \
               [(r[0], r[1], r[2]) for r in before]


class TestFailurePath:
    def test_failure_keeps_previous_model_and_stays_safe(self, temp_db,
                                                         monkeypatch):
        conn = temp_db
        _seed(conn, _sequence())

        good = recalculate_app_brain(conn)
        assert good["ok"] is True
        previous_version = good["model_version"]
        readings_before = _readings_snapshot(conn)

        set_setting(conn, "auto_click_enabled", "1")
        conn.commit()

        class _BoomEngine:
            def __init__(self, *args, **kwargs):
                pass

            def observe(self, *args, **kwargs):
                raise RuntimeError("forced failure")

        monkeypatch.setattr(rebuild_mod, "LearningEngine", _BoomEngine)

        result = recalculate_app_brain(conn)

        assert result["ok"] is False
        assert result["error"]
        # The previously published model is still the current one.
        assert current_model_version(conn) == previous_version
        # Clicking stays blocked and the stale flag is raised.
        assert model_is_stale(conn) is True
        assert get_setting(conn, "model_stale", "1") == "1"
        assert get_setting(conn, "auto_click_enabled", "1") == "0"
        # The failed attempt left nothing half-written.
        failed_version = conn.execute(
            "SELECT MAX(version) AS v FROM model_versions").fetchone()["v"]
        assert failed_version != previous_version
        status = conn.execute(
            "SELECT status FROM model_versions WHERE version = ?",
            (failed_version,)).fetchone()["status"]
        assert status == "FAILED"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM pattern_stats WHERE model_version = ?",
            (failed_version,)).fetchone()["n"] == 0
        # Readings are byte-for-byte unchanged.
        assert _readings_snapshot(conn) == readings_before


class TestReloadDeterminism:
    def test_reloaded_engine_matches_a_fresh_replay(self, temp_db):
        conn = temp_db
        groups = _seed(conn, _sequence())

        result = recalculate_app_brain(conn)
        assert result["ok"] is True
        model_version = result["model_version"]

        config = load_range_config(conn)
        fresh = LearningEngine(abbreviation_fn=config.abbreviation)
        for gid in groups:
            fresh.observe(gid)

        # The published pattern statistics equal a from-scratch replay.
        assert _db_stats(conn, model_version) == _engine_stats(fresh)

        # Rehydrating from the database yields the same next prediction.
        loaded = learn_repo.load_engine_state(
            conn, model_version, abbreviation_fn=config.abbreviation)
        assert loaded.predict_next() == fresh.predict_next()
