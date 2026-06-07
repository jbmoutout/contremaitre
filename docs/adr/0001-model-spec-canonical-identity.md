---
status: proposed
date: 2026-06-07
---

# Model identity is a stored structured record, not a display string

## Context

A run's model identity — for the agent, the SIM, and the extra reviewer — has
to survive four very different surfaces: the `default.toml`/`defaults.toml`
operator config, the live TUI, the orchestrator's internal logic, and the
post-hoc `viewer/`. Today identity is smuggled inside a **display string** and
re-parsed downstream, which has produced five distinct, compounding defects.

The string is built by `role_model_label()` in
[models.py](../../contremaitre/models.py) (lines 70-95):

```
"gpt-5.5 (codex, effort=high)"          # codex CLI role
"claude default (claude, effort=high)"  # claude CLI role, account default
"openrouter/deepseek/deepseek-v4-flash" # opencode/OpenRouter role
```

**1. Effort lives in prose, decoded by three separate regexes.** The
`(codex|claude, effort=…)` suffix is a parse contract masquerading as a label.
It is re-decoded independently in
[tui.py:137](../../contremaitre/tui.py) (`effort=(\w+)`),
[viewer/index.py:298](../../contremaitre/viewer/index.py) (`_CLI_LABEL_RE`),
and [viewer/index.py:301](../../contremaitre/viewer/index.py)
(`_canonical_model`). A spacing or wording change silently breaks all three at
once, with no type error to catch it.

**2. The same field has two incompatible formats in two files.**
`run_config.json["agent_model"]` is the **raw RunConfig slug** written by
[manifest.py:145](../../contremaitre/manifest.py) — e.g.
`openrouter/deepseek/deepseek-v4-flash`, which for a CLI run is the slug
codex/claude *ignore*. `stats.json["agent_model"]` is the **label string**
written by [orchestrator.py:1183](../../contremaitre/orchestrator.py) —
`gpt-5.5 (codex, effort=high)`. `tui._read_run_models` prefers run_config and
falls back to stats, so it gets a *different format depending on which file
happens to exist*.

**3. The model that actually ran is never persisted for CLI roles.** Claude
reports its resolved model in the `system/init` stream event (verified:
`claude-sonnet-4-6` even when `claude_model=""`), but that value is used only
for live TUI relabel ([tui.py:2497-2500](../../contremaitre/tui.py)) and then
discarded. Codex's stream carries no model field at all (verified: a codex
run's events are `thread.started`/`turn.*`/`item.*` with no `model`, `effort`,
or `provider` key anywhere). So after the fact a `"claude default"` run is
indistinguishable between opus and sonnet — they collide in every grouping.

**4. The canary's comparability key is corrupted by #2 and #3.**
`manifest_digest()` ([manifest.py:165-199](../../contremaitre/manifest.py))
hashes `agent_model`/`sim_model` read from the manifest — i.e. the raw opencode
slug. For CLI runs that is the *ignored* slug, and effort is not in the digest
at all. Consequence: codex-`high` vs codex-`xhigh`, and claude-opus vs
claude-sonnet-default, hash to the **same** digest. The two-variable guard that
is supposed to catch "you changed both prompt and model in one cycle" is blind
to every CLI model swap and every effort change.

**5. Five overlapping normalizers that disagree.** On
`openrouter/deepseek/deepseek-v4-flash`:
`tui._short_model` → `deepseek-v4-flash`;
`viewer._short_model` → `deepseek/deepseek-v4-flash` (the bug its own docstring
admits); `viewer._canonical_model` → `deepseek-v4-flash`;
`model_family.model_family` → `deepseek`. Every surface invented its own split
of the same slug.

The through-line: **runtime and effort are derived data that were never given a
home as fields**, so each consumer re-derives them, and the files disagree on
what `agent_model` means.

## Decision

Introduce a single structured record, `ModelSpec`, as the canonical model
identity. **Stored fields are atomic and are never re-parsed; the human display
string is *derived* from the fields and is never read back as a source of
truth.** One factory builds it, the orchestrator persists it, and every reader
and every display routes through the same two methods.

### The record (in `models.py`)

