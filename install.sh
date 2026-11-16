#!/usr/bin/env bash
# BlenderFarm Lite - instalador/arranque de 1 paso para cualquier instancia Ubuntu/Debian (RunPod, Vast.ai, VPS...).
# Uso:  bash install.sh        (como root; es idempotente: la 2a vez arranca directo)
#
# Variables de entorno (todas opcionales, ponlas en "Environment Variables" de tu plantilla):
#   BFARM_PASSWORD   contrasena de la web. Si no la pones se genera una aleatoria de 32 caracteres (sale en el log).
#   SSH_PUBKEY       tu llave publica -> activa SSH (usuario bf). Tambien vale PUBLIC_KEY.
#   SSH_PORT         puerto SSH propio (por defecto 2222)
#   PORT             puerto de la web (por defecto 8080)
#   BLENDER_VERSION  version de Blender (por defecto 5.3.0)
#   MAX_UPLOAD_GB    limite de subida (20)     KEEP_DAYS  auto-borrar trabajos viejos (0 = nunca)
set -euo pipefail

BLENDER_VERSION="${BLENDER_VERSION:-5.3.0}"
PORT="${PORT:-8080}"
SSH_PORT="${SSH_PORT:-2222}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# /workspace es el volumen persistente en RunPod/Vast: Blender y datos sobreviven a reinicios
if [ -d /workspace ]; then BASE=/workspace/blenderfarm-env; else BASE=/opt/blenderfarm-env; fi
DATA="$BASE/data"; BLENDER_DIR="$BASE/blender"
say() { echo "[blenderfarm] $*"; }

[ "$(id -u)" = 0 ] || { echo "Ejecuta como root (sudo bash install.sh)"; exit 1; }
command -v apt-get >/dev/null || { echo "Solo Ubuntu/Debian"; exit 1; }

# ---------- 1. dependencias minimas (sin escritorio, sin X, sin VNC) ----------
if [ ! -f "$BASE/.deps-ok" ]; then
  say "Instalando dependencias..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  PKGS="python3 curl ca-certificates xz-utils libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 libgl1 libegl1 libgomp1"
  [ -n "${SSH_PUBKEY:-${PUBLIC_KEY:-}}" ] && PKGS="$PKGS openssh-server"
  # shellcheck disable=SC2086
  apt-get install -y -qq --no-install-recommends $PKGS >/dev/null
  rm -rf /var/lib/apt/lists/*
  mkdir -p "$BASE" && touch "$BASE/.deps-ok"
fi
# si activaron SSH despues de la primera instalacion
if [ -n "${SSH_PUBKEY:-${PUBLIC_KEY:-}}" ] && ! command -v sshd >/dev/null; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openssh-server >/dev/null
fi

# ---------- 2. Blender (headless) ----------
if [ ! -x "$BLENDER_DIR/blender" ] || ! "$BLENDER_DIR/blender" --version 2>/dev/null | grep -q "Blender ${BLENDER_VERSION%.*}"; then
  say "Descargando Blender $BLENDER_VERSION (~400 MB, solo la primera vez)..."
  M="${BLENDER_VERSION%.*}"
  TMP="$(mktemp -d)"
  curl -fL --retry 3 -o "$TMP/b.tar.xz" "https://download.blender.org/release/Blender$M/blender-$BLENDER_VERSION-linux-x64.tar.xz"
  rm -rf "$BLENDER_DIR"; mkdir -p "$BLENDER_DIR"
  tar -xJf "$TMP/b.tar.xz" -C "$BLENDER_DIR" --strip-components=1
  rm -rf "$TMP"
fi

# ---------- 3. usuario sin privilegios + datos ----------
id bf >/dev/null 2>&1 || useradd -m -s /bin/bash bf
mkdir -p "$DATA/inbox"
chown -R bf:bf "$DATA"
install -m755 "$SRC/bfarm" /usr/local/bin/bfarm
chmod -R a+rX "$SRC" "$BASE" 2>/dev/null || true   # bf debe poder leer la app y Blender

# ---------- 4. SSH opcional (solo llave publica, puerto propio) ----------
KEY="${SSH_PUBKEY:-${PUBLIC_KEY:-}}"
if [ -n "$KEY" ]; then
  install -d -m700 -o bf -g bf /home/bf/.ssh
  echo "$KEY" > /home/bf/.ssh/authorized_keys
  chown bf:bf /home/bf/.ssh/authorized_keys; chmod 600 /home/bf/.ssh/authorized_keys
  mkdir -p /run/sshd; ssh-keygen -A >/dev/null
  pkill -f "sshd -p $SSH_PORT" 2>/dev/null || true
  /usr/sbin/sshd -p "$SSH_PORT" -o PasswordAuthentication=no -o PermitRootLogin=no -o AllowUsers=bf
  say "SSH activo en el puerto $SSH_PORT (usuario bf, solo llave)"
  say "  subir:  scp -P $SSH_PORT escena.blend bf@IP:$DATA/inbox/"
  say "  usar :  ssh -p $SSH_PORT bf@IP bfarm escena.blend -p final"
fi

# ---------- 5. arrancar la web (en primer plano: asi sale en los logs de la instancia) ----------
export BFARM_DATA="$DATA" BLENDER_BIN="$BLENDER_DIR/blender" PORT HOST=0.0.0.0
export BFARM_PASSWORD="${BFARM_PASSWORD:-${BFARM_TOKEN:-}}"
export BFARM_WEB="$SRC/web"
if [ -n "$BFARM_PASSWORD" ] && [ "${#BFARM_PASSWORD}" -lt 12 ]; then
  say "AVISO: tu contrasena es corta (<12). Usa una larga o deja vacio para generar una de 32 caracteres."
fi
if [ -n "${RUNPOD_POD_ID:-}" ]; then
  say "RunPod: abre https://${RUNPOD_POD_ID}-${PORT}.proxy.runpod.net/ (el puerto $PORT debe estar en 'Expose HTTP Ports')"
fi
say "Iniciando... la contrasena aparece en el siguiente bloque (solo en este log)."
exec setpriv --reuid=bf --regid=bf --init-groups python3 "$SRC/server/app.py"
