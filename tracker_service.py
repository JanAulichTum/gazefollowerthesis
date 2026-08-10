# -*- coding: utf-8 -*-
"""
GazeFollower Tracker Service (subprocess)
=========================================

Runs GazeFollower — a deep-learning webcam eye tracker
(https://github.com/GanchengZhu/GazeFollower) — in its OWN process,
separate from the Flask server.

Why a subprocess?
-----------------
GazeFollower's calibration UI uses a pygame window, and on macOS GUI
windows must be created from a process's *main* thread.  Flask handles
requests on worker threads, so calibration cannot run inside the web
server directly.  This script therefore runs standalone, owns the
webcam + pygame window, and receives commands from Flask via stdin,
replying on stdout.

Protocol (line-based JSON)
--------------------------
Flask → service (stdin), one JSON object per line::

    {"cmd": "calibrate"}
    {"cmd": "rate_check_start"}
    {"cmd": "rate_check_result", "tail_seconds": 8}
    {"cmd": "start"}
    {"cmd": "stop_save", "csv": "/abs/path/out.csv"}
    {"cmd": "ping"}
    {"cmd": "shutdown"}

Service → Flask (stdout): each reply is one line prefixed with ``@GF@``
so that any stray prints from third-party libraries cannot corrupt the
protocol::

    @GF@{"ok": true, "cmd": "calibrate"}
    @GF@{"ok": false, "cmd": "start", "error": "..."}

All diagnostic logging goes to stderr.
"""

import collections
import ctypes
import glob
import json
# NOTE: module-level, deliberately. _focal_px() is a @staticmethod that
# uses math on its fallback path, and several methods previously relied
# on a LOCAL `import math` inside _metrics_from_face_info. That local
# import does not cover the static method, so the uncalibrated path
# raised NameError — silently, because the position guide swallows
# exceptions, which is why est_distance_cm never reached the manifest.
import math
import os
import platform
import statistics
import subprocess
import sys
import time
import traceback

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


# Reply prefix — the Flask side only parses stdout lines with this marker.
MARKER = "@GF@"

# Inherited from the Flask process (TEST_MODE=1 python app.py):
# shortens calibration so the pipeline can be checked quickly.
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() in ("1", "true", "yes")

# The camera's rated frame rate. Used only to detect BURSTS: gaps shorter
# than one frame period cannot come from a camera running at this rate,
# so they mean frames were queued in the driver buffer while the consumer
# stalled, then read back-to-back. That pattern is diagnostic — it says
# the pipeline is stalling intermittently, not running uniformly slowly.
NOMINAL_CAMERA_FPS = float(os.environ.get("NOMINAL_SAMPLING_HZ", "30"))

# The authors' stronger base model (trained on 32M images, vs 7M for the
# bundled one). Bundled here so it loads automatically — no env var
# needed. GF_MODEL_PATH still overrides this if set.
DEFAULT_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "base_32M.mnn"
)


def reply(payload: dict) -> None:
    """Write a protocol reply line to stdout and flush immediately."""
    sys.stdout.write(MARKER + json.dumps(payload) + "\n")
    sys.stdout.flush()


def status(stage: str) -> None:
    """Push an asynchronous progress event (not a command reply).

    The Flask side forwards these to the browser so the participant sees
    what is happening during slow steps (model loading can take minutes
    on weaker laptops and previously looked like a freeze).
    """
    reply({"event": "status", "stage": stage})


def log(msg: str) -> None:
    """Diagnostic output → stderr (never stdout, which is protocol-only)."""
    sys.stderr.write("[tracker_service] " + msg + "\n")
    sys.stderr.flush()


_mnn_preloaded = False


def _preload_mnn_runtime() -> None:
    """Work around broken macOS MNN wheels.

    Some MNN wheels link ``_mnncengine*.so`` with flat-namespace
    undefined symbols that must already be loaded when the module is
    imported. If a ``libMNN.dylib`` ships inside the wheel but is not
    preloaded, ``import MNN`` fails with::

        symbol not found in flat namespace '__ZN3MNN10getVersionEv'

    Preloading every MNN dylib we can find with RTLD_GLOBAL makes those
    symbols visible. Harmless no-op when nothing is found.
    """
    global _mnn_preloaded
    if _mnn_preloaded or sys.platform != "darwin":
        return
    _mnn_preloaded = True
    try:
        import site

        bases = list(site.getsitepackages())
        try:
            bases.append(site.getusersitepackages())
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        bases = []
    bases = [b for b in set(bases) if b and os.path.isdir(b)]

    candidates: list[str] = []
    for base in bases:
        candidates += glob.glob(
            os.path.join(base, "MNN", "**", "*.dylib"), recursive=True
        )
        candidates += glob.glob(os.path.join(base, "*MNN*.dylib"))

    for lib in sorted(set(candidates)):
        try:
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            log("Preloaded MNN runtime library: %s" % lib)
        except OSError as exc:
            log("Could not preload %s: %s" % (lib, exc))


def _set_macos_dock_visibility(visible: bool) -> None:
    """Show/hide this process in the macOS Dock & app switcher.

    While the calibration window is open the process must be a regular
    foreground app. Afterwards it keeps running (recording gaze), and
    without this call macOS shows a lingering "Python" app that looks
    crashed. Transforming to a UIElement app "closes" it visually and
    returns focus to the browser, while sampling continues.
    """
    if sys.platform != "darwin":
        return
    try:
        class _PSN(ctypes.Structure):
            _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]

        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices"
        )
        psn = _PSN(0, 2)  # kCurrentProcess
        # 1 = foreground app (Dock icon), 4 = UIElement (no Dock icon)
        lib.TransformProcessType(ctypes.byref(psn), 1 if visible else 4)
        log("Dock icon %s" % ("shown" if visible else "hidden"))
    except Exception as exc:  # noqa: BLE001 — cosmetic feature, never fatal
        log("Could not change Dock visibility: %s" % exc)


def _arch_info() -> str:
    """Machine architecture, flagging Rosetta translation on Apple Silicon."""
    mach = platform.machine()
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "sysctl.proc_translated"],
                capture_output=True, text=True, timeout=5,
            )
            if out.stdout.strip() == "1":
                return (mach + "  (WARNING: x86_64 Python under Rosetta on an "
                        "Apple Silicon Mac — MNN x86_64 wheels are broken; "
                        "use a native arm64 Python)")
        except Exception:  # noqa: BLE001
            pass
    return mach


