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


#: Everything the position payload carries into the session manifest.
#: Module level, and shared with the --distance probe, because the probe
#: exists to answer "will a session record this?" - and it can only
#: answer that if it is looking at the same list the session uses. The
#: bug this replaces was a field that app.py read and the tracker never
#: sent; a second copy of the list would reintroduce it.
POSITION_FIELDS = (
    "face_center_x", "face_center_y", "eyes_y",
    "inter_ocular_px", "est_distance_cm", "roll_deg", "openness_ratio",
    "distance_source", "distance_cm_iris", "distance_cm_iod",
    "distance_agreement_pct", "distance_estimates_agree",
    "distance_rel_sd_pct", "distance_warning", "distance_disagreement",
    "iris_error", "iris_traceback", "iris_landmarks_from",
    "iris_asymmetry_warning",
    "focal_px", "focal_measured",
)

#: Of those, the ones without which a distance cannot be attributed to a
#: ruler. A session missing these records a number and not a measurement.
POSITION_REQUIRED = ("est_distance_cm", "distance_source")

_IRIS_MESH = None


def refined_landmarks_for_frame(frame):
    """478-point landmarks for a BGR frame, or None.

    The refined mesh is the only one that carries the iris points
    (468-477), and GazeFollower does not use it — its FaceInfo holds the
    coarse 468-point mesh, which is why the iris ruler silently never
    ran. Built lazily and reused: the FaceMesh constructor is expensive,
    the inference is not.

    Module-level ON PURPOSE. The live session reaches it through
    ``Service._refined_landmarks`` with a frame grabbed from
    GazeFollower's camera, and the standalone probe reaches it with a
    frame it captured itself. A probe that verified its own private copy
    of this code would verify nothing about the session.

    Never raises — an unavailable iris must degrade to the inter-ocular
    estimate, not stop a validation.
    """
    global _IRIS_MESH
    try:
        import cv2

        if frame is None:
            return None
        if _IRIS_MESH is None:
            import mediapipe as mp

            _IRIS_MESH = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=False, max_num_faces=1,
                refine_landmarks=True,     # <- the whole point
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5)
            log("Refined FaceMesh created for iris measurement "
                "(GazeFollower's own mesh has no iris landmarks).")
        res = _IRIS_MESH.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            return None
        return res.multi_face_landmarks[0].landmark
    except Exception as exc:  # noqa: BLE001
        log("Refined mesh unavailable (%s) — falling back to the "
            "inter-ocular distance." % exc)
        return None


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
        self._install_callback_timer(gf)
        return True

    def _install_callback_timer(self, gf) -> bool:
        """Time the WHOLE per-frame callback, not just the two models.

        WHY THE MODEL SPLIT IS NOT ENOUGH
        ---------------------------------
        The stage timers cover ``face_alignment.detect`` and
        ``gaze_estimator.detect``. They do NOT cover the rest of the
        callback: GazeFollower's own filtering, its subscriber dispatch,
        the CSV write and flush it performs per sample, and our
        ``_on_sample``. On a real webcam ``capture`` is None, so
        ``callback_ms_median`` was unavailable and the duty calculation
        silently used the MODEL cost as if it were the whole cost.

        That gap produced a wrong diagnosis on this project: 18 ms of
        models inside a 66.7 ms frame interval reads as 27 % duty and
        "the pipeline is idle waiting for the camera" — while the camera
        was independently measured at 31 fps. Both facts are true, and
        together they mean the missing time is in the untimed remainder
        of the callback.

        This matters because the callback runs SYNCHRONOUSLY inside the
        capture loop. The loop cannot begin the next frame until the
        callback returns, so the moment total callback work crosses the
        frame period (33.3 ms at 30 fps) the loop misses every second
        frame and the rate does not degrade gracefully — it HALVES.
        30.2 -> 15.0 is that signature exactly, and no partial timing can
        see it.

        Also records frame ARRIVAL timestamps, which give the delivered
        camera rate on a real webcam for the first time: if frames arrive
        at 30 Hz but samples leave at 15 Hz, the loss is downstream of
        capture, which is a different bug again.
        """
        if getattr(self, "_callback_timer_installed", False):
            return True
        cam = getattr(gf, "camera", None)
        orig = getattr(gf, "process_frame", None)
        if cam is None or not callable(orig):
            log("Callback timer NOT installed (camera=%s, process_frame=%s) "
                "— total per-frame cost will be unavailable and the duty "
                "figure will understate the work."
                % (type(cam).__name__ if cam else None, callable(orig)))
            return False

        self._callback_ms = collections.deque(maxlen=4000)
        self._frame_arrivals = collections.deque(maxlen=4000)

        def timed_process(state, timestamp, frame):
            t0 = time.perf_counter()
            self._frame_arrivals.append(t0)
            # KEEP THE FRAME. GazeFollower owns the camera during a
            # session, so _grab_frame() had nothing to read and the
            # refined FaceMesh never ran — which is why every recorded
            # session fell back to the inter-ocular ruler while the
            # standalone probe, which opens the camera itself, measured
            # the iris on 100 % of frames. This callback already receives
            # every frame; holding a reference costs nothing and is the
            # only place the frame is available.
            self._last_frame = frame
            try:
                return orig(state, timestamp, frame)
            finally:
                self._callback_ms.append(
                    (time.perf_counter() - t0) * 1000.0)

        # BOTH, and in this order. ``gf.process_frame`` is the method
        # GazeFollower calls internally; ``set_on_image_callback`` is what
        # the camera thread actually invokes, and it captured a reference
        # to the ORIGINAL bound method when sampling was first set up.
        # Rebinding only the attribute would leave the camera calling the
        # untimed original — a timer that installs cleanly, reports
        # nothing, and is indistinguishable from "the callback is free".
        # diagnose_rate.py does both for the same reason.
        gf.process_frame = timed_process
        try:
            cam.set_on_image_callback(timed_process)
        except Exception as exc:  # noqa: BLE001
            gf.process_frame = orig
            log("Callback timer NOT installed: set_on_image_callback failed "
                "(%s). Total per-frame cost unavailable." % exc)
            return False

        self._callback_timer_installed = True
        log("Callback timer installed on process_frame + "
            "%s.set_on_image_callback (total per-frame cost and delivered "
            "camera rate)." % type(cam).__name__)
        return True

    def _callback_stats(self) -> "dict | None":
        """Total per-frame callback cost and the delivered camera rate."""
        cb = sorted(getattr(self, "_callback_ms", ()) or ())
        arrivals = list(getattr(self, "_frame_arrivals", ()) or ())
        if not cb:
            return None
        out = {
            "callback_ms_median": round(statistics.median(cb), 1),
            "callback_ms_p90": round(
                cb[min(len(cb) - 1, int(0.90 * len(cb)))], 1),
            "n_callbacks": len(cb),
        }
        if len(arrivals) >= 10:
            gaps = sorted(b - a for a, b in zip(arrivals[:-1], arrivals[1:])
                          if b > a)
            if gaps:
                out["delivered_hz"] = round(
                    1.0 / statistics.median(gaps), 1)
        return out

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
        # Total per-frame callback cost. Prefer the fake camera's own
        # counter where it exists; otherwise the live wrapper, which is
        # the only source on a real webcam.
        live_cb = self._callback_stats()
        result["callback"] = live_cb
        stages = result["stages"]
        if stages:
            models = stages.get("models_ms_median") or 0.0
            cb = ((capture or {}).get("callback_ms_median")
                  or (live_cb or {}).get("callback_ms_median") or 0.0)
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
            # ── BOTTLENECK ATTRIBUTION ───────────────────────────────
            # A low rate has three causes with three different fixes,
            # and the rate alone cannot tell them apart:
            #
            #   1 the per-frame WORK fills the frame interval
            #       -> the machine is the limit (EcoQoS demotion,
            #          thermal, contention). Fix the machine.
            #   2 the work is cheap AND the camera is delivering slowly
            #       -> the camera is the limit. The usual cause is
            #          auto-exposure: a webcam cannot integrate for
            #          longer than one frame period, so in dim light it
            #          halves the rate to buy exposure time. Fix the
            #          light.
            #   3 the work is cheap AND the camera is delivering fast
            #       -> frames are arriving and being THROWN AWAY
            #          downstream. Neither the machine nor the light
            #          will help.
            #
            # The critical word is WORK, and getting it wrong is how
            # this project produced a confidently wrong diagnosis: the
            # duty figure used the two MODEL stages (18 ms) as if they
            # were the whole callback, concluded the pipeline was idle
            # 73 % of every frame, and blamed the camera — which was
            # independently measured at 31 fps moments later. The
            # untimed remainder of the callback (GazeFollower's filter,
            # its subscriber dispatch, the per-sample CSV write and
            # flush, our own handler) is now included, because that
            # remainder is exactly where a synchronous capture loop
            # loses its frames.
            #
            # And note WHY the failure is a clean halving rather than a
            # gradual slide: the callback runs INSIDE the capture loop,
            # so the loop cannot start frame N+1 until frame N returns.
            # The moment total work crosses the frame period the loop
            # misses every second frame. 30.2 -> 15.0 is that, not a
            # camera.
            work = cb or models
            delivered = (live_cb or {}).get("delivered_hz")
            if work and sustained > 0:
                interval_ms = 1000.0 / sustained
                result["frame_interval_ms"] = round(interval_ms, 1)
                result["work_ms_median"] = round(work, 1)
                result["work_is_models_only"] = not bool(cb)
                result["pipeline_duty_pct"] = round(
                    100.0 * work / interval_ms, 1)
                low = sustained < 0.85 * NOMINAL_CAMERA_FPS
                busy = work >= 0.60 * interval_ms
                # The camera is only exonerated when it was MEASURED to
                # be fast. Absent that measurement, "cheap work + low
                # rate" is genuinely ambiguous and must say so rather
                # than pick the flattering explanation.
                cam_fast = bool(delivered
                                and delivered >= 0.85 * NOMINAL_CAMERA_FPS)
                cam_slow = bool(delivered
                                and delivered < 0.85 * NOMINAL_CAMERA_FPS)
                result["delivered_hz"] = delivered
                # THE non-circular data-loss figure. detected_pct is
                # counted inside _on_sample, which only ever sees frames
                # that already produced a sample — so it reports 100 %
                # even when half the frames never got there. (The same
                # self-referential trap as the old gaze_samples_pct.)
                # Comparing samples OUT against frames IN to the callback
                # is the first measurement here that can actually see a
                # dropped frame.
                if delivered:
                    result["sample_yield_pct"] = round(
                        100.0 * min(1.0, sustained / delivered), 1)
                result["cpu_throttled"] = bool(low and busy)
                result["camera_throttled"] = bool(
                    low and not busy and cam_slow)
                result["frames_discarded"] = bool(
                    low and not busy and cam_fast)
                result["bottleneck_unclear"] = bool(
                    low and not busy and delivered is None)

                if result["cpu_throttled"]:
                    log("Bottleneck: PER-FRAME WORK. The callback takes "
                        "%.1f ms of a %.1f ms frame interval (%.0f %% duty; "
                        "%.1f ms of that is the two models, %.1f ms is "
                        "everything else). Because the callback runs inside "
                        "the capture loop, work above the frame period "
                        "makes the loop skip alternate frames — which is "
                        "why the rate halves instead of sagging. Check perf "
                        "mode, the subscriber count (%s; 2 is correct, each "
                        "extra one is another CSV write per frame), "
                        "thermals and other running processes."
                        % (work, interval_ms, result["pipeline_duty_pct"],
                           models, result["overhead_ms_median"] or 0.0,
                           result["subscribers"]))
                elif result["camera_throttled"]:
                    log("Bottleneck: THE CAMERA. It delivered only %.1f Hz "
                        "to the callback, while the callback itself took "
                        "%.1f ms of the %.1f ms interval (%.0f %% duty). "
                        "Most likely auto-exposure lengthening in dim "
                        "light; confirm with camera_remedy.py."
                        % (delivered, work, interval_ms,
                           result["pipeline_duty_pct"]))
                elif result["frames_discarded"]:
                    log("Bottleneck: NEITHER the camera nor the CPU. The "
                        "camera delivered %.1f Hz to the callback and the "
                        "callback took only %.1f ms of the %.1f ms "
                        "interval (%.0f %% duty), yet samples emerged at "
                        "%.1f Hz — so roughly %.0f %% of frames produced "
                        "no sample. The loss is between the callback and "
                        "the sample stream: check detection failures "
                        "(%s %% detected) and the subscriber count (%s)."
                        % (delivered, work, interval_ms,
                           result["pipeline_duty_pct"], sustained,
                           100.0 * max(0.0, delivered - sustained) / delivered,
                           result["detected_pct"], result["subscribers"]))
                elif result["bottleneck_unclear"]:
                    log("Bottleneck: UNRESOLVED. The callback takes only "
                        "%.1f ms of a %.1f ms interval (%.0f %% duty), so "
                        "per-frame work is not the limit — but the "
                        "delivered camera rate was not measured, so the "
                        "camera and downstream frame loss cannot be told "
                        "apart. Run camera_remedy.py to measure the camera "
                        "directly." % (work, interval_ms,
                                       result["pipeline_duty_pct"]))
        # ALWAYS log the callback figures, passing or failing. A control
        # measurement is only worth having if it exists for the healthy
        # case too: diagnose_rate.py reports 31.1 Hz at 17.1 ms per frame
        # with 0.2 ms of non-model work, and that number is only useful
        # if the live app prints the comparable one on every run rather
        # than only when something has already gone wrong.
        if live_cb:
            log("Callback (live): total %s ms median / %s ms p90 over %d "
                "frames | camera delivered %s Hz INTO the callback, %s Hz "
                "of samples came OUT (%s %% yield) | subscribers %s"
                % (live_cb.get("callback_ms_median"),
                   live_cb.get("callback_ms_p90"),
                   live_cb.get("n_callbacks"),
                   live_cb.get("delivered_hz"), result["sustained_hz"],
                   result.get("sample_yield_pct"), result["subscribers"]))
        else:
            log("Callback (live): NOT MEASURED — the timer did not install, "
                "so total per-frame cost and the delivered camera rate are "
                "unavailable and any duty figure below counts only the two "
                "model stages.")
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
                   # NOTE: capture_limited means only that nothing is
                   # lost BETWEEN capture and the sample stream. It does
                   # NOT by itself prove the per-frame work is expensive
                   # — a camera delivering 15 fps because of exposure
                   # also produces served == sustained. over_frame_budget
                   # is the flag that separates those two.
                   ("no downstream loss; per-frame work is OVER budget, "
                    "so the capture loop is the limit"
                    if result.get("over_frame_budget") else
                    "no downstream loss, and per-frame work is UNDER "
                    "budget — so the camera itself is delivering slowly")
                   if result.get("capture_limited")
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

    def _refined_landmarks(self):
        """478-point landmarks for the CURRENT frame, or None.

        Never raises — an unavailable iris must degrade to the
        inter-ocular estimate, not stop a validation.
        """
        frame = self._grab_frame()
        if frame is None:
            return None
        return refined_landmarks_for_frame(frame)

    def _grab_frame(self):
        """Best-effort read of GazeFollower's latest camera frame.

        The callback wrapper stashes every frame it sees, so during a
        session this returns immediately and correctly. The attribute
        probing below is the fallback for the window before sampling
        starts, and is what used to return None for the whole session.

        GazeFollower owns the webcam, so we cannot open it separately.
        Different versions expose the frame under different attributes;
        try the known paths and return ``None`` if none work (the guide
        then degrades to static advice — it never blocks calibration).
        """
        frame = getattr(self, "_last_frame", None)
        if frame is not None and getattr(frame, "ndim", 0) == 3:
            return frame
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
                # Name the ruler HERE, so the field is never blank. The
                # iris block below overwrites it when it succeeds; if it
                # does not, a reader still learns which measurement
                # produced the distance every degree divides by.
                #
                # A blank source is what the 2026-08-11 session showed
                # ("measured at the pre_check check via ?"), and it hid
                # the fact that the iris estimate had failed and the
                # figure came from GazeFollower's EYE RECTANGLES — whose
                # centres are not guaranteed to be pupil centres, which
                # is the thing POPULATION_IOD_CM describes.
                m["distance_source"] = "inter-ocular (GazeFollower eye rects)"
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
            # GazeFollower's FaceInfo carries the COARSE 468-point mesh.
            # The iris landmarks are 468-477 and simply do not exist
            # there, so the better ruler was never available and every
            # session silently fell back to the eye rectangles — whose
            # centres are not the pupil centres that the 6.3 cm
            # inter-pupillary constant describes.
            #
            # Run our OWN refined mesh on the current frame instead.
            # This is affordable because it happens on demand, at
            # validation time, not on the per-frame path: one extra
            # FaceMesh pass costs ~10 ms and buys the physiological
            # constant (iris 11.7 mm +- 0.5, ~4 %) in place of a
            # population mean applied to the wrong landmarks (~11 %,
            # and yaw-dependent).
            # `lm is None`, NOT `not lm`. GazeFollower's FaceInfo
            # carries the landmarks as a NUMPY ARRAY, and `not array`
            # raises ValueError: the truth value of an array with more
            # than one element is ambiguous. That exception was swallowed
            # by a bare except for every session ever recorded, so the
            # iris ruler never ran and the distance silently came from
            # the inter-ocular fallback. len() works on both a list and
            # an array; truthiness does not.
            if lm is None or len(lm) < 478:
                own = self._refined_landmarks()
                if own is not None:
                    lm = own
                    m["iris_landmarks_from"] = "own refined FaceMesh"
            focal_px, _meas = self._focal_px(w)
            iris = iris_distance.estimate(
                lm, m.get("inter_ocular_px") or 0.0, focal_px, w, h)
            # WHY the iris failed, when it does. The iris is the better
            # ruler and the pipeline is built to prefer it, so a session
            # that silently fell back to the inter-ocular distance
            # should say so rather than leaving the reader to infer it
            # from an empty field. The usual cause is that
            # GazeFollower's FaceInfo carries the COARSE 468-point mesh:
            # the iris points are 468-477 and simply do not exist there.
            if lm is None or len(lm) == 0:
                m["iris_error"] = ("no landmarks on FaceInfo — cannot "
                                   "measure the iris")
            elif iris and iris.get("error"):
                m["iris_error"] = str(iris["error"])[:120]
            elif iris and (iris.get("iris") or {}).get("error"):
                # estimate() catches its own failures and reports them
                # INSIDE the "iris" block; only a raised exception lands
                # at the top level. Checking one level was why every
                # session recorded iris_error as null while silently
                # using the worse ruler.
                m["iris_error"] = str(iris["iris"]["error"])[:120]
            elif not iris:
                m["iris_error"] = "iris estimate returned nothing"

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
        except Exception as exc:  # noqa: BLE001 — never block the guide
            # RECORD it. `pass` here is why PILOT_01 reported a distance
            # from the inter-ocular fallback with iris_error empty: the
            # iris block raised somewhere before it could set its own
            # error field, and the exception went into the void. A
            # silent fallback to a ruler that reads 73.8 cm where the
            # iris reads 54.2 is a 36 % error in every angle of that
            # session, arriving with no evidence that anything happened.
            m["iris_error"] = "%s: %s" % (type(exc).__name__, exc)[:160]
            m["iris_traceback"] = traceback.format_exc()[-400:]
            log("Iris distance failed (%s) — falling back to the "
                "inter-ocular estimate. %s" % (type(exc).__name__, exc))
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
        # The whitelist carried est_distance_cm but NOT the fields that
        # say where it came from, so the manifest recorded a distance of
        # 68.3 cm with source, iris and iod all null — a number with no
        # provenance, presented in the session summary as "MEASURED".
        #
        # It also meant the iris/inter-ocular cross-check never reached
        # the session record, so the one place the two rulers are
        # measured on the same frames could not be inspected. Everything
        # computed alongside the distance now travels with it.
        for k in POSITION_FIELDS:
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


