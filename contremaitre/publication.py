"""The publication decision.

This is the decision the orchestration exists to protect: given the
post-approval facts, may the run open a draft PR? The Module is pure and
in-process — it takes *computed facts* (the diff scan result, whether the
worktree is clean, whether the diff hash still matches the approved one, and
the executable-check results) and returns a decision. It does not know how to
compute those facts: all git I/O stays in the orchestrator, which builds
`GateInputs` and consumes the `GateDecision`.

By the time the decision is reached the SIM verdict has already been resolved
(APPROVED), so the verdict is not an input here — verdict-shaped branching
lives in the orchestrator's review loop.

Precedence: the L0 hard gates (forbidden paths, clean worktree, diff-hash
match, draft-only posture) decide first; only if they pass do the L1
executable `--check-cmd` results gate publication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .checks import CheckResult
from .diffscan import DiffScanResult

# Block reasons — kept identical to the strings the orchestrator emitted before
# the extraction so events / eval payloads / tests are unchanged.
HARD_GATE_REASON = "hard gate failed"
EXECUTABLE_CHECKS_REASON = "executable checks failed"


@dataclass(frozen=True)
class GateInputs:
    """The computed facts the publication decision needs.

    All fields are already-evaluated results — no `GitRepo`, no base ref. The
    orchestrator computes them (diff hash, diff scan, worktree status) and hands
    them across the seam.
    """

    diff_scan: DiffScanResult
    clean_worktree: bool
    diff_hash_matched: bool
    checks: list[CheckResult] = field(default_factory=list)
    draft_only: bool = True


@dataclass(frozen=True)
class GateDecision:
    """The outcome of the publication decision.

    `hard_gates` is the payload `pr_eval.json` already consumes; both
    orchestrator arms read it rather than rebuilding it. `block_reason` is
    `None` exactly when `publish` is True.
    """

    publish: bool
    hard_gates: dict[str, Any]
    block_reason: str | None


def hard_gate_payload(
    *,
    diff_scan: DiffScanResult | None,
    clean_worktree: bool,
    diff_hash_matched: bool,
    draft_only: bool = True,
) -> dict[str, Any]:
    """Build the L0 hard-gate payload (the shape `pr_eval.json` records).

    `clean_worktree` is expected to hold trivially in normal flow because the
    orchestrator commits agent changes before this gate runs. Kept as a
    belt-and-suspenders check: if a downstream change ever moves the commit
    boundary or introduces post-commit edits, this fails loud.
    """

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


def gates_not_evaluated() -> dict[str, Any]:
    """The hard-gate payload for terminal paths that never reached the gate.

    Malformed verdict, missing SETTLED, cap trip, etc. — nothing was evaluated,
    so every L0 check is False except the always-true draft-only posture. One
    owner for this shape instead of a hand-written literal in the orchestrator.
    """

    return {
        "passed": False,
        "checks": {
            "diff_scan": False,
            "clean_worktree": False,
            "diff_hash_matched": False,
            "draft_only": True,
        },
        "forbidden_files": [],
        "changed_files": [],
    }


def decide_publication(inputs: GateInputs) -> GateDecision:
    """Decide whether the post-approval run may publish.

    Hard gates win precedence over executable checks: a forbidden path / dirty
    worktree / diff drift blocks with `HARD_GATE_REASON` even if a check would
    also have failed. Only when the hard gates pass does a failing `--check-cmd`
    block with `EXECUTABLE_CHECKS_REASON`. No `--check-cmd` configured → empty
    results → no-op (the operator opted out; L0 still applies).
    """

    hard_gates = hard_gate_payload(
        diff_scan=inputs.diff_scan,
        clean_worktree=inputs.clean_worktree,
        diff_hash_matched=inputs.diff_hash_matched,
        draft_only=inputs.draft_only,
    )
    if not hard_gates["passed"]:
        return GateDecision(publish=False, hard_gates=hard_gates, block_reason=HARD_GATE_REASON)
    if any(not check.passed for check in inputs.checks):
        return GateDecision(
            publish=False, hard_gates=hard_gates, block_reason=EXECUTABLE_CHECKS_REASON
        )
    return GateDecision(publish=True, hard_gates=hard_gates, block_reason=None)
