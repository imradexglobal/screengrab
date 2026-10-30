"""Main application window for Screen Monitor.

This module provides the primary PySide6 GUI window containing:
- Dashboard with live monitoring controls, statistics, and real-time detection feed
- Region management interface (add, edit, delete, test OCR)
- History and CSV export functionality
- Settings panel (scan interval, OCR confidence, monitoring mode)
"""

import csv
import html
import logging
import shutil
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QRect, QRectF, QSize
from PySide6.QtGui import QAction, QColor, QFont, QCursor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QSystemTrayIcon,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from src.automation.clicker import BLOCK_EXPLANATIONS, explain
from src.capture.screen_capture import logical_rect_to_physical, physical_rect_to_logical
from src.config import repository as config_repo
from src.config.ranges import Group
from src.database.init_db import (
    DB_PATH,
    REGION_A_ID,
    REGION_B_ID,
    get_connection,
    initialise_database,
)
from src.monitor.engine import MonitoringEngine, create_engine
from src.monitor.region_manager import Region, RegionManager, create_region_manager
from src.normalisation import parse_and_normalise
from src.ui.widgets import (
    EMPTY_CELL_COLOUR,
    NumericTableWidgetItem,
    PALETTE_TEXT,
    PlaceholderTable,
    add_help_label,
    add_help_row,
    configure_fit_table,
    fit_columns,
    group_colour_map,
    help_badge,
    help_text,
    make_button,
    make_item,
    page_header,
    set_button_role,
    sort_safely,
)

logger = logging.getLogger(__name__)

#: Clear English names for the two fixed region slots.
REGION_A_LABEL = "Region A — Active reader (learning + automation gate)"
REGION_B_LABEL = "Region B — Secondary (store only)"

#: Fixed, non-configurable automation gate (spec section 12).
MANDATORY_REGION_A_RECORDS = 1000

#: Dimensions of the live outcome grid on the dashboard: 6 columns x 10 rows.
LIVE_GRID_COLUMNS = 6
LIVE_GRID_ROWS = 10
LIVE_GRID_CELLS = LIVE_GRID_COLUMNS * LIVE_GRID_ROWS

#: Accent colour -> (pill background, pill border, pill foreground).  The
#: dashboard status is drawn as a pill so the monitoring state is readable at
#: a glance, while the foreground stays dark enough to pass a contrast check.
_STATUS_PILLS = {
    "#2e7d32": ("#e8f5e9", "#c8e6c9", "#1b5e20"),
    "#f57c00": ("#fff3e0", "#ffe0b2", "#e65100"),
    "#64748b": ("#f1f5f9", "#e2e8f0", "#475569"),
    "#c62828": ("#ffebee", "#ffcdd2", "#b71c1c"),
}

#: Application-wide look and feel.  Every ``QPushButton`` gets a real button
#: surface with hover, pressed and *visibly disabled* states; the ``role``
#: dynamic property (set through :func:`set_button_role`) gives an action its
#: colour while still rendering the disabled variant as unavailable.
_BASE_STYLESHEET = """
QWidget { font-size: 10pt; color: #0f172a; }
QLabel { padding: 1px 2px; }
QToolTip {
    background-color: #0f172a;
    color: #f8fafc;
    border: 1px solid #334155;
    padding: 4px 6px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QListWidget {
    min-height: 30px;
    padding: 3px 8px;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    background: #ffffff;
    selection-background-color: #38bdf8;
    selection-color: #0f172a;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QDateEdit:focus {
    border: 1px solid #0ea5e9;
}
/* Spin boxes and combo boxes keep their natural width in wide forms. */
QSpinBox { max-width: 170px; }
QDoubleSpinBox { max-width: 170px; }
QComboBox { max-width: 380px; }
/* The frame belongs to the box; Qt's internal line edit inside a spin box (or
   an editable combo) must not paint a second frame on top of it. */
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit, QDateEdit QLineEdit,
QComboBox QLineEdit {
    border: none;
    border-radius: 0;
    background: transparent;
    padding: 0 4px;
    min-height: 0px;
}
QListWidget { padding: 4px; }
QListWidget::item { padding: 5px 6px; border-radius: 4px; }
QListWidget::item:hover { background-color: #e2e8f0; }
QListWidget::item:selected { background-color: #bae6fd; color: #0f172a; }

QPushButton {
    min-height: 32px;
    padding: 6px 16px;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    background-color: #ffffff;
    color: #0f172a;
    font-weight: 600;
}
QPushButton:hover { background-color: #f1f5f9; border-color: #94a3b8; }
QPushButton:pressed {
    background-color: #e2e8f0;
    border-color: #64748b;
}
/* Keyboard focus must stay visible; the colour-only change keeps geometry stable. */
QPushButton:focus { border-color: #0ea5e9; }
QPushButton[role]:focus { border-color: #0ea5e9; }
/* Unavailable actions are always a neutral, readable grey - never a faded
   version of their enabled colour, which used to make white text on a pale
   background. */
QPushButton:disabled {
    background-color: #eef2f6;
    color: #7d8b9c;
    border-color: #dde4ea;
}
QPushButton[role="primary"]:disabled,
QPushButton[role="success"]:disabled,
QPushButton[role="warning"]:disabled,
QPushButton[role="danger"]:disabled,
QPushButton[role="accent"]:disabled,
QPushButton[role="quiet"]:disabled {
    background-color: #eef2f6;
    color: #7d8b9c;
    border-color: #dde4ea;
}
QPushButton:disabled:hover,
QPushButton[role]:disabled:hover { background-color: #eef2f6; border-color: #dde4ea; }

QPushButton[role="primary"] { background-color: #1976d2; border-color: #1565c0; color: #ffffff; }
QPushButton[role="primary"]:hover { background-color: #1e88e5; border-color: #1565c0; }
QPushButton[role="primary"]:pressed { background-color: #0d47a1; }

QPushButton[role="success"] { background-color: #2e7d32; border-color: #256b2a; color: #ffffff; }
QPushButton[role="success"]:hover { background-color: #388e3c; border-color: #256b2a; }
QPushButton[role="success"]:pressed { background-color: #1b5e20; }

QPushButton[role="warning"] { background-color: #f57c00; border-color: #ef6c00; color: #ffffff; }
QPushButton[role="warning"]:hover { background-color: #fb8c00; border-color: #ef6c00; }
QPushButton[role="warning"]:pressed { background-color: #e65100; }

QPushButton[role="danger"] { background-color: #c62828; border-color: #b71c1c; color: #ffffff; }
QPushButton[role="danger"]:hover { background-color: #d32f2f; border-color: #b71c1c; }
QPushButton[role="danger"]:pressed { background-color: #8e0000; }

QPushButton[role="accent"] { background-color: #6a1b9a; border-color: #4a148c; color: #ffffff; }
QPushButton[role="accent"]:hover { background-color: #7b1fa2; border-color: #4a148c; }
QPushButton[role="accent"]:pressed { background-color: #4a148c; }

QPushButton[role="quiet"] { background-color: #f8fafc; color: #334155; }
QPushButton[role="quiet"]:hover { background-color: #e2e8f0; }

QGroupBox {
    font-weight: bold;
    background-color: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    margin-top: 20px;
    padding: 16px 14px 14px 14px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 8px;
    color: #0f172a;
    background-color: #ffffff;
}
QTableWidget {
    background: #ffffff;
    alternate-background-color: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: #dbeafe;
    selection-color: #0f172a;
}
QHeaderView::section {
    padding: 8px 8px;
    border: none;
    border-right: 1px solid #e2e8f0;
    border-bottom: 1px solid #e2e8f0;
    background: #f1f5f9;
    color: #334155;
    font-weight: 600;
}
QTableCornerButton::section { background: #f1f5f9; border: none; }
QProgressBar {
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    background: #f1f5f9;
    text-align: center;
    min-height: 18px;
}
QProgressBar::chunk { background-color: #38bdf8; border-radius: 5px; }
QScrollArea { border: none; background-color: transparent; }
QScrollBar:vertical { background: #f1f5f9; width: 12px; margin: 0; border-radius: 6px; }
QScrollBar::handle:vertical {
    background: #cbd5e1; min-height: 30px; border-radius: 6px; margin: 2px;
}
QScrollBar::handle:vertical:hover { background: #94a3b8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #f1f5f9; height: 12px; margin: 0; border-radius: 6px; }
QScrollBar::handle:horizontal {
    background: #cbd5e1; min-width: 30px; border-radius: 6px; margin: 2px;
}
QScrollBar::handle:horizontal:hover { background: #94a3b8; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""

#: Folder holding the tiny SVG pictograms used by ``APP_STYLESHEET``.
_ASSET_DIR = Path(__file__).resolve().parent / "assets"


def _stepper_art_css() -> str:
    """Build the stylesheet block that paints the spin/combo arrow art.

    Qt stops drawing the native arrow as soon as the matching sub-control is
    styled, and a border-based CSS triangle renders as a filled block - so the
    arrows ship as two small SVGs next to this module.  If they are missing
    (a partial install) the block is dropped entirely and Qt's own arrows are
    used instead.
    """
    candidates = [_ASSET_DIR]
    if getattr(sys, "frozen", False):  # PyInstaller keeps data files in here
        candidates.append(Path(sys._MEIPASS) / "src" / "ui" / "assets")
    asset_dir = next((c for c in candidates if (c / "arrow_up.svg").is_file()), None)
    if asset_dir is None:
        return ""
    up_url = (asset_dir / "arrow_up.svg").as_posix()
    down_url = (asset_dir / "arrow_down.svg").as_posix()
    return f"""
