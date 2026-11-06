from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def replace(rel, old, new, count=1):
    p=ROOT/rel; s=p.read_text(encoding='utf-8')
    if old not in s: raise RuntimeError(f'anchor missing in {rel}: {old[:90]!r}')
    p.write_text(s.replace(old,new,count),encoding='utf-8')

ui='src/ui/main_window.py'

# Add bypass toggle to Click Conditions.
replace(ui, '''        add_help_row(conditions_layout, "Minimum resolved predictions",
                     self.min_evidence_spin,
                     "How many predictions must be resolved before clicking starts.")

        self.recent_safeguard_check = QCheckBox("Enable recent-accuracy safeguard")
''','''        add_help_row(conditions_layout, "Minimum resolved predictions",
                     self.min_evidence_spin,
                     "How many predictions must be resolved before clicking starts.")

        self.ignore_click_conditions_check = QCheckBox(
            "Trigger on active pattern match (ignore prediction statistics)")
        self.ignore_click_conditions_check.setToolTip(
            "A match from an active pattern can trigger without outcome statistics. "
            "The master enable, mandatory Region A record gate, and dispatch safeguards remain active.")
        conditions_layout.addRow("", self.ignore_click_conditions_check)

        self.recent_safeguard_check = QCheckBox("Enable recent-accuracy safeguard")
''')

# Click-point table only displays the picked location; typed coordinate spinners are removed.
replace(ui, '''        self.targets_table = PlaceholderTable(
            0, 3,
            "No click points yet - use Pick on Screen or Use Cursor Position "
            "to define where automation clicks.")
        self.targets_table.setHorizontalHeaderLabels(
            ["#", "X (physical px)", "Y (physical px)"])
''','''        self.targets_table = PlaceholderTable(
            0, 2,
            "No click points yet - use Pick on Screen or Use Cursor Position "
            "to define where automation clicks.")
        self.targets_table.setHorizontalHeaderLabels(
            ["#", "Selected screen point (physical px)"])
''')
replace(ui, '''        self.add_target_btn = make_button("Add Point", "quiet",
                                          "Append a new point with typed coordinates.")
        self.add_target_btn.clicked.connect(self._add_point_row)
        point_buttons.addWidget(self.add_target_btn)

''','')

# Ordered amount steps editor.
replace(ui, '''        target_layout.addLayout(delay_row)

        self.calibration_label = QLabel("Not calibrated")
''','''        target_layout.addLayout(delay_row)

        amount_row = QHBoxLayout()
        self.amount_position_spin = QSpinBox()
        self.amount_position_spin.setRange(0, 100)
        self.amount_position_spin.setToolTip(
            "Insert after this many click-location steps (0 = before the first click).")
        self.amount_mode_combo = QComboBox()
        self.amount_mode_combo.addItems(["Auto (10% of latest Range B)", "Fixed value"])
        self.amount_value_spin = QDoubleSpinBox()
        self.amount_value_spin.setRange(0.0, 999999999.0)
        self.amount_value_spin.setDecimals(6)
        self.amount_value_spin.setSingleStep(0.01)
        self.amount_value_spin.setEnabled(False)
        self.amount_mode_combo.currentIndexChanged.connect(
            lambda i: self.amount_value_spin.setEnabled(i == 1))
        self.amount_new_btn = make_button("New Amount Step", "quiet")
        self.amount_new_btn.clicked.connect(self._new_amount_step)
        self.amount_add_btn = make_button("Add / Update Selected", "accent")
        self.amount_add_btn.clicked.connect(self._add_amount_step)
        self.amount_remove_btn = make_button("Remove Selected", "danger")
        self.amount_remove_btn.clicked.connect(self._remove_selected_amount_step)
        amount_row.addWidget(QLabel("Insert after click locations:"))
        amount_row.addWidget(self.amount_position_spin)
        amount_row.addWidget(self.amount_mode_combo)
        amount_row.addWidget(self.amount_value_spin)
        amount_row.addWidget(self.amount_new_btn)
        amount_row.addWidget(self.amount_add_btn)
        amount_row.addWidget(self.amount_remove_btn)
        target_layout.addLayout(amount_row)
        target_layout.addWidget(QLabel("Saved amount steps (select a row to edit):"))
        self.amount_steps_list = QListWidget()
        self.amount_steps_list.setMinimumHeight(90)
        self.amount_steps_list.currentRowChanged.connect(self._select_amount_step)
        target_layout.addWidget(self.amount_steps_list)
        self._amount_steps = []
        self._load_amount_steps()

        self.calibration_label = QLabel("Not calibrated")
''')

# Restore manual live test-sequence button.
replace(ui, '''        self.save_btn.clicked.connect(self._save_settings)
        buttons.addWidget(self.save_btn)

        self.kill_btn = make_button("Kill Switch (halt automation now)", "danger",
''','''        self.save_btn.clicked.connect(self._save_settings)
        buttons.addWidget(self.save_btn)

        self.test_sequence_btn = make_button(
            "Test Click Sequence Now (LIVE)", "accent",
            "Immediately run the saved workflow without waiting for a pattern or condition.")
        self.test_sequence_btn.clicked.connect(self._test_click_sequence)
        buttons.addWidget(self.test_sequence_btn)

        self.kill_btn = make_button("Kill Switch (halt automation now)", "danger",
''')

