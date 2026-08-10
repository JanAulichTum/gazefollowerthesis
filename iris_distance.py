# -*- coding: utf-8 -*-
"""
Viewing distance from the IRIS — a better ruler, and a second opinion.

WHY THE IRIS
------------
Distance is estimated by comparing a known physical size against its
size in pixels. The quality of the estimate is therefore the quality of
the ruler, and the pipeline has been using a poor one.

    inter-ocular distance   6.3 cm, SD ~0.4  ->  +-6.3 % biological
                            spread. Varies with sex, ethnicity and age.

    IRIS DIAMETER           11.7 mm, SD 0.5  ->  +-4.3 % biological
                            spread, and it is a PHYSIOLOGICAL CONSTANT,
                            not a population average: adult iris size is
                            essentially independent of age, sex and
                            ethnicity. This is the basis of Google's
                            MediaPipe Iris depth estimation, which
                            reports 4.3 % mean relative error (4.8 % with
                            eyeglasses).

MediaPipe's refined face mesh already emits the iris landmarks
(468-477), and GazeFollower runs that mesh with refine_landmarks
enabled for every frame. So the better ruler costs nothing — it is
already being computed and thrown away.

THE SECOND OPINION (this is the real addition)
----------------------------------------------
Having two independent physiological rulers on the SAME frame gives a
consistency check that neither provides alone. Iris and IOD scale with
distance identically, so under normal conditions the two estimates must
agree. When they diverge, something is wrong with the measurement:

    * landmark fit failed on one eye (partial occlusion, hair, squint)
    * eyeglass frames or strong prescription distorting the iris edge
    * extreme head yaw — the IOD foreshortens with cos(yaw) while the
      iris, being roughly circular and frontal to its own eye, does not
    * one eye closed or mid-blink

None of those raise an error today; they silently corrupt the distance,
and through it every degree figure in the thesis. ``cross_check()``
turns that silence into a number.

WHAT THIS DOES NOT SOLVE
------------------------
Absolute distance still needs the camera's focal length, and a single
uncalibrated camera cannot recover it — that is scale ambiguity, a
fundamental limit, not a missing feature. Phones expose focal length via
EXIF/camera APIs; laptop webcams generally do not. So either calibrate
once (camera_geometry.py --calibrate) or accept the assumed field of
view. What the iris improves is the OTHER error term, and what
cross_check() adds is a way to detect when either estimate is broken.
"""

from __future__ import annotations

import math

# Horizontal iris diameter. Physiological constant, not a population
# mean: 11.7 mm +- 0.5 across adults, stable across age/sex/ethnicity.
IRIS_DIAMETER_MM = 11.7
IRIS_DIAMETER_SD_MM = 0.5

# MediaPipe refined-mesh iris landmark indices.
#   468 = left-eye iris centre, 469-472 its rim (right, top, left, bottom)
#   473 = right-eye iris centre, 474-477 its rim
LEFT_IRIS = (468, 469, 470, 471, 472)
RIGHT_IRIS = (473, 474, 475, 476, 477)

# Two estimates of the same quantity should not differ by more than
# this. 15 % is roughly three times the iris method's own 4.3 % error,
# so a flag means "something is actually wrong", not "noise".
AGREEMENT_TOLERANCE_PCT = 15.0


def _xy(landmark, img_w: float, img_h: float):
    """Pixel coordinates from a landmark in whichever form it arrives.

    MediaPipe emits normalised objects with .x/.y; GazeFollower may hand
    on numpy rows or plain tuples, already in pixels or not. Guessing
    wrong silently produces a plausible-but-wrong diameter, so treat
    values <= 1.5 as normalised and scale them.
    """
    try:
        x = float(getattr(landmark, "x", landmark[0]))
        y = float(getattr(landmark, "y", landmark[1]))
    except Exception:  # noqa: BLE001
        return None
    if abs(x) <= 1.5 and abs(y) <= 1.5:      # normalised
        x, y = x * img_w, y * img_h
    return x, y


def iris_diameter_px(landmarks, img_w: float, img_h: float) -> dict:
    """Horizontal iris diameter in pixels, per eye.

    Horizontal, not vertical: the eyelids clip the top and bottom of the
    iris in normal open-eye posture, so the vertical extent is routinely
    an underestimate. The horizontal extent is visible whenever the eye
    is open at all.
    """
    out: dict = {}
    if landmarks is None:
        return {"error": "no landmarks"}
    try:
        n = len(landmarks)
    except Exception:  # noqa: BLE001
        return {"error": "landmarks not indexable"}
    if n < 478:
        return {"error": "mesh has %d landmarks; iris needs the refined "
                         "478-point mesh (refine_landmarks=True)" % n}

    for name, idx in (("left", LEFT_IRIS), ("right", RIGHT_IRIS)):
        # rim order is (right, top, left, bottom) -> horizontal = 1 vs 3
        a = _xy(landmarks[idx[1]], img_w, img_h)
        b = _xy(landmarks[idx[3]], img_w, img_h)
        if a and b:
            d = math.hypot(a[0] - b[0], a[1] - b[1])
            if d > 1:
                out[name + "_px"] = round(d, 2)
    if not out:
        return {"error": "iris landmarks unusable"}
    vals = [v for k, v in out.items() if k.endswith("_px")]
    out["mean_px"] = round(sum(vals) / len(vals), 2)
    if len(vals) == 2:
        # Left/right iris should be the same size. A large difference
        # means one eye's landmarks are unreliable (occlusion, yaw).
        out["asymmetry_pct"] = round(
            100 * abs(vals[0] - vals[1]) / out["mean_px"], 1)
    return out


