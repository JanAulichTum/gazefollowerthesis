# Pre-collection audit — webcam gaze + LLM feedback instrument

**Audited commit:** `c921a06ad38b0ec97b389afb230eed999ae13cc8` (`main`, 2026-08-19, = `origin/main` = local HEAD)
**Audit date:** 2026-10-01 · **Mode:** read-only (no code, config or data changed; this file is the only addition)
**Machine used:** macOS (Intel i7-1068NG7), project env `gaze_thesis` (Python 3.11.14). The collection machine (Windows) was **not** accessed; its state is inferred from its logs in `../data-2/`.

> **Do not commit or push this file to the public repository until the GitHub purge in PB-1 is complete.** It contains the commit IDs of the exposed objects. Two pilot sessions whose labels embed first names are written here as `[named]_P1` and `[named]_P2`.

---

## 1. Verdict

**Not ready to collect.**
1. The public GitHub repository still serves a pre-rewrite commit holding a server log that its own commit message says contains real participant names. Separately, the pre-registration that would fix the exclusion rules is unsigned, contradicts Design A, and leaves all ten pilot sessions classified as evaluation data.
2. The recording path has failure modes that are cheap to fix now and impossible to repair later:
   - In 9/9 pilots the first-presented clip's time window is 335–600 ms longer than the 30.000 s video, with no media-clock log to locate the error.
   - The manifest, the only record of accuracy, is written after a fragile Excel rewrite.
   - Validation attempts disappear on a page reload, and rate-gate overrides are overwritten.
   - Counterbalancing is unbalanced at plausible N.
   - No session records which code, packages, model or stimuli produced it, while `START.bat` auto-pulls new code.
3. The measurement core is sound. From raw files I re-derived every validation figure, both degree rulers, data loss and sampling rate to within 0.3 px / 0.01°. perf_mode holds 29–30 Hz.
4. Two analysis-side faults:
   - `claim_check` applies the gain correction incorrectly, so every stored correspondence figure on a corrected session is wrong. That includes F19's 16.9 / 28.8 %, which re-scores to **54.2 / 76.3 %**.
   - The coding tool cannot yet run Design A: no free text on WRONG, no real blind-first pass, and codes keyed only by fixation index.
5. Getting to Go needs roughly 4–6 working days of code, one rehearsal day on the collection machine, and a GitHub purge request that does not depend on any code.

---

## 2. Go/No-Go checklist (before participant 1)

| # | Must be true before the first real participant | Status | Evidence / note |
|---|---|---|---|
| 1 | Public repo no longer serves the name-bearing log; pilot first names removed from tracked files | **FAIL** | PB-1 |
| 2 | One signed, dated pre-registration consistent with Design A. It fixes the primary outcome, coding unit, rate floor(s), accuracy ruler, per-target floor and the blind-first estimator | **FAIL** | PB-2 |
| 3 | `EVALUATION_FROM_DATE` set to the first participant's start time; pilots moved out of `data/study/` | **FAIL** | PB-3 |
| 4 | Stimulus onset/offset logged against the **media clock**; first-clip excess explained or eliminated | **FAIL** | PB-4 |
| 5 | Order allocation balanced by construction (allocation table); ID format validated | **FAIL** | PB-5 |
| 6 | Full manifest written atomically **before** segmentation and Excel; researcher sees a SAVED/FAILED verdict | **FAIL** | PB-6 |
| 7 | Every calibration, validation and slider event persisted append-only; manual slider disabled; recalibration path records a deviation | **FAIL** | PB-7 |
| 8 | Manifest carries git commit + dirty flag, package versions, model SHA-256, stimulus SHA-256, browser UA, config snapshot; auto-update disabled during collection | **FAIL** | PB-8 |
| 9 | IPC replies matched to requests | **FAIL** | H-5 |
| 10 | Socket disconnect cannot silently leave stimuli unrecorded | **FAIL** | H-6 |
| 11 | A single `inclusion()` implementation used by every tool, with boundary tests | **FAIL** | H-8 |
| 12 | I-DT parameters as run == as written (0.10 vs 0.20 s decided; dispersion in degrees) | **FAIL** | H-9 |
| 13 | Participant ID removed from LLM prompt; Gemini tier/DPA confirmed; consent text matches what is sent | **FAIL / UNVERIFIED** | H-10 |
| 14 | Pre-flight **blocks** on tracker self-check failure (camera, MNN, model) | **FAIL** | H-12 |
| 15 | Per-target validation display timestamps logged | **FAIL** | H-13 |
| 16 | Socket.IO client served locally (works offline) | **FAIL** | M-2 |
| 17 | Server bound to `127.0.0.1` | **FAIL** | M-3 |
| 18 | Screen diagonal default = collection display (15.6″) or field made required | **FAIL** | M-5 |
| 19 | `SESSION_STIMULUS_MODE=all` for every real session | **PASS (launcher)** | `windows/run_session.bat:60`; default is still `clip30` (L-2) |
| 20 | Exactly two clips, 30.000 s, no hard cuts | **PASS** | ffprobe: 750 frames @ 25 fps each; no scene change > 0.3 |
| 21 | perf_mode active, sustained ≥ 28 Hz under a fullscreen browser | **PASS (pilots)** / UNVERIFIED (today) | gates 28.0–30.2 Hz, `perf … ACTIVE` in 9/9 |
| 22 | Display 100 % scaling, fullscreen, single monitor, tracker = browser space | **PASS (pilots)** | dpr 1, 1920×1080 both sides, `mismatch: false` in all 27 validations |
| 23 | 32M model present on the collection machine | **PASS (18 Aug)** / UNVERIFIED (today) | `data-2/tracker_service.log:7090ff` |
| 24 | Collection machine at the audited commit, clean tree, history reconciled after the 24 Sep rewrite | **UNVERIFIED** | §9 |
| 25 | Test suite green **on the collection machine** | **UNVERIFIED** | macOS: 1,143 pass / 2 known fails |
| 26 | Full rehearsal with failure injection done on the collection machine | **FAIL (not done)** | §5 |
| 27 | Coding tool supports Design A (needed before the first coded item, not before P1) | **FAIL** | H-2, H-3 |
| 28 | Design A analysis script frozen (needed before looking at codes) | **FAIL** | H-7 |
| 29 | `claim_check` fixed and F19 amended (before any correspondence figure is cited) | **FAIL** | H-1 |

---

## 3. Findings, ranked by severity

Effort: **S** ≤ 2 h · **M** ½–1 day · **L** ≥ 2 days. **C** = Confirmed (reproduced or read in code) · **S?** = Suspected (plausible, not verified).
Area: A integrity/loss · B measurement · C pre-spec/analysis · D LLM · E robustness · F tests/repro · G docs · 0 repo.

### 3.1 Blockers — fix before participant 1

