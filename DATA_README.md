# Data Dictionary — Eye-Tracking Experiment

All output lives in `data/`.

## Files

| File | Content |
|---|---|
| `participants.xlsx` | Participant ID, hashed password, consent flag, registration time, screen diagonal (if entered) |
| `gazefollower_data.xlsx` | **Gaze data** — one row per gaze sample, segmented per stimulus |
| `gazefollower_raw/*.csv` | Raw continuous session recordings (backup; one file per session), named `<participant>_<YYYY-MM-DD>_<HHMMSS>.csv` — the basename is the `session_id` |
| `gazefollower_raw/*_manifest.json` | Per-session record: **pre/post validation results (px + degrees)**, preregistered quality thresholds + pass/fail verdicts, stimulus timing windows, screen geometry, data-quality metrics |
| `llm_logs/*.json` | Audit trail of every LLM call: pinned model, parameters, all prompt text, image hashes, raw response |
| `server.log`, `tracker_service.log` | Diagnostics (check these first when something fails) |

Legacy: `webgazer_data.xlsx` may exist from sessions recorded before the
secondary WebGazer stream was removed; it is no longer written.

## `gazefollower_data.xlsx` — columns

| Column | Meaning |
|---|---|
| `participant_id` | Who was recorded |
| `session_id` | Which recording session (a participant can have several; **always group by participant + session + stimulus**) |
| `stimulus_name` | Which video was playing |
| `video_time_s` | **Seconds since this video started** — use this as your time axis |
| `clock_time_utc` | Human-readable wall-clock time (UTC) of the sample |
| `timestamp` | Raw camera timestamp in epoch **nanoseconds** (ns since 1970-01-01 UTC). 19 digits; the leading digits only change over months, which makes values look "constant" — the differences are in the last ~10 digits. `timestamp / 1e9` = Unix seconds. |
| `raw_gaze_position_x/y` | Uncalibrated model output (arbitrary units) — ignore for analysis |
| `calibrated_gaze_position_x/y` | Gaze position in **screen pixels** (logical px, origin top-left) after calibration |
| `filtered_gaze_position_x/y` | Same, after smoothing |
| `corrected_gaze_position_x/y` | Filtered position after the session's **gain correction** (only present when a correction was active; parameters in the manifest's `gain_correction` block) — **recommended for analysis** |
| `gaze_video_nx`, `gaze_video_ny` | **Gaze position relative to the video frame**: (0,0) = top-left, (1,1) = bottom-right of the video content; values outside 0–1 = gaze on the letterbox/off the frame. Computed from the corrected position when a correction was active. Basis for the replay overlay, fixation statistics, and AI feedback. |
| `left/right_eye_openness` | Eye-opening measure (relative units; useful for blink detection) |
| `tracking_status`, `status` | 1 = face/gaze successfully tracked in this frame |
| `event` | GazeFollower internal event code |
| `trigger` | Stimulus markers: onset = 100+2k, offset = 101+2k (k = 0,1,2,…); 0 = no marker |

## Researcher tools

- **Gaze replay**: `http://localhost:5050/review` (linked from the welcome
  and debrief pages) — replay any recording, selected per session, with the
  gaze point and trail overlaid on the video.
