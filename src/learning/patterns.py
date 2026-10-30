"""Pure statistical pattern-learning engine.

This module contains **no** Qt, database, screen-capture or automation code so
that it can be exercised independently (spec section 10: "The statistical
engine must be independently testable without requiring actual screen capture
or mouse clicks").

Model
-----
The engine consumes a strictly chronological sequence of category identifiers
(group_ids) belonging to Region A.  For every observed transition it updates
counters for every pattern length in :data:`DEFAULT_PATTERN_LENGTHS` (4-8).

Two distinct quantities are tracked per ``(pattern_length, pattern_sequence,
candidate_group)`` key:

``opportunities``
    How many times the pattern was *actually* followed by that candidate.  This
    is ground truth derived from history and drives candidate **selection**.

``resolved`` / ``correct`` / ``incorrect``
    How many *predictions* for that pattern/candidate have had their outcome
    determined.  This drives **confidence** and **evidence**, exactly as the
    specification requires ("Calculate empirical accuracy using resolved
    predictions only").

Selection rule (documented, deterministic)
------------------------------------------
1. Consider every length ``L`` in ``pattern_lengths`` whose pattern at the
   current position has been seen at least ``min_pattern_opportunities`` times.
2. For that pattern pick the "best" candidate: highest ``opportunities`` (the
   successor this pattern has actually preceded most often); ties broken by the
   candidate's ordinal so the result is stable.
3. Score each pattern-candidate by the **Wilson score lower bound** of
   ``opportunities / total_opportunities`` - how reliably this successor
   followed this pattern, discounted for how little evidence backs it.  That
   ranks "followed 199 of 200 times" above "followed 1 of 1", which neither raw
   frequency nor raw accuracy can do.  A candidate with opportunities but no
   resolved outcome yet is still eligible, because the specification requires
   learning and predicting to start immediately ("Do not wait for 1,000
   readings before learning or predicting"); what stops such a prediction
   authorising a click is the confidence and minimum-evidence gates, not a
   refusal to predict.
4. Rank by ``(support desc, total_opportunities desc, L asc, ordinal asc)``.
   Shorter patterns win ties so the longest pattern is never assumed to be best.
   The winner becomes the prediction.
5. If the two highest-ranked candidates have **different** candidate groups,
   rest on at least ``ambiguity_min_support`` observations each, and their
   support scores fall within ``ambiguity_margin`` percentage points of each
   other, the patterns genuinely conflict, so no prediction is produced rather
   than inventing certainty.  The support floor matters: a handful of readings
   cannot establish that two patterns really disagree, so below it the
   deterministic ordering of step 4 decides instead.

``confidence`` on the resulting prediction remains the specification's
resolved-only figure, ``100 * correct / resolved`` (``0`` until the first
outcome resolves) - the Wilson bound affects only which pattern *wins*, never
what the winner's confidence is reported as.

Learning itself begins with the very first eligible transition - there is no
1,000-reading warm-up, and a prediction is produced as soon as one of the
configured pattern lengths has been fully observed.
"""

import math
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

#: Pattern lengths required by the specification.
DEFAULT_PATTERN_LENGTHS: Tuple[int, ...] = (4, 5, 6, 7, 8)

#: Default size of the "recent performance" rolling window.
DEFAULT_RECENT_WINDOW = 50

#: Default z-value of the Wilson score interval used to rank candidates.
DEFAULT_WILSON_Z = 1.96

#: Two ranked candidates closer than this (percentage points) are considered
#: equally supported, provided both also have at least
#: :data:`DEFAULT_AMBIGUITY_MIN_SUPPORT` observations behind them.
DEFAULT_AMBIGUITY_MARGIN = 5.0

#: Observations a contender needs before a near-tie counts as genuine
#: ambiguity.  Below this the deterministic tie-break decides instead, because a
#: few observations cannot establish that two patterns really conflict.
DEFAULT_AMBIGUITY_MIN_SUPPORT = 10

#: Longest gap (in seconds) between two consecutive readings that still belongs
#: to the same pattern sequence.
DEFAULT_MAX_SEQUENCE_GAP = 300.0


