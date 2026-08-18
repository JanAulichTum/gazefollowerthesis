/**
 * Eye-Tracking Experiment — Client-Side Controller
 * =================================================
 * Handles the GazeFollower native calibration trigger (server-side
 * tracker), the MANDATORY pre/post accuracy validation, SocketIO
 * communication, and sequential video stimulus playback.
 *
 * Validation design (methods-chapter relevant):
 *   pre  — 5 targets immediately after calibration
 *   post — 3 targets after the last video (drift check)
 * Errors are reported in px AND degrees of visual angle; the px→degree
 * conversion uses the participant-entered screen diagonal (or a logged
 * default assumption) and the configured viewing distance.
 *
 * Technical University of Munich (TUM)
 */

/* global io */

/** Promise-based sleep. */
function sleepMs(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

// ──────────────────────────────────────────────────────────────
// Screen ↔ viewport coordinate conversion
// GazeFollower reports gaze in SCREEN coordinates (origin = display
// top-left); the page works in VIEWPORT coordinates. The difference is
// the window position plus the browser chrome (tab/URL bar). In
// fullscreen both offsets are ~0.
// ──────────────────────────────────────────────────────────────
function screenToViewportOffsets() {
    const chromeX = (window.outerWidth - window.innerWidth) / 2; // side borders
    const chromeY = window.outerHeight - window.innerHeight;      // top chrome
    return {
        x: window.screenX + chromeX,
        y: window.screenY + chromeY,
    };
}

// ──────────────────────────────────────────────────────────────
// px → degrees of visual angle
// ──────────────────────────────────────────────────────────────
// Measured viewing distance (cm) from the position guide, when
// available — overrides the assumed constant for the DVA conversion.
window.measuredDistanceCm = null;

function pxToDegrees(errPx) {
    const diagIn = window.screenDiagInches || 14;
    const distCm = window.measuredDistanceCm || window.viewingDistanceCm || 60;
    const ppi = Math.hypot(window.screen.width, window.screen.height) / diagIn;
    const errCm = (errPx / ppi) * 2.54;
    return 2 * Math.atan2(errCm, 2 * distCm) * (180 / Math.PI);
}

// ──────────────────────────────────────────────────────────────
// SocketIO connection (shared across all pages)
// ──────────────────────────────────────────────────────────────
const socket = io();

socket.on('connect', () => {
    console.log('[SocketIO] Connected:', socket.id);
});

socket.on('disconnect', (reason) => {
    console.warn('[SocketIO] Disconnected:', reason);
});

socket.on('connect_error', (err) => {
    console.error('[SocketIO] Connection error:', err.message);
});


// ============================================================
// CALIBRATION PAGE — GazeFollower Native Calibration
// ============================================================
// The tracker runs server-side and owns the webcam. Its calibration
// opens a fullscreen NATIVE window on this machine; the browser merely
// triggers it and waits for the result.

/** Human-readable text for tracker progress stages. */
const NATIVE_STAGE_TEXT = {
    loading_model: 'Loading the gaze model — this can take a few minutes on slower laptops. Please wait…',
    model_ready: '✓ Eye tracker ready — you can start the calibration.',
    opening_window: 'Opening the fullscreen calibration window…',
    preview: 'Camera preview open — follow the instructions in the fullscreen window.',
    calibrating: 'Calibrating — look at the dots in the fullscreen window.',
    finished: 'Finishing up…',
};

class NativeCalibration {
    constructor() {
        /** @type {HTMLElement} */
        this.statusEl = document.getElementById('nativeCalStatus');
        /** @type {HTMLElement} */
        this.loadingEl = document.getElementById('nativeCalLoading');
        /** @type {HTMLButtonElement} */
        this.startBtn = document.getElementById('startNativeCal');
        /** @type {HTMLButtonElement} */
        this.validateBtn = document.getElementById('runValidationBtn');
        /** @type {HTMLButtonElement} */
        this.startVideosBtn = document.getElementById('startVideosBtn');
        /** @type {HTMLElement} */
        this.previewDot = document.getElementById('gazePreviewDot');
        /** Gain-correction UI */
        this.gainControl = document.getElementById('gainControl');
        this.gainSlider = document.getElementById('gainSlider');
        this.gainValue = document.getElementById('gainValue');
        this.gainSource = document.getElementById('gainSource');
        this.gainAutoBtn = document.getElementById('gainAutoBtn');
        /** Position guide */
        this.positionBtn = document.getElementById('positionCheckBtn');
        this.positionPanel = document.getElementById('positionPanel');
        this.positionVerdict = document.getElementById('positionVerdict');
        this.positionGuidance = document.getElementById('positionGuidance');
        this.positionMetrics = document.getElementById('positionMetrics');
        this.positionActive = false;
        /** Pre-session rate gate */
        this.ratePanel = document.getElementById('ratePanel');
        this.rateVerdict = document.getElementById('rateVerdict');
        this.rateDetail = document.getElementById('rateDetail');
        this.rateRetryBtn = document.getElementById('rateRetryBtn');
        this.rateOverrideBtn = document.getElementById('rateOverrideBtn');
        this.rateGate = null;
    }

    /**
     * Render the rate-gate verdict and gate the ACCURACY CHECK on it.
     *
     * The gate runs after calibration (GazeFollower cannot produce gaze
     * samples without a calibration model), so the button it guards is
     * the accuracy check — the next step toward the videos — not the
     * calibration button.
     *
     * Deliberately a WARNING with an explicit override rather than a hard
     * block: the participant is sitting there, and pilot/demo runs are
     * legitimate. The override is recorded in the manifest, so a degraded
     * session can never be mistaken for a clean one later.
     */
    /** Show the "measuring…" state in the rate panel. */
    showRateMeasuring(text) {
        if (!this.ratePanel) return;
        this.ratePanel.hidden = false;
        this.rateVerdict.textContent = text || 'Measuring the sampling rate…';
        this.rateVerdict.className = 'position-panel__verdict';
        this.rateDetail.textContent =
            'Running in the background — carry on with the accuracy check. '
            + 'The result appears here in about 25 s.';
    }

    renderRateGate(g) {
        if (!this.ratePanel) return;
        this.rateGate = g;
        this.ratePanel.hidden = false;
        const blocked = g && g.ok !== false && !g.passes && !g.overridden;

        if (g && g.ok === false) {
            this.rateVerdict.textContent = 'Sampling rate not measured';
            this.rateVerdict.className = 'position-panel__verdict';
            this.rateDetail.textContent = (g.error || 'unavailable')
                + ' — you can continue, but the recording rate is unknown.';
        } else if (g && g.passes) {
            this.rateVerdict.textContent =
                '✓ Sampling rate OK — ' + g.sustained_hz + ' Hz sustained';
            this.rateVerdict.className =
                'position-panel__verdict position-panel__verdict--ok';
            this.rateDetail.textContent =
                'Measured over ' + g.measured_s + ' s'
                + (g.detected_pct !== null && g.detected_pct !== undefined
                    ? ', ' + g.detected_pct + ' % of frames detected' : '')
                + '. Threshold is ' + g.min_sampling_hz + ' Hz.';
        } else if (g) {
            this.rateVerdict.textContent =
                '⚠ Sampling rate too low — ' + g.sustained_hz + ' Hz '
                + '(need ' + g.min_sampling_hz + ' Hz)';
            this.rateVerdict.className =
                'position-panel__verdict position-panel__verdict--bad';
            // Say WHICH problem it is rather than guessing. A frame that
            // arrives but yields no gaze estimate is a detection failure
            // (fix the seating and lighting); a low frame rate with good
            // detection is a compute problem (fix power/load). They look
            // identical in the rate alone.
            const det = g.detected_pct;
            let why;
            if (g.bursty) {
                // A peak above the camera's rated rate is impossible from
                // live capture: frames queued in the driver buffer during
                // a stall and were then read back-to-back. Those frames
                // are STALE, so anything timing-sensitive measured during
                // a burst — an accuracy check especially — is invalid.
                why = 'Frames are arriving in BURSTS (peaks of '
                    + g.peak_hz + ' Hz, faster than the camera’s '
                    + (g.nominal_camera_fps || 30) + ' fps, which is only '
                    + 'possible if buffered frames were read back-to-back '
                    + 'after a stall). The pipeline is stalling '
                    + 'intermittently, and buffered frames are stale. '
                    + 'Close other applications, then set '
                    + 'GF_CAMERA_FIX=1 (it caps the driver buffer at one '
                    + 'frame) and measure again. Treat any accuracy check '
                    + 'taken during this period as invalid.';
            } else if (det !== null && det !== undefined && det < 85) {
                why = 'Only ' + det + ' % of camera frames produced a gaze '
                    + 'estimate — the camera is keeping up, but your face '
                    + 'or eyes are not being detected reliably. Fix the '
                    + 'SETUP, not the computer: raise the camera to eye '
                    + 'level, sit about 60 cm away so your face fills more '
                    + 'of the frame, and put light on your face rather '
                    + 'than behind you. Then measure again.';
            } else if (g.camera_throttled) {
                // The camera was MEASURED delivering slowly to the
                // callback while the callback itself was cheap. Only
                // this combination justifies naming the camera — an
                // earlier version inferred it from cheap models alone
                // and was wrong, because "cheap models" is not "cheap
                // frame".
                why = 'The camera delivered only ' + g.delivered_hz
                    + ' Hz to the tracker, while the per-frame work took '
                    + g.work_ms_median + ' ms of the '
                    + g.frame_interval_ms + ' ms between frames ('
                    + g.pipeline_duty_pct + ' % duty). This is a CAMERA '
                    + 'problem, not a compute problem: in dim light a '
                    + 'webcam lengthens its exposure, and because it '
                    + 'cannot expose for longer than one frame it halves '
                    + 'the frame rate instead (30 → 15). Put a lamp on '
                    + 'your FACE (not behind you, not aimed at the '
                    + 'screen), raise the screen brightness, and measure '
                    + 'again. Changing the power plan will not help.';
            } else if (g.frames_discarded) {
                why = 'The camera delivered ' + g.delivered_hz + ' Hz and '
                    + 'the per-frame work took only ' + g.work_ms_median
                    + ' ms of the ' + g.frame_interval_ms + ' ms between '
                    + 'frames, yet only ' + g.sustained_hz + ' Hz of gaze '
                    + 'samples came out — so frames are arriving and being '
                    + 'discarded. Neither the lighting nor the power plan '
                    + 'will help. Restart the session; if it persists, run '
                    + '"python diagnose_rate.py" and check the subscriber '
                    + 'count in the log.';
            } else if (g.cpu_throttled || g.turbo_drop) {
                why = 'Frames arrived at ' + g.initial_hz + ' Hz at first '
                    + 'and settled to ' + g.sustained_hz + ' Hz, with '
                    + (det === null || det === undefined ? 'good' : det + ' %')
                    + ' detection, and the per-frame work ('
                    + g.work_ms_median + ' ms) is filling the '
                    + g.frame_interval_ms + ' ms frame interval — so the '
                    + 'COMPUTER is the limit. Because that work happens '
                    + 'inside the capture loop, going over the frame '
                    + 'budget makes the rate halve rather than sag. Check '
                    + 'the log says "perf_mode … ACTIVE", close other '
                    + 'apps, set the power plan to best performance, and '
                    + 'measure again.';
            } else if (g.bottleneck_unclear) {
                why = 'Frames settled to ' + g.sustained_hz + ' Hz, and '
                    + 'the per-frame work (' + g.work_ms_median + ' ms of '
                    + g.frame_interval_ms + ' ms) is NOT the limit — but '
                    + 'the camera’s own delivery rate could not be '
                    + 'measured, so the camera and downstream frame loss '
                    + 'cannot be told apart yet. Stop the server and run '
                    + '"python camera_remedy.py" to measure the camera '
                    + 'directly.';
            } else {
                why = 'Frames are arriving slowly (' + g.sustained_hz
                    + ' Hz) with '
                    + (det === null || det === undefined ? 'unknown' : det + ' %')
                    + ' detection. Close other apps, check AC power, and '
                    + 'run "python diagnose_rate.py" to see which pipeline '
                    + 'stage is responsible.';
            }
            this.rateDetail.textContent =
                why + ' Fixation timing is unreliable below '
                + g.min_sampling_hz + ' Hz.';
        }
        if (this.rateOverrideBtn) {
            this.rateOverrideBtn.hidden = !blocked;
        }
        if (this.rateRetryBtn) {
            this.rateRetryBtn.hidden = !blocked;
        }
        // Gate the VIDEOS, not the accuracy check. The accuracy check is
        // cheap and repeatable; the videos are the irreplaceable part, so
        // that is where a failing rate has to stop things. Nothing in the
        // flow ever waits on this measurement.
        if (this.startVideosBtn) {
            this.startVideosBtn.dataset.rateBlocked = blocked ? '1' : '';
            if (blocked) {
                this.startVideosBtn.disabled = true;
                this.startVideosBtn.title =
                    'Sampling rate is below the preregistered threshold — '
                    + 'fix the setup and measure again, or record anyway.';
            } else {
                this.startVideosBtn.title = '';
            }
        }
    }

    start() {
        console.log('[NativeCal] Ready.');

        // Pre-load the gaze model + camera NOW, while the participant
        // reads the instructions — clicking the button later is then
        // near-instant instead of a minutes-long apparent freeze.
        socket.emit('warmup_tracker', {});
        this.setLoading(true);
        this.setStatus('Preparing the eye tracker (loading model)…', 'unknown');

        socket.on('tracker_warmed', (data) => {
            if (data && data.ok) {
                this.setLoading(false);
                this.setStatus('✓ Eye tracker ready — click the button below to calibrate.', 'detected');
            }
            // Failure details arrive via the calibration pre-flight check,
            // so no scary message here — the button stays usable.
        });

        // ── Pre-session rate gate ──
        socket.on('rate_gate', (g) => {
            console.log('[RateGate]', g);
            clearTimeout(this._rateGateTimer);
            this.renderRateGate(g);
            if (g && g.passes) {
                this.setStatus(
                    '✓ Sampling rate OK — now run the required accuracy '
                    + 'check.', 'detected');
            }
        });
        if (this.rateRetryBtn) {
            this.rateRetryBtn.addEventListener('click', () => {
                this.showRateMeasuring('Measuring again…');
                this.rateRetryBtn.hidden = true;
                this.rateOverrideBtn.hidden = true;
                socket.emit('run_rate_gate', {});
            });
        }
        if (this.rateOverrideBtn) {
            this.rateOverrideBtn.addEventListener('click', () => {
                const reason = window.prompt(
                    'This session will be flagged as low-rate. Why record '
                    + 'anyway? (e.g. pilot run, demo)', '') || '';
                socket.emit('rate_gate_override', { reason: reason });
            });
        }

        // Live progress stages from the tracker subprocess
        socket.on('native_calibration_status', (msg) => {
            const stage = msg && msg.stage;
            console.log('[NativeCal] stage:', stage);
            const text = NATIVE_STAGE_TEXT[stage];
            if (text) {
                this.setStatus(text, stage === 'model_ready' ? 'detected' : 'unknown');
            }
            this.setLoading(!(stage === 'model_ready' || stage === 'finished'));
        });

        // ── Head-position guide (optional, before calibration) ──
        if (this.positionBtn) {
            this.positionBtn.addEventListener('click', () => {
                if (this.positionActive) {
                    this.positionActive = false;
                    socket.emit('stop_position_check', {});
                    this.positionPanel.hidden = true;
                    this.positionBtn.textContent = 'Check my position (recommended)';
                } else {
                    this.positionActive = true;
                    this.positionPanel.hidden = false;
                    this.positionVerdict.textContent = 'Starting camera…';
                    this.positionVerdict.className = 'position-panel__verdict';
                    socket.emit('start_position_check', {});
                    this.positionBtn.textContent = 'Hide position check';
                }
            });
            socket.on('position_info', (d) => this.renderPosition(d));
        }

        this.startBtn.addEventListener('click', () => {
            // Stop the position check when calibration begins (frees the
            // tracker for the calibration window).
            if (this.positionActive) {
                this.positionActive = false;
                socket.emit('stop_position_check', {});
                if (this.positionPanel) this.positionPanel.hidden = true;
            }
            // Go fullscreen for the WHOLE flow from here on: with the
            // browser fullscreen, screen coordinates == viewport
            // coordinates, so the validation targets (and any gaze/page
            // mapping) have no window-position or URL-bar offset.
            try {
                if (!document.fullscreenElement) {
                    document.documentElement.requestFullscreen()
                        .catch((e) => console.warn('[NativeCal] Fullscreen denied:', e));
                }
            } catch (e) {
                console.warn('[NativeCal] Fullscreen unavailable:', e);
            }
            this.startBtn.disabled = true;
            this.setLoading(true);
            this.setStatus('Starting calibration…', 'unknown');
            socket.emit('start_native_calibration', {});
        });

        socket.on('native_calibration_started', () => {
            console.log('[NativeCal] Calibration window opening…');
        });

        socket.on('native_calibration_result', (data) => {
            this.setLoading(false);
            if (data && data.success) {
                console.log('[NativeCal] ✓ Calibration succeeded.');
                this.setStatus(
                    '✓ Eye tracker calibrated — the green dot should now follow '
                    + 'your gaze. Next, run the required accuracy check.',
                    'detected');
                this.validateBtn.disabled = false;
                if (this.gainControl) this.gainControl.hidden = false;
                this.startGazeVerification();
                // The rate measurement runs in the BACKGROUND from here.
                // Nothing waits for it: the participant goes straight
                // into the accuracy check, and the measurement happens
                // underneath (which also samples the rate under
                // realistic load rather than while idle). The verdict
                // gates the VIDEOS, not the accuracy check — that is the
                // point where a bad rate would actually cost you data.
                this.showRateMeasuring();
            } else {
                const err = (data && data.error) || 'Unknown error';
                console.error('[NativeCal] Calibration failed:', err);
                this.setStatus('✗ Calibration failed: ' + err + ' — please retry.', 'not-detected');
                this.startBtn.disabled = false;
                this.startBtn.textContent = 'Retry Eye-Tracker Calibration';
            }
        });

        // Mandatory pre-validation → enables the videos.
        //
        // TWO checks, run back to back as ONE action. The first fits the
        // gain correction on grid A; the second measures the corrected
        // accuracy on grid B, which the correction has never seen.
        //
        // They are deliberately chained rather than offered as two
        // buttons. A button that can be pressed again is a button that
        // gets pressed until the number looks good, and choosing the
        // best of several attempts is optimisation on the primary
        // outcome measure — the single easiest thing for an examiner to
        // attack. One press, two checks, both recorded, no discretion.
        this.validateBtn.addEventListener('click', () => {
            this.stopGazeVerification();
            this.validateBtn.disabled = true;
            window.__validation.run('pre_fit', () => {
                // The server has now fitted the correction from grid A.
                // Grid B measures what it generalises to.
                window.__validation.run('pre_check', () => {
                    // Validation submitted (pass or fail — the result is
                    // logged in the session manifest either way).
                    // Respect a failing rate gate: the accuracy check
                    // never waits for the measurement, but the VIDEOS
                    // do.
                    this.startVideosBtn.disabled =
                        this.startVideosBtn.dataset.rateBlocked === '1';
                    this.startGazeVerification();
                });
            });
        });

        // Start the videos (only reachable after the pre-validation)
        this.startVideosBtn.addEventListener('click', () => {
            this.stopGazeVerification();
            this.beginExperiment();
        });

        // ── Gain correction (auto-fit + manual slider) ──
        // Server pushes the current correction state; keep UI in sync.
        socket.on('gain_correction', (data) => {
            if (!this.gainSlider) return;
            if (data && data.active) {
                const g = data.gain_mean || 1.0;
                this.gainSlider.value = String(g);
                this.gainValue.textContent = '×' + g.toFixed(2);
                this.gainSource.textContent = '(' + (data.source || '') + ')';
                if (data.source && data.source.indexOf('auto') === 0) {
                    this.gainAutoBtn.disabled = false;
                    this.setStatus(
                        '✓ Automatic gain correction fitted (×' + g.toFixed(2)
                        + ') — the green dot should now reach the screen '
                        + 'edges. Re-run the accuracy check to verify.',
                        'detected');
                }
            } else {
                this.gainValue.textContent = '×1.00';
                this.gainSource.textContent = '(off)';
            }
        });

        if (this.gainSlider) {
            this.gainSlider.addEventListener('input', () => {
                const g = parseFloat(this.gainSlider.value) || 1.0;
                this.gainValue.textContent = '×' + g.toFixed(2);
                this.gainSource.textContent = '(manual)';
                socket.emit('set_gain_correction', {
                    gain: g,
                    center_x: window.screen.width / 2,
                    center_y: window.screen.height / 2,
                });
            });
        }
        if (this.gainAutoBtn) {
            this.gainAutoBtn.addEventListener('click', () => {
                socket.emit('set_gain_correction', { mode: 'auto' });
            });
        }
    }

    /** Render a head-position update from the tracker. */
    renderPosition(d) {
        if (!this.positionPanel) return;
        if (!d || d.available === false) {
            let msg = 'Live position check unavailable on this setup — '
                + 'please follow the written positioning tips above.';
            if (d && d.reason) msg += ' (' + d.reason + ')';
            this.positionVerdict.textContent = msg;
            this.positionVerdict.className = 'position-panel__verdict';
            this.positionGuidance.innerHTML = '';
            this.positionMetrics.textContent = '';
            return;
        }
        const ready = !!d.ready && d.face;
        this.positionVerdict.textContent = d.warming
            ? 'Starting camera…'
            : !d.face
                ? 'No face detected — center yourself in front of the camera.'
                : ready ? '✓ Good position — you can calibrate now.'
                        : 'Adjust your position:';
        this.positionVerdict.className = 'position-panel__verdict '
            + (ready ? 'position-panel__verdict--ok'
                     : 'position-panel__verdict--warn');
        this.positionGuidance.innerHTML = '';
        (d.guidance || []).forEach((g) => {
            const li = document.createElement('li');
            li.textContent = g;
            this.positionGuidance.appendChild(li);
        });
        const bits = [];
        if (d.est_distance_cm) {
            bits.push('~' + d.est_distance_cm + ' cm'
                + (d.assumed_hfov_deg ? ' (' + d.assumed_hfov_deg + '° FOV)' : ''));
        }
        if (d.roll_deg !== undefined && d.roll_deg !== null) {
            bits.push('head roll ' + d.roll_deg + '°');
        }
        if (d.openness_ratio) bits.push('eye symmetry ' + d.openness_ratio + '×');
        this.positionMetrics.textContent = bits.join(' · ');
        // Feed the measured distance into the DVA conversion so the
        // validation degrees rest on data, not the assumed 60 cm.
        if (d.est_distance_cm && d.est_distance_cm > 30
                && d.est_distance_cm < 120) {
            window.measuredDistanceCm = d.est_distance_cm;
        }
    }

    /** Hide phase 1 and run the experiment inside this page (keeps
     *  fullscreen — a navigation would exit it). */
    async beginExperiment() {
        document.getElementById('phase1').hidden = true;
        document.body.dataset.theme = 'dark';
        document.body.classList.add('experiment-running');
        const section = document.getElementById('experimentSection');
        if (section) section.hidden = false;

        try {
            if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen().catch(() => {});
            }
        } catch (e) { /* optional */ }

        let list = [];
        let data = {};
        try {
            const resp = await fetch('/api/stimuli');
            data = await resp.json();
            list = data.stimuli || [];
        } catch (err) {
            console.error('[NativeCal] Could not fetch stimuli, falling back:', err);
            window.location.href = '/stimuli';   // old two-page flow
            return;
        }

        const runner = new ExperimentRunner(list, {
            inPage: true,
            testMode: !!data.test_mode,
            testSeconds: data.test_video_seconds || 5,
        });
        runner.start();
    }

    /** Live post-calibration check: a green dot follows the gaze. */
    startGazeVerification() {
        socket.on('gaze_preview', (data) => {
            if (!this.previewDot) return;
            if (data && data.detected) {
                // Gaze arrives in SCREEN coordinates (logical px = CSS px,
                // no devicePixelRatio scaling) — convert to viewport
                // coordinates by subtracting window position + browser
                // chrome, otherwise the dot is offset by the tab/URL bar.
                const off = screenToViewportOffsets();
                this.previewDot.hidden = false;
                this.previewDot.style.left = (data.x - off.x) + 'px';
                this.previewDot.style.top = (data.y - off.y) + 'px';
            } else {
                this.previewDot.hidden = true;
            }
        });
        socket.emit('start_gaze_preview', {});
    }

    stopGazeVerification() {
        socket.emit('stop_gaze_preview', {});
        socket.off('gaze_preview');
        if (this.previewDot) this.previewDot.hidden = true;
    }

    setStatus(text, kind) {
        this.statusEl.textContent = text;
        this.statusEl.className = 'detection-status detection-status--' + kind;
    }

    /** Show/hide the indeterminate loading bar. */
    setLoading(active) {
        if (this.loadingEl) this.loadingEl.hidden = !active;
    }
}


