# -*- coding: utf-8 -*-
"""Signed bias, robust accuracy, and out-of-sample selection of the
validation-based gain correction.

WHY THIS MODULE EXISTS
----------------------
Every accuracy figure in this pipeline was a MEAN UNSIGNED DISTANCE
between measured gaze and target. That number cannot tell the two
failure modes apart:

    scattered 60 px in random directions   ->  mean error 60 px
    displaced 60 px upward, every sample   ->  mean error 60 px

The first is noise and averages out of any aggregate. The second is a
systematic offset that every downstream spatial claim inherits — region
assignment, correspondence with the model's boxes, the ambiguity rate.
A participant reported it before any metric did ("all the eye gaze was
above the people"), because no reported quantity anywhere was SIGNED.

``signed_bias`` makes it a number. ``select_correction`` stops the gain
correction from manufacturing one.

THE OVERFITTING RESULT (2026-08-17, F30)
----------------------------------------
The correction is fitted on validation grid A (7 targets, 2 free
parameters per axis) and was applied unconditionally. Measured on the
two sessions that carry per-target records:

    session      grid A (fit set)        grid B (never fitted to)
    Manuel_P2    96.5 -> 37.4 px  -61 %   65.8 -> 61.6 px   -6 %
    PILOT_02     79.0 -> 45.8 px  -42 %  113.9 -> 112.2 px  -1 %

It removes 42-61 % of the error where it was fitted and 1-6 % anywhere
else. Leave-one-out on grid A ALONE already exposes this — Manuel
96.5 -> 56.8 px under LOO against 37.4 px refit — so the diagnosis needed
neither a second grid nor a participant.

The underlying reason it cannot generalise is that the quantity being
modelled is not stable. Raw signed bias across the three checks of one
session, minutes apart:

    Manuel_P2   dy  +72.4 -> +20.1 -> +69.7    dx  -32.0 -> -58.4 -> +55.6
    PILOT_02    dy  +37.1 -> -34.8 -> +41.4    dx  +34.1 -> +94.0 -> +87.7

A session-wise constant cannot describe a number that swings 50-90 px
inside one session. Manuel's fitted vertical polynomial was
``[1.0008, -72.9]`` — a gain of 1.000 and a pure -73 px offset, i.e. the
fit subtracted a snapshot of a moving quantity, and that subtraction is
most of the upward displacement the participant saw.

WHY SELECTION HAPPENS ON GRID A AND NOT GRID B
----------------------------------------------
Selecting the model on grid B would spend the one held-out measurement
the two-grid protocol exists to provide: ``pre_check`` would become
in-sample-for-selection and could no longer be reported as the corrected
accuracy. So the model is chosen by leave-one-out cross-validation
INSIDE grid A, and grid B is never consulted by the decision. Grid B
remains what it was designed to be — an out-of-sample report.

A NOTE ON SIGNED BIAS ON THE FIT SET
------------------------------------
A least-squares affine fit zeroes its own mean residual. The signed bias
of grid A AFTER the correction is therefore identically (0, 0) — an
algebraic identity, not a measurement. ``signed_bias`` marks it
``in_sample=True`` so no report can present it as evidence.
"""

from __future__ import annotations

import math

import numpy as np

# ── The pre-declared decision rule ────────────────────────────────────
# Fixed 2026-08-17, BEFORE application to any session, and applied
# identically to all. The previous rule was: fit on grid A, apply
# unconditionally, no check that it generalised.
#
# A candidate correction is accepted only if, under leave-one-out
# cross-validation on grid A, it beats the no-correction baseline on
# BOTH of:
#
#   * mean 2-D error, by more than SELECT_SE_MARGIN standard errors of
#     the PAIRED per-target difference (not a fixed percentage: with
#     seven targets a 5 % gain is inside the noise, and the noise is
#     what the standard error measures), and
#   * the magnitude of the signed bias, which must not increase.
#
# The bias condition is the one the mean unsigned error cannot express,
# and it is the condition this study needed: a correction that trades
# scatter for a systematic offset improves the headline figure while
# making every downstream spatial claim worse.
#
# Ties and near-ties go to the simpler model, in the order
# none < affine < quadratic-vertical, because the failure being guarded
# against is a flexible model fitting session-moment noise.
SELECT_SE_MARGIN = 1.0
CANDIDATE_ORDER = ("none", "affine", "quadratic-vertical")

