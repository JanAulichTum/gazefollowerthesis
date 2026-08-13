# -*- coding: utf-8 -*-
"""
Replace the assumed 60 cm viewing distance with a measured one.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
Every accuracy figure in this project is measured in PIXELS and then
converted to degrees of visual angle. The pixel figure is exact. The
conversion is not: it divides by the viewing distance, so the reported
degrees inherit the distance error proportionally.

With a measured error of 129.7 px on this display::

    45 cm -> 2.97 deg      60 cm -> 2.23 deg      80 cm -> 1.67 deg

The same recording either fails or comfortably passes a 3 deg threshold
depending on an assumption nobody measured. That is not acceptable for a
number the inclusion criteria depend on.

THE TWO ASSUMPTIONS, AND WHICH ONE YOU CAN KILL
-----------------------------------------------
Distance is estimated from the inter-ocular separation in pixels::

    distance_cm = IOD_cm * focal_px / IOD_px

which needs two things the pipeline currently guesses:

  focal_px   derived from an ASSUMED 60 deg camera field of view. Real
             laptop webcams range ~55-75 deg, so this alone can be 20 %
             out. **This is fully removable**: measure it once per
             machine against a tape measure (calibrate() below) and the
             assumption is gone forever.

  IOD_cm     the participant's actual inter-pupillary distance. Adult
             mean is ~6.3 cm with SD ~0.4, so assuming the mean costs
             about +-11 % (2 SD). Measuring it with a ruler takes ten
             seconds and reduces that to ~3 %.

So: calibrate the camera ONCE per machine, and optionally measure each
participant's IOD. Both are recorded, and whichever you skip is
propagated into an explicit uncertainty rather than silently ignored.

WHAT YOU REPORT AFTERWARDS
--------------------------
``degrees_with_uncertainty()`` returns the value AND a range, so the
thesis can say "2.21 deg (95 % CI 2.08-2.36, from a measured viewing
distance of 58.4 +- 3.1 cm)" instead of "2.21 deg (assumed 60 cm)".
That is a stronger claim, and it is honest about what was measured.

Usage::

    # ONE command: sit at a tape-measured distance and hold still.
    python camera_geometry.py --calibrate 62.5 --measure

    python camera_geometry.py --show
    python camera_geometry.py --sensitivity 129.7  # how much does it matter?

Measure from the CAMERA LENS to the bridge of your nose — not to the
front edge of the laptop, which on a 15" machine is a good 10 cm short
and would put a 17 % error into every degree figure in the study.

Stop the experiment server first; only one process can own the webcam.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
GEOMETRY_FILE = os.path.join(BASE, "data", "camera_geometry.json")

# Adult inter-pupillary distance: mean ~6.3 cm, SD ~0.4 cm. Used only
# when a participant's own IOD has not been measured.
POPULATION_IOD_CM = 6.3
POPULATION_IOD_SD_CM = 0.4
# A ruler measurement of one person's IOD is good to roughly this.
MEASURED_IOD_SD_CM = 0.2
# Assumed field of view, used ONLY as a fallback before calibration.
FALLBACK_HFOV_DEG = 60.0


def load() -> dict:
    if os.path.isfile(GEOMETRY_FILE):
        try:
            with open(GEOMETRY_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {}


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(GEOMETRY_FILE), exist_ok=True)
    with open(GEOMETRY_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def focal_from_hfov(image_w_px: int, hfov_deg: float = FALLBACK_HFOV_DEG
                    ) -> float:
    """Fallback focal length from an assumed field of view."""
    return (image_w_px / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def calibrate(iod_px: float, known_distance_cm: float,
              iod_cm: float = POPULATION_IOD_CM,
              image_w_px: int = 640,
              distance_sd_cm: float = 1.0) -> dict:
    """Solve for the camera's focal length from ONE measured distance.

    Sit at a distance you have actually measured with a tape, look at
    the camera, and record the inter-ocular pixel separation. Then::

        focal_px = IOD_px * distance_cm / IOD_cm

    This absorbs the field of view, the sensor size and any software
    resize in a single empirical constant — none of which need to be
    known separately. Valid for this camera at this capture resolution;
    re-run if either changes.
    """
    if iod_px <= 0 or known_distance_cm <= 0 or iod_cm <= 0:
        raise ValueError("iod_px, distance and iod_cm must all be positive")
    focal = iod_px * known_distance_cm / iod_cm
    implied_hfov = 2 * math.degrees(math.atan((image_w_px / 2.0) / focal))
    return {
        "focal_px": round(focal, 1),
        "image_w_px": image_w_px,
        "calibrated_at": datetime.now().isoformat(timespec="seconds"),
        "known_distance_cm": known_distance_cm,
        "distance_sd_cm": distance_sd_cm,
        "iod_cm_used": iod_cm,
        "iod_px_observed": round(iod_px, 1),
        "implied_hfov_deg": round(implied_hfov, 1),
        "fallback_hfov_deg": FALLBACK_HFOV_DEG,
        "hfov_error_vs_assumption_pct": round(
            100 * (implied_hfov / FALLBACK_HFOV_DEG - 1), 1),
        "note": "focal_px absorbs FOV, sensor size and software resize. "
                "Re-calibrate if the camera or capture resolution changes.",
    }


def estimate_distance(iod_px: float, geometry: dict = None,
                      iod_cm: float = None, image_w_px: int = 640) -> dict:
    """Viewing distance in cm, with an uncertainty and its sources.

    Returns ``distance_cm`` plus ``distance_sd_cm``, and states which
    assumptions remain so the manifest records what was measured versus
    inferred.
    """
    # `None` means "I did not specify one — go and find the saved
    # calibration". An EMPTY DICT means "there is no calibration",
    # deliberately. `geometry or load()` conflated the two, so passing
    # {} to model an uncalibrated camera silently loaded whatever
    # calibration happened to be on that machine.
    #
    # That is how run_tests passed everywhere except the one machine
    # that mattered: the test for the uncalibrated uncertainty budget
    # was correct until the collection laptop was actually calibrated,
    # at which point {} started returning the MEASURED figure and the
    # assertion failed. A test that only breaks once the setup is
    # complete is worse than no test.
    if geometry is None:
        geometry = load()
    sources = []

    if geometry.get("focal_px"):
        focal = float(geometry["focal_px"])
        # Rescale if capturing at a different width than at calibration.
        cal_w = geometry.get("image_w_px") or image_w_px
        if cal_w and image_w_px and cal_w != image_w_px:
            focal *= image_w_px / float(cal_w)
        focal_rel_sd = (geometry.get("distance_sd_cm", 1.0)
                        / max(1e-6, geometry.get("known_distance_cm", 60.0)))
        sources.append("focal length MEASURED (%.0f px, implied HFOV %.1f deg)"
                       % (focal, geometry.get("implied_hfov_deg", 0)))
    else:
        focal = focal_from_hfov(image_w_px, FALLBACK_HFOV_DEG)
        focal_rel_sd = 0.10          # a 55-75 deg FOV range is ~+-10 %
        sources.append("focal length ASSUMED (%.0f deg FOV) — run "
                       "camera_geometry.py --calibrate" % FALLBACK_HFOV_DEG)

    if iod_cm:
        iod_sd = MEASURED_IOD_SD_CM
        sources.append("participant IOD MEASURED (%.1f cm)" % iod_cm)
    else:
        iod_cm = POPULATION_IOD_CM
        iod_sd = POPULATION_IOD_SD_CM
        sources.append("participant IOD ASSUMED (population mean %.1f cm, "
                       "SD %.1f)" % (POPULATION_IOD_CM, POPULATION_IOD_SD_CM))

    if iod_px <= 0:
        return {"distance_cm": None, "error": "no inter-ocular pixels",
                "sources": sources}

    distance = iod_cm * focal / iod_px
    # Relative errors add in quadrature (independent sources).
    rel_sd = math.hypot(focal_rel_sd, iod_sd / iod_cm)
    return {
        "distance_cm": round(distance, 1),
        "distance_sd_cm": round(distance * rel_sd, 1),
        "relative_sd_pct": round(100 * rel_sd, 1),
        "focal_px": round(focal, 1),
        "iod_px": round(iod_px, 1),
        "iod_cm": iod_cm,
        "focal_measured": bool(geometry.get("focal_px")),
        "iod_measured": bool(iod_cm != POPULATION_IOD_CM),
        "sources": sources,
    }


def px_per_degree(distance_cm: float, w_px: int = 1920, h_px: int = 1080,
                  diag_in: float = 15.6) -> float:
    diag_cm = diag_in * 2.54
    w_cm = diag_cm * math.cos(math.atan2(h_px, w_px))
    return (w_px / w_cm) * distance_cm * math.tan(math.radians(1.0))


def degrees_with_uncertainty(error_px: float, distance_cm: float,
                             distance_sd_cm: float = 0.0,
                             w_px: int = 1920, h_px: int = 1080,
                             diag_in: float = 15.6) -> dict:
    """Convert px to degrees AND carry the distance uncertainty through.

    Reporting a bare "2.21 deg" hides that the number rests on a
    distance. Reporting the interval makes the dependency visible, and
    makes it obvious when a session sits close enough to the inclusion
    threshold that the distance uncertainty alone could flip it.
    """
    if not distance_cm or distance_cm <= 0:
        return {"deg": None, "error": "no viewing distance"}

    def _deg(d):
        return math.degrees(math.atan(
            (error_px / (px_per_degree(1.0, w_px, h_px, diag_in)
                         / math.tan(math.radians(1.0)))) / d))

    centre = _deg(distance_cm)
    out = {"deg": round(centre, 2), "distance_cm": distance_cm,
           "error_px": round(error_px, 1)}
    if distance_sd_cm:
        # 95 % interval: +-1.96 SD on the distance. Nearer = larger angle.
        lo = _deg(distance_cm + 1.96 * distance_sd_cm)
        hi = _deg(distance_cm - 1.96 * distance_sd_cm)
        out.update({"deg_lo": round(lo, 2), "deg_hi": round(hi, 2),
                    "distance_sd_cm": distance_sd_cm,
                    "interval_note": "95 % interval from the viewing-distance "
                                     "uncertainty alone; the pixel "
                                     "measurement itself is exact"})
    return out


def _sensitivity(error_px: float) -> int:
    print("Measured error: %.1f px" % error_px)
    print()
    print("  %-12s %-10s %-14s" % ("distance", "degrees", "vs 60 cm"))
    print("  " + "-" * 38)
    base = degrees_with_uncertainty(error_px, 60.0)["deg"]
    for d in (45, 50, 55, 60, 65, 70, 80):
        v = degrees_with_uncertainty(error_px, float(d))["deg"]
        print("  %-12s %-10.2f %+.0f %%" % ("%d cm" % d, v,
                                            100 * (v / base - 1)))
    print()
    print("  The pixel measurement is identical in every row. Only the")
    print("  assumed distance changes — and with it, whether the session")
    print("  passes a 3 deg threshold.")
    return 0


def measure_live(seconds: float = 6.0, camera: int = 0,
                 width: int = 640, height: int = 480) -> "dict | None":
    """Read the iris and inter-ocular pixel sizes straight from the camera.

    WHY THIS EXISTS
    ---------------
    Calibration previously required running the app, opening the
    position guide, reading a number off the screen while holding a tape
    measure, and typing it into a second terminal. Every one of those
    steps is a chance to move, and moving is the entire error term. This
    does it in one command: sit still at the measured distance and it
    samples for a few seconds.

    Returns MEDIANS over the window, plus the spread. The spread is not
    decoration — it is how you know whether you actually held still. A
    5 % spread in iris pixels at 60 cm is 3 cm of head movement, which
    is larger than the error the calibration is trying to remove.

    Both rulers are measured because they have different priors:

        iris   11.7 mm +- 0.5  (~4 %)  — a physiological constant, and
                                         nearly yaw-invariant
        IOD    6.3 cm +- 0.4   (~11 %) — population mean, and it
                                         foreshortens as the head turns

    The iris is the better basis and is what the runtime prefers, so it
    is what the focal length is solved from — but computing both and
    comparing them catches a bad landmark fit, which would otherwise
    bake a silent error into every distance in the study.
    """
    try:
        import cv2
        import mediapipe as mp
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        print("Missing dependency: %s" % exc)
        return None

    import iris_distance

    cap = cv2.VideoCapture(camera, cv2.CAP_DSHOW) \
        if sys.platform.startswith("win") else cv2.VideoCapture(camera)
    if not cap or not cap.isOpened():
        print("Could not open camera %d — is the experiment server running? "
              "It owns the webcam." % camera)
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    # refine_landmarks=True is REQUIRED: the iris points (468-477) do not
    # exist in the coarse 468-point mesh, and without them this silently
    # measures nothing.
    mesh = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False, max_num_faces=1,
        refine_landmarks=True, min_detection_confidence=0.5,
        min_tracking_confidence=0.5)

    iris_vals: list = []
    iod_vals: list = []
    asym_vals: list = []
    frames = 0
    faces = 0
    # WHY THESE ARE COLLECTED RATHER THAN SWALLOWED
    # The first version of this function wrapped the iris read in a bare
    # `except: pass` AND asked for the wrong dictionary key, so it
    # reported "0 usable frames out of 177" on a run where the face was
    # detected in every single frame. The failure and "your face wasn't
    # visible" were indistinguishable, and the advice was wrong. A
    # measurement tool that cannot say WHY it measured nothing is worse
    # than no tool.
    reasons: dict = {}

    def _note(msg: str) -> None:
        reasons[msg] = reasons.get(msg, 0) + 1

    t_end = time.perf_counter() + seconds
    try:
        while time.perf_counter() < t_end:
            ok, frame = cap.read()
            if not ok:
                break
            frames += 1
            h, w = frame.shape[:2]
            res = mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if not res.multi_face_landmarks:
                _note("no face detected in the frame")
                continue
            faces += 1
            lm = res.multi_face_landmarks[0].landmark
            try:
                iris = iris_distance.iris_diameter_px(lm, w, h)
                # NOTE the key: iris_diameter_px returns mean_px (the
                # average of left_px and right_px), not iris_px.
                if iris.get("error"):
                    _note(str(iris["error"]))
                elif iris.get("mean_px"):
                    iris_vals.append(float(iris["mean_px"]))
                    if iris.get("asymmetry_pct") is not None:
                        asym_vals.append(float(iris["asymmetry_pct"]))
                else:
                    _note("iris_diameter_px returned no mean_px: %s"
                          % sorted(iris.keys()))
            except Exception as exc:  # noqa: BLE001
                _note("iris read raised %s: %s"
                      % (type(exc).__name__, str(exc)[:80]))
            if frames == 1 and not iris_vals:
                # Fail on the FIRST frame rather than after the full
                # window. Six seconds of sitting still is cheap; six
                # seconds of sitting still to be told nothing was
                # measured is not.
                print("  (first frame produced no iris reading — "
                      "continuing, but expect this to fail)")
            # IRIS CENTRES (468, 473) — the actual pupil centres in the
            # refined mesh, and therefore the only pair whose separation
            # is the INTER-PUPILLARY distance that POPULATION_IOD_CM
            # (6.3 cm) describes.
            #
            # This used landmarks 33 and 263, which are the OUTER EYE
            # CORNERS. Outer-canthal separation is ~8.4 cm in adults, not
            # 6.3 — so dividing it by 6.3 inflated the focal length by
            # exactly that ratio and produced a spurious 24.7 % quarrel
            # with the iris. Both numbers were measuring the camera
            # correctly; one of them was being told the wrong thing about
            # the face. The cross-check is meant to catch a bad landmark
            # fit, so a cross-check that cries wolf is worse than none.
            try:
                lx, ly = lm[468].x * w, lm[468].y * h
                rx, ry = lm[473].x * w, lm[473].y * h
                iod_vals.append(float(np.hypot(rx - lx, ry - ly)))
            except Exception as exc:  # noqa: BLE001
                _note("IOD read raised %s" % type(exc).__name__)
    finally:
        cap.release()
        try:
            mesh.close()
        except Exception:  # noqa: BLE001
            pass

    if len(iris_vals) < 10:
        print("Only %d usable iris reads from %d frames (%d of which had a "
              "detected face)." % (len(iris_vals), frames, faces))
        if faces >= 10:
            # The distinction that matters: the camera and the face
            # detector were fine, so telling the user to fix their
            # lighting would send them to fix nothing.
            print("Your face WAS detected — this is not a lighting or "
                  "seating problem. The iris measurement itself failed:")
        for msg, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:4]:
            print("   %5d x  %s" % (n, msg))
        if not reasons:
            print("   (no reason recorded — the loop never ran)")
        return None

    def _spread(vals) -> float:
        med = statistics.median(vals)
        return 100.0 * (statistics.pstdev(vals) / med) if med else 0.0

    return {
        "iris_px": statistics.median(iris_vals),
        "iris_spread_pct": _spread(iris_vals),
        "iod_px": statistics.median(iod_vals) if iod_vals else None,
        "iod_spread_pct": _spread(iod_vals) if iod_vals else None,
        "n_frames": len(iris_vals),
        # Left and right irises are the same physical size, so a
        # persistent difference means one eye's landmarks are wrong
        # (head yaw, a glasses rim, hair). Averaging them then quietly
        # biases the focal length.
        "iris_asymmetry_pct": (round(statistics.median(asym_vals), 1)
                               if asym_vals else None),
    }


def fit_multi(points: list, iris_mm: float = None) -> dict:
    """Fit ONE focal length to SEVERAL measured distances.

    WHY THIS EXISTS
    ---------------
    ``--calibrate`` solves the focal length from a single tape reading and
    overwrites whatever was there. Calibrating twice therefore does not
    accumulate evidence: the second run replaces the first, and the
    information in the discarded fit is lost.

    That is wasteful, because focal length is a property of the CAMERA
    and must not depend on how far away the person sat. Several
    observations at different distances are therefore not repetitions —
    they are a test the single-point procedure cannot perform, and
    fitting one focal length across all of them uses every point.

    THE OFFSET TERM
    ---------------
    Two models are fitted and compared:

      A. ``d = k*f / px``            focal only
      B. ``d = k*f / px - delta``    focal plus a constant tape offset

    Model B exists because the most likely systematic error in this
    procedure is not the model but the ruler: measuring consistently to
    the screen surface rather than the lens, or to the front of the face
    rather than the nose bridge, shifts EVERY reading by the same
    amount. That signature is a focal length that appears to grow with
    the calibration distance — which is exactly what two points at 45
    and 65 cm showed.

    With two points, model B fits exactly and tests nothing (two
    parameters, two observations). **Three or more points are needed
    before the offset means anything**, and the function says so rather
    than reporting a number that cannot be wrong.
    """
    import iris_distance as _id

    k = (iris_mm if iris_mm is not None else _id.IRIS_DIAMETER_MM) / 10.0
    pts = [(float(d), float(px)) for d, px in points if d > 0 and px > 0]
    if len(pts) < 2:
        return {"error": "need at least two (distance, iris_px) points"}

    # Model A: minimise squared error in the DISTANCE the ruler would
    # report, because that is the quantity the study consumes.
    num = sum(d / px for d, px in pts)
    den = k * sum(1.0 / (px * px) for _, px in pts)
    f_a = num / den
    res_a = [(k * f_a / px) - d for d, px in pts]

    out = {
        "n_points": len(pts),
        "focal_px": round(f_a, 1),
        "residuals_cm": [round(r, 2) for r in res_a],
        "rms_cm": round((sum(r * r for r in res_a) / len(res_a)) ** 0.5, 2),
        "worst_pct": round(max(abs(r) / d * 100 for r, (d, _) in
                               zip(res_a, pts)), 1),
        "points": [{"tape_cm": d, "iris_px": round(px, 2),
                    "focal_if_alone_px": round(px * d / k, 1)} for d, px in pts],
    }

    # Model B: focal and a constant tape offset, by ordinary least
    # squares on [k/px, -1].
    n = len(pts)
    x = [k / px for _, px in pts]
    sxx = sum(v * v for v in x)
    sx = sum(x)
    sy = sum(d for d, _ in pts)
    sxy = sum(v * d for v, (d, _) in zip(x, pts))
    detm = sxx * n - sx * sx
    if abs(detm) > 1e-12:
        f_b = (sxy * n - sx * sy) / detm
        delta = (sxx * sy - sx * sxy) / detm
        delta = f_b * 0 + (f_b * sx - sy) / n  # d = f*x - delta
        res_b = [(k * f_b / px) - delta - d for d, px in pts]
        out["offset_model"] = {
            "focal_px": round(f_b, 1),
            "tape_offset_cm": round(-delta, 2),
            "rms_cm": round((sum(r * r for r in res_b) / n) ** 0.5, 2),
            "meaningful": n >= 3,
            "note": ("two points fit two parameters exactly — this offset "
                     "cannot be wrong and therefore tests nothing. Add a "
                     "third distance." if n < 3 else
                     "compare rms_cm against the focal-only model; a large "
                     "reduction means the tape, not the model, is off"),
        }
    return out


def _report_fit(points: list, do_save: bool = False) -> int:
    res = fit_multi(points)
    print("=" * 68)
    print("  MULTI-POINT FOCAL FIT — one focal length, several distances")
    print("=" * 68)
    if res.get("error"):
        print("  %s" % res["error"])
        return 1
    print("  points used        : %d" % res["n_points"])
    for p in res["points"]:
        print("    %5.1f cm  iris %5.2f px   (alone would give %.1f px)"
              % (p["tape_cm"], p["iris_px"], p["focal_if_alone_px"]))
    print()
    print("  FOCAL ONLY")
    print("    focal            : %.1f px" % res["focal_px"])
    print("    residuals        : %s cm"
          % ", ".join("%+.2f" % r for r in res["residuals_cm"]))
    print("    rms              : %.2f cm   worst %.1f %%"
          % (res["rms_cm"], res["worst_pct"]))
    ob = res.get("offset_model")
    if ob:
        print()
        print("  FOCAL + CONSTANT TAPE OFFSET")
        print("    focal            : %.1f px" % ob["focal_px"])
        # Sign spelled out in words. A bare "+4.4 cm" is ambiguous about
        # which of the two distances it applies to, and getting it
        # backwards inverts the remedy.
        _off = ob["tape_offset_cm"]
        print("    tape offset      : %.2f cm — the tape reads %s the true "
              "distance" % (abs(_off), "SHORT of" if _off < 0 else "LONG of"))
        print("                       (true distance = tape %s %.2f cm)"
              % ("+" if _off < 0 else "-", abs(_off)))
        print("    rms              : %.2f cm" % ob["rms_cm"])
        print("    %s" % ob["note"])
    print()
    if res["worst_pct"] <= 5.0:
        print("  One focal length fits every distance to within %.1f %%."
              % res["worst_pct"])
        print("  The ruler is consistent across the range measured.")
        if do_save:
            # The pooled estimate replaces whichever single fit happened
            # to run last, and carries its own provenance so a reader can
            # see how many points it rests on.
            geom = load() or {}
            geom.update({
                "focal_px": res["focal_px"],
                "focal_basis": "pooled fit over %d measured distances "
                               "(iris)" % res["n_points"],
                "fit_points": res["points"],
                "fit_residuals_cm": res["residuals_cm"],
                "fit_rms_cm": res["rms_cm"],
                "fit_worst_pct": res["worst_pct"],
                "known_distance_cm": None,
                "calibrated_at": datetime.now().isoformat(timespec="seconds"),
            })
            if res.get("offset_model"):
                geom["fit_offset_model"] = res["offset_model"]
            save(geom)
            print()
            print("  SAVED — focal_px is now %.1f px, from %d points."
                  % (res["focal_px"], res["n_points"]))
            print("  Verify it at a distance you did NOT calibrate at:")
            print("      python camera_geometry.py --verify <cm>")
        else:
            print()
            print("  Nothing written. Add --save to adopt this focal length.")
        return 0
    print("  No single focal length fits all points (worst %.1f %%)."
          % res["worst_pct"])
    print("  Either a tape reading is wrong or the iris measurement failed")
    print("  at one distance. Look at the residuals above before refitting.")
    return 1


def _verify(tape_cm: float, seconds: float = 6.0, width: int = 640) -> int:
    """Does the SAVED focal length reproduce a tape measurement?

    CALIBRATION IS NOT VALIDATION, and the difference is the whole
    point of this function. ``--calibrate 60`` SOLVES the focal length
    so that 60 cm comes out; asking it afterwards whether it reads 60 cm
    is asking a fit to score itself, and it will always pass. Running it
    again at a new distance does not help either — it just replaces the
    constant, and the check stays circular.

    This one measures with the constant it already has and compares
    against an independent tape reading. Nothing is written, so the
    check cannot quietly become a refit.

    Verify at a distance DIFFERENT from the one you calibrated at. A
    ruler that is right at its own fit point and wrong 15 cm away is not
    a ruler, and participants will not all sit where you sat.
    """
    geom = load() or {}
    focal = geom.get("focal_px")
    print("=" * 68)
    print("  VERIFY THE RULER — no refit, nothing is written")
    print("=" * 68)
    if not focal:
        print("  No saved calibration to verify. Run:")
        print("      python camera_geometry.py --calibrate <cm> --measure")
        return 1
    print("  saved focal        : %.1f px  (calibrated at %s cm)"
          % (focal, geom.get("known_distance_cm", "?")))
    print("  tape says          : %.1f cm" % tape_cm)
    print("  Sit at exactly that distance, camera lens to the bridge of")
    print("  your nose, and hold still for %.0f s…" % seconds)
    print()

    live = measure_live(seconds, width=width)
    if not live:
        return 1

    import iris_distance

    est = iris_distance.distance_from_iris(live["iris_px"], focal)
    if not est:
        print("  no usable iris measurement")
        return 1
    got = est["distance_cm"]
    err_pct = 100.0 * abs(got - tape_cm) / tape_cm

    print("  iris               : %.2f px (spread %.1f %% over %d frames)"
          % (live["iris_px"], live["iris_spread_pct"], live["n_frames"]))
    print("  iris says          : %.1f cm" % got)
    print("  disagreement       : %.1f %%" % err_pct)
    print()
    if live["iris_spread_pct"] > 3.0:
        print("  SPREAD %.1f %% — you moved. That is not a verification of"
              % live["iris_spread_pct"])
        print("  the ruler, it is a measurement of your head. Re-run.")
        return 1
    if err_pct <= 5.0:
        print("  PASS — the saved focal length reproduces an independent")
        print("  measurement to within %.1f %%, which is inside the iris"
              % err_pct)
        print("  ruler's own ~4 %% biological spread. Distances, and so")
        print("  every accuracy figure in degrees, are trustworthy at")
        print("  this distance.")
        return 0
    print("  FAIL — %.1f %% off. Every degree figure scales with the" % err_pct)
    print("  distance, so an accuracy of 1.0 deg measured this way is")
    print("  really %.2f deg. Check the tape (lens to nose bridge, not"
          % (1.0 * tape_cm / got if got else 0.0))
    print("  to the laptop edge) before re-calibrating — a bad tape")
    print("  reading is the usual cause, and re-calibrating on it bakes")
    print("  the error in permanently.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calibrate", type=float, metavar="CM",
                    help="your MEASURED distance from the screen, in cm")
    ap.add_argument("--measure", action="store_true",
                    help="read the iris/IOD pixels from the camera instead "
                         "of prompting for them (recommended)")
    ap.add_argument("--seconds", type=float, default=6.0,
                    help="how long to sample when using --measure")
    ap.add_argument("--iod-px", type=float,
                    help="observed inter-ocular pixels (from the position "
                         "guide); omit to be prompted")
    ap.add_argument("--iod-cm", type=float, default=POPULATION_IOD_CM,
                    help="your own inter-pupillary distance, if measured")
    ap.add_argument("--image-w", type=int, default=640)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--sensitivity", type=float, metavar="ERROR_PX")
    ap.add_argument("--verify", type=float, metavar="CM",
                    help="check the SAVED focal length against a tape "
                         "measurement WITHOUT refitting it")
    ap.add_argument("--fit", metavar="D:PX,D:PX,...",
                    help="fit ONE focal length across several measured "
                         "distances, e.g. --fit 45:16.38,65:11.66")
    ap.add_argument("--save", action="store_true",
                    help="with --fit: adopt the pooled focal length")
    args = ap.parse_args()

    if args.sensitivity:
        return _sensitivity(args.sensitivity)

    if args.verify:
        return _verify(args.verify, args.seconds, args.image_w)

    if args.fit:
        pts = []
        for chunk in args.fit.replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                d_s, px_s = chunk.split(":")
                pts.append((float(d_s), float(px_s)))
            except ValueError:
                print("  cannot read %r — use  45:16.38,65:11.66" % chunk)
                return 1
        return _report_fit(pts, do_save=args.save)

    if args.show:
        g = load()
        if not g:
            print("No camera calibration yet. Distance estimates fall back "
                  "to an assumed %.0f deg field of view (~10 %% error)."
                  % FALLBACK_HFOV_DEG)
            print("Fix it once:  python camera_geometry.py --calibrate <cm>")
            return 1
        for k, v in g.items():
            print("  %-32s %s" % (k, v))
        print()
        print("  Implied FOV differs from the %.0f deg assumption by %s %%."
              % (FALLBACK_HFOV_DEG, g.get("hfov_error_vs_assumption_pct")))
        return 0

    if args.calibrate:
        iod_px = args.iod_px
        live = None
        if args.measure and not iod_px:
            print("=" * 68)
            print("  MEASURING — sit at exactly %.1f cm and HOLD STILL"
                  % args.calibrate)
            print("=" * 68)
            print("  Measure from the CAMERA LENS to the bridge of your")
            print("  nose, not to the front edge of the laptop. Look at")
            print("  the camera. Sampling for %.0f s…" % args.seconds)
            print()
            live = measure_live(args.seconds, width=args.image_w)
            if not live:
                return 1
            iod_px = live["iod_px"]
            print("  iris   %6.2f px  (spread %.1f %% over %d frames)"
                  % (live["iris_px"], live["iris_spread_pct"],
                     live["n_frames"]))
            if live.get("iod_px"):
                print("  IOD    %6.2f px  (spread %.1f %%)"
                      % (live["iod_px"], live["iod_spread_pct"]))
            print()
            # A wide spread means the head moved, and head movement is
            # the whole error term this calibration exists to remove.
            # Calibrating on a moving head bakes that motion into a
            # constant used by every distance in the study.
            worst = max(live["iris_spread_pct"],
                        live.get("iod_spread_pct") or 0.0)
            if worst > 3.0:
                print("  *** SPREAD %.1f %% — that is head movement, not"
                      % worst)
                print("      measurement noise. At 60 cm it is roughly")
                print("      %.0f cm of drift. Re-run and hold still; a"
                      % (0.6 * worst))
                print("      calibration is only as good as the moment it")
                print("      was taken. ***")
                print()
        if not iod_px:
            print("Start the app, open the position guide, and read off the")
            print("inter-ocular pixel value while sitting at exactly %.1f cm."
                  % args.calibrate)
            print("(Or just re-run with --measure and skip all that.)")
            try:
                iod_px = float(input("inter_ocular_px: ").strip())
            except (ValueError, EOFError):
                print("Not a number — aborted.")
                return 1
        data = calibrate(iod_px, args.calibrate, args.iod_cm, args.image_w)
        if live:
            # Solve the focal length from the IRIS as well. Same physical
            # constant, much tighter prior (11.7 mm +- 0.5 is ~4 %, versus
            # ~11 % for a population-mean IOD), and it is the ruler the
            # runtime actually prefers. If the two disagree by more than a
            # few percent the landmark fit is suspect and neither number
            # should be trusted.
            import iris_distance

            iris_focal = (live["iris_px"] * args.calibrate
                          / (iris_distance.IRIS_DIAMETER_MM / 10.0))
            data["focal_px_from_iris"] = round(iris_focal, 1)
            data["iris_px_observed"] = round(live["iris_px"], 2)
            data["iris_spread_pct"] = round(live["iris_spread_pct"], 2)
            data["focal_disagreement_pct"] = round(
                100.0 * abs(iris_focal - data["focal_px"])
                / data["focal_px"], 1)
            # Prefer the iris. Keep the IOD figure alongside it so the
            # choice is visible and reversible rather than silent.
            data["focal_px_from_iod"] = data["focal_px"]
            data["focal_px"] = round(iris_focal, 1)
            data["focal_basis"] = ("iris (11.7 mm +- 0.5); the IOD figure "
                                   "is retained for comparison")
            # RECOMPUTE the derived FOV fields. calibrate() filled them
            # from the IOD focal, and overwriting focal_px without them
            # left a record whose implied_hfov_deg described a focal
            # length no longer in the file — the report printed 40.4 deg
            # next to a 655.9 px focal that actually implies 52.0.
            implied = 2 * math.degrees(
                math.atan((args.image_w / 2.0) / iris_focal))
            data["implied_hfov_deg"] = round(implied, 1)
            data["hfov_error_vs_assumption_pct"] = round(
                100 * (implied / FALLBACK_HFOV_DEG - 1), 1)
        save(data)
        print()
        for k, v in data.items():
            print("  %-32s %s" % (k, v))
        print()
        print("  Saved to %s" % os.path.relpath(GEOMETRY_FILE, BASE))
        print("  The %.0f deg FOV assumption is now GONE — distance is "
              "derived from a measured focal length." % FALLBACK_HFOV_DEG)
        dis = data.get("focal_disagreement_pct")
        if dis is not None and dis > 8.0:
            print()
            print("  *** The iris and the IOD imply focal lengths %.1f %% "
                  "apart. *** " % dis)
            print("      They measure the same camera, so a gap this wide")
            print("      means the landmark fit is off (glasses, head yaw,")
            print("      or poor lighting) — or your own IPD is far from")
            print("      the 6.3 cm population mean. Re-run facing the")
            print("      camera squarely before trusting this.")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
