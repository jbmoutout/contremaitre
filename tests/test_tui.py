"""Tests for tui.py pure helper functions.

All helpers tested here work on plain dicts (JSONL-deserialized events) and
do not require textual to be installed. They form the data-contract between
the JSONL files on disk and the TUI rendering layer.

The fixtures deliberately construct events using `events.<CONSTANT>` rather
than bare strings so that a rename of a constant (e.g. after PR #3's typed
event migration) breaks these tests instead of silently degrading the TUI.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from contremaitre import events
from contremaitre.tui import (
    _activity_state,
    _build_event_row,
    _fmt_elapsed,
    _impl_complete_in,
    _is_free_model,
    _latest_pending_tool,
    _read_jsonl,
    _render_guardrail,
    _review_summary,
    _settled_in,
    _state_breadcrumb,
    _task_count,
    _tests_summary,
    _text_event_count,
)


# ---------- helpers ----------


def _g(kind: str, **fields) -> dict:
    """Build a minimal guardrail event dict."""
    return {"ts": "2026-01-01T00:00:00.000Z", "event": kind, **fields}


def _actor_start(role: str) -> dict:
    return _g(events.OPENCODE_ACTOR_START, role=role)


# ===== _read_jsonl =====


def test_read_jsonl_missing_file(tmp_path):
    assert _read_jsonl(tmp_path / "nonexistent.jsonl") == []


def test_read_jsonl_empty_file(tmp_path):
    (tmp_path / "f.jsonl").write_text("")
    assert _read_jsonl(tmp_path / "f.jsonl") == []


def test_read_jsonl_skips_malformed_lines(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
    result = _read_jsonl(p)
    assert result == [{"a": 1}, {"b": 2}]


def test_read_jsonl_skips_non_dict_values(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\n[1, 2]\n42\n')
    result = _read_jsonl(p)
    assert result == [{"a": 1}]


# ===== _fmt_elapsed =====


def test_fmt_elapsed_none():
    assert _fmt_elapsed(None) == "—"


def test_fmt_elapsed_seconds():
    assert _fmt_elapsed(45) == "45s"


def test_fmt_elapsed_minutes():
    assert _fmt_elapsed(90) == "1m30s"


def test_fmt_elapsed_hours():
    assert _fmt_elapsed(3700) == "1h01m"


# ===== _activity_state =====


def test_activity_state_no_container_is_idle():
    assert _activity_state(container_present=False, file_age=0.5) == "idle"


def test_activity_state_container_recent_write_is_active():
    assert _activity_state(container_present=True, file_age=1.0) == "active"


def test_activity_state_container_stale_write_is_thinking():
    assert _activity_state(container_present=True, file_age=5.0) == "thinking"


def test_activity_state_container_no_file_is_thinking():
    assert _activity_state(container_present=True, file_age=None) == "thinking"


# ===== _is_free_model =====


def test_is_free_model_empty():
    assert not _is_free_model("")


def test_is_free_model_free_suffix():
    assert _is_free_model("opencode/claude-3-5-sonnet-free")


def test_is_free_model_big_pickle():
    assert _is_free_model("opencode/big-pickle")


def test_is_free_model_openrouter_free():
    assert _is_free_model("openrouter/google/gemini-flash:free")


def test_is_free_model_paid():
    assert not _is_free_model("openrouter/anthropic/claude-3-5-sonnet")


# ===== _state_breadcrumb =====
# These tests verify the TUI's event-string contract: if events.py renames
# a constant and dump() changes the serialized string, breadcrumb logic
# silently regresses. Tests use the constants as ground truth.


def test_breadcrumb_init_with_no_events():
    text = _state_breadcrumb([], terminal_stats=None)
    plain = text.plain
    assert "INIT" in plain
    assert "WORK" in plain


def test_breadcrumb_advances_to_work_on_agent_start():
    guardrails = [_actor_start("agent")]
    text = _state_breadcrumb(guardrails, terminal_stats=None)
    plain = text.plain
    assert "INIT" in plain and "WORK" in plain


def test_breadcrumb_advances_to_review_on_review_start():
    guardrails = [_actor_start("agent"), _actor_start("review")]
    text = _state_breadcrumb(guardrails, terminal_stats=None)
    assert "REVIEW" in text.plain


def test_breadcrumb_advances_to_published():
    guardrails = [
        _actor_start("agent"),
        _actor_start("review"),
        _g(events.PUBLISHED),
    ]
    text = _state_breadcrumb(guardrails, terminal_stats=None)
    assert "PUBLISHED" in text.plain


def test_breadcrumb_shows_blocked_on_publication_blocked():
    guardrails = [
        _actor_start("agent"),
        _g(events.PUBLICATION_BLOCKED),
    ]
    text = _state_breadcrumb(guardrails, terminal_stats=None)
    assert "BLOCKED" in text.plain


def test_breadcrumb_shows_failed_on_infra_failure():
    guardrails = [_g(events.INFRA_FAILURE)]
    text = _state_breadcrumb(guardrails, terminal_stats=None)
    assert "FAILED" in text.plain


# ===== _review_summary =====


def test_review_summary_empty():
    assert _review_summary([]) is None


def test_review_summary_approved():
    cycles = [{"round": 1, "verdict": "APPROVED"}]
    text = _review_summary(cycles)
    assert text is not None
    assert "R 1" in text.plain
    assert "✓" in text.plain


def test_review_summary_changes_requested():
    cycles = [{"round": 2, "verdict": "CHANGES_REQUESTED"}]
    text = _review_summary(cycles)
    assert text is not None
    assert "R 2" in text.plain
    assert "✗" in text.plain


def test_review_summary_uses_last_row():
    cycles = [
        {"round": 1, "verdict": "CHANGES_REQUESTED"},
        {"round": 2, "verdict": "APPROVED"},
    ]
    text = _review_summary(cycles)
    assert text is not None
    assert "R 2" in text.plain
    assert "✓" in text.plain


# ===== _tests_summary =====


def test_tests_summary_empty():
    assert _tests_summary([]) is None


def test_tests_summary_all_pass():
    runs = [{"returncode": 0}, {"returncode": 0}]
    text = _tests_summary(runs)
    assert text is not None
    assert "2/2" in text.plain
    assert "✓" in text.plain


def test_tests_summary_some_fail():
    runs = [{"returncode": 0}, {"returncode": 1}]
    text = _tests_summary(runs)
    assert text is not None
    assert "1/2" in text.plain
    assert "✗" in text.plain


# ===== _text_event_count / _task_count =====


def test_text_event_count():
    evts = [
        {"type": "text"},
        {"type": "tool_use"},
        {"type": "text"},
    ]
    assert _text_event_count(evts) == 2


def test_task_count():
    evts = [
        {"type": "tool_use", "part": {"tool": "task"}},
        {"type": "tool_use", "part": {"tool": "bash"}},
        {"type": "tool_use", "part": {"tool": "task"}},
    ]
    assert _task_count(evts) == 2


# ===== _settled_in =====


def _write_tool_event(tool: str, file_path: str = "", status: str = "completed") -> dict:
    return {
        "type": "tool_use",
        "part": {
            "tool": tool,
            "state": {
                "status": status,
                "input": {"filePath": file_path},
            },
        },
    }


def test_settled_in_false_when_empty():
    assert not _settled_in([])


def test_settled_in_true_on_settled_design_write():
    evts = [_write_tool_event("write", "/worktree/SETTLED_DESIGN.md")]
    assert _settled_in(evts)


def test_settled_in_false_when_not_completed():
    evts = [_write_tool_event("write", "/worktree/SETTLED_DESIGN.md", status="running")]
    assert not _settled_in(evts)


def test_impl_complete_in_true():
    evts = [_write_tool_event("write", "/worktree/IMPLEMENTATION_COMPLETE")]
    assert _impl_complete_in(evts)


# ===== _latest_pending_tool =====


def _pending_tool_event(tool: str, **inp) -> dict:
    return {
        "type": "tool_use",
        "part": {
            "tool": tool,
            "state": {"status": "running", "input": inp},
        },
    }


def test_latest_pending_tool_none_when_empty():
    assert _latest_pending_tool([]) is None


def test_latest_pending_tool_bash():
    evts = [_pending_tool_event("bash", command="pytest tests/")]
    result = _latest_pending_tool(evts)
    assert result is not None
    assert "bash" in result


def test_latest_pending_tool_read_shows_filename():
    evts = [_pending_tool_event("read", filePath="/src/foo.py")]
    result = _latest_pending_tool(evts)
    assert result is not None
    assert "foo.py" in result


def test_latest_pending_tool_completed_returns_none():
    evts = [
        {
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "state": {"status": "completed", "input": {"command": "ls"}},
            },
        }
    ]
    assert _latest_pending_tool(evts) is None


# ===== _render_guardrail =====
# Smoke-tests that each styled event kind produces output containing the kind
# string. The main guard here is that the style-dispatch branches don't raise.


@pytest.mark.parametrize(
    "kind",
    [
        events.PUBLISHED,
        events.PUBLICATION_BLOCKED,
        events.INFRA_FAILURE,
        events.REVISION_REQUESTED,
        events.REVIEW_VERDICT,
        events.CHECK_COMPLETED,
        events.HARD_GATES_CHECKED,
        events.OPENCODE_ACTOR_START,
        events.WORK_SESSION_END,
        events.TURN_CAP,
    ],
)
def test_render_guardrail_contains_kind(kind):
    ev = _g(kind, verdict="APPROVED", passed=True, returncode=0, role="agent")
    body = _render_guardrail(ev)
    assert kind in body.plain


def test_render_guardrail_recovery_kind_substring():
    ev = {"ts": "2026-01-01T00:00:00.000Z", "kind": events.SQLITE_RECOVERY_SILENT_STALL}
    body = _render_guardrail(ev)
    assert events.SQLITE_RECOVERY_SILENT_STALL in body.plain


# ===== _build_event_row =====


def test_build_event_row_text():
    ev = {"type": "text", "part": {"text": "hello world"}, "timestamp": None}
    marker, ts, typ, tool, body = _build_event_row(ev)
    assert "text" in typ.plain


def test_build_event_row_tool_use():
    ev = {
        "type": "tool_use",
        "part": {"tool": "bash", "state": {"status": "completed", "input": {"command": "ls"}}},
        "timestamp": None,
    }
    marker, ts, typ, tool, body = _build_event_row(ev)
    assert "bash" in tool.plain


def test_build_event_row_unknown_type():
    ev = {"type": "something_new", "timestamp": None}
    marker, ts, typ, tool, body = _build_event_row(ev)
    assert "something_new" in typ.plain
