from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def rep(rel, old, new, n=1):
 p=ROOT/rel; s=p.read_text(encoding='utf-8')
 if old not in s: raise RuntimeError(f'anchor missing {rel}: {old[:100]!r}')
 p.write_text(s.replace(old,new,n),encoding='utf-8')

ui='src/ui/main_window.py'
rep(ui, '''        token_range_layout.setSpacing(4)
        self.token_range_buttons = []
        token_range_layout.addStretch(1)
        self.token_range_container.setVisible(False)
''','''        token_range_layout.setSpacing(4)
        self.token_range_buttons = []
        self.token_range_container.setVisible(False)
''')
rep(ui, '''        try:
            groups = config_repo.load_groups(self.engine._conn)
            group_labels = _group_bound_labels(self.engine._conn)
        except Exception:  # noqa: BLE001
            return
        layout = self.token_range_container.layout()
        for g in groups:
            label = group_labels.get(g.group_id, g.name or g.group_id)
            btn = make_button(label, "quiet")
            btn.clicked.connect(
                lambda _checked=False, gid=g.group_id: self._add_token(gid))
            layout.addWidget(btn)
            self.token_range_buttons.append(btn)
        layout.addStretch(1)
''','''        try:
            groups = config_repo.load_groups(self.engine._conn)
            group_labels = _group_bound_labels(self.engine._conn)
        except Exception:  # noqa: BLE001
            return
        layout = self.token_range_container.layout()
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.token_range_buttons = []
        for g in groups:
            label = group_labels.get(g.group_id, g.name or g.group_id)
            btn = make_button(label, "quiet")
            btn.clicked.connect(
                lambda _checked=False, gid=g.group_id: self._add_token(gid))
            layout.addWidget(btn)
            self.token_range_buttons.append(btn)
        layout.addStretch(1)
''')

# Generic learner resolves the most-specific allowed wildcard definition matching an observed suffix.
patterns='src/learning/patterns.py'
rep(patterns, '''    def _sequence_text(self, groups: Iterable[str]) -> str:
        return ",".join(self._abbrev(g) for g in groups)

    def _breaks_sequence(self, now: Optional[str]) -> bool:
''','''    def _sequence_text(self, groups: Iterable[str]) -> str:
        return ",".join(self._abbrev(g) for g in groups)

    def _allowed_definition_for(self, sequence: str) -> Optional[int]:
        """Return the most-specific allowed definition matching a suffix.

        A question mark in a user-defined sequence matches exactly one observed
        token. Exact matches win over wildcard matches; among wildcard matches,
        the pattern with the most literal tokens wins deterministically.
        """
        if self.allowed_sequences is None:
            return None
        observed = sequence.split(",")
        matches = []
        for pattern, definition_id in self.allowed_sequences.items():
            tokens = pattern.split(",")
            if len(tokens) != len(observed):
                continue
            if all(expected == "?" or expected == actual
                   for expected, actual in zip(tokens, observed)):
                specificity = sum(token != "?" for token in tokens)
                matches.append((specificity, pattern, definition_id))
        if not matches:
            return None
        matches.sort(key=lambda item: (-item[0], item[1], item[2]))
        return matches[0][2]

    def _breaks_sequence(self, now: Optional[str]) -> bool:
''')
rep(patterns, '''        if self.allowed_sequences is not None:
            if sequence not in self.allowed_sequences:
                return None
            if definition_id is None:
                definition_id = self.allowed_sequences[sequence]
''','''        if self.allowed_sequences is not None:
            matched_definition = self._allowed_definition_for(sequence)
            if matched_definition is None:
                return None
            if definition_id is None:
                definition_id = matched_definition
''')
rep(patterns, '''                if (self.allowed_sequences is not None
                        and sequence_text not in self.allowed_sequences):
                    continue
''','''                if (self.allowed_sequences is not None
                        and self._allowed_definition_for(sequence_text) is None):
                    continue
''')
rep(patterns, '''            if (self.allowed_sequences is not None
                    and sequence_text not in self.allowed_sequences):
                continue
''','''            if (self.allowed_sequences is not None
                    and self._allowed_definition_for(sequence_text) is None):
                continue
''')
rep(patterns, '''            definition_id = best.definition_id
            if definition_id is None and self.allowed_sequences is not None:
                definition_id = self.allowed_sequences.get(sequence_text)
''','''            definition_id = best.definition_id
            if definition_id is None and self.allowed_sequences is not None:
                definition_id = self._allowed_definition_for(sequence_text)
''')

# Regression: wildcard matches any observed range at each ? and continues learning statistics.
tests='tests/test_manual_brain.py'
rep(tests, '''class TestMovementPattern:
''','''class TestWildcardRangePattern:
    def test_question_marks_match_any_range_and_learn_the_suffix(self):
        definition = PatternDefinition(9, KIND_RANGE,
                                      ("g1", "g2", "?", "?", "?"))
        brain = ManualBrain([definition])
        groups = ["g1", "g2", "g3", "g1", "g2", "g3"]
        values = [1.0, 2.0, 3.0, 1.0, 2.0, 3.0]
        _observe(brain, groups, values)

        assert 9 in brain._pending_range_patterns
        rows = brain.range_engine.stats.values()
        assert any(row.definition_id == 9 for row in rows)

    def test_question_mark_pattern_rejects_nonmatching_literal_prefix(self):
        definition = PatternDefinition(9, KIND_RANGE,
                                      ("g1", "g2", "?", "?", "?"))
        brain = ManualBrain([definition])
        _observe(brain, ["g2", "g2", "g3", "g1", "g2"],
                 [2.0, 2.0, 3.0, 1.0, 2.0])
        assert 9 not in brain._pending_range_patterns


class TestMovementPattern:
''')
