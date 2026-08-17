# -*- coding: utf-8 -*-
"""
The authoritative list of metrics this thesis needs, per research question.

WHY A SPEC RATHER THAN A DOCUMENT
---------------------------------
"Which metrics do I collect?" and "did I actually collect them?" are the
same question asked twice, and they drift apart the moment they live in
different places. This module is the single declaration; verify_metrics.py
checks a real session against it. If a metric is not listed here it is not
required, and if it is listed here a session that lacks it is flagged.

DERIVED THRESHOLDS
------------------
Two numbers are not free parameters and must not be set by taste:

1. **Accuracy threshold vs AOI size.** For a gaze point to be assigned to
   the right AOI, the spatial error must be smaller than half the AOI's
   smallest dimension — otherwise error alone can move a point into a
   neighbouring region. So::

       min AOI dimension (px) >= 2 x accuracy (px)

   At 1920x1080, 15.6", 60 cm: 1 deg = 58 px, so a 3 deg threshold
   admits sessions needing AOIs of >= 350 px (18 % of screen width).
   Individual student FACES (~110 px) would need <= 0.9 deg and are
   therefore NOT supportable at any threshold this pipeline can meet.
   This is a design constraint on the stimulus annotation, not a
   tuning knob.

2. **I-DT minimum duration vs sampling rate.** A dispersion window needs
   at least three samples to be meaningful, so::

       min fixation duration (s) >= 3 / sampling_hz

   At 21 Hz that is 143 ms; at 25 Hz, 120 ms; at 30 Hz, 100 ms. Setting
   it below that floor invents fixations from two points; setting it far
   above discards genuine short fixations and inflates the median. See
   fixation_min_duration_for().

STATUS VALUES
-------------
    "collected"  produced today, verified by verify_metrics.py
    "derived"    computed on demand from collected data
    "missing"    required by an RQ but NOT yet implemented
"""

from __future__ import annotations

import math

# ── Display geometry used for degree conversions ──────────────────────
# Overridden per session by the recorded manifest values; these are the
# defaults for planning calculations.
SCREEN_W_PX = 1920
SCREEN_H_PX = 1080
SCREEN_DIAG_IN = 15.6
VIEWING_DISTANCE_CM = 60.0


def px_per_degree(w_px: int = SCREEN_W_PX, h_px: int = SCREEN_H_PX,
                  diag_in: float = SCREEN_DIAG_IN,
                  distance_cm: float = VIEWING_DISTANCE_CM) -> float:
    """Pixels subtended by one degree of visual angle."""
    diag_cm = diag_in * 2.54
    w_cm = diag_cm * math.cos(math.atan2(h_px, w_px))
    return (w_px / w_cm) * distance_cm * math.tan(math.radians(1.0))


def min_aoi_px(accuracy_deg: float, **geom) -> float:
    """Smallest AOI dimension that the given accuracy can resolve.

    Factor of two: the error must not be able to carry a gaze point from
    the centre of one AOI across its boundary into another.
    """
    return 2.0 * accuracy_deg * px_per_degree(**geom)


def fixation_min_duration_for(sampling_hz: float,
                              preferred_s: float = 0.10) -> float:
    """I-DT minimum duration, floored by what the sampling rate supports.

    Returns the larger of the preferred value (literature default ~100 ms)
    and the three-sample floor. Reporting BOTH the value used and the
    floor makes the constraint auditable.
    """
    if not sampling_hz or sampling_hz <= 0:
        return preferred_s
    return max(preferred_s, 3.0 / sampling_hz)


# ══════════════════════════════════════════════════════════════════════
#  THE SPEC
#  (name, level, unit, status, why it is needed)
# ══════════════════════════════════════════════════════════════════════

