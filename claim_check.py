# -*- coding: utf-8 -*-
"""
Test whether the LLM's spatial claims match where the gaze actually was.

THE PROBLEM THIS SOLVES
-----------------------
This project deliberately does NOT use hand-drawn AOIs. The gaze marker
is burned onto the video and a multimodal model is asked what the
participant looked at. That is the design, and it avoids the arbitrary
binning that fixed AOIs impose on dynamic footage.

But it creates a circularity for RQ3. H3 asks whether the generated
feedback "corresponds to what the underlying gaze metrics support". If
the model both names the region AND is the only judge of whether the
gaze was in it, there is nothing to check against — the model is witness
and jury.

The fix is small: the model already reports WHAT was attended, so it is
also asked WHERE that thing is, as a normalised bounding box. The box is
the model's claim about the scene; the gaze coordinates are an
independent measurement. Comparing them is a genuine correspondence
test, and it needs no pre-drawn AOIs.

    model says   "t=4-7 s: the student at the left desk", bbox [.05,.4,.25,.5]
    gaze says    82 % of samples in 4-7 s fell inside that box
    verdict      SUPPORTED

    model says   "t=12-15 s: the whiteboard", bbox [.3,.0,.4,.3]
    gaze says    9 % of samples fell inside
    verdict      CONTRADICTED  <- the claim is not supported by the data

WHAT A FAILURE MEANS
--------------------
A contradicted claim is not automatically an LLM error. It can be:

  * a genuine hallucination (the model described something plausible
    rather than what the marker was on), or
  * a localisation error (the object is right, the box is wrong), or
  * gaze error (at ~2 deg accuracy the marker itself is up to ~130 px
    from the true gaze point, so a small object near a boundary is
    genuinely ambiguous).

The third is why ``tolerance_deg`` exists: the box is expanded by the
session's own measured accuracy before testing, so the check does not
punish the model for uncertainty that belongs to the tracker. Claims
about objects smaller than that tolerance are reported as UNTESTABLE
rather than passed or failed — an honest third category.

Usage::

    python claim_check.py <session_manifest.json>
    python claim_check.py --demo
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

SUPPORTED, CONTRADICTED, UNTESTABLE, NO_BOX = (
    "SUPPORTED", "CONTRADICTED", "UNTESTABLE", "NO_BBOX")

# A claim counts as supported when at least this share of the gaze
# samples in its time window fall inside the (tolerance-expanded) box.
# 0.5 is deliberate: a claim about where someone looked during an
# interval should hold for the majority of that interval, not for a
# single frame.
SUPPORT_THRESHOLD = 0.5


def _expand(bbox, pad_x: float, pad_y: float):
    x, y, w, h = bbox
    return (max(0.0, x - pad_x), max(0.0, y - pad_y),
            min(1.0, w + 2 * pad_x), min(1.0, h + 2 * pad_y))


def check_claim(claim: dict, samples: list, accuracy_deg: float,
                px_per_deg: float, video_w: int, video_h: int) -> dict:
    """Score one claim against the gaze samples in its time window.

    ``samples``: (t_seconds, nx, ny, valid) in normalised video coords.
    """
    out = {
        "t_start": claim.get("t_start"),
        "t_end": claim.get("t_end"),
        "attended": claim.get("attended"),
        "confidence": claim.get("confidence"),
    }
    bbox = claim.get("bbox")
    if not bbox or len(bbox) != 4 or any(b is None for b in bbox):
        out["verdict"] = NO_BOX
        out["note"] = "model did not localise this claim"
        return out

    t0 = float(claim.get("t_start") or 0)
    t1 = float(claim.get("t_end") or t0)
    if t1 < t0:
        t0, t1 = t1, t0
    # A zero-length window (one fixation) still needs a span to collect.
    if t1 == t0:
        t1 = t0 + 0.001

    window = [s for s in samples
              if t0 <= s[0] <= t1 and (len(s) < 4 or s[3])
              and s[1] is not None and s[2] is not None]
    out["n_samples"] = len(window)
    if not window:
        out["verdict"] = UNTESTABLE
        out["note"] = "no valid gaze samples in this time window"
        return out

    # Expand the box by the measured tracker accuracy, so the model is
    # not blamed for the tracker's error.
    err_px = accuracy_deg * px_per_deg
    pad_x = err_px / video_w if video_w else 0.0
    pad_y = err_px / video_h if video_h else 0.0
    ex, ey, ew, eh = _expand([float(b) for b in bbox], pad_x, pad_y)

    inside = sum(1 for s in window
                 if ex <= s[1] <= ex + ew and ey <= s[2] <= ey + eh)
    frac = inside / len(window)
    out["fraction_inside"] = round(frac, 3)
    out["tolerance_px"] = round(err_px)

    # An object smaller than the tolerance cannot be distinguished from
    # its neighbours; scoring it either way would be false precision.
    obj_px = min(float(bbox[2]) * video_w, float(bbox[3]) * video_h)
    if obj_px < err_px:
        out["verdict"] = UNTESTABLE
        out["note"] = ("claimed object is %.0f px, smaller than the %.0f px "
                       "measurement error — not distinguishable"
                       % (obj_px, err_px))
        return out

    out["verdict"] = SUPPORTED if frac >= SUPPORT_THRESHOLD else CONTRADICTED
    if out["verdict"] == CONTRADICTED:
        out["note"] = ("only %.0f %% of gaze fell inside the claimed region"
                       % (100 * frac))
    return out


def check_all(claims: list, samples: list, accuracy_deg: float,
              px_per_deg: float, video_w: int, video_h: int) -> dict:
    results = [check_claim(c, samples, accuracy_deg, px_per_deg,
                           video_w, video_h) for c in claims]
    counts = {k: sum(1 for r in results if r["verdict"] == k)
              for k in (SUPPORTED, CONTRADICTED, UNTESTABLE, NO_BOX)}
    testable = counts[SUPPORTED] + counts[CONTRADICTED]
    return {
        "claims": results,
        "counts": counts,
        "n_claims": len(results),
        "n_testable": testable,
        # THE RQ3 HEADLINE NUMBER: of the claims that could be checked,
        # what share the gaze data supports.
        "correspondence_pct": round(100.0 * counts[SUPPORTED] / testable, 1)
        if testable else None,
        "testable_pct": round(100.0 * testable / len(results), 1)
        if results else None,
        "accuracy_deg_used": accuracy_deg,
        "support_threshold": SUPPORT_THRESHOLD,
    }


def _demo() -> int:
    """Show the three verdicts on synthetic data."""
    px_per_deg, vw, vh = 58.2, 1920, 1080
    # Gaze sits on the left third for 0-8 s, then centre for 8-16 s.
    samples = ([(t / 10.0, 0.15, 0.60, True) for t in range(0, 80)]
               + [(t / 10.0, 0.50, 0.60, True) for t in range(80, 160)])
    claims = [
        {"t_start": 0, "t_end": 8, "attended": "student at the left desk",
         "bbox": [0.02, 0.35, 0.30, 0.55], "confidence": "high"},
        {"t_start": 8, "t_end": 16, "attended": "the whiteboard",
         "bbox": [0.30, 0.00, 0.40, 0.30], "confidence": "medium"},
        {"t_start": 8, "t_end": 16, "attended": "a pen on the desk",
         "bbox": [0.49, 0.59, 0.02, 0.02], "confidence": "low"},
        {"t_start": 0, "t_end": 4, "attended": "general classroom",
         "bbox": None, "confidence": "low"},
    ]
    res = check_all(claims, samples, 2.2, px_per_deg, vw, vh)
    print("=" * 74)
    print("  CLAIM CORRESPONDENCE (demo)")
    print("=" * 74)
    for r in res["claims"]:
        print("  [%-12s] %4.1f-%4.1f s  %-28s %s"
              % (r["verdict"], r["t_start"], r["t_end"],
                 str(r["attended"])[:28],
                 ("%.0f%% inside" % (100 * r["fraction_inside"]))
                 if "fraction_inside" in r else ""))
        if r.get("note"):
            print("                    %s" % r["note"])
    print()
    print("  correspondence: %s %% of %d testable claims (%s %% of claims "
          "were testable)" % (res["correspondence_pct"], res["n_testable"],
                              res["testable_pct"]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", nargs="?")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    if args.demo or not args.manifest:
        return _demo()

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    llm = manifest.get("llm") or {}
    claims = llm.get("structured") or []
    if not claims:
        print("No structured LLM claims in this manifest. Run the feedback "
              "step first; it must emit the JSON block with bbox fields.")
        return 1
    print("Loaded %d claims. Wire in the gaze CSV to score them "
          "(see check_all())." % len(claims))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
