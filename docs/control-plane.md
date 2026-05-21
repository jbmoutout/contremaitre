# Control Plane

This document is the implementation map for humans and future agents. Contremaitre's control plane is deterministic Python; the agent and SIM live inside opencode-in-Docker containers and never hold git or GitHub credentials.

## Shape

```
INIT  →  WORK  →  REVIEW  →  APPROVED   (PR opened)
                          ↘  WORK       (CHANGES_REQUESTED, up to max_review_rounds)
                          ↘  NO_PR      (CHANGES_REQUESTED exhausted, NEEDS_HUMAN,
                                          malformed verdict, cap trip, no marker)
                             FAILED     (infrastructure error)
```

- **WORK** is one multi-turn opencode session. The agent runs `/improve-codebase-architecture` end-to-end (Explore → Present → Grill → settle → implement) while the read-only tooled SIM responds turn by turn as the SWE the skill talks to. The loop terminates when the agent writes `.contremaitre/IMPLEMENTATION_COMPLETE` in the worktree, or when a cap fires.
- **REVIEW** is a single-shot SIM call against `/review/SETTLED_DESIGN.md` and `/review/diff.patch` (read-only mounts). The SIM emits a strict JSON verdict.
- **Revision** is not a separate state. A `CHANGES_REQUESTED` verdict clears the marker file, sends the required changes to the agent's WORK session (same opencode session, resumed via `--session`), and re-enters WORK.
- **APPROVED** runs hard gates (diff-scan, diff-hash match, clean worktree), executable checks, then the publisher.

The multi-turn loop pattern is vendored from `references/evals/scripts/run_grilling_openevals.py::run_tooled_simulation`.

## Module Map

- `cli.py` — argument parsing and command dispatch.
- `orchestrator.py` — state machine, caps, worktree lifecycle, WORK loop, review loop.
- `prompts/` — INITIAL_PROMPT, SIM tooled persona, SIM review prompt; markdown files loaded into module constants for easy tweaking.
- `actors.py` — process adapters (`FakeActorRunner`, `OpencodeActorRunner`) implementing the `agent_turn` / `sim_turn` / `sim_review` protocol.
- `fake_actor.py` — deterministic fake agent/SIM for fixture smoke runs.
- `git_utils.py` — logged git command wrapper.
- `verdicts.py` — strict SIM verdict parser and diff hashing.
- `diffscan.py` — deterministic forbidden-path scanner.
- `checks.py` — executable check runner.
- `envfile.py` — dependency-free `.env` loader for local operator secrets.
- `evaluator.py` — gate-first PR-eval report writer.
- `publisher.py` — publication boundary (`StubPublisher`, `GhPublisher`).
- `preflight.py` — operational checks for live opencode runs.
- `fixture.py` — local fixture repo creation for smoke tests.

## Host-owned boundaries

The agent and SIM never hold:

- git credentials. The orchestrator commits, scans, and pushes.
- a GitHub token. The orchestrator runs `gh pr create --draft`.

The opencode containers see only `OPENROUTER_API_KEY` (provider-side bounded) and proxy variables supplied through CLI flags. Ambient environment is never inherited.

Read-only enforcement is belt-and-suspenders:

- SIM container `:ro` mount on `/app`.
- SIM persona explicitly forbids `write` / `edit` / `apply_patch` / `bash` / `task`.
- Host diff-scan blocks publication if forbidden paths appear.

## Operational checks (preflight)

Live opencode runs execute preflight before worktree creation. The report is persisted to `eval/preflight_report.json`. `contremaitre doctor` runs the same checks without starting a run.

Blocks:
- missing target repo / base ref;
- missing Docker daemon or target image;
- opencode binary failures inside the image;
- failed `:ro` mount enforcement;
- open container egress unless a network/proxy is supplied or `--allow-open-egress` is set;
- missing, unlimited, over-cap, or unverified OpenRouter key.

Warns (does not block):
- OpenRouter key limit excludes BYOK usage (acceptable for non-BYOK models).

Still operational, not solved purely in code:
- Provider-side OpenRouter spend limits must be set in OpenRouter.
- Domain-restricted egress requires `--docker-network` or proxy flags.
- Real opencode images/configs are environment-specific and must be verified on the target machine.

## Artifact Contract

Every run writes:

- `initial_prompt.txt`
- `raw_export.jsonl` (agent JSONL stream)
- `sim_raw_export.jsonl` (SIM JSONL stream)
- `transcript.md`
- `timeline.jsonl`
- `trajectory.json`
- `stats.json`
- `git_log.jsonl`
- `test_runs.jsonl`
- `review_cycles.jsonl`
- `worktree_state.jsonl`
- `guardrail_events.jsonl`
- `pr.json`
- `eval/pr_eval.{json,md}`
- `eval/checks_report.json`
- `eval/settled_diff_report.json`
- `eval/architecture_delta_report.json`
- `eval/trajectory_report.json`
- `eval/cost_report.json`
- `eval/preflight_report.json`

These are product artifacts, not the workbench eval substrate. `score.json` and the old weighted composite are intentionally absent.

## Terminal signal

`.contremaitre/IMPLEMENTATION_COMPLETE` in the worktree ends the WORK loop. The marker is a Contremaitre scaffold — it is not part of the `improve-codebase-architecture` skill. The INITIAL_PROMPT tells the agent to write it only after SETTLED is locked, the implementation matches SETTLED, and the relevant local checks have been attempted.

`.contremaitre/SETTLED_DESIGN.md` is also a Contremaitre scaffold (the skill doesn't prescribe it). It is the design handoff artifact the review pass reads.