class Service:
    """Wraps the GazeFollower lifecycle behind the line protocol."""

    def __init__(self) -> None:
        self.gf = None            # GazeFollower instance (lazy)
        self.calibrated = False
        self.sampling = False
        self._cali_mode = None    # calibration mode of the live instance
        # Newest FaceInfo from GazeFollower's sample stream. GazeInfo does
        # NOT carry face/eye geometry — only FaceInfo (dispatched to
        # subscribers every frame) has face_rect/left_rect/right_rect,
        # img_w/img_h and per-eye openness, which the pre-calibration
        # position guide needs. Populated by ``_on_sample``.
        self._latest_face_info = None
        self._face_subscribed = False   # subscribe to GF only once
        # MNN runtime overrides actually applied (GF_MNN_BACKEND etc.).
        # Recorded in the session manifest: the inference backend changes
        # the sampling rate, and the sampling rate changes the fixation
        # statistics — so it is a methods-section fact, not a detail.
        self.mnn_overrides: dict = {}
        # Passive sampling-rate measurement (see cmd_rate_check_start):
        # arrival times recorded by _on_sample, never by a blocking poll.
        self._rate_stamps: list = []
        self._rate_collecting = False
        self._rate_ok = 0          # frames that yielded a gaze estimate
        self._rate_failed = 0      # frames where detection failed
        # ALWAYS-ON rolling window, independent of the rate check, so
        # telemetry can read a live rate at any moment without starting a
        # measurement (which would reset the real one). Bounded, so the
        # cost is two appends per frame and no growth.
        self._live_stamps: collections.deque = collections.deque(maxlen=150)
        self._live_ok = 0
        self._live_failed = 0

    @staticmethod
    def _resolve_model_path() -> str:
        """Which .mnn file GF_MODEL_PATH currently resolves to (env var
        overrides the bundled default; "" forces the library's stock
        model). Shared by ``_ensure_gf`` and ``cmd_check`` so the
        self-check report can never drift from what actually loads."""
        env_model_path = os.environ.get("GF_MODEL_PATH")
        if env_model_path is not None:
            return env_model_path.strip()
        return DEFAULT_MODEL_PATH

    @staticmethod
    def _describe_fake_mode() -> str:
        """Whether the camera/calibration are being simulated."""
        try:
            from fake_camera import describe

            return describe()
        except Exception as exc:  # noqa: BLE001
            return "unknown (%s)" % exc

    def _describe_perf_mode(self) -> str:
        """Whether Windows' background demotion has been opted out of."""
        state = getattr(self, "perf_mode", None) or globals().get("_EARLY_PERF")
        try:
            import perf_mode

            base = perf_mode.describe()
        except Exception:  # noqa: BLE001
            return "unavailable"
        if not state:
            return base + " (not yet applied)"
        if state.get("applied"):
            return "%s — ACTIVE%s" % (base,
                                      ", pinned to %d cores"
                                      % state["pinned_cores"]
                                      if state.get("pinned_cores") else "")
        return "%s — NOT active (%s)" % (
            base, state.get("skipped") or state.get("eco_qos_error")
            or state.get("priority_error") or "unknown")

    @staticmethod
    def _describe_sample_writer() -> str:
        """Whether failed-detection frames are recorded or dropped."""
        try:
            from sample_patch import describe

            return describe()
        except Exception as exc:  # noqa: BLE001
            return "unknown (%s)" % exc

    @staticmethod
    def _describe_camera_mode() -> str:
        """Whether the camera-capture fix is active."""
        try:
            from camera_patch import describe

            return describe()
        except Exception as exc:  # noqa: BLE001
            return "unknown (%s)" % exc

    @staticmethod
    def _describe_mnn_runtime() -> str:
        """Which MNN backend gaze inference will actually use."""
        try:
            from mnn_backend import GF_DEFAULT, desired_config

            overrides = desired_config()
        except Exception as exc:  # noqa: BLE001
            return "unknown (%s)" % exc
        if not overrides:
            return ("CPU, %d threads (GazeFollower's hardcoded default — "
                    "the GPU is NOT used; set GF_MNN_BACKEND to change, and "
                    "benchmark with 'python mnn_backend.py')"
                    % GF_DEFAULT["numThread"])
        return ("overridden: %s — MNN falls back to CPU silently if this "
                "backend is not compiled into the installed wheel; verify "
                "with 'python mnn_backend.py'" % overrides)

    @classmethod
    def _describe_model_status(cls) -> str:
        """Human-readable summary of which gaze model will load, for the
        startup self-check (so it's visible in the console/log without
        having to grep after a calibration attempt)."""
        model_path = cls._resolve_model_path()
        if not model_path:
            return "stock 7M model (bundled with gazefollower; GF_MODEL_PATH disabled)"
        if os.path.isfile(model_path):
            size_mb = os.path.getsize(model_path) / (1024 * 1024)
            label = "32M base model" if model_path == DEFAULT_MODEL_PATH else "custom model"
            return "ok — %s: %s (%.1f MB)" % (label, model_path, size_mb)
        return "NOT FOUND at %s — will fall back to stock 7M model" % model_path

    # ------------------------------------------------------------------
    # Lazy initialisation — importing gazefollower loads deep-learning
    # models and opens the webcam, so we defer it until first use.
    # ------------------------------------------------------------------
    def _ensure_gf(self, cali_mode=None):
        # Desired calibration mode: explicit request > env var > default
        # (13 points normally — better vertical accuracy; 5 in TEST_MODE)
        desired = int(
            cali_mode
            or os.environ.get("GF_CALI_MODE", "5" if TEST_MODE else "13")
        )
        if self.gf is not None:
            if self._cali_mode in (None, desired):
                return self.gf
            # Mode changed (test-options panel) — needs a fresh instance
            log("Calibration mode %s → %s: recreating tracker"
                % (self._cali_mode, desired))
            try:
                if self.sampling:
                    self.gf.stop_sampling()
                    self.sampling = False
                self.gf.release()
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
            self.gf = None
            self.calibrated = False

        log("Initialising GazeFollower (loading model, opening webcam)…")
        status("loading_model")
        _preload_mnn_runtime()   # macOS MNN wheel workaround

        # GazeFollower hardcodes its MNN runtime to CPU
        # ({'backend': 0, 'numThread': 4}) — the GPU is never used on any
        # machine. Per-frame inference cost is what decides whether a
        # session records at ~29 Hz or halves to ~14.5 Hz, so allow it to
        # be overridden via GF_MNN_BACKEND / GF_MNN_THREADS. Must happen
        # BEFORE GazeFollower builds its estimator.
        # CAUTION: MNN silently falls back to CPU when the requested
        # backend is not compiled into the installed wheel — always
        # verify a change with 'python mnn_backend.py'.
        try:
            from mnn_backend import apply_backend_override

            applied = apply_backend_override(log=log)
            if applied:
                self.mnn_overrides = applied
        except Exception:  # noqa: BLE001 — must never block a session
            log("MNN backend override unavailable:\n" + traceback.format_exc())

        # Opt out of Windows' background-process treatment BEFORE any
        # inference happens. This process is a subprocess of a server
        # while a fullscreen browser holds the foreground, so Windows
        # classifies it as background work and may park it on efficiency
        # cores — which slows EVERY stage by the same factor rather than
        # producing any visible error.
        try:
            import perf_mode

            self.perf_mode = perf_mode.apply(log=log)
        except Exception:  # noqa: BLE001 — must never block a session
            log("Performance mode unavailable:\n" + traceback.format_exc())

        from gazefollower import GazeFollower  # noqa: WPS433 (deliberate lazy import)

        gf_config = None
        try:
            from gazefollower.misc import DefaultConfig

            gf_config = DefaultConfig()
            gf_config.cali_mode = desired
            log("Calibration mode: %d points%s" % (
                gf_config.cali_mode, " (TEST MODE)" if TEST_MODE else ""))
        except Exception as exc:  # noqa: BLE001 — fall back to defaults
            log("Could not set calibration mode (%s) — using defaults" % exc)

        # The authors' stronger base model (trained on 32M images, vs 7M
        # for the bundled one; available for academic use from
        # zhiguo@zju.edu.cn). Defaults to the copy shipped in
        # models/base_32M.mnn; override with
        # GF_MODEL_PATH=/path/to/other_model.mnn, or set GF_MODEL_PATH=""
        # to force the bundled 7M model instead.
        gaze_estimator = None
        model_path = self._resolve_model_path()
        if model_path:
            if os.path.isfile(model_path):
                try:
                    from gazefollower.gaze_estimator import MGazeNetGazeEstimator

                    gaze_estimator = MGazeNetGazeEstimator(model_path=model_path)
                    log("Using custom gaze model: %s" % model_path)
                except Exception as exc:  # noqa: BLE001
                    log("Could not load custom model (%s) — using bundled" % exc)
            else:
                log("GF_MODEL_PATH not found: %s — using bundled model" % model_path)

        kwargs = {}
        if gf_config is not None:
            kwargs["config"] = gf_config
        if gaze_estimator is not None:
            kwargs["gaze_estimator"] = gaze_estimator

        # Opt-in camera fix (GF_CAMERA_FIX=1). GazeFollower applies its
        # resolution/FPS settings to an UNOPENED VideoCapture, so they do
        # nothing: frames arrive at the webcam's native size and are
        # converted + resized in software on every frame, inside the
        # capture loop that also runs inference. See camera_patch.py.
        # FAKE CAMERA (GF_FAKE_CAMERA=clip.mp4) takes precedence: replays
        # a fixed recording so the input is byte-identical every run.
        # Used to separate "the machine/code is slow" from "the person
        # moved" when chasing the sampling rate. Never for real data.
        fixed_camera = None
        try:
            from fake_camera import make_fake_camera

            fixed_camera = make_fake_camera(log=log)
        except Exception:  # noqa: BLE001
            log("Fake camera unavailable:\n" + traceback.format_exc())

        if fixed_camera is None:
            try:
                from camera_patch import make_camera

                fixed_camera = make_camera(log=log)
            except Exception:  # noqa: BLE001 — never block a session
                log("Camera fix unavailable:\n" + traceback.format_exc())
        if fixed_camera is not None:
            kwargs["camera"] = fixed_camera

        # Face alignment backend. GazeFollower ships BlazeFace explicitly
        # "to reduce inference time" versus the default MediaPipe
        # FaceMesh — it is the library's own documented speed lever.
        # Worth knowing: on this hardware FaceMesh measured only ~2.2 ms
        # of a ~19.7 ms frame, so swapping it cannot buy much here. Opt in
        # with GF_FACE_ALIGNMENT=blazeface and MEASURE the difference
        # (diagnose_rate.py) rather than assuming one.
        align = os.environ.get("GF_FACE_ALIGNMENT", "").strip().lower()
        if align in ("blazeface", "blaze"):
            try:
                from gazefollower import face_alignment as _fa

                cls = getattr(_fa, "BlazeFaceAlignment", None)
                if cls is not None and not callable(cls):
                    # Some builds export a module of the same name.
                    cls = getattr(cls, "BlazeFaceAlignment", None)
                if cls is None or not callable(cls):
                    raise ImportError("BlazeFaceAlignment class not found")
                kwargs["face_alignment"] = cls()
                log("Face alignment: BlazeFace (GF_FACE_ALIGNMENT=blazeface). "
                    "NOTE: landmark geometry differs from FaceMesh, so "
                    "accuracy may shift — re-validate before trusting data.")
            except Exception as exc:  # noqa: BLE001
                log("BlazeFace unavailable (%s) — using MediaPipe FaceMesh"
                    % exc)

        self.gf = GazeFollower(**kwargs)

        # GazeFollower RAISES inside _write_sample on any frame whose
        # detection failed (raw_gaze_coordinates is None), so the sample
        # is dropped and two tracebacks are logged instead. That is why
        # `status` was always 1 in the CSVs — failed frames never made it
        # in — and why the recorded rate understated the real capture
        # rate. Must be applied BEFORE start_sampling(), which registers
        # the bound method as a subscriber. See sample_patch.py.
        try:
            from sample_patch import apply_sample_patch

            apply_sample_patch(self.gf, log=log)
        except Exception:  # noqa: BLE001 — never block a session
            log("Sample patch unavailable:\n" + traceback.format_exc())

        # Optional stubbed calibration, so the full flow can run with no
        # person present (GF_FAKE_CALIBRATION=1). Marks the instance
        # calibrated with a pass-through mapping.
        try:
            from fake_camera import apply_fake_calibration

            if apply_fake_calibration(self.gf, log=log):
                self.calibrated = True
        except Exception:  # noqa: BLE001
            log("Fake calibration unavailable:\n" + traceback.format_exc())

        self._cali_mode = desired
        log("GazeFollower initialised.")
        status("model_ready")
        return self.gf

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def cmd_check(self) -> dict:
        """Step-by-step dependency and camera diagnosis.

        Imports each requirement individually so a failure pinpoints the
        exact broken package (e.g. a bad MNN wheel) instead of a generic
        'calibration failed'.
        """
        report: dict = {
            "python": sys.executable + "  (%s)" % sys.version.split()[0],
            "arch": _arch_info(),
            # GazeFollower hardcodes backend 0 (CPU). Show what is
            # actually in force, so "is the GPU being used?" is
            # answerable without reading the source.
            "mnn_runtime": self._describe_mnn_runtime(),
            "camera_mode": self._describe_camera_mode(),
            "sample_writer": self._describe_sample_writer(),
            "fake_mode": self._describe_fake_mode(),
            "perf_mode": self._describe_perf_mode(),
        }

        _preload_mnn_runtime()   # macOS MNN wheel workaround

        for mod in ("numpy", "cv2", "pygame", "MNN", "gazefollower"):
            try:
                m = __import__(mod)
                version = getattr(m, "__version__", "?")
                report[mod] = "ok (%s)" % version
            except Exception as exc:  # noqa: BLE001 — report every failure kind
                report[mod] = "FAIL: %s" % exc
                # Targeted diagnosis for the common mediapipe/protobuf
                # break on Python 3.12: newest mediapipe (0.10.31) fails
                # to expose `solutions`, which GazeFollower needs.
                if mod == "gazefollower" and "solutions" in str(exc):
                    try:
                        import mediapipe as _mp

                        mpv = getattr(_mp, "__version__", "?")
                    except Exception:  # noqa: BLE001
                        mpv = "?"
                    report["mediapipe"] = (
                        "FAIL: version %s lacks a working `solutions` API "
                        "(protobuf incompatibility). FIX: pip install "
                        "\"mediapipe==0.10.21\"  then restart." % mpv
                    )

        # Which gaze model will load (bundled 32M base model, a custom
        # override, or the library's stock 7M model). Informational only
        # — a missing custom model falls back gracefully, so it doesn't
        # gate the overall pass/fail below.
        report["gaze_model"] = self._describe_model_status()

        # Camera check (only if OpenCV imports)
        if report.get("cv2", "").startswith("ok"):
            try:
                import cv2

                cap = cv2.VideoCapture(0)
                grabbed, _frame = cap.read()
                cap.release()
                report["camera"] = (
                    "ok" if grabbed else "FAIL: camera 0 opened but returned no frame "
                    "(check camera permission for the terminal app)"
                )
            except Exception as exc:  # noqa: BLE001
                report["camera"] = "FAIL: %s" % exc
        else:
            report["camera"] = "skipped (cv2 failed)"

        # Informational rows describe the environment rather than pass or
        # fail, so they must be excluded — otherwise adding a new one
        # silently turns every self-check into "PROBLEMS FOUND".
        all_ok = all(
            str(v).startswith("ok")
            for k, v in report.items()
            if k not in ("python", "arch", "gaze_model", "mnn_runtime",
                         "camera_mode", "sample_writer", "fake_mode",
                         "perf_mode")
        )
        for key, val in report.items():
            log("check %-14s %s" % (key + ":", val))
        return {"ok": all_ok, "report": report}

    def cmd_calibrate(self, skip=False, skip_preview=None, cali_mode=None) -> dict:
        """Open a fullscreen pygame window, run preview + calibration.

        Args:
            skip: Reuse the last SAVED calibration model instead of
                calibrating (test-mode option; fails if none exists).
            skip_preview: Skip the camera-positioning preview screen.
                Defaults to True in TEST_MODE.
            cali_mode: Calibration points (5/9/13); recreates the tracker
                if it differs from the current instance.
        """
        # ── Pre-flight: pinpoint problems BEFORE opening any window and
        # return actionable instructions instead of a raw traceback. ──
        if self.gf is None:
            pre = self.cmd_check()
            if not pre["ok"]:
                fails = {
                    k: v for k, v in pre["report"].items()
                    if str(v).startswith("FAIL")
                }
                hints = []
                # mediapipe/protobuf break (esp. Python 3.12) — check
                # this FIRST: it presents as a gazefollower import fail
                # but the real fix is a mediapipe pin, not MNN.
                if "solutions" in str(fails.get("gazefollower", "")) \
                        or "mediapipe" in fails:
                    hints.append(
                        'mediapipe is incompatible with this Python — run:  '
                        '"%s" -m pip install "mediapipe==0.10.21"  then '
                        "restart the server" % sys.executable
                    )
                elif "MNN" in fails or "gazefollower" in fails:
                    if "Rosetta" in pre["report"].get("arch", ""):
                        hints.append(
                            "your Python is an Intel build running under "
                            "Rosetta on an Apple Silicon Mac — MNN's x86_64 "
                            "wheels are broken; run 'bash fix_environment.sh' "
                            "to create a native arm64 environment"
                        )
                    else:
                        hints.append(
                            "broken MNN package — run: bash fix_environment.sh"
                        )
                if "pygame" in fails or "numpy" in fails or "cv2" in fails:
                    hints.append(
                        "missing packages — run:  "
                        '"%s" -m pip install -r requirements.txt' % sys.executable
                    )
                if "camera" in fails:
                    hints.append(
                        "no camera access — macOS: System Settings → Privacy & "
                        "Security → Camera → enable your terminal app, then "
                        "RESTART the server"
                    )
                return {
                    "ok": False,
                    "error": "Pre-flight check failed: " + "; ".join(hints),
                    "report": pre["report"],
                }

        gf = self._ensure_gf(cali_mode)

        # Headless mode: with a stubbed calibration there is nothing for a
        # participant to do, and opening a fullscreen pygame window would
        # just block an automated run. Skip straight to "calibrated".
        try:
            from fake_camera import fake_calibration_enabled

            if fake_calibration_enabled():
                self.calibrated = True
                log("FAKE CALIBRATION — skipping the calibration UI. Gaze "
                    "coordinates are meaningless; timing results are valid.")
                status("finished")
                return {"ok": True, "note": "fake calibration (no person)"}
        except Exception:  # noqa: BLE001
            pass

        # ── Test-mode "skip": reuse a calibration model found on disk. ──
        # NOTE: this almost always fails, and that is CORRECT. GazeFollower
        # never persists a calibration — SVRCalibration.save_model() exists
        # but nothing calls it — so there is normally nothing to reuse.
        # Do NOT "fix" that by calling save_model() after calibration:
        # SVRCalibration auto-loads any saved model at construction, so a
        # persisted file would let a later participant silently inherit an
        # earlier one's mapping. Test-mode convenience is not worth that.
        if skip:
            has_model = bool(
                getattr(getattr(gf, "calibration", None), "has_calibrated", False)
            )
            if has_model:
                self.calibrated = True
                log("Reusing saved calibration model (skip requested).")
                status("finished")
                return {"ok": True, "note": "reused saved calibration"}
            return {
                "ok": False,
                "error": "No saved calibration model to reuse — run a "
                         "calibration once first.",
            }

        # GazeFollower's preview()/calibrate() REQUIRE a non-sampling
        # state ("It is under sampling or calibrating and cannot start
        # previewing"). The pre-calibration position guide and the gaze
        # verification both start sampling for their live feed, so ensure
        # a clean state here before opening the calibration window.
        if self.sampling:
            try:
                gf.stop_sampling()
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
            self.sampling = False
        # Belt-and-suspenders: clear any sampling GazeFollower may have
        # begun itself (some builds start on init/warmup), ignoring the
        # "not sampling" error when it was already idle.
        else:
            try:
                gf.stop_sampling()
            except Exception:  # noqa: BLE001 — expected when already idle
                pass

        import pygame

        status("opening_window")
        _set_macos_dock_visibility(True)   # needs to be a foreground app
        pygame.init()
        info = pygame.display.Info()
        win = pygame.display.set_mode(
            (info.current_w, info.current_h), pygame.FULLSCREEN
        )
        pygame.display.set_caption("Eye-Tracking Calibration")

        try:
            # Skip the camera-preview screen if requested (it only exists
            # so participants can position themselves). Default: skip in
            # TEST_MODE, show otherwise.
            if skip_preview is None:
                skip_preview = TEST_MODE
            if not skip_preview:
                log("Starting camera preview…")
                status("preview")
                gf.preview(win=win)
            log("Starting calibration…")
            status("calibrating")
            gf.calibrate(win=win)
            self.calibrated = True
            log("Calibration finished.")
            status("finished")
            return {"ok": True}
        finally:
            # Fully shut pygame down (not just the display): a half-alive
            # pygame app whose event queue is never pumped again gets
            # flagged by macOS as "not responding" and can leave a ghost
            # fullscreen window. The tracker keeps sampling headlessly —
            # it does not need pygame.
            try:
                pygame.event.pump()
            except Exception:  # noqa: BLE001
                pass
            try:
                pygame.display.quit()
                pygame.quit()
            except Exception:  # noqa: BLE001
                pass
            # Visually "close" the Python app: hide it from the Dock and
            # app switcher; focus returns to the browser automatically.
            _set_macos_dock_visibility(False)

    def cmd_screen_info(self) -> dict:
        """Which screen geometry GazeFollower maps gaze into.

        THIS IS THE ONE TO CHECK when calibration looks perfect but
        validation is wildly wrong. GazeFollower's DefaultConfig does::

            self._monitors = get_monitors()          # screeninfo
            self.screen_size = [monitors[0].width, monitors[0].height]

        and `_calibration_controller.convert_to_pixel` scales normalized
        gaze into THAT space. Two ways it can disagree with the browser:

        1. **Display scaling.** screeninfo reports PHYSICAL pixels; the
           browser lays targets out in CSS pixels. At Windows 150 %
           scaling a 2560x1440 panel is 1707x960 to the browser, so every
           gaze coordinate is ~1.5x too large. Calibration still looks
           perfect — it is internally consistent inside pygame's physical
           space — and only the browser-side validation exposes it.
        2. **Multi-monitor.** ``monitors[0]`` is whatever screeninfo lists
           first, which need not be the monitor the browser is on.

        Both produce "great calibration, terrible validation".
        """
        info: dict = {"ok": True, "monitors": [], "gaze_screen_size": None}
        try:
            from screeninfo import get_monitors

            for m in get_monitors():
                info["monitors"].append({
                    "name": getattr(m, "name", None),
                    "width": m.width, "height": m.height,
                    "x": getattr(m, "x", 0), "y": getattr(m, "y", 0),
                    "is_primary": bool(getattr(m, "is_primary", False)),
                })
        except Exception as exc:  # noqa: BLE001
            info["monitors_error"] = str(exc)

        # What the live instance will actually use, if one exists.
        try:
            size = getattr(self.gf, "screen_size", None)
            if size is not None:
                info["gaze_screen_size"] = [int(size[0]), int(size[1])]
            elif info["monitors"]:
                info["gaze_screen_size"] = [info["monitors"][0]["width"],
                                            info["monitors"][0]["height"]]
        except Exception as exc:  # noqa: BLE001
            info["gaze_screen_size_error"] = str(exc)
        log("Screen info: %s" % info)
        return info

    def cmd_warmup(self, cali_mode=None) -> dict:
        """Load the model & open the camera ahead of time.

        Triggered when the participant reaches the calibration page, so
        the slow initialisation happens while they read the instructions
        instead of after they click the button.
        """
        self._ensure_gf(cali_mode)
        return {"ok": True}

    def _subscriber_count(self) -> "int | None":
        """How many callbacks GazeFollower invokes per frame.

        Should stay at 2 (its own writer + our _on_sample). Anything more
        means duplicates accumulated across start/stop cycles, and each
        duplicate costs a full CSV write on every frame.
        """
        try:
            subs = getattr(self.gf, "subscribers", None)
            return len(subs) if subs is not None else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _bucket_rates(stamps: list, bucket_s: float = 5.0) -> list:
        """Median Hz per time bucket — the shape, not just the average."""
        if len(stamps) < 4:
            return []
        t0 = stamps[0]
        out: list = []
        edge = bucket_s
        current: list = []
        prev = stamps[0]
        for s in stamps[1:]:
            current.append(s - prev)
            prev = s
            if s - t0 >= edge:
                if len(current) >= 3:
                    current.sort()
                    mid = current[len(current) // 2]
                    out.append(round(1.0 / mid, 1) if mid > 0 else 0.0)
                current = []
                edge += bucket_s
        if len(current) >= 3:
            current.sort()
            mid = current[len(current) // 2]
            out.append(round(1.0 / mid, 1) if mid > 0 else 0.0)
        return out

    def cmd_cycle_sampling(self, cycles: int = 1) -> dict:
        """Repeat the stop/start sampling churn that a real session does.

        DIAGNOSTIC ONLY. ``cmd_calibrate`` stops sampling (twice — once
        guarded, once belt-and-braces), the calibration UI runs, then the
        verification preview starts it again; the accuracy check stops
        and restarts it; then recording starts it once more. Every
        ``start_sampling()`` appends ``_write_sample`` to GazeFollower's
        subscriber list, and its ``remove_subscriber`` deletes from the
        list it is iterating over.

        No offline benchmark reproduced that churn — they all start
        sampling once — which is precisely why a session-only slowdown
        could hide there. This lets a probe reproduce it without a person
        or a pygame window, and report the subscriber count afterwards.
        """
        gf = self._ensure_gf()
        for _ in range(max(0, int(cycles))):
            try:
                gf.stop_sampling()
            except Exception:  # noqa: BLE001 — "not sampling" is expected
                pass
            try:
                gf.stop_sampling()      # the belt-and-braces second call
            except Exception:  # noqa: BLE001
                pass
            try:
                gf.start_sampling()
            except Exception:  # noqa: BLE001
                log("cycle_sampling: start failed\n" + traceback.format_exc())
        self.sampling = True
        count = self._subscriber_count()
        log("Sampling cycled %d time(s); subscribers now %s"
            % (cycles, count))
        return {"ok": True, "cycles": cycles, "subscribers": count}

    def cmd_rate_check_start(self) -> dict:
        """Begin measuring the sampling rate PASSIVELY.

        Returns immediately. Sample arrival times are recorded by
        ``_on_sample``, which GazeFollower already calls once per frame,
        so nothing here occupies the command channel.

        This design is deliberate and was learned the hard way. The first
        version blocked inside a 25 s polling loop, which held
        GazeService's command lock for the whole measurement. Every
        ``gaze_info`` call from the browser's verification preview queued
        behind it, so the green dot froze, and an accuracy check started
        during that window collected stale or missing samples and
        reported a meaningless ~5 deg error. Measuring passively also
        gives a MORE representative number: the rate is observed while
        the preview is actually running, i.e. under real session load.
        """
        gf = self._ensure_gf()
        if not getattr(getattr(gf, "calibration", None),
                       "has_calibrated", False):
            return {
                "ok": False,
                "needs_calibration": True,
                "error": "No calibration model loaded — GazeFollower cannot "
                         "produce gaze samples without one.",
            }
        self._ensure_face_subscriber()
        if not self.sampling:
            gf.start_sampling()
            self.sampling = True
            log("Continuous sampling started (for the rate check).")
        self._rate_stamps = []
        self._rate_ok = 0
        self._rate_failed = 0
        self._rate_collecting = True
        # Snapshot the camera's own frame counter so the report can
        # compare frames DELIVERED against samples PRODUCED over the same
        # window. Without this, a halved rate is ambiguous between "the
        # capture loop is slow" and "frames are being dropped".
        self._rate_cam0 = self._camera_stats()
        self._rate_t0 = time.perf_counter()
        # Stage split, cleared so the numbers describe THIS window only.
        self._install_stage_timers(gf)
        for _d in (getattr(self, "_face_ms", None),
                   getattr(self, "_gaze_ms", None)):
            if _d is not None:
                _d.clear()
        log("Rate check started (passive).")
        return {"ok": True, "started": True}

    @staticmethod
    def _camera_stats() -> "dict | None":
        """Live capture-loop counters, when running against a fake clip."""
        try:
            from fake_camera import camera_stats

            return camera_stats()
        except Exception:  # noqa: BLE001
            return None

    def cmd_telemetry(self) -> dict:
        """Cheap read-only snapshot for the 1 Hz telemetry sampler.

        Deliberately touches NOTHING: no camera, no GazeFollower call, no
        lock, no state change. It reads counters the capture thread has
        already written. That matters because this is polled during live
        recording — a probe that took the command lock, or that started a
        measurement, would perturb the session it is supposed to observe.

        Returns {} rather than raising if the tracker is not running.

        NOTE the ``ok`` key is REQUIRED. ``reply()`` sends this dict
        verbatim, and every caller gates on ``reply.get("ok")`` — a bare
        dict is silently discarded, which is exactly what happened for a
        whole session's telemetry before this was fixed.
        """
        out: dict = {"ok": True}
        try:
            stamps = list(self._live_stamps)
            if len(stamps) >= 3:
                span = stamps[-1] - stamps[0]
                if span > 0:
                    out["sampling_hz"] = round((len(stamps) - 1) / span, 1)
                out["live_window_s"] = round(span, 1)
            total = self._live_ok + self._live_failed
            if total:
                out["detected_pct_cumulative"] = round(
                    100.0 * self._live_ok / total, 1)
                out["frames_seen"] = total
            out["sampling"] = bool(self.sampling)
            out["calibrated"] = bool(self.calibrated)
        except Exception:  # noqa: BLE001
            pass

        # Stage costs, from the always-installed timers.
        try:
            face = list(getattr(self, "_face_ms", ()) or ())
            gaze = list(getattr(self, "_gaze_ms", ()) or ())
            if face:
                out["face_ms_median"] = round(statistics.median(face), 1)
            if gaze:
                out["gaze_ms_median"] = round(statistics.median(gaze), 1)
            shape = getattr(self, "_frame_shape", None)
            if shape:
                out["frame_size"] = "%dx%d" % shape
        except Exception:  # noqa: BLE001
            pass

        try:
            out["subscribers"] = self._subscriber_count()
        except Exception:  # noqa: BLE001
            pass

        # Head geometry, so "the rate was fine but the participant moved"
        # is answerable after the fact. FaceInfo is already cached by
        # _on_sample; nothing is computed from pixels here.
        try:
            fi = self._latest_face_info
            if fi is not None:
                rect = getattr(fi, "face_rect", None)
                if rect is not None and len(rect) >= 4:
                    out["face_w"] = int(rect[2])
                    out["face_h"] = int(rect[3])
                    out["face_cx"] = int(rect[0]) + int(rect[2]) // 2
                    out["face_cy"] = int(rect[1]) + int(rect[3]) // 2
                for attr in ("left_openness", "right_openness"):
                    val = getattr(fi, attr, None)
                    if isinstance(val, (int, float)):
                        out[attr] = round(float(val), 3)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _install_stage_timers(self, gf) -> bool:
        """Time the two model stages inside the live per-frame callback.

        WHY IN THE LIVE APP AND NOT JUST IN ``diagnose_rate.py``
        -------------------------------------------------------
        The per-frame cost measured OFFLINE is not the cost that matters:
        the rate only collapses when the browser is up, and no offline
        profiler can observe that. Knowing the total is 64 ms instead of
        33 ms says the pipeline is too slow but not WHERE, and the two
        halves have different remedies:

          face_alignment.detect  MediaPipe FaceMesh — single-threaded
                                 CPU. Cheaper input = cheaper stage.
          gaze_estimator.detect  the MNN CNN, 4 threads. Sensitive to how
                                 many cores are actually free, so this is
                                 the stage that inflates under contention.

        If BOTH roughly double, nothing is wrong with the code and the
        machine is simply oversubscribed. If only one does, that stage is
        the bug. Idempotent, and never raises: a failed install just
        means no split is reported.
        """
        if getattr(self, "_stage_timers_installed", False):
            return True
        try:
            fa, ge = gf.face_alignment, gf.gaze_estimator
            orig_face, orig_gaze = fa.detect, ge.detect
        except Exception:  # noqa: BLE001
            return False

        self._face_ms = collections.deque(maxlen=4000)
        self._gaze_ms = collections.deque(maxlen=4000)

        def timed_face(timestamp, frame):
            # Frame SIZE, recorded here because this is the only place we
            # see what the camera actually delivered. GazeFollower applies
            # its resolution settings to an unopened capture, so they do
            # nothing and frames arrive at the webcam's native size, then
            # get resized in software every frame. A real session can
            # therefore be feeding FaceMesh 720p where a test clip fed it
            # 480p — a large cost difference that is otherwise invisible.
            try:
                shape = getattr(frame, "shape", None)
                if shape is not None:
                    self._frame_shape = (int(shape[1]), int(shape[0]))
            except Exception:  # noqa: BLE001
                pass
            t0 = time.perf_counter()
            try:
                return orig_face(timestamp, frame)
            finally:
                self._face_ms.append((time.perf_counter() - t0) * 1000.0)

        def timed_gaze(image, face_info):
            t0 = time.perf_counter()
            try:
                return orig_gaze(image, face_info)
            finally:
                self._gaze_ms.append((time.perf_counter() - t0) * 1000.0)

        fa.detect = timed_face
        ge.detect = timed_gaze
        self._stage_timers_installed = True
        log("Stage timers installed (FaceMesh / gaze CNN split).")
        return True

    def _stage_split(self) -> "dict | None":
        """Median + p90 cost of each model stage over the last window."""
        face = sorted(getattr(self, "_face_ms", ()) or ())
        gaze = sorted(getattr(self, "_gaze_ms", ()) or ())
        if not face and not gaze:
            return None

        def _stat(vals):
            if not vals:
                return None, None
            p90 = vals[min(len(vals) - 1, int(0.90 * len(vals)))]
            return round(statistics.median(vals), 1), round(p90, 1)

        f_med, f_p90 = _stat(face)
        g_med, g_p90 = _stat(gaze)
        shape = getattr(self, "_frame_shape", None)
        megapixels = (shape[0] * shape[1] / 1e6) if shape else None
        return {
            "face_ms_median": f_med, "face_ms_p90": f_p90,
            "gaze_ms_median": g_med, "gaze_ms_p90": g_p90,
            "models_ms_median": round((f_med or 0) + (g_med or 0), 1),
            "n_face": len(face), "n_gaze": len(gaze),
            "frame_size": ("%dx%d" % shape) if shape else None,
            # 640x480 = 0.31 MP is the intended input. Anything larger is
            # being paid for on every single frame.
            "frame_megapixels": round(megapixels, 2) if megapixels else None,
            "oversized_frames": bool(shape and (shape[0] > 640
                                                or shape[1] > 480)),
        }

    def cmd_rate_check_result(self, tail_seconds: float = 8.0) -> dict:
        """Stop collecting and report the observed rate.

        The verdict uses the FINAL ``tail_seconds`` — the sustained
        figure, not the flattering opening burst, since the CPU turbo
        window can close partway through and halve the rate.
        """
        self._rate_collecting = False
        stamps = list(self._rate_stamps)
        ok, failed = self._rate_ok, self._rate_failed
        if len(stamps) < 10:
            return {"ok": False,
                    "error": "too few samples (%d) — was a face visible and "
                             "lit, and was sampling running?" % len(stamps)}

        def _hz(window: "list[float]") -> float:
            if len(window) < 2 or window[-1] <= window[0]:
                return 0.0
            return (len(window) - 1) / (window[-1] - window[0])

        elapsed = stamps[-1] - stamps[0]
        tail = [s for s in stamps if s >= stamps[-1] - tail_seconds]
        head = [s for s in stamps if s <= stamps[0] + min(8.0, elapsed / 3)]
        sustained = _hz(tail) if len(tail) >= 3 else _hz(stamps)
        initial = _hz(head) if len(head) >= 3 else sustained
        gaps = sorted(b - a for a, b in zip(stamps[:-1], stamps[1:]) if b > a)
        peak = 1.0 / gaps[max(0, int(0.10 * len(gaps)))] if gaps else 0.0

        # ── Capture loop vs sample stream ────────────────────────────
        # THE decisive comparison. served_hz is what the camera thread
        # actually delivered during this window; sustained_hz is what
        # came out the other end. If they agree, the capture loop is the
        # bottleneck and callback_ms says why. If served_hz is ~2x
        # sustained_hz, capture is fine and frames are being DROPPED
        # between the callback and the sample stream — a different bug
        # with a different fix.
        cam_now = self._camera_stats()
        cam0 = getattr(self, "_rate_cam0", None)
        capture = None
        if cam_now:
            capture = dict(cam_now)
            window = time.perf_counter() - getattr(self, "_rate_t0", 0.0)
            if cam0 and window > 0.5:
                delivered = (cam_now.get("frames_served") or 0) \
                    - (cam0.get("frames_served") or 0)
                capture["frames_this_window"] = delivered
                capture["served_hz_this_window"] = round(delivered / window, 1)
                capture["late_this_window"] = (cam_now.get("frames_late") or 0) \
                    - (cam0.get("frames_late") or 0)

        result = {
            "ok": True,
            "sustained_hz": round(sustained, 1),
            "initial_hz": round(initial, 1),
            "peak_hz": round(peak, 1),
            "overall_hz": round(_hz(stamps), 1),
            "n_samples": len(stamps),
            "measured_s": round(elapsed, 1),
            # A >25 % fall from the opening window to the tail means the
            # turbo window closed mid-measurement; the tail is the honest
            # figure for what the stimuli will record at.
            "turbo_drop": bool(initial > 0 and sustained < 0.75 * initial),
            # WHY the rate is what it is. A frame that arrives but yields
            # no gaze estimate is a DETECTION failure, not a speed
            # problem, and the two need opposite fixes. detected_pct
            # distinguishes them; without it a low rate is ambiguous.
            "frames_detected": ok,
            "frames_failed": failed,
            "detected_pct": round(100.0 * ok / (ok + failed), 1)
            if (ok + failed) else None,
            # BURSTS: a peak faster than the camera's rated rate is
            # physically impossible from live capture. It means frames
            # piled up in the driver buffer during a stall and were then
            # read back-to-back. So the pipeline is stalling in bursts,
            # not running uniformly slowly — and the buffered frames are
            # STALE, which corrupts anything timing-sensitive (a
            # validation run during such a burst reads gaze from up to
            # several hundred ms ago).
            "bursty": bool(peak > 1.15 * NOMINAL_CAMERA_FPS),
            "nominal_camera_fps": NOMINAL_CAMERA_FPS,
            # SHAPE of the degradation, in 5 s buckets. "It gets worse and
            # worse" and "it dropped once and stayed down" are different
            # faults — a monotonic slide points at something accumulating
            # (growing buffers, piled-up subscribers, an ever-larger file
            # being rescanned), a single step points at power/thermal.
            # A single median Hz cannot tell them apart.
            "profile_hz": self._bucket_rates(stamps, 5.0),
            # Provenance: the inference backend determines the rate, and
            # the rate determines the fixation statistics.
            "mnn_runtime": self.mnn_overrides or "CPU (GazeFollower default)",
            # Loud marker so a simulated run can never be mistaken for
            # participant data in the manifest.
            "fake_mode": self._describe_fake_mode(),
            # SUBSCRIBER COUNT — the one thing every offline test misses.
            # GazeFollower's start_sampling() APPENDS self._write_sample to
            # its subscriber list, and stop_sampling() removes it with a
            # loop that mutates the list while iterating it (a classic
            # skip-every-other bug). The real session flow starts and
            # stops sampling repeatedly — position guide, preview,
            # calibration, verification, validation — so duplicates can
            # accumulate. Every duplicate means another full CSV
            # write+flush PER FRAME, which would slow the loop
            # progressively and ONLY in a real session. Expect 2: the
            # writer plus our own _on_sample.
            "subscribers": self._subscriber_count(),
            # Capture-loop truth (fake-camera runs only): frames the
            # camera delivered, and how long the synchronous per-frame
            # work took. None on a real webcam, which has no such counter.
            "capture": capture,
            # WHERE the per-frame time goes: FaceMesh vs the gaze CNN.
            "stages": self._stage_split(),
            # Whether the process was exempted from background demotion.
            # Recorded WITH the rate so a low-rate session always carries
            # the answer to "was perf mode on?" — the single largest
            # factor found on this project (2.2x on every stage).
            "perf_mode": self._describe_perf_mode(),
        }
        stages = result["stages"]
        if stages:
            models = stages.get("models_ms_median") or 0.0
            cb = (capture or {}).get("callback_ms_median") or 0.0
            # Anything in the callback that is NOT the two models: the
            # writer, the filter, subscriber dispatch, lock waits.
            result["overhead_ms_median"] = round(max(0.0, cb - models), 1) \
                if cb else None
            log("Stage split: FaceMesh %s ms (p90 %s) + gaze CNN %s ms "
                "(p90 %s) = %s ms of models%s"
                % (stages.get("face_ms_median"), stages.get("face_ms_p90"),
                   stages.get("gaze_ms_median"), stages.get("gaze_ms_p90"),
                   models,
                   ("; %s ms is everything else in the callback"
                    % result["overhead_ms_median"]) if cb else ""))
        if capture:
            served = capture.get("served_hz_this_window")
            cb_med = capture.get("callback_ms_median")
            budget = capture.get("frame_budget_ms") or 33.3
            if served:
                # A sample stream materially slower than the frames that
                # were delivered means frames are being lost downstream.
                result["frames_dropped_pct"] = round(
                    max(0.0, 100.0 * (served - sustained) / served), 1)
                result["capture_limited"] = bool(
                    sustained >= 0.85 * served)
            if cb_med:
                result["over_frame_budget"] = bool(cb_med > budget)
            log("Capture loop: delivered %s Hz, %s frames late | per-frame "
                "callback median %s ms / p90 %s ms (budget %s ms) -> %s"
                % (served, capture.get("late_this_window"), cb_med,
                   capture.get("callback_ms_p90"), budget,
                   "the capture loop IS the limit — per-frame work is too "
                   "expensive" if result.get("capture_limited")
                   else "capture is FASTER than the sample stream — frames "
                        "are being dropped downstream"))
        log("Rate check: sustained %.1f Hz (initial %.1f, peak %.1f) over "
            "%.0f s, %d frames, %s%% detected%s | 5s profile: %s"
            % (result["sustained_hz"], result["initial_hz"],
               result["peak_hz"], result["measured_s"], result["n_samples"],
               result["detected_pct"],
               " — rate FELL during the check" if result["turbo_drop"]
               else "", result["profile_hz"]))
        return result

    # NOTE on GazeFollower's sampling lifecycle: ``save_data()`` closes
    # the internal sample stream PERMANENTLY — a GazeFollower instance
    # supports exactly ONE save per session. Recording must therefore
    # run continuously across all stimuli; per-stimulus segmentation is
    # done afterwards on the Flask side using timestamps + triggers.

    def cmd_begin_stimulus(self, trigger: int) -> dict:
        """Mark stimulus onset. Starts continuous sampling on first call."""
        # DATA-INTEGRITY GUARD. SVRCalibration auto-loads any model found
        # in ~/GazeFollower/calibration at construction and sets
        # has_calibrated=True. GazeFollower itself never writes those
        # files, so this normally cannot happen — but if anything ever
        # did (a manual save_model(), a copied home directory), a session
        # whose calibration was skipped or failed would record silently
        # against a PREVIOUS PARTICIPANT'S mapping, and nothing
        # downstream could detect it. self.calibrated is set only by an
        # actual calibration in THIS process, so require it.
        if not self.calibrated:
            log("REFUSING to record: no calibration was performed in this "
                "session. (Any model on disk belongs to an earlier run and "
                "must not be reused for a new participant.)")
            return {"ok": False,
                    "error": "No calibration was performed in this session — "
                             "recording refused to prevent gaze data being "
                             "mapped through a previous participant's "
                             "calibration."}
        gf = self._ensure_gf()
        if not self.sampling:
            gf.start_sampling()
            self.sampling = True
            log("Continuous sampling started.")
        gf.send_trigger(int(trigger))
        return {"ok": True, "t_ns": time.time_ns()}

    def cmd_end_stimulus(self, trigger: int) -> dict:
        """Mark stimulus offset (sampling continues between stimuli)."""
        if self.gf is None or not self.sampling:
            return {"ok": False, "error": "not sampling"}
        self.gf.send_trigger(int(trigger))
        return {"ok": True, "t_ns": time.time_ns()}

    def cmd_gaze_info(self) -> dict:
        """Latest live gaze estimate (starts continuous sampling if needed).

        Used by the browser's post-calibration verification: a dot that
        follows the participant's gaze proves the tracker works. Starting
        sampling early is harmless — pre-video samples are excluded by
        the per-stimulus timestamp windows during segmentation.
        """
        gf = self._ensure_gf()
        if not self.sampling:
            gf.start_sampling()
            self.sampling = True
            log("Continuous sampling started (for gaze preview).")
        gi = gf.get_gaze_info()
        if gi is None or not getattr(gi, "status", False):
            return {"ok": True, "detected": False, "x": 0, "y": 0}
        try:
            x, y = gi.filtered_gaze_coordinates
            x, y = float(x), float(y)
        except Exception:  # noqa: BLE001
            return {"ok": True, "detected": False, "x": 0, "y": 0}
        if x <= -65000 or y <= -65000:   # GazeFollower's invalid marker
            return {"ok": True, "detected": False, "x": 0, "y": 0}
        return {"ok": True, "detected": True, "x": round(x, 1), "y": round(y, 1)}

    # Average human inter-pupillary distance (cm) — for a coarse
    # monocular distance estimate from the eyes' pixel separation.
    @staticmethod
    def _focal_px(image_w_px: int) -> "tuple[float, bool]":
        """Camera focal length in px, and whether it was MEASURED.

        Prefers a one-off calibration (camera_geometry.py --calibrate),
        which removes the assumed field of view entirely. Falls back to
        the assumption, which is worth roughly +-10 % on the distance —
        and therefore the same on every reported degree figure.
        """
        try:
            import camera_geometry

            geom = camera_geometry.load()
            if geom.get("focal_px"):
                focal = float(geom["focal_px"])
                cal_w = geom.get("image_w_px") or image_w_px
                if cal_w and image_w_px and cal_w != image_w_px:
                    focal *= image_w_px / float(cal_w)
                return focal, True
        except Exception:  # noqa: BLE001 — never block the guide
            pass
        return ((image_w_px / 2.0)
                / math.tan(math.radians(Service._ASSUMED_HFOV_DEG) / 2.0),
                False)

    _REAL_IOD_CM = 6.3
    # Assumed webcam horizontal field of view (deg) — typical laptop
    # cameras are ~55–65°. Logged with every estimate so the assumption
    # is auditable; only the absolute distance depends on it, not the
    # positioning guidance (which is relative).
    _ASSUMED_HFOV_DEG = 60.0

    def _grab_frame(self):
        """Best-effort read of GazeFollower's latest camera frame.

        GazeFollower owns the webcam, so we cannot open it separately.
        Different versions expose the frame under different attributes;
        try the known paths and return ``None`` if none work (the guide
        then degrades to static advice — it never blocks calibration).
        """
        gf = self.gf
        if gf is None:
            return None
        cam = getattr(gf, "camera", None) or getattr(gf, "_camera", None)
        for obj, attr in ((cam, "current_frame"), (cam, "frame"),
                          (cam, "latest_frame"), (gf, "current_frame")):
            if obj is None:
                continue
            val = getattr(obj, attr, None)
            if callable(val):
                try:
                    val = val()
                except Exception:  # noqa: BLE001
                    val = None
            if val is not None and hasattr(val, "shape") \
                    and getattr(val, "ndim", 0) == 3:
                return val
        for meth in ("get_current_frame", "get_frame", "read"):
            fn = getattr(cam, meth, None) if cam else None
            if callable(fn):
                try:
                    out = fn()
                    frame = out[1] if isinstance(out, tuple) else out
                    if frame is not None and getattr(frame, "ndim", 0) == 3:
                        return frame
                except Exception:  # noqa: BLE001
                    continue
        return None

    @staticmethod
    def _attr(obj, names):
        """First present attribute from *names* (list), else None."""
        for n in names:
            v = getattr(obj, n, None)
            if v is not None:
                return v
        return None

    def _on_sample(self, face_info, gaze_info):
        """GazeFollower sample-stream subscriber (public add_subscriber API).

        Runs in GazeFollower's sampling thread once per frame — must be
        fast and never raise. Stores the newest FaceInfo so the position
        guide can read real head geometry (face/eye rects, image dims,
        openness), which GazeInfo does not carry.
        """
        self._latest_face_info = face_info
        # Always-on rolling window for telemetry. Deliberately kept to
        # two appends and one branch: this runs on the capture thread,
        # inside the synchronous per-frame callback, on a machine with
        # ~10 % frame-budget headroom. Anything expensive here would cost
        # sampling rate — the exact thing being monitored.
        self._live_stamps.append(time.perf_counter())
        if getattr(gaze_info, "status", False):
            self._live_ok += 1
        else:
            self._live_failed += 1
        if self._rate_collecting:
            self._rate_stamps.append(time.perf_counter())
            # Record whether this frame actually YIELDED a gaze estimate.
            # Without this, a low rate is ambiguous: it could be the
            # machine failing to keep up, or the face simply not being
            # detected. Those need opposite fixes, and guessing wrong
            # wastes days (it did).
            if getattr(gaze_info, "status", False):
                self._rate_ok += 1
            else:
                self._rate_failed += 1

    def _ensure_face_subscriber(self):
        """Register ``_on_sample`` with GazeFollower exactly once.

        ``add_subscriber`` only appends to GazeFollower's subscriber list
        (it does not displace the internal ``_write_sample`` recorder), so
        this is non-destructive to the gaze recording.
        """
        if self._face_subscribed or self.gf is None:
            return
        add = getattr(self.gf, "add_subscriber", None)
        if not callable(add):
            return
        try:
            add(self._on_sample)
            self._face_subscribed = True
        except Exception as exc:  # noqa: BLE001 — guide is optional
            log("could not add face-info subscriber: %s" % exc)
        # Install the stage timers here too, not only at rate_check_start.
        # Telemetry samples every second for the whole session, including
        # while the stimuli play — which is the window that actually
        # matters and the one no rate check covers.
        self._install_stage_timers(self.gf)

    def _metrics_from_face_info(self) -> "dict | None":
        """Head-position metrics from GazeFollower's OWN FaceInfo.

        FaceInfo is dispatched to subscribers every sampled frame and
        carries face_rect, per-eye left_rect/right_rect ([x, y, w, h]),
        img_w/img_h, 478 landmarks and per-eye openness — the same
        geometry shown in GazeFollower's preview. GazeInfo carries NONE of
        this, which is why the previous get_gaze_info() path always
        returned None. Every access stays defensive so a GazeFollower API
        change degrades to the static tips, never blocks calibration.
        """
        fi = self._latest_face_info
        if fi is None or not getattr(fi, "status", False):
            return None

        def _rect(r):
            try:
                return [float(r[0]), float(r[1]), float(r[2]), float(r[3])]
            except Exception:  # noqa: BLE001
                return None
        face = _rect(getattr(fi, "face_rect", None))
        # face_rect defaults to [0,0,0,0] when nothing is detected.
        if not face or face[2] <= 1 or face[3] <= 1:
            return None
        rr = _rect(getattr(fi, "right_rect", None))
        lr = _rect(getattr(fi, "left_rect", None))
        w = float(getattr(fi, "img_w", 0) or 640)
        h = float(getattr(fi, "img_h", 0) or 480)
        op_l = getattr(fi, "left_eye_openness", None)
        op_r = getattr(fi, "right_eye_openness", None)
        import math

        m: dict = {"img_w": w, "img_h": h,
                   "face_center_x": (face[0] + face[2] / 2) / w,
                   "face_center_y": (face[1] + face[3] / 2) / h,
                   "face_width_frac": face[2] / w}
        if rr and lr and rr[2] > 1 and lr[2] > 1:
            rc = (rr[0] + rr[2] / 2, rr[1] + rr[3] / 2)
            lc = (lr[0] + lr[2] / 2, lr[1] + lr[3] / 2)
            iod = math.hypot(rc[0] - lc[0], rc[1] - lc[1])
            m["inter_ocular_px"] = round(iod, 1)
            m["eyes_y"] = ((rc[1] + lc[1]) / 2) / h
            m["roll_deg"] = round(math.degrees(
                math.atan2(rc[1] - lc[1], abs(rc[0] - lc[0]) or 1)), 1)
            if iod > 1:
                focal, measured = self._focal_px(w)
                m["est_distance_cm"] = round(self._REAL_IOD_CM * focal / iod, 1)
                m["focal_px"] = round(focal, 1)
                # Whether the distance rests on a MEASURED focal length or
                # on the assumed field of view. Every degree figure in the
                # thesis divides by this distance, so which one it was is
                # a methods fact, not a detail.
                m["focal_measured"] = measured
                m["distance_rel_sd_pct"] = 6.5 if measured else 11.8
        if op_l and op_r and float(op_l) > 0 and float(op_r) > 0:
            m["openness_ratio"] = round(
                max(float(op_l), float(op_r)) / min(float(op_l), float(op_r)),
                2)

        # ── Iris-based distance + cross-check ────────────────────────
        # The IOD is a poor ruler (6.3 cm, SD 0.4 = +-6.3 % biological
        # spread, and it FORESHORTENS with head yaw). The iris is 11.7 mm
        # SD 0.5 (+-4.3 %) and is a physiological constant that does not
        # foreshorten. MediaPipe's refined mesh already computes the iris
        # landmarks every frame, so this costs nothing.
        #
        # Both estimates share the same focal length, so agreement does
        # not prove the distance is right — but DISAGREEMENT proves a
        # measurement is broken, which is the check that did not exist.
        try:
            import iris_distance

            lm = self._attr(fi, ("landmarks", "face_landmarks",
                                 "landmark", "points", "mesh"))
            focal_px, _meas = self._focal_px(w)
            iris = iris_distance.estimate(
                lm, m.get("inter_ocular_px") or 0.0, focal_px, w, h)
            if iris and not iris.get("error"):
                chk = iris.get("check") or {}
                if chk.get("distance_cm"):
                    m["distance_cm_iris"] = (iris.get("from_iris")
                                             or {}).get("distance_cm")
                    m["distance_cm_iod"] = (iris.get("from_iod")
                                            or {}).get("distance_cm")
                    m["distance_agreement_pct"] = chk.get("difference_pct")
                    m["distance_estimates_agree"] = chk.get("agree")
                    # ALWAYS prefer the iris when it is available. It is
                    # the better ruler (4.3 % vs 6.3 % biological spread)
                    # AND it is yaw-invariant, whereas the IOD
                    # foreshortens as cos(yaw): at 35 deg the IOD claims
                    # 73 cm for a head actually at 60.
                    #
                    # Disagreement is therefore a WARNING, not a reason
                    # to fall back to the worse estimate — falling back
                    # would substitute the number most likely to be wrong
                    # precisely when something is known to be wrong.
                    if m["distance_cm_iris"]:
                        m["est_distance_cm"] = m["distance_cm_iris"]
                        m["distance_source"] = "iris"
                        m["distance_rel_sd_pct"] = 4.3 if _meas else 10.9
                    if not chk.get("agree") and chk.get("warning"):
                        m["distance_warning"] = chk["warning"]
                        m["distance_disagreement"] = True
                if chk.get("iris_asymmetry_warning"):
                    m["iris_asymmetry_warning"] = chk["iris_asymmetry_warning"]
        except Exception:  # noqa: BLE001 — never block the position guide
            pass
        return m

    def _guidance_from_metrics(self, m: dict) -> dict:
        """Turn head-position metrics into participant guidance."""
        cx = m.get("face_center_x")
        est_cm = m.get("est_distance_cm")
        eyes_y = m.get("eyes_y")
        roll = m.get("roll_deg")
        ratio = m.get("openness_ratio")
        guidance: list[str] = []
        ready = True
        if cx is not None and cx < 0.40:
            guidance.append("Move slightly to your right"); ready = False
        elif cx is not None and cx > 0.60:
            guidance.append("Move slightly to your left"); ready = False
        if est_cm is not None:
            if est_cm < 45:
                guidance.append("Move back a little (~%d cm)" % est_cm)
                ready = False
            elif est_cm > 75:
                guidance.append("Move closer (~%d cm)" % est_cm)
                ready = False
        if eyes_y is not None and eyes_y > 0.58:
            guidance.append("Raise your laptop so the camera is at eye level "
                            "(your eyes sit low in the frame)")
            ready = False
        elif eyes_y is not None and eyes_y < 0.28:
            guidance.append("Lower your screen slightly — the camera is "
                            "above your eyes")
            ready = False
        if roll is not None and abs(roll) > 6:
            guidance.append("Level your head — it is tilted ~%d°"
                            % abs(roll))
            ready = False
        # Openness asymmetry: appearance-based gaze models degrade badly
        # when one eye patch is much more closed/dim than the other.
        if ratio is not None and ratio > 1.5:
            guidance.append("Your eyes read unevenly (one more closed/dim) — "
                            "face the camera squarely and light your face "
                            "evenly from the front")
            ready = False
        if not guidance:
            guidance.append("Good position — hold still and calibrate.")
        out = {"ok": True, "available": True, "face": True, "ready": ready,
               "assumed_hfov_deg": self._ASSUMED_HFOV_DEG, "guidance": guidance}
        for k in ("face_center_x", "face_center_y", "eyes_y",
                  "inter_ocular_px", "est_distance_cm", "roll_deg",
                  "openness_ratio"):
            if m.get(k) is not None:
                out[k] = round(m[k], 3) if isinstance(m[k], float) else m[k]
        return out

    def cmd_position_info(self) -> dict:
        """Live head-position metrics for the pre-calibration guide.

        Prefers GazeFollower's own face/eye/landmark/openness fields;
        falls back to Haar detection on a grabbed frame; then to
        available:false (UI shows the static positioning tips). Never
        blocks calibration.
        """
        try:
            import cv2

            self._ensure_gf()
            if not self.sampling:
                self.gf.start_sampling()
                self.sampling = True
            # Subscribe to the sample stream so FaceInfo (with face/eye
            # geometry) flows in — GazeInfo alone has none.
            self._ensure_face_subscriber()

            # Primary: GazeFollower's own computed face geometry (FaceInfo).
            try:
                m = self._metrics_from_face_info()
                if m:
                    return self._guidance_from_metrics(m)
            except Exception as exc:  # noqa: BLE001
                log("FaceInfo metrics unavailable (%s) — trying frame" % exc)

            # Camera warm-up: subscriber registered but no frame processed
            # yet. Report a transient "starting" state instead of the
            # dead-end "unavailable" message, which would otherwise flash
            # on the first poll(s) before the first sample arrives.
            if self._face_subscribed and self._latest_face_info is None:
                return {"ok": True, "available": True, "face": False,
                        "warming": True,
                        "guidance": ["Camera starting — look at the screen "
                                     "and hold still…"]}

            # Fallback: Haar detection on a raw frame.
            frame = self._grab_frame()
            if frame is None:
                return {"ok": True, "available": False,
                        "reason": "no face geometry from FaceInfo or frame"}
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (320, int(320 * h / w)))
            sh, sw = small.shape[:2]
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            cascades = cv2.data.haarcascades
            face_cc = cv2.CascadeClassifier(
                cascades + "haarcascade_frontalface_default.xml")
            eye_cc = cv2.CascadeClassifier(cascades + "haarcascade_eye.xml")
            faces = face_cc.detectMultiScale(gray, 1.2, 5,
                                             minSize=(60, 60))
            if len(faces) == 0:
                return {"ok": True, "available": True, "face": False,
                        "guidance": ["Center your face in front of the "
                                     "camera — no face detected."]}
            fx, fy, fw, fh = max(faces, key=lambda b: b[2] * b[3])
            cx = (fx + fw / 2) / sw
            cy = (fy + fh / 2) / sh

            iod_px = None
            eyes_y = None
            roi = gray[fy:fy + fh, fx:fx + fw]
            eyes = eye_cc.detectMultiScale(roi, 1.1, 6, minSize=(18, 18))
            if len(eyes) >= 2:
                eyes = sorted(eyes, key=lambda b: b[2] * b[3],
                              reverse=True)[:2]
                c = [(fx + ex + ew / 2, fy + ey + eh / 2)
                     for ex, ey, ew, eh in eyes]
                iod_px_small = abs(c[0][0] - c[1][0])
                iod_px = iod_px_small * (w / sw)   # back to full-res px
                eyes_y = (c[0][1] + c[1][1]) / 2 / sh

            est_cm = None
            if iod_px and iod_px > 1:
                import math

                focal_px, _measured = self._focal_px(w)
                est_cm = round(self._REAL_IOD_CM * focal_px / iod_px, 1)

            m = {"face_center_x": cx, "face_center_y": cy, "eyes_y": eyes_y,
                 "inter_ocular_px": round(iod_px, 1) if iod_px else None,
                 "est_distance_cm": est_cm}
            return self._guidance_from_metrics(m)
        except Exception as exc:  # noqa: BLE001 — guide is optional
            log("position_info failed: %s" % exc)
            return {"ok": True, "available": False, "reason": str(exc)}

    def cmd_end_session(self, csv_path: str) -> dict:
        """Stop sampling and save the ENTIRE session to one CSV.

        This is the single permitted ``save_data`` call per GazeFollower
        instance (it closes the sample stream irreversibly).
        """
        if self.gf is None or not self.sampling:
            return {"ok": False, "error": "not sampling"}
        self.gf.stop_sampling()
        self.sampling = False
        self.gf.save_data(csv_path)
        log("Session data saved to %s" % csv_path)

        # save_data() closes GazeFollower's sample stream PERMANENTLY —
        # this instance can never record again. Release it so the next
        # participant (in the same server run) gets a fresh instance
        # with a fresh sample stream, instead of silently re-saving
        # this session's stale data.
        try:
            self.gf.release()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            log(traceback.format_exc())
        self.gf = None
        self.calibrated = False
        log("GazeFollower instance released — next session starts fresh.")
        return {"ok": True, "csv": csv_path}

    def cmd_shutdown(self) -> dict:
        """Release camera/model resources."""
        if self.gf is not None:
            try:
                if self.sampling:
                    self.gf.stop_sampling()
                self.gf.release()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                log(traceback.format_exc())
            self.gf = None
        return {"ok": True}

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    @staticmethod
    def _pump_pygame() -> None:
        """Keep the pygame/NSApp event queue alive while idle.

        Without this, macOS flags the process as "not responding"
        whenever a pygame window exists but no code is pumping events.
        """
        try:
            import pygame

            if pygame.get_init() and pygame.display.get_init():
                pygame.event.pump()
        except Exception:  # noqa: BLE001
            pass

    def _read_lines(self):
        """Yield stdin lines, pumping pygame events every 250 ms while
        idle (select-based; falls back to blocking reads if the platform
        doesn't support selecting on stdin).

        On Windows ``select()`` works only on sockets, not stdin/pipes —
        and it fails when ``select()`` is CALLED, not when the selector
        is registered — so we skip the selector path entirely there and
        use plain blocking reads. (macOS pumps its pygame/NSApp queue via
        the selector timeout; on Windows pygame does not need this, since
        calibration is a single blocking call.)"""
        if sys.platform.startswith("win"):
            yield from sys.stdin
            return
        try:
            import selectors

            sel = selectors.DefaultSelector()
            sel.register(sys.stdin, selectors.EVENT_READ)
            while True:
                if sel.select(timeout=0.25):
                    line = sys.stdin.readline()
                    if not line:          # EOF — parent process gone
                        return
                    yield line
                else:
                    self._pump_pygame()
        except Exception:  # noqa: BLE001 — any select/register failure
            yield from sys.stdin

    def run(self) -> None:
        reply({"ok": True, "cmd": "ready"})
        for line in self._read_lines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply({"ok": False, "error": "invalid JSON: %r" % line[:80]})
                continue

            cmd = msg.get("cmd", "")
            try:
                if cmd == "ping":
                    result = {"ok": True}
                elif cmd == "check":
                    result = self.cmd_check()
                elif cmd == "warmup":
                    result = self.cmd_warmup(msg.get("cali_mode"))
                elif cmd == "screen_info":
                    result = self.cmd_screen_info()
                elif cmd == "cycle_sampling":
                    result = self.cmd_cycle_sampling(
                        int(msg.get("cycles", 1)))
                elif cmd == "rate_check_start":
                    result = self.cmd_rate_check_start()
                elif cmd == "rate_check_result":
                    result = self.cmd_rate_check_result(
                        tail_seconds=float(msg.get("tail_seconds", 8.0)))
                elif cmd == "calibrate":
                    result = self.cmd_calibrate(
                        skip=bool(msg.get("skip")),
                        skip_preview=msg.get("skip_preview"),
                        cali_mode=msg.get("cali_mode"),
                    )
                elif cmd == "begin_stimulus":
                    result = self.cmd_begin_stimulus(msg.get("trigger", 0))
                elif cmd == "end_stimulus":
                    result = self.cmd_end_stimulus(msg.get("trigger", 0))
                elif cmd == "telemetry":
                    result = self.cmd_telemetry()
                elif cmd == "gaze_info":
                    result = self.cmd_gaze_info()
                elif cmd == "position_info":
                    result = self.cmd_position_info()
                elif cmd == "end_session":
                    result = self.cmd_end_session(msg.get("csv", "gaze.csv"))
                elif cmd == "shutdown":
                    result = self.cmd_shutdown()
                    result["cmd"] = cmd
                    reply(result)
                    break
                else:
                    result = {"ok": False, "error": "unknown cmd: %s" % cmd}
            except Exception as exc:  # noqa: BLE001 — report, don't crash
                log(traceback.format_exc())
                result = {"ok": False, "error": str(exc)}

            result["cmd"] = cmd
            reply(result)


def _apply_perf_mode_early() -> dict:
    """Opt out of Windows background demotion at process start.

    Applied here rather than lazily in ``_ensure_gf`` for two reasons:
    the policy should be in force before ANY work happens (including the
    self-check's own timings), and the self-check should be able to
    REPORT it as active rather than "not yet applied" — a status line
    that says nothing is worse than no status line.
    """
    try:
        import perf_mode

        return perf_mode.apply(log=log)
    except Exception:  # noqa: BLE001 — must never block a session
        log("Performance mode unavailable:\n" + traceback.format_exc())
        return {}


if __name__ == "__main__":
    _EARLY_PERF = _apply_perf_mode_early()
    if "--check" in sys.argv:
        # Standalone diagnosis:  python tracker_service.py --check
        result = Service().cmd_check()
        print()
        for key, val in result["report"].items():
            print("  %-14s %s" % (key + ":", val))
        print()
        print("  OVERALL: %s" % ("OK — tracker should work"
                                 if result["ok"] else "PROBLEMS FOUND (see FAIL lines)"))
        sys.exit(0 if result["ok"] else 1)
    Service().run()
