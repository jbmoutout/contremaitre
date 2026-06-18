"""Tests for the eval writer's hard-gate trichotomy.

`write_eval_reports` owns how the L0 gate's presence/absence is recorded. The
interface is the test surface: feed it a gate (or `None`) and read back
`pr_eval.json` — no orchestrator, no container.
"""

from __future__ import annotations

import json
from pathlib import Path

from contremaitre.diffscan import DiffScanResult
from contremaitre.evaluator import hard_gate_payload, sim_review_summary, write_eval_reports
from contremaitre.gates import L0GateResult
from contremaitre.models import TerminalVerdict
from contremaitre.paths import build_run_paths


def _gate(*, passed: bool) -> L0GateResult:
    diff_scan = DiffScanResult(passed=passed, changed_files=["a.py"], forbidden_files=[])
    return L0GateResult(
        passed=passed,
        recomputed_hash="deadbeef",
        diff_hash_matched=True,
        diff_scan=diff_scan,
        clean_worktree=True,
        payload=hard_gate_payload(diff_scan=diff_scan, clean_worktree=True, diff_hash_matched=True),
    )


def _write(tmp_path: Path, *, gate: L0GateResult | None, verdict: TerminalVerdict) -> dict:
    paths = build_run_paths(tmp_path, "run_eval")
    paths.eval_dir.mkdir(parents=True, exist_ok=True)
    write_eval_reports(
        paths=paths,
        verdict=verdict,
        gate=gate,
        checks=[],
        sim_review=sim_review_summary(verdict=None, confidence=None, summary="x"),
        trajectory={"turns": 0, "states": [], "process_reliability": 0.5},
        needs_human=[],
    )
    return json.loads(paths.pr_eval.read_text(encoding="utf-8"))


def test_gate_none_records_not_evaluated_with_null_details(tmp_path: Path):
    # A run that ended before L0 ran: distinct from FAIL, mirroring the sibling
    # `checks` field's NOT_CONFIGURED sentinel.
    payload = _write(tmp_path, gate=None, verdict=TerminalVerdict.NO_PR_NEEDS_HUMAN)
    assert payload["hard_gates"] == "NOT_EVALUATED"
    assert payload["hard_gate_details"] is None


def test_gate_failed_records_fail_with_payload(tmp_path: Path):
    payload = _write(tmp_path, gate=_gate(passed=False), verdict=TerminalVerdict.NO_PR_NEEDS_HUMAN)
    assert payload["hard_gates"] == "FAIL"
    assert payload["hard_gate_details"]["passed"] is False


def test_gate_passed_records_pass_with_payload(tmp_path: Path):
    payload = _write(tmp_path, gate=_gate(passed=True), verdict=TerminalVerdict.READY_FOR_DRAFT_PR)
    assert payload["hard_gates"] == "PASS"
    assert payload["hard_gate_details"]["passed"] is True
