"""Gate-first PR evaluation report writer.

What is measured today (load-bearing):
  - L0 hard gates: diff scan, diff-hash match, clean worktree, draft-only posture
  - L1 executable checks: configured `--check-cmd` results
  - SIM review verdict: APPROVED / CHANGES_REQUESTED / NEEDS_HUMAN (one number)
  - Trajectory: turns, states, terminal reason

What is NOT yet measured (PENDING — needs focused judge passes):
  - L2 SETTLED-to-diff conformance — a per-axis judge reading SETTLED + diff
  - L3 architecture delta — a judge assessing caller-knowledge reduction,
    shallow-path deletion, behavior preservation

L2/L3 belong as separate focused-judge LLM calls per axis. Until those exist,
the scorecard reports only what is actually measured and marks the rest
`PENDING`. We do not import an external grading substrate at runtime; any
patterns we adopt are copied in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .checks import CheckResult
from .diffscan import DiffScanResult
from .jsonlog import write_json
from .models import RunPaths, TerminalVerdict


def write_eval_reports(
    *,
    paths: RunPaths,
    verdict: TerminalVerdict,
    hard_gates: dict[str, Any],
    checks: list[CheckResult],
    sim_review: dict[str, Any],
    trajectory: dict[str, Any],
    needs_human: list[str],
) -> None:
    checks_payload = {
        "status": _checks_status(checks),
        "results": [
            {
                "cmd": check.cmd,
                "returncode": check.returncode,
                "duration_seconds": check.duration_seconds,
                "stdout": check.stdout,
                "stderr": check.stderr,
            }
            for check in checks
        ],
    }
    write_json(paths.checks_report, checks_payload)
    write_json(paths.settled_diff_report, _pending_report("L2 SETTLED-to-diff focused judge not implemented"))
    write_json(paths.architecture_delta_report, _pending_report("L3 architecture-delta focused judge not implemented"))
    write_json(paths.trajectory_report, trajectory)

    payload = {
        "verdict": verdict.value,
        "hard_gates": "PASS" if hard_gates.get("passed") else "FAIL",
        "checks": checks_payload["status"],
        "sim_review": sim_review,
        "settled_conformance": "PENDING",
        "architecture_delta": "PENDING",
        "needs_human": needs_human,
        "scorecard": {
            "executable_confidence": _executable_confidence(checks_payload["status"]),
            "sim_review_confidence": sim_review.get("confidence"),
            "process_reliability": trajectory.get("process_reliability", 0.0),
            "design_conformance": None,
            "architecture_value": None,
        },
        "hard_gate_details": hard_gates,
    }
    write_json(paths.pr_eval, payload)
    paths.pr_eval_md.write_text(_render_md(payload), encoding="utf-8")


def hard_gate_payload(
    *,
    diff_scan: DiffScanResult | None,
    clean_worktree: bool,
    diff_hash_matched: bool,
    draft_only: bool = True,
) -> dict[str, Any]:
    # `clean_worktree` is expected to hold trivially in normal flow because the
    # orchestrator commits agent changes before this gate runs. Kept as a
    # belt-and-suspenders check: if a downstream change ever moves the commit
    # boundary or introduces post-commit edits, this fails loud.
    checks = {
        "diff_scan": diff_scan.passed if diff_scan else False,
        "clean_worktree": clean_worktree,
        "diff_hash_matched": diff_hash_matched,
        "draft_only": draft_only,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "forbidden_files": diff_scan.forbidden_files if diff_scan else [],
        "changed_files": diff_scan.changed_files if diff_scan else [],
    }


def sim_review_summary(
    *,
    verdict: str | None,
    confidence: float | None,
    summary: str,
    required_changes: list[str] | None = None,
    checks_performed: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "summary": summary,
        "required_changes": required_changes or [],
        "checks_performed": checks_performed or [],
    }


def _pending_report(reason: str) -> dict[str, Any]:
    return {"status": "PENDING", "reason": reason}


def _checks_status(checks: list[CheckResult]) -> str:
    if not checks:
        return "NOT_CONFIGURED"
    return "PASS" if all(check.passed for check in checks) else "FAIL"


def _executable_confidence(status: str) -> float | None:
    # NOT_CONFIGURED → null (operator opted out; absence of signal, not a
    # zero-confidence signal). Distinguishing this from FAIL keeps the
    # scorecard honest for downstream readers.
    if status == "PASS":
        return 1.0
    if status == "FAIL":
        return 0.0
    return None


def _render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# PR Eval",
        "",
        f"- verdict: `{payload['verdict']}`",
        f"- hard_gates: `{payload['hard_gates']}`",
        f"- checks: `{payload['checks']}`",
        f"- sim_review: `{(payload.get('sim_review') or {}).get('verdict', 'n/a')}`",
        f"- settled_conformance: `{payload['settled_conformance']}` (focused-judge pass not implemented)",
        f"- architecture_delta: `{payload['architecture_delta']}` (focused-judge pass not implemented)",
        "",
        "## Needs Human",
    ]
    needs = payload.get("needs_human") or []
    if not needs:
        lines.append("- none")
    else:
        lines.extend(f"- {item}" for item in needs)
    lines.extend(["", "## Scorecard"])
    for key, value in payload["scorecard"].items():
        rendered = "n/a" if value is None else value
        lines.append(f"- {key}: {rendered}")
    return "\n".join(lines) + "\n"

