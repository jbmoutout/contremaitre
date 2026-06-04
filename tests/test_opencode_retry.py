"""Unit tests for the opencode retry surface.

Covers the helpers wired up to support the transient-error retry loop in
`OpencodeActorRunner._opencode_turn`:

- `_detect_provider_fast_fail` correctly identifies quota vs transient
  markers from both the stdout JSONL stream and the internal log file.
- `_classify_fast_fail_marker` routes each marker to the right event
  kind, so the caller can decide retry-vs-terminal.
- `events_offset` plumbing keeps attempt N+1 from re-detecting attempt
  N's error events.
- The retry wrapper retries on `PROVIDER_TRANSIENT_ERROR`, bypasses
  retry on `PROVIDER_QUOTA_EXHAUSTED`, and raises with an "(after N
  retries)" suffix once attempts are exhausted.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contremaitre import events
from contremaitre.actors import (
    ActorError,
    ActorOutput,
    OpencodeActorRunner,
    _classify_fast_fail_marker,
    _count_jsonl_events,
    _detect_provider_fast_fail,
    _latest_error_after_text_count,
    _latest_internal_log_size,
)
from contremaitre.fixture import init_fixture
from contremaitre.models import ActorMode, RunConfig
from contremaitre.paths import build_run_paths, new_run_id


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


class FastFailDetectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.stream = self.root / "raw_export.jsonl"
        self.state_dir = self.root / "state"
        (self.state_dir / "log").mkdir(parents=True)

    def test_quota_marker_in_stream_detected(self) -> None:
        _write_jsonl(
            self.stream,
            [
                {"type": "error", "error": "FreeUsageLimitError: per-day quota"},
            ],
        )
        marker = _detect_provider_fast_fail(self.stream, 0, state_dir=self.state_dir)
        self.assertEqual(marker, "FreeUsageLimitError")
        self.assertEqual(
            _classify_fast_fail_marker(marker),
            events.PROVIDER_QUOTA_EXHAUSTED,
        )

    def test_transient_marker_in_stream_detected(self) -> None:
        _write_jsonl(
            self.stream,
            [
                {"type": "error", "error": "Provider returned error"},
            ],
        )
        marker = _detect_provider_fast_fail(self.stream, 0, state_dir=self.state_dir)
        self.assertEqual(marker, "Provider returned error")
        self.assertEqual(
            _classify_fast_fail_marker(marker),
            events.PROVIDER_TRANSIENT_ERROR,
        )

    def test_marker_in_internal_log_detected(self) -> None:
        # Empty stream — marker is only in the opencode log.
        _write_jsonl(self.stream, [])
        (self.state_dir / "log" / "2026-06-04T120000.log").write_text(
            "INFO start\nERROR service=llm error=Provider returned error stream error\n",
            encoding="utf-8",
        )
        marker = _detect_provider_fast_fail(self.stream, 0, state_dir=self.state_dir)
        self.assertEqual(marker, "Provider returned error")

    def test_no_marker_returns_none(self) -> None:
        _write_jsonl(
            self.stream,
            [{"type": "text", "part": {"text": "hello"}}],
        )
        self.assertIsNone(_detect_provider_fast_fail(self.stream, 0, state_dir=self.state_dir))

    def test_new_text_after_baseline_cancels_subsequent_match(self) -> None:
        # A real reply lands after baseline → any later error is stale
        # tail noise from before the reply arrived.
        _write_jsonl(
            self.stream,
            [
                {"type": "text", "part": {"text": "reply"}},
                {"type": "error", "error": "Provider returned error"},
            ],
        )
        self.assertIsNone(_detect_provider_fast_fail(self.stream, 0, state_dir=self.state_dir))

    def test_events_offset_skips_prior_attempt_error(self) -> None:
        # Simulates: attempt 1 wrote an error event (index 0), attempt 2
        # started — events_offset=1 should ignore the prior error.
        _write_jsonl(
            self.stream,
            [
                {"type": "error", "error": "Provider returned error"},
            ],
        )
        self.assertEqual(
            _detect_provider_fast_fail(self.stream, 0, state_dir=self.state_dir, events_offset=0),
            "Provider returned error",
        )
        self.assertIsNone(
            _detect_provider_fast_fail(self.stream, 0, state_dir=self.state_dir, events_offset=1)
        )


class LatestErrorOffsetTest(unittest.TestCase):
    def test_events_offset_excludes_prior_attempt(self) -> None:
        # Stream shape mirrors what the retry loop sees: a text event
        # from the prior turn, then attempt 1 of the current turn wrote
        # an error event with no text. baseline_text_count=1 marks the
        # turn boundary; events_offset=2 marks attempt-2 start.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stream = Path(tmp.name) / "raw_export.jsonl"
        _write_jsonl(
            stream,
            [
                {"type": "text", "part": {"text": "prior turn reply"}},
                {"type": "error", "error": "from attempt 1"},
            ],
        )
        # Without offset: finds the error after the prior-turn text.
        self.assertIsNotNone(_latest_error_after_text_count(stream, 1))
        # With offset past the error: no error in scan range.
        self.assertIsNone(_latest_error_after_text_count(stream, 1, events_offset=2))


class LatestInternalLogSizeTest(unittest.TestCase):
    """Stall detector's "is opencode doing anything internally?" signal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)

    def test_no_state_dir_returns_zero(self) -> None:
        self.assertEqual(_latest_internal_log_size(None), 0)

    def test_missing_log_dir_returns_zero(self) -> None:
        self.assertEqual(_latest_internal_log_size(self.state_dir), 0)

    def test_empty_log_dir_returns_zero(self) -> None:
        (self.state_dir / "log").mkdir()
        self.assertEqual(_latest_internal_log_size(self.state_dir), 0)

    def test_latest_by_mtime_is_used(self) -> None:
        # Two log files; the newer (larger mtime) should dictate the size.
        log_dir = self.state_dir / "log"
        log_dir.mkdir()
        older = log_dir / "2026-06-04T100000.log"
        newer = log_dir / "2026-06-04T120000.log"
        older.write_text("x" * 9999, encoding="utf-8")
        newer.write_text("y" * 42, encoding="utf-8")
        # Force older mtime to be in the past so max(...) picks newer.
        import os

        os.utime(older, (1, 1))
        self.assertEqual(_latest_internal_log_size(self.state_dir), 42)

    def test_size_reflects_growth(self) -> None:
        log_dir = self.state_dir / "log"
        log_dir.mkdir()
        log = log_dir / "2026-06-04T120000.log"
        log.write_text("initial", encoding="utf-8")
        size_before = _latest_internal_log_size(self.state_dir)
        log.write_text("initial + more bytes", encoding="utf-8")
        size_after = _latest_internal_log_size(self.state_dir)
        self.assertGreater(size_after, size_before)


