# The evaluation rubric

**Status: CANDIDATE v2 — revised 2026-08-11 against the professional-vision
literature and F7/F9/F10/F11/F12/F16/F19. Not yet frozen. Freeze per §8
before the first evaluation session (`EVALUATION_FROM_DATE`
2026-08-11T14:00).**

## 1 · What it is, mechanically

The rubric is the free text typed into **Evaluation criteria** on the
review page. It reaches the model as one line:

```
Researcher's evaluation criteria: <your text>
```

and the model answers, for every unit, with a `criteria_met` field.

With no rubric the prompt says "no specific evaluation criteria were
provided", every `criteria_met` comes back `null`, and there is nothing
for a human coder to agree or disagree with. That is why κ is currently
undefined and why `verify_metrics` reports it as the one MISSING metric,
and why the open-items list in METHODOLOGY_FINDINGS says the evaluative
half of RQ3 has no data at all.

So the rubric is not documentation. **It is the operationalisation of
the dependent variable in RQ3.** It is also, per Duvivier et al. (2026),
the exact thing this literature is worst at reporting: their review of
27 professional-vision eye-tracking publications finds AOI definition
handled inconsistently — defined before collection in some studies and
after in others, with the shape frequently unspecified — and singles out
the one study that used multiple coders and reported a reliability score
as the exception rather than the norm. A frozen, published rubric with a
κ attached is therefore a contribution in itself.

> **Verified 2026-08-11 against the published article.** The DOI is
> correct and the substance holds. The phrase "a central methodological
> flaw" was in quotation marks here and is not theirs — removed. Read
> their §on AOIs before citing it in the thesis; the paraphrase above is
> from the article text, not from the abstract.

## 2 · What it has to satisfy

1. **Decidable from what is visible.** The model sees fixation keyframes
   with a gaze marker and a stated uncertainty. "Attends to a struggling
   student" is not decidable from that; "gaze is on the student area
   rather than the board" is.

2. **Binary per unit, one construct per binary.** One judgment per
   criterion per unit. A criterion needing three levels is two criteria
   or a rating scale — and a rating scale means weighted κ, not Cohen's κ.
   **Criteria are NOT conjoined into one composite** (§3).

3. **At a granularity the tracker can resolve.** Inclusion accuracy is
   1.04° (F7). By F9 what governs attribution is the SEPARATION between
   candidates, not their size: adjacent students in the pilot clip sit
   ~134 px apart at ~124 px of error, so *which* student is at the edge
   of resolvable, while *student area vs board vs door vs teacher's desk*
   is safe. Write criteria at the **region level**. A rubric that needs
   face-level resolution measures the tracker, not the participant.

4. **Independent of the model's bounding boxes.** F19 is decisive here:
   the model names correctly (88 % human-judged) and localises badly
   (16.9 % strict / 28.8 % lenient), and the 11.9 pp gap proves the
   misses are far larger than the measurement error. Every criterion
   below therefore rides on `attended` — the *name* — and on the recorded
   gaze, never on `bbox`. A rubric that depends on the model's box
   measures a capability the model does not have.

5. **The same text goes to the human coder.** Both raters must answer the
   same question or κ measures a translation error. `agreement_kit.py`
   prints `ctx["rubric"]` verbatim at the top of each rating sheet for
   this reason — which means **the string in §5 must be self-contained**.
   Anything a coder learns in training that is not in that string is an
   asymmetry and is listed in §7.

6. **Frozen before participant 1.** Changing the wording mid-collection
   splits the data into two studies. If it turns out to be wrong, say so
   in the limitations — do not edit it.

## 3 · Why the draft's single conjunctive binary had to go

The v1 draft ANDed three criteria into one `criteria_met`. Three problems,
in ascending order of severity:

- **The constructs are distinct in the literature.** Keskin, Seidel,
  Stürmer & Gegenfurtner's (2024) meta-analysis of 98 studies isolates
  exactly two canonical DVs — gaze proportion on students (*g* = 0.926
  expert–novice) and Gini-coefficient evenness of gaze distribution
  (*g* = 0.501) — as *separate* effects. Event-driven noticing is a third
  strand (van den Bogert et al., 2014). ANDing them produces a number
  that estimates none of the three.