// ============================================================
// MANDATORY ACCURACY VALIDATION (pre & post)
// Targets at known positions; the median measured gaze per target is
// compared against the target. Results (px AND degrees of visual
// angle) are sent to the server and stored in the session manifest —
// per-session accuracy is a primary outcome of a validation study.
// ============================================================

// Pre-validation deliberately samples FIVE vertical elevations
// (12/31/50/69/88 %), not three: the webcam up-gaze overshoot is
// nonlinear, and extra elevations let the quadratic vertical correction
// characterize and remove it. Post keeps three targets for a quick
// drift check.
//
// THIRTEEN targets across FIVE vertical elevations, not seven
// (extended 2026-08-18, F33/brief item 2). A per-axis correction cannot
// represent m_yx — vertical error caused by HORIZONTAL position, the
// shear a participant reported before any metric did ("when looking
// right the y axis behaves weirdly") — at any polynomial degree. A full
// 2x2 affine candidate can, but six free parameters need more than
// seven targets can support under leave-one-out: F33 measured 4-7% gain
// at n=7 (indistinguishable from noise) against 15-28% pooling to ~14
// targets, on exactly the two sessions that show shear and nowhere
// else. That gate was re-verified independently against the nine
// sessions recorded under the 7-target protocol before this grid was
// extended — see validation_stats.FULL_AFFINE_MIN_TARGETS and the
// commit that added the full-affine candidate.
//
// The extra six targets add x-DIVERSITY at elevations that previously
// had only one x value each (12/31/69 had a single x besides the
// corners), which is what lets m_yx be estimated at more than one
// height instead of only inferred from the two 12%-elevation corners.
// Costs ~15 s of extra validation time per session. Keep this grid and
// VALIDATION_CHECK_GRID DISJOINT (run_tests.py [17] asserts it) and the
// two grids' eccentricity matched (also asserted) — a check grid that
// is easier than the fit grid would turn "the correction generalises"
// into "the second grid was nearer the centre".
const VALIDATION_GRID = [
    [12, 12], [88, 12], [35, 12], [65, 12],
    [50, 31], [20, 31], [80, 31],
    [15, 50], [85, 50],
    [50, 69], [30, 69], [70, 69],
    [50, 88],
];

