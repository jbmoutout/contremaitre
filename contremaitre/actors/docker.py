from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .. import events
from ..jsonlog import read_jsonl
from ..models import RunConfig, RunPaths
from .base import ActorError


def build_docker_command(
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


def _run_detached_container(
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
) -> tuple[int, str, str | None]:
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
                        grew = cur_stdout > stall_last_stdout or cur_internal > stall_last_internal
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
                                f"{role} opencode stalled for {stdout_stall_seconds}s "
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
        return returncode, stderr_bytes.decode("utf-8", errors="replace"), fast_fail_reason
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            timeout=15,
        )


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


def _count_text_events(path: Path) -> int:
    return sum(1 for event in read_jsonl(path) if event.get("type") == "text")


def _count_jsonl_events(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _latest_text(path: Path) -> str:
    for event in reversed(read_jsonl(path)):
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    return text
    return ""


def _latest_session_id(path: Path) -> str | None:
    for event in reversed(read_jsonl(path)):
        session = event.get("sessionID") or event.get("session_id")
        if isinstance(session, str):
            return session
    return None


def _latest_error_after_text_count(
    path: Path, baseline_text_count: int, *, events_offset: int = 0
) -> str | None:
    events = read_jsonl(path)
    seen_text = 0
    cutoff_idx = 0
    for i, event in enumerate(events):
        if event.get("type") == "text":
            seen_text += 1
            if seen_text > baseline_text_count:
                cutoff_idx = i
                break
    else:
        cutoff_idx = 0
        seen_text = 0
        for i, event in enumerate(events):
            if event.get("type") == "text":
                seen_text += 1
            if seen_text >= baseline_text_count:
                cutoff_idx = i + 1
                break
    cutoff_idx = max(cutoff_idx, events_offset)
    for event in reversed(events[cutoff_idx:]):
        if event.get("type") == "error":
            return json.dumps(event, sort_keys=True)
    return None
