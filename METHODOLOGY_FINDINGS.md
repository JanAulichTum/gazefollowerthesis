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

---

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
