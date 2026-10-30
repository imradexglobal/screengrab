"""Safe desktop auto-click engine.

The mandatory 1,000-record automation gate is enforced **here**, inside the
execution path - not in the UI.  :meth:`AutoClickEngine.try_click` re-queries
the qualifying Region A reading count from the persistent database on every
attempt and refuses to dispatch unless *every* safety condition passes.

A dispatch may cover an arbitrary number of calibrated points: every point of
the ``click_targets`` sequence is clicked in order with a configurable pause
(default 200 ms) between two consecutive points, and the whole sequence is
refused as soon as one point fails its display-calibration check.

Nothing in this module can be bypassed by changing a setting, restarting the
app, recalculating the model, importing unrelated events, counting Region B
data, counting OCR attempts, counting failed readings, or counting predictions.
"""

import ctypes
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from src.config.repository import (
    get_allowed_categories,
    get_click_targets,
    get_click_sequence_delay_ms,
    get_setting,
    get_setting_bool,
    get_setting_float,
    get_setting_int,
)
from src.database.init_db import count_valid_region_a_readings

logger = logging.getLogger(__name__)

#: Absolute, non-configurable minimum number of qualifying Region A readings.
MIN_REGION_A_RECORDS = 1000

#: Stable machine-readable block reasons (spec section 13).
BLOCK_CODES = (
    "KILL_SWITCH_ACTIVE",
    "REGION_A_RECORDS_BELOW_1000",
    "AUTO_CLICK_DISABLED",
    "MODEL_RECALCULATION_REQUIRED",
    "REBUILD_IN_PROGRESS",
    "CATEGORY_NOT_ENABLED",
    "CONFIDENCE_TOO_LOW",
    "INSUFFICIENT_EVIDENCE",
    "RECENT_ACCURACY_TOO_LOW",
    "INVALID_TARGET_COORDINATES",
    "DISPLAY_CALIBRATION_CHANGED",
    "TARGET_WINDOW_NOT_VERIFIED",
    "DUPLICATE_ACTION",
    "COOLDOWN_ACTIVE",
)

BLOCK_EXPLANATIONS: Dict[str, str] = {
    "KILL_SWITCH_ACTIVE": "The master kill switch is engaged; no clicks are permitted.",
    "REGION_A_RECORDS_BELOW_1000": "Region A does not yet hold 1,000 valid saved readings.",
    "AUTO_CLICK_DISABLED": "Automated clicking is switched off.",
    "MODEL_RECALCULATION_REQUIRED": "The learning model is stale; recalculate it first.",
    "REBUILD_IN_PROGRESS": "A recalculation is running; clicking is suspended.",
    "CATEGORY_NOT_ENABLED": "The predicted category is not selected for clicking.",
    "CONFIDENCE_TOO_LOW": "Confidence is not strictly above the configured threshold.",
    "INSUFFICIENT_EVIDENCE": "There are not enough resolved predictions for this pattern.",
    "RECENT_ACCURACY_TOO_LOW": "Recent performance is below the configured safeguard.",
    "INVALID_TARGET_COORDINATES": "The click target has not been calibrated.",
    "DISPLAY_CALIBRATION_CHANGED": "The display layout changed since calibration.",
    "TARGET_WINDOW_NOT_VERIFIED": "The intended target window could not be verified.",
    "DUPLICATE_ACTION": "A click was already dispatched for this prediction.",
    "COOLDOWN_ACTIVE": "The cooldown period has not elapsed.",
}


def explain(code: str) -> str:
    return BLOCK_EXPLANATIONS.get(code, code.replace("_", " ").capitalize())


@dataclass
class ClickDecision:
    allowed: bool
    code: str
    detail: str = ""
    outcome: str = "BLOCKED"       # BLOCKED | DRY_RUN | DISPATCHED | FAILED | UNKNOWN
    region_a_count: int = 0
    prediction_id: Optional[int] = None
    target_x: Optional[int] = None
    target_y: Optional[int] = None

    @property
    def message(self) -> str:
        if self.allowed:
            return f"{self.outcome}: {self.detail}"
        return f"Blocked ({self.code}): {explain(self.code)}"


# ---------------------------------------------------------------------------
# Dispatchers
# ---------------------------------------------------------------------------

