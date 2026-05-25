"""Commit-message formatting from SETTLED_DESIGN.md.

Extracted from orchestrator.py so the publisher can import it at module
level without creating a circular coupling.
"""

from __future__ import annotations

from pathlib import Path

SETTLED_RELPATH = Path(".contremaitre") / "SETTLED_DESIGN.md"


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
