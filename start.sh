#!/bin/bash
# Launch Tasks.md on OpenHost.
#
# Topology:
#
#   browser → OpenHost outer Caddy (TLS termination)
#          → OpenHost router (subdomain tasks-md.<zone>; JWT-
#                              verifies and stamps
#                              X-OpenHost-Is-Owner: true)
#          → container :8090   (auth_proxy.py — header gate)
#          → 127.0.0.1:8080    (Tasks.md / Node.js server)
#
# Two auth gates layered:
#
#   1. OpenHost router: anonymous visitors get 302'd to /login;
#      we never see them.  Owners arrive with
#      X-OpenHost-Is-Owner: true.
#   2. auth_proxy.py: 403's any request without the owner
#      header.  Defence in depth: the router strips client-
#      supplied versions of the header before stamping its
#      own, so a hostile client can't forge identity, but
#      we re-strip and re-check anyway in case the router
#      has a bug or is bypassed.
#
# Tasks.md itself has NO per-user authentication.  It trusts
# that anyone reaching it has been authorised upstream.  Same
# model as openhost-syncthing.
#
# We use bash specifically (not /bin/sh) for `wait -n`.
set -euo pipefail

# -----------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------
#
# OpenHost mounts the persistent app-data dir at
# OPENHOST_APP_DATA_DIR.  In a real deploy this resolves to
# /data/app_data/tasks-md inside the container.  The upstream
# Tasks.md image hardcodes /tasks and /config as the data
# directories; we symlink those into the persistent volume
# so the upstream entrypoint sees the data it expects.
PERSIST="${OPENHOST_APP_DATA_DIR:-/data/app_data/tasks-md}"
TASKS_DIR="$PERSIST/tasks"
CONFIG_DIR="$PERSIST/config"
mkdir -p "$TASKS_DIR" "$CONFIG_DIR"

# Symlink the persistent dirs into the locations the upstream
# entrypoint expects.  The upstream image VOLUME-declares
# /tasks and /config; podman will preserve the symlink we
# create here as long as nothing's mounted on top of it.
#
# Why symlink rather than mount-binding via the OpenHost
# manifest?  The OpenHost data-dir model is one persistent
# volume per app; symlinking lets us partition that one
# volume into the two upstream-expected dirs without needing
# multi-volume support.
if [[ ! -L /tasks ]]; then
    rm -rf /tasks 2>/dev/null || true
    ln -s "$TASKS_DIR" /tasks
fi
if [[ ! -L /config ]]; then
    rm -rf /config 2>/dev/null || true
    ln -s "$CONFIG_DIR" /config
fi

# -----------------------------------------------------------------
# Initialise config dir
# -----------------------------------------------------------------
#
# The SPA is pre-built in our Dockerfile (stage 2) so we skip
# the upstream entrypoint's `npm run build` step entirely.
# That saves ~30 s on cold start and several hundred MiB of
# peak build memory.  We still need to (a) ensure the config
# directory exists with the upstream's expected layout, and
# (b) sync the persistent stylesheets/ subdirectory with
# /api/static/stylesheets/ so the SPA can serve the operator's
# custom CSS overrides.

mkdir -p "$CONFIG_DIR/stylesheets" "$CONFIG_DIR/images" "$CONFIG_DIR/sort"

# Initialise default custom.css if missing.  The upstream
# code path imports the adwaita theme by default; we replicate
# that here so a fresh deployment shows the operator the same
# default appearance as a stock Tasks.md install.
if [[ ! -f "$CONFIG_DIR/stylesheets/custom.css" ]]; then
    echo "@import url(/stylesheets/color-themes/adwaita.css)" \
        > "$CONFIG_DIR/stylesheets/custom.css"
fi

# Sync stylesheets/ both directions: pre-built /api/static
# carries the bundled themes (adwaita, nord, catppuccin) and
# the persistent dir carries the operator's custom.css.
# Tasks.md serves both from /api/static/stylesheets, so we
# copy the bundled themes INTO the persistent dir (so the
# operator can edit them) and copy the operator's custom.css
# INTO /api/static (so the runtime serves it).
cp -r /api/static/stylesheets/. "$CONFIG_DIR/stylesheets/" 2>/dev/null || true
cp -r "$CONFIG_DIR/stylesheets/." /api/static/stylesheets/

# -----------------------------------------------------------------
# Launch Tasks.md
# -----------------------------------------------------------------
#
# Bind 0.0.0.0:8080 (the upstream image's default).  The port
# is loopback-reachable from the auth-proxy sibling process
# in the same container.  We don't EXPOSE 8080 in the
# Dockerfile; the only exposed port is 8090 (the auth-proxy).
echo "[start.sh] Starting Tasks.md on 0.0.0.0:8080"
CONFIG_DIR="$CONFIG_DIR" TASKS_DIR="$TASKS_DIR" \
    node /api/server.js &
TASKS_PID=$!

# Wait for Tasks.md to bind 8080.
for _ in $(seq 1 30); do
    if python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(0.5)
sys.exit(0 if s.connect_ex(('127.0.0.1', 8080)) == 0 else 1)
" 2>/dev/null; then
        break
    fi
    if ! kill -0 "$TASKS_PID" 2>/dev/null; then
        wait "$TASKS_PID" || true
        echo "[start.sh] Tasks.md exited before binding 8080"
        exit 1
    fi
    sleep 1
done

# -----------------------------------------------------------------
# Launch auth-proxy
# -----------------------------------------------------------------

echo "[start.sh] Starting auth-proxy on 0.0.0.0:8090 -> 127.0.0.1:8080"
export AUTH_PROXY_LISTEN_PORT="${AUTH_PROXY_LISTEN_PORT:-8090}"
export AUTH_PROXY_UPSTREAM_HOST="127.0.0.1"
export AUTH_PROXY_UPSTREAM_PORT="8080"
python3 /opt/openhost-tasks-md/auth_proxy.py &
PROXY_PID=$!

# -----------------------------------------------------------------
# Supervision
# -----------------------------------------------------------------

trap 'kill -TERM "$TASKS_PID" "$PROXY_PID" 2>/dev/null; wait' TERM INT

set +e
wait -n "$TASKS_PID" "$PROXY_PID"
EXIT_CODE=$?
set -e

echo "[start.sh] Child exited (code=$EXIT_CODE); shutting down"
kill -TERM "$TASKS_PID" "$PROXY_PID" 2>/dev/null || true
wait || true
exit "$EXIT_CODE"
