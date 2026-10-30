"""Tests for Screen Monitor GUI components.

The window must expose exactly seven pages, a two-slot region model, and the
mandatory "Recalculate App Brain" action.  No control may be decorative: each
assertion below corresponds to a real backend capability.
"""

import os

# Ensure offscreen mode for headless test runs
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from src.database.init_db import REGION_A_ID, REGION_B_ID
from src.ui.main_window import MainWindow, MANDATORY_REGION_A_RECORDS


@pytest.fixture(scope="session")
def qapp():
    """Create QApplication instance for tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_main_window_creation(qapp):
    """MainWindow instantiates with exactly seven pages."""
    window = MainWindow()
    assert window is not None
    assert window.windowTitle() == "Screen Monitor - Number Detection"
    assert window.stack.count() == 7
    assert window.engine is not None
    window.close()


def test_all_seven_pages_exist(qapp):
    window = MainWindow()
    assert window.dashboard_page is not None
    assert window.regions_page is not None
    assert window.history_page is not None
    assert window.learning_page is not None
    assert window.automation_page is not None
    assert window.activity_page is not None
    assert window.settings_page is not None

    # The stacked widget order must be stable: index -> page.
    assert window.stack.widget(0) is window.dashboard_page
    assert window.stack.widget(1) is window.regions_page
    assert window.stack.widget(2) is window.history_page
    assert window.stack.widget(3) is window.learning_page
    assert window.stack.widget(4) is window.automation_page
    assert window.stack.widget(5) is window.activity_page
    assert window.stack.widget(6) is window.settings_page
    window.close()


def test_navigation_switches_pages(qapp):
    window = MainWindow()
    for index, button in enumerate(window._nav_buttons):
        button.click()
        assert window.stack.currentIndex() == index
        assert button.isChecked() is True
    window.close()


def test_dashboard_widget(qapp):
    """Dashboard controls start in the correct state and expose the gate."""
    window = MainWindow()
    dashboard = window.dashboard_page
    assert dashboard.start_btn.isEnabled() is True
    assert dashboard.pause_btn.isEnabled() is False
    assert dashboard.stop_btn.isEnabled() is False
    assert "Idle" in dashboard.status_label.text()
    # The mandatory gate must be visible on the dashboard.
    assert "1,000" in dashboard.gate_label.text()
    window.close()


def test_settings_widget(qapp):
    """SettingsWidget loads current configuration and the nine groups."""
    window = MainWindow()
    settings = window.settings_page
    assert settings.interval_spin.value() >= 200
    assert settings.confidence_spin.value() > 0
    assert settings.save_mode_combo.count() == 2
    assert settings.groups_table.rowCount() == 9
    window.close()


def test_learning_widget_recalculate_button(qapp):
    """The recalculate action is present with the exact mandated label."""
    window = MainWindow()
    learning = window.learning_page
    assert learning.recalc_btn.text() == "Recalculate App Brain"
    assert learning.recalc_btn.isEnabled() is True
    assert learning.patterns_table.columnCount() == 8
    window.close()


def test_automation_widget_gate_is_not_configurable(qapp):
    """Automation exposes its controls but no way to change the 1,000 gate."""
    window = MainWindow()
    automation = window.automation_page
    assert automation.kill_btn is not None
    assert automation.enable_check is not None
    assert automation.dry_run_check is not None
    assert automation.confidence_spin is not None
    assert automation.min_evidence_spin is not None
    assert automation.recent_min_accuracy_spin is not None
    assert automation.cooldown_spin is not None
    assert automation.categories_list.count() == 9

    # The gate value must come from the backend, never from a local widget.
    assert f"{MANDATORY_REGION_A_RECORDS:,}" in automation.gate_label.text()
    window.close()


def test_activity_widget(qapp):
    window = MainWindow()
    activity = window.activity_page
    assert activity.audit_table.columnCount() == 7
    assert activity.diagnostics_table.columnCount() == 5
    assert "scans_attempted" in activity.counter_labels
    window.close()


def test_regions_page_uses_two_slots(qapp):
    """Region A is active, Region B is a reserved inactive slot."""
    window = MainWindow()
    regions = window.regions_page
    assert REGION_A_ID == "region-A"
    assert REGION_B_ID == "region-B"
    assert "Region A" in regions.region_a_group.title()
    assert "Region B" in regions.region_b_group.title()
    assert regions.select_a_btn is not None
    assert regions.select_b_btn is not None
    # Region B is never active.
    assert "Inactive" in regions.region_b_state.text()
    window.close()
