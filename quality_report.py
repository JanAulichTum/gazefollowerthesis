# -*- coding: utf-8 -*-
"""
Gaze data quality report.

Analyses raw GazeFollower session CSVs (data/gazefollower_raw/*.csv) and
quantifies tracking quality:

  * effective sampling rate & gaps
  * detection dropout (status == 0)
  * spatial precision: sample-to-sample jitter (horizontal vs vertical)
  * raw → filtered improvement, plus headroom from stronger smoothing
    (moving-median simulation)
  * VALIDATION results from the session manifest: pre/post accuracy &
    precision in degrees, calibration DRIFT (post − pre), pass/fail
    against the preregistered thresholds
  * per-stimulus FIXATION statistics (I-DT; count, median duration,
    fixation-time share)
  * CENTER-BIAS diagnostic: webcam gaze estimators regress toward the
    screen center when the eye signal is weak or calibration did not
    span the screen. Reports (a) corner-vs-center validation error and
    the INWARD component of corner errors, (b) gaze span vs screen
    size, (c) the distribution of gaze distance from screen center —
    and a verdict separating estimator compression from genuinely
    central attention.

Usage:  python quality_report.py [session.csv …]
        (no arguments = analyse every session in data/gazefollower_raw)
"""

import glob
import json
import os
import sys

import numpy as np
import pandas as pd

# ── Windows console encoding ──────────────────────────────────────────
# Windows defaults stdout to cp1252, and piping through a subprocess
# makes Python use it even on Python 3.12. A single non-ASCII character
# (≈, ✓, ≥) then raises UnicodeEncodeError and kills the whole run
# mid-report. Force UTF-8 so the output survives any console/pipe.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — older Python / exotic stream
    pass


try:
    from config import NOMINAL_SAMPLING_HZ, RATE_MULTIMODAL_RATIO
except ImportError:      # standalone use outside the project venv
    NOMINAL_SAMPLING_HZ, RATE_MULTIMODAL_RATIO = 30.0, 1.5

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE, "data", "gazefollower_raw")


def report_manifest(path: str) -> "dict | None":
    """Print validation & threshold results from the session manifest."""
    manifest_path = os.path.splitext(path)[0] + "_manifest.json"
    if not os.path.isfile(manifest_path):
        print("  (no manifest — validations unavailable for this session)")
        return None
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    thresholds = manifest.get("quality_thresholds", {})
    validations = manifest.get("validations", [])
    by_phase = {v.get("phase"): v for v in validations}
    for phase in ("pre", "post"):
        v = by_phase.get(phase)
        if not v:
            print(f"  validation ({phase}): MISSING")
            continue
        err_px = v.get("mean_err_px")
        err_deg = v.get("mean_err_deg")
        prec_px = v.get("mean_precision_px")
        prec_deg = v.get("mean_precision_deg")
        verdict = "PASS" if v.get("passes_threshold") else "FAIL"
        line = f"  validation ({phase}): accuracy "
        line += f"{err_px:.0f} px" if err_px is not None else "n/a"
        if err_deg is not None:
            line += f" ≈ {err_deg:.2f}°"
        if prec_px is not None:
            line += f" | precision {prec_px:.0f} px"
            if prec_deg is not None:
                line += f" ≈ {prec_deg:.2f}°"
        line += f"   [{verdict}"
        if thresholds.get("max_validation_error_deg") is not None:
            line += f" vs ≤ {thresholds['max_validation_error_deg']}°"
        print(line + "]")

    pre, post = by_phase.get("pre"), by_phase.get("post")
    if pre and post and pre.get("mean_err_deg") is not None \
            and post.get("mean_err_deg") is not None:
        drift = post["mean_err_deg"] - pre["mean_err_deg"]
        print(f"  calibration drift (post − pre): {drift:+.2f}°")

    for stim, q in (manifest.get("data_quality") or {}).items():
        verdict = "PASS" if q.get("passes_gaze_samples_threshold") else "FAIL"
        print(f"  gaze samples {stim}: {q.get('gaze_samples_pct', 0):.1f} % "
              f"[{verdict} vs ≥ {thresholds.get('min_gaze_samples_pct', '?')} %]")
        if q.get("sampling_hz"):
            flag = "LOW-RATE (frame decimation?)" if q.get(
                "low_sampling_rate") else "ok"
            print(f"  sampling rate {stim}: {q['sampling_hz']:.1f} Hz "
                  f"[{flag} vs ≥ {thresholds.get('min_sampling_hz', '?')} Hz]")
    return manifest


