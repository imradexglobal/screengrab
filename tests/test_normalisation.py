"""Tests for numeric normalisation module."""

import pytest
from decimal import Decimal, ROUND_HALF_UP

from src.normalisation import parse_and_normalise


class TestNumericNormalisation:
    """Test numeric normalisation per the project requirements."""

    def test_integer_one(self):
        """1 becomes 1.00."""
        result = parse_and_normalise("1")
        assert result == ("1.00", "1.00")

    def test_decimal_one_point_five(self):
        """1.5 becomes 1.50."""
        result = parse_and_normalise("1.5")
        assert result == ("1.50", "1.50")

    def test_three_decimals_rounds_down(self):
        """1.234 becomes 1.23 (ROUND_HALF_UP)."""
        result = parse_and_normalise("1.234")
        assert result == ("1.23", "1.23")

    def test_three_decimals_rounds_up(self):
        """1.235 becomes 1.24 (ROUND_HALF_UP)."""
        result = parse_and_normalise("1.235")
        assert result == ("1.24", "1.24")

    def test_negative_seven_point_eight(self):
        """-7.8 becomes -7.80."""
        result = parse_and_normalise("-7.8")
        assert result == ("-7.80", "-7.80")

    def test_leading_decimal(self):
        # .75 becomes 0.75
        result = parse_and_normalise(".75")
        assert result == ("0.75", "0.75")

    def test_thousands_separator(self):
        """1,250.50 → 1250.50."""
        result = parse_and_normalise("1,250.50")
        assert result == ("1250.50", "1250.50")

    def test_negative_with_decimal(self):
        """-10.50 → -10.50."""
        result = parse_and_normalise("-10.50")
        assert result == ("-10.50", "-10.50")

    def test_invalid_text_rejected(self):
        """Invalid numeric text should be rejected (return None)."""
        result = parse_and_normalise("abc")
        assert result is None

        result = parse_and_normalise("")
        assert result is None

        result = parse_and_normalise("hello world")
        assert result is None

    def test_currency_symbol_extraction(self):
        """Currency symbols should be stripped, numeric component extracted."""
        # $1,250.50 → 1250.50
        result = parse_and_normalise("$1,250.50")
        assert result == ("1250.50", "1250.50")

        # ₦500 → 500.00
        result = parse_and_normalise("₦500")
        assert result == ("500.00", "500.00")

        # 25.5% → 25.50
        result = parse_and_normalise("25.5%")
        assert result == ("25.50", "25.50")

    def test_leading_plus_sign(self):
        "+25.5 → 25.50"
        result = parse_and_normalise("+25.5")
        assert result == ("25.50", "25.50")

    def test_very_large_number(self):
        """1500 → 1500.00."""
        result = parse_and_normalise("1500")
        assert result == ("1500.00", "1500.00")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])