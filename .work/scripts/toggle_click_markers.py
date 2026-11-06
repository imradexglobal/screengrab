from pathlib import Path
p=Path(__file__).resolve().parents[2]/'src/ui/main_window.py'
s=p.read_text(encoding='utf-8')
old='''        self._points: List[tuple] = []   # list of (logical_x, logical_y)
        self._affinity_applied = False
'''
new='''        self._points: List[tuple] = []   # list of (logical_x, logical_y)
        self._markers_visible = True
        self._affinity_applied = False
'''
assert old in s; s=s.replace(old,new,1)
old='''        self._points = logical
        if logical:
            if not self.isVisible():
                self.show()
                self._apply_capture_exclusion()
            self.raise_()
        else:
            self.hide()
        self.update()

    def clear(self) -> None:
'''
new='''        self._points = logical
        if logical and self._markers_visible:
            if not self.isVisible():
                self.show()
                self._apply_capture_exclusion()
            self.raise_()
        else:
            self.hide()
        self.update()

    def set_markers_visible(self, visible: bool) -> None:
        """Show or hide all calibrated markers without discarding their points."""
        self._markers_visible = bool(visible)
        if self._markers_visible and self._points:
            if not self.isVisible():
                self.show()
                self._apply_capture_exclusion()
            self.raise_()
        else:
            self.hide()
        self.update()

    def clear(self) -> None:
'''
assert old in s; s=s.replace(old,new,1)
old='''        target_layout.addWidget(target_hint)

        points_row = QHBoxLayout()
'''
new='''        target_layout.addWidget(target_hint)
        self.show_click_positions_check = QCheckBox("Show all click positions on screen")
        self.show_click_positions_check.setChecked(True)
        self.show_click_positions_check.setToolTip(
            "Toggle the numbered on-screen markers for configured click locations. "
            "This does not change the saved click sequence.")
        self.show_click_positions_check.toggled.connect(self._set_click_positions_visible)
        target_layout.addWidget(self.show_click_positions_check)

        points_row = QHBoxLayout()
'''
assert old in s; s=s.replace(old,new,1)
old='''        self.cooldown_spin.setValue(
            config_repo.get_setting_int(conn, "cooldown_seconds", 5))

        self._load_categories()
'''
new='''        self.cooldown_spin.setValue(
            config_repo.get_setting_int(conn, "cooldown_seconds", 5))
        markers_visible = config_repo.get_setting_bool(
            conn, "click_positions_visible", True)
        self.show_click_positions_check.blockSignals(True)
        self.show_click_positions_check.setChecked(markers_visible)
        self.show_click_positions_check.blockSignals(False)
        self._click_spot_overlay.set_markers_visible(markers_visible)

        self._load_categories()
'''
assert old in s; s=s.replace(old,new,1)
old='''    def _load_categories(self):
'''
new='''    def _set_click_positions_visible(self, visible: bool):
        self._click_spot_overlay.set_markers_visible(visible)
        if self.engine and self.engine._conn is not None:
            config_repo.set_setting(
                self.engine._conn, "click_positions_visible", "1" if visible else "0")
            self.engine._conn.commit()

    def _load_categories(self):
'''
assert old in s; s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')
