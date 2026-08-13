# -*- coding: utf-8 -*-
"""Cut the stimulus clips to the protocol duration, on whichever machine.

WHY THIS IS A SCRIPT AND NOT A ONE-OFF
--------------------------------------
The clips are not in the repository - they are third-party classroom
video and the repository is public - so a cut made on one machine cannot
travel to another. Only the *procedure* can. Running this on the
recording machine produces clips that are byte-identical in duration and
encoding settings to a cut made anywhere else, which is what "the same
stimulus for every participant" actually requires.

WHY 30 SECONDS
--------------
Not an aesthetic choice. At the pilot fixation rate of ~2.33 per second,
two clips of length T produce roughly 4.7*T fixations between them, and
the feedback step sends one keyframe per fixation against a cap of
LLM_MAX_FRAMES. The cap therefore begins to bind above ~86 s of total
stimulus, and when it binds it drops the SHORTEST fixations silently -
the failure that produced 88 % correct coding before the cut-off and
0 % after it. Two 30 s clips give ~140 keyframes against a cap of 200.

WHAT IT DOES
------------
For every playable video in the stimulus folder that is longer than the
target, writes a cut copy alongside it and moves the original into
``full_originals/``, which the app does not look into. Already-cut
clips are left alone, so running it twice is safe.

A record of every cut - source, start, duration, encoder - is written to
``cut_provenance.json`` beside the clips, because "which 30 seconds"
is a methods question that has to be answerable later.

Usage::

    python cut_stimuli.py                      # first 30 s of each
    python cut_stimuli.py --seconds 30
    python cut_stimuli.py --start 45           # 45 s -> 75 s, all clips
    python cut_stimuli.py --start Stimuli_1.mp4=12,Stimuli_5.mp4=63
    python cut_stimuli.py --dry-run            # say what it would do
    python cut_stimuli.py --restore            # put the originals back
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import config

ORIGINALS_DIRNAME = "full_originals"
PROVENANCE = "cut_provenance.json"
SUFFIX = "_{n}s"


def _ffmpeg() -> "str | None":
    """ffmpeg on PATH, or the copy bundled with imageio-ffmpeg.

    Windows rarely has ffmpeg installed, but imageio-ffmpeg ships a
    binary and is already a transitive dependency here. Preferring the
    system one keeps behaviour predictable where it does exist.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def _duration_s(path: str, ffmpeg: str) -> "float | None":
    """Clip length in seconds, via ffprobe or by decoding with OpenCV."""
    probe = shutil.which("ffprobe")
    if probe:
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=60)
            return float(out.stdout.strip())
        except Exception:  # noqa: BLE001
            pass
    try:
        import cv2

        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        if fps > 0 and frames > 0:
            return frames / fps
    except Exception:  # noqa: BLE001
        pass
    return None


def _parse_starts(spec: str) -> "tuple[float, dict]":
    """Either one number for every clip, or name=seconds pairs."""
    spec = (spec or "").strip()
    if not spec:
        return 0.0, {}
    if "=" not in spec:
        return float(spec), {}
    per = {}
    for chunk in spec.replace(";", ",").split(","):
        if not chunk.strip():
            continue
        name, _, val = chunk.partition("=")
        per[name.strip()] = float(val)
    return 0.0, per


