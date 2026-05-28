# AGENTS.md

Working notes for coding agents (Claude Code, opencode, Codex, etc.) modifying this repository.

**Source of truth** for what this project is and how it's wired: [docs/control-plane.md](docs/control-plane.md). Read that first.

## Build / test

```bash
uv run pytest
```

No build step. Python ≥ 3.11. Core CLI is zero-dep; tests use the `dev`
dependency group (`uv sync --group dev`). The TUI requires `textual`
(optional extra).

## Where to edit

- **Prompts** — `contremaitre/prompts/*.md`. Tweak the markdown, not the Python wrapper.
- **Event names** — `contremaitre/events.py` constants. Don't write `"some_event"` string literals when emitting to `guardrail_events.jsonl` / `recoveries.jsonl`; renaming a constant breaks at import time instead of silently at runtime.
- **State machine / caps / cleanup** — `orchestrator.py`.
- **Docker / opencode launch** — `actors.py` (`OpencodeActorRunner.build_docker_command`).
- **Hard gates** — `evaluator.py` + `diffscan.py` + `verdicts.py`. Strict by design.
- **Live UI** — `tui.py`. Reads JSONL artifacts; never writes.
- **CLI subcommands** — `cli.py` (`run`, `doctor`, `fixture`, `image`, `tui`, `cleanup`, `eval`).
- **Eval canary** — `eval.py` (cases under `golden_cases/`) + `manifest.py` (run provenance).

## Conventions

- **Docs in sync with code.** Before committing any behavior or interface change, verify [README.md](README.md) (user-facing CLI, run flow, cleanup) and [docs/control-plane.md](docs/control-plane.md) (state machine, host-owned boundaries, artifact contract, lifecycle) still match. Update them in the same commit, not in a follow-up.
- **Host owns git and GitHub.** Never put `git push` / `gh pr create` / credentials into an actor adapter. The orchestrator's publisher is the only thing that publishes.
- **Hard gates are deterministic.** L0 (diff scan, diff-hash match, clean worktree, draft-only) and L1 (executable checks). LLM judgement never gates publication.
- **Prompts as markdown files.** The `.md` is the source; `prompts/__init__.py` loads them. Edit the markdown.
- **Skill vocabulary.** Module / Interface / Implementation / Depth / Seam / Adapter / Leverage / Locality. Don't drift into "component / service / API / boundary."
- **No backwards-compat layers.** Pre-1.0. Change the shape, update callers, update tests. No deprecation shims.
- **Fix what you find.** If you spot a bug, broken test, or stale doc while doing something else, fix it in the same change. Don't punt with "not related to my edit."
- **Run observations go in `LEARNINGS.md`** (gitignored). When a live run surfaces something non-obvious about agent/SIM behavior, the skill, or the orchestrator — append a dated entry. **Facts only**: turn-by-turn what happened, what the skill prescribed, what the prompt said. No interpretation, no fix proposals, no "this means…". Interpretation lives in the conversation that produced the fix; the notepad is forensic.

## Eval canary

`golden_cases/<case_id>/` holds **real opencode-mode** evals. Each case pins a *task* (target_url + base_sha + intent) in `case.toml`; one or more *configurations* (agent/SIM/reviewer combos) live under `configs/<name>.toml` and produce independent baselines under `baselines/<name>.json`. n=3 per (case, config). Run before merging anything that touches prompts, models, the cli_reviewer prompt, or the orchestrator's review/publish flow:

```bash
python3 -m contremaitre eval run case_01_sqlite_utils_8f0c06e --config default --n 3
python3 -m contremaitre eval compare case_01_sqlite_utils_8f0c06e --config default
python3 -m contremaitre eval promote case_01_sqlite_utils_8f0c06e --config default
```

To test the same task with a different model combo, add a sibling config (e.g. `configs/qwen_sim.toml`) rather than editing `default.toml` — each config has its own baseline. `promote` refuses on a dirty contremaitre tree, on `n<3`, or if any cli_review failed to parse — commit first. The two-variable guard (EVAL_ROADMAP §5) warns when both contremaitre code and the case-pinned tuple drift in one cycle.

When you change anything that moves `system_digest` (prompts, image, contremaitre code, models), append an entry to `docs/eval_systems.md` with the **Intent / Outcome / Learning** triple. The journal is the methodology doc — generalizable principles emerge from specific experiments, so they're recorded next to the experiment that produced them. Per-run notes (forensic, no interpretation) go in `LEARNINGS.md` (gitignored).

`smoke_cases/` holds the fake-actor integration scaffolds (state-machine canary, not eval). They are not picked up by `contremaitre eval`.

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