/* Spin boxes: a readable stepper column instead of the default slivers. */
QSpinBox, QDoubleSpinBox {{ padding-right: 26px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid #e2e8f0;
    border-bottom: 1px solid #e2e8f0;
    border-top-right-radius: 6px;
    background-color: #f8fafc;
    image: url({up_url});
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 24px;
    border-left: 1px solid #e2e8f0;
    border-bottom-right-radius: 6px;
    background-color: #f8fafc;
    image: url({down_url});
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background-color: #e2e8f0;
}}
QComboBox::drop-down {{ width: 26px; border: none; background-color: transparent; }}
QComboBox::down-arrow {{ image: url({down_url}); }}
"""


#: Inline stylesheet applied to the whole application.
APP_STYLESHEET = _BASE_STYLESHEET + _stepper_art_css()

#: Inline stylesheet for the light "card" frames used to group controls on a
#: page.  Cards keep related controls visually together and adapt to any
#: window width because their inner layouts stretch.
_CARD_STYLE = """
QFrame#card {
    background-color: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
}
QFrame#card:hover { border-color: #cbd5e1; }
"""


def _scrolled_page(page: QWidget) -> QVBoxLayout:
    """Give ``page`` a scrollable body and return the body's layout.

    Every page is wrapped in a scroll area so that controls keep their natural
    size instead of being squeezed when the window is small; scroll bars appear
    automatically in whichever direction is needed.
    """
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    area = QScrollArea(page)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    body = QWidget()
    body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    inner = QVBoxLayout(body)
    area.setWidget(body)
    outer.addWidget(area)
    return inner


def _form_caption(form: QFormLayout, caption: str, value_widget: QWidget,
                  explanation: str = "") -> None:
    """Add a form row whose caption carries a hovering ``?`` explanation."""
    add_help_row(form, caption, value_widget, explanation)



class PointSelectorOverlay(QWidget):
    """Fullscreen transparent overlay for selecting a single point on screen.

    The user clicks anywhere to pick a coordinate.
    """

    point_selected = Signal(int, int)  # emits (x, y)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)

        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen:
            self._virtual_geo = screen.virtualGeometry()
            self.setGeometry(self._virtual_geo)
        else:
            self._virtual_geo = None

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QColor, QPen
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

        # Draw crosshair at mouse position
        pos = self.mapFromGlobal(QCursor.pos())
        pen = QPen(QColor(56, 189, 233), 1)
        painter.setPen(pen)
        painter.drawLine(0, pos.y(), self.width(), pos.y())
        painter.drawLine(pos.x(), 0, pos.x(), self.height())

        # Help text
        painter.setPen(QColor(255, 255, 255))
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, "Click to select target coordinate")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            # Absolute screen coordinates
            offset_x = self._virtual_geo.x() if self._virtual_geo else 0
            offset_y = self._virtual_geo.y() if self._virtual_geo else 0
            self.point_selected.emit(pos.x() + offset_x, pos.y() + offset_y)
            self.close()

    def mouseMoveEvent(self, event):
        self.update()


class RegionSelectorOverlay(QWidget):
    """Fullscreen transparent overlay for selecting a screen region by dragging.

    The user clicks on the overlay and drags to draw a rectangle.
    On mouse release, the bounding box (x, y, w, h) is emitted.
    """

    region_selected = Signal(tuple)  # emits (x, y, width, height) as (int, int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)

        # Full virtual screen geometry covering all monitors
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen:
            self._virtual_geo = screen.virtualGeometry()
        else:
            self._virtual_geo = screen.geometry() if screen else None

        if self._virtual_geo:
            self.setGeometry(self._virtual_geo)

        self._start_pos = None
        self._end_pos = None
        self._selecting = False

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QColor, QPen
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Draw semi-transparent dark overlay
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))

        if self._start_pos and self._end_pos:
            x1, y1 = self._start_pos.x(), self._start_pos.y()
            x2, y2 = self._end_pos.x(), self._end_pos.y()
            x = min(x1, x2)
            y = min(y1, y2)
            w = abs(x2 - x1)
            h = abs(y2 - y1)

            # Dim outside area (vignette effect)
            full_rect = self.rect()
            selected_rect = QRect(x, y, w, h)
            painter.fillRect(full_rect, QColor(0, 0, 0, 120))

            # Clear selected area visually
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(selected_rect, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            # Draw selection border (bright green with glow feel)
            pen = QPen(QColor(56, 189, 233), 2)
            painter.setPen(pen)
            painter.drawRect(selected_rect)

            # Draw coordinate label
            label = f"X:{x}  Y:{y}  W:{w}  H:{h}"
            font = QFont()
            font.setPointSize(11)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(selected_rect.adjusted(0, h + 4, 0, -h - 4), Qt.AlignCenter, label)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start_pos = event.position().toPoint()
            self._end_pos = self._start_pos
            self._selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._end_pos = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False

            # Compute rectangle BEFORE clearing positions so the shape
            # of the selection is captured even when start==end is a
            # degenerate case handled below.
            sx = self._start_pos.x()
            sy = self._start_pos.y()
            ex = self._end_pos.x()
            ey = self._end_pos.y()
            x = min(sx, ex)
            y = min(sy, ey)
            w = abs(ex - sx)
            h = abs(ey - sy)

            self._start_pos = None
            self._end_pos = None

            if w < 5 or h < 5:
                # Reject tiny selections
                self.close()
                return

            self.region_selected.emit((int(x), int(y), int(w), int(h)))
            self.close()

    def keyPressEvent(self, event):
        # ESC cancels
        if event.key() == Qt.Key_Escape:
            self.close()


class RegionBorderOverlay(QWidget):
    """Always-on-top green frame marking the area Region A is reading.

    The window is transparent to mouse input and never takes focus, so it can
    stay on screen permanently without interfering with anything underneath it.

    The stroke is kept strictly outside the rectangle the engine captures.  The
    capture is a plain screen grab, so anything drawn on top of the surveyed
    area would be fed to OCR as if it were part of the reading.  No text is
    painted: an on-screen label would end up inside the OCR frame.
    """

    BORDER_WIDTH = 2
    #: Gap between the frame stroke and the captured rectangle.
    PADDING = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = QRectF()
        self._affinity_applied = False
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)

    def show_physical_rect(self, x: int, y: int, w: int, h: int) -> None:
        """Place the border around a *physical* (capture-space) rectangle."""
        lx, ly, lw, lh = physical_rect_to_logical(int(x), int(y), int(w), int(h))
        pad = self.PADDING
        self.setGeometry(lx - pad, ly - pad, lw + pad * 2, lh + pad * 2)
        #: The captured rectangle, in this widget's own coordinates.
        self._frame = QRectF(pad, pad, max(1, lw), max(1, lh))
        if not self.isVisible():
            self.show()
            self._apply_capture_exclusion()
        self.raise_()
        self.update()

    def _apply_capture_exclusion(self) -> None:
        """Ask Windows to leave this window out of screen captures.

        ``WDA_EXCLUDEFROMCAPTURE`` (Windows 10 2004+) makes the border invisible
        to capture APIs such as the one used by MSS, so it can never end up
        inside an OCR frame.  On older systems the call simply fails and the
        geometric layout above keeps the overlay out of the captured rectangle
        instead.
        """
        if self._affinity_applied:
            return
        self._affinity_applied = True
        try:
            import ctypes
            import ctypes.wintypes
            import sys

            if sys.platform != "win32":
                return
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            if not ctypes.windll.user32.SetWindowDisplayAffinity(
                    ctypes.wintypes.HWND(int(self.winId())),
                    WDA_EXCLUDEFROMCAPTURE):
                logger.debug(
                    "This Windows version cannot exclude the Region A border "
                    "from screen capture; relying on its on-screen position.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not exclude the Region A border from capture: %s", exc)

    def paintEvent(self, event):  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # The stroke is centred on a path one half-width *outside* the captured
        # rectangle, so no pixel of it ever lands inside the region.
        half = self.BORDER_WIDTH / 2.0
        painter.setPen(QPen(QColor(34, 197, 94), self.BORDER_WIDTH))
        painter.drawRect(self._frame.adjusted(-half, -half, half, half))


class DashboardWidget(QWidget):
    """Dashboard widget showing live monitoring status, controls, and recent detections."""

    def __init__(self, engine: MonitoringEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._setup_ui()
        self._wire_signals()
        self._refresh_stats()

    def _setup_ui(self):
        layout = _scrolled_page(self)
        layout.setSpacing(18)
        layout.setContentsMargins(20, 20, 20, 20)
        page_header(
            layout, "Dashboard",
            "Start, pause and stop monitoring, watch the automation gate and "
            "follow the newest Region A readings as they are stored.")

        # Monitoring Control Panel
        control_group = QGroupBox("Monitoring Controls")
        control_layout = QVBoxLayout(control_group)

        # Status indicator
        self.status_label = QLabel("Status: Idle")
        status_font = QFont()
        status_font.setPointSize(12)
        status_font.setBold(True)
        self.status_label.setFont(status_font)
        self.status_label.setFixedHeight(34)
        # The pill hugs its text instead of stretching across the whole card.
        self.status_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        control_layout.addWidget(self.status_label)
        self._set_status("Status: Idle", "#64748b")

        # Control buttons -------------------------------------------------
        # Each button is a real button: raised surface, hover feedback, a
        # pressed state and a clearly greyed-out disabled state.  Which of them
        # is usable is decided in exactly one place (_sync_controls) from the
        # engine's real state, so the Start button can never be pressed while
        # monitoring is already running - and vice versa.
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self.start_btn = make_button("Start Monitoring", "success")
        self.start_btn.setToolTip(
            "Start the scan loop. Disabled while monitoring is already running "
            "or paused - use Pause/Stop first.")
        self.pause_btn = make_button("Pause", "warning")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setToolTip(
            "Pause the scan loop without losing the session. Disabled until "
            "monitoring is running.")
        self.stop_btn = make_button("Stop", "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setToolTip(
            "Stop monitoring and close the current session. Disabled until "
            "monitoring is running.")

        for button in (self.start_btn, self.pause_btn, self.stop_btn):
            button.setMinimumHeight(40)

        self.start_btn.clicked.connect(self._on_start_clicked)
        self.pause_btn.clicked.connect(self._on_pause_clicked)
        self.stop_btn.clicked.connect(self._on_stop_clicked)

        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.pause_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addStretch(1)
        control_layout.addLayout(btn_layout)

        self.control_hint = QLabel(
            "Start is only enabled when monitoring is stopped; Pause and Stop "
            "are only enabled while monitoring is running.")
        self.control_hint.setWordWrap(True)
        self.control_hint.setStyleSheet("color: #64748b; font-size: 9pt;")
        control_layout.addWidget(self.control_hint)

        layout.addWidget(control_group)

        # Automation gate + learning status
        gate_group = QGroupBox("Automation Gate && Learning Status")
        gate_layout = QFormLayout(gate_group)
        gate_layout.setHorizontalSpacing(10)
        self.gate_label = QLabel("Auto-click locked: 0 / 1,000 valid Region A readings.")
        gate_font = QFont()
        gate_font.setBold(True)
        self.gate_label.setFont(gate_font)
        self.gate_label.setWordWrap(True)
        self.model_status_label = QLabel("STALE")
        self.auto_click_status_label = QLabel("Disabled")
        self.prediction_label = QLabel("None")
        self.prediction_label.setWordWrap(True)
        self.latest_reading_label = QLabel("—")

        _form_caption(
            gate_layout, "Gate:", self.gate_label,
            "Automation unlocks only after 1,000 valid Region A readings are "
            "stored. The count is re-read from the database on every click "
            "attempt and cannot be changed here.")
        _form_caption(
            gate_layout, "Model state:", self.model_status_label,
            "VALID means the learned patterns were built from the current "
            "readings and range configuration. STALE means you must run "
            "\"Recalculate App Brain\" on the Learning page before any click.")
        _form_caption(
            gate_layout, "Auto-click:", self.auto_click_status_label,
            "Whether automated clicking may run right now, including dry-run "
            "mode, suspension during a rebuild or an engaged kill switch.")
        _form_caption(
            gate_layout, "Latest prediction:", self.prediction_label,
            "The most recent prediction produced by the learned model: the "
            "category it expects next, its confidence and its evidence.")
        _form_caption(
            gate_layout, "Region A latest:", self.latest_reading_label,
            "The newest stored Region A reading and the range (category) it "
            "belongs to. These readings are exactly what the live grid shows.")
        layout.addWidget(gate_group)

        # Statistics & Counters (Side by Side)
        stats_row = QHBoxLayout()
        stats_row.setSpacing(18)

        stats_group = QGroupBox("Monitoring Statistics")
        stats_layout = QFormLayout(stats_group)
        self.total_detections_label = QLabel("0")
        self.enabled_regions_label = QLabel("0")
        self.current_interval_label = QLabel("1.0s")
        self.last_scan_label = QLabel("Never")
        self.mode_label = QLabel("Selected regions mode")

        stats_layout.addRow(QLabel("Qualifying Region A Records:"), self.total_detections_label)
        stats_layout.addRow(QLabel("Active Regions:"), self.enabled_regions_label)
        stats_layout.addRow(QLabel("Scan Interval:"), self.current_interval_label)
        stats_layout.addRow(QLabel("Last Successful Scan:"), self.last_scan_label)
        stats_layout.addRow(QLabel("Monitoring Mode:"), self.mode_label)
        stats_row.addWidget(stats_group, 1)

        counters_group = QGroupBox("Reading Outcomes (this session)")
        counters_layout = QFormLayout(counters_group)
        self.duplicate_count_label = QLabel("0")
        self.low_confidence_label = QLabel("0")
        self.invalid_candidate_label = QLabel("0")
        self.out_of_range_label = QLabel("0")
        self.ambiguous_label = QLabel("0")
        self.empty_label = QLabel("0")
        self.ocr_failure_label = QLabel("0")

        counters_layout.addRow(QLabel("Duplicates suppressed:"), self.duplicate_count_label)
        counters_layout.addRow(QLabel("Low quality rejected:"), self.low_confidence_label)
        counters_layout.addRow(QLabel("Invalid text rejected:"), self.invalid_candidate_label)
        counters_layout.addRow(QLabel("Out of range rejected:"), self.out_of_range_label)
        counters_layout.addRow(QLabel("Ambiguous rejected:"), self.ambiguous_label)
        counters_layout.addRow(QLabel("Empty frames:"), self.empty_label)
        counters_layout.addRow(QLabel("OCR failures:"), self.ocr_failure_label)
        stats_row.addWidget(counters_group, 1)

        layout.addLayout(stats_row)

        # Live recent outcomes (6 x 10 grid, newest first) -------------------
        # The grid mirrors exactly what the automation gate learns from: the
        # stored Region A readings.  Newest at the top-left, and every box is
        # painted with the colour dedicated to the numerical range the value
        # falls into.
        live_group = QGroupBox("Live Recent Outcomes — Region A (automation-gate readings)")
        live_layout = QVBoxLayout(live_group)
        live_layout.setSpacing(10)

        self.live_caption = QLabel(
            "The newest stored Region A readings, newest first (top-left). "
            "Each box is painted with the colour of its numerical range; "
            "\"?\" marks a slot that has no reading yet.")
        self.live_caption.setWordWrap(True)
        self.live_caption.setStyleSheet("color: #64748b; font-size: 9pt;")
        live_layout.addWidget(self.live_caption)

        self.live_gate_caption = QLabel("Gate: 0 / 1,000")
        self.live_gate_caption.setStyleSheet(
            "color: #334155; font-weight: bold; font-size: 9pt;")
        live_layout.addWidget(self.live_gate_caption)

        # The grid is colour-coded, so the colours need a key: one swatch per
        # range, rebuilt only when the ranges themselves change.
        self.live_legend = QLabel()
        self.live_legend.setWordWrap(True)
        self.live_legend.setTextFormat(Qt.RichText)
        self.live_legend.setStyleSheet(
            "color: #475569; font-size: 9pt; padding: 0px;")
        self._legend_key = None
        live_layout.addWidget(self.live_legend)

        live_host = QWidget()
        self.live_grid = QGridLayout(live_host)
        self.live_grid.setContentsMargins(0, 0, 0, 0)
        self.live_grid.setSpacing(6)
        for column in range(LIVE_GRID_COLUMNS):
            self.live_grid.setColumnStretch(column, 1)

        self.live_cells: List[QLabel] = []
        for index in range(LIVE_GRID_CELLS):
            cell = QLabel("?")
            cell.setAlignment(Qt.AlignCenter)
            cell.setMinimumHeight(42)
            cell.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            cell.setWordWrap(False)
            cell.setToolTip("No reading yet")
            row, column = divmod(index, LIVE_GRID_COLUMNS)
            self.live_grid.addWidget(cell, row, column)
            self.live_cells.append(cell)
        live_layout.addWidget(live_host)

        layout.addWidget(live_group, 1)

        # Periodic refresh timer for statistics
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_stats)
        self.refresh_timer.start(2000)

    def _wire_signals(self):
        if self.engine:
            self.engine.status_updated.connect(self._on_status_updated)
            self.engine.error_occurred.connect(self._on_error_occurred)
            self.engine.detection_saved.connect(self._on_detection_saved)
            self.engine.reading_saved.connect(self._on_reading_saved)
            self.engine.prediction_made.connect(self._on_prediction_made)
            self.engine.counters_updated.connect(lambda _c: self._refresh_stats())

    # ------------------------------------------------------------------
    # Monitoring controls
    # ------------------------------------------------------------------
    def _sync_controls(self, update_status: bool = True) -> None:
        """Derive every control's enabled state from the engine's real state.

        One rule set, used after every action and every status message, so the
        buttons always show what is actually usable:

        * stopped  -> Start enabled, Pause/Stop disabled
        * running  -> Start disabled, Pause/Stop enabled
        * paused   -> Start disabled, Stop enabled, Pause reads "Resume"

        ``update_status=False`` keeps the current status text (used when the
        engine reports something else, e.g. a recalculation) while still
        re-syncing which buttons are usable.
        """
        engine = self.engine
        running = bool(engine is not None and engine._is_running)
        paused = bool(running and engine._is_paused)

        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        self.pause_btn.setText("Resume" if paused else "Pause")

        if not update_status:
            return
        if paused:
            self._set_status("Status: Paused", "#f57c00")
            self.control_hint.setText(
                "Monitoring is paused. Resume to continue or Stop to close the "
                "session; Start stays disabled until the session ends.")
        elif running:
            self._set_status("Status: Monitoring Active", "#2e7d32")
            self.control_hint.setText(
                "Monitoring is running. Start is disabled while a session is "
                "active; Pause suspends it and Stop ends it.")
        else:
            self._set_status("Status: Idle", "#64748b")
            self.control_hint.setText(
                "Start is only enabled when monitoring is stopped; Pause and "
                "Stop are only enabled while monitoring is running.")

    def _set_status(self, text: str, colour: str) -> None:
        """Render the status as a coloured pill so the state reads instantly.

        ``colour`` is the accent; the pill itself uses a tinted background with
        a dark foreground so the text keeps a comfortable contrast ratio.
        """
        tint, border, foreground = _STATUS_PILLS.get(
            colour, ("#f1f5f9", "#e2e8f0", "#475569"))
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {foreground};"
            f"background-color: {tint};"
            f"border: 1px solid {border};"
            f"border-radius: 8px;"
            f"padding: 4px 12px;"
        )

    def _on_start_clicked(self):
        if not self.engine:
            return
        success = self.engine.start_monitoring()
        if success:
            self._sync_controls()
            self._refresh_stats()
        else:
            # The engine refused (already running / cannot initialise): show
            # the truth instead of leaving the buttons in an optimistic state.
            self._sync_controls()

    def _on_pause_clicked(self):
        if not self.engine or not self.engine._is_running:
            self._sync_controls()
            return
        if self.engine._is_paused:
            self.engine.resume_monitoring()
        else:
            self.engine.pause_monitoring()
        self._sync_controls()

    def _on_stop_clicked(self):
        if not self.engine:
            return
        if self.engine._is_running:
            self.engine.stop_monitoring()
        self._sync_controls()
        self._refresh_stats()

    @Slot(str)
    def _on_status_updated(self, msg: str):
        lowered = msg.lower()
        if "started" in lowered or "resumed" in lowered:
            self._set_status("Status: Monitoring Active", "#2e7d32")
            handled = True
        elif "paused" in lowered:
            self._set_status("Status: Paused", "#f57c00")
            handled = True
        elif "stopped" in lowered:
            self._set_status("Status: Idle", "#64748b")
            handled = True
        else:
            # Any other engine message (recalculating, ...) keeps its text:
            # only the button states are re-derived underneath it.
            handled = False
        self._sync_controls(update_status=handled)

    @Slot(str)
    def _on_error_occurred(self, err: str):
        self._set_status(f"Error: {err}", "#c62828")

    @Slot(dict)
    def _on_detection_saved(self, det: dict):
        # The dashboard feed is the live 6x10 grid; a saved reading simply
        # refreshes the statistics (which repaints the grid as well).
        self._refresh_stats()

    @Slot(dict)
    def _on_reading_saved(self, reading: dict):
        value = reading.get("canonical_value", "?")
        group = _group_label(self.engine, reading.get("group_id"))
        self.latest_reading_label.setText(f"{value}  ({group})")
        self._refresh_live_grid()

    @Slot(dict)
    def _on_prediction_made(self, prediction: dict):
        group = _group_label(self.engine, prediction.get("candidate_group"))
        conf = prediction.get("confidence") or 0.0
        resolved = prediction.get("resolved_count") or 0
        self.prediction_label.setText(
            f"{group} — confidence {float(conf):.1f}% "
            f"(resolved {resolved}, len {prediction.get('pattern_length', '?')})"
        )

    # ------------------------------------------------------------------
    # Live outcome grid
    # ------------------------------------------------------------------
    def _refresh_live_grid(self) -> None:
        """Repaint the 6 x 10 grid from the gate's own reading source.

        The grid shows the newest stored Region A readings - the exact rows
        the automation gate counts - newest first at the top-left.  Every box
        carries the colour dedicated to the range its value falls into.
        """
        if not self.engine or not self.live_cells:
            return
        try:
            rows = self.engine.recent_readings(
                limit=LIVE_GRID_CELLS, region_id=REGION_A_ID)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not refresh the live grid: %s", exc)
            return

        colour_map, group_index, groups = self._group_colours()
        self._refresh_legend(colour_map, group_index, groups)
        for index, cell in enumerate(self.live_cells):
            if index >= len(rows):
                self._paint_cell(cell, "?", EMPTY_CELL_COLOUR, "No reading yet")
                continue
            row = rows[index]
            group_id = str(row.get("group_id") or "")
            if not group_id:
                group_id = self._classify(row.get("numeric_value")) or ""
            colour = colour_map.get(group_id, EMPTY_CELL_COLOUR)
            value = str(row.get("canonical_value") or "?")
            group_name = str(row.get("group_name") or "")
            bounds = group_index.get(group_id, ("", ""))
            low, high = bounds
            span = f"{low} – {high}" if high else f"from {low}"
            timestamp = str(row.get("capture_timestamp") or "").replace("T", " ")[:19]
            tooltip = (f"{value}  ·  {group_name or group_id or 'unclassified'}"
                       f"{'  (' + span + ')' if low else ''}"
                       f"\n{timestamp}")
            self._paint_cell(cell, value, colour, tooltip)

        # Keep the gate caption in step with the same source.
        try:
            gate = self.engine.gate_status()
            count = int(gate.get("region_a_count", 0))
            required = int(gate.get("required", MANDATORY_REGION_A_RECORDS))
            self.live_gate_caption.setText(
                f"Gate: {count:,} / {required:,} valid Region A readings   "
                f"({min(len(rows), LIVE_GRID_CELLS)} newest shown of {LIVE_GRID_CELLS})")
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _paint_cell(cell: QLabel, text: str, colour: str, tooltip: str) -> None:
        text_colour = PALETTE_TEXT if colour != EMPTY_CELL_COLOUR else "#64748b"
        cell.setText(text)
        cell.setToolTip(tooltip)
        cell.setStyleSheet(
            f"QLabel {{"
            f"  background-color: {colour};"
            f"  color: {text_colour};"
            f"  border: 1px solid #cbd5e1;"
            f"  border-radius: 6px;"
            f"  font-weight: 600;"
            f"  font-size: 11pt;"
            f"}}"
        )

    def _group_colours(self):
        """``(colour per group_id, (lower, upper) per group_id, groups)`` triple."""
        groups = []
        conn = self.engine._conn if self.engine else None
        try:
            if conn is not None:
                groups = config_repo.load_groups(conn)
        except Exception:  # noqa: BLE001
            groups = []
        colour_map = group_colour_map(groups)
        bounds = {
            g.group_id: (_format_bound(g.lower_bound), _format_bound(g.upper_bound))
            for g in groups
        }
        return colour_map, bounds, groups

    def _refresh_legend(self, colour_map: Dict[str, str],
                        bounds: Dict[str, tuple], groups) -> None:
        """Repaint the colour key under the live grid (only when it changed)."""
        key = (
            tuple(colour_map.items()),
            tuple(sorted((g.group_id, getattr(g, "name", ""),
                          _format_bound(getattr(g, "lower_bound", None)),
                          _format_bound(getattr(g, "upper_bound", None)))
                         for g in groups)),
        )
        if key == self._legend_key:
            return
        self._legend_key = key

        parts = ["<b>Colour key:</b> "]
        for group in groups:
            colour = colour_map.get(group.group_id)
            if not colour:
                continue
            low, high = bounds.get(group.group_id, ("", ""))
            span = f"{low} &ndash; {high}" if high else f"from {low}"
            name = html.escape(str(getattr(group, "name", group.group_id)))
            parts.append(
                f'<span style="background-color: {colour}; color: #0f172a;">'
                f"&nbsp;{name} {span}&nbsp;</span>&nbsp;"
            )
        if len(parts) == 1:
            self._legend_key = None
            self.live_legend.hide()
            return
        self.live_legend.setText("".join(parts))
        self.live_legend.show()

    def _classify(self, value) -> str:
        """Fallback range lookup for rows written before a group was stored."""
        conn = self.engine._conn if self.engine else None
        if conn is None or value is None:
            return ""
        try:
            config = config_repo.load_range_config(conn)
            return config.classify(float(value)) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _refresh_stats(self):
        if not self.engine:
            return
        try:
            # Keep the control states truthful on every refresh (the engine
            # can also be started/stopped from the tray menu).
            self._sync_controls(update_status=False)

            counters = self.engine.counters()
            interval_ms = int(self.engine._get_setting("scan_interval", "1000"))
            mode = self.engine._get_setting("monitoring_mode", "selected")

            # Qualifying Region A count comes straight from the database.
            self.total_detections_label.setText(str(self.engine.total_readings()))
            self.enabled_regions_label.setText(str(self.engine.region_count()))
            self.current_interval_label.setText(f"{interval_ms / 1000.0:.1f} s ({interval_ms} ms)")
            self.mode_label.setText("Full-screen mode" if mode == "fullscreen" else "Selected regions mode")

            self.duplicate_count_label.setText(str(counters.get("duplicates", 0)))
            self.low_confidence_label.setText(str(counters.get("low_quality", 0)))
            self.invalid_candidate_label.setText(str(counters.get("invalid_text", 0)))
            self.out_of_range_label.setText(str(counters.get("out_of_range", 0)))
            self.ambiguous_label.setText(str(counters.get("ambiguous", 0)))
            self.empty_label.setText(str(counters.get("empty", 0)))
            self.ocr_failure_label.setText(str(counters.get("ocr_failures", 0)))

            self._refresh_gate(counters)

            conn = self.engine._conn
            if conn is not None:
                try:
                    row = conn.execute(
                        "SELECT MAX(capture_timestamp) AS ts FROM readings"
                    ).fetchone()
                    ts = row["ts"] if row is not None else None
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            self.last_scan_label.setText(
                                dt.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
                        except Exception:
                            self.last_scan_label.setText(str(ts)[:19])
                    else:
                        self.last_scan_label.setText("Never")
                except Exception:
                    pass

            self._refresh_live_grid()
        except Exception as e:
            logger.error(f"Error refreshing dashboard stats: {e}")

    def _refresh_gate(self, counters: dict):
        """Show the real gate count, model state, auto-click state, prediction."""
        try:
            gate = self.engine.gate_status()
        except Exception as e:
            logger.error(f"Error reading gate status: {e}")
            return

        count = int(gate.get("region_a_count", 0))
        required = int(gate.get("required", MANDATORY_REGION_A_RECORDS))
        if gate.get("unlocked"):
            self.gate_label.setText(
                f"Auto-click unlocked: {count:,} / {required:,} valid Region A readings.")
            self.gate_label.setStyleSheet("color: #2e7d32;")
        else:
            self.gate_label.setText(
                f"Auto-click locked: {count:,} / {required:,} valid Region A readings.")
            self.gate_label.setStyleSheet("color: #c62828;")

        conn = self.engine._conn
        try:
            state = config_repo.model_status(conn) if conn is not None else "UNKNOWN"
        except Exception:
            state = "UNKNOWN"
        self.model_status_label.setText(state)
        self.model_status_label.setStyleSheet(
            "color: #2e7d32;" if state == "VALID" else "color: #c62828;")

        if gate.get("killed"):
            auto = "KILLED — automation halted by kill switch"
        elif not gate.get("rebuilding") and gate.get("auto_click_enabled"):
            auto = "Dry-run (no real clicks)" if gate.get("dry_run") else "Enabled"
        elif gate.get("rebuilding"):
            auto = "Suspended during recalculation"
        else:
            auto = "Disabled"
        self.auto_click_status_label.setText(auto)


class RegionsWidget(QWidget):
    """Two-slot region configuration: Region A and Region B.

    Both regions can be monitored and recorded in the history.
    """

    region_changed = Signal()

    def __init__(self, engine: MonitoringEngine, region_manager: RegionManager, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.region_manager = region_manager
        self._selector_overlay = None  # lazy-instantiated on demand
        self._pending_slot: Optional[str] = None
        self._virtual_geo = None
        #: Persistent green frames that show exactly where regions are reading.
        self._border_overlay_a = RegionBorderOverlay()
        self._border_overlay_b = RegionBorderOverlay()
        self._border_visible_a = False
        self._border_visible_b = False
        self._setup_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _setup_ui(self):
        layout = _scrolled_page(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)
        page_header(
            layout, "Regions",
            "Two region slots are available. Region A is the learning reader "
            "that feeds the automation gate. Region B is a store-only reader: "
            "it records its values into their own history table and never "
            "affects the model, the gate or clicking.")

        # --- Region A --------------------------------------------------
        self.region_a_group = QGroupBox(REGION_A_LABEL)
        a_layout = QFormLayout(self.region_a_group)
        a_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.region_a_geometry = QLabel("Not configured")
        self.region_a_monitor = QLabel("—")
        self.region_a_validity = QLabel("—")
        add_help_label(a_layout, "Geometry", self.region_a_geometry,
                       "The physical screen rectangle Region A reads from, in "
                       "pixels: left, top, width and height.")
        add_help_label(a_layout, "Monitor", self.region_a_monitor,
                       "Which display the rectangle lives on (primary unless "
                       "the screen layout has several monitors).")
        add_help_label(a_layout, "Coordinates valid", self.region_a_validity,
                       "Yes means the rectangle still lies inside the current "
                       "display layout. Recalibrate if monitors changed.")

        a_buttons = QGridLayout()
        a_buttons.setHorizontalSpacing(8)
        a_buttons.setVerticalSpacing(8)
        self.select_a_btn = make_button("Select Region A", "primary",
                                        "Drag a rectangle on the screen to set Region A.")
        self.recalibrate_a_btn = make_button("Recalibrate Region A", "accent",
                                             "Redraw the selection rectangle for Region A.")
        self.test_ocr_btn = make_button("Test OCR on Region A", "quiet",
                                        "Read Region A once and show every OCR candidate.")
        self.clear_a_btn = make_button("Clear Region A", "danger",
                                       "Disable Region A. History is never deleted.")
        # Action buttons keep a sensible reading width instead of inflating to
        # fill the whole card.
        for button in (self.select_a_btn, self.recalibrate_a_btn,
                       self.test_ocr_btn, self.clear_a_btn):
            button.setMinimumWidth(240)
            button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        a_buttons.addWidget(self.select_a_btn, 0, 0)
        a_buttons.addWidget(self.recalibrate_a_btn, 0, 1)
        a_buttons.addWidget(self.test_ocr_btn, 1, 0)
        a_buttons.addWidget(self.clear_a_btn, 1, 1)
        a_layout.addRow("", a_buttons)
        layout.addWidget(self.region_a_group)

        # --- Region B --------------------------------------------------
        self.region_b_group = QGroupBox(REGION_B_LABEL)
        b_layout = QFormLayout(self.region_b_group)
        b_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.region_b_geometry = QLabel("Not configured")
        self.region_b_monitor = QLabel("—")
        self.region_b_state = QLabel("Inactive — not configured")
        self.region_b_state.setStyleSheet("color: #8a6d00; font-weight: bold;")
        self.region_b_stored = QLabel("0 readings stored")
        self.region_b_latest = QLabel("—")
        add_help_label(b_layout, "Geometry", self.region_b_geometry,
                       "The physical screen rectangle Region B reads from.")
        add_help_label(b_layout, "Monitor", self.region_b_monitor,
                       "Which display Region B's rectangle lives on.")
        add_help_label(b_layout, "State", self.region_b_state,
                       "Active means Region B is being read every cycle. Its "
                       "values are stored only — they never influence the "
                       "model, the gate or clicking.")
        add_help_label(b_layout, "Readings stored", self.region_b_stored,
                       "How many Region B readings exist in their own history "
                       "table (separate from Region A).")
        add_help_label(b_layout, "Latest value", self.region_b_latest,
                       "The most recent number Region B read.")

        b_buttons = QGridLayout()
        b_buttons.setHorizontalSpacing(8)
        b_buttons.setVerticalSpacing(8)
        self.select_b_btn = make_button("Activate / Select Region B", "primary",
                                        "Drag a rectangle on the screen to activate Region B.")
        self.clear_b_btn = make_button("Deactivate Region B", "danger",
                                       "Disable Region B. Stored readings are kept.")
        for button in (self.select_b_btn, self.clear_b_btn):
            button.setMinimumWidth(240)
            button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        b_buttons.addWidget(self.select_b_btn, 0, 0)
        b_buttons.addWidget(self.clear_b_btn, 0, 1)
        b_layout.addRow("", b_buttons)
        layout.addWidget(self.region_b_group)

        layout.addStretch(1)

        self.select_a_btn.clicked.connect(lambda: self.show_selector_overlay(REGION_A_ID))
        self.recalibrate_a_btn.clicked.connect(lambda: self.show_selector_overlay(REGION_A_ID))
        self.select_b_btn.clicked.connect(lambda: self.show_selector_overlay(REGION_B_ID))
        self.test_ocr_btn.clicked.connect(self._test_ocr_on_region_a)
        self.clear_a_btn.clicked.connect(lambda: self._clear_slot(REGION_A_ID))
        self.clear_b_btn.clicked.connect(lambda: self._clear_slot(REGION_B_ID))

        # Region B's stored counters follow the engine in real time.
        self._region_b_timer = QTimer(self)
        self._region_b_timer.setInterval(2000)
        self._region_b_timer.timeout.connect(self._refresh_region_b_stats)
        self._region_b_timer.start()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def refresh(self):
        regions = self.engine._regions if self.engine else {}
        self._render_slot(REGION_A_ID, regions.get(REGION_A_ID))
        self._render_slot(REGION_B_ID, regions.get(REGION_B_ID))
        self._sync_borders(regions)
        self._refresh_region_b_stats()

    def _refresh_region_b_stats(self):
        """Update Region B's state/readings labels from the engine."""
        if not self.engine:
            return
        region_b = self.engine._regions.get(REGION_B_ID)
        active = bool(region_b and region_b.get("enabled"))
        if active:
            self.region_b_state.setText("Active — reading and storing only")
            self.region_b_state.setStyleSheet("color: #2e7d32; font-weight: bold;")
        else:
            self.region_b_state.setText("Inactive — not configured")
            self.region_b_state.setStyleSheet("color: #8a6d00; font-weight: bold;")
        try:
            count = self.engine.total_region_b_readings()
            summary = self.engine.region_b_summary()
        except Exception:  # noqa: BLE001
            count, summary = 0, {}
        self.region_b_stored.setText(
            f"{count:,} reading{'s' if count != 1 else ''} stored")
        latest = summary.get("latest_value")
        if latest is not None:
            try:
                self.region_b_latest.setText(f"{float(latest):.2f}")
            except (TypeError, ValueError):
                self.region_b_latest.setText(str(latest))
        else:
            self.region_b_latest.setText("—")

    def _sync_borders(self, regions: Dict[str, dict]):
        """Keep the always-on-top green frames over the capture areas."""
        self._sync_one_border(
            REGION_A_ID, regions.get(REGION_A_ID),
            self._border_overlay_a, "_border_visible_a"
        )
        self._sync_one_border(
            REGION_B_ID, regions.get(REGION_B_ID),
            self._border_overlay_b, "_border_visible_b"
        )

    def _sync_one_border(self, slot_id: str, region: Optional[dict],
                          overlay: RegionBorderOverlay, visibility_attr: str):
        visible = getattr(self, visibility_attr)
        usable = (
            region is not None
            and bool(region.get("enabled"))
            and int(region.get("width") or 0) > 0
            and int(region.get("height") or 0) > 0
        )
        if not usable:
            if visible:
                overlay.hide()
                setattr(self, visibility_attr, False)
            return
        try:
            overlay.show_physical_rect(
                int(region["x"]), int(region["y"]),
                int(region["width"]), int(region["height"]))
            setattr(self, visibility_attr, True)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Could not display the {slot_id} border: {exc}")

    def _render_slot(self, slot_id: str, region: Optional[dict]):
        is_a = slot_id == REGION_A_ID
        geom_label = self.region_a_geometry if is_a else self.region_b_geometry
        mon_label = self.region_a_monitor if is_a else self.region_b_monitor

        if region is None or not region.get("enabled"):
            geom_label.setText("Not configured")
            mon_label.setText("—")
            if is_a:
                self.region_a_validity.setText("—")
            return

        geom_label.setText(
            f"x={region['x']}, y={region['y']}, "
            f"{region['width']}×{region['height']}"
        )
        mon_label.setText(str(region.get("monitor_id") or "primary"))

        if is_a:
            probe = Region(
                region_id=region["region_id"], name=region["name"],
                monitor_id=region.get("monitor_id") or "primary",
                x=int(region["x"]), y=int(region["y"]),
                width=int(region["width"]), height=int(region["height"]),
            )
            if probe.is_valid:
                self.region_a_validity.setText("Yes — inside the current display layout")
                self.region_a_validity.setStyleSheet("color: #2e7d32;")
            else:
                self.region_a_validity.setText(
                    "No — outside the current display layout; recalibrate this region.")
                self.region_a_validity.setStyleSheet("color: #c62828; font-weight: bold;")

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def show_selector_overlay(self, slot_id: str = REGION_A_ID):
        """Show the fullscreen overlay and store the drag selection in ``slot_id``."""
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            QMessageBox.warning(self, "Error", "No screen available for region selection.")
            return
        self._pending_slot = slot_id
        self._virtual_geo = screen.virtualGeometry()

        self._selector_overlay = RegionSelectorOverlay()
        self._selector_overlay.region_selected.connect(self._on_region_dragged)
        self._selector_overlay.show()
        self._selector_overlay.activateWindow()
        self._selector_overlay.setFocus()

    def _on_region_dragged(self, bbox: tuple):
        """Store the dragged selection in the pending slot.

        The overlay reports *Qt logical* pixels.  Capture and clicking work in
        *physical* pixels, so on a scaled display (Windows 125 %/150 %/…) the
        rectangle must be converted before it is stored — otherwise the engine
        reads a different, usually blank, area of the screen.
        """
        slot_id = self._pending_slot or REGION_A_ID
        # Overlay-local coordinates must be offset by the virtual desktop origin
        # so they are absolute screen coordinates ready for capture and clicking.
        offset_x = self._virtual_geo.x() if self._virtual_geo else 0
        offset_y = self._virtual_geo.y() if self._virtual_geo else 0
        logical_x = int(bbox[0]) + offset_x
        logical_y = int(bbox[1]) + offset_y
        w, h = int(bbox[2]), int(bbox[3])

        x, y, w, h = logical_rect_to_physical(logical_x, logical_y, w, h)

        if not self.engine:
            return

        existing = self.engine._regions.get(slot_id)
        if existing is None:
            result = self.engine.add_region(
                name="Region A" if slot_id == REGION_A_ID else "Region B",
                monitor_id="primary",
                x=x, y=y, width=w, height=h,
                enabled=True,
                region_id=slot_id,
            )
            if result is None:
                QMessageBox.warning(
                    self, "Region Not Saved",
                    "The region could not be saved. Check the coordinates are valid.")
                return
        else:
            if not self.engine.edit_region(
                    slot_id, x, y, w, h, enabled=True):
                QMessageBox.warning(self, "Region Not Saved",
                                    "The region coordinates were rejected.")
                return
            self.engine.rename_region(
                slot_id, "Region A" if slot_id == REGION_A_ID else "Region B")

        self._pending_slot = None
        self.refresh()
        self.region_changed.emit()

    def _clear_slot(self, slot_id: str):
        label = "Region A" if slot_id == REGION_A_ID else "Region B"
        if self.engine is None or slot_id not in self.engine._regions:
            QMessageBox.information(self, "Clear Region", f"{label} is not configured.")
            return
        confirm = QMessageBox.question(
            self,
            "Confirm Clear",
            f"Disable {label}?\n\nAll historical readings are preserved and are never deleted.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.engine.delete_region(slot_id)
        self.refresh()
        self.region_changed.emit()

    # ------------------------------------------------------------------
    # OCR test
    # ------------------------------------------------------------------
    def _test_ocr_on_region_a(self):
        if not self.engine:
            return
        region = self.engine._regions.get(REGION_A_ID)
        if region is None:
            QMessageBox.information(self, "Test OCR",
                                    "Region A is not configured yet.")
            return

        from src.capture.screen_capture import ScreenCapture
        try:
            frame = ScreenCapture.get_instance().capture_screenshot(
                (int(region["x"]), int(region["y"]),
                 int(region["width"]), int(region["height"])))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Test OCR", f"Failed to capture screen: {exc}")
            return
        if frame is None:
            QMessageBox.warning(self, "Test OCR", "Failed to capture screen.")
            return

        try:
            results = self.engine._ocr.process_raw(frame)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Test OCR", f"OCR failed: {exc}")
            return

        if not results:
            QMessageBox.information(
                self, "Test OCR Result",
                "No text detected in Region A.\n\n"
                f"Coordinates: ({region['x']}, {region['y']}, "
                f"{region['width']}×{region['height']})\n"
                "Check that the target number is currently visible in this area.",
            )
            return
        lines = [f"Found {len(results)} text candidate(s):"]
        for r in results:
            canonical = r.get("canonical_value") or "(not a number)"
            lines.append(
                f"• {canonical}  ←  '{r['text']}'  (conf {float(r['confidence']):.1f}%)")
        QMessageBox.information(self, "Test OCR Result", "\n".join(lines))


class HistoryWidget(QWidget):
    """Reading history: auto-refreshing, paginated, with row deletion."""

    PAGE_SIZES = (50, 100, 250, 500)
    EXPORT_BATCH = 500
    #: How often the page re-reads the database while it is on screen.
    AUTO_REFRESH_MS = 3000

    def __init__(self, engine: MonitoringEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._page = 0
        self._total = 0
        self._setup_ui()
        self.reload()

        # The page keeps itself up to date; no manual refresh is needed.
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(self.AUTO_REFRESH_MS)
        self._auto_timer.timeout.connect(self._auto_refresh)
        self._auto_timer.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _setup_ui(self):
        layout = _scrolled_page(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)
        page_header(
            layout, "History",
            "Browse, filter, export and delete stored Region A readings. The "
            "table refreshes itself while this page is open.")

        # --- filter / export card ---------------------------------------
        filter_card = QFrame()
        filter_card.setObjectName("card")
        filter_card.setStyleSheet(_CARD_STYLE)
        filter_layout = QVBoxLayout(filter_card)
        filter_layout.setContentsMargins(14, 12, 14, 12)
        filter_layout.setSpacing(8)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by number (e.g. 2.34)...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._render_page)
        filter_row.addWidget(self.search_edit, 2)

        self.region_filter = QLineEdit()
        self.region_filter.setPlaceholderText("Filter by region...")
        self.region_filter.setClearButtonEnabled(True)
        self.region_filter.textChanged.connect(self._render_page)
        filter_row.addWidget(self.region_filter, 1)

        self.export_btn = make_button("Export CSV", "primary",
                                      "Stream every stored reading to a CSV file.")
        self.export_btn.clicked.connect(self._export_csv)
        filter_row.addWidget(self.export_btn)

        self.backup_btn = make_button("Backup Database", "accent",
                                      "Copy the live database with SQLite's safe backup API.")
        self.backup_btn.clicked.connect(self._backup_database)
        filter_row.addWidget(self.backup_btn)
        filter_layout.addLayout(filter_row)

        # --- selection / deletion ---------------------------------------
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.select_all_btn = make_button("Select All", "quiet",
                                          "Toggle selection of every row on this page.")
        self.select_all_btn.setCheckable(True)
        self.select_all_btn.toggled.connect(self._on_select_all_toggled)
        action_row.addWidget(self.select_all_btn)

        self.delete_selected_btn = make_button("Delete Selected", "danger",
                                               "Permanently delete the selected readings.")
        self.delete_selected_btn.clicked.connect(self._delete_selected)
        action_row.addWidget(self.delete_selected_btn)
        action_row.addStretch(1)

        self.auto_status_label = QLabel(
            f"Auto-updating every {self.AUTO_REFRESH_MS // 1000} s")
        self.auto_status_label.setStyleSheet("color: #555555;")
        action_row.addWidget(self.auto_status_label)
        filter_layout.addLayout(action_row)
        layout.addWidget(filter_card)

        # --- pagination -------------------------------------------------
        pager_card = QFrame()
        pager_card.setObjectName("card")
        pager_card.setStyleSheet(_CARD_STYLE)
        pager = QHBoxLayout(pager_card)
        pager.setContentsMargins(14, 8, 14, 8)
        pager.setSpacing(8)
        pager.addWidget(QLabel("Page size:"))
        self.page_size_combo = QComboBox()
        for size in self.PAGE_SIZES:
            self.page_size_combo.addItem(f"{size} per page", size)
        self.page_size_combo.setCurrentIndex(1)  # 100
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        pager.addWidget(self.page_size_combo)

        self.first_btn = make_button("First", "quiet")
        self.prev_btn = make_button("Prev", "quiet")
        self.next_btn = make_button("Next", "quiet")
        self.last_btn = make_button("Last", "quiet")
        self.first_btn.setToolTip("Jump to the first page")
        self.prev_btn.setToolTip("Go to the previous page")
        self.next_btn.setToolTip("Go to the next page")
        self.last_btn.setToolTip("Jump to the last page")
        self.first_btn.clicked.connect(lambda: self.go_to_page(0))
        self.prev_btn.clicked.connect(lambda: self.go_to_page(self._page - 1))
        self.next_btn.clicked.connect(lambda: self.go_to_page(self._page + 1))
        self.last_btn.clicked.connect(lambda: self.go_to_page(self.page_count() - 1))

        pager.addWidget(self.first_btn)
        pager.addWidget(self.prev_btn)
        self.page_label = QLabel("Page 1 / 1")
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_btn)
        pager.addWidget(self.last_btn)
        pager.addStretch(1)
        self.total_label = QLabel("0 valid Region A records")
        pager.addWidget(self.total_label)
        layout.addWidget(pager_card)

        # --- table ------------------------------------------------------
        self.table = PlaceholderTable(
            0, 7,
            "No readings to show yet - clear the filters, or start "
            "monitoring on the Dashboard to collect data.")
        self.table.setHorizontalHeaderLabels(
            ["Canonical Value", "Original Text", "Group", "Region",
             "Confidence", "Capture Timestamp", "Session"]
        )
        configure_fit_table(self.table, min_height=320)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._sync_select_all_btn)
        fit_columns(self.table)
        layout.addWidget(self.table, 1)

    # ------------------------------------------------------------------
    # Paging
    # ------------------------------------------------------------------
    def page_size(self) -> int:
        data = self.page_size_combo.currentData()
        return int(data) if data else self.PAGE_SIZES[1]

    def page_count(self) -> int:
        size = self.page_size()
        return max(1, (self._total + size - 1) // size)

    def _on_page_size_changed(self):
        self.go_to_page(0)

    def go_to_page(self, page: int):
        self._page = max(0, min(int(page), self.page_count() - 1))
        self._render_page()

    def reload(self):
        """Re-read the total record count from the database and redraw the page."""
        if not self.engine:
            return
        try:
            self._total = int(self.engine.total_readings())
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error reading total readings: {e}")
            self._total = 0
        self._page = max(0, min(self._page, self.page_count() - 1))
        self._render_page()
        self.auto_status_label.setText(
            f"Auto-updated {datetime.now().strftime('%H:%M:%S')}")

    def _auto_refresh(self):
        """Periodic redraw while the page is on screen.

        Nothing is redrawn while the user has rows selected, so a deletion in
        progress is never pulled out from under them.
        """
        if not self.isVisible():
            return
        model = self.table.selectionModel()
        if model is not None and model.hasSelection():
            return
        self.reload()

    def _render_page(self):
        if not self.engine:
            return
        size = self.page_size()
        offset = self._page * size
        rows = self.engine.recent_readings(limit=size, offset=offset)

        search = self.search_edit.text().strip().lower()
        region_query = self.region_filter.text().strip().lower()
        group_labels = _group_bound_labels(self.engine._conn)

        filtered = []
        for r in rows:
            val = str(r.get("canonical_value", ""))
            reg = str(r.get("region_name") or r.get("region_id") or "")
            if search and search not in val.lower():
                continue
            if region_query and region_query not in reg.lower():
                continue
            filtered.append(r)

        self.table.setRowCount(len(filtered))
        for row_idx, r in enumerate(filtered):
            conf = r.get("ocr_confidence") or 0.0
            ts = r.get("capture_timestamp") or ""
            if "T" in ts:
                ts = ts.replace("T", " ")[:19]
            value_item = QTableWidgetItem(str(r.get("canonical_value") or ""))
            # The reading's own id travels with the row so the selected rows can
            # be deleted without re-deriving them from the visible text.
            value_item.setData(Qt.UserRole, r.get("reading_id"))
            self.table.setItem(row_idx, 0, value_item)
            self.table.setItem(row_idx, 1, QTableWidgetItem(str(r.get("original_text") or "")))
            group_id = str(r.get("group_id") or "")
            self.table.setItem(
                row_idx, 2,
                QTableWidgetItem(group_labels.get(group_id, group_id or "-")))
            self.table.setItem(
                row_idx, 3,
                QTableWidgetItem(str(r.get("region_name") or r.get("region_id") or "")))
            self.table.setItem(row_idx, 4, QTableWidgetItem(f"{float(conf):.1f}%"))
            self.table.setItem(row_idx, 5, QTableWidgetItem(ts))
            self.table.setItem(row_idx, 6, QTableWidgetItem(str(r.get("session_id") or "")))

        pages = self.page_count()
        self.page_label.setText(f"Page {self._page + 1} / {pages}")
        self.total_label.setText(f"{self._total:,} valid Region A records")
        has_prev = self._page > 0
        has_next = self._page < pages - 1
        self.first_btn.setEnabled(has_prev)
        self.prev_btn.setEnabled(has_prev)
        self.next_btn.setEnabled(has_next)
        self.last_btn.setEnabled(has_next)
        self._sync_select_all_btn()

    # ------------------------------------------------------------------
    # Selection / deletion
    # ------------------------------------------------------------------
    def _on_select_all_toggled(self, checked: bool):
        if checked:
            self.table.selectAll()
        else:
            self.table.clearSelection()

    def _sync_select_all_btn(self):
        """Keep the Select All toggle in step with the real table selection."""
        model = self.table.selectionModel()
        all_selected = (
            model is not None
            and self.table.rowCount() > 0
            and len(model.selectedRows()) == self.table.rowCount()
        )
        self.select_all_btn.blockSignals(True)
        self.select_all_btn.setChecked(all_selected)
        self.select_all_btn.blockSignals(False)

    def _selected_reading_ids(self) -> List[int]:
        model = self.table.selectionModel()
        if model is None:
            return []
        ids: List[int] = []
        for index in model.selectedRows():
            item = self.table.item(index.row(), 0)
            value = item.data(Qt.UserRole) if item is not None else None
            if value is not None:
                ids.append(int(value))
        return ids

    def _delete_selected(self):
        """Permanently delete the rows the user has selected."""
        if not self.engine:
            return
        reading_ids = self._selected_reading_ids()
        if not reading_ids:
            QMessageBox.information(
                self, "Delete Readings",
                "Select the rows you want to delete first.")
            return
        confirm = QMessageBox.question(
            self, "Delete Readings",
            f"Permanently delete {len(reading_ids):,} selected reading(s)?\n\n"
            "This cannot be undone. The learned model will be marked stale and "
            "must be rebuilt from the remaining readings before automated "
            "clicking can resume.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            deleted = self.engine.delete_readings(reading_ids)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to delete readings: {e}")
            QMessageBox.critical(self, "Delete Readings",
                                 f"Failed to delete readings: {e}")
            return
        self.table.clearSelection()
        self.reload()
        QMessageBox.information(
            self, "Delete Readings", f"Deleted {deleted:,} reading(s).")

    # ------------------------------------------------------------------
    # Export / backup
    # ------------------------------------------------------------------
    def _export_csv(self):
        """Stream every stored reading to CSV in bounded batches."""
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Export Readings to CSV", "screen_monitor_history.csv",
            "CSV Files (*.csv);;All Files (*)"
        )
        if not filepath:
            return

        written = 0
        group_labels = _group_bound_labels(self.engine._conn if self.engine else None)
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Reading ID", "Canonical Value", "Original Text", "Group",
                    "Region", "Confidence (%)", "Capture Timestamp",
                    "Processing Timestamp", "Session ID", "Source",
                ])
                offset = 0
                while True:
                    batch = self.engine.recent_readings(
                        limit=self.EXPORT_BATCH, offset=offset)
                    if not batch:
                        break
                    for r in batch:
                        group_id = str(r.get("group_id") or "")
                        writer.writerow([
                            r.get("reading_id", ""),
                            r.get("canonical_value", ""),
                            r.get("original_text") or "",
                            group_labels.get(group_id, group_id),
                            r.get("region_name") or r.get("region_id") or "",
                            r.get("ocr_confidence") or 0.0,
                            r.get("capture_timestamp", ""),
                            r.get("processing_timestamp", ""),
                            r.get("session_id", ""),
                            r.get("source", "live"),
                        ])
                    offset += len(batch)
                    written += len(batch)
            QMessageBox.information(
                self, "Export Complete", f"Exported {written:,} records to {filepath}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to export CSV: {e}")
            QMessageBox.critical(self, "Export Error", f"Failed to export CSV: {e}")

    def _backup_database(self):
        """Copy the live database using SQLite's online backup API."""
        if not self.engine or self.engine._conn is None:
            QMessageBox.warning(self, "Backup", "The database is not available.")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"screen_monitor_backup_{stamp}.db"
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Backup Database", default_name,
            "SQLite Database (*.db);;All Files (*)"
        )
        if not filepath:
            return

        try:
            dest = sqlite3.connect(filepath)
            try:
                with dest:
                    self.engine._conn.backup(dest)
            finally:
                dest.close()
            QMessageBox.information(
                self, "Backup Complete",
                f"Database backed up to:\n{filepath}\n\n"
                "This operation never modifies the existing database.")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to back up database: {e}")
            QMessageBox.critical(self, "Backup Error", f"Failed to back up database: {e}")


