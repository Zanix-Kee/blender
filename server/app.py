#!/usr/bin/env python3
"""BlenderFarm Lite: servidor web minimo (solo libreria estandar) para renderizar .blend en la nube."""
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import zlib
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BFARM_DATA", "/data"))
BLENDER = os.environ.get("BLENDER_BIN", "blender")
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_UPLOAD = int(float(os.environ.get("MAX_UPLOAD_GB", "20")) * 1024**3)
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "0"))  # 0 = no borrar solo
WEB = Path(os.environ.get("BFARM_WEB", HERE.parent / "web"))

UPLOADS, JOBS, INBOX = DATA / "uploads", DATA / "jobs", DATA / "inbox"
for d in (UPLOADS, JOBS, INBOX):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- ajustes
DEFAULTS = dict(
    preset="normal", engine="cycles", device="auto", mode="still",
    frame=0, start=0, end=0, step=1,            # 0 = usar lo que trae el archivo
    samples=128, denoise=True, scale=100, res_x=0, res_y=0,
    format="PNG", quality=90, threads=0, scripts=False, preview=True,
    noise=0.0, peer=True,                                  # umbral de ruido adaptativo; 0 = el del archivo
)
ENUMS = dict(engine=("cycles", "eevee"), device=("auto", "cpu", "gpu", "gpu+cpu"),
             mode=("still", "animation"), format=("PNG", "JPEG", "EXR", "MP4"))
RANGES = dict(frame=(0, 10**6), start=(0, 10**6), end=(0, 10**6), step=(1, 1000),
              samples=(0, 65536), scale=(1, 400), res_x=(0, 16384), res_y=(0, 16384),
              quality=(1, 100), threads=(0, 512), noise=(0.0, 1.0))
PRESETS = {
    # imagen fija: de menos a mas calidad
    "sketch":  dict(samples=8,    scale=25,  noise=0.1,   mode="still"),
    "draft":   dict(samples=32,   scale=50,  noise=0.05,  mode="still"),
    "normal":  dict(samples=128,  scale=100, noise=0.0,   mode="still"),
    "high":    dict(samples=256,  scale=100, noise=0.02,  mode="still"),
    "final":   dict(samples=512,  scale=100, noise=0.01,  mode="still"),
    "cine":    dict(samples=1024, scale=100, noise=0.005, mode="still", format="EXR"),
    "ultra":   dict(samples=2048, scale=100, noise=0.002, mode="still", format="EXR"),
    # animacion
    "anim_sketch":  dict(samples=16,  scale=25,  noise=0.1,  mode="animation", format="MP4"),
    "anim_preview": dict(samples=24,  scale=50,  noise=0.05, mode="animation", format="MP4"),
    "anim_fast":    dict(samples=64,  scale=100, noise=0.03, mode="animation", format="PNG"),
    "anim_final":   dict(samples=128, scale=100, noise=0.0,  mode="animation", format="PNG"),
    "anim_cine":    dict(samples=256, scale=100, noise=0.01, mode="animation", format="EXR"),
}


def clean(raw):
    d = dict(DEFAULTS)
    raw = raw if isinstance(raw, dict) else {}
    d.update(PRESETS.get(raw.get("preset"), {}))
    for k, v in raw.items():
        if k not in DEFAULTS or k == "preset":
            continue
        t = type(DEFAULTS[k])
        try:
            if t is bool:
                v = bool(v)
            elif t is int:
                lo, hi = RANGES[k]
                v = max(lo, min(hi, int(v)))
            elif t is float:
                lo, hi = RANGES[k]
                v = max(lo, min(hi, float(v)))
            elif k in ENUMS and v not in ENUMS[k]:
                continue
        except (TypeError, ValueError):
            continue
        d[k] = v
    d["preset"] = raw.get("preset") if raw.get("preset") in PRESETS else "custom"
    if d["mode"] == "still" and d["format"] == "MP4":
        d["format"] = "PNG"
    return d


# ---------------------------------------------------------------- token
GENERATED = [True]  # True si la contrasena no vino de BFARM_PASSWORD


def load_token():
    f = DATA / "token"
    t = (os.environ.get("BFARM_PASSWORD") or os.environ.get("BFARM_TOKEN") or "").strip()
    if t:
        GENERATED[0] = False
    elif f.exists():
        return f.read_text().strip()
    t = t or secrets.token_urlsafe(24)  # 32 caracteres aleatorios seguros
    f.write_text(t)  # la CLI `bfarm` lo lee de aqui
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return t


TOKEN = load_token()


# ---------------------------------------------------------------- hardware
HW = {"cpu_threads": os.cpu_count() or 1, "gpus": [], "backend": "", "blender": "", "ready": False}


