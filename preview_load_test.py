# -*- coding: utf-8 -*-
"""
Does the live gaze preview (the green dot) cost sampling rate?

WHY THIS TEST EXISTS
--------------------
``hz_experiment.py`` showed the tracking pipeline holds 30.0 Hz for six
minutes — but it ran GazeFollower IN-PROCESS, with no Flask, no SocketIO,
no browser and no inter-process traffic. It proved the models are fast.
It said nothing about the preview.

``diagnose_rate.py``'s "polled" condition was also misleading here: it
called ``gf.get_gaze_info()`` inside the same process, which is a cheap
attribute read. The REAL preview is a full round trip:

    Flask  --JSON on stdin-->  tracker main thread  --reply on stdout-->

For every poll the tracker's main thread must wake, parse JSON, call
``get_gaze_info``, serialise and write — **contending for the GIL with
the capture thread that is running MediaPipe and MNN in that same
process**. That is a plausible way to lose frames, and nothing measured
so far tests it.

This runs the REAL stack — tracker subprocess, GazeService, the same
command protocol — against the fixed clip, and varies only the polling:

    no_preview     no polling at all
    preview_150ms  the app's actual rate (socketio.sleep(0.15))
    preview_50ms   3x faster, to see whether the effect scales
    preview_20ms   pathological, to make any effect unmissable

If the rate falls as polling gets faster, the preview is the cause and
the fix is straightforward (poll slower, or stop the preview while
recording). If all four match, the preview is exonerated and the
remaining suspect is the browser/video decode itself.

PREREQUISITE::

    python fake_camera.py --record --seconds 30

USAGE::

    python preview_load_test.py               # ~5 min, unattended
    python preview_load_test.py --seconds 30
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DEFAULT_CLIP = os.path.join(DATA, "fake_face.mp4")

# label -> poll interval in seconds (None = no polling)
CONDITIONS = [
    ("no_preview", None),
    ("preview_150ms", 0.15),      # what app.py actually does
    ("preview_50ms", 0.05),
    ("preview_20ms", 0.02),
]


def run_condition(label: str, interval: "float | None", seconds: float,
                  clip: str) -> dict:
    """Measure the rate through the real tracker subprocess."""
    os.environ["GF_FAKE_CAMERA"] = clip
    os.environ["GF_FAKE_CALIBRATION"] = "1"

    from gaze_service import GazeService

    svc = GazeService()
    polls: list = []
    stop = threading.Event()

    def poller():
        while not stop.is_set():
            t0 = time.perf_counter()
            svc.gaze_info()               # the exact call the preview makes
            polls.append((time.perf_counter() - t0) * 1000.0)
            stop.wait(interval)

    try:
        if not svc.available:
            return {"ok": False, "error": "tracker subprocess unavailable"}
        # Warm up: load the model and open the (fake) camera.
        svc.warmup()
        # Sampling must be running before the passive rate check.
        svc.gaze_info()
        time.sleep(2.0)

        started = svc.rate_check_start()
        if not started or not started.get("ok"):
            return {"ok": False,
                    "error": (started or {}).get("error", "start failed")}

        if interval is not None:
            threading.Thread(target=poller, daemon=True).start()
        time.sleep(seconds)
        stop.set()
        time.sleep(0.2)

        res = svc.rate_check_result(8.0)
        if not res or not res.get("ok"):
            return {"ok": False,
                    "error": (res or {}).get("error", "no result")}
        res["label"] = label
        res["poll_interval_ms"] = None if interval is None else interval * 1000
        res["polls"] = len(polls)
        res["poll_ms_median"] = round(statistics.median(polls), 2) if polls \
            else None
        res["poll_ms_p90"] = round(sorted(polls)[int(0.9 * (len(polls) - 1))],
                                   2) if len(polls) > 1 else None
        return res
    finally:
        stop.set()
        try:
            svc.shutdown()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--clip", default=DEFAULT_CLIP)
    args = ap.parse_args()

    try:
        from env_check import require

        require("gazefollower", "MNN", "cv2")
    except ImportError:
        pass

    clip = args.clip if os.path.isabs(args.clip) \
        else os.path.join(BASE, args.clip)
    if not os.path.isfile(clip):
        print("No reference clip at %s" % clip)
        print("Record one first:  python fake_camera.py --record --seconds 30")
        return 1

    print("=" * 74)
    print("  DOES THE GAZE PREVIEW COST SAMPLING RATE?")
    print("=" * 74)
    print("  Real tracker subprocess + real command protocol, fixed clip.")
    print("  %d conditions x %.0f s — about %.0f min, unattended."
          % (len(CONDITIONS), args.seconds,
             len(CONDITIONS) * (args.seconds + 25) / 60.0))
    print()

    results = []
    for i, (label, interval) in enumerate(CONDITIONS, 1):
        print("[%d/%d] %-14s " % (i, len(CONDITIONS), label), end="",
              flush=True)
        try:
            res = run_condition(label, interval, args.seconds, clip)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "label": label, "error": str(exc)[:120]}
        results.append(res)
        if res.get("ok"):
            print("%5.1f Hz   %s" % (res["sustained_hz"],
                                     res.get("profile_hz")))
        else:
            print("FAILED — %s" % str(res.get("error"))[:80])
        time.sleep(2.0)

    print()
    print("-" * 74)
    print("%-14s %10s %10s %10s %12s"
          % ("CONDITION", "Hz", "polls", "poll ms", "poll p90 ms"))
    print("-" * 74)
    for r in results:
        if r.get("ok"):
            print("%-14s %10.1f %10s %10s %12s"
                  % (r["label"], r["sustained_hz"], r.get("polls", 0),
                     r.get("poll_ms_median", "-"), r.get("poll_ms_p90", "-")))
        else:
            print("%-14s     failed" % r.get("label", "?"))

    ok = [r for r in results if r.get("ok")]
    print()
    print("=" * 74)
    print("  VERDICT")
    print("=" * 74)
    base = next((r for r in ok if r["label"] == "no_preview"), None)
    if not base or len(ok) < 2:
        print("  Not enough conditions succeeded to conclude anything.")
    else:
        app_rate = next((r for r in ok if r["label"] == "preview_150ms"), None)
        worst = min(ok, key=lambda r: r["sustained_hz"])
        if app_rate and app_rate["sustained_hz"] < 0.85 * base["sustained_hz"]:
            print("  *** THE PREVIEW COSTS RATE. ***")
            print("  %.1f Hz without it vs %.1f Hz at the app's own 150 ms "
                  "polling" % (base["sustained_hz"], app_rate["sustained_hz"]))
            print()
            print("  Each poll is a full IPC round trip into the tracker")
            print("  process, competing with the capture thread for the GIL.")
            print("  Fixes, cheapest first:")
            print("    - stop the preview while RECORDING (it is only there")
            print("      to reassure during setup)")
            print("    - poll slower (0.3-0.5 s is plenty for a dot)")
            print("    - push samples from the tracker instead of polling")
        elif worst["sustained_hz"] < 0.85 * base["sustained_hz"]:
            print("  The app's own 150 ms polling looks harmless (%.1f vs "
                  "%.1f Hz)," % (app_rate["sustained_hz"] if app_rate else 0,
                                 base["sustained_hz"]))
            print("  but heavier polling DOES cost rate (%s at %.1f Hz)."
                  % (worst["label"], worst["sustained_hz"]))
            print("  So the mechanism is real but the app is not currently")
            print("  triggering it. Do not add more polling.")
        else:
            print("  The preview is NOT the cause: %s"
                  % ", ".join("%s %.1f Hz" % (r["label"], r["sustained_hz"])
                              for r in ok))
            print()
            print("  Even pathological 20 ms polling did not move the rate.")
            print("  Remaining suspect: the BROWSER itself — fullscreen video")
            print("  decode and compositing — rather than the app's own")
            print("  traffic. Next test: run a session with the video")
            print("  replaced by a static image and compare.")

    os.makedirs(DATA, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out = os.path.join(DATA, "preview_load_%s.json" % stamp)
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"when": stamp, "seconds": args.seconds,
                       "results": results}, fh, indent=2)
        print()
        print("  Saved: %s" % out)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