| ID | Area | Finding | Evidence | Consequence for the data | Proposed fix | Effort | C/S? | Type |
|---|---|---|---|---|---|---|---|---|
| PB-1 | 0 · privacy | The 24 Sep history rewrite did not remove the exposed objects from GitHub. Commit `1ef6dd2a504856aa71da02bb949e0643cd08a080` adds `data/server_[name]_backup.log`. The later commit `999dac8…` says it "contained real participant names". Both are still served. Separately, pilot session labels with first names appear in **7 tracked files at HEAD**. | `curl …/commit/1ef6dd2…` → 200 (web and API); raw blob URL → 200, 192,409 B, which equals the blob size in `_git_backups/…before-cleanup_2026-09-24.bundle`. Repo `visibility: public`, `forks_count: 0`, `pushed_at 2026-09-24T18:05:45Z`. The log contains 13 login lines and 8 label+timestamp pairs; one timestamp matches a publicly committed pseudonymised manifest. `git grep` at HEAD finds the labels in PREREGISTRATION, STATUS, METHODOLOGY_FINDINGS, BRIEF, `rebuild_manifest.py`, `run_tests.py`, `validation_stats.py`. | Ongoing exposure of pilot participants' names. One public pseudonymised record is re-linkable to a name using public material alone. | GitHub Support request to purge unreachable objects and cached views (or delete and re-create the repo; currently no forks). Replace the labels at HEAD. Extend `githooks/pre-push` to block all of `data/**` except an allow-list (it currently omits `data/study`, `data/llm_replay`, `data/coding`, `data/*backup*.log`). Tell the supervisor/DPO per TUM policy. | S + external lead time | C | admin + code |
| PB-2 | C · spec | **No frozen pre-specification, and the documents contradict each other and Design A.** The draft pre-registration is unsigned. Its §1/§4 make `claim_check` and **claim-level** coding with κ the RQ3 carrier; THESIS_PLAN and Methods say fixation census, percent correct, no κ. Rate floor: config 20 Hz flag-only; prereg 20 Hz inclusion + 28 Hz for fixation measures (not in code); THESIS_PLAN "≥25 Hz"; `run_session.bat` "under 25 Hz stop". Accuracy ruler: prereg "measured", code "browser". The 25-sample per-target floor is not implemented. Methods says the clips were "selected to differ in visual crowding … a directional prediction fixed in advance" (`03_methods.tex:148–159`) and also "the two clips were not selected to differ" (`:399–400`). | PREREG §0–§7; `config.py:152`; `metrics_spec.py:270–299`; `THESIS_PLAN.md:182`; `OPEN_QUESTIONS.md` Q29–30, Q35, Q44; `windows/run_session.bat:122`; full list in §6. | Every exclusion threshold and the primary RQ3 estimand would be chosen after the data exist. The study's own rule ("deciding later is indefensible") is violated by default. | One signed, dated document that supersedes the rest. It names the estimand, unit, UNCLEAR handling, blind-first estimator and subset rule, rate floor (and which statistic: mean or median), ruler, per-target floor, stopping rule for N, and clip hypothesis (yes/no). Mirror every number as a config constant, with a test asserting config == document. | M | C | design gap + code |
| PB-3 | A/C | `EVALUATION_FROM_DATE = "2026-08-11T14:00"`, while the config's own comment says "EMPTY means collection has NOT started — that is the current state". All 10 pilot sessions sit in `data/study/` as evaluation data. The test suite **requires** a non-empty boundary, so the stale value keeps tests green. | `config.py:114–132`; `ls data/study` (10 sessions); `run_tests.py:3798–3801`; PREREG §0 | Pilots pooled with evaluation data by every tool that uses `is_evaluation_session`. | Set the boundary (with time) to participant 1's start, in config **and** `run_session.bat`. Retire pilots from `data/study/` with `retire_session.py`. Add a test that no file in `data/study` predates the boundary. | S | C | config + procedure |
| PB-4 | A/B · meas | **Order-dependent stimulus-window excess with no media clock.** The first-presented clip's window exceeds the 30.000 s video by **335–600 ms (mean 486, 9/9 sessions, both clips)**; the second by 62–92 ms (mean 75). The tracker-side trigger spans agree (30.35–30.61 s vs 30.04–30.13 s), so the extra time is real. Onset is stamped server-side after `await play()`, the socket hop and a tracker IPC round-trip. Nothing logs `currentTime`, `requestVideoFrameCallback`, `waiting`/`stalled`, dropped frames or `fullscreenchange`. | `experiment.js:1233–1257`; `app.py:2570–2571, 3889–3899`; grep for those events: none. Table A2 (`timing.py`). Clips: ffprobe 750 frames @ 25 fps = 30.000 s. | If the ~0.4 s is a start-up stall (audio/decoder warm-up: **suspected**, cannot be located from the logs), `video_time_s` for the first-presented clip is late by up to ~0.5 s, about 1.5–2 median fixations (223–432 ms). Keyframes shown to model and coder then come from a later moment, for half of all clip-presentations. Content changes visibly in 2.8–4.9 % of 16×16 blocks per 0.48 s (p90 5.4–9.0 %), concentrated on moving people, where gaze goes. **Irreparable after collection.** | Log media-clock pairs (`requestVideoFrameCallback` `mediaTime` + `expectedDisplayTime`, plus `playing`/`waiting`/`stalled`/`ended` with epoch ms, clock-synced to the server at connect) and derive `video_time_s` from the media clock. Prime the audio/decoder with a silent pre-roll, or strip the audio tracks. Record `getVideoPlaybackQuality()`. Sketch in App. C3. | M | C (existence) / S? (location) | code |
| PB-5 | A/C | **Counterbalancing is deterministic but not balanced.** The order is `random.Random(int(sha256(id))).shuffle(sorted clips)`. N floats, so the realised split is arbitrary and is format- and case-sensitive (`P1` ≠ `P01`). | `app.py:731–741`. The replica reproduces the recorded order of all 9 non-reconstructed pilots. IDs `P01…`: 10/5 at N=15, 13/7 at N=20, 15/15 at N=30. IDs `P1…`: **12/3 at N=15** (exact binomial p = 0.04), 15/5 at N=20. | A clip × position confound. Combined with PB-4, one clip disproportionately carries the first-position artefact; per-clip estimates become unbalanced. Cannot be rebalanced afterwards. | Pre-generated allocation table (permuted blocks of 2 by enrolment number, fixed seed, committed before P1). Reject IDs not in the table; record intended vs realised order. | S | C | code + procedure |
| PB-6 | A | **The manifest is written last, behind the slowest and most fragile step.** The "provisional manifest" contains only `stimulus_log` and `correction`, **not** validations, distance, rate gates or head position. Each stimulus triggers a full read/rewrite of a 34,564-row workbook ending in `os.replace`, with no try/except. If any append raises, the full manifest is never written. The browser redirects to "Study Complete … recorded successfully" after a fixed 20 s, regardless. | `app.py:363–375` (provisional dict), `:190–229` (`_append_to_excel`, `os.replace` at 229), `:523–526` (unguarded call), `:2478–2505` (manifest after it); `experiment.js:1408` (`setTimeout(go, 20000)`); `complete.html:20–24` | The workbook open in Excel on Windows (locks the target, so `os.replace` fails), a full disk, or the server closed during the ~1 min window leaves a session **without its accuracy record**. It then can be neither included nor excluded under the pre-registered rule. The participant and researcher are told it worked. | Build and write the complete manifest atomically (tmp + `fsync` + `os.replace`) **first**. Make the workbook append best-effort (try/except, status recorded), or build the workbook offline. Show SAVED ✓ / FAILED ✗ to the researcher from the server ack only. Sketch in App. C1. | S–M | C (code) / S? (Windows lock: rehearse) | code |
| PB-7 | A/C | **Validation attempts and recalibrations are not durable; repeats are unconstrained and invisible.** After a successful calibration there is no recalibrate control, and the accuracy button disables itself. So after "recalibration recommended" the only route is a page reload. The reload gives a new socket state; the old one has no `stimulus_log`, so it is discarded unfinalised. The attempt counter therefore restarts at 1 and the earlier validations exist only in `server.log`. The tracker instance and temp CSV survive, so the next CSV contains the aborted attempt's samples and `calibrated` stays True. Separately, a **participant-visible gain slider** can override the auto-correction after the canonical `pre_check`. | `experiment.js:403–428, 443–460, 491–501`; `calibration.html:117–141`; `app.py:2533–2544, 2740–2773, 3166–3175`; `tracker_service.py:2175–2200` | The pre-registered "first attempt is canonical; never select" cannot be enforced or even detected. Selection on the outcome becomes possible without trace. A slider moved after `pre_check` means the canonical accuracy no longer describes the correction applied to the stimuli. | An append-only JSONL per Flask session written at every calibration, validation, slider and gate event (independent of the socket). Explicit "Abort session" that calls `end_session`. A "Recalibrate" button that records a deviation and resets the correction and attempt epoch. Slider only in TEST_MODE. | M | C | code |
| PB-8 | F/E | **No per-session provenance, while the launcher auto-updates code.** `START.bat` pulls at every launch. Manifests contain no git commit/dirty flag, package versions, model file/hash, stimulus hashes or browser UA. Requirements are lower bounds only (`pandas>=2.0` already resolved to 3.0.5 here; `gazefollower`, `MNN`, `opencv` unpinned). The collection machine runs **Python 3.12.10** in a `.venv`, against documented 3.11. MNN and GazeFollower versions print "?". `models/base_32M.mnn` (7,653,384 B, sha256 `afbd96…`) **≠** the bundled `base.mnn` (6,245,556 B, `2f96b9…`), the fallback is silent, and `models/README.md` says no download is needed. | `windows/START.bat:63, 111, 156`; grep of `app.py`/`experiment.js` for commit/UA/model hash: 0; `requirements.txt`; `data-2/tracker_service.log:7463–7495`; `shasum` | The instrument can change between participants with no trace. A missing model file silently swaps the network. Sessions cannot be attributed to a version. | Tag a release. Commit a `pip freeze` lockfile. Disable auto-update in collection mode. Write `{git_commit, git_dirty, python, packages, model_path, model_sha256, stimuli_sha256, browser_ua, os_build, config snapshot, EVALUATION_FROM_DATE}` into every manifest. Pre-flight blocks if the model hash ≠ expected. App. C8. | S–M | C | code + procedure |

### 3.2 High

