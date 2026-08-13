@echo off
REM Live webcam FPS + brightness meter (Windows).
REM Stop the experiment server first - only one process can use the camera.
REM Watch FPS drop as the room darkens and rise with more front light.
cd /d "%~dp0\.."
".venv\Scripts\python.exe" camera_fps_test.py %*
