# Golden cases — v0 regression canary

Pinned `(target_url, base_sha)` inputs run by the **real opencode actor** with real prompts, real models, and the codex cli_reviewer. The canary measures emergent agent + reviewer behavior across n=3 runs and compares to a per-case baseline. See [`EVAL_ROADMAP.md`](../EVAL_ROADMAP.md).

Fake-actor scaffolds live under [`smoke_cases/`](../smoke_cases/) — they're integration tests of the state machine, not evals.

## Layout

```
golden_cases/
  case_<NN>_<slug>_<sha7>/
    case.toml       # target + base + models + cli_reviewer
    baseline.json   # written only by `contremaitre eval promote`
```

## `case.toml` schema

```toml
id = "case_01_sqlite_utils_8f0c06e"
description = "Human-readable one-liner; documentation only, not fed to the agent."

target_url = "git@github.com:<you>/<target>.git"
base = "eval/case-N"                                        # ref name, pinned by tag/branch
expected_base_sha = "8f0c06e..."                            # cell rejects runs whose base_sha differs
publish_mode = "gh"                                         # required for cli_reviewer to fire

[models]
agent_model = "opencode/deepseek-v4-flash-free"
sim_model = "opencode/deepseek-v4-flash-free"
cli_reviewer = "codex"                                      # or "claude"; v0 ships codex only
extra_reviewer_model = "..."                                # optional, for cross-family agreement
```

## Run it

```bash
contremaitre eval run case_01_sqlite_utils_8f0c06e --n 3      # ~3 × ~15 min
contremaitre eval compare case_01_sqlite_utils_8f0c06e        # diff vs baseline (no-op on first run)
contremaitre eval promote case_01_sqlite_utils_8f0c06e        # snapshot cell as baseline
```

`eval compare` exits 1 on any regression (a headline panel drops past its envelope, format-compliance drops, or terminal-verdict mix worsens). `eval promote` refuses if any contributing run had a dirty contremaitre tree (`contremaitre_git_dirty=true`) or if any cli_review failed to parse (which would normalize a baseline to a reviewer-prompt bug).

## Adding a case

1. Pick a target repo + commit SHA you want to canary against. Fork it to your GitHub account if you haven't.
2. Create a stable branch on your fork pointing at the SHA:
   ```bash
   gh api -X POST repos/<you>/<target>/git/refs \
     -f ref="refs/heads/eval/case-N" -f sha="<SHA>"
   ```
3. Write `golden_cases/case_NN_<slug>_<sha7>/case.toml` per the schema above.
4. Run `contremaitre eval run <case_id> --n 3`. Inspect each `eval/canary.json`. If all three look right (and 3/3 are `ok: true`):
5. Commit (so `contremaitre_git_dirty=false`), then `contremaitre eval promote <case_id>`. The baseline now lives in the repo.

## What's measured

### Headline (7 panels — drives pass/fail)

| Panel | Source | Direction | Regression rule |
|---|---|---|---|
| `cli_review_score` | `<tool>_review.md` line 1 → LOOKS_GOOD=1.0 / NEEDS_ATTENTION=0.5 / MUST_FIX=0.0 | ↑ | drop ≥ 0.30 |
| `terminal_score` | `stats.json.verdict` → READY=1.0 / NO_PR_*=0.0 / FAILED=−1.0 | ↑ | any drop |
| `files_changed` | parse latest `review_diff_round_<N>.diff` | flat | outside ±50% |
| `loc_net_delta` | added − deleted from same diff | flat | outside ±50% |
| `review_rounds` | `review_cycles.jsonl` distinct rounds | ↓ | outside ±50% |
| `cost_usd` | `stats.json.recorded_cost_usd` | ↓ | outside ±20% |
| `wall_seconds` | `stats.json.duration_seconds` | ↓ | outside ±30% |
| `cross_family_agreement_rate` | `pr_eval.json.scorecard.cross_family_agreement` | ↑ | drop ≥ 0.30 |

### Diagnostic (informational; named in regression report when headline shifts)

- **format_compliance**: `cli_review_parse_ok`, `sim_verdicts_parse_ok`, `hard_gates_passed`, `implementation_complete_written`.
- **discipline**: `settled_before_code`, `self_verified`, `runtime_install_required`, `context_pollution_events`, `exploration_convergence`, `time_to_settled_design_seconds`, `tokens_to_settled_design`, `sim_useful_call_ratio`.
- **review_depth**: `total_checks_performed`, `total_required_changes`, `sim_review_confidence`, `extra_reviewer_confidence`, `process_reliability`.
- **cli_review_breakdown**: `finding_count`, `citation_count`, `by_label` (issue / suggestion / nit / question / praise / thought).
- **diff_detail**: `loc_added`, `loc_deleted`.
- **efficiency**: `turns`, `agent_tool_call_count`, `sim_tool_call_count`.

## Two-variable guard

Per [EVAL_ROADMAP §5](../EVAL_ROADMAP.md), bump one variable at a time. `eval compare` emits a loud warning when **both** the `system_digest` (contremaitre code / prompts / image / skills) **and** the `input_digest` (target / base / cli_reviewer) differ from the baseline. The warning doesn't block; it names which variables moved so attribution stays clean. Re-baseline after changing one variable before bumping the next.

## Known limits (v0)

- **Coarse resolution**: cli_review_score has 3 values per run × n=3 = 7 possible cell means. Complement with `finding_count` + `citation_count` (continuous).
- **Same-family bias**: if `agent_model = claude-*` and `cli_reviewer = claude`, [Future AGI](https://futureagi.com/blog/evaluating-llm-judge-bias-mitigation-2026/) cites 10–25% inflation. Codex-only reviewer in v0; **claude-side rotation is deferred** — add a second case with `cli_reviewer = "claude"` once the codex baseline is stable.
- **Reviewer-prompt regressions** look like agent regressions: a `cli_reviewer_prompt.md` edit that breaks `parse_verdict()` will tank the score. Diagnostics name the manifest delta so attribution is recoverable.
- **L2/L3 LLM judges** (settled-conformance, architecture-delta) stay `PENDING` per [EVAL_ROADMAP §6](../EVAL_ROADMAP.md). Adding them requires calibrated rubrics + human-anchored findings files.
- **Real Draft PRs accumulate** on the target fork. Cleanup is manual (`gh pr close --delete-branch ...`) for now.

## Why old runs in `.contremaitre/runs/` are not valid baselines

The artifact contract has evolved (`run_config.json` added partway through May, verdict keys renamed in commit `1db1dcc`, `tool_use.json` → `eval/flow_use.json`). Comparing against pre-canary runs would conflate orchestrator drift with intended behavior. Baselines must be freshly generated on the current commit.
