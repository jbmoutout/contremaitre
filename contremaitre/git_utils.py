"""Git command wrapper with durable logging.

Every orchestrator-owned git command should flow through :class:`GitRepo` so
`git_log.jsonl` can explain what happened after the fact. The wrapper avoids
shell execution and injects commit identity for fixture repos that do not have
global git config.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .jsonlog import append_jsonl


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


class GitRepo:
    def __init__(self, cwd: Path, log_path: Path | None = None):
        self.cwd = cwd
        self.log_path = log_path

    def run(self, *args: str, check: bool = True) -> GitResult:
        cmd = ["git", *args]
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "Contremaitre"),
            "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", "contremaitre@example.invalid"),
            "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "Contremaitre"),
            "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", "contremaitre@example.invalid"),
        }
        proc = subprocess.run(
            cmd,
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        result = GitResult(
            args=cmd,
            cwd=self.cwd,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        if self.log_path:
            append_jsonl(
                self.log_path,
                {
                    "cmd": cmd,
                    "cwd": str(self.cwd),
                    "returncode": proc.returncode,
                    "stdout": proc.stdout[-4000:],
                    "stderr": proc.stderr[-4000:],
                },
            )
        if check and proc.returncode != 0:
            raise GitError(f"git command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
        return result

    def output(self, *args: str) -> str:
        return self.run(*args).stdout

    def diff_bytes(self, base: str) -> bytes:
        """`git diff <base>...HEAD` as raw bytes (for byte-exact hashing).

        Can't go through `run()` because that path text-decodes, and the
        diff-hash must be byte-stable for the drift check.
        """

        cmd = ["git", "diff", f"{base}...HEAD"]
        proc = subprocess.run(cmd, cwd=self.cwd, capture_output=True, timeout=120)
        if self.log_path:
            append_jsonl(
                self.log_path,
                {
                    "cmd": cmd,
                    "cwd": str(self.cwd),
                    "returncode": proc.returncode,
                    "stdout_bytes": len(proc.stdout),
                    "stderr": proc.stderr[-4000:].decode("utf-8", errors="replace"),
                },
            )
        if proc.returncode != 0:
            raise GitError(
                f"git command failed ({proc.returncode}): {' '.join(cmd)}\n"
                f"{proc.stderr.decode('utf-8', errors='replace')}"
            )
        return proc.stdout

    def status_porcelain(self) -> str:
        return self.output("status", "--porcelain")


def derive_commit_message(worktree: Path, run_id: str) -> tuple[str, str]:
    """Read SETTLED_DESIGN.md and turn it into (commit title, commit body).

    Title: first non-empty line, stripped of ``# `` and any "Settled design — "
    prefix the skill tends to emit. Falls back to a run-id-tagged generic
    when SETTLED is missing or empty.

    Body: the full SETTLED text + a trailer with the run id, so the commit
    is self-contained for anyone reading ``git log`` later.
    """

    SETTLED_RELPATH = Path(".contremaitre") / "SETTLED_DESIGN.md"
    fallback_title = f"Contremaitre refactor ({run_id})"
    settled = worktree / SETTLED_RELPATH
    if not settled.exists():
        return fallback_title, f"Run: {run_id}\n"
    text = settled.read_text(encoding="utf-8").strip()
    if not text:
        return fallback_title, f"Run: {run_id}\n"
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    title = first_line.lstrip("#").strip()
    for prefix in ("Settled design — ", "Settled design - ", "Settled design: "):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()
            break
    if not title:
        title = fallback_title
    body = f"{text}\n\n---\nRun: {run_id}\n"
    return title, body

