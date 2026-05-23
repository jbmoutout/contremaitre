"""Executable check runner for PR-eval L1.

In OPENCODE mode each check runs in a one-shot sidecar container that
mounts the agent's worktree + per-run node_modules volume — the host
worktree has no project deps, so running `npx tsc --noEmit` on the host
hangs until the 600s timeout. The sidecar reuses the same image the
agent ran in, so the check sees the same toolchain.

In FAKE mode (fixture smoke runs) the check runs on the host directly:
fake mode never touches docker, and forcing it to here would make the
test suite need a docker daemon.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import events
from .jsonlog import append_jsonl
from .models import ActorMode, RunConfig, RunPaths


@dataclass(frozen=True)
class CheckResult:
    cmd: str
    returncode: int
    duration_seconds: float
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def run_checks(
    *,
    config: RunConfig,
    paths: RunPaths,
    emit_event: Callable[..., None] | None = None,
) -> list[CheckResult]:
    """Run executable checks against the post-implementation worktree.

    `emit_event` is the orchestrator's `_emit` (or None). When supplied,
    each check pair emits `check_started` / `check_completed` to
    guardrail_events so the TUI / a tail of guardrail_events.jsonl shows
    forward motion even when a check runs for the full 600s timeout.
    """

    results: list[CheckResult] = []
    in_container = config.actor_mode == ActorMode.OPENCODE
    for index, cmd in enumerate(config.check_cmds):
        if emit_event is not None:
            emit_event(events.CheckStarted(cmd=cmd, index=index, in_container=in_container))
        started = time.monotonic()
        try:
            if in_container:
                proc = _run_sidecar(cmd, config=config, paths=paths)
            else:
                proc = _run_host(cmd, worktree=paths.worktree)
        except subprocess.TimeoutExpired:
            if emit_event is not None:
                emit_event(events.CheckCompleted(
                    cmd=cmd,
                    index=index,
                    returncode=None,
                    duration_seconds=round(time.monotonic() - started, 3),
                    timed_out=True,
                ))
            raise
        result = CheckResult(
            cmd=cmd,
            returncode=proc.returncode,
            duration_seconds=round(time.monotonic() - started, 3),
            stdout=proc.stdout[-8000:],
            stderr=proc.stderr[-8000:],
        )
        append_jsonl(
            paths.test_runs,
            {
                "cmd": result.cmd,
                "returncode": result.returncode,
                "duration_seconds": result.duration_seconds,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "in_container": in_container,
            },
        )
        if emit_event is not None:
            # On failure, include the head of stdout so the TUI's activity
            # panel surfaces the actual error without the operator having
            # to grep test_runs.jsonl. Full output is always in that file.
            stdout_head: str | None = None
            if result.returncode != 0:
                head = "\n".join((result.stdout or result.stderr or "").splitlines()[:8])
                if head:
                    stdout_head = head
            emit_event(events.CheckCompleted(
                cmd=cmd,
                index=index,
                returncode=result.returncode,
                duration_seconds=result.duration_seconds,
                timed_out=False,
                stdout_head=stdout_head,
            ))
        results.append(result)
    return results


def _run_host(cmd: str, *, worktree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        shlex.split(cmd),
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _run_sidecar(
    cmd: str,
    *,
    config: RunConfig,
    paths: RunPaths,
) -> subprocess.CompletedProcess[str]:
    docker_cmd = [
        "docker", "run", "--rm",
        "--label", f"contremaitre.run-id={paths.run_id}",
        "--label", "contremaitre.role=check",
    ]
    if config.container_user:
        docker_cmd.extend(["--user", config.container_user])
    if config.docker_network:
        docker_cmd.extend(["--network", config.docker_network])
    docker_cmd.extend(["-v", f"{paths.worktree}:/app:rw"])
    if config.deps_volume:
        # RW so a check that needs to install something (rare but real)
        # doesn't hit EACCES. Matches the agent-side mount mode.
        docker_cmd.extend(["-v", f"{config.deps_volume}:/app/node_modules:rw"])
    docker_cmd.extend(["-w", "/app", config.docker_image, "sh", "-lc", cmd])
    return subprocess.run(
        docker_cmd,
        capture_output=True,
        text=True,
        timeout=600,
    )