- **Disagreement cannot be localised.** A `false`/`true` mismatch tells
  you nothing about which of three judgments the raters split on, so a
  low κ gives no repair path.
- **The conjunction is prevalence-hostile.** ANDing three criteria drives
  the positive rate down and pushes κ into the region where Byrt, Bishop
  & Carlin's (1993) prevalence index dominates the statistic (§6).

Rao & Callison-Burch (2026) address rubric-based LLM judges directly.
Their reporting checklist names the judgment scale, the abstention and
tie handling mode, coverage, **the confusion matrix**, and the
aggregation level alongside any scalar coefficient — and their analytic
result is that on non-degenerate binary criteria, Pearson's *r*,
Spearman's ρ, Kendall's τ_b, φ and MCC all collapse to the same number,
so reporting several of them "only creates an illusion of corroborating
evidence". Cohen's κ is the one coefficient that adds information,
because it normalises differently from φ and the gap between them
measures how far the judge's positive-label rate has drifted from the
human's — which is exactly the prevalence problem in §6.4, arriving
from a second direction.

> **Verified 2026-08-11 against the arXiv abstract.** Two earlier
> claims in this section did not survive: a quoted sentence about the
> 2×2 table, and a "34.8 pp" spread attributed to protocol choices. The
> string "34.8" does not occur anywhere in the paper, and the quoted
> sentence is not in it either. The *substance* — report the confusion
> matrix, protocol choices are not neutral preprocessing — is the
> paper's actual argument. Paraphrase it; do not quote it.

So: **three independent binaries, plus a derived composite.** The
composite is reported but is not the primary result.

## 4 · The unit of judgment

`LLM_WINDOW_SECONDS = 5` and `detail="windows"` — **use this mode, not
`fixations` or `phases`.** (Orquin, Ashby & Clarke, 2016, argue the same
point for space that windows mode enforces in time: AOI boundaries are a
signal-detection decision made by the researcher, and when fixation
distributions to neighbouring objects overlap, a generous boundary
manufactures false positives. Their recommendation — no margin when
overlap is expected, >0.5° when it is not — is the spatial twin of F9,
and is why the four region categories in §5 are drawn to be separated
rather than to be small.) The windows mode is the only one that forces
both raters onto identical, pre-declared boundaries ("Do NOT choose your
own boundaries… The windows are fixed so that different raters describe
the SAME units"). `phases` lets the model pick its own ≤8 boundaries,
which makes row-wise alignment with a human sheet an assumption rather
than a fact; `fixations` inherits F12's ±1-frame alignment risk on every
single row.

At 30 s per clip (F16) that is **6 windows × 2 clips = 12 per
participant**.

**`windows` mode is implemented but unreachable from the UI.** `app.py`
accepts it (L1601, L1813) and sizes the token budget for it (L1707), but
`templates/review.html:73–74` offers only `fixations` and `phases`, and
L448 falls back to `fixations`. Add the option before freezing:

```html
<option value="windows">Fixed 5 s windows</option>
```

Without it the rubric below is unusable as written, and this is the sort
of gap that surfaces on the first evaluation session rather than during
planning.

| N participants | window-judgments per criterion |
|---|---|
| 10 | 120 |
| 15 | 180 |
| 20 | 240 |

Windows within a participant are not independent. Report κ pooled, and
bootstrap the CI **by resampling participants, not windows** — otherwise
the interval is too narrow by roughly the design effect. Sim & Wright
(2005) is the citation for both halves of that sentence: κ needs an
explicit sample-size justification and a confidence interval, and the
width of that interval is what decides whether a κ of 0.61 is
distinguishable from one of 0.45. At N = 10 it very likely is not — state
the target N and its CI width in the methods rather than reporting a bare
point estimate.

A 5 s window holds ~12 fixations at the measured 2.33 fixations/s. That
is enough for a proportion judgment to be stable, which is why the
thresholds below are proportions rather than counts. Do not shorten the
window to buy N: below ~3 s the proportions get noisy and F12's
adjacent-frame risk starts to bite. Buy N with participants.

## 5 · The rubric — canonical text

This is the verbatim string for the **Evaluation criteria** box. It is
what the model receives and what `agreement_kit.py` prints to the human
coder. It is deliberately long: it must be self-contained.

> Judge each 5-second window on THREE INDEPENDENT criteria. Answer each
> one separately; do not let one answer influence another.
>
> **Region categories.** Assign gaze to one of these four regions only.
> Do not attempt to identify individual students — the measurement
> cannot separate them.
> - STUDENTS — the seated pupil area, including any pupil currently
>   speaking, standing or moving.
> - INSTRUCTIONAL SURFACE — board, screen, projection, or the material
>   under discussion.
> - NON-INSTRUCTIONAL — door, window, ceiling, floor, bare wall, unused
>   furniture, the camera.
> - UNRESOLVABLE — gaze off-screen, absent, or falling between two
>   regions closer together than the stated measurement error.
>
> **C1 — Attention to students.** TRUE if more than half of the window's
> resolvable gaze time is on STUDENTS. FALSE if not. NULL if more than
> half the window is UNRESOLVABLE.
>
> **C2 — Following the event.** Applies only to windows in which the
> supplied event list marks a salient event (a pupil raises a hand,
> stands, speaks, or is addressed). TRUE if the gaze reaches the region
> of that event during the window in which it occurs or the one
> immediately after. FALSE if it does not. NULL if the window has no
> marked event, or if more than half the window is UNRESOLVABLE.
>
> **C3 — Distribution.** TRUE if at least two distinct regions each
> receive at least 20 % of the window's resolvable gaze time (at 5 s,
> roughly one second each). FALSE if a single region takes essentially
> the whole window. NULL if more than half the window is UNRESOLVABLE.
>
> Answer NULL rather than FALSE whenever you could not tell. NULL means
> "not decidable from what I was shown"; FALSE means "decidable, and the
> criterion does not hold". Name the general region rather than making
> over-precise claims.

