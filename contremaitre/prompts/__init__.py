"""Contremaitre prompts.

All prompts live as markdown files in this directory so they can be tweaked
without touching Python. This module loads them at import time and exposes
them as module-level constants.

Files:
  - `initial_prompt.md`        — sent to the agent on turn 1 of the WORK session.
  - `sim_tooled_persona.md`    — sent to the SIM on its first turn of the WORK session.
  - `sim_review_prompt.md`     — sent to the SIM in the single-shot REVIEW pass.
  - `cli_reviewer_prompt.md`   — sent to the local CLI reviewer (claude / codex)
                                 post-publish; takes a `{pr_url}` placeholder.

The agent and SIM share one multi-turn opencode session each (re-using
session IDs across turns), so first-turn prompts are baked into session memory
and not re-sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..verdicts import ParsedVerdict

_HERE = Path(__file__).resolve().parent

INITIAL_PROMPT: str = (_HERE / "initial_prompt.md").read_text(encoding="utf-8")
SIM_TOOLED_PERSONA: str = (_HERE / "sim_tooled_persona.md").read_text(encoding="utf-8")
SIM_REVIEW_PROMPT: str = (_HERE / "sim_review_prompt.md").read_text(encoding="utf-8")
CLI_REVIEWER_PROMPT: str = (_HERE / "cli_reviewer_prompt.md").read_text(encoding="utf-8")


def sim_first_turn(agent_text: str) -> str:
    """Build the SIM's first WORK-session turn: persona preamble + agent message."""

    return (
        "You are about to act as the SIM persona described below for the "
        "remainder of this conversation. Read it, internalise it, and respond "
        "only as that persona from now on.\n\n"
        "─── PERSONA ───\n"
        f"{SIM_TOOLED_PERSONA}\n"
        "─── END PERSONA ───\n\n"
        "The architecture agent's latest message is below. Respond as the SWE.\n\n"
        "AGENT:\n"
        f"{agent_text}"
    )


def sim_subsequent_turn(agent_text: str) -> str:
    """Build a non-first SIM turn — persona is already in session memory."""

    return "AGENT:\n" + agent_text


def revision_followup(
    required_changes: list[str],
    *,
    sim: ParsedVerdict | None = None,
) -> str:
    """Build the message that resumes the agent's WORK session after CHANGES_REQUESTED.

    Forwards the SIM's natural-language `summary` alongside the
    `required_changes` bullets so the agent gets framing (what's right,
    what's wrong) rather than just terse instructions.
    """

    bullets = "\n".join(f"- {item}" for item in required_changes) or "- (none specified)"
    summary = (f"[SIM]\n{sim.summary.strip()}\n\n") if sim is not None else ""
    return (
        "The review pass returned CHANGES_REQUESTED.\n\n"
        f"{summary}"
        "Required changes:\n"
        f"{bullets}\n\n"
        "Address these items, then re-write "
        "`.contremaitre/IMPLEMENTATION_COMPLETE` when ready for another review."
    )


def cli_revision_followup(
    required_changes: list[str],
    *,
    round_n: int = 1,
    round_of: int = 3,
) -> str:
    """Build the message that resumes the agent's session after a CLI reviewer round.

    Lean context: the agent already has the full codebase in its session
    memory. The `[CLI]` tag distinguishes this from SIM-driven revisions.
    `round_n/round_of` lets the agent know how many rounds remain.
    """

    bullets = "\n".join(f"- {item}" for item in required_changes) or "- (none specified)"
    return (
        f"[CLI] Post-PR review round {round_n}/{round_of} did not reach LOOKS_GOOD.\n\n"
        "Required changes:\n"
        f"{bullets}\n\n"
        "Address these items, then re-write `.contremaitre/IMPLEMENTATION_COMPLETE`."
    )


__all__ = [
    "INITIAL_PROMPT",
    "SIM_TOOLED_PERSONA",
    "SIM_REVIEW_PROMPT",
    "sim_first_turn",
    "sim_subsequent_turn",
    "revision_followup",
    "cli_revision_followup",
]
