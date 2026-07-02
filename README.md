# ovrtx-render

USDZ rendering with NVIDIA **ovrtx 0.3.0** on **Modal** serverless GPUs (L40S).

## Status

Modal works. As of 2026-07-02, ovrtx 0.3.0 renders end-to-end on Modal's gVisor
sandbox — `renderer.step()` no longer SIGSEGVs (it did in earlier attempts; the
issue is gone). Verified on an L40S with the NVIDIA robot sample and local USDZ
files.

Getting there required three image fixes, none of them gVisor-related:

| Symptom | Fix |
|---------|-----|
| `libgomp.so.1: cannot open shared object file` | `apt install libgomp1` |
| `libOpenGL.so.0` missing → `open_usd` fails on the URL resolver | `apt install libopengl0` |
| `ovrtx-…manylinux_2_35…whl is not a supported wheel` | base image Ubuntu 22.04 (glibc ≥ 2.35) |

`nvidia-smi` sees the GPU and ovrtx's bundled Vulkan binds device 0. The
standalone `vulkaninfo` tool reports `ERROR_INCOMPATIBLE_DRIVER` — that's a
loader quirk unrelated to ovrtx, which brings its own Vulkan.

## Files

| File | Purpose |
|------|---------|
| `modal_render.py` | USDZ → PNG renderer. Frames the model with a spherical camera + dome/key lighting from scene bounds. |
| `modal_smoke_test.py` | Minimal proof that `renderer.step()` survives on Modal (fixed NVIDIA robot scene). |

## Render a USDZ

```bash
cd ovrtx-render

# smallest-to-largest, framed automatically
modal run modal_render.py --usdz "/path/to/model.usdz"

# custom camera / resolution / convergence
modal run modal_render.py \
  --usdz "/path/to/model.usdz" \
  --distance-multiplier 2.5 \
  --azimuth 45 --elevation 30 \
  --width 1920 --height 1080 \
  --warmup-frames 32 \
  --out render.png
```

Output PNG is written locally (`render.png` by default).

## Smoke test

```bash
modal run modal_smoke_test.py     # renders the NVIDIA robot sample, saves modal_render.png
```

## How it works

1. Local entrypoint reads the `.usdz` and ships its bytes to the GPU function.
2. On the worker, **usd-core in an isolated venv** (clean `LD_LIBRARY_PATH` /
   `PYTHONPATH` / `PXR_PLUGINPATH_NAME`) computes world bounds and writes a
   `wrapper.usda` that references the model, positions a look-at camera by
   spherical coords, and adds a dome + distant key light. It also **prunes
   width-less CAD "Edge" curve prims** (`SetActive(False)`) — ovrtx would
   otherwise draw them as fat 1-unit tubes that swamp the model and can crash the
   render. The venv isolation avoids TfType registry conflicts with ovrtx's
   bundled USD at `/opt`.
3. Xvfb provides a headless display; ovrtx opens `wrapper.usda`, runs warmup
   frames for path-tracer convergence, and maps the `LdrColor` render var to CPU.
4. The frame comes back as base64 PNG.

## Notes

- **Cold start is slow (minutes).** Each run boots a fresh container and ovrtx
  compiles/caches shaders on first render. Nothing here reuses that cache across
  runs yet — a Modal Volume for the shader cache and/or a warm container would
  cut repeat-run latency. This is expected, not a hang.
- **Clean up ephemeral apps.** If a `modal run` client disconnects abnormally,
  its ephemeral app can linger holding the GPU. Check with `modal app list` and
  stop stragglers with `modal app stop <app-id>`.
- Image config: Ubuntu 22.04 + Python 3.11, ovrtx `0.3.0.312915` from
  `pypi.nvidia.com`, GPU `L40S`.

## Reference: NVIDIA ovrtx SDK

This project is self-contained — the Modal image installs ovrtx from the wheel
URL pinned in `modal_render.py`, and depends on nothing outside this repo.

For API guidance, the NVIDIA ovrtx SDK is the reference:
**https://github.com/NVIDIA-Omniverse/ovrtx** — its `skills/` and `examples/`
document the renderer API (`open_usd`, `step`, render vars, render settings,
etc.). It is a reference only, not a dependency.

To bump the ovrtx version: find the current build in that repo's
`examples/python/minimal/pyproject.toml` and update `OVRTX_WHEEL` in
`modal_render.py`.