def _distance_probe(seconds: float = 10.0) -> int:
    """Which ruler measures the head distance — live, without recording.

    Every accuracy figure in this study is an ANGLE, and an angle is
    pixels divided by a distance. If the iris measurement fails, the
    code falls back to an inter-ocular estimate whose population spread
    is ~11 % rather than ~4 %, and it does so silently: the number still
    appears, still looks reasonable, and every degree in the thesis is
    quietly scaled by it.

    WHY THIS DOES NOT GO THROUGH GAZEFOLLOWER
    -----------------------------------------
    The obvious probe — start sampling and read the position guide —
    cannot work before a calibration exists. In SAMPLING state
    GazeFollower calls ``calibration.predict`` and RAISES when no model
    has been fitted, and it does so BEFORE ``dispatch_face_gaze_info``:

        gaze_info = self.gaze_estimator.detect(frame, face_info)
        if gaze_info.status ...:
            calibrated, coords = self.calibration.predict(...)
            if not calibrated:
                raise Exception("No calibration model is available")
        self.dispatch_face_gaze_info(face_info, gaze_info)   # never reached

    So no FaceInfo is ever dispatched, no metrics exist, and the probe
    would report "no face" — blaming the camera for a calibration state.
    GazeFollower also never persists a calibration between runs, so
    there is no fitted model to borrow.

    What this probe therefore does is capture its own frames and run
    ``refined_landmarks_for_frame`` — the SAME function the live session
    uses — followed by the same ``iris_distance.estimate``. That covers
    the part that actually failed before (the coarse mesh has no iris
    landmarks) without needing a calibration.

    WHAT IT DOES NOT COVER: the plumbing from that measurement into the
    manifest. That is verified on the first real session by reading
    ``head_distance_cm`` — it names its own ruler.

    Requires the camera to be FREE: close any running session first.
    """
    print("=" * 66)
    print("  DISTANCE PROBE — which ruler is actually measuring?")
    print("=" * 66)
    print("  Sit as you would for a session and look at the camera.")
    print("  %.0f seconds. Nothing is recorded, no session is created."
          % seconds)
    print()

    try:
        import cv2

        import camera_geometry
        import iris_distance
    except Exception as exc:  # noqa: BLE001
        print("  cannot import what the probe needs: %s" % exc)
        return 1

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("  CAMERA BUSY OR UNAVAILABLE.")
        print("  Close any running session (the webcam has one owner)")
        print("  and try again.")
        return 1

    ok_iris = 0
    frames = 0
    faces = 0
    dists: list = []
    iod_dists: list = []
    errors: dict = {}
    focal_px = None
    focal_measured = False
    try:
        t_end = time.time() + seconds
        while time.time() < t_end:
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frames += 1
            h, w = frame.shape[0], frame.shape[1]
            if focal_px is None:
                geom = camera_geometry.load() or {}
                focal_px = geom.get("focal_px")
                focal_measured = bool(focal_px)
                if not focal_px:
                    # Same assumed 60 deg HFOV the service falls back to.
                    focal_px = (w / 2.0) / math.tan(math.radians(60.0 / 2))
            lm = refined_landmarks_for_frame(frame)
            if lm is None:
                continue
            faces += 1
            res = iris_distance.estimate(lm, 0.0, focal_px, w, h)
            if res.get("error"):
                e = str(res["error"])[:100]
                errors[e] = errors.get(e, 0) + 1
                continue
            iris_blk = res.get("iris") or {}
            if iris_blk.get("error"):
                e = str(iris_blk["error"])[:100]
                errors[e] = errors.get(e, 0) + 1
            cm = ((res.get("from_iris") or {}) or {}).get("distance_cm")
            if cm:
                ok_iris += 1
                dists.append(float(cm))
            iod_cm = ((res.get("from_iod") or {}) or {}).get("distance_cm")
            if iod_cm:
                iod_dists.append(float(iod_cm))
            time.sleep(0.05)
    finally:
        cap.release()

    print("  frames read        : %d" % frames)
    print("  frames with a face : %d" % faces)
    print("  focal length       : %.1f px (%s)"
          % (focal_px or 0.0,
             "MEASURED — camera_geometry.json" if focal_measured
             else "ASSUMED 60 deg HFOV — run the focal calibration"))
    if dists:
        dists.sort()
        print("  iris distance      : %.1f cm median (range %.1f-%.1f)"
              % (dists[len(dists) // 2], dists[0], dists[-1]))
    for e, n in errors.items():
        print("  iris error         : %s  (x%d)" % (e, n))
    print()

    if not frames:
        print("  THE CAMERA RETURNED NO FRAMES. Not a ruler result — a")
        print("  camera problem. Fix that first.")
        return 1
    if not faces:
        print("  NO FACE was detected in any frame. Not a ruler result —")
        print("  a lighting or positioning problem. Fix that first.")
        return 1

    # THE PAYLOAD A SESSION WOULD RECORD, built from this measurement
    # through the same field list the live path uses. The probe proved
    # only that the iris COULD be measured; it could not say whether the
    # measurement would reach the manifest with its provenance intact -
    # and it did not, for every session recorded so far.
    _mid = dists[len(dists) // 2] if dists else None
    payload = {
        "est_distance_cm": round(_mid, 1) if _mid else None,
        "distance_source": "iris" if ok_iris else None,
        "distance_cm_iris": round(_mid, 1) if _mid else None,
        "focal_px": round(focal_px, 1) if focal_px else None,
        "focal_measured": focal_measured,
        "iris_error": (sorted(errors)[0] if errors else None),
    }
    print("  WHAT A SESSION WOULD RECORD")
    for _k in POSITION_FIELDS:
        if payload.get(_k) is not None:
            print("    %-24s %s" % (_k, payload[_k]))
    _missing = [k for k in POSITION_REQUIRED if payload.get(k) is None]
    if _missing:
        print()
        print("    MISSING: %s" % ", ".join(_missing))
        print("    A distance without a source is a number, not a")
        print("    measurement. Do not record a participant.")
        return 1
    print()

    share = 100.0 * ok_iris / faces
    if ok_iris >= 0.5 * faces:
        print("  PASS — the iris measured %.0f %% of the frames that had a"
              % share)
        print("  face. Distances rest on an 11.7 mm anatomical constant")
        print("  with a ~4 % population spread, not on a population mean")
        print("  applied to eye-rectangle centres.")
        if not focal_measured:
            print()
            print("  BUT the focal length is ASSUMED, so the distance is")
            print("  only as good as a guessed field of view. Run the")
            print("  focal calibration (menu 7 -> c) to make it measured.")
            return 1
        print()
        print("  Confirm on the first session: head_distance_cm should")
        print("  read 'via iris', not 'via UNKNOWN RULER'.")
        return 0
    print("  FALLBACK IN USE — the iris measured only %.0f %% of frames"
          % share)
    print("  with a face. Distances would come from the inter-ocular")
    print("  estimate, whose population spread is ~11 % and which uses")
    print("  eye-rect centres that are not pupil centres. Every accuracy")
    print("  figure in degrees inherits that. Report it as a limitation,")
    print("  or fix the iris path before collecting.")
    return 1


if __name__ == "__main__":
    _EARLY_PERF = _apply_perf_mode_early()
    if "--distance" in sys.argv:
        # python tracker_service.py --distance
        _secs = 10.0
        for _i, _a in enumerate(sys.argv):
            if _a == "--seconds" and _i + 1 < len(sys.argv):
                _secs = float(sys.argv[_i + 1])
        sys.exit(_distance_probe(_secs))
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