// ── The CHECK grid: different points, same eccentricity ─────────────
// Fitting the gain correction on grid A and then reporting the error at
// grid A is scoring the fit on its own training points. Measuring again
// at the SAME positions with fresh samples is better — the samples are
// new — but it is still not a generalisation estimate, because the
// correction was tuned to minimise error at exactly those seven
// locations. The obvious question at a defence is "of course it does
// well there; what does it do everywhere else?"
//
// So the second check uses seven DIFFERENT positions. They are chosen
// to interleave with grid A and to match it on both eccentricity
// measures, so the two accuracies are comparable rather than merely
// different:
//
//     mean |x − 50|   A 21.2    B 20.7
//     mean |y − 50|   A 23.4    B 21.7
//     distinct y      A 5       B 5      (12/31/50/69/88)
//     shared points   0
//
// Grid A grew from 7 to 13 targets (2026-08-18, F33/brief item 2); the
// figures above are grid A's post-extension eccentricity, still within
// run_tests.py [17]'s 2.0 tolerance of grid B's — grid B itself did NOT
// change and stays at 7, so drift (which pairs pre_check with post,
// both grid B) is unaffected.
//
// Same difficulty, no overlap. The error measured here is out of sample
// in BOTH space and time, which is the figure a corrected accuracy
// claim needs.
//
// If you edit these, keep the two eccentricity means within ~1 % of
// grid A's and keep the intersection EMPTY — run_tests asserts both.
// A B-grid that is easier than A would turn "the correction
// generalises" into "the second grid was nearer the centre".
const VALIDATION_CHECK_GRID = [
    [50, 12], [15, 31], [85, 31], [50, 50], [20, 69], [80, 69], [65, 88],
];

