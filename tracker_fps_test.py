# -*- coding: utf-8 -*-
"""
Measure GazeFollower's REAL sample rate (inference included).

`camera_fps_test.py` measures what the webcam delivers (no gaze model).
This tool measures how fast GazeFollower actually produces gaze samples,
i.e. camera capture + MediaPipe FaceMesh + eye models + gaze CNN (MNN).

Compare the two:
  * camera_fps_test.py  ~= 28 FPS   (raw camera)
  * tracker_fps_test.py ~= 13 Hz    (GazeFollower)
=> the gap is the per-frame INFERENCE cost. If the tracker rate is far
below the camera rate, the limiter is compute/CPU (or thermal), NOT
lighting and NOT the camera.

It also watches the rate over time, so a turbo-then-thermal-throttle
drop (high for the first ~20 s, then sustained lower) is visible.

Usage:
    python tracker_fps_test.py                # 30 s measurement
    python tracker_fps_test.py --seconds 60

IMPORTANT: stop the experiment server first (it owns the webcam/model).

NO CALIBRATION IS NEEDED — but not for the reason you might assume.
GazeFollower's ``process_frame`` RAISES "No calibration model is
available" on every frame unless ``calibration.has_calibrated`` is True,
and GazeFollower **never persists a calibration**: ``SVRCalibration``
has a ``save_model()`` method, but nothing in GazeFollower ever calls it.
So ``~/GazeFollower/calibration/svr_*.xml`` is never written, a fresh
instance always starts uncalibrated, and this measurement would be
impossible on any machine, ever.

Since we are timing THROUGHPUT, the calibration mapping is irrelevant:
it is one SVR predict on a 1x12 feature vector, microseconds against a
~20 ms frame. So we stub ``calibration.predict`` to pass the raw
estimate through. Everything expensive — camera capture, MediaPipe
FaceMesh, patch clipping/resizing, the gaze CNN — still runs exactly as
in a real session, so the rate is the real rate. The gaze COORDINATES
are meaningless here; only the timing is used.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

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
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="measurement duration")
    args = ap.parse_args()

    # Check the interpreter FIRST: "No module named 'gazefollower'" nearly
    # always means the project venv is not active, not that anything is
    # broken. env_check says so explicitly and exits.
    try:
        from env_check import require

        require("gazefollower", "MNN")
    except ImportError:
        pass                      # env_check missing → fall through

    try:
        from gazefollower import GazeFollower
    except Exception as exc:  # noqa: BLE001
        print("Could not import GazeFollower:", exc)
        print("Run this in the project venv, with the server stopped.")
        return 1

    print("Loading GazeFollower (model + camera)…", flush=True)
    try:
        gf = GazeFollower()
    except Exception as exc:  # noqa: BLE001
        print("GazeFollower init failed:", exc)
        print("Is the experiment server still running (it owns the camera)?")
        return 1

    # Bypass calibration so frames can flow (see the module docstring:
    # GazeFollower never saves a calibration, so has_calibrated is False
    # on every fresh instance and process_frame would raise on every
    # frame). Timing is unaffected — the stub replaces one microsecond
    # SVR predict; all the expensive work still runs.
    calibration = getattr(gf, "calibration", None)
    if calibration is None:
        print("Unexpected GazeFollower build: no .calibration attribute.")
        return 1
    if not getattr(calibration, "has_calibrated", False):
        calibration.predict = lambda features, estimated: (True, estimated)
        calibration.has_calibrated = True
        print("(no saved calibration — using a pass-through stub; the")
        print(" gaze COORDINATES are meaningless, only the RATE is used)")
    else:
        print("(using the existing calibration model)")

    def _sample_timestamp(gi) -> "float | None":
        """A per-sample timestamp to dedupe by (ns or s). Falls back to
        the gaze coordinates when no timestamp attribute exists."""
        for attr in ("timestamp", "time", "t"):
            v = getattr(gi, attr, None)
            if v is not None:
                try:
                    return float(v)
                except Exception:  # noqa: BLE001
                    pass
        coords = getattr(gi, "filtered_gaze_coordinates", None) \
            or getattr(gi, "gaze_coordinates", None)
        if coords is not None:
            try:
                return float(coords[0]) * 1e6 + float(coords[1])
            except Exception:  # noqa: BLE001
                return None
        return None

    seen: set = set()
    stamps: "list[float]" = []          # wall-clock arrival of each NEW sample
    try:
        gf.start_sampling()
        print("Sampling. Move a little so the gaze estimate changes.\n"
              "Measuring for %.0f s…\n" % args.seconds, flush=True)
        t0 = time.perf_counter()
        last_print = 0.0
        while time.perf_counter() - t0 < args.seconds:
            gi = gf.get_gaze_info()
            if gi is not None and getattr(gi, "status", False):
                key = _sample_timestamp(gi)
                if key is not None and key not in seen:
                    seen.add(key)
                    stamps.append(time.perf_counter())
            wall = time.perf_counter() - t0
            if wall - last_print >= 1.0:
                last_print = wall
                # rate over the last ~3 s
                recent = [s for s in stamps if s >= time.perf_counter() - 3]
                rate = (len(recent) - 1) / (recent[-1] - recent[0]) \
                    if len(recent) >= 2 else 0.0
                sys.stdout.write("\r  t=%4.0fs  new samples=%5d  "
                                 "current rate=%5.1f Hz"
                                 % (wall, len(stamps), rate))
                sys.stdout.flush()
            time.sleep(0.005)      # poll faster than any plausible rate
    except KeyboardInterrupt:
        pass
    finally:
        try:
            gf.stop_sampling()
        except Exception:  # noqa: BLE001
            pass
        try:
            gf.release()
        except Exception:  # noqa: BLE001
            pass

    print("\n")
    if len(stamps) < 5:
        print("Too few samples captured (%d). Was a face visible and lit?"
              % len(stamps))
        return 1

    gaps = [b - a for a, b in zip(stamps[:-1], stamps[1:]) if b > a]
    overall = (len(stamps) - 1) / (stamps[-1] - stamps[0])
    med_hz = 1.0 / statistics.median(gaps) if gaps else 0.0
    # first third vs last third — reveals a turbo→thermal drop
    third = max(1, len(stamps) // 3)
    early = stamps[:third]
    late = stamps[-third:]
    early_hz = (len(early) - 1) / (early[-1] - early[0]) if len(early) > 1 else 0
    late_hz = (len(late) - 1) / (late[-1] - late[0]) if len(late) > 1 else 0

    print("GazeFollower sample rate: overall %.1f Hz | median-interval "
          "%.1f Hz" % (overall, med_hz))
    print("  early third %.1f Hz  ->  late third %.1f Hz" % (early_hz, late_hz))
    if early_hz and late_hz < 0.75 * early_hz:
        print("  -> DROP over time: consistent with CPU thermal throttling "
              "after the turbo window (a compute limit, not the camera).")
    print("\nCompare with camera_fps_test.py (raw camera). If the camera "
          "sustains ~30 FPS but this is ~13 Hz, GazeFollower's per-frame "
          "inference is the bottleneck — a faster machine / GPU-backed MNN "
          "would raise the rate; lighting would not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
