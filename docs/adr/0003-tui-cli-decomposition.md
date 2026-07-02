---
status: proposed
date: 2026-07-02
---

# Decompose `tui.py` and `cli.py` into single-concern modules

`tui.py` (3,180 lines, 95 defs) and `cli.py` (2,026 lines, 56 defs) are the
two largest modules in the tree — 50% and 16% larger than the next
(`eval.py`). Neither is one Module; each is four-to-seven Modules fused into
one file, and the fusion is now producing the concrete defects below. This ADR
proposes a pure-movement decomposition (plus one seam fix), no behavior
change.

## Context

### tui.py is four Modules in one file

**1. Run-state interpretation (~lines 85–1489, ~1,400 lines, textual-free).**
Pure snapshot readers and derivations over the run dir:
[`_derive_phase`](../../contremaitre/tui.py) (line 262),
`_reviewer_status` (424), `_cli_review_status_glyph` (481),
`_read_pr_json` (868), `_read_run_models` (3139). None of these touch
textual or rich beyond `Text` construction; all of them are *domain*
interpretation of the artifact contract.

Evidence this layer is a de facto Interface, not private chrome:

- `tests/test_cli_reviewer.py:13` imports `_derive_phase` **from tui** — a
  *reviewer* test reaching into the TUI file for domain logic.
- `tests/test_tui.py` imports **~20 underscore-private helpers** across two
  import blocks (lines 21, 57) plus a dozen inline `from contremaitre.tui
  import …` statements. The test suite treats these functions as the module's
  Interface; the leading underscores are fiction.

This also cuts against the Artifact-reader doctrine in
[CONTEXT.md](../../CONTEXT.md): the upper layer reads through `RunArtifacts`,
and the TUI mostly does (`self._arts.phases(live=True)`, tui.py:2765) — but
the file still carries a parallel colony of ad-hoc readers
(`_read_pr_json`, `_read_run_models`, the quota readers below) living next to
the widgets instead of beside the reader.

**2. Provider quota telemetry (~lines 1065–1445, ~380 lines).**
`_latest_codex_rollout` / `_read_codex_usage` parse codex session-rollout
files; `_last_claude_statusline_usages` / `_claude_usage_from_statusline`
parse `statusline.jsonl`; `_footer_meter_tokens` (1434) formats both. This is
per-CLI-tool telemetry decoding — knowledge that belongs beside the
`CliDriver` seam, not interleaved with widget helpers. It duplicates the
class of runtime-shape knowledge that CONTEXT.md declares "an internal seam"
of `flow_use`.

**3. Docker probing (lines 962–1064).** `_docker_info` and
`_container_mount_mode` shell out to `docker inspect` / `docker ps` via
subprocess. Host telemetry, zero rendering content.

**4. Rich renderables + the textual App (lines 1490–3180).**
The per-event Rich renderables section (banner at 1490) decodes opencode,
codex, and claude event shapes *again* (`_codex_item_row` 1643,
`_claude_event_rows` 1719) — a second home for per-runtime stream knowledge.
Then the ~915-line `ContremaitreTUI` App class is defined **inside**
`if _TEXTUAL_AVAILABLE:` (line 2047) — conditionally defined classes are
hostile to static analysis, and the guard forces every pure helper above it
to share a file with the optional dependency. `attach` (2993) and
`spawn_and_attach` (3024) are process management, not UI.

### cli.py is command dispatch plus six embedded services

Only `__main__.py` imports `cli` — the module boundary is clean from outside.
Inside, `build_parser` + dispatch (`main`, 2018) share the file with:

- **Clone cache** — `_default_cache_path` (610), `_slug_from_url` (623),
  `_ensure_local_clone` (641). Git-domain logic, belongs beside
  `git_utils`.
- **Model catalog network I/O** — `_normalize_openrouter_slug` (1599),
  `_fetch_openrouter_catalog` (1614), `_fetch_free_models` (1646),
  `_probe_zen_model` (1087). Live HTTP fetches embedded in the argparse
  file, shared by the picker and `models` cmd.
- **Interactive onboarding UX** — `_onboard_claude_token` (821),
  `_pick_zen_model_interactive` (887), `_preflight_presence_check` (951),
  `_recap_and_confirm` (1032), the `_codex_token_line` family (786–820).
- **Image lifecycle** — `_ensure_image_for` (1145), `_dockerfile_hash`
  (1209), `_build_image_inline` (1412): a second home for image-staleness
  logic whose first home is `runtime_image.py`.
