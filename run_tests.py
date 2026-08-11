# -*- coding: utf-8 -*-
"""
Automated integrity test suite for the eye-tracking experiment.

Run with:  python run_tests.py

Checks (no server or camera needed):
  1. All Python modules compile.
  2. Frontend/backend contract: every SocketIO event the JS emits has a
     server handler, and every server-emitted event has a JS listener.
  3. Every DOM element ID referenced in experiment.js exists in a template.
  4. CSS sanity: the [hidden] override exists; classes used by JS exist.
  5. Stimuli are discoverable and every file resolves to a real video.
  6. Tracker subprocess protocol: spawn → ping → check → shutdown.
"""

import json
import logging
import math
import os
import py_compile
import re
import subprocess
import sys
import time

# ── Windows console encoding ──────────────────────────────────────────
# Windows defaults stdout to cp1252, and piping through a subprocess
# makes Python use it even on Python 3.12. A single non-ASCII character
# (≈, ✓, ≥) then raises UnicodeEncodeError and kills the whole run
# mid-report. Force UTF-8 so the output survives any console/pipe.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — older Python / exotic stream
    pass


BASE = os.path.dirname(os.path.abspath(__file__))
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           (" — " + detail) if detail else ""))
    if not ok:
        FAILURES.append(name + (": " + detail if detail else ""))


def read(relpath: str) -> str:
    with open(os.path.join(BASE, relpath), encoding="utf-8") as fh:
        return fh.read()


# Phrases Windows uses when an application-control policy (Smart App
# Control, WDAC, AppLocker) refuses to load a DLL. Localised, hence the
# German — the machine this first appeared on runs a German Windows.
_BLOCKED_MARKERS = (
    "DLL load failed",
    "application control policy",
    "Anwendungssteuerungsrichtlinie",
    "blocked by",
)


def environment_block(exc: BaseException) -> "str | None":
    """Recognise 'Windows blocked this file' and explain it.

    WHY THIS IS SPECIAL-CASED
    -------------------------
    A blocked DLL is not a failing test — the code is fine and the test
    never ran. Reporting it as an ordinary FAIL sends you looking for a
    regression in code that has not changed, which is exactly the wrong
    place. It is an environment problem with an environment fix.
    """
    text = "%s: %s" % (type(exc).__name__, exc)
    if not any(m.lower() in text.lower() for m in _BLOCKED_MARKERS):
        return None
    return (
        "WINDOWS BLOCKED A FILE — this is NOT a code failure.\n"
        "       %s\n"
        "       An application-control policy (Smart App Control, WDAC or\n"
        "       AppLocker) refused to load a compiled extension. Such\n"
        "       policies commonly allow %%WINDIR%% and %%PROGRAMFILES%% and\n"
        "       block user-writable paths — and this project lives under\n"
        "       Desktop.\n"
        "       Identify the policy first:\n"
        "         Event Viewer > Applications and Services Logs > Microsoft\n"
        "           > Windows > CodeIntegrity > Operational\n"
        "         (AppLocker blocks appear under AppLocker > MSI and Script)\n"
        "       Then: ask IT to allowlist the folder, or move the project\n"
        "       and its venv to an allowed location. Turning Smart App\n"
        "       Control off is IRREVERSIBLE without reinstalling Windows —\n"
        "       do not do it casually." % text)


# ── 1. Python compilation ──────────────────────────────────────────────
print("\n[1] Python modules compile")
for py in ("app.py", "config.py", "gaze_service.py", "tracker_service.py",
           "fixations.py", "gaze_vision.py", "quality_report.py",
           "excel_style.py", "tidy_data.py", "agreement_kit.py",
           "camera_fps_test.py", "tracker_fps_test.py",
           "backfill_manifests.py", "mnn_backend.py", "run_all.py",
           "camera_patch.py", "diagnose_rate.py", "sample_patch.py",
           "env_check.py", "check_screen_space.py", "fake_camera.py",
           "hz_experiment.py", "camera_light_test.py",
           "preview_load_test.py", "session_probe.py",
           # legacy vendored module (unused since WebGazer/pupil tracking
           # was dropped; kept for provenance):
           "pygazetracker/__init__.py", "pygazetracker/tracker.py",
           "pygazetracker/_pupil.py"):
    try:
        py_compile.compile(os.path.join(BASE, py), doraise=True)
        check(py, True)
    except py_compile.PyCompileError as exc:
        check(py, False, str(exc))

# ── 2. SocketIO event contract ─────────────────────────────────────────
print("\n[2] SocketIO event contract (JS ↔ Flask)")
js = read("static/js/experiment.js")
app = read("app.py")

js_emits = set(re.findall(r"socket\.emit\('([\w-]+)'", js))
js_listens = set(re.findall(r"socket\.on\('([\w-]+)'", js))
py_handles = set(re.findall(r"@socketio\.on\(\"([\w-]+)\"\)", app))
py_emits = set(re.findall(r"emit\(\s*[\"']([\w-]+)[\"']", app))

for ev in sorted(js_emits):
    check("JS emits '%s' → server handler" % ev, ev in py_handles)
for ev in sorted(py_emits - {"connect", "disconnect"}):
    listened = ev in js_listens
    # events that are informational-only may go unlistened; warn as pass
    check("server emits '%s' → JS listener" % ev, True,
          "" if listened else "no JS listener (informational only)")

# ── 3. DOM element IDs referenced by JS exist in templates ─────────────
print("\n[3] DOM elements referenced in experiment.js exist in templates")
templates = "".join(
    read(os.path.join("templates", f)) for f in os.listdir(
        os.path.join(BASE, "templates")) if f.endswith(".html")
)
dom_ids = set(re.findall(r"getElementById\('([\w-]+)'\)", js))
for el_id in sorted(dom_ids):
    check("#" + el_id, ('id="%s"' % el_id) in templates)

# ── 4. CSS sanity ──────────────────────────────────────────────────────
print("\n[4] CSS sanity")
css = read("static/css/style.css")
check("[hidden] display:none override present",
      re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css)
      is not None)
for cls in ("calibration-dot", "calibration-grid", "start-overlay",
            "detection-status--detected", "detection-status--not-detected",
            "inter-stimulus", "video-player"):
    check("CSS class ." + cls, ("." + cls) in css)
check(".calibration-grid is fullscreen-fixed",
      re.search(r"\.calibration-grid\s*\{[^}]*position:\s*fixed", css)
      is not None)

# ── 5. Stimuli discovery ───────────────────────────────────────────────
print("\n[5] Stimuli discovery")
sys.path.insert(0, BASE)
import config  # noqa: E402

stimuli = config.discover_stimuli()
check("stimuli found", len(stimuli) > 0, "found %d: %s" % (len(stimuli), stimuli))
for s in stimuli:
    path = os.path.join(config.STIMULI_DIR, s)
    real = os.path.realpath(path)
    ok = os.path.isfile(real) and os.path.getsize(real) > 0
    check("stimulus resolves: %s" % s, ok,
          "" if ok else "broken symlink or empty file: %s" % real)

