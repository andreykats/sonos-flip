#!/usr/bin/env bash
# Build sonos-flip from this checkout on a remote Docker host over ssh and (re)start the container.
# Useful for testing changes before they're published; to just run it, use the Docker Hub image.
#
#   HOST=root@myserver.local CONFIG_DIR=/srv/sonos-flip examples/deploy-ssh.sh
#
# Or put HOST and CONFIG_DIR in deploy.env at the top of the checkout (ignored by git).
# CONFIG_DIR on the host is mounted at /config. If it has no config.toml yet, config.example.toml
# is copied there and the script stops so you can fill in [speakers]; an existing config.toml is
# never overwritten. The container gets the host's time zone (from /etc/timezone) for its logs.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f deploy.env ]] && source deploy.env
: "${HOST:?set HOST, e.g. HOST=root@myserver.local}"
: "${CONFIG_DIR:?set CONFIG_DIR, the folder on the host for config.toml, e.g. /srv/sonos-flip}"
NAME="${NAME:-sonos-flip}"

ssh "$HOST" mkdir -p "$CONFIG_DIR"
if ! ssh "$HOST" test -e "$CONFIG_DIR/config.toml"; then
  ssh "$HOST" "cat > '$CONFIG_DIR/config.toml'" < config.example.toml
  echo "No config.toml on $HOST, so the example was copied to $CONFIG_DIR/config.toml."
  echo "Fill in [speakers] there, then run this again."
  exit 1
fi
port=$(ssh "$HOST" cat "$CONFIG_DIR/config.toml" | sed -n 's/^port[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
port="${port:-8080}"
tz=$(ssh "$HOST" cat /etc/timezone 2>/dev/null || echo Etc/UTC)

echo "Building $NAME on $HOST"
# COPYFILE_DISABLE / --no-xattrs: keep macOS metadata out of the build context
COPYFILE_DISABLE=1 tar --no-xattrs --exclude __pycache__ -cf - Dockerfile config.example.toml sonos_flip \
  | ssh "$HOST" docker build -q -t "$NAME" -

echo "Starting $NAME"
ssh "$HOST" docker rm -f "$NAME" >/dev/null 2>&1 || true
# The watchtower label stops watchtower from trying to pull this locally built image.
ssh "$HOST" docker run -d --name "$NAME" --network host --restart unless-stopped \
  -v "$CONFIG_DIR:/config" -e "TZ=$tz" \
  --label com.centurylinklabs.watchtower.enable=false "$NAME" >/dev/null

for _ in $(seq 30); do
  if body=$(ssh "$HOST" curl -fsS -m 3 "http://127.0.0.1:$port/health" 2>/dev/null); then
    echo "Up: $body"
    ssh "$HOST" docker logs --tail 10 "$NAME"
    exit 0
  fi
  sleep 1
done
echo "$NAME isn't healthy on port $port yet. Logs:" >&2
ssh "$HOST" docker logs --tail 30 "$NAME" >&2
exit 1
