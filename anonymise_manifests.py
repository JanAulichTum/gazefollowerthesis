# -*- coding: utf-8 -*-
"""
Produce committable, de-identified copies of the session manifests.

WHY
---
The manifests hold exactly the metadata a thesis needs to be auditable —
sampling rates, validation errors, gain corrections, per-stimulus quality
— and none of the gaze coordinates. That makes them the right artefact to
version-control. But as written they are NOT de-identified:

    * the FILENAME carries the participant name  (<name>_2026-07-15_...)
    * ``participant_id`` carries it again inside the file
    * ``session_csv`` points at a file named the same way

Git history is permanent and, once pushed, effectively unrecallable. So
this writes SEPARATE, pseudonymised copies to a committable directory and
never modifies the originals.

The name -> pseudonym mapping is written to a key file that is
**gitignored**, because a pseudonym plus a public key file is not
pseudonymisation at all. Keep that file wherever you keep your consent
forms, not in the repo.

Pseudonyms are assigned in order of first session, so P01 is the first
participant recorded. Re-running is stable: an existing key file is
reused, so a participant keeps their pseudonym as new sessions arrive.

Usage::

    python anonymise_manifests.py --dry-run
    python anonymise_manifests.py
    python anonymise_manifests.py --check      # verify no names remain
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE, "data", "gazefollower_raw")
OUT_DIR = os.path.join(BASE, "data", "manifests_anonymised")
# NOT committed — see .gitignore. Holds the reidentification mapping.
KEY_FILE = os.path.join(BASE, "data", "participant_key.json")

# Keys whose values are free text that could name a person or a machine.
_SCRUB_KEYS = ("participant_id", "session_csv", "session_id", "hostname",
               "csv_path", "path", "file", "telemetry_file",
               "python_executable", "cwd")
# Absolute paths embed usernames on both platforms.
_PATH_PATTERNS = (
    (re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\\\"/]+", re.I), r"<WINUSER>"),
    (re.compile(r"/Users/[^/\"]+"), r"/Users/<USER>"),
    (re.compile(r"/home/[^/\"]+"), r"/home/<USER>"),
)


def load_key() -> dict:
    if os.path.isfile(KEY_FILE):
        try:
            with open(KEY_FILE, encoding="utf-8") as fh:
                return json.load(fh).get("mapping", {})
        except (OSError, ValueError):
            pass
    return {}


def save_key(mapping: dict) -> None:
    payload = {
        "warning": "REIDENTIFICATION KEY — never commit, never share. "
                   "Store with the consent forms.",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "mapping": mapping,
    }
    with open(KEY_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def strip_gaze_coordinates(data: dict) -> int:
    """Remove measured gaze positions from validation targets.

    WHY
    ---
    Each validation target records ``tx``/``ty`` (where the dot was) and
    ``mx``/``my`` (where the participant actually looked). The target is
    a fixed grid and carries no information about anyone; the measured
    position is a gaze measurement from a human subject.

    ``err_px`` and ``precision_px`` are DERIVED from those coordinates,
    so removing mx/my costs nothing analytically — the accuracy and
    precision figures a reader needs are all retained. Dropped anyway
    because a public repository is not the place for per-person gaze
    positions, however few.

    Returns the number of targets stripped.
    """
    n = 0
    for record in data.get("validations") or []:
        for target in record.get("targets") or []:
            for key in ("mx", "my"):
                if key in target:
                    del target[key]
                    n += 1
    return n


def _scrub_paths(text: str) -> str:
    for pattern, repl in _PATH_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def scrub(obj, name_map: dict):
    """Recursively replace real names and strip user paths."""
    if isinstance(obj, dict):
        return {k: scrub(v, name_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, name_map) for v in obj]
    if isinstance(obj, str):
        out = obj
        # Longest first, so "annaB" is not half-replaced by "anna".
        for real in sorted(name_map, key=len, reverse=True):
            if real and real in out:
                out = out.replace(real, name_map[real])
        return _scrub_paths(out)
    return obj


def participant_of(path: str) -> str:
    """Participant id from the manifest, falling back to the filename."""
    try:
        with open(path, encoding="utf-8") as fh:
            pid = json.load(fh).get("participant_id")
        if pid:
            return str(pid)
    except (OSError, ValueError):
        pass
    return os.path.basename(path).split("_")[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="scan the output for any surviving real name")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(RAW_DIR, "*_manifest.json")))
    if not files:
        print("No manifests in %s" % RAW_DIR)
        return 1

    mapping = load_key()
    # Assign pseudonyms in order of first session.
    for path in files:
        pid = participant_of(path)
        if pid not in mapping:
            mapping[pid] = "P%02d" % (len(mapping) + 1)

    if args.check:
        bad = []
        for out in sorted(glob.glob(os.path.join(OUT_DIR, "*.json"))):
            text = open(out, encoding="utf-8").read()
            for real in mapping:
                # A pseudonym that contains its own real name would be a
                # false positive; none do, but check the raw name only.
                if re.search(r"\b%s\b" % re.escape(real), text):
                    bad.append((os.path.basename(out), real))
            if "Users" in text and "<" not in text:
                bad.append((os.path.basename(out), "user path"))
            # Measured gaze positions must not survive into a public repo.
            if re.search(r'"m[xy]"\s*:', text):
                bad.append((os.path.basename(out), "measured gaze mx/my"))
        if bad:
            print("LEAKS FOUND — do not commit:")
            for f, what in bad:
                print("  %s contains %r" % (f, what))
            return 1
        print("Checked %d files — no participant names or user paths found."
              % len(glob.glob(os.path.join(OUT_DIR, "*.json"))))
        return 0

    if not args.dry_run:
        os.makedirs(OUT_DIR, exist_ok=True)

    print("%d manifests, %d participants" % (len(files), len(mapping)))
    print()
    written = 0
    for path in files:
        pid = participant_of(path)
        alias = mapping[pid]
        # Keep the timestamp: session ORDER and spacing are analytically
        # meaningful, and a date alone does not identify anyone here.
        rest = os.path.basename(path)
        if rest.startswith(pid + "_"):
            rest = rest[len(pid) + 1:]
        out_name = "%s_%s" % (alias, rest)
        print("  %-44s -> %s" % (os.path.basename(path), out_name))
        if args.dry_run:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            print("    ! skipped: %s" % exc)
            continue
        data = scrub(data, mapping)
        stripped = strip_gaze_coordinates(data)
        data["participant_id"] = alias
        data["anonymised"] = {
            "tool": "anonymise_manifests.py",
            "at": datetime.now().isoformat(timespec="seconds"),
            "note": "Pseudonymised copy. The reidentification key is held "
                    "outside version control.",
            "gaze_coordinates_removed": stripped,
            "retained": "Validation targets keep tx/ty (the fixed grid), "
                        "err_px and precision_px. The measured gaze "
                        "positions (mx/my) they were computed from are "
                        "removed.",
        }
        with open(os.path.join(OUT_DIR, out_name), "w",
                  encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        written += 1

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    save_key(mapping)
    print()
    print("Wrote %d files to %s" % (written, os.path.relpath(OUT_DIR, BASE)))
    print("Key written to %s — GITIGNORED, keep it with your consent forms."
          % os.path.relpath(KEY_FILE, BASE))
    print()
    print("Verify before committing:  python anonymise_manifests.py --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
