# Eye-Tracking Web Experiment

Browser-based eye-tracking study for a master's thesis at the Technical
University of Munich (TUM), supervised by Dr. Christian Kosel.
Participants watch five video stimuli while their gaze is recorded with
[GazeFollower](https://github.com/GanchengZhu/GazeFollower)
(deep-learning webcam eye tracking). Every session includes a
**mandatory accuracy validation before and after the videos** — the
per-session accuracy (in px and degrees of visual angle) is a primary
outcome of the study and is stored in the session manifest.

## Quick start

```bash
# one-time setup (fixes the MNN wheel, creates a clean env if needed)
bash fix_environment.sh

# normal run
python app.py                      # → http://localhost:5050

# quick pipeline test: 1 video, 5 seconds, orange TEST MODE badge
TEST_MODE=1 python app.py       # macOS / Linux
python app.py --test            # any shell (Windows cmd/PowerShell too)
```

Health checks: `python tracker_service.py --check` (dependency + camera
diagnosis) or open `http://localhost:5050/tracker_check` while running.
`python run_tests.py` runs the integrity suite.

Lighting/frame-rate check: `python camera_fps_test.py` (stop the server
first) shows delivered webcam FPS and mean frame brightness live —
confirm a stable ≥ 25 Hz before recording. Webcams throttle their frame
rate in low light (auto-exposure), so sampling rate is
illumination-dependent; add front lighting until the FPS holds.

## Participant flow

1. **Login & consent** (`/`) — ID + password (hashed), mandatory
   consent, optional screen-diagonal entry (used for the px → degree
   conversion; a logged default is assumed otherwise).
2. **Environment setup + calibration** — standardized setup checklist
   (distance, lighting, camera height, head still), then the native
   fullscreen GazeFollower calibration (13 points). A green dot follows
   the gaze as live proof that calibration worked.
3. **Pre-validation (required)** — 5 targets at known positions; median
   measured gaze vs. target → **accuracy** (mean offset) and
   **precision** (RMS of sample-to-sample distances), each in px and
   degrees, checked against the preregistered threshold (default ≤ 3°,
   see `MAX_VALIDATION_ERROR_DEG`). Stored in the manifest.
   Afterwards a **gain correction** is auto-fitted from the validation
   targets (webcam trackers under-shoot eccentric gaze — "right
   direction, not far enough"); a slider allows manual fine-tuning with
   the live gaze dot, and the accuracy check can be re-run to verify.
   The correction is applied post-hoc (raw data untouched, corrected
   columns added, parameters logged in the manifest) — a session-wise
   recalibration in the sense of recent webcam eye-tracking work.
4. **Videos** — fullscreen, randomized order (deterministic per
   participant), gaze recorded continuously and segmented per video.
5. **Post-validation (required)** — 3 targets; quantifies calibration
   drift over the session (compare with the pre-validation error).
6. **Debrief** (`/complete`).

## Researcher tools

- **Gaze replay**: `http://localhost:5050/review` — replay any recording
  with the gaze point overlaid on the video.
- **AI feedback (research pipeline)**: same page. State-of-the-art
  design: keyframes at detected **fixations** (offline I-DT,
  `fixations.py`), gaze marker whose radius reflects the session's
  measured validation error, a **zoomed crop** of the attended region
  per keyframe, a **two-step prompt chain** (scene description without
  gaze → gaze evaluation against the rubric), **structured JSON output**
  next to the prose (for Cohen's-kappa validation against human
  coders), and optional **repeated runs** with a consistency metric.
  The Gemini model is **pinned** (`GEMINI_MODEL`, no rolling aliases)
  and every request/response is logged to `data/llm_logs/`.

## LLM–human agreement study (the main validation result)

```bash
# 1. Generate LLM feedback for the recordings in the review tool (/review)
# 2. Build blind-coding rating sheets from the logged structured output:
python agreement_kit.py export
# 3. Human raters watch the replays and fill the human_* columns
#    (instructions are embedded in each sheet)
# 4. Compute Cohen's kappa + raw agreement (per sheet and pooled):
python agreement_kit.py kappa data/agreement/*.xlsx
```

Note: run consistency (`n_runs` in the review tool) aligns phases by
index across runs — a simple heuristic; report it as such.

## Data

Everything lands in `data/` — see [DATA_README.md](DATA_README.md) for the
full data dictionary. Excel writes are atomic; corrupt files are backed up
(never overwritten); every session writes a JSON manifest (validation
results, preregistered quality thresholds, pass/fail verdicts, stimulus
timing, screen geometry) alongside the raw CSV for reproducibility.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `5050` | Server port |
| `TEST_MODE` | off | `1` = one stimulus, 5 s playback |
| `GF_CALI_MODE` | `13` | Calibration points (5/9/13) |
| `GF_MODEL_PATH` | `models/base_32M.mnn` | Path to the gaze model. The authors' 32M-image base model is bundled and loads by default; set to a different path to use another model, or `""` to force the library's stock 7M-image model |
| `SECRET_KEY` | auto | Flask session key (persisted in `.secret_key`) |
| `GEMINI_MODEL` | `gemini-3.5-flash` | **Pinned** LLM model (open `/llm_check` to list available models; record changes in the methods section) |
| `LLM_MAX_FRAMES` | `60` | Max keyframes per AI-feedback request |
| `VIEWING_DISTANCE_CM` | `60` | Assumed viewing distance (px → degrees) |
| `SCREEN_DIAG_INCHES` | `13.3` | Fallback screen diagonal if not entered |
| `MAX_VALIDATION_ERROR_DEG` | `3.0` | Preregistered accuracy threshold |
| `MIN_GAZE_SAMPLES_PCT` | `60` | Preregistered data-loss threshold |
| `FIXATION_DISPERSION_NORM` | `0.05` | I-DT dispersion threshold (normalized) |
| `FIXATION_MIN_DURATION_S` | `0.20` | I-DT minimum fixation duration |

## Methods-section checklist (what this software gives you)

Report per session: calibration points, pre/post validation error in
degrees (+ the assumed viewing distance and screen diagonal), Tobii-style
`gaze_samples_pct` per stimulus, sampling rate, exclusions against the
preregistered thresholds, the pinned LLM model + parameters, number of
keyframes/method, and LLM run consistency. Everything is in the session
manifests and `data/llm_logs/`.

## Citation

Zhu, G., Duan, X., Huang, Z., Wang, R., Zhang, S., & Wang, Z. (2025).
GazeFollower: An open-source system for deep learning-based gaze tracking
with web cameras. *Proceedings of the ACM on Computer Graphics and
Interactive Techniques, 8*(2), 1–18. (CC BY-NC-SA 4.0 — research use)

Salvucci, D. D., & Goldberg, J. H. (2000). Identifying fixations and
saccades in eye-tracking protocols. *Proceedings of ETRA 2000*, 71–78.
(basis of the I-DT fixation detection in `fixations.py`)
