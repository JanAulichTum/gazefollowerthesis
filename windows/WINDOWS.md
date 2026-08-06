# Running the experiment on Windows

**The application code is cross-platform — you run the *same* files as on
macOS.** This folder only holds Windows-specific setup helpers; do not
copy or fork the Python code. Keeping one source tree avoids the two
copies drifting apart.

## Why it already works on Windows

- No POSIX-only dependencies (`fcntl`, `resource`, `os.fork`, eventlet,
  signals) — Flask-SocketIO runs in `threading` async mode.
- All macOS-specific code (Dock visibility, MNN dylib preload, the
  Rosetta check) is guarded with `sys.platform != "darwin"` and is a
  no-op on Windows.
- Paths use `os.path.join`; atomic Excel writes use `os.replace`, which
  is atomic on Windows too.
- The tracker subprocess reads stdin with a plain blocking loop on
  Windows (macOS uses a `selectors` timeout to pump its pygame/NSApp
  queue, which Windows doesn't need).

## Setup

1. Install **Python 3.11 (64-bit)** from python.org — tick *"Add
   python.exe to PATH"*. 3.11 x64 has the best wheel coverage for the
   dependencies (notably **MNN**, GazeFollower's inference backend).
2. Open **Command Prompt** in the `eye_tracking_experiment` folder.
3. Run:
   ```
   windows\setup_windows.bat
   ```
   This creates `.venv\`, installs `requirements.txt`, and runs the
   tracker self-check.

## Running

```
.venv\Scripts\python.exe app.py
```
Then open <http://localhost:5050>. Test run (use the `--test` flag —
`TEST_MODE=1 python app.py` is bash-only and does NOT work in cmd /
PowerShell):
```
.venv\Scripts\python.exe app.py --test
```
or just double-click / run `windows\run_test.bat`.
Integrity suite:
```
.venv\Scripts\python.exe run_tests.py
```

## Known issue: mediapipe on Python 3.12 (FIXED via pin)

GazeFollower uses MediaPipe's legacy `mediapipe.solutions` API. The
newest mediapipe (0.10.31) ships a protobuf that is incompatible with
Python 3.12, so `mediapipe.solutions` fails to load and the tracker
reports:

```
gazefollower: FAIL: module 'mediapipe' has no attribute 'solutions'
```

`requirements.txt` now pins `mediapipe==0.10.21` (compatible protobuf,
wheels for all platforms), so a fresh setup is fine. If you already have
a broken environment, just run:

```
.venv\Scripts\python.exe -m pip install "mediapipe==0.10.21"
```

then restart the server. The tracker self-check now detects this exact
case and prints the fix.

## The one real dependency risk: MNN

GazeFollower depends on **MNN**. On macOS Apple-Silicon the wheels are
broken (hence `fix_environment.sh`); on Windows the issue is simply
whether a wheel exists for your Python version. If `pip install` fails on
MNN:

- Use **Python 3.11 x64** (most wheels target it).
- Pin a version: `pip install "MNN==3.2.0"` (or 3.0.0 / 2.8.1).
- Confirm with `.venv\Scripts\python.exe tracker_service.py --check` —
  the `MNN`, `gazefollower`, and `camera` lines must say `ok`.

## Windows notes

- **Camera permission**: Settings → Privacy & security → Camera → allow
  desktop apps. If the self-check's `camera` line says it opened but
  returned no frame, that's the permission or another app holding the
  webcam.
- **Firewall**: the first run may prompt to allow Python on the local
  network — allow it (the server binds `0.0.0.0:5050`); it's local-only.
- **Fullscreen calibration** uses pygame and works the same; press the
  key shown on screen to advance/stop the preview.
- Everything else (validation, gain correction, position guide, LLM
  feedback, quality report, agreement kit) is pure Python and behaves
  identically.
