# Golden cases — v0 regression canary

Each case under `golden_cases/<case_id>/` decouples **task** (what's being evaluated) from **configuration** (the system being evaluated). One case can have many configs; cells and baselines are keyed by `(case_id, config_name)` so model swaps don't destroy the task trail and cross-config comparison is a first-class operation. Generalizable eval-design principles surfaced by past cells live in [Methodology notes](#methodology-notes) below; per-`system_digest` run-by-run forensics stay in each operator's gitignored `docs/eval_systems.md`.

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

`default.toml`'s baseline stays intact. Future comparisons are within-config (`compare --config qwen_sim` vs `baselines/qwen_sim.json`); for cross-config comparison, use `eval ab` (below).

## Head-to-head (A/B) comparison

To compare two configs of the same case scientifically — same pinned task, one model variable moved — run:

```bash
contremaitre eval ab <case_id> --config-a default --config-b qwen_sim --n 3
# report only, from runs already on disk:
contremaitre eval ab <case_id> --config-a default --config-b qwen_sim --report-only --open
```

The two arms launch **interleaved** (A,B,A,B,…) so provider load and time-of-day drift spread across both instead of confounding one; on provider quota exhaustion the batch aborts (unequal-n arms in different time windows aren't a comparison) and the report is still written over what completed.

Output is a self-contained `ab--<case_id>--<a>-vs-<b>.html` under the runs root:

- **Provenance + validity checklist** — base-SHA pinning verified per run, judge parity (`cli_reviewer` must match across arms or judge metrics are flagged non-comparable), environment uniformity (contremaitre code/prompts/image/skills identical across all runs), clean-tree and sample-size checks.
- **Every scorecard metric head to head** — the same `check_run` extraction that gates baselines, shown as median [min–max] plus every per-run value. Infra-failed runs (`FAILED_INFRA`, `QUOTA_EXHAUSTED`) stay visible in the roster but are excluded from the metric vectors.
- **Range-separation signal, no p-values** — at n=3 per arm an arm "separates" only when all its values lie strictly beyond the other arm's (one-sided rank-sum p ≈ 0.05); everything else is labelled *overlap*. Win attribution only on metrics with a defensible better-direction; scope metrics (LoC, files, rounds) render as *differs*.
- **Per-run cards** — verdict, PR link, cost/duration/diffstat/token pills, embedded final diff + cli_review body, and a link into each run's `viewer.html` for the full trace.

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

### Diagnostic (informational)

`format_compliance`, `discipline`, `review_depth`, `cli_review_breakdown`, `diff_detail`, `efficiency` panels — see `eval show <case_id> --config <name>` for the full layout.

## Single-variable rule + two-variable guard

**Rule** — between any two cells you intend to compare, change ONE input only: a prompt under `contremaitre/prompts/*.md`, OR a model in `configs/<config>.toml`, OR the cli_reviewer prompt, OR the docker image, OR the contremaitre code. Without a control axis you can't attribute the delta.

**Guard** — `eval compare` warns when both `system_digest` (contremaitre code + prompts + image + skills + models) AND `input_digest` (target + base + cli_reviewer) differ from baseline. The warning doesn't block; it names which variables moved so attribution stays explicit.

## Tracking what each `system_digest` means

A `system_digest` is just a hash. Keep an operator-local journal (`docs/eval_systems.md` is gitignored as an operator's notebook) with an **Intent / Outcome / Learning** entry per new digest. When a Learning generalizes beyond its experiment, lift it into the **Methodology notes** section below so the public canary carries the principle forward.

## Methodology notes

Generalizable principles surfaced by running cells under this canary on real models. These are about *eval design*, not about contremaitre specifically.

- **`n=3` is the floor for continuous metrics, not for 3-state verdicts.** At n=3, continuous panels (`wall_seconds`, `loc_net_delta`, `agent_discipline_score`, `cost_usd`) stabilize. A 3-state LLM verdict mapped to 1.0 / 0.5 / 0.0 produces a near-useless median — one bad sample dominates. Build headlines around continuous metrics; treat 3-state outcomes as distribution shapes (`verdict_mix`), not numeric scores.

- **A/A controls are mandatory before claiming a regression at small n.** Run the same config twice and confirm the regression reproduces. Without the control, a one-cell regression flag is plausibly sampling noise. The ~45min runtime cost buys high-confidence ground truth that the variable you changed really is the cause.

- **Correlated metrics are one signal, not three confirmations.** When three favorable panels move in the same direction, they often share one upstream cause (removing the formatter-MF class shifts `cli_review_score`, `cli_issue_count`, and hard-gates-pass-rate simultaneously). Gate regression-confirmation claims at the metric-family level, not the raw-metric level.

- **Same-family agent ↔ SIM is the bias scenario.** Two same-tier same-harness judges share failure modes — given more structured context, the SIM becomes *less* critical of sibling-model output, not more. Cross-family in the agent ↔ SIM channel is the architectural target; don't add a secondary reviewer axis as a substitute for that primary pairing.

- **Self-reported / heuristic metrics can be structurally zero-pinned.** A flow_use matcher requiring a verbatim ≥20-char grep-output line in the SIM verdict was 0.0 for every SIM ever, because SIMs are prompted to paraphrase. Audit any "low-variance, drop from the gate" recommendation against the *unfiltered* run set before committing it.

## Known limits (v0)

- **n=3 floor is metric-dependent** (L1): continuous panels stabilize, 3-state verdicts don't. `agent_discipline_score` reproduces cleanly A/A; `cli_review_score` median is more variable.
- **L2/L3 LLM judges remain `PENDING`** — only L0 (host hard gates: diff scan, diff-hash, clean worktree, draft-only) and L1 (executable `--check-cmd` results + verdict-format compliance) are deterministic. Cross-family reviewer agreement is tracked but not yet a gate.
- **Real Draft PRs accumulate** on the target fork. Cleanup is manual (`gh pr close --delete-branch ...`) for now.
- **Cross-case / cross-config comparison is manual**. A future `contremaitre eval diff <case_a>:<config_a> <case_b>:<config_b>` subcommand could automate it.

## Why old runs in `.contremaitre/runs/` may not be valid baselines

The artifact contract has evolved (`run_config.json` added partway through; verdict keys renamed; headline panels grew; case-config split). Comparing against pre-refactor runs would conflate orchestrator drift with intended behavior. Baselines must be freshly generated on the current commit. Legacy run slugs (`<ts>-eval-<case_id>-<rep>` without config infix) are accepted by the matcher as `default` config for backward compatibility.
