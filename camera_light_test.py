# -*- coding: utf-8 -*-
"""
Does the SCREEN's brightness change the camera's frame rate?

WHY THIS IS THE REMAINING SUSPECT
---------------------------------
hz_experiment.py showed the full tracking pipeline holds 30.0 Hz for six
minutes with a recorded clip as input — flat, repeatable, unaffected by
threads, the writer, or the flush. So the software is not the limit.

The one thing that experiment REPLACES is the live webcam. And webcams
have an automatic behaviour that fits every observation: in dim light
they lengthen exposure, and since exposure time cannot exceed the frame
interval, the driver DROPS THE FRAME RATE to compensate. 33 ms per frame
at 30 fps becomes 66 ms at 15 fps, or 100 ms at 10 fps.

During a session the screen is the main light source on the face, and it
goes from a bright browser page to a dark fullscreen video. Exposure
adapts over a few seconds — which is why it feels like it gets "worse and
worse" — while detection stays at 100 %, because the image is still
perfectly usable, there are simply fewer frames of it.

WHAT THIS DOES
--------------
Shows a fullscreen window that alternates WHITE and BLACK while measuring
the camera's delivered FPS and image brightness in each phase. If FPS
drops when the screen goes dark, that is the mechanism, and it is fixable
with lighting rather than code.

USAGE::

    python camera_light_test.py                 # 4 phases, 15 s each
    python camera_light_test.py --seconds 20
    python camera_light_test.py --no-window     # room lighting only

Sit in front of the screen as you would during a session, in the room you
will actually record in, with the same lights. Press ESC to abort.

Stop the experiment server first — only one process can own the webcam.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def measure_phase(cap, cv2, seconds: float, label: str) -> dict:
    """Grab frames for *seconds*, returning fps and brightness."""
    stamps: list = []
    brightness: list = []
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        ok, frame = cap.read()
        if not ok:
            break
        stamps.append(time.perf_counter())
        # Sample sparsely: converting every frame would itself cost time.
        if len(stamps) % 5 == 0:
            brightness.append(float(frame[::8, ::8].mean()))
    if len(stamps) < 5:
        return {"label": label, "ok": False}
    gaps = [b - a for a, b in zip(stamps[:-1], stamps[1:]) if b > a]
    return {
        "label": label,
        "ok": True,
        "fps": 1.0 / statistics.median(gaps),
        "frames": len(stamps),
        "brightness": statistics.median(brightness) if brightness else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=15.0,
                    help="duration of each phase")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--no-window", action="store_true",
                    help="skip the screen phases; measure room light only")
    args = ap.parse_args()

    try:
        from env_check import require

        require("cv2")
    except ImportError:
        pass

    import cv2

    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW) \
        if sys.platform.startswith("win") else cv2.VideoCapture(args.camera)
    if not cap or not cap.isOpened():
        print("Could not open camera %d — is the experiment server running?"
              % args.camera)
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)

    print("=" * 70)
    print("  DOES SCREEN BRIGHTNESS CHANGE THE CAMERA'S FRAME RATE?")
    print("=" * 70)
    print("  Sit where you would during a session, with the room lit as it")
    print("  will be when recording. Each phase lasts %.0f s." % args.seconds)
    print()

    results: list = []
    win = None
    if not args.no_window:
        try:
            import pygame

            pygame.init()
            info = pygame.display.Info()
            win = pygame.display.set_mode((info.current_w, info.current_h),
                                          pygame.FULLSCREEN)
            pygame.display.set_caption("Camera light test")
        except Exception as exc:  # noqa: BLE001
            print("  (pygame unavailable: %s — falling back to --no-window)"
                  % exc)
            win = None

    phases = [("room light (no window)", None)] if win is None else [
        ("WHITE screen", (255, 255, 255)),
        ("BLACK screen", (0, 0, 0)),
        ("WHITE screen (again)", (255, 255, 255)),
        ("BLACK screen (again)", (0, 0, 0)),
    ]

    try:
        for label, colour in phases:
            if win is not None and colour is not None:
                import pygame

                win.fill(colour)
                pygame.display.flip()
                pygame.event.pump()
                # Let auto-exposure settle before measuring, otherwise the
                # first seconds of each phase average the transition away.
                time.sleep(3.0)
            print("  measuring: %-22s " % label, end="", flush=True)
            res = measure_phase(cap, cv2, args.seconds, label)
            results.append(res)
            if res["ok"]:
                print("%5.1f fps   brightness %5.1f/255"
                      % (res["fps"], res["brightness"]))
            else:
                print("failed")
            if win is not None:
                import pygame

                for ev in pygame.event.get():
                    if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                        raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\n  (aborted)")
    finally:
        cap.release()
        if win is not None:
            try:
                import pygame

                pygame.display.quit()
                pygame.quit()
            except Exception:  # noqa: BLE001
                pass

    good = [r for r in results if r.get("ok")]
    if len(good) < 2:
        print("\nNot enough phases measured to conclude anything.")
        return 1

    print()
    print("=" * 70)
    print("  VERDICT")
    print("=" * 70)
    fastest = max(good, key=lambda r: r["fps"])
    slowest = min(good, key=lambda r: r["fps"])
    drop = fastest["fps"] - slowest["fps"]

    if drop > 0.20 * fastest["fps"]:
        print("  *** THE SCREEN'S BRIGHTNESS CONTROLS THE FRAME RATE. ***")
        print()
        print("  %5.1f fps at brightness %.0f  (%s)"
              % (fastest["fps"], fastest["brightness"], fastest["label"]))
        print("  %5.1f fps at brightness %.0f  (%s)"
              % (slowest["fps"], slowest["brightness"], slowest["label"]))
        print()
        print("  The camera lengthens its exposure in dim light, and cannot")
        print("  expose for longer than one frame interval — so it halves")
        print("  the frame rate instead. During a session the screen is the")
        print("  main light on your face, and a dark video makes it dim.")
        print()
        print("  This is a LIGHTING problem, not a computer problem:")
        print("    - put a lamp on your FACE (not behind you, not on the")
        print("      screen); a cheap desk lamp bounced off a wall is ideal")
        print("    - or disable the webcam's auto-exposure and fix the")
        print("      exposure manually (Windows Camera settings, or")
        print("      CAP_PROP_AUTO_EXPOSURE / CAP_PROP_EXPOSURE in OpenCV)")
        print("    - re-run this test after changing the lighting; the two")
        print("      phases should then read within a few fps of each other")
    else:
        print("  Frame rate is stable across screen brightness")
        print("  (%.1f - %.1f fps, brightness %.0f - %.0f)."
              % (slowest["fps"], fastest["fps"],
                 slowest["brightness"], fastest["brightness"]))
        print()
        print("  Auto-exposure throttling is NOT your problem. Since the")
        print("  pipeline also holds 30 Hz on recorded input, the remaining")
        print("  suspect is the app layer — the browser, Flask/SocketIO, or")
        print("  the video decode competing with the capture loop.")
        print("  Next: run a session and compare the rate gate's reading")
        print("  with the browser closed vs open.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
