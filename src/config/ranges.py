"""Numerical ranges and category groups.

Defines the editable numerical categories ("groups") used to classify every
accepted Region A reading, the nine defaults required by the specification,
strict validation of user-edited boundaries, and the classification function
used by both the live pipeline and the "Recalculate App Brain" rebuild.

All comparisons use a single canonical rounding strategy
(:data:`COMPARISON_PRECISION`) so that a value is never classified from an
unrounded float and never compared against unrounded floats.
"""

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Iterable, List, Optional, Sequence

#: All boundary comparisons are performed at the same precision as the stored
#: two-decimal display value.
COMPARISON_PRECISION = Decimal("0.01")

#: The nine default groups (spec section 9).
DEFAULT_GROUPS: List["Group"] = [
    {"group_id": "g1", "ordinal": 1, "name": "Nano", "abbreviation": "NA",
     "lower_bound": 1.00, "upper_bound": 1.25},
    {"group_id": "g2", "ordinal": 2, "name": "Micro", "abbreviation": "MI",
     "lower_bound": 1.26, "upper_bound": 1.50},
    {"group_id": "g3", "ordinal": 3, "name": "Mini", "abbreviation": "MN",
     "lower_bound": 1.51, "upper_bound": 2.00},
    {"group_id": "g4", "ordinal": 4, "name": "Small", "abbreviation": "SM",
     "lower_bound": 2.01, "upper_bound": 2.50},
    {"group_id": "g5", "ordinal": 5, "name": "Medium", "abbreviation": "MD",
     "lower_bound": 2.51, "upper_bound": 5.00},
    {"group_id": "g6", "ordinal": 6, "name": "Large", "abbreviation": "LG",
     "lower_bound": 5.01, "upper_bound": 10.00},
    {"group_id": "g7", "ordinal": 7, "name": "Mega", "abbreviation": "MG",
     "lower_bound": 10.01, "upper_bound": 24.00},
    {"group_id": "g8", "ordinal": 8, "name": "Super", "abbreviation": "SP",
     "lower_bound": 24.01, "upper_bound": 100.00},
    {"group_id": "g9", "ordinal": 9, "name": "Ultra", "abbreviation": "UL",
     "lower_bound": 100.01, "upper_bound": None},
]