// THE THREE CHECKS, and why each exists.
//
//   pre_fit    grid A. Uncorrected. This is the tracker's NATIVE
//              accuracy and it is also the set the gain correction is
//              fitted on. Reported as the raw figure.
//   pre_check  grid B. Correction active, evaluated at positions it was
//              never fitted to. This is the CORRECTED accuracy that can
//              be defended.
//   post       grid B. Correction active. Differenced against pre_check
//              on the UNCORRECTED basis to give drift.
//
// post uses grid B, not A, because drift is only a like-for-like
// comparison when both ends sit at identical screen eccentricities —
// and its partner is pre_check.
const VALIDATION_POSITIONS = {
    pre_fit: VALIDATION_GRID,
    pre_check: VALIDATION_CHECK_GRID,
    post: VALIDATION_CHECK_GRID,
    // Legacy alias: sessions recorded before the split used 'pre'.
    pre: VALIDATION_GRID,
};

class ValidationTest {
    constructor() {
        this.overlay = document.getElementById('trackingTest');
        this.target = document.getElementById('testTarget');
        this.info = document.getElementById('testInfo');
        this.results = document.getElementById('testResults');
        this.marks = document.getElementById('testMarks');
        this.verdictEl = document.getElementById('testVerdict');
        this.summaryEl = document.getElementById('testSummary');
        this.gazeDot = document.getElementById('testGazeDot');
        this.closeBtn = document.getElementById('testCloseBtn');

        this.samples = [];
        this.collecting = false;
        this.onGaze = this.onGaze.bind(this);
        this.onDone = null;
        this.running = false;      // re-entrancy guard for run()
        this.geometry = null;      // viewport state captured per run

        this.closeBtn.addEventListener('click', () => this.close());
    }

