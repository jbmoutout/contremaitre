from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from contremaitre.flow_use import compute_flow_use


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


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
