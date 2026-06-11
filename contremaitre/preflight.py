"""Operational preflight checks for live Contremaitre runs.

These checks turn deployment assumptions into explicit pass/fail evidence. They
do not replace provider-side limits or network policy, but they prevent live
opencode runs from starting when those controls are absent or unverifiable.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonlog import write_json
from .models import ActorMode, RunConfig, RunPaths, is_zen_model


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class PreflightReport:
    passed: bool
    checks: list[PreflightCheck]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "message": check.message,
                    "details": check.details,
                }
                for check in self.checks
            ],
        }

    def failure_summary(self) -> str:
        failures = [check for check in self.checks if check.status == "FAIL"]
        return "; ".join(f"{check.name}: {check.message}" for check in failures)


def _active_cli_tools(config: RunConfig) -> set[str]:
    """The CLI tool(s) in play this run.

    Agent, SIM, and post-publish reviewer may differ. The reviewer is included
    because it is still a Dockerized subscription-CLI container and needs the
    same provider auth + egress lock checks.
    """

    tools: set[str] = set()
    if config.actor_mode == ActorMode.CLI:
        tools.add(config.cli_tool)
    if (config.sim_actor_mode or config.actor_mode) == ActorMode.CLI:
        tools.add(config.sim_cli_tool or config.cli_tool)
    if config.cli_reviewer in {"claude", "codex"}:
        tools.add(config.cli_reviewer)
    return tools


def run_preflight(config: RunConfig) -> PreflightReport:
    # The agent uses actor_mode; the SIM may override it (per-role mixing), so
    # we validate the UNION of both runtimes' requirements. OpenRouter is only
    # checked if opencode is in play; codex auth only if the CLI actor is.
    modes = {config.actor_mode, config.sim_actor_mode or config.actor_mode}
    funcs = [_check_repo]
    if ActorMode.OPENCODE in modes:
        funcs += [
            _check_opencode_config,
            _check_docker_image,
            _check_opencode_binary,
            _check_readonly_mount,
            _check_network_policy,
            _check_openrouter_key,
        ]
    active_cli_tools = _active_cli_tools(config)
    if active_cli_tools:
        # CLI actor: no OpenRouter key (the CLI uses the operator's subscription),
        # but the SAME egress policy applies (the in-container token is
        # long-lived), plus an auth-source check per ACTIVE CLI tool — so a mixed
        # codex+claude run validates both the codex token and the claude OAuth token.
        funcs += [_check_docker_image, _check_readonly_mount, _check_network_policy]
        for tool in sorted(active_cli_tools):
            funcs.append(_check_claude_auth if tool == "claude" else _check_codex_auth)
    # Dedupe by function identity (image/readonly/network are shared) so each
    # check runs once even when both runtimes are active.
    seen: set = set()
    ordered = [f for f in funcs if not (f in seen or seen.add(f))]
    checks = [f(config) for f in ordered]
    passed = all(check.status != "FAIL" for check in checks)
    return PreflightReport(passed=passed, checks=checks)


def enforce_preflight(config: RunConfig, paths: RunPaths) -> None:
    report = run_preflight(config)
    write_json(paths.preflight_report, report.to_dict())
    if not report.passed:
        raise PreflightError(report.failure_summary())


def _check_repo(config: RunConfig) -> PreflightCheck:
    if not config.repo.exists():
        return _fail("repo", f"repo does not exist: {config.repo}", {})
    proc = _run(["git", "-C", str(config.repo), "rev-parse", "--is-inside-work-tree"])
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return _fail("repo", "repo is not a git worktree", _proc_details(proc))
    # Only the remote-tracking ref is authoritative. The clone cache is a
    # performance hop for git objects; local branches (`refs/heads/<base>`)
    # are never used by the orchestrator — `_create_worktree` creates the
    # worktree from `origin/<base>` precisely to avoid trusting local refs.
    # `_ensure_local_clone` runs `git fetch origin <base>` on every run, so
    # `origin/<base>` reflects the remote as of this run's start.
    remote_ref = f"origin/{config.base}"
    base = _run(["git", "-C", str(config.repo), "rev-parse", "--verify", remote_ref])
    if base.returncode != 0:
        return _fail(
            "repo_base", f"base ref not found on origin: {remote_ref}", _proc_details(base)
        )
    return _pass(
        "repo", "repo and base ref are available", {"repo": str(config.repo), "base": remote_ref}
    )


def _check_opencode_config(config: RunConfig) -> PreflightCheck:
    if not config.opencode_config:
        return _fail("opencode_config", "--opencode-config is required for --agent opencode", {})
    if not config.opencode_config.exists():
        return _fail(
            "opencode_config", f"opencode config does not exist: {config.opencode_config}", {}
        )
    return _pass("opencode_config", "opencode config exists", {"path": str(config.opencode_config)})


def _check_docker_image(config: RunConfig) -> PreflightCheck:
    docker = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if docker.returncode != 0:
        return _fail("docker", "docker daemon is not available", _proc_details(docker))
    inspect = _run(
        ["docker", "image", "inspect", config.docker_image, "--format", "{{.Id}} {{.Created}}"]
    )
    if inspect.returncode != 0:
        return _fail(
            "docker_image",
            f"docker image not found: {config.docker_image} "
            f"(build via `contremaitre image build` or pass --docker-image <existing-tag>)",
            _proc_details(inspect),
        )
    return _pass(
        "docker_image",
        "docker image is available",
        {"image": config.docker_image, "inspect": inspect.stdout.strip()},
    )


def _check_opencode_binary(config: RunConfig) -> PreflightCheck:
    proc = _run(
        ["docker", "run", "--rm", config.docker_image, "/root/.opencode/bin/opencode", "--version"]
    )
    if proc.returncode != 0:
        return _fail("opencode_binary", "opencode binary failed inside image", _proc_details(proc))
    return _pass(
        "opencode_binary", "opencode binary runs inside image", {"version": proc.stdout.strip()}
    )


def _check_readonly_mount(config: RunConfig) -> PreflightCheck:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        probe = root / ".contremaitre_ro_probe"
        proc = _run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{root}:/app:ro",
                "-w",
                "/app",
                config.docker_image,
                "sh",
                "-lc",
                "touch /app/.contremaitre_ro_probe",
            ]
        )
        if proc.returncode == 0 or probe.exists():
            return _fail(
                "readonly_mount",
                "container could write to a read-only /app mount",
                {"returncode": proc.returncode, "probe_exists": probe.exists()},
            )
        return _pass("readonly_mount", "read-only /app mount rejected writes", _proc_details(proc))


def _check_network_policy(config: RunConfig) -> PreflightCheck:
    active = _active_cli_tools(config)
    codex_active = "codex" in active
    claude_active = "claude" in active
    opencode_active = ActorMode.OPENCODE in {config.actor_mode, config.sim_actor_mode}
    # Only codex carries a token INSIDE its container, so only codex requires the
    # egress lock: an `--internal` docker network AND an allowlisting https proxy
    # (exactly `cli_actor._assert_egress_locked` for codex). Gate on BOTH — a
    # generic "either is set" check would pass a half-configured run the first
    # codex turn then refuses. `_maybe_provision_cli_egress` sets both before we
    # land.
    if codex_active and not config.allow_open_egress:
        if config.docker_network and config.https_proxy:
            return _pass(
                "network_policy",
                "codex egress locked (internal docker network + allowlist https proxy)",
                {"docker_network": config.docker_network, "https_proxy": True},
            )
        return _fail(
            "network_policy",
            "codex role requires BOTH an --internal docker_network and an "
            "allowlisting --https-proxy (or --allow-open-egress); auto-provisioning "
            "did not set both — check Docker is up.",
            {"docker_network": config.docker_network, "https_proxy": bool(config.https_proxy)},
        )
    # claude carries NO in-container credential — the host auth-inject proxy
    # (`cli_auth_proxy`) holds the token and adds the bearer per request — so
    # open egress is the safe default. (A codex reviewer in the same run may set
    # docker_network for its own lock; the claude container ignores it.)
    if claude_active and not codex_active and not opencode_active:
        return _pass(
            "network_policy",
            "claude runs open egress (no in-container credential; host auth-inject proxy holds the token)",
            {},
        )
    # opencode (or a CLI role explicitly opened): either a proxy or a network is
    # acceptable; `--allow-open-egress` is the explicit open override.
    explicit_proxy = bool(config.http_proxy or config.https_proxy)
    explicit_network = bool(config.docker_network)
    if explicit_proxy or explicit_network:
        return _pass(
            "network_policy",
            "explicit Docker network/proxy configuration supplied",
            {
                "docker_network": config.docker_network,
                "http_proxy": bool(config.http_proxy),
                "https_proxy": bool(config.https_proxy),
            },
        )
    if config.allow_open_egress:
        return _warn(
            "network_policy",
            "open container egress explicitly allowed by --allow-open-egress",
            {},
        )
    return _fail(
        "network_policy",
        "opencode mode requires --docker-network or proxy flags, or explicit --allow-open-egress",
        {},
    )


def _check_cli_auth(config: RunConfig) -> PreflightCheck:
    """Dispatch the CLI actor's auth-source check by `cli_tool`."""

    if config.cli_tool == "claude":
        return _check_claude_auth(config)
    return _check_codex_auth(config)


