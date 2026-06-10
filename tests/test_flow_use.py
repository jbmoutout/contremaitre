from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from contremaitre.flow_use import (
    compute_flow_use,
    compute_phases,
    compute_phases_from_paths,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _phase_paths(tmp_path: Path):
    return SimpleNamespace(
        raw_export=tmp_path / "raw_export.jsonl",
        review_cycles=tmp_path / "review_cycles.jsonl",
        guardrail_events=tmp_path / "guardrail_events.jsonl",
    )


# Two agent turn boundaries (ts 1000, 3000), one sim (2000) — a SETTLED
# write at ts 3500 lands in the 2nd agent turn, so 1 agent + 1 sim turn
# precede it: grilling = min(1, 1) = 1, impl = 1.
def _phase_guardrails() -> list[dict]:
    return [
        {"event": "actor_start", "role": "agent", "ts": 1000},
        {"event": "actor_start", "role": "sim", "ts": 2000},
        {"event": "actor_start", "role": "agent", "ts": 3000},
        {"event": "actor_start", "role": "review", "ts": 4000},
    ]


def test_compute_phases_opencode_splits_on_settled_write(tmp_path):
    paths = _phase_paths(tmp_path)
    _write_jsonl(
        paths.raw_export,
        [
            {
                "type": "tool_use",
                "timestamp": 3500,
                "part": {
                    "tool": "write",
                    "state": {
                        "status": "completed",
                        "input": {
                            "filePath": "/app/.contremaitre/SETTLED_DESIGN.md",
                            "content": "x",
                        },
                    },
                },
            }
        ],
    )
    _write_jsonl(paths.guardrail_events, _phase_guardrails())
    _write_jsonl(paths.review_cycles, [{"reviewer": "sim", "round": 1, "verdict": "APPROVED"}])
    phases = compute_phases_from_paths(paths)
    assert phases["grilling_exchanges"] == 1
    assert phases["impl_turns"] == 1
    assert phases["pre_settled_agent_turns"] == 1
    assert phases["review_rounds"] == 1


def test_compute_phases_detects_claude_assistant_write(tmp_path):
    # The regression target: claude `assistant` tool_use blocks (epoch-ms
    # int `ts`) were invisible to the opencode-only parser, so CLI runs
    # silently fell back to min-of-total-turns. They must now split.
    paths = _phase_paths(tmp_path)
    _write_jsonl(
        paths.raw_export,
        [
            {
                "type": "assistant",
                "ts": 3500,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": "/app/.contremaitre/SETTLED_DESIGN.md",
                                "content": "y",
                            },
                        }
                    ]
                },
            }
        ],
    )
    _write_jsonl(paths.guardrail_events, _phase_guardrails())
    _write_jsonl(paths.review_cycles, [{"reviewer": "sim", "round": 1, "verdict": "APPROVED"}])
    phases = compute_phases_from_paths(paths)
    assert phases["grilling_exchanges"] == 1
    assert phases["impl_turns"] == 1


def test_compute_phases_codex_without_timestamp_is_none_not_garbage(tmp_path):
    # codex `item.completed` events carry no timestamp and write via opaque
    # bash, so no SETTLED anchor is recoverable → honest None, never the
    # old min-of-total-turns number. review_rounds still resolves.
    paths = _phase_paths(tmp_path)
    _write_jsonl(
        paths.raw_export,
        [
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
        ],
    )
    _write_jsonl(paths.guardrail_events, _phase_guardrails())
    _write_jsonl(paths.review_cycles, [{"reviewer": "sim", "round": 2, "verdict": "APPROVED"}])
    phases = compute_phases_from_paths(paths)
    assert phases["grilling_exchanges"] is None
    assert phases["impl_turns"] is None
    assert phases["pre_settled_agent_turns"] is None
    assert phases["review_rounds"] == 2  # always recoverable


def test_compute_phases_none_when_no_agent_turn_logged(tmp_path):
    # Pre-`actor-start` CLI runs logged sim turns but no agent ones — the
    # split can't be anchored even with a SETTLED write → None.
    paths = _phase_paths(tmp_path)
    _write_jsonl(
        paths.raw_export,
        [
            {
                "type": "tool_use",
                "timestamp": 3500,
                "part": {
                    "tool": "write",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": "/app/.contremaitre/SETTLED_DESIGN.md"},
                    },
                },
            }
        ],
    )
    _write_jsonl(
        paths.guardrail_events,
        [{"event": "actor_start", "role": "sim", "ts": 2000}],
    )
    _write_jsonl(paths.review_cycles, [])
    phases = compute_phases_from_paths(paths)
    assert phases["grilling_exchanges"] is None
    assert phases["review_rounds"] == 0


