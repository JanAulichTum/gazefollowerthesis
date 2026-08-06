# -*- coding: utf-8 -*-
"""
Locate WHY the sampling rate is below the camera rate — stage by stage.

`tracker_fps_test.py` tells you the rate. This tells you where the time
GOES, by instrumenting the REAL GazeFollower pipeline in-process:

    capture  -> BGR2RGB convert -> resize -> FaceMesh -> gaze CNN -> callback

It wraps ``face_alignment.detect`` and ``gaze_estimator.detect`` with
timers and measures the full per-frame callback around them, so the
residual (callback total minus the two models) is the cost of everything
GazeFollower does itself: colour conversion, the software resize, patch
clipping, the calibration predict, the filter, and the per-sample CSV
write+flush.

It also reports the resolution the camera ACTUALLY delivers, which is the
crux of one hypothesis: GazeFollower applies its capture settings to an
unopened VideoCapture, so they do nothing and frames may arrive at 720p
or 1080p and be downscaled in software every frame (see camera_patch.py).

SCENARIOS
  1. idle        — sampling only, nothing else running
  2. polled      — same, while a thread polls get_gaze_info() at the rate
                   the browser's live preview does. Tests whether the
                   preview's IPC/GIL traffic steals time from the capture
                   thread. If the rate drops here, the preview is the
                   problem, not the models.
  3. camerafix   — scenario 1 with GF_CAMERA_FIX=1 (native 640x480)

Usage::

    python diagnose_rate.py                 # all scenarios, 20 s each
    python diagnose_rate.py --seconds 30
    python diagnose_rate.py --only idle

Stop the experiment server first — only one process can own the webcam.
No calibration needed (the calibration step is stubbed; see
tracker_fps_test.py for why).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time

# ── Windows console encoding ──────────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BUDGET_MS = 1000.0 / 30.0


def _stats(values: list) -> dict:
    if not values:
        return {"n": 0, "median": float("nan"), "p90": float("nan")}
    s = sorted(values)
    return {
        "n": len(s),
        "median": statistics.median(s),
        "p90": s[int(0.90 * (len(s) - 1))],
    }


def run_scenario(name: str, seconds: float, poll: bool,
                 camera_fix: bool) -> "dict | None":
    """Measure one scenario. Returns timings, or None if setup failed."""
    os.environ["GF_CAMERA_FIX"] = "1" if camera_fix else ""

    try:
        from gazefollower import GazeFollower
    except Exception as exc:  # noqa: BLE001
        print("  Could not import GazeFollower:", exc)
        return None

    kwargs = {}
    try:
        from camera_patch import make_camera

        cam = make_camera()
        if cam is not None:
            kwargs["camera"] = cam
    except Exception:  # noqa: BLE001
        pass

    try:
        gf = GazeFollower(**kwargs)
    except Exception as exc:  # noqa: BLE001
        print("  GazeFollower init failed:", exc)
        print("  (is the experiment server still running? it owns the camera)")
        return None

    # Calibration is never persisted by GazeFollower, so process_frame
    # would raise on every frame. Stub it — timing is unaffected.
    calib = getattr(gf, "calibration", None)
    if calib is not None and not getattr(calib, "has_calibrated", False):
        calib.predict = lambda features, estimated: (True, estimated)
        calib.has_calibrated = True

    face_ms: list = []
    gaze_ms: list = []
    frame_ms: list = []
    arrivals: list = []
    frame_shape = {"value": None}
    # Detection outcomes. THIS is usually the real story: GazeFollower
    # drops every frame whose detection failed (it raises in
    # _write_sample), so the RECORDED rate is the success rate, not the
    # capture rate. A pipeline capturing a healthy 31 Hz with 40 %
    # failures records as ~18 Hz and looks like a performance problem.
    outcomes = {"frames": 0, "no_face": 0, "no_gaze": 0, "ok": 0}

    # ── Wrap the two model stages ──
    fa, ge = gf.face_alignment, gf.gaze_estimator
    orig_face, orig_gaze = fa.detect, ge.detect

    def timed_face(timestamp, frame):
        if frame_shape["value"] is None:
            frame_shape["value"] = getattr(frame, "shape", None)
        t0 = time.perf_counter()
        try:
            return orig_face(timestamp, frame)
        finally:
            face_ms.append((time.perf_counter() - t0) * 1000)

    def timed_gaze(image, face_info):
        t0 = time.perf_counter()
        info = None
        try:
            info = orig_gaze(image, face_info)
            return info
        finally:
            gaze_ms.append((time.perf_counter() - t0) * 1000)
            outcomes["frames"] += 1
            if not getattr(face_info, "status", False) or \
                    not getattr(face_info, "can_gaze_estimation", False):
                outcomes["no_face"] += 1
            elif not getattr(info, "status", False):
                outcomes["no_gaze"] += 1
            else:
                outcomes["ok"] += 1

    fa.detect = timed_face
    ge.detect = timed_gaze

    # ── Wrap the whole per-frame callback ──
    orig_process = gf.process_frame

    def timed_process(state, timestamp, frame):
        t0 = time.perf_counter()
        try:
            return orig_process(state, timestamp, frame)
        finally:
            frame_ms.append((time.perf_counter() - t0) * 1000)
            arrivals.append(time.perf_counter())

    gf.process_frame = timed_process
    gf.camera.set_on_image_callback(timed_process)

    stop = threading.Event()
    polls: list = []

    def poller():
        """Mimic the browser preview loop: poll gaze_info every 150 ms."""
        while not stop.is_set():
            t0 = time.perf_counter()
            gf.get_gaze_info()
            polls.append((time.perf_counter() - t0) * 1000)
            stop.wait(0.15)

    try:
        gf.start_sampling()
        if poll:
            threading.Thread(target=poller, daemon=True).start()
        time.sleep(2.0)                       # settle
        face_ms.clear(); gaze_ms.clear(); frame_ms.clear(); arrivals.clear()
        for k in outcomes:
            outcomes[k] = 0
        time.sleep(seconds)
        stop.set()
    finally:
        try:
            gf.stop_sampling()
        except Exception:  # noqa: BLE001
            pass
        try:
            gf.release()
        except Exception:  # noqa: BLE001
            pass

    if len(arrivals) < 5:
        print("  Too few frames (%d) — was a face visible and lit?"
              % len(arrivals))
        return None

    gaps = [(b - a) * 1000 for a, b in zip(arrivals[:-1], arrivals[1:])]
    f, g, fr = _stats(face_ms), _stats(gaze_ms), _stats(frame_ms)
    residual = fr["median"] - f["median"] - g["median"]
    span = arrivals[-1] - arrivals[0]
    return {
        "name": name,
        "hz_median": 1000.0 / statistics.median(gaps) if gaps else 0.0,
        "hz_overall": (len(arrivals) - 1) / span if span > 0 else 0.0,
        "frame_shape": frame_shape["value"],
        "face": f, "gaze": g, "frame": fr,
        "residual_ms": residual,
        "poll_ms": _stats(polls) if polls else None,
        "outcomes": dict(outcomes),
    }


def report(r: dict) -> None:
    print()
    print("-" * 70)
    print("  SCENARIO: %s" % r["name"])
    print("-" * 70)
    shape = r["frame_shape"]
    if shape is not None and len(shape) >= 2:
        print("  camera delivers : %dx%d px%s"
              % (shape[1], shape[0],
                 "  <- NOT 640x480: every frame is resized in software"
                 if (shape[1], shape[0]) != (640, 480) else ""))
    print("  achieved rate   : %.1f Hz (median interval), %.1f Hz overall"
          % (r["hz_median"], r["hz_overall"]))
    print()
    print("  PER-FRAME COST (median | p90), budget %.1f ms at 30 fps"
          % BUDGET_MS)
    print("    MediaPipe FaceMesh   %6.1f | %6.1f ms"
          % (r["face"]["median"], r["face"]["p90"]))
    print("    gaze CNN (MNN)       %6.1f | %6.1f ms"
          % (r["gaze"]["median"], r["gaze"]["p90"]))
    print("    everything else      %6.1f ms   <- convert + resize + clip +"
          % r["residual_ms"])
    print("                                       calibrate + filter +")
    print("                                       CSV write/flush")
    print("    ----------------------------------")
    print("    TOTAL per frame      %6.1f | %6.1f ms"
          % (r["frame"]["median"], r["frame"]["p90"]))
    over = r["frame"]["median"] - BUDGET_MS
    if over > 0:
        print("    -> %.1f ms OVER budget: the capture loop misses every "
              "other frame" % over)
    else:
        print("    -> %.1f ms under budget" % (-over))
    if r["poll_ms"]:
        print("  preview polls        %6.1f ms median (%d calls)"
              % (r["poll_ms"]["median"], r["poll_ms"]["n"]))

    o = r.get("outcomes") or {}
    n = o.get("frames", 0)
    if n:
        ok_pct = 100.0 * o["ok"] / n
        print()
        print("  DETECTION OUTCOMES (%d frames captured)" % n)
        print("    gaze estimated       %5d  (%.1f %%)" % (o["ok"], ok_pct))
        print("    no face / not usable %5d  (%.1f %%)"
              % (o["no_face"], 100.0 * o["no_face"] / n))
        print("    face but no gaze     %5d  (%.1f %%)"
              % (o["no_gaze"], 100.0 * o["no_gaze"] / n))
        if o["ok"] < n:
            print("    -> RECORDED rate would be %.1f Hz, not the %.1f Hz "
                  "captured:" % (r["hz_median"] * ok_pct / 100.0,
                                 r["hz_median"]))
            print("       unpatched GazeFollower DROPS every failed frame "
                  "(it raises")
            print("       in _write_sample). sample_patch.py records them "
                  "with status=0.")
        else:
            print("    -> no losses: recorded rate == captured rate "
                  "(%.1f Hz)." % r["hz_median"])
            print("       NOTE: this is the BEST case — you were facing the")
            print("       camera. Detection during real video-watching is "
                  "the")
            print("       number that matters; read valid_pct from a real "
                  "session.")


def verdict(results: dict) -> None:
    print()
    print("=" * 70)
    print("  WHERE THE TIME GOES")
    print("=" * 70)
    idle = results.get("idle")
    if not idle:
        print("  (baseline scenario did not run — nothing to conclude)")
        return

    total = idle["frame"]["median"]

    # Answer the real question first: is this a SPEED problem or a
    # DETECTION problem? They look identical in the recorded data,
    # because failed frames are dropped rather than written.
    o = idle.get("outcomes") or {}
    n = o.get("frames", 0)
    if n:
        ok_pct = 100.0 * o["ok"] / n
        if total <= BUDGET_MS and ok_pct < 85:
            print("  *** NOT A SPEED PROBLEM. *** The pipeline runs at "
                  "%.1f ms/frame," % total)
            print("  comfortably inside the %.1f ms budget, and captures "
                  "%.1f Hz." % (BUDGET_MS, idle["hz_median"]))
            print("  But only %.1f %% of frames yield a gaze estimate, and "
                  "GazeFollower" % ok_pct)
            print("  DROPS the rest (it raises in _write_sample), so the "
                  "session records")
            print("  ~%.1f Hz. Chasing CPU/GPU here would achieve nothing."
                  % (idle["hz_median"] * ok_pct / 100.0))
            print()
            if o["no_face"] > o["no_gaze"]:
                print("  The failures are FACE/LANDMARK failures, so fix the "
                      "capture conditions:")
                print("    - camera at eye level, face filling more of the "
                      "frame (sit closer, ~60 cm)")
                print("    - bright, even light ON YOUR FACE (not behind "
                      "you); avoid backlight")
                print("    - no strong glasses glare; keep head roll small")
                print("    - use the app's position guide before calibrating")
            else:
                print("  Faces are found but gaze estimation rejects them — "
                      "usually eye")
                print("  crops too small/dark, or eyes near-closed. Move "
                      "closer and add")
                print("  front lighting, then re-run.")
            print()

    parts = [("MediaPipe FaceMesh", idle["face"]["median"]),
             ("gaze CNN", idle["gaze"]["median"]),
             ("GazeFollower overhead", idle["residual_ms"])]
    parts.sort(key=lambda p: -p[1])
    print("  Dominant cost: %s at %.1f ms of %.1f ms total (%.0f %%)."
          % (parts[0][0], parts[0][1], total, 100 * parts[0][1] / total
             if total else 0))

    shape = idle["frame_shape"]
    if shape is not None and len(shape) >= 2 and (shape[1], shape[0]) != (640, 480):
        print("  The camera is delivering %dx%d, not 640x480 — GazeFollower's"
              % (shape[1], shape[0]))
        print("  capture settings are applied before the device is opened and")
        print("  therefore do nothing. Every frame is converted and resized in")
        print("  software inside the capture loop. Try GF_CAMERA_FIX=1.")

    polled = results.get("polled")
    if polled:
        drop = idle["hz_median"] - polled["hz_median"]
        if drop > 0.15 * idle["hz_median"]:
            print("  POLLING COSTS %.1f Hz (%.1f -> %.1f). The live gaze"
                  % (drop, idle["hz_median"], polled["hz_median"]))
            print("  preview competes with the capture thread. Reduce its")
            print("  frequency, or stop it while recording.")
        else:
            print("  The live preview costs ~%.1f Hz — not the problem." % drop)

    fix = results.get("camerafix")
    if fix and idle:
        gain = fix["hz_median"] - idle["hz_median"]
        if gain > 0.15 * idle["hz_median"]:
            print("  GF_CAMERA_FIX=1 GAINS %.1f Hz (%.1f -> %.1f). Adopt it:"
                  % (gain, idle["hz_median"], fix["hz_median"]))
            print("      setx GF_CAMERA_FIX 1        (Windows, persistent)")
            print("  and record the change in the methods section.")
        else:
            print("  GF_CAMERA_FIX changes the rate by %.1f Hz — not the "
                  "answer here." % gain)

    print()
    print("  Budget is %.1f ms per frame at 30 fps. Anything above it makes"
          % BUDGET_MS)
    print("  the loop skip frames, which is why the rate halves rather than")
    print("  degrading smoothly.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--only", choices=["idle", "polled", "camerafix"],
                    help="run a single scenario")
    args = ap.parse_args()

    scenarios = [
        ("idle", dict(poll=False, camera_fix=False)),
        ("polled", dict(poll=True, camera_fix=False)),
        ("camerafix", dict(poll=False, camera_fix=True)),
    ]
    if args.only:
        scenarios = [s for s in scenarios if s[0] == args.only]

    print("=" * 70)
    print("  PER-STAGE RATE DIAGNOSIS")
    print("=" * 70)
    # Fail early and clearly if the project venv is not active — the bare
    # "No module named 'gazefollower'" sends people reinstalling packages
    # that were never broken.
    try:
        from env_check import require

        require("gazefollower", "MNN", "mediapipe", "cv2")
    except ImportError:
        pass                      # env_check missing → let imports fail later

    print("  %d scenario(s), %.0f s each (plus 2 s settle)."
          % (len(scenarios), args.seconds))
    print("  Sit in front of the camera, normally lit, and stay still.")

    results = {}
    for name, kw in scenarios:
        print("\n>>> running '%s'…" % name, flush=True)
        r = run_scenario(name, args.seconds, **kw)
        if r:
            results[name] = r
            report(r)
        time.sleep(1.0)          # let the camera settle between scenarios

    if results:
        verdict(results)
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
