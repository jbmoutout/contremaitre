"""Unit tests for WorkSession.

All tests are pure-Python: no filesystem (beyond an empty tmp_path for the
worktree dir that FakeActorRunner needs), no git, no Docker.
I/O callables are replaced with capturing lambdas.
"""

from __future__ import annotations

import time
import unittest
from dataclasses import dataclass
from typing import Any

from contremaitre import events
from contremaitre.actors import ActorOutput, ActorRunner
from contremaitre.models import Caps
from contremaitre.work_session import WorkSession, _WorktreeSnapshot


# ---------------------------------------------------------------------------
# Minimal test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Response:
    text: str


class _SequenceActor:
    """Returns pre-scripted agent and SIM responses in order."""

    def __init__(
        self,
        agent_texts: list[str] | None = None,
        sim_texts: list[str] | None = None,
    ) -> None:
        self._agent = iter(agent_texts or [])
        self._sim = iter(sim_texts or [])

    def agent_turn(self, message: str) -> ActorOutput:
        return ActorOutput(text=next(self._agent, "agent-default"))

    def sim_turn(self, message: str) -> ActorOutput:
        return ActorOutput(text=next(self._sim, "sim-default"))

    def sim_review(self, **kwargs: Any) -> ActorOutput:
        raise NotImplementedError


def _make_session(
    *,
    actor: ActorRunner | None = None,
    caps: Caps | None = None,
    started: float | None = None,
    is_done_flag: list[bool] | None = None,
    emitted: list[tuple[str, dict]] | None = None,
    cost_value: float = 0.0,
    snapshot: _WorktreeSnapshot | None = None,
) -> WorkSession:
    """Build a WorkSession with injectable doubles."""

    _is_done_flag = is_done_flag if is_done_flag is not None else [False]
    _emitted = emitted if emitted is not None else []
    _snapshot = snapshot or _WorktreeSnapshot(status="", diff_stat="")

    return WorkSession(
        actor=actor or _SequenceActor(),
        caps=caps or Caps(),
        started=started if started is not None else time.monotonic(),
        is_done=lambda: _is_done_flag[0],
        emit=lambda event, **kw: _emitted.append((event, kw)),
        cost_estimate=lambda: cost_value,
        write_cost_report=lambda _cost: None,
        record_agent_snapshot=lambda _label: _snapshot,
    )


# ---------------------------------------------------------------------------
# Cap enforcement tests
# ---------------------------------------------------------------------------


class TurnCapTest(unittest.TestCase):
    def test_fires_after_initial_agent_turn(self):
        """max_turns=1: cap fires after turn 1 (before the SIM loop starts)."""

        emitted: list[tuple[str, dict]] = []
        session = _make_session(
            actor=_SequenceActor(agent_texts=["hello"]),
            caps=Caps(max_turns=1),
            emitted=emitted,
        )

        result = session.run("start")

        self.assertTrue(result.cap_tripped)
        self.assertEqual(result.stop_reason, "cap_tripped_turn_1")
        self.assertFalse(result.implementation_complete)
        self.assertEqual(session.turns, 1)
        cap_events = [e for e, _ in emitted if e == events.TURN_CAP]
        self.assertEqual(len(cap_events), 1)

    def test_fires_mid_sim_loop(self):
        """max_turns=2: cap fires after the first SIM turn."""

        emitted: list[tuple[str, dict]] = []
        session = _make_session(
            actor=_SequenceActor(
                agent_texts=["agent-1"],
                sim_texts=["sim-1"],
            ),
            caps=Caps(max_turns=2),
            emitted=emitted,
        )

        result = session.run("start")

        self.assertTrue(result.cap_tripped)
        self.assertIn("cap_tripped_after_sim_turn_2", result.stop_reason)
        self.assertEqual(session.turns, 2)

    def test_turn_count_carries_over_between_runs(self):
        """Second run() call sees turns accumulated from first run."""

        emitted: list[tuple[str, dict]] = []
        session = _make_session(
            actor=_SequenceActor(
                agent_texts=["a1", "a2"],
                sim_texts=["s1"],
            ),
            caps=Caps(max_turns=3),
            emitted=emitted,
        )

        # First run: max_turns=3 cap fires after 3 turns (a1, s1, a2).
        result1 = session.run("first")
        self.assertTrue(result1.cap_tripped)
        self.assertEqual(session.turns, 3)

        # Second run: cap fires immediately (turns still at 3 >= max_turns=3).
        result2 = session.run("second")
        self.assertTrue(result2.cap_tripped)
        self.assertEqual(result2.stop_reason, "cap_tripped_turn_1")


class WallCapTest(unittest.TestCase):
    def test_fires_when_started_is_in_the_past(self):
        """Wall cap fires when started is old enough to exceed max_wall_minutes."""

        emitted: list[tuple[str, dict]] = []
        # Pretend the run started 999 minutes ago; max is 180.
        old_start = time.monotonic() - 999 * 60
        session = _make_session(
            actor=_SequenceActor(agent_texts=["hello"]),
            caps=Caps(max_wall_minutes=180),
            started=old_start,
            emitted=emitted,
        )

        result = session.run("start")

        self.assertTrue(result.cap_tripped)
        wall_events = [e for e, _ in emitted if e == events.WALL_CAP]
        self.assertEqual(len(wall_events), 1)


