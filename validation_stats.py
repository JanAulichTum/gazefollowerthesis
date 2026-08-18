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
# none < affine < quadratic-vertical < full-affine, because the failure
# being guarded against is a flexible model fitting session-moment noise.
SELECT_SE_MARGIN = 1.0
CANDIDATE_ORDER = ("none", "affine", "quadratic-vertical", "full-affine")

# A correction whose local gain leaves this band anywhere on the screen
# is rejected regardless of its cross-validated error: it is fold-over
# or runaway magnification, not a calibration.
GAIN_MIN, GAIN_MAX = 0.5, 3.0

# full-affine has SIX free parameters (a 2x2 matrix plus an offset) where
# every other candidate has two. F33 measured what that costs at n=7:
# leave-one-out gain of 4-7% on the two sheared sessions, indistinguishable
# from noise at that sample size and worse everywhere else — while pooling
# grid A and grid B to simulate FOURTEEN targets gave 15-28% on exactly
# those two sessions and a loss on the rest (independently re-verified,
# 2026-08-18, against the nine recorded sessions). Below this many
# measured targets, full-affine is not even attempted: fitting it at n=7
# would repeat F30's overfitting mistake with more parameters, and
# "not fittable" (rather than a silent skip) is what select_correction
# already reports for a candidate that cannot be tried at this sample
# size.
FULL_AFFINE_MIN_TARGETS = 12

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
            row = [float(t["mx"]), float(t["my"])]
            tgt = [float(t["tx"]), float(t["ty"])]
        except (TypeError, ValueError, KeyError):
            continue
        # A non-finite measurement is a FAILED recovery, not a value.
        # invert_poly returns NaN where a correction has no preimage;
        # averaging that in would turn one unrecoverable target into a
        # NaN accuracy for the whole grid, or — worse, before this guard
        # existed — into a fabricated one.
        if not all(math.isfinite(x) for x in row + tgt):
            continue
        ms.append(row)
        ts.append(tgt)
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
    )
    # ── The flag, computed BOTH ways ─────────────────────────────────
    # Is this error an offset or is it scatter? The obvious statistic —
    # |mean bias| / mean error — is defeated by a single bad target,
    # because the outlier inflates the denominator without moving the
    # numerator much. On the real session that motivated all of this,
    # Manuel_P2's post check is 76 px of almost pure displacement and
    # scored 0.41, under the 0.5 bar, purely because one target sat
    # 634 px away. The phase most damaged was the one that escaped the
    # flag.
    #
    # So the ratio is computed a second time from medians, and EITHER
    # exceeding the bar raises it. The two disagreeing is itself worth
    # seeing, which is why both are kept.
    med_bias = math.hypot(out["median_bias_x_px"], out["median_bias_y_px"])
    med_err = out["median_err_px"]
    ratio_med = (med_bias / med_err) if med_err > 0 else 0.0
    out["bias_ratio_median"] = round(ratio_med, 2)
    out["offset_dominated"] = bool(
        max(ratio, ratio_med) > OFFSET_DOMINATED_RATIO and not in_sample)
    # Meaningless on a fit set, where least squares forces the answer.
    out["offset_dominated_basis"] = (
        "n/a (fit set)" if in_sample else
        "mean and median" if (ratio > OFFSET_DOMINATED_RATIO
                              and ratio_med > OFFSET_DOMINATED_RATIO) else
        "median only — one target inflates the mean error"
        if ratio_med > OFFSET_DOMINATED_RATIO else
        "mean only — the offset is carried by a minority of targets"
        if ratio > OFFSET_DOMINATED_RATIO else "not dominated")
    if deg_per_px:
        out["bias_deg"] = round(bias * deg_per_px, 2)
        out["bias_x_deg"] = round(bx * deg_per_px, 2)
        out["bias_y_deg"] = round(by * deg_per_px, 2)
        out["median_err_deg"] = round(out["median_err_px"] * deg_per_px, 2)
    return out


