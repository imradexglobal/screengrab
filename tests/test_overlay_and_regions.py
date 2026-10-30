"""Regression tests for Phase 1 foundation fixes.

Covers:
1. Region selector overlay geometry fix (positions must be computed before
   they are cleared). Verified at source level to avoid headless Qt widget
   construction that otherwise causes an unrecoverable stack overflow on
   this host under QT_QPA_PLATFORM=offscreen.
2. RegionManager can create regions without AttributeError from a missing
   `time` import or undefined `region_id_exists(name)` call.
"""

import os
import tempfile

# Ensure offscreen Qt platform for any pure-logic tests
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.monitor.region_manager import create_region_manager
from src.database.init_db import initialise_database


class TestOverlayGeometryFixSource:
    """Verify the overlay fix is correct in source form.

    We read the relevant slice of main_window.py and confirm the dependency
    order is right: rectangle computed from start/end positions BEFORE those
    positions are cleared to None.
    """

    def test_rectangle_computed_before_clearing(self):
        with open(r"C:\Users\User\Documents\GitHub\screengrab\src\ui\main_window.py", encoding="utf-8") as f:
            src = f.read()

        # Extract mouseReleaseEvent body
        start = src.index("def mouseReleaseEvent")
        end = src.index("def keyPressEvent", start)
        body = src[start:end]

        # The four coordinate computations must all precede the clearing of
        # positions so the rectangle is captured before being dropped.
        for marker in ["self._start_pos.x()",
                       "self._start_pos.y()",
                       "self._end_pos.x()",
                       "self._end_pos.y()"]:
            assert marker in body, f"missing computation: {marker}"

        for marker in ["self._start_pos = None", "self._end_pos = None"]:
            assert marker in body, f"missing clear: {marker}"
            for cmt in ["x = min(sx, ex)",
                        "y = min(sy, ey)",
                        "w = abs(ex - sx)",
                        "h = abs(ey - sy)"]:
                assert body.index(cmt) < body.index(marker), \
                    f"{cmt} must appear before {marker}"


class TestRegionManagerBaseline:
    """Confirm region CRUD works at baseline (after time-import fix)."""

    def test_add_region_works_without_attribute_error(self, region_manager_with_db):
        conn, db = region_manager_with_db
        mgr = create_region_manager(db_path=db)
        try:
            ok = mgr.initialise()
            assert ok
            added = mgr.add_region("Region A", "primary", 100, 100, 200, 200, enabled=True)
            assert added is not None
            cur = conn.execute("SELECT COUNT(*) FROM regions WHERE region_id = ?", (added.region_id,))
            assert cur.fetchone()[0] == 1
            region = mgr.get_region(added.region_id)
            assert region is not None
            assert region.name == "Region A"
            assert region.enabled is True
        finally:
            try:
                if mgr._conn is not None:
                    mgr._conn.close()
            except Exception:
                pass

    def test_edit_region_changes_coordinates(self, region_manager_with_db):
        conn, db = region_manager_with_db
        mgr = create_region_manager(db_path=db)
        try:
            mgr.initialise()
            added = mgr.add_region("Region A", "primary", 100, 100, 200, 200, enabled=True)
            assert added is not None
            assert mgr.edit_region(added.region_id, 50, 50, 100, 80, enabled=False)
            region = mgr.get_region(added.region_id)
            assert region.x == 50 and region.y == 50
            assert region.width == 100 and region.height == 80
            assert region.enabled is False
        finally:
            try:
                if mgr._conn is not None:
                    mgr._conn.close()
            except Exception:
                pass


@pytest.fixture
def region_manager_with_db():
    tmp = tempfile.mktemp(suffix=".db")
    conn = initialise_database(tmp)
    yield conn, tmp
    conn.close()
    if os.path.exists(tmp):
        os.unlink(tmp)
