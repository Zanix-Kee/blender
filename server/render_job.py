"""Corre DENTRO de Blender (headless): blender -b file.blend -P render_job.py -- config.json
Tambien lista dispositivos: blender -b -P render_job.py -- devices.json  ({"action":"devices"})
"""
import json
import os
import sys

import bpy

cfg = json.load(open(sys.argv[sys.argv.index("--") + 1]))


def log(msg):
    print("[bfarm] " + msg, flush=True)


def setup_devices(scene, want):
    """want: auto | cpu | gpu | gpu+cpu. Activa TODAS las GPU encontradas (multi-GPU)."""
    prefs = bpy.context.preferences.addons["cycles"].preferences
    found = []
    for kind in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
        try:
            prefs.compute_device_type = kind
            try:
                prefs.refresh_devices()
            except Exception:
                prefs.get_devices()
        except Exception:
            continue
        gpus = [d for d in prefs.devices if d.type == kind]
        if gpus:
            found = gpus
            break
    cpus = [d for d in prefs.devices if d.type == "CPU"]
    return prefs, found, cpus


if cfg.get("action") == "devices":
    prefs, gpus, cpus = setup_devices(None, "auto")
    print("BFARM_DEVICES " + json.dumps({
        "backend": prefs.compute_device_type,
        "gpus": [d.name for d in gpus],
        "cpus": [d.name for d in cpus],
        "blender": bpy.app.version_string,
    }), flush=True)
    sys.exit(0)

scene = bpy.context.scene
s = cfg["settings"]
out = cfg["outdir"]
os.makedirs(out, exist_ok=True)

# ---- motor ----
if s["engine"] == "cycles":
    scene.render.engine = "CYCLES"
    c = scene.cycles
    prefs, gpus, cpus = setup_devices(scene, s["device"])
    wanted = s["device"]
    use_gpu = wanted in ("auto", "gpu", "gpu+cpu") and bool(gpus)
    if wanted in ("gpu", "gpu+cpu") and not gpus:
        log("AVISO: no se encontro GPU, se usa CPU")
    for d in prefs.devices:
        d.use = (d in gpus and use_gpu) or (d.type == "CPU" and (not use_gpu or wanted == "gpu+cpu"))
    c.device = "GPU" if use_gpu else "CPU"
    log("Dispositivo: %s via %s %s" % (c.device, prefs.compute_device_type, [d.name for d in prefs.devices if d.use]))
    if s["samples"] > 0:
        c.samples = s["samples"]
        c.use_adaptive_sampling = True
    c.use_denoising = bool(s["denoise"])
    if s["denoise"] and use_gpu and prefs.compute_device_type == "OPTIX":
        try:
            c.denoiser = "OPTIX"
        except Exception:
            pass
else:
    scene.render.engine = "BLENDER_EEVEE"
    if s["samples"] > 0:
        scene.eevee.taa_render_samples = s["samples"]
    log("Eevee necesita GPU con OpenGL/EGL; si falla usa Cycles")

# ---- resolucion / hilos ----
r = scene.render
if s["res_x"] > 0 and s["res_y"] > 0:
    r.resolution_x, r.resolution_y = s["res_x"], s["res_y"]
r.resolution_percentage = s["scale"]
r.threads_mode = "FIXED" if s["threads"] > 0 else "AUTO"
if s["threads"] > 0:
    r.threads = s["threads"]

# ---- formato ----
fmt = s["format"]
im = r.image_settings
if fmt == "MP4":
    im.file_format = "FFMPEG"
    r.ffmpeg.format = "MPEG4"
    r.ffmpeg.codec = "H264"
    r.ffmpeg.constant_rate_factor = "HIGH"
    im.color_mode = "RGB"
else:
    im.file_format = {"PNG": "PNG", "JPEG": "JPEG", "EXR": "OPEN_EXR"}[fmt]
    if fmt == "JPEG":
        im.quality = s["quality"]
        im.color_mode = "RGB"

# ---- rango ----
if s["mode"] == "animation":
    if s["start"] > 0:
        scene.frame_start = s["start"]
    if s["end"] > 0:
        scene.frame_end = s["end"]
    scene.frame_step = max(1, s["step"])
    r.use_overwrite = False  # permite reanudar si se corta
    r.filepath = os.path.join(out, "frame_")
    log("Animacion %d-%d" % (scene.frame_start, scene.frame_end))
    bpy.ops.render.render(animation=True)
else:
    if s["frame"] > 0:
        scene.frame_set(s["frame"])
    r.filepath = os.path.join(out, "render_%04d" % scene.frame_current)
    log("Imagen fija, frame %d" % scene.frame_current)
    bpy.ops.render.render(write_still=True)
log("TERMINADO")
