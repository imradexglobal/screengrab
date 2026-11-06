from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def edit(rel, old, new, count=1):
    p=ROOT/rel
    s=p.read_text(encoding='utf-8')
    if old not in s: raise RuntimeError(f'anchor not found in {rel}: {old[:90]!r}')
    p.write_text(s.replace(old,new,count),encoding='utf-8')

# Decimal fixed/automatic entry. Preserve fractional values in stored workflow steps.
edit('src/config/repository.py', '''            try:
                value = max(0, int(float(step.get("value", 0))))
            except (TypeError, ValueError):
                value = 0
''','''            try:
                value = max(0.0, float(step.get("value", 0)))
            except (TypeError, ValueError):
                value = 0.0
''')

# Include decimal key in the standard keypad requirements, and serialize decimal amounts.
edit('src/automation/clicker.py', 'required_keys = set("0123456789") | {"clear", "done"}', 'required_keys = set("0123456789") | {".", "clear", "done"}')
edit('src/automation/clicker.py', '''                amount = last_b if step.get("mode") == "auto" else max(0, int(step.get("value", 0)))
                events.append(keypad["clear"])
                events.extend(keypad[d] for d in str(amount))
''','''                amount = last_b if step.get("mode") == "auto" else max(0.0, float(step.get("value", 0)))
                amount_text = format(amount, ".12g")
                events.append(keypad["clear"])
                events.extend(keypad[d] for d in amount_text)
''')

# Pattern-stat bypass in the automated clicker. Core automation safety gates remain.
edit('src/automation/clicker.py', 'def try_click(self, prediction: Dict, dry_run: Optional[bool] = None) -> ClickDecision:', 'def try_click(self, prediction: Dict, dry_run: Optional[bool] = None,\n                  ignore_prediction_conditions: bool = False) -> ClickDecision:')
edit('src/automation/clicker.py', '''        # 5) Predicted category selected
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
''','''        # 5–8) Prediction-quality conditions can be ignored only for an
        # active user-defined pattern match. Master enable, stale-model state,
        # the hard record gate and dispatch safeguards remain enforced.
        if ignore_prediction_conditions:
            definition_id = prediction.get("definition_id")
            if definition_id is None:
                return blocked("CATEGORY_NOT_ENABLED", "no active pattern definition matched")
            from src.learning import repository as learn_repo
            definition = learn_repo.get_pattern_definition(conn, int(definition_id))
            if definition is None or not definition.enabled:
                return blocked("CATEGORY_NOT_ENABLED", "matched pattern is not active")
        else:
            allowed = set(get_allowed_categories(conn))
            candidate = prediction.get("candidate_group")
            if candidate not in allowed:
                return blocked("CATEGORY_NOT_ENABLED", f"category '{candidate}' is not enabled")

            threshold = get_setting_float(conn, "confidence_threshold", 70.0)
            confidence = float(prediction.get("confidence") or 0.0)
            if not confidence > threshold:
                return blocked("CONFIDENCE_TOO_LOW",
                               f"confidence {confidence:.2f}% is not > {threshold:.2f}%")

            min_evidence = get_setting_int(conn, "min_evidence", 30)
            resolved_count = int(prediction.get("resolved_count") or 0)
            if resolved_count < min_evidence:
                return blocked("INSUFFICIENT_EVIDENCE",
                               f"{resolved_count} resolved predictions < {min_evidence} required")

            if get_setting_bool(conn, "recent_safeguard_enabled", True):
                recent = prediction.get("recent_accuracy")
                recent_min = get_setting_float(conn, "recent_min_accuracy", 60.0)
                if recent is None or float(recent) < recent_min:
                    return blocked(
                        "RECENT_ACCURACY_TOO_LOW",
                        f"recent accuracy {recent if recent is not None else 'n/a'} < {recent_min:.2f}%",
                    )
''')

# Add ignore-statistics toggle to Automation conditions and persist.
edit('src/ui/main_window.py', '''        add_help_row(conditions_layout, "Minimum resolved predictions",
                     self.min_evidence_spin,
                     "How many predictions must be resolved before clicking starts.")

        self.recent_safeguard_check = QCheckBox("Enable recent-accuracy safeguard")
''','''        add_help_row(conditions_layout, "Minimum resolved predictions",
                     self.min_evidence_spin,
                     "How many predictions must be resolved before clicking starts.")

        self.ignore_click_conditions_check = QCheckBox(
            "Trigger on active pattern match (ignore prediction statistics)")
        self.ignore_click_conditions_check.setToolTip(
            "When enabled, any active pattern match may trigger the sequence, "
            "even before it has outcome statistics. Master automation safety gates still apply.")
        conditions_layout.addRow("", self.ignore_click_conditions_check)

        self.recent_safeguard_check = QCheckBox("Enable recent-accuracy safeguard")
''')
edit('src/ui/main_window.py', '''        self.min_evidence_spin.setValue(
            config_repo.get_setting_int(conn, "min_evidence", 30))
        self.recent_safeguard_check.setChecked(
''','''        self.min_evidence_spin.setValue(
            config_repo.get_setting_int(conn, "min_evidence", 30))
        self.ignore_click_conditions_check.setChecked(
            config_repo.get_setting_bool(conn, "ignore_click_conditions", False))
        self.recent_safeguard_check.setChecked(
''')
edit('src/ui/main_window.py', '''        config_repo.set_setting(
            conn, "min_evidence", str(int(self.min_evidence_spin.value())))
''','''        config_repo.set_setting(
            conn, "min_evidence", str(int(self.min_evidence_spin.value())))
        config_repo.set_setting(
            conn, "ignore_click_conditions",
            "1" if self.ignore_click_conditions_check.isChecked() else "0")
''')

