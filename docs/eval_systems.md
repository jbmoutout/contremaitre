# Eval Systems Journal

One entry per unique `system_digest` that's been used for an eval cell. Each entry captures:

- **Intent** — the hypothesis the bump was meant to test
- **Outcome** — what the cells produced
- **Learning** — the methodology principle that emerged

The journal IS the methodology doc. Generalizable principles emerge from specific experiments, so they're recorded next to the experiment that produced them rather than in a separate maintained-in-parallel file. A new system version may re-derive an old principle (good — confirms it across cases) or surface a new one.

> `system_digest` is recomputed from each run's `run_config.json` at `check_run` time, so values reported by `eval show` reflect the CURRENT digest function. Old `canary.json` files may carry stale digests from a prior digest schema; trust the current computation.

---

## sys 27666e87 — 2026-05-28

**Intent**: establish the baseline reference cell for case_01.

**Outcome**: n=3 produced 1 NO_PR_NEEDS_HUMAN + 1 LOOKS_GOOD + 1 MUST_FIX. Range on `cli_review_score` spans [0.0, 1.0] — the full scale on 3 samples.

**Learning**: at n=3, a 3-state LLM verdict mapped to 1.0/0.5/0.0 produces a near-useless median — one bad sample dominates. Continuous metrics (`wall_seconds`, `loc_net_delta`, `tokens_to_settled_design`, `context_pollution_events`) stabilize at n=3 in a way 3-state ones don't. Build scorecard headlines around continuous metrics where possible; treat 3-state outcomes as distribution shapes (`verdict_mix`), not numeric scores.

- contremaitre @ `ce28777`
- `sim_tooled_persona.md`: original
- agent / sim: `opencode/deepseek-v4-flash-free` (same family — see sys 8ff09360 Learning)
- cli_reviewer: codex

Cells:
- `case_01_sqlite_utils_8f0c06e / default` — baseline (`baselines/default.json`)
  - 20260528-015544 / 020353 / 022111

## sys 8ff09360 — 2026-05-28

**Intent**: prompt edit — SIM reads `/app/.contremaitre/architecture-review.html` at the candidate-pick gate, in addition to the agent's chat summary. Hypothesis: structured HTML context lets the SIM grill more precisely.

**Outcome**: `cli_review_score` median dropped 0.5 → 0.0 (one full grade). The initial regression detection flagged only that one panel. After expanding the scorecard to mine the cli_reviewer's structured output (severity-weighted findings, per-label counts, citation density) AND adding an orchestrator-side continuous composite (`agent_discipline_score`), 4 independent axes were moving in the same bad direction. SIM's `total_required_changes` median collapsed 3 → 0; agent's `exploration_convergence` went mostly_narrowed → thrashed; `loc_net_delta` median grew 25 → 432.

**Learning** (two of them):
1. The LLM judge's verdict key throws away most of its output. Conventional Comments labels already imply severity; mining issue×3 + suggestion×2 + nit×1 distinguishes "1 trivial nit MF" from "4 CI-breaks MF" — both collapse to the same verdict key. Adds 4× more signal at zero LLM cost.
2. Same-family agent ↔ SIM (both deepseek) is the bias scenario: when given more structured context, the SIM became *less* critical, not more — judges trained on similar data are systematically more lenient on sibling-model outputs. Cross-family in the agent↔SIM channel isn't a nice-to-have, it's the architectural target.

- contremaitre @ `453a8ec`
- `sim_tooled_persona.md`: `8507f716` (adds the HTML-read instruction)
- agent / sim: `opencode/deepseek-v4-flash-free`
- cli_reviewer: codex

Cells:
- `case_01_sqlite_utils_8f0c06e / default` — first experiment cell
  - 20260528-133607 / 140811 / 143806

## sys 3dfcbc3a — 2026-05-28

**Intent**: A/A control of `sys 8ff09360` — same prompt, different contremaitre infra commits between cells. Test whether the regression reproduces.

**Outcome**: regression reproduced and intensified — `verdict_mix` collapsed to MUST_FIX×3 (no LG this time). `agent_discipline_score` median was IDENTICAL between the two cells (0.333). Some panels remained stable A/A (continuous composites, exploration_convergence mix), others swung wildly (`loc_net_delta` 432 → 109).

**Learning** (three of them):
1. A/A controls are mandatory before iterating on regression signals at small n. Without the control, the original regression flag was plausibly sampling noise. Total cost: ~45min runtime; net win: high-confidence ground truth that the prompt change really is the cause.
2. At n=3, continuous orchestrator-side composites are dramatically more reproducible than individual metrics. Trust composites for attribution; treat the swingy individual metrics as diagnostic.
3. The right primary regression gate is a continuous composite of process-quality signals (`agent_discipline_score`), not the 3-state LLM-judge median. The LLM judge stays on the headline (it IS the judge) but the score-derived-from-verdict is too coarse to be the gate.

- contremaitre @ `fcb97a5` — also includes `fcbf039` (QUOTA_EXHAUSTED verdict) and intervening fixes; none affect successful runs
- `sim_tooled_persona.md`: `8507f716` (same as sys 8ff09360)
- agent / sim: `opencode/deepseek-v4-flash-free`
- cli_reviewer: codex

Cells:
- `case_01_sqlite_utils_8f0c06e / default` — A/A cell
  - 20260528-154527 / 162505 / 165524

---

### Entry template (copy + fill)

```markdown
## sys <prefix-8> — YYYY-MM-DD

**Intent**: <one sentence — what hypothesis is this system version testing?>

**Outcome**: <what the cells produced; lead with the numbers that moved>

**Learning**: <methodology principle that emerged; skip if no novel learning>

- contremaitre @ `<sha-short>` (commit subject if useful)
- `<prompt_file>.md`: `<hash-short>` (`<what changed>`)
- agent / sim: `<model>` / `<model>`
- cli_reviewer: `<codex|claude|none>`

Cells:
- `<case_id> / <config_name>` — <short description>
  - <ts1> / <ts2> / <ts3>
```

Find the current `system_digest` of a run via `eval show` (header line). Cell-level digests live in `baselines/<config>.json` under `system_digests`.
