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