    onGaze(data) {
        if (!data || !data.detected) return;
        const off = screenToViewportOffsets();
        const x = data.x - off.x;
        const y = data.y - off.y;
        this.gazeDot.hidden = false;
        this.gazeDot.style.left = x + 'px';
        this.gazeDot.style.top = y + 'px';
        if (this.collecting) this.samples.push([x, y]);
    }

    /**
     * Run the validation sequence.
     * @param {'pre'|'post'} phase
     * @param {function} [onDone] — called after the results are closed.
     */
    async run(phase, onDone) {
        // Re-entrancy guard. When the overlay froze, clicking anything
        // could start a SECOND run on top of the first — which is what
        // "it looped" was. One check at a time.
        if (this.running) {
            console.warn('[Validation] already running — ignoring re-entry');
            return;
        }
        this.running = true;
        console.log('[Validation] Starting (' + phase + ')…');
        this.phase = phase;
        this.onDone = onDone || null;
        const positions = VALIDATION_POSITIONS[phase] || VALIDATION_POSITIONS.pre;

        socket.on('gaze_preview', this.onGaze);
        // Ask for the FULL tracker rate, not the 7 Hz the reassurance
        // dot runs at. The accuracy check reads this same stream, and
        // its precision metric is a sample-to-sample RMS: at 7 Hz that
        // is ~10 samples per target with 150 ms of drift accumulating
        // between each pair, which inflates scatter and is not
        // comparable to a published precision figure. The tracker
        // already produces 30 Hz; the poll interval was discarding it.
        socket.emit('start_gaze_preview', { interval_s: 1 / 30 });

        // AWAIT the fullscreen transition. It was previously fired and
        // forgotten, so the FIRST target was positioned using the
        // WINDOWED window.innerWidth/innerHeight while the gaze samples
        // that followed arrived in the fullscreen frame — the target and
        // the gaze were in different coordinate systems. Entering
        // fullscreen also changes screenToViewportOffsets() from
        // (window position + chrome) to (0, 0), so both sides must be
        // read after the transition has settled.
        try {
            if (!document.fullscreenElement) {
                await document.documentElement.requestFullscreen()
                    .catch(() => {});
                // requestFullscreen resolves before layout settles in
                // some browsers; wait for a stable viewport size.
                let lastW = -1;
                for (let i = 0; i < 20 && lastW !== window.innerWidth; i++) {
                    lastW = window.innerWidth;
                    await sleepMs(50);
                }
            }
        } catch (e) { /* optional */ }

        // Geometry actually in force while measuring — recorded with the
        // result so a coordinate problem is visible afterwards instead of
        // being inferred.
        const offAtStart = screenToViewportOffsets();
        // On `this`, not a local: submit() reads it, and submit() is a
        // separate method. As a local it raised a ReferenceError AFTER
        // the last target, which — inside an async method with no
        // handler — rejected silently and left the overlay frozen on
        // "Look at the ring (7/7)" with no error anywhere.
        this.geometry = {
            fullscreen: !!document.fullscreenElement,
            inner: [window.innerWidth, window.innerHeight],
            outer: [window.outerWidth, window.outerHeight],
            screen: [window.screen.width, window.screen.height],
            offsets: [offAtStart.x, offAtStart.y],
            device_pixel_ratio: window.devicePixelRatio,
        };
        console.log('[Validation] geometry', this.geometry);

        this.overlay.hidden = false;
        this.results.hidden = true;
        this.target.hidden = false;

        const outcomes = [];
        for (let i = 0; i < positions.length; i++) {
            const tx = window.innerWidth * positions[i][0] / 100;
            const ty = window.innerHeight * positions[i][1] / 100;
            this.target.style.left = tx + 'px';
            this.target.style.top = ty + 'px';
            this.info.textContent =
                'Look at the ring (' + (i + 1) + ' / ' + positions.length + ')';

            await sleepMs(1000);           // settle on the new target
            this.samples = [];
            this.collecting = true;
            await sleepMs(1600);           // collect
            this.collecting = false;

            // n_samples is recorded even on failure: "0 samples" and
            // "samples landed in the wrong place" look identical in the
            // error alone, and they are completely different faults.
            let out = { tx, ty, ok: false, n: this.samples.length };
            if (this.samples.length >= 3) {
                const med = (arr) => arr.sort((a, b) => a - b)[Math.floor(arr.length / 2)];
                const mx = med(this.samples.map((s) => s[0]));
                const my = med(this.samples.map((s) => s[1]));
                // PRECISION, two ways, because one of them is not
                // comparable across sampling rates and this project
                // changed its rate.
                //
                // S2S — RMS of sample-to-sample distances. The common
                // reported metric, but it measures how far the signal
                // moves between CONSECUTIVE samples, so it depends on
                // how far apart in time those samples are. Raising the
                // poll rate from 7 Hz to 30 Hz exposes high-frequency
                // noise that decimation was hiding, and s2s goes UP
                // even though the underlying signal is unchanged. A
                // tracker that looks worse after you sample it better
                // has not got worse.
                //
                // SD — RMS distance from the target's own median
                // position. This is dispersion, not step size: it does
                // not care how many samples the cloud contains, so it
                // IS comparable across rates. Use it whenever comparing
                // sessions recorded at different rates.
                let s2s = 0;
                for (let k = 1; k < this.samples.length; k++) {
                    const d = Math.hypot(
                        this.samples[k][0] - this.samples[k - 1][0],
                        this.samples[k][1] - this.samples[k - 1][1]);
                    s2s += d * d;
                }
                const prec = Math.sqrt(s2s / (this.samples.length - 1));
                let sq = 0;
                for (const s of this.samples) {
                    sq += Math.pow(s[0] - mx, 2) + Math.pow(s[1] - my, 2);
                }
                const sd = Math.sqrt(sq / this.samples.length);
                out = {
                    tx, ty, mx, my, ok: true,
                    err: Math.hypot(mx - tx, my - ty),
                    prec: prec,
                    prec_sd: sd,
                    n: this.samples.length,
                };
            }
            outcomes.push(out);
        }

        socket.off('gaze_preview', this.onGaze);
        socket.emit('stop_gaze_preview', {});
        this.target.hidden = true;
        this.gazeDot.hidden = true;

        // An exception here used to reject this async method silently,
        // leaving the overlay frozen on the last target with nothing in
        // the console and no way forward. Never fail invisibly: show the
        // error and leave the Close button reachable so the participant
        // is not trapped mid-session.
        try {
            this.submit(outcomes);
            this.showResults(outcomes);
        } catch (err) {
            console.error('[Validation] failed after the last target:', err);
            this.info.textContent = '';
            this.verdictEl.textContent = '✗ The accuracy check could not be '
                + 'completed';
            this.summaryEl.textContent =
                'An internal error occurred after the last target ('
                + (err && err.message ? err.message : err) + '). The '
                + 'measurement was not recorded. Close this and try again; '
                + 'if it repeats, send the browser console output.';
            this.results.hidden = false;
        } finally {
            this.running = false;
        }
    }

