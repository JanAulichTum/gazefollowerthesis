# -*- coding: utf-8 -*-
"""
Fail with a useful message when the wrong Python interpreter is used.

"ModuleNotFoundError: No module named 'gazefollower'" almost never means
the package is broken — it means the project virtualenv is not active and
the system Python is running instead. The bare traceback sends you off
reinstalling packages that were fine all along.

``require("gazefollower", "MNN")`` checks the imports, and on failure
prints which interpreter is actually running, whether a project venv
exists next to the script, and the exact command to activate it.
"""

from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))


def venv_python() -> "str | None":
    """Path to the project venv's interpreter, if one exists."""
    for rel in (os.path.join(".venv", "Scripts", "python.exe"),   # Windows
                os.path.join(".venv", "bin", "python")):          # POSIX
        candidate = os.path.join(BASE, rel)
        if os.path.isfile(candidate):
            return candidate
    return None


def conda_env() -> "str | None":
    """Name of the active conda environment, if any."""
    name = os.environ.get("CONDA_DEFAULT_ENV")
    if name and name != "base":
        return name
    # Fall back to inferring it from the interpreter path.
    parts = os.path.abspath(sys.executable).split(os.sep)
    if "envs" in parts:
        idx = parts.index("envs")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def in_project_venv() -> bool:
    """True when the interpreter is a plausible project environment.

    The project supports BOTH layouts: a `.venv` (Windows) and a conda
    env (macOS, created by setup_env.sh). An active conda env counts —
    otherwise this would tell conda users to create a `.venv` they do
    not want, which is worse than saying nothing.
    """
    if conda_env():
        return True
    expected = venv_python()
    if not expected:
        return True          # no venv in the project → nothing to compare
    try:
        return os.path.samefile(sys.executable, expected)
    except OSError:
        return os.path.normcase(os.path.abspath(sys.executable)) == \
            os.path.normcase(os.path.abspath(expected))


def missing(modules: "tuple[str, ...]") -> list:
    out = []
    for name in modules:
        try:
            __import__(name)
        except Exception:  # noqa: BLE001 — a broken install counts as missing
            out.append(name)
    return out


def require(*modules: str, exit_code: int = 1) -> None:
    """Exit with an actionable message if *modules* cannot be imported."""
    gone = missing(modules)
    if not gone:
        return

    print()
    print("=" * 70)
    print("  CANNOT IMPORT: %s" % ", ".join(gone))
    print("=" * 70)
    print("  running python : %s" % sys.executable)
    print("  version        : %s" % sys.version.split()[0])

    expected = venv_python()
    if expected and not in_project_venv():
        print("  project venv   : %s" % expected)
        print()
        print("  >>> You are NOT using the project virtualenv. That is almost")
        print("  >>> certainly the whole problem — the packages are installed")
        print("  >>> in the venv, not in this interpreter.")
        print()
        print("  Activate it and re-run:")
        if sys.platform.startswith("win"):
            print("      .\\.venv\\Scripts\\activate")
        else:
            print("      source .venv/bin/activate")
        print("  Or call the venv's python directly, without activating:")
        print('      "%s" %s' % (expected, os.path.basename(sys.argv[0])))
    elif expected:
        print("  project venv   : %s  (ACTIVE)" % expected)
        print()
        print("  The right interpreter is running, so the package really is")
        print("  missing or broken. Reinstall into this venv:")
        print("      python -m pip install -r requirements.txt")
    elif conda_env():
        print("  conda env     : %s  (ACTIVE)" % conda_env())
        print()
        print("  The right kind of environment is active, so the package is")
        print("  genuinely missing or broken IN it.")
        if "MNN" in gone or "gazefollower" in gone:
            print()
            print("  MNN/gazefollower failing together almost always means the")
            print("  known-broken macOS MNN wheel. Check the architecture:")
            print("      python -c 'import platform; print(platform.machine())'")
            print("  x86_64 on an Apple Silicon Mac = Rosetta, where MNN's")
            print("  wheels do not load. You need a NATIVE arm64 conda:")
            print("      brew install --cask miniforge")
            print("      bash setup_env.sh")
            print("  The existing 'gaze_native' env was built natively and")
            print("  works:  conda activate gaze_native")
        else:
            print("      python -m pip install -r requirements.txt")
    else:
        print()
        print("  No project environment detected in %s" % BASE)
        print("  macOS (conda, recommended here):")
        print("      bash setup_env.sh && conda activate gaze_thesis")
        print("  Windows (venv):")
        print("      python -m venv .venv")
        print("      .\\.venv\\Scripts\\activate")
        print("      python -m pip install -r requirements.txt")
    print()
    raise SystemExit(exit_code)


def report() -> str:
    """One-line interpreter summary for diagnostics."""
    if conda_env():
        where = "conda env '%s'" % conda_env()
    elif in_project_venv():
        where = "project venv"
    else:
        where = "NOT the project venv"
    return "%s (%s, %s)" % (sys.executable, sys.version.split()[0], where)


if __name__ == "__main__":
    import platform

    print("interpreter :", report())
    print("architecture:", platform.machine(), end="")
    if sys.platform == "darwin" and platform.machine() == "x86_64":
        print("   <-- if this Mac is Apple Silicon, you are under Rosetta "
              "and MNN will not load")
    else:
        print()
    print("conda env   :", conda_env() or "(none)")
    print("venv python :", venv_python() or "(none found)")
    for mod in ("gazefollower", "MNN", "mediapipe", "cv2", "pygame",
                "numpy", "pandas", "flask"):
        try:
            __import__(mod)
            print("  %-14s ok" % mod)
        except Exception as exc:  # noqa: BLE001
            print("  %-14s MISSING (%s)" % (mod, str(exc)[:50]))
