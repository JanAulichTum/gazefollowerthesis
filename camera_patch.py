# -*- coding: utf-8 -*-
"""
Work around two performance bugs in GazeFollower's WebCamCamera.

BUG 1 — the resolution/FPS settings never take effect.
``WebCamCamera.__init__`` does::

    self._cap = cv2.VideoCapture()                       # NOT opened
    self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    self._cap.set(cv2.CAP_PROP_FPS, 30)

...and only later, in ``open()``, calls ``self._cap.open(webcam_id)``.
``set()`` on an unopened VideoCapture is a no-op, and opening a device
resets its properties anyway. So the camera runs at whatever its native
default is — commonly 1280x720 or 1920x1080 on a modern laptop webcam —
and every frame then pays for a full-size BGR->RGB conversion plus a
software ``cv2.resize`` down to 640x480 before any inference starts.

That is pure waste, and it lands in the worst possible place: the
callback runs SYNCHRONOUSLY inside the capture loop (see ``capture()``),
so per-frame cost directly gates the capture rate. Once total per-frame
work exceeds the camera's frame period (33.3 ms at 30 fps), the loop
misses every other frame and the rate halves — the 29 -> 14.5 Hz cliff.

BUG 2 — no buffer-size limit.
With a slow consumer, frames queue in the driver buffer and the loop
processes ever-staler frames. ``CAP_PROP_BUFFERSIZE = 1`` keeps it
current (best-effort; not every backend honours it).

THE FIX
Subclass ``WebCamCamera`` and apply the properties AFTER opening. On
Windows, also prefer the DirectShow backend, which exposes resolution
and FPS far more reliably than the default MSMF one.

This is OPT-IN, because it changes what the gaze model sees (a natively
captured 640x480 frame instead of a downscaled 720p one) and could in
principle shift accuracy. Measure it, do not assume it::

    python tracker_fps_test.py --seconds 60             # before

    set GF_CAMERA_FIX=1                                 # Command Prompt
    $env:GF_CAMERA_FIX='1'                              # PowerShell
    export GF_CAMERA_FIX=1                              # bash
    python tracker_fps_test.py --seconds 60             # after

If the rate improves, keep it and record the choice in the methods
section. If it does not, leave it off.

Enable with ``GF_CAMERA_FIX=1``; tune with ``GF_CAM_WIDTH`` /
``GF_CAM_HEIGHT`` / ``GF_CAM_FPS`` (defaults 640 / 480 / 30).
"""

from __future__ import annotations

import os
import statistics
import sys
import time

# ── Exposure ──────────────────────────────────────────────────────────
# A UVC webcam cannot integrate light for longer than one frame period.
# When auto-exposure decides the scene is dim it lengthens the exposure,
# and once the required exposure exceeds the frame period the driver
# HALVES the delivered frame rate to make room: 30 -> 15 -> 10 fps. This
# is invisible in every software metric except the rate itself, because
# detection stays high — the image is still perfectly usable, there is
# simply less of it. It also drifts DURING a session, since the screen
# is the main light on the participant's face and a dark video is a dark
# lamp.
#
# Capping the exposure time at one frame period removes the mechanism.
# The cost is a darker image, which the camera's gain partly recovers at
# the price of noise. Whether that trade is acceptable is an empirical
# question about detection rate, so the cap is OPT-IN and self-reverting:
# if the resulting frame is too dark to be useful, auto-exposure is
# restored and the reason logged, because a bright 15 Hz recording is
# better than a black 30 Hz one.
#
# CAP_PROP_EXPOSURE is in log2 seconds on the DirectShow/V4L2 backends:
# -5 -> 2^-5 s = 31.25 ms, which fits inside a 33.3 ms frame.
EXPOSURE_LOG2_FOR_30FPS = -5
MIN_USABLE_BRIGHTNESS = 35.0    # mean of 0-255; below this, revert
MAX_USABLE_BRIGHTNESS = 235.0   # blown out; below this, revert