RQ1_QUALITY = [
    ("accuracy_raw_deg", "session", "deg", "collected",
     "Pre-stimulus validation, uncorrected. The tracker's native spatial "
     "accuracy and the fit set for the gain correction."),
    ("accuracy_corrected_out_of_sample_deg", "session", "deg", "collected",
     "Post-stimulus validation with the correction applied. The ONLY "
     "unbiased estimate of the accuracy the stimulus data was recorded at."),
    ("accuracy_corrected_in_sample_deg", "session", "deg", "collected",
     "Optional pre re-run. Fit diagnostic ONLY — never report as accuracy."),
    ("precision_px", "session", "px", "collected",
     "Within-target scatter; separates a noisy signal from a biased one."),
    ("drift_deg", "session", "deg", "collected",
     "Post minus pre on the UNCORRECTED basis, so it measures tracking "
     "stability rather than the correction's effect."),
    ("per_target_error_px", "session", "px", "collected",
     "Error at each of the 7 targets. Reveals spatially structured error "
     "(e.g. worse at the bottom of the screen) that a mean conceals."),
    ("signed_bias_px", "session", "px", "collected",
     "SIGNED mean of (measured - target), per axis and as a magnitude, on "
     "the grid-B checks. Accuracy is a mean UNSIGNED distance and reads "
     "identically for 60 px of scatter and 60 px of uniform upward "
     "displacement. The two are not interchangeable: scatter averages out "
     "of any aggregate, an offset moves every gaze point the same way, and "
     "region assignment, correspondence with the model's boxes and the "
     "ambiguity rate all inherit it. A participant reported this study's "
     "before any metric did (F30). NOT reported on the fit grid, where "
     "least squares zeroes its own mean residual and the answer is an "
     "algebraic identity rather than a measurement."),
    ("bias_ratio", "session", "-", "collected",
     "|signed bias| / mean unsigned error. Above 0.5 the error is a "
     "systematic displacement rather than scatter and the mean alone "
     "misdescribes the recording. Both grid-B checks of both sessions "
     "measured so far sit at 0.80-0.96."),
    ("median_validation_error_px", "session", "px", "collected",
     "Median of the 7 per-target errors, reported BESIDE the mean, not "
     "instead of it. The mean has no outlier rule: one target measured "
     "649 px from its mark took a session's post check from 2.41 deg on "
     "the median to 3.52 deg on the mean, across the 3.0 deg inclusion "
     "bar. Which figure the criterion uses is an open pre-registration "
     "decision, deliberately not settled by this spec."),
    ("correction_decision", "session", "json", "collected",
     "Which of {none, affine, quadratic-vertical} was applied, the "
     "leave-one-out figures for each, the criterion, the date the "
     "criterion was fixed, and the rule it replaced. A session whose "
     "correction was fitted and then REJECTED is otherwise "
     "indistinguishable from one where none was ever fitted."),
    ("bias_stability_px", "session", "px", "derived",
     "Change in raw signed bias between consecutive validations, tested "
     "against the target-to-target scatter each mean was estimated from "
     "(correction_audit.py). Measured at 0.9-1.3 deg over 21-40 s in both "
     "sessions carrying per-target records, which is why no session-wise "
     "correction can remove the offset, and why the accuracy claim has to "
     "carry an irreducible directional term (F30)."),
    ("sampling_hz_empirical", "stimulus", "Hz", "collected",
     "Median rate over the recorded segment. Every event metric's "
     "resolution derives from this; the nominal 30 Hz must never be used "
     "in its place."),
    ("gaze_samples_pct", "stimulus", "%", "collected",
     "Tobii-style data loss against the FIXED nominal rate."),
    ("detected_pct", "stimulus", "%", "collected",
     "Frames yielding a gaze estimate. Distinguishes a detection failure "
     "from a speed problem — they need opposite fixes."),
    ("perf_mode_active", "session", "bool", "collected",
     "Whether the OS scheduling opt-out was in force. Halved the rate "
     "when absent, so a session without it is not comparable."),
    ("frame_size", "session", "px", "collected",
     "Delivered camera resolution; drives per-frame cost."),
    ("head_distance_cm", "session", "cm", "collected",
     "MEASURED at validation time from the IRIS (11.7 mm +- 0.5, a "
     "physiological constant) rather than assumed. Every degree figure "
     "divides by it, so it is not a detail: the same pixel error reads "
     "2.97 deg at 45 cm and 1.67 deg at 80 cm."),
    ("distance_estimates_agree", "session", "bool", "collected",
     "Iris vs inter-ocular distance on the same frame. Both share a "
     "focal length, so agreement does not prove correctness — but "
     "DISAGREEMENT proves a measurement is broken (failed landmark fit, "
     "eyeglasses, or head yaw, which foreshortens the IOD but not the "
     "iris). At 35 deg yaw the IOD claims 73 cm for a head at 60."),
    ("mean_err_deg_measured", "session", "deg", "collected",
     "Accuracy recomputed server-side from the measured distance. The "
     "browser's own figure used a hardcoded 60 cm and is retained only "
     "for comparison."),
]