- **Cleanup scanning** — `_scan_stale_containers` (1337),
  `_scan_stale_worktrees` (1377), `_scan_dangling_images` (1397),
  `_cleanup_cmd` (1229).
- **RunConfig assembly** — `_resolve_actor_fields` (1472),
  `_config_from_args` (1493).

### The argv-surgery seam between the two files

`_tui_run_cmd` (cli.py:1707) does **not** parse its forwarded args with the
real parser. It string-surgeries the raw argv via `_extract_flag_value` /
`_remove_flag` / `_set_flag_value` (1560–1598) — a second, hand-rolled flag
parser that must be kept in lockstep with `build_parser` by discipline alone.
Every flag added to `run` that `tui run` needs to observe requires
remembering to extend the surgery. This is a parse contract masquerading as
string manipulation — the same defect shape ADR-0001 removed from model
identity.

## Decision

Split both files by Module, moving code verbatim wherever possible. One
behavioral seam fix (argv parsing); everything else is movement.

### tui.py → `contremaitre/tui/` package

| New module | Contents (from current tui.py) | Depends on |
|---|---|---|
| `tui/run_state.py` | `_derive_phase`, `_phase_trail`, `_reviewer_status`, `_reviewer_glyph`, `_cli_review_status_glyph`, `_review_status_tail`, `_current_review_round`, `_current_cli_review_round`, `_round_verdict`, `_verdict_glyph`, `_aggregate_cli_review_verdict`, `_derive_cli_review_states`, `_read_pr_json`, `_pr_number_from_url`, `_read_run_models`, `_run_config_spec`, `_activity_state`, `_provider_fast_fail`, `_infra_failure_label`, `_terminal_badge`, `_persistent_review_token`, `_warnings_token`, `_text_event_count`, `_task_count`, `_latest_pending_tool` | stdlib, `models`, `run_artifacts` — **no rich, no textual** |
| `tui/quota.py` | `_latest_codex_rollout`, `_read_codex_usage`, `_codex_usage_from_payload`, `_statusline_model_id`, `_claude_usage_from_statusline`, `_last_claude_statusline_usage(s)`, `_read_claude_usage(s)`, `_select_claude_usage`, `_codex_usage_token`, `_claude_usage_token`, `_footer_meter_tokens`, `_fmt_window_minutes`, `_fmt_reset`, `_usage_pct_style`, `_append_secondary_window`, `_short_quota_model`, `_claude_family`, `_is_free_model` | stdlib, rich `Text` |
| `tui/docker_probe.py` | `_docker_info`, `_container_mount_mode` | stdlib subprocess |
| `tui/render.py` | everything under the "Per-event Rich renderables" banner (1490–2043): `_TOOL_STYLES`, `_tool_body`, `_build_event_row`, `_codex_item_row`, `_claude_tool_body`, `_claude_tool_result_text`, `_claude_event_rows`, `_event_table`, `_render_event`, `_render_guardrail`, `_turn_separator`, `_cli_review_tool_header`, `_role_label`, plus the small formatters (`_fmt_ts`, `_fmt_elapsed`, `_truncate`, `_short_repo`, `_render_pane_subheader`, `_current_phase_label`) | rich only — **no textual** |
| `tui/app.py` | `_NoFocusRichLog`, `ContremaitreTUI`, the CSS, constants (`DOCKER_REFRESH_S`, `CODEX_USAGE_REFRESH_S`, `_UNSET_ACTIVE`) | textual (unconditional import — this module is only imported after the guard) |
| `tui/launch.py` | `_require_textual`, `attach`, `spawn_and_attach`, `_print_final_urls` | imports `tui.app` lazily *after* `_require_textual` |
| `tui/__init__.py` | re-export `attach` + `spawn_and_attach` only (the two names `cli.py` uses) | — |

Rules:

- The `try: import textual` guard **dies**. `app.py` imports textual
  unconditionally; `launch._require_textual` remains the sole gate and runs
  *before* the `tui.app` import. `ContremaitreTUI` becomes an
  unconditionally-defined class in its module.
- `run_state.py` and `quota.py` take a `RunArtifacts` (or plain paths) at
  their edges — they must stay importable with textual absent. This is what
  legitimizes `test_cli_reviewer.py`'s use of `_derive_phase`: it becomes an
  import from a domain module, not a TUI internal. Promote the names the
  test suite already treats as the Interface (drop the leading underscore on
  `derive_phase`, `reviewer_status`, `read_run_models`, `footer_meter_tokens`
  at minimum; keep genuinely local helpers private).
