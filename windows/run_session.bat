@echo off
REM ====================================================================
REM  RECORD A PARTICIPANT SESSION - the frozen collection configuration
REM ====================================================================
REM
REM  Use THIS file for every participant. Do not start app.py by hand.
REM
REM  WHY A BATCH FILE AND NOT A LIST OF ENV VARS
REM  Two settings changed the measured sampling rate by more than 2x:
REM
REM    GF_PERF_MODE=1     opts out of Windows EcoQoS. Without it the
REM                       tracker is scheduled onto the i9-13900H's
REM                       E-cores whenever the browser holds the
REM                       foreground: 29.4 Hz -> 12.1 Hz, with BOTH
REM                       model stages slowed by the same 2.26x.
REM
REM    GF_CAMERA_FIX=1    captures natively at 640x480 instead of letting
REM                       GazeFollower resize a larger frame in software
REM                       every frame: 21.4 Hz -> 32.0 Hz.
REM
REM  A participant recorded without these samples at roughly half rate,
REM  and half-rate data is not comparable with full-rate data because
REM  fixation duration is quantised by the sampling interval. Freezing
REM  the configuration in one file is what stops that happening by
REM  accident on a tired afternoon.
REM
REM  Everything here is recorded per session (perf_mode, frame_size,
REM  sampling rate), so a session run with the wrong settings is
REM  detectable afterwards - but prevention beats detection.
REM ====================================================================

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   No .venv found in %CD%
    echo   Run windows\setup_windows.bat first.
    echo.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

REM ---- Frozen recording configuration -------------------------------
set GF_PERF_MODE=1
set GF_PERF_PRIORITY=above_normal
set GF_CAMERA_FIX=1
set GF_TELEMETRY=1

REM Present EVERY stimulus in the folder, full length. The default in
REM config.py is "clip30" (one 30 s helper clip) because that is right
REM for a pilot run; real collection shows the actual stimulus set, and
REM that decision belongs in the frozen launcher rather than in a
REM default someone has to remember to override.
REM
REM Files beginning with _testclip are helper clips and are never
REM presented, so the folder should contain exactly the two 30 s study
REM clips (F16). The check below refuses to start if it does not.
set SESSION_STIMULUS_MODE=all

REM Fake-camera switches MUST be empty for a real participant. Setting
REM them marks the data as simulated in the manifest, but an unnoticed
REM leftover would waste the participant's time entirely.
set GF_FAKE_CAMERA=
set GF_FAKE_CALIBRATION=

REM P-core pinning (i9-13900H: 12 P-core threads, then 8 E-cores).
REM OFF by default - the EcoQoS opt-out already gives ~45%% headroom,
REM and pinning also stops the scheduler using E-cores for genuinely
REM background work. Uncomment only to A/B it.
REM set GF_PERF_PIN_CORES=12

echo.
echo   ================================================
echo    RECORDING CONFIGURATION (frozen)
echo   ================================================
echo    perf mode    : ON  (EcoQoS opt-out, above_normal)
echo    camera fix   : ON  (native 640x480 capture)
echo    telemetry    : ON
echo    fake camera  : off (real participant)
echo   ================================================
REM ---- How many REAL stimuli will be presented? ---------------------
REM  "all" with an empty folder is a session that records nothing, and
REM  the participant is already sitting down when you find out.
REM
REM  Written to a file and read back with set /p. NOT for /f: inside
REM  for /f the command string is handed to a second shell, where
REM  parentheses and quotes are metacharacters again, and when that
REM  parse fails cmd does not name the line - it prints a syntax error
REM  about a single character and terminates the console. set /p reads
REM  a file directly and has no subshell and no quoting exposure.
echo    Counting stimuli...
python count_stimuli.py > "%TEMP%\nstim.txt" 2>nul
set "NSTIM="
set /p NSTIM=<"%TEMP%\nstim.txt"
del "%TEMP%\nstim.txt" >nul 2>&1
if "%NSTIM%"=="" set NSTIM=?

if "%NSTIM%"=="0" (
    echo.
    echo    *** NO STIMULI FOUND. ***
    echo    SESSION_STIMULUS_MODE=all but the stimuli folder contains no
    echo    playable video (files starting with _testclip do not count).
    echo    The participant would watch nothing. Fix this first.
    echo.
    pause
    exit /b 1
)
if not "%NSTIM%"=="2" (
    echo.
    echo    NOTE: %NSTIM% stimuli found, not the 2 the protocol specifies.
    echo    Every participant must see the SAME set - a set that changes
    echo    between participants cannot be pooled.
    echo.
)

echo.
echo    stimuli     : %NSTIM% (mode=all, full length)
echo    Expect ~30 Hz and "perf_mode ... ACTIVE" in the log.
echo    If the rate gate reports under 25 Hz, stop and investigate
echo    BEFORE running the participant.
echo.

python app.py
