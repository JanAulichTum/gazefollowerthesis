# -*- coding: utf-8 -*-
"""
Stop Windows from treating the tracker as background work.

THE PROBLEM
-----------
The gaze tracker runs as a SUBPROCESS of a Flask server while the
participant looks at a fullscreen browser. To Windows that makes the
browser the foreground application and the tracker background work — and
Windows 11 acts on that judgement in two ways:

1. **EcoQoS.** Background processes are marked for "efficiency", which
   caps their clock and biases the scheduler against them.
2. **Thread Director.** On hybrid CPUs (Intel 12th gen and later, incl.
   Meteor Lake / Core Ultra) threads deemed background are parked on
   E-cores or the SoC-tile low-power E-cores, which are markedly slower
   per clock than P-cores.

Neither is a bug and neither shows up as an error. The pipeline simply
takes longer per frame, and because the demotion applies to ALL code, it
slows every stage by the SAME factor — which is exactly the signature
measured on this project::

    no browser (foreground)   FaceMesh  9.3 ms   gaze CNN 19.3 ms
    browser    (background)   FaceMesh 21.1 ms   gaze CNN 43.6 ms
                              ratio     2.27x    ratio     2.26x

Two unrelated inference engines — MediaPipe is single-threaded TFLite,
MNN is 4-threaded — cannot be slowed by an identical factor through
resource contention. A uniform scalar across all CPU work means the CPU
itself is executing more slowly. Note this is NOT the AC/battery axis:
EcoQoS is a scheduler policy and applies on mains power too.

WHAT THIS DOES
--------------
``apply()`` asks Windows to leave the process alone:

    SetProcessInformation(ProcessPowerThrottling)  disable EcoQoS
    SetPriorityClass(ABOVE_NORMAL / HIGH)          outrank the browser
    SetProcessPriorityBoost(enabled)               keep dynamic boosts

Optionally ``pin_to_fast_cores()`` sets an affinity mask. That is a
blunter instrument — on Intel hybrid parts the P-cores are conventionally
enumerated first, but this is not architectural, so it is OFF by default
and must be requested explicitly.

Everything here is a no-op off Windows and never raises: a tracker that
cannot set its own priority must still record the session.

Environment:

    GF_PERF_MODE=0      disable (default: enabled)
    GF_PERF_PRIORITY    above_normal (default) | high | normal
    GF_PERF_PIN_CORES   number of leading logical cores to pin to

Usage::

    python perf_mode.py            # report and self-test
"""

from __future__ import annotations

import ctypes
import os
import sys

# ── Windows constants ────────────────────────────────────────────────
# PROCESS_INFORMATION_CLASS
_PROCESS_POWER_THROTTLING = 4
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1

_NORMAL_PRIORITY_CLASS = 0x00000020
_ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
_HIGH_PRIORITY_CLASS = 0x00000080

_PRIORITIES = {
    "normal": _NORMAL_PRIORITY_CLASS,
    "above_normal": _ABOVE_NORMAL_PRIORITY_CLASS,
    "high": _HIGH_PRIORITY_CLASS,
}


class _POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("ControlMask", ctypes.c_ulong),
        ("StateMask", ctypes.c_ulong),
    ]


# ── macOS constants ──────────────────────────────────────────────────
# <sys/resource.h>: PRIO_DARWIN_BG marks a process as background, which
# is macOS's equivalent of EcoQoS — throttled I/O and, on Apple Silicon,
# scheduling onto the efficiency cores. Setting the value back to 0
# clears it.
_PRIO_DARWIN_PROCESS = 4
_PRIO_DARWIN_BG = 0x1000
# <sys/qos.h> qos_class_t. Threads inherit QoS from their creator, so
# setting this on the main thread before GazeFollower spawns its capture
# thread propagates to the thread that actually does the work.
_QOS_CLASS_USER_INTERACTIVE = 0x21
_QOS_CLASS_USER_INITIATED = 0x19


def enabled() -> bool:
    return os.environ.get("GF_PERF_MODE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def _libc():
    return ctypes.CDLL("libc.dylib", use_errno=True)


def _macos_clear_background() -> "str | None":
    """Clear PRIO_DARWIN_BG, macOS's background-process throttle."""
    try:
        libc = _libc()
        libc.setpriority.argtypes = [ctypes.c_int, ctypes.c_uint,
                                     ctypes.c_int]
        libc.setpriority.restype = ctypes.c_int
        if libc.setpriority(_PRIO_DARWIN_PROCESS, 0, 0) != 0:
            return "setpriority(PRIO_DARWIN_PROCESS, 0) failed (errno %d)" \
                % ctypes.get_errno()
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:120]


