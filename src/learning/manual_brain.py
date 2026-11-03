"""User-defined pattern brain.

The application no longer discovers patterns on its own.  A pattern exists only
because the user added it, and it is stored in ``pattern_definitions``.

Two kinds of pattern are supported:

``range``
    A sequence of category (group) identifiers, e.g. ``g3,g4,g3``.  It predicts
    the next category, which is what automated clicking needs.

``movement``
    A sequence of raw movements, e.g. ``UP,DOWN,UP,DOWN`` where ``UP`` means
    the current value is higher than the previous one at two decimal places.
    It predicts the next movement.

Every definition also records the *other* outcome so the Learning page can show
both dimensions:

* a range pattern records the movement that followed it;
* a movement pattern records the range that followed it.

This module is pure Python - no Qt, no SQL, no screen access - so it is
independently testable, exactly like :mod:`src.learning.patterns`.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.learning.patterns import (
    DEFAULT_MAX_SEQUENCE_GAP,
    DEFAULT_RECENT_WINDOW,
    LearningEngine,
    ObservationResult,
    Prediction,
    parse_timestamp,
)

logger = logging.getLogger(__name__)

#: Pattern kinds stored in ``pattern_definitions.kind``.
KIND_RANGE = "range"
KIND_MOVEMENT = "movement"
PATTERN_KINDS = (KIND_RANGE, KIND_MOVEMENT)

#: Derived statistic kinds stored in ``pattern_stats.pattern_kind``.
STAT_RANGE = "range"                       # range pattern -> next range
STAT_MOVEMENT = "movement"                 # movement pattern -> next movement
STAT_RANGE_MOVEMENT = "range_movement"     # range pattern -> next movement
STAT_MOVEMENT_RANGE = "movement_range"     # movement pattern -> next range

MOVEMENT_UP = "UP"
MOVEMENT_DOWN = "DOWN"
MOVEMENT_FLAT = "FLAT"
MOVEMENT_TOKENS = (MOVEMENT_UP, MOVEMENT_DOWN, MOVEMENT_FLAT)

#: Pattern length bounds accepted by the UI and the repository.
MIN_PATTERN_LENGTH = 2
MAX_PATTERN_LENGTH = 12

_COMPARE = Decimal("0.01")


def normalise_movement(token: str) -> str:
    """Return the canonical movement token (``UP``/``DOWN``/``FLAT``)."""
    value = str(token or "").strip().upper()
    aliases = {
        "UP": MOVEMENT_UP, "U": MOVEMENT_UP, "HIGHER": MOVEMENT_UP,
        "+": MOVEMENT_UP, "INCREASE": MOVEMENT_UP,
        "DOWN": MOVEMENT_DOWN, "D": MOVEMENT_DOWN, "LOWER": MOVEMENT_DOWN,
        "-": MOVEMENT_DOWN, "DECREASE": MOVEMENT_DOWN,
        "FLAT": MOVEMENT_FLAT, "F": MOVEMENT_FLAT, "SAME": MOVEMENT_FLAT,
        "EQUAL": MOVEMENT_FLAT, "0": MOVEMENT_FLAT,
    }
    if value not in aliases:
        raise ValueError(
            f"unknown movement '{token}'; use UP, DOWN or FLAT")
    return aliases[value]


def _quantise(value: float) -> Decimal:
    return Decimal(str(value)).quantize(_COMPARE, rounding=ROUND_HALF_UP)


def movement_between(previous: float, current: float) -> str:
    """Classify ``current`` relative to ``previous`` at two decimal places."""
    prev = _quantise(previous)
    cur = _quantise(current)
    if cur > prev:
        return MOVEMENT_UP
    if cur < prev:
        return MOVEMENT_DOWN
    return MOVEMENT_FLAT


@dataclass(frozen=True)
class PatternDefinition:
    """One user-defined pattern."""

    pattern_id: int
    kind: str
    tokens: Tuple[str, ...]
    label: str = ""
    enabled: bool = True
    expected_kind: Optional[str] = None  # "range" or "movement"
    expected_value: Optional[str] = None

    @property
    def length(self) -> int:
        return len(self.tokens)

    @property
    def sequence_text(self) -> str:
        return ",".join(self.tokens)

    def preview(self, label_fn=None) -> str:
        if label_fn is None:
            return " → ".join(self.tokens)
        return " → ".join(label_fn(t) for t in self.tokens)


@dataclass
class ManualObservation:
    """Everything one new Region A reading produced."""

    range_result: ObservationResult
    movement_result: Optional[ObservationResult] = None
    prediction: Optional[Prediction] = None
    #: Updated cross-outcome rows (range->movement, movement->range).
    cross_stats: List[Dict] = field(default_factory=list)


def _allowed_map(definitions: Iterable[PatternDefinition]) -> Dict[str, int]:
    return {d.sequence_text: d.pattern_id for d in definitions}


def _lengths(definitions: Iterable[PatternDefinition]) -> Tuple[int, ...]:
    lengths = sorted({d.length for d in definitions})
    return tuple(lengths) if lengths else (MIN_PATTERN_LENGTH,)


class ManualBrain:
    """Incremental learner restricted to the user's own pattern definitions."""

    def __init__(
        self,
        definitions: Sequence[PatternDefinition],
        recent_window: int = DEFAULT_RECENT_WINDOW,
        max_sequence_gap: Optional[float] = DEFAULT_MAX_SEQUENCE_GAP,
    ):
        self.definitions: List[PatternDefinition] = list(definitions)
        self._by_id: Dict[int, PatternDefinition] = {
            d.pattern_id: d for d in self.definitions
        }
        self._range_defs = [
            d for d in self.definitions if d.kind == KIND_RANGE and d.enabled
        ]
        self._movement_defs = [
            d for d in self.definitions if d.kind == KIND_MOVEMENT and d.enabled
        ]

        self.recent_window = int(recent_window)
        self.max_sequence_gap = max_sequence_gap
        self.range_engine = LearningEngine(
            abbreviation_fn=lambda token: str(token),
            pattern_lengths=_lengths(self._range_defs),
            recent_window=self.recent_window,
            allowed_sequences=_allowed_map(self._range_defs),
            pattern_kind=STAT_RANGE,
            max_sequence_gap=max_sequence_gap,
        )
        self.movement_engine = LearningEngine(
            abbreviation_fn=lambda token: str(token),
            pattern_lengths=_lengths(self._movement_defs),
            recent_window=self.recent_window,
            allowed_sequences=_allowed_map(self._movement_defs),
            pattern_kind=STAT_MOVEMENT,
            max_sequence_gap=max_sequence_gap,
        )

        #: ``definition_id -> movement -> count`` for range patterns.
        self.range_movement_counts: Dict[int, Dict[str, int]] = {
            d.pattern_id: {} for d in self._range_defs
        }
        #: ``definition_id -> group -> count`` for movement patterns.
        self.movement_range_counts: Dict[int, Dict[str, int]] = {
            d.pattern_id: {} for d in self._movement_defs
        }

        self._last_numeric: Optional[float] = None
        self._last_timestamp: Optional[str] = None
        self._pending_range_patterns: List[int] = []
        self._pending_movement_patterns: List[int] = []

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def stats(self) -> List:
        """Every primary stat row (range + movement)."""
        return list(self.range_engine.stats.values()) + \
            list(self.movement_engine.stats.values())

    @property
    def observed_count(self) -> int:
        """Number of Region A readings fed to the brain."""
        return self.range_engine.observed_count

    def prime_pending(self) -> None:
        """Re-match the patterns that ended at the restored sequence tail.

        After a reload the last stored reading has already been observed but
        its cross-outcome (range -> movement, movement -> range) has not been
        recorded yet, because that requires the *next* reading.  Priming the
        pending lists keeps that one transition from being lost.
        """
        self._pending_range_patterns = self._matched(
            self._range_defs, self.range_engine.sequence)
        self._pending_movement_patterns = self._matched(
            self._movement_defs, self.movement_engine.sequence)

    def all_stat_rows(self) -> List[Dict]:
        rows = [s.to_row() for s in self.range_engine.stats.values()]
        rows.extend(s.to_row() for s in self.movement_engine.stats.values())
        rows.extend(self.cross_stat_rows())
        return rows

    def cross_stat_rows(self) -> List[Dict]:
        rows: List[Dict] = []
        for definition_id, counts in self.range_movement_counts.items():
            definition = self._by_id.get(definition_id)
            if definition is None:
                continue
            for movement, count in counts.items():
                rows.append(self._cross_row(
                    definition, STAT_RANGE_MOVEMENT, movement, count))
        for definition_id, counts in self.movement_range_counts.items():
            definition = self._by_id.get(definition_id)
            if definition is None:
                continue
            for group_id, count in counts.items():
                rows.append(self._cross_row(
                    definition, STAT_MOVEMENT_RANGE, group_id, count))
        return rows

    @staticmethod
    def _cross_row(definition: PatternDefinition, kind: str,
                   candidate: str, opportunities: int) -> Dict:
        return {
            "pattern_length": definition.length,
            "pattern_sequence": definition.sequence_text,
            "candidate_group": str(candidate),
            "opportunities": int(opportunities),
            "resolved": 0,
            "correct": 0,
            "incorrect": 0,
            "recent_outcomes": [],
            "first_seen": None,
            "last_seen": None,
            "pattern_kind": kind,
            "definition_id": definition.pattern_id,
        }

    # ------------------------------------------------------------------
    # Core incremental API
    # ------------------------------------------------------------------
    def _gap_breaks(self, now: Optional[str]) -> bool:
        if self.max_sequence_gap is None or not now or not self._last_timestamp:
            return False
        previous = parse_timestamp(self._last_timestamp)
        current = parse_timestamp(now)
        if previous is None or current is None:
            return False
        return (current - previous).total_seconds() > float(self.max_sequence_gap)

    def _matched(self, definitions: Sequence[PatternDefinition],
                 sequence: Sequence[str]) -> List[int]:
        matched: List[int] = []
        for definition in definitions:
            length = definition.length
            if len(sequence) >= length and \
                    self._pattern_matches(definition.tokens, tuple(sequence[-length:])):
                matched.append(definition.pattern_id)
        return matched

    @staticmethod
    def _pattern_matches(pattern: Sequence[str],
                         sequence: Sequence[str]) -> bool:
        """True when ``pattern`` matches ``sequence`` treating ``?`` as wildcard."""
        return all(p == "?" or p == s for p, s in zip(pattern, sequence))

    def _record_cross(self, range_patterns: Iterable[int],
                      movement_patterns: Iterable[int],
                      group_id: str, movement: Optional[str]) -> List[Dict]:
        changed: List[Dict] = []
        if movement is not None:
            for definition_id in range_patterns:
                counts = self.range_movement_counts.setdefault(definition_id, {})
                counts[movement] = counts.get(movement, 0) + 1
                definition = self._by_id.get(definition_id)
                if definition is not None:
                    changed.append(self._cross_row(
                        definition, STAT_RANGE_MOVEMENT, movement,
                        counts[movement]))
        if group_id:
            for definition_id in movement_patterns:
                counts = self.movement_range_counts.setdefault(definition_id, {})
                counts[group_id] = counts.get(group_id, 0) + 1
                definition = self._by_id.get(definition_id)
                if definition is not None:
                    changed.append(self._cross_row(
                        definition, STAT_MOVEMENT_RANGE, group_id,
                        counts[group_id]))
        return changed

    def observe(
        self,
        group_id: str,
        numeric_value: Optional[float] = None,
        now: Optional[str] = None,
        basis_reading_id: Optional[int] = None,
        expected_reading_id: Optional[int] = None,
    ) -> ManualObservation:
        """Feed one Region A reading and return what the brain learned.

        The order matters: cross-outcomes for patterns matched on the previous
        reading are recorded *before* the new reading's own patterns are
        matched, so no future information leaks backwards.
        """
        gap = self._gap_breaks(now)
        if gap:
            self._pending_range_patterns = []
            self._pending_movement_patterns = []
            self._last_numeric = None
            # The movement engine is only observed when a movement exists, so
            # clear its continuity here as well rather than waiting for the
            # reading after the gap.
            self.movement_engine.sequence = []
            self.movement_engine.set_pending(None)

        movement: Optional[str] = None
        if numeric_value is not None and self._last_numeric is not None:
            movement = movement_between(self._last_numeric, numeric_value)

        # Cross outcomes of the previous position.
        cross_stats = self._record_cross(
            self._pending_range_patterns,
            self._pending_movement_patterns,
            group_id, movement,
        )

        # Primary range learning.
        range_result = self.range_engine.observe(
            group_id, now=now, basis_reading_id=basis_reading_id,
            expected_reading_id=expected_reading_id,
        )

        # Primary movement learning (only when a movement exists).
        movement_result = None
        if movement is not None:
            movement_result = self.movement_engine.observe(movement, now=now)

        # Remember what matched now so the *next* reading can record outcomes.
        self._pending_range_patterns = self._matched(
            self._range_defs, self.range_engine.sequence)
        self._pending_movement_patterns = self._matched(
            self._movement_defs, self.movement_engine.sequence)

        # Attach the movement forecast to the range prediction.
        prediction = range_result.prediction
        if prediction is not None:
            movement_pending = self.movement_engine.pending
            if movement_pending is not None:
                prediction.next_movement = movement_pending.candidate_group
                prediction.next_movement_confidence = movement_pending.confidence

        self._last_numeric = numeric_value
        if now:
            self._last_timestamp = now

        return ManualObservation(
            range_result=range_result,
            movement_result=movement_result,
            prediction=prediction,
            cross_stats=cross_stats,
        )

    # ------------------------------------------------------------------
    # Rehydration helpers
    # ------------------------------------------------------------------
    def restore_position(self, last_numeric: Optional[float],
                         last_timestamp: Optional[str]) -> None:
        self._last_numeric = last_numeric
        self._last_timestamp = last_timestamp
        self.range_engine.restore_last_observed(last_timestamp)
        self.movement_engine.restore_last_observed(last_timestamp)

    def refresh_movement_prediction(self, now: Optional[str] = None) -> None:
        """Recompute the informational movement forecast after a reload."""
        self.movement_engine.predict_next(now=now)
