# -*- coding: utf-8 -*-
"""
MNN runtime backend override + benchmark (GPU vs CPU for gaze inference).

THE PROBLEM
-----------
GazeFollower hardcodes its inference runtime in
``gaze_estimator/MGazeNetGazeEstimator.py``::

    config = {'precision': 'low', 'backend': 0, 'numThread': 4}
    rt = MNN.nn.create_runtime_manager((config,))

``backend: 0`` is ``MNN_FORWARD_CPU``. **The GPU is never used**, on any
machine, regardless of what hardware is present. On this project that
matters: per-frame inference cost is what decides whether a session
records at ~29 Hz or drops to ~14.5 Hz.

MNN backend codes: 0=CPU, 1=METAL, 2=CUDA, 3=OPENCL, 6=OPENGL,
7=VULKAN, 8=HIAI, 9=TRT. ``numThread`` only affects the CPU backend.

THE CATCH
---------
Requesting a GPU backend is NOT enough. MNN's Python wheels are built
per-backend: the default PyPI ``MNN`` wheel is **CPU-only**, and OpenCL /
Vulkan / CUDA builds are separate packages. When a requested backend is
not compiled in, MNN **silently falls back to CPU** — you get no error
and no speedup, just the same numbers.

So this module does two things:

1. ``apply_backend_override()`` — patches ``MNN.nn.create_runtime_manager``
   (the single choke point, so it does not depend on GazeFollower's
   internal class layout) to inject a backend/thread/precision chosen by
   environment variables.
2. ``python mnn_backend.py`` — **measures** the real gaze model on every
   backend the installed wheel will accept, so you find out empirically
   whether the GPU is actually doing anything rather than assuming it.

Environment variables (read by tracker_service at startup):

    GF_MNN_BACKEND    CPU | CUDA | OPENCL | VULKAN | METAL  (or a number)
    GF_MNN_THREADS    CPU thread count (default: GazeFollower's 4)
    GF_MNN_PRECISION  low | normal | high   (GazeFollower uses 'low')

Usage::

    python mnn_backend.py                 # benchmark all backends
    python mnn_backend.py --runs 200      # more iterations

IMPORTANT: this only covers the gaze CNN. MediaPipe FaceMesh (face +
478 landmarks), the other half of per-frame cost, runs on CPU through
MediaPipe's Python API regardless of this setting. Use ``--profile`` to
see the split before optimising the wrong half.
"""

from __future__ import annotations

import argparse
import os
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


# MNN forward-type codes (see MNN docs, Interpreter.createRuntime).
BACKENDS = {
    "CPU": 0,
    "METAL": 1,
    "CUDA": 2,
    "OPENCL": 3,
    "OPENGL": 6,
    "VULKAN": 7,
    "TRT": 9,
}
# GazeFollower's hardcoded defaults, for reference and fallback.
GF_DEFAULT = {"precision": "low", "backend": 0, "numThread": 4}

_original_create_runtime_manager = None


def resolve_backend(name: "str | None") -> "int | None":
    """Map a backend name (or numeric string) to an MNN forward code."""
    if not name:
        return None
    name = str(name).strip()
    if not name:
        return None
    if name.isdigit():
        return int(name)
    return BACKENDS.get(name.upper())


def desired_config() -> dict:
    """The runtime overrides requested via environment variables."""
    cfg: dict = {}
    backend = resolve_backend(os.environ.get("GF_MNN_BACKEND"))
    if backend is not None:
        cfg["backend"] = backend
    threads = os.environ.get("GF_MNN_THREADS", "").strip()
    if threads.isdigit():
        cfg["numThread"] = int(threads)
    precision = os.environ.get("GF_MNN_PRECISION", "").strip()
    if precision:
        cfg["precision"] = precision
    return cfg


