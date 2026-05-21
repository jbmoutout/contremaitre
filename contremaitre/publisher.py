"""Publication boundary.

The orchestrator is the only component allowed to publish. Actor containers do
not receive GitHub credentials, and this module runs only after SIM approval,
diff-hash verification, executable checks, and deterministic diff-scan pass.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .jsonlog import append_jsonl, write_json
from .models import PublishMode, RunConfig, RunPaths


@dataclass(frozen=True)
class PublishResult:
    created: bool
    dry_run: bool
    branch: str
    base: str
    url: str | None
    reason: str


class Publisher:
    def publish(self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str) -> PublishResult:
        raise NotImplementedError


class StubPublisher(Publisher):
    def publish(self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str) -> PublishResult:
        result = PublishResult(
            created=True,
            dry_run=True,
            branch=branch,
            base=config.base,
            url=None,
            reason="publisher stub: would push branch and open a draft PR after approval",
        )
        _write_pr_json(paths, config, result, diff_hash)
        return result


class GhPublisher(Publisher):
    """Host-side GitHub publisher using local git + GitHub CLI."""

    def publish(self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str) -> PublishResult:
        if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
            raise RuntimeError("GITHUB_TOKEN or GH_TOKEN is required for --publish-mode gh")
        if not config.fork:
            raise RuntimeError("--fork is required for --publish-mode gh")

        env = os.environ.copy()
        pr_body = _write_pr_body(paths, config, diff_hash)
        self._run(["git", "push", "origin", f"HEAD:{branch}"], cwd=paths.worktree, paths=paths, env=env)
        cmd = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            config.base,
            "--head",
            branch,
            "--title",
            config.pr_title or f"Contremaitre: {paths.run_id}",
            "--body-file",
            str(pr_body),
        ]
        if config.gh_repo:
            cmd.extend(["--repo", config.gh_repo])
        proc = self._run(cmd, cwd=paths.worktree, paths=paths, env=env)
        url = _extract_url(proc.stdout)
        result = PublishResult(
            created=True,
            dry_run=False,
            branch=branch,
            base=config.base,
            url=url,
            reason="pushed branch and opened draft PR via gh",
        )
        _write_pr_json(paths, config, result, diff_hash)
        return result

    def _run(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        paths: RunPaths,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
        append_jsonl(
            paths.git_log,
            {
                "cmd": cmd,
                "cwd": str(cwd),
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
                "publisher": "gh",
            },
        )
        if proc.returncode != 0:
            raise RuntimeError(f"publisher command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
        return proc


def make_publisher(config: RunConfig) -> Publisher:
    if config.publish_mode == PublishMode.STUB:
        return StubPublisher()
    if config.publish_mode == PublishMode.GH:
        return GhPublisher()
    raise RuntimeError(f"unknown publish mode: {config.publish_mode}")


def write_no_pr(paths: RunPaths, *, reason: str, branch: str | None = None) -> None:
    write_json(
        paths.pr_json,
        {
            "created": False,
            "dry_run": True,
            "branch": branch,
            "url": None,
            "reason": reason,
        },
    )


def _write_pr_json(
    paths: RunPaths,
    config: RunConfig,
    result: PublishResult,
    diff_hash: str,
) -> None:
    write_json(
        paths.pr_json,
        {
            "created": result.created,
            "dry_run": result.dry_run,
            "branch": result.branch,
            "base": result.base,
            "url": result.url,
            "diff_hash": diff_hash,
            "reason": result.reason,
            "publish_mode": config.publish_mode.value,
        },
    )


def _write_pr_body(paths: RunPaths, config: RunConfig, diff_hash: str) -> Path:
    body = paths.run_dir / "pr_body.md"
    if config.pr_body:
        text = config.pr_body
    else:
        text = (
            "Draft PR produced by Contremaitre.\n\n"
            f"- Run id: `{paths.run_id}`\n"
            f"- Diff hash: `{diff_hash}`\n"
            f"- Eval: `eval/pr_eval.md`\n"
        )
    body.write_text(text, encoding="utf-8")
    return body


def _extract_url(stdout: str) -> str | None:
    for token in stdout.split():
        if token.startswith("http://") or token.startswith("https://"):
            return token
    return stdout.strip() or None

