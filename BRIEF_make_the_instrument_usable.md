# Brief — make the instrument usable, or bound what it can't do

*Paste this as the first message of a Claude Code session in
`eye_tracking_experiment/`. Written 2026-08-18. Read `CLAUDE.md` and
`METHODOLOGY_FINDINGS.md` F29–F36 before touching anything.*

---

## READ THIS FIRST

The last three sessions of work produced a good measurement apparatus and
**zero improvement in gaze accuracy**. That is the problem you are being
handed. A large fraction of that output was the assistant fixing bugs it
had introduced one entry earlier. Do not repeat that shape of work.

**You are not here to write findings entries. You are here to move a
number.**

---

## THE STATE — established, do not re-derive

All of this is measured and logged. Take it as given.

- **The gain correction was overfitted** (F30). Fit on grid A, applied
  unconditionally, it removed 42–61 % of the error on the fit grid and
  1–6 % on grid B. It is now selected by leave-one-out on grid A
  (`validation_stats.select_correction`, rule fixed 2026-08-17).
- **The tracker's systematic offset is nonstationary on some
  participants** — 0.9–1.3° in 21–40 s on PILOT_02 and Manuel_P2
  (p = 0.016–0.021), but **stable** on PILOT_05 (all p > 0.3). It is not
  a universal property.
- **There is a shear** (F33): vertical error depends on *horizontal*
  position. PILOT_03 `m_yx = +0.228` (CI excludes zero), 437 px ≈ 6.8°
  across the screen. PILOT_04 the same with opposite sign. **A per-axis
  correction cannot represent it** — `m_yx / m_yy` is invariant under any
  diagonal map, at any polynomial degree.
- **Seven targets cannot support fixing the shear.** A full 2-D affine
  gains 4–7 % under LOO at n = 7, but **28 % (PILOT_03) and 15 %
  (PILOT_04) at n = 14**, and loses on the four sessions without shear.
- **PILOT_05's vertical error is almost entirely the top row** (F36).
  Drop the two `ty = 130` targets and mean dy falls to +2.6 px.
- **Everything is now reported signed**, with medians, off-diagonal
  terms, `residual_ratio`, and a recorded correction decision.

Tools that exist and work: `correction_audit.py`, `rederive_session.py`,
`show_validations.py`, `validation_stats.py`. 1054 checks in
`run_tests.py`.

## WHAT HAS NOT IMPROVED — the actual problem

| session | canonical (measured ruler) | out-of-sample bias | verdict |
|---|---|---|---|
| PILOT_00 | 1.25° | 0.52° | pass |
| PILOT_02 | 1.56° | 1.46° | pass |
| PILOT_01 | 1.57° | 0.32° | pass |
| PILOT_04 | 1.75° | 0.14° | pass |
| Manuel_P2 | 2.31° | 1.11° | pass |
| **PILOT_05** | 2.66° | **1.95°** | pass |
| **PILOT_03** | **3.88°** | 2.21° | **FAIL** |

One session in seven fails outright. Several carry a systematic
displacement of 1–2° that no correction removes. The participant
complaint that started this ("all the eye gaze was above the people") is
**not fixed**. Nothing in F30–F36 made a single recorded session more
accurate.

## THE OBJECTIVE

**Reduce the measured out-of-sample error and systematic bias on grid B,
or establish with evidence that it cannot be reduced and state the bound.**

Success is a number in this table moving, or a defensible demonstration
that it cannot move and why. Not a document.

Report progress as: *"grid-B mean error / |bias| went from X to Y on
sessions Z, measured by `correction_audit.py`."* If you cannot say that,
you have not made progress.

---

## THE WORK, in order of cost-to-value

Do these one at a time. After each, run `correction_audit.py` on every
session and report whether the table above moved.

### 1. Head position — free, untested, nine sessions of nothing

`head_position` is **null in all nine manifests**. The positioning guide
has never run. This is the single cheapest untested hypothesis for the
shear: an off-centre head, or a head pose that differs between
calibration and validation, produces exactly the shear signature
observed (same-signed off-diagonals; head roll would give opposite
signs).

- Find why it is never populated — is the guide optional, skipped, or
  silently failing? Read `app.py` around `position_snapshot` and the
  tracker's `position_info()`.
- Make it **mandatory and automatic** at calibration and at each
  validation, not an optional guide someone opens. Record head centre
  offset, yaw/pitch/roll if available, and distance, per phase.
- Then test: does `m_yx`, or the signed bias, track head placement
  across sessions and across phases within a session?

**Decision criterion:** if head offset explains a material share of the
shear or the bias, a head-position gate before recording is the fix and
it costs nothing. If it does not, say so and drop the hypothesis — it has
been the leading explanation on no evidence for two entries.

### 2. Densify grid A so the shear becomes correctable

F33 established the shear is real and estimable at 14 targets but not 7.
The obvious move is more fit targets.

- Extend grid A from 7 to 13 targets, keeping grid B at 7 and **keeping
  the two grids disjoint** — `run_tests.py` section [17] asserts the
  eccentricity match and empty intersection; keep those passing.
- Add `full-affine` (6 parameters, 2×2 + offset) as a fourth candidate in
  `validation_stats.CANDIDATE_ORDER`, fitted and selected by the same LOO
  rule on grid A. It must beat the diagonal model by the declared SE
  margin or lose.
- Cost: ~15 extra seconds of validation per session.

