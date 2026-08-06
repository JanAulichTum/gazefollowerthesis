# -*- coding: utf-8 -*-
"""
Run the whole pipeline with NO person and NO live camera.

WHY
---
Chasing the sampling-rate problem with a live webcam is unreliable: the
input changes every run (lighting, posture, blinks), so a rate difference
could always be you rather than the software. This replaces the camera
with a **fixed, replayable recording**, so every run sees byte-identical
frames. Any remaining variation in the rate is then the machine or the
code — which is exactly the question.

It also removes the need to sit in front of the screen for a 25 s rate
gate plus a 7-target accuracy check every time.

WHAT IS AND IS NOT FAKED
------------------------
Faked : the camera source, and (optionally) the calibration mapping.
REAL  : MediaPipe FaceMesh, the MNN gaze CNN, patch clipping/resizing,
        the filter, the CSV writer, the Flask/SocketIO layer.

That is the point — all the expensive per-frame work still happens, so
the measured rate is meaningful. Only the pixels are deterministic.

USAGE
-----
1. Record a reference clip ONCE (needs you and a camera, ~30 s)::

       python fake_camera.py --record --seconds 30

   Writes data/fake_face.mp4 — a real face, so FaceMesh and the gaze
   model behave exactly as in a session.

2. Run anything against it, with no person present::

       set  GF_FAKE_CAMERA=data\\fake_face.mp4       (Command Prompt)
       set  GF_FAKE_CALIBRATION=1
       python app.py

   or, on macOS/Linux::

       export GF_FAKE_CAMERA=data/fake_face.mp4
       export GF_FAKE_CALIBRATION=1
       python app.py

3. Verify the clip is usable (face detected in most frames)::

       python fake_camera.py --check data/fake_face.mp4

SAFETY
------
Fake mode is loud: the tracker logs it, and it is recorded in the session
manifest as ``fake_mode``. Data recorded this way must never be treated
as participant data. ``GF_FAKE_CAMERA`` unset = completely inert.
"""

from __future__ import annotations

import argparse
import collections
import os
import statistics
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CLIP = os.path.join(BASE, "data", "fake_face.mp4")

# The live FakeCamera, so the rate check can read its counters without
# threading a reference through GazeFollower's internals.
_LIVE_CAMERA = None


def camera_stats() -> "dict | None":
    """Counters from the running fake camera, or None when not in fake mode.

    THE QUESTION THIS ANSWERS
    -------------------------
    When a session records at half rate there are exactly two
    possibilities, and they need opposite fixes:

      (a) the capture loop is SLOW — each frame's synchronous processing
          (FaceMesh + MNN + writer) exceeds the 33.3 ms budget, so fewer
          frames are produced. Fix: make per-frame work cheaper.
      (b) the capture loop is FINE — frames are produced at 30 Hz but
          only half of them become samples, i.e. they are dropped
          downstream. Fix: find the drop, not the cost.

    Comparing ``served_hz`` (frames the camera delivered) against the
    rate check's ``sustained_hz`` (samples that came out) separates them
    in a single run. ``callback_ms`` then says how much of the budget the
    processing actually used.
    """
    cam = _LIVE_CAMERA
    if cam is None:
        return None
    try:
        return cam.stats()
    except Exception:  # noqa: BLE001
        return None


def fake_camera_path() -> "str | None":
    """Clip to replay, or None when fake mode is off."""
    raw = os.environ.get("GF_FAKE_CAMERA", "").strip()
    if not raw:
        return None
    path = raw if os.path.isabs(raw) else os.path.join(BASE, raw)
    return path


def fake_calibration_enabled() -> bool:
    return os.environ.get("GF_FAKE_CALIBRATION", "").strip().lower() in (
        "1", "true", "yes")


def describe() -> str:
    """One-line summary for the self-check report."""
    path = fake_camera_path()
    if not path:
        return "off (real webcam)"
    ok = "found" if os.path.isfile(path) else "MISSING"
    return ("REPLAYING %s (%s)%s — NOT REAL DATA"
            % (path, ok,
               ", calibration stubbed" if fake_calibration_enabled() else ""))


