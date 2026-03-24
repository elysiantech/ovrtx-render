import base64, io, os, subprocess, textwrap, math, requests, runpod
from PIL import Image
import numpy as np


def _build_wrapper_with_usd_core(url: str, distance_multiplier: float = 3.0, azimuth: float = 45.0, elevation: float = 30.0, width: int = 1920, height: int = 1080):
    in_path='/tmp/input.usdz'
    with open(in_path,'wb') as f:
        f.write(requests.get(url,timeout=180).content)

    # Isolated venv for usd-core to avoid ovrtx conflict
    venv_path = '/tmp/usdvenv'
    if not os.path.exists(f'{venv_path}/bin/python'):
        # Clean environment for venv creation to avoid inheriting ovrtx paths
        clean_env = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LD_LIBRARY_PATH': '', 'PYTHONPATH': '', 'PXR_PLUGINPATH_NAME': ''}
        subprocess.check_call(['python3','-m','venv',venv_path], env=clean_env)
        subprocess.check_call([f'{venv_path}/bin/pip','install','--no-cache-dir','usd-core'], env=clean_env)

    script=textwrap.dedent(f'''
        import math
        from pxr import Usd, UsdGeom, UsdRender, UsdLux, Sdf, Gf

        src=Usd.Stage.Open('/tmp/input.usdz')
        root=src.GetDefaultPrim()
        root_path=root.GetPath() if root else Sdf.Path('/Scene')
        cache=UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy])
        rng=cache.ComputeWorldBound(src.GetPrimAtPath(root_path)).ComputeAlignedRange()
        mn=rng.GetMin(); mx=rng.GetMax()
        cx, cy, cz = (mn[0]+mx[0])/2, (mn[1]+mx[1])/2, (mn[2]+mx[2])/2

        # Scene diagonal
        dx, dy, dz = mx[0]-mn[0], mx[1]-mn[1], mx[2]-mn[2]
        diagonal = math.sqrt(dx*dx + dy*dy + dz*dz)

        # Camera parameters
        distance_multiplier = {distance_multiplier}
        azimuth = {azimuth}
        elevation = {elevation}
        width = {width}
        height = {height}

        distance = diagonal * distance_multiplier
        az_rad = math.radians(azimuth)
        el_rad = math.radians(elevation)

        # Camera position (spherical coords, Y-up for USD)
        cam_x = cx + distance * math.cos(el_rad) * math.sin(az_rad)
        cam_y = cy + distance * math.sin(el_rad)
        cam_z = cz + distance * math.cos(el_rad) * math.cos(az_rad)

        st=Usd.Stage.CreateNew('/tmp/wrapper.usda')
        st.SetMetadata('upAxis','Y')
        w=st.DefinePrim('/World','Xform')
        m=st.DefinePrim('/World/Model','Xform')
        m.GetReferences().AddReference('/tmp/input.usdz')

        sky=UsdLux.DomeLight.Define(st,'/World/DomeLight')
        sky.CreateIntensityAttr(3000.0)
        sky.CreateColorAttr(Gf.Vec3f(1.0,1.0,1.0))

        sun=UsdLux.DistantLight.Define(st,'/World/KeyLight')
        sun.CreateIntensityAttr(6000.0)
        sun.AddRotateXYZOp().Set(Gf.Vec3f(-35.0,45.0,0.0))

        # Single camera with configurable position
        cam_path=Sdf.Path('/World/Camera')
        cam=UsdGeom.Camera.Define(st,cam_path)

        # Look-at transform
        pos = Gf.Vec3d(cam_x, cam_y, cam_z)
        target = Gf.Vec3d(cx, cy, cz)
        up = Gf.Vec3d(0, 1, 0)
        look=Gf.Matrix4d(1.0)
        look.SetLookAt(pos, target, up)
        cam.AddTransformOp().Set(look.GetInverse())
        cam.CreateFocalLengthAttr(35.0)
        cam.CreateHorizontalApertureAttr(36.0)
        cam.CreateVerticalApertureAttr(24.0)
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.01, 100000.0))

        rp_path=Sdf.Path('/Render/RenderProduct')
        rp=UsdRender.Product.Define(st,rp_path)
        rp.CreateCameraRel().SetTargets([cam_path])
        rp.CreateResolutionAttr().Set(Gf.Vec2i(width, height))
        rv_path=Sdf.Path('/Render/RenderProduct/LdrColor')
        rv=UsdRender.Var.Define(st,rv_path)
        rv.CreateSourceNameAttr().Set('LdrColor')
        rv.CreateDataTypeAttr().Set('color4f')
        rp.CreateOrderedVarsRel().SetTargets([rv_path])

        st.SetDefaultPrim(w)
        st.GetRootLayer().Save()

        # Output bounds for response
        print(f'BOUNDS_MIN:{{mn[0]}},{{mn[1]}},{{mn[2]}}')
        print(f'BOUNDS_MAX:{{mx[0]}},{{mx[1]}},{{mx[2]}}')
        print(f'CAMERA_DISTANCE:{{distance}}')
        print('WRAPPER_OK')
    ''')
    with open('/tmp/build_wrapper.py','w') as f:
        f.write(script)
    # Run with clean environment to avoid ovrtx USD conflicts
    # Explicitly clear LD_LIBRARY_PATH and PYTHONPATH to prevent shared library conflicts
    clean_env = {
        'PATH': f'{venv_path}/bin:/usr/bin:/bin',
        'HOME': '/root',
        'LD_LIBRARY_PATH': '',
        'PYTHONPATH': '',
        'PXR_PLUGINPATH_NAME': '',
    }
    result = subprocess.run([f'{venv_path}/bin/python','/tmp/build_wrapper.py'],
                           capture_output=True, text=True, env=clean_env)
    if result.returncode != 0:
        raise RuntimeError(f"build_wrapper failed: {result.stderr}")

    # Parse output for metadata
    meta = {}
    for line in result.stdout.split('\n'):
        if line.startswith('BOUNDS_MIN:'):
            meta['bounds_min'] = [float(x) for x in line.split(':')[1].split(',')]
        elif line.startswith('BOUNDS_MAX:'):
            meta['bounds_max'] = [float(x) for x in line.split(':')[1].split(',')]
        elif line.startswith('CAMERA_DISTANCE:'):
            meta['camera_distance'] = float(line.split(':')[1])
    return meta


