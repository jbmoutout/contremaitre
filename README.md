# Contremaitre

Deterministic orchestration shell that runs Matt Pocock's [`improve-codebase-architecture`](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture) skill end-to-end against a target repository and produces a draft PR — then drives an optional **CLI review-and-revise loop**: `codex` or `claude` on your subscription reviews the diff, comments on the PR, and sends the agent back to fix what it flags until the review passes and the host gates are clean.

The agent and SIM live inside per-run Docker containers. Each role runs one of two actor runtimes — **opencode** (an OpenRouter or free OpenCode Zen model) or a **subscription CLI** driven headless (`codex` on your ChatGPT plan, or `claude` on your Claude plan) — and the two can mix (a CLI agent + opencode SIM, or the reverse). Git, GitHub, diff-scan, and cap enforcement stay host-owned — the agent has no outbound git/GitHub credentials. A **claude** role also holds no provider credential (a host-side proxy injects it per request) and runs open egress; a **codex** role mounts a short-lived token and runs behind an allowlist egress lock by default (overridable with `--allow-open-egress`).

**Status**: v0.2.0 — pre-1.0, expect rough edges.

## Quickstart

```bash
make run BASE=main FORK=git@github.com:<you>/<target-repo>.git
```

The TUI shows the agent and SIM working live, side-by-side. When the run finishes successfully, a draft PR opens on your fork.

**Prerequisites**

