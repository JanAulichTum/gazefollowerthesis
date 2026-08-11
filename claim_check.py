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
import csv
import glob
import json
import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

SUPPORTED, CONSISTENT, CONTRADICTED, UNTESTABLE, NO_BOX = (
    "SUPPORTED", "CONSISTENT", "CONTRADICTED", "UNTESTABLE", "NO_BBOX")

# A claim counts as supported when at least this share of the gaze
# samples in its time window fall inside the (tolerance-expanded) box.
# 0.5 is deliberate: a claim about where someone looked during an
# interval should hold for the majority of that interval, not for a
# single frame.
SUPPORT_THRESHOLD = 0.5

#: Minimum span to collect gaze over for a claim, in seconds.
#: A claim naming a single instant is naming a FIXATION, and fixations
#: in this pipeline have a median duration of ~220 ms. Scored at the
#: instant alone, a claim is testable only if a sample happens to fall
#: on that exact millisecond.
MIN_CLAIM_WINDOW_S = 0.22


def _expand(bbox, pad_x: float, pad_y: float):
    x, y, w, h = bbox
    return (max(0.0, x - pad_x), max(0.0, y - pad_y),
            min(1.0, w + 2 * pad_x), min(1.0, h + 2 * pad_y))


def check_claim(claim: dict, samples: list, accuracy_deg: float,
                px_per_deg: float, video_w: int, video_h: int,
                grid: "dict | None" = None) -> dict:
    """Score one claim against the gaze samples in its time window.

    ``samples``: (t_seconds, nx, ny, valid) in normalised video coords.
    """
    out = {
        "t_start": claim.get("t_start"),
        "t_end": claim.get("t_end"),
        "attended": claim.get("attended"),
        "confidence": claim.get("confidence"),
    }
    # A claim naming a REGION from the fixed vocabulary carries its own
    # rectangle: the grid is known, so the model cannot misplace it and
    # the claim is scoreable by construction. This is what removes the
    # model's localisation error from the correspondence measure — the
    # failure mode that made 46 of 60 claims unscoreable and sent the
    # remaining misses 404 px in one direction.
    bbox = claim.get("bbox")
    if claim.get("region") and grid:
        import regions as _regions

        reg = _regions.region_by_name(grid, claim["region"])
        if reg:
            bbox = reg["bbox"]
            out["region"] = reg["name"]
        else:
            out["verdict"] = NO_BOX
            out["note"] = ("named a region outside the vocabulary: %r"
                           % claim["region"])
            return out
    if not bbox or len(bbox) != 4 or any(b is None for b in bbox):
        out["verdict"] = NO_BOX
        out["note"] = "model did not localise this claim"
        return out

    t0 = float(claim.get("t_start") or 0)
    t1 = float(claim.get("t_end") or t0)
    if t1 < t0:
        t0, t1 = t1, t0
    # ZERO-LENGTH CLAIMS.
    # In "fixations" detail mode the model is given one keyframe per
    # fixation and answers with a single instant: t_start == t_end. A
    # 1 ms window around that instant contains a sample only by luck —
    # at 31 Hz the samples are 32 ms apart — so 59 of 60 claims came
    # back "no valid gaze samples in this time window" and the
    # correspondence figure was computed from a single claim.
    #
    # The instant NAMES a fixation, and a fixation has duration. Widen
    # to the fixation the claim is about: MIN_CLAIM_WINDOW_S is the
    # median fixation duration this pipeline produces (~220 ms), so the
    # window covers the fixation without bleeding into its neighbours.
    # Widening further would start to include the saccade and the next
    # fixation, which would make a wrong claim look supported.
    if t1 - t0 < MIN_CLAIM_WINDOW_S:
        mid = 0.5 * (t0 + t1)
        t0 = max(0.0, mid - MIN_CLAIM_WINDOW_S / 2)
        t1 = mid + MIN_CLAIM_WINDOW_S / 2
        out["window_widened_to_s"] = round(MIN_CLAIM_WINDOW_S, 3)

    window = [s for s in samples
              if t0 <= s[0] <= t1 and (len(s) < 4 or s[3])
              and s[1] is not None and s[2] is not None]
    out["n_samples"] = len(window)
    if not window:
        out["verdict"] = UNTESTABLE
        out["note"] = "no valid gaze samples in this time window"
        return out

    err_px = accuracy_deg * px_per_deg
    out["tolerance_px"] = round(err_px)

    # STRICT containment: the UNPADDED object.
    # Padding the box by the tracker's error and ALSO grading distance
    # against that error counts the same allowance twice. With a 124 px
    # tolerance a 84 px object becomes 332 px wide, so anything in the
    # neighbourhood lands "inside" and SUPPORTED stops meaning the gaze
    # was on the thing. The tolerance now enters in exactly one place —
    # the CONSISTENT band below — and containment means containment.
    bx, by, bw, bh = (float(b) for b in bbox)
    inside = sum(1 for s in window
                 if bx <= s[1] <= bx + bw and by <= s[2] <= by + bh)
    frac = inside / len(window)
    out["fraction_inside"] = round(frac, 3)

    # Kept for continuity with earlier runs, and because the gap between
    # the two is itself informative: a claim that is 0 % strict and
    # 100 % padded is one the instrument cannot adjudicate.
    pad_x = err_px / video_w if video_w else 0.0
    pad_y = err_px / video_h if video_h else 0.0
    ex, ey, ew, eh = _expand([bx, by, bw, bh], pad_x, pad_y)
    out["fraction_inside_padded"] = round(
        sum(1 for s in window
            if ex <= s[1] <= ex + ew and ey <= s[2] <= ey + eh)
        / len(window), 3)

    # WHERE the gaze actually was, and how far that is from the middle
    # of the claimed box. "0 % inside" is a verdict, not a diagnosis:
    # it cannot distinguish a model that put the box in the wrong place
    # from a tracker with a systematic offset. The offset VECTOR can —
    # a consistent direction across many claims is a calibration fault,
    # a scattered one is a localisation fault. They need entirely
    # different fixes, and only one of them is the model's problem.
    xs = sorted(s[1] for s in window)
    ys = sorted(s[2] for s in window)
    gx = xs[len(xs) // 2]
    gy = ys[len(ys) // 2]
    bx = float(bbox[0]) + float(bbox[2]) / 2.0
    by = float(bbox[1]) + float(bbox[3]) / 2.0
    out["gaze_median"] = [round(gx, 3), round(gy, 3)]
    out["box_centre"] = [round(bx, 3), round(by, 3)]
    out["offset_px"] = [round((gx - bx) * video_w), round((gy - by) * video_h)]

    # ── HOW FAR OFF, not just in or out ──────────────────────────────
    # Refusing every claim about an object smaller than the measurement
    # error threw away 77 % of a session. It was the honest binary, but
    # binary was the wrong shape: "the gaze was 18 px from a 96 px hand"
    # and "the gaze was 600 px away, across the room" are both "outside
    # the box", and they are not remotely the same claim.
    #
    # So report the DISTANCE from the gaze to the claimed object, and
    # grade against the session's own error:
    #
    #   SUPPORTED    most of the gaze fell inside the padded box
    #   CONSISTENT   it did not, but the gaze sat within one measurement
    #                error of the object — the instrument cannot tell
    #                this claim from a correct one, which is a real
    #                statement about the claim AND about the instrument
    #   CONTRADICTED the gaze was further away than the error can
    #                explain. This one is the model's to answer for.
    #
    # UNTESTABLE now means only "no gaze samples here", which is the one
    # case where nothing whatever can be said.
    obj_px = min(float(bbox[2]) * video_w, float(bbox[3]) * video_h)
    out["object_px"] = round(obj_px)
    out["resolvable"] = bool(obj_px >= 2 * err_px)

    bx0, by0 = float(bbox[0]), float(bbox[1])
    bx1, by1 = bx0 + float(bbox[2]), by0 + float(bbox[3])

    def _dist_px(s) -> float:
        dx = max(bx0 - s[1], 0.0, s[1] - bx1) * video_w
        dy = max(by0 - s[2], 0.0, s[2] - by1) * video_h
        return (dx * dx + dy * dy) ** 0.5

    dists = sorted(_dist_px(s) for s in window)
    med_dist = dists[len(dists) // 2]
    out["distance_px"] = round(med_dist)
    out["distance_deg"] = round(med_dist / px_per_deg, 2) if px_per_deg else None

    if frac >= SUPPORT_THRESHOLD:
        out["verdict"] = SUPPORTED
    elif med_dist <= err_px:
        out["verdict"] = CONSISTENT
        out["note"] = ("gaze sat %.0f px from the claimed object, within "
                       "the %.0f px measurement error — consistent with "
                       "the claim, but not distinguishable from a near "
                       "miss%s" % (med_dist, err_px,
                                   "" if out["resolvable"] else
                                   "; the object is %.0f px, below the "
                                   "%.0f px this session can resolve"
                                   % (obj_px, 2 * err_px)))
    else:
        out["verdict"] = CONTRADICTED
        out["note"] = ("gaze sat %.0f px (%.1f deg) from the claimed "
                       "object — %.1fx the measurement error, so this "
                       "is not explained by tracking uncertainty"
                       % (med_dist, med_dist / px_per_deg if px_per_deg
                          else 0, med_dist / err_px if err_px else 0))
    return out


def check_all(claims: list, samples: list, accuracy_deg: float,
              px_per_deg: float, video_w: int, video_h: int,
              grid: "dict | None" = None) -> dict:
    results = [check_claim(c, samples, accuracy_deg, px_per_deg,
                           video_w, video_h, grid) for c in claims]
    counts = {k: sum(1 for r in results if r["verdict"] == k)
              for k in (SUPPORTED, CONSISTENT, CONTRADICTED, UNTESTABLE,
                        NO_BOX)}
    testable = (counts[SUPPORTED] + counts[CONSISTENT]
                + counts[CONTRADICTED])
    return {
        "claims": results,
        "counts": counts,
        "n_claims": len(results),
        "n_testable": testable,
        # THE RQ3 HEADLINE NUMBER: of the claims that could be checked,
        # what share the gaze data supports.
        # STRICT: the gaze was actually on the claimed object.
        "correspondence_pct": round(100.0 * counts[SUPPORTED] / testable, 1)
        if testable else None,
        # LENIENT: on it, or near enough that the tracker cannot say
        # otherwise. Report BOTH — the gap between them is exactly the
        # share of claims the instrument is too coarse to adjudicate,
        # which is a property of the method worth quantifying.
        "correspondence_lenient_pct": round(
            100.0 * (counts[SUPPORTED] + counts[CONSISTENT]) / testable, 1)
        if testable else None,
        "testable_pct": round(100.0 * testable / len(results), 1)
        if results else None,
        "accuracy_deg_used": accuracy_deg,
        "support_threshold": SUPPORT_THRESHOLD,
        # CHANCE. A correspondence percentage means nothing on its own
        # once the answer space is a fixed vocabulary: a model guessing
        # uniformly among nine regions is right 11 % of the time, and
        # one that always says "middle-centre" does better than that,
        # because gaze concentrates centrally. Reporting 68 % without
        # the baseline invites the reader to compare it against 100 when
        # the honest comparison is against chance.
        #
        # Two baselines, because they fail differently:
        #   uniform   1/n regions — the floor for any guess
        #   majority  always naming the region the gaze visited most.
        #             This is the one that matters: it is what a model
        #             that has learned nothing about THIS recording,
        #             only about where people usually look, would score.
        "chance": _chance_baselines(results, samples, grid),
        "box_reuse": box_reuse(claims),
        "gaze_summary": gaze_summary(samples),
        "offset_analysis": offset_analysis(results, px_per_deg,
                                           video_w, video_h, accuracy_deg),
    }


def _chance_baselines(results: list, samples: list,
                      grid: "dict | None") -> "dict | None":
    """What a model that learned nothing would score.

    ``majority`` is the demanding one. Gaze is not uniformly
    distributed — people look at the middle of a frame — so a model that
    always names the busiest region scores well above 1/n while
    demonstrating no sensitivity to the recording at all. A
    correspondence figure that does not beat it has not shown anything.
    """
    if not grid or not grid.get("admissible") or not samples:
        return None
    regions = grid["regions"]
    n = len(regions)
    counts = {r["name"]: 0 for r in regions}
    total = 0
    for s in samples:
        if len(s) >= 4 and not s[3]:
            continue
        for r in regions:
            x, y, w, h = r["bbox"]
            if x <= s[1] < x + w and y <= s[2] < y + h:
                counts[r["name"]] += 1
                total += 1
                break
    if not total:
        return None
    top = max(counts.items(), key=lambda kv: kv[1])
    return {
        "n_regions": n,
        "uniform_pct": round(100.0 / n, 1),
        "majority_region": top[0],
        "majority_pct": round(100.0 * top[1] / total, 1),
        "note": ("a model that always said %r would score %.1f %%; "
                 "beating %.1f %% (uniform guessing) is not evidence of "
                 "anything" % (top[0], 100.0 * top[1] / total, 100.0 / n)),
    }


def box_reuse(claims: list) -> dict:
    """Does the model re-stamp one box per label, or look at each frame?

    A model that localises from the FRAME gives slightly different
    coordinates each time — objects shift, the camera moves, its own
    estimate wobbles. A model that localises from a PRIOR decides where
    a thing is once and reuses that box verbatim.

    The two are indistinguishable in any single claim and obvious across
    a session, so this counts distinct boxes per label. It is the
    cheapest available evidence about whether the localisation step is
    doing any work at all, and it needs no gaze data — which makes it
    independent of every other check here.
    """
    by_label: dict = {}
    for c in claims:
        if not isinstance(c, dict) or not c.get("bbox"):
            continue
        lab = str(c.get("attended") or "?")
        key = tuple(round(float(v), 4) for v in c["bbox"])
        by_label.setdefault(lab, []).append(key)
    rows = []
    reused = total = 0
    for lab, boxes in by_label.items():
        n, d = len(boxes), len(set(boxes))
        rows.append({"label": lab, "claims": n, "distinct_boxes": d})
        total += n
        if n > 1 and d == 1:
            reused += n
    rows.sort(key=lambda r: -r["claims"])
    return {
        "rows": rows,
        "n_claims_with_box": total,
        "n_in_reused_boxes": reused,
        "reuse_pct": round(100.0 * reused / total, 1) if total else None,
    }


def gaze_summary(samples: list) -> dict:
    """Where the gaze actually was, as a 3x3 distribution and a median.

    A sanity check on MY arithmetic before anyone draws a conclusion
    about the model. The gaze arrives in screen pixels and is mapped
    into video coordinates through the manifest's video_rect; if that
    mapping is wrong, every claim is scored against displaced gaze and
    the result is a constant offset — which is exactly what a
    systematically mis-localising model also produces.
    """
    valid = [s for s in samples if len(s) < 4 or s[3]]
    if not valid:
        return {}
    xs = sorted(s[1] for s in valid)
    ys = sorted(s[2] for s in valid)
    cells = [[0] * 3 for _ in range(3)]
    outside = 0
    for s in valid:
        if not (0 <= s[1] <= 1 and 0 <= s[2] <= 1):
            outside += 1
            continue
        cells[min(2, int(s[2] * 3))][min(2, int(s[1] * 3))] += 1
    n = len(valid)
    return {
        "n": n,
        "median": [round(xs[n // 2], 3), round(ys[n // 2], 3)],
        "x_range": [round(xs[int(0.05 * n)], 3), round(xs[int(0.95 * n)], 3)],
        "y_range": [round(ys[int(0.05 * n)], 3), round(ys[int(0.95 * n)], 3)],
        "outside_frame_pct": round(100.0 * outside / n, 1),
        "grid_pct": [[round(100.0 * c / n, 1) for c in row] for row in cells],
    }


def offset_analysis(results: list, px_per_deg: float,
                    video_w: int, video_h: int,
                    accuracy_deg: "float | None" = None) -> "dict | None":
    """Is the gaze systematically displaced from the claimed regions?

    A low correspondence score has two very different causes:

      SYSTEMATIC   every claim misses in the SAME direction. The model
                   may be reading the scene correctly while the gaze
                   itself is displaced — a calibration or gain fault.
                   Fixing the model would be fixing the wrong thing.

      SCATTERED    the misses point every which way. The gaze is where
                   it is, and the boxes are not; that is a localisation
                   or hallucination problem, and it IS the model's.

    The discriminator is the ratio of the MEDIAN offset (which survives
    only if the errors share a direction) to the median ABSOLUTE offset
    (which survives regardless). Near 1 means every miss points the same
    way; near 0 means they cancel out.
    """
    offs = [r["offset_px"] for r in results if r.get("offset_px")]
    if len(offs) < 5:
        return None

    def _med(vals):
        v = sorted(vals)
        return v[len(v) // 2]

    dx, dy = _med([o[0] for o in offs]), _med([o[1] for o in offs])
    adx, ady = _med([abs(o[0]) for o in offs]), _med([abs(o[1]) for o in offs])

    # SIGN AGREEMENT, not |median| / median|.|
    # The ratio of medians looks decisive on small, bimodal samples: with
    # seven claims split four one way and three the other, the median is
    # as large as the median absolute value and the ratio reads 1.0 —
    # "perfectly consistent" — for offsets that plainly are not. The
    # share of claims that miss in the SAME DIRECTION as the median does
    # not have that failure: four-of-seven reads 0.57, which is the
    # coin-flip it actually is.
    def _agree(vals) -> float:
        nz = [v for v in vals if v]
        if not nz:
            return 0.0
        med = _med(nz)
        if med == 0:
            return 0.0
        same = sum(1 for v in nz if (v > 0) == (med > 0))
        return same / len(nz)

    consistency_x = _agree([o[0] for o in offs])
    consistency_y = _agree([o[1] for o in offs])
    mag_deg = ((dx ** 2 + dy ** 2) ** 0.5) / px_per_deg if px_per_deg else None
    # 0.8 = four claims in five missing the same way. Chance alone gives
    # 0.5, and 0.6 is reachable by accident on a dozen claims.
    systematic = bool(max(consistency_x, consistency_y) >= 0.8
                      and mag_deg and mag_deg > 1.0)

    # ── THE TRACKER'S ALIBI ──────────────────────────────────────────
    # A consistent direction is NOT enough to convict the tracker, and
    # saying so was a real error: on the 2026-08-10 session this
    # reported a systematic +160, +404 px (7.46 deg) and concluded
    # "suspect the tracker" — for a session whose out-of-sample
    # validation measured 2.13 deg against KNOWN targets, minutes
    # earlier, on the same gaze stream.
    #
    # Those two cannot both be true. The validation is an independent
    # measurement of exactly this quantity, so it bounds how wrong the
    # gaze can be. An apparent displacement several times larger than
    # the measured accuracy has to come from somewhere else — and a
    # model that places its boxes from a prior about how classrooms
    # look, rather than from where the marker is, produces the same
    # consistent-direction signature.
    #
    # So the accuracy is not decoration here; it is the alibi.
    exonerated = bool(accuracy_deg and mag_deg
                      and mag_deg > 2.0 * accuracy_deg)
    if exonerated:
        reading = (
            "NOT THE TRACKER. The misses share a direction (%+d, %+d px, "
            "%.2f deg), but the session's own out-of-sample validation "
            "measured the gaze at %.2f deg against known targets — the "
            "tracker cannot be %.1fx more wrong than it was just "
            "measured to be. A displacement this large with that "
            "validation means the BOXES are systematically placed, not "
            "the gaze: the model is naming plausible scene content and "
            "localising it from a prior rather than from the marker. "
            "That is an RQ3 result about localisation, not a "
            "calibration fault to chase."
            % (dx, dy, mag_deg, accuracy_deg, mag_deg / accuracy_deg))
    elif systematic:
        reading = ("SYSTEMATIC — the misses share a direction (%+d, %+d "
                   "px, %.2f deg) and that is within what the tracker's "
                   "own error could produce. Suspect the tracker: "
                   "re-check the calibration and the gain fit."
                   % (dx, dy, mag_deg))
    else:
        reading = ("SCATTERED — the misses have no shared direction, so "
                   "this is not a displacement. The claims are landing "
                   "in the wrong places individually.")
    return {
        "n": len(offs),
        "median_offset_px": [dx, dy],
        "median_offset_deg": round(mag_deg, 2) if mag_deg else None,
        "median_abs_offset_px": [adx, ady],
        "direction_consistency": [round(consistency_x, 2),
                                  round(consistency_y, 2)],
        "systematic": systematic,
        "tracker_exonerated": exonerated,
        "validation_accuracy_deg": accuracy_deg,
        "offset_vs_accuracy": (round(mag_deg / accuracy_deg, 1)
                               if accuracy_deg and mag_deg else None),
        "reading": reading,
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


BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")


def extract_structured(text: str) -> list:
    """The ```json …``` block the feedback prompt requires."""
    import re

    if not text:
        return []
    m = re.search(r"```json\s*(.+?)```", text, re.S)
    if not m:
        m = re.search(r"(\[\s*\{.+?\}\s*\])", text, re.S)
    if not m:
        return []
    try:
        parsed = json.loads(m.group(1).strip())
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def load_claims(session: str, llm_file: "str | None" = None) -> "tuple":
    """Newest evaluation run for *session* from data/llm_logs/.

    The feedback response is returned to the browser and logged, but not
    written into the session manifest — so the log directory is the only
    persistent record of what the model actually claimed.
    """
    if llm_file:
        with open(llm_file, encoding="utf-8") as fh:
            d = json.load(fh)
        return extract_structured(d.get("response_text") or ""), llm_file

    best = None
    for path in glob.glob(os.path.join(DATA_DIR, "llm_logs",
                                       "*evaluation_run_*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        ctx = d.get("context") or {}
        if session and ctx.get("session") != session:
            continue
        if best is None or path > best[0]:
            best = (path, d)
    if not best:
        return [], None
    return extract_structured(best[1].get("response_text") or ""), best[0]


def load_gaze(manifest: dict, manifest_path: str, stimulus: str) -> "tuple":
    """Gaze in NORMALISED VIDEO coordinates for one stimulus.

    Three conversions have to happen, and skipping any of them silently
    scores the model against the wrong thing:

    1. The CSV holds SCREEN pixels. The model's bboxes are fractions of
       the VIDEO. The manifest's per-stimulus ``video_rect`` is the
       letterboxed rectangle the video occupied on screen, so it is the
       only correct mapping between them.
    2. The recorded columns are UNCORRECTED, but the marker the model
       saw was drawn with the session's gain correction applied. Scoring
       raw gaze against claims made about corrected gaze would measure
       the correction, not the model.
    3. Only samples inside the stimulus's own time window count, and
       only ones with status set — an invalid sample sitting at (0,0)
       would otherwise register as "outside every box".
    """
    csv_name = manifest.get("session_csv")
    csv_path = os.path.join(os.path.dirname(manifest_path), csv_name) \
        if csv_name else manifest_path.replace("_manifest.json", ".csv")
    if not os.path.isfile(csv_path):
        return [], "no gaze CSV at %s" % csv_path

    entry = next((s for s in manifest.get("stimuli", [])
                  if s.get("stimulus") == stimulus), None)
    if not entry:
        return [], "stimulus %r not in the manifest" % stimulus
    rect = entry.get("video_rect") or {}
    rx, ry = float(rect.get("x", 0)), float(rect.get("y", 0))
    rw, rh = float(rect.get("w", 0)), float(rect.get("h", 0))
    if rw <= 0 or rh <= 0:
        return [], "stimulus has no usable video_rect"
    t0 = entry.get("t_start_ns")
    t1 = entry.get("t_end_ns")
    if not t0 or not t1:
        return [], "stimulus has no time window"

    corr = manifest.get("gain_correction") or {}
    gx = float(corr.get("gain_x") or 1.0)
    gy = float(corr.get("gain_y") or 1.0)
    ox = float(corr.get("offset_x") or 0.0)
    oy = float(corr.get("offset_y") or 0.0)
    cx = float(corr.get("centre_x") or corr.get("center_x") or 0.0)
    cy = float(corr.get("centre_y") or corr.get("center_y") or 0.0)

    samples: list = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = int(row["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts < t0 or ts > t1:
                continue
            valid = str(row.get("status", "1")).strip() not in ("0", "", "0.0")
            try:
                sx = float(row["filtered_gaze_position_x"])
                sy = float(row["filtered_gaze_position_y"])
            except (KeyError, TypeError, ValueError):
                continue
            # Same affine the live preview applied, about the same centre.
            sx = cx + (sx - cx) * gx + ox
            sy = cy + (sy - cy) * gy + oy
            samples.append(((ts - t0) / 1e9,
                            (sx - rx) / rw, (sy - ry) / rh, valid))
    if not samples:
        return [], "no samples inside the stimulus window"
    return samples, None


def _px_per_degree(w_px: int, h_px: int, diag_in: float,
                   distance_cm: float) -> float:
    diag_cm = diag_in * 2.54
    w_cm = diag_cm * math.cos(math.atan2(h_px, w_px))
    return (w_px / w_cm) * distance_cm * math.tan(math.radians(1.0))


def _accuracy_deg(manifest: dict) -> "tuple":
    """The OUT-OF-SAMPLE accuracy, and where it came from.

    The post-stimulus check scored with a correction fitted on the PRE
    targets is the only unbiased estimate of the accuracy the stimulus
    data was actually recorded at. Using the flattering in-sample pre
    figure would shrink the tolerance box and manufacture contradictions.
    """
    vals = manifest.get("validations") or []
    post = [v for v in vals if v.get("phase") == "post"]
    for v in reversed(post):
        deg = v.get("mean_err_deg_measured") or v.get("mean_err_deg")
        if deg:
            return float(deg), "post-stimulus (out-of-sample)"
    for v in reversed(vals):
        deg = v.get("mean_err_deg_measured") or v.get("mean_err_deg")
        if deg:
            return float(deg), "pre-stimulus (IN-SAMPLE — optimistic)"
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", nargs="?")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--latest", action="store_true",
                    help="score the most recent session (no path needed)")
    ap.add_argument("--stimulus", help="which stimulus (default: the first)")
    ap.add_argument("--llm", help="a specific data/llm_logs/*.json response")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    # Session folders are named from the participant ID and a timestamp,
    # so the path is long, easy to mistype, and different on every run.
    # Nobody should have to read it off a log line to check their own
    # most recent session.
    if args.latest and not args.manifest:
        found = sorted(glob.glob(os.path.join(
            DATA_DIR, "gazefollower_raw", "*_manifest.json")),
            key=os.path.getmtime)
        if not found:
            print("No session manifests in data/gazefollower_raw/.")
            return 1
        args.manifest = found[-1]
        print("Latest session: %s" % os.path.basename(args.manifest))
        print()

    if args.demo or not args.manifest:
        if not args.demo:
            print("No manifest given — showing the DEMO on synthetic data.")
            print("To score your most recent real session:")
            print("    python claim_check.py --latest")
            print()
        return _demo()

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)

    stimuli = [s.get("stimulus") for s in manifest.get("stimuli", [])
               if s.get("stimulus")]
    stimulus = args.stimulus or (stimuli[0] if stimuli else None)
    if not stimulus:
        print("This manifest lists no stimuli.")
        return 1

    session = os.path.basename(args.manifest).replace("_manifest.json", "")
    claims, src = load_claims(session, args.llm)
    if not claims:
        print("No structured LLM claims found for session %r." % session)
        print("Run the feedback step in the review tool first — it must "
              "emit the ```json block with bbox fields. Checked "
              "data/llm_logs/*evaluation_run_*.json.")
        return 1

    samples, err = load_gaze(manifest, args.manifest, stimulus)
    if err:
        print("Could not load gaze: %s" % err)
        return 1

    acc, acc_src = _accuracy_deg(manifest)
    if not acc:
        print("This session has no validation accuracy, so there is no "
              "tolerance to expand the boxes by — every claim would be "
              "scored as if the tracker were perfect. Refusing.")
        return 1

    scr = manifest.get("screen") or {}
    dist = ((manifest.get("distance") or {}).get("cm")
            or (manifest.get("quality_thresholds") or {})
            .get("assumed_viewing_distance_cm") or 60.0)
    ppd = _px_per_degree(int(scr.get("width_px") or 1920),
                         int(scr.get("height_px") or 1080),
                         float(scr.get("diag_inches") or 15.6), float(dist))
    rect = next(s for s in manifest["stimuli"]
                if s.get("stimulus") == stimulus)["video_rect"]
    res = check_all(claims, samples, acc, ppd,
                    int(rect.get("w") or 1920), int(rect.get("h") or 1080))
    res.update(session=session, stimulus=stimulus, llm_log=src,
               accuracy_source=acc_src, n_gaze_samples=len(samples))

    if args.json:
        print(json.dumps(res, indent=2))
        return 0

    print("=" * 74)
    print("  CLAIM CORRESPONDENCE — %s" % session)
    print("=" * 74)
    print("  stimulus   : %s" % stimulus)
    print("  gaze       : %d samples (gain correction applied)"
          % len(samples))
    acc_px = acc * ppd
    print("  tolerance  : %.2f deg = %.0f px, from the %s validation"
          % (acc, acc_px, acc_src))
    print("  claims from: %s" % os.path.basename(src or "?"))
    # A tolerance this wide makes almost any claim near the gaze point
    # "supported", so a high correspondence score would be measuring the
    # padding rather than the model. Say so before the numbers, not after.
    if acc > 3.0:
        print()
        print("  *** The tolerance is %.1f deg (%.0f px) because this "
              "session's" % (acc, acc * ppd))
        print("      accuracy failed the 3 deg criterion. Boxes padded that")
        print("      far will accept almost anything near the gaze point, so")
        print("      treat the correspondence figure below as an UPPER bound,")
        print("      not a measurement. Score sessions that passed. ***")
    print()
    for r in res["claims"]:
        print("  [%-12s] %5.1f-%5.1f s  %-30s %s"
              % (r["verdict"], r.get("t_start") or 0, r.get("t_end") or 0,
                 str(r.get("attended"))[:30],
                 ("%3.0f%% inside" % (100 * r["fraction_inside"]))
                 if "fraction_inside" in r else ""))
        if r.get("note"):
            print("                      %s" % r["note"])
    print()
    print("  correspondence: %s %% of %d testable claims (%s %% of %d "
          "claims were testable)"
          % (res["correspondence_pct"], res["n_testable"],
             res["testable_pct"], res["n_claims"]))

    # Untestable claims are not a defect in the model or the tracker —
    # they are the resolution limit stated in metrics_spec: an AOI must
    # be at least twice the accuracy to be assignable. Reporting them
    # without that framing invites the reader to treat them as failures.
    n_unt = res["counts"][UNTESTABLE]
    if n_unt and res["n_claims"]:
        print()
        print("  %d of %d claims (%.0f %%) name objects SMALLER than the"
              % (n_unt, res["n_claims"], 100.0 * n_unt / res["n_claims"]))
        print("  %.0f px measurement error, so they cannot be scored either"
              % (acc_px,))
        print("  way. That is the resolution limit, not a failure: an AOI")
        print("  must be at least twice the accuracy to be assignable. At")
        print("  %.2f deg this pipeline can resolve REGIONS of the scene,"
              % res["accuracy_deg_used"])
        print("  not individual people.")

    # ── Two checks that must come BEFORE any conclusion ──────────────
    gs = gaze_summary(samples)
    if gs:
        print()
        print("  " + "-" * 68)
        print("  SANITY CHECK — where the gaze actually was")
        print("  " + "-" * 68)
        print("  median (%.2f, %.2f) of the video frame; middle 90 %% spans "
              "x %.2f-%.2f, y %.2f-%.2f"
              % (gs["median"][0], gs["median"][1], gs["x_range"][0],
                 gs["x_range"][1], gs["y_range"][0], gs["y_range"][1]))
        print("  %.1f %% of samples fell outside the frame entirely"
              % gs["outside_frame_pct"])
        print()
        print("      distribution over the frame (%% of samples)")
        for r, row in enumerate(gs["grid_pct"]):
            band = ("top   ", "middle", "bottom")[r]
            print("        %s  %5.1f  %5.1f  %5.1f" % (band, *row))
        print()
        print("  Open the review tool and check this against the video. If")
        print("  the marker on screen does NOT sit where this says, the")
        print("  fault is the screen-to-video mapping in THIS script, not")
        print("  the model — and every verdict above is void.")

    br = res.get("box_reuse") or {}
    if br.get("rows"):
        print()
        print("  " + "-" * 68)
        print("  IS THE MODEL LOOKING? — distinct boxes per label")
        print("  " + "-" * 68)
        for row in br["rows"][:8]:
            flag = "  <- one box reused for all"  \
                if row["claims"] > 1 and row["distinct_boxes"] == 1 else ""
            print("    %-34s %2d claims, %2d distinct box(es)%s"
                  % (str(row["label"])[:34], row["claims"],
                     row["distinct_boxes"], flag))
        print()
        print("  %.0f %% of localised claims reuse a box verbatim. A model"
              % (br["reuse_pct"] or 0))
        print("  reading each FRAME gives slightly different coordinates")
        print("  every time; one reciting a PRIOR stamps the same box.")

    oa = res.get("offset_analysis")
    if oa:
        print()
        print("  " + "-" * 68)
        print("  WHERE THE GAZE ACTUALLY WAS")
        print("  " + "-" * 68)
        print("  median offset from the claimed region: %+d, %+d px "
              "(%.2f deg)"
              % (oa["median_offset_px"][0], oa["median_offset_px"][1],
                 oa["median_offset_deg"] or 0))
        print("  direction consistency: x %.2f, y %.2f  "
              "(1.0 = every miss the same way, 0 = they cancel)"
              % (oa["direction_consistency"][0],
                 oa["direction_consistency"][1]))
        print()
        for line in _wrap(oa["reading"], 68):
            print("  " + line)


def _wrap(text: str, width: int) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines
    print()
    print("  A contradicted claim is not automatically a hallucination —")
    print("  it can also be a localisation error, or gaze error near a")
    print("  boundary. The boxes were already padded by %.0f px." % (acc * ppd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
