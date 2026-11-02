"""Storage / save-mode behaviour tests.

Driven through :func:`src.learning.repository.insert_reading` and
:mod:`src.monitor.pipeline`, plus the legacy ``detections`` -> ``readings``
migration performed by :func:`src.database.init_db.initialise_database`.
"""

import os
import sqlite3
import tempfile

import pytest

from src.config.ranges import DEFAULT_GROUPS, RangeConfig
from src.database.init_db import (
    REGION_A_ID,
    REGION_B_ID,
    count_valid_region_a_readings,
    initialise_database,
)
from src.learning import repository as learn_repo
from src.monitor import pipeline

# Minimal byte-compatible v1 ``detections`` definition (mirrors init_db).
_DETECTIONS_DDL = """
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
"""

_V1_TABLES = {"settings", "regions", "detections", "scan_sessions"}
_V2_TABLES = {
    "schema_meta", "readings", "groups", "range_config_versions", "model_versions",
    "pattern_stats", "predictions", "click_audit", "diagnostics",
}
_V3_TABLES = {"region_b_readings"}
_V4_TABLES = {"pattern_definitions"}


def _cleanup(tmp):
    for suffix in ("", "-wal", "-shm"):
        path = tmp + suffix
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


@pytest.fixture
def temp_db():
    tmp = tempfile.mktemp(suffix=".db")
    conn = initialise_database(tmp)
    try:
        yield conn
    finally:
        conn.close()
        _cleanup(tmp)


def _cand(canonical, confidence=95.0):
    return {"text": canonical, "confidence": confidence, "canonical_value": canonical}


def _scan(conn, canonical, mode, region_id=REGION_A_ID):
    """Reproduce one scan cycle: evaluate then store only when VALID."""
    previous = learn_repo.last_reading(conn, region_id)
    previous_canonical = previous["canonical_value"] if previous is not None else None
    evaluation = pipeline.evaluate_candidates(
        [_cand(canonical)],
        RangeConfig(DEFAULT_GROUPS),
        min_confidence=70.0,
        mode=mode,
        previous_canonical=previous_canonical,
        require_range=(region_id != REGION_B_ID),
    )
    if evaluation.is_valid:
        learn_repo.insert_reading(
            conn,
            region_id=region_id,
            canonical_value=evaluation.canonical_value,
            numeric_value=evaluation.numeric_value,
            original_text=evaluation.original_text,
            ocr_confidence=evaluation.ocr_confidence,
            group_id=evaluation.group_id,
            range_config_version=None,
            capture_timestamp="2026-01-01T00:00:00",
            processing_timestamp="2026-01-01T00:00:00",
            session_id="session-test",
        )
    return evaluation


def _count(conn, region_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE region_id = ?", (region_id,)
    ).fetchone()["n"]


class TestSaveModes:
    def test_every_mode_stores_both_identical_values(self, temp_db):
        conn = temp_db
        ev1 = _scan(conn, "1.25", pipeline.MODE_EVERY)
        ev2 = _scan(conn, "1.25", pipeline.MODE_EVERY)
        assert ev1.status == pipeline.VALID
        assert ev2.status == pipeline.VALID
        assert _count(conn, REGION_A_ID) == 2

    def test_on_change_mode_suppresses_second_identical_value(self, temp_db):
        conn = temp_db
        ev1 = _scan(conn, "1.25", pipeline.MODE_ON_CHANGE)
        ev2 = _scan(conn, "1.25", pipeline.MODE_ON_CHANGE)
        assert ev1.status == pipeline.VALID
        assert ev2.status == pipeline.DUPLICATE
        assert _count(conn, REGION_A_ID) == 1


