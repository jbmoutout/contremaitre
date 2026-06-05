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

    # Detached so the container's lifecycle is owned by the docker daemon,
    # not by this python process: terminal close / SIGHUP no longer
    # orphans the run, signal handlers can `docker stop` by label, and
    # we get the container id back on stdout without a cidfile.
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
        # Lockhash-keyed deps volume, RW so the agent can install
        # mid-run when the design genuinely needs a new dep (test
        # framework, lint plugin, etc.). The trade-off: parallel runs
        # against the same lockfile share the volume and can race on
        # writes. Acceptable for solo-operator sequential workflow;
        # revisit if multi-run-in-parallel becomes a real pattern.
        # Mounted over the worktree bind at /app/{mount_path}; the
        # worktree's own copy of that directory (if any) is shadowed.
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
    """Start the detached container, stream its logs, wait for exit.

    Returns `(returncode, stderr, fast_fail_reason)`. `fast_fail_reason` is
    non-None when the container was killed early because a known
    non-retryable error was detected. Two sources are scanned:

    - `stdout_path` (the raw event stream opencode writes to its stdout) —
      catches errors that opencode chose to surface to the caller.
    - `state_dir/log/*.log` (opencode's internal log) — catches errors
      opencode classifies as `isRetryable: true` and silently retries
      internally without ever surfacing them to stdout. The free-tier
      `FreeUsageLimitError` falls into this bucket: opencode will keep
      hammering the API until the docker timeout fires, but the retry
      can't change the outcome (per-day/per-hour user quota).

    On timeout (real wall-clock timeout, not fast-fail): docker stop the
    container, then raise. Same surface the previous Popen-based runner
    had so the orchestrator doesn't need to know about the detached model.
    """

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
            # Stall detection: kill the subprocess if BOTH the stdout
            # stream AND opencode's internal log have been silent for
            # `stdout_stall_seconds`. Watching only stdout would
            # false-positive on subagent dispatches: a `Task` subagent's
            # tool calls and LLM streams stay scoped to the subagent's
            # session and never surface to the parent's `raw_export`.
            # The internal log still records them, so a growing log =
            # opencode is doing something even when stdout looks frozen.
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
        # Final fast-fail scan: opencode can write a stream error to its
        # internal log then exit cleanly (returncode 0) within one poll
        # interval, so the in-loop detector may not have had a chance to
        # fire on the post-error state. The detector's "new text landed
        # after baseline → return None" gate keeps a successful turn from
        # tripping on a recovered-from internal retry marker.
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
# Generic upstream-provider errors (5xx surfaced as a stream error). Distinct
# from quota markers: retry MAY succeed on a different request, so the caller
# raises a transient-error kind that a retry layer can catch.
# Known transient upstream-error messages. Opencode wraps them in its
# `{name: "UnknownError", data: {message: ...}}` envelope and surfaces
# them either as `{type: error}` events in raw_export.jsonl or as ERROR
# lines in its internal log. Substring match against the serialized
# error keeps it cheap; add new strings here as new variants surface.
_PROVIDER_TRANSIENT_ERROR_MARKERS = (
    "Provider returned error",
    "Upstream idle timeout exceeded",
)
_FAST_FAIL_MARKERS = _QUOTA_ERROR_MARKERS + _PROVIDER_TRANSIENT_ERROR_MARKERS


def _classify_fast_fail_marker(marker: str) -> str:
    """Return the events.* kind for a marker matched by _detect_provider_fast_fail."""

    if any(m in marker for m in _QUOTA_ERROR_MARKERS):
        return events.PROVIDER_QUOTA_EXHAUSTED
    return events.PROVIDER_TRANSIENT_ERROR


def _latest_internal_log_size(state_dir: Path | None) -> int:
    """Bytes of `state_dir`'s latest opencode log file, or 0 if absent.

    Used by the stall detector as a second "is opencode doing anything?"
    signal. opencode delegates `Task` calls to subagents that run in
    their own internal session — their tool calls and LLM streams DO
    NOT surface to the parent's stdout (`raw_export.jsonl`). They do
    land in the parent process's opencode log. Watching the log lets us
    distinguish "model is dead" from "subagent is grinding silently".
    """

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
    """Detect fast-fail markers in two places per call.

    1. `path` (raw_export.jsonl) — error events opencode surfaced to stdout.
       Gated by `baseline_text_count` so a recovered transient burst from
       an earlier turn doesn't count: if a real text reply landed after
       baseline, prior errors are stale.

    2. `state_dir/log/*.log` (opencode's internal log; the latest file by
       mtime is this turn's). Catches errors opencode marks `isRetryable`
       and silently retries — `FreeUsageLimitError` (quota; retry can't
       help) and `Provider returned error` (generic upstream 5xx) are the
       canonical cases. Without this, opencode hammers until the docker
       timeout fires.

    Returns the matched marker string (caller passes it to
    `_classify_fast_fail_marker` to decide quota-vs-transient handling), or
    None.
    """

    # Layer 1 — surfaced errors in the JSONL stream.
    if path.exists():
        stream = read_jsonl(path)
        seen_text = 0
        for i, event in enumerate(stream):
            if event.get("type") == "text":
                seen_text += 1
                if seen_text > baseline_text_count:
                    # A real text reply landed — anything before it is stale.
                    return None
            if event.get("type") != "error":
                continue
            if seen_text < baseline_text_count:
                continue
            if i < events_offset:
                # Error came from an earlier retry attempt of this same
                # turn — its outcome has already been handled. Ignore it
                # so attempt N+1 can run without immediately re-detecting
                # attempt N's failure.
                continue
            serialized = json.dumps(event, sort_keys=True)
            for marker in _FAST_FAIL_MARKERS:
                if marker in serialized:
                    return marker

    # Layer 2 — silent retries in opencode's internal log.
    if state_dir is not None:
        marker = _scan_opencode_log_for_marker(state_dir)
        if marker is not None:
            return marker

    return None


def _scan_opencode_log_for_marker(state_dir: Path) -> str | None:
    """Scan the latest opencode log file for any fast-fail marker.

    Each opencode container invocation creates a new log file at
    `state_dir/log/<ISO-timestamp>.log`. We pick the newest by mtime so a
    recovered earlier turn's log doesn't generate a false positive.
    """

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
    """Keep logs useful without exposing long prompts."""

    redacted = list(cmd)
    if redacted:
        redacted[-1] = f"<prompt {len(redacted[-1])} chars>"
    return redacted


def _count_text_events(path: Path) -> int:
    return sum(1 for event in read_jsonl(path) if event.get("type") == "text")


def _count_jsonl_events(path: Path) -> int:
    """Return the number of newline-delimited records in `path` (0 if absent)."""

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
    """Return the latest error event that arrived AFTER the Nth text event.

    Multi-turn streams accumulate errors from old turns. When checking
    whether the *current* turn failed, ignore errors from prior turns.
    `events_offset` further narrows the scan to events at or after a
    given index — used by the retry wrapper so attempt N+1 does not pick
    up attempt N's error from earlier in the same turn.
    """

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
        # No new text event landed; everything after `baseline_text_count` text events counts.
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
