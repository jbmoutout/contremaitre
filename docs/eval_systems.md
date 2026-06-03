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

**Outcome**: `cli_review_score` median dropped 0.5 → 0.0 (one full grade). The initial regression detection flagged only that one panel. After expanding the scorecard to mine the cli_reviewer's structured output (severity-weighted findings, per-label counts, citation density) AND adding an orchestrator-side continuous composite (`agent_discipline_score`), four independent axes were moving in the same bad direction: `cli_review_score` 0.5 → 0.0, `cli_findings_weighted` 1.5 → 3.0, `cli_citation_density` 2.0 → 1.0, `agent_discipline_score` 0.5 → 0.333. Supporting shifts: SIM's `total_required_changes` median 3 → 0; `exploration_convergence` mixed×3 → thrashed×2 + mixed×1; `loc_net_delta` median 25 → 432.

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

## sys 74744be6 — 2026-05-29

**Intent**: cross-family SIM. Swap SIM from `opencode/deepseek-v4-flash-free` to `openrouter/qwen/qwen3.6-plus` while holding agent + prompt + reviewer constant. Tests two hypotheses: (a) same-family agent ↔ SIM bias was the root cause of the sys 8ff09360 regression, (b) deepseek-v4-flash-free is too weak for the SIM role.

**Outcome** (n=3, no A/A control yet):

Cell median vs deepseek-SIM baseline (same agent, prompt, target):

| Panel | Baseline | Qwen | Δ |
|---|---|---|---|
| `cli_review_score` median | 0.5 | 0.0 | −0.5 |
| `verdict_mix` | LG×1, MF×1, (no-review×1) | MF×2, NA×1 | LG lost |
| `cli_findings_weighted` median | 1.5 | 3 | +1.5 |
| `agent_discipline_score` median | 0.5 | 0.0 | −0.5 |
| `settled_before_code` rate | 1.0 | 0.33 | −0.67 |
| `self_verified` rate | 0.67 | 0.0 | −0.67 |
| `exploration_convergence` | mixed×3 | thrashed×2, mixed×1 | worse |
| `loc_net_delta` median | 25 [−10, 163] | −42 [−358, 193] | wider range |
| `wall_seconds` median | 581 | 1525 | 2.6× |
| `cost_usd` median | 0.0 | $0.30 | paid |
| `review_rounds` median | 1 | 2 | +1 |
| `total_checks_performed` median | 11 | 35 | 3× |
| `total_required_changes` median | 3 | 2 | **−1** |

Per-run breakdown reveals run 01 as an outlier on many axes (26 files, −358 LOC, 3 rounds, 64min wall, $0.76, 252 agent tool calls, 9 required_changes). Runs 02 and 03 were normal-shaped (3 files, smaller diffs, 2 rounds, 24-25min wall). The cell median represents 02/03; the range column hides run 01.

Two observations the data does support unambiguously:
1. The SIM was more *deliberative* per round (3× the check count, +1 round) — but on aggregate asked for *fewer* required_changes per cell (median 2 vs 3). "More work" ≠ "more pushback in change requests."
2. Agent process metrics declined across the board (settled_before_code, self_verified, exploration_convergence) while SIM activity rose. These two trends co-occurred; the data doesn't say one *caused* the other.

**Learning** (grounded — no causal claims without an A/A):
1. **More SIM work doesn't imply better outcomes.** A more capable cross-family SIM that performed 3× more grep/read activity per review did not produce better-judged PRs. Codex still flagged MF×2 + NA×1; no LOOKS_GOOD survived. The intuition "stricter judge → better output" is at best unproven here.
2. **Co-occurrence ≠ causation; a stronger SIM coincided with weaker agent metrics, but several causal stories fit**: (a) agent capacity bound — deepseek-v4-flash-free struggles when conversations get longer/more complex; (b) Qwen's grilling style triggers agent thrashing rather than focus; (c) longer wall time → context degradation; (d) sampling noise across just 3 runs. An A/A is needed before claiming any of these.
3. **Outlier behavior is real.** Run 01 (26 files, −358 LOC, 64min) is qualitatively different from runs 02/03. With n=3 the cell median masks this completely. Headline-only readings are insufficient; the per-run table is load-bearing.
4. **Cost floor**: ~$1 per n=3 cell at this model tier. The free-tier discipline of the deepseek-only configuration is lost with paid models.

