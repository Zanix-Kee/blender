"""Pruebas locales del servidor con Blender y nvidia-smi simulados.  python tests/test_server.py"""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D = tempfile.mkdtemp()
PORT = 8094
PW = "testtesttest1"
env = dict(os.environ, BFARM_DATA=D, BLENDER_BIN=str(ROOT / "tests" / "fake_blender.cmd"), PORT=str(PORT),
           BFARM_PASSWORD=PW, BFARM_STDBUF="0", NVIDIA_SMI=str(ROOT / "tests" / "fake_smi.cmd"))
srv = subprocess.Popen([sys.executable, str(ROOT / "server" / "app.py")], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
U = "http://127.0.0.1:%d" % PORT
ok = True


def call(path, data=None, method=None):
    r = urllib.request.Request(U + path, data=data, method=method or ("POST" if data is not None else "GET"),
                               headers={"Authorization": "Bearer " + PW})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        return {"http": e.code, **json.loads(e.read())}


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name, extra if not cond else "")


def wait(jid, want=("done", "failed", "canceled"), t=30):
    end = time.time() + t
    while time.time() < end:
        j = call("/api/jobs/" + jid)
        if j["status"] in want:
            return j
        time.sleep(0.3)
    return call("/api/jobs/" + jid)


try:
    time.sleep(2)
    # --- presets
    info = call("/api/info")
    check("12 presets expuestos", len(info["presets"]) == 12, list(info["presets"]))
    check("default noise existe", "noise" in info["defaults"])

    # --- subida simple + job A
    up = call("/api/upload?name=escena.blend", b"BLENDER" + os.urandom(2000))
    A = call("/api/jobs", json.dumps({"upload": up["upload"], "name": "A", "settings": {"preset": "cine", "noise": 7}}).encode())
    check("preset cine aplicado (EXR/1024)", A["settings"]["samples"] == 1024 and A["settings"]["format"] == "EXR")
    check("noise acotado a 1.0", A["settings"]["noise"] == 1.0, A["settings"]["noise"])

    # --- repetir sin re-subir: B espera en cola mientras A corre
    B = call("/api/jobs/%s/rerun" % A["id"], json.dumps({"settings": {"preset": "draft"}}).encode())
    check("rerun crea job en cola", B.get("status") in ("queued", "running") and B["settings"]["preset"] == "draft", B)
    time.sleep(0.3)
    jb = call("/api/jobs/" + B["id"])
    check("B muestra posicion y a quien espera", jb["status"] == "queued" and jb.get("pos") == 1 and jb.get("wait") == "A", jb)

    ja = wait(A["id"])
    check("A terminado", ja["status"] == "done", ja["status"] + " " + ja["error"])
    check("aviso de driver viejo detectado", "driver" in (ja.get("warn") or "").lower(), ja.get("warn"))
    jb = wait(B["id"])
    check("B (2o trabajo) se ejecuta despues de A", jb["status"] == "done" and jb["started"] >= ja["finished"] - 0.5, jb["status"])

    # --- borrar A no rompe B; se puede repetir B
    call("/api/jobs/" + A["id"], method="DELETE")
    check("input de B sigue tras borrar A", (Path(D) / "jobs" / B["id"] / "input" / "escena.blend").exists())
    C = call("/api/jobs/%s/rerun" % B["id"], json.dumps({"settings": {"preset": "sketch"}}).encode())
    jc = wait(C["id"])
    check("repetir un repetido funciona", jc["status"] == "done", jc)
    miss = call("/api/jobs/%s/rerun" % A["id"], json.dumps({"settings": {}}).encode())
    check("rerun de job borrado da error claro", miss.get("http") == 400 or miss.get("http") == 404, miss)

    # --- progreso real, fases, ETA y deteccion de inicio del render (imagen fija con vista previa)
    def watch(jid, t=40):
        seen, end = [], time.time() + t
        while time.time() < end:
            j = call("/api/jobs/" + jid)
            seen.append(j)
            if j["status"] in ("done", "failed", "canceled"):
                break
            time.sleep(0.1)
        return seen

    seen = watch(call("/api/jobs/%s/rerun" % B["id"], json.dumps({"settings": {"preset": "normal"}}).encode())["id"])
    stages = []
    for j in seen:
        if not stages or stages[-1] != j["stage"]:
            stages.append(j["stage"])
    check("fases en orden (previa antes del render real)", [x for x in stages if x in ("Vista previa rapida", "Renderizando")][:2] == ["Vista previa rapida", "Renderizando"], stages)
    check("detecta kernels/escena antes de renderizar", any(j["status"] == "running" and j["stage"] in ("Preparando", "Preparando la escena") for j in seen), stages)
    pre = [j["progress"] for j in seen if j["stage"] in ("Preparando", "Preparando la escena", "Vista previa rapida")]
    check("previa/preparacion NO adelantan el progreso (<=8%)", all(x <= 0.081 for x in pre), max(pre or [0]))
    mid = [j for j in seen if j["stage"] == "Renderizando"]
    check("progreso sube durante el render", len({round(j["progress"], 2) for j in mid}) >= 4 and mid[-1]["progress"] > 0.5, [round(j["progress"], 2) for j in mid])
    check("tiene tiempo restante (ETA)", any(j.get("eta") is not None for j in mid))
    check("sabe cuando empezo el render real", all(j.get("render_s") is not None for j in mid[1:]), mid[:2])
    check("detecta dispositivo", "OPTIX" in (mid[-1].get("device") or ""), mid[-1].get("device"))
    check("termina en 100%", seen[-1]["status"] == "done" and seen[-1]["progress"] == 1.0, seen[-1]["progress"])
    check("hay tiempo total transcurrido", seen[-1].get("elapsed", 0) >= 1)

    # --- animacion: 3 frames
    E = call("/api/jobs/%s/rerun" % B["id"], json.dumps({"settings": {"preset": "anim_fast"}}).encode())
    seen = watch(E["id"])
    prog = [j["progress"] for j in seen if j["status"] == "running"]
    check("animacion: total de frames", seen[-1]["total"] == 3, seen[-1]["total"])
    check("animacion: progreso crece por frame, sin saltos", prog == sorted(prog) and max(prog) <= 0.99 and len(set(round(x, 1) for x in prog)) >= 4, sorted(set(round(x, 2) for x in prog)))
    check("animacion termina", seen[-1]["status"] == "done" and len(seen[-1]["files"]) == 3, seen[-1]["files"])

    # --- stats
    s = call("/api/stats")
    for k in ("gpus", "cpu", "ram", "disk", "idle_since", "idle_warn_min", "driver", "cuda"):
        check("stats trae " + k, k in s)
    check("2 GPU detectadas", len(s["gpus"]) == 2 and s["gpus"][0]["util"] == 87.0 and s["gpus"][0]["vram_total"] == 12288.0, s["gpus"])
    check("driver y CUDA", s["driver"] == "560.35" and s["cuda"] == "12.6", (s["driver"], s["cuda"]))
    check("disco con datos", s["disk"].get("free", 0) > 0)

    # --- auth
    r = urllib.request.Request(U + "/api/stats")
    try:
        urllib.request.urlopen(r)
        check("stats exige contrasena", False)
    except urllib.error.HTTPError as e:
        check("stats exige contrasena", e.code == 401)
finally:
    srv.terminate()
    srv.wait()
    sys.path.insert(0, str(ROOT / "server"))
    os.environ.update(BFARM_DATA=D)
    try:
        import app
        check("guarda de disco rechaza tamano enorme", app.need_disk(10 ** 18) is False)
        check("guarda de disco acepta tamano chico", app.need_disk(1024) is True)
    except Exception as e:
        check("importar app para probar guarda de disco", False, e)
    shutil.rmtree(D, ignore_errors=True)
print("\nRESULTADO:", "TODO OK" if ok else "HAY FALLOS")
sys.exit(0 if ok else 1)
