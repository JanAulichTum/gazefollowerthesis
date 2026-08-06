# -*- coding: utf-8 -*-
"""
Read a session telemetry file and say what happened.

WHY
---
Telemetry that nobody reads is just disk usage. This turns a recorded
session into the three things worth knowing:

    1. WHAT THE MACHINE WAS   the environment the data was produced in,
                              including the settings that have already
                              been shown to halve the sampling rate
    2. WHAT HAPPENED WHEN     a timeline of calibration, validations,
                              rate gates and stimuli, with the rate at
                              each point
    3. WHAT LOOKS WRONG       anomalies, each with the specific thing to
                              check — and crucially, WHEN it started,
                              because "it was bad from the first sample"
                              and "it degraded during stimulus 3" have
                              completely different causes

Usage::

    python diagnose_session.py                       # newest file
    python diagnose_session.py <telemetry.json>
    python diagnose_session.py --list
    python diagnose_session.py --compare A.json B.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
TELEMETRY_DIR = os.path.join(BASE, "data", "telemetry")

try:
    from config import MIN_SAMPLING_HZ, NOMINAL_SAMPLING_HZ
except ImportError:
    MIN_SAMPLING_HZ, NOMINAL_SAMPLING_HZ = 20.0, 30.0

FRAME_BUDGET_MS = 1000.0 / NOMINAL_SAMPLING_HZ


def hr(title: str) -> None:
    print("\n" + "=" * 74)
    print("  " + title)
    print("=" * 74)


def _col(series: list, key: str) -> list:
    return [r[key] for r in series if isinstance(r.get(key), (int, float))]


def _stats(vals: list) -> "dict | None":
    if not vals:
        return None
    s = sorted(vals)
    return {"min": s[0], "median": statistics.median(s), "max": s[-1],
            "n": len(s)}


def _fmt(st: "dict | None", unit: str = "") -> str:
    if not st:
        return "—"
    return "%.1f / %.1f / %.1f%s" % (st["min"], st["median"], st["max"], unit)


# ──────────────────────────────────────────────────────────────────────

def show_environment(env: dict) -> None:
    hr("MACHINE AND SETTINGS")
    cpu = env.get("cpu") or {}
    print("  host        : %s" % env.get("hostname"))
    print("  platform    : %s" % env.get("platform"))
    print("  cpu         : %s" % cpu.get("processor"))
    print("  cores       : %s physical / %s logical"
          % (cpu.get("physical_cores"), cpu.get("logical_cores")))
    print("  ram         : %s GB" % cpu.get("ram_total_gb"))
    print("  python      : %s%s" % (env.get("python"),
                                    "" if env.get("in_venv") else "  (NOT in a venv!)"))
    perf = env.get("perf_mode") or {}
    print("  perf mode   : %s" % perf.get("describe"))
    power = env.get("power_at_start") or {}
    if power:
        print("  power       : %s (%s%%)"
              % ("AC" if power.get("on_ac_power") else "battery",
                 power.get("battery_pct")))
    envv = env.get("env_vars") or {}
    if envv:
        print("  env vars    : %s"
              % ", ".join("%s=%s" % (k, v) for k, v in sorted(envv.items())))
    pkgs = env.get("packages") or {}
    key_pkgs = [(k, pkgs.get(k)) for k in
                ("numpy", "mediapipe", "MNN", "gazefollower", "cv2")]
    print("  packages    : %s"
          % ", ".join("%s %s" % (k, v) for k, v in key_pkgs if v))
    srcs = env.get("sources") or {}
    stamps = sorted(v.get("mtime") for v in srcs.values()
                    if isinstance(v, dict) and v.get("mtime"))
    if stamps:
        print("  code stamp  : newest source %s" % stamps[-1])


def show_timeline(data: dict) -> None:
    hr("TIMELINE")
    events = data.get("events") or []
    series = data.get("series") or []
    if not events:
        print("  (no events recorded)")
        return

    def rate_at(t: float) -> "float | None":
        near = [r for r in series
                if isinstance(r.get("sampling_hz"), (int, float))
                and abs(r.get("t", -999) - t) <= 3.0]
        return statistics.median([r["sampling_hz"] for r in near]) \
            if near else None

    print("  %8s  %-24s %-9s %s" % ("t (s)", "event", "rate", "detail"))
    print("  " + "-" * 70)
    for ev in events:
        t = ev.get("t", 0.0)
        hz = rate_at(t)
        detail = ", ".join(
            "%s=%s" % (k, v) for k, v in ev.items()
            if k not in ("t", "utc", "event") and v is not None)
        print("  %8.1f  %-24s %-9s %s"
              % (t, str(ev.get("event"))[:24],
                 ("%.1f Hz" % hz) if hz else "—", detail[:60]))


def show_series(data: dict) -> None:
    hr("MEASUREMENTS  (min / median / max)")
    series = data.get("series") or []
    if not series:
        print("  (no samples — was the session shorter than a second?)")
        return
    rows = [
        ("sampling rate", "sampling_hz", " Hz"),
        ("FaceMesh", "face_ms_median", " ms"),
        ("gaze CNN", "gaze_ms_median", " ms"),
        ("detected", "detected_pct_cumulative", " %"),
        ("CPU (system)", "cpu_pct_system", " %"),
        ("CPU (process)", "cpu_pct_process", " %"),
        ("CPU clock", "cpu_mhz", " MHz"),
        ("RAM used", "ram_used_pct", " %"),
        ("process RSS", "proc_rss_mb", " MB"),
        ("threads", "proc_threads", ""),
        ("battery", "battery_pct", " %"),
        ("face width", "face_w", " px"),
    ]
    for label, key, unit in rows:
        st = _stats(_col(series, key))
        if st:
            print("  %-16s %s" % (label, _fmt(st, unit)))
    sizes = {r.get("frame_size") for r in series if r.get("frame_size")}
    if sizes:
        print("  %-16s %s" % ("frame size", ", ".join(sorted(sizes))))
    subs = {r.get("subscribers") for r in series
            if r.get("subscribers") is not None}
    if subs:
        print("  %-16s %s" % ("subscribers", sorted(subs)))


def find_anomalies(data: dict) -> list:
    """Everything worth flagging, with what to do about it."""
    out = []
    series = data.get("series") or []
    env = data.get("environment") or {}
    perf = (env.get("perf_mode") or {}).get("describe") or ""
    hz = _stats(_col(series, "sampling_hz"))
    face = _stats(_col(series, "face_ms_median"))
    gaze = _stats(_col(series, "gaze_ms_median"))

    # ── The one that cost this project days ──
    if "NOT active" in perf or "off (" in perf:
        out.append((
            "CRITICAL", "Performance mode was OFF.",
            "Windows/macOS may have scheduled the tracker onto efficiency "
            "cores. On this project that alone took 29.4 Hz down to 12.1 Hz, "
            "slowing BOTH model stages by a similar factor. Re-run with "
            "GF_PERF_MODE=1 before trusting any timing in this session."))

    if hz and hz["median"] < MIN_SAMPLING_HZ:
        out.append((
            "CRITICAL",
            "Median sampling rate %.1f Hz is below the %.0f Hz threshold."
            % (hz["median"], MIN_SAMPLING_HZ),
            "Fixation timing is unreliable here. Check the per-stage costs "
            "below to see whether it is FaceMesh or the gaze CNN."))

    # Per-frame budget.
    if face and gaze:
        total = face["median"] + gaze["median"]
        if total > FRAME_BUDGET_MS:
            out.append((
                "CRITICAL",
                "Per-frame model cost %.1f ms exceeds the %.1f ms budget."
                % (total, FRAME_BUDGET_MS),
                "%.0f Hz is arithmetically impossible at this cost. "
                "FaceMesh %.1f ms + gaze CNN %.1f ms."
                % (NOMINAL_SAMPLING_HZ, face["median"], gaze["median"])))
        elif total > 0.85 * FRAME_BUDGET_MS:
            out.append((
                "WARNING",
                "Per-frame cost %.1f ms uses %.0f %% of the %.1f ms budget."
                % (total, 100 * total / FRAME_BUDGET_MS, FRAME_BUDGET_MS),
                "It works, but there is no margin — any extra load will "
                "push it over and the rate will fall proportionally."))

    # Oversized camera frames.
    sizes = {r.get("frame_size") for r in series if r.get("frame_size")}
    for size in sizes:
        try:
            w, h = (int(x) for x in size.split("x"))
        except Exception:  # noqa: BLE001
            continue
        if w > 640 or h > 480:
            out.append((
                "WARNING", "Camera delivered %s frames." % size,
                "GazeFollower resizes every frame in software, and FaceMesh "
                "is being fed %.1fx the intended 640x480 pixels. "
                "Try GF_CAMERA_FIX=1." % ((w * h) / (640 * 480))))

    # Degradation WITHIN the session — first third vs last third.
    rates = [(r.get("t", 0), r["sampling_hz"]) for r in series
             if isinstance(r.get("sampling_hz"), (int, float))]
    if len(rates) >= 12:
        third = len(rates) // 3
        early = statistics.median([v for _, v in rates[:third]])
        late = statistics.median([v for _, v in rates[-third:]])
        if late < 0.85 * early:
            out.append((
                "WARNING",
                "Rate FELL during the session: %.1f -> %.1f Hz."
                % (early, late),
                "Something accumulated or the machine slowed. Check the CPU "
                "clock and subscriber count over time — a steady slide and a "
                "single step have different causes."))
        elif early < 0.85 * late:
            out.append((
                "INFO",
                "Rate ROSE during the session: %.1f -> %.1f Hz."
                % (early, late),
                "Usually a cold start: models warming, or the CPU ramping "
                "out of a low power state."))

    # Subscriber accumulation (each duplicate costs a write per frame).
    subs = [r["subscribers"] for r in series
            if isinstance(r.get("subscribers"), int)]
    if subs and max(subs) > min(subs):
        out.append((
            "WARNING", "Subscriber count changed: %s -> %s."
            % (min(subs), max(subs)),
            "Expected a constant 2. Duplicates mean an extra CSV write per "
            "frame, accumulating across start/stop cycles."))

    # Detection.
    det = _stats(_col(series, "detected_pct_cumulative"))
    if det and det["median"] < 90:
        out.append((
            "WARNING", "Face detected in only %.0f %% of frames."
            % det["median"],
            "This is a DETECTION problem, not a speed problem — check "
            "lighting, camera angle and whether the participant left frame. "
            "The two need opposite fixes."))

    # Power.
    ac = [r.get("on_ac_power") for r in series if "on_ac_power" in r]
    if ac and not any(ac):
        out.append((
            "INFO", "Recorded on battery throughout.",
            "On this project battery alone did NOT reduce the rate — perf "
            "mode was the real factor — but it is worth knowing."))
    clock = _stats(_col(series, "cpu_mhz"))
    if clock and clock["max"] > 0 and clock["min"] < 0.6 * clock["max"]:
        out.append((
            "INFO", "CPU clock varied %.0f - %.0f MHz."
            % (clock["min"], clock["max"]),
            "Timings measured at different clocks are not comparable with "
            "each other."))

    if data.get("sampler_errors"):
        out.append((
            "INFO", "Telemetry sampler hit %d errors."
            % data["sampler_errors"],
            "Some samples may be missing; the session itself is unaffected."))
    return out


def show_anomalies(data: dict) -> int:
    hr("WHAT LOOKS WRONG")
    found = find_anomalies(data)
    if not found:
        print("  Nothing flagged. Rate, per-frame cost, detection, "
              "subscribers\n  and power all look normal for this session.")
        return 0
    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    for level, what, why in sorted(found, key=lambda f: order.get(f[0], 9)):
        print("\n  [%s] %s" % (level, what))
        for line in _wrap(why, 66):
            print("         " + line)
    return sum(1 for f in found if f[0] == "CRITICAL")


def _wrap(text: str, width: int) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def compare(a: dict, b: dict) -> None:
    hr("COMPARISON")
    print("  %-22s %18s %18s" % ("", "A", "B"))
    print("  " + "-" * 60)
    rows = [("sampling rate (Hz)", "sampling_hz"),
            ("FaceMesh (ms)", "face_ms_median"),
            ("gaze CNN (ms)", "gaze_ms_median"),
            ("CPU system (%)", "cpu_pct_system"),
            ("CPU clock (MHz)", "cpu_mhz")]
    for label, key in rows:
        sa = _stats(_col(a.get("series") or [], key))
        sb = _stats(_col(b.get("series") or [], key))
        print("  %-22s %18s %18s"
              % (label,
                 "%.1f" % sa["median"] if sa else "—",
                 "%.1f" % sb["median"] if sb else "—"))
    pa = ((a.get("environment") or {}).get("perf_mode") or {}).get("describe")
    pb = ((b.get("environment") or {}).get("perf_mode") or {}).get("describe")
    print("\n  perf mode A: %s" % pa)
    print("  perf mode B: %s" % pb)


def _newest() -> "str | None":
    files = sorted(glob.glob(os.path.join(TELEMETRY_DIR, "*_telemetry.json")))
    return files[-1] if files else None


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", help="telemetry JSON (default: newest)")
    ap.add_argument("--list", action="store_true", help="list saved files")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()

    if args.list:
        files = sorted(glob.glob(os.path.join(TELEMETRY_DIR,
                                              "*_telemetry.json")))
        if not files:
            print("No telemetry files in %s" % TELEMETRY_DIR)
            return 1
        for f in files:
            print("  %s  (%.0f KB)"
                  % (os.path.basename(f), os.path.getsize(f) / 1024))
        return 0

    if args.compare:
        compare(_load(args.compare[0]), _load(args.compare[1]))
        return 0

    path = args.path or _newest()
    if not path or not os.path.isfile(path):
        print("No telemetry file found. Run a session first "
              "(telemetry is on by default; GF_TELEMETRY=0 disables it).")
        return 1

    data = _load(path)
    print("=" * 74)
    print("  SESSION DIAGNOSIS")
    print("=" * 74)
    print("  file       : %s" % os.path.basename(path))
    print("  session    : %s" % data.get("session_id"))
    print("  started    : %s" % data.get("started_utc"))
    print("  duration   : %s s  (%d samples, %d events)"
          % (data.get("duration_s"), len(data.get("series") or []),
             len(data.get("events") or [])))

    show_environment(data.get("environment") or {})
    show_series(data)
    show_timeline(data)
    critical = show_anomalies(data)
    print()
    return 2 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