**Decision criterion:** re-run the LOO comparison on grid A alone at
n = 13 using the existing sessions' geometry as a simulation first. Only
change the protocol if the simulation says the full model would be
selected on the sheared sessions and rejected on the others. **Do not
ship a 6-parameter model on 7 targets — that is F30's mistake with more
parameters.**

### 3. The top row

PILOT_05 loses the top ~12 % of the screen and is accurate everywhere
else. PILOT_03's worst errors are also positional.

- Establish whether this is the tracker's vertical range, the
  participant's gaze, or the camera geometry. The `_uncorrected` per-target
  data across all nine sessions is on disk — look at dy against `ty` for
  every session before running anyone.
- If the tracker cannot resolve the top band, **that is a stimulus-design
  constraint**, not a calibration problem: it bounds where AOIs can be
  and belongs in `metrics_spec` beside `min_aoi_px`.

### 4. Finish what is half-done

- **PILOT_02 has not been re-derived.** The rule rejected its correction
  on 2026-08-17 and its recorded gaze still carries it.
  `rederive_session.py --dry-run` then apply, **on the Windows machine
  where `gazefollower_data.xlsx` lives**. Then regenerate its fixations,
  `data_quality` and LLM feedback — the tool marks them stale and does
  not recompute them.
- **PILOT_03 has not been retired.** It fails at 3.88°. Use
  `retire_session.py` with the reason; report the exclusion as a result.
- **One commit is unpushed** (`11f427b`). The Windows recording machine
  runs stale code until it is pushed.

### 5. Only after 1–4: the luminance question

F29's original hypothesis — that a dark validation screen and a bright
video present different eye images — was never tested and its evidence
was withdrawn as confounded. The test is validation targets overlaid on a
playing clip or a still frame from one, against the same participant's
dark-screen validation. It is a real open question. It is **not** the
explanation for anything currently measured.

---

## HOW TO WORK

**Every change must move a measured number or be reverted.** Before
starting a change, write down which figure you expect to move and by how
much. After, run `correction_audit.py` on all sessions and check. If it
did not move, say so plainly and revert rather than keeping the change
because the code is nicer.

**Reproduce offline before asking for a session.** Nine participants have
been recorded. Almost everything learned so far was recoverable from a
saved manifest. Ask for hardware time only when you can say exactly which
number the new session will produce that the existing nine cannot.

**Test discipline — these three failures all happened, twice each:**

1. A **source-text assertion passes for a module that cannot be
   imported.** Three such checks passed while `verify_metrics.check_session`
   raised `NameError` on its first line. Always pair a source-text check
   with one that *executes* the path.
2. **A test that re-implements what it checks verifies nothing.** A ruler
   test rebuilt the conversion inside itself and passed while the real
   code was mutated back to the bug. Call the real function.
3. **A property too weak to separate fix from bug.** "Small for affine,
   large for noise" is equally true of the un-normalised residual.
   Assert an invariant the bug violates — scale invariance, exact
   round-trip, a known-truth recovery.

**Mutation-test every fix.** Break it deliberately, confirm the test
fails, restore. **Clear `__pycache__` between runs** — a `.pyc` restored
by `shutil.move` can match on size and mtime and produce phantom
failures.

`python run_tests.py` must be green before and after every change.

**Commit per fix**, reasoning in the message. **Nothing is pushed
automatically** — say when a push is needed.

## WHAT NOT TO DO

- **Do not write a findings entry about a bug you introduced and fixed in
  the same session unless it reached a recorded session.** The log is for
  measured insights about the instrument, not a changelog of your own
  regressions. F32, F34, F35 and F36 are substantially that, and they
  crowded out the work.
- **Do not add another diagnostic.** There are enough. Signed bias,
  medians, off-diagonal terms, `residual_ratio`, stability tests,
  correction decisions with bootstrap intervals and sign tests. Adding a
  ninth measure of the same problem is not progress. If a diagnostic is
  the honest answer, it must come with the accuracy consequence stated.
- **Do not switch a pre-declared analysis step silently.** Two are open
  and must be decided by Jan, dated, not by you: (a) which ruler the
  3.0° inclusion criterion uses — browser vs measured distance, they
  differ −22 % to +15 %; (b) whether the 7-target mean gets an outlier
  rule. Report both, decide neither.
- **Do not refactor for its own sake.** Every file touched is a chance to
  reintroduce a bug that a passing test does not catch.

## STOP AND ASK

- If the answer is "the instrument cannot do better than ~1–2° systematic
  offset", stop and say so with the evidence. That is a legitimate
  result and it changes the thesis' claims and the rubric's AOI sizes —
  it does not need more code.
- If a fix requires recording new participants, stop and say exactly what
  the session must contain.
- If two candidate fixes conflict, present both with their measured
  consequences and let Jan choose.

## FILES

```
validation_stats.py     signed bias, LOO selection, spatial terms, inversion
correction_audit.py     the per-session report — THE scoreboard
rederive_session.py     apply a correction decision to recorded gaze
show_validations.py     per-target dump
app.py                  validation records, _degree_fields, finalisation
metrics_spec.py         the metric spec and INCLUSION thresholds
run_tests.py            1054 checks; sections [7], [7b], [7c], [7d]
METHODOLOGY_FINDINGS.md F1–F36
data/study/             manifests + CSVs (on the Windows machine)
```

Start by running `python correction_audit.py` on every session and
reporting the current table. That is your baseline, and every later claim
is measured against it.
