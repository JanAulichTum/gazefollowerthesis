# -*- coding: utf-8 -*-
"""Re-derive a recorded session under the current correction rule.

WHY THIS EXISTS
---------------
F30 fixed a rule for whether a validation-based gain correction is
applied: it must beat no-correction out of sample, judged by leave-one-out
on the fit grid. Applying that rule to sessions already recorded is not a
matter of editing the manifest — the correction was baked into the
recorded gaze at finalisation time, and the analysis reads the recorded
gaze.

Deciding that a session's correction should not have been applied, and
then leaving the corrected coordinates in place, would be worse than not
having the rule at all: the manifest would say one thing and the data
would be another.

WHAT IS RE-DERIVABLE, AND WHY
-----------------------------
Nothing is lost by applying a correction, because the correction is
applied DOWNSTREAM of everything it touches:

    filtered_gaze_position_x/y      untouched by the correction
    corrected_gaze_position_x/y     = poly(filtered)
    gaze_video_nx/ny                = (corrected - rect.x) / rect.w
                                      (or from filtered, when no
                                       correction was active)

So the whole chain regenerates from ``filtered_gaze_position_*`` and the
per-stimulus ``video_rect`` recorded in the manifest. This mirrors
``app.finalize_gazefollower_session`` exactly, including the rounding, so
a re-derived session is byte-comparable with one recorded under the same
correction. ``run_tests.py`` asserts that equivalence rather than trusting
the two copies to stay in step.

WHAT IT WILL NOT DO
-------------------
* It never deletes. The workbook and the manifest are copied to
  ``*.pre-rederive.*`` first, and the manifest keeps the previous
  correction under ``superseded_gain_correction``.
* It does not recompute the LLM feedback, the fixation events or the
  quality metrics. Those derive from the gaze it rewrites, so a
  re-derived session must have them regenerated; the manifest is marked
  ``rederived`` with ``events_stale: true`` so nothing downstream can
  quietly use the old numbers.
* It does not decide anything. The rule decides
  (``validation_stats.select_correction``); this applies the decision.

Usage::

    python rederive_session.py --dry-run             # every session
    python rederive_session.py --dry-run <manifest>
    python rederive_session.py <manifest>            # apply
    python rederive_session.py <manifest> --workbook path/to/data.xlsx
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import pandas as pd

import validation_stats as vs


def video_coords(gx, gy, rect: dict):
    """Normalised video coordinates, matching finalisation exactly.

    Kept in one place and asserted against app.py by the test suite: two
    copies of this arithmetic drifting apart would silently move every
    gaze point relative to the stimulus, which is the one error this
    whole exercise is about.
    """
    return (((gx - rect["x"]) / rect["w"]).round(4),
            ((gy - rect["y"]) / rect["h"]).round(4))


def session_id_of(manifest: dict, manifest_path: str) -> str:
    """The id the workbook's ``session_id`` column actually holds.

    Finalisation derives it as the basename of the session CSV
    (``app.finalize_gazefollower_session``), and most manifests DO NOT
    store it — only reconstructed ones do. Reading ``manifest["session_id"]``
    and falling back to the manifest filename matches nothing and reports
    "no rows for this session", which reads like a clean result and is a
    silent no-op on the one operation that must not silently no-op.
    """
    csv = manifest.get("session_csv")
    if csv:
        return os.path.splitext(os.path.basename(csv))[0]
    if manifest.get("session_id"):
        return str(manifest["session_id"])
    return os.path.basename(manifest_path).replace("_manifest.json", "")


def decide(manifest: dict) -> dict:
    """What the current rule says about this session."""
    vals = {v.get("phase"): v for v in (manifest.get("validations") or [])
            if v.get("targets")}
    gc = manifest.get("gain_correction") or {}
    applied = {"px": gc.get("px"), "py": gc.get("py")} if gc.get("px") else None
    out = {"applied": applied, "applied_kind": gc.get("kind")}
    fit = vals.get("pre_fit") or vals.get("pre")
    if not fit:
        out.update(decidable=False,
                   why="no per-target fit-grid record; the rule cannot be "
                       "evaluated and nothing is changed")
        return out
    scr = fit.get("screen") or {}
    w = float(scr.get("width_px") or 1920)
    h = float(scr.get("height_px") or 1080)
    active = applied if (fit.get("correction_active") or {}).get("active") \
        else None
    raw = vs.raw_targets(fit["targets"], active, w, h)
    sel = vs.select_correction(raw, w, h)
    out.update(decidable=True, decision=sel["decision"],
               chosen=sel["decision"]["chosen"], correction=sel["correction"])
    # Does the verdict differ from what is in the recorded data?
    was = bool(applied)
    now = sel["correction"] is not None
    if was != now:
        out["changes"] = "applied" if now else "removed"
    elif now and applied and (
            [round(c, 6) for c in sel["correction"]["px"]] != [
                round(c, 6) for c in applied["px"]]
            or [round(c, 6) for c in sel["correction"]["py"]] != [
                round(c, 6) for c in applied["py"]]):
        out["changes"] = "refitted"
    else:
        out["changes"] = None
    return out


def rederive(manifest_path: str, workbook: str, apply: bool) -> dict:
    with open(manifest_path, encoding="utf-8") as fh:
        man = json.load(fh)
    sid = session_id_of(man, manifest_path)
    verdict = decide(man)
    report = {"session": sid, "manifest": manifest_path, **verdict,
              "rows_rewritten": 0, "applied_to_disk": False}
    if not verdict.get("decidable"):
        return report
    if not verdict.get("changes"):
        report["note"] = ("the recorded gaze already matches the rule; "
                          "nothing to do")
        return report
    if not os.path.isfile(workbook):
        report["note"] = ("WORKBOOK NOT FOUND at %s — the manifest can be "
                          "updated but the recorded gaze cannot be, and a "
                          "manifest that disagrees with its data is worse "
                          "than neither. Nothing changed." % workbook)
        return report

    df = pd.read_excel(workbook)
    need = {"session_id", "stimulus_name", "filtered_gaze_position_x",
            "filtered_gaze_position_y"}
    missing = need - set(df.columns)
    if missing:
        report["note"] = "workbook lacks %s" % ", ".join(sorted(missing))
        return report

    rects = {s["stimulus"]: s.get("video_rect") or {}
             for s in (man.get("stimuli") or [])}
    corr = verdict.get("correction")
    mask = df["session_id"].astype(str) == str(sid)
    if not mask.any():
        report["note"] = "no rows for this session in the workbook"
        return report

    changed = 0
    for stim, rect in rects.items():
        sel = mask & (df["stimulus_name"] == stim)
        if not sel.any() or not (rect.get("w") and rect.get("h")):
            continue
        gx = pd.to_numeric(df.loc[sel, "filtered_gaze_position_x"],
                           errors="coerce")
        gy = pd.to_numeric(df.loc[sel, "filtered_gaze_position_y"],
                           errors="coerce")
        if corr:
            gx = pd.Series(vs.apply_axis(gx.to_numpy(), corr.get("px")),
                           index=gx.index)
            gy = pd.Series(vs.apply_axis(gy.to_numpy(), corr.get("py")),
                           index=gy.index)
            df.loc[sel, "corrected_gaze_position_x"] = gx.round(2)
            df.loc[sel, "corrected_gaze_position_y"] = gy.round(2)
        else:
            # No correction now: the corrected columns must not linger,
            # or "recommended for analysis" (DATA_README) still points at
            # numbers the rule rejected.
            for col in ("corrected_gaze_position_x",
                        "corrected_gaze_position_y"):
                if col in df.columns:
                    df.loc[sel, col] = pd.NA
        nx, ny = video_coords(gx, gy, rect)
        df.loc[sel, "gaze_video_nx"] = nx
        df.loc[sel, "gaze_video_ny"] = ny
        changed += int(sel.sum())
    report["rows_rewritten"] = changed

    if not apply:
        return report

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(workbook, "%s.pre-rederive.%s.xlsx"
                 % (os.path.splitext(workbook)[0], stamp))
    shutil.copy2(manifest_path, "%s.pre-rederive.%s.json"
                 % (os.path.splitext(manifest_path)[0], stamp))
    df.to_excel(workbook, index=False)

    man["superseded_gain_correction"] = man.get("gain_correction")
    man["gain_correction"] = ({"active": False} if corr is None
                              else vs.payload(corr))
    man["correction_decision"] = verdict["decision"]
    man["rederived"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "rule_fixed_on": verdict["decision"].get("rule_fixed_on"),
        "change": verdict["changes"],
        "rows_rewritten": changed,
        # Everything computed FROM the gaze is now out of date. Saying so
        # in the record is the difference between a re-derived session
        # and a half-re-derived one.
        "events_stale": True,
        "stale": ["events (fixations, saccades)", "data_quality",
                  "LLM feedback and any claim_check output",
                  "replay payloads"],
        "why": ("F30: the correction is applied only when it beats "
                "no-correction out of sample under leave-one-out on the "
                "fit grid. This session's recorded gaze did not match "
                "that decision."),
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)
    report["applied_to_disk"] = True
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workbook", default=None)
    args = ap.parse_args()

    try:
        import config
        default_wb = config.GAZEFOLLOWER_DATA_FILE
        dirs = [config.STUDY_CSV_DIR, config.GAZEFOLLOWER_CSV_DIR]
    except Exception:  # noqa: BLE001
        default_wb = os.path.join("data", "gazefollower_data.xlsx")
        dirs = [os.path.join("data", "study"),
                os.path.join("data", "gazefollower_raw")]
    workbook = args.workbook or default_wb

    files = args.paths
    if not files:
        for d in dirs:
            files += sorted(glob.glob(os.path.join(d, "*_manifest.json")))
    if not files:
        print("No manifests found.")
        return 1

    for f in files:
        r = rederive(f, workbook, apply=not args.dry_run)
        print("=" * 74)
        print("  %s" % r["session"])
        if not r.get("decidable"):
            print("  %s" % r.get("why"))
            continue
        print("  applied: %s   rule chooses: %s   -> %s"
              % (r.get("applied_kind") or "none", r["chosen"],
                 r["changes"] or "no change"))
        print("  %s" % r["decision"].get("reason", ""))
        if r.get("note"):
            print("  %s" % r["note"])
        if r["changes"]:
            print("  rows %s: %d"
                  % ("rewritten" if r["applied_to_disk"] else "that WOULD be "
                     "rewritten", r["rows_rewritten"]))
            if r["applied_to_disk"]:
                print("  *** events, data_quality and any LLM feedback for "
                      "this session are now STALE and must be regenerated. "
                      "The manifest says so. ***")
            elif args.dry_run:
                print("  (dry run — nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
