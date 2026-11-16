#!/bin/sh
# Arranque de la imagen. Variables opcionales: BFARM_PASSWORD, SSH_PUBKEY (o PUBLIC_KEY), SSH_PORT, PORT, MAX_UPLOAD_GB, KEEP_DAYS
set -e
mkdir -p /data/inbox
chown bf:bf /data /data/inbox

KEY="${SSH_PUBKEY:-$PUBLIC_KEY}"
SSH_PORT="${SSH_PORT:-2222}"
# SSH opcional: solo con llave publica, sin contrasenas, sin root, puerto propio
if [ -n "$KEY" ] && command -v sshd >/dev/null; then
  install -d -m700 -o bf -g bf /home/bf/.ssh
  echo "$KEY" > /home/bf/.ssh/authorized_keys
  chown bf:bf /home/bf/.ssh/authorized_keys && chmod 600 /home/bf/.ssh/authorized_keys
  ssh-keygen -A >/dev/null
  /usr/sbin/sshd -p "$SSH_PORT" -o PasswordAuthentication=no -o PermitRootLogin=no -o AllowUsers=bf
  echo "SSH activo en el puerto $SSH_PORT (usuario bf, solo llave)"
  echo "  subir: scp -P $SSH_PORT escena.blend bf@IP:/data/inbox/"
fi

# la app corre sin privilegios; si no hay BFARM_PASSWORD genera una de 32 caracteres y la muestra en el log
export BFARM_PASSWORD="${BFARM_PASSWORD:-$BFARM_TOKEN}"
exec setpriv --reuid=bf --regid=bf --init-groups python3 /app/server/app.py