RQ2_EVENTS = [
    ("fixation_count", "stimulus", "n", "collected",
     "SECONDARY metric only — biased down ~3x at 21 Hz. Always report "
     "alongside fixation_rate so the bias is visible."),
    ("fixation_duration_median_ms", "stimulus", "ms", "collected",
     "Biased UP by merging. Report with its quantisation uncertainty."),
    ("fixation_duration_uncertainty_ms", "stimulus", "ms", "collected",
     "One inter-sample interval. This is quantisation, NOT variability."),
    ("fixation_rate_per_s", "stimulus", "1/s", "collected",
     "The honest undersampling indicator: natural viewing is 3-4 /s."),
    ("saccade_count", "stimulus", "n", "collected",
     "Named explicitly in H2. Inter-fixation transitions "
     "(fixations.saccade_metrics)."),
    ("saccade_amplitude_median_deg", "stimulus", "deg", "collected",
     "Displacement between consecutive fixation centroids. Velocity and "
     "duration are NOT recoverable at 21-30 Hz and are not reported."),
    ("gaze_off_video_pct", "stimulus", "%", "collected",
     "Share of gaze outside the video frame — the model cannot describe "
     "what was looked at there, so it bounds every feedback claim."),
    ("idt_dispersion_threshold_deg", "stimulus", "deg", "missing",
     "Currently stored normalised (0.05), which is screen-dependent. "
     "Record in degrees so it is comparable across setups."),
    ("idt_min_duration_s", "stimulus", "s", "collected",
     "Must be reported WITH the three-sample floor it was checked against."),
]

RQ3_FEEDBACK = [
    ("llm_model_id", "session", "str", "collected",
     "Pinned model version; feedback is not reproducible without it."),
    ("llm_claims_structured", "stimulus", "json", "collected",
     "Phases with t_start/t_end/attended/criteria_met/confidence."),
    ("llm_claim_bbox", "stimulus x claim", "norm xywh", "collected",
     "WHERE the model says the attended thing is. Without it a spatial "
     "claim cannot be checked against the gaze, and RQ3 becomes "
     "circular — the model would be both witness and judge."),
    ("claim_correspondence_pct", "stimulus", "%", "collected",
     "THE RQ3 HEADLINE: share of testable claims the gaze data supports "
     "(claim_check.py). Independent of the model, because the gaze "
     "coordinates are a separate measurement."),
    ("claim_testable_pct", "stimulus", "%", "collected",
     "Share of claims that could be checked at all. A low value is "
     "itself a finding: the model is making unlocalisable claims."),
    ("marker_uncertainty_deg", "session", "deg", "collected",
     "The radius drawn on the video AND stated in the prompt. Ties "
     "every feedback claim to the session's measured accuracy."),
    ("human_agreement_kappa", "study", "kappa", "missing",
     "Cohen's kappa on criteria_met against human coders. Tooling "
     "exists (agreement_kit.py); the study has not been run. This is "
     "the remaining RQ3 validity gap."),
]

# NOT APPLICABLE — recorded here so the choice is explicit rather than an
# omission. This project deliberately uses NO hand-drawn AOIs: the gaze
# marker is burned onto the video and a multimodal model identifies the
# content. Fixed AOIs would impose arbitrary binning on dynamic footage,
# and at the achievable accuracy could not resolve individual people
# anyway (a face subtends ~1.9 deg; assignment needs <= 0.9 deg).
NOT_APPLICABLE = [
    ("aoi_dwell_proportion", "superseded by claim_correspondence_pct"),
    ("aoi_dwell_time_s", "no fixed AOI set to dwell in"),
    ("aoi_first_entry_s", "superseded by the model's phase timeline"),
    ("aoi_revisits", "no fixed AOI set to revisit"),
    ("aoi_coverage_pct", "no fixed AOI set exists to cover"),
]
NOT_APPLICABLE_NAMES = {n for n, _ in NOT_APPLICABLE}