# ══════════════════════════════════════════════════════════════════════
#  The off-diagonal terms — vertical error that depends on HORIZONTAL
#  position, and the reverse
# ══════════════════════════════════════════════════════════════════════
#
# Reported by a participant before any metric saw it, for the second
# time: "when looking right the y axis behaves weirdly" (F33).
#
# Fit the full 2-D map
#
#     measured = M . (target - centre) + offset
#
# and the correction this pipeline applies is only ever the DIAGONAL of
# M — a gain per axis — plus an offset. ``myx`` (vertical error per unit
# of horizontal position) and ``mxy`` are structurally unrepresentable by
# it, and no reported quantity contained them.
#
# On PILOT_03 ``myx = +0.228`` with a bootstrap interval excluding zero:
# 437 px, about 7.5 deg, of vertical displacement across the screen width
# purely from looking left versus right. PILOT_04 shows the same fault
# with the opposite sign. Both are far larger than the 3.0 deg inclusion
# threshold's worth of error.
#
# mxy and myx share a sign in every session measured, which makes this a
# SHEAR and not a rotation — head roll would give opposite signs. An
# off-centre head, or a head pose that differs between calibration and
# validation, produces exactly this.
#
# This measures it. It does not correct it: six parameters cannot be
# estimated from seven targets (leave-one-out gains 4-7 %, inside the
# noise). At fourteen targets the same fit gains 15-28 % on precisely the
# two sessions that show shear and loses on the four that do not — so
# correcting it is a protocol change, not a code change, and pooling the
# two grids to get there would spend the out-of-sample check.

SHEAR_LARGE = 0.08          # |shear| above this displaces > 150 px across
#                             a 1920 px screen — about 2.6 deg, most of
#                             the inclusion budget, from position alone.


