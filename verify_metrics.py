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

import metrics_spec as SPEC  # noqa: E402

PRESENT, MISSING, DEGENERATE = "PRESENT", "MISSING", "DEGENERATE"
#: A metric this study deliberately does not collect. Reporting it as
#: MISSING treats a design decision as an omission — and five phantom
#: gaps in every report is how a reader stops reading the gaps.
NOT_APPLICABLE = "N/A"


class Result:
    def __init__(self):
        self.rows = []

    def add(self, rq, name, status, value="", note=""):
        # A metric can be checked by two code paths — once from the
        # events block and once from a module that has not been wired
        # in. Reporting it PRESENT and then MISSING makes the count
        # meaningless: the 2026-08-11 report listed saccade_count,
        # saccade_amplitude_median_deg and idt_min_duration_s in both
        # columns at once. If it was found, it is not missing.
        base = str(name).split(" [")[0]
        if status == MISSING and any(
                str(n).split(" [")[0] == base and s == PRESENT
                for _rq, n, s, _v, _n in self.rows):
            return
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

    Read from the session date against config.EVALUATION_FROM_DATE. The
    boundary lives in config precisely so it cannot be decided per
    session, after the numbers are in — which is the difference between
    a pre-registration and a rationalisation.
    """
    try:
        import config
    except Exception:  # noqa: BLE001
        return None, ""

    start = (getattr(config, "EVALUATION_FROM_DATE", "") or "").strip()
    if not start:
        return True, ("DEVELOPMENT — collection has not started. Every "
                      "session so far exists to build and debug the "
                      "pipeline and counts toward nothing. Set "
                      "EVALUATION_FROM_DATE in config.py on the first "
                      "real collection day.")

    # Ask config, do NOT compare date strings.
    # The boundary carries a TIME on the first day, and "2026-08-11" <
    # "2026-08-11T14:00" is true as a string — so a session recorded at
    # 14:30 that day would have been filed as development. The routing
    # that decides which FOLDER a session is written to already uses
    # the real comparison; the label must use the same one or the two
    # disagree.
    session_id = os.path.basename(path).replace("_manifest.json", "")
    date = _session_date(path) or "?"
    if config.is_evaluation_session(session_id):
        return False, "EVALUATION session (recorded %s)" % date
    return True, ("DEVELOPMENT — recorded %s, before collection started "
                  "at %s. Do not pool with evaluation sessions."
                  % (date, start))


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
        # Out of sample in BOTH space and time. Reported, but NOT the
        # figure the inclusion criterion is applied to — see below.
        s, v = _grade(chk[0].get("mean_err_deg"), lo=0.1, hi=20)
        res.add("RQ1", "accuracy_corrected_out_of_sample_deg", s, v,
                "grid B before the stimuli — the correction was never "
                "fitted to these positions")
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
                "grid B after the stimuli")

    # ── THE SIGNED BIAS ──────────────────────────────────────────────
    # An accuracy figure is a mean UNSIGNED distance and cannot tell 60 px
    # of scatter from 60 px of uniform upward displacement. Scatter
    # averages out of any aggregate; a displacement moves every gaze point
    # the same way, so region assignment, correspondence with the model's
    # boxes and the ambiguity rate all inherit it. A participant reported
    # this study's before any metric did (F30), because no quantity
    # reported anywhere was signed.
    #
    # Only the grid-B phases are reported. On the fit grid the
    # post-correction bias is zero by construction — least squares zeroes
    # its own mean residual — and printing an algebraic identity next to
    # measurements is how it gets mistaken for one.
    for _v in (chk or []) + (post or []):
        _ph = _v.get("phase")
        if _v.get("bias_px") is None:
            res.add("RQ1", "signed_bias_%s_px" % _ph, MISSING, "",
                    "recorded before the signed bias was reported "
                    "(2026-08-17) — re-derive with correction_audit.py")
            continue
        _dom = bool(_v.get("offset_dominated"))
        res.add("RQ1", "signed_bias_%s_px" % _ph,
                DEGENERATE if _dom else PRESENT,
                "%.0f px (%+.0f, %+.0f)" % (_v["bias_px"],
                                            _v.get("bias_x_px") or 0,
                                            _v.get("bias_y_px") or 0),
                ("OFFSET-DOMINATED: %.0f %% of the error is a fixed "
                 "displacement — %s. Every spatial claim from this "
                 "recording carries it."
                 % (100 * (_v.get("bias_ratio") or 0),
                    _v.get("bias_direction") or "direction unrecorded"))
                if _dom else
                "the error is mostly scatter, which averages out of an "
                "aggregate")

    # ── HOW MUCH ONE TARGET MOVED THE FIGURE ─────────────────────────
    # Accuracy is a mean over seven targets with no rejection rule. One
    # target measured 649 px from its mark took a real session's post
    # check from 2.41 deg on the median to 3.52 deg on the mean — the
    # difference between passing and failing the 3.0 deg bar. Both are
    # reported so the leverage is visible rather than silent. No rule is
    # applied here: which figure the inclusion criterion uses is a
    # pre-registration decision, not something this report may make.
    for _v in (chk or []) + (post or []):
        _mean, _med = _v.get("mean_err_px"), _v.get("median_err_px")
        if not (_mean and _med):
            continue
        _lev = abs(_mean - _med) / _mean
        res.add("RQ1", "outlier_leverage_%s" % _v.get("phase"),
                DEGENERATE if _lev > 0.25 else PRESENT,
                "mean %.0f px vs median %.0f px" % (_mean, _med),
                "one or two of the seven targets carry the mean, and there "
                "is no rejection rule — state which figure a claim uses"
                if _lev > 0.25 else
                "mean and median agree; no single target dominates")

    # ── WHICH RULER IS THE INCLUSION FIGURE MEASURED WITH? ───────────
    # Every degree below comes from `mean_err_deg`, which the BROWSER
    # computed by dividing by window.measuredDistanceCm — in practice a
    # hardcoded 60 cm (F21). The server recomputes the same error from
    # the distance measured at validation time and stores it as
    # `mean_err_deg_measured`, and metrics_spec already calls that one
    # authoritative: "the browser's own figure used a hardcoded 60 cm and
    # is retained only for comparison".
    #
    # The applied rule and the declared rule are therefore reading
    # different numbers. Across the recorded sessions they differ by
    # -22 % to +15 %, in whichever direction the participant sat relative
    # to 60 cm. No verdict has flipped yet; one will.
    #
    # This does NOT switch the figure. Which ruler the criterion uses is
    # a pre-registration decision and must be made deliberately and
    # dated, not changed inside a report (F34).
    _pairs_deg = []
    for _v in (chk or []) + (post or []):
        _b, _m = _v.get("mean_err_deg"), _v.get("mean_err_deg_measured")
        if _b and _m:
            _pairs_deg.append((_v.get("phase"), _b, _m))
    if _pairs_deg:
        _bm = sum(b for _, b, _m in _pairs_deg) / len(_pairs_deg)
        _mm = sum(_m for _, _b, _m in _pairs_deg) / len(_pairs_deg)
        _thr = SPEC.INCLUSION["max_validation_error_deg"]
        _disagree = (_bm > _thr) != (_mm > _thr)
        _gap = abs(_mm - _bm) / _bm if _bm else 0
        res.add("RQ1", "accuracy_ruler",
                DEGENERATE if (_disagree or _gap > 0.05) else PRESENT,
                "browser %.2f vs measured %.2f deg" % (_bm, _mm),
                ("THE TWO RULERS DISAGREE ABOUT THE THRESHOLD — this "
                 "session passes on one and fails on the other. Which "
                 "ruler the criterion uses must be settled and dated "
                 "before this session is admitted or excluded."
                 if _disagree else
                 "the two differ by %.0f %%. The figure reported above "
                 "uses the BROWSER's assumed distance; metrics_spec "
                 "calls the measured one authoritative (F34)." % (100 * _gap)
                 if _gap > 0.05 else
                 "browser and measured distance agree to within 5 %"))

    # ── WAS THE CORRECTION APPLIED, AND WHY ──────────────────────────
    _dec = (manifest.get("correction_decision") or {})
    if _dec:
        res.add("RQ1", "correction_decision", PRESENT,
                _dec.get("chosen", "?"),
                "%s | rule fixed %s" % (_dec.get("reason", ""),
                                        _dec.get("rule_fixed_on", "?")))
    elif manifest.get("validations"):
        res.add("RQ1", "correction_decision", MISSING, "",
                "recorded before the correction was cross-validated "
                "(2026-08-17). The correction on this session was applied "
                "unconditionally — run correction_audit.py to see what the "
                "current rule would have chosen and whether it differs.")

    # ── THE INCLUSION FIGURE ─────────────────────────────────────────
    # The mean of the two grid-B checks, one either side of the
    # recording.
    #
    # The accuracy the STIMULUS data was recorded at is not measured
    # directly — it is bracketed by a check before and a check after.
    # Taking either end alone answers a question nobody asked: pre_check
    # is the accuracy at the moment recording started, post is the
    # accuracy once it finished, and the data sits between them. Their
    # mean is the best single estimate of the thing the criterion is
    # about.
    #
    # Both ends are out of sample on grid B, so neither flatters the
    # correction, and the mean cannot be gamed by choosing the kinder
    # end — which is the reason to fix the rule in advance rather than
    # per session.
    if chk and post:
        a = chk[0].get("mean_err_deg")
        b = post[-1].get("mean_err_deg")
        if a is not None and b is not None:
            incl = (float(a) + float(b)) / 2.0
            max_deg = SPEC.INCLUSION["max_validation_error_deg"]
            res.add("RQ1", "accuracy_for_inclusion_deg",
                    DEGENERATE if incl > max_deg else PRESENT,
                    "%.2f" % incl,
                    ("FAILS the %.1f deg criterion (pre_check %.2f, post "
                     "%.2f)" % (max_deg, a, b)) if incl > max_deg else
                    ("mean of pre_check %.2f and post %.2f — the "
                     "recording sits between them" % (a, b)))
    else:
        res.add("RQ1", "accuracy_for_inclusion_deg", MISSING, "",
                "needs BOTH a pre_check and a post on grid B")

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
            dist.get("from_phase") or "?",
            dist.get("source") or "UNKNOWN RULER")
        if dist.get("iris_error"):
            # Not fatal, but it means the fallback ruler was used and
            # the reader should know which one produced the number
            # every degree in the session divides by.
            note += " [iris unavailable: %s]" % str(dist["iris_error"])[:60]
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
    # AOI metrics are NOT MISSING — this study deliberately draws no
    # AOIs. metrics_spec.NOT_APPLICABLE records the reason for each, so
    # the decision is auditable instead of looking like an oversight.
    aoi = manifest.get("aoi") or {}
    _why = dict(SPEC.NOT_APPLICABLE)
    for name in ("aoi_dwell_proportion", "aoi_dwell_time_s",
                 "aoi_first_entry_s", "aoi_revisits", "aoi_coverage_pct"):
        if aoi:
            res.add("RQ2", name, PRESENT, "", "")
        else:
            res.add("RQ2", name, NOT_APPLICABLE, "",
                    "by design: no hand-drawn AOIs — %s"
                    % _why.get(name, "see metrics_spec.NOT_APPLICABLE"))

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
    # The dispersion threshold IS recorded in degrees, per stimulus, in
    # the events block — `idt_dispersion_deg`. This looked for it under
    # quality_thresholds and reported MISSING while the value sat in the
    # manifest, which is the same lookup mismatch that hid the RQ2
    # metrics and the viewing distance.
    _disp = [(stim, blk.get("idt_dispersion_deg"))
             for stim, blk in events.items()
             if isinstance(blk, dict) and blk.get("idt_dispersion_deg")]
    if _disp:
        for stim, val in _disp:
            res.add("RQ2", "idt_dispersion_threshold_deg [%s]" % str(stim)[:18],
                    PRESENT, "%.2f" % float(val),
                    "converted from the normalised %.3g using this "
                    "session's own geometry"
                    % (thr.get("fixation_dispersion_norm")
                       or _get(manifest, "quality_thresholds",
                               "fixation_dispersion_norm") or 0.05))
    elif thr.get("fixation_dispersion_deg"):
        res.add("RQ2", "idt_dispersion_threshold_deg", PRESENT,
                "%.2f" % float(thr["fixation_dispersion_deg"]))
    else:
        res.add("RQ2", "idt_dispersion_threshold_deg", MISSING, "",
                "not in manifest['events'] — was the session finalised?")

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
            # BOTH rates, always. The strict one counts only claims whose
            # box CONTAINS the gaze; the lenient one adds claims that
            # miss by less than the session's own measurement error. A
            # strict figure quoted alone reads as "the model was wrong
            # 83 % of the time" when much of that gap is the tracker's
            # error, and a lenient figure alone assumes every near miss
            # was really a hit. Neither is defensible without the other,
            # so the report never shows one without the other.
            lenient = corr.get("correspondence_lenient_pct")
            val = "%.1f %% of %d" % (pct, testable)
            if lenient is not None:
                val = "%.1f %% strict / %.1f %% lenient of %d" % (
                    pct, lenient, testable)
            res.add("RQ3", "claim_metric_correspondence %s" % tag, PRESENT,
                    val,
                    "strict = gaze inside the box; lenient adds misses "
                    "smaller than this session's error. Tolerance from "
                    "the %s"
                    % (corr.get("accuracy_source") or "validation"))

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


def rubric_drift(paths: list) -> dict:
    """Is every evaluation session carrying the SAME rubric string?

    A rubric that changes mid-collection splits the data into two
    studies, and the change is invisible: each session looks fine on its
    own, `criteria_met` is populated throughout, and κ is computed over
    judgments made to two different standards. The manifest already
    stores the rubric text (app.py writes it per session), so the check
    costs nothing — it was simply never made.

    Text, not a hash. A hash tells you that something moved; the text
    tells you what it moved to, which is what you need in order to
    decide whether the sessions can still be pooled.
    """
    import config

    seen: dict = {}
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                man = json.load(fh)
        except (OSError, ValueError):
            continue
        if not config.is_evaluation_session(os.path.basename(p)):
            continue
        for stim, blk in (man.get("llm") or {}).items():
            if not isinstance(blk, dict):
                continue
            text = (blk.get("rubric") or "").strip()
            seen.setdefault(text, []).append(
                "%s [%s]" % (os.path.basename(p), stim))
    return seen


def _report_rubric(paths: list) -> int:
    variants = rubric_drift(paths)
    print("=" * 78)
    print("  RUBRIC FREEZE CHECK — evaluation sessions only")
    print("=" * 78)
    if not variants:
        print("  No evaluation session carries an LLM run yet.")
        return 0
    empty = variants.pop("", None)
    if empty:
        print("  %d run(s) with NO rubric at all:" % len(empty))
        for s in empty[:8]:
            print("      %s" % s)
        print("  criteria_met is null for these; they cannot enter kappa.")
        print()
    if not variants:
        return 1
    if len(variants) == 1:
        text = next(iter(variants))
        print("  OK — one rubric across %d run(s), %d characters."
              % (len(next(iter(variants.values()))), len(text)))
        print()
        print("  " + (text[:200] + ("…" if len(text) > 200 else "")))
        return 0
    print("  DRIFT — %d DIFFERENT rubrics are in use." % len(variants))
    print("  Judgments made to different standards cannot be pooled;")
    print("  kappa over them is not an estimate of anything.")
    for i, (text, where) in enumerate(sorted(
            variants.items(), key=lambda kv: -len(kv[1])), 1):
        print()
        print("  variant %d — %d run(s), %d characters"
              % (i, len(where), len(text)))
        for s in where[:5]:
            print("      %s" % s)
        print("      \"%s…\"" % text[:120])
    return 1


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
        # .get, not [], and every status listed. A bare lookup here
        # crashed the whole report the first time an N/A row reached it:
        # the metrics were all computed correctly and none of them were
        # printed, because one renderer did not know about a status the
        # rest of the file had used for days.
        mark = {PRESENT: "OK  ", MISSING: "MISS", DEGENERATE: "BAD ",
                NOT_APPLICABLE: "n/a "}.get(status, "????")
        line = "   [%s] %-42s %s" % (mark, name[:42], value)
        print(line)
        if note:
            for chunk in _wrap(note, 62):
                print("          %s" % chunk)

    print()
    print("-" * 78)
    print("  %d present · %d missing · %d degenerate · %d n/a by design"
          % (res.count(PRESENT), res.count(MISSING), res.count(DEGENERATE),
             res.count(NOT_APPLICABLE)))
    # Name what is actually outstanding. A count alone sends you looking
    # through the whole report for the entries that matter.
    _gaps = [r[1] for r in res.rows if r[2] == MISSING]
    if _gaps:
        print("  still missing: %s" % ", ".join(_gaps))

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
    ap.add_argument("--rubric", action="store_true",
                    help="check every evaluation session carries the SAME "
                         "rubric, and exit")
    args = ap.parse_args()

    if args.rubric:
        return _report_rubric(_session_glob())

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

    files = _session_glob()
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