def apply_backend_override(log=None) -> dict:
    """Patch MNN so GazeFollower's hardcoded runtime config is overridden.

    Must be called BEFORE GazeFollower builds its estimator (i.e. before
    the first ``GazeFollower()`` instantiation). Safe to call repeatedly;
    a no-op when no environment overrides are set, and it never raises —
    a failed patch must not stop the experiment from running.

    Returns the applied overrides ({} if none).
    """
    global _original_create_runtime_manager

    overrides = desired_config()
    if not overrides:
        return {}
    try:
        import MNN
    except Exception as exc:  # noqa: BLE001
        if log:
            log("MNN backend override skipped (MNN not importable: %s)" % exc)
        return {}

    if _original_create_runtime_manager is None:
        _original_create_runtime_manager = MNN.nn.create_runtime_manager

    original = _original_create_runtime_manager

    def _patched(configs, *args, **kwargs):
        try:
            patched_configs = []
            for cfg in configs:
                merged = dict(cfg)
                merged.update(overrides)
                patched_configs.append(merged)
            return original(tuple(patched_configs), *args, **kwargs)
        except Exception:  # noqa: BLE001 — never break inference
            return original(configs, *args, **kwargs)

    MNN.nn.create_runtime_manager = _patched
    if log:
        log("MNN runtime override active: %s (GazeFollower default was %s). "
            "NOTE: MNN silently falls back to CPU if the backend is not "
            "compiled into the installed wheel — verify with "
            "'python mnn_backend.py'." % (overrides, GF_DEFAULT))
    return overrides


# ──────────────────────────────────────────────────────────────────────
# Benchmark
# ──────────────────────────────────────────────────────────────────────

def _model_path() -> str:
    env = os.environ.get("GF_MODEL_PATH")
    if env and env.strip():
        return env.strip()
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "models", "base_32M.mnn")
    if os.path.isfile(local):
        return local
    try:
        import gazefollower
        bundled = os.path.join(os.path.dirname(gazefollower.__file__),
                               "res", "model_weights", "base.mnn")
        if os.path.isfile(bundled):
            return bundled
    except Exception:  # noqa: BLE001
        pass
    return ""


