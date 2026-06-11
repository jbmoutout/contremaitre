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
`worktree_state`, …) off disk. Owns the file I/O and the low-level coercion
(timestamps), and *composes* the pure interpreters (`compute_phases`,
`compute_flow_use`, `sum_*_in_events`, `resolved_model_from_events`) over its
own memoized streams — it does not re-home them. Carried by `RunArtifacts` in
`run_artifacts.py`. Snapshot semantics: lazy + per-instance memoization, a fresh
instance to re-read (the live TUI builds one per refresh tick).

It is the sole reader among the **upper layer** — the live TUI, eval scorecard,
viewer index, and PR-body builder all read through `RunArtifacts`, where the
reach-arounds used to be. `flow_use` is a **pure interpreter** the reader
composes (no file I/O — the edge `run_artifacts → flow_use` stays acyclic). Two
reads stay outside, both for structural reasons, not oversight:

- a *live tail* (`eval._emit_new_events` seeks `guardrail_events.jsonl` during a
  running subprocess) must keep its own handle — a memoized snapshot reader would
  never see new lines;
- `extract` is an **acyclic leaf below the reader** (`run_artifacts → flow_use →
  extract`, since `flow_use` imports `extract.parse_apply_patch`), so it reads
  its own streams rather than closing that import loop.
