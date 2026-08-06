# -*- coding: utf-8 -*-
"""
Run the whole verification pass in one command, and summarise the result.

    python run_all.py                 # full pass, prompts for the two
                                      # steps that need a human
    python run_all.py --auto          # automated steps only, no prompts
    python run_all.py --seconds 60    # longer camera/tracker timings
    python run_all.py --skip-session  # everything except recording a session

WHAT IT DOES
  0. Environment + hardware summary
  1. Integrity suite (run_tests.py)
  2. Tracker self-check (dependencies, camera, MNN runtime)
  3. MNN backend benchmark — is the GPU used, and would it help?
  4. Calibration model check; offers to run one if missing
  5. Camera FPS (camera alone)
  6. Tracker Hz (camera + gaze inference)
  7. Optional: record one full session
  8. Quality report + manifest backfill dry-run
  9. VERDICT — the numbers that decide whether the data is usable

Everything is echoed live AND written to data/run_all_<timestamp>.log,
with a readable summary at data/run_all_<timestamp>.md.

Cross-platform: works the same on Windows and macOS. Stop the experiment
server before starting — only one process can own the webcam.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import platform
import re
import subprocess
import sys
import time
from datetime import datetime

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
DATA = os.path.join(BASE, "data")
PY = sys.executable or "python"

# GazeFollower persists its calibration here (SVRCalibration defaults to
# {HOME}/GazeFollower/calibration). Checking the files means we can
# detect a missing calibration WITHOUT opening the camera.
CALIB_DIR = pathlib.Path.home() / "GazeFollower" / "calibration"
CALIB_FILES = (CALIB_DIR / "svr_x.xml", CALIB_DIR / "svr_y.xml")

RESULTS: dict = {}
_LOG_FH = None


def _child_env() -> dict:
    """Environment for sub-steps, forcing UTF-8 output.

    A piped child process inherits the Windows locale encoding (cp1252)
    for stdout even on Python 3.12, so the first '≈' or '✓' raises
    UnicodeEncodeError and kills that step halfway through its report.
    PYTHONUTF8/PYTHONIOENCODING remove the whole class of failure; each
    tool also reconfigures its own stdout as a second line of defence.
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def out(text: str = "") -> None:
    print(text, flush=True)
    if _LOG_FH:
        _LOG_FH.write(text + "\n")
        _LOG_FH.flush()


def section(title: str) -> None:
    out("")
    out("=" * 72)
    out("  " + title)
    out("=" * 72)


def run(args: list, title: str, timeout: int = 900) -> str:
    """Run a step, stream its output, and return it. Never raises."""
    section(title)
    out("> %s %s" % (os.path.basename(PY), " ".join(args)))
    out("")
    try:
        proc = subprocess.run(
            [PY] + args, cwd=BASE, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # encoding= is REQUIRED, not optional: text=True alone decodes
            # with the locale encoding (cp1252 on Windows), which turns
            # the children's UTF-8 output into mojibake ("â€”") in the log.
            text=True, encoding="utf-8", errors="replace", env=_child_env(),
        )
        out(proc.stdout.rstrip())
        if proc.returncode != 0:
            out("\n!! exited with code %d — see the message above."
                % proc.returncode)
        return proc.stdout
    except subprocess.TimeoutExpired:
        out("!! timed out after %d s" % timeout)
        return ""
    except Exception as exc:  # noqa: BLE001
        out("!! could not run: %s" % exc)
        return ""


def interactive(args: list, title: str) -> None:
    """Launch a step that needs the participant/researcher at the keyboard."""
    section(title)
    out("> %s %s   (interactive — finish in the app, then close it)"
        % (os.path.basename(PY), " ".join(args)))
    out("")
    try:
        subprocess.run([PY] + args, cwd=BASE)
    except KeyboardInterrupt:
        out("\n(stopped)")
    except Exception as exc:  # noqa: BLE001
        out("!! could not run: %s" % exc)


