# -*- coding: utf-8 -*-
"""
Eye-Tracking Web Experiment – Main Flask + SocketIO Application
================================================================

This is the backend server for a browser-based eye-tracking study.
Participants log in, complete a calibration phase with MANDATORY
accuracy validation (pre and post), then view video stimuli while their
gaze is recorded with **GazeFollower** – a deep-learning webcam gaze
tracker (~1 cm accuracy after calibration).  It runs in a dedicated
subprocess (see ``tracker_service.py``) that owns the webcam and the
native calibration window; controlled via :mod:`gaze_service`.

Data is persisted to Excel files for offline analysis; every session
writes a JSON manifest with validation results, preregistered quality
thresholds, and pass/fail verdicts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sys

# Cross-shell test-mode switch: `python app.py --test` works on Windows
# cmd/PowerShell as well as macOS/Linux, where `TEST_MODE=1 python app.py`
# is bash-only. Must run BEFORE `config` is imported (config reads
# TEST_MODE at import time); the tracker subprocess inherits this env.
if "--test" in sys.argv:
    os.environ["TEST_MODE"] = "1"
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from typing import Any

import pandas as pd
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    FIXATION_DISPERSION_NORM,
    DATA_DIR,
    DEFAULT_SCREEN_DIAG_INCHES,
    TEST_CLIP_30S,
    GAZEFOLLOWER_CSV_DIR,
    GAZEFOLLOWER_DATA_FILE,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LLM_LOG_DIR,
    LLM_MAX_FRAMES,
    LLM_TIMEOUT_BASE_S,
    LLM_TIMEOUT_PER_FRAME_S,
    LLM_N_RUNS_MAX,
    LLM_WINDOW_SECONDS,
    MAX_VALIDATION_ERROR_DEG,
    MIN_GAZE_SAMPLES_PCT,
    MIN_SAMPLING_HZ,
    NOMINAL_SAMPLING_HZ,
    RATE_GATE_SECONDS,
    RATE_GATE_TAIL_SECONDS,
    RATE_MULTIMODAL_RATIO,
    SESSION_STIMULUS_MODE,
    PARTICIPANTS_FILE,
    SECRET_KEY,
    STIMULI_DIR,
    TEST_MODE,
    TEST_VIDEO_SECONDS,
    VIEWING_DISTANCE_CM,
    discover_stimuli,
)
from excel_style import style_workbook
from gaze_service import GazeService

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

# Persist all server logs to data/server.log so problems can be
# diagnosed after the fact (console output is easy to lose).
os.makedirs(DATA_DIR, exist_ok=True)
_file_handler = logging.FileHandler(
    os.path.join(DATA_DIR, "server.log"), encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s – %(message)s")
)
logging.getLogger().addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Flask & SocketIO initialisation
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Cache-busting version for static assets (changes on every server start,
# so browsers always fetch the current experiment.js / style.css instead
# of serving stale cached copies after an update).
ASSET_VERSION = str(int(time.time()))


@app.context_processor
def inject_globals():
    return {
        "asset_version": ASSET_VERSION,
        "test_mode": TEST_MODE,
        "current_year": datetime.now().year,
    }

# "threading" async mode: works on all Python versions (eventlet is
# deprecated and incompatible with Python >= 3.12).  WebSocket transport
# is provided by the `simple-websocket` package.
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# ---------------------------------------------------------------------------
# GazeFollower tracker service (one shared instance — single-participant lab
# setup; the tracker subprocess owns the physical webcam)
# ---------------------------------------------------------------------------
gaze_service = GazeService()

# Forward tracker progress (model loading, window opening, …) to the
# browser so slow steps don't look like a frozen application.
gaze_service.set_status_callback(
    lambda msg: socketio.emit("native_calibration_status", msg)
)

# ---------------------------------------------------------------------------
# Thread-safe locks for shared data files
# ---------------------------------------------------------------------------
_participants_lock = threading.Lock()
_gazefollower_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Per-session state (keyed by SocketIO session ID)
# ---------------------------------------------------------------------------
active_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()

# =========================================================================
# Data-persistence helpers
# =========================================================================

_PARTICIPANTS_COLUMNS = [
    "participant_id",
    "password_hash",
    "consent_given",
    "created_at",
    "screen_diag_inches",
]



def _read_or_create_excel(
    path: str, columns: list[str], lock: threading.Lock
) -> pd.DataFrame:
    """Read an Excel file, or return an empty DataFrame with *columns*."""
    with lock:
        if os.path.isfile(path):
            try:
                return pd.read_excel(path)
            except Exception:
                logger.exception("Corrupt Excel file – recreating: %s", path)
        return pd.DataFrame(columns=columns)


def _append_to_excel(
    path: str,
    new_rows: pd.DataFrame,
    columns: list[str],
    lock: threading.Lock,
) -> None:
    """Append *new_rows* to an existing Excel workbook (or create it).

    Robustness guarantees:
    * A corrupt existing file is preserved as ``<name>.corrupt-<ts>``
      instead of being silently overwritten (research data is precious).
    * The write is ATOMIC: data is written to a temporary file first and
      then moved into place, so a crash mid-write can never destroy the
      existing workbook.
    """
    with lock:
        if os.path.isfile(path):
            try:
                existing = pd.read_excel(path)
            except Exception:
                backup = "%s.corrupt-%d" % (path, int(time.time()))
                logger.exception(
                    "Corrupt Excel — preserving as %s and starting fresh", backup
                )
                try:
                    os.replace(path, backup)
                except OSError:
                    logger.exception("Could not back up corrupt file")
                existing = pd.DataFrame(columns=columns)
        else:
            existing = pd.DataFrame(columns=columns)

        combined = pd.concat([existing, new_rows], ignore_index=True)

        # NOTE: the temp file MUST end in .xlsx — pandas infers the Excel
        # writer engine from the extension and raises otherwise.
        tmp_path = path + ".tmp.xlsx"
        combined.to_excel(tmp_path, index=False)
        style_workbook(tmp_path)     # header style, frozen row, widths
        os.replace(tmp_path, path)   # atomic on POSIX filesystems


# ---- Participant management ------------------------------------------------

def save_participant(
    participant_id: str,
    password_hash: str,
    consent_given: bool,
    screen_diag_inches: "float | None" = None,
) -> None:
    """Persist a new participant record to the participants Excel file."""
    row = pd.DataFrame(
        [
            {
                "participant_id": participant_id,
                "password_hash": password_hash,
                "consent_given": consent_given,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "screen_diag_inches": screen_diag_inches,
            }
        ]
    )
    _append_to_excel(PARTICIPANTS_FILE, row, _PARTICIPANTS_COLUMNS, _participants_lock)
    logger.info("Saved new participant: %s", participant_id)


def get_participant(participant_id: str) -> dict | None:
    """Load a participant record by ID, or return *None*."""
    df = _read_or_create_excel(
        PARTICIPANTS_FILE, _PARTICIPANTS_COLUMNS, _participants_lock
    )
    match = df[df["participant_id"] == participant_id]
    if match.empty:
        return None
    return match.iloc[0].to_dict()


# ---- Gaze data ---------------------------------------------------------

def finalize_gazefollower_session(
    participant_id: str,
    csv_path: str,
    stimulus_log: list[dict[str, Any]],
    correction: "dict | None" = None,
) -> dict[str, int]:
    """Save the continuous session recording and split it per stimulus.

    GazeFollower records ONE continuous CSV per session (its API allows
    only a single ``save_data`` call). Each row carries an absolute
    epoch-nanosecond timestamp from the camera. The Flask side logged
    wall-clock start/end times for every stimulus, so rows are assigned
    to stimuli by timestamp window. Samples between stimuli (rest
    screens) are excluded. Trigger markers (onset 100+2k / offset
    101+2k) are additionally embedded in the CSV as redundancy.

    Returns:
        Dict of ``{stimulus_name: sample_count}``.
    """
    # Session identity: distinguishes repeated runs by the same
    # participant (otherwise the replay viewer would interleave several
    # recordings of the same stimulus).
    session_id = os.path.splitext(os.path.basename(csv_path))[0]

    if gaze_service.end_session(csv_path) is None:
        logger.error("GazeFollower session save failed — no data written.")
        return {}

    try:
        df = pd.read_csv(csv_path)
    except Exception:
        logger.exception("Could not read GazeFollower session CSV: %s", csv_path)
        return {}
    if df.empty or "timestamp" not in df.columns:
        logger.warning("GazeFollower session CSV empty or malformed.")
        return {}

    # Session's ACHIEVED median frame-to-frame gap. NOTE: this is the
    # measured rate, NOT a nominal rate — it is used only for the
    # relative-yield diagnostic below, never as the denominator of
    # gaze_samples_pct (see NOMINAL_SAMPLING_HZ).
    median_dt_ns = float(df["timestamp"].sort_values().diff().median()) \
        if len(df) > 1 else 0.0
    # Fixed, recording-independent sampling interval for the Tobii-style
    # data-loss metric.
    nominal_dt_ns = 1e9 / NOMINAL_SAMPLING_HZ if NOMINAL_SAMPLING_HZ else 0.0

    counts: dict[str, Any] = {}
    events: dict = {}
    quality: dict[str, dict] = {}
    for entry in stimulus_log:
        segment = df[
            (df["timestamp"] >= entry["t_start_ns"])
            & (df["timestamp"] <= entry["t_end_ns"])
        ].copy()
        counts[entry["stimulus"]] = len(segment)

        # ── Data-loss metrics (Tobii-style "gaze samples") ──
        # valid_pct       : % of captured frames with successful estimation.
        #                   GazeFollower reports status==1 for every frame
        #                   it emits, so this is ~100 % by construction and
        #                   is NOT a data-quality measure on its own.
        # gaze_samples_pct: valid samples ÷ samples expected at the
        #                   NOMINAL rate (Tobii's definition). The nominal
        #                   rate is a fixed constant, so this figure
        #                   captures dropped/decimated frames — which is
        #                   the dominant loss mechanism here.
        # relative_yield_pct: samples ÷ expected at the session's OWN
        #                   median rate. Near 100 % by construction;
        #                   useful only to show whether THIS stimulus ran
        #                   slower than the rest of the session. Never
        #                   report it as a data-loss figure.
        dur_ns = entry["t_end_ns"] - entry["t_start_ns"]
        expected = (dur_ns / nominal_dt_ns) if nominal_dt_ns > 0 else 0.0
        expected_rel = (dur_ns / median_dt_ns) if median_dt_ns > 0 else 0.0
        valid = int((segment["status"] == 1).sum()) \
            if "status" in segment.columns else len(segment)
        # Effective sampling rate for THIS stimulus segment (webcam
        # capture can decimate to half rate under load; below
        # MIN_SAMPLING_HZ the temporal metrics are unreliable and the
        # segment is flagged).
        seg_gaps = segment["timestamp"].sort_values().diff().dropna()
        seg_dt_ns = float(seg_gaps.median()) if len(seg_gaps) else 0.0
        seg_hz = round(1e9 / seg_dt_ns, 1) if seg_dt_ns > 0 else 0.0
        # Multimodality: a session that alternates between full rate and
        # decimated rate has a median interval well above its fastest
        # (p10) interval. Reporting a single Hz for such a recording is
        # misleading, so flag it explicitly.
        dt_p10_ns = float(seg_gaps.quantile(0.10)) if len(seg_gaps) else 0.0
        rate_ratio = (seg_dt_ns / dt_p10_ns) if dt_p10_ns > 0 else 1.0
        quality[entry["stimulus"]] = {
            "samples": len(segment),
            "valid_samples": valid,
            "valid_pct": round(100 * valid / len(segment), 1)
            if len(segment) else 0.0,
            "gaze_samples_pct": round(min(100.0, 100 * valid / expected), 1)
            if expected else 0.0,
            "nominal_sampling_hz": NOMINAL_SAMPLING_HZ,
            "relative_yield_pct": round(
                min(100.0, 100 * valid / expected_rel), 1)
            if expected_rel else 0.0,
            "sampling_hz": seg_hz,
            "sampling_hz_fastest": round(1e9 / dt_p10_ns, 1)
            if dt_p10_ns > 0 else 0.0,
            "rate_ratio": round(rate_ratio, 2),
            "rate_multimodal": bool(rate_ratio >= RATE_MULTIMODAL_RATIO),
            "low_sampling_rate": bool(seg_hz and seg_hz < MIN_SAMPLING_HZ),
        }

        if segment.empty:
            logger.warning(
                "No GazeFollower samples for stimulus %s (face not "
                "detected during that window?)", entry["stimulus"],
            )
            continue
        segment.insert(0, "participant_id", participant_id)
        segment.insert(1, "session_id", session_id)
        segment.insert(2, "stimulus_name", entry["stimulus"])
        # Relative time within the stimulus (seconds) — the main time
        # axis for analysis ("where in the video was the participant
        # looking at second X").
        segment.insert(
            3, "video_time_s",
            ((segment["timestamp"] - entry["t_start_ns"]) / 1e9).round(3),
        )
        # Human-readable wall-clock time. The raw `timestamp` column is
        # epoch NANOSECONDS (19 digits) — it only looks constant because
        # its leading digits change on the scale of months.
        segment.insert(
            4, "clock_time_utc",
            pd.to_datetime(segment["timestamp"], unit="ns")
            .dt.strftime("%Y-%m-%d %H:%M:%S.%f"),
        )
        # Validation-based gain correction (POST-HOC): webcam gaze
        # estimators systematically under-shoot eccentric gaze ("right
        # direction, not far enough"), and the vertical axis is often
        # nonlinear (up-gaze too high). The per-axis polynomial
        # correction (linear x, up to quadratic y) is fitted from the
        # pre-validation targets (or set via the manual slider) and
        # applied here — the raw filtered/calibrated columns stay
        # untouched; corrected coordinates get their own columns and
        # drive the video-normalized coordinates below.
        gx = segment["filtered_gaze_position_x"]
        gy = segment["filtered_gaze_position_y"]
        if correction:
            gx, gy = _apply_series(gx, gy, correction)
            segment["corrected_gaze_position_x"] = gx.round(2)
            segment["corrected_gaze_position_y"] = gy.round(2)

        # Normalized video coordinates: (0,0) = video top-left,
        # (1,1) = video bottom-right; values outside 0–1 = gaze on the
        # letterbox/outside the frame. Basis for the replay overlay and
        # region-of-interest statistics.
        vr = entry.get("video_rect") or {}
        if vr.get("w") and vr.get("h"):
            segment["gaze_video_nx"] = ((gx - vr["x"]) / vr["w"]).round(4)
            segment["gaze_video_ny"] = ((gy - vr["y"]) / vr["h"]).round(4)
        # Readability: sub-pixel precision is far below the tracker's
        # real accuracy — 2 decimals is plenty (raw CSV keeps originals).
        for col in segment.columns:
            if "gaze_position" in col:
                segment[col] = pd.to_numeric(
                    segment[col], errors="coerce").round(2)
        _append_to_excel(
            GAZEFOLLOWER_DATA_FILE, segment, list(segment.columns),
            _gazefollower_lock,
        )
        logger.info(
            "Saved %d GazeFollower samples – participant=%s, stimulus=%s",
            len(segment), participant_id, entry["stimulus"],
        )

        # ── RQ2 event metrics, PERSISTED ─────────────────────────────
        # Previously these were computed only inside quality_report.py,
        # so they existed on screen and nowhere else — which is why every
        # RQ2 metric showed as MISSING in verify_metrics. Recording them
        # here makes them part of the session record.
        try:
            events[entry["stimulus"]] = _event_metrics(segment)
        except Exception:  # noqa: BLE001 — never lose a session over stats
            logger.exception("Event metrics failed for %s", entry["stimulus"])

    logger.info(
        "Data loss (Tobii-style gaze samples %%): %s",
        {k: v["gaze_samples_pct"] for k, v in quality.items()},
    )
    counts["__quality__"] = quality
    counts["__events__"] = events
    return counts


# =========================================================================
# Auth decorator
# =========================================================================

def login_required(f):
    """Redirect unauthenticated users to the landing page."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "participant_id" not in session:
            return redirect(url_for("index"))
        return f(*args, **kwargs)

    return decorated_function


# =========================================================================
# Flask routes
# =========================================================================

@app.route("/")
def index():
    """Landing / login page."""
    return render_template("index.html")


