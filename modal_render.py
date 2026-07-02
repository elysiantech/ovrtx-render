# ovrtx 0.3.0 USDZ renderer on Modal.
#
# Renders a local .usdz to a PNG on a Modal GPU. Frames the model with a
# spherical camera + dome/key lighting, computed from the scene bounds.
#
# Run:
#   modal run modal_render.py --usdz "/path/to/model.usdz"
#   modal run modal_render.py --usdz "/path/to/model.usdz" --distance-multiplier 2.5 --azimuth 45 --elevation 30
#
# Bounds are computed with usd-core in an ISOLATED venv (clean env) because
# ovrtx bundles its own USD at /opt and the two registries conflict in-process.
import modal

OVRTX_WHEEL = (
    "https://pypi.nvidia.com/ovrtx/"
    "ovrtx-0.3.0.312915-py3-none-manylinux_2_35_x86_64.whl"
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
        "libgomp1",       # ovrtx libovrtx links libgomp.so.1
        "libglvnd0",
        "libopengl0",     # libOpenGL.so.0 -- needed by omni.usd_resolver plugin
        "python3-venv",
    )
    .pip_install("numpy", "pillow")
    .pip_install(OVRTX_WHEEL)
    # Isolated venv with usd-core for bounds computation (kept out of the
    # ovrtx process to avoid TfType registry conflicts with bundled USD).
    .run_commands(
        "python3 -m venv /opt/usdvenv",
        "/opt/usdvenv/bin/pip install --no-cache-dir usd-core",
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

app = modal.App("ovrtx-render")

# usd-core script: reads /tmp/input.usdz, computes bounds, writes /tmp/wrapper.usda
# framing the model. Runs in the isolated venv with a clean environment.
_WRAPPER_SCRIPT = r'''
import math, sys
from pxr import Usd, UsdGeom, UsdRender, UsdLux, Sdf, Gf

distance_multiplier, azimuth, elevation, width, height = (
    float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]),
    int(sys.argv[4]), int(sys.argv[5]),
)

src = Usd.Stage.Open('/tmp/input.usdz')
up_axis = src.GetMetadata('upAxis') or 'Y'
root = src.GetDefaultPrim()
root_path = root.GetPath() if root else Sdf.Path('/Scene')

# CAD "Edge" curve prims usually have no width -> ovrtx draws them as fat
# 1-scene-unit tubes that blob over the model. Collect them (relative to the
# default prim) so the wrapper can hide them, and EXCLUDE them from the bounds
# so framing keys off the real solid geometry, not stray curves.
curve_rel_paths = []
for prim in src.Traverse():
    if prim.IsA(UsdGeom.BasisCurves) or prim.IsA(UsdGeom.NurbsCurves):
        curve_rel_paths.append(prim.GetPath().MakeRelativePath(root_path))

# Bounds from mesh geometry only (ignore curves).
cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                         [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy])
mesh_range = Gf.Range3d()
for prim in src.Traverse():
    if prim.IsA(UsdGeom.Mesh):
        b = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        if not b.IsEmpty():
            mesh_range.UnionWith(b)
if mesh_range.IsEmpty():  # fallback: whole model
    mesh_range = cache.ComputeWorldBound(src.GetPrimAtPath(root_path)).ComputeAlignedRange()
mn, mx = mesh_range.GetMin(), mesh_range.GetMax()
cx, cy, cz = (mn[0]+mx[0])/2, (mn[1]+mx[1])/2, (mn[2]+mx[2])/2
dx, dy, dz = mx[0]-mn[0], mx[1]-mn[1], mx[2]-mn[2]
diagonal = math.sqrt(dx*dx + dy*dy + dz*dz)

distance = diagonal * distance_multiplier
az, el = math.radians(azimuth), math.radians(elevation)
if up_axis == 'Z':
    cam = Gf.Vec3d(cx + distance*math.cos(el)*math.sin(az),
                   cy + distance*math.cos(el)*math.cos(az),
                   cz + distance*math.sin(el))
    up = Gf.Vec3d(0, 0, 1)
else:
    cam = Gf.Vec3d(cx + distance*math.cos(el)*math.sin(az),
                   cy + distance*math.sin(el),
                   cz + distance*math.cos(el)*math.cos(az))
    up = Gf.Vec3d(0, 1, 0)

st = Usd.Stage.CreateNew('/tmp/wrapper.usda')
st.SetMetadata('upAxis', up_axis)
w = st.DefinePrim('/World', 'Xform')
m = st.DefinePrim('/World/Model', 'Xform')
m.GetReferences().AddReference('/tmp/input.usdz')

# PRUNE the width-less CAD curve prims (deactivate, don't just hide): SetActive
# (False) removes them from composition so Hydra never tessellates them into fat
# 1-unit tubes. Merely setting visibility=invisible still loads/tessellates them
# and blew up the render. Verified locally: 675 curves -> 0, 807 meshes kept.
for rel in curve_rel_paths:
    st.OverridePrim(Sdf.Path('/World/Model').AppendPath(rel)).SetActive(False)

sky = UsdLux.DomeLight.Define(st, '/World/DomeLight')
sky.CreateIntensityAttr(3000.0)
sky.CreateColorAttr(Gf.Vec3f(1.0, 1.0, 1.0))
sun = UsdLux.DistantLight.Define(st, '/World/KeyLight')
sun.CreateIntensityAttr(6000.0)
sun.AddRotateXYZOp().Set(Gf.Vec3f(-35.0, 45.0, 0.0))

cam_path = Sdf.Path('/World/Camera')
camera = UsdGeom.Camera.Define(st, cam_path)
look = Gf.Matrix4d(1.0)
look.SetLookAt(cam, Gf.Vec3d(cx, cy, cz), up)
camera.AddTransformOp().Set(look.GetInverse())
camera.CreateFocalLengthAttr(35.0)
camera.CreateHorizontalApertureAttr(36.0)
camera.CreateVerticalApertureAttr(24.0)
camera.CreateClippingRangeAttr(Gf.Vec2f(0.01, 100000.0))

rp = UsdRender.Product.Define(st, Sdf.Path('/Render/RenderProduct'))
rp.CreateCameraRel().SetTargets([cam_path])
rp.CreateResolutionAttr().Set(Gf.Vec2i(width, height))
rv = UsdRender.Var.Define(st, Sdf.Path('/Render/RenderProduct/LdrColor'))
rv.CreateSourceNameAttr().Set('LdrColor')
rv.CreateDataTypeAttr().Set('color4f')
rp.CreateOrderedVarsRel().SetTargets([Sdf.Path('/Render/RenderProduct/LdrColor')])
st.SetDefaultPrim(w)
st.GetRootLayer().Save()

print(f'BOUNDS_MIN:{mn[0]},{mn[1]},{mn[2]}')
print(f'BOUNDS_MAX:{mx[0]},{mx[1]},{mx[2]}')
print(f'CAMERA_DISTANCE:{distance}')
print(f'UP_AXIS:{up_axis}')
print(f'CURVES_HIDDEN:{len(curve_rel_paths)}')
'''


@app.function(image=image, gpu="L40S", timeout=900)
def render(usdz_bytes: bytes, distance_multiplier: float = 3.0, azimuth: float = 45.0,
           elevation: float = 30.0, width: int = 1920, height: int = 1080,
           warmup_frames: int = 24):
    import os
    import subprocess
    import time
    import traceback

    with open("/tmp/input.usdz", "wb") as f:
        f.write(usdz_bytes)
    with open("/tmp/build_wrapper.py", "w") as f:
        f.write(_WRAPPER_SCRIPT)

    # Compute bounds + build wrapper in the isolated usd-core venv (clean env).
    clean_env = {"PATH": "/opt/usdvenv/bin:/usr/bin:/bin", "HOME": "/root",
                 "LD_LIBRARY_PATH": "", "PYTHONPATH": "", "PXR_PLUGINPATH_NAME": ""}
    p = subprocess.run(
        ["/opt/usdvenv/bin/python", "/tmp/build_wrapper.py",
         str(distance_multiplier), str(azimuth), str(elevation), str(width), str(height)],
        capture_output=True, text=True, env=clean_env,
    )
    if p.returncode != 0:
        return {"ok": False, "error": "build_wrapper failed", "stderr": p.stderr}
    meta = {}
    for line in p.stdout.splitlines():
        if line.startswith("BOUNDS_MIN:"):
            meta["bounds_min"] = [float(x) for x in line.split(":")[1].split(",")]
        elif line.startswith("BOUNDS_MAX:"):
            meta["bounds_max"] = [float(x) for x in line.split(":")[1].split(",")]
        elif line.startswith("CAMERA_DISTANCE:"):
            meta["camera_distance"] = float(line.split(":")[1])
        elif line.startswith("UP_AXIS:"):
            meta["up_axis"] = line.split(":")[1]
        elif line.startswith("CURVES_HIDDEN:"):
            meta["curves_hidden"] = int(line.split(":")[1])
    print("bounds:", meta)

    os.system("Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &")
    os.environ["DISPLAY"] = ":99"
    time.sleep(2)

    try:
        import base64
        import io

        import numpy as np
        import ovrtx
        from PIL import Image

        renderer = ovrtx.Renderer()
        renderer.open_usd("/tmp/wrapper.usda")

        products = None
        for i in range(max(1, warmup_frames)):
            products = renderer.step(render_products={"/Render/RenderProduct"},
                                     delta_time=1.0 / 60)

        img = None
        for _name, product in products.items():
            for frame in product.frames:
                if "LdrColor" in frame.render_vars:
                    var = frame.render_vars["LdrColor"].map(device=ovrtx.Device.CPU)
                    img = Image.fromarray(np.from_dlpack(var))

        if img is None:
            return {"ok": False, "error": "no frame produced", "meta": meta}

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        px = np.asarray(img)
        return {
            "ok": True,
            "meta": meta,
            "width": img.width,
            "height": img.height,
            "pixel_mean": float(px.mean()),
            "pixel_std": float(px.std()),
            "image_base64": base64.b64encode(buf.getvalue()).decode(),
        }
    except BaseException as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(), "meta": meta}


@app.local_entrypoint()
def main(usdz: str, distance_multiplier: float = 3.0, azimuth: float = 45.0,
         elevation: float = 30.0, width: int = 1920, height: int = 1080,
         warmup_frames: int = 24, out: str = "render.png"):
    import base64
    import json
    import os

    with open(usdz, "rb") as f:
        data = f.read()
    print(f"uploading {usdz} ({len(data)} bytes)")

    result = render.remote(data, distance_multiplier, azimuth, elevation,
                           width, height, warmup_frames)
    print(json.dumps({k: v for k, v in result.items() if k != "image_base64"}, indent=2))

    if result.get("image_base64"):
        with open(out, "wb") as f:
            f.write(base64.b64decode(result["image_base64"]))
        print(f"saved -> {os.path.abspath(out)}")
