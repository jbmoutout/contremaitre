"""Container lifecycle management for the actor runtime.

Extracted from `actors.py` to separate the "how to run a container and get
its logs" concern from the "what to say to the actor" protocol concern.

The module name describes the abstraction (running a container), not the
implementation (Docker). If the runtime ever swaps to podman / nerdctl the
import path stays meaningful.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .jsonlog import count_text_events, read_events


QUOTA_ERROR_MARKERS = ("FreeUsageLimitError",)


def redact_command(cmd: list[str]) -> list[str]:
    redacted = list(cmd)
    if redacted:
        redacted[-1] = f"<prompt {len(redacted[-1])} chars>"
    return redacted


def build_docker_command(
    *,
    config,
    paths,
    worktree: Path,
    state_dir: Path,
    mount_mode: str,
    model: str,
    prompt: str,
    session_id: str | None,
    role: str,
    extra_mounts: list[tuple[Path, str, str]] | None = None,
) -> tuple[list[str], dict[str, str]]:
    from .actors import ActorError

    env = os.environ.copy()
    env_var = config.openrouter_env_var
    if env_var not in env:
        raise ActorError(f"{env_var} is required for opencode actor mode")
    proxy_vars: list[str] = []
    if config.http_proxy:
        env["HTTP_PROXY"] = config.http_proxy
        proxy_vars.append("HTTP_PROXY")
    if config.https_proxy:
        env["HTTPS_PROXY"] = config.https_proxy
        proxy_vars.append("HTTPS_PROXY")
    if config.no_proxy:
        env["NO_PROXY"] = config.no_proxy
        proxy_vars.append("NO_PROXY")

    opencode_cmd = [
        "/root/.opencode/bin/opencode",
        "run",
        "--dangerously-skip-permissions",
        "--format",
        "json",
        "--model",
        model,
    ]
    if session_id:
        opencode_cmd.extend(["--session", session_id])
    opencode_cmd.append(prompt)

    cmd = ["docker", "run", "-d"]
    cmd.extend(["--label", f"contremaitre.run-id={paths.run_id}"])
    cmd.extend(["--label", f"contremaitre.role={role}"])
    if config.container_user:
        cmd.extend(["--user", config.container_user])
    if config.docker_network:
        cmd.extend(["--network", config.docker_network])
    cmd.extend(
        [
            "-v",
            f"{paths.run_dir}:/results",
            "-v",
            f"{state_dir}:/root/.local/share/opencode",
            "-v",
            f"{worktree}:/app:{mount_mode}",
        ]
    )
    if config.deps_volume:
        cmd.extend(["-v", f"{config.deps_volume.name}:/app/{config.deps_volume.mount_path}:rw"])
        for key, value in config.deps_volume.runtime_env:
            cmd.extend(["-e", f"{key}={value}"])
    if config.opencode_config:
        cmd.extend(["-v", f"{config.opencode_config}:/app/opencode.json:ro"])
    for host_path, container_path, mode in extra_mounts or []:
        cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
    cmd.extend(["-e", env_var])
    for proxy_var in proxy_vars:
        cmd.extend(["-e", proxy_var])
    cmd.extend(["-w", "/app", config.docker_image, *opencode_cmd])
    return cmd, env


def run_container(
    *,
    cmd: list[str],
    env: dict[str, str],
    stdout_path: Path,
    timeout_seconds: int,
    role: str,
    baseline_text_count: int = 0,
) -> tuple[int, str, str | None]:
    from .actors import ActorError

    create = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if create.returncode != 0:
        raise ActorError(f"{role} docker run -d failed: {create.stderr[:500]}")
    container_id = create.stdout.strip()
    if not container_id:
        raise ActorError(f"{role} docker run -d produced no container id")

    log_proc: subprocess.Popen[bytes] | None = None
    fast_fail_reason: str | None = None
    try:
        with stdout_path.open("ab") as stdout_f:
            log_proc = subprocess.Popen(
                ["docker", "logs", "-f", container_id],
                stdout=stdout_f,
                stderr=subprocess.PIPE,
            )
            wait_proc = subprocess.Popen(
                ["docker", "wait", container_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + timeout_seconds
            poll_interval = 2.0
            try:
                while True:
                    try:
                        wait_proc.wait(timeout=poll_interval)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if time.monotonic() > deadline:
                        subprocess.run(
                            ["docker", "stop", "-t", "5", container_id],
                            capture_output=True,
                            timeout=15,
                        )
                        log_proc.kill()
                        try:
                            wait_proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            wait_proc.kill()
                        raise ActorError(f"{role} opencode timed out after {timeout_seconds}s")
                    fast_fail_reason = detect_provider_quota_exhausted(
                        stdout_path, baseline_text_count
                    )
                    if fast_fail_reason is not None:
                        subprocess.run(
                            ["docker", "stop", "-t", "5", container_id],
                            capture_output=True,
                            timeout=15,
                        )
                        try:
                            wait_proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            wait_proc.kill()
                        break
            finally:
                try:
                    log_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log_proc.kill()
            stderr_bytes = log_proc.stderr.read() if log_proc.stderr else b""
            wait_stdout = wait_proc.stdout.read() if wait_proc.stdout else ""
        returncode = int((wait_stdout or "").strip() or "1")
        return returncode, stderr_bytes.decode("utf-8", errors="replace"), fast_fail_reason
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            timeout=15,
        )


def detect_provider_quota_exhausted(path: Path, baseline_text_count: int) -> str | None:
    if not path.exists():
        return None
    events = read_events(path)
    seen_text = 0
    for event in events:
        if event.get("type") == "text":
            seen_text += 1
            if seen_text > baseline_text_count:
                return None
        if event.get("type") != "error":
            continue
        if seen_text < baseline_text_count:
            continue
        serialized = json.dumps(event, sort_keys=True)
        for marker in QUOTA_ERROR_MARKERS:
            if marker in serialized:
                return marker
    return None
