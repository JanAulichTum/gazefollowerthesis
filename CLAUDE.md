# Project Memory — Eye-Tracking Web Experiment (Master Thesis, Jan Aulich, TUM)

Handoff context for any AI assistant continuing work on this project.
Read this FIRST. Supervisor: Dr. Christian Kosel.

**Scope, decided 2026-08-16.** This is a METHOD proof of concept: can a
webcam-gaze + multimodal-LLM pipeline produce data of known quality, at a
known analytic resolution, with feedback that corresponds to the recorded
gaze. The classroom domain is the vehicle, not the subject. There is NO
novice-vs-expert contrast and no domain hypothesis. Do not reintroduce one.

## What this is

Local Flask web app (port **5050**, `python app.py`) that runs a
webcam-eye-tracking experiment: login/consent → native calibration →
validation → fullscreen classroom videos → gaze data in Excel → researcher
review page with gaze replay + Gemini-based "what did they look at"
feedback. Everything runs on one machine; participants use the same
machine as the researcher. **Development is on macOS, but data
collection runs on Windows** — a difference that matters more than it
looks: the sampling-rate behaviour, the DPI/screen-space mismatch and
the console encoding are all platform-specific. Never assume the
collection platform from the development one; the manifest records
which produced each dataset.

## Architecture (three processes)

1. **Browser** — Jinja2 templates + `static/js/experiment.js` (single-page
   flow: calibration page hosts calibration, verification, AND video
   playback so fullscreen never breaks). SocketIO for events.
2. **Flask** (`app.py`) — routes, SocketIO, data persistence (pandas →
   xlsx, atomic writes), session segmentation, LLM feedback endpoint.
