# -*- coding: utf-8 -*-
"""Audit the validation-based gain correction on recorded sessions.

WHAT IT ANSWERS
---------------
1. What did every validation phase measure, CORRECTED and UNCORRECTED,
   as accuracy AND as a signed bias? Both figures come from the same
   seven measurements, so the pair isolates the correction's effect
   instead of comparing two different moments.

2. Did the correction generalise? Leave-one-out on the fit grid answers
   this without touching grid B, so grid B stays an honest out-of-sample
   report.

3. Is the tracker's systematic offset stable within a session? The
   between-phase change in raw signed bias is tested against the
   target-to-target scatter that produced each phase's mean. If the
   change is larger than that noise, no session-wise correction fitted at
   one moment can remove an offset measured at another.

4. What would the pre-declared selection rule (F30, fixed 2026-08-17)
   have chosen for this session, and does that differ from what was
   actually applied?

Usage::

    python correction_audit.py                 # every session on disk
    python correction_audit.py <manifest.json> [...]
    python correction_audit.py --markdown      # table for the log
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import numpy as np

import validation_stats as vs

PHASES = ("pre_fit", "pre_check", "post")


def _manifests() -> list:
    out = []
    try:
        import config
        dirs = [config.STUDY_CSV_DIR, config.GAZEFOLLOWER_CSV_DIR]
    except Exception:  # noqa: BLE001 — the audit must run outside the app
        dirs = [os.path.join("data", "study"),
                os.path.join("data", "gazefollower_raw")]
    for d in dirs:
        if os.path.isdir(d):
            out += sorted(glob.glob(os.path.join(d, "*_manifest.json")))
    return out


def _deg_per_px(vals: dict) -> "float | None":
    """Degrees per pixel, taken from a phase that reported both."""
    for v in vals.values():
        px, deg = v.get("mean_err_px"), v.get("mean_err_deg")
        if px and deg:
            return float(deg) / float(px)
    return None


def _active(v: dict) -> bool:
    return bool((v.get("correction_active") or {}).get("active"))


def audit(path: str) -> "dict | None":
    with open(path, encoding="utf-8") as fh:
        man = json.load(fh)
    vals = {v.get("phase"): v for v in (man.get("validations") or [])
            if v.get("targets")}
    if not vals:
        return {"path": path, "session": man.get("session_id")
                or os.path.basename(path), "usable": False,
                "why": "no per-target validation records "
                       "(reconstructed or pre-dates the two-grid protocol)"}

    gc = man.get("gain_correction") or {}
    applied = {"px": gc.get("px"), "py": gc.get("py")} if gc.get("px") else None
    scr = (vals.get("pre_fit") or next(iter(vals.values()))).get("screen") or {}
    W = float(scr.get("width_px") or 1920)
    H = float(scr.get("height_px") or 1080)
    dpp = _deg_per_px(vals)

    # Every phase mapped back to what the tracker actually produced.
    raw = {ph: vs.raw_targets(v["targets"], applied if _active(v) else None,
                              W, H)
           for ph, v in vals.items()}

    out: dict = {
        "path": path,
        "session": man.get("session_id") or os.path.basename(path),
        "usable": True,
        "applied": {"kind": gc.get("kind"), "px": gc.get("px"),
                    "py": gc.get("py"), "source": gc.get("source")},
        "deg_per_px": dpp,
        "phases": {},
        "recorded_at": {ph: v.get("recorded_at_utc") for ph, v in vals.items()},
    }

    # ── 1. Both ways, every phase ────────────────────────────────────
    for ph in PHASES:
        if ph not in raw:
            continue
        row = {}
        for label, corr in (("raw", None), ("corrected", applied)):
            in_s = (ph == "pre_fit" and corr is not None)
            row[label] = vs.signed_bias(vs.corrected_targets(raw[ph], corr),
                                        dpp, in_sample=in_s)
        out["phases"][ph] = row

    # ── 1b. The off-diagonal terms, per phase ────────────────────────
    # Vertical error that depends on HORIZONTAL position. The correction
    # models a gain per axis and an offset — the diagonal of the map —
    # so a shear passes through it untouched and no reported quantity
    # contained it (F33).
    out["spatial"] = {ph: vs.spatial_terms(raw[ph], W, H)
                      for ph in PHASES if ph in raw}

    # ── 2. Did it generalise? LOO on the fit grid ────────────────────
    if "pre_fit" in raw:
        sel = vs.select_correction(raw["pre_fit"], W, H)
        out["selection"] = sel["decision"]
        out["would_choose"] = sel["decision"]["chosen"]
        out["was_applied"] = bool(applied)
        out["rule_changes_this_session"] = bool(
            (sel["decision"]["chosen"] == "none") != (not applied))
        # In-sample refit vs LOO: the size of the overfit.
        m, t = vs._pairs(raw["pre_fit"])
        refit = vs._fit_candidate(m, t, "affine", W, H)
        if refit:
            insample = vs.signed_bias(
                vs.corrected_targets(raw["pre_fit"], refit), dpp)
            loo = next((c for c in sel["decision"]["candidates"]
                        if c["candidate"] == "affine"), None)
            base = next((c for c in sel["decision"]["candidates"]
                         if c["candidate"] == "none"), None)
            if loo and base:
                out["overfit"] = {
                    "raw_px": base["loo_mean_err_px"],
                    "refit_in_sample_px": insample["mean_err_px"],
                    "loo_out_of_sample_px": loo["loo_mean_err_px"],
                }

    # ── 3. Is the offset stable within the session? ──────────────────
    # The UNEQUAL-VARIANCE (Welch) standard error of the difference
    # between two phases' mean signed residuals. The null is "the
    # tracker's systematic offset did not change between these two
    # checks"; the scatter across targets inside each phase is the noise
    # the mean was estimated from.
    #
    # This was called "Welch's t" and reported only as a count of
    # standard errors. That named a test it did not perform: no
    # Satterthwaite degrees of freedom and no p-value. It matters — on
    # the recorded sessions the df comes out between 9.7 and 11.6, so
    # 2.7 SE is p = 0.021, not the 0.007 a normal approximation would
    # give. The df and the p-value are computed now, so the figure means
    # what it is called.
    #
    # The test stays CONSERVATIVE for a separate reason worth keeping in
    # view: the within-phase scatter includes spatially structured error
    # (the targets sit at different screen positions), so the noise term
    # is inflated and a real change is harder to detect, not easier.
    stab = []
    order = [p for p in PHASES if p in raw]
    for a, b in zip(order, order[1:]):
        ma, ta = vs._pairs(raw[a])
        mb, tb = vs._pairs(raw[b])
        if len(ma) < 3 or len(mb) < 3:
            continue
        da, db = ma - ta, mb - tb
        entry = {"from": a, "to": b,
                 "seconds": _gap_seconds(vals.get(a), vals.get(b))}
        for axis, i in (("x", 0), ("y", 1)):
            xa, xb = da[:, i], db[:, i]
            sa = xa.std(ddof=1) / math.sqrt(len(xa))
            sb = xb.std(ddof=1) / math.sqrt(len(xb))
            se = math.hypot(sa, sb)
            shift = float(xb.mean() - xa.mean())
            # Welch-Satterthwaite degrees of freedom, so the SE count
            # can be turned into a probability honestly.
            df = ((sa ** 2 + sb ** 2) ** 2 /
                  ((sa ** 4) / (len(xa) - 1) + (sb ** 4) / (len(xb) - 1))) \
                if (sa > 0 and sb > 0) else float("nan")
            t = (shift / se) if se > 0 else None
            try:
                from scipy import stats as _st
                pval = (2 * (1 - _st.t.cdf(abs(t), df))
                        if (t is not None and df == df) else None)
            except Exception:  # noqa: BLE001 — scipy is not a hard dep
                pval = None
            entry["d" + axis] = {
                "from_px": round(float(xa.mean()), 1),
                "to_px": round(float(xb.mean()), 1),
                "shift_px": round(shift, 1),
                "se_px": round(se, 1),
                "t": round(t, 2) if t is not None else None,
                "welch_df": round(float(df), 1) if df == df else None,
                "p": round(float(pval), 4) if pval is not None else None,
                "changed": bool(se > 0 and abs(shift) > 2 * se),
            }
        stab.append(entry)
    out["stability"] = stab

    # ── 4. Head position per phase, if this session has it ───────────
    # F33's leading hypothesis for the shear (an off-centre head, or a
    # head pose that differs between calibration and validation) was
    # untestable on every session recorded before the automatic capture
    # landed — head_position was null in all nine manifests because the
    # only thing that ever wrote it was an opt-in guide nobody opened.
    # Surfaced here, next to the shear it might explain, rather than as
    # a separate report — reported, not yet interpreted: nine sessions
    # is not enough to say whether it correlates with anything.
    hp = man.get("head_position") or {}
    by_phase = hp.get("by_phase") or {}
    if by_phase:
        out["head_position"] = {ph: by_phase[ph] for ph in
                                ("calibration",) + PHASES if ph in by_phase}
    return out


def _gap_seconds(a: "dict | None", b: "dict | None") -> "float | None":
    from datetime import datetime
    try:
        ta = datetime.fromisoformat(a["recorded_at_utc"])
        tb = datetime.fromisoformat(b["recorded_at_utc"])
        return round((tb - ta).total_seconds(), 1)
    except Exception:  # noqa: BLE001
        return None


# ══════════════════════════════════════════════════════════════════════
#  Rendering
# ══════════════════════════════════════════════════════════════════════

def _fmt_phase(ph: str, label: str, s: dict, dpp) -> str:
    if not s.get("bias_available"):
        return "  %-10s %-10s  no measured targets" % (ph, label)
    deg = ("  %.2f deg" % (s["mean_err_px"] * dpp)) if dpp else ""
    tag = ""
    if s.get("bias_in_sample"):
        tag = "  <- IN-SAMPLE, bias is zero by construction"
    elif s.get("offset_dominated"):
        tag = "  <- OFFSET-DOMINATED (%.0f %% of the error is a fixed " \
              "displacement)" % (100 * s["bias_ratio"])
    return ("  %-10s %-10s mean %6.1f px%s | median %6.1f px | "
            "bias (%+6.1f, %+6.1f) = %5.1f px%s"
            % (ph, label, s["mean_err_px"], deg, s["median_err_px"],
               s["bias_x_px"], s["bias_y_px"], s["bias_px"], tag))


def render(a: dict) -> None:
    print("=" * 78)
    print("  %s" % a["session"])
    print("=" * 78)
    if not a.get("usable"):
        print("  %s\n" % a["why"])
        return
    ap = a["applied"]
    print("  applied correction : %s  px=%s  py=%s"
          % (ap.get("kind") or "none", ap.get("px"), ap.get("py")))
    dpp = a.get("deg_per_px")
    print()
    for ph in PHASES:
        row = a["phases"].get(ph)
        if not row:
            continue
        print(_fmt_phase(ph, "raw", row["raw"], dpp))
        print(_fmt_phase(ph, "corrected", row["corrected"], dpp))
    print()

    of = a.get("overfit")
    if of:
        print("  Did it generalise?  raw %.1f px -> %.1f px refit on all "
              "seven (IN-SAMPLE) -> %.1f px leave-one-out"
              % (of["raw_px"], of["refit_in_sample_px"],
                 of["loo_out_of_sample_px"]))
        gap = of["refit_in_sample_px"] - of["loo_out_of_sample_px"]
        print("                      the %.1f px between the last two is the "
              "overfit, and it needed no second grid to see." % abs(gap))
        print()

    sel = a.get("selection")
    if sel:
        print("  Pre-declared rule (fixed %s) would choose: %s"
              % (sel.get("rule_fixed_on"), sel["chosen"].upper()))
        print("    %s" % sel.get("reason", ""))
        for c in sel.get("candidates", []):
            if c.get("loo_mean_err_px") is None:
                print("      %-20s %s" % (c["candidate"],
                                          c.get("status", "not evaluated")))
                continue
            print("      %-20s LOO mean %7.1f px  median %7.1f px  "
                  "|bias| %6.1f px  %s"
                  % (c["candidate"], c["loo_mean_err_px"],
                     c["loo_median_err_px"], c["loo_bias_px"],
                     ("beats none by %.1f SE" % c["improvement_se_units"])
                     if c.get("improvement_se_units") is not None else ""))
            # The standard error above treats seven leave-one-out folds
            # as independent and they share five of seven training
            # targets each, so it is optimistic by an unknown amount.
            # These two are distribution-free and corroborate — or fail
            # to corroborate — the figure the rule actually used.
            if c.get("loo_bootstrap_ci_px"):
                lo, hi = c["loo_bootstrap_ci_px"]
                print("      %-20s   bootstrap 95%% CI [%+.1f, %+.1f] px "
                      "(%s zero) · improved %s targets, sign test p = %.3f"
                      % ("", lo, hi,
                         "excludes" if c.get("loo_bootstrap_excludes_zero")
                         else "SPANS",
                         c.get("loo_targets_improved"),
                         c.get("loo_sign_test_p")))
        if a.get("rule_changes_this_session"):
            print("    *** This DIFFERS from what was applied. The session "
                  "must be re-derived. ***")
        print()

    sp = a.get("spatial") or {}
    if any(v.get("spatial_available") for v in sp.values()):
        print("  Off-diagonal terms  (measured = M . (target - centre) + "
              "offset; the correction models only M's DIAGONAL)")
        for ph in PHASES:
            v = sp.get(ph)
            if not (v and v.get("spatial_available")):
                continue
            print("    %-10s mxx %6.3f  mxy %6.3f  myx %6.3f  myy %6.3f  |"
                  "  shear %+.3f  rot %+.2f deg  resid %5.1f px"
                  % (ph, v["m_xx"], v["m_xy"], v["m_yx"], v["m_yy"],
                     v["shear"], v["rotation_deg"], v["residual_px"]))
            print("               myx = %+.3f, 95%% CI [%+.3f, %+.3f] (%s) "
                  "-> %+.0f px of VERTICAL error across the screen from "
                  "HORIZONTAL position alone%s"
                  % (v["m_yx"], v["m_yx_ci"][0], v["m_yx_ci"][1],
                     "excludes 0" if v["m_yx_excludes_zero"] else "spans 0",
                     v["dy_across_screen_px"],
                     "   *** SHEARED ***" if v["shear_large"] else ""))
            print("               %s" % v["structure"])
        print()

    stab = a.get("stability") or []
    if stab:
        print("  Is the offset stable?  (change in RAW signed bias between "
              "consecutive checks)")
        for e in stab:
            gap = ("%.0f s apart" % e["seconds"]) if e.get("seconds") \
                else "gap unknown"
            for axis in ("dx", "dy"):
                d = e.get(axis)
                if not d:
                    continue
                print("    %-9s -> %-9s %-13s %s %+7.1f -> %+7.1f px "
                      "(shift %+6.1f, %.1f SE%s)%s"
                      % (e["from"], e["to"], gap, axis, d["from_px"],
                         d["to_px"], d["shift_px"], abs(d["t"] or 0),
                         (", Welch df %.1f, p = %.3f"
                          % (d["welch_df"], d["p"]))
                         if d.get("p") is not None else "",
                         "   CHANGED" if d["changed"] else ""))
        print()

    hp = a.get("head_position") or {}
    if hp:
        print("  Head position per phase (automatic capture)")
        for ph, snap in hp.items():
            if not snap.get("available"):
                print("    %-11s no face geometry at capture time" % ph)
                continue
            bits = []
            if snap.get("est_distance_cm") is not None:
                bits.append("dist %.1f cm (%s)"
                            % (snap["est_distance_cm"],
                               snap.get("distance_source") or "?"))
            if snap.get("roll_deg") is not None:
                bits.append("roll %+.1f deg" % snap["roll_deg"])
            if snap.get("face_center_x") is not None:
                bits.append("face_x %.2f" % snap["face_center_x"])
            if snap.get("face_center_y") is not None:
                bits.append("face_y %.2f" % snap["face_center_y"])
            print("    %-11s %s" % (ph, ", ".join(bits) or "(no geometry "
                                                            "fields)"))
        print()
    elif a.get("spatial"):
        # Only worth saying when the session HAS something head position
        # could explain — printing this on every session would be noise.
        if any(v.get("shear_large") for v in a["spatial"].values()):
            print("  Head position: not recorded for this session "
                  "(pre-dates automatic capture) — cannot test whether "
                  "head placement explains the shear above.")
            print()


def render_markdown(audits: list) -> None:
    print("| session | phase | basis | mean px | median px | bias x | "
          "bias y | \\|bias\\| | offset-dominated |")
    print("|---|---|---|---|---|---|---|---|---|")
    for a in audits:
        if not a.get("usable"):
            continue
        for ph in PHASES:
            row = a["phases"].get(ph)
            if not row:
                continue
            for label in ("raw", "corrected"):
                s = row[label]
                if not s.get("bias_available"):
                    continue
                flag = "in-sample" if s.get("bias_in_sample") else \
                    ("**yes**" if s.get("offset_dominated") else "no")
                print("| %s | %s | %s | %.1f | %.1f | %+.1f | %+.1f | %.1f "
                      "| %s |" % (a["session"], ph, label, s["mean_err_px"],
                                  s["median_err_px"], s["bias_x_px"],
                                  s["bias_y_px"], s["bias_px"], flag))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--markdown", action="store_true",
                    help="emit the comparison table for METHODOLOGY_FINDINGS")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    files = args.paths or _manifests()
    if not files:
        print("No manifests found.")
        return 1
    audits = [audit(f) for f in files]
    audits = [a for a in audits if a]
    if args.json:
        print(json.dumps(audits, indent=2, default=float))
    elif args.markdown:
        render_markdown(audits)
    else:
        for a in audits:
            render(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
