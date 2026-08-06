# -*- coding: utf-8 -*-
"""
Live webcam frame-rate + brightness meter.

Purpose: prove (or rule out) that a low sampling rate is caused by the
camera's **auto-exposure throttling in low light** rather than the
computer. Consumer webcams lengthen their exposure in dim scenes and
drop the frame rate to fit it, so delivered FPS falls as the image gets
darker. Watch the two numbers move together as you change the lighting.

Usage:
    python camera_fps_test.py               # live window + console
    python camera_fps_test.py --no-window   # console only (headless/SSH)
    python camera_fps_test.py --seconds 30  # auto-stop after 30 s
    python camera_fps_test.py --camera 1    # use a different camera index

IMPORTANT: stop the experiment server first — only one process can own
the webcam at a time. This tool opens the camera DIRECTLY (it does not
go through GazeFollower), so it measures exactly what the camera
delivers. It does NOT run any gaze inference — so a high FPS here with a
low rate in the actual recording means GazeFollower's per-frame
deep-learning pipeline (not the camera) is the limiter. Use
`tracker_fps_test.py` to measure GazeFollower's real sample rate and
compare.

Note: a fully black image (brightness < ~10, e.g. a covered lens) does
NOT exercise auto-exposure — point the camera at a normally-lit scene
and vary the room light to test whether lighting affects the frame rate.

Press  q  or  Esc  in the window (or Ctrl-C in the console) to stop.
A summary (min/median/max FPS, brightness range, verdict) prints at exit.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque

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



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", type=int, default=0, help="camera index")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="auto-stop after N seconds (0 = until quit)")
    ap.add_argument("--no-window", action="store_true",
                    help="console only, no preview window")
    args = ap.parse_args()

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        print("Missing dependency:", exc)
        print("Install with:  pip install opencv-python numpy")
        return 1

    # On Windows, DirectShow exposes exposure/fps more reliably than the
    # default MSMF backend; fall back if it fails.
    cap = None
    if sys.platform.startswith("win"):
        cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
        if not cap or not cap.isOpened():
            cap = None
    if cap is None:
        cap = cv2.VideoCapture(args.camera)
    if not cap or not cap.isOpened():
        print("Could not open camera %d. Is the experiment server still "
              "running (it owns the webcam)? Close it and retry." % args.camera)
        return 1

    reported = cap.get(cv2.CAP_PROP_FPS)
    print("Camera %d opened. Camera-reported FPS setting: %s"
          % (args.camera, ("%.0f" % reported) if reported and reported > 0
             else "unknown"))
    print("Watch FPS vs brightness. Darken the room -> FPS should drop; "
          "add front light -> FPS should rise. Ctrl-C or q to stop.\n")

    times: "deque[float]" = deque(maxlen=60)   # timestamps of recent frames
    all_fps: list[float] = []
    all_bri: list[float] = []
    t_start = time.time()
    last_print = 0.0
    window = "Camera FPS / brightness meter  (q or Esc to quit)"

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Frame grab failed — stopping.")
                break
            now = time.perf_counter()
            times.append(now)

            # Rolling FPS over the recent window
            fps = 0.0
            if len(times) >= 2:
                span = times[-1] - times[0]
                if span > 0:
                    fps = (len(times) - 1) / span

            # Mean luminance (0–255) as a brightness proxy
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            bri = float(gray.mean())

            all_fps.append(fps)
            all_bri.append(bri)

            wall = time.time() - t_start
            if wall - last_print >= 0.5:
                last_print = wall
                bar = "#" * int(min(fps, 40) / 2)
                sys.stdout.write("\rFPS %5.1f | brightness %5.1f/255 | %-20s"
                                 % (fps, bri, bar))
                sys.stdout.flush()

            if not args.no_window:
                h, w = frame.shape[:2]
                color = ((0, 200, 0) if fps >= 25 else
                         (0, 165, 255) if fps >= 18 else (0, 0, 255))
                cv2.putText(frame, "FPS %.1f" % fps, (12, 36),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 2,
                            cv2.LINE_AA)
                cv2.putText(frame, "brightness %.0f/255" % bri, (12, 72),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
                            cv2.LINE_AA)
                hint = ("good" if fps >= 25 else
                        "add light" if bri < 90 else "throttled?")
                cv2.putText(frame, hint, (12, h - 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2,
                            cv2.LINE_AA)
                cv2.imshow(window, frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):  # q or Esc
                    break

            if args.seconds and wall >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if not args.no_window:
            cv2.destroyAllWindows()

    # ── Summary ──
    print("\n")
    warm = all_fps[10:] if len(all_fps) > 20 else all_fps  # skip warm-up
    if warm:
        import numpy as np

        fps_med = float(np.median(warm))
        fps_peak = float(np.percentile(warm, 95))
        print("Frames: %d over %.0f s" % (len(all_fps), time.time() - t_start))
        print("FPS   : median %.1f | peak %.1f | min %.1f"
              % (fps_med, fps_peak, min(warm)))
        print("Bright: median %.0f | min %.0f | max %.0f /255"
              % (np.median(all_bri), min(all_bri), max(all_bri)))
        bri_med = float(np.median(all_bri))
        if bri_med < 10:
            print("\nNOTE: the image was almost black (brightness %.0f/255). "
                  "That does NOT test auto-exposure throttling — point the "
                  "camera at a normally-lit scene and vary the room light to "
                  "check whether lighting changes the FPS." % bri_med)
        if fps_peak >= 25 and fps_med < 0.75 * fps_peak:
            print("\nVERDICT: raw-camera FPS varied (reached %.0f, sustained "
                  "%.0f). If this tracked the room light, it is auto-exposure "
                  "throttling — add front light. If not, it is the camera "
                  "driver/USB." % (fps_peak, fps_med))
        elif fps_med >= 25:
            print("\nVERDICT: the CAMERA sustains %.0f FPS. If your actual "
                  "GazeFollower recordings are slower (e.g. ~13 Hz), the "
                  "limiter is NOT the camera but GazeFollower's per-frame "
                  "inference / CPU — confirm with tracker_fps_test.py."
                  % fps_med)
        else:
            print("\nVERDICT: CONSISTENTLY LOW (%.0f FPS, never higher). "
                  "Try a different camera index or the DirectShow backend; "
                  "if the image is bright, it is the camera/driver, not "
                  "lighting." % fps_med)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