def detect_hw():
    cfg = DATA / "devices.json"
    cfg.write_text(json.dumps({"action": "devices"}))
    try:
        p = subprocess.run([BLENDER, "-b", "--factory-startup", "-P", str(HERE / "render_job.py"), "--", str(cfg)],
                           capture_output=True, text=True, timeout=120)
        for line in p.stdout.splitlines():
            if line.startswith("BFARM_DEVICES "):
                j = json.loads(line[14:])
                HW.update(gpus=j["gpus"], backend=j["backend"], blender=j["blender"])
    except Exception as e:  # blender ausente / lento: la UI sigue funcionando
        print("detect_hw:", e, file=sys.stderr)
    HW["ready"] = True


# ---------------------------------------------------------------- jobs
LOCK = threading.RLock()
STATE = {}          # id -> dict
QUEUE = []          # ids en orden
PROC = {}           # id -> Popen
WAKE = threading.Event()
PARTS = {}          # subidas en trozos en curso

RE_FRAME = re.compile(r"Fra:(\d+)")
RE_DRIVER = re.compile(r"driver.{0,40}too old|too old.{0,40}driver", re.I)
RE_OOM = re.compile(r"out of (device |gpu )?memory|CUDA_ERROR_OUT_OF_MEMORY|cudaErrorMemoryAllocation", re.I)
WARN_DRIVER = ("El driver NVIDIA de esta maquina es antiguo para esta version de Blender. "
               "Usa la imagen :lts o alquila una maquina con driver mas nuevo (CUDA 12.8+).")
WARN_OOM = "La GPU se quedo sin memoria (VRAM). Baja el tamano %, las muestras, o usa 'Solo CPU'."
RE_RANGE = re.compile(r"\[bfarm\] Animacion (\d+)-(\d+)")


def save(j):
    (JOBS / j["id"] / "job.json").write_text(json.dumps(j))


def outputs(jid):
    out = JOBS / jid / "out"
    if not out.exists():
        return []
    return sorted(f.name for f in out.iterdir() if f.is_file())


def _link(a, b):
    try:
        os.link(a, b)          # mismo disco: no duplica GB
    except OSError:
        shutil.copy2(a, b)


def new_job(name, blend, settings, src=None, link=False):
    """src: carpeta con el .blend. link=True la copia con hardlinks (re-render sin re-subir)."""
    jid = secrets.token_hex(6)
    (JOBS / jid).mkdir()
    if src is not None:
        if link:
            shutil.copytree(src, JOBS / jid / "input", copy_function=_link)
        else:
            shutil.move(str(src), str(JOBS / jid / "input"))
    j = dict(id=jid, name=name, blend=blend, settings=clean(settings), status="queued",
             created=time.time(), started=None, finished=None, progress=0.0,
             frame=None, total=None, sample=None, samples=None, error="", warn="")
    with LOCK:
        STATE[jid] = j
        QUEUE.append(jid)
        save(j)
    WAKE.set()
    return j


def rerun(src_id, settings):
    src = STATE.get(src_id)
    if not src or not (JOBS / src_id / "input").is_dir():
        raise ValueError("el archivo original ya no existe en el servidor")
    return new_job(src["name"], src["blend"], settings, src=JOBS / src_id / "input", link=True)


HMS = r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)"
RE_TIME = re.compile(r"Time:" + HMS)
RE_REMAIN = re.compile(r"Remaining:" + HMS)
RE_SAMPLE = re.compile(r"[Ss]ample\s+(\d+)\s*/\s*(\d+)|Rendering\s+(\d+)\s*/\s*(\d+)\s+samples")
RE_DEVICE = re.compile(r"\[bfarm\] Dispositivo: (\w+) via (\w+) (\[.*\])")
PREV_W = 0.08       # peso de la vista previa rapida dentro del progreso de una imagen fija
DETAIL = (("kernel", "Cargando kernels de la GPU (la 1a vez puede tardar varios minutos)"),
          ("bvh", "Construyendo la geometria (BVH)"), ("synchroniz", "Sincronizando objetos"),
          ("denois", "Quitando ruido"), ("loading image", "Cargando texturas"), ("updating", "Actualizando la escena"),
          ("loading", "Cargando datos"), ("compil", "Compilando"))


def hms(m):
    return int(m[0] or 0) * 3600 + int(m[1]) * 60 + float(m[2])


