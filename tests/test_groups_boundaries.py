"""Nine-group definition and boundary-validation tests.

Covers :mod:`src.config.ranges` (``DEFAULT_GROUPS``, ``RangeConfig.validate``,
``classify``) and :mod:`src.config.repository` (``save_range_config``,
``load_groups``, ``mark_model_valid``).
"""

import os
import tempfile
from decimal import Decimal

import pytest

from src.config.ranges import (
    COMPARISON_PRECISION,
    DEFAULT_GROUPS,
    Group,
    RangeConfig,
    RangeValidationError,
)
from src.config.repository import (
    current_range_version,
    get_setting,
    load_groups,
    mark_model_valid,
    model_is_stale,
    save_range_config,
)
from src.database.init_db import REGION_A_ID, initialise_database
from src.learning import repository as learn_repo


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


def _groups():
    return list(DEFAULT_GROUPS)


def _copy_defaults():
    return [dict(g) for g in DEFAULT_GROUPS]


class TestDefaultGroups:
    def test_exactly_nine_groups(self):
        assert len(DEFAULT_GROUPS) == 9
        cfg = RangeConfig(DEFAULT_GROUPS)
        assert len(cfg.groups) == 9
        assert [g.ordinal for g in cfg.groups] == list(range(1, 10))

    def test_contiguous_from_one_cent(self):
        cfg = RangeConfig(DEFAULT_GROUPS)
        groups = cfg.groups
        assert Decimal(str(groups[0].lower_bound)) == Decimal("1.00")
        for prev, nxt in zip(groups, groups[1:]):
            assert prev.upper_bound is not None
            expected = (Decimal(str(prev.upper_bound))
                        + COMPARISON_PRECISION)
            assert Decimal(str(nxt.lower_bound)) == expected

    def test_last_group_is_open_ended(self):
        cfg = RangeConfig(DEFAULT_GROUPS)
        assert cfg.groups[-1].upper_bound is None
        assert cfg.validate() == []


class TestBoundaryClassification:
    @pytest.mark.parametrize("value,expected", [
        (1.00, "g1"),
        (1.25, "g1"),
        (1.26, "g2"),
        (2.00, "g3"),
        (2.01, "g4"),
        (5.00, "g5"),
        (5.01, "g6"),
        (10.00, "g6"),
        (10.01, "g7"),
        (24.00, "g7"),
        (24.01, "g8"),
        (100.00, "g8"),
        (100.01, "g9"),
        (0.99, None),
    ])
    def test_boundaries(self, value, expected):
        cfg = RangeConfig(DEFAULT_GROUPS)
        assert cfg.classify(value) == expected

    def test_boundaries_are_rounded_to_two_decimals(self):
        cfg = RangeConfig(DEFAULT_GROUPS)
        # 1.254 rounds to 1.25 -> g1; 1.255 rounds (half-up) to 1.26 -> g2.
        assert cfg.classify(1.254) == "g1"
        assert cfg.classify(1.255) == "g2"


class TestValidation:
    def test_overlap_rejected(self):
        groups = _copy_defaults()
        groups[1]["lower_bound"] = 1.20  # now <= g1.upper (1.25)
        errors = RangeConfig(groups).validate()
        assert any("overlap" in e.lower() for e in errors)

    def test_gap_rejected(self):
        groups = _copy_defaults()
        groups[1]["lower_bound"] = 1.28  # 1.28 - 1.25 = 0.03 > 0.01
        groups[1]["upper_bound"] = 1.50
        errors = RangeConfig(groups).validate()
        assert any("gap" in e.lower() for e in errors)

    def test_reversed_range_rejected(self):
        groups = [{
            "group_id": "g1", "ordinal": 1, "name": "Nano", "abbreviation": "NA",
            "lower_bound": 2.00, "upper_bound": 1.00,
        }]
        errors = RangeConfig(groups).validate()
        assert any("reversed range" in e for e in errors)

    def test_duplicates_rejected(self):
        groups = _copy_defaults() + [dict(DEFAULT_GROUPS[0])]
        errors = RangeConfig(groups).validate()
        assert any("Duplicate group identifier" in e for e in errors)


class TestSaveRangeConfig:
    def test_invalid_input_raises_and_changes_nothing(self, temp_db):
        conn = temp_db
        before_version = current_range_version(conn)
        before = load_groups(conn)

        groups = _copy_defaults()
        groups[1]["lower_bound"] = 1.20  # overlapping -> invalid
        with pytest.raises(RangeValidationError):
            save_range_config(conn, groups)

        assert current_range_version(conn) == before_version
        after = load_groups(conn)
        assert [(g.group_id, g.lower_bound, g.upper_bound) for g in after] == \
               [(g.group_id, g.lower_bound, g.upper_bound) for g in before]

    def test_valid_input_versions_and_flags_model_stale(self, temp_db):
        conn = temp_db
        # Make the model currently fresh so the flag change is observable.
        mark_model_valid(conn, 1)
        assert model_is_stale(conn) is False
        before_version = current_range_version(conn)

        new_version = save_range_config(conn, _groups(), note="edited")
        assert new_version > before_version
        assert current_range_version(conn) == new_version
        assert model_is_stale(conn) is True
        assert get_setting(conn, "model_stale", "1") == "1"

    def test_name_and_abbreviation_edits_persist(self, temp_db):
        conn = temp_db
        groups = load_groups(conn)
        edited = []
        for g in groups:
            if g.group_id == "g1":
                edited.append(Group(
                    group_id=g.group_id, ordinal=g.ordinal, name="Renamed",
                    abbreviation="RN", lower_bound=g.lower_bound,
                    upper_bound=g.upper_bound))
            else:
                edited.append(g)
        save_range_config(conn, edited, note="rename g1")

        reloaded = {g.group_id: g for g in load_groups(conn)}
        assert reloaded["g1"].name == "Renamed"
        assert reloaded["g1"].abbreviation == "RN"

    def test_existing_readings_are_untouched(self, temp_db):
        conn = temp_db
        for i in range(5):
            learn_repo.insert_reading(
                conn, region_id=REGION_A_ID, canonical_value=f"1.2{i}",
                numeric_value=1.2, original_text="1.20", ocr_confidence=99.0,
                group_id="g1", range_config_version=1)
        before = [
            (r["reading_id"], r["canonical_value"], r["numeric_value"])
            for r in conn.execute(
                "SELECT reading_id, canonical_value, numeric_value FROM readings "
                "ORDER BY reading_id")
        ]

        save_range_config(conn, _groups(), note="touch config")

        after = [
            (r["reading_id"], r["canonical_value"], r["numeric_value"])
            for r in conn.execute(
                "SELECT reading_id, canonical_value, numeric_value FROM readings "
                "ORDER BY reading_id")
        ]
        assert after == before
        assert len(after) == 5
