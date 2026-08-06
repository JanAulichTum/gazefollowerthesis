# -*- coding: utf-8 -*-
"""
Find the cheapest per-frame configuration this machine can run — all of it,
in one unattended pass.

WHY THIS EXISTS
---------------
The investigation established the shape of the problem precisely:

    browser-free   28.9 ms per frame  ->  29.9 Hz   (87 % of a 33.3 ms budget)
    with browser   64.5 ms per frame  ->  15.0 Hz   (0 % of frames lost)

Nothing is dropping frames. The pipeline is simply too slow per frame, and
it was already at 87 % of budget with nothing else running. The split is::

    MediaPipe FaceMesh   9.3 ms   (single-threaded CPU, always)
    MNN gaze CNN        19.3 ms   (4 threads — the contention-sensitive half)
    everything else      0.4 ms   (our writer/filter/dispatch: irrelevant)

So the remaining question is not "what is broken" but "what makes the two
model stages cheaper, and how much does core contention cost". This script
measures every knob that could move those numbers, on the actual machine,
and ranks them.

WHAT IT MEASURES
----------------
    [1] Machine        cores, clocks, AC/battery, Windows power plan
    [2] Power state    whether the plan/battery is throttling the CPU
    [3] MNN threads    1/2/4/6/8/all — the gaze CNN is 67 % of the cost
    [4] MNN backends   which the installed wheel will actually accept
    [5] FaceMesh       refine_landmarks on/off, 480p vs 720p input
    [6] Contention     per-frame cost with 0/1/2/4 competing busy cores
                       — reproduces what the browser does, without a browser
    [7] Priority       normal vs high process priority
    [8] Verdict        best configuration found, as copy-pasteable env vars

Nothing here touches the experiment or writes session data. Every stage is
independently guarded: a stage that cannot run reports why and the rest
continue.

USAGE::

    python optimize_rate.py                 # ~4 min, unattended
    python optimize_rate.py --quick         # ~90 s
    python optimize_rate.py --runs 200      # more iterations per point
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import platform
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

# The budget a 30 fps camera gives us, and the measured baseline this
# script is trying to beat (browser-free, from session_probe).
FRAME_BUDGET_MS = 1000.0 / 30.0
BASELINE = {"face_ms": 9.3, "gaze_ms": 19.3, "other_ms": 0.4}

RESULTS: dict = {}


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print("  " + title)
    print("=" * 72)


def _median(vals) -> "float | None":
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else None


# ──────────────────────────────────────────────────────────────────────
# [1] Machine
# ──────────────────────────────────────────────────────────────────────

def _cpu_mhz() -> "int | None":
    """Current CPU clock, or None. Never raises."""
    try:
        import psutil

        freq = psutil.cpu_freq()
        return round(freq.current) if freq else None
    except Exception:  # noqa: BLE001
        return None


def _run(cmd: list) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def machine() -> dict:
    hr("[1] MACHINE")
    info = {
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "logical_cores": os.cpu_count(),
        "physical_cores": None,
        "python": sys.version.split()[0],
    }
    try:
        import psutil

        info["physical_cores"] = psutil.cpu_count(logical=False)
        info["ram_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
        freq = psutil.cpu_freq()
        if freq:
            info["cpu_mhz_current"] = round(freq.current)
            info["cpu_mhz_max"] = round(freq.max) if freq.max else None
        batt = psutil.sensors_battery()
        if batt is not None:
            info["on_ac_power"] = bool(batt.power_plugged)
            info["battery_pct"] = round(batt.percent)
    except Exception as exc:  # noqa: BLE001
        info["psutil"] = "unavailable (%s) — install with: pip install psutil" \
            % str(exc)[:60]

    if sys.platform.startswith("win"):
        scheme = _run(["powercfg", "/getactivescheme"])
        if scheme:
            info["power_plan"] = scheme.split("(")[-1].rstrip(")") \
                if "(" in scheme else scheme

    for k, v in info.items():
        print("  %-18s %s" % (k, v))

    # The single most consequential fact for a 4-thread inference load.
    phys = info.get("physical_cores")
    if phys:
        print()
        if phys <= 4:
            print("  >>> %d physical cores. MNN is configured for 4 threads, so"
                  % phys)
            print("      the gaze CNN alone wants every core this machine has.")
            print("      A browser rendering fullscreen video CANNOT run for")
            print("      free — contention is structural, not incidental.")
        else:
            print("  >>> %d physical cores: there is room to give inference"
                  % phys)
            print("      more threads. See [3].")
    return info


def power_warnings(info: dict) -> list:
    hr("[2] POWER STATE")
    warns = []
    plan = str(info.get("power_plan", "")).lower()
    if plan:
        print("  Active power plan : %s" % info.get("power_plan"))
        if "balanced" in plan or "power saver" in plan or "energiesparmodus" in plan:
            warns.append(
                "Power plan is '%s'. Windows caps sustained CPU clocks on "
                "anything but High performance / Ultimate. This applies to "
                "EVERY number measured so far." % info.get("power_plan"))
    if info.get("on_ac_power") is False:
        warns.append(
            "Running on BATTERY (%s%%). Laptop firmware throttles sustained "
            "CPU hard on battery — sessions must be recorded on AC."
            % info.get("battery_pct"))
    cur, mx = info.get("cpu_mhz_current"), info.get("cpu_mhz_max")
    if cur and mx and cur < 0.6 * mx:
        warns.append(
            "CPU is at %d MHz of a %d MHz maximum (%.0f %%). The processor "
            "is being held below its rated speed right now. (psutil's "
            "'max' is often the BASE clock on Intel, so the true turbo "
            "headroom is usually larger than this ratio suggests.)"
            % (cur, mx, 100.0 * cur / mx))
    if warns:
        for w in warns:
            print("\n  *** %s" % w)
    else:
        print("  No power-related throttling detected.")
    return warns


# ──────────────────────────────────────────────────────────────────────
# [3][4] MNN — the gaze CNN is 67 % of per-frame cost
# ──────────────────────────────────────────────────────────────────────

def mnn_threads(runs: int) -> dict:
    hr("[3] MNN THREAD SWEEP  (gaze CNN — 19.3 ms of the 28.9 ms baseline)")
    try:
        from mnn_backend import _model_path, benchmark_backend
    except Exception as exc:  # noqa: BLE001
        print("  mnn_backend unavailable: %s" % exc)
        return {}
    model = _model_path()
    if not model or not os.path.isfile(model):
        print("  No .mnn model found — skipped.")
        return {}

    n = os.cpu_count() or 4
    counts = sorted({1, 2, 4, 6, 8, n})
    counts = [c for c in counts if c <= max(8, n)]
    out = {}
    print("  %-10s %12s %12s %12s" % ("threads", "median ms", "p90 ms", "max Hz"))
    print("  " + "-" * 48)
    for c in counts:
        res = benchmark_backend(model, 0, c, runs)
        if not res.get("ok"):
            print("  %-10d  failed: %s" % (c, str(res.get("error"))[:40]))
            continue
        out[c] = round(res["median_ms"], 2)
        print("  %-10d %12.2f %12.2f %12.1f"
              % (c, res["median_ms"], res["p90_ms"], res["max_hz"]))
    if out:
        best = min(out, key=lambda k: out[k])
        cur = out.get(4)
        print()
        print("  Best: %d threads at %.2f ms." % (best, out[best]))
        if cur and best != 4:
            saved = cur - out[best]
            print("  GazeFollower hardcodes 4 threads (%.2f ms). Switching to "
                  "%d saves %.2f ms per frame." % (cur, best, saved))
            if saved > 1.0:
                print("  -> set GF_MNN_THREADS=%d" % best)
        elif cur:
            print("  GazeFollower's hardcoded 4 is already optimal here.")
    return out


def mnn_backends(runs: int) -> dict:
    hr("[4] MNN BACKENDS  (is any GPU path actually compiled in?)")
    try:
        from mnn_backend import BACKENDS, _model_path, benchmark_backend
    except Exception as exc:  # noqa: BLE001
        print("  mnn_backend unavailable: %s" % exc)
        return {}
    model = _model_path()
    if not model or not os.path.isfile(model):
        print("  No .mnn model found — skipped.")
        return {}
    out = {}
    cpu_ms = None
    for name, code in BACKENDS.items():
        res = benchmark_backend(model, code, 4, max(30, runs // 3))
        if res.get("ok"):
            out[name] = round(res["median_ms"], 2)
            if name == "CPU":
                cpu_ms = out[name]
            print("  %-8s %8.2f ms" % (name, res["median_ms"]))
        else:
            print("  %-8s unavailable" % name)
    # MNN falls back to CPU silently, so "available" means nothing unless
    # it is measurably FASTER than CPU.
    real = {k: v for k, v in out.items()
            if k != "CPU" and cpu_ms and v < 0.85 * cpu_ms}
    print()
    if real:
        best = min(real, key=lambda k: real[k])
        print("  *** %s is genuinely faster (%.2f ms vs %.2f ms CPU)."
              % (best, real[best], cpu_ms))
        print("  -> set GF_MNN_BACKEND=%s" % best)
    else:
        print("  No backend beats CPU. MNN accepts the request and silently")
        print("  falls back, so equal timings mean the GPU is NOT being used.")
        print("  A GPU path would need a different MNN wheel entirely.")
    return out


# ──────────────────────────────────────────────────────────────────────
# [5] MediaPipe FaceMesh — the other 9.3 ms
# ──────────────────────────────────────────────────────────────────────

def _face_frame(w: int, h: int):
    """A frame with a REAL face in it, at the requested size.

    METHODOLOGICAL NOTE (this was a bug in the first version).
    Benchmarking FaceMesh on random noise measures the wrong thing: no
    face is found, so the graph runs the lightweight DETECTOR and exits,
    skipping the landmark model entirely. The result was ~2 ms, against
    a real measured cost of 9-21 ms, and it made "no-iris" look SLOWER
    than iris — arrant nonsense that only made sense once the input was
    the problem. Use the reference clip, which by construction contains
    a face.
    """
    import cv2
    import numpy as np

    clip = os.path.join(DATA, "fake_face.mp4")
    if os.path.isfile(clip):
        cap = cv2.VideoCapture(clip)
        try:
            ok, frame = cap.read()
            if ok and frame is not None:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                if (frame.shape[1], frame.shape[0]) != (w, h):
                    frame = cv2.resize(frame, (w, h))
                return frame, True
        finally:
            cap.release()
    return (np.random.rand(h, w, 3) * 255).astype("uint8"), False


def facemesh(runs: int) -> dict:
    hr("[5] FACEMESH  (9.3 ms of the baseline — CPU-only, cannot use a GPU)")
    try:
        import mediapipe as mp
        import numpy as np  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print("  mediapipe unavailable: %s" % exc)
        return {}

    _, real_face = _face_frame(640, 480)
    if not real_face:
        print("  !! No data/fake_face.mp4 — falling back to noise, which")
        print("     measures only the face DETECTOR and understates the true")
        print("     cost several-fold. Record a clip first:")
        print("       python fake_camera.py --record --seconds 30")

    out = {}
    print("  %-24s %10s %10s" % ("configuration", "median ms", "vs 480p+iris"))
    print("  " + "-" * 48)
    ref = None
    for refine in (True, False):
        for (w, h) in ((640, 480), (1280, 720)):
            label = "%dx%d %s" % (w, h, "iris" if refine else "no-iris")
            try:
                mesh = mp.solutions.face_mesh.FaceMesh(
                    static_image_mode=False, max_num_faces=1,
                    refine_landmarks=refine, min_detection_confidence=0.5)
                frame, _ = _face_frame(w, h)
                for _ in range(8):       # warm-up: XNNPACK delegate setup
                    mesh.process(frame)
                times = []
                for _ in range(max(20, runs // 4)):
                    t0 = time.perf_counter()
                    mesh.process(frame)
                    times.append((time.perf_counter() - t0) * 1000.0)
                mesh.close()
                med = statistics.median(times)
                out[label] = round(med, 2)
                if ref is None:
                    ref = med
                print("  %-24s %10.2f %9s" % (label, med,
                                              "%+.2f" % (med - ref)))
            except Exception as exc:  # noqa: BLE001
                print("  %-24s  failed: %s" % (label, str(exc)[:30]))
    iris = out.get("640x480 iris")
    noiris = out.get("640x480 no-iris")
    hd = out.get("1280x720 iris")
    print()
    if iris and noiris and iris - noiris > 1.0:
        print("  Iris refinement costs %.2f ms per frame." % (iris - noiris))
        print("  CHECK whether GazeFollower reads the iris landmarks at all;")
        print("  if it does not, this is free time. (It is an upstream")
        print("  constructor argument, so it needs a patch, not an env var.)")
    if iris and hd and hd - iris > 1.0:
        print("  720p input costs %.2f ms MORE per frame than 480p."
              % (hd - iris))
        print("  A real webcam delivers its NATIVE size and GazeFollower")
        print("  resizes in software every frame — so a 720p camera pays")
        print("  this on top of the resize. -> set GF_CAMERA_FIX=1")
    return out


# ──────────────────────────────────────────────────────────────────────
# [6] Contention — what the browser actually does to us
# ──────────────────────────────────────────────────────────────────────

def _busy(stop_at: float) -> None:
    """Occupy one core with plain arithmetic until stop_at."""
    x = 0
    while time.time() < stop_at:
        for _ in range(200000):
            x += 1
    return


def contention(runs: int) -> dict:
    hr("[6] CPU CONTENTION  (a browser, simulated — no browser needed)")
    print("  The browser run cost 64.5 ms per frame vs 28.9 ms browser-free.")
    print("  If competing load reproduces that here, the mechanism is core")
    print("  starvation and the fix is scheduling, not the models.")
    print()
    try:
        from mnn_backend import _model_path, benchmark_backend
    except Exception as exc:  # noqa: BLE001
        print("  mnn_backend unavailable: %s" % exc)
        return {}
    model = _model_path()
    if not model or not os.path.isfile(model):
        print("  No .mnn model found — skipped.")
        return {}

    out = {}
    base = None
    print("  %-14s %12s %12s %14s"
          % ("busy cores", "gaze ms", "vs idle", "predicted Hz"))
    print("  " + "-" * 56)
    for busy in (0, 1, 2, 4):
        procs = []
        try:
            if busy:
                stop_at = time.time() + 60.0
                for _ in range(busy):
                    p = multiprocessing.Process(target=_busy, args=(stop_at,),
                                                daemon=True)
                    p.start()
                    procs.append(p)
                time.sleep(1.0)          # let them ramp
            res = benchmark_backend(model, 0, 4, max(40, runs // 2))
        finally:
            for p in procs:
                try:
                    p.terminate()
                    p.join(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
        if not res.get("ok"):
            print("  %-14d  failed" % busy)
            continue
        ms = res["median_ms"]
        out[busy] = round(ms, 2)
        if base is None:
            base = ms
        # FaceMesh presumably suffers similarly; predict with the same ratio.
        ratio = ms / base if base else 1.0
        frame = BASELINE["face_ms"] * ratio + ms + BASELINE["other_ms"]
        print("  %-14d %12.2f %11s %14.1f"
              % (busy, ms, "%+.0f%%" % (100 * (ratio - 1)), 1000.0 / frame))
    print()
    loaded = {k: v for k, v in out.items() if k > 0}
    if base and loaded:
        worst = max(loaded, key=lambda k: loaded[k])
        best = min(loaded, key=lambda k: loaded[k])
        if loaded[worst] > 1.6 * base:
            print("  *** CONFIRMED: competing load inflates inference %.0f %%."
                  % (100 * (loaded[worst] / base - 1)))
            print("  This is the same mechanism as the browser run. The models")
            print("  are not slow — they are being starved of cores.")
        elif loaded[best] < 0.9 * base:
            # Counter-intuitive but diagnostic: on a throttled laptop,
            # extra load RAISES the clock, so the benchmark speeds up.
            print("  *** Inference got %.0f %% FASTER under load "
                  "(%.2f -> %.2f ms)."
                  % (100 * (1 - loaded[best] / base), base, loaded[best]))
            print("  That is impossible if cores were scarce, and it is the")
            print("  signature of a CPU sitting in a LOW POWER STATE: the")
            print("  busy processes pull the clock up. The bottleneck is the")
            print("  clock, not the core count. See [2].")
        else:
            print("  Competing load barely moved inference (%.0f %% worst)."
                  % (100 * (loaded[worst] / base - 1)))
            print("  Core starvation does NOT explain the browser result.")
    return out


# ──────────────────────────────────────────────────────────────────────
# [7] Process priority
# ──────────────────────────────────────────────────────────────────────

def priority(runs: int) -> dict:
    hr("[7] PROCESS PRIORITY  (can we simply outrank the browser?)")
    try:
        import psutil

        from mnn_backend import _model_path, benchmark_backend
    except Exception as exc:  # noqa: BLE001
        print("  psutil/mnn_backend unavailable: %s" % exc)
        return {}
    model = _model_path()
    if not model or not os.path.isfile(model):
        print("  No .mnn model found — skipped.")
        return {}

    proc = psutil.Process()
    original = proc.nice()
    out = {}
    try:
        high = psutil.HIGH_PRIORITY_CLASS \
            if sys.platform.startswith("win") else -5
        for label, value in (("normal", original), ("high", high)):
            try:
                proc.nice(value)
            except Exception as exc:  # noqa: BLE001
                print("  %-8s could not set priority (%s)"
                      % (label, str(exc)[:50]))
                continue
            stop_at = time.time() + 30.0
            procs = [multiprocessing.Process(target=_busy, args=(stop_at,),
                                             daemon=True)
                     for _ in range(2)]
            for p in procs:
                p.start()
            time.sleep(1.0)
            try:
                res = benchmark_backend(model, 0, 4, max(40, runs // 2))
            finally:
                for p in procs:
                    try:
                        p.terminate()
                        p.join(timeout=2.0)
                    except Exception:  # noqa: BLE001
                        pass
            if res.get("ok"):
                out[label] = round(res["median_ms"], 2)
                print("  %-8s %8.2f ms (under 2 competing cores)"
                      % (label, res["median_ms"]))
    finally:
        try:
            proc.nice(original)
        except Exception:  # noqa: BLE001
            pass
    if len(out) == 2 and out["high"] < 0.9 * out["normal"]:
        print()
        print("  *** High priority recovers %.2f ms per frame under load."
              % (out["normal"] - out["high"]))
        print("  Worth setting on the tracker subprocess for real sessions.")
    elif len(out) == 2:
        print()
        print("  Priority makes no meaningful difference here.")
    return out


# ──────────────────────────────────────────────────────────────────────
# [8] Verdict
# ──────────────────────────────────────────────────────────────────────

def eco_qos(runs: int) -> dict:
    """Does opting out of Windows' background treatment speed inference?

    THE HYPOTHESIS THIS TESTS
    -------------------------
    Both model stages slowed by an identical 2.26x when the browser was
    in the foreground. Contention cannot do that to two engines with
    different threading models; a slower CPU can. On a hybrid Intel part
    the mechanism is EcoQoS + Thread Director parking "background"
    threads on efficiency cores.

    If disabling it recovers the time, the fix is a scheduler policy and
    costs nothing. If it does not, the hypothesis is wrong and the
    uniform ratio still needs an explanation.
    """
    hr("[7b] ECOQOS / EFFICIENCY-CORE DEMOTION")
    try:
        import perf_mode

        from mnn_backend import _model_path, benchmark_backend
    except Exception as exc:  # noqa: BLE001
        print("  unavailable: %s" % exc)
        return {}
    if not perf_mode.is_windows():
        print("  Windows-only mechanism — skipped on %s." % sys.platform)
        return {}
    model = _model_path()
    if not model or not os.path.isfile(model):
        print("  No .mnn model found — skipped.")
        return {}

    out = {}
    res = benchmark_backend(model, 0, 4, max(40, runs // 2))
    if res.get("ok"):
        out["default"] = round(res["median_ms"], 2)
        print("  default (as Windows scheduled us) : %8.2f ms" % out["default"])
    applied = perf_mode.apply(log=lambda m: print("  " + m))
    if not applied.get("applied"):
        print("  Could not apply performance mode: %s" % applied)
        return out
    res = benchmark_backend(model, 0, 4, max(40, runs // 2))
    if res.get("ok"):
        out["perf_mode"] = round(res["median_ms"], 2)
        print("  EcoQoS off + raised priority      : %8.2f ms"
              % out["perf_mode"])
    print()
    if len(out) == 2:
        gain = out["default"] - out["perf_mode"]
        if gain > 0.1 * out["default"]:
            print("  *** %.2f ms recovered (%.0f %% faster) just by opting"
                  % (gain, 100 * gain / out["default"]))
            print("      out of background treatment. -> keep GF_PERF_MODE=1")
        else:
            print("  No meaningful change (%.2f ms). Note this console is"
                  % gain)
            print("  ALREADY the foreground process, so it was probably never")
            print("  demoted — the test is only conclusive inside a real")
            print("  session, with the browser in front. Compare the")
            print("  'Stage split' line from app.py instead.")
    return out


def verdict(res: dict) -> None:
    hr("[8] VERDICT")

    # POWER FIRST. Every other number on this page is measured in units
    # of "however fast the CPU happened to be running". If the clock was
    # throttled, comparing model configurations is comparing noise, and
    # presenting a ranked optimisation list would be actively misleading.
    if res.get("power_warnings"):
        print("  *** STOP. The CPU was throttled during this sweep.")
        for w in res["power_warnings"]:
            print("      - %s" % w)
        print()
        print("  Per-frame cost scales almost exactly with clock speed, and")
        print("  BOTH model stages scale together — which is precisely the")
        print("  pattern observed between the 28.9 ms and 65.0 ms runs.")
        print("  Nothing below is trustworthy until this is fixed:")
        print()
        print("    1. Plug in the AC adapter.")
        print("    2. powercfg /setactive SCHEME_MIN     (High performance)")
        print("    3. Re-run:  python optimize_rate.py")
        print("                python session_probe.py")
        print()
        print("  Only then is a model-level optimisation worth discussing.")
        print("  " + "-" * 68)
        print()

    face = BASELINE["face_ms"]
    gaze = BASELINE["gaze_ms"]
    other = BASELINE["other_ms"]
    print("  Measured baseline (browser-free, from session_probe):")
    print("    FaceMesh %.1f + gaze CNN %.1f + other %.1f = %.1f ms -> %.1f Hz"
          % (face, gaze, other, face + gaze + other,
             1000.0 / (face + gaze + other)))
    print("    Frame budget at 30 fps: %.1f ms (using %.0f %% of it)"
          % (FRAME_BUDGET_MS, 100 * (face + gaze + other) / FRAME_BUDGET_MS))

    actions = []
    threads = res.get("threads") or {}
    if threads:
        best = min(threads, key=lambda k: threads[k])
        cur = threads.get(4)
        if cur and best != 4 and cur - threads[best] > 1.0:
            gaze = gaze - (cur - threads[best])
            actions.append(("set GF_MNN_THREADS=%d" % best,
                            "saves %.1f ms in the gaze CNN"
                            % (cur - threads[best])))
    fm = res.get("facemesh") or {}
    if fm.get("640x480 iris") and fm.get("640x480 no-iris"):
        d = fm["640x480 iris"] - fm["640x480 no-iris"]
        if d > 1.0:
            actions.append(("disable refine_landmarks in GazeFollower",
                            "saves %.1f ms — needs a source patch, and only "
                            "if the iris points are unused" % d))
    if fm.get("1280x720 iris") and fm.get("640x480 iris"):
        d = fm["1280x720 iris"] - fm["640x480 iris"]
        if d > 1.0:
            actions.append(("set GF_CAMERA_FIX=1",
                            "avoids up to %.1f ms if the webcam is 720p native"
                            % d))
    for w in res.get("power_warnings") or []:
        actions.append(("FIX POWER STATE", w))
    cont = res.get("contention") or {}
    if cont and 0 in cont:
        worst = max(cont, key=lambda k: cont[k])
        if cont[worst] > 1.6 * cont[0]:
            actions.append((
                "close other applications / do not run the browser on the "
                "same cores",
                "competing load inflated inference %.0f %%"
                % (100 * (cont[worst] / cont[0] - 1))))
    prio = res.get("priority") or {}
    if len(prio) == 2 and prio.get("high", 1e9) < 0.9 * prio.get("normal", 0):
        actions.append(("raise the tracker's process priority",
                        "recovers %.1f ms under load"
                        % (prio["normal"] - prio["high"])))

    best_frame = face + gaze + other
    print()
    print("  Best configuration found: %.1f ms -> %.1f Hz"
          % (best_frame, 1000.0 / best_frame))
    if best_frame > FRAME_BUDGET_MS:
        print("  *** STILL OVER BUDGET by %.1f ms. Even fully tuned, this"
              % (best_frame - FRAME_BUDGET_MS))
        print("      machine cannot sustain 30 Hz with a browser running.")
        print("      That is a hardware finding, and it belongs in the")
        print("      thesis as a measured limitation rather than a bug.")
    else:
        headroom = FRAME_BUDGET_MS - best_frame
        print("  Within budget by %.1f ms (%.0f %% headroom) — but ONLY with"
              % (headroom, 100 * headroom / FRAME_BUDGET_MS))
        print("  nothing else running. The browser was measured to add")
        print("  ~35 ms per frame, so this margin does not survive a session.")
        print("  Headroom, not the average, is what has to improve.")

    print()
    if actions:
        print("  ACTIONS, most valuable first:")
        for i, (what, why) in enumerate(actions, 1):
            print("   %d. %s" % (i, what))
            print("      %s" % why)
    else:
        print("  No configuration change measurably helps. The per-frame cost")
        print("  is intrinsic to the models on this CPU.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--quick", action="store_true", help="fewer iterations")
    args = ap.parse_args()
    runs = 40 if args.quick else args.runs

    print("=" * 72)
    print("  RATE OPTIMISATION SWEEP")
    print("=" * 72)
    print("  Measures every knob that could reduce per-frame cost.")
    print("  Nothing here writes session data. Roughly %s."
          % ("90 s" if args.quick else "4 min"))

    res: dict = {}
    try:
        info = machine()
        res["machine"] = info
        res["power_warnings"] = power_warnings(info)
        res["threads"] = mnn_threads(runs)
        res["backends"] = mnn_backends(runs)
        res["facemesh"] = facemesh(runs)
        res["contention"] = contention(runs)
        res["priority"] = priority(runs)
        res["eco_qos"] = eco_qos(runs)
        # Clock drift across the sweep. If the CPU slowed WHILE measuring,
        # the later stages are not comparable with the earlier ones and
        # any ranking between them is an artefact.
        end = _cpu_mhz()
        start = info.get("cpu_mhz_current")
        res["cpu_mhz_end"] = end
        if start and end and abs(end - start) > 0.15 * start:
            print("\n  !! CPU clock moved %d -> %d MHz DURING this sweep."
                  % (start, end))
            print("     Stages measured at different clocks cannot be")
            print("     compared with each other. Re-run on AC power.")
        verdict(res)
    except KeyboardInterrupt:
        print("\n  Interrupted.")

    os.makedirs(DATA, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out = os.path.join(DATA, "optimize_rate_%s.json" % stamp)
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"when": stamp, "baseline": BASELINE,
                       "frame_budget_ms": FRAME_BUDGET_MS, "results": res},
                      fh, indent=2, default=str)
        print("\n  Saved: %s" % out)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
