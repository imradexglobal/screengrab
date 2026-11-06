"""Tests for the manual sequence-test trigger."""

import os
import tempfile

import pytest

from src.automation.clicker import AutoClickEngine, RecordingDispatcher
from src.config.repository import set_click_target, set_setting
from src.database.init_db import REGION_A_ID, initialise_database


@pytest.fixture
def conn():
    path = tempfile.mktemp(suffix=".db")
    db = initialise_database(path)
    try:
        yield db
    finally:
        db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def seed_region_a(db, count):
    stamp = "2026-11-04T20:00:00+00:00"
    row = (REGION_A_ID, "1", 1.0, "1", 99.0, None, "g1", 1,
           stamp, stamp, "manual-test", 0, 0, "test", None, "live")
    db.executemany(
        """INSERT INTO readings
           (region_id, canonical_value, numeric_value, original_text,
            ocr_confidence, quality_meta, group_id, range_config_version,
            capture_timestamp, processing_timestamp, session_id, screen_x,
            screen_y, monitor_id, monitor_geometry, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [row] * count,
    )
    db.commit()


def test_manual_test_dispatches_immediately_ignoring_dry_run_and_kill_switch(conn):
    seed_region_a(conn, 1000)
    set_click_target(conn, {"x": 100, "y": 200})
    set_setting(conn, "auto_click_enabled", "0")
    set_setting(conn, "auto_click_dry_run", "1")
    set_setting(conn, "model_stale", "1")
    conn.commit()

    dispatcher = RecordingDispatcher()
    engine = AutoClickEngine(conn, dispatcher=dispatcher)
    engine.kill()
    result = engine.test_sequence()

    assert result.allowed is True
    assert result.outcome == "DISPATCHED"
    assert dispatcher.calls == [(100, 200)]


def test_manual_test_does_not_require_region_a_records(conn):
    seed_region_a(conn, 999)
    set_click_target(conn, {"x": 100, "y": 200})
    set_setting(conn, "auto_click_dry_run", "1")
    conn.commit()

    dispatcher = RecordingDispatcher()
    engine = AutoClickEngine(conn, dispatcher=dispatcher)
    result = engine.test_sequence()

    assert result.allowed is True
    assert result.outcome == "DISPATCHED"
    assert dispatcher.calls == [(100, 200)]