3. **Tracker subprocess** (`tracker_service.py`, spawned via
   `gaze_service.py` over a stdin/stdout JSON protocol, replies prefixed
   `@GF@`) — owns [GazeFollower](https://github.com/GanchengZhu/GazeFollower)
   (deep-learning webcam gaze, MNN runtime) + the pygame calibration UI.
   Separate process because macOS requires GUI on a main thread.

Key files: `config.py` (all constants/env), `gaze_vision.py` (gaze-annotated
video frames for the LLM), `fixations.py` (I-DT fixation detection),
`quality_report.py`, `run_tests.py` (integrity suite — RUN AFTER CHANGES),
`tidy_data.py`, `fix_environment.sh`, `DATA_README.md` (data dictionary —
kept current), `README.md`.

## Environment (macOS development / Windows collection)

- **Platform split.** `setup_env.sh` / `fix_environment.sh` are the macOS
  path; `check_setup.ps1` and `windows/` cover the collection machine.
  Anything touching sampling rate, DPI scaling or console encoding must
  be verified on Windows specifically — see the EcoQoS entry under
  hard-won gotchas, the screen-space mismatch entry, and the cp1252
  entry. `perf_mode.py` is active on BOTH (EcoQoS on Windows, QoS +
  `PRIO_DARWIN_BG` on macOS) because Apple Silicon has the same
  P-core/E-core demotion reached through a different API.
- **`bash setup_env.sh` creates the current env, `gaze_thesis`**, from the
  pinned `environment.yml` and then verifies it (arch, imports, tracker
  self-check, integrity suite). On Apple Silicon it forces
  `CONDA_SUBDIR=osx-arm64` and pins that into the env — otherwise conda
  can build an x86_64 env that runs under Rosetta, where MNN's wheels are
  broken and inference is roughly half speed. `-f` replaces an existing
  env. The older `gaze_native` env from `fix_environment.sh` is left in
  place as a fallback; **record which env produced each dataset**, since
  package versions change the sampling rate and the rate changes the
  fixation statistics.
- Python 3.11 deliberately: 3.12 pulls a protobuf that breaks mediapipe's
  legacy `mediapipe.solutions` API (which GazeFollower needs), and the
  Anaconda base 3.9 had a broken MNN wheel. Camera permission for the
  terminal app is required (System Settings → Privacy → Camera).
- `TEST_MODE=1 python app.py` → login page shows test panel: calibration
  quick(5pt, no preview)/full(13pt)/skip(reuse saved model); videos
  5s/1 full/all. Orange badge shown.
- Env vars: `PORT` (5050), `GF_CALI_MODE` (13 default), `GF_MODEL_PATH`
  (authors' 32M model — request via email template in README),
  `LLM_MAX_FRAMES` (60), `GEMINI_API_KEY` or `.gemini_key` file (a default
  key is stored there).

## Hard-won gotchas (violate these and things silently break)

- **GazeFollower allows ONE `save_data()` per instance** (closes its
  stream permanently). Hence: continuous recording per session, segmented
  afterwards by wall-clock ns windows; instance is released & recreated
  after every session (`cmd_end_session`).
- **Gaze coordinates are LOGICAL pixels == CSS pixels** (macOS points).
  NEVER scale by devicePixelRatio. Screen coords == page coords only in
  fullscreen; otherwise subtract `screenToViewportOffsets()` (window pos +
  browser chrome).
- **`timestamp` = epoch nanoseconds** (19 digits; looks constant, isn't).
  Flask `time.time_ns()` and camera timestamps share the same clock.
- **Browser autoplay**: video+audio playback needs a user gesture within a
  few seconds ("transient activation"); the per-video retry overlay
  (`#startOverlay`) handles expiry. Don't remove it.
- **CSS `[hidden]{display:none!important}`** exists because class-level
  `display:flex` otherwise defeats the `hidden` attribute (caused
  invisible-video and overlapping-page bugs). Keep it.
- **pandas `to_excel` infers engine from the file EXTENSION** — temp files
  for atomic writes must end in `.xlsx`.
- **Background-task exceptions in Flask-SocketIO threading mode are
  silent** — always try/except + `logger.exception` in
  `start_background_task` bodies.
- Excel appends are atomic; corrupt files are preserved as
  `.corrupt-<ts>`; every session writes a `_manifest.json` (validation
  results, quality metrics, timing windows, screen geometry).
- Group analysis rows by **participant + session_id + stimulus** (repeat
  runs otherwise interleave — caused a "flickering replay" bug once).
- Multiple sessions per server run are fine; the tracker resets itself.
  pygame is fully quit + Dock icon hidden after calibration
  (`TransformProcessType`) — else macOS shows "Python not responding".
- Static assets are cache-busted with `?v=` per server start; users must
  restart the server to see JS/CSS changes.

## Data & quality (see DATA_README.md for the full dictionary)

- Primary output `data/gazefollower_data.xlsx`; use
  `corrected_gaze_position_x/y` (or `filtered_*` if no correction) and
  `gaze_video_nx/ny` (0–1 relative to video frame; outside 0–1 =
  letterbox). `video_time_s` is the analysis time axis.
- Tobii-style **`gaze_samples_pct`** per stimulus in each manifest =
  valid ÷ expected at **`NOMINAL_SAMPLING_HZ`** (config constant, 30 Hz =
  the camera's rated rate). Report the percentage AND the nominal rate;
  the number is meaningless without its denominator. Until 2026-07-31 the
  denominator was derived from the session's own median frame interval,
  which made it circular — every session scored ~100 % and frame
  decimation cancelled out. Re-run `quality_report.py` for any figure
  taken from a manifest written before that date. `relative_yield_pct`
  keeps the old ratio for comparison; it is a diagnostic, not data loss.
- **THE BIG ONE: GazeFollower DROPS every failed-detection frame.**
  `process_frame` dispatches all frames, but `_gaze_info_2_string` does
  `raw_gaze_coordinates[0]` — which is `None` when detection failed — so
  `_write_sample` raises `TypeError`, `WebCamCamera.capture` swallows it,
  and logs TWO full tracebacks. The sample is lost. Consequences:
  `status` was always 1 (only successes ever reached the CSV);
  `valid_pct` was meaningless; and **the recorded rate is the DETECTION
  SUCCESS rate, not the capture rate**. Measured 2026-08-03: pipeline
  captures 31.3 Hz at 17.4 ms/frame (half the 33.3 ms budget) but records
  ~18 Hz because ~42 % of frames fail detection. `sample_patch.py` fixes
  it — failed frames are written with `status=0` and coordinate sentinel
  −65536 instead of being dropped. On by default; `GF_SAMPLE_PATCH=0`
  restores the lossy behaviour for comparison.
  **Therefore a low recorded rate has two distinct causes that look
  identical in the number alone**: frames arriving fine but yielding no
  gaze estimate (detection — lighting, distance, camera height, glare),
  or frames arriving slowly (compute — EcoQoS/E-core demotion, competing
  load). `detected_pct` alongside `sustained_hz` is what separates them;
  never diagnose from the rate alone. See the EcoQoS entry below —
  chasing the detection explanation exclusively cost days.
- **Gaze inference runs on the CPU, always.** GazeFollower hardcodes
  `{'precision':'low','backend':0,'numThread':4}` in
  `MGazeNetGazeEstimator.__init__`; MNN backend 0 = CPU, and `numThread`
  only affects CPU. `mnn_backend.py` patches
  `MNN.nn.create_runtime_manager` (a single choke point, so it does not
  depend on GazeFollower's class layout) to honour `GF_MNN_BACKEND` /
  `GF_MNN_THREADS` / `GF_MNN_PRECISION`; applied in `_ensure_gf` before
  the first `GazeFollower()`. **MNN silently falls back to CPU when the
  requested backend is not compiled into the wheel** — the stock PyPI
  `MNN` is CPU-only — so always verify with `python mnn_backend.py`,
  which flags any backend whose timing matches CPU. MediaPipe FaceMesh
  is CPU-only in its Python API regardless. Codes: 0=CPU, 1=METAL,
  2=CUDA, 3=OPENCL, 7=VULKAN. The active runtime appears in
  `tracker_service --check` and in `manifest["rate_gate"]["mnn_runtime"]`.
- **GazeFollower's camera settings never take effect.**
  `WebCamCamera.__init__` calls `_cap.set(WIDTH/HEIGHT/FPS)` on a
  VideoCapture that has NOT been opened yet (`_cap.open()` happens later,
  in `open()`), so the sets are no-ops. Frames arrive at the webcam's
  native resolution and every frame pays a full-size BGR→RGB convert plus
  a software `cv2.resize` to 640×480. Worse, `capture()` invokes the
  callback **synchronously in the capture loop**, so per-frame cost
  directly gates capture — cross 33.3 ms and the rate halves.
  `camera_patch.py` subclasses it to apply the properties after `open()`
  (plus `CAP_PROP_BUFFERSIZE=1`, DirectShow on Windows). **Opt-in**
  via `GF_CAMERA_FIX=1` because it changes what the model sees; A/B it
  with `tracker_fps_test.py` before adopting, and record the choice.
- **GazeFollower never PERSISTS a calibration.** `SVRCalibration` has
  `save_model()`, but nothing in GazeFollower calls it, so
  `~/GazeFollower/calibration/svr_*.xml` is never written and every fresh
  instance starts uncalibrated. Do NOT "fix" this: `SVRCalibration`
  auto-loads any file it finds and sets `has_calibrated=True`, so a
  persisted model would let a later participant silently inherit an
  earlier one's mapping. `cmd_begin_stimulus` refuses to record unless
  `self.calibrated` was set by a calibration in THIS process.
  `tracker_fps_test.py` therefore stubs `calibration.predict` (timing is
  unaffected — it replaces a microsecond SVR predict).
- **Windows consoles are cp1252.** Piping through a subprocess makes
  Python use it even on 3.12, so one `≈`/`✓`/`≥` raises
  UnicodeEncodeError and kills a report mid-run. Every CLI tool
  reconfigures its own stdout to UTF-8, and `run_all.py` also sets
  `PYTHONUTF8`/`PYTHONIOENCODING` for its children. Keep both.
- **GazeFollower cannot sample without a calibration model.**
  `process_frame` RAISES on every frame ("No calibration model is
  available") rather than returning empty gaze — zero samples plus a
  flood of identical tracebacks. Guard any sampling code that may run
  pre-calibration with `gf.calibration.has_calibrated`. This bit both the
  rate gate and `tracker_fps_test.py`; both now pre-flight it. The model
  persists to `~/GazeFollower` (once per machine).
- **The rate check must stay PASSIVE and two-phase.** `cmd_rate_check_start`
  / `cmd_rate_check_result`: sample arrival times are appended by
  `_on_sample` (already called once per frame), and app.py yields with
  `socketio.sleep` in between. The first version blocked in a 25 s
  polling loop, which held `GazeService._lock` for the whole window —
  the browser's live gaze preview froze, and an accuracy check run
  during it collected stale samples and reported ~5° error, which then
  made the gain auto-fit bail with "implausible horizontal gain". Never
  reintroduce a long-blocking tracker command. The JS also keeps the
  accuracy-check button disabled until the verdict arrives (with a 90 s
  watchdog so a missing verdict can't strand a participant).
- **A normal run shows ONE 30 s clip** (`SESSION_STIMULUS_MODE`=clip30 by
  default; `1full` or `all` are the alternatives). Set it to `all` for
  real data collection and record which mode produced each dataset.
- **Nothing in the participant flow waits for the rate measurement.** It
  runs in the background from calibration onward; the verdict gates the
  *videos* button, not the accuracy check. Gating the accuracy check made
  participants sit and wait, which is why it moved.
- **The accuracy check used to RACE the live preview.**
  `stop_gaze_preview` only sets a flag the loop checks every 150 ms, and
  `start_gaze_preview` refused to start while that flag was still set —
  so stopping and immediately restarting (exactly what the accuracy check
  does) could leave NO preview loop running. The validation then
  collected almost no samples and reported a meaningless error. Fixed
  with a `preview_generation` counter: the newest loop wins, older ones
  retire and must not clear the shared flag. Also, `requestFullscreen()`
  was fired and not awaited, so the FIRST target was positioned in the
  windowed viewport while the gaze that followed arrived in the
  fullscreen frame. Both now fixed; every validation records `geometry`
  (fullscreen state, inner/outer/screen size, offsets, devicePixelRatio)
  and per-target `n_samples`, and the log warns when any target
  collected < 5 samples (a 1.6 s window at 30 Hz should yield ~45).
- **"Calibration perfect, validation catastrophic" = PIXEL-SPACE
  MISMATCH, not a tracking failure.** GazeFollower's `DefaultConfig` sets
  `screen_size = [get_monitors()[0].width, .height]` from **screeninfo**
  — PHYSICAL monitor pixels — and `convert_to_pixel` scales all gaze into
  that space. The browser positions validation targets in **CSS** pixels.
  They diverge when (a) display scaling ≠ 100 % (a 2560×1440 panel at
  Windows 150 % is 1707×960 to the browser → every gaze coordinate ~1.5×
  too far from the origin, error growing toward the edges), or (b) there
  are multiple monitors and the browser is not on `monitors[0]`.
  Calibration stays self-consistent inside the tracker's own space, so it
  looks flawless; only the browser-side check exposes it. **Recalibrating
  cannot fix it.** `cmd_screen_info` + `_screen_space_check()` compare the
  two spaces on every validation and store `screen_space` in the record;
  a mismatch is shown first in the review panel. `check_screen_space.py`
  is the standalone check (also queries the Windows DPI scale factor).
- **SOLVED 2026-08-04 — the rate collapse was Windows parking the tracker
  on efficiency cores.** Windows 11 judges the tracker subprocess to be
  background work (the fullscreen browser holds the foreground) and acts
  on it twice: **EcoQoS** caps its clock, and on hybrid CPUs **Thread
  Director** parks its threads on E-cores. Neither raises an error;
  frames simply take longer.
  The diagnostic signature is decisive — per-stage timers in the live app
  showed both model stages slowing by the *same* factor with the browser
  up:

  | | no browser | browser | ratio |
  |---|---|---|---|
  | MediaPipe FaceMesh | 9.3 ms | 21.1 ms | 2.27x |
  | MNN gaze CNN | 19.3 ms | 43.6 ms | 2.26x |
  | per frame | 28.9 ms | 65.0 ms | — |

  MediaPipe is single-threaded TFLite, MNN is 4-threaded. **Resource
  contention cannot slow two engines with different threading models by
  an identical factor.** A uniform scalar across all CPU work means the
  CPU is executing more slowly. **The fix is `perf_mode.py`** (EcoQoS
  opt-out via `SetProcessInformation`/`StateMask=0`, ABOVE_NORMAL
  priority; macOS equivalent clears `PRIO_DARWIN_BG` and raises thread
  QoS to USER_INTERACTIVE). Applied at tracker start, before any
  inference: **15.1 Hz → 29.4 Hz**, FaceMesh back to 9.6 ms and the gaze
  CNN to 20.0 ms. `GF_PERF_MODE=0` disables it; the active policy is
  recorded in every manifest and asserted by `run_tests.py`.
- **Why `hz_experiment.py` said the opposite, and why that was not a
  contradiction.** The same day, `hz_experiment.py` (60 s per condition,
  recorded clip as input) measured a flat 30.0 Hz in EVERY condition —
  baseline, two repeats, GazeFollower's own writer, flush-on-every-sample,
  8 MNN threads — 100 % detection, no slide across twelve 5-second
  buckets, and concluded the software was exonerated. **It ran with no
  browser**, so the tracker WAS the foreground process and was never
  demoted; note its 30.0 Hz matches the 28.9 ms/frame "no browser" row
  above. The experiment was sound and its conclusion was true of the
  condition it tested — it simply never reproduced the condition that
  caused the bug. **Lesson worth keeping: a benchmark that omits the
  browser cannot speak to a problem whose mechanism IS the browser
  holding the foreground.** Any future rate benchmark must either run a
  real session or state explicitly that it does not.
- **`python session_probe.py`** walks the REAL session lifecycle against
  the fixed clip — the churn every other benchmark skips. `cmd_cycle_sampling`
  reproduces the stop/start pattern calibration and the accuracy check
  perform (each `start_sampling()` appends `_write_sample`; upstream's
  `remove_subscriber` deletes while iterating), and the probe reports the
  rate AND the subscriber count after each stage. Expect `subscribers`=2.
- **The live app now keeps a `rate_history`** — every measurement with a
  `stage` label, logged as a timeline and stored in the manifest. The
  rate is measured before the videos and again after them, so a drift
  across a session is visible from one run instead of needing to be
  reproduced live.
- **`python preview_load_test.py`** tests whether the live gaze preview
  costs rate. IMPORTANT: `hz_experiment.py` ran the tracker IN-PROCESS
  with no Flask/IPC, and `diagnose_rate.py`'s "polled" condition called
  `get_gaze_info()` in-process — a cheap attribute read. Neither tests
  the real preview, which is a full JSON round trip into the tracker
  subprocess (`GazeService._send`, holding a lock) whose main thread then
  contends for the GIL with the capture thread running MediaPipe + MNN.
  This test uses the real subprocess and brackets the app's actual
  `socketio.sleep(0.15)` with 50 ms and 20 ms polling.
- **`python camera_light_test.py`** alternates a fullscreen white/black
  window while measuring delivered camera fps and image brightness. A
  large fps drop on the dark phase means the webcam is lengthening
  exposure (it cannot expose longer than one frame interval, so it halves
  the rate) — a LIGHTING fix, not a code fix.
- **`python hz_experiment.py`** is the unattended rate investigation:
  runs the real pipeline against the fixed clip under one-variable-at-a-
  time conditions (baseline, two repeats, stock writer, flush-every-
  sample, 8 threads, BlazeFace), **each in a fresh subprocess**, then
  prints a table and a verdict. The fresh process is the point: it
  separates an in-process leak (repeats identical, slide within each run)
  from thermal/system-wide decay (later runs start lower). ~7 min.
- **Headless replay mode** (`fake_camera.py`): `GF_FAKE_CAMERA=clip.mp4`
  replaces the webcam with a looping recording, `GF_FAKE_CALIBRATION=1`
  stubs the calibration mapping and skips the calibration UI. Everything
  expensive still runs — FaceMesh, the gaze CNN, clipping/resizing, the
  filter, the CSV writer — only the pixels are deterministic, so rate
  measurements become repeatable and need no person. Record a reference
  clip once with `python fake_camera.py --record`, verify with
  `--check`. Inert unless the env vars are set; every simulated run is
  marked `fake_mode` in the self-check and the manifest. **Never treat
  fake-mode output as participant data** (the gaze coordinates are raw
  model output, unmapped).
- **`GF_FACE_ALIGNMENT=blazeface`** switches face alignment to
  GazeFollower's BlazeFace, which upstream ships explicitly "to reduce
  inference time". Measured here, FaceMesh is only ~2.2 ms of a ~19.7 ms
  frame, so it cannot buy much on this hardware — and BlazeFace's
  landmark geometry differs, so accuracy must be re-validated. Measure
  before adopting.
- **A low rate has TWO causes that look identical.** Frames arriving
  slowly (compute: **EcoQoS / E-core demotion first — check
  `perf_mode` in the manifest before anything else** — then competing
  load) versus frames arriving fine but yielding no gaze estimate
  (detection: camera height, distance, lighting, glare). The gate reports
  `detected_pct` alongside `sustained_hz` so the UI can name which one
  instead of guessing — earlier versions asserted "the turbo window
  closed" with no evidence, and the opposite error (asserting detection
  quality, treating compute as closed) cost days on the EcoQoS hunt.
  **Neither cause may be asserted without its measurement.**
- **Rate gate** (`RATE_GATE_SECONDS`=25, tail 8 s) runs
  **after calibration succeeds**, via `_run_rate_gate()` in the
  `native_calibration_result` path (`run_rate_gate` event re-runs it).
  It cannot run earlier — see above — and that placement is better
  anyway: calibration is a 1–2 min inference workload, so the machine is
  already warm and under the real load, and the figure is a sustained
  rate rather than a cold-start one.
  Below `MIN_SAMPLING_HZ` the *accuracy-check* button is disabled; the
  researcher can override with a reason, stored in
  `manifest["rate_gate"]`. It WARNS rather than hard-blocks on purpose —
  the participant is physically present and pilot runs are legitimate —
  but an overridden session is permanently marked.
- **The rate is bimodal at 29 Hz / 14.5 Hz — a cliff, not a slope.**
  Per-frame inference sits right at the 33.3 ms camera frame period; when
  it crosses, the loop misses every other frame and locks to half rate.
  Observed sessions start at either 29 or 15 Hz, and some drop 29→15
  mid-session. Two 4-minute sessions held 29 Hz throughout, so the
  hardware CAN sustain it — the setup is marginal, not incapable.
  **What decides which side of the cliff a session lands on is
  overwhelmingly whether EcoQoS demoted the tracker** (2.26x on all CPU
  work is more than enough to cross a 33.3 ms budget); "the turbo window
  closed" was the story before `perf_mode.py` and is not the mechanism.
  **AC power is NOT the axis** — 29.4 Hz was measured on battery at 53 %
  with EcoQoS disabled, and 15.1 Hz on the same charge with it active.
  Jan said the charger made no difference and he was right. Residual
  headroom is thin: 29.9 ms of a 33.3 ms budget (p90 33.5), so competing
  CPU load still matters at the margin. `GF_PERF_PRIORITY=high` and
  `GF_PERF_PIN_CORES` are untried levers if more is needed.
- Measured quality on this hardware: jitter RMS ~45–60 px filtered;
  vertical accuracy is the weak axis (gain compression) — mitigations:
  13-pt calibration, camera at eye level, gain correction, 32M model.
- Validation: **two grids, not one.** Grid A (`pre_fit`, the one the
  correction is fitted to) has **13 targets**; grid B (`pre_check` and
  `post`, never fitted to) has **7** — both five vertical elevations,
  disjoint positions, eccentricity-matched (`VALIDATION_GRID` /
  `VALIDATION_CHECK_GRID` in experiment.js; `run_tests.py` [17] asserts
  the disjointness and the match). Identical grid-B sets pre/post are
  required for drift to be like-for-like; the old 3-target post check
  made drift partly an artefact of which targets were dropped. Grid A
  grew from 7 to 13 on 2026-08-18 (F33/brief item 2) so a full-affine
  correction — the only candidate that can represent `m_yx`, vertical
  error caused by horizontal position — has enough leave-one-out
  headroom to be more than noise; see
  `validation_stats.FULL_AFFINE_MIN_TARGETS`. Sessions recorded before
  that date have a 7-target grid A and cannot receive a full-affine
  correction, by design (`select_correction` gates it on target count).
- Drift is computed from **uncorrected** errors (`mean_err_deg_raw`) —
  the pre check is measured raw and the post check with the gain
  correction applied, so differencing the reported numbers conflates
  drift with the correction's effect. `drift_basis` records which basis
  was used.
- The gain correction reports **`gain_x` and `gain_y` separately**. The
  old single `gain_mean` could read ×1.0 while one axis was compressed
  and the other expanded — the exact webcam failure mode.

## LLM feedback (research pipeline, `/review` page)

- Gemini API (key optional in UI; falls back to `.gemini_key`). Model
  pinned; every call logged to `data/llm_logs/`. temperature 0.
- Pipeline: I-DT fixations → one annotated keyframe per fixation (marker
  radius = session's validation error) + zoomed crop → two-step chain
  (scene description without gaze first = hallucination check, then
  rubric evaluation) → structured JSON summary for Cohen's-kappa
  validation against human raters.
- **One API call per evaluation step** (frames ride inline). Scientific
  design: calls = participants × videos × 3–5 repetitions; report
  inter-run consistency + LLM-vs-human agreement on a ~20% subset.
  Cost with billing ≈ half a cent per call.
- **No novice/expert characterisation is asked of the model.** An earlier
  prompt asked for expert-vs-novice viewing patterns; no research question
  consumes that, and asking for it adds uncontrolled degrees of freedom to
  the output. The model reports what region was attended at the gaze
  location and nothing about viewer expertise.

## Design system

Welcome-page card format everywhere (light theme, TUM blue #0065BD,
Inter, `.card`/`.card-actions`, ONE filled primary button per view,
outline `.btn-secondary`); dark theme (blue-navy, accent #4A9FE8) ONLY
while dots/videos are on screen (`document.body.dataset.theme`).
Single-hue TUM-blue gradients; a11y: aria-live statuses, focus-visible,
reduced-motion. TEST badge + options panel amber #b45309.

## Debugging order

1. `data/server.log` and `data/tracker_service.log` (full tracebacks land
   there; tracker stderr is redirected to the log file).
2. `http://localhost:5050/tracker_check` or
   `python tracker_service.py --check` — per-dependency + camera + arch
   diagnosis (detects the broken-MNN / Rosetta cases).
0. `python run_all.py` — **the one-command entry point.** Runs every
   check below in order, pauses for the two interactive steps
   (calibration, full session), and prints a plain-language VERDICT plus
   `data/run_all_<ts>.md`. `--auto --quick` for the non-interactive
   subset. See `TESTING.md` for how to read each stage.
3. `python run_tests.py` — static integrity checks (socket-event
   contracts, DOM ids, CSS invariants, stimuli resolvable, tracker
   protocol, gain round-trip) **plus section [8] quality-metric
   integrity**, which asserts the *formulas* (nominal denominator,
   pre/post target parity, drift basis, per-axis gain, rate-gate wiring).
   Those bugs were silent — they produced plausible numbers that measured
   nothing — so only a formula assertion catches a regression. Keep green.
4. `python quality_report.py` for data-quality questions.
4b. `python diagnose_rate.py` when the RATE is the question — instruments
   the real pipeline and splits per-frame cost into FaceMesh / gaze CNN /
   GazeFollower overhead, reports the resolution the camera actually
   delivers, and A/Bs the live preview and `GF_CAMERA_FIX`.
4c. `python perf_mode.py --verify` — **check this FIRST on any rate
   question.** Confirms the EcoQoS opt-out and priority actually took.
   Caveat it prints itself: a console is already the foreground process,
   so a clean result proves the API calls succeed, NOT that the rate is
   recovered under a fullscreen browser. Only a real session tests that;
   compare `perf_mode` across sessions with `diagnose_session.py`.
5. `python backfill_manifests.py --dry-run` after changing any quality
   formula, to see how existing sessions would be re-scored.

## History in one paragraph

Started from a PyGaze/Tobii plan → pivoted to webcam-only (WebGazer +
vendored pupil tracker) → replaced with GazeFollower as primary (WebGazer
kept as backup, later removed entirely) → long debugging arc (MNN wheels,
camera permission, autoplay, coordinate spaces, one-save-per-instance,
Excel atomicity) → session-based recording with post-hoc segmentation →
researcher tooling (replay overlay, accuracy validation, quality metrics,
Gemini frame-based feedback with rubric, llm audit logs, fixation
detection). The stimuli are classroom scenes; the research frame is
teacher professional vision (noticing), which the LLM feedback rubric
operationalizes.