| ID | Area | Finding | Evidence | Consequence | Proposed fix | Effort | C/S? | Type |
|---|---|---|---|---|---|---|---|---|
| H-1 | D/G · interp | **`claim_check.load_gaze` re-applies the correction wrongly.** It reads `gain_x`, `gain_y`, `offset_*`, `centre_*`, but `validation_stats.payload()` never writes `offset_*`/`centre_*`. So every correction collapses to `x·gain_x, y·gain_y` about the screen origin: affine intercepts, the quadratic term and full-affine shear are all lost. **F19's headline is an artefact of this.** | `claim_check.py:780–803`; `validation_stats.py:618–668`. Displacement vs the correction actually applied: **113 px (1.82°)** PILOT_06, **197 px (2.94°)** PILOT_04, **137 px (2.55°)** [named]_P2. Re-scoring F19 (`13_47_11.08`) with claim_check's own `check_all`: as run **16.9 / 28.8 %**, offset (13, 190) px = 2.63° = 3.6× accuracy ("tracker exonerated"); with the true correction **54.2 / 76.3 %**, offset (−20, 41) px = 0.63° = 0.9× accuracy (not exonerated). The workbook's corrected columns match the applied correction exactly (0 diff). | Every `correspondence` block written into a corrected session's manifest is wrong. F19's conclusions ("localises badly", "proves the tracker is not the cause"), THESIS_PLAN's "localises at 16.9–28.8 %", and the motivation for `inverse_check` are unsupported. Repairable post hoc. | Use `validation_stats.from_payload` + `apply_points`, or the session's `gaze_video_nx/ny`. Re-score. Add a dated amendment to F19. Add a test with active affine **and** full-affine payloads. App. C5. | S | C | code |
| H-2 | C | **The coding tool cannot execute Design A.** It has RIGHT/WRONG/UNCLEAR buttons only: **no free-text field on WRONG**. "Blind mode" hides the claim but records neither the coder's own naming nor per-verdict blind/revealed state, so blind-first is not implementable. Codes are keyed by `units[idx].index` of fixations **re-detected at coding time**, with no per-unit content. There is no per-verdict timestamp (`saved_utc` is overwritten). Recordings are chosen manually, with no randomised blocks. The instruction asks "does the model's claim describe **what the participant was actually looking at**", outside the claim boundary. `agreement_kit export` is the dropped `criteria_met` sheet, with LLM labels in the same row. | `coder.html:33–35, 49–58, 91–93, 189, 248, 261–264`; `app.py:915–933, 976–986`; `agreement_kit.py:95–125` | The free-text failure analysis (the stated contribution), the anchoring estimate, block-drift detection and intra-coder re-code are impossible. Any re-derivation of fixations silently re-attaches codes to other fixations. Coders judge a different construct from the one claimed. | Before the first coded item: a unit record `{session, stimulus, fixation_id, t_start, t_end, nx, ny, frame_time, claim_id}` stored with each verdict. Blind-first mode with a mandatory free-text naming **before** reveal. Free text on WRONG. Per-verdict timestamp + revealed flag. Seeded randomisation into blocks. Instruction aligned to the claim boundary (you write it). Pre-specify how blind naming is adjudicated against the claim. | M | C | code + design |
| H-3 | C/D | **Claims are joined to fixations by time proximity, not identity** (±0.35 s window, nearest centre; claim times are copied by the model from 0.1 s labels; fixations are re-detected for coding). | `app.py:840, 887–902, 910–913` | When the model skips or merges an entry, a neighbour's claim is presented for a fixation, and the same claim can be judged twice. This is undetectable afterwards. **Observed so far: 7/7 fixation-mode runs had JSON count = frame count and 0 time mismatches**, so impact to date is zero. | Put a fixation ID in each keyframe label and require it in the JSON. Join on ID. Store the exact fixation list sent to the model and code against that list. | S | C (code) / S? (impact) | code |
| H-4 | D · meas | **Keyframes do not show what the prompt says they show.** The zoomed crop is clamped inside the frame and taken *before* the marker is drawn. For gaze outside the central band it is off-centre and unmarked, yet the prompt says it is "of the region around the gaze point — use it to identify the attended object precisely". The marker radius uses `err_px/2.5`, while the correct scale is 512 / video-rect width (1920): drawn **1.49×** too large, though the prompt says the radius *is* the measured uncertainty. The coder's ring uses a different hard-coded scale (58.2 px/°, 1680 px). The label text covers the top-left of every frame. | `gaze_vision.py:74–83, 160–161, 185–190`; `app.py:1851–1857`; `coder.html:237`. **325 of 1,219 pilot fixations (27 %; 0–59 % per recording)** get an off-centre, unmarked crop. | For about 1 in 4 fixations the model is steered to the crop centre rather than the gaze. Model and coder see different uncertainty circles, so the "same annotated frame" premise is false as implemented. | Pad (letterbox) instead of clamping so the gaze stays centred. Draw the marker on the crop. Derive the radius from `video_rect.w`. Serve the coder the **identical** JPEG the model received. Move the label outside the image. | S | C | code |
| H-5 | A | **IPC replies are not matched to requests.** `_send` pops the next queued reply. The tracker echoes `cmd`, but it is never checked. Timeouts are 2–30 s (telemetry 2 s, `gaze_info` 10 s, ready 15 s). | `gaze_service.py:131–143`; `tracker_service.py:2321` | One late reply desynchronises every later command for the server's lifetime. `calibrate` can "succeed" on a stale reply; `begin_stimulus`/`end_session` can report success before acting. Silent. | Add a sequence id (or check `cmd`) and drain stale replies. On a mismatch, restart the tracker and mark the session. App. C2. | S | C (code) / S? (frequency) | code |
| H-6 | A | **Any socket disconnect finalises immediately, and later stimuli are silently unrecorded.** `end_session` releases the tracker and resets `calibrated`. After a reconnect, `begin_stimulus` refuses, but the browser still receives `recording_started`, and the stimulus is left out of `stimulus_log`. The post-validation lands in a new state with an empty `stimulus_log`, so it is never saved. | `app.py:2533–2544, 2570–2588, 3889–3899`; `tracker_service.py:1648–1656, 2193–2199`; no socket-loss handling in `experiment.js` | A transient drop (sleep, GPU hang, reload) splits a session. The participant keeps watching unrecorded clips. The manifest misstates what was shown. | Add a grace period keyed by Flask session; restore state on reconnect. If recording is not active, stop the run visibly. Record `shown_not_recorded` entries. | M | C (code) / S? (runtime) | code |
| H-7 | C | **No analysis code exists for Design A.** The only resampling code is in `validation_stats`/`correction_audit`. `coding_report.py` summarises per file and pools nothing. It has no CI, no inclusion filter, no `is_evaluation_session`, and cannot exclude never-shown units. A dry run on the pilot codes gives 74.6 % for the F11 file; F11's 88 % came from a manual exclusion. | `grep bootstrap\|resampl`; dry run of `coding_report.py --paste` on a scratch copy of `data-2/coding` | The estimator (pooled ratio vs mean of participant proportions), UNCLEAR handling and CI method are undefined as code. Any later choice is post hoc. | Write and freeze `analysis_designA.py` before P1. Participant-cluster bootstrap with a fixed seed and B, a frozen inclusion function, per-clip and pooled estimates, UNCLEAR rate with CI, blind-first contrast. Dry-run it on pilot codes. App. C6. | M | C | code + design |
| H-8 | B/C | **The inclusion rule as implemented ≠ as declared, and differs between tools.** `verify_metrics` averages `pre_check[0]` and `post[-1]` (first vs **last** attempt) on the **browser** ruler. `app.py` computes per-phase `passes_threshold` on the browser ruler. `claim_check` and the coder ring use **post-only, measured**. The prompt's uncertainty uses the browser figure. The 25-sample and 28 Hz floors are absent. | `verify_metrics.py:360–375`; `app.py:2376–2380, 1744–1751`; `claim_check.py:820–838` | Different stages use different accuracy figures. Rulers differ by −22 %…+15 % per session; no verdict has flipped in pilots, but one will. | One `inclusion(manifest)` function used everywhere. Boundary tests at 2.995 / 3.000 / 3.005°, 59.9 / 60.0 %, 27.9 / 28.0 Hz. | S | C | code |
| H-9 | B/D/G | **I-DT as run ≠ Methods.** `FIXATION_MIN_DURATION_S = 0.20` is imported but never used; the effective minimum is `max(0.10, 3/rate)` = **0.10 s at 30 Hz** (17 of 18 pilot recordings record `idt_min_duration_s = 0.1`; one records 0.102). Methods says 0.20 s. Dispersion is 0.05 of normalised width **plus** height: 1.65° if purely horizontal, 0.93° if purely vertical, and the manifest reports 1.65° only. It is applied after the per-session correction (gain changes effective noise). One failed sample splits a fixation. | `fixations.py:21, 139–148, 159–165, 179–180`; `config.py:197–198`; manifests `events`; `03_methods.tex:238–245` | The census unit differs from the specified one. Counts and durations depend on correction choice and detection rate. | Decide 0.10 vs 0.20 s now and pass it explicitly. Define dispersion in degrees (isotropic). Record both in the manifest. | S | C | code + text |
| H-10 | D · privacy | **The participant ID is in every Gemini prompt.** Pilot IDs containing first names were sent. The consent text mentions only "(pseudonymous) gaze position" and "an AI analysis service". The free vs paid tier (training use, human review) is unknown. Replay payloads keep full base64 frames. | `app.py:1843–1845, 2031–2075`; `llmlogs.py`: ID present in all 16 logged evaluation prompts; `index.html:125–132`; OPEN_QUESTIONS Q10 | Identifiers go to a third party beyond the consent, and the prompt varies per participant in an uncontrolled way. | Remove the ID from the prompt. Confirm paid tier / DPA in writing. Align the consent text (you write it). Set a retention rule for `llm_replay/`. | S + admin | C / unverified (tier) | code + admin |
| H-11 | D | **LLM call logging and failure handling are incomplete.** The log is written only after a successful parse. No `modelVersion`, usage, latency or safety data is logged. No retry. HTTP 400 silently falls back to the model's default thinking config. Empty, blocked or `MAX_TOKENS` replies are still persisted and **overwrite** a previous good `llm` block. `_persist_llm_result` rewrites the manifest non-atomically and without `default=`. | `app.py:1489–1585` (log at 1574; 400 fallback at 1552–1555), `:2094–2123`, `:3684–3686` | Failures leave no trace. Model-version drift behind a stable name cannot be detected. One bad call can destroy the claims the coder uses. A non-serialisable value would truncate the manifest (the pattern that already cost one session). | Log every attempt. Write results to an append-only per-run file, never into the manifest. Retry with backoff. Refuse to persist non-STOP results. Record the served `modelVersion`. | S–M | C (code) / S? (truncation) | code |
| H-12 | F | **Tests give false assurance where it matters.** Test [6] "self-check runs" asserts only `"report" in report`; it **passed here with MNN, gazefollower and camera all FAIL and the model NOT FOUND**. `check_before_participant.bat` ignores the self-check exit code. The e2e fixture has no `gain_correction` and a top-level `screen` key that production never writes, so it cannot catch H-1 or the default-geometry fallback in `_persist_llm_result`. About 54 % of 957 `check()` sites match source text. `gaze_vision`, `gaze_service`, `coding_report`, `agreement_kit`, `quality_report` and `api_coding_units` are never executed. | `run_tests.py:219–220, 4865–4888`; `windows/check_before_participant.bat:47`; AST count of `check()` sites | The suite is green on a machine that cannot record, and the claim_check and geometry bugs pass. | Assert `report["ok"]`. Pre-flight blocks on self-check failure. Fixtures built by the production writers. Execute (not grep) keyframes, claim join and correction on a synthetic session with an active correction. | M | C | code |
| H-13 | B · design | **Validation does not cover the stimulus condition, and per-sample validation error cannot be reconstructed.** F29's luminance question is untested: dark dot screen vs bright video. The validation payload stores per-target medians but **no per-target display timestamps**. | `experiment.js:951–960`; METHODOLOGY_FINDINGS F29 | The accuracy that gates inclusion and sets every tolerance is measured in a different visual condition. The F22 signal-detection rebuild needs per-sample error distributions that will not exist. **Irreversible.** | Log per-target onset/offset epoch times now. Decide now whether to add grid-B targets over a frozen stimulus frame. | S (log) / design | C | code + design gap |

