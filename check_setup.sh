#!/usr/bin/env bash
# check_setup.sh — run every automated diagnostic in one go (macOS/Linux).
#
#   bash check_setup.sh            # full pass
#   bash check_setup.sh --quick    # skip the 45 s camera timings
#
# Console output is also saved to data/setup_check_<timestamp>.log.
#
# STOP THE EXPERIMENT SERVER FIRST — only one process can own the webcam.
#
# Non-interactive checks only; the two steps needing you at the keyboard
# are listed at the end.

set -uo pipefail
cd "$(dirname "$0")"

QUICK=0
SECONDS_RUN=45
for arg in "$@"; do
    case "$arg" in
        --quick) QUICK=1 ;;
        --seconds=*) SECONDS_RUN="${arg#*=}" ;;
    esac
done

mkdir -p data
LOG="data/setup_check_$(date +%Y-%m-%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

section() { printf '\n%s\n  %s\n%s\n' "$(printf '=%.0s' {1..70})" "$1" "$(printf '=%.0s' {1..70})"; }
step() { section "$1"; shift; echo "> python $*"; echo; python "$@" || echo $'\n!! exited non-zero — read the message above.'; }

section "ENVIRONMENT"
echo "when    : $(date)"
echo "python  : $(command -v python)"
echo "version : $(python --version 2>&1)"
echo "cwd     : $(pwd)"
if [[ "$OSTYPE" == darwin* ]]; then
    echo "machine : $(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo '?')"
    echo "gpu     : $(system_profiler SPDisplaysDataType 2>/dev/null | awk -F': ' '/Chipset Model/{print $2}' | paste -sd' | ' -)"
    echo "power   : $(pmset -g ps 2>/dev/null | head -1)"
fi
echo
echo "NOTE: for the timing steps, be on AC power with Low Power Mode OFF."
echo "On battery you are measuring the power policy, not the hardware."

step "1/5  INTEGRITY SUITE (expect: ALL TESTS PASSED)" run_tests.py
step "2/5  TRACKER SELF-CHECK (expect: every line 'ok')" tracker_service.py --check
step "3/5  MNN BACKEND BENCHMARK (is the GPU used? not by default)" mnn_backend.py --profile

if [[ "$QUICK" -eq 0 ]]; then
    step "4/5  CAMERA FPS (${SECONDS_RUN}s, camera only)" camera_fps_test.py --no-window --seconds "$SECONDS_RUN"
    step "5/5  TRACKER FPS (${SECONDS_RUN}s, camera + inference)" tracker_fps_test.py --seconds "$SECONDS_RUN"
else
    section "4-5/5  CAMERA TIMINGS SKIPPED (--quick)"
fi

section "WHAT TO DO NEXT"
cat <<EOF
Read the log at:
  $LOG

Then the two INTERACTIVE steps this script cannot do for you:

  A. If step 5 said "No saved calibration model", run ONE calibration:
         python app.py
     log in, complete the calibration, stop the server, then re-run:
         bash check_setup.sh
     The model persists in ~/GazeFollower — once per machine.

  B. Record one full session under good conditions (AC power, bright
     front light, camera at eye level, ~60 cm, everything else closed):
         python app.py
     Watch for: the rate-gate verdict after calibration, 7 targets in
     BOTH validations, and per-axis gain (x and y reported separately).

  Then verify what you recorded:
         python quality_report.py
         python backfill_manifests.py --dry-run

THE THREE NUMBERS THAT DECIDE EVERYTHING
  * camera FPS           — under 25 means fix the camera/lighting first
  * tracker Hz sustained — under 20 means unusable fixation timing
  * per-frame budget     — 33.3 ms at 30 fps; above it the loop misses
                           every other frame and the rate halves
EOF