    /** Send the validation result to the server (→ session manifest). */
    submit(outcomes) {
        const measured = outcomes.filter((o) => o.ok);
        const meanPx = measured.length
            ? measured.reduce((s, o) => s + o.err, 0) / measured.length
            : null;
        const meanPrec = measured.length
            ? measured.reduce((s, o) => s + (o.prec || 0), 0) / measured.length
            : null;
        socket.emit('validation_result', {
            phase: this.phase,
            // Viewport geometry in force while measuring — lets a
            // coordinate-space problem be diagnosed from the recorded
            // data rather than reproduced by hand.
            geometry: this.geometry || null,
            targets: outcomes.map((o) => ({
                tx: Math.round(o.tx), ty: Math.round(o.ty),
                mx: o.ok ? Math.round(o.mx) : null,
                my: o.ok ? Math.round(o.my) : null,
                err_px: o.ok ? Math.round(o.err * 10) / 10 : null,
                precision_px: o.ok ? Math.round(o.prec * 10) / 10 : null,
                n_samples: o.n || 0,
                precision_sd_px: o.prec_sd === undefined
                    ? null : Math.round(o.prec_sd * 10) / 10,
            })),
            targets_measured: measured.length,
            mean_err_px: meanPx === null ? null : Math.round(meanPx * 10) / 10,
            mean_err_deg: meanPx === null
                ? null : Math.round(pxToDegrees(meanPx) * 100) / 100,
            mean_precision_px: meanPrec === null
                ? null : Math.round(meanPrec * 10) / 10,
            mean_precision_deg: meanPrec === null
                ? null : Math.round(pxToDegrees(meanPrec) * 100) / 100,
            // Rate-INDEPENDENT precision: dispersion about each target's
            // own median, averaged. Compare this across sessions, not
            // the sample-to-sample figure above.
            mean_precision_sd_px: (function () {
                const v = measured.map((o) => o.prec_sd)
                    .filter((x) => x !== undefined && x !== null);
                return v.length
                    ? Math.round((v.reduce((a, b) => a + b, 0) / v.length)
                                 * 10) / 10
                    : null;
            })(),
            screen: {
                width_px: window.screen.width,
                height_px: window.screen.height,
                device_pixel_ratio: window.devicePixelRatio || 1,
                diag_inches: window.screenDiagInches || null,
                diag_assumed: !!window.screenDiagAssumed,
                viewing_distance_cm:
                    window.measuredDistanceCm || window.viewingDistanceCm || 60,
                viewing_distance_measured: !!window.measuredDistanceCm,
            },
        });
    }

