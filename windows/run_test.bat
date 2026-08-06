@echo off
REM Start the experiment in TEST MODE on Windows.
REM (`TEST_MODE=1 python app.py` is bash-only; use the --test flag here.)
cd /d "%~dp0\.."
".venv\Scripts\python.exe" app.py --test