**On the removed TEACHER category.** Four regions, not five. This is
only safe if no teaching adult is visible in either clip. **Check both
clips before freezing.** If an adult IS visible, decide *now* which
region they belong to and write it into the string — looking at the
teacher is not the same as looking at a door, so folding them into
NON-INSTRUCTIONAL changes what C1 measures. What must not happen is
leaving it unstated: two raters will each resolve it silently, in
different directions, and the disagreement will look like rater
unreliability when it is an unwritten rubric.

### Output schema change this requires

`criteria_met` becomes an object. In `app.py`, in the `detail ==
"windows"` branch, **L1838**, replace

```
"\"criteria_met\": <true|false|null>, "
```

with

```
"\"criteria_met\": {\"C1\": <true|false|null>, "
"\"C2\": <true|false|null>, \"C3\": <true|false|null>}, "
```

Keep `bbox` in the schema — it feeds `claim_check` / `inverse_check` —
but **no criterion reads it** (§2.4).

**Four downstream call sites read `criteria_met`, and every one of them
fails silently rather than loudly on a dict.** Fix all four in the same
commit or the change will look like it worked:

| site | current code | what a dict does |
|---|---|---|
| `app.py:1550` `_consistency` | `bool(p[i].get("criteria_met"))` | any non-empty dict is truthy → **reports 100 % consistency always** |
| `model_comparison.py:315` | appends the raw value into `ratings` for `fleiss_kappa` | unhashable dict in a rating vector → crash or garbage κ |
| `verify_metrics.py:539` | `c.get("criteria_met") is not None` | a dict of three nulls is not None → **reports the rubric as present when it is empty**, defeating the MISSING check that exists to catch exactly this |
| `agreement_kit.py:114,190–195` | one `llm_criteria_met` / `human_criteria_met` pair | needs three of each (`*_c1/c2/c3`), κ per criterion, plus `C1 ∧ C2 ∧ C3` as a fourth derived row |

The `verify_metrics` one is the dangerous one: it is the guard that
currently tells you the rubric is missing, and the schema change would
switch it off exactly when you start relying on it.

Also update `metrics_spec.py:175` (the schema description) and `:191`
(the κ metric description) so the spec and the code do not drift, and
extend the `criteria_met` fixtures in `model_comparison.py:345–352` and
the `run_tests.py` section that covers them.

## 6 · What to report, and why

Per criterion, and per clip as well as pooled (F16: pooling a crowded and
a sparse scene averages two different ambiguity rates into a number that
describes neither):

1. **The 2×2 table and N.** Non-negotiable — Rao & Callison-Burch (2026).
   Everything else is recoverable from it; it is not recoverable from
   anything else.
