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
    format="PNG", quality=90, threads=0, scripts=False,
)
ENUMS = dict(engine=("cycles", "eevee"), device=("auto", "cpu", "gpu", "gpu+cpu"),
             mode=("still", "animation"), format=("PNG", "JPEG", "EXR", "MP4"))
RANGES = dict(frame=(0, 10**6), start=(0, 10**6), end=(0, 10**6), step=(1, 1000),
              samples=(0, 65536), scale=(1, 400), res_x=(0, 16384), res_y=(0, 16384),
              quality=(1, 100), threads=(0, 512))
PRESETS = {
    "draft":   dict(samples=32, scale=50, denoise=True, mode="still"),
    "normal":  dict(samples=128, scale=100, denoise=True, mode="still"),
    "final":   dict(samples=512, scale=100, denoise=True, mode="still"),
    "anim_preview": dict(samples=24, scale=50, denoise=True, mode="animation", format="MP4"),
    "anim_final":   dict(samples=128, scale=100, denoise=True, mode="animation", format="PNG"),
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

RE_FRAME = re.compile(r"Fra:(\d+)")
RE_SAMPLE = re.compile(r"Sample (\d+)/(\d+)")
RE_RANGE = re.compile(r"\[bfarm\] Animacion (\d+)-(\d+)")


def save(j):
    (JOBS / j["id"] / "job.json").write_text(json.dumps(j))


def outputs(jid):
    out = JOBS / jid / "out"
    if not out.exists():
        return []
    return sorted(f.name for f in out.iterdir() if f.is_file())


def new_job(name, blend, settings):
    jid = secrets.token_hex(6)
    (JOBS / jid).mkdir()
    j = dict(id=jid, name=name, blend=blend, settings=clean(settings), status="queued",
             created=time.time(), started=None, finished=None, progress=0.0,
             frame=None, total=None, sample=None, samples=None, error="")
    with LOCK:
        STATE[jid] = j
        QUEUE.append(jid)
        save(j)
    WAKE.set()
    return j


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
        j.update(status="running", started=j["started"] or time.time(), error="")
        save(j)
    first = last = None
    done = 0
    t0 = time.time()
    with open(jd / "log.txt", "a", errors="replace") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        try:
            if shutil.which("stdbuf"):   # Blender no vacia stdout en tuberias: sin esto el progreso no se ve
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
            m = RE_RANGE.search(line)
            if m:
                a, b = int(m[1]), int(m[2])
                j["total"] = max(1, (b - a) // max(1, s["step"]) + 1)
                first = a
            m = RE_FRAME.search(line)
            if m:
                j["frame"] = int(m[1])
            m = RE_SAMPLE.search(line)
            if m:
                j["sample"], j["samples"] = int(m[1]), int(m[2])
            if line.startswith("Saved:"):
                done += 1
            total = j["total"] or 1
            frac = (j["sample"] or 0) / (j["samples"] or 1) if j["samples"] else 0
            j["progress"] = min(0.99, (done + (frac if done < total else 0)) / total)
            if j["progress"] > 0.02:
                j["eta"] = int((time.time() - t0) * (1 - j["progress"]) / j["progress"])
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
    j = new_job(name or blend, blend, settings)
    shutil.move(str(src_dir), str(JOBS / j["id"] / "input"))
    return j


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
        if path == "/api/jobs":
            with LOCK:
                js = sorted(STATE.values(), key=lambda j: -j["created"])
                return self.send(200, [self.view(j) for j in js])
        m = re.fullmatch(r"/api/jobs/([a-f0-9]{12})(/log|/zip|/files/(.+))?", path)
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
            return self.file(j, unquote(m[3]))
        self.err(404, "no existe")

    def file(self, j, name):
        out = (JOBS / j["id"] / "out").resolve()
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
            if p.path == "/api/jobs":
                b = self.body_json()
                uid = str(b.get("upload", ""))
                if not re.fullmatch(r"[a-f0-9]{16}", uid) or not (UPLOADS / uid).is_dir():
                    return self.err(400, "subida invalida")
                j = adopt(UPLOADS / uid, b.get("name"), b.get("settings"))
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
        uid = secrets.token_hex(8)
        d = UPLOADS / uid
        d.mkdir()
        dst = d / name
        try:
            left = n
            with open(dst, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise ValueError("subida interrumpida")
                    f.write(chunk)
                    left -= len(chunk)
            if name.lower().endswith(".zip"):
                safe_unzip(dst, d)
            blend = find_blend(d)
            if not blend:
                raise ValueError("no hay ningun .blend dentro")
        except Exception as e:
            shutil.rmtree(d, ignore_errors=True)
            return self.err(400, str(e))
        self.send(201, {"upload": uid, "name": Path(name).stem, "blend": blend})


def main():
    load_jobs()
    threading.Thread(target=detect_hw, daemon=True).start()
    for fn in (worker, inbox_watcher, cleaner):
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