def _check_claude_auth(config: RunConfig) -> PreflightCheck:
    """Validate that a claude subscription credential resolves on the host.

    claude never carries a token in-container: the host auth-inject proxy
    (`cli_auth_proxy`) holds it and adds the bearer per request. We confirm a
    credential SOURCE resolves — `CLAUDE_CODE_OAUTH_TOKEN` env (from
    `claude setup-token`), the macOS keychain, or `~/.claude/.credentials.json`
    — not that any specific env var is set. `claude` need not be on the host PATH
    (it's baked into the runtime image).
    """

    from .cli_auth_proxy import AuthProxyError, resolve_claude_token

    try:
        resolve_claude_token()
    except AuthProxyError as exc:
        return _fail("claude_auth", str(exc), {})
    return _pass("claude_auth", "claude subscription credential resolves on the host", {})


def _check_codex_auth(config: RunConfig) -> PreflightCheck:
    """Validate the operator's codex subscription token for CLI actor mode.

    The CLI actor re-seeds a neutered copy of `~/.codex/auth.json` into each
    container; here we confirm the source exists and its access token isn't
    about to expire (codex would then try — and, with the neutered token,
    fail — to refresh mid-run). The caller only runs this when codex is an active
    CLI tool (agent or SIM), so it no longer gates on `config.cli_tool` itself.
    """

    from .cli_actor import _access_token_exp

    auth = Path.home() / ".codex" / "auth.json"
    if not auth.exists():
        return _fail("codex_auth", f"{auth} not found; run codex once to log in", {})
    exp = _access_token_exp(auth)
    if exp is None:
        return _warn("codex_auth", "codex access token present (opaque; expiry unknown)", {})
    remaining = exp - int(time.time())
    if remaining <= 24 * 3600:
        # WARN, not FAIL: a near-expiry (or expired) token is RECOVERABLE — the
        # runner host-refreshes it in `CodexDriver.prepare_home` before each turn
        # (real refresh_token, host-side, never in a container) and hard-fails
        # there if it can't renew. Failing preflight would abort the run before
        # that refresh ever runs (preflight precedes actor setup), making the
        # recovery path unreachable.
        return _warn(
            "codex_auth",
            f"codex access token expires in ~{remaining // 3600}h; the runner will "
            "attempt a host-side refresh before launch (run `codex login` if it can't).",
            {"remaining_s": remaining},
        )
    return _pass(
        "codex_auth",
        f"codex subscription token present and valid (~{remaining // 3600}h left)",
        {"remaining_s": remaining},
    )


