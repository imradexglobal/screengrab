"""OCR reading validation pipeline.

Turns raw OCR output for one region into **exactly one** of a small, closed set
of outcomes.  Keeping this logic pure (no Qt, no SQL, no screen access) makes the
taxonomy independently testable, which the specification requires.

Outcome taxonomy
----------------
VALID          a single, confidently-read number that falls inside a configured
               group and is allowed to become a reading.
EMPTY          OCR ran successfully but found no text at all.
FAILURE        OCR itself raised; the frame was never read.
INVALID_TEXT   text was found but none of it is a usable number.
AMBIGUOUS      more than one *different* number was read in the same region, so
               the intended value cannot be determined without guessing.
OUT_OF_RANGE   a number was read but no configured numerical group covers it.
LOW_QUALITY    the frame or the OCR confidence is too poor to trust.
DUPLICATE      a valid number identical to the immediately preceding reading
               while the save mode is "on change".

A failure is **never** coerced into the value zero, and only ``VALID`` produces a
reading that can ever count towards the Region A automation gate.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# --- outcome constants -----------------------------------------------------
VALID = "VALID"
EMPTY = "EMPTY"
FAILURE = "FAILURE"
INVALID_TEXT = "INVALID_TEXT"
AMBIGUOUS = "AMBIGUOUS"
OUT_OF_RANGE = "OUT_OF_RANGE"
LOW_QUALITY = "LOW_QUALITY"
DUPLICATE = "DUPLICATE"

ALL_OUTCOMES = (VALID, EMPTY, FAILURE, INVALID_TEXT, AMBIGUOUS,
                OUT_OF_RANGE, LOW_QUALITY, DUPLICATE)

#: Save modes (spec section 6).
MODE_ON_CHANGE = "on_change"
MODE_EVERY = "every"

#: Minimum acceptable frame variability (std-dev of grey pixels).  A completely
#: uniform frame carries no information, so it is rejected as a quality failure
#: rather than being read as a number.
MIN_FRAME_STD = 2.0


@dataclass
class Evaluation:
    """The single outcome of validating one region's OCR result."""

    status: str
    detail: str = ""
    canonical_value: Optional[str] = None
    numeric_value: Optional[float] = None
    group_id: Optional[str] = None
    original_text: Optional[str] = None
    ocr_confidence: Optional[float] = None

    @property
    def is_valid(self) -> bool:
        return self.status == VALID


def frame_quality_ok(image) -> Tuple[bool, str]:
    """Cheap frame sanity check used before trusting OCR output.

    Returns ``(ok, reason)``.  ``image`` may be a numpy array; anything that
    cannot be inspected is treated as acceptable so the pipeline never blocks
    on an environment quirk.
    """
    if image is None:
        return False, "no frame captured"
    try:
        import numpy as np
        arr = np.asarray(image)
        if arr.size == 0:
            return False, "empty frame"
        if arr.ndim == 3:
            grey = arr[..., :3].mean(axis=2)
        else:
            grey = arr.astype("float64")
        if float(grey.std()) < MIN_FRAME_STD:
            return False, f"uniform frame (std {float(grey.std()):.2f})"
        return True, ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Frame quality check skipped: %s", exc)
        return True, ""


def evaluate_candidates(
    candidates: Sequence[Dict],
    range_config,
    min_confidence: float,
    mode: str = MODE_ON_CHANGE,
    previous_canonical: Optional[str] = None,
) -> Evaluation:
    """Validate the OCR candidates of a single region.

    ``candidates`` are dicts produced by :meth:`OCREngine.process`, each with
    ``text``, ``confidence`` and ``canonical_value``.

    Exactly one :class:`Evaluation` is returned; the caller stores a reading only
    when :attr:`Evaluation.is_valid` is true.
    """
    if not candidates:
        return Evaluation(status=EMPTY, detail="no text detected")

    # 1) Confidence filter (quality gate).
    confident = [c for c in candidates
                 if float(c.get("confidence") or 0.0) >= float(min_confidence)]
    if not confident:
        best = max((float(c.get("confidence") or 0.0) for c in candidates), default=0.0)
        return Evaluation(
            status=LOW_QUALITY,
            detail=f"best OCR confidence {best:.1f}% below {float(min_confidence):.1f}%",
        )

    # 2) Ambiguity: more than one *distinct* number in one region is unusable.
    distinct: Dict[str, Dict] = {}
    for c in confident:
        canonical = c.get("canonical_value")
        if canonical is None:
            continue
        current = distinct.get(canonical)
        if current is None or float(c.get("confidence") or 0.0) > float(
                current.get("confidence") or 0.0):
            distinct[canonical] = c

    if not distinct:
        texts = [str(c.get("text") or "") for c in confident]
        return Evaluation(
            status=INVALID_TEXT,
            detail=f"unparseable text: {texts[:5]}",
            original_text=texts[0] if texts else None,
            ocr_confidence=float(confident[0].get("confidence") or 0.0),
        )

    if len(distinct) > 1:
        return Evaluation(
            status=AMBIGUOUS,
            detail=f"{len(distinct)} distinct numbers in one region: "
                   f"{sorted(distinct.keys())[:5]}",
        )

    canonical, chosen = next(iter(distinct.items()))

    # 3) Range membership.
    try:
        numeric = float(canonical)
    except (TypeError, ValueError):
        return Evaluation(
            status=INVALID_TEXT, detail=f"non-numeric canonical value '{canonical}'",
            original_text=chosen.get("text"),
            ocr_confidence=float(chosen.get("confidence") or 0.0),
        )

    group_id = range_config.classify(numeric) if range_config is not None else None
    if group_id is None:
        return Evaluation(
            status=OUT_OF_RANGE,
            detail=f"{canonical} is outside every configured group",
            canonical_value=canonical, numeric_value=numeric,
            original_text=chosen.get("text"),
            ocr_confidence=float(chosen.get("confidence") or 0.0),
        )

    # 4) Save mode: "on change" suppresses an immediate repeat.
    if mode == MODE_ON_CHANGE and previous_canonical is not None \
            and canonical == previous_canonical:
        return Evaluation(
            status=DUPLICATE, detail=f"{canonical} unchanged since previous reading",
            canonical_value=canonical, numeric_value=numeric, group_id=group_id,
            original_text=chosen.get("text"),
            ocr_confidence=float(chosen.get("confidence") or 0.0),
        )

    return Evaluation(
        status=VALID,
        detail="",
        canonical_value=canonical,
        numeric_value=numeric,
        group_id=group_id,
        original_text=chosen.get("text"),
        ocr_confidence=float(chosen.get("confidence") or 0.0),
    )