### 3.3 Medium

| ID | Area | Finding | Evidence | Consequence | Proposed fix | Effort | C/S? | Type |
|---|---|---|---|---|---|---|---|---|
| M-1 | A | The rate-gate override and the gating measurement are overwritten by the post-video re-measurement. History entries are copies taken before any override. | `app.py:3388, 3402–3406, 3480–3500`; PILOT_06 top-level `rate_gate` = 28.0 Hz = post-video (pre-video 29.3) | The record of a protocol deviation disappears from the manifest. | Append-only list of gate decisions; store the pre-video gate separately. | S | C | code |
| M-2 | E | Socket.IO client and fonts are loaded from CDNs. | `templates/base.html:13–15, 22` | Offline or captive portal: `io` is undefined and no session can run. Visits are also disclosed to Cloudflare/Google. | Vendor `socket.io.min.js` and the fonts into `static/`. | S | C | code |
| M-3 | E · privacy | The server listens on `0.0.0.0`, the researcher APIs are unauthenticated, and CORS is `*`. | `app.py:137, 791–793, 3996–4002` | Anyone on the network can read gaze data, trigger paid LLM calls with the stored key, and emit events to the shared tracker. | `host="127.0.0.1"`; drop the wildcard CORS. | S | C | code |
| M-4 | A · privacy | GazeFollower writes every participant's raw gaze to `~/GazeFollower/tmp/em_my_session_*.csv` (flushed ~1 s). These files are never deleted and never used for recovery. Withdrawal-deletion (Methods `:39–40`) cannot reach them, nor the workbook rows, telemetry, LLM logs/replays, coding files or the `data-2` copy. | upstream `GazeFollower.py:469–515`; `sample_patch.py:114–153`; this Mac: 49 files / 5.5 MB; no repo reference | Retention outside the DMP. A tracker crash loses the session at app level although the samples are on disk. | Recovery tool + delete after verified finalisation. A deletion script covering every copy. | S–M | C | code + procedure |
| M-5 | B | The screen diagonal defaults to 13.3″ when not typed; the collection display is 15.6″ (typed in every pilot). | `config.py:140–141`; `app.py:606–613`; `index.html:61–68` | One forgotten field makes degrees ~15 % **too small** (pass-inflating). | Set `SCREEN_DIAG_INCHES=15.6` in `run_session.bat`, or make the field required. | S | C | config |
| M-6 | D | The prompt is not frozen or versioned. It contains session-varying text (ID, sample count, browser-ruler uncertainty, free-text rubric), an unneeded "evaluate the gaze behaviour" task, and the model's own scene description injected as "ground truth". Generation configs varied over the pilots (3000 / 4000 / 8000 tokens, with and without `thinkingConfig`). | `app.py:1820–2027`; `llmlogs.py` config histogram | Instrument drift; runs cannot be repeated. | Template file with a version id and SHA-256 recorded per call. Remove the evaluation-of-behaviour task. | S | C | code |
| M-7 | B/G | The sampling-rate statistic is mislabelled: it is 1 / median gap. | `app.py:441–443`. Median-based 29.8–31.2 Hz vs mean `(n−1)/span` **29.0–30.1 Hz** | The reported rate exceeds the camera's 30 fps rating. A 28 Hz floor judged on the median is ~1 Hz lenient. | Report the mean rate and the median interval; define floors on the mean. | S | C | code + text |
| M-8 | B · meas | Undocumented timing offsets: upstream `HeuristicFilter(look_ahead=3)` returns the smoothed value of sample *n−3* under sample *n*'s timestamp. Timestamps are taken after `cap.read()`, not at exposure. | upstream `filter/HeuristicFilter.py:18–92`, `camera/WebCamCamera.py:62–64`; grep of the repo: no mention | `filtered_*` lags its timestamp by ~100 ms at 30 Hz (200 ms at 15 Hz), plus camera latency. This adds to PB-4. | Document it. Either shift `filtered_*` by 3 samples or use the unfiltered columns for timing. | S | C | code + text |
| M-9 | A | One workbook mixes dev and evaluation sessions, is fully rewritten on every append, and is the source for the coder and LLM. | 34,564 rows, 27 sessions, 7 stimulus names; `app.py:190–229, 779–788` | Growing finalisation time (feeds PB-6), Windows lock risk, dev sessions offered for coding. | Per-session data files plus an offline build. The coder lists only included evaluation sessions. | M | C | code |
| M-10 | E | Camera index is fixed at 0, and the self-check opens it separately. | `camera_patch.py:228–232`; `tracker_service.py:648` | A virtual camera (Teams, OBS, Studio Effects) at index 0 means the wrong device. | `GF_CAMERA_INDEX` setting; log the device name. | S | C / S? | code |
| M-11 | B | No `fullscreenchange` handling: `video_rect` is measured once. The offset heuristic is wrong in fullscreen: it gives (0, −11) with `fullscreen: true`. | `experiment.js:32–39, 1298–1316`; pilot geometry: inner 1920×1080, outer 1898×1058 | Esc during a clip silently corrupts normalisation for the rest of that clip. The −11 px largely cancels between validation and video (≤ 3 px residual). | Listener that records the event and pauses or re-measures. Use (0, 0) when `document.fullscreenElement`. | S | C | code |
| M-12 | A | Silent sample loss on write error (bare `except: pass`). | `sample_patch.py:140–155` | A full disk or I/O error drops samples while the rate gate still looks healthy. | Count write failures, expose them in telemetry and the gate, abort if > 0. | S | C | code |
| M-13 | C | Circularity details: the model's scene description is fed back as "ground truth", so Methods' "hallucination control" is never evaluated. `crowding_analysis` uses the model's own boxes as the moderator. | `app.py:1874–1880`; `claim_check.py:376–470` | The "hallucination control" claim is unsupported, and the crowding result is partly self-referential. | Evaluate step-1 vs step-2 consistency, or drop the claim. Derive crowding independently of the model. | S | C | code + text |
| M-14 | G · interp | F11 mixes denominators. The coding file has 44 correct / 15 wrong / 12 unclear (71). "88 %" = 44/50 after excluding 9 never-shown (all coded wrong). "Unclear 16.9 %" = 12/71, i.e. **including** those 9; on the same basis it is **12/62 = 19.4 %**. Repeated in THESIS_PLAN and OPEN_QUESTIONS. | `data-2/coding/13_47_…__Jan.json`; F11; `THESIS_PLAN.md:87–88, 222–223` | A wrong planning figure, and a non-reproducible headline (single dev session, manual exclusion). | Amend F11; report everything on one basis. | S | C | text |
| M-15 | G · interp | The prereg §2.2–2.3 example is on the **raw** basis, while the criterion uses the corrected basis. [named]_P2 post raw: mean 205.2 px = **3.52°**, median 140.4 px = **2.41°**, max **652 px**. Corrected (the inclusion basis): 3.19° / 1.68° (browser), 3.47° / 1.83° (measured), max 634 px. `metrics_spec` says 649 px. | `valid.py` / inverse of the stored affine; `metrics_spec.py:143`; PREREG §2.2–2.3 | The leverage argument still holds on the corrected basis; the numbers carry the wrong label. | Restate on the corrected basis, or label "uncorrected". | S | C | text |
| M-16 | G · interp | Planning numbers rest on single dev sessions. Fixation rate "2.4/s" vs the pilot mean **2.13/s** (64 per clip, range 47–81): about 1,920 items at N=15, not ~2,200. THESIS_PLAN's RQ1 "2.13° corrected, 31 Hz, 0 % loss, +0.51° drift" comes from dev session `20_48_10.08`: superseded protocol (a corrected repeat `pre` on grid A), `_testclip_30s`, post-only, browser ruler, ruler source unrecorded. | manifests `events`; `20_48_10.08` manifest | Coding-time and precision budgets are optimistic, and RQ1 is "answered" by a non-protocol session. | Recompute from evaluation data only. | S | C | text |
| M-17 | B | GazeFollower's in-window calibration loop (repeat until the participant accepts) and its calibration error are never recorded. `calibrated` is set unconditionally after `gf.calibrate()`. | upstream `GazeFollower.py:199–235`; `tracker_service.py:817–818` | Repeated calibrations are invisible, and there is no calibration-fit metric. | Count loop iterations; capture `mean_euclidean_error`. | S–M | C | code |
| M-18 | privacy/procedure | Personal data sits in several divergent copies: `data/`, `data-2/` (two `participants.xlsx` with 17 vs 34 rows), the history bundle (which contains the names log), and `~/GazeFollower/tmp`. Session IDs are free text (first names typed in pilots). | `ls`; row counts | Deletion on withdrawal and retention control are not achievable. | One canonical store per DMP; enforce the ID format; scripted deletion. | S | C | procedure |
| M-19 | D | Cost and runtime are unmeasured: usage and latency are not logged. Estimate per clip: scene call 64 images, evaluation call 128 images, about 50k input + ~8k output tokens at 258 tokens/image, so N = 30 is roughly 3 M input tokens. | `llmlogs.py` (no usage keys) | The budget and timeout margin are unknown. | Log `usageMetadata`; price one run. | S | unverified | code |

