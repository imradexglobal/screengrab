"""Numeric normalisation module for Screen Monitor.

Handles parsing and normalisation of OCR-detected numeric text to
exactly two decimal places using Python's Decimal with ROUND_HALF_UP.
"""

import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional, Tuple


#: Characters used to present a reading as a multiplier (e.g. "22.11x",
#: "1×").  They decorate the number and are never part of its value.
MULTIPLIER_MARKERS = "xX×✕✖✗╳*"


# The canonical two-decimal string pattern
TWO_DECIMAL_PATTERN = re.compile(
    r"""
    [+-]?                                          # optional sign
    (?:
        \d{1,3}(?:,\d{3})*                         # thousands-separated integer e.g. 1,250
        |
        \d+                                        # bare integer e.g. 1250
        |
        \d*\.\d+                                   # decimal e.g. 1.25 or .75
    )
    """,
    re.VERBOSE,
)


def strip_thousands_separators(text: str) -> str:
    """Remove thousands separators (commas) from a numeric string.

    Args:
        text: Raw OCR text potentially containing commas as thousands separators.

    Returns:
        Text with commas removed, preserving other characters.
    """
    return text.replace(",", "")


def parse_and_normalise(raw_text: str) -> Optional[Tuple[str, str]]:
    """Parse raw OCR text and normalise to a canonical two-decimal string.

    This function:
    1. Strips thousands separators (commas)
    2. Validates the resulting text is a valid number
    3. Converts to Decimal
    4. Rounds to exactly two decimal places using ROUND_HALF_UP
    5. Returns the canonical two-decimal string

    Args:
        raw_text: The original OCR text extracted from screen.

    Returns:
        A (canonical_value, normalised_text) tuple if successful, or None
        if the text cannot be parsed as a valid number.
        Example: ("1250.50", "1250.50") for input "1,250.50"
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()

    # Characters that decorate a numeric display but are never part of the
    # value itself: currency/unit symbols and a percent sign.
    decoration = "$£€₹₽₩₱¥￥₦₲₡₢₣₤₧₨₭₮₯₰₳₵₶₷₸₺₻₼₾₿元円圆圓%"
    cleaned = text.strip(decoration)

    # A multiplier marker is how the value is *presented*, not part of it: the
    # monitored display shows readings such as "22.11x", "1×" or "1x".  OCR may
    # also return a space before the marker ("1 x"), so trailing whitespace is
    # tolerated.  The marker is only dropped when the remaining text ends in a
    # digit, so non-numeric text ("SPO", "Region A") is still rejected.
    marker_stripped = cleaned.rstrip()
    if len(marker_stripped) > 1 and marker_stripped[-1] in MULTIPLIER_MARKERS:
        head = marker_stripped[:-1].rstrip()
        if head and head[-1].isdigit():
            cleaned = head
    cleaned = cleaned.strip(decoration)

    # A trailing separator means the read was cut off mid-number ("15." for a
    # display of "15.39").  Reject it instead of saving a value that was never
    # on screen.
    if cleaned.endswith((".", ",")):
        return None

    # Remove thousands separators after decoration stripping
    cleaned = strip_thousands_separators(cleaned)

    # Validate against the numeric pattern.  This is a *full* match on purpose:
    # a partial read such as "15." or stray text such as "SPO" must be rejected
    # rather than silently truncated to a number that was never on screen.
    # Partial reads carry high confidence scores but the wrong digits.
    if not TWO_DECIMAL_PATTERN.fullmatch(cleaned):
        return None

    # Handle leading decimal point (e.g., ".75" -> "0.75")
    # Python's Decimal can handle ".75" directly, but let's be explicit
    try:
        decimal_value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None

    # Check for special Decimal values we want to reject
    # NaN, Infinity, etc.
    if not decimal_value.is_finite():
        return None

    # Normalise to two decimal places using ROUND_HALF_UP
    # Quantize to 0.01 (two decimal places)
    quantized = decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Format as exactly two decimal places
    canonical = f"{quantized:.2f}"

    return (canonical, canonical)