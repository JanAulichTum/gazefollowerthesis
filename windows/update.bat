@echo off
REM ====================================================================
REM  UPDATE THIS MACHINE FROM GITHUB - double-click, or run from cmd.
REM ====================================================================
REM
REM  The counterpart to push.command on the Mac. Pulls the latest code,
REM  reinstalls dependencies only if they changed, and runs the test
REM  suite - because a pull that lands mid-refactor and a pull that
REM  lands cleanly look identical until something is executed.
REM
REM  Safe to run before any collection day. It does not touch data\.
REM ====================================================================

cd /d "%~dp0.."

echo.
echo ======================================================
echo   UPDATING FROM GITHUB
echo ======================================================
echo.

git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
    echo   This folder is not a git repository - nothing to update.
    pause & exit /b 1
)

REM Refuse to clobber local edits. Overwriting a change made on the
REM collection machine, unrecorded, is far worse than stopping here.
for /f %%i in ('git status --porcelain 2^>nul ^| find /c /v ""') do set DIRTY=%%i
if not "%DIRTY%"=="0" (
    echo   You have %DIRTY% uncommitted change^(s^) on THIS machine:
    echo.
    git status --short
    echo.
    echo   Refusing to pull over them. Either commit them, or discard
    echo   them with:   git checkout -- .
    echo.
    pause & exit /b 1
)

echo   Before:
git log --oneline -1
echo.

git pull --ff-only
if errorlevel 1 (
    echo.
    echo   PULL FAILED. The branches have diverged, which needs a
    echo   human decision rather than a script. Run:  git status
    pause & exit /b 1
)

echo.
echo   After:
git log --oneline -1
echo.

REM Dependencies: only reinstall when the file actually changed, so the
REM common case stays fast.
git diff --name-only HEAD@{1} HEAD 2>nul | findstr /i "requirements.txt" >nul
if not errorlevel 1 (
    echo   requirements.txt changed - reinstalling...
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
    echo.
)

echo ======================================================
echo   VERIFYING
echo ======================================================
call .venv\Scripts\activate.bat
python run_tests.py
if errorlevel 1 (
    echo.
    echo   TESTS FAILED after the update. Do NOT record participants
    echo   until this is resolved.
    pause & exit /b 1
)

echo.
echo   Update complete. Start a participant with:
echo       windows\run_session.bat
echo.
pause
