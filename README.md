# ovrtx-render

USDZ rendering service using NVIDIA ovrtx on RunPod serverless with RTX 5090 GPUs.

## Current Deployment

| Resource | ID | Notes |
|----------|-----|-------|
| **Template** | `u22zdyfhjp` | ovrtx-render-v3 |
| **Endpoint** | `hno4cb59647lfb` | ovrtx-render |
| **GPU** | RTX 5090 | 32GB VRAM |

## API

**Endpoint URL:** `https://api.runpod.ai/v2/hno4cb59647lfb/runsync`

### Request

```bash
curl -X POST "https://api.runpod.ai/v2/hno4cb59647lfb/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "usdz_url": "https://example.com/model.usdz",
      "distance_multiplier": 3.0,
      "azimuth": 45.0,
      "elevation": 30.0,
      "width": 1920,
      "height": 1080,
      "warmup_frames": 10,
      "format": "png"
    }
  }'
```

### Input Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `usdz_url` | string | **required** | URL to USDZ file |
| `distance_multiplier` | float | 3.0 | Camera distance as multiple of scene diagonal |
| `azimuth` | float | 45.0 | Horizontal camera angle in degrees |
| `elevation` | float | 30.0 | Vertical camera angle in degrees |
| `width` | int | 1920 | Output image width |
| `height` | int | 1080 | Output image height |
| `warmup_frames` | int | 10 | Path tracer convergence frames |
| `format` | string | "png" | Output format: "png" or "jpeg" |

### Response

```json
{
  "ok": true,
  "image_base64": "iVBORw0KGgo...",
  "format": "png",
  "width": 1920,
  "height": 1080,
  "scene_bounds": {
    "min": [-14.57, -0.02, -12.24],
    "max": [18.81, 33.26, 12.24]
  },
  "camera": {
    "distance": 159.37,
    "azimuth": 45.0,
    "elevation": 30.0,
    "distance_multiplier": 3.0
  }
}
```

## Deploy

```bash
./deploy.sh
```

Creates both template and endpoint. Outputs the endpoint URL when done.

> **Note:** Template is created via GraphQL API because `runpodctl template create --serverless` has a bug.

### Manage Endpoints

```bash
runpodctl serverless list              # List endpoints
runpodctl serverless delete <id>       # Delete endpoint
```

## What NOT to Use

### Flash SDK

Do not use `flash run` or `flash deploy`. Flash:
- Scans ALL `.py` files for `@Endpoint` decorators
- Creates multiple endpoints (GPU worker, load balancer, CPU worker)
- Appends `-fb` suffix to endpoint names
- `startScript` in `PodTemplate` does not execute on workers

### Modal

Do not use Modal for ovrtx. Modal's gvisor sandbox causes SIGSEGV on `renderer.step()`.

## Files

| File | Purpose |
|------|---------|
| `deploy.sh` | Creates template + endpoint |
| `ovrtx_render.py` | Handler source (reference only, embedded in template) |
| `.env` | RUNPOD_API_KEY |

## Template Config

- **Image:** `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`
- **Disk:** 50GB
- **GPU:** RTX 5090
- **Installs:** xvfb, libvulkan1, vulkan-tools, mesa-vulkan-drivers, ovrtx

## Handler Architecture

The handler:
1. Downloads USDZ from URL
2. Creates isolated venv with `usd-core` in a clean environment (clears `LD_LIBRARY_PATH`, `PYTHONPATH`, `PXR_PLUGINPATH_NAME` to avoid conflicts with ovrtx's bundled USD)
3. Uses usd-core to compute scene bounds
4. Generates `wrapper.usda` with camera positioned using spherical coordinates
5. Starts Xvfb for headless Vulkan
6. Runs ovrtx renderer with warmup frames
7. Returns PNG/JPEG as base64

> **Why environment isolation?** ovrtx bundles its own USD at `/opt/USD/` with custom schemas. Without clearing environment variables, the usd-core subprocess loads both USD installations, causing TfType registry conflicts.
