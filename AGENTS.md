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

## No-vendor-runtime policy

This repo is self-contained. No runtime imports from sibling/parent directories on the operator's machine, no `sys.path` hacks, no soft dependencies on external research repos. When a pattern (a multi-turn loop, a focused-judge call, a recovery routine) comes from outside, **copy it in and own it**. The vendored copy is the source of truth; the upstream may move on and we will not chase.

This repo should be treated as public. Do not commit absolute paths to anyone's machine, internal project names, links to private repos, or references to non-public design documents. Anything operator-specific belongs in `AGENTS.local.md` (gitignored).

## What NOT to do

- Don't add cosmetic "scorecard" numbers without an actual judge backing them. L2 (SETTLED-to-diff conformance) and L3 (architecture-delta) are explicitly `PENDING` in `evaluator.py` until focused-judge passes exist. When implementing them, copy the pattern in — don't import from external paths.
- Don't put `--no-verify`, `--no-gpg-sign`, or destructive force operations in the publisher. The diff-scan and diff-hash check are load-bearing, not advisory.
- Don't move git operations into the agent or SIM containers. The threat model is built on the agent having no outbound credentials.
- Don't reintroduce phase-based prompts. The agent runs the skill end-to-end in one multi-turn session. The orchestrator yields to the SIM between agent turns; it does not re-prompt the agent with phase instructions.
