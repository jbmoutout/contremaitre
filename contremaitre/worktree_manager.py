from __future__ import annotations

import shutil
import subprocess as _sp
from pathlib import Path

from . import events
from .git_utils import GitRepo
from .jsonlog import append_jsonl
from .models import RunConfig, RunPaths

SETTLED_RELPATH = Path(".contremaitre") / "SETTLED_DESIGN.md"
IMPLEMENTATION_COMPLETE_RELPATH = Path(".contremaitre") / "IMPLEMENTATION_COMPLETE"

_HOST_COMMIT_EXCLUDES = (
    ".contremaitre",
    "opencode.json",
    "dist",
    "build",
    "out",
    ".next",
    "__pycache__",
)


def _derive_commit_message(worktree: Path, run_id: str) -> tuple[str, str]:
    settled = worktree / SETTLED_RELPATH
    fallback_title = f"Contremaitre refactor ({run_id})"
    if not settled.exists():
        return fallback_title, f"Run: {run_id}\n"
    text = settled.read_text(encoding="utf-8").strip()
    if not text:
        return fallback_title, f"Run: {run_id}\n"
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    title = first_line.lstrip("#").strip()
    for prefix in ("Settled design \u2014 ", "Settled design - ", "Settled design: "):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()
            break
    if not title:
        title = fallback_title
    body = f"{text}\n\n---\nRun: {run_id}\n"
    return title, body


def _only_contremaitre_changes(porcelain: str) -> bool:
    _INTERNAL_PREFIXES = (
        ".contremaitre/", ".contremaitre",
        "opencode.json",
        "dist/", "build/", "out/", ".next/",
        "__pycache__/",
    )
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if not any(path == p or path.startswith(p) for p in _INTERNAL_PREFIXES):
            return False
    return True


def _is_gitignored(repo: GitRepo, path: str) -> bool:
    return repo.run("check-ignore", "-q", "--", path, check=False).returncode == 0


class WorktreeManager:
    def __init__(self, config: RunConfig, paths: RunPaths, emit):
        self.config = config
        self.paths = paths
        self.emit = emit

    def create(self, repo: GitRepo, branch: str) -> str:
        if self.paths.worktree.exists():
            if self.paths.worktree.name.startswith("contremaitre-"):
                shutil.rmtree(self.paths.worktree)
            else:
                raise RuntimeError(f"refusing to remove non-Contremaitre path: {self.paths.worktree}")
        source_url = self.config.upstream or self.config.fork
        if source_url:
            repo.run("remote", "set-url", "origin", source_url, check=False)
        repo.run("fetch", "origin", self.config.base)
        base_ref = f"origin/{self.config.base}"
        base_sha = repo.run("rev-parse", base_ref).stdout.strip()
        repo.run("worktree", "add", str(self.paths.worktree), "-b", branch, base_ref)
        worktree_git = GitRepo(self.paths.worktree, self.paths.git_log)
        if self.config.fork:
            worktree_git.run("remote", "remove", "origin", check=False)
            worktree_git.run("remote", "add", "origin", self.config.fork)
        if self.config.upstream:
            worktree_git.run("remote", "remove", "upstream", check=False)
            worktree_git.run("remote", "add", "upstream", self.config.upstream)
        return base_sha

    def snapshot(self, repo: GitRepo, diff_base: str) -> tuple[str, str]:
        status = repo.run("status", "--porcelain", check=False).stdout
        diff_stat = repo.run("diff", "--stat", f"{diff_base}...HEAD", check=False).stdout
        return status, diff_stat

    def record_worktree_state(self, repo: GitRepo, label: str, diff_base: str) -> tuple[str, str]:
        status, diff_stat = self.snapshot(repo, diff_base)
        append_jsonl(
            self.paths.worktree_state,
            {"label": label, "status": status, "diff_stat": diff_stat},
        )
        return status, diff_stat

    def commit_agent_changes(self, repo: GitRepo) -> None:
        if _only_contremaitre_changes(repo.status_porcelain()):
            self.emit(events.HOST_COMMIT_SKIPPED, reason="worktree clean")
            return
        title, body = _derive_commit_message(self.paths.worktree, self.paths.run_id)
        excludes = [
            f":(exclude){path}"
            for path in _HOST_COMMIT_EXCLUDES
            if not _is_gitignored(repo, path)
        ]
        repo.run("add", "--", ".", *excludes)
        repo.run("commit", "-m", title, "-m", body)
        self.emit(
            events.HOST_COMMIT_CREATED,
            reason="actor left worktree changes for orchestrator-owned git boundary",
            title=title,
        )

    def commit_drift(self, repo: GitRepo) -> None:
        drift = self.paths.worktree / ".contremaitre" / "drift_after_approval.txt"
        drift.parent.mkdir(parents=True, exist_ok=True)
        drift.write_text("committed after approval to force diff-hash mismatch\n", encoding="utf-8")
        repo.run("add", str(drift.relative_to(self.paths.worktree)))
        repo.run("commit", "-m", "Simulate drift after approval")
        self.emit(events.SIMULATED_DIFF_DRIFT)

    def cleanup(self, repo: GitRepo) -> None:
        if not self.paths.worktree.name.startswith("contremaitre-"):
            return
        self._stop_run_containers()
        self._remove_run_volumes()
        worktree_existed = self.paths.worktree.exists()
        if worktree_existed:
            repo.run("worktree", "remove", "--force", str(self.paths.worktree), check=False)
        if self.paths.worktree.exists():
            shutil.rmtree(self.paths.worktree)
        repo.run("worktree", "prune", check=False)
        if worktree_existed:
            self.emit(events.WORKTREE_REMOVED, path=str(self.paths.worktree))

    def _remove_run_volumes(self) -> None:
        try:
            ls = _sp.run(
                ["docker", "volume", "ls", "-q", "--filter", f"label=contremaitre.run-id={self.paths.run_id}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, _sp.TimeoutExpired):
            return
        for name in (line for line in ls.stdout.split() if line):
            try:
                _sp.run(["docker", "volume", "rm", "-f", name], capture_output=True, timeout=15)
            except (OSError, _sp.TimeoutExpired):
                continue

    def _stop_run_containers(self) -> None:
        try:
            ps = _sp.run(
                ["docker", "ps", "-q", "--filter", f"label=contremaitre.run-id={self.paths.run_id}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, _sp.TimeoutExpired):
            return
        ids = [line for line in ps.stdout.split() if line]
        for cid in ids:
            try:
                _sp.run(["docker", "stop", "-t", "5", cid], capture_output=True, timeout=15)
            except (OSError, _sp.TimeoutExpired):
                continue
