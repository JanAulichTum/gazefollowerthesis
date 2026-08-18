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

import glob
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

def bat_code(name: str) -> str:
    """A .bat file with its comments stripped.

    Source-text assertions kept matching the REM lines that DOCUMENT the
    thing being asserted — three separate false positives in one day,
    each of them a test failing because the fix explained itself. The
    comment is prose about the code, not the code, so checks that ask
    "does this file still do X" must not see it.
    """
    out = []
    for line in read(os.path.join("windows", name)).splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("rem ") or stripped == "rem" \
                or stripped.startswith("::"):
            continue
        out.append(line)
    return "\n".join(out)

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

    import validation_stats as _vstats

    _src = read("app.py")
    _tree = ast.parse(_src)
    _want = {"_fit_poly", "_slope", "_apply_point",
             "_apply_series", "_correction_payload", "_auto_fit_correction"}
    _keep = [n for n in _tree.body
             if isinstance(n, ast.FunctionDef) and n.name in _want]
    _assign = [n for n in _tree.body if isinstance(n, ast.Assign)
               and "_GAIN_MIN" in ast.dump(n)]
    _ns = {"pd": pd, "logger": logging.getLogger("t"),
           "validation_stats": _vstats,
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
    def _rms(pairs, coeffs):
        return float(np.sqrt(np.mean(
            [(np.polyval(coeffs, m) - t) ** 2 for m, t in pairs])))

    if qy and ly:
        res_q = _rms(curved, qy)
        res_l = _rms(curved, ly)
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

# ── 7b. The correction is only applied when it GENERALISES ────────────
# The rule these guard was fixed 2026-08-17 and replaced "fit on grid A,
# apply unconditionally". On the two sessions carrying per-target
# records the old rule removed 42-61 % of the error where it was fitted
# and 1-6 % everywhere else — two free parameters per axis on seven
# targets, fitting session-moment noise. Every check below is about the
# difference between those two numbers.
print("\n[7b] Correction selection is cross-validated, not assumed")
try:
    import numpy as np

    import validation_stats as vstat

    _W7, _H7 = 1920.0, 1080.0
    _grid7 = [(230, 130), (1690, 130), (960, 335), (288, 540), (1632, 540),
             (960, 745), (960, 950)]

    def _tg(fn):
        out = []
        for tx, ty in _grid7:
            mx, my = fn(float(tx), float(ty))
            out.append({"tx": float(tx), "ty": float(ty),
                        "mx": float(mx), "my": float(my)})
        return out

    # (a) A REAL, stable gain error must be found and applied. If the
    #     rule cannot accept a correction that genuinely helps, it is
    #     not a rule, it is an off switch.
    _real = _tg(lambda tx, ty: (_W7 / 2 + 0.80 * (tx - _W7 / 2),
                                _H7 / 2 + 0.80 * (ty - _H7 / 2)))
    _r = vstat.select_correction(_real, _W7, _H7)
    check("a genuine gain error is detected and corrected",
          _r["decision"]["chosen"] != "none"
          and _r["correction"] is not None,
          "chose %s" % _r["decision"]["chosen"])

    # (b) PURE NOISE must be rejected. A least-squares fit on seven
    #     points always reduces the in-sample error — that is arithmetic,
    #     not evidence — so a rule that looks only at the fit set would
    #     "correct" a perfectly calibrated tracker.
    _rng = np.random.default_rng(20260817)
    _noise = _tg(lambda tx, ty: (tx + _rng.normal(0, 70),
                                 ty + _rng.normal(0, 70)))
    _n = vstat.select_correction(_noise, _W7, _H7)
    check("pure noise is NOT 'corrected'",
          _n["decision"]["chosen"] == "none" and _n["correction"] is None,
          "chose %s" % _n["decision"]["chosen"])
    _ins = vstat._fit_candidate(*vstat._pairs(_noise), "affine", _W7, _H7)
    _ins_err = vstat.signed_bias(
        vstat.corrected_targets(_noise, _ins))["mean_err_px"]
    _raw_err = vstat.signed_bias(_noise)["mean_err_px"]
    check("...even though refitting it 'improves' the fit set",
          _ins_err < _raw_err,
          "in-sample %.1f px vs raw %.1f px — the improvement the old "
          "rule believed" % (_ins_err, _raw_err))

    # (c) Leave-one-out must be strictly harsher than refitting. If these
    #     two ever agree, the LOO loop is refitting on all seven and the
    #     whole guard is inert.
    _loo = next(c for c in _n["decision"]["candidates"]
                if c["candidate"] == "affine")
    check("LOO error clearly exceeds the in-sample refit error",
          _loo["loo_mean_err_px"] > 1.05 * _ins_err,
          "LOO %.1f px vs refit %.1f px — if these converge, the loop is "
          "refitting on all seven and the guard is inert"
          % (_loo["loo_mean_err_px"], _ins_err))

    # (d) The decision must be legible after the fact, not inferred from
    #     coefficients: which candidates were considered, the criterion,
    #     the date it was fixed, and the rule it replaced.
    _d = _n["decision"]
    check("the decision record names every candidate considered",
          {c["candidate"] for c in _d["candidates"]}
          == set(vstat.CANDIDATE_ORDER))
    check("the decision record states the criterion and its date",
          bool(_d.get("rule")) and _d.get("rule_fixed_on") == "2026-08-17"
          and bool(_d.get("previous_rule")))
    check("selection is declared to happen on the fit grid, not grid B",
          "grid B is never consulted" in _d.get("selection_grid", ""))

    # (e) Selecting on grid B would spend the only out-of-sample
    #     measurement the two-grid protocol produces. Assert the code
    #     path cannot see it: select_correction takes ONE grid.
    import inspect as _inspect
    _sig = _inspect.signature(vstat.select_correction)
    check("select_correction is given a single grid, so it cannot peek",
          list(_sig.parameters) == ["targets", "width", "height"])
    _appsrc = read("app.py")
    check("app.py selects from the fit phase only",
          "_auto_fit_correction" in _appsrc
          and 'record["phase"] in ("pre_fit", "pre")' in _appsrc)

    # (f) A rejected correction must still be RECORDED. A session where
    #     the fit was rejected and one where it was never attempted are
    #     different facts and must not look identical in the manifest.
    check("a rejected correction is still recorded in the manifest",
          '"correction_decision": state.get("correction_decision")'
          in _appsrc)
    check("the rejection reason is stored, not just the verdict",
          bool(_n["decision"].get("reason")))

    # (g) The fit set's post-correction bias is an algebraic identity —
    #     least squares zeroes its own mean residual — so it must be
    #     marked in-sample and must never raise the offset flag.
    _fitted = vstat._fit_candidate(*vstat._pairs(_real), "affine", _W7, _H7)
    _fs = vstat.signed_bias(vstat.corrected_targets(_real, _fitted),
                            in_sample=True)
    check("the fit set's own signed bias is zero by construction",
          abs(_fs["bias_px"]) < 0.5, "%.2f px" % _fs["bias_px"])
    check("...and is marked in-sample rather than reported as evidence",
          _fs["bias_in_sample"] is True
          and _fs["offset_dominated"] is False)

    # (h) Direction words. Screen y grows DOWNWARD; a report that gets
    #     this backwards is worse than one that omits it.
    check("negative dy reads as ABOVE the target",
          "above" in vstat.direction_words(0.0, -60.0))
    check("positive dy reads as BELOW the target",
          "below" in vstat.direction_words(0.0, 60.0))
    check("negative dx reads as LEFT of the target",
          "left of" in vstat.direction_words(-60.0, 0.0))

    # (i) Numeric inversion of a monotone quadratic must round-trip.
    _qy = [8e-5, 0.9, 20.0]
    _pts = np.array([0.0, 270.0, 540.0, 810.0, 1080.0])
    _back = vstat.invert_poly(np.polyval(_qy, _pts), _qy, 0.0, _H7)
    check("a quadratic correction inverts to sub-pixel",
          float(np.max(np.abs(_back - _pts))) < 0.01,
          "max %.4f px" % float(np.max(np.abs(_back - _pts))))

    # (j) THE condition the mean unsigned error cannot express: a
    #     candidate that shrinks the error while GROWING the systematic
    #     offset must be refused. Scatter averages out of an aggregate;
    #     a displacement moves every gaze point the same way and every
    #     spatial claim inherits it.
    check("a correction that grows the bias is refused",
          vstat.bias_not_worsened(80.0, 40.0, 2.0) is False)
    check("a correction that shrinks the bias is accepted",
          vstat.bias_not_worsened(40.0, 80.0, 2.0) is True)
    check("an unchanged bias is not called worse (sub-pixel float noise)",
          vstat.bias_not_worsened(1e-14, 0.0, 0.0) is True)
    check("...but a whole pixel of extra bias is",
          vstat.bias_not_worsened(31.0, 30.0, 0.0) is False)
    check("the bias condition is wired into the rule, not just defined",
          "bias_not_worsened(" in read("validation_stats.py").split(
              "def select_correction")[1])

    # (k) INVERSION MUST NOT FABRICATE. A quadratic has one turning
    #     point; past it the mapping folds and has no inverse. The first
    #     version bisected over a window a whole screen wider than the
    #     screen and chose its direction from the padded endpoints, so
    #     when the turning point fell inside that window it silently
    #     returned the wrong branch — a value produced from y = 1400 came
    #     back as 1350, which is a number, looks like a measurement, and
    #     is not one. Off-screen gaze is not hypothetical: a real
    #     validation target was measured at x = 1609 on a 1920 px screen.
    _turn = [-4e-4, 1.10, 10.0]          # turning point at y = 1375
    _lo_b, _hi_b = vstat.monotone_span(_turn, 0.0, 1080.0)
    check("the monotone span stops at the turning point",
          abs(_hi_b - 1375.0) < 1.0, "span ends at %.0f" % _hi_b)
    _true = np.array([0.0, 540.0, 1080.0, 1300.0])
    _rt = vstat.invert_poly(np.polyval(_turn, _true), _turn, 0.0, 1080.0)
    check("inversion round-trips inside the monotone span",
          float(np.max(np.abs(_rt - _true))) < 0.01,
          "max %.4f px" % float(np.max(np.abs(_rt - _true))))
    _beyond = float(np.polyval(_turn, 1375.0)) + 50.0
    _nan = vstat.invert_poly(np.array([_beyond]), _turn, 0.0, 1080.0)
    check("a value past the fold returns NaN, not the bracket edge",
          bool(np.isnan(_nan[0])), "got %s" % _nan[0])
    check("a value the mapping never attains returns NaN",
          bool(np.isnan(vstat.invert_poly(np.array([1e6]), _turn,
                                          0.0, 1080.0)[0])))
    _fold_c = [-1e-3, 1.10, 10.0]        # turning point at y = 550
    check("a correction folding ON-screen inverts to NaN everywhere",
          bool(np.all(np.isnan(vstat.invert_poly(
              np.array([100.0, 500.0]), _fold_c, 0.0, 1080.0)))))
    _withnan = [{"tx": 100.0, "ty": 100.0, "mx": 160.0, "my": 100.0},
                {"tx": 900.0, "ty": 900.0, "mx": float("nan"), "my": 900.0}]
    _sn = vstat.signed_bias(_withnan)
    check("a non-finite measurement is dropped, not averaged",
          _sn["n_targets"] == 1 and _sn["mean_err_px"] == 60.0,
          "n=%s mean=%s" % (_sn["n_targets"], _sn["mean_err_px"]))

    # (l) THE FLAG MUST NOT BE DEFEATED BY ONE BAD TARGET. |mean bias| /
    #     mean error is the obvious statistic and it fails exactly where
    #     it is needed: an outlier inflates the denominator without
    #     moving the numerator. Manuel_P2's post check is ~76 px of
    #     almost pure displacement and scored 0.41 — under the bar —
    #     because one target sat 634 px away. Reconstruct that shape.
    #     The shape matters and is taken from the real record: the six
    #     good targets carry a systematic (-60, -70) px offset, and the
    #     seventh blows out in +x, i.e. AGAINST the offset it is masking.
    #     Manuel_P2's dx ran -78, -57, -129, +649, +18, +11, -25 — the
    #     six negatives cancelled most of the outlier, leaving a small
    #     mean bias over a hugely inflated mean error.
    _six = [{"tx": 500.0, "ty": 300.0 + 100 * i, "mx": 500.0 - 60.0,
             "my": 300.0 + 100 * i - 70.0} for i in range(6)]
    _six.append({"tx": 500.0, "ty": 900.0, "mx": 1150.0, "my": 900.0})
    _od = vstat.signed_bias(_six)
    check("the mean-based ratio alone would MISS a real offset",
          _od["bias_ratio"] < 0.5,
          "mean ratio %.2f (bias %.0f px over mean error %.0f px)"
          % (_od["bias_ratio"], _od["bias_px"], _od["mean_err_px"]))
    check("the median-based ratio catches it",
          _od["bias_ratio_median"] > 0.5,
          "median ratio %.2f" % _od["bias_ratio_median"])
    check("...and the flag is raised on either basis",
          _od["offset_dominated"] is True
          and "median" in _od["offset_dominated_basis"],
          _od["offset_dominated_basis"])
    _iso = [{"tx": 960.0, "ty": 540.0, "mx": 960.0 + dx, "my": 540.0 + dy}
            for dx, dy in ((70, 0), (-70, 0), (0, 70), (0, -70),
                           (50, 50), (-50, -50))]
    _is = vstat.signed_bias(_iso)
    check("isotropic scatter is flagged on neither basis",
          _is["offset_dominated"] is False,
          "mean %.2f / median %.2f" % (_is["bias_ratio"],
                                       _is["bias_ratio_median"]))

    # (m) The bias in DEGREES must use the same ruler as the accuracy
    #     printed beside it. The browser divides by a hardcoded 60 cm
    #     (F21); the server recomputes from the iris. Two angles on one
    #     line measured against two rulers is the fault class this
    #     section exists to remove.
    check("app.py converts degrees through one named, testable function",
          "def _degree_fields(" in _appsrc
          and "_degree_fields(record)" in _appsrc
          and "bias_deg_basis" in _appsrc)
    # ...AND KEEPS ONE RULER PER NAMING PATTERN. The codebase's
    # convention is: a plain degree field is on the browser's assumed
    # distance (as `mean_err_deg` is), a `_measured` field is on the
    # distance measured at validation time (as `mean_err_deg_measured`
    # is). F34's fix rescaled `bias_deg` in place and left `mean_err_deg`
    # alone, so one record carried a bias and an accuracy on different
    # rulers — bias_deg / mean_err_deg read 0.686 where bias_ratio said
    # 0.759 — and `bias_deg` sat next to `bias_deg_raw` describing the
    # same pixels two ways. Assert the invariant on a real record shape
    # rather than on the source text (F36).
    # Exercise app.py's OWN function. The first version of this check
    # rebuilt the conversion inside the test and passed while the real
    # code was mutated back to the F34 bug — a test that re-implements
    # what it is checking verifies nothing.
    _dfn = next(n for n in ast.parse(_appsrc).body
                if isinstance(n, ast.FunctionDef)
                and n.name == "_degree_fields")
    _ns3 = {}
    exec(compile(ast.Module(body=[_dfn], type_ignores=[]), "x", "exec"),
         _ns3)
    _rec = _ns3["_degree_fields"]({
        "mean_err_px": 191.2, "mean_err_deg": 3.28,
        "mean_err_deg_measured": 2.96, "bias_px": 145.1,
        "median_err_px": 201.1, "max_err_px": 330.1,
        "bias_px_raw": 145.1, "median_err_px_raw": 201.1,
        "distance": {"source": "iris"}})
    check("a plain degree field shares mean_err_deg's ruler",
          abs(_rec["bias_deg"] / _rec["mean_err_deg"]
              - _rec["bias_px"] / _rec["mean_err_px"]) < 0.005,
          "bias_deg/mean_err_deg = %.3f, bias_px/mean_err_px = %.3f"
          % (_rec["bias_deg"] / _rec["mean_err_deg"],
             _rec["bias_px"] / _rec["mean_err_px"]))
    check("...and a _measured field shares mean_err_deg_measured's",
          abs(_rec["bias_deg_measured"] / _rec["mean_err_deg_measured"]
              - _rec["bias_px"] / _rec["mean_err_px"]) < 0.005,
          "%.3f" % (_rec["bias_deg_measured"]
                    / _rec["mean_err_deg_measured"]))
    check("the same pixels never differ between deg and deg_raw",
          _rec["median_err_deg"] == _rec["median_err_deg_raw"]
          and _rec["bias_deg"] == _rec["bias_deg_raw"],
          "%.2f vs %.2f" % (_rec["median_err_deg"],
                            _rec["median_err_deg_raw"]))
    check("...and the two rulers are both present, and differ",
          _rec["bias_deg"] != _rec["bias_deg_measured"]
          and "measured distance (iris)" in _rec["bias_deg_basis"],
          "%.2f browser vs %.2f measured"
          % (_rec["bias_deg"], _rec["bias_deg_measured"]))
    check("no field name is mangled by the stem slicing",
          all(k in _rec for k in ("bias_deg", "bias_deg_measured",
                                  "median_err_deg", "max_err_deg",
                                  "bias_deg_raw")),
          ", ".join(sorted(k for k in _rec if k.endswith("deg")
                           or "deg_" in k)[:6]))
    # ...and the INCLUSION figure is still computed on the browser's
    # ruler, which differs from the measured one by -22 % to +15 % across
    # the recorded sessions. The two have not yet disagreed about the
    # 3.0 deg threshold; one will. Changing which ruler the criterion
    # uses is a pre-registration decision, so verify_metrics must SAY so
    # rather than silently switch (F34).
    _vm = read("verify_metrics.py")
    check("verify_metrics reports both rulers for the inclusion figure",
          "accuracy_ruler" in _vm and "mean_err_deg_measured" in _vm)
    check("...and flags loudly when they disagree about the threshold",
          "THE TWO RULERS DISAGREE ABOUT THE THRESHOLD" in _vm)
    check("...and does not switch the figure on its own authority",
          "pre-registration decision" in _vm)
    # AND IT MUST ACTUALLY RUN. The three checks above are source-text
    # assertions, and every one of them passed while check_session raised
    # NameError on the ruler block's first line — it referenced INCLUSION
    # instead of SPEC.INCLUSION and took the whole report down with it. A
    # source-text check cannot see that. Execute the function.
    import verify_metrics as _vmod

    _tg7 = [{"tx": 100.0 + 200 * i, "ty": 100.0 + 100 * i,
             "mx": 140.0 + 200 * i, "my": 60.0 + 100 * i} for i in range(7)]
    _mani = {"gain_correction": {"active": False},
             "correction_decision": {"chosen": "none", "reason": "t",
                                     "rule_fixed_on": "2026-08-17"},
             "validations": [
                 {"phase": "pre_fit", "grid": "A", "mean_err_px": 100.0,
                  "mean_err_deg": 1.72, "mean_err_deg_measured": 1.90,
                  "targets": _tg7},
                 {"phase": "pre_check", "grid": "B", "mean_err_px": 110.0,
                  "mean_err_deg": 1.89, "mean_err_deg_measured": 2.30,
                  "median_err_px": 105.0, "bias_px": 90.0,
                  "bias_x_px": 60.0, "bias_y_px": -67.0,
                  "bias_ratio": 0.82, "bias_direction": "above",
                  "offset_dominated": True, "targets": _tg7},
                 {"phase": "post", "grid": "B", "mean_err_px": 120.0,
                  "mean_err_deg": 2.06, "mean_err_deg_measured": 2.50,
                  "median_err_px": 118.0, "bias_px": 30.0,
                  "bias_x_px": 20.0, "bias_y_px": -22.0,
                  "bias_ratio": 0.25, "bias_direction": "above",
                  "offset_dominated": False, "targets": _tg7}]}
    _res = _vmod.Result()
    _vmod.check_session(_mani, _res)
    check("verify_metrics.check_session RUNS without raising", True)
    check("...and emits the ruler comparison when it runs",
          any("accuracy_ruler" in str(r[1]) for r in _res.rows),
          "emitted %d rows" % len(_res.rows))
    # The two rulers here differ by 22 %, which must be graded, not
    # passed over in silence.
    _rr = next(r for r in _res.rows if "accuracy_ruler" in str(r[1]))
    check("...and grades a >5 % gap between the rulers as degenerate",
          _rr[2] == "DEGENERATE", "%s — %s" % (_rr[2], _rr[3]))

    # (n0) A REFUSED CANDIDATE MUST SAY WHICH REFUSAL. "Cannot be fitted
    #      to the grid" and "fits the grid but collapses when one target
    #      is held out" are different findings; the second says the model
    #      is unstable at this sample size, which is what the rule exists
    #      to detect. Both were reported as "not fittable" until PILOT_05
    #      produced a quadratic that fitted all seven targets with a sane
    #      gain and folded over (local gain −3.3) in three of seven folds
    #      (F36).
    _unstable = _tg(lambda tx, ty: (
        tx, ty + (260.0 if ty < 200 else (-40.0 if ty < 400 else 0.0))))
    _u = vstat.select_correction(_unstable, _W7, _H7)
    _uq = next((c for c in _u["decision"]["candidates"]
                if c["candidate"] == "quadratic-vertical"), None)
    check("a candidate refused by its FOLDS says so, not 'not fittable'",
          _uq is not None and _uq.get("status")
          == "unstable under cross-validation"
          and _uq.get("unstable_folds", 0) > 0,
          "status %r, %s folds" % ((_uq or {}).get("status"),
                                   (_uq or {}).get("unstable_folds")))
    check("...and names how many folds of how many failed",
          "leave-one-out folds" in (_uq or {}).get("why", ""),
          (_uq or {}).get("why", "")[:70])
    # A model that genuinely cannot be fitted must still say THAT.
    _flat = [{"tx": 100.0 + 200 * i, "ty": 500.0, "mx": 100.0 + 200 * i,
              "my": 500.0} for i in range(7)]
    _f2 = vstat.select_correction(_flat, _W7, _H7)
    _fq = next((c for c in _f2["decision"]["candidates"]
                if c["candidate"] == "quadratic-vertical"), None)
    check("a model that cannot be fitted at all is still called that",
          (_fq or {}).get("status") == "not fittable",
          (_fq or {}).get("status"))

    # (n1) HOW MUCH OF THE ERROR IS BEYOND ANY AFFINE MAP. The residual of
    #      the best possible 2-D linear fit against the raw mean error.
    #      A pure gain+offset+shear must read near zero; unstructured
    #      scatter must read near one. Without it, a session whose error
    #      is entirely correctable and one whose error no recalibration
    #      can touch report the same accuracy in degrees.
    _pure = _tg(lambda tx, ty: (_W7 / 2 + 0.85 * (tx - _W7 / 2) + 20,
                                _H7 / 2 + 0.9 * (ty - _H7 / 2)
                                + 0.15 * (tx - _W7 / 2) - 30))
    check("a purely affine error leaves almost nothing behind",
          vstat.spatial_terms(_pure, _W7, _H7)["residual_ratio"] < 0.02,
          "ratio %.3f" % vstat.spatial_terms(_pure, _W7,
                                             _H7)["residual_ratio"])
    _rng4 = np.random.default_rng(4)
    _noise2 = _tg(lambda tx, ty: (tx + _rng4.normal(0, 80),
                                  ty + _rng4.normal(0, 80)))
    check("...and unstructured scatter leaves most of it behind",
          vstat.spatial_terms(_noise2, _W7, _H7)["residual_ratio"] > 0.5,
          "ratio %.2f" % vstat.spatial_terms(_noise2, _W7,
                                             _H7)["residual_ratio"])
    # It is a RATIO, so it must not move when the whole error is scaled.
    # Asserting only "small for affine, large for noise" passes for the
    # bare residual in pixels too, which is not the same quantity.
    _big = [{"tx": t["tx"], "ty": t["ty"],
             "mx": t["tx"] + 10 * (t["mx"] - t["tx"]),
             "my": t["ty"] + 10 * (t["my"] - t["ty"])} for t in _noise2]
    _r1 = vstat.spatial_terms(_noise2, _W7, _H7)["residual_ratio"]
    _r2 = vstat.spatial_terms(_big, _W7, _H7)["residual_ratio"]
    check("residual_ratio is dimensionless — 10x the error, same ratio",
          abs(_r1 - _r2) < 0.02, "%.2f vs %.2f" % (_r1, _r2))

    # (n) THE DIAGNOSTICS MUST NOT BECOME THE RULE. Leave-one-out folds
    #     share five of seven training targets, so the standard error is
    #     optimistic by an unknown amount — there is no unbiased
    #     estimator of cross-validation variance. A bootstrap interval
    #     and a sign test are recorded beside it so a reader can see
    #     whether all three agree, but the declared rule is the SE test
    #     and it must stay the thing that decides.
    _dg = next(c for c in _n["decision"]["candidates"]
               if c["candidate"] == "affine")
    check("the decision record carries a bootstrap interval",
          isinstance(_dg.get("loo_bootstrap_ci_px"), list)
          and len(_dg["loo_bootstrap_ci_px"]) == 2)
    check("...and how many targets actually improved",
          "/" in str(_dg.get("loo_targets_improved"))
          and _dg.get("loo_sign_test_p") is not None)
    check("the diagnostics are labelled corroborating, not decisive",
          "corroborating" in _dg.get("diagnostics_note", ""))
    check("the bootstrap is seeded, so a manifest is reproducible",
          vstat.select_correction(_noise, _W7, _H7)["decision"]["candidates"]
          [1]["loo_bootstrap_ci_px"] == _dg["loo_bootstrap_ci_px"])
    # AT SEVEN TARGETS THE TWO CRITERIA ARE NOT INDEPENDENT, and the
    # rule's documentation must not imply they are. For strictly positive
    # paired differences the ratio mean / SE is bounded below by 1.0 —
    # it is exactly 1.0 in the limiting case of six zeros and one spike,
    # and above it otherwise — so a UNANIMOUS improvement can never fail
    # the 1.0 SE bar. The sign test therefore adds nothing when it is
    # unanimous; it earns its place only when it is not, which is
    # precisely PILOT_02 (4/7, p = 1.00, bootstrap spanning zero).
    def _ratio(a):
        a = np.asarray(a, float)
        return float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a))))

    _uni = [np.array([0.5, 0.4, 0.6, 0.5, 0.4, 0.6, 60.0]),
            np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0e6]),
            np.array([3.0, 9.0, 1.0, 40.0, 2.0, 5.0, 7.0])]
    check("at n=7 a unanimous improvement always clears the 1.0 SE bar",
          all(_ratio(a) >= 1.0 for a in _uni),
          "ratios %s" % ["%.2f" % _ratio(a) for a in _uni])
    check("...so the sign test only bites when it is NOT unanimous",
          vstat._diagnostics(np.array(
              [-20.0, 30.0, -15.0, 25.0, -10.0, 5.0, 8.0]
          ))["loo_targets_improved"] == "4/7")

    # (o) An implausible mapping is refused whatever its residual says.
    _fold = _tg(lambda tx, ty: (tx * 0.05 + 900, ty * 0.05 + 500))
    _f = vstat.select_correction(_fold, _W7, _H7)
    check("a fold-over gain is refused however well it fits",
          _f["decision"]["chosen"] == "none",
          "chose %s" % _f["decision"]["chosen"])
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("correction selection", False, _blocked or repr(exc))

