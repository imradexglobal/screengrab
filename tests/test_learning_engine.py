"""Learning / prediction tests.

Exercises the pure :class:`~src.learning.patterns.LearningEngine`, the
deterministic ``replay`` helper, and the confidence / evidence / recent-accuracy
comparisons enforced by :class:`~src.automation.clicker.AutoClickEngine`.
"""

import os
import tempfile

import pytest

from src.automation.clicker import AutoClickEngine, RecordingDispatcher
from src.config.repository import set_allowed_categories, set_click_target, set_setting
from src.database.init_db import REGION_A_ID, initialise_database
from src.learning.patterns import LearningEngine, replay


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


def configure(conn, region_a=1000, **overrides):
    """Make every gate pass by default so a single condition can be isolated."""
    _seed_region_a(conn, region_a)
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


def prediction(confidence=95.0, resolved_count=100, recent_accuracy=90.0,
               candidate="g1"):
    return {
        "prediction_id": None,
        "candidate_group": candidate,
        "confidence": confidence,
        "resolved_count": resolved_count,
        "recent_accuracy": recent_accuracy,
        "click_dispatched": 0,
    }


class TestLearningBootstrapping:
    @staticmethod
    def _observe(engine, sequence):
        for gid in sequence:
            engine.observe(gid)

    def test_counters_start_without_warmup(self):
        """Counters are populated after a handful of observations - there is no
        1,000-reading warm-up.  A length-*L* pattern needs *L+1* observations."""
        engine = LearningEngine()
        self._observe(engine, ["g1", "g2", "g3", "g4"] * 2)  # 8 observations
        assert any(
            s.pattern_length == 4 and s.opportunities >= 1
            for s in engine.stats.values()
        )

    def test_unresolved_pattern_is_selected_with_zero_confidence(self):
        """Learning and predicting start immediately.

        A stat with opportunities but zero *resolved* predictions is still
        eligible for prediction (otherwise the model could never bootstrap:
        ``resolved`` only grows by resolving a prediction).  What it may never
        do is carry usable confidence, so its accuracy is ``None`` / 0 and it
        can never authorise a click.
        """
        engine = LearningEngine()
        self._observe(engine, ["g1", "g2", "g3", "g4"] * 2)
        stat = engine.stats[(4, "g1,g2,g3,g4", "g1")]
        assert stat.opportunities == 1
        assert stat.resolved == 0
        assert stat.accuracy is None

        candidate = engine.select_candidate()
        assert candidate is not None
        assert candidate[2] == "g1"

        prediction = engine.predict_next()
        assert prediction is not None
        assert prediction.candidate_group == "g1"
        assert prediction.confidence == 0.0
        assert prediction.resolved_count == 0

    def test_selection_and_confidence_use_resolved_outcomes(self):
        """Once a stat has resolved outcomes it is selected and its confidence
        equals 100 * correct / resolved (resolved-only accuracy)."""
        engine = LearningEngine()
        self._observe(engine, ["g1", "g2", "g3", "g4"] * 2)
        key = (4, "g1,g2,g3,g4", "g1")
        engine.stats[key].resolved = 10
        engine.stats[key].correct = 9

        candidate = engine.select_candidate()
        assert candidate is not None
        assert candidate[2] == "g1"

        pred = engine.predict_next()
        assert pred is not None
        assert pred.candidate_group == "g1"
        assert pred.resolved_count == 10
        assert pred.confidence == pytest.approx(90.0)

    def test_prediction_appears_without_warmup(self):
        """Intended behaviour: learning starts immediately, so a repeating
        sequence should yield a prediction long before 1,000 readings."""
        engine = LearningEngine()
        predictions = []
        for gid in ["g1", "g2", "g3", "g4"] * 25:
            result = engine.observe(gid)
            if result.prediction is not None:
                predictions.append(result.prediction)
        assert predictions, (
            "GENUINE BUG: LearningEngine never yields a prediction. "
            "select_candidate() in src/learning/patterns.py skips any stat with "
            "`best.resolved <= 0`, but `resolved` is only incremented in observe() "
            "when an existing pending prediction is resolved - and a pending "
            "prediction is only created when a stat already has resolved > 0. "
            "This deadlock means no prediction (and therefore no learning "
            "progress) can ever occur."
        )


class TestReplayDeterminism:
    def test_replay_matches_incremental_observation(self):
        sequence = ["g1", "g2", "g3", "g1", "g2", "g4"] * 12

        replayed, _predictions = replay(sequence)
        incremental = LearningEngine()
        for gid in sequence:
            incremental.observe(gid)

        def counters(engine):
            return {
                key: (s.opportunities, s.resolved, s.correct, s.incorrect)
                for key, s in engine.stats.items()
            }

        assert counters(replayed) == counters(incremental)
        assert (replayed.pending is None) == (incremental.pending is None)
        if replayed.pending is not None and incremental.pending is not None:
            assert replayed.pending.as_dict() == incremental.pending.as_dict()


class TestExecutionGateComparisons:
    def test_confidence_threshold_is_strictly_greater(self, temp_db):
        conn = temp_db
        configure(conn, confidence_threshold="70")
        engine, dispatcher = make_engine(conn)

        at_threshold = engine.try_click(prediction(confidence=70.0))
        assert at_threshold.allowed is False
        assert at_threshold.code == "CONFIDENCE_TOO_LOW"

        above = engine.try_click(prediction(confidence=70.01))
        assert above.allowed is True
        assert len(dispatcher.calls) == 1

    def test_min_evidence_blocks_below_and_allows_at_threshold(self, temp_db):
        conn = temp_db
        configure(conn, min_evidence="30")
        engine, dispatcher = make_engine(conn)

        below = engine.try_click(prediction(resolved_count=29))
        assert below.allowed is False
        assert below.code == "INSUFFICIENT_EVIDENCE"

        at = engine.try_click(prediction(resolved_count=30))
        assert at.allowed is True
        assert len(dispatcher.calls) == 1

    def test_recent_accuracy_safeguard(self, temp_db):
        conn = temp_db
        configure(conn, recent_safeguard_enabled="1", recent_min_accuracy="60")
        engine, dispatcher = make_engine(conn)

        low = engine.try_click(prediction(recent_accuracy=50.0))
        assert low.allowed is False
        assert low.code == "RECENT_ACCURACY_TOO_LOW"

        unknown = engine.try_click(prediction(recent_accuracy=None))
        assert unknown.allowed is False
        assert unknown.code == "RECENT_ACCURACY_TOO_LOW"

        ok = engine.try_click(prediction(recent_accuracy=80.0))
        assert ok.allowed is True
        assert len(dispatcher.calls) == 1

    def test_recent_safeguard_can_be_disabled(self, temp_db):
        conn = temp_db
        configure(conn, recent_safeguard_enabled="0")
        engine, dispatcher = make_engine(conn)
        decision = engine.try_click(prediction(recent_accuracy=1.0))
        assert decision.allowed is True
        assert len(dispatcher.calls) == 1