    showResults(outcomes) {
        this.marks.innerHTML = '';
        const measured = outcomes.filter((o) => o.ok);

        outcomes.forEach((o) => {
            const ring = document.createElement('div');
            ring.className = 'test-mark-target';
            ring.style.left = o.tx + 'px';
            ring.style.top = o.ty + 'px';
            this.marks.appendChild(ring);
            if (o.ok) {
                const dot = document.createElement('div');
                dot.className = 'test-mark-gaze';
                dot.style.left = o.mx + 'px';
                dot.style.top = o.my + 'px';
                dot.title = Math.round(o.err) + ' px';
                this.marks.appendChild(dot);
                const label = document.createElement('span');
                label.className = 'test-mark-label';
                label.style.left = o.tx + 'px';
                label.style.top = (o.ty + 34) + 'px';
                label.textContent = Math.round(o.err) + ' px';
                this.marks.appendChild(label);
            }
        });

        let verdict;
        let summary;
        if (!measured.length) {
            verdict = '✗ No gaze detected';
            summary = 'The tracker did not deliver gaze data — check the '
                + 'camera and recalibrate.';
        } else {
            const mean = measured.reduce((s, o) => s + o.err, 0) / measured.length;
            const deg = pxToDegrees(mean);
            const threshold = window.maxValidationErrorDeg || 3.0;
            const pass = deg <= threshold;
            verdict = pass
                ? '✓ Accuracy OK (' + deg.toFixed(1) + '° ≤ ' + threshold + '°)'
                : '⚠ Accuracy above threshold (' + deg.toFixed(1) + '° > '
                  + threshold + '°) — recalibration recommended';
            const prec = measured.reduce((s, o) => s + (o.prec || 0), 0)
                / measured.length;
            summary = 'Blue rings = targets, green dots = your measured gaze '
                + '(median over 1.6 s). Accuracy: '
                + Math.round(mean) + ' px ≈ ' + deg.toFixed(2) + '° · '
                + 'Precision (RMS-S2S): ' + Math.round(prec) + ' px ≈ '
                + pxToDegrees(prec).toFixed(2) + '° across '
                + measured.length + ' of ' + outcomes.length
                + ' targets. The result was recorded.';

            // Region-specific hint: if the TOP targets are much worse
            // than the rest, the gaze reads too high near the top edge
            // (the classic webcam up-gaze overshoot) — tell the user the
            // concrete remedy before they proceed.
            const topH = window.innerHeight * 0.30;
            const top = measured.filter((o) => o.ty < topH);
            const rest = measured.filter((o) => o.ty >= topH);
            if (top.length && rest.length) {
                const topErr = top.reduce((s, o) => s + o.err, 0) / top.length;
                const restErr = rest.reduce((s, o) => s + o.err, 0) / rest.length;
                if (topErr > 1.8 * restErr && topErr > 120) {
                    summary += ' ⚠ Your gaze is tracked noticeably too high '
                        + 'near the top of the screen. This usually means '
                        + 'the webcam sits below eye level — raise your '
                        + 'laptop (or lower your chair) so the camera is at '
                        + 'eye height, then recalibrate for better accuracy '
                        + 'in the upper area.';
                }
            }
        }
        this.verdictEl.textContent = verdict;
        this.summaryEl.textContent = summary;
        this.info.textContent = '';
        this.results.hidden = false;
    }

    close() {
        this.overlay.hidden = true;
        const cb = this.onDone;
        this.onDone = null;
        if (cb) cb();
    }
}


// ============================================================
// EXPERIMENT — Sequential Video Playback
// ============================================================

class ExperimentRunner {
    /**
     * @param {string[]} stimuliList — Array of video filenames.
     * @param {{inPage?: boolean}} [options]
     *   inPage: running inside the calibration page (fullscreen already
     *   active, no start overlay, post-validation available).
     */
    constructor(stimuliList, options = {}) {
        /** @type {string[]} */
        this.stimuli = stimuliList;
        /** @type {boolean} */
        this.inPage = !!options.inPage;
        /** Test mode: only a few seconds of video are played */
        this.testMode = !!options.testMode;
        /** Seconds of video to play in test mode */
        this.testSeconds = options.testSeconds || 5;
        /** @type {number} */
        this.currentIndex = 0;

        /** @type {HTMLVideoElement} */
        this.videoPlayer = document.getElementById('videoPlayer');
        /** @type {HTMLElement} */
        this.interStimulus = document.getElementById('interStimulus');
        /** @type {HTMLElement} */
        this.progressIndicator = document.getElementById('progressIndicator');
        /** @type {HTMLElement} */
        this.currentVideoEl = document.getElementById('currentVideo');
        /** @type {HTMLElement} */
        this.totalVideosEl = document.getElementById('totalVideos');

        /** Duration of inter-stimulus rest screen in ms */
        this.REST_DURATION = 3000;
    }

    /**
     * Main entry point — begin playback.
     */
    async start() {
        console.log('[Runner] Starting experiment with', this.stimuli.length, 'stimuli.');

        if (!this.stimuli || this.stimuli.length === 0) {
            console.error('[Runner] No stimuli to play!');
            this.complete();
            return;
        }

        // Update total count
        if (this.totalVideosEl) {
            this.totalVideosEl.textContent = this.stimuli.length;
        }

        // Require one user click before playback: browsers block
        // autoplay with audio on pages the user hasn't interacted with.
        // In-page mode skips this (the start click just happened); if
        // that activation expires during the rest screen, the per-video
        // retry overlay asks for a fresh click instead.
        if (!this.inPage) {
            await this.waitForUserGesture();
        }

        // Begin sequential playback
        await this.playNextStimulus();
    }

    /**
     * Show the fullscreen overlay and resolve on the first click.
     * @param {string} [message] — optional replacement text.
     * @returns {Promise<void>}
     */
    waitForUserGesture(message) {
        const overlay = document.getElementById('startOverlay');
        if (!overlay) return Promise.resolve();
        const textEl = document.getElementById('startOverlayText');
        if (message && textEl) textEl.textContent = message;
        overlay.hidden = false;
        overlay.focus && overlay.focus();
        return new Promise((resolve) => {
            let done = false;
            const proceed = () => {
                if (done) return;
                done = true;
                overlay.hidden = true;
                // Fullscreen for distraction-free stimulus presentation
                // (page navigation exits fullscreen, so re-request here).
                try {
                    if (!document.fullscreenElement) {
                        document.documentElement.requestFullscreen()
                            .catch(() => { /* optional — continue windowed */ });
                    }
                } catch (e) { /* optional — continue windowed */ }
                resolve();
            };
            overlay.addEventListener('click', proceed, { once: true });
            // Keyboard accessibility: Enter or Space also proceeds
            overlay.addEventListener('keydown', (e) => {
                if (e.code === 'Enter' || e.code === 'Space') {
                    e.preventDefault();
                    proceed();
                }
            });
        });
    }