def _macos_is_background() -> "bool | None":
    """Whether this process is currently marked background, or None."""
    try:
        libc = _libc()
        libc.getpriority.argtypes = [ctypes.c_int, ctypes.c_uint]
        libc.getpriority.restype = ctypes.c_int
        ctypes.set_errno(0)
        val = libc.getpriority(_PRIO_DARWIN_PROCESS, 0)
        if val == -1 and ctypes.get_errno() != 0:
            return None
        return bool(val & _PRIO_DARWIN_BG)
    except Exception:  # noqa: BLE001
        return None


def _macos_set_qos(user_interactive: bool = True) -> "str | None":
    """Raise the calling thread's QoS class.

    On Apple Silicon the QoS class is what decides P-core vs E-core
    placement — the same mechanism as Thread Director on Intel hybrid
    parts, reached through a different API.
    """
    try:
        libc = _libc()
        fn = libc.pthread_set_qos_class_self_np
        fn.argtypes = [ctypes.c_uint, ctypes.c_int]
        fn.restype = ctypes.c_int
        qos = _QOS_CLASS_USER_INTERACTIVE if user_interactive \
            else _QOS_CLASS_USER_INITIATED
        if fn(qos, 0) != 0:
            return "pthread_set_qos_class_self_np failed (errno %d)" \
                % ctypes.get_errno()
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:120]


def _macos_disable_app_nap() -> "str | None":
    """Hold an activity assertion so App Nap cannot throttle us.

    Requires pyobjc, which is NOT a project dependency — absence is
    reported, not raised. The QoS and background-flag changes above are
    the load-bearing ones; this is belt and braces.
    """
    try:
        from Foundation import NSActivityLatencyCritical  # noqa: WPS433
        from Foundation import (NSActivityUserInitiated, NSProcessInfo)
    except Exception:  # noqa: BLE001
        return "pyobjc not installed (optional)"
    try:
        opts = NSActivityUserInitiated | NSActivityLatencyCritical
        token = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            opts, "eye-tracking session recording")
        globals()["_APP_NAP_TOKEN"] = token   # hold a reference
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:120]


def _kernel32():
    """kernel32 with explicit signatures.

    Without argtypes/restypes ctypes assumes ``int`` for handles, which
    is 32-bit. ``GetCurrentProcess()`` returns the pseudo-handle -1 and
    the truncation makes these calls fail silently on 64-bit Windows —
    the exact failure mode this module exists to avoid.
    """
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.GetCurrentProcess.argtypes = []
    k.SetProcessInformation.restype = ctypes.c_bool
    k.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_void_p, ctypes.c_ulong]
    k.SetPriorityClass.restype = ctypes.c_bool
    k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k.SetProcessPriorityBoost.restype = ctypes.c_bool
    k.SetProcessPriorityBoost.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    k.SetProcessAffinityMask.restype = ctypes.c_bool
    k.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    return k


def _disable_eco_qos() -> "str | None":
    """Opt out of EcoQoS. Returns an error string, or None on success.

    ControlMask selects which policy to control, StateMask sets it.
    EXECUTION_SPEED with StateMask=0 means "do NOT throttle me".
    """
    try:
        kernel32 = _kernel32()
        state = _POWER_THROTTLING_STATE(
            Version=_PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            ControlMask=_PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            StateMask=0,
        )
        ok = kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(),
            _PROCESS_POWER_THROTTLING,
            ctypes.byref(state),
            ctypes.sizeof(state),
        )
        if not ok:
            return "SetProcessInformation failed (err %d)" \
                % ctypes.get_last_error()
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:120]


def _set_priority(name: str) -> "str | None":
    try:
        kernel32 = _kernel32()
        cls = _PRIORITIES.get(name, _ABOVE_NORMAL_PRIORITY_CLASS)
        if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), cls):
            return "SetPriorityClass failed (err %d)" % ctypes.get_last_error()
        # Priority boost ON: we WANT the scheduler's dynamic boosts.
        kernel32.SetProcessPriorityBoost(kernel32.GetCurrentProcess(), False)
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:120]


def pin_to_fast_cores(n: int) -> "str | None":
    """Restrict the process to the first *n* logical CPUs.

    HEURISTIC, not a guarantee: on Intel hybrid parts firmware normally
    enumerates P-cores first, but nothing in the architecture requires
    it. Off by default for that reason — use it as an experiment, and
    verify with a measurement rather than trusting the ordering.
    """
    if n <= 0:
        return "no cores requested"
    try:
        kernel32 = _kernel32()
        mask = (1 << n) - 1
        if not kernel32.SetProcessAffinityMask(
                kernel32.GetCurrentProcess(), ctypes.c_size_t(mask)):
            return "SetProcessAffinityMask failed (err %d)" \
                % ctypes.get_last_error()
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:120]