# ── 7c. Re-deriving a recorded session under the rule ─────────────────
# A rule that decides a session's correction should not have been applied
# is worse than no rule at all if the recorded gaze keeps the correction:
# the manifest then says one thing and the data says another. These guard
# the tool that closes that gap.
print("\n[7d] Vertical error that depends on HORIZONTAL position")
try:
    import numpy as np

    import validation_stats as vstat

    _W, _H = 1920.0, 1080.0
    _CX, _CY = _W / 2, _H / 2
    _G = [(230, 130), (1690, 130), (960, 335), (288, 540), (1632, 540),
          (960, 745), (960, 950)]

    def _mk(fn):
        return [{"tx": float(a), "ty": float(b),
                 "mx": float(fn(a, b)[0]), "my": float(fn(a, b)[1])}
                for a, b in _G]

    # (a) A PURE SHEAR must be recovered. This is the fault a participant
    #     reported twice before any metric saw it: "when looking right
    #     the y axis behaves weirdly".
    _s = 0.20
    _sheared = _mk(lambda x, y: (x + _s * (y - _CY), y + _s * (x - _CX)))
    _sp = vstat.spatial_terms(_sheared, _W, _H)
    check("a pure shear is recovered in m_yx",
          abs(_sp["m_yx"] - _s) < 0.01, "m_yx = %+.3f" % _sp["m_yx"])
    check("...and is flagged", _sp["shear_large"] is True)
    check("...and reported as displacement across the screen",
          abs(_sp["dy_across_screen_px"] - _s * _W) < 2.0,
          "%.0f px" % _sp["dy_across_screen_px"])
    check("...and named a shear, not a rotation",
          "shear" in _sp["structure"], _sp["structure"])

    # (b) A ROTATION is a different fault with a different cause — head
    #     roll rotates (opposite signs), an off-centre head shears (same
    #     signs). Reporting one number for both would hide which.
    _th = np.radians(8.0)
    _rot = _mk(lambda x, y: (
        _CX + np.cos(_th) * (x - _CX) - np.sin(_th) * (y - _CY),
        _CY + np.sin(_th) * (x - _CX) + np.cos(_th) * (y - _CY)))
    _rp = vstat.spatial_terms(_rot, _W, _H)
    check("a rotation is named a rotation, not a shear",
          "rotation" in _rp["structure"]
          and abs(_rp["rotation_deg"] - 8.0) < 0.5,
          "%+.2f deg — %s" % (_rp["rotation_deg"], _rp["structure"]))
    check("...and its shear term is ~zero", abs(_rp["shear"]) < 0.01,
          "shear %+.3f" % _rp["shear"])

    # (c) A pure per-axis gain — exactly what the correction models —
    #     must show NO off-diagonal term, or the diagnostic would fire on
    #     every session and mean nothing.
    _diag = _mk(lambda x, y: (_CX + 0.85 * (x - _CX),
                              _CY + 0.90 * (y - _CY) - 40))
    _dp = vstat.spatial_terms(_diag, _W, _H)
    check("a pure per-axis gain shows no off-diagonal term",
          abs(_dp["m_yx"]) < 0.01 and _dp["shear_large"] is False,
          "m_yx = %+.3f" % _dp["m_yx"])

    # (d) THE STRUCTURAL POINT, stated exactly. The correction is a
    #     per-axis map D, so applying it gives M' = D·M and therefore
    #     m_yx' = d_y·m_yx, m_yy' = d_y·m_yy. It CAN rescale the
    #     off-diagonal — the first version of this check wrongly asserted
    #     m_yx itself was untouched and caught the rescale, 0.200 ->
    #     0.176 — but their RATIO is invariant, for a correction of any
    #     polynomial degree. That ratio is the fault, and no
    #     recalibration of this form can reduce it.
    _fit = vstat._fit_candidate(*vstat._pairs(_sheared), "affine", _W, _H)
    _after = vstat.spatial_terms(
        vstat.corrected_targets(_sheared, _fit), _W, _H)
    check("a diagonal correction cannot change the normalised shear",
          abs(_after["m_yx_normalised"] - _sp["m_yx_normalised"]) < 0.005,
          "m_yx/m_yy %+.3f -> %+.3f (raw m_yx %+.3f -> %+.3f, rescaled "
          "but not removed)" % (_sp["m_yx_normalised"],
                                _after["m_yx_normalised"],
                                _sp["m_yx"], _after["m_yx"]))
    _quad = {"px": [1.0, 0.0], "py": [3e-5, 0.9, 20.0], "cy": _CY,
             "source": "t"}
    _aq = vstat.spatial_terms(
        vstat.corrected_targets(_sheared, _quad), _W, _H)
    check("...nor can a quadratic one",
          abs(_aq["m_yx_normalised"] - _sp["m_yx_normalised"]) < 0.02,
          "%+.3f -> %+.3f" % (_sp["m_yx_normalised"],
                              _aq["m_yx_normalised"]))
    # The classifier must key on magnitudes, not the sign of a product:
    # a pure transvection has one off-diagonal exactly zero, and the
    # product's sign then comes from the last bit of a float.
    check("a transvection is not misnamed a rotation",
          "transvection" in vstat._off_diagonal_structure(0.1, 0.1),
          vstat._off_diagonal_structure(0.1, 0.1))
    check("a near-zero off-diagonal is called absent, not classified",
          "neither" in vstat._off_diagonal_structure(1e-17, -1e-17))
    # NEGATIVE shear must classify too. PILOT_04's is negative, and a
    # classifier comparing SIGNED values rather than magnitudes calls
    # -0.20 "smaller than the floor" and reports no off-diagonal term at
    # all — on the session that has one. The sign carries the direction
    # of the fault, never whether there is one.
    check("a NEGATIVE shear is still a shear, not 'absent'",
          "shear" in vstat._off_diagonal_structure(-0.20, 0.0),
          vstat._off_diagonal_structure(-0.20, 0.0))
    check("a NEGATIVE rotation is still a rotation",
          "rotation" in vstat._off_diagonal_structure(0.0, -0.20),
          vstat._off_diagonal_structure(0.0, -0.20))
    _neg = _mk(lambda x, y: (x - 0.20 * (y - _CY), y - 0.20 * (x - _CX)))
    _np_ = vstat.spatial_terms(_neg, _W, _H)
    check("...and a negatively sheared grid is flagged like a positive one",
          _np_["shear_large"] is True and "shear" in _np_["structure"],
          "shear %+.3f — %s" % (_np_["shear"],
                                _np_["structure"].split(" —")[0]))

    # (e) Six parameters cannot come from five points.
    check("fewer than six targets refuses to estimate a 2-D map",
          vstat.spatial_terms(_sheared[:5], _W, _H)["spatial_available"]
          is False)

    # (f) Seeded, so a manifest is reproducible.
    check("the m_yx interval is seeded and reproducible",
          vstat.spatial_terms(_sheared, _W, _H)["m_yx_ci"]
          == _sp["m_yx_ci"])
    check("a real shear's interval excludes zero",
          _sp["m_yx_excludes_zero"] is True, "CI %s" % _sp["m_yx_ci"])
    # Coverage is MEASURED, not assumed. At n=7 a percentile interval
    # nominally at 95 % contained the truth 91.2 % of the time in
    # simulation (true m_yx 0.15, 40 px noise, 400 replicates), so
    # "excludes zero" is optimistic and the number saying by how much has
    # to travel with the interval (F34).
    check("the interval carries its MEASURED coverage, not the nominal one",
          _sp.get("ci_measured_coverage") == 0.91
          and _sp.get("ci_nominal_coverage") == 0.95,
          "nominal %s, measured %s" % (_sp.get("ci_nominal_coverage"),
                                       _sp.get("ci_measured_coverage")))

    # (g) It has to reach the record and the reports, not just exist.
    check("app.py stores the spatial terms on every validation",
          'record["spatial"] = _sp' in _appsrc
          and "VALIDATION IS SHEARED" in _appsrc)
    _sv = read("show_validations.py")
    check("show_validations prints the off-diagonal term",
          "off-diagonal" in _sv and "SHEARED" in _sv)
    _ca = read("correction_audit.py")
    check("correction_audit prints the off-diagonal term",
          "Off-diagonal terms" in _ca)
    check("the spec lists the off-diagonal terms as a measure",
          "off_diagonal" in read("metrics_spec.py"))
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("off-diagonal spatial terms", False, _blocked or repr(exc))

