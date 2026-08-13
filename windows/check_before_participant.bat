@echo off
REM ====================================================================
REM  PRE-FLIGHT - run once per collection day, before the first
REM  participant arrives.
REM ====================================================================
REM
REM  Every check here has already caught a real failure on this project:
REM
REM    run_tests.py        caught a self-referential data-quality metric
REM                        that scored every session ~100 %%, and a
REM                        NameError that silently disabled the distance
REM                        estimate.
REM    perf_mode --verify  the EcoQoS opt-out is what separates 30 Hz
REM                        from 12 Hz.
REM    verify_metrics      caught sessions with 3 validation targets
REM                        instead of 7, and sessions below the
REM                        pre-registered inclusion thresholds.
REM
REM  Two minutes here protects a whole day of recruitment.
REM ====================================================================

cd /d "%~dp0.."
call .venv\Scripts\activate.bat

echo.
echo ================== 1. dependencies =====================
python -c "import psutil, numpy, cv2, mediapipe, MNN, gazefollower; print('  all imports OK')" || (
    echo   MISSING DEPENDENCIES - run: pip install -r requirements.txt
    pause & exit /b 1
)

echo.
echo ================== 2. test suite =======================
python run_tests.py
if errorlevel 1 (
    echo.
    echo   TESTS FAILED - do not record until this is resolved.
    pause & exit /b 1
)

echo.
echo ================== 3. performance mode =================
python perf_mode.py --verify

echo.
echo ================== 4. tracker self-check ===============
python tracker_service.py --check

echo.
echo ================== 5. yesterday's sessions =============
REM Deliberately NOT gated on errorlevel: a gap in an earlier session
REM is worth seeing, but it is not a reason to stop today's recording.
REM A TRACEBACK here is different from a MISSING metric - the first is
REM a bug in the reporting tool and should be reported, the second is
REM data that was never produced.
python verify_metrics.py --today
if errorlevel 1 (
    echo.
    echo   ^(gaps above are informational - they do not block today^)
)

echo.
echo ========================================================
echo   Pre-flight complete. Start participants with:
echo       windows\run_session.bat
echo ========================================================
pause
