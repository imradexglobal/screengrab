"""Validation taxonomy tests for :mod:`src.monitor.pipeline`.

The pipeline must map any OCR result onto exactly one of a small, closed set of
outcomes, and - critically - a failed / empty / low-quality read must never be
coerced into the numeric value zero.
"""

import os
import re
import tempfile

import numpy as np
import pytest

from src.config.ranges import DEFAULT_GROUPS, RangeConfig
from src.database.init_db import initialise_database
from src.monitor import pipeline


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


def _cfg():
    return RangeConfig(DEFAULT_GROUPS)


def _cand(text, confidence=95.0, canonical=None):
    return {"text": text, "confidence": confidence, "canonical_value": canonical}


class TestEvaluationTaxonomy:
    def test_no_candidates_is_empty(self, temp_db):
        ev = pipeline.evaluate_candidates([], _cfg(), min_confidence=70.0)
        assert ev.status == pipeline.EMPTY
        assert ev.canonical_value is None
        assert ev.numeric_value is None

    def test_all_below_min_confidence_is_low_quality(self, temp_db):
        candidates = [
            _cand("1.25", confidence=40.0, canonical="1.25"),
            _cand("1.30", confidence=65.0, canonical="1.30"),
        ]
        ev = pipeline.evaluate_candidates(candidates, _cfg(), min_confidence=70.0)
        assert ev.status == pipeline.LOW_QUALITY
        # No canonical value may leak out of a quality rejection.
        assert ev.canonical_value is None
        assert ev.numeric_value is None
        # The human-readable message must not itself be a bare number.
        assert ev.detail
        assert re.fullmatch(r"[+-]?\d+(\.\d+)?", ev.detail.strip()) is None

    def test_unparseable_text_is_invalid_text(self, temp_db):
        candidates = [_cand("hello", confidence=95.0, canonical=None)]
        ev = pipeline.evaluate_candidates(candidates, _cfg(), min_confidence=70.0)
        assert ev.status == pipeline.INVALID_TEXT
        assert ev.canonical_value is None
        assert ev.numeric_value is None

    def test_two_distinct_numbers_is_ambiguous(self, temp_db):
        candidates = [
            _cand("1.10", confidence=95.0, canonical="1.10"),
            _cand("1.30", confidence=95.0, canonical="1.30"),
        ]
        ev = pipeline.evaluate_candidates(candidates, _cfg(), min_confidence=70.0)
        assert ev.status == pipeline.AMBIGUOUS
        assert ev.canonical_value is None
        assert ev.numeric_value is None

    def test_repeated_same_number_is_not_ambiguous(self, temp_db):
        candidates = [
            _cand("1.25", confidence=95.0, canonical="1.25"),
            _cand("1.25", confidence=90.0, canonical="1.25"),
        ]
        ev = pipeline.evaluate_candidates(candidates, _cfg(), min_confidence=70.0)
        assert ev.status == pipeline.VALID
        assert ev.canonical_value == "1.25"

    def test_number_outside_every_group_is_out_of_range(self, temp_db):
        # 0.99 sits below the lowest default group boundary.
        below = pipeline.evaluate_candidates(
            [_cand("0.99", confidence=95.0, canonical="0.99")],
            _cfg(), min_confidence=70.0,
        )
        assert below.status == pipeline.OUT_OF_RANGE

        # The default top group (g9) is open-ended, so to exercise the explicit
        # 500000.00 example we use a bounded top group.
        bounded = [dict(g) for g in DEFAULT_GROUPS]
        bounded[-1]["upper_bound"] = 1000.00
        bounded_cfg = RangeConfig(bounded)
        assert bounded_cfg.validate() == []
        high = pipeline.evaluate_candidates(
            [_cand("500000.00", confidence=95.0, canonical="500000.00")],
            bounded_cfg, min_confidence=70.0,
        )
        assert high.status == pipeline.OUT_OF_RANGE
        assert high.canonical_value == "500000.00"  # the text is preserved
        assert high.group_id is None

    def test_default_top_group_is_open_ended(self, temp_db):
        # Document the real semantics: with the shipped configuration the top
        # group has no upper limit, so a very large number is still in range.
        ev = pipeline.evaluate_candidates(
            [_cand("500000.00", confidence=95.0, canonical="500000.00")],
            _cfg(), min_confidence=70.0,
        )
        assert ev.status == pipeline.VALID
        assert ev.group_id == "g9"

    def test_valid_number_inside_group(self, temp_db):
        ev = pipeline.evaluate_candidates(
            [_cand("1.25", confidence=95.0, canonical="1.25")],
            _cfg(), min_confidence=70.0,
        )
        assert ev.status == pipeline.VALID
        assert ev.is_valid is True
        assert ev.canonical_value == "1.25"
        assert ev.numeric_value == pytest.approx(1.25)
        assert ev.group_id == "g1"


class TestSaveModes:
    def test_on_change_repeat_is_duplicate(self, temp_db):
        ev = pipeline.evaluate_candidates(
            [_cand("1.25", confidence=95.0, canonical="1.25")],
            _cfg(), min_confidence=70.0,
            mode=pipeline.MODE_ON_CHANGE, previous_canonical="1.25",
        )
        assert ev.status == pipeline.DUPLICATE
        assert ev.canonical_value == "1.25"

    def test_every_mode_repeat_is_valid(self, temp_db):
        ev = pipeline.evaluate_candidates(
            [_cand("1.25", confidence=95.0, canonical="1.25")],
            _cfg(), min_confidence=70.0,
            mode=pipeline.MODE_EVERY, previous_canonical="1.25",
        )
        assert ev.status == pipeline.VALID


class TestFrameQuality:
    def test_uniform_black_frame_is_rejected(self):
        black = np.zeros((60, 60), dtype=np.uint8)
        ok, reason = pipeline.frame_quality_ok(black)
        assert ok is False
        assert reason

    def test_noisy_frame_is_accepted(self):
        rng = np.random.default_rng(1234)
        noisy = rng.integers(0, 256, size=(60, 60, 3)).astype(np.uint8)
        ok, reason = pipeline.frame_quality_ok(noisy)
        assert ok is True
        assert reason == ""


class TestFailureNeverBecomesZero:
    """OCR failure must never be silently converted into the value 0.00."""

    def _evaluations(self):
        yield pipeline.evaluate_candidates([], _cfg(), min_confidence=70.0)  # EMPTY
        yield pipeline.evaluate_candidates(
            [_cand("abc", confidence=95.0, canonical=None)],
            _cfg(), min_confidence=70.0)  # INVALID_TEXT
        yield pipeline.evaluate_candidates(
            [_cand("1.25", confidence=10.0, canonical="1.25")],
            _cfg(), min_confidence=70.0)  # LOW_QUALITY
        yield pipeline.Evaluation(status=pipeline.FAILURE)  # FAILURE (engine-level)

    def test_no_zero_canonical_from_failures(self):
        seen_statuses = set()
        for ev in self._evaluations():
            seen_statuses.add(ev.status)
            assert ev.canonical_value is None, (
                f"{ev.status} produced canonical_value={ev.canonical_value!r}")
            assert ev.numeric_value is None, (
                f"{ev.status} produced numeric_value={ev.numeric_value!r}")
            assert ev.canonical_value != "0.00"
            assert ev.numeric_value != 0.0
        assert {"EMPTY", "INVALID_TEXT", "LOW_QUALITY", "FAILURE"} <= seen_statuses
