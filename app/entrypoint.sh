#!/bin/bash
# Start the Easynews gateway, mount it with rclone, and auto-provision Silo.
set -e

: "${EASYNEWS_USER:?set EASYNEWS_USER}"
: "${EASYNEWS_PASS:?set EASYNEWS_PASS}"

MOUNT_DIR="${MOUNT_DIR:-/mount}"
GATEWAY_PORT="${GATEWAY_PORT:-8791}"
TITLES_FILE="${TITLES_FILE:-/config/titles.json}"
VFS_CACHE_MODE="${VFS_CACHE_MODE:-off}"

mkdir -p "$MOUNT_DIR" "$(dirname "$TITLES_FILE")"

# seed a demo title list on first run (open-source movies) so it works out of the box
if [ ! -f "$TITLES_FILE" ]; then
  printf '%s\n' '["Big Buck Bunny (2008)", "Sintel (2010)", "Tears of Steel (2012)"]' > "$TITLES_FILE"
  echo "[entrypoint] seeded demo titles at $TITLES_FILE"
fi

echo "[entrypoint] starting gateway on :$GATEWAY_PORT"
python3 /app/gateway.py &
GW=$!

# wait for the gateway to accept connections
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:${GATEWAY_PORT}/" >/dev/null 2>&1 && break
  sleep 1
done

echo "[entrypoint] mounting at $MOUNT_DIR (vfs-cache-mode=$VFS_CACHE_MODE)"
rclone mount :http: "$MOUNT_DIR" \
  --http-url="http://127.0.0.1:${GATEWAY_PORT}" \
  --read-only --allow-other \
  --dir-cache-time 30s \
  --vfs-cache-mode "$VFS_CACHE_MODE" \
  --vfs-read-chunk-size 32M --buffer-size 64M --attr-timeout 1s &
RC=$!

# wait for the mount to populate
for _ in $(seq 1 30); do
  ls "$MOUNT_DIR" >/dev/null 2>&1 && break
  sleep 1
done

# auto-provision Silo (best effort; retries internally, never fatal)
python3 /app/provision.py || true

# if either the gateway or the mount dies, exit so the container restarts
wait -n "$GW" "$RC"
echo "[entrypoint] a child process exited; shutting down"
kill "$GW" "$RC" 2>/dev/null || true
fusermount3 -uz "$MOUNT_DIR" 2>/dev/null || true
exit 1
