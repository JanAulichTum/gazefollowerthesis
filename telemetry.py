# -*- coding: utf-8 -*-
"""
Record everything about a session that might matter later.

WHY THIS EXISTS
---------------
The sampling-rate investigation on this project took days and roughly a
dozen dead ends, and almost every one of them ended the same way: the
question was answerable in principle, but nobody had recorded the number
at the time. "Was it on battery?" "Was perf mode on?" "What size were the
frames?" "Did it degrade over the session or start bad?" Each of those
cost a fresh round of measurement on a machine in a different state.

The eventual root cause — Windows parking the tracker on efficiency
cores — was invisible in every artefact the session produced. It only
became visible once both model stages were timed separately and their
ratio compared. That is the general lesson: a rate is a symptom, and a
symptom without context is a guess.

So this records the context, continuously, at 1 Hz, for every session
including real participants. When something looks wrong three weeks from
now, the answer is already on disk.

WHAT IS RECORDED
----------------
    environment   ~60 one-off facts: OS, CPU, cores, RAM, every package
                  version, MNN backend and threads, perf mode, camera
                  mode, display geometry, thresholds, git-ish file stamps
    series        1 Hz samples: CPU %, RAM, clock, battery, sampling rate,
                  FaceMesh/gaze stage cost, frame size, detection rate,
                  subscriber count, head distance, thread count
    events        a timestamped timeline: calibration, validations, rate
                  gates, each stimulus start/end, errors

COST
----
One background thread, one sample per second, all reads either cached or
sub-millisecond. Measured overhead is well under 0.1 % CPU. This matters
because the machine this was written for runs at ~90 % of its per-frame
budget: telemetry that cost sampling rate would be self-defeating.

Every read is individually guarded. A telemetry failure must NEVER
affect a recording — the worst case is a null in a JSON field.

Disable with ``GF_TELEMETRY=0``.

Read a file back with::

    python diagnose_session.py data/telemetry/<file>.json
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import threading
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
TELEMETRY_DIR = os.path.join(BASE, "data", "telemetry")
SAMPLE_INTERVAL_S = 1.0
# Cap the series so a forgotten server cannot fill the disk: 4 h at 1 Hz.
MAX_SAMPLES = 14400


def enabled() -> bool:
    return os.environ.get("GF_TELEMETRY", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _safe(fn, default=None):
    """Call fn, swallow anything. Telemetry never breaks a session."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


# ──────────────────────────────────────────────────────────────────────
# Environment snapshot — the one-off facts
# ──────────────────────────────────────────────────────────────────────

_PACKAGES = (
    "numpy", "pandas", "cv2", "mediapipe", "MNN", "gazefollower",
    "flask", "flask_socketio", "pygame", "screeninfo", "openpyxl",
    "psutil", "scipy", "sklearn",
)


def _package_versions() -> dict:
    """Version of every package whose behaviour we have been bitten by.

    numpy<2 and mediapipe==0.10.21 are both load-bearing pins on this
    project; recording what was ACTUALLY imported catches the case where
    the pin silently did not hold.
    """
    out = {}
    for name in _PACKAGES:
        def _get(n=name):
            mod = __import__(n)
            return getattr(mod, "__version__", None) \
                or getattr(mod, "version", None) or "unknown"
        out[name] = _safe(_get, "not installed")
    return out


def _cpu_facts() -> dict:
    facts = {
        "processor": _safe(platform.processor, "unknown"),
        "machine": _safe(platform.machine, "unknown"),
        "logical_cores": _safe(os.cpu_count),
    }

    def _ps():
        import psutil

        freq = psutil.cpu_freq()
        return {
            "physical_cores": psutil.cpu_count(logical=False),
            "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 1),
            "cpu_mhz_current": round(freq.current) if freq else None,
            # NOTE: psutil reports the BASE clock as 'max' on Intel, so
            # this is not the turbo ceiling. Recorded for relative
            # comparison across samples, not as an absolute.
            "cpu_mhz_max_reported": round(freq.max) if freq and freq.max
            else None,
        }
    facts.update(_safe(_ps, {}) or {})
    return facts


