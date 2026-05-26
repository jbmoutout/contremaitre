"""SETTLED_DESIGN.md reading for orchestrator and publisher.

The two constants and the reader function that both the orchestrator (for the
host commit message) and the publisher (for the PR body) need to pull title +
body from .contremaitre/SETTLED_DESIGN.md. Lives here instead of in either
consumer so the publication boundary stays one-directional.
"""

from __future__ import annotations

from pathlib import Path

SETTLED_RELPATH = Path(".contremaitre") / "SETTLED_DESIGN.md"
IMPLEMENTATION_COMPLETE_RELPATH = Path(".contremaitre") / "IMPLEMENTATION_COMPLETE"


def read_settled_design(worktree: Path, run_id: str) -> tuple[str, str]:
    """Read SETTLED_DESIGN.md and return (title, body).

    Title is the first non-empty line, stripped of ``# `` and any
    "Settled design — " prefix the skill tends to emit. Falls back to a
    run-id-tagged generic when the file is missing or empty.

    Body is the full SETTLED text with NO trailer appended — each call
    site appends its own footer.
    """

    settled = worktree / SETTLED_RELPATH
    fallback_title = f"Contremaitre refactor ({run_id})"
    if not settled.exists():
        return fallback_title, ""
    text = settled.read_text(encoding="utf-8").strip()
    if not text:
        return fallback_title, ""
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    title = first_line.lstrip("#").strip()
    for prefix in ("Settled design — ", "Settled design - ", "Settled design: "):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix) :].strip()
            break
    if not title:
        title = fallback_title
    return title, text