# Load settings and amount-step editor behavior.
replace(ui, '''        self.min_evidence_spin.setValue(
            config_repo.get_setting_int(conn, "min_evidence", 30))
        self.recent_safeguard_check.setChecked(
''','''        self.min_evidence_spin.setValue(
            config_repo.get_setting_int(conn, "min_evidence", 30))
        self.ignore_click_conditions_check.setChecked(
            config_repo.get_setting_bool(conn, "ignore_click_conditions", False))
        self.recent_safeguard_check.setChecked(
''')
replace(ui, '''        self._load_categories()
        self._load_target()

    def _load_categories(self):
''','''        self._load_categories()
        self._load_target()
        self._load_amount_steps()

    def _load_categories(self):
''')

# Point row and collection representation.
replace(ui, '''        number_item = make_item(str(row + 1))
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
''','''        number_item = make_item(str(row + 1))
        number_item.setTextAlignment(Qt.AlignCenter)
        self.targets_table.setItem(row, 0, number_item)
        point_item = make_item(f"({int(x)}, {int(y)})")
        point_item.setData(Qt.UserRole, (int(x), int(y)))
        self.targets_table.setItem(row, 1, point_item)
''')
replace(ui, '''        points: List[Dict] = []
        for row in range(self.targets_table.rowCount()):
            x_spin = self.targets_table.cellWidget(row, 1)
            y_spin = self.targets_table.cellWidget(row, 2)
            if x_spin is None or y_spin is None:
                continue
            points.append({"x": int(x_spin.value()), "y": int(y_spin.value())})
        return points
''','''        points: List[Dict] = []
        for row in range(self.targets_table.rowCount()):
            item = self.targets_table.item(row, 1)
            coords = item.data(Qt.UserRole) if item is not None else None
            if coords and len(coords) == 2:
                points.append({"x": int(coords[0]), "y": int(coords[1])})
        return points
''')

# Save bypass option and fully ordered workflow including amount actions.
replace(ui, '''        config_repo.set_setting(
            conn, "min_evidence", str(int(self.min_evidence_spin.value())))
        config_repo.set_setting(
            conn, "recent_safeguard_enabled",
''','''        config_repo.set_setting(
            conn, "min_evidence", str(int(self.min_evidence_spin.value())))
        config_repo.set_setting(
            conn, "ignore_click_conditions",
            "1" if self.ignore_click_conditions_check.isChecked() else "0")
        config_repo.set_setting(
            conn, "recent_safeguard_enabled",
''')
replace(ui, '''        config_repo.set_click_targets(conn, payloads)
        config_repo.set_click_sequence_delay_ms(conn, int(self.delay_spin.value()))
        conn.commit()
''','''        config_repo.set_click_targets(conn, payloads)
        workflow = []
        by_position = {}
        for amount in self._amount_steps:
            by_position.setdefault(max(0, int(amount.get("after", 0))), []).append(amount)
        for position in range(len(payloads) + 1):
            for amount in by_position.get(position, []):
                workflow.append({"type": "amount", "mode": amount.get("mode", "auto"),
                                 "value": amount.get("value", 0)})
            if position < len(payloads):
                workflow.append({"type": "click", "target": payloads[position]})
        config_repo.set_click_workflow(conn, workflow)
        import json
        config_repo.set_setting(conn, "click_amount_actions", json.dumps(self._amount_steps))
        config_repo.set_click_sequence_delay_ms(conn, int(self.delay_spin.value()))
        conn.commit()
''')