    /**
     * Play the next stimulus in the list, with inter-stimulus rest.
     */
    async playNextStimulus() {
        if (this.currentIndex >= this.stimuli.length) {
            this.complete();
            return;
        }

        const stimulusName = this.stimuli[this.currentIndex];
        console.log(`[Runner] Preparing stimulus ${this.currentIndex + 1}/${this.stimuli.length}: ${stimulusName}`);

        // Update progress
        if (this.currentVideoEl) {
            this.currentVideoEl.textContent = this.currentIndex + 1;
        }

        // ── Show inter-stimulus rest screen ──
        this.videoPlayer.hidden = true;
        this.interStimulus.hidden = false;
        this.progressIndicator.hidden = false;

        console.log('[Runner] Inter-stimulus rest (3s)…');
        await this.sleep(this.REST_DURATION);

        // ── Load and play video ──
        this.interStimulus.hidden = true;
        this.videoPlayer.hidden = false;

        // Served via the Flask /stimulus/<filename> route (supports HTTP
        // range requests; the videos live outside the static folder).
        const videoSrc = '/stimulus/' + encodeURIComponent(stimulusName);
        this.videoPlayer.src = videoSrc;
        console.log('[Runner] Loading video:', videoSrc);

        // Wait for video to be ready
        await new Promise((resolve) => {
            const onCanPlay = () => {
                this.videoPlayer.removeEventListener('canplay', onCanPlay);
                resolve();
            };
            this.videoPlayer.addEventListener('canplay', onCanPlay);
            this.videoPlayer.load();
        });

        // Start playback. If the browser still blocks it (autoplay
        // policy), ask for another click and retry.
        try {
            await this.videoPlayer.play();
            console.log('[Runner] ▶ Playback started:', stimulusName);
        } catch (err) {
            console.warn('[Runner] Playback blocked, requesting click:', err);
            await this.waitForUserGesture('Click to start the next video');
            try {
                await this.videoPlayer.play();
                console.log('[Runner] ▶ Playback started after click:', stimulusName);
            } catch (err2) {
                console.error('[Runner] Playback failed permanently:', err2);
            }
        }

        // If the video still isn't playing, skip it rather than hanging
        // forever on an 'ended' event that will never fire.
        if (this.videoPlayer.paused) {
            console.error('[Runner] Skipping unplayable stimulus:', stimulusName);
            this.currentIndex++;
            await this.playNextStimulus();
            return;
        }

        // Start recording (server-side GazeFollower)
        this.startRecording(stimulusName);

        // Wait for video to end. In TEST MODE, cut playback short after
        // a few seconds so the whole pipeline can be exercised quickly.
        await new Promise((resolve) => {
            let settled = false;
            const finish = () => {
                if (settled) return;
                settled = true;
                this.videoPlayer.removeEventListener('ended', finish);
                resolve();
            };
            this.videoPlayer.addEventListener('ended', finish);
            if (this.testMode) {
                setTimeout(() => {
                    console.log('[Runner] TEST MODE — cutting video after',
                        this.testSeconds, 's');
                    try { this.videoPlayer.pause(); } catch (e) { /* fine */ }
                    finish();
                }, this.testSeconds * 1000);
            }
        });

        console.log('[Runner] ■ Playback ended:', stimulusName);

        // Stop recording and wait for server acknowledgment
        await this.stopRecording(stimulusName);

        // Advance to next stimulus
        this.currentIndex++;
        await this.playNextStimulus();
    }

    /**
     * Compute the video's CONTENT rectangle in LOGICAL screen pixels
     * (object-fit: contain letterboxes the frame). GazeFollower's
     * calibrated gaze coordinates are in the same logical-pixel space
     * as CSS pixels, so NO devicePixelRatio scaling is applied.
     * Used server-side to convert gaze coordinates → normalized video
     * coordinates.
     */
    getVideoContentRect() {
        const v = this.videoPlayer;
        if (!v || !v.videoWidth) return null;
        const rect = v.getBoundingClientRect();
        const scale = Math.min(rect.width / v.videoWidth,
                               rect.height / v.videoHeight);
        const cw = v.videoWidth * scale;
        const ch = v.videoHeight * scale;
        const cx = rect.left + (rect.width - cw) / 2;
        const cy = rect.top + (rect.height - ch) / 2;
        // Convert viewport → SCREEN coordinates (gaze data is screen-
        // based). In fullscreen the offsets are ~0; if fullscreen was
        // denied, this still keeps the mapping correct.
        const off = screenToViewportOffsets();
        return {
            x: cx + off.x, y: cy + off.y, w: cw, h: ch,
            video_w: v.videoWidth, video_h: v.videoHeight,
        };
    }

    /** Tell the server a stimulus started (GazeFollower marks onset). */
    startRecording(stimulusName) {
        console.log('[Runner] ● Start recording:', stimulusName);
        socket.emit('start_recording', {
            stimulus_name: stimulusName,
            video_rect: this.getVideoContentRect(),
        });
    }

    /**
     * Stop recording and wait for the server to acknowledge.
     * @param {string} stimulusName
     * @returns {Promise<void>}
     */
    stopRecording(stimulusName) {
        return new Promise((resolve) => {
            console.log('[Runner] ○ Stopping recording:', stimulusName);

            // Listen for server ack (guard against double-resolution)
            let settled = false;
            const onAck = (data) => {
                if (settled) return;
                settled = true;
                console.log('[Runner] ✓ Server acknowledged recording stop:', data);
                socket.off('recording_stopped', onAck);
                resolve();
            };
            socket.on('recording_stopped', onAck);

            // Tell the server to stop recording
            socket.emit('stop_recording', { stimulus_name: stimulusName });

            // Safety timeout — don't hang forever if server doesn't respond
            setTimeout(() => {
                if (settled) return;
                settled = true;
                socket.off('recording_stopped', onAck);
                console.warn('[Runner] Ack timeout — continuing anyway.');
                resolve();
            }, 5000);
        });
    }

    /**
     * Experiment complete — run the MANDATORY post-validation (drift
     * check), then finalize the session recording server-side and
     * redirect.
     */
    complete() {
        console.log('[Runner] ✓ All stimuli complete.');

        // Hide the video UI so the validation overlay is unobstructed
        if (this.videoPlayer) this.videoPlayer.hidden = true;
        if (this.progressIndicator) this.progressIndicator.hidden = true;
        if (this.interStimulus) this.interStimulus.hidden = true;

        const finalize = () => this.finalizeSession();

        if (window.__validation) {
            // Post-validation: 3 targets — quantifies calibration DRIFT
            // over the session (compare with the pre-validation error).
            // Measure the rate again AFTER the videos. Comparing this
            // with the pre-video reading is the cheapest way to see
            // whether a session degrades over its own duration — the
            // question a single reading cannot answer.
            socket.emit('run_rate_gate', { stage: 'post-video' });
            window.__validation.run('post', finalize);
        } else {
            console.warn('[Runner] No validation overlay on this page — '
                + 'skipping post-validation.');
            finalize();
        }
    }

    /** Ask the server to save & segment the recording, then redirect. */
    finalizeSession() {
        console.log('[Runner] Finalizing session…');
        // The server also finalizes on disconnect, so data cannot be
        // lost even if this ack never arrives.
        let redirected = false;
        const go = () => {
            if (redirected) return;
            redirected = true;
            window.location.href = '/complete';
        };
        socket.on('experiment_saved', (data) => {
            console.log('[Runner] ✓ Session saved:', data);
            go();
        });
        socket.emit('experiment_done', {});
        setTimeout(go, 20000);
    }

    /**
     * Promise-based sleep helper.
     * @param {number} ms
     * @returns {Promise<void>}
     */
    sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }
}


// ============================================================
// PAGE INITIALISATION
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    const page = document.body.dataset.page;
    console.log('[Init] Page:', page);

    // ── Calibration page ──
    if (page === 'calibration') {
        const nativeCal = new NativeCalibration();
        window.__nativeCal = nativeCal;
        window.__validation = new ValidationTest();
        nativeCal.start();
    }

    // ── Stimuli page (standalone fallback flow) ──
    if (page === 'stimuli') {
        if (!window.stimuliList || window.stimuliList.length === 0) {
            console.error('[Init] No stimuli provided via window.stimuliList!');
            return;
        }
        const runner = new ExperimentRunner(window.stimuliList, {
            testMode: !!window.testMode,
        });
        runner.start();
    }
});
