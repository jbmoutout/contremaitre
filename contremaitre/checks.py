"""Executable check runner for PR-eval L1."""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

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


def run_checks(worktree: Path, commands: tuple[str, ...], log_path: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for cmd in commands:
        started = time.monotonic()
        proc = subprocess.run(
            shlex.split(cmd),
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=600,
        )
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
        results.append(result)
    return results

