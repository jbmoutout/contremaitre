"""Scaffold path constants and commit-message derivation.

Shared by the orchestrator (host commit) and the publisher (PR title/body).
Neither module should own these exclusively — they are Contremaitre's
convention, not a concern of the state machine or the publication boundary.
"""

from __future__ import annotations

from pathlib import Path


SETTLED_RELPATH = Path(".contremaitre") / "SETTLED_DESIGN.md"
IMPLEMENTATION_COMPLETE_RELPATH = Path(".contremaitre") / "IMPLEMENTATION_COMPLETE"

# Single canonical source of truth for what counts as "orchestration-internal"
# in the worktree. Two consumers derive from this:
#   - HOST_COMMIT_EXCLUDES: bare names for `:(exclude)` git-add pathspecs
#   - only_contremaitre_changes(): predicate on `git status --porcelain` output
# Updating this tuple is the only place to change when a new internal path is added.
_CONTREMAITRE_INTERNALS: tuple[str, ...] = (
    ".contremaitre",
    "opencode.json",
    "dist",
    "build",
    "out",
    ".next",
    "__pycache__",
)

HOST_COMMIT_EXCLUDES: tuple[str, ...] = _CONTREMAITRE_INTERNALS


def only_contremaitre_changes(porcelain: str) -> bool:
    """True iff every `git status --porcelain` row is orchestration-internal.

    Files excluded from commits by pathspec (`.contremaitre/*`, `opencode.json`)
    are deliberately untracked in the worktree. Both the host-commit step and the
    clean-worktree hard gate need to treat a worktree whose only changes are in
    these paths as "clean for our purposes". Empty porcelain is also clean.
    """
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if not any(path == name or path.startswith(name + "/") for name in _CONTREMAITRE_INTERNALS):
            return False
    return True


def derive_commit_message(worktree: Path, run_id: str) -> tuple[str, str]:
    """Read SETTLED_DESIGN.md and turn it into (commit title, commit body).

    Title: first non-empty line, stripped of `# ` and any "Settled design — "
    prefix the skill tends to emit. Falls back to a run-id-tagged generic
    when SETTLED is missing or empty (shouldn't happen post-WORK since the
    orchestrator gates on it, but the host commit must never fail here).
    Body: the full SETTLED text + a trailer with the run id, so the commit
    is self-contained for anyone reading `git log` later.
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
            title = title[len(prefix) :].strip()
            break
    if not title:
        title = fallback_title
    body = f"{text}\n\n---\nRun: {run_id}\n"
    return title, body