def _check_openrouter_key(config: RunConfig) -> PreflightCheck:
    if config.skip_openrouter_key_check:
        return _warn("openrouter_key", "OpenRouter key check skipped", {})
    # Free OpenCode Zen models (opencode/*) don't need a key — the opencode
    # binary has built-in access — and a CLI role (codex/claude) ignores its
    # opencode-namespaced model entirely. Decide on the SELECTED models first:
    # when none is a non-Zen slug, the key is irrelevant to this run, so don't
    # hit the network to verify it (a present-but-unused key, or a verification
    # failure behind a corporate/self-signed-cert proxy, must not block a
    # Zen-only or pure-CLI run).
    selected = [m for m in (config.agent_model, config.sim_model) if m]
    non_free = [m for m in selected if not is_zen_model(m)]
    key = os.environ.get(config.openrouter_env_var)
    if not non_free:
        note = "present but unused" if key else "not set"
        return _pass(
            "openrouter_key",
            f"no non-free model selected; OpenRouter key {note} (free OpenCode Zen / CLI only)",
            {"models": selected},
        )
    if not key:
        return _fail(
            "openrouter_key",
            f"{config.openrouter_env_var} is not set and non-free model(s) selected: "
            + ", ".join(non_free),
            {"non_free_models": non_free},
        )
    try:
        info = _fetch_openrouter_key(config.openrouter_key_url, key)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return _fail("openrouter_key", f"could not verify OpenRouter key: {exc}", {})
    data = info.get("data") if isinstance(info, dict) else None
    if not isinstance(data, dict):
        return _fail(
            "openrouter_key",
            "OpenRouter key response did not include data object",
            {"response": info},
        )

    limit = data.get("limit")
    remaining = data.get("limit_remaining")
    include_byok = data.get("include_byok_in_limit")
    if limit is None or remaining is None:
        if config.allow_unlimited_openrouter_key:
            return _warn(
                "openrouter_key",
                "OpenRouter key is unlimited; explicit bypass supplied",
                _safe_key_details(data),
            )
        return _fail(
            "openrouter_key",
            "OpenRouter key has no provider-side credit limit",
            _safe_key_details(data),
        )
    if not isinstance(remaining, (int, float)):
        return _fail(
            "openrouter_key", "OpenRouter limit_remaining is not numeric", _safe_key_details(data)
        )
    if remaining <= 0:
        return _fail(
            "openrouter_key",
            "OpenRouter key has no remaining limited credit",
            _safe_key_details(data),
        )
    if remaining > config.caps.max_cost_usd:
        # Provider-side limit is the anti-runaway backstop; orchestrator
        # --max-cost-usd is the per-run budget. The two layers don't have to
        # be ordered — they enforce different things. Warn so operators can
        # see the gap, but don't fail.
        return _warn(
            "openrouter_key",
            "OpenRouter remaining key limit exceeds Contremaitre max cost cap "
            "(orchestrator cap is the per-run limit; provider limit is the daily backstop)",
            {**_safe_key_details(data), "max_cost_usd": config.caps.max_cost_usd},
        )
    if include_byok is False:
        return _warn(
            "openrouter_key",
            "OpenRouter key limit excludes BYOK usage; safe for non-BYOK models only",
            _safe_key_details(data),
        )
    return _pass(
        "openrouter_key", "OpenRouter key has a bounded remaining limit", _safe_key_details(data)
    )


def _fetch_openrouter_key(url: str, key: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as response:
        payload = response.read().decode("utf-8")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("response is not a JSON object")
    return parsed


def _safe_key_details(data: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "label",
        "limit",
        "limit_reset",
        "limit_remaining",
        "include_byok_in_limit",
        "usage",
        "usage_daily",
        "usage_weekly",
        "usage_monthly",
        "is_free_tier",
    }
    return {key: data.get(key) for key in sorted(allowed) if key in data}


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _proc_details(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
    }


def _pass(name: str, message: str, details: dict[str, Any]) -> PreflightCheck:
    return PreflightCheck(name=name, status="PASS", message=message, details=details)


def _warn(name: str, message: str, details: dict[str, Any]) -> PreflightCheck:
    return PreflightCheck(name=name, status="WARN", message=message, details=details)


def _fail(name: str, message: str, details: dict[str, Any]) -> PreflightCheck:
    return PreflightCheck(name=name, status="FAIL", message=message, details=details)