ALL = {"RQ1": RQ1_QUALITY, "RQ2": RQ2_EVENTS, "RQ3": RQ3_FEEDBACK}

# Inclusion criteria — the rule that decides which sessions enter the
# analysis. It lives HERE, in code, with a date, because that is what
# makes it checkable: a threshold chosen after seeing which participants
# fail it is not a criterion, it is a result being selected.
#
# The thresholds are in DEGREES, which already contain the viewing
# distance, so the switch from the inter-ocular ruler to the iris ruler
# does not change what 3.0 means. What it changes is the measured
# values: the same recording now reports a LARGER angle, because the
# angle was previously divided by a distance that was too big. Sessions
# measured before that fix are not comparable with these thresholds.
INCLUSION = {
    "max_validation_error_deg": 3.0,
    "min_gaze_samples_pct": 60.0,
    "min_sampling_hz": 20.0,
    # MUST name the same figure verify_metrics computes, or the written
    # rule and the applied rule are two different rules. F6: the
    # recording sits between the two out-of-sample checks, and neither
    # end alone answers what the stimulus data was recorded at.
    "canonical_accuracy": "mean of pre_check and post, both grid B "
                          "(out-of-sample)",
    "decided_on": "2026-08-06",
    "revised_on": "2026-08-11 — canonical figure changed from the "
                  "post-stimulus check alone to the mean of the two "
                  "grid-B checks when the two-grid protocol was "
                  "introduced (F6). Thresholds unchanged.",
    "applies_from": "sessions recorded after EVALUATION_FROM_DATE",
    # Re-affirmed KNOWING it got harder, which is the part that makes it
    # auditable. On 2026-08-11 the distance ruler was corrected from the
    # inter-ocular estimate to the iris; measured angles rise by roughly
    # a quarter, and a development session that read 2.91 deg now reads
    # ~3.76 and would FAIL. The threshold was left at 3.0 anyway, before
    # any evaluation session existed. Geometry: 3.0 deg = 175 px of
    # error, so 349 px is the smallest region the criterion can resolve,
    # and the four rubric regions are separated by far more than that.
    "reaffirmed_on": "2026-08-11 — kept at 3.0 deg after the iris-ruler "
                     "correction made it stricter, not looser; no "
                     "evaluation session had been recorded. The "
                     "resulting exclusion rate is reported as a result, "
                     "not treated as a problem to tune away.",
}


def summary() -> str:
    lines = []
    for rq, items in ALL.items():
        done = sum(1 for i in items if i[3] == "collected")
        lines.append("%s: %d/%d collected, %d missing"
                     % (rq, done, len(items),
                        sum(1 for i in items if i[3] == "missing")))
    return " | ".join(lines)


if __name__ == "__main__":
    ppd = px_per_degree()
    print("Geometry: 1 deg = %.0f px at %.0f cm on a %.1f\" %dx%d display"
          % (ppd, VIEWING_DISTANCE_CM, SCREEN_DIAG_IN, SCREEN_W_PX, SCREEN_H_PX))
    print()
    print("%-10s %-12s %-14s" % ("threshold", "error px", "min AOI px"))
    for d in (1.5, 2.0, 2.5, 3.0):
        print("%-10s %-12.0f %-14.0f" % ("%.1f deg" % d, d * ppd, min_aoi_px(d)))
    print()
    print("I-DT minimum duration floor (3 samples):")
    for hz in (15, 21, 25, 30):
        print("  %2d Hz -> %.0f ms floor, use %.0f ms"
              % (hz, 3000.0 / hz, 1000 * fixation_min_duration_for(hz)))
    print()
    print(summary())
    for rq, items in ALL.items():
        missing = [i[0] for i in items if i[3] == "missing"]
        if missing:
            print("\n%s MISSING: %s" % (rq, ", ".join(missing)))
