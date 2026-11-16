import sys, json, time, os
cfg = json.load(open(sys.argv[sys.argv.index("--") + 1]))
if cfg.get("action") == "devices":
    print('BFARM_DEVICES {"backend":"OPTIX","gpus":["Fake RTX 4090","Fake RTX 4090"],"cpus":["Fake CPU"],"blender":"5.3.0"}'); sys.exit(0)
s, out = cfg["settings"], cfg["outdir"]; os.makedirs(out, exist_ok=True)
frames = [1] if s["mode"] == "still" else list(range(1, 4))
if s["mode"] != "still": print("[bfarm] Animacion 1-3", flush=True)
for f in frames:
    for k in range(1, 5):
        print("Fra:%d Mem:1M | Time:00:00.1 | Sample %d/4" % (f, k), flush=True); time.sleep(0.2)
    p = os.path.join(out, "render_%04d.png" % f)
    open(p, "wb").write(b"\x89PNG fake"); print("Saved: '%s'" % p, flush=True)