class Tracker:
    """Convierte la salida de Blender en fase, progreso, tiempo restante y dispositivo."""

    def __init__(self, j):
        self.j, self.s = j, j["settings"]
        self.preview = self.s["mode"] == "still" and self.s.get("preview", True) and self.s["engine"] == "cycles"
        self.phase, self.done, self.t_render = "final", 0, None
        j.update(stage="Iniciando Blender", detail="", device="", phase="", render_t0=None, progress=0.0,
                 eta=None, sample=None, samples=None, frame=None, total=None, rendering=False)

    def feed(self, line, now):
        j, s, t = self.j, self.s, line.strip()
        if not t:
            return
        if t.startswith("[bfarm] FASE "):
            self.phase = t[13:].strip()
            j.update(sample=None, samples=None, rendering=False, phase=self.phase)
            j["stage"] = {"preparando": "Preparando", "preview": "Vista previa rapida",
                          "final": "Preparando el render final"}.get(self.phase, self.phase)
        elif t.startswith("[bfarm] AVISO") and not j.get("warn"):
            j["warn"] = t[8:].strip()
        elif t.startswith("[bfarm] Memoria"):
            j["pool"] = t[8:].strip()
        elif "Read blend" in t:
            j["stage"] = "Cargando el archivo .blend"
        elif t.startswith("[bfarm] TERMINADO"):
            j["stage"] = "Terminado"
        m = RE_DEVICE.search(t)
        if m:
            j["device"] = "%s · %s · %s" % (m[1], m[2], ", ".join(re.findall(r"'([^']+)'", m[3])) or "sin nombre")
            j["stage"] = "Configurando el dispositivo"
        m = RE_RANGE.search(t)
        if m:
            j["total"] = max(1, (int(m[2]) - int(m[1])) // max(1, s["step"]) + 1)
        if t.startswith("Saved:") and "preview.png" not in t:
            self.done += 1
            j["stage"] = "Guardando"
        if t.startswith("Fra:") or "| Scene" in t:
            m = RE_FRAME.search(t)
            if m:
                j["frame"] = int(m[1])
            segs = [x.strip() for x in t.split("|")]
            m = RE_SAMPLE.search(t)
            if m:
                cur, tot = int(m[1] or m[3]), int(m[2] or m[4])
                j.update(sample=cur, samples=tot, rendering=True)
                j["stage"] = "Vista previa rapida" if self.phase == "preview" else "Renderizando"
                j["detail"] = "muestra %d de %d" % (cur, tot)
                if self.phase != "preview" and self.t_render is None:
                    self.t_render = j["render_t0"] = now      # aqui empieza el render de verdad
            elif len(segs) > 3:
                j["detail"] = segs[-1][:90]
                low = segs[-1].lower()
                if "preview" not in self.phase:
                    j["stage"] = "Preparando la escena"
                for k, txt in DETAIL:
                    if k in low:
                        j["detail"] = txt
                        break
        # ---- progreso
        total = j.get("total") or 1
        frac = (j["sample"] or 0) / (j["samples"] or 1) if j.get("samples") else 0
        if self.phase == "preview":
            j["progress"] = PREV_W * frac
        else:
            base = PREV_W if self.preview else 0.0
            u = min(1.0, (self.done + (frac if self.done < total else 0)) / total)
            j["progress"] = min(0.99, base + (1 - base) * u)
            if self.t_render is not None and u > 0.005:
                m = RE_REMAIN.search(t)
                if m and s["mode"] == "still":              # estimacion propia de Blender (mas exacta)
                    j["eta"] = int(hms(m.groups()))
                else:
                    j["eta"] = int((now - self.t_render) * (1 - u) / u)


def run_job(j):
    jid = j["id"]
    jd = JOBS / jid
    s = j["settings"]
    cfg = jd / "config.json"
    cfg.write_text(json.dumps(dict(settings=s, outdir=str(jd / "out"))))
    cmd = [BLENDER, "-b", "--factory-startup"]
    if not s["scripts"]:
        cmd.append("--disable-autoexec")   # no ejecutar Python embebido en el .blend
    cmd += [str(jd / "input" / j["blend"]), "-P", str(HERE / "render_job.py"), "--", str(cfg)]
    with LOCK:
        j.update(status="running", started=j["started"] or time.time(), error="", last_at=time.time())
        save(j)
    trk = Tracker(j)
    with open(jd / "log.txt", "a", errors="replace") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        try:
            if os.environ.get("BFARM_STDBUF", "1") != "0" and shutil.which("stdbuf"):   # Blender no vacia stdout en tuberias: sin esto el progreso no se ve
                cmd = ["stdbuf", "-o0", "-e0"] + cmd
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 errors="replace", cwd=str(jd), start_new_session=True)
        except OSError as e:
            return finish(j, "failed", "No se pudo iniciar Blender: %s" % e)
        PROC[jid] = p
        for line in p.stdout:
            log.write(line)
            log.flush()
            if line.strip():
                j["last"] = line.strip()[:200]
                j["last_at"] = time.time()
                if RE_DRIVER.search(line):
                    j["warn"] = WARN_DRIVER
                elif RE_OOM.search(line):
                    j["warn"] = WARN_OOM
            trk.feed(line, time.time())
        rc = p.wait()
    PROC.pop(jid, None)
    if j["status"] == "canceled":
        return finish(j, "canceled", "")
    if rc == 0 and outputs(jid):
        finish(j, "done", "")
    else:
        finish(j, "failed", "Blender termino con codigo %s. Mira el registro." % rc)


def finish(j, status, err):
    with LOCK:
        j.update(status=status, error=err, finished=time.time(), eta=0)
        if status == "done":
            j["progress"] = 1.0
        save(j)


