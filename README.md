# Contremaitre

Deterministic orchestration shell that runs Matt Pocock's [`improve-codebase-architecture`](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture) skill end-to-end against a target repository and produces a draft PR.

The agent and SIM live inside opencode-in-Docker containers. Git, GitHub, diff-scan, and cap enforcement stay host-owned — the agent has no outbound credentials.

## Quickstart — live run to draft PR

The main command. Watches the run live in a two-pane TUI (Agent | SIM), opens a draft PR on your fork when SIM approves the diff:

```bash
GITHUB_TOKEN=$(gh auth token) python3 -m contremaitre tui run -- \
  --actor opencode \
  --repo ~/code/<target-repo> \
  --base main \
  --fork git@github.com:<you>/<target-repo>.git \
  --check-cmd "npx tsc --noEmit" \
  --publish-mode gh \
  --allow-open-egress \
  --max-turns 20 \
  --max-wall-minutes 45 \
  --max-cost-usd 5
```

**One-time setup** (auto-handled on first run):
- Runtime image `contremaitre-agent:latest` builds itself on first opencode-mode run. ~3 min on a warm host.
- TUI requires `textual`: `python3 -m pip install --user textual` (skip if you'd rather watch via JSONL tail).
- `OPENROUTER_API_KEY` in `.env` (cwd or repo root) — bounded by a provider-side daily limit. Preflight refuses to start a run without one.

### What each flag does

| Flag | Meaning | Required? |
|---|---|---|
| `--actor opencode` | live mode (vs. `--actor fake` for deterministic smoke) | **yes** |
| `--repo` | local git checkout the worktree is sourced from | **yes** |
| `--base` | branch the worktree forks off + PR base | **yes** (typical: `main`) |
| `--fork` | git URL where the run's branch is pushed | **yes** for `--publish-mode gh` |
| `--opencode-config` | path to your `opencode.json` (provider + model registry) | **yes** for opencode mode |
| `--check-cmd` | executable check the post-implementation worktree must pass; repeatable | **yes** (publication blocked if absent or failing) |
| `--publish-mode gh` | open a real draft PR via `gh pr create --draft` | optional (default: `stub` — no PR, just simulates) |
| `--allow-open-egress` | accept unrestricted container egress; alternative is `--docker-network` / proxy flags | required if no proxy is configured |
| `--max-turns` | per-actor turn budget | optional (default `30`) |
| `--max-wall-minutes` | wall-clock budget | optional (default `180`) |
| `--max-cost-usd` | orchestrator cost cap, on top of OpenRouter's daily limit | optional (default `30`) |

### Useful defaults you may want to override

| Flag | Default | When to change |
|---|---|---|
| `--agent-model` | `openrouter/deepseek/deepseek-v4-flash` | cheap workhorse; bump to `openrouter/anthropic/claude-opus-4.7` for thornier targets |
| `--sim-model` | same | usually keep matched to agent-model |
| `--docker-image` | `contremaitre-agent:latest` | only when you've built a custom image |

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
  --repo ~/code/<target-repo> \
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

**Publication** runs only after `APPROVED` clears hard gates (diff scan, diff-hash match, clean worktree) and the configured executable checks.

See [docs/control-plane.md](docs/control-plane.md) for the implementation map.

## Smoke run (fake actor, no docker, no spend)

Useful for verifying the install + state machine without launching containers:

```bash
python3 -m contremaitre fixture init /tmp/contremaitre-fixture
python3 -m contremaitre run \
  --repo /tmp/contremaitre-fixture \
  --base main \
  --run-slug smoke \
  --check-cmd "python3 -m unittest discover -s tests"
```

Artifacts land in `.contremaitre/runs/<run-id>/`.

## Subsystems

### Doctor / preflight

The doctor runs the same checks as preflight without starting a run:

```bash
python3 -m contremaitre doctor \
  --repo ~/code/<target-repo> \
  --opencode-config /path/to/opencode.json \
  --allow-open-egress
```

Verifies: target repo + base ref, Docker daemon + image, opencode binary inside the image, `:ro` mount enforcement, network/proxy posture, OpenRouter key bounded via `GET /api/v1/key`.

Bypass flags exist but are noisy on purpose: `--skip-preflight`, `--skip-openrouter-key-check`, `--allow-unlimited-openrouter-key`, `--allow-open-egress`.

### Image build

The first opencode-mode run auto-builds `contremaitre-agent:latest`. Pre-build or force a rebuild:

```bash
python3 -m contremaitre image build              # build with cache
python3 -m contremaitre image build --no-cache   # force fresh layers
```

The image is generic opencode-in-Docker with `mattpocock/skills` installed globally. No target codebase is baked in; the orchestrator mounts the per-run worktree at `/app` at run time.

### Prompts

All prompts live as markdown in [`contremaitre/prompts/`](contremaitre/prompts/):

- [`initial_prompt.md`](contremaitre/prompts/initial_prompt.md) — turn-1 message to the agent. Invokes the skill; adds three host-owned scaffolds (no git, write `SETTLED_DESIGN.md`, write `IMPLEMENTATION_COMPLETE`).
- [`sim_tooled_persona.md`](contremaitre/prompts/sim_tooled_persona.md) — SIM's first-turn persona. Tooled SWE collaborator, `read`/`glob`/`grep` only, skill vocabulary, read-first-claim-second discipline.
- [`sim_review_prompt.md`](contremaitre/prompts/sim_review_prompt.md) — strict-JSON review against SETTLED + diff.

### TUI — attach to a finished run

The quickstart already uses `tui run`. To inspect an already-completed run without spawning a new one:

```bash
python3 -m contremaitre tui attach .contremaitre/runs/<run-id>
```

`Ctrl-C` quits. When wrapping a live run, `Ctrl-C` also SIGTERMs the orchestrator so its emergency-flush handler fires.

### Cleanup

Each opencode-mode run creates a per-run docker volume + a worktree at `/tmp/contremaitre-<run-id>/`; both are removed by the orchestrator in `finally`. `image build` prunes dangling images on success.

If a parent gets SIGKILL'd mid-run, leftovers can survive. Sweep them:

```bash
python3 -m contremaitre cleanup --dry-run   # see what would be removed
python3 -m contremaitre cleanup             # actually remove
```

Cleans: stale per-run docker volumes (run dir gone), leftover `/tmp/contremaitre-*` worktrees, dangling docker images (`--skip-images` to keep).

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

The CLI loads `.env` from the current directory and the source checkout before argument parsing. Shell values win. Intended for `OPENROUTER_API_KEY`; `.env` is gitignored.

## Tests

```bash
python3 -m unittest discover -s tests
```

Working on the code itself? See [AGENTS.md](AGENTS.md).
