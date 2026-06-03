# Control Plane

This is Contremaitre's implementation map. The control plane is deterministic Python on the host; the agent and SIM run inside opencode-in-Docker containers and never hold git or GitHub credentials.

Audience: humans modifying the orchestrator, and LLMs that need to reason about the system end-to-end. Read it top-to-bottom for orientation; the section anchors are stable for targeted lookup.

## Architecture

```
┌─ HOST ─────────────────────────────────────────────────────────────────────────┐
│                                                                                │
│   orchestrator.py ─── state machine, caps, marker watches, hard gates          │
│        │             cleanup labels, SIGTERM emergency-flush                   │
│        ├── git_utils.py ─── logged git wrapper (clone, fetch, commit, push)    │
│        ├── diffscan.py ──── forbidden-path scanner (.env, *.pem, *.key)        │
│        ├── verdicts.py ──── fence-tolerant strict-JSON parser, diff hash       │
│        ├── checks.py ────── --check-cmd runner (sidecar container)             │
│        ├── publisher.py ─── StubPublisher / GhPublisher (`gh pr create`)       │
│        ├── cli_reviewer.py  post-publish `claude -p` / `codex exec`            │
│        ├── runtime_image.py lockhash-keyed deps volumes                        │
│        └── manifest.py ─── provenance: model IDs, image digest, prompt hashes  │
│                                                                                │
└────────────┬─────────────────────────────────────────────────────────────────┬─┘
             │                                                                 │
             │ docker (label=contremaitre.run-id=<id>)                         │
             ▼                                                                 ▼
┌─ DOCKER ──────────────────────────────────────┐     ┌─ EXTERNAL ───────────────┐
│                                               │     │                          │
│   agent container   role=agent   /app  RW    ─┼─►   │   OpenRouter             │
│   sim container     role=sim     /app  :ro   ─┼─►   │   / OpenCode Zen         │
│   review container  role=review  /review :ro ─┼─►   │                          │
│   extra reviewer    role=sim     /review :ro │     │   GitHub                  │
│   check sidecar     role=check   /app  RW   ◄┼─── (host `gh pr create --draft`)│
│   deps-install      role=deps-install        │     │                          │
│   deps-clone        role=deps-clone          │     └──────────────────────────┘
│                                               │
│   Mount layout                                │
│     /app             → run worktree           │
│     /review          → diff + SETTLED only    │
│     /app/<deps_mount>→ per-run deps volume    │
│                                               │
└───────────────────────────────────────────────┘
```

Containers are launched detached (`docker run -d`) and labeled with `contremaitre.run-id=<id>` and `contremaitre.role=<agent|sim|review|check|deps-install|deps-clone>` so `_stop_run_containers` + `contremaitre cleanup` can sweep them by label.

## Run flow (skill-aware)

The skill ([`improve-codebase-architecture`](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture)) drives the conversation; the orchestrator drives container lifecycle, marker watches, and gates. Inside `WORK`, every row below is one or more turns of the same multi-turn opencode session.