**Required next step before drawing conclusions**: A/A control (second n=3 with same config). The cell B A/A of sys 8ff09360 turned out essential for separating signal from sampling; same here. (Already in progress.)

> **Correction (2026-06-03, see sys `063aee1a` Learning 1)**: the `total_required_changes median 2 vs 3` and downstream "more deliberative SIM asks fewer changes" framing should be taken at face value (counts are real), but any attempt to read the broader picture through `sim_useful_call_ratio` is invalid — that metric is structurally 0.0 for every SIM, qwen included. The `total_required_changes`, `total_checks_performed`, `review_rounds` numbers in the table above are NOT affected by the bug.

- contremaitre @ `a1c4a96`
- `sim_tooled_persona.md`: original (post-revert, same as sys 27666e87)
- agent: `opencode/deepseek-v4-flash-free`
- sim: **`openrouter/qwen/qwen3.6-plus`**
- cli_reviewer: codex

Per-run snapshot:

| Run | terminal | cli_review | files | loc_net | rounds | wall_s | cost | req_changes | settled | self_verified | exploration |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 154612-01 | READY | MF | **26** | **−358** | **3** | **3860** | **$0.76** | **9** | ✓ | ✗ | thrashed |
| 170214-02 | READY | MF | 3 | +193 | 2 | 1467 | $0.27 | 2 | ✗ | ✗ | thrashed |
| 172644-03 | READY | NA | 3 | −42 | 2 | 1525 | $0.30 | 2 | ✗ | ✗ | mixed |

Cells:
- `case_01_sqlite_utils_8f0c06e / qwen_sim` — first experimental cell (no baseline; cross-config comparison vs `default` baseline is informal)
  - 20260529-154612 / 170214 / 172644

## sys 0b1fb838 — 2026-05-29

**Intent**: agent-capacity test. Hold SIM at the cheap same-family `opencode/deepseek-v4-flash-free`, swap agent to `openrouter/anthropic/claude-sonnet-4.6` (strong, cross-family). If outcomes recover with strong-agent + weak-SIM, agent capacity was the limit (the hypothesis surfaced by sys 74744be6).

**Outcome** (n=3 but only n=1 of quality data):

- Run 01: `FAILED_INFRA` — SIM stalled mid-review, sqlite recovery found nothing. $3.26.
- Run 02: `READY_FOR_DRAFT_PR`, codex verdict **NEEDS_ATTENTION** with **0 issues, 0 suggestions, 1 nit**. 3 files / +21 LOC. SIM approved on round 1 with **0 required_changes**. `settled_before_code=True`, `self_verified=True`, exploration `mixed`. $3.03.
- Run 03: `FAILED_INFRA` — OpenRouter daily credit cap hit mid-run (32k tokens requested, 28k affordable). $1.24.

Total cost ~$7.50; OpenRouter daily limit nearly exhausted.

Cell unpromotable (`all_runs_ok=False`, `dirty=True`).

**Learning** (short — n=1 is suggestive, not conclusive):
1. The single quality run is the cleanest result on this case to date — focused 21-LOC diff, SIM had nothing to fix, codex flagged only one nit. **Consistent with** the agent-bound hypothesis from sys 74744be6, but not a proof at n=1.
2. Paid configs (Sonnet via OpenRouter) hit operational ceilings that free configs don't — daily credit cap, longer wall times → more SIM stalls during slow rounds. The eval workflow needs budget headroom and a clean tree before claiming results.
3. **Required next step**: re-run `sonnet_agent` n=3 with budget headroom and a clean tree; ideally A/A.

- contremaitre @ ~`a1c4a96` (tree dirty during these runs)
- `sim_tooled_persona.md`: original (post-revert)
- agent: **`openrouter/anthropic/claude-sonnet-4.6`**
- sim: `opencode/deepseek-v4-flash-free`
- cli_reviewer: codex

Cells:
- `case_01_sqlite_utils_8f0c06e / sonnet_agent`
  - 20260529-202058 (FAILED_INFRA — SIM stall) / 203532 (READY, NA) / 205250 (FAILED_INFRA — OpenRouter cap)

## sys d0c4c42c — 2026-05-29 / 2026-06-02

**Intent**: extra reviewer test. Default + `extra_reviewer_model = opencode/big-pickle` (cross-family, opencode-zen free-tier). Tests whether a second cross-family review pass in the agent↔SIM channel improves outcomes.

