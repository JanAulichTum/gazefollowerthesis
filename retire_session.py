# -*- coding: utf-8 -*-
"""Retire a recorded session. Move it aside with a reason; never delete it.

WHY NOT JUST DELETE
-------------------
A session that disappears leaves a gap in the record, and a gap invites
exactly the question you cannot answer later: was that participant
dropped because the equipment failed, or because the numbers were
inconvenient? The distinction is the whole difference between a clean
exclusion and a suspect one, and it cannot be reconstructed from an
absence.

So a retired session keeps its files, gains a dated reason, and moves to
``data/retired/``, which no analysis tool reads. The registry file lists
every retirement in order, so the count of sessions recorded and the
count analysed can both be stated and reconciled.

The reason is REQUIRED. A retirement without one is the thing this
script exists to prevent.

RE-RECORDING THE SAME PERSON
----------------------------
If the participant is recorded again, say so with ``--rerecorded-as``.
The second session is not equivalent to a first: they have already seen
the stimuli, and prior exposure changes what a viewer notices - which is
the very construct under study here. That is a limitation to declare,
not a detail, and it is only declarable if someone wrote it down at the
time.

Usage::

    python retire_session.py --list
    python retire_session.py Jan_First_Participant_2026-08-13_154903 \\
        --reason "recorded on a build where the iris distance silently
                  fell back to the inter-ocular ruler" \\
        --rerecorded-as P01_2026-08-13_161500
    python retire_session.py <session> --reason "..." --dry-run
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import config

RETIRED_DIR = os.path.join(config.DATA_DIR, "retired")
REGISTRY = os.path.join(RETIRED_DIR, "REGISTRY.md")


def _session_files(stem: str) -> list:
    """Every file belonging to a session, wherever it was written."""
    hits = []
    roots = [config.STUDY_CSV_DIR, config.GAZEFOLLOWER_CSV_DIR,
             config.DATA_DIR, config.LLM_LOG_DIR,
             os.path.join(config.DATA_DIR, "coding"),
             os.path.join(config.DATA_DIR, "llm_replay")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path in glob.glob(os.path.join(root, "*")):
            if os.path.isfile(path) and stem in os.path.basename(path):
                hits.append(path)
    return sorted(set(hits))


def _stem_of(arg: str) -> str:
    base = os.path.basename(arg)
    for suffix in ("_manifest.json", ".csv", ".json"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


def list_retired() -> int:
    if not os.path.isdir(RETIRED_DIR):
        print("  Nothing retired.")
        return 0
    entries = sorted(d for d in os.listdir(RETIRED_DIR)
                     if os.path.isdir(os.path.join(RETIRED_DIR, d)))
    if not entries:
        print("  Nothing retired.")
        return 0
    print("  %d retired session(s):" % len(entries))
    for name in entries:
        note = os.path.join(RETIRED_DIR, name, "retired.json")
        reason = "?"
        rerec = None
        if os.path.isfile(note):
            try:
                with open(note, encoding="utf-8") as fh:
                    rec = json.load(fh)
                reason = rec.get("reason", "?")
                rerec = rec.get("rerecorded_as")
            except (OSError, ValueError):
                pass
        print()
        print("    %s" % name)
        print("      %s" % reason)
        if rerec:
            print("      re-recorded as %s — PRIOR EXPOSURE to the stimuli"
                  % rerec)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", nargs="?",
                    help="session id, or any file belonging to it")
    ap.add_argument("--reason", help="why it is being retired (REQUIRED)")
    ap.add_argument("--rerecorded-as", dest="rerecorded_as",
                    help="session id of the repeat recording, if any")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.list:
        return list_retired()
    if not args.session:
        print("  Which session? Use --list to see what exists.")
        return 1
    if not args.reason or len(args.reason.strip()) < 15:
        print("  --reason is required, and must actually say something.")
        print("  A retirement with no stated cause is indistinguishable")
        print("  from dropping data that did not suit.")
        return 1

    stem = _stem_of(args.session)
    files = _session_files(stem)
    print("=" * 70)
    print("  RETIRE  %s" % stem)
    print("=" * 70)
    if not files:
        print("  No files found for that session.")
        return 1
    for f in files:
        print("    %s" % os.path.relpath(f, config.BASE_DIR))
    print()
    print("  reason : %s" % args.reason.strip())
    if args.rerecorded_as:
        print("  repeat : %s" % args.rerecorded_as)
    print()

    if args.dry_run:
        print("  Nothing moved (--dry-run).")
        return 0

    dest = os.path.join(RETIRED_DIR, stem)
    os.makedirs(dest, exist_ok=True)
    moved = []
    for f in files:
        target = os.path.join(dest, os.path.basename(f))
        shutil.move(f, target)
        moved.append(os.path.relpath(f, config.BASE_DIR))

    rec = {
        "session": stem,
        "retired_at": datetime.now().isoformat(timespec="seconds"),
        "reason": args.reason.strip(),
        "rerecorded_as": args.rerecorded_as,
        "files": moved,
        "note": ("Retired, not deleted. The recording exists and can be "
                 "inspected; it is excluded from analysis because of the "
                 "reason above."),
    }
    with open(os.path.join(dest, "retired.json"), "w",
              encoding="utf-8") as fh:
        json.dump(rec, fh, indent=2)

    os.makedirs(RETIRED_DIR, exist_ok=True)
    new = not os.path.isfile(REGISTRY)
    with open(REGISTRY, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# Retired sessions\n\n"
                     "Recorded, then excluded from analysis. Each entry "
                     "states why, at the time.\n"
                     "Nothing here is deleted.\n\n")
        fh.write("## %s\n\n" % stem)
        fh.write("- retired: %s\n" % rec["retired_at"])
        fh.write("- reason: %s\n" % rec["reason"])
        if args.rerecorded_as:
            fh.write("- re-recorded as: %s\n" % args.rerecorded_as)
            fh.write("- **the repeat is not a first viewing**: this "
                     "participant had already seen the stimuli, and prior "
                     "exposure changes what a viewer notices. Declare it.\n")
        fh.write("- files: %d\n\n" % len(moved))

    print("  Moved %d file(s) to data/retired/%s/" % (len(moved), stem))
    print("  Registry: data/retired/REGISTRY.md")
    print()
    print("  No analysis tool reads data/retired/, so the session is out")
    print("  of every count and every mean from here on.")
    if args.rerecorded_as:
        print()
        print("  RECORD THIS IN THE THESIS: %s has already seen the"
              % args.rerecorded_as)
        print("  stimuli. It is not a first-exposure session and cannot")
        print("  be treated as one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