```
WORK SESSION (one opencode session — `agent_turn → sim_turn → agent_turn → …`)

  AGENT (role=agent, /app RW)                  SIM (role=sim, /app :ro)
  ───────────────────────────                   ───────────────────────

  Explore  ────────  read repo, glob, grep
    │ event: `turn`                            (no SIM turn yet)
    │ TUI phase: exploring
    │
  HTML cards
    │ writes .contremaitre/                   ◄── reads via /app:ro mount
    │   architecture-review.html
    │ convention (telemetry only —
    │   no harness check, no event)
    │
  Pick gate
    │ agent: "Which would you like              SIM: picks one + 2-3 sentence
    │   to explore?"                  ◄────►    justification in skill vocab
    │ event: `turn` (per turn,                 (Module / Seam / Depth …)
    │   role-tagged from actor JSONL)          forbidden tools: write / edit /
    │ TUI phase: grilling                      bash / task / apply_patch
    │
  Grill / Deepening
    │ N turns. Agent proposes               ◄────► SIM cites constraints,
    │   interfaces, pushes back on              names load-bearing details,
    │   SIM friction.                            pushes back. One turn = one
    │ counter: grilling_exchanges                complete reply (no
    │ (post-run, via flow_use.py)                placeholders).
    │
  Settle
    │ writes .contremaitre/                    SIM role shifts to drift-watch
    │   SETTLED_DESIGN.md
    │ HARNESS GATE — required for              (presence detected by
    │   REVIEW; missing → NO_PR                 filesystem poll —
    │                                           no dedicated event)
    │
  Implement
    │ M turns. Agent edits files.           ◄────► SIM reads each diff.
    │ counter: impl_turns                        "Faithful at x.ts:42" or
    │ TUI phase: implementing                    "Drift — new abstraction at
    │                                           y.ts:9, not in SETTLED."
    │
  CI gates
    │ runs project's test suite +              (no SIM turn)
    │   formatter/lint scoped to
    │   changed files. Discovers them
    │   via .github/workflows/,
    │   .pre-commit-config.yaml, etc.
    │ convention (telemetry: `tested ✗`)
    │
  Marker
    │ writes .contremaitre/                   SIM: "OK — handing off to review."
    │   IMPLEMENTATION_COMPLETE                yields turn
    │ HARNESS GATE — terminal signal           (presence detected by
    │   for the WORK loop                       filesystem poll;
    │ event: `work_session_end`                 `work_session_end` follows)

  Loop termination: marker present, cap trip (`turn_cap` / `wall_cap` /
  `recorded_cost_cap` / `no_progress_cap` events), or max_turns reached.

                              │
                              ▼

REVIEW round N  (N = 1 … max_review_rounds, default 3)

  REVIEWER container (role=review, /review :ro)
  ─────────────────────────────────────────────
  reads:  /review/diff.patch
          /review/SETTLED_DESIGN.md
  emits:  strict JSON
          { verdict, confidence, required_changes, checks_performed, summary }
  parser: verdicts.parse_sim_verdict — fence-tolerant
  event:  review_verdict

  Extra reviewer (optional, --extra-reviewer-model)
    Runs in parallel for the same round. Both verdicts must APPROVE.
    Disagreement is tracked (`cross_family_agreement_rate`) but does NOT
    gate the publication decision on its own.

  ┌── APPROVED ─────────────────────────► hard gates → publish
  │
  ├── CHANGES_REQUESTED                    event: `revision_requested`
  │                                        clears IMPLEMENTATION_COMPLETE,
  │                                        sends required_changes summary
  │                                        back into the SAME opencode
  │                                        session (--session, agents.py
  │                                        threads `session_id` across
  │                                        review rounds), loops to WORK
  │
  └── NEEDS_HUMAN / malformed (after       NO_PR_NEEDS_HUMAN
      max_verdict_retries) / max_review_   NO_PR_CHANGES_REQUESTED
      rounds exhausted                     (event: `malformed_verdict` on
                                            each unparseable attempt)

                              │
                              ▼

HARD GATES (host, all must pass; deterministic)

  1. diff_scan          forbidden paths in diff?  (diffscan.py)
  2. diff_hash_matched  diff hash == hash at APPROVED?  (verdicts.diff_hash)
  3. clean_worktree     `git status --porcelain` clean except .contremaitre/*?
  4. draft_only         publication mode is `gh --draft`?  (always True;
                        belt-and-suspenders — published comes from publisher.py)
  5. executable checks  every --check-cmd passes in the sidecar container?
                        (skip if none configured → executable_confidence: null)

  Forbidden paths      .env, *.pem, *.key, and nested forms
  Forbidden exceptions .env.example, .env.sample, .env.template, .env.defaults

  All pass:  publisher.GhPublisher → `gh pr create --draft`
             then optional CLI review (claude / codex).
  Any fail:  NO_PR_NEEDS_HUMAN; PR not opened.

                              │
                              ▼

POST-PUBLISH CLI REVIEW  (only when --cli-reviewer != none)

  Runs on the HOST (no container). cli_reviewer.py:
    - detect: `shutil.which("claude")`, `shutil.which("codex")`
    - both: claude first, then codex (sequentially, two PR comments)
    - prompt: prompts/cli_reviewer_prompt.md with {pr_url} substituted
    - command (subprocess receives expanded paths, not shell `~`):
        claude:  claude -p --permission-mode bypassPermissions <prompt>
        codex:   codex exec --skip-git-repo-check --sandbox workspace-write
                 --add-dir <Path.home() / ".cache">
                 -o <final_message_path> <prompt>
    - env: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, OPENAI_API_KEY blanked
           in the subprocess env (forces OAuth subscription, never billed API)
    - timeout: 600s per tool
    - post: `gh pr comment <pr_url> --body-file <tool>_review.md`
    - failures: logged, NEVER block the run — the PR is already published

  Verdict glyph (parsed from line 1 of the review):
    🟢 LOOKS_GOOD   → 1.0    cli_review_score
    🟠 NEEDS_ATTENTION → 0.5
    🔴 MUST_FIX     → 0.0
```

## State machine reference

The orchestrator's POV. `State` enum in [models.py:14-20](../contremaitre/models.py#L14-L20).

```
INIT  ──►  WORK  ──►  REVIEW  ──►  APPROVED  ──►  (hard gates + publish)
                              ↘
                              CHANGES_REQUESTED  ──►  WORK  (up to max_review_rounds)
                              ↘
                              NO_PR
                              FAILED
```

- **INIT** — worktree creation, deps volume provisioning, preflight. Transition to WORK on success.
- **WORK** — one multi-turn opencode session. Terminates on `.contremaitre/IMPLEMENTATION_COMPLETE` (harness gate), cap trip, or `max_turns`.
- **REVIEW** — single-shot reviewer container (role=review) reads `/review/diff.patch` + `/review/SETTLED_DESIGN.md`, emits JSON verdict.
- **APPROVED** — runs hard gates, then the publisher. Success → `READY_FOR_DRAFT_PR`.
- **NO_PR** — terminal without publication.
- **FAILED** — infrastructure exception; the SIGTERM handler also routes here.

## Terminal verdicts