def _format_bound(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.2f}"


def _group_bound_labels(conn) -> Dict[str, str]:
    """Map every ``group_id`` to the group's lower bound, its user-facing name.

    The internal identifiers (``g1``, ``g2``, …) mean nothing to a user, so the
    group's lower bound (``1.00``, ``1.26``, …) is shown in their place.
    """
    if conn is None:
        return {}
    try:
        return {g.group_id: _format_bound(g.lower_bound)
                for g in config_repo.load_groups(conn)}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read group bounds: %s", exc)
        return {}


def _group_label(engine, group_id) -> str:
    """Human-readable label for a single group id, falling back to the id."""
    if not group_id:
        return "—"
    label = _group_bound_labels(engine._conn if engine else None).get(str(group_id))
    return label if label else str(group_id)


class SettingsWidget(QWidget):
    """Capture cadence, OCR threshold, save mode and the nine numerical groups."""

    settings_changed = Signal(dict)

    def __init__(self, engine: MonitoringEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._group_ids: List[str] = []
        self._setup_ui()
        self._load_settings()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        layout = _scrolled_page(self)
        layout.setSpacing(18)
        layout.setContentsMargins(20, 20, 20, 20)
        page_header(
            layout, "Settings",
            "Scan cadence, OCR thresholds, storage behaviour and the "
            "numerical ranges every page and the automation rely on.")

        # --- Capture & scan frequency -----------------------------------
        interval_group = QGroupBox("Capture && Scan Frequency")
        interval_layout = QFormLayout(interval_group)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(200, 60000)
        self.interval_spin.setSingleStep(250)
        self.interval_label = QLabel("1000 ms (1.0 s)")
        self.interval_spin.valueChanged.connect(
            lambda v: self.interval_label.setText(f"{v} ms ({v / 1000.0:.1f} s)")
        )
        interval_layout.addRow(QLabel("Scan Interval:"), self.interval_spin)
        interval_layout.addRow("", self.interval_label)
        layout.addWidget(interval_group)

        # --- OCR thresholds ---------------------------------------------
        confidence_group = QGroupBox("OCR Thresholds")
        confidence_layout = QFormLayout(confidence_group)
        self.confidence_spin = QDoubleSpinBox()
        self.confidence_spin.setRange(1.0, 100.0)
        self.confidence_spin.setSingleStep(5.0)
        confidence_layout.addRow(QLabel("Minimum OCR Confidence (%):"), self.confidence_spin)
        layout.addWidget(confidence_group)

        # --- Save mode ---------------------------------------------------
        storage_group = QGroupBox("Storage")
        storage_layout = QFormLayout(storage_group)
        self.save_mode_combo = QComboBox()
        self.save_mode_combo.addItem("Save only when the value changes", "on_change")
        self.save_mode_combo.addItem("Save every scan", "every")
        storage_layout.addRow(QLabel("Save Mode:"), self.save_mode_combo)
        storage_note = QLabel(
            "Readings are never deleted or reset. Rejected frames are recorded as "
            "diagnostics only and never stored as a zero value."
        )
        storage_note.setWordWrap(True)
        storage_note.setStyleSheet("color: #555555;")
        storage_layout.addRow("", storage_note)
        layout.addWidget(storage_group)

        # --- Monitoring target mode --------------------------------------
        mode_group = QGroupBox("Monitoring Target Mode")
        mode_layout = QVBoxLayout(mode_group)
        self.mode_selected = QRadioButton(
            "Selected regions monitoring (only capture the configured regions)")
        self.mode_fullscreen = QRadioButton(
            "Full-screen monitoring (scan the entire display)")
        mode_layout.addWidget(self.mode_selected)
        mode_layout.addWidget(self.mode_fullscreen)
        layout.addWidget(mode_group)

        # --- Numerical groups ---------------------------------------------
        groups_group = QGroupBox("Numerical Groups (Range Configuration)")
        groups_layout = QVBoxLayout(groups_group)
        groups_note = QLabel(
            "Nine default groups. Names, abbreviations and boundaries are editable. "
            "Leave the Upper Bound empty for an open-ended final group. "
            "Saving a new configuration marks the model STALE until you run "
            "\"Recalculate App Brain\"."
        )
        groups_note.setWordWrap(True)
        groups_note.setStyleSheet("color: #555555;")
        groups_layout.addWidget(groups_note)

        self.groups_table = QTableWidget(0, 4)
        self.groups_table.setHorizontalHeaderLabels(
            ["Name", "Abbreviation", "Lower Bound", "Upper Bound"])
        configure_fit_table(self.groups_table, min_height=340)
        self.groups_table.setEditTriggers(QAbstractItemView.DoubleClicked)
        groups_layout.addWidget(self.groups_table)

        self.save_groups_btn = make_button(
            "Save Group Configuration", "accent",
            "Persist the edited category ranges. The model becomes stale and "
            "must be recalculated.")
        self.save_groups_btn.clicked.connect(self._save_groups)
        groups_layout.addWidget(self.save_groups_btn)
        layout.addWidget(groups_group)

        # --- Save ---------------------------------------------------------
        self.save_btn = make_button("Save Settings", "primary",
                                    "Persist every setting on this page.")
        self.save_btn.clicked.connect(self._save_settings)
        layout.addWidget(self.save_btn)

        layout.addStretch(1)

    # ------------------------------------------------------------------
    def _load_settings(self):
        if not self.engine or self.engine._conn is None:
            return
        conn = self.engine._conn
        interval = config_repo.get_setting_int(conn, "scan_interval", 1000)
        self.interval_spin.setValue(interval)
        self.interval_label.setText(f"{interval} ms ({interval / 1000.0:.1f} s)")

        confidence = config_repo.get_setting_float(conn, "min_ocr_confidence", 70.0)
        self.confidence_spin.setValue(confidence)

        save_mode = config_repo.get_setting(conn, "save_mode", "on_change")
        index = self.save_mode_combo.findData(save_mode)
        self.save_mode_combo.setCurrentIndex(index if index >= 0 else 0)

        mode = config_repo.get_setting(conn, "monitoring_mode", "selected")
        if mode == "fullscreen":
            self.mode_fullscreen.setChecked(True)
        else:
            self.mode_selected.setChecked(True)

        self._load_groups_table()

    def _load_groups_table(self):
        if not self.engine or self.engine._conn is None:
            return
        groups = config_repo.load_groups(self.engine._conn)
        self._group_ids = [g.group_id for g in groups]
        self.groups_table.setRowCount(len(groups))
        for row, g in enumerate(groups):
            self.groups_table.setItem(row, 0, QTableWidgetItem(g.name))
            self.groups_table.setItem(row, 1, QTableWidgetItem(g.abbreviation))
            self.groups_table.setItem(row, 2, QTableWidgetItem(_format_bound(g.lower_bound)))
            self.groups_table.setItem(row, 3, QTableWidgetItem(_format_bound(g.upper_bound)))
        fit_columns(self.groups_table)

    # ------------------------------------------------------------------
    def _save_settings(self):
        if not self.engine or self.engine._conn is None:
            return
        conn = self.engine._conn
        interval_ms = int(self.interval_spin.value())
        confidence = float(self.confidence_spin.value())
        mode = "fullscreen" if self.mode_fullscreen.isChecked() else "selected"
        save_mode = self.save_mode_combo.currentData() or "on_change"

        config_repo.set_setting(conn, "scan_interval", str(interval_ms))
        config_repo.set_setting(conn, "min_ocr_confidence", str(confidence))
        config_repo.set_setting(conn, "monitoring_mode", mode)
        config_repo.set_setting(conn, "save_mode", save_mode)
        conn.commit()

        self.engine.reload()

        self.settings_changed.emit({
            "scan_interval": str(interval_ms),
            "min_ocr_confidence": str(confidence),
            "monitoring_mode": mode,
            "save_mode": save_mode,
        })
        QMessageBox.information(self, "Settings Saved", "Settings updated successfully.")

    def _save_groups(self):
        if not self.engine or self.engine._conn is None:
            return

        groups: List[Group] = []
        for row, group_id in enumerate(self._group_ids):
            def cell(col: int) -> str:
                item = self.groups_table.item(row, col)
                return item.text().strip() if item is not None else ""

            name = cell(0) or group_id
            abbreviation = cell(1) or group_id.upper()
            try:
                lower = float(cell(2))
            except ValueError:
                QMessageBox.warning(
                    self, "Invalid Configuration",
                    f"Group '{name}': the lower bound must be a number.")
                return
            upper_text = cell(3)
            if upper_text == "":
                upper = None
            else:
                try:
                    upper = float(upper_text)
                except ValueError:
                    QMessageBox.warning(
                        self, "Invalid Configuration",
                        f"Group '{name}': the upper bound must be a number or empty.")
                    return

            groups.append(Group(
                group_id=group_id, ordinal=row + 1, name=name,
                abbreviation=abbreviation, lower_bound=lower, upper_bound=upper))

        try:
            version = config_repo.save_range_config(
                self.engine._conn, groups, note="edited in Settings")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Group Configuration Rejected", str(e))
            return

        self.engine.reload()
        self._load_groups_table()
        self.settings_changed.emit({"range_config_version": str(version)})
        QMessageBox.information(
            self, "Group Configuration Saved",
            f"Range configuration version {version} saved.\n\n"
            "Historical readings were not modified. The derived model is now STALE: "
            "run \"Recalculate App Brain\" on the Learning page before automated "
            "clicking can resume.")


class LearningWidget(QWidget):
    """Pattern-learning view with the mandatory "Recalculate App Brain" action."""

    recalc_progress = Signal(float, str)
    recalc_finished = Signal(dict)

    def __init__(self, engine: MonitoringEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._setup_ui()
        self.recalc_progress.connect(self._on_recalc_progress)
        self.recalc_finished.connect(self._on_recalc_finished)
        self.refresh()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        layout = _scrolled_page(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)
        page_header(
            layout, "Learning",
            "What the model learned from stored readings, how accurate its "
            "predictions are, and the one action that rebuilds it.")

        # --- twin model summaries side by side ---------------------------
        summary_row = QHBoxLayout()
        summary_row.setSpacing(16)

        summary_group = QGroupBox("Model Summary — All categories")
        summary_layout = QFormLayout(summary_group)
        summary_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.model_version_label = QLabel("-")
        self.model_state_label = QLabel("STALE")
        self.pattern_count_label = QLabel("0")
        self.observed_count_label = QLabel("0")
        self.predictions_label = QLabel("0 total / 0 resolved")
        self.accuracy_label = QLabel("-")
        self.unresolved_label = QLabel("0")
        self.recent_label = QLabel("-")
        self.evidence_label = QLabel("-")
        add_help_row(summary_layout, "Model version", self.model_version_label,
                     "The recalculation version currently in use. Every pattern "
                     "and prediction shown belongs to this version.")
        add_help_row(summary_layout, "Model state", self.model_state_label,
                     "VALID means the model matches the stored readings. STALE "
                     "means readings changed since the last recalculation.")
        add_help_row(summary_layout, "Pattern rows", self.pattern_count_label,
                     "Learned pattern rows in the model (sequence lengths 4-8).")
        add_help_row(summary_layout, "Observations", self.observed_count_label,
                     "How many sequences the learner has seen so far.")
        add_help_row(summary_layout, "Predictions", self.predictions_label,
                     "Total predictions made and how many have been resolved "
                     "against a later real outcome.")
        add_help_row(summary_layout, "Resolved accuracy", self.accuracy_label,
                     "Share of resolved predictions that turned out correct.")
        add_help_row(summary_layout, "Unresolved", self.unresolved_label,
                     "Predictions still waiting for a later outcome.")
        add_help_row(summary_layout, "Recent performance", self.recent_label,
                     "Accuracy over the most recent resolved predictions, "
                     "compared with the safeguard threshold.")
        add_help_row(summary_layout, "Evidence sufficiency", self.evidence_label,
                     "Resolved predictions versus the minimum needed before "
                     "automated clicking may resume.")
        summary_row.addWidget(summary_group, 1)

        filtered_group = QGroupBox(
            "Model Summary — Clickable categories (Settings)")
        filtered_layout = QFormLayout(filtered_group)
        filtered_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.f_model_version_label = QLabel("-")
        self.f_categories_label = QLabel("-")
        self.f_readings_label = QLabel("0")
        self.f_patterns_label = QLabel("0")
        self.f_predictions_label = QLabel("0 total / 0 resolved")
        self.f_accuracy_label = QLabel("-")
        self.f_unresolved_label = QLabel("0")
        self.f_recent_label = QLabel("-")
        add_help_row(filtered_layout, "Model version", self.f_model_version_label,
                     "Same model version as the left panel; only the category "
                     "filter differs.")
        add_help_row(filtered_layout, "Categories", self.f_categories_label,
                     "The categories ticked in Settings as clickable. Every "
                     "number on this panel counts only those categories.")
        add_help_row(filtered_layout, "Readings", self.f_readings_label,
                     "Stored Region A readings that fall inside the clickable "
                     "categories.")
        add_help_row(filtered_layout, "Pattern rows", self.f_patterns_label,
                     "Learned patterns whose next group is a clickable category.")
        add_help_row(filtered_layout, "Predictions", self.f_predictions_label,
                     "Predictions for clickable categories, total and resolved.")
        add_help_row(filtered_layout, "Resolved accuracy", self.f_accuracy_label,
                     "Accuracy restricted to clickable categories.")
        add_help_row(filtered_layout, "Unresolved", self.f_unresolved_label,
                     "Clickable-category predictions still awaiting an outcome.")
        add_help_row(filtered_layout, "Recent performance", self.f_recent_label,
                     "Recent accuracy restricted to clickable categories.")
        summary_row.addWidget(filtered_group, 1)
        layout.addLayout(summary_row)

        # --- recalculation ------------------------------------------------
        recalc_group = QGroupBox("Model Recalculation")
        recalc_layout = QVBoxLayout(recalc_group)
        note = QLabel(
            "Recalculation rebuilds every derived pattern and prediction from the "
            "stored Region A readings. Automated clicking is suspended while it runs. "
            "Raw readings are never deleted or modified."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555555;")
        recalc_layout.addWidget(note)

        recalc_row = QHBoxLayout()
        self.recalc_btn = make_button(
            "Recalculate App Brain", "primary",
            "Rebuild every pattern and prediction from stored readings.")
        self.recalc_btn.clicked.connect(self._on_recalculate)
        recalc_row.addWidget(self.recalc_btn)
        self.recalc_status_label = QLabel("Idle")
        recalc_row.addWidget(self.recalc_status_label, 1)
        recalc_layout.addLayout(recalc_row)

        self.recalc_progress_bar = QProgressBar()
        self.recalc_progress_bar.setRange(0, 100)
        self.recalc_progress_bar.setValue(0)
        recalc_layout.addWidget(self.recalc_progress_bar)
        layout.addWidget(recalc_group)

        # --- patterns -----------------------------------------------------
        patterns_group = QGroupBox("Learned Patterns (lengths 4-8)")
        patterns_layout = QVBoxLayout(patterns_group)
        patterns_hint = help_text(
            "Click any column header to sort A-Z; click again for Z-A. "
            "Numeric columns sort by value, not by text.")
        patterns_layout.addWidget(patterns_hint)
        self.patterns_table = PlaceholderTable(
            0, 8,
            "No learned patterns yet - the model learns once enough Region A "
            "readings have been stored and the brain has been recalculated.")
        self.patterns_table.setHorizontalHeaderLabels(
            ["Length", "Pattern", "Next Group", "Opportunities",
             "Resolved", "Correct", "Incorrect", "Accuracy %"])
        configure_fit_table(self.patterns_table, min_height=280)
        self.patterns_table.setSortingEnabled(True)
        patterns_layout.addWidget(self.patterns_table)
        layout.addWidget(patterns_group, 1)

        # --- predictions --------------------------------------------------
        predictions_group = QGroupBox("Recent Predictions")
        predictions_layout = QVBoxLayout(predictions_group)
        predictions_hint = help_text(
            "Click any column header to sort A-Z; click again for Z-A.")
        predictions_layout.addWidget(predictions_hint)
        self.predictions_table = PlaceholderTable(
            0, 7,
            "No predictions yet - predictions appear after the model has seen "
            "enough sequences to forecast the next category.")
        self.predictions_table.setHorizontalHeaderLabels(
            ["ID", "Length", "Predicted", "Confidence %",
             "Resolved Count", "Recent Acc %", "Outcome"])
        configure_fit_table(self.predictions_table, min_height=260)
        self.predictions_table.setSortingEnabled(True)
        predictions_layout.addWidget(self.predictions_table)
        layout.addWidget(predictions_group, 1)

        self.refresh_btn = make_button("Refresh", "accent",
                                       "Re-read the model from the database now.")
        self.refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(self.refresh_btn)

    # ------------------------------------------------------------------
    def refresh(self):
        if not self.engine or self.engine._conn is None:
            return
        conn = self.engine._conn
        try:
            summary = self.engine.learning_summary()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error reading learning summary: {e}")
            return

        predictions = summary.get("predictions") or {}
        self.model_version_label.setText(str(summary.get("model_version", 0)))
        self.pattern_count_label.setText(str(summary.get("patterns", 0)))
        self.observed_count_label.setText(str(summary.get("observed", 0)))

        try:
            state = config_repo.model_status(conn)
        except Exception:  # noqa: BLE001
            state = "UNKNOWN"
        self.model_state_label.setText(state)
        self.model_state_label.setStyleSheet(
            "color: #2e7d32; font-weight: bold;" if state == "VALID"
            else "color: #c62828; font-weight: bold;")

        total = int(predictions.get("total") or 0)
        resolved = int(predictions.get("resolved") or 0)
        correct = int(predictions.get("correct") or 0)
        incorrect = int(predictions.get("incorrect") or 0)
        self.predictions_label.setText(f"{total} total / {resolved} resolved")
        accuracy = predictions.get("accuracy")
        self.accuracy_label.setText(
            f"{float(accuracy):.1f}% ({correct} correct / {incorrect} incorrect)"
            if accuracy is not None else "-")
        self.unresolved_label.setText(str(int(predictions.get("unresolved") or 0)))

        recent_window = config_repo.get_setting_int(conn, "recent_window", 50)
        recent_min = config_repo.get_setting_float(conn, "recent_min_accuracy", 60.0)
        recent_values = [
            p for p in self.engine.recent_predictions(limit=recent_window)
            if p.get("resolved_at") is not None
        ]
        if recent_values:
            recent_correct = sum(1 for p in recent_values if p.get("is_correct") == 1)
            recent_acc = 100.0 * recent_correct / len(recent_values)
            self.recent_label.setText(
                f"{recent_acc:.1f}% over the last {len(recent_values)} resolved "
                f"predictions (safeguard {recent_min:.0f}%)")
        else:
            self.recent_label.setText(
                f"No resolved predictions yet (safeguard {recent_min:.0f}%)")

        min_evidence = config_repo.get_setting_int(conn, "min_evidence", 30)
        self.evidence_label.setText(
            f"{resolved} resolved vs {min_evidence} required "
            f"({'sufficient' if resolved >= min_evidence else 'insufficient'})")

        self._refresh_filtered_summary(conn, summary)
        self._fill_patterns()
        self._fill_predictions()

    def _refresh_filtered_summary(self, conn, overall_summary: dict) -> None:
        """Fill the right-hand summary restricted to clickable categories."""
        try:
            allowed = config_repo.get_allowed_categories(conn)
        except Exception:  # noqa: BLE001
            allowed = []
        if not allowed:
            self.f_categories_label.setText("None selected — showing zeros")
            self.f_model_version_label.setText("-")
            self.f_readings_label.setText("0")
            self.f_patterns_label.setText("0")
            self.f_predictions_label.setText("0 total / 0 resolved")
            self.f_accuracy_label.setText("-")
            self.f_unresolved_label.setText("0")
            self.f_recent_label.setText("-")
            return

        names: List[str] = []
        group_labels = _group_bound_labels(conn)
        for group_id in allowed:
            names.append(group_labels.get(group_id, group_id))
        self.f_categories_label.setText(", ".join(names) or "-")

        try:
            filtered = self.engine.learning_summary(group_ids=allowed)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error reading filtered learning summary: {e}")
            return

        self.f_model_version_label.setText(str(filtered.get("model_version", 0)))
        self.f_readings_label.setText(f"{int(filtered.get('readings') or 0):,}")
        self.f_patterns_label.setText(f"{int(filtered.get('patterns') or 0):,}")

        predictions = filtered.get("predictions") or {}
        total = int(predictions.get("total") or 0)
        resolved = int(predictions.get("resolved") or 0)
        correct = int(predictions.get("correct") or 0)
        incorrect = int(predictions.get("incorrect") or 0)
        self.f_predictions_label.setText(f"{total} total / {resolved} resolved")
        accuracy = predictions.get("accuracy")
        self.f_accuracy_label.setText(
            f"{float(accuracy):.1f}% ({correct} correct / {incorrect} incorrect)"
            if accuracy is not None else "-")
        self.f_unresolved_label.setText(str(int(predictions.get("unresolved") or 0)))

        recent = filtered.get("recent") or {}
        recent_acc = recent.get("accuracy") if isinstance(recent, dict) else recent
        recent_count = recent.get("count") if isinstance(recent, dict) else None
        if recent_acc is not None:
            suffix = f" over the last {int(recent_count)}" if recent_count else ""
            self.f_recent_label.setText(f"{float(recent_acc):.1f}%{suffix}")
        else:
            self.f_recent_label.setText("No resolved predictions yet")

    def _fill_patterns(self):
        rows = self.engine.top_patterns(limit=200)
        group_labels = _group_bound_labels(self.engine._conn if self.engine else None)

        def fill():
            self.patterns_table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                opportunities = int(r.get("opportunities") or 0)
                resolved = int(r.get("resolved") or 0)
                correct = int(r.get("correct") or 0)
                incorrect = int(r.get("incorrect") or 0)
                accuracy = (100.0 * correct / resolved) if resolved else 0.0
                group_id = str(r.get("candidate_group") or "")
                self.patterns_table.setItem(
                    i, 0, make_item(str(r.get("pattern_length", ""))))
                self.patterns_table.setItem(
                    i, 1, make_item(str(r.get("pattern_sequence", ""))))
                self.patterns_table.setItem(
                    i, 2, make_item(group_labels.get(group_id, group_id)))
                self.patterns_table.setItem(i, 3, make_item(str(opportunities), opportunities))
                self.patterns_table.setItem(i, 4, make_item(str(resolved), resolved))
                self.patterns_table.setItem(i, 5, make_item(str(correct), correct))
                self.patterns_table.setItem(i, 6, make_item(str(incorrect), incorrect))
                self.patterns_table.setItem(
                    i, 7, make_item(f"{accuracy:.1f}", accuracy))

        sort_safely(self.patterns_table, fill)
        fit_columns(self.patterns_table)

    def _fill_predictions(self):
        rows = self.engine.recent_predictions(limit=100)
        group_labels = _group_bound_labels(self.engine._conn if self.engine else None)

        def label(group_id) -> str:
            key = str(group_id or "")
            return group_labels.get(key, key)

        def fill():
            self.predictions_table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                if r.get("resolved_at") is None:
                    outcome = "unresolved"
                elif r.get("is_correct") == 1:
                    outcome = "correct"
                else:
                    outcome = f"incorrect (saw {label(r.get('actual_group')) or '?'})"
                recent = r.get("recent_accuracy")
                confidence = float(r.get("confidence") or 0.0)
                resolved_count = int(r.get("resolved_count", 0))
                self.predictions_table.setItem(
                    i, 0, make_item(str(r.get("prediction_id", ""))))
                self.predictions_table.setItem(
                    i, 1, make_item(str(r.get("pattern_length", ""))))
                self.predictions_table.setItem(
                    i, 2, make_item(label(r.get("candidate_group"))))
                self.predictions_table.setItem(
                    i, 3, make_item(f"{confidence:.1f}", confidence))
                self.predictions_table.setItem(
                    i, 4, make_item(str(resolved_count), resolved_count))
                if recent is not None:
                    self.predictions_table.setItem(
                        i, 5, make_item(f"{float(recent):.1f}", float(recent)))
                else:
                    self.predictions_table.setItem(i, 5, make_item("-"))
                self.predictions_table.setItem(i, 6, make_item(outcome))

        sort_safely(self.predictions_table, fill)
        fit_columns(self.predictions_table)

    # ------------------------------------------------------------------
    def _on_recalculate(self):
        if not self.engine or self.engine._conn is None:
            QMessageBox.warning(self, "Recalculate", "The database is not available.")
            return
        confirm = QMessageBox.question(
            self,
            "Recalculate App Brain",
            "Rebuild all learned patterns and predictions from the stored readings?\n\n"
            "Automated clicking is suspended while this runs. Stored readings are "
            "never deleted or modified.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.recalc_btn.setEnabled(False)
        self.recalc_status_label.setText("Recalculating...")
        self.recalc_progress_bar.setValue(0)
        threading.Thread(target=self._recalculate_worker, daemon=True).start()

    def _recalculate_worker(self):
        def report(frac: float, message: str) -> None:
            self.recalc_progress.emit(float(frac), str(message))

        try:
            result = self.engine.recalculate(progress=report)
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": str(e)}
        self.recalc_finished.emit(dict(result or {}))

    @Slot(float, str)
    def _on_recalc_progress(self, frac: float, message: str):
        self.recalc_progress_bar.setValue(int(max(0.0, min(1.0, frac)) * 100))
        self.recalc_status_label.setText(message)

    @Slot(dict)
    def _on_recalc_finished(self, result: dict):
        self.recalc_btn.setEnabled(True)
        self.recalc_progress_bar.setValue(100 if result.get("ok") else 0)
        if result.get("ok"):
            self.recalc_status_label.setText(
                f"Complete - model v{result.get('model_version')}, "
                f"{result.get('patterns', 0)} patterns, "
                f"{result.get('predictions', 0)} predictions")
        else:
            self.recalc_status_label.setText(
                f"Failed: {result.get('error', 'unknown error')}")
            QMessageBox.critical(
                self, "Recalculation Failed",
                f"Recalculation failed:\n{result.get('error', 'unknown error')}\n\n"
                "The previous model version was preserved.")
        self.refresh()
        self.recalc_finished_ui_refresh()

    def recalc_finished_ui_refresh(self):
        """Hook for the main window to refresh the dashboard after a rebuild."""
        return


class AutomationWidget(QWidget):
    """Automated-click configuration. The 1,000-record gate is not configurable."""

    settings_changed = Signal(dict)

    def __init__(self, engine: MonitoringEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._group_ids: List[str] = []
        self._setup_ui()
        self._load_settings()
        self.refresh_status()

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start(3000)

    # ------------------------------------------------------------------
    def _setup_ui(self):
        layout = _scrolled_page(self)
        layout.setSpacing(18)
        layout.setContentsMargins(20, 20, 20, 20)
        page_header(
            layout, "Automation",
            "Decide when the app is allowed to click. Every click is gated by "
            "1,000 valid Region A readings and stays in dry-run until you "
            "switch it on.")

        # Top row: gate status beside the master switches.
        top_row = QHBoxLayout()
        top_row.setSpacing(16)

        gate_group = QGroupBox("Automation Gate")
        gate_layout = QFormLayout(gate_group)
        gate_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.gate_label = QLabel("0 / 1,000 valid Region A readings")
        self.gate_label.setStyleSheet("font-weight: bold;")
        self.eligibility_label = QLabel("-")
        self.last_event_label = QLabel("No click event recorded yet")
        self.last_event_label.setWordWrap(True)
        add_help_row(gate_layout, "Qualifying Region A records", self.gate_label,
                     "Region A readings that count towards the mandatory gate. "
                     "The gate is fixed at 1,000 and cannot be changed.")
        add_help_row(gate_layout, "Current click eligibility", self.eligibility_label,
                     "Whether clicking may happen right now, and if not, why.")
        add_help_row(gate_layout, "Last click / block event", self.last_event_label,
                     "The most recent click attempt and its outcome or block reason.")

        gate_note = QLabel(
            "The 1,000-record Region A gate is mandatory and non-configurable. "
            "It is enforced inside the automation execution path, not in this window."
        )
        gate_note.setWordWrap(True)
        gate_note.setStyleSheet("color: #c62828;")
        gate_layout.addRow("", gate_note)
        top_row.addWidget(gate_group, 3)

        switch_group = QGroupBox("Master Switches")
        switch_layout = QVBoxLayout(switch_group)
        self.enable_check = QCheckBox("Enable automated clicking")
        self.dry_run_check = QCheckBox(
            "Dry run (evaluate conditions and log, but never move the mouse)")
        switch_layout.addWidget(self.enable_check)
        switch_layout.addWidget(self.dry_run_check)
        switch_layout.addStretch(1)
        top_row.addWidget(switch_group, 2)
        layout.addLayout(top_row)

        # Middle row: conditions beside the clickable categories.
        middle_row = QHBoxLayout()
        middle_row.setSpacing(16)

        conditions_group = QGroupBox("Click Conditions")
        conditions_layout = QFormLayout(conditions_group)
        conditions_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.confidence_spin = QDoubleSpinBox()
        self.confidence_spin.setRange(0.0, 100.0)
        self.confidence_spin.setSingleStep(5.0)
        add_help_row(conditions_layout, "Confidence threshold (strict >)",
                     self.confidence_spin,
                     "A prediction must exceed this OCR confidence to be clicked.")

        self.min_evidence_spin = QSpinBox()
        self.min_evidence_spin.setRange(0, 100000)
        add_help_row(conditions_layout, "Minimum resolved predictions",
                     self.min_evidence_spin,
                     "How many predictions must be resolved before clicking starts.")

        self.recent_safeguard_check = QCheckBox("Enable recent-accuracy safeguard")
        conditions_layout.addRow("", self.recent_safeguard_check)

        self.recent_window_spin = QSpinBox()
        self.recent_window_spin.setRange(1, 10000)
        add_help_row(conditions_layout, "Recent window (predictions)",
                     self.recent_window_spin,
                     "How many recent predictions the safeguard looks back over.")

        self.recent_min_accuracy_spin = QDoubleSpinBox()
        self.recent_min_accuracy_spin.setRange(0.0, 100.0)
        self.recent_min_accuracy_spin.setSingleStep(5.0)
        add_help_row(conditions_layout, "Minimum recent accuracy (%)",
                     self.recent_min_accuracy_spin,
                     "Clicking pauses while recent accuracy stays below this.")

        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 3600)
        self.cooldown_spin.setSuffix(" s")
        add_help_row(conditions_layout, "Cooldown between clicks",
                     self.cooldown_spin,
                     "Minimum quiet time between two consecutive clicks.")
        middle_row.addWidget(conditions_group, 1)

        categories_group = QGroupBox("Allowed Categories (groups eligible to be clicked)")
        categories_layout = QVBoxLayout(categories_group)
        categories_hint = help_text(
            "Tick every category that may be clicked. Unticked categories are "
            "never clicked even when a prediction points at them.")
        categories_layout.addWidget(categories_hint)
        self.categories_list = QListWidget()
        self.categories_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.categories_list.setMinimumHeight(230)
        categories_layout.addWidget(self.categories_list)
        middle_row.addWidget(categories_group, 1)
        layout.addLayout(middle_row)

        # --- multi-point click sequence -----------------------------------
        target_group = QGroupBox("Click Target Sequence")
        target_layout = QVBoxLayout(target_group)
        target_hint = help_text(
            "Set as many positions as you need. When automation runs, every "
            "position is clicked in order, waiting the configured gap between "
            "clicks. Positions are physical pixels on the target monitor.")
        target_layout.addWidget(target_hint)

        points_row = QHBoxLayout()
        points_row.setSpacing(8)

        self.targets_table = PlaceholderTable(
            0, 3,
            "No click points yet - use Pick on Screen or Use Cursor Position "
            "to define where automation clicks.")
        self.targets_table.setHorizontalHeaderLabels(
            ["#", "X (physical px)", "Y (physical px)"])
        configure_fit_table(self.targets_table, min_height=160)
        self.targets_table.horizontalHeader().setStretchLastSection(False)
        points_row.addWidget(self.targets_table, 2)

        point_buttons = QVBoxLayout()
        point_buttons.setSpacing(6)
        self.select_target_btn = make_button("Pick on Screen", "primary",
                                             "Click once anywhere to add a point.")
        self.select_target_btn.clicked.connect(self._show_target_selector)
        point_buttons.addWidget(self.select_target_btn)

        self.cursor_btn = make_button("Use Cursor Position", "accent",
                                      "Add the point currently under the mouse.")
        self.cursor_btn.clicked.connect(self._use_cursor_position)
        point_buttons.addWidget(self.cursor_btn)

        self.add_target_btn = make_button("Add Point", "quiet",
                                          "Append a new point with typed coordinates.")
        self.add_target_btn.clicked.connect(self._add_point_row)
        point_buttons.addWidget(self.add_target_btn)

        self.remove_target_btn = make_button("Remove Selected", "danger",
                                             "Delete the selected points from the sequence.")
        self.remove_target_btn.clicked.connect(self._remove_selected_points)
        point_buttons.addWidget(self.remove_target_btn)

        self.clear_targets_btn = make_button("Clear All", "danger",
                                             "Remove every point in the sequence.")
        self.clear_targets_btn.clicked.connect(self._clear_points)
        point_buttons.addWidget(self.clear_targets_btn)
        point_buttons.addStretch(1)
        points_row.addLayout(point_buttons)
        target_layout.addLayout(points_row)

        delay_row = QHBoxLayout()
        delay_row.setSpacing(6)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60000)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setValue(200)
        delay_row.addWidget(QLabel("Gap between clicks"))
        delay_row.addWidget(help_badge(
            "How long to wait after each click before the next one in the "
            "sequence fires. Default is 200 ms."))
        delay_row.addWidget(self.delay_spin)
        delay_row.addStretch(1)
        target_layout.addLayout(delay_row)

        self.calibration_label = QLabel("Not calibrated")
        self.calibration_label.setWordWrap(True)
        target_layout.addWidget(self.calibration_label)
        layout.addWidget(target_group)

        # --- actions -------------------------------------------------------
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.save_btn = make_button("Save Automation Settings", "primary",
                                    "Persist every setting on this page.")
        self.save_btn.clicked.connect(self._save_settings)
        buttons.addWidget(self.save_btn)

        self.kill_btn = make_button("Kill Switch (halt automation now)", "danger",
                                    "Stop automated clicking immediately.")
        self.kill_btn.clicked.connect(self._kill_switch)
        buttons.addWidget(self.kill_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        layout.addStretch(1)

    # ------------------------------------------------------------------
    def _load_settings(self):
        if not self.engine or self.engine._conn is None:
            return
        conn = self.engine._conn

        self.enable_check.setChecked(
            config_repo.get_setting_bool(conn, "auto_click_enabled", False))
        self.dry_run_check.setChecked(
            config_repo.get_setting_bool(conn, "auto_click_dry_run", True))
        self.confidence_spin.setValue(
            config_repo.get_setting_float(conn, "confidence_threshold", 70.0))
        self.min_evidence_spin.setValue(
            config_repo.get_setting_int(conn, "min_evidence", 30))
        self.recent_safeguard_check.setChecked(
            config_repo.get_setting_bool(conn, "recent_safeguard_enabled", True))
        self.recent_window_spin.setValue(
            config_repo.get_setting_int(conn, "recent_window", 50))
        self.recent_min_accuracy_spin.setValue(
            config_repo.get_setting_float(conn, "recent_min_accuracy", 60.0))
        self.cooldown_spin.setValue(
            config_repo.get_setting_int(conn, "cooldown_seconds", 5))

        self._load_categories()
        self._load_target()

    def _load_categories(self):
        conn = self.engine._conn
        allowed = set(config_repo.get_allowed_categories(conn))
        groups = config_repo.load_groups(conn)
        self._group_ids = [g.group_id for g in groups]

        self.categories_list.clear()
        for g in groups:
            item = QListWidgetItem(
                f"{g.name} ({g.abbreviation}) - from {_format_bound(g.lower_bound)}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if g.group_id in allowed else Qt.Unchecked)
            item.setData(Qt.UserRole, g.group_id)
            self.categories_list.addItem(item)

    def _load_target(self):
        """Fill the point sequence from the stored click targets."""
        targets = config_repo.get_click_targets(self.engine._conn)
        self._set_points([(int(t["x"]), int(t["y"])) for t in targets
                          if "x" in t and "y" in t])
        self.delay_spin.setValue(
            config_repo.get_click_sequence_delay_ms(self.engine._conn))

        if targets:
            first = targets[0]
            monitor = first.get("monitor_id") or "unknown monitor"
            when = first.get("calibrated_at") or "unknown time"
            if len(targets) == 1:
                self.calibration_label.setText(
                    f"1 point calibrated at ({first.get('x')}, {first.get('y')}) "
                    f"on {monitor} at {when}")
            else:
                self.calibration_label.setText(
                    f"{len(targets)} points calibrated on {monitor} at {when}")
        else:
            self.calibration_label.setText(
                "Not calibrated - clicks are blocked until at least one point is set.")

    # --- point-sequence editing -----------------------------------------
    def _add_point_row(self, x: int = 0, y: int = 0) -> None:
        """Append an editable point row (defaults to 0,0)."""
        row = self.targets_table.rowCount()
        self.targets_table.insertRow(row)

        number_item = make_item(str(row + 1))
        number_item.setTextAlignment(Qt.AlignCenter)
        self.targets_table.setItem(row, 0, number_item)

        x_spin = QSpinBox()
        x_spin.setRange(0, 100000)
        x_spin.setValue(int(x))
        y_spin = QSpinBox()
        y_spin.setRange(0, 100000)
        y_spin.setValue(int(y))
        self.targets_table.setCellWidget(row, 1, x_spin)
        self.targets_table.setCellWidget(row, 2, y_spin)
        # Keep the visible sort keys in step with the spin values.
        x_spin.valueChanged.connect(
            lambda v, r=row: self._reindex_points())
        y_spin.valueChanged.connect(
            lambda v, r=row: self._reindex_points())

    def _reindex_points(self) -> None:
        """Renumber the '#' column after an add/remove/edit."""
        for row in range(self.targets_table.rowCount()):
            item = self.targets_table.item(row, 0)
            if item is not None:
                item.setText(str(row + 1))

    def _set_points(self, points) -> None:
        """Replace the whole sequence with ``points`` (iterable of (x, y))."""
        self.targets_table.setRowCount(0)
        for x, y in points:
            self._add_point_row(x, y)
        self._reindex_points()

    def _collect_points(self):
        """Read every point back out of the table as a list of dicts."""
        points: List[Dict] = []
        for row in range(self.targets_table.rowCount()):
            x_spin = self.targets_table.cellWidget(row, 1)
            y_spin = self.targets_table.cellWidget(row, 2)
            if x_spin is None or y_spin is None:
                continue
            points.append({"x": int(x_spin.value()), "y": int(y_spin.value())})
        return points

    def _remove_selected_points(self) -> None:
        rows = sorted({index.row() for index in
                       self.targets_table.selectionModel().selectedRows()}
                      if self.targets_table.selectionModel() else set(),
                      reverse=True)
        for row in rows:
            self.targets_table.removeRow(row)
        self._reindex_points()

    def _clear_points(self) -> None:
        self.targets_table.setRowCount(0)
        self._reindex_points()

    # ------------------------------------------------------------------
    def _use_cursor_position(self):
        """Append the current mouse position as a new sequence point."""
        pos = QCursor.pos()
        self._append_logical_point(pos.x(), pos.y())

    def _show_target_selector(self):
        """Show the point selector overlay to pick a target coordinate."""
        self._selector = PointSelectorOverlay()
        self._selector.point_selected.connect(self._on_target_selected)
        self._selector.show()
        self._selector.activateWindow()

    def _on_target_selected(self, x: int, y: int):
        self._append_logical_point(x, y)

    def _append_logical_point(self, logical_x: int, logical_y: int):
        """Convert a Qt-logical pick to physical pixels and append it."""
        # Coordinates must be physical pixels for the automation clicker.
        px, py, _, _ = logical_rect_to_physical(logical_x, logical_y, 1, 1)
        self._add_point_row(px, py)
        self.targets_table.scrollToBottom()

        sig = capture_display_signature(px, py)
        mon = sig.get("monitor_id", "?") if sig else "?"
        total = self.targets_table.rowCount()
        self.calibration_label.setText(
            f"{total} point{'s' if total != 1 else ''} set; latest ({px}, {py}) "
            f"on {mon} at {datetime.now().isoformat()}")

    def _set_target_coordinates(self, logical_x: int, logical_y: int):
        """Backward-compatible single-point setter used by older callers/tests."""
        px, py, _, _ = logical_rect_to_physical(logical_x, logical_y, 1, 1)
        self._set_points([(px, py)])
        sig = capture_display_signature(px, py)
        mon = sig.get("monitor_id", "?") if sig else "?"
        now = datetime.now().isoformat()
        self.calibration_label.setText(f"Calibrated at ({px}, {py}) on {mon} at {now}")

    def _save_settings(self):
        if not self.engine or self.engine._conn is None:
            return
        conn = self.engine._conn

        config_repo.set_setting(
            conn, "auto_click_enabled", "1" if self.enable_check.isChecked() else "0")
        config_repo.set_setting(
            conn, "auto_click_dry_run", "1" if self.dry_run_check.isChecked() else "0")
        config_repo.set_setting(
            conn, "confidence_threshold", str(float(self.confidence_spin.value())))
        config_repo.set_setting(
            conn, "min_evidence", str(int(self.min_evidence_spin.value())))
        config_repo.set_setting(
            conn, "recent_safeguard_enabled",
            "1" if self.recent_safeguard_check.isChecked() else "0")
        config_repo.set_setting(
            conn, "recent_window", str(int(self.recent_window_spin.value())))
        config_repo.set_setting(
            conn, "recent_min_accuracy", str(float(self.recent_min_accuracy_spin.value())))
        config_repo.set_setting(
            conn, "cooldown_seconds", str(int(self.cooldown_spin.value())))

        selected = []
        for i in range(self.categories_list.count()):
            item = self.categories_list.item(i)
            if item.checkState() == Qt.Checked:
                selected.append(item.data(Qt.UserRole))
        config_repo.set_allowed_categories(conn, selected)

        from src.automation.clicker import calibration_payload
        payloads = [
            calibration_payload(p["x"], p["y"]) for p in self._collect_points()
        ]
        config_repo.set_click_targets(conn, payloads)
        config_repo.set_click_sequence_delay_ms(conn, int(self.delay_spin.value()))
        conn.commit()

        self.engine.reload()
        self._load_target()
        self.settings_changed.emit({"automation": "saved"})
        QMessageBox.information(
            self, "Automation Settings Saved",
            "Automation settings saved. The 1,000-record Region A gate remains "
            "mandatory and cannot be changed here.")

    def _kill_switch(self):
        if not self.engine:
            return
        self.engine.kill_switch()
        self.enable_check.setChecked(False)
        self.refresh_status()
        QMessageBox.warning(
            self, "Kill Switch Engaged",
            "Automated clicking has been halted immediately.\n\n"
            "The kill switch stays engaged until the application is restarted.")

    # ------------------------------------------------------------------
    def refresh_status(self):
        if not self.engine:
            return
        try:
            gate = self.engine.gate_status()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error reading gate status: {e}")
            return

        count = int(gate.get("region_a_count", 0))
        required = int(gate.get("required", MANDATORY_REGION_A_RECORDS))
        self.gate_label.setText(f"{count:,} / {required:,} valid Region A readings")
        self.gate_label.setStyleSheet(
            "font-weight: bold; color: #2e7d32;" if gate.get("unlocked")
            else "font-weight: bold; color: #c62828;")

        if gate.get("killed"):
            eligibility = "Blocked - kill switch is engaged"
        elif not gate.get("unlocked"):
            eligibility = (
                f"Blocked - {max(0, required - count):,} more valid Region A "
                "readings required")
        elif not gate.get("auto_click_enabled"):
            eligibility = "Blocked - automated clicking is switched off"
        elif gate.get("rebuilding"):
            eligibility = "Blocked - recalculation in progress"
        elif gate.get("model_stale"):
            eligibility = "Blocked - model is stale, recalculate the app brain"
        elif gate.get("dry_run"):
            eligibility = "Eligible - dry run (conditions are logged, not clicked)"
        else:
            eligibility = "Eligible - live clicking is active"
        self.eligibility_label.setText(eligibility)

        audit = self.engine.click_audit(limit=1)
        if audit:
            row = audit[0]
            code = row.get("block_code")
            detail = row.get("detail") or ""
            reason = explain(code) if code else ""
            self.last_event_label.setText(
                f"{row.get('created_at', '')} - {row.get('outcome', '')}"
                f"{' (' + code + ')' if code else ''}\n"
                f"{reason}{' - ' + detail if detail else ''}"
            )
        else:
            self.last_event_label.setText("No click event recorded yet")


class ActivityWidget(QWidget):
    """Live diagnostics, click audit trail and session counters."""

    def __init__(self, engine: MonitoringEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._setup_ui()
        self.refresh()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(3000)

    # ------------------------------------------------------------------
    def _setup_ui(self):
        layout = _scrolled_page(self)
        layout.setSpacing(18)
        layout.setContentsMargins(20, 20, 20, 20)
        page_header(
            layout, "Activity",
            "Session counters, the click audit trail and every rejected "
            "reading or error, newest first.")

        header = QHBoxLayout()
        header.setSpacing(8)
        self.summary_label = QLabel("0 readings - 0 regions")
        self.summary_label.setStyleSheet("font-size: 11pt; font-weight: bold;")
        header.addWidget(self.summary_label, 1)
        self.refresh_btn = make_button("Refresh", "accent",
                                       "Re-read counters and tables now.")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        counters_group = QGroupBox("Reading Outcomes (this session)")
        counters_layout = QFormLayout(counters_group)
        counters_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.counter_labels = {}
        for key, caption, explanation in (
            ("scans_attempted", "Scans attempted",
             "How many capture cycles the engine started."),
            ("scans_completed", "Scans completed",
             "How many capture cycles finished (frame captured and processed)."),
            ("valid_readings", "Valid readings saved",
             "Readings that passed every check and were stored."),
            ("duplicates", "Duplicates suppressed",
             "Readings identical to the previous one were not stored again."),
            ("low_quality", "Low quality rejected",
             "Frames too blurry or too small to trust were rejected."),
            ("invalid_text", "Invalid text rejected",
             "OCR text that could not be parsed as a number was rejected."),
            ("out_of_range", "Out of range rejected",
             "Numbers outside every configured range were rejected."),
            ("ambiguous", "Ambiguous rejected",
             "Frames where several readings competed were rejected."),
            ("empty", "Empty frames",
             "Frames where no readable content was found."),
            ("ocr_failures", "OCR failures",
             "Times the OCR engine itself failed on a frame."),
            ("predictions", "Predictions produced",
             "How many next-category predictions the model made."),
            ("clicks_dispatched", "Clicks dispatched",
             "How many real (or dry-run) clicks the automation issued."),
            ("clicks_blocked", "Clicks blocked",
             "How many click attempts were stopped by a safety condition."),
            ("operational_errors", "Operational errors",
             "Unexpected engine errors; each one is logged in diagnostics."),
        ):
            value_label = QLabel("0")
            self.counter_labels[key] = value_label
            add_help_row(counters_layout, caption, value_label, explanation)
        layout.addWidget(counters_group)

        audit_group = QGroupBox("Click Audit Trail")
        audit_layout = QVBoxLayout(audit_group)
        audit_hint = help_text(
            "Every click attempt in order, newest first, with the reason it "
            "was allowed or blocked.")
        audit_layout.addWidget(audit_hint)
        self.audit_table = PlaceholderTable(
            0, 7,
            "No click attempts yet - every allowed or blocked click will be "
            "listed here with its reason.")
        self.audit_table.setHorizontalHeaderLabels(
            ["Timestamp", "Outcome", "Block Code", "Reason",
             "Target", "Dry Run", "Region A Count"])
        configure_fit_table(self.audit_table, min_height=240)
        fit_columns(self.audit_table)
        audit_layout.addWidget(self.audit_table)
        layout.addWidget(audit_group, 1)

        diagnostics_group = QGroupBox("Diagnostics (rejected readings and errors)")
        diagnostics_layout = QVBoxLayout(diagnostics_group)
        diag_hint = help_text(
            "Every rejected reading and operational error, newest first.")
        diagnostics_layout.addWidget(diag_hint)
        self.diagnostics_table = PlaceholderTable(
            0, 5,
            "Nothing rejected and no operational errors - every reading so "
            "far has been accepted.")
        self.diagnostics_table.setHorizontalHeaderLabels(
            ["Timestamp", "Category", "Detail", "Raw OCR Text", "Region"])
        configure_fit_table(self.diagnostics_table, min_height=240)
        fit_columns(self.diagnostics_table)
        diagnostics_layout.addWidget(self.diagnostics_table)
        layout.addWidget(diagnostics_group, 1)

    # ------------------------------------------------------------------
    def refresh(self):
        if not self.engine:
            return
        try:
            counters = self.engine.counters()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error reading counters: {e}")
            return

        for key, label in self.counter_labels.items():
            label.setText(str(counters.get(key, 0)))

        self.summary_label.setText(
            f"{self.engine.total_readings():,} valid Region A records - "
            f"{self.engine.region_count()} active region(s)")

        self._fill_audit()
        self._fill_diagnostics()

    def _fill_audit(self):
        rows = self.engine.click_audit(limit=200)
        self.audit_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            code = r.get("block_code") or ""
            reason = BLOCK_EXPLANATIONS.get(code, "") if code else (r.get("detail") or "")
            target = "-"
            if r.get("target_x") is not None and r.get("target_y") is not None:
                target = f"({r.get('target_x')}, {r.get('target_y')})"
            values = [
                str(r.get("created_at") or "").replace("T", " ")[:19],
                str(r.get("outcome") or ""),
                code or "-",
                reason,
                target,
                "yes" if r.get("dry_run") else "no",
                str(r.get("region_a_count", 0)),
            ]
            for col, value in enumerate(values):
                self.audit_table.setItem(i, col, QTableWidgetItem(value))

    def _fill_diagnostics(self):
        rows = self.engine.recent_diagnostics(limit=200)
        self.diagnostics_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            values = [
                str(r.get("created_at") or "").replace("T", " ")[:19],
                str(r.get("category") or ""),
                str(r.get("detail") or ""),
                str(r.get("raw_text") or ""),
                str(r.get("region_id") or ""),
            ]
            for col, value in enumerate(values):
                self.diagnostics_table.setItem(i, col, QTableWidgetItem(value))


class MainWindow(QMainWindow):
    """Main application window for Screen Monitor."""

    def __init__(self):
        super().__init__()
        self.engine: Optional[MonitoringEngine] = None
        self.region_manager: Optional[RegionManager] = None
        self.tray_icon = None

        # Give every page consistent, comfortably sized controls.
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(APP_STYLESHEET)

        self._initialize_engine()
        self._setup_ui()
        self._setup_tray()

    def _initialize_engine(self):
        try:
            self.engine = create_engine()
            if not self.engine.initialise():
                logger.warning("Monitoring engine initialisation had warnings")
            self.region_manager = create_region_manager()
            self.region_manager.initialise()
            # The engine owns the region state; make sure it has loaded the
            # persisted two-slot configuration before the UI reads it.
            if self.engine is not None:
                self.engine._load_regions()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Engine initialisation exception: {e}")

    def _create_app_icon(self) -> QIcon:
        """Create a simple icon for the app/tray."""
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(56, 189, 233))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, 64, 64)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawRoundedRect(12, 16, 40, 28, 4, 4)
        painter.setBrush(QColor(56, 189, 233))
        painter.drawRect(28, 46, 8, 2)
        painter.drawRoundedRect(24, 48, 16, 2, 1, 1)
        painter.end()
        return QIcon(pixmap)

    def _setup_tray(self):
        """Keep monitoring alive when the window is minimized or closed."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("System tray not available - monitoring will stop on close")
            return

        self.tray_icon = QSystemTrayIcon(self._create_app_icon(), self)
        self.tray_icon.setToolTip("Screen Monitor - Running")

        tray_menu = QMenu(self)
        show_action = QAction("Show Window", self)
        show_action.triggered.connect(self._show_window)
        tray_menu.addAction(show_action)

        hide_action = QAction("Minimize to Tray", self)
        hide_action.triggered.connect(self.hide)
        tray_menu.addAction(hide_action)

        tray_menu.addSeparator()

        if self.engine:
            if self.engine._is_running:
                stop_action = QAction("Stop Monitoring", self)
                stop_action.triggered.connect(self._tray_stop_monitoring)
                tray_menu.addAction(stop_action)
            else:
                start_action = QAction("Start Monitoring", self)
                start_action.triggered.connect(self._tray_start_monitoring)
                tray_menu.addAction(start_action)
            tray_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()

    def _show_window(self):
        self.show()
        self.activateWindow()
        self.raise_()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _tray_start_monitoring(self):
        if self.engine:
            self.engine.start_monitoring()
            self._setup_tray()

    def _tray_stop_monitoring(self):
        if self.engine:
            self.engine.stop_monitoring()
            self._setup_tray()

    def _quit_app(self):
        if self.engine and self.engine._is_running:
            self.engine.stop_monitoring()
        if self.tray_icon:
            self.tray_icon.hide()
        QApplication.quit()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _setup_ui(self):
        self.setWindowTitle("Screen Monitor - Number Detection")
        self.setWindowIcon(self._create_app_icon())
        self.resize(1200, 820)
        self.setMinimumSize(900, 640)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setFixedWidth(210)
        sidebar.setStyleSheet(
            """
            QWidget {
                background-color: #1e293b;
                color: #e2e8f0;
            }
            QPushButton {
                background-color: transparent;
                color: #cbd5e1;
                border: none;
                /* Reserve the accent bar in every state so switching pages
                   never nudges the label sideways. */
                border-left: 4px solid transparent;
                text-align: left;
                padding: 12px 18px;
                font-size: 14px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2b3a4f;
                color: #ffffff;
            }
            QPushButton:checked {
                background-color: #0f172a;
                color: #38bdf8;
                font-weight: bold;
                border-left: 4px solid #38bdf8;
            }
            QPushButton:checked:hover {
                background-color: #0f172a;
                color: #7dd3fc;
            }
        """
        )

        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 16, 0, 16)
        sidebar_layout.setSpacing(4)

        app_title = QLabel("SCREEN MONITOR")
        app_title.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #38bdf8; padding: 12px 18px;")
        sidebar_layout.addWidget(app_title)

        self.btn_dashboard = QPushButton("Dashboard")
        self.btn_regions = QPushButton("Regions")
        self.btn_history = QPushButton("History")
        self.btn_learning = QPushButton("Learning")
        self.btn_automation = QPushButton("Automation")
        self.btn_activity = QPushButton("Activity")
        self.btn_settings = QPushButton("Settings")

        self._nav_buttons = [
            self.btn_dashboard, self.btn_regions, self.btn_history,
            self.btn_learning, self.btn_automation, self.btn_activity,
            self.btn_settings,
        ]
        for button in self._nav_buttons:
            button.setCheckable(True)
            sidebar_layout.addWidget(button)
        self.btn_dashboard.setChecked(True)

        sidebar_layout.addStretch(1)

        v_label = QLabel(" v2.0.0 (Local OCR)")
        v_label.setStyleSheet("color: #64748b; font-size: 11px; padding: 12px 18px;")
        sidebar_layout.addWidget(v_label)

        main_layout.addWidget(sidebar)

        # --- pages --------------------------------------------------------
        self.stack = QStackedWidget()
        # NB: the selector must stay typed.  A bare "background-color" rule has
        # no selector, so Qt applies it to this widget *and every descendant* -
        # including QPushButton, where it overrides each role's background and
        # leaves the role's white text on a white button (invisible buttons).
        self.stack.setStyleSheet(
            "QStackedWidget { background-color: #ffffff; }")

        self.dashboard_page = DashboardWidget(self.engine)
        self.regions_page = RegionsWidget(self.engine, self.region_manager)
        self.history_page = HistoryWidget(self.engine)
        self.learning_page = LearningWidget(self.engine)
        self.automation_page = AutomationWidget(self.engine)
        self.activity_page = ActivityWidget(self.engine)
        self.settings_page = SettingsWidget(self.engine)

        self.pages = [
            self.dashboard_page, self.regions_page, self.history_page,
            self.learning_page, self.automation_page, self.activity_page,
            self.settings_page,
        ]
        for page in self.pages:
            self.stack.addWidget(page)

        main_layout.addWidget(self.stack, 1)

        for index, button in enumerate(self._nav_buttons):
            button.clicked.connect(lambda _=False, i=index: self._switch_page(i))

        # --- inter-widget refresh -----------------------------------------
        self.regions_page.region_changed.connect(self._refresh_all)
        self.settings_page.settings_changed.connect(self._refresh_all)
        self.automation_page.settings_changed.connect(self._refresh_all)
        self.learning_page.recalc_finished.connect(self._on_recalc_finished)

    def _refresh_all(self, *_args):
        self.dashboard_page._refresh_stats()
        self.learning_page.refresh()
        self.automation_page.refresh_status()
        self.automation_page._load_categories()
        self.activity_page.refresh()

    @Slot(dict)
    def _on_recalc_finished(self, _result: dict):
        self.dashboard_page._refresh_stats()
        self.activity_page.refresh()

    def _switch_page(self, index: int):
        index = max(0, min(int(index), len(self.pages) - 1))
        self.stack.setCurrentIndex(index)
        for i, button in enumerate(self._nav_buttons):
            button.setChecked(i == index)

        page = self.pages[index]
        if page is self.history_page:
            page.reload()
        elif page is self.learning_page:
            page.refresh()
        elif page is self.automation_page:
            page.refresh_status()
        elif page is self.activity_page:
            page.refresh()
        elif page is self.dashboard_page:
            page._refresh_stats()

    def closeEvent(self, event):
        # Minimize to tray instead of quitting - monitoring continues
        if self.tray_icon and self.tray_icon.isVisible():
            self.hide()
            if not hasattr(self, '_tray_shown'):
                self.tray_icon.showMessage(
                    "Screen Monitor",
                    "Application minimized to system tray. Monitoring continues.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000
                )
                self._tray_shown = True
            event.ignore()
            return
        # Fallback if tray not available
        if self.engine and self.engine._is_running:
            self.engine.stop_monitoring()
        if self.tray_icon:
            self.tray_icon.hide()
        super().closeEvent(event)

def main():
    """Entry point for the Screen Monitor UI."""
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("Screen Monitor")
    app.setApplicationVersion("2.0.0")
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