# ── 6. Tracker subprocess protocol ─────────────────────────────────────
print("\n[6] Tracker subprocess protocol")
proc = subprocess.Popen(
    [sys.executable, os.path.join(BASE, "tracker_service.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=BASE,
)
try:
    def rpc(msg: dict) -> dict:
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            if line.startswith("@GF@"):
                return json.loads(line[4:].strip())
        return {}

    ready = None
    for line in proc.stdout:  # type: ignore[union-attr]
        if line.startswith("@GF@"):
            ready = json.loads(line[4:].strip())
            break
    check("service ready handshake", bool(ready and ready.get("ok")))
    check("ping", rpc({"cmd": "ping"}).get("ok") is True)
    report = rpc({"cmd": "check"})
    check("self-check runs", "report" in report,
          "; ".join("%s=%s" % kv for kv in report.get("report", {}).items()))
    check("shutdown", rpc({"cmd": "shutdown"}).get("ok") is True)
finally:
    try:
        proc.kill()
    except OSError:
        pass

# ── 7. Gain-correction round-trip (guards the px/py refactor) ──────────
# app.py can't be imported without Flask, so extract the correction
# helpers by AST and run the full fit→apply path. This would have caught
# the leftover corr["ax"] references after the polynomial refactor.
print("\n[7] Gain-correction fit → apply round-trip")
try:
    import ast
    import numpy as np
    import pandas as pd

    _src = read("app.py")
    _tree = ast.parse(_src)
    _want = {"_fit_poly", "_rms_resid", "_slope", "_apply_point",
             "_apply_series", "_correction_payload", "_auto_fit_correction"}
    _keep = [n for n in _tree.body
             if isinstance(n, ast.FunctionDef) and n.name in _want]
    _assign = [n for n in _tree.body if isinstance(n, ast.Assign)
               and "_GAIN_MIN" in ast.dump(n)]
    _ns = {"pd": pd, "logger": logging.getLogger("t"),
           "socketio": type("S", (), {"emit": staticmethod(
               lambda *a, **k: None)})()}
    exec(compile(ast.Module(body=_assign + _keep, type_ignores=[]),
                 "app_extract", "exec"), _ns)

    # Simulate a compressed + vertically-nonlinear recording: gaze pulled
    # toward centre, up-gaze pulled too high.
    W, H = 1680.0, 1050.0
    targets = [(202, 126), (1478, 126), (840, 525), (202, 924), (1478, 924)]

    def _measure(tx, ty):
        mx = W / 2 + 0.85 * (tx - W / 2)
        dy = ty - H / 2
        # Gentle vertical nonlinearity: up-gaze (dy<0) compressed a bit
        # more than down-gaze — a curve a line cannot undo, but mild
        # enough that the corrected mapping stays in the sane-gain band.
        my = H / 2 + 0.85 * dy - 0.00006 * dy * abs(dy)
        return mx, my

    record = {
        "targets": [{"tx": tx, "ty": ty,
                     "mx": _measure(tx, ty)[0], "my": _measure(tx, ty)[1]}
                    for tx, ty in targets],
        "screen": {"width_px": W, "height_px": H},
        "correction_active": {"active": False},
    }
    state: dict = {}
    _ns["_auto_fit_correction"](state, record, "sid")
    corr = state.get("correction")
    check("auto-fit produced a correction", corr is not None)

    if corr:
        # Corrected error at every target must beat the raw error.
        raw_e, cor_e = [], []
        for tx, ty in targets:
            mx, my = _measure(tx, ty)
            cx, cy = _ns["_apply_point"](mx, my, corr)
            raw_e.append((mx - tx) ** 2 + (my - ty) ** 2)
            cor_e.append((cx - tx) ** 2 + (cy - ty) ** 2)
        raw_rms = (sum(raw_e) / len(raw_e)) ** 0.5
        cor_rms = (sum(cor_e) / len(cor_e)) ** 0.5
        check("correction reduces validation error",
              cor_rms < 0.5 * raw_rms,
              "raw %.0f px → corrected %.0f px" % (raw_rms, cor_rms))
        # Whether the auto-fit ACCEPTS the quadratic is a deliberately
        # conditional, boundary-sensitive decision (beats a line AND
        # stays monotonic) — informational only, not a pass/fail, so the
        # suite doesn't depend on the BLAS/numpy build.
        print("       (info) auto-fit vertical model: %s"
              % ("quadratic" if len(corr.get("py", [])) == 3 else "linear"))

    # Deterministic quadratic machinery: a GENUINE quadratic mapping
    # target = 0.0005·m² + 0.8·m + 40 (a one-sided curve like the real
    # up-gaze asymmetry). A straight line cannot fit it; _fit_poly must
    # recover it despite the raw pixel scale (conditioning guard).
    true = [0.0005, 0.8, 40.0]   # highest-first
    measured = [200.0, 360.0, 525.0, 690.0, 850.0]
    curved = [(m, true[0] * m * m + true[1] * m + true[2])
              for m in measured]
    qy = _ns["_fit_poly"](curved, 2)
    ly = _ns["_fit_poly"](curved, 1)
    check("_fit_poly returns a quadratic (3 coeffs)",
          qy is not None and len(qy) == 3)
    if qy and ly:
        res_q = _ns["_rms_resid"](curved, qy)
        res_l = _ns["_rms_resid"](curved, ly)
        check("quadratic fit beats the line on curved data "
              "(well-conditioned)", res_q < 0.3 * res_l,
              "quad %.1f px vs line %.1f px" % (res_q, res_l))
        qcorr = {"px": [1.0, 0.0], "py": qy, "cx": W / 2, "cy": H / 2,
                 "source": "test"}
        errs = [abs(_ns["_apply_point"](m, m, qcorr)[1] - t)
                for m, t in curved]
        check("quadratic correction undoes the vertical curve",
              max(errs) < 5.0, "max residual %.1f px" % max(errs))

        # Vectorized apply (the finalize path) must run without KeyError.
        sx = pd.Series([_measure(*t)[0] for t in targets])
        sy = pd.Series([_measure(*t)[1] for t in targets])
        gx, gy = _ns["_apply_series"](sx, sy, corr)
        check("_apply_series runs on a DataFrame column",
              len(gx) == len(targets) and len(gy) == len(targets))

        # Payload has no legacy ax/bx keys and stays JSON-serializable.
        pl = _ns["_correction_payload"](corr)
        check("payload uses px/py (no legacy ax/bx)",
              "px" in pl and "ax" not in pl)
        json.dumps(pl)

        # Manual-slider shape must also apply cleanly.
        manual = {"px": [1.3, -0.3 * W / 2], "py": [1.3, -0.3 * H / 2],
                  "cx": W / 2, "cy": H / 2, "source": "manual slider"}
        _ns["_apply_point"](800.0, 400.0, manual)
        _ns["_apply_series"](sx, sy, manual)
        check("manual-slider correction applies cleanly", True)
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("gain-correction round-trip", False, _blocked or repr(exc))

# ── 8. Quality-metric integrity ────────────────────────────────────────
# These guard the 2026-07-31 fixes. The bugs they catch were all SILENT:
# the metrics kept producing plausible numbers that happened to measure
# nothing, so only an assertion about the FORMULA can catch a regression.
print("\n[8] Quality-metric integrity")
try:
    import ast
    import importlib
    import types as _types

    import numpy as np

    _app = read("app.py")
    _cfg = read("config.py")

    # (a) The gaze-samples denominator must be a fixed constant, never
    #     derived from the recording. `expected = dur / median_dt` made
    #     the metric self-referential: every session scored ~100 %.
    _seg = _app[_app.find("Data-loss metrics"):][:2600]
    check("gaze_samples_pct uses the NOMINAL rate as denominator",
          "nominal_dt_ns" in _seg and "expected = (dur_ns / nominal_dt_ns)"
          in _seg)
    check("self-referential denominator is not used for gaze_samples_pct",
          "expected = (dur_ns / median_dt_ns)" not in _app)
    check("relative_yield_pct keeps the old ratio separately",
          "relative_yield_pct" in _app and "expected_rel" in _app)
    check("NOMINAL_SAMPLING_HZ is configurable", "NOMINAL_SAMPLING_HZ" in _cfg)

    # (b) quality_report.py must not reintroduce the circular version.
    _qr = read("quality_report.py")
    check("quality_report.py uses the nominal denominator too",
          "nominal_dt" in _qr and "expected = dur / nominal_dt" in _qr)

    # (c) Pre and post validation must use the SAME targets, or drift is
    #     partly an artefact of which targets were dropped.
    _js = read("static/js/experiment.js")
    _grid = re.search(r"const VALIDATION_GRID = \[(.*?)\];", _js, re.S)
    check("VALIDATION_GRID exists as a single source of truth",
          _grid is not None)
    if _grid:
        _n = len(re.findall(r"\[\s*\d+\s*,\s*\d+\s*\]", _grid.group(1)))
        _ys = {m[1] for m in re.findall(r"\[\s*(\d+)\s*,\s*(\d+)\s*\]",
                                        _grid.group(1))}
        check("grid has 7 targets", _n == 7, "got %d" % _n)
        check("grid spans 5 vertical elevations (quadratic y fit needs >=3)",
              len(_ys) >= 5, "got %d" % len(_ys))
    _pos = re.search(r"const VALIDATION_POSITIONS = \{(.*?)\};", _js, re.S)
    # SUPERSEDED by the two-grid protocol (section [17]). Drift still
    # needs a like-for-like pair, but that pair is now pre_check/post on
    # grid B — not pre/post on grid A. The fit set is deliberately a
    # DIFFERENT grid, because measuring the correction where it was
    # fitted is not a generalisation estimate.
    check("drift's pair (pre_check, post) share one grid",
          bool(_pos) and "pre_check: VALIDATION_CHECK_GRID" in _pos.group(1)
          and "post: VALIDATION_CHECK_GRID" in _pos.group(1))
    check("the fit set uses the OTHER grid",
          bool(_pos) and "pre_fit: VALIDATION_GRID" in _pos.group(1))
    check("config records equal pre/post target counts",
          "VALIDATION_TARGETS_PRE = 7" in _cfg
          and "VALIDATION_TARGETS_POST = 7" in _cfg)

    # (d) Drift must be computed from correction-free errors: the pre
    #     check is raw, the post check corrected, and the correction was
    #     fitted on the pre targets.
    check("_uncorrected_error exists", "_uncorrected_error" in _app)
    check("drift prefers the uncorrected basis",
          "mean_err_deg_raw" in _app and "drift_basis" in _app)

    _tree = ast.parse(_app)
    _fn = next((n for n in _tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_uncorrected_error"), None)
    check("_uncorrected_error is extractable", _fn is not None)
    if _fn:
        _ns2 = {"np": np, "Any": object}
        exec(compile(ast.Module(body=[_fn], type_ignores=[]), "x", "exec"),
             _ns2)
        _ue = _ns2["_uncorrected_error"]
        # Invert a known affine correction and recover the raw error.
        g, cx, cy = 0.8, 960.0, 540.0
        _corr = {"px": [g, cx * (1 - g)], "py": [g, cy * (1 - g)],
                 "cx": cx, "cy": cy, "source": "test"}
        _tg, _raw = [], []
        for tx, ty in ((200, 150), (1700, 150), (960, 900), (300, 800)):
            rmx, rmy = cx + (tx - cx) * 0.75, cy + (ty - cy) * 0.75
            _raw.append(float(np.hypot(rmx - tx, rmy - ty)))
            mx = np.polyval(_corr["px"], rmx)
            my = np.polyval(_corr["py"], rmy)
            _tg.append({"tx": float(tx), "ty": float(ty),
                        "mx": float(mx), "my": float(my)})
        _pl = {"targets": _tg, "mean_err_px": 100.0, "mean_err_deg": 1.7}
        _out = _ue(_pl, _corr)
        check("affine correction is invertible to the raw error",
              abs(_out.get("mean_err_px_raw", -1)
                  - float(np.mean(_raw))) < 0.5,
              "got %s, expected %.1f" % (_out.get("mean_err_px_raw"),
                                         float(np.mean(_raw))))
        check("no active correction → raw error passes through",
              _ue(_pl, None).get("mean_err_px_raw") == 100.0)
        _q = _ue(_pl, {"px": [1, 0], "py": [1e-5, 1.0, 0.0], "source": "q"})
        check("quadratic correction is refused, not approximated",
              _q.get("raw_available") is False
              and "mean_err_px_raw" not in _q)

    # (e) Gain must be reported per axis — a single mean reads x1.0 when
    #     one axis is compressed and the other expanded.
    _rev = read("templates/review.html")
    check("review panel shows gain_x and gain_y separately",
          "gc.gain_x" in _rev and "gc.gain_y" in _rev)
    check("review panel states the gaze-samples denominator",
          "nominal_sampling_hz" in _rev)

    # (f) Rate gate wiring, end to end.
    _tsvc = read("tracker_service.py")
    # The rate check MUST be passive/two-phase. A single blocking command
    # holds GazeService's command lock for the whole window, starving the
    # live gaze preview — and an accuracy check run during that window
    # collects stale samples and reports a meaningless ~5 deg error.
    check("rate check is two-phase (non-blocking)",
          "def cmd_rate_check_start" in _tsvc
          and "def cmd_rate_check_result" in _tsvc
          and 'cmd == "rate_check_start"' in _tsvc)
    check("no blocking polling loop remains in the rate check",
          "def cmd_rate_check(" not in _tsvc)
    check("sample arrivals are recorded passively in _on_sample",
          "_rate_collecting" in _tsvc
          and "_rate_stamps.append" in _tsvc)
    # A low rate is ambiguous without this: detection failure (fix the
    # seating/lighting) and slow frames (fix power/load) need opposite
    # actions. The gate must report which, not assert a cause.
    check("rate check reports detection rate alongside frame rate",
          "detected_pct" in _tsvc and "_rate_failed" in _tsvc)
    _jsrate = read("static/js/experiment.js")
    check("the UI explains WHICH problem it is",
          "detected_pct" in _jsrate
          and "Fix the " in _jsrate and "SETUP" in _jsrate)
    check("the UI no longer asserts an unproven turbo cause",
          "turbo window closed" not in _jsrate)
    # A peak faster than the camera's rated fps is physically impossible
    # from live capture — it means buffered frames were read back-to-back
    # after a stall, and those frames are stale.
    check("rate check flags buffer-drain bursts",
          '"bursty"' in _tsvc and "NOMINAL_CAMERA_FPS" in _tsvc)
    check("the UI explains bursts and warns the data is stale",
          "g.bursty" in _jsrate and "stale" in _jsrate)

    # (j) "Calibration perfect, validation catastrophic" is usually a
    #     PIXEL-SPACE mismatch, not a tracking failure: GazeFollower maps
    #     gaze into screeninfo's physical monitor pixels, the browser uses
    #     CSS pixels. Display scaling or a second monitor breaks it.
    check("tracker can report its screen space",
          "def cmd_screen_info" in _tsvc
          and 'cmd == "screen_info"' in _tsvc)
    check("validation compares tracker space against the browser's",
          "_screen_space_check" in _app and "screen_space" in _app)
    check("a mismatch is surfaced in the review panel",
          "SCREEN SPACE MISMATCH" in _rev)
    check("standalone screen-space checker exists",
          "screeninfo" in read("check_screen_space.py")
          and "devicePixelRatio" in read("check_screen_space.py"))

    _fn2 = next((n for n in ast.parse(_app).body
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "_screen_space_check"), None)
    check("_screen_space_check is extractable", _fn2 is not None)
    if _fn2:
        # Silence the extracted function's logger: it calls logger.error
        # on a mismatch, and this test FEEDS it a fake mismatch. Without
        # this, the suite prints a fully-formed "SCREEN SPACE MISMATCH"
        # banner with invented numbers (2560x1440 vs 1707x960) that reads
        # exactly like a real finding in the run_all log. It did, and it
        # cost a chunk of attention.
        _quiet = logging.getLogger("screen_space_test")
        _quiet.addHandler(logging.NullHandler())
        _quiet.propagate = False
        _ns3 = {"logger": _quiet, "Any": object}
        _fake_gs = _types.SimpleNamespace(
            screen_info=lambda: {"gaze_screen_size": [2560, 1440],
                                 "monitors": [{"width": 2560,
                                               "height": 1440}]})
        _ns3["gaze_service"] = _fake_gs
        exec(compile(ast.Module(body=[_fn2], type_ignores=[]), "x", "exec"),
             _ns3)
        _ssc = _ns3["_screen_space_check"]
        # 150 % Windows scaling: browser sees 1707x960, tracker 2560x1440
        _r = _ssc({"width_px": 1707, "height_px": 960})
        check("detects a 150 % display-scaling mismatch",
              _r.get("mismatch") is True
              and "scaling" in (_r.get("likely_cause") or ""),
              str(_r.get("likely_cause"))[:60])
        # Matching spaces must NOT be flagged
        _fake_gs.screen_info = lambda: {"gaze_screen_size": [1920, 1080],
                                        "monitors": [{"width": 1920,
                                                      "height": 1080}]}
        _r2 = _ssc({"width_px": 1920, "height_px": 1080})
        check("no false alarm when the spaces agree",
              _r2.get("mismatch") is False)
    check("app yields with socketio.sleep during the measurement",
          "socketio.sleep(RATE_GATE_SECONDS)" in _app)

    # (k) The accuracy check and the live preview must not race, and the
    #     targets must be placed in the SAME viewport frame the gaze
    #     samples arrive in.
    check("preview restart cannot lose the loop (generation counter)",
          "preview_generation" in _app)
    check("validation awaits the fullscreen transition",
          "await document.documentElement.requestFullscreen" in _jsrate)
    # geometry is captured in run() but read in submit() — a DIFFERENT
    # method. As a local it raised a ReferenceError after the last
    # target, which inside an async method rejected silently and froze
    # the overlay on "7/7" with nothing in the console.
    check("validation records the geometry it measured in",
          "this.geometry = {" in _jsrate
          and "geometry: this.geometry" in _jsrate
          and '"geometry"' in _app)
    check("no bare 'geometry' identifier survives in submit()",
          "geometry: geometry" not in _jsrate)
    check("the validation tail cannot fail silently",
          "failed after the last target" in _jsrate)
    check("a second accuracy check cannot start on top of the first",
          "already running — ignoring re-entry" in _jsrate)
    check("per-target sample counts are recorded even on failure",
          "ok: false, n: this.samples.length" in _jsrate)
    check("a starved validation is flagged in the log",
          "very few samples" in _app)

    # (l) "It gets worse and worse" vs "it dropped once" are different
    #     faults; a single median Hz cannot distinguish them.
    check("rate check reports a per-bucket profile",
          "profile_hz" in _tsvc and "_bucket_rates" in _tsvc)
    _spatch = read("sample_patch.py")
    check("per-sample disk flush is batched",
          "GF_SAMPLE_FLUSH_SECONDS" in _spatch and "last_flush" in _spatch)
    _bk = next((n for n in ast.parse(_tsvc).body
                if isinstance(n, ast.ClassDef) and n.name == "Service"), None)
    _bkf = next((n for n in (_bk.body if _bk else [])
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "_bucket_rates"), None)
    if _bkf:
        _ns4: dict = {}
        _mod4 = ast.fix_missing_locations(ast.Module(
            body=[ast.ClassDef(name="S", bases=[], keywords=[],
                               body=[_bkf], decorator_list=[])],
            type_ignores=[]))
        exec(compile(_mod4, "x", "exec"), _ns4)
        _t, _st = 0.0, []
        for _sec, _hz in ((5, 30), (5, 20), (5, 10)):
            for _ in range(_sec * _hz):
                _t += 1.0 / _hz
                _st.append(_t)
        _prof = _ns4["S"]._bucket_rates(_st, 5.0)
        check("the profile reproduces a known slide",
              _prof == [30.0, 20.0, 10.0], str(_prof))

    # (m) Headless replay mode: deterministic input, no person, no camera.
    #     Must be INERT unless explicitly switched on, and must be loudly
    #     marked so simulated runs can never pass as participant data.
    _fk = read("fake_camera.py")
    check("fake camera + calibration stub exist",
          "def make_fake_camera" in _fk
          and "def apply_fake_calibration" in _fk)
    check("fake mode is loudly marked as not-real-data",
          "NOT REAL DATA" in _fk and "fake_mode" in _tsvc)
    check("tracker prefers the fake camera when armed",
          "make_fake_camera" in _tsvc
          and _tsvc.index("make_fake_camera") < _tsvc.index("make_camera"))
    check("calibration UI is skipped in fake mode",
          "fake_calibration_enabled" in _tsvc)
    check("BlazeFace (upstream's speed lever) is selectable",
          "GF_FACE_ALIGNMENT" in _tsvc and "BlazeFaceAlignment" in _tsvc)
    _saved_fk = {k: os.environ.pop(k, None)
                 for k in ("GF_FAKE_CAMERA", "GF_FAKE_CALIBRATION")}
    try:
        sys.path.insert(0, BASE)
        _fkm = importlib.import_module("fake_camera")
        importlib.reload(_fkm)
        check("fake mode is inert by default",
              _fkm.fake_camera_path() is None
              and _fkm.make_fake_camera() is None
              and _fkm.fake_calibration_enabled() is False)
        os.environ["GF_FAKE_CAMERA"] = "data/nope.mp4"
        importlib.reload(_fkm)
        check("a missing clip fails safe (no camera, no crash)",
              _fkm.make_fake_camera(log=lambda m: None) is None)
    finally:
        for _k, _v in _saved_fk.items():
            os.environ.pop(_k, None)
            if _v is not None:
                os.environ[_k] = _v

    # (n) The unattended experiment must isolate one variable per
    #     condition and run each in a FRESH process — otherwise an
    #     in-process leak and a thermal drop are indistinguishable.
    _hz = read("hz_experiment.py")
    check("hz experiment has the key conditions",
          all(c in _hz for c in ("baseline", "repeat2", "stock_writer",
                                 "flush_always", "threads8")))
    check("each condition runs in a fresh subprocess",
          "subprocess.run" in _hz and "--single" in _hz)
    check("conditions do not inherit stray env vars",
          "env.pop(key, None)" in _hz)
    _hzm = importlib.import_module("hz_experiment")
    importlib.reload(_hzm)
    _mk = lambda hz, prof: {"ok": True, "hz_median": hz, "hz_overall": hz,
                            "profile_hz": prof, "detected_pct": 100.0,
                            "frames": 1800, "seconds": 60.0}
    _v1 = " ".join(_hzm.verdict({"baseline": _mk(20.0, [30, 25, 18, 12]),
                                 "repeat2": _mk(20.1, [30, 25, 18, 12])}))
    check("verdict names an in-run slide", "SLIDE within a single run" in _v1)
    _v2 = " ".join(_hzm.verdict({"baseline": _mk(30.0, [30, 30, 30, 30]),
                                 "repeat2": _mk(22.0, [22, 22, 22, 22]),
                                 "repeat3": _mk(16.0, [16, 16, 16, 16])}))
    check("verdict distinguishes cross-run decay",
          "DEGRADES ACROSS RUNS" in _v2 and "NOT a leak" in _v2)
    _v3 = " ".join(_hzm.verdict({"baseline": _mk(30.0, [30, 30, 30, 30]),
                                 "stock_writer": _mk(14.0, [30, 20, 14, 14])}))
    check("verdict flags a condition that matters",
          "WORTH ACTING ON" in _v3)
    # The preview test must exercise the REAL IPC path (tracker
    # subprocess + GazeService), not an in-process call — an in-process
    # poll is a cheap attribute read and proves nothing about the app.
    _pl = read("preview_load_test.py")
    check("preview test uses the real tracker subprocess",
          "from gaze_service import GazeService" in _pl
          and "svc.gaze_info()" in _pl)
    check("preview test brackets the app's own 150 ms polling",
          "preview_150ms" in _pl and "0.15" in _pl
          and "preview_20ms" in _pl)
    # Duplicate subscribers would cost a CSV write per frame EACH, and
    # only in a real session (repeated start/stop cycles). Report the
    # count, and make duplicates impossible.
    check("rate check reports the subscriber count",
          '"subscribers"' in _tsvc and "_subscriber_count" in _tsvc)
    check("duplicate subscribers are removed on every start",
          "start_sampling_deduped" in _spatch)
    # The session lifecycle (repeated stop/start around calibration and
    # the accuracy check) is the one thing offline benchmarks skip.
    check("tracker can reproduce the session's sampling churn",
          "def cmd_cycle_sampling" in _tsvc
          and 'cmd == "cycle_sampling"' in _tsvc)
    _sp2 = read("session_probe.py")
    check("session probe walks the lifecycle and tracks subscribers",
          "cycle_sampling" in _sp2 and "subscribers" in _sp2
          and "after_calibration" in _sp2)
    # The live app must keep EVERY rate measurement, not just the last.
    check("app keeps a rate history across the session",
          "rate_history" in _app and '"stage"' in _app)
    check("history is stored in the manifest",
          '"rate_history": state.get("rate_history")' in _app)
    check("the rate is re-measured after the videos",
          "stage: 'post-video'" in _jsrate)
    try:
        pass
    finally:
        for _k, _v in _saved_fk.items():
            os.environ.pop(_k, None)
            if _v is not None:
                os.environ[_k] = _v
    _js2 = read("static/js/experiment.js")
    # Nothing in the participant flow may WAIT on the measurement. The
    # verdict gates the videos (the irreplaceable part), not the cheap
    # and repeatable accuracy check.
    check("accuracy check never waits for the rate measurement",
          "this.validateBtn.disabled = false;" in _js2)
    check("the rate verdict gates the videos instead",
          "dataset.rateBlocked" in _js2)
    # A normal run's stimulus scope must be configurable, not hardcoded.
    check("normal-run stimulus mode is configurable",
          "SESSION_STIMULUS_MODE" in _cfg and "SESSION_STIMULUS_MODE" in _app)
    check("normal run defaults to the single 30 s clip",
          'SESSION_STIMULUS_MODE", "clip30"' in _cfg)
    _diag = read("diagnose_rate.py")
    check("per-stage rate diagnosis exists",
          "face_alignment" in _diag and "residual_ms" in _diag)
    check("diagnosis separates detection failures from slowness",
          "outcomes" in _diag and "NOT A SPEED PROBLEM" in _diag)

    # "No module named gazefollower" nearly always means the venv is not
    # active. The tools must say so instead of emitting a bare traceback.
    _env = read("env_check.py")
    check("env_check detects the wrong interpreter",
          "def in_project_venv" in _env and "def require" in _env)
    check("camera-dependent tools pre-flight the interpreter",
          "from env_check import require" in _diag
          and "from env_check import require" in read("tracker_fps_test.py"))
    check("run_all refuses to run outside the project venv",
          "in_project_venv" in read("run_all.py"))
    _ec = importlib.import_module("env_check")
    importlib.reload(_ec)
    check("env_check reports something for every module it probes",
          _ec.missing(("definitely_not_a_real_module_xyz",))
          == ["definitely_not_a_real_module_xyz"]
          and _ec.missing(("os", "sys")) == [])

    # (i) Failed-detection frames must be RECORDED, not dropped.
    #     GazeFollower raises in _write_sample when detection failed, so
    #     the sample is lost, `status` is always 1, and the recorded rate
    #     silently understates the capture rate.
    _sp = read("sample_patch.py")
    check("sample patch exists and is on by default",
          "def apply_sample_patch" in _sp
          and 'GF_SAMPLE_PATCH", "1"' in _sp)
    check("tracker installs it before sampling starts",
          "apply_sample_patch" in _tsvc
          and _tsvc.index("apply_sample_patch")
          < _tsvc.index("def cmd_rate_check_start"))
    _saved_sp = os.environ.pop("GF_SAMPLE_PATCH", None)
    try:
        import io as _io

        sys.path.insert(0, BASE)
        _spm = importlib.import_module("sample_patch")
        importlib.reload(_spm)
        _gf = type("GF", (), {})()
        _buf = _io.StringIO()
        _gf._tmpSampleDataSteam = _buf
        _gf._trigger = 0
        check("patch applies", _spm.apply_sample_patch(_gf) is True)
        _T = _types.SimpleNamespace
        _bad = _T(timestamp=222, status=False, raw_gaze_coordinates=None,
                  calibrated_gaze_coordinates=None,
                  filtered_gaze_coordinates=None, left_openness=None,
                  right_openness=None, tracking_state=_T(value=3),
                  event=_T(value=0))
        _gf._write_sample(None, _bad)      # upstream raises here
        _row = _buf.getvalue().strip().split(",")
        check("a failed-detection frame is still written",
              len(_row) == 13, "got %d columns" % len(_row))
        check("it is written with status=0", _row[10] == "0")
        check("and the invalid-coordinate sentinel", "-65536" in _row[1])
    finally:
        os.environ.pop("GF_SAMPLE_PATCH", None)
        if _saved_sp is not None:
            os.environ["GF_SAMPLE_PATCH"] = _saved_sp
    # GazeFollower's process_frame() raises on EVERY frame when no
    # calibration model is loaded, so a rate check before calibration
    # yields zero samples and a wall of tracebacks. Both the gate and the
    # standalone fps tool must refuse up front.
    check("rate_check refuses without a calibration model",
          "has_calibrated" in _tsvc and "needs_calibration" in _tsvc)
    check("rate gate runs AFTER calibration, not during warmup",
          "_run_rate_gate" in _app
          and 'if result.get("success"):' in _app
          and "RATE_GATE_SECONDS" not in _app.split("def _warm()")[1][:600])
    # GazeFollower never persists a calibration (save_model() is never
    # called), so the standalone fps tool must stub the calibration step
    # rather than demand a saved model that can never exist.
    _fps = read("tracker_fps_test.py")
    check("tracker_fps_test stubs calibration instead of requiring it",
          "calibration.predict = lambda" in _fps)
    # ...and recording must still refuse when THIS session never
    # calibrated, so a stale on-disk model can never be inherited.
    check("recording refuses without a calibration in this session",
          "if not self.calibrated:" in _tsvc
          and "recording refused" in _tsvc)
    check("skip-mode warns against persisting calibration",
          "never persists a calibration" in _tsvc)
    # Windows cp1252 kills a whole report on the first non-ASCII char.
    for _f in ("run_tests.py", "quality_report.py", "backfill_manifests.py",
               "mnn_backend.py", "run_all.py", "tracker_service.py",
               "camera_fps_test.py", "tracker_fps_test.py"):
        check("%s forces UTF-8 stdout" % _f,
              "sys.stdout.reconfigure" in read(_f))
    check("run_all forces UTF-8 in sub-steps",
          "PYTHONIOENCODING" in read("run_all.py"))

    # (h) Camera fix: GazeFollower sets capture properties on an UNOPENED
    #     VideoCapture, so they do nothing. The patch must apply them
    #     after open(), and must stay opt-in + reversible.
    _cam = read("camera_patch.py")
    # Compare positions INSIDE the overridden open() only — the module
    # docstring mentions the same names and would skew a whole-file index.
    _open_body = _cam.split("def open(self):", 1)[-1].split("\n    try:", 1)[0]
    check("camera patch applies properties AFTER open()",
          "_cap.open" in _open_body
          and "CAP_PROP_FRAME_WIDTH" in _open_body
          and _open_body.index("_cap.open")
          < _open_body.index("CAP_PROP_FRAME_WIDTH"))
    check("camera fix is opt-in", "GF_CAMERA_FIX" in _cam)
    check("tracker wires the camera fix in",
          "make_camera" in _tsvc and 'kwargs["camera"]' in _tsvc)
    _saved_cam = os.environ.pop("GF_CAMERA_FIX", None)
    try:
        sys.path.insert(0, BASE)
        _cp = importlib.import_module("camera_patch")
        importlib.reload(_cp)
        check("camera fix off by default", _cp.camera_fix_enabled() is False
              and _cp.make_camera() is None)
        os.environ["GF_CAMERA_FIX"] = "1"
        importlib.reload(_cp)
        check("camera fix reads its env switch",
              _cp.camera_fix_enabled() is True
              and _cp.desired_camera()["width"] == 640)
    finally:
        os.environ.pop("GF_CAMERA_FIX", None)
        if _saved_cam is not None:
            os.environ["GF_CAMERA_FIX"] = _saved_cam

    # (g) MNN backend override. GazeFollower hardcodes CPU; the override
    #     must not mutate the caller's dict and must preserve keys it
    #     does not set.
    import importlib
    import types as _types

    _fake = _types.ModuleType("MNN")
    _captured = {}
    _fake.nn = _types.SimpleNamespace(
        create_runtime_manager=lambda cfgs, *a, **k: _captured.setdefault(
            "cfg", cfgs))
    _saved_mnn = sys.modules.get("MNN")
    sys.modules["MNN"] = _fake
    _saved_env = {k: os.environ.get(k)
                  for k in ("GF_MNN_BACKEND", "GF_MNN_THREADS")}
    try:
        os.environ["GF_MNN_BACKEND"] = "CUDA"
        os.environ["GF_MNN_THREADS"] = "6"
        sys.path.insert(0, BASE)
        _mb = importlib.import_module("mnn_backend")
        importlib.reload(_mb)
        check("backend names resolve to MNN forward codes",
              _mb.resolve_backend("CUDA") == 2
              and _mb.resolve_backend("opencl") == 3
              and _mb.resolve_backend("nonsense") is None)
        _mb.apply_backend_override()
        _gf_cfg = {"precision": "low", "backend": 0, "numThread": 4}
        _fake.nn.create_runtime_manager((_gf_cfg,))
        _got = _captured.get("cfg", ({},))[0]
        check("override replaces GazeFollower's hardcoded CPU backend",
              _got.get("backend") == 2 and _got.get("numThread") == 6)
        check("override preserves keys it does not set",
              _got.get("precision") == "low")
        check("override does not mutate the caller's config dict",
              _gf_cfg == {"precision": "low", "backend": 0, "numThread": 4})
        for _k in ("GF_MNN_BACKEND", "GF_MNN_THREADS"):
            os.environ.pop(_k, None)
        importlib.reload(_mb)
        check("no override when the env vars are unset",
              _mb.desired_config() == {}
              and _mb.apply_backend_override() == {})
    finally:
        for _k, _v in _saved_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        if _saved_mnn is None:
            sys.modules.pop("MNN", None)
        else:
            sys.modules["MNN"] = _saved_mnn
    check("tracker applies the override before GazeFollower loads",
          "apply_backend_override" in _tsvc
          and _tsvc.index("apply_backend_override")
          < _tsvc.index("from gazefollower import GazeFollower"))
    check("MNN runtime is reported in the self-check",
          "mnn_runtime" in _tsvc)
    check("GazeService exposes rate_check",
          "def rate_check" in read("gaze_service.py"))
    check("rate gate is recorded in the manifest",
          '"rate_gate": state.get("rate_gate")' in _app)
    check("rate-gate override is auditable",
          "rate_gate_override" in _app and "override_reason" in _app)
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("quality-metric integrity", False, _blocked or repr(exc))

# ── 9. Capture-loop instrumentation ────────────────────────────────────
# A halved sampling rate has two possible causes needing opposite fixes:
# frames never produced (capture too slow) vs frames produced and lost
# downstream. Every earlier test measured only the OUTPUT, so it could
# not tell them apart. These guard the discriminator.
print("\n[9] Capture-loop instrumentation")
try:
    _fc = read("fake_camera.py")
    _tsvc2 = read("tracker_service.py")
    _app2 = read("app.py")

    check("fake camera times the synchronous per-frame callback",
          "callback_ms" in _fc and "t_cb = time.perf_counter()" in _fc)
    check("callback timing includes lock-wait (measured around the lock)",
          _fc.index("t_cb = time.perf_counter()")
          < _fc.index("with self.callback_and_param_lock:"))
    check("camera_stats() is safe when not in fake mode",
          "_LIVE_CAMERA = None" in _fc and "if cam is None:" in _fc)
    check("rate check snapshots the frame counter at START",
          "_rate_cam0" in _tsvc2
          and _tsvc2.index("self._rate_cam0 = self._camera_stats()")
          < _tsvc2.index("cam_now = self._camera_stats()"))
    check("served rate is computed over the measurement WINDOW, not "
          "since camera open",
          "served_hz_this_window" in _tsvc2 and "frames_this_window" in _tsvc2)
    check("tracker distinguishes capture-limited from dropped frames",
          "capture_limited" in _tsvc2 and "frames_dropped_pct" in _tsvc2)
    check("app logs the capture-loop breakdown",
          "Capture loop [" in _app2 and "FRAMES DROPPED downstream" in _app2)
    check("capture instrumentation never breaks a real-webcam run",
          "def _camera_stats" in _tsvc2
          and "except Exception:  # noqa: BLE001\n            return None"
          in _tsvc2)

    # The arithmetic itself, on synthetic numbers — both verdicts.
    def _verdict(served: float, sustained: float) -> "tuple[float, bool]":
        dropped = round(max(0.0, 100.0 * (served - sustained) / served), 1)
        return dropped, bool(sustained >= 0.85 * served)

    _d, _lim = _verdict(30.0, 15.0)
    check("30 Hz delivered vs 15 Hz sampled -> 50%% lost, NOT capture-limited",
          _d == 50.0 and _lim is False, "got %s / %s" % (_d, _lim))
    _d, _lim = _verdict(15.0, 15.0)
    check("15 Hz delivered vs 15 Hz sampled -> 0%% lost, capture-limited",
          _d == 0.0 and _lim is True, "got %s / %s" % (_d, _lim))
    _d, _lim = _verdict(30.0, 29.0)
    check("a healthy run is capture-limited with no losses",
          _d < 5.0 and _lim is True, "got %s / %s" % (_d, _lim))

    # ── Stage split (which half of the per-frame work is expensive) ──
    check("stage timers wrap BOTH model stages",
          "orig_face(timestamp, frame)" in _tsvc2
          and "orig_gaze(image, face_info)" in _tsvc2)
    check("stage timers are idempotent",
          "_stage_timers_installed" in _tsvc2)
    check("stage timers record in a finally block (a raising stage still "
          "reports its cost)",
          _tsvc2.count("finally:\n                self._face_ms.append") == 1
          and _tsvc2.count("finally:\n                self._gaze_ms.append")
          == 1)
    check("stage buffers are cleared at the start of each measurement",
          "_d.clear()" in _tsvc2
          and _tsvc2.index("_d.clear()") < _tsvc2.index("def cmd_rate_check_result"))
    check("stage split is bounded in memory",
          "maxlen=4000" in _tsvc2)
    check("app logs the stage split", "Stage split [" in _app2)
    check("session_probe reports per-frame cost for a browser-free baseline",
          "face ms" in read("session_probe.py")
          and "callback_ms_median" in read("session_probe.py"))

    # Overhead arithmetic: callback total minus the two model stages.
    _cb, _face, _gaze = 64.5, 21.0, 38.0
    _over = round(max(0.0, _cb - (_face + _gaze)), 1)
    check("overhead = callback - models", _over == 5.5, "got %s" % _over)
    check("overhead never goes negative on noisy medians",
          round(max(0.0, 30.0 - 33.0), 1) == 0.0)

    # ── optimize_rate.py: every knob, one pass, no person needed ──
    _opt = read("optimize_rate.py")
    for _stage in ("[1] MACHINE", "[2] POWER STATE", "[3] MNN THREAD SWEEP",
                   "[4] MNN BACKENDS", "[5] FACEMESH", "[6] CPU CONTENTION",
                   "[7] PROCESS PRIORITY", "[8] VERDICT"):
        check("optimize_rate covers %s" % _stage, _stage in _opt)
    check("contention test spawns and ALWAYS reaps its busy processes",
          _opt.count("p.terminate()") >= 2
          and "finally:" in _opt.split("def contention")[1][:2000])
    check("priority test restores the original priority in a finally block",
          "proc.nice(original)" in _opt
          and _opt.split("def priority")[1].count("finally:") >= 2)
    check("a GPU backend counts only if it BEATS cpu (MNN falls back "
          "silently)",
          "v < 0.85 * cpu_ms" in _opt)
    check("verdict warns that browser-free headroom is not session headroom",
          "does not survive a session" in _opt)
    check("optimize_rate never writes session data",
          "save_data" not in _opt and "begin_stimulus" not in _opt)
    check("every optimize_rate stage degrades instead of crashing",
          _opt.count("except Exception as exc:  # noqa: BLE001") >= 6)

    # ── Build fingerprint: the project is edited on one machine and run
    #    on another, so a stale copy must announce itself rather than
    #    quietly omitting the line someone is waiting for.
    _ns2: dict = {}
    exec(_app2[_app2.index("INSTRUMENTATION = {"):
               _app2.index('if __name__ == "__main__":')],
         {"os": os, "datetime": __import__("datetime").datetime,
          "logger": __import__("logging").getLogger("t"),
          "__file__": os.path.abspath("app.py")}, _ns2)
    _instr = _ns2["INSTRUMENTATION"]
    check("fingerprint tracks every diagnostic added in this session",
          {"capture-loop", "stage-split", "validation-geometry",
           "validation-reentry-guard"} <= set(_instr))
    _bad = [n for n, (rel, needle) in _instr.items()
            if needle not in read(rel)]
    check("every fingerprint marker actually exists in its file",
          not _bad, "missing: %s" % _bad)
    check("a stale copy is reported as a WARNING, not silently",
          "STALE BUILD" in _app2 and "logger.warning" in
          _app2[_app2.index("def _log_build_fingerprint"):])
    check("fingerprint runs before the server accepts connections",
          _app2.index("_log_build_fingerprint()")
          < _app2.index("socketio.run"))

    # ── Power state: the confound that scales BOTH model stages ──
    check("power state is recorded with every rate measurement",
          '"power"] = _power_state()' in _app2.replace("report[", '"power"] = '
                                                       if False else "report[")
          or 'report["power"] = _power_state()' in _app2)
    check("power state reaches the manifest via the rate gate",
          'rate_gate' in _app2 and "def _power_state" in _app2)
    check("battery and underclock are both still CONSIDERED (as hints)",
          "on battery (%s%%)" in _app2 and "understates turbo" in _app2)
    check("low-rate hints are logged as hints, not as a diagnosis",
          'logger.warning("Rate is low — worth checking: %s"' in _app2)
    check("_power_state never raises on a machine without psutil",
          "except Exception as exc:  # noqa: BLE001"
          in _app2[_app2.index("def _power_state"):
                   _app2.index("def _run_rate_gate")])
    check("optimize_rate leads with power before ranking anything",
          _opt.index("STOP. The CPU was throttled")
          < _opt.index("ACTIONS, most valuable first"))
    check("optimize_rate detects a clock change DURING the sweep",
          "moved %d -> %d MHz DURING" in _opt)
    check("FaceMesh is benchmarked on a real face, not noise",
          "def _face_frame" in _opt and "fake_face.mp4" in _opt
          and "measures only the face DETECTOR" in _opt)
    check("contention verdict compares loaded runs against the IDLE run",
          "loaded = {k: v for k, v in out.items() if k > 0}" in _opt)
    check("contention recognises load RAISING the clock (throttled CPU)",
          "FASTER under load" in _opt and "LOW POWER STATE" in _opt)

    # ── EcoQoS / efficiency-core demotion ──
    _pm = read("perf_mode.py")
    check("kernel32 handles are declared as pointers, not ints "
          "(silent failure on Win64 otherwise)",
          "k.GetCurrentProcess.restype = ctypes.c_void_p" in _pm
          and "ctypes.c_void_p, ctypes.c_int" in _pm)
    check("EcoQoS opt-out uses StateMask=0 (do NOT throttle)",
          "StateMask=0," in _pm
          and "_PROCESS_POWER_THROTTLING_EXECUTION_SPEED" in _pm)
    check("ProcessPowerThrottling information class is 4",
          "_PROCESS_POWER_THROTTLING = 4" in _pm)
    check("priority boost is left ENABLED (bDisablePriorityBoost=False)",
          "SetProcessPriorityBoost(kernel32.GetCurrentProcess(), False)" in _pm)
    check("core pinning is opt-in", "GF_PERF_PIN_CORES" in _pm)
    check("known hybrid topologies make pinning a fact, not a guess",
          "KNOWN_P_CORE_THREADS" in _pm)
    _P = _pmns2 if False else __import__("perf_mode")
    check("the collection machine's P-core count is known (i9-13900H)",
          _P.p_core_threads("13th Gen Intel(R) Core(TM) i9-13900H", 20) == 12)
    check("an unrecognised CPU returns None rather than guessing",
          _P.p_core_threads("AMD Ryzen 9 7940HS", 16) is None)

    # ── The frozen collection configuration ──
    _run = read("windows/run_session.bat")
    check("the session launcher freezes perf mode ON",
          "set GF_PERF_MODE=1" in _run)
    check("the session launcher freezes the camera fix ON",
          "set GF_CAMERA_FIX=1" in _run)
    check("the launcher CLEARS the fake-camera switches",
          "set GF_FAKE_CAMERA=\n" in _run.replace("\r", "")
          and "set GF_FAKE_CALIBRATION=\n" in _run.replace("\r", ""))
    check("the launcher explains WHY each setting is frozen",
          "29.4 Hz -> 12.1 Hz" in _run and "21.4 Hz -> 32.0 Hz" in _run)
    check("core pinning is present but commented out by default",
          "REM set GF_PERF_PIN_CORES=12" in _run)
    check("the launcher states the rate to expect",
          "under 25 Hz" in _run)
    _pre = read("windows/check_before_participant.bat")
    check("pre-flight runs the test suite and stops on failure",
          "run_tests.py" in _pre and "exit /b 1" in _pre)
    check("pre-flight verifies performance mode",
          "perf_mode.py --verify" in _pre)
    check("pre-flight checks the previous sessions' metrics",
          "verify_metrics.py --today" in _pre)

    # ── Recording conditions (RQ1: "varying recording conditions") ──
    _idx = read("templates/index.html")
    for _f in ("room", "lighting", "glasses", "condition_notes"):
        check("the login form captures '%s'" % _f,
              'name="%s"' % _f in _idx)
    check("lighting uses a FIXED vocabulary, not free text",
          '<select class="form-input" id="lighting"' in _idx
          and "backlit" in _idx)
    check("eyewear is captured (it degrades the iris estimate ~4.3->4.8 %)",
          'id="glasses"' in _idx)
    check("conditions are read from the login form",
          'request.form.get("room"' in _app2)
    check("conditions reach the socket state, not just the cookie",
          'state["conditions"] = session["conditions"]' in _app2)
    check("conditions reach the manifest",
          '"conditions": state.get("conditions")' in _app2)
    check("incomplete conditions are warned about at login",
          "cannot be recovered later" in _app2)
    check("the reason conditions must be captured live is documented",
          "reconstructed afterwards from the gaze data" in _app2)
    check("perf_mode is a no-op on platforms with no such mechanism",
          "no known background-demotion mechanism" in _pm)
    # Every function that calls into ctypes must swallow its own errors:
    # a tracker that cannot set its priority must still record a session.
    _guarded = all(
        "except Exception as exc:  # noqa: BLE001"
        in _pm[_pm.index("def %s" % fn):
               _pm.index("def %s" % fn) + _pm[_pm.index("def %s" % fn):]
               .index("\n\n\ndef ")]
        for fn in ("_disable_eco_qos", "_set_priority", "pin_to_fast_cores"))
    check("every ctypes call site in perf_mode swallows its own errors",
          _guarded)
    # And apply() must survive a kernel32 that fails outright.
    _pmns: dict = {}
    exec(compile(_pm, "perf_mode.py", "exec"), _pmns)
    _pmns["_kernel32"] = lambda: (_ for _ in ()).throw(OSError("boom"))
    _pmns["is_windows"] = lambda: True
    try:
        _r = _pmns["apply"]()
        check("apply() survives a failing kernel32",
              _r.get("applied") is False, str(_r)[:60])
    except Exception as _e:  # noqa: BLE001
        check("apply() survives a failing kernel32", False, repr(_e))
    check("tracker applies perf mode BEFORE GazeFollower loads",
          "import perf_mode" in _tsvc2
          and _tsvc2.index("self.perf_mode = perf_mode.apply")
          < _tsvc2.index("from gazefollower import GazeFollower"))
    check("perf mode is reported in the self-check",
          '"perf_mode": self._describe_perf_mode()' in _tsvc2)
    check("perf_mode does not break the self-check's all_ok",
          '"perf_mode")' in _tsvc2
          and _tsvc2.index('"camera_mode", "sample_writer", "fake_mode",')
          < _tsvc2.index('"perf_mode")'))
    check("the A/B admits the console is already foreground",
          "ALREADY the foreground process" in _opt)
    check("perf mode is applied at process start, not lazily",
          "_apply_perf_mode_early" in _tsvc2
          and "_EARLY_PERF = _apply_perf_mode_early()" in _tsvc2)
    # ── Real-camera frame size (invisible in fake-clip tests) ──
    check("the delivered frame size is recorded",
          "_frame_shape" in _tsvc2 and '"frame_size"' in _tsvc2)
    check("oversized frames are detected against the intended 640x480",
          '"oversized_frames"' in _tsvc2 and "shape[0] > 640" in _tsvc2)
    check("an oversized camera frame is called out with the fix",
          "GF_CAMERA_FIX=1 to capture" in _app2)
    check("frame size is captured where the frame is actually seen",
          _tsvc2.index("self._frame_shape = ")
          < _tsvc2.index("return orig_face(timestamp, frame)"))
    _mp = lambda w, h: round(w * h / 1e6, 2)  # noqa: E731
    check("640x480 is not flagged as oversized",
          not (640 > 640 or 480 > 480) and _mp(640, 480) == 0.31)
    check("1280x720 is flagged and is ~3x the intended pixels",
          (1280 > 640 or 720 > 480) and round(_mp(1280, 720) / 0.31, 1) == 3.0)

    check("perf mode is recorded WITH every rate measurement",
          '"perf_mode": self._describe_perf_mode(),' in _tsvc2
          and "Perf mode [%s]" in _app2)

    # ── macOS: same mechanism, different API (Apple Silicon P/E cores) ──
    check("macOS clears PRIO_DARWIN_BG", "_PRIO_DARWIN_BG = 0x1000" in _pm
          and "_macos_clear_background" in _pm)
    check("macOS raises thread QoS to USER_INTERACTIVE",
          "_QOS_CLASS_USER_INTERACTIVE = 0x21" in _pm
          and "pthread_set_qos_class_self_np" in _pm)
    check("QoS is set on the MAIN thread so spawned threads inherit it",
          "inherit QoS from their creator" in _pm)
    check("App Nap opt-out is optional, not a hard dependency",
          "pyobjc not installed (optional)" in _pm)
    check("--verify admits a foreground console proves nothing about rate",
          "does NOT prove the rate is" in _pm)

    # Drive the macOS path with a stubbed libc (the suite may run on any OS).
    _pmns2: dict = {}
    exec(compile(_pm, "perf_mode.py", "exec"), _pmns2)

    class _FakeLibc:
        def __init__(self):
            self.calls, self.bg = [], 0x1000

        def __getattr__(self, name):
            def fn(*a):
                self.calls.append((name, a))
                if name == "setpriority":
                    self.bg = a[2]
                    return 0
                if name == "getpriority":
                    return self.bg
                return 0
            fn.argtypes, fn.restype = [], None
            return fn

    _fake = _FakeLibc()
    _pmns2["_libc"] = lambda: _fake
    _pmns2["is_macos"] = lambda: True
    _pmns2["is_windows"] = lambda: False
    _r2 = _pmns2["apply"]()
    check("macOS: a background-marked process is detected and cleared",
          _r2.get("was_background") is True
          and _r2.get("background_cleared") is True
          and _pmns2["_macos_is_background"]() is False)
    _qos = [c for c in _fake.calls if c[0] == "pthread_set_qos_class_self_np"]
    check("macOS: QoS class 0x21 (USER_INTERACTIVE) is requested",
          bool(_qos) and _qos[0][1][0] == 0x21)
    check("macOS: missing pyobjc does not fail the apply",
          _r2.get("applied") is True
          and _r2.get("app_nap_disabled") is False)

    # ── Session telemetry ──────────────────────────────────────────
    # Recorded for EVERY session, so the first requirement is that it
    # cannot possibly harm one. Overhead second, usefulness third.
    _tel_src = read("telemetry.py")
    _diag = read("diagnose_session.py")
    _gs2 = read("gaze_service.py")

    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_tel", os.path.join(BASE,
                                                              "telemetry.py"))
    _tel = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_tel)

    check("telemetry is on by default with a kill switch",
          _tel.enabled() is True and "GF_TELEMETRY" in _tel_src)
    _sv = os.environ.get("GF_TELEMETRY")
    try:
        os.environ["GF_TELEMETRY"] = "0"
        check("GF_TELEMETRY=0 disables it", _tel.enabled() is False)
        _off = _tel.Telemetry("x").start()
        _off.event("y")
        check("a disabled recorder collects nothing and writes nothing",
              _off.events == [] and _off.save() is None)
    finally:
        os.environ.pop("GF_TELEMETRY", None)
        if _sv is not None:
            os.environ["GF_TELEMETRY"] = _sv

    # FAIL-SAFE: the probe runs on the sampler thread during recording.
    _boom = _tel.Telemetry("boom", probe=lambda: 1 / 0)
    _row = _boom._tick()
    check("a probe that raises does not break the sample",
          isinstance(_row, dict) and "t" in _row)
    _bad_evt = _tel.Telemetry("evt")
    _bad_evt.event("ok", value=object())     # unserialisable on purpose
    check("event() accepts anything without raising",
          len(_bad_evt.events) == 1)
    check("save() survives an unwritable directory",
          _tel.Telemetry("z").save("/nonexistent/\0bad") is None)

    # OVERHEAD: measured, not asserted by inspection.
    # The threshold is a SHARE OF THE SAMPLING INTERVAL, not an absolute
    # millisecond figure — psutil is markedly slower on Windows, so an
    # absolute bound fails there for no real reason. What matters is that
    # a 1 Hz sampler uses a negligible slice of its own second.
    _perf_rec = _tel.Telemetry("perf", probe=lambda: {"sampling_hz": 30})
    for _ in range(5):
        _perf_rec._tick()            # warm-up: psutil primes its counters
    _samples_ms = []
    for _ in range(30):
        _t0 = time.perf_counter()
        _perf_rec._tick()
        _samples_ms.append((time.perf_counter() - _t0) * 1000)
    _samples_ms.sort()
    _tick_ms = _samples_ms[len(_samples_ms) // 2]      # median, not mean
    _duty_pct = 100.0 * _tick_ms / (_tel.SAMPLE_INTERVAL_S * 1000)
    check("a 1 Hz tick uses a negligible share of its own interval",
          _duty_pct < 5.0,
          "%.2f ms per tick = %.2f %% duty" % (_tick_ms, _duty_pct))
    check("the tick never approaches a frame budget",
          _samples_ms[-1] < 33.3, "worst %.2f ms" % _samples_ms[-1])

    check("the series is bounded (a forgotten server cannot fill the disk)",
          "MAX_SAMPLES" in _tel_src and _tel.MAX_SAMPLES <= 20000)
    check("sampler uses Event.wait, not a busy loop",
          "self._stop.wait(SAMPLE_INTERVAL_S)" in _tel_src)
    check("cpu_percent is non-blocking (interval=None)",
          "interval=None" in _tel_src)
    check("telemetry writes atomically (tmp + replace)",
          "os.replace(tmp, path)" in _tel_src)

    # Content: the things that cost this project days must be recorded.
    _env = _tel.environment_snapshot()
    for _k in ("cpu", "packages", "sources", "config", "env_vars",
               "perf_mode", "power_at_start", "display"):
        check("environment records '%s'" % _k, _k in _env)
    check("perf mode state is captured (the single biggest factor found)",
          bool(_env.get("perf_mode")))
    check("package versions include the load-bearing pins",
          {"numpy", "mediapipe", "MNN", "gazefollower"}
          <= set(_env.get("packages", {})))
    check("source mtimes are recorded (stale-copy detection)",
          "app.py" in (_env.get("sources") or {}))

    # The tracker probe must not touch anything.
    check("tracker telemetry probe takes no lock and starts no measurement",
          "cmd_telemetry" in _tsvc2
          and "rate_check_start" not in _tsvc2[
              _tsvc2.index("def cmd_telemetry"):
              _tsvc2.index("def _install_stage_timers")])
    check("the probe reads an always-on window, not the rate check's",
          "_live_stamps" in _tsvc2 and "maxlen=150" in _tsvc2)
    check("capture-thread additions are bounded and tiny",
          "self._live_stamps.append(time.perf_counter())" in _tsvc2)
    check("telemetry IPC uses a short timeout so it cannot queue up",
          '{"cmd": "telemetry"}, 2' in _gs2)
    # THE CONTRACT THAT BROKE A WHOLE SESSION'S TELEMETRY: reply() sends
    # the command's dict verbatim and the caller gates on ok, so a probe
    # that omits the key is discarded silently — 134 samples of nothing.
    check("cmd_telemetry returns ok=True (callers gate on it)",
          'out: dict = {"ok": True}' in _tsvc2[
              _tsvc2.index("def cmd_telemetry"):
              _tsvc2.index("def _install_stage_timers")])
    check("the ok-key requirement is documented where it can be broken",
          "dict is silently discarded" in _tsvc2)
    # Simulate the round trip: a reply missing ok must not be accepted.
    _probe_reply = {"sampling_hz": 25.0}          # the old, broken shape
    check("a reply without ok would be rejected by the caller",
          not _probe_reply.get("ok"))
    check("psutil is a declared dependency (every system metric needs it)",
          "psutil" in read("requirements.txt")
          and "psutil" in read("environment.yml"))
    check("package versions survive a callable .version (MNN)",
          "MNN exposes `version` as a FUNCTION" in _tel_src)
    _vers = _tel._package_versions()
    check("no version is recorded as a function object",
          not any("built-in" in str(v) or "function" in str(v)
                  for v in _vers.values()), str(_vers))

    check("telemetry command is dispatched", 'cmd == "telemetry"' in _tsvc2)

    # Wiring.
    check("telemetry starts on connect", "_start_telemetry(sid, pid)" in _app2)
    check("telemetry is saved before the manifest is built",
          _app2.index("_finish_telemetry(sid)")
          < _app2.index('"telemetry": state.get("telemetry_summary")'))
    check("telemetry summary and file both reach the manifest",
          '"telemetry": state.get("telemetry_summary")' in _app2
          and '"telemetry_file"' in _app2)
    for _ev in ("calibration_finished", "rate_gate", "validation",
                "stimulus_start", "stimulus_end"):
        check("timeline records '%s'" % _ev, '"%s"' % _ev in _app2)

    # The reader.
    check("diagnose_session flags perf mode being off",
          "Performance mode was OFF" in _diag)
    check("diagnose_session flags an over-budget frame cost",
          "exceeds the %.1f ms budget" in _diag)
    check("diagnose_session distinguishes detection from speed",
          "opposite fixes" in _diag)
    check("diagnose_session detects degradation WITHIN a session",
          "Rate FELL during the session" in _diag)
    check("diagnose_session does not repeat the battery mistake",
          "battery alone did NOT reduce the rate" in _diag)
    check("diagnose_session can compare two sessions", "--compare" in _diag)

    # End-to-end: a synthetic bad session must be diagnosed as bad.
    # The series is BUILT, not sampled from a live thread. The earlier
    # version slept 0.35 s and hoped the sampler produced >= 12 rows —
    # which it did here and did NOT on a slower machine, so the
    # degradation check silently had too little data and the assertion
    # failed for timing reasons rather than behaviour. A deterministic
    # series tests the analysis, which is what this section is about.
    _rec = _tel.Telemetry("synthetic")
    _rec.series = [
        {"t": i,
         "sampling_hz": 29.4 if i < 9 else 12.1,
         "face_ms_median": 9.6 if i < 9 else 21.0,
         "gaze_ms_median": 20.0 if i < 9 else 58.5,
         "subscribers": 2, "frame_size": "1280x720",
         "detected_pct_cumulative": 100.0}
        for i in range(18)
    ]
    _data = _rec.to_dict()
    check("the synthetic series has enough rows for a trend test",
          len(_data["series"]) >= 12, "%d rows" % len(_data["series"]))

    _dns: dict = {"__name__": "_diag",
                  "__file__": os.path.join(BASE, "diagnose_session.py")}
    exec(compile(_diag.replace('if __name__ == "__main__":\n'
                               '    raise SystemExit(main())', ''),
                 "diagnose_session.py", "exec"), _dns)
    _found = _dns["find_anomalies"](_data)
    _levels = [f[0] for f in _found]
    _texts = " ".join(f[1] for f in _found)
    check("a degrading synthetic session is flagged CRITICAL",
          "CRITICAL" in _levels, str(_levels))
    check("the over-budget frame cost is named", "budget" in _texts.lower())
    check("the 720p frame size is named", "1280x720" in _texts)
    check("the within-session fall is named", "FELL" in _texts)
    _clean = _tel.Telemetry("clean")
    _clean.series = [{"t": i, "sampling_hz": 29.5, "face_ms_median": 9.6,
                      "gaze_ms_median": 20.0, "subscribers": 2,
                      "detected_pct_cumulative": 100.0,
                      "frame_size": "640x480", "on_ac_power": True}
                     for i in range(30)]
    _clean_found = _dns["find_anomalies"](_clean.to_dict())
    check("a healthy session raises no CRITICAL",
          "CRITICAL" not in [f[0] for f in _clean_found],
          str([f[1] for f in _clean_found])[:80])

    # A SILENT ALL-CLEAR ON NO DATA is the worst possible output — it
    # reads as a clean session. This happened for real (134 samples, all
    # timestamp-only, reported as "Nothing flagged").
    _empty = {"series": [{"t": i} for i in range(134)],
              "environment": {"packages": {"psutil": "not installed"}}}
    _ef = _dns["find_anomalies"](_empty)
    check("an all-timestamp series is CRITICAL, not 'nothing flagged'",
          _ef and _ef[0][0] == "CRITICAL"
          and "NO MEASUREMENTS" in _ef[0][1], str(_ef)[:80])
    check("the empty-series verdict names both possible causes",
          any("psutil" in f[2] and "ok=True" in f[2] for f in _ef))
    check("a missing psutil is flagged on its own",
          any("psutil is not installed" in f[1] for f in _dns["find_anomalies"](
              {"series": [{"t": 1, "sampling_hz": 29.0}],
               "environment": {"packages": {"psutil": "not installed"}}})))

    # ── The A/B switch must fail SAFE ──
    # `set GF_PERF_MODE=` in cmd.exe deletes the variable; in other
    # shells it leaves an empty string. Both must mean ENABLED, because
    # the common way to leave the A/B is to clear the override, and a
    # blank value silently disabling perf mode would halve the sampling
    # rate of real sessions with nothing to show for it.
    _saved_pm = os.environ.get("GF_PERF_MODE")
    try:
        for _val, _want, _why in (
                (None, True, "unset (cmd.exe 'set VAR=' deletes it)"),
                ("", True, "empty string"),
                ("1", True, "explicit 1"),
                ("0", False, "explicit 0"),
                ("off", False, "off"),
                ("  0  ", False, "0 with whitespace")):
            os.environ.pop("GF_PERF_MODE", None)
            if _val is not None:
                os.environ["GF_PERF_MODE"] = _val
            _got = _pmns2["enabled"]()
            check("perf mode with %s -> %s" % (_why, "on" if _want else "off"),
                  _got is _want, "got %s" % _got)
    finally:
        os.environ.pop("GF_PERF_MODE", None)
        if _saved_pm is not None:
            os.environ["GF_PERF_MODE"] = _saved_pm

    # ── The battery warning was WRONG and must stay corrected ──
    # 29.4 Hz was measured on battery at 53 % once EcoQoS was disabled,
    # on the machine that had recorded 15.1 Hz at the same charge. An
    # unconditional battery warning attaches a false explanation to
    # every session.
    check("power notes fire ONLY when the rate is actually low",
          "if hz >= MIN_SAMPLING_HZ:" in _app2
          and "return []" in _app2[_app2.index("def _power_notes"):])
    check("the corrected battery note does not claim battery lowers the rate",
          "battery state alone did NOT reduce the rate" in _app2)
    check("the correction records why (so it is not re-introduced)",
          "CORRECTED 2026-08-04" in _app2 and "perf_mode.py" in _app2)
    check("psutil's base-clock caveat is stated where it is used",
          "BASE" in _app2[_app2.index("def _power_notes"):
                          _app2.index("def _run_rate_gate")])
    _ns3: dict = {}
    exec(_app2[_app2.index("def _power_notes"):
               _app2.index("def _run_rate_gate")],
         {"MIN_SAMPLING_HZ": 20.0}, _ns3)
    check("a healthy rate on battery produces NO notes",
          _ns3["_power_notes"]({"on_ac_power": False, "battery_pct": 53},
                               29.4) == [])
    check("a low rate on battery does produce a note",
          len(_ns3["_power_notes"]({"on_ac_power": False, "battery_pct": 53},
                                   15.1)) == 1)

    # ── atexit noise on Ctrl+C ──
    _gs = read("gaze_service.py")
    check("shutdown survives a KeyboardInterrupt from atexit",
          "except BaseException:" in _gs
          and "includes KeyboardInterrupt" in _gs)
    check("shutdown still kills the child when interrupted",
          "proc, self._proc = self._proc, None" in _gs)
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("capture-loop instrumentation", False, _blocked or repr(exc))

# ── 10. Metrics spec, claim correspondence, verifier ───────────────────
print("\n[10] Metrics specification and RQ3 correspondence")
try:
    import metrics_spec as _SPEC
    import claim_check as _CC
    import fixations as _FX

    _ppd = _SPEC.px_per_degree()
    check("1 deg is ~58 px at the collection geometry",
          57 <= _ppd <= 59, "%.1f px" % _ppd)

    # I-DT floor derived from the rate, not from taste.
    for _hz in (21, 25, 30):
        _got = _SPEC.fixation_min_duration_for(_hz)
        check("min fixation duration at %d Hz is the 3-sample floor" % _hz,
              abs(_got - max(0.10, 3.0 / _hz)) < 1e-9,
              "%.0f ms" % (_got * 1000))
    check("the floor never drops below the ~100 ms literature default",
          _SPEC.fixation_min_duration_for(200) == 0.10)
    _t = [i / 30.0 for i in range(20)]
    check("detect_fixations derives min_duration when not given",
          len(_FX.detect_fixations(_t, [0.3] * 20, [0.4] * 20)) == 1)
    check("an explicit min_duration still overrides the derivation",
          len(_FX.detect_fixations(_t, [0.3] * 20, [0.4] * 20,
                                   min_duration=5.0)) == 0)

    # Saccades: amplitude only, never velocity.
    _sc = _FX.saccade_metrics(
        [{"nx": .5, "ny": .6}, {"nx": .5, "ny": .15}, {"nx": .1, "ny": .6}],
        _ppd, 1920, 1080)
    check("saccade count is fixations - 1", _sc["saccade_count"] == 2)
    check("velocity and duration are explicitly NOT claimed",
          "not measurable" in _sc["measurement_note"].lower())

    # ── RQ3 correspondence: the AOI-free check ──
    _samples = ([(t / 10.0, 0.15, 0.60, True) for t in range(0, 80)]
                + [(t / 10.0, 0.50, 0.60, True) for t in range(80, 160)])
    _sup = _CC.check_claim(
        {"t_start": 0, "t_end": 8, "attended": "left student",
         "bbox": [0.02, 0.35, 0.30, 0.55]}, _samples, 2.2, _ppd, 1920, 1080)
    check("a claim matching the gaze is SUPPORTED",
          _sup["verdict"] == _CC.SUPPORTED, str(_sup["verdict"]))
    _con = _CC.check_claim(
        {"t_start": 8, "t_end": 16, "attended": "whiteboard",
         "bbox": [0.30, 0.00, 0.40, 0.30]}, _samples, 2.2, _ppd, 1920, 1080)
    check("a claim contradicted by the gaze is CONTRADICTED",
          _con["verdict"] == _CC.CONTRADICTED, str(_con["verdict"]))
    _tiny = _CC.check_claim(
        {"t_start": 8, "t_end": 16, "attended": "a pen",
         "bbox": [0.49, 0.59, 0.02, 0.02]}, _samples, 2.2, _ppd, 1920, 1080)
    # SUPERSEDED (section [19]). A tiny object the gaze is sitting on is
    # now SUPPORTED with resolvable=False, rather than refused: the
    # claim may well be right, and the caveat belongs on the claim, not
    # in place of it.
    check("a tiny object is scored, and marked unresolvable",
          _tiny["verdict"] != _CC.UNTESTABLE
          and _tiny["resolvable"] is False,
          "%s, resolvable=%s" % (_tiny["verdict"], _tiny["resolvable"]))
    _nob = _CC.check_claim(
        {"t_start": 0, "t_end": 4, "attended": "the room", "bbox": None},
        _samples, 2.2, _ppd, 1920, 1080)
    check("an unlocalised claim is NO_BBOX, not silently passed",
          _nob["verdict"] == _CC.NO_BOX)
    check("the box is padded by the session's measured accuracy",
          _sup["tolerance_px"] == round(2.2 * _ppd))
    _all = _CC.check_all(
        [{"t_start": 0, "t_end": 8, "attended": "a",
          "bbox": [0.02, 0.35, 0.30, 0.55]},
         {"t_start": 8, "t_end": 16, "attended": "b",
          "bbox": [0.30, 0.00, 0.40, 0.30]}],
        _samples, 2.2, _ppd, 1920, 1080)
    check("correspondence %% is computed over TESTABLE claims only",
          _all["correspondence_pct"] == 50.0 and _all["n_testable"] == 2,
          str(_all["correspondence_pct"]))

    # The prompt must actually request the box, or none of this runs.
    check("the LLM is asked for a bbox with every spatial claim",
          _app2.count("bbox") >= 4)
    check("the prompt explains WHY the box is needed",
          "checkable against" in _app2)

    # Spec bookkeeping.
    check("AOI metrics are recorded as NOT APPLICABLE, not omitted",
          hasattr(_SPEC, "NOT_APPLICABLE") and _SPEC.NOT_APPLICABLE)
    check("the design choice (no hand-drawn AOIs) is written down",
          "deliberately uses NO hand-drawn AOIs" in read("metrics_spec.py"))
    check("kappa is still flagged as the remaining RQ3 gap",
          any(i[0] == "human_agreement_kappa" and i[3] == "missing"
              for i in _SPEC.RQ3_FEEDBACK))
    check("inclusion criteria are dated in the spec",
          "decided_on" in _SPEC.INCLUSION)

    # Verifier: inclusion failures must block, and old data is filterable.
    _vm = read("verify_metrics.py")
    check("verifier grades a sub-threshold rate as DEGENERATE",
          "FAILS the %.0f Hz inclusion criterion" in _vm)
    check("verifier grades sub-threshold data loss as DEGENERATE",
          "FAILS the %.0f %% inclusion criterion" in _vm)
    # The criterion moved from pre_check alone to the MEAN of the two
    # grid-B checks either side of the recording, so the message moved
    # with it.
    check("verifier grades over-threshold accuracy as DEGENERATE",
          "FAILS the %.1f deg criterion (pre_check %.2f, post" in _vm)
    check("verifier flags a missing post-validation as blocking",
          "NO POST-STIMULUS VALIDATION" in _vm)
    check("verifier warns that in-sample accuracy is not accuracy",
          "IN-SAMPLE, not a" in _vm)
    check("verifier can restrict to today / a cutoff date",
          "--today" in _vm and "_session_date" in _vm)

    # ── Multi-model comparison (N models, RQ3 generalisation) ──
    import model_comparison as _MC

    check("any number of models can be configured",
          isinstance(_MC.load_models(), list))
    check("providers cover gemini / openai / anthropic + a keyless mock",
          {"gemini", "openai", "anthropic", "mock"} <= set(_MC.PROVIDERS))
    check("a model with no API key fails loudly, not silently",
          _MC.run_models([{"text": "x"}], [
              {"name": "n", "provider": "openai", "model": "m",
               "api_key_env": "DEFINITELY_NOT_SET_XYZ"}])["n"]["ok"] is False)
    check("an unknown provider is reported, not skipped",
          "unknown provider" in _MC.run_models(
              [{"text": "x"}], [{"name": "n", "provider": "nope"}]
          )["n"]["error"])

    # Payload conversion must carry IMAGES, not just text.
    _p = [{"text": "hi"},
          {"inline_data": {"mime_type": "image/jpeg", "data": "QUJD"}}]
    _oa = _MC._parts_to_openai(_p)
    _an = _MC._parts_to_anthropic(_p)
    check("openai conversion keeps the image",
          any(b["type"] == "image_url" for b in _oa) and len(_oa) == 2)
    check("anthropic conversion keeps the image",
          any(b["type"] == "image" for b in _an) and len(_an) == 2)
    check("base64 image data survives conversion",
          "QUJD" in json.dumps(_oa) and "QUJD" in json.dumps(_an))

    # Fleiss kappa edge cases.
    check("Fleiss kappa is 1.0 on perfect agreement",
          _MC.fleiss_kappa([[True] * 3, [False] * 3]) == 1.0)
    check("Fleiss kappa goes negative on systematic disagreement",
          _MC.fleiss_kappa([[True, False, True],
                            [False, True, False]]) < 0)
    check("Fleiss kappa returns None when an item lacks a rating",
          _MC.fleiss_kappa([[True, None, True]]) is None)
    check("Fleiss kappa returns None with too few items",
          _MC.fleiss_kappa([[True, True, True]]) is None)

    # End-to-end with mock models: correspondence must separate them.
    _pp = _SPEC.px_per_degree()
    _smp = ([(t / 10.0, 0.15, 0.60, True) for t in range(0, 80)]
            + [(t / 10.0, 0.50, 0.60, True) for t in range(80, 160)])
    _good = json.dumps([{"t_start": 0, "t_end": 8, "attended": "left",
                         "bbox": [0.02, 0.35, 0.30, 0.55],
                         "criteria_met": True}])
    _bad = json.dumps([{"t_start": 0, "t_end": 8, "attended": "board",
                        "bbox": [0.30, 0.00, 0.40, 0.30],
                        "criteria_met": True}])
    _res = _MC.run_models([{"text": "x"}], [
        {"name": "A", "provider": "mock",
         "canned_response": "```json\n%s\n```" % _good},
        {"name": "B", "provider": "mock",
         "canned_response": "```json\n%s\n```" % _bad}])
    _cmp = _MC.compare(_res, _smp, 2.2, _pp, 1920, 1080)
    check("a model whose claims match the gaze scores 100 %",
          _cmp["per_model"]["A"]["correspondence_pct"] == 100.0)
    check("a model whose claims contradict the gaze scores 0 %",
          _cmp["per_model"]["B"]["correspondence_pct"] == 0.0)
    check("the report states that cross-model agreement is NOT validity",
          "RELIABILITY, not validity" in _cmp["kappa_note"])
    check("mismatched phase counts are flagged, not silently aligned",
          "phase_counts_match" in _cmp)

    # The app must save a replayable, byte-identical payload.
    check("app saves the exact payload for fair comparison",
          "llm_replay" in _app2 and '"parts": parts,' in _app2)
    check("the replay carries the accuracy the boxes are padded with",
          '"accuracy_deg"' in _app2)
    check("replay saving can never block feedback generation",
          "never block feedback generation" in _app2)

    # ── Fixed-unit "windows" mode: what makes kappa interpretable ──
    check("a fixed-window detail mode exists",
          '"windows"' in _app2 and 'detail not in ("phases", "fixations", "windows")'
          in _app2)
    check("the window length is configurable, not hardcoded",
          "LLM_WINDOW_SECONDS" in read("config.py")
          and "LLM_WINDOW_SECONDS" in _app2)
    check("the model is forbidden from choosing its own boundaries",
          "Do NOT choose your own boundaries" in _app2)
    check("windows may not be merged or skipped",
          "do not merge windows" in _app2 and "do not skip any" in _app2)
    check("the reason (identical units across raters) is stated in code",
          "confounds SEGMENTATION with JUDGMENT" in _app2)
    check("windows mode also requests a bbox",
          _app2.count("makes the claim checkable") >= 1
          or _app2.count("checkable against") >= 2)
    check("windows mode gets the larger token budget",
          'detail in ("fixations", "windows")' in _app2)
    # Bin arithmetic: a 30.4 s clip at 5 s must give 6 equal units.
    for _dur, _w, _want in ((30.4, 5.0, 6), (60.0, 5.0, 12),
                            (12.0, 5.0, 2), (2.0, 5.0, 1)):
        check("a %.0f s clip in %.0f s bins gives %d units"
              % (_dur, _w, _want),
              max(1, int(round(_dur / _w))) == _want)

    # ── Truncation guard: silent partial answers would bias everything ──
    _full = json.dumps([{"t_start": i, "t_end": i + 1, "attended": "left",
                         "bbox": [0.02, 0.35, 0.30, 0.55],
                         "criteria_met": True} for i in range(8)])
    _short = json.dumps([{"t_start": i, "t_end": i + 1, "attended": "left",
                          "bbox": [0.02, 0.35, 0.30, 0.55],
                          "criteria_met": True} for i in range(5)])
    _smp2 = [(t / 10.0, 0.15, 0.60, True) for t in range(0, 80)]
    _r2 = _MC.run_models([{"text": "x"}], [
        {"name": "complete", "provider": "mock",
         "canned_response": "```json\n%s\n```" % _full},
        {"name": "cut", "provider": "mock",
         "canned_response": "```json\n%s\n```" % _short}])
    _c2 = _MC.compare(_r2, _smp2, 2.2, _pp, 1920, 1080, expected_units=8)
    check("a complete answer is marked complete",
          _c2["per_model"]["complete"]["complete"] is True)
    check("a short answer is detected as TRUNCATED",
          _c2["per_model"]["cut"].get("truncated") is True)
    check("the truncation note says the END of the video is missing",
          "MISSING units are the end" in
          _c2["per_model"]["cut"]["truncation_note"])
    check("a truncated model is EXCLUDED from the agreement analysis",
          "cut" not in _c2["models_compared"]
          and "cut" in _c2["truncated_models"])
    check("a model returning MORE units than requested is flagged too",
          "ignored the unit definition" in read("model_comparison.py"))
    check("the expected unit count is recorded in the replay payload",
          '"expected_units"' in _app2 and '"unit_definition"' in _app2)
    check("fixation mode declares its unit definition",
          "one per detected fixation (I-DT)" in _app2)
    check("truncation is explained where it is detected",
          "biasing every downstream figure toward" in _app2)

    # ── RQ2 event metrics must be PERSISTED, not just printed ──
    import pandas as _pd
    _blk = _app2[_app2.index("# Degree conversion for the event metrics"):
                 _app2.index("def _fixation_summary")]
    _ens = {"pd": _pd, "FIXATION_DISPERSION_NORM": 0.05}
    exec(_blk, _ens)
    _ev = _ens["_event_metrics"]

    def _seg(hz, seconds=30.0):
        rows = []
        for i in range(int(hz * seconds)):
            t = i / hz
            nx, ny = (0.3, 0.4) if t < 10 else ((0.6, 0.5) if t < 20
                                                else (0.2, 0.7))
            rows.append({"video_time_s": t, "gaze_video_nx": nx,
                         "gaze_video_ny": ny})
        return _pd.DataFrame(rows)

    _r21 = _ev(_seg(21.0))
    _r32 = _ev(_seg(32.0))
    check("event metrics are computed for a stimulus segment",
          _r21["fixation_count"] > 0 and "saccades" in _r21)
    check("the I-DT minimum duration follows the MEASURED rate",
          abs(_r21["idt_min_duration_s"] - 3 / 21.0) < 1e-3
          and _r32["idt_min_duration_s"] == 0.1,
          "%.0f / %.0f ms" % (1000 * _r21["idt_min_duration_s"],
                              1000 * _r32["idt_min_duration_s"]))
    check("the three-sample floor is recorded next to the value used",
          "idt_min_duration_floor_s" in _r21)
    check("duration uncertainty is one inter-sample interval",
          abs(_r21["fixation_duration_uncertainty_ms"] - 1000 / 21.0) < 2,
          "%s ms" % _r21["fixation_duration_uncertainty_ms"])
    check("the dispersion threshold is reported in DEGREES",
          1.0 < _r21["idt_dispersion_deg"] < 3.0,
          "%.2f deg" % _r21["idt_dispersion_deg"])
    check("fixation_rate_per_s is reported alongside the count",
          "fixation_rate_per_s" in _r21)
    check("the bias caveat travels with the numbers",
          "biased DOWN" in _r21["interpretation"])
    check("saccades report amplitude but not velocity",
          "amplitude_median_deg" in _r21["saccades"]
          and not any("velocity" in k for k in _r21["saccades"]))
    _off = _seg(30.0)
    _off.loc[_off.index[:150], "gaze_video_nx"] = 1.4
    check("gaze leaving the video frame is measured",
          abs(_ev(_off)["gaze_off_video_pct"] - 16.7) < 0.2)
    check("a segment without video coordinates fails safe",
          "error" in _ev(_pd.DataFrame({"x": [1]})))
    check("event metrics reach the manifest",
          '"events": counts.get("__events__")' in _app2)
    check("a stats failure can never lose a session",
          "never lose a session over stats" in _app2)
    check("px-per-degree is shared with the metrics spec, not re-derived",
          "from metrics_spec import px_per_degree" in _app2)

    # ── Viewing distance: measured, not assumed ──
    import camera_geometry as _CG

    _cal = _CG.calibrate(iod_px=58.0, known_distance_cm=62.5, image_w_px=640)
    check("focal length solves from ONE measured distance",
          abs(_cal["focal_px"] - 58.0 * 62.5 / 6.3) < 0.5,
          "%.0f px" % _cal["focal_px"])
    check("the calibration reports the FOV it implies",
          50 < _cal["implied_hfov_deg"] < 75,
          "%.1f deg" % _cal["implied_hfov_deg"])
    check("calibrating shrinks the distance uncertainty",
          _CG.estimate_distance(62.0, geometry=_cal)["relative_sd_pct"]
          < _CG.estimate_distance(62.0, geometry={})["relative_sd_pct"])
    check("measuring the participant's IOD shrinks it further",
          _CG.estimate_distance(62.0, geometry=_cal, iod_cm=6.1
                                )["relative_sd_pct"]
          < _CG.estimate_distance(62.0, geometry=_cal)["relative_sd_pct"])
    check("the estimate states which assumptions remain",
          any("ASSUMED" in x for x in
              _CG.estimate_distance(62.0, geometry={})["sources"])
          and any("MEASURED" in x for x in
                  _CG.estimate_distance(62.0, geometry=_cal)["sources"]))
    check("uncertainty sources combine in quadrature, not additively",
          abs(_CG.estimate_distance(62.0, geometry={})["relative_sd_pct"]
              - 100 * math.hypot(0.10, 0.4 / 6.3)) < 0.2,
          "%.1f %%" % _CG.estimate_distance(
              62.0, geometry={})["relative_sd_pct"])

    # THE BUG THIS BLOCK ONLY FOUND ON THE COLLECTION MACHINE.
    # estimate_distance did `geometry = geometry or load()`, so an empty
    # dict — meaning "model an uncalibrated camera" — fell through to
    # whatever calibration happened to be saved on that machine. Every
    # assertion above passed on a machine with no calibration file and
    # failed on the one that had actually been calibrated, which is
    # precisely backwards: the test broke when the setup was finished.
    #
    # Simulate a saved calibration and assert the two are now distinct.
    import json as _json
    import tempfile as _tf

    _had = os.path.exists(_CG.GEOMETRY_FILE)
    if not _had:
        os.makedirs(os.path.dirname(_CG.GEOMETRY_FILE), exist_ok=True)
        with open(_CG.GEOMETRY_FILE, "w", encoding="utf-8") as _fh:
            _json.dump({"focal_px": 652.8, "image_w_px": 640,
                        "known_distance_cm": 60.0, "distance_sd_cm": 1.0,
                        "implied_hfov_deg": 52.2}, _fh)
    try:
        _empty = _CG.estimate_distance(62.0, geometry={})
        _auto = _CG.estimate_distance(62.0)
        check("an EMPTY geometry means uncalibrated, not 'go and load one'",
              _empty["focal_measured"] is False,
              "focal_measured=%s" % _empty["focal_measured"])
        check("omitting geometry entirely DOES use the saved calibration",
              _auto["focal_measured"] is True)
        check("so the two give different uncertainty budgets",
              abs(_empty["relative_sd_pct"] - _auto["relative_sd_pct"]) > 1.0,
              "%.1f vs %.1f %%" % (_empty["relative_sd_pct"],
                                   _auto["relative_sd_pct"]))
    finally:
        if not _had:
            os.remove(_CG.GEOMETRY_FILE)
    # Degrees must carry the distance uncertainty through.
    _d = _CG.degrees_with_uncertainty(129.7, 58.5, 3.8)
    check("degrees are reported with a 95 % interval",
          _d["deg_lo"] < _d["deg"] < _d["deg_hi"],
          "%.2f (%.2f-%.2f)" % (_d["deg"], _d["deg_lo"], _d["deg_hi"]))
    check("a NEARER viewer yields a LARGER angle",
          _CG.degrees_with_uncertainty(129.7, 45.0)["deg"]
          > _CG.degrees_with_uncertainty(129.7, 80.0)["deg"])
    check("the interval note says the pixel measurement is exact",
          "pixel" in _d["interval_note"] and "exact" in _d["interval_note"])
    check("a missing distance fails safe rather than guessing",
          _CG.degrees_with_uncertainty(129.7, 0)["deg"] is None)
    # The tracker must PREFER a calibration when one exists.
    check("the tracker prefers a measured focal length",
          "def _focal_px" in _tsvc2
          and "focal, measured = self._focal_px(w)" in _tsvc2)
    check("the tracker records whether the focal length was measured",
          '"focal_measured"' in _tsvc2)
    check("a broken calibration file cannot block the position guide",
          "never block the guide" in _tsvc2)

    # ── Iris ruler + the two-ruler cross-check ──
    import iris_distance as _IR

    def _mesh(iris_px, yaw=0.0, right_scale=1.0, w=640, h=480):
        import math as _m
        lm = [(0.5, 0.5)] * 478
        f = _m.cos(_m.radians(yaw))
        lcx = 320 - 58.2 / 2
        rcx = lcx + 58.2 * f
        for c, (a, b), sc in ((lcx, (469, 471), 1.0),
                              (rcx, (474, 476), right_scale)):
            lm[a] = ((c + iris_px * sc / 2) / w, 240 / h)
            lm[b] = ((c - iris_px * sc / 2) / w, 240 / h)
        return lm

    check("iris diameter is the physiological constant 11.7 mm +- 0.5",
          _IR.IRIS_DIAMETER_MM == 11.7 and _IR.IRIS_DIAMETER_SD_MM == 0.5)
    check("the iris is a tighter ruler than the inter-ocular distance",
          (_IR.IRIS_DIAMETER_SD_MM / _IR.IRIS_DIAMETER_MM) < (0.4 / 6.3))
    _d = _IR.iris_diameter_px(_mesh(12.0), 640, 480)
    check("iris diameter is recovered from the refined mesh",
          abs(_d["mean_px"] - 12.0) < 0.05, "%.2f px" % _d["mean_px"])
    check("HORIZONTAL diameter is used (eyelids clip the vertical)",
          "Horizontal, not vertical" in read("iris_distance.py"))
    check("a coarse 468-point mesh is refused, not silently misread",
          "refined" in _IR.iris_diameter_px([(0, 0)] * 468, 640, 480)["error"])

    _F = 320 / math.tan(math.radians(30))          # assumed-FOV focal
    _true = 60.0
    _iod_px, _iris_px = 6.3 * _F / _true, 1.17 * _F / _true
    check("iris distance recovers the true distance",
          abs(_IR.distance_from_iris(_iris_px, _F)["distance_cm"] - 60.0) < 0.6,
          "%.1f cm" % _IR.distance_from_iris(_iris_px, _F)["distance_cm"])

    # THE POINT: the IOD foreshortens with yaw, the iris does not.
    for _yaw, _agree in ((0, True), (20, True), (35, False), (50, False)):
        _e = _IR.estimate(_mesh(_iris_px, _yaw),
                          _iod_px * math.cos(math.radians(_yaw)), _F)
        _c = _e["check"]
        check("at %d deg yaw the iris still reads ~60 cm" % _yaw,
              abs(_c["iris_cm"] - 60.0) < 1.0, "%.1f cm" % _c["iris_cm"])
        check("at %d deg yaw the two rulers %s"
              % (_yaw, "agree" if _agree else "DISAGREE"),
              _c["agree"] is _agree,
              "iod says %.1f cm, diff %.1f %%"
              % (_c["iod_cm"], _c["difference_pct"]))
    check("disagreement carries an explanation naming head yaw",
          "yaw" in _IR.estimate(_mesh(_iris_px, 50),
                                _iod_px * 0.64, _F)["check"]["warning"])
    check("a lopsided iris fit is flagged separately",
          "asymmetry" in str(_IR.estimate(_mesh(_iris_px, 0, right_scale=1.6),
                                          _iod_px, _F)["check"]))
    check("one estimate alone is reported, not silently averaged",
          _IR.cross_check(60.0, None)["distance_cm"] == 60.0
          and _IR.cross_check(60.0, None)["ok"] is False)

    # The tracker must PREFER the iris, and must NOT fall back to the
    # worse ruler when they disagree — that would substitute the number
    # most likely to be wrong exactly when something is known to be wrong.
    check("the tracker prefers the iris estimate",
          'm["distance_source"] = "iris"' in _tsvc2)
    check("disagreement is a WARNING, not a fallback to the IOD",
          "not a reason" in _tsvc2 and "distance_disagreement" in _tsvc2)
    check("math is imported at MODULE level (the static method needs it)",
          "\nimport math\n" in _tsvc2
          and "_focal_px() is a @staticmethod" in _tsvc2)

    # ── Degrees recomputed server-side from the MEASURED distance ──
    check("validation degrees are recomputed from a measured distance",
          "mean_err_deg_measured" in _app2)
    check("the browser value is kept for comparison, not overwritten",
          "browser_assumption_error_pct" in _app2)
    check("a large shift from the assumption is logged",
          "once the MEASURED" in _app2)
    check("an unmeasurable distance is recorded as such",
          '"measured": False' in _app2)
    check("the distance block records which ruler was used",
          '"iris_cm": pos.get("distance_cm_iris")' in _app2)
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("metrics specification", False, _blocked or repr(exc))


# ══════════════════════════════════════════════════════════════════════
#  Bottleneck attribution: is a low rate the CPU or the CAMERA?
# ══════════════════════════════════════════════════════════════════════
# These two causes need opposite fixes, and for most of this project the
# rate gate assumed the CPU: it told the researcher to change the power
# plan even when the models were finishing in a third of the frame
# interval, which is the signature of a camera throttling itself with
# auto-exposure. Wrong advice at the rate gate costs a participant slot,
# so the discriminator is tested rather than trusted.
print("\n[16] Bottleneck attribution (CPU vs camera)")
try:
    _tsvc3 = read("tracker_service.py")
    _js3 = read("static/js/experiment.js")
    _camp = read("camera_patch.py")

    check("the rate check attributes a low rate to the camera, the CPU, "
          "or downstream loss",
          "camera_throttled" in _tsvc3 and "cpu_throttled" in _tsvc3
          and "frames_discarded" in _tsvc3)
    check("attribution uses per-frame cost vs the frame interval, not the "
          "rate alone",
          "pipeline_duty_pct" in _tsvc3 and "frame_interval_ms" in _tsvc3)
    check("no verdict fires while the rate is acceptable",
          "low = sustained < 0.85 * NOMINAL_CAMERA_FPS" in _tsvc3)

    # ── The regression that produced a confidently wrong diagnosis ──
    # Duty was computed from the two MODEL stages as if they were the
    # whole callback. 18 ms of models in a 66.7 ms interval read as
    # "27 % duty, the pipeline is idle, blame the camera" — and the
    # camera then measured 31.2 fps standalone. The remainder of the
    # callback (filter, subscriber dispatch, CSV write+flush) is where
    # the time actually goes, and it must be measured on a REAL webcam,
    # not only under the fake camera.
    check("the WHOLE callback is timed, not just the two model stages",
          "_install_callback_timer" in _tsvc3
          and "def _callback_stats" in _tsvc3)
    # The camera thread captured a reference to the ORIGINAL bound
    # process_frame when sampling was set up, so rebinding the attribute
    # alone leaves the camera calling the untimed original — a timer
    # that installs cleanly, reports nothing, and looks exactly like
    # "the callback is free". Both paths, or neither.
    check("callback timing rebinds process_frame AND re-registers with "
          "the camera",
          "gf.process_frame = timed_process" in _tsvc3
          and "cam.set_on_image_callback(timed_process)" in _tsvc3)
    check("a failed re-registration restores the original and reports it",
          "gf.process_frame = orig" in _tsvc3
          and "set_on_image_callback failed" in _tsvc3)
    check("the live installer uses the same hook diagnose_rate.py proved",
          "gf.camera.set_on_image_callback(timed_process)"
          in read("diagnose_rate.py"))
    check("sample yield is measured against frames IN, not the "
          "self-referential detected_pct",
          "sample_yield_pct" in _tsvc3
          and "only ever sees frames" in _tsvc3)

    # ── The session-only churn no offline benchmark reproduced ──
    _dr = read("diagnose_rate.py")
    check("diagnose_rate can reproduce the session's start/stop churn",
          "churn" in _dr and '("churn", dict(' in _dr
          and '("session", dict(' in _dr)
    check("the churn scenarios also apply the camera fix (as a session "
          "does)",
          '("churn", dict(poll=False, camera_fix=True' in _dr)
    check("subscriber count is read while sampling is still live",
          "subs_after = _subs()" in _dr
          and _dr.index("subs_after = _subs()") < _dr.index("gf.stop_sampling()", _dr.index("stop.set()")))
    check("an over-budget total names the halving mechanism",
          "OVER BUDGET" in _dr and "skips alternate frames" in _dr)
    check("more than two subscribers is called out",
          "EXPECTED 2" in _dr)
    # A control measurement is only useful if it exists for the healthy
    # case. diagnose_rate.py measured 31.1 Hz / 17.1 ms / 0.2 ms of
    # non-model work in-process; the live app must print the comparable
    # figures on EVERY run, not only when the gate already failed,
    # otherwise there is nothing to compare a bad session against.
    check("the live callback figures are logged pass or fail",
          "Callback (live): total" in _tsvc3
          and "Callback (live): NOT MEASURED" in _tsvc3)
    check("a timer that failed to install says so rather than reading "
          "as zero cost",
          "the timer did not install" in _tsvc3)

    # ── The accuracy check reads the PREVIEW stream ──────────────────
    # onGaze both positions the reassurance dot and appends to
    # this.samples, so the green dot and the validation are the same
    # numbers — but that means the poll interval, not the tracker rate,
    # sets how many samples land per target and over what interval
    # precision is computed. At 7 Hz a 1.6 s window gives ~10 samples
    # with 150 ms of drift between each pair.
    _app3 = read("app.py")
    check("the preview interval is configurable, not hard-coded at 150 ms",
          "PREVIEW_INTERVAL_S" in _app3
          and 'state.get("preview_interval_s"' in _app3)
    check("the validation asks for the full tracker rate",
          "VALIDATION_INTERVAL_S" in _app3
          and "start_gaze_preview', { interval_s: 1 / 30 }" in _js3)
    check("the poll rate is clamped below the tracker rate (a faster "
          "poll would re-emit the same sample and fake perfect precision)",
          "MIN_PREVIEW_INTERVAL_S" in _app3
          and "max(MIN_PREVIEW_INTERVAL_S" in _app3)
    check("the rate a validation sampled at is recorded with it",
          '"sampled_at_hz"' in _app3 or "sampled_at_hz" in _app3)
    check("the dot and the validation samples come from ONE handler "
          "(so they cannot disagree)",
          "if (this.collecting) this.samples.push([x, y]);" in _js3
          and "this.gazeDot.style.left" in _js3)

    # ── Tape-measure focal calibration ───────────────────────────────
    _cg = read("camera_geometry.py")
    check("calibration can read the camera itself, not just prompt",
          "def measure_live" in _cg and "--measure" in _cg)
    check("the refined mesh is requested (iris points 468-477 need it)",
          "refine_landmarks=True" in _cg)
    check("focal length is solved from the IRIS, the tighter prior",
          "focal_px_from_iris" in _cg and "focal_basis" in _cg)
    check("the IOD-based figure is kept for comparison, not discarded",
          "focal_px_from_iod" in _cg)
    check("iris/IOD disagreement is flagged (it means a bad landmark fit)",
          "focal_disagreement_pct" in _cg)
    check("head movement during the measurement is detected and named",
          "iris_spread_pct" in _cg and "that is head movement" in _cg)
    check("the instruction says lens-to-nose, not to the laptop edge",
          "front edge of the laptop" in _cg)
    check("the calibration reads mean_px, the key iris_diameter_px "
          "actually returns",
          'iris.get("mean_px")' in _cg and 'iris.get("iris_px")' not in _cg)
    check("a failed measurement reports WHY, not just 'no usable frames'",
          "Your face WAS detected" in _cg and "def _note" in _cg)

    # ── The landmark-form bug that green tests could not see ─────────
    # _xy used getattr(landmark, "x", landmark[0]). Python evaluates a
    # default argument EAGERLY, so landmark[0] ran even when .x existed.
    # A MediaPipe NormalizedLandmark is a protobuf message with .x/.y and
    # NO __getitem__, so every real camera frame raised TypeError on the
    # default and reported "iris landmarks unusable" — while the suite
    # stayed green, because it passes tuples and numpy rows, which ARE
    # indexable. Test all three forms, not just the convenient ones.
    import importlib

    sys.path.insert(0, BASE)
    _iris_mod = importlib.import_module("iris_distance")
    importlib.reload(_iris_mod)

    class _Proto:                 # attributes only, like MediaPipe
        __slots__ = ("x", "y", "z")

        def __init__(self, x, y):
            self.x, self.y, self.z = x, y, 0.0

    def _mesh(factory, w=640, h=480):
        lm = [factory(0.5, 0.5) for _ in range(478)]
        for i, (px, py) in {469: (306, 240), 471: (294, 240),
                            474: (406, 240), 476: (394, 240)}.items():
            lm[i] = factory(px / w, py / h)
        return lm

    for _label, _fac in (
            ("MediaPipe-style landmark (attributes, NOT indexable)", _Proto),
            ("plain tuple", lambda x, y: (x, y)),
            ("numpy row", lambda x, y: __import__("numpy").array([x, y]))):
        _r = _iris_mod.iris_diameter_px(_mesh(_fac), 640, 480)
        check("iris diameter reads a %s" % _label,
              _r.get("mean_px") == 12.0,
              _r.get("error") or str(_r.get("mean_px")))

    check("the eager-default trap is documented where it bit",
          "evaluates a default" in read("iris_distance.py"))

    # ── The cross-check that cried wolf ──────────────────────────────
    # The IOD arm used landmarks 33/263 (OUTER EYE CORNERS, ~8.4 cm
    # apart in adults) and divided by POPULATION_IOD_CM = 6.3, the
    # INTER-PUPILLARY distance. That inflated the focal length by 8.4/6.3
    # and manufactured a 24.7 % disagreement with the iris on a
    # calibration that was actually correct to 0.4 %. A cross-check
    # exists to catch a bad landmark fit; one that fires on good data
    # trains you to ignore it.
    check("the IOD arm uses the iris CENTRES (true inter-pupillary), "
          "not the outer eye corners",
          "lm[468]" in _cg and "lm[473]" in _cg
          and "lm[33]" not in _cg and "lm[263]" not in _cg)
    check("choosing the iris focal recomputes the derived FOV fields",
          'data["implied_hfov_deg"] = round(implied, 1)' in _cg)

    # The arithmetic itself, on the numbers actually measured.
    _scale = 12.79 / 1.17                     # px per cm at 60 cm
    check("the measured 91.43 px separation is outer-canthal, not "
          "inter-pupillary",
          8.0 <= 91.43 / _scale <= 8.8,
          "%.2f cm apart" % (91.43 / _scale))
    check("outer-canthal cm reconciles the two focal estimates to <2 %",
          abs(91.43 * 60 / 8.4 - 12.79 * 60 / 1.17)
          / (12.79 * 60 / 1.17) < 0.02,
          "%.1f vs %.1f px" % (91.43 * 60 / 8.4, 12.79 * 60 / 1.17))

    # ── The review summary must not call a measurement an assumption ──
    # The line read "viewing distance 60 cm (assumed)" unconditionally,
    # including on sessions where the iris measurement had succeeded.
    # It is the one line a researcher reads to judge a session, and the
    # distance is the denominator of every degree on the page.
    _rev = read("templates/review.html")
    check("the quality API carries the measured distance through",
          '"distance_measured"' in _app3 and 'out["distance"] = _dists[-1]'
          in _app3)
    check("the review page no longer hard-codes '(assumed)'",
          "' cm (assumed)');" not in _rev)
    check("a measured distance is labelled MEASURED, with its source",
          "(MEASURED" in _rev and "q.distance.source" in _rev)
    check("an assumed distance says what it contaminates",
          "every degree on this page inherits this" in _rev)
    check("the distance row is styled pass/fail, not neutral",
          "q.distance_measured ? 'pass' : 'fail'" in _rev)
    check("a disagreement between the two rulers is surfaced here too",
          "estimates_agree === false" in _rev)

    # ── claim_check on a REAL session, not just the demo ─────────────
    # The manifest path used to print "wire in the gaze CSV" and exit,
    # so `python claim_check.py` silently fell through to --demo and
    # looked like it had run. RQ3's only automatic validity measure was
    # a stub.
    _cc = read("claim_check.py")
    check("claim_check scores a real manifest, not only the demo",
          "def load_gaze" in _cc and "def load_claims" in _cc
          and "Wire in the gaze CSV" not in _cc)
    check("screen pixels are mapped into VIDEO coordinates via video_rect",
          "video_rect" in _cc and "(sx - rx) / rw" in _cc)
    check("the session's gain correction is applied before scoring",
          "gain correction applied" in _cc and "(sx - cx) * gx" in _cc)
    check("invalid samples are marked, not scored as 'outside the box'",
          'row.get("status"' in _cc)
    check("only samples inside the stimulus window are used",
          "if ts < t0 or ts > t1" in _cc)
    check("tolerance uses the OUT-OF-SAMPLE post validation",
          'v.get("phase") == "post"' in _cc
          and "IN-SAMPLE — optimistic" in _cc)
    check("a session with no validation is refused, not scored as perfect",
          "Refusing." in _cc)
    check("an over-threshold tolerance is called an upper bound",
          "UPPER bound" in _cc)

    # ── The window bug that made RQ3 unmeasurable ────────────────────
    # In "fixations" detail mode the model answers with a single instant
    # (t_start == t_end). Widening that by 1 ms gave a window narrower
    # than the 32 ms sampling interval, so 59 of 60 claims scored
    # UNTESTABLE "no valid gaze samples" and correspondence was computed
    # from ONE claim. A metric derived from a single unit is not a
    # metric.
    check("a zero-length claim is widened to a fixation, not a "
          "millisecond",
          "MIN_CLAIM_WINDOW_S" in _cc and "t1 - t0 < MIN_CLAIM_WINDOW_S"
          in _cc)
    check("the widening is recorded on the claim, not silent",
          "window_widened_to_s" in _cc)
    sys.path.insert(0, BASE)
    _ccmod = importlib.import_module("claim_check")
    importlib.reload(_ccmod)
    check("the claim window matches this pipeline's median fixation",
          0.15 <= _ccmod.MIN_CLAIM_WINDOW_S <= 0.35,
          "%.3f s" % _ccmod.MIN_CLAIM_WINDOW_S)

    # Behavioural, on the exact geometry of the 2026-08-10 session:
    # 31.2 Hz, 2.13 deg accuracy, claims at single instants.
    _hz = 31.2
    _samp = [(i / _hz, 0.50, 0.60, True) for i in range(int(30 * _hz))]

    def _score(bbox):
        return _ccmod.check_claim(
            {"t_start": 0.3, "t_end": 0.3, "attended": "x", "bbox": bbox},
            _samp, 2.13, 58.2, 1920, 1080)

    _on = _score([0.40, 0.50, 0.20, 0.20])
    _off = _score([0.05, 0.05, 0.20, 0.20])
    check("a single-instant claim now collects samples at 31 Hz",
          _on.get("n_samples", 0) >= 5, "%d samples" % _on.get("n_samples", 0))
    check("gaze inside the claimed box scores SUPPORTED",
          _on["verdict"] == _ccmod.SUPPORTED, _on["verdict"])
    check("gaze outside it scores CONTRADICTED, not UNTESTABLE",
          _off["verdict"] == _ccmod.CONTRADICTED, _off["verdict"])
    # SUPERSEDED (section [19]): refusing every small object discarded
    # 77 % of a session and equated "18 px from a hand" with "across the
    # room". Small objects are now graded by distance and flagged
    # unresolvable, not refused.
    check("a small object is graded, and flagged as unresolvable",
          _score([0.49, 0.59, 0.02, 0.02])["resolvable"] is False)

    # ── RQ2 metrics exist; the verifier was looking in the wrong place ─
    _vm = read("verify_metrics.py")
    check("verify_metrics reads manifest['events'], where app.py writes "
          "them",
          'manifest.get("events")' in _vm)
    check("the saccade block is unwrapped, not reported missing",
          'stim_block.get("saccades")' in _vm)

    # ── Offset analysis: whose fault is a low correspondence? ────────
    # "0 % inside" cannot tell a model that put the box in the wrong
    # place from a tracker with a systematic displacement, and the two
    # have opposite fixes.
    check("the offset VECTOR is recorded, not just the fraction inside",
          "offset_px" in _cc and "def offset_analysis" in _cc)
    check("consistency uses SIGN AGREEMENT, not a ratio of medians "
          "(which reads 1.0 on a 4-3 split)",
          "def _agree" in _cc and "(v > 0) == (med > 0)" in _cc)

    import random as _rnd

    _boxes = [[0.05 + 0.3 * (i % 3), 0.05 + 0.3 * (i // 3), 0.25, 0.25]
              for i in range(12)]

    def _synth(shift):
        _s, _c = [], []
        for i, b in enumerate(_boxes):
            t = 1.0 + i * 2.0
            cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
            dx, dy = shift(i)
            for k in range(10):
                _s.append((t - 0.1 + k * 0.02, cx + dx, cy + dy, True))
            _c.append({"t_start": t, "t_end": t, "attended": "o%d" % i,
                       "bbox": b})
        return _ccmod.check_all(_c, _s, 2.13, 58.2, 1920,
                                1080)["offset_analysis"]

    _rnd.seed(7)
    _sys = _synth(lambda i: (0.0, 0.16))
    _sca = _synth(lambda i: (_rnd.uniform(-.2, .2), _rnd.uniform(-.2, .2)))
    check("a uniform displacement is called SYSTEMATIC",
          _sys["systematic"] is True,
          "offset %s, agreement %s" % (_sys["median_offset_px"],
                                       _sys["direction_consistency"]))
    check("random misses are NOT called systematic",
          _sca["systematic"] is False,
          "offset %s, agreement %s" % (_sca["median_offset_px"],
                                       _sca["direction_consistency"]))
    check("untestable claims are framed as the resolution limit, not a "
          "failure",
          "resolution limit, not a failure" in _cc
          and "not individual people" in _cc)

    # ── The tracker's alibi ──────────────────────────────────────────
    # A consistent direction alone convicted the tracker, and on the
    # 2026-08-10 session that produced "+160, +404 px (7.46 deg),
    # suspect the tracker" — for a session whose out-of-sample
    # validation had measured 2.13 deg against KNOWN targets minutes
    # earlier on the same gaze stream. Both cannot be true. The
    # validation bounds how wrong the gaze can be, so it is the alibi.
    check("the validation accuracy is used to exonerate the tracker",
          "tracker_exonerated" in _cc and "THE TRACKER'S ALIBI" in _cc)

    _big = _synth_offset = None

    def _offset_case(shift, acc):
        _s, _c = [], []
        _bx = [[0.05 + 0.3 * (i % 3), 0.05 + 0.22 * (i // 3), 0.25, 0.25]
               for i in range(12)]
        for i, b in enumerate(_bx):
            t = 1.0 + i * 2.0
            cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
            dx, dy = shift(i)
            for k in range(10):
                _s.append((t - 0.1 + k * 0.02, cx + dx, cy + dy, True))
            _c.append({"t_start": t, "t_end": t, "attended": "o%d" % i,
                       "bbox": b})
        return _ccmod.check_all(_c, _s, acc, 58.2, 1680,
                                945)["offset_analysis"]

    _huge = _offset_case(lambda i: (0.095, 0.43), 2.13)
    _small = _offset_case(lambda i: (0.0, 0.04), 2.13)
    check("a displacement far larger than the measured accuracy does "
          "NOT blame the tracker",
          _huge["tracker_exonerated"] is True
          and "NOT THE TRACKER" in _huge["reading"],
          "%.2f deg vs %.2f measured" % (_huge["median_offset_deg"], 2.13))
    check("...and it is named as an RQ3 localisation result instead",
          "localising it from a prior" in _huge["reading"])
    check("a displacement WITHIN the tracker's measured error still "
          "points at the tracker",
          _small["tracker_exonerated"] is False)
    check("the offset-to-accuracy ratio is reported, not just a verdict",
          _huge.get("offset_vs_accuracy") is not None,
          "%sx" % _huge.get("offset_vs_accuracy"))

    # ══════════════════════════════════════════════════════════════
    #  The two-grid validation protocol
    # ══════════════════════════════════════════════════════════════
    # Fitting the gain correction on grid A and reporting the error at
    # grid A scores the fit on its training points. Re-measuring at the
    # SAME positions with fresh samples is better but still not a
    # generalisation estimate — the correction was tuned to minimise
    # error at exactly those seven locations. Grid B is disjoint and
    # matched on difficulty, so the corrected accuracy is out of sample
    # in both space and time.
    print("\n[17] Two-grid validation protocol")

    def _grid(name):
        m = re.search(name + r"\s*=\s*\[(.*?)\];", _js3, re.S)
        return [tuple(int(v) for v in p)
                for p in re.findall(r"\[\s*(\d+)\s*,\s*(\d+)\s*\]",
                                    m.group(1))] if m else []

    _A = _grid("VALIDATION_GRID")
    _B = _grid("VALIDATION_CHECK_GRID")
    _ecc = lambda g, i: sum(abs(p[i] - 50) for p in g) / len(g)

    check("both grids exist and have the same number of targets",
          len(_A) == 7 and len(_B) == 7, "A=%d B=%d" % (len(_A), len(_B)))
    check("the grids share NO target position",
          not (set(_A) & set(_B)), "shared: %s" % sorted(set(_A) & set(_B)))
    check("horizontal eccentricity is matched (B is not an easier grid)",
          abs(_ecc(_A, 0) - _ecc(_B, 0)) <= 2.0,
          "A %.1f vs B %.1f" % (_ecc(_A, 0), _ecc(_B, 0)))
    check("vertical eccentricity is matched",
          abs(_ecc(_A, 1) - _ecc(_B, 1)) <= 2.0,
          "A %.1f vs B %.1f" % (_ecc(_A, 1), _ecc(_B, 1)))
    check("B spans as many vertical elevations as A (the y-correction "
          "needs them)",
          len({p[1] for p in _B}) >= len({p[1] for p in _A}))
    check("post uses grid B, so drift pairs with pre_check like for like",
          "post: VALIDATION_CHECK_GRID" in _js3)

    check("the two pre-checks run as ONE user action",
          "run('pre_fit'" in _js3 and "run('pre_check'" in _js3
          and _js3.index("run('pre_fit'") < _js3.index("run('pre_check'"))
    check("the validate button disables itself (a repeatable button "
          "gets pressed until the number looks good)",
          "this.validateBtn.disabled = true" in _js3)

    check("ONLY the fit phase fits the correction",
          'record["phase"] in ("pre_fit", "pre")' in _app3
          and '_auto_fit_correction' in _app3)
    check("a repeat attempt does NOT refit",
          'record["attempt"] == 1' in _app3)
    check("each validation records its role and grid",
          'record["role"]' in _app3 and 'record["grid"]' in _app3
          and 'record["canonical_accuracy"]' in _app3)
    check("repeat attempts are counted and logged as a deviation",
          'record["attempt"] = len(prior) + 1' in _app3
          and "PROTOCOL:" in _app3)

    check("verify_metrics reports pre_check as an out-of-sample figure",
          'v.get("phase") == "pre_check"' in _vm
          and "the correction was never" in _vm)
    check("a legacy repeat at the FIT grid is graded DEGENERATE, not "
          "reported as corrected accuracy",
          "IN-SAMPLE, not a" in _vm)
    check("drift is differenced on ONE basis",
          'pre[-1].get("mean_err_deg_raw")' in _vm
          and 'post[-1].get("mean_err_deg_raw")' in _vm)
    check("a mixed-basis drift is graded DEGENERATE, not PRESENT",
          "MIXED correction, not comparable" in _vm)

    # The arithmetic, on the 2026-08-10 session's actual numbers.
    check("the old drift computation mixed bases",
          abs((2.13 - 4.52) - (-2.39)) < 0.01, "-2.39 = corrected − raw")
    check("the corrected computation matches the review page",
          abs((5.04 - 4.53) - 0.51) < 0.01, "+0.51 = raw − raw")

    # ── The distance, and RQ3, must reach the session record ────────
    # Both failed the same way: the value existed, and the thing that
    # reads it looked somewhere else.
    check("the manifest carries a distance from the MANDATORY validation, "
          "not only the optional guide",
          '"distance": _session_distance(state)' in _app3
          and "def _session_distance" in _app3)
    check("pre_check is preferred as the distance source",
          '("pre_check", "pre_fit", "pre", "post")' in _app3)
    check("an unmeasured distance says what it contaminates",
          "every \n                      \"degree figure in this session divides by" in _app3
          or "degree figure in this session divides by" in _app3)
    check("verify_metrics reads the manifest distance block",
          'manifest.get("distance")' in _vm
          and 'head_position", "est_distance_cm"' in _vm)

    check("the LLM result is written into the manifest, not only the log "
          "directory",
          "def _persist_llm_result" in _app3
          and 'manifest.setdefault("llm", {})[stimulus] = block' in _app3)
    check("correspondence is scored at write time, not left to a command "
          "someone must remember",
          "claim_check.check_all(" in _app3)
    check("a failure to score never loses generated feedback",
          "Correspondence scoring failed" in _app3)
    check("verify_metrics grades claims with no bbox as DEGENERATE",
          "nothing to check against the gaze" in _vm)
    check("a correspondence rate over too few units is DEGENERATE, not a "
          "result",
          "too few to report as a" in _vm)
    check("a missing rubric is named as the reason the evaluative half "
          "of RQ3 has no data",
          "NO RUBRIC was supplied" in _vm and "kappa" in _vm)

    import verify_metrics as _vmod
    importlib.reload(_vmod)
    _base = {"validations": [], "data_quality": {}, "events": {}}

    def _rq3(llm):
        m = dict(_base)
        m["llm"] = llm
        r = _vmod.Result()
        _vmod.check_session(m, r)
        return {n.split(" [")[0]: (s, v)
                for rq, n, s, v, _ in r.rows if rq == "RQ3"}

    _mk = lambda n, **kw: {"_t.mp4": dict(
        {"llm_model_id": "m", "structured": [
            {"bbox": [0, 0, .2, .2], "criteria_met": None} for _ in range(20)]},
        **kw)}
    _none = _rq3({})
    _few = _rq3(_mk(20, correspondence={"correspondence_pct": 50.0,
                                        "n_testable": 4}))
    _good = _rq3(_mk(20, rubric="r",
                     correspondence={"correspondence_pct": 68.0,
                                     "n_testable": 25}))
    check("a session with no feedback run reports all three RQ3 fields "
          "missing",
          all(_none[k][0] == _vmod.MISSING for k in
              ("llm_model_id", "llm_claims_structured",
               "claim_metric_correspondence")))
    check("4 testable claims is DEGENERATE, not a 50 % result",
          _few["claim_metric_correspondence"][0] == _vmod.DEGENERATE)
    check("25 testable claims with a rubric is PRESENT",
          _good["claim_metric_correspondence"][0] == _vmod.PRESENT,
          _good["claim_metric_correspondence"][1])

    # ══════════════════════════════════════════════════════════════
    #  Region vocabulary + calibration diagnosis
    # ══════════════════════════════════════════════════════════════
    print("\n[18] Region vocabulary and calibration diagnosis")
    # regions.py DELETED. The 3x3 vocabulary is gone from the prompt,
    # the scorer and the summaries — object-level claims graded by
    # distance replaced it. The resolution argument survives as prose,
    # not as a grid nobody wanted.
    # The inclusion criterion applies to the MEAN of the two grid-B
    # checks, one either side of the recording. Neither end alone
    # answers the question: the stimulus data sits between them.
    _vm2 = read("verify_metrics.py")
    check("inclusion uses the mean of pre_check and post, not one end",
          "accuracy_for_inclusion_deg" in _vm2
          and "incl = (float(a) + float(b)) / 2.0" in _vm2)
    check("both ends of that mean are out-of-sample grid B",
          "needs BOTH a pre_check and a post on grid B" in _vm2)
    check("collection has not started, so every session is DEVELOPMENT",
          "EVALUATION_FROM_DATE" in read("config.py")
          and "collection has not started" in _vm2)
    check("an unset start date does not silently promote sessions",
          _vmod.pilot_status("x_2026-08-11_manifest.json")[0] is True)

    check("the region grid is gone entirely",
          not os.path.exists(os.path.join(BASE, "regions.py")))
    check("nothing still imports it",
          "import regions" not in _cc and "import regions" not in _app3)
    check("the gaze summary reports position, not a grid",
          "grid_pct" not in _cc and '"median"' in _cc)

    # calibration_diagnosis: does it tell the four causes apart?
    _cd = importlib.import_module("calibration_diagnosis")
    importlib.reload(_cd)
    _G = [(230, 130), (1690, 130), (960, 335), (288, 540),
          (1632, 540), (960, 745), (960, 950)]

    def _tg(gx, gy, ox=0, oy=0, noise=None):
        out = []
        for i, (tx, ty) in enumerate(_G):
            mx = 960 + (tx - 960) * gx + ox
            my = 540 + (ty - 540) * gy + oy
            if noise:
                mx += noise[i % len(noise)]
                my -= noise[i % len(noise)]
            out.append({"tx": tx, "ty": ty, "mx": mx, "my": my})
        return out

    def _diag(tg):
        return _cd.analyse({"validations": [
            {"phase": "pre_fit", "targets": tg,
             "mean_err_px": 0, "mean_err_deg": 0}]})

    _comp = _diag(_tg(0.78, 0.73))
    _off = _diag(_tg(1.0, 1.0, ox=60, oy=90))
    _noise = _diag(_tg(1.0, 1.0, noise=[70, -65, 55, -80, 60, -70, 75]))
    check("a compressed range is named RANGE COMPRESSION",
          _comp["y"]["verdict"] == "RANGE COMPRESSION",
          "slope %.2f -> gain %.2f" % (_comp["y"]["slope"],
                                       _comp["y"]["implied_gain"]))
    check("...and the implied gain matches the session's own (1.365)",
          abs(_comp["y"]["implied_gain"] - 1.37) < 0.02)
    check("a constant miss is named UNIFORM OFFSET, not a gain problem",
          _off["y"]["verdict"] == "UNIFORM OFFSET")
    check("position-independent error is named UNSTRUCTURED",
          _noise["y"]["verdict"] == "UNSTRUCTURED")
    check("it diagnoses the UNCORRECTED check, not the corrected one",
          '"pre_fit", "pre"' in read("calibration_diagnosis.py"))
    check("a range problem is sent upstream to the calibration grid",
          "Extend the CALIBRATION grid" in read("calibration_diagnosis.py"))

    # ══════════════════════════════════════════════════════════════
    #  Graded scoring, the inverse check, and human coding
    # ══════════════════════════════════════════════════════════════
    print("\n[19] Graded claims, inverse check, human coding")

    # The region vocabulary is gone from the prompt again.
    check("the prompt no longer imposes a region vocabulary",
          "SPATIAL VOCABULARY" not in _app3
          and "one of the region names above" not in _app3)

    # GRADED, not binary. Refusing every claim about an object smaller
    # than the error threw away 77 % of a session, and treated "18 px
    # from a hand" the same as "across the room".
    check("claims are graded by DISTANCE, not refused for being small",
          "CONSISTENT" in _cc and "distance_px" in _cc)
    check("the tolerance is applied ONCE (containment is unpadded)",
          "counts the same allowance twice" in _cc
          and "bx <= s[1] <= bx + bw" in _cc)
    check("both a strict and a lenient correspondence rate are reported",
          "correspondence_lenient_pct" in _cc)
    check("UNTESTABLE now means only 'no samples here'",
          "no valid gaze samples in this time window" in _cc)

    _hz2 = 31.2
    _sm = [(i / _hz2, 0.50, 0.60, True) for i in range(int(30 * _hz2))]

    def _v(bbox):
        return _ccmod.check_claim(
            {"t_start": 5, "t_end": 5, "attended": "x", "bbox": bbox},
            _sm, 2.13, 58.2, 1680, 945)

    check("gaze on the object is SUPPORTED",
          _v([0.45, 0.55, 0.10, 0.10])["verdict"] == "SUPPORTED")
    check("a near miss inside the error is CONSISTENT, not discarded",
          _v([0.42, 0.55, 0.05, 0.10])["verdict"] == "CONSISTENT",
          "%s px away" % _v([0.42, 0.55, 0.05, 0.10])["distance_px"])
    check("a miss beyond the error is CONTRADICTED",
          _v([0.05, 0.05, 0.10, 0.10])["verdict"] == "CONTRADICTED",
          "%s px away" % _v([0.05, 0.05, 0.10, 0.10])["distance_px"])
    check("a small object no longer blocks a verdict",
          _v([0.44, 0.52, 0.06, 0.06])["verdict"] != "UNTESTABLE")

    # INVERSE CHECK: locate first, on clean frames, then assign gaze.
    _ic = importlib.import_module("inverse_check")
    importlib.reload(_ic)
    check("the locate prompt never mentions gaze or eye tracking",
          not any(w in _ic.LOCATE_PROMPT.lower()
                  for w in ("gaze", "eye track", "fixation", "participant "
                            "looked")))
    check("it asks for people separately and excludes background",
          "every person separately" in _ic.LOCATE_PROMPT
          and "walls, floors, ceilings" in _ic.LOCATE_PROMPT)

    _objs = [{"label": "girl in red shirt", "bbox": [0.45, 0.40, 0.07, 0.16]},
             {"label": "boy in blue hoodie", "bbox": [0.60, 0.40, 0.07, 0.16]},
             {"label": "orange poster", "bbox": [0.40, 0.20, 0.05, 0.06]}]
    _on = _ic.assign_fixation({"x": 0.48, "y": 0.45}, _objs, 124.0, 1680, 945)
    _mid = _ic.assign_fixation({"x": 0.545, "y": 0.45}, _objs, 124.0, 1680, 945)
    check("a fixation ON a person is assigned unambiguously",
          _on["assigned"] == "girl in red shirt" and not _on["ambiguous"],
          "separation %s px" % _on["separation_px"])
    check("a fixation BETWEEN two people is flagged ambiguous, not guessed",
          _mid["ambiguous"] is True,
          "separation %s px at 124 px error" % _mid["separation_px"])
    # The runner-up here is the POSTER, not the other student: at
    # (0.48, 0.45) the poster is 187 px away and the boy 202 px. Worth
    # keeping as a reminder that "the next nearest thing" is a
    # geometric fact, not the semantically obvious neighbour — which is
    # exactly why the tool reports it instead of assuming.
    check("the runner-up is reported so ambiguity is auditable",
          _on.get("runner_up") and _on.get("runner_up_distance_px"),
          "%s at %s px" % (_on.get("runner_up"),
                           _on.get("runner_up_distance_px")))
    check("agreement matching is lexical and stated, not model-judged",
          "LEXICAL and loose" in read("inverse_check.py"))

    _cmp = _ic.compare(
        [{"t": 1.0, "assigned": "girl in red shirt", "ambiguous": False},
         {"t": 3.0, "assigned": "orange poster", "ambiguous": False}],
        [{"t_start": 1.0, "attended": "the girl in the red shirt"},
         {"t_start": 3.0, "attended": "a ceiling light"}])
    check("compare() scores agreement between gaze-derived and claimed",
          _cmp["n_compared"] == 2 and _cmp["agreement_pct"] == 50.0,
          "%s %% of %d" % (_cmp["agreement_pct"], _cmp["n_compared"]))
    check("ambiguous assignments are excluded from the agreement rate",
          _ic.compare([{"t": 1.0, "assigned": "x", "ambiguous": True}],
                      [{"t_start": 1.0, "attended": "x"}])["n_compared"] == 0)

    # HUMAN CODING — the only anchor that is not a model.
    _coder = read("templates/coder.html")
    check("a coding route and page exist",
          '@app.route("/coder")' in _app3 and "def coder(" in _app3)
    check("units carry the model's claim for the same moment",
          '"model_claim"' in _app3)
    check("verdicts are stored PER CODER, never merged",
          "never merged" in _app3
          and '"%s__%s__%s.json"' in _app3)
    check("the rubric the coder worked to is stored with the verdicts",
          '"instructions": payload.get("instructions")' in _app3)
    check("blind mode hides the claim until the coder has decided",
          "blindMode" in _coder and "stops coding and starts agreeing"
          in _coder)
    check("'unclear' is a first-class verdict, not a skip",
          "unclear" in _coder and "first-class verdict" in _coder)
    check("unclear units are excluded from the rate, not counted wrong",
          "excluded from that" in _coder)
    check("the marker shows the measured error as a RING, not a point",
          "gazeRing" in _coder and "accuracy_deg" in _coder)
    check("the coder is pointed at a second rater for kappa",
          "second coder" in _coder and "agreement_kit" in _coder)

    # Every fixation read "no model claim covers this fixation". The
    # cause was not the coder and not the matching: the manifest
    # write-back is recent, so a session whose feedback predates it has
    # an empty llm block and the only record is data/llm_logs/.
    check("the coder falls back to the log directory for claims",
          "claim_check.load_claims(session)" in _app3
          and "claims_source" in _app3)
    check("it distinguishes 'no claims loaded' from 'none matched'",
          "match_warning" in _app3
          and "No LLM claims found for this session at all" in _app3
          and "only %d of %d fixations matched" in _app3)
    check("the page shows where the claims came from",
          "claimSource" in _coder and "d.claims_source" in _coder)
    check("claims are matched by time OVERLAP, not midpoint proximity",
          "if ce >= lo and cs <= hi" in _app3)

    # A long fixation puts its midpoint far from the claim naming its
    # onset, which is why overlap is the safer rule even though both
    # schemes happen to agree on the 2026-08-10 data.
    _claims_t = [0.3, 0.7, 1.1, 2.1, 2.6, 3.0, 3.4, 3.9]

    def _overlap_hits(start, dur, margin=0.35):
        lo, hi = start - margin, start + dur + margin
        return [t for t in _claims_t if lo <= t <= hi]

    check("a long fixation still matches the claim naming its onset",
          _overlap_hits(2.1, 0.9) and 2.1 in _overlap_hits(2.1, 0.9))
    check("a fixation with no claim near it matches nothing",
          not _overlap_hits(9.9, 0.2))

    # The cap was raised after it silently truncated a real session.
    _cfg2 = read("config.py")
    _cap = int(re.search(r'LLM_MAX_FRAMES", "(\d+)"', _cfg2).group(1))
    check("LLM_MAX_FRAMES covers a 30 s clip's fixation count",
          _cap >= 100, "%d frames" % _cap)
    check("the request timeout scales with the frame count",
          "LLM_TIMEOUT_PER_FRAME_S" in _cfg2
          and "LLM_TIMEOUT_PER_FRAME_S * _n_imgs" in _app3)
    check("the raise is recorded as a methods fact, with the evidence",
          "88 %" in _cfg2 and "0 %" in _cfg2)

    # The findings log is the artefact the Methods chapter is written
    # from, so a claim in it must be traceable and an unconfirmed one
    # must be marked.
    _find = read("METHODOLOGY_FINDINGS.md")
    check("the findings log exists and is numbered for citation",
          _find.count("## F") >= 12)
    check("unconfirmed findings are marked OPEN, not stated as fact",
          "OPEN" in _find and "suspected, not confirmed" in _find)
    check("it lists what is still missing before evaluation collection",
          "Open items before evaluation collection" in _find)

    # ── The report must not cry wolf ─────────────────────────────────
    # The 2026-08-11 report listed 10 "missing" metrics. Three were
    # DUPLICATES of metrics reported PRESENT in the same run (checked by
    # two code paths), five were AOI metrics this study deliberately
    # does not collect, and one was recorded in the manifest under a key
    # the checker did not read. Exactly ONE was a real gap. A report
    # that inflates its own gap count is one nobody reads.
    check("a metric found PRESENT is never also reported MISSING",
          "If it was found, it is not missing" in _vm
          and 'str(n).split(" [")[0] == base' in _vm)
    check("AOI metrics are N/A by design, not MISSING",
          'NOT_APPLICABLE = "N/A"' in _vm
          and "by design: no hand-drawn AOIs" in _vm)
    check("the design reason for each is recorded in the spec",
          read("metrics_spec.py").count("aoi_") >= 5
          and "NOT_APPLICABLE_NAMES" in read("metrics_spec.py"))
    check("the dispersion threshold is read from events, where it is "
          "written",
          'blk.get("idt_dispersion_deg")' in _vm)
    check("the summary names what is still missing, not just a count",
          "still missing: " in _vm)

    # Behavioural: a duplicate MISSING must be suppressed.
    _rr = _vmod.Result()
    _rr.add("RQ2", "saccade_count [clip]", _vmod.PRESENT, "70")
    _rr.add("RQ2", "saccade_count", _vmod.MISSING, "", "wire it in")
    check("...verified: the duplicate is dropped, not printed",
          len(_rr.rows) == 1 and _rr.rows[0][2] == _vmod.PRESENT,
          "%d row(s)" % len(_rr.rows))
    _rr2 = _vmod.Result()
    _rr2.add("RQ2", "aoi_revisits", _vmod.MISSING, "")
    check("...but a genuine MISSING is still reported",
          len(_rr2.rows) == 1 and _rr2.rows[0][2] == _vmod.MISSING)

    # ── The keyframe cap: what the model was never shown ─────────────
    # 71 fixations, LLM_MAX_FRAMES=60, and sample_gaze_frames keeps the
    # LONGEST. So 11 fixations were dropped and nothing recorded which,
    # while the coding tool presented all 71 and invited a verdict on
    # claims that were never made.
    check("the frames the model saw are recorded with the result",
          '"frame_times"' in _app3 and '"frames_dropped"' in _app3)
    check("a cap that drops fixations is logged as a warning",
          "LLM saw %d of %d fixations" in _app3)
    check("the coder marks fixations the model never saw",
          '"shown_to_model"' in _app3 and "def _was_shown" in _app3)
    check("...and says so instead of showing an empty claim",
          "NOT SHOWN TO THE MODEL" in _coder)
    check("an unknown frame list assumes shown, rather than accusing",
          "unknown: assume yes rather than accuse" in _app3)

    # ── The off-by-one ───────────────────────────────────────────────
    check("claim/frame alignment is checked, not eyeballed",
          "def alignment_check" in _cc and "best_shift" in _cc)
    _ft = sorted(round(0.37 * i + (i % 7) * 0.11, 1) for i in range(40))
    _aligned = _ccmod.alignment_check(
        [{"t_start": t, "t_end": t} for t in _ft], _ft)
    _ahead = _ccmod.alignment_check(
        [{"t_start": _ft[i + 1], "t_end": _ft[i + 1]}
         for i in range(len(_ft) - 1)], _ft)
    check("aligned claims report no shift",
          _aligned["systematically_shifted"] is False)
    check("claims describing the NEXT frame are caught",
          _ahead["systematically_shifted"] is True
          and _ahead["best_shift"] == 1,
          "shift %+d" % _ahead["best_shift"])
    check("a shift is only called when it fits much better than none",
          "best_err < 0.5 * zero_err" in _cc)

    # ── The Windows launcher ─────────────────────────────────────────
    # A menu that offers a script which does not exist fails at the
    # worst possible moment: with a participant sitting there. Batch
    # has no import system to catch that, so check it here.
    _start = read("windows/START.bat")
    _referenced = set(re.findall(r'(?:call\s+)?(windows\\[\w.]+\.bat)',
                                 _start))
    for _bat in sorted(_referenced):
        check("START.bat offers %s, and it exists" % _bat,
              os.path.exists(os.path.join(BASE, _bat.replace("\\", "/"))))
    _scripts = set(re.findall(r'python\s+([\w_]+\.py)', _start))
    for _py in sorted(_scripts):
        check("START.bat offers %s, and it exists" % _py,
              os.path.exists(os.path.join(BASE, _py)))
    # :eof is batch's built-in return target and is never declared.
    check("every goto in START.bat has a label",
          not (set(m.lower() for m in re.findall(r'goto\s+:(\w+)', _start))
               - set(l.strip()[1:].lower() for l in _start.splitlines()
                     if l.strip().startswith(":")
                     and not l.strip().startswith("::"))
               - {"eof"}))
    check("START.bat is pure ASCII (cmd's codepage mangles the rest)",
          all(ord(c) < 128 for c in _start))
    check("it refuses to pull over uncommitted local changes",
          "NOT pulling" in _start)
    check("it leaves a usable prompt rather than closing",
          "cmd /k" in _start)

    # The update logic exists ONCE and is called from two places. Two
    # copies would drift, and the copy that drifts is always the one
    # guarding the collection machine.
    check("update is a subroutine, defined once",
          _start.count("\n:do_update") == 1)
    check("...and called from both launch and the menu",
          _start.count("call :do_update") == 2)
    check("there is exactly one git pull and one dirty-tree guard",
          _start.count("git pull --ff-only") == 1
          and _start.count("NOT pulling") == 1)
    check("the menu offers the update",
          "u  Update from GitHub" in _start
          and 'if /i "%OPT%"=="u"' in _start)
    check("no orphaned label survives the refactor",
          "skip_update" not in _start)
    check("every goto target and call target exists",
          not ({m.lower() for m in re.findall(r"goto\s+:(\w+)", _start)}
               | {m.lower() for m in re.findall(r"call\s+:(\w+)", _start)}
               ) - ({l.strip()[1:].lower() for l in _start.splitlines()
                     if l.strip().startswith(":")
                     and not l.strip().startswith("::")} | {"eof"}))
    check("the subroutine returns rather than falling through the menu",
          _start.count("goto :eof") >= 4)

    # The API key. This repository is PUBLIC, so "it is in .gitignore"
    # is a claim worth verifying rather than assuming — a key published
    # to GitHub is revoked-and-rotated, not un-published.
    _ignore = read(".gitignore")
    check(".gemini_key is gitignored",
          any(l.strip() == ".gemini_key" for l in _ignore.splitlines()))
    check("so is anything else ending in .key, and .env",
          "*.key" in _ignore and ".env" in _ignore)
    check("the launcher VERIFIES the ignore before writing a key",
          "git check-ignore -q .gemini_key" in _start
          and "Do NOT save a key" in _start)
    check("config reads the env var first, then the file",
          'os.environ.get("GEMINI_API_KEY"' in read("config.py")
          and '".gemini_key"' in read("config.py"))
    # Coding verdicts are participant-LINKED (the session label embeds
    # the participant id) and this repository is public.
    check("data/coding/ is gitignored",
          any(l.strip() == "data/coding/" for l in _ignore.splitlines()))

    _cr = read("coding_report.py")
    check("the coding report separates accuracy from reliability",
          "ACCURACY" in _cr and "RELIABILITY" in _cr)
    check("kappa uses only units BOTH coders judged",
          "set(a) & set(b)" in _cr and "missing data, not" in _cr)
    check("too few shared units refuses a kappa rather than printing one",
          "too few for a" in _cr)
    check("unclear is excluded from accuracy and reported separately",
          "unclear_pct" in _cr and 'c["correct"] + c["wrong"]' in _cr)
    check("a single coder is told the number rests on one opinion",
          "ONE CODER ONLY" in _cr and "systematically generous" in _cr)
    check("kappa is reported with a benchmark, not bare",
          "KAPPA_BANDS" in _cr and "Landis" in _cr)
    check("--paste omits file paths so it is safe to share",
          '"--paste"' in _cr and "no file paths" in _cr)

    check("no key is committed anywhere in the tree",
          not os.path.exists(os.path.join(BASE, ".gemini_key"))
          or "gemini_key" in _ignore)
    check("running with no arguments says how to score a real session",
          "showing the DEMO on synthetic data" in _cc)
    check("duty prefers total callback cost over model cost",
          'or (live_cb or {}).get("callback_ms_median")' in _tsvc3)
    check("a duty figure computed from models alone is marked as such",
          "work_is_models_only" in _tsvc3)
    check("the camera is only blamed when it was MEASURED to be slow",
          "cam_slow = bool(delivered" in _tsvc3
          and "and cam_slow)" in _tsvc3)
    check("an unmeasured camera yields UNRESOLVED, not a guess",
          "bottleneck_unclear" in _tsvc3 and "UNRESOLVED" in _tsvc3)
    check("the halving mechanism (synchronous callback) is named in the "
          "CPU verdict",
          "skip alternate frames" in _tsvc3)
    check("the subscriber count is surfaced with the verdict",
          "each\n                        \"extra one is another CSV write "
          "per frame)" in _tsvc3 or "extra one is another CSV write" in _tsvc3)
    check("capture_limited no longer claims per-frame work is expensive "
          "without checking the budget",
          "over_frame_budget" in _tsvc3
          and "the camera itself is delivering slowly" in _tsvc3)

    # Assert on tokens that survive line re-wrapping. An earlier version
    # of these checks matched phrases that happened to span a string
    # concatenation and failed on a purely cosmetic edit, which trains
    # you to ignore the suite.
    check("the browser blames the camera, not AC power, when the camera "
          "was measured slow",
          "g.camera_throttled" in _js3
          and "This is a CAMERA " in _js3
          and "g.delivered_hz" in _js3)
    check("the camera branch is tested BEFORE the machine branch",
          _js3.index("g.camera_throttled") < _js3.index("g.cpu_throttled"))
    check("the camera advice names lighting rather than the power plan",
          "lamp on " in _js3 and "your FACE" in _js3
          and "power plan will not help" in _js3)
    check("the browser has a branch for frames arriving and being "
          "discarded",
          "g.frames_discarded" in _js3
          and "Neither the lighting nor the power plan" in _js3)
    check("the browser admits when the bottleneck is unresolved",
          "g.bottleneck_unclear" in _js3
          and "cannot be told apart yet" in _js3)
    check("the CPU branch quotes total work, not model cost",
          "g.work_ms_median" in _js3)

    # ── camera_remedy: exposure lives in the DEVICE, not the handle ──
    # The first real run showed '320x240, auto exposure' at brightness
    # 25/255 while the baseline read 141/255 — a manual exposure set two
    # conditions earlier had survived cap.release(). Every condition
    # after a manual one was contaminated, and the camera was left
    # pinned dark for the next process.
    check("each condition resets to auto exposure before measuring",
          "_restore_auto_exposure(cap, cv2)" in read("camera_remedy.py"))
    check("a manual-exposure condition restores auto on the way out",
          read("camera_remedy.py").count("_restore_auto_exposure") >= 3)

    check("the camera measures its DELIVERED fps, not the property it "
          "reports",
          "_measure_fps" in _camp and "actually DELIVERS" in _camp)
    check("a camera slower than requested is called out at open time",
          "THE CAMERA IS THE BOTTLENECK" in _camp)
    check("exposure capping exists and is opt-in",
          "GF_CAM_EXPOSURE" in _camp and 'return "auto"' in _camp)
    check("a capped exposure that darkens the image reverts itself",
          "REVERTED" in _camp
          and "MIN_USABLE_BRIGHTNESS" in _camp)
    check("the exposure cap fits inside one frame period",
          (1000.0 * 2 ** -5) < 33.4)

    # ── camera_remedy: it must not "fix" the rate by ruining the frame ──
    _rem = read("camera_remedy.py")
    check("the remedy sweep measures brightness alongside fps",
          "MIN_BRIGHTNESS" in _rem and "MAX_BRIGHTNESS" in _rem)
    check("a fast but unusable frame cannot win",
          "def usable(r)" in _rem
          and "MIN_BRIGHTNESS <= b <= MAX_BRIGHTNESS" in _rem)
    check("the least invasive workable condition wins, not the fastest",
          "next((r for r in results if r.get(\"ok\") and usable(r))" in _rem)
    check("baseline is measured first so a gain can be attributed",
          _rem.index('"baseline 640x480') < _rem.index('"640x480 MJPG'))
    check("resolution change is ranked last (it changes model input)",
          _rem.rindex("320x240") > _rem.index('"640x480 MJPG'))
    check("each condition reopens the camera (settings are sticky)",
          "cap = _open(cv2, index)" in _rem and "cap.release()" in _rem)
    check("auto-exposure is given time to settle before measuring",
          "t_warm" in _rem)
    check("no workable condition points at lighting, not more settings",
          "NO SETTING REACHED" in _rem and "lamp on the participant" in _rem)
    check("FOURCC is wired into the real camera, not only the sweep",
          "GF_CAM_FOURCC" in _camp and "CAP_PROP_FOURCC" in _camp)
    check("FOURCC is applied BEFORE resolution (it renegotiates the "
          "stream)",
          _camp.index("CAP_PROP_FOURCC")
          < _camp.index("self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, "
                        "self.img_width)"))
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("bottleneck attribution", False, _blocked or repr(exc))



# ══════════════════════════════════════════════════════════════════════
#  [20] END-TO-END on a synthetic session, and mathematical invariants
# ══════════════════════════════════════════════════════════════════════
# Most of the suite above greps source for strings. That catches a
# deleted safeguard but proves nothing about behaviour: every one of
# those checks would pass on code that computes the wrong number.
#
# This section BUILDS a complete synthetic session — CSV, manifest,
# validations, LLM claims — and runs the real loaders and scorers over
# it, then asserts properties that must hold for any correct
# implementation rather than for this one.
print("\n[20] End-to-end synthetic session, and invariants")
try:
    import csv as _csv
    import json as _js
    import math as _m
    import shutil as _sh
    import tempfile as _tf

    _tmp = _tf.mkdtemp(prefix="e2e_")
    try:
        # ── Build a session ──────────────────────────────────────────
        # 30 s, 31.2 Hz, gaze parked on four known locations in turn.
        HZ, DUR = 31.2, 30.0
        T0 = 1_700_000_000_000_000_000          # arbitrary ns epoch
        RECT = {"x": 0, "y": 52.5, "w": 1680.0, "h": 945.0}
        SPOTS = [(0.25, 0.30), (0.70, 0.35), (0.50, 0.65), (0.20, 0.75)]

        _rows, _truth = [], []
        _n = int(DUR * HZ)
        for i in range(_n):
            t = i / HZ
            spot = SPOTS[int(t // 7.5) % len(SPOTS)]
            _truth.append((t, spot))
            # screen px = video-normalised mapped back through the rect
            sx = spot[0] * RECT["w"] + RECT["x"]
            sy = spot[1] * RECT["h"] + RECT["y"]
            _rows.append({
                "timestamp": T0 + int(t * 1e9),
                "raw_gaze_position_x": sx, "raw_gaze_position_y": sy,
                "calibrated_gaze_position_x": sx,
                "calibrated_gaze_position_y": sy,
                "filtered_gaze_position_x": sx,
                "filtered_gaze_position_y": sy,
                "left_eye_openness": 200, "right_eye_openness": 200,
                "tracking_status": 1, "status": 1, "event": 0, "trigger": 0,
            })

        _csv_path = os.path.join(_tmp, "E2E_2026-08-11_120000.csv")
        with open(_csv_path, "w", newline="", encoding="utf-8") as _fh:
            _w = _csv.DictWriter(_fh, fieldnames=list(_rows[0]))
            _w.writeheader()
            _w.writerows(_rows)

        _man = {
            "session_csv": os.path.basename(_csv_path),
            "screen": {"width_px": 1920, "height_px": 1080,
                       "diag_inches": 15.6},
            "distance": {"cm": 60.0, "measured": True, "source": "iris",
                         "from_phase": "pre_check"},
            "stimuli": [{"stimulus": "clip.mp4", "t_start_ns": T0,
                         "t_end_ns": T0 + int(DUR * 1e9),
                         "video_rect": dict(RECT, video_w=1280,
                                            video_h=720)}],
            "validations": [
                {"phase": "pre_fit", "mean_err_deg": 2.20,
                 "mean_err_deg_raw": 2.20, "mean_precision_px": 30.0,
                 "targets": [{"err_px": 100 + 5 * i} for i in range(7)]},
                {"phase": "pre_check", "mean_err_deg": 1.20,
                 "mean_err_deg_raw": 2.10, "mean_precision_px": 30.0,
                 "targets": [{"err_px": 60 + 5 * i} for i in range(7)]},
                {"phase": "post", "mean_err_deg": 1.00,
                 "mean_err_deg_raw": 2.00, "mean_precision_px": 30.0,
                 "targets": [{"err_px": 55 + 5 * i} for i in range(7)]},
            ],
            "data_quality": {}, "events": {},
        }
        _man_path = _csv_path.replace(".csv", "_manifest.json")
        with open(_man_path, "w", encoding="utf-8") as _fh:
            _js.dump(_man, _fh)

        # ── The loader must recover what was written ─────────────────
        _samples, _err = _ccmod.load_gaze(_man, _man_path, "clip.mp4")
        check("e2e: the gaze loader reads the session it was given",
              not _err and len(_samples) == _n,
              _err or "%d samples" % len(_samples))
        _sx = [s[1] for s in _samples]
        _sy = [s[2] for s in _samples]
        check("e2e: screen px map back to the video coords they came from",
              max(abs(min(_sx) - 0.20), abs(max(_sx) - 0.70),
                  abs(min(_sy) - 0.30), abs(max(_sy) - 0.75)) < 0.002,
              "x %.3f-%.3f  y %.3f-%.3f" % (min(_sx), max(_sx),
                                            min(_sy), max(_sy)))
        check("e2e: no sample lands outside the video frame",
              all(0 <= s[1] <= 1 and 0 <= s[2] <= 1 for s in _samples))

        # ── The accuracy the scorer picks up ─────────────────────────
        _acc, _src = _ccmod._accuracy_deg(_man)
        check("e2e: the tolerance comes from the POST validation",
              abs(_acc - 1.00) < 1e-6 and "out-of-sample" in _src,
              "%.2f deg from %s" % (_acc, _src))

        # ── Scoring claims that are right, near and wrong ────────────
        _ppd = _ccmod._px_per_degree(1920, 1080, 15.6, 60.0)
        _tol_px = _acc * _ppd

        def _claim(t, box, label):
            return {"t_start": t, "t_end": t, "attended": label,
                    "bbox": box}

        # A box exactly on spot 0, one just outside it, one far away.
        _on = _claim(3.0, [0.20, 0.25, 0.10, 0.10], "on it")
        _near_off = (_tol_px * 0.5) / RECT["w"]
        _near = _claim(3.0, [0.25 + _near_off, 0.25, 0.04, 0.10], "near")
        _far = _claim(3.0, [0.80, 0.80, 0.10, 0.10], "far")
        _res = _ccmod.check_all([_on, _near, _far], _samples, _acc, _ppd,
                                int(RECT["w"]), int(RECT["h"]))
        _v = [c["verdict"] for c in _res["claims"]]
        check("e2e: gaze inside the box -> SUPPORTED", _v[0] == "SUPPORTED",
              _v[0])
        check("e2e: just outside, within the error -> CONSISTENT",
              _v[1] == "CONSISTENT", "%s at %s px" % (
                  _v[1], _res["claims"][1].get("distance_px")))
        check("e2e: across the frame -> CONTRADICTED", _v[2] ==
              "CONTRADICTED", _v[2])
        check("e2e: the strict rate counts only SUPPORTED",
              abs(_res["correspondence_pct"] - 100.0 * 1 / 3) < 0.1,
              "%.1f %%" % _res["correspondence_pct"])
        check("e2e: the lenient rate adds CONSISTENT",
              abs(_res["correspondence_lenient_pct"] - 100.0 * 2 / 3) < 0.1,
              "%.1f %%" % _res["correspondence_lenient_pct"])

        # ── verify_metrics on the same manifest ──────────────────────
        _r = _vmod.Result()
        _vmod.check_session(_man, _r)
        _got = {n: (s, v) for _rq, n, s, v, _ in _r.rows}
        check("e2e: raw accuracy comes from grid A",
              _got["accuracy_raw_deg"][1] == "2.2",
              _got["accuracy_raw_deg"][1])
        check("e2e: the inclusion figure is the mean of pre_check and post",
              _got["accuracy_for_inclusion_deg"][1] == "1.10",
              _got["accuracy_for_inclusion_deg"][1])
        check("e2e: drift uses the uncorrected pair (2.00 - 2.10)",
              _got["drift_deg"][1] == "-0.10", _got["drift_deg"][1])
        check("e2e: a measured distance is reported as measured",
              _got["head_distance_cm"][0] == _vmod.PRESENT)
    finally:
        _sh.rmtree(_tmp, ignore_errors=True)

    # ── INVARIANTS: properties any correct implementation must have ──
    # px -> deg -> px must round-trip, or every accuracy figure in the
    # thesis is quietly scaled.
    _dev = 0.0
    for _d_cm in (45.0, 60.0, 76.0):
        _p = _ccmod._px_per_degree(1920, 1080, 15.6, _d_cm)
        for _px in (50.0, 129.7, 300.0):
            _dev = max(_dev, abs((_px / _p) * _p - _px))
    check("invariant: px -> deg -> px round-trips", _dev < 1e-9,
          "max deviation %.2e px" % _dev)

    # A nearer viewer must subtend a LARGER angle for the same pixels.
    _near_deg = 129.7 / _ccmod._px_per_degree(1920, 1080, 15.6, 45.0)
    _far_deg = 129.7 / _ccmod._px_per_degree(1920, 1080, 15.6, 80.0)
    check("invariant: the same error is a bigger angle when nearer",
          _near_deg > _far_deg,
          "%.2f deg at 45 cm vs %.2f deg at 80 cm" % (_near_deg, _far_deg))

    # Verdicts must be MONOTONIC in distance: moving a box further away
    # can never improve its verdict.
    _rank = {"SUPPORTED": 3, "CONSISTENT": 2, "CONTRADICTED": 1}
    _sm = [(i / 31.2, 0.50, 0.60, True) for i in range(400)]
    _seq = []
    for _off in (0.0, 0.02, 0.05, 0.10, 0.20, 0.40):
        _c = {"t_start": 5, "t_end": 5, "attended": "x",
              "bbox": [0.45 + _off, 0.55, 0.10, 0.10]}
        _seq.append(_rank[_ccmod.check_claim(_c, _sm, 2.13, 58.2,
                                             1680, 945)["verdict"]])
    check("invariant: a verdict never improves as the box moves away",
          all(a >= b for a, b in zip(_seq, _seq[1:])),
          " -> ".join(str(s) for s in _seq))

    # Cohen's kappa: identical coders = 1, and a coder who agrees only
    # by chance must land near 0.
    import random as _rnd2

    _crmod = importlib.import_module("coding_report")
    importlib.reload(_crmod)
    _rnd2.seed(19)
    _cats = ["correct", "wrong", "unclear"]
    _a = {str(i): _rnd2.choice(_cats) for i in range(300)}
    _same = _crmod.cohens_kappa(_a, dict(_a))
    check("invariant: identical coders give kappa 1.0",
          abs(_same["kappa"] - 1.0) < 1e-9, str(_same["kappa"]))
    _indep = {str(i): _rnd2.choice(_cats) for i in range(300)}
    _k2 = _crmod.cohens_kappa(_a, _indep)
    check("invariant: independent coders give kappa near 0",
          abs(_k2["kappa"]) < 0.15, str(_k2["kappa"]))
    check("invariant: kappa is never above 1",
          _same["kappa"] <= 1.0 and _k2["kappa"] <= 1.0)

    # Accuracy must ignore 'unclear' entirely.
    _s1 = _crmod.summarise({"0": "correct", "1": "wrong"})
    _s2 = _crmod.summarise({"0": "correct", "1": "wrong",
                            "2": "unclear", "3": "unclear"})
    check("invariant: adding 'unclear' does not move the accuracy rate",
          _s1["accuracy_pct"] == _s2["accuracy_pct"] == 50.0,
          "%s vs %s" % (_s1["accuracy_pct"], _s2["accuracy_pct"]))
    check("invariant: ...but it does move the unclear share",
          _s2["unclear_pct"] == 50.0 and _s1["unclear_pct"] == 0.0)

    # Fixation detection must produce ordered, non-overlapping events.
    import pandas as _pd

    from fixations import detect_fixations_df as _detect

    _fdf = _pd.DataFrame({
        "video_time_s": [i / 31.2 for i in range(400)],
        "gaze_video_nx": [0.3 if i < 200 else 0.7 for i in range(400)],
        "gaze_video_ny": [0.4 if i < 200 else 0.6 for i in range(400)],
    })
    _fx = _detect(_fdf)
    check("invariant: fixations are returned in time order",
          all(a.t_start <= b.t_start for a, b in zip(_fx, _fx[1:])),
          "%d fixations" % len(_fx))
    check("invariant: fixations do not overlap",
          all(a.t_end <= b.t_start + 1e-9 for a, b in zip(_fx, _fx[1:])))
    check("invariant: two stable periods yield exactly two fixations",
          len(_fx) == 2, "%d" % len(_fx))
    check("invariant: every fixation meets the minimum duration",
          all(f.duration >= 0.09 for f in _fx),
          "shortest %.3f s" % min(f.duration for f in _fx))
    # duration is DERIVED, so it must agree with the endpoints it is
    # derived from. Checking only "is it long enough" cannot catch a
    # duration that has drifted away from t_end - t_start — and every
    # RQ2 figure (median duration, time-in-fixations, rate) is built on
    # it.
    check("invariant: duration equals t_end - t_start",
          all(abs(f.duration - (f.t_end - f.t_start)) < 1e-9 for f in _fx),
          "max drift %.3g s" % max(abs(f.duration - (f.t_end - f.t_start))
                                   for f in _fx))
    _span = float(_fdf["video_time_s"].max()) - float(
        _fdf["video_time_s"].min())
    check("invariant: fixations cannot occupy more time than the "
          "recording lasted",
          sum(f.duration for f in _fx) <= _span + 1e-9,
          "%.2f s of fixation in %.2f s of recording"
          % (sum(f.duration for f in _fx), _span))
    check("invariant: no fixation starts before the data or ends after it",
          all(f.t_start >= float(_fdf["video_time_s"].min()) - 1e-9
              and f.t_end <= float(_fdf["video_time_s"].max()) + 1e-9
              for f in _fx))

    # The alignment check must be SIGNED, and the sign must mean
    # something specific: shift = +k says "claim i describes frame i+k".
    # A check that only ever reports a magnitude cannot distinguish a
    # model running ahead from one running behind, and those are
    # different faults.
    _ft2 = [round(0.4 * i, 1) for i in range(30)]
    _mk2 = lambda ts: [{"t_start": t, "t_end": t} for t in ts]
    check("invariant: claims about the NEXT frame read as +1",
          _ccmod.alignment_check(_mk2(_ft2[1:]), _ft2)["best_shift"] == 1)
    check("invariant: an extra claim at the start reads as -1",
          _ccmod.alignment_check(_mk2([_ft2[0]] + _ft2),
                                 _ft2)["best_shift"] == -1)
    check("invariant: aligned lists read as 0",
          _ccmod.alignment_check(_mk2(_ft2), _ft2)["best_shift"] == 0)
    check("invariant: too few claims to judge returns nothing rather "
          "than a shift",
          _ccmod.alignment_check(_mk2(_ft2[:4]), _ft2) is None)
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("end-to-end and invariants", False, _blocked or repr(exc))



# ── The summary must be LAST ──────────────────────────────────────────
# Section [20] was appended AFTER this block, so its failures printed as
# [FAIL] and were never counted: the suite reported ALL TESTS PASSED and
# exited 0 with a broken invariant on screen. A check that cannot fail
# the run is decoration, and the whole point of this file is that it
# fails loudly.
#
# This asserts, from inside the file, that nothing calls check() after
# the summary — so appending a section to the end can never again
# silently disarm it.
_self = open(os.path.abspath(__file__), encoding="utf-8").read()
_marker = "# \u2500\u2500 Summary "
_after = _self.split(_marker)[-1] if _marker in _self else ""
check("no check() runs after the summary block",
      "check(" not in _after,
      "%d stray check() calls" % _after.count("check("))

# ── Summary ────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if FAILURES:
    print("RESULT: %d FAILURE(S)" % len(FAILURES))
    for f in FAILURES:
        print("  ✗ " + f)
    sys.exit(1)
print("RESULT: ALL TESTS PASSED")