# A correction whose local gain leaves this band anywhere on the screen
# is rejected regardless of its cross-validated error: it is fold-over
# or runaway magnification, not a calibration.
GAIN_MIN, GAIN_MAX = 0.5, 3.0

# Flag threshold: when |bias| exceeds this share of the mean unsigned
# error, the error is dominated by a systematic offset rather than by
# scatter, and reporting the mean alone is misleading.
OFFSET_DOMINATED_RATIO = 0.5

# Two bias magnitudes closer than this are the same bias. At 58 px per
# degree half a pixel is 0.009 deg.
BIAS_EQUAL_PX = 0.5


# ══════════════════════════════════════════════════════════════════════
#  Signed bias
# ══════════════════════════════════════════════════════════════════════

def _pairs(targets: "list[dict]") -> "tuple[np.ndarray, np.ndarray]":
    """(measured, target) arrays from validation target records.

    Records missing a measurement are dropped, not treated as zero.
    """
    ms, ts = [], []
    for t in targets or []:
        if not isinstance(t, dict):
            continue
        if t.get("mx") is None or t.get("my") is None:
            continue
        try:
            ms.append([float(t["mx"]), float(t["my"])])
            ts.append([float(t["tx"]), float(t["ty"])])
        except (TypeError, ValueError, KeyError):
            continue
    if not ms:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.asarray(ms, float), np.asarray(ts, float)


def direction_words(bias_x: float, bias_y: float,
                    deadband_px: float = 5.0) -> str:
    """Plain-language direction of a signed bias.

    Screen coordinates: y grows DOWNWARD, so a negative dy means the
    recorded gaze sits ABOVE the target. Getting that backwards in a
    report is the whole point of writing the direction out in words
    rather than leaving a reader to infer it from a sign.
    """
    parts = []
    if abs(bias_y) >= deadband_px:
        parts.append("%.0f px %s" % (abs(bias_y),
                                     "above" if bias_y < 0 else "below"))
    if abs(bias_x) >= deadband_px:
        parts.append("%.0f px %s" % (abs(bias_x),
                                     "left of" if bias_x < 0 else "right of"))
    if not parts:
        return "no appreciable offset"
    return " and ".join(parts) + " the target"


def signed_bias(targets: "list[dict]", deg_per_px: "float | None" = None,
                in_sample: bool = False) -> dict:
    """Signed bias and robust accuracy for one validation grid.

    ``targets`` are the per-target records as stored in the manifest
    (``tx``/``ty`` true, ``mx``/``my`` measured). ``deg_per_px`` converts
    to degrees; omit it and the degree fields are absent rather than
    guessed.

    ``in_sample=True`` marks a grid the correction was FITTED on, where
    the post-correction bias is zero by construction.
    """
    m, t = _pairs(targets)
    out: dict = {"n_targets": int(len(m)), "bias_in_sample": bool(in_sample)}
    if len(m) == 0:
        out["bias_available"] = False
        return out
    d = m - t
    err = np.hypot(d[:, 0], d[:, 1])
    bx, by = float(d[:, 0].mean()), float(d[:, 1].mean())
    bias = math.hypot(bx, by)
    mean_err = float(err.mean())
    ratio = (bias / mean_err) if mean_err > 0 else 0.0
    out.update(
        bias_available=True,
        bias_x_px=round(bx, 1),
        bias_y_px=round(by, 1),
        bias_px=round(bias, 1),
        bias_direction=direction_words(bx, by),
        # Medians alongside the means. The mean over seven targets has no
        # outlier rule: one target measured 649 px away moved a session's
        # reported accuracy from 2.41 to 3.52 deg. Both are reported so
        # that leverage is visible rather than silent.
        mean_err_px=round(mean_err, 1),
        median_err_px=round(float(np.median(err)), 1),
        median_bias_x_px=round(float(np.median(d[:, 0])), 1),
        median_bias_y_px=round(float(np.median(d[:, 1])), 1),
        max_err_px=round(float(err.max()), 1),
        bias_ratio=round(ratio, 2),
        # The flag the brief asked for: is this error an offset or is it
        # scatter? Meaningless on a fit set, where the answer is forced.
        offset_dominated=bool(ratio > OFFSET_DOMINATED_RATIO
                              and not in_sample),
    )
    if deg_per_px:
        out["bias_deg"] = round(bias * deg_per_px, 2)
        out["bias_x_deg"] = round(bx * deg_per_px, 2)
        out["bias_y_deg"] = round(by * deg_per_px, 2)
        out["median_err_deg"] = round(out["median_err_px"] * deg_per_px, 2)
    return out