def spatial_terms(targets: "list[dict]", width: float = 1920.0,
                  height: float = 1080.0, n_boot: int = 5000,
                  seed: int = 20260817) -> dict:
    """The full 2-D linear map, and how much of it the correction misses.

    Returns the four entries of ``M``, the symmetric (shear) and
    antisymmetric (rotation) parts, a seeded bootstrap interval for
    ``myx``, and a flag when the off-diagonal structure is large enough
    to matter. Six free parameters need more than the five targets this
    refuses below.
    """
    m, t = _pairs(targets)
    out: dict = {"n_targets": int(len(m))}
    if len(m) < 6:
        out["spatial_available"] = False
        out["why"] = ("fewer than six measured targets; a 2-D linear map "
                      "has six parameters and cannot be estimated")
        return out
    cx, cy = width / 2.0, height / 2.0
    T = np.c_[t[:, 0] - cx, t[:, 1] - cy, np.ones(len(t))]
    Y = np.c_[m[:, 0] - cx, m[:, 1] - cy]
    sol, *_ = np.linalg.lstsq(T, Y, rcond=None)
    M = sol[:2, :].T
    resid = Y - T @ sol
    mxx, mxy, myx, myy = (float(M[0, 0]), float(M[0, 1]),
                          float(M[1, 0]), float(M[1, 1]))
    # Decompose the off-diagonal into its symmetric (shear) and
    # antisymmetric (rotation) parts. Classifying by the SIGN of the
    # product mxy·myx looked equivalent and is not: when one term is
    # numerically zero the product's sign is set by the last bit of a
    # float, and a pure transvection — one off-diagonal zero — was named
    # a rotation on the strength of a -1e-17.
    shear = (myx + mxy) / 2.0
    anti = (myx - mxy) / 2.0
    rot = float(np.degrees(np.arctan2(myx - mxy, mxx + myy)))

    rng = np.random.default_rng(seed)
    n = len(m)
    boots = []
    for _ in range(n_boot):
        k = rng.integers(0, n, n)
        try:
            s, *_ = np.linalg.lstsq(T[k], Y[k], rcond=None)
            boots.append(s[0, 1])
        except np.linalg.LinAlgError:
            continue
    lo, hi = (np.percentile(boots, [2.5, 97.5]) if boots else (np.nan,) * 2)

    out.update(
        spatial_available=True,
        m_xx=round(mxx, 3), m_xy=round(mxy, 3),
        m_yx=round(myx, 3), m_yy=round(myy, 3),
        shear=round(float(shear), 3),
        rotation_deg=round(rot, 2),
        residual_px=round(float(np.sqrt((resid ** 2).sum(axis=1).mean())), 1),
        # HOW MUCH OF THE ERROR NO AFFINE MAP CAN REACH. The residual of
        # the best possible 2-D linear fit — six parameters, more than
        # any correction here applies — against the raw mean error. Near
        # 1.0 means the error has no linear structure at all and no
        # recalibration of any form will help; near 0 means it is almost
        # entirely gain, offset and shear.
        #
        # It separates two very different sessions that a mean cannot:
        # PILOT_00 reads 1.16 (nothing linear to remove) while Manuel_P2
        # reads 0.27 (almost all of it linear). Both were reported only
        # as an accuracy in degrees.
        residual_ratio=(round(float(np.sqrt((resid ** 2).sum(axis=1).mean())
                                    / np.hypot(*(m - t).T).mean()), 2)
                        if np.hypot(*(m - t).T).mean() > 0 else None),
        # What the off-diagonal actually costs, in the units a reader
        # cares about: vertical displacement from one edge of the screen
        # to the other, caused by horizontal position alone.
        dy_across_screen_px=round(myx * width, 0),
        m_yx_ci=[round(float(lo), 3), round(float(hi), 3)],
        m_yx_excludes_zero=bool(np.isfinite(lo) and (lo > 0 or hi < 0)),
        # MEASURED coverage, not nominal. A percentile bootstrap on seven
        # points under-covers: simulated at a true m_yx of 0.15 with
        # 40 px of noise over 400 replicates, the nominal 95 % interval
        # contained the truth 91 % of the time. "Excludes zero" is
        # therefore slightly optimistic, and an interval whose near end
        # sits close to zero should be read as suggestive rather than
        # established.
        ci_nominal_coverage=0.95,
        ci_measured_coverage=0.91,
        shear_large=bool(abs(shear) > SHEAR_LARGE),
        antisymmetric=round(float(anti), 3),
        # ── The quantity NO correction here can change ────────────────
        # A per-axis correction is a diagonal map D, and applying it
        # gives M' = D·M, so m_yx' = d_y·m_yx and m_yy' = d_y·m_yy. Their
        # RATIO is therefore invariant under every correction this
        # pipeline can produce, of any polynomial degree. It is the
        # honest measure of the off-diagonal fault: the vertical error
        # caused by horizontal position, expressed as a fraction of the
        # vertical gain, and no recalibration of this form touches it.
        m_yx_normalised=(round(myx / myy, 3) if abs(myy) > 1e-9 else None),
        # A shear and a rotation are different faults with different
        # causes. Head roll rotates; an off-centre head shears. Saying
        # which was measured is the value of reporting the pair.
        structure=_off_diagonal_structure(shear, anti),
        note=("the applied correction models only the DIAGONAL of this "
              "map plus an offset; it can rescale these terms but cannot "
              "null them, and m_yx_normalised is invariant under it"),
    )
    return out


def _off_diagonal_structure(shear: float, anti: float,
                            floor: float = 0.02) -> str:
    """Name the off-diagonal structure from its two parts.

    Judged on MAGNITUDES, not on the sign of a product: when one
    off-diagonal term is numerically zero the product's sign comes from
    the last bit of a float, and a pure transvection was named a rotation
    on the strength of a -1e-17.
    """
    s, a = abs(shear), abs(anti)
    if s < floor and a < floor:
        return "neither term is present"
    if s >= 2 * a:
        return ("shear — an off-centre head, or a head pose that differs "
                "between calibration and validation, produces this; head "
                "roll cannot")
    if a >= 2 * s:
        return ("rotation — consistent with head roll, which a shear "
                "cannot produce")
    return ("shear and rotation in comparable measure — a transvection; "
            "neither cause is isolated by this measurement alone")


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
    if candidate == "full-affine":
        return _fit_full_affine(m, t, width, height)
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