class CostCapTest(unittest.TestCase):
    def test_fires_when_cost_exceeds_limit(self):
        """Cost cap fires when cost_estimate returns >= max_cost_usd."""

        emitted: list[tuple[str, dict]] = []
        session = _make_session(
            actor=_SequenceActor(agent_texts=["hello"]),
            caps=Caps(max_cost_usd=10.0),
            cost_value=15.0,
            emitted=emitted,
        )

        result = session.run("start")

        self.assertTrue(result.cap_tripped)
        cost_events = [e for e, _ in emitted if e == events.RECORDED_COST_CAP]
        self.assertEqual(len(cost_events), 1)
        self.assertEqual(cost_events[0], events.RECORDED_COST_CAP)
        cost_kw = [kw for e, kw in emitted if e == events.RECORDED_COST_CAP][0]
        self.assertEqual(cost_kw["max_cost_usd"], 10.0)

    def test_does_not_fire_when_cost_is_below_limit(self):
        is_done_flag = [False]
        session = _make_session(
            actor=_SequenceActor(agent_texts=["hello"]),
            caps=Caps(max_turns=1, max_cost_usd=10.0),
            is_done_flag=is_done_flag,
            cost_value=5.0,
        )

        result = session.run("start")

        # Cap fires via turn limit, not cost.
        self.assertNotEqual(result.stop_reason, "recorded_cost_cap")
        self.assertIn("cap_tripped", result.stop_reason)


class NoProgressCapTest(unittest.TestCase):
    def test_fires_after_no_progress_streak(self):
        """no_progress streak increments when snapshot and text length are identical."""

        emitted: list[tuple[str, dict]] = []
        # Identical snapshot + identical response length → no progress.
        fixed_snapshot = _WorktreeSnapshot(status="M foo.py", diff_stat="1 file changed")

        session = _make_session(
            actor=_SequenceActor(
                # All responses same length so no-progress key never changes.
                agent_texts=["aaaa", "aaaa", "aaaa", "aaaa"],
                sim_texts=["bbbb", "bbbb", "bbbb"],
            ),
            caps=Caps(no_progress_turns=2, max_turns=30),
            emitted=emitted,
            snapshot=fixed_snapshot,
        )

        result = session.run("start")

        self.assertTrue(result.cap_tripped)
        np_events = [e for e, _ in emitted if e == events.NO_PROGRESS_CAP]
        self.assertGreaterEqual(len(np_events), 1)

    def test_resets_streak_on_progress(self):
        """Changing the snapshot resets the no-progress streak to 0."""

        emitted: list[tuple[str, dict]] = []
        snapshots = [
            _WorktreeSnapshot(status="M a.py", diff_stat="1 file"),
            _WorktreeSnapshot(status="M b.py", diff_stat="2 files"),  # changed → streak reset
        ]
        snap_iter = iter(snapshots)

        session = _make_session(
            actor=_SequenceActor(
                agent_texts=["hello", "world"],
                sim_texts=["sim-reply"],
            ),
            caps=Caps(no_progress_turns=1, max_turns=4),
            emitted=emitted,
            snapshot=None,  # ignored below
        )
        # Override record_agent_snapshot to cycle through changing snapshots.
        session._record_agent_snapshot = lambda _label: next(snap_iter, snapshots[-1])

        session.run("start")

        # With changing snapshots, no_progress_streak never reaches the cap
        # during the first two agent turns.
        no_prog_cap_events = [e for e, _ in emitted if e == events.NO_PROGRESS_CAP]
        self.assertEqual(len(no_prog_cap_events), 0)
        prog_events = [e for e, _ in emitted if e == events.PROGRESS]
        self.assertGreaterEqual(len(prog_events), 2)


# ---------------------------------------------------------------------------
# Terminal signal tests
# ---------------------------------------------------------------------------


class TerminalSignalTest(unittest.TestCase):
    def test_exits_immediately_on_turn_1_if_done(self):
        is_done_flag = [False]
        call_count = [0]

        def agent_response(message: str) -> ActorOutput:
            call_count[0] += 1
            is_done_flag[0] = True  # mark done after first agent turn
            return ActorOutput(text="I wrote the file")

        class _DoneActor:
            def agent_turn(self, message):
                return agent_response(message)

            def sim_turn(self, message):
                raise AssertionError("SIM should not be called after is_done")

            def sim_review(self, **kwargs):
                raise NotImplementedError

        session = _make_session(
            actor=_DoneActor(),
            caps=Caps(max_turns=30),
            is_done_flag=is_done_flag,
        )

        result = session.run("start")

        self.assertTrue(result.implementation_complete)
        self.assertFalse(result.cap_tripped)
        self.assertEqual(result.stop_reason, "implementation_complete_turn_1")
        self.assertEqual(session.turns, 1)

    def test_exits_after_sim_turn_if_done(self):
        is_done_flag = [False]
        sim_call_count = [0]

        class _DoneAfterSimActor:
            def agent_turn(self, message):
                return ActorOutput(text="agent output")

            def sim_turn(self, message):
                sim_call_count[0] += 1
                is_done_flag[0] = True
                return ActorOutput(text="sim agrees — done")

            def sim_review(self, **kwargs):
                raise NotImplementedError

        session = _make_session(
            actor=_DoneAfterSimActor(),
            caps=Caps(max_turns=30),
            is_done_flag=is_done_flag,
        )

        result = session.run("start")

        self.assertTrue(result.implementation_complete)
        self.assertFalse(result.cap_tripped)
        self.assertIn("implementation_complete_after_sim_turn", result.stop_reason)
        self.assertEqual(sim_call_count[0], 1)