# ══════════════════════════════════════════════════════════════════════
#  Correction candidates, cross-validated on the fit grid
# ══════════════════════════════════════════════════════════════════════

def fit_poly(xs, ts, degree: int) -> "list | None":
    """Least-squares ``target = poly(measured)``, coeffs highest-first.

    numpy's ``Polynomial.fit`` works in a scaled/shifted domain and is
    well-conditioned for raw pixel inputs in the hundreds; a plain
    ``polyfit`` at degree 2 on such values collapses the quadratic term
    silently.
    """
    xs = np.asarray(xs, float)
    ts = np.asarray(ts, float)
    if len(xs) < degree + 1:
        return None
    if len(np.unique(np.round(xs, 1))) < degree + 1:
        return None
    try:
        coeffs = list(np.polynomial.Polynomial.fit(
            xs, ts, degree).convert().coef)[::-1]
    except Exception:  # noqa: BLE001 — a degenerate fit is not an error
        return None
    while len(coeffs) < degree + 1:
        coeffs.insert(0, 0.0)
    return [float(c) for c in coeffs]


def local_gain(coeffs: list, at: float) -> float:
    """Derivative of the mapping at a coordinate — the local gain."""
    if not coeffs or len(coeffs) < 2:
        return 0.0
    return float(np.polyval(np.polyder(np.asarray(coeffs, float)), at))


def gain_is_sane(coeffs: "list | None", extent: float) -> bool:
    """Local gain stays inside the plausible band across the screen."""
    if coeffs is None:
        return False
    if extent <= 0:
        return GAIN_MIN <= local_gain(coeffs, 0.0) <= GAIN_MAX
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        if not (GAIN_MIN <= local_gain(coeffs, extent * frac) <= GAIN_MAX):
            return False
    return True


def bias_not_worsened(candidate_bias_px: float, baseline_bias_px: float,
                      bias_se_px: float = 0.0) -> bool:
    """Did a candidate correction leave the systematic offset no worse?

    THE condition the mean unsigned error cannot express, and the one
    this study needed: a correction that trades scatter for a systematic
    displacement improves the headline accuracy while making every
    downstream spatial claim worse, because scatter averages out of an
    aggregate and an offset does not.

    Judged against the bias's own sampling error, and never below half a
    pixel — comparing two bias magnitudes exactly made the rule turn on
    the last bit of a float, where a perfect fit produced 2e-14 and lost
    to a baseline of exactly 0.0.
    """
    return bool(candidate_bias_px
                <= baseline_bias_px + max(bias_se_px, BIAS_EQUAL_PX))


def _degrees_for(candidate: str) -> "tuple[int, int] | None":
    return {"none": None, "affine": (1, 1),
            "quadratic-vertical": (1, 2)}.get(candidate)


