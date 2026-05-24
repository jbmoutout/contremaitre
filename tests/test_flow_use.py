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