```python
@dataclass(frozen=True)
class ModelSpec:
    runtime: str            # "codex" | "claude" | "opencode" | "fake"  — how it was driven
    requested: str          # EXACTLY what config asked, raw & verbatim — the "always store the raw":
                            #   codex:    "gpt-5.5"
                            #   claude:   ""  (account default) | "opus"
                            #   opencode: "openrouter/deepseek/deepseek-v4-flash"
    effort: str | None      # "high"/"xhigh"/"max"… for a CLI role; None for opencode/fake
    resolved: str | None    # what the STREAM said it actually ran (claude system/init model);
                            #   None when unknown — codex is silent, so it falls back to `requested`
    provider: str | None    # "opencode" | "openrouter" for a zen/OpenRouter slug; None for a CLI role

    # ---- derived, never persisted, never re-parsed from a composite string ----

    def canonical(self) -> tuple[str, str]:
        """Stable (name, runtime) grouping key for the viewer/pipeline tab.
        Prefers `resolved` over `requested` so two 'claude default' runs that
        actually ran different models do NOT collide into one bucket."""
        name = (self.resolved or self.requested or "?").rsplit("/", 1)[-1]
        return (name.replace(" ", "-"), self.runtime)

    def display(self) -> str:
        """The ONE clean human string. No consumer ever parses it back."""
        name = (self.resolved or self.requested).rsplit("/", 1)[-1] or "default"
        prefix = self.provider or self.runtime      # openrouter / opencode / codex / claude
        eff = f" · {self.effort}" if self.effort else ""
        return f"{prefix}/{name}{eff}"

    def to_dict(self) -> dict: ...                    # the JSON blob persisted (atomic fields only)

    @classmethod
    def from_record(cls, obj) -> "ModelSpec":         # accepts the new dict OR a legacy string
        ...                                           # the ONLY place the old regex survives

    @classmethod
    def for_role(cls, config, role) -> "ModelSpec":   # single factory, replaces role_model_label()
        ...                                           # role ∈ {"agent","sim","extra"}; applies the
                                                      # sim_actor_mode / sim_cli_tool overrides
```

`display()` output, one function for every surface:

| run | `display()` | `canonical()` |
|---|---|---|
| codex gpt-5.5, high | `codex/gpt-5.5 · high` | `("gpt-5.5", "codex")` |
| claude default → sonnet, max | `claude/claude-sonnet-4-6 · max` | `("claude-sonnet-4-6", "claude")` |
| opencode zen | `opencode/deepseek-v4-flash-free` | `("deepseek-v4-flash-free", "opencode")` |
| OpenRouter | `openrouter/deepseek-v4-flash` | `("deepseek-v4-flash", "opencode")` |

### Storage layout (dual-write, both files, same schema)

The orchestrator persists one `ModelSpec.to_dict()` per role into **both**
`stats.json` and `run_config.json` under a new `*_spec` key, while leaving the
existing string/slug key exactly as it is today:

```jsonc
// stats.json  AND  run_config.json — identical schema in both (fixes defect #2)
{
  "agent_model": "gpt-5.5 (codex, effort=high)",   // LEGACY string — UNCHANGED, old readers untouched
  "agent_model_spec": {                            // NEW canonical record — new readers prefer this
    "runtime": "codex",
    "requested": "gpt-5.5",
    "effort": "high",
    "resolved": null,                              // codex stream is silent
    "provider": null
  },
  "sim_model": "...", "sim_model_spec": { ... },
  "extra_reviewer_model": "...", "extra_reviewer_model_spec": { ... }
}
```

> **Keying note (one open sub-decision).** The dual-write was chosen so *old
> readers keep working untouched*. That goal is only fully met if the existing
> `agent_model` key keeps its existing type — the legacy **string** in
> `stats.json`, the legacy **slug** in `run_config.json` — and the new dict is
> added under `agent_model_spec`. The alternative (make `agent_model` itself the
> dict and move the string to `agent_model_label`) would force a touch of every
> current reader, e.g. `_short_model(stats["agent_model"])` would crash on a
> dict — which defeats "untouched." This ADR recommends the non-breaking keying
> above; flip it only if you specifically want the dict to own the canonical key
> name.