def apply(log=None) -> dict:
    """Apply the performance policy. Never raises."""
    result: dict = {"applied": False, "platform": sys.platform}
    if not enabled():
        result["skipped"] = "GF_PERF_MODE=0"
        if log:
            log("Performance mode DISABLED by GF_PERF_MODE=0 — the tracker "
                "may be demoted to efficiency cores while the browser is in "
                "the foreground.")
        return result
    if is_macos():
        result["mechanism"] = "macOS QoS + PRIO_DARWIN_BG"
        result["was_background"] = _macos_is_background()
        err = _macos_clear_background()
        result["background_cleared"] = err is None
        if err:
            result["background_error"] = err
        err = _macos_set_qos(True)
        result["qos_user_interactive"] = err is None
        if err:
            result["qos_error"] = err
        nap = _macos_disable_app_nap()
        result["app_nap_disabled"] = nap is None
        if nap:
            result["app_nap_note"] = nap
        result["applied"] = bool(result.get("background_cleared")
                                 or result.get("qos_user_interactive"))
        if log and result["applied"]:
            log("Performance mode (macOS): background flag cleared, thread "
                "QoS raised to USER_INTERACTIVE. On Apple Silicon this is "
                "what keeps the capture thread on performance cores rather "
                "than efficiency cores.")
        return result

    if not is_windows():
        result["skipped"] = "%s — no known background-demotion mechanism " \
            "to opt out of" % sys.platform
        return result

    result["mechanism"] = "Windows EcoQoS + priority class"
    err = _disable_eco_qos()
    result["eco_qos_disabled"] = err is None
    if err:
        result["eco_qos_error"] = err

    prio = os.environ.get("GF_PERF_PRIORITY", "above_normal").strip().lower()
    err = _set_priority(prio)
    result["priority"] = prio if err is None else None
    if err:
        result["priority_error"] = err

    pin = os.environ.get("GF_PERF_PIN_CORES", "").strip()
    if pin.isdigit():
        err = pin_to_fast_cores(int(pin))
        result["pinned_cores"] = int(pin) if err is None else None
        if err:
            result["pin_error"] = err

    result["applied"] = bool(result.get("eco_qos_disabled")
                             or result.get("priority"))
    if log and result["applied"]:
        log("Performance mode: EcoQoS %s, priority %s%s. This stops Windows "
            "parking the tracker on efficiency cores while the browser holds "
            "the foreground."
            % ("disabled" if result.get("eco_qos_disabled") else "NOT disabled",
               result.get("priority") or "unchanged",
               ", pinned to %d cores" % result["pinned_cores"]
               if result.get("pinned_cores") else ""))
    elif log:
        log("Performance mode could not be applied: %s" % result)
    return result


def describe() -> str:
    """One-line summary for the tracker self-check."""
    if not enabled():
        return "off (GF_PERF_MODE=0)"
    if is_macos():
        return "macOS QoS USER_INTERACTIVE + background flag cleared"
    if is_windows():
        return ("EcoQoS opt-out + %s priority"
                % os.environ.get("GF_PERF_PRIORITY", "above_normal"))
    return "n/a (%s)" % sys.platform


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true",
                    help="check the policy actually took effect")
    args = ap.parse_args()

    print("Performance mode")
    print("  platform :", sys.platform)
    print("  enabled  :", enabled())
    print("  setting  :", describe())
    print()
    res = apply(log=lambda m: print("  " + m))
    print()
    for k, v in res.items():
        print("  %-20s %s" % (k, v))

    if args.verify:
        print()
        print("  VERIFY")
        ok = True
        if is_macos():
            bg = _macos_is_background()
            print("    background flag now : %s" % bg)
            if bg:
                print("    !! still marked background — the clear failed")
                ok = False
        elif is_windows():
            if not res.get("eco_qos_disabled"):
                print("    !! EcoQoS opt-out did NOT take: %s"
                      % res.get("eco_qos_error"))
                ok = False
            if not res.get("priority"):
                print("    !! priority not set: %s" % res.get("priority_error"))
                ok = False
        if ok and res.get("applied"):
            print("    OK — the policy applied cleanly in THIS process.")
            print()
            print("    NOTE: this console is already the foreground process,")
            print("    so it was probably never demoted. This confirms the")
            print("    API calls succeed; it does NOT prove the rate is")
            print("    recovered. Only a real session with the browser in")
            print("    front tests that — see the A/B in the README.")
        elif not res.get("applied"):
            print("    NOT APPLIED — %s" % (res.get("skipped") or res))
            ok = False
        return 0 if ok else 1

    if not (is_windows() or is_macos()):
        print("\n  No known background-demotion mechanism on %s."
              % sys.platform)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
