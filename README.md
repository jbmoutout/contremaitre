# Contremaitre

Deterministic orchestration shell that runs Matt Pocock's [`improve-codebase-architecture`](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture) skill end-to-end against a target repository and produces a draft PR.

The agent and SIM live inside opencode-in-Docker containers. Git, GitHub, diff-scan, and cap enforcement stay host-owned — the agent has no outbound credentials.

## Quickstart — live run to draft PR

The main command. Watches the run live in a two-pane TUI (Agent | SIM), opens a draft PR on your fork when SIM approves the diff:

```bash
GITHUB_TOKEN=$(gh auth token) python3 -m contremaitre tui run -- \
  --actor opencode \
  --base main \
  --fork git@github.com:<you>/<target-repo>.git \
  --publish-mode gh \
  --allow-open-egress \
  --max-turns 20 \
  --max-wall-minutes 45 \
  --max-cost-usd 5
```

The target repo is cloned lazily into `~/.cache/contremaitre/<host>-<owner>-<repo>/` on first run (subsequent runs reuse the cache + `git fetch origin <base>` for freshness). You never need a parallel local checkout — only the `--fork` URL.

Before launching, contremaitre walks through pre-flight checks and decisions in this order: an OpenRouter key-status banner, the model picker (free OpenCode Zen models by default — paste any OpenRouter slug when a key is set), a cli-reviewer availability check, a free-tier quota probe, then a decision-free recap (target, branch, models, code-review, caps, network) and `Continue? [Y/n]`. Pass `-y` / `--yes` to skip the prompt (CI / scripts); non-TTY mode collapses the banners to `[info]` log lines so logs explain what was auto-assumed.