def _fit_candidate(m: np.ndarray, t: np.ndarray, candidate: str,
                   width: float, height: float) -> "dict | None":
    degs = _degrees_for(candidate)
    if degs is None:
        return None
    dx, dy = degs
    px = fit_poly(m[:, 0], t[:, 0], dx)
    py = fit_poly(m[:, 1], t[:, 1], dy)
    if px is None or py is None:
        return None
    if not (gain_is_sane(px, width) and gain_is_sane(py, height)):
        return None
    return {"px": px, "py": py,
            "cx": width / 2.0 if width else 0.0,
            "cy": height / 2.0 if height else 0.0,
            "source": "auto-fit (%s)" % candidate,
            "kind": candidate}


def apply_point(x: float, y: float, corr: "dict | None") -> "tuple":
    if not corr:
        return x, y
    px, py = corr.get("px"), corr.get("py")
    return (float(np.polyval(px, x)) if px else x,
            float(np.polyval(py, y)) if py else y)


def _predict(m: np.ndarray, corr: "dict | None") -> np.ndarray:
    if not corr:
        return m.copy()
    px, py = corr.get("px"), corr.get("py")
    out = m.copy()
    if px:
        out[:, 0] = np.polyval(px, m[:, 0])
    if py:
        out[:, 1] = np.polyval(py, m[:, 1])
    return out


def loo_errors(m: np.ndarray, t: np.ndarray, candidate: str,
               width: float, height: float) -> "np.ndarray | None":
    """Per-target leave-one-out 2-D errors for a candidate model.

    Each target is predicted by a model refitted WITHOUT it. This is the
    estimate the unconditional rule never computed: refitting on all
    seven and scoring on the same seven reported a 42-61 % improvement
    where the honest figure was 14-41 %.
    """
    n = len(m)
    if candidate == "none":
        return np.hypot(m[:, 0] - t[:, 0], m[:, 1] - t[:, 1])
    degs = _degrees_for(candidate)
    if degs is None or n < max(degs) + 2:
        return None
    errs = np.empty((n, 2), float)
    for i in range(n):
        keep = np.arange(n) != i
        corr = _fit_candidate(m[keep], t[keep], candidate, width, height)
        if corr is None:
            return None
        pred = _predict(m[i:i + 1], corr)[0]
        errs[i] = pred - t[i]
    return errs


def _loo_summary(m, t, candidate, width, height) -> "dict | None":
    e = loo_errors(m, t, candidate, width, height)
    if e is None:
        return None
    if e.ndim == 1:                       # the "none" baseline
        d = m - t
        dist = e
    else:
        d = e
        dist = np.hypot(e[:, 0], e[:, 1])
    n = len(dist)
    # Standard error of the bias VECTOR. The bias is a mean of n
    # residuals and carries its own sampling error; comparing two bias
    # magnitudes with a bare `<=` makes the rule turn on the last bit of
    # a float, which it did — a perfect fit produced a bias of 1e-14 and
    # lost to a baseline bias of exactly 0.0.
    se = math.hypot(float(d[:, 0].std(ddof=1)) / math.sqrt(n),
                    float(d[:, 1].std(ddof=1)) / math.sqrt(n)) if n > 1 \
        else 0.0
    return {"candidate": candidate,
            "loo_mean_err_px": float(dist.mean()),
            "loo_median_err_px": float(np.median(dist)),
            "loo_bias_x_px": float(d[:, 0].mean()),
            "loo_bias_y_px": float(d[:, 1].mean()),
            "loo_bias_px": float(math.hypot(d[:, 0].mean(), d[:, 1].mean())),
            "loo_bias_se_px": se,
            "_per_target": dist}