### 3.4 Low

| ID | Area | Finding | Evidence | Fix | C/S? |
|---|---|---|---|---|---|
| L-1 | G | Stale constants and comments: `VALIDATION_TARGETS_PRE/POST=7` vs grid A = 13; `experiment.js:8–10` "pre 5 / post 3"; `calibration.html:87–91, 157–161` ("measured before calibration", "5/3 targets"); CLAUDE.md `LLM_MAX_FRAMES (60)`, κ design (`:399–407`), "what did they look at" (`:17`); `metrics_spec.py:148` candidates exclude full-affine; `index.html` test option "All 5 videos". | as cited | Sweep. These matter because CLAUDE.md steers future AI edits. | C |
| L-2 | A | Default `SESSION_STIMULUS_MODE=clip30` presents `_testclip_30s.mp4`, not the study clips, whenever the server is started other than via `run_session.bat`. | `config.py:395–396`; `app.py:705–728` | Refuse a non-test session unless mode == `all`. | C |
| L-3 | G | The `[named]_P1` manifest is reconstructed: `stimulus_mode: clip30` and its order are inconsistent with the hash, and there is no per-target data. Reconstructed fields reflect rebuild-time config. | manifest; `cb.py` | Mark reconstructed fields; never use them as data. | C |
| L-4 | B | `gaze_off_video_pct` counts `status=0` rows as off-video (the sentinel survives correction). | `app.py:1389–1395` | Filter on `status`. Immaterial in pilots: only 18 failed rows, none during stimuli. | C |
| L-5 | E/G | Python 3.12.10 on the collection machine vs documented 3.11; the Methods stack table lacks Python, MNN and GazeFollower versions. | `data-2/tracker_service.log:7463` | Record the real versions. | C |
| L-6 | G | The correction is chosen per session by LOO among four candidates. Methods calls it "fixed-form … not a per-session tuning parameter". PILOT_06 has not been re-derived. | `validation_stats.py:859ff`; `03_methods.tex:227–236` | Text. | C |
| L-7 | B | Validation precision (s2s RMS) is computed on polled preview samples that can include duplicates: 30 Hz poll vs ~30 Hz production, with no sample id. | `app.py:3549–3574`; `experiment.js:883–890` | Use the CSV samples (needs H-13 timestamps). | S? |
| L-8 | E | The room field is free text ("HFP", "Office", "office") despite the "fixed vocabulary" comment. | manifests | Use a select list. | C |
| L-9 | A | Public pilot pseudonyms are `P01–P09` (`data/manifests_anonymised`). Reusing `P01…` for real participants invites confusion. | `git ls-files data` | Use a distinct prefix. | C |
| L-10 | A | `_finalize_session` idempotency is check-then-set without a lock. | `app.py:2327–2329` | Use a lock. | S? |

### 3.5 Classification

- **Code-fixable:** PB-3–PB-8, H-1, H-3–H-6, H-8, H-9, H-11, H-12, and all of M-1–M-19 except M-18 (procedure).
- **Design gaps that no code can fix:** PB-2 (the decisions themselves), H-13 (validation condition), PB-5 (allocation policy with floating N), H-2 (how blind naming is adjudicated against a claim), RQ2 under Design A (§8, F9/F22), and the shared-frame circularity (inherent; state it).
- **Measurement problems:** PB-4, PB-6, PB-7, H-4, H-5, H-6, H-9, H-13, M-1, M-7, M-8, M-11, M-12, M-17.
- **Interpretation problems:** H-1 (F19), M-14 (F11), M-15, M-16, M-13, and the Methods text items in §6.

---

## 4. Data-loss and irreversibility register

These cannot be repaired after a participant is recorded. Each needs a decision or a fix before P1.

| # | Not recoverable afterwards | Why | Mitigation before P1 |
|---|---|---|---|
| R1 | Mapping of gaze timestamps to the **media clock** (true `video_time_s`) | Only wall-clock windows are stored; the first-clip excess cannot be located | PB-4: media-clock log |
| R2 | Order balance across clips | Fixed by IDs at login | PB-5: allocation table |
| R3 | Code, package, model and stimulus identity per session | Not written; auto-update can change it | PB-8 |
| R4 | Every calibration and validation attempt, recalibration and slider change | Discarded with the socket state | PB-7 |
| R5 | Accuracy record when finalisation fails | Full manifest written last | PB-6 |
| R6 | Rate-gate overrides and the gate value that actually gated the videos | Overwritten | M-1 |
| R7 | Per-target validation display windows, hence per-sample validation error | Not logged | H-13 |
| R8 | Accuracy measured under stimulus-like luminance | Not measured | H-13 (design decision) |
| R9 | Whether each stimulus was shown but not recorded | Omitted from `stimulus_log` | H-6 |
| R10 | Browser and display state during clips (UA, zoom, fullscreen exits, dropped frames) | Not logged | M-11, PB-4 |
| R11 | Calibration loop count and calibration error | Not logged | M-17 |
| R12 | Sample-write failures | Swallowed | M-12 |
| R13 | Consent text version and protocol version per session | Not logged (Methods claims a protocol string is) | PB-8 |
| R14 | Correct screen diagonal when the field is left empty | Default 13.3″ | M-5 |