2. **Coverage.** The share of windows scored NULL by either rater, and by
   which one. F11 already measured this construct at the fixation level:
   16.9 % unclear, "the rate at which the instrument cannot adjudicate
   between two plausible objects — a property of the method, not of the
   model." Under exclusion of abstentions, accuracy over all cases is
   pinned down only to an interval as wide as the uncovered fraction, so
   a κ reported without its coverage rate is not interpretable.
3. **Raw agreement AND Cohen's κ.** Norman, Rivera & Hughes (2026) name
   the gap "kappa deflation" and measure exact-match overstating
   discriminative ability by 33–41 pp across 21 judges. Report both, and
   never the raw number alone.
4. **Prevalence index and bias index** (Byrt, Bishop & Carlin, 1993). If
   C1 is met in ~85 % of windows — plausible, since participants watching
   a classroom clip mostly look at people — κ collapses while agreement
   stays high. That is the kappa paradox, not a failure of the pipeline.
   Report PI and BI alongside κ so a reader can tell the two apart; add
   PABAK **as a supplement, never as a replacement**.
5. **The human–human ceiling.** See §7. Report κ(LLM, human) *against*
   κ(human, human), not in isolation.
6. **FP vs FN asymmetry.** Which direction the model errs in is the
   finding; a symmetric κ hides it.
7. **Excluded-frame count** per session (F10) and the sampling rate
   alongside any precision figure (F8).

Landis & Koch's κ > 0.6 stays as the pre-declared bar — it is what
`agreement_kit.py` already prints — but declare it *before* looking, and
interpret it against the ceiling, not against the scale.

## 7 · The two asymmetries, and the cheap fix for both

**A second coder is required, and is the highest-value hour in the
project.** F11: *"One coder only, so there is no reliability estimate. A
coder who is systematically generous produces the same number as a fair
one."* Beyond that, κ(human, human) is the **ceiling** for κ(LLM, human).
Without it, a κ of 0.55 is uninterpretable: it could be a mediocre model
or an ambiguous rubric, and those have opposite remedies. Have the second
coder do all 12 windows on a subset of participants — ~5 participants,
60 windows, under an hour — blind to both the LLM columns and the first
coder.

**Evidence asymmetry.** The model sees fixation keyframes plus a fixation
summary; the human watches continuous replay with a gaze overlay. These
are not the same evidence, so some disagreement is not judgment
disagreement. Do not try to eliminate it — the pipeline's whole point is
that the model works from keyframes. Instead **quantify it**: have one
coder re-code ~20 windows from the exported keyframes alone, and report
the keyframe-only vs replay κ for that subset. That single number
separates "the model judges differently" from "the model saw less", and
it is a result about multimodal LLMs as an analysis instrument, in the
same family as F19.

Both belong in the protocol checklist Rao & Callison-Burch require:
scale & target, handling rules with coverage rates, aggregation and
resampling unit, and evidence (2×2 + N, FP/FN pattern, degenerate cases).

## 8 · Two things to build before freezing

**The event list for C2.** C2 needs salient events marked on the two
clips independently of any rater — otherwise the LLM is again its own
ground truth, which is validity gap #2. This is two 30 s clips: annotate
onset time and region for each hand-raise, stand, utterance and
address, have the second coder do the same clips blind, and report
κ(event annotation). An hour of work that converts C2 from circular to
anchored. **If this does not get built, drop C2 rather than ship it
circular** — a two-criterion rubric with clean anchors beats a
three-criterion one with a soft centre.

**The computed Gini anchor for C3.** C3 is the one criterion whose truth
is also computable from the gaze data with no rater at all. Gini over
resolvable gaze time across the four regions, per window, is the
window-level form of the measure Cortina, Miller, McKenzie & Epstein
(2015) and Smidekova, Janik, Minarikova & Holmqvist (2020) use at lesson
level, and which the Keskin meta-analysis reports at *g* = 0.501. Compute
it per window; the C3 binary is TRUE when at least two regions clear
20 %, which is a threshold on the same quantity.

That gives a **third rater that is neither human nor LLM**, and it is the
only place in this design where one is available. Report
κ(LLM, computed) and κ(human, computed) beside κ(LLM, human). If the LLM
tracks the computed value but the human does not, the rubric is
mis-worded; if the human tracks it and the LLM does not, that is a
finding about the model. Either way you learn which, and neither the
human nor the LLM can be the reference for the other. Follow Smidekova
et al. and use `reldist::gini` or an equivalent standard implementation
rather than hand-rolling it, and state which.

