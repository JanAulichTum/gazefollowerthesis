# -*- coding: utf-8 -*-
"""
Walk the REAL session lifecycle and measure the rate at every stage.

THE GAP THIS FILLS
------------------
Everything measured so far starts sampling ONCE and then times frames:

    hz_experiment.py      in-process, no Flask, no IPC, no calibration
    preview_load_test.py  real subprocess, but sampling started once
    diagnose_rate.py      in-process

A real session does something none of those do — it starts and stops
sampling repeatedly:

    position guide -> preview -> CALIBRATION (stops twice, then the UI)
      -> verification preview -> accuracy check (stops, restarts)
      -> verification again -> recording

Each ``start_sampling()`` appends ``_write_sample`` to GazeFollower's
subscriber list; its ``remove_subscriber`` deletes from the list it is
iterating over. If duplicates survive, every one costs an extra CSV
write **per frame** — a slowdown that can only appear in a real session,
which is exactly why no benchmark reproduced it.

This drives the real tracker subprocess through that same churn against
the fixed clip, measuring the rate AND the subscriber count after each
stage. If the rate falls stage by stage, the lifecycle is the cause and
the table shows precisely where.

PREREQUISITE::

    python fake_camera.py --record --seconds 30

USAGE::

    python session_probe.py                 # ~4 min, unattended
    python session_probe.py --seconds 20
    python session_probe.py --cycles 10     # exaggerate the churn
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=25.0,
                    help="measurement window per stage")
    ap.add_argument("--cycles", type=int, default=3,
                    help="stop/start cycles per lifecycle stage")
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

    os.environ["GF_FAKE_CAMERA"] = clip
    os.environ["GF_FAKE_CALIBRATION"] = "1"

    from gaze_service import GazeService

    svc = GazeService()
    stages: list = []

    def measure(label: str) -> dict:
        started = svc.rate_check_start()
        if not started or not started.get("ok"):
            return {"label": label, "ok": False,
                    "error": (started or {}).get("error", "start failed")}
        time.sleep(args.seconds)
        res = svc.rate_check_result(8.0) or {}
        res["label"] = label
        return res

    print("=" * 76)
    print("  SESSION LIFECYCLE PROBE")
    print("=" * 76)
    print("  Reproduces the start/stop sampling churn of a real session")
    print("  against a fixed clip. %d stages x %.0f s — roughly %.0f min."
          % (5, args.seconds, 5 * (args.seconds + 8) / 60.0))
    print()

    try:
        if not svc.available:
            print("Tracker subprocess unavailable.")
            return 1

        print("  warming up (model + fake camera)…", flush=True)
        svc.warmup()
        svc.gaze_info()                  # starts sampling, as the app does
        time.sleep(2.0)

        # Stage 1: baseline, sampling started once.
        print("  [1/5] fresh              ", end="", flush=True)
        stages.append(measure("fresh"))
        print(_fmt(stages[-1]))

        # Stage 2: the churn calibration performs.
        info = svc.cycle_sampling(args.cycles)
        print("  [2/5] after calibration  ", end="", flush=True)
        stages.append(measure("after_calibration"))
        stages[-1]["cycled"] = (info or {}).get("cycles")
        print(_fmt(stages[-1]))

        # Stage 3: verification preview + accuracy check churn.
        svc.cycle_sampling(args.cycles)
        print("  [3/5] after validation   ", end="", flush=True)
        stages.append(measure("after_validation"))
        print(_fmt(stages[-1]))

        # Stage 4: more churn, standing in for repeat checks.
        svc.cycle_sampling(args.cycles * 2)
        print("  [4/5] after extra churn  ", end="", flush=True)
        stages.append(measure("after_extra_churn"))
        print(_fmt(stages[-1]))

        # Stage 5: a long window, to expose any slow slide.
        print("  [5/5] sustained (2x)     ", end="", flush=True)
        saved = args.seconds
        args.seconds = saved * 2
        stages.append(measure("sustained"))
        args.seconds = saved
        print(_fmt(stages[-1]))
    finally:
        try:
            svc.shutdown()
        except Exception:  # noqa: BLE001
            pass

    ok = [s for s in stages if s.get("ok")]
    print()
    print("-" * 76)
    # Per-frame cost is the headline number now: a live session with the
    # browser up spends ~64 ms per frame against a 33.3 ms budget, so the
    # comparison that matters is this run's cost, browser-free.
    print("%-18s %7s %6s %9s %9s %9s  %s"
          % ("STAGE", "Hz", "det%", "frame ms", "face ms", "gaze ms",
             "5 s profile"))
    print("-" * 76)
    for s in stages:
        if s.get("ok"):
            cap = s.get("capture") or {}
            st = s.get("stages") or {}
            print("%-18s %7.1f %5s%% %9s %9s %9s  %s"
                  % (s["label"], s["sustained_hz"], s.get("detected_pct"),
                     cap.get("callback_ms_median"), st.get("face_ms_median"),
                     st.get("gaze_ms_median"), s.get("profile_hz")))
        else:
            print("%-18s   failed: %s"
                  % (s.get("label"), str(s.get("error"))[:40]))

    print()
    print("=" * 76)
    print("  VERDICT")
    print("=" * 76)
    if len(ok) < 2:
        print("  Not enough stages measured.")
    else:
        first, last = ok[0], ok[-1]
        subs = [s.get("subscribers") for s in ok
                if s.get("subscribers") is not None]
        if subs and max(subs) > min(subs):
            print("  *** SUBSCRIBERS GREW: %s ***" % subs)
            print("  Each extra subscriber is another CSV write per frame.")
            print("  This accumulates ONLY across a session's start/stop")
            print("  churn, which is why offline benchmarks missed it.")
        elif subs:
            print("  Subscriber count stayed at %s — no accumulation."
                  % subs[0])
        drop = first["sustained_hz"] - last["sustained_hz"]
        if drop > 0.15 * first["sustained_hz"]:
            print("  RATE FELL across the lifecycle: %.1f -> %.1f Hz."
                  % (first["sustained_hz"], last["sustained_hz"]))
            print("  The stage where it drops is the one to investigate.")
        else:
            print("  Rate held across the lifecycle (%.1f -> %.1f Hz)."
                  % (first["sustained_hz"], last["sustained_hz"]))
            print()
            print("  So the session lifecycle is NOT the cause either.")
            print("  With the tracker, the preview, thermal, the writer and")
            print("  now the lifecycle all cleared, the difference between")
            print("  this (~30 Hz) and a live session (~10 Hz) must come")
            print("  from what this probe still does not include: the real")
            print("  CAMERA and the BROWSER. Test those by running a real")
            print("  session with the video replaced by a static image.")

    os.makedirs(DATA, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out = os.path.join(DATA, "session_probe_%s.json" % stamp)
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"when": stamp, "seconds": args.seconds,
                       "cycles": args.cycles, "stages": stages}, fh, indent=2)
        print()
        print("  Saved: %s" % out)
    except OSError:
        pass
    return 0


def _fmt(stage: dict) -> str:
    if not stage.get("ok"):
        return "failed — %s" % str(stage.get("error"))[:60]
    return "%5.1f Hz  subs=%s  %s" % (
        stage["sustained_hz"], stage.get("subscribers"),
        stage.get("profile_hz"))


if __name__ == "__main__":
    raise SystemExit(main())
