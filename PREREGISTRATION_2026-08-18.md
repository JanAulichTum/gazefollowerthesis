# Pre-registration addendum — 2026-08-18

**Status: DRAFT for sign-off. Not in force until dated and signed below.**

Settles the analysis and exclusion rules left open after F30–F38, and
records one scope decision that removes most of what was outstanding.
Supersedes nothing in `metrics_spec.INCLUSION` except where stated; each
item names what it replaces.

Every rule here is stated **with what it does to the sessions already
recorded**, because a rule whose effect on existing data is not disclosed
cannot be distinguished from a rule chosen to produce that effect.

---

## 0 · READ FIRST — the sessions recorded so far are PILOT data

`EVALUATION_FROM_DATE` currently reads `2026-08-11T14:00`. Every session
recorded to date falls after it, which would make all of them evaluation
data and would mean the rules below were written with knowledge of them.

That is not tenable, and it is also not what happened. The protocol
changed materially on **2026-08-18**: validation grid A went from 7 to 13
targets, a full-affine correction candidate was added, and head position
began being captured. Sessions recorded before that ran a different
instrument and cannot be pooled with sessions recorded after it,
independently of any question about pre-registration.

**Decision required:** reset `EVALUATION_FROM_DATE` to the date this
document is signed. Every session to date — Julianne_P1, Manuel_P2,
PILOT_00 through PILOT_07 — is reclassified as pilot data and reported as
such. Pilot data may inform the thresholds below; that is what pilots are
for. It may not be pooled with evaluation data.

☐ Agreed — `EVALUATION_FROM_DATE` = ______________

---

## 1 · Scope: RQ3 is a correspondence claim, not an evaluation claim

**Decided 2026-08-18.** The rubric-based evaluation of teaching quality
(`RUBRIC.md` C1/C2/C3, the four region categories, the event list, the
Gini anchor) is **out of scope and dropped**.

RQ3 asks one question: **does the model's account of what the participant
looked at correspond to where the gaze actually was?**

This is consistent with the scope already recorded in `CLAUDE.md`
(2026-08-16): *"a METHOD proof of concept… the classroom domain is the
vehicle, not the subject. There is NO novice-vs-expert contrast and no
domain hypothesis."* The rubric had reintroduced a domain hypothesis by
the back door — judging whether attention was *well* distributed is a
claim about teaching, not about the instrument.

**What this removes:** the AOI question in its entirety, the region
vocabulary, the C2 circularity and its event list, the Gini anchor, and
the "top-band AOI constraint" reading of F37.

**What now carries RQ3:** `claim_check.py`. The model reports *what* was
attended **and** a normalised bounding box for *where* it is; the gaze
coordinates are an independent measurement; the box either contains the
gaze or it does not. `claim_correspondence_pct` and `claim_testable_pct`
are the headline figures. No AOIs are drawn at any point.

`RUBRIC.md` is retained, headed with a dated note recording that the
evaluative half was considered and descoped, and why.

---

## 2 · Inclusion criteria

### 2.1 The accuracy threshold is measured in degrees on the MEASURED viewing distance

**Replaces:** the implementation that used `mean_err_deg`, the browser's
figure, which divides by a hardcoded 60 cm.

The threshold stays **3.0°**, unchanged, on the canonical figure already
defined (`mean of pre_check and post, both grid B`). The change is which
quantity that figure is computed from: `mean_err_deg_measured`, derived
from the iris distance measured at validation time.

**Why this is a correction, not a revision.** `metrics_spec` already
describes `mean_err_deg_measured` as authoritative and the browser figure
as "retained only for comparison". The 3.0° threshold was reaffirmed on
2026-08-11 *knowing* the iris ruler made it stricter. The rule was
already written this way; only the code disagreed.

The point estimate decides. The interval (`mean_err_deg_lo/_hi`,
reflecting the ~4.3 % distance uncertainty) is reported beside it. A
session whose interval spans 3.0° is reported as borderline and is not
re-tested on any other basis.