## 9 · Freeze protocol

1. Build the event list (§8) or drop C2. Build the Gini computation.
2. Ship the schema change (§5) and confirm a run parses; check
   `model_comparison.py` still reports consistency.
3. Run the rubric on a **development** session — `13:47 11.08`, 59 claims
   and a full LLM run, safely before `EVALUATION_FROM_DATE` — and code
   ten windows yourself. If you disagree with the model on a window and
   cannot name the criterion that decides it, the rubric is
   underspecified. Fix it now, while there is no evaluation data to
   invalidate.
4. Check the prevalence of each criterion on those ten windows. If C1 is
   near-universally TRUE, say so in the methods as a predicted κ
   limitation rather than discovering it in the results.
5. Freeze: record the SHA-256 of the canonical string (§5) in the session
   manifest, so every session provably carries the same rubric and a
   reader can verify it. Do not edit after the first evaluation session.

## 10 · Review, 2026-08-11 — five things that do not yet close

Recorded here rather than fixed inline, because each is a decision, not
an edit.

### 10.1 · C1 and C3 need the AOIs the design says it does not have

C1 is "more than half of the window's **resolvable gaze time** is on
STUDENTS". C3 is "at least two regions each receive **20 % of the
window's resolvable gaze time**". Both are proportions of gaze time
*per region*. Computing a proportion per region requires region
boundaries on the video — an AOI set.

But `verify_metrics` reports all five `aoi_*` metrics as **"N/A by
design: no hand-drawn AOIs"**, with the reasons frozen in
`metrics_spec.NOT_APPLICABLE`, and that appears in every session report.
The thesis cannot say both.

The resolution is not to abandon one: it is to notice that **F9 already
licenses coarse region AOIs and forbids only fine ones.** What F9 rules
out is face-level attribution, because adjacent students are separated
by less than the error. Four regions that are separated by far more than
124 px are exactly the case F9 says *is* attributable. Draw them, and
the N/A justification changes from "no AOIs" to "no *object-level*
AOIs — region AOIs only, at a separation the measurement supports",
which is a stronger claim than the one being made now.

Two static clips, four polygons each, one camera position, ~20 minutes.
Without them, §8's computed Gini anchor is not merely unbuilt — it is
**uncomputable**, and the design loses the only non-circular reference
it has.

### 10.2 · The Gini anchor answers a different question from C3

§8 says the C3 binary "is a threshold on the same quantity" as Gini. It
is not. "At least two regions ≥ 20 %" is not a monotone function of the
Gini coefficient: 34/33/33 across three regions and 79/21 across two
both satisfy C3, and their Gini values are far apart; conversely two
distributions with identical Gini can fall on opposite sides of the
20 % rule.

So κ(LLM, computed) computed against Gini would measure how well the
model tracks a statistic nobody asked it to judge. **Define the computed
rater with the identical decision rule** — two regions each clearing
20 % of resolvable time — and it becomes a true third rater. Report
Gini separately as a continuous descriptive, which is where it connects
to Keskin's second DV. Both are worth having; they are not the same
thing.

### 10.3 · The N table is right for C1 and C3 and wrong for C2

§4 gives 120/180/240 judgments "per criterion". C2 is NULL in every
window with no marked event. Two 30 s clips will hold perhaps two or
three salient events each, so C2's real N is roughly **4–6 per
participant**, i.e. 40–60 at N = 10 — and κ on 50 judgments with a
skewed positive rate has a confidence interval wide enough to cover
most of Landis & Koch's scale, which is the exact failure Sim & Wright
warn about. State the expected N **per criterion**, not per rubric, and
decide in advance whether C2 is reportable at that N or is exploratory.

### 10.4 · C2 references an event list that no code path supplies

The canonical string says "the supplied event list marks a salient
event". Nothing in `app.py` supplies one. The model will therefore
decide for itself what counts as salient — which is precisely the
circularity §8 identifies, arriving through the prompt rather than
through the annotation.

