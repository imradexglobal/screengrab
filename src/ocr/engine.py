"""OCR engine module for Screen Monitor.

Integrates RapidOCR with numeric normalisation and confidence filtering.
All OCR processing remains local to the computer.
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

try:
    from rapidocr_onnxruntime import RapidOCR
    RAPIDOCR_AVAILABLE = True
except ImportError:
    try:
        from rapidocr_paddle import RapidOCR
        RAPIDOCR_AVAILABLE = True
    except ImportError:
        RAPIDOCR_AVAILABLE = False

try:
    from src.normalisation import parse_and_normalise
except ImportError:
    from ..normalisation import parse_and_normalise

logger = logging.getLogger(__name__)

#: RapidOCR detector settings tuned for a fixed on-screen numeric readout.
#:
#: The library default is ``limit_type='min'`` with a 736 px limit, which
#: *upscales* the image until its shorter side reaches 736 px.  A typical
#: selected region (350 x 114) was therefore inflated about 6.5x on every scan,
#: costing roughly two seconds per frame.  ``'max'`` only ever shrinks a frame
#: that is larger than the limit, so a small region is read at its native
#: resolution: measured ~2.0 s -> ~0.09 s per scan with identical readings.
#: The 0/180-degree text classifier is not needed for a numeric display and
#: costs about 100 ms per scan, so it is switched off as well.
_RAPIDOCR_KWARGS = {
    "det_limit_side_len": 960,
    "det_limit_type": "max",
    "use_cls": False,
}


class OCREngine:
    """Wraps RapidOCR engine with preprocessing and filtering."""

    def __init__(self, min_confidence: float = 70.0):
        """Initialise the OCR engine.

        Args:
            min_confidence: Minimum OCR confidence threshold (0-100).
                Readings below this threshold are rejected.
        """
        self.min_confidence = min_confidence
        if RAPIDOCR_AVAILABLE:
            try:
                self._engine = RapidOCR(**_RAPIDOCR_KWARGS)
            except TypeError:
                # Older RapidOCR builds do not accept configuration overrides.
                self._engine = RapidOCR()
            self._engine_initialised = True
        else:
            # Mock engine for testing - will return empty results
            self._engine = None
            self._engine_initialised = False

    def capture_region(self, screenshot: np.ndarray, bbox: Optional[tuple] = None) -> np.ndarray:
        """Extract a region of interest from a screenshot.

        Args:
            screenshot: Full screenshot array (BGR format from MSS).
            bbox: Optional (x, y, width, height) bounding box. If None,
                returns the full screenshot.

        Returns:
            Cropped region as BGR numpy array.
        """
        if bbox is None:
            return screenshot
        x, y, w, h = bbox
        # Ensure coordinates are within bounds
        h_img, w_img = screenshot.shape[:2]
        x = max(0, min(x, w_img - 1))
        y = max(0, min(y, h_img - 1))
        w = max(1, min(w, w_img - x))
        h = max(1, min(h, h_img - y))
        return screenshot[y:y+h, x:x+w]

    def detect(self, screenshot: np.ndarray, bbox: Optional[tuple] = None) -> List[Dict[str, Any]]:
        """Perform OCR on a screenshot region and return detected text with confidence.

        Args:
            screenshot: Full screenshot array (BGR format from MSS).
            bbox: Optional (x, y, width, height) bounding box.

        Returns:
            List of dicts, each containing:
                - "text": detected text string
                - "confidence": float confidence percentage (0-100)
                - "bbox": bounding box coordinates [(x1, y1), (x2, y2)]
        """
        # If RapidOCR not available, return empty results
        if self._engine is None:
            return []

        # Crop to region if specified
        region = self.capture_region(screenshot, bbox)

        # RapidOCR returns (result, elapse) where result is a list of
        # dicts with keys: text, confidence, bbox
        try:
            result, elapse = self._engine(region)
        except Exception as e:
            logger.error(f"RapidOCR engine error: {e}")
            return []

        # Normalise result format
        detections = []
        if result is None:
            result = []

        for item in result:
            if isinstance(item, dict):
                text = item.get("text", "")
                conf = float(item.get("confidence", 0.0) or 0.0)
                confidence = conf * 100.0 if 0.0 < conf <= 1.0 else conf
                bbox_data = item.get("bbox", [])
                detections.append({
                    "text": str(text) if text else "",
                    "confidence": confidence,
                    "bbox": bbox_data,
                })
            elif isinstance(item, (list, tuple)):
                # RapidOCR standard return format: [dt_boxes, rec_res, score]
                if len(item) >= 3:
                    bbox_data = item[0]
                    text = str(item[1]) if item[1] is not None else ""
                    try:
                        conf = float(item[2])
                    except (ValueError, TypeError):
                        conf = 0.0
                    confidence = conf * 100.0 if 0.0 < conf <= 1.0 else conf
                    detections.append({
                        "text": text,
                        "confidence": confidence,
                        "bbox": bbox_data,
                    })
                elif len(item) == 2:
                    text = str(item[0]) if item[0] is not None else ""
                    try:
                        conf = float(item[1])
                    except (ValueError, TypeError):
                        conf = 0.0
                    confidence = conf * 100.0 if 0.0 < conf <= 1.0 else conf
                    detections.append({
                        "text": text,
                        "confidence": confidence,
                        "bbox": [],
                    })

        return detections

    def process_raw(self, screenshot: np.ndarray, bbox: Optional[tuple] = None) -> List[Dict[str, Any]]:
        """Return every OCR detection with a parsed value where possible.

        Unlike :meth:`process`, nothing is discarded here: unparseable text and
        low-confidence detections are kept so the validation pipeline can apply
        its own taxonomy (empty / invalid / ambiguous / out-of-range / quality).
        """
        raw_candidates = []
        for det in self.detect(screenshot, bbox):
            normalised = parse_and_normalise(det["text"])
            raw_candidates.append({
                "text": det["text"],
                "confidence": det["confidence"],
                "bbox": det.get("bbox", []),
                "canonical_value": normalised[0] if normalised else None,
                "normalised_text": normalised[1] if normalised else None,
                "is_valid": normalised is not None,
            })
        return raw_candidates

    @property
    def available(self) -> bool:
        """True when the underlying OCR engine was constructed successfully."""
        return self._engine is not None

    def process(self, screenshot: np.ndarray, bbox: Optional[tuple] = None) -> List[Dict[str, Any]]:
        """Process OCR on a screenshot and return only valid numeric candidates.

        This is the main entry point: performs OCR, filters by confidence,
        and attempts to parse each candidate as a number.

        Args:
            screenshot: Full screenshot array (BGR format from MSS).
            bbox: Optional (x, y, width, height) bounding box.

        Returns:
            List of dicts with keys:
                - "text": original OCR text
                - "confidence": OCR confidence percentage
                - "canonical_value": normalised two-decimal string (or None if invalid)
                - "is_valid": whether the text parsed as a valid number
        """
        detections = self.detect(screenshot, bbox)
        valid_candidates = []

        for det in detections:
            text = det["text"]
            confidence = det["confidence"]

            # Step 1: Confidence filtering
            if confidence is None or confidence < self.min_confidence:
                continue

            # Step 2: Parse and normalise
            normalised = parse_and_normalise(text)
            if normalised is None:
                continue

            canonical_value, normalised_text = normalised

            valid_candidates.append({
                "text": text,
                "confidence": confidence,
                "canonical_value": canonical_value,
                "is_valid": True,
                "normalised_text": normalised_text,
            })

        return valid_candidates