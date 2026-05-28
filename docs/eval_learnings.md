# Eval Methodology Learnings

Generalizable principles from building and running regression canaries on coding-agent orchestration. Distinct from [`LEARNINGS.md`](../LEARNINGS.md) (gitignored, per-run facts) and [`EVAL_ROADMAP.md`](../EVAL_ROADMAP.md) (project plan).

Project-specific implementation notes belong in code comments, commit messages, and the per-run notepad — not here.

---

## L1 — The n=3 floor is metric-dependent, not universal

n=3 per cell is the floor for continuous metrics (wall time, LOC delta, tokens, ratios) — distributions stabilize enough to read medians. It is **below** the floor for discrete few-state outcomes. A 3-state LLM verdict mapped to 1.0/0.5/0.0 has ~5 possible cell medians; a single bad sample dominates and ranges routinely span the full scale.

If a discrete-outcome metric is the headline regression gate, plan for n≥10 or accept that only catastrophic shifts are detectable. Better: build the headline around continuous metrics, treat discrete-outcome ones as distribution shapes (`verdict_mix`), not numeric scores.

## L2 — The post-publication reviewer IS your LLM judge; mine its full output

If your pipeline already has a model reviewing the produced artifact (a code reviewer, a graded rubric, anything emitting structured findings), that IS your LLM-as-judge by any standard definition. Don't reach for a "separate eval judge" prematurely — you'd be re-implementing what's already wired in and re-calibrating cross-family bias mitigation from scratch.

But: the verdict key is the *least informative* slice of the judge's output. A single 3-state verdict hides whether a "fail" is one trivial nit or four CI-breaking issues. Mine the rest: severity-weighted finding counts (label tags already imply severity), citation density (grounded vs hand-waved findings), per-class distributions. Each adds a distinct independent axis. A run that scores identically on the verdict key can vary 4× on weighted-findings — that's the actual signal.

## L3 — Validate every regression with an A/A control before iterating

n=3 is small enough that a single regression signal is plausibly sampling noise. Before drawing conclusions or shipping a "fix," run a second n=3 with the *same* config and compare cell-to-cell. If the metrics reproduce, the signal is real. If they bounce back, it was noise — and any iteration would have been chasing ghosts.

A useful side effect: A/A reveals which panels are stable and which are noisy at your chosen n. The stable panels are the ones to anchor attribution on; the noisy ones are diagnostic only. In practice some continuous composites (process-quality scores aggregating multiple signals) are remarkably stable A/A, while individual continuous metrics (LOC delta, wall time) can swing meaningfully — trust the composites.

## L4 — Cross-family in the judging chain is architectural, not stylistic

Running the agent and any judge/SIM on the same model family creates same-family bias: judges trained on similar data are systematically more lenient on outputs from sibling models (Future AGI cites 10–25% inflation). Cross-family in the agent↔SIM channel — and ideally across agent, SIM, and reviewer — isn't a nice-to-have; it's the architectural target.

When a single-family pipeline hits a regression, you cannot distinguish "weak judge" from "biased judge" from "agent regression" with the same-family configuration. Swapping in a different-family model in the SIM/judge role disambiguates: if grilling intensity and finding counts shift, family was the issue; if they don't, the agent has its own failure mode.

## L5 — Case identity = the full experimental tuple; swaps make new cases

A "case" is the complete pinned input: `(target_repo, base_sha, agent_model, sim_model, judge_model, reviewer_prompt_version, ...)`. Anything experimental that differs makes a new case, not an in-place edit of an existing one.

In-place edits destroy the trail: the baseline tied to the old configuration gets overwritten by the new one, you lose the ability to compare across configurations, and the `git log` of `case.toml` becomes the only record (and a confusing one). Sibling case directories with shared `case_id` prefixes keep the lineage explicit: `case_01_<target>`, `case_02_<target>_<flavor>` (where `<flavor>` describes what differs). Each has its own baseline; runs are slug-namespaced so trails never mix.

Cross-case comparison is then a deliberate operator action ("compare baseline of case_01 vs case_02"), not an implicit consequence of editing config. Two cells with different system identities should *not* aggregate together silently.

---

*Append new learnings as we accumulate them. Each entry: principle + why + how to apply. Keep them generalizable — if it could only apply to one project's implementation, it belongs in code comments or commit history.*