# Replace click-target coordinate spinner table with read-only selected point labels.
edit('src/ui/main_window.py', '''        self.targets_table = PlaceholderTable(
            0, 3,
            "No click points yet - use Pick on Screen or Use Cursor Position "
            "to define where automation clicks.")
        self.targets_table.setHorizontalHeaderLabels(
            ["#", "X (physical px)", "Y (physical px)"])
''','''        self.targets_table = PlaceholderTable(
            0, 2,
            "No click points yet - use Pick on Screen or Use Cursor Position "
            "to define where automation clicks.")
        self.targets_table.setHorizontalHeaderLabels(["#", "Selected screen point (physical px)"])
''')
edit('src/ui/main_window.py', '''        self.add_target_btn = make_button("Add Point", "quiet",
                                          "Append a new point with typed coordinates.")
        self.add_target_btn.clicked.connect(self._add_point_row)
        point_buttons.addWidget(self.add_target_btn)

''','')
edit('src/ui/main_window.py', '''        row = self.targets_table.rowCount()
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
''','''        row = self.targets_table.rowCount()
        self.targets_table.insertRow(row)
        number_item = make_item(str(row + 1))
        number_item.setTextAlignment(Qt.AlignCenter)
        self.targets_table.setItem(row, 0, number_item)
        point_item = make_item(f"({int(x)}, {int(y)})")
        point_item.setData(Qt.UserRole, (int(x), int(y)))
        self.targets_table.setItem(row, 1, point_item)
''')
edit('src/ui/main_window.py', '''        points: List[Dict] = []
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

# Editable list of amount steps; fixed values retain fractional digits.
edit('src/ui/main_window.py', '''        self.amount_value_spin = QSpinBox()
        self.amount_value_spin.setRange(0, 999999999)
''','''        self.amount_value_spin = QDoubleSpinBox()
        self.amount_value_spin.setRange(0.0, 999999999.0)
        self.amount_value_spin.setDecimals(6)
        self.amount_value_spin.setSingleStep(0.01)
''')
edit('src/ui/main_window.py', '''        self.amount_clear_btn = make_button("Clear Amount Steps", "quiet")
        self.amount_clear_btn.clicked.connect(self._clear_amount_steps)
''','''        self.amount_remove_btn = make_button("Remove Selected", "danger")
        self.amount_remove_btn.clicked.connect(self._remove_selected_amount_step)
''')
edit('src/ui/main_window.py', '''        amount_row.addWidget(self.amount_add_btn)
        amount_row.addWidget(self.amount_clear_btn)
        target_layout.addLayout(amount_row)
        self.amount_steps_label = QLabel("No amount steps")
        self.amount_steps_label.setWordWrap(True)
        target_layout.addWidget(self.amount_steps_label)
        self._amount_steps = []
''','''        amount_row.addWidget(self.amount_add_btn)
        amount_row.addWidget(self.amount_remove_btn)
        target_layout.addLayout(amount_row)
        self.amount_steps_list = QListWidget()
        self.amount_steps_list.setMinimumHeight(90)
        self.amount_steps_list.currentRowChanged.connect(self._select_amount_step)
        target_layout.addWidget(QLabel("Saved amount steps (select one to edit):"))
        target_layout.addWidget(self.amount_steps_list)
        self._amount_steps = []
''')
edit('src/ui/main_window.py', '''    def _refresh_amount_steps_label(self):
        self.amount_steps_label.setText("Amount steps: " + ("; ".join(
            f"after {int(a.get('after', 0))} click(s): {'auto (10% of latest Range B)' if a.get('mode') == 'auto' else 'fixed ' + str(a.get('value', 0))}"
            for a in self._amount_steps) or "none"))

    def _add_amount_step(self):
        self._amount_steps.append({"after": int(self.amount_position_spin.value()),
                                   "mode": "auto" if self.amount_mode_combo.currentIndex() == 0 else "fixed",
                                   "value": int(self.amount_value_spin.value())})
        self._amount_steps.sort(key=lambda a: int(a.get("after", 0)))
        self._refresh_amount_steps_label()

    def _clear_amount_steps(self):
        self._amount_steps = []
        self._refresh_amount_steps_label()
''','''    def _refresh_amount_steps_label(self):
        selected = self.amount_steps_list.currentRow()
        self.amount_steps_list.blockSignals(True)
        self.amount_steps_list.clear()
        for i, action in enumerate(self._amount_steps, 1):
            mode = "Auto (10% of latest Range B)" if action.get("mode") == "auto" else f"Fixed {action.get('value', 0)}"
            self.amount_steps_list.addItem(f"{i}. After {int(action.get('after', 0))} click(s) — {mode}")
        if self._amount_steps:
            self.amount_steps_list.setCurrentRow(min(max(selected, 0), len(self._amount_steps) - 1))
        self.amount_steps_list.blockSignals(False)

    def _select_amount_step(self, row):
        if row < 0 or row >= len(self._amount_steps):
            return
        action = self._amount_steps[row]
        self.amount_position_spin.setValue(int(action.get("after", 0)))
        self.amount_mode_combo.setCurrentIndex(0 if action.get("mode") == "auto" else 1)
        self.amount_value_spin.setValue(float(action.get("value", 0)))

    def _add_amount_step(self):
        row = self.amount_steps_list.currentRow()
        action = {"after": int(self.amount_position_spin.value()),
                  "mode": "auto" if self.amount_mode_combo.currentIndex() == 0 else "fixed",
                  "value": float(self.amount_value_spin.value())}
        if 0 <= row < len(self._amount_steps):
            self._amount_steps[row] = action
        else:
            self._amount_steps.append(action)
        self._amount_steps.sort(key=lambda a: int(a.get("after", 0)))
        self._refresh_amount_steps_label()

    def _remove_selected_amount_step(self):
        row = self.amount_steps_list.currentRow()
        if 0 <= row < len(self._amount_steps):
            del self._amount_steps[row]
            self._refresh_amount_steps_label()
''')

# Decimal coordinate key for the input amount page.
edit('src/ui/main_window.py', '"Calibrate the on-screen numeric keypad used by amount workflow steps.")', '"Calibrate the on-screen numeric keypad, including the decimal point, used by amount workflow steps.")')
edit('src/ui/main_window.py', 'keys = ["1","2","3","4","5","6","7","8","9","0","clear","done"]', 'keys = ["1","2","3","4","5","6","7","8","9","0",".","clear","done"]')
edit('src/ui/main_window.py', 'button = QPushButton(key.title() if key in ("clear", "done") else key)', 'button = QPushButton({"clear": "Clear", "done": "Done", ".": ". (decimal)"}.get(key, key))')

# Add new ignore-statistics active-pattern trigger from raw active-definition suffix matches.
edit('src/monitor/engine.py', '''        if new_prediction_id is None or result.prediction is None:
            return

        p = result.prediction
''','''        ignore_click_conditions = config_repo.get_setting_bool(
            self._conn, "ignore_click_conditions", False)
        if ignore_click_conditions:
            # Match suffixes only against the learner's enabled definitions.
            # This also creates a trigger before a forecast has any outcome stats.
            matched_ids = list(self._pending_range_patterns) + list(self._pending_movement_patterns)
            active_matches = [learn_repo.get_pattern_definition(self._conn, pid)
                              for pid in matched_ids]
            active_matches = [d for d in active_matches if d is not None and d.enabled]
            if active_matches:
                definition = active_matches[0]
                prediction = {
                    "prediction_id": None,
                    "candidate_group": group_id,
                    "confidence": 0.0,
                    "resolved_count": 0,
                    "correct_count": 0,
                    "recent_accuracy": None,
                    "pattern_length": definition.length,
                    "pattern_sequence": definition.sequence_text,
                    "pattern_kind": definition.kind,
                    "definition_id": definition.pattern_id,
                    "next_movement": None,
                    "next_movement_confidence": None,
                    "active_pattern_match": True,
                }
                self.prediction_made.emit(prediction)
                decision = self._auto_click.try_click(
                    prediction, ignore_prediction_conditions=True) if self._auto_click else None
                if decision is not None:
                    if decision.allowed:
                        self._counters["clicks_dispatched"] += 1
                    else:
                        self._counters["clicks_blocked"] += 1
                    self.click_event.emit({
                        "allowed": decision.allowed, "outcome": decision.outcome,
                        "code": decision.code, "message": decision.message,
                        "region_a_count": decision.region_a_count,
                        "prediction": prediction,
                    })
                return

        if new_prediction_id is None or result.prediction is None:
            return

        p = result.prediction
''')
# The option also bypasses statistical checks for normal forecasts when their pattern is active.
edit('src/monitor/engine.py', 'decision: ClickDecision = self._auto_click.try_click(prediction)', 'decision: ClickDecision = self._auto_click.try_click(\n                prediction, ignore_prediction_conditions=ignore_click_conditions)')
