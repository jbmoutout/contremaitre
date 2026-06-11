# Context — domain language

Domain glossary for Contremaitre. Architecture vocabulary lives in
`docs/skill_glossary.md` (module / interface / seam / depth / …); this file
names the *domain* concepts those terms describe.

## Terms

**Model identity**
What model a run was driven by, for the agent or the SIM, as a durable fact —
*not* a display string. Atomic parts: the **runtime** that drove it
(`opencode` / `codex` / `claude` / `fake`), the **requested** slug or model
(verbatim from config), the **effort** (CLI roles only), the **resolved**
model the stream reported it actually ran (claude `system/init`; `null` when
the runtime is silent), and the **provider** (`opencode` / `openrouter`) for a
zen/OpenRouter slug. Carried by the `ModelSpec` record in `models.py`.

**ModelSpec**
The single module that owns model identity. Stored fields are atomic and never
re-parsed; the human display string and the grouping key are *derived*
(`display()` / `canonical()`) and never read back as a source of truth. One
factory (`for_role`) builds it from a `RunConfig`; one classmethod
(`from_record`) reads it back — and is the *only* place that tolerates a
legacy on-disk string. Persisted under `agent_model` / `sim_model` in
`stats.json` and `run_config.json`.

**Resolved model**
The model a run *actually* ran, as reported by the runtime's stream
(claude's `system/init` carries it; codex is silent; opencode's requested slug
*is* the resolved model). Distinct from **requested** so two `claude default`
runs that resolved to different models do not collide in grouping.

**Artifact reader**
The single Module that reads a run's **artifact contract** (the JSONL/JSON
streams under `<run-id>/` — `raw_export`, `guardrail_events`, `review_cycles`,
…) off disk. Owns the file I/O and the low-level coercion (timestamps), and
*composes* the pure interpreters (`compute_phases`, `sum_*_in_events`,
`resolved_model_from_events`) over its own memoized streams — it does not
re-home them. Carried by `RunArtifacts` in `run_artifacts.py`. Snapshot
semantics: lazy + per-instance memoization, a fresh instance to re-read (the
live TUI builds one per refresh tick).

**CLI review verdict**
The post-publish CLI reviewer's per-round judgement: one of `LOOKS_GOOD` /
`NEEDS_ATTENTION` / `MUST_FIX`. Carries a canonical **severity** order
(`LOOKS_GOOD < NEEDS_ATTENTION < MUST_FIX`) — the load-bearing fact behind
worst-of-N aggregation, the eval scorecard, and the TUI glyph. Owned by the
`CliReviewVerdict` enum in `models.py`, which parses it from a reviewer body
(**worst-first**: the first key found wins, so a justification that *mentions*
a milder key never outranks the stated verdict), ranks it (`severity`),
aggregates it (`worst`), and exposes whether it blocks (`is_blocking` —
`MUST_FIX`). The parser reads a **header-less** body: stripping any posted-file
metadata header is the reader's job, not the verdict's. Distinct from the SIM
**review verdict** (`ReviewVerdict`: APPROVED / CHANGES_REQUESTED /
NEEDS_HUMAN) and the run's **terminal verdict** (`TerminalVerdict`).