def make_fake_camera(log=None):
    """Build the replay camera, or None when fake mode is off/unusable."""
    path = fake_camera_path()
    if not path:
        return None
    if not os.path.isfile(path):
        if log:
            log("GF_FAKE_CAMERA set but the clip does not exist: %s "
                "(record one with: python fake_camera.py --record)" % path)
        return None
    try:
        import cv2
        from gazefollower.camera import Camera
    except Exception as exc:  # noqa: BLE001
        if log:
            log("Fake camera unavailable (%s)" % exc)
        return None

    target_fps = float(os.environ.get("GF_FAKE_FPS", "30"))

    class FakeCamera(Camera):  # type: ignore[misc, valid-type]
        """Replays a video file at a fixed rate, looping forever.

        Deliberately paces itself with a deadline rather than
        ``sleep(1/fps)``: a fixed sleep would let per-frame processing
        cost silently reduce the delivered rate, which is the very thing
        being measured. With a deadline, if the consumer cannot keep up
        the frames are LATE rather than fewer, and that shows up as a
        genuine processing limit instead of a quieter camera.
        """

        def __init__(self) -> None:
            super().__init__()
            self._thread = None
            self._running = False
            self.frames_served = 0
            self.frames_late = 0
            # Per-frame cost of the SYNCHRONOUS callback — i.e. the whole
            # FaceMesh + gaze-model + writer chain, which GazeFollower
            # runs inline on the capture thread. This is the number that
            # decides the ceiling: if it exceeds 33.3 ms, 30 Hz is
            # arithmetically impossible no matter what else is fixed.
            self.callback_ms = collections.deque(maxlen=4000)
            self._t_open = None

        def stats(self) -> dict:
            ms = sorted(self.callback_ms)
            elapsed = (time.perf_counter() - self._t_open) \
                if self._t_open else 0.0

            def _pct(p: float) -> "float | None":
                if not ms:
                    return None
                return round(ms[min(len(ms) - 1, int(p * len(ms)))], 1)

            return {
                "frames_served": self.frames_served,
                "frames_late": self.frames_late,
                "late_pct": round(100.0 * self.frames_late / self.frames_served, 1)
                if self.frames_served else None,
                "served_hz": round(self.frames_served / elapsed, 1)
                if elapsed > 0 else None,
                "target_fps": target_fps,
                "callback_ms_median": round(statistics.median(ms), 1)
                if ms else None,
                "callback_ms_p90": _pct(0.90),
                "callback_ms_max": round(ms[-1], 1) if ms else None,
                "frame_budget_ms": round(1000.0 / target_fps, 1)
                if target_fps > 0 else None,
            }

        def open(self):
            global _LIVE_CAMERA
            self._cap = cv2.VideoCapture(path)
            if not self._cap.isOpened():
                raise Exception("Could not open fake clip: %s" % path)
            self._running = True
            self._t_open = time.perf_counter()
            _LIVE_CAMERA = self
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            if log:
                log("FAKE CAMERA replaying %s at %.0f fps — NOT REAL DATA"
                    % (os.path.basename(path), target_fps))

        def _loop(self):
            period = 1.0 / target_fps if target_fps > 0 else 0.0
            next_due = time.perf_counter()
            while self._running:
                ok, frame = self._cap.read()
                if not ok:                      # loop the clip
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = self._cap.read()
                    if not ok:
                        break
                # Match WebCamCamera's own preprocessing exactly, so the
                # comparison with a live run stays honest.
                if len(frame.shape) == 3 and frame.shape[2] == 3:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                timestamp = time.time_ns()
                t_cb = time.perf_counter()
                try:
                    with self.callback_and_param_lock:
                        if self.callback_func is not None:
                            self.callback_func(
                                self.camera_running_state, timestamp, frame,
                                *self.callback_args, **self.callback_kwargs)
                except Exception as exc:  # noqa: BLE001
                    if log:
                        log("Fake camera callback error: %s" % exc)
                # Includes lock-wait: if something else holds
                # callback_and_param_lock (a gaze_info poll, say), the
                # capture thread is stalled and that stall belongs in the
                # per-frame cost, because that is what limits the rate.
                self.callback_ms.append((time.perf_counter() - t_cb) * 1000.0)

                self.frames_served += 1
                next_due += period
                slack = next_due - time.perf_counter()
                if slack > 0:
                    time.sleep(slack)
                else:
                    # Consumer is slower than the target rate: do not try
                    # to catch up (that would create artificial bursts).
                    self.frames_late += 1
                    next_due = time.perf_counter()

        def close(self):
            self._running = False
            if self._thread is not None:
                self._thread.join(timeout=2.0)
                self._thread = None
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            if log:
                log("Fake camera closed (served %d frames, %d late)"
                    % (self.frames_served, self.frames_late))

        def release(self):
            self.close()

    return FakeCamera()


