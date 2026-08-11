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


def pilot_status(path: str) -> "tuple":
    """Is this session pilot data or evaluation data?

    Read from the session date against config.PILOT_BEFORE_DATE. The
    boundary lives in config precisely so it cannot be decided per
    session, after the numbers are in — which is the difference between
    a pre-registration and a rationalisation.
    """
    try:
        import config

        cutoff = getattr(config, "PILOT_BEFORE_DATE", None)
    except Exception:  # noqa: BLE001
        cutoff = None
    date = _session_date(path)
    if not cutoff or not date:
        return None, ""
    if date < cutoff:
        return True, ("PILOT — recorded %s, before the %s boundary. "
                      "Proof of concept: the protocol was still changing. "
                      "Do not pool with evaluation sessions."
                      % (date, cutoff))
    return False, "evaluation session (recorded %s)" % date


def check_session(manifest: dict, res: Result) -> None:
    # ── RQ1: validations ──────────────────────────────────────────
    vals = manifest.get("validations") or []
    # "pre", "pre_fit" and "pre_check" are all pre-stimulus checks.
    pre = [v for v in vals if str(v.get("phase") or "").startswith("pre")]
    post = [v for v in vals if v.get("phase") == "post"]

    # The two-grid protocol: pre_fit (grid A, uncorrected, the fit set)
    # then pre_check (grid B, corrected, positions never fitted to).
    fit = [v for v in pre if v.get("phase") in ("pre_fit", "pre")]
    chk = [v for v in pre if v.get("phase") == "pre_check"]

    if not fit:
        res.add("RQ1", "accuracy_raw_deg", MISSING, "",
                "no pre-stimulus validation recorded")
    else:
        s, v = _grade(fit[0].get("mean_err_deg"), lo=0.1, hi=20)
        res.add("RQ1", "accuracy_raw_deg", s, v,
                "grid A, uncorrected — the tracker's native accuracy")
        # More than one attempt at the SAME phase is a protocol
        # deviation. It is not fatal, but it must be visible: choosing
        # between attempts after seeing them is optimisation on the
        # primary outcome, and it is the first thing an examiner probes.
        if len(fit) > 1:
            res.add("RQ1", "validation_attempts", DEGENERATE,
                    "%d fit attempts" % len(fit),
                    "PROTOCOL DEVIATION — the rule is ONE attempt per "
                    "phase; the FIRST is canonical. Do not select the "
                    "best.")

    if chk:
        # Out of sample in BOTH space and time. This is the defensible
        # corrected accuracy.
        s, v = _grade(chk[0].get("mean_err_deg"), lo=0.1, hi=20)
        max_deg = SPEC.INCLUSION["max_validation_error_deg"]
        err = chk[0].get("mean_err_deg") or 0
        if s == PRESENT and err > max_deg:
            s = DEGENERATE
        res.add("RQ1", "accuracy_corrected_out_of_sample_deg", s, v,
                "FAILS the %.1f deg inclusion criterion" % max_deg
                if err > max_deg
                else "grid B — canonical corrected accuracy (the "
                     "correction was never fitted to these positions)")
    elif len(fit) > 1:
        # Legacy sessions: a repeated pre at the SAME grid. The samples
        # are new but the positions are the fit's own, so this is not a
        # generalisation estimate and must not be reported as accuracy.
        res.add("RQ1", "accuracy_corrected_in_sample_deg", DEGENERATE,
                "%.3g" % (fit[-1].get("mean_err_deg") or 0),
                "re-measured at the FIT positions — IN-SAMPLE, not a "
                "corrected-accuracy claim. Re-record with the pre_fit / "
                "pre_check protocol.")
        res.add("RQ1", "accuracy_corrected_out_of_sample_deg", MISSING, "",
                "no pre_check on an unseen grid")
    else:
        res.add("RQ1", "accuracy_corrected_out_of_sample_deg", MISSING, "",
                "no pre_check recorded")

    if not post:
        res.add("RQ1", "accuracy_post_stimulus_deg", MISSING, "",
                "NO POST-STIMULUS VALIDATION — without it there is no "
                "drift estimate and no evidence the tracking held for "
                "the duration of the recording")
    else:
        s, v = _grade(post[-1].get("mean_err_deg"), lo=0.1, hi=20)
        res.add("RQ1", "accuracy_post_stimulus_deg", s, v,
                "grid B after the stimuli — the accuracy during "
                "recording lies between this and the pre_check")

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
        # DRIFT MUST BE DIFFERENCED ON ONE BASIS.
        # This subtracted a RAW pre from a CORRECTED post and reported
        # the result as drift: on the 2026-08-10 session that gave
        # -2.39 deg while the review page, differencing the uncorrected
        # pair, gave +0.51. Same session, opposite sign, and the -2.39
        # is not drift at all — it is mostly the gain correction's
        # effect, which was fitted on the pre targets and so cannot
        # legitimately appear in a before/after comparison.
        #
        # Drift means "did tracking degrade over the session", so both
        # ends must be measured the same way. mean_err_deg_raw is the
        # uncorrected figure recorded for exactly this purpose.
        a = pre[-1].get("mean_err_deg_raw")
        b = post[-1].get("mean_err_deg_raw")
        basis = "uncorrected (both ends)"
        if a is None or b is None:
            a = pre[-1].get("mean_err_deg")
            b = post[-1].get("mean_err_deg")
            basis = ("as-reported — MIXED correction, not comparable; "
                     "re-run with mean_err_deg_raw recorded")
        if a is None or b is None:
            res.add("RQ1", "drift_deg", MISSING, "", "no comparable pair")
        else:
            status = PRESENT if basis.startswith("uncorrected") else DEGENERATE
            res.add("RQ1", "drift_deg", status, "%+.2f" % (b - a), basis)
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
    # The MANDATORY validation measures this; the optional position
    # guide is only a fallback. Reading head_position.distance_cm meant
    # reading a block only the guide fills, under a key the guide does
    # not even write ("est_distance_cm") — so this reported MISSING on
    # every session while the correct value sat in the manifest.
    dist = manifest.get("distance") or {}
    hd = (dist.get("cm")
          or _get(manifest, "head_position", "est_distance_cm")
          or _get(manifest, "head_position", "distance_cm"))
    s, v = _grade(hd, lo=25, hi=120)
    note = ""
    if s == MISSING:
        note = ("NOT MEASURED — every degree in this session divides by "
                "the assumed %s cm" % (dist.get("assumed_cm") or 60))
    else:
        note = "measured at the %s check via %s" % (
            dist.get("from_phase") or "?", dist.get("source") or "?")
        if dist.get("estimates_agree") is False:
            s = DEGENERATE
            note += " — iris and pupil estimates DISAGREE"
    res.add("RQ1", "head_distance_cm", s, v, note)

    # ── RQ2: events ───────────────────────────────────────────────
    # app.py writes these per stimulus under manifest["events"] (from
    # counts["__events__"]). This block looked for manifest["fixations"],
    # which nothing ever writes — so every RQ2 metric reported MISSING
    # on sessions that had computed all of them and printed them to the
    # log at finalisation. A verifier that cannot find data it already
    # has is worse than none: it sends you to re-collect.
    events = manifest.get("events") or manifest.get("fixations") or {}
    # Saccade figures are nested one level deeper.
    def _event(stim_block: dict, name: str):
        if name in stim_block:
            return stim_block[name]
        sacc = stim_block.get("saccades") or {}
        if name == "saccade_amplitude_median_deg":
            return sacc.get("amplitude_median_deg")
        return sacc.get(name)

    for n, _l, _u, status, _w in SPEC.RQ2_EVENTS:
        if status != "collected":
            continue
        found = [(stim, _event(blk, n)) for stim, blk in events.items()
                 if isinstance(blk, dict) and _event(blk, n) is not None]
        if not found:
            res.add("RQ2", n, MISSING, "",
                    "not in manifest['events'] — was the session "
                    "finalised? (events are written at finalisation)")
            continue
        for stim, val in found:
            res.add("RQ2", "%s [%s]" % (n, stim), PRESENT, val, "")
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
    # manifest["llm"] is keyed by stimulus: one feedback run per video.
    llm = manifest.get("llm") or {}
    blocks = [(k, v) for k, v in llm.items() if isinstance(v, dict)]
    if not blocks:
        for n in ("llm_model_id", "llm_claims_structured",
                  "claim_metric_correspondence"):
            res.add("RQ3", n, MISSING, "",
                    "no feedback run recorded for any stimulus — open the "
                    "review tool and generate it")
    for stim, blk in blocks:
        tag = "[%s]" % str(stim)[:18]
        res.add("RQ3", "llm_model_id %s" % tag,
                PRESENT if blk.get("llm_model_id") else MISSING,
                blk.get("llm_model_id", ""),
                "pin this exact string in the methods section")

        claims = blk.get("structured") or []
        # Claims with no bbox cannot be scored, so a run that produced
        # only unlocalised claims has not operationalised RQ3 even
        # though it produced text.
        boxed = [c for c in claims
                 if isinstance(c, dict) and c.get("bbox")]
        st = PRESENT if boxed else (DEGENERATE if claims else MISSING)
        res.add("RQ3", "llm_claims_structured %s" % tag, st,
                "%d claims, %d localised" % (len(claims), len(boxed)),
                "" if boxed else
                "claims carry no bbox — nothing to check against the gaze")

        corr = blk.get("correspondence") or {}
        pct = corr.get("correspondence_pct")
        testable = corr.get("n_testable") or 0
        if pct is None:
            res.add("RQ3", "claim_metric_correspondence %s" % tag, MISSING,
                    "", corr.get("error") or "not scored")
        elif testable < 10:
            # A proportion over a handful of units is not an estimate.
            res.add("RQ3", "claim_metric_correspondence %s" % tag,
                    DEGENERATE, "%.1f %%" % pct,
                    "only %d testable claims — too few to report as a "
                    "rate" % testable)
        else:
            res.add("RQ3", "claim_metric_correspondence %s" % tag, PRESENT,
                    "%.1f %% of %d" % (pct, testable),
                    "scored against the recorded gaze, tolerance from the "
                    "%s" % (corr.get("accuracy_source") or "validation"))

        # The evaluative half of RQ3. With no rubric the prompt tells
        # the model to return criteria_met: null, so there is no
        # judgment for a human coder to agree or disagree with and
        # Cohen's kappa is undefined.
        judged = [c for c in claims
                  if isinstance(c, dict) and c.get("criteria_met") is not None]
        if not blk.get("rubric"):
            res.add("RQ3", "criteria_met %s" % tag, MISSING, "",
                    "NO RUBRIC was supplied, so every criteria_met is null "
                    "and the evaluative half of RQ3 has no data — kappa "
                    "against human coders is undefined")
        else:
            res.add("RQ3", "criteria_met %s" % tag,
                    PRESENT if judged else DEGENERATE,
                    "%d/%d judged" % (len(judged), len(claims)))


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
    is_pilot, why = pilot_status(path)
    if is_pilot is not None:
        print("  status      : %s" % why)
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
