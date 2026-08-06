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

    python camera_geometry.py --calibrate 62.5     # sit at a measured 62.5 cm
    python camera_geometry.py --show
    python camera_geometry.py --sensitivity 129.7  # how much does it matter?
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
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
    geometry = geometry or load()
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calibrate", type=float, metavar="CM",
                    help="your MEASURED distance from the screen, in cm")
    ap.add_argument("--iod-px", type=float,
                    help="observed inter-ocular pixels (from the position "
                         "guide); omit to be prompted")
    ap.add_argument("--iod-cm", type=float, default=POPULATION_IOD_CM,
                    help="your own inter-pupillary distance, if measured")
    ap.add_argument("--image-w", type=int, default=640)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--sensitivity", type=float, metavar="ERROR_PX")
    args = ap.parse_args()

    if args.sensitivity:
        return _sensitivity(args.sensitivity)

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
        if not iod_px:
            print("Start the app, open the position guide, and read off the")
            print("inter-ocular pixel value while sitting at exactly %.1f cm."
                  % args.calibrate)
            try:
                iod_px = float(input("inter_ocular_px: ").strip())
            except (ValueError, EOFError):
                print("Not a number — aborted.")
                return 1
        data = calibrate(iod_px, args.calibrate, args.iod_cm, args.image_w)
        save(data)
        print()
        for k, v in data.items():
            print("  %-32s %s" % (k, v))
        print()
        print("  Saved to %s" % os.path.relpath(GEOMETRY_FILE, BASE))
        print("  The %.0f deg FOV assumption is now GONE — distance is "
              "derived from a measured focal length." % FALLBACK_HFOV_DEG)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
