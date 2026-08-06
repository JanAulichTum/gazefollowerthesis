# Model weights (not included in this repository)

This repository is public, and the gaze-estimation network is **not
ours to redistribute** — so it is excluded. This file records what the
code expects, so anyone who obtains the weights independently can
reproduce a session.

Get them by installing the upstream package, which ships its own copy:

```bash
pip install gazefollower
```

The tracker falls back to that bundled copy automatically, so **no
manual download is needed** for a normal setup.

## The file

```
models/base_32M.mnn      ~7.3 MB
```

`tracker_service.py` looks here first and falls back to the copy bundled
inside the installed `gazefollower` package
(`gazefollower/res/model_weights/base.mnn`). Set `GF_MODEL_PATH` to
point somewhere else.

## Where it comes from

GazeFollower — MGazeNet, run through the MNN runtime.

- Upstream: https://github.com/GanchengZhu/GazeFollower
- Paper: Zhu et al. (2025), https://dl.acm.org/doi/10.1145/3729410
- Licence: **CC BY-NC-SA** (research use; not ours to redistribute)

Installing the package pulls its own copy:

```bash
pip install gazefollower
```

## Why the exact build matters for the thesis

Inference cost sets the ceiling on the sampling rate, and the sampling
rate determines fixation-duration quantisation. On the collection
machine the gaze CNN accounted for roughly two thirds of per-frame cost
(~20 ms of ~30 ms). A different model build, or a different MNN
backend, changes the recorded data — so it is a methods-section fact,
not an implementation detail.

Record alongside your results:

- model file and size (`base_32M.mnn`, 7.3 MB)
- MNN version and backend actually in force — note MNN **silently falls
  back to CPU** when a requested GPU backend is not compiled into the
  installed wheel, so verify with `python mnn_backend.py` rather than
  trusting the request
- thread count (GazeFollower hardcodes 4)

The tracker self-check reports all of these at startup, and they are
captured in every telemetry file under `environment.packages`.