def wilson_lower_bound(successes: int, total: int,
                       z: float = DEFAULT_WILSON_Z) -> float:
    """Lower bound of the Wilson score interval, as a percentage.

    Ranks "this successor followed the pattern 199 of 200 times" above "1 of 1",
    which neither raw frequency nor raw accuracy can do.
    """
    if total <= 0:
        return 0.0
    phat = successes / total
    denom = 1.0 + (z * z) / total
    centre = phat + (z * z) / (2.0 * total)
    margin = z * math.sqrt((phat * (1.0 - phat) + (z * z) / (4.0 * total)) / total)
    return max(0.0, (centre - margin) / denom) * 100.0


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Naive timestamps are treated as UTC so that a mixture of naive and aware
    values never raises ``TypeError`` on subtraction.  Returns ``None`` for
    values that cannot be parsed, which disables the gap check for that call.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class PatternStat:
    """Counters for one ``(length, pattern, candidate)`` key."""

    pattern_length: int
    pattern_sequence: str
    candidate_group: str
    opportunities: int = 0
    resolved: int = 0
    correct: int = 0
    incorrect: int = 0
    recent: Deque[bool] = field(default_factory=lambda: deque(maxlen=DEFAULT_RECENT_WINDOW))
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None

    @property
    def accuracy(self) -> Optional[float]:
        """Empirical accuracy in percent from resolved predictions only."""
        if self.resolved <= 0:
            return None
        return 100.0 * self.correct / self.resolved

    @property
    def recent_accuracy(self) -> Optional[float]:
        if not self.recent:
            return None
        return 100.0 * sum(1 for x in self.recent if x) / len(self.recent)

    def to_row(self) -> Dict:
        return {
            "pattern_length": self.pattern_length,
            "pattern_sequence": self.pattern_sequence,
            "candidate_group": self.candidate_group,
            "opportunities": self.opportunities,
            "resolved": self.resolved,
            "correct": self.correct,
            "incorrect": self.incorrect,
            "recent_outcomes": [1 if x else 0 for x in self.recent],
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


@dataclass
class Prediction:
    """A single prediction event."""

    pattern_length: int
    pattern_sequence: str
    candidate_group: str
    confidence: float
    resolved_count: int
    correct_count: int
    recent_accuracy: Optional[float]
    created_at: str
    basis_reading_id: Optional[int] = None
    expected_reading_id: Optional[int] = None
    total_opportunities: int = 0
    # filled in when the outcome arrives
    actual_group: Optional[str] = None
    resolved_at: Optional[str] = None
    is_correct: Optional[bool] = None

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ObservationResult:
    """What happened when one new category was fed to the engine."""

    resolved: Optional[Prediction] = None
    prediction: Optional[Prediction] = None
    updated_stats: List[Dict] = field(default_factory=list)


class LearningEngine:
    """Incremental, deterministic pattern learner."""

    def __init__(
        self,
        abbreviation_fn: Optional[Callable[[str], str]] = None,
        pattern_lengths: Sequence[int] = DEFAULT_PATTERN_LENGTHS,
        recent_window: int = DEFAULT_RECENT_WINDOW,
        min_pattern_opportunities: int = 1,
        ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
        ambiguity_min_support: int = DEFAULT_AMBIGUITY_MIN_SUPPORT,
        wilson_z: float = DEFAULT_WILSON_Z,
        max_sequence_gap: Optional[float] = DEFAULT_MAX_SEQUENCE_GAP,
    ):
        self._abbrev = abbreviation_fn or (lambda gid: str(gid))
        self.pattern_lengths: Tuple[int, ...] = tuple(
            sorted(int(x) for x in pattern_lengths)
        )
        self.recent_window = int(recent_window)
        self.min_pattern_opportunities = int(min_pattern_opportunities)
        self.ambiguity_margin = float(ambiguity_margin)
        self.ambiguity_min_support = int(ambiguity_min_support)
        self.wilson_z = float(wilson_z)
        #: ``None`` disables gap-based sequence breaks entirely.
        self.max_sequence_gap = (
            None if max_sequence_gap is None else float(max_sequence_gap)
        )

        self._stats: Dict[Tuple[int, str, str], PatternStat] = {}
        self.sequence: List[str] = []
        self._pending: Optional[Prediction] = None
        self._last_observed_at: Optional[datetime] = None
        #: Number of times a break was applied, for diagnostics/tests.
        self.sequence_breaks = 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def stats(self) -> Dict[Tuple[int, str, str], PatternStat]:
        return self._stats

    @property
    def pending(self) -> Optional[Prediction]:
        return self._pending

    def set_pending(self, prediction: Optional[Prediction]) -> None:
        """Restore an outstanding prediction (used when rehydrating state)."""
        self._pending = prediction

    def restore_last_observed(self, value: Optional[str]) -> None:
        """Restore the timestamp of the previous observation.

        Needed when rehydrating: without it the *first* reading after a restart
        would never be recognised as too far removed from the last one, so one
        cross-session boundary would leak into the pattern sequence.
        """
        self._last_observed_at = parse_timestamp(value)

    @property
    def observed_count(self) -> int:
        return len(self.sequence)

    def _sequence_text(self, groups: Iterable[str]) -> str:
        return ",".join(self._abbrev(g) for g in groups)

    def _breaks_sequence(self, now: Optional[str]) -> bool:
        """True when ``now`` is too far after the previous observation.

        Gap-based breaks only ever fire when both timestamps are present and
        parseable, so callers that do not supply timestamps (and the tests that
        exercise the pure counters) keep the original uninterrupted behaviour.
        """
        if self.max_sequence_gap is None or not now:
            return False
        previous = self._last_observed_at
        if previous is None:
            return False
        current = parse_timestamp(now)
        if current is None:
            return False
        return (current - previous).total_seconds() > self.max_sequence_gap

    def _get_stat(self, length: int, sequence: str, candidate: str,
                  now: Optional[str] = None) -> PatternStat:
        key = (length, sequence, candidate)
        stat = self._stats.get(key)
        if stat is None:
            stat = PatternStat(
                pattern_length=length,
                pattern_sequence=sequence,
                candidate_group=candidate,
            )
            stat.recent = deque(maxlen=self.recent_window)
            stat.first_seen = now
            self._stats[key] = stat
        if now:
            stat.last_seen = now
        return stat

    # ------------------------------------------------------------------
    # Core incremental API
    # ------------------------------------------------------------------
    def observe(
        self,
        group_id: str,
        now: Optional[str] = None,
        basis_reading_id: Optional[int] = None,
        expected_reading_id: Optional[int] = None,
    ) -> ObservationResult:
        """Feed the next actual category and return what the engine learned.

        Steps (in this order, so that no future information leaks into a
        historical prediction):

        0. Drop the sequence if this reading is too far after the previous one.
        1. Resolve the pending prediction against ``group_id``.
        2. Update pattern counters for every configured length.
        3. Build a new prediction for the *next* position using only the
           information available now.
        """
        result = ObservationResult()

        # 0) A long gap means the previous reading is not the immediate
        #    predecessor of this one, so the sequence restarts.  Learned
        #    counters are kept - only the continuity is dropped.
        if self._breaks_sequence(now):
            self.sequence = []
            self._pending = None
            self.sequence_breaks += 1
        if now:
            self._last_observed_at = parse_timestamp(now) or self._last_observed_at

        # 1) resolve the outstanding prediction
        if self._pending is not None:
            p = self._pending
            p.actual_group = group_id
            p.resolved_at = now
            p.is_correct = (p.candidate_group == group_id)
            key = (p.pattern_length, p.pattern_sequence, p.candidate_group)
            stat = self._get_stat(p.pattern_length, p.pattern_sequence,
                                  p.candidate_group, now)
            stat.resolved += 1
            if p.is_correct:
                stat.correct += 1
            else:
                stat.incorrect += 1
            stat.recent.append(bool(p.is_correct))
            result.resolved = p
            result.updated_stats.append(stat.to_row())
            self._pending = None

        # 2) update counters for the transition just observed
        self.sequence.append(group_id)
        n = len(self.sequence)
        if n >= 2:
            prev_idx = n - 2  # index of the category immediately before the new one
            for length in self.pattern_lengths:
                if prev_idx + 1 < length:
                    continue
                pattern_groups = self.sequence[prev_idx - length + 1: prev_idx + 1]
                sequence_text = self._sequence_text(pattern_groups)
                stat = self._get_stat(length, sequence_text, group_id, now)
                stat.opportunities += 1
                stat.last_seen = now
                result.updated_stats.append(stat.to_row())

        # 3) predict the next position
        result.prediction = self.predict_next(
            now=now,
            basis_reading_id=basis_reading_id,
            expected_reading_id=expected_reading_id,
        )
        return result

    def predict_next(
        self,
        now: Optional[str] = None,
        basis_reading_id: Optional[int] = None,
        expected_reading_id: Optional[int] = None,
    ) -> Optional[Prediction]:
        """Build (and internally register) the prediction for the next category."""
        candidate = self.select_candidate()
        if candidate is None:
            self._pending = None
            return None

        length, sequence_text, group, stat, total_opp = candidate
        prediction = Prediction(
            pattern_length=length,
            pattern_sequence=sequence_text,
            candidate_group=group,
            confidence=float(stat.accuracy or 0.0),
            resolved_count=stat.resolved,
            correct_count=stat.correct,
            recent_accuracy=stat.recent_accuracy,
            created_at=now or "",
            basis_reading_id=basis_reading_id,
            expected_reading_id=expected_reading_id,
            total_opportunities=total_opp,
        )
        self._pending = prediction
        return prediction

    def select_candidate(
        self,
    ) -> Optional[Tuple[int, str, str, PatternStat, int]]:
        """Return ``(length, sequence, group, stat, total_opportunities)``.

        Applies the documented selection rule described in the module docstring.
        Returns ``None`` when no pattern qualifies or when patterns conflict.
        """
        n = len(self.sequence)
        ranked: List[Tuple[float, int, int, int, Tuple[int, str, str, PatternStat, int]]] = []

        for length in self.pattern_lengths:
            if n < length:
                continue
            pattern_groups = self.sequence[n - length: n]
            sequence_text = self._sequence_text(pattern_groups)

            # gather all candidates recorded for this pattern
            candidates: List[PatternStat] = [
                s for (l, seq, cand), s in self._stats.items()
                if l == length and seq == sequence_text
            ]
            if not candidates:
                continue

            total_opportunities = sum(s.opportunities for s in candidates)
            if total_opportunities < self.min_pattern_opportunities:
                continue

            best = max(candidates, key=lambda s: (s.opportunities, -self._ordinal(s.candidate_group)))
            if best.opportunities <= 0:
                continue

            # Rank by how *often* this pattern was actually followed by this
            # candidate, judged with the Wilson lower bound so that "followed
            # 199 of 200 times" outranks "followed 1 of 1".  Using the
            # transition counts (not the resolved-prediction accuracy) keeps a
            # pattern that is seen constantly but rarely predicted from being
            # permanently ignored.  The spec's confidence formula is
            # unchanged - only the ranking between competing patterns is.
            support = wilson_lower_bound(
                best.opportunities, total_opportunities, self.wilson_z)

            ranked.append(
                (support, total_opportunities, length, self._ordinal(best.candidate_group),
                 (length, sequence_text, best.candidate_group, best, total_opportunities))
            )

        if not ranked:
            return None

        # Strongest support first; then the most evidence; then the SHORTER
        # pattern (never assume the longest pattern is best); then ordinal.
        ranked.sort(key=lambda t: (-t[0], -t[1], t[2], t[3]))
        top = ranked[0]
        if len(ranked) > 1:
            second = ranked[1]
            # Only a genuine near-tie between *different* predicted groups is a
            # conflict, and only when both contenders rest on enough evidence
            # for the tie to be meaningful.  A handful of observations cannot
            # establish that two patterns really disagree, so below the support
            # floor the deterministic ordering above decides instead.
            if (top[4][2] != second[4][2]
                    and top[4][3].opportunities >= self.ambiguity_min_support
                    and second[4][3].opportunities >= self.ambiguity_min_support
                    and abs(top[0] - second[0]) < self.ambiguity_margin):
                # Genuine conflict between near-equally-supported patterns.
                return None
        return top[4]

    # ------------------------------------------------------------------
    def _ordinal(self, group_id: str) -> int:
        """Deterministic tie-break key for a group identifier.

        Uses the digits embedded in the identifier when present (``g1`` -> 1),
        otherwise a stable checksum so results are reproducible across runs.
        """
        text = str(group_id)
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            return int(digits)
        return sum((i + 1) * ord(ch) for i, ch in enumerate(text))

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def top_patterns(self, limit: int = 50) -> List[PatternStat]:
        """Return the most-observed pattern/candidate pairs (for the UI)."""
        ordered = sorted(
            self._stats.values(),
            key=lambda s: (-s.opportunities, s.pattern_length, s.pattern_sequence,
                           s.candidate_group),
        )
        return ordered[:limit]


def replay(
    group_ids: Sequence[str],
    abbreviation_fn: Optional[Callable[[str], str]] = None,
    pattern_lengths: Sequence[int] = DEFAULT_PATTERN_LENGTHS,
    recent_window: int = DEFAULT_RECENT_WINDOW,
    min_pattern_opportunities: int = 1,
    timestamps: Optional[Sequence[str]] = None,
    reading_ids: Optional[Sequence[int]] = None,
) -> Tuple[LearningEngine, List[Prediction]]:
    """Replay a whole sequence chronologically and return every prediction.

    The returned prediction list is in creation order; resolutions are attached
    to the same objects, so the caller can persist both without recomputation.
    """
    engine = LearningEngine(
        abbreviation_fn=abbreviation_fn,
        pattern_lengths=pattern_lengths,
        recent_window=recent_window,
        min_pattern_opportunities=min_pattern_opportunities,
    )
    predictions: List[Prediction] = []
    for idx, gid in enumerate(group_ids):
        ts = timestamps[idx] if timestamps else None
        rid = reading_ids[idx] if reading_ids else None
        next_rid = reading_ids[idx + 1] if reading_ids and idx + 1 < len(reading_ids) else None
        result = engine.observe(gid, now=ts, basis_reading_id=rid, expected_reading_id=next_rid)
        if result.prediction is not None:
            predictions.append(result.prediction)
    return engine, predictions
