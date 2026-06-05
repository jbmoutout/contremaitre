from __future__ import annotations

from contremaitre import events
from contremaitre.artifact_signals import (
    compute_phase_counts,
    compute_self_verification,
    detect_artifact_writes,
    tokens_before,
    tool_paths,
)


def _write_tool_event(
    tool: str,
    file_path: str = "",
    *,
    status: str = "completed",
    timestamp: int = 1_000,
    content: str = "",
) -> dict:
    return {
        "type": "tool_use",
        "timestamp": timestamp,
        "part": {
            "tool": tool,
            "state": {
                "status": status,
                "input": {"filePath": file_path, "content": content},
            },
        },
    }


def _completed_bash(*, timestamp: int, command: str, output: str = "") -> dict:
    return {
        "type": "tool_use",
        "timestamp": timestamp,
        "part": {
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": command},
                "output": output,
            },
        },
    }


def _completed_apply_patch(*, timestamp: int, path: str, body: str = "new") -> dict:
    return {
        "type": "tool_use",
        "timestamp": timestamp,
        "part": {
            "tool": "apply_patch",
            "state": {
                "status": "completed",
                "input": {
                    "patchText": (
                        f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+{body}\n"
                        "*** End Patch\n"
                    )
                },
            },
        },
    }


def test_detect_artifact_writes_false_when_empty():
    writes = detect_artifact_writes([])

    assert writes.architecture_review is None
    assert writes.settled_design is None
    assert writes.implementation_complete is None


def test_detect_settled_design_write():
    events_list = [
        _write_tool_event(
            "write",
            "/worktree/.contremaitre/SETTLED_DESIGN.md",
            content="settled",
        )
    ]

    writes = detect_artifact_writes(events_list)

    assert writes.settled_design is not None
    assert writes.settled_design.chars == len("settled")


def test_detect_ignores_incomplete_writes():
    events_list = [
        _write_tool_event(
            "write",
            "/worktree/.contremaitre/SETTLED_DESIGN.md",
            status="running",
        )
    ]

    assert detect_artifact_writes(events_list).settled_design is None


def test_detect_implementation_complete_apply_patch():
    events_list = [
        {
            "type": "tool_use",
            "timestamp": 1_000,
            "part": {
                "tool": "apply_patch",
                "state": {
                    "status": "completed",
                    "input": {
                        "patchText": (
                            "*** Begin Patch\n"
                            "*** Add File: .contremaitre/IMPLEMENTATION_COMPLETE\n"
                            "+done\n"
                            "*** End Patch\n"
                        )
                    },
                },
            },
        }
    ]

    assert detect_artifact_writes(events_list).implementation_complete is not None


def test_detect_architecture_review_write_and_apply_patch():
    write_events = [_write_tool_event("write", "/worktree/.contremaitre/architecture-review.html")]
    patch_events = [
        {
            "type": "tool_use",
            "timestamp": 1_000,
            "part": {
                "tool": "apply_patch",
                "state": {
                    "status": "completed",
                    "input": {
                        "patchText": (
                            "*** Begin Patch\n"
                            "*** Add File: .contremaitre/architecture-review.html\n"
                            "+<html/>\n"
                            "*** End Patch\n"
                        )
                    },
                },
            },
        }
    ]

    assert detect_artifact_writes(write_events).architecture_review is not None
    assert detect_artifact_writes(patch_events).architecture_review is not None


def test_first_code_edit_excludes_contremaitre_paths():
    events_list = [
        _write_tool_event("write", "/worktree/.contremaitre/SETTLED_DESIGN.md", timestamp=1_000),
        _completed_apply_patch(timestamp=2_000, path="app/foo.py"),
    ]

    writes = detect_artifact_writes(events_list)

    assert writes.first_code_edit is not None
    assert writes.first_code_edit.timestamp_ms == 2_000


def test_tool_paths_parses_apply_patch_paths():
    event = _completed_apply_patch(timestamp=1_000, path="app/foo.py")

    assert tool_paths(event) == ("app/foo.py",)


def test_tokens_before_uses_signal_index():
    events_list = [
        {"type": "step_finish", "part": {"tokens": {"total": 10}}},
        _write_tool_event("write", "/worktree/.contremaitre/SETTLED_DESIGN.md"),
        {"type": "step_finish", "part": {"tokens": {"total": 20}}},
    ]
    signal = detect_artifact_writes(events_list).settled_design

    assert tokens_before(events_list, signal) == 10


def test_self_verified_counts_test_after_last_code_edit():
    events_list = [
        _completed_bash(timestamp=1_000, command="pytest -q", output="1 passed"),
        _completed_apply_patch(timestamp=2_000, path="app/foo.py"),
    ]
    assert compute_self_verification(events_list).self_verified is False

    events_list.append(_completed_bash(timestamp=3_000, command="pytest -q", output="1 passed"))
    verification = compute_self_verification(events_list)
    assert verification.self_verified is True
    assert verification.output_suggests_pass is True


def test_self_verification_stops_at_implementation_complete():
    events_list = [
        _completed_apply_patch(timestamp=1_000, path="app/foo.py"),
        _write_tool_event(
            "write", "/worktree/.contremaitre/IMPLEMENTATION_COMPLETE", timestamp=2_000
        ),
        _completed_bash(timestamp=3_000, command="pytest -q", output="1 passed"),
    ]

    assert compute_self_verification(events_list).self_verified is False


def test_self_verification_flags_failed_or_zero_tests():
    events_list = [
        _completed_apply_patch(timestamp=1_000, path="app/foo.py"),
        _completed_bash(timestamp=2_000, command="pytest -q", output="no tests ran"),
    ]

    verification = compute_self_verification(events_list)

    assert verification.self_verified is True
    assert verification.output_suggests_pass is False


def test_phase_counts_use_max_round_not_len():
    agent_events = [
        _write_tool_event("write", "/worktree/.contremaitre/SETTLED_DESIGN.md", timestamp=3_500),
        _write_tool_event(
            "write",
            "/worktree/.contremaitre/IMPLEMENTATION_COMPLETE",
            timestamp=4_500,
        ),
    ]
    guardrails = [
        {"timestamp": 1_000, "event": events.OPENCODE_ACTOR_START, "role": "agent"},
        {"timestamp": 2_000, "event": events.OPENCODE_ACTOR_START, "role": "sim"},
        {"timestamp": 3_000, "event": events.OPENCODE_ACTOR_START, "role": "agent"},
    ]
    review_cycles = [
        {"round": 1, "reviewer": "sim"},
        {"round": 1, "reviewer": "extra"},
        {"round": 2, "reviewer": "sim"},
        {"round": 2, "reviewer": "extra"},
    ]

    counts = compute_phase_counts(agent_events, guardrails, review_cycles)

    assert counts.grilling_exchanges == 1
    assert counts.impl_turns == 1
    assert counts.review_rounds == 2