# Insert amount editor methods before _load_target.
replace(ui, '''    def _load_target(self):
        """Fill the point sequence from the stored click targets."""
''','''    def _load_amount_steps(self):
        import json
        try:
            raw = config_repo.get_setting(self.engine._conn, "click_amount_actions", "[]")
            data = json.loads(raw)
            self._amount_steps = data if isinstance(data, list) else []
        except Exception:
            self._amount_steps = []
        self._refresh_amount_steps_list()

    def _refresh_amount_steps_list(self):
        selected = self.amount_steps_list.currentRow()
        self.amount_steps_list.blockSignals(True)
        self.amount_steps_list.clear()
        for i, action in enumerate(self._amount_steps, 1):
            mode = ("Auto (10% of latest Range B)" if action.get("mode") == "auto"
                    else f"Fixed {action.get('value', 0)}")
            self.amount_steps_list.addItem(
                f"{i}. After {int(action.get('after', 0))} click(s) — {mode}")
        if self._amount_steps:
            self.amount_steps_list.setCurrentRow(
                min(max(selected, 0), len(self._amount_steps) - 1))
        self.amount_steps_list.blockSignals(False)

    def _select_amount_step(self, row):
        if row < 0 or row >= len(self._amount_steps):
            return
        action = self._amount_steps[row]
        self.amount_position_spin.setValue(int(action.get("after", 0)))
        self.amount_mode_combo.setCurrentIndex(0 if action.get("mode") == "auto" else 1)
        self.amount_value_spin.setValue(float(action.get("value", 0)))

    def _new_amount_step(self):
        self.amount_steps_list.setCurrentRow(-1)
        self.amount_position_spin.setValue(0)
        self.amount_mode_combo.setCurrentIndex(0)
        self.amount_value_spin.setValue(0.0)

    def _add_amount_step(self):
        row = self.amount_steps_list.currentRow()
        action = {"after": int(self.amount_position_spin.value()),
                  "mode": "auto" if self.amount_mode_combo.currentIndex() == 0 else "fixed",
                  "value": float(self.amount_value_spin.value())}
        if 0 <= row < len(self._amount_steps):
            self._amount_steps[row] = action
        else:
            self._amount_steps.append(action)
        self._amount_steps.sort(key=lambda item: int(item.get("after", 0)))
        self._refresh_amount_steps_list()

    def _remove_selected_amount_step(self):
        row = self.amount_steps_list.currentRow()
        if 0 <= row < len(self._amount_steps):
            del self._amount_steps[row]
            self._refresh_amount_steps_list()

    def _test_click_sequence(self):
        if not self.engine:
            return
        decision = self.engine.test_click_sequence()
        if decision is None:
            QMessageBox.warning(self, "Test Click Sequence", "Automation engine is not ready.")
        elif decision.outcome == "DISPATCHED":
            QMessageBox.information(self, "Test Sequence Result", decision.message)
        else:
            QMessageBox.warning(self, "Test Sequence Result", decision.message)
        self.refresh_status()

    def _load_target(self):
        """Fill the point sequence from the stored click targets."""
''')

# Restore Input Amount page widget.
replace(ui, '''class ActivityWidget(QWidget):
''','''class InputAmountWidget(QWidget):
    """Calibrate every numeric keypad position used by amount actions."""
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._selector = None
        layout = _scrolled_page(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        page_header(layout, "Input Amount",
                    "Calibrate screen positions for digits 0–9, decimal point, Clear, and Done.")
        self.key_buttons = {}
        grid = QGridLayout()
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", ".", "clear", "done"]
        for i, key in enumerate(keys):
            label = {"clear": "Clear", "done": "Done", ".": ". (decimal)"}.get(key, key)
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, k=key: self._pick(k))
            grid.addWidget(button, i // 3, i % 3)
            self.key_buttons[key] = button
        layout.addLayout(grid)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.save_btn = make_button("Save Keypad Positions", "primary")
        self.save_btn.clicked.connect(self._save)
        layout.addWidget(self.save_btn)
        self._load()

    def _load(self):
        self.positions = (config_repo.get_amount_keypad(self.engine._conn)
                          if self.engine and self.engine._conn else {})
        self._refresh()

    def _refresh(self):
        missing = [key for key in self.key_buttons if key not in self.positions]
        calibrated = ", ".join(
            f"{key}=({self.positions[key]['x']}, {self.positions[key]['y']})"
            for key in self.key_buttons if key in self.positions) or "none"
        self.status.setText("Calibrated: " + calibrated + "\\n" +
                            ("Still needed: " + ", ".join(missing) if missing else "Keypad ready."))

    def _pick(self, key):
        self._pending_key = key
        self._selector = PointSelectorOverlay()
        self._selector.point_selected.connect(self._selected)
        self._selector.show()
        self._selector.activateWindow()

    def _selected(self, x, y):
        px, py, _, _ = logical_rect_to_physical(x, y, 1, 1)
        from src.automation.clicker import calibration_payload
        self.positions[self._pending_key] = calibration_payload(px, py)
        self._refresh()

    def _save(self):
        if self.engine and self.engine._conn:
            config_repo.set_amount_keypad(self.engine._conn, self.positions)
            self.engine._conn.commit()
            self._refresh()
            QMessageBox.information(self, "Keypad Saved", "Amount keypad positions saved.")


class ActivityWidget(QWidget):
''')

# Restore navigation/page ordering: Automation, Input Amount, Activity, Settings.
replace(ui, '''        self.btn_automation = QPushButton("Automation")
        self.btn_activity = QPushButton("Activity")
''','''        self.btn_automation = QPushButton("Automation")
        self.btn_input_amount = QPushButton("Input Amount")
        self.btn_activity = QPushButton("Activity")
''')
replace(ui, '''            self.btn_learning, self.btn_automation, self.btn_activity,
            self.btn_settings,
''','''            self.btn_learning, self.btn_automation, self.btn_input_amount,
            self.btn_activity, self.btn_settings,
''')
replace(ui, '''        self.learning_page = LearningWidget(self.engine)
        self.automation_page = AutomationWidget(self.engine)
        self.activity_page = ActivityWidget(self.engine)
''','''        self.learning_page = LearningWidget(self.engine)
        self.automation_page = AutomationWidget(self.engine)
        self.input_amount_page = InputAmountWidget(self.engine)
        self.activity_page = ActivityWidget(self.engine)
''')
replace(ui, '''            self.learning_page, self.automation_page, self.activity_page,
            self.settings_page,
''','''            self.learning_page, self.automation_page, self.input_amount_page,
            self.activity_page, self.settings_page,
''')
