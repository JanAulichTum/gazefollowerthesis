# Test & demo plan

What to run, in order, to convince yourself — and your supervisor — that
the pipeline works end to end. Roughly 45 minutes for the full pass.

Everything below assumes the project root as working directory, with the
project environment active — the conda env from `fix_environment.sh` on
macOS, or `.venv\Scripts\activate` on Windows.

**Windows note — check which shell you are in.** The prompt tells you:
`C:\...>` is **Command Prompt**, `PS C:\...>` is **PowerShell**. They set
environment variables differently, and using the wrong one gives the
unhelpful "The filename, directory name, or volume label syntax is
incorrect" (in German: *"Die Syntax für den Dateinamen ... ist falsch"*).

| shell | set | clear |
|---|---|---|
| Command Prompt (`C:\...>`) | `set TEST_MODE=1` | `set TEST_MODE=` |
| PowerShell (`PS C:\...>`) | `$env:TEST_MODE=1` | `Remove-Item Env:TEST_MODE` |
| bash (macOS/Linux) | `export TEST_MODE=1` | `unset TEST_MODE` |

In Command Prompt: no `$env:`, no quotes, and **no spaces around the
`=`**. The variable then applies to that window until it closes.

Common ones, in Command Prompt form:

```cmd
set GF_CAMERA_FIX=1
set TEST_MODE=1
set RATE_GATE_SECONDS=8
set SESSION_STIMULUS_MODE=all
python app.py
```

To make one permanent across all future windows: `setx GF_CAMERA_FIX 1`
(note: no `=`, and it only affects windows opened afterwards). The Mac-specific advice
elsewhere in this file translates as: *Low Power Mode* → Windows power
plan set to **Best performance** with battery saver off; *Rosetta* has no
Windows equivalent (check `arch` in `tracker_service.py --check` anyway).
`camera_fps_test.py` already selects the DirectShow backend on Windows,
which reports FPS more reliably than the default MSMF one.

---

## Just run everything

```bash
python run_all.py
```

One command. It runs every stage below in order, pauses for the two
steps that need you at the keyboard (calibrating, and recording a
session), and finishes with a **verdict** — plain-language conclusions
plus the three numbers that decide whether your data is usable. Output
lands in `data/run_all_<timestamp>.log` and a readable summary in
`data/run_all_<timestamp>.md`.

Useful flags:

| flag | effect |
|---|---|
| `--auto` | automated steps only; never prompts or opens the app |
| `--quick` | skip the 45 s camera/tracker timings |
| `--skip-session` | everything except recording a full session |
| `--seconds 60` | longer timing runs |

The rest of this document explains each stage and how to read it — worth
reading once, then `run_all.py` is enough.

---

## Stage 0 — Static suite (2 min, no camera)

```bash
python run_tests.py
```

Must end in `RESULT: ALL TESTS PASSED`. Section `[8] Quality-metric
integrity` is the one that matters most: it asserts the *formulas*
behind the quality numbers, not just that code runs. Those bugs were
silent — they produced plausible-looking percentages that measured
nothing — so a formula assertion is the only thing that catches a
regression.

```bash
python tracker_service.py --check
```

Every line should read `ok`. `arch=arm64` (not `x86_64`) on Apple
Silicon — Rosetta roughly halves the inference rate and will push you
onto the wrong side of the rate cliff.

---

## Stage 1 — Establish the hardware ceiling (10 min)

**Stop the experiment server first** — only one process can own the
webcam.

```bash
python camera_fps_test.py --seconds 60      # camera alone, no inference
```

`camera_fps_test.py` needs nothing but a webcam. **`tracker_fps_test.py`
additionally needs a saved calibration model**: GazeFollower's
`process_frame()` raises `No calibration model is available` on every
frame without one, so you get zero samples and a wall of tracebacks. If
you have never calibrated on this machine, do that first:

```bash
python app.py            # log in, complete the calibration, then stop
```

The model persists in `~/GazeFollower`, so this is once per machine.
Then:

```bash
python tracker_fps_test.py --seconds 60     # camera + gaze inference
```

