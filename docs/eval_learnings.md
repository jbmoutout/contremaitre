# Eval Learnings

Methodology learnings from building and running the v0 regression canary. Distinct from [`LEARNINGS.md`](../LEARNINGS.md) (gitignored, per-run facts) and [`EVAL_ROADMAP.md`](../EVAL_ROADMAP.md) (forward-looking plan). This doc is committed and grows over time as we use the canary against real changes.

Each entry: a learning, the evidence, what it means for future eval design.

---

## L1 — n=3 is a metric-dependent floor, not universal

**Learning**: n=3 per cell is adequate for *continuous* metrics (`wall_seconds`, `loc_net_delta`, `context_pollution_events`, `tokens_to_settled_design`) but below the detection floor for *discrete few-state outcome* metrics (3-state codex verdicts, terminal-verdict buckets).

**Evidence**:
- `cli_review_score` derives from LOOKS_GOOD/NEEDS_ATTENTION/MUST_FIX → 1.0/0.5/0.0. At n=3 the median has ~5 possible values, and a single MF dominates → range spans [0, 1] regularly.
- An A/A control (same prompt, two separate n=3 cells) confirmed the cell-to-cell variance on `cli_review_score` median was small (both at 0.0), but a single LG-vs-MF swing was enough to move the median across the 0.30 envelope.
- Meanwhile `agent_discipline_score` (continuous composite over exploration_convergence + sim_useful_call_ratio + self_verified) returned identical median 0.333 between the two A/A cells. Continuous distributions stabilize at n=3 in a way 3-state ones don't.

**Implication**:
- Build scorecard headlines around continuous metrics where possible.
- Treat 3-state-derived medians as categorical-distribution signals (`verdict_mix`), not numeric scores, at n=3.
- If a 3-state metric MUST be the headline, plan for n≥10 or accept "only catastrophic regressions detectable."

## L2 — The cli_reviewer IS the LLM-as-judge; don't demote it on category error

**Learning**: codex (or claude) running over the produced PR after publication and emitting a verdict + structured findings IS the LLM-as-judge per any standard definition. The EVAL_ROADMAP §6 "LLM judges last" defers *focused* judges (SETTLED→diff conformance, architecture-delta), not the general one we already have running. Demoting the production reviewer to "diagnostic" on the theory it's "operational" rather than "evaluative" is a category error.

**Evidence**:
- The cli_reviewer reads the PR, applies a structured rubric (verdict + Conventional Comments labels), emits parseable output. That's the textbook judge shape.
- Cross-family (codex judging deepseek/qwen output) is already the bias mitigation §5 wants. Decoupling would require re-engineering it.
- The reviewer's verdict directly reflects what an operator sees on the PR — it's the most operationally meaningful single signal we have.

**Implication**:
- Keep `cli_review_score` on the headline.
- The right critique of the 3-state encoding (see L1) is "wrong derived metric," not "wrong category." Mine the full review output (severity-weighted findings, citation density) rather than collapsing to one number.
- A future *separate* focused judge (per §6) decouples eval calibration from production reviewer calibration. Useful when we want sub-LG/NA/MF granularity, but not blocking v0.

## L3 — Mine the structured text, not just the verdict key

**Learning**: `cli_review_score` (3-state → 1.0/0.5/0.0) throws away most of codex's per-PR output. The same MUST_FIX hides "1 trivial nit" and "4 CI breaks + a behavior regression." The text already has labels (`**issue:**`/`**suggestion:**`/`**nit:**`/`**question:**`) that imply severity — count and weight them.

**Evidence**:
- Two consecutive MUST_FIX runs on case_01 had `cli_findings_weighted` = 3 and 12 (severity-weighted: issue×3 + suggestion×2 + nit×1). Both collapse to `cli_review_score = 0.0` in the old encoding. The 4× weighted gap is the actual signal about *how bad* each run was.
- `cli_citation_density` (`path:line` citations per finding) drifted from 2.0 (baseline) to 1.0 (post-prompt) — a separate-and-independent signal about reviewer thoroughness.

**Implication**:
- Promote `cli_findings_weighted`, per-label counts, and `cli_citation_density` to headline alongside the verdict-derived score. Each adds a distinct axis.
- The same principle applies to SIM verdicts: `review_cycles.jsonl` has `checks_performed` and `required_changes` arrays per round. Counts of those carry more information than the verdict alone.

## L4 — Always validate a regression hypothesis with an A/A control before iterating

**Learning**: n=3 is small enough that a single regression signal is plausibly sampling noise. Before drawing conclusions or making prompt edits to "fix" the regression, run a second n=3 with the *same* config and compare cell-to-cell. If the regression reproduces, it's real; if not, it was noise.