def apply_fake_calibration(gf, log=None) -> bool:
    """Mark the tracker calibrated with a pass-through mapping.

    Lets the whole flow run with no calibration step. The gaze
    COORDINATES are then meaningless — raw model output, unmapped — but
    every per-frame cost is unchanged, so timing questions are still
    answerable. Never enable this for real data.
    """
    if not fake_calibration_enabled():
        return False
    calib = getattr(gf, "calibration", None)
    if calib is None:
        return False
    calib.predict = lambda features, estimated: (True, estimated)
    calib.has_calibrated = True
    if log:
        log("FAKE CALIBRATION active — gaze coordinates are meaningless; "
            "only timing/throughput results are valid.")
    return True


# ──────────────────────────────────────────────────────────────────────
# CLI helpers
# ──────────────────────────────────────────────────────────────────────

def record(seconds: float, out_path: str, camera_index: int = 0) -> int:
    """Capture a reference clip from the real webcam."""
    import cv2

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW) \
        if sys.platform.startswith("win") else cv2.VideoCapture(camera_index)
    if not cap or not cap.isOpened():
        print("Could not open camera %d (is the server running?)" % camera_index)
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)

    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"),
                             30.0, (640, 480))
    print("Recording %.0f s to %s" % (seconds, out_path))
    print("Sit as you would during a session and LOOK AROUND THE SCREEN —")
    print("the clip should contain the same head movements a participant")
    print("makes, or it will be an unrealistically easy input.")
    t0 = time.time()
    n = 0
    while time.time() - t0 < seconds:
        ok, frame = cap.read()
        if not ok:
            break
        if frame.shape[1] != 640 or frame.shape[0] != 480:
            frame = cv2.resize(frame, (640, 480))
        writer.write(frame)
        n += 1
        left = seconds - (time.time() - t0)
        sys.stdout.write("\r  %4.1f s left, %d frames" % (max(0, left), n))
        sys.stdout.flush()
    writer.release()
    cap.release()
    print("\nWrote %d frames to %s" % (n, out_path))
    print("Check it with:  python fake_camera.py --check %s" % out_path)
    return 0


def check(path: str) -> int:
    """Confirm the clip is usable: does FaceMesh find a face in it?"""
    import cv2

    try:
        import mediapipe as mp
    except Exception as exc:  # noqa: BLE001
        print("mediapipe unavailable (%s)" % exc)
        return 1
    if not os.path.isfile(path):
        print("No such clip:", path)
        return 1

    cap = cv2.VideoCapture(path)
    mesh = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False, max_num_faces=1, refine_landmarks=True,
        min_detection_confidence=0.5)
    total = found = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        total += 1
        res = mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if res.multi_face_landmarks:
            found += 1
    cap.release()
    mesh.close()

    if not total:
        print("Clip contains no frames.")
        return 1
    pct = 100.0 * found / total
    print("Clip     : %s" % path)
    print("Frames   : %d" % total)
    print("Face found: %d (%.1f %%)" % (found, pct))
    if pct >= 95:
        print("-> Excellent reference clip.")
    elif pct >= 70:
        print("-> Usable, but %.0f %% of frames have no face. Fine for "
              "timing tests; remember the detection rate is baked in." % (100 - pct))
    else:
        print("-> POOR. Re-record with better lighting and the camera at "
              "eye level, or timing runs will be dominated by detection "
              "failures rather than throughput.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--record", action="store_true",
                    help="capture a reference clip from the real webcam")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", default=DEFAULT_CLIP)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--check", metavar="CLIP", help="verify a clip is usable")
    args = ap.parse_args()

    if args.check:
        return check(args.check)
    if args.record:
        return record(args.seconds, args.out, args.camera)
    print(__doc__)
    print("Current setting:", describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
