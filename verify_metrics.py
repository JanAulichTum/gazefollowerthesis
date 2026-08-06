# -*- coding: utf-8 -*-
"""
Check that a recorded session actually contains every metric the RQs need.

WHY
---
"Which metrics do I collect?" is answered by metrics_spec.py. This answers
the second, more dangerous question: did this session actually produce
them, and are the values meaningful rather than merely present?

The distinction matters. A field can exist and be null. A fixation count
can be present and be zero because the detector never fired. A dwell
proportion can be 100 % because every sample landed outside every AOI and
the denominator collapsed. Each of those passes a naive "is the key
there?" check and fails the only check that counts.

So every metric is graded three ways:

    PRESENT      the field exists and holds a usable value
    MISSING      not produced at all
    DEGENERATE   present but not trustworthy (null, zero where zero is
                 impossible, or outside a physically plausible range)

Run it after every pilot session. The point is to discover a gap while
you can still re-record, not while writing up.

Usage::

    python verify_metrics.py                       # newest session
    python verify_metrics.py <manifest.json>
    python verify_metrics.py --all                 # every session
    python verify_metrics.py --spec                # print the spec only
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE, "data", "gazefollower_raw")

import metrics_spec as SPEC  # noqa: E402

PRESENT, MISSING, DEGENERATE = "PRESENT", "MISSING", "DEGENERATE"


class Result:
    def __init__(self):
        self.rows = []

    def add(self, rq, name, status, value="", note=""):
        self.rows.append((rq, name, status, value, note))

    def count(self, status):
        return sum(1 for r in self.rows if r[2] == status)


def _session_date(path: str) -> "str | None":
    """YYYY-MM-DD from the manifest filename, or None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
    return m.group(1) if m else None


def _get(d, *path, default=None):
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _grade(value, *, lo=None, hi=None, zero_ok=False):
    """Plausibility grading shared by every numeric metric."""
    if value is None:
        return MISSING, ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return PRESENT, str(value)
    if v == 0 and not zero_ok:
        return DEGENERATE, "0 (implausible)"
    if lo is not None and v < lo:
        return DEGENERATE, "%.3g (below %.3g)" % (v, lo)
    if hi is not None and v > hi:
        return DEGENERATE, "%.3g (above %.3g)" % (v, hi)
    return PRESENT, "%.3g" % v


