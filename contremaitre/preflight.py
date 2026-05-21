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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonlog import write_json
from .models import ActorMode, RunConfig, RunPaths


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


def run_preflight(config: RunConfig) -> PreflightReport:
    checks = [_check_repo(config)]
    if config.actor_mode == ActorMode.OPENCODE:
        checks.extend(
            [
                _check_opencode_config(config),
                _check_docker_image(config),
                _check_opencode_binary(config),
                _check_readonly_mount(config),
                _check_network_policy(config),
                _check_openrouter_key(config),
            ]
        )
    passed = all(check.status != "FAIL" for check in checks)
    return PreflightReport(passed=passed, checks=checks)


def enforce_preflight(config: RunConfig, paths: RunPaths) -> None:
    if config.skip_preflight:
        write_json(
            paths.preflight_report,
            {
                "passed": True,
                "skipped": True,
                "reason": "--skip-preflight was set",
            },
        )
        return
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
    base = _run(["git", "-C", str(config.repo), "rev-parse", "--verify", config.base])
    if base.returncode != 0:
        return _fail("repo_base", f"base ref not found: {config.base}", _proc_details(base))
    return _pass("repo", "repo and base ref are available", {"repo": str(config.repo), "base": config.base})


def _check_opencode_config(config: RunConfig) -> PreflightCheck:
    if not config.opencode_config:
        return _fail("opencode_config", "--opencode-config is required for --actor opencode", {})
    if not config.opencode_config.exists():
        return _fail("opencode_config", f"opencode config does not exist: {config.opencode_config}", {})
    return _pass("opencode_config", "opencode config exists", {"path": str(config.opencode_config)})


def _check_docker_image(config: RunConfig) -> PreflightCheck:
    docker = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if docker.returncode != 0:
        return _fail("docker", "docker daemon is not available", _proc_details(docker))
    inspect = _run(["docker", "image", "inspect", config.docker_image, "--format", "{{.Id}} {{.Created}}"])
    if inspect.returncode != 0:
        return _fail("docker_image", f"docker image not found: {config.docker_image}", _proc_details(inspect))
    return _pass("docker_image", "docker image is available", {"image": config.docker_image, "inspect": inspect.stdout.strip()})


def _check_opencode_binary(config: RunConfig) -> PreflightCheck:
    proc = _run(["docker", "run", "--rm", config.docker_image, "/root/.opencode/bin/opencode", "--version"])
    if proc.returncode != 0:
        return _fail("opencode_binary", "opencode binary failed inside image", _proc_details(proc))
    return _pass("opencode_binary", "opencode binary runs inside image", {"version": proc.stdout.strip()})


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


def _check_openrouter_key(config: RunConfig) -> PreflightCheck:
    if config.skip_openrouter_key_check:
        return _warn("openrouter_key", "OpenRouter key check skipped", {})
    key = os.environ.get(config.openrouter_env_var)
    if not key:
        return _fail("openrouter_key", f"{config.openrouter_env_var} is not set", {})
    try:
        info = _fetch_openrouter_key(config.openrouter_key_url, key)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return _fail("openrouter_key", f"could not verify OpenRouter key: {exc}", {})
    data = info.get("data") if isinstance(info, dict) else None
    if not isinstance(data, dict):
        return _fail("openrouter_key", "OpenRouter key response did not include data object", {"response": info})

    limit = data.get("limit")
    remaining = data.get("limit_remaining")
    include_byok = data.get("include_byok_in_limit")
    if limit is None or remaining is None:
        if config.allow_unlimited_openrouter_key:
            return _warn("openrouter_key", "OpenRouter key is unlimited; explicit bypass supplied", _safe_key_details(data))
        return _fail("openrouter_key", "OpenRouter key has no provider-side credit limit", _safe_key_details(data))
    if not isinstance(remaining, (int, float)):
        return _fail("openrouter_key", "OpenRouter limit_remaining is not numeric", _safe_key_details(data))
    if remaining <= 0:
        return _fail("openrouter_key", "OpenRouter key has no remaining limited credit", _safe_key_details(data))
    if remaining > config.caps.max_cost_usd and not config.allow_openrouter_limit_above_cap:
        return _fail(
            "openrouter_key",
            "OpenRouter remaining key limit exceeds Contremaitre max cost cap",
            {**_safe_key_details(data), "max_cost_usd": config.caps.max_cost_usd},
        )
    if include_byok is False:
        return _warn(
            "openrouter_key",
            "OpenRouter key limit excludes BYOK usage; safe for non-BYOK models only",
            _safe_key_details(data),
        )
    return _pass("openrouter_key", "OpenRouter key has a bounded remaining limit", _safe_key_details(data))


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
