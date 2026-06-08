# CONTEXT

Domain language for Contremaitre. Architecture vocabulary (Module / Interface /
Implementation / Depth / Seam / Adapter / Leverage / Locality) follows the skill glossary;
the terms below name *this project's* concepts so reviews and refactors share words.

## Hard gates (L0)

The deterministic, host-side checks that must pass before a draft PR is published, and again
before any post-publish revision is pushed. Distinct from **L1** executable checks
(`--check-cmd`). The L0 set: forbidden-path diff scan, diff-hash match against the approved
hash, clean worktree (modulo internal paths), draft-only publication. Owned by
[`contremaitre/gates.py`](contremaitre/gates.py) — `evaluate_l0()` runs the recipe and returns
an `L0GateResult`. L0 never folds L1: the two produce separate, user-visible block reasons.

## Internal-path policy

The set of orchestration-internal and conventionally-gitignored build-output paths the
orchestrator tolerates in the worktree without treating them as agent changes:
`.contremaitre`, `opencode.json`, `dist`, `build`, `out`, `.next`, `__pycache__`. The single
source is `gates.INTERNAL_PATHS`. Two Interfaces derive from it: the clean-worktree predicate
(`gates.only_internal_changes`, via `gates.is_internal_path`) and the host-commit
`:(exclude)<path>` pathspecs in `orchestrator._commit_agent_changes`. The two derivations
differ (porcelain matching vs git pathspec) but must not name different sets.
