"""Sharper tests that lock specific artifacts at the actor + publication boundary.

The pre-existing suite verifies that artifacts EXIST. These tests verify
their CONTENT — what role/phase the actor logged, what shape pr.json takes
per terminal kind, what transcript rows landed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from contremaitre.actors import FakeActorRunner
from contremaitre.fixture import init_fixture
from contremaitre.models import Caps, RunConfig, TerminalVerdict
from contremaitre.orchestrator import run
from contremaitre.paths import build_run_paths, new_run_id


# ---------- actor-boundary tests (FakeActorRunner alone, no orchestrator) ----------


class FakeActorWritesItsOwnArtifactsTest(unittest.TestCase):
    """C1's invariant: the actor owns raw_export + transcript writes.

    These tests instantiate FakeActorRunner directly and check that each
    public method (agent_turn, sim_turn, sim_review) writes the right JSONL
    event and the right transcript row.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo = init_fixture(root / "repo")
        runs_root = root / "runs"
        run_id = new_run_id("actor-test")
        self.paths = build_run_paths(runs_root, run_id)
        self.paths.run_dir.mkdir(parents=True)
        # Worktree gets created by the orchestrator normally; for these tests
        # we point the fake actor at the fixture repo directly so its
        # subprocess can write inside a real directory.
        self.paths = self.paths.__class__(
            **{**self.paths.__dict__, "worktree": self.repo}
        )
        self.actor = FakeActorRunner(
            paths=self.paths,
            agent_scenario="normal",
            sim_scenario="approved",
        )

    def _read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_agent_turn_writes_text_event_with_role_and_phase(self):
        out = self.actor.agent_turn("ignored by fake")

        self.assertTrue(out.text)
        events_list = self._read_jsonl(self.paths.raw_export)
        text_events = [e for e in events_list if e.get("type") == "text"]
        self.assertEqual(len(text_events), 1)
        self.assertEqual(text_events[0]["role"], "agent")
        self.assertEqual(text_events[0]["phase"], "WORK")
        self.assertEqual(text_events[0]["part"]["text"], out.text)

    def test_sim_turn_writes_text_event_to_sim_raw_export(self):
        out = self.actor.sim_turn("ignored")

        self.assertTrue(out.text)
        # No leakage into the agent stream.
        self.assertFalse(self.paths.raw_export.exists())
        events_list = self._read_jsonl(self.paths.sim_raw_export)
        text_events = [e for e in events_list if e.get("type") == "text"]
        self.assertEqual(len(text_events), 1)
        self.assertEqual(text_events[0]["role"], "sim")
        self.assertEqual(text_events[0]["phase"], "WORK")
        self.assertEqual(text_events[0]["part"]["text"], out.text)

    def test_sim_review_writes_strict_json_verdict(self):
        # sim_review needs the agent to have produced SETTLED + a diff.
        # Easier here to fake the inputs directly.
        diff_file = self.paths.run_dir / "diff.patch"
        settled_file = self.paths.run_dir / "SETTLED_DESIGN.md"
        diff_file.write_text("dummy diff", encoding="utf-8")
        settled_file.write_text("dummy settled", encoding="utf-8")

        out = self.actor.sim_review(
            diff_file=diff_file,
            settled_file=settled_file,
            scenario="approved",
            attempt=1,
        )

        verdict = json.loads(out.text)
        self.assertEqual(verdict["verdict"], "APPROVED")
        self.assertIn("confidence", verdict)
        # The actor's REVIEW-phase event lands in sim_raw_export.
        events_list = self._read_jsonl(self.paths.sim_raw_export)
        review_text_events = [
            e for e in events_list if e.get("type") == "text" and e.get("phase") == "REVIEW"
        ]
        self.assertEqual(len(review_text_events), 1)
        self.assertEqual(review_text_events[0]["role"], "sim")

    def test_actor_interleaves_transcript_rows(self):
        self.actor.agent_turn("first")
        self.actor.sim_turn("second")

        transcript = self.paths.transcript.read_text(encoding="utf-8")
        # The two rows must land in order, each tagged with their phase.
        agent_idx = transcript.find("## WORK - agent")
        sim_idx = transcript.find("## WORK - sim")
        self.assertNotEqual(agent_idx, -1)
        self.assertNotEqual(sim_idx, -1)
        self.assertLess(agent_idx, sim_idx)


# ---------- pr.json schema-by-kind tests ----------


class PrJsonSchemaPerKindTest(unittest.TestCase):
    """Lock C2's invariant: pr.json carries the same key set across every
    terminal, with the two hash fields populated by drift case.
    """

    REQUIRED_KEYS = {
        "kind",
        "branch",
        "base",
        "url",
        "approved_diff_hash",
        "current_diff_hash",
        "reason",
        "publish_mode",
        "dry_run",
    }

    def test_published_has_equal_hashes(self):
        pr = self._run_and_read("published")
        self.assertEqual(set(pr.keys()), self.REQUIRED_KEYS)
        self.assertEqual(pr["kind"], "PUBLISHED")
        self.assertIsNotNone(pr["approved_diff_hash"])
        self.assertEqual(pr["approved_diff_hash"], pr["current_diff_hash"])

    def test_blocked_on_drift_has_diverging_hashes(self):
        pr = self._run_and_read("drift", simulate_drift_after_approval=True)
        self.assertEqual(set(pr.keys()), self.REQUIRED_KEYS)
        self.assertEqual(pr["kind"], "BLOCKED")
        self.assertIsNotNone(pr["approved_diff_hash"])
        self.assertIsNotNone(pr["current_diff_hash"])
        self.assertNotEqual(pr["approved_diff_hash"], pr["current_diff_hash"])

    def test_no_pr_has_null_hashes(self):
        pr = self._run_and_read("needs", sim_scenario="needs_human")
        self.assertEqual(set(pr.keys()), self.REQUIRED_KEYS)
        self.assertEqual(pr["kind"], "NO_PR")
        self.assertIsNone(pr["approved_diff_hash"])
        self.assertIsNone(pr["current_diff_hash"])

    def _run_and_read(self, slug: str, **overrides) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=root / "runs",
            run_slug=slug,
            check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
            caps=Caps(),
            **overrides,
        )
        result = run(config)
        return json.loads((result.run_dir / "pr.json").read_text(encoding="utf-8"))


# ---------- recoveries.jsonl content (sanity check the recovery surface) ----------


class RecoveryArtifactShapeTest(unittest.TestCase):
    def test_fake_run_produces_no_recoveries_file(self):
        """Fake mode never touches docker, so no recovery events should fire."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=root / "runs",
            run_slug="no-recoveries",
            check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
        )
        result = run(config)

        recoveries = result.run_dir / "recoveries.jsonl"
        self.assertEqual(result.verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        self.assertFalse(
            recoveries.exists(),
            f"fake mode shouldn't write recoveries.jsonl; got: "
            f"{recoveries.read_text(encoding='utf-8') if recoveries.exists() else ''}",
        )


if __name__ == "__main__":
    unittest.main()