def exposure_mode() -> str:
    """``auto`` (default) or ``capped``."""
    raw = os.environ.get("GF_CAM_EXPOSURE", "").strip().lower()
    if raw in ("capped", "cap", "fixed", "manual", "1", "true", "yes"):
        return "capped"
    return "auto"

# ── Windows console encoding ──────────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def camera_fix_enabled() -> bool:
    return os.environ.get("GF_CAMERA_FIX", "").strip().lower() in (
        "1", "true", "yes")


def desired_camera() -> dict:
    return {
        "width": int(os.environ.get("GF_CAM_WIDTH", "640")),
        "height": int(os.environ.get("GF_CAM_HEIGHT", "480")),
        "fps": int(os.environ.get("GF_CAM_FPS", "30")),
    }


def make_camera(log=None):
    """Return a patched WebCamCamera, or None to use GazeFollower's own.

    Never raises: a failure here must fall back to stock behaviour rather
    than stop a session.
    """
    if not camera_fix_enabled():
        return None
    try:
        import cv2
        from gazefollower.camera import WebCamCamera
    except Exception as exc:  # noqa: BLE001
        if log:
            log("Camera fix skipped (import failed: %s)" % exc)
        return None

    cfg = desired_camera()

    def _mean_brightness(cap, n: int = 5) -> "float | None":
        vals = []
        for _ in range(n):
            ok, frame = cap.read()
            if ok and frame is not None:
                vals.append(float(frame[::8, ::8].mean()))
        return statistics.median(vals) if vals else None

    def _measure_fps(cap, seconds: float = 1.5) -> "float | None":
        """Frames the camera ACTUALLY delivers.

        CAP_PROP_FPS reports what was requested, not what arrives — a
        camera throttled by auto-exposure still claims 30. Only timing
        real reads tells the truth, so the self-check and the manifest
        use this rather than the property.
        """
        stamps = []
        t_end = time.perf_counter() + seconds
        while time.perf_counter() < t_end:
            ok, _ = cap.read()
            if not ok:
                break
            stamps.append(time.perf_counter())
        if len(stamps) < 5:
            return None
        gaps = [b - a for a, b in zip(stamps[:-1], stamps[1:]) if b > a]
        return 1.0 / statistics.median(gaps) if gaps else None

    def _cap_exposure(cap) -> str:
        """Stop auto-exposure from halving the frame rate. Self-reverting."""
        before = _mean_brightness(cap)
        # AUTO_EXPOSURE semantics differ by backend and even by build:
        # DirectShow commonly uses 0/1, V4L2 uses 1 (manual) / 3 (auto),
        # and some OpenCV builds use 0.25/0.75. Try them in order and
        # keep whichever actually changes the delivered exposure.
        applied = False
        for manual_value in (0.25, 0, 1):
            try:
                if not cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, manual_value):
                    continue
                if cap.set(cv2.CAP_PROP_EXPOSURE, EXPOSURE_LOG2_FOR_30FPS):
                    applied = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not applied:
            return "auto (camera refused manual exposure)"
        # Give gain a chance to recover the light we just took away.
        for prop in ("CAP_PROP_GAIN", "CAP_PROP_BRIGHTNESS"):
            try:
                cap.set(getattr(cv2, prop), cap.get(getattr(cv2, prop)))
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)
        after = _mean_brightness(cap)
        if after is None:
            return "auto (could not verify brightness)"
        if after < MIN_USABLE_BRIGHTNESS or after > MAX_USABLE_BRIGHTNESS:
            # A frame the model cannot use is worse than a slow one.
            for auto_value in (0.75, 3, 1):
                try:
                    if cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, auto_value):
                        break
                except Exception:  # noqa: BLE001
                    pass
            return ("auto (REVERTED: capping exposure left the image at "
                    "%.0f/255, outside the usable %.0f-%.0f band%s — add "
                    "light to the participant's face instead)"
                    % (after, MIN_USABLE_BRIGHTNESS, MAX_USABLE_BRIGHTNESS,
                       "" if before is None else
                       ", was %.0f on auto" % before))
        return ("capped at 2^%d s = %.1f ms (brightness %.0f/255%s)"
                % (EXPOSURE_LOG2_FOR_30FPS,
                   1000.0 * 2 ** EXPOSURE_LOG2_FOR_30FPS, after,
                   "" if before is None else ", was %.0f on auto" % before))

    class FixedWebCamCamera(WebCamCamera):  # type: ignore[misc, valid-type]
        """WebCamCamera that actually applies its capture properties."""

        def open(self):
            # Prefer DirectShow on Windows: the default MSMF backend
            # frequently ignores resolution/FPS requests.
            backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else 0
            opened = self._cap.open(self.webcam_id, backend) if backend \
                else self._cap.open(self.webcam_id)
            if not opened and backend:
                opened = self._cap.open(self.webcam_id)   # fall back
            if not opened:
                raise Exception("Failed to open webcam camera")

            # NOW the properties stick — the capture device exists.
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.img_width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.img_height)
            self._cap.set(cv2.CAP_PROP_FPS, self.cam_fps)
            try:
                self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:  # noqa: BLE001 — not all backends support it
                pass

            exposure = "auto (webcam decides; may halve the frame rate "
            exposure += "in dim light)"
            if exposure_mode() == "capped":
                exposure = _cap_exposure(self._cap)

            # The DELIVERED rate, measured. Doing this here — before a
            # participant is calibrated — is what turns "the session was
            # 15 Hz" into "the camera was 15 Hz from the moment it
            # opened", which is a different and much shorter search.
            delivered = _measure_fps(self._cap)
            self.measured_fps = delivered
            self.exposure_mode_desc = exposure

            if log:
                log("Camera fix active: requested %dx%d @%d fps -> got "
                    "%.0fx%.0f, camera CLAIMS %.0f fps, actually DELIVERS "
                    "%s fps | exposure: %s"
                    % (self.img_width, self.img_height, self.cam_fps,
                       self._cap.get(cv2.CAP_PROP_FRAME_WIDTH),
                       self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
                       self._cap.get(cv2.CAP_PROP_FPS),
                       "%.1f" % delivered if delivered else "?",
                       exposure))
                if delivered and delivered < 0.85 * self.cam_fps:
                    log("*** THE CAMERA IS THE BOTTLENECK: it delivers "
                        "%.1f fps with no tracking running at all, against "
                        "a requested %d. Nothing downstream can be faster "
                        "than this. The usual cause is auto-exposure "
                        "lengthening in dim light — put a lamp on the "
                        "participant's face, raise the screen brightness, "
                        "or set GF_CAM_EXPOSURE=capped. Confirm with "
                        "camera_light_test.py. ***"
                        % (delivered, self.cam_fps))
            self._create_capture_thread()

    try:
        return FixedWebCamCamera(img_width=cfg["width"],
                                 img_height=cfg["height"],
                                 cam_fps=cfg["fps"])
    except Exception as exc:  # noqa: BLE001
        if log:
            log("Camera fix skipped (construction failed: %s)" % exc)
        return None


def describe() -> str:
    """One-line summary for the self-check report."""
    if not camera_fix_enabled():
        return ("stock GazeFollower camera (its resolution/FPS settings are "
                "applied to an UNOPENED capture and therefore do nothing — "
                "frames arrive at the webcam's native size and are resized "
                "in software every frame). Set GF_CAMERA_FIX=1 to capture "
                "natively at 640x480 and A/B it with tracker_fps_test.py")
    cfg = desired_camera()
    return ("patched: native capture at %dx%d @%d fps, buffer size 1, "
            "exposure %s" % (cfg["width"], cfg["height"], cfg["fps"],
                             "CAPPED at one frame period (auto-exposure "
                             "cannot halve the frame rate)"
                             if exposure_mode() == "capped"
                             else "AUTO (the webcam may halve the frame "
                                  "rate in dim light — set "
                                  "GF_CAM_EXPOSURE=capped to prevent it)"))


if __name__ == "__main__":
    print(describe())
