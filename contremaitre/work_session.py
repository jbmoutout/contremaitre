"""WORK-session multi-turn loop.

Extracted from the Orchestrator so the turn loop, cap enforcement, and
progress detection have a clean seam. The Orchestrator creates one
WorkSession per run and calls `run()` once per review round; carried
state (turn count, no-progress streak) persists across rounds because
all caps are run-level.

All I/O is injected via callables so the loop is testable without
filesystem, git, or Docker.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import events, prompts
from .actors import ActorRunner
from .models import Caps


@dataclass(frozen=True)
class _WorktreeSnapshot:
    """Git status + diff-stat pair captured after each agent turn.

    Moved here from orchestrator.py because WorkSession owns every caller.
    Shared with Orchestrator via import so _record_worktree_state (which
    stays in the Orchestrator) can produce the same type.
    """

    status: str
    diff_stat: str


@dataclass(frozen=True)
class SessionResult:
    """Outcome of one WorkSession.run() call."""

    stop_reason: str
    implementation_complete: bool
    cap_tripped: bool


class WorkSession:
    """Stateful multi-turn WORK loop.

    Created once per run in Orchestrator._review_rounds; run() is called
    once per review round. State (turns, no_progress_streak,
    _last_progress_key) carries over between rounds because all caps are
    run-level.

    Injected callables:

      emit(event, **kw)
          Routes events: TURN → timeline.jsonl; all others →
          guardrail_events.jsonl. Tests pass a capturing lambda.

      cost_estimate() -> float
          Reads raw_export JSONL for recorded spend. Injected to keep
          the session off the filesystem. Tests return a constant.

      write_cost_report(cost: float) -> None
          Writes cost_report.json. Tests use a no-op.

      record_agent_snapshot(label: str) -> _WorktreeSnapshot
          Runs git status/diff and writes worktree_state.jsonl. Stays
          in the Orchestrator (uses _diff_base + GitRepo) and is
          injected here as a callable. Tests return a fixed snapshot.

      is_done() -> bool
          True when IMPLEMENTATION_COMPLETE marker exists. Tests flip
          a captured flag.

    Public methods beyond run():

      before_turn() -- called by Orchestrator._run_one_reviewer (review
          phase) so review attempts consume the same max_turns budget.

      cap_tripped() -- called by Orchestrator._review_rounds after
          session exits (guards the edge case where implementation_complete
          is written and then the wall/cost cap fires before the next check).
    """

    def __init__(
        self,
        *,
        actor: ActorRunner,
        caps: Caps,
        started: float,
        is_done: Callable[[], bool],
        emit: Callable[..., None],
        cost_estimate: Callable[[], float],
        write_cost_report: Callable[[float], None],
        record_agent_snapshot: Callable[[str], _WorktreeSnapshot],
    ) -> None:
        self._actor = actor
        self._caps = caps
        self._started = started
        self._is_done = is_done
        self._emit = emit
        self._cost_estimate = cost_estimate
        self._write_cost_report = write_cost_report
        self._record_agent_snapshot = record_agent_snapshot
        # Carried state — survives across run() calls (run-level caps).
        self.turns: int = 0
        self.no_progress_streak: int = 0
        self._last_progress_key: tuple[str, str] | None = None

    # ----- public interface -----

    def run(self, first_message: str) -> SessionResult:
        """Run one WORK session until terminal signal or cap.

        Caller provides first_message: prompts.INITIAL_PROMPT for round 1,
        prompts.revision_followup(...) for revision rounds.
        """

        agent_text = self._agent_turn(first_message)
        if self._is_done():
            return SessionResult(
                stop_reason="implementation_complete_turn_1",
                implementation_complete=True,
                cap_tripped=False,
            )
        if self.cap_tripped():
            return SessionResult(
                stop_reason="cap_tripped_turn_1",
                implementation_complete=False,
                cap_tripped=True,
            )

        sim_first = True
        for turn in range(2, self._caps.max_turns + 1):
            sim_message = (
                prompts.sim_first_turn(agent_text)
                if sim_first
                else prompts.sim_subsequent_turn(agent_text)
            )
            sim_text = self._sim_turn(sim_message)
            sim_first = False
            if self._is_done():
                return SessionResult(
                    stop_reason=f"implementation_complete_after_sim_turn_{turn}",
                    implementation_complete=True,
                    cap_tripped=False,
                )
            if self.cap_tripped():
                return SessionResult(
                    stop_reason=f"cap_tripped_after_sim_turn_{turn}",
                    implementation_complete=False,
                    cap_tripped=True,
                )

            agent_text = self._agent_turn(sim_text)
            if self._is_done():
                return SessionResult(
                    stop_reason=f"implementation_complete_turn_{turn}",
                    implementation_complete=True,
                    cap_tripped=False,
                )
            if self.cap_tripped():
                return SessionResult(
                    stop_reason=f"cap_tripped_after_agent_turn_{turn}",
                    implementation_complete=False,
                    cap_tripped=True,
                )

        return SessionResult(
            stop_reason="max_turns",
            implementation_complete=False,
            cap_tripped=False,
        )

    def before_turn(self) -> None:
        """Increment the turn counter and emit the TURN timeline event.

        Public so the Orchestrator can call it from _run_one_reviewer
        (review phase), where review attempts consume the same run-level
        max_turns budget.
        """

        self.turns += 1
        self._emit(events.TURN, turn=self.turns)

    def cap_tripped(self) -> bool:
        """Check all run-level caps; emit and return True if any fire.

        Public so the Orchestrator can call it after a session exits
        (edge case: implementation_complete written before wall/cost cap
        fires between session exit and the post-session check).
        """

        wall_minutes = (time.monotonic() - self._started) / 60.0
        if self.turns >= self._caps.max_turns:
            self._emit(events.TURN_CAP, turns=self.turns)
            return True
        if wall_minutes >= self._caps.max_wall_minutes:
            self._emit(events.WALL_CAP, wall_minutes=wall_minutes)
            return True
        recorded_cost = self._cost_estimate()
        self._write_cost_report(recorded_cost)
        if recorded_cost >= self._caps.max_cost_usd:
            self._emit(
                events.RECORDED_COST_CAP,
                recorded_cost_usd=recorded_cost,
                max_cost_usd=self._caps.max_cost_usd,
            )
            return True
        if self.no_progress_streak >= self._caps.no_progress_turns:
            self._emit(
                events.NO_PROGRESS_CAP,
                no_progress_streak=self.no_progress_streak,
                no_progress_turns=self._caps.no_progress_turns,
            )
            return True
        return False

    # ----- private helpers -----

    def _agent_turn(self, message: str) -> str:
        self.before_turn()
        output = self._actor.agent_turn(message)
        text = output.text
        label = f"after-agent-turn-{self.turns}"
        snapshot = self._record_agent_snapshot(label)
        self._record_progress(snapshot, label, text)
        return text

    def _sim_turn(self, message: str) -> str:
        self.before_turn()
        output = self._actor.sim_turn(message)
        return output.text

    def _record_progress(self, snapshot: _WorktreeSnapshot, label: str, text: str) -> None:
        key = (snapshot.status + "\n" + snapshot.diff_stat, str(len(text.strip())))
        if self._last_progress_key is None or key != self._last_progress_key:
            self.no_progress_streak = 0
            self._last_progress_key = key
            event = events.PROGRESS
        else:
            self.no_progress_streak += 1
            event = events.NO_PROGRESS
        self._emit(event, label=label, no_progress_streak=self.no_progress_streak)