What you are testing: whether the machine can hold the rate for a full
session, not just at the start.

| Observation | Meaning |
|---|---|
| Camera ~30 FPS, tracker ~29 Hz sustained | Good. This is your target state. |
| Camera ~30 FPS, tracker drops to ~14.5 Hz | The cliff. Inference crossed the 33.3 ms frame period and the loop now misses every other frame. |
| Camera itself below 25 FPS | Camera/driver or lighting problem — fix before touching anything else. |
| `tracker_fps_test` reports `early third → late third` drop | CPU turbo window closing. Expected on battery. |

Run `tracker_fps_test.py` twice: once on battery, once on AC with Low
Power Mode off. The difference is usually the whole story. **Record both
numbers** — they belong in the methods section as your hardware
characterisation.

---

## Stage 1b — Is the GPU being used? (10 min)

**No.** GazeFollower hardcodes `{'precision': 'low', 'backend': 0,
'numThread': 4}` in `MGazeNetGazeEstimator.__init__`, and MNN backend 0
is CPU. The GPU is idle on every machine.

Whether switching helps is a separate question, because MNN's Python
wheels are built per-backend: the stock PyPI `MNN` package is CPU-only,
and it **falls back to CPU silently** when you ask for a backend it was
not compiled with. So measure, don't assume:

```bash
python mnn_backend.py --profile
```

This times the real gaze model on every backend and flags any that match
CPU timing as a silent fallback. `--profile` also times MediaPipe
FaceMesh, the *other* half of per-frame cost — which is CPU-only in
MediaPipe's Python API and cannot be moved to the GPU at all. Check the
split before optimising the wrong half.

The number that matters: **33.3 ms** is the frame budget at 30 fps. Total
per-frame cost above it means the loop misses every other frame and the
rate halves.

If a backend is genuinely faster, enable it and re-measure end to end:

```powershell
$env:GF_MNN_BACKEND='OPENCL'      # or CUDA / VULKAN / METAL
python tracker_fps_test.py --seconds 60
```

`GF_MNN_THREADS` overrides the CPU thread count (GazeFollower uses 4,
which may be low for your laptop — the cheapest thing to try first).
`python tracker_service.py --check` now prints the runtime in force.

Whatever you settle on, **record it in the methods section**: the
inference backend sets the sampling rate, and the sampling rate sets your
fixation statistics.

---

## Stage 2 — Rate gate (5 min)

```bash
TEST_MODE=1 python app.py
```

The gate runs **immediately after calibration succeeds** — it cannot run
earlier, because GazeFollower produces no gaze samples until a
calibration model exists. Calibration is itself a 1–2 minute inference
workload, so by that point the CPU turbo window has already closed and
the measured figure is the honest sustained rate.

Log in, calibrate, then watch the panel above the accuracy-check button.

- It should say *"Measuring the sampling rate…"* for ~25 s, then give a
  verdict.
- **Pass path:** green *"✓ Sampling rate OK — NN Hz sustained"*, and the
  accuracy-check button is enabled.
- **Fail path:** provoke it — unplug AC, enable Low Power Mode, or start
  a video call in the background. You should get the red verdict, a
  disabled accuracy-check button, and two buttons: *Measure again* and
  *Record anyway (flagged in the data)*.
- Click *Record anyway*, enter a reason, confirm the button unlocks, and
  check the reason later appears in the session manifest under
  `rate_gate`.

To shorten the wait while iterating: `RATE_GATE_SECONDS=8 TEST_MODE=1
python app.py`, with `cal=skip` in the test panel to reuse the saved
calibration. Set `RATE_GATE_SECONDS=0` to disable the gate entirely.

---

## Stage 3 — Full session, deliberately good (15 min)

Conditions: AC power, Low Power Mode off, everything else closed, bright
front lighting, camera at eye level, ~60 cm.

```bash
python app.py
```

Run one complete participant flow: consent → position check →
calibration → pre-validation → videos → post-validation.

Watch for:

1. Rate gate passes at ~29 Hz.
2. Position guide: face centred, roll < 6°, openness ratio < 1.5.
3. Pre-validation shows **7 targets**, post-validation shows **7 targets**
   in the same positions. This is what makes drift a like-for-like
   comparison — it used to be 7 vs 3.
4. After the pre-check, the gain auto-fit fires. The panel should report
   `x ×… · y ×…` **separately**. If both read ≈1.00 you get an explicit
   warning that the fit is the identity — meaning "corrected" columns
   equal the raw ones.

---

## Stage 4 — Verify the recording (5 min)

```bash
python quality_report.py
```

For the session you just recorded, check:

- `rate STABILITY` — should say `(stable)`. If it reports **CPU
  THROTTLING**, the drop time is printed; compare it against when
  calibration happened.
- `gaze samples (Tobii-style)` — should be **> 60 %** and is now always
  printed with its denominator (`… expected at 30 Hz nominal`). The
  percentage is meaningless without it.
- `relative yield` — will read ~100 %. That is expected and is *not* a
  quality signal; it is there only to show whether one stimulus ran
  slower than the rest of the session.
- `rate shape` — `x1.0x` means a single Hz genuinely describes the
  recording. `MULTIMODAL` means it alternated and the fixation timing is
  biased in a time-varying way.
- `calibration drift` — reported on the **uncorrected** basis.

Then open `/review`, pick the session, and check the Quality panel shows:
pre-session rate gate, rate stability, fixation count and rate, gaze
samples with denominator, both validations with target counts, drift with
its basis, and per-axis gain.

---

## Stage 5 — Re-score the historical sessions (2 min)

```bash
python backfill_manifests.py --dry-run
```

Already applied once (2026-07-31). Re-running the dry run should now show
**no verdict flips** — that is the idempotency check. Originals are at
`data/gazefollower_raw/*_manifest.pre-backfill.json`, and the old values
also live inside each manifest under `legacy_data_quality`.

To revert a manifest: copy its `.pre-backfill.json` back over it.

---

## Stage 6 — LLM feedback (5 min)

On `/review`, generate feedback for a session that **passed** the quality
thresholds. Confirm:

- Step 1 (scene description, no gaze) is plausible and gaze-independent —
  this is your hallucination control.
- Step 2 evaluates against the rubric.
- A log lands in `data/llm_logs/` with the pinned model name.
- The marker radius reflects that session's validation error.

Do not demo LLM feedback on a low-rate session: the fixations feeding it
are merged artefacts, so you would be showing the model a distorted
scanpath.

---

## What to show in a demo, in order

1. `run_tests.py` green — the system self-checks, including its own
   metric formulas.
2. The rate gate catching a deliberately degraded setup and refusing to
   proceed. This is the strongest single slide: it demonstrates you know
   the failure mode *and* prevent it prospectively.
3. A clean session end to end.
4. `quality_report.py` on that session, then on P08 (29 Hz,
   94.2 % gaze samples) as the good-quality exhibit.
5. The backfill dry-run output showing four historical sessions flipping
   PASS → FAIL. Framed correctly this is a *strength*: you found a
   measurement error in your own instrument and corrected it
   non-destructively, with the originals preserved.
6. LLM feedback on the clean session.

---

## Known limitations to state out loud

Do not let a reviewer find these first.

- **Viewing distance is assumed (60 cm)**, not measured, unless the
  position guide captured it. Every degree-of-visual-angle figure
  inherits that assumption.
- **No hardware ground truth.** Accuracy is validated against the
  system's own targets; there is no independent eye tracker to check it.
- **Sampling rate is marginal.** The hardware sits at the cliff edge
  between 29 and 14.5 Hz. Sessions below `MIN_SAMPLING_HZ` are flagged,
  not silently dropped — say which sessions were excluded and why.
- **Fixation durations at low rate are quantized** to one sample
  interval, and undersampled saccades merge adjacent fixations. Below
  ~20 Hz, report dwell only — no fixation counts or durations.
- **`status` is always 1**, so `valid_pct` measures nothing. All real
  loss here is frame decimation.
