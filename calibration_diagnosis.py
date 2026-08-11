# -*- coding: utf-8 -*-
"""
Why doesn't the calibration already fix this?

THE QUESTION
------------
GazeFollower runs an SVR calibration on a grid of points before every
session. Its whole job is to map the CNN's gaze features onto screen
coordinates, absorbing exactly the gain and offset that a post-hoc
correction removes. So a session that still needs a x1.37 vertical gain
afterwards is telling you something, and "apply a correction" is not an
answer to it — it is a way of not asking.

The question matters beyond tidiness. If the correction is repairing a
KNOWN, characterisable limitation of the calibration, it is a declared
pipeline step and defensible. If it is soaking up an unexamined
coordinate bug, it is a fudge factor that happens to reduce a number,
and an examiner who asks "what is your correction correcting?" will
get an answer that does not survive follow-up.

WHAT THIS DISTINGUISHES
-----------------------
Every validation records each target's position AND where the gaze
actually landed, which is enough to tell four causes apart:

  RANGE COMPRESSION   gaze consistently falls SHORT of eccentric
                      targets, proportionally: the further out, the
                      bigger the miss, and always toward the centre.
                      This is the appearance-based-CNN signature —
                      regression toward the training mean — and it is
                      also what happens when validation targets sit
                      outside the span of the calibration grid, so the
                      model is extrapolating. A gain correction is the
                      RIGHT repair, and the better repair is a
                      calibration grid that reaches as far as the
                      stimuli do.

  UNIFORM OFFSET      every target missed by the same vector,
                      regardless of position. That is a coordinate-space
                      or head-position shift between calibration and
                      validation, NOT something a gain should be fixing:
                      a gain applied to an offset distorts the centre to
                      repair the edges.

  ONE-SIDED           error concentrated at one edge (usually the top).
                      Up-gaze is genuinely harder — the eyelid occludes
                      the iris — so this is a real limit of the method,
                      and a symmetric correction will over-correct the
                      good side to help the bad one.

  UNSTRUCTURED        error unrelated to position. Then it is noise, not
                      distortion, and NO correction of any shape will
                      help. Fitting one anyway will overfit the seven
                      targets and generalise worse.

Usage::

    python calibration_diagnosis.py --latest
    python calibration_diagnosis.py <manifest.json>
    python calibration_diagnosis.py --all      # across sessions
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

#: A slope this far below 1 means the gaze spans less of the screen than
#: the targets do — the definition of range compression. 0.9 allows for
#: sampling noise on seven points.
COMPRESSION_SLOPE = 0.90
#: Share of the total error that a constant vector must explain before
#: the miss counts as an offset rather than a distortion.
OFFSET_DOMINANCE = 0.60


def _fit_slope(xs: list, ys: list) -> "tuple":
    """Least-squares slope and intercept of y on x, or (None, None)."""
    n = len(xs)
    if n < 3:
        return None, None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None, None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return slope, my - slope * mx


def analyse_axis(targets: list, axis: int) -> dict:
    """One axis: is the gaze compressed, offset, one-sided, or noisy?"""
    tk, mk = ("tx", "mx") if axis == 0 else ("ty", "my")
    pts = [(float(t[tk]), float(t[mk])) for t in targets
           if t.get(tk) is not None and t.get(mk) is not None]
    if len(pts) < 4:
        return {"ok": False, "reason": "only %d usable targets" % len(pts)}

    ts = [p[0] for p in pts]
    ms = [p[1] for p in pts]
    errs = [m - t for t, m in pts]

    # SLOPE of measured-on-target. 1.0 = the gaze spans the screen as
    # the targets do. Below 1 = compressed toward the centre; the
    # reciprocal is the gain that would undo it.
    slope, intercept = _fit_slope(ts, ms)
    mean_err = sum(errs) / len(errs)
    # How much of the miss a single constant vector explains. If the
    # residual after removing the mean is small, it IS an offset.
    resid = [e - mean_err for e in errs]
    total = sum(abs(e) for e in errs) or 1e-9
    offset_share = 1.0 - (sum(abs(r) for r in resid) / total)

    # ONE-SIDEDNESS: compare the error at targets below the centre with
    # those above it. A symmetric correction cannot fix an asymmetric
    # error without making the good side worse.
    centre = (max(ts) + min(ts)) / 2.0
    lo = [abs(e) for t, e in zip(ts, errs) if t < centre]
    hi = [abs(e) for t, e in zip(ts, errs) if t > centre]
    lo_m = sum(lo) / len(lo) if lo else 0.0
    hi_m = sum(hi) / len(hi) if hi else 0.0
    worse = "low" if lo_m > hi_m else "high"
    ratio = (max(lo_m, hi_m) / min(lo_m, hi_m)) if min(lo_m, hi_m) > 1 else None

    if slope is not None and slope < COMPRESSION_SLOPE:
        verdict = "RANGE COMPRESSION"
        detail = ("gaze spans %.0f %% of the target range; a x%.2f gain "
                  "would undo it. Extend the CALIBRATION grid to the "
                  "eccentricities the stimuli use, rather than "
                  "correcting afterwards."
                  % (100 * slope, 1.0 / slope if slope else 0))
    elif offset_share >= OFFSET_DOMINANCE and abs(mean_err) > 20:
        verdict = "UNIFORM OFFSET"
        detail = ("a constant %+.0f px explains %.0f %% of the miss. This "
                  "is a coordinate-space or head-position shift between "
                  "calibration and validation — a GAIN is the wrong "
                  "repair for it."
                  % (mean_err, 100 * offset_share))
    elif ratio and ratio >= 2.0:
        verdict = "ONE-SIDED"
        detail = ("error is %.1fx larger at the %s end (%.0f vs %.0f px). "
                  "A symmetric correction will over-correct the good "
                  "side." % (ratio, worse, max(lo_m, hi_m), min(lo_m, hi_m)))
    else:
        verdict = "UNSTRUCTURED"
        detail = ("error is not explained by target position (slope "
                  "%.2f, offset share %.0f %%). This is noise, not "
                  "distortion — no correction of any shape will remove "
                  "it, and fitting one will overfit these targets."
                  % (slope or 0, 100 * offset_share))

    return {
        "ok": True, "verdict": verdict, "detail": detail,
        "slope": round(slope, 3) if slope is not None else None,
        "implied_gain": round(1.0 / slope, 3) if slope else None,
        "mean_error_px": round(mean_err, 1),
        "offset_share": round(offset_share, 2),
        "worse_end": worse,
        "asymmetry_ratio": round(ratio, 2) if ratio else None,
        "n_targets": len(pts),
    }


def _polyval(coeffs, x: float) -> float:
    out = 0.0
    for c in coeffs:
        out = out * x + float(c)
    return out


def residual_after_correction(targets: list, corr: dict) -> "dict | None":
    """Apply the fitted correction to the raw gaze and see what is left.

    THE QUESTION THIS ANSWERS
    ------------------------
    "The marker sits above where I am actually looking." A gain fitted
    to reduce the MEAN error can still make particular parts of the
    screen worse, and it reliably does so when the error is ONE-SIDED:
    a symmetric expansion about the centre pulls both ends outward, so
    correcting an end that was short necessarily pushes the end that
    was already fine past the target.

    Reporting the mean residual hides this — the overshoot at one end
    cancels the undershoot at the other and the average looks fine. So
    this splits by screen half and reports each end separately, in the
    direction words a person actually experiences: ABOVE or BELOW.
    """
    if not corr or not corr.get("active"):
        return None
    py = corr.get("py")
    px = corr.get("px")
    if not py:
        return None

    rows = []
    for t in targets:
        try:
            ty, my = float(t["ty"]), float(t["my"])
            tx, mx = float(t["tx"]), float(t["mx"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({
            "ty": ty,
            "raw_dy": my - ty,
            "corr_dy": _polyval(py, my) - ty,
            "raw_dx": mx - tx,
            "corr_dx": (_polyval(px, mx) - tx) if px else (mx - tx),
        })
    if len(rows) < 4:
        return None

    centre = (max(r["ty"] for r in rows) + min(r["ty"] for r in rows)) / 2.0
    upper = [r for r in rows if r["ty"] < centre]     # small y = up the screen
    lower = [r for r in rows if r["ty"] >= centre]

    def _mean(rs, key):
        return sum(r[key] for r in rs) / len(rs) if rs else 0.0

    def _word(dy):
        # Screen y grows DOWNWARD, so a negative residual means the
        # recorded point sits higher on the screen than the target.
        return "ABOVE" if dy < 0 else "BELOW"

    up_raw, up_cor = _mean(upper, "raw_dy"), _mean(upper, "corr_dy")
    lo_raw, lo_cor = _mean(lower, "raw_dy"), _mean(lower, "corr_dy")
    all_raw = _mean(rows, "raw_dy")
    all_cor = _mean(rows, "corr_dy")

    # An overshoot is the correction making an end WORSE. Two ways it
    # happens, and the second is the one that matters most:
    #
    #   sign flip   the end was short, is now long
    #   made worse  the end was already fine and got pushed off
    #
    # Only checking for a sign flip misses the second entirely: an end
    # sitting at 0 px error that the correction moves to 61 px never
    # changes sign, because it had no sign to change. That is precisely
    # the case a symmetric gain creates on a ONE-SIDED error, and it is
    # what "the marker is above where I am looking" feels like.
    flipped = []
    for name, raw, cor in (("upper half", up_raw, up_cor),
                           ("lower half", lo_raw, lo_cor)):
        worse_by = abs(cor) - abs(raw)
        if abs(cor) > 20 and (raw * cor < 0 or worse_by > 20):
            flipped.append(
                "%s: %.0f px %s -> %.0f px %s (%s by %.0f px)"
                % (name, abs(raw), _word(raw) if abs(raw) > 5 else "off",
                   abs(cor), _word(cor),
                   "WORSE" if worse_by > 0 else "better", abs(worse_by)))

    return {
        "upper_raw_px": round(up_raw), "upper_corrected_px": round(up_cor),
        "lower_raw_px": round(lo_raw), "lower_corrected_px": round(lo_cor),
        "mean_raw_px": round(all_raw), "mean_corrected_px": round(all_cor),
        "mean_abs_raw_px": round(sum(abs(r["raw_dy"]) for r in rows)
                                 / len(rows)),
        "mean_abs_corrected_px": round(sum(abs(r["corr_dy"]) for r in rows)
                                       / len(rows)),
        "overshoots": flipped,
        "upper_word": _word(up_cor), "lower_word": _word(lo_cor),
    }


def analyse(manifest: dict) -> dict:
    vals = manifest.get("validations") or []
    # The UNCORRECTED check: the fit set, before any gain is applied.
    # Diagnosing the calibration on already-corrected data would be
    # diagnosing the correction.
    fit = [v for v in vals if v.get("phase") in ("pre_fit", "pre")]
    if not fit:
        return {"ok": False, "reason": "no uncorrected pre-validation"}
    rec = fit[0]
    targets = rec.get("targets") or []
    if len(targets) < 4:
        return {"ok": False,
                "reason": "only %d targets — need at least 4" % len(targets)}
    return {
        "ok": True,
        "phase": rec.get("phase"),
        "mean_err_px": rec.get("mean_err_px"),
        "mean_err_deg": rec.get("mean_err_deg"),
        "x": analyse_axis(targets, 0),
        "y": analyse_axis(targets, 1),
        "applied_correction": manifest.get("gain_correction"),
        "residual": residual_after_correction(
            targets, manifest.get("gain_correction") or {}),
    }


def report(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    res = analyse(manifest)
    name = os.path.basename(path).replace("_manifest.json", "")
    print("=" * 74)
    print("  CALIBRATION DIAGNOSIS — %s" % name)
    print("=" * 74)
    if not res.get("ok"):
        print("  %s" % res.get("reason"))
        return 1
    print("  uncorrected accuracy: %s px (%s deg), from the %s check"
          % (res["mean_err_px"], res["mean_err_deg"], res["phase"]))
    corr = res.get("applied_correction") or {}
    if corr.get("active"):
        print("  correction applied  : x x%s, y x%s (%s)"
              % (corr.get("gain_x"), corr.get("gain_y"),
                 corr.get("source")))
    print()
    for axis, label in (("x", "HORIZONTAL"), ("y", "VERTICAL")):
        a = res[axis]
        print("  %s" % label)
        if not a.get("ok"):
            print("    %s" % a.get("reason"))
            continue
        print("    verdict      : %s" % a["verdict"])
        print("    slope        : %.3f  (1.00 = gaze spans the full "
              "target range)" % (a["slope"] or 0))
        print("    mean error   : %+.0f px" % a["mean_error_px"])
        for line in _wrap(a["detail"], 64):
            print("    %s" % line)
        print()

    # ── What the correction actually did, per screen half ──────────
    res_r = res.get("residual")
    if res_r:
        print("  AFTER THE CORRECTION (vertical, per screen half)")
        print("    upper half : %+4d px raw  ->  %+4d px  (%s the target)"
              % (res_r["upper_raw_px"], res_r["upper_corrected_px"],
                 res_r["upper_word"]))
        print("    lower half : %+4d px raw  ->  %+4d px  (%s the target)"
              % (res_r["lower_raw_px"], res_r["lower_corrected_px"],
                 res_r["lower_word"]))
        print("    mean |error| : %d px -> %d px"
              % (res_r["mean_abs_raw_px"], res_r["mean_abs_corrected_px"]))
        if res_r["overshoots"]:
            print()
            print("    *** THE CORRECTION OVERSHOOTS ***")
            for line in res_r["overshoots"]:
                print("      %s" % line)
            print("    A residual that changed SIGN is the correction's")
            print("    doing, not the tracker's: a symmetric gain about")
            print("    the centre pulls both ends outward, so fixing the")
            print("    short end pushes the good end past the target.")
        print()

    # The one that changes what you DO next.
    verdicts = {res["x"].get("verdict"), res["y"].get("verdict")}
    print("  " + "-" * 70)
    if "RANGE COMPRESSION" in verdicts:
        print("  ACTION: this is a calibration-RANGE problem, not a")
        print("  tracking failure. The gain correction is repairing it")
        print("  legitimately, but the better repair is upstream: run the")
        print("  FULL calibration (13 points, not 5) so the grid reaches")
        print("  the eccentricities the validation and stimuli use. A")
        print("  model asked to extrapolate beyond its calibrated span")
        print("  compresses toward the centre — which is exactly the")
        print("  slope printed above.")
    elif "UNIFORM OFFSET" in verdicts:
        print("  ACTION: do NOT fit a gain to this. A constant miss is a")
        print("  coordinate-space or seating shift between calibration")
        print("  and validation; a gain repairs the edges by distorting")
        print("  the centre. Check the screen_space block in the manifest")
        print("  and whether the participant moved after calibrating.")
    elif "UNSTRUCTURED" in verdicts:
        print("  ACTION: the error has no spatial structure, so no")
        print("  correction will remove it. Fitting one overfits seven")
        print("  targets. Improve the SIGNAL instead — lighting on the")
        print("  face, seating distance, a full 13-point calibration.")
    else:
        print("  ACTION: error is one-sided. Report it as a directional")
        print("  limitation rather than correcting symmetrically, and")
        print("  keep stimuli away from the affected edge where you can.")
    return 0


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", nargs="?")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    paths = sorted(_session_glob(), key=os.path.getmtime)
    if args.all:
        if not paths:
            print("No manifests found.")
            return 1
        for p in paths:
            report(p)
            print()
        return 0
    target = args.manifest or (paths[-1] if paths else None)
    if not target:
        print("No manifests found in %s" % RAW_DIR)
        return 1
    return report(target)


if __name__ == "__main__":
    raise SystemExit(main())
