# Methodology findings

Append-only log of things that were **measured**, not assumed. Each entry
names the evidence and the chapter it belongs in, so the thesis can be
written from this file rather than from memory.

Rules for adding: state the number, state how it was obtained, state what
it licenses you to claim. If something was suspected but not confirmed,
say so in the entry — a log that mixes the two is worth nothing.

---

## F1 · Windows parks the tracker on efficiency cores
**2026-08-06 · Methods (apparatus), Limitations**

Sampling rate collapsed from ~30 Hz to 15 Hz whenever the fullscreen
browser held the foreground. Cause: Windows EcoQoS plus Intel Thread
Director scheduling the tracker subprocess onto the i9-13900H's E-cores.

The diagnostic was that **both** model stages slowed by the *same* 2.26×.
MediaPipe FaceMesh is single-threaded TFLite and the MNN gaze CNN runs
four threads, so resource contention cannot slow them equally — a uniform
scalar means the CPU itself became slower. Confirmed by hardware:
Gracemont E-cores run 2–2.5× slower than the P-cores.

Fix: `SetProcessInformation(ProcessPowerThrottling, StateMask=0)` at
tracker startup (`perf_mode.py`). **15.1 → 29.4 Hz.**

Report as a hardware/OS constraint that any webcam eye-tracking study on
modern Windows laptops must handle, not as an implementation detail.

## F2 · GazeFollower's capture settings never take effect
**2026-08-06 · Methods (apparatus)**

`WebCamCamera.__init__` calls `set(CAP_PROP_FRAME_WIDTH, …)` on a
`VideoCapture` that has not been opened, then opens it in `open()`.
`set()` on an unopened capture is a no-op and opening resets properties,
so frames arrive at the webcam's native resolution and are resized in
software every frame.

Fix: apply properties **after** opening (`camera_patch.py`, opt-in via
`GF_CAMERA_FIX=1`). **21.4 → 32.0 Hz.**

## F3 · Perf mode holds under CPU downclocking
**2026-08-11 · Results (data quality)**

Session `13:47 11.08`, on battery at 40 %: the CPU dropped from 2600 MHz
to 1496 MHz at t=126 s and stayed there for the rest of the recording.
The sampling rate held at **29–30 Hz throughout**, with model stages
steady at 10.0 ms (FaceMesh) and 21.5 ms (gaze CNN).

Evidence that the fix in F1 is robust to power state, not merely to the
condition it was developed under. Worth stating explicitly: it answers
"does your rate depend on the laptop being plugged in?" with data.

## F4 · The sampling rate is not limited by lighting on this hardware
**2026-08-10 · Methods, Limitations**

Auto-exposure throttling was hypothesised (a UVC webcam halves its frame
rate in dim light because it cannot integrate longer than one frame
period) and **ruled out by measurement**: `camera_remedy.py` measured
31.2 fps at brightness 141/255 with no changes, and neither MJPG nor a
capped exposure improved it.

A useful negative result. It also demonstrates the discriminator: models
finishing in a fraction of the frame interval means the pipeline is idle
waiting, which the CPU cannot cause.

## F5 · The gain correction repairs vertical range compression
**2026-08-11 · Methods (analysis), Limitations**

Across sessions, `calibration_diagnosis.py` finds vertical **range
compression** in the majority: the gaze spans less of the screen than the
targets do, proportionally to eccentricity. Slopes observed 0.60–0.88
(one session 0.33). Horizontal error is mostly **unstructured**.

Interpretation: the appearance-based CNN regresses toward its training
mean, and validation targets sitting outside the span of the calibration
grid make it extrapolate. This makes the correction a **declared,
characterised pipeline step**, not a fudge factor — which is the answer
to "what is your correction correcting?".

**Caveat, measured:** where the error is ONE-SIDED, a symmetric gain
about the centre pushes the good end past the target. Simulated on the
observed pattern: an upper half at 0 px error moves to 61 px *above*,
and mean absolute error rises 21 → 36 px. The mean hides this because
the ends cancel.

## F6 · Two-grid validation, and why one grid is not enough
**2026-08-11 · Methods (procedure)**

Fitting the correction on a target grid and reporting the error at that
same grid scores the fit on its training points. Re-measuring at the same
positions with fresh samples is better but still not a generalisation
estimate.

Protocol: `pre_fit` on grid A (uncorrected, the fit set), `pre_check` on
grid B (corrected, positions never fitted to), `post` on grid B. Grid B
shares no target with A and is matched on eccentricity — mean |x−50|
20.7 vs 20.9, mean |y−50| 21.7 vs 21.7, five vertical elevations each —
so "the correction generalises" cannot be confused with "grid B was
easier".

**Inclusion figure** is the mean of `pre_check` and `post`: the accuracy
the stimulus data was recorded at is bracketed by them, and neither end
alone answers that question. Fixed in advance, applied to every session.

## F7 · Best measured tracking quality
**2026-08-11 · Results**

Session `13:47 11.08`, seven targets per grid, ≥40 samples per target:

| | |
|---|---|
| accuracy, uncorrected (grid A) | 2.23° |
| accuracy, corrected out-of-sample (grid B) | 1.17° |
| accuracy, post-stimulus (grid B) | 0.90° |
| **inclusion figure (mean of the two)** | **1.04°** |
| sampling rate | 29.7 Hz |
| detection | 100 % |
| drift (uncorrected basis) | −0.29° |

