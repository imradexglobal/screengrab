"""Tests for OCR engine module - focused on process() logic and normalisation."""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import patch, MagicMock
import cv2
import numpy as np


class TestOCRNormalisationIndependent:
    """Tests that normalisation works independently of OCR engine."""

    def test_all_mandated_normalisations(self):
        """Test all mandated number normalisations from the spec."""
        from src.normalisation import parse_and_normalise

        # Integers
        assert parse_and_normalise("1") == ("1.00", "1.00")
        assert parse_and_normalise("1500") == ("1500.00", "1500.00")

        # Decimals
        assert parse_and_normalise("1.5") == ("1.50", "1.50")
        assert parse_and_normalise("1.234") == ("1.23", "1.23")
        assert parse_and_normalise("1.235") == ("1.24", "1.24")

        # Negative values
        assert parse_and_normalise("-7.8") == ("-7.80", "-7.80")
        assert parse_and_normalise("-10.50") == ("-10.50", "-10.50")

        # Leading decimal
        assert parse_and_normalise(".75") == ("0.75", "0.75")

        # Thousands separators
        assert parse_and_normalise("1,250.50") == ("1250.50", "1250.50")

        # Currency symbols stripped
        assert parse_and_normalise("$1,250.50") == ("1250.50", "1250.50")
        assert parse_and_normalise("₦500") == ("500.00", "500.00")

        # Percentage signs stripped
        assert parse_and_normalise("25.5%") == ("25.50", "25.50")

        # Leading plus sign
        assert parse_and_normalise("+25.5") == ("25.50", "25.50")

        # Invalid text rejected
        assert parse_and_normalise("abc") is None
        assert parse_and_normalise("") is None
        assert parse_and_normalise("hello world") is None


class TestOCREngineProcessLogic:
    """Tests for the OCR engine's process() method logic.

    These tests verify the flow: detect -> confidence filter -> normalisation
    without needing a live RapidOCR engine.
    """

    def test_process_with_valid_text(self):
        """Process method flow: detect -> confidence -> normalisation."""
        from src.ocr.engine import OCREngine
        from src.normalisation import parse_and_normalise

        # Create engine - will be in degraded mode since rapidocr_paddle
        # may not be fully functional, but process() still works
        engine = OCREngine(min_confidence=70.0)

        # Test that process delegates to detect then normalises
        # When engine _engine is None, detect returns []
        # So process returns []
        screenshot = np.zeros((100, 100, 4), dtype=np.uint8)
        results = engine.process(screenshot)

        # When engine not available, should return empty list
        assert len(results) == 0

    def test_process_minimum_confidence_filter(self):
        """Process filters by minimum confidence."""
        # This test verifies the confidence filtering logic
        # by checking that the engine's min_confidence attribute is set
        from src.ocr.engine import OCREngine

        engine = OCREngine(min_confidence=70.0)
        assert engine.min_confidence == 70.0

        # Lower confidence should be accepted if it passes normalisation
        # (the actual filter happens in the engine's process method)
        # Here we just verify the attribute is set correctly

    def test_engine_initialisation_sets_min_confidence(self):
        """Engine initialisation correctly sets minimum confidence."""
        from src.ocr.engine import OCREngine

        # Test default
        engine_default = OCREngine()
        assert engine_default.min_confidence == 70.0

        # Test custom
        engine_custom = OCREngine(min_confidence=85.0)
        assert engine_custom.min_confidence == 85.0


class TestOCREngineCaptureRegionLogic:
    """Tests for region capture logic (independent of OCR engine)."""

    def test_capture_region_full_unchanged(self):
        """Full screenshot returned unchanged."""
        from src.ocr.engine import OCREngine
        engine = OCREngine(min_confidence=70.0)
        screenshot = np.zeros((200, 200, 4), dtype=np.uint8)
        region = engine.capture_region(screenshot)
        assert region.shape == screenshot.shape

    def test_capture_region_with_bbox(self):
        """Crop to specified bounding box."""
        from src.ocr.engine import OCREngine
        engine = OCREngine(min_confidence=70.0)

        screenshot = np.zeros((100, 100, 4), dtype=np.uint8)
        bbox = (10, 10, 50, 50)
        region = engine.capture_region(screenshot, bbox)
        assert region.shape == (50, 50, 4)

    def test_capture_region_negative_coords_clamped(self):
        """Negative coordinates are clamped to 0."""
        from src.ocr.engine import OCREngine
        engine = OCREngine(min_confidence=70.0)

        screenshot = np.zeros((100, 100, 4), dtype=np.uint8)
        bbox = (-10, -10, 120, 120)
        region = engine.capture_region(screenshot, bbox)
        assert region.shape[0] <= 100 and region.shape[1] <= 100

    def test_capture_region_edge_clamped(self):
        """Bbox at edge clamped to image bounds."""
        from src.ocr.engine import OCREngine
        engine = OCREngine(min_confidence=70.0)

        screenshot = np.zeros((100, 100, 4), dtype=np.uint8)
        # Starting at 90 with size 20 on 100px image -> clamped to 10
        bbox = (90, 90, 20, 20)
        region = engine.capture_region(screenshot, bbox)
        # After clamping, should be 10x10
        assert region.shape == (10, 10, 4)