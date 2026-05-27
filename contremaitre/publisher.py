"""Publication boundary.

The orchestrator is the only component allowed to publish. Actor containers do
not receive GitHub credentials, and this module runs only after SIM approval,
diff-hash verification, executable checks, and deterministic diff-scan pass.

`PublishOutcome` is the single tagged result type for every terminal of the
state machine — PUBLISHED, BLOCKED, NO_PR — and `record_publication` is the
one writer of `pr.json`. Schema drift between the published and not-published
paths is structurally impossible.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .jsonlog import append_jsonl, write_json
from .models import PublishMode, RunConfig, RunPaths
from .pr_metadata import derive_pr_metadata


class PublishOutcomeKind(str, Enum):
    PUBLISHED = "PUBLISHED"  # Publisher ran. May be dry-run (stub) or real (gh).
    BLOCKED = "BLOCKED"      # Hard gate or executable check refused publication.
    NO_PR = "NO_PR"          # Run ended before publication was attempted.


@dataclass(frozen=True)
class PublishOutcome:
    """Tagged result for every terminal of the publication path.

    Two hash fields by design:
      - `approved_diff_hash`: what the SIM signed off on. Stable across the
        BLOCKED-on-drift case so we can still see what was approved.
      - `current_diff_hash`: what the worktree contains right now. Differs from
        `approved_diff_hash` only on a drift block — that's the whole signal
        that drift happened, and it would be lost if we collapsed the two
        into one field.

    For PUBLISHED outcomes the two are equal by definition (drift check passed).
    For NO_PR outcomes both are None (no diff was reviewed).
    """

    kind: PublishOutcomeKind
    base: str
    publish_mode: PublishMode
    reason: str
    branch: str | None = None
    url: str | None = None
    approved_diff_hash: str | None = None
    current_diff_hash: str | None = None
    dry_run: bool = True  # True for stub or for non-PUBLISHED kinds; False only when gh actually opened a PR.
    # PR title as passed to `gh pr create --title` (or what would have been
    # passed in stub mode). None for non-PUBLISHED outcomes. Exposed in
    # pr.json so downstream readers (TUI footer, viewer) can render it
    # without re-parsing SETTLED_DESIGN.md.
    title: str | None = None


def record_publication(paths: RunPaths, outcome: PublishOutcome) -> None:
    """Write the single canonical pr.json row for this run."""

    write_json(
        paths.pr_json,
        {
            "kind": outcome.kind.value,
            "branch": outcome.branch,
            "base": outcome.base,
            "url": outcome.url,
            "approved_diff_hash": outcome.approved_diff_hash,
            "current_diff_hash": outcome.current_diff_hash,
            "reason": outcome.reason,
            "publish_mode": outcome.publish_mode.value,
            "dry_run": outcome.dry_run,
            "title": outcome.title,
        },
    )


class Publisher:
    def publish(self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str) -> PublishOutcome:
        raise NotImplementedError


class StubPublisher(Publisher):
    def publish(self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str) -> PublishOutcome:
        # PUBLISHED implies the drift check passed, so approved == current.
        # Derive title even in stub mode so pr.json carries the same shape
        # as real publishes (and the schema lock test holds).
        derived_title, _ = derive_pr_metadata(paths, diff_hash)
        outcome = PublishOutcome(
            kind=PublishOutcomeKind.PUBLISHED,
            base=config.base,
            publish_mode=config.publish_mode,
            reason="publisher stub: would push branch and open a draft PR after approval",
            branch=branch,
            url=None,
            approved_diff_hash=diff_hash,
            current_diff_hash=diff_hash,
            dry_run=True,
            title=config.pr_title or derived_title,
        )
        record_publication(paths, outcome)
        return outcome


class GhPublisher(Publisher):
    """Host-side GitHub publisher using local git + GitHub CLI."""

    def publish(self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str) -> PublishOutcome:
        if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
            raise RuntimeError("GITHUB_TOKEN or GH_TOKEN is required for --publish-mode gh")
        if not config.fork:
            raise RuntimeError("--fork is required for --publish-mode gh")

        env = os.environ.copy()
        derived_title, derived_body = derive_pr_metadata(paths, diff_hash)
        pr_body = _write_pr_body(paths, config, derived_body)
        final_title = config.pr_title or derived_title
        self._run(["git", "push", "origin", f"HEAD:{branch}"], cwd=paths.worktree, paths=paths, env=env)
        cmd = [
            "gh", "pr", "create",
            "--draft",
            "--base", config.base,
            "--head", branch,
            "--title", final_title,
            "--body-file", str(pr_body),
        ]
        if config.gh_repo:
            cmd.extend(["--repo", config.gh_repo])
        proc = self._run(cmd, cwd=paths.worktree, paths=paths, env=env)
        outcome = PublishOutcome(
            kind=PublishOutcomeKind.PUBLISHED,
            base=config.base,
            publish_mode=config.publish_mode,
            reason="pushed branch and opened draft PR via gh",
            branch=branch,
            url=_extract_url(proc.stdout),
            approved_diff_hash=diff_hash,
            current_diff_hash=diff_hash,
            dry_run=False,
            title=final_title,
        )
        record_publication(paths, outcome)
        return outcome

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


def _write_pr_body(paths: RunPaths, config: RunConfig, derived_body: str) -> Path:
    body = paths.run_dir / "pr_body.md"
    text = config.pr_body or derived_body
    body.write_text(text, encoding="utf-8")
    return body








def _extract_url(stdout: str) -> str | None:
    for token in stdout.split():
        if token.startswith("http://") or token.startswith("https://"):
            return token
    return stdout.strip() or None
