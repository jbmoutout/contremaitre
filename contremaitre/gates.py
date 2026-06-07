"""Publication gate runner.

A single seam between the orchestrator and the five pre-publication checks it
previously stitched together manually: diff-hash drift, diff scan, clean-worktree,
hard-gate payload assembly, and executable-check evaluation.

The Protocol + DefaultGateRunner pattern mirrors actors.py (ActorRunner /
FakeActorRunner / OpencodeActorRunner): one protocol, one production adapter,
one test stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .checks import CheckResult
from .diffscan import DiffScanResult, scan_diff
from .git_utils import GitRepo
from .scaffolds import only_contremaitre_changes
from .verdicts import diff_hash


# ---------- result type ----------


@dataclass(frozen=True)
class GateResult:
    """Outcome of a single gate pass.

    `passed` is the AND of all L0 (diff_scan, clean_worktree, diff_hash_matched)
    and L1 (executable checks) gates. Individual fields give the breakdown so
    callers can distinguish which gate failed and build structured reports.

    `payload` is the serialisable dict assembled by the gate, consumed as-is by
    the eval writer (write_eval_reports). It carries only L0 state — the same
    shape hard_gate_payload() used to return — so the eval schema is unchanged.
    """

    passed: bool
    diff_hash: str
    diff_hash_matched: bool
    diff_scan: DiffScanResult | None
    clean_worktree: bool
    checks_failed: bool
    payload: dict[str, Any]


# ---------- protocol ----------


class GateRunner(Protocol):
    def run(
        self,
        *,
        worktree_git: GitRepo,
        base: str,
        approved_hash: str,
        checks: list[CheckResult],
    ) -> GateResult: ...


# ---------- production adapter ----------


class DefaultGateRunner:
    """Runs the full pre-publication gate sequence.

    Ordering is enforced here, not at the call site:
      1. recompute hash (drift check)
      2. scan diff for forbidden paths
      3. check worktree cleanliness
      4. assemble L0 payload
      5. evaluate L1 executable checks
    """

    def run(
        self,
        *,
        worktree_git: GitRepo,
        base: str,
        approved_hash: str,
        checks: list[CheckResult],
    ) -> GateResult:
        recomputed_hash = diff_hash(worktree_git, base)
        diff_hash_matched = recomputed_hash == approved_hash
        diff_scan_result = scan_diff(worktree_git, base)
        clean = only_contremaitre_changes(worktree_git.status_porcelain())
        payload = _hard_gate_payload(
            diff_scan=diff_scan_result,
            clean_worktree=clean,
            diff_hash_matched=diff_hash_matched,
        )
        checks_failed = any(not check.passed for check in checks)
        return GateResult(
            passed=bool(payload["passed"]) and not checks_failed,
            diff_hash=recomputed_hash,
            diff_hash_matched=diff_hash_matched,
            diff_scan=diff_scan_result,
            clean_worktree=clean,
            checks_failed=checks_failed,
            payload=payload,
        )


# ---------- test stub ----------


class FakeGateRunner:
    """Returns a preset GateResult without touching the filesystem or git.

    Construct with a fully-built GateResult so individual tests can exercise
    specific sub-paths (e.g. passed=False + diff_hash_matched=True for a
    drift-only rejection, or a forbidden-files scan result with passed=True
    for the eval-output path) without encoding that branching inside the stub.
    """

    def __init__(self, *, result: GateResult) -> None:
        self._result = result

    def run(self, **_kwargs: object) -> GateResult:
        return self._result


# ---------- internal ----------


def _hard_gate_payload(
    *,
    diff_scan: DiffScanResult | None,
    clean_worktree: bool,
    diff_hash_matched: bool,
    draft_only: bool = True,
) -> dict[str, Any]:
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