- Docker (the runtime image builds itself on first run, ~3 min)
- [`gh`](https://cli.github.com/) authenticated (`gh auth login`)
- Python 3.11+, [`uv`](https://docs.astral.sh/uv/) — `uv sync --extra tui` for the TUI; `uv sync --group dev` to run tests
- `make` — already present on macOS/Linux

The interactive launcher confirms with `Continue? [Y/n]` before each run. See [docs/control-plane.md#launch-sequence](docs/control-plane.md#launch-sequence) for the exact flow.

## Models

By default the picker offers free [OpenCode Zen](https://opencode.ai/docs/zen/) models served by OpenCode (slugs like `opencode/big-pickle`, `opencode/deepseek-v4-flash-free`) — **no API key required**. The catalog is fetched live at launch.

To use paid OpenRouter models, set `OPENROUTER_API_KEY` in `.env` and paste any OpenRouter slug at the picker prompt (e.g. `openrouter/anthropic/claude-sonnet-4.6`, `openrouter/qwen/qwen3-coder-plus`). Preflight verifies the key has a provider-side credit limit and warns on unlimited keys. See [`.env.example`](.env.example).

Skip the picker by passing `--agent-model` / `--sim-model` explicitly (or setting `AGENT_MODEL` / `SIM_MODEL` in the Makefile). Run `make models` to see the current Zen catalog with live quota status.

### Codex / Claude (your subscription CLI)

Instead of opencode, a role can run a **frontier CLI headless on your subscription** — no API key, no per-token billing. Two tools are wired:

- **codex** on your ChatGPT plan (`--agent codex`): model + effort via `--codex-model` (default `gpt-5.5`) / `--codex-effort` (default `high`). Auth is a logged-in `codex` on the host (`~/.codex/auth.json`); preflight checks the token isn't about to expire (missing → `codex login`).
- **claude** on your Claude plan (`--agent claude`): model + effort via `--claude-model` (empty = your `~/.claude` account default) / `--claude-effort` (`low|medium|high|max`, default `high`). Auth never enters the container: a host-side proxy injects the token per request. The token resolves from `CLAUDE_CODE_OAUTH_TOKEN` (run `claude setup-token` — the pre-run check offers to do it and write `.env` for you), or, failing that, from your interactive `claude` login (macOS keychain / `~/.claude/.credentials.json`).
- **opencode** on a paid OpenRouter model: the pre-run check verifies `OPENROUTER_API_KEY` is set (add it to `.env`). Free OpenCode Zen models (`opencode/…`) need no key.

Select a role via `--agent claude` or `--agent codex` (or `AGENT=claude` / `AGENT=codex` in the Makefile). Mix with `--sim opencode` for a CLI agent + opencode SIM (or the reverse). Egress posture follows the credential: **claude** holds no in-container token (the host proxy injects it), so it runs **open egress** by default — free to install deps from PyPI/npm/GitHub. **codex** still mounts a short-lived token, so it's **locked by default** behind a providers-only allowlist proxy (OpenAI/OpenRouter/Zen); pass `--allow-open-egress` (or `ALLOW_OPEN_EGRESS=1`) to open it. Details: [docs/control-plane.md#cli-actor-codex--claude-auth--egress-lock](docs/control-plane.md#cli-actor-codex--claude-auth--egress-lock).

## How it works

Inside one multi-turn session — driven by an opencode model or a subscription CLI (codex/claude), same flow either way — the agent runs the skill end-to-end:

1. **Explore + propose** — agent reads the codebase, writes `.contremaitre/architecture-review.html` (candidate cards), then asks the SIM which to deepen.
2. **Grill** — N turns of agent ↔ SIM exchanges. SIM searches the codebase to verify claims and cites constraints by file/line; the agent defends or revises.
3. **Settle** — agent writes `.contremaitre/SETTLED_DESIGN.md` — the design handoff that REVIEW reads.
4. **Implement** — agent edits files; SIM watches each diff for drift from SETTLED. The project's tests + CI lint/format gate run scoped to changed files.
5. **Marker** — agent writes `.contremaitre/IMPLEMENTATION_COMPLETE`, ending WORK.

A fresh reviewer container then reads the diff + SETTLED and emits a strict-JSON verdict. `APPROVED` → host hard gates (diff scan, hash match, clean worktree, plus any `--check-cmd`) → `gh pr create --draft`. `CHANGES_REQUESTED` clears the marker and loops back to WORK (up to 3 rounds). If the agent never writes the marker, or any hard gate fails, the run terminates without a PR.

State machine, all six terminal verdicts, and the artifact contract: [docs/control-plane.md](docs/control-plane.md).

## Configuration

### Flags worth knowing

The launcher takes the same flags whether you call `make run …` or `python3 -m contremaitre run …` directly. The most common ones:

- `--base main` *(required)* — branch the worktree is sourced from and the PR target.
- `--fork git@github.com:<you>/<repo>.git` *(required for real PRs)* — push remote for the run branch.
- `--upstream …` + `--gh-repo <owner>/<repo>` — when `--fork` is *your* fork and you want the PR opened on the upstream repo.
- `--agent claude|codex|opencode|fake` — per-run **agent** runtime. `opencode` = a live model (see `--agent-model`); `claude` / `codex` = a subscription CLI headless; `fake` = smoke fixture.
- `--sim claude|codex|opencode` — override the **SIM** runtime, enabling a mixed run (e.g. `--agent codex --sim opencode`). Omit to mirror `--agent`.
- `--agent-model` / `--sim-model` — OpenRouter model slug, or an OpenCode Zen model, for an opencode role. Omit to pick interactively (TTY) or set in the Makefile; in headless opencode-agent runs, an omitted `--sim-model` mirrors `--agent-model`.
- `--codex-model` / `--codex-effort` — codex-native model (default `gpt-5.5`) and reasoning effort (default `high`) for a codex role.
- `--claude-model` / `--claude-effort` — claude model (empty = `~/.claude` account default) and effort (`low|medium|high|max`, default `high`) for a claude role.
- `--cli-reviewer auto|codex|claude|none` — after the draft PR opens, run a post-publish revision loop: a CLI reviewer (codex/claude) in a read-only container reviews the diff; the host posts the returned markdown as the PR comment. `LOOKS_GOOD` exits; `NEEDS_ATTENTION`/`MUST_FIX` sends the agent back to revise on the same branch, then the host re-runs the deterministic gates and pushes only if they pass. After `--max-cli-review-rounds` (default 3) without `LOOKS_GOOD`, the run ends `PR_NEEDS_HUMAN`. Uses your Claude Pro/Max or ChatGPT Plus subscription (NOT API); `auto` detects what's installed, `none` skips. Also projects the worst verdict as a `contremaitre/cli-review` commit status for branch protection. Full mechanics: [docs/control-plane.md](docs/control-plane.md).
- `--check-cmd "<command>"` *(repeatable)* — fast deterministic check the post-implementation worktree must pass before publishing (e.g. `"npx tsc --noEmit"`, `"uv run pytest -q"`). Also run once at startup on the agent's (locked) network as an offline-readiness probe: if the check can't pass with the cached deps under the egress lock, the run fails fast at INIT rather than after the agent burns turns on a missing build backend. With no `--check-cmd`, an ecosystem canary stands in for the uv family (a failing canary warns, doesn't abort).
- `--publish-mode stub|gh` — `stub` (default) is a full dry-run with no `git push` or `gh pr create`; `gh` opens the draft PR.
- `--max-turns 30` / `--max-wall-minutes 180` / `--max-cost-usd 30` — per-run budgets; the orchestrator aborts cleanly on cap. A cap exit keeps its worktree + session homes and writes a `resume.json` checkpoint so the run can be continued.
- `--continue <run-id>` — resume a run that exited by tripping a budget cap (wall / turns / no-progress / cost). It reattaches the kept worktree and the agent's CLI session and picks up where it left off with a **fresh** budget — re-pass the cap flags (e.g. `--max-wall-minutes 360`) to set the new ceiling. Target, models, and mounts come from the saved checkpoint; you only pass `--continue <run-id>` (and any new caps). CLI actor only (codex/claude); `--max-cost-usd` is cumulative across the original run.
- `--allow-open-egress` — accept open egress instead of configuring `--docker-network` / `--http-proxy` / `--https-proxy`. A **codex** role is egress-locked by default (its in-container token is exfiltratable); pass this to run it open (e.g. to install deps from PyPI/npm the provider-only allowlist blocks) — warned. A **claude** role already runs open (no in-container token), so the flag is a no-op for it.

Headless mode is the same command minus the TUI prefix:

```bash
GITHUB_TOKEN=$(gh auth token) python3 -m contremaitre run --base main --fork … --publish-mode gh
```

The full flag reference lives in [docs/control-plane.md#cli-reference](docs/control-plane.md#cli-reference), or run `python3 -m contremaitre run --help`.

### Caveats

- **Subscription CLI runs burn your plan, not an API bill.** Every codex/claude role *and* every `--cli-reviewer` round runs headless against your Claude Pro/Max or ChatGPT Plus subscription; revision rounds also burn agent quota. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are scrubbed from the reviewer container so it can't fall through to billed API. The TUI footer shows per-role cost/free/quota — codex's 5h rollout limit, Claude's statusLine percentages, or `claude ?` / `codex ?` when a counter is unavailable.
- **Free models are rate-limited.** OpenCode Zen's free tier is generous but bounded; long runs or many evals eventually hit a daily cap that surfaces as `QUOTA_EXHAUSTED`.
- **Paid OpenRouter runs cost real money.** A single `n=3` eval cell on `openrouter/anthropic/claude-sonnet-4.6` runs ~$7–10. The default eval config uses free Zen models for a reason.

## Other commands

- `contremaitre doctor` — preflight check without spawning a run (target/base, Docker, opencode binary, `:ro` mount, network posture, OpenRouter key bounds, CLI freshness vs npm).
- `contremaitre cleanup [--deps] [--repos]` — sweep label-tagged containers, worktrees, dangling images; opt-in to deps volumes and the clone cache.
- `contremaitre viewer <run-dir> [--open]` — rebuild the per-run `viewer.html`: transcript, timeline, sub-agents, written files, guardrail events, eval reports. Self-contained — no server needed.
- `contremaitre index [<runs-root>] [--open]` — rebuild `index.html` across every run under the root: one summary card per run (verdict, models, PR link, cost, duration), newest first, each linking to its viewer. Auto-rebuilt at the end of every run.
- `contremaitre tui attach <run-dir>` — read-only TUI over a finished run.
- `contremaitre eval {run|check|compare|promote|all|show} <case_id>` — v0 regression canary. See [golden_cases/README.md](golden_cases/README.md).
- `contremaitre image build [--variant base|rust|go]` — build the runtime image. The default-variant image auto-builds on first opencode-mode run and auto-rebuilds when the Dockerfile changes.
- `contremaitre fixture init <path>` — create a tiny git repo for fake-actor smoke runs.

For controlled egress on an **opencode** run (instead of `--allow-open-egress`), pass `--docker-network`, `--http-proxy`, `--https-proxy`, `--no-proxy`. Ambient proxy env vars are *not* forwarded into containers — only what you pass explicitly. A **codex** role or codex CLI reviewer needs none of this by default: it auto-provisions its own internal network + allowlist proxy (and refuses to run open unless you pass `--allow-open-egress`). A **claude** role or reviewer needs no egress config at all — it runs open and reaches its model through the host injecting proxy.

## When something goes wrong

- **Browse the run.** Open `.contremaitre/runs/<run-id>/viewer.html` — self-contained, no server. If it's missing, rebuild it: `contremaitre viewer .contremaitre/runs/<run-id>`.
- **Read the event log.** `guardrail_events.jsonl` in the run dir is the structured timeline of every state transition, cap trip, gate result, recovery, and verdict. Pair with `stats.json` (`reason` field disambiguates same-verdict failure modes).
- **Sanity-check the environment.** `contremaitre doctor --base main --fork …` runs the same preflight as `run` (Docker daemon + image, opencode binary, `:ro` mount enforcement, network posture, OpenRouter key bounds, CLI freshness vs npm) without spawning a run.
- **Sweep leftovers.** A SIGKILL'd parent can leave label-tagged containers and worktrees behind. `contremaitre cleanup --dry-run` shows what's stale; `contremaitre cleanup` removes them. Add `--deps` / `--repos` to also clear cross-run caches.

## Further reading

- [docs/control-plane.md](docs/control-plane.md) — implementation map: actor runtimes (opencode / codex / claude), CLI auth + egress lock, state machine, host-owned boundaries, hard gates, artifact contract, full CLI reference, module map.
- [golden_cases/README.md](golden_cases/README.md) — eval canary: case/config schema, headline panels, single-variable rule, methodology notes, how to add a case.
- [AGENTS.md](AGENTS.md) — conventions for coding agents modifying this repo.

## Contributing

PRs welcome. Conventions live in [AGENTS.md](AGENTS.md); run `uv run pytest` and `make lint` before opening one.

## License

Licensed under the MIT License — see [LICENSE](LICENSE).
