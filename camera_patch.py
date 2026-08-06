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
import sys

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

            if log:
                log("Camera fix active: requested %dx%d @%d fps -> got "
                    "%.0fx%.0f @%.0f fps (native capture; GazeFollower's "
                    "own settings are applied before open() and silently "
                    "do nothing)"
                    % (self.img_width, self.img_height, self.cam_fps,
                       self._cap.get(cv2.CAP_PROP_FRAME_WIDTH),
                       self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
                       self._cap.get(cv2.CAP_PROP_FPS)))
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
    return "patched: native capture at %dx%d @%d fps, buffer size 1" % (
        cfg["width"], cfg["height"], cfg["fps"])


if __name__ == "__main__":
    print(describe())
