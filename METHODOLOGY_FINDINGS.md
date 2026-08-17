# Methodology findings

Append-only log of things that were **measured**, not assumed. Each entry
names the evidence and the chapter it belongs in, so the thesis can be
written from this file rather than from memory.

Rules for adding: state the number, state how it was obtained, state what
it licenses you to claim. If something was suspected but not confirmed,
say so in the entry — a log that mixes the two is worth nothing.

**F-numbers are unique, contiguous from F1, and never reused.** Entries
are cited by number from the chapters and from `CLAUDE.md`, so a
duplicate silently makes a citation ambiguous. `run_tests.py` section
[8] asserts this — it was added after a duplicate F22 (2026-08-12 and
2026-08-13) went unnoticed; the second was renumbered to F23 on
2026-08-15.

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

**AMENDED 2026-08-12 — the worked example below does not
reconcile; see F22. The principle stands, the derivation does not.**

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

**RESOLVED 2026-08-11.** The cause was confirmed: GazeFollower's
FaceInfo carries the coarse 468-point mesh, and the iris landmarks are
468-477 — they do not exist in it, so the better ruler was never
available. Both 2026-08-11 sessions therefore read ~75 cm from the eye
RECTANGLES, whose centres are not the pupil centres the 6.3 cm
inter-pupillary constant describes.

Fix: when the supplied landmarks are coarse, the tracker now runs its
OWN refined FaceMesh on the current frame. Affordable because it is on
demand at validation time, not per frame (~10 ms, once). Trades a
population mean applied to the wrong landmarks (~11 %, yaw-dependent)
for a physiological constant (iris 11.7 mm ± 0.5, ~4 %).

**Check on the next session:** `head_distance_cm` should report
`via iris`, not `UNKNOWN RULER`. If the measured distance moves away
from ~75 cm, every degree figure in the earlier sessions was scaled by
that error — 1.03° at 74.7 cm would be 1.28° at 60 cm.

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

## F15 · The metric report was inflating its own gap count
**2026-08-11 · (housekeeping, but it changes what the reports mean)**

`verify_metrics` listed **10 missing** metrics on a healthy session. On
inspection: 3 were duplicates of metrics reported PRESENT in the same
run (checked by two code paths — `saccade_count`,
`saccade_amplitude_median_deg`, `idt_min_duration_s`), 5 were AOI
metrics this study **deliberately does not collect**, and 1 was recorded
in the manifest under a key the checker did not read
(`idt_dispersion_deg`, in the events block).

**Exactly one was a real gap: `criteria_met`, because no rubric exists.**

Fixed: a metric found PRESENT can no longer also be reported MISSING;
AOI metrics report **N/A by design** with the reason from
`metrics_spec.NOT_APPLICABLE`; the dispersion threshold is read from
where it is written; and the summary now names what is outstanding
instead of only counting it.

Worth stating because a report that over-reports gaps is one that stops
being read — and the real gap was buried among nine phantoms.

---

## F16 · Stimulus design: two 30 s clips, differing in crowding
**2026-08-11 · Methods (materials) — DECISION**

This is a pipeline validation, not a study of teacher attention, so the
stimulus set is chosen to test the METHOD rather than to sample a
domain. Two 30 s clips, identical for every participant.

**Why two rather than one.** One clip cannot separate "the pipeline
works" from "the pipeline works on this clip", and F9 makes that a live
concern: attribution depends on how far apart the candidate objects
are, so a crowded scene and a sparse one should behave differently. Two
clips chosen to DIFFER on crowding turn a redundancy into a
manipulation with a testable prediction — correspondence should be
higher, and the ambiguous share lower, on the sparse clip.

**Why not more.** The precision gain is small and falls off fast:

| clips | fixations/participant | claims at N=10 | 95 % CI (worst case) |
|---|---|---|---|
| 1 | 70 | 699 | ±3.7 pp |
| 2 | 140 | 1398 | ±2.6 pp |
| 3 | 210 | 2097 | ±2.1 pp |

The second clip costs 30 s of participant time and buys both a scene
contrast and a third off the confidence interval. The third buys 0.5 pp
and no new contrast.

**Why 30 s specifically.** At the measured 2.33 fixations/s, the
200-frame cap binds above **86 s** of video (F10). A 30 s clip sends
~70 frames — comfortably inside, with no silent truncation.

**Order** is already counterbalanced: `_stimuli_for()` shuffles
deterministically per participant from a hash of the participant ID, so
roughly half see each clip first and the order is reproducible. The
actual presentation order is recorded per session in the manifest's
`stimuli` list, so it is auditable rather than assumed.

**Analysis note:** report correspondence PER CLIP as well as pooled.
Pooling a crowded and a sparse scene averages two different ambiguity
rates into a number that describes neither.

> **CORRECTED 2026-08-15 — the crowding contrast asserted above does not
> exist.** Measured in F25: the two selected clips differ by ~3 % in
> visible-face count and ~13 % in nearest-neighbour separation. The
> prediction "correspondence should be higher, and the ambiguous share
> lower, on the sparse clip" is **withdrawn**; there is no sparse clip.
> Two clips remain justified on the precision and
> not-a-single-clip grounds above, which are unaffected. Per-clip
> reporting also stands, but as a check that the result is not
> clip-specific — not as a manipulation. See F25.

---

## F17 · Collection boundary and data separation
**2026-08-11 · Methods (procedure) — DECISION**

`EVALUATION_FROM_DATE = 2026-08-11T14:00`. Everything before it is
development data; everything after counts toward the study.

**Why it carries a TIME, not just a date.** Collection starting "today"
must not sweep in the debugging sessions recorded that same morning —
and two were (`HFP 9:40` and `13:47 11.08`). A date-only boundary would
have promoted both into the evaluation set, which is precisely what the
constant exists to prevent.