def worker():
    while True:
        WAKE.wait(2)
        WAKE.clear()
        while True:
            with LOCK:
                jid = next((i for i in QUEUE if STATE[i]["status"] == "queued"), None)
                if jid:
                    QUEUE.remove(jid)
            if not jid:
                break
            try:
                run_job(STATE[jid])
            except Exception as e:
                finish(STATE[jid], "failed", repr(e))


def load_jobs():
    for f in sorted(JOBS.glob("*/job.json"), key=lambda p: p.stat().st_mtime):
        try:
            j = json.loads(f.read_text())
        except ValueError:
            continue
        STATE[j["id"]] = j
        if j["status"] in ("queued", "running"):   # reanuda tras reinicio
            j["status"] = "queued"
            QUEUE.append(j["id"])


def cleaner():
    while KEEP_DAYS > 0:
        time.sleep(3600)
        with LOCK:
            old = [i for i, j in STATE.items() if j["status"] in ("done", "failed", "canceled")
                   and time.time() - (j["finished"] or time.time()) > KEEP_DAYS * 86400]
            for i in old:
                delete_job(i)


def delete_job(jid):
    j = STATE.get(jid)
    if not j:
        return False
    if j["status"] == "running":
        cancel_job(jid)
    STATE.pop(jid, None)
    if jid in QUEUE:
        QUEUE.remove(jid)
    shutil.rmtree(JOBS / jid, ignore_errors=True)
    return True


def cancel_job(jid):
    j = STATE.get(jid)
    if not j or j["status"] not in ("queued", "running"):
        return False
    j["status"] = "canceled"
    p = PROC.get(jid)
    if p:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            p.terminate()
    else:
        finish(j, "canceled", "")
    save(j)
    return True


# ---------------------------------------------------------------- subidas
def safe_name(n):
    n = re.sub(r"[^\w.\- ]", "_", os.path.basename(n or "")).strip(". ") or "file"
    return n[:120]


def safe_unzip(zpath, dest):
    total = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep) and target != dest.resolve():
                raise ValueError("zip con rutas peligrosas")
            total += info.file_size
            if total > MAX_UPLOAD * 4:
                raise ValueError("zip demasiado grande al descomprimir")
        z.extractall(dest)
    zpath.unlink()


def find_blend(d):
    c = sorted(d.rglob("*.blend"), key=lambda p: (len(p.relative_to(d).parts), str(p)))
    return str(c[0].relative_to(d)) if c else None


def adopt(src_dir, name, settings):
    """Mueve una carpeta de subida (con input ya listo) a un job."""
    blend = find_blend(src_dir)
    if not blend:
        raise ValueError("No hay ningun .blend (sube un .blend o un .zip que lo contenga)")
    return new_job(name or blend, blend, settings, src=src_dir)


def inbox_watcher():
    """scp/rsync un .blend (o .zip) a /data/inbox y se renderiza solo. name.json opcional = ajustes."""
    seen = {}
    while True:
        time.sleep(3)
        for f in list(INBOX.iterdir()):
            if not f.is_file() or f.suffix.lower() not in (".blend", ".zip"):
                continue
            size = f.stat().st_size
            if seen.get(f) != size:      # aun copiandose
                seen[f] = size
                continue
            seen.pop(f, None)
            try:
                stage = UPLOADS / secrets.token_hex(8)
                stage.mkdir()
                if f.suffix.lower() == ".zip":
                    shutil.move(str(f), str(stage / "in.zip"))
                    safe_unzip(stage / "in.zip", stage)
                else:
                    shutil.move(str(f), str(stage / safe_name(f.name)))
                side = f.with_suffix(".json")
                settings = {}
                if side.exists():
                    settings = json.loads(side.read_text())
                    side.unlink()
                adopt(stage, f.stem, settings)
            except Exception as e:
                print("inbox:", f.name, e, file=sys.stderr)
                f.rename(f.with_suffix(f.suffix + ".error"))


# ---------------------------------------------------------------- recursos (GPU / CPU / RAM / disco)
SMI = os.environ.get("NVIDIA_SMI", "nvidia-smi")
START = time.time()
_ST = {"t": 0.0, "v": {}, "cpu": None, "cuda": None}
_STL = threading.Lock()


