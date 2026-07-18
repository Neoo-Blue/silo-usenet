FROM python:3.12-slim

# TARGETARCH is provided by buildx (amd64 / arm64) for multi-arch images
ARG TARGETARCH=amd64

# rclone (current static build for the target arch) + fuse3 for the mount, curl for health waits
RUN apt-get update && apt-get install -y --no-install-recommends \
      fuse3 curl ca-certificates bash unzip \
 && curl -fsSL "https://downloads.rclone.org/rclone-current-linux-${TARGETARCH}.zip" -o /tmp/rclone.zip \
 && unzip -j /tmp/rclone.zip '*/rclone' -d /usr/local/bin \
 && chmod +x /usr/local/bin/rclone \
 && rm -rf /tmp/rclone.zip /var/lib/apt/lists/* \
 && echo "user_allow_other" >> /etc/fuse.conf

COPY app/ /app/
RUN chmod +x /app/entrypoint.sh

ENV GATEWAY_PORT=8791 \
    MOUNT_DIR=/mount \
    TITLES_FILE=/config/titles.json \
    VFS_CACHE_MODE=off

VOLUME ["/config"]
ENTRYPOINT ["/app/entrypoint.sh"]
