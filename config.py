# -*- coding: utf-8 -*-
"""
Configuration constants for the Eye-Tracking Web Experiment.

This module centralises all configuration values used across the application,
including Flask settings, file paths, eye-tracker parameters, and stimulus
discovery.
"""

import os
import secrets

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")


# ---------------------------------------------------------------------------
# Google Gemini API key (AI-feedback proof of concept)
# Priority: GEMINI_API_KEY env var > .gemini_key file next to this module.
# The key file keeps the secret out of the source code — do NOT commit or
# share it.
# ---------------------------------------------------------------------------
def _load_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.join(BASE_DIR, ".gemini_key"),
                  encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


GEMINI_API_KEY = _load_gemini_key()

# PINNED model version for all study runs. A methods thesis must state
# exactly which model produced the results — never use rolling aliases
# like "gemini-flash-latest" for data that goes into the thesis.
# Override with the GEMINI_MODEL env var if the pinned model is retired
# (open /llm_check to list the models your API key can access), and
# record any change in the methods section.
# Pin history: gemini-2.5-flash (retired) → gemini-3.5-flash (2026-07-15)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip()

# Max annotated frames per AI-feedback request. All frames travel in ONE
# API call. 60 ≈ one frame per 2 s on a 2-min video (~16k input tokens) —
# dense but comfortably within Gemini's free-tier per-request limits.
LLM_MAX_FRAMES = int(os.environ.get("LLM_MAX_FRAMES", "60"))

# Every LLM request/response is logged here (audit trail for the thesis:
# model, prompts, parameters, raw responses — images are logged as
# timestamps + SHA-256 hashes, not base64, to keep files small).
LLM_LOG_DIR = os.path.join(DATA_DIR, "llm_logs")

# Upper bound for the repeated-generation consistency mode (n_runs).
LLM_N_RUNS_MAX = int(os.environ.get("LLM_N_RUNS_MAX", "10"))

# ---------------------------------------------------------------------------
# Validation & preregistered data-quality thresholds
# (report these in the methods section; decided BEFORE data collection)
# ---------------------------------------------------------------------------
# Assumed viewing distance for px → degrees-of-visual-angle conversion.
# Webcam setups cannot measure this; the assumption is logged per session.
VIEWING_DISTANCE_CM = float(os.environ.get("VIEWING_DISTANCE_CM", "60"))

# Fallback screen diagonal (inches) when the participant does not enter
# one at login. Logged per session so the assumption is auditable.
DEFAULT_SCREEN_DIAG_INCHES = float(
    os.environ.get("SCREEN_DIAG_INCHES", "13.3"))

# Inclusion thresholds (per stimulus / per session):
MAX_VALIDATION_ERROR_DEG = float(
    os.environ.get("MAX_VALIDATION_ERROR_DEG", "3.0"))
MIN_GAZE_SAMPLES_PCT = float(os.environ.get("MIN_GAZE_SAMPLES_PCT", "60"))

# Minimum effective sampling rate (Hz). Webcam capture can silently
# decimate to half rate under CPU/thermal load; below this, fixation
# onset/offset quantization and merged-fixation bias make the temporal
# metrics unreliable, so the session is FLAGGED (not auto-excluded).
MIN_SAMPLING_HZ = float(os.environ.get("MIN_SAMPLING_HZ", "20"))

# NOMINAL (rated) sampling rate of the tracking pipeline, in Hz — the
# DENOMINATOR of the Tobii-style ``gaze_samples_pct`` data-loss metric.
#
# This MUST be a constant that is independent of the recording, otherwise
# the metric is circular: deriving "expected samples" from the session's
# own median frame interval makes every session score ~100 % by
# construction and hides exactly the frame decimation the metric exists
# to detect. Tobii's definition uses the *rated* frequency of the device,
# so we use the camera's rated frame rate (30 fps on this hardware);
# GazeFollower consumes one camera frame per gaze sample, so 30 Hz is the
# ceiling the pipeline could deliver if inference kept up.
#
# Measure yours with ``python camera_fps_test.py`` and record the value in
# the methods section together with the resulting percentages.
NOMINAL_SAMPLING_HZ = float(os.environ.get("NOMINAL_SAMPLING_HZ", "30"))

# ── Pre-session rate gate ──
# Seconds of continuous inference run BEFORE calibration, while the
# participant reads the instructions (so it costs no extra time). Long
# enough to outlast the CPU turbo window, which is what makes the rate
# halve 20–40 s into a session. Doubles as the warm-up burn: afterwards,
# calibration and the stimuli are recorded at the same sustained rate.
# Set to 0 to disable the gate entirely.
RATE_GATE_SECONDS = float(os.environ.get("RATE_GATE_SECONDS", "25"))
# The gate verdict is the rate over the FINAL N seconds — the sustained
# figure, not the flattering opening burst.
RATE_GATE_TAIL_SECONDS = float(os.environ.get("RATE_GATE_TAIL_SECONDS", "8"))

# A session whose frame intervals are MULTIMODAL (e.g. a mix of ~33 ms and
# ~78 ms gaps) is not simply "slow" — it alternates between full rate and
# decimated rate, which biases fixation detection in a time-varying way.
# Flagged when the ratio of the median to the 10th-percentile interval
# exceeds this factor.
RATE_MULTIMODAL_RATIO = float(os.environ.get("RATE_MULTIMODAL_RATIO", "1.5"))