**Outcome** (canonical n=3 from 2026-06-02; earlier 2026-05-29 attempts had infra failures):

| Run (2026-06-02) | Terminal | cli_review | findings (weighted/issues) | rounds | files | LOC | required_changes | cross_fam |
|---|---|---|---|---|---|---|---|---|
| -01 | READY | MF | 12 / 4 | 3 | 1 | +14 | 4 | True |
| -02 | READY | MF | 3 / 1 | 1 | 2 | +1 | 0 | True |
| -03 | READY | MF | 3 / 1 | 1 | 2 | −3 | 0 | True |

Cell: **MF×3** (no LG, no NA), `cli_review_score` median 0.0, `loc_net_delta` median **1** (essentially no-op diffs), `cross_family_agreement_rate` **1.00**, `total_required_changes` median **0** (both SIMs approved most rounds with no asks).

Vs baseline (default, no extra): LG×1+MF×1+NO_PR×1 → MF×3; loc_net_delta median 25 → **1**; required_changes median 3 → **0**; published rate 2/3 → 3/3.

**Learning** (n=3 clean, two findings, both surprising):
1. **`cross_family_agreement = 1.0` does not validate correctness.** Both SIMs approved every diff and AGREED every time; codex MF'd every PR. Two same-tier same-harness judges share failure modes — they both miss things codex catches but agree they aren't missing anything. The agreement metric measures consistency between similarly-bounded judges, not independent corroboration. Useful information about the *metric*, not just the experiment.
2. **The extra reviewer shifted agent behavior, not output quality.** Diffs collapsed from baseline median +25 LOC to **+1 LOC** (run 03 net-deleted 3 lines, run 02 net-added 1). All three published; all three got MF. Mirror of sys 74744be6 (qwen_sim → scope explosion); here, scope **collapse**. The agent appears to be optimizing for "what two SIMs will approve" rather than "what the codebase needs."

**Cumulative across 4 configs on this case**: every SIM-channel modification (qwen_sim, extra_big_pickle, both) has produced WORSE codex outcomes than the same-family default. The lone clean Sonnet-agent run (sys 0b1fb838) is the only result that improved cli_review_score — by *changing the agent*, not the SIM. **The leverage isn't in the SIM channel.**

- contremaitre @ ~`a1c4a96` (tree dirty)
- `sim_tooled_persona.md`: original
- agent: `opencode/deepseek-v4-flash-free`
- sim: `opencode/deepseek-v4-flash-free`
- **extra_reviewer: `opencode/big-pickle`**
- cli_reviewer: codex

Cells:
- `case_01_sqlite_utils_8f0c06e / extra_big_pickle`
  - **2026-06-02 clean cell (canonical)**: 20260602-203011 / 210302 / 211332
  - 2026-05-29 incomplete: 20260529-211108 (both reviewers, AGREED) / 215353 (extra timed out → SIM-only) / 223245 (QUOTA_EXHAUSTED, 12s)

## sys (autopsy) — 2026-06-02 — nemotron-as-agent fails yield discipline

**Status**: 2 runs exist on disk under config slug `nemotron_minimax` but the config at the time of execution was same-family (agent=sim=`opencode/nemotron-3-super-free`). The config has since been corrected to cross-family (agent=nemotron, sim=`opencode/minimax-m3-free`); these 2 runs do NOT describe that pair. Both runs are FAILED_INFRA and contribute no SIM-channel data — autopsy below addresses the actual finding, which is about the agent.

**Outcome**:

| Run | Terminal | Turns | Wall | SIM rounds | codex |
|---|---|---|---|---|---|
| 20260602-223503-...-01 | FAILED_INFRA (1800s wall) | 1 | 1809s | 0 | not invoked |
| 20260602-230514-...-02 | FAILED_INFRA (killed at +3min) | 1 | n/a | 0 | not invoked |

Run 01 made 57 tool calls inside a single `agent_turn()` that never returned. Run 02 was manually killed shortly after start. The SIM and cli_reviewer were never invoked in either run.

For contrast, deepseek-as-agent on the same case ([default-01](.contremaitre/runs/20260528-015544-eval-case_01_sqlite_utils_8f0c06e-default-01)) yielded after 87s for the first SIM turn, then alternated agent↔SIM for 7 turns before writing IMPLEMENTATION_COMPLETE — total wall 485s.

**Learning** (two of them):