def report_fixations(df: pd.DataFrame, manifest: "dict | None") -> None:
    """Per-stimulus fixation statistics (needs video_rect from manifest)."""
    if not manifest:
        return
    try:
        from fixations import detect_fixations_df
    except ImportError:
        return
    for entry in manifest.get("stimuli", []):
        vr = entry.get("video_rect") or {}
        if not (vr.get("w") and vr.get("h")):
            continue
        seg = df[
            (df["timestamp"] >= entry["t_start_ns"])
            & (df["timestamp"] <= entry["t_end_ns"])
        ].copy()
        if seg.empty:
            continue
        seg["video_time_s"] = (seg["timestamp"] - entry["t_start_ns"]) / 1e9
        seg["gaze_video_nx"] = (
            seg["filtered_gaze_position_x"] - vr["x"]) / vr["w"]
        seg["gaze_video_ny"] = (
            seg["filtered_gaze_position_y"] - vr["y"]) / vr["h"]
        try:
            fx = detect_fixations_df(seg)
        except Exception:
            continue
        dur_s = (entry["t_end_ns"] - entry["t_start_ns"]) / 1e9
        if fx:
            durs = sorted(f.duration for f in fx)
            dwell = sum(durs)
            print(f"  fixations {entry['stimulus']}: n={len(fx)}, "
                  f"median {durs[len(durs)//2]*1000:.0f} ms, "
                  f"fixation time {100*dwell/dur_s:.0f} % of {dur_s:.0f} s")
        else:
            print(f"  fixations {entry['stimulus']}: none detected "
                  f"(noisy recording?)")


def rms(series: np.ndarray) -> float:
    """Root-mean-square of sample-to-sample differences (jitter)."""
    d = np.diff(series)
    return float(np.sqrt(np.mean(d ** 2))) if len(d) else float("nan")