Add `--check-cmd "<command>"` (repeatable) if your target has a fast deterministic check worth gating publication on — see [ecosystem examples](#executable-checks-per-ecosystem) below. Without it, publication still requires SIM approval + L0 hard gates (diff scan, diff-hash match, clean worktree).

**Shortcut:** a [`justfile`](justfile) at the repo root wraps the long form. `brew install just`, then:

```bash
just                                              # list recipes
just tui-run main git@github.com:<you>/<target>.git
just deepdeep tui-run main git@github.com:<you>/<target>.git
```

`deepdeep` is a model preset that pins both `--agent-model` and `--sim-model` to `openrouter/deepseek/deepseek-v4-flash`. Add per-target convenience recipes (e.g. `just my-repo`) and more presets as the workflow grows.

**One-time setup** (auto-handled on first run):
- Local clone cache is created on demand under `~/.cache/contremaitre/`.
- Runtime image `contremaitre-agent:latest` builds itself on first opencode-mode run. ~3 min on a warm host. Auto-rebuilds on subsequent runs when the Dockerfile content changes (image carries a `contremaitre.dockerfile-sha256` label; mismatch triggers a rebuild). Ships with `uv` + `poetry` so Python targets get a working runtime out of the box.
- TUI requires `textual` — run `uv sync --extra tui` (or `--extra tui --group dev` for the full dev env). Skip if you'd rather watch via JSONL tail.
- `OPENROUTER_API_KEY` in `.env` (cwd or repo root) — **optional**. Without a key, runs use free [OpenCode Zen](https://opencode.ai/docs/zen/) models served by OpenCode (no auth needed). Set a key to unlock paid OpenRouter models and to paste arbitrary OpenRouter slugs in the picker; preflight verifies the key has a provider-side credit limit (configurable in your OpenRouter dashboard) and warns on unlimited keys. See [`.env.example`](.env.example).

### What each flag does

| Flag | Meaning | Required? |
|---|---|---|
| `--actor opencode` | live mode (vs. `--actor fake` for deterministic smoke) | **yes** |
| `--base` | branch the worktree is sourced from + PR target | **yes** (typical: `main`) |
| `--fork` | git URL where the run's branch is pushed; also the default clone source for the cache | **yes** for `--publish-mode gh` |
| `--upstream` | canonical clone source when `--fork` is your fork of someone else's repo | optional (preferred over `--fork` for cloning when set) |
| `--repo-cache` | override the auto-derived cache path (default: `~/.cache/contremaitre/<slug>/`) | optional |
| `--opencode-config` | path to your `opencode.json` (provider + model registry) | **yes** for opencode mode |
| `--check-cmd` | executable check the post-implementation worktree must pass; repeatable | optional (publication blocked only on a configured-and-failing check) |
| `--cli-reviewer` | post-publish code review by a locally-installed CLI on your subscription: `auto` / `codex` / `claude` / `none`. `auto` (default) detects what's installed and prompts on TTY. Posts a single review comment on the Draft PR; never blocks the run. | optional (default: `auto`) |
| `--publish-mode gh` | open a real draft PR via `gh pr create --draft` | optional (default: `stub` — no PR, just simulates) |
| `--yes` / `-y` | skip the pre-launch Y/n prompt | optional (auto-skipped in non-TTY) |
| `--allow-open-egress` | accept unrestricted container egress; alternative is `--docker-network` / proxy flags | required if no proxy is configured |
| `--max-turns` | per-actor turn budget | optional (default `30`) |
| `--max-wall-minutes` | wall-clock budget | optional (default `180`) |
| `--max-cost-usd` | orchestrator cost cap, on top of OpenRouter's daily limit | optional (default `30`) |

### Useful defaults you may want to override

| Flag | Default | When to change |
|---|---|---|
| `--agent-model` | omit to pick interactively at launch (numbered list of [OpenCode Zen](https://opencode.ai/docs/zen/) free models — no auth needed, served by OpenCode); CLI fallback is `openrouter/deepseek/deepseek-v4-flash` | bump to `openrouter/anthropic/claude-opus-4.7` for thornier targets |
| `--sim-model` | omit to pick (second prompt; defaults to your agent pick) | usually keep matched to agent-model |
| `--docker-image` | `contremaitre-agent:latest` | only when you've built a custom image |

Passing both `--agent-model` and `--sim-model` skips the picker (this is how the `just deepdeep` preset works). The picker is also skipped when stdin isn't a TTY (CI / scripts) or when `--yes` is set.

### Cross-fork PR (target is upstream, not your fork)

Add these flags when your `--fork` is your fork of someone else's repo and you want the PR opened on the upstream:

```bash
  --upstream git@github.com:<owner>/<target-repo>.git \
  --gh-repo <owner>/<target-repo>
```

### Without the TUI

Drop the `tui run --` prefix and the orchestrator runs headless, printing the verdict at end:

```bash
GITHUB_TOKEN=$(gh auth token) python3 -m contremaitre run \
  --actor opencode \
  --base main \
  --fork git@github.com:<you>/<target-repo>.git \
  ... (same flags)
```

### Dry-run before pulling the trigger

`--publish-mode stub` runs everything through to the publisher and stops — no `git push`, no `gh pr create`. Useful for the first run against a target:

```bash
  --publish-mode stub \
  # GITHUB_TOKEN not needed
```

## Run shape

```
INIT → WORK → REVIEW → APPROVED → draft PR
            ↘ WORK    (CHANGES_REQUESTED, up to max_review_rounds)
            ↘ NO_PR   (NEEDS_HUMAN, cap trip, missing marker, max rounds, malformed verdict)
              FAILED  (infrastructure error)
```

**WORK** is one multi-turn opencode session: the agent invokes the skill end-to-end, the tooled SIM responds as the SWE/user. The loop ends when the agent writes `.contremaitre/IMPLEMENTATION_COMPLETE` in the worktree.

**REVIEW** is a single-shot SIM call against `.contremaitre/SETTLED_DESIGN.md` and the diff. The SIM returns a strict JSON verdict.

**Publication** runs only after `APPROVED` clears hard gates (diff scan, diff-hash match, clean worktree) and any configured executable checks. Skipping `--check-cmd` is fine — the L1 gate becomes a no-op and the scorecard records `executable_confidence: null`.

**Post-publish code review** (optional). If `--cli-reviewer` is set (default `auto` detects `claude`/`codex` on PATH and prompts on TTY), the orchestrator invokes the chosen CLI after the Draft PR opens. It pulls the PR diff via `gh`, produces a verdict-led markdown review (line 1 is `🟢 LOOKS_GOOD` / `🟠 NEEDS_ATTENTION` / `🔴 MUST_FIX` followed by a one-sentence justification), and posts it as a single PR comment. The CLI runs on your host with your OAuth subscription (Claude Pro/Max, ChatGPT Plus) — no API quota, no container. Subprocess env scrubs `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` so it can't silently bill API. Failures are logged but never block the run — the PR is already published.

See [docs/control-plane.md](docs/control-plane.md) for the implementation map.

### Executable checks per ecosystem

`--check-cmd` is target-agnostic: pass whatever fast deterministic command tells you the diff is at least mechanically sound. Examples:

| Stack | Lockfile | Example |
|---|---|---|
| Node / TS | `package-lock.json` / `pnpm-lock.yaml` / `yarn.lock` | `--check-cmd "npx tsc --noEmit"` |
| Python (uv) | `uv.lock` | `--check-cmd "uv run pytest -q"` |
| Python (poetry) | `poetry.lock` | `--check-cmd "poetry run pytest -q"` |
| Python (rye / pip-tools) | `requirements.lock` | `--check-cmd "python -m pytest -q"` |
| Rust | `Cargo.lock` (needs `--docker-image contremaitre-agent-rust:latest`) | `--check-cmd "cargo check --all-targets"` |
| Go | `go.sum` (needs `--docker-image contremaitre-agent-go:latest`) | `--check-cmd "go build ./..."` |

The check runs in a sidecar container that mounts the same worktree + lockhash-keyed deps volume the agent used, with a 600s timeout per command. The deps volume sits at `/app/{node_modules,.venv,.cargo-cache,.go-mod-cache}` (per-ecosystem); runtime env vars (`VIRTUAL_ENV`, `CARGO_HOME`, `GOPATH`) are auto-injected so ecosystem tools find it without per-target setup. Repeat the flag to gate on more than one command.

## Eval canary (v0, regression detection)

Pinned `(target_url, base_sha)` cases under [`golden_cases/`](golden_cases/) run the **real opencode actor** with real prompts, real models, and the codex cli_reviewer. Each case runs n=3 times; the cell summary (verdict-key score, terminal mix, LoC + files-changed, review rounds, cost, wall time, cross-family agreement) is compared against a per-case `baseline.json`. Manual trigger.

```bash
python3 -m contremaitre eval run case_01_sqlite_utils_8f0c06e --n 3
python3 -m contremaitre eval compare case_01_sqlite_utils_8f0c06e
python3 -m contremaitre eval promote case_01_sqlite_utils_8f0c06e
```

A canary cycle on sqlite-utils with the deepseek-v4-flash-free models takes ~3 × ~15min on opencode. `eval compare` exits non-zero on any headline-panel regression (drop ≥ 0.30 on `cli_review_score`, terminal-mix worsened, format-compliance dropped, etc.). `eval promote` refuses to baseline a dirty tree or a cell where any cli_review failed to parse. Two-variable guard fires when both contremaitre's `system_digest` and the case's `input_digest` differ from baseline (don't bump prompts AND models in one go — [EVAL_ROADMAP §5](EVAL_ROADMAP.md)). See [`golden_cases/README.md`](golden_cases/README.md) to add a case. L2/L3 LLM judges remain `PENDING` per [EVAL_ROADMAP §6](EVAL_ROADMAP.md).

Fake-actor scaffolds under [`smoke_cases/`](smoke_cases/) are integration tests of the state machine, not evals. They are not picked up by `contremaitre eval`.

## Smoke run (fake actor, no docker, no spend)

Useful for verifying the install + state machine without launching containers:

```bash
python3 -m contremaitre fixture init /tmp/contremaitre-fixture
python3 -m contremaitre run \
  --base main \
  --fork file:///tmp/contremaitre-fixture \
  --run-slug smoke \
  --check-cmd "python3 -m unittest discover -s tests" \
  --yes
```

Artifacts land in `.contremaitre/runs/<run-id>/`.

## Subsystems

### Doctor / preflight

The doctor runs the same checks as preflight without starting a run:

```bash
python3 -m contremaitre doctor \
  --base main \
  --fork git@github.com:<you>/<target-repo>.git \
  --opencode-config /path/to/opencode.json \
  --allow-open-egress
```

Verifies: target repo + base ref, Docker daemon + image, opencode binary inside the image, `:ro` mount enforcement, network/proxy posture, OpenRouter key bounded via `GET /api/v1/key`.

Bypass flags exist but are noisy on purpose: `--skip-preflight`, `--skip-openrouter-key-check`, `--allow-unlimited-openrouter-key`, `--allow-open-egress`.

### Image build

The first opencode-mode run auto-builds `contremaitre-agent:latest`. Subsequent runs auto-rebuild when the Dockerfile content has changed (image staleness is detected by comparing the `contremaitre.dockerfile-sha256` label against the on-disk Dockerfile). Pre-build or force a rebuild:

```bash
python3 -m contremaitre image build                    # base (default)
python3 -m contremaitre image build --variant rust     # adds Rust toolchain
python3 -m contremaitre image build --variant go       # adds Go toolchain
python3 -m contremaitre image build --no-cache         # force fresh layers
```

The base image is generic opencode-in-Docker with `uv`, `poetry`, and `mattpocock/skills` installed globally. No target codebase is baked in; the orchestrator mounts the per-run worktree at `/app` at run time. Variants chain `FROM contremaitre-agent:latest` and add their toolchain — use them via `--docker-image contremaitre-agent-{rust,go}:latest` for Cargo / Go targets.

### Prompts

All prompts live as markdown in [`contremaitre/prompts/`](contremaitre/prompts/):

- [`initial_prompt.md`](contremaitre/prompts/initial_prompt.md) — turn-1 message to the agent. Invokes the skill; adds three host-owned scaffolds (no git, write `SETTLED_DESIGN.md`, write `IMPLEMENTATION_COMPLETE`).
- [`sim_tooled_persona.md`](contremaitre/prompts/sim_tooled_persona.md) — SIM's first-turn persona. Tooled SWE collaborator, `read`/`glob`/`grep` only, skill vocabulary, read-first-claim-second discipline.
- [`sim_review_prompt.md`](contremaitre/prompts/sim_review_prompt.md) — strict-JSON review against SETTLED + diff.
- [`cli_reviewer_prompt.md`](contremaitre/prompts/cli_reviewer_prompt.md) — post-publish code review handed to the locally-installed CLI (claude/codex). Enforces a verdict-led format (`🟢 LOOKS_GOOD` / `🟠 NEEDS_ATTENTION` / `🔴 MUST_FIX` + one-sentence justification) + Conventional Comments labels (`issue`/`suggestion`/`nit`/`question`).

### TUI — attach to a finished run

The quickstart already uses `tui run`. To inspect an already-completed run without spawning a new one:

```bash
python3 -m contremaitre tui attach .contremaitre/runs/<run-id>
```

`Ctrl-C` quits. When wrapping a live run, `Ctrl-C` also SIGTERMs the orchestrator so its emergency-flush handler fires.

### Viewer — single-file HTML over a finished run

Every run terminus (success or failure) writes `viewer.html` into the run directory alongside `stats.json` / `transcript.md` / `extracted_files/`. The viewer is self-contained (no network, no build step) — open it in a browser to browse the transcript, timeline, sub-agents, written files, guardrail events, and eval reports for that run.

Rebuild it for an existing run directory at any time:

```bash
python3 -m contremaitre viewer .contremaitre/runs/<run-id>
python3 -m contremaitre viewer .contremaitre/runs/<run-id> --open   # also opens in default browser
```

### Cleanup

Each opencode-mode run creates a worktree at `/tmp/contremaitre-<run-id>/` and label-tagged containers (`contremaitre.run-id=<id>`); both are removed by the orchestrator in `finally`. `image build` prunes dangling images on success. The lockhash-keyed deps volumes and the auto-managed local clone caches under `~/.cache/contremaitre/` are kept by default (cross-run caches).

If a parent gets SIGKILL'd mid-run, leftovers can survive. Sweep them:

```bash
python3 -m contremaitre cleanup --dry-run    # see what would be removed
python3 -m contremaitre cleanup              # remove stale containers + worktrees + dangling images
python3 -m contremaitre cleanup --deps       # also nuke lockhash-keyed deps volumes
python3 -m contremaitre cleanup --repos      # also nuke ~/.cache/contremaitre/<slug>/ clones
```

Cleans, in order: containers labeled `contremaitre.run-id=*` whose run dir is gone, leftover `/tmp/contremaitre-*` worktrees, dangling docker images (`--skip-images` to keep), and optionally the cross-run caches behind `--deps` / `--repos`.

### Network posture

Open egress is allowed via `--allow-open-egress` (above). For a controlled proxy:

```bash
  --docker-network <network>
  --http-proxy http://127.0.0.1:8080
  --https-proxy http://127.0.0.1:8080
  --no-proxy localhost,127.0.0.1
```

Ambient proxy environment variables are not forwarded into containers — only what you pass explicitly.

### `.env` loading

The CLI loads `.env` from the current directory and the source checkout before argument parsing. Shell values win. Intended for `OPENROUTER_API_KEY` (optional — see [`.env.example`](.env.example) and the one-time-setup note above); `.env` is gitignored.

## Tests

One-time setup (creates `.venv` with pytest, rich, and the project in editable mode):

```bash
uv sync --group dev
```

Add `--extra tui` if you also want the live TUI (`textual`). Run the suite:

```bash
uv run pytest
```

Working on the code itself? See [AGENTS.md](AGENTS.md).
