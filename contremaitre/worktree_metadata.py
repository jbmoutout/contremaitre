"""Worktree scaffold metadata: constants and extraction functions for
.contremaitre/ marker files.

Both orchestrator.py and publisher.py import from this module instead of
defining the path constants and metadata functions locally. This eliminates
the publisher→orchestrator reverse import and concentrates all worktree-
metadata logic in one seam.
"""

from __future__ import annotations

from pathlib import Path


SETTLED_RELPATH = Path(".contremaitre") / "SETTLED_DESIGN.md"
IMPLEMENTATION_COMPLETE_RELPATH = Path(".contremaitre") / "IMPLEMENTATION_COMPLETE"


def derive_commit_message(worktree: Path, run_id: str) -> tuple[str, str]:
    """Read SETTLED_DESIGN.md and turn it into (commit title, commit body).

    Title: first non-empty line, stripped of ``# `` and any "Settled design — "
    prefix the skill tends to emit. Falls back to a run-id-tagged generic
    when SETTLED is missing or empty.
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


def read_impl_complete(marker_path: Path) -> str:
    """Return the agent's one-line summary, or "" if the marker is missing."""
    if not marker_path.exists():
        return ""
    try:
        return marker_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
