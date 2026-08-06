# -*- coding: utf-8 -*-
"""
Flask-compatible pupil tracker wrapping the low-level ``_pupil`` detection
functions in a thread-safe manager.

Adapted from esdalmaijer/webcam-eyetracker.  Instead of directly accessing
the webcam (which would conflict with browser-side WebGazer.js access),
this tracker receives webcam frames forwarded from the browser via SocketIO
as base64-encoded JPEG images.

Typical lifecycle per participant viewing session::

    tracker = PupilTracker(threshold=50)
    tracker.start_recording("P001", "video_happy.mp4")

    # … frames arrive via SocketIO …
    result = tracker.feed_frame(base64_jpeg)

    samples = tracker.stop_recording()
"""

import logging
import threading
import time
from typing import Optional

from ._pupil import detect_pupil, preprocess_base64_frame

logger = logging.getLogger(__name__)


class PupilTracker:
    """Flask-compatible pupil tracker.

    Thread safety is guaranteed via a ``threading.Lock`` around all
    mutable state.  The class is designed so that one instance is
    created per participant session (i.e. per SocketIO connection).
    """

    def __init__(self, threshold: int = 50) -> None:
        self._threshold: int = threshold
        self._recording: bool = False
        self._buffer: list[tuple[float, int, int, float]] = []
        self._lock = threading.Lock()
        self._latest_sample: Optional[dict] = None
        self._prev_position: Optional[tuple[int, int]] = None
        self._participant_id: Optional[str] = None
        self._stimulus_name: Optional[str] = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_threshold(self, value: int) -> None:
        """Adjust the pupil detection threshold.

        Lower values are more selective (only the very darkest pixels
        count as pupil candidates); higher values are more permissive.
        Typically called from the calibration UI so the experimenter
        can tune it in real time.

        Args:
            value: Intensity threshold in the range 0-255.
        """
        with self._lock:
            self._threshold = max(0, min(255, int(value)))
            logger.info("Pupil threshold set to %d", self._threshold)

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------

    def feed_frame(self, base64_data: str) -> Optional[dict]:
        """Accept a base64-encoded JPEG frame and run pupil detection.

        If the tracker is currently recording, the detection result is
        appended to the internal buffer for later retrieval via
        :meth:`stop_recording`.

        Args:
            base64_data: Base64 string (with or without data-URI prefix)
                representing a JPEG webcam snapshot taken by the browser.

        Returns:
            A dict ``{"x": int, "y": int, "size": float,
            "detected": True}`` on successful detection, or
            ``{"x": 0, "y": 0, "size": 0, "detected": False}`` when
            the pupil could not be located.  Returns ``None`` only when
            frame decoding itself fails.
        """
        frame = preprocess_base64_frame(base64_data)
        if frame is None:
            return None

        with self._lock:
            threshold = self._threshold
            prev_pos = self._prev_position

        result = detect_pupil(
            frame,
            threshold=threshold,
            prev_position=prev_pos,
        )

        timestamp = time.time()

        if result is not None:
            px, py, radius = result
            sample = {
                "x": px,
                "y": py,
                "size": round(radius * 2, 2),  # diameter
                "detected": True,
            }
            with self._lock:
                self._prev_position = (px, py)
                self._latest_sample = sample
                if self._recording:
                    self._buffer.append((timestamp, px, py, round(radius * 2, 2)))
        else:
            sample = {"x": 0, "y": 0, "size": 0.0, "detected": False}
            with self._lock:
                self._latest_sample = sample

        return sample

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    def start_recording(self, participant_id: str, stimulus_name: str) -> None:
        """Begin buffering pupil detections for a stimulus.

        Clears any existing buffer, resets the previous-position tracker,
        and flags the instance as recording.

        Args:
            participant_id: Unique ID for the current participant.
            stimulus_name: Name of the stimulus being viewed.
        """
        with self._lock:
            self._participant_id = participant_id
            self._stimulus_name = stimulus_name
            self._buffer.clear()
            self._prev_position = None
            self._recording = True
            logger.info(
                "Recording started – participant=%s, stimulus=%s",
                participant_id,
                stimulus_name,
            )

    def stop_recording(self) -> list[tuple[float, int, int, float]]:
        """Stop recording and return the collected samples.

        Returns:
            A list of ``(timestamp, pupil_x, pupil_y, pupil_size)``
            tuples collected since :meth:`start_recording` was called.
            The buffer is cleared after this call.
        """
        with self._lock:
            self._recording = False
            samples = list(self._buffer)
            self._buffer.clear()
            logger.info(
                "Recording stopped – participant=%s, stimulus=%s, "
                "samples=%d",
                self._participant_id,
                self._stimulus_name,
                len(samples),
            )
            self._participant_id = None
            self._stimulus_name = None
            return samples

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_latest(self) -> Optional[dict]:
        """Return the most recent pupil detection result.

        Returns:
            The same dict format returned by :meth:`feed_frame`, or
            ``None`` if no frame has been processed yet.
        """
        with self._lock:
            return self._latest_sample
