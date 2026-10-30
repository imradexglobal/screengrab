"""Screen capture module for Screen Monitor.

Handles screen capture using MSS (Multi-Screen Shot) for selecting regions
and full-screen monitoring across multiple monitors.
"""

import logging
from typing import Optional, Tuple, List
import mss
import cv2
import numpy as np

logger = logging.getLogger(__name__)


class ScreenCapture:
    """Handles screen capture using MSS for multi-monitor setups.

    Coordinates are always stored and exchanged in *physical* pixels (MSS's
    native space), so multi-monitor and high-DPI arrangements are represented
    consistently without per-call DPI conversion races. Logical Qt screen
    coordinates can be converted to physical pixels via ``logical_to_physical``
    when needed by callers that receive logical coordinates from UI widgets.
    """

    _instance: Optional["ScreenCapture"] = None

    def __init__(self):
        self._mss = mss.mss()
        self._monitor_info: List[dict] = []
        self._update_monitor_info()

    def _update_monitor_info(self) -> None:
        """Update monitor information from MSS.

        monitors[0] is the virtual screen spanning all monitors;
        monitors[1] is the primary monitor (Windows typically puts it here),
        monitors[n] are individual physical monitors.
        """
        try:
            self._monitor_info = self._mss.monitors
            logger.info(f"Detected {max(len(self._monitor_info) - 1, 0)} monitors")
        except Exception as e:
            logger.error(f"Failed to update monitor info: {e}")
            self._monitor_info = []

    @classmethod
    def get_instance(cls) -> "ScreenCapture":
        """Return the shared capture singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Monitor discovery
    # ------------------------------------------------------------------

    def get_monitors(self) -> List[dict]:
        """Get list of monitor dictionaries from MSS.

        Each dict contains: left, top, width, height, name
        """
        return self._monitor_info

    def get_primary_monitor(self) -> dict:
        """Get the primary monitor information (Windows convention: index 1)."""
        if len(self._monitor_info) >= 2:
            return self._monitor_info[1]
        return self._monitor_info[0] if self._monitor_info else {}

    def get_all_monitors(self) -> List[dict]:
        """Return all monitors including the virtual-screen bounding box."""
        return list(self._monitor_info) if self._monitor_info else []

    # ------------------------------------------------------------------
    # Coordinate conversion (Qt logical -> MSS physical, DPI aware)
    # ------------------------------------------------------------------

    @staticmethod
    def logical_to_physical(logical_rect: Tuple[int, int, int, int],
                           dpr: float = 1.0) -> Tuple[int, int, int, int]:
        """Convert a Qt logical bounding box to MSS physical pixels.

        Args:
            logical_rect: (x, y, w, h) in logical pixels as produced by Qt.
            dpr: device pixel ratio of the target screen.
        
        Returns:
            (x, y, w, h) in physical pixels.
        """
        x, y, w, h = logical_rect
        return (int(round(x * dpr)), int(round(y * dpr)),
                int(round(w * dpr)), int(round(h * dpr)))

    @staticmethod
    def physical_to_logical(physical_rect: Tuple[int, int, int, int],
                            dpr: float = 1.0) -> Tuple[int, int, int, int]:
        """Inverse of :meth:`logical_to_physical`."""
        x, y, w, h = physical_rect
        return (int(round(x / dpr)), int(round(y / dpr)),
                int(round(w / dpr)), int(round(h / dpr)))

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture_screenshot(self, bbox: Optional[Tuple[int, int, int, int]] = None) -> Optional[np.ndarray]:
        """Capture a screenshot of the screen or a region.

        Args:
            bbox: Optional (left, top, width, height) bounding box in physical
                MSS coordinates. If None, captures the primary monitor.

        Returns:
            numpy array in BGR format, or None on failure.
        """
        try:
            if bbox is None:
                monitor = self.get_primary_monitor()
                if not monitor:
                    return None
                bbox = (monitor["left"], monitor["top"],
                        monitor["width"], monitor["height"])

            x, y, w, h = bbox
            # Validate the requested rectangle against available monitors and
            # clamp any part outside the virtual desktop, so a region left over
            # from a different resolution (or a monitor that is no longer
            # attached) degrades to a smaller frame instead of raising.
            x, y, w, h = clamp_rect_to_virtual(x, y, w, h)
            shot = self._mss.grab({"left": x, "top": y, "width": w, "height": h})

            img = np.array(shot)
            if img.shape[2] == 4:
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            return img
        except Exception as e:
            logger.error(f"Screen capture error: {e}")
            return None

    def capture_region(self, bbox: Tuple[int, int, int, int]) -> Optional[np.ndarray]:
        """Capture a specific screen region (alias of capture_screenshot)."""
        return self.capture_screenshot(bbox)


# -------------------------------------------------------------------------
# Factory function
# -------------------------------------------------------------------------

def create_screen_capture() -> ScreenCapture:
    """Factory function returning the shared capture instance."""
    return ScreenCapture.get_instance()


# -------------------------------------------------------------------------
# Module-level helpers (safe to call without constructing a capture object)
# -------------------------------------------------------------------------

def primary_monitor() -> dict:
    """Return the primary monitor geometry, or an empty dict on failure."""
    try:
        return ScreenCapture.get_instance().get_primary_monitor()
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.warning("Unable to determine primary monitor: %s", exc)
        return {}


def all_monitors() -> list:
    """Return every monitor geometry MSS reports (index 0 is the virtual desktop)."""
    try:
        return ScreenCapture.get_instance().get_all_monitors()
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.warning("Unable to enumerate monitors: %s", exc)
        return []


def geometry_signature() -> tuple:
    """A hashable fingerprint of the current display layout.

    Used to detect resolution / arrangement / multi-monitor changes so that a
    stale click calibration can be refused before anything is dispatched.
    """
    sig = []
    for mon in all_monitors():
        sig.append((mon.get("left"), mon.get("top"),
                    mon.get("width"), mon.get("height")))
    return tuple(sig)


def _display_transform() -> Tuple[float, float, int, int, int, int]:
    """Return how Qt logical coordinates map onto MSS physical pixels.

    Qt reports the virtual desktop in *device-independent* pixels while MSS
    reports it in *physical* pixels.  On a scaled display (e.g. Windows 150 %)
    the two differ, so a rectangle drawn by the user must be converted before
    it is used for capture or clicking.  The scale is derived by comparing the
    two descriptions of the very same virtual desktop, which also keeps the
    translation correct when a secondary monitor sits above or to the left of
    the primary one (negative coordinates).

    Returns:
        ``(scale_x, scale_y, logical_origin_x, logical_origin_y,
        physical_origin_x, physical_origin_y)``
    """
    from PySide6.QtGui import QGuiApplication

    scale_x = scale_y = 1.0
    logical_x = logical_y = 0
    physical_x = physical_y = 0

    screen = QGuiApplication.primaryScreen()
    if screen is not None:
        geo = screen.virtualGeometry()
        logical_x, logical_y = geo.x(), geo.y()

    monitors = all_monitors()
    if monitors:
        virtual = monitors[0]
        physical_x = int(virtual.get("left", 0) or 0)
        physical_y = int(virtual.get("top", 0) or 0)
        if screen is not None:
            geo = screen.virtualGeometry()
            if geo.width():
                scale_x = float(virtual.get("width", geo.width())) / geo.width()
            if geo.height():
                scale_y = float(virtual.get("height", geo.height())) / geo.height()

    return scale_x, scale_y, logical_x, logical_y, physical_x, physical_y


def logical_rect_to_physical(x: int, y: int, w: int, h: int) -> Tuple[int, int, int, int]:
    """Convert a Qt logical virtual-desktop rectangle to MSS physical pixels."""
    scale_x, scale_y, lx, ly, px, py = _display_transform()
    return (
        int(round(px + (x - lx) * scale_x)),
        int(round(py + (y - ly) * scale_y)),
        max(1, int(round(w * scale_x))),
        max(1, int(round(h * scale_y))),
    )


def physical_rect_to_logical(x: int, y: int, w: int, h: int) -> Tuple[int, int, int, int]:
    """Inverse of :func:`logical_rect_to_physical`.

    Used to place always-on-top overlays (the green Region A border) exactly
    over the area the engine actually captures.
    """
    scale_x, scale_y, lx, ly, px, py = _display_transform()
    return (
        int(round(lx + (x - px) / scale_x)) if scale_x else int(x),
        int(round(ly + (y - py) / scale_y)) if scale_y else int(y),
        max(1, int(round(w / scale_x))) if scale_x else max(1, w),
        max(1, int(round(h / scale_y))) if scale_y else max(1, h),
    )


def device_pixel_ratio() -> float:
    """Best-effort device pixel ratio of the primary screen (Qt if available)."""
    try:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            return float(screen.devicePixelRatio())
    except Exception:
        pass
    return 1.0


def monitor_for_point(x: int, y: int) -> Optional[dict]:
    """Return the physical monitor whose bounds contain ``(x, y)``."""
    for mon in all_monitors():
        if mon.get("name") == "Screen 0":
            continue
        left, top = mon.get("left", 0), mon.get("top", 0)
        w, h = mon.get("width", 0), mon.get("height", 0)
        if left <= x < left + w and top <= y < top + h:
            return mon
    return None


def clamp_rect_to_virtual(x: int, y: int, w: int, h: int) -> Tuple[int, int, int, int]:
    """Clamp a rectangle to the virtual desktop so capture never raises."""
    mons = all_monitors()
    if not mons:
        return (max(0, x), max(0, y), max(1, w), max(1, h))
    virtual = mons[0]  # MSS index 0 spans every monitor
    vx, vy = virtual.get("left", 0), virtual.get("top", 0)
    vw, vh = virtual.get("width", 1), virtual.get("height", 1)
    x2 = min(x + w, vx + vw)
    y2 = min(y + h, vy + vh)
    x = max(x, vx)
    y = max(y, vy)
    return (int(x), int(y), int(max(1, x2 - x)), int(max(1, y2 - y)))