@app.route("/login", methods=["POST"])
def login():
    """Handle participant login / registration.

    * **New participant**: hash password, store record, set session.
    * **Existing participant**: verify password hash, set session.
    """
    participant_id = request.form.get("participant_id", "").strip()
    password = request.form.get("password", "")
    consent = request.form.get("consent")  # checkbox value or None

    # --- Basic validation ---------------------------------------------------
    if not participant_id:
        flash("Please enter a participant ID.", "error")
        return redirect(url_for("index"))

    if not password:
        flash("Please enter a password.", "error")
        return redirect(url_for("index"))

    # --- Test-mode run configuration (from the login form) -------------------
    if TEST_MODE:
        session["test_opts"] = {
            "cal": request.form.get("test_cal", "quick"),
            "videos": request.form.get("test_videos", "5s"),
        }

    # --- Screen diagonal (optional; needed for px → degrees) -----------------
    # Logged per session; falls back to a documented default assumption.
    try:
        screen_diag = float(request.form.get("screen_diag", "").strip())
        if not (5 < screen_diag < 60):
            screen_diag = None
    except (TypeError, ValueError):
        screen_diag = None
    session["screen_diag_inches"] = screen_diag or DEFAULT_SCREEN_DIAG_INCHES
    session["screen_diag_assumed"] = screen_diag is None

    # --- Recording conditions (RQ1: "varying recording conditions") ---------
    # Collection happens in DIFFERENT ROOMS with different light. That
    # variation is the independent variable RQ1 asks about, but it is
    # only analysable if it is recorded AT THE TIME. It cannot be
    # reconstructed afterwards from the gaze data, and "I think that one
    # was the bright room" is not a covariate.
    #
    # Free text is deliberately avoided for the categorical fields: a
    # fixed vocabulary can be tabulated across 10 sessions, "quite
    # bright I guess" cannot.
    conditions = {
        "room": (request.form.get("room", "").strip() or None),
        "lighting": (request.form.get("lighting", "").strip() or None),
        "glasses": (request.form.get("glasses", "").strip() or None),
        "time_of_day": datetime.now().strftime("%H:%M"),
        "notes": (request.form.get("condition_notes", "").strip() or None)[:300]
        if request.form.get("condition_notes", "").strip() else None,
    }
    session["conditions"] = conditions
    if not conditions["room"] or not conditions["lighting"]:
        logger.warning(
            "Recording conditions incomplete (room=%s, lighting=%s). RQ1 asks "
            "about VARYING recording conditions; unrecorded conditions cannot "
            "be analysed and cannot be recovered later.",
            conditions["room"], conditions["lighting"])

    # --- Existing participant -----------------------------------------------
    existing = get_participant(participant_id)

    if existing is not None:
        if not check_password_hash(existing["password_hash"], password):
            flash("Incorrect password for this participant ID.", "error")
            return redirect(url_for("index"))

        session["participant_id"] = participant_id
        logger.info("Returning participant logged in: %s", participant_id)
        return redirect(url_for("calibration"))

    # --- New participant ----------------------------------------------------
    if not consent:
        flash("You must give consent before participating.", "error")
        return redirect(url_for("index"))

    pw_hash = generate_password_hash(password)
    save_participant(participant_id, pw_hash, consent_given=True,
                     screen_diag_inches=screen_diag)

    session["participant_id"] = participant_id
    logger.info("New participant registered: %s", participant_id)
    return redirect(url_for("calibration"))


@app.route("/calibration")
@login_required
def calibration():
    """Eye-tracker calibration page (GazeFollower native calibration +
    mandatory accuracy validation)."""
    return render_template(
        "calibration.html",
        screen_diag_inches=session.get(
            "screen_diag_inches", DEFAULT_SCREEN_DIAG_INCHES),
        viewing_distance_cm=VIEWING_DISTANCE_CM,
        max_validation_error_deg=MAX_VALIDATION_ERROR_DEG,
    )


@app.route("/stimuli")
@login_required
def stimuli():
    """Stimulus presentation page.

    The stimulus list is deterministically shuffled per participant so
    that each participant sees the same random order on revisits, while
    different participants get different orderings.
    """
    participant_id: str = session["participant_id"]
    stimuli_list, clip = _apply_test_options(_stimuli_for(participant_id))
    return render_template(
        "stimuli.html",
        stimuli=stimuli_list,
        participant_id=participant_id,
        clip_video=clip is not None,
    )


def _apply_test_options(stimuli: list[str]) -> "tuple[list[str], int | None]":
    """Apply the test-mode video scope. Returns (stimuli, clip_seconds).

    clip_seconds is None when videos play full length.
    """
    if not TEST_MODE:
        # A normal run follows SESSION_STIMULUS_MODE (default: one real
        # 30 s clip). Short sessions hold the sampling rate steady and
        # make pilot runs repeatable; set SESSION_STIMULUS_MODE=all for
        # real data collection.
        choice = SESSION_STIMULUS_MODE
    else:
        choice = (session.get("test_opts") or {}).get("videos", "5s")
    if choice == "all":
        return stimuli, None
    if choice == "1full":
        return stimuli[:1], None
    if choice == "clip30":
        # A REAL 30 s video (an actual short clip file), played in full —
        # not a full-length stimulus cut short after N seconds.
        if os.path.isfile(os.path.join(STIMULI_DIR, TEST_CLIP_30S)):
            return [TEST_CLIP_30S], None
        logger.warning("SESSION_STIMULUS_MODE=clip30 but %s is missing in "
                       "%s — falling back to one full stimulus.",
                       TEST_CLIP_30S, STIMULI_DIR)
        return stimuli[:1], None      # clip missing → fall back to 1 full
    if not TEST_MODE:
        return stimuli[:1], None      # unknown mode → one full stimulus
    return stimuli[:1], TEST_VIDEO_SECONDS   # test default: "5s"


def _stimuli_for(participant_id: str) -> list[str]:
    """Deterministically shuffled stimulus list for a participant.

    NOTE: built-in hash() is salted per process (PYTHONHASHSEED), so a
    cryptographic digest is used instead to guarantee the same order
    for the same participant across server restarts.
    """
    stimulus_list = discover_stimuli()
    seed = int(hashlib.sha256(participant_id.encode("utf-8")).hexdigest(), 16)
    random.Random(seed).shuffle(stimulus_list)
    return stimulus_list


@app.route("/api/stimuli")
@login_required
def api_stimuli():
    """JSON stimulus list — used by the single-page calibration flow to
    start the videos directly after calibration without a page change
    (page navigation would exit fullscreen).

    In TEST_MODE only the first stimulus is returned and the client
    limits playback to a few seconds.
    """
    stimuli, clip = _apply_test_options(_stimuli_for(session["participant_id"]))
    return {
        "stimuli": stimuli,
        "test_mode": clip is not None,       # true = clip playback
        "test_video_seconds": clip or TEST_VIDEO_SECONDS,
    }


@app.route("/stimulus/<path:filename>")
def stimulus_file(filename: str):
    # No login gate: also used by the researcher review tool.
    """Serve a stimulus video file.

    Videos may live outside the Flask ``static`` folder (e.g. in the
    thesis-level ``Stimuli`` directory), so they are served through this
    dedicated route.  ``send_from_directory`` supports HTTP range
    requests, which browsers require for smooth video playback/seeking.
    """
    return send_from_directory(STIMULI_DIR, filename, conditional=True)


# =========================================================================
# Researcher tools: gaze replay viewer + LLM feedback proof-of-concept
# =========================================================================

def _load_gaze_table() -> "pd.DataFrame | None":
    """Load the combined GazeFollower workbook (or None if absent)."""
    with _gazefollower_lock:
        if not os.path.isfile(GAZEFOLLOWER_DATA_FILE):
            return None
        try:
            return pd.read_excel(GAZEFOLLOWER_DATA_FILE)
        except Exception:
            logger.exception("Could not read %s", GAZEFOLLOWER_DATA_FILE)
            return None


# NOTE: the review tool and its APIs are intentionally NOT behind the
# participant login — the researcher opens them from the welcome page.
# The app only serves on the local machine.

@app.route("/review")
def review():
    """Researcher page: replay a stimulus with the gaze overlay."""
    return render_template("review.html")


@app.route("/coder")
def coder():
    """Human coding of individual fixations — the study's only human anchor.

    Everything else in RQ3 is one model's output checked against another
    measurement. That establishes correspondence, not correctness: a
    model and a tracker can agree and both be describing the wrong
    thing. Only a person looking at the frame can say whether "the
    participant looked at the girl in the red shirt" is true.

    It is also the only route to a reliability coefficient. Cohen's
    kappa needs two raters assigning the same labels to the same units;
    with no human ratings at all there is nothing to correlate, and
    "the model agreed with itself across runs" is consistency, not
    validity.
    """
    return render_template("coder.html")


@app.route("/api/coding_units")
def api_coding_units():
    """The fixations to code, each with the model's claim about it."""
    participant = request.args.get("participant", "")
    stimulus = request.args.get("stimulus", "")
    session = request.args.get("session", "")

    df = _load_gaze_table()
    if df is None or df.empty:
        return {"units": [], "error": "no data file yet"}
    sel = df[(df["participant_id"] == participant)
             & (df["stimulus_name"] == stimulus)]
    if session and "session_id" in sel.columns:
        sel = sel[sel["session_id"].fillna("legacy") == session]
    if sel.empty:
        return {"units": [], "error": "no samples for that recording"}

    try:
        from fixations import detect_fixations_df

        fixations = detect_fixations_df(sel)
    except Exception as exc:  # noqa: BLE001
        return {"units": [], "error": "fixation detection failed: %s" % exc}

    # The model's claims. TWO sources, in order, because the manifest
    # write-back is recent: sessions whose feedback was generated before
    # it exists have an empty llm block, and the only record of what the
    # model said is the log directory. Without the fallback every
    # fixation reads "no model claim covers this", which looks like a
    # coding problem and is not one.
    claims = []
    claims_source = None
    frame_times = []
    mpath = os.path.join(GAZEFOLLOWER_CSV_DIR, session + "_manifest.json")
    accuracy_deg = None
    if os.path.isfile(mpath):
        try:
            with open(mpath, encoding="utf-8") as fh:
                man = json.load(fh)
            _blk = (man.get("llm") or {}).get(stimulus) or {}
            claims = _blk.get("structured") or []
            frame_times = _blk.get("frame_times") or []
            if claims:
                claims_source = "session manifest"
            import claim_check

            accuracy_deg = claim_check._accuracy_deg(man)[0]
        except Exception:  # noqa: BLE001
            pass
    if not claims:
        try:
            import claim_check

            claims, log_path = claim_check.load_claims(session)
            if claims:
                claims_source = "log: %s" % os.path.basename(log_path or "?")
        except Exception:  # noqa: BLE001
            pass

    # OVERLAP, not proximity to the midpoint.
    # A claim in "fixations" mode names an INSTANT (t_start == t_end);
    # a fixation has DURATION. Comparing the claim's start against the
    # fixation's midpoint misses whenever the fixation is long — a
    # 900 ms fixation puts its midpoint 450 ms from the claim that
    # names its onset. Match anything landing inside the fixation, plus
    # a margin for the sampling interval, and take the nearest.
    MATCH_MARGIN_S = 0.35

    def _match(f):
        lo = f.t_start - MATCH_MARGIN_S
        hi = f.t_start + f.duration + MATCH_MARGIN_S
        hits = []
        for c in claims:
            if not isinstance(c, dict):
                continue
            cs = float(c.get("t_start") or 0)
            ce = float(c.get("t_end") or cs)
            if ce >= lo and cs <= hi:
                centre = f.t_start + f.duration / 2.0
                hits.append((abs((cs + ce) / 2.0 - centre), c))
        hits.sort(key=lambda h: h[0])
        return hits[0][1] if hits else None

    # Was this fixation SHOWN to the model?
    # If it was not, no claim about it can be right or wrong — the model
    # was never asked. Marking these rather than hiding them keeps the
    # denominator honest: "the model saw 60 of 71 fixations" is a fact
    # about the pipeline that belongs in the results, not a detail to
    # quietly drop.
    def _was_shown(mid: float) -> bool:
        if not frame_times:
            return True          # unknown: assume yes rather than accuse
        return any(abs(float(t) - mid) <= 0.25 for t in frame_times)

    units = []
    for i, f in enumerate(fixations):
        mid = f.t_start + (f.duration / 2.0)
        matched = _match(f)
        near = [matched] if matched else []
        shown = _was_shown(mid)
        units.append({
            "shown_to_model": shown,
            "index": i,
            "t_start": round(f.t_start, 3),
            "t_end": round(f.t_start + f.duration, 3),
            "t_mid": round(mid, 3),
            "duration_ms": int(1000 * f.duration),
            "x": round(f.nx, 4),
            "y": round(f.ny, 4),
            "model_claim": (near[0].get("attended") if near else None),
            "model_bbox": (near[0].get("bbox") if near else None),
            "model_confidence": (near[0].get("confidence") if near else None),
        })
    matched = sum(1 for u in units if u["model_claim"])
    n_shown = sum(1 for u in units if u.get("shown_to_model"))
    return {"units": units, "stimulus": stimulus,
            "n_shown_to_model": n_shown,
            "n_frames_sent": len(frame_times),
            "participant": participant, "session": session,
            "accuracy_deg": accuracy_deg,
            "n_claims": len(claims),
            "claims_source": claims_source,
            "n_matched": matched,
            # "no claims were loaded" and "claims were loaded but none
            # line up in time" are different faults with different
            # fixes, and they look identical from inside the coder.
            "match_warning": (
                "No LLM claims found for this session at all — generate "
                "the feedback in the review tool first."
                if not claims else
                ("Loaded %d claims but only %d of %d fixations matched one "
                 "in time. Check that the claims come from THIS stimulus."
                 % (len(claims), matched, len(units))
                 if matched < 0.5 * len(units) else None))}


@app.route("/api/coding_save", methods=["POST"])
def api_coding_save():
    """Persist one coder's verdicts.

    Written per CODER, never merged. Two coders' judgments must stay
    separable or there is no kappa to compute — and a merged file
    silently becomes one rater with no way back.
    """
    payload = request.get_json(silent=True) or {}
    coder = _safe_filename(str(payload.get("coder") or "").strip())
    session = _safe_filename(str(payload.get("session") or "").strip())
    stimulus = _safe_filename(
        os.path.splitext(str(payload.get("stimulus") or ""))[0])
    if not (coder and session and stimulus):
        return {"ok": False, "error": "coder, session and stimulus required"}, 400

    out_dir = os.path.join(DATA_DIR, "coding")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s__%s__%s.json" % (session, stimulus, coder))
    record = {
        "coder": payload.get("coder"),
        "session": payload.get("session"),
        "stimulus": payload.get("stimulus"),
        "saved_utc": datetime.now(timezone.utc).isoformat(),
        "codes": payload.get("codes") or {},
        # The rubric the coder was working to. Judgments made under
        # different instructions are not the same variable, so a file
        # without this cannot safely be pooled with another.
        "instructions": payload.get("instructions"),
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}, 500
    logger.info("Coding saved: %s (%d units)", os.path.basename(path),
                len(record["codes"]))
    return {"ok": True, "path": os.path.basename(path),
            "n_coded": len(record["codes"])}


@app.route("/api/coding_load")
def api_coding_load():
    """Any verdicts this coder already recorded for this recording."""
    coder = _safe_filename(request.args.get("coder", ""))
    session = _safe_filename(request.args.get("session", ""))
    stimulus = _safe_filename(
        os.path.splitext(request.args.get("stimulus", ""))[0])
    path = os.path.join(DATA_DIR, "coding",
                        "%s__%s__%s.json" % (session, stimulus, coder))
    if not os.path.isfile(path):
        return {"codes": {}, "found": False}
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        return {"codes": rec.get("codes") or {}, "found": True,
                "saved_utc": rec.get("saved_utc")}
    except (OSError, ValueError) as exc:
        return {"codes": {}, "found": False, "error": str(exc)}


@app.route("/api/review_index")
def api_review_index():
    """Distinct (participant, stimulus) recordings available for replay."""
    df = _load_gaze_table()
    if df is None or df.empty:
        return {"recordings": []}
    if "session_id" not in df.columns:
        df = df.assign(session_id="legacy")
    df["session_id"] = df["session_id"].fillna("legacy")
    pairs = (
        df[["participant_id", "session_id", "stimulus_name"]]
        .drop_duplicates()
        .to_dict("records")
    )
    return {"recordings": pairs}


@app.route("/api/gaze")
def api_gaze():
    """Gaze samples for one recording, in video-relative coordinates."""
    participant = request.args.get("participant", "")
    stimulus = request.args.get("stimulus", "")
    session = request.args.get("session", "")
    df = _load_gaze_table()
    if df is None:
        return {"points": [], "error": "no data file yet"}
    df = df[
        (df["participant_id"].astype(str) == participant)
        & (df["stimulus_name"] == stimulus)
    ]
    # Filter to ONE session — otherwise several runs of the same
    # participant+stimulus would interleave and the overlay would jump
    # between different gaze trajectories.
    if session and "session_id" in df.columns:
        df = df[df["session_id"].fillna("legacy").astype(str) == session]
    if df.empty:
        return {"points": [], "error": "no samples for this recording"}
    if "gaze_video_nx" not in df.columns or df["gaze_video_nx"].isna().all():
        return {
            "points": [],
            "error": "recording predates normalized coordinates — "
                     "re-run the experiment to enable the overlay",
        }
    pts = [
        {
            "t": round(float(r.video_time_s), 3),
            "nx": float(r.gaze_video_nx),
            "ny": float(r.gaze_video_ny),
        }
        for r in df.itertuples()
        if pd.notna(r.gaze_video_nx) and pd.notna(r.gaze_video_ny)
    ]
    return {"points": pts, "error": None}