- **Deletion test on duplication, not unification by default.** The
  per-runtime event decoding in `render.py` overlaps `flow_use`'s marker
  decoding in *knowledge*, not in code path — the renderables need
  role/tool/row shapes `flow_use` deliberately doesn't model. Do **not**
  force them into one decoder in this change; note the seam and leave it.

### cli.py → thin dispatch + extracted services

| New module | Contents (from current cli.py) |
|---|---|
| `clone_cache.py` | `_default_cache_path`, `_slug_from_url`, `_ensure_local_clone` |
| `catalog.py` | `_normalize_openrouter_slug`, `_fetch_openrouter_catalog`, `_fetch_free_models`, `_probe_zen_model`, `_synthesize_opencode_config` |
| `onboarding.py` | `_onboard_claude_token`, `_pick_zen_model_interactive`, `_preflight_presence_check`, `_recap_and_confirm`, `_codex_token_line`, `_claude_token_line`, `_opencode_key_line`, `_b`, `_d` |
| `runtime_image.py` (existing) | absorb `_ensure_image_for`, `_ensure_default_image_built`, `_dockerfile_hash`, `_build_image_inline` — image staleness gets one home |
| `cleanup.py` | `_cleanup_cmd` body, `_scan_stale_containers`, `_scan_stale_worktrees`, `_scan_dangling_images`, `_prune_dangling_images`, `_list_cache_clones` |
| `runconfig_build.py` | `_resolve_actor_fields`, `_config_from_args`, `_agent_name_to_runtime`, `_active_codex_roles`, `_cli_egress_is_auto`, `_maybe_provision_cli_egress` |
| `cli.py` (kept) | `build_parser`, `_shared_run_doctor_parser`, `main`, and the per-subcommand `_*_cmd` shims — each shim a thin call into the modules above |

### Seam fix: `tui run` parses with the real parser

Delete `_extract_flag_value` / `_remove_flag` / `_set_flag_value`.
`_tui_run_cmd` parses `forwarded` with the same `run` subparser
`build_parser` builds (expose it, or re-invoke
`build_parser().parse_args(["run", *forwarded])`), reads the resulting
namespace for slug/root/fork/base/agent/models, and reconstructs the child
argv from the namespace (or forwards the original argv verbatim once the
values are extracted — the child re-parses anyway). One parsing
implementation; a new `run` flag becomes visible to `tui run` for free.
This is the only intended behavior-adjacent change; `tests/test_tui_launch.py`
pins the spawn contract and must stay green.

## Consequences

- **Tests move with their subjects.** `test_tui.py` splits along the same
  lines (`test_tui_run_state.py`, `test_tui_quota.py`, `test_tui_render.py`,
  …); `test_cli_reviewer.py`'s import becomes
  `from contremaitre.tui.run_state import derive_phase`. Per AGENTS.md,
  **no re-export shims for the old private names** — update every importer
  in the same change.
- **`pyproject.toml` untouched** except that coverage keeps working (source
  is package-level). `textual` remains an optional extra; the importable
  surface without it *grows* (run_state, quota, render, docker_probe all
  work bare).
- **Docs in the same commit(s):** AGENTS.md "Where to edit" gains the new
  module names (`tui/app.py` for live UI, `cleanup.py`, `catalog.py`,
  `runconfig_build.py`); control-plane.md's module map likewise.
- **`system_digest` moves** (contremaitre code changes), so per AGENTS.md the
  eval-canary note applies: append an Intent/Outcome/Learning entry; no
  baseline re-promotion is expected since behavior is unchanged, but
  `eval compare` on case_01 before/after is the cheap insurance.
- **PR sequencing** (each lands green, `uv run pytest` + `make lint`):
  1. Extract `tui/` package: run_state + quota + docker_probe + render moved
     verbatim, app + launch split, guard deleted. Update `cli.py` lazy
     imports and all test imports.
  2. Extract cli.py services (clone_cache, catalog, onboarding, cleanup,
     runconfig_build; fold image fns into runtime_image).
  3. The argv-surgery seam fix, isolated so `test_tui_launch.py` failures
     bisect to it.

## What NOT to do

- Don't "improve" logic while moving it. Movement commits contain movement;
  a reviewer must be able to verify them by symbol diff.
- Don't add an `__init__` re-export layer preserving `contremaitre.tui._derive_phase`
  style paths. Pre-1.0, no compat shims — change the shape, update callers.
- Don't unify `render.py`'s event decoding with `flow_use`'s in this change.
  Two adapters may make that a real seam later; today it's one hypothetical.
- Don't let `run_state.py` or `quota.py` grow a textual import. If a helper
  needs a widget, it belongs in `app.py`.
