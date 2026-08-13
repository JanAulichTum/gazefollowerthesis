# -*- coding: utf-8 -*-
"""Reconstruct a session manifest from the server log.

WHEN THIS IS NEEDED
-------------------
Finalisation re-reads the whole gaze CSV once per stimulus and writes the
manifest LAST, so it takes about a minute after the participant finishes.
Close the app inside that window and the recording survives while the
session record does not: no validations, no distance, no quality
metrics, nothing that makes the data analysable.

Everything lost that way was logged on its way through. This parses the
log back into a manifest.

WHAT IT RECOVERS, AND WHAT IT CANNOT
------------------------------------
    recovered   validations (phase, mean error in px and degrees, targets
                measured, samples per target, browser geometry), the
                measured distance and its ruler, the gain correction,
                both rate gates, the stimulus presentation windows, the
                session's routing decision
    NOT         per-target error breakdown - the log records the mean and
                the sample counts, not each target's error

A reconstructed manifest is MARKED as one. It carries a ``reconstructed``
block naming the log it came from and listing what is absent, so no
analysis can mistake it for a session that finalised normally. That
matters more than the convenience: a study that silently mixes recorded
and reconstructed records cannot answer which is which.

Usage::

    python rebuild_manifest.py --session Julianne_P1_2026-08-13_160841
    python rebuild_manifest.py --session <id> --log data/server.log
    python rebuild_manifest.py --session <id> --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import config

TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+"

RE_ROUTE = re.compile(TS + r".*Session (\S+) -> (\w+)")
RE_VALIDATION = re.compile(
    TS + r".*Validation \((\w+)\): mean error ([\d.]+) px / ([\d.]+) deg"
    r" \| targets measured (\d+)/(\d+), samples per target \[([\d, ]+)\]"
    r"(?: \| fullscreen=(\w+) inner=\[([\d, ]+)\] offsets=\[([-\d, ]+)\]"
    r" dpr=([\d.]+))?")
RE_DISTANCE = re.compile(
    TS + r".*MEASURED distance \(([\d.]+) cm, via ([^)]+(?:\([^)]*\))?)\)"
    r" replaces the browser's assumption: ([\d.]+) -> ([\d.]+) deg")
RE_GAIN = re.compile(
    TS + r".*Gain correction auto-fitted \((\w+)\): gain_x ([\d.]+),"
    r" gain_y \(centre\) ([\d.]+)")
RE_RATE = re.compile(
    TS + r".*Rate gate \[([\w-]+) #?\d*\]: ([\d.]+) Hz sustained"
    r" \(initial ([\d.]+), peak ([\d.]+)\) \| ([\d.]+)% detected")
RE_REC = re.compile(
    TS + r".*Recording (started|stopped) . sid=\S+, participant=(.+?),"
    r" stimulus=(\S+?)(?:,|$)")
RE_SAVED = re.compile(
    TS + r".*Saved (\d+) GazeFollower samples . participant=(.+?),"
    r" stimulus=(\S+)")
RE_PERF = re.compile(TS + r".*Perf mode \[([\w-]+)\]: (.+)$")


def _parse(log_text: str, session: str) -> dict:
    """Everything the log holds about one session.

    Log lines do not all name the session, so the window is bounded by
    the participant label: it appears on the login, the recordings and
    the saves. Lines between the first and last mention belong to it.
    """
    lines = log_text.splitlines()

    # The routing line is the one place the session id appears verbatim.
    end_idx, participant = None, None
    for i, line in enumerate(lines):
        m = RE_ROUTE.search(line)
        if m and m.group(2) == session:
            end_idx = i
    if end_idx is None:
        # Fall back to the id being present anywhere.
        for i, line in enumerate(lines):
            if session in line:
                end_idx = i
    if end_idx is None:
        return {}

    # The participant label, taken from a Recording line before the end.
    for line in reversed(lines[:end_idx + 1]):
        m = RE_REC.search(line)
        if m:
            participant = m.group(3)
            break

    # Walk backwards to the login for this participant.
    start_idx = 0
    for i in range(end_idx, -1, -1):
        if participant and ("New participant registered: %s" % participant
                            in lines[i]
                            or "SocketIO connected" in lines[i]
                            and participant in lines[i]):
            start_idx = i
            break

    window = lines[start_idx:end_idx + 2000]
    out = {"participant": participant, "validations": [], "rate_gates": [],
           "stimulus_log": [], "saved": {}, "gain": None, "log_lines":
           len(window)}
    pending_distance = None

    for line in window:
        m = RE_DISTANCE.search(line)
        if m:
            pending_distance = {
                "cm": float(m.group(2)), "source": m.group(3).strip(),
                "deg_browser": float(m.group(4)),
                "deg_measured": float(m.group(5)),
                "at": m.group(1),
            }
            continue
        m = RE_GAIN.search(line)
        if m:
            out["gain"] = {"kind": m.group(2), "gain_x": float(m.group(3)),
                           "gain_y": float(m.group(4)), "at": m.group(1),
                           "source": "auto-fit (%s)" % m.group(2)}
            continue
        m = RE_VALIDATION.search(line)
        if m:
            rec = {
                "phase": m.group(2),
                "mean_err_px": float(m.group(3)),
                "mean_err_deg": float(m.group(4)),
                "targets_measured": int(m.group(5)),
                "n_targets": int(m.group(6)),
                "samples_per_target": [int(x) for x in
                                       m.group(7).split(",")],
                "recorded_at": m.group(1),
            }
            if m.group(8):
                rec["geometry"] = {
                    "fullscreen": m.group(8) == "True",
                    "inner": [int(x) for x in m.group(9).split(",")],
                    "offsets": [int(x) for x in m.group(10).split(",")],
                    "dpr": float(m.group(11)),
                }
            if pending_distance and pending_distance["at"] == m.group(1):
                rec["distance"] = dict(pending_distance, measured=True)
                rec["mean_err_deg_measured"] = pending_distance["deg_measured"]
                pending_distance = None
            out["validations"].append(rec)
            continue
        m = RE_RATE.search(line)
        if m:
            out["rate_gates"].append({
                "phase": m.group(2), "hz_sustained": float(m.group(3)),
                "hz_initial": float(m.group(4)), "hz_peak": float(m.group(5)),
                "detected_pct": float(m.group(6)), "at": m.group(1)})
            continue
        m = RE_REC.search(line)
        if m:
            out["stimulus_log"].append({
                "event": m.group(2), "stimulus": m.group(4),
                "at": m.group(1)})
            continue
        m = RE_SAVED.search(line)
        if m:
            out["saved"][m.group(4)] = int(m.group(2))
    return out


def build(session: str, parsed: dict) -> dict:
    """A manifest in the normal shape, flagged as reconstructed."""
    vals = parsed.get("validations") or []
    dist = None
    for phase in ("pre_check", "pre_fit", "pre", "post"):
        for v in vals:
            if v.get("phase") == phase and v.get("distance"):
                dist = dict(v["distance"])
                dist["from_phase"] = phase
                break
        if dist:
            break

    windows = []
    open_at = {}
    for ev in parsed.get("stimulus_log") or []:
        if ev["event"] == "started":
            open_at[ev["stimulus"]] = ev["at"]
        elif ev["event"] == "stopped" and ev["stimulus"] in open_at:
            windows.append({"stimulus": ev["stimulus"],
                            "started_at": open_at.pop(ev["stimulus"]),
                            "stopped_at": ev["at"]})

    return {
        "session_id": session,
        "participant_id": parsed.get("participant"),
        "finalized_at_utc": None,
        "validations": vals,
        "distance": dist,
        "correction": parsed.get("gain"),
        "rate_gates": parsed.get("rate_gates"),
        "stimulus_log": windows,
        "stimulus_order": [w["stimulus"] for w in windows],
        "stimulus_mode": config.SESSION_STIMULUS_MODE,
        "samples_per_stimulus": parsed.get("saved"),
        "quality_thresholds": {
            "max_validation_error_deg": config.MAX_VALIDATION_ERROR_DEG,
            "min_gaze_samples_pct": config.MIN_GAZE_SAMPLES_PCT,
            "min_sampling_hz": config.MIN_SAMPLING_HZ,
            "nominal_sampling_hz": config.NOMINAL_SAMPLING_HZ,
        },
        "reconstructed": {
            "rebuilt_at": datetime.now().isoformat(timespec="seconds"),
            "source": "server log",
            "reason": ("finalisation was interrupted before the manifest "
                       "was written; the session data existed only in the "
                       "log"),
            "absent": [
                "per-target error breakdown (the log records the mean and "
                "the per-target sample counts, not each target's error)",
                "derived event metrics (fixations, saccades) — recompute "
                "from the CSV with backfill_manifests.py",
                "uncorrected/raw degree figures per validation",
            ],
            "warning": ("This manifest was RECONSTRUCTED. It is not "
                        "equivalent to one written by a completed "
                        "session and must be reported as such."),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True)
    ap.add_argument("--log", default=os.path.join(config.DATA_DIR,
                                                  "server.log"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=" * 72)
    print("  REBUILD MANIFEST FROM LOG")
    print("=" * 72)
    print("  session : %s" % args.session)
    print("  log     : %s" % args.log)
    if not os.path.isfile(args.log):
        print("  No such log.")
        return 1

    with open(args.log, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    parsed = _parse(text, args.session)
    if not parsed:
        print("  That session does not appear in the log.")
        return 1

    man = build(args.session, parsed)
    print("  participant : %s" % man["participant_id"])
    print()
    for v in man["validations"]:
        d = v.get("distance") or {}
        print("  %-10s %7.1f px / %.2f deg  %d/%d targets%s"
              % (v["phase"], v["mean_err_px"], v["mean_err_deg"],
                 v["targets_measured"], v["n_targets"],
                 ("  [%.1f cm via %s]" % (d["cm"], d["source"]))
                 if d else ""))
    print()
    for w in man["stimulus_log"]:
        print("  stimulus    %s  %s -> %s"
              % (w["stimulus"], w["started_at"][-8:], w["stopped_at"][-8:]))
    for g in man["rate_gates"]:
        print("  rate gate   %-12s %.1f Hz, %.0f%% detected"
              % (g["phase"], g["hz_sustained"], g["detected_pct"]))
    print()

    if len(man["validations"]) < 3:
        print("  WARNING: only %d validation(s) recovered. A session needs"
              % len(man["validations"]))
        print("  pre_fit, pre_check and post to yield an inclusion figure.")
        print()

    target_dir = config.session_dir_for(args.session) \
        if hasattr(config, "session_dir_for") else config.STUDY_CSV_DIR
    out_path = os.path.join(target_dir, args.session + "_manifest.json")
    if os.path.isfile(out_path):
        print("  A manifest already exists there. Refusing to overwrite:")
        print("    %s" % out_path)
        return 1
    if args.dry_run:
        print("  Nothing written (--dry-run). Would write:")
        print("    %s" % out_path)
        return 0

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)
    print("  Written: %s" % out_path)
    print()
    print("  It is MARKED as reconstructed. Next:")
    print("    python backfill_manifests.py     # derive the event metrics")
    print("    python show_validations.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
