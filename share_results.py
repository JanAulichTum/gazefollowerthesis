# -*- coding: utf-8 -*-
"""
Publish the small analysis artefacts so results can be read from GitHub.

WHAT IT SHARES, AND WHAT IT DOES NOT
------------------------------------
Everything needed to diagnose a session is small JSON. The raw gaze CSVs
are large, the stimulus videos are not yours to redistribute, and the
LLM replay payloads contain base64 frames — so none of those go.

    shared     session manifests, human coding verdicts, the structured
               LLM claims, telemetry summaries
    NOT shared raw gaze CSVs, videos, base64 payloads, API keys

PARTICIPANT NAMES
-----------------
Pseudonymised by DEFAULT, and the reason is not your privacy — it is
that the other people in these recordings did not agree to have their
first names on a public repository, and a session label like
"JULE_HFP_06.08" is a name. The mapping is written to a file that stays
local, so you can still tell who is who.

``--real-names`` turns it off if you have their consent. That is a
decision to make once, deliberately, rather than by default.

Usage::

    python share_results.py                 # refresh data/shared/
    python share_results.py --real-names
    python share_results.py --list          # show what would be shared
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE, "data", "gazefollower_raw")
CODING_DIR = os.path.join(BASE, "data", "coding")
LLM_DIR = os.path.join(BASE, "data", "llm_logs")
SHARED = os.path.join(BASE, "data", "shared")
MAP_FILE = os.path.join(BASE, "data", "share_pseudonyms.json")

#: Keys that must never leave the machine, whatever else changes.
FORBIDDEN_KEYS = {"api_key", "gemini_api_key", "secret_key", "b64",
                  "crop_b64", "inline_data", "inlineData", "request_parts"}


def _replace_names(text: str, mapping: dict) -> str:
    """Swap every label, LONGEST FIRST.

    Order matters and getting it wrong is silent: with both "Test" and
    "Test1" in the map, replacing "Test" first turns "Test1" into
    "P-532E1" — a pseudonym with a digit glued on, which is neither the
    real name nor a stable ID, and which differs from what the file
    CONTENTS were given. Longest-first makes the longer label win.
    """
    for real in sorted(mapping, key=len, reverse=True):
        if real:
            text = text.replace(real, mapping[real])
    return text


def _pseudonym(name: str, mapping: dict) -> str:
    """Stable pseudonym for a participant label.

    Stable, so the same person keeps the same ID across runs and the
    shared files stay comparable over time. Derived from a hash rather
    than a counter for the same reason: adding a participant must not
    renumber everyone recorded before them.
    """
    if name in mapping:
        return mapping[name]
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()[:4].upper()
    mapping[name] = "P-%s" % h
    return mapping[name]


def _scrub(obj, mapping: dict, real_names: bool):
    """Recursively drop forbidden keys and swap participant labels."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                out[k] = "<omitted>"
                continue
            out[k] = _scrub(v, mapping, real_names)
        return out
    if isinstance(obj, list):
        return [_scrub(v, mapping, real_names) for v in obj]
    if isinstance(obj, str) and not real_names:
        return _replace_names(obj, mapping)
    return obj


def _collect_names() -> list:
    """Participant labels appearing in session filenames and manifests."""
    names = set()
    for path in glob.glob(os.path.join(RAW_DIR, "*_manifest.json")):
        stem = os.path.basename(path).replace("_manifest.json", "")
        # "<label>_YYYY-MM-DD_HHMMSS" — take everything before the date.
        m = re.match(r"(.+?)_\d{4}-\d{2}-\d{2}_", stem)
        if m:
            names.add(m.group(1))
        try:
            with open(path, encoding="utf-8") as fh:
                pid = (json.load(fh) or {}).get("participant_id")
            if pid:
                names.add(str(pid))
        except (OSError, ValueError):
            pass
    return sorted(names)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--real-names", action="store_true",
                    help="publish participant labels as recorded")
    ap.add_argument("--list", action="store_true",
                    help="show what would be shared, write nothing")
    args = ap.parse_args()

    mapping = {}
    if os.path.isfile(MAP_FILE):
        try:
            with open(MAP_FILE, encoding="utf-8") as fh:
                mapping = json.load(fh)
        except (OSError, ValueError):
            mapping = {}
    for name in _collect_names():
        _pseudonym(name, mapping)

    sources = [
        ("manifests", sorted(glob.glob(
            os.path.join(RAW_DIR, "*_manifest.json")))),
        ("coding", sorted(glob.glob(os.path.join(CODING_DIR, "*.json")))),
        ("telemetry", sorted(glob.glob(
            os.path.join(BASE, "data", "*_telemetry.json")))),
    ]

    print("=" * 70)
    print("  SHARE RESULTS  ->  data/shared/")
    print("=" * 70)
    print("  names: %s" % ("AS RECORDED (--real-names)" if args.real_names
                           else "pseudonymised (%d labels)" % len(mapping)))
    print()

    total = 0
    for label, paths in sources:
        print("  %-10s %d file(s)" % (label, len(paths)))
        if args.list:
            for p in paths[:5]:
                print("      %s" % os.path.basename(p))
            if len(paths) > 5:
                print("      ... and %d more" % (len(paths) - 5))
            continue
        out_dir = os.path.join(SHARED, label)
        os.makedirs(out_dir, exist_ok=True)
        for p in paths:
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            name = os.path.basename(p)
            if not args.real_names:
                name = _replace_names(name, mapping)
            with open(os.path.join(out_dir, name), "w",
                      encoding="utf-8") as fh:
                json.dump(_scrub(data, mapping, args.real_names), fh,
                          indent=2)
            total += 1

    # The LLM claims, without the base64 frames that make the logs huge.
    if not args.list:
        out_dir = os.path.join(SHARED, "llm_claims")
        os.makedirs(out_dir, exist_ok=True)
        for p in sorted(glob.glob(os.path.join(LLM_DIR,
                                               "*evaluation_run_*.json"))):
            try:
                with open(p, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            slim = {k: d.get(k) for k in
                    ("step", "model", "generation_config", "finish_reason",
                     "requested_at_utc", "context", "response_text")}
            name = os.path.basename(p)
            if not args.real_names:
                name = _replace_names(name, mapping)
            with open(os.path.join(out_dir, name), "w",
                      encoding="utf-8") as fh:
                json.dump(_scrub(slim, mapping, args.real_names), fh,
                          indent=2)
            total += 1

    if args.list:
        print()
        print("  (nothing written — drop --list to publish)")
        return 0

    # The mapping stays LOCAL. It is the only thing that can undo the
    # pseudonymisation, so publishing it would make the whole step
    # theatre.
    with open(MAP_FILE, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2)

    with open(os.path.join(SHARED, "README.md"), "w",
              encoding="utf-8") as fh:
        fh.write(
            "# Shared analysis artefacts\n\n"
            "Written by `share_results.py`. Small JSON only: session\n"
            "manifests, human coding verdicts, structured LLM claims and\n"
            "telemetry summaries.\n\n"
            "NOT here: raw gaze CSVs, stimulus videos, base64 frames,\n"
            "API keys.\n\n"
            "Participant labels are %s.\n"
            % ("as recorded" if args.real_names
               else "pseudonymised; the mapping is not published"))

    print()
    print("  %d file(s) written to data/shared/" % total)
    print("  pseudonym map (LOCAL, not published): %s"
          % os.path.relpath(MAP_FILE, BASE))
    print()
    print("  Commit and push, then the results are readable from GitHub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
