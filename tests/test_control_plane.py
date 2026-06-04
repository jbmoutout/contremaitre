from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from contremaitre import events
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
        # Scope to 2 review rounds: one CHANGES_REQUESTED triggers a real
        # revision-followup turn, the second exhausts the cap. The previous
        # version used the default (3 rounds) which ran the loop slower
        # without exercising the revision path more thoroughly.
        result, _ = self._run_fixture(
            run_slug="changes",
            sim_scenario="changes_requested",
            caps=Caps(max_review_rounds=2),
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_CHANGES_REQUESTED)
        pr = self._read_json(result.run_dir / "pr.json")
        self.assertEqual(pr["kind"], "NO_PR")
        # Revision flow fired at least once with the SIM's required_changes
        # surfacing in the guardrail event. A regression that dropped the
        # bullets from the agent's revision prompt would leave the field
        # empty here.
        guardrail_lines = [
            json.loads(line)
            for line in (result.run_dir / "guardrail_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        revisions = [g for g in guardrail_lines if g.get("event") == events.REVISION_REQUESTED]
        self.assertGreaterEqual(len(revisions), 1)
        self.assertEqual(
            revisions[0]["required_changes"],
            ["Add the missing boundary test before review."],
        )

    def test_malformed_verdict_retries_to_needs_human(self):
        result, _ = self._run_fixture(
            run_slug="malformed",
            sim_scenario="malformed",
            caps=Caps(malformed_verdict_retries=1),
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        guardrails = (result.run_dir / "guardrail_events.jsonl").read_text(encoding="utf-8")
        self.assertIn(events.MALFORMED_VERDICT, guardrails)

    def test_forbidden_path_blocks_approved_publication(self):
        result, _ = self._run_fixture(run_slug="forbidden", agent_scenario="forbidden_path")

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        pr_eval = self._read_json(result.run_dir / "eval" / "pr_eval.json")
        self.assertEqual(pr_eval["hard_gates"], "FAIL")
        self.assertIn(".env", pr_eval["hard_gate_details"]["forbidden_files"])

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
        guardrails = (result.run_dir / "guardrail_events.jsonl").read_text(encoding="utf-8")
        self.assertIn(events.TURN_CAP, guardrails)

    def test_extra_reviewer_disabled_back_compat(self):
        """Single-SIM run unchanged: no extra_reviewer entries in cycles, no
        cross_family_agreement field surfaced, sim_review payload is flat."""

        result, _ = self._run_fixture(run_slug="extra-off")

        self.assertEqual(result.verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        cycles = self._read_jsonl(result.run_dir / "review_cycles.jsonl")
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["reviewer"], "sim")
        pr_eval = self._read_json(result.run_dir / "eval" / "pr_eval.json")
        # Flat (back-compat) sim_review block: no per-reviewer split.
        self.assertNotIn("sim", pr_eval["sim_review"])
        self.assertNotIn("extra", pr_eval["sim_review"])
        self.assertNotIn("cross_family_agreement", pr_eval["sim_review"])
        # Scorecard exposes cross_family_agreement=None when extra disabled.
        self.assertIsNone(pr_eval["scorecard"]["cross_family_agreement"])
        self.assertIsNone(pr_eval["scorecard"]["extra_reviewer_confidence"])

    def test_extra_reviewer_both_approve_publishes_with_agreement(self):
        """SIM and extra both APPROVE: PR publishes; pr_eval carries the
        structured sim/extra/merged shape with cross_family_agreement=True."""

        result, _ = self._run_fixture(
            run_slug="extra-both-ok",
            extra_reviewer_model="opencode/qwen-32b-free",
            extra_reviewer_scenario="approved",
        )

        self.assertEqual(result.verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        cycles = self._read_jsonl(result.run_dir / "review_cycles.jsonl")
        # Two entries: one sim + one extra in round 1.
        self.assertEqual(len(cycles), 2)
        reviewers = sorted(c["reviewer"] for c in cycles)
        self.assertEqual(reviewers, ["extra", "sim"])
        self.assertTrue(all(c["round"] == 1 for c in cycles))
        # No `unavailable` rows.
        self.assertFalse(any(c.get("unavailable") for c in cycles))

        pr_eval = self._read_json(result.run_dir / "eval" / "pr_eval.json")
        self.assertIn("sim", pr_eval["sim_review"])
        self.assertIn("extra", pr_eval["sim_review"])
        self.assertEqual(pr_eval["sim_review"]["merged_verdict"], "APPROVED")
        self.assertTrue(pr_eval["sim_review"]["cross_family_agreement"])
        self.assertTrue(pr_eval["scorecard"]["cross_family_agreement"])
        self.assertIsNotNone(pr_eval["scorecard"]["sim_confidence"])
        self.assertIsNotNone(pr_eval["scorecard"]["extra_reviewer_confidence"])

    def test_extra_reviewer_needs_human_escalates(self):
        """SIM=APPROVED + extra=NEEDS_HUMAN must terminate NO_PR_NEEDS_HUMAN.
        Verifies strictest-wins severity merge ordering."""

        result, _ = self._run_fixture(
            run_slug="extra-nh",
            extra_reviewer_model="opencode/qwen-32b-free",
            extra_reviewer_scenario="needs_human",
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        cycles = self._read_jsonl(result.run_dir / "review_cycles.jsonl")
        sim_entry = next(c for c in cycles if c["reviewer"] == "sim")
        extra_entry = next(c for c in cycles if c["reviewer"] == "extra")
        self.assertEqual(sim_entry["verdict"], "APPROVED")
        self.assertEqual(extra_entry["verdict"], "NEEDS_HUMAN")

    def test_extra_reviewer_bounces_loops_with_extra_tag(self):
        """SIM=APPROVED + extra=CHANGES_REQUESTED: orchestrator loops with
        the extra's required_changes tagged [EXTRA] in the revision prompt,
        then exhausts max_review_rounds → NO_PR_CHANGES_REQUESTED."""

        result, _ = self._run_fixture(
            run_slug="extra-bounce",
            extra_reviewer_model="opencode/qwen-32b-free",
            extra_reviewer_scenario="changes_requested",
            caps=Caps(max_review_rounds=2),
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_CHANGES_REQUESTED)
        guardrails = self._read_jsonl(result.run_dir / "guardrail_events.jsonl")
        revisions = [g for g in guardrails if g.get("event") == events.REVISION_REQUESTED]
        self.assertGreaterEqual(len(revisions), 1)
        # Required changes carry the [EXTRA] tag (since SIM had no required
        # changes; merged list is extra-only with [EXTRA] tag).
        first_revision = revisions[0]["required_changes"]
        self.assertTrue(
            any(c.startswith("[EXTRA]") for c in first_revision),
            f"expected [EXTRA] tag in required_changes; got {first_revision!r}",
        )

    def test_extra_reviewer_unavailable_falls_back_to_sim_only(self):
        """Extra reviewer malformed-verdict exhaustion records an
        extra_reviewer_unavailable recovery and lets the SIM verdict drive
        the round (no regression vs single-SIM)."""

        result, _ = self._run_fixture(
            run_slug="extra-unavail",
            extra_reviewer_model="opencode/qwen-32b-free",
            extra_reviewer_scenario="malformed",
            caps=Caps(malformed_verdict_retries=0),
        )

        self.assertEqual(result.verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        cycles = self._read_jsonl(result.run_dir / "review_cycles.jsonl")
        sim_entry = next(c for c in cycles if c["reviewer"] == "sim")
        extra_entry = next(c for c in cycles if c["reviewer"] == "extra")
        self.assertEqual(sim_entry["verdict"], "APPROVED")
        self.assertTrue(extra_entry["unavailable"])
        recoveries = self._read_jsonl(result.run_dir / "recoveries.jsonl")
        self.assertTrue(any(r.get("kind") == events.EXTRA_REVIEWER_UNAVAILABLE for r in recoveries))
        pr_eval = self._read_json(result.run_dir / "eval" / "pr_eval.json")
        # Structured shape with extra=None and cross_family_agreement=None
        # because the extra reviewer was attempted but unavailable.
        self.assertIsNone(pr_eval["sim_review"]["extra"])
        self.assertIsNone(pr_eval["sim_review"]["cross_family_agreement"])

    def _run_fixture(
        self,
        *,
        run_slug: str,
        sim_scenario: str = "approved",
        extra_reviewer_model: str | None = None,
        extra_reviewer_scenario: str = "approved",
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
            extra_reviewer_model=extra_reviewer_model,
            extra_reviewer_scenario=extra_reviewer_scenario,
            agent_scenario=agent_scenario,
            simulate_drift_after_approval=simulate_drift_after_approval,
            caps=caps or Caps(),
        )
        return run(config), runs_root

    @staticmethod
    def _read_json(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


if __name__ == "__main__":
    unittest.main()