def _display_facts() -> dict:
    def _mon():
        import screeninfo

        return [
            {"width": m.width, "height": m.height,
             "width_mm": getattr(m, "width_mm", None),
             "height_mm": getattr(m, "height_mm", None),
             "is_primary": bool(getattr(m, "is_primary", False)),
             "name": getattr(m, "name", None)}
            for m in screeninfo.get_monitors()
        ]
    return {"monitors": _safe(_mon, [])}


def _source_stamps() -> dict:
    """Modification times of the files whose behaviour matters.

    Stands in for a commit hash: this project is edited on one machine
    and run on another, and a stale copy has already cost a debugging
    cycle. If a session behaves oddly, this says which code produced it.
    """
    out = {}
    for rel in ("app.py", "tracker_service.py", "config.py", "fixations.py",
                "perf_mode.py", "fake_camera.py", "static/js/experiment.js"):
        def _stamp(r=rel):
            p = os.path.join(BASE, r)
            st = os.stat(p)
            return {"mtime": datetime.fromtimestamp(st.st_mtime).isoformat(
                timespec="seconds"), "bytes": st.st_size}
        out[rel] = _safe(_stamp)
    return out


def _analysis_params() -> dict:
    """The thresholds and constants the numbers will be judged against.

    Recorded per session because they are configurable, and a result is
    not interpretable without knowing which values produced it. The I-DT
    parameters in particular determine the fixation counts, and those
    are currently under review at ~21-30 Hz sampling.
    """
    def _cfg():
        import config

        keys = [k for k in dir(config)
                if k.isupper() and isinstance(
                    getattr(config, k), (int, float, str, bool, list, tuple))]
        return {k: getattr(config, k) for k in sorted(keys)
                if not k.endswith("_DIR") and "PATH" not in k}
    return _safe(_cfg, {}) or {}


def environment_snapshot(extra: dict = None) -> dict:
    """Everything worth knowing that does not change during a session."""
    snap = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "hostname": _safe(socket.gethostname, "unknown"),
        "platform": _safe(platform.platform, "unknown"),
        "os_release": _safe(platform.release, "unknown"),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "in_venv": bool(getattr(sys, "base_prefix", sys.prefix) != sys.prefix),
        "cwd": os.getcwd(),
        "cpu": _cpu_facts(),
        "display": _display_facts(),
        "packages": _package_versions(),
        "sources": _source_stamps(),
        "config": _analysis_params(),
        "env_vars": {k: os.environ.get(k) for k in (
            "GF_PERF_MODE", "GF_PERF_PRIORITY", "GF_PERF_PIN_CORES",
            "GF_MNN_BACKEND", "GF_MNN_THREADS", "GF_MNN_PRECISION",
            "GF_CAMERA_FIX", "GF_FAKE_CAMERA", "GF_FAKE_CALIBRATION",
            "GF_TELEMETRY", "GF_SAMPLE_PATCH", "PYTHONUTF8",
        ) if os.environ.get(k) is not None},
    }

    def _perf():
        import perf_mode

        return {"describe": perf_mode.describe(), "enabled": perf_mode.enabled()}
    snap["perf_mode"] = _safe(_perf, {})

    def _power():
        import psutil

        batt = psutil.sensors_battery()
        return {"on_ac_power": bool(batt.power_plugged),
                "battery_pct": round(batt.percent)} if batt else {}
    snap["power_at_start"] = _safe(_power, {})

    if extra:
        snap.update(extra)
    return snap


# ──────────────────────────────────────────────────────────────────────
# The recorder
# ──────────────────────────────────────────────────────────────────────

