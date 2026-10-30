"""The mandatory 1,000-record Region A gate, enforced in the execution path.

This is the most important safety test file: :meth:`AutoClickEngine.try_click`
must refuse to dispatch unless the *persistent database* holds at least 1,000
qualifying Region A readings, no matter what any setting, UI value or cached
counter says.  A real mouse click is never dispatched - only the
``RecordingDispatcher`` is used.
"""

import os
import tempfile

import pytest

from src.automation.clicker import (
    BLOCK_CODES,
    MIN_REGION_A_RECORDS,
    AutoClickEngine,
    RecordingDispatcher,
)
from src.config.repository import (
    set_allowed_categories,
    set_click_target,
    set_setting,
)
from src.database.init_db import (
    REGION_A_ID,
    REGION_B_ID,
    count_valid_region_a_readings,
    initialise_database,
)
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


def configure(conn, region_a=1000, **overrides):
    """Seed readings and turn every unrelated gate condition into a pass."""
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


def _audit_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM click_audit").fetchone()["n"]


class TestMandatoryRegionAGate:
    @pytest.mark.parametrize("region_a", [0, 1, 999])
    def test_blocked_below_1000_even_when_everything_else_passes(self, temp_db,
                                                                region_a):
        conn = temp_db
        configure(conn, region_a=region_a)
        engine, dispatcher = make_engine(conn)

        decision = engine.try_click(prediction())

        assert decision.allowed is False
        assert decision.code == "REGION_A_RECORDS_BELOW_1000"
        assert dispatcher.calls == []
        assert count_valid_region_a_readings(conn) == region_a

    def test_eligible_at_exactly_1000_and_one_click_dispatched(self, temp_db):
        conn = temp_db
        configure(conn, region_a=1000)
        engine, dispatcher = make_engine(conn)

        decision = engine.try_click(prediction())

        assert decision.allowed is True
        assert decision.region_a_count == MIN_REGION_A_RECORDS
        assert len(dispatcher.calls) == 1
        assert _audit_count(conn) == 1

    def test_region_b_never_contributes_to_the_gate(self, temp_db):
        conn = temp_db
        configure(conn, region_a=500)
        _seed(conn, 5000, region_id=REGION_B_ID)
        engine, dispatcher = make_engine(conn)

        assert count_valid_region_a_readings(conn) == 500
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE region_id = ?",
            (REGION_B_ID,)).fetchone()["n"] == 5000

        decision = engine.try_click(prediction())
        assert decision.allowed is False
        assert decision.code == "REGION_A_RECORDS_BELOW_1000"
        assert dispatcher.calls == []


class TestGateIsReevaluated:
    def test_enable_toggle_after_gate_is_open(self, temp_db):
        conn = temp_db
        configure(conn, region_a=1000, auto_click_enabled="1")
        engine, dispatcher = make_engine(conn)

        assert engine.try_click(prediction()).allowed is True

        set_setting(conn, "auto_click_enabled", "0")
        conn.commit()
        blocked = engine.try_click(prediction())
        assert blocked.allowed is False
        assert blocked.code == "AUTO_CLICK_DISABLED"

        set_setting(conn, "auto_click_enabled", "1")
        conn.commit()
        assert engine.try_click(prediction()).allowed is True
        assert len(dispatcher.calls) == 2

    def test_gate_count_is_the_live_database_count(self, temp_db):
        conn = temp_db
        configure(conn, region_a=1000)
        engine, _dispatcher = make_engine(conn)

        allowed = engine.try_click(prediction())
        assert allowed.allowed is True
        # The value used by try_click is exactly the real Region A row count.
        assert allowed.region_a_count == count_valid_region_a_readings(conn) == 1000
        assert engine.gate_status()["region_a_count"] == 1000

        # Remove a single real Region A row: the very next call must re-query
        # the database and reflect 999 - never a cached or UI value.
        conn.execute(
            "DELETE FROM readings WHERE reading_id = "
            "(SELECT MAX(reading_id) FROM readings WHERE region_id = ?)",
            (REGION_A_ID,))
        conn.commit()
        assert count_valid_region_a_readings(conn) == 999

        again = engine.try_click(prediction())
        assert again.allowed is False
        assert again.code == "REGION_A_RECORDS_BELOW_1000"
        assert again.region_a_count == 999


