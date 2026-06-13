"""Unit tests for `StateMachine` — the explicit state machine.

`StateMachine` owns the verb-based transition table, current-state
tracking, trajectory recording, and the `force` escape hatch. The
interface is the test surface: call `transition(verb)` and assert on
`current`, `is_terminal`, `trajectory`, or `allowed_verbs`.
"""

from __future__ import annotations

import pytest

from contremaitre.models import State
from contremaitre.statemachine import StateMachine


# --------------------------------------------------------------------------
# Initial state
# --------------------------------------------------------------------------


def test_starts_at_init():
    sm = StateMachine()
    assert sm.current == State.INIT
    assert not sm.is_terminal


# --------------------------------------------------------------------------
# Valid transitions — every verb from every state
# --------------------------------------------------------------------------


def test_worktree_ready_moves_init_to_work():
    sm = StateMachine()
    sm.transition("worktree_ready")
    assert sm.current == State.WORK
    assert not sm.is_terminal


def test_session_done_moves_work_to_review():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    assert sm.current == State.REVIEW
    assert not sm.is_terminal


def test_needs_revision_moves_review_to_work():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("needs_revision")
    assert sm.current == State.WORK


def test_approved_moves_review_to_approved():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("approved")
    assert sm.current == State.APPROVED
    assert sm.is_terminal


def test_rejected_moves_review_to_no_pr():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("rejected")
    assert sm.current == State.NO_PR
    assert sm.is_terminal


# --------------------------------------------------------------------------
# Full happy-path flow
# --------------------------------------------------------------------------


def test_full_happy_path():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("approved")
    assert sm.current == State.APPROVED
    assert sm.is_terminal
    assert len(sm.trajectory) == 3


def test_full_revision_loop():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("needs_revision")
    assert sm.current == State.WORK
    sm.transition("session_done")
    sm.transition("approved")
    assert sm.current == State.APPROVED


def test_full_rejected_path():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("rejected")
    assert sm.current == State.NO_PR
    assert sm.is_terminal


# --------------------------------------------------------------------------
# Invalid verb
# --------------------------------------------------------------------------


def test_invalid_verb_raises():
    sm = StateMachine()
    with pytest.raises(ValueError, match="no verb 'not_a_verb' from INIT"):
        sm.transition("not_a_verb")


def test_invalid_verb_from_work():
    sm = StateMachine()
    sm.transition("worktree_ready")
    with pytest.raises(ValueError, match="no verb 'approved' from WORK"):
        sm.transition("approved")


def test_terminal_state_rejects_verbs():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("approved")
    with pytest.raises(ValueError):
        sm.transition("worktree_ready")


# --------------------------------------------------------------------------
# force() escape hatch
# --------------------------------------------------------------------------


def test_force_skips_validation():
    sm = StateMachine()
    sm.force(State.FAILED, note="something broke")
    assert sm.current == State.FAILED
    assert sm.is_terminal
    assert len(sm.trajectory) == 1
    assert sm.trajectory[0]["verb"] == "__force__"


def test_force_from_any_state():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.force(State.NO_PR, note="aborted")
    assert sm.current == State.NO_PR
    assert sm.is_terminal


# --------------------------------------------------------------------------
# is_terminal
# --------------------------------------------------------------------------


def test_approved_is_terminal():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("approved")
    assert sm.is_terminal


def test_no_pr_is_terminal():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("rejected")
    assert sm.is_terminal


def test_failed_is_terminal():
    sm = StateMachine()
    sm.force(State.FAILED)
    assert sm.is_terminal


def test_work_is_not_terminal():
    sm = StateMachine()
    sm.transition("worktree_ready")
    assert not sm.is_terminal


def test_review_is_not_terminal():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    assert not sm.is_terminal


# --------------------------------------------------------------------------
# allowed_verbs
# --------------------------------------------------------------------------


def test_allowed_verbs_from_init():
    sm = StateMachine()
    assert sm.allowed_verbs == ["worktree_ready"]


def test_allowed_verbs_from_review():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    assert sorted(sm.allowed_verbs) == ["approved", "needs_revision", "rejected"]


def test_allowed_verbs_from_terminal():
    sm = StateMachine()
    sm.force(State.APPROVED)
    assert sm.allowed_verbs == []


# --------------------------------------------------------------------------
# Trajectory recording
# --------------------------------------------------------------------------


def test_trajectory_records_from_state_and_verb_and_state():
    sm = StateMachine()
    sm.transition("worktree_ready", note="created worktree")
    assert len(sm.trajectory) == 1
    entry = sm.trajectory[0]
    assert entry == {
        "from": "INIT",
        "state": "WORK",
        "verb": "worktree_ready",
        "note": "created worktree",
    }


def test_trajectory_tracks_full_path():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("needs_revision")
    assert len(sm.trajectory) == 3
    assert sm.trajectory[0]["verb"] == "worktree_ready"
    assert sm.trajectory[0]["state"] == "WORK"
    assert sm.trajectory[1]["verb"] == "session_done"
    assert sm.trajectory[1]["state"] == "REVIEW"
    assert sm.trajectory[2]["verb"] == "needs_revision"
    assert sm.trajectory[2]["state"] == "WORK"


def test_force_appears_in_trajectory():
    sm = StateMachine()
    sm.force(State.FAILED, note="explosion")
    assert sm.trajectory[0]["verb"] == "__force__"
    assert sm.trajectory[0]["state"] == "FAILED"


# --------------------------------------------------------------------------
# on_transition callback
# --------------------------------------------------------------------------


def test_on_transition_fires_with_from_verb_note():
    calls: list[tuple] = []

    def cb(from_state, verb, note):
        calls.append((from_state, verb, note))

    sm = StateMachine(on_transition=cb)
    sm.transition("worktree_ready", note="hello")
    assert len(calls) == 1
    assert calls[0] == (State.INIT, "worktree_ready", "hello")


def test_on_transition_fires_for_force():
    calls: list[tuple] = []

    def cb(from_state, verb, note):
        calls.append((from_state, verb, note))

    sm = StateMachine(on_transition=cb)
    sm.force(State.FAILED, note="kaboom")
    assert len(calls) == 1
    assert calls[0] == (State.INIT, "__force__", "kaboom")


def test_on_transition_fires_multiple_transitions():
    calls: list[tuple] = []

    def cb(from_state, verb, note):
        calls.append((from_state, verb, note))

    sm = StateMachine(on_transition=cb)
    sm.transition("worktree_ready")
    sm.transition("session_done")
    sm.transition("approved")
    assert len(calls) == 3


def test_on_transition_none_does_not_crash():
    sm = StateMachine(on_transition=None)
    sm.transition("worktree_ready")
    sm.force(State.FAILED)
    assert sm.current == State.FAILED


# --------------------------------------------------------------------------
# No-op: verbs targeting None in the table
# --------------------------------------------------------------------------


def test_force_to_current_state():
    sm = StateMachine()
    sm.transition("worktree_ready")
    sm.force(State.WORK)
    assert sm.current == State.WORK