So §8's "build the event list" is not only an annotation task. The list
has to be **injected into the prompt per clip**, and given to the human
coder in the same form, or the two raters are answering different
questions. That is a code change, and it is the only one C2 needs
beyond the schema work in §5.

### 10.5 · The freeze is stronger than §9.5 thinks, and unenforced

§9.5 proposes recording the SHA-256 of the canonical string. But
`app.py:1991` already writes **the rubric text itself** into the
manifest. Storing the text strictly dominates storing a hash: a hash
detects drift, the text also tells you what it drifted to.

What is missing is not a hash — it is a *check*. `verify_metrics.py
--rubric` now collects the rubric from every evaluation session and
fails if they are not byte-identical, naming the sessions that differ.
That converts §6 of the freeze protocol from a discipline into a
mechanism, which is the only form of freeze that survives a long
collection period.

## 11 · References

- Byrt, T., Bishop, J., & Carlin, J. B. (1993). Bias, prevalence and
  kappa. *Journal of Clinical Epidemiology, 46*(5), 423–429.
  https://doi.org/10.1016/0895-4356(93)90018-V
- Cortina, K. S., Miller, K. F., McKenzie, R., & Epstein, A. (2015).
  Where low and high inference data converge: Validation of CLASS
  assessment of mathematics instruction using mobile eye tracking with
  expert and novice teachers. *International Journal of Science and
  Mathematics Education, 13*(2), 389–403.
  https://doi.org/10.1007/s10763-014-9610-5
- Duvivier, V., Derobertmasure, A., & Demeuse, M. (2026). Professional
  vision in teaching: Review of methodological concepts using
  eye-tracking. *Quality & Quantity, 60*(3), 11005–11034.
  https://doi.org/10.1007/s11135-026-02702-4
- Holzberger, D., Seidel, T., Schnitzler, K., Kosel, C., & Stürmer, K.
  (2021). Student characteristics in the eyes of teachers: Differences
  between novice and expert teachers in judgment accuracy, observed
  behavioral cues, and gaze. *Educational Psychology Review, 33*(1),
  69–89. https://doi.org/10.1007/s10648-020-09532-2
- Keskin, Ö., Seidel, T., Stürmer, K., & Gegenfurtner, A. (2024).
  Eye-tracking research on teacher professional vision: A meta-analytic
  review. *Educational Research Review, 42*, 100586.
  https://doi.org/10.1016/j.edurev.2023.100586
  *Verified: real, 98 studies, meta-analyses gaze proportion AND the
  Gini coefficient, direction as stated. The effect sizes quoted in §3
  (g = 0.926, g = 0.501) are NOT confirmable from any open source —
  read them off the PDF before either number enters the thesis.*
- Landis, J. R., & Koch, G. G. (1977). The measurement of observer
  agreement for categorical data. *Biometrics, 33*(1), 159–174.
- Norman, J. D., Rivera, M. U., & Hughes, D. A. (2026). Reliability
  without validity: A systematic, large-scale evaluation of
  LLM-as-a-judge models across agreement, consistency, and bias.
  arXiv:2606.19544.
- Orquin, J. L., Ashby, N. J. S., & Clarke, A. D. F. (2016). Areas of
  interest as a signal detection problem in behavioral eye-tracking
  research. *Journal of Behavioral Decision Making, 29*(2–3), 103–115.
  https://doi.org/10.1002/bdm.1867
- Rao, D., & Callison-Burch, C. (2026). Agreement metrics for
  LLM-as-Judge evaluation: What to report and why. arXiv:2606.00093
  (submitted 25 May 2026). *Note: the arXiv listing title and the HTML
  render differ; this is the canonical one.*
- Sim, J., & Wright, C. C. (2005). The kappa statistic in reliability
  studies: Use, interpretation, and sample size requirements. *Physical
  Therapy, 85*(3), 257–268.
- Smidekova, Z., Janik, M., Minarikova, E., & Holmqvist, K. (2020).
  Teachers' gaze over space and time in a real-world classroom. *Journal
  of Eye Movement Research, 13*(4), 1.
  https://doi.org/10.16910/jemr.13.4.1
- van den Bogert, N., van Bruggen, J., Kostons, D., & Jochems, W. (2014).
  First steps into understanding teachers' visual perception of classroom
  events. *Teaching and Teacher Education, 37*, 208–216.
  https://doi.org/10.1016/j.tate.2013.09.001
