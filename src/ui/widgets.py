"""Reusable presentation helpers shared by every Screen Monitor page.

The helpers here keep the pages consistent:

* :func:`set_button_role` gives a ``QPushButton`` a real button look with
  hover, pressed and *visibly disabled* states (the role drives the
  application-wide stylesheet through a dynamic property).
* :func:`help_badge` / :func:`help_row` add the small ``?`` badge whose tooltip
  explains a label when it is hovered.
* :func:`page_header` gives every page the same title + purpose line.
* :func:`configure_fit_table` / :func:`fit_columns` build tables whose columns
  share the available width and follow window resizes automatically.
* :class:`PlaceholderTable` adds a friendly empty state to a table with no rows.
* :class:`NumericTableWidgetItem` makes numeric columns sort numerically
  instead of lexically when a table is made sortable.
* :data:`GROUP_PALETTE` maps a numerical group to its dedicated colour so a
  value can be shown on its range's colour.
"""

from typing import Dict, Iterable, List, Optional, Sequence

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

#: Recognised button roles.  Each one is styled in ``APP_STYLESHEET`` through
#: the ``role`` dynamic property, including the ``:disabled`` variant so an
#: unavailable action always *looks* unavailable.
BUTTON_ROLES = ("primary", "success", "warning", "danger", "accent", "quiet")

#: One dedicated colour per numerical group ordinal (the nine defaults).
#: Pastel backgrounds with dark text keep every cell readable and keep the
#: "colour belongs to the range" mapping stable across the whole UI.
GROUP_PALETTE: List[str] = [
    "#b2dfdb",  # g1 Nano
    "#b3e5fc",  # g2 Micro
    "#c5cae9",  # g3 Mini
    "#d1c4e9",  # g4 Small
    "#f0f4c3",  # g5 Medium
    "#fff59d",  # g6 Large
    "#ffe0b2",  # g7 Mega
    "#ffccbc",  # g8 Super
    "#f8bbd0",  # g9 Ultra
]

#: Text colour used on every palette background (all are pastel).
PALETTE_TEXT = "#0f172a"

#: Colour for an empty / unknown live cell.
EMPTY_CELL_COLOUR = "#e2e8f0"


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

def set_button_role(button: QPushButton, role: str) -> QPushButton:
    """Tag ``button`` with a stylesheet role and repaint it immediately."""
    if role not in BUTTON_ROLES:
        role = "quiet"
    button.setProperty("role", role)
    # Re-polish so the dynamic property is picked up even after first show.
    style = button.style()
    style.unpolish(button)
    style.polish(button)
    return button


def make_button(text: str, role: str = "quiet", tooltip: str = "") -> QPushButton:
    """Create a button that already looks and feels like a button."""
    button = QPushButton(text)
    set_button_role(button, role)
    if tooltip:
        button.setToolTip(tooltip)
        button.setAccessibleDescription(tooltip)
    button.setCursor(Qt.PointingHandCursor)
    button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    return button


# ---------------------------------------------------------------------------
# Help badges
# ---------------------------------------------------------------------------

def help_badge(text: str) -> QLabel:
    """Small ``?`` badge whose tooltip describes the control next to it.

    The badge stays visually quiet (plain muted text) until it is hovered,
    so a form that explains many fields does not turn into a wall of circles.
    """
    badge = QLabel("?")
    badge.setToolTip(text)
    badge.setAccessibleName(text)
    badge.setAlignment(Qt.AlignCenter)
    badge.setCursor(Qt.WhatsThisCursor)
    badge.setFixedSize(16, 16)
    badge.setStyleSheet(
        "QLabel {"
        "  background-color: transparent;"
        "  color: #94a3b8;"
        "  border: none;"
        "  border-radius: 8px;"
        "  font-weight: bold;"
        "  font-size: 10px;"
        "  padding: 0px;"
        "}"
        "QLabel:hover { background-color: #e0f2fe; color: #0369a1; }"
    )
    return badge


