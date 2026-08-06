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

# Literature default for I-DT (Salvucci & Goldberg 2000 and after use
# ~100 ms). Used as the LOWER bound of the derived minimum duration;
# the sampling rate raises it when three samples take longer than this.
PREFERRED_MIN_DURATION_S = 0.10


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
    min_duration: "float | None" = None,
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
        min_duration: Minimum fixation duration in seconds. ``None`` →
            DERIVED from the measured rate as max(100 ms, min_samples /
            sampling_hz).

            WHY DERIVED RATHER THAN FIXED. The configured default was a
            flat 200 ms, which is roughly double the ~100 ms used in the
            I-DT literature and therefore discards genuine short
            fixations — contributing directly to the measured 1.1 /s
            fixation rate against the 3–4 /s of natural viewing.
            But it cannot simply be lowered either: a dispersion window
            needs at least ``min_samples`` points to mean anything, which
            at 21 Hz is 143 ms and at 30 Hz is 100 ms. The floor is a
            property of the recording, not a preference, so it is
            computed per segment and reported alongside the result.
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

    # Rate-derived minimum duration (see the docstring). The floor is
    # min_samples x the inter-sample interval; below that a "fixation"
    # would rest on too few points for its dispersion to be meaningful.
    if min_duration is None:
        floor = min_samples * dt if dt > 0 else 0.0
        min_duration = max(PREFERRED_MIN_DURATION_S, floor)

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

# ──────────────────────────────────────────────────────────────────
# Saccades
# ──────────────────────────────────────────────────────────────────

import math  # noqa: E402  (kept local to the saccade section)

def saccade_metrics(fixations: list, px_per_deg: float,
                    screen_w_px: int, screen_h_px: int) -> dict:
    """Inter-fixation transitions, derived from the fixation sequence.

    HONEST LIMITATION, to be reported with the numbers: at 21-30 Hz the
    saccade itself is not observed — typically no sample falls during it.
    What is measured is the DISPLACEMENT between consecutive fixation
    centroids. That is a valid amplitude estimate, but velocity, peak
    velocity and duration are NOT recoverable at this sampling rate and
    must not be reported.

    Fixations that I-DT merged also remove the saccade between them, so
    the count is biased DOWN by the same mechanism that biases the
    fixation count.
    """
    if len(fixations) < 2:
        return {"saccade_count": 0, "amplitudes_deg": [],
                "note": "fewer than two fixations"}
    amps = []
    for a, b in zip(fixations[:-1], fixations[1:]):
        dx = (b["nx"] - a["nx"]) * screen_w_px
        dy = (b["ny"] - a["ny"]) * screen_h_px
        amps.append(math.hypot(dx, dy) / px_per_deg if px_per_deg else 0.0)
    amps_sorted = sorted(amps)
    n = len(amps_sorted)
    return {
        "saccade_count": n,
        "amplitude_median_deg": round(amps_sorted[n // 2], 2),
        "amplitude_mean_deg": round(sum(amps_sorted) / n, 2),
        "amplitude_p90_deg": round(amps_sorted[min(n - 1, int(0.9 * n))], 2),
        "amplitude_min_deg": round(amps_sorted[0], 2),
        "amplitude_max_deg": round(amps_sorted[-1], 2),
        "measurement_note": "Amplitude is the displacement between "
                            "consecutive fixation centroids. Saccade "
                            "velocity and duration are NOT measurable at "
                            "this sampling rate and are not reported.",
    }
