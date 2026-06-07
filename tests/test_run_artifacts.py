from __future__ import annotations

from contremaitre import events
from contremaitre.run_artifacts import (
    Marker,
    compute_phases_from_events,
    marker_timestamp_ms,
    marker_tokens_before,
    marker_write_chars,
    marker_written,
)


def _write_event(path: str, *, timestamp: int = 1_000, content: str = "done") -> dict:
    return {
        "type": "tool_use",
        "timestamp": timestamp,
        "part": {
            "tool": "write",
            "state": {
                "status": "completed",
                "input": {"filePath": path, "content": content},
            },
        },
    }


def _patch_event(path: str, *, body: str = "+done\n") -> dict:
    return {
        "type": "tool_use",
        "timestamp": 1_000,
        "part": {
            "tool": "apply_patch",
            "state": {
                "status": "completed",
                "input": {
                    "patchText": (f"*** Begin Patch\n*** Add File: {path}\n{body}*** End Patch\n"),
                },
            },
        },
    }


def _start(role: str, timestamp: int) -> dict:
    return {"event": events.OPENCODE_ACTOR_START, "role": role, "timestamp": timestamp}


def test_marker_facts_hide_event_shape():
    events_list = [
        {"type": "step_finish", "part": {"tokens": {"total": 17}}},
        _write_event("/app/.contremaitre/SETTLED_DESIGN.md", timestamp=2_000, content="abc"),
    ]

    assert marker_written(events_list, Marker.SETTLED_DESIGN)
    assert marker_timestamp_ms(events_list, Marker.SETTLED_DESIGN) == 2_000
    assert marker_write_chars(events_list, Marker.SETTLED_DESIGN) == 3
    assert marker_tokens_before(events_list, Marker.SETTLED_DESIGN) == 17


def test_marker_write_chars_counts_apply_patch_body_for_matching_path():
    events_list = [_patch_event(".contremaitre/IMPLEMENTATION_COMPLETE", body="+done\n")]

    assert marker_written(events_list, Marker.IMPLEMENTATION_COMPLETE)
    assert marker_write_chars(events_list, Marker.IMPLEMENTATION_COMPLETE) == len("done\n")


def test_compute_phases_from_events_uses_max_review_round():
    agent_events = [
        _write_event("/app/.contremaitre/SETTLED_DESIGN.md", timestamp=1_500),
        _write_event("/app/.contremaitre/IMPLEMENTATION_COMPLETE", timestamp=3_500),
    ]
    guardrails = [
        _start("agent", 1_000),
        _start("sim", 2_000),
        _start("agent", 3_000),
        _start("review", 4_000),
    ]
    review_cycles = [
        {"round": 1, "reviewer": "sim"},
        {"round": 1, "reviewer": "extra"},
    ]

    assert compute_phases_from_events(agent_events, guardrails, review_cycles) == {
        "pre_settled_agent_turns": 0,
        "pre_settled_sim_turns": 0,
        "grilling_exchanges": 0,
        "impl_turns": 2,
        "review_rounds": 1,
    }