**Repairable post hoc** (lower priority before P1): H-1 claim_check, H-3 claim join (if the frame list is stored), H-7 analysis code, H-9 I-DT (raw data kept), M-7/M-8 statistics, M-13–M-16 text, H-2 coding tool (before coding starts).

---

## 5. Rehearsal protocol: one day on the collection machine, before participant 1

Use participant IDs `REHEARSAL_01…`. Set `EVALUATION_FROM_DATE` to a future date for the rehearsal, so nothing lands in `data/study/`. For every step, record what you did, what happened, and the manifest/CSV/log evidence.

**0. Freeze and fingerprint**
1. `git fetch && git status && git rev-parse HEAD`. Expect a clean tree at the release tag. The 24 Sep history rewrite means an old checkout fails `--ff-only`; reconcile it first.
2. `.venv\Scripts\python -m pip freeze > freeze_<date>.txt`. `certutil -hashfile models\base_32M.mnn SHA256` must equal `afbd9688…e04a`. `certutil -hashfile` both stimuli: expect `e710455d…` (`Stimuli_1_30s`) and `a395e60a…` (`Stimuli_5_30s`), first 16 hex digits.
3. Windows: display scaling 100 %, single monitor, Focus Assist on, Windows Update paused, OneDrive/AV exclusions for the project and `%USERPROFILE%\GazeFollower`, power plan, ≥ 10 GB free, correct time zone. Close Excel.
4. `windows\check_before_participant.bat`. Then run `tracker_service.py --check` by hand and read **every** line: MNN ok, gazefollower ok, camera ok, "32M base model".

**1. Happy path (two complete sessions, confederate)**
5. Run two sessions back-to-back without restarting the server. Expect each CSV to contain only its own samples. The second session must calibrate afresh, and `rate_history` should show ≥ 28 Hz.
6. Time the finalisation, from post-validation "Continue" to the "Session finalized" line in `server.log`. Check that the manifest has validations, distance (`source: iris`), `stimulus_order`, `rate_history` and the PB-8 provenance block (once added).
7. If PB-4 logging is added: compare media-clock onset with `t_start_ns` for clip 1 and clip 2, and locate the ~0.4 s. If it is not added: film the screen and the console with a phone to see when clip 1's first frame appears relative to the "Playback started" log line.

**2. Failure injection (one throwaway session each; write down expected vs observed)**

| # | Inject | Expected (after fixes) | Pass if |
|---|---|---|---|
| F1 | Unplug the camera during the rate gate, and again during clip 1 | Visible error; session marked; no "complete ✓" | The manifest states the failure; CSV samples stop with `status`/gap evidence |
| F2 | F5 reload after `pre_check`, before videos | Attempt 2 recorded; first attempt still present | Both attempts are in the append-only log and manifest |
| F3 | F5 reload during clip 1 | Session aborted or resumed explicitly | No unrecorded clip is shown silently; `shown_not_recorded` is logged |
| F4 | Kill `python … tracker_service.py` in Task Manager during clip 2 | Visible error; recovery from `~\GazeFollower\tmp` | The data are recoverable and the session is marked |
| F5 | Close the server console 10 s after "Study Complete" appears | Full manifest already on disk | Manifest has validations (PB-6) |
| F6 | Open `gazefollower_data.xlsx` in Excel during finalisation | Manifest written; workbook failure recorded | No lost manifest |
| F7 | Data directory on a nearly full drive (small VHD or USB) | Write failure detected | Failure counts > 0 and the session is aborted (M-12) |
| F8 | Disconnect the network, then load the login page | UI works offline | Calibration page runs (M-2) |
| F9 | Press Esc during clip 1 | Event recorded; pause or re-measure | `fullscreenchange` in the record (M-11) |
| F10 | Alt-Tab away for 60 s during clip 2 | No disconnect, or a graceful one | Session intact (H-6) |
| F11 | CPU stress (e.g. a video encode) to push the gate below the floor, then override | Override persisted | `rate_gate` decisions list shows the override (M-1) |
| F12 | LLM: invalid key, network off, then `GEMINI_MODEL=nonexistent` | Each attempt logged; the manifest's claims are not overwritten | Failure logs exist (H-11) |
| F13 | Leave the screen diagonal empty | Correct 15.6″ used, or the form refuses | `diag_assumed` false / 15.6 |
| F14 | Log in with `p01` and `P01` | Rejected by format, or mapped consistently | Same allocation (PB-5) |
| F15 | Set display scaling to 125 % | Mismatch flagged and **blocks** | Researcher is stopped before the videos; revert afterwards |

**3. Close out:** retire every rehearsal session. Set `EVALUATION_FROM_DATE` to participant 1's actual start time. Re-run the pre-flight. Tag the release. Write the rehearsal log into METHODOLOGY_FINDINGS.

---

## 6. Discrepancy list (code ↔ config ↔ plans ↔ LaTeX)

| # | Topic | Code / config | THESIS_PLAN / OPEN_QUESTIONS | PREREG (draft) | Methods (`03_methods.tex`) | Others |
|---|---|---|---|---|---|---|
| 1 | Primary RQ3 outcome and unit | Coder: fixation, index-keyed. `claim_check` correspondence written into manifests as "THE RQ3 HEADLINE" | Fixation census, % correct (plan). OQ Q29–30: 20 per participant, mechanical selection | Claim-level, κ(human, claim_check); `claim_check` carries RQ3 | Fixation census, % correct; plus a strict/lenient correspondence pair against "region vocabulary" baselines (`:415–422`) | `metrics_spec.py:226–239` κ "missing, remaining RQ3 validity gap" |
| 2 | κ | `coding_report.py` and `agreement_kit.py` compute κ | Dropped | Reported | `:33–34` "chance-corrected baseline"; `:434–435` | CLAUDE.md `:399–407` |
| 3 | Rate floor | 20 Hz flag (`config.py:152`); gate verdict 20 | ≥ 25 Hz exclude (plan `:182`); OQ Q44 | 20 inclusion + 28 for fixation measures | 20 Hz flags only (`:78–80`) | `run_session.bat:122` "under 25 Hz stop" |
| 4 | Accuracy ruler | Browser 60 cm (`verify_metrics`, `app`, prompt); measured (`claim_check`, coder) | — | Measured | "session's own viewing distance" (`:250–251`) | `metrics_spec` "measured authoritative" |
| 5 | Inclusion figure | `pre_check[0]` & `post[-1]` (verify_metrics); per phase (app); post only (claim_check) | — | Mean of pre_check & post, grid B | Mean of the two (`:206`) | — |
| 6 | Per-target floor | None (≥ 3 samples counts a target) | — | 25 samples | — | — |
| 7 | Grid sizes | A = 13, B = 7 (JS); `config.py:410–411` = 7/7 | — | — | "Each grid has seven targets" (`:199–201`) | `experiment.js:8–10` "5 / 3"; `calibration.html:157–161` |
| 8 | Rate-gate placement | After calibration; gates the **videos**; override allowed | "pre-session gate" | — | Before calibration; "recording is blocked" (`:177–184`) | `config.py:170–176` and `calibration.html:87–91` say "before calibration" |
| 9 | I-DT minimum duration | 0.10 s at 30 Hz | — | — | 0.20 s (`:239`) | `config.py:197` 0.20 (unused) |
| 10 | Fixation volume | Pilot mean 2.13/s, 64 per clip | 2.4/s, ~72 per clip, ~2,200 at N=15 | — | ~140 per participant (`:65–67, 349–352`) | — |
| 11 | Clips | Two 30 s clips | Crowded/sparse sentence "removed (F25)" | — | "Selected to differ in crowding … directional prediction" (`:148–159`) vs "not selected to differ" (`:399–400`) | F16 vs F25 |
| 12 | Regions | None | Dropped | Dropped | "Four region categories" (`:84–91, 356–360`); `\ref{sub:rubric}` points to a removed label (`:288`) | `metrics_spec.py:293` "four rubric regions" |
| 13 | Gain correction | Per-session LOO choice among none / affine / quadratic-vertical / full-affine | — | — | "Fixed-form … applied identically … not a per-session tuning parameter" (`:227–236`) | `metrics_spec.py:148` lists 3 candidates |
| 14 | Versions | Lower bounds only; Python 3.12.10 on the collection machine | — | — | "Versions are pinned" (`:118–120`) | CLAUDE.md / `environment.yml`: Python 3.11 |
| 15 | Camera | `GF_CAMERA_FIX=1`: 640×480 requested, buffer 1 (`run_session.bat:48`) | — | — | "capture runs at the camera's native resolution" (`:142–143`) | CLAUDE.md "opt-in" |
| 16 | Coding verdicts published | `.gitignore` excludes `data/coding/` | — | — | `:47–49` excluded vs `:445–447` "published" | — |
| 17 | Images in the audit trail | `llm_logs` hold hashes; `llm_replay` holds full base64 frames | — | — | "SHA-256 references rather than pixels" (`:53–55, 326–328`) | — |
| 18 | Protocol string in manifest | Absent | — | — | "written into every session manifest … byte-identity machine-checked" (`:333–338`) | — |
| 19 | Model id and pin history | Model id only in the `llm` block after feedback; no history | Q39 open | — | "recorded in every manifest" (`:313–314`) | — |
| 20 | Rooms | `app.py:615–620`: "DIFFERENT ROOMS … the independent variable RQ1 asks about" | — | — | "same machine in the same room" (`:95–96`) | Manifests: "HFP", "Office", "office" |
| 21 | Counterbalancing | Hash shuffle; balance not guaranteed | Q26 | — | "counterbalanced" (`:166–168`) | PB-5 |
| 22 | Blinding | Coder "blind mode" hides the claim; the verdict is about the claim | Plan: verification + blind-first subset; OQ Q35: "coders must not see the label before coding" | — | Verification + blind-first (`:374–381`) | H-2 |
| 23 | Stimulus mode default | `clip30` | Q25 | — | Two clips | `run_session.bat` sets `all` |
| 24 | Evaluation boundary | `2026-08-11T14:00`; the config comment says it should be empty | — | Reset to the signing date | Describes routing (`:19–24`) | `run_tests.py:3798` requires non-empty |
| 25 | F11 unclear rate | 12/71 = 16.9 % (tool) | "16.9 %" | — | — | Same basis as the 88 %: 19.4 % |
| 26 | F19 | Re-scored 54.2 / 76.3 % | "localises at 16.9–28.8 %" | — | — | H-1 |
| 27 | Sampling rate | Median: 29.8–31.2 Hz; mean: 29.0–30.1 Hz | "31 Hz" | "30.3–31.2 Hz" | "median inter-sample interval" (`:267`) | M-7 |
| 28 | [named]_P2 example | Corrected: 3.19 / 1.68° (browser), max 634 px | — | 3.52 / 2.41°, 652 px (raw) | — | `metrics_spec` 649 px |
| 29 | Granularity rule | No signal-detection code | F9 broken (F22) | Open | "350 px … four region categories" (`:84–91`); uses 60 cm (measured ~64 cm gives 372 px) | `metrics_spec.py:17–27` |
| 30 | Claim boundary | Coder: "what the participant was actually looking at" (`coder.html:33–35, 261–263`); prompt "<what the participant looked at>" (`app.py:1960–1961`), "marks where the participant was looking" (`:1851–1852`) | Plan states the boundary | — | Methods respects it (`:424–428`) | CLAUDE.md `:17`; F19 text |