def handler(job):
    try:
        inp=job.get('input',{})
        url=inp.get('usdz_url') or inp.get('usd_url')
        if not url:
            return {'ok':False,'error':'missing usdz_url'}

        # Configurable parameters with defaults
        distance_multiplier = float(inp.get('distance_multiplier', 3.0))
        azimuth = float(inp.get('azimuth', 45.0))
        elevation = float(inp.get('elevation', 30.0))
        width = int(inp.get('width', 1920))
        height = int(inp.get('height', 1080))
        warmup_frames = int(inp.get('warmup_frames', 10))
        output_format = inp.get('format', 'png').lower()

        meta = _build_wrapper_with_usd_core(url, distance_multiplier, azimuth, elevation, width, height)

        os.environ.setdefault('DISPLAY',':99')
        os.system('pkill Xvfb >/dev/null 2>&1 || true')
        os.system('Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &')

        import ovrtx
        r=ovrtx.Renderer()
        r.add_usd('/tmp/wrapper.usda')

        render_product = '/Render/RenderProduct'

        # Warmup frames for path tracer convergence
        for _ in range(warmup_frames):
            r.step(render_products={render_product}, delta_time=1.0/60.0)

        # Final render
        outs=r.step(render_products={render_product}, delta_time=1.0/60.0)

        rendered_image = None
        for _,prod in outs.items():
            for frame in prod.frames:
                if 'LdrColor' in frame.render_vars:
                    with frame.render_vars['LdrColor'].map(device=ovrtx.Device.CPU) as var:
                        rendered_image = np.from_dlpack(var.tensor)
                        break

        if rendered_image is None:
            return {'ok':False,'error':'No frame rendered'}

        img = Image.fromarray(rendered_image)
        buf=io.BytesIO()
        img_format = output_format.upper()
        if img_format == 'JPEG':
            img = img.convert('RGB')
        img.save(buf, format=img_format if img_format in ('PNG','JPEG') else 'PNG')

        return {
            'ok': True,
            'image_base64': base64.b64encode(buf.getvalue()).decode('ascii'),
            'format': output_format,
            'width': int(img.size[0]),
            'height': int(img.size[1]),
            'scene_bounds': {
                'min': meta.get('bounds_min', []),
                'max': meta.get('bounds_max', []),
            },
            'camera': {
                'distance': meta.get('camera_distance', 0),
                'azimuth': azimuth,
                'elevation': elevation,
                'distance_multiplier': distance_multiplier,
            },
            'source': url,
        }
    except Exception as e:
        import traceback
        return {'ok':False,'error':str(e),'traceback':traceback.format_exc()}

runpod.serverless.start({'handler':handler})