Drift is *negative*: tracking did not degrade across the session.

## F8 · Precision is not comparable across sampling rates
**2026-08-11 · Methods (measures)**

`mean_precision_px` is a sample-to-sample RMS, so it measures movement
between *consecutive* samples and depends on how far apart in time they
are. Raising the validation poll rate from 7 Hz to 30 Hz exposed
high-frequency noise that decimation had hidden, and the figure rose
although the signal was unchanged.

Added `mean_precision_sd_px` — dispersion about each target's own median
— which is rate-independent. **Compare that one across sessions.** Report
the sampling rate alongside any precision figure.

## F9 · Attribution is governed by SEPARATION, not object size
**2026-08-11 · Results (the method's ceiling)**

The rule *min AOI ≥ 2 × accuracy* governs an object in isolation. What
actually governs whether a fixation can be attributed is the distance to
the **next nearest candidate**.

Measured on the real geometry: two students 134 px apart at 124 px
accuracy. A fixation squarely on one is 186 px closer to it than to the
other and is attributable; only a fixation *between* them is ambiguous.

So the ceiling bites on **crowded parts of a scene**, not on whole
recordings, and the ambiguous share is reportable per session. This is
the study's central methodological contribution and should be stated up
front rather than discovered in the results.

## F10 · Keyframe cap silently truncated the LLM's input
**2026-08-11 · Methods (LLM pipeline), Limitations**

`LLM_MAX_FRAMES = 60` against a recording with **71 fixations**.
`sample_gaze_frames` keeps the **longest** fixations when it must drop
some, so the 11 dropped were the shortest — and nothing recorded which.

Human coding of that session: **88 % correct for fixations 0–61, 0 % for
62–70.** The cliff is the sampler, not the model: those frames were never
sent, so the claims scored against them were about nothing.

Raised to 200 on 2026-08-11 (covers a 30 s clip completely). The request
timeout now scales with the frame count — a constant 120 s was already
marginal at 60 frames. `frame_times`, `n_fixations_total` and
`frames_dropped` are now recorded per run, and the coding tool marks any
fixation the model never saw.

**Report the excluded-frame count for every session.** A capped input is
a property of the pipeline that a reader needs.

## F11 · First human-coded accuracy
**2026-08-11 · Results (RQ3)**

Session `13:47 11.08`, coder Jan, blind mode on, 71 fixations:

- **Excluding the 9 fixations the model never saw (F10): 88 % correct**
  (44 correct, 6 wrong, 12 unclear).
- Unclear share 16.9 % — the rate at which the instrument cannot
  adjudicate between two plausible objects. A property of the method,
  not of the model.

**One coder only, so there is no reliability estimate.** A coder who is
systematically generous produces the same number as a fair one. A second
coder on the same recording is required before this figure is reportable.

## F12 · Claims can describe a neighbouring frame
**2026-08-11 · Limitations (LLM pipeline) — OPEN**

Observed by the coder: "before the fixation it was saying it was the space
on the left, one screenshot before I looked left" — a claim describing the
*next* frame. Invisible per claim, because each one is individually
plausible.

Two candidate causes, not yet distinguished: the model **transcribing**
the `t=X.Xs` frame label incorrectly, or **answering** about the adjacent
frame. `alignment_check` in `claim_check.py` now tries every integer
shift between the claim times and the frames actually sent and reports
which fits best, only calling a shift when it fits at least twice as well
as none.

**Status: suspected, not confirmed.** Re-run the feedback and read the
ARE THE CLAIMS ALIGNED block before writing anything about it.

## F13 · The iris distance measurement can fail silently
**2026-08-11 · Methods (measures) — OPEN**

Session `HFP 9:40` reported `head_distance_cm = 76` with the source
blank. Blank meant the **iris** estimate had failed and the figure came
from GazeFollower's eye *rectangles*, whose centres are not guaranteed to
be pupil centres — the quantity the 6.3 cm inter-pupillary constant
describes.

This matters because every degree divides by the distance: a wrong 76 cm
makes the accuracy look better than it is.

`distance_source` is now always set and `iris_error` records why the
better ruler was unavailable. Most likely cause: GazeFollower's FaceInfo
carries the coarse 468-point mesh, in which the iris landmarks (468–477)
do not exist.

**Status: mechanism identified, not yet confirmed on a live run.**

## F14 · Camera focal length, measured
**2026-08-10 · Methods (apparatus)**

Tape-measured calibration at 60 cm, iris-based:

- `focal_px` **652.8**, implied horizontal FOV **52.2°**
- The 60° assumption previously in use was **13 % wide**, which made the
  tracker read a face at 60 cm as **50.9 cm** — an under-read in the
  direction that *inflates* every degree figure.
- Independent check: the pupil-based estimate gave 639.5 px, **2.1 %**
  apart. Repeatability across two runs: iris 12.79 vs 12.73 px (0.5 %).

---

## Open items before evaluation collection

- `EVALUATION_FROM_DATE` is empty: **every session so far is development
  data.** Set it on the first real collection day.
- No rubric has been supplied, so `criteria_met` is null throughout and
  the **evaluative half of RQ3 has no data at all**. Write it once and
  freeze it.
- No second coder, so no κ (F11).
- Drift is measured over 30 s clips only; re-measure on full-length
  stimuli.
- No concurrent-validity comparison against a research-grade tracker.
  State as a limitation.
