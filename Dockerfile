FROM ubuntu:24.04
ARG BLENDER_VERSION=5.3.0
ARG WITH_SSH=1
ENV DEBIAN_FRONTEND=noninteractive \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics \
    BLENDER_BIN=/opt/blender/blender BFARM_DATA=/data PORT=8080

# Blender sin interfaz: solo librerias de runtime (no hay X, VNC ni escritorio)
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3-minimal ca-certificates curl xz-utils \
      libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 libgl1 libegl1 libgomp1 \
 && if [ "$WITH_SSH" = 1 ]; then apt-get install -y --no-install-recommends openssh-server && mkdir -p /run/sshd; fi \
 && M=$(echo $BLENDER_VERSION | cut -d. -f1,2) \
 && curl -fsSL https://download.blender.org/release/Blender$M/blender-$BLENDER_VERSION-linux-x64.tar.xz \
    | tar -xJ -C /opt && mv /opt/blender-$BLENDER_VERSION-linux-x64 /opt/blender \
 && rm -rf /opt/blender/*/datafiles/locale /var/lib/apt/lists/* \
 && useradd -m -s /bin/bash bf

COPY server /app/server
COPY web /app/web
COPY bfarm /usr/local/bin/bfarm
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/bfarm /entrypoint.sh

EXPOSE 8080 2222
VOLUME /data
HEALTHCHECK --interval=30s CMD curl -fs http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["/entrypoint.sh"]