def analyse(path: str) -> None:
    df = pd.read_csv(path)
    name = os.path.basename(path)
    print("\n" + "=" * 72)
    print("SESSION:", name)

    n = len(df)
    dur = (df.timestamp.max() - df.timestamp.min()) / 1e9
    rate = n / dur if dur else float("nan")
    gaps = np.diff(df.timestamp.values) / 1e9
    print(f"  samples: {n}   duration: {dur:.1f} s   rate: {rate:.1f} Hz")
    print(f"  frame gaps: median {np.median(gaps)*1000:.0f} ms, "
          f"p95 {np.percentile(gaps, 95)*1000:.0f} ms, "
          f"max {gaps.max()*1000:.0f} ms")

    lost = (df.status == 0).mean() * 100
    print(f"  detection dropout (status=0): {lost:.1f} %")

    # ── Rate stability over time: distinguish a camera that CAN'T keep up
    # (consistently low) from auto-exposure THROTTLING in low light (starts
    # high, then drops and stays low). The latter is a lighting problem,
    # not a CPU problem. ──
    tsec = (df.timestamp.values.astype(float) - df.timestamp.min()) / 1e9
    win = 10.0
    rates = []
    w = 0.0
    while w < tsec[-1]:
        mask = (tsec >= w) & (tsec < w + win)
        if mask.sum() > 3:
            gg = np.diff(df.timestamp.values[mask].astype(float)) / 1e9
            if len(gg):
                rates.append((w, 1.0 / np.median(gg)))
        w += win
    if len(rates) >= 3:
        rs = [r for _, r in rates]
        peak, med = max(rs), float(np.median(rs))
        # first-window rate vs the sustained median
        first = rs[0]
        if peak >= 25 and med < 0.75 * peak:
            # Locate the drop: the first window that falls below 75 % of
            # the peak. WHEN it happens discriminates the two causes.
            drop_at = next((w for w, r in rates if r < 0.75 * peak), None)
            recovered = any(r > 0.9 * peak
                            for w, r in rates
                            if drop_at is not None and w > drop_at)
            print(f"  rate STABILITY: starts {first:.0f} Hz, peak {peak:.0f} Hz, "
                  f"sustained median {med:.0f} Hz"
                  + (f", drops at t≈{drop_at:.0f} s" if drop_at is not None
                     else ""))
            if drop_at is not None and drop_at <= 40 and not recovered:
                print("    -> CPU THROTTLING (drop early and PERMANENT): the "
                      "rate holds at the peak only for the processor's turbo "
                      "window, then halves and never recovers. That is the "
                      "signature of a sustained-power/thermal limit on the "
                      "per-frame gaze inference — NOT lighting (room light "
                      "does not change abruptly at one moment and stay "
                      "changed) and NOT the camera.")
                print("       Fixes, in order of effect: run on AC power "
                      "with Low Power Mode off; close other CPU load "
                      "(especially browsers/video calls); verify the process "
                      "is native, not Rosetta (python tracker_service.py "
                      "--check); then either accept the SUSTAINED rate and "
                      "preregister the threshold at that value, or move to "
                      "a machine that holds the peak.")
                print("       IMPORTANT for study design: if calibration "
                      "runs inside the turbo window and the stimuli after "
                      "it, the two are recorded at different rates. Warm the "
                      "machine up before calibrating so both happen at the "
                      "sustained rate.")
            else:
                print("    -> VARIABLE rate. If it tracks the room light it "
                      "is webcam auto-exposure throttling (add front light, "
                      "disable auto-exposure); if it tracks CPU load it is "
                      "compute. Run camera_fps_test.py (camera only) and "
                      "tracker_fps_test.py (camera + inference) to separate "
                      "them: a camera that sustains ~30 FPS while the "
                      "tracker does not means inference is the limiter.")
        else:
            print(f"  rate STABILITY: {first:.0f} Hz start, {med:.0f} Hz "
                  f"median, {peak:.0f} Hz peak (stable)")

    # Tobii-style "gaze samples": valid samples / expected at the NOMINAL
    # rate. The denominator must be a fixed constant — deriving it from
    # this recording's own median interval makes the metric circular
    # (every session scores ~100 %, and frame decimation, the dominant
    # loss mechanism here, cancels out of both numerator and denominator).
    median_dt = float(np.median(gaps))
    nominal_dt = 1.0 / NOMINAL_SAMPLING_HZ if NOMINAL_SAMPLING_HZ else 0.0
    expected = dur / nominal_dt if nominal_dt > 0 else 0
    valid = int((df.status == 1).sum())
    if expected:
        print(f"  gaze samples (Tobii-style): "
              f"{min(100.0, 100 * valid / expected):.1f} % "
              f"({valid} valid / ~{expected:.0f} expected at "
              f"{NOMINAL_SAMPLING_HZ:.0f} Hz nominal)")
    if median_dt > 0:
        rel = dur / median_dt
        print(f"  relative yield (vs this session's own median rate): "
              f"{min(100.0, 100 * valid / rel):.1f} %  "
              f"— diagnostic only, NOT a data-loss figure")
    # Rate shape: report the fastest sustained rate next to the median so
    # a decimated/bimodal recording is not summarised by one Hz.
    dt_fast = float(np.quantile(gaps, 0.10))
    if dt_fast > 0:
        ratio = median_dt / dt_fast
        print(f"  rate shape: median {1/median_dt:.1f} Hz, fastest "
              f"{1/dt_fast:.1f} Hz (x{ratio:.2f})"
              + ("  -> MULTIMODAL: alternates between full and decimated "
                 "rate; fixation timing is biased in a time-varying way"
                 if ratio >= RATE_MULTIMODAL_RATIO else ""))

    ok = df[df.status == 1]
    if len(ok) < 10:
        print("  too few valid samples for precision analysis")
        return

    fx = ok.filtered_gaze_position_x.values
    fy = ok.filtered_gaze_position_y.values
    cx = ok.calibrated_gaze_position_x.values
    cy = ok.calibrated_gaze_position_y.values

    print(f"  jitter RMS  (calibrated): x {rms(cx):6.1f} px | y {rms(cy):6.1f} px")
    print(f"  jitter RMS  (filtered)  : x {rms(fx):6.1f} px | y {rms(fy):6.1f} px")

    # Headroom simulation: rolling median (k=5 ≈ 350 ms at ~14 Hz)
    med_x = pd.Series(fx).rolling(5, center=True, min_periods=1).median().values
    med_y = pd.Series(fy).rolling(5, center=True, min_periods=1).median().values
    print(f"  jitter RMS  (median-5 simulation): "
          f"x {rms(med_x):6.1f} px | y {rms(med_y):6.1f} px")

    # Range usage: does the estimate span the screen sensibly?
    print(f"  x span: [{np.percentile(fx, 2):.0f}, {np.percentile(fx, 98):.0f}] px"
          f"   y span: [{np.percentile(fy, 2):.0f}, {np.percentile(fy, 98):.0f}] px")

    # Vertical-vs-horizontal noise ratio (values > 1 = vertical is noisier)
    ratio = rms(fy) / rms(fx) if rms(fx) else float("nan")
    print(f"  vertical/horizontal noise ratio: {ratio:.2f}")

    # ── Validation, drift, thresholds, fixations (manifest-based) ──
    manifest = report_manifest(path)
    report_fixations(df, manifest)
    report_center_bias(df, manifest)