class CountJsonlEventsTest(unittest.TestCase):
    def test_missing_file_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_count_jsonl_events(Path(tmp) / "absent.jsonl"), 0)

    def test_counts_newline_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            _write_jsonl(path, [{"a": 1}, {"b": 2}, {"c": 3}])
            self.assertEqual(_count_jsonl_events(path), 3)


class RetryWrapperTest(unittest.TestCase):
    """Drives `_opencode_turn` via a monkeypatched `_opencode_turn_attempt`.

    Verifies retry semantics without spinning up Docker: the wrapper
    behavior is the surface we care about (which errors retry, which
    don't, the "after N retries" suffix, the retry event log).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        repo = init_fixture(root / "repo")
        run_id = new_run_id("retry-test")
        runs_root = root / "runs"
        self.paths = build_run_paths(runs_root, run_id)
        self.paths.run_dir.mkdir(parents=True)
        self.paths = self.paths.__class__(**{**self.paths.__dict__, "worktree": repo})
        self.config = RunConfig(
            repo=repo,
            base="main",
            runs_root=runs_root,
            run_slug=run_id,
            actor_mode=ActorMode.OPENCODE,
            opencode_transient_retry_max=2,
            opencode_transient_retry_backoff_seconds=0,
        )
        self.runner = OpencodeActorRunner(config=self.config, paths=self.paths)

    def _replace_attempt(self, side_effects: list) -> list[int]:
        """Swap _opencode_turn_attempt for a deterministic stub.

        `side_effects` is consumed in order. Each entry is either an
        exception (raised) or an `ActorOutput` (returned). Returns a
        list that will be populated with the 1-based attempt index for
        each call, so the test can assert call count.
        """

        calls: list[int] = []
        iterator = iter(side_effects)

        def fake_attempt(**kwargs):
            calls.append(len(calls) + 1)
            outcome = next(iterator)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.runner._opencode_turn_attempt = fake_attempt  # type: ignore[assignment]
        return calls

    def _invoke(self):
        return self.runner._opencode_turn(
            role="sim",
            prompt="probe",
            raw_export=self.paths.sim_raw_export,
            state_dir=self.runner.sim_state,
            mount_mode="ro",
            model="opencode/x",
            timeout_seconds=60,
            session_attr=None,
        )

    def test_transient_error_retries_and_succeeds(self) -> None:
        success = ActorOutput(text="ok", stderr="", returncode=0)
        calls = self._replace_attempt(
            [
                ActorError(
                    "sim opencode aborted early — transient provider error",
                    kind=events.PROVIDER_TRANSIENT_ERROR,
                ),
                success,
            ]
        )
        result = self._invoke()
        self.assertEqual(result.text, "ok")
        self.assertEqual(calls, [1, 2])

    def test_quota_error_does_not_retry(self) -> None:
        calls = self._replace_attempt(
            [
                ActorError(
                    "sim opencode aborted early — provider quota exhausted",
                    kind=events.PROVIDER_QUOTA_EXHAUSTED,
                ),
            ]
        )
        with self.assertRaises(ActorError) as ctx:
            self._invoke()
        self.assertEqual(ctx.exception.kind, events.PROVIDER_QUOTA_EXHAUSTED)
        self.assertEqual(calls, [1])
        self.assertNotIn("after", str(ctx.exception))

    def test_unrelated_error_does_not_retry(self) -> None:
        # No kind set — not transient. Wrapper should pass through unchanged.
        calls = self._replace_attempt([ActorError("sim opencode exited 1: boom")])
        with self.assertRaises(ActorError) as ctx:
            self._invoke()
        self.assertEqual(calls, [1])
        self.assertEqual(str(ctx.exception), "sim opencode exited 1: boom")

    def test_exhaustion_includes_retry_count_in_message(self) -> None:
        failure = ActorError(
            "sim opencode transient error",
            kind=events.PROVIDER_TRANSIENT_ERROR,
        )
        # max_retries=2 → up to 3 attempts before exhaustion.
        calls = self._replace_attempt([failure, failure, failure])
        with self.assertRaises(ActorError) as ctx:
            self._invoke()
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(ctx.exception.kind, events.PROVIDER_TRANSIENT_ERROR)
        self.assertIn("(after 2 retries)", str(ctx.exception))

    def test_retry_event_is_logged(self) -> None:
        success = ActorOutput(text="ok", stderr="", returncode=0)
        self._replace_attempt(
            [
                ActorError(
                    "sim opencode transient error",
                    kind=events.PROVIDER_TRANSIENT_ERROR,
                ),
                success,
            ]
        )
        self._invoke()
        records = [
            json.loads(line)
            for line in self.paths.guardrail_events.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        retry_events = [
            r for r in records if r.get("event") == events.PROVIDER_TRANSIENT_ERROR_RETRY
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["role"], "sim")
        self.assertEqual(retry_events[0]["attempt"], 1)
        self.assertEqual(retry_events[0]["backoff_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
