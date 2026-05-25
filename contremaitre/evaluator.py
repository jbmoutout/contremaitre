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
from .flow_use import compute_flow_use
from .jsonlog import write_json
from .models import ParsedVerdict, RunPaths, TerminalVerdict


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

    try:
        flow_use = compute_flow_use(paths)
        write_json(paths.flow_use_report, flow_use)
    except Exception as exc:
        flow_use = {"status": "error", "reason": repr(exc), "agent": {}, "sim": {}}
        write_json(paths.flow_use_report, flow_use)

    sim_block = sim_review.get("sim") if isinstance(sim_review.get("sim"), dict) else None
    extra_block = sim_review.get("extra") if isinstance(sim_review.get("extra"), dict) else None
    sim_confidence = sim_block["confidence"] if sim_block else sim_review.get("confidence")
    extra_reviewer_confidence = extra_block["confidence"] if extra_block else None
    cross_family_agreement = sim_review.get("cross_family_agreement")

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
            "sim_confidence": sim_confidence,
            "extra_reviewer_confidence": extra_reviewer_confidence,
            "cross_family_agreement": cross_family_agreement,
            "process_reliability": trajectory.get("process_reliability", 0.0),
            "self_verified": flow_use["agent"].get("self_verified", {}).get("value"),
            "settled_before_code": flow_use["agent"].get("settled_write_before_first_code_edit", {}).get("value"),
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


def combined_review_summary(
    *,
    sim: ParsedVerdict,
    extra: ParsedVerdict | None,
    merged: ParsedVerdict,
    extra_attempted: bool,
) -> dict[str, Any]:
    """Build pr_eval.json's `sim_review` block from the two reviewer verdicts.

    `extra_attempted=False` (extra disabled) returns the flat back-compat
    shape so downstream readers unaware of the extra reviewer see exactly
    the same JSON as before.

    `extra_attempted=True` returns the structured shape: per-reviewer
    sub-objects, `merged_verdict`, `cross_family_agreement`, and the merged
    values at the top level (so downstream readers without extra-reviewer
    awareness still pick up the load-bearing publication verdict).
    """

    sim_payload = _verdict_payload(sim)
    flat = {
        "verdict": sim.verdict.value,
        "confidence": sim.confidence,
        "summary": sim.summary,
        "required_changes": sim.required_changes,
        "checks_performed": sim.checks_performed,
    }
    if not extra_attempted:
        return flat

    extra_payload = _verdict_payload(extra) if extra is not None else None
    cross_family_agreement: bool | None
    if extra is None:
        cross_family_agreement = None
    else:
        cross_family_agreement = sim.verdict == extra.verdict

    return {
        "sim": sim_payload,
        "extra": extra_payload,
        "merged_verdict": merged.verdict.value,
        "cross_family_agreement": cross_family_agreement,
        # Top-level mirrors of the merged verdict — kept so readers (eval
        # markdown, downstream tools) still see a top-level verdict /
        # confidence / required_changes without needing to know about the
        # primary-vs-extra split.
        "verdict": merged.verdict.value,
        "confidence": merged.confidence,
        "summary": merged.summary,
        "required_changes": merged.required_changes,
        "checks_performed": merged.checks_performed,
    }


def _verdict_payload(verdict: ParsedVerdict) -> dict[str, Any]:
    return {
        "verdict": verdict.verdict.value,
        "confidence": verdict.confidence,
        "summary": verdict.summary,
        "required_changes": verdict.required_changes,
        "checks_performed": verdict.checks_performed,
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