def _screen_dims(manifest: "dict | None") -> "tuple[float, float, str] | None":
    """Best available screen size in logical px: (w, h, source)."""
    if manifest:
        for v in manifest.get("validations", []):
            scr = v.get("screen") or {}
            if scr.get("width_px") and scr.get("height_px"):
                return float(scr["width_px"]), float(scr["height_px"]), \
                    "validation record"
        # Approximation from the fullscreen video rect (content is
        # centred, so screen ≈ content + both letterbox margins).
        best = None
        for entry in manifest.get("stimuli", []):
            vr = entry.get("video_rect") or {}
            if vr.get("w") and vr.get("h"):
                w = vr["w"] + 2 * max(0.0, vr.get("x", 0.0))
                h = vr["h"] + 2 * max(0.0, vr.get("y", 0.0))
                if best is None or w * h > best[0] * best[1]:
                    best = (w, h)
        if best:
            return best[0], best[1], "approx. from video rect"
    return None


def report_center_bias(df: pd.DataFrame, manifest: "dict | None") -> None:
    """Diagnose 'gaze stuck in the middle': estimator compression vs.
    genuinely central attention."""
    dims = _screen_dims(manifest)
    findings: list[str] = []
    print("  center-bias diagnostic:")

    # ── (a) Corner-vs-center validation error + inward bias ──────────
    # Compression signature: corner targets show larger error than the
    # central target AND the error points INWARD (toward screen center).
    pre = None
    if manifest:
        for v in manifest.get("validations", []):
            if v.get("phase") == "pre":
                pre = v          # keep the LAST (most recent) pre check
    if pre and dims:
        sw, sh, _src = dims
        cx, cy = sw / 2, sh / 2
        corners, center = [], []
        for t in pre.get("targets", []):
            if t.get("err_px") is None:
                continue
            d_center = np.hypot(t["tx"] - cx, t["ty"] - cy)
            (corners if d_center > 0.25 * np.hypot(sw, sh) else
             center).append(t)
        if corners and center:
            corner_err = float(np.mean([t["err_px"] for t in corners]))
            center_err = float(np.mean([t["err_px"] for t in center]))
            inward = []
            for t in corners:
                if t.get("mx") is None:
                    continue
                to_center = np.array([cx - t["tx"], cy - t["ty"]])
                to_center = to_center / (np.linalg.norm(to_center) or 1.0)
                err_vec = np.array([t["mx"] - t["tx"], t["my"] - t["ty"]])
                inward.append(float(np.dot(err_vec, to_center)))
            inward_px = float(np.mean(inward)) if inward else float("nan")
            print(f"    corner error {corner_err:.0f} px vs center error "
                  f"{center_err:.0f} px | inward component "
                  f"{inward_px:+.0f} px")
            if corner_err > 1.5 * center_err and inward_px > 0.4 * corner_err:
                findings.append(
                    "corner validation errors point inward → "
                    "calibration/estimator COMPRESSION")
    else:
        print("    (no pre-validation in manifest — corner check skipped; "
              "legacy session?)")

    ok = df[df.status == 1] if "status" in df.columns else df
    if len(ok) < 10 or not dims:
        if not dims:
            print("    (screen size unknown — span/radial checks skipped)")
        _center_verdict(findings)
        return
    sw, sh, src = dims
    fx = ok.filtered_gaze_position_x.values
    fy = ok.filtered_gaze_position_y.values

    # ── (b) Gaze span vs screen ──────────────────────────────────────
    span_x = (np.percentile(fx, 95) - np.percentile(fx, 5)) / sw
    span_y = (np.percentile(fy, 95) - np.percentile(fy, 5)) / sh
    print(f"    gaze span (p5–p95): {100*span_x:.0f} % of screen width, "
          f"{100*span_y:.0f} % of height  [screen {sw:.0f}x{sh:.0f} px, "
          f"{src}]")
    if span_x < 0.45 and span_y < 0.45:
        findings.append("gaze span < 45 % of the screen on both axes → "
                        "range COMPRESSION")

    # ── (c) Distance from screen center ──────────────────────────────
    # Normalized so 1.0 = screen edge (short axis); uniform viewing of a
    # fullscreen video yields a median around ~0.5.
    r = np.hypot((fx - sw / 2) / (sw / 2), (fy - sh / 2) / (sh / 2))
    print(f"    distance from center: median {np.median(r):.2f}, "
          f"p90 {np.percentile(r, 90):.2f} "
          f"(1.0 = screen edge; uniform viewing ≈ 0.5 median)")
    if np.median(r) < 0.30 and not findings:
        findings.append("strongly central gaze WITHOUT compression "
                        "markers → plausibly genuine central attention "
                        "(compare across stimuli/participants)")

    _center_verdict(findings)


def _center_verdict(findings: "list[str]") -> None:
    if findings:
        for f in findings:
            print("    ⚠ " + f)
    else:
        print("    ✓ no compression markers")


def main() -> None:
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(RAW_DIR, "*.csv")))
    if not paths:
        print("No session CSVs found in", RAW_DIR)
        return
    for p in paths:
        try:
            analyse(p)
        except Exception as exc:  # noqa: BLE001
            print("  ! failed to analyse %s: %s" % (p, exc))
    print("\nInterpretation guide:")
    print("  rate      : webcam trackers deliver ~10–30 Hz (hardware-bound)")
    print("  dropout   : > 10 % suggests lighting/positioning problems")
    print("  jitter    : RMS < ~40 px is decent for a webcam tracker;")
    print("              vertical/horizontal ratio > 1.5 = weak vertical axis")
    print("  median-5  : if clearly lower than 'filtered', stronger smoothing")
    print("              would still help (at the cost of ~0.2 s lag)")


if __name__ == "__main__":
    main()