def distance_from_iris(iris_px: float, focal_px: float,
                       iris_mm: float = IRIS_DIAMETER_MM) -> "dict | None":
    """Distance in cm from the iris, with its biological uncertainty."""
    if not iris_px or iris_px <= 0 or not focal_px or focal_px <= 0:
        return None
    distance_cm = (iris_mm / 10.0) * focal_px / iris_px
    rel_sd = IRIS_DIAMETER_SD_MM / IRIS_DIAMETER_MM
    return {
        "distance_cm": round(distance_cm, 1),
        "biological_sd_pct": round(100 * rel_sd, 1),
        "ruler": "iris %.1f mm +- %.1f" % (iris_mm, IRIS_DIAMETER_SD_MM),
    }


def distance_from_iod(iod_px: float, focal_px: float,
                      iod_cm: float = 6.3) -> "dict | None":
    """Distance in cm from the inter-ocular separation, for comparison."""
    if not iod_px or iod_px <= 0 or not focal_px or focal_px <= 0:
        return None
    return {
        "distance_cm": round(iod_cm * focal_px / iod_px, 1),
        "biological_sd_pct": round(100 * 0.4 / iod_cm, 1),
        "ruler": "inter-ocular %.1f cm +- 0.4" % iod_cm,
    }


def cross_check(iris_cm: "float | None", iod_cm: "float | None",
                tolerance_pct: float = AGREEMENT_TOLERANCE_PCT) -> dict:
    """Do the two independent rulers agree?

    Agreement does not prove the distance is right — both share the same
    focal length, so a wrong focal length moves them together. What
    disagreement proves is that at least one MEASUREMENT is broken, and
    that is worth knowing before the number propagates into every degree
    figure for the session.
    """
    if iris_cm is None or iod_cm is None:
        return {"ok": False,
                "reason": "only one estimate available (%s)"
                          % ("iris" if iris_cm else "iod" if iod_cm else "none"),
                "distance_cm": iris_cm or iod_cm}
    mean = (iris_cm + iod_cm) / 2.0
    diff_pct = 100 * abs(iris_cm - iod_cm) / mean if mean else 0.0
    agree = diff_pct <= tolerance_pct
    out = {
        "ok": True,
        "iris_cm": iris_cm,
        "iod_cm": iod_cm,
        # The iris is the better ruler, so it is the reported value when
        # the two agree; the IOD is corroboration, not an average.
        "distance_cm": iris_cm,
        "difference_pct": round(diff_pct, 1),
        "tolerance_pct": tolerance_pct,
        "agree": agree,
    }
    if not agree:
        out["warning"] = (
            "iris and inter-ocular distance estimates differ by %.0f %% "
            "(%.1f vs %.1f cm). Likely causes: landmark fit failed on one "
            "eye, eyeglasses distorting the iris rim, or large head yaw "
            "(which foreshortens the IOD but not the iris). Treat this "
            "session's degree figures as unreliable until checked."
            % (diff_pct, iris_cm, iod_cm))
    return out


def estimate(landmarks, iod_px: float, focal_px: float,
             img_w: float = 640, img_h: float = 480,
             iris_mm: float = IRIS_DIAMETER_MM) -> dict:
    """Both estimates plus their agreement, in one call.

    Never raises: a distance estimate is a nice-to-have during a live
    session and must not be able to interrupt one.
    """
    result: dict = {}
    try:
        iris = iris_diameter_px(landmarks, img_w, img_h)
        result["iris"] = iris
        iris_est = (distance_from_iris(iris.get("mean_px"), focal_px, iris_mm)
                    if not iris.get("error") else None)
        iod_est = distance_from_iod(iod_px, focal_px)
        result["from_iris"] = iris_est
        result["from_iod"] = iod_est
        result["check"] = cross_check(
            (iris_est or {}).get("distance_cm"),
            (iod_est or {}).get("distance_cm"))
        # A large left/right iris difference is its own warning, even if
        # the mean happens to agree with the IOD.
        asym = iris.get("asymmetry_pct")
        if asym is not None and asym > 20:
            result["check"]["iris_asymmetry_warning"] = (
                "left and right iris differ by %.0f %% — one eye's "
                "landmarks are probably unreliable" % asym)
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:120]
    return result
