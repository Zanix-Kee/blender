"""Blender simulado: imita el formato real de salida (Fra:..., Sample x/y, Remaining, Saved:) y los marcadores [bfarm]."""
import json, os, sys, time

cfg = json.load(open(sys.argv[sys.argv.index("--") + 1]))
if cfg.get("action") == "devices":
    print('BFARM_DEVICES {"backend":"OPTIX","gpus":["Fake RTX 4090","Fake RTX 4090"],"cpus":["Fake CPU"],"blender":"5.3.0"}')
    sys.exit(0)
s, out = cfg["settings"], cfg["outdir"]
os.makedirs(out, exist_ok=True)
STEP = float(os.environ.get("FAKE_STEP", "0.15"))
M = "Mem:12.5M (Peak 12.5M) | Time:%s | %sMem:0.00M, Peak:0.00M | Scene, ViewLayer | %s"


def p(x):
    print(x, flush=True)


def tm(t):
    return "%02d:%05.2f" % (int(t // 60), t % 60)


p('Read blend: "input.blend"')
p("00:04.222  cycles           | WARNING OptiX initialization failed because the installed NVIDIA driver is too old.")
p("[bfarm] Dispositivo: GPU via OPTIX ['NVIDIA GeForce RTX 3060']")
p("[bfarm] FASE preparando")
p("Fra:1 " + M % (tm(0.5), "", "Loading render kernels (may take a few minutes the first time)"))
time.sleep(STEP * 3)
p("Fra:1 " + M % (tm(1.2), "", "Synchronizing object | Cube"))
time.sleep(STEP)


def render(frame, samples, remaining=True):
    t0 = time.time()
    for k in range(1, samples + 1):
        el = time.time() - t0
        rem = ("Remaining:%s | " % tm(el * (samples - k) / k)) if remaining else ""
        p("Fra:%d " % frame + M % (tm(el), rem, "Sample %d/%d" % (k, samples)))
        time.sleep(STEP)


if s["mode"] == "still" and s.get("preview", True):
    p("[bfarm] FASE preview")
    render(1, 4, remaining=False)
    pv = os.path.join(os.path.dirname(out), "preview.png")
    open(pv, "wb").write(b"\x89PNG fake")
    p("Saved: '%s'" % pv)
    p("[bfarm] Vista previa lista")
p("[bfarm] FASE final")
if s["mode"] == "still":
    p("[bfarm] Imagen fija, frame 1")
    render(1, 8)
    path = os.path.join(out, "render_0001.png")
    open(path, "wb").write(b"\x89PNG fake")
    p("Saved: '%s'" % path)
else:
    p("[bfarm] Animacion 1-3")
    for f in (1, 2, 3):
        render(f, 4)
        path = os.path.join(out, "frame_%04d.png" % f)
        open(path, "wb").write(b"\x89PNG fake")
        p("Saved: '%s'" % path)
p("[bfarm] TERMINADO")