# --- pure core: events in, counts out (no paths / no disk) ------------------


def _settled_write(ts: int) -> dict:
    return {
        "type": "tool_use",
        "timestamp": ts,
        "part": {
            "tool": "write",
            "state": {
                "status": "completed",
                "input": {"filePath": "/app/.contremaitre/SETTLED_DESIGN.md", "content": "x"},
            },
        },
    }


def test_compute_phases_pure_core_splits_without_io():
    # Same fixture as the paths-based opencode test, fed directly as lists.
    phases = compute_phases(
        [_settled_write(3500)],
        _phase_guardrails(),
        [{"reviewer": "sim", "round": 1, "verdict": "APPROVED"}],
    )
    assert phases["grilling_exchanges"] == 1
    assert phases["impl_turns"] == 1
    assert phases["pre_settled_agent_turns"] == 1


def test_compute_phases_live_counts_before_settled_where_posthoc_is_none():
    # No SETTLED write yet, but an agent turn has started. live=True must emit
    # the accruing grilling counter; the default (post-hoc) must stay None so
    # eval/report history semantics for never-settled runs are unchanged.
    guardrails = [
        {"event": "actor_start", "role": "agent", "ts": 1000},
        {"event": "actor_start", "role": "sim", "ts": 2000},
    ]
    posthoc = compute_phases([], guardrails, [])
    assert posthoc["grilling_exchanges"] is None
    assert posthoc["impl_turns"] is None

    live = compute_phases([], guardrails, [], live=True)
    assert live["grilling_exchanges"] == 1  # min(1 agent, 1 sim)
    assert live["impl_turns"] == 0


def test_compute_phases_live_still_none_without_agent_turn():
    # `live` does not rescue a genuinely unrecoverable split: no agent
    # actor-start means no anchor, so both modes return None.
    guardrails = [{"event": "actor_start", "role": "sim", "ts": 2000}]
    live = compute_phases([], guardrails, [], live=True)
    assert live["grilling_exchanges"] is None


def test_compute_phases_review_rounds_use_max_round_not_row_count():
    # Retry / unavailable rows inflate len(cycles); max(round) is the canonical
    # counter, unified across every surface (was len() in the old TUI mirror).
    cycles = [
        {"reviewer": "sim", "round": 1, "verdict": "UNAVAILABLE"},
        {"reviewer": "sim", "round": 1, "verdict": "CHANGES_REQUESTED"},
        {"reviewer": "sim", "round": 2, "verdict": "APPROVED"},
    ]
    phases = compute_phases([_settled_write(3500)], _phase_guardrails(), cycles)
    assert phases["review_rounds"] == 2  # not len(cycles) == 3


def _paths(tmp_path: Path):
    return SimpleNamespace(
        raw_export=tmp_path / "raw_export.jsonl",
        sim_raw_export=tmp_path / "sim_raw_export.jsonl",
        review_cycles=tmp_path / "review_cycles.jsonl",
    )


def _tool_event(
    *,
    tool: str,
    timestamp: int,
    input_: dict | None = None,
    output: str = "",
) -> dict:
    return {
        "type": "tool_use",
        "timestamp": timestamp,
        "part": {
            "tool": tool,
            "state": {
                "status": "completed",
                "input": input_ or {},
                "output": output,
            },
        },
    }


def test_compute_flow_use_handles_text_events_without_numeric_timestamp(tmp_path):
    paths = _paths(tmp_path)
    _write_jsonl(
        paths.raw_export,
        [
            {"type": "text", "ts": "2026-01-01T00:00:00Z", "part": {"text": "one"}},
            {"type": "text", "ts": "2026-01-01T00:00:02Z", "part": {"text": "two"}},
        ],
    )

    flow_use = compute_flow_use(paths)

    assert flow_use["schema"] == "flow_use v1"
    assert flow_use["agent"]["tool_call_count"]["value"] == 0
    assert flow_use["agent"]["wall_seconds_total"]["value"] == 2.0


