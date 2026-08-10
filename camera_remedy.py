# -*- coding: utf-8 -*-
"""
The camera is delivering half its rated frame rate. Which fix works?

WHY THIS EXISTS
---------------
The rate gate can now prove the CAMERA is the bottleneck rather than the
CPU: it compares what the model stages cost against the interval between
frames, and a pipeline running at 27 % duty cannot be the thing making
frames arrive slowly. That is a diagnosis, not a fix.

There are four candidate remedies, and they are not interchangeable:

    lighting        the webcam lengthens exposure in dim light, and
                    since it cannot expose for longer than one frame
                    period it halves the frame rate instead. More light
                    on the face removes the reason. FREE, and the image
                    stays clean — but it depends on the room, so it has
                    to be re-established at every collection site.

    capped exposure pin the exposure inside one frame period and let
                    gain make up the difference. Works in any room, at
                    the price of a darker, noisier frame — which can
                    cost detection accuracy, the thing the whole study
                    depends on.

    MJPG            some webcams offer 30 fps only in MJPG and fall
                    back to a slower uncompressed mode. Costs a JPEG
                    decode per frame (trivial at 640x480) and nothing
                    else.

    lower resolution  fewer pixels to expose and move. Changes what the
                    gaze model sees, so it is the last resort.

Guessing between them wastes participant slots. This measures all of
them on THIS camera in THIS room in about a minute, reports the
brightness alongside the rate so a "fix" that blacks out the image is
visible as such, and prints the exact configuration to use.

USAGE::

    python camera_remedy.py                 # ~60 s, all conditions
    python camera_remedy.py --seconds 10    # longer, steadier estimates
    python camera_remedy.py --camera 1

Stop the experiment server first — only one process can own the webcam.

Run it TWICE: once in the room as it is, and once with a lamp on your
face. The difference between those two runs is the answer to "is this a
lighting problem?", and it is also a recording-condition finding worth
reporting, since the sampling rate sets the resolution of every fixation
statistic in the study.
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

TARGET_FPS = 30.0
# Below this the fixation detector loses its three-sample window.
ACCEPTABLE_FPS = 27.0
# A frame outside this band is not usable for landmark detection, so a
# remedy that produces one is a regression however fast it is.
MIN_BRIGHTNESS = 35.0
MAX_BRIGHTNESS = 235.0
# CAP_PROP_EXPOSURE is log2 seconds on DirectShow/V4L2: 2^-5 = 31.25 ms,
# which fits inside a 33.3 ms frame at 30 fps.
EXPOSURE_LOG2 = -5


def _measure(cap, cv2, seconds: float) -> dict:
    """Delivered fps and mean brightness, after letting exposure settle."""
    # Auto-exposure takes a second or two to converge. Measuring through
    # the transition averages the old state into the new one and makes
    # every condition look the same.
    t_warm = time.perf_counter() + 2.0
    while time.perf_counter() < t_warm:
        cap.read()

    stamps: list = []
    bri: list = []
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        ok, frame = cap.read()
        if not ok:
            break
        stamps.append(time.perf_counter())
        if len(stamps) % 5 == 0 and frame is not None:
            bri.append(float(frame[::8, ::8].mean()))
    if len(stamps) < 6:
        return {"ok": False}
    gaps = [b - a for a, b in zip(stamps[:-1], stamps[1:]) if b > a]
    if not gaps:
        return {"ok": False}
    return {
        "ok": True,
        "fps": 1.0 / statistics.median(gaps),
        "brightness": statistics.median(bri) if bri else None,
        "frames": len(stamps),
    }


def _open(cv2, index: int):
    """DirectShow on Windows — MSMF ignores most property writes."""
    if sys.platform.startswith("win"):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if cap and cap.isOpened():
            return cap
    return cv2.VideoCapture(index)


def _restore_auto_exposure(cap, cv2) -> None:
    """Hand the camera back to auto-exposure.

    NOT optional, and not merely tidy. Exposure lives in the DEVICE, not
    in the OpenCV handle: on Windows/DirectShow a manual exposure set by
    one process survives ``release()`` and is still in force when the
    next process opens the camera. Without this, every condition after a
    manual-exposure one inherits it — which is exactly what happened on
    the first real run of this script, where the '320x240, auto
    exposure' row reported brightness 25/255 while the baseline reported
    141/255. Same camera, same room, same auto setting; the only
    difference was that two manual conditions had run in between.

    Worse, it leaves the participant's camera pinned dark AFTER the
    script exits, so the next session records in the dark for reasons
    nothing in the log explains.
    """
    for auto in (0.75, 3, 1):
        try:
            if cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, auto):
                return
        except Exception:  # noqa: BLE001
            continue


def _set_manual_exposure(cap, cv2) -> bool:
    """Return True if manual exposure actually took.

    AUTO_EXPOSURE encoding is not standardised: DirectShow builds use
    0/1, V4L2 uses 1 (manual) / 3 (auto), and several OpenCV builds use
    0.25/0.75. Trying them in order and checking the write succeeded is
    the only portable approach.
    """
    for manual in (0.25, 0, 1):
        try:
            if not cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, manual):
                continue
            if cap.set(cv2.CAP_PROP_EXPOSURE, EXPOSURE_LOG2):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _condition(cv2, index: int, seconds: float, *, width: int, height: int,
               fourcc: "str | None", manual_exposure: bool) -> dict:
    """Open the camera fresh, configure it, measure it, close it.

    Reopening per condition matters: exposure and format settings are
    sticky, so measuring them in sequence on one handle would let an
    earlier condition contaminate a later one.
    """
    cap = _open(cv2, index)
    if not cap or not cap.isOpened():
        return {"ok": False, "error": "could not open camera"}
    try:
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, 30)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:  # noqa: BLE001
            pass
        applied = True
        # ALWAYS start from auto, whatever the previous condition left
        # in the device, then opt into manual if this condition wants
        # it. Reopening the handle is not enough — see
        # _restore_auto_exposure.
        _restore_auto_exposure(cap, cv2)
        if manual_exposure:
            applied = _set_manual_exposure(cap, cv2)
        res = _measure(cap, cv2, seconds)
        res["exposure_applied"] = applied
        res["actual_size"] = "%.0fx%.0f" % (
            cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return res
    finally:
        # Never leave the camera pinned dark for the next process.
        if manual_exposure:
            try:
                _restore_auto_exposure(cap, cv2)
            except Exception:  # noqa: BLE001
                pass
        cap.release()


CONDITIONS = [
    # (label, kwargs, env vars that reproduce it)
    ("baseline 640x480, auto exposure",
     dict(width=640, height=480, fourcc=None, manual_exposure=False),
     ["GF_CAMERA_FIX=1"]),
    ("640x480 MJPG, auto exposure",
     dict(width=640, height=480, fourcc="MJPG", manual_exposure=False),
     ["GF_CAMERA_FIX=1", "GF_CAM_FOURCC=MJPG"]),
    ("640x480, exposure CAPPED at 31 ms",
     dict(width=640, height=480, fourcc=None, manual_exposure=True),
     ["GF_CAMERA_FIX=1", "GF_CAM_EXPOSURE=capped"]),
    ("640x480 MJPG + exposure CAPPED",
     dict(width=640, height=480, fourcc="MJPG", manual_exposure=True),
     ["GF_CAMERA_FIX=1", "GF_CAM_FOURCC=MJPG", "GF_CAM_EXPOSURE=capped"]),
    ("320x240, auto exposure (last resort)",
     dict(width=320, height=240, fourcc=None, manual_exposure=False),
     ["GF_CAMERA_FIX=1", "GF_CAM_WIDTH=320", "GF_CAM_HEIGHT=240"]),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=6.0,
                    help="measurement window per condition (after a 2 s "
                         "warm-up for auto-exposure to settle)")
    args = ap.parse_args()

    try:
        from env_check import require

        require("cv2")
    except ImportError:
        pass

    import cv2

    print("=" * 72)
    print("  WHICH CAMERA SETTING RESTORES THE FRAME RATE?")
    print("=" * 72)
    print("  Sit as you would during a session, with the room lit as it")
    print("  will be when recording. %d conditions x ~%.0f s."
          % (len(CONDITIONS), args.seconds + 2))
    print()

    results = []
    for label, kwargs, env in CONDITIONS:
        print("  %-38s " % label, end="", flush=True)
        try:
            res = _condition(cv2, args.camera, args.seconds, **kwargs)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": str(exc)[:60]}
        res.update(label=label, env=env)
        results.append(res)
        if not res.get("ok"):
            print("failed (%s)" % res.get("error", "no frames"))
            continue
        note = ""
        if kwargs["manual_exposure"] and not res.get("exposure_applied"):
            note = "  [camera refused manual exposure]"
        print("%5.1f fps   brightness %s/255%s"
              % (res["fps"],
                 "%5.1f" % res["brightness"] if res["brightness"] is not None
                 else "    ?", note))

    good = [r for r in results if r.get("ok")]
    if not good:
        print("\n  No condition produced frames. Is the experiment server "
              "still running? It owns the webcam.")
        return 1

    # ── Verdict ──────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("  VERDICT")
    print("=" * 72)

    baseline = good[0] if good[0]["label"].startswith("baseline") else None

    def usable(r) -> bool:
        b = r.get("brightness")
        return (r["fps"] >= ACCEPTABLE_FPS and b is not None
                and MIN_BRIGHTNESS <= b <= MAX_BRIGHTNESS)

    # Honour the declared order: earlier conditions change less about
    # what the gaze model sees, so the first workable one is the one to
    # adopt. Picking the fastest instead would happily recommend 320x240.
    winner = next((r for r in results if r.get("ok") and usable(r)), None)

    if baseline and baseline["fps"] >= ACCEPTABLE_FPS:
        print("  THE CAMERA IS NOT THE PROBLEM.")
        print()
        print("  It delivers %.1f fps at brightness %.0f/255 with no"
              % (baseline["fps"], baseline["brightness"] or 0))
        print("  changes at all, so no camera setting is going to help and")
        print("  none of the rows above is worth adopting.")
        print()
        print("  If the rate gate still reports roughly HALF this, the")
        print("  loss is inside the pipeline. The mechanism to suspect is")
        print("  the synchronous capture callback: GazeFollower runs the")
        print("  whole per-frame chain — FaceMesh, the gaze CNN, its")
        print("  filter, its subscriber dispatch and a CSV write+flush —")
        print("  INSIDE the capture loop, so the loop cannot start the")
        print("  next frame until that returns. The instant total work")
        print("  crosses the frame period (33.3 ms at 30 fps) the loop")
        print("  misses every second frame, and the rate does not sag,")
        print("  it HALVES. An exact 30 -> 15 is that signature.")
        print()
        print("  Note that the MODEL stages alone (~18 ms) are not the")
        print("  whole callback — the rest of it was untimed until now.")
        print("  Re-run a session on the current build: the rate gate")
        print("  reports total callback cost and the subscriber count,")
        print("  which is where duplicated CSV writers show up.")
        return 0

    if winner is None:
        fastest = max(good, key=lambda r: r["fps"])
        print("  *** NO SETTING REACHED %.0f fps. ***" % ACCEPTABLE_FPS)
        print()
        print("  Best was %.1f fps (%s)." % (fastest["fps"], fastest["label"]))
        dark = [r for r in good
                if r.get("brightness") is not None
                and r["brightness"] < MIN_BRIGHTNESS]
        if dark:
            print("  %d condition(s) went too dark to use, which is the "
                  "signature of a room that is simply underlit: capping "
                  "the exposure only moves the problem from the frame "
                  "rate to the image." % len(dark))
        print()
        print("  This is now a LIGHTING problem, and no software setting")
        print("  will solve it. Put a lamp on the participant's FACE — not")
        print("  behind them, not aimed at the screen — raise the screen")
        print("  brightness, and run this again. The two runs together are")
        print("  a recording-condition result worth reporting.")
        return 1

    gained = (" (up from %.1f)" % baseline["fps"]) if baseline else ""
    print("  Use: %s" % winner["label"])
    print("  %.1f fps%s, brightness %.0f/255 — usable."
          % (winner["fps"], gained, winner["brightness"]))
    print()
    print("  Windows (Command Prompt):")
    for e in winner["env"]:
        print("      set %s" % e)
    print()
    print("  macOS / Linux:")
    print("      export %s" % " ".join(winner["env"]))
    print()
    if any("EXPOSURE" in e for e in winner["env"]):
        print("  NOTE: this pins the exposure, which trades image")
        print("  brightness for frame rate. Confirm it did not cost you")
        print("  detection: run one session and check detected_pct is")
        print("  still ~100 %%. If it falls, prefer more light instead.")
    if any("WIDTH" in e for e in winner["env"]):
        print("  NOTE: this changes the resolution the GAZE MODEL sees, so")
        print("  it may shift accuracy. Re-run the validation and compare")
        print("  before adopting it for participants.")
    print()
    print("  Then add the line(s) above to windows\\run_session.bat so the")
    print("  configuration is frozen rather than remembered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