def _fit_full_affine(m: np.ndarray, t: np.ndarray,
                     width: float, height: float) -> "dict | None":
    """Fit ``target = A . (measured - centre) + b`` — a genuine 2x2 map.

    Every other candidate is a polynomial per axis: it cannot represent
    ``m_yx`` (vertical error caused by HORIZONTAL position — F33's
    shear) at any degree, because a per-axis fit never sees the other
    axis. This is the only candidate that can, and it needs
    FULL_AFFINE_MIN_TARGETS measured targets before it is even attempted
    (see that constant for why).
    """
    n = len(m)
    if n < FULL_AFFINE_MIN_TARGETS:
        return None
    cx, cy = width / 2.0 if width else 0.0, height / 2.0 if height else 0.0
    X = np.c_[m[:, 0] - cx, m[:, 1] - cy, np.ones(n)]
    try:
        sol, *_ = np.linalg.lstsq(X, t, rcond=None)
    except np.linalg.LinAlgError:
        return None
    A = np.array([[sol[0, 0], sol[1, 0]], [sol[0, 1], sol[1, 1]]])
    b = [float(sol[2, 0]), float(sol[2, 1])]
    # Gain sanity on the diagonal, same band every other candidate is
    # held to. The off-diagonal (the whole point of this candidate) is
    # deliberately NOT bounded the same way — a real shear can be large.
    if not (GAIN_MIN <= A[0, 0] <= GAIN_MAX
            and GAIN_MIN <= A[1, 1] <= GAIN_MAX):
        return None
    # Singular (or near it): raw_targets could not invert this back to a
    # measurement, so a correction that cannot be inverted is refused
    # the same way a folded quadratic is (F32).
    if abs(np.linalg.det(A)) < 1e-6:
        return None
    return {"kind": "full-affine", "A": A.tolist(), "b": b,
            "cx": cx, "cy": cy, "source": "auto-fit (full-affine)"}


def _is_full_affine(corr: "dict | None") -> bool:
    return bool(corr) and corr.get("kind") == "full-affine"


def apply_point(x: float, y: float, corr: "dict | None") -> "tuple":
    if not corr:
        return x, y
    if _is_full_affine(corr):
        xs, ys = apply_points([x], [y], corr)
        return float(xs[0]), float(ys[0])
    px, py = corr.get("px"), corr.get("py")
    return (float(np.polyval(px, x)) if px else x,
            float(np.polyval(py, y)) if py else y)


def apply_axis(values, coeffs: "list | None"):
    """Apply one axis of a correction to an array of measurements.

    Only meaningful for a DIAGONAL correction (affine / quadratic-
    vertical / none), where the two axes are independent. A full-affine
    correction mixes both axes by construction — that is the entire
    point of it (F33's shear, m_yx, is off-diagonal) — so there is no
    single-axis form of it to apply here. Callers that may see either
    kind must use ``apply_points`` instead, which both derives from and
    dispatches to this for the diagonal case.
    """
    v = np.asarray(values, float)
    return np.polyval(coeffs, v) if coeffs else v


def apply_points(xs, ys, corr: "dict | None"):
    """Vectorized correction for two ALIGNED arrays -> (xs', ys').

    THE canonical bulk-application path. app.py's per-sample gaze
    correction and rederive_session.py's offline re-derivation both call
    this instead of each independently deciding how to apply a
    correction — two implementations of "apply the correction" that
    could silently disagree is exactly F30's failure class, and it is
    also the only correct way to apply a full-affine correction: its two
    axes cannot be computed independently, so a per-axis ``apply_axis``
    call on x and then y separately is not merely inconvenient for this
    kind, it is wrong.
    """
    xs_arr = np.asarray(xs, dtype=float)
    ys_arr = np.asarray(ys, dtype=float)
    if not corr:
        return xs_arr, ys_arr
    if _is_full_affine(corr):
        A = np.asarray(corr["A"], float)
        b = np.asarray(corr["b"], float)
        cx, cy = corr.get("cx", 0.0), corr.get("cy", 0.0)
        m = np.c_[xs_arr - cx, ys_arr - cy]
        out = m @ A.T + b
        return out[:, 0], out[:, 1]
    px, py = corr.get("px"), corr.get("py")
    gx = apply_axis(xs_arr, px) if px else xs_arr
    gy = apply_axis(ys_arr, py) if py else ys_arr
    return gx, gy


