# BlenderFarm Lite

Página web mínima para subir `.blend`, renderizar en la nube (RunPod, Vast.ai, cualquier VPS con Docker) y descargar el resultado. Blender corre **sin interfaz**; la página se dibuja en tu navegador. Sin VNC, sin escritorio, backend en Python estándar (0 dependencias).

## Publicar la imagen (una sola vez, sin Docker en tu PC)
1. Sube esta carpeta a un repo de GitHub (rama `main`). El flujo `.github/workflows/docker.yml` construye la imagen y la publica en `ghcr.io/TU_USUARIO/blenderfarm:latest` (Blender ya viene dentro).
2. En GitHub: tu perfil -> Packages -> `blenderfarm` -> Package settings -> **Change visibility: Public** (para que Vast/RunPod la descarguen sin credenciales).

## Usar la imagen como plantilla (Vast.ai, RunPod, cualquier nube con Docker)
- **Image:** `ghcr.io/TU_USUARIO/blenderfarm:latest`
- **Puertos:** `8080` (web) y `2222` (SSH, opcional)
- **Launch mode (Vast):** *Docker ENTRYPOINT* (arranca la imagen tal cual)
- **Environment Variables (opcionales):** `BFARM_PASSWORD` = tu contraseña. Si no la pones, se genera una de 32 caracteres que aparece **solo en los logs** de la instancia.

Inicia, abre los logs, entra a la URL, arrastra tu `.blend` -> **Renderizar** -> descarga. Arranca en segundos: no instala nada.

| Variable | Para qué |
|---|---|
| `BFARM_PASSWORD` | contraseña de la web (vacía = aleatoria de 32 caracteres en el log) |
| `SSH_PUBKEY` | tu llave pública -> activa SSH (usuario `bf`, solo llave) |
| `SSH_PORT` | puerto SSH propio (2222) |
| `PORT` | puerto web (8080) |
| `BLENDER_VERSION` | versión de Blender (5.3.0) |
| `IDLE_WARN_MIN` | minutos sin renders antes de avisarte que apagues la instancia (10) |
| `MAX_UPLOAD_GB`, `KEEP_DAYS` | límite de subida (20) y auto-borrado de trabajos viejos (0 = nunca) |

### Alternativa sin imagen: `install.sh`
En cualquier Ubuntu: `bash install.sh` (instala Blender y arranca; mismas variables).

### Docker local
```bash
docker build -t blenderfarm .
docker run -d --gpus all -p 8080:8080 -v bfdata:/data -e BFARM_PASSWORD=tu-clave blenderfarm
```

## Modo SSH (más privado)
Pon `SSH_PUBKEY` (tu llave pública) en las Environment Variables. Solo llave, sin contraseña ni root. Cambia `-P 2222` si usaste otro `SSH_PORT`.
```bash
scp -P 2222 escena.blend bf@IP:/data/inbox/      # se renderiza solo
ssh -p 2222 bf@IP bfarm status
ssh -p 2222 bf@IP bfarm escena.blend -p final     # o: bfarm x.blend -a --start 1 --end 120
scp -P 2222 bf@IP:/data/jobs/ID/out/* .
```
Ajustes opcionales: junto a `escena.blend` copia `escena.json`, ej. `{"preset":"final","samples":256}`.

## Qué incluye
- **Imagen o animación** (rango, salto); las animaciones cortadas se reanudan.
- **12 presets**: imagen = Boceto, Borrador, Normal, Alta, Final, Cine, Ultra; animación = Boceto MP4, Vista previa MP4, Rápida PNG, Final PNG, Cine EXR. Cada uno muestra su tiempo relativo (1× = Normal).
- Calidad (muestras), umbral de ruido, resolución (720p a 8K), tamaño %, PNG/JPEG/EXR/MP4, denoise.
- CPU, GPU, GPU+CPU; **todas las GPU** (OptiX/CUDA/HIP/oneAPI/Metal) y todos los núcleos. Un trabajo a la vez usando todas las GPU.
- **Monitor de recursos** en la web: uso, VRAM, temperatura y vatios de cada GPU; CPU (núcleos reales del contenedor), RAM, RAM que usa Blender, disco libre, driver y CUDA.
- **Mis archivos**: lo subido se queda en la lista; botón **↻ Repetir** en cada trabajo para renderizar otra vez con otros ajustes sin volver a subir.
- Subida comprimida y en 4 trozos paralelos con reintentos; vista previa rápida; último frame en vivo en animaciones.
- **Progreso real por fases**: cargando, preparando (kernels/BVH), vista previa, "renderizando de verdad" (muestra x/y), guardando; tiempo transcurrido, restante y hora de término; qué dispositivo usa Blender de verdad.
- Interfaz de cristal tipo sistema operativo (ventanas, dock, anillos de progreso); toda la web pesa ~45 KB, sin librerías ni imágenes.
- Avisos: driver NVIDIA viejo (sugiere la imagen `:lts`), GPU sin memoria, trabajo sin actividad, poco disco, instancia inactiva (apágala para no pagar de más) y notificación del navegador al terminar.

## Seguridad
- Contraseña obligatoria (la tuya o una aleatoria de 32 caracteres); comparación en tiempo constante.
- Blender corre con `--disable-autoexec`: **los scripts dentro del .blend no se ejecutan** salvo que lo actives por trabajo.
- Procesos como usuario sin privilegios; zips validados (anti zip-slip); nombres saneados; límite de subida (`MAX_UPLOAD_GB`, 20).
- HTTP sin TLS: en RunPod/Vast usa su proxy HTTPS, o ponlo tras Caddy/nginx, o usa el túnel SSH: `ssh -L 8080:localhost:8080 ...`.

## Notas
- Eevee necesita GPU con OpenGL/EGL; en headless Cycles es lo fiable.
- Sin probar contra Blender real ni Docker en esta máquina (solo con un Blender simulado): prueba primero con un `.blend` pequeño.

## Licencia
MIT + Commons Clause © 2026 Zanix: puedes usarlo, editarlo y publicarlo (conservando el aviso de autor), **pero no venderlo** ni ofrecerlo como servicio de pago. `server/render_job.py` es GPL-3.0-or-later por usar la API de Blender. Detalles en [LICENSE](LICENSE) y [NOTICE.md](NOTICE.md).
