"""Tests for the run-viewer assembler.

The viewer is observability — it must build at every run terminus, must not
let agent-written content break out of the DATA payload, and must surface
codex `exec --json` streams (a foreign event vocabulary with no clock) as
chat turns alongside opencode's.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from contremaitre import events
from contremaitre.fixture import init_fixture
from contremaitre.jsonlog import write_json
from contremaitre.models import Caps, RunConfig
from contremaitre.orchestrator import run
from contremaitre.paths import build_run_paths, new_run_id
from contremaitre.viewer import (
    VIEWER_FILENAME,
    _assign_synthetic_timestamps,
    _build_chat,
    _is_claude_stream,
    _normalize_events,
    build_viewer,
)


def _codex_turn_events(command, output, message, *, input_tokens=100, output_tokens=20):
    """One codex `exec --json` turn: a command, its result, and a final reply."""

    return [
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": command,
                "aggregated_output": output,
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": message},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    ]


def _extract_data_payload(html: str) -> dict:
    """Pull the JSON between `const DATA = ` and `;\\n</script>`.

    The viewer test relies on this exact framing — the renderer reads it.
    """

    marker = "const DATA = "
    start = html.index(marker) + len(marker)
    end = html.index(";\n</script>", start)
    return json.loads(html[start:end])


class ViewerTest(unittest.TestCase):
    def test_fake_run_writes_viewer_html(self):
        """End-to-end: orchestrator's `finally` produces viewer.html with
        structural anchors and a DATA payload tagged with this run's id.

        The previous version only asserted `size > 1000` — the embedded CSS
        alone is 20KB, so a viewer that wrote only the CSS (no DATA, no
        body) would still pass. These assertions catch that regression.
        """

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=root / "runs",
            run_slug="viewer-e2e",
            check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
            caps=Caps(),
        )
        result = run(config)
        viewer = result.run_dir / VIEWER_FILENAME
        self.assertTrue(viewer.exists())
        html = viewer.read_text(encoding="utf-8")
        # Structural anchors — these would all be absent in a viewer that
        # only dumped the CSS or only rendered an error page.
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("const DATA = ", html)
        self.assertIn("</html>", html)
        # The DATA payload is parseable JSON and identifies THIS run.
        data = _extract_data_payload(html)
        self.assertEqual(data.get("run_id"), result.run_id)

    def test_escapes_closing_script_tag_in_payload(self):
        """`</script>` in extracted-file bodies must not close the page tag."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = build_run_paths(root / "runs", new_run_id("escape"))
        paths.run_dir.mkdir(parents=True)
        write_json(paths.stats, {"run_id": paths.run_id, "verdict": "FAILED_INFRA"})
        paths.extracted_files_dir.mkdir(parents=True)
        (paths.extracted_files_dir / "evil.html").write_text(
            "<script>alert(1)</script>",
            encoding="utf-8",
        )

        out = build_viewer(paths)
        html = out.read_text(encoding="utf-8")
        marker = "const DATA = "
        start = html.index(marker) + len(marker)
        end = html.index(";\n</script>", start)
        self.assertNotIn("</script>", html[start:end])
        # The escaped payload is still parseable as JSON — escaping must
        # not corrupt the data the renderer reads.
        _extract_data_payload(html)

    def test_surfaces_per_round_cli_review_artifacts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = build_run_paths(root / "runs", new_run_id("cli-round"))
        paths.run_dir.mkdir(parents=True)
        write_json(paths.stats, {"run_id": paths.run_id, "verdict": "PR_NEEDS_HUMAN"})
        extras_dir = paths.run_dir / "extras" / "cli_review_001"
        extras_dir.mkdir(parents=True)
        (extras_dir / "codex_review.md").write_text(
            "### reviewed by `codex`\n\n"
            "MUST_FIX - blocking issue\n\n"
            "**issue:** README.md:1 needs an update.\n",
            encoding="utf-8",
        )
        (extras_dir / "codex_raw_export.jsonl").write_text(
            '{"type":"text","part":{"text":"model: gpt-5.5"}}\n',
            encoding="utf-8",
        )

        html = build_viewer(paths).read_text(encoding="utf-8")
        data = _extract_data_payload(html)

        self.assertIn(
            {
                "tool": "codex",
                "source": "round-001-codex",
                "status": "completed",
                "verdict": "MUST_FIX",
                "duration_s": None,
                "model": "gpt-5.5",
                "url": None,
                "markdown": "### reviewed by `codex`\n\n"
                "MUST_FIX - blocking issue\n\n"
                "**issue:** README.md:1 needs an update.\n",
                "fail_reason": None,
                "posted_to_pr": None,
            },
            data["cli_review_extras"],
        )

    def test_root_cli_review_uses_latest_round_events(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = build_run_paths(root / "runs", new_run_id("cli-latest"))
        paths.run_dir.mkdir(parents=True)
        write_json(paths.stats, {"run_id": paths.run_id, "verdict": "READY_FOR_DRAFT_PR"})
        paths.guardrail_events.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {"event": events.CLI_REVIEW_STARTED, "tool": "codex", "round": 1},
                    {
                        "event": events.CLI_REVIEW_COMPLETED,
                        "tool": "codex",
                        "round": 1,
                        "verdict": "MUST_FIX",
                    },
                    {"event": events.CLI_REVIEW_STARTED, "tool": "codex", "round": 2},
                    {
                        "event": events.CLI_REVIEW_COMPLETED,
                        "tool": "codex",
                        "round": 2,
                        "verdict": "LOOKS_GOOD",
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (paths.run_dir / "codex_review.md").write_text(
            "### reviewed by `codex`\n\nLOOKS_GOOD - clean\n",
            encoding="utf-8",
        )

        data = _extract_data_payload(build_viewer(paths).read_text(encoding="utf-8"))

        self.assertEqual(data["cli_review"]["status"], "completed")
        self.assertEqual(data["cli_review"]["verdict"], "LOOKS_GOOD")

    def test_root_cli_review_unparseable_verdict_shows_failed_status(self):
        # Current format: only CLI_REVIEW_FAILED emitted for unparseable verdicts
        # (CLI_REVIEW_COMPLETED is suppressed when verdict is None).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = build_run_paths(root / "runs", new_run_id("cli-unparseable"))
        paths.run_dir.mkdir(parents=True)
        write_json(paths.stats, {"run_id": paths.run_id, "verdict": "PR_NEEDS_HUMAN"})
        paths.guardrail_events.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {"event": events.CLI_REVIEW_STARTED, "tool": "codex", "round": 1},
                    {
                        "event": events.CLI_REVIEW_FAILED,
                        "tool": "codex",
                        "round": 1,
                        "reason": "unparseable_verdict",
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (paths.run_dir / "codex_review.md").write_text(
            "### reviewed by `codex`\n\nLooks fine to me\n",
            encoding="utf-8",
        )

        data = _extract_data_payload(build_viewer(paths).read_text(encoding="utf-8"))

        self.assertEqual(data["cli_review"]["status"], "failed")

    def test_root_cli_review_unparseable_old_format_backwards_compat(self):
        # Old format (pre event-ordering fix): both CLI_REVIEW_COMPLETED(verdict=None)
        # and CLI_REVIEW_FAILED were emitted. Viewer must still show status=failed.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = build_run_paths(root / "runs", new_run_id("cli-old-fmt"))
        paths.run_dir.mkdir(parents=True)
        write_json(paths.stats, {"run_id": paths.run_id, "verdict": "PR_NEEDS_HUMAN"})
        paths.guardrail_events.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {"event": events.CLI_REVIEW_STARTED, "tool": "codex", "round": 1},
                    {
                        "event": events.CLI_REVIEW_COMPLETED,
                        "tool": "codex",
                        "round": 1,
                        "verdict": None,
                    },
                    {
                        "event": events.CLI_REVIEW_FAILED,
                        "tool": "codex",
                        "round": 1,
                        "reason": "unparseable_verdict",
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (paths.run_dir / "codex_review.md").write_text(
            "### reviewed by `codex`\n\nLooks fine to me\n",
            encoding="utf-8",
        )

        data = _extract_data_payload(build_viewer(paths).read_text(encoding="utf-8"))

        self.assertEqual(data["cli_review"]["status"], "failed")


class CodexNormalizationTest(unittest.TestCase):
    """codex `exec --json` streams must surface in the chat like opencode's.

    codex speaks a thread/turn/item vocabulary with no per-event clock, so
    without normalization its agent/SIM turns are invisible in the viewer.
    """

    def test_codex_stream_becomes_chat_turns(self):
        events = _normalize_events(
            _codex_turn_events("ls -la", "total 0", "First reply.")
            + _codex_turn_events("cat x.py", "print(1)", "Second reply.")
        )
        _assign_synthetic_timestamps([events])
        chat = _build_chat(events, [])
        turns = chat["turns"]
        self.assertEqual([t["text"] for t in turns], ["First reply.", "Second reply."])
        # The command became a bash tool card on its turn.
        self.assertEqual(turns[0]["tools"][0]["tool"], "bash")
        self.assertEqual(turns[0]["tools"][0]["input"]["command"], "ls -la")
        self.assertEqual(turns[0]["tools"][0]["output"], "total 0")
        # turn.completed usage rolled into the turn's token total.
        self.assertEqual(chat["totals"]["AGENT"]["tokens"], 240)
        self.assertEqual(chat["totals"]["AGENT"]["msgs"], 2)

    def test_opencode_stream_passes_through_untouched(self):
        opencode = [
            {"type": "text", "timestamp": 1000, "part": {"text": "hi"}},
            {
                "type": "step_finish",
                "timestamp": 1001,
                "part": {"tokens": {"input": 5, "output": 3}},
            },
        ]
        self.assertIs(_normalize_events(opencode), opencode)

    def test_real_codex_timestamps_are_preserved_not_synthesized(self):
        """Newer codex runs back-fill real timestamps at the source
        (cli_actor._stamp_codex_slice); the viewer must use them verbatim and
        NOT overwrite them with the synthetic spread reserved for legacy runs."""

        events = _codex_turn_events("ls", "out", "the reply")
        for i, ev in enumerate(events):  # as the source stamper would
            ev["timestamp"] = 5000 + i
        norm = _normalize_events(events)
        _assign_synthetic_timestamps([norm])
        turn = _build_chat(norm, [])["turns"][0]
        self.assertFalse(turn["ts_synthetic"])
        # The bubble carries the agent_message's real clock, not a guess.
        self.assertEqual(turn["ts"], 5002)

    def test_synthetic_timestamps_interleave_codex_into_real_clock(self):
        """A codex SIM turn must sort between the opencode agent turns, not at t=0."""

        agent = [
            {"type": "text", "timestamp": 1000, "part": {"text": "agent early"}},
            {"type": "text", "timestamp": 3000, "part": {"text": "agent late"}},
        ]
        sim = _normalize_events(_codex_turn_events("grep x", "found", "sim middle"))
        _assign_synthetic_timestamps([agent, sim])
        chat = _build_chat(agent, sim)
        order = [(t["who"], t["text"]) for t in chat["turns"]]
        self.assertEqual(
            order,
            [("AGENT", "agent early"), ("SIM", "sim middle"), ("AGENT", "agent late")],
        )
        sim_turn = next(t for t in chat["turns"] if t["who"] == "SIM")
        self.assertTrue(sim_turn["ts_synthetic"])

    def test_all_codex_run_alternates_by_round(self):
        """No real clock anywhere: agent N and SIM N stagger in round order."""

        agent = _normalize_events(
            _codex_turn_events("a1", "o1", "agent r1") + _codex_turn_events("a2", "o2", "agent r2")
        )
        sim = _normalize_events(
            _codex_turn_events("s1", "o1", "sim r1") + _codex_turn_events("s2", "o2", "sim r2")
        )
        _assign_synthetic_timestamps([agent, sim])
        chat = _build_chat(agent, sim)
        self.assertEqual(
            [(t["who"], t["text"]) for t in chat["turns"]],
            [
                ("AGENT", "agent r1"),
                ("SIM", "sim r1"),
                ("AGENT", "agent r2"),
                ("SIM", "sim r2"),
            ],
        )


if __name__ == "__main__":
    unittest.main()


def _claude_turn_events(command, output, message, *, input_tokens=100, output_tokens=20, cost=0.01):
    """One claude `stream-json` turn: init, an assistant tool call, its result, a final reply."""

    return [
        {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-4-8"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "working"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": output}]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": message,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "total_cost_usd": cost,
        },
    ]


class ClaudeNormalizeTest(unittest.TestCase):
    def test_is_claude_stream_detects_claude_not_codex_or_opencode(self):
        self.assertTrue(_is_claude_stream([{"type": "result", "subtype": "success"}]))
        self.assertTrue(_is_claude_stream([{"type": "assistant", "message": {}}]))
        self.assertFalse(_is_claude_stream([{"type": "text", "part": {}}]))
        self.assertFalse(_is_claude_stream([{"type": "item.completed", "item": {}}]))

    def test_claude_stream_becomes_chat_turns(self):
        events = _normalize_events(
            _claude_turn_events("ls -la", "total 0", "First reply.")
            + _claude_turn_events("cat x.py", "print(1)", "Second reply.")
        )
        _assign_synthetic_timestamps([events])
        chat = _build_chat(events, [])
        turns = chat["turns"]
        # Final text is result.result, one bubble per turn.
        self.assertEqual([t["text"] for t in turns], ["First reply.", "Second reply."])
        # The tool_use block became a bash card with input + stitched output.
        self.assertEqual(turns[0]["tools"][0]["tool"], "bash")
        self.assertEqual(turns[0]["tools"][0]["input"]["command"], "ls -la")
        self.assertEqual(turns[0]["tools"][0]["output"], "total 0")
        # result.usage rolled into the turn's token total (input + output).
        self.assertEqual(chat["totals"]["AGENT"]["tokens"], 240)
        self.assertEqual(chat["totals"]["AGENT"]["msgs"], 2)