def payload(corr: "dict | None") -> dict:
    """The manifest/UI form of a correction.

    One implementation, shared by the server, the review page and the
    re-derivation tool. Two copies of this would let the manifest and the
    recorded data describe different corrections, which is the failure
    the whole exercise exists to prevent.
    """
    if not corr:
        return {"active": False}
    if _is_full_affine(corr):
        A = corr.get("A") or [[1.0, 0.0], [0.0, 1.0]]
        b = corr.get("b") or [0.0, 0.0]
        return {
            "active": True,
            "kind": "full-affine",
            # No per-axis polynomial exists for this kind — explicit
            # None rather than an absent key, so a reader checking
            # `corr.get("px")` sees "there is none" rather than a
            # KeyError two calls later.
            "px": None, "py": None,
            "A": [[round(float(v), 6) for v in row] for row in A],
            "b": [round(float(v), 6) for v in b],
            "cx": round(float(corr.get("cx", 0.0)), 1),
            "cy": round(float(corr.get("cy", 0.0)), 1),
            "gain_x": round(float(A[0][0]), 3),
            "gain_y": round(float(A[1][1]), 3),
            "gain_mean": round((float(A[0][0]) + float(A[1][1])) / 2, 2),
            # What a diagonal correction cannot represent at all —
            # reported here so the same field that carries gain_x/gain_y
            # also carries the reason this candidate exists.
            "shear_yx": round(float(A[1][0]), 4),
            "shear_xy": round(float(A[0][1]), 4),
            "source": corr.get("source", "unknown"),
        }
    px, py = corr.get("px", [1, 0]), corr.get("py", [1, 0])
    cy = corr.get("cy", 0.0)
    gx = px[0] if len(px) == 2 else local_gain(px, corr.get("cx", 0.0))
    gy = py[0] if len(py) == 2 else local_gain(py, cy)
    return {
        "active": True,
        "px": [round(c, 6) for c in px],
        "py": [round(c, 6) for c in py],
        "cy": round(cy, 1),
        "kind": corr.get("kind") or ("quadratic-vertical" if len(py) == 3
                                     else "affine"),
        "gain_x": round(gx, 3),
        "gain_y": round(gy, 3),
        "gain_mean": round((gx + gy) / 2, 2),
        "source": corr.get("source", "unknown"),
    }


def from_payload(gc: "dict | None") -> "dict | None":
    """Reconstruct an APPLIABLE correction from its manifest/UI form.

    The inverse of ``payload()``. app.py (recovering the correction that
    was ACTIVE while a validation was measured) and rederive_session.py
    (recovering the one recorded in ``gain_correction``) both need this,
    and both used to rebuild ``{"px": gc.get("px"), "py": gc.get("py")}``
    by hand — which is a second, independent reconstruction of the exact
    kind ``payload()``'s own docstring warns about, and it silently
    produced a truthy-but-empty correction for full-affine, whose
    payload form has neither key. `raw_targets`/`apply_points` would
    then see a dict with no ``kind`` and no ``A``/``b``, fall through to
    the diagonal path, and invert or apply NOTHING — a session recorded
    under an active full-affine correction treated as though none had
    been applied.
    """
    if not gc or not gc.get("active"):
        return None
    if gc.get("kind") == "full-affine":
        return {"kind": "full-affine", "A": gc.get("A"), "b": gc.get("b"),
                "cx": gc.get("cx", 0.0), "cy": gc.get("cy", 0.0)}
    return {"px": gc.get("px"), "py": gc.get("py"),
            "cx": gc.get("cx", 0.0), "cy": gc.get("cy", 0.0),
            "kind": gc.get("kind")}


