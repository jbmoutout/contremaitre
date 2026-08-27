"""Container lifecycle for Docker-based actor runners.

The single module that owns container launch, log streaming, wait, stall
detection, wall-clock timeout, cleanup, and fast-fail marker scanning.
Domain logic (config assembly, event parsing, transcript writing) stays
on the runner classes — this module has no opinion on what runs inside.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from . import events
from .jsonlog import read_jsonl
from .models import RunConfig, RunPaths, is_zen_model
from .runtime_image import deps_mount_mode


# ---- result types -----------------------------------------------------------


@dataclass(frozen=True)
class ContainerResult:
    """Outcome of one `run_detached` call."""

    returncode: int
    stderr: str
    fast_fail_reason: str | None


# ---- seam (ABC) -------------------------------------------------------------


class ContainerLifecycle(ABC):
    """Container lifecycle — two substitutable methods."""

    @abstractmethod
    def build_argv(
        self,
        *,
        config: RunConfig,
        paths: RunPaths,
        worktree: Path,
        state_dir: Path,
        mount_mode: str,
        model: str,
        prompt: str,
        session_id: str | None,
        role: str,
        extra_mounts: list[tuple[Path, str, str]] | None = None,
    ) -> tuple[list[str], dict[str, str]]: ...

    @abstractmethod
    def run_detached(
        self,
        *,
        cmd: list[str],
        env: dict[str, str],
        stdout_path: Path,
        timeout_seconds: int,
        role: str,
        baseline_text_count: int = 0,
        state_dir: Path | None = None,
        stdout_stall_seconds: int | None = None,
        events_offset: int = 0,
    ) -> ContainerResult: ...


# ---- real implementation (Docker subprocess) --------------------------------


class DockerContainerLifecycle(ContainerLifecycle):
    """Real Docker subprocess lifecycle — launches, streams, waits, cleans up."""

    def build_argv(
        self,
        *,
        config: RunConfig,
        paths: RunPaths,
        worktree: Path,
        state_dir: Path,
        mount_mode: str,
        model: str,
        prompt: str,
        session_id: str | None,
        role: str,
        extra_mounts: list[tuple[Path, str, str]] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        env = os.environ.copy()
        env_var = config.openrouter_env_var
        if not is_zen_model(model) and env_var not in env:
            from .actors import ActorError

            raise ActorError(f"{env_var} is required for opencode model {model!r}")
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
        deps_mode = deps_mount_mode(role, mount_mode)
        if config.deps_volume and deps_mode:
            d = config.deps_volume
            cmd.extend(["-v", f"{d.name}:/app/{d.mount_path}:{deps_mode}"])
            for key, value in d.runtime_env:
                cmd.extend(["-e", f"{key}={value}"])
        if config.opencode_config:
            if mount_mode == "ro":
                (worktree / "opencode.json").touch(exist_ok=True)
            cmd.extend(["-v", f"{config.opencode_config}:/app/opencode.json:ro"])
        for host_path, container_path, mode in extra_mounts or []:
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
        cmd.extend(["-e", env_var])
        for proxy_var in proxy_vars:
            cmd.extend(["-e", proxy_var])
        cmd.extend(["-w", "/app", config.docker_image, *opencode_cmd])
        return cmd, env

    def run_detached(
        self,
        *,
        cmd: list[str],
        env: dict[str, str],
        stdout_path: Path,
        timeout_seconds: int,
        role: str,
        baseline_text_count: int = 0,
        state_dir: Path | None = None,
        stdout_stall_seconds: int | None = None,
        events_offset: int = 0,
    ) -> ContainerResult:
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
                stall_last_stdout = stdout_path.stat().st_size if stdout_path.exists() else 0
                stall_last_internal = _latest_internal_log_size(state_dir)
                stall_last_growth_at = time.monotonic()
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
                        if stdout_stall_seconds is not None:
                            cur_stdout = (
                                stdout_path.stat().st_size
                                if stdout_path.exists()
                                else stall_last_stdout
                            )
                            cur_internal = _latest_internal_log_size(state_dir)
                            grew = (
                                cur_stdout > stall_last_stdout or cur_internal > stall_last_internal
                            )
                            if grew:
                                stall_last_stdout = cur_stdout
                                stall_last_internal = cur_internal
                                stall_last_growth_at = time.monotonic()
                            elif time.monotonic() - stall_last_growth_at > stdout_stall_seconds:
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
                                raise ActorError(
                                    f"{role} opencode stalled for "
                                    f"{stdout_stall_seconds}s "
                                    f"(no stdout or internal-log activity)",
                                    kind=events.OPENCODE_STALL,
                                )
                        fast_fail_reason = _detect_provider_fast_fail(
                            stdout_path,
                            baseline_text_count,
                            state_dir=state_dir,
                            events_offset=events_offset,
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
            if fast_fail_reason is None:
                fast_fail_reason = _detect_provider_fast_fail(
                    stdout_path,
                    baseline_text_count,
                    state_dir=state_dir,
                    events_offset=events_offset,
                )
            return ContainerResult(
                returncode=returncode,
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                fast_fail_reason=fast_fail_reason,
            )
        finally:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_id],
                    capture_output=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass


# ---- fast-fail detection (moved from actors.py) -----------------------------

_QUOTA_ERROR_MARKERS = ("FreeUsageLimitError",)

_PROVIDER_TRANSIENT_ERROR_MARKERS = (
    "Provider returned error",
    "Upstream idle timeout exceeded",
)

_FAST_FAIL_MARKERS = _QUOTA_ERROR_MARKERS + _PROVIDER_TRANSIENT_ERROR_MARKERS


def _classify_fast_fail_marker(marker: str) -> str:
    if any(m in marker for m in _QUOTA_ERROR_MARKERS):
        return events.PROVIDER_QUOTA_EXHAUSTED
    return events.PROVIDER_TRANSIENT_ERROR


def _latest_internal_log_size(state_dir: Path | None) -> int:
    if state_dir is None:
        return 0
    log_dir = state_dir / "log"
    if not log_dir.exists():
        return 0
    try:
        candidates = [p for p in log_dir.iterdir() if p.is_file()]
    except OSError:
        return 0
    if not candidates:
        return 0
    try:
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        return latest.stat().st_size
    except OSError:
        return 0


def _detect_provider_fast_fail(
    path: Path,
    baseline_text_count: int,
    *,
    state_dir: Path | None = None,
    events_offset: int = 0,
) -> str | None:
    if path.exists():
        stream = read_jsonl(path)
        seen_text = 0
        for i, event in enumerate(stream):
            if event.get("type") == "text":
                seen_text += 1
                if seen_text > baseline_text_count:
                    return None
            if event.get("type") != "error":
                continue
            if seen_text < baseline_text_count:
                continue
            if i < events_offset:
                continue
            serialized = json.dumps(event, sort_keys=True)
            for marker in _FAST_FAIL_MARKERS:
                if marker in serialized:
                    return marker

    if state_dir is not None:
        marker = _scan_opencode_log_for_marker(state_dir)
        if marker is not None:
            return marker

    return None


def _scan_opencode_log_for_marker(state_dir: Path) -> str | None:
    log_dir = state_dir / "log"
    if not log_dir.exists():
        return None
    try:
        candidates = [p for p in log_dir.iterdir() if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    try:
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for marker in _FAST_FAIL_MARKERS:
        if marker in text:
            return marker
    return None


def redact_command(cmd: list[str]) -> list[str]:
    redacted = list(cmd)
    if redacted:
        redacted[-1] = f"<prompt {len(redacted[-1])} chars>"
    return redacted
