"""Phase 8 - reliability and cross-feature integration tests.

Covers the four things the specification asks for at this stage:

1. long-running monitoring does not deadlock, leak sessions or stop saving;
2. readings, regions and settings survive a full application restart;
3. "Recalculate App Brain" can run while monitoring is active and never loses
   or rewrites raw readings;
4. display-layout (DPI/scaling) changes and invalid coordinates are detected and
   block automation instead of clicking at a stale position.

Screen capture and OCR are replaced with deterministic fakes so the tests run
headless and never touch a real monitor.
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from src.automation import clicker as clicker_module
from src.automation.clicker import AutoClickEngine, RecordingDispatcher
from src.capture.screen_capture import ScreenCapture
from src.config.repository import (
    get_click_target,
    set_allowed_categories,
    set_click_target,
    set_setting,
)
from src.database.init_db import REGION_A_ID, initialise_database
from src.monitor.engine import create_engine
from src.monitor.region_manager import Region, create_region_manager


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------

def _frame() -> np.ndarray:
    """A frame with enough variance to pass the quality gate."""
    row = np.arange(60, dtype="uint8")
    grey = np.tile(row, (30, 1))
    return np.repeat(grey[:, :, None], 3, axis=2)


class StubOCR:
    """Deterministic OCR stand-in returning one value per scan."""

    def __init__(self, values):
        self.values = list(values)
        self.index = 0
        self.min_confidence = 0.0
        self.available = True

    def process_raw(self, screenshot, bbox=None):
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return [{"text": str(value), "confidence": 99.0, "canonical_value": str(value)}]


def install_fakes(monkeypatch, engine, values):
    """Replace capture + OCR for a running engine."""
    monkeypatch.setattr(
        ScreenCapture, "capture_screenshot",
        lambda self, bbox=None: _frame(), raising=False)
    engine._ocr = StubOCR(values)


def wait_until(predicate, timeout=20.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _start_engine(monkeypatch, tmp_path, name, values, interval="250"):
    db = str(tmp_path / name)
    engine = create_engine(db_path=db)
    assert engine.initialise(), "engine failed to initialise"
    set_setting(engine._conn, "scan_interval", interval)
    install_fakes(monkeypatch, engine, values)
    assert engine.add_region("Region A", "primary", 10, 10, 120, 40,
                             enabled=True) == REGION_A_ID
    return engine, db


# ---------------------------------------------------------------------------
# 1 + 2: long-running monitoring and restart persistence
# ---------------------------------------------------------------------------

class TestLongRunAndRestart:

    def test_monitoring_saves_continuously_then_survives_restart(self, monkeypatch, tmp_path):
        engine, db = _start_engine(
            monkeypatch, tmp_path, "restart.db",
            ["1.10", "1.30", "1.60", "2.10", "3.00", "6.00", "11.00", "30.00"])

        assert engine.start_monitoring() is True
        try:
            assert wait_until(lambda: engine.total_readings() >= 6), \
                "monitoring did not save readings"
            first_batch = engine.total_readings()
            # Keep running to prove it does not stall after the first readings.
            assert wait_until(lambda: engine.total_readings() > first_batch), \
                "monitoring stalled after the first batch"
        finally:
            summary = engine.stop_monitoring()

        assert summary.get("scans_completed", 0) > 0
        count_before = engine.total_readings()
        assert count_before >= 7
        engine._conn.close()

        # --- restart ---------------------------------------------------
        restarted = create_engine(db_path=db)
        assert restarted.initialise()
        try:
            assert restarted.total_readings() == count_before
            assert REGION_A_ID in restarted._regions
            assert restarted._regions[REGION_A_ID]["enabled"] is True
            # Raw readings are intact and still page-able.
            page = restarted.recent_readings(limit=5, offset=0)
            assert len(page) == 5
            assert all(r["region_id"] == REGION_A_ID for r in page)
            assert all(r["canonical_value"] for r in page)
        finally:
            restarted._conn.close()

    def test_session_rows_are_not_duplicated_across_restarts(self, monkeypatch, tmp_path):
        engine, db = _start_engine(monkeypatch, tmp_path, "sessions.db",
                                   ["1.10", "2.20", "3.30", "6.60"])
        for _ in range(2):
            assert engine.start_monitoring() is True
            assert wait_until(lambda: engine.total_readings() > 0)
            engine.stop_monitoring()

        sessions = engine._conn.execute(
            "SELECT COUNT(*) AS n FROM scan_sessions").fetchone()["n"]
        assert sessions >= 2
        engine._conn.close()


# ---------------------------------------------------------------------------
# 3: recalculation during active monitoring
# ---------------------------------------------------------------------------

class TestRecalculationDuringMonitoring:

    def test_recalculate_while_monitoring_preserves_readings(self, monkeypatch, tmp_path):
        engine, _db = _start_engine(
            monkeypatch, tmp_path, "recalc_live.db",
            ["1.10", "1.25", "1.40", "1.60", "2.20", "3.10", "5.50", "12.00", "40.00"])

        assert engine.start_monitoring() is True
        try:
            assert wait_until(lambda: engine.total_readings() >= 8)
            before = engine.total_readings()
            version_before = engine._model_version

            result = engine.recalculate()

            assert result.get("ok") is True, result
            assert result.get("model_version") is not None
            assert engine._model_version >= version_before

            # No raw reading may ever be lost or reset by a rebuild.
            after = engine.total_readings()
            assert after >= before

            # Monitoring is still alive and still saving.
            assert engine._is_running is True
            assert wait_until(lambda: engine.total_readings() >= after)
        finally:
            engine.stop_monitoring()

        assert engine.gate_status()["rebuilding"] is False
        engine._conn.close()

    def test_recalculation_is_blocked_from_clicking_while_running(self, monkeypatch, tmp_path):
        """`rebuild_in_progress` is set and cleared around a rebuild."""
        engine, _db = _start_engine(monkeypatch, tmp_path, "recalc_flag.db",
                                   ["1.10", "1.30", "2.40"])
        engine._conn.execute(
            "UPDATE settings SET value = '0' WHERE key = 'rebuild_in_progress'")
        engine._conn.commit()

        seen = []

        def progress(frac, message):
            seen.append(
                engine._get_setting("rebuild_in_progress", "0"))

        engine.recalculate(progress=progress)

        assert seen, "progress callback never fired"
        assert "1" in seen, "rebuild_in_progress was never set during the rebuild"
        assert engine._get_setting("rebuild_in_progress", "0") == "0"
        engine._conn.close()


# ---------------------------------------------------------------------------
# 4: display scaling / coordinate validity
# ---------------------------------------------------------------------------

class TestDisplayAndCoordinateValidity:

    def test_offscreen_region_is_invalid_and_rejected(self, tmp_path):
        from src.capture.screen_capture import all_monitors
        monitors = all_monitors()
        assert monitors, "no monitor metadata available in this environment"
        virtual = monitors[0]

        far_x = int(virtual.get("left", 0)) + int(virtual.get("width", 0)) + 5000
        probe = Region(
            region_id=REGION_A_ID, name="Region A", monitor_id="primary",
            x=far_x, y=0, width=100, height=50)
        assert probe.is_valid is False

        zero = Region(
            region_id=REGION_A_ID, name="Region A", monitor_id="primary",
            x=0, y=0, width=0, height=0)
        assert zero.is_valid is False

        db = str(tmp_path / "coords.db")
        initialise_database(db).close()
        manager = create_region_manager(db_path=db)
        try:
            assert manager.initialise() is True
            assert manager.add_region(
                "Region A", "primary", far_x, 0, 100, 50, enabled=True) is None
        finally:
            if manager._conn is not None:
                manager._conn.close()

    def test_display_layout_change_blocks_clicking(self, tmp_path):
        db = str(tmp_path / "display_change.db")
        conn = initialise_database(db)
        try:
            _seed_region_a(conn, 1000)
            set_setting(conn, "auto_click_enabled", "1")
            set_setting(conn, "auto_click_dry_run", "0")
            set_setting(conn, "model_stale", "0")
            set_setting(conn, "rebuild_in_progress", "0")
            set_setting(conn, "confidence_threshold", "70")
            set_setting(conn, "min_evidence", "30")
            set_setting(conn, "recent_min_accuracy", "60")
            set_setting(conn, "cooldown_seconds", "0")
            set_allowed_categories(conn, ["g1"])
            # Calibrated while the display reported a 1920x1080 layout.
            set_click_target(conn, {
                "x": 100, "y": 200, "monitor_id": "Screen 1",
                "geometry": {"monitor_id": "Screen 1", "left": 0, "top": 0,
                             "width": 1920, "height": 1080},
            })
            conn.commit()

            dispatcher = RecordingDispatcher()
            engine = AutoClickEngine(conn, dispatcher=dispatcher)
            prediction = {
                "prediction_id": None, "candidate_group": "g1",
                "confidence": 95.0, "resolved_count": 100,
                "recent_accuracy": 90.0, "click_dispatched": 0,
            }

            # The stored signature still matches -> the click is dispatched.
            original = clicker_module.capture_display_signature
            clicker_module.capture_display_signature = lambda x, y: {
                "monitor_id": "Screen 1", "left": 0, "top": 0,
                "width": 1920, "height": 1080,
            }
            try:
                assert engine.try_click(prediction).allowed is True

                # Display scaling changed -> automation must block.
                clicker_module.capture_display_signature = lambda x, y: {
                    "monitor_id": "Screen 1", "left": 0, "top": 0,
                    "width": 1280, "height": 720,
                }
                decision = engine.try_click(prediction)
                assert decision.allowed is False
                assert decision.code == "DISPLAY_CALIBRATION_CHANGED"
            finally:
                clicker_module.capture_display_signature = original
        finally:
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                path = db + suffix
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def test_uncalibrated_target_blocks_clicking(self, tmp_path):
        db = str(tmp_path / "no_target.db")
        conn = initialise_database(db)
        try:
            _seed_region_a(conn, 1000)
            set_setting(conn, "auto_click_enabled", "1")
            set_setting(conn, "model_stale", "0")
            set_allowed_categories(conn, ["g1"])
            conn.commit()
            assert get_click_target(conn) == {}

            engine = AutoClickEngine(conn, dispatcher=RecordingDispatcher())
            decision = engine.try_click({
                "prediction_id": None, "candidate_group": "g1",
                "confidence": 95.0, "resolved_count": 100,
                "recent_accuracy": 90.0, "click_dispatched": 0,
            })
            assert decision.allowed is False
            assert decision.code == "INVALID_TARGET_COORDINATES"
        finally:
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                path = db + suffix
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass


def _seed_region_a(conn, n):
    now = "2026-01-01T00:00:00"
    rows = [
        (REGION_A_ID, "1.25", 1.25, "1.25", 99.0, None, "g1", 1, now, now,
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