class ClickDispatcher:
    """Interface implemented by real and mock dispatchers."""

    def click(self, x: int, y: int) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class WindowsClickDispatcher(ClickDispatcher):
    """Dispatch a real left click at physical screen coordinates."""

    #: When True the mouse cursor is returned to its original position after
    #: the click, so a stray click cannot leave the desktop in a odd state.
    restore_cursor = True

    def click(self, x: int, y: int) -> bool:
        if sys.platform != "win32":
            logger.error("WindowsClickDispatcher used on a non-Windows platform")
            return False
        try:
            import ctypes.wintypes as wintypes
            user32 = ctypes.windll.user32

            original = None
            if self.restore_cursor:
                pt = wintypes.POINT()
                if user32.GetCursorPos(ctypes.byref(pt)):
                    original = pt

            user32.SetCursorPos(int(x), int(y))
            MOUSEEVENTF_LEFTDOWN = 0x0002
            MOUSEEVENTF_LEFTUP = 0x0004
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            if original is not None:
                user32.SetCursorPos(int(original.x), int(original.y))
            return True
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.error("Click dispatch failed: %s", exc)
            return False


class RecordingDispatcher(ClickDispatcher):
    """Mock dispatcher for tests and dry runs (never touches the desktop)."""

    def __init__(self, succeed: bool = True):
        self.succeed = succeed
        self.calls: list = []

    def click(self, x: int, y: int) -> bool:
        self.calls.append((x, y))
        return self.succeed


# ---------------------------------------------------------------------------
# Target calibration
# ---------------------------------------------------------------------------

def capture_display_signature(x: int, y: int) -> Optional[Dict]:
    """Describe the display layout around a physical screen coordinate."""
    try:
        from src.capture.screen_capture import ScreenCapture
        capture = ScreenCapture.get_instance()
        monitors = capture.get_all_monitors()
    except Exception:
        return None

    for mon in monitors:
        left, top = mon.get("left", 0), mon.get("top", 0)
        w, h = mon.get("width", 0), mon.get("height", 0)
        if mon.get("name") == "Screen 0":
            continue  # virtual desktop aggregate
        if left <= x < left + w and top <= y < top + h:
            return {
                "monitor_id": mon.get("name", "?"),
                "left": left, "top": top, "width": w, "height": h,
            }
    return None


def calibration_payload(x: int, y: int) -> Dict:
    sig = capture_display_signature(x, y) or {}
    return {
        "x": int(x),
        "y": int(y),
        "monitor_id": sig.get("monitor_id"),
        "geometry": sig,
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
    }