def to_decimal(value) -> Decimal:
    """Quantise any numeric input to the canonical two-decimal grid."""
    return Decimal(str(value)).quantize(COMPARISON_PRECISION, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Group:
    """A single numerical category."""

    group_id: str
    ordinal: int
    name: str
    abbreviation: str
    lower_bound: float
    upper_bound: Optional[float]

    def contains(self, value: float) -> bool:
        """True when ``value`` (rounded to 2dp) falls inside [lo, hi]."""
        v = to_decimal(value)
        if v < to_decimal(self.lower_bound):
            return False
        if self.upper_bound is not None and v > to_decimal(self.upper_bound):
            return False
        return True

    def to_dict(self) -> Dict:
        return {
            "group_id": self.group_id,
            "ordinal": self.ordinal,
            "name": self.name,
            "abbreviation": self.abbreviation,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
        }


def _coerce(raw) -> Group:
    if isinstance(raw, Group):
        return raw
    return Group(
        group_id=str(raw["group_id"]),
        ordinal=int(raw["ordinal"]),
        name=str(raw["name"]),
        abbreviation=str(raw["abbreviation"]),
        lower_bound=float(raw["lower_bound"]),
        upper_bound=None if raw.get("upper_bound") is None else float(raw["upper_bound"]),
    )


def group_rows(groups: Iterable) -> List[Dict]:
    """Serialise groups to plain dicts (used for DB rows and JSON snapshots)."""
    return [_coerce(g).to_dict() for g in groups]


class RangeValidationError(ValueError):
    """Raised when a range configuration is invalid and must be rejected."""


class RangeConfig:
    """An ordered, validated collection of :class:`Group` objects."""

    def __init__(self, groups: Sequence, version: int = 0):
        self._groups: List[Group] = sorted((_coerce(g) for g in groups),
                                           key=lambda g: g.ordinal)
        self.version = int(version)
        self._by_id: Dict[str, Group] = {g.group_id: g for g in self._groups}

    # -- accessors -----------------------------------------------------
    @property
    def groups(self) -> List[Group]:
        return list(self._groups)

    def get(self, group_id: str) -> Optional[Group]:
        return self._by_id.get(group_id)

    def abbreviation(self, group_id: Optional[str]) -> str:
        g = self._by_id.get(group_id) if group_id else None
        return g.abbreviation if g else "?"

    def abbreviation_map(self) -> Dict[str, str]:
        return {g.group_id: g.abbreviation for g in self._groups}

    # -- validation ----------------------------------------------------
    def validate(self) -> List[str]:
        """Return a list of human-readable validation errors (empty = valid)."""
        errors: List[str] = []
        if not self._groups:
            return ["At least one group is required."]

        seen_ids, seen_abbr, seen_ord = set(), set(), set()
        for g in self._groups:
            if not g.group_id:
                errors.append("A group is missing its identifier.")
            if g.group_id in seen_ids:
                errors.append(f"Duplicate group identifier '{g.group_id}'.")
            seen_ids.add(g.group_id)

            abbr = (g.abbreviation or "").strip()
            if not abbr:
                errors.append(f"Group '{g.name}' has an empty abbreviation.")
            elif abbr.upper() in seen_abbr:
                errors.append(f"Abbreviation '{abbr}' is not unique.")
            seen_abbr.add(abbr.upper())

            if g.ordinal in seen_ord:
                errors.append(f"Duplicate ordinal {g.ordinal}.")
            seen_ord.add(g.ordinal)

            if g.upper_bound is not None and to_decimal(g.upper_bound) < to_decimal(g.lower_bound):
                errors.append(
                    f"Group '{g.name}' has a reversed range "
                    f"({g.lower_bound} > {g.upper_bound})."
                )

        open_ended = [g for g in self._groups if g.upper_bound is None]
        if len(open_ended) > 1:
            errors.append("Only one group may have no upper limit.")
        if open_ended and open_ended[0] is not self._groups[-1]:
            errors.append("The group without an upper limit must be the last group.")

        ordered = sorted(self._groups, key=lambda g: to_decimal(g.lower_bound))
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev.upper_bound is None:
                errors.append(
                    f"Group '{prev.name}' has no upper limit but is not the last range."
                )
                continue
            if to_decimal(nxt.lower_bound) <= to_decimal(prev.upper_bound):
                errors.append(
                    f"Groups '{prev.name}' and '{nxt.name}' overlap "
                    f"({prev.upper_bound} >= {nxt.lower_bound})."
                )
            elif to_decimal(nxt.lower_bound) - to_decimal(prev.upper_bound) > Decimal("0.01"):
                errors.append(
                    f"A gap exists between '{prev.name}' (ends {prev.upper_bound}) and "
                    f"'{nxt.name}' (starts {nxt.lower_bound})."
                )
        return errors

    def require_valid(self) -> None:
        errors = self.validate()
        if errors:
            raise RangeValidationError("; ".join(errors))

    # -- classification ------------------------------------------------
    def classify(self, value: float) -> Optional[str]:
        """Return the group_id covering ``value``, or None when uncovered.

        Values below the lowest configured boundary, or falling into a
        configured gap, are unclassified and must not enter the learning
        sequence.
        """
        target = to_decimal(value)
        for g in self._groups:
            if g.contains(target):
                return g.group_id
        return None

    def classify_sequence(self, values: Iterable[float]) -> List[str]:
        """Classify many values, dropping unclassified ones."""
        out = []
        for v in values:
            gid = self.classify(v)
            if gid is not None:
                out.append(gid)
        return out

    def with_updated(self, group_id: str, **changes) -> "RangeConfig":
        """Return a copy with one group's fields replaced (no persistence)."""
        updated = []
        for g in self._groups:
            updated.append(replace(g, **changes) if g.group_id == group_id else g)
        return RangeConfig(updated, self.version)

    def to_snapshot(self) -> List[Dict]:
        return group_rows(self._groups)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def classify_value(config: RangeConfig, value: float) -> Optional[str]:
    """Module-level wrapper used by the rebuild and live pipelines."""
    return config.classify(value)


def validate_groups(groups: Sequence) -> List[str]:
    """Validate a raw sequence of group definitions without constructing a full
    :class:`RangeConfig`."""
    return RangeConfig(groups).validate()