# ---------------------------------------------------------------------------
# Turn event tests
# ---------------------------------------------------------------------------


class TurnEventTest(unittest.TestCase):
    def test_emits_turn_event_for_each_before_turn(self):
        emitted: list[tuple[str, dict]] = []
        is_done_flag = [False]

        class _TwoTurnActor:
            _calls = [0]

            def agent_turn(self, message):
                self._calls[0] += 1
                if self._calls[0] >= 2:
                    is_done_flag[0] = True
                return ActorOutput(text="agent")

            def sim_turn(self, message):
                return ActorOutput(text="sim")

            def sim_review(self, **kwargs):
                raise NotImplementedError

        session = _make_session(
            actor=_TwoTurnActor(),
            caps=Caps(max_turns=30),
            is_done_flag=is_done_flag,
            emitted=emitted,
        )

        session.run("start")

        turn_events = [kw["turn"] for e, kw in emitted if e == events.TURN]
        # Turn events should be sequential: 1, 2, 3 (agent, sim, agent-done)
        self.assertEqual(turn_events, list(range(1, len(turn_events) + 1)))

    def test_before_turn_increments_counter(self):
        """before_turn() is also callable directly (used by review phase)."""

        session = _make_session()
        self.assertEqual(session.turns, 0)
        session.before_turn()
        self.assertEqual(session.turns, 1)
        session.before_turn()
        self.assertEqual(session.turns, 2)

    def test_before_turn_emits_turn_event(self):
        emitted: list[tuple[str, dict]] = []
        session = _make_session(emitted=emitted)

        session.before_turn()

        self.assertEqual(len(emitted), 1)
        event, kw = emitted[0]
        self.assertEqual(event, events.TURN)
        self.assertEqual(kw["turn"], 1)


# ---------------------------------------------------------------------------
# Message selection tests
# ---------------------------------------------------------------------------


class MessageSelectionTest(unittest.TestCase):
    def test_first_run_uses_initial_prompt(self):
        """The first message passed to run() is forwarded directly to the actor."""

        received: list[str] = []

        class _RecordingActor:
            def agent_turn(self, message):
                received.append(message)
                return ActorOutput(text="done")

            def sim_turn(self, message):
                return ActorOutput(text="sim")

            def sim_review(self, **kwargs):
                raise NotImplementedError

        is_done_flag = [True]  # done immediately
        session = _make_session(
            actor=_RecordingActor(),
            is_done_flag=is_done_flag,
        )

        sentinel = "SENTINEL_FIRST_MESSAGE"
        session.run(sentinel)

        self.assertEqual(received[0], sentinel)

    def test_sim_first_turn_uses_persona_preamble(self):
        """First SIM turn wraps the agent text with sim_first_turn prompt."""

        from contremaitre import prompts

        sim_messages: list[str] = []
        is_done_flag = [False]
        agent_call_count = [0]

        class _TrackingActor:
            def agent_turn(self, message):
                agent_call_count[0] += 1
                if agent_call_count[0] > 1:
                    is_done_flag[0] = True
                return ActorOutput(text="AGENT_OUTPUT")

            def sim_turn(self, message):
                sim_messages.append(message)
                return ActorOutput(text="sim-reply")

            def sim_review(self, **kwargs):
                raise NotImplementedError

        session = _make_session(
            actor=_TrackingActor(),
            caps=Caps(max_turns=30),
            is_done_flag=is_done_flag,
        )

        session.run("start")

        self.assertEqual(len(sim_messages), 1)
        # The first SIM turn should include the persona preamble.
        expected = prompts.sim_first_turn("AGENT_OUTPUT")
        self.assertEqual(sim_messages[0], expected)


# ---------------------------------------------------------------------------
# cap_tripped() public interface
# ---------------------------------------------------------------------------


class CapTrippedPublicTest(unittest.TestCase):
    def test_returns_false_when_no_caps_exceeded(self):
        session = _make_session(caps=Caps(max_turns=30))
        # No turns yet, well within all limits.
        self.assertFalse(session.cap_tripped())

    def test_returns_true_after_turn_count_hits_max(self):
        session = _make_session(caps=Caps(max_turns=2))
        session.before_turn()
        session.before_turn()
        self.assertTrue(session.cap_tripped())


if __name__ == "__main__":
    unittest.main()