class TestRegionBRangeBypass:
    def test_region_b_stores_zero_even_though_it_is_out_of_range(self, temp_db):
        conn = temp_db
        evaluation = _scan(conn, "0.00", pipeline.MODE_EVERY,
                           region_id=REGION_B_ID)
        assert evaluation.status == pipeline.VALID
        assert evaluation.group_id is None
        row = conn.execute(
            "SELECT group_id, numeric_value FROM readings WHERE region_id = ?",
            (REGION_B_ID,),
        ).fetchone()
        assert row is not None
        assert row["group_id"] is None
        assert row["numeric_value"] == pytest.approx(0.0)

    def test_region_b_still_rejects_non_numeric_text(self, temp_db):
        conn = temp_db
        evaluation = _scan(conn, "NGN", pipeline.MODE_EVERY,
                           region_id=REGION_B_ID)
        assert evaluation.status == pipeline.INVALID_TEXT
        assert _count(conn, REGION_B_ID) == 0

    def test_region_a_still_rejects_out_of_range(self, temp_db):
        conn = temp_db
        evaluation = _scan(conn, "0.00", pipeline.MODE_EVERY,
                           region_id=REGION_A_ID)
        assert evaluation.status == pipeline.OUT_OF_RANGE
        assert _count(conn, REGION_A_ID) == 0


class TestRegionAGateCounting:
    def test_region_b_readings_do_not_count(self, temp_db):
        conn = temp_db
        _scan(conn, "1.25", pipeline.MODE_EVERY, region_id=REGION_A_ID)
        assert count_valid_region_a_readings(conn) == 1

        for _ in range(5):
            _scan(conn, "2.10", pipeline.MODE_EVERY, region_id=REGION_B_ID)

        # Region B rows are stored ...
        assert _count(conn, REGION_B_ID) == 5
        # ... but excluded from the gate count.
        assert count_valid_region_a_readings(conn) == 1

    def test_count_is_region_a_only(self, temp_db):
        conn = temp_db
        for i in range(3):
            learn_repo.insert_reading(
                conn, region_id=REGION_B_ID, canonical_value=f"2.1{i}",
                numeric_value=2.1, original_text="2.10", ocr_confidence=99.0,
                group_id="g4", range_config_version=1)
        assert count_valid_region_a_readings(conn) == 0
        learn_repo.insert_reading(
            conn, region_id=REGION_A_ID, canonical_value="1.25",
            numeric_value=1.25, original_text="1.25", ocr_confidence=99.0,
            group_id="g1", range_config_version=1)
        assert count_valid_region_a_readings(conn) == 1


