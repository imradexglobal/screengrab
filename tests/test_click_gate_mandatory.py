"""Regression tests for unrestricted click availability.

The click engine must not require a minimum number of Region A readings. These
checks keep the behavior explicit while continuing to exercise the real safety
configuration and the recording dispatcher.
"""

import os
import tempfile

import pytest

from src.automation.clicker import BLOCK_CODES, AutoClickEngine, RecordingDispatcher
from src.config.repository import set_allowed_categories, set_click_target, set_setting
from src.database.init_db import REGION_A_ID, REGION_B_ID, count_valid_region_a_readings, initialise_database
from src.learning import repository as learn_repo
from src.learning.patterns import Prediction


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


def _seed(conn, n, region_id=REGION_A_ID):
    now = "2026-01-01T00:00:00"
    rows = [
        (region_id, "1.25", 1.25, "1.25", 99.0, None, "g1", 1, now, now,
         "session-test", 0, 0, "mon", None, "live")
        for _ in range(n)
    ]
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


def configure(conn, region_a=0, **overrides):
    _seed(conn, region_a)
    settings = {
        "auto_click_enabled": "1",
        "auto_click_dry_run": "0",
        "model_stale": "0",
        "model_version": "1",
        "rebuild_in_progress": "0",
        "confidence_threshold": "70",
        "min_evidence": "30",
        "recent_safeguard_enabled": "1",
        "recent_min_accuracy": "60",
        "cooldown_seconds": "0",
    }
    settings.update({k: str(v) for k, v in overrides.items()})
    for key, value in settings.items():
        set_setting(conn, key, value)
    set_allowed_categories(conn, ["g1"])
    set_click_target(conn, {"x": 100, "y": 200})
    conn.commit()


def make_engine(conn):
    dispatcher = RecordingDispatcher()
    return AutoClickEngine(conn, dispatcher=dispatcher), dispatcher


def prediction(prediction_id=None, confidence=99.0, resolved_count=500,
               recent_accuracy=95.0, candidate="g1"):
    return {
        "prediction_id": prediction_id,
        "candidate_group": candidate,
        "confidence": confidence,
        "resolved_count": resolved_count,
        "recent_accuracy": recent_accuracy,
        "click_dispatched": 0,
    }


def test_click_is_allowed_with_zero_region_a_rows(temp_db):
    configure(temp_db, region_a=0)
    engine, dispatcher = make_engine(temp_db)

    decision = engine.try_click(prediction())

    assert decision.allowed is True
    assert decision.region_a_count == 0
    assert dispatcher.calls == [(100, 200)]


def test_click_is_allowed_with_any_available_row_count(temp_db):
    configure(temp_db, region_a=37)
    engine, dispatcher = make_engine(temp_db)

    decision = engine.try_click(prediction())

    assert decision.allowed is True
    assert decision.region_a_count == 37
    assert dispatcher.calls == [(100, 200)]


def test_region_b_rows_do_not_change_region_a_status(temp_db):
    configure(temp_db, region_a=0)
    _seed(temp_db, 5000, region_id=REGION_B_ID)
    engine, dispatcher = make_engine(temp_db)

    decision = engine.try_click(prediction())

    assert count_valid_region_a_readings(temp_db) == 0
    assert decision.allowed is True
    assert dispatcher.calls == [(100, 200)]


def test_gate_status_reports_count_without_a_threshold(temp_db):
    configure(temp_db, region_a=3)
    engine, _dispatcher = make_engine(temp_db)

    status = engine.gate_status()

    assert status["region_a_count"] == 3
    assert status["required"] is None
    assert status["remaining"] == 0
    assert status["unlocked"] is True


def test_other_safety_gates_still_apply(temp_db):
    configure(temp_db, region_a=0, auto_click_enabled="0")
    engine, dispatcher = make_engine(temp_db)

    decision = engine.try_click(prediction())

    assert decision.allowed is False
    assert decision.code == "AUTO_CLICK_DISABLED"
    assert decision.code in BLOCK_CODES
    assert dispatcher.calls == []


def test_duplicate_dispatch_is_still_blocked(temp_db):
    configure(temp_db, region_a=0)
    engine, dispatcher = make_engine(temp_db)

    pid = learn_repo.insert_prediction(
        temp_db, 1, REGION_A_ID,
        Prediction(pattern_length=4, pattern_sequence="NA,MI,MN,SM",
                   candidate_group="g1", confidence=99.0, resolved_count=500,
                   correct_count=490, recent_accuracy=95.0,
                   created_at="2026-01-01T00:00:00"))
    temp_db.commit()

    first = engine.try_click(prediction(prediction_id=pid))
    second = engine.try_click(prediction(prediction_id=pid))

    assert first.allowed is True
    assert second.allowed is False
    assert second.code == "DUPLICATE_ACTION"
    assert len(dispatcher.calls) == 1
