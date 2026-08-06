# -*- coding: utf-8 -*-
"""
Unattended experiment to find WHY the sampling rate degrades.

Runs the real pipeline repeatedly against a fixed recorded clip, varying
one thing at a time, and prints a comparison table. No person, no live
camera, no clicking — start it and walk away.

Each condition is measured in a FRESH SUBPROCESS. That matters: if the
degradation accumulates inside a process (buffers, subscribers, a growing
file), a fresh process resets it and the repeats will look identical; if
it is thermal or system-wide, later runs start lower. One is a code bug,
the other is not, and nothing else here distinguishes them.

CONDITIONS
  baseline      current defaults
  repeat2/3     identical to baseline — does run N differ from run 1?
  stock_writer  GF_SAMPLE_PATCH=0   → GazeFollower's own writer, which
                flushes to disk on EVERY sample and drops failed frames
  flush_always  patched writer but GF_SAMPLE_FLUSH_SECONDS=0 → isolates
                the per-sample flush from the rest of the patch
  threads8      GF_MNN_THREADS=8
  blazeface     GF_FACE_ALIGNMENT=blazeface (upstream's speed lever)

PREREQUISITE — record a reference clip once (needs you, ~30 s)::

    python fake_camera.py --record --seconds 30
    python fake_camera.py --check data/fake_face.mp4

USAGE::

    python hz_experiment.py                  # ~7 min, all conditions
    python hz_experiment.py --seconds 30     # quicker
    python hz_experiment.py --only baseline,repeat2,repeat3
    python hz_experiment.py --list

Results are written to data/hz_experiment_<timestamp>.json alongside a
readable summary, so runs can be compared later.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
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
BUDGET_MS = 1000.0 / 30.0

# name -> environment overrides applied on top of the fake-camera setup
CONDITIONS: "dict[str, dict]" = {
    "baseline":     {},
    "repeat2":      {},
    "repeat3":      {},
    "stock_writer": {"GF_SAMPLE_PATCH": "0"},
    "flush_always": {"GF_SAMPLE_FLUSH_SECONDS": "0"},
    "threads8":     {"GF_MNN_THREADS": "8"},
    "blazeface":    {"GF_FACE_ALIGNMENT": "blazeface"},
}


# ──────────────────────────────────────────────────────────────────────
# One measurement (runs in a child process)
# ──────────────────────────────────────────────────────────────────────

def measure(seconds: float) -> dict:
    """Drive GazeFollower against the fake clip and time every frame."""
    from fake_camera import apply_fake_calibration, make_fake_camera

    result: dict = {"ok": False}
    try:
        from gazefollower import GazeFollower
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "GazeFollower import failed: %s" % exc}

    kwargs = {}
    cam = make_fake_camera()
    if cam is None:
        return {"ok": False,
                "error": "fake camera unavailable — record a clip first: "
                         "python fake_camera.py --record"}
    kwargs["camera"] = cam

    if os.environ.get("GF_FACE_ALIGNMENT", "").lower().startswith("blaze"):
        # The package exports a MODULE of this name as well as the class,
        # and which one you get depends on the build — hence the fallback
        # to the module attribute ("'module' object is not callable").
        try:
            from gazefollower import face_alignment as _fa

            cls = getattr(_fa, "BlazeFaceAlignment", None)
            if cls is not None and not callable(cls):
                cls = getattr(cls, "BlazeFaceAlignment", None)
            if cls is None or not callable(cls):
                return {"ok": False,
                        "error": "BlazeFaceAlignment class not found in this "
                                 "gazefollower build"}
            kwargs["face_alignment"] = cls()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "BlazeFace unavailable: %s" % exc}

    try:
        gf = GazeFollower(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "GazeFollower init failed: %s" % exc}

    try:
        from sample_patch import apply_sample_patch

        apply_sample_patch(gf)
    except Exception:  # noqa: BLE001
        pass
    apply_fake_calibration(gf)
    # Calibration must be usable even when the stub is off, or every
    # frame raises and we would measure error handling instead.
    calib = getattr(gf, "calibration", None)
    if calib is not None and not getattr(calib, "has_calibrated", False):
        calib.predict = lambda features, estimated: (True, estimated)
        calib.has_calibrated = True

    arrivals: list = []
    detected = [0]
    orig = gf.process_frame

    def timed(state, timestamp, frame):
        try:
            return orig(state, timestamp, frame)
        finally:
            arrivals.append(time.perf_counter())

    gf.process_frame = timed
    gf.camera.set_on_image_callback(timed)

    def counter(face_info, gaze_info):
        if getattr(gaze_info, "status", False):
            detected[0] += 1

    try:
        gf.add_subscriber(counter)
    except Exception:  # noqa: BLE001
        pass

    try:
        gf.start_sampling()
        time.sleep(2.0)                      # settle
        arrivals.clear()
        detected[0] = 0
        time.sleep(seconds)
    finally:
        try:
            gf.stop_sampling()
        except Exception:  # noqa: BLE001
            pass
        try:
            gf.release()
        except Exception:  # noqa: BLE001
            pass

    if len(arrivals) < 10:
        return {"ok": False, "error": "only %d frames" % len(arrivals)}

    gaps = [b - a for a, b in zip(arrivals[:-1], arrivals[1:]) if b > a]
    span = arrivals[-1] - arrivals[0]
    result = {
        "ok": True,
        "frames": len(arrivals),
        "detected_pct": round(100.0 * detected[0] / len(arrivals), 1),
        "hz_median": round(1.0 / statistics.median(gaps), 1),
        "hz_overall": round((len(arrivals) - 1) / span, 1) if span else 0.0,
        "profile_hz": _buckets(arrivals, 5.0),
        "seconds": round(span, 1),
    }
    return result


def _buckets(stamps: list, bucket_s: float) -> list:
    if len(stamps) < 4:
        return []
    t0, out, edge, cur, prev = stamps[0], [], bucket_s, [], stamps[0]
    for s in stamps[1:]:
        cur.append(s - prev)
        prev = s
        if s - t0 >= edge:
            if len(cur) >= 3:
                cur.sort()
                mid = cur[len(cur) // 2]
                out.append(round(1.0 / mid, 1) if mid > 0 else 0.0)
            cur = []
            edge += bucket_s
    if len(cur) >= 3:
        cur.sort()
        mid = cur[len(cur) // 2]
        out.append(round(1.0 / mid, 1) if mid > 0 else 0.0)
    return out


# ──────────────────────────────────────────────────────────────────────
# Orchestration (parent process)
# ──────────────────────────────────────────────────────────────────────

def run_condition(name: str, overrides: dict, seconds: float,
                  clip: str) -> dict:
    env = dict(os.environ)
    env["GF_FAKE_CAMERA"] = clip
    env["GF_FAKE_CALIBRATION"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Clear anything a previous shell may have exported, so each
    # condition differs ONLY by its own overrides.
    for key in ("GF_SAMPLE_PATCH", "GF_SAMPLE_FLUSH_SECONDS",
                "GF_MNN_THREADS", "GF_FACE_ALIGNMENT", "GF_MNN_BACKEND"):
        env.pop(key, None)
    env.update(overrides)

    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__),
         "--single", "--seconds", str(seconds)],
        cwd=BASE, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=seconds + 180,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("@RESULT@"):
            try:
                return json.loads(line[len("@RESULT@"):])
            except ValueError:
                pass
    return {"ok": False,
            "error": (proc.stdout + proc.stderr).strip()[-300:] or "no result"}


def verdict(results: "dict[str, dict]") -> list:
    lines: list = []
    ok = {k: v for k, v in results.items() if v.get("ok")}
    if not ok:
        return ["No condition produced a measurement."]

    base = ok.get("baseline")
    repeats = [ok[k] for k in ("baseline", "repeat2", "repeat3") if k in ok]

    # 1. Does it slide WITHIN a run?
    if base and len(base.get("profile_hz") or []) >= 3:
        prof = base["profile_hz"]
        first, last = prof[0], prof[-1]
        if last < 0.75 * first:
            monotonic = all(b <= a * 1.05 for a, b in zip(prof, prof[1:]))
            lines.append(
                ("SLIDE within a single run: %.1f -> %.1f Hz %s. "
                 % (first, last,
                    "monotonically" if monotonic else "unevenly"))
                + ("Something accumulates as the run proceeds."
                   if monotonic else
                   "Uneven, so more likely external interference."))
        else:
            lines.append("STABLE within a run (%.1f -> %.1f Hz). The "
                         "degradation you saw live is NOT reproducible with "
                         "fixed input — so it came from the input (your "
                         "position/lighting) or from the app layer, not the "
                         "tracker." % (first, last))

    # 2. Does it degrade ACROSS runs?
    if len(repeats) >= 2:
        firsts = [r["hz_median"] for r in repeats]
        drop = firsts[0] - min(firsts)
        if drop > 0.15 * firsts[0]:
            lines.append(
                "DEGRADES ACROSS RUNS (%s Hz). Each run is a fresh process, "
                "so this is thermal or system-wide, NOT a leak in the code."
                % " -> ".join("%.1f" % f for f in firsts))
        else:
            lines.append(
                "Repeat runs agree (%s Hz) — no cross-run degradation."
                % " -> ".join("%.1f" % f for f in firsts))

    # 3. Per-condition comparisons against baseline
    if base:
        for name, label in (
                ("stock_writer", "GazeFollower's own writer (per-sample "
                                 "flush + dropped frames)"),
                ("flush_always", "flushing on every sample"),
                ("threads8", "8 MNN threads"),
                ("blazeface", "BlazeFace instead of FaceMesh")):
            if name not in ok:
                continue
            delta = ok[name]["hz_median"] - base["hz_median"]
            if abs(delta) < 0.10 * base["hz_median"]:
                lines.append("%s: no meaningful difference (%+.1f Hz)."
                             % (label, delta))
            else:
                lines.append("%s: %+.1f Hz (%.1f vs %.1f) — WORTH ACTING ON."
                             % (label, delta, ok[name]["hz_median"],
                                base["hz_median"]))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="measurement window per condition")
    ap.add_argument("--clip", default=DEFAULT_CLIP)
    ap.add_argument("--only", help="comma-separated condition names")
    ap.add_argument("--list", action="store_true", help="list conditions")
    ap.add_argument("--single", action="store_true",
                    help=argparse.SUPPRESS)      # internal: one measurement
    args = ap.parse_args()

    if args.list:
        for name, env in CONDITIONS.items():
            print("  %-14s %s" % (name, env or "(defaults)"))
        return 0

    if args.single:
        print("@RESULT@" + json.dumps(measure(args.seconds)))
        return 0

    try:
        from env_check import require

        require("gazefollower", "MNN", "cv2")
    except ImportError:
        pass

    clip = args.clip if os.path.isabs(args.clip) \
        else os.path.join(BASE, args.clip)
    if not os.path.isfile(clip):
        print("No reference clip at %s" % clip)
        print()
        print("Record one first (this is the ONLY step needing you):")
        print("    python fake_camera.py --record --seconds 30")
        print("    python fake_camera.py --check data/fake_face.mp4")
        return 1

    names = [n.strip() for n in args.only.split(",")] if args.only \
        else list(CONDITIONS)
    unknown = [n for n in names if n not in CONDITIONS]
    if unknown:
        print("Unknown condition(s): %s" % ", ".join(unknown))
        print("Available: %s" % ", ".join(CONDITIONS))
        return 1

    total_min = len(names) * (args.seconds + 12) / 60.0
    print("=" * 72)
    print("  SAMPLING-RATE EXPERIMENT (unattended)")
    print("=" * 72)
    print("  clip       : %s" % clip)
    print("  conditions : %s" % ", ".join(names))
    print("  each       : %.0f s in a fresh process" % args.seconds)
    print("  estimated  : %.0f min — you can walk away now." % total_min)
    print()

    results: "dict[str, dict]" = {}
    for i, name in enumerate(names, 1):
        print("[%d/%d] %-14s " % (i, len(names), name), end="", flush=True)
        t0 = time.time()
        res = run_condition(name, CONDITIONS[name], args.seconds, clip)
        results[name] = res
        if res.get("ok"):
            print("%.1f Hz  (%.0f%% detected)  %s"
                  % (res["hz_median"], res["detected_pct"],
                     res["profile_hz"]))
        else:
            print("FAILED — %s" % str(res.get("error"))[:90])
        time.sleep(max(0.0, 3.0 - (time.time() - t0) % 1))

    print()
    print("-" * 72)
    print("%-14s %8s %8s %10s  %s"
          % ("CONDITION", "Hz", "detect%", "frames", "5 s profile"))
    print("-" * 72)
    for name in names:
        r = results[name]
        if r.get("ok"):
            print("%-14s %8.1f %7.0f%% %10d  %s"
                  % (name, r["hz_median"], r["detected_pct"], r["frames"],
                     r["profile_hz"]))
        else:
            print("%-14s   failed" % name)

    print()
    print("=" * 72)
    print("  WHAT THIS SHOWS")
    print("=" * 72)
    for line in verdict(results):
        print("  * %s" % line)

    os.makedirs(DATA, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out = os.path.join(DATA, "hz_experiment_%s.json" % stamp)
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"when": stamp, "seconds": args.seconds,
                       "clip": clip, "results": results}, fh, indent=2)
        print()
        print("  Saved: %s" % out)
    except OSError as exc:
        print("  (could not save results: %s)" % exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
