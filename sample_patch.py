# -*- coding: utf-8 -*-
"""
Fix GazeFollower silently DROPPING every frame where detection fails.

THE BUG
-------
``GazeFollower.process_frame`` dispatches every frame to its subscribers,
including frames where face alignment or gaze estimation failed. On such
a frame ``gaze_info.raw_gaze_coordinates`` is still ``None``, and
``_gaze_info_2_string`` does::

    f"{gaze_info.raw_gaze_coordinates[0]}, ..."

which raises ``TypeError: 'NoneType' object is not subscriptable``. The
exception propagates up to ``WebCamCamera.capture``, which catches it and
logs a **full traceback** — twice, via ``Log.e``.

Three consequences, all bad:

1. **The sample is lost.** No row is written for that frame. The CSV
   therefore contains only SUCCESSFUL frames, which is why ``status`` is
   always 1 and why ``valid_pct`` looked like a meaningless 100 %.
2. **The measured "sampling rate" is not the capture rate.** It is the
   rate of successful detections. A pipeline capturing a healthy 31 Hz
   with 40 % detection failures records as ~18 Hz, and looks like a
   performance problem when it is really a tracking-quality problem.
3. **Formatting and writing two tracebacks costs far more than the frame
   itself**, so failures also slow down the loop that follows them.

THE FIX
-------
Replace the bound ``_write_sample`` with one that tolerates missing
coordinates: failed frames are written with GazeFollower's own invalid
marker (-65536) and ``status = 0``, exactly as an eye tracker should
record a lost sample. Nothing is dropped, no traceback is produced, and
``status`` becomes meaningful again — 0 means "detection failed here",
which is what the data-loss metrics have always assumed it meant.

This must be applied BEFORE ``start_sampling()``: GazeFollower registers
``self._write_sample`` as a subscriber at that point, and an instance
attribute set beforehand is what gets registered.

Enabled by default. Set ``GF_SAMPLE_PATCH=0`` to reproduce the stock
(lossy) behaviour for comparison.
"""

from __future__ import annotations

import os
import sys
import time

# ── Windows console encoding ──────────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# GazeFollower's own "no valid gaze" sentinel (see cmd_gaze_info, which
# treats anything <= -65000 as undetected).
INVALID = -65536.0


def sample_patch_enabled() -> bool:
    return os.environ.get("GF_SAMPLE_PATCH", "1").strip().lower() not in (
        "0", "false", "no")


def _pair(value) -> "tuple[float, float]":
    """Coordinates as a 2-tuple, or the invalid sentinel."""
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError):
        return INVALID, INVALID


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def apply_sample_patch(gf, log=None) -> bool:
    """Install the loss-free sample writer. Returns True if applied.

    Never raises: failing to patch must not stop a session, it just
    means reverting to GazeFollower's lossy behaviour.
    """
    if not sample_patch_enabled():
        if log:
            log("Sample patch DISABLED (GF_SAMPLE_PATCH=0) — frames whose "
                "detection fails will be dropped and will produce "
                "tracebacks; the recorded rate will understate the true "
                "capture rate.")
        return False
    try:
        stream = getattr(gf, "_tmpSampleDataSteam", None)   # sic: upstream typo
        if stream is None:
            if log:
                log("Sample patch skipped (no sample stream on this build)")
            return False

        stats = {"written": 0, "failed": 0}
        # GazeFollower flushes to disk on EVERY sample — ~30 synchronous
        # writes per second, inside the capture loop. On Windows that is
        # exactly the pattern real-time antivirus and file-sync clients
        # hook, and the cost can grow as the file does, which looks like
        # the pipeline "getting worse and worse". Batch instead: the data
        # is still written continuously, just flushed about once a second.
        # save_data() copies the file at the end, so at most the last
        # second could be lost to a hard crash.
        flush_every = float(os.environ.get("GF_SAMPLE_FLUSH_SECONDS", "1.0"))
        last_flush = [time.perf_counter()]

        def _write_sample(face_info, gaze_info):
            _ = face_info
            gf._gaze_info = gaze_info
            trigger = 0
            current = getattr(gf, "_trigger", 0)
            if isinstance(current, int) and current != 0:
                trigger = current
                gf._trigger = 0

            status = bool(getattr(gaze_info, "status", False))
            rx, ry = _pair(getattr(gaze_info, "raw_gaze_coordinates", None))
            cx, cy = _pair(getattr(gaze_info,
                                   "calibrated_gaze_coordinates", None))
            fx, fy = _pair(getattr(gaze_info,
                                   "filtered_gaze_coordinates", None))
            tracking = getattr(getattr(gaze_info, "tracking_state", None),
                               "value", -1)
            event = getattr(getattr(gaze_info, "event", None), "value", 0)

            stats["written"] += 1
            if not status:
                stats["failed"] += 1

            try:
                stream.write(
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%d,%d,%d\n" % (
                        getattr(gaze_info, "timestamp", 0),
                        rx, ry, cx, cy, fx, fy,
                        _num(getattr(gaze_info, "left_openness", 0.0)),
                        _num(getattr(gaze_info, "right_openness", 0.0)),
                        tracking, int(status), int(event), trigger,
                    )
                )
                now = time.perf_counter()
                if flush_every <= 0 or now - last_flush[0] >= flush_every:
                    stream.flush()
                    last_flush[0] = now
            except Exception:  # noqa: BLE001 — a write failure must not
                pass          # kill the capture thread

        gf._write_sample = _write_sample
        gf._sample_stats = stats

        # DEDUPLICATE SUBSCRIBERS on every start_sampling().
        # GazeFollower appends self._write_sample each time sampling
        # starts, and its remove_subscriber() deletes from the list it is
        # iterating — the classic skip-every-other bug. A real session
        # starts and stops sampling many times (position guide, preview,
        # calibration, verification, validation, recording), so copies
        # accumulate. Each copy is another CSV write+flush PER FRAME, so
        # the loop gets progressively slower — and only in a real
        # session, which is why no offline benchmark reproduced it.
        try:
            orig_start = gf.start_sampling

            def start_sampling_deduped(*args, **kwargs):
                out = orig_start(*args, **kwargs)
                try:
                    seen = set()
                    unique = []
                    for entry in gf.subscribers:
                        key = id(entry[0])
                        if key in seen:
                            continue
                        seen.add(key)
                        unique.append(entry)
                    removed = len(gf.subscribers) - len(unique)
                    if removed and log:
                        log("Removed %d DUPLICATE sample subscriber(s) — "
                            "each would have cost a full CSV write per "
                            "frame." % removed)
                    gf.subscribers[:] = unique
                except Exception:  # noqa: BLE001 — never break sampling
                    pass
                return out

            gf.start_sampling = start_sampling_deduped
        except Exception:  # noqa: BLE001
            pass
        if log:
            log("Sample patch active: frames whose detection fails are now "
                "RECORDED with status=0 instead of being dropped with a "
                "traceback. 'status' is meaningful again, and the recorded "
                "rate is the true capture rate.")
        return True
    except Exception as exc:  # noqa: BLE001
        if log:
            log("Sample patch failed (%s) — continuing unpatched" % exc)
        return False


def describe() -> str:
    """One-line summary for the self-check report."""
    if not sample_patch_enabled():
        return ("DISABLED (GF_SAMPLE_PATCH=0) — failed-detection frames are "
                "dropped by GazeFollower and the recorded rate understates "
                "the capture rate")
    return ("active — failed-detection frames recorded with status=0 "
            "instead of dropped (GazeFollower raises on them and loses "
            "the sample)")


if __name__ == "__main__":
    print(describe())
