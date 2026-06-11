"""Tests for the host-side auth-inject proxy (`cli_auth_proxy`).

Covers the credential resolver precedence, the header swap (dummy → real bearer)
with EOF framing, the pinned upstream, and idempotent lifecycle — the security
contract that keeps the claude credential on the host and out of every container.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from contremaitre import cli_auth_proxy
from contremaitre.cli_auth_proxy import (
    AuthProxyError,
    Provider,
    ensure_auth_proxy,
    resolve_claude_token,
    stop_auth_proxies,
)


class ResolveClaudeTokenTest(unittest.TestCase):
    def test_env_wins(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "env-token"}):
            self.assertEqual(resolve_claude_token(), "env-token")

    def test_falls_back_to_keychain(self):
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
            patch.object(cli_auth_proxy, "_keychain_claude_token", return_value="kc-token"),
            patch.object(cli_auth_proxy, "_credentials_file_claude_token", return_value=None),
        ):
            self.assertEqual(resolve_claude_token(), "kc-token")

    def test_falls_back_to_credentials_file(self):
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
            patch.object(cli_auth_proxy, "_keychain_claude_token", return_value=None),
            patch.object(
                cli_auth_proxy, "_credentials_file_claude_token", return_value="file-token"
            ),
        ):
            self.assertEqual(resolve_claude_token(), "file-token")

    def test_raises_when_no_source(self):
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
            patch.object(cli_auth_proxy, "_keychain_claude_token", return_value=None),
            patch.object(cli_auth_proxy, "_credentials_file_claude_token", return_value=None),
        ):
            with self.assertRaises(AuthProxyError):
                resolve_claude_token()


class _FakeUpstream:
    """A throwaway HTTPS-less upstream that records the headers it receives."""

    def __init__(self):
        self.seen_auth: str | None = None
        self.seen_headers: dict[str, str] = {}
        captured = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                captured.seen_auth = self.headers.get("Authorization")
                captured.seen_headers = {k.lower(): v for k, v in self.headers.items()}
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def hostport(self) -> str:
        host, port = self.server.server_address
        return f"{host}:{port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ProxyInjectionTest(unittest.TestCase):
    """The proxy must swap whatever auth the client sent for the real bearer.

    We make `http.client.HTTPSConnection` actually a plain-HTTP connection to a
    local fake upstream so we can inspect what the proxy forwarded, without TLS.
    """

    def tearDown(self):
        stop_auth_proxies()

    def test_swaps_dummy_for_real_and_pins_upstream(self):
        upstream = _FakeUpstream()
        self.addCleanup(upstream.stop)
        host, port = upstream.server.server_address

        def _plain_conn(hostport, *, context=None, timeout=None):
            # The proxy targets HTTPS; for the test forward over plain HTTP to the
            # local fake upstream, dropping the TLS `context` kwarg.
            return http.client.HTTPConnection(hostport, timeout=timeout)

        provider = Provider(upstream_host=f"{host}:{port}", resolve_token=lambda: "REAL-TOKEN")
        with (
            patch.dict(cli_auth_proxy.PROVIDERS, {"test": provider}),
            patch.object(http.client, "HTTPSConnection", _plain_conn),
        ):
            url = ensure_auth_proxy("test")
            # Reach the proxy on loopback (container would use host.docker.internal).
            proxy_port = cli_auth_proxy._servers["test"].server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
            conn.request(
                "POST",
                "/v1/messages",
                body=b'{"hi":1}',
                headers={
                    "Authorization": "Bearer contremaitre-injected",
                    "Content-Type": "application/json",
                },
            )
            resp = conn.getresponse()
            body = resp.read()
            conn.close()

        self.assertTrue(url.startswith("http://host.docker.internal:"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        # The dummy the client sent was replaced with the real injected bearer.
        self.assertEqual(upstream.seen_auth, "Bearer REAL-TOKEN")

    def test_idempotent_same_url(self):
        provider = Provider(upstream_host="example.invalid", resolve_token=lambda: "tok")
        with patch.dict(cli_auth_proxy.PROVIDERS, {"claude": provider}):
            a = ensure_auth_proxy("claude")
            b = ensure_auth_proxy("claude")
            self.assertEqual(a, b)

    def test_unknown_provider_raises(self):
        with self.assertRaises(AuthProxyError):
            ensure_auth_proxy("nope")


if __name__ == "__main__":
    unittest.main()
