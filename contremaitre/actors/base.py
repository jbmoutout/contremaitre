from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ActorError(RuntimeError):
    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ActorOutput:
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
