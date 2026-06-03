# Contremaitre

Deterministic orchestration shell that runs Matt Pocock's [`improve-codebase-architecture`](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture) skill end-to-end against a target repository and produces a draft PR.

The agent and SIM live inside opencode-in-Docker containers. Git, GitHub, diff-scan, and cap enforcement stay host-owned — the agent has no outbound credentials.

**Status**: v0.1.2 — pre-1.0, expect rough edges.

## Quickstart

```bash
just tui-run main git@github.com:<you>/<target-repo>.git
```

The TUI shows the agent and SIM working live, side-by-side. When the run finishes successfully, a draft PR opens on your fork.

**Prerequisites**

- [`just`](https://github.com/casey/just) — `brew install just`
- Docker (the runtime image builds itself on first run, ~3 min)
- [`gh`](https://cli.github.com/) authenticated (`gh auth login`)
- Python 3.11+, [`uv`](https://docs.astral.sh/uv/) — `uv sync --extra tui` for the TUI; `uv sync --group dev` to run tests
The interactive launcher confirms with `Continue? [Y/n]` before each run; pass `-y` to skip or `--no-prompt` for full automation. See [docs/control-plane.md#launch-sequence](docs/control-plane.md#launch-sequence) for the exact flow.

## Models

By default the picker offers free [OpenCode Zen](https://opencode.ai/docs/zen/) models served by OpenCode (slugs like `opencode/big-pickle`, `opencode/deepseek-v4-flash-free`) — **no API key required**. The catalog is fetched live at launch.

To use paid OpenRouter models, set `OPENROUTER_API_KEY` in `.env` and paste any OpenRouter slug at the picker prompt (e.g. `openrouter/anthropic/claude-sonnet-4.6`, `openrouter/qwen/qwen3-coder-plus`). Preflight verifies the key has a provider-side credit limit and warns on unlimited keys. See [`.env.example`](.env.example).

Skip the picker entirely with `--no-prompt` (uses [`.contremaitre/defaults.toml`](#saved-picker-defaults)) or pass `--agent-model` / `--sim-model` explicitly.

## How it works

Inside one multi-turn opencode session the agent runs the skill end-to-end:

1. **Explore + propose** — agent reads the codebase, writes `.contremaitre/architecture-review.html` (candidate cards), then asks the SIM which to deepen.
2. **Grill** — N turns of agent ↔ SIM exchanges. SIM cites constraints from the code (`read` / `glob` / `grep` only); the agent defends or revises.
3. **Settle** — agent writes `.contremaitre/SETTLED_DESIGN.md` — the design handoff that REVIEW reads.
4. **Implement** — agent edits files; SIM watches each diff for drift from SETTLED. The project's tests + CI lint/format gate run scoped to changed files.
5. **Marker** — agent writes `.contremaitre/IMPLEMENTATION_COMPLETE`, ending WORK.

A fresh reviewer container then reads the diff + SETTLED and emits a strict-JSON verdict. `APPROVED` → host hard gates (diff scan, hash match, clean worktree, plus any `--check-cmd`) → `gh pr create --draft`. `CHANGES_REQUESTED` clears the marker and loops back to WORK (up to 3 rounds). If the agent never writes the marker, or any hard gate fails, the run terminates without a PR.

State machine, all five terminal verdicts, and the artifact contract: [docs/control-plane.md](docs/control-plane.md).

## Configuration

### Flags worth knowing

The launcher takes the same flags whether you call `just tui-run …` or `python3 -m contremaitre run …` directly. The most common ones:

- `--base main` *(required)* — branch the worktree is sourced from and the PR target.
- `--fork git@github.com:<you>/<repo>.git` *(required for real PRs)* — push remote for the run branch.
- `--upstream …` + `--gh-repo <owner>/<repo>` — when `--fork` is *your* fork and you want the PR opened on the upstream repo.
- `--agent-model` / `--sim-model` — OpenRouter model slug, or an OpenCode Zen model. Omit to pick interactively.
- `--cli-reviewer auto|codex|claude|both|none` — after the draft PR opens, run a code review on your host via `claude -p` or `codex exec` and post the result as a PR comment. Uses your Claude Pro/Max or ChatGPT Plus subscription (NOT API). `auto` detects what's installed; `both` runs claude then codex (two comments); `none` skips.
- `--check-cmd "<command>"` *(repeatable)* — fast deterministic check the post-implementation worktree must pass before publishing (e.g. `"npx tsc --noEmit"`, `"uv run pytest -q"`).
- `--publish-mode stub|gh` — `stub` (default) is a full dry-run with no `git push` or `gh pr create`; `gh` opens the draft PR.
- `--max-turns 30` / `--max-wall-minutes 180` / `--max-cost-usd 30` — per-run budgets; the orchestrator aborts cleanly on cap.
- `--allow-open-egress` — required if you haven't configured `--docker-network` / `--http-proxy` / `--https-proxy`.
- `-y` / `--yes` — skip the confirmation prompt (CI / scripts). `--no-prompt` also skips the interactive pickers.

Headless mode is the same command minus the TUI prefix:

```bash
GITHUB_TOKEN=$(gh auth token) python3 -m contremaitre run --base main --fork … --publish-mode gh
```

The full flag reference lives in [docs/control-plane.md#cli-reference](docs/control-plane.md#cli-reference), or run `python3 -m contremaitre run --help`.

### Saved picker defaults

Hand-edit `.contremaitre/defaults.toml` (cwd-local; XDG fallback at `$XDG_CONFIG_HOME/contremaitre/defaults.toml`) to seed the launch-screen pickers. The file is gitignored; per-run CLI flags always win.

```toml
agent_model = "opencode/big-pickle"
sim_model = "opencode/big-pickle"
extra_reviewer_model = "opencode/nemotron-3-super-free"  # or "skip" to flip Enter to skip
cli_reviewer = "both"                                    # auto | codex | claude | both | none
```

`--no-prompt` skips the pickers entirely and uses these values verbatim.

### Caveats

- **`--cli-reviewer` is not free.** It calls `claude -p` or `codex exec` on your machine against *your subscription* (Claude Pro/Max, ChatGPT Plus). Each review burns your usage allowance. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are scrubbed from the subprocess env so it can't silently fall through to billed API.
- **Free models are rate-limited.** OpenCode Zen's free tier is generous but bounded; long runs or many evals eventually hit a daily cap that surfaces as `QUOTA_EXHAUSTED`.
- **Paid OpenRouter runs cost real money.** A single `n=3` eval cell on `openrouter/anthropic/claude-sonnet-4.6` runs ~$7–10. The default eval config uses free Zen models for a reason.

## Other commands

- `contremaitre doctor` — preflight check without spawning a run (target/base, Docker, opencode binary, `:ro` mount, network posture, OpenRouter key bounds).
- `contremaitre cleanup [--deps] [--repos]` — sweep label-tagged containers, worktrees, dangling images; opt-in to deps volumes and the clone cache.
- `contremaitre viewer <run-dir> [--open]` — rebuild the per-run `viewer.html`: transcript, timeline, sub-agents, written files, guardrail events, eval reports. Self-contained — no server needed.
- `contremaitre index [<runs-root>] [--open]` — rebuild `index.html` across every run under the root: one summary card per run (verdict, models, PR link, cost, duration), newest first, each linking to its viewer. Auto-rebuilt at the end of every run.
- `contremaitre tui attach <run-dir>` — read-only TUI over a finished run.
- `contremaitre eval {run|show|compare|promote|all} <case_id>` — v0 regression canary. See [golden_cases/README.md](golden_cases/README.md).
- `contremaitre image build [--variant base|rust|go]` — build the runtime image. The default-variant image auto-builds on first opencode-mode run and auto-rebuilds when the Dockerfile changes.
- `contremaitre fixture init <path>` — create a tiny git repo for fake-actor smoke runs.

For controlled egress (instead of `--allow-open-egress`), pass `--docker-network`, `--http-proxy`, `--https-proxy`, `--no-proxy`. Ambient proxy env vars are *not* forwarded into containers — only what you pass explicitly.

## When something goes wrong

- **Browse the run.** Open `.contremaitre/runs/<run-id>/viewer.html` — self-contained, no server. If it's missing, rebuild it: `contremaitre viewer .contremaitre/runs/<run-id>`.
- **Read the event log.** `guardrail_events.jsonl` in the run dir is the structured timeline of every state transition, cap trip, gate result, recovery, and verdict. Pair with `stats.json` (`reason` field disambiguates same-verdict failure modes).
- **Sanity-check the environment.** `contremaitre doctor --base main --fork …` runs the same preflight as `run` (Docker daemon + image, opencode binary, `:ro` mount enforcement, network posture, OpenRouter key bounds) without spawning a run.
- **Sweep leftovers.** A SIGKILL'd parent can leave label-tagged containers and worktrees behind. `contremaitre cleanup --dry-run` shows what's stale; `contremaitre cleanup` removes them. Add `--deps` / `--repos` to also clear cross-run caches.

## Further reading

- [docs/control-plane.md](docs/control-plane.md) — implementation map: state machine, host-owned boundaries, hard gates, artifact contract, full CLI reference, module map.
- [golden_cases/README.md](golden_cases/README.md) — eval canary: case/config schema, headline panels, single-variable rule, methodology notes, how to add a case.
- [AGENTS.md](AGENTS.md) — conventions for coding agents modifying this repo.

## Contributing

PRs welcome. Conventions live in [AGENTS.md](AGENTS.md); run `uv run pytest` and `just lint` before opening one.

## License

Licensed under the MIT License — see [LICENSE](LICENSE).
