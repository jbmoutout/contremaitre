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

The multi-turn loop is self-contained; Contremaitre does not import any external orchestration substrate at runtime.

## Module Map

- `cli.py` — argument parsing and command dispatch (`run`, `doctor`, `fixture`, `image`, `cleanup`, `tui`, `viewer`). Derives an auto-managed local clone cache at `~/.cache/contremaitre/<host>-<owner>-<repo>/` from the `--upstream` (preferred) or `--fork` URL; clones lazily on first run, reused thereafter. The operator never points contremaitre at a parallel local checkout. Pre-launch Y/n prompt summarises base / source / publish target / caps (skippable via `-y` or non-TTY stdin).
- `orchestrator.py` — state machine, caps, worktree lifecycle, WORK loop, review loop, host-side commit (with SETTLED-derived title + body), publication gate, label-driven container cleanup, SIGTERM emergency-flush.
- `prompts/` — INITIAL_PROMPT, SIM tooled persona, SIM review prompt; markdown files loaded into module constants for easy tweaking.
- `actors.py` — process adapters (`FakeActorRunner`, `OpencodeActorRunner`). Opencode containers run **detached** (`docker run -d`) with `contremaitre.run-id=<id>` + `contremaitre.role=<agent|sim|review>` labels; output streamed via `docker logs -f`, exit awaited via `docker wait`.
- `fake_actor.py` — deterministic fake agent/SIM for fixture smoke runs.
- `git_utils.py` — logged git command wrapper.
- `verdicts.py` — strict SIM verdict parser (fence-tolerant) and diff hashing.
- `diffscan.py` — deterministic forbidden-path scanner.
- `checks.py` — executable check runner. In OPENCODE mode each `--check-cmd` runs in a sidecar container that mounts the worktree + deps volume; FAKE mode runs on the host (tests don't need docker).
- `runtime_image.py` — lockhash-keyed deps caching. Detects `package-lock.json` / `pnpm-lock.yaml` / `yarn.lock` / `poetry.lock` / `uv.lock` / `Cargo.lock` / `go.sum`, populates `contremaitre-deps-<lockfile>-<digest>` named volume once per lockfile via a one-shot install container (host repo mounted RO at `/install`, volume RW at `/install/node_modules`). Raises `DepsInstallError` on failure with a log path; CLI hard-fails the run.
- `envfile.py` — dependency-free `.env` loader for local operator secrets.
- `evaluator.py` — gate-first PR-eval report writer. `executable_confidence` is `null` (not `0.0`) when no `--check-cmd` is configured.
- `publisher.py` — publication boundary (`StubPublisher`, `GhPublisher`). PR title + body are derived from `.contremaitre/SETTLED_DESIGN.md` (same helper as the commit) + SIM verdict summary; `--pr-title` / `--pr-body` flags override.
- `preflight.py` — operational checks for live opencode runs.
- `fixture.py` — local fixture repo creation for smoke tests.
- `events.py` — single source of truth for guardrail-event name strings (writer + structural-reader side). TUI classifies by substring pattern, not import.
- `viewer/` — builds `viewer.html` (single-file HTML over the run dir's JSONL artifacts) from the orchestrator's `finally` so it lands on success and failure paths.
- `tui.py` — read-only Textual TUI tailing JSONL artifacts. Bottom-bar state breadcrumb (`INIT › WORK › REVIEW › APPROVED › PUBLISHED`); animated Braille spinner with `active` / `thinking` / `idle` states per pane; per-turn separator in each pane log; elapsed clock + last-write age freeze at terminal state.

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
- `guardrail_events.jsonl` (per-turn lifecycle events + `check_started`/`check_completed`, `host_commit_created`, `review_verdict`, `hard_gates_checked`, `published`/`publication_blocked`, `worktree_removed`)
- `recoveries.jsonl` (sqlite-recovery / SIGTERM-emergency events)
- `pr.json`
- `subagents/agent_NN_<slug>.md` (one per `task` tool_use; populated by `extract.py` in the orchestrator's `finally`)
- `extracted_files/<host_name>` (every file the agent wrote via `write`, `edit`, or `apply_patch`)
- `viewer.html` (self-contained single-file viewer over the artifacts above; built by `contremaitre/viewer/` in the orchestrator's `finally`, so it lands on success **and** failure paths. Rebuild for an existing run with `contremaitre viewer <run-dir>`.)
- `eval/pr_eval.{json,md}`
- `eval/checks_report.json`
- `eval/settled_diff_report.json`
- `eval/architecture_delta_report.json`
- `eval/trajectory_report.json`
- `eval/cost_report.json`
- `eval/preflight_report.json`

These are product artifacts. The eval-style `score.json` / weighted composite shapes some readers may expect are intentionally absent — Contremaitre uses a gate-first verdict (`READY_FOR_DRAFT_PR` / `NO_PR_*` / `FAILED_INFRA`) with an explanatory scorecard, not a single score.

## Lifecycle / cleanup

Per opencode-mode run, the orchestrator owns these external artifacts beyond the run directory:

- **Worktree** at `/tmp/contremaitre-<run-id>/` — removed by `_cleanup_worktree` in `finally`.
- **Detached containers** labeled `contremaitre.run-id=<id>` — agent / SIM / review / check / deps-install. `--rm` (one-shot) for the per-turn ones, explicit `docker rm -f` after `docker wait` for the streamed-log ones. `_stop_run_containers` runs in `finally` and on SIGTERM, scans by label, and `docker stop`s anything still alive.
- **Lockhash-keyed deps volume** `contremaitre-deps-<lockfile>-<digest>` — labeled `contremaitre.purpose=deps-cache`. **Kept** across runs by design (it's the cross-run dependency cache; populating it is the slowest step of a fresh target). Mounted RO at `/app/node_modules` in agent / SIM / check containers so parallel runs against the same lockfile can't poison each other.
- **Local clone cache** at `~/.cache/contremaitre/<host>-<owner>-<repo>/` — auto-managed; cloned lazily on first run from `--upstream` (or `--fork`). **Kept** across runs by design. Subsequent runs reuse it and `git fetch origin <base>` for freshness. `--repo-cache` overrides the path.
- **opencode state dirs** `opencode-{agent,sim,review}-state/` inside the run dir — kept on purpose: `_recover_text_from_sqlite` reads them when opencode silent-stalls, and they're the source of truth for forensic recovery. Not auto-pruned.

If a parent is SIGKILL'd, the worktree + label-tagged containers can survive. `contremaitre cleanup` scans `docker ps -a --filter label=contremaitre.run-id` for containers whose run-dir is gone, sweeps stale `/tmp/contremaitre-*` worktrees, and prunes dangling docker images. Pass `--deps` to also remove the lockhash-keyed deps volumes, `--repos` to nuke the local clone cache (next run will full re-clone). `contremaitre image build` runs `docker image prune -f` after a successful build so rebuilds with the same tag don't accumulate `<none>:<none>` orphans.

## Terminal signal

`.contremaitre/IMPLEMENTATION_COMPLETE` in the worktree ends the WORK loop. The marker is a Contremaitre scaffold — it is not part of the `improve-codebase-architecture` skill. The INITIAL_PROMPT tells the agent to write it only after SETTLED is locked, the implementation matches SETTLED, and the relevant local checks have been attempted.

`.contremaitre/SETTLED_DESIGN.md` is also a Contremaitre scaffold (the skill doesn't prescribe it). It is the design handoff artifact the review pass reads (from a pre-staged copy at `/review/SETTLED_DESIGN.md` in the REVIEW container), and the source of the host's commit title + body and the PR title + body.

`.contremaitre/*` is **excluded from the committed diff** via a pathspec exclude at `git add` time (`git add -- . ':(exclude).contremaitre'`). The files stay in the worktree across WORK rounds (so the WORK-phase SIM keeps reading SETTLED via `/app:ro` even after CHANGES_REQUESTED loops back), but never enter the published commit or PR — the SETTLED content is already carried by the commit body and PR description, so duplicating it in the target repo's history would just be noise. The clean-worktree hard gate filters `.contremaitre/*` accordingly via `_only_contremaitre_changes`.