**Evidence**:
- After the sim_tooled_persona.md edit, cell A showed `cli_review_score = 0.0` (vs baseline 0.5). A/A cell B reproduced the same outcome with the verdict_mix collapsing further (MF×3, no LG).
- `agent_discipline_score` median was identical between cells A and B (0.333), confirming the process-quality shift wasn't sampling.
- Total cost: ~45min of extra runtime. Net win: high-confidence ground truth before another iteration cycle.

**Implication**:
- Standard workflow: when `eval compare` flags a regression, run a second n=3 with the same config. Treat the first signal as a hypothesis; the A/A is the confirmation.
- Some panels are stable A/A (agent_discipline_score, verdict mix shape, exploration_convergence) and some are noisy (loc_net_delta swung 432→109 between A and B). Trust the stable ones for attribution.

## L5 — Same-family agent+SIM is the bias scenario; cross-family is the architectural target

**Learning**: Running agent and SIM on the same model family (both deepseek, both claude, etc.) creates same-family bias per [Future AGI 2026](https://futureagi.com/blog/evaluating-llm-judge-bias-mitigation-2026/). The SIM is more lenient on agent outputs because it "thinks like" the agent. Cross-family in the agent↔SIM channel is the architectural target, not just a §5 ideal.

**Evidence**:
- Case_01 baseline: agent=deepseek, SIM=deepseek, reviewer=codex. The reviewer was the only cross-family voice. SIM's `total_required_changes` median was 3 in baseline; after a prompt change reduced grilling, it dropped to 0 — and codex (cross-family) caught real bugs the same-family SIM rubber-stamped.
- The "weak SIM" hypothesis and the "same-family bias" hypothesis predict the same observable: SIM defers, reviewer catches issues. Disambiguating them requires swapping the SIM model.

**Implication**:
- Pick SIM model from a different family than the agent (and ideally a different family than the reviewer too).
- For Qwen agent / Qwen SIM / Codex reviewer: still two-family. For Deepseek agent / Qwen SIM / Codex reviewer: full three-family. Prefer the latter.

## L6 — Cache must refresh on every run; trust only remote-tracking refs

**Learning**: The clone cache (`~/.cache/contremaitre/<host>-<owner>-<repo>/`) is a performance hop for git objects, not a source of truth for refs. Local branches in the cache (`refs/heads/<base>`) are never used by the orchestrator. Preflight, worktree creation, and base-SHA capture all flow through `origin/<base>` (refreshed at run start).

**Evidence**:
- First eval cycle on sqlite-utils hit "base ref not found: eval/case-1" — the freshly-pushed branch existed on GitHub but the local cache didn't know about it.
- Initial preflight checked `git rev-parse --verify <base>` (local ref) — failed because the cache had `refs/remotes/origin/<base>` but no local branch.
- Fix: `_ensure_local_clone` now does `git fetch --prune origin <base>` on every run (best-effort, offline-tolerant); preflight checks `origin/<base>` to match the orchestrator's `_create_worktree`.

**Implication**:
- Any code touching the clone cache must use `origin/<base>`, not bare `<base>`.
- Eval cases pin refs by creating branches on the GitHub fork (`gh api -X POST .../git/refs`), not by maintaining local checkouts.
- Stored as project memory: see `~/.claude/projects/-Users-jbmoutout-code-contremaitre/memory/project_remote_only_refs.md`.

## L7 — FreeUsageLimitError detection must scan opencode's internal log, not just stdout

**Learning**: opencode classifies `FreeUsageLimitError` (and similar provider-side quota errors) as `isRetryable: true` and silently retries the API call without surfacing the error event to stdout. The fast-fail detector scanning raw_export.jsonl missed it; opencode hammered the API until the docker timeout fired, appearing as a stuck eval CLI with no progress.

**Evidence**:
- A real run sat for many minutes with no agent progress; the marker `FreeUsageLimitError` was in `<run_dir>/opencode-{role}-state/log/<ISO>.log` (2 occurrences from opencode's retry loop) but never in `raw_export.jsonl`.
- Fix: `_detect_provider_quota_exhausted` now scans both — raw_export (existing, gated by baseline_text_count) AND the latest log file in `state_dir/log/` (new). Polling at 2s interval triggers the existing fast-fail path within 2s of the error landing in the log.

**Implication**:
- For any provider whose CLI client silently retries: detect at the host-side log level, not just the stdout level.
- Eval canary's `cmd_run` aborts the batch on `terminal=QUOTA_EXHAUSTED` (distinct from `FAILED_INFRA`) with an actionable message; the operator chooses (wait / switch model / accept partial cell).

## L8 — IP-based rate limits are not the eval's fight to win

**Learning**: opencode-zen's free tier rate-limits by IP for anonymous traffic (no account auth). VPN rotation (Gluetun + Proton free or similar) could rotate the IP but: (a) likely violates the provider's ToS, (b) Proton-free IPs are probably already on opencode-zen's noisier-neighbor lists with stricter limits, (c) introduces an uncontrollable variable that breaks eval reproducibility, (d) the intended escape valve already exists — opencode-zen supports `/connect` to mint an account API key that gives a much higher quota bucket.

**Evidence**:
- Tested anonymous opencode-zen for case_01: persistent rate-limit errors on multi-turn runs.
- After `/connect` + API key plumbing: runs complete cleanly.

**Implication**:
- Default eval setup: use authenticated opencode-zen OR paid OpenRouter, never anonymous gateways. The hidden-variable cost of "which IP did we land on?" exceeds the dollar saving.

## L9 — System digest must include models (the SUT is models + code + prompts)

**Learning**: `system_digest` (used by the two-variable guard and cell-comparability checks) initially hashed contremaitre git SHA + dockerfile + skills-lock + prompt files but NOT `agent_model` / `sim_model` / `extra_reviewer_model`. A model swap would change the system under test invisibly to the canary — two cells with different SIM models would appear comparable.

**Evidence**:
- Discussed before kicking off the Qwen SIM experiment. The risk: swapping `sim_model = deepseek` → `sim_model = qwen` with the original digest function leaves both cells' `system_digests` identical → the canary aggregates them as if they were the same system.

**Implication**:
- `agent_model`, `sim_model`, `extra_reviewer_model` are now part of `manifest_digest`. Any model swap forces a fresh baseline (system_digest moves).
- `cli_reviewer` stays in `input_digest` (it's a judge choice, not the SUT — analogous to choosing which test suite runs).
- When the operator interprets system_digest movements, they need to know that contremaitre infra commits (eval.py refinements, etc.) also move the digest — not every digest change is an experimental variable change.

## L9b — Case identity = (target, base_sha, model combination); model swaps make new cases

**Learning**: A "case" is a pinned input tuple — not just `(target, base_sha)` but `(target, base_sha, agent_model, sim_model, cli_reviewer)`. Swapping any of those produces a different system being evaluated against the same target. The clean shape is a *new case directory* with its own baseline, not an in-place edit of the existing case's `case.toml`.

**Evidence**:
- Initial instinct was to edit `case_01_sqlite_utils_8f0c06e/case.toml` to point at Qwen for the cross-family SIM experiment. The user caught this: in-place edit destroys the deepseek-SIM trail in `baseline.json` and overwrites the case_01 reference.
- Correct shape: `case_02_sqlite_utils_8f0c06e_qwen_sim/case.toml` as a sibling. Same target, same base, same agent — different SIM. Two independent baselines, two independent run trails (run dirs are slug-namespaced by `eval-<case_id>-`), one shared GitHub branch (`eval/case-1`).

**Implication**:
- Case naming convention: `case_NN_<target_slug>_<sha7>[_<flavor>]`. Flavor suffix describes what differs from the predecessor (e.g. `_qwen_sim`, `_claude_reviewer`).
- `baseline.json` is per-case, not per-target. Cells from different cases are NOT directly comparable via `eval compare` (they have different `system_digests`); cross-case comparison is a manual operator task for v0.
- This keeps the trail: `git log golden_cases/<case_id>/` shows the case's own history; old cells stay intact in `.contremaitre/runs/<ts>-eval-<case_id>-NN/` namespaced by case_id.
- Future: a `contremaitre eval diff <case_a> <case_b>` subcommand could surface the cross-case delta cleanly. Not v0.

## L10 — Bootstrap moves are sometimes necessary; document them

**Learning**: Twice in v0 we needed to manually edit baseline state to recover from schema/discipline mismatches: (a) patching `contremaitre_git_dirty=false` in baseline runs' run_config.json after the dirty-flag guard caught legitimately-iterated commits; (b) re-aggregating baseline.json after adding headline fields and again after adding models to system_digest.

**Evidence**:
- Both bootstraps were ad-hoc Python invocations against the canonical aggregation machinery. Same code paths as a normal `eval promote`; just with explicit run_dir selection instead of `latest_n_runs_for_case`.

**Implication**:
- Don't engineer a "general manual override" CLI for v0. Each bootstrap was different enough that a generic flag would be overfitting.
- DO document bootstraps in commit messages and this learnings doc so future contributors know what they're seeing in the git log.
- A `contremaitre eval rebaseline --from-runs <ids>` subcommand might be worth adding if this happens a third time — third use is the design signal.

---

*Add new learnings above this line as we accumulate them. Each entry: learning + evidence + implication.*
