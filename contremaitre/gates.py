"""Hard gates (L0) — the deterministic, host-side publication floor.

This Module gives the named **Hard gates (L0)** concept (see docs/control-plane.md)
a real home. It concentrates the L0 *computation* that the
orchestrator runs in two places — once before publishing a draft PR, once before
pushing a post-publish revision — behind one small typed Interface.

It deliberately owns only the deterministic computation:

  - recompute the diff hash and compare it to the approved/expected hash,
  - scan the diff for forbidden paths,
  - decide whether the worktree is clean (modulo the internal-path policy),
  - assemble the eval-artifact payload.

It does NOT own:

  - **L1 executable checks** (`--check-cmd`). The two call sites combine L1 with L0
    differently and produce *different, user-visible* block reasons, so L1 stays
    entirely caller-side. `L0GateResult.passed` is L0-only.
  - the `HARD_GATES_CHECKED` telemetry. The emits legitimately diverge per call
    site (the revision path folds L1 into its `passed` and adds `context` / `round`
    / `failed_checks`), so each caller projects the event from its `L0GateResult`.
  - the eval-artifact *schema*. `evaluate_l0` calls `evaluator.hard_gate_payload`
    to build `.payload`; the dict shape stays where the eval reports live.
"""

from __future__ import annotations

from dataclasses import dataclass

from .diffscan import DiffScanResult, scan_diff
from .git_utils import GitRepo
from .verdicts import diff_hash

# Single source for the orchestration-internal / build-output tolerance policy.
# `.contremaitre` and `opencode.json` are orchestration-internal; the rest are
# conventionally-gitignored build output that some agents produce as a
# verification step (the worktree may not carry the upstream .gitignore for all
# of them, so we belt-and-suspenders). Two Interfaces derive from this tuple with
# two different derivations — the clean-worktree predicate below, and the
# host-commit `:(exclude)<path>` pathspecs in `orchestrator._commit_agent_changes`
# — but they must never name different sets.
INTERNAL_PATHS: tuple[str, ...] = (
    ".contremaitre",
    "opencode.json",
    "dist",
    "build",
    "out",
    ".next",
    "__pycache__",
)


def is_internal_path(path: str) -> bool:
    """True iff `path` is an orchestration-internal / tolerated build-output path.

    Exact matches are limited to true orchestration-internal paths
    (`.contremaitre`, `opencode.json`). Build-output names are tolerated only
    as directories or directory contents (e.g. `dist/`, `dist/foo`), so a real
    root file named `dist` does not silently pass the clean-worktree gate.
    """

    if path in (".contremaitre", "opencode.json"):
        return True
    return any(path.startswith(n + "/") for n in INTERNAL_PATHS)


def only_internal_changes(porcelain: str) -> bool:
    """True iff every `git status --porcelain` row is an internal/tolerated path.

    Files excluded from commits by pathspec (`.contremaitre/*`, `opencode.json`)
    are deliberately untracked in the worktree. The host-commit step and the
    clean-worktree hard gate both treat a worktree whose only changes are in these
    paths as "clean for our purposes":

    - host-commit: skip instead of producing an empty PR.
    - clean-worktree gate: pass.

    Empty porcelain (no changes at all) is also "clean".
    """

    for line in porcelain.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if not is_internal_path(path):
            return False
    return True


def hard_gate_payload(
    *,
    diff_scan: DiffScanResult | None,
    clean_worktree: bool,
    diff_hash_matched: bool,
    draft_only: bool = True,
) -> dict[str, object]:
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


@dataclass(frozen=True)
class L0GateResult:
    """Outcome of one L0 evaluation. `passed` is L0-only — it never folds L1."""

    passed: bool
    recomputed_hash: str
    diff_hash_matched: bool
    diff_scan: DiffScanResult
    clean_worktree: bool
    payload: dict


def evaluate_l0(
    *,
    worktree_git: GitRepo,
    diff_base: str,
    expected_hash: str,
) -> L0GateResult:
    """Run the deterministic L0 gate recipe against the worktree.

    `expected_hash` is the diff hash captured at SIM-APPROVED (publish path) or at
    the start of a post-publish revision round. The returned `payload` is the
    `evaluator.hard_gate_payload` dict, unchanged in schema, ready to thread into
    `_write_eval` / `_blocked_by_gates`.
    """

    recomputed_hash = diff_hash(worktree_git, diff_base)
    diff_hash_matched = recomputed_hash == expected_hash
    diff_scan = scan_diff(worktree_git, diff_base)
    clean = only_internal_changes(worktree_git.status_porcelain())
    payload = hard_gate_payload(
        diff_scan=diff_scan,
        clean_worktree=clean,
        diff_hash_matched=diff_hash_matched,
    )
    return L0GateResult(
        passed=bool(payload["passed"]),
        recomputed_hash=recomputed_hash,
        diff_hash_matched=diff_hash_matched,
        diff_scan=diff_scan,
        clean_worktree=clean,
        payload=payload,
    )