def check_session(manifest: dict, res: Result) -> None:
    # ── RQ1: validations ──────────────────────────────────────────
    vals = manifest.get("validations") or []
    pre = [v for v in vals if v.get("phase") == "pre"]
    post = [v for v in vals if v.get("phase") == "post"]

    if not pre:
        res.add("RQ1", "accuracy_raw_deg", MISSING, "",
                "no pre-stimulus validation recorded")
    else:
        s, v = _grade(pre[0].get("mean_err_deg"), lo=0.1, hi=20)
        res.add("RQ1", "accuracy_raw_deg", s, v,
                "first pre-validation = the fit set")
        if len(pre) > 1:
            res.add("RQ1", "accuracy_corrected_in_sample_deg", PRESENT,
                    "%.3g" % (pre[-1].get("mean_err_deg") or 0),
                    "%d pre-validations — IN-SAMPLE, do not report as "
                    "accuracy" % len(pre))

    if not post:
        res.add("RQ1", "accuracy_corrected_out_of_sample_deg", MISSING, "",
                "NO POST-STIMULUS VALIDATION — this is the canonical "
                "accuracy figure; the session cannot support an accuracy "
                "claim without it")
    else:
        s, v = _grade(post[-1].get("mean_err_deg"), lo=0.1, hi=20)
        max_deg = SPEC.INCLUSION["max_validation_error_deg"]
        err = post[-1].get("mean_err_deg") or 0
        if s == PRESENT and err > max_deg:
            s = DEGENERATE
        res.add("RQ1", "accuracy_corrected_out_of_sample_deg", s, v,
                "FAILS the %.1f deg inclusion criterion" % max_deg
                if err > max_deg else "canonical accuracy for this session")

    src = post or pre
    if src:
        s, v = _grade(src[-1].get("mean_precision_px"), lo=0.5, hi=500)
        res.add("RQ1", "precision_px", s, v)
        targets = src[-1].get("targets") or []
        errs = [t.get("err_px") for t in targets if t.get("err_px") is not None]
        if len(errs) >= 5:
            res.add("RQ1", "per_target_error_px", PRESENT,
                    "%d targets" % len(errs),
                    "spread %.0f-%.0f px" % (min(errs), max(errs)))
        else:
            res.add("RQ1", "per_target_error_px",
                    MISSING if not errs else DEGENERATE,
                    "%d targets" % len(errs), "expected 7")

    if pre and post:
        d = (post[-1].get("mean_err_deg") or 0) - (pre[0].get("mean_err_deg") or 0)
        res.add("RQ1", "drift_deg", PRESENT, "%+.2f" % d,
                "post − pre; confirm it uses the UNCORRECTED basis")
    else:
        res.add("RQ1", "drift_deg", MISSING, "", "needs both validations")

    # ── RQ1: per-stimulus quality ─────────────────────────────────
    quality = manifest.get("data_quality") or {}
    if not quality:
        for n in ("sampling_hz_empirical", "gaze_samples_pct", "detected_pct"):
            res.add("RQ1", n, MISSING, "", "no data_quality block")
    for stim, q in quality.items():
        tag = "[%s]" % str(stim)[:18]
        # A value that exists but fails a PRE-REGISTERED INCLUSION
        # CRITERION is not "present" — the session is excludable. Grading
        # it OK with a footnote is how a failing session gets analysed by
        # accident.
        min_hz = SPEC.INCLUSION["min_sampling_hz"]
        min_pct = SPEC.INCLUSION["min_gaze_samples_pct"]
        s, v = _grade(q.get("sampling_hz"), lo=5, hi=120)
        hz_val = q.get("sampling_hz") or 0
        if s == PRESENT and hz_val < min_hz:
            s = DEGENERATE
        res.add("RQ1", "sampling_hz_empirical " + tag, s, v,
                "FAILS the %.0f Hz inclusion criterion" % min_hz
                if hz_val < min_hz else "")
        s, v = _grade(q.get("gaze_samples_pct"), lo=1, hi=100.01)
        pct_val = q.get("gaze_samples_pct") or 0
        if s == PRESENT and pct_val < min_pct:
            s = DEGENERATE
        res.add("RQ1", "gaze_samples_pct " + tag, s, v,
                "FAILS the %.0f %% inclusion criterion" % min_pct
                if pct_val < min_pct else "")
        s, v = _grade(q.get("valid_pct"), lo=1, hi=100.01)
        res.add("RQ1", "detected_pct " + tag, s, v)

    # ── RQ1: recording conditions ─────────────────────────────────
    gate = manifest.get("rate_gate") or {}
    pm = gate.get("perf_mode") or _get(manifest, "telemetry", "perf_mode")
    if pm is None:
        res.add("RQ1", "perf_mode_active", MISSING, "",
                "not recorded — cannot confirm the session was not "
                "scheduled onto efficiency cores")
    else:
        active = "ACTIVE" in str(pm)
        res.add("RQ1", "perf_mode_active", PRESENT if active else DEGENERATE,
                "ACTIVE" if active else "NOT ACTIVE",
                "" if active else "sampling rate may be ~half of nominal")
    fs = _get(gate, "stages", "frame_size")
    res.add("RQ1", "frame_size", PRESENT if fs else MISSING, fs or "",
            "" if not fs or fs == "640x480" else "larger than 640x480")
    hd = _get(manifest, "head_position", "distance_cm")
    s, v = _grade(hd, lo=25, hi=120)
    res.add("RQ1", "head_distance_cm", s, v,
            "assumed 60 cm used if missing" if s == MISSING else "")

    # ── RQ2: events ───────────────────────────────────────────────
    fixes = manifest.get("fixations") or {}
    if not fixes:
        for n, _l, _u, status, _w in SPEC.RQ2_EVENTS:
            if status == "collected":
                res.add("RQ2", n, MISSING, "",
                        "fixations are computed by quality_report.py but "
                        "not stored in the manifest — see recommendation")
    # AOI + saccades are computed by aoi_metrics.py; they are only in the
    # manifest if the analysis step has been run and written back.
    for name in ("saccade_count", "saccade_amplitude_median_deg"):
        res.add("RQ2", name,
                PRESENT if manifest.get("saccades") else MISSING, "",
                "aoi_metrics.saccade_metrics() exists; wire it into the "
                "analysis step" if not manifest.get("saccades") else "")
    aoi = manifest.get("aoi") or {}
    for name in ("aoi_dwell_proportion", "aoi_dwell_time_s",
                 "aoi_first_entry_s", "aoi_revisits", "aoi_coverage_pct"):
        res.add("RQ2", name, PRESENT if aoi else MISSING, "",
                "no AOI definition for this stimulus (data/aoi/<stem>.json)"
                if not aoi else "")

    # I-DT parameters, and whether the minimum duration is defensible.
    thr = manifest.get("quality_thresholds") or {}
    hz_vals = [q.get("sampling_hz") for q in quality.values()
               if q.get("sampling_hz")]
    hz = min(hz_vals) if hz_vals else None
    mind = thr.get("fixation_min_duration_s")
    if mind is None:
        res.add("RQ2", "idt_min_duration_s", MISSING, "",
                "record the I-DT parameters in the manifest")
    elif hz:
        floor = 3.0 / hz
        ok = mind >= floor
        res.add("RQ2", "idt_min_duration_s", PRESENT if ok else DEGENERATE,
                "%.0f ms" % (mind * 1000),
                "3-sample floor at %.1f Hz is %.0f ms" % (hz, floor * 1000))
    res.add("RQ2", "idt_dispersion_threshold_deg",
            PRESENT if thr.get("fixation_dispersion_deg") else MISSING, "",
            "stored normalised (screen-dependent); record in degrees")

    # ── RQ3 ───────────────────────────────────────────────────────
    llm = manifest.get("llm") or {}
    res.add("RQ3", "llm_model_id", PRESENT if llm.get("model") else MISSING,
            llm.get("model", ""))
    res.add("RQ3", "llm_claims_structured",
            PRESENT if llm.get("feedback") else MISSING, "")
    res.add("RQ3", "claim_metric_correspondence", MISSING, "",
            "not implemented — this is the operationalisation of RQ3")


