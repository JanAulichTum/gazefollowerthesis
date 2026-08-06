# check_setup.ps1 — run every automated diagnostic in one go (Windows).
#
#   .\check_setup.ps1              # full pass
#   .\check_setup.ps1 -Quick       # skip the 45 s camera timings
#
# Everything is echoed to the console AND saved to
# data\setup_check_<timestamp>.log, so you can paste the log somewhere.
#
# STOP THE EXPERIMENT SERVER FIRST — only one process can own the webcam.
#
# This covers the non-interactive checks only. Two steps need you at the
# keyboard and are called out at the end.

param(
    [switch]$Quick,
    [int]$Seconds = 45
)

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$logDir = Join-Path $PSScriptRoot "data"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "setup_check_$stamp.log"

function Section($title) {
    $bar = "=" * 70
    Write-Output ""
    Write-Output $bar
    Write-Output "  $title"
    Write-Output $bar
}

function RunStep($title, $scriptArgs) {
    Section $title
    Write-Output "> python $($scriptArgs -join ' ')"
    Write-Output ""
    & python @scriptArgs 2>&1 | ForEach-Object { $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Output ""
        Write-Output "!! exited with code $LASTEXITCODE — read the message above."
    }
}

Start-Transcript -Path $log -Force | Out-Null

Section "ENVIRONMENT"
Write-Output "when    : $(Get-Date)"
Write-Output "python  : $((Get-Command python).Source)"
Write-Output "version : $(python --version 2>&1)"
Write-Output "cwd     : $PSScriptRoot"
try {
    $gpu = Get-CimInstance Win32_VideoController |
        Select-Object -ExpandProperty Name
    Write-Output "gpu     : $($gpu -join ' | ')"
} catch { Write-Output "gpu     : (could not query)" }
try {
    $plan = powercfg /getactivescheme
    Write-Output "power   : $plan"
} catch { Write-Output "power   : (could not query)" }
Write-Output ""
Write-Output "NOTE: for the timing steps, be on AC power with the power plan"
Write-Output "set to 'Best performance' and battery saver OFF. On battery you"
Write-Output "are measuring the power policy, not the hardware."

# ── 1. Static suite: no camera, no calibration needed ──
RunStep "1/5  INTEGRITY SUITE (expect: ALL TESTS PASSED)" @("run_tests.py")

# ── 2. Dependency + camera + runtime diagnosis ──
RunStep "2/5  TRACKER SELF-CHECK (expect: every line 'ok')" @("tracker_service.py", "--check")

# ── 3. Is the GPU doing anything? ──
RunStep "3/5  MNN BACKEND BENCHMARK (is the GPU used? spoiler: not by default)" @("mnn_backend.py", "--profile")

if (-not $Quick) {
    # ── 4. Camera alone — no calibration model needed ──
    RunStep "4/5  CAMERA FPS ($Seconds s, camera only, no inference)" @("camera_fps_test.py", "--no-window", "--seconds", "$Seconds")

    # ── 5. Camera + inference — NEEDS a saved calibration model ──
    RunStep "5/5  TRACKER FPS ($Seconds s, camera + gaze inference)" @("tracker_fps_test.py", "--seconds", "$Seconds")
} else {
    Section "4-5/5  CAMERA TIMINGS SKIPPED (-Quick)"
}

Section "WHAT TO DO NEXT"
@"
Read the log at:
  $log

Then the two INTERACTIVE steps this script cannot do for you:

  A. If step 5 said "No saved calibration model", run ONE calibration:
         python app.py
     log in, complete the eye-tracker calibration, then stop the server
     (Ctrl-C) and re-run:
         .\check_setup.ps1 -Quick:`$false
     The model persists in ~\GazeFollower — once per machine.

  B. Record one full session under good conditions (AC power, bright
     front light, camera at eye level, ~60 cm, everything else closed):
         python app.py
     Watch for: the rate gate verdict after calibration, 7 targets in
     BOTH validations, and per-axis gain (x and y reported separately).

  Then verify what you recorded:
         python quality_report.py
         python backfill_manifests.py --dry-run

THE THREE NUMBERS THAT DECIDE EVERYTHING
  * camera FPS          — if this is under 25, fix the camera/lighting first
  * tracker Hz sustained— if this is under 20, sessions are unusable for
                          fixation timing; that is the whole problem
  * per-frame budget    — 33.3 ms at 30 fps. Above it, the loop misses
                          every other frame and the rate halves.
"@ | Write-Output

Stop-Transcript | Out-Null
Write-Host ""
Write-Host "Saved to: $log" -ForegroundColor Green
