# Golden cases — v0 regression canary

Each case under `golden_cases/<case_id>/` decouples **task** (what's being evaluated) from **configuration** (the system being evaluated). One case can have many configs; cells and baselines are keyed by `(case_id, config_name)` so model swaps don't destroy the task trail and cross-config comparison is a first-class operation. Methodology lessons that emerged from specific system versions are recorded in [`docs/eval_systems.md`](../docs/eval_systems.md) (one Intent / Outcome / Learning block per `system_digest`).

Fake-actor scaffolds live under [`smoke_cases/`](../smoke_cases/) — they're integration tests of the state machine, not evals.

## Layout

```
golden_cases/
  case_<NN>_<target_slug>_<sha7>/
    case.toml                # task: target_url, base, expected_base_sha
    configs/
      default.toml           # one (agent, sim, reviewer) combo
      <flavor>.toml          # additional combos (qwen_sim, claude_reviewer, ...)
    baselines/
      default.json           # baseline per config (written by `eval promote`)
      <flavor>.json
```

## `case.toml` schema (task only)

```toml
id = "case_01_sqlite_utils_8f0c06e"
description = "Human-readable one-liner."
target_url = "git@github.com:<you>/<target>.git"
base = "eval/case-N"                                        # ref name pinned by tag/branch
expected_base_sha = "8f0c06e..."                            # cell rejects runs whose base_sha differs
```

## `configs/<name>.toml` schema (system under test)

```toml
publish_mode = "gh"                                         # required for cli_reviewer to fire

[models]
agent_model = "opencode/deepseek-v4-flash-free"
sim_model = "opencode/deepseek-v4-flash-free"
cli_reviewer = "codex"                                      # or "claude"
extra_reviewer_model = "..."                                # optional cross-family second SIM
```

Convention: config names use underscores (no dashes) so run slugs `<ts>-eval-<case_id>-<config>-<rep>` parse unambiguously.

## Run it

```bash
contremaitre eval run case_01_sqlite_utils_8f0c06e --config default --n 3     # ~3 × ~15 min
contremaitre eval show case_01_sqlite_utils_8f0c06e --config default          # pretty scorecard
contremaitre eval compare case_01_sqlite_utils_8f0c06e --config default       # vs baseline
contremaitre eval promote case_01_sqlite_utils_8f0c06e --config default       # snapshot cell as baseline
```

`eval compare` exits 1 on any regression. `eval promote` refuses if any contributing run had a dirty contremaitre tree, fewer than 3 runs, or a cli_review that failed to parse.

## Adding a case

1. Pick a target repo + commit SHA. Fork to your GitHub account if needed.
2. Create a stable branch on the fork:
   ```bash
   gh api -X POST repos/<you>/<target>/git/refs \
     -f ref="refs/heads/eval/case-N" -f sha="<SHA>"
   ```
3. Create `golden_cases/case_NN_<slug>_<sha7>/case.toml` (task fields) and `configs/default.toml` (first config).
4. Run `contremaitre eval run <case_id> --config default --n 3`. Inspect each `eval/canary.json`. If all three are `ok: true`:
5. Commit, then `contremaitre eval promote <case_id> --config default`. Baseline lives at `baselines/default.json`.

## Adding a configuration to an existing case

To test the same task with a different model combination (e.g. cross-family SIM, alternative reviewer), add a sibling file under `configs/` rather than editing `default.toml`:

```bash
# Copy and edit
cp golden_cases/<case_id>/configs/default.toml golden_cases/<case_id>/configs/qwen_sim.toml
# Edit qwen_sim.toml to point at the new model
contremaitre eval run <case_id> --config qwen_sim --n 3
contremaitre eval promote <case_id> --config qwen_sim
```

`default.toml`'s baseline stays intact. Future comparisons are within-config (`compare --config qwen_sim` vs `baselines/qwen_sim.json`); cross-config comparison is a deliberate manual operator action for v0.

## What's measured

### Headline panels (drive pass/fail)

**LLM judge (cli_reviewer):**
- `cli_review_score` — LG=1.0 / NA=0.5 / MF=0.0 (3-state derived; coarse at n=3 — see L1)
- `cli_findings_weighted` — issue×3 + suggestion×2 + nit×1 (severity-weighted, mined from the review text)
- `cli_issue_count` / `cli_suggestion_count` / `cli_nit_count` — per-label distributions
- `cli_citation_density` — `path:line` citations per finding (grounding signal)

**Orchestrator-side (process):**
- `agent_discipline_score` — continuous composite over exploration_convergence + sim_useful_call_ratio + self_verified
- `terminal_score` — READY=1.0 / NO_PR_*=0.0 / FAILED_INFRA=-1.0
- `files_changed`, `loc_net_delta`, `review_rounds`, `cost_usd`, `wall_seconds`
- `cross_family_agreement_rate` (when extra_reviewer_model is configured)

### Diagnostic (informational)

`format_compliance`, `discipline`, `review_depth`, `cli_review_breakdown`, `diff_detail`, `efficiency` panels — see `eval show <case_id> --config <name>` for the full layout.

## Two-variable guard

Per [EVAL_ROADMAP §5](../EVAL_ROADMAP.md), bump one variable at a time. `eval compare` warns when both `system_digest` (contremaitre code + prompts + image + skills + models) AND `input_digest` (target + base + cli_reviewer) differ from baseline. The warning doesn't block — it names which variables moved so attribution stays clean.

## Tracking what each `system_digest` means

A `system_digest` is just a hash. To interpret one later, append an **Intent / Outcome / Learning** entry to [`../docs/eval_systems.md`](../docs/eval_systems.md) whenever a new digest appears. The journal IS the methodology doc — principles are recorded next to the specific experiment that produced them, so future operators see the lesson in context of why it was learned.

## Known limits (v0)

- **n=3 floor is metric-dependent** (L1): continuous panels stabilize, 3-state verdicts don't. `agent_discipline_score` reproduces cleanly A/A; `cli_review_score` median is more variable.
- **L2/L3 LLM judges remain `PENDING`** per [EVAL_ROADMAP §6](../EVAL_ROADMAP.md).
- **Real Draft PRs accumulate** on the target fork. Cleanup is manual (`gh pr close --delete-branch ...`) for now.
- **Cross-case / cross-config comparison is manual**. A future `contremaitre eval diff <case_a>:<config_a> <case_b>:<config_b>` subcommand could automate it.

## Why old runs in `.contremaitre/runs/` may not be valid baselines

The artifact contract has evolved (`run_config.json` added partway through; verdict keys renamed; headline panels grew; case-config split). Comparing against pre-refactor runs would conflate orchestrator drift with intended behavior. Baselines must be freshly generated on the current commit. Legacy run slugs (`<ts>-eval-<case_id>-<rep>` without config infix) are accepted by the matcher as `default` config for backward compatibility.
