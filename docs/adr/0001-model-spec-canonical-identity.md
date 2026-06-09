---
status: accepted
date: 2026-06-07
---

# Model identity is a stored structured record, not a display string

> **Implemented 2026-06-08.** `ModelSpec` is live in `models.py`; identity is
> persisted as the per-role dict in `stats.json` and `run_config.json`, and the
> five normalizers are deleted. Two deviations from the proposal below, both
> driven by `AGENTS.md` conventions:
>
> - **No dual-write (no-shim).** `AGENTS.md` forbids backwards-compat layers
>   pre-1.0, so `agent_model`/`sim_model` *became* the `ModelSpec` dict and
>   `role_model_label` was deleted outright — no legacy string kept alongside.
>   `ModelSpec.from_record` still *reads* legacy on-disk strings (a reader-edge
>   adapter only; the sole home of the old regex), so historical run dirs render.
> - **`model_family.py` deleted, not re-expressed.** It had zero importers — the
>   deletion test said delete it rather than rebuild it on `ModelSpec`.
>
> The digest change landed with a `DIGEST_VERSION` bump (`manifest.py`), which
> resets promoted baselines as the Consequences section warns.

## Context

A run's model identity — for the agent and the SIM — has to survive four very
different surfaces: the now-deleted operator defaults layer, the live TUI, the
orchestrator's internal logic, and the post-hoc `viewer/`.
Today identity is smuggled inside a **display string** and re-parsed
downstream, which has produced five distinct, compounding defects.

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

### Storage layout (the dict owns `agent_model` / `sim_model`, both files)

> **As built (no-shim).** The proposal originally weighed a non-breaking
> dual-write (legacy string kept, new dict added under `*_spec`). That was
> **not** taken — `AGENTS.md` forbids backwards-compat layers pre-1.0. The
> `ModelSpec` dict **is** the value of `agent_model`/`sim_model`; there is no
> legacy string and no `*_spec` key. The strikethrough text below is preserved
> only to show what the keying note debated.

The orchestrator persists one `ModelSpec.to_dict()` per role into **both**
`stats.json` and `run_config.json`, as the value of `agent_model`/`sim_model`:

```jsonc
// stats.json  AND  run_config.json — identical schema in both (fixes defect #2)
{
  "agent_model": {                                 // the canonical record IS the value
    "runtime": "codex",
    "requested": "gpt-5.5",
    "effort": "high",
    "resolved": null,                              // codex stream is silent
    "provider": null
  },
  "sim_model": { ... }
}
```

> **Keying note (resolved).** The proposal recommended the non-breaking keying —
> ~~keep `agent_model` as the legacy string/slug and add the dict under
> `agent_model_spec`~~ — to leave old readers untouched. The implementation took
> the rejected alternative instead: **the dict owns `agent_model`** and every
> reader was updated. Because we control all four readers (tui, viewer,
> manifest-digest, eval) and `AGENTS.md` forbids shims, "untouched old readers"
> was not a goal worth a legacy key. Pre-migration run dirs that still hold the
> legacy string are tolerated at read time by `ModelSpec.from_record`, which is
> the only place the old shape is understood.

**Resolved back-fill.** At run end the orchestrator reads the raw stream via
`models.resolved_model_from_events` (the claude `system/init` parser) and fills
`resolved` before writing — so the effective model becomes durable on disk, not
merely on-screen (fixes defect #3). For codex and opencode `resolved` stays
`null` (codex is silent; opencode's `requested` *is* the resolved model).

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
  dirs.** **Chosen.** The dict owns `agent_model`/`sim_model`; no legacy string
  is written. `ModelSpec.from_record` reads old run dirs (the read-time shim).
  Per `AGENTS.md` (no backwards-compat layers pre-1.0), the dual-write
  alternative below was rejected. _(The proposal originally recommended
  dual-write; the implementation reversed that — see the as-built notes.)_

- ~~**Dual-write: keep the legacy string alongside a new `*_spec` dict.**~~
  Rejected at implementation: it is a backwards-compat layer `AGENTS.md`
  forbids, and we control every reader, so "untouched old readers" bought
  nothing worth the on-disk redundancy.

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

- **No on-disk redundancy.** Identity is stored once, as the dict under
  `agent_model`/`sim_model`. There is no legacy string. (The proposal weighed a
  dual-write that stored it twice; that was rejected — see Considered options.)

- **`role_model_label()` is deleted, not retained as a shim.** Its callers
  (orchestrator stats, manifest, tui recap/header, cli recap) now build a
  `ModelSpec` and call `display()` (for humans) or persist `to_dict()`. Test
  snapshots that asserted on the legacy string were updated to the dict shape —
  the no-shim choice means snapshots move, and that is accepted.

## Implementation checklist (as built, no-shim)

1. `models.py` — added `ModelSpec` (+ `build`, `for_role`, `to_dict`,
   `from_record`, `canonical`, `display`, `with_resolved`) and
   `resolved_model_from_events`. **Deleted** `role_model_label` (no
   `legacy_string` shim). `from_record` reads the canonical dict *or* a legacy
   on-disk string — the only place the old regex survives. `is_zen_model` kept
   as the provider-detection helper feeding `provider`.
2. `orchestrator.py:_write_final_stats` — writes the `ModelSpec.to_dict()` per
   role as the value of `agent_model`/`sim_model`; back-fills `resolved` from
   the raw stream (`raw_export` / `sim_raw_export`) via
   `resolved_model_from_events`.
3. `manifest.py:build_manifest` — writes the dict under `agent_model`/`sim_model`;
   `manifest_digest` → canonical()+effort, behind a `DIGEST_VERSION` bump.
4. `tui.py` — deleted `_short_model` / `_model_effort_display` /
   `_relabel_with_real_model` (and the small label-parse helpers); header, pane
   titles, and launch recap call `ModelSpec(...).display()`. The live relabel is
   `spec.with_resolved(stream_model)` — prefer the spec's `resolved`, else
   stream-fill it. No string surgery.
5. `viewer/index.py` — deleted `_short_model`, `_canonical_model`,
   `_CLI_LABEL_RE`; `_summarize_run`, `_collect_pipeline_pairings`, and
   `_infra_only_pairings` route through `ModelSpec.from_record(...).canonical()`.
   `viewer/__init__.py:_assemble_data` embeds `from_record(...).display()` so the
   per-run `_renderer.js` consumes a derived string, never the raw dict.
6. `model_family.py` — **deleted** (zero importers; dead code). The TUI's
   claude-footer family scan moved to a local `_claude_family(name)` over a
   plain model name.
7. Tests — new `tests/test_models.py` unit tests (each runtime, account-default
   with empty `requested` preserved, the slug shapes, legacy-string +
   dict round-trips via `from_record`); snapshots that asserted on
   `stats.json`/`run_config.json` updated to the dict shape (no `*_spec` keys;
   the legacy strings are gone).