class TestLegacyMigration:
    def test_fresh_db_keeps_empty_detections_table(self, temp_db):
        conn = temp_db
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='detections'"
        ).fetchone() is not None
        assert conn.execute("SELECT COUNT(*) AS n FROM detections").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='readings'"
        ).fetchone() is not None

    def test_pre_existing_detections_are_copied_into_readings(self):
        """A v1-style database is migrated on first initialise."""
        tmp = tempfile.mktemp(suffix=".db")
        try:
            raw = sqlite3.connect(tmp)
            raw.executescript(_DETECTIONS_DDL)
            raw.execute(
                """INSERT INTO detections
                   (region_id, canonical_value, original_text, ocr_confidence,
                    screen_x, screen_y, monitor_id, capture_timestamp,
                    processing_timestamp, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("region-001", "125.50", "125.50", 95.0, 10, 20, "mon-1",
                 "2026-01-01T00:00:00", "2026-01-01T00:00:01", "legacy-session"),
            )
            raw.commit()
            raw.close()

            conn = initialise_database(tmp)
            try:
                migrated = conn.execute(
                    "SELECT * FROM readings WHERE legacy_detection_id IS NOT NULL"
                ).fetchall()
                assert len(migrated) == 1
                row = migrated[0]
                assert row["legacy_detection_id"] == 1
                # Historical devices had no Region B: legacy rows land on Region A.
                assert row["region_id"] == REGION_A_ID
                assert row["legacy_region_id"] == "region-001"
                assert row["source"] == "migrated"
                assert row["canonical_value"] == "125.50"
                assert row["numeric_value"] == pytest.approx(125.50)

                # The original detections row is untouched.
                det = conn.execute(
                    "SELECT * FROM detections WHERE detection_id = 1"
                ).fetchone()
                assert det is not None
                assert det["canonical_value"] == "125.50"
                assert det["session_id"] == "legacy-session"
                assert count_valid_region_a_readings(conn) == 1
            finally:
                conn.close()
        finally:
            _cleanup(tmp)

    def test_migration_is_one_shot(self, temp_db):
        """Rows inserted *after* the first initialise are not re-migrated.

        ``_migrate_legacy_detections`` records a one-shot marker, so the legacy
        copy happens exactly once, on the first upgrade of a v1 database.
        """
        conn = temp_db
        conn.execute(
            """INSERT INTO detections
               (region_id, canonical_value, original_text, ocr_confidence,
                capture_timestamp, processing_timestamp, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("region-001", "9.99", "9.99", 95.0,
             "2026-01-01T00:00:00", "2026-01-01T00:00:00", "s"),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"] == 0

        # Re-initialising does not re-run the migration (marker already set).
        conn2 = initialise_database(conn.execute("PRAGMA database_list").fetchone()["file"])
        try:
            assert conn2.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"] == 0
            assert conn2.execute(
                "SELECT value FROM schema_meta WHERE key='legacy_detections_migrated'"
            ).fetchone()["value"] == "1"
        finally:
            conn2.close()

    def test_upgrade_purges_only_legacy_derived_rows(self, temp_db):
        conn = temp_db
        conn.execute(
            """INSERT INTO pattern_stats
               (model_version, pattern_length, pattern_sequence,
                candidate_group, opportunities, pattern_kind, definition_id)
               VALUES (1, 4, 'g1,g2,g3,g4', 'g1', 10, 'range', NULL)"""
        )
        conn.execute(
            "UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")
        conn.commit()
        db_file = conn.execute("PRAGMA database_list").fetchone()["file"]

        conn2 = initialise_database(db_file)
        try:
            assert conn2.execute(
                "SELECT COUNT(*) AS n FROM pattern_stats "
                "WHERE definition_id IS NULL"
            ).fetchone()["n"] == 0
        finally:
            conn2.close()

    def test_repeated_initialise_keeps_manual_rows_and_settings(self, temp_db):
        conn = temp_db
        pattern_id = learn_repo.create_pattern_definition(
            conn, "range", ("g1", "g2"))
        conn.execute(
            """INSERT INTO pattern_stats
               (model_version, pattern_length, pattern_sequence,
                candidate_group, opportunities, pattern_kind, definition_id)
               VALUES (1, 2, 'g1,g2', 'g1', 10, 'range', ?)""",
            (pattern_id,),
        )
        conn.execute(
            "UPDATE settings SET value = '0' WHERE key = 'model_stale'")
        conn.execute(
            "UPDATE settings SET value = '1' WHERE key = 'auto_click_enabled'")
        conn.commit()
        db_file = conn.execute("PRAGMA database_list").fetchone()["file"]

        conn2 = initialise_database(db_file)
        try:
            assert conn2.execute(
                "SELECT value FROM settings WHERE key = 'model_stale'"
            ).fetchone()["value"] == "0"
            assert conn2.execute(
                "SELECT value FROM settings WHERE key = 'auto_click_enabled'"
            ).fetchone()["value"] == "1"
            assert conn2.execute(
                "SELECT COUNT(*) AS n FROM pattern_stats "
                "WHERE definition_id = ?", (pattern_id,)
            ).fetchone()["n"] == 1
        finally:
            conn2.close()

    def test_repeated_initialise_drops_nothing(self, temp_db):
        conn = temp_db
        db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
        conn2 = initialise_database(db_file)
        try:
            tables = {
                r["name"] for r in conn2.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert _V1_TABLES <= tables
            assert _V2_TABLES <= tables
            assert _V3_TABLES <= tables
            assert _V4_TABLES <= tables
            versions = {
                r["value"] for r in conn2.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'")
            }
            assert versions == {"4"}
        finally:
            conn2.close()