def help_text(text: str) -> QLabel:
    """A muted one-line explanation rendered under a control."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #64748b; font-size: 9pt;")
    return label


def page_header(layout: QVBoxLayout, title: str, subtitle: str = "") -> None:
    """Prepend a page heading (and optional one-line purpose) to ``layout``.

    Every page opens the same way - a bold title with a short sentence that
    says what the page is for - so the user always knows where they are even
    before reading the group boxes below.
    """
    heading = QLabel(title)
    font = QFont()
    font.setPointSize(15)
    font.setBold(True)
    heading.setFont(font)
    heading.setStyleSheet("color: #0f172a; padding: 0px;")
    layout.insertWidget(0, heading)

    if subtitle:
        caption = QLabel(subtitle)
        caption.setWordWrap(True)
        caption.setStyleSheet("color: #64748b; font-size: 9.5pt; padding: 0px;")
        layout.insertWidget(1, caption)


def _label_cell(caption: str, help_text_content: str = "") -> QWidget:
    """Build the ``caption + ? badge`` cell used as a form's label column.

    The cell carries its own internal layout (so QFormLayout treats it as one
    label widget) which keeps every field in a form aligned in a single column
    while the ``?`` badge always sits directly after its own caption.
    """
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    label = QLabel(caption)
    row.addWidget(label)
    if help_text_content:
        row.addWidget(help_badge(help_text_content))
    row.addStretch(1)
    holder.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    return holder


def labelled_row(caption: str, widget: QWidget,
                 help_text_content: str = "") -> QHBoxLayout:
    """Build a plain ``(label + ? badge) | control`` horizontal row.

    Prefer :func:`add_help_row` inside a ``QFormLayout``: it uses
    :func:`_label_cell` so the fields of every row line up in one column.
    """
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(_label_cell(caption, help_text_content), 1)
    row.addWidget(widget, 1)
    return row


def add_help_row(form: QFormLayout, caption: str, widget: QWidget,
                 help_text_content: str = "") -> None:
    """Insert a form row whose label carries a hovering ``?`` explanation.

    The label is added as a real label widget, so QFormLayout sizes the label
    column from the longest caption and every field in the form starts at the
    same horizontal position - no matter how long the individual captions are.
    """
    form.addRow(_label_cell(caption, help_text_content), widget)


def add_help_label(form: QFormLayout, caption: str, value: QWidget,
                   help_text_content: str = "") -> None:
    """Same as :func:`add_help_row` for read-only value widgets (labels)."""
    add_help_row(form, caption, value, help_text_content)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def configure_fit_table(table: QTableWidget, min_height: int = 240,
                        stretch: bool = True,
                        edit_triggers: Optional[int] = None) -> None:
    """Modern, window-adaptive table baseline.

    Columns share the available width (``stretch``), rows alternate in colour,
    the grid is replaced by clean separators and only rows are selectable.
    """
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setSortIndicatorShown(False)
    header.setMinimumSectionSize(64)
    header.setSectionResizeMode(
        QHeaderView.Stretch if stretch else QHeaderView.Interactive)

    table.setWordWrap(False)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setFrameShape(QTableWidget.NoFrame)
    table.setSortingEnabled(False)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection)
    table.setEditTriggers(
        QAbstractItemView.NoEditTriggers if edit_triggers is None
        else edit_triggers)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(26)
    table.setMinimumHeight(min_height)
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)


def fit_columns(table: QTableWidget) -> None:
    """Re-fit column widths to their content, then keep them stretching.

    Measuring first and only then switching to ``Stretch`` makes every column
    keep a width proportional to its longest cell while the total still fills
    the visible width, so the table follows window resizes exactly.
    """
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeToContents)
    table.resizeColumnsToContents()
    # From here on the header keeps every section proportional to the size it
    # just measured, and re-scales them automatically whenever the view is
    # resized, so the table always fills exactly the visible width.
    header.setSectionResizeMode(QHeaderView.Stretch)


def sort_safely(table: QTableWidget, fill) -> None:
    """Run ``fill`` with sorting suspended so a refill keeps the user's order."""
    was_enabled = table.isSortingEnabled()
    table.setSortingEnabled(False)
    try:
        fill()
    finally:
        table.setSortingEnabled(was_enabled)


