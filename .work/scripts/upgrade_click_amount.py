from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def replace(path, old, new, count=1):
    p = ROOT / path
    s = p.read_text(encoding='utf-8')
    if old not in s:
        raise RuntimeError(f'Anchor not found in {path}: {old[:100]!r}')
    s = s.replace(old, new, count)
    p.write_text(s, encoding='utf-8')

# Persist ordered click/amount actions and the keypad map as JSON settings.
replace('src/config/repository.py', 'def get_click_target(conn: sqlite3.Connection) -> Dict:\n', '''def get_click_workflow(conn: sqlite3.Connection) -> List[Dict]:
    """Load ordered click and amount actions; legacy targets become click steps."""
    import json
    raw = get_setting(conn, "click_workflow", "[]")
    try:
        rows = json.loads(raw)
        if isinstance(rows, list) and rows:
            return [x for x in rows if isinstance(x, dict) and x.get("type") in ("click", "amount")]
    except (TypeError, ValueError):
        pass
    return [{"type": "click", "target": t} for t in get_click_targets(conn)]


def set_click_workflow(conn: sqlite3.Connection, steps: List[Dict]) -> None:
    import json
    cleaned = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("type") == "click" and isinstance(step.get("target"), dict):
            t = step["target"]
            if "x" in t and "y" in t:
                cleaned.append({"type": "click", "target": {**t, "x": int(t["x"]), "y": int(t["y"])}})
        elif step.get("type") == "amount":
            mode = "auto" if step.get("mode") == "auto" else "fixed"
            try:
                value = max(0, int(float(step.get("value", 0))))
            except (TypeError, ValueError):
                value = 0
            cleaned.append({"type": "amount", "mode": mode, "value": value})
    set_setting(conn, "click_workflow", json.dumps(cleaned))


def get_amount_keypad(conn: sqlite3.Connection) -> Dict:
    import json
    try:
        data = json.loads(get_setting(conn, "amount_keypad", "{}"))
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def set_amount_keypad(conn: sqlite3.Connection, keys: Dict) -> None:
    import json
    set_setting(conn, "amount_keypad", json.dumps(keys))


def get_click_target(conn: sqlite3.Connection) -> Dict:
''')

# Clicker: perform typed amount action via calibrated keypad, using latest B value in auto mode.
replace('src/automation/clicker.py', '    get_click_targets,\n', '    get_click_targets,\n    get_click_workflow,\n    get_amount_keypad,\n')
replace('src/automation/clicker.py', '        targets = get_click_targets(conn)\n        if not targets:\n            return blocked("INVALID_TARGET_COORDINATES", "no calibrated target")\n', '''        workflow = get_click_workflow(conn)
        targets = [s["target"] for s in workflow if s.get("type") == "click"]
        if not targets:
            return blocked("INVALID_TARGET_COORDINATES", "no calibrated click target")
        keypad = get_amount_keypad(conn)
        amount_steps = [s for s in workflow if s.get("type") == "amount"]
        if amount_steps:
            required_keys = set("0123456789") | {"clear", "done"}
            if not required_keys.issubset(keypad):
                return blocked("INVALID_TARGET_COORDINATES", "amount keypad is not fully calibrated")
            targets += [keypad[k] for k in required_keys]
''')
replace('src/automation/clicker.py', '        point_summary = ", ".join(f"({int(t[\'x\'])}, {int(t[\'y\'])})"\n                                  for t in targets)\n        if dry_run:\n', '''        point_summary = ", ".join(f"({int(t['x'])}, {int(t['y'])})" for t in targets)
        if dry_run:
''')
replace('src/automation/clicker.py', '                f"dry run: would click {len(targets)} point(s): {point_summary}"\n', '                f"dry run: would run {len(workflow)} workflow step(s); points: {point_summary}"\n')
replace('src/automation/clicker.py', '            ok, dispatch_detail = self._dispatch_sequence(targets, delay_ms)\n', '''            ok, dispatch_detail = self._dispatch_workflow(workflow, keypad, prediction, delay_ms)
''')
replace('src/automation/clicker.py', '    def _dispatch_sequence(self, targets: List[Dict], delay_ms: int) -> tuple:\n', '''    def _dispatch_workflow(self, workflow: List[Dict], keypad: Dict,
                           prediction: Dict, delay_ms: int) -> tuple:
        """Execute click points and keypad-entry actions in their configured order."""
        if not workflow:
            return False, "empty workflow"
        last_b = None
        amount_steps = [s for s in workflow if s.get("type") == "amount"]
        if amount_steps and any(s.get("mode") == "auto" for s in amount_steps):
            from src.learning.repository import last_region_b_reading
            row = last_region_b_reading(self._conn)
            if row is None:
                return False, "automatic amount unavailable: no Region B reading"
            last_b = int(round(float(row["numeric_value"]) * 0.10))
        events = []
        for step in workflow:
            if step.get("type") == "click":
                events.append(step["target"])
            elif step.get("type") == "amount":
                amount = last_b if step.get("mode") == "auto" else max(0, int(step.get("value", 0)))
                events.append(keypad["clear"])
                events.extend(keypad[d] for d in str(amount))
                events.append(keypad["done"])
        if not events:
            return False, "workflow has no executable actions"
        return self._dispatch_sequence(events, delay_ms)

    def _dispatch_sequence(self, targets: List[Dict], delay_ms: int) -> tuple:
''')

