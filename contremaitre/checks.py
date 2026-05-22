"""Executable check runner for PR-eval L1."""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import events
from .jsonlog import append_jsonl


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
    worktree: Path,
    commands: tuple[str, ...],
    log_path: Path,
    emit_event: Callable[..., None] | None = None,
) -> list[CheckResult]:
    """Run executable checks in the worktree.

    `emit_event` is the orchestrator's `_emit` (or None). When supplied, each
    check pair emits `check_started` / `check_completed` to guardrail_events
    so the TUI / a tail of guardrail_events.jsonl shows forward motion even
    when a check runs for the full 600s timeout — closes the silent-stall
    failure mode where a hung check made the whole REVIEW handover look
    frozen.
    """

    results: list[CheckResult] = []
    for index, cmd in enumerate(commands):
        if emit_event is not None:
            emit_event(events.CHECK_STARTED, cmd=cmd, index=index)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                shlex.split(cmd),
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            if emit_event is not None:
                emit_event(
                    events.CHECK_COMPLETED,
                    cmd=cmd,
                    index=index,
                    returncode=None,
                    duration_seconds=round(time.monotonic() - started, 3),
                    timed_out=True,
                )
            raise
        result = CheckResult(
            cmd=cmd,
            returncode=proc.returncode,
            duration_seconds=round(time.monotonic() - started, 3),
            stdout=proc.stdout[-8000:],
            stderr=proc.stderr[-8000:],
        )
        append_jsonl(
            log_path,
            {
                "cmd": result.cmd,
                "returncode": result.returncode,
                "duration_seconds": result.duration_seconds,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
        if emit_event is not None:
            emit_event(
                events.CHECK_COMPLETED,
                cmd=cmd,
                index=index,
                returncode=result.returncode,
                duration_seconds=result.duration_seconds,
                timed_out=False,
            )
        results.append(result)
    return results
