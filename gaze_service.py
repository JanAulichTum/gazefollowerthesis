# -*- coding: utf-8 -*-
"""
Flask-side manager for the GazeFollower tracker subprocess.

Spawns ``tracker_service.py`` as a child process and exposes a small,
thread-safe API for the Flask app:

    service = GazeService()
    service.calibrate()                    # opens native calibration window
    service.start_recording()              # begin sampling
    csv = service.stop_recording(path)     # stop + save samples to CSV

If GazeFollower is not installed or the subprocess dies, every method
degrades gracefully (returns False / None) so the UI can surface the
problem instead of crashing — but no gaze data will be recorded.
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
from typing import Optional

from config import DATA_DIR

logger = logging.getLogger(__name__)

MARKER = "@GF@"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_SCRIPT = os.path.join(BASE_DIR, "tracker_service.py")

# All tracker subprocess diagnostics (including full tracebacks) are
# appended here — check this file first when calibration fails.
TRACKER_LOG_FILE = os.path.join(DATA_DIR, "tracker_service.log")

# Generous timeouts (seconds): model loading and manual calibration are slow.
TIMEOUT_READY = 15
TIMEOUT_CALIBRATE = 600     # participant works through the calibration UI
TIMEOUT_COMMAND = 30


class GazeService:
    """Thread-safe wrapper around the tracker subprocess."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._replies: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.Lock()        # serialises command/response pairs
        self.calibrated = False
        self._status_cb = None               # forwards async progress events

    def set_status_callback(self, cb) -> None:
        """Register a callable receiving ``{"stage": …}`` progress events."""
        self._status_cb = cb

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def _ensure_process(self) -> bool:
        """Start the subprocess if it isn't running. Returns availability."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            log_fh = open(  # noqa: SIM115 — handle lives as long as the process
                TRACKER_LOG_FILE, "a", buffering=1, encoding="utf-8"
            )
            log_fh.write("\n===== tracker service starting =====\n")
            self._proc = subprocess.Popen(
                [sys.executable, SERVICE_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_fh,              # full tracebacks → log file
                cwd=BASE_DIR,
                text=True,
                bufsize=1,
            )
            logger.info("Tracker diagnostics → %s", TRACKER_LOG_FILE)
        except OSError:
            logger.exception("Could not spawn tracker_service.py")
            self._proc = None
            return False

        # Drain stdout on a daemon thread, queueing protocol replies.
        threading.Thread(
            target=self._reader, args=(self._proc,), daemon=True
        ).start()

        reply = self._wait_reply(TIMEOUT_READY)
        if reply is None or not reply.get("ok"):
            logger.error("Tracker service failed to become ready: %s", reply)
            return False
        logger.info("Tracker service is ready.")
        return True

    def _reader(self, proc: subprocess.Popen) -> None:
        """Background thread: parse @GF@ reply lines from the subprocess."""
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith(MARKER):
                continue  # stray print from a third-party library — ignore
            try:
                msg = json.loads(line[len(MARKER):])
            except json.JSONDecodeError:
                logger.warning("Unparseable tracker reply: %r", line[:120])
                continue
            # Async progress events are routed to the status callback and
            # must NOT be queued as command replies.
            if msg.get("event") == "status":
                logger.info("Tracker status: %s", msg.get("stage"))
                if self._status_cb is not None:
                    try:
                        self._status_cb(msg)
                    except Exception:  # noqa: BLE001
                        logger.exception("Status callback failed")
                continue
            self._replies.put(msg)
        logger.info("Tracker service stdout closed (process exited).")

    def _wait_reply(self, timeout: float) -> Optional[dict]:
        try:
            return self._replies.get(timeout=timeout)
        except queue.Empty:
            return None

    def _send(self, msg: dict, timeout: float) -> Optional[dict]:
        """Send one command and wait for its reply (serialised by lock)."""
        with self._lock:
            if not self._ensure_process():
                return None
            assert self._proc is not None and self._proc.stdin is not None
            try:
                self._proc.stdin.write(json.dumps(msg) + "\n")
                self._proc.stdin.flush()
            except OSError:
                logger.exception("Tracker service pipe broken")
                return None
            return self._wait_reply(timeout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True if the subprocess is (or can be made) alive."""
        with self._lock:
            return self._ensure_process()

    def self_check(self) -> dict:
        """Run the tracker's dependency/camera diagnosis.

        Returns ``{"ok": bool, "report": {package: status, …}}``.
        Results are also written to the tracker log.
        """
        reply = self._send({"cmd": "check"}, 60)
        if reply is None:
            return {"ok": False,
                    "report": {"service": "FAIL: unavailable or timed out"}}
        return {"ok": bool(reply.get("ok")), "report": reply.get("report", {})}

    def warmup(self, cali_mode: "int | None" = None) -> bool:
        """Load the gaze model & open the camera ahead of time (slow)."""
        msg: dict = {"cmd": "warmup"}
        if cali_mode:
            msg["cali_mode"] = int(cali_mode)
        reply = self._send(msg, 300)
        ok = bool(reply and reply.get("ok"))
        if not ok:
            logger.warning("Tracker warmup failed: %s", reply)
        return ok

    def screen_info(self) -> "dict | None":
        """Screen geometry GazeFollower maps gaze into (see
        tracker_service.cmd_screen_info — this is what to compare against
        the browser when calibration looks fine but validation does not).
        """
        reply = self._send({"cmd": "screen_info"}, TIMEOUT_COMMAND)
        if reply is None or not reply.get("ok"):
            logger.warning("Screen info unavailable: %s", reply)
            return None
        return reply

    def cycle_sampling(self, cycles: int = 1) -> "dict | None":
        """DIAGNOSTIC: repeat a session's stop/start sampling churn."""
        reply = self._send({"cmd": "cycle_sampling", "cycles": int(cycles)},
                           TIMEOUT_COMMAND)
        if reply is None or not reply.get("ok"):
            logger.warning("cycle_sampling failed: %s", reply)
            return None
        return reply

    def rate_check_start(self) -> "dict | None":
        """Begin the PASSIVE sampling-rate measurement. Returns at once.

        Two-phase on purpose. A single blocking command would hold this
        class's command lock for the whole measurement window, starving
        the browser's live gaze preview (and any accuracy check run
        during it) — which is exactly the bug this replaced.
        """
        reply = self._send({"cmd": "rate_check_start"}, TIMEOUT_COMMAND)
        if reply is None:
            logger.warning("Rate check could not start (tracker timed out)")
            return None
        if not reply.get("ok"):
            logger.warning("Rate check start failed: %s", reply.get("error"))
        return reply

    def rate_check_result(self, tail_seconds: float = 8.0) -> "dict | None":
        """Collect the passive measurement started by rate_check_start."""
        reply = self._send(
            {"cmd": "rate_check_result", "tail_seconds": float(tail_seconds)},
            TIMEOUT_COMMAND,
        )
        if reply is None:
            logger.warning("Rate check result unavailable (tracker timed out)")
            return None
        if not reply.get("ok"):
            logger.warning("Rate check failed: %s", reply.get("error"))
            return {"ok": False, "error": reply.get("error", "unknown")}
        return reply

    def calibrate(self, options: "dict | None" = None) -> dict:
        """Run native preview + calibration. Blocks until finished.

        *options* (all optional): ``cali_mode`` (5/9/13), ``skip``
        (reuse the last saved calibration model), ``skip_preview``.
        Returns a dict ``{"success": bool, "error": str | None}``.
        """
        msg: dict = {"cmd": "calibrate"}
        msg.update(options or {})
        reply = self._send(msg, TIMEOUT_CALIBRATE)
        if reply is None:
            return {"success": False,
                    "error": "Tracker service unavailable or timed out."}
        if reply.get("ok"):
            self.calibrated = True
            return {"success": True, "error": None}
        return {"success": False, "error": reply.get("error", "unknown error")}

    def begin_stimulus(self, trigger: int) -> bool:
        """Mark a stimulus onset (starts continuous sampling on first call)."""
        reply = self._send(
            {"cmd": "begin_stimulus", "trigger": trigger}, TIMEOUT_COMMAND
        )
        ok = bool(reply and reply.get("ok"))
        if not ok:
            logger.warning("GazeFollower begin_stimulus failed: %s", reply)
        return ok

    def end_stimulus(self, trigger: int) -> bool:
        """Mark a stimulus offset (sampling keeps running in between)."""
        reply = self._send(
            {"cmd": "end_stimulus", "trigger": trigger}, TIMEOUT_COMMAND
        )
        ok = bool(reply and reply.get("ok"))
        if not ok:
            logger.warning("GazeFollower end_stimulus failed: %s", reply)
        return ok

    def gaze_info(self) -> Optional[dict]:
        """Latest live gaze estimate for the verification preview."""
        reply = self._send({"cmd": "gaze_info"}, 10)
        if reply and reply.get("ok"):
            return reply
        return None

    def telemetry(self) -> Optional[dict]:
        """Cheap tracker snapshot for the 1 Hz telemetry sampler.

        Short timeout ON PURPOSE. This is observational: if the tracker
        is busy, the right outcome is a missing sample, not a queued
        command that lands late and perturbs a recording. Telemetry that
        can degrade the session it monitors is worse than no telemetry.
        """
        try:
            reply = self._send({"cmd": "telemetry"}, 2)
        except Exception:  # noqa: BLE001 — never disturb a session
            return None
        if reply and reply.get("ok"):
            return {k: v for k, v in reply.items()
                    if k not in ("ok", "cmd", "error")}
        return None

    def position_info(self) -> Optional[dict]:
        """Live head-position metrics for the pre-calibration guide."""
        reply = self._send({"cmd": "position_info"}, 10)
        if reply and reply.get("ok"):
            return reply
        return None

    def end_session(self, csv_path: str) -> Optional[str]:
        """Stop sampling and save the whole session CSV (once per session).

        Returns the CSV path on success, or ``None`` on failure.
        """
        reply = self._send(
            {"cmd": "end_session", "csv": csv_path}, TIMEOUT_COMMAND
        )
        if reply and reply.get("ok"):
            return csv_path
        logger.warning("GazeFollower end_session failed: %s", reply)
        return None

    def shutdown(self) -> None:
        """Release the tracker and terminate the subprocess.

        Runs from an ``atexit`` handler, including on Ctrl+C. A second
        Ctrl+C (or one arriving while this is waiting) raises
        KeyboardInterrupt *inside* the handler, which Python reports as
        "Exception ignored in atexit callback" plus a traceback — noise
        that looks like a crash at the end of every manual run and
        obscures whatever the session actually logged. Catching
        BaseException here is deliberate: shutdown must still kill the
        child, and there is nothing above us to handle it anyway.
        """
        try:
            with self._lock:
                if self._proc is None or self._proc.poll() is not None:
                    return
                try:
                    assert self._proc.stdin is not None
                    self._proc.stdin.write(
                        json.dumps({"cmd": "shutdown"}) + "\n")
                    self._proc.stdin.flush()
                    self._proc.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    self._proc.kill()
                self._proc = None
        except BaseException:  # noqa: BLE001 — includes KeyboardInterrupt
            proc, self._proc = self._proc, None
            if proc is not None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
