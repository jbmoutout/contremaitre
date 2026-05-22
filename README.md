# Contremaitre

Deterministic orchestration shell that runs Matt Pocock's [`improve-codebase-architecture`](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture) skill end-to-end against a target repository and produces a draft PR.

The agent and SIM live inside opencode-in-Docker containers. Git, GitHub, diff-scan, and cap enforcement stay host-owned — the agent has no outbound credentials.

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

## Smoke run (fake actor)

```bash
python3 -m contremaitre fixture init /tmp/contremaitre-fixture
python3 -m contremaitre run \
  --repo /tmp/contremaitre-fixture \
  --base main \
  --run-slug smoke \
  --check-cmd "python3 -m unittest discover -s tests"
```

Artifacts land in `.contremaitre/runs/<run-id>/`.

## Live run (opencode actor)

```bash
python3 -m contremaitre run \
  --actor opencode \
  --repo ~/code/itadakimasu \
  --fork git@github.com:<user>/itadakimasu.git \
  --base main \
  --agent-model openrouter/deepseek/deepseek-v4-flash \
  --sim-model openrouter/deepseek/deepseek-v4-flash \
  --docker-image arch001-eval-app:latest \
  --opencode-config /path/to/opencode.json \
  --check-cmd "npm test" \
  --publish-mode stub
```

To actually publish a draft PR, set `--publish-mode gh` and supply `GITHUB_TOKEN`:

```bash
GITHUB_TOKEN=... python3 -m contremaitre run \
  --actor opencode \
  --publish-mode gh \
  --gh-repo <owner>/<repo> \
  ...
```

The GitHub token is used only by the host publisher — it never reaches the containers.

The CLI loads `.env` from the current directory and the source checkout before argument parsing. Shell values win. Intended for `OPENROUTER_API_KEY`; `.env` is gitignored.

Network posture is explicit: pass `--docker-network`, `--http-proxy`, `--https-proxy`, `--no-proxy` when running behind a controlled proxy. Ambient proxy variables are not forwarded.

## Doctor / preflight

```bash
python3 -m contremaitre doctor \
  --repo ~/code/itadakimasu \
  --docker-image arch001-eval-app:latest \
  --opencode-config /path/to/opencode.json \
  --http-proxy http://127.0.0.1:8080
```

Verifies: target repo + base ref, Docker daemon + image, opencode binary inside the image, `:ro` mount enforcement, explicit network/proxy posture, OpenRouter key bounded via `GET /api/v1/key`.

Bypass flags exist but are noisy on purpose: `--skip-preflight`, `--skip-openrouter-key-check`, `--allow-unlimited-openrouter-key`, `--allow-open-egress`.

## Prompts

All prompts live as markdown in [`contremaitre/prompts/`](contremaitre/prompts/):

- [`initial_prompt.md`](contremaitre/prompts/initial_prompt.md) — turn-1 message to the agent. Invokes the skill; adds three host-owned scaffolds (no git, write `SETTLED_DESIGN.md`, write `IMPLEMENTATION_COMPLETE`).
- [`sim_tooled_persona.md`](contremaitre/prompts/sim_tooled_persona.md) — SIM's first-turn persona. Tooled SWE collaborator, `read`/`glob`/`grep` only, skill vocabulary, read-first-claim-second discipline.
- [`sim_review_prompt.md`](contremaitre/prompts/sim_review_prompt.md) — strict-JSON review against SETTLED + diff.

## Tests

```bash
python3 -m unittest discover -s tests
```

Working on the code itself? See [AGENTS.md](AGENTS.md).
