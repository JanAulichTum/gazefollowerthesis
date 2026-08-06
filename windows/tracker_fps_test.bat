@echo off
REM Measure GazeFollower's REAL sample rate (inference included) on Windows.
REM Stop the experiment server first (it owns the camera + model).
REM Compare the result with camera_fps_test.bat: if the camera does ~30 FPS
REM but this is ~13 Hz, the bottleneck is inference/CPU, not lighting.
cd /d "%~dp0\.."
".venv\Scripts\python.exe" tracker_fps_test.py %*
