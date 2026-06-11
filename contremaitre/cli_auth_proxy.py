"""Host-side auth-injecting reverse proxy for CLI actors.

The claude CLI actor used to carry `CLAUDE_CODE_OAUTH_TOKEN` *inside* the
agent/SIM/reviewer container. Because the container runs untrusted target-repo
code, that long-lived bearer was the worst-case thing to expose. This proxy
moves the credential to the host: the container holds only a dummy
`ANTHROPIC_AUTH_TOKEN` + an `ANTHROPIC_BASE_URL` pointed here, and this proxy —
running in the host orchestrator process — swaps the dummy for the real token
(resolved live, per request) before forwarding to the provider. The container
never sees a usable credential, so it can run on open egress without the
catastrophic credential-theft leg.

Same host-owns-the-credential model as git/GitHub (`git_utils.py` /
`publisher.py`). Runs as a daemon thread (not a container) so it can re-resolve
short-lived/rotating tokens for free and keep the token out of *every*
container. Idempotent per provider: one shared instance serves every turn
(agent, SIM, reviewer, and the statusLine meter) for the process's lifetime.

Codex is intentionally NOT injected here — it already neuters its durable
refresh token (only a short-lived host-refreshed JWT enters the container) and
its `chatgpt_base_url` is validated to chatgpt.com/localhost with a
WebSocket-first responses transport, so injection is disproportionate. See the
provider registry below; the shape is multi-provider so codex can slot in later.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import ssl
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# The container reaches the host orchestrator process here. Docker Desktop maps
# host.docker.internal to the host (incl. loopback-bound services); on Linux the
# agent container is launched with --add-host=host.docker.internal:host-gateway.
CONTAINER_FACING_HOST = "host.docker.internal"
_CLAUDE_OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_BIND_HOST = "127.0.0.1"

# Hop-by-hop headers (RFC 7230) plus framing we re-derive: never forwarded.
_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


class AuthProxyError(RuntimeError):
    pass


# ----- token resolvers --------------------------------------------------------


def resolve_claude_token() -> str:
    """The operator's claude subscription bearer, from the first source that has one.

    Ordered, portable: (1) `CLAUDE_CODE_OAUTH_TOKEN` env (the documented default,
    from `claude setup-token`); (2) macOS keychain (the interactive login's
    short-lived access token); (3) `~/.claude/.credentials.json` (Linux/file
    store). Re-read per request, so a rotated keychain/file token is picked up
    live. Raises if nothing resolves.
    """

    env = os.environ.get(_CLAUDE_OAUTH_ENV)
    if env:
        return env
    token = _keychain_claude_token() or _credentials_file_claude_token()
    if token:
        return token
    raise AuthProxyError(
        f"no claude credential found: set {_CLAUDE_OAUTH_ENV} (run `claude "
        "setup-token`), or log in with `claude` (macOS keychain / "
        "~/.claude/.credentials.json)."
    )


def _keychain_claude_token() -> str | None:
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if raw.returncode != 0:
        return None
    try:
        return json.loads(raw.stdout)["claudeAiOauth"]["accessToken"] or None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _credentials_file_claude_token() -> str | None:
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth")
    if isinstance(oauth, dict):
        tok = oauth.get("accessToken")
        return tok if isinstance(tok, str) and tok else None
    return None


@dataclass(frozen=True)
class Provider:
    """How to inject auth for one CLI tool's provider API."""

    upstream_host: str  # pinned HTTPS upstream (allowlist-of-one)
    resolve_token: Callable[[], str]  # called live per request

    def identity_headers(self) -> dict[str, str]:  # pragma: no cover - claude needs none
        return {}


PROVIDERS: dict[str, Provider] = {
    "claude": Provider(upstream_host="api.anthropic.com", resolve_token=resolve_claude_token),
}


# ----- the proxy --------------------------------------------------------------


def _make_handler(provider: Provider):
    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _relay(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""

            out = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
            # Strip whatever auth the container sent (the dummy) and inject the real
            # token + any provider identity headers, resolved live on the host.
            for k in [k for k in out if k.lower() in ("authorization", "x-api-key")]:
                out.pop(k)
            out["Authorization"] = f"Bearer {provider.resolve_token()}"
            out.update(provider.identity_headers())

            conn = http.client.HTTPSConnection(
                provider.upstream_host, context=ssl.create_default_context(), timeout=600
            )
            try:
                conn.request(self.command, self.path, body=body, headers=out)
                resp = conn.getresponse()
                # Frame by closing at EOF: we forward the de-chunked body without a
                # Content-Length, so the client must read until the socket closes.
                # Without this the client hangs waiting for more body.
                self.close_connection = True
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in _HOP:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            finally:
                conn.close()

        do_GET = _relay
        do_POST = _relay
        do_PUT = _relay
        do_DELETE = _relay
        do_PATCH = _relay

        def log_message(self, *_args) -> None:  # silence default stderr logging
            pass

    return _Handler


class _ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


# Idempotent per-provider singletons: one proxy thread serves every turn.
_servers: dict[str, _ThreadingServer] = {}
_lock = threading.Lock()


def ensure_auth_proxy(provider_name: str = "claude") -> str:
    """Start (once) the auth-inject proxy for `provider_name`; return its base URL.

    Idempotent within the process — repeated calls return the same running
    proxy's container-facing URL (`http://host.docker.internal:<port>`). Raises
    if the credential can't be resolved, so a misconfigured run fails before a
    container launches rather than mid-turn.
    """

    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise AuthProxyError(f"no auth-proxy provider registered for {provider_name!r}")
    provider.resolve_token()  # fail fast if no credential source

    with _lock:
        server = _servers.get(provider_name)
        if server is None:
            server = _ThreadingServer((_BIND_HOST, 0), _make_handler(provider))
            threading.Thread(
                target=server.serve_forever,
                name=f"auth-proxy-{provider_name}",
                daemon=True,
            ).start()
            _servers[provider_name] = server
        port = server.server_address[1]
    return f"http://{CONTAINER_FACING_HOST}:{port}"


def stop_auth_proxies() -> None:
    """Shut every running auth proxy down (orchestrator cleanup)."""

    with _lock:
        for server in _servers.values():
            server.shutdown()
            server.server_close()
        _servers.clear()
