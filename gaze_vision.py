# -*- coding: utf-8 -*-
"""
Gaze-annotated video frames for content-aware LLM feedback.

State-of-the-art frame selection (cf. GazeLog, ETRA 2025; GazeLLM,
AHs 2025): instead of sampling frames at fixed intervals, keyframes are
selected at DETECTED FIXATIONS — moments of sustained attention — and
annotated with the fixation centroid and duration. A multimodal LLM can
then literally SEE what the participant saw, where they were looking,
and for how long.

Per keyframe the pipeline produces:
* the full video frame with a gaze marker whose radius is scaled to the
  session's MEASURED validation error (not a hardcoded guess), plus a
  timestamp/duration label;
* a zoomed CROP centred on the gaze point ("dual granularity") — MLLMs
  are weak at precise spatial localization on full frames, and a crop of
  the attended region markedly improves object identification.

Fallback: if too few fixations are detected (very noisy recording), the
pipeline reverts to uniform temporal sampling with cluster-robust gaze
aggregation (densest-cluster medoid rather than an independent per-axis
median, which can land on a point the participant never looked at).
"""

import base64
from typing import Any

import cv2
import numpy as np

from fixations import Fixation, detect_fixations_df

# Half-width of the time window used in FALLBACK uniform sampling (s)
WINDOW_S = 0.5

# Minimum number of fixations before the fixation-based path is used
MIN_FIXATIONS = 3

# Crop size around the gaze point, as a fraction of the frame width
CROP_FRACTION = 0.35
CROP_OUTPUT_PX = 288


