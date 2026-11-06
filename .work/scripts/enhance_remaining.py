from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def edit(rel,old,new,count=1):
 p=ROOT/rel; s=p.read_text(encoding='utf-8')
 if old not in s: raise RuntimeError(f'anchor not found {rel}: {old[:70]!r}')
 p.write_text(s.replace(old,new,count),encoding='utf-8')

edit('src/ui/main_window.py', 'page_header(layout, "Input Amount", "Calibrate the screen positions for digits 0–9, Clear, and Done. Amount actions click these positions to enter the amount.")', 'page_header(layout, "Input Amount", "Calibrate screen positions for digits 0–9, decimal point, Clear, and Done.")')
edit('src/automation/clicker.py', '            last_b = int(round(float(row["numeric_value"]) * 0.10))\n', '            from decimal import Decimal\n            last_b = Decimal(str(row["numeric_value"])) * Decimal("0.10")\n')
edit('src/automation/clicker.py', '''                amount = last_b if step.get("mode") == "auto" else max(0.0, float(step.get("value", 0)))
                amount_text = format(amount, ".12g")
                events.append(keypad["clear"])
''','''                from decimal import Decimal
                amount = last_b if step.get("mode") == "auto" else Decimal(str(max(0.0, float(step.get("value", 0)))))
                amount_text = format(amount, "f")
                if "." in amount_text:
                    amount_text = amount_text.rstrip("0").rstrip(".")
                events.append(keypad["clear"])
''')
edit('src/automation/clicker.py', '''        # 5–8) Prediction-quality conditions can be ignored only for an
''','''        definition_id = prediction.get("definition_id")
        if definition_id is not None:
            from src.learning import repository as learn_repo
            definition = learn_repo.get_pattern_definition(conn, int(definition_id))
            if definition is None or not definition.enabled:
                return blocked("CATEGORY_NOT_ENABLED", "matched pattern is not active")

        # 5–8) Prediction-quality conditions can be ignored only for an
''')
edit('src/monitor/engine.py', '''        if new_prediction_id is None or result.prediction is None:
            return

        p = result.prediction
''','''        ignore_click_conditions = config_repo.get_setting_bool(
            self._conn, "ignore_click_conditions", False)
        if ignore_click_conditions:
            matched_ids = list(self._pending_range_patterns) + list(self._pending_movement_patterns)
            active_matches = [learn_repo.get_pattern_definition(self._conn, pid)
                              for pid in matched_ids]
            active_matches = [d for d in active_matches if d is not None and d.enabled]
            if active_matches:
                definition = active_matches[0]
                prediction = {
                    "prediction_id": None, "candidate_group": group_id,
                    "confidence": 0.0, "resolved_count": 0,
                    "correct_count": 0, "recent_accuracy": None,
                    "pattern_length": definition.length,
                    "pattern_sequence": definition.sequence_text,
                    "pattern_kind": definition.kind,
                    "definition_id": definition.pattern_id,
                    "next_movement": None, "next_movement_confidence": None,
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
edit('src/monitor/engine.py', 'decision: ClickDecision = self._auto_click.try_click(prediction)', 'decision: ClickDecision = self._auto_click.try_click(\n                prediction, ignore_prediction_conditions=ignore_click_conditions)')
