from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ActorError(RuntimeError):
    """Generic actor failure surface.

    `kind` is a free-form tag that lets callers (and the TUI) distinguish
    failure modes worth a custom label without parsing the message string.
    Defaults to `None` for legacy errors that don't classify themselves.
    """

    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ActorOutput:
    """One turn's text reply, after the actor has logged itself.

    Adapters own raw_export.jsonl + transcript.md writes for their own turns.
    The orchestrator just consumes `text` and moves on. No bool field telling
    the caller "did I log for you?" — that was a leaking-abstraction marker.
    """

    text: str
    stderr: str = ""
    returncode: int = 0


class ActorRunner(Protocol):
    def agent_turn(self, message: str) -> ActorOutput: ...

    def sim_turn(self, message: str) -> ActorOutput: ...

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
        reviewer_id: str = "sim",
        model_override: str | None = None,
    ) -> ActorOutput: ...
