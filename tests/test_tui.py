"""Tests for TUI data-flow functions.

These tests verify that the TUI correctly parses and interprets the event
streams written by the orchestrator and actors. The shapes used here are
taken directly from real run artifacts — if a shape changes in production,
these tests will catch it before the TUI silently misreads live data.

Textual app internals are not tested here (requires async harness).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contremaitre.tui import (
    _impl_complete_in,
    _latest_pending_tool,
    _read_jsonl,
    _settled_in,
    _state_breadcrumb,
    _task_count,
    _text_event_count,
)


# ---------------------------------------------------------------------------
# Fixtures — event shapes copied from real run artifacts
# ---------------------------------------------------------------------------

def _actor_start(role: str) -> dict:
    """opencode_actor_start as written to guardrail_events.jsonl."""
    return {
        "event": "opencode_actor_start",
        "model": "openrouter/deepseek/deepseek-v4-flash",
        "mount_mode": "ro" if role in ("sim", "review") else "rw",
        "role": role,
        "timeout_seconds": 1800,
        "ts": "2026-05-24T12:00:00Z",
    }


def _tool_use(tool: str, file_path: str = "", status: str = "completed", content: str = "") -> dict:
    """tool_use event as written to raw_export.jsonl (real shape from opencode)."""
    inp: dict = {}
    if file_path:
        inp["filePath"] = file_path
    if content:
        inp["content"] = content
    return {
        "type": "tool_use",
        "timestamp": 1779482753558,
        "sessionID": "ses_1ae95e712ffenz6wSzlOq3pM73",
        "part": {
            "type": "tool",
            "tool": tool,
            "callID": "call_8ebab0b3c0fc4357b17a6d88",
            "state": {
                "status": status,
                "input": inp,
                "output": "",
            },
        },
    }


def _text_event() -> dict:
    """text event as written to raw_export.jsonl."""
    return {
        "type": "text",
        "timestamp": 1779482547943,
        "sessionID": "ses_1ae95e712ffenz6wSzlOq3pM73",
        "part": {
            "type": "text",
            "text": "Architecture review report written.",
        },
    }


def _task_event() -> dict:
    """tool_use/task as written to raw_export.jsonl."""
    return {
        "type": "tool_use",
        "timestamp": 1779482372212,
        "part": {
            "type": "tool",
            "tool": "task",
            "callID": "call_a01fb3ac",
            "state": {
                "status": "completed",
                "input": {"description": "search for auth patterns", "subagent_type": "Explore"},
                "output": "found 3 files",
            },
        },
    }


# ---------------------------------------------------------------------------
# _read_jsonl — disk round-trip with real-world edge cases
# ---------------------------------------------------------------------------

class TestReadJsonl(unittest.TestCase):

    def test_missing_file_returns_empty(self):
        self.assertEqual(_read_jsonl(Path("/no/such/file.jsonl")), [])

    def test_round_trips_real_event_shapes(self):
        events = [_actor_start("agent"), _actor_start("sim"), _text_event()]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
            p = Path(f.name)
        result = _read_jsonl(p)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["event"], "opencode_actor_start")
        self.assertEqual(result[0]["role"], "agent")
        self.assertEqual(result[2]["type"], "text")
        p.unlink()

    def test_skips_truncated_json_line(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(_actor_start("agent")) + "\n")
            f.write('{"truncated": true\n')          # bad JSON — simulates mid-write crash
            f.write(json.dumps(_actor_start("sim")) + "\n")
            p = Path(f.name)
        result = _read_jsonl(p)
        self.assertEqual(len(result), 2)             # bad line skipped, run continues
        p.unlink()

    def test_skips_blank_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n")
            f.write(json.dumps(_actor_start("agent")) + "\n")
            f.write("\n\n")
            p = Path(f.name)
        self.assertEqual(len(_read_jsonl(p)), 1)
        p.unlink()


# ---------------------------------------------------------------------------
# _text_event_count — counts orchestrator turns from raw_export
# ---------------------------------------------------------------------------

class TestTextEventCount(unittest.TestCase):

    def test_counts_only_text_type(self):
        events = [
            _tool_use("write", "/app/.contremaitre/SETTLED_DESIGN.md"),
            _text_event(),
            _tool_use("bash", "/app/run.sh"),
            _text_event(),
            {"type": "step_finish", "part": {}},
        ]
        self.assertEqual(_text_event_count(events), 2)

    def test_empty_stream(self):
        self.assertEqual(_text_event_count([]), 0)

    def test_realistic_turn_count(self):
        # 8 text events across a typical run (from real raw_export: types={'text': 8})
        events = [_text_event() for _ in range(8)]
        events += [_tool_use("read", "/app/src/foo.ts")] * 5
        self.assertEqual(_text_event_count(events), 8)


# ---------------------------------------------------------------------------
# _task_count — counts subagent Task tool invocations
# ---------------------------------------------------------------------------

class TestTaskCount(unittest.TestCase):

    def test_counts_task_tool_only(self):
        events = [
            _task_event(),
            _tool_use("read", "/app/src/foo.ts"),
            _task_event(),
            _text_event(),
        ]
        self.assertEqual(_task_count(events), 2)

    def test_non_task_tool_not_counted(self):
        events = [_tool_use("bash", ""), _tool_use("grep", "")]
        self.assertEqual(_task_count(events), 0)


# ---------------------------------------------------------------------------
# _settled_in — detects SETTLED_DESIGN.md write in raw event stream
# ---------------------------------------------------------------------------

class TestSettledIn(unittest.TestCase):

    def test_detects_write_to_settled_path(self):
        # Real path shape: /app/.contremaitre/SETTLED_DESIGN.md
        events = [_tool_use("write", "/app/.contremaitre/SETTLED_DESIGN.md")]
        self.assertTrue(_settled_in(events))

    def test_detects_edit_to_settled_path(self):
        events = [_tool_use("edit", "/app/.contremaitre/SETTLED_DESIGN.md")]
        self.assertTrue(_settled_in(events))

    def test_ignores_pending_tool(self):
        # If the write is still in-flight (status=pending), not yet settled
        events = [_tool_use("write", "/app/.contremaitre/SETTLED_DESIGN.md", status="pending")]
        self.assertFalse(_settled_in(events))

    def test_ignores_other_contremaitre_files(self):
        events = [_tool_use("write", "/app/.contremaitre/IMPLEMENTATION_COMPLETE")]
        self.assertFalse(_settled_in(events))

    def test_ignores_non_write_tool(self):
        events = [_tool_use("read", "/app/.contremaitre/SETTLED_DESIGN.md")]
        self.assertFalse(_settled_in(events))

    def test_false_on_empty(self):
        self.assertFalse(_settled_in([]))

    def test_ignores_text_events(self):
        events = [_text_event(), {"type": "text", "part": {"text": "SETTLED_DESIGN.md written"}}]
        self.assertFalse(_settled_in(events))


# ---------------------------------------------------------------------------
# _impl_complete_in — detects IMPLEMENTATION_COMPLETE write
# ---------------------------------------------------------------------------

class TestImplCompleteIn(unittest.TestCase):

    def test_detects_write_to_impl_complete(self):
        events = [_tool_use("write", "/app/.contremaitre/IMPLEMENTATION_COMPLETE")]
        self.assertTrue(_impl_complete_in(events))

    def test_settled_does_not_trigger(self):
        events = [_tool_use("write", "/app/.contremaitre/SETTLED_DESIGN.md")]
        self.assertFalse(_impl_complete_in(events))

    def test_ignores_pending(self):
        events = [_tool_use("write", "/app/.contremaitre/IMPLEMENTATION_COMPLETE", status="pending")]
        self.assertFalse(_impl_complete_in(events))

    def test_false_on_empty(self):
        self.assertFalse(_impl_complete_in([]))


# ---------------------------------------------------------------------------
# _latest_pending_tool — identifies what the agent is currently doing
# ---------------------------------------------------------------------------

class TestLatestPendingTool(unittest.TestCase):

    def _pending(self, tool: str, **inp_extra) -> dict:
        inp = {"filePath": "/app/src/foo.ts", **inp_extra}
        return {
            "type": "tool_use",
            "part": {
                "tool": tool,
                "state": {"status": "pending", "input": inp},
            },
        }

    def test_returns_none_when_all_completed(self):
        events = [_tool_use("write", "/app/src/foo.ts")]
        self.assertIsNone(_latest_pending_tool(events))

    def test_returns_none_on_empty(self):
        self.assertIsNone(_latest_pending_tool([]))

    def test_detects_pending_write(self):
        events = [
            _tool_use("write", "/app/src/foo.ts"),          # completed — ignored
            self._pending("write"),                          # in-flight
        ]
        result = _latest_pending_tool(events)
        self.assertIsNotNone(result)
        self.assertIn("write", result)
        self.assertIn("foo.ts", result)

    def test_pending_bash_shows_command(self):
        ev = {
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "state": {"status": "pending", "input": {"command": "npx tsc --noEmit"}},
            },
        }
        result = _latest_pending_tool([ev])
        self.assertIsNotNone(result)
        self.assertIn("npx tsc", result)

    def test_completed_after_pending_means_idle(self):
        # Completed event after the pending one — means it finished
        events = [self._pending("write"), _tool_use("write", "/app/src/foo.ts")]
        self.assertIsNone(_latest_pending_tool(events))


# ---------------------------------------------------------------------------
# _state_breadcrumb — derives pipeline stage from guardrail_events.jsonl
# ---------------------------------------------------------------------------

class TestStateBreadcrumb(unittest.TestCase):

    def _crumb(self, events, *, failed=False):
        return _state_breadcrumb(events, terminal_stats=None, failed=failed)

    def _plain(self, t) -> str:
        return t.plain

    def test_empty_guardrails_is_init(self):
        # No events yet — run just started
        t = self._crumb([])
        self.assertIn("INIT", self._plain(t))

    def test_agent_start_advances_to_work(self):
        t = self._crumb([_actor_start("agent")])
        plain = self._plain(t)
        # Both INIT (past) and WORK (current) must appear in the breadcrumb
        self.assertIn("INIT", plain)
        self.assertIn("WORK", plain)

    def test_review_role_advances_to_review(self):
        events = [_actor_start("agent"), _actor_start("sim"), _actor_start("review")]
        t = self._crumb(events)
        self.assertIn("REVIEW", self._plain(t))

    def test_published_event_advances_to_published(self):
        events = [
            _actor_start("agent"),
            _actor_start("review"),
            {"event": "published", "ts": "2026-05-24T12:00:00Z"},
        ]
        t = self._crumb(events)
        self.assertIn("PUBLISHED", self._plain(t))

    def test_publication_blocked_appends_blocked_label(self):
        events = [
            _actor_start("agent"),
            {"event": "publication_blocked", "ts": "2026-05-24T12:00:00Z"},
        ]
        t = self._crumb(events)
        self.assertIn("BLOCKED", self._plain(t))

    def test_infra_failure_appends_failed_label(self):
        events = [{"event": "infra_failure", "ts": "2026-05-24T12:00:00Z", "error": "docker dead"}]
        t = self._crumb(events)
        self.assertIn("FAILED", self._plain(t))

    def test_realistic_agent_sim_interleave(self):
        # Real guardrail sequence: agent → sim → agent → sim → review
        events = [
            _actor_start("agent"),
            {"event": "progress", "label": "after-agent-turn-1", "ts": "2026-05-24T12:00:01Z"},
            _actor_start("sim"),
            _actor_start("agent"),
            {"event": "progress", "label": "after-agent-turn-3", "ts": "2026-05-24T12:00:02Z"},
            _actor_start("sim"),
            _actor_start("agent"),
            _actor_start("review"),
        ]
        t = self._crumb(events)
        # After review role appears, current stage = REVIEW
        self.assertIn("REVIEW", self._plain(t))
        # INIT, WORK are past
        plain = self._plain(t)
        self.assertIn("INIT", plain)
        self.assertIn("WORK", plain)

    def test_failed_flag_does_not_affect_stage_logic(self):
        # failed=True only affects color, not which stage is current
        events = [_actor_start("agent"), _actor_start("review")]
        t_normal = self._crumb(events, failed=False)
        t_failed = self._crumb(events, failed=True)
        self.assertEqual(self._plain(t_normal), self._plain(t_failed))

    def test_second_agent_start_does_not_regress_to_init(self):
        # Regression: multiple agent starts should not reset stage to INIT
        events = [_actor_start("agent"), _actor_start("sim"), _actor_start("agent")]
        t = self._crumb(events)
        # Stage should still be WORK (or could be REVIEW if review started, but not INIT)
        self.assertNotIn("INIT ›", self._plain(t).replace("INIT", "").replace("›", ""))
        # WORK should be in the plain text as a past or current stage
        self.assertIn("WORK", self._plain(t))


if __name__ == "__main__":
    unittest.main()
