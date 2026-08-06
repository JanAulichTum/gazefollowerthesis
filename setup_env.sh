#!/usr/bin/env bash
# Create a clean conda environment for this project, then verify it.
#
#   bash setup_env.sh                 # create "gaze_thesis"
#   bash setup_env.sh my_env_name     # different name
#   bash setup_env.sh gaze_thesis -f  # delete and recreate if it exists
#
# Why not just `conda env create -f environment.yml`? On Apple Silicon,
# conda will happily build an x86_64 environment that then runs under
# Rosetta, where MNN's wheels are broken and inference is ~half speed.
# This script forces the native subdir first and pins it into the env, so
# every later `conda install` in it stays native too.
#
# The existing `gaze_native` env is left untouched — it stays as a
# fallback if anything here goes wrong.

set -uo pipefail
cd "$(dirname "$0")"

ENV_NAME="${1:-gaze_thesis}"
FORCE="${2:-}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n!! %s\n' "$*"; exit 1; }

command -v conda >/dev/null 2>&1 || fail \
  "conda not found. Install Miniforge (recommended on Apple Silicon):
     brew install --cask miniforge
   then reopen the terminal and re-run this script."

# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh" || fail "could not source conda"

# ── Native architecture on Apple Silicon ──
SUBDIR=""
if [[ "$(uname -s)" == "Darwin" ]]; then
    if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" == "1" ]]; then
        SUBDIR="osx-arm64"
        say "Apple Silicon detected — need CONDA_SUBDIR=$SUBDIR"
        echo "  (an x86_64 env runs under Rosetta, where MNN's wheels are"
        echo "   broken: 'symbol not found in flat namespace"
        echo "   __ZN3MNN10getVersionEv')"

        # CRITICAL: an x86_64 conda installation cannot build a working
        # osx-arm64 environment. CONDA_SUBDIR is accepted and then
        # quietly ignored/mixed, and you end up with an Intel env whose
        # MNN wheel will not load. Detect that here rather than after a
        # ten-minute install and a confusing dlopen error.
        CONDA_ARCH="$(conda run -n base python -c \
            'import platform; print(platform.machine())' 2>/dev/null || echo '?')"
        echo "  conda's own architecture: $CONDA_ARCH"
        if [[ "$CONDA_ARCH" != "arm64" ]]; then
            fail "Your conda is $CONDA_ARCH (Intel) on an Apple Silicon Mac.
   It CANNOT create a working arm64 environment — MNN will fail to load
   with 'symbol not found in flat namespace __ZN3MNN10getVersionEv'.

   Install a native conda, then re-run this script:
       brew install --cask miniforge
       source \"\$(brew --prefix)/Caskroom/miniforge/base/etc/profile.d/conda.sh\"
       conda init zsh          # then reopen the terminal
       bash setup_env.sh

   Alternatively keep using the existing 'gaze_native' env, which was
   built natively by fix_environment.sh and works:
       conda activate gaze_native"
        fi
    fi
fi

# ── Create ──
if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
    if [[ "$FORCE" == "-f" ]]; then
        say "Removing existing env '$ENV_NAME'"
        conda env remove -n "$ENV_NAME" -y || fail "could not remove $ENV_NAME"
    else
        fail "env '$ENV_NAME' already exists. Re-run with -f to replace it:
     bash setup_env.sh $ENV_NAME -f
   or pick another name:
     bash setup_env.sh gaze_thesis2"
    fi
fi

say "Creating '$ENV_NAME' from environment.yml"
if [[ -n "$SUBDIR" ]]; then
    CONDA_SUBDIR="$SUBDIR" conda env create -f environment.yml -n "$ENV_NAME" \
        || fail "environment creation failed (see the output above)"
    # Pin it so future installs into this env stay native.
    conda env config vars set CONDA_SUBDIR="$SUBDIR" -n "$ENV_NAME" >/dev/null
else
    conda env create -f environment.yml -n "$ENV_NAME" \
        || fail "environment creation failed (see the output above)"
fi

PY="$(conda run -n "$ENV_NAME" python -c 'import sys; print(sys.executable)' 2>/dev/null)"
ARCH="$(conda run -n "$ENV_NAME" python -c 'import platform; print(platform.machine())' 2>/dev/null)"

say "VERIFYING"
echo "  python : $PY"
echo "  arch   : $ARCH"
if [[ "$SUBDIR" == "osx-arm64" && "$ARCH" != "arm64" ]]; then
    fail "Expected arm64 but got $ARCH — this env runs under Rosetta and
   MNN will not load. Remove it and install a native conda first:
       conda env remove -n $ENV_NAME -y
       brew install --cask miniforge"
fi

# MNN is the single most fragile dependency; a broken wheel is silent
# until the first calibration. Check it explicitly and stop here if it
# is not importable, rather than reporting success on a dead env.
if ! conda run -n "$ENV_NAME" python -c "import MNN" >/dev/null 2>&1; then
    MNN_ERR="$(conda run -n "$ENV_NAME" python -c 'import MNN' 2>&1 | tail -2)"
    say "!! MNN DOES NOT IMPORT — this environment cannot run the tracker"
    echo "$MNN_ERR"
    echo
    if [[ "$MNN_ERR" == *"flat namespace"* ]]; then
        echo "  'symbol not found in flat namespace' is the known-broken"
        echo "  macOS MNN wheel. It happens when the environment is x86_64"
        echo "  (Rosetta). Confirm with:"
        echo "      conda run -n $ENV_NAME python -c \\"
        echo "        'import platform; print(platform.machine())'"
        echo "  If that prints x86_64 on an Apple Silicon Mac, you need a"
        echo "  native conda (miniforge) — see the message above."
    else
        echo "  Try reinstalling it into this env:"
        echo "      conda run -n $ENV_NAME python -m pip install --force-reinstall 'MNN>=3.0'"
    fi
    echo
    echo "  Meanwhile 'gaze_native' still works:  conda activate gaze_native"
    exit 1
fi

echo
echo "  Imports:"
conda run -n "$ENV_NAME" python env_check.py 2>&1 | sed -n '3,20p'

echo
echo "  Tracker self-check:"
conda run -n "$ENV_NAME" python tracker_service.py --check 2>&1 \
    | grep -E "^  [a-z_]+:" || echo "    (self-check produced no output)"

echo
echo "  Integrity suite:"
conda run -n "$ENV_NAME" python run_tests.py 2>&1 | tail -3

say "DONE — use it with:"
cat <<EOF
    conda activate $ENV_NAME
    python app.py

Diagnostics in this env:
    python run_all.py                 # everything, with a verdict
    python diagnose_rate.py --only idle
    python check_screen_space.py

Environment variables (bash):
    export GF_CAMERA_FIX=1            # native 640x480 capture, buffer=1
    export SESSION_STIMULUS_MODE=all  # full stimulus set for real runs
    unset  GF_CAMERA_FIX              # clear

The old 'gaze_native' env is untouched, so you can fall back to it at any
time. Record in the methods section WHICH env produced each dataset —
package versions change the sampling rate, and the rate changes the
fixation statistics.
EOF