def ask(question: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer.startswith("y")


def has_calibration() -> bool:
    return all(p.exists() for p in CALIB_FILES)


# ── Output parsing (regexes match the exact strings the tools print) ──

def parse_all(name: str, text: str) -> None:
    if name == "tests":
        RESULTS["tests_pass"] = "RESULT: ALL TESTS PASSED" in text
        m = re.search(r"RESULT: (\d+) FAILURE", text)
        RESULTS["tests_failures"] = int(m.group(1)) if m else 0
    elif name == "check":
        RESULTS["check_fails"] = re.findall(r"^\s*(\w+):\s+FAIL", text,
                                            re.MULTILINE)
        m = re.search(r"arch:\s*(.+)", text)
        RESULTS["arch"] = m.group(1).strip() if m else "?"
    elif name == "camera":
        m = re.search(r"FPS\s*:\s*median\s+([\d.]+)\s*\|\s*peak\s+([\d.]+)",
                      text)
        if m:
            RESULTS["camera_median_fps"] = float(m.group(1))
            RESULTS["camera_peak_fps"] = float(m.group(2))
    elif name == "tracker":
        m = re.search(r"sample rate: overall ([\d.]+) Hz \| median-interval "
                      r"([\d.]+) Hz", text)
        if m:
            RESULTS["tracker_overall_hz"] = float(m.group(1))
            RESULTS["tracker_median_hz"] = float(m.group(2))
        m = re.search(r"early third ([\d.]+) Hz\s*->\s*late third ([\d.]+) Hz",
                      text)
        if m:
            RESULTS["tracker_early_hz"] = float(m.group(1))
            RESULTS["tracker_late_hz"] = float(m.group(2))
        RESULTS["tracker_needs_calibration"] = \
            "No saved calibration model" in text
    elif name == "mnn":
        rows = re.findall(
            r"^(\w+)\s+(\d+)\s+([\d.]+) ms\s+([\d.]+) ms\s+([\d.]+)(.*)$",
            text, re.MULTILINE)
        RESULTS["mnn_rows"] = [
            {"backend": r[0], "threads": int(r[1]), "median_ms": float(r[2]),
             "max_hz": float(r[4]), "note": r[5].strip()} for r in rows
        ]
        m = re.search(r"MediaPipe FaceMesh\s*:\s*([\d.]+) ms", text)
        if m:
            RESULTS["facemesh_ms"] = float(m.group(1))
    elif name == "backfill":
        RESULTS["backfill_flips"] = text.count("VERDICT FLIPPED")


# ── Verdict ──────────────────────────────────────────────────────────

def verdict() -> list:
    """Turn the collected numbers into plain-language conclusions."""
    lines: list = []
    budget_ms = 1000.0 / 30.0

    if RESULTS.get("tests_pass"):
        lines.append(("ok", "Integrity suite passed — the metric formulas "
                            "are intact."))
    elif RESULTS.get("tests_failures"):
        lines.append(("bad", "Integrity suite FAILED (%d) — fix before "
                             "trusting any number below."
                      % RESULTS["tests_failures"]))
    else:
        lines.append(("bad", "Integrity suite did not COMPLETE (it crashed "
                             "or was interrupted) — scroll up to its section "
                             "for the traceback."))

    fails = RESULTS.get("check_fails") or []
    if fails:
        lines.append(("bad", "Self-check failures: %s" % ", ".join(fails)))
    else:
        lines.append(("ok", "All tracker dependencies and the camera are OK."))

    cam = RESULTS.get("camera_median_fps")
    if cam is not None:
        if cam >= 25:
            lines.append(("ok", "Camera sustains %.0f FPS — the camera is "
                                "not your bottleneck." % cam))
        else:
            lines.append(("bad", "Camera only reaches %.0f FPS. Fix lighting "
                                 "or the camera/driver FIRST — no amount of "
                                 "compute tuning beats a slow camera." % cam))

    hz = RESULTS.get("tracker_median_hz")
    if RESULTS.get("tracker_needs_calibration"):
        lines.append(("warn", "Tracker rate not measured — no calibration "
                              "model. Run one calibration, then re-run."))
    elif hz is not None:
        if hz >= 25:
            lines.append(("ok", "Tracker sustains %.1f Hz — full rate. "
                                "Fixation timing is trustworthy." % hz))
        elif hz >= 20:
            lines.append(("warn", "Tracker at %.1f Hz — above the %.0f Hz "
                                  "threshold but with little margin." % (hz, 20)))
        else:
            lines.append(("bad", "Tracker at %.1f Hz — BELOW the 20 Hz "
                                 "threshold. Fixation counts and durations "
                                 "are not reportable at this rate; dwell "
                                 "measures only." % hz))
        if cam and hz < 0.6 * cam:
            lines.append(("warn", "Tracker (%.1f Hz) is far below the camera "
                                  "(%.0f FPS) — per-frame INFERENCE is the "
                                  "limiter, not the camera or lighting."
                          % (hz, cam)))
    early = RESULTS.get("tracker_early_hz")
    late = RESULTS.get("tracker_late_hz")
    if early and late and late < 0.75 * early:
        lines.append(("bad", "Rate fell %.0f Hz -> %.0f Hz during the "
                             "measurement: the CPU turbo window closed. "
                             "Warm the machine up BEFORE calibrating, so "
                             "calibration and the videos share one rate."
                      % (early, late)))

    rows = RESULTS.get("mnn_rows") or []
    cpu = next((r for r in rows if r["backend"] == "CPU"), None)
    if cpu:
        gpu_wins = [r for r in rows
                    if r["backend"] != "CPU" and "faster" in r["note"]]
        best_cpu = min((r for r in rows if r["backend"] == "CPU"),
                       key=lambda r: r["median_ms"])
        lines.append(("info", "Gaze model on CPU: %.1f ms (%d threads). "
                              "Frame budget at 30 fps is %.1f ms."
                      % (best_cpu["median_ms"], best_cpu["threads"],
                         budget_ms)))
        # Only call a thread count a "win" if it clears run-to-run noise.
        # Sub-1% differences are measurement scatter, and recommending a
        # config change on scatter is worse than saying nothing.
        four = next((r["median_ms"] for r in rows
                     if r["backend"] == "CPU" and r["threads"] == 4),
                    float("inf"))
        if best_cpu["threads"] != 4 and best_cpu["median_ms"] < 0.95 * four:
            lines.append(("ok", "Threads=%d beats GazeFollower's hardcoded 4 "
                                "(%.1f vs %.1f ms) — set GF_MNN_THREADS=%d."
                          % (best_cpu["threads"], best_cpu["median_ms"],
                             four, best_cpu["threads"])))
        elif four < float("inf"):
            lines.append(("info", "Thread count makes no difference (%.1f ms "
                                  "at 4, 10 and 20) — this model does not "
                                  "parallelise. Leave GF_MNN_THREADS alone."
                          % four))
        if gpu_wins:
            b = min(gpu_wins, key=lambda r: r["median_ms"])
            lines.append(("ok", "%s is genuinely faster (%.1f ms). Enable "
                                "with GF_MNN_BACKEND=%s, then re-run this "
                                "script to confirm the END-TO-END rate "
                                "improved." % (b["backend"], b["median_ms"],
                                               b["backend"])))
        else:
            lines.append(("info", "No GPU backend beat CPU — either none is "
                                  "compiled into your MNN wheel (the stock "
                                  "PyPI build is CPU-only) or the model is "
                                  "too small to benefit. Not worth chasing "
                                  "unless the CPU number is near the budget."))
    fm = RESULTS.get("facemesh_ms")
    if fm and cpu:
        total = fm + min(r["median_ms"] for r in rows if r["backend"] == "CPU")
        lines.append(("info", "FaceMesh %.1f ms + gaze model = ~%.1f ms per "
                              "frame vs a %.1f ms budget -> ~%.1f Hz ceiling. "
                              "FaceMesh is CPU-only in MediaPipe's Python "
                              "API and cannot be moved to the GPU."
                      % (fm, total, budget_ms, 1000.0 / total)))

    if RESULTS.get("backfill_flips"):
        lines.append(("warn", "%d historical stimulus verdict(s) still flip "
                              "under the corrected metric — expected only if "
                              "you have new pre-fix sessions; re-run "
                              "backfill_manifests.py to apply."
                      % RESULTS["backfill_flips"]))
    return lines


def write_report(path: str, lines: list, log_path: str) -> None:
    icon = {"ok": "PASS", "warn": "WARN", "bad": "FAIL", "info": "INFO"}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Setup verification — %s\n\n"
                 % datetime.now().strftime("%Y-%m-%d %H:%M"))
        fh.write("Machine: %s %s, Python %s\n\n"
                 % (platform.system(), platform.machine(),
                    platform.python_version()))
        fh.write("Full log: `%s`\n\n## Verdict\n\n"
                 % os.path.basename(log_path))
        for kind, text in lines:
            fh.write("- **%s** — %s\n" % (icon[kind], text))
        fh.write("\n## Key numbers\n\n| metric | value |\n|---|---|\n")
        for key in ("camera_median_fps", "tracker_median_hz",
                    "tracker_early_hz", "tracker_late_hz", "facemesh_ms",
                    "arch"):
            if key in RESULTS:
                fh.write("| %s | %s |\n" % (key.replace("_", " "),
                                            RESULTS[key]))
        rows = RESULTS.get("mnn_rows") or []
        if rows:
            fh.write("\n## Inference backends\n\n"
                     "| backend | threads | median ms | max Hz | note |\n"
                     "|---|---|---|---|---|\n")
            for r in rows:
                fh.write("| %s | %d | %.2f | %.1f | %s |\n"
                         % (r["backend"], r["threads"], r["median_ms"],
                            r["max_hz"], r["note"]))