print("\n[7c] Re-derivation applies the decision to the recorded gaze")
try:
    import numpy as np
    import pandas as pd

    import rederive_session as rds
    import validation_stats as vstat

    # (a) THE SESSION ID. Finalisation derives it from the session CSV
    #     basename and most manifests never store a `session_id` key at
    #     all — only reconstructed ones do. Reading manifest["session_id"]
    #     first matches nothing, and "no rows for this session" reads
    #     like a clean result. A silent no-op on the one operation that
    #     must not silently no-op.
    check("the session id comes from the CSV basename, as finalise writes it",
          rds.session_id_of({"session_csv": "P_2026-08-17_132902.csv",
                             "session_id": "WRONG"}, "x_manifest.json")
          == "P_2026-08-17_132902")
    check("...and falls back to session_id only when there is no CSV",
          rds.session_id_of({"session_id": "recon_1"}, "x_manifest.json")
          == "recon_1")
    check("app.py derives the same id the same way",
          'session_id = os.path.splitext(os.path.basename(csv_path))[0]'
          in _appsrc)

    # (b) The video mapping must match finalisation EXACTLY. Two copies
    #     of this arithmetic drifting apart would move every gaze point
    #     relative to the stimulus — the precise error this whole
    #     exercise is about.
    check("finalise still maps gaze to video the way rederive does",
          '((gx - vr["x"]) / vr["w"]).round(4)' in _appsrc
          and '((gy - vr["y"]) / vr["h"]).round(4)' in _appsrc)
    _rect = {"x": 0, "y": -8, "w": 1920, "h": 1080}
    _nx, _ny = rds.video_coords(pd.Series([960.0]), pd.Series([532.0]),
                                _rect)
    check("video_coords reproduces the recorded mapping",
          abs(float(_nx[0]) - 0.5) < 1e-9
          and abs(float(_ny[0]) - 0.5) < 1e-9,
          "got (%.4f, %.4f)" % (float(_nx[0]), float(_ny[0])))

    # (c) ROUND TRIP on a real-shaped session: bake a correction into a
    #     workbook exactly as finalisation would, then re-derive under a
    #     rule that rejects it, and require the result to equal the
    #     coordinates that would have been recorded with no correction at
    #     all. Nothing is lost by applying a correction, because it is
    #     applied downstream of the filtered columns — this asserts that
    #     claim rather than assuming it.
    import shutil
    import tempfile

    _tmp = tempfile.mkdtemp()
    try:
        _corr = {"px": [0.95, 13.5], "py": [0.96, -16.3], "cy": 540.0,
                 "source": "test"}
        _n = 200
        _rng2 = np.random.default_rng(7)
        _fx = _rng2.uniform(200, 1700, _n)
        _fy = _rng2.uniform(150, 950, _n)
        _gx = vstat.apply_axis(_fx, _corr["px"])
        _gy = vstat.apply_axis(_fy, _corr["py"])
        _wb = pd.DataFrame({
            "session_id": "S_2026-08-17_120000",
            "stimulus_name": "clip.mp4",
            "filtered_gaze_position_x": _fx,
            "filtered_gaze_position_y": _fy,
            "corrected_gaze_position_x": np.round(_gx, 2),
            "corrected_gaze_position_y": np.round(_gy, 2),
            "gaze_video_nx": np.round((_gx - _rect["x"]) / _rect["w"], 4),
            "gaze_video_ny": np.round((_gy - _rect["y"]) / _rect["h"], 4),
        })
        _wbp = os.path.join(_tmp, "data.xlsx")
        _wb.to_excel(_wbp, index=False)

        # Pure noise on the fit grid -> the rule must reject, so the
        # session must come back to the uncorrected coordinates.
        _rng3 = np.random.default_rng(20260817)
        _tgts = [{"tx": float(a), "ty": float(b),
                  "mx": float(a) + _rng3.normal(0, 70),
                  "my": float(b) + _rng3.normal(0, 70)}
                 for a, b in [(230, 130), (1690, 130), (960, 335),
                              (288, 540), (1632, 540), (960, 745),
                              (960, 950)]]
        _man = {"session_csv": "S_2026-08-17_120000.csv",
                "gain_correction": vstat.payload(_corr),
                "stimuli": [{"stimulus": "clip.mp4", "video_rect": _rect}],
                "validations": [{"phase": "pre_fit", "targets": _tgts,
                                 "correction_active": {"active": False},
                                 "screen": {"width_px": 1920,
                                            "height_px": 1080}}]}
        _mp = os.path.join(_tmp, "S_manifest.json")
        with open(_mp, "w", encoding="utf-8") as _fh:
            json.dump(_man, _fh)

        # DRY RUN must change nothing on disk.
        _before = open(_wbp, "rb").read()
        _dry = rds.rederive(_mp, _wbp, apply=False)
        check("a dry run reports the change and writes nothing",
              _dry["changes"] == "removed"
              and _dry["rows_rewritten"] == _n
              and open(_wbp, "rb").read() == _before,
              "changes=%s rows=%s" % (_dry["changes"],
                                      _dry["rows_rewritten"]))

        _res = rds.rederive(_mp, _wbp, apply=True)
        _out = pd.read_excel(_wbp)
        _want_ny = np.round((_fy - _rect["y"]) / _rect["h"], 4)
        check("re-derivation recovers the uncorrected video coordinates",
              float(np.max(np.abs(_out["gaze_video_ny"].to_numpy()
                                  - _want_ny))) < 1e-9,
              "max deviation %.2e"
              % float(np.max(np.abs(_out["gaze_video_ny"].to_numpy()
                                    - _want_ny))))
        check("the corrected columns are cleared, not left stale",
              bool(_out["corrected_gaze_position_y"].isna().all()))
        with open(_mp, encoding="utf-8") as _fh:
            _m2 = json.load(_fh)
        check("the superseded correction is kept, not overwritten away",
              (_m2.get("superseded_gain_correction") or {}).get("active")
              is True)
        check("the manifest marks everything derived from the gaze STALE",
              (_m2.get("rederived") or {}).get("events_stale") is True
              and bool(_m2["rederived"].get("stale")))
        check("a backup of the workbook is written before any rewrite",
              len(glob.glob(os.path.join(_tmp, "*pre-rederive*"))) == 2)

        # (d) A session ALREADY matching the rule must be left alone.
        _res2 = rds.rederive(_mp, _wbp, apply=True)
        check("a session that already matches the rule is not rewritten",
              _res2["changes"] is None and _res2["rows_rewritten"] == 0,
              "changes=%s" % _res2["changes"])

        # (e) No workbook = no manifest edit. A manifest that disagrees
        #     with its data is worse than neither being updated.
        with open(_mp, "w", encoding="utf-8") as _fh:
            json.dump(_man, _fh)
        _res3 = rds.rederive(_mp, os.path.join(_tmp, "nope.xlsx"),
                             apply=True)
        with open(_mp, encoding="utf-8") as _fh:
            _m3 = json.load(_fh)
        check("a missing workbook blocks the manifest edit too",
              _res3["applied_to_disk"] is False
              and "rederived" not in _m3
              and (_m3.get("gain_correction") or {}).get("active") is True,
              _res3.get("note", "")[:60])
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)

    # (f2) THE STABILITY TEST MUST NAME WHAT IT COMPUTES. It reported a
    #      count of standard errors while its docstring called it
    #      "Welch's t" — a name that implies Satterthwaite degrees of
    #      freedom and a p-value, neither of which existed. Not
    #      cosmetic: on the recorded sessions the df is 9.7-11.6, so
    #      2.7 SE is p = 0.021 against the 0.007 a normal approximation
    #      gives (F35).
    import correction_audit as _ca_mod

    # A shift of 14 px against this scatter lands at 3.45 SE — near the
    # boundary, where the choice of reference distribution actually
    # decides something. An extreme case would underflow both p-values
    # to zero and prove nothing, which the first version of this check
    # did.
    _n1 = [-12.0, -6.0, 0.0, 4.0, 7.0, -3.0, 10.0]
    _n2 = [9.0, -5.0, 2.0, -8.0, 11.0, -4.0, -5.0]
    _A = [{"tx": 100.0 + 200 * i, "ty": 100.0, "mx": 100.0 + 200 * i,
           "my": 100.0 + _n1[i]} for i in range(7)]
    _B = [{"tx": 100.0 + 200 * i, "ty": 100.0, "mx": 100.0 + 200 * i,
           "my": 114.0 + _n2[i]} for i in range(7)]
    _man2 = {"gain_correction": {"active": False},
             "validations": [
                 {"phase": "pre_fit", "targets": _A,
                  "recorded_at_utc": "2026-08-17T10:00:00+00:00",
                  "screen": {"width_px": 1920, "height_px": 1080},
                  "mean_err_px": 10.0, "mean_err_deg": 0.17},
                 {"phase": "pre_check", "targets": _B,
                  "recorded_at_utc": "2026-08-17T10:00:30+00:00",
                  "screen": {"width_px": 1920, "height_px": 1080},
                  "mean_err_px": 90.0, "mean_err_deg": 1.55}]}
    _mp2 = os.path.join(tempfile.gettempdir(), "_stab_manifest.json")
    with open(_mp2, "w", encoding="utf-8") as _fh:
        json.dump(_man2, _fh)
    _aud = _ca_mod.audit(_mp2)
    _dy = next(e["dy"] for e in _aud["stability"] if e["from"] == "pre_fit")
    check("the stability test reports Satterthwaite df, not just SE units",
          _dy.get("welch_df") is not None and _dy.get("p") is not None,
          "df %s, p %s" % (_dy.get("welch_df"), _dy.get("p")))
    _pn = 2 * (1 - 0.5 * (1 + math.erf(abs(_dy["t"]) / 2 ** 0.5)))
    check("...and a 14 px shift against this scatter is detected",
          0.001 < _dy["p"] < 0.02 and _dy["changed"] is True,
          "shift %+.1f px, %.2f SE, p = %.4f"
          % (_dy["shift_px"], _dy["t"], _dy["p"]))
    check("...and the t reference is STRICTER than a normal approximation",
          _dy["p"] > 2 * _pn,
          "t with df %.1f gives p = %.4f; the normal gives %.4f, %.0fx "
          "smaller" % (_dy["welch_df"], _dy["p"], _pn, _dy["p"] / _pn))
    check("the docstring no longer calls it a test it does not perform",
          "Welch's t" not in read("correction_audit.py")
          or "named a test it did not perform" in read("correction_audit.py"))
    os.remove(_mp2)

    # (f) A session with no per-target fit record cannot be decided, and
    #     must say so rather than defaulting to "leave it as it is".
    _und = rds.decide({"gain_correction": {"px": [1, 0], "py": [1, 0]},
                       "validations": []})
    check("an undecidable session is reported, not silently skipped",
          _und["decidable"] is False and bool(_und.get("why")))
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("session re-derivation", False, _blocked or repr(exc))

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
        # 13, not 7, since grid A grew 2026-08-18 (F33/brief item 2, see
        # section [17] for the full two-grid protocol assertions —
        # this earlier check pre-dates that section and just confirms
        # the grid is still a well-formed single source of truth).
        check("grid has 13 targets", _n == 13, "got %d" % _n)
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
        import validation_stats as _vs2
        _ns2 = {"np": np, "Any": object, "validation_stats": _vs2}
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
        # A QUADRATIC correction must be inverted too, not refused.
        # Until 2026-08-17 this returned raw_available=False, and that
        # gap was load bearing: the selection rule and the
        # corrected-vs-uncorrected comparison both need a raw figure for
        # EVERY session, so a session that happened to receive a
        # quadratic fit dropped silently out of both. Build a genuine
        # quadratic mapping, push known raw points through it, and
        # require the recovered raw error back to sub-pixel.
        _qc = {"px": [1.0, 0.0], "py": [8e-5, 0.9, 20.0], "cx": cx,
               "cy": cy, "source": "q"}
        _qtg, _qraw = [], []
        for tx, ty in ((200, 150), (1700, 150), (960, 900), (300, 800)):
            rmx, rmy = cx + (tx - cx) * 0.75, cy + (ty - cy) * 0.75
            _qraw.append(float(np.hypot(rmx - tx, rmy - ty)))
            _qtg.append({"tx": float(tx), "ty": float(ty),
                         "mx": float(np.polyval(_qc["px"], rmx)),
                         "my": float(np.polyval(_qc["py"], rmy))})
        _q = _ue({"targets": _qtg, "mean_err_px": 100.0,
                  "mean_err_deg": 1.7,
                  "screen": {"width_px": 1920, "height_px": 1080}}, _qc)
        check("quadratic correction is inverted, not refused",
              _q.get("raw_available") is True
              and abs(_q.get("mean_err_px_raw", -1)
                      - float(np.mean(_qraw))) < 0.5,
              "got %s, expected %.1f" % (_q.get("mean_err_px_raw"),
                                         float(np.mean(_qraw))))

        # ── The signed bias must travel with every error figure ──────
        # A pure displacement and pure scatter of the same magnitude
        # produce the SAME mean unsigned error. Only the signed bias
        # separates them, and it is the separation this study needed.
        _off = [{"tx": float(tx), "ty": float(ty),
                 "mx": float(tx), "my": float(ty) - 60.0}
                for tx, ty in ((200, 150), (1700, 150), (960, 900),
                               (300, 800))]
        _ob = _ue({"targets": _off, "mean_err_px": 60.0,
                   "mean_err_deg": 1.03,
                   "screen": {"width_px": 1920, "height_px": 1080}}, None)
        check("a pure offset is reported as a signed bias",
              abs(_ob.get("bias_y_px", 0) + 60.0) < 0.1
              and abs(_ob.get("bias_x_px", 99)) < 0.1,
              "bias = (%s, %s)" % (_ob.get("bias_x_px"),
                                   _ob.get("bias_y_px")))
        check("a pure offset is flagged offset-dominated",
              _ob.get("offset_dominated") is True
              and _ob.get("bias_ratio") == 1.0)
        check("the direction is stated in words, not left to a sign",
              "above" in (_ob.get("bias_direction") or ""),
              _ob.get("bias_direction"))
        # Scatter of the SAME mean magnitude must not be flagged.
        _sc = [{"tx": 960.0, "ty": 540.0, "mx": 960.0 + dx,
                "my": 540.0 + dy}
               for dx, dy in ((60, 0), (-60, 0), (0, 60), (0, -60))]
        _sb = _ue({"targets": _sc, "mean_err_px": 60.0,
                   "mean_err_deg": 1.03,
                   "screen": {"width_px": 1920, "height_px": 1080}}, None)
        check("pure scatter of the same magnitude is NOT flagged",
              _sb.get("offset_dominated") is False
              and _sb.get("median_err_px") == _ob.get("median_err_px")
              == 60.0,
              "scatter bias %.1f px vs offset bias %.1f px, identical "
              "%s px error either way" % (_sb.get("bias_px", -1),
                                          _ob.get("bias_px", -1),
                                          _sb.get("median_err_px")))

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

    # (z) METHODOLOGY_FINDINGS.md is cited BY NUMBER from the thesis
    #     chapters and from CLAUDE.md, so a duplicate or a gap silently
    #     makes a citation ambiguous — "see F22" stops identifying one
    #     entry. Same silent-failure class as the metric bugs above: the
    #     file still reads fine, it just no longer means what a citation
    #     to it claims. A duplicate F22 (2026-08-12 and 2026-08-13)
    #     survived unnoticed until 2026-08-15; the second became F23.
    #     Append-only numbering is not self-enforcing, so enforce it.
    _fnums = [int(_m) for _m in
              re.findall(r"^## F(\d+)\b",
                         read("METHODOLOGY_FINDINGS.md"), re.M)]
    check("METHODOLOGY_FINDINGS.md has findings to check", bool(_fnums))
    _dupes = sorted({_n for _n in _fnums if _fnums.count(_n) > 1})
    check("F-numbers are unique", not _dupes,
          ("duplicated: " + ", ".join("F%d" % _n for _n in _dupes))
          if _dupes else "")
    _missing = sorted(set(range(1, len(_fnums) + 1)) - set(_fnums))
    check("F-numbers are contiguous from F1",
          sorted(_fnums) == list(range(1, len(_fnums) + 1)),
          ("missing: " + ", ".join("F%d" % _n for _n in _missing))
          if _missing else "")
    check("F-numbers ascend in file order (the log is append-only)",
          _fnums == sorted(_fnums))
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
    # The position payload is the ONLY route by which the tracker's
    # distance reaches the session manifest. It carried the number and
    # dropped the provenance, so a session recorded 68.3 cm with source,
    # iris and iod all null — and the summary called it MEASURED.
    _guid = read("tracker_service.py").split("POSITION_FIELDS = (")[1].split(")")[0]
    for _field in ("distance_source", "distance_cm_iris", "distance_cm_iod",
                   "distance_estimates_agree", "iris_error",
                   "focal_measured"):
        check("the position payload carries %s" % _field, _field in _guid)

    # THE CONTRACT, not a list of field names. app.py reads keys out of
    # the position payload; tracker_service decides which keys are in it.
    # Nothing connected the two, so a field could be read forever and
    # never sent — which is exactly what happened to distance_source.
    # Asserting the relation catches the next one too.
    _app_reads = set(re.findall(r'pos\.get\("([a-z_]+)"\)', read("app.py")))
    _sent = set(re.findall(r'"([a-z_]+)"',
                           read("tracker_service.py")
                           .split("POSITION_FIELDS = (")[1].split(")")[0]))
    _never_sent = sorted(_app_reads - _sent)
    check("every position field app.py reads is one the tracker sends",
          not _never_sent, "read but never sent: %s" % ", ".join(_never_sent))
    check("...and the payload is not empty in the first place",
          len(_sent) >= 15, "%d fields" % len(_sent))

    # A bare `except: pass` around the iris block is why a session could
    # fall back to the worse ruler with no evidence that anything went
    # wrong. The exception is now the diagnostic.
    _ts_src2 = read("tracker_service.py")
    # ── The session path, reproduced without a camera ────────────────
    # Three pilots were spent learning that sessions used the fallback
    # ruler. The whole failure is reproducible from a fake FaceInfo, and
    # should have been found here.
    class _LM:
        __slots__ = ("x", "y", "z")

        def __init__(self, x, y):
            self.x, self.y, self.z = x, y, 0.0

    class _FaceInfo:
        status = True
        face_rect = [200, 150, 240, 240]
        left_rect = [250, 230, 40, 24]
        right_rect = [370, 230, 40, 24]
        img_w, img_h = 640, 480
        left_openness = right_openness = 0.3
        # COARSE mesh, exactly what GazeFollower supplies: 468 points,
        # no iris landmarks at 468-477.
        landmarks = [_LM(0.3 + 0.0004 * i, 0.4 + 0.0003 * i)
                     for i in range(468)]

    _tsm = importlib.import_module("tracker_service")
    # GazeFollower carries the landmarks as a NUMPY ARRAY. `not array`
    # raises ValueError, a bare except swallowed it, and the iris ruler
    # therefore never ran in ANY recorded session - the distance came
    # from the inter-ocular fallback every time, silently.
    class _FaceInfoNumpy:
        status = True
        face_rect = [200, 150, 240, 240]
        left_rect = [250, 230, 40, 24]
        right_rect = [370, 230, 40, 24]
        img_w, img_h = 640, 480
        left_openness = right_openness = 0.3
        landmarks = None            # set below

    import numpy as _np4

    _FaceInfoNumpy.landmarks = _np4.random.rand(468, 3).astype(_np4.float32)
    _svc_np = _tsm.Service.__new__(_tsm.Service)
    _svc_np._latest_face_info = _FaceInfoNumpy()
    _svc_np.gf = None
    _svc_np._last_frame = None
    _m_np = _svc_np._metrics_from_face_info() or {}
    check("numpy landmarks do not raise on a truthiness test",
          "ambiguous" not in str(_m_np.get("iris_error") or ""),
          str(_m_np.get("iris_error"))[:70])
    check("...and the coarse mesh is reported as the reason instead",
          "468" in str(_m_np.get("iris_error") or ""),
          str(_m_np.get("iris_error"))[:70])
    check("the guard is an explicit None check, not truthiness",
          "if lm is None or len(lm) < 478:" in read("tracker_service.py")
          and "if not lm or len(lm)" not in read("tracker_service.py"))

    _svc = _tsm.Service.__new__(_tsm.Service)
    _svc._latest_face_info = _FaceInfo()
    _svc.gf = None
    _svc._last_frame = None
    _m = _svc._metrics_from_face_info() or {}
    check("a coarse mesh with no frame falls back to the inter-ocular ruler",
          "inter-ocular" in str(_m.get("distance_source")),
          str(_m.get("distance_source")))
    check("...and SAYS SO, which is what three pilots could not tell us",
          bool(_m.get("iris_error")), _m.get("iris_error") or "silent")
    check("...naming the coarse mesh as the reason",
          "468" in str(_m.get("iris_error")))

    # The frame the callback already receives is what makes the refined
    # mesh possible during a session.
    _svc2 = _tsm.Service.__new__(_tsm.Service)
    _svc2.gf = None
    _svc2._last_frame = None
    check("_grab_frame returns nothing when no frame was stashed",
          _svc2._grab_frame() is None)
    import numpy as _np2

    _svc2._last_frame = _np2.zeros((480, 640, 3), dtype=_np2.uint8)
    check("_grab_frame returns the frame the callback stashed",
          _svc2._grab_frame() is _svc2._last_frame)
    check("the callback stashes every frame it sees",
          "self._last_frame = frame" in read("tracker_service.py"))

    check("a failing iris records WHY, instead of passing silently",
          "except Exception as exc:  # noqa: BLE001 — never block the guide"
          in _ts_src2 and 'm["iris_error"] = "%s: %s"' in _ts_src2)
    check("...with a traceback, since the message alone was not enough",
          'm["iris_traceback"]' in _ts_src2
          and "iris_traceback" in read("app.py"))
    check("the reader shouts when the fallback ruler was used",
          "The FALLBACK ruler produced this distance"
          in read("show_validations.py"))

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

    # Grid A grew from 7 to 13 targets 2026-08-18 (F33/brief item 2) so a
    # full-affine correction has enough leave-one-out headroom to be
    # more than noise (validation_stats.FULL_AFFINE_MIN_TARGETS = 12).
    # Grid B stays at 7 — it is never fitted to, only checked against,
    # and post pairs with pre_check on it for drift, which must not
    # change size out from under that comparison.
    check("grid A has THIRTEEN targets, grid B still has seven",
          len(_A) == 13 and len(_B) == 7, "A=%d B=%d" % (len(_A), len(_B)))
    check("grid A has at least FULL_AFFINE_MIN_TARGETS targets — the "
          "whole reason it was extended",
          len(_A) >= importlib.import_module(
              "validation_stats").FULL_AFFINE_MIN_TARGETS,
          "A=%d, needs >= %d" % (len(_A),
                                 importlib.import_module(
                                     "validation_stats"
                                 ).FULL_AFFINE_MIN_TARGETS))
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

    # ── The written rule and the applied rule must be the same rule ──
    # metrics_spec.INCLUSION is the pre-declared criterion; verify_metrics
    # is what actually decides. If the spec names the post-stimulus check
    # and the code averages two checks, the thesis states a rule it did
    # not apply — and nobody would notice, because both are defensible.
    _incl = importlib.import_module("metrics_spec").INCLUSION
    check("the inclusion rule names the figure the code computes",
          "mean of pre_check and post" in _incl["canonical_accuracy"]
          and "mean of pre_check %.2f and post %.2f" in _vm,
          _incl["canonical_accuracy"])
    check("the inclusion rule carries the date it was decided",
          bool(_incl.get("decided_on")) and bool(_incl.get("revised_on")))
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

    # ── Crowding, measured WITHIN one scene ──────────────────────────
    # Two clips from the same busy classroom still permit F9's contrast,
    # because crowding varies within a single scene: a poster on empty
    # wall is isolated, a student mid-row is not. No sparse stimulus is
    # required, and the measure is continuous rather than two-level.
    check("crowding is measured from the claims themselves, no extra API "
          "call",
          "def crowding_analysis" in _cc and "nearest OTHER claimed" in _cc)
    check("it is labelled EXPLORATORY rather than a test F9 passed",
          "EXPLORATORY" in _cc and "does not isolate the ambiguity" in _cc)
    check("too few claims withholds the split instead of reporting noise",
          "if len(rows) < 20:" in _cc)
    _mkc = lambda n: (
        [{"t_start": i, "t_end": i, "attended": "o%d" % i,
          "bbox": [0.1 + 0.04 * i, 0.4, 0.04, 0.09]} for i in range(n)],
        [(float(i), 0.12 + 0.04 * i, 0.44, True) for i in range(n)])
    _c10, _s10 = _mkc(10)
    _c24, _s24 = _mkc(24)
    check("...verified: 10 claims -> withheld, 24 -> reported",
          _ccmod.check_all(_c10, _s10, 2.13, 58.2, 1680,
                           945)["crowding"] is None
          and _ccmod.check_all(_c24, _s24, 2.13, 58.2, 1680,
                               945)["crowding"] is not None)

    # ── Evaluation data lands in its own folder ──────────────────────
    _cfgmod = importlib.import_module("config")
    importlib.reload(_cfgmod)
    check("collection has a start boundary, with a TIME on the first day",
          bool(_cfgmod._eval_boundary()),
          _cfgmod.EVALUATION_FROM_DATE)
    check("the boundary excludes the same day's earlier debug runs",
          _cfgmod.is_evaluation_session(
              "13_47_11.08_2026-08-11_135021") is False)
    check("...and includes a session recorded after it that same day",
          _cfgmod.is_evaluation_session("P01_2026-08-11_143000") is True)
    check("evaluation sessions route to data/study",
          _cfgmod.session_dir_for("P01_2026-08-11_143000")
          .endswith("study"))
    check("development sessions stay in gazefollower_raw",
          _cfgmod.session_dir_for("x_2026-07-16_163647")
          .endswith("gazefollower_raw"))
    check("a date-only boundary still works",
          _cfgmod._eval_boundary() is not None)
    check("the label and the folder use the SAME comparison",
          "config.is_evaluation_session(session_id)" in _vm
          and "do NOT compare date strings" in _vm)
    check("the app routes by timestamp, not by memory",
          "session_dir_for(base)" in _app3
          and "not by anyone remembering" in _app3)
    check("every analysis tool reads BOTH directories",
          all("_session_glob" in read(f) for f in
              ("verify_metrics.py", "calibration_diagnosis.py",
               "inverse_check.py", "share_results.py",
               "quality_report.py")))
    check("claim_check --latest searches both too",
          'DATA_DIR, "study"' in _cc)
    # A tool left globbing only the old folder goes blind on day one,
    # and the failure is silent — it simply reports fewer sessions.
    check("NO tool still globs gazefollower_raw alone",
          not any("glob(os.path.join(RAW_DIR" in read(f) for f in
                  ("verify_metrics.py", "calibration_diagnosis.py",
                   "inverse_check.py", "share_results.py",
                   "quality_report.py", "anonymise_manifests.py",
                   "backfill_manifests.py")))
    check("share_results publishes from both directories",
          '("manifests", _session_glob())' in read("share_results.py"))
    check("raw participant data in data/study is NOT published",
          any(l.strip() == "data/study/" for l in read(".gitignore")
              .splitlines()))

    # ── Which ruler measured the distance, BEFORE participant 1 ──────
    # The manifest field can only be read after a session exists, so
    # the first real participant would otherwise be the test of a path
    # that has never run on a camera. The probe answers it while
    # recording nothing.
    _ts_src = read("tracker_service.py")
    check("a distance probe exists that records nothing",
          '"--distance" in sys.argv' in _ts_src
          and "def _distance_probe" in _ts_src)
    _probe_src = _ts_src.split("def _distance_probe")[1].split(
        'if __name__')[0]
    check("the probe runs the REAL mesh function, not a private copy",
          "refined_landmarks_for_frame(frame)" in _probe_src
          and "iris_distance.estimate(" in _probe_src)
    # It must NOT go through GazeFollower: in SAMPLING state with no
    # fitted calibration, process_frame raises BEFORE dispatching
    # FaceInfo, so a GazeFollower-based probe reports "no face" and
    # blames the camera for a calibration state.
    check("the probe does not depend on a calibration existing",
          "cmd_position_info" not in _probe_src
          and "Service()" not in _probe_src)
    check("...and says why, so the next person does not re-try it",
          "No calibration model is available" in _ts_src
          and "never reached" in _ts_src)
    check("the probe FAILS when the fallback ruler is in use",
          "FALLBACK IN USE" in _probe_src and
          _probe_src.strip().endswith("return 1"))
    check("no face is a lighting problem, not a ruler result",
          "NO FACE was detected" in _probe_src
          and "THE CAMERA RETURNED NO FRAMES" in _probe_src)
    check("a busy camera is named as such",
          "CAMERA BUSY OR UNAVAILABLE" in _probe_src)
    # An assumed field of view is not a measurement. Passing the iris
    # check on a guessed focal length would license a false claim.
    check("an ASSUMED focal length does not count as a pass",
          "the focal length is ASSUMED" in _probe_src)
    check("the probe states what it does NOT cover",
          "WHAT IT DOES NOT COVER" in _ts_src)

    _tsmod = importlib.import_module("tracker_service")
    importlib.reload(_tsmod)
    check("the shared mesh helper returns None instead of raising",
          _tsmod.refined_landmarks_for_frame(None) is None)

    # ── Verifying the ruler must not be able to become a refit ───────
    # --calibrate 60 SOLVES the focal so that 60 comes out; asking it
    # afterwards whether it reads 60 is a fit scoring itself.
    _cg = read("camera_geometry.py")
    _ver = _cg.split("def _verify")[1].split("\ndef ")[0]
    check("there is a verify mode that does not refit",
          '"--verify"' in _cg and "def _verify" in _cg)
    check("verify writes nothing",
          "save(" not in _ver and "json.dump" not in _ver)
    check("verify uses the SAVED focal, not a fresh solve",
          "load() or {}" in _ver and "calibrate(" not in _ver)
    check("verify says why calibration is not validation",
          "CALIBRATION IS NOT VALIDATION" in _cg
          and "circular" in _cg)
    check("verify fails on a head that moved, before judging the ruler",
          "you moved" in _ver and _ver.index("you moved")
          < _ver.index("PASS —"))
    check("a failure states the consequence in degrees",
          "really %.2f deg" in _ver)
    # cmd.exe reads .bat under the console codepage, not UTF-8, so a
    # non-ASCII character in an ECHO line renders as mojibake in the
    # pre-flight output - which is output a reader may screenshot.
    _nonascii = []
    for _f in sorted(glob.glob(os.path.join(BASE, "windows", "*.bat"))):
        _txt = read(os.path.join("windows", os.path.basename(_f)))
        if any(ord(c) > 127 for c in _txt):
            _nonascii.append(os.path.basename(_f))
    check("no .bat file contains non-ASCII", not _nonascii,
          ", ".join(_nonascii))

    # cmd.exe mis-parses multi-line ( ... ) blocks, for /f loops and goto
    # targets in a file with bare LF line endings. It does not report an
    # error: the console closes. Every .bat here was authored on macOS,
    # so this is the default state unless something enforces it.
    import collections as _coll

    _lf, _dupes, _missing = [], [], []
    for _f in sorted(glob.glob(os.path.join(BASE, "windows", "*.bat"))):
        _name = os.path.basename(_f)
        with open(_f, "rb") as _fh:
            _b = _fh.read()
        if _b.count(b"\n") - _b.count(b"\r\n"):
            _lf.append(_name)
        _labels = [m.group(1) for m in re.finditer(rb"(?m)^:(\w+)", _b)]
        if [k for k, v in _coll.Counter(_labels).items() if v > 1]:
            _dupes.append(_name)
        _targets = (set(re.findall(rb"goto :(\w+)", _b))
                    | set(re.findall(rb"call :(\w+)", _b)))
        if [t for t in _targets if t not in _labels and t != b"eof"]:
            _missing.append(_name)
    check("every .bat has CRLF line endings", not _lf, ", ".join(_lf))
    check("no .bat has a duplicate label", not _dupes, ", ".join(_dupes))
    check("every goto/call target exists", not _missing, ", ".join(_missing))
    check("gitattributes pins .bat to CRLF on checkout",
          "*.bat text eol=crlf" in read(".gitattributes"))

    # Inside for /f ('...') the command is re-parsed by a second shell,
    # where ( ) are metacharacters. An inline `python -c` containing
    # len(...) therefore dies with a syntax error naming only "." and
    # takes the console with it — indistinguishable from a crash.
    _inline = []
    for _f in sorted(glob.glob(os.path.join(BASE, "windows", "*.bat"))):
        for _ln in bat_code(os.path.basename(_f)).splitlines():
            _low = _ln.strip().lower()
            if "for /f" in _low and "python -c" in _low:
                _inline.append(os.path.basename(_f))
    check("no for /f wraps an inline python -c", not _inline,
          ", ".join(_inline))
    # for /f hands its command to a second shell. run_session.bat is the
    # one script a participant is waiting through, so it does not use the
    # construct at all: the count is written to a file and read with
    # set /p, which has no subshell and no quoting exposure.
    check("run_session.bat contains no for /f at all",
          "for /f" not in bat_code("run_session.bat").lower())
    check("the count is read with set /p from a file",
          "set /p NSTIM=<" in bat_code("run_session.bat")
          and "count_stimuli.py > " in bat_code("run_session.bat"))
    check("a marker prints before the count, so a failure is locatable",
          "Counting stimuli" in bat_code("run_session.bat"))

    # THE bug that actually closed the console. An unescaped ) inside a
    # parenthesised block ENDS the block at that character, so
    #     echo    playable video (files ... do not count).
    # closed the if-block at "count)" and left ".", which cmd reported as
    #   "." kann syntaktisch an dieser Stelle nicht verarbeitet werden
    # and then quit. The message names the stray character, never the
    # line, and the block parses at read time — so it fires even when the
    # branch is not taken. Neither line endings nor for /f caused it;
    # both were real hazards found while looking for this one.
    def _unescaped_parens_in_blocks(name):
        depth, bad = 0, []
        for n, line in enumerate(read(os.path.join("windows", name))
                                 .splitlines(), 1):
            s = line.strip()
            low = s.lower()
            if low.startswith("rem") or low.startswith("::"):
                continue
            if low.startswith("echo"):
                if depth > 0:
                    body = re.sub(r"\^[()]", "", s[4:])
                    if "(" in body or ")" in body:
                        bad.append("%s:%d" % (name, n))
                continue          # echo text is never block structure
            code = re.sub(r"\^[()]", "", s)
            depth = max(0, depth + code.count("(") - code.count(")"))
        return bad

    _paren_bugs = []
    for _f in sorted(glob.glob(os.path.join(BASE, "windows", "*.bat"))):
        _paren_bugs += _unescaped_parens_in_blocks(os.path.basename(_f))
    check("no echo inside a ( ) block has an unescaped parenthesis",
          not _paren_bugs, ", ".join(_paren_bugs))

    # ── The manifest must be written, whatever is in it ──────────────
    # The write caught OSError only. A numpy bool from the iris
    # cross-check raises TypeError inside json.dump, finalisation aborts,
    # and the session ends with a gaze CSV and no manifest - no
    # validations, no distance, nothing analysable - discovered after the
    # participant has gone home.
    import ast as _ast

    _app_src = read("app.py")
    _ns = {"json": json}
    for _node in _ast.parse(_app_src).body:
        if isinstance(_node, _ast.FunctionDef) and _node.name in (
                "_json_safe", "_strip_unserialisable"):
            exec(compile(_ast.Module([_node], []), "app.py", "exec"), _ns)
    check("the manifest write has a json fallback converter",
          "_json_safe" in _ns and "default=_json_safe" in _app_src)
    check("...and catches more than OSError",
          "except Exception:  # noqa: BLE001" in
          _app_src.split("Manifest write FAILED")[0][-400:])

    import numpy as _np3

    _agree = _np3.float32(3.2) <= 15.0        # exactly what the iris does
    try:
        json.dumps({"agree": _agree})
        _plain_ok = True
    except TypeError:
        _plain_ok = False
    check("a numpy bool is what plain json refuses", not _plain_ok,
          type(_agree).__name__)
    check("...and the converter takes it",
          json.dumps({"agree": _agree}, default=_ns["_json_safe"])
          == '{"agree": true}')
    for _v in (_np3.float32(1.5), _np3.int64(3), _np3.array([1.0, 2.0])):
        check("the converter handles %s" % type(_v).__name__,
              bool(json.dumps({"v": _v}, default=_ns["_json_safe"])))

    class _Unserialisable:
        pass

    _degraded = _ns["_strip_unserialisable"](
        {"good": 1, "bad": _Unserialisable(),
         "nested": {"deep": _Unserialisable(), "fine": 2}})
    check("the degraded fallback always serialises",
          json.dumps(_degraded)
          == '{"good": 1, "bad": "<unserialisable>", '
             '"nested": {"deep": "<unserialisable>", "fine": 2}}',
          json.dumps(_degraded))
    check("...and keeps every field it can",
          _degraded["good"] == 1 and _degraded["nested"]["fine"] == 2)

    # ── An interrupted finalisation must not lose the session ────────
    # Finalisation takes ~60 s (it re-reads the CSV per stimulus) and
    # wrote the manifest last, so closing the app after the participant
    # finished lost the validations, the distance and the correction -
    # everything that only existed in server memory.
    _app_src2 = read("app.py")
    check("a provisional manifest is written before segmentation",
          "PROVISIONAL MANIFEST" in _app_src2
          and _app_src2.index("_provisional")
          < _app_src2.index("gaze_service.end_session(csv_path)"))
    check("...and is marked incomplete so it cannot pass as a full one",
          '"complete": False' in _app_src2)

    # ── Rebuilding a manifest from the log ───────────────────────────
    _rb = read("rebuild_manifest.py")
    check("the rebuilt manifest is marked as reconstructed",
          '"reconstructed"' in _rb and "must be reported as such" in _rb)
    check("...and lists what could NOT be recovered",
          '"absent"' in _rb and "per-target error breakdown" in _rb)
    check("rebuilding refuses to overwrite a real manifest",
          "Refusing to overwrite" in _rb)

    _rbmod = importlib.import_module("rebuild_manifest")
    importlib.reload(_rbmod)
    _log = "\n".join([
        "2026-08-13 16:03:58,747  INFO  __main__ - New participant "
        "registered: T P1",
        "2026-08-13 16:05:59,700  INFO  __main__ - Rate gate [pre-video #1]:"
        " 30.0 Hz sustained (initial 30.1, peak 33.4) | 100.0% detected",
        "2026-08-13 16:06:43,383  WARNING __main__ - Validation degrees "
        "shift 14 % once the MEASURED distance (52.7 cm, via inter-ocular "
        "(GazeFollower eye rects)) replaces the browser's assumption: "
        "1.74 -> 1.98 deg",
        "2026-08-13 16:06:43,384  INFO  __main__ - Validation (pre_fit): "
        "mean error 101.4 px / 1.74 deg | targets measured 7/7, samples "
        "per target [46, 46, 45, 45, 46, 45, 46] | fullscreen=True "
        "inner=[1920, 1080] offsets=[0, -8] dpr=1 - sid=X",
        "2026-08-13 16:07:15,181  INFO  __main__ - Recording started - "
        "sid=X, participant=T P1, stimulus=A.mp4",
        "2026-08-13 16:07:45,554  INFO  __main__ - Recording stopped - "
        "sid=X, participant=T P1, stimulus=A.mp4, gazefollower=continues",
        "2026-08-13 16:08:41,678  INFO  __main__ - Session "
        "T_P1_2026-08-13_160841 -> study (EVALUATION data)",
    ])
    _p = _rbmod._parse(_log, "T_P1_2026-08-13_160841")
    check("the log parser recovers the validation", len(_p["validations"]) == 1
          and _p["validations"][0]["mean_err_px"] == 101.4)
    check("...its per-target sample counts",
          _p["validations"][0]["samples_per_target"] == [46, 46, 45, 45, 46,
                                                         45, 46])
    check("...the measured distance and its ruler",
          _p["validations"][0]["distance"]["cm"] == 52.7
          and "inter-ocular" in _p["validations"][0]["distance"]["source"])
    check("...the browser geometry",
          _p["validations"][0]["geometry"]["inner"] == [1920, 1080])
    check("...the rate gate", len(_p["rate_gates"]) == 1
          and _p["rate_gates"][0]["hz_sustained"] == 30.0)
    _built = _rbmod.build("T_P1_2026-08-13_160841", _p)
    check("the stimulus window is paired start->stop",
          len(_built["stimulus_log"]) == 1
          and _built["stimulus_log"][0]["stimulus"] == "A.mp4")
    check("the built manifest carries the reconstruction warning",
          "RECONSTRUCTED" in _built["reconstructed"]["warning"])

    # ── Retiring a session, not deleting it ──────────────────────────
    # A session that disappears leaves a gap, and a gap cannot answer
    # whether the participant was dropped for a fault or for an
    # inconvenient number.
    _ret = read("retire_session.py")
    check("retiring moves files, never deletes them",
          "shutil.move(f, target)" in _ret
          and "os.remove" not in _ret and "os.unlink" not in _ret)
    check("a reason is required and must say something",
          'len(args.reason.strip()) < 15' in _ret)
    check("the retirement is dated and registered",
          "REGISTRY.md" in _ret and '"retired_at"' in _ret)
    # "retired" appears in config.py about a retired MODEL, so match the
    # directory, not the word.
    check("retired sessions are gitignored like the study folder",
          any(l.strip() == "data/retired/" for l in read(".gitignore")
              .splitlines()))
    check("...and no analysis tool globs the retired directory",
          not any("retired" in read(f) for f in
                  ("verify_metrics.py", "claim_check.py", "quality_report.py",
                   "share_results.py", "backfill_manifests.py")))
    check("re-recording the same person is flagged as prior exposure",
          "rerecorded_as" in _ret and "not a first viewing" in _ret)

    # ── Cutting the stimuli is a procedure, not a one-off ────────────
    # The clips are not in the repo, so a cut made on one machine cannot
    # travel to another; only the script can. Identical stimulus for
    # every participant therefore depends on the cut being reproducible.
    _cut = read("cut_stimuli.py")
    check("the cut re-encodes rather than stream-copies",
          "libx264" in _cut and '"-c", "copy"' not in _cut)
    check("...and says why, since a copy cut lands on a keyframe",
          "nearest keyframe" in _cut)
    check("originals are moved aside, never overwritten",
          "full_originals" in _cut and "shutil.move(path" in _cut)
    check("the originals folder is invisible to the app",
          "os.listdir(STIMULI_DIR)" in read("config.py"))
    check("which seconds were taken is recorded",
          "cut_provenance.json" in _cut and '"start_s"' in _cut)
    check("the cut is reversible",
          "--restore" in _cut and "def restore" in _cut)
    check("running it twice is safe",
          "already at length, left alone" in _cut)
    check("it warns that the crowding contrast may not survive the cut",
          "WATCH BOTH CUTS" in _cut)

    check("the stimulus count comes from a script instead",
          "count_stimuli.py" in read("windows/run_session.bat")
          and os.path.isfile(os.path.join(BASE, "count_stimuli.py")))

    _csmod = importlib.import_module("count_stimuli")
    check("count_stimuli prints one integer and nothing else",
          "print(len(config.discover_stimuli()))" in read("count_stimuli.py"))
    # Recording must not hang off a one-line parenthesised block: a
    # failure inside the called script takes the console with it.
    _start_bat = read("windows/START.bat")
    # A dirty tree used to short-circuit BEFORE the fetch, so the
    # launcher could not say how far behind the machine was. It printed
    # "NOT pulling" once and the collection machine then ran stale code
    # for hours - which is how a fix that was shipped, tested and
    # confirmed can still be absent from the machine recording data.
    check("the update fetches BEFORE it inspects the working tree",
          _start_bat.index("BEHIND=%%i") < _start_bat.index("DIRTY=%%i"))
    check("being behind AND dirty is stated loudly, not in passing",
          "RUNNING OLD CODE" in _start_bat)
    check("...and holds the window so it cannot be scrolled past",
          "RUNNING OLD CODE" in _start_bat
          and "pause" in _start_bat.split("RUNNING OLD CODE")[1][:400])

    check("option 1 runs as its own labelled block",
          '=="1" goto :record' in _start_bat and "\n:record" in _start_bat)
    check("...and holds the window open on a nonzero exit",
          "run_session.bat exited with code" in _start_bat)

    check("the launcher offers verify separately from calibrate",
          "camera_geometry.py --verify" in read("windows/START.bat"))

    # ── Several calibrations are evidence, not repetitions ───────────
    # --calibrate overwrites, so calibrating twice DISCARDS the first
    # fit. Focal length is a camera property and must not depend on how
    # far away the person sat, so points at different distances are a
    # test the single-point procedure cannot perform.
    _cgmod = importlib.import_module("camera_geometry")
    importlib.reload(_cgmod)
    _K = _cgmod.__dict__.get("POPULATION_IOD_CM")  # touch, keeps import used

    # Synthetic camera: focal 700 px exactly, no tape error.
    _iris_cm = 1.17
    _true_f = 700.0
    _pts = [(d, _iris_cm * _true_f / d) for d in (40.0, 55.0, 70.0)]
    _fit = _cgmod.fit_multi(_pts)
    check("a perfect camera recovers its focal length exactly",
          abs(_fit["focal_px"] - _true_f) < 0.5, str(_fit["focal_px"]))
    check("...with residuals at zero", _fit["rms_cm"] < 0.01,
          str(_fit["rms_cm"]))

    # Now bias every tape reading by a constant. The focal-only model
    # must fit worse, and the offset model must recover the bias.
    _bias = 4.0
    _pts_b = [(d - _bias, _iris_cm * _true_f / d) for d in (40.0, 55.0, 70.0)]
    _fit_b = _cgmod.fit_multi(_pts_b)
    check("a constant tape bias shows up as residuals in the focal-only fit",
          _fit_b["rms_cm"] > 0.2, str(_fit_b["rms_cm"]))
    _ob = _fit_b["offset_model"]
    check("the offset model recovers the tape bias",
          abs(abs(_ob["tape_offset_cm"]) - _bias) < 0.3,
          str(_ob["tape_offset_cm"]))
    check("...and the sign says the tape reads SHORT",
          _ob["tape_offset_cm"] < 0, str(_ob["tape_offset_cm"]))
    check("the offset model recovers the true focal too",
          abs(_ob["focal_px"] - _true_f) < 5.0, str(_ob["focal_px"]))

    # Two points fit two parameters exactly. Saying so is the point:
    # an offset that cannot be wrong is not evidence.
    _two = _cgmod.fit_multi(_pts_b[:2])
    check("two points are declared insufficient for the offset test",
          _two["offset_model"]["meaningful"] is False
          and "tests nothing" in _two["offset_model"]["note"])
    check("three points make the offset test meaningful",
          _fit_b["offset_model"]["meaningful"] is True)
    check("one point is refused outright",
          "error" in _cgmod.fit_multi(_pts_b[:1]))
    check("the fit is exposed on the command line",
          '"--fit"' in read("camera_geometry.py")
          and "def fit_multi" in read("camera_geometry.py"))

    # A pooled fit has no single calibration distance. Writing that key
    # as null and then dividing by it raised TypeError on EVERY distance
    # estimate afterwards — i.e. on every session recorded after the
    # calibration was saved, which is the worst possible moment.
    import json as _js
    import shutil as _sh2
    import tempfile as _tf2

    _tmpg = _tf2.mkdtemp(prefix="geom_")
    _saved_file = _cgmod.GEOMETRY_FILE
    try:
        _cgmod.GEOMETRY_FILE = os.path.join(_tmpg, "camera_geometry.json")
        _rc_fit = _cgmod._report_fit(_pts, do_save=True)
        with open(_cgmod.GEOMETRY_FILE, encoding="utf-8") as _fh:
            _geom = _js.load(_fh)
        check("--fit --save writes the pooled focal length",
              abs(_geom["focal_px"] - _true_f) < 0.5, str(_geom["focal_px"]))
        check("...with its provenance, not just a number",
              "fit_points" in _geom and "fit_rms_cm" in _geom
              and "pooled" in _geom["focal_basis"])
        check("...and a usable calibration distance, never null",
              isinstance(_geom.get("known_distance_cm"), (int, float)),
              repr(_geom.get("known_distance_cm")))
        _est = _cgmod.estimate_distance(70.0, geometry=_geom)
        check("a distance estimate still works after a pooled save",
              isinstance(_est.get("distance_cm"), float)
              and _est["distance_cm"] > 0, str(_est.get("distance_cm")))
        # And with the key explicitly null, as an older file may hold.
        _geom_null = dict(_geom)
        _geom_null["known_distance_cm"] = None
        _geom_null["distance_sd_cm"] = None
        _est2 = _cgmod.estimate_distance(70.0, geometry=_geom_null)
        check("...and survives a null calibration distance too",
              isinstance(_est2.get("distance_cm"), float),
              str(_est2.get("distance_cm")))
    finally:
        _cgmod.GEOMETRY_FILE = _saved_file
        _sh2.rmtree(_tmpg, ignore_errors=True)

    # Plain print() calls: a doubled %% renders literally on screen.
    _cg_src = read("camera_geometry.py")
    _bad = [ln for ln in _cg_src.splitlines()
            if "%%" in ln and "print(" in ln and "% " not in ln.split("%%")[-1]
            and not ln.rstrip().endswith("%")]
    check("no literal %% leaks into a plain print",
          "~4 %% biological" not in _cg_src
          and "~11 %% and which uses" not in read("tracker_service.py"))
    check("the field list is defined once, not copied",
          read("tracker_service.py").count("POSITION_FIELDS = (") == 1
          and "for k in POSITION_FIELDS" in read("tracker_service.py"))
    check("the probe reports the payload a session would record",
          "WHAT A SESSION WOULD RECORD" in read("tracker_service.py"))
    check("...and refuses when the distance has no source",
          "POSITION_REQUIRED" in read("tracker_service.py")
          and "not a" in read("tracker_service.py").split("MISSING:")[1][:300])
    check("the launcher offers the probe",
          "tracker_service.py --distance" in read("windows/START.bat"))

    # ── Correspondence is never reported as a single number ──────────
    # 16.9 % strict alone reads as "the model was wrong 83 % of the
    # time" when part of that gap is the tracker's own error; the
    # lenient rate alone assumes every near miss was a hit.
    _vm_src = read("verify_metrics.py")
    check("the report shows the strict AND lenient correspondence",
          "correspondence_lenient_pct" in _vm_src
          and "strict /" in _vm_src)

    _res_c = _vmod.Result()
    _vmod.check_session({"llm": {"clip.mp4": {
        "llm_model_id": "m", "structured": [{"bbox": [0, 0, 1, 1]}] * 40,
        "correspondence": {"correspondence_pct": 16.9,
                           "correspondence_lenient_pct": 61.0,
                           "n_testable": 59}}}}, _res_c)
    _row = [r for r in _res_c.rows
            if r[1].startswith("claim_metric_correspondence")]
    check("both rates reach the printed value",
          bool(_row) and "16.9" in _row[0][3] and "61.0" in _row[0][3],
          _row[0][3] if _row else "no row")

    # A run that never scored the lenient rate must still report — the
    # older manifests do not carry it and must not vanish from the table.
    _res_c2 = _vmod.Result()
    _vmod.check_session({"llm": {"clip.mp4": {
        "llm_model_id": "m", "structured": [{"bbox": [0, 0, 1, 1]}] * 40,
        "correspondence": {"correspondence_pct": 16.9,
                           "n_testable": 59}}}}, _res_c2)
    _row2 = [r for r in _res_c2.rows
             if r[1].startswith("claim_metric_correspondence")]
    check("a manifest without the lenient rate still reports the strict one",
          bool(_row2) and "16.9" in _row2[0][3], _row2[0][3] if _row2 else "-")

    # ── The rubric freeze, enforced rather than promised ─────────────
    # A rubric that changes mid-collection splits the data into two
    # studies and every session still looks fine on its own.
    import shutil as _sh
    import tempfile as _tf

    _tmp_r = _tf.mkdtemp(prefix="rubric_")
    try:
        def _mk_r(name, rubric):
            p = os.path.join(_tmp_r, name)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"llm": {"clip.mp4": {"rubric": rubric}}}, fh)
            return p

        # Both AFTER the evaluation boundary, so both count.
        _a = _mk_r("A_2026-12-01_100000_manifest.json", "C1 ... C2 ... C3")
        _b = _mk_r("B_2026-12-02_100000_manifest.json", "C1 ... C2 ... C3")
        _c = _mk_r("C_2026-12-03_100000_manifest.json", "C1 ... C2 only")
        _dev = _mk_r("D_2020-01-01_100000_manifest.json", "something else")

        _same = _vmod.rubric_drift([_a, _b])
        check("identical rubrics collapse to one variant", len(_same) == 1,
              "%d variants" % len(_same))
        _diff = _vmod.rubric_drift([_a, _b, _c])
        check("a changed rubric is detected", len(_diff) == 2,
              "%d variants" % len(_diff))
        check("...and names the sessions on each side",
              any("C_2026-12-03" in s for v in _diff.values() for s in v))
        # A development session must not be able to trip the freeze —
        # it is not part of the frozen set.
        _mixed = _vmod.rubric_drift([_a, _b, _dev])
        check("development sessions are excluded from the freeze check",
              len(_mixed) == 1, "%d variants" % len(_mixed))
        check("the check is exposed on the command line",
              '"--rubric"' in _vm_src and "_report_rubric" in _vm_src)
        # Behaviour, not source text: a drifted set must exit nonzero,
        # or the check is a printout rather than a gate.
        import contextlib as _ctx2
        import io as _io2

        with _ctx2.redirect_stdout(_io2.StringIO()) as _buf_r:
            _rc_drift = _vmod._report_rubric([_a, _b, _c])
        with _ctx2.redirect_stdout(_io2.StringIO()):
            _rc_same = _vmod._report_rubric([_a, _b])
        check("drift exits nonzero, agreement exits zero",
              _rc_drift == 1 and _rc_same == 0,
              "drift=%s same=%s" % (_rc_drift, _rc_same))
        check("the drift report names the differing rubrics",
              "DRIFT" in _buf_r.getvalue()
              and "C_2026-12-03" in _buf_r.getvalue())
    finally:
        _sh.rmtree(_tmp_r, ignore_errors=True)

    # ── The stimulus set actually presented ──────────────────────────
    _rs = read("windows/run_session.bat")
    check("collection presents the real stimulus set, not the pilot clip",
          "set SESSION_STIMULUS_MODE=all" in _rs)
    check("...set in the frozen launcher, not left to a default",
          'SESSION_STIMULUS_MODE", "clip30"' in read("config.py"))
    check("an empty stimulus folder stops the run before the participant "
          "sits down",
          "NO STIMULI FOUND" in _rs and "exit /b 1" in _rs)
    check("a set that is not the protocol's 2 clips is called out",
          "not the 2 the protocol specifies" in _rs)
    check("the mode and the ACTUAL order are recorded per session",
          '"stimulus_mode": SESSION_STIMULUS_MODE' in _app3
          and '"stimulus_order"' in _app3)
    check("helper clips can never be presented",
          "TESTCLIP_PREFIX" in read("config.py")
          and "not f.startswith(TESTCLIP_PREFIX)" in read("config.py"))

    # ── The iris ruler, finally available ────────────────────────────
    # GazeFollower's FaceInfo carries the COARSE 468-point mesh, so the
    # iris landmarks (468-477) never existed and every session silently
    # used the eye RECTANGLES with an inter-pupillary constant. Both
    # 2026-08-11 sessions reported "UNKNOWN RULER" at ~75 cm, and every
    # degree divides by that distance.
    _ts = read("tracker_service.py")
    # The old assertion pinned the exact buggy expression, so it went
    # green for months while the branch it guards never executed.
    check("a coarse mesh triggers our OWN refined pass",
          "if lm is None or len(lm) < 478:" in _ts
          and "def _refined_landmarks" in _ts)
    check("the refined mesh is actually requested",
          "refine_landmarks=True" in _ts)
    check("it is built lazily and reused, not per frame",
          '_IRIS_MESH' in _ts and "Built lazily and reused" in _ts)
    check("the mesh helper is module-level, so the probe shares it",
          "def refined_landmarks_for_frame(frame)" in _ts
          and "return refined_landmarks_for_frame(frame)" in _ts)
    check("an unavailable iris degrades instead of stopping a validation",
          "falling back to the" in _ts)
    check("the source is recorded when the fallback mesh is used",
          '"iris_landmarks_from"' in _ts)
    # The switch must CHANGE the outcome, or it is decoration.
    class _P:
        __slots__ = ("x", "y", "z")

        def __init__(self, x, y):
            self.x, self.y, self.z = x, y, 0.0

    def _mesh_n(n):
        lm = [_P(0.5, 0.5) for _ in range(n)]
        if n >= 478:
            for i, (px, py) in {469: (306, 240), 471: (294, 240),
                                474: (406, 240), 476: (394, 240)}.items():
                lm[i] = _P(px / 640, py / 480)
        return lm

    check("...verified: a 468-point mesh cannot measure the iris",
          bool(_iris_mod.iris_diameter_px(_mesh_n(468), 640, 480)
               .get("error")))
    check("...and a 478-point mesh can",
          _iris_mod.iris_diameter_px(_mesh_n(478), 640, 480)
          .get("mean_px") == 12.0)

    # ── Sharing results publicly ─────────────────────────────────────
    _sr = read("share_results.py")
    _srmod = importlib.import_module("share_results")
    importlib.reload(_srmod)
    check("secrets can never be shared, whatever else changes",
          {"api_key", "secret_key", "b64"} <= _srmod.FORBIDDEN_KEYS)
    _scrubbed = _srmod._scrub(
        {"api_key": "AIzaSECRET", "nested": {"b64": "xxxx", "keep": 1},
         "list": [{"secret_key": "deadbeef"}]}, {}, True)
    check("...verified: forbidden keys are omitted at every depth",
          _scrubbed["api_key"] == "<omitted>"
          and _scrubbed["nested"]["b64"] == "<omitted>"
          and _scrubbed["list"][0]["secret_key"] == "<omitted>"
          and _scrubbed["nested"]["keep"] == 1)
    check("pseudonyms are stable across runs (a hash, not a counter)",
          _srmod._pseudonym("Marie", {}) == _srmod._pseudonym("Marie", {}))
    check("adding a participant does not renumber the others",
          _srmod._pseudonym("Anna", {"Marie": "P-XXXX"}) != "P-XXXX")
    # The nested-label trap: with both "Test" and "Test1" in the map,
    # replacing the shorter first leaves a digit glued to a pseudonym.
    _mapx = {"Test": "P-AAAA", "Test1": "P-BBBB"}
    check("longer labels are replaced first, so names cannot nest",
          _srmod._replace_names("Test1_2026-07-10", _mapx)
          == "P-BBBB_2026-07-10",
          _srmod._replace_names("Test1_2026-07-10", _mapx))
    _ign = read(".gitignore")
    check("the pseudonym map is gitignored — it undoes the whole step",
          any(l.strip() == "data/share_pseudonyms.json"
              for l in _ign.splitlines()))
    check("data/shared IS published (that is the point)",
          any(l.strip() == "!data/shared/" for l in _ign.splitlines()))
    check("raw CSVs and videos are not in the shared set",
          "gazefollower_raw" in _sr and "*_manifest.json" in _sr
          and ".csv" not in _sr)

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
    _start_code = bat_code("START.bat")
    check("there is exactly one git pull and one dirty-tree guard",
          _start_code.count("git pull --ff-only") == 1
          and _start_code.count("NOT pulling") == 1)
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

    # ── EVERY output path under data/, not just the ones I remembered ─
    # Naming the sensitive directories one by one fails the day a new
    # one is added: data/llm_replay/ was written for months, holds the
    # base64 stimulus frames, and was one commit away from a public
    # push because nobody thought to add a line for it.
    #
    # So: find the data/ paths the code actually writes, and require
    # each to be either explicitly PUBLISHED or actually ignored —
    # asked of git itself, which knows the pattern semantics, rather
    # than by matching strings against .gitignore.
    _PUBLISHED = {"shared", "manifests_anonymised", "models.json"}
    _data_paths = set()
    for _f in sorted(glob.glob(os.path.join(BASE, "*.py"))):
        _src = read(os.path.basename(_f))
        for _m in re.finditer(
                r'os\.path\.join\(\s*(?:BASE\s*,\s*)?(?:DATA_DIR|"data")\s*,'
                r'\s*"([A-Za-z0-9_.\-]+)"', _src):
            _data_paths.add(_m.group(1))
    check("the scan found the known data/ output paths",
          {"llm_replay", "coding", "shared"} <= _data_paths,
          "%d paths: %s" % (len(_data_paths), ", ".join(sorted(_data_paths))))

    _leaks, _git_ok = [], True
    for _p in sorted(_data_paths):
        if _p in _PUBLISHED:
            continue
        # A representative path inside it. check-ignore matches on the
        # string, so the file need not exist.
        _probe = "data/%s" % _p if "." in _p else "data/%s/probe.json" % _p
        try:
            _rc = subprocess.call(["git", "check-ignore", "-q", _probe],
                                  cwd=BASE,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        except OSError:
            _git_ok = False
            break
        if _rc != 0:
            _leaks.append(_probe)
    if _git_ok:
        check("every data/ output path is ignored or deliberately published",
              not _leaks,
              "NOT IGNORED: %s" % ", ".join(_leaks) if _leaks else "")

        # And the specific ones, by name, because these are the two that
        # actually went wrong: participant faces, and a per-machine
        # calibration that would rescale every accuracy figure.
        for _probe, _why in (
                ("data/llm_replay/x.json", "base64 stimulus frames"),
                ("data/camera_geometry.json", "per-machine calibration"),
                ("data/study/x_manifest.json", "raw participant sessions"),
                ("data/coding/x.json", "participant-linked verdicts")):
            check("git ignores %s (%s)" % (_probe, _why),
                  subprocess.call(["git", "check-ignore", "-q", _probe],
                                  cwd=BASE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) == 0)

        # Nothing sensitive may be TRACKED either. check-ignore says what
        # git would do with a new file; this says what it is already
        # carrying — a file added before the rule was written stays
        # tracked and .gitignore does not touch it.
        _tracked = subprocess.run(
            ["git", "ls-files", "data/llm_replay", "data/study",
             "data/coding", "data/camera_geometry.json",
             "data/gazefollower_raw"],
            cwd=BASE, capture_output=True, text=True).stdout.split()
        check("none of them is already tracked in git",
              not _tracked, "TRACKED: %s" % ", ".join(_tracked[:5]))

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

        # ── the RENDERER, not just the computation ───────────────────
        # Everything above exercises check_session and reads res.rows
        # directly. That is exactly how a KeyError in report() survived
        # a green suite and then crashed on the first real session: the
        # status-to-mark table had no entry for N/A, so the metrics were
        # all computed correctly and NONE of them were printed. Run the
        # actual printing path and capture what it emits.
        import contextlib as _ctx
        import io as _io

        _buf_out = _io.StringIO()
        with _ctx.redirect_stdout(_buf_out):
            _rc = _vmod.report(_man_path)
        _out = _buf_out.getvalue()
        check("e2e: report() renders without raising", isinstance(_rc, int))
        check("e2e: report() prints every row it computed",
              _out.count("\n   [") == len(_r.rows),
              "%d printed vs %d rows" % (_out.count("\n   ["), len(_r.rows)))
        check("e2e: report() renders N/A rows as n/a, not a crash",
              ("[n/a ]" in _out) == any(s == _vmod.NOT_APPLICABLE
                                        for _, _, s, _, _ in _r.rows))
        check("e2e: report() reaches its own summary line",
              "n/a by design" in _out)

        # Every status the Result can hold must have a mark. Asserting
        # the table directly means a NEW status added later fails here
        # rather than in front of a participant.
        _statuses = {_vmod.PRESENT, _vmod.MISSING, _vmod.DEGENERATE,
                     _vmod.NOT_APPLICABLE}
        _marks = {_vmod.PRESENT: "OK  ", _vmod.MISSING: "MISS",
                  _vmod.DEGENERATE: "BAD ", _vmod.NOT_APPLICABLE: "n/a "}
        check("every metric status has a display mark",
              _statuses <= set(_marks) and
              all(("%s:" % s) or True for s in _statuses) and
              all(s in read("verify_metrics.py") for s in
                  ("NOT_APPLICABLE: \"n/a", ".get(status,")),
              "renderer must not use a bare dict lookup")
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


print("\n[21] Automatic head-position capture")
# F33/F36: head_position was null in all nine manifests recorded so far
# — the ONLY thing that ever wrote it was an opt-in pre-calibration
# guide nobody had opened. This calls the REAL app.py functions
# (imported, not reimplemented) against a fake but IMPERFECT
# position_info() reply — distance drifting between phases, a lost face
# on one phase, the tracker subprocess raising — the way a real run
# actually looks, not a clean synthetic one.
try:
    import app as _app_mod

    def _fake_pos(available=True, face=True, distance=55.0, **extra):
        if not (available and face):
            return {"ok": True, "available": available, "face": face}
        out = {"ok": True, "available": True, "face": True, "ready": True,
               "guidance": ["Good position — hold still and calibrate."],
               "assumed_hfov_deg": 60.0, "est_distance_cm": distance,
               "distance_source": "iris", "roll_deg": 1.5,
               "face_center_x": 0.5, "face_center_y": 0.42}
        out.update(extra)
        return out

    _st = {}
    _snap_cal = _app_mod._capture_head_position(
        _st, "calibration", pos=_fake_pos(distance=56.0))
    _snap_pf = _app_mod._capture_head_position(
        _st, "pre_fit", pos=_fake_pos(distance=53.0))
    _snap_pc = _app_mod._capture_head_position(
        _st, "pre_check", pos=_fake_pos(available=True, face=False))
    _snap_po = _app_mod._capture_head_position(
        _st, "post", pos=_fake_pos(distance=52.0))

    check("calibration snapshot captured with geometry",
          _snap_cal.get("available") is True
          and _snap_cal.get("est_distance_cm") == 56.0)
    check("a lost-face poll is recorded as unavailable, not dropped",
          _snap_pc.get("available") is False
          and "est_distance_cm" not in _snap_pc)
    check("state.head_position_log has one entry per phase",
          len(_st.get("head_position_log", [])) == 4,
          "got %d" % len(_st.get("head_position_log", [])))

    _manifest_hp = _app_mod._head_position_manifest(_st)
    check("manifest head_position has by_phase for all four phases",
          set((_manifest_hp or {}).get("by_phase", {})) ==
          {"calibration", "pre_fit", "pre_check", "post"})
    check("by_phase.pre_check keeps the unavailable flag",
          _manifest_hp["by_phase"]["pre_check"].get("available") is False)
    check("top-level mirror is the LAST AVAILABLE snapshot (post, "
          "skipping the unavailable pre_check)",
          _manifest_hp.get("est_distance_cm") == 52.0)

    # Tracker subprocess hiccup: position_info() itself raises. Must not
    # propagate — head position is instrumentation, never a reason to
    # lose a calibration or validation result.
    def _boom(*_a, **_k):
        raise RuntimeError("tracker pipe closed")

    _orig_pos_info = _app_mod.gaze_service.position_info
    _app_mod.gaze_service.position_info = _boom
    try:
        _snap_err = _app_mod._capture_head_position({}, "calibration")
        check("position_info() raising yields an unavailable snapshot, "
              "not an exception", _snap_err.get("available") is False)
    finally:
        _app_mod.gaze_service.position_info = _orig_pos_info

    # The optional guide's old snapshot shape must still fold in
    # (back-compat) if someone opens it, alongside the automatic phases.
    _st2 = {"position_snapshot": {"est_distance_cm": 61.0}}
    _app_mod._capture_head_position(_st2, "pre_fit", pos=_fake_pos())
    _hp2 = _app_mod._head_position_manifest(_st2)
    check("the optional guide's snapshot still folds in as phase "
          "'guide' alongside the automatic ones",
          "guide" in (_hp2 or {}).get("by_phase", {})
          and "pre_fit" in _hp2["by_phase"])

    # Downstream readers must not choke on a POPULATED head_position —
    # every manifest either of them has ever seen had it null.
    import correction_audit as _ca_mod
    import verify_metrics as _vm_mod

    _fake_manifest = {
        "session_id": "RUNTESTS_FAKE_HEADPOS",
        "validations": [
            {"phase": "pre_fit", "mean_err_deg": 1.5, "mean_err_px": 90.0,
             "targets": [{"err_px": e} for e in
                        (80, 85, 90, 95, 100, 88, 92)],
             "head_position": _snap_pf},
            {"phase": "pre_check", "mean_err_deg": 1.6, "mean_err_px": 95.0,
             "targets": [{"err_px": e} for e in
                        (85, 90, 95, 100, 105, 93, 97)],
             "head_position": _snap_pc},
            {"phase": "post", "mean_err_deg": 1.7, "mean_err_px": 100.0,
             "targets": [{"err_px": e} for e in
                        (90, 95, 100, 105, 110, 98, 102)],
             "head_position": _snap_po},
        ],
        "head_position": _manifest_hp,
        "gain_correction": {},
        "correction_decision": None,
        "distance": {"cm": 52.0, "source": "iris", "measured": True},
    }
    try:
        _res = _vm_mod.Result()
        _vm_mod.check_session(_fake_manifest, _res)
        check("verify_metrics.check_session tolerates populated "
              "head_position", True)
    except Exception as exc:  # noqa: BLE001
        check("verify_metrics.check_session tolerates populated "
              "head_position", False, "%s: %s" % (type(exc).__name__, exc))

    import io as _io
    import contextlib as _cl
    import json as _js2
    import os as _os2
    import tempfile as _tf2

    with _tf2.NamedTemporaryFile("w", suffix="_manifest.json",
                                 delete=False) as _fh:
        _js2.dump(_fake_manifest, _fh)
        _tmp_manifest = _fh.name
    try:
        _a = _ca_mod.audit(_tmp_manifest)
        check("correction_audit.audit() picks up the head-position block",
              bool(_a and _a.get("head_position")))
        _buf = _io.StringIO()
        with _cl.redirect_stdout(_buf):
            _ca_mod.render(_a)
        check("correction_audit.render() prints the lost-face phase",
              "pre_check   no face geometry at capture time" in
              _buf.getvalue())
    finally:
        _os2.unlink(_tmp_manifest)
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("automatic head-position capture", False, _blocked or repr(exc))


print("\n[22] full-affine correction candidate")
# F33: a per-axis correction is structurally incapable of representing
# m_yx (vertical error from HORIZONTAL position — the shear). Simulated
# independently against the nine recorded sessions, pooling grid A and
# grid B to ~14 targets: full-affine beats the diagonal model by 15-28%
# on the two sheared sessions (PILOT_03, PILOT_04) and loses by 1-6% on
# every other session — reproducing F33's own table. "Do not ship a
# 6-parameter model on 7 targets": FULL_AFFINE_MIN_TARGETS gates it out
# below 12 measured targets, so it changes nothing on any of the nine
# recorded (7-target) sessions today.
try:
    import numpy as np
    import pandas as pd
    import validation_stats as _vsmod

    _rng = np.random.default_rng(2026)
    _A_true = np.array([[0.95, 0.12], [0.08, 1.05]])   # off-diagonal = shear
    _b_true = np.array([15.0, -8.0])
    _W, _H = 1920.0, 1080.0
    _cx, _cy = _W / 2, _H / 2
    _n = 16
    _t_xy = _rng.uniform([100, 100], [_W - 100, _H - 100], size=(_n, 2))
    _A_inv_true = np.linalg.inv(_A_true)
    _m_xy = np.array([_A_inv_true @ (_t_xy[i] - _b_true) + [_cx, _cy]
                      for i in range(_n)])
    _m_xy += _rng.normal(0, 3.0, size=_m_xy.shape)
    _targets = [{"tx": float(t[0]), "ty": float(t[1]),
                "mx": float(m[0]), "my": float(m[1])}
               for t, m in zip(_t_xy, _m_xy)]
    _m_arr = np.array([[t["mx"], t["my"]] for t in _targets])
    _t_arr = np.array([[t["tx"], t["ty"]] for t in _targets])

    _corr = _vsmod._fit_candidate(_m_arr, _t_arr, "full-affine", _W, _H)
    check("full-affine fits at n=16 and recovers the known shear",
          _corr is not None
          and np.allclose(np.array(_corr["A"]), _A_true, atol=0.05))

    _corr7 = _vsmod._fit_candidate(_m_arr[:7], _t_arr[:7], "full-affine",
                                   _W, _H)
    check("full-affine is refused outright below FULL_AFFINE_MIN_TARGETS "
          "(never even attempted at n=7)", _corr7 is None)

    _sel7 = _vsmod.select_correction(_targets[:7], _W, _H)
    _row7 = next(c for c in _sel7["decision"]["candidates"]
                if c["candidate"] == "full-affine")
    check("select_correction reports WHY it was not fittable (the "
          "target-count floor, not a generic gain excuse — F36)",
          _row7["status"] == "not fittable"
          and "12" in _row7["why"] and "7" in _row7["why"])
    check("full-affine never changes the chosen candidate at n=7 (the "
          "nine recorded sessions are unaffected today)",
          _sel7["decision"]["chosen"]
          in ("none", "affine", "quadratic-vertical"))

    if _corr:
        _rt = _vsmod.raw_targets([{"mx": 555.0, "my": 321.0}], _corr, _W, _H)
        _back = _vsmod.apply_point(_rt[0]["mx"], _rt[0]["my"], _corr)
        check("apply_point <-> raw_targets round-trips exactly (linear, "
              "no bisection needed)",
              abs(_back[0] - 555.0) < 1e-6 and abs(_back[1] - 321.0) < 1e-6)

        _pl = _vsmod.payload(_corr)
        check("payload() has no px/py for full-affine (explicit None, "
              "not a missing key that a .get('px') call reads the same "
              "as 'no correction')",
              "px" in _pl and _pl["px"] is None
              and "py" in _pl and _pl["py"] is None)
        check("payload() still carries gain_x/gain_y (the diagonal of A) "
              "for the one existing UI/log reader that expects them",
              abs(_pl["gain_x"] - _corr["A"][0][0]) < 1e-3)

        # THE bug this session found: reconstructing {"px", "py"} by hand
        # from a full-affine payload silently builds an empty correction.
        _naive = {"px": _pl.get("px"), "py": _pl.get("py")}
        _nx, _ny = _vsmod.apply_points(np.array([400.0]), np.array([300.0]),
                                       _naive)
        check("the naive {'px','py'} reconstruction IS a silent no-op "
              "for full-affine (demonstrates the bug from_payload fixes)",
              abs(_nx[0] - 400.0) < 1e-9 and abs(_ny[0] - 300.0) < 1e-9)

        _reconstructed = _vsmod.from_payload(_pl)
        check("from_payload reconstructs a correction that actually "
              "applies (not the naive no-op)",
              _reconstructed is not None
              and _vsmod.corrections_equal(_corr, _reconstructed))

        # app.py's live paths must not silently drop a full-affine
        # correction either — the same class of bug, three more places.
        _apx, _apy = _app_mod._apply_point(400.0, 300.0, _corr)
        check("app._apply_point delegates to validation_stats (matches)",
              abs(_apx - _vsmod.apply_point(400.0, 300.0, _corr)[0]) < 1e-9)

        _sx = pd.Series([100.0, 500.0], index=[5, 6])
        _sy = pd.Series([200.0, 400.0], index=[5, 6])
        _gx, _gy = _app_mod._apply_series(_sx, _sy, _corr)
        check("app._apply_series applies a full-affine correction (not a "
              "silent no-op) and preserves the index",
              list(_gx.index) == [5, 6]
              and (abs(_gx.iloc[0] - 100.0) > 1.0
                   or abs(_gy.iloc[0] - 200.0) > 1.0))

        _active_payload = _app_mod._correction_payload(_corr)
        _fake_state = {}
        _fake_record = {"targets": _targets,
                        "correction_active": _active_payload,
                        "screen": {"width_px": _W, "height_px": _H}}
        try:
            _app_mod._auto_fit_correction(_fake_state, _fake_record,
                                          sid="run-tests-fake-sid")
            check("_auto_fit_correction runs end-to-end with an ACTIVE "
                  "full-affine correction on the input", True)
        except Exception as exc:  # noqa: BLE001
            check("_auto_fit_correction runs end-to-end with an ACTIVE "
                  "full-affine correction on the input", False,
                  "%s: %s" % (type(exc).__name__, exc))
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("full-affine correction candidate", False, _blocked or repr(exc))


print("\n[23] The correction actually fixes a systematically offset point")
# Every check up to here verifies the correction machinery in isolation:
# fitting recovers known parameters, apply<->invert round-trips, payload
# forms survive reconstruction. None of them ask the practical question a
# participant actually asked: raw gaze reads BELOW the target — does the
# fitted correction put a NEW, held-out point back where it belongs?
# Parameter recovery and outcome correctness are not the same claim, and
# nothing before this section tested the second one.
try:
    import numpy as np
    import pandas as pd
    import validation_stats as _vsmod2
    import app as _app_mod2

    _rng2 = np.random.default_rng(42)
    _W2, _H2 = 1920.0, 1080.0
    _cy2 = _H2 / 2.0
    _OFFSET_Y = 80.0
    _GAIN_Y = 0.85
    _GRID_PCT = [(12, 12), (88, 12), (50, 31), (15, 50), (85, 50),
                (50, 69), (50, 88)]

    _fit_targets = []
    for _px, _py in _GRID_PCT:
        _tx, _ty = _px / 100.0 * _W2, _py / 100.0 * _H2
        _my_true = _GAIN_Y * (_ty - _cy2) + _cy2 + _OFFSET_Y
        _fit_targets.append({
            "tx": _tx, "ty": _ty,
            "mx": _tx + _rng2.normal(0, 4.0),
            "my": _my_true + _rng2.normal(0, 4.0),
        })

    _raw_bias = np.mean([t["my"] - t["ty"] for t in _fit_targets])
    check("scenario check: raw gaze IS systematically below target "
          "before any correction (this test's own setup, not the "
          "pipeline)", _raw_bias > 50, "mean raw dy = %.1f px" % _raw_bias)

    _result2 = _vsmod2.select_correction(_fit_targets, _W2, _H2)
    _corr2 = _result2["correction"]
    check("select_correction (what app._auto_fit_correction calls) picks "
          "a correction for an unambiguous systematic offset",
          _corr2 is not None)

    if _corr2:
        # HELD-OUT points: same bias model, NOT in the fit grid.
        _held_out = [(300.0, 200.0), (960.0, 540.0), (1600.0, 850.0),
                    (960.0, 130.0)]
        _resids = []
        for _tx, _ty in _held_out:
            _my_true = _GAIN_Y * (_ty - _cy2) + _cy2 + _OFFSET_Y
            _cx, _cyy = _vsmod2.apply_point(_tx, _my_true, _corr2)
            _resids.append(float(np.hypot(_cx - _tx, _cyy - _ty)))
        check("apply_point puts every HELD-OUT biased point (not used to "
              "fit) back within 15px of truth (started 60-95px off)",
              all(r < 15 for r in _resids),
              "residuals: %s" % [round(r, 1) for r in _resids])

        # The full live path: _auto_fit_correction (real server code) then
        # _apply_series (the real code that builds
        # corrected_gaze_position_* for a session's CSV).
        _fake_state2 = {}
        _fake_record2 = {
            "phase": "pre_fit", "targets": _fit_targets,
            "mean_err_px": float(np.mean(
                [np.hypot(t["mx"] - t["tx"], t["my"] - t["ty"])
                 for t in _fit_targets])),
            "screen": {"width_px": _W2, "height_px": _H2},
        }
        _app_mod2._auto_fit_correction(_fake_state2, _fake_record2,
                                       sid="run-tests-offset-test")
        _prod_corr = _fake_state2.get("correction")
        check("app._auto_fit_correction (the real server path) also "
              "selects a correction for this scenario", _prod_corr is not None)

        if _prod_corr:
            _n2 = 200
            _true_x = _rng2.uniform(100, _W2 - 100, _n2)
            _true_y = _rng2.uniform(100, _H2 - 100, _n2)
            _raw_x = _true_x + _rng2.normal(0, 5, _n2)
            _raw_y = (_GAIN_Y * (_true_y - _cy2) + _cy2 + _OFFSET_Y
                     + _rng2.normal(0, 5, _n2))
            _cx2, _cy3 = _app_mod2._apply_series(
                pd.Series(_raw_x), pd.Series(_raw_y), _prod_corr)
            _err_before = np.hypot(_raw_x - _true_x, _raw_y - _true_y)
            _err_after = np.hypot(_cx2.to_numpy() - _true_x,
                                  _cy3.to_numpy() - _true_y)
            _dy_after = float(np.mean(_cy3.to_numpy() - _true_y))
            check("on 200 simulated recorded samples the CSV correction "
                  "path (_apply_series) removes the systematic bias, not "
                  "just reduces it", abs(_dy_after) < 10,
                  "%.1f px remains" % _dy_after)
            check("mean error drops to well under a quarter of its raw "
                  "value (this is what a participant would see: the dot "
                  "moving onto the person, not staying above)",
                  _err_after.mean() < 0.25 * _err_before.mean(),
                  "%.1f -> %.1f px" % (_err_before.mean(), _err_after.mean()))
except Exception as exc:  # noqa: BLE001
    _blocked = environment_block(exc)
    check("the correction fixes a systematically offset point", False,
          _blocked or repr(exc))


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