**Physical separation.** Evaluation sessions are written to
`data/study/`, development ones stay in `data/gazefollower_raw/`, and
the routing is automatic from the session's own timestamp. Two folders
distinguished only by a date inside a filename is an analysis waiting
to pool them by accident, silently. Every analysis tool reads both
directories, so nothing goes half-blind the day collection starts.

The label a report prints and the folder a session is written to use
the SAME comparison — an earlier version compared date strings, under
which `"2026-08-11" < "2026-08-11T14:00"` is true and a session
recorded at 14:30 would have been filed as development while sitting in
the study folder.

## F18 · The replay payload is the file that must never be published
**2026-08-11 · Ethics / data management**

`app.py` writes the LLM request to `data/llm_replay/` **with the images
included**, so `model_comparison.py` can send a byte-identical prompt to
a second model. That is the right design for a fair model comparison —
rebuilding the prompt per model would confound model identity with
prompt drift — but it means those files hold base64 JPEG frames of the
classroom stimulus: identifiable people, in a school, on a public
repository if committed.

Three of these were staged for commit before anything caught it. What
caught it in the end was the *update guard* refusing to pull with a
dirty index, not a rule about the files themselves.

Fixed structurally rather than by name: the test suite now enumerates
every `data/` path the code writes to, and asks **git** — not a string
match against `.gitignore` — whether each is ignored. Anything neither
ignored nor on the short published allowlist (`data/shared/`,
`data/manifests_anonymised/`) fails the suite. It immediately found a
sixth directory nobody had listed, `data/agreement/`.

Related, and a different kind of hazard: `data/camera_geometry.json` is
a **per-machine** measurement. Sharing it is not a privacy problem, it
is a correctness one — a pull could replace the recording laptop's own
focal length with another machine's, and every distance, and therefore
every accuracy figure in degrees, would rescale with nothing visible in
the output to show it. Also excluded.

For the thesis: state that the audit log stores images as SHA-256
references and only the local replay file holds pixels, and that the
published artefact set is JSON metrics with pseudonymised labels.

## F19 · The model names correctly and localises badly — and the gap
## between strict and lenient proves the tracker is not the cause
**2026-08-11 · Results (RQ3) · PROVISIONAL, development session**

Session `13:47 11.08`, 59 claims, all 59 localised, tracker accuracy
0.90° out-of-sample:

