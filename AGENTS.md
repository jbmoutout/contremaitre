# AGENTS.md

Working notes for coding agents (Claude Code, opencode, Codex, etc.) modifying this repository.

**Source of truth** for what this project is and how it's wired: [docs/control-plane.md](docs/control-plane.md). Read that first.

## Build / test

```bash
python3 -m unittest discover -s tests
```

No build step. Python ≥ 3.11. Core CLI is zero-dep; the TUI requires `textual` (optional extra).

## Where to edit

- **Prompts** — `contremaitre/prompts/*.md`. Tweak the markdown, not the Python wrapper.
- **State machine / caps / cleanup** — `orchestrator.py`.
- **Docker / opencode launch** — `actors.py` (`OpencodeActorRunner.build_docker_command`).
- **Hard gates** — `evaluator.py` + `diffscan.py` + `verdicts.py`. Strict by design.
- **Live UI** — `tui.py`. Reads JSONL artifacts; never writes.
- **CLI subcommands** — `cli.py` (`run`, `doctor`, `fixture`, `image`, `tui`, `cleanup`).

## Conventions

- **Host owns git and GitHub.** Never put `git push` / `gh pr create` / credentials into an actor adapter. The orchestrator's publisher is the only thing that publishes.
- **Hard gates are deterministic.** L0 (diff scan, diff-hash match, clean worktree, draft-only) and L1 (executable checks). LLM judgement never gates publication.
- **Prompts as markdown files.** The `.md` is the source; `prompts/__init__.py` loads them. Edit the markdown.
- **Skill vocabulary.** Module / Interface / Implementation / Depth / Seam / Adapter / Leverage / Locality. Don't drift into "component / service / API / boundary."
- **No backwards-compat layers.** Pre-1.0. Change the shape, update callers, update tests. No deprecation shims.

## Dependency policy

- **Local sibling directories on the operator's machine** — never import at runtime. Copy patterns in and own the vendored copy.
- **Public PyPI packages** — allowed when they carry real weight (>30 lines of hand-rolled). Add to `pyproject.toml`.
- **Private GitHub repos** — copy in, don't depend on access.

Repo is treated as public. No absolute paths, internal codenames, or links to private repos in committed files. Operator-specific notes go in `AGENTS.local.md` (gitignored).

## What NOT to do

- Don't add scorecard numbers without a real judge backing them. L2 / L3 are `PENDING` in `evaluator.py` until focused-judge passes exist.
- Don't put `--no-verify`, force-push, or destructive flags in the publisher. Hard gates are load-bearing, not advisory.
- Don't move git operations into the agent or SIM containers. The threat model relies on the agent having no outbound credentials.
- Don't reintroduce phase-based prompts. One multi-turn WORK session, SIM yields between agent turns, no orchestrator-side phase re-prompting.
