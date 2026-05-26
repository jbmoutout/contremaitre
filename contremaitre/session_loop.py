from __future__ import annotations

from . import prompts
from .actors import ActorRunner
from .cap_guard import CapGuard
from .git_utils import GitRepo
from .jsonlog import append_jsonl
from .models import ParsedVerdict, RunConfig, RunPaths
from .worktree_manager import IMPLEMENTATION_COMPLETE_RELPATH, WorktreeManager


class SessionLoop:
    def __init__(
        self,
        config: RunConfig,
        paths: RunPaths,
        cap_guard: CapGuard,
        worktree_manager: WorktreeManager,
        emit,
        trajectory: list[dict] | None = None,
    ):
        self.config = config
        self.paths = paths
        self.cap = cap_guard
        self.wm = worktree_manager
        self.emit = emit
        self.trajectory = trajectory if trajectory is not None else []

    @property
    def turns(self) -> int:
        return self.cap.turns

    def run(
        self,
        *,
        actor: ActorRunner,
        review_round: int,
        required_changes: list[str],
        sim_parsed: ParsedVerdict | None,
        extra_parsed: ParsedVerdict | None,
        diff_base: str,
    ) -> str:
        if review_round == 1:
            first_message = prompts.INITIAL_PROMPT
        else:
            first_message = prompts.revision_followup(
                required_changes,
                sim=sim_parsed,
                extra=extra_parsed,
            )

        agent_text = self._agent_turn(actor, first_message, diff_base)
        if self._implementation_complete():
            return "implementation_complete_turn_1"
        if self._cap_tripped():
            return "cap_tripped_turn_1"

        sim_first = True
        for turn in range(2, self.config.caps.max_turns + 1):
            sim_message = (
                prompts.sim_first_turn(agent_text)
                if sim_first
                else prompts.sim_subsequent_turn(agent_text)
            )
            sim_text = self._sim_turn(actor, sim_message)
            sim_first = False
            if self._implementation_complete():
                return f"implementation_complete_after_sim_turn_{turn}"
            if self._cap_tripped():
                return f"cap_tripped_after_sim_turn_{turn}"

            agent_text = self._agent_turn(actor, sim_text, diff_base)
            if self._implementation_complete():
                return f"implementation_complete_turn_{turn}"
            if self._cap_tripped():
                return f"cap_tripped_after_agent_turn_{turn}"

        return "max_turns"

    def _agent_turn(self, actor: ActorRunner, message: str, diff_base: str) -> str:
        self.cap.before_turn()
        output = actor.agent_turn(message)
        text = output.text
        worktree_git = GitRepo(self.paths.worktree, self.paths.git_log)
        label = f"after-agent-turn-{self.cap.turns}"
        status, diff_stat = self.wm.record_worktree_state(worktree_git, label, diff_base)
        self._transition("WORK", label)
        event = self.cap.record_progress(status, diff_stat, label, text)
        self.emit(event, label=label, no_progress_streak=self.cap.no_progress_streak)
        return text

    def _sim_turn(self, actor: ActorRunner, message: str) -> str:
        self.cap.before_turn()
        output = actor.sim_turn(message)
        return output.text

    def _transition(self, state: str, note: str) -> None:
        record = {"state": state, "note": note, "turns": self.cap.turns}
        self.trajectory.append(record)
        append_jsonl(self.paths.timeline, record)

    def _implementation_complete(self) -> bool:
        return (self.paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH).exists()

    def _cap_tripped(self) -> bool:
        return self.cap.tripped(
            self.paths.raw_export,
            self.paths.sim_raw_export,
            self.paths.cost_report,
            self.emit,
        ) is not None