**Resolved back-fill.** At run end the orchestrator reads the raw stream
(reusing the claude `system/init` parser already in
[cli_actor.py:328](../../contremaitre/cli_actor.py)) and fills `resolved`
before writing — so the effective model becomes durable on disk, not merely
on-screen (fixes defect #3). For codex and opencode `resolved` stays `null`
(codex is silent; opencode's `requested` *is* the resolved model).

### Reading & display: five normalizers collapse to one

These all delete and route through `ModelSpec.from_record(record).display()` or
`.canonical()`:

- `tui._short_model`, `tui._model_effort_display`
- `viewer._short_model`, `viewer._canonical_model` (and its `_CLI_LABEL_RE`)
- `model_family.model_family`

`from_record()` accepts the new dict *or* a legacy string, so the single
surviving copy of the old regex lives there and nowhere else (fixes #1 and #5).

### Canary digest fix

`manifest_digest()` switches from hashing the raw `agent_model`/`sim_model`
slug to hashing each role's `canonical()` tuple **plus its effort**. CLI model
swaps and effort changes then register as a different system under test
(fixes defect #4). See Consequences for the baseline-reset implication.

## Considered options

- **Keep the label string, just standardize the format.** Rejected: it leaves
  identity as text that every consumer must still parse, and never fixes the
  account-default `resolved` gap or the canary digest. It's polishing the
  contract, not removing it.

- **Persist effort as a separate scalar but keep `agent_model` a string.**
  Rejected as half-measure: it fixes effort but not runtime detection, not the
  two-file format split, not `resolved`, and leaves four of the five
  normalizers in place.

- **Structured dict only (no legacy string), with a read-time shim for old run
  dirs.** Viable and cleaner on disk, but the chosen path keeps the legacy
  string *alongside* the dict so existing readers and tests stay byte-for-byte
  untouched during rollout. The redundancy is the deliberate price of a
  zero-break migration.

## Consequences

- **The canary digest change resets baselines.** Switching `manifest_digest()`
  to a canonical+effort key changes the digest for *every* run, opencode
  included (slug → leaf+runtime). Any baseline already promoted on disk becomes
  non-comparable. Gate this behind a digest version bump and a deliberate
  baseline recapture; do not ship it silently mid-experiment. (This is why
  "implement but skip the digest change" was offered as a separate scope — the
  digest fix is correct but is the one piece with reach beyond display.)

- **`resolved` is populated for claude only.** Codex never reports its model, so
  a codex `ModelSpec` always has `resolved == null` and falls back to
  `requested` (`config.codex_model`, which *is* authoritative for codex). This
  is correct, but means "resolved vs requested drift" is a claude-only signal.

- **On-disk redundancy.** Dual-write stores identity twice (string + dict). The
  string is legacy ballast that a later ADR can drop once no reader depends on
  it; until then it is the back-compat guarantee.

- **`role_model_label()` becomes a thin shim** over `ModelSpec.for_role(...)
  .legacy_string()` so the legacy key keeps emitting the exact same string and
  no current test snapshot moves.

## Implementation checklist (for a later session)

1. `models.py` — add `ModelSpec` (+ `for_role`, `to_dict`, `from_record`,
   `canonical`, `display`, `legacy_string`); reduce `role_model_label` to a
   shim calling `legacy_string()`. Keep `is_zen_model` as the
   provider-detection helper feeding `provider`.
2. `orchestrator.py:_write_final_stats` — write `*_spec` dicts alongside the
   existing `*_model` strings; back-fill `resolved` from the raw stream
   (`raw_export` / `sim_raw_export`) via the claude-init parser.
3. `manifest.py:build_manifest` — add the `*_spec` dicts next to the existing
   slug fields; `manifest_digest` → canonical()+effort, behind a digest version
   bump.
4. `tui.py` — delete `_short_model` / `_model_effort_display` /
   `_relabel_with_real_model`; header, pane titles, and launch recap call
   `ModelSpec.from_record(...).display()`. The live relabel becomes "prefer the
   spec's `resolved`, else stream-fill it" — same behavior, no string surgery.
5. `viewer/index.py` — delete `_short_model`, `_canonical_model`, `_CLI_LABEL_RE`;
   `_summarize_run`, `_collect_pipeline_pairings`, `_infra_only_pairings`, and
   `_model_html` consume `ModelSpec`. The pipeline tab's `(name, runtime)`
   bucket key becomes `spec.canonical()` directly.
6. `model_family.py` — re-express `model_family()` on top of `ModelSpec`, or
   fold the family notion into the record if the picker still needs it.
7. Tests — new `ModelSpec` unit tests (each runtime, account-default, the three
   slug shapes, legacy-string round-trip via `from_record`); update any
   snapshot that asserts on `stats.json`/`run_config.json` to expect the added
   `*_spec` keys (the legacy strings should not move).