---

## 7. What you should not worry about (looks odd, is fine)

- **Timestamp resolution.** Windows timestamps have 100 ns granularity: 3,556 distinct sub-ms values in PILOT_07, no 15.6 ms quantisation.
- **"31 Hz" above a 30 fps camera.** This is the reciprocal of the median of a right-skewed gap distribution (p95 39–49 ms). The mean is 29.0–30.1 Hz. Relabel it (M-7); nothing is broken.
- **`gaze_samples_pct`.** I recomputed it from the raw CSVs for all 18 recordings and it matches exactly. The nominal-denominator fix holds.
- **Validation arithmetic and both rulers.** Reproduced from raw targets to ≤ 0.3 px and ≤ 0.01°. The server's `atan(e/d)` and the browser's `2·atan(e/2d)` differ by < 0.1 % at 2°.
- **The correction applied to the recorded data.** The workbook's corrected columns equal the stored polynomial exactly (0 difference on three sessions). Only `claim_check`'s re-implementation is wrong.
- **Grid A/B design.** Disjoint, with eccentricity matched as the comments claim: mean |x−50| 21.2 vs 20.7, |y−50| 23.4 vs 21.7.
- **Drift comparability.** Every evaluation-protocol session has 7/7 grid-B targets pre/post on the raw basis.
- **Keyframe cap.** 200 frames vs 47–81 fixations per clip, so nothing is dropped. In fixations mode the JSON entries equalled the frames, with 0 timestamp mismatches in 7/7 runs.
- **Detection failures.** Essentially absent in the pilots (18 `status=0` rows in one session, none during stimuli). The fixation splitting (H-9) and off-video conflation (L-4) are latent only.
- **DPI and multi-monitor.** All pilot validations were fullscreen at dpr 1, with tracker = browser = 1920×1080 and no mismatch.
- **The −11 px fullscreen offset.** Applied equally to validation and video normalisation; ≤ 3 px residual.
- **Stimuli.** Exactly 30.000 s, 25 fps, no hard cuts, cut provenance recorded.
- **`run_tests.py` and `data/camera_geometry.json`.** It writes a fake calibration only if none exists and deletes it afterwards. An existing calibration is never overwritten.
- **`__pycache__` not cleared.** CPython invalidates pyc files on source mtime + size, so stale bytecode is very unlikely.
- **Secrets.** No API keys or `.secret_key` anywhere in current or old history. The wheels are gone from HEAD (still reachable via old SHAs: licence issue only).
- **Python 3.12 on the collection machine.** It works with mediapipe 0.10.21. Document it (L-5); don't rebuild the environment mid-study.

---

## 8. Status of the known issues you listed