**Effect on recorded sessions — no verdict changes.**

| session | browser | **measured** | verdict |
|---|---|---|---|
| PILOT_00 | 1.44° | **1.25°** | pass |
| PILOT_02 | 1.65° | **1.56°** | pass |
| PILOT_01 | 1.92° | **1.57°** | pass |
| PILOT_07 | — | **1.68°** | pass |
| PILOT_04 | 2.04° | **1.75°** | pass |
| PILOT_06 | — | **2.12°** | pass |
| Manuel_P2 | 2.12° | **2.31°** | pass |
| PILOT_05 | 2.82° | **2.66°** | pass |
| Julianne_P1 | 2.46° | **2.82°** | pass *(reconstructed; no per-target data)* |
| PILOT_03 | 4.19° | **3.88°** | **FAIL on both** |

The one exclusion is unaffected by the choice. This is stated because it
is the argument for deciding now: the rule costs nothing today, which is
the only moment at which it can be adopted without suspicion.

### 2.2 Per-target rejection is by CAUSE, not by error size

**New rule.** A validation target is excluded from a phase's mean if it
collected **fewer than 25 gaze samples**. Excluded targets are recorded
individually in the manifest, with their count.

**Why a sample floor and not an error threshold.** Rejecting a target
because its error is large is selecting on the quantity being measured.
The motivating case (Manuel_P2, one target 652 px from its mark)
collected 45 samples — a full complement — so no cause-based rule catches
it, and no outcome-based rule is defensible. The honest position is that
the two cannot be told apart from the data available.

**Effect on recorded sessions: none.** Across 147 targets in 21 validation
phases the observed range is **39–47 samples** (median 43). A floor at 25
excludes **0 of 147**. It exists to catch a collection failure, not to
filter results, and its having no effect to date is the evidence for that.

### 2.3 The mean stays canonical; the median is a declared sensitivity analysis

**New rule.** The reported accuracy is the **mean** of the surviving
per-target errors, as now. The **median** is computed and reported
alongside for every phase, and a sensitivity analysis in the appendix
reports every figure on the median basis, **naming any session whose
inclusion verdict differs between the two**.

No session's verdict is decided by the median. This exists so that the
leverage a single target holds is visible rather than silent — Manuel_P2
reads 3.52° on the mean and 2.41° on the median for the same phase.

### 2.4 Two rate floors, for two purposes

**Replaces:** the single `min_sampling_hz = 20.0` applied to everything.

- **Session inclusion: ≥ 20 Hz**, unchanged.
- **Fixation and saccade measures: ≥ 28 Hz** on the stimulus in question.
  Below that, fixation count, fixation duration, fixation rate, saccade
  count and saccade amplitude are **not reported** for that stimulus.
  Session-level and correspondence measures are unaffected.

**Why two.** F24 established that fixation count is biased down roughly
threefold at 21 Hz relative to 30 Hz. That confound is specific to
event-detection measures; it does not touch accuracy, data loss, or claim
correspondence. Excluding a whole session over it would discard usable
data.

**Effect on recorded sessions: none.** All 14 stimulus recordings sit at
**30.3–31.2 Hz**. The floor exists so that a session that drops back into
the ~14.5 Hz mode this pipeline used to exhibit (before the EcoQoS fix)
cannot silently enter the fixation analysis.

---

## 3 · The correspondence test

### 3.1 Tolerance is applied as a SHIFT then a pad, not a symmetric expansion

**Replaces:** `claim_check._expand()` padding each bounding box
symmetrically by the session's mean unsigned error.

For each session, before testing any claim:

1. The recorded gaze is **shifted by the session's measured signed bias**
   (`bias_x_px`, `bias_y_px` from the canonical grid-B checks).
2. The bounding box is then padded by the **residual** scatter — the part
   of the error not explained by that offset — not by the full unsigned
   error.

