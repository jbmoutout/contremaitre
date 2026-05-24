"""Tests for TUI pure functions.

The Textual app itself requires an async harness; we don't test it here.
What we do test are the pure functions that have driven real bugs:
  - _state_breadcrumb  (wrong colors, stuck-stage detection)
  - _review_summary    (verdict icons, round count)
  - _tests_summary     (pass/fail ratio)
  - _render_guardrail  (semantic icons/colors for check/review/publish events)
  - _settled_in / _impl_complete_in  (gate detection from raw events)
  - _fmt_elapsed       (time formatting edge cases)
  - _text_event_count / _task_count  (turn/subagent metrics)
  - _short_model / _is_free_model    (model label helpers)
  - _read_jsonl        (resilience to bad input)
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contremaitre.tui import (
    _PAL_BRIGHT,
    _PAL_DIM,
    _PAL_ERROR,
    _PAL_SUCCESS,
    _PAL_TEXT,
    _PAL_WARN,
    _fmt_elapsed,
    _impl_complete_in,
    _is_free_model,
    _read_jsonl,
    _render_guardrail,
    _review_summary,
    _settled_in,
    _short_model,
    _state_breadcrumb,
    _task_count,
    _tests_summary,
    _text_event_count,
)


# ---------- helpers ----------

def _plain(text) -> str:
    """Strip Rich markup from a Text object → plain string."""
    return text.plain


def _styles(text) -> list[tuple[str, str]]:
    """Return [(span_text, style_string), ...] for a Rich Text."""
    return [(span.plain, str(span.style)) for span in text._spans]  # type: ignore[attr-defined]


def _style_of(text, substring: str) -> str | None:
    """Return the style applied to the first span containing `substring`."""
    for span in text._spans:  # type: ignore[attr-defined]
        if substring in text.plain[span.start:span.end]:
            return str(span.style)
    return None


# ---------- _fmt_elapsed ----------

class TestFmtElapsed(unittest.TestCase):
    def test_none(self):
        self.assertEqual(_fmt_elapsed(None), "—")

    def test_seconds(self):
        self.assertEqual(_fmt_elapsed(0), "0s")
        self.assertEqual(_fmt_elapsed(45), "45s")
        self.assertEqual(_fmt_elapsed(59), "59s")

    def test_minutes(self):
        self.assertEqual(_fmt_elapsed(60), "1m00s")
        self.assertEqual(_fmt_elapsed(643), "10m43s")
        self.assertEqual(_fmt_elapsed(3599), "59m59s")

    def test_hours(self):
        self.assertEqual(_fmt_elapsed(3600), "1h00m")
        self.assertEqual(_fmt_elapsed(3661), "1h01m")


# ---------- _short_model / _is_free_model ----------

class TestModelHelpers(unittest.TestCase):
    def test_short_model_strips_prefix(self):
        self.assertEqual(_short_model("openrouter/deepseek/deepseek-v4-flash"), "deepseek-v4-flash")
        self.assertEqual(_short_model("opencode/big-pickle"), "big-pickle")

    def test_short_model_bare(self):
        self.assertEqual(_short_model("gpt-4o"), "gpt-4o")

    def test_short_model_empty(self):
        self.assertEqual(_short_model(""), "?")

    def test_is_free_model_zen(self):
        self.assertTrue(_is_free_model("opencode/deepseek-v4-flash-free"))

    def test_is_free_model_openrouter_suffix(self):
        self.assertTrue(_is_free_model("openrouter/qwen/qwen-2.5-coder:free"))

    def test_is_free_model_big_pickle(self):
        self.assertTrue(_is_free_model("opencode/big-pickle"))

    def test_is_free_model_paid(self):
        self.assertFalse(_is_free_model("openrouter/deepseek/deepseek-v4"))
        self.assertFalse(_is_free_model("openai/gpt-4o"))


# ---------- _read_jsonl ----------

class TestReadJsonl(unittest.TestCase):
    def test_missing_file(self):
        self.assertEqual(_read_jsonl(Path("/no/such/file.jsonl")), [])

    def test_valid(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"a": 1}\n{"b": 2}\n')
            p = Path(f.name)
        self.assertEqual(_read_jsonl(p), [{"a": 1}, {"b": 2}])
        p.unlink()

    def test_skips_blank_lines_and_bad_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"ok": true}\n\nnot-json\n{"also": "ok"}\n')
            p = Path(f.name)
        result = _read_jsonl(p)
        self.assertEqual(len(result), 2)
        p.unlink()

    def test_skips_non_dict_rows(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('[1,2,3]\n{"good": true}\n')
            p = Path(f.name)
        result = _read_jsonl(p)
        self.assertEqual(result, [{"good": True}])
        p.unlink()


# ---------- _text_event_count / _task_count ----------

class TestEventCounts(unittest.TestCase):
    def _ev(self, t, **extra):
        return {"type": t, **extra}

    def test_text_event_count(self):
        events = [self._ev("text"), self._ev("tool_use"), self._ev("text"), self._ev("step_finish")]
        self.assertEqual(_text_event_count(events), 2)

    def test_text_event_count_empty(self):
        self.assertEqual(_text_event_count([]), 0)

    def test_task_count(self):
        events = [
            {"type": "tool_use", "part": {"tool": "task"}},
            {"type": "tool_use", "part": {"tool": "read"}},
            {"type": "tool_use", "part": {"tool": "task"}},
            {"type": "text"},
        ]
        self.assertEqual(_task_count(events), 2)


# ---------- _settled_in ----------

def _tool_event(tool: str, fp: str, status: str = "completed") -> dict:
    return {
        "type": "tool_use",
        "part": {
            "tool": tool,
            "state": {"status": status, "input": {"filePath": fp}},
        },
    }


class TestSettledIn(unittest.TestCase):
    def test_empty(self):
        self.assertFalse(_settled_in([]))

    def test_write_settled(self):
        ev = _tool_event("write", ".contremaitre/SETTLED_DESIGN.md")
        self.assertTrue(_settled_in([ev]))

    def test_edit_settled(self):
        ev = _tool_event("edit", "/app/.contremaitre/SETTLED_DESIGN.md")
        self.assertTrue(_settled_in([ev]))

    def test_wrong_file(self):
        ev = _tool_event("write", ".contremaitre/IMPLEMENTATION_COMPLETE")
        self.assertFalse(_settled_in([ev]))

    def test_not_completed_skipped(self):
        ev = _tool_event("write", ".contremaitre/SETTLED_DESIGN.md", status="pending")
        self.assertFalse(_settled_in([ev]))

    def test_non_tool_event_ignored(self):
        self.assertFalse(_settled_in([{"type": "text", "part": {"text": "SETTLED_DESIGN.md"}}]))


# ---------- _impl_complete_in ----------

class TestImplCompleteIn(unittest.TestCase):
    def test_empty(self):
        self.assertFalse(_impl_complete_in([]))

    def test_write_impl_complete(self):
        ev = _tool_event("write", ".contremaitre/IMPLEMENTATION_COMPLETE")
        self.assertTrue(_impl_complete_in([ev]))

    def test_settled_does_not_trigger(self):
        ev = _tool_event("write", ".contremaitre/SETTLED_DESIGN.md")
        self.assertFalse(_impl_complete_in([ev]))


# ---------- _review_summary ----------

class TestReviewSummary(unittest.TestCase):
    def test_empty(self):
        self.assertNone(_review_summary([]))

    def assertNone(self, val):  # noqa: N802
        self.assertIsNone(val)

    def test_approved(self):
        t = _review_summary([{"round": 1, "verdict": "APPROVED"}])
        self.assertIn("R 1", _plain(t))
        self.assertIn("✓", _plain(t))
        self.assertIn(_PAL_SUCCESS, _style_of(t, "✓") or "")

    def test_changes_requested(self):
        t = _review_summary([{"round": 1, "verdict": "CHANGES_REQUESTED"}])
        self.assertIn("✗", _plain(t))
        self.assertIn(_PAL_WARN, _style_of(t, "✗") or "")

    def test_two_rounds_approved(self):
        t = _review_summary([
            {"round": 1, "verdict": "CHANGES_REQUESTED"},
            {"round": 2, "verdict": "APPROVED"},
        ])
        self.assertIn("R 2", _plain(t))
        self.assertIn("✓", _plain(t))

    def test_round_falls_back_to_list_length(self):
        t = _review_summary([{"verdict": "APPROVED"}])
        self.assertIn("R 1", _plain(t))

    def test_unknown_verdict(self):
        t = _review_summary([{"round": 1, "verdict": "SOMETHING_ELSE"}])
        self.assertIn("·", _plain(t))


# ---------- _tests_summary ----------

class TestTestsSummary(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(_tests_summary([]))

    def test_all_pass(self):
        t = _tests_summary([{"returncode": 0}, {"returncode": 0}])
        self.assertIn("tests 2/2", _plain(t))
        self.assertIn("✓", _plain(t))
        self.assertIn(_PAL_SUCCESS, _style_of(t, "✓") or "")

    def test_some_fail(self):
        t = _tests_summary([{"returncode": 0}, {"returncode": 1}])
        self.assertIn("tests 1/2", _plain(t))
        self.assertIn("✗", _plain(t))
        self.assertIn(_PAL_ERROR, _style_of(t, "✗") or "")

    def test_all_fail(self):
        t = _tests_summary([{"returncode": 1}])
        self.assertIn("tests 0/1", _plain(t))
        self.assertIn("✗", _plain(t))


# ---------- _state_breadcrumb ----------

def _guardrail(event: str, **kw) -> dict:
    return {"event": event, **kw}


class TestStateBreadcrumb(unittest.TestCase):
    def _crumb(self, events, *, terminal_stats=None, failed=False):
        return _state_breadcrumb(events, terminal_stats=terminal_stats, failed=failed)

    def test_init_state(self):
        t = self._crumb([])
        plain = _plain(t)
        self.assertIn("INIT", plain)
        self.assertIn("WORK", plain)
        # INIT is current → bright; others dim
        self.assertIn(_PAL_BRIGHT, _style_of(t, "INIT") or "")
        self.assertIn(_PAL_DIM, _style_of(t, "WORK") or "")

    def test_work_state(self):
        events = [_guardrail("opencode_actor_start", role="agent")]
        t = self._crumb(events)
        self.assertIn(_PAL_SUCCESS, _style_of(t, "INIT") or "")   # past → green
        self.assertIn(_PAL_BRIGHT, _style_of(t, "WORK") or "")    # current → bright

    def test_review_state(self):
        events = [
            _guardrail("opencode_actor_start", role="agent"),
            _guardrail("opencode_actor_start", role="review"),
        ]
        t = self._crumb(events)
        self.assertIn(_PAL_BRIGHT, _style_of(t, "REVIEW") or "")

    def test_published_state_green(self):
        events = [
            _guardrail("opencode_actor_start", role="agent"),
            _guardrail("opencode_actor_start", role="review"),
            _guardrail("published"),
        ]
        t = self._crumb(events)
        # PUBLISHED is terminal success → bold green, not bold white
        style = _style_of(t, "PUBLISHED") or ""
        self.assertIn(_PAL_SUCCESS, style)
        self.assertNotIn(_PAL_BRIGHT, style)

    def test_failed_state_colors_current_red(self):
        events = [
            _guardrail("opencode_actor_start", role="agent"),
            _guardrail("opencode_actor_start", role="review"),
        ]
        t = self._crumb(events, failed=True)
        self.assertIn(_PAL_ERROR, _style_of(t, "REVIEW") or "")

    def test_blocked_appends_label(self):
        events = [_guardrail("publication_blocked")]
        t = self._crumb(events)
        self.assertIn("BLOCKED", _plain(t))
        self.assertIn(_PAL_ERROR, _style_of(t, "BLOCKED") or "")

    def test_infra_failure_appends_label(self):
        events = [_guardrail("infra_failure")]
        t = self._crumb(events)
        self.assertIn("FAILED", _plain(t))

    def test_past_stages_green(self):
        events = [
            _guardrail("opencode_actor_start", role="agent"),
            _guardrail("opencode_actor_start", role="review"),
            _guardrail("published"),
        ]
        t = self._crumb(events)
        for stage in ("INIT", "WORK", "REVIEW", "APPROVED"):
            self.assertIn(_PAL_SUCCESS, _style_of(t, stage) or "", msg=f"{stage} should be green")


# ---------- _render_guardrail ----------

class TestRenderGuardrail(unittest.TestCase):
    def _g(self, event_kind: str, **kw) -> dict:
        return {"event": event_kind, "ts": "2026-05-24T12:00:00Z", **kw}

    def _plain(self, event_kind, **kw):
        return _render_guardrail(self._g(event_kind, **kw)).plain

    def _has_style(self, text_obj, substring: str, color: str) -> bool:
        for span in text_obj._spans:  # type: ignore[attr-defined]
            segment = text_obj.plain[span.start:span.end]
            if substring in segment and color in str(span.style):
                return True
        return False

    def test_check_completed_pass_icon(self):
        ev = self._g("check_completed", returncode=0, cmd="npx tsc", duration_seconds=2.1)
        t = _render_guardrail(ev)
        self.assertIn("✓", t.plain)
        self.assertTrue(self._has_style(t, "✓", _PAL_SUCCESS))

    def test_check_completed_fail_icon(self):
        ev = self._g("check_completed", returncode=1, cmd="npx tsc", duration_seconds=1.0)
        t = _render_guardrail(ev)
        self.assertIn("✗", t.plain)
        self.assertTrue(self._has_style(t, "✗", _PAL_ERROR))

    def test_check_completed_fail_shows_rc(self):
        ev = self._g("check_completed", returncode=2, cmd="pnpm test")
        self.assertIn("rc=2", _render_guardrail(ev).plain)

    def test_check_completed_pass_shows_cmd(self):
        ev = self._g("check_completed", returncode=0, cmd="npx tsc --noEmit", duration_seconds=1.5)
        plain = _render_guardrail(ev).plain
        self.assertIn("npx tsc --noEmit", plain)
        self.assertIn("1.5s", plain)

    def test_review_verdict_approved(self):
        ev = self._g("review_verdict", verdict="APPROVED", round=1)
        t = _render_guardrail(ev)
        self.assertIn("✓", t.plain)
        self.assertTrue(self._has_style(t, "✓", _PAL_SUCCESS))

    def test_review_verdict_changes_requested(self):
        ev = self._g("review_verdict", verdict="CHANGES_REQUESTED", round=1)
        t = _render_guardrail(ev)
        self.assertIn("✗", t.plain)
        self.assertTrue(self._has_style(t, "✗", _PAL_WARN))

    def test_published_icon(self):
        ev = self._g("published")
        t = _render_guardrail(ev)
        self.assertIn("✓", t.plain)
        self.assertTrue(self._has_style(t, "✓", _PAL_SUCCESS))

    def test_hard_gates_pass(self):
        ev = self._g("hard_gates_checked", passed=True)
        t = _render_guardrail(ev)
        self.assertIn("✓", t.plain)

    def test_hard_gates_fail(self):
        ev = self._g("hard_gates_checked", passed=False)
        t = _render_guardrail(ev)
        self.assertIn("✗", t.plain)
        self.assertTrue(self._has_style(t, "✗", _PAL_ERROR))

    def test_infra_failure_red(self):
        ev = self._g("infra_failure", error="docker daemon not running")
        t = _render_guardrail(ev)
        self.assertTrue(self._has_style(t, "infra_failure", _PAL_ERROR))

    def test_recovery_orange(self):
        ev = self._g("recovery_sqlite_silent_stall", recovered_chars=325)
        t = _render_guardrail(ev)
        self.assertTrue(self._has_style(t, "recovery_sqlite_silent_stall", _PAL_WARN))

    def test_stdout_head_shown_on_failure(self):
        ev = self._g("check_completed", returncode=1, stdout_head="error TS2345: ...")
        plain = _render_guardrail(ev).plain
        self.assertIn("error TS2345", plain)

    def test_unknown_event_dim(self):
        ev = self._g("some_internal_event")
        t = _render_guardrail(ev)
        self.assertTrue(self._has_style(t, "some_internal_event", "dim"))

    def test_revision_requested_orange(self):
        ev = self._g("revision_requested", round=1)
        t = _render_guardrail(ev)
        self.assertTrue(self._has_style(t, "revision_requested", _PAL_WARN))


if __name__ == "__main__":
    unittest.main()
