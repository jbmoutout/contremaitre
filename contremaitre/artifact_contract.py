"""Artifact paths and commit-message derivation shared by orchestrator and publisher.

Extracted from orchestrator.py to break the circular import (orchestrator
imports publisher; publisher lazily imported orchestrator's private helpers
and constants). This module has zero dependencies on other contremaitre
modules — only pathlib.
"""

from __future__ import annotations

from pathlib import Path


SETTLED_RELPATH = Path(".contremaitre") / "SETTLED_DESIGN.md"
IMPLEMENTATION_COMPLETE_RELPATH = Path(".contremaitre") / "IMPLEMENTATION_COMPLETE"


def only_contremaitre_changes(porcelain: str) -> bool:
    """True iff every `git status --porcelain` row is orchestration-internal.

    Files excluded from commits by pathspec (``.contremaitre/*``,
    ``opencode.json``) are deliberately untracked in the worktree. The
    host-commit step and the clean-worktree hard gate both need to treat
    a worktree whose only changes are in these paths as "clean for our
    purposes":

    - host-commit: skip instead of producing an empty PR.
    - clean-worktree gate: pass.

    Empty porcelain (no changes at all) is also "clean".
    """

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


def derive_commit_message(worktree: Path, run_id: str) -> tuple[str, str]:
    """Read SETTLED_DESIGN.md and turn it into (commit title, commit body).

    Title: first non-empty line, stripped of ``# `` and any "Settled design — "
    prefix the skill tends to emit. Falls back to a run-id-tagged generic
    when SETTLED is missing or empty (shouldn't happen post-WORK since the
    orchestrator gates on it, but the host commit must never fail here).
    Body: the full SETTLED text + a trailer with the run id, so the commit
    is self-contained for anyone reading ``git log`` later.
    """

    settled = worktree / SETTLED_RELPATH
    fallback_title = f"Contremaitre refactor ({run_id})"
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