class Telemetry:
    """Per-session recorder: snapshot + 1 Hz series + event timeline.

    Thread-safe and fail-safe. ``probe`` is an optional callable the
    sampler invokes each tick to pull tracker-side numbers (rate, stage
    costs, head distance); it must be cheap and must not block, because
    it runs on the sampler thread while a session is recording.
    """

    def __init__(self, session_id: str, probe=None, extra: dict = None):
        self.session_id = session_id
        self.started_monotonic = time.monotonic()
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.environment = environment_snapshot(extra) if enabled() else {}
        self.series: list = []
        self.events: list = []
        self.probe = probe
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._sampler_errors = 0

    # ── timeline ──
    def event(self, name: str, **fields) -> None:
        """Record a timestamped event. Safe to call from any thread."""
        if not enabled():
            return
        try:
            entry = {"t": round(time.monotonic() - self.started_monotonic, 3),
                     "utc": datetime.now(timezone.utc).isoformat(),
                     "event": str(name)[:80]}
            for k, v in (fields or {}).items():
                entry[str(k)[:40]] = v
            with self._lock:
                self.events.append(entry)
        except Exception:  # noqa: BLE001
            pass

    # ── 1 Hz series ──
    def _tick(self) -> dict:
        row = {"t": round(time.monotonic() - self.started_monotonic, 2)}

        def _ps():
            import psutil

            vm = psutil.virtual_memory()
            proc = psutil.Process()
            freq = psutil.cpu_freq()
            batt = psutil.sensors_battery()
            out = {
                # interval=None => non-blocking, delta since last call.
                # A blocking call here would sleep the sampler and could
                # perturb the very thing being measured.
                "cpu_pct_system": psutil.cpu_percent(interval=None),
                "cpu_pct_process": proc.cpu_percent(interval=None),
                "ram_used_pct": vm.percent,
                "ram_available_gb": round(vm.available / 1e9, 2),
                "proc_rss_mb": round(proc.memory_info().rss / 1e6, 1),
                "proc_threads": proc.num_threads(),
                "cpu_mhz": round(freq.current) if freq else None,
            }
            if batt is not None:
                out["on_ac_power"] = bool(batt.power_plugged)
                out["battery_pct"] = round(batt.percent)
            return out
        row.update(_safe(_ps, {}) or {})

        if self.probe is not None:
            probed = _safe(self.probe, None)
            if isinstance(probed, dict):
                row.update(probed)
        return row

    def _loop(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL_S):
            try:
                row = self._tick()
                with self._lock:
                    if len(self.series) < MAX_SAMPLES:
                        self.series.append(row)
            except Exception:  # noqa: BLE001
                self._sampler_errors += 1

    def start(self) -> "Telemetry":
        if not enabled() or self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="telemetry")
        self._thread.start()
        self.event("telemetry_started")
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ── output ──
    def to_dict(self) -> dict:
        with self._lock:
            series = list(self.series)
            events = list(self.events)
        return {
            "session_id": self.session_id,
            "started_utc": self.started_utc,
            "ended_utc": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(time.monotonic() - self.started_monotonic, 1),
            "sample_interval_s": SAMPLE_INTERVAL_S,
            "sampler_errors": self._sampler_errors,
            "environment": self.environment,
            "events": events,
            "series": series,
        }

    def save(self, directory: str = None) -> "str | None":
        """Write the telemetry file. Returns its path, or None."""
        if not enabled():
            return None
        try:
            directory = directory or TELEMETRY_DIR
            os.makedirs(directory, exist_ok=True)
            safe_id = "".join(c if (c.isalnum() or c in "-_") else "_"
                              for c in str(self.session_id))[:80]
            path = os.path.join(
                directory, "%s_%s_telemetry.json"
                % (datetime.now().strftime("%Y-%m-%d_%H%M%S"), safe_id))
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2, default=str)
            os.replace(tmp, path)
            return path
        except Exception:  # noqa: BLE001
            return None


def summarise(data: dict) -> dict:
    """Condense a telemetry file into the handful of numbers worth
    putting in the session manifest, so the manifest stays readable."""
    series = data.get("series") or []

    def _col(key):
        return [r[key] for r in series
                if isinstance(r.get(key), (int, float))]

    def _stat(key):
        vals = sorted(_col(key))
        if not vals:
            return None
        return {"min": round(vals[0], 1),
                "median": round(vals[len(vals) // 2], 1),
                "max": round(vals[-1], 1)}

    return {
        "samples": len(series),
        "duration_s": data.get("duration_s"),
        "cpu_pct_system": _stat("cpu_pct_system"),
        "cpu_mhz": _stat("cpu_mhz"),
        "sampling_hz": _stat("sampling_hz"),
        "face_ms": _stat("face_ms_median"),
        "gaze_ms": _stat("gaze_ms_median"),
        "ram_used_pct": _stat("ram_used_pct"),
        "on_ac_power": next((r.get("on_ac_power") for r in series
                             if "on_ac_power" in r), None),
        "events": len(data.get("events") or []),
        "sampler_errors": data.get("sampler_errors"),
    }
