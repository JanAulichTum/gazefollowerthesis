# -*- coding: utf-8 -*-
"""
Design A analysis: does the LLM name the region at the reported gaze
location? Frozen 2026-10-02, before the first evaluation participant.

WHAT IT COMPUTES
----------------
Unit: one coded fixation (data/coding/, written by /coder). Sessions:
evaluation sessions only (config.is_evaluation_session) that pass the
ONE inclusion rule (metrics_spec.inclusion); per stimulus, a stimulus
failing its data-loss / rate floor is dropped.

  pct_right    RIGHT / (RIGHT + WRONG), pooled over fixations
  pct_unclear  UNCLEAR / (RIGHT + WRONG + UNCLEAR)
  per clip     the same, per stimulus
  blind-first  pct_right on blind-first units vs normal units, and the
               difference (normal − blind-first) = the inflation caused
               by seeing the claim before judging it

UNCERTAINTY: fixations within a participant are not independent, so
every interval is a PARTICIPANT-CLUSTER bootstrap (resample participants
with replacement, recompute the pooled ratio), B = 10,000, fixed seed,
95 % percentile interval.

FAILURE ANALYSIS: every WRONG verdict's free text is written to a CSV
for post-hoc classification, with its unit (time, position, clip,
participant, session accuracy).

Usage::

    python analysis_designA.py                 # first coder per file
    python analysis_designA.py --coder JA
    python analysis_designA.py --include-dev   # dry run on pilot data
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import config
import metrics_spec as SPEC
from coding_report import verdict as _verdict

SEED = 20261002
N_BOOT = 10_000
CODING_DIR = os.path.join(config.DATA_DIR, "coding")
OUT_DIR = os.path.join(config.DATA_DIR, "analysis")


def _pooled(units: list) -> dict:
    r = sum(1 for u in units if u["v"] == "correct")
    w = sum(1 for u in units if u["v"] == "wrong")
    c = sum(1 for u in units if u["v"] == "unclear")
    return {"right": r, "wrong": w, "unclear": c, "n": r + w + c,
            "pct_right": 100.0 * r / (r + w) if (r + w) else None,
            "pct_unclear": 100.0 * c / (r + w + c) if (r + w + c) else None}


def _boot(by_pid: dict, stat, n_boot: int = N_BOOT) -> "tuple | None":
    pids = sorted(by_pid)
    if len(pids) < 2:
        return None
    rng = np.random.default_rng(SEED)
    vals = []
    for _ in range(n_boot):
        pick = rng.choice(len(pids), size=len(pids), replace=True)
        units = [u for i in pick for u in by_pid[pids[i]]]
        s = stat(units)
        if s is not None:
            vals.append(s)
    if not vals:
        return None
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return round(float(lo), 1), round(float(hi), 1)


def _load_manifest(session: str) -> "dict | None":
    p = config.find_manifest(session)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def collect(coder: "str | None", include_dev: bool) -> tuple:
    units: list = []
    excluded: list = []
    seen = set()
    for path in sorted(glob.glob(os.path.join(CODING_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        session, stim = rec.get("session"), rec.get("stimulus")
        if coder and rec.get("coder") != coder:
            continue
        if (session, stim) in seen:      # first coder only, unless --coder
            continue
        seen.add((session, stim))
        if not include_dev and not config.is_evaluation_session(session):
            excluded.append((session, stim, "not an evaluation session"))
            continue
        man = _load_manifest(session)
        if man is None:
            excluded.append((session, stim, "no manifest"))
            continue
        inc = SPEC.inclusion(man)
        s_inc = (inc["stimuli"].get(stim) or {"include": inc["include"],
                                               "reasons": []})
        if not inc["include"] or not s_inc["include"]:
            excluded.append((session, stim, "; ".join(
                inc["reasons"] + s_inc["reasons"]) or "excluded"))
            continue
        pid = man.get("participant_id") or session.rsplit("_", 2)[0]
        for uid, c in (rec.get("codes") or {}).items():
            v = _verdict(c)
            if v not in ("correct", "wrong", "unclear"):
                continue
            c = c if isinstance(c, dict) else {}
            units.append({"pid": pid, "session": session, "stimulus": stim,
                          "unit": uid, "v": v,
                          "blind_first": bool(c.get("blind_first")),
                          "blind_naming": c.get("blind_naming"),
                          "claim": c.get("claim"),
                          "note": c.get("wrong_note"),
                          "t_start": c.get("t_start"), "x": c.get("x"),
                          "y": c.get("y"),
                          "accuracy_deg": inc.get("accuracy_deg"),
                          "coder": rec.get("coder"),
                          "coded_at_utc": c.get("coded_at_utc")})
    return units, excluded


def _by_pid(units: list) -> dict:
    d = defaultdict(list)
    for u in units:
        d[u["pid"]].append(u)
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coder", default=None)
    ap.add_argument("--include-dev", action="store_true",
                    help="DRY RUN on development/pilot sessions")
    args = ap.parse_args()

    units, excluded = collect(args.coder, args.include_dev)
    print("=" * 72)
    print("  DESIGN A — LLM names the region at the reported gaze location")
    print("  inclusion rule fixed %s (ruler: %s)"
          % (SPEC.INCLUSION.get("fixed_on"), SPEC.INCLUSION.get("ruler")))
    if args.include_dev:
        print("  *** DRY RUN including development data — not a result ***")
    print("=" * 72)
    for s, st, why in excluded:
        print("  excluded: %s / %s — %s" % (s, st, why))
    if not units:
        print("\n  No coded fixations in included sessions yet.")
        return 1

    pr = lambda us: _pooled(us)["pct_right"]          # noqa: E731
    pu = lambda us: _pooled(us)["pct_unclear"]        # noqa: E731
    bp = _by_pid(units)
    res = {"n_participants": len(bp), "n_units": len(units),
           "overall": _pooled(units),
           "ci95_pct_right": _boot(bp, pr),
           "ci95_pct_unclear": _boot(bp, pu),
           "per_clip": {}, "excluded": excluded,
           "seed": SEED, "n_boot": N_BOOT,
           "inclusion_rule": SPEC.INCLUSION}
    for stim in sorted({u["stimulus"] for u in units}):
        us = [u for u in units if u["stimulus"] == stim]
        res["per_clip"][stim] = dict(_pooled(us),
                                     ci95_pct_right=_boot(_by_pid(us), pr))
    bf = [u for u in units if u["blind_first"]]
    nb = [u for u in units if not u["blind_first"]]

    def _diff(us):
        a = _pooled([u for u in us if not u["blind_first"]])["pct_right"]
        b = _pooled([u for u in us if u["blind_first"]])["pct_right"]
        return None if a is None or b is None else a - b
    res["blind_first"] = {
        "blind_first": _pooled(bf), "normal": _pooled(nb),
        "inflation_pct_points": _diff(units),
        "ci95_inflation": _boot(bp, _diff)}

    o = res["overall"]
    print("\n  participants %d · coded fixations %d" % (len(bp), len(units)))
    print("  RIGHT %d · WRONG %d · UNCLEAR %d" % (o["right"], o["wrong"],
                                                o["unclear"]))
    print("  %% RIGHT (of RIGHT+WRONG): %s  95%% CI %s"
          % ("%.1f" % o["pct_right"] if o["pct_right"] is not None else "-",
             res["ci95_pct_right"]))
    print("  %% UNCLEAR (of all):       %s  95%% CI %s"
          % ("%.1f" % o["pct_unclear"] if o["pct_unclear"] is not None
             else "-", res["ci95_pct_unclear"]))
    for stim, r in res["per_clip"].items():
        print("  %-22s %% RIGHT %s  CI %s  (n=%d)" % (
            stim, "%.1f" % r["pct_right"] if r["pct_right"] is not None
            else "-", r["ci95_pct_right"], r["n"]))
    b = res["blind_first"]
    f1 = lambda x: "-" if x is None else "%.1f" % x    # noqa: E731
    print("  blind-first %% RIGHT %s (n=%d) vs normal %s (n=%d) -> "
          "inflation %s pp, CI %s"
          % (f1(b["blind_first"]["pct_right"]), b["blind_first"]["n"],
             f1(b["normal"]["pct_right"]), b["normal"]["n"],
             f1(b["inflation_pct_points"]), b["ci95_inflation"]))

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    jp = os.path.join(OUT_DIR, "designA_%s.json" % stamp)
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2, default=str)
    cp = os.path.join(OUT_DIR, "designA_wrong_notes_%s.csv" % stamp)
    with open(cp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["participant", "session", "stimulus", "unit", "t_start",
                    "x", "y", "claim", "what_is_under_marker",
                    "blind_first", "accuracy_deg", "coder", "coded_at_utc"])
        for u in units:
            if u["v"] == "wrong":
                w.writerow([u["pid"], u["session"], u["stimulus"], u["unit"],
                            u["t_start"], u["x"], u["y"], u["claim"],
                            u["note"], u["blind_first"], u["accuracy_deg"],
                            u["coder"], u["coded_at_utc"]])
    print("\n  written: %s\n           %s" % (jp, cp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