- **AI feedback (research pipeline)**: on the same page. The server
  detects **fixations** (I-DT, `fixations.py`), extracts a keyframe per
  fixation with the gaze position drawn onto it (marker radius = the
  session's measured validation error) plus a zoomed crop of the
  attended region, and runs a **two-step prompt chain** against the
  **pinned** Gemini model: (1) scene descriptions without gaze
  (hallucination check), (2) evaluation against your free-text
  **evaluation criteria**, with a structured JSON summary
  (`criteria_met` per phase — the basis for Cohen's-kappa validation
  against human raters). Optional repeated runs report a consistency
  metric. The prompt's numeric context is a grid-free **fixation
  summary** (count, median duration, time-in-fixations %, off-video %,
  per video third) — the coarse 3×3 region grid is used ONLY in the
  fallback when the video file cannot be read (without images the model
  needs some spatial vocabulary). Every request/response is logged to
  `llm_logs/`. The API key is used per request and never stored.
- **Accuracy validation (mandatory)**: five targets after calibration
  (pre) and three targets after the last video (post/drift check) —
  measured median gaze per target, per-target pixel error, mean error
  in px and **degrees of visual angle**, verdict against the
  preregistered threshold. Results land in the session manifest.
- **Quality report**: `python quality_report.py [session.csv]` — sampling
  rate, frame gaps, dropout, Tobii-style gaze samples %, jitter (raw vs
  filtered vs extra-smoothing headroom), vertical/horizontal noise ratio.

## Test mode

Start with `TEST_MODE=1 python app.py`. The login page then shows a
test-options panel: calibration **quick** (5 points, no preview) / **full**
(13 points + preview) / **skip** (reuse last saved calibration model), and
videos **first video 5 s** / **first video full** / **all 5 full**. An
orange badge marks all pages while test mode is active. Normal runs are
unaffected: 13-point calibration with preview, all videos, full length.

## Data loss (Tobii-style "gaze samples")

Every session manifest contains a `data_quality` block per stimulus:

- `valid_pct` — successful gaze estimations ÷ captured camera frames.
  **Not a quality measure here:** GazeFollower marks every frame it emits
  as valid (`status` is always 1), so this is ~100 % by construction.
- `gaze_samples_pct` — Tobii's definition: valid samples ÷ samples
  expected at the **nominal** rate (`NOMINAL_SAMPLING_HZ`, 30 Hz = the
  camera's rated frame rate, recorded in `quality_thresholds`). Because
  the denominator is a fixed constant, dropped/decimated frames actually
  count as loss — which is the dominant loss mechanism on this setup.
  **This is the number to report in the methods section**, always
  together with its nominal rate. Measured range on this hardware:
  46–94 %.
- `relative_yield_pct` — the same ratio against *this session's own*
  median rate. Near 100 % by construction; a diagnostic for whether one
  stimulus ran slower than the rest of the session, **never** a
  data-loss figure.
- `sampling_hz`, `sampling_hz_fastest`, `rate_ratio`, `rate_multimodal` —
  rate shape. A recording that alternates between full and decimated rate
  is not described by a single Hz, and its fixation timing is biased in a
  time-varying way.

> **Sessions recorded before 2026-07-31** used a `gaze_samples_pct`
> whose denominator was derived from the recording itself, so every
> session scored ~100 % regardless of decimation. Run
> `python backfill_manifests.py` to recompute them; the original values
> are preserved under `legacy_data_quality` and as
> `*_manifest.pre-backfill.json`. Four pilot sessions flip PASS → FAIL.

`manifest["rate_gate"]` additionally records the sustained rate measured
*before* calibration, and whether a failing verdict was overridden (with
the researcher's reason). This separates "we knew the session was
degraded" from "we found out afterwards".

The same metrics are printed by `quality_report.py` and logged to
`server.log` at the end of every session.

## Notes

- Recording runs **continuously** across all videos; rows are assigned to
  videos by timestamp window. Samples between videos (rest screens) are
  not included in the Excel file but remain in the raw session CSV.
- Screen coordinates are **logical pixels** (identical to CSS pixels),
  origin at the top-left of the display. The experiment runs fullscreen,
  so screen and page coordinates coincide during recording.
- Rows where `status` = 0 (face lost) should be excluded from analysis.
- Typical sampling rate is hardware-dependent (≈ 15–30 Hz on laptop
  webcams) and can **decimate to half rate under CPU/thermal load**.
  Each session manifest records `sampling_hz` and a `low_sampling_rate`
  flag per stimulus (threshold `MIN_SAMPLING_HZ`, default 20 Hz);
  low-rate sessions inflate fixation durations (onset/offset
  quantization) and should be reported/excluded accordingly. Fixation
  detection is rate-adaptive and each fixation carries a
  `duration_uncertainty_s` (= one inter-sample interval).
- The pre-calibration **position guide** estimates viewing distance from
  inter-ocular pixels; when captured, the manifest's `head_position`
  block and the validations' `viewing_distance_measured` flag record
  that the degrees-of-visual-angle conversion used a measured distance
  rather than the assumed constant.
- Excel writes are atomic and corrupt files are preserved as
  `.corrupt-<timestamp>` backups — data is never silently overwritten.
- All workbooks are auto-styled on write (bold header, frozen top row,
  filter, column widths); gaze positions are rounded to 2 decimals in
  Excel (originals stay in the raw CSVs). `python tidy_data.py --apply`
  migrates legacy file names and re-applies the styling.
- Calibration: 13 points by default (better vertical accuracy), 5 in test
  mode; override with `GF_CALI_MODE`. The authors' stronger 32M-image
  model can be used via `GF_MODEL_PATH=/path/to/model.mnn`.
