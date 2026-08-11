@echo off
REM ====================================================================
REM  START HERE  -  double-click this. It is the only file you need.
REM ====================================================================
REM
REM  Does the four things that were being retyped every session:
REM    1. moves to the project folder
REM    2. pulls the latest code (and stops if that would clobber your
REM       local edits)
REM    3. creates or repairs the virtual environment
REM    4. activates it and offers the commands you actually run
REM
REM  Option 9 drops you at a normal prompt with the environment already
REM  active and the working directory already right, so anything not on
REM  the menu still works without setup.
REM
REM  Deliberately does NOT auto-run the test suite on every launch: it
REM  takes 20 s and you do not need it to open the coding tool. The
REM  update step runs it, because that is when code changed.
REM ====================================================================

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."
title Eye-Tracking Study  -  %CD%
color 07

cls
echo.
echo   ================================================================
echo    EYE-TRACKING STUDY
echo   ================================================================
echo    folder : %CD%
echo.

REM ---- 1. Virtual environment ---------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo    No virtual environment found. Creating one - this takes a
    echo    few minutes the first time only.
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo    Could not create the venv. Is Python on PATH?
        echo    Check with:  python --version
        pause & exit /b 1
    )
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo    Dependency install FAILED. Fix that before continuing.
        pause & exit /b 1
    )
) else (
    call .venv\Scripts\activate.bat
)

REM ---- 2. Update ------------------------------------------------------
git rev-parse --git-dir >nul 2>&1
if errorlevel 1 goto :skip_update

for /f %%i in ('git status --porcelain 2^>nul ^| find /c /v ""') do set DIRTY=%%i
if not "%DIRTY%"=="0" (
    echo    %DIRTY% uncommitted change^(s^) on this machine - NOT pulling.
    echo    Overwriting an edit made here, unrecorded, is worse than
    echo    running slightly behind. Run:  git status
    echo.
    goto :skip_update
)

echo    Checking for updates...
git fetch --quiet origin 2>nul
for /f %%i in ('git rev-list --count HEAD..@{u} 2^>nul') do set BEHIND=%%i
if "%BEHIND%"=="" set BEHIND=0
if "%BEHIND%"=="0" (
    echo    Already up to date.
    echo.
    goto :skip_update
)

echo    %BEHIND% new commit^(s^). Pulling...
git pull --ff-only
if errorlevel 1 (
    echo    Pull failed - the branches have diverged. Run: git status
    pause
    goto :skip_update
)

git diff --name-only HEAD@{1} HEAD 2>nul | findstr /i "requirements.txt" >nul
if not errorlevel 1 (
    echo    requirements.txt changed - reinstalling...
    pip install -r requirements.txt
)

echo.
echo    Verifying the update...
python run_tests.py >nul 2>&1
if errorlevel 1 (
    echo    *** TESTS FAILED after the update. ***
    echo    Do not record participants until this is resolved.
    echo    See the detail with:  python run_tests.py
    echo.
    pause
) else (
    echo    Tests pass.
    echo.
)

:skip_update

REM ====================================================================
REM  MENU
REM ====================================================================
:menu
echo   ----------------------------------------------------------------
echo    1  Record a participant            (run_session.bat)
echo    2  Pre-flight check                (start of a collection day)
echo.
echo    3  Verify today's metrics          (verify_metrics --today)
echo    4  Calibration diagnosis           (why the gain is needed)
echo    5  Claim correspondence            (claim_check --latest)
echo.
echo    6  Open the fixation CODER         (server + browser)
echo    7  Camera / rate diagnostics
echo.
echo    8  Run the test suite
echo    9  Just give me a prompt
echo    0  Quit
echo   ----------------------------------------------------------------
set "OPT="
set /p OPT=   Choose:
echo.

if "%OPT%"=="1" ( call windows\run_session.bat & goto :menu )
if "%OPT%"=="2" ( call windows\check_before_participant.bat & goto :menu )
if "%OPT%"=="3" ( python verify_metrics.py --today & pause & goto :menu )
if "%OPT%"=="4" ( python calibration_diagnosis.py --all & pause & goto :menu )
if "%OPT%"=="5" ( python claim_check.py --latest & pause & goto :menu )
if "%OPT%"=="6" goto :coder
if "%OPT%"=="7" goto :diag
if "%OPT%"=="8" ( python run_tests.py & pause & goto :menu )
if "%OPT%"=="9" goto :shell
if "%OPT%"=="0" exit /b 0
echo    Not an option.
echo.
goto :menu

:coder
REM The coder needs the server, and the server owns the webcam - so it
REM runs in its OWN window and stays there. Closing that window is how
REM you stop it.
echo    Starting the server in a separate window...
start "Eye-Tracking Server" cmd /k "cd /d "%CD%" && call .venv\Scripts\activate.bat && python app.py"
echo    Waiting for it to come up...
timeout /t 8 /nobreak >nul
start "" http://localhost:5050/coder
echo.
echo    The coding tool should be open in your browser.
echo    Close the "Eye-Tracking Server" window when you are done.
echo.
pause
goto :menu

:diag
echo   ----------------------------------------------------------------
echo    a  Which camera setting restores the frame rate
echo    b  Per-stage rate diagnosis (session-like conditions)
echo    c  Camera focal calibration (needs a tape measure)
echo    d  Back
echo   ----------------------------------------------------------------
set "D="
set /p D=   Choose:
echo.
if /i "%D%"=="a" ( python camera_remedy.py & pause & goto :diag )
if /i "%D%"=="b" ( python diagnose_rate.py --only session & pause & goto :diag )
if /i "%D%"=="c" (
    set "CM="
    set /p CM=   Measured distance, camera lens to bridge of nose, in cm:
    python camera_geometry.py --calibrate !CM! --measure
    pause & goto :diag
)
goto :menu

:shell
echo   ================================================================
echo    Environment active, folder set. Type "exit" to close.
echo.
echo    python verify_metrics.py --today
echo    python claim_check.py --latest
echo    python calibration_diagnosis.py --all
echo   ================================================================
echo.
cmd /k