**Why.** F30–F36 measured that **80–96 % of the out-of-sample error is a
systematic displacement, not scatter**. A symmetric pad models the error
as isotropic noise, which it is not. Padding a box by 130 px in every
direction when the gaze is displaced 120 px in one of them is
simultaneously too generous — inflating `claim_correspondence_pct`, the
headline figure — and still able to fail a correct claim on the opposite
edge.

**This will change the headline number, possibly substantially, and in an
unknown direction.** It is adopted because the current model contradicts
the measured error structure, not because of its effect, which is not yet
known. The figure under both models will be reported once, at first
application, so the size of the change is on the record.

### 3.2 Gaze in the top band makes a claim UNTESTABLE

**New rule, from F37.** The top **12 %** of screen height undershoots by
0.77°–5.62° in every session with per-target data. A claim whose gaze
samples fall predominantly in that band is scored **UNTESTABLE**, not
SUPPORTED or CONTRADICTED.

The samples are **not** discarded from the recording. Removing gaze where
the instrument misbehaves would remove the evidence of the fault and
change what the accuracy figures describe.

The share of claims scored UNTESTABLE for this reason is reported. If it
is large, that is a finding about the instrument's usable field, not a
nuisance.

### 3.3 Claims smaller than the tolerance remain UNTESTABLE

Unchanged from the existing implementation, restated for completeness: a
claim whose bounding box is smaller than the session's tolerance cannot
be tested and is reported as such rather than passed or failed.

---

## 4 · The agreement study

**Unit: one LLM claim.** The coder is shown the marked video for the
claim's time window together with the claim text, and answers
**RIGHT / WRONG / UNCLEAR**, with free text on WRONG naming what the
marker was actually on.

**Replaces:** F28's fixation-level census (~2200 items at N=15), which
was designed for the rubric that §1 drops. Claim-level coding matches
what `claim_check.py` tests, so κ(human, claim_check) is directly
interpretable, and the item count is tractable.

UNCLEAR is a substantive response, not an abstention: its rate is the
study's measure of how often the method resolves attention at all.

Reported: κ(human, LLM) on the RIGHT/WRONG judgement, the UNCLEAR rate
for each rater, and the free-text failure analysis. Coding is a census of
all claims in the included sessions — no sampling rule, so no selection
bias is available as a competing explanation.

---

## 5 · What is superseded

| superseded | by | note |
|---|---|---|
| `mean_err_deg` as the inclusion figure | §2.1 | implementation never matched the declared rule |
| single `min_sampling_hz` for all measures | §2.4 | session floor unchanged at 20 Hz |
| `RUBRIC.md` C1/C2/C3 and region vocabulary | §1 | retained, marked superseded |
| F28's fixation-level coding census | §4 | designed for the dropped rubric |
| symmetric tolerance expansion in `claim_check` | §3.1 | contradicts the measured error structure |
| "no hand-drawn AOIs" as a live tension | §1 | moot; no AOIs of any kind are used |

---

## 6 · Open, and deliberately not settled here

- **F9 / F22** — the attribution derivation is broken and must be rebuilt
  as a signal-detection quantity before any chapter or defence cites it.
  Analysis-time; does not block collection.
- **PILOT_06 re-derivation** — recorded with an affine correction; the
  current rule selects full-affine. Low stakes, still outstanding.
- **N** — not fixed in advance. Recruit for the collection window and
  report the achieved N. Legitimate because nothing here is powered for a
  hypothesis test; the exclusion rules above are pre-specified, which is
  the part that matters.

---

## 7 · Sign-off

These rules are fixed on the date below and apply identically to every
session recorded after it. Any later change is recorded as a new dated
entry in `METHODOLOGY_FINDINGS.md` stating the previous rule, the new
rule, and the reason — never edited in place.

Signed: ______________________  Date: ______________

Supervisor notified: ☐