def corrections_equal(a: "dict | None", b: "dict | None",
                      tol: float = 1e-6) -> bool:
    """Do two FIT-form corrections (from select_correction / from_payload)
    describe the same mapping?

    Kind-aware: a full-affine correction has no px/py to compare, so a
    comparison written only against those two keys silently treats every
    full-affine correction as unchanged (both sides read as
    ``[None, None]`` equal) regardless of what it actually recovered —
    the same fault ``from_payload`` exists to remove, in the one place
    that DECIDES whether a session needs re-deriving.
    """
    if bool(a) != bool(b):
        return False
    if not a:
        return True
    if (a.get("kind") == "full-affine") or (b.get("kind") == "full-affine"):
        if a.get("kind") != b.get("kind"):
            return False
        Aa, Ab, ba, bb = a.get("A"), b.get("A"), a.get("b"), b.get("b")
        if Aa is None or Ab is None or ba is None or bb is None:
            return False
        return bool(np.allclose(Aa, Ab, atol=tol)
                   and np.allclose(ba, bb, atol=tol))
    pa, pb = a.get("px"), b.get("px")
    qa, qb = a.get("py"), b.get("py")
    if (pa is None) != (pb is None) or (qa is None) != (qb is None):
        return False
    if pa is None and qa is None:
        return True
    ok_x = pa is None or (len(pa) == len(pb)
                          and np.allclose(pa, pb, atol=tol))
    ok_y = qa is None or (len(qa) == len(qb)
                          and np.allclose(qa, qb, atol=tol))
    return bool(ok_x and ok_y)


def _predict(m: np.ndarray, corr: "dict | None") -> np.ndarray:
    if not corr:
        return m.copy()
    if _is_full_affine(corr):
        A = np.asarray(corr["A"], float)
        b = np.asarray(corr["b"], float)
        cx, cy = corr.get("cx", 0.0), corr.get("cy", 0.0)
        return (m - np.array([cx, cy])) @ A.T + b
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
    if candidate == "full-affine":
        # NOT _degrees_for: that dict has no entry for full-affine (it
        # is not a per-axis polynomial degree pair), and "no entry" used
        # to mean "return None here unconditionally" — so full-affine
        # could NEVER reach the loop below, no matter how well it fit,
        # and select_correction's fallback then mislabelled a fit with
        # ZERO failed leave-one-out folds as "unstable under
        # cross-validation" only because loo_errors had already bailed.
        # Found on PILOT_06, the first real 13-target session: full-
        # affine reported "0 of 13 folds produce a local gain outside
        # [...]" and STILL showed as unstable, because that status was
        # never actually informed by evaluating this candidate.
        if n < FULL_AFFINE_MIN_TARGETS:
            return None
    else:
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