def _encode_jpeg(frame: "np.ndarray", quality: int = 72) -> "str | None":
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY),
                                           quality])
    if not ok:
        return None
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _draw_marker(frame: "np.ndarray", x: int, y: int,
                 radius_px: int) -> None:
    """Red gaze marker with a white outer ring (visible on any content).

    The radius communicates measurement uncertainty: it is scaled to the
    session's validation error, so the LLM (and any human reader) sees
    the area the gaze plausibly covers, not a false point estimate.
    """
    r = max(10, int(radius_px))
    cv2.circle(frame, (x, y), r + 4, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.circle(frame, (x, y), r, (0, 0, 255), 3, cv2.LINE_AA)
    cv2.circle(frame, (x, y), 3, (0, 0, 255), -1, cv2.LINE_AA)


def _label(frame: "np.ndarray", text: str) -> None:
    cv2.putText(frame, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(frame, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (255, 255, 255), 1, cv2.LINE_AA)


def _crop_around(frame: "np.ndarray", x: int, y: int) -> "np.ndarray":
    """Square crop centred on (x, y), clamped to the frame."""
    h, w = frame.shape[:2]
    half = max(32, int(w * CROP_FRACTION / 2))
    x0 = min(max(0, x - half), max(0, w - 2 * half))
    y0 = min(max(0, y - half), max(0, h - 2 * half))
    crop = frame[y0:y0 + 2 * half, x0:x0 + 2 * half]
    if crop.size == 0:
        return frame
    return cv2.resize(crop, (CROP_OUTPUT_PX, CROP_OUTPUT_PX))


def _read_frame(cap: "cv2.VideoCapture", t: float,
                frame_width: int) -> "np.ndarray | None":
    cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000.0)
    ok, frame = cap.read()
    if not ok:
        return None
    h, w = frame.shape[:2]
    scale = frame_width / w
    return cv2.resize(frame, (frame_width, max(1, int(h * scale))))


def _densest_cluster_medoid(
    xs: "list[float]", ys: "list[float]", radius: float = 0.08
) -> "tuple[float, float] | None":
    """Medoid of the densest gaze cluster in a sample window.

    Robust replacement for independent per-axis medians: with a bimodal
    window (gaze on two targets) the per-axis median can fall BETWEEN
    the targets — a location never actually looked at. The medoid is by
    construction an actually-observed sample.
    """
    pts = [(x, y) for x, y in zip(xs, ys) if x == x and y == y]
    if not pts:
        return None
    best, best_n = pts[0], -1
    for p in pts:
        n = sum(1 for q in pts
                if abs(q[0] - p[0]) < radius and abs(q[1] - p[1]) < radius)
        if n > best_n:
            best, best_n = p, n
    return best


def sample_gaze_frames(
    video_path: str,
    gaze_df: "Any",           # pandas.DataFrame with video_time_s / nx / ny
    max_frames: int = 12,
    frame_width: int = 512,
    error_px: "float | None" = None,
    with_crops: bool = True,
    draw_marker: bool = True,
) -> list[dict]:
    """Return gaze-annotated keyframes for the LLM pipeline.

    Args:
        video_path: Path to the stimulus MP4.
        gaze_df: Gaze samples with columns ``video_time_s``,
            ``gaze_video_nx``, ``gaze_video_ny`` (normalized 0–1).
        max_frames: Upper bound on the number of keyframes (API cost).
        frame_width: Output frame width in px (height keeps aspect).
        error_px: Session's measured validation error in SCREEN px;
            scales the marker radius. ``None`` → default radius.
        with_crops: Also return a zoomed crop around the gaze point.
        draw_marker: Draw the gaze marker (set False for the
            scene-description step of the prompt chain).

    Returns:
        List of dicts: ``{"t": float, "b64": str, "status": str,
        "duration_s": float | None, "crop_b64": str | None,
        "method": "fixation" | "uniform"}``.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("Cannot open video: %s" % video_path)

    try:
        duration = float(gaze_df["video_time_s"].max())
        if not np.isfinite(duration) or duration <= 0:
            raise RuntimeError("No usable gaze timeline for this recording")

        # Marker radius: scale measured screen-px error to output-frame px.
        # (Screen px → output px ≈ frame_width / on-screen video width;
        # unknown here, so a conservative 1/2.5 scale is applied. The
        # radius is a lower-bounded visual cue, not a measurement.)
        radius = 14 if error_px is None else int(
            np.clip(error_px / 2.5, 10, 60))

        # ── Preferred path: fixation-based keyframes (I-DT) ──
        fixations: list[Fixation] = []
        try:
            fixations = detect_fixations_df(gaze_df)
        except Exception:
            fixations = []

        frames: list[dict] = []
        if len(fixations) >= MIN_FIXATIONS:
            selected = fixations
            if len(selected) > max_frames:
                # Keep the longest fixations (sustained attention),
                # then restore temporal order.
                selected = sorted(selected, key=lambda f: -f.duration)
                selected = sorted(selected[:max_frames],
                                  key=lambda f: f.t_start)
            for fx in selected:
                frame = _read_frame(cap, fx.t_mid, frame_width)
                if frame is None:
                    continue
                h, w = frame.shape[:2]
                x, y = int(fx.nx * w), int(fx.ny * h)
                crop_b64 = _encode_jpeg(_crop_around(frame, x, y)) \
                    if with_crops else None
                if draw_marker:
                    _draw_marker(frame, x, y, radius)
                _label(frame, "t=%.1fs  fixation %dms"
                       % (fx.t_mid, int(fx.duration * 1000)))
                b64 = _encode_jpeg(frame)
                if b64 is None:
                    continue
                frames.append({
                    "t": round(fx.t_mid, 1),
                    "b64": b64,
                    "status": "fixation",
                    "duration_s": round(fx.duration, 2),
                    "crop_b64": crop_b64,
                    "method": "fixation",
                })
            if frames:
                return frames

        # ── Fallback: uniform sampling with cluster-robust aggregation ──
        n = int(np.clip(duration // 2 + 1, 4, max_frames))
        times = np.linspace(0.4, max(duration - 0.4, 0.4), n)
        for t in times:
            frame = _read_frame(cap, float(t), frame_width)
            if frame is None:
                continue
            h, w = frame.shape[:2]

            win = gaze_df[
                (gaze_df["video_time_s"] >= t - WINDOW_S)
                & (gaze_df["video_time_s"] <= t + WINDOW_S)
            ]
            status = "no gaze data"
            crop_b64 = None
            if len(win):
                pt = _densest_cluster_medoid(
                    win["gaze_video_nx"].astype(float).tolist(),
                    win["gaze_video_ny"].astype(float).tolist(),
                )
                if pt is not None:
                    nx, ny = pt
                    if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
                        x, y = int(nx * w), int(ny * h)
                        crop_b64 = _encode_jpeg(_crop_around(frame, x, y)) \
                            if with_crops else None
                        if draw_marker:
                            _draw_marker(frame, x, y, radius)
                        status = "gaze marked"
                    else:
                        status = "gaze off-video"

            _label(frame, "t=%.1fs" % t)
            b64 = _encode_jpeg(frame)
            if b64 is None:
                continue
            frames.append({
                "t": round(float(t), 1),
                "b64": b64,
                "status": status,
                "duration_s": None,
                "crop_b64": crop_b64,
                "method": "uniform",
            })
        return frames
    finally:
        cap.release()