def report(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    res = Result()
    check_session(manifest, res)

    print("=" * 78)
    print("  METRIC VERIFICATION — %s" % os.path.basename(path))
    print("=" * 78)
    print("  participant : %s" % manifest.get("participant_id"))
    print("  finalised   : %s" % manifest.get("finalized_at_utc"))
    print()
    cur = None
    for rq, name, status, value, note in res.rows:
        if rq != cur:
            print("\n  ── %s ──" % rq)
            cur = rq
        mark = {PRESENT: "OK  ", MISSING: "MISS", DEGENERATE: "BAD "}[status]
        line = "   [%s] %-42s %s" % (mark, name[:42], value)
        print(line)
        if note:
            for chunk in _wrap(note, 62):
                print("          %s" % chunk)

    print()
    print("-" * 78)
    print("  %d present · %d missing · %d degenerate"
          % (res.count(PRESENT), res.count(MISSING), res.count(DEGENERATE)))

    blocking = [r for r in res.rows
                if r[2] == DEGENERATE
                or (r[2] == MISSING and r[1].startswith(
                    ("accuracy_corrected_out", "sampling_hz", "gaze_samples")))]
    if blocking:
        print()
        print("  WOULD BLOCK AN ACCURACY OR QUALITY CLAIM:")
        for r in blocking:
            print("    - %s %s" % (r[1], ("(%s)" % r[3]) if r[3] else ""))
    return 1 if blocking else 0


def _wrap(text, width):
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
    ap.add_argument("path", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--today", action="store_true",
                    help="only sessions recorded today")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="only sessions on or after this date")
    ap.add_argument("--spec", action="store_true",
                    help="print the specification and exit")
    args = ap.parse_args()

    if args.spec:
        ppd = SPEC.px_per_degree()
        print("1 deg = %.0f px. Accuracy threshold %.1f deg -> AOIs must be "
              ">= %.0f px." % (ppd, SPEC.INCLUSION["max_validation_error_deg"],
                               SPEC.min_aoi_px(
                                   SPEC.INCLUSION["max_validation_error_deg"])))
        print()
        for rq, items in SPEC.ALL.items():
            print("%s" % rq)
            for name, level, unit, status, why in items:
                print("  %-9s %-42s %-14s %s" % (status.upper(), name,
                                                 level, unit))
        print()
        print(SPEC.summary())
        return 0

    files = sorted(glob.glob(os.path.join(RAW_DIR, "*_manifest.json")))
    if args.path:
        files = [args.path]
    else:
        # Filenames carry the date: <participant>_YYYY-MM-DD_HHMMSS.
        cutoff = args.since
        if args.today:
            cutoff = datetime.now().strftime("%Y-%m-%d")
        if cutoff:
            files = [f for f in files if _session_date(f)
                     and _session_date(f) >= cutoff]
            if not files:
                print("No sessions on or after %s." % cutoff)
                return 1
        elif not args.all and files:
            files = files[-1:]
    if not files:
        print("No manifests found in %s" % RAW_DIR)
        return 1

    worst = 0
    for f in files:
        worst = max(worst, report(f))
        print()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
