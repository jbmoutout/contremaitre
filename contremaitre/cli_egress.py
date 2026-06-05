"""Turnkey egress lock for the CLI actor (ActorMode.CLI).

The CLI actor refuses to run on open egress because the in-container codex
token is a long-lived (~10-day) JWT. This module stands up the proven
two-layer lock so the operator doesn't hand-roll it:

  1. an `--internal` docker network — no route to the outside, and no external
     DNS resolution (so DNS-tunnel exfil is closed too),
  2. a squid proxy, dual-homed (internal net + bridge), that allowlists only the
     model providers' domains (OpenAI for codex; OpenRouter for a paired opencode
     SIM in a mixed run) and is the network's sole exit.

The agent container joins the internal network with HTTPS_PROXY pointed at the
proxy. Idempotent and shared across runs — the allowlist is static and
secret-free, so one long-lived proxy serves every CLI run.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

NETWORK_NAME = "contremaitre-cli-egress"
PROXY_NAME = "contremaitre-egress-proxy"
PROXY_IMAGE = "ubuntu/squid:latest"
PROXY_PORT = 3128
_SQUID_CONF = Path(__file__).resolve().parent / "cli_egress_squid.conf"
_SQUID_HASH_LABEL = "contremaitre.squid-sha256"
_READY_TIMEOUT_S = 20


class EgressError(RuntimeError):
    pass


def proxy_url() -> str:
    return f"http://{PROXY_NAME}:{PROXY_PORT}"


def ensure_egress_proxy(*, runner=subprocess.run, sleep=time.sleep) -> tuple[str, str]:
    """Ensure the internal network + allowlist proxy exist.

    Returns `(network_name, proxy_url)` to inject as `docker_network` /
    `https_proxy`. Idempotent: a no-op when both already exist. `runner` and
    `sleep` are injected for testing.
    """

    _ensure_network(runner)
    _ensure_proxy(runner, sleep)
    return NETWORK_NAME, proxy_url()


def _docker(runner, args: list[str]) -> subprocess.CompletedProcess:
    return runner(["docker", *args], capture_output=True, text=True)


def _ensure_network(runner) -> None:
    if _docker(runner, ["network", "inspect", NETWORK_NAME]).returncode == 0:
        return
    created = _docker(runner, ["network", "create", "--internal", NETWORK_NAME])
    if created.returncode != 0:
        raise EgressError(
            f"could not create internal network {NETWORK_NAME}: {created.stderr.strip()}"
        )


def _squid_conf_hash() -> str:
    return hashlib.sha256(_SQUID_CONF.read_bytes()).hexdigest()


def _ensure_proxy(runner, sleep) -> None:
    if not _SQUID_CONF.exists():
        raise EgressError(f"squid allowlist config missing: {_SQUID_CONF}")
    want_hash = _squid_conf_hash()
    running = _docker(runner, ["ps", "-q", "-f", f"name=^{PROXY_NAME}$", "-f", "status=running"])
    if running.stdout.strip():
        # Recreate when the allowlist changed under a running proxy — squid bakes
        # the config in at start, so an edited squid.conf would otherwise silently
        # never take effect (e.g. a newly added provider domain). Mirrors the
        # Dockerfile-hash image-staleness guard.
        label = _docker(
            runner,
            [
                "inspect",
                PROXY_NAME,
                "--format",
                '{{ index .Config.Labels "' + _SQUID_HASH_LABEL + '" }}',
            ],
        )
        if label.stdout.strip() == want_hash:
            return
    # Clear any stale (exited or out-of-date) container before recreating.
    _docker(runner, ["rm", "-f", PROXY_NAME])
    created = _docker(
        runner,
        [
            "run",
            "-d",
            "--name",
            PROXY_NAME,
            "--network",
            NETWORK_NAME,
            "--label",
            f"{_SQUID_HASH_LABEL}={want_hash}",
            "-v",
            f"{_SQUID_CONF}:/etc/squid/squid.conf:ro",
            PROXY_IMAGE,
        ],
    )
    if created.returncode != 0:
        raise EgressError(f"could not start egress proxy: {created.stderr.strip()}")
    # The internal network has no outbound; give the proxy a bridge interface.
    conn = _docker(runner, ["network", "connect", "bridge", PROXY_NAME])
    if conn.returncode != 0 and "already exists" not in (conn.stderr or ""):
        raise EgressError(f"could not connect proxy to bridge: {conn.stderr.strip()}")
    _wait_ready(runner, sleep)


def _wait_ready(runner, sleep) -> None:
    """Best-effort wait until squid is accepting connections.

    Not fatal if undetected — the agent container starts seconds later and
    squid is typically up by then — so a timeout just returns rather than
    raising.
    """

    for _ in range(_READY_TIMEOUT_S):
        logs = _docker(runner, ["logs", PROXY_NAME])
        if "Accepting HTTP Socket connections" in (logs.stdout + logs.stderr):
            return
        sleep(1)