# Add Input Amount widget with per-key screen calibration, and eighth page/navigation.
replace('src/ui/main_window.py', 'class MainWindow(QMainWindow):\n', '''class InputAmountWidget(QWidget):
    """Calibrate the on-screen numeric keypad used by amount workflow steps."""
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._selector = None
        layout = _scrolled_page(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        page_header(layout, "Input Amount", "Calibrate the screen positions for digits 0–9, Clear, and Done. Amount actions type digits by clicking these positions.")
        self.key_buttons = {}
        grid = QGridLayout()
        keys = ["1","2","3","4","5","6","7","8","9","0","clear","done"]
        for i, key in enumerate(keys):
            button = QPushButton(key.title() if key in ("clear", "done") else key)
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
        keys = config_repo.get_amount_keypad(self.engine._conn) if self.engine and self.engine._conn else {}
        self.positions = dict(keys)
        self._refresh()

    def _refresh(self):
        missing = [k for k in self.key_buttons if k not in self.positions]
        self.status.setText("Calibrated: " + (", ".join(f"{k}=({self.positions[k]['x']}, {self.positions[k]['y']})" for k in self.key_buttons if k in self.positions) or "none") + ("\nStill needed: " + ", ".join(missing) if missing else "\nKeypad ready."))

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


class MainWindow(QMainWindow):
''')
replace('src/ui/main_window.py', '        self.btn_settings = QPushButton("Settings")\n', '        self.btn_settings = QPushButton("Settings")\n        self.btn_input_amount = QPushButton("Input Amount")\n')
replace('src/ui/main_window.py', '            self.btn_learning, self.btn_automation, self.btn_activity,\n            self.btn_settings,\n', '            self.btn_learning, self.btn_automation, self.btn_input_amount, self.btn_activity,\n            self.btn_settings,\n')
replace('src/ui/main_window.py', '        self.automation_page = AutomationWidget(self.engine)\n', '        self.automation_page = AutomationWidget(self.engine)\n        self.input_amount_page = InputAmountWidget(self.engine)\n')
replace('src/ui/main_window.py', '            self.learning_page, self.automation_page, self.activity_page,\n            self.settings_page,\n', '            self.learning_page, self.automation_page, self.input_amount_page, self.activity_page,\n            self.settings_page,\n')

# Update the UI contract test for the new page.
replace('tests/test_ui.py', 'The window must expose exactly seven pages,', 'The window must expose exactly eight pages,')
replace('tests/test_ui.py', 'assert window.stack.count() == 7', 'assert window.stack.count() == 8')
replace('tests/test_ui.py', '    assert window.settings_page is not None\n', '    assert window.settings_page is not None\n    assert window.input_amount_page is not None\n')
replace('tests/test_ui.py', '    assert window.stack.widget(6) is window.settings_page\n', '    assert window.stack.widget(6) is window.input_amount_page\n    assert window.stack.widget(7) is window.settings_page\n')