class TestBlockedDecisionContract:
    def test_every_blocked_decision_has_stable_code_and_message(self, temp_db):
        conn = temp_db
        scenarios = []

        # gate below 1000
        configure(conn, region_a=999)
        engine, _ = make_engine(conn)
        scenarios.append(engine.try_click(prediction()))

        # kill switch
        configure(conn, region_a=1000)
        engine, _ = make_engine(conn)
        engine.kill()
        scenarios.append(engine.try_click(prediction()))

        # auto click disabled
        configure(conn, region_a=1000, auto_click_enabled="0")
        engine, _ = make_engine(conn)
        scenarios.append(engine.try_click(prediction()))

        # stale model
        configure(conn, region_a=1000, model_stale="1")
        engine, _ = make_engine(conn)
        scenarios.append(engine.try_click(prediction()))

        # rebuild in progress
        configure(conn, region_a=1000, rebuild_in_progress="1")
        engine, _ = make_engine(conn)
        scenarios.append(engine.try_click(prediction()))

        # category not enabled
        configure(conn, region_a=1000)
        set_allowed_categories(conn, ["g2"])
        conn.commit()
        engine, _ = make_engine(conn)
        scenarios.append(engine.try_click(prediction(candidate="g1")))

        # confidence too low
        configure(conn, region_a=1000, confidence_threshold="70")
        engine, _ = make_engine(conn)
        scenarios.append(engine.try_click(prediction(confidence=10.0)))

        # insufficient evidence
        configure(conn, region_a=1000, min_evidence="30")
        engine, _ = make_engine(conn)
        scenarios.append(engine.try_click(prediction(resolved_count=1)))

        for decision in scenarios:
            assert decision.allowed is False
            assert decision.code in BLOCK_CODES
            assert decision.message
            assert decision.code in decision.message

    def test_audit_row_written_for_blocked_and_allowed(self, temp_db):
        conn = temp_db
        configure(conn, region_a=999)
        engine, dispatcher = make_engine(conn)

        blocked = engine.try_click(prediction())
        assert blocked.allowed is False
        assert _audit_count(conn) == 1

        # Cross the gate: the next decision is allowed and audited too.
        _seed(conn, 1)
        assert count_valid_region_a_readings(conn) == 1000
        allowed = engine.try_click(prediction())
        assert allowed.allowed is True
        assert _audit_count(conn) == 2

        rows = learn_repo.recent_click_audit(conn)
        assert len(rows) == 2
        outcomes = {r["outcome"] for r in rows}
        assert "DISPATCHED" in outcomes
        assert len(dispatcher.calls) == 1

    def test_duplicate_dispatch_is_blocked(self, temp_db):
        conn = temp_db
        configure(conn, region_a=1000)
        engine, dispatcher = make_engine(conn)

        pid = learn_repo.insert_prediction(
            conn, 1, REGION_A_ID,
            Prediction(pattern_length=4, pattern_sequence="NA,MI,MN,SM",
                       candidate_group="g1", confidence=99.0, resolved_count=500,
                       correct_count=490, recent_accuracy=95.0,
                       created_at="2026-01-01T00:00:00"))
        conn.commit()

        first = engine.try_click(prediction(prediction_id=pid))
        assert first.allowed is True

        second = engine.try_click(prediction(prediction_id=pid))
        assert second.allowed is False
        assert second.code == "DUPLICATE_ACTION"
        assert len(dispatcher.calls) == 1

    def test_kill_switch_blocks_and_disables(self, temp_db):
        conn = temp_db
        configure(conn, region_a=1000, auto_click_enabled="1")
        engine, dispatcher = make_engine(conn)

        engine.kill()
        decision = engine.try_click(prediction())
        assert decision.allowed is False
        assert decision.code == "KILL_SWITCH_ACTIVE"
        assert dispatcher.calls == []
        # The kill switch also persists auto_click_enabled = 0.
        assert conn.execute(
            "SELECT value FROM settings WHERE key='auto_click_enabled'"
        ).fetchone()["value"] == "0"
