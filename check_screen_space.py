# -*- coding: utf-8 -*-
"""
Is the tracker using the same pixel space as the browser?

Run this when calibration looks perfect but the accuracy check is wildly
wrong. That combination is almost never a tracking failure — it is the
two halves of the system measuring in different units.

GazeFollower's ``DefaultConfig`` does::

    self._monitors = get_monitors()                       # screeninfo
    self.screen_size = [monitors[0].width, monitors[0].height]

and scales every gaze estimate into that space. ``screeninfo`` reports
**physical** pixels. The browser lays validation targets out in **CSS**
pixels. They differ whenever:

  * display scaling is not 100 % — a 2560x1440 panel at Windows 150 % is
    1707x960 to the browser, so gaze lands ~1.5x too far from the origin,
    with the error growing toward the edges; or
  * there is more than one monitor and the browser is not on
    ``monitors[0]``.

Calibration stays self-consistent inside the tracker's own space, so it
looks flawless. Only a browser-side check exposes the mismatch.

Usage::

    python check_screen_space.py

It prints the tracker's view, then a one-line snippet to paste into the
browser console (F12) on the experiment page to print the browser's view.
Compare the two numbers.
"""

from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def main() -> int:
    print("=" * 70)
    print("  SCREEN SPACE CHECK")
    print("=" * 70)

    try:
        from screeninfo import get_monitors
    except Exception as exc:  # noqa: BLE001
        print("  screeninfo unavailable (%s)" % exc)
        print("  Is the project venv active?  .\\.venv\\Scripts\\activate")
        return 1

    monitors = list(get_monitors())
    print("  Monitors seen by screeninfo (GazeFollower uses [0]):")
    for i, m in enumerate(monitors):
        print("    [%d] %-16s %dx%d at (%d,%d)%s%s"
              % (i, getattr(m, "name", "?") or "?", m.width, m.height,
                 getattr(m, "x", 0), getattr(m, "y", 0),
                 "  PRIMARY" if getattr(m, "is_primary", False) else "",
                 "   <-- GazeFollower maps gaze into THIS" if i == 0 else ""))
    if len(monitors) > 1:
        print()
        print("  !! MORE THAN ONE MONITOR. GazeFollower always uses [0].")
        print("     If the browser runs on a different screen, every gaze")
        print("     coordinate is in the wrong space. Run the experiment on")
        print("     monitor [0], or disconnect the others while recording.")

    # Windows: ask the OS for the scale factor directly.
    if sys.platform.startswith("win"):
        try:
            import ctypes

            user32 = ctypes.windll.user32
            # Unaware size (what a non-DPI-aware app sees) vs the real one.
            unaware_w = user32.GetSystemMetrics(0)
            unaware_h = user32.GetSystemMetrics(1)
            user32.SetProcessDPIAware()
            aware_w = user32.GetSystemMetrics(0)
            aware_h = user32.GetSystemMetrics(1)
            print()
            print("  Windows metrics:")
            print("    DPI-unaware  : %dx%d" % (unaware_w, unaware_h))
            print("    DPI-aware    : %dx%d" % (aware_w, aware_h))
            if unaware_w and aware_w != unaware_w:
                scale = aware_w / float(unaware_w)
                print("    -> display scaling is about %d %%" % round(scale * 100))
                print()
                print("  !! SCALING IS NOT 100 %. This is the most likely")
                print("     cause of 'calibration fine, validation awful'.")
                print("     Set Display settings -> Scale to 100 %, log out")
                print("     and back in, then recalibrate.")
            else:
                print("    -> scaling appears to be 100 % (good)")
        except Exception as exc:  # noqa: BLE001
            print("  (could not query Windows DPI: %s)" % exc)

    print()
    print("-" * 70)
    print("  NOW THE BROWSER SIDE")
    print("-" * 70)
    print("  Open the experiment page, press F12, and paste this into the")
    print("  Console tab:")
    print()
    print("    console.log(screen.width, screen.height,"
          " window.devicePixelRatio)")
    print()
    print("  Compare with monitor [0] above:")
    print("    * same numbers, devicePixelRatio 1  -> spaces agree, the")
    print("      validation error is real and this is not your problem")
    print("    * browser smaller by a constant factor -> display scaling;")
    print("      that factor is exactly how far off every gaze point is")
    print("    * completely different -> the browser is on another monitor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
