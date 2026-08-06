@echo off
REM ============================================================================
REM  One-shot Windows setup for the eye-tracking experiment.
REM
REM  Run from the eye_tracking_experiment folder (NOT from windows\):
REM      windows\setup_windows.bat
REM
REM  Unlike the macOS fix_environment.sh, Windows has no Rosetta / broken-
REM  MNN-dylib problem, so this just creates a venv, installs requirements,
REM  and runs the tracker self-check.
REM ============================================================================
setlocal

REM Move to the project root (parent of this windows\ folder)
cd /d "%~dp0\.."

echo Current Python:
py -3 -c "import sys, platform; print(sys.executable, sys.version.split()[0], platform.machine())" 2>nul
if errorlevel 1 (
    echo.
    echo [X] Python launcher 'py' not found. Install Python 3.11 x64 from
    echo     https://www.python.org/downloads/  ^(check "Add python.exe to PATH"^),
    echo     then re-run this script.
    exit /b 1
)

echo.
echo -- Creating virtual environment .venv --
py -3 -m venv .venv
if errorlevel 1 ( echo [X] venv creation failed & exit /b 1 )

set "PYEXE=.venv\Scripts\python.exe"

echo.
echo -- Upgrading pip --
"%PYEXE%" -m pip install --upgrade pip

echo.
echo -- Installing requirements --
"%PYEXE%" -m pip install --no-cache-dir -r requirements.txt
if errorlevel 1 (
    echo.
    echo [!] Requirements install hit an error. The usual culprit on Windows
    echo     is MNN ^(the GazeFollower backend^). Try a specific version:
    echo         "%PYEXE%" -m pip install "MNN==3.2.0"
    echo     If no MNN wheel exists for your Python, install Python 3.11 x64
    echo     ^(best-supported^) and re-run.
)

echo.
echo -- Pinning mediapipe (GazeFollower needs the legacy solutions API; --
echo --  the newest mediapipe breaks it on Python 3.12) --
"%PYEXE%" -m pip install "mediapipe==0.10.21"

echo.
echo -- Tracker self-check --
"%PYEXE%" tracker_service.py --check

echo.
echo Done. Start the server with:
echo     .venv\Scripts\python.exe app.py
echo Then open http://localhost:5050 in your browser.
endlocal
