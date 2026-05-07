"""OpenHost auth-proxy sidecar for Tasks.md.

Sits between the OpenHost router and the Tasks.md Node.js
server.  Trusts the OpenHost router's
``X-OpenHost-Is-Owner: true`` header as the sole authentication
signal: the router stamps that header AFTER JWT-verifying the
visitor's ``zone_auth`` cookie (or API bearer token), and
strips any client-supplied versions before stamping its own.
Combined with our ``public_paths = []`` in openhost.toml, this
means anonymous traffic never reaches us — the router 302s
anonymous visitors to the zone /login page and we only ever
see authenticated requests.

Tasks.md itself has NO per-user authentication.  This proxy is
the only auth gate.  Everyone the router lets through is
treated as the zone owner; non-owners get 403 from us before
the upstream sees the request.

Defence in depth: the proxy ALSO strips client-supplied
``X-OpenHost-Is-Owner`` and ``X-OpenHost-User`` headers on
inbound requests.  The OpenHost router does this strip too,
so we'd have to be both bypassed AND a hostile client
injecting forged headers for this to matter, but stripping
again costs nothing and closes the loop.

This is the simplest of the OpenHost auth patterns — no
cookie set, no session minted, no WebSocket forwarding (Tasks
.md doesn't use WS), no app-side login dance.  Same shape as
openhost-syncthing's hard-gate model.

Implementation derives from openhost-minio/auth_proxy.py
(stripped of the auto-login + WebSocket paths) and
openhost-syncthing/auth_proxy.py (stripped of the JWKS
verification, since the router already does it).
"""

from __future__ import annotations

import http.client
import logging
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import AbstractSet, Iterable

# -- Constants -----------------------------------------------------

OWNER_HEADER_NAME = "X-OpenHost-Is-Owner"
USER_HEADER_NAME = "X-OpenHost-User"

# Hop-by-hop headers (RFC 9110 §7.6.1) plus a few we rewrite at
# the proxy seam.  Same list as openhost-minio.
HOP_BY_HOP_HEADERS = frozenset(
    h.lower()
    for h in (
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "Host",
        "Content-Length",
    )
)

# Trust headers a hostile client could try to forge.  ALWAYS
# stripped from inbound requests.
ALWAYS_STRIP_HEADERS = frozenset(
    h.lower() for h in (OWNER_HEADER_NAME, USER_HEADER_NAME)
)

# Read timeout on the inbound socket so a slow-loris client
# can't hold a thread forever.
CLIENT_READ_TIMEOUT_SECONDS = 60

# 64 MiB body cap.  Tasks.md's biggest legitimate POST is an
# image upload (the SPA caps it client-side; the upstream
# Node.js handler will reject anything past its own limits).
# Our cap mainly bounds RAM exposure if a hostile client
# sends a huge Content-Length.
MAX_BODY_BYTES = 64 * 1024 * 1024

