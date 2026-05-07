# OpenHost Tasks.md container.
#
# Layers an OpenHost auth-proxy sidecar on top of the upstream
# Tasks.md image.  The auth-proxy gates every request on the
# router-stamped X-OpenHost-Is-Owner: true header (Pattern A
# trusted-stamp model — same as openhost-syncthing).  Tasks.md
# itself has no per-user authentication, so this is the only
# auth gate.
#
# Auth flow:
#
#   1. Browser hits https://tasks-md.<zone>/.  The OpenHost
#      router verifies the visitor's zone_auth JWT (or API
#      bearer token) and stamps X-OpenHost-Is-Owner: true on
#      the request before forwarding to the auth-proxy on
#      container port 8090.
#   2. Auth-proxy: 403 if header missing, otherwise pass
#      through to Tasks.md on 127.0.0.1:8080.
#   3. Tasks.md: serves the SPA + REST API verbatim with no
#      authentication; trusts that anyone reaching it has
#      been gated upstream.
#
# The 8090 vs 8080 split is the standard OpenHost "auth-proxy
# binds the public port; upstream binds loopback only"
# topology — same shape as openhost-syncthing.

# Stage 1: lift the upstream Tasks.md build artefacts.
#
# Pin to 3.3.0 (latest stable as of Mar 2026) by image tag.
# The upstream image is published to docker.io/baldissaramatheus/tasks.md.
FROM docker.io/baldissaramatheus/tasks.md:3.3.0 AS tasksmd-source

# Stage 2: build the runtime image.
#
# We need (a) the Tasks.md app (Node.js + the prebuilt SPA),
# and (b) Python 3 + bash for the auth-proxy.  Tasks.md's
# upstream image is alpine-based; alpine has Python 3 in the
# main repo.  Layering on top of the upstream image directly
# is cleaner than rebuilding from scratch — we get the
# pre-built SPA + npm install for free.
FROM docker.io/baldissaramatheus/tasks.md:3.3.0

# -- Python + bash for the auth-proxy + start.sh ----------------
#
# Alpine's package names: python3 and bash.  Both are
# uncontroversial small additions; py3 is ~50 MiB and bash
# is ~2 MiB.  We could use ash (alpine's default /bin/sh) for
# start.sh but bash gives us `wait -n` which is the cleanest
# way to multiplex two children with cooperative shutdown.
USER root
RUN apk add --no-cache python3 bash

# -- auth-proxy + start.sh -------------------------------------
#
# Both files are committed with mode 0755 (verify with
# `git ls-files --stage`).  Buildah/podman preserves the git
# index mode through COPY; no `RUN chmod +x` is needed (which
# fails on operator hosts where the system crun rejects newer
# OCI metadata).
COPY auth_proxy.py /opt/openhost-tasks-md/auth_proxy.py
COPY start.sh      /opt/openhost-tasks-md/start.sh

# -- runtime ---------------------------------------------------
#
# 8090 = auth-proxy (the openhost.toml `port`, gated by the
#        OpenHost router upstream of us, owner-stamped).
# 8080 = Tasks.md (loopback only via start.sh; never EXPOSE'd).
EXPOSE 8090

# Override the upstream ENTRYPOINT.  Our start.sh handles the
# upstream entrypoint's work (build SPA, prepare config dir,
# launch node server) plus the auth-proxy supervision.
ENTRYPOINT ["/opt/openhost-tasks-md/start.sh"]
