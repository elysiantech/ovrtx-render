# Modal smoke test for ovrtx 0.3.0.
#
# Purpose: re-check whether Modal's gVisor sandbox still SIGSEGVs on
# `renderer.step()`. This is the exact render path from smoke_test_handler.py
# (open_usd -> step -> map LdrColor), lifted into a Modal GPU function.
#
# Run:  modal run modal_smoke_test.py
#
# It always prints diagnostics (nvidia-smi + vulkaninfo) BEFORE touching the
# renderer, so even a hard crash tells us how far we got:
#   - Vulkan sees llvmpipe only  -> GPU/driver not reaching the sandbox (not the gVisor bug)
#   - Vulkan sees NVIDIA, step()  crashes/SIGSEGV -> gVisor bug still present
#   - step() returns a PNG        -> Modal fixed it
import modal

OVRTX_WHEEL = (
    "https://pypi.nvidia.com/ovrtx/"
    "ovrtx-0.3.0.312915-py3-none-manylinux_2_35_x86_64.whl"
)
USD_URL = (
    "https://omniverse-content-production.s3.us-west-2.amazonaws.com/"
    "Samples/Robot-OVRTX/robot-ovrtx.usda"
)

image = (
    # Ubuntu 22.04 == glibc 2.35, required by the ovrtx manylinux_2_35 wheel.
    modal.Image.from_registry("ubuntu:22.04", add_python="3.11")
    .apt_install(
        "xvfb",
        "libvulkan1",
        "vulkan-tools",
        "mesa-vulkan-drivers",
        "libgl1",
        "libglx-mesa0",
        "libgomp1",       # OpenMP runtime -- ovrtx libovrtx links against libgomp.so.1
        "libglvnd0",
        "libopengl0",     # libOpenGL.so.0 -- needed by omni.usd_resolver plugin
        "curl",
    )
    .pip_install("numpy", "pillow")
    .pip_install(OVRTX_WHEEL)
    # Register the NVIDIA Vulkan ICD so Vulkan can reach the driver Modal
    # injects at runtime (the driver libs are mounted, but the ICD json isn't).
    .run_commands(
        "mkdir -p /etc/vulkan/icd.d",
        'printf \'{"file_format_version":"1.0.0","ICD":{"library_path":'
        '"libGLX_nvidia.so.0","api_version":"1.3.277"}}\' '
        "> /etc/vulkan/icd.d/nvidia_icd.json",
    )
    .env(
        {
            "NVIDIA_DRIVER_CAPABILITIES": "all",
            "NVIDIA_VISIBLE_DEVICES": "all",
            "VK_ICD_FILENAMES": "/etc/vulkan/icd.d/nvidia_icd.json",
        }
    )
)

app = modal.App("ovrtx-modal-smoke-test")


def _run(cmd, timeout=60):
    import subprocess

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"<error: {e}>"


@app.function(image=image, gpu="L40S", timeout=600)
def smoke_test(warmup: int = 8):
    import os
    import time
    import traceback

    diag = {}

    # --- Diagnostics BEFORE the renderer, so a crash is still informative. ---
    diag["nvidia_smi"] = _run(
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
         "--format=csv,noheader"]
    )
    vk = _run(["vulkaninfo", "--summary"])
    diag["vulkan"] = [
        l.strip() for l in vk.splitlines()
        if any(k in l for k in ("deviceName", "driverName", "driverInfo", "GPU id"))
    ][:12] or vk[:800]
    diag["nvidia_gl_lib"] = _run(
        ["bash", "-c", "ldconfig -p | grep -iE 'GLX_nvidia|nvidia.*vulkan|libvulkan' || echo none"]
    )
    print("=== DIAGNOSTICS ===")
    print("nvidia-smi:", diag["nvidia_smi"])
    print("vulkan:", diag["vulkan"])

    # --- Headless display for Vulkan. ---
    os.system("Xvfb :99 -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &")
    os.environ["DISPLAY"] = ":99"
    time.sleep(2)

    try:
        import numpy as np
        import ovrtx
        from PIL import Image

        diag["ovrtx_version"] = getattr(ovrtx, "__version__", "unknown")
        print("ovrtx version:", diag["ovrtx_version"])

        # Download the scene locally to isolate the renderer from the URL resolver.
        import urllib.request

        local_usd = "/tmp/scene.usda"
        urllib.request.urlretrieve(USD_URL, local_usd)
        print(f"downloaded USD -> {local_usd} ({os.path.getsize(local_usd)} bytes)")

        renderer = ovrtx.Renderer()
        renderer.open_usd(local_usd)
        print("open_usd OK -- calling step() (this is where gVisor SIGSEGV'd)...")

        products = None
        for i in range(max(1, warmup)):
            products = renderer.step(
                render_products={"/Render/Camera"}, delta_time=1.0 / 60
            )
            print(f"  step {i + 1}/{warmup} OK")

        img = None
        for _name, product in products.items():
            for frame in product.frames:
                var = frame.render_vars["LdrColor"].map(device=ovrtx.Device.CPU)
                img = Image.fromarray(np.from_dlpack(var))

        if img is None:
            return {"ok": False, "error": "no frames produced", "diag": diag}

        import base64
        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        px = np.asarray(img)
        return {
            "ok": True,
            "diag": diag,
            "width": img.width,
            "height": img.height,
            "pixel_mean": float(px.mean()),   # >0 and non-flat => a real render
            "pixel_std": float(px.std()),
            "image_base64": base64.b64encode(buf.getvalue()).decode(),
            "note": "renderer.step() SURVIVED under Modal gVisor",
        }
    except BaseException as e:  # noqa: BLE001  (catch SystemExit too)
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
            "diag": diag,
        }


@app.local_entrypoint()
def main():
    import json

    result = smoke_test.remote()
    print("\n=== RESULT ===")
    print(json.dumps({k: v for k, v in result.items() if k != "image_base64"}, indent=2))

    if result.get("image_base64"):
        import base64

        out = "modal_render.png"
        with open(out, "wb") as f:
            f.write(base64.b64decode(result["image_base64"]))
        print(f"saved render -> {out}")