def restore() -> int:
    """Move the originals back and delete the cut copies."""
    src = os.path.join(config.STIMULI_DIR, ORIGINALS_DIRNAME)
    if not os.path.isdir(src):
        print("  Nothing to restore - no %s/ folder." % ORIGINALS_DIRNAME)
        return 1
    moved = 0
    for name in sorted(os.listdir(src)):
        dest = os.path.join(config.STIMULI_DIR, name)
        if os.path.exists(dest):
            print("  %s already present, leaving the copy in place" % name)
            continue
        shutil.move(os.path.join(src, name), dest)
        print("  restored %s" % name)
        moved += 1
    # Remove the cuts, identified from the provenance record rather than
    # by guessing at filenames.
    rec_path = os.path.join(config.STIMULI_DIR, PROVENANCE)
    if os.path.isfile(rec_path):
        try:
            with open(rec_path, encoding="utf-8") as fh:
                for entry in json.load(fh).get("cuts", []):
                    cut = os.path.join(config.STIMULI_DIR, entry["output"])
                    if os.path.isfile(cut):
                        os.remove(cut)
                        print("  removed %s" % entry["output"])
        except (OSError, ValueError):
            pass
        os.remove(rec_path)
    print()
    print("  %d original(s) restored." % moved)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="target clip length (default 30)")
    ap.add_argument("--start", default="0",
                    help="start offset in seconds, or NAME=SECONDS pairs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true",
                    help="undo: move full_originals/ back and drop the cuts")
    args = ap.parse_args()

    print("=" * 68)
    print("  CUT STIMULI TO %.0f s" % args.seconds)
    print("=" * 68)
    print("  folder : %s" % config.STIMULI_DIR)

    if not os.path.isdir(config.STIMULI_DIR):
        print("  That folder does not exist.")
        return 1
    if args.restore:
        return restore()

    ffmpeg = _ffmpeg()
    if not ffmpeg:
        print("  No ffmpeg available, and imageio-ffmpeg is not installed.")
        print("  Fix with:  pip install imageio-ffmpeg")
        return 1
    print("  ffmpeg : %s" % ffmpeg)

    default_start, per_file = _parse_starts(args.start)
    clips = config.discover_stimuli()
    if not clips:
        print("  No playable stimuli found.")
        return 1

    originals_dir = os.path.join(config.STIMULI_DIR, ORIGINALS_DIRNAME)
    cuts, skipped = [], []
    print()

    for name in clips:
        path = os.path.join(config.STIMULI_DIR, name)
        dur = _duration_s(path, ffmpeg)
        start = per_file.get(name, default_start)
        if dur is None:
            print("  %-28s duration unreadable - SKIPPED" % name)
            skipped.append(name)
            continue
        # Half a second of slack: a clip cut to 30 s can measure 30.02.
        if dur <= args.seconds + 0.5 and start <= 0:
            print("  %-28s %.1f s - already at length, left alone"
                  % (name, dur))
            skipped.append(name)
            continue
        if start + args.seconds > dur + 0.5:
            print("  %-28s %.1f s - start %.1f s leaves less than %.0f s, "
                  "SKIPPED" % (name, dur, start, args.seconds))
            skipped.append(name)
            continue

        stem, ext = os.path.splitext(name)
        out_name = stem + SUFFIX.format(n=int(args.seconds)) + ext
        out_path = os.path.join(config.STIMULI_DIR, out_name)
        print("  %-28s %.1f s -> %s  [%.0f s .. %.0f s]"
              % (name, dur, out_name, start, start + args.seconds))
        if args.dry_run:
            continue

        # Re-encode rather than stream-copy: a copy cut lands on the
        # nearest keyframe, so the clips would differ in length and in
        # starting frame between machines - which is precisely what
        # "identical stimulus for every participant" forbids.
        cmd = [ffmpeg, "-v", "error", "-y",
               "-ss", "%.3f" % start, "-t", "%.3f" % args.seconds,
               "-i", path,
               "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
               "-c:a", "aac", "-movflags", "+faststart", out_path]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=900)
        except Exception as exc:  # noqa: BLE001
            print("      ffmpeg failed: %s" % exc)
            return 1
        if res.returncode != 0 or not os.path.isfile(out_path):
            print("      ffmpeg failed: %s" % (res.stderr or "")[:300])
            return 1

        os.makedirs(originals_dir, exist_ok=True)
        shutil.move(path, os.path.join(originals_dir, name))
        cuts.append({
            "source": name,
            "output": out_name,
            "start_s": start,
            "duration_s": args.seconds,
            "source_duration_s": round(dur, 3),
            "encoder": "libx264 crf 18 veryfast, aac",
            "cut_at": datetime.now().isoformat(timespec="seconds"),
        })

    if args.dry_run:
        print()
        print("  Nothing written (--dry-run).")
        return 0

    if cuts:
        # WHICH 30 seconds is a methods question. Recording it here means
        # it can be answered later without rewatching anything.
        rec = {"target_seconds": args.seconds, "cuts": cuts}
        with open(os.path.join(config.STIMULI_DIR, PROVENANCE), "w",
                  encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2)

    print()
    print("  %d cut, %d left alone." % (len(cuts), len(skipped)))
    remaining = config.discover_stimuli()
    print("  The app now sees: %s" % (", ".join(remaining) or "nothing"))
    if len(remaining) != 2:
        print()
        print("  NOTE: the protocol specifies exactly 2 stimuli.")
    if cuts:
        print()
        print("  Originals kept in %s/ - the app does not look there."
              % ORIGINALS_DIRNAME)
        print("  Provenance written to %s" % PROVENANCE)
        print()
        print("  WATCH BOTH CUTS before recording. The two clips are meant")
        print("  to differ in visual crowding; if the crowded stretch of")
        print("  either one falls outside the window taken, the")
        print("  manipulation is gone. Recut with:")
        print("      python cut_stimuli.py --restore")
        print("      python cut_stimuli.py --start NAME=SECONDS,NAME=SECONDS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