def _txt(path):
    try:
        return Path(path).read_text()
    except OSError:
        return None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _smi(*args, timeout=6):
    try:
        return subprocess.run([SMI, *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gpu_stats():
    base = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,driver_version"
    out = _smi("--query-gpu=" + base + ",pcie.link.gen.current,pcie.link.width.current", "--format=csv,noheader,nounits")
    if not out.strip() or "not a valid" in out.lower():          # drivers viejos: sin campos PCIe
        out = _smi("--query-gpu=" + base, "--format=csv,noheader,nounits")
    gpus, driver = [], ""
    for ln in out.strip().splitlines():
        c = [x.strip() for x in ln.split(",")]
        if len(c) < 9 or not c[0].isdigit():
            continue
        driver = c[8]
        gpus.append(dict(i=int(c[0]), name=c[1], util=_num(c[2]), vram_used=_num(c[3]), vram_total=_num(c[4]),
                         temp=_num(c[5]), power=_num(c[6]), power_max=_num(c[7]),
                         pcie_gen=_num(c[9]) if len(c) > 9 else None, pcie_w=_num(c[10]) if len(c) > 10 else None))
    if gpus and _ST["cuda"] is None:
        m = re.search(r"CUDA Version:\s*([\d.]+)", _smi())
        _ST["cuda"] = m[1] if m else ""
    if not gpus:                      # AMD (mejor esfuerzo)
        try:
            j = json.loads(subprocess.run(["rocm-smi", "--showuse", "--showmeminfo", "vram", "--showproductname",
                                           "--json"], capture_output=True, text=True, timeout=5).stdout)
            for k, v in j.items():
                if k.startswith("card"):
                    gpus.append(dict(i=int(k[4:]), name=v.get("Card Series") or v.get("Card series") or k,
                                     util=_num(v.get("GPU use (%)")),
                                     vram_used=(_num(v.get("VRAM Total Used Memory (B)")) or 0) / 1048576,
                                     vram_total=(_num(v.get("VRAM Total Memory (B)")) or 0) / 1048576,
                                     temp=None, power=None, power_max=None, pcie_gen=None, pcie_w=None))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return gpus, driver


# ---- topologia entre GPU (NVLink / PCIe / XGMI): cambia poco, se refresca en segundo plano
TOPO = {"v": {"kind": "unknown", "n": 0, "groups": [], "links": [], "speed": None}}


def parse_topo(topo_txt, link_txt, n):
    """topo_txt: salida de `nvidia-smi topo -m`; link_txt: de `nvidia-smi nvlink --status`."""
    mat = [[None] * n for _ in range(n)]
    for ln in topo_txt.splitlines():
        m = re.match(r"^GPU(\d+)\s+(.*)$", ln.strip())
        if m and int(m[1]) < n:
            for j, tok in enumerate(m[2].split()[:n]):
                mat[int(m[1])][j] = tok
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    nv = 0
    for i in range(n):
        for j in range(n):
            t = mat[i][j] or ""
            if i != j and re.fullmatch(r"NV\d+", t):
                nv += 1
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    groups = [g for g in groups.values()]
    links, speed, cur = [0] * n, None, -1
    for ln in link_txt.splitlines():
        m = re.match(r"^GPU\s+(\d+):", ln.strip())
        if m:
            cur = int(m[1])
            continue
        m = re.search(r"Link\s+\d+:\s*([\d.]+)\s*GB/s", ln)
        if m and 0 <= cur < n:
            links[cur] += 1
            speed = speed or float(m[1])
    if n < 2:
        kind = "single"
    elif nv:
        kind = "nvlink"
    elif any(any(r) for r in mat):
        kind = "pcie"
    else:
        kind = "unknown"
    return dict(kind=kind, n=n, groups=groups, links=links, speed=speed)


def topo_loop():
    while True:
        try:
            n = len(gpu_stats()[0]) if _smi("-L").strip() else 0
            if n:
                t = parse_topo(_smi("topo", "-m"), _smi("nvlink", "--status", timeout=15), n)
                if t["kind"] == "unknown" and n > 1:
                    t["kind"] = "pcie"
                TOPO["v"] = t
            else:
                try:
                    x = subprocess.run(["rocm-smi", "--showtopotype"], capture_output=True, text=True, timeout=8).stdout
                except (OSError, subprocess.SubprocessError):
                    x = ""
                if "XGMI" in x.upper():
                    TOPO["v"] = dict(kind="xgmi", n=0, groups=[], links=[], speed=None)
        except Exception as e:
            print("topo:", e, file=sys.stderr)
        time.sleep(60)


def cpu_stats():
    pct = None
    t = _txt("/proc/stat")
    if t:
        f = [int(x) for x in t.splitlines()[0].split()[1:9]]
        idle, total = f[3] + f[4], sum(f)
        prev = _ST["cpu"]
        _ST["cpu"] = (idle, total)
        if prev and total > prev[1]:
            pct = round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)
    cores = None
    q = (_txt("/sys/fs/cgroup/cpu.max") or "").split()      # limite real del contenedor
    if len(q) == 2 and q[0] != "max" and _num(q[1]):
        cores = round(int(q[0]) / int(q[1]), 1)
    if cores is None:
        try:
            cores = len(os.sched_getaffinity(0))
        except AttributeError:
            cores = os.cpu_count()
    model = ""
    for ln in (_txt("/proc/cpuinfo") or "").splitlines():
        if ln.startswith("model name"):
            model = ln.split(":", 1)[1].strip()
            break
    return dict(pct=pct, cores=cores, model=model, load=os.getloadavg()[0] if hasattr(os, "getloadavg") else None)


def ram_stats():
    total = used = None
    mi = {}
    for ln in (_txt("/proc/meminfo") or "").splitlines():
        k, _, v = ln.partition(":")
        mi[k] = int(v.split()[0]) * 1024 if v.split() else 0
    if mi.get("MemTotal"):
        total, used = mi["MemTotal"], mi["MemTotal"] - mi.get("MemAvailable", 0)
    for lim, cur, stat, key in (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current",
                                 "/sys/fs/cgroup/memory.stat", "inactive_file"),
                                ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes",
                                 "/sys/fs/cgroup/memory/memory.stat", "total_inactive_file")):
        L, C = _num((_txt(lim) or "").strip()), _num((_txt(cur) or "").strip())
        if L and C and (total is None or L < total):    # limite del contenedor (si es menor que el host)
            cache = 0
            for ln in (_txt(stat) or "").splitlines():
                if ln.startswith(key + " "):
                    cache = int(ln.split()[1])
            total, used = int(L), int(C - cache)
            break
    rss = None
    for jid, p in list(PROC.items()):
        m = re.search(r"VmRSS:\s+(\d+) kB", _txt("/proc/%d/status" % p.pid) or "")
        if m:
            rss = (rss or 0) + int(m[1]) * 1024
    return dict(total=total, used=used, blender=rss)