def test_sim_read_diff_accepts_review_mount_diff_path(tmp_path):
    paths = _paths(tmp_path)
    _write_jsonl(
        paths.sim_raw_export,
        [
            _tool_event(
                tool="read",
                timestamp=1_000,
                input_={"filePath": "/review/SETTLED_DESIGN.md"},
            ),
            _tool_event(
                tool="read",
                timestamp=2_000,
                input_={"filePath": "/review/diff.patch", "limit": 120},
            ),
        ],
    )

    flow_use = compute_flow_use(paths)

    assert flow_use["sim"]["sim_read_settled"]["value"] is True
    assert flow_use["sim"]["sim_read_diff"]["value"] is True
    assert flow_use["sim"]["sim_read_diff_partial"]["value"] is True


def test_apply_patch_is_code_edit_for_order_and_self_verification(tmp_path):
    paths = _paths(tmp_path)
    _write_jsonl(
        paths.raw_export,
        [
            _tool_event(
                tool="bash",
                timestamp=1_000,
                input_={"command": "pytest -q"},
                output="1 passed",
            ),
            _tool_event(
                tool="apply_patch",
                timestamp=2_000,
                input_={
                    "patchText": (
                        "*** Begin Patch\n"
                        "*** Update File: app/foo.py\n"
                        "@@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    )
                },
            ),
            _tool_event(
                tool="write",
                timestamp=3_000,
                input_={
                    "filePath": "/app/.contremaitre/SETTLED_DESIGN.md",
                    "content": "settled design after code edit",
                },
            ),
            _tool_event(
                tool="write",
                timestamp=4_000,
                input_={"filePath": "/app/.contremaitre/IMPLEMENTATION_COMPLETE"},
            ),
        ],
    )

    flow_use = compute_flow_use(paths)

    assert flow_use["agent"]["settled_write_before_first_code_edit"]["value"] is False
    assert flow_use["agent"]["self_verified"]["value"] is False


def _sim_grep_setup(tmp_path: Path, *, greps: list[dict], verdict: str):
    """Helper: write a SIM raw export with the given grep calls and a
    review_cycles file with a single SIM cycle whose summary is `verdict`."""
    paths = _paths(tmp_path)
    events = [
        _tool_event(tool="grep", timestamp=1_000 + i, input_=inp, output="ignored")
        for i, inp in enumerate(greps)
    ]
    _write_jsonl(paths.sim_raw_export, events)
    _write_jsonl(
        paths.review_cycles,
        [{"reviewer": "sim", "summary": verdict, "checks_performed": []}],
    )
    return paths


def test_sim_useful_call_ratio_counts_pattern_mentions_not_output(tmp_path):
    paths = _sim_grep_setup(
        tmp_path,
        greps=[{"pattern": "_compile_code"}, {"pattern": "render_html"}],
        verdict="reviewed _compile_code path, looks fine",
    )
    flow_use = compute_flow_use(paths)
    assert flow_use["sim"]["sim_useful_call_ratio"]["value"] == 0.5


def test_sim_useful_call_ratio_falls_back_through_regex_metachars(tmp_path):
    paths = _sim_grep_setup(
        tmp_path,
        greps=[{"pattern": r"_compile_\w+"}],
        verdict="all _compile_ helpers are covered",
    )
    flow_use = compute_flow_use(paths)
    assert flow_use["sim"]["sim_useful_call_ratio"]["value"] == 1.0


def test_sim_useful_call_ratio_ignores_short_patterns(tmp_path):
    paths = _sim_grep_setup(
        tmp_path,
        greps=[{"pattern": "id"}],
        verdict="id appears all over the place",
    )
    flow_use = compute_flow_use(paths)
    assert flow_use["sim"]["sim_useful_call_ratio"]["value"] == 0.0


def test_sim_useful_call_ratio_credits_path_or_include_args(tmp_path):
    paths = _sim_grep_setup(
        tmp_path,
        greps=[{"pattern": "x", "include": "tests/test_extract.py"}],
        verdict="checked tests/test_extract.py for coverage",
    )
    flow_use = compute_flow_use(paths)
    assert flow_use["sim"]["sim_useful_call_ratio"]["value"] == 1.0