def select_correction(targets: "list[dict]", width: float,
                      height: float) -> dict:
    """Choose a correction by leave-one-out cross-validation on grid A.

    Returns ``{"correction": <corr or None>, "decision": {...}}``. The
    decision record is written to the manifest so that WHY a session was
    corrected — or was not — is auditable after the fact rather than
    inferred from the coefficients.
    """
    m, t = _pairs(targets)
    decision: dict = {
        "rule": "leave-one-out on the fit grid; a candidate must beat "
                "no-correction on BOTH mean 2-D error (by > %.1f SE of "
                "the paired per-target difference) and |signed bias|; "
                "ties go to the simpler model"
                % SELECT_SE_MARGIN,
        "rule_fixed_on": "2026-08-17",
        "previous_rule": "fit on grid A, apply unconditionally, no "
                         "generalisation check (F30)",
        "selection_grid": "A (the fit grid) — grid B is never consulted, "
                          "so pre_check remains out of sample",
        "se_margin": SELECT_SE_MARGIN,
        "n_targets": int(len(m)),
    }
    if len(m) < 4:
        decision.update(chosen="none", reason="too few measured targets")
        return {"correction": None, "decision": decision}

    base = _loo_summary(m, t, "none", width, height)
    base["status"] = "baseline"
    rows = [base]
    for cand in CANDIDATE_ORDER[1:]:
        s = _loo_summary(m, t, cand, width, height)
        if s is None:
            # Recorded, not dropped. "This model was refused" and "this
            # model was never tried" are different facts about a session.
            rows.append({"candidate": cand, "status": "not fittable",
                         "why": "too few distinct measured levels, or a "
                                "local gain outside [%.1f, %.1f] somewhere "
                                "on the screen" % (GAIN_MIN, GAIN_MAX),
                         "_per_target": None})
            continue
        s["status"] = "evaluated"
        diff = base["_per_target"] - s["_per_target"]     # >0 = candidate better
        n = len(diff)
        se = float(diff.std(ddof=1) / math.sqrt(n)) if n > 1 else 0.0
        s["mean_improvement_px"] = float(diff.mean())
        s["improvement_se_px"] = se
        s["improvement_se_units"] = (float(diff.mean() / se)
                                     if se > 0 else float("inf")
                                     if diff.mean() > 0 else 0.0)
        s["beats_baseline_error"] = bool(
            diff.mean() > SELECT_SE_MARGIN * se) if se > 0 \
            else bool(diff.mean() > 0)
        s["beats_baseline_bias"] = bias_not_worsened(
            s["loo_bias_px"], base["loo_bias_px"], s["loo_bias_se_px"])
        rows.append(s)

    # Walk the candidates from simplest to most flexible. A more complex
    # model replaces the incumbent only if it beats THE INCUMBENT by the
    # same standard-error margin — not merely by a smaller number. On
    # seven targets the quadratic beat the affine fit by 2.5 px (0.2 SE)
    # on one real session; taking the lower figure there would be
    # selecting noise, which is the exact failure this rule exists to
    # stop.
    incumbent = base
    for r in rows[1:]:
        if r.get("_per_target") is None:
            continue
        if not (r.get("beats_baseline_error")
                and r.get("beats_baseline_bias")):
            r["beats_incumbent"] = False
            continue
        d = incumbent["_per_target"] - r["_per_target"]
        n = len(d)
        se = float(d.std(ddof=1) / math.sqrt(n)) if n > 1 else 0.0
        better = (d.mean() > SELECT_SE_MARGIN * se) if se > 0 \
            else (d.mean() > 0)
        r["beats_incumbent"] = bool(better)
        r["vs_incumbent"] = "%s: %+.1f px (%.1f SE)" % (
            incumbent["candidate"], d.mean(), (d.mean() / se) if se else 0.0)
        if better:
            incumbent = r
    chosen = incumbent["candidate"]

    decision["chosen"] = chosen
    decision["candidates"] = [
        {k: (round(v, 2) if isinstance(v, float) else v)
         for k, v in r.items() if not k.startswith("_")} for r in rows]
    if chosen == "none":
        decision["reason"] = (
            "no candidate beat no-correction out of sample on both "
            "criteria; the correction is recorded but NOT applied")
        return {"correction": None, "decision": decision}

    corr = _fit_candidate(m, t, chosen, width, height)
    decision["reason"] = (
        "%s beat no-correction under LOO by %.1f px (%.1f SE) and did not "
        "increase |bias| (%.1f -> %.1f px)"
        % (chosen,
           next(r["mean_improvement_px"] for r in rows[1:]
                if r["candidate"] == chosen),
           next(r["improvement_se_units"] for r in rows[1:]
                if r["candidate"] == chosen),
           base["loo_bias_px"],
           next(r["loo_bias_px"] for r in rows[1:]
                if r["candidate"] == chosen)))
    return {"correction": corr, "decision": decision}