| | |
|---|---|
| strict (gaze inside the claimed box) | **16.9 %** |
| lenient (adds misses smaller than the session's own error) | **28.8 %** |
| human coding of the same session, semantic | **88 % correct** |

The lenient rule adds only **11.9 percentage points**. That is the
informative part. If the tracker's error were responsible for the
misses, relaxing the criterion by exactly that error would recover
most of them; it recovers a fifth. **The misses are much larger than
the measurement error**, which exonerates the tracker without needing
a separate argument — and matches the earlier observation of shared
+160 and +404 px offsets.

Against that, a human watching the replay judged 88 % of the claims
correct. The two are not in conflict: they measure different things.
The human is asking *did it name the right object*; the correspondence
metric is asking *did it put the box where the participant looked*.

So the finding is: **the model identifies plausible attended content
but cannot localise it**, and a marker burned into the frame is enough
for it to name something without being able to place it. This is a
result about multimodal LLMs as an eye-tracking analysis instrument,
not a defect in the pipeline — and it is the direct justification for
the inverse check (`inverse_check.py`), where localisation is done on
CLEAN frames with no gaze present and the assignment is arithmetic.

Consequence for the design: report correspondence as a PAIR (strict,
lenient) with the accuracy that separates them, never as one number.
Treat the LLM's own boxes as evidence about the model, and the inverse
check as the attribution instrument.

The inverse check is post-hoc analysis over recorded fixations and
clean frames — it consumes nothing from the session protocol, so it
can be built after collection without splitting participants across
pipeline versions.

## F20 · Nothing about the face can be measured before calibration
**2026-08-11 · Methods (apparatus) · upstream behaviour**

`GazeFollower.process_frame` in SAMPLING state predicts gaze before it
dispatches anything, and raises when no calibration model has been
fitted:

```
gaze_info = self.gaze_estimator.detect(frame, face_info)
if gaze_info.status ...:
    calibrated, coords = self.calibration.predict(...)
    if not calibrated:
        raise Exception("No calibration model is available")
self.dispatch_face_gaze_info(face_info, gaze_info)   # never reached
```

So **FaceInfo is never delivered to subscribers before calibration** —
not merely the gaze. Consequences worth stating rather than
discovering: the pre-calibration positioning guide cannot use
GazeFollower's face geometry and falls back to its own detection; and
any diagnostic that wants face measurements before a calibration must
capture its own frames. Combined with the fact that GazeFollower never
persists a calibration between runs, there is no fitted model to borrow
either.

The head distance in the manifest is measured at the `pre_check`
validation, which is *after* calibration — so the iris ruler is
available where it matters. `tracker_service.py --distance` verifies
the measurement itself from its own capture, sharing the same
`refined_landmarks_for_frame` the session uses; the plumbing into the
manifest is confirmed by reading `head_distance_cm` on the first
session, which names its own ruler.

## F21 · The distance ruler was wrong, and the inclusion bar was kept anyway
**2026-08-11 · Methods (apparatus, inclusion criteria)**

The live probe measured the iris at **58.8 cm** median (100 % of frames,
on a measured focal length of 652.8 px). The same setup had been
recording **74.7 and 76.0 cm** from the inter-ocular fallback — a 27 %
overestimate.

Every accuracy figure is an angle, and `error_deg = error_px /
px_per_deg` with `px_per_deg ∝ distance`. A distance that is too large
makes the angle too small. Corrected, the two development sessions read
~1.31° and ~3.76° rather than 1.03° and 2.91°.

The inclusion threshold is in DEGREES, so 3.0° means the same thing
before and after; what changed is the measured values. **The threshold
was left at 3.0°** — a decision taken on 2026-08-11, after learning that
it had become stricter and that a known session would now fail it, and
before any evaluation session existed. Derivation: 3.0° = 175 px of
error, so 349 px is the smallest region it can resolve, and the four
rubric regions are separated by considerably more (F9).

The exclusion rate that follows is reported as a **result** — "webcam
eye tracking at this quality excludes N of M participants" is the kind
of number a method-validation study exists to produce — and not as a
parameter to tune until everyone passes.

Open: whether the 27 % gap is the ruler or a difference in posture
between the probe and the sessions. The first session's manifest
measures both rulers on the same frames and records
`distance_agreement_pct`, which settles it.

## F22 · The F9 worked example does not reconcile, and a mean error is not a bound
**2026-08-12 · Methods (measures), Results — OPEN, blocks F9 as written**

F9 states: *"two students 134 px apart at 124 px accuracy. A fixation
squarely on one is 186 px closer to it than to the other and is
attributable."* Four objections, none of which touch the underlying
principle:

1. **The arithmetic does not close.** Candidates 134 px apart, gaze
   exactly on one → the difference in distance to the two is 134 px, not
   186. The 186 figure is unexplained; plausibly a Euclidean separation
   from a 2D geometry whose components were never written down, in which
   case 134 px is not the quantity that should be compared to accuracy.
   As written the two numbers cannot both describe the same geometry.
2. **A mean is not a worst case.** 124 px is the mean of an error
   distribution — roughly half the samples exceed it. The example
   concludes attributability from `186 > 124`, which only follows if 124
   bounds the error. With a 134 px separation the perpendicular bisector
   sits ~67 px from each candidate, well inside the distribution.
3. **Recorded ≠ true position.** "A fixation squarely on one" is not
   observable; what is observed is a displaced estimate, and that
   displacement is the entire content of an accuracy figure.
4. **The error is not isotropic.** Vertical range compression (F5) and
   heteroscedastic vertical error are both documented here, so the
   scalar comparison behaves differently for horizontal than for
   vertical separations.

**The principle survives; the derivation must be rebuilt as signal
detection, not comparison.** For each pair of candidate regions, compute
the probability that the session's EMPIRICAL error distribution carries
the recorded point across their separating boundary; declare a fixation
ambiguous when that probability exceeds a threshold fixed in advance;
report the ambiguous share per session. Computable from validation data
already collected, handles anisotropy natively, and has a citation in
Orquin, Ashby & Clarke (2016), who make exactly this signal-detection
argument for AOI margins.

**Until then the example appears in no chapter and in no defence.** F9
is currently described as the study's central methodological
contribution, so shipping it with a derivation that does not close is
the single most examinable weakness in the project.

## F23 · Focal length calibrated over the operating range, and verified
**2026-08-13 · Methods (apparatus)**

A single-point calibration solves the focal length so that its own tape
reading comes out right; it cannot detect its own error. Five
independent calibrations were therefore taken across 45-65 cm and
pooled, since focal length is a property of the camera and must not
depend on how far away the person sat.

| tape distance | iris | focal if fitted alone |
|---|---|---|
| 45 cm | 16.38 px | 630.0 px |
| 50 cm | 15.56 px | 665.0 px |
| 55 cm | 13.91 px | 653.9 px |
| 60 cm | 12.91 px | 662.1 px |
| 65 cm | 11.66 px | 647.8 px |

**Adopted: 656.1 px, fitted over 50-65 cm.** Residuals -0.67, +0.18,
-0.54, +0.83 cm; RMS 0.61 cm; worst point 1.3 %. **Independently
verified at 58 cm — the distance participants actually sit at — to
0.9 %.**

**The 45 cm point is excluded, and this is stated rather than buried.**
It is the lowest individual focal in every subset and every fit
containing it is worse: all five points give 652.4 px at RMS 0.94 cm and
3.6 % worst, against 656.1 px at 0.61 cm and 1.3 % without it. The
declared reason for exclusion is range coverage, not fit quality --- 45
cm lies outside the range participants view from, and it is the closest
point, where perspective effects on the iris are largest and a fixed
tape error is proportionally biggest. Both fits are reported so a reader
can check the decision either way.

**A constant tape offset was hypothesised, tested, and rejected.** The
likeliest systematic error in this procedure is the ruler rather than
the optics --- measuring consistently to the screen surface rather than
the lens shifts every reading equally --- and its signature is a focal
length that grows with calibration distance, which the first two points
appeared to show. Fitted as a second parameter, the offset came out at
+3.70 cm on two points (an exact fit, testing nothing), +1.58 cm on
five, and **-4.36 cm** on four. A parameter whose sign depends on which
subset is used is fitting noise, and the single-parameter model is
retained.

Camera field of view implied by the fit is ~52-54 deg, against the 60
deg fallback the pipeline used before any calibration existed --- a
10-13 % error in every distance, and therefore in every angle, that the
calibration removes.

Secondary consistency check: the iris-derived and inter-ocular-derived
focal lengths disagree by 3.1 %, 4.4 % and 3.3 % at three of the
distances. That the disagreement is *stable* across distance is the
informative part --- both landmarks scale correctly, and a constant gap
is consistent with this participant's inter-pupillary distance sitting a
few per cent above the 6.3 cm population mean the inter-ocular path
assumes.

## F24 · Fixation counts and durations are an artefact of the sampling rate
**2026-08-15 · Methods (measures), Limitations — CONFIRMED, controlled test**

Halving the sampling rate roughly halves the fixation count and inflates
median fixation duration by ~70 %. This was measured on ONE session
(`fachschaft_2026-07-15_160219`, the clean 29.4 Hz exhibit) decimated in
software, so participant, stimulus, gaze data and detector parameters
are all held constant and the sampling rate is the only thing that
varies. Nothing about the eyes changed between these rows.

| retained | effective Hz | fixations | median duration | p90 duration | median samples | fixations/s |
|---|---|---|---|---|---|---|
| every sample | 29.4 | 276 | 0.240 s | 0.517 s | 8 | 2.41 |
| every 2nd | 14.5 | 127 | 0.407 s | 0.688 s | 7 | 1.11 |
| every 3rd | 9.7 | 100 | 0.443 s | 0.837 s | 5 | 0.87 |

Mechanism: I-DT accepts a window while dispersion stays under threshold.
Removing intermediate samples removes the evidence that the gaze left
the window, so genuinely separate fixations merge into one longer one.
The effect is a measurement artefact of the detector-plus-rate pair, not
a property of the viewer.

**Consequence, and it is a serious one.** Recorded sessions are bimodal
at 29 / 14.5 Hz, so **fixation count and fixation duration are not
comparable between a 29 Hz session and a 14.5 Hz session.** The observed
gap between `HFP_2026-07-16` (14.5 Hz, median 0.550 s) and `fachschaft`
(29.4 Hz, median 0.240 s) is largely reproduced by decimation alone
(0.407 s), so most of that between-participant difference is
instrumentation, not behaviour. Any novice-vs-expert contrast built on
fixation counts or durations across sessions of differing rate is
confounded by design.

**What this licenses.** (a) The rate gate stops being a quality nicety
and becomes a *comparability* precondition — for evaluation sessions the
rate must be reported per session and the analysis must either restrict
to one rate band or model rate as a covariate. (b) Every reported
fixation statistic must carry its session's effective rate. (c) Extends
F8 (precision is not comparable across sampling rates) from precision to
the fixation-level measures.

Reproduce: load one session's `gaze_video_nx/ny` + `video_time_s`, slice
`[::k]` for k = 1, 2, 3, run `fixations.detect_fixations` on each.

**Note this also settles an algorithm question.** Switching I-DT for a
different fixation classifier (I2MC and similar) cannot repair this —
the information destroyed by decimation is absent from the input, not
mis-segmented by the detector. Rate matching is the fix; a different
algorithm is not.

## F25 · The two stimulus clips are not a crowding manipulation
**2026-08-15 · Methods (materials), Analysis plan — CONFIRMED, corrects F16**

F16 selected two 30 s clips "chosen to DIFFER on crowding" and derived a
testable prediction from it: correspondence higher, ambiguous share
lower, on the sparse clip. Jan stated on 2026-08-15 that the clips are
"not super different". Measured, he is right and F16 is wrong.

Method: every 15th frame of each clip (50 sampled frames per clip),
OpenCV Haar frontal-face detection, identical detector and parameters
for both. Crowding operationalised two ways — how many faces are
visible, and how far apart the nearest two are, since F9 makes
*separation* the quantity attribution actually depends on.

| clip | faces/frame | mean nearest-neighbour | as % of frame width | centroid spread |
|---|---|---|---|---|
| `Stimuli_1_30s.mp4` | 7.92 | 111.1 px | 8.7 % | 133.6 |
| `Stimuli_5_30s.mp4` | 7.68 | 96.8 px | 7.6 % | 111.2 |

Both 1280×720 @ 25 fps. A 3 % difference in face count and a 13 %
difference in separation is not a manipulation; **neither clip is
sparse.** Both are crowded classroom scenes with roughly eight visible
faces about 100 px apart.

**Consequences.**
1. F16's prediction is withdrawn (annotated in place). Withdrawing a
   prediction before collection costs nothing; reporting it as a null
   result after collection would have cost a chapter.
2. The Methods sentence "pooling a crowded and a sparse scene averages
   two different ambiguity rates into a number that describes neither"
   is **unsupported and must be rewritten.** Per-clip reporting stays,
   justified as a check that the result is not clip-specific.
3. Two clips remain justified: precision (±3.7 → ±2.6 pp at N=10) and
   not resting the conclusion on a single clip. Those grounds never
   depended on the contrast.
4. **This corroborates the region-level decision.** Nearest-neighbour
   separation of 97–111 px sits below the 134 px used in F9's worked
   example and near the measured error scale (~1° ≈ 61 px, jitter RMS
   45–60 px). Face-level attribution would be unreliable in *both*
   clips, which is exactly why the rubric codes at region level.

**Limitation of the measurement.** Haar frontal-face detection misses
profile and back-of-head students, so absolute counts understate the
people present. Both clips were measured with the identical detector, so
the comparison is sound even though the absolute numbers are floors.

**If a real contrast is wanted**, it must come from re-selecting: five
source videos exist in `Stimuli/full_originals`, and the same measurement
run across all five would identify the widest-separation pair. That is a
stimulus-design decision to take before collection, not after.

## F26 - The region taxonomy does not match the stimuli, and both criteria are prevalence-skewed
**2026-08-16 - Methods (measures), rubric freeze - CONFIRMED, blocks the freeze**

The five-region scheme in `RUBRIC.md` was written without inspecting the
selected clips. Measured over both full clips (every 10th frame, Haar
frontal-face, identical parameters), plus visual inspection of sampled
frames:

| | Stimuli_1_30s | Stimuli_5_30s |
|---|---|---|
| face detections | 589 | 579 |
| face x (p5 / p50 / p95) | 0.18 / 0.44 / 0.79 | 0.18 / 0.40 / 0.67 |
| face y (p5 / p50 / p95) | 0.35 / 0.39 / 0.48 | 0.33 / 0.38 / 0.49 |
| faces above y = 0.30 | 0 (0.0 %) | 1 (0.2 %) |
| mean frame-to-frame change | 3.07/255 | 2.08/255 |

**1. TEACHER does not exist.** No face appears in the upper third of
either clip across the full 30 s. The camera faces the class, so any
teaching adult is behind it. Drop the category - it is not a judgement
call, it is absent. Confirmed by Jan from a frame: "no teacher, no
whiteboard".

**2. INSTRUCTIONAL SURFACE is nearly absent.** There is no board. The
only instructional content is three hand-drawn posters and an anatomical
model on the rear wall, roughly y in [0.13, 0.28] - a band about 0.15 of
frame height, which is only ~1.5-2x the vertical measurement error. Gaze
attributed there will be unreliable, so expect a high UNRESOLVABLE share
for that region specifically.

**3. The scene is three horizontal bands.** Faces occupy y in
[0.33, 0.49]; with bodies the student band is roughly y in [0.30, 0.60],
x in [0.13, 0.85]. Above it: wall, posters, window, ceiling truss.
Below it: desks, bags, floor. Static camera. Polygon drawing is
therefore trivial and stable across the whole clip - one set per clip,
no tracking needed.

**4. Both surviving criteria are prevalence-skewed, in opposite
directions.** Students are the only people in frame, centrally placed,
and carry essentially all the salient content, so C1 (">50 % of
resolvable gaze on STUDENTS") should be TRUE in the large majority of
windows. C3 ("two regions each >=20 %") then fails for the same reason -
if nearly all gaze is on students, a second region rarely reaches 20 %.
Skew in either direction deflates kappa (Byrt et al. 1993; Norman et al.
2026 measure 33-41 pp), so as written **both criteria risk producing an
uninterpretable headline number regardless of how well the model
performs.**

**Option worth deciding before freeze:** partition WITHIN the student
band instead of around it. Face x spans 0.18-0.79, so three blocks of
~0.2 normalised width are ~256 px each - about 4x the horizontal error
and comfortably above the 134 px F9 uses. A distribution criterion over
LEFT / CENTRE / RIGHT student blocks has genuinely balanced prevalence,
because some viewers scan and some fixate one area. That is a rubric
design decision for Jan, not a code change.

**Reproduce:** `cv2` Haar over both clips, every 10th frame, record
normalised face centres.

## F27 - Design A: naming accuracy replaces rubric agreement
**2026-08-16 - Design decision, supersedes RUBRIC.md and the kappa plan**

The study now asks whether the LLM **correctly names the region at the
reported gaze location**, judged by a human coder against the same
annotated frames. Percent agreement plus a disagreement analysis. The
C1/C2/C3 rubric, Cohen's kappa, the human-human ceiling, prevalence and
bias indices, PABAK and the participant bootstrap are all **dropped**.

**Why kappa went.** Kappa corrects for chance agreement between two
judges exercising *judgement* against a standard. Naming what is at a
location has a right answer, so the correction answers a question the
design no longer asks. F26 also showed both surviving criteria were
prevalence-skewed in opposite directions, which would have deflated
kappa regardless of model performance.

**The claim boundary, which is load-bearing.** Two bounded claims that
compose:
1. instrument accuracy, from the validation targets, in degrees;
2. LLM scene-reading, from coder-vs-LLM on identical annotated frames.

The wording is "the LLM correctly names the region **at the reported
gaze location**", never "what the participant looked at". The gap
between those sentences IS claim 1, and collapsing them is the most
likely way this design fails at a defence.

**Known weakness, to be stated in Limitations.** Coder and model read
the same annotated frame, so a systematically displaced marker produces
*agreement on the wrong object* and is invisible to the comparison. The
only independent check on the gaze is the validation-target accuracy.
Region AOIs would have supplied a **computed** unresolvable rate - per
sample, whether the gaze fell close enough to a boundary that
attribution is unreliable - and were rejected as too time-consuming to
draw. Partial mitigation at no cost: the coder marks UNCLEAR when they
cannot tell, making the unclear rate a measured rather than computed
property (F11 measured 16.9 % at fixation level).

**Correction to an earlier cost claim.** Coding was priced against
rubric judgements, which are slow. A naming call is fast, so
"coding is quicker" is defensible for THIS task even though it was not
for the previous one.

**Consequences in code - all three rubric blockers are void.**
The unit is the **fixation**, already produced by the pipeline and
already the UI default (`detail=fixations`). So: no `windows` option
needed in `review.html`; no per-criterion `criteria_met` dict, meaning
the four silent call sites (`app.py:1641`, `verify_metrics.py:539`,
`model_comparison.py:315`, `agreement_kit.py:114,190-195`) must be **left
alone**; C2's event list is moot. `verify_metrics` reporting `aoi_*` as
"N/A by design: no hand-drawn AOIs" is now simply true.

**Open, before freezing:** how many fixations per participant are coded
(~70 per 30 s clip makes all of them infeasible; 20 gives ~300 items at
N=15), and the selection rule - which must be mechanical (every k-th, or
a seeded draw), never "the interesting ones".

## F28 - Coding protocol: census, RIGHT/WRONG/UNCLEAR, and the anchoring correction
**2026-08-16 - Methods (coding protocol) - DECISION**

Settled with Jan on 2026-08-16, completing F27.

**Census, not a sample.** EVERY fixation is coded: ~72 per 30 s clip at
2.4 fixations/s, ~145 per participant, ~2200 items at N=15. Rationale
worth one sentence in Methods: a census has no selection rule, so
selection bias is not available as a competing explanation; and the model
already labels every fixation (F10's keyframe cap exceeds the ~70 a 30 s
clip produces), so sampling would discard output already generated.

**N is not fixed in advance.** Recruit for the whole collection window
and report the achieved N. Legitimate because nothing is powered for a
hypothesis test. **The exclusion rules are a different matter and must
still be pre-specified** - the rate gate, the 3.0 deg inclusion limit and
the 60 % valid-sample floor - because choosing a threshold after seeing
which sessions it removes is unfalsifiable.

**Response options: RIGHT / WRONG / UNCLEAR.** UNCLEAR is reserved for
"cannot tell which of two candidate regions the marker sits on" and is a
substantive response, not an abstention - its rate is the study's measure
of how often the method resolves attention at all (F11 measured 16.9 % at
fixation level).

**Two additions the format obliges.**
1. *Free text on WRONG* - what the region actually was. Fires only on the
   minority of items and preserves the failure analysis, which is the
   contribution; a bare WRONG flag loses what F19 found (names at 88 %,
   localises at 16.9-28.8 %).
2. *A blind-first subset* - RIGHT/WRONG/UNCLEAR is a VERIFICATION task,
   so the coder sees the model's answer before responding. This is
   inherent to the format, not a procedural lapse, and it biases accuracy
   upward: raters accept a plausible label more readily than they would
   generate it. On a subset the coder names the region before the answer
   is revealed; the difference on the same items estimates the anchoring
   inflation. Both figures reported, never one alone.

**Fatigue controls a census requires:** fixed-length blocks, randomised
participant order, coding date recorded per block, and ~100 items
re-coded blind after an interval for intra-coder consistency.

**Non-independence.** Consecutive fixations often share a region, so
~2200 judgments are not 2200 independent observations. Resample
PARTICIPANTS for every interval; the effective sample size is N.

## F29 · Gaze during the stimulus is biased UPWARD, and the validation cannot see it
**2026-08-17 · Results, Limitations — the study's most consequential finding so far**

> **CORRECTED 2026-08-17, same day.** The observation below is real and
> the direction is right. **The explanation and the numbers were wrong,
> and are superseded by F30.** The evidence originally offered — a
> comparison of validation-period and stimulus-period gaze medians,
> giving shifts of 154–211 px — is confounded and cannot support the
> claim: the validation *instructs* fixation at targets placed
> symmetrically about screen centre, so its median is fixed by the task
> rather than by the tracker. Comparing it with a free-viewing median
> measures the difference between the two tasks, not a tracker bias.
> The mechanism proposed — that a dark validation screen and a bright
> video present different eye images to an appearance-based estimator —
> **remains untested** and is now a separate open question, not this
> finding's explanation.
>
> The signed per-target error, which does support the claim, is in F30.
> Nothing below is deleted, because a log whose corrections are invisible
> cannot be audited.

Reported by the participant first: "all the eye gaze was above the
people, even though I never looked above", together with "I thought the
accuracy check was better because the green dot was very close". Both
observations are correct, and together they identify the problem.

**SUPERSEDED — the comparison below is confounded; see the correction above.** Splitting each session's gaze CSV by the stimulus triggers:

| | validation median | stimulus median | shift | in degrees |
|---|---|---|---|---|
| Julianne P1 | 553 px | 342 px | −211 px | **−3.64°** |
| Manuel P2 | 629 px | 465 px | −164 px | **−2.83°** |
| PILOT_02 | 503 px | 348 px | −154 px | **−2.66°** |

All three shift the same way, by roughly the same amount. **The bias
during viewing is as large as, or larger than, the validation accuracy
those same sessions report** (1.65–2.82°).

The tracker is not broken and the calibration does not drift: the
validations bracketing the stimuli both span the full screen (p10–p90 of
730–900 px) and centre near 500–630 px. Horizontal position is
unaffected — the stimulus-period median x is 961 px on a 1920 px screen.
The bias is vertical, appears only while the video plays, and disappears
again afterwards.

**SUPERSEDED — why the two-grid protocol was thought not to detect it.** (It does detect it, once the error is reported SIGNED. See F30.) Validation measures
accuracy against dots on a dark screen; the stimulus is a bright,
full-frame classroom video. Different luminance, different eye image,
and an appearance-based estimator cannot distinguish "the eye looks
different because of the light" from "the eye looks different because it
moved". The protocol was built to make accuracy honest about
generalisation across TARGET POSITIONS; it says nothing about
generalisation across VIEWING CONDITIONS, and the second gap is the
larger one.

**Consequence for the inclusion figure.** The reported accuracy
describes the validation task, not the recording. A session admitted at
1.65° may carry a systematic 2.7° vertical offset through both clips.
Every downstream spatial claim — region assignment, correspondence with
the model's boxes, the ambiguity rate — inherits it.

**Caveat, stated because it weakens the compression half of the
result.** The gaze range also narrows during viewing (span ~330 px vs
~750 px). That is partly expected and not evidence of anything: the
validation *instructs* fixation at 12 % and 88 % of screen height, while
video content sits centrally. The SHIFT is the robust finding; the
compression is confounded with the task.

**The test that would measure it directly:** overlay validation targets
on a playing clip, or on a still frame from one, so that luminance and
content match the stimulus condition, and compare against the same
participant's dark-screen validation. That is a measurement nobody in
this literature appears to make, and it converts a limitation into a
number.

---

---

## F30 · The gain correction was overfitted, and the offset it removes does not hold still
**2026-08-17 · Methods, Results, Limitations — supersedes the explanation in F29**

The participant reported it first: *"all the eye gaze was above the
people, even though I never looked above"*, together with *"I thought the
accuracy check was better because the green dot was very close"*. Both
are correct and they are the same fault seen from two sides.

### 1. No reported quantity was signed

Accuracy was reported as the **mean unsigned distance** between measured
gaze and target. That statistic reads identically for

    scattered 60 px in random directions   →  mean error 60 px
    displaced 60 px upward, every sample   →  mean error 60 px

and the two are not interchangeable. Scatter averages out of any
aggregate; a displacement moves every gaze point the same way, so region
assignment, correspondence with the model's boxes and the ambiguity rate
all inherit it. A participant noticed before any metric did, because
nothing anywhere in the pipeline was signed.

Measured per target, on the two sessions carrying per-target records
(`correction_audit.py`):

| session | phase | basis | mean px | median px | bias (x, y) | \|bias\| | bias / error |
|---|---|---|---|---|---|---|---|
| Manuel_P2 | pre_fit | raw | 96.5 | 84.5 | (−32.0, **+72.4**) | 79.2 | 0.82 |
| Manuel_P2 | pre_check | raw | 65.8 | 56.3 | (−58.4, +20.1) | 61.8 | 0.94 |
| Manuel_P2 | pre_check | corrected | 61.6 | 65.5 | (−27.7, **−52.3**) | 59.2 | 0.96 |
| Manuel_P2 | post | corrected | 185.7 | 97.9 | (+76.0, −2.7) | 76.0 | 0.41 |
| PILOT_02 | pre_fit | raw | 79.0 | 81.6 | (+34.1, **+37.1**) | 50.5 | 0.64 |
| PILOT_02 | pre_check | raw | 113.9 | 111.6 | (+94.0, −34.8) | 100.2 | 0.88 |
| PILOT_02 | pre_check | corrected | 112.2 | 116.4 | (+55.0, **−71.4**) | 90.2 | 0.80 |
| PILOT_02 | post | corrected | 80.5 | 57.6 | (+49.0, +1.7) | 49.0 | 0.61 |

**80–96 % of the out-of-sample error is a fixed displacement, not
scatter.** Negative y is above the target: the corrected pre-check sits
0.9–1.2° above where the participant was looking, which is what they
reported.

Two claims in the original brief do not survive this table. The bias is
**not** vertical only — PILOT_02's horizontal bias exceeds its vertical
in every phase. And the signed bias on the FIT grid after correction is
exactly (0.0, 0.0) in both sessions, which is not a measurement: least
squares zeroes its own mean residual. It is reported as `in_sample` and
must never be quoted as evidence.

### 2. The correction is overfitted

Seven targets, two free parameters per axis, applied unconditionally:

| session | grid A (the fit set) | grid B, `pre_check` | grid B, `post` |
|---|---|---|---|
| Manuel_P2 | 96.5 → 37.4 px (**−61 %**) | 65.8 → 61.6 px (**−6 %**) | 205.2 → 185.7 px (−10 %) |
| PILOT_02 | 79.0 → 45.8 px (**−42 %**) | 113.9 → 112.2 px (**−1 %**) | 109.4 → 80.5 px (−26 %) |

It removes 42–61 % of the error where it was fitted and 1–6 % where it
was not.

**This was visible from grid A alone.** Leave-one-out on the seven fit
targets — predict each from a model refitted without it — gives 56.8 px
for Manuel_P2 against 37.4 px refit, and 67.6 px for PILOT_02 against
45.8 px. The gap between the LOO figure and the refit figure *is* the
overfitting. It required no second grid, no second validation and no
participant. Three participants were spent learning something a saved
CSV would have shown.

### 3. Why it cannot generalise: the offset moves

Change in RAW signed bias between consecutive checks, tested against the
target-to-target scatter each mean was estimated from (Welch, per axis):

| session | interval | gap | axis | shift | evidence |
|---|---|---|---|---|---|
| Manuel_P2 | pre_fit → pre_check | 21 s | dy | +72.4 → +20.1 px | 3.1 SE |
| PILOT_02 | pre_fit → pre_check | 40 s | dy | +37.1 → −34.8 px | 2.7 SE |
| PILOT_02 | pre_fit → pre_check | 40 s | dx | +34.1 → +94.0 px | 2.0 SE |
| PILOT_02 | pre_check → post | 90 s | dy | −34.8 → +41.4 px | 2.8 SE |

The systematic offset changes by **0.9–1.3° in 21–40 seconds**, and the
change is larger than the noise its own estimate carries. Manuel_P2's
fitted vertical polynomial was `[1.0008, −72.9]` — a gain of 1.000 and a
pure −73 px offset. The correction subtracted a snapshot of a moving
quantity, at a moment when the quantity had already moved, and that
subtraction is most of the upward displacement the participant saw.

**No session-wise correction can fix this**, and the measurements say so
directly. Out-of-sample \|bias\| on `pre_check` under every candidate
model, including no correction at all:

| session | none | affine | quadratic-vertical |
|---|---|---|---|
| Manuel_P2 | 61.8 px | 59.2 px | 61.7 px |
| PILOT_02 | 100.2 px | 90.2 px | 89.5 px |

The correction rotates the residual — Manuel_P2's (−58, +20) becomes
(−28, −52) — without shrinking it.

### 4. The rule, fixed 2026-08-17, before application to any session

**Previous rule:** fit on grid A, apply unconditionally, no check that it
generalised.

**New rule** (`validation_stats.select_correction`): fit
{none, affine, quadratic-vertical} on grid A and select by **leave-one-out
cross-validation on grid A**. A candidate is accepted only if it beats
no-correction on BOTH the mean 2-D error — by more than one standard
error of the paired per-target difference, because on seven targets a
5 % gain is inside the noise — and the magnitude of the signed bias,
which must not increase. Ties and near-ties go to the simpler model. The
chosen model, every candidate's figures, the criterion and the date it
was fixed are written to the manifest as `correction_decision`, so a
session whose correction was fitted and REJECTED is distinguishable from
one where none was ever fitted.

**Selection happens on grid A, not grid B, and this is the point.**
Choosing the model on `pre_check` would spend the only held-out
measurement the two-grid protocol produces: `pre_check` would become
in-sample-for-selection and could no longer be reported as the corrected
accuracy.

**What the rule does to the recorded sessions**, stated because it is a
change to a pre-specified analysis step made after seeing data:

* **Manuel_P2 → affine**, i.e. the correction already applied. Nothing
  changes. Its −52 px upward out-of-sample bias survives, because
  nothing can remove it.
* **PILOT_02 → none.** The affine fit beat no-correction by 11.4 px
  under LOO, which is 0.8 SE, below the 1.0 SE bar. Its recorded gaze is
  re-derived uncorrected.

**Disclosed, because it matters:** on grid B that rejected correction was
in fact helping PILOT_02 (`post` 109.4 → 80.5 px, \|bias\| 97.0 → 49.0).
The rule is conservative and in this instance it is conservative in the
wrong direction, judged against a set it is forbidden to consult. The
margin was fixed before the figure was computed and has not been moved
since; 0.8 SE is close enough to the bar that the alternative should be
stated rather than buried. With two sessions, one instance decides
nothing.

### 5. What this costs the thesis

The instrument carries a **systematic directional offset of roughly
1.0–1.7°** on the out-of-sample check, which recalibration does not
reduce, in addition to scatter. That belongs in the accuracy claim and in
the limitations.

It bears directly on the region rubric. `metrics_spec.min_aoi_px` derives
the smallest resolvable AOI from accuracy on the assumption that error
can move a gaze point either way across a boundary. A *systematic* 1.5°
displacement is worse than 1.5° of isotropic noise: it moves every point
in the same direction, so it does not average out over a fixation, over a
clip, or over participants, and it will push gaze consistently into
whichever region lies above the true one. The 3.0° inclusion threshold
admits sessions whose systematic component alone is half the bar.

### 6. Code

* `validation_stats.py` — new. Signed bias, robust accuracy, the
  cross-validated selection rule, and numeric inversion of any monotone
  correction.
* `correction_audit.py` — new. Both-ways table, the LOO diagnostic and
  the stability test, for any manifest.
* `app.py` — `_auto_fit_correction` selects instead of assuming;
  `_uncorrected_error` now inverts a quadratic instead of refusing it
  (it previously reported `raw_available: False`, which would have
  dropped any quadratic-fitted session out of both the rule and the
  comparison); validation records and the manifest carry the signed
  bias and the decision.
* `verify_metrics.py`, `show_validations.py`, `templates/review.html`,
  `metrics_spec.py` — every place an error is reported now reports the
  signed bias beside it and flags offset-dominated error.
* `run_tests.py` — section [7b], 27 new checks. All six deliberate
  mutations of the new logic are caught.

**Sessions recorded before today have no `correction_decision` and no
recorded bias.** Every tool says so explicitly rather than showing a
blank, and `correction_audit.py` derives both from the per-target records.

---

## F31 · Validation accuracy is a seven-target mean with no rejection rule
**2026-08-17 · Methods, Limitations — found while measuring F30**

Manuel_P2's post-stimulus validation contains a target at (960, 540)
measured at (1609, 479): **dx = +649 px**, on a 1920 px screen. Two
others read dy +235 and +169. Per-target errors for that phase:

    89, 68, 165, 634, 190, 98, 57 px

The reported accuracy is the mean of seven such numbers, with no outlier
rule anywhere in the pipeline. For that phase:

| basis | mean | median |
|---|---|---|
| raw | 205.2 px = **3.52°** | 140.4 px = 2.41° |
| corrected | 185.7 px = **3.19°** | 97.9 px = 1.68° |

The inclusion threshold is 3.0°. Whether that phase passes turns on
whether one blown target is counted. The session is admitted anyway
because the canonical figure averages `pre_check` with `post`
(2.12°) — which means the criterion's behaviour on a session with one
bad target depends on a second measurement, not on a rule.

**No rule is imposed here.** Both figures are now reported everywhere,
plus the worst single target, and `verify_metrics.py` flags any phase
where mean and median differ by more than 25 %. Which figure the
inclusion criterion uses, and whether a per-target rejection rule exists,
is a pre-registration decision and must be made deliberately and dated —
not settled by whichever choice rescues a session already recorded.

Three candidates, for that decision:

1. **Report both, keep the mean.** Honest, changes nothing, leaves the
   leverage in place.
2. **Median becomes canonical.** Robust, but chosen after seeing which
   session it rescues.
3. **Pre-declared per-target rejection** (e.g. error > 3× the median, or
   fewer than N samples on that target), recompute the mean, record every
   dropped target. Keeps the mean, removes the single-point leverage.

The reason a single target can be that wrong is itself unmeasured: 45
samples were collected at it, so it is not a sampling failure. A blink, a
glance away, or a momentary tracking loss are all consistent with the
data available, and none is currently recorded.


## Open items before evaluation collection

- ~~`EVALUATION_FROM_DATE`~~ **SET to 2026-08-11T14:00** (F17).
- No rubric has been supplied, so `criteria_met` is null throughout and
  the **evaluative half of RQ3 has no data at all**. Write it once and
  freeze it.
- No second coder, so no κ (F11).
- Drift is measured over 30 s clips only; re-measure on full-length
  stimuli.
- No concurrent-validity comparison against a research-grade tracker.
  State as a limitation.
- F9's attribution derivation is broken (F22). Rebuild it as a
  probability over the empirical error distribution before any chapter
  or defence uses it.
- **Fixation measures are rate-confounded (F24).** Decide before
  collection whether evaluation sessions are restricted to one rate band
  or whether rate enters the analysis as a covariate, and pre-specify it
  in `config.py` with the other thresholds. Comparing fixation counts or
  durations across rate bands is not defensible as things stand.