1. **The WORK loop assumes the agent yields its turn between Explore and Grill.** [orchestrator.py:347-376](../contremaitre/orchestrator.py#L347-L376) drives `agent_turn → sim_turn → agent_turn → ...` and can only invoke the SIM after `actor.agent_turn(message)` returns. The `/improve-codebase-architecture` skill is structured to yield naturally — Step 2 says "After the file is written, ask the user: 'Which of these would you like to explore?'" Deepseek follows that yield point; Nemotron treats the skill as a single end-to-end task and proceeds straight through to implementation without ever asking the user (= SIM). No model-capacity bump fixes this — it's a structural mismatch with the control plane.

2. **"Agent timed out" can mean two very different things.** sys 0b1fb838's Sonnet FAILED_INFRA was a *SIM stall* mid-review at round 3; the agent had yielded several times by then. This autopsy's nemotron FAILED_INFRA was a *single never-yielding agent turn*. Both surface as `FAILED_INFRA` + wall-clock timeout, but they imply different fixes (SIM retry policy vs. agent yield discipline). The `reason` string in `stats.json` (`"agent opencode timed out after 1800s"` vs. `"SIM stalled..."`) is the disambiguator; `terminal_verdict` alone isn't enough.

**Implications for the cross-family nemotron+minimax cell**: with this agent, the minimax SIM would never get a chance to speak. Either swap roles (minimax as agent, nemotron as SIM) or drop nemotron from the agent slot for this case.

- contremaitre @ `a1c4a96` (tree dirty during these runs)
- `sim_tooled_persona.md`: original
- agent / sim: `opencode/nemotron-3-super-free` / `opencode/nemotron-3-super-free` (config later corrected to nemotron + minimax-m3-free; these runs predate that fix)
- cli_reviewer: codex (not invoked)

Cells:
- `case_01_sqlite_utils_8f0c06e / nemotron_minimax` — DO NOT AGGREGATE as nemotron_minimax cell
  - 20260602-223503 / 230514 (preserved; flag as `nemotron_same_family` if ever promoted to a real cell)

## sys 347f21a0 — 2026-06-02 / 2026-06-03

**Intent**: cross-family SIM swap with the proven-to-yield agent. After the autopsy above ruled out nemotron-as-agent on yield-discipline grounds, the cross-family SIM hypothesis (EVAL_ROADMAP §5) gets re-tested here as `nemotron_sim`: deepseek agent + nemotron SIM + codex reviewer, free-tier. Parallel to qwen_sim, different SIM family.

**Outcome** (n=3):

| Run | Terminal | wall_s | turns | rounds | files | LOC net | codex | failure / hiccup |
|---|---|---|---|---|---|---|---|---|
| [-01](../.contremaitre/runs/20260602-235843-eval-case_01_sqlite_utils_8f0c06e-nemotron_sim-01) | FAILED_INFRA | 1668 | 6 | 0 | 2 | +120 | not invoked | nemotron-SIM stalled fatally: `"review opencode emitted no text and sqlite recovery found nothing"` |
| [-02](../.contremaitre/runs/20260603-002636-eval-case_01_sqlite_utils_8f0c06e-nemotron_sim-02) | READY_FOR_DRAFT_PR | 1251 | 6 | 1 | 1 | −96 | **LOOKS_GOOD** | clean |
| [-03](../.contremaitre/runs/20260603-004729-eval-case_01_sqlite_utils_8f0c06e-nemotron_sim-03) | READY_FOR_DRAFT_PR | 2149 | 13 | 3 | 1 | +52 | **MUST_FIX** (black not clean) | codex 400 on first attempt → operator re-ran codex (`.rerun` artifacts); also 5 sqlite-DB recoveries during the run (3 SIM-side + 2 review-side) |

Cell mix (after the eval-rerun parser fix in commit `08a44f5`): READY+LG×1, READY+MF×1, FAILED_INFRA×1 — verdict shape identical to the default baseline (sys 27666e87: LG×1 + MF×1 + NO_PR×1).

Diagnostic: `sim_useful_call_ratio = 0.0` in BOTH successful runs despite the nemotron-SIM making 19 (run 02) and 33 (run 03) tool calls. Run 02 approved a −96 LOC refactor on round 1 with 0 required_changes; run 03 went 3 rounds and asked for 2 required_changes total. `process_reliability = 1.0`, `sim_review_confidence = 0.95` in both.

> **Correction (2026-06-03, see sys `063aee1a` Learning 1)**: the `sim_useful_call_ratio=0.0` reading does NOT support a rubber-stamp interpretation. That metric returns 0.0 for every SIM ever run on case_01 (deepseek, qwen, nemotron) because [flow_use.py:570-576](../contremaitre/flow_use.py#L570-L576) requires a verbatim ≥20-char grep-output line in the verdict text, while SIMs paraphrase. Strike the "vacuous review" framing from Learning 2 below — the metric was reading noise. Learning 1 (text-emit instability) and Learning 3 (FAILED_INFRA source taxonomy) stand.

**Learning** (three of them):

1. **Nemotron's "doesn't reliably emit text" trait is symmetric across roles.** The autopsy above established it as a yield failure when nemotron is the agent. As the SIM it surfaces differently: run 01 was a fatal silent stall the sqlite-recovery couldn't salvage, run 03 needed 5 in-run sqlite-recoveries to extract text from opencode's internal session storage. Free-tier infra reliability is the binding constraint, not capability. Two of three runs survived only because the sqlite-recovery path exists.

2. **Cross-family swap didn't degrade outcomes vs baseline.** ~~`sim_useful_call_ratio=0.0` in both completed runs.~~ The cell looks shape-identical to baseline (LG×1 + MF×1 + 1 unreviewable). Whether that's "non-biased cross-family review" or sampling noise at n=2 is open; an A/A would help. (The original "rubber-stamp risk" caveat is retracted — see Correction above.)

3. **`FAILED_INFRA` covers three distinct failure sources on this cell alone** — yield discipline (autopsy above), SIM stall (run 01 here), codex 400 (run 03 here, recovered via operator rerun). Same terminal verdict, different fixes. `stats.json.reason` discriminates; the headline does not.

**Eval-side fix in flight**: commit `08a44f5` makes `_parse_cli_review` prefer `<tool>_review_raw_export.rerun.jsonl` over the failed-first-attempt plain file. Without that, run 03's cli-review panel was silently NULL despite a clean MUST_FIX on disk.

- contremaitre @ `a89f81e` (clean tree per run_config)
- `sim_tooled_persona.md`: original
- agent: `opencode/deepseek-v4-flash-free`
- sim: **`opencode/nemotron-3-super-free`** (cross-family)
- cli_reviewer: codex
- `system_digest`: `347f21a0…`

Cells:
- `case_01_sqlite_utils_8f0c06e / nemotron_sim` — first cell
  - 20260602-235843 (FAILED_INFRA — SIM stall) / 20260603-002636 (READY, LG) / 20260603-004729 (READY, MF; codex rerun required)

## sys 063aee1a — 2026-06-03 — actor-side CI formatter/lint gate

**Intent**: patch the actor prompt (`initial_prompt.md`) to discover and run the project's CI formatter/lint gates against changed files before writing `IMPLEMENTATION_COMPLETE`. Hypothesis grounded in a retrospective audit of every case_01 MUST_FIX verdict to date: a non-trivial share are *purely* mechanical (Black-not-clean, flake8 F401/E402) rather than judgement-level. Discovery-driven prompt (no hardcoded tool names) keeps the change project-agnostic — generalizes to any repo whose CI runs a formatter/linter.

**Pre-fix audit** (16 case_01 MUST_FIX runs across sys 27666e87, 8ff09360, 3dfcbc3a, d0c4c42c — classified by the *kind* of blocker called out in `codex_review.md`):

| Class | Count | Runs |
|---|---|---|
| Pure formatter/lint — verdict expected to flip MF → LG | 4 | [154527-default-01](../.contremaitre/runs/20260528-154527-eval-case_01_sqlite_utils_8f0c06e-default-01) (flake8 F401), [210302-extra_big_pickle-02](../.contremaitre/runs/20260602-210302-eval-case_01_sqlite_utils_8f0c06e-extra_big_pickle-02) (flake8 E402+F401), [211332-extra_big_pickle-03](../.contremaitre/runs/20260602-211332-eval-case_01_sqlite_utils_8f0c06e-extra_big_pickle-03) (black), [004729-nemotron_sim-03 rerun](../.contremaitre/runs/20260603-004729-eval-case_01_sqlite_utils_8f0c06e-nemotron_sim-03) (black) |
| Mixed — lint noise on top of a semantic bug, verdict stays MF but findings shrink | 3 | [133607-default-01](../.contremaitre/runs/20260528-133607-eval-case_01_sqlite_utils_8f0c06e-default-01) (F401 + FutureWarning leak), [162505-default-02](../.contremaitre/runs/20260528-162505-eval-case_01_sqlite_utils_8f0c06e-default-02) (F401 + CSV dialect drop), [165524-default-03](../.contremaitre/runs/20260528-165524-eval-case_01_sqlite_utils_8f0c06e-default-03) (F401 + plugins never loaded) |
| Pure semantic / API / contract — verdict stays MF (reviewer doing real work) | 9 | 20260528-001559, 022111-default-03, 140811-default-02, 154612-qwen_sim-01, 170214-qwen_sim-02, 181555-qwen_sim-01, 211108-extra_big_pickle-01, 215353-extra_big_pickle-02, 203011-extra_big_pickle-01 |

Headline split: **4/16 should flip, 3/16 should get cleaner, 9/16 should stay (and should).**

**Outcome** (n=3 on `nemotron_sim`):

| Run | Terminal | wall_s | turns | rounds | files | LOC net | codex | findings (weighted, issues) | failure |
|---|---|---|---|---|---|---|---|---|---|
| [-01](../.contremaitre/runs/20260603-044948-eval-case_01_sqlite_utils_8f0c06e-nemotron_sim-01) | READY | 1721 | 8 | 1 | 3 | +98 | **NEEDS_ATTENTION** | 4w / **0 issues** / 2 suggestions | clean |
| [-02](../.contremaitre/runs/20260603-051832-eval-case_01_sqlite_utils_8f0c06e-nemotron_sim-02) | FAILED_INFRA | 1067 | 2 | 0 | n/a | n/a | not invoked | n/a | nemotron-SIM 900s timeout |
| [-03](../.contremaitre/runs/20260603-053622-eval-case_01_sqlite_utils_8f0c06e-nemotron_sim-03) | READY | 1631 | 8 | 1 | 4 | −6 | **LOOKS_GOOD** | 0 | clean |

Vs the prior `nemotron_sim` cell (sys 347f21a0) — same agent + SIM + reviewer, only `initial_prompt.md` changed (`527caa18` → `a0be7117`):

| Panel | Prior (347f21a0) | New (063aee1a) | Δ |
|---|---|---|---|
| terminal mix | FAILED×1 + READY×2 | FAILED×1 + READY×2 | same |
| cli_review verdict mix | LG×1 + MF×1 | LG×1 + **NA×1** | **MF → NA** |
| `cli_issue_count` total across cell | 1 | **0** | dropped to zero |
| `total_required_changes` across cell | 0 + 2 | 0 + 0 | tighter |
| `review_rounds` per successful run | 1, 3 | 1, 1 | faster convergence |
| `agent_discipline_score` per successful run | 0.5, 0.0 | 0.5, 0.5 | stable (per-run, not median) |

The MF→NA shift matches the pre-fix audit prediction for the formatter-pure category: run 03 of the prior cell was a `black`-only MF (one of the 4 "expected to flip"). After the gate, no `issue` labels remain; only `suggestion`/`nit`.

**Learning** (three of them):

1. **A scorecard metric was reading noise as signal across the whole journal.** The spot-check that prompted this entry confirmed `sim_useful_call_ratio = 0.0` in **every** SIM run on case_01 to date — 4 deepseek-SIM, 3 qwen-SIM, 6 nemotron-SIM. The matcher in [flow_use.py:570-576](../contremaitre/flow_use.py#L570-L576) requires a verbatim ≥20-char grep-output line inside the SIM's `summary` + `checks_performed` text, but SIMs are prompted to write *paraphrased* bullet descriptions (e.g. `"grep _compile_code sqlite_utils/utils.py (no output, confirming removal)"`), not paste raw output. The metric is structurally 0.0; the `agent_discipline_score` composite has been treating that as a load-bearing third (alongside `settled_before_code` and `self_verified`). Corrections were added to sys 347f21a0 and 74744be6 — no past claim in this journal that hung on this metric is safe.

2. **The CI-gate prompt edit landed its predicted MF→NA shift, but the signal is one axis, not three.** `cli_review_score`, `cli_issue_count`, and "hard-gates pass rate" all moved favorably in the same direction. That looks like three confirmations; it isn't. The intervention removes the formatter-MF class from codex's input population; all three panels are downstream of that same effect. Reporting them as independent corroborating axes is double-counting. The DUMP.md correlation-audit framing (independent metric families) becomes load-bearing for any future regression-confirmation claim.

3. **Nemotron-SIM ~33% infra-failure rate holds across cells.** Cell A (sys 347f21a0): 1/3 fatal SIM stall. Cell B (here): 1/3 SIM 900s timeout. Same root cause (nemotron's text-emit instability), different surfacing. With deepseek-as-agent + codex-as-reviewer held constant, the nemotron-SIM is the one reliability cost in this configuration — independent of any review-quality measurement (which the corrected metric tells us nothing about anyway).

**Observation worth a follow-up, not yet a Learning**: in cell B run -03, the nemotron-SIM listed 9 `grep …` entries in `checks_performed` but actually executed only 2 grep calls — aspirational verdict prose. Run -01 was 15-listed / 15-executed (clean). A "listed_checks vs executed_tool_calls" metric would catch this; `sim_useful_call_ratio` was never going to. Worth coding before the next nemotron cell.

- contremaitre @ `382ef87`
- `initial_prompt.md`: `a0be7117` (adds discover+run CI formatter/lint gates pre-`IMPLEMENTATION_COMPLETE`, check-only or scoped to changed files, install dev tooling if missing)
- agent: `opencode/deepseek-v4-flash-free`
- sim: `opencode/nemotron-3-super-free`
- cli_reviewer: codex
- `system_digest`: `063aee1a…`

Cells:
- `case_01_sqlite_utils_8f0c06e / nemotron_sim` — first cell on the patched prompt
  - 20260603-044948 (READY, NA) / 20260603-051832 (FAILED_INFRA — SIM timeout) / 20260603-053622 (READY, LG)

Pending: re-run on `case_01 / extra_big_pickle` (targets the 2/3 MFs that were flake8+Black) to test the gate's effect on a more lint-heavy MF population.

## sys (metrology) — 2026-06-03 — fix `sim_useful_call_ratio` + demote correlated headline metrics

**Intent**: act on sys `063aee1a` Learning 1 (the `sim_useful_call_ratio` matcher was structurally zero-pinned across every SIM ever run on case_01) and Learning 2 (correlated headline metrics were being treated as independent corroboration). Both are metrology fixes — no new agent runs; 22 existing `ok=true` canaries on case_01 regenerated in place.

**Outcome**:

PR 1 (commit `c494c2c`) — `_grep_cited_in` → `_grep_args_cited_in` in [flow_use.py:570](../contremaitre/flow_use.py#L570). New matcher tests pattern + path/include/glob args against verdict prose; falls back to the longest literal fragment between regex metachars (so `_compile_\w+` credits a verdict saying `_compile_`). Validated against 22 `ok=true` runs after regenerating `flow_use.json` + `canary.json`:

| metric | before | after |
|---|---|---|
| `sim_useful_call_ratio` | 0.0 in 22/22 runs (1 distinct value) | 0.17–1.00, 19 distinct, mean 0.66 |
| `agent_discipline_score` | 4 distinct values (0.0–0.5) | 19 distinct values (0.121–0.833) |
| `terminal_score` / `hard_gates_passed` / `process_reliability` | constant 1.0 (survivor bias from `ok=true` filter) | binary — `default-01` correctly has 0 |

`agent_discipline_score` envelope check temporarily disabled at [eval.py:1271](../contremaitre/eval.py#L1271) — composite semantics changed; pre-fix baselines aren't comparable; restorable in one line once a baseline is regenerated.

PR 2 (commit `ad574c2`) — `cli_findings_weighted`, `review_rounds`, `cli_citation_density` removed from `_DRIFT_ENVELOPES` in [eval.py:58](../contremaitre/eval.py#L58). All three still computed and recorded; just no longer raise REGRESSIONS / drifts.

`eval show case_01_sqlite_utils_8f0c06e --config default --n 3` against the current (pre-fix) baseline:

| | before PR 2 | after PR 2 |
|---|---|---|
| REGRESSIONS | 3 (cli_review_score 0.5→0, cli_findings_weighted 1.5→6, cli_citation_density 2→1) | 1 (cli_review_score 0.5→0) |
| drifts | 2 (loc_net_delta, wall_seconds) | 2 (same) |
| improvements | 1 (agent_discipline_score 0.50→0.62) | 1 (same) |

The remaining cli_review_score 0.5 → 0.0 IS a real cli_reviewer drift between the baseline trio (20260528-0xxxx) and the latest trio (20260528-15xxxx, the sys `3dfcbc3a` A/A cell). Not papered over — `default` baseline was NOT promoted.

**Learning** (five, ordered by future-blast-radius):

1. **`flow_use.json` is read stale by `check_run`.** Changing a flow_use metric definition requires a two-step regeneration: (a) re-run `compute_flow_use` over each run dir to rewrite `flow_use.json`, then (b) `eval check` to refresh `canary.json`. `evaluator.write_json(paths.flow_use_report, ...)` only fires on live runs; [eval.py:845](../contremaitre/eval.py#L845) `check_run` reads the file from disk and does not recompute. The first pass of `eval check` after PR 1 silently kept zero values until the explicit `compute_flow_use` regeneration. Either `check_run` should recompute, or there should be an explicit `eval reflow <run_dir>` command — otherwise the next flow_use heuristic change repeats this trap.

2. **The `ok=true` filter was hiding real signal via survivor bias.** Three metrics that A4's first-pass analysis flagged "zero-variance, drop from the gate" — `terminal_score`, `hard_gates_passed`, `process_reliability` — all gained binary variance after regeneration because [default-01](../.contremaitre/runs/20260528-015544-eval-case_01_sqlite_utils_8f0c06e-default-01) flipped `ok=false` → `ok=true`. That run has `terminal_score=0`, `NO_PR_NEEDS_HUMAN` (agent gave up after 1 SIM round, 3 required_changes unaddressed, hard-gates fail). It's the load-bearing data point for task-survival measurement. **Audit lesson: any "drop this metric, it has no variance" recommendation must be re-checked against the *unfiltered* run set before being committed to gate logic.**

3. **The `ok` flag conflates two failure modes.** Of the 6 current `ok=false` runs: 4 are FAILED_INFRA (no analyzable artifacts), 1 is QUOTA_EXHAUSTED (no analyzable artifacts), and [extra_big_pickle-02](../.contremaitre/runs/20260529-215353-eval-case_01_sqlite_utils_8f0c06e-extra_big_pickle-02) is `contremaitre_git_dirty: true` with FULL artifacts populated (READY_FOR_DRAFT_PR, cli_review_score=0/MUST_FIX, `sim_useful_call_ratio=0.818`). That last case is a usable data point being thrown away by the `ok` filter. Splitting `ok` into `ok_infra` (data present?) and `ok_hermetic` (analysis valid for cli comparisons?) reclaims it — small refactor; natural fit for DUMP.md A5.

4. **`contremaitre_git_dirty` is reassessed at `eval check` time, not snapshot at run start.** That's *why* default-01 flipped on regeneration: it originally ran against a dirty contremaitre tree, but `eval check` re-records the dirty flag from the CURRENT working tree, which is clean. The flag is "is contremaitre dirty NOW?" not "was it dirty WHEN this run ran?" — a separate provenance bug; natural fit for DUMP.md A7.

5. **The sys `063aee1a` Learning 2 framing held under load.** "Three correlated panels moving = one signal, not three confirmations" was correct: removing two correlated metrics from the headline gate collapsed 3 REGRESSIONS to 1 on a real cli_reviewer drift event, without losing the underlying signal. Future regression-confirmation claims must be at the family level (per the A4 correlation audit), not the raw-metric level.

**Cells**: none new (no agent runs performed). 22 existing `ok=true` canary.json files across [case_01_sqlite_utils_8f0c06e](../golden_cases/case_01_sqlite_utils_8f0c06e) regenerated in place. `default` baseline NOT promoted — the surfaced cli_review_score drift is real signal worth investigating, not absorbing into a fresh baseline.

- contremaitre @ `ad574c2`
- `flow_use.py`: `c494c2c` (matcher rewrite + 4 new unit tests); `eval.py`: `ad574c2` (headline demotions)
- prompts / agent / sim / cli_reviewer: unchanged
- A4 correlation report: `/tmp/contremaitre_metric_independence_report.md`

Pending: investigate the cli_review_score 0.5 → 0.0 drift between sys `27666e87` and sys `3dfcbc3a` default cells (A/A control claims the prompt change caused it; the drift now reads as one panel, not three) before considering any baseline promotion.

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
