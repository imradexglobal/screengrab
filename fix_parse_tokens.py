with open('src/ui/main_window.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace the _parse_tokens method to handle "?" tokens
old_method = """    def _parse_tokens(self, kind: str) -> List[str]:
        text = self.tokens_edit.text().strip()
        if not text:
            raise ValueError("Enter at least two tokens separated by commas.")
        raw = [t for t in text.replace(",", " ").replace(";", " ").split() if t]

        if kind == "movement":
            tokens = [normalise_movement(t) for t in raw]
        else:
            conn = self.engine._conn if self.engine else None
            groups = config_repo.load_groups(conn) if conn is not None else []
            by_id = {g.group_id for g in groups}
            by_bound = {f"{float(g.lower_bound):.2f}": g.group_id
                        for g in groups}
            tokens = []
            for token in raw:
                if token in by_id:
                    tokens.append(token)
                    continue
                try:
                    value = float(token)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"'{token}' is not a configured range. Use one of: "
                        f"{self._range_options_text() or 'the group ids'}.")
                key = f"{value:.2f}"
                if key not in by_bound:
                    raise ValueError(
                        f"{key} is not the lower bound of any configured "
                        f"range. Available ranges: "
                        f"{self._range_options_text() or 'none'}.")
                tokens.append(by_bound[key])

        if not (MIN_PATTERN_LENGTH <= len(tokens) <= MAX_PATTERN_LENGTH):
            raise ValueError(
                f"A pattern needs between {MIN_PATTERN_LENGTH} and "
                f"{MAX_PATTERN_LENGTH} tokens.")
        return tokens"""

new_method = """    def _parse_tokens(self, kind: str) -> List[str]:
        text = self.tokens_edit.text().strip()
        if not text:
            raise ValueError("Enter at least two tokens separated by commas.")
        raw = [t for t in text.replace(",", " ").replace(";", " ").split() if t]

        if kind == "movement":
            tokens = []
            for t in raw:
                if t == "?":
                    tokens.append("?")
                else:
                    tokens.append(normalise_movement(t))
        else:
            conn = self.engine._conn if self.engine else None
            groups = config_repo.load_groups(conn) if conn is not None else []
            by_id = {g.group_id for g in groups}
            by_bound = {f"{float(g.lower_bound):.2f}": g.group_id
                        for g in groups}
            tokens = []
            for token in raw:
                if token == "?":
                    tokens.append("?")
                elif token in by_id:
                    tokens.append(token)
                else:
                    try:
                        value = float(token)
                    except (TypeError, ValueError):
                        raise ValueError(
                            f"'{token}' is not a configured range. Use one of: "
                            f"{self._range_options_text() or 'the group ids'}.")
                    key = f"{value:.2f}"
                    if key not in by_bound:
                        raise ValueError(
                            f"{key} is not the lower bound of any configured "
                            f"range. Available ranges: "
                            f"{self._range_options_text() or 'none'}.")
                    tokens.append(by_bound[key])

        if not (MIN_PATTERN_LENGTH <= len(tokens) <= MAX_PATTERN_LENGTH):
            raise ValueError(
                f"A pattern needs between {MIN_PATTERN_LENGTH} and "
                f"{MAX_PATTERN_LENGTH} tokens.")
        return tokens"""

if old_method in content:
    content = content.replace(old_method, new_method)
    with open('src/ui/main_window.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('SUCCESS: _parse_tokens method updated')
else:
    print('FAILED: old_method not found')
    lines = content.split('\n')
    for i in range(2741, 2746):
        if i < len(lines):
            print(f'{i}: {lines[i]}')