logging.basicConfig(
    level=os.environ.get("AUTH_PROXY_LOG_LEVEL", "INFO"),
    format="[auth-proxy] %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("auth_proxy")


# -- Helpers -------------------------------------------------------


def _strip_headers(
    headers: Iterable[tuple[str, str]], drop: AbstractSet[str]
) -> list[tuple[str, str]]:
    drop_lower = {h.lower() for h in drop}
    return [(k, v) for k, v in headers if k.lower() not in drop_lower]


# -- Request handler -----------------------------------------------


class AuthProxyHandler(BaseHTTPRequestHandler):
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8080

    def log_message(self, format: str, *args) -> None:  # noqa: A002, N802
        log.info("%s - " + format, self.address_string(), *args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def _safe_send_error(self, code: int, message: str) -> None:
        try:
            self.send_error(code, message)
        except OSError as exc:
            log.debug("client disconnected before error response: %s", exc)

    def _dispatch(self) -> None:
        try:
            self.connection.settimeout(CLIENT_READ_TIMEOUT_SECONDS)
        except OSError:
            pass

        # Auth gate.  Trust the router's stamp: anonymous
        # traffic never reaches us (router 302's to /login
        # first), so any request that arrives here without
        # X-OpenHost-Is-Owner: true is either bypassing the
        # router (impossible in production) or a router bug.
        is_owner = (
            self.headers.get(OWNER_HEADER_NAME, "").lower() == "true"
        )
        if not is_owner:
            # 403 not 401: 401 invites the browser to pop a
            # basic-auth dialog, but our auth flow is the
            # OpenHost zone_auth cookie / API token, not basic
            # auth.
            self._safe_send_error(403, "Forbidden")
            return

        self._proxy()

    def _proxy(self) -> None:
        cleaned_headers = _strip_headers(
            self.headers.items(),
            HOP_BY_HOP_HEADERS | ALWAYS_STRIP_HEADERS,
        )

        transfer_encoding = (
            self.headers.get("Transfer-Encoding", "").lower().strip()
        )
        if transfer_encoding and transfer_encoding != "identity":
            self._safe_send_error(501, "Transfer-Encoding not supported")
            return

        body: bytes | None = None
        content_length_header = self.headers.get("Content-Length")
        if content_length_header:
            try:
                length = int(content_length_header)
            except ValueError:
                self._safe_send_error(400, "invalid Content-Length")
                return
            if length < 0:
                self._safe_send_error(400, "negative Content-Length")
                return
            if length > MAX_BODY_BYTES:
                self._safe_send_error(413, "request body too large")
                return
            if length > 0:
                try:
                    body = self.rfile.read(length)
                except (OSError, TimeoutError) as exc:
                    log.info("client read error: %s", exc)
                    self._safe_send_error(400, "request body read failed")
                    return
                if len(body) != length:
                    log.info(
                        "short read: expected %d bytes, got %d",
                        length,
                        len(body),
                    )
                    self._safe_send_error(400, "incomplete request body")
                    return
            else:
                body = b""
        elif self.command in ("POST", "PUT", "PATCH", "DELETE"):
            body = b""

        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=60
        )
        try:
            try:
                conn.putrequest(
                    self.command,
                    self.path,
                    skip_host=False,
                    skip_accept_encoding=True,
                )
                for key, value in cleaned_headers:
                    conn.putheader(key, value)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(message_body=body)
                upstream = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream error: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                return

            try:
                payload = upstream.read(MAX_BODY_BYTES + 1)
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream read error: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                try:
                    upstream.close()
                except Exception as close_exc:  # noqa: BLE001 - best effort
                    log.debug("upstream.close() raised: %s", close_exc)
                return
            try:
                upstream.close()
            except Exception as exc:  # noqa: BLE001 - best effort only
                log.debug("upstream.close() raised (ignored): %s", exc)
            if len(payload) > MAX_BODY_BYTES:
                log.warning(
                    "upstream response exceeded %d bytes; returning 502",
                    MAX_BODY_BYTES,
                )
                self._safe_send_error(502, "upstream response too large")
                return

            reason = upstream.reason or ""
            try:
                self.send_response(upstream.status, reason)
                for key, value in upstream.getheaders():
                    if key.lower() in HOP_BY_HOP_HEADERS:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except OSError as exc:
                log.debug("client disconnected mid-response: %s", exc)
        finally:
            conn.close()


# -- Server bootstrap ---------------------------------------------


class IPv4ThreadingServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True


def _port_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer: {exc}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name}={raw!r} is out of range (1-65535)")
    return port


def main() -> int:
    try:
        listen_port = _port_from_env("AUTH_PROXY_LISTEN_PORT", 8090)
        upstream_port = _port_from_env("AUTH_PROXY_UPSTREAM_PORT", 8080)
    except ValueError as exc:
        log.error("invalid port configuration: %s", exc)
        return 1

    upstream_host = (
        os.environ.get("AUTH_PROXY_UPSTREAM_HOST", "").strip() or "127.0.0.1"
    )

    AuthProxyHandler.upstream_host = upstream_host
    AuthProxyHandler.upstream_port = upstream_port

    try:
        server = IPv4ThreadingServer(
            ("0.0.0.0", listen_port), AuthProxyHandler
        )
    except OSError as exc:
        log.error(
            "failed to bind auth-proxy listener on 0.0.0.0:%d: %s",
            listen_port,
            exc,
        )
        return 1
    log.info(
        "listening on 0.0.0.0:%d -> %s:%d",
        listen_port,
        upstream_host,
        upstream_port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
