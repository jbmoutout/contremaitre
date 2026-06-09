"""Shared detached-container lifecycle and actor types.

Module-internal (`_`-prefix file = not CLI-public). Two callers share the
container runner on this side of the seam — actors.py (opencode path) and
cli_actor.py (CLI tool path).

Seam rule: what `run_detached`'s call graph needs moves here. SQLite recovery
and ActorOutput assembly stay caller-side because they're opencode-specific.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import events
from .jsonlog import read_jsonl


class ActorError(RuntimeError):
    """Generic actor failure surface.

    `kind` is a free-form tag that lets callers (and the TUI) distinguish
    failure modes worth a custom label without parsing the message string.
    Defaults to `None` for legacy errors that don't classify themselves.
    """

    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ActorOutput:
    """One turn's text reply, after the actor has logged itself.

    Adapters own raw_export.jsonl + transcript.md writes for their own turns.
    The orchestrator just consumes `text` and moves on. No bool field telling
    the caller "did I log for you?" — that was a leaking-abstraction marker.
    """

    text: str
    stderr: str = ""
    returncode: int = 0


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


def run_detached(
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
        # `docker rm -f` on a container that is still stopping (SIGTERM sent but
        # not yet dead) can block until the kernel delivers SIGKILL and the
        # container exits — empirically up to several minutes after a long run.
        # Use a generous timeout and swallow any failure so that (a) the cleanup
        # attempt is as thorough as possible and (b) a timeout here never escapes
        # the finally block and replaces the original exception (the bug that left
        # container afae7f275dd0 stranded after a 1800s timeout hit).
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run already killed its child on TimeoutExpired; the
            # container will be stale on the host — the caller's infra_failure
            # path handles reporting.
            pass
        except Exception:
            pass
