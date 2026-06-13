from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .models import State


_state_table: dict[State, dict[str, State | None]] = {
    State.INIT: {"worktree_ready": State.WORK},
    State.WORK: {"session_done": State.REVIEW},
    State.REVIEW: {
        "needs_revision": State.WORK,
        "approved": State.APPROVED,
        "rejected": State.NO_PR,
    },
    State.APPROVED: {},
    State.NO_PR: {},
    State.FAILED: {},
}

_TERMINAL = frozenset({State.NO_PR, State.FAILED, State.APPROVED})


@dataclass
class StateMachine:
    current: State = State.INIT
    trajectory: list[dict] = field(default_factory=list)
    on_transition: Callable[[State, str, str], None] | None = None

    def transition(self, verb: str, note: str = "") -> None:
        row = _state_table.get(self.current, {})
        target = row.get(verb)
        if target is None and verb not in row:
            allowed = list(row.keys())
            raise ValueError(f"no verb {verb!r} from {self.current.value}; allowed: {allowed}")
        from_state = self.current
        if target is not None:
            self.current = target
        self._record(from_state, verb, note)

    def force(self, to: State, note: str = "") -> None:
        from_state = self.current
        self.current = to
        self._record(from_state, "__force__", note)

    @property
    def allowed_verbs(self) -> list[str]:
        return list(_state_table.get(self.current, {}).keys())

    @property
    def is_terminal(self) -> bool:
        return self.current in _TERMINAL

    def _record(self, from_state: State, verb: str, note: str) -> None:
        entry = {
            "from": from_state.value,
            "state": self.current.value,
            "verb": verb,
            "note": note,
        }
        self.trajectory.append(entry)
        if self.on_transition:
            self.on_transition(from_state, verb, note)
