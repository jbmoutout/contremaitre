"""Unit tests crossing the publication decision Interface directly.

These exercise the gate without an orchestrator, fixture, or docker — the
interface is the test surface. The end-to-end wiring stays covered by the
fixture integration tests in test_control_plane.py.
"""

from __future__ import annotations

import unittest

from contremaitre.checks import CheckResult
from contremaitre.diffscan import DiffScanResult
from contremaitre.publication import (
    EXECUTABLE_CHECKS_REASON,
    HARD_GATE_REASON,
    GateInputs,
    decide_publication,
    gates_not_evaluated,
    hard_gate_payload,
)


def _clean_scan() -> DiffScanResult:
    return DiffScanResult(passed=True, changed_files=["a.py"], forbidden_files=[])


def _forbidden_scan() -> DiffScanResult:
    return DiffScanResult(passed=False, changed_files=[".env"], forbidden_files=[".env"])


def _passing_check() -> CheckResult:
    return CheckResult(cmd="pytest", returncode=0, duration_seconds=0.1, stdout="", stderr="")


def _failing_check() -> CheckResult:
    return CheckResult(cmd="pytest", returncode=1, duration_seconds=0.1, stdout="", stderr="boom")


class DecidePublicationTest(unittest.TestCase):
    def test_all_pass_publishes(self):
        decision = decide_publication(
            GateInputs(
                diff_scan=_clean_scan(),
                clean_worktree=True,
                diff_hash_matched=True,
                checks=[_passing_check()],
            )
        )
        self.assertTrue(decision.publish)
        self.assertIsNone(decision.block_reason)
        self.assertTrue(decision.hard_gates["passed"])

    def test_no_checks_configured_still_publishes(self):
        decision = decide_publication(
            GateInputs(diff_scan=_clean_scan(), clean_worktree=True, diff_hash_matched=True)
        )
        self.assertTrue(decision.publish)
        self.assertIsNone(decision.block_reason)

    def test_forbidden_path_blocks_as_hard_gate(self):
        decision = decide_publication(
            GateInputs(
                diff_scan=_forbidden_scan(),
                clean_worktree=True,
                diff_hash_matched=True,
                checks=[_passing_check()],
            )
        )
        self.assertFalse(decision.publish)
        self.assertEqual(decision.block_reason, HARD_GATE_REASON)
        self.assertIn(".env", decision.hard_gates["forbidden_files"])

    def test_diff_hash_drift_blocks_as_hard_gate(self):
        decision = decide_publication(
            GateInputs(diff_scan=_clean_scan(), clean_worktree=True, diff_hash_matched=False)
        )
        self.assertFalse(decision.publish)
        self.assertEqual(decision.block_reason, HARD_GATE_REASON)
        self.assertFalse(decision.hard_gates["checks"]["diff_hash_matched"])

    def test_dirty_worktree_blocks_as_hard_gate(self):
        decision = decide_publication(
            GateInputs(diff_scan=_clean_scan(), clean_worktree=False, diff_hash_matched=True)
        )
        self.assertFalse(decision.publish)
        self.assertEqual(decision.block_reason, HARD_GATE_REASON)

    def test_failing_check_blocks_when_hard_gates_pass(self):
        decision = decide_publication(
            GateInputs(
                diff_scan=_clean_scan(),
                clean_worktree=True,
                diff_hash_matched=True,
                checks=[_passing_check(), _failing_check()],
            )
        )
        self.assertFalse(decision.publish)
        self.assertEqual(decision.block_reason, EXECUTABLE_CHECKS_REASON)

    def test_hard_gate_failure_wins_precedence_over_checks(self):
        # Both a forbidden path AND a failing check: the hard gate reason wins.
        decision = decide_publication(
            GateInputs(
                diff_scan=_forbidden_scan(),
                clean_worktree=True,
                diff_hash_matched=True,
                checks=[_failing_check()],
            )
        )
        self.assertFalse(decision.publish)
        self.assertEqual(decision.block_reason, HARD_GATE_REASON)


class GatePayloadShapeTest(unittest.TestCase):
    def test_gates_not_evaluated_shape(self):
        payload = gates_not_evaluated()
        self.assertFalse(payload["passed"])
        self.assertEqual(
            set(payload["checks"]),
            {"diff_scan", "clean_worktree", "diff_hash_matched", "draft_only"},
        )
        self.assertTrue(payload["checks"]["draft_only"])
        self.assertFalse(payload["checks"]["diff_scan"])
        self.assertEqual(payload["forbidden_files"], [])
        self.assertEqual(payload["changed_files"], [])

    def test_hard_gate_payload_matches_decision_hard_gates(self):
        scan = _clean_scan()
        standalone = hard_gate_payload(diff_scan=scan, clean_worktree=True, diff_hash_matched=True)
        decision = decide_publication(
            GateInputs(diff_scan=scan, clean_worktree=True, diff_hash_matched=True)
        )
        self.assertEqual(standalone, decision.hard_gates)

    def test_hard_gate_payload_none_scan(self):
        payload = hard_gate_payload(diff_scan=None, clean_worktree=True, diff_hash_matched=True)
        self.assertFalse(payload["passed"])
        self.assertFalse(payload["checks"]["diff_scan"])


if __name__ == "__main__":
    unittest.main()