# ══════════════════════════════════════════════════════════════════════
#  Recovering the raw measurement, for any correction form
# ══════════════════════════════════════════════════════════════════════

def invert_poly(values, coeffs: "list | None", lo: float, hi: float):
    """Invert a monotone polynomial mapping numerically.

    ``_uncorrected_error`` inverted AFFINE corrections only and reported
    a quadratic-vertical fit as "not recoverable". That gap is load
    bearing: the apply-if-it-helps rule and the corrected-vs-uncorrected
    comparison both need the raw figure for EVERY session, and a session
    that happened to get a quadratic fit would silently drop out of both.

    Affine is inverted in closed form. Higher degrees are inverted by
    bisection on the screen extent, which is valid because a correction
    is only ever accepted when its local gain is positive and bounded
    across that extent (``gain_is_sane``), so the mapping is monotone
    there.
    """
    v = np.asarray(values, float)
    if not coeffs:
        return v
    if len(coeffs) == 2:
        a, b = coeffs
        if a == 0:
            return np.full_like(v, np.nan)
        return (v - b) / a
    pad = (hi - lo) if hi > lo else 1000.0
    lo_b = np.full_like(v, lo - pad)
    hi_b = np.full_like(v, hi + pad)
    increasing = np.polyval(coeffs, hi + pad) > np.polyval(coeffs, lo - pad)
    for _ in range(60):
        mid = 0.5 * (lo_b + hi_b)
        f = np.polyval(coeffs, mid)
        go_up = (f < v) if increasing else (f > v)
        lo_b = np.where(go_up, mid, lo_b)
        hi_b = np.where(go_up, hi_b, mid)
    return 0.5 * (lo_b + hi_b)


def raw_targets(targets: "list[dict]", corr: "dict | None",
                width: float = 1920.0, height: float = 1080.0) -> list:
    """Per-target records with the measurement mapped back to raw.

    ``corr`` is the correction that was ACTIVE while these targets were
    measured. Pass ``None`` when nothing was applied.
    """
    if not corr:
        return [dict(t) for t in (targets or [])]
    px, py = corr.get("px"), corr.get("py")
    out = []
    for t in targets or []:
        r = dict(t)
        if t.get("mx") is not None and t.get("my") is not None:
            r["mx"] = float(invert_poly([float(t["mx"])], px, 0.0, width)[0])
            r["my"] = float(invert_poly([float(t["my"])], py, 0.0, height)[0])
        out.append(r)
    return out


def corrected_targets(targets: "list[dict]", corr: "dict | None") -> list:
    """Per-target records with a correction applied to the measurement."""
    if not corr:
        return [dict(t) for t in (targets or [])]
    out = []
    for t in targets or []:
        r = dict(t)
        if t.get("mx") is not None and t.get("my") is not None:
            r["mx"], r["my"] = apply_point(float(t["mx"]), float(t["my"]),
                                           corr)
        out.append(r)
    return out


def both_ways(targets: "list[dict]", corr: "dict | None",
              correction_was_active: bool,
              deg_per_px: "float | None" = None,
              width: float = 1920.0, height: float = 1080.0,
              in_sample: bool = False) -> dict:
    """Accuracy and signed bias for one grid, corrected AND uncorrected.

    Both figures come from the same seven measurements, so the pair is a
    like-for-like comparison of the correction's effect rather than a
    comparison of two different moments.
    """
    if correction_was_active:
        raw = raw_targets(targets, corr, width, height)
        cor = [dict(t) for t in (targets or [])]
    else:
        raw = [dict(t) for t in (targets or [])]
        cor = corrected_targets(targets, corr)
    return {
        "raw": signed_bias(raw, deg_per_px, in_sample=False),
        "corrected": signed_bias(cor, deg_per_px, in_sample=in_sample),
        "correction_was_active": bool(correction_was_active),
    }
