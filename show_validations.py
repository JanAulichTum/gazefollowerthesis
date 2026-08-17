# -*- coding: utf-8 -*-
"""Print every validation record of a session, target by target.

WHY
---
The session summary showed ``pre_check`` and ``post`` with an identical
corrected accuracy - 83.6 px to the tenth of a pixel - while their
UNCORRECTED figures differed (4.31 vs 4.17 deg). Two independent
seven-target measurements taken a minute apart do not agree to 0.1 px by
chance, so one of three things is true and they have different remedies:

  1. the two records hold the same per-target errors, i.e. one
     validation was written twice and one measurement does not exist;
  2. the per-target errors differ and the means coincide, which is
     luck and needs nothing;
  3. the correction is re-fitted to each validation's own targets, in
     which case both figures are IN-SAMPLE residuals and the two-grid
     design has quietly collapsed - ``pre_check`` would no longer be
     out-of-sample and could not be reported as corrected accuracy.

Only the per-target numbers separate these, so this prints them.

Usage::

    python show_validations.py                  # most recent session
    python show_validations.py --all
    python show_validations.py <manifest.json>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import config


def _manifests() -> list:
    """Newest last, ordered by the timestamp IN THE NAME.

    Not by mtime: a file can be touched long after it was recorded - by
    a backfill, a copy, a sync - and then "the most recent session" is
    whichever file was last written to, which is not the same question.
    The session id already carries the recording time.
    """
    out = []
    for d in (config.STUDY_CSV_DIR, config.GAZEFOLLOWER_CSV_DIR):
        if os.path.isdir(d):
            out += [os.path.join(d, f) for f in os.listdir(d)
                    if f.endswith("_manifest.json")]

    def _key(path):
        m = re.search(r"(\d{4}-\d{2}-\d{2}_\d{6})", os.path.basename(path))
        return (m.group(1) if m else "0000-00-00_000000",
                os.path.basename(path))
    return sorted(out, key=_key)


def _signed_bias(v: dict) -> "dict | None":
    """Signed bias for one validation record, from its per-target rows.

    Computed rather than read: the point of this tool is to see what the
    per-target numbers say, and the sessions that most need looking at
    are the ones recorded before the record carried a bias field.
    """
    targets = v.get("per_target") or v.get("targets") or []
    px, deg = v.get("mean_err_px"), v.get("mean_err_deg")
    dpp = (deg / px) if (px and deg) else None
    try:
        import validation_stats
    except ImportError:
        return None
    out = validation_stats.signed_bias(targets, dpp)
    return out if out.get("bias_available") else None


def report(path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        man = json.load(fh)
    print("=" * 74)
    print("  %s" % os.path.basename(path))
    print("=" * 74)

    dist = (man.get("distance") or {})
    if dist:
        print("  distance : %s cm via %s   (iris %s / iod %s, agree=%s)"
              % (dist.get("cm"), dist.get("source"), dist.get("iris_cm"),
                 dist.get("iod_cm"), dist.get("estimates_agree")))
        if dist.get("iris_error"):
            print("             iris unavailable: %s" % dist["iris_error"])
        if dist.get("iris_traceback"):
            print("             where it failed:")
            for _ln in str(dist["iris_traceback"]).strip().splitlines()[-6:]:
                print("               %s" % _ln)
        if dist.get("source") and "iris" not in str(dist.get("source")):
            print()
            print("  *** The FALLBACK ruler produced this distance. Its")
            print("      population spread is ~11 % and it foreshortens")
            print("      with head yaw. Every angle in this session is")
            print("      scaled by it. ***")
        print()

    vals = man.get("validations") or []
    fingerprints = {}
    for v in vals:
        print("  %-10s grid=%-3s corrected=%-5s n=%-3s mean_err_px=%s "
              "raw_deg=%s deg=%s"
              % (v.get("phase"), v.get("grid"), v.get("correction_active"),
                 (v.get("n_targets") if v.get("n_targets") is not None
                  else len(v.get("targets") or []) or None),
                 v.get("mean_err_px"),
                 v.get("mean_err_deg_raw"), v.get("mean_err_deg")))
        per = v.get("per_target") or v.get("targets") or []
        errs = []
        for t in per:
            if isinstance(t, dict):
                errs.append(t.get("err_px") if t.get("err_px") is not None
                            else t.get("error_px"))
        if errs:
            print("      per-target px: %s"
                  % ", ".join("%.0f" % e for e in errs if e is not None))
            fingerprints.setdefault(tuple(errs), []).append(v.get("phase"))
        # ── The SIGNED bias ─────────────────────────────────────────
        # Derived from the per-target numbers rather than read from the
        # record, so sessions written before 2026-08-17 show it too. A
        # mean unsigned error reads the same for 60 px of scatter and
        # 60 px of uniform displacement; only this separates them, and
        # this study had the second (F30).
        _b = _signed_bias(v)
        if _b:
            print("      signed bias  : %.0f px (x %+.0f, y %+.0f) — %s"
                  % (_b["bias_px"], _b["bias_x_px"], _b["bias_y_px"],
                     _b["bias_direction"]))
            print("      mean / median: %.0f px / %.0f px   worst target "
                  "%.0f px" % (_b["mean_err_px"], _b["median_err_px"],
                               _b["max_err_px"]))
            if _b["offset_dominated"]:
                print("      *** OFFSET-DOMINATED: %.0f %% of the error is "
                      "a fixed displacement," % (100 * _b["bias_ratio"]))
                print("          not scatter. Scatter averages out of an "
                      "aggregate; this does not — every")
                print("          gaze point in the recording is moved the "
                      "same way. ***")
        print("      recorded_at  : %s"
              % (v.get("recorded_at_utc") or v.get("recorded_at", "?")))
        print()

    dec = man.get("correction_decision")
    if dec:
        print("  Correction decision: %s — %s"
              % (str(dec.get("chosen")).upper(), dec.get("reason", "")))
        for c in dec.get("candidates", []):
            if c.get("loo_mean_err_px") is None:
                print("    %-20s %s" % (c["candidate"], c.get("status", "")))
                continue
            print("    %-20s leave-one-out %7.1f px | |bias| %6.1f px%s"
                  % (c["candidate"], c["loo_mean_err_px"], c["loo_bias_px"],
                     ("   (%.1f SE vs none)" % c["improvement_se_units"])
                     if c.get("improvement_se_units") is not None else ""))
        print("    Rule fixed %s. It replaced: %s"
              % (dec.get("rule_fixed_on"), dec.get("previous_rule")))
        print()
    elif any(v.get("targets") for v in vals):
        print("  *** No correction decision recorded. This session pre-dates")
        print("      the cross-validated rule (2026-08-17): its correction")
        print("      was fitted on grid A and applied unconditionally, with")
        print("      no check that it generalised. correction_audit.py shows")
        print("      what the current rule would choose. ***")
        print()

    for errs, phases in fingerprints.items():
        if len(phases) > 1:
            print("  *** %s share IDENTICAL per-target errors. That is not a"
                  % " and ".join(phases))
            print("      coincidence: one measurement was written twice and")
            print("      the other does not exist. ***")
            print()
    if len(fingerprints) == len(
            [v for v in vals if (v.get("per_target") or v.get("targets"))]) \
            and len(vals) > 1:
        print("  Per-target errors differ between phases, so the records are")
        print("  genuinely separate measurements.")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    files = _manifests()
    if args.path:
        files = [args.path]
    elif not args.all:
        files = files[-1:]
    if not files:
        print("No manifests found.")
        return 1
    if not args.path and not args.all:
        print("  %d session(s) on disk; showing the most recent."
              % len(_manifests()))
        print("  Use --all to see every one, or pass a filename.")
        print()
    for f in files:
        report(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
