"""Container egress posture — the single source of truth for the CLI actor.

ONE rule: a CLI container is egress-LOCKED iff it mounts a credential an injected
command could exfiltrate. Otherwise it runs OPEN (default bridge + the host
auth-inject proxy reached via host.docker.internal).

  codex  → mounts a short-lived access token in its per-run home → LOCKED
  claude → host-injected (cli_auth_proxy holds the bearer)       → OPEN

"Locked" means an `--internal` docker network (no route, no external DNS) plus an
allowlisting https proxy as the sole exit. `allow_open_egress` is the operator's
explicit, warned override.

This rule used to be restated independently in four places (provisioning in
cli.py, the preflight check, the runner's refuse-to-launch guard, and the docker
flags). They now all read from here, so it can't drift; adding a credential-
bearing CLI tool is a one-line edit to the frozenset below.

Scope: CLI tools only (codex / claude). The opencode runtime is deliberately out
of scope — it simply inherits whatever network the run already set.
"""

from __future__ import annotations

from collections.abc import Iterable

# CLI tools that mount a usable credential INSIDE their container. A tool NOT
# listed here is host-injected (cli_auth_proxy adds the bearer per request), so it
# carries nothing to exfiltrate and runs open, only needing host.docker.internal.
CREDENTIAL_BEARING_CLI_TOOLS = frozenset({"codex"})


def is_credential_bearing(tool: str) -> bool:
    """True if `tool` mounts a credential in-container (vs. host-injected).

    This is the axis the docker flags key on: host-injected tools need
    `--add-host host.docker.internal`; credential-bearing tools get the lock's
    network + proxy. It is independent of `allow_open_egress` (a codex container
    is still token-bearing even when the operator forces open egress).
    """

    return tool in CREDENTIAL_BEARING_CLI_TOOLS


def cli_tool_locked(tool: str, *, allow_open_egress: bool) -> bool:
    """Whether a single CLI tool's container must run under the egress lock."""

    return is_credential_bearing(tool) and not allow_open_egress


def any_locked_cli_tool(tools: Iterable[str], *, allow_open_egress: bool) -> bool:
    """Whether any active CLI tool needs the lock (provisioning / preflight)."""

    return any(cli_tool_locked(t, allow_open_egress=allow_open_egress) for t in tools)
