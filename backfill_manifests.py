# -*- coding: utf-8 -*-
"""
Recompute quality metrics in existing session manifests.

WHY THIS EXISTS
---------------
Manifests written before 2026-07-31 contain a ``gaze_samples_pct`` that
was computed against the recording's OWN median frame interval::

    expected = duration / median_dt        # <- derived from the data

Frame decimation therefore cancelled out of numerator and denominator and
every session scored ~100 %, including sessions that were running at half
rate. The pilot manifests reported 87–100 % where the true Tobii-style
figures are 48–92 % — several sessions FAIL the preregistered >= 60 %
threshold that they previously "passed".

This tool recomputes the affected fields from the raw session CSVs using
a fixed nominal rate (``NOMINAL_SAMPLING_HZ``), and adds the rate-shape
diagnostics. **Nothing is overwritten destructively**: the original file
is copied to ``<name>_manifest.pre-backfill.json`` first, and the old
values are preserved inside the manifest under ``legacy_data_quality``
so the correction itself stays auditable for the thesis.

Usage::

    python backfill_manifests.py --dry-run     # show what would change
    python backfill_manifests.py               # apply
    python backfill_manifests.py --dry-run one_manifest.json

Re-running is safe (idempotent): already-backfilled manifests are
recomputed from the CSV again, not from the previously corrected values.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

import numpy as np
import pandas as pd

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


try:
    from config import (MIN_GAZE_SAMPLES_PCT, MIN_SAMPLING_HZ,
                        NOMINAL_SAMPLING_HZ, RATE_MULTIMODAL_RATIO)
except ImportError:      # standalone use outside the project venv
    MIN_GAZE_SAMPLES_PCT, MIN_SAMPLING_HZ = 60.0, 20.0
    NOMINAL_SAMPLING_HZ, RATE_MULTIMODAL_RATIO = 30.0, 1.5

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE, "data", "gazefollower_raw")
BACKFILL_VERSION = "2026-07-31-nominal-rate"


def recompute(manifest: dict, df: pd.DataFrame) -> "tuple[dict, list[str]]":
    """Return (updated manifest, human-readable change log)."""
    changes: list[str] = []
    nominal_dt_ns = 1e9 / NOMINAL_SAMPLING_HZ if NOMINAL_SAMPLING_HZ else 0.0
    session_dt_ns = float(df["timestamp"].sort_values().diff().median()) \
        if len(df) > 1 else 0.0

    quality = dict(manifest.get("data_quality") or {})
    legacy = manifest.get("legacy_data_quality")
    if legacy is None:
        # First backfill of this file — keep the original numbers so the
        # correction remains auditable.
        legacy = {k: dict(v) for k, v in quality.items()}

    for entry in manifest.get("stimuli", []):
        stim = entry.get("stimulus")
        if stim is None:
            continue
        seg = df[
            (df["timestamp"] >= entry["t_start_ns"])
            & (df["timestamp"] <= entry["t_end_ns"])
        ]
        if seg.empty:
            continue
        dur_ns = entry["t_end_ns"] - entry["t_start_ns"]
        valid = int((seg["status"] == 1).sum()) \
            if "status" in seg.columns else len(seg)
        expected = (dur_ns / nominal_dt_ns) if nominal_dt_ns > 0 else 0.0
        expected_rel = (dur_ns / session_dt_ns) if session_dt_ns > 0 else 0.0

        gaps = seg["timestamp"].sort_values().diff().dropna()
        dt_med = float(gaps.median()) if len(gaps) else 0.0
        dt_p10 = float(gaps.quantile(0.10)) if len(gaps) else 0.0
        seg_hz = round(1e9 / dt_med, 1) if dt_med > 0 else 0.0
        ratio = (dt_med / dt_p10) if dt_p10 > 0 else 1.0

        new_pct = round(min(100.0, 100 * valid / expected), 1) \
            if expected else 0.0
        old_pct = (quality.get(stim) or {}).get("gaze_samples_pct")

        q = dict(quality.get(stim) or {})
        q.update({
            "samples": len(seg),
            "valid_samples": valid,
            "valid_pct": round(100 * valid / len(seg), 1) if len(seg) else 0.0,
            "gaze_samples_pct": new_pct,
            "nominal_sampling_hz": NOMINAL_SAMPLING_HZ,
            "relative_yield_pct": round(
                min(100.0, 100 * valid / expected_rel), 1)
            if expected_rel else 0.0,
            "sampling_hz": seg_hz,
            "sampling_hz_fastest": round(1e9 / dt_p10, 1) if dt_p10 > 0 else 0.0,
            "rate_ratio": round(ratio, 2),
            "rate_multimodal": bool(ratio >= RATE_MULTIMODAL_RATIO),
            "low_sampling_rate": bool(seg_hz and seg_hz < MIN_SAMPLING_HZ),
            "passes_gaze_samples_threshold": new_pct >= MIN_GAZE_SAMPLES_PCT,
        })
        quality[stim] = q

        if old_pct is not None and abs(old_pct - new_pct) >= 0.1:
            was = "PASS" if old_pct >= MIN_GAZE_SAMPLES_PCT else "FAIL"
            now = "PASS" if new_pct >= MIN_GAZE_SAMPLES_PCT else "FAIL"
            flip = "  <<< VERDICT FLIPPED" if was != now else ""
            changes.append(
                "    %-24s gaze samples %5.1f%% (%s) -> %5.1f%% (%s)%s"
                % (stim[:24], old_pct, was, new_pct, now, flip))
        if q["rate_multimodal"]:
            changes.append(
                "    %-24s MULTIMODAL rate: median %.1f Hz, fastest %.1f Hz"
                % (stim[:24], seg_hz, q["sampling_hz_fastest"]))

    manifest["data_quality"] = quality
    manifest["legacy_data_quality"] = legacy
    thresholds = dict(manifest.get("quality_thresholds") or {})
    thresholds["nominal_sampling_hz"] = NOMINAL_SAMPLING_HZ
    manifest["quality_thresholds"] = thresholds
    manifest["backfill"] = {
        "version": BACKFILL_VERSION,
        "note": "gaze_samples_pct recomputed against a fixed nominal rate; "
                "the original self-referential values are kept in "
                "legacy_data_quality",
    }
    return manifest, changes


def process(path: str, dry_run: bool) -> bool:
    """Backfill one manifest. Returns True if it changed."""
    csv_path = path.replace("_manifest.json", ".csv")
    if not os.path.isfile(csv_path):
        print("  ! no CSV for %s — skipped" % os.path.basename(path))
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        df = pd.read_csv(csv_path)
    except (OSError, ValueError) as exc:
        print("  ! could not read %s: %s" % (os.path.basename(path), exc))
        return False
    if df.empty or "timestamp" not in df.columns:
        print("  ! empty/malformed CSV for %s" % os.path.basename(path))
        return False

    manifest, changes = recompute(manifest, df)
    print("  %s" % os.path.basename(path))
    for line in changes:
        print(line)
    if not changes:
        print("    (no metric changes)")

    if dry_run:
        return bool(changes)
    backup = path.replace(".json", ".pre-backfill.json")
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, path)          # atomic
    return bool(changes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifests", nargs="*",
                    help="manifest paths (default: all in data/gazefollower_raw)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report changes without writing")
    args = ap.parse_args()

    paths = args.manifests or sorted(
        glob.glob(os.path.join(RAW_DIR, "*_manifest.json")))
    paths = [p for p in paths if not p.endswith(".pre-backfill.json")]
    if not paths:
        print("No manifests found in", RAW_DIR)
        return 1

    print("Nominal rate: %.0f Hz | thresholds: >= %.0f%% gaze samples, "
          ">= %.0f Hz\n" % (NOMINAL_SAMPLING_HZ, MIN_GAZE_SAMPLES_PCT,
                            MIN_SAMPLING_HZ))
    print("DRY RUN — nothing will be written\n" if args.dry_run else "")
    changed = sum(process(p, args.dry_run) for p in paths)
    print("\n%d of %d manifests %s."
          % (changed, len(paths),
             "would change" if args.dry_run else "updated"))
    if not args.dry_run and changed:
        print("Originals preserved as *_manifest.pre-backfill.json; the old "
              "values also live in each manifest's legacy_data_quality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
