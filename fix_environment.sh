#!/bin/bash
# ============================================================================
# One-shot environment fixer for the eye-tracking experiment (v3).
#
# Usage (from the eye_tracking_experiment folder):
#     bash fix_environment.sh
#
# Handles the broken-MNN problem in three escalating steps:
#   1. Detect Apple Silicon + Intel(Rosetta) Python — if so, create a
#      NATIVE arm64 conda env (x86_64 MNN wheels are broken on macOS).
#   2. Otherwise purge + reinstall MNN and verify the import (the
#      tracker also preloads MNN's dylibs at runtime as a workaround).
#   3. If the newest MNN is still broken, hunt through older versions
#      until one imports cleanly.
# ============================================================================
cd "$(dirname "$0")"

PY="${PYTHON:-python}"
echo "Current Python: $($PY -c 'import sys, platform; print(sys.executable, sys.version.split()[0], platform.machine())')"

verify_mnn () {  # verify_mnn <python>  → 0 if MNN imports
    "$1" - <<'EOF' 2>/dev/null
import ctypes, glob, os, site, sys
# Same dylib-preload workaround the tracker uses
for base in set(site.getsitepackages() + [site.getusersitepackages()]):
    for lib in glob.glob(os.path.join(base, "MNN", "**", "*.dylib"), recursive=True):
        try: ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
        except OSError: pass
import MNN
print("MNN import OK")
EOF
}

finish_ok () {  # finish_ok <python> <how-to-start>
    echo
    echo "── Installing all requirements ──"
    "$1" -m pip install --no-cache-dir -r requirements.txt
    echo
    echo "── Full self-check ──"
    "$1" tracker_service.py --check
    echo
    echo "✓ Start the server with:  $2"
    exit 0
}

# ── Step 1: Apple Silicon running an Intel Python? ─────────────────────────
HW_ARM=$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)
PY_ARCH=$("$PY" -c 'import platform; print(platform.machine())')

if [ "$HW_ARM" = "1" ] && [ "$PY_ARCH" = "x86_64" ]; then
    echo
    echo "⚠ Apple Silicon Mac, but your Python is an Intel build (Rosetta)."
    echo "  MNN's x86_64 macOS wheels are broken — creating a NATIVE arm64 env."
    if command -v conda >/dev/null 2>&1; then
        CONDA_SUBDIR=osx-arm64 conda create -n gaze_native python=3.11 -y || exit 1
        conda env config vars set CONDA_SUBDIR=osx-arm64 -n gaze_native >/dev/null 2>&1
        NPY="$(conda run -n gaze_native python -c 'import sys; print(sys.executable)')"
        echo "  Native env python: $NPY ($(conda run -n gaze_native python -c 'import platform; print(platform.machine())'))"
        "$NPY" -m pip install --no-cache-dir "MNN>=3.0"
        if verify_mnn "$NPY"; then
            finish_ok "$NPY" "conda activate gaze_native && python app.py"
        fi
        echo "  Native env MNN still failing — continuing with fallbacks…"
        PY="$NPY"
    else
        echo "  conda not found — install native Python 3.11 from python.org"
        echo "  (choose the 'macOS 64-bit universal2' installer), then run:"
        echo "      PYTHON=/usr/local/bin/python3.11 bash fix_environment.sh"
        exit 1
    fi
fi

# ── Step 2: purge + reinstall newest MNN ────────────────────────────────────
echo
echo "── Purge + reinstall MNN ──"
"$PY" -m pip uninstall -y MNN mnn 2>/dev/null
"$PY" -m pip cache remove "MNN*" 2>/dev/null
"$PY" -m pip install --no-cache-dir --force-reinstall "MNN>=3.0"
if verify_mnn "$PY"; then
    finish_ok "$PY" "$PY app.py"
fi

# ── Step 3: hunt through older MNN versions ────────────────────────────────
echo
echo "── Newest MNN broken on this system — trying older versions ──"
for V in 3.4.0 3.2.0 3.0.0 2.8.1 2.7.1; do
    echo "  … trying MNN==$V"
    "$PY" -m pip install --no-cache-dir --force-reinstall "MNN==$V" 2>/dev/null || continue
    if verify_mnn "$PY"; then
        echo "  ✓ MNN==$V works!"
        finish_ok "$PY" "$PY app.py"
    fi
done

echo
echo "✗ No MNN wheel works with this Python."
echo "  Please report the output of:  $PY tracker_service.py --check"
echo "  (the 'arch' line matters most)"
exit 1
