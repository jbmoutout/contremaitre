# Capabilities roadmap — what to add to contremaitre

**Status:** exploration / notes. Not committed direction. See
[control-plane.md](control-plane.md) for what exists.

## The driving insight

The machinery (sandboxed actors, host-owned git/publish, L0/L1 gates, SIM + CLI
review loops, eval canary) is **general**, but it's pinned to **one skill**
(`improve-codebase-architecture`, hardcoded in
[initial_prompt.md](../contremaitre/prompts/initial_prompt.md)) and **one flow**
(`INIT→WORK→REVIEW→APPROVED→publish`, hardcoded in
[orchestrator.py](../contremaitre/orchestrator.py)). Neither is a first-class seam
— no `--skill`, no `flow` field.

Leverage test for any addition: **does it reuse the paid-for machinery, and does it
feed the gate/eval spine with machine-checkable signal?** The current skill fails
the second half — an architecture refactor is subjectively graded, which is exactly
why L2/L3 sit `PENDING` in [evaluator.py](../contremaitre/evaluator.py) and the eval
leans on proxy metrics. The system is a judging machine running its least-judgeable
skill.

## Ranked

**Tier 1 — keystones**
1. **A machine-checkable skill** — `fix-from-failing-test` or `test-backfill`.
   Output that **L1 already grades** (target test passes, others still pass,
   coverage moved). Adds capability *and* gives the eval canary the objective axis
   it lacks. Reuses 100% of the pipeline. Pair with the `tests-review` skill as SIM.
2. **Build L2** (SETTLED-to-diff conformance judge) — the system's own `PENDING`,
   already designed (focused read-only container, structured verdict). Completes
   eval for the existing skill *and* is the selector a tournament flow needs.

**Tier 2 — new capability, fits existing patterns**
3. **CI-reaction / fix-forward flow** — treat CI as a deterministic reviewer; on
   red, feed the failure into a revision round (reuses the CLI-review revision
   machinery). Turns "a draft PR" into "a PR that's green."
4. **Review-only flow** — point the sandboxed reviewer at an arbitrary existing PR
   (no WORK/publish). Low effort; reuses `_run_cli_review_loop`.

**Tier 3 — powerful but costly / gated**
5. **Tournament / N-attempt flow** — K cheap attempts, judge, publish the winner.
   Needs L2 (#2) as selector; K× cost. After #2 only.

## The enabling move

Make **Skill** and **Flow** first-class seams (`RunConfig.skill` / `RunConfig.flow`
+ dispatch at `Orchestrator.run()`):
- `Skill` Interface = { initial prompt, SIM persona, success-check, eval metrics }.
- `Flow` Interface = the state sequence.

Then every item above drops from "a project" to "a markdown file + a config case."
This is the exact deepening contremaitre's own skill is built to find.

## Don't add
- More subjective-output skills (docs, more refactor variants) — un-gradable.
- More harnesses (incl. pi) — the actor layer isn't the constraint; skill/flow/judge
  layers are. See [harness-pi-exploration.md](harness-pi-exploration.md) §0.

## If you do one thing
#1 on the back of a minimal `--skill` seam: it adds a capability, hardens the eval,
and proves the pluggability that makes #3–#5 cheap.
</content>