def _geometry_signature(target: Dict) -> Optional[tuple]:
    geom = target.get("monitor_id"), target.get("geometry")
    mon_id, geo = target.get("monitor_id"), target.get("geometry") or {}
    if not mon_id or not geo:
        return None
    return (mon_id, geo.get("left"), geo.get("top"), geo.get("width"), geo.get("height"))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AutoClickEngine:
    """Evaluates every safety condition and (optionally) dispatches a click."""

    def __init__(self, conn: sqlite3.Connection,
                 dispatcher: Optional[ClickDispatcher] = None,
                 session_id: Optional[str] = None,
                 clock: Optional[Callable[[], datetime]] = None,
                 target_verifier: Optional[Callable[[Dict], bool]] = None):
        self._conn = conn
        self._dispatcher = dispatcher or WindowsClickDispatcher()
        self._session_id = session_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._target_verifier = target_verifier
        self._killed = False

    # -- kill switch ---------------------------------------------------
    def kill(self) -> None:
        """Master kill switch: prevents any new click immediately."""
        self._killed = True
        set_setting_safe(self._conn, "auto_click_enabled", "0")

    def release_kill(self) -> None:
        self._killed = False

    @property
    def killed(self) -> bool:
        return self._killed

    # -- main entry point ---------------------------------------------
    def try_click(self, prediction: Dict, dry_run: Optional[bool] = None) -> ClickDecision:
        """Evaluate all conditions for one prediction and dispatch if allowed.

        ``prediction`` must carry: ``prediction_id``, ``candidate_group``,
        ``confidence``, ``resolved_count``, ``recent_accuracy``,
        ``click_dispatched``.
        """
        conn = self._conn
        now = self._clock()

        # Always re-query the qualifying count from the persistent database.
        region_a_count = count_valid_region_a_readings(conn)

        def blocked(code: str, detail: str = "") -> ClickDecision:
            decision = ClickDecision(
                allowed=False, code=code, detail=detail,
                region_a_count=region_a_count,
                prediction_id=prediction.get("prediction_id"),
            )
            self._audit(decision, dry_run=bool(dry_run))
            return decision

        # 0) Master kill switch
        if self._killed:
            return blocked("KILL_SWITCH_ACTIVE", "master kill switch engaged")

        # 1) MANDATORY 1,000-record gate
        if region_a_count < MIN_REGION_A_RECORDS:
            return blocked(
                "REGION_A_RECORDS_BELOW_1000",
                f"{region_a_count} / {MIN_REGION_A_RECORDS} valid Region A readings",
            )

        # 2) Master enable
        if not get_setting_bool(conn, "auto_click_enabled", False):
            return blocked("AUTO_CLICK_DISABLED")

        # 3/4) Model validity and recalculation state
        if get_setting_bool(conn, "rebuild_in_progress", False):
            return blocked("REBUILD_IN_PROGRESS")
        if get_setting(conn, "model_stale", "1") == "1":
            return blocked("MODEL_RECALCULATION_REQUIRED")

        # 5) Predicted category selected
        allowed = set(get_allowed_categories(conn))
        candidate = prediction.get("candidate_group")
        if candidate not in allowed:
            return blocked("CATEGORY_NOT_ENABLED", f"category '{candidate}' is not enabled")

        # 6) Confidence strictly greater than the threshold
        threshold = get_setting_float(conn, "confidence_threshold", 70.0)
        confidence = float(prediction.get("confidence") or 0.0)
        if not confidence > threshold:
            return blocked("CONFIDENCE_TOO_LOW",
                           f"confidence {confidence:.2f}% is not > {threshold:.2f}%")

        # 7) Minimum evidence
        min_evidence = get_setting_int(conn, "min_evidence", 30)
        resolved_count = int(prediction.get("resolved_count") or 0)
        if resolved_count < min_evidence:
            return blocked("INSUFFICIENT_EVIDENCE",
                           f"{resolved_count} resolved predictions < {min_evidence} required")

        # 8) Recent-performance safeguard
        if get_setting_bool(conn, "recent_safeguard_enabled", True):
            recent = prediction.get("recent_accuracy")
            recent_min = get_setting_float(conn, "recent_min_accuracy", 60.0)
            if recent is None or float(recent) < recent_min:
                return blocked(
                    "RECENT_ACCURACY_TOO_LOW",
                    f"recent accuracy {recent if recent is not None else 'n/a'} < {recent_min:.2f}%",
                )

        # 9/10) Click points and display calibration
        # Every configured point must exist and still sit on the display layout
        # it was calibrated against; the whole sequence is refused if any point
        # drifted, so a partially stale sequence never runs.
        targets = get_click_targets(conn)
        if not targets:
            return blocked("INVALID_TARGET_COORDINATES", "no calibrated target")
        for index, target in enumerate(targets, start=1):
            tx_i, ty_i = int(target["x"]), int(target["y"])
            current_sig = capture_display_signature(tx_i, ty_i)
            stored_sig = _geometry_signature(target)
            if stored_sig is not None and current_sig is not None:
                current = (current_sig.get("monitor_id"), current_sig.get("left"),
                           current_sig.get("top"), current_sig.get("width"),
                           current_sig.get("height"))
                if current != stored_sig:
                    return blocked(
                        "DISPLAY_CALIBRATION_CHANGED",
                        f"point {index}/{len(targets)}: stored {stored_sig} "
                        f"vs current {current}",
                    )
        tx, ty = int(targets[0]["x"]), int(targets[0]["y"])

        # 11) Target-window verification (only when the design requires it)
        if get_setting_bool(conn, "require_window_verification", False):
            if self._target_verifier is None or not self._target_verifier(targets[0]):
                return blocked("TARGET_WINDOW_NOT_VERIFIED")

        # 12) Duplicate dispatch prevention
        prediction_id = prediction.get("prediction_id")
        if prediction_id is not None:
            row = conn.execute(
                "SELECT click_dispatched FROM predictions WHERE prediction_id = ?",
                (prediction_id,),
            ).fetchone()
            if row is not None and int(row["click_dispatched"]) == 1:
                return blocked("DUPLICATE_ACTION", "already dispatched for this prediction")

        # 13) Cooldown
        cooldown = get_setting_float(conn, "cooldown_seconds", 5.0)
        last_ts = self._last_dispatch_time()
        if last_ts is not None:
            elapsed = (now - last_ts).total_seconds()
            if elapsed < cooldown:
                return blocked("COOLDOWN_ACTIVE",
                               f"{elapsed:.1f}s elapsed of {cooldown:.1f}s cooldown")

        # All conditions passed.
        if dry_run is None:
            dry_run = get_setting_bool(conn, "auto_click_dry_run", True)

        decision = ClickDecision(
            allowed=True, code="OK", region_a_count=region_a_count,
            prediction_id=prediction_id, target_x=tx, target_y=ty,
        )
        point_summary = ", ".join(f"({int(t['x'])}, {int(t['y'])})"
                                  for t in targets)
        if dry_run:
            decision.outcome = "DRY_RUN"
            decision.detail = (
                f"dry run: would click {len(targets)} point(s): {point_summary}"
            )
            self._audit(decision, dry_run=True)
            return decision

        delay_ms = get_click_sequence_delay_ms(conn)
        try:
            ok, dispatch_detail = self._dispatch_sequence(targets, delay_ms)
        except Exception as exc:  # pragma: no cover - platform dependent
            decision.outcome = "UNKNOWN"
            decision.detail = f"dispatch raised: {exc}"
            self._audit(decision, dry_run=False)
            return decision

        decision.outcome = "DISPATCHED" if ok else "FAILED"
        decision.detail = dispatch_detail or f"click ({tx}, {ty})"
        if ok and prediction_id is not None:
            conn.execute(
                "UPDATE predictions SET click_dispatched = 1 WHERE prediction_id = ?",
                (prediction_id,),
            )
            conn.commit()
        self._audit(decision, dry_run=False)
        return decision

    def _dispatch_sequence(self, targets: List[Dict], delay_ms: int) -> tuple:
        """Click every configured point in order, ``delay_ms`` apart.

        Returns ``(all_succeeded, detail)``.  The pause is applied *between*
        two consecutive points, never before the first one, so a single-point
        sequence behaves exactly like the old one-click behaviour.
        """
        total = len(targets)
        for index, target in enumerate(targets):
            if index:
                time.sleep(max(0.0, float(delay_ms)) / 1000.0)
            x, y = int(target["x"]), int(target["y"])
            try:
                ok = bool(self._dispatcher.click(x, y))
            except Exception as exc:  # pragma: no cover - platform dependent
                return False, (f"point {index + 1}/{total} at ({x}, {y}) "
                               f"raised: {exc}")
            if not ok:
                return False, (f"point {index + 1}/{total} at ({x}, {y}) "
                               f"failed; stopped after {index} successful click(s)")
        return True, (f"{total} point(s) clicked with {delay_ms} ms between them"
                      if total > 1 else f"click ({targets[0]['x']}, {targets[0]['y']})")

    # -- helpers -------------------------------------------------------
    def _last_dispatch_time(self) -> Optional[datetime]:
        row = self._conn.execute(
            """SELECT created_at FROM click_audit
               WHERE outcome = 'DISPATCHED' ORDER BY audit_id DESC LIMIT 1"""
        ).fetchone()
        if row is None or not row["created_at"]:
            return None
        try:
            return datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _audit(self, decision: ClickDecision, dry_run: bool) -> None:
        try:
            self._conn.execute(
                """INSERT INTO click_audit
                   (created_at, prediction_id, outcome, block_code, detail,
                    target_x, target_y, dry_run, region_a_count, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self._clock().isoformat(), decision.prediction_id,
                    decision.outcome,
                    None if decision.allowed else decision.code,
                    decision.detail, decision.target_x, decision.target_y,
                    1 if dry_run else 0, decision.region_a_count, self._session_id,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.error("Failed to write click audit: %s", exc)

    # -- gate visibility for the UI ------------------------------------
    def gate_status(self) -> Dict:
        """Describe the current gate state for the dashboard (read-only)."""
        conn = self._conn
        count = count_valid_region_a_readings(conn)
        return {
            "region_a_count": count,
            "required": MIN_REGION_A_RECORDS,
            "remaining": max(0, MIN_REGION_A_RECORDS - count),
            "unlocked": count >= MIN_REGION_A_RECORDS,
            "auto_click_enabled": get_setting_bool(conn, "auto_click_enabled", False),
            "dry_run": get_setting_bool(conn, "auto_click_dry_run", True),
            "killed": self._killed,
            "model_stale": get_setting(conn, "model_stale", "1") == "1",
            "rebuilding": get_setting_bool(conn, "rebuild_in_progress", False),
        }


def set_setting_safe(conn: sqlite3.Connection, key: str, value) -> None:
    from src.config.repository import set_setting
    set_setting(conn, key, value)
    conn.commit()
