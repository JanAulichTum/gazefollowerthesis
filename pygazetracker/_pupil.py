# -*- coding: utf-8 -*-
"""
Pupil detection using OpenCV threshold-based dark-patch detection.

Adapted from esdalmaijer/webcam-eyetracker and rewritten for modern
Python 3 without any PyGame dependencies.  Uses Haar cascade classifiers
for optional face/eye ROI narrowing before searching for the pupil.

Typical pipeline:
    base64 JPEG  →  preprocess_base64_frame()  →  detect_pupil()
                                                   ↳ (x, y, radius) or None
"""

import base64
import logging
import math
from typing import Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Haar cascade classifiers (loaded once at module level)
# ---------------------------------------------------------------------------
_FACE_CASCADE: Optional[cv2.CascadeClassifier] = None
_EYE_CASCADE: Optional[cv2.CascadeClassifier] = None


def _load_cascades() -> Tuple[Optional[cv2.CascadeClassifier],
                               Optional[cv2.CascadeClassifier]]:
    """Lazy-load Haar cascade classifiers shipped with OpenCV."""
    global _FACE_CASCADE, _EYE_CASCADE

    if _FACE_CASCADE is not None:
        return _FACE_CASCADE, _EYE_CASCADE

    face_xml = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    eye_xml = cv2.data.haarcascades + "haarcascade_eye.xml"

    _FACE_CASCADE = cv2.CascadeClassifier(face_xml)
    if _FACE_CASCADE.empty():
        logger.warning("Failed to load face cascade from %s", face_xml)
        _FACE_CASCADE = None

    _EYE_CASCADE = cv2.CascadeClassifier(eye_xml)
    if _EYE_CASCADE.empty():
        logger.warning("Failed to load eye cascade from %s", eye_xml)
        _EYE_CASCADE = None

    return _FACE_CASCADE, _EYE_CASCADE


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def preprocess_base64_frame(base64_data: str) -> Optional[np.ndarray]:
    """Decode a base64-encoded JPEG frame into an OpenCV BGR numpy array.

    Handles the optional ``data:image/…;base64,`` prefix that browsers
    attach when reading from a ``<canvas>`` element.

    Args:
        base64_data: Raw base64 string (with or without data-URI prefix).

    Returns:
        BGR numpy array, or ``None`` if decoding fails.
    """
    try:
        # Strip the data-URI prefix if present
        if "," in base64_data:
            base64_data = base64_data.split(",", 1)[1]

        raw_bytes = base64.b64decode(base64_data)
        np_arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        return frame
    except Exception:
        logger.debug("Failed to decode base64 frame", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def _find_eye_rois(
    gray: np.ndarray,
    use_face_detection: bool,
) -> list[Tuple[int, int, int, int]]:
    """Return a list of (x, y, w, h) eye ROI rectangles.

    Strategy
    --------
    1. Detect face → restrict eye search to the upper half of the face.
    2. Detect eyes inside the face ROI.
    3. If face detection is disabled or fails, return the full frame as
       a single ROI so detection can still proceed.
    """
    if not use_face_detection:
        h, w = gray.shape[:2]
        return [(0, 0, w, h)]

    face_cascade, eye_cascade = _load_cascades()

    if face_cascade is None:
        h, w = gray.shape[:2]
        return [(0, 0, w, h)]

    faces = face_cascade.detectMultiScale(
        gray,
        scaleFactor=1.3,
        minNeighbors=5,
        minSize=(60, 60),
    )

    if len(faces) == 0:
        h, w = gray.shape[:2]
        return [(0, 0, w, h)]

    # Use the largest face
    fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])

    # Restrict to upper half of the face (where the eyes are)
    eye_region_gray = gray[fy: fy + fh // 2, fx: fx + fw]

    if eye_cascade is None:
        # No eye cascade – return the upper-face region
        return [(fx, fy, fw, fh // 2)]

    eyes = eye_cascade.detectMultiScale(
        eye_region_gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(20, 20),
    )

    if len(eyes) == 0:
        return [(fx, fy, fw, fh // 2)]

    # Translate eye coords back to full-frame coordinates
    rois = []
    for ex, ey, ew, eh in eyes:
        rois.append((fx + ex, fy + ey, ew, eh))
    return rois


def detect_pupil(
    frame: np.ndarray,
    threshold: int = 50,
    min_area: int = 20,
    max_area_ratio: float = 0.3,
    prev_position: Optional[Tuple[int, int]] = None,
    max_distance_ratio: float = 0.2,
    use_face_detection: bool = True,
) -> Optional[Tuple[int, int, float]]:
    """Detect the pupil in a webcam frame using threshold-based dark-patch
    detection.

    Algorithm (adapted from esdalmaijer/webcam-eyetracker):

    1. Convert to grayscale.
    2. Optionally detect face region using Haar cascades to narrow search
       area.
    3. Apply binary threshold (inverted) to find dark regions.
    4. Find contours and select the most likely pupil candidate.
    5. Return ``(x, y, radius)`` or ``None`` if no pupil found.

    Args:
        frame: BGR webcam frame (numpy array).
        threshold: Pixel intensity below which a region is considered
            "dark" (pupil candidate).
        min_area: Minimum contour area in pixels² to consider (filters
            noise).
        max_area_ratio: Maximum contour area as ratio of ROI area.
        prev_position: Previous pupil position ``(x, y)`` for continuity
            filtering.
        max_distance_ratio: Maximum allowed movement as ratio of the
            frame diagonal.
        use_face_detection: Whether to use Haar cascade face/eye
            detection to narrow the search region.

    Returns:
        ``(pupil_x, pupil_y, pupil_radius)`` in pixel coordinates
        relative to the full frame, or ``None`` if no pupil was found.
    """
    if frame is None or frame.size == 0:
        return None

    frame_h, frame_w = frame.shape[:2]
    frame_diagonal = math.hypot(frame_w, frame_h)

    # Step 1: convert to grayscale
    if len(frame.shape) == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame.copy()

    # Step 2: obtain eye ROI(s)
    rois = _find_eye_rois(gray, use_face_detection)

    best_candidate: Optional[Tuple[int, int, float]] = None
    best_area = 0

    for (rx, ry, rw, rh) in rois:
        # Clamp ROI to frame bounds
        rx = max(0, rx)
        ry = max(0, ry)
        rw = min(rw, frame_w - rx)
        rh = min(rh, frame_h - ry)
        if rw <= 0 or rh <= 0:
            continue

        roi_gray = gray[ry: ry + rh, rx: rx + rw]
        roi_area = rw * rh

        # Step 3: binary threshold (inverted) – dark pixels become white
        _, binary = cv2.threshold(
            roi_gray, threshold, 255, cv2.THRESH_BINARY_INV
        )

        # Light morphological opening to reduce noise
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

        # Step 4: find contours
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        for cnt in contours:
            area = cv2.contourArea(cnt)

            # Filter by minimum absolute area
            if area < min_area:
                continue

            # Filter by maximum area relative to ROI
            if area > max_area_ratio * roi_area:
                continue

            # Compute enclosing circle
            (cx, cy), radius = cv2.minEnclosingCircle(cnt)
            cx_frame = int(cx) + rx
            cy_frame = int(cy) + ry

            # Continuity filter: reject if too far from previous position
            if prev_position is not None:
                dist = math.hypot(
                    cx_frame - prev_position[0],
                    cy_frame - prev_position[1],
                )
                if dist > max_distance_ratio * frame_diagonal:
                    continue

            # Select the largest qualifying contour
            if area > best_area:
                best_area = area
                best_candidate = (cx_frame, cy_frame, float(radius))

    return best_candidate