def _diagnostics(diff: np.ndarray, n_boot: int = 20000,
                 seed: int = 20260817) -> dict:
    """Distribution-free views of a paired per-target improvement.

    Recorded beside the standard-error test, never in place of it. Seeded
    so a manifest is reproducible: a decision record whose numbers move
    between runs cannot be audited.
    """
    n = len(diff)
    if n < 2:
        return {}
    rng = np.random.default_rng(seed)
    means = rng.choice(diff, size=(n_boot, n), replace=True).mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    improved = int((diff > 0).sum())
    # Two-sided exact binomial against p = 0.5, on the targets that moved.
    moved = int((diff != 0).sum())
    k = improved
    if moved:
        tail = sum(math.comb(moved, i)
                   for i in range(min(k, moved - k), -1, -1)) \
            + sum(math.comb(moved, i)
                  for i in range(max(k, moved - k), moved + 1))
        p = min(1.0, tail / (2 ** moved))
    else:
        p = 1.0
    return {
        "loo_bootstrap_ci_px": [round(float(lo), 1), round(float(hi), 1)],
        "loo_bootstrap_excludes_zero": bool(lo > 0 or hi < 0),
        "loo_targets_improved": "%d/%d" % (improved, n),
        "loo_sign_test_p": round(float(p), 3),
        "diagnostics_note": ("corroborating only — the rule is the "
                             "standard-error test; these exist because "
                             "leave-one-out folds are not independent and "
                             "the SE is optimistic by an unknown amount"),
    }


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
            #
            # And WHICH refusal matters. A candidate that cannot be fitted
            # to the full grid is a different finding from one that fits
            # the full grid but collapses when a single target is held
            # out — the second says the model is unstable at this sample
            # size, which is exactly what the rule exists to detect, and
            # reporting it as "not fittable" hides that. PILOT_05's
            # quadratic fitted the seven targets with a sane gain
            # everywhere and produced a FOLD-OVER (local gain -3.3) in
            # three of its seven folds (F36).
            # full-affine has its own floor, checked separately from the
            # generic "not fittable" path below: below FULL_AFFINE_MIN_
            # TARGETS it was never attempted at all, which is a different
            # fact from "attempted and degenerate" — the exact distinction
            # this whole block exists to preserve (F36).
            if cand == "full-affine" and len(m) < FULL_AFFINE_MIN_TARGETS:
                rows.append({"candidate": cand, "status": "not fittable",
                             "why": ("needs at least %d measured targets "
                                     "for six free parameters; this grid "
                                     "has %d"
                                     % (FULL_AFFINE_MIN_TARGETS, len(m))),
                             "unstable_folds": 0, "_per_target": None})
                continue
            full = _fit_candidate(m, t, cand, width, height)
            bad = []
            for i in range(len(m)):
                keep = np.arange(len(m)) != i
                if _fit_candidate(m[keep], t[keep], cand,
                                  width, height) is None:
                    bad.append(i)
            rows.append({"candidate": cand,
                         "status": ("unstable under cross-validation"
                                    if full is not None else "not fittable"),
                         "why": (("fits all %d targets, but %d of %d "
                                  "leave-one-out folds produce a local gain "
                                  "outside [%.1f, %.1f] — the model is not "
                                  "supported at this sample size"
                                  % (len(m), len(bad), len(m),
                                     GAIN_MIN, GAIN_MAX))
                                 if full is not None else
                                 ("cannot be fitted to the full grid: too "
                                  "few distinct measured levels, or a local "
                                  "gain outside [%.1f, %.1f]"
                                  % (GAIN_MIN, GAIN_MAX))),
                         "unstable_folds": len(bad),
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
        # ── Corroborating diagnostics. NOT criteria. ─────────────────
        # The standard error above treats the seven leave-one-out errors
        # as independent, and they are not: consecutive folds share five
        # of their seven training targets. There is no unbiased estimator
        # of cross-validation variance, so the SE is optimistic and the
        # test is anti-conservative by an unknown amount.
        #
        # Two distribution-free views are therefore recorded alongside
        # it: a percentile bootstrap over the paired differences, and a
        # sign test counting how many of the seven targets the candidate
        # actually improves. Neither decides anything — the rule is the
        # rule as declared — but a reader can see whether all three
        # agree. On the two sessions measured they do, and they disagree
        # about how close the call was: PILOT_02's affine fit reads a
        # marginal 0.8 SE, yet its bootstrap interval spans zero and it
        # improves 4 targets out of 7, which is a coin flip.
        s.update(_diagnostics(diff))
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

def monotone_span(coeffs: list, lo: float, hi: float) -> "tuple":
    """The largest interval around [lo, hi] on which the map is monotone.

    A quadratic's derivative is linear, so it has at most one turning
    point; beyond it the mapping folds and is not invertible at all. The
    first version of ``invert_poly`` bisected over ``[lo - pad, hi + pad]``
    and decided the direction by comparing the two padded endpoints. When
    the turning point sat inside that padded window — which it does for
    any real quadratic fit, since the pad is a whole screen height — the
    direction was decided by the wrong branch and the function returned a
    plausible, silently wrong number: a value produced from y = 1400 came
    back as 1350.

    Returns ``(lo_bound, hi_bound)`` clipped at the turning points.
    """
    c = np.asarray(coeffs, float)
    pad = (hi - lo) if hi > lo else 1000.0
    lo_b, hi_b = lo - pad, hi + pad
    if len(c) < 3:
        return lo_b, hi_b
    deriv = np.polyder(c)
    roots = np.roots(deriv) if len(deriv) > 1 else np.array([])
    for r in np.real(roots[np.isreal(roots)]):
        if lo <= r <= hi:
            # A turning point INSIDE the screen means the mapping is not
            # monotone where it is used. gain_is_sane refuses such a fit,
            # so this is a corrupt or hand-edited correction.
            return float("nan"), float("nan")
        if r < lo:
            lo_b = max(lo_b, float(r))
        else:
            hi_b = min(hi_b, float(r))
    return lo_b, hi_b


def invert_poly(values, coeffs: "list | None", lo: float, hi: float):
    """Invert a monotone polynomial mapping numerically.

    ``_uncorrected_error`` inverted AFFINE corrections only and reported
    a quadratic-vertical fit as "not recoverable". That gap is load
    bearing: the selection rule and the corrected-vs-uncorrected
    comparison both need the raw figure for EVERY session, and a session
    that happened to get a quadratic fit would silently drop out of both.

    Affine is inverted in closed form. Higher degrees are inverted by
    bisection, restricted to the monotone span (see ``monotone_span``).

    A value the mapping never attains on that span — off-screen gaze past
    a turning point, or a corrupt correction — returns **NaN**. It used
    to return the edge of the search bracket, which is a number, looks
    like a measurement, and is not one. Callers drop non-finite
    measurements rather than averaging them.
    """
    v = np.asarray(values, float)
    if not coeffs:
        return v
    if len(coeffs) == 2:
        a, b = coeffs
        if a == 0:
            return np.full_like(v, np.nan)
        return (v - b) / a
    lo_b0, hi_b0 = monotone_span(coeffs, lo, hi)
    if not (np.isfinite(lo_b0) and np.isfinite(hi_b0)):
        return np.full_like(v, np.nan)
    f_lo = float(np.polyval(coeffs, lo_b0))
    f_hi = float(np.polyval(coeffs, hi_b0))
    increasing = f_hi > f_lo
    reach_lo, reach_hi = (f_lo, f_hi) if increasing else (f_hi, f_lo)
    lo_b = np.full_like(v, lo_b0)
    hi_b = np.full_like(v, hi_b0)
    for _ in range(80):
        mid = 0.5 * (lo_b + hi_b)
        f = np.polyval(coeffs, mid)
        go_up = (f < v) if increasing else (f > v)
        lo_b = np.where(go_up, mid, lo_b)
        hi_b = np.where(go_up, hi_b, mid)
    out = 0.5 * (lo_b + hi_b)
    # Outside the attainable range there is no preimage. Say so.
    return np.where((v < reach_lo - 1e-9) | (v > reach_hi + 1e-9),
                    np.nan, out)


def raw_targets(targets: "list[dict]", corr: "dict | None",
                width: float = 1920.0, height: float = 1080.0) -> list:
    """Per-target records with the measurement mapped back to raw.

    ``corr`` is the correction that was ACTIVE while these targets were
    measured. Pass ``None`` when nothing was applied.

    A full-affine correction's two axes cannot be inverted independently
    (that is the point of it), so it is inverted as one 2x2 matrix solve
    per point rather than through ``invert_poly``'s per-axis bisection.
    Unlike a folded quadratic, a full-affine map with a non-zero
    determinant (already required by ``_fit_full_affine``, or it would
    not have been accepted as a correction) is invertible everywhere —
    no monotone-span clipping, no off-screen NaN.
    """
    if not corr:
        return [dict(t) for t in (targets or [])]
    if _is_full_affine(corr):
        A = np.asarray(corr["A"], float)
        b = np.asarray(corr["b"], float)
        cx, cy = corr.get("cx", 0.0), corr.get("cy", 0.0)
        det = np.linalg.det(A)
        A_inv = np.linalg.inv(A) if abs(det) >= 1e-9 else None
        out = []
        for t in targets or []:
            r = dict(t)
            if t.get("mx") is not None and t.get("my") is not None:
                if A_inv is None:
                    r["mx"], r["my"] = float("nan"), float("nan")
                else:
                    corrected = np.array([float(t["mx"]), float(t["my"])])
                    raw = A_inv @ (corrected - b) + np.array([cx, cy])
                    r["mx"], r["my"] = float(raw[0]), float(raw[1])
            out.append(r)
        return out
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