@app.route("/api/session_quality")
def api_session_quality():
    """Everything known about a recording's measurement quality.

    Combines the session manifest (validations, drift, gain correction,
    preregistered thresholds, Tobii-style data loss) with metrics
    computed from the recording itself (sampling rate, fixations,
    off-video share). Rendered as the Quality panel in the review tool.
    """
    participant = request.args.get("participant", "")
    stimulus = request.args.get("stimulus", "")
    session_id = request.args.get("session", "")
    out: dict[str, Any] = {"session": session_id, "stimulus": stimulus}

    # ── Manifest-based: validations, drift, correction, thresholds ──
    mpath = os.path.join(GAZEFOLLOWER_CSV_DIR, session_id + "_manifest.json")
    if session_id and os.path.isfile(mpath):
        try:
            with open(mpath, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            manifest = None
        if manifest:
            vals = []
            for v in manifest.get("validations", []):
                vals.append({
                    "phase": v.get("phase"),
                    "recorded_at_utc": v.get("recorded_at_utc"),
                    "mean_err_px": v.get("mean_err_px"),
                    "mean_err_deg": v.get("mean_err_deg"),
                    "mean_precision_px": v.get("mean_precision_px"),
                    "mean_precision_deg": v.get("mean_precision_deg"),
                    "targets_measured": v.get("targets_measured"),
                    "passes_threshold": v.get("passes_threshold"),
                    "correction_active":
                        (v.get("correction_active") or {}).get("active"),
                    "mean_err_deg_raw": v.get("mean_err_deg_raw"),
                    "n_targets": len(v.get("targets") or []),
                    "screen_space": v.get("screen_space"),
                    # Carried through so the summary can stop saying
                    # "(assumed)" over a distance that was measured.
                    "distance": v.get("distance"),
                    "mean_err_deg_measured": v.get("mean_err_deg_measured"),
                    "sampled_at_hz": v.get("sampled_at_hz"),
                })
            out["validations"] = vals
            # The distance that was actually in force. Reported at the
            # top level because it applies to the whole session's degree
            # figures, not to one validation — and because the review
            # summary previously printed "(assumed)" unconditionally,
            # which is the single line a researcher reads to decide
            # whether a session is usable. Telling them an assumption was
            # made when it was in fact measured is the wrong error to
            # make in that sentence.
            _dists = [v.get("distance") for v in vals
                      if (v.get("distance") or {}).get("cm")]
            if _dists:
                out["distance"] = _dists[-1]
                out["distance_measured"] = True
            else:
                out["distance_measured"] = False
                out["distance_reason"] = ((vals[-1].get("distance") or {})
                                          .get("reason") if vals else None)
            pre = [v for v in vals if str(v["phase"]).startswith("pre")
                   and v["mean_err_deg"] is not None]
            post = [v for v in vals if v["phase"] == "post"
                    and v["mean_err_deg"] is not None]
            if pre and post:
                # Prefer the correction-free comparison: the pre-check is
                # measured raw and the post-check with the correction
                # applied, so differencing the two REPORTED numbers
                # conflates drift with the correction's effect (and the
                # correction was fitted on the pre targets). Fall back to
                # the reported numbers only when the raw pair is missing,
                # and say which basis was used.
                a, b = pre[-1], post[-1]
                if a.get("mean_err_deg_raw") is not None \
                        and b.get("mean_err_deg_raw") is not None:
                    out["drift_deg"] = round(
                        b["mean_err_deg_raw"] - a["mean_err_deg_raw"], 2)
                    out["drift_basis"] = "uncorrected"
                else:
                    out["drift_deg"] = round(
                        b["mean_err_deg"] - a["mean_err_deg"], 2)
                    out["drift_basis"] = "as-reported (mixed correction)"
                # Drift is only interpretable when both phases used the
                # same target geometry.
                out["drift_comparable"] = bool(
                    a.get("n_targets") and a["n_targets"] == b.get("n_targets"))
            out["gain_correction"] = manifest.get("gain_correction")
            out["rate_gate"] = manifest.get("rate_gate")
            out["quality_thresholds"] = manifest.get("quality_thresholds")
            out["data_quality"] = (
                manifest.get("data_quality") or {}).get(stimulus)
            scr = next((v.get("screen") for v in
                        manifest.get("validations", [])
                        if v.get("screen")), None)
            out["screen"] = scr

    # ── Recording-based: rate, fixations, off-video ──
    df = _load_gaze_table()
    if df is not None:
        df = df[
            (df["participant_id"].astype(str) == participant)
            & (df["stimulus_name"] == stimulus)
        ]
        if session_id and "session_id" in df.columns:
            df = df[df["session_id"].fillna("legacy").astype(str)
                    == session_id]
        if not df.empty and "gaze_video_nx" in df.columns:
            dfv = df.dropna(subset=["gaze_video_nx", "gaze_video_ny"])
            duration = float(df["video_time_s"].max())
            # Effective rate from the median inter-sample interval (not
            # samples/duration, which is diluted by dropouts).
            import numpy as np

            from fixations import detect_fixations_df, effective_sampling_hz

            hz = round(effective_sampling_hz(
                df["video_time_s"].astype(float).tolist()), 1)
            rec: dict[str, Any] = {
                "n_samples": int(len(df)),
                "duration_s": round(duration, 1),
                "rate_hz": hz or (round(len(df) / duration, 1)
                                  if duration > 0 else None),
                "low_sampling_rate": bool(hz and hz < MIN_SAMPLING_HZ),
                "min_sampling_hz": MIN_SAMPLING_HZ,
            }
            # Rate shape: a recording that alternates between full and
            # decimated rate is NOT described by a single Hz. Report the
            # fastest sustained rate next to the median so the gap is
            # visible, and flag the multimodal case.
            gaps = np.diff(np.sort(df["video_time_s"].astype(float).values))
            gaps = gaps[gaps > 0]
            if len(gaps) >= 10:
                dt_med = float(np.median(gaps))
                dt_fast = float(np.quantile(gaps, 0.10))
                rec["rate_hz_fastest"] = round(1 / dt_fast, 1) \
                    if dt_fast > 0 else None
                rec["rate_ratio"] = round(dt_med / dt_fast, 2) \
                    if dt_fast > 0 else None
                rec["rate_multimodal"] = bool(
                    dt_fast > 0
                    and dt_med / dt_fast >= RATE_MULTIMODAL_RATIO)
                # Fixation onset/offset quantization: durations can only
                # be resolved to ±1 sample interval.
                rec["sample_interval_ms"] = int(round(1000 * dt_med))
            if len(dfv):
                off = dfv[
                    (dfv["gaze_video_nx"] < 0) | (dfv["gaze_video_nx"] > 1)
                    | (dfv["gaze_video_ny"] < 0) | (dfv["gaze_video_ny"] > 1)
                ]
                rec["off_video_pct"] = round(100 * len(off) / len(dfv), 1)
                try:
                    fxs = detect_fixations_df(dfv)
                    if fxs:
                        durs = sorted(f.duration for f in fxs)
                        rec["n_fixations"] = len(fxs)
                        rec["median_fixation_ms"] = int(
                            1000 * durs[len(durs) // 2])
                        # Duration quantization from the sampling rate
                        rec["fixation_ms_uncertainty"] = int(
                            1000 * fxs[0].dt_s)
                        rec["time_in_fixations_pct"] = round(
                            100 * sum(durs) / duration, 1) \
                            if duration > 0 else None
                except Exception:
                    logger.exception("Fixation stats failed")
            out["recording"] = rec

    return out


# ---- LLM feedback (proof of concept) ---------------------------------------

_REGION_LABELS = [
    ["top-left", "top-centre", "top-right"],
    ["middle-left", "centre", "middle-right"],
    ["bottom-left", "bottom-centre", "bottom-right"],
]


def _gaze_region_stats(df: pd.DataFrame) -> dict:
    """Attention distribution over a 3×3 grid of the video frame.

    Preferred basis: FIXATION DWELL TIME — raw samples conflate
    saccades and tracking noise with attention, whereas dwell time per
    region is the standard attention measure. Falls back to raw-sample
    proportions when too few fixations are detected.
    """
    total = len(df)
    stats: dict[str, Any] = {}
    off = df[
        (df["gaze_video_nx"] < 0) | (df["gaze_video_nx"] > 1)
        | (df["gaze_video_ny"] < 0) | (df["gaze_video_ny"] > 1)
    ]
    stats["off-video (letterbox/outside)"] = round(100 * len(off) / total, 1)

    # ── Fixation-based dwell time per region ──
    try:
        from fixations import detect_fixations_df

        fixations = detect_fixations_df(df)
    except Exception:
        fixations = []
    if len(fixations) >= 3:
        total_dwell = sum(f.duration for f in fixations)
        for f in fixations:
            row = min(2, max(0, int(f.ny * 3)))
            col = min(2, max(0, int(f.nx * 3)))
            label = _REGION_LABELS[row][col]
            stats[label] = stats.get(label, 0.0) + f.duration
        for label in list(stats):
            if label != "off-video (letterbox/outside)":
                stats[label] = round(100 * stats[label] / total_dwell, 1)
        stats["basis"] = "% of fixation dwell time"
        stats["n_fixations"] = len(fixations)
        stats["median_fixation_ms"] = int(
            1000 * sorted(f.duration for f in fixations)[len(fixations) // 2]
        )
        return stats

    # ── Fallback: raw-sample proportions ──
    on = df.drop(off.index)
    for r in on.itertuples():
        row = min(2, max(0, int(r.gaze_video_ny * 3)))
        col = min(2, max(0, int(r.gaze_video_nx * 3)))
        label = _REGION_LABELS[row][col]
        stats[label] = stats.get(label, 0) + 1
    for label in list(stats):
        if label != "off-video (letterbox/outside)":
            stats[label] = round(100 * stats[label] / total, 1)
    stats["basis"] = "% of raw gaze samples (too few fixations detected)"
    return stats


# Degree conversion for the event metrics. Derived once from the same
# geometry helper the metrics spec uses, so px<->deg never disagrees
# between the spec, the AOI feasibility maths and the manifest.
try:
    from metrics_spec import SCREEN_H_PX as SCREEN_H_PX_FOR_DEG
    from metrics_spec import SCREEN_W_PX as SCREEN_W_PX_FOR_DEG
    from metrics_spec import px_per_degree as _ppd_fn

    _PX_PER_DEG = _ppd_fn()
except Exception:  # noqa: BLE001
    SCREEN_W_PX_FOR_DEG, SCREEN_H_PX_FOR_DEG, _PX_PER_DEG = 1920, 1080, None


def _event_metrics(segment: pd.DataFrame) -> dict:
    """RQ2 event metrics for one recorded stimulus, for the manifest.

    Everything here is reported WITH the caveat that makes it
    interpretable, because at 21-32 Hz the numbers are not
    self-explanatory:

      * fixation_count is biased DOWN and duration UP, because a saccade
        falling entirely between two samples is invisible and I-DT then
        merges the fixations either side of it. fixation_rate_per_s is
        included as the honest indicator — natural viewing is 3-4 /s.
      * idt_min_duration_s is recorded next to the three-sample floor it
        was checked against, so the parameter choice is auditable.
      * saccade amplitude is a displacement between fixation centroids.
        Velocity and duration are NOT recoverable at this rate.
    """
    from fixations import (PREFERRED_MIN_DURATION_S, detect_fixations,
                           effective_sampling_hz, saccade_metrics)

    out: dict = {}
    if "video_time_s" not in segment.columns \
            or "gaze_video_nx" not in segment.columns:
        return {"error": "segment lacks video-normalised coordinates"}

    times = segment["video_time_s"].tolist()
    xs = segment["gaze_video_nx"].tolist()
    ys = segment["gaze_video_ny"].tolist()
    span = (max(times) - min(times)) if len(times) > 1 else 0.0
    hz = effective_sampling_hz(times)

    fx = detect_fixations(times, xs, ys)
    durs = sorted(f.duration for f in fx)
    out["sampling_hz_empirical"] = round(hz, 1)
    out["fixation_count"] = len(fx)
    out["fixation_rate_per_s"] = round(len(fx) / span, 2) if span > 0 else None
    if durs:
        out["fixation_duration_median_ms"] = int(1000 * durs[len(durs) // 2])
        out["fixation_duration_uncertainty_ms"] = int(1000 * fx[0].dt_s)
        out["time_in_fixations_pct"] = round(100 * sum(durs) / span, 1) \
            if span > 0 else None
    # The I-DT parameters actually used, and the floor they respect.
    out["idt_min_duration_s"] = round(
        max(PREFERRED_MIN_DURATION_S, 3.0 / hz) if hz else
        PREFERRED_MIN_DURATION_S, 3)
    out["idt_min_duration_floor_s"] = round(3.0 / hz, 3) if hz else None
    out["idt_dispersion_norm"] = FIXATION_DISPERSION_NORM
    out["idt_dispersion_deg"] = round(
        FIXATION_DISPERSION_NORM * SCREEN_W_PX_FOR_DEG / _PX_PER_DEG, 2) \
        if _PX_PER_DEG else None

    # Gaze that left the video frame: the model cannot describe what was
    # attended there, so it bounds every RQ3 feedback claim.
    n = len(segment)
    if n:
        off = segment[(segment["gaze_video_nx"] < 0)
                      | (segment["gaze_video_nx"] > 1)
                      | (segment["gaze_video_ny"] < 0)
                      | (segment["gaze_video_ny"] > 1)]
        out["gaze_off_video_pct"] = round(100.0 * len(off) / n, 1)

    if len(fx) >= 2 and _PX_PER_DEG:
        out["saccades"] = saccade_metrics(
            [{"nx": f.nx, "ny": f.ny} for f in fx],
            _PX_PER_DEG, SCREEN_W_PX_FOR_DEG, SCREEN_H_PX_FOR_DEG)
    out["interpretation"] = (
        "fixation_count is biased DOWN and duration UP at this sampling "
        "rate (merged fixations); report fixation_rate_per_s alongside "
        "them. Saccade velocity/duration are not measurable here.")
    return out


def _fixation_summary(df: pd.DataFrame) -> dict:
    """Grid-free attention metrics for the LLM prompt.

    Standard eye-tracking measures with no arbitrary spatial binning
    (used in the MAIN pipeline — the keyframes already tell the model
    WHERE the gaze was on the actual content, so no 3×3 grid is needed
    and its boundary artifacts are avoided): fixation count, median
    fixation duration, share of time spent in fixations, and share of
    gaze off the video, for the whole video and per third.
    """
    duration = float(df["video_time_s"].max())
    try:
        from fixations import detect_fixations_df

        fixations = detect_fixations_df(df)
    except Exception:
        fixations = []

    def _stats(fxs: list, seg: pd.DataFrame, span_s: float) -> dict:
        out: dict[str, Any] = {"n_fixations": len(fxs)}
        if fxs:
            durs = sorted(f.duration for f in fxs)
            out["median_fixation_ms"] = int(1000 * durs[len(durs) // 2])
            out["time_in_fixations_pct"] = round(
                100 * sum(durs) / span_s, 1) if span_s > 0 else 0.0
        if len(seg):
            off = seg[
                (seg["gaze_video_nx"] < 0) | (seg["gaze_video_nx"] > 1)
                | (seg["gaze_video_ny"] < 0) | (seg["gaze_video_ny"] > 1)
            ]
            out["gaze_off_video_pct"] = round(100 * len(off) / len(seg), 1)
        return out

    summary: dict[str, Any] = {
        "video_duration_s": round(duration, 1),
        "whole_video": _stats(fixations, df, duration),
    }
    for i, name in enumerate(("beginning_third", "middle_third",
                              "end_third")):
        lo, hi = duration * i / 3, duration * (i + 1) / 3
        fxs = [f for f in fixations if lo <= f.t_mid < hi + 0.001]
        seg = df[(df["video_time_s"] >= lo)
                 & (df["video_time_s"] < hi + 0.001)]
        summary[name] = _stats(fxs, seg, duration / 3)
    return summary


def _log_llm_call(meta: dict) -> None:
    """Append an audit-trail record of one LLM request/response.

    Every call that could end up in the thesis is logged: pinned model,
    parameters, all text parts, image references (timestamp + SHA-256,
    not base64 — keeps files reviewable), and the raw response text.
    """
    try:
        os.makedirs(LLM_LOG_DIR, exist_ok=True)
        fname = "%d_%s.json" % (
            time.time_ns(), _safe_filename(meta.get("step", "call")))
        with open(os.path.join(LLM_LOG_DIR, fname), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, ensure_ascii=False)
    except OSError:
        logger.exception("Could not write LLM log")


def _loggable_parts(parts: list) -> list:
    """Replace inline image data with hashes for the audit log."""
    out = []
    for p in parts:
        if "inline_data" in p:
            data = p["inline_data"].get("data", "")
            out.append({
                "image_sha256": hashlib.sha256(
                    data.encode("ascii")).hexdigest(),
                "bytes_b64": len(data),
            })
        else:
            out.append(p)
    return out


def _call_gemini(api_key: str, parts: "str | list",
                 step: str = "call", context: "dict | None" = None,
                 max_tokens: int = 4000) -> str:
    """Google Gemini API call against the PINNED model (stdlib only).

    The model is pinned via ``config.GEMINI_MODEL`` — a methods thesis
    must state exactly which model produced its results, so there is no
    rolling-alias fallback. temperature 0: as deterministic as the API
    allows. Every request/response is logged to ``data/llm_logs/``.
    """
    if isinstance(parts, str):
        parts = [{"text": parts}]

    generation_config = {
        "maxOutputTokens": max_tokens,
        "temperature": 0,
    }
    # Thinking models spend their token budget on internal reasoning,
    # which truncates the visible answer (and destroys the trailing
    # ```json block). Disable thinking; fall back to a plain config for
    # models that reject the thinkingConfig field.
    body_fast = {
        "contents": [{"parts": parts}],
        "generationConfig": dict(
            generation_config, thinkingConfig={"thinkingBudget": 0}),
    }
    body_plain = {
        "contents": [{"parts": parts}],
        "generationConfig": generation_config,
    }

    data = None
    used_config = None
    for body in (body_fast, body_plain):
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-goog-api-key": api_key,
                "content-type": "application/json",
            },
        )
        try:
            # Scale with the payload. A constant was already marginal
            # at 60 frames (one request timed out and succeeded on
            # retry) and 200 frames is several times the upload.
            _n_imgs = sum(1 for p in (parts if isinstance(parts, list) else [])
                          if isinstance(p, dict)
                          and ("inline_data" in p or "inlineData" in p))
            _timeout = LLM_TIMEOUT_BASE_S + LLM_TIMEOUT_PER_FRAME_S * _n_imgs
            with urllib.request.urlopen(req, timeout=_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            used_config = body["generationConfig"]
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(
                    "Pinned Gemini model '%s' is unavailable (404). Set "
                    "the GEMINI_MODEL env var to an available model (see "
                    "/llm_check) — and record the change in the methods "
                    "section." % GEMINI_MODEL
                ) from exc
            if exc.code == 400 and body is body_fast:
                logger.warning("Gemini rejected thinkingConfig (400) — "
                               "retrying with plain config")
                continue
            raise
    if data is None:
        raise RuntimeError("Gemini request failed for all configs")

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(
            "Gemini returned no candidates: %s" % str(data)[:200]
        )
    out_parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in out_parts).strip()
    finish_reason = candidates[0].get("finishReason")
    if finish_reason == "MAX_TOKENS":
        logger.warning("Gemini output TRUNCATED (MAX_TOKENS) — step=%s",
                       step)
        text += ("\n\n*(Output was truncated by the token limit — "
                 "treat this run as incomplete and regenerate.)*")

    _log_llm_call({
        "step": step,
        "model": GEMINI_MODEL,
        "generation_config": used_config,
        "finish_reason": finish_reason,
        "requested_at_utc": datetime.now(timezone.utc).isoformat(),
        "context": context or {},
        "request_parts": _loggable_parts(parts),
        "response_text": text,
    })
    logger.info("Gemini call '%s' completed (model %s)", step, GEMINI_MODEL)
    return text


def _session_validation_error(session_id: str) -> "dict | None":
    """Measured accuracy of a session from its manifest (pre-validation).

    Returns ``{"mean_err_px": …, "mean_err_deg": …}`` or None. Used to
    scale the gaze-marker radius and to tell the LLM the ACTUAL
    measurement uncertainty instead of a hardcoded guess.
    """
    if not session_id:
        return None
    path = os.path.join(GAZEFOLLOWER_CSV_DIR, session_id + "_manifest.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return None
    validations = [v for v in (manifest.get("validations") or [])
                   if v.get("mean_err_px") is not None]
    if not validations:
        return None
    # The workbook data is written WITH the gain correction applied, so
    # the uncertainty must come from a validation measured under the
    # same conditions: prefer the most recent record with the correction
    # active; fall back to the most recent 'pre'; then most recent any.
    def _ts(v: dict) -> str:
        return v.get("recorded_at_utc", "")

    corrected = [v for v in validations
                 if (v.get("correction_active") or {}).get("active")]
    pre = [v for v in validations if v.get("phase") == "pre"]
    pool = corrected or pre or validations
    chosen = max(pool, key=_ts)
    return {
        "mean_err_px": float(chosen["mean_err_px"]),
        "mean_err_deg": chosen.get("mean_err_deg"),
    }


def _extract_structured(text: str) -> "list | dict | None":
    """Parse the ```json …``` block the evaluation prompt requests."""
    import re as _re

    m = _re.search(r"```json\s*(.*?)```", text, _re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _consistency(runs: list[dict]) -> "dict | None":
    """Simple agreement metric across repeated runs.

    Compares the ``criteria_met`` booleans of structurally parsed runs,
    phase by phase. Returns None when fewer than 2 runs parsed.
    """
    parsed = [r["structured"] for r in runs
              if isinstance(r.get("structured"), list)]
    if len(parsed) < 2:
        return None
    n_phases = min(len(p) for p in parsed)
    if n_phases == 0:
        return None
    agree = 0
    for i in range(n_phases):
        vals = {bool(p[i].get("criteria_met")) for p in parsed
                if isinstance(p[i], dict) and "criteria_met" in p[i]}
        if len(vals) == 1:
            agree += 1
    return {
        "runs_parsed": len(parsed),
        "phases_compared": n_phases,
        "criteria_agreement_pct": round(100 * agree / n_phases, 1),
    }


@app.route("/api/llm_feedback", methods=["POST"])
def api_llm_feedback():
    """Content-aware LLM feedback on a recording (research pipeline).

    Design (state of the art, mid-2026):
    * Keyframes at DETECTED FIXATIONS (I-DT), not fixed intervals.
    * Dual granularity: full annotated frame + zoomed crop per keyframe.
    * PROMPT CHAIN: step 1 describes the scene content (no gaze) —
      independently checkable against hallucination; step 2 evaluates
      the gaze against the rubric.
    * Structured JSON output next to the prose (validatable vs. human
      coders: Cohen's kappa on ``criteria_met``).
    * Optional repeated generation (``n_runs``) with a consistency
      metric.
    * The marker radius and the uncertainty stated to the model use the
      session's MEASURED validation error.
    * Pinned model + full request/response audit log.

    The API key is used for this one request only and never stored.
    """
    payload = request.get_json(silent=True) or {}
    # Key priority: pasted in the UI > stored default (.gemini_key / env)
    api_key = (payload.get("api_key") or "").strip() or GEMINI_API_KEY
    participant = payload.get("participant", "")
    stimulus = payload.get("stimulus", "")
    session = payload.get("session", "")
    n_runs = max(1, min(int(payload.get("n_runs") or 1), LLM_N_RUNS_MAX))
    chain = payload.get("chain", True)
    # Output granularity.
    #   "phases"    model-chosen groupings (readable narrative feedback)
    #   "fixations" one entry per fixation keyframe
    #   "windows"   FIXED time bins — the unit is decided by us, not the
    #               model. Use this for the agreement analysis: when the
    #               model picks its own boundaries, a disagreement between
    #               two models (or between a model and a human coder)
    #               confounds SEGMENTATION with JUDGMENT, and kappa stops
    #               being interpretable. Fixed bins make the units
    #               identical across every rater, which is what kappa
    #               assumes.
    detail = payload.get("detail", "phases")
    if detail not in ("phases", "fixations", "windows"):
        detail = "phases"
    # Bin length for "windows" mode. 5 s is a compromise: long enough that
    # a rater can say what was attended, short enough that a 30 s clip
    # yields 6 codable units rather than 1.
    window_s = float(payload.get("window_s") or LLM_WINDOW_SECONDS)
    n_bins = None            # set in "windows" mode below
    if not api_key:
        return {
            "feedback": None,
            "error": "No API key — paste one, or store a default in the "
                     ".gemini_key file / GEMINI_API_KEY env var.",
        }, 400

    df = _load_gaze_table()
    if df is None:
        return {"feedback": None, "error": "No gaze data recorded yet."}, 404
    df = df[
        (df["participant_id"].astype(str) == participant)
        & (df["stimulus_name"] == stimulus)
    ]
    if session and "session_id" in df.columns:
        df = df[df["session_id"].fillna("legacy").astype(str) == session]
    if df.empty or "gaze_video_nx" not in df.columns:
        return {
            "feedback": None,
            "error": "No usable samples for this recording.",
        }, 404
    df = df.dropna(subset=["gaze_video_nx", "gaze_video_ny"])
    if df.empty:
        return {"feedback": None, "error": "No valid gaze samples."}, 404

    duration = float(df["video_time_s"].max())
    rubric = (payload.get("rubric") or "").strip()

    # ── Measured per-session accuracy (manifest) — used for the marker
    # radius and the uncertainty statement in the prompt. ──
    val = _session_validation_error(session)
    error_px = val["mean_err_px"] if val else None
    if val and val.get("mean_err_deg") is not None:
        uncertainty_text = (
            "measured gaze uncertainty for THIS recording: "
            "±%.0f px (≈%.1f° of visual angle)"
            % (val["mean_err_px"], val["mean_err_deg"])
        )
    elif val:
        uncertainty_text = (
            "measured gaze uncertainty for THIS recording: ±%.0f px"
            % val["mean_err_px"]
        )
    else:
        uncertainty_text = (
            "no per-session validation available; assume roughly "
            "±50–100 px error (typical for webcam eye tracking)"
        )

    # ── Fixation-based keyframes: annotated (for evaluation) and clean
    # (for the scene-description chain step). ──
    frames: list = []
    clean_frames: list = []
    video_file = os.path.join(STIMULI_DIR, stimulus)
    if os.path.isfile(video_file):
        try:
            from gaze_vision import sample_gaze_frames

            frames = sample_gaze_frames(
                video_file, df, max_frames=LLM_MAX_FRAMES,
                error_px=error_px, with_crops=True,
            )
            logger.info(
                "Prepared %d keyframes (%s) for LLM feedback",
                len(frames), frames[0]["method"] if frames else "-",
            )
            if chain and frames:
                clean_frames = sample_gaze_frames(
                    video_file, df, max_frames=LLM_MAX_FRAMES,
                    error_px=error_px, with_crops=False,
                    draw_marker=False,
                )
        except Exception:
            logger.exception("Frame annotation failed — stats-only feedback")

    # Every fixation in the recording, so the number the model SAW can
    # be compared against the number that exist. The gap is what makes
    # the tail of a long recording unexplainable.
    _all_fix_times = []
    try:
        from fixations import detect_fixations_df

        _all_fix_times = [round(f.t_mid, 1) for f in detect_fixations_df(df)]
    except Exception:  # noqa: BLE001
        _all_fix_times = []
    if _all_fix_times and frames and len(_all_fix_times) > len(frames):
        logger.warning(
            "LLM saw %d of %d fixations (LLM_MAX_FRAMES=%d). "
            "sample_gaze_frames keeps the LONGEST fixations, so the "
            "%d dropped are the SHORTEST — any claim about them is "
            "unfounded, and the coding tool must not present them.",
            len(frames), len(_all_fix_times), LLM_MAX_FRAMES,
            len(_all_fix_times) - len(frames))

    log_ctx = {"participant": participant, "stimulus": stimulus,
               "session": session, "rubric": rubric, "n_runs": n_runs,
               "detail": detail}
    # Per-fixation output is much longer (one line + one JSON object per
    # keyframe) — give it a correspondingly larger output budget.
    out_tokens = 8000 if detail in ("fixations", "windows") else 4000

    # ── Chain step 1: scene descriptions WITHOUT gaze markers.
    # Serves two purposes: grounding for step 2, and an independent
    # hallucination check (objects named here must exist in the video).
    scene_description = ""
    if clean_frames:
        parts1: list = [{"text": (
            "You are assisting an eye-tracking researcher. Below are "
            f"keyframes from the video stimulus '{stimulus}' "
            f"({duration:.1f} s). For EACH frame, describe in 1-2 short "
            "sentences what is visible (people, objects, text, layout). "
            "Use the format 't=X s: description'. Do not speculate about "
            "anything not visible."
        )}]
        for fr in clean_frames:
            parts1.append({"text": "Frame at t=%.1f s:" % fr["t"]})
            parts1.append({"inline_data": {
                "mime_type": "image/jpeg", "data": fr["b64"],
            }})
        try:
            scene_description = _call_gemini(
                api_key, parts1, step="scene_description", context=log_ctx)
        except Exception:
            logger.exception("Scene-description step failed — continuing")

    # ── Chain step 2 (or single step): gaze evaluation ──
    parts: list = []
    if frames:
        fixation_based = frames[0]["method"] == "fixation"
        parts.append({"text": (
            "You are assisting an eye-tracking researcher. A participant "
            f"(ID: {participant}) watched the video stimulus '{stimulus}' "
            f"({duration:.1f} s, {len(df)} gaze samples). Below are "
            + ("keyframes captured at DETECTED FIXATIONS (sustained "
               "attention); each label gives the fixation duration."
               if fixation_based else
               "frames sampled across the video.")
            + " On each full frame, a RED CIRCLE WITH A WHITE RING marks "
            "where the participant was looking; the circle radius "
            "reflects the measurement uncertainty (" + uncertainty_text +
            "). After each full frame, a ZOOMED CROP of the region "
            "around the gaze point is provided — use it to identify the "
            "attended object precisely. Frames labelled 'gaze off-video' "
            "mean the participant looked outside the video; 'no gaze "
            "data' means tracking dropped out briefly."
        )})
        for fr in frames:
            label = "Frame at t=%.1f s — %s" % (fr["t"], fr["status"])
            if fr.get("duration_s"):
                label += " (%.0f ms)" % (fr["duration_s"] * 1000)
            parts.append({"text": label + ":"})
            parts.append({"inline_data": {
                "mime_type": "image/jpeg", "data": fr["b64"],
            }})
            if fr.get("crop_b64"):
                parts.append({"text": "Zoomed crop around the gaze point:"})
                parts.append({"inline_data": {
                    "mime_type": "image/jpeg", "data": fr["crop_b64"],
                }})

    if scene_description:
        parts.append({"text": (
            "Independent scene descriptions of the same keyframes "
            "(generated WITHOUT gaze information — use them as ground "
            "truth for what is present in each frame):\n"
            + scene_description
        )})

    if frames:
        # Main path: grid-free fixation metrics — the keyframes already
        # show WHERE the gaze was on the actual content, so no spatial
        # binning (and none of its boundary artifacts) is needed.
        stats_text = (
            "Fixation summary (dispersion-based fixation detection; "
            "standard attention metrics, whole video and per third): "
            + json.dumps(_fixation_summary(df))
        )
    else:
        # Fallback only (video unreadable): without images the model
        # needs SOME spatial vocabulary — a coarse 3×3 grid provides it.
        thirds = []
        for i, name in enumerate(("beginning", "middle", "end")):
            seg = df[
                (df["video_time_s"] >= duration * i / 3)
                & (df["video_time_s"] < duration * (i + 1) / 3 + 0.001)
            ]
            if not seg.empty:
                thirds.append((name, _gaze_region_stats(seg)))
        stats_text = (
            "Gaze region statistics (3x3 grid of the video frame — "
            "coarse; cells are larger than the measurement error), "
            f"whole video: {json.dumps(_gaze_region_stats(df))}\n"
            + "\n".join(
                f"{name.capitalize()} third: {json.dumps(s)}"
                for name, s in thirds
            )
        )

    criteria_text = (
        "Researcher's evaluation criteria: " + rubric
        if rubric else
        "No specific evaluation criteria were provided — give neutral, "
        "descriptive feedback."
    )

    if frames and detail == "windows":
        n_bins = max(1, int(round(duration / window_s)))
        task_text = (
            "\n\nYour tasks:\n"
            "1. The video is %.1f s long. Describe it in EXACTLY %d "
            "consecutive time windows of %.1f s each, starting at t=0. "
            "Do NOT choose your own boundaries, do not merge windows, and "
            "do not skip any — even if nothing changes between them, and "
            "even if the gaze was off-screen (say so for that window). "
            "The windows are fixed so that different raters describe the "
            "SAME units.\n"
            "2. For each window write ONE short sentence naming the "
            "general object/area under the gaze marker. Given the stated "
            "measurement uncertainty, name the general area rather than "
            "making over-precise claims.\n"
            "3. Close with 2-3 sentences evaluating the gaze behaviour "
            "against the researcher's criteria above.\n"
            "Format in simple Markdown. NEVER use LaTeX or math "
            "notation — write times plainly, e.g. 't=4.5 s'.\n"
            "4. AFTER the prose, output a machine-readable summary as a "
            "fenced code block starting with ```json — a JSON array of "
            "EXACTLY %d objects, one per window, in order: "
            "[{\"t_start\": <s>, \"t_end\": <s>, "
            "\"attended\": \"<object/area>\", "
            "\"bbox\": [x, y, w, h], "
            "\"criteria_met\": <true|false|null>, "
            "\"confidence\": \"<low|medium|high>\"}]. "
            "\"bbox\" is the region of the VIDEO FRAME occupied by the "
            "thing you named, in NORMALISED coordinates (0-1, origin "
            "top-left) — it is what makes the claim checkable against the "
            "recorded gaze; use null if you cannot localise it. Use "
            "criteria_met: null when no criteria were provided. The array "
            "MUST have exactly %d entries. The JSON block is REQUIRED — "
            "never omit it."
            % (duration, n_bins, window_s, n_bins, n_bins)
        )
    elif frames and detail == "fixations":
        task_text = (
            "\n\nYour tasks:\n"
            "1. For EVERY keyframe above (each one is a single fixation), "
            "write EXACTLY ONE short sentence in a bullet list:\n"
            "'t=<time> s (<duration> ms): <what the participant looked "
            "at>' — name the specific object/person/area under the gaze "
            "marker, using the zoomed crop to identify it. Given the "
            "stated measurement uncertainty, name the general "
            "object/area rather than making over-precise claims. Do not "
            "skip, merge, or reorder fixations.\n"
            "2. Close with 2-3 sentences evaluating the gaze behaviour "
            "against the researcher's criteria above.\n"
            "Format in simple Markdown. NEVER use LaTeX or math "
            "notation — write times plainly, e.g. 't=4.5 s'.\n"
            "3. AFTER the prose, output a machine-readable summary as a "
            "fenced code block starting with ```json — a JSON array with "
            "ONE object PER FIXATION, same order: [{\"t_start\": <s>, "
            "\"t_end\": <s>, \"attended\": \"<object/area>\", "
            "\"bbox\": [x, y, w, h], "
            "\"criteria_met\": <true|false|null>, \"confidence\": "
            "\"<low|medium|high>\"}] (use the fixation time for both "
            "t_start and t_end). \"bbox\" is the region of the VIDEO FRAME "
            "occupied by the thing you named, in NORMALISED "
            "coordinates (0-1, origin top-left) — it is what makes "
            "the claim checkable against the recorded gaze; use null "
            "if you cannot localise it. Use criteria_met: null when "
            "no criteria were provided. The JSON block is REQUIRED — "
            "never omit it."
        )
    elif frames:
        task_text = (
            "\n\nYour tasks:\n"
            "1. Describe WHAT the participant looked at over time (people, "
            "objects, areas of the scene), citing timestamps.\n"
            "2. Evaluate the participant's gaze behaviour according to the "
            "researcher's criteria above, referring to concrete moments.\n"
            "Given the stated measurement uncertainty, describe the "
            "general object/area under the marker rather than making "
            "over-precise claims. Summarize the gaze timeline as PHASES: "
            "group consecutive frames with similar targets into time "
            "ranges — AT MOST 8 PHASES for the whole video, even if that "
            "means merging brief glances into their surrounding phase. "
            "Do NOT list every frame or every fixation individually. "
            "Keep the prose under 300 words and make sure it ends with a "
            "complete concluding sentence. Format in simple Markdown "
            "(### headings, **bold**, bullet lists). NEVER use LaTeX or "
            "math notation — write times plainly, e.g. 't=4.5 s'.\n"
            "3. AFTER the prose, output a machine-readable summary as a "
            "fenced code block starting with ```json — a JSON array of "
            "the SAME phases (max 8): [{\"t_start\": <s>, \"t_end\": <s>, "
            "\"attended\": \"<object/area>\", "
            "\"bbox\": [x, y, w, h], "
            "\"criteria_met\": <true|false|null>, "
            "\"confidence\": \"<low|medium|high>\"}]. "
            "\"bbox\" is the region of the VIDEO FRAME occupied by the "
            "thing you named, in NORMALISED coordinates (0-1, origin "
            "top-left). It is what makes your claim checkable against "
            "the recorded gaze coordinates, so give your honest best "
            "estimate of the object's extent rather than a box around "
            "the marker. Use null if you cannot localise it. "
            "Use criteria_met: null when no criteria were provided. The "
            "JSON block is REQUIRED — never omit it."
        )
    else:
        task_text = (
            "\n\nNo video frames were available, so base your feedback on "
            "the region statistics alone. Write a concise plain-language "
            "summary (4-6 sentences) of where attention was directed and "
            "evaluate it against the criteria if any were given. Region "
            "statistics cannot identify specific objects — do not guess. "
            "Format in simple Markdown; never use LaTeX or math notation."
        )

    parts.append({"text": stats_text + "\n\n" + criteria_text + task_text})

    # ── Save the EXACT payload for multi-model comparison ────────────
    # A fair comparison across models requires byte-identical input. The
    # audit log stores images as SHA-256 references (deliberately, to stay
    # reviewable), so it cannot be replayed. This writes the full payload,
    # images included, to data/llm_replay/ for model_comparison.py.
    try:
        _replay_dir = os.path.join(DATA_DIR, "llm_replay")
        os.makedirs(_replay_dir, exist_ok=True)
        _replay_name = "%s__%s__%s.json" % (
            _safe_filename(participant or "unknown"),
            _safe_filename(os.path.splitext(stimulus)[0] or "stimulus"),
            detail)
        with open(os.path.join(_replay_dir, _replay_name), "w",
                  encoding="utf-8") as _fh:
            json.dump({
                "participant": participant,
                "stimulus": stimulus,
                "session": session,
                "detail": detail,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "reference_model": GEMINI_MODEL,
                "max_tokens": out_tokens,
                # The measured accuracy the marker radius and the prompt
                # were built from — claim_check needs it to pad boxes.
                "accuracy_deg": (val or {}).get("mean_err_deg"),
                "accuracy_px": (val or {}).get("mean_err_px"),
                # How many entries the JSON block MUST contain. In
                # "fixations" mode the units are defined by I-DT, not by
                # the model, so a short reply means the output was
                # TRUNCATED — and a truncated reply silently drops the
                # last fixations, biasing every downstream figure toward
                # the start of the video. Recorded so the comparison can
                # detect it instead of averaging over a partial answer.
                "expected_units": (len(frames) if detail == "fixations"
                                   else (n_bins if detail == "windows"
                                         else None)),
                "unit_definition": ("one per detected fixation (I-DT)"
                                    if detail == "fixations"
                                    else ("fixed %.1f s bins" % window_s
                                          if detail == "windows"
                                          else "model-chosen phases")),
                "parts": parts,
            }, _fh)
    except Exception:  # noqa: BLE001 — never block feedback generation
        logger.exception("Could not write LLM replay payload")

    try:
        runs: list[dict] = []
        for i in range(n_runs):
            text = _call_gemini(
                api_key, parts, step="evaluation_run_%d" % (i + 1),
                context=log_ctx, max_tokens=out_tokens)
            runs.append({
                "feedback": text,
                "structured": _extract_structured(text),
            })
        first = runs[0]
        # RQ3's primary outcome belongs in the session record, not in a
        # log directory. Until now the feedback was returned to the
        # browser and written to data/llm_logs/, so llm_model_id,
        # llm_claims_structured and claim_metric_correspondence all
        # reported MISSING — the operationalisation of RQ3 existed but
        # left no trace in the data a reader would be given.
        _persist_llm_result(session, stimulus, {
            "llm_model_id": GEMINI_MODEL,
            "detail": detail,
            "rubric": rubric or None,
            "n_runs": n_runs,
            "keyframe_method": frames[0]["method"] if frames else None,
            "frames_used": len(frames),
            # WHICH fixations the model actually saw.
            # sample_gaze_frames caps at LLM_MAX_FRAMES and, when there
            # are more fixations than that, keeps the LONGEST ones. So a
            # 71-fixation recording sends 60 frames and drops 11 — and
            # nothing downstream knew which 11. The coding tool showed
            # all 71 and invited a human to judge claims that were never
            # made about fixations the model never saw.
            #
            # Recording the timestamps makes the sampled set explicit
            # everywhere afterwards, and makes the gap visible instead
            # of leaving it to be discovered by a coder wondering why
            # the tail is nonsense.
            "frame_times": [f.get("t") for f in frames],
            "n_fixations_total": len(_all_fix_times) if _all_fix_times
            else None,
            "frames_dropped": (len(_all_fix_times) - len(frames))
            if _all_fix_times else None,
            "chained": bool(scene_description),
            "measured_error_px": error_px,
            "structured": first["structured"],
            "consistency": _consistency(runs) if n_runs > 1 else None,
            "requested_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        return {
            "feedback": first["feedback"],
            "structured": first["structured"],
            "runs": len(runs),
            "consistency": _consistency(runs) if n_runs > 1 else None,
            "all_runs": runs if n_runs > 1 else None,
            "error": None,
            "mode": "visual" if frames else "stats",
            "detail": detail,
            "keyframe_method": frames[0]["method"] if frames else None,
            "frames_used": len(frames),
            "model": GEMINI_MODEL,
            "chained": bool(scene_description),
            "measured_error_px": error_px,
        }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        logger.warning("Gemini API error %s: %s", exc.code, detail)
        return {
            "feedback": None,
            "error": f"API error {exc.code}: {detail}",
        }, 502
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM feedback failed")
        return {"feedback": None, "error": str(exc)}, 502


@app.route("/llm_check")
def llm_check():
    """List the Gemini models available to the stored API key.

    Runs locally (the key never appears in a browser URL). Use this
    when the pinned model returns 404: pick a listed model, set it via
    the GEMINI_MODEL env var, and record the change in the methods
    section.
    """
    if not GEMINI_API_KEY:
        return {"pinned": GEMINI_MODEL, "models": None,
                "error": "No API key stored (.gemini_key / "
                         "GEMINI_API_KEY)."}, 400
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models"
        "?pageSize=100",
        headers={"x-goog-api-key": GEMINI_API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"pinned": GEMINI_MODEL, "models": None,
                "error": str(exc)}, 502
    models = sorted(
        m["name"].removeprefix("models/")
        for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    )
    return {
        "pinned": GEMINI_MODEL,
        "pinned_available": GEMINI_MODEL in models,
        "models": models,
    }


@app.route("/tracker_check")
def tracker_check():
    """JSON diagnosis of the GazeFollower tracker environment.

    Open http://localhost:5050/tracker_check in a browser (or check
    data/server.log) to see exactly which dependency or camera step is
    broken when calibration fails.
    """
    result = gaze_service.self_check()
    logger.info("Tracker self-check: %s", result)
    return result


@app.route("/complete")
def complete():
    """Study-complete page.  Clears the session so the browser slot can
    be reused by another participant."""
    session.clear()
    return render_template("complete.html")


# =========================================================================
# SocketIO helpers
# =========================================================================

def _get_session_state(sid: str) -> dict[str, Any]:
    """Return (or lazily create) the per-connection state dict."""
    with _sessions_lock:
        if sid not in active_sessions:
            active_sessions[sid] = {
                "participant_id": None,
                "recording": False,
                # Per-stimulus timing windows for post-hoc segmentation
                # of the continuous GazeFollower recording:
                # [{stimulus, t_start_ns, t_end_ns}, …]
                "stimulus_log": [],
                # Mandatory accuracy validations (pre/post), reported by
                # the browser: [{phase, mean_err_px, mean_err_deg, …}, …]
                "validations": [],
                "gazefollower_active": False,
                "finalized": False,
                "telemetry": None,
            }
        return active_sessions[sid]


def _start_telemetry(sid: str, participant_id: str) -> None:
    """Begin background recording of session context.

    Started once per connection, as early as the participant is known.
    Everything it captures is context for interpreting the gaze data
    later — see telemetry.py for why this exists at all.
    """
    state = _get_session_state(sid)
    if state.get("telemetry") is not None:
        return
    try:
        import telemetry as _tel

        if not _tel.enabled():
            return
        rec = _tel.Telemetry(
            session_id=participant_id or sid,
            probe=gaze_service.telemetry,
            extra={"sid": sid, "participant_id": participant_id},
        ).start()
        state["telemetry"] = rec
        logger.info("Telemetry recording started – participant=%s",
                    participant_id)
    except Exception:  # noqa: BLE001 — never block a session
        logger.exception("Telemetry could not be started (continuing)")


def _telemetry_event(sid: str, name: str, **fields) -> None:
    """Add an event to the session timeline. Always safe."""
    try:
        rec = _get_session_state(sid).get("telemetry")
        if rec is not None:
            rec.event(name, **fields)
    except Exception:  # noqa: BLE001
        pass


def _finish_telemetry(sid: str) -> "tuple[str | None, dict | None]":
    """Stop sampling and write the file. Returns (path, summary)."""
    try:
        rec = _get_session_state(sid).get("telemetry")
        if rec is None:
            return None, None
        import telemetry as _tel

        rec.event("session_finalised")
        rec.stop()
        path = rec.save()
        summary = _tel.summarise(rec.to_dict())
        if path:
            logger.info("Telemetry written: %s (%d samples, %d events)",
                        os.path.basename(path), summary.get("samples", 0),
                        summary.get("events", 0))
        return path, summary
    except Exception:  # noqa: BLE001
        logger.exception("Telemetry could not be saved (session unaffected)")
        return None, None


def _safe_filename(value: str) -> str:
    """Reduce *value* to a filesystem-safe token."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)


def _remove_session_state(sid: str) -> None:
    """Clean up state when a client disconnects."""
    with _sessions_lock:
        active_sessions.pop(sid, None)


# =========================================================================
# SocketIO events
# =========================================================================

@socketio.on("connect")
def handle_connect():
    """Initialise per-connection state when a client connects."""
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)
    # Propagate the Flask session participant_id if available
    pid = session.get("participant_id")
    if pid:
        state["participant_id"] = pid
    # Recording conditions live in the Flask session (set at login) but
    # the manifest is built from the socket state, so they have to be
    # carried across or they silently never reach the record.
    if session.get("conditions"):
        state["conditions"] = session["conditions"]
    logger.info("SocketIO connected: sid=%s, participant=%s", sid, pid)
    _start_telemetry(sid, pid)


def _finalize_session(sid: str, state: dict[str, Any]) -> dict[str, int]:
    """Save & segment the GazeFollower session recording (idempotent)."""
    if state.get("finalized") or not state.get("stimulus_log"):
        return {}
    state["finalized"] = True

    # Stop and write telemetry FIRST, so its summary is available to the
    # manifest built below and the file exists even if segmentation
    # later fails — a failed session is exactly when the context is
    # most worth having.
    tel_path, tel_summary = _finish_telemetry(sid)
    state["telemetry_file"] = os.path.basename(tel_path) if tel_path else None
    state["telemetry_summary"] = tel_summary

    participant_id = state.get("participant_id") or "unknown"
    os.makedirs(GAZEFOLLOWER_CSV_DIR, exist_ok=True)
    # Human-readable session name: <participant>_<date>_<time>. The
    # basename (without .csv) doubles as the session_id in the Excel
    # workbook, the manifest name, and the review tool.
    base = "%s_%s" % (
        _safe_filename(participant_id),
        datetime.now().strftime("%Y-%m-%d_%H%M%S"),
    )
    csv_path = os.path.join(GAZEFOLLOWER_CSV_DIR, base + ".csv")
    n = 2
    while os.path.exists(csv_path):   # same participant, same second
        csv_path = os.path.join(GAZEFOLLOWER_CSV_DIR,
                                "%s_run%d.csv" % (base, n))
        n += 1
    counts = finalize_gazefollower_session(
        participant_id, csv_path, state["stimulus_log"],
        correction=state.get("correction"),
    )
    quality = counts.pop("__quality__", {})

    # Per-stimulus pass/fail against the PREREGISTERED quality threshold
    for q in quality.values():
        q["passes_gaze_samples_threshold"] = (
            q.get("gaze_samples_pct", 0.0) >= MIN_GAZE_SAMPLES_PCT
        )

    # Validation verdicts (pre/post accuracy vs. preregistered threshold)
    validations = list(state.get("validations", []))
    for v in validations:
        err = v.get("mean_err_deg")
        v["passes_threshold"] = (
            err is not None and err <= MAX_VALIDATION_ERROR_DEG
        )

    # Session manifest: reproducibility record of exactly what was shown
    # when, with which screen geometry — invaluable when reanalysing.
    manifest = {
        "participant_id": participant_id,
        "finalized_at_utc": datetime.now(timezone.utc).isoformat(),
        "session_csv": os.path.basename(csv_path),
        "test_mode": TEST_MODE,
        "sample_counts": counts,
        # Tobii-style data-loss metrics per stimulus:
        # valid_pct = successful estimations / captured frames (≈100 %
        #   here — GazeFollower flags every emitted frame as valid);
        # gaze_samples_pct = valid / expected at NOMINAL_SAMPLING_HZ, a
        #   fixed constant, so decimated frames actually count as loss;
        # relative_yield_pct = the old self-referential ratio, kept for
        #   comparison with sessions recorded before this fix.
        "data_quality": quality,
        # Mandatory accuracy validations (pre = after calibration,
        # post = after the last video → drift check) with the
        # assumptions used for the px → degrees conversion.
        "validations": validations,
        # Post-hoc gain correction applied to the corrected_* columns
        # and the video-normalized coordinates (raw columns untouched)
        "gain_correction": _correction_payload(state.get("correction")),
        # Head-position snapshot from the pre-calibration guide. OPTIONAL
        # — the guide is a convenience for seating the participant, and
        # in most sessions nobody opens it, so this is usually null.
        "head_position": state.get("position_snapshot"),
        # ── THE VIEWING DISTANCE, from the MANDATORY validation ───────
        # Every degree figure in this study divides by this number, so it
        # cannot depend on whether someone happened to open an optional
        # guide. It is measured during the accuracy check, which always
        # runs, at the moment the participant is sitting exactly as they
        # will for the stimuli.
        #
        # It was already being measured there and written into the
        # validation record — and then nothing read it. verify_metrics
        # looked in head_position.distance_cm: a block only the optional
        # guide fills, under a key ("distance_cm") that even the guide
        # does not use (it writes "est_distance_cm"). Two mismatches in
        # series, so head_distance_cm reported MISSING on every session
        # ever recorded while the correct value sat in the manifest.
        "distance": _session_distance(state),
        # Pre-session rate gate: the sustained sampling rate measured
        # BEFORE calibration, and whether the researcher overrode a
        # failing verdict. Lets analysis separate "we knew this session
        # was degraded" from "we found out afterwards".
        # RQ2 event metrics per stimulus (fixations, saccades, off-video),
        # each carrying the caveat that makes it interpretable at this
        # sampling rate. Previously computed only in quality_report.py and
        # never persisted.
        "events": counts.get("__events__"),
        # Recording conditions, captured at login. RQ1 asks about
        # varying recording conditions; this is the only record of them.
        "conditions": state.get("conditions"),
        "rate_gate": state.get("rate_gate"),
        # Condensed background telemetry (full series in data/telemetry/).
        # Keeps the manifest readable while making the headline context —
        # CPU, clock, power, per-stage cost — visible without opening a
        # second file.
        "telemetry": state.get("telemetry_summary"),
        "telemetry_file": state.get("telemetry_file"),
        # Every rate measurement taken during this session, in order, so
        # a drift across the session is visible afterwards instead of
        # having to be reproduced live.
        "rate_history": state.get("rate_history"),
        "quality_thresholds": {
            "max_validation_error_deg": MAX_VALIDATION_ERROR_DEG,
            "min_gaze_samples_pct": MIN_GAZE_SAMPLES_PCT,
            "min_sampling_hz": MIN_SAMPLING_HZ,
            # Denominator of gaze_samples_pct — report this alongside the
            # percentage; the percentage is meaningless without it.
            "nominal_sampling_hz": NOMINAL_SAMPLING_HZ,
            "assumed_viewing_distance_cm": VIEWING_DISTANCE_CM,
        },
        "stimuli": state["stimulus_log"],
        # WHAT WAS PRESENTED, and in what order.
        # "all" vs "clip30" changes the entire dataset, and the order is
        # randomised per participant — so a reader cannot reconstruct
        # either from the code. Both belong in the record: the order is
        # how you check the counterbalancing actually balanced, and the
        # mode is how you tell a pilot run from a collection run months
        # later.
        "stimulus_mode": SESSION_STIMULUS_MODE,
        "stimulus_order": [s.get("stimulus")
                           for s in (state["stimulus_log"] or [])
                           if s.get("stimulus")],
    }
    try:
        with open(csv_path.replace(".csv", "_manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
    except OSError:
        logger.exception("Could not write session manifest")

    logger.info(
        "Session finalized – sid=%s, participant=%s, per-stimulus samples: %s",
        sid, participant_id, counts,
    )
    return counts


@socketio.on("experiment_done")
def handle_experiment_done(_payload=None):
    """All stimuli shown — save & segment the session recording."""
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)

    def _run() -> None:
        # Background-task exceptions are otherwise swallowed silently —
        # always log them and always release the waiting client.
        try:
            counts = _finalize_session(sid, state)
        except Exception:  # noqa: BLE001
            logger.exception("Session finalize FAILED")
            counts = {}
        socketio.emit("experiment_saved", {"gazefollower_samples": counts}, to=sid)

    socketio.start_background_task(_run)


@socketio.on("disconnect")
def handle_disconnect():
    """Tear down per-connection state (finalizing any unsaved session
    recording first — e.g. if the browser was closed mid-experiment)."""
    sid = request.sid  # type: ignore[attr-defined]
    with _sessions_lock:
        state = active_sessions.get(sid)
    if state and state.get("stimulus_log") and not state.get("finalized"):
        logger.warning("Disconnect with unsaved session — finalizing now.")
        _finalize_session(sid, state)
    _remove_session_state(sid)
    logger.info("SocketIO disconnected: sid=%s", sid)


@socketio.on("start_recording")
def handle_start_recording(payload: dict):
    """Begin recording gaze/pupil data for a stimulus.

    Expected payload::

        {"stimulus_name": "video_happy.mp4"}
    """
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)

    stimulus_name = payload.get("stimulus_name", "unknown")
    participant_id = state.get("participant_id") or session.get("participant_id", "unknown")
    state["participant_id"] = participant_id

    state["recording"] = True
    state["preview_active"] = False   # stop the verification preview loop
    _telemetry_event(sid, "stimulus_start", stimulus=stimulus_name,
                     index=len(state["stimulus_log"]))

    # Mark stimulus onset in the continuous GazeFollower recording.
    # Trigger IDs: onset = 100 + 2k, offset = 101 + 2k (k = stimulus #).
    k = len(state["stimulus_log"])
    state["gazefollower_active"] = gaze_service.begin_stimulus(100 + 2 * k)
    state["current_t_start_ns"] = time.time_ns()
    # Video content rectangle in physical screen px (for converting gaze
    # screen coordinates → normalized video coordinates later)
    state["current_video_rect"] = payload.get("video_rect")
    if not state["gazefollower_active"]:
        logger.warning(
            "GazeFollower unavailable for stimulus %s – "
            "no gaze data will be recorded for it!",
            stimulus_name,
        )

    logger.info(
        "Recording started – sid=%s, participant=%s, stimulus=%s",
        sid,
        participant_id,
        stimulus_name,
    )
    emit("recording_started", {"stimulus_name": stimulus_name})


# ──────────────────────────────────────────────────────────────
# Validation-based gain correction
# Appearance-based webcam trackers under-shoot eccentric gaze (the
# "squished toward the center" pattern: correct direction, gain < 1).
# The validation targets give us measured-vs-true pairs, so a per-axis
# affine correction (target = a·measured + b) can be fitted and applied
# post-hoc — a session-wise recalibration, as in recent webcam
# eye-tracking work. A manual gain slider is offered as well; both are
# logged in the manifest.
# ──────────────────────────────────────────────────────────────

_GAIN_MIN, _GAIN_MAX = 0.5, 3.0

# A correction maps measured gaze → corrected gaze per axis with a
# polynomial (coeffs highest-order first, as np.polyfit/np.polyval):
#   {"px": [a, b], "py": [a2, a1, a0], "cy": <center>, "source": str}
# Degree 1 (affine) is the default; the VERTICAL axis may use degree 2
# to correct the common webcam nonlinearity where up-gaze is estimated
# too high (a straight-line gain cannot fix a curved error).


def _fit_poly(pairs: "list[tuple[float, float]]",
              degree: int) -> "list | None":
    """Least-squares fit target = poly(measured); coeffs highest-first.

    Uses numpy's Polynomial.fit, which fits in a scaled/shifted domain
    and is therefore well-conditioned even for raw pixel inputs in the
    hundreds — a plain polyfit at degree 2 on such values is
    numerically unstable and silently collapses the quadratic term.
    """
    import numpy as np

    if len(pairs) < degree + 1:
        return None
    xs = [p[0] for p in pairs]
    ts = [p[1] for p in pairs]
    if len({round(x, 1) for x in xs}) < degree + 1:
        return None          # not enough distinct measured levels
    try:
        fitted = np.polynomial.Polynomial.fit(xs, ts, degree)
        # .convert() → standard basis, ascending order; reverse to the
        # highest-first convention used by np.polyval throughout.
        coeffs = list(fitted.convert().coef)[::-1]
        # Pad in case a leading coeff collapsed to ~0 (degree preserved).
        while len(coeffs) < degree + 1:
            coeffs.insert(0, 0.0)
        return [float(c) for c in coeffs]
    except Exception:
        return None


def _rms_resid(pairs: "list[tuple[float, float]]", coeffs: list) -> float:
    import numpy as np

    return float(np.sqrt(np.mean(
        [(np.polyval(coeffs, m) - t) ** 2 for m, t in pairs])))


def _slope(coeffs: list, at: float) -> float:
    """Local gain (derivative) of the mapping at coordinate *at*."""
    import numpy as np

    if len(coeffs) < 2:
        return 0.0
    return float(np.polyval(np.polyder(coeffs), at))


def _apply_point(x: float, y: float, corr: "dict | None") -> "tuple":
    import numpy as np

    if not corr:
        return x, y
    px, py = corr.get("px"), corr.get("py")
    return (float(np.polyval(px, x)) if px else x,
            float(np.polyval(py, y)) if py else y)


def _apply_series(x_series, y_series, corr):
    """Vectorized correction for pandas Series → (Series, Series)."""
    import numpy as np

    px, py = corr.get("px"), corr.get("py")
    gx = pd.Series(np.polyval(px, x_series.to_numpy()),
                   index=x_series.index) if px else x_series
    gy = pd.Series(np.polyval(py, y_series.to_numpy()),
                   index=y_series.index) if py else y_series
    return gx, gy


def _correction_payload(corr: "dict | None") -> dict:
    if not corr:
        return {"active": False}
    px, py = corr.get("px", [1, 0]), corr.get("py", [1, 0])
    cy = corr.get("cy", 0.0)
    gx = px[0] if len(px) == 2 else _slope(px, corr.get("cx", 0.0))
    gy = py[0] if len(py) == 2 else _slope(py, cy)
    return {
        "active": True,
        "px": [round(c, 6) for c in px],
        "py": [round(c, 6) for c in py],
        "cy": round(cy, 1),
        "kind": "quadratic-vertical" if len(py) == 3 else "affine",
        "gain_x": round(gx, 3),
        "gain_y": round(gy, 3),
        "gain_mean": round((gx + gy) / 2, 2),
        "source": corr["source"],
    }


def _auto_fit_correction(state: dict, record: dict, sid: str) -> None:
    """Fit measured→true from a completed pre-validation.

    Recovers the RAW measured gaze (inverting any affine correction that
    was active during the check), then fits target = poly(raw): degree 1
    for x, and degree 2 for y when it materially beats a line and stays
    monotonic across the screen (the up-gaze overshoot fix). Fitting on
    recovered raw avoids composing corrections, so repeated checks can
    only improve — never double-apply.
    """
    active = record.get("correction_active") or {}
    px0, py0 = active.get("px"), active.get("py")
    affine_active = bool(active.get("active")) and \
        (px0 and len(px0) == 2 and py0 and len(py0) == 2)
    if active.get("active") and not affine_active:
        logger.info("Gain auto-fit skipped (active correction is not "
                    "affine-invertible — keeping current)")
        return

    def _raw(m: float, coeffs) -> float:
        return (m - coeffs[1]) / coeffs[0] if affine_active else m

    pairs_x, pairs_y = [], []
    for t in record.get("targets", []):
        if t.get("mx") is None or t.get("my") is None:
            continue
        pairs_x.append((_raw(float(t["mx"]), px0), float(t["tx"])))
        pairs_y.append((_raw(float(t["my"]), py0), float(t["ty"])))

    fit_x = _fit_poly(pairs_x, 1)
    lin_y = _fit_poly(pairs_y, 1)
    if not fit_x or not lin_y:
        logger.info("Gain auto-fit skipped (too few / degenerate targets)")
        return
    if not (_GAIN_MIN <= fit_x[0] <= _GAIN_MAX):
        logger.info("Gain auto-fit skipped (implausible horizontal gain)")
        return

    # Try a quadratic vertical fit; accept only if it clearly beats the
    # line AND the mapping stays monotonic with a sane local gain across
    # the whole screen height (no fold-over, no runaway magnification).
    chosen_y, kind = lin_y, "affine"
    quad_y = _fit_poly(pairs_y, 2)
    height = float((record.get("screen") or {}).get("height_px") or 0)
    if quad_y and height > 0:
        ok = True
        for yy in (0.0, height * 0.5, height):
            g = _slope(quad_y, yy)
            if not (_GAIN_MIN <= g <= _GAIN_MAX):   # monotone & sane
                ok = False
                break
        if ok and _rms_resid(pairs_y, quad_y) < 0.8 * _rms_resid(pairs_y,
                                                                 lin_y):
            chosen_y, kind = quad_y, "quadratic-vertical"

    corr = {
        "px": fit_x,
        "py": chosen_y,
        "cx": (fit_x and (record.get("screen") or {}).get("width_px", 0)
               / 2) or 0.0,
        "cy": height / 2 if height else 0.0,
        "source": "auto-fit (%s)" % kind,
    }
    state["correction"] = corr
    state["auto_correction"] = dict(corr)     # restorable via "Auto"
    logger.info("Gain correction auto-fitted (%s): gain_x %.2f, gain_y "
                "(centre) %.2f", kind, fit_x[0], _slope(chosen_y, corr["cy"]))
    socketio.emit("gain_correction", _correction_payload(corr), to=sid)


@socketio.on("set_gain_correction")
def handle_set_gain_correction(payload: dict):
    """Manual gain slider / auto restore / off.

    Payloads: {"mode": "auto"} · {"mode": "off"} ·
              {"gain": 1.4, "center_x": 720, "center_y": 450}
    """
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)
    mode = payload.get("mode")
    if mode == "auto":
        state["correction"] = state.get("auto_correction")
    elif mode == "off":
        state["correction"] = None
    else:
        try:
            g = min(max(float(payload.get("gain", 1.0)), _GAIN_MIN),
                    _GAIN_MAX)
            cx = float(payload.get("center_x", 0))
            cy = float(payload.get("center_y", 0))
        except (TypeError, ValueError):
            return
        # Uniform scaling around the screen center (the fixed point):
        # corrected = center + gain · (measured − center), i.e. the
        # affine polynomial [g, center·(1−g)] on each axis.
        state["correction"] = {
            "px": [g, cx * (1 - g)],
            "py": [g, cy * (1 - g)],
            "cx": cx, "cy": cy,
            "source": "manual slider",
        }
    logger.info("Gain correction set: %s",
                _correction_payload(state.get("correction")))
    emit("gain_correction", _correction_payload(state.get("correction")))


def _screen_space_check(browser_screen: dict) -> dict:
    """Compare the tracker's pixel space with the browser's.

    GazeFollower's ``DefaultConfig`` sets
    ``screen_size = [get_monitors()[0].width, .height]`` — PHYSICAL
    pixels of whichever monitor screeninfo lists first — and scales all
    gaze into that space. The browser positions validation targets in CSS
    pixels on whichever monitor it happens to be on. Those spaces differ
    whenever:

      * Windows/macOS display scaling is not 100 % (a 2560x1440 panel at
        150 % is 1707x960 to the browser → every gaze coordinate is
        ~1.5x too large), or
      * there is more than one monitor and the browser is not on
        ``monitors[0]``.

    Either way calibration looks flawless — it is internally consistent
    inside the tracker's own space — and the browser-side accuracy check
    is the first thing to reveal it. That is a coordinate bug, not a
    tracking failure, and no amount of recalibrating will fix it.
    """
    out: dict = {"checked": False}
    bw = browser_screen.get("width_px")
    bh = browser_screen.get("height_px")
    if not (bw and bh):
        return out
    info = gaze_service.screen_info()
    if not info:
        return out

    size = info.get("gaze_screen_size")
    monitors = info.get("monitors") or []
    out.update(checked=True, browser=[bw, bh], tracker=size,
               monitor_count=len(monitors), monitors=monitors)
    if not size:
        return out

    sx, sy = size[0] / float(bw), size[1] / float(bh)
    out["scale_x"], out["scale_y"] = round(sx, 3), round(sy, 3)
    # 2 % tolerance absorbs rounding and browser chrome quirks.
    out["mismatch"] = bool(abs(sx - 1) > 0.02 or abs(sy - 1) > 0.02)
    if out["mismatch"]:
        if abs(sx - sy) < 0.05:
            out["likely_cause"] = (
                "display scaling — the tracker's space is %.2fx the "
                "browser's on both axes, which is what an OS scale factor "
                "of %d %% looks like" % (sx, round(sx * 100)))
        elif len(monitors) > 1:
            out["likely_cause"] = (
                "multiple monitors — GazeFollower uses monitors[0] "
                "(%dx%d), which may not be the screen the browser is on"
                % (size[0], size[1]))
        else:
            out["likely_cause"] = "unequal scale on the two axes"
        logger.error(
            "SCREEN SPACE MISMATCH: tracker maps gaze into %sx%s but the "
            "browser reports %sx%s (scale %.2f x %.2f). %s. Validation "
            "error is a COORDINATE bug, not a tracking failure — "
            "recalibrating will not help.",
            size[0], size[1], bw, bh, sx, sy, out["likely_cause"])
    return out


def _uncorrected_error(payload: dict, corr: "dict | None") -> "dict":
    """Recover the validation error the tracker would have shown WITHOUT
    the active gain correction.

    Why this exists: the pre-check is measured before the auto-fit runs
    (raw), the post-check afterwards (corrected). Subtracting them yields
    "drift" that actually mixes true drift with the effect of the
    correction — and the correction was FITTED on the pre targets, so the
    comparison is also in-sample on one side. Storing a raw figure for
    every phase makes drift a like-for-like, out-of-sample comparison.

    Only affine corrections are analytically invertible here; a quadratic
    vertical fit is reported as not invertible rather than approximated.
    """
    import numpy as np

    out: dict[str, Any] = {"raw_available": False}
    if not corr:
        # Nothing was applied — the measured error IS the raw error.
        out.update(raw_available=True,
                   mean_err_px_raw=payload.get("mean_err_px"),
                   mean_err_deg_raw=payload.get("mean_err_deg"))
        return out
    px, py = corr.get("px"), corr.get("py")
    if not (px and len(px) == 2 and py and len(py) == 2 and px[0] and py[0]):
        return out                      # quadratic / degenerate → skip
    errs = []
    for t in payload.get("targets", []):
        if t.get("mx") is None or t.get("my") is None:
            continue
        rx = (float(t["mx"]) - px[1]) / px[0]
        ry = (float(t["my"]) - py[1]) / py[0]
        errs.append(float(np.hypot(rx - float(t["tx"]), ry - float(t["ty"]))))
    if not errs:
        return out
    mean_px = float(np.mean(errs))
    # Reuse the browser's px→degree scale (it knows the screen geometry).
    err_px, err_deg = payload.get("mean_err_px"), payload.get("mean_err_deg")
    deg_per_px = (err_deg / err_px) if (err_px and err_deg) else None
    out.update(raw_available=True, mean_err_px_raw=round(mean_px, 1))
    if deg_per_px:
        out["mean_err_deg_raw"] = round(mean_px * deg_per_px, 2)
    return out


@socketio.on("validation_result")
def handle_validation_result(payload: dict):
    """Store a mandatory accuracy-validation result (pre or post).

    Expected payload (computed in the browser, where screen geometry
    is known)::

        {"phase": "pre" | "post",
         "targets": [{"tx":…, "ty":…, "mx":…, "my":…, "err_px":…}, …],
         "mean_err_px": 74.2, "mean_err_deg": 1.61,
         "screen": {"width_px":…, "height_px":…, "diag_inches":…,
                    "diag_assumed": false,
                    "viewing_distance_cm": 60}}
    """
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)
    record = {
        "phase": payload.get("phase", "unknown"),
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "targets": payload.get("targets", []),
        "targets_measured": payload.get("targets_measured"),
        "mean_err_px": payload.get("mean_err_px"),
        "mean_err_deg": payload.get("mean_err_deg"),
        # Precision = RMS of sample-to-sample distances (reported next
        # to accuracy, as is standard in eye-tracking method sections)
        "mean_precision_px": payload.get("mean_precision_px"),
        "mean_precision_deg": payload.get("mean_precision_deg"),
        # RATE-INDEPENDENT precision. mean_precision_px is a
        # sample-to-sample RMS, so it depends on how far apart in time
        # consecutive samples are: raising the poll rate from 7 to
        # 30 Hz exposes high-frequency noise decimation was hiding and
        # the figure rises even though the signal is unchanged. This
        # one is dispersion about the target median and does not.
        "mean_precision_sd_px": payload.get("mean_precision_sd_px"),
        # Whether a gain correction was active while this validation
        # was measured (pre before fit: raw; post: usually corrected)
        "correction_active": _correction_payload(state.get("correction")),
        "screen": payload.get("screen", {}),
        # Browser viewport geometry while the check ran (fullscreen state,
        # inner/outer size, screen size, the screen→viewport offsets and
        # devicePixelRatio). Without this, a coordinate-space fault is
        # indistinguishable from bad tracking after the fact.
        "geometry": payload.get("geometry"),
    }
    # Correction-free error for like-for-like pre/post drift (see
    # _uncorrected_error for why the raw figures are the ones to compare).
    record.update(_uncorrected_error(payload, state.get("correction")))
    # COORDINATE-SPACE CHECK. The classic "calibration looked perfect,
    # validation was terrible" failure is not tracking at all: it is the
    # tracker and the browser using different pixel spaces. GazeFollower
    # maps gaze into screeninfo's PHYSICAL monitor pixels; the browser
    # lays targets out in CSS pixels. Under Windows display scaling (or
    # with the browser on a second monitor) those differ by a constant
    # factor, so calibration stays self-consistent and only the
    # browser-side check exposes it.
    record["screen_space"] = _screen_space_check(payload.get("screen") or {})

    # The rate this check SAMPLED at. Not the tracker's rate: the
    # validation reads the preview stream, so the poll interval sets how
    # many samples land per target and, more importantly, the interval
    # over which precision (a sample-to-sample RMS) is computed. Two
    # sessions measured at different poll rates do not have comparable
    # precision figures, so the rate belongs in the record rather than
    # being inferred from the sample counts afterwards.
    record["sampled_at_hz"] = round(
        1.0 / max(1e-6, state.get("preview_interval_s",
                                  PREVIEW_INTERVAL_S)), 1)

    # ── AUTHORITATIVE degrees, recomputed server-side ────────────────
    # The browser converts px to degrees using window.measuredDistanceCm,
    # which is only set if the OPTIONAL position guide ran and produced a
    # plausible value. In every session recorded so far it did not, so
    # every reported degree silently used the hardcoded 60 cm — visible
    # in the manifests as "viewing_distance_measured": false.
    #
    # The validation itself is mandatory and the tracker is sampling
    # during it, so the distance is measured HERE, at the moment of
    # measurement, and the degrees are recomputed from it. The browser's
    # value is kept for comparison rather than overwritten: if the two
    # differ, that difference is exactly the error the assumption caused.
    try:
        pos = gaze_service.position_info() or {}
        dist = pos.get("est_distance_cm")
        scr = payload.get("screen") or {}
        if dist and 25 < float(dist) < 120 and record.get("mean_err_px"):
            import camera_geometry

            geom = {"w_px": int(scr.get("width_px") or 1920),
                    "h_px": int(scr.get("height_px") or 1080),
                    "diag_in": float(scr.get("diag_inches") or 15.6)}
            rel_sd = float(pos.get("distance_rel_sd_pct") or 0) / 100.0
            for field in ("mean_err", "mean_precision"):
                px = record.get(field + "_px")
                if px is None:
                    continue
                conv = camera_geometry.degrees_with_uncertainty(
                    float(px), float(dist), float(dist) * rel_sd, **geom)
                record[field + "_deg_measured"] = conv.get("deg")
                if conv.get("deg_lo") is not None:
                    record[field + "_deg_lo"] = conv["deg_lo"]
                    record[field + "_deg_hi"] = conv["deg_hi"]
            record["distance"] = {
                "cm": dist,
                "source": pos.get("distance_source"),
                "rel_sd_pct": pos.get("distance_rel_sd_pct"),
                "iris_cm": pos.get("distance_cm_iris"),
                "iod_cm": pos.get("distance_cm_iod"),
                "estimates_agree": pos.get("distance_estimates_agree"),
                "warning": pos.get("distance_warning"),
                # Why the better ruler was not used, if it was not.
                "iris_error": pos.get("iris_error"),
                "focal_measured": pos.get("focal_measured"),
                "measured": True,
            }
            browser_deg = record.get("mean_err_deg")
            if browser_deg and record.get("mean_err_deg_measured"):
                shift = 100 * (record["mean_err_deg_measured"]
                               / browser_deg - 1)
                record["distance"]["browser_assumption_error_pct"] = round(
                    shift, 1)
                if abs(shift) > 10:
                    logger.warning(
                        "Validation degrees shift %.0f %% once the MEASURED "
                        "distance (%.1f cm, via %s) replaces the browser's "
                        "assumption: %.2f -> %.2f deg",
                        shift, dist, pos.get("distance_source"),
                        browser_deg, record["mean_err_deg_measured"])
            if pos.get("distance_warning"):
                logger.warning("DISTANCE: %s", pos["distance_warning"])
        else:
            record["distance"] = {"measured": False,
                                  "reason": "no usable distance from the "
                                            "tracker at validation time",
                                  "assumed_cm": (scr or {}).get(
                                      "viewing_distance_cm")}
    except Exception:  # noqa: BLE001 — never lose a validation over this
        logger.exception("Could not recompute validation degrees")
    # ── WHICH CHECK IS WHICH, decided here and recorded ──────────────
    # pre_fit    grid A, uncorrected. Native accuracy AND the fit set.
    # pre_check  grid B, corrected, positions the fit never saw. The
    #            corrected accuracy that can be defended.
    # post       grid B, corrected. Drift against pre_check, uncorrected
    #            basis.
    #
    # The role is written into the record rather than inferred later
    # from the order of the list, because "which validation counted"
    # decided after seeing the numbers is exactly the criticism this
    # design exists to answer.
    ROLES = {
        "pre_fit": ("fit set — uncorrected native accuracy; the gain "
                    "correction is fitted on these targets, so its error "
                    "here is IN-SAMPLE by construction"),
        "pre_check": ("out-of-sample check — corrected accuracy at seven "
                      "positions the correction was never fitted to. This "
                      "is the canonical corrected accuracy"),
        "post": ("drift check — same grid as pre_check, differenced on "
                 "the UNCORRECTED basis"),
        "pre": "legacy single pre-validation (recorded before the split)",
    }
    record["role"] = ROLES.get(record["phase"], "unknown")
    record["grid"] = "A" if record["phase"] in ("pre_fit", "pre") else "B"
    record["canonical_accuracy"] = bool(record["phase"] == "pre_check")
    # How many times this phase has been attempted in this session. A
    # protocol deviation must be visible in the data, not only in
    # someone's memory of the session.
    prior = [v for v in state.get("validations", [])
             if v.get("phase") == record["phase"]]
    record["attempt"] = len(prior) + 1
    if record["attempt"] > 1:
        logger.warning(
            "PROTOCOL: %s validation attempt #%d. The pre-registered rule "
            "is ONE attempt per phase; repeats are recorded and the FIRST "
            "attempt remains canonical. Do not select between them.",
            record["phase"], record["attempt"])

    state.setdefault("validations", []).append(record)
    # Only the FIT phase may fit. Re-fitting on the check set would
    # destroy the one property that makes the check meaningful.
    if record["phase"] in ("pre_fit", "pre") \
            and record.get("mean_err_px") is not None \
            and record["attempt"] == 1:
        _auto_fit_correction(state, record, sid)
    # Log the things that distinguish a coordinate fault from bad
    # tracking, so a bad validation is diagnosable from the log alone.
    geom = record.get("geometry") or {}
    counts = [t.get("n_samples", 0) for t in record["targets"]]
    logger.info(
        "Validation (%s): mean error %.1f px / %.2f deg | targets measured "
        "%s/%s, samples per target %s | fullscreen=%s inner=%s offsets=%s "
        "dpr=%s – sid=%s",
        record["phase"],
        record["mean_err_px"] or -1,
        record["mean_err_deg"] or -1,
        payload.get("targets_measured"), len(record["targets"]), counts,
        geom.get("fullscreen"), geom.get("inner"), geom.get("offsets"),
        geom.get("device_pixel_ratio"), sid,
    )
    _telemetry_event(sid, "validation",
                     phase=record["phase"],
                     mean_err_px=record.get("mean_err_px"),
                     mean_err_deg=record.get("mean_err_deg"),
                     targets=len(record["targets"]),
                     min_samples_per_target=min(counts) if counts else None,
                     fullscreen=geom.get("fullscreen"))
    if counts and min(counts) < 5:
        logger.warning(
            "Validation had targets with very few samples (%s). At ~30 Hz a "
            "1.6 s collection should yield ~45. Few samples means the gaze "
            "preview was not streaming — the medians are then computed from "
            "almost nothing and the error is meaningless.", counts)
    emit("validation_saved", {"phase": record["phase"]})


_CAL_MODE_MAP = {"quick": 5, "full": 13, "skip": 5}


def _calibration_options() -> dict:
    """Test-mode calibration choice from the login form (empty otherwise)."""
    if not TEST_MODE:
        return {}
    cal = (session.get("test_opts") or {}).get("cal", "quick")
    return {
        "cali_mode": _CAL_MODE_MAP.get(cal, 5),
        "skip": cal == "skip",
        "skip_preview": cal == "quick",
    }


@socketio.on("warmup_tracker")
def handle_warmup_tracker(_payload=None):
    """Pre-load the gaze model & camera while the participant reads the
    calibration instructions (avoids a long, freeze-like wait after the
    calibration button is clicked on slower laptops)."""
    opts = _calibration_options()   # session access needs request context

    sid = request.sid  # type: ignore[attr-defined]

    def _warm() -> None:
        ok = gaze_service.warmup(opts.get("cali_mode"))
        socketio.emit("tracker_warmed", {"ok": ok}, to=sid)
        logger.info("Tracker warmup finished: ok=%s", ok)

    logger.info("Tracker warmup requested (opts=%s)", opts)
    socketio.start_background_task(_warm)


def _power_state() -> dict:
    """AC/battery and current CPU clock, for the manifest and the gate.

    WHY THIS IS RECORDED WITH EVERY SESSION
    ---------------------------------------
    Per-frame cost scales with clock speed, and both model stages scale
    TOGETHER — measured at 2.26x for FaceMesh and 2.26x for the gaze CNN
    between an unthrottled and a throttled run. A session recorded on
    battery therefore samples at roughly half the rate of one recorded on
    AC, with no other difference and nothing in the data to show it.

    That is a confound, not an inconvenience: sampling rate determines
    fixation duration quantisation, so a battery session and an AC
    session are not directly comparable. Recording the power state makes
    it auditable after the fact instead of invisible.
    """
    state: dict = {}
    try:
        import psutil

        batt = psutil.sensors_battery()
        if batt is not None:
            state["on_ac_power"] = bool(batt.power_plugged)
            state["battery_pct"] = round(batt.percent)
        freq = psutil.cpu_freq()
        if freq:
            state["cpu_mhz"] = round(freq.current)
            state["cpu_mhz_max"] = round(freq.max) if freq.max else None
    except Exception as exc:  # noqa: BLE001
        state["error"] = str(exc)[:80]
    state["warnings"] = []
    return state


def _power_notes(power: dict, hz: float) -> list:
    """Power-related notes, but ONLY when the rate is actually low.

    CORRECTED 2026-08-04. This first warned about battery power
    unconditionally. That was wrong, and measurably so: with EcoQoS
    disabled this machine records 29.4 Hz on battery at 53 %, while the
    same machine on the same charge recorded 15.1 Hz with EcoQoS active.
    Battery state was never the mechanism — Windows' background-process
    demotion was (see perf_mode.py).

    An unconditional warning is worse than none: it attaches a confident
    false explanation to every session, and a researcher chasing a real
    problem would spend the day on the charger. So these are reported as
    THINGS TO CHECK, and only when the measured rate is actually low.
    The measurement is the authority; this is a hint list.
    """
    if hz >= MIN_SAMPLING_HZ:
        return []
    notes = []
    if power.get("on_ac_power") is False:
        notes.append(
            "on battery (%s%%) — worth ruling out, though on this project "
            "battery state alone did NOT reduce the rate"
            % power.get("battery_pct"))
    cur, mx = power.get("cpu_mhz"), power.get("cpu_mhz_max")
    if cur and mx and cur < 0.6 * mx:
        notes.append(
            "CPU at %d of %d MHz (%.0f%%) — note psutil reports the BASE "
            "clock as 'max' on Intel, so this ratio understates turbo "
            "headroom and is only a rough signal"
            % (cur, mx, 100.0 * cur / mx))
    return notes


def _run_rate_gate(sid: str) -> None:
    """Measure the sustained sampling rate and gate the session on it.

    MUST run AFTER calibration: GazeFollower's ``process_frame`` raises
    on every frame when no calibration model is loaded, so a pre-
    calibration rate check yields zero samples and a flood of tracebacks.
    (That is also why ``tracker_fps_test.py`` needs a saved model.)

    Running it here is the better placement anyway:
      1. Calibration itself is a 1–2 minute inference workload, so by
         this point the CPU turbo window has already closed — the number
         measured here is the SUSTAINED rate the stimuli will be
         recorded at, not a flattering cold-start burst.
      2. It measures the fully assembled pipeline (camera → face → gaze
         model), which is exactly what records the session.

    A failing gate blocks the accuracy check (and therefore the videos)
    until the researcher fixes the setup or explicitly overrides.
    """
    if not RATE_GATE_SECONDS:
        return
    state = _get_session_state(sid)
    started = gaze_service.rate_check_start()
    if not started or not started.get("ok"):
        state["rate_gate"] = {
            "ok": False,
            "error": (started or {}).get("error", "unavailable"),
            "needs_calibration": bool((started or {}).get("needs_calibration")),
        }
        socketio.emit("rate_gate", state["rate_gate"], to=sid)
        _telemetry_event(sid, "rate_gate_failed",
                         error=state["rate_gate"].get("error"))
        logger.info("Rate gate: %s", state["rate_gate"])
        return
    # Yield for the measurement window. socketio.sleep (NOT time.sleep)
    # so the server keeps serving the live gaze preview and everything
    # else; the tracker records sample arrivals passively meanwhile.
    socketio.sleep(RATE_GATE_SECONDS)
    report = gaze_service.rate_check_result(RATE_GATE_TAIL_SECONDS)
    if not report or not report.get("ok"):
        state["rate_gate"] = {
            "ok": False,
            "error": (report or {}).get("error", "unavailable"),
            "needs_calibration": bool((report or {}).get("needs_calibration")),
        }
    else:
        hz = report.get("sustained_hz") or 0.0
        report["passes"] = bool(hz >= MIN_SAMPLING_HZ)
        report["min_sampling_hz"] = MIN_SAMPLING_HZ
        # Power state travels WITH the rate, so a low reading always
        # carries its most likely explanation instead of prompting
        # another investigation.
        report["power"] = _power_state()
        state["rate_gate"] = report
    socketio.emit("rate_gate", state["rate_gate"], to=sid)
    _telemetry_event(sid, "rate_gate",
                     stage=state.get("rate_stage"),
                     hz=(state.get("rate_gate") or {}).get("sustained_hz"),
                     passes=(state.get("rate_gate") or {}).get("passes"),
                     face_ms=((state.get("rate_gate") or {}).get("stages")
                              or {}).get("face_ms_median"),
                     gaze_ms=((state.get("rate_gate") or {}).get("stages")
                              or {}).get("gaze_ms_median"))
    # Keep EVERY measurement, not just the newest. The rate gate can run
    # several times in a session (initial, "Measure again", the probes
    # around the videos), and the interesting question is how the rate
    # moves ACROSS the session — a single final number cannot show that.
    history = state.setdefault("rate_history", [])
    entry = dict(state["rate_gate"])
    entry["at"] = datetime.now(timezone.utc).isoformat()
    entry["stage"] = state.get("rate_stage", "pre-video")
    history.append(entry)
    logger.info(
        "Rate gate [%s #%d]: %s Hz sustained (initial %s, peak %s) | "
        "%s%% detected | subscribers=%s | bursty=%s | profile %s",
        entry["stage"], len(history),
        entry.get("sustained_hz"), entry.get("initial_hz"),
        entry.get("peak_hz"), entry.get("detected_pct"),
        entry.get("subscribers"), entry.get("bursty"),
        entry.get("profile_hz"),
    )
    for _w in _power_notes(entry.get("power") or {},
                           entry.get("sustained_hz") or 0.0):
        logger.warning("Rate is low — worth checking: %s", _w)
    cap = entry.get("capture")
    if cap:
        # Only present in fake-camera runs, and the single most
        # informative line in the log when it is: it says whether the
        # missing frames were never produced or were produced and lost.
        logger.info(
            "Capture loop [%s]: camera delivered %s Hz vs %s Hz sampled "
            "(%s%% lost downstream) | per-frame work %s ms median / %s ms "
            "p90 of a %s ms budget | %s",
            entry["stage"], cap.get("served_hz_this_window"),
            entry.get("sustained_hz"), entry.get("frames_dropped_pct"),
            cap.get("callback_ms_median"), cap.get("callback_ms_p90"),
            cap.get("frame_budget_ms"),
            "CAPTURE-LIMITED (per-frame work too slow)"
            if entry.get("capture_limited")
            else "FRAMES DROPPED downstream of capture",
        )
    if entry.get("perf_mode"):
        logger.info("Perf mode [%s]: %s", entry["stage"], entry["perf_mode"])
    st = entry.get("stages")
    if st:
        logger.info(
            "Stage split [%s]: FaceMesh %s ms (p90 %s) + gaze CNN %s ms "
            "(p90 %s) = %s ms models + %s ms other = %s ms per frame",
            entry["stage"], st.get("face_ms_median"), st.get("face_ms_p90"),
            st.get("gaze_ms_median"), st.get("gaze_ms_p90"),
            st.get("models_ms_median"), entry.get("overhead_ms_median"),
            (entry.get("capture") or {}).get("callback_ms_median"),
        )
        if st.get("oversized_frames"):
            logger.warning(
                "Camera delivers %s (%.2f MP) — GazeFollower resizes every "
                "frame in software because its resolution settings are "
                "applied to an unopened capture. FaceMesh is being fed %.1fx "
                "the intended 640x480. Set GF_CAMERA_FIX=1 to capture "
                "natively at 640x480 and re-measure this line.",
                st.get("frame_size"), st.get("frame_megapixels") or 0.0,
                (st.get("frame_megapixels") or 0.31) / 0.31)
    if len(history) > 1:
        logger.info(
            "Rate across this session so far: %s",
            " -> ".join("%s(%s Hz, subs=%s)"
                        % (h.get("stage"), h.get("sustained_hz"),
                           h.get("subscribers"))
                        for h in history))


@socketio.on("run_rate_gate")
def handle_run_rate_gate(payload: dict = None):
    """Re-measure the rate on request (the 'Measure again' button).

    An optional ``stage`` label lets a measurement be attributed to a
    point in the session ("pre-video", "post-video"), so the history
    reads as a timeline rather than a list of numbers.
    """
    sid = request.sid  # type: ignore[attr-defined]
    stage = str((payload or {}).get("stage", "manual"))[:40]
    _get_session_state(sid)["rate_stage"] = stage
    socketio.start_background_task(_run_rate_gate, sid)


@socketio.on("rate_gate_override")
def handle_rate_gate_override(payload: dict = None):
    """Researcher acknowledges a failed rate gate and proceeds anyway.

    The gate WARNS rather than hard-blocks: a participant is physically
    present, and there are legitimate reasons to record a known-degraded
    session (pilot runs, equipment demos). But the override is recorded
    in the manifest with its reason, so a low-rate session can never be
    mistaken for a clean one during analysis.
    """
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)
    gate = state.get("rate_gate") or {}
    gate["overridden"] = True
    gate["override_reason"] = str((payload or {}).get("reason", ""))[:200]
    gate["overridden_at_utc"] = datetime.now(timezone.utc).isoformat()
    state["rate_gate"] = gate
    logger.warning("Rate gate OVERRIDDEN by researcher: %s Hz sustained, "
                   "reason=%r", gate.get("sustained_hz"),
                   gate.get("override_reason"))
    emit("rate_gate", gate)


#: Preview poll interval. 150 ms (~7 Hz) is plenty for a dot that only
#: has to reassure a participant the calibration took.
PREVIEW_INTERVAL_S = 0.15
#: ...but the accuracy check MEASURES this same stream, and at 7 Hz a
#: 1.6 s window yields ~10 samples per target, of which the precision
#: metric is a sample-to-sample RMS. Precision quoted at 7 Hz is not
#: comparable to any published figure and overstates scatter, because
#: 150 ms of drift accumulates between consecutive samples. The tracker
#: itself runs at 30 Hz, so the resolution is there — it was being
#: thrown away by the poll interval. Validation asks for this instead.
VALIDATION_INTERVAL_S = 1.0 / 30.0
#: Never poll faster than the tracker can produce, or the same sample is
#: emitted repeatedly and precision reads as artificially perfect.
MIN_PREVIEW_INTERVAL_S = 1.0 / 60.0


@socketio.on("start_gaze_preview")
def handle_start_gaze_preview(_payload=None):
    """Stream live gaze estimates to the browser so the participant can
    VERIFY the calibration worked: a dot on the page follows their gaze.

    ``payload["interval_s"]`` raises the rate for the accuracy check,
    which reads this same stream (see VALIDATION_INTERVAL_S). Runs until
    stopped or recording starts.
    """
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)
    try:
        interval = float((_payload or {}).get("interval_s")
                         or PREVIEW_INTERVAL_S)
    except (TypeError, ValueError):
        interval = PREVIEW_INTERVAL_S
    interval = max(MIN_PREVIEW_INTERVAL_S, min(1.0, interval))
    state["preview_interval_s"] = interval
    # RACE FIX. The old code returned early if a preview was still
    # "active", but stop_gaze_preview only sets a flag the loop checks
    # every 150 ms. Stopping and immediately restarting — exactly what
    # the accuracy check does — therefore hit a window where the new
    # start was refused AND the old loop then exited, leaving NO preview
    # running. The validation would silently collect few or no samples
    # and report a meaningless error. A generation counter removes the
    # window: the newest loop always wins, older ones retire themselves.
    generation = state.get("preview_generation", 0) + 1
    state["preview_generation"] = generation
    state["preview_active"] = True

    def _loop() -> None:
        misses = 0
        while state.get("preview_active") \
                and state.get("preview_generation") == generation \
                and not state.get("recording"):
            info = gaze_service.gaze_info()
            if info is None:
                misses += 1
                if misses > 5:
                    break
            else:
                x, y = info.get("x", 0), info.get("y", 0)
                corr = state.get("correction")
                if corr and info.get("detected"):
                    x, y = _apply_point(x, y, corr)
                socketio.emit(
                    "gaze_preview",
                    {
                        "detected": info.get("detected", False),
                        "x": x,
                        "y": y,
                    },
                    to=sid,
                )
            socketio.sleep(state.get("preview_interval_s",
                                     PREVIEW_INTERVAL_S))
        # Only the CURRENT generation may clear the shared flag; a
        # retiring older loop must not switch off its replacement.
        if state.get("preview_generation") == generation:
            state["preview_active"] = False
        logger.info("Gaze preview loop ended (gen %d) – sid=%s",
                    generation, sid)

    logger.info("Gaze preview started at %.0f Hz – sid=%s",
                1.0 / max(1e-6, state.get("preview_interval_s",
                                          PREVIEW_INTERVAL_S)), sid)
    socketio.start_background_task(_loop)


@socketio.on("stop_gaze_preview")
def handle_stop_gaze_preview(_payload=None):
    sid = request.sid  # type: ignore[attr-defined]
    _get_session_state(sid)["preview_active"] = False


@socketio.on("start_position_check")
def handle_start_position_check(_payload=None):
    """Stream head-position guidance (~2 Hz) before calibration, so the
    participant can reach the optimal, camera-at-eye-level pose. The last
    detected geometry is stored and written to the session manifest,
    replacing the blind viewing-distance assumption where possible."""
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)
    if state.get("position_active"):
        return
    state["position_active"] = True

    def _loop() -> None:
        misses = 0
        while state.get("position_active") and not state.get("recording"):
            info = gaze_service.position_info()
            if info is None:
                misses += 1
                if misses > 3:
                    socketio.emit("position_info",
                                  {"available": False}, to=sid)
                    break
            else:
                if info.get("available") and info.get("face"):
                    state["position_snapshot"] = {
                        k: info.get(k) for k in
                        ("est_distance_cm", "inter_ocular_px", "eyes_y",
                         "face_center_x", "face_center_y", "assumed_hfov_deg")
                    }
                socketio.emit("position_info", info, to=sid)
            socketio.sleep(0.5)
        state["position_active"] = False

    socketio.start_background_task(_loop)


def _persist_llm_result(session: str, stimulus: str, block: dict) -> None:
    """Write the LLM result AND its correspondence score into the manifest.

    Scoring happens here, at write time, rather than being left to a
    separate command someone has to remember. RQ3's headline number is
    "of the claims that could be checked, what share does the recorded
    gaze support" — computing it automatically means a session either
    has that number or visibly does not.

    Never raises: feedback that was generated must not be lost because
    the scoring step failed.
    """
    if not session:
        return
    path = os.path.join(GAZEFOLLOWER_CSV_DIR, "%s_manifest.json" % session)
    if not os.path.isfile(path):
        logger.warning("No manifest at %s — LLM result not persisted", path)
        return
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)

        try:
            import claim_check

            claims = block.get("structured") or []
            samples, err = claim_check.load_gaze(manifest, path, stimulus)
            acc, acc_src = claim_check._accuracy_deg(manifest)
            if claims and not err and acc:
                scr = manifest.get("screen") or {}
                dist = ((manifest.get("distance") or {}).get("cm")
                        or VIEWING_DISTANCE_CM)
                ppd = claim_check._px_per_degree(
                    int(scr.get("width_px") or 1920),
                    int(scr.get("height_px") or 1080),
                    float(scr.get("diag_inches") or 15.6), float(dist))
                rect = next(s["video_rect"] for s in manifest["stimuli"]
                            if s.get("stimulus") == stimulus)
                vw = int(rect.get("w") or 1920)
                vh = int(rect.get("h") or 1080)
                scored = claim_check.check_all(
                    claims, samples, acc, ppd, vw, vh,
                    frame_times=block.get("frame_times"))
                scored["accuracy_source"] = acc_src
                block["correspondence"] = scored
            else:
                block["correspondence"] = {
                    "error": err or ("no claims" if not claims
                                     else "no validation accuracy")}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Correspondence scoring failed")
            block["correspondence"] = {"error": str(exc)[:200]}

        manifest.setdefault("llm", {})[stimulus] = block
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        corr = (block.get("correspondence") or {}).get("correspondence_pct")
        logger.info("LLM result persisted to the manifest – %s / %s | "
                    "correspondence %s %%", session, stimulus, corr)
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist the LLM result")


def _session_distance(state: dict) -> dict:
    """The viewing distance in force for this session, and its provenance.

    Preference order, and the reason for it:

      pre_check   measured at the accuracy check whose error is the
                  canonical accuracy — same moment, same posture, so the
                  distance and the degrees it converts belong together.
      pre_fit     the earlier check, if pre_check has no usable reading.
      post        after the stimuli; still measured, still better than
                  an assumption.
      guide       the optional position guide, if someone opened it.
      assumed     nothing measured. Say so loudly: this is the state
                  every session was silently in.
    """
    vals = state.get("validations") or []
    order = ("pre_check", "pre_fit", "pre", "post")
    for phase in order:
        for v in vals:
            if v.get("phase") != phase:
                continue
            d = v.get("distance") or {}
            if d.get("cm"):
                out = dict(d)
                out["measured"] = True
                out["from_phase"] = phase
                return out
    snap = state.get("position_snapshot") or {}
    if snap.get("est_distance_cm"):
        return {"cm": snap["est_distance_cm"], "measured": True,
                "source": "position guide (inter-ocular)",
                "from_phase": "guide"}
    return {"measured": False, "cm": None,
            "assumed_cm": VIEWING_DISTANCE_CM,
            "reason": "no usable distance at any validation; every "
                      "degree figure in this session divides by the "
                      "assumed %.0f cm" % VIEWING_DISTANCE_CM}


@socketio.on("stop_position_check")
def handle_stop_position_check(_payload=None):
    sid = request.sid  # type: ignore[attr-defined]
    _get_session_state(sid)["position_active"] = False


@socketio.on("start_native_calibration")
def handle_start_native_calibration(_payload=None):
    """Run GazeFollower's native preview + calibration window.

    The calibration UI opens as a fullscreen pygame window on the
    participant's machine (the server and browser run on the same
    computer).  This blocks for as long as the participant needs, so it
    runs in a SocketIO background task; the result is pushed back via
    the ``native_calibration_result`` event.
    """
    sid = request.sid  # type: ignore[attr-defined]

    if not gaze_service.available:
        emit(
            "native_calibration_result",
            {
                "success": False,
                "error": (
                    "GazeFollower is not available on the server. "
                    "Install it with: pip install gazefollower"
                ),
            },
        )
        return

    opts = _calibration_options()   # session access needs request context

    def _run_calibration() -> None:
        result = gaze_service.calibrate(opts)
        socketio.emit("native_calibration_result", result, to=sid)
        logger.info("Native calibration finished: %s", result)
        _telemetry_event(sid, "calibration_finished",
                         success=bool((result or {}).get("success")),
                         error=(result or {}).get("error"))
        # The rate gate needs a calibration model, so it runs here —
        # never before calibration (see _run_rate_gate).
        if result.get("success"):
            _run_rate_gate(sid)

    logger.info("Native calibration requested – sid=%s, opts=%s", sid, opts)
    emit("native_calibration_started", {})
    socketio.start_background_task(_run_calibration)


@socketio.on("stop_recording")
def handle_stop_recording(payload: dict):
    """Stop recording and flush buffered data to Excel.

    Expected payload::

        {"stimulus_name": "video_happy.mp4"}
    """
    sid = request.sid  # type: ignore[attr-defined]
    state = _get_session_state(sid)

    stimulus_name = payload.get("stimulus_name", "unknown")
    participant_id = state.get("participant_id") or session.get("participant_id", "unknown")

    state["recording"] = False
    _telemetry_event(sid, "stimulus_end", stimulus=stimulus_name)

    # --- Mark stimulus offset in the continuous GazeFollower recording -------
    # (data is saved & segmented once, at the END of the whole session —
    # GazeFollower's API only permits a single save per session)
    if state.get("gazefollower_active"):
        k = len(state["stimulus_log"])
        gaze_service.end_stimulus(101 + 2 * k)
        state["stimulus_log"].append(
            {
                "stimulus": stimulus_name,
                "t_start_ns": state.get("current_t_start_ns", 0),
                "t_end_ns": time.time_ns(),
                "video_rect": state.get("current_video_rect"),
            }
        )

    logger.info(
        "Recording stopped – sid=%s, participant=%s, stimulus=%s, "
        "gazefollower=%s",
        sid,
        participant_id,
        stimulus_name,
        "recording continues (segmented at session end)"
        if state.get("gazefollower_active") else "inactive",
    )
    emit("recording_stopped", {"stimulus_name": stimulus_name})


# =========================================================================
# Entry point
# =========================================================================

# ── Build fingerprint ─────────────────────────────────────────────────
# This project is edited on one machine and RUN on another. A stale copy
# looks identical in the log right up until the line you are waiting for
# is silently absent, and the run gets read as a result rather than as a
# missing file. So the server states, at startup, which diagnostics the
# code it is actually executing contains.
INSTRUMENTATION = {
    "capture-loop": ("tracker_service.py", "served_hz_this_window"),
    "stage-split": ("tracker_service.py", "_install_stage_timers"),
    "validation-geometry": ("static/js/experiment.js", "this.geometry"),
    "validation-reentry-guard": ("static/js/experiment.js", "this.running"),
    "rate-history": ("app.py", "rate_history"),
}


def _log_build_fingerprint() -> None:
    """Log which instrumentation is present in the files being executed."""
    present, missing = [], []
    for name, (rel, needle) in INSTRUMENTATION.items():
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                (present if needle in fh.read() else missing).append(name)
        except OSError:
            missing.append("%s (unreadable)" % name)
    newest = 0.0
    for rel in {v[0] for v in INSTRUMENTATION.values()}:
        try:
            newest = max(newest, os.path.getmtime(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), rel)))
        except OSError:
            pass
    stamp = datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M") \
        if newest else "unknown"
    logger.info("Build: sources last modified %s | instrumentation present: "
                "%s", stamp, ", ".join(sorted(present)) or "none")
    if missing:
        logger.warning(
            "STALE BUILD — missing instrumentation: %s. This copy is older "
            "than the one those diagnostics were added to; its log will NOT "
            "contain them. Copy the updated files over before drawing any "
            "conclusion from this run.", ", ".join(sorted(missing)))


if __name__ == "__main__":
    import atexit

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(GAZEFOLLOWER_CSV_DIR, exist_ok=True)
    atexit.register(gaze_service.shutdown)

    # Run the tracker self-check in the background at startup so any
    # dependency problem is logged (data/server.log + console) before
    # the first participant even arrives.
    def _startup_check() -> None:
        result = gaze_service.self_check()
        if result["ok"]:
            logger.info("Tracker self-check PASSED: %s", result["report"])
        else:
            logger.error(
                "Tracker self-check FAILED — GazeFollower will not record! "
                "Details: %s", result["report"],
            )

    threading.Thread(target=_startup_check, daemon=True).start()

    _log_build_fingerprint()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║    Eye-Tracking Experiment Server        ║")
    port = int(os.environ.get("PORT", 5050))
    print(f"  ║    Open http://localhost:{port} in your    ║")
    print("  ║    browser to begin.                     ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    # allow_unsafe_werkzeug: this is a local, single-machine research
    # application, so Werkzeug's development server is acceptable.
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
