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

**Run-signal predicate**
A pure predicate over an actor's raw event stream that detects one of the
harness filesystem contracts: the architecture-review card, the
`SETTLED_DESIGN.md` write, self-verification (a test command between the last
code edit and the final `IMPLEMENTATION_COMPLETE` write), and the
`IMPLEMENTATION_COMPLETE` write itself. Owned by `flow_use.py`
(`marker_writes` / `self_verification`); the **Artifact reader** composes
them post-hoc and the live TUI calls them per tick — one
implementation, so the live chrome and the eval scorecard cannot disagree
about the same run. Observability-only: the orchestrator's handoff gate is a
filesystem check on the worktree, never these predicates. Marker matching is
anchored (exact basename; `apply_patch` requires an `Add/Update File:`
header); runtime decoding (opencode `tool_use` / claude `assistant` blocks;
codex deliberately unhandled — no per-event timestamps) is an internal seam,
not a public one.

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
composes (no file I/O — the edge `run_artifacts → flow_use` stays acyclic). Three
reads stay outside, each for a structural reason, not oversight:

- a *live tail* (`eval._emit_new_events` seeks `guardrail_events.jsonl` during a
  running subprocess) must keep its own handle — a memoized snapshot reader would
  never see new lines;
- `extract` is an **acyclic leaf below the reader** (`run_artifacts → flow_use →
  extract`, since `flow_use` imports `extract.parse_apply_patch`), so it reads
  its own streams rather than closing that import loop;
- a **parse-validity check** (`eval._sim_verdicts_parse_ok`) reads the bytes
  directly because the tolerant reader (`read_jsonl`) silently drops the
  malformed / non-object lines the check exists to fail on.