def benchmark_backend(model_path: str, backend: int, threads: int,
                      runs: int, precision: str = "low") -> dict:
    """Time the real gaze model on one backend. Never raises."""
    import numpy as np

    try:
        import MNN
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "MNN not importable: %s" % exc}

    original = _original_create_runtime_manager or MNN.nn.create_runtime_manager
    try:
        config = {"precision": precision, "backend": backend,
                  "numThread": threads}
        rt = original((config,))
        module = MNN.nn.load_module_from_file(
            model_path, ["face", "left", "right", "rect"], ["output_0"],
            runtime_manager=rt,
        )
        # Same input shapes GazeFollower uses.
        face = MNN.expr.placeholder((1, 224, 224, 3), MNN.expr.NHWC)
        left = MNN.expr.placeholder((1, 112, 112, 3), MNN.expr.NHWC)
        right = MNN.expr.placeholder((1, 112, 112, 3), MNN.expr.NHWC)
        rect = MNN.expr.placeholder((1, 12))
        face.write(np.random.rand(1, 224, 224, 3).astype(np.float32))
        left.write(np.random.rand(1, 112, 112, 3).astype(np.float32))
        right.write(np.random.rand(1, 112, 112, 3).astype(np.float32))
        rect.write(np.random.rand(1, 12).astype(np.float32))
        inputs = [face, left, right, rect]

        for _ in range(10):            # warm-up (GPU kernel compilation)
            module.onForward(inputs)

        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            out = module.onForward(inputs)
            if out:
                out[0].read()          # force completion, not just enqueue
            times.append((time.perf_counter() - t0) * 1000.0)
        times.sort()
        return {
            "ok": True,
            "median_ms": statistics.median(times),
            "p10_ms": times[int(0.10 * len(times))],
            "p90_ms": times[int(0.90 * len(times))],
            "max_hz": 1000.0 / statistics.median(times),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


def profile_pipeline(runs: int = 60) -> None:
    """Split per-frame cost into FaceMesh vs gaze CNN.

    Optimising the gaze model is pointless if MediaPipe dominates — and
    MediaPipe's Python API is CPU-only, so that half cannot be moved to
    the GPU at all.
    """
    import numpy as np

    print("\n" + "=" * 68)
    print("PIPELINE PROFILE (where the per-frame time actually goes)")
    print("=" * 68)
    try:
        import cv2  # noqa: F401
        import mediapipe as mp
    except Exception as exc:  # noqa: BLE001
        print("  mediapipe/cv2 unavailable (%s) — skipped" % exc)
        return
    try:
        mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False, max_num_faces=1,
            refine_landmarks=True, min_detection_confidence=0.5)
        frame = (np.random.rand(480, 640, 3) * 255).astype(np.uint8)
        for _ in range(5):
            mesh.process(frame)
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            mesh.process(frame)
            times.append((time.perf_counter() - t0) * 1000.0)
        print("  MediaPipe FaceMesh : %6.1f ms median  (CPU-only in the "
              "Python API — cannot be moved to the GPU)"
              % statistics.median(times))
        mesh.close()
    except Exception as exc:  # noqa: BLE001
        print("  FaceMesh profiling failed: %s" % str(exc)[:120])
    print("  (Compare against the gaze-CNN numbers above. Whichever is "
          "larger is the one worth attacking.)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=100,
                    help="timed iterations per backend")
    ap.add_argument("--threads", type=int, default=0,
                    help="CPU threads to test (default: 4 and os.cpu_count)")
    ap.add_argument("--profile", action="store_true",
                    help="also profile MediaPipe FaceMesh cost")
    args = ap.parse_args()

    model = _model_path()
    if not model or not os.path.isfile(model):
        print("No .mnn model found. Set GF_MODEL_PATH, or place "
              "models/base_32M.mnn next to this script.")
        return 1

    try:
        import MNN
        version = getattr(MNN, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001
        print("MNN is not importable:", exc)
        return 1

    print("=" * 68)
    print("MNN GAZE-MODEL BACKEND BENCHMARK")
    print("=" * 68)
    print("  MNN version : %s" % version)
    print("  model       : %s (%.1f MB)"
          % (model, os.path.getsize(model) / (1024 * 1024)))
    print("  CPU cores   : %s" % (os.cpu_count() or "?"))
    print("  GazeFollower's hardcoded runtime: %s  <- backend 0 = CPU"
          % GF_DEFAULT)
    print()
    print("  Camera frame budget: 33.3 ms at 30 fps. If total per-frame")
    print("  cost exceeds that, the capture loop misses every other frame")
    print("  and the rate halves to ~15 Hz. That is the whole game.")
    print()

    thread_options = [args.threads] if args.threads else \
        sorted({4, max(1, (os.cpu_count() or 4) // 2), os.cpu_count() or 4})

    print("-" * 68)
    print("%-10s %-8s %10s %10s %10s" % ("BACKEND", "THREADS", "MEDIAN",
                                         "p10", "MAX Hz"))
    print("-" * 68)
    cpu_median = None
    for name, code in BACKENDS.items():
        opts = thread_options if code == 0 else [4]
        for threads in opts:
            r = benchmark_backend(model, code, threads, args.runs)
            if not r["ok"]:
                print("%-10s %-8s   unavailable — %s"
                      % (name, threads, r["error"][:40]))
                continue
            if code == 0 and cpu_median is None:
                cpu_median = r["median_ms"]
            note = ""
            if code != 0 and cpu_median:
                ratio = cpu_median / r["median_ms"]
                if 0.9 <= ratio <= 1.1:
                    note = "  <- same as CPU: SILENT FALLBACK, not real GPU"
                elif ratio > 1.1:
                    note = "  <- %.1fx faster than CPU" % ratio
                else:
                    note = "  <- SLOWER than CPU"
            print("%-10s %-8s %8.2f ms %8.2f ms %8.1f%s"
                  % (name, threads, r["median_ms"], r["p10_ms"],
                     r["max_hz"], note))

    if args.profile:
        profile_pipeline()

    print()
    print("HOW TO READ THIS")
    print("  * A GPU backend that matches CPU timing did NOT run on the")
    print("    GPU — MNN fell back silently because that backend is not")
    print("    compiled into your wheel. The stock PyPI 'MNN' package is")
    print("    CPU-only; OpenCL/Vulkan/CUDA need a separately built wheel.")
    print("  * If a backend IS genuinely faster, enable it for real runs:")
    print("      Windows PowerShell:  $env:GF_MNN_BACKEND='OPENCL'")
    print("      macOS/Linux:         export GF_MNN_BACKEND=METAL")
    print("    then re-run tracker_fps_test.py to confirm the END-TO-END")
    print("    rate improved — the gaze CNN is only half the per-frame")
    print("    cost, so a 2x win here may be much less overall.")
    print("  * Whatever you choose, record it: inference backend changes")
    print("    the sampling rate, and the sampling rate changes your")
    print("    fixation statistics. It belongs in the methods section.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