| Issue | Status at `c921a06` | Evidence |
|---|---|---|
| F9/F22 two-students example | **Open.** No signal-detection code. Methods still states the 350 px / four-region rule. **New:** Design A has no candidate regions, so the rebuild has no input pairs. The only measured ambiguity is the coder's UNCLEAR. Analysis-time design gap, but per-target validation timestamps (H-13) must be logged now if the rebuild is to use empirical per-sample error. | grep; `03_methods.tex:84–91` |
| EcoQoS 15 vs 30 Hz | **Fixed.** `perf_mode` ACTIVE in 9/9 sessions; gates 28.0–30.2 Hz. Headroom is thin: 31.0 ms work in a 33.6 ms interval (92 % duty, PILOT_07). | manifests `rate_gate` |
| `gaze_samples_pct` circularity | **Fixed**, verified by recomputation. The `min(100, …)` cap hides > 100 % (harmless). | `timing.py` |
| Pre/post drift comparability | **Fixed** (grid B both ends, raw basis). Drift spans −1.42…+2.82°. | `valid.py` |
| Camera settings no-op | **Worked around** by `GF_CAMERA_FIX=1` in `run_session.bat`; the log confirms 640×480 delivered. Methods wording is wrong (#15). | `data-2/tracker_service.log:7505` |
| Calibration never persisted | **Handled correctly** (nothing persisted; `~/GazeFollower/calibration` is empty here). **New:** upstream default arguments make `SVRCalibration` and `HeuristicFilter` process-wide singletons. Combined with aborted attempts (PB-7) this lets state carry across sessions. | upstream `GazeFollower.py:33–38` |
| Synchronous capture callback | **Unchanged by design.** Mitigated by perf_mode. | — |
| MNN hardcoded to CPU | **Unchanged and recorded** (`mnn_runtime: CPU (GazeFollower default)` in the manifest). Fine as long as it does not change mid-study. | PILOT_07 |
| Validation shear | **Instrumented** (13-target grid A, full-affine candidate, F38 fix). PILOT_06 not re-derived. Whether full-affine generalises is unproven (PILOT_07 chose `none`). | STATUS; manifests |
| Degree ruler (60 cm vs iris) | **Unresolved in code** (H-8, #4). Iris in PILOT_02–07; inter-ocular in PILOT_01 and [named]_P2; **source missing** for PILOT_00. | `valid.py` |

---

## 9. Repository state (§0)

- **Remote:** `https://github.com/JanAulichTum/gazefollowerthesis` (public). `main` = `c921a06…`; local HEAD is identical. A fresh clone into the scratchpad was used for tests.
- **Working tree:** clean. 135 tracked files. 26 ignored entries: `.secret_key`, both wheels (33.3 MB mediapipe-manylinux, 5.8 MB gazefollower), `models/base_32M.mnn`, all of `data/*` except `manifests_anonymised/`, `shared/`, `models.json`, plus `_to_delete/` (9 transfer archives, 1.4 MB).
- **History:** rewritten on 2026-09-24 to 12 commits. The tree differs from the pre-rewrite HEAD only in `.gitignore` and the two wheels. The pre-rewrite history (112 commits) is in `../_git_backups/…bundle` and **still on GitHub by SHA** (PB-1).
- **Large files:** none in the current history (largest blob is `run_tests.py`, 299 KB). The old SHAs still expose the wheels, including CC BY-NC-SA weights.
- **Secrets:** none found (`AIza…`, hex-64, assignment patterns) in current or old history.
- **`data/` in git:** `manifests_anonymised/` (13) and `shared/` (13 manifests + 12 claim files). Two pseudonym systems carry identical timestamps, so they are trivially linkable to each other. Raw recordings: none.
- **Collection machine:** unknown HEAD. If it still has the pre-rewrite history, `START.bat`'s `--ff-only` pull fails at every launch and the machine runs whatever it has.

---

## 10. What I could not verify here

- Nothing was run on the **Windows collection machine**: EcoQoS behaviour today, DirectShow, Chrome on Windows (the first-clip stall), Excel file locking, disk-full behaviour, the current HEAD/venv/model.
- **No live session:** camera access was not granted to this process (self-check `camera: FAIL`), and GazeFollower/MNN do not import here without the tracker's preload.
- **No Gemini calls:** model availability, served `modelVersion`, alias behaviour of `gemini-3.5-flash`, pricing, rate limits, and whether the key is free or paid tier are all unverified.
- **No clean install:** it would require downloading packages. I evidenced drift from the existing environment (pandas 3.0.5) instead.
- **Location of the first-clip excess:** start-up stall vs end vs mid-playback cannot be determined from the stored data.
- **Coder UI:** not exercised in a browser; analysed from source.
- **Whether the first-name labels are real names:** the backup-log commit message states real names were typed at login.

---

## Appendix A — Re-derivations (hand algebra, no project code)

**A1. Validation, per session** (`valid.py`). Error = mean ‖(mx,my)−(tx,ty)‖ over targets. Degrees = atan((px / (hypot(w,h)/(diag·2.54))) / d) with d = 60 cm (browser) or the iris-measured distance. Stored vs recomputed agree to ≤ 0.3 px and ≤ 0.01° in every phase of PILOT_00–07 and [named]_P2. Canonical figure (mean of pre_check and post):

| session | browser | measured | raw drift (post − pre_check) | ruler source |
|---|---|---|---|---|
| PILOT_00 | 1.44 | 1.25 | −0.14 | **missing** |
| PILOT_01 | 1.92 | 1.57 | +0.55 | inter-ocular |
| PILOT_02 | 1.65 | 1.56 | −0.09 | iris |
| PILOT_03 | 4.19 | 3.88 | +2.82 | iris |
| PILOT_04 | 2.04 | 1.75 | −1.42 | iris |
| PILOT_05 | 2.82 | 2.66 | +0.00 | iris |
| PILOT_06 | 2.25 | 2.12 | +0.05 | iris |
| PILOT_07 | 1.78 | 1.68 | −1.31 | iris |
| [named]_P2 | 2.12 | 2.31 | +2.39 | inter-ocular |
| [named]_P1 | 2.46 | 2.82 | n/a (no per-target data) | — |

**A2. Timing and rate, per stimulus** (`timing.py`). Excess = (t_end − t_start) − 30.000 s. Trigger span = offset-trigger sample − onset-trigger sample. Rates as 1 / median gap and as mean (n−1)/span.

| session | 1st clip excess (ms) | 2nd clip excess (ms) | trigger span 1st / 2nd (s) | rate median / mean (Hz) |
|---|---|---|---|---|
| [named]_P2 | 345 | 86 | 30.353 / 30.064 | 31.2 / 30.1 |
| PILOT_00 | 335 | 85 | 30.351 / 30.127 | 31.2 / 30.1 |
| PILOT_01 | 353 | 92 | 30.352 / 30.097 | 31.2 / 30.1 |
| PILOT_02 | 545 | 81 | 30.588 / 30.123 | 30.3–30.9 / 29.4–29.7 |
| PILOT_03 | 557 | 62 | 30.535 / 30.037 | 30.5–30.6 / 29.2–29.3 |
| PILOT_04 | 516 | 65 | 30.535 / 30.078 | 30.7–31.0 / 30.1 |
| PILOT_05 | 587 | 73 | 30.596 / 30.111 | 30.9–31.1 / 29.5–29.8 |
| PILOT_06 | 600 | 68 | 30.605 / 30.086 | 29.8–30.0 / 29.0–29.3 |
| PILOT_07 | 536 | 64 | 30.546 / 30.050 | 31.0–31.1 / 29.6–29.8 |

Onset lag of Flask `t_start_ns` after the onset-trigger sample: −2 … +50 ms. `gaze_samples_pct` recomputed = stored for all 18 recordings (96.7–100 %).

**A3. `claim_check` correction error** (`cc_bug.py`): constant displacement equal to the dropped affine intercept (see H-1). **F19 re-score** (`f19_rerun.py`, using claim_check's own `check_all`, accuracy 0.72°, distance 74.7 cm, 72.5 px/°): as run 10 / 7 / 42 supported / consistent / contradicted; with the true correction 32 / 13 / 14.

**A4. Counterbalancing replica** (`cb.py`): reproduces the recorded order of all nine hash-based pilots. Balance by ID scheme is in PB-5.

**A5. Keyframe crop** (`gaze_vision` geometry on a 512×288 frame, crop half-width 89 px): centred only if 89 ≤ x ≤ 423 and 89 ≤ y ≤ 199, which 325 / 1,219 pilot fixations fail.

## Appendix B — Commands used (all read-only; scripts live in the session scratchpad)

- History: `git ls-remote`; `git clone`; `git rev-list --objects --all | git cat-file --batch-check`; `git bundle list-heads …before-cleanup….bundle`; `curl -s -o /dev/null -w '%{http_code}' https://github.com/JanAulichTum/gazefollowerthesis/commit/<sha>` (status only; content not printed).
- Tests: in the clone, with `Stimuli` symlinked: `PYTHONDONTWRITEBYTECODE=1 …/envs/gaze_thesis/bin/python run_tests.py`. Result: 1,143 PASS, 2 FAIL (macOS `perf_mode` mocking), 12.9 s.
- Data: `timing.py`, `valid.py`, `cc_bug.py`, `f19_rerun.py`, `cb.py`, `llmlogs.py` (prints structure only, no prompt text), `ffprobe`/`ffmpeg scene` on the clips, `shasum -a 256` on the models.

## Appendix C — Fix sketches (not applied; you decide)

**C1. Manifest first, atomically**
```python
def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=_json_safe); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)
# _finalize_session: build manifest (validations, distance, gates, correction, head position,
# provenance) -> _atomic_json -> end_session -> segment -> update data_quality -> _atomic_json
# -> try: workbook append  except Exception as e: manifest["workbook_append_error"] = repr(e)
```
**C2. IPC matching**: add `seq` to every request; the tracker echoes it. `_send` discards replies with a different `seq` until its deadline, and sets `self._poisoned = True` (restart the tracker, mark the session) on timeout.
**C3. Media clock**: in `playNextStimulus`, register `requestVideoFrameCallback((now, md) => log.push({epoch_ms: performance.timeOrigin + md.expectedDisplayTime, media_s: md.mediaTime, presented: md.presentedFrames}))` plus `playing`/`waiting`/`stalled`/`ended` stamps. Send the log with `stop_recording`. Server: store it in `stimulus_log`, align the browser and server clocks with a ping at connect, and compute `video_time_s` by interpolating `media_s` over epoch time.
**C4. Allocation**: `allocation.csv` (enrolment_no, participant_id, order), generated once with permuted blocks of 2 and a fixed seed, committed and dated before P1. `_stimuli_for` reads it and refuses unknown IDs.
**C5. `claim_check`**: `corr = validation_stats.from_payload(manifest.get("gain_correction"))`, then `sx, sy = validation_stats.apply_point(sx, sy, corr) if corr else (sx, sy)`. The test fixture must carry a real affine and a full-affine payload.
**C6. Design A analysis**: per participant p: k_p RIGHT, j_p WRONG, u_p UNCLEAR. Pre-specify one estimand, e.g. pooled Σk / Σ(k+j), plus UNCLEAR share Σu / Σ(k+j+u). Participant-cluster bootstrap `rng = np.random.default_rng(<fixed seed>)`, B = 10,000, percentile CI. Per-clip and pooled. Blind-first contrast computed on the pre-specified subset. Run only on sessions passing the frozen `inclusion()`.
**C7. Single inclusion function** in `metrics_spec`, used by `app`, `verify_metrics`, `quality_report`, `claim_check`, the coder and the analysis, with boundary tests.
**C8. Provenance block** at session start and in the manifest: `git rev-parse HEAD`, `git status --porcelain` non-empty flag, `sys.version`, `importlib.metadata` versions for the stack, SHA-256 of the model and both stimuli, `navigator.userAgent`, `platform.platform()`, a config snapshot including `EVALUATION_FROM_DATE`.
