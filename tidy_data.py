# -*- coding: utf-8 -*-
"""
One-off data tidy-up / migration.

Run with:  python tidy_data.py            (dry run — shows the plan)
           python tidy_data.py --apply    (performs the changes)

What it does:
1. Renames legacy raw session files
       <participant>__session_<epoch-ns>.csv
   to the current human-readable scheme
       <participant>_<YYYY-MM-DD>_<HHMMSS>.csv
   (timestamp decoded from the epoch nanoseconds), together with their
   ``*_manifest.json`` files. Manifests are updated to reference the
   new CSV name.
2. Rewrites the ``session_id`` column in ``gazefollower_data.xlsx`` so
   existing rows keep pointing at the renamed sessions (the review tool
   and the LLM pipeline look sessions up by this ID).
3. Applies the house Excel style (bold header, frozen row, filter,
   column widths) to all workbooks.
4. Moves clutter (Office lock files ``~$…``, ``*.backup.xlsx``,
   ``*.corrupt-*``) into ``data/archive/``.

Nothing is deleted; every change is printed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime

import pandas as pd

from config import DATA_DIR, GAZEFOLLOWER_CSV_DIR, GAZEFOLLOWER_DATA_FILE
from excel_style import style_workbook

LEGACY_RE = re.compile(r"^(?P<pid>.+?)__session_(?P<ns>\d{16,20})\.csv$")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")
CLUTTER_PREFIXES = ("~$",)
CLUTTER_MARKERS = (".backup.xlsx", ".corrupt-", ".tmp.xlsx")


def plan_renames() -> "list[tuple[str, str]]":
    """(old_base, new_base) for every legacy raw CSV, collision-safe."""
    renames: list[tuple[str, str]] = []
    taken: set[str] = set(
        os.path.splitext(f)[0]
        for f in os.listdir(GAZEFOLLOWER_CSV_DIR)
        if f.endswith(".csv") and not LEGACY_RE.match(f)
    )
    for fname in sorted(os.listdir(GAZEFOLLOWER_CSV_DIR)):
        m = LEGACY_RE.match(fname)
        if not m:
            continue
        ts = datetime.fromtimestamp(int(m.group("ns")) / 1e9)
        base = "%s_%s" % (m.group("pid"), ts.strftime("%Y-%m-%d_%H%M%S"))
        candidate, n = base, 2
        while candidate in taken:
            candidate = "%s_run%d" % (base, n)
            n += 1
        taken.add(candidate)
        renames.append((os.path.splitext(fname)[0], candidate))
    return renames


def main() -> None:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY RUN (use --apply to perform)"
    print("Tidying %s  [%s]\n" % (DATA_DIR, mode))

    # ── 1+2. Rename raw sessions & remap session_id in the workbook ──
    renames = plan_renames()
    if not renames:
        print("• Raw sessions: nothing to rename (already tidy).")
    for old, new in renames:
        print("• Rename session: %s → %s" % (old, new))
        if not apply:
            continue
        for suffix in (".csv", "_manifest.json"):
            src = os.path.join(GAZEFOLLOWER_CSV_DIR, old + suffix)
            dst = os.path.join(GAZEFOLLOWER_CSV_DIR, new + suffix)
            if os.path.isfile(src):
                os.replace(src, dst)
        manifest_path = os.path.join(GAZEFOLLOWER_CSV_DIR,
                                     new + "_manifest.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, encoding="utf-8") as fh:
                    manifest = json.load(fh)
                manifest["session_csv"] = new + ".csv"
                manifest["renamed_from"] = old + ".csv"
                with open(manifest_path, "w", encoding="utf-8") as fh:
                    json.dump(manifest, fh, indent=2)
            except (OSError, ValueError) as exc:
                print("  ! manifest update failed: %s" % exc)

    if os.path.isfile(GAZEFOLLOWER_DATA_FILE):
        df = pd.read_excel(GAZEFOLLOWER_DATA_FILE)
        mapping = dict(renames)
        n_mapped = 0
        if "session_id" in df.columns and mapping:
            mask = df["session_id"].astype(str).isin(mapping)
            n_mapped = int(mask.sum())
            df.loc[mask, "session_id"] = (
                df.loc[mask, "session_id"].astype(str).map(mapping)
            )
        print("• gazefollower_data.xlsx: %d rows remapped to new "
              "session names" % n_mapped)
        if apply:
            # Readability: trim sub-pixel noise (raw CSVs keep originals)
            for col in df.columns:
                if "gaze_position" in col:
                    df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
            tmp = GAZEFOLLOWER_DATA_FILE + ".tmp.xlsx"
            df.to_excel(tmp, index=False)
            style_workbook(tmp)
            os.replace(tmp, GAZEFOLLOWER_DATA_FILE)

    # ── 3. Style the remaining workbooks ──
    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith(".xlsx"):
            continue
        if fname.startswith(CLUTTER_PREFIXES) \
                or any(m in fname for m in CLUTTER_MARKERS):
            continue
        if fname == os.path.basename(GAZEFOLLOWER_DATA_FILE):
            continue  # already handled above
        path = os.path.join(DATA_DIR, fname)
        print("• Style workbook: %s" % fname)
        if apply:
            style_workbook(path)

    # ── 4. Archive clutter ──
    for fname in sorted(os.listdir(DATA_DIR)):
        if fname.startswith(CLUTTER_PREFIXES) \
                or any(m in fname for m in CLUTTER_MARKERS):
            print("• Archive clutter: %s → archive/" % fname)
            if apply:
                os.makedirs(ARCHIVE_DIR, exist_ok=True)
                shutil.move(os.path.join(DATA_DIR, fname),
                            os.path.join(ARCHIVE_DIR, fname))

    print("\nDone." if apply else "\nDry run complete — nothing changed.")


if __name__ == "__main__":
    main()
