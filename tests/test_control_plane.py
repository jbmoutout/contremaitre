from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from contremaitre.fixture import init_fixture
from contremaitre.models import Caps, RunConfig, TerminalVerdict
from contremaitre.orchestrator import run


class ControlPlaneTest(unittest.TestCase):
    def test_approved_run_writes_artifacts_and_stub_pr(self):
        result, runs_root = self._run_fixture(run_slug="approved")

        self.assertEqual(result.verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        pr = self._read_json(result.run_dir / "pr.json")
        self.assertEqual(pr["kind"], "PUBLISHED")
        self.assertTrue(pr["dry_run"])  # stub publisher
        self.assertTrue((result.run_dir / "raw_export.jsonl").exists())
        self.assertTrue((result.run_dir / "sim_raw_export.jsonl").exists())
        self.assertTrue((result.run_dir / "eval" / "pr_eval.json").exists())
        self.assertTrue(result.run_dir.exists())

    def test_changes_requested_is_safe_no_pr(self):
        result, _ = self._run_fixture(run_slug="changes", sim_scenario="changes_requested")

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_CHANGES_REQUESTED)
        pr = self._read_json(result.run_dir / "pr.json")
        self.assertEqual(pr["kind"], "NO_PR")

    def test_malformed_verdict_retries_to_needs_human(self):
        result, _ = self._run_fixture(
            run_slug="malformed",
            sim_scenario="malformed",
            caps=Caps(malformed_verdict_retries=1),
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        events = (result.run_dir / "guardrail_events.jsonl").read_text(encoding="utf-8")
        self.assertIn("malformed_verdict", events)

    def test_forbidden_path_blocks_approved_publication(self):
        result, _ = self._run_fixture(run_slug="forbidden", agent_scenario="forbidden_path")

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        pr_eval = self._read_json(result.run_dir / "eval" / "pr_eval.json")
        self.assertEqual(pr_eval["hard_gates"], "FAIL")
        self.assertIn("prisma/migrations/0001_forbidden.sql", pr_eval["hard_gate_details"]["forbidden_files"])

    def test_diff_hash_drift_blocks_publication(self):
        result, _ = self._run_fixture(run_slug="drift", simulate_drift_after_approval=True)

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        pr_eval = self._read_json(result.run_dir / "eval" / "pr_eval.json")
        self.assertFalse(pr_eval["hard_gate_details"]["checks"]["diff_hash_matched"])

    def test_turn_cap_stops_safely(self):
        # Without IMPLEMENTATION_COMPLETE the WORK loop runs until a cap fires.
        result, _ = self._run_fixture(
            run_slug="cap",
            agent_scenario="no_impl_complete",
            caps=Caps(max_turns=1),
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        events = (result.run_dir / "guardrail_events.jsonl").read_text(encoding="utf-8")
        self.assertIn("turn_cap", events)

    def _run_fixture(
        self,
        *,
        run_slug: str,
        sim_scenario: str = "approved",
        agent_scenario: str = "normal",
        simulate_drift_after_approval: bool = False,
        caps: Caps | None = None,
    ):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        runs_root = root / "runs"
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=runs_root,
            run_slug=run_slug,
            check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
            sim_scenario=sim_scenario,
            agent_scenario=agent_scenario,
            simulate_drift_after_approval=simulate_drift_after_approval,
            caps=caps or Caps(),
        )
        return run(config), runs_root

    @staticmethod
    def _read_json(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