def main() -> int:
    global _LOG_FH

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--auto", action="store_true",
                    help="automated steps only; never prompt or open the app")
    ap.add_argument("--seconds", type=int, default=45,
                    help="duration of the camera/tracker timings")
    ap.add_argument("--skip-session", action="store_true",
                    help="do not offer to record a full session")
    ap.add_argument("--quick", action="store_true",
                    help="skip the camera/tracker timings entirely")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = os.path.join(DATA, "run_all_%s.log" % stamp)
    md_path = os.path.join(DATA, "run_all_%s.md" % stamp)
    _LOG_FH = open(log_path, "w", encoding="utf-8")   # noqa: SIM115
    t0 = time.time()

    section("0/8  ENVIRONMENT")
    # Every sub-step inherits this interpreter, so running run_all.py with
    # the system Python makes ALL of them fail with confusing import
    # errors. Catch it once, here, rather than eight times downstream.
    try:
        from env_check import in_project_venv, venv_python

        if not in_project_venv():
            out("!! WRONG INTERPRETER")
            out("   running   : %s" % PY)
            out("   should be : %s" % venv_python())
            out("")
            out("   Every step below would fail with import errors.")
            out("   Activate the project venv and re-run:")
            out("       .\\.venv\\Scripts\\activate     (Windows)"
                if os.name == "nt" else
                "       source .venv/bin/activate     (macOS/Linux)")
            out("")
            if _LOG_FH:
                _LOG_FH.close()
            return 1
    except ImportError:
        pass

    out("when     : %s" % datetime.now())
    out("machine  : %s %s (%s)" % (platform.system(), platform.release(),
                                   platform.machine()))
    out("python   : %s (%s)" % (PY, platform.python_version()))
    out("project  : %s" % BASE)
    out("calib    : %s" % ("found at %s" % CALIB_DIR if has_calibration()
                           else "NOT FOUND — step 4 will offer to fix this"))
    out("")
    out("Be on AC power, with Low Power Mode / battery saver OFF and the")
    out("power plan at maximum performance. On battery you measure the")
    out("power policy, not the hardware. Stop the experiment server too —")
    out("only one process can own the webcam.")

    parse_all("tests", run(["run_tests.py"], "1/8  INTEGRITY SUITE"))
    parse_all("check", run(["tracker_service.py", "--check"],
                           "2/8  TRACKER SELF-CHECK"))
    parse_all("mnn", run(["mnn_backend.py", "--profile"],
                         "3/8  MNN BACKEND BENCHMARK (is the GPU used?)"))

    # ── 4. Calibration persistence (informational) ──
    section("4/8  CALIBRATION PERSISTENCE")
    if has_calibration():
        out("A saved calibration exists at %s" % CALIB_DIR)
        out("")
        out("WARNING: SVRCalibration auto-loads any saved model at startup")
        out("and marks itself calibrated. If a session ever skipped or")
        out("failed calibration, it would silently inherit THIS model —")
        out("i.e. a previous participant's mapping. Delete these files")
        out("before collecting real data.")
    else:
        out("No saved calibration at %s — this is NORMAL." % CALIB_DIR)
        out("")
        out("GazeFollower never persists a calibration: SVRCalibration has")
        out("a save_model() method, but nothing in GazeFollower calls it.")
        out("Each session calibrates in memory, which is exactly what you")
        out("want for a study — no chance of reusing another participant's")
        out("mapping. The tracker timing below stubs the calibration step")
        out("so it can measure throughput regardless.")

    # ── 5-6. Timings ──
    if args.quick:
        section("5-6/8  CAMERA & TRACKER TIMINGS SKIPPED (--quick)")
    else:
        parse_all("camera", run(
            ["camera_fps_test.py", "--no-window", "--seconds",
             str(args.seconds)],
            "5/8  CAMERA FPS (camera alone, no inference)",
            timeout=args.seconds + 120))
        parse_all("tracker", run(
            ["tracker_fps_test.py", "--seconds", str(args.seconds)],
            "6/8  TRACKER Hz (camera + gaze inference)",
            timeout=args.seconds + 180))

    # ── 7. Full session ──
    section("7/8  FULL SESSION")
    if args.auto or args.skip_session:
        out("Skipped (--auto/--skip-session).")
    elif ask("Record one full session now (calibration -> videos)?",
             default=False):
        out("")
        out("Watch for: the rate-gate verdict after calibration, 7 targets")
        out("in BOTH validations, and gain reported separately for x and y.")
        interactive(["app.py"], "7b/8  SESSION (interactive)")
    else:
        out("Skipped.")

    # ── 8. Data quality ──
    run(["quality_report.py"], "8/8  QUALITY REPORT (all sessions)")
    parse_all("backfill", run(["backfill_manifests.py", "--dry-run"],
                              "8b/8  MANIFEST BACKFILL (dry run)"))

    # ── Verdict ──
    lines = verdict()
    section("VERDICT")
    icon = {"ok": "  [PASS]", "warn": "  [WARN]", "bad": "  [FAIL]",
            "info": "  [INFO]"}
    for kind, text in lines:
        out("%s %s" % (icon[kind], text))
    out("")
    out("Elapsed: %.1f min" % ((time.time() - t0) / 60))

    try:
        write_report(md_path, lines, log_path)
        out("")
        out("Log    : %s" % log_path)
        out("Summary: %s" % md_path)
    except Exception as exc:  # noqa: BLE001
        out("(could not write the summary: %s)" % exc)

    if _LOG_FH:
        _LOG_FH.close()
    return 0 if RESULTS.get("tests_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