# ---------------------------------------------------------------------------
# Fixation detection (offline I-DT, adapted to low webcam sampling rates)
# ---------------------------------------------------------------------------
# Dispersion threshold in NORMALIZED video coordinates (fraction of the
# video frame; 0.05 ≈ 1.5° for a video spanning ~30° of visual angle).
FIXATION_DISPERSION_NORM = float(
    os.environ.get("FIXATION_DISPERSION_NORM", "0.05"))
# Minimum fixation duration in seconds (≥ 3 samples at 15 Hz).
FIXATION_MIN_DURATION_S = float(
    os.environ.get("FIXATION_MIN_DURATION_S", "0.20"))

# Stimuli: prefer a local static/stimuli folder if it contains videos,
# otherwise fall back to the "Stimuli" folder next to the project
# (MASTER THESIS/Stimuli), where the MP4 files actually live.
_LOCAL_STIMULI = os.path.join(BASE_DIR, "static", "stimuli")
_PARENT_STIMULI = os.path.join(os.path.dirname(BASE_DIR), "Stimuli")


def _pick_stimuli_dir() -> str:
    """Return the first stimuli directory containing PLAYABLE video files.

    ``os.path.isfile`` follows symlinks, so a folder of broken symlinks
    (e.g. after the project is moved) is correctly rejected in favour of
    the folder with the real files.
    """
    for candidate in (_LOCAL_STIMULI, _PARENT_STIMULI):
        if os.path.isdir(candidate) and any(
            os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
            and os.path.isfile(os.path.join(candidate, f))
            and os.path.getsize(os.path.join(candidate, f)) > 0
            for f in os.listdir(candidate)
        ):
            return candidate
    return _LOCAL_STIMULI


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
# Persist the secret key across restarts so participant sessions survive
# a server restart mid-study.
_SECRET_KEY_FILE = os.path.join(BASE_DIR, ".secret_key")


def _load_or_create_secret_key() -> str:
    try:
        if os.path.isfile(_SECRET_KEY_FILE):
            with open(_SECRET_KEY_FILE, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
                if key:
                    return key
        key = secrets.token_hex(32)
        with open(_SECRET_KEY_FILE, "w", encoding="utf-8") as fh:
            fh.write(key)
        return key
    except OSError:
        # Fall back to an ephemeral key if the file cannot be written
        return secrets.token_hex(32)


SECRET_KEY = os.environ.get("SECRET_KEY") or _load_or_create_secret_key()

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
PARTICIPANTS_FILE = os.path.join(DATA_DIR, "participants.xlsx")

# GazeFollower (sole tracker): per-stimulus raw CSVs plus a combined
# Excel workbook for analysis.
GAZEFOLLOWER_DATA_FILE = os.path.join(DATA_DIR, "gazefollower_data.xlsx")
GAZEFOLLOWER_CSV_DIR = os.path.join(DATA_DIR, "gazefollower_raw")

# ---------------------------------------------------------------------------
# Stimulus discovery
# ---------------------------------------------------------------------------
SUPPORTED_EXTENSIONS = {".mp4", ".webm", ".ogg"}

STIMULI_DIR = _pick_stimuli_dir()


# Files beginning with this prefix are helper clips (e.g. the 30 s test
# clip) — never presented as real stimuli.
TESTCLIP_PREFIX = "_testclip"
TEST_CLIP_30S = "_testclip_30s.mp4"


def discover_stimuli() -> list[str]:
    """Return a sorted list of stimulus filenames found in STIMULI_DIR.

    Helper clips (``_testclip*``) are excluded so they never appear in a
    real participant's randomized stimulus list.
    """
    if not os.path.isdir(STIMULI_DIR):
        return []
    return sorted(
        f
        for f in os.listdir(STIMULI_DIR)
        if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
        and not f.startswith(TESTCLIP_PREFIX)
    )


# ---------------------------------------------------------------------------
# Test mode — start the server with  TEST_MODE=1 python app.py
# Only the first stimulus is shown, and playback stops after 5 seconds.
# ---------------------------------------------------------------------------
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() in ("1", "true", "yes")
TEST_VIDEO_SECONDS = 5

# ---------------------------------------------------------------------------
# What a NORMAL (non-test) run presents.
#   "clip30"  one real 30 s clip (_testclip_30s.mp4), played in full
#   "1full"   the participant's first shuffled stimulus, full length
#   "all"     every stimulus, full length
# Default is a single 30 s clip: short sessions keep the sampling rate
# stable and make pilot/demo runs repeatable. Switch to "all" for real
# data collection and RECORD which mode produced each dataset.
# ---------------------------------------------------------------------------
SESSION_STIMULUS_MODE = os.environ.get(
    "SESSION_STIMULUS_MODE", "clip30").strip().lower()

# ---------------------------------------------------------------------------
# In-browser validation targets (pre = after calibration, post = after
# the last video; both are MANDATORY — accuracy is a per-session outcome).
# Both phases sample the SAME 7 targets across FIVE vertical elevations so
# that (a) the nonlinear up-gaze overshoot can be characterized and
# corrected (quadratic vertical fit) and (b) pre/post drift is a
# like-for-like comparison on identical geometry. A smaller post set makes
# the drift estimate depend on WHICH targets were dropped — with 3 of 7
# targets the post mean is dominated by different screen eccentricities
# than the pre mean, so "drift" partly measures the target set, not the
# tracker. The exact positions live in experiment.js.
# ---------------------------------------------------------------------------
VALIDATION_TARGETS_PRE = 7
VALIDATION_TARGETS_POST = 7