class PlaceholderTable(QTableWidget):
    """Table that shows a friendly empty-state message while it has no rows.

    A freshly built page is full of blank tables; without a hint the user has
    no idea whether the app is broken, still loading or simply has nothing to
    show yet.  This subclass keeps a centred label over the viewport that
    appears whenever ``rowCount() == 0`` and disappears as soon as data is
    written - every fill path already goes through ``setRowCount`` /
    ``insertRow``, so no call site has to manage the visibility itself.
    """

    def __init__(self, rows: int, columns: int, placeholder: str = "",
                 parent=None):
        super().__init__(rows, columns, parent)
        self._placeholder_label = QLabel(placeholder, self.viewport())
        self._placeholder_label.setAlignment(Qt.AlignCenter)
        self._placeholder_label.setWordWrap(True)
        self._placeholder_label.setStyleSheet(
            "color: #94a3b8; font-size: 10pt; background-color: transparent;")
        self._placeholder_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._placeholder_label.hide()
        self.viewport().installEventFilter(self)
        self._placeholder_label.setText(placeholder)
        self._sync_placeholder()

    # -- placeholder text ------------------------------------------------
    def set_placeholder_text(self, text: str) -> None:
        """Replace the message shown while the table is empty."""
        self._placeholder_label.setText(text)

    # -- geometry --------------------------------------------------------
    def eventFilter(self, watched, event):  # noqa: N802 - Qt protocol
        if watched is self.viewport() and event.type() in (
                QEvent.Resize, QEvent.Show):
            self._placeholder_label.setGeometry(self.viewport().rect())
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):  # noqa: N802 - Qt protocol
        super().resizeEvent(event)
        self._placeholder_label.setGeometry(self.viewport().rect())

    # -- empty-state visibility -----------------------------------------
    def setRowCount(self, count: int) -> None:  # noqa: N802 - Qt protocol
        super().setRowCount(count)
        self._sync_placeholder()

    def insertRow(self, row: int) -> None:  # noqa: N802 - Qt protocol
        super().insertRow(row)
        self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        show = self.rowCount() == 0 and bool(
            self._placeholder_label.text().strip())
        self._placeholder_label.setVisible(show)
        if show:
            self._placeholder_label.raise_()
            self._placeholder_label.setGeometry(self.viewport().rect())


class NumericTableWidgetItem(QTableWidgetItem):
    """Table item that sorts numerically when both cells are numbers.

    Plain ``QTableWidgetItem`` compares display strings, so ``10.00`` would
    sort before ``2.00``.  This subclass parses both sides as floats first and
    falls back to a case-insensitive string comparison otherwise, which gives
    every column a correct A-Z / Z-A toggle.
    """

    def __lt__(self, other) -> bool:  # noqa: D105 - Qt protocol
        if other is None:
            return False
        left = (self.data(Qt.UserRole + 1) if self.data(Qt.UserRole + 1) is not None
                else self.text())
        right = (other.data(Qt.UserRole + 1) if other.data(Qt.UserRole + 1) is not None
                 else other.text())
        left_num = _as_float(left)
        right_num = _as_float(right)
        if left_num is not None and right_num is not None:
            return left_num < right_num
        if left_num is not None:
            return True
        if right_num is not None:
            return False
        return str(left).lower() < str(right).lower()


def _as_float(value) -> Optional[float]:
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def numeric_item(text: str, sort_value=None) -> NumericTableWidgetItem:
    """Create an item whose sort key can differ from its displayed text."""
    item = NumericTableWidgetItem(str(text))
    if sort_value is not None:
        item.setData(Qt.UserRole + 1, sort_value)
    return item


def make_item(text: str, sort_value=None) -> NumericTableWidgetItem:
    """Create a sortable item that shows ``text`` but sorts on ``sort_value``."""
    return numeric_item(str(text), text if sort_value is None else sort_value)


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

def group_colour_map(groups: Sequence) -> Dict[str, str]:
    """Map ``group_id`` -> dedicated colour, ordered by the group's ordinal."""
    ordered = sorted(groups, key=lambda g: getattr(g, "ordinal", 0))
    colours: Dict[str, str] = {}
    for index, group in enumerate(ordered):
        colour = GROUP_PALETTE[index % len(GROUP_PALETTE)]
        colours[group.group_id] = colour
    return colours


def readable_text(colour: str) -> str:
    """Return a dark or light foreground colour for ``colour``'s background."""
    c = QColor(colour)
    if not c.isValid():
        return PALETTE_TEXT
    # Relative luminance (sRGB) decides the contrast.
    luminance = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
    return "#ffffff" if luminance < 0.55 else PALETTE_TEXT


def bold_font(size_pt: Optional[float] = None, bold: bool = True) -> QFont:
    font = QFont()
    font.setBold(bold)
    if size_pt is not None:
        font.setPointSizeF(size_pt)
    return font
