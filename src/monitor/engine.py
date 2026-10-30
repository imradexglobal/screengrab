"""Monitoring engine for Screen Monitor.

Owns the scan loop and wires together the pieces of the upgraded pipeline:

    capture -> OCR -> validation -> readings store -> learning -> auto-click

Two region slots only (spec section 5): ``region-A`` is the active reader that
feeds learning, statistics, predictions and clicks; ``region-B`` may also be
activated and is captured by the same pipeline, but its rows are written to the
store-only ``region_b_readings`` table - they never enter ``readings``, the
model, the statistics or the gate.

The mandatory 1,000-record automation gate is **not** implemented here - it
lives inside :mod:`src.automation.clicker`, in the execution path itself, so
nothing in this module (or the UI) can bypass it.
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import numpy as np
from PySide6.QtCore import QObject, Signal

from src.automation.clicker import AutoClickEngine, ClickDecision
from src.capture.screen_capture import ScreenCapture, primary_monitor
from src.config.repository import (
    current_model_version,
    get_setting,
    get_setting_bool,
    get_setting_float,
    get_setting_int,
    load_range_config,
    mark_model_stale,
)
from src.database.init_db import (
    DB_PATH,
    REGION_A_ID,
    REGION_B_ID,
    count_region_b_readings,
    count_valid_region_a_readings,
    get_connection,
    initialise_database,
)
from src.learning import repository as learn_repo
from src.learning.patterns import DEFAULT_PATTERN_LENGTHS, LearningEngine
from src.learning.rebuild import recalculate_app_brain
from src.monitor import pipeline
from src.ocr.engine import OCREngine

logger = logging.getLogger(__name__)

#: Records which pixel space stored region rectangles use.  Rows written by
#: older builds are in Qt logical pixels and are converted to physical pixels
#: once on load (see :meth:`MonitoringEngine._migrate_region_coordinates`).
COORDS_SPACE_SETTING = "region_coords_space"

#: Maps a pipeline outcome to the ``diagnostics`` category used for reporting.
_DIAGNOSTIC_CATEGORY = {
    pipeline.EMPTY: "OCR_EMPTY",
    pipeline.FAILURE: "OCR_FAILURE",
    pipeline.INVALID_TEXT: "INVALID_TEXT",
    pipeline.AMBIGUOUS: "AMBIGUOUS_READING",
    pipeline.OUT_OF_RANGE: "OUT_OF_RANGE",
    pipeline.LOW_QUALITY: "LOW_QUALITY",
}


class MonitoringEngine(QObject):
    """Core monitoring engine: scanning, OCR, storage, learning, automation."""

    # Qt signals for GUI communication
    scan_completed = Signal(dict)
    status_updated = Signal(str)
    error_occurred = Signal(str)
    detection_saved = Signal(dict)   # kept for backward compatibility
    reading_saved = Signal(dict)     # new: one valid Region A reading
    region_b_saved = Signal(dict)    # store-only Region B reading (never learned)
    reading_rejected = Signal(dict)  # new: one non-valid outcome
    prediction_made = Signal(dict)   # new: a fresh prediction
    prediction_resolved = Signal(dict)
    click_event = Signal(dict)       # new: a click decision (blocked or not)
    counters_updated = Signal(dict)

    def __init__(self, parent: Optional[QObject] = None, db_path: Optional[str] = None):
        super().__init__(parent)
        self.db_path = db_path or DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        self._ocr: Optional[OCREngine] = None
        self._learner: Optional[LearningEngine] = None
        self._auto_click: Optional[AutoClickEngine] = None
        self._range_config = None
        self._model_version = 0
        self._pending_prediction_id: Optional[int] = None

        self._is_running = False
        self._is_paused = False
        self._thread: Optional[threading.Thread] = None
        self._current_session_id: Optional[str] = None
        self._regions: Dict[str, Dict[str, Any]] = {}

        # Pathological protection: a rebuild must never interleave with the
        # incremental learning writes performed by the scan loop.
        self._model_lock = threading.RLock()

        self._counters: Dict[str, int] = {
            "scans_attempted": 0,
            "scans_completed": 0,
            "valid_readings": 0,
            "duplicates": 0,
            "low_quality": 0,
            "out_of_range": 0,
            "ambiguous": 0,
            "invalid_text": 0,
            "empty": 0,
            "ocr_failures": 0,
            "predictions": 0,
            "clicks_dispatched": 0,
            "clicks_blocked": 0,
            "operational_errors": 0,
        }

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialise(self) -> bool:
        """Initialise the engine: database, OCR, regions and learning model."""
        try:
            self._conn = initialise_database(self.db_path)
            self._ocr = OCREngine(
                min_confidence=float(self._get_setting("min_ocr_confidence", "70"))
            )
            self._auto_click = AutoClickEngine(self._conn, session_id=None)
            self._load_regions()
            self._reload_model()
            self._current_session_id = self._new_session_id()
            self._enforce_two_region_model()
            logger.info("MonitoringEngine initialised")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to initialise MonitoringEngine: %s", exc, exc_info=True)
            self.error_occurred.emit(f"Initialisation error: {exc}")
            return False

    def _reload_model(self) -> None:
        """Refresh range configuration and rehydrate the learning engine."""
        if self._conn is None:
            return
        with self._model_lock:
            self._range_config = load_range_config(self._conn)
            self._model_version = current_model_version(self._conn)
            lengths = self._pattern_lengths()
            self._learner = learn_repo.load_engine_state(
                self._conn,
                self._model_version,
                abbreviation_fn=self._range_config.abbreviation,
                pattern_lengths=lengths,
                recent_window=get_setting_int(self._conn, "recent_window", 50),
                region_id=REGION_A_ID,
            )
            self._pending_prediction_id = learn_repo.pending_prediction_id(
                self._conn, self._model_version
            )

    def _pattern_lengths(self) -> tuple:
        raw = self._get_setting("pattern_lengths", "4,5,6,7,8")
        out = []
        for part in str(raw).split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return tuple(sorted(out)) or DEFAULT_PATTERN_LENGTHS

    def _enforce_two_region_model(self) -> None:
        """Keep the fixed two-slot model: only Region A and Region B may exist.

        Both slots are legitimate and may be enabled independently, so their
        persisted state is left untouched - a region the user selected must
        survive a restart.  Any *other* region row (legacy data from before the
        two-slot model) is disabled rather than deleted, so the readings that
        reference it stay intact.
        """
        if self._conn is None:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            for rid in list(self._regions):
                if rid in (REGION_A_ID, REGION_B_ID):
                    continue
                self._regions[rid]["enabled"] = False
                self._conn.execute(
                    "UPDATE regions SET enabled = 0, updated_at = ? WHERE region_id = ?",
                    (now, rid),
                )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Two-region enforcement warning: %s", exc)

    # ------------------------------------------------------------------
    # Region management (fixed two-slot model)
    # ------------------------------------------------------------------

    def _load_regions(self) -> None:
        if self._conn is None:
            return
        try:
            rows = self._conn.execute(
                "SELECT region_id, name, monitor_id, x, y, width, height, enabled "
                "FROM regions"
            ).fetchall()
            self._regions = {}
            for r in rows:
                self._regions[r["region_id"]] = {
                    "region_id": r["region_id"],
                    "name": r["name"],
                    "monitor_id": r["monitor_id"],
                    "x": r["x"], "y": r["y"],
                    "width": r["width"], "height": r["height"],
                    "enabled": bool(r["enabled"]),
                }
            self._migrate_region_coordinates()
        except Exception as exc:  # noqa: BLE001
            logger.error("Error loading regions: %s", exc)

    def _migrate_region_coordinates(self) -> None:
        """Convert legacy region rectangles from Qt logical to physical pixels.

        Earlier builds stored the selection exactly as the overlay reported it,
        i.e. in Qt *logical* pixels, while capture and clicking both work in
        *physical* pixels.  On a scaled display those rows read the wrong part
        of the screen, so convert them once and record the coordinate space so
        the conversion never runs again.
        """
        if self._conn is None:
            return
        if self._get_setting(COORDS_SPACE_SETTING, "logical") == "physical":
            return
        try:
            from src.capture.screen_capture import logical_rect_to_physical
            now = datetime.now(timezone.utc).isoformat()
            for region_id, region in self._regions.items():
                x, y, w, h = logical_rect_to_physical(
                    int(region["x"]), int(region["y"]),
                    int(region["width"]), int(region["height"]))
                region.update({"x": x, "y": y, "width": w, "height": h})
                self._conn.execute(
                    "UPDATE regions SET x = ?, y = ?, width = ?, height = ?, "
                    "updated_at = ? WHERE region_id = ?",
                    (x, y, w, h, now, region_id))
            self._conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (COORDS_SPACE_SETTING, "physical", now))
            self._conn.commit()
            if self._regions:
                logger.info(
                    "Migrated %d region(s) from logical to physical pixels",
                    len(self._regions))
        except Exception as exc:  # noqa: BLE001
            logger.error("Region coordinate migration failed: %s", exc)

    def _new_session_id(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        return f"session-{ts}"

    def add_region(self, name: str, monitor_id: str, x: int, y: int,
                   width: int, height: int, enabled: bool = True,
                   region_id: Optional[str] = None) -> Optional[str]:
        """Add a region into one of the two fixed slots.

        When ``region_id`` is given the region is stored in exactly that slot;
        this is what the UI uses so that "Select Region B" can never end up
        filling the Region A slot.  Without it the first free slot is used
        (Region A first), preserving the original positional behaviour.

        A slot that was previously disabled (``delete_region`` keeps the row so
        historical readings stay intact) is re-activated in place instead of
        being rejected, so a cleared region can always be selected again.
        """
        if self._conn is None:
            return None

        if region_id is None:
            if REGION_A_ID not in self._regions and not self._region_exists_in_db(REGION_A_ID):
                region_id = REGION_A_ID
            elif REGION_B_ID not in self._regions and not self._region_exists_in_db(REGION_B_ID):
                region_id = REGION_B_ID
            else:
                logger.warning("Both region slots are defined; refusing '%s'.", name)
                return None
        elif region_id not in (REGION_A_ID, REGION_B_ID):
            logger.error("Unknown region slot: %s", region_id)
            return None
        elif region_id in self._regions:
            logger.warning("Region slot %s is already configured; use edit.", region_id)
            return None

        # Both slots can be active.
        if not all(isinstance(v, int) for v in (x, y, width, height)):
            logger.error("Region coordinates must be integers.")
            return None
        if width <= 0 or height <= 0:
            logger.error("Region dimensions must be positive.")
            return None

        now = datetime.now(timezone.utc).isoformat()
        existing = self._conn.execute(
            "SELECT created_at FROM regions WHERE region_id = ?", (region_id,)
        ).fetchone()
        if existing is not None:
            # Re-activate a disabled slot in place.  Historical readings that
            # reference this slot are untouched.
            created_at = existing["created_at"] or now
            self._conn.execute(
                """UPDATE regions SET name = ?, monitor_id = ?, x = ?, y = ?,
                                      width = ?, height = ?, enabled = ?, updated_at = ?
                   WHERE region_id = ?""",
                (name, monitor_id, x, y, width, height, int(enabled), now, region_id),
            )
        else:
            created_at = now
            self._conn.execute(
                """INSERT INTO regions (region_id, name, monitor_id, x, y, width, height,
                                        enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (region_id, name, monitor_id, x, y, width, height, int(enabled),
                 created_at, now),
            )
        self._conn.commit()
        self._regions[region_id] = {
            "region_id": region_id, "name": name, "monitor_id": monitor_id,
            "x": x, "y": y, "width": width, "height": height,
            "enabled": bool(enabled), "created_at": created_at, "updated_at": now,
        }
        logger.info("Added region %s: %s", region_id, name)
        return region_id

    def rename_region(self, region_id: str, new_name: str) -> bool:
        if region_id not in self._regions or self._conn is None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                "UPDATE regions SET name = ?, updated_at = ? WHERE region_id = ?",
                (new_name, now, region_id),
            )
            self._conn.commit()
            self._regions[region_id].update({"name": new_name, "updated_at": now})
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Error renaming region %s: %s", region_id, exc)
            return False

    def edit_region(self, region_id: str, x: int, y: int, width: int, height: int,
                    enabled: bool = True) -> bool:
        """Update a region's geometry.

        Both fixed slots may be enabled: Region A is the learning reader while
        Region B is captured for storage only.
        """
        if region_id not in self._regions or self._conn is None:
            return False
        if not all(isinstance(v, int) for v in (x, y, width, height)):
            return False
        if width <= 0 or height <= 0:
            return False
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                """UPDATE regions SET x = ?, y = ?, width = ?, height = ?, enabled = ?,
                                      updated_at = ? WHERE region_id = ?""",
                (x, y, width, height, int(enabled), now, region_id),
            )
            self._conn.commit()
            self._regions[region_id].update({
                "x": x, "y": y, "width": width, "height": height,
                "enabled": bool(enabled), "updated_at": now,
            })
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Error editing region %s: %s", region_id, exc)
            return False

    def delete_region(self, region_id: str) -> bool:
        """Disable a region.  Historical readings are always preserved."""
        if region_id not in self._regions and not self._region_exists_in_db(region_id):
            return False
        if self._conn is None:
            return False
        try:
            self._conn.execute(
                "UPDATE regions SET enabled = 0, updated_at = ? WHERE region_id = ?",
                (datetime.now(timezone.utc).isoformat(), region_id),
            )
            self._conn.commit()
            self._regions.pop(region_id, None)
            logger.info("Region %s disabled (readings preserved)", region_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Error deleting region %s: %s", region_id, exc)
            return False

    def _region_exists_in_db(self, region_id: str) -> bool:
        if self._conn is None:
            return False
        return self._conn.execute(
            "SELECT 1 FROM regions WHERE region_id = ?", (region_id,)
        ).fetchone() is not None

    # ------------------------------------------------------------------
    # Monitoring control
    # ------------------------------------------------------------------

    def start_monitoring(self) -> bool:
        if self._is_running:
            logger.warning("Monitoring already running")
            return False
        if self._conn is None or self._ocr is None:
            if not self.initialise():
                return False

        self._is_running = True
        self._is_paused = False
        for key in self._counters:
            self._counters[key] = 0
        self._current_session_id = self._new_session_id()
        if self._auto_click is not None:
            self._auto_click._session_id = self._current_session_id

        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()
        logger.info("Monitoring started")
        self.status_updated.emit("Monitoring started")
        return True

    def pause_monitoring(self) -> bool:
        if not self._is_running:
            return False
        self._is_paused = True
        self.status_updated.emit("Monitoring paused")
        return True

    def resume_monitoring(self) -> bool:
        if not self._is_running:
            return False
        self._is_paused = False
        self.status_updated.emit("Monitoring resumed")
        return True

    def stop_monitoring(self) -> Dict[str, Any]:
        self._is_running = False
        self._is_paused = False
        if self._thread is not None:
            try:
                self._thread.join(timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
        summary = self._finalise_session()
        self.status_updated.emit("Monitoring stopped")
        return summary

    def stop_monitoring_async(self) -> Dict[str, Any]:
        """Stop without waiting (used by a UI thread to avoid blocking)."""
        self._is_running = False
        self._is_paused = False
        thread = self._thread

        def _wait():
            if thread is not None:
                try:
                    thread.join(timeout=5.0)
                except Exception:  # noqa: BLE001
                    pass
            self._finalise_session()
            self.status_updated.emit("Monitoring stopped")

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        return {"stopping": True}

    def _finalise_session(self) -> Dict[str, Any]:
        if self._conn is None or self._current_session_id is None:
            return {}
        c = self._counters
        invalid = (c["invalid_text"] + c["ambiguous"] + c["out_of_range"] + c["empty"])
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO scan_sessions
                   (session_id, start_timestamp, end_timestamp, scans_attempted,
                    scans_completed, new_values_saved, duplicates_skipped,
                    low_confidence_skipped, invalid_candidates_skipped, operational_errors)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self._current_session_id,
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    c["scans_attempted"], c["scans_completed"],
                    c["valid_readings"], c["duplicates"], c["low_quality"],
                    invalid, c["operational_errors"],
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("Error finalising session: %s", exc)
        return dict(self._counters, session_id=self._current_session_id)

    # ------------------------------------------------------------------
    # Scan loop
    # ------------------------------------------------------------------

    def _scan_loop(self) -> None:
        interval_ms = max(200, int(self._get_setting("scan_interval", "1000")))
        logger.info("Scan loop started (%dms interval)", interval_ms)

        while self._is_running:
            while self._is_paused and self._is_running:
                time.sleep(0.1)

            started = time.time()
            self._counters["scans_attempted"] += 1
            try:
                for region in self._active_regions():
                    self._scan_region(region)
                self._counters["scans_completed"] += 1
                self.scan_completed.emit(self.counters())
            except Exception as exc:  # noqa: BLE001
                logger.error("Scan loop iteration error: %s", exc)
                self._operational_error()

            elapsed = time.time() - started
            time.sleep(max(0.0, (interval_ms / 1000.0) - elapsed))

        logger.info("Scan loop exited")

    def _active_regions(self) -> List[Dict[str, Any]]:
        """Return the regions to scan.

        Both ``region-A`` and ``region-B`` are scanned if they are enabled.
        When no region is defined and the legacy "fullscreen" mode is selected,
        the primary monitor is used.
        """
        active = [r for rid, r in self._regions.items()
                  if rid in (REGION_A_ID, REGION_B_ID) and r.get("enabled")]
        if active:
            return active
        if self._get_setting("monitoring_mode", "selected") == "fullscreen":
            mon = primary_monitor()
            if mon:
                return [{
                    "region_id": REGION_A_ID,
                    "name": "Full Screen (Region A)",
                    "monitor_id": mon.get("name", "primary"),
                    "x": int(mon.get("left", 0)), "y": int(mon.get("top", 0)),
                    "width": int(mon.get("width", 0)), "height": int(mon.get("height", 0)),
                    "enabled": True,
                }]
        return []

    def _scan_region(self, region: Dict[str, Any]) -> None:
        region_id = region["region_id"]
        bbox = (int(region["x"]), int(region["y"]),
                int(region["width"]), int(region["height"]))
        if bbox[2] <= 0 or bbox[3] <= 0:
            self._reject(region_id, pipeline.LOW_QUALITY, "zero-sized region")
            return

        # --- capture -------------------------------------------------
        try:
            capture = ScreenCapture.get_instance()
            frame = capture.capture_screenshot(bbox)
        except Exception as exc:  # noqa: BLE001
            self._reject(region_id, pipeline.FAILURE, f"capture error: {exc}",
                         count_ocr_failure=True)
            return
        if frame is None:
            self._reject(region_id, pipeline.FAILURE, "capture returned no frame",
                         count_ocr_failure=True)
            return

        ok, reason = pipeline.frame_quality_ok(frame)
        if not ok:
            self._reject(region_id, pipeline.LOW_QUALITY, reason)
            return

        # --- OCR -----------------------------------------------------
        if not self._ocr.available:
            self._reject(region_id, pipeline.FAILURE, "OCR engine unavailable",
                         count_ocr_failure=True)
            return
        try:
            candidates = self._ocr.process_raw(frame)
        except Exception as exc:  # noqa: BLE001
            self._reject(region_id, pipeline.FAILURE, f"OCR error: {exc}",
                         count_ocr_failure=True)
            return

        # --- validation + persistence (serialised against recalculation) ---
        with self._model_lock:
            if self._conn is None:
                return
            previous = learn_repo.last_reading(self._conn, region_id)
            previous_canonical = (
                previous["canonical_value"] if previous is not None else None)

            evaluation = pipeline.evaluate_candidates(
                candidates,
                self._range_config,
                min_confidence=float(self._get_setting("min_ocr_confidence", "70")),
                mode=self._save_mode(),
                previous_canonical=previous_canonical,
            )

            if not evaluation.is_valid:
                self._reject(region_id, evaluation.status, evaluation.detail,
                             raw_text=evaluation.original_text)
                return

            self._accept(region_id, region, evaluation)

    # ------------------------------------------------------------------
    # Accept / reject
    # ------------------------------------------------------------------

    def _save_mode(self) -> str:
        mode = self._get_setting("save_mode", pipeline.MODE_ON_CHANGE)
        return mode if mode in (pipeline.MODE_ON_CHANGE, pipeline.MODE_EVERY) \
            else pipeline.MODE_ON_CHANGE

    def _accept(self, region_id: str, region: Dict[str, Any],
                evaluation: pipeline.Evaluation) -> None:
        if self._conn is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        mon = primary_monitor()
        monitor_geometry = None
        if mon:
            monitor_geometry = f"{mon.get('left')},{mon.get('top')}," \
                               f"{mon.get('width')},{mon.get('height')}"

        # Region B is store-only: the row goes to its own table so it can never
        # reach the Region A history, the learning model, the statistics or the
        # automation gate - the data is simply kept.
        if region_id == REGION_B_ID:
            reading_id = learn_repo.insert_region_b_reading(
                self._conn,
                canonical_value=evaluation.canonical_value,
                numeric_value=evaluation.numeric_value,
                original_text=evaluation.original_text,
                ocr_confidence=evaluation.ocr_confidence,
                group_id=evaluation.group_id,
                range_config_version=getattr(self._range_config, "version", None),
                capture_timestamp=now,
                processing_timestamp=now,
                session_id=self._current_session_id,
                screen_x=int(region["x"]), screen_y=int(region["y"]),
                monitor_id=region.get("monitor_id"),
                monitor_geometry=monitor_geometry,
                quality_meta={"status": evaluation.status},
                source="live",
            )
            self._counters["valid_readings"] += 1
            self.region_b_saved.emit({
                "reading_id": reading_id,
                "region_id": REGION_B_ID,
                "region_name": region.get("name", REGION_B_ID),
                "canonical_value": evaluation.canonical_value,
                "numeric_value": evaluation.numeric_value,
                "original_text": evaluation.original_text,
                "confidence": evaluation.ocr_confidence,
                "group_id": evaluation.group_id,
                "timestamp": now,
            })
            self.counters_updated.emit(self.counters())
            return

        reading_id = learn_repo.insert_reading(
            self._conn,
            region_id=region_id,
            canonical_value=evaluation.canonical_value,
            numeric_value=evaluation.numeric_value,
            original_text=evaluation.original_text,
            ocr_confidence=evaluation.ocr_confidence,
            group_id=evaluation.group_id,
            range_config_version=getattr(self._range_config, "version", None),
            capture_timestamp=now,
            processing_timestamp=now,
            session_id=self._current_session_id,
            screen_x=int(region["x"]), screen_y=int(region["y"]),
            monitor_id=region.get("monitor_id"),
            monitor_geometry=monitor_geometry,
            quality_meta={"status": evaluation.status},
            source="live",
        )
        self._counters["valid_readings"] += 1

        payload = {
            "reading_id": reading_id,
            "region_id": region_id,
            "region_name": region.get("name", region_id),
            "canonical_value": evaluation.canonical_value,
            "numeric_value": evaluation.numeric_value,
            "original_text": evaluation.original_text,
            "confidence": evaluation.ocr_confidence,
            "group_id": evaluation.group_id,
            "timestamp": now,
        }
        self.reading_saved.emit(payload)
        # Backward-compatible signal for the existing dashboard/history widgets.
        self.detection_saved.emit({
            "region_id": region_id,
            "region_name": region.get("name", region_id),
            "canonical_value": evaluation.canonical_value,
            "original_text": evaluation.original_text,
            "confidence": evaluation.ocr_confidence,
            "timestamp": now,
        })

        # Learning (Region A only) - immediate, no warm-up.
        if region_id == REGION_A_ID and evaluation.group_id:
            self._learn(evaluation.group_id, reading_id, now)
        self.counters_updated.emit(self.counters())

    def _reject(self, region_id: str, status: str, detail: str,
                raw_text: Optional[str] = None,
                count_ocr_failure: bool = False) -> None:
        if count_ocr_failure:
            self._counters["ocr_failures"] += 1
        bucket = {
            pipeline.EMPTY: "empty",
            pipeline.INVALID_TEXT: "invalid_text",
            pipeline.AMBIGUOUS: "ambiguous",
            pipeline.OUT_OF_RANGE: "out_of_range",
            pipeline.LOW_QUALITY: "low_quality",
            pipeline.DUPLICATE: "duplicates",
        }.get(status)
        if bucket:
            self._counters[bucket] += 1

        if status != pipeline.DUPLICATE and self._conn is not None:
            from src.config.repository import record_diagnostic
            record_diagnostic(
                self._conn, _DIAGNOSTIC_CATEGORY.get(status, status), detail,
                raw_text=raw_text, region_id=region_id,
                session_id=self._current_session_id,
            )
        self.reading_rejected.emit({
            "region_id": region_id, "status": status, "detail": detail,
            "raw_text": raw_text,
        })
        self.counters_updated.emit(self.counters())

    # ------------------------------------------------------------------
    # Learning + automation
    # ------------------------------------------------------------------

    def _learn(self, group_id: str, reading_id: int, timestamp: str) -> None:
        if self._conn is None or self._learner is None:
            return
        with self._model_lock:
            result = self._learner.observe(
                group_id, now=timestamp, basis_reading_id=reading_id,
            )
            resolved, new_prediction_id = learn_repo.apply_learning(
                self._conn, self._model_version, REGION_A_ID, result,
                self._pending_prediction_id,
            )
            self._pending_prediction_id = new_prediction_id

        if resolved and result.resolved is not None:
            p = result.resolved
            self.prediction_resolved.emit({
                "candidate_group": p.candidate_group,
                "actual_group": p.actual_group,
                "is_correct": bool(p.is_correct),
                "confidence": p.confidence,
            })

        if new_prediction_id is None or result.prediction is None:
            return

        p = result.prediction
        self._counters["predictions"] += 1
        prediction = {
            "prediction_id": new_prediction_id,
            "candidate_group": p.candidate_group,
            "confidence": p.confidence,
            "resolved_count": p.resolved_count,
            "correct_count": p.correct_count,
            "recent_accuracy": p.recent_accuracy,
            "pattern_length": p.pattern_length,
            "pattern_sequence": p.pattern_sequence,
        }
        self.prediction_made.emit(prediction)

        if self._auto_click is not None:
            decision: ClickDecision = self._auto_click.try_click(prediction)
            if decision.allowed:
                self._counters["clicks_dispatched"] += 1
            else:
                self._counters["clicks_blocked"] += 1
            self.click_event.emit({
                "allowed": decision.allowed,
                "outcome": decision.outcome,
                "code": decision.code,
                "message": decision.message,
                "region_a_count": decision.region_a_count,
                "prediction": prediction,
            })

    # ------------------------------------------------------------------
    # Public helpers used by the UI
    # ------------------------------------------------------------------

    def counters(self) -> Dict[str, int]:
        return dict(self._counters)

    def region_count(self) -> int:
        return sum(1 for r in self._regions.values() if r.get("enabled"))

    def total_readings(self) -> int:
        if self._conn is None:
            return 0
        return count_valid_region_a_readings(self._conn)

    def total_region_b_readings(self) -> int:
        """Rows Region B has stored in its own (store-only) table."""
        if self._conn is None:
            return 0
        return count_region_b_readings(self._conn)

    def region_b_summary(self) -> Dict[str, Any]:
        """Describe Region B's store-only data for the Regions page."""
        summary: Dict[str, Any] = {
            "stored": self.total_region_b_readings(),
            "latest_value": None,
            "latest_group": None,
            "latest_timestamp": None,
            "configured": REGION_B_ID in self._regions,
            "enabled": bool(self._regions.get(REGION_B_ID, {}).get("enabled")),
        }
        row = learn_repo.last_region_b_reading(self._conn) \
            if self._conn is not None else None
        if row is not None:
            summary["latest_value"] = row["canonical_value"]
            summary["latest_group"] = row["group_id"]
            summary["latest_timestamp"] = row["capture_timestamp"]
        return summary

    def total_detections(self) -> int:
        """Backward-compatible alias for the Region A reading count."""
        return self.total_readings()

    def gate_status(self) -> Dict[str, Any]:
        if self._auto_click is None or self._conn is None:
            return {
                "region_a_count": 0, "required": 1000, "remaining": 1000,
                "unlocked": False, "auto_click_enabled": False, "dry_run": True,
                "killed": False, "model_stale": True, "rebuilding": False,
            }
        return self._auto_click.gate_status()

    def kill_switch(self) -> None:
        if self._auto_click is not None:
            self._auto_click.kill()

    def learning_summary(self, group_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Summarise the derived model.

        Without ``group_ids`` the summary covers every prediction and pattern of
        the current model version.  With ``group_ids`` (the categories allowed
        to be clicked) every figure is narrowed to those categories, which is
        what the Learning page shows as its second, settings-based panel.
        """
        if self._conn is None:
            return {"model_version": 0, "patterns": 0, "predictions": {}}
        conn = self._conn
        summary = learn_repo.prediction_summary(conn, self._model_version, group_ids)
        patterns = learn_repo.pattern_count(conn, self._model_version, group_ids)
        observed = self._learner.observed_count if self._learner else 0
        recent_window = get_setting_int(conn, "recent_window", 50)
        recent = learn_repo.recent_resolved_accuracy(
            conn, self._model_version, recent_window, group_ids)
        return {
            "model_version": self._model_version,
            "patterns": patterns,
            "observed": observed,
            "predictions": summary,
            "readings": learn_repo.region_a_reading_count(conn, group_ids),
            "recent": recent,
            "filtered": group_ids is not None,
            "groups": list(group_ids) if group_ids is not None else None,
        }

    def latest_prediction(self) -> Optional[Dict[str, Any]]:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM predictions ORDER BY prediction_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def recent_predictions(self, limit: int = 100) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        return [dict(r) for r in learn_repo.recent_predictions(self._conn, limit)]

    def top_patterns(self, limit: int = 100) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        return [dict(r) for r in learn_repo.top_patterns_from_db(
            self._conn, self._model_version, limit)]

    def recent_readings(self, limit: int = 200, offset: int = 0,
                        region_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        return [dict(r) for r in learn_repo.recent_readings(
            self._conn, limit, offset, region_id)]

    def delete_readings(self, reading_ids: List[int]) -> int:
        """Delete specific readings chosen in the History page.

        Deleting raw readings invalidates the derived model, so the model is
        flagged stale and auto-clicking is disabled until it is rebuilt.
        """
        if self._conn is None or not reading_ids:
            return 0
        deleted = learn_repo.delete_readings(self._conn, reading_ids)
        if deleted:
            mark_model_stale(
                self._conn,
                f"{deleted} reading(s) deleted from History; rebuild the model")
            logger.info("Deleted %d reading(s) from History", deleted)
        return deleted

    def get_recent_detections(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Backward-compatible history feed backed by the readings table."""
        out = []
        for r in self.recent_readings(limit=limit):
            out.append({
                "detection_id": r.get("reading_id"),
                "reading_id": r.get("reading_id"),
                "region_id": r.get("region_id"),
                "region_name": r.get("region_name") or r.get("region_id"),
                "canonical_value": r.get("canonical_value"),
                "original_text": r.get("original_text") or "",
                "ocr_confidence": r.get("ocr_confidence") or 0.0,
                "capture_timestamp": r.get("capture_timestamp"),
                "processing_timestamp": r.get("processing_timestamp"),
                "group_id": r.get("group_id"),
            })
        return out

    def recent_diagnostics(self, limit: int = 200) -> List[Dict[str, Any]]:
        from src.config.repository import recent_diagnostics
        if self._conn is None:
            return []
        return [dict(r) for r in recent_diagnostics(self._conn, limit)]

    def click_audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        return [dict(r) for r in learn_repo.recent_click_audit(self._conn, limit)]

    def reload(self) -> None:
        """Re-read settings/regions/model after the UI changed them."""
        self._load_regions()
        self._reload_model()
        if self._ocr is not None:
            self._ocr.min_confidence = float(self._get_setting("min_ocr_confidence", "70"))

    def recalculate(self, progress=None) -> Dict[str, Any]:
        """Run "Recalculate App Brain" and refresh in-memory state afterwards."""
        if self._conn is None:
            return {"ok": False, "error": "database not initialised"}
        self.status_updated.emit("Recalculating app brain...")
        result = recalculate_app_brain(
            self._conn,
            algorithm_version=self._get_setting("algorithm_version", "1.0"),
            pattern_lengths=self._pattern_lengths(),
            recent_window=get_setting_int(self._conn, "recent_window", 50),
            progress=progress,
        )
        self._reload_model()
        if result.get("ok"):
            self.status_updated.emit(
                f"Recalculation complete (model v{result.get('model_version')})")
        else:
            self.status_updated.emit(
                f"Recalculation failed: {result.get('error')}")
            self.error_occurred.emit(f"Recalculation failed: {result.get('error')}")
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _operational_error(self) -> None:
        self._counters["operational_errors"] += 1
        self.error_occurred.emit("Operational error during scanning")

    def _get_setting(self, key: str, default: str) -> str:
        if self._conn is None:
            return default
        try:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row is not None else default
        except Exception:  # noqa: BLE001
            return default


def create_engine(parent: Optional[QObject] = None,
                  db_path: Optional[str] = None) -> MonitoringEngine:
    """Factory function to create a configured MonitoringEngine instance."""
    return MonitoringEngine(parent, db_path=db_path)