def stats():
    with _STL:
        if time.time() - _ST["t"] < 1:      # cache: varios espectadores no multiplican nvidia-smi
            return _ST["v"]
        gpus, driver = gpu_stats()
        try:
            du = shutil.disk_usage(DATA)
            disk = dict(free=du.free, total=du.total)
        except OSError:
            disk = {}
        with LOCK:
            busy = any(j["status"] in ("queued", "running") for j in STATE.values())
            last = max([j["finished"] or 0 for j in STATE.values()] + [START])
            running = next((j["name"] for j in STATE.values() if j["status"] == "running"), "")
        _ST["v"] = dict(now=time.time(), gpus=gpus, driver=driver, cuda=_ST["cuda"] or "", cpu=cpu_stats(),
                        ram=ram_stats(), disk=disk, running=running, idle_since=None if busy else last,
                        idle_warn_min=float(os.environ.get("IDLE_WARN_MIN", "10")), topo=TOPO["v"],
                        blender_gpus=len(HW["gpus"]) if HW["ready"] else None)
        _ST["t"] = time.time()
        return _ST["v"]


def need_disk(size):
    try:
        return shutil.disk_usage(DATA).free >= size
    except OSError:
        return True


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bfarm"

    def log_message(self, *a):
        pass

    # -- utilidades
    def send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def err(self, code, msg):
        self.send(code, {"error": msg})

    def authed(self):
        t = ""
        a = self.headers.get("Authorization", "")
        if a.startswith("Bearer "):
            t = a[7:]
        else:
            t = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        if hmac.compare_digest(t.encode(), TOKEN.encode()):
            return True
        time.sleep(0.5)  # frena fuerza bruta
        self.err(401, "token invalido")
        return False

    def body_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 1 << 20:
            raise ValueError("cuerpo muy grande")
        return json.loads(self.rfile.read(n) or b"{}")

    def view(self, j):
        v = {k: j[k] for k in ("id", "name", "status", "settings", "created", "started", "finished",
                               "progress", "frame", "total", "sample", "samples", "error")}
        v["eta"] = j.get("eta")
        v["last"] = j.get("last", "")
        v["files"] = outputs(j["id"])
        pv = JOBS / j["id"] / "preview.png"
        v["preview"] = int(pv.stat().st_mtime) if pv.exists() else 0
        v["warn"] = j.get("warn", "")
        for k in ("stage", "detail", "device", "rendering", "pool"):
            v[k] = j.get(k, "")
        now = time.time()
        if j["status"] == "running":
            v["elapsed"] = int(now - (j.get("started") or now))
            v["render_s"] = int(now - j["render_t0"]) if j.get("render_t0") else None
        elif j.get("started") and j.get("finished"):
            v["elapsed"] = int(j["finished"] - j["started"])
        if j["status"] == "running":
            v["idle_s"] = int(time.time() - j.get("last_at", time.time()))
        if j["status"] == "queued":
            q = [i for i in QUEUE if STATE[i]["status"] == "queued"]
            v["pos"] = q.index(j["id"]) + 1 if j["id"] in q else 0
            run = next((x for x in STATE.values() if x["status"] == "running"), None)
            v["wait"] = run["name"] if run else ""
        return v

    # -- rutas
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8",
                             {"Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; "
                              "script-src 'unsafe-inline'; img-src 'self' blob:; media-src 'self'; frame-ancestors 'none'"})
        if path == "/healthz":
            return self.send(200, {"ok": True})
        if not self.authed():
            return
        if path == "/api/info":
            return self.send(200, dict(hw=HW, presets=PRESETS, defaults=DEFAULTS, max_upload=MAX_UPLOAD,
                                       inbox=str(INBOX), version=1))
        if path == "/api/stats":
            return self.send(200, stats())
        if path == "/api/jobs":
            with LOCK:
                js = sorted(STATE.values(), key=lambda j: -j["created"])
                return self.send(200, [self.view(j) for j in js])
        m = re.fullmatch(r"/api/jobs/([a-f0-9]{12})(/log|/zip|/preview|/files/(.+))?", path)
        if m and m[1] in STATE:
            j = STATE[m[1]]
            if not m[2]:
                return self.send(200, self.view(j))
            if m[2] == "/log":
                lf = JOBS / j["id"] / "log.txt"
                txt = lf.read_text(errors="replace")[-20000:] if lf.exists() else ""
                return self.send(200, txt, "text/plain; charset=utf-8")
            if m[2] == "/zip":
                return self.zip(j)
            if m[2] == "/preview":
                return self.file(j, "preview.png", JOBS / j["id"])
            return self.file(j, unquote(m[3]))
        self.err(404, "no existe")

    def file(self, j, name, base=None):
        out = ((base or JOBS / j["id"] / "out")).resolve()
        f = (out / name).resolve()
        if f.parent != out or not f.is_file():
            return self.err(404, "no existe")
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".mp4": "video/mp4"}.get(f.suffix.lower(), "application/octet-stream")
        size = f.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'inline; filename="%s"' % safe_name(f.name))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        with open(f, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1 << 20)
        self.close_connection = True

    def zip(self, j):
        files = outputs(j["id"])
        if not files:
            return self.err(404, "sin resultados")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="%s.zip"' % safe_name(j["name"]))
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        w = self.wfile

        class Chunked:  # zipfile puede escribir en streams sin seek
            pos = 0
            def write(s, b):
                if b:
                    w.write(b"%x\r\n" % len(b) + bytes(b) + b"\r\n")
                    s.pos += len(b)
                return len(b)
            def tell(s): return s.pos
            def flush(s): pass

        with zipfile.ZipFile(Chunked(), "w", zipfile.ZIP_STORED) as z:
            for n in files:
                z.write(JOBS / j["id"] / "out" / n, n)
        w.write(b"0\r\n\r\n")
        self.close_connection = True

    def do_POST(self):
        if not self.authed():
            return
        p = urlparse(self.path)
        try:
            if p.path == "/api/upload":      # cuerpo crudo = archivo; ?name=x.blend
                return self.upload(parse_qs(p.query).get("name", ["file.blend"])[0])
            if p.path in ("/api/upload/start", "/api/upload/chunk", "/api/upload/finish"):
                return self.chunked(p.path.rsplit("/", 1)[1], parse_qs(p.query))
            if p.path == "/api/jobs":
                b = self.body_json()
                uid = str(b.get("upload", ""))
                if not re.fullmatch(r"[a-f0-9]{16}", uid) or not (UPLOADS / uid).is_dir():
                    return self.err(400, "subida invalida")
                j = adopt(UPLOADS / uid, b.get("name"), b.get("settings"))
                return self.send(201, self.view(j))
            m = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/rerun", p.path)
            if m:
                with LOCK:
                    j = rerun(m[1], self.body_json().get("settings"))
                return self.send(201, self.view(j))
            m = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/cancel", p.path)
            if m:
                with LOCK:
                    return self.send(200, {"ok": cancel_job(m[1])})
        except (ValueError, zipfile.BadZipFile, json.JSONDecodeError) as e:
            return self.err(400, str(e))
        self.err(404, "no existe")

    def do_DELETE(self):
        if not self.authed():
            return
        m = re.fullmatch(r"/api/jobs/([a-f0-9]{12})", urlparse(self.path).path)
        if m:
            with LOCK:
                return self.send(200, {"ok": delete_job(m[1])})
        self.err(404, "no existe")

    def upload(self, name):
        n = int(self.headers.get("Content-Length", -1))
        if n < 0 or n > MAX_UPLOAD:
            return self.err(413, "archivo demasiado grande (max %d GB)" % (MAX_UPLOAD >> 30))
        name = safe_name(name)
        if not name.lower().endswith((".blend", ".zip")):
            return self.err(400, "solo .blend o .zip")
        if not need_disk(n * 3):
            return self.err(507, "poco espacio en disco en el servidor")
        uid = secrets.token_hex(8)
        d = UPLOADS / uid
        d.mkdir()
        dst = d / name
        try:
            left = n
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if self.headers.get("X-Encoding") == "gzip" else None
            written = 0
            with open(dst, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise ValueError("subida interrumpida")
                    left -= len(chunk)
                    parts = []
                    if dec:   # el navegador comprime el .blend: se envia 2-4x menos
                        while chunk:
                            parts.append(dec.decompress(chunk, 1 << 24))
                            chunk = dec.unconsumed_tail
                    else:
                        parts.append(chunk)
                    for b in parts:
                        written += len(b)
                        if written > MAX_UPLOAD:
                            raise ValueError("archivo demasiado grande al descomprimir")
                        f.write(b)
                if dec:
                    f.write(dec.flush())
            if name.lower().endswith(".zip"):
                safe_unzip(dst, d)
            blend = find_blend(d)
            if not blend:
                raise ValueError("no hay ningun .blend dentro")
        except Exception as e:
            shutil.rmtree(d, ignore_errors=True)
            return self.err(400, str(e))
        self.send(201, {"upload": uid, "name": Path(name).stem, "blend": blend})

    # ---- subida en trozos paralelos (reintentable): start -> chunk x N -> finish
    def chunked(self, step, q):
        g = lambda k, d="": q.get(k, [d])[0]
        if step == "start":
            size = int(g("size", -1))
            name = safe_name(g("name"))
            if not 0 < size <= MAX_UPLOAD:
                return self.err(413, "tamano invalido o mayor al maximo (%d GB)" % (MAX_UPLOAD >> 30))
            if not name.lower().endswith((".blend", ".zip")):
                return self.err(400, "solo .blend o .zip")
            if not need_disk(size * 3):
                return self.err(507, "poco espacio en disco en el servidor (libera espacio o usa mas disco)")
            uid = secrets.token_hex(8)
            (UPLOADS / uid).mkdir()
            part = UPLOADS / uid / (name + ".part")
            with open(part, "wb") as f:
                f.truncate(size)
            with LOCK:
                PARTS[uid] = dict(name=name, size=size, got={}, gz=g("enc") == "gzip", lock=threading.Lock())
            return self.send(201, {"id": uid})
        u = PARTS.get(g("id"))
        if not u:
            return self.err(404, "subida desconocida")
        d = UPLOADS / g("id")
        part = d / (u["name"] + ".part")
        if step == "chunk":
            off, n = int(g("off", -1)), int(self.headers.get("Content-Length", -1))
            if off < 0 or n <= 0 or n > 64 << 20 or off + n > u["size"]:
                return self.err(400, "trozo invalido")
            body = self.rfile.read(n)
            if len(body) != n:
                return self.err(400, "trozo incompleto")
            with u["lock"]:
                with open(part, "r+b") as f:
                    f.seek(off)
                    f.write(body)
                u["got"][off] = n    # reenviar el mismo trozo es seguro (idempotente)
            return self.send(200, {"got": sum(u["got"].values())})
        # finish
        if sum(u["got"].values()) != u["size"]:
            return self.err(400, "faltan trozos (%d de %d bytes)" % (sum(u["got"].values()), u["size"]))
        try:
            dst = d / u["name"]
            if u["gz"]:
                dec, written = zlib.decompressobj(16 + zlib.MAX_WBITS), 0
                with open(part, "rb") as src, open(dst, "wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        while chunk:
                            b = dec.decompress(chunk, 1 << 24)
                            written += len(b)
                            if written > MAX_UPLOAD:
                                raise ValueError("archivo demasiado grande al descomprimir")
                            out.write(b)
                            chunk = dec.unconsumed_tail
                    out.write(dec.flush())
                part.unlink()
            else:
                part.rename(dst)
            if u["name"].lower().endswith(".zip"):
                safe_unzip(dst, d)
            blend = find_blend(d)
            if not blend:
                raise ValueError("no hay ningun .blend dentro")
        except Exception as e:
            shutil.rmtree(d, ignore_errors=True)
            PARTS.pop(g("id"), None)
            return self.err(400, str(e))
        PARTS.pop(g("id"), None)
        self.send(201, {"upload": g("id"), "name": Path(u["name"]).stem, "blend": blend})


def main():
    for d in UPLOADS.iterdir():   # subidas abandonadas (>1 dia)
        if d.is_dir() and time.time() - d.stat().st_mtime > 86400:
            shutil.rmtree(d, ignore_errors=True)
    load_jobs()
    threading.Thread(target=detect_hw, daemon=True).start()
    for fn in (worker, inbox_watcher, cleaner, topo_loop):
        threading.Thread(target=fn, daemon=True).start()
    WAKE.set()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    srv.daemon_threads = True
    print("=" * 60, flush=True)
    print("BlenderFarm Lite listo en puerto %d" % PORT, flush=True)
    if GENERATED[0]:
        print("CONTRASENA (generada, 32 caracteres): %s" % TOKEN, flush=True)
        print("Abre: http://IP:%d/#t=%s" % (PORT, TOKEN), flush=True)
    else:
        print("Contrasena: la que pusiste en BFARM_PASSWORD", flush=True)
        print("Abre: http://IP:%d/  y escribela" % PORT, flush=True)
    print("=" * 60, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
