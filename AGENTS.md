# AGENTS.md

Working notes for coding agents (Claude Code, opencode, Codex, etc.) modifying this repository.

## What this project is

Contremaitre is the deterministic control plane around an architecture-improvement agent. It launches `improve-codebase-architecture` end-to-end in an opencode-in-Docker container, pairs it with a tooled SIM (read-only opencode), and produces a draft PR after deterministic gates.

Read [docs/control-plane.md](docs/control-plane.md) for the implementation map and [README.md](README.md) for the run shape.

## Build / test

```bash
python3 -m unittest discover -s tests
```

No build step. No external dependencies (the dependency-free `.env` parser and JSONL helpers are deliberate). Python ≥ 3.11.

## File layout

- `contremaitre/orchestrator.py` — state machine, WORK multi-turn loop, REVIEW pass, hard gates.
- `contremaitre/actors.py` — `FakeActorRunner` (subprocess) and `OpencodeActorRunner` (opencode-in-Docker).
- `contremaitre/prompts/` — markdown prompts loaded into constants. Tweak the `.md`, not the Python.
- `contremaitre/preflight.py` — operational checks for live opencode runs.
- `contremaitre/publisher.py` — `StubPublisher` and `GhPublisher`. Host-side only.
- `contremaitre/{checks,costs,diffscan,verdicts,evaluator,git_utils,jsonlog,fixture,envfile,paths,models}.py` — small focused modules.
- `tests/` — `test_control_plane.py` (state machine end-to-end), `test_opencode_boundaries.py` (docker command shape, publisher, prompts), `test_preflight.py`, `test_envfile.py`.

## Conventions

- **Host owns git and GitHub.** Never put `git push` / `gh pr create` / credential handling into an actor adapter. The orchestrator's publisher is the only thing that publishes.
- **Strict JSON SIM verdicts.** Parser in `verdicts.py` rejects markdown fences. Don't loosen it.
- **Hard gates are deterministic.** L0 (diff scan, diff-hash match, clean worktree, draft-only) and L1 (executable checks). LLM judgement never gates publication.
- **Prompts as markdown files.** `contremaitre/prompts/*.md` are the source of truth; the `__init__.py` loads them into constants. Edit the markdown.
- **Skill vocabulary.** When writing prompts or persona text, use Module / Interface / Implementation / Depth / Seam / Adapter / Leverage / Locality. Don't drift into "component / service / API / boundary."
- **No backwards-compat layers.** This is pre-1.0. Change the shape, update the callers, update the tests. No deprecation shims.

## What NOT to do

- Don't add cosmetic "scorecard" numbers without an actual judge backing them. L2 (SETTLED-to-diff) and L3 (architecture-delta) are explicitly `PENDING` in `evaluator.py` until focused-judge passes exist. Patterns to vendor are in `references/evals/scripts/grade_judge.py` and `grade_constraints.py` when implementing them.
- Don't put `--no-verify`, `--no-gpg-sign`, or destructive force operations in the publisher. The diff-scan and diff-hash check are load-bearing, not advisory.
- Don't move git operations into the agent or SIM containers. The threat model is built on the agent having no outbound credentials.
- Don't reintroduce phase-based prompts. The agent runs the skill end-to-end in one multi-turn session. The orchestrator yields to the SIM between agent turns; it does not re-prompt the agent with phase instructions.

## Related context

The eval substrate at `~/code/workbench/references/evals/` and the methodology doc at `~/code/workbench/projects/_evals.md` validate the AGENT and SIM mechanics (opencode JSONL streams, session-id persistence, sqlite recovery, tooled-SIM personas) across 18 reps. When working on actor adapters or the multi-turn loop, that body of work is the empirical reference.

The CLI plan is `~/code/workbench/projects/_contremaitre_cli.md`. It is the design intent; this repo is the implementation.
