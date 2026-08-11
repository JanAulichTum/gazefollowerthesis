# -*- coding: utf-8 -*-
"""
Locate the objects FIRST, then bring the gaze to them.

THE PROBLEM WITH ASKING ONCE
----------------------------
The feedback prompt shows the model a frame with the gaze marker burned
on and asks two things at once: what was the participant looking at, and
where is that thing. Those interfere. On 2026-08-10 the model named
plausible classroom content and then placed its boxes from a prior about
how classrooms are arranged — posters high on the back wall, students in
the middle — rather than from where the marker was. The misses shared a
direction of +160, +404 px, four times the tracker's validated accuracy,
so the tracker could not have caused them.

Worse, the marker is a hint. A model that sees a red dot on a student
will say "a student" whether or not it can localise anything, so a
correspondence score computed that way is partly measuring the model's
ability to read a dot back to us.

THE INVERSE
-----------
Split the two jobs and reverse their order:

  1. LOCATE   Show the model CLEAN frames — no marker, no gaze, no
              mention of eye tracking — and ask only "what is in this
              frame and where". Pure scene description. Nothing about
              this step can be contaminated by the gaze, because the
              gaze is not in it.

  2. ASSIGN   Take the recorded fixations and find which inventory
              object each one lands on. This is arithmetic, not
              judgment.

  3. COMPARE  Step 2 says what the participant looked at, derived from
              an independent detection plus a measurement. The feedback
              run said what it thought they looked at. Agreement between
              those is a genuine correspondence test.

This also produces something the study otherwise lacks: an AOI set that
was not hand-drawn and not chosen by anyone with a stake in the result.
The objects come from a model that never saw the gaze.

WHAT IT CANNOT DO
-----------------
It does not make small objects resolvable. If two students stand 80 px
apart and the accuracy is 124 px, assignment between them is a coin
flip, and the tool says so per fixation (``ambiguous``) rather than
picking one. The honest output is a distribution over candidate objects,
not a label.

Usage::

    python inverse_check.py --latest
    python inverse_check.py <manifest.json> --stimulus <name>
    python inverse_check.py --latest --dry-run    # no API calls
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE, "data", "gazefollower_raw")
STUDY_DIR = os.path.join(BASE, "data", "study")


def _session_glob(pattern: str = "*_manifest.json") -> list:
    """Sessions from BOTH directories.

    Evaluation sessions are written to data/study/ and development ones
    to data/gazefollower_raw/. A tool that globs only one of them goes
    quietly blind to half the study the day collection starts, which is
    the worst possible moment for a silent failure.
    """
    import glob as _g

    out = []
    for d in (STUDY_DIR, RAW_DIR):
        out.extend(_g.glob(os.path.join(d, pattern)))
    return sorted(out, key=lambda p: os.path.basename(p))
OUT_DIR = os.path.join(BASE, "data", "inverse_check")

#: A fixation is assigned to an object only if the runner-up is at least
#: this much further away, in units of the session's measurement error.
#: Below it the two candidates are not distinguishable and the fixation
#: is reported as ambiguous — which is information, not a failure.
SEPARATION_MARGIN = 1.0

LOCATE_PROMPT = (
    "You are annotating video frames for a computer-vision dataset. "
    "For each frame, list the distinct PEOPLE and OBJECTS that are "
    "clearly visible, with their bounding boxes.\n\n"
    "Return ONLY a fenced ```json block: a JSON array with one entry "
    "per frame, in the order the frames were given:\n"
    "[{\"t\": <seconds>, \"objects\": [{\"label\": \"<short noun "
    "phrase>\", \"bbox\": [x, y, w, h]}]}]\n\n"
    "Coordinates are NORMALISED to the frame (0-1, origin top-left). "
    "Give every person separately, described so they can be told apart "
    "(clothing colour, position, posture). Include salient non-person "
    "objects: posters, boards, screens, windows, doors. Do NOT include "
    "walls, floors, ceilings or empty space — those are background, "
    "not objects.\n\n"
    "Be accurate about POSITION. A box that names the right thing in "
    "the wrong place is worse than no box, because it will be compared "
    "against measured coordinates."
)


def _dist_px(bbox, x: float, y: float, w_px: int, h_px: int) -> float:
    """Distance in pixels from a normalised point to a normalised box."""
    bx, by, bw, bh = (float(v) for v in bbox)
    dx = max(bx - x, 0.0, x - (bx + bw)) * w_px
    dy = max(by - y, 0.0, y - (by + bh)) * h_px
    return (dx * dx + dy * dy) ** 0.5


def assign_fixation(fix: dict, objects: list, err_px: float,
                    w_px: int, h_px: int) -> dict:
    """Which inventory object was this fixation on?

    Returns the nearest object, the runner-up, and whether the two can
    be told apart at this session's accuracy. Reporting the runner-up is
    the point: "a student, but the one beside them is equally close" is
    a different claim from "a student", and only the first is honest at
    124 px of error.
    """
    if not objects:
        return {"assigned": None, "reason": "no objects detected"}
    ranked = sorted(
        ({"label": o.get("label"), "bbox": o.get("bbox"),
          "distance_px": round(_dist_px(o.get("bbox") or [0, 0, 0, 0],
                                        fix["x"], fix["y"], w_px, h_px))}
         for o in objects if o.get("bbox")),
        key=lambda o: o["distance_px"])
    if not ranked:
        return {"assigned": None, "reason": "no usable boxes"}

    best = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    gap = (runner["distance_px"] - best["distance_px"]) if runner else 1e9
    ambiguous = bool(gap < SEPARATION_MARGIN * err_px)
    return {
        "assigned": best["label"],
        "distance_px": best["distance_px"],
        "on_object": bool(best["distance_px"] == 0),
        "within_error": bool(best["distance_px"] <= err_px),
        "runner_up": runner["label"] if runner else None,
        "runner_up_distance_px": runner["distance_px"] if runner else None,
        "separation_px": round(gap) if runner else None,
        "ambiguous": ambiguous,
        "note": (("%r and %r are only %.0f px apart at %.0f px accuracy — "
                  "not distinguishable" % (best["label"],
                                           runner["label"], gap, err_px))
                 if ambiguous and runner else ""),
    }


def compare(assignments: list, claims: list) -> dict:
    """Do the feedback model's claims match the gaze-derived assignment?

    Matching is deliberately LEXICAL and loose — a shared content word
    counts. The alternative is to have a model judge whether two phrases
    mean the same thing, which puts a model back in the position of
    marking its own work. A loose rule that is stated is better than a
    clever one that is not auditable.
    """
    def _words(s):
        stop = {"the", "a", "an", "of", "in", "on", "at", "with", "and",
                "student", "person", "man", "woman", "male", "female"}
        return {w for w in str(s or "").lower().replace(",", " ").split()
                if len(w) > 2 and w not in stop}

    agreed = disagreed = unmatched = 0
    rows = []
    for a in assignments:
        if a.get("ambiguous") or not a.get("assigned"):
            continue
        near = [c for c in claims
                if abs(float(c.get("t_start") or 0) - a["t"]) <= 0.5]
        if not near:
            unmatched += 1
            continue
        claim = near[0]
        overlap = _words(claim.get("attended")) & _words(a["assigned"])
        ok = bool(overlap)
        rows.append({"t": a["t"], "gaze_says": a["assigned"],
                     "model_says": claim.get("attended"),
                     "agree": ok, "shared": sorted(overlap)})
        if ok:
            agreed += 1
        else:
            disagreed += 1
    total = agreed + disagreed
    return {
        "n_compared": total,
        "agreement_pct": round(100.0 * agreed / total, 1) if total else None,
        "unmatched_in_time": unmatched,
        "rows": rows,
    }


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", nargs="?")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--stimulus")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan and the prompt; make no API calls")
    args = ap.parse_args()

    path = args.manifest
    if not path and (args.latest or True):
        found = sorted(_session_glob(), key=os.path.getmtime)
        if not found:
            print("No session manifests in data/gazefollower_raw/.")
            return 1
        path = found[-1]
    manifest = _load(path)
    session = os.path.basename(path).replace("_manifest.json", "")
    stimuli = [s.get("stimulus") for s in manifest.get("stimuli", [])
               if s.get("stimulus")]
    stimulus = args.stimulus or (stimuli[0] if stimuli else None)

    import claim_check

    acc, acc_src = claim_check._accuracy_deg(manifest)
    samples, err = claim_check.load_gaze(manifest, path, stimulus)
    scr = manifest.get("screen") or {}
    dist = (manifest.get("distance") or {}).get("cm") or 60.0
    ppd = claim_check._px_per_degree(
        int(scr.get("width_px") or 1920), int(scr.get("height_px") or 1080),
        float(scr.get("diag_inches") or 15.6), float(dist))
    rect = next((s.get("video_rect") for s in manifest.get("stimuli", [])
                 if s.get("stimulus") == stimulus), {}) or {}
    vw, vh = int(rect.get("w") or 1920), int(rect.get("h") or 1080)

    print("=" * 74)
    print("  INVERSE CHECK — %s" % session)
    print("=" * 74)
    print("  stimulus : %s" % stimulus)
    print("  gaze     : %s" % (err or "%d samples" % len(samples)))
    print("  accuracy : %s deg (%s)" % (acc, acc_src))
    if acc and ppd:
        print("  tolerance: %.0f px" % (acc * ppd))
    print("  video    : %dx%d px" % (vw, vh))
    print()

    if args.dry_run or True:
        print("  STEP 1 — LOCATE (clean frames, no marker, no gaze)")
        print("  " + "-" * 70)
        for line in LOCATE_PROMPT.split("\n"):
            print("  | " + line)
        print()
        print("  STEP 2 — ASSIGN each fixation to the nearest object")
        print("  STEP 3 — COMPARE against the feedback run's claims")
        print()
        print("  Wire step 1 to the same Gemini call app.py already makes")
        print("  for the clean-frame scene description (it samples frames")
        print("  with draw_marker=False), then run assign_fixation() over")
        print("  the fixations and compare() against the stored claims.")
        print()
        print("  The unit tests exercise steps 2 and 3 directly, so the")
        print("  scoring logic is verified without spending API calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
