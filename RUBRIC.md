# The evaluation rubric

**Status: DRAFT — the criteria below are placeholders. Replace them with
your own construct definition, then freeze this file before P01.**

## What it is, mechanically

The rubric is the free text you type into **Evaluation criteria** on the
review page. It reaches the model as one line:

```
Researcher's evaluation criteria: <your text>
```

and the model answers, for every time window, with

```json
"criteria_met": true | false | null
```

With no rubric the prompt says "no specific evaluation criteria were
provided", every `criteria_met` comes back `null`, and there is nothing
for a human coder to agree or disagree with. That is why κ is currently
undefined and why `verify_metrics` reports it as the one MISSING metric.

So the rubric is not documentation. **It is the operationalisation of
the dependent variable in RQ3.**

## What it has to satisfy

1. **Decidable from what is visible.** The model sees frames with a gaze
   marker and a stated uncertainty. "Attends to a struggling student" is
   not decidable from that; "gaze is on the student group rather than
   the board" is.

2. **Binary per window.** One judgment per fixed time window. If a
   criterion needs three levels, it is two criteria or it is a rating
   scale — and a rating scale means weighted κ, not Cohen's κ.

3. **At a granularity the tracker can resolve.** Accuracy is ~1.0–1.3°.
   By F9 what governs attribution is the SEPARATION between candidates,
   not their size: adjacent students in this clip sit ~134 px apart at
   ~124 px of error, so *which* student is at the edge of resolvable,
   while *student area vs board vs door vs teacher's desk* is safe.
   Write criteria at the region level. A rubric that needs face-level
   resolution measures your tracker, not the teacher.

4. **The same text goes to the human coder.** Both raters must be
   answering the same question or κ is measuring a translation error.
   `agreement_kit.py` prints the exact rubric the model received at the
   top of each rating sheet for this reason.

5. **Frozen before participant 1.** Changing the wording mid-collection
   splits your data into two studies. If it turns out to be wrong, say
   so in the limitations — do not edit it.

## Draft (replace the content, keep the shape)

> Judge each time window against these criteria, in order. Answer
> `criteria_met: true` only if ALL applicable criteria hold.
>
> 1. **Attention to students.** The gaze is on the student area
>    (seated pupils, including the pupil currently speaking or moving)
>    rather than on non-instructional regions (door, window, ceiling,
>    the teacher's own desk, the camera).
> 2. **Following the event.** When something salient happens — a pupil
>    raises a hand, stands up, or is addressed — the gaze reaches the
>    region of that event within the window in which it occurs or the
>    one immediately after.
> 3. **Distribution.** The window's gaze is not confined to a single
>    small region for the whole window when other salient regions are
>    active.
>
> If the gaze was off-screen or unmeasurable for most of the window,
> answer `null`, not `false`.

The `null` clause matters: it separates *the criterion was not met* from
*we could not tell*, and the coding tools already treat "unclear" as
excluded from the accuracy rate rather than counted as wrong.

## Before you freeze it

Run it once on a development session (`13:47 11.08` has 59 claims and a
full LLM run) and read ten windows yourself. If you disagree with the
model on a window and cannot say which criterion decides it, the rubric
is underspecified — fix it now, while there is no evaluation data to
invalidate.