All five values from `TerminalVerdict` ([models.py:35-45](../contremaitre/models.py#L35-L45)):

| Verdict | Trigger | Reference |
|---|---|---|
| `READY_FOR_DRAFT_PR` | APPROVED + hard gates pass + checks pass + publisher succeeds | [orchestrator.py:761](../contremaitre/orchestrator.py#L761) |
| `NO_PR_CHANGES_REQUESTED` | `max_review_rounds` exhausted on CHANGES_REQUESTED | [orchestrator.py:319](../contremaitre/orchestrator.py#L319) |
| `NO_PR_NEEDS_HUMAN` | NEEDS_HUMAN verdict / malformed verdict (after retries) / missing SETTLED / missing IMPLEMENTATION_COMPLETE / cap trip / hard-gates fail / executable check fail | [orchestrator.py:243, 249, 257, 278, 287, 804, 811, 815](../contremaitre/orchestrator.py#L243) |
| `FAILED_INFRA` | Unhandled exception during run; SIGTERM | [orchestrator.py:151, 178](../contremaitre/orchestrator.py#L151) |
| `QUOTA_EXHAUSTED` | `ActorError` with `kind == PROVIDER_QUOTA_EXHAUSTED` (e.g. OpenCode Zen `FreeUsageLimitError`). Distinct from FAILED_INFRA so the eval canary aborts the n=3 batch instead of retrying. | [orchestrator.py:174-176](../contremaitre/orchestrator.py#L174-L176), [models.py:40-45](../contremaitre/models.py#L40-L45) |

## Host-owned boundaries

The agent and SIM never hold:

- git credentials. `git_utils.py` runs on the host (clone, fetch, commit, push).
- a GitHub token. `publisher.GhPublisher` runs `gh pr create --draft` on the host.

Read-only enforcement is belt-and-suspenders:

1. SIM container mounts `/app:ro`.
2. SIM persona (`prompts/sim_tooled_persona.md`) explicitly forbids `write` / `edit` / `apply_patch` / `bash` / `task`.
3. Host `diffscan.py` blocks publication if forbidden paths appear in the diff.

Opencode containers see only `OPENROUTER_API_KEY` (when set) and the proxy variables passed via CLI flags. Ambient host env is never inherited. When `OPENROUTER_API_KEY` is absent, runs default to free OpenCode Zen models served by OpenCode; the container's `OPENROUTER_API_KEY` is simply not exported.

## Preflight

Live opencode runs run preflight before worktree creation; the report is persisted to `eval/preflight_report.json`. `contremaitre doctor` runs the same checks without starting a run.

**Blocks the run:**

- missing target repo / base ref;
- missing Docker daemon or target image;
- opencode binary failures inside the image;
- failed `:ro` mount enforcement test;
- open container egress when no network/proxy is configured and `--allow-open-egress` is not set;
- missing, unlimited, over-cap, or unverified OpenRouter key (when key is required).

**Warns (does not block):**

- OpenRouter key limit excludes BYOK usage (acceptable for non-BYOK models).

**Bypass flags** (loud on purpose):

- `--skip-preflight` — skip everything.
- `--skip-openrouter-key-check` — don't query the key endpoint.
- `--allow-unlimited-openrouter-key` — accept a key without a credit limit.
- `--allow-open-egress` — accept unrestricted container egress.

## Launch sequence

On TTY runs the launcher walks through (`cli.py:_launch_screen`):

1. **OpenRouter key banner** — probes `$OPENROUTER_API_KEY` (or `--openrouter-env-var`), reports presence / limit / remaining via `GET /api/v1/key`.
2. **Model picker** — numbered list of OpenCode Zen free models, plus a paste box for OpenRouter slugs when a key is set. Picks agent → SIM → optional extra-reviewer in sequence.
3. **CLI-reviewer availability banner** — detects `claude` / `codex` on PATH; prompts when `--cli-reviewer auto`.
4. **Pre-flight ping** — probes the chosen Zen models via `_probe_zen_model()` so `FreeUsageLimitError` surfaces *before* the run starts; OpenRouter slugs are verified against the catalog fetch.
5. **Run summary recap** — target URL, branch, agent / SIM / extra models, cli-reviewer choice, cost + wall caps, network posture.
6. **Confirmation prompt** — `Continue? [Y/n]`.

Non-TTY runs and `-y` / `--yes` auto-confirm. `--no-prompt` skips the pickers even on TTY and uses `.contremaitre/defaults.toml` (or the hardcoded fallback below). All three modes still emit `[info]` log lines so the run log explains what was auto-assumed.

## Model selection

Two model sources, picked at launch from a single TTY picker ([cli.py:684](../contremaitre/cli.py#L684)):

- **OpenCode Zen** — free models served by OpenCode itself. Catalog is fetched live at launch via `_fetch_free_models()` from `https://models.dev/api.json` (the same source the opencode binary uses, so the picker never offers a slug the binary will reject). Filtered to entries with `-free`-suffixed IDs plus a small allow-list (e.g. `big-pickle`). Slugs are `opencode/<id>`. No auth — the opencode binary has built-in routing to Zen. Quota probe hits `https://opencode.ai/zen/v1/chat/completions` to surface `FreeUsageLimitError` before the run starts. Why not OpenRouter `:free` slugs: those route through third-party providers whose daily quota is shared across all OpenRouter users, producing `"Out of credits"` mid-run.
- **OpenRouter** — paid models. Requires `OPENROUTER_API_KEY` (`.env`, cwd or repo root; never inherited from ambient host env). Any `openrouter/<provider>/<model>` slug can be pasted at the picker prompt. Preflight does `GET https://openrouter.ai/api/v1/key` and blocks the run if the key has no credit limit (unless `--allow-unlimited-openrouter-key`).

Hardcoded fallback when the picker is skipped (`--no-prompt` + no `defaults.toml`): `openrouter/deepseek/deepseek-v4-flash` ([models.py:95-96](../contremaitre/models.py#L95-L96)). That default requires an OpenRouter key; non-interactive operators on free tier should set `.contremaitre/defaults.toml` to a Zen slug.

Containers see `OPENROUTER_API_KEY` only when set on the host. The opencode binary reads the key when invoking an OpenRouter model; for Zen models the key is unused. Provider-side spend caps remain the real guardrail — the `--max-cost-usd` flag is a *recorded-cost* watcher, not a hard budget enforcer.

## Module map

Every `.py` under [contremaitre/](../contremaitre/). One line each — the code itself is the long form.

- [`actors.py`](../contremaitre/actors.py) — `FakeActorRunner` + `OpencodeActorRunner`. Opencode containers run detached with role labels; output streamed via `docker logs -f`, exit awaited via `docker wait`.
- [`checks.py`](../contremaitre/checks.py) — `--check-cmd` runner. OPENCODE mode: sidecar container with the run's worktree + deps volume, 600s timeout. FAKE mode: runs on the host.
- [`cli.py`](../contremaitre/cli.py) — argparse, subcommand dispatch, auto-derived clone cache at `~/.cache/contremaitre/<host>-<owner>-<repo>/`, launch-screen banners + pickers, image staleness rebuild (compares `contremaitre.dockerfile-sha256` label).
- [`cli_review_extra.py`](../contremaitre/cli_review_extra.py) — utility for re-judging a finished run with a different CLI reviewer.
- [`cli_reviewer.py`](../contremaitre/cli_reviewer.py) — post-publish CLI reviewer: detection, prompt assembly, `claude` / `codex` subprocess, API-key scrubbing, `gh pr comment` posting, verdict + model extraction, H3 metadata header.
- [`costs.py`](../contremaitre/costs.py) — recorded-cost extraction from JSONL streams; provider-side limits remain the real guardrail.
- [`defaults.py`](../contremaitre/defaults.py) — operator picker prefills from `.contremaitre/defaults.toml` (cwd-local) or XDG fallback. Hand-edited TOML; missing or malformed → empty defaults.
- [`diffscan.py`](../contremaitre/diffscan.py) — deterministic forbidden-path scanner against the working diff.
- [`envfile.py`](../contremaitre/envfile.py) — dependency-free `.env` loader; shell env wins, never overwritten.
- [`eval.py`](../contremaitre/eval.py) — v0 regression canary against `golden_cases/<id>/`. Subprocess-invokes `contremaitre run --actor opencode` so the production launch path is canaried as-is. Extracts a two-layer scorecard (headline + diagnostic) from artifacts the orchestrator already writes, aggregates n samples into a cell, compares against the (case, config) baseline. Generalizable methodology principles: [golden_cases/README.md](../golden_cases/README.md#methodology-notes).
- [`evaluator.py`](../contremaitre/evaluator.py) — gate-first PR-eval writer + non-blocking flow-use observability. `executable_confidence` is `null` (not `0.0`) when no `--check-cmd` is configured.
- [`events.py`](../contremaitre/events.py) — guardrail-event name constants. Single source of truth so writer + reader stay aligned at import time.
- [`extract.py`](../contremaitre/extract.py) — post-run artifact extraction: subagent markdown files (one per `task` tool-use), files written via `write` / `edit` / `apply_patch`, edit-accumulation `.edits.md` files, scaffold salvage.
- [`fake_actor.py`](../contremaitre/fake_actor.py) — deterministic fake agent/SIM for fixture smoke runs.
- [`fixture.py`](../contremaitre/fixture.py) — local fixture repo creator for smoke tests.
- [`flow_use.py`](../contremaitre/flow_use.py) — agent + SIM tool-use observability: `time_to_settled_design`, `self_verified`, `grilling_exchanges`, `impl_turns`, `sim_useful_call_ratio`, `runtime_install_required`.
- [`git_utils.py`](../contremaitre/git_utils.py) — logged git command wrapper. Every invocation appended to `git_log.jsonl` for forensic audit.
- [`jsonlog.py`](../contremaitre/jsonlog.py) — append-only JSONL + JSON-write helpers.
- [`manifest.py`](../contremaitre/manifest.py) — provenance manifest: model IDs, image digest, dockerfile-sha256, skills-lock hash, prompt hashes, contremaitre git SHA + dirty flag, python + contremaitre versions. Tolerates missing tools (returns `None`, never raises). `manifest_digest()` hashes the fields that define "the system under test".
- [`model_family.py`](../contremaitre/model_family.py) — coarse family classification (deepseek / qwen / glm / anthropic / openai / nemotron / minimax / etc.) for picker suggestions and TUI labels.
- [`models.py`](../contremaitre/models.py) — `State`, `ReviewVerdict`, `CliReviewVerdict`, `TerminalVerdict`, `ActorMode`, `PublishMode` enums; `RunConfig`, `RunPaths`, `Caps`, `DepsVolume`, `ParsedVerdict`, `RunResult` dataclasses. The stable seam between CLI, orchestrator, and actors.
- [`orchestrator.py`](../contremaitre/orchestrator.py) — state machine, caps, worktree lifecycle, WORK loop, review loop, host-side commit (with SETTLED-derived title + body), publication gate, label-driven cleanup, SIGTERM emergency-flush, post-publish CLI review hook.
- [`paths.py`](../contremaitre/paths.py) — slug validation, run-id generation, contained-path builder (prevents escape outside `run_dir`).
- [`preflight.py`](../contremaitre/preflight.py) — operational checks for live opencode runs (see above).
- [`prompts/`](../contremaitre/prompts/) — `initial_prompt.md` (agent's first turn), `sim_tooled_persona.md` (SIM's first turn), `sim_review_prompt.md` (single-shot review), `cli_reviewer_prompt.md` (post-publish review). Markdown is the source; `prompts/__init__.py` loads them.
- [`publisher.py`](../contremaitre/publisher.py) — publication boundary: `StubPublisher` (dry-run) vs `GhPublisher` (real `gh pr create --draft`). PR title + body derived from `.contremaitre/SETTLED_DESIGN.md` + SIM verdict summary; `--pr-title` / `--pr-body` override.
- [`runtime_image.py`](../contremaitre/runtime_image.py) — lockhash-keyed deps caching (see below).
- [`tui.py`](../contremaitre/tui.py) — read-only Textual TUI tailing JSONL artifacts. 7-phase footer (init → exploring → grilling → implementing → reviewing → cli_review → done) + per-reviewer status glyphs + warning tokens + verdict badge.
- [`verdicts.py`](../contremaitre/verdicts.py) — strict SIM verdict parser (fence-tolerant JSON extraction) and `diff_hash()` used by the diff-hash gate.
- [`viewer/`](../contremaitre/viewer/) — single-file run viewer (`viewer.html`) over the JSONL artifacts (transcript, timeline, sub-agents, written files, guardrail events, eval reports). Built by the orchestrator's `finally` so it lands on success and failure. Companion [`viewer/index.py`](../contremaitre/viewer/index.py) scans a runs root for `viewer.html` files and emits `index.html` — one summary card per run (verdict, models, PR link, cost, duration), newest first — rebuilt at the end of every run so the dashboard is always current.

## Artifact contract

Every opencode-mode run writes to `<runs_root>/<run-id>/`. The control plane is additive — readers must use `.get()`-style access. Paths are registered in [models.RunPaths](../contremaitre/models.py#L145-L181).

### Conversation streams

- `initial_prompt.txt` — agent's turn-1 message
- `raw_export.jsonl` — agent JSONL stream
- `sim_raw_export.jsonl` — SIM JSONL stream
- `extra_reviewer_raw_export.jsonl` — extra-reviewer stream (only when `--extra-reviewer-model` is set)
- `claude_review_raw_export.jsonl` *or* `codex_review_raw_export.jsonl` — post-publish CLI review (only when `--cli-reviewer` is set; whichever tool ran). With `--cli-reviewer both` both files are present.
- `codex_final_message.md` — codex-only; source of `codex_review.md`
- `<tool>_review.md` — posted PR-comment body for the CLI review, with H3 metadata header

### Transcript + state

- `transcript.md`
- `timeline.jsonl` — state transitions + turn markers
- `trajectory.json` — final state sequence
- `stats.json` — run summary
- `git_log.jsonl` — every git command invoked
- `test_runs.jsonl` — `--check-cmd` results
- `review_cycles.jsonl` — one entry per review round
- `worktree_state.jsonl` — git-status snapshots
- `guardrail_events.jsonl` — per-turn lifecycle + `check_started`/`_completed`, `host_commit_created`, `review_verdict`, `hard_gates_checked`, `published`/`publication_blocked`, `cli_review_started`/`_completed`/`_failed`, `worktree_removed`
- `recoveries.jsonl` — sqlite-recovery / SIGTERM-emergency events
- `pr.json` — publication outcome

### Extracted from agent activity

- `subagents/agent_NN_<slug>.md` — one per `task` tool-use; populated by `extract.py` in the orchestrator's `finally`
- `extracted_files/<host_name>` — every file the agent wrote via `write` / `edit` / `apply_patch`
- `extracted_files/<host_name>.edits.md` — accumulated edit history when the file went through `edit` ([extract.py:100-111](../contremaitre/extract.py#L100-L111))
- `extracted_files/.contremaitre__<name>` — worktree-scaffold salvage (the `.contremaitre/` markers the agent wrote) ([extract.py:184](../contremaitre/extract.py#L184))

### Eval (gate-first scorecard)

- `eval/pr_eval.json`, `eval/pr_eval.md`
- `eval/checks_report.json`
- `eval/settled_diff_report.json`, `eval/architecture_delta_report.json` — currently `PENDING` placeholders
- `eval/trajectory_report.json`
- `eval/flow_use.json` — tool-use observability (`time_to_settled_design`, `self_verified`, `grilling_exchanges`, `impl_turns`, `sim_useful_call_ratio`, …)
- `eval/judge_attempts.jsonl` — path is registered in [models.RunPaths](../contremaitre/models.py#L145-L181) but **not currently written**; reserved for future per-attempt verdict logging (cross-judge replay, malformed-retry audit). Treat as a known gap.
- `eval/cost_report.json`
- `eval/preflight_report.json`
- `eval/canary.json` — only when driven by `contremaitre eval run`

### Provenance + viewer

- `run_config.json` — provenance manifest ([manifest.py](../contremaitre/manifest.py))
- `viewer.html` — self-contained run viewer (built in orchestrator's `finally`; lands on success and failure)

The runs root also gets a top-level **`index.html`** rebuilt on each run, summarising every run under it ([viewer/index.py](../contremaitre/viewer/index.py)).

## Runtime image

The base image ([`contremaitre/Dockerfile`](../contremaitre/Dockerfile)) is generic opencode-in-Docker — no target codebase baked in. Layers:

- `node:24-bookworm-slim` plus `git`, `curl`, `jq`, `python3` + venv + pip.
- [`uv`](https://docs.astral.sh/uv/) at `/root/.local/bin/uv` and [`poetry`](https://python-poetry.org/) via pip — used by `runtime_image.ensure_deps_volume`'s install one-shots, and by agents running `uv run …` / `poetry run …` against the worktree.
- [`opencode`](https://opencode.ai/) at `/root/.opencode/bin/opencode` — the actor binary the host invokes.
- [`mattpocock/skills`](https://github.com/mattpocock/skills) installed globally via `npx -y skills@latest add … --all --global`, so `/improve-codebase-architecture` is on-PATH for opencode regardless of which target repo is mounted.

The image is built with a `contremaitre.dockerfile-sha256=<sha>` label, so `_ensure_default_image_built` in [cli.py](../contremaitre/cli.py) detects Dockerfile drift and auto-rebuilds before the next run.

**Variants** chain `FROM contremaitre-agent:latest` and add their toolchain:

- [`Dockerfile.rust`](../contremaitre/Dockerfile.rust) — `rustup` stable, minimal profile, docs stripped. Build with `contremaitre image build --variant rust` (tags `contremaitre-agent-rust:latest`); use via `--docker-image contremaitre-agent-rust:latest` or the `just rust` preset. Crate deps download at check time over `--allow-open-egress`; a `CARGO_HOME` volume cache is feasible but unimplemented.
- [`Dockerfile.go`](../contremaitre/Dockerfile.go) — Go `1.23.4` toolchain at `/usr/local/go`. Build with `contremaitre image build --variant go` (tags `contremaitre-agent-go:latest`). Module deps land in the lockhash-keyed `contremaitre-deps-<project>-go-sum-<digest>` volume; `GOPATH` points at it.

Each variant runs a smoke check (`rustc --version`, `go version`) at build time so a broken install fails fast.

## Deps caching

`runtime_image.py`. The agent shouldn't pay an `npm ci` / `uv sync` tax every run; lockfile-keyed volumes amortise it.

**Lockfile → ecosystem matrix:**

| Lockfile | Install command | Mount path | Runtime env |
|---|---|---|---|
| `package-lock.json` | `npm ci --no-audit --no-fund` | `node_modules` | — |
| `pnpm-lock.yaml` | `corepack pnpm install --frozen-lockfile` | `node_modules` | — |
| `yarn.lock` | `yarn install --frozen-lockfile --non-interactive` | `node_modules` | — |
| `poetry.lock` | `POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --no-root` | `.venv` | `VIRTUAL_ENV=/app/.venv` |
| `uv.lock` | `uv sync --frozen --no-install-project` | `.venv` | `VIRTUAL_ENV=/app/.venv` |
| `requirements.lock` | `uv venv .venv && uv pip install --no-deps -r requirements.lock` | `.venv` | `VIRTUAL_ENV=/app/.venv` |
| `Cargo.lock` | `cargo fetch` | `.cargo-cache` | `CARGO_HOME=/app/.cargo-cache` |
| `go.sum` | `go mod download` | `.go-mod-cache` | `GOPATH=/app/.go-mod-cache` |

**Pristine volume** `contremaitre-deps-<project>-<lockfile>-<digest>`, labeled `contremaitre.purpose=deps-cache` + `contremaitre.project=<project>`. Populated once per (project, lockfile, sha-256[:12]) by a one-shot install container. Install mount path *equals* the runtime mount path so embedded paths (e.g. uv's `#!/app/.venv/bin/python` shebangs) resolve later.

**Per-run volume** `contremaitre-run-<run-id>-deps`, labeled `contremaitre.purpose=deps-run` + `contremaitre.run-id=<run-id>`. Cloned from the pristine via a one-shot `cp -a /src/. /dst/` (typical 5–15s). Mounted RW at `/app/<mount_path>` in the agent / SIM / check containers; the agent can freely install into it without leaking writes into the pristine or into the next run.

`_prune_stale_deps_volumes` is scoped by `<project>-<lockfile>` so projects don't evict each other's caches when their lockfiles bump. Deps install failures raise `DepsInstallError`; the orchestrator hard-fails the run rather than silently degrade.

## Lifecycle / cleanup

Per opencode-mode run, the orchestrator owns these external artifacts beyond the run directory:

- **Worktree** `/tmp/contremaitre-<run-id>/` — removed by `_cleanup_worktree` in `finally`.
- **Detached containers** labeled `contremaitre.run-id=<id>` — agent / SIM / review / check / deps-install / deps-clone. `--rm` for one-shot turns, explicit `docker rm -f` after `docker wait` for streamed-log ones. `_stop_run_containers` runs in `finally` and on SIGTERM, scans by label, and stops anything still alive.
- **Per-run deps volume** `contremaitre-run-<run-id>-deps` — removed by `_remove_run_volumes` in `_cleanup_worktree`'s `finally`.
- **Pristine deps volumes** — kept across runs by design (avoids the 60–90s install re-cost). Same-project + same-lockfile-kind volumes with stale digests are pruned automatically after a fresh install lands.
- **Local clone cache** `~/.cache/contremaitre/<host>-<owner>-<repo>/` — kept across runs; `git fetch origin <base>` for freshness on every run.
- **opencode state dirs** `opencode-{agent,sim,review}-state/` under the run dir — kept on purpose; `_recover_text_from_sqlite` reads them when opencode silent-stalls.

If the parent is SIGKILL'd before cleanup, label-tagged containers, per-run volumes, and worktrees can survive. `contremaitre cleanup` sweeps them:

```bash
contremaitre cleanup --dry-run   # see what would be removed
contremaitre cleanup             # containers + worktrees + dangling images
contremaitre cleanup --deps      # also nuke lockhash-keyed pristine volumes
contremaitre cleanup --repos     # also nuke ~/.cache/contremaitre/ clones
```

`contremaitre image build` runs `docker image prune -f` after a successful build so rebuilds with the same tag don't accumulate `<none>:<none>` orphans.

## Recoveries

`recoveries.jsonl` records degradations the orchestrator handled without aborting. Each entry is mirrored into `guardrail_events.jsonl` as `recovery_<kind>` so a single tail catches both surfaces.

- **`sqlite_recovery_silent_stall`** — opencode occasionally persists message parts to its SQLite store (`<run_dir>/opencode-{agent,sim,review}-state/opencode.db`) but doesn't flush the corresponding `text` event to its `--format=json` stdout before the docker process exits. The data is intact in the DB. [`_recover_text_from_sqlite`](../contremaitre/actors.py#L839) reads the latest message back (`mode=ro`, 2s timeout) and the run continues with the recovered text. The state dirs are kept on purpose for exactly this path.
- **`sigterm_emergency_write`** — host process receives SIGTERM mid-run. The handler ([orchestrator.py:151](../contremaitre/orchestrator.py#L151)) writes a `FAILED_INFRA` terminal with `reason="killed_via_sigterm"`, runs `_stop_run_containers` to stop label-tagged containers, then exits. Partial artifacts are preserved (raw exports, transcript fragments, any `viewer.html` that had landed) so the run dir stays browsable.
- **`extract_failed`** — `extract.py` raised while harvesting `task` sub-agent files or `extracted_files/*`. Logged; the rest of the artifact contract still lands.
- **`viewer_build_failed`** — `viewer.build_viewer()` raised. Logged; other artifacts unaffected. Rebuildable later with `contremaitre viewer <run-dir>`.
- **`extra_reviewer_unavailable`** — extra-reviewer container died or returned a malformed verdict that survived `--malformed-verdict-retries`. The run continues with the primary SIM verdict alone; `cross_family_agreement_rate` records the dropout instead of an agreement.

None of these abort the run. The orchestrator's `finally` writes terminal state, runs the viewer build, and sweeps containers regardless — so partial information is always recoverable.

## Scaffold semantics

`.contremaitre/IMPLEMENTATION_COMPLETE` and `.contremaitre/SETTLED_DESIGN.md` are **Contremaitre scaffolds, not part of the skill**. The skill prescribes neither.

- `IMPLEMENTATION_COMPLETE` ends the WORK loop; the agent is told (via [initial_prompt.md](../contremaitre/prompts/initial_prompt.md)) to write it only after SETTLED is locked, the implementation matches SETTLED, and both the test suite + the project's CI lint/format gate pass against the changed files.
- `SETTLED_DESIGN.md` is the design handoff artifact the REVIEW pass reads (from a pre-staged copy at `/review/SETTLED_DESIGN.md`) and the source of the host's commit title + body and the PR title + body.
- `architecture-review.html` is the skill's HTML candidate cards. The orchestrator does **not** check for it (telemetry only) — the SIM reads it via the `:ro` mount.

`.contremaitre/*` is **excluded from the committed diff** via `git add -- . ':(exclude).contremaitre'`. The files stay in the worktree across WORK rounds so the SIM keeps reading SETTLED after CHANGES_REQUESTED loop-back, but never enter the published commit or PR — the SETTLED content is already in the commit body + PR description.

The clean-worktree hard gate filters via [`_only_contremaitre_changes`](../contremaitre/orchestrator.py#L1259). The filter tolerates more than just `.contremaitre/`: it also passes `opencode.json`, `dist/`, `build/`, `out/`, `.next/`, and `__pycache__/`. The intent is that conventional build output + opencode's emitted config file shouldn't block publication if they happen to land in the worktree.

---

## CLI Reference

The dozen most-used flags live in [README.md](../README.md#flags-worth-knowing). Below is the full surface.

### Subcommands

| Subcommand | Purpose |
|---|---|
| `run` | Run the WORK + REVIEW loop with optional PR publication. |
| `doctor` | Validate live-run operational prerequisites without starting a run. |
| `fixture init <path>` | Create a tiny local git repo for fake-mode smoke runs. |
| `image build [--variant base\|rust\|go]` | Build the runtime Docker image. |
| `cleanup [--deps] [--repos]` | Prune stale containers, worktrees, and (opt-in) deps volumes + clone caches. |
| `tui run -- <run-args>` | Spawn `contremaitre run` and attach the live TUI. |
| `tui attach <run-dir>` | Read-only TUI over a finished run. |
| `viewer <run-dir> [--open]` | Rebuild `viewer.html` for an existing run. |
| `index [<runs-root>] [--open]` | Build `index.html` over all runs under a root. |
| `eval {run\|check\|compare\|promote\|all\|show} <case_id> [--config <name>] [--n 3]` | v0 regression canary. See [golden_cases/README.md](../golden_cases/README.md). |

### Shared flags (`run` + `doctor`)

| Flag | Default | Purpose |
|---|---|---|
| `--base BASE` | *(required)* | Branch the worktree is sourced from + PR target. Fetched as `origin/<base>`. |
| `--repo-cache PATH` | `~/.cache/contremaitre/<host>-<owner>-<repo>/` | Override the auto-derived clone cache. |
| `--runs-root PATH` | `.contremaitre/runs` | Where per-run directories land. |
| `--docker-image NAME` | `contremaitre-agent:latest` | Runtime image. |
| `--opencode-config PATH` | (synthesized from `--agent-model`) | Path to opencode.json. |
| `--openrouter-env-var NAME` | `OPENROUTER_API_KEY` | Env-var name holding the OpenRouter key. |
| `--docker-network NAME` | — | Docker `--network` for opencode containers. |
| `--http-proxy URL` / `--https-proxy URL` / `--no-proxy LIST` | — | Container proxy settings (host env is not forwarded). |
| `--allow-open-egress` | False | Accept unrestricted egress (otherwise a network/proxy is required). |
| `--skip-openrouter-key-check` | False | Don't query OpenRouter key metadata. |
| `--allow-unlimited-openrouter-key` | False | Accept a key with no provider-side credit limit. |
| `--openrouter-key-url URL` | `https://openrouter.ai/api/v1/key` | OpenRouter key-metadata endpoint. |
| `--max-cost-usd FLOAT` | `30.0` | Orchestrator cost cap (on top of OpenRouter's daily limit). |

### `run`-specific flags

| Flag | Default | Purpose |
|---|---|---|
| `--fork URL` | — | Push remote for the run branch. Required for `--publish-mode gh`. |
| `--upstream URL` | — | Canonical (read-only) remote, mounted as `upstream`. Preferred over `--fork` for cloning when set. |
| `--gh-repo OWNER/REPO` | — | Override the `gh pr create --repo` target (cross-fork PRs). |
| `--branch-prefix STR` | `refactor` | Prefix for generated branch names. |
| `--agent-model SLUG` | `openrouter/deepseek/deepseek-v4-flash` | OpenRouter / OpenCode model slug for the agent (ignored in `--actor fake`). |
| `--sim-model SLUG` | `openrouter/deepseek/deepseek-v4-flash` | Model for the SIM. Independent default; pickable separately from `--agent-model`. |
| `--extra-reviewer-model SLUG` | — | Optional second SIM; both must APPROVE. Cross-family pick gives cheap bias-mitigation signal. |
| `--cli-reviewer auto\|codex\|claude\|both\|none` | `auto` | Post-publish CLI review tool. `auto` detects + prompts on TTY; `both` runs claude first then codex, two PR comments; `none` skips. |
| `--actor fake\|opencode` | `fake` | Fake actor for smoke runs; opencode for live runs. |
| `--run-slug STR` | `run` | Identifier for `<runs-root>/<run-id>/` naming. |
| `--check-cmd CMD` | — | Executable check command, repeatable; blocks publication on failure. |
| `--publish-mode stub\|gh` | `stub` | `stub` dry-runs everything except `git push` / `gh pr create`. |
| `-y` / `--yes` | False | Skip the pre-launch Y/n confirmation. Auto-implied in non-TTY. |
| `--no-prompt` | False | Skip the interactive pickers entirely (uses `defaults.toml` or hardcoded fallbacks). Implies `--yes`. |
| `--keep-worktree` | False | Preserve the worktree after the run. |
| `--simulate-drift-after-approval` | False | Inject post-APPROVED diff drift to exercise the diff-hash gate. |
| `--container-user UID:GID` | — | Docker `--user` value. |
| `--skip-preflight` | False | Bypass operational preflight checks. |
| `--agent-timeout-seconds INT` | `1800` | Per-agent-turn timeout. |
| `--sim-timeout-seconds INT` | `1500` | Per-SIM-turn timeout. |
| `--opencode-stdout-stall-seconds INT` | `300` | Kill opencode if its stdout hasn't grown for this many seconds. `0` to disable. |
| `--pr-title STR` / `--pr-body STR` | (derived from SETTLED) | Override PR title / body. |
| `--max-turns INT` | `30` | Per-actor turn budget. |
| `--max-wall-minutes INT` | `180` | Wall-clock budget. |
| `--no-progress-turns INT` | `5` | Stagnation threshold; aborts on no marker progress. |
| `--malformed-verdict-retries INT` | `2` | Retries for an unparseable SIM verdict. |
| `--max-review-rounds INT` | `3` | Max REVIEW → WORK loops before `NO_PR_CHANGES_REQUESTED`. |
| `--sim-scenario {approved,changes_requested,needs_human,malformed,malformed_then_approved}` | `approved` | Fake-SIM behavior (ignored in `--actor opencode`). |
| `--extra-reviewer-scenario {…}` | `approved` | Fake extra-reviewer behavior. |
| `--agent-scenario {normal,forbidden_path,no_impl_complete}` | `normal` | Fake-agent behavior. |

### `cleanup` flags

| Flag | Default | Purpose |
|---|---|---|
| `--runs-root PATH` | `.contremaitre/runs` | Runs root used to decide which container labels are still live. |
| `--dry-run` | False | Report what would be removed without touching. |
| `--skip-images` | False | Skip dangling-image prune. |
| `--deps` | False | Also remove lockhash-keyed pristine deps volumes. |
| `--repos` | False | Also remove `~/.cache/contremaitre/<slug>/` clones. |

### `image build` flags

| Flag | Default | Purpose |
|---|---|---|
| `--variant base\|rust\|go` | `base` | Image variant; `rust` / `go` chain `FROM contremaitre-agent:latest` and add their toolchain. |
| `--image-name NAME` | (derived) | Override output tag. |
| `--dockerfile PATH` | (derived) | Override Dockerfile path. |
| `--no-cache` | False | Force fresh layers. |

### `eval` flags (shared across `run` / `check` / `compare` / `promote` / `all` / `show`)

| Flag | Default | Purpose |
|---|---|---|
| `--config NAME` | `default` | Config name under `golden_cases/<case_id>/configs/`. |
| `--n INT` | `3` | Number of runs to aggregate (n=3 is the floor; lower values are forbidden by `promote`). |
| `--runs-root PATH` | `.contremaitre/runs` | Runs root. |
| `--json` (compare only) | False | Emit raw JSON instead of the pretty scorecard. |
