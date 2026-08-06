# -*- coding: utf-8 -*-
"""
Offline fixation detection for low-sampling-rate webcam gaze data.

Implements the dispersion-based I-DT algorithm (Salvucci & Goldberg,
2000), which is the recommended choice for webcam eye tracking: at
15–30 Hz, velocity-based algorithms (I-VT) cannot resolve saccades
reliably, whereas dispersion over a duration window remains meaningful.

Operates on NORMALIZED video coordinates (``gaze_video_nx/ny``, 0–1
inside the video frame) with the ``video_time_s`` column as time axis —
the same representation used by the replay overlay and the LLM feedback
pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import FIXATION_DISPERSION_NORM, FIXATION_MIN_DURATION_S


@dataclass
class Fixation:
    """A detected fixation in normalized video coordinates."""

    t_start: float          # seconds since video start
    t_end: float
    nx: float               # centroid, 0–1 within the video frame
    ny: float
    n_samples: int
    dt_s: float = 0.0       # local inter-sample interval (duration σ)

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start

    @property
    def t_mid(self) -> float:
        return (self.t_start + self.t_end) / 2.0

    @property
    def duration_uncertainty_s(self) -> float:
        """±1 inter-sample interval — the onset/offset quantization that
        the sampling rate imposes on the measured fixation duration."""
        return self.dt_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "t_start": round(self.t_start, 3),
            "t_end": round(self.t_end, 3),
            "duration_s": round(self.duration, 3),
            "duration_uncertainty_s": round(self.dt_s, 3),
            "nx": round(self.nx, 4),
            "ny": round(self.ny, 4),
            "n_samples": self.n_samples,
        }


def effective_sampling_hz(times: "list[float]") -> float:
    """Median-based effective sampling rate (Hz) from a timestamp list."""
    if len(times) < 2:
        return 0.0
    diffs = sorted(t2 - t1 for t1, t2 in zip(times[:-1], times[1:]) if t2 > t1)
    if not diffs:
        return 0.0
    dt = diffs[len(diffs) // 2]
    return 1.0 / dt if dt > 0 else 0.0


def detect_fixations(
    times: "list[float]",
    xs: "list[float]",
    ys: "list[float]",
    dispersion_threshold: float = FIXATION_DISPERSION_NORM,
    min_duration: float = FIXATION_MIN_DURATION_S,
    max_gap_s: "float | None" = None,
    min_samples: int = 3,
) -> list[Fixation]:
    """I-DT fixation detection (rate-adaptive).

    Args:
        times: Sample timestamps (seconds), ascending.
        xs, ys: Normalized gaze coordinates (may contain values outside
            0–1; such samples break fixation groups — looking off-video
            is not a fixation on the content).
        dispersion_threshold: Max (max-min)x + (max-min)y within a
            fixation, in normalized units.
        min_duration: Minimum fixation duration in seconds.
        max_gap_s: A frame gap larger than this (tracking dropout)
            terminates the current candidate window. ``None`` → derived
            from the effective sampling rate (2.5× the median interval),
            so decimated low-rate recordings are handled correctly.
        min_samples: A fixation must contain at least this many samples.
            At low sampling rates this is the binding constraint — it
            prevents 1–2 stray samples from being reported as a fixation
            the rate cannot actually resolve.

    Returns:
        List of :class:`Fixation`, in temporal order.
    """
    n = len(times)
    if n == 0 or n != len(xs) or n != len(ys):
        return []

    # Effective inter-sample interval → rate-adaptive gap tolerance and
    # per-fixation duration uncertainty (onset/offset quantization).
    dt = 0.0
    if n > 1:
        gaps = sorted(t2 - t1 for t1, t2 in zip(times[:-1], times[1:])
                      if t2 > t1)
        if gaps:
            dt = gaps[len(gaps) // 2]
    if max_gap_s is None:
        max_gap_s = max(0.25, 2.5 * dt) if dt > 0 else 0.25

    def _valid(i: int) -> bool:
        x, y = xs[i], ys[i]
        return (
            x == x and y == y                     # not NaN
            and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
        )

    fixations: list[Fixation] = []
    i = 0
    while i < n:
        if not _valid(i):
            i += 1
            continue

        # Grow the initial window to cover min_duration
        j = i
        while (
            j + 1 < n
            and _valid(j + 1)
            and times[j + 1] - times[j] <= max_gap_s
            and times[j + 1] - times[i] < min_duration
        ):
            j += 1
        if times[j] - times[i] < min_duration * 0.5 and j - i < 2:
            i += 1
            continue

        window_x = xs[i:j + 1]
        window_y = ys[i:j + 1]
        dispersion = (max(window_x) - min(window_x)) + \
                     (max(window_y) - min(window_y))
        if dispersion > dispersion_threshold:
            i += 1
            continue

        # Window qualifies — extend while dispersion stays under threshold
        while j + 1 < n and _valid(j + 1) \
                and times[j + 1] - times[j] <= max_gap_s:
            new_x = window_x + [xs[j + 1]]
            new_y = window_y + [ys[j + 1]]
            d = (max(new_x) - min(new_x)) + (max(new_y) - min(new_y))
            if d > dispersion_threshold:
                break
            window_x, window_y = new_x, new_y
            j += 1

        if times[j] - times[i] >= min_duration \
                and (j - i + 1) >= min_samples:
            fixations.append(Fixation(
                t_start=float(times[i]),
                t_end=float(times[j]),
                nx=float(sum(window_x) / len(window_x)),
                ny=float(sum(window_y) / len(window_y)),
                n_samples=j - i + 1,
                dt_s=float(dt),
            ))
        i = j + 1

    return fixations


def detect_fixations_df(gaze_df: "Any", **kwargs) -> list[Fixation]:
    """Convenience wrapper for a pandas DataFrame with the standard
    ``video_time_s`` / ``gaze_video_nx`` / ``gaze_video_ny`` columns."""
    df = gaze_df.dropna(subset=["video_time_s"]).sort_values("video_time_s")
    return detect_fixations(
        df["video_time_s"].astype(float).tolist(),
        df["gaze_video_nx"].astype(float).tolist(),
        df["gaze_video_ny"].astype(float).tolist(),
        **kwargs,
    )
