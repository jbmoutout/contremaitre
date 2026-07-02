# Control Plane

This is Contremaitre's implementation map. The control plane is deterministic Python on the host; the agent and SIM run inside per-run Docker containers and never hold git or GitHub credentials.

Each role runs one of three real **actor runtimes** — `opencode` (an OpenRouter/Zen model driven by the opencode CLI), `codex` (the operator's ChatGPT-subscription codex CLI driven headless), or `claude` (a Claude subscription via a headless OAuth token) — and the two roles can mix (e.g. a codex agent with an opencode SIM). The orchestrator, gates, and artifact contract are runtime-agnostic; see [Actor runtimes](#actor-runtimes) and the [CLI actor (codex / claude)](#cli-actor-codex--claude-auth--egress-lock) deep-dive.

Audience: humans modifying the orchestrator, and LLMs that need to reason about the system end-to-end. Read it top-to-bottom for orientation; the section anchors are stable for targeted lookup.

## Architecture

```
┌─ HOST ─────────────────────────────────────────────────────────────────────────┐
│                                                                                │
│   orchestrator.py ─── state machine, caps, marker watches, hard gates          │
│        │             cleanup labels, SIGTERM emergency-flush                   │
│        ├── git_utils.py ─── logged git wrapper (clone, fetch, commit, push)    │
│        ├── diffscan.py ──── forbidden-path scanner (.env, *.pem, *.key)        │
│        ├── verdicts.py ──── fence-tolerant strict-JSON parser, diff hash       │
│        ├── checks.py ────── --check-cmd runner (sidecar container)             │
│        ├── publisher.py ─── StubPublisher / GhPublisher (`gh pr create`)       │
│        ├── cli_reviewer.py  post-publish Docker CLI review loop helpers         │
│        ├── runtime_image.py lockhash-keyed deps volumes                        │
│        └── manifest.py ─── provenance: model IDs, image digest, prompt hashes  │
│                                                                                │
└────────────┬─────────────────────────────────────────────────────────────────┬─┘
             │                                                                 │
             │ docker (label=contremaitre.run-id=<id>)                         │
             ▼                                                                 ▼
┌─ DOCKER ──────────────────────────────────────┐     ┌─ EXTERNAL ───────────────┐
│                                               │     │                          │
│   agent container   role=agent      /app  RW    ─┼─►   │   OpenRouter             │
│   sim container     role=sim        /app  :ro   ─┼─►   │   / OpenCode Zen         │
│   review container  role=review     /review :ro ─┼─►   │                          │
│   cli-reviewer      role=cli_review /review :ro ─┼─►   │   model providers only   │
│   check sidecar     role=check      /app  RW   ◄─┼─── (host `gh pr create --draft`; │
│                                                 │      `gh pr comment` + status) │
│   deps-install      role=deps-install        │     │                          │
│   deps-clone        role=deps-clone          │     └──────────────────────────┘
│                                               │
│   Mount layout                                │
│     /app             → run worktree           │
│     /review          → host-built PR context  │
│     /app/<deps_mount>→ per-run deps volume    │
│                                               │
└───────────────────────────────────────────────┘
```

Containers are launched detached (`docker run -d`) and labeled with `contremaitre.run-id=<id>` and `contremaitre.role=<agent|sim|review|check|deps-install|deps-clone|deps-assert>` so `_stop_run_containers` + `contremaitre cleanup` can sweep them by label. The diagram above shows the `opencode` runtime; a `codex` role reuses the same role labels, mounts, and lifecycle — only the in-container binary and its egress posture differ ([Actor runtimes](#actor-runtimes)).

## Actor runtimes

The orchestrator is runtime-agnostic: it owns the WORK/REVIEW loop, gates, and container lifecycle, and delegates each turn to an **actor runner** chosen by [`make_actor_runner`](../contremaitre/actors.py). Three runtimes implement one `ActorRunner` protocol (`agent_turn` / `sim_turn` / `sim_review`):

- **`fake`** ([`fake_actor.py`](../contremaitre/fake_actor.py)) — deterministic fixtures for smoke runs; no containers, no model, no network.
- **`opencode`** ([`OpencodeActorRunner`](../contremaitre/actors.py)) — the opencode CLI driving an OpenRouter/Zen model inside the container. The default for real runs.
- **`codex` / `claude`** (`ActorMode.CLI`, [`CliActorRunner`](../contremaitre/cli_actor.py)) — a frontier CLI driven headless inside the container on the operator's subscription (no API key, no per-token API billing). `config.cli_tool` (`"codex"` | `"claude"`) selects which; both are baked into the image. The runner owns the shared orchestration and delegates the tool-specific seams (auth, in-container argv, event parsing, home) to a `CliDriver` — `CodexDriver` / `ClaudeDriver`.

**Per-role mixing.** The agent uses `config.actor_mode` + `config.cli_tool`; the SIM uses `config.sim_actor_mode` / `config.sim_cli_tool` when set, else the agent's. When the resolved (runtime, tool) pair differs between roles, `make_actor_runner` returns a [`CompositeActorRunner`](../contremaitre/actors.py) that routes `agent_turn` to one runner and `sim_turn` / `sim_review` to the other. This covers both axes of mixing: a CLI agent + a cheap opencode SIM (or the reverse), **and cross-CLI** — codex agent + claude SIM, or the reverse (two `CliActorRunner`s whose per-run homes are tool-namespaced, `codex-*-home` vs `claude-*-home`, so they never collide). Preflight validates the **union** of requirements: an OpenRouter key only if opencode is in play, and an auth check per *active* CLI tool (`_active_cli_tools`) — so a cross-CLI run validates both the codex token and that a claude credential resolves on the host; the egress lock applies to **codex** roles (claude carries no in-container token and runs open).

**Selection.** `--agent {fake,opencode,claude,codex}` + `--sim {opencode,claude,codex}` on the CLI (or `AGENT=` / `SIM=` in the Makefile). `claude` and `codex` map to `ActorMode.CLI` with the corresponding `cli_tool`; `opencode` maps to `ActorMode.OPENCODE`. A bare per-role model flag still feeds whichever runtime that role uses; a CLI tool ignores opencode-namespaced model names (see below).

## CLI actor (codex / claude): auth + egress lock

A subscription CLI runs headless in the per-run container. Two security-critical mechanisms make that safe; both live in [`cli_actor.py`](../contremaitre/cli_actor.py) and are shared across tools — only the auth seam (in the per-tool `CliDriver`) differs.

### Auth — codex: minimised subscription token

The codex access token is a ~10-day JWT, so the in-container credential outlives the run. `CodexDriver` hands the container the *least* usable form of it:

- **Neutered refresh token.** `~/.codex/auth.json` is copied into a per-run home with `tokens.refresh_token` overwritten by a dummy (`"x"`, `_NEUTERED_REFRESH_TOKEN`). codex's parser and refresh API both reject an *empty* refresh token, so it can't simply be dropped — but a valid access token in a writable home means codex never refreshes, so the dummy is inert. The real refresh token never enters a container.
- **Re-seeded every turn.** codex can delete `auth.json` on a failed refresh, so the home (mounted RW — codex writes PATH / app-server / sessions) is re-seeded from the host each turn via `CodexDriver.prepare_home`.
- **Host-side expiry gate.** If the access JWT has less than `_REFRESH_MARGIN_SECONDS` (1h) left, the host triggers a *host-side* refresh (`codex login status`, no model call) before launch; if it doesn't renew, the run refuses rather than letting codex attempt an in-container refresh the neutered token would fail. The margin only needs to outlast one container's wall-clock (the 1800s agent turn is the longest) plus headroom — it deliberately sits well under the ~10-day JWT life so a still-valid token isn't rejected hours early.

Preflight's `_check_codex_auth` confirms `~/.codex/auth.json` exists and isn't about to expire (codex CLI runs only).

### Auth — claude: host-injected, no in-container token

claude carries **no credential inside the container**. A host-side auth-inject proxy ([`cli_auth_proxy.py`](../contremaitre/cli_auth_proxy.py)) holds the token and swaps it in per request — the same host-owns-the-credential model as git/GitHub. `ClaudeDriver.container_env` points the container at the proxy: `ANTHROPIC_BASE_URL=http://host.docker.internal:<port>` plus a **dummy** `CLAUDE_CODE_OAUTH_TOKEN` (`"contremaitre-injected"` — keeps claude in subscription/OAuth mode so the interactive usage meter tracks `rate_limits.five_hour`/`seven_day`), a force-emptied `ANTHROPIC_AUTH_TOKEN` (prevents API-key-mode override), and a force-emptied `ANTHROPIC_API_KEY` (no fall-through to billed API). The proxy — a daemon thread in the host orchestrator process, bound to loopback — strips the dummy `Authorization` and injects the real bearer, then forwards to the pinned upstream `api.anthropic.com`. A `printenv` / `cat` in a compromised container yields only the dummy; the durable credential never enters it. (`IS_SANDBOX=1` is still set so claude permits `--permission-mode bypassPermissions` as root.)

The proxy resolves the token **live per request**, from the first source that has one: `CLAUDE_CODE_OAUTH_TOKEN` env (the documented default, from `claude setup-token`), then the macOS keychain (`Claude Code-credentials` → `claudeAiOauth.accessToken`), then `~/.claude/.credentials.json`. Live resolution means a rotated/short-lived token is picked up without restarting anything, and the same machinery works on Linux hosts where there is no keychain. The per-run mount is still **only** `/root/.claude/projects` (no credential file written; the image's baked `/root/.claude/skills` stays visible), and `ClaudeDriver.prepare_home` writes the generated `--settings` statusLine bridge. The exact subscription meter (a bounded no-tools interactive container after each successful turn, with a non-secret `/root/.claude.json` onboarding seed, the model from the turn's `system/init` event, and no `bypassPermissions`) authenticates through the **same** proxy — base-url + dummy, no token — and writes `rate_limits.five_hour` / `rate_limits.seven_day` into `.contremaitre/statusline.jsonl` for the host TUI. Preflight's `_check_claude_auth` confirms a credential *source* resolves on the host (not that any specific env var is set); `_check_cli_auth` dispatches on `cli_tool`.

On Docker Desktop the container reaches the loopback-bound proxy via `host.docker.internal`; on Linux the container is launched with `--add-host=host.docker.internal:host-gateway`.

> **Why claude, not codex.** codex's `chatgpt_base_url` is validated to chatgpt.com/localhost with a WebSocket-first responses transport, and it already neuters its durable refresh token (only a short-lived host-refreshed JWT enters the container) — so host-injection buys little for a lot of machinery. claude's bearer had no such bound, so removing it from the container is the high-value move. `cli_auth_proxy`'s provider registry is multi-provider so codex could slot in later.

### Egress — per tool: codex locked, claude open

Egress posture follows *what credential the container holds*, resolved per role by the driver (`CliActorRunner._egress_docker_flags`):

- **claude** holds no usable credential (the host auth-inject proxy adds the bearer), so there is nothing to exfiltrate — it runs **open egress** and only needs `host.docker.internal` to reach the proxy. The catastrophic credential-theft leg is gone; the residual is ordinary data exfiltration (repo contents) under open egress — the same class as any open run — plus inference quota the proxy can rate-limit. A practical upside: a claude role can `uv sync` / `npm install` ad-hoc tooling that the providers-only allowlist would block.
- **codex** still mounts a short-lived access token, so it stays **locked**. [`cli_egress.py`](../contremaitre/cli_egress.py) stands up a turnkey two-layer lock (`ensure_egress_proxy`), shared and idempotent across runs:
  1. an **`--internal` docker network** (`contremaitre-cli-egress`) — no route to the outside, no external DNS resolution (closing DNS-tunnel exfil), and
  2. a **squid allowlist proxy** (`contremaitre-egress-proxy`, dual-homed on the internal net + bridge) that is the network's sole exit and CONNECT-allows only the provider domains ([`cli_egress_squid.conf`](../contremaitre/cli_egress_squid.conf): `.chatgpt.com` / `.openai.com` for codex; `.openrouter.ai`, `.opencode.ai`, `.models.dev` for an opencode SIM on an OpenRouter *or* free Zen model). Everything else is denied.

A run can **mix** the two: the default `claude` agent + `codex` reviewer stands up both the auth-proxy (claude) and the squid lock (codex). The claude container ignores the internal lock network; the codex container joins it.

> **Locked ≠ full network (codex).** For a codex role the allowlist is providers-only — **package registries (PyPI / npm / GitHub / …) are NOT on it.** A locked codex run can't `uv sync` / `npm install` ad-hoc; opt into open egress with `--allow-open-egress` (or pre-bake the [deps volume](#deps-caching)). A claude role already runs open.

The codex lock is the **secure default, not mandatory** — `--allow-open-egress` is the explicit, warned override (codex's neutered refresh token bounds the accepted risk to ~10-day quota abuse). A post-publish CLI reviewer is governed by *its own* tool: a codex reviewer triggers the lock even when the agent is claude; a claude reviewer runs open even when the agent is codex. The layers (codex only):

| Layer | Function | Behavior |
|---|---|---|
| Host (pre-run) | `_maybe_provision_cli_egress` ([cli.py](../contremaitre/cli.py)) | Auto-provisions the network + proxy when a **codex** role is active (agent, SIM, or reviewer; an `auto` reviewer counts as possibly-codex) with no explicit `--docker-network`/`--https-proxy` and no `--allow-open-egress`. |
| Preflight | `_check_network_policy` ([preflight.py](../contremaitre/preflight.py)) | codex role + no policy → FAIL (refuse rather than run open); `--allow-open-egress` → WARN-passes; a claude-only run → PASS (open). |
| Runner (launch) | `_assert_egress_locked` ([cli_actor.py](../contremaitre/cli_actor.py)) | codex: launches if locked or `--allow-open-egress`, else refuses. claude: returns early (no token to contain). |

The squid proxy carries a `contremaitre.squid-sha256` label, so an edited allowlist auto-recreates it (the same staleness pattern as the image's `dockerfile-sha256`); it is kept across runs (static, secret-free). The host auth-inject proxy is per-process (a daemon thread bound to loopback), torn down in orchestrator cleanup (`stop_auth_proxies`).

### Model + reasoning effort

A CLI tool ignores opencode-namespaced model names (`openrouter/…`, `opencode/…`), so a CLI role takes its model from the tool's config field — **`config.codex_model`** (default `gpt-5.5`) or **`config.claude_model`** (empty → the `~/.claude` account default): a bare per-role `--agent-model`/`--sim-model` that is itself tool-native wins, anything namespaced (or empty) falls back. Effort is pinned on every turn — codex via an exec-level `-c model_reasoning_effort=<config.codex_effort>` (default `high`, `minimal|low|medium|high|xhigh`), claude via the `--effort <config.claude_effort>` flag (default `high`, `low|medium|high|max`). Set by `--codex-model`/`--codex-effort` or `--claude-model`/`--claude-effort`, with the Makefile variables carrying the common operator setup.

### Multi-turn

Like opencode's `--session`, a CLI tool carries context across separate `docker run`s: turn N writes session state into the persisted per-role home, turn N+1 resumes it by id. Both tools mint their own session id, so the runner captures it from the turn's stream and resumes by it on the next turn — codex via `codex exec … resume <id>`, claude via `claude … --resume <id>` (claude ignores a supplied `--session-id` in `-p` mode, so we don't set one). The per-role homes (`codex-{agent,sim,review}-home/` and `claude-{agent,sim,review}-home/` under the run dir) persist for exactly this; Claude homes are the mounted `projects` store plus the generated status-line bridge. The session id is stashed only after a *successful* turn, so a failed turn 1 retries fresh rather than resuming a session that was never written.

## Run flow (skill-aware)

The skill ([`improve-codebase-architecture`](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture)) drives the conversation; the orchestrator drives container lifecycle, marker watches, and gates. Inside `WORK`, every row below is one or more turns of the same multi-turn opencode session.

```
WORK SESSION (one opencode session — `agent_turn → sim_turn → agent_turn → …`)

  AGENT (role=agent, /app RW)                  SIM (role=sim, /app :ro)
  ───────────────────────────                   ───────────────────────

  Explore  ────────  read repo, glob, grep
    │ event: `turn`                            (no SIM turn yet)
    │ TUI phase: exploring
    │
  HTML cards
    │ writes .contremaitre/                   ◄── reads via /app:ro mount
    │   architecture-review.html
    │ convention (telemetry only —
    │   no harness check, no event)
    │
  Pick gate
    │ agent: "Which would you like              SIM: picks one + 2-3 sentence
    │   to explore?"                  ◄────►    justification in skill vocab
    │ event: `turn` (per turn,                 (Module / Seam / Depth …)
    │   role-tagged from actor JSONL)          forbidden operations: write /
    │ TUI phase: grilling                      edit / delete (mount is :ro)
    │
  Grill / Deepening
    │ N turns. Agent proposes               ◄────► SIM cites constraints,
    │   interfaces, pushes back on              names load-bearing details,
    │   SIM friction.                            pushes back. One turn = one
    │ counter: grilling_exchanges                complete reply (no
    │ (post-run, via flow_use.py)                placeholders).
    │
  Settle
    │ writes .contremaitre/                    SIM role shifts to drift-watch
    │   SETTLED_DESIGN.md
    │ HARNESS GATE — required for              (presence detected by
    │   REVIEW; missing → NO_PR                 filesystem poll —
    │                                           no dedicated event)
    │
  Implement
    │ M turns. Agent edits files.           ◄────► SIM reads each diff.
    │ counter: impl_turns                        "Faithful at x.ts:42" or
    │ TUI phase: implementing                    "Drift — new abstraction at
    │                                           y.ts:9, not in SETTLED."
    │
  CI gates
    │ runs project's test suite +              (no SIM turn)
    │   formatter/lint scoped to
    │   changed files. Discovers them
    │   via .github/workflows/,
    │   .pre-commit-config.yaml, etc.
    │ convention (telemetry: `tested ✗`)
    │
  Marker
    │ writes .contremaitre/                   SIM: "OK — handing off to review."
    │   IMPLEMENTATION_COMPLETE                yields turn
    │ HARNESS GATE — terminal signal           (presence detected by
    │   for the WORK loop                       filesystem poll;
    │ event: `work_session_end`                 `work_session_end` follows)

  Loop termination: marker present, cap trip (`turn_cap` / `wall_cap` /
  `recorded_cost_cap` / `no_progress_cap` events), or max_turns reached.

                              │
                              ▼

REVIEW round N  (N = 1 … max_review_rounds, default 3)

  REVIEWER container (role=review, /review :ro)
  ─────────────────────────────────────────────
  reads:  /review/diff.patch
          /review/SETTLED_DESIGN.md
  emits:  strict JSON
          { verdict, confidence, required_changes, checks_performed, summary }
  parser: verdicts.parse_sim_verdict — fence-tolerant
  event:  review_verdict

  ┌── APPROVED ─────────────────────────► hard gates → publish
  │
  ├── CHANGES_REQUESTED                    event: `revision_requested`
  │                                        clears IMPLEMENTATION_COMPLETE,
  │                                        sends required_changes summary
  │                                        back into the SAME opencode
  │                                        session (--session, agents.py
  │                                        threads `session_id` across
  │                                        review rounds), loops to WORK
  │
  └── NEEDS_HUMAN / malformed (after       NO_PR_NEEDS_HUMAN
      max_verdict_retries) / max_review_   NO_PR_CHANGES_REQUESTED
      rounds exhausted                     (event: `malformed_verdict` on
                                            each unparseable attempt)

                              │
                              ▼

HARD GATES (host, all must pass; deterministic)

  1. diff_scan          forbidden paths in diff?  (diffscan.py)
  2. diff_hash_matched  diff hash == hash at APPROVED?  (verdicts.diff_hash)
  3. clean_worktree     `git status --porcelain` clean except .contremaitre/*?
  4. draft_only         publication mode is `gh --draft`?  (always True;
                        belt-and-suspenders — published comes from publisher.py)
  5. executable checks  every --check-cmd passes in the sidecar container?
                        (skip if none configured → executable_confidence: null)

  Forbidden paths      .env, *.pem, *.key, and nested forms
  Forbidden exceptions .env.example, .env.sample, .env.template, .env.defaults

  All pass:  publisher.GhPublisher → `gh pr create --draft`
             then optional CLI review (claude / codex).
  Any fail:  NO_PR_NEEDS_HUMAN; PR not opened.

                              │
                              ▼

POST-PUBLISH CLI REVIEW LOOP  (only when --cli-reviewer != none)

  Runs in Docker (role=cli_review, /review :ro, provider-only CLI egress). /app is
  a throwaway copy of the worktree mounted rw + the deps volume, so the reviewer
  may run the project's tests offline to ground its findings; its edits are
  discarded (it emits markdown only, the published diff is untouched).
  Up to max_cli_review_rounds (default 3) rounds. cli_reviewer.py + _run_cli_review_loop():

    - host writes extras/cli_review_{n:03d}/input/ with:
        PR.md, pr.json, pr_body.md, diff.patch, changed_files.txt,
        SETTLED_DESIGN.md, previous_cli_reviews.md, head_sha.txt
    - prompt: prompts/cli_reviewer_prompt.md with {review_path, round_n, round_of}
    - runtime: CliActorRunner, session_attr=None → fresh session each round;
        reviewer reads `/review` + `/app:ro` and emits markdown only
    - GitHub stays host-owned: no GitHub credentials are passed into Docker;
        host posts `gh pr comment <pr_url> --body-file` after the reviewer exits
    - LOOKS_GOOD (all tools in the same round): loop exits → READY_FOR_DRAFT_PR;
        worst verdict projected as commit status → success
    - NEEDS_ATTENTION or MUST_FIX: extract `## Required changes` numbered list,
        send cli_revision_followup() (tagged [CLI]) to agent on same branch,
        require a fresh IMPLEMENTATION_COMPLETE marker, commit, rerun L1 checks
        (every --check-cmd in the sidecar container), rerun L0 gates: diff_scan
        (forbidden paths), diff_hash_matched (hash stable across checks),
        clean_worktree (git status clean except .contremaitre/*), draft_only
        (always passes post-publish); push HEAD → branch only if all pass,
        then next round
    - resync on divergence: if the revision push is rejected non-fast-forward
        (a concurrent writer — e.g. a CI auto-formatter — advanced
        origin/<branch> during the reviewer round), the host fetches that tip,
        rebases the revision onto it, re-runs L0+L1 on the merged tree (its
        diff, and thus diff_hash, legitimately grows to include the remote's
        commits), and retries the now-fast-forward push. Bounded to
        MAX_PUSH_ATTEMPTS (=3); force-push stays forbidden. Emits
        cli_review_loop_resync per rebase. A rebase conflict / fetch failure /
        exhausted retries aborts to PR_NEEDS_HUMAN with the specific reason.
    - revision gate or push failure after publication → PR_NEEDS_HUMAN;
        existing PR remains published for human follow-up
    - max_cli_review_rounds exhausted without all-LOOKS_GOOD → PR_NEEDS_HUMAN;
        worst verdict projected as commit status
    - tool failure (exception or empty output): logged, that tool skipped for
        the round; does NOT abort the loop
    - commit status (context `contremaitre/cli-review`):
        MUST_FIX → state=failure; everything else → success. PAT-viable (the
        Checks API would need a GitHub App). Require the context in branch
        protection to gate merge on it.

  Verdict format: prompt enforces `<glyph> KEY — one-sentence justification`
    on line 1 of the markdown output. Parser scans the first 5 lines
    defensively (containment check, in case the agent prepends stray text):
    LOOKS_GOOD       → loop done
    NEEDS_ATTENTION  → revision triggered
    MUST_FIX         → revision triggered
    (missing/failure)→ treated as revision trigger, no comment posted

  Artifacts per round stored under extras/cli_review_{n:03d}/:
    input/                    host-built PR context mounted as /review:ro
    {tool}_raw_export.jsonl   raw CLI stream
    {tool}_review.md          H3 header + review body (posted as PR comment)
```

## State machine reference

The orchestrator's POV. `State` enum in [models.py](../contremaitre/models.py).

```
INIT  ──►  WORK  ──►  REVIEW  ──►  APPROVED  ──►  (hard gates + publish)
                              ↘
                              CHANGES_REQUESTED  ──►  WORK  (up to max_review_rounds)
                              ↘
                              NO_PR
                              FAILED
```

- **INIT** — worktree creation, deps volume provisioning, the offline-readiness assert (`assert_deps_offline`), preflight. Transition to WORK on success.
- **WORK** — one multi-turn opencode session. Terminates on `.contremaitre/IMPLEMENTATION_COMPLETE` (harness gate), cap trip, or `max_turns`.
- **REVIEW** — single-shot reviewer container (role=review) reads `/review/diff.patch` + `/review/SETTLED_DESIGN.md`, emits JSON verdict.
- **APPROVED** — runs hard gates, then the publisher. Success → `READY_FOR_DRAFT_PR`.
- **NO_PR** — terminal without publication.
- **FAILED** — infrastructure exception; the SIGTERM handler also routes here.

## Terminal verdicts

All six values from `TerminalVerdict` ([models.py](../contremaitre/models.py)); triggers live in [orchestrator.py](../contremaitre/orchestrator.py):

| Verdict | Trigger |
|---|---|
| `READY_FOR_DRAFT_PR` | APPROVED + hard gates pass + checks pass + publisher succeeds + CLI review loop all-LOOKS_GOOD (or skipped) |
| `PR_NEEDS_HUMAN` | PR published but CLI review loop exhausted `max_cli_review_rounds` without all-LOOKS_GOOD, or a post-publish revision could not pass gates / push safely. Yellow: PR exists on GitHub, a human should review before merging. |
| `NO_PR_CHANGES_REQUESTED` | `max_review_rounds` exhausted on CHANGES_REQUESTED |
| `NO_PR_NEEDS_HUMAN` | NEEDS_HUMAN verdict / malformed verdict (after retries) / missing SETTLED / missing IMPLEMENTATION_COMPLETE / cap trip / hard-gates fail / executable check fail |
| `FAILED_INFRA` | Unhandled exception during run; SIGTERM |
| `QUOTA_EXHAUSTED` | `ActorError` with `kind == PROVIDER_QUOTA_EXHAUSTED` (e.g. OpenCode Zen `FreeUsageLimitError`). Distinct from FAILED_INFRA so the eval canary aborts the n=3 batch instead of retrying. |

## Host-owned boundaries

The agent and SIM never hold:

- git credentials. `git_utils.py` runs on the host (clone, fetch, commit, push).
- a GitHub token. `publisher.GhPublisher` runs `gh pr create --draft` on the host.
- a claude credential. The token stays on the host in the auth-inject proxy ([`cli_auth_proxy.py`](../contremaitre/cli_auth_proxy.py)), which adds the bearer per request; the container holds only a dummy `CLAUDE_CODE_OAUTH_TOKEN` + a base-url. (codex still mounts a *neutered* short-lived token — see below.)

Read-only enforcement is belt-and-suspenders:

1. SIM container mounts `/app:ro`.
2. SIM persona (`prompts/sim_tooled_persona.md`) forbids write/edit/delete operations — runtime-agnostic so the persona works for Codex (`exec_command`) and Claude/opencode (`Bash`, `Read`, etc.).
3. Host `diffscan.py` blocks publication if forbidden paths appear in the diff.

Opencode containers see only `OPENROUTER_API_KEY` (when set) and the proxy variables passed via CLI flags. Ambient host env is never inherited. When `OPENROUTER_API_KEY` is absent, runs default to free OpenCode Zen models served by OpenCode; the container's `OPENROUTER_API_KEY` is simply not exported.

Codex containers hold no OpenRouter key at all — only a neutered copy of the subscription token (a mounted home with `tokens.refresh_token` dummied) and the egress-proxy variables. Claude containers hold no provider credential at all (the host proxy injects it). The same git/GitHub host-ownership holds for both: a CLI agent edits the worktree but never pushes or opens the PR. See [CLI actor (codex / claude)](#cli-actor-codex--claude-auth--egress-lock) for codex's token-minimisation + locked egress and claude's host-injected auth + open egress.

## Preflight

Live opencode and CLI runs run preflight before worktree creation; the report is persisted to `eval/preflight_report.json`. `contremaitre doctor` runs the same checks without starting a run. Checks are the **union** of the active runtimes (per-role), so a mixed run validates both opencode and CLI prerequisites.

**Blocks the run:**

- missing target repo / base ref;
- missing Docker daemon or target image;
- opencode binary failures inside the image (opencode roles);
- failed `:ro` mount enforcement test;
- open container egress with no network/proxy configured and `--allow-open-egress` unset (opencode) — a **codex** role additionally fails if its default egress lock couldn't be auto-provisioned and `--allow-open-egress` wasn't passed (it refuses rather than running a codex container open); a **claude** role runs open by design (no in-container token);
- a missing or near-expiry codex subscription token (`~/.codex/auth.json`, codex roles), or no claude credential resolving on the host (env `CLAUDE_CODE_OAUTH_TOKEN` / keychain / `~/.claude/.credentials.json`, claude roles) — `_check_cli_auth` dispatches on `cli_tool`;
- missing, unlimited, over-cap, or unverified OpenRouter key (opencode roles, when key is required).

**Warns (does not block):**

- OpenRouter key limit excludes BYOK usage (acceptable for non-BYOK models).
- a frontier CLI baked into the image lags npm (`_check_cli_freshness`, active CLI tools). The CLIs are installed by an unpinned `npm i -g` (see [the runtime image](#runtime-image)) and nothing refreshes them at launch, so the in-image `claude` / `codex` can silently fall behind — and a just-released model fails until rebuilt. The check compares the in-image version (`docker run … --version`) against the npm registry (`registry.npmjs.org/<pkg>/latest`, 6h on-disk cache) and WARNs — never FAILs (a stale CLI usually still works) — with the exact rebuild command (`contremaitre image build --no-cache`, `--variant` for non-base tags). "Couldn't read the in-image version" and "npm unreachable" both WARN too. Surfaced in `doctor` / the report **and** as a row on the interactive pre-Y/n screen (skipped there when the image isn't built yet).

**Bypass flags** (loud on purpose):

- `--skip-openrouter-key-check` — don't query the key endpoint.
- `--allow-unlimited-openrouter-key` — accept a key without a credit limit.
- `--allow-open-egress` — accept unrestricted container egress.

## Launch sequence

On TTY runs `_run_cmd` walks through:

1. **Clone** — `_ensure_local_clone` fetches `origin/<base>` into the repo cache (lazy, auto-created).
2. **Zen model picker** — *opencode roles only, when `--agent-model` / `--sim-model` are absent and stdin is a TTY.* Numbered list of OpenCode Zen free models; a paste box for OpenRouter slugs. Non-TTY + opencode + no model → abort with an explicit error. CLI roles (claude/codex) skip this step entirely.
3. **Pre-flight presence check** — one line per active role (agent, SIM, cli-reviewer):
   - opencode role: liveness probe via `_probe_zen_model()` — surfaces `FreeUsageLimitError` before the run starts. A paid (non-Zen) model also gets an `OPENROUTER_API_KEY` presence row; free Zen models need no key.
   - CLI role: token presence check (codex: `~/.codex/auth.json` exists / not about to expire, else `codex login`; claude: `CLAUDE_CODE_OAUTH_TOKEN` set). On a TTY, a missing claude token triggers guided onboarding — offer to run `claude setup-token`, then write the pasted token to `./.env` (`upsert_env_var`) and export it for the in-flight run. Full auth validation (expiry, key limits, network) runs inside `run()` after confirmation.
4. **Recap + Y/n** — one-line summary: roles, models, target, branch, network posture. `Continue? [Y/n]` on TTY; non-TTY proceeds automatically.
5. **Egress provision** — `_maybe_provision_cli_egress` if a CLI role is active with no explicit egress override.
6. **Run** — `run(config)` — full preflight, worktree, orchestrator.

All modes still emit `[info]` log lines so the run log explains what was assumed.

## Model selection

For an **opencode** role, two model sources are picked at launch from a single TTY picker ([cli.py](../contremaitre/cli.py)):

- **OpenCode Zen** — free models served by OpenCode itself. Catalog is fetched live at launch via `_fetch_free_models()` from `https://models.dev/api.json` (the same source the opencode binary uses, so the picker never offers a slug the binary will reject). Filtered to entries with `-free`-suffixed IDs plus a small allow-list (e.g. `big-pickle`). Slugs are `opencode/<id>`. No auth — the opencode binary has built-in routing to Zen. Quota probe hits `https://opencode.ai/zen/v1/chat/completions` to surface `FreeUsageLimitError` before the run starts. Why not OpenRouter `:free` slugs: those route through third-party providers whose daily quota is shared across all OpenRouter users, producing `"Out of credits"` mid-run.
- **OpenRouter** — paid models. Requires `OPENROUTER_API_KEY` (`.env`, cwd or repo root; never inherited from ambient host env). Any `openrouter/<provider>/<model>` slug can be pasted at the picker prompt. Preflight does `GET https://openrouter.ai/api/v1/key` and blocks the run if the key has no credit limit (unless `--allow-unlimited-openrouter-key`).

For a **codex** role there is no picker: the model is `config.codex_model` (default `gpt-5.5`, settable via `--codex-model` or `CODEX_MODEL` in the Makefile) and reasoning effort is `config.codex_effort` (default `high`, `minimal|low|medium|high|xhigh`). codex rejects opencode-namespaced names on a ChatGPT account, so a namespaced `--agent-model`/`--sim-model` is ignored for that role and the codex default is used; only a bare codex-native name passes through. No API key — codex runs on the operator's subscription. See [CLI actor → Model + reasoning effort](#model--reasoning-effort).

Non-interactive opencode runs: pass `--agent-model` explicitly (or set `AGENT_MODEL` in the Makefile) to skip the picker. When an opencode agent uses an explicit model and the SIM also uses opencode, an omitted `--sim-model` mirrors the agent model; a CLI agent with an opencode SIM must pass `--sim-model`. On TTY without a model, the picker appears.

Containers see `OPENROUTER_API_KEY` only when set on the host. The opencode binary reads the key when invoking an OpenRouter model; for Zen models the key is unused. Provider-side spend caps remain the real guardrail — the `--max-cost-usd` flag is a *recorded-cost* watcher, not a hard budget enforcer.

## Module map

Every `.py` under [contremaitre/](../contremaitre/). One line each — the code itself is the long form.

- [`actors.py`](../contremaitre/actors.py) — `ActorRunner` protocol, `FakeActorRunner` + `OpencodeActorRunner`, the `make_actor_runner` factory, and `CompositeActorRunner` (routes the agent turn to one runtime and SIM/review turns to another for a mixed run). Opencode containers run detached with role labels; output streamed via `docker logs -f`, exit awaited via `docker wait`.
- [`checks.py`](../contremaitre/checks.py) — `--check-cmd` runner. Every real runtime (opencode AND cli) runs each check in a sidecar container with the run's worktree + deps volume, joined to the agent's `docker_network`, 600s timeout — so the gate is hermetic and executes under the same toolchain + egress the agent faced (a codex gate runs under the lock, offline, against the warmed deps). Only FAKE mode (no docker) runs on the host. The sidecar is credential-free (no token/home), so it's safe under the lock.
- [`cli.py`](../contremaitre/cli.py) — argparse, subcommand dispatch (`run`, `doctor`, `models`, `fixture`, `image`, `tui`, `cleanup`, `eval`), auto-derived clone cache at `~/.cache/contremaitre/<host>-<owner>-<repo>/`, slim launch sequence (Zen picker → pre-flight presence check → recap+Y/n → egress provision), codex egress auto-provision (`_maybe_provision_cli_egress`), image staleness rebuild (compares `contremaitre.dockerfile-sha256` label).
- [`cli_actor.py`](../contremaitre/cli_actor.py) — `CliActorRunner` + the `CliDriver` abstraction (`CodexDriver` / `ClaudeDriver`): drives `codex` or `claude` headless in the per-run container as agent / SIM / reviewer. The runner owns shared orchestration (egress lock, per-run home, detached run + stdout→raw_export, timestamp back-fill, session-attr, transcript, docker wrapper); each driver owns its auth (codex: neutered refresh token, per-turn re-seed, host-side expiry refresh / claude: no in-container token — base-url + dummy bearer, with the real token injected by the host `cli_auth_proxy`), in-container argv, and event parsing. See [CLI actor (codex / claude)](#cli-actor-codex--claude-auth--egress-lock).
- [`egress.py`](../contremaitre/egress.py) — single source of truth for CLI egress posture: `CREDENTIAL_BEARING_CLI_TOOLS` + `is_credential_bearing` / `cli_tool_locked` / `any_locked_cli_tool`. The one rule (codex token-bearing → locked; claude host-injected → open) that provisioning (`cli.py`), preflight, the runner's refuse-to-launch guard, and the docker flags all read from so it can't drift.
- [`cli_egress.py`](../contremaitre/cli_egress.py) (+ [`cli_egress_squid.conf`](../contremaitre/cli_egress_squid.conf)) — turnkey two-layer egress lock for codex: an `--internal` docker network + an allowlist squid proxy (`ensure_egress_proxy`). Idempotent + shared across runs; recreates the proxy on squid-conf hash drift (`contremaitre.squid-sha256` label).
- [`cli_reviewer.py`](../contremaitre/cli_reviewer.py) — post-publish CLI review loop helpers: prompt assembly (`build_prompt` with round context), verdict parsing (`parse_verdict`, `extract_required_changes`), model extraction, H3 metadata header (`format_header`), `gh pr comment` posting, worst-of-N verdict → `gh api` commit-status projection (context `contremaitre/cli-review`). The reviewer itself runs via `CliActorRunner.cli_reviewer_turn()` in Docker; this module owns only the stateless helpers + host-side `gh` calls.
- [`costs.py`](../contremaitre/costs.py) — recorded-cost extraction from JSONL streams; provider-side limits remain the real guardrail.
- [`diffscan.py`](../contremaitre/diffscan.py) — deterministic forbidden-path scanner against the working diff.
- [`envfile.py`](../contremaitre/envfile.py) — dependency-free `.env` loader; shell env wins, never overwritten.
- [`eval.py`](../contremaitre/eval.py) — v0 regression canary against `golden_cases/<id>/`. Subprocess-invokes `contremaitre run --agent opencode` so the production launch path is canaried as-is. Extracts a two-layer scorecard (headline + diagnostic) from artifacts the orchestrator already writes, aggregates n samples into a cell, compares against the (case, config) baseline. Also owns `cmd_ab` — the two-config head-to-head: launches both arms interleaved (A,B,A,B,…) so provider drift spreads across arms, then delegates the comparison report to `viewer/ab.py`. Generalizable methodology principles: [golden_cases/README.md](../golden_cases/README.md#methodology-notes).
- [`evaluator.py`](../contremaitre/evaluator.py) — gate-first PR-eval writer + non-blocking flow-use observability. `executable_confidence` is `null` (not `0.0`) when no `--check-cmd` is configured.
- [`events.py`](../contremaitre/events.py) — guardrail-event name constants. Single source of truth so writer + reader stay aligned at import time.
- [`extract.py`](../contremaitre/extract.py) — post-run artifact extraction: subagent markdown files (one per `task` tool-use), files written via `write` / `edit` / `apply_patch`, edit-accumulation `.edits.md` files, scaffold salvage.
- [`fake_actor.py`](../contremaitre/fake_actor.py) — deterministic fake agent/SIM for fixture smoke runs.
- [`fixture.py`](../contremaitre/fixture.py) — local fixture repo creator for smoke tests.
- [`flow_use.py`](../contremaitre/flow_use.py) — agent + SIM tool-use observability (`time_to_settled_design`, `self_verified`, `grilling_exchanges`, `impl_turns`, `sim_useful_call_ratio`, `runtime_install_required`) and the **run-signal predicates** (`marker_writes` / `self_verification` — anchored detection of the SETTLED / IMPLEMENTATION_COMPLETE / architecture-review writes, opencode + claude streams). The one implementation behind the live TUI chrome, the phase split, and the eval scorecard, so they cannot disagree about the same run; phase anchors use the *first* marker write, the self-verification bound the *last* (revision rounds rewrite the marker).
- [`gates.py`](../contremaitre/gates.py) — the **Hard gates (L0)** Module. `evaluate_l0()` runs the deterministic L0 recipe (diff-hash match, forbidden-path scan, clean worktree, payload assembly) and returns a typed `L0GateResult`; both the pre-publish gate and the post-publish revision gate in `orchestrator.py` call it. Owns the **internal-path policy** (`INTERNAL_PATHS` + `is_internal_path` + `only_internal_changes`) shared by the clean-worktree gate and the host-commit excludes. L0 only — L1 executable checks and the `HARD_GATES_CHECKED` emit stay caller-side, since the two call sites combine L1 and project telemetry differently.
- [`git_utils.py`](../contremaitre/git_utils.py) — logged git command wrapper. Every invocation appended to `git_log.jsonl` for forensic audit.
- [`jsonlog.py`](../contremaitre/jsonlog.py) — append-only JSONL + JSON-write helpers.
- [`manifest.py`](../contremaitre/manifest.py) — provenance manifest: per-role `ModelSpec` identity dicts, image digest, dockerfile-sha256, skills-lock hash, prompt hashes, contremaitre git SHA + dirty flag, python + contremaitre versions. Tolerates missing tools (returns `None`, never raises). `manifest_digest()` hashes the fields that define "the system under test" — each role's `ModelSpec.canonical()` + effort, behind a `DIGEST_VERSION` token so a contract change resets baselines deliberately.
- [`models.py`](../contremaitre/models.py) — `State`, `ReviewVerdict`, `CliReviewVerdict`, `TerminalVerdict`, `ActorMode` (`fake` / `opencode` / `cli`), `PublishMode` enums; `ModelSpec` (canonical model identity — atomic fields, derived `display()` / `canonical()`, one `for_role` factory, one `from_record` reader that absorbs legacy on-disk strings); `RunConfig` (incl. `actor_mode`, `sim_actor_mode`, `cli_tool`, `codex_model`, `codex_effort`, `claude_model`, `claude_effort`), `RunPaths`, `Caps`, `DepsVolume`, `ParsedVerdict`, `RunResult`, `RunOutcome` (the value the two no-PR terminals hand to `orchestrator._finalize`; `gate` is `L0GateResult | None`, so a run that ended before L0 records `hard_gates="NOT_EVALUATED"`, never a fabricated failure) dataclasses. The stable seam between CLI, orchestrator, and actors.
- [`orchestrator.py`](../contremaitre/orchestrator.py) — state machine, caps, worktree lifecycle, WORK loop, review loop, host-side commit (with SETTLED-derived title + body), publication gate, label-driven cleanup, SIGTERM emergency-flush, post-publish CLI review hook (incl. worst-of-N commit-status projection).
- [`paths.py`](../contremaitre/paths.py) — slug validation, run-id generation, contained-path builder (prevents escape outside `run_dir`).
- [`preflight.py`](../contremaitre/preflight.py) — operational checks for live opencode + CLI runs, validated as the per-role union plus the post-publish CLI reviewer: repo/base ref, Docker image, `:ro` mount, network policy (CLI defaults to locked, `--allow-open-egress` overrides), OpenRouter key bounds (opencode), codex / claude auth checks for active CLI tools, CLI freshness vs npm (active CLI tools, WARN-only). See [Preflight](#preflight).
- [`prompts/`](../contremaitre/prompts/) — `initial_prompt.md` (agent's first turn), `sim_tooled_persona.md` (SIM's first turn), `sim_review_prompt.md` (single-shot review), `cli_reviewer_prompt.md` (post-publish review). Markdown is the source; `prompts/__init__.py` loads them.
- [`publisher.py`](../contremaitre/publisher.py) — publication boundary: `StubPublisher` (dry-run) vs `GhPublisher` (real `gh pr create --draft`). PR title + body derived from `.contremaitre/SETTLED_DESIGN.md` + SIM verdict summary; `--pr-title` / `--pr-body` override.
- [`runtime_image.py`](../contremaitre/runtime_image.py) — lockhash-keyed deps caching (see below).
- [`tui.py`](../contremaitre/tui.py) — read-only Textual TUI tailing JSONL artifacts. 7-phase footer (init → exploring → grilling → implementing → reviewing → cli_review → done) + SIM reviewer status glyphs + CLI review loop status + warning tokens + subscription-window usage (codex rollout snapshots / claude statusLine snapshots) + verdict badge.
- [`verdicts.py`](../contremaitre/verdicts.py) — strict SIM verdict parser (fence-tolerant JSON extraction) and `diff_hash()` used by the diff-hash gate.
- [`viewer/`](../contremaitre/viewer/) — single-file run viewer (`viewer.html`) over the JSONL artifacts (transcript, timeline, sub-agents, written files, guardrail events, eval reports). Built by the orchestrator's `finally` so it lands on success and failure. Companion [`viewer/index.py`](../contremaitre/viewer/index.py) scans a runs root for `viewer.html` files and emits `index.html` — one summary card per run (verdict, models, PR link, cost, duration), newest first — rebuilt at the end of every run so the dashboard is always current. [`viewer/ab.py`](../contremaitre/viewer/ab.py) renders the `eval ab` head-to-head report (`ab--<case>--<a>-vs-<b>.html` at the runs root): provenance + validity checklist, every `check_run` scorecard metric with median [min–max] + per-run values + a range-separation signal (infra-failed runs badged in the roster, excluded from metric vectors), per-run cards linking into each run's viewer.

## Artifact contract

Every opencode-mode run writes to `<runs_root>/<run-id>/`. The control plane is additive — readers must use `.get()`-style access. Paths are registered in [models.RunPaths](../contremaitre/models.py).

### Conversation streams

- `initial_prompt.txt` — agent's turn-1 message
- `raw_export.jsonl` — agent JSONL stream
- `sim_raw_export.jsonl` — SIM JSONL stream
- `extras/cli_review_{n:03d}/input/` — host-built PR context mounted read-only at `/review` for the post-publish CLI reviewer (`PR.md`, `pr.json`, `pr_body.md`, `diff.patch`, `changed_files.txt`, `SETTLED_DESIGN.md`, `previous_cli_reviews.md`, `head_sha.txt`)
- `extras/cli_review_{n:03d}/{tool}_raw_export.jsonl` — per-round CLI reviewer stream (only when `--cli-reviewer` is set)
- `extras/cli_review_{n:03d}/{tool}_review.md` — per-round posted PR-comment body, H3 metadata header + review text
- `claude-*-home/.contremaitre/statusline.jsonl` — claude CLI roles only; whitelisted Claude Code status-line snapshots used by the TUI footer for exact 5-hour / 7-day Claude.ai subscription-window usage when the account exposes those fields. Populated by the post-turn no-tools statusLine meter for the role's active Claude model, not by the main `claude -p` event stream.
- `claude-*-home/.contremaitre/statusline_meter_*.log` — claude meter stdout / TTY diagnostics when the best-effort meter cannot populate a snapshot.

### Transcript + state

- `transcript.md`
- `timeline.jsonl` — state transitions + turn markers
- `trajectory.json` — final state sequence
- `stats.json` — run summary
- `git_log.jsonl` — every git command invoked
- `test_runs.jsonl` — `--check-cmd` results
- `review_cycles.jsonl` — one entry per review round
- `worktree_state.jsonl` — git-status snapshots
- `guardrail_events.jsonl` — per-turn lifecycle + `check_started`/`_completed`, `host_commit_created`, `review_verdict`, `hard_gates_checked`, `published`/`publication_blocked`, `cli_review_started`/`_completed`/`_failed`/`_status`, `worktree_removed`
- `recoveries.jsonl` — sqlite-recovery / SIGTERM-emergency events
- `pr.json` — publication outcome

### Extracted from agent activity

- `subagents/agent_NN_<slug>.md` — one per `task` tool-use; populated by `extract.py` in the orchestrator's `finally`
- `extracted_files/<host_name>` — every file the agent wrote via `write` / `edit` / `apply_patch`
- `extracted_files/<host_name>.edits.md` — accumulated edit history when the file went through `edit` ([extract.py](../contremaitre/extract.py))
- `extracted_files/.contremaitre__<name>` — worktree-scaffold salvage (the `.contremaitre/` markers the agent wrote) ([extract.py](../contremaitre/extract.py))

### Eval (gate-first scorecard)

- `eval/pr_eval.json`, `eval/pr_eval.md`
- `eval/checks_report.json`
- `eval/settled_diff_report.json`, `eval/architecture_delta_report.json` — currently `PENDING` placeholders
- `eval/trajectory_report.json`
- `eval/flow_use.json` — tool-use observability (`time_to_settled_design`, `self_verified`, `grilling_exchanges`, `impl_turns`, `sim_useful_call_ratio`, …)
- `eval/judge_attempts.jsonl` — path is registered in [models.RunPaths](../contremaitre/models.py) but **not currently written**; reserved for future per-attempt verdict logging (cross-judge replay, malformed-retry audit). Treat as a known gap.
- `eval/cost_report.json`
- `eval/preflight_report.json`
- `eval/canary.json` — only when driven by `contremaitre eval run`

### Provenance + viewer

- `run_config.json` — provenance manifest ([manifest.py](../contremaitre/manifest.py))
- `viewer.html` — self-contained run viewer (built in orchestrator's `finally`; lands on success and failure)

The runs root also gets a top-level **`index.html`** rebuilt on each run, summarising every run under it ([viewer/index.py](../contremaitre/viewer/index.py)), and — when the operator runs `eval ab` — one **`ab--<case>--<a>-vs-<b>.html`** head-to-head comparison per config pair ([viewer/ab.py](../contremaitre/viewer/ab.py)).

## Runtime image

The base image ([`contremaitre/Dockerfile`](../contremaitre/Dockerfile)) is generic opencode-in-Docker — no target codebase baked in — and also ships the frontier CLIs the codex/CLI actor drives. Layers:

- `node:24-bookworm-slim` plus `git`, `curl`, `jq`, `python3` + venv + pip.
- [`uv`](https://docs.astral.sh/uv/) at `/root/.local/bin/uv` and [`poetry`](https://python-poetry.org/) via pip — used by `runtime_image.ensure_deps_volume`'s install one-shots, and by agents running `uv run …` / `poetry run …` against the worktree.
- [`opencode`](https://opencode.ai/) at `/root/.opencode/bin/opencode` — the actor binary the host invokes for an opencode role.
- [`@openai/codex`](https://github.com/openai/codex) + [`@anthropic-ai/claude-code`](https://github.com/anthropics/claude-code) (npm-global) plus `ripgrep` — the frontier CLIs `ActorMode.CLI` drives headless on the operator's subscription (`codex` on a ChatGPT plan, `claude` on a Claude plan). ripgrep is what both CLIs reach for by default (without it codex falls back to slower grep).
- [`mattpocock/skills`](https://github.com/mattpocock/skills) installed globally via `npx -y skills@latest add … --all --global`, so `/improve-codebase-architecture` is on-PATH for opencode regardless of which target repo is mounted.

The image is built with a `contremaitre.dockerfile-sha256=<sha>` label, so `_ensure_default_image_built` in [cli.py](../contremaitre/cli.py) detects Dockerfile drift and auto-rebuilds before the next run.

**Variants** chain `FROM contremaitre-agent:latest` and add their toolchain:

- [`Dockerfile.rust`](../contremaitre/Dockerfile.rust) — `rustup` stable, minimal profile, docs stripped. Build with `contremaitre image build --variant rust` (tags `contremaitre-agent-rust:latest`); use via `--docker-image contremaitre-agent-rust:latest` or the `just rust` preset. Crate deps download at check time over `--allow-open-egress`; a `CARGO_HOME` volume cache is feasible but unimplemented.
- [`Dockerfile.go`](../contremaitre/Dockerfile.go) — Go `1.23.4` toolchain at `/usr/local/go`. Build with `contremaitre image build --variant go` (tags `contremaitre-agent-go:latest`). Module deps land in the lockhash-keyed `contremaitre-deps-<project>-go-sum-<digest>` volume; `GOPATH` points at it.

Each variant runs a smoke check (`rustc --version`, `go version`) at build time so a broken install fails fast.

## Deps caching

`runtime_image.py`. The agent shouldn't pay an `npm ci` / `uv sync` tax every run; lockfile-keyed volumes amortise it.

**Lockfile → ecosystem matrix:**

| Lockfile | Install command | Mount path | Runtime env |
|---|---|---|---|
| `package-lock.json` | `npm ci --no-audit --no-fund` (falls back to `npm install` on an EUSAGE lock-sync refusal — see below) | `node_modules` | — |
| `pnpm-lock.yaml` | `corepack pnpm install --frozen-lockfile` | `node_modules` | — |
| `yarn.lock` | `yarn install --frozen-lockfile --non-interactive` | `node_modules` | — |
| `poetry.lock` | `POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --no-root` | `.venv` | `VIRTUAL_ENV=/app/.venv` |
| `uv.lock` | `uv sync --frozen` | `.venv` | `VIRTUAL_ENV=/app/.venv`, `UV_NO_SYNC=1` |
| `requirements.lock` | `uv venv .venv && uv pip install --no-deps -r requirements.lock` | `.venv` | `VIRTUAL_ENV=/app/.venv`, `UV_NO_SYNC=1` |
| `Cargo.lock` | `cargo fetch` | `.cargo-cache` | `CARGO_HOME=/app/.cargo-cache` |
| `go.sum` | `go mod download` | `.go-mod-cache` | `GOPATH=/app/.go-mod-cache` |

**Pristine volume** `contremaitre-deps-<project>-<lockfile>-<digest>-<recipe>`, labeled `contremaitre.purpose=deps-cache` + `contremaitre.project=<project>`. Populated once per (project, lockfile sha-256[:12], install-command hash) by a one-shot install container. The `<recipe>` segment (`_recipe_tag`, a hash of the install command) means editing a recipe forces a fresh volume rather than silently reusing one built by the old command — without it, a recipe fix no-ops against any cached volume whose lockfile digest is unchanged. The prefix-scoped prune (`<project>-<lockfile>-`) then sweeps the superseded volume on the same run, so a recipe change self-heals without `cleanup --deps`. Install mount path *equals* the runtime mount path so embedded paths (e.g. uv's `#!/app/.venv/bin/python` shebangs) resolve later.

**Per-run volume** `contremaitre-run-<run-id>-deps`, labeled `contremaitre.purpose=deps-run` + `contremaitre.run-id=<run-id>`. Cloned from the pristine via a one-shot `cp -a /src/. /dst/` (typical 5–15s). The agent can freely install into it without leaking writes into the pristine or into the next run.

**Role-aware mount (`deps_mount_mode`, single home for `actors.py` + `cli_actor.py`).** Deps follow *execution*: the **agent** mounts the volume RW at `/app/<mount_path>` (it runs the project's tests to self-verify, and may install); the **SIM** mounts it RO (reasons over the diff, never installs); **review / cli_review** get *no* deps mount at all. Test execution is a separate deterministic gate (the agent's self-verify + the L1 `check` sidecar), never an LLM reviewer's job — so the reviewers stay deps-free, which also keeps the `cli_reviewer_prompt.md` "(no deps…)" line literally true. This holds across **both** runtimes: deps are provisioned for every real actor_mode (opencode AND cli, only fake skips), so a **codex agent** — which can't fetch under the locked egress — finally has a warmed venv to run `uv run pytest` against. The deps volume holds only cached public packages in a discardable per-run clone, so mounting it into a locked codex container adds nothing exfiltratable.

`_prune_stale_deps_volumes` is scoped by `<project>-<lockfile>` so projects don't evict each other's caches when their lockfiles bump. Deps install failures raise `DepsInstallError`; the orchestrator hard-fails the run rather than silently degrade.

**Frozen-install fallback (npm).** One install failure is *not* terminal: `npm ci` aborts with `code EUSAGE … in sync` when the lockfile is valid but platform-incomplete — optional native/wasm bindings (e.g. tailwind v4's `oxide-wasm32-wasi` floating `@emnapi/*` at a caret range) that the host which generated the lock never descended into, but which linux `npm ci` insists be pinned. Such a repo builds fine locally and on Vercel; only the strict frozen path rejects it, so failing the run would be a false negative. `_is_lock_sync_failure` matches that exact signature and the warm step retries with `npm install` (warm time → open egress → can re-resolve). The fallback rewrites the worktree lockfile as a side effect; the orchestrator restores it (`git checkout -- package-lock.json`) immediately so the change never lands in the agent's diff. A *non*-EUSAGE failure (real missing package, crashing postinstall) does not match and stays terminal — the silent-degrade hazard above still holds for genuine breaks.

**Warm/run parity (`assert_deps_offline`).** The warm container runs with **open egress** (it must fetch); the agent runs under the **codex lock** (or open, for opencode/claude). That asymmetry hid a gap: `uv.lock`'s old `--no-install-project` cached deps but not the build backend (`setuptools>=68`) that a runtime `uv run` needs to install the project — fine at warm time, but blocked under the lock, so the agent hit it mid-run. Two parts close it: (1) the recipe installs the project at warm time (`uv sync --frozen`) and pins `UV_NO_SYNC=1` so runtime `uv run` won't rebuild; (2) after the per-run clone, the orchestrator runs the operator's `--check-cmd` (joined with `&&`) — or, with none set, the ecosystem **canary** (`uv run python -c ''` for the uv family; none for Node/Rust/Go) — in an L1-shaped sidecar **on the same network the agent will face**. It emits `DEPS_OFFLINE_ASSERT`. Severity is asymmetric and deliberate: a failing **check command under a locked network** hard-fails the run (the publish gate can't pass and the agent can't fetch its way out); a failing **canary**, or any failure under open egress, is recorded but not raised — the orchestrator never hard-blocks on a heuristic it authored.

## Lifecycle / cleanup

Per opencode-mode run, the orchestrator owns these external artifacts beyond the run directory:

- **Worktree** `/tmp/contremaitre-<run-id>/` — removed by `_cleanup_worktree` in `finally`.
- **Detached containers** labeled `contremaitre.run-id=<id>` — agent / SIM / review / check / deps-install / deps-clone / deps-assert. `--rm` for one-shot turns (deps-install / deps-clone / deps-assert are synchronous `--rm` one-shots), explicit `docker rm -f` after `docker wait` for streamed-log ones. `_stop_run_containers` runs in `finally` and on SIGTERM, scans by label, and stops anything still alive.
- **Per-run deps volume** `contremaitre-run-<run-id>-deps` — removed by `_remove_run_volumes` in `_cleanup_worktree`'s `finally`.
- **Pristine deps volumes** — kept across runs by design (avoids the 60–90s install re-cost). Same-project + same-lockfile-kind volumes with stale digests are pruned automatically after a fresh install lands.
- **Local clone cache** `~/.cache/contremaitre/<host>-<owner>-<repo>/` — kept across runs; `git fetch origin <base>` for freshness on every run.
- **opencode state dirs** `opencode-{agent,sim,review}-state/` under the run dir — kept on purpose; `_recover_text_from_sqlite` reads them when opencode silent-stalls.
- **CLI egress proxy + network** `contremaitre-egress-proxy` / `contremaitre-cli-egress` (codex or claude roles) — kept across runs by design: the allowlist is static and secret-free, so one long-lived proxy serves every CLI run. Carries no `run-id` label, so `contremaitre cleanup` leaves it; it auto-recreates when `cli_egress_squid.conf` changes. Per-run CLI homes (`codex-{agent,sim,review}-home/`, `claude-{agent,sim,review}-home/`) live under the run dir and go with it.

If the parent is SIGKILL'd before cleanup, label-tagged containers, per-run volumes, and worktrees can survive. `contremaitre cleanup` sweeps them:

```bash
contremaitre cleanup --dry-run   # see what would be removed
contremaitre cleanup             # containers + worktrees + dangling images
contremaitre cleanup --deps      # also nuke lockhash-keyed pristine volumes
contremaitre cleanup --repos     # also nuke ~/.cache/contremaitre/ clones
```

`contremaitre image build` runs `docker image prune -f` after a successful build so rebuilds with the same tag don't accumulate `<none>:<none>` orphans.

## Recoveries

`recoveries.jsonl` records degradations the orchestrator handled without aborting. Each entry is mirrored into `guardrail_events.jsonl` as `recovery_<kind>` so a single tail catches both surfaces.

- **`sqlite_recovery_silent_stall`** — opencode occasionally persists message parts to its SQLite store (`<run_dir>/opencode-{agent,sim,review}-state/opencode.db`) but doesn't flush the corresponding `text` event to its `--format=json` stdout before the docker process exits. The data is intact in the DB. [`_recover_text_from_sqlite`](../contremaitre/actors.py) reads the latest message back (`mode=ro`, 2s timeout) and the run continues with the recovered text. The state dirs are kept on purpose for exactly this path.
- **`sigterm_emergency_write`** — host process receives SIGTERM mid-run. The handler ([orchestrator.py](../contremaitre/orchestrator.py)) writes a `FAILED_INFRA` terminal with `reason="killed_via_sigterm"`, runs `_stop_run_containers` to stop label-tagged containers, then exits. Partial artifacts are preserved (raw exports, transcript fragments, any `viewer.html` that had landed) so the run dir stays browsable.
- **`extract_failed`** — `extract.py` raised while harvesting `task` sub-agent files or `extracted_files/*`. Logged; the rest of the artifact contract still lands.
- **`viewer_build_failed`** — `viewer.build_viewer()` raised. Logged; other artifacts unaffected. Rebuildable later with `contremaitre viewer <run-dir>`.
- **`cli_review_failed`** — a CLI reviewer tool returned empty output or threw during a round. Logged; the round continues with the remaining tools and the loop is not aborted.

None of these abort the run. The orchestrator's `finally` writes terminal state, runs the viewer build, and sweeps containers regardless — so partial information is always recoverable.

## Scaffold semantics

`.contremaitre/IMPLEMENTATION_COMPLETE` and `.contremaitre/SETTLED_DESIGN.md` are **Contremaitre scaffolds, not part of the skill**. The skill prescribes neither.

- `IMPLEMENTATION_COMPLETE` ends the WORK loop; the agent is told (via [initial_prompt.md](../contremaitre/prompts/initial_prompt.md)) to write it only after SETTLED is locked, the implementation matches SETTLED, and both the test suite + the project's CI lint/format gate pass against the changed files.
- `SETTLED_DESIGN.md` is the design handoff artifact the REVIEW pass reads (from a pre-staged copy at `/review/SETTLED_DESIGN.md`) and the source of the host's commit title + body and the PR title + body.
- `architecture-review.html` is the skill's HTML candidate cards. The orchestrator does **not** check for it (telemetry only) — the SIM reads it via the `:ro` mount.

`.contremaitre/*` is **excluded from the committed diff** via `git add -- . ':(exclude).contremaitre'`. The files stay in the worktree across WORK rounds so the SIM keeps reading SETTLED after CHANGES_REQUESTED loop-back, but never enter the published commit or PR — the SETTLED content is already in the commit body + PR description.

The clean-worktree hard gate filters via [`gates.only_internal_changes`](../contremaitre/gates.py). The filter tolerates more than just `.contremaitre/`: it also passes `opencode.json`, and build-output directories `dist/`, `build/`, `out/`, `.next/`, and `__pycache__/`. Same-named root files like `dist` stay dirty. The intent is that conventional build output + opencode's emitted config file shouldn't block publication if they happen to land in the worktree, without letting real source files be skipped because their names collide with build directories. The tolerated set lives once as `gates.INTERNAL_PATHS` — the same tuple the host commit derives its `:(exclude)<path>` pathspecs from (`orchestrator._commit_agent_changes`), so the clean-worktree predicate and the commit excludes can't name different sets.

---

## CLI Reference

The dozen most-used flags live in [README.md](../README.md#flags-worth-knowing). Below is the full surface.

### Subcommands

| Subcommand | Purpose |
|---|---|
| `run` | Run the WORK + REVIEW loop with optional PR publication. |
| `doctor` | Validate live-run operational prerequisites without starting a run. |
| `fixture init <path>` | Create a tiny local git repo for fake-mode smoke runs. |
| `image build [--variant base\|rust\|go]` | Build the runtime Docker image. |
| `cleanup [--deps] [--repos]` | Prune stale containers, worktrees, and (opt-in) deps volumes + clone caches. |
| `tui run -- <run-args>` | Spawn `contremaitre run` and attach the live TUI. |
| `tui attach <run-dir>` | Read-only TUI over a finished run. |
| `viewer <run-dir> [--open]` | Rebuild `viewer.html` for an existing run. |
| `index [<runs-root>] [--open]` | Build `index.html` over all runs under a root. |
| `eval {run\|check\|compare\|promote\|all\|show} <case_id> [--config <name>] [--n 3]` | v0 regression canary. See [golden_cases/README.md](../golden_cases/README.md). |

### Shared flags (`run` + `doctor`)

| Flag | Default | Purpose |
|---|---|---|
| `--base BASE` | *(required)* | Branch the worktree is sourced from + PR target. Fetched as `origin/<base>`. |
| `--repo-cache PATH` | `~/.cache/contremaitre/<host>-<owner>-<repo>/` | Override the auto-derived clone cache. |
| `--runs-root PATH` | `.contremaitre/runs` | Where per-run directories land. |
| `--docker-image NAME` | `contremaitre-agent:latest` | Runtime image. |
| `--opencode-config PATH` | (synthesized from `--agent-model`) | Path to opencode.json. |
| `--openrouter-env-var NAME` | `OPENROUTER_API_KEY` | Env-var name holding the OpenRouter key. |
| `--docker-network NAME` | — | Docker `--network` for opencode containers. |
| `--http-proxy URL` / `--https-proxy URL` / `--no-proxy LIST` | — | Container proxy settings (host env is not forwarded). |
| `--allow-open-egress` | False | Accept unrestricted egress (otherwise a network/proxy is required for opencode, and a CLI role/reviewer auto-locks). For a **codex/claude** role this is the explicit override of the default lock — warned, since the token is exfiltratable; use it when the agent must reach package registries the allowlist blocks. |
| `--skip-openrouter-key-check` | False | Don't query OpenRouter key metadata. |
| `--allow-unlimited-openrouter-key` | False | Accept a key with no provider-side credit limit. |
| `--openrouter-key-url URL` | `https://openrouter.ai/api/v1/key` | OpenRouter key-metadata endpoint. |
| `--max-cost-usd FLOAT` | `30.0` | Orchestrator cost cap (on top of OpenRouter's daily limit). |

### `run`-specific flags

| Flag | Default | Purpose |
|---|---|---|
| `--fork URL` | — | Push remote for the run branch. Required for `--publish-mode gh`. |
| `--upstream URL` | — | Canonical (read-only) remote, mounted as `upstream`. Preferred over `--fork` for cloning when set. |
| `--gh-repo OWNER/REPO` | — | Override the `gh pr create --repo` target (cross-fork PRs). |
| `--branch-prefix STR` | `refactor` | Prefix for generated branch names. |
| `--agent fake\|opencode\|claude\|codex` | `fake` | Per-run **agent** runtime. `fake` = smoke fixture; `opencode` = live model; `claude` / `codex` = subscription CLI headless. |
| `--sim opencode\|claude\|codex` | (same as `--agent`) | Override the **SIM** runtime, enabling mixed runs (e.g. `--agent codex --sim opencode`). |
| `--agent-model SLUG` | — | OpenRouter / OpenCode model slug for an opencode agent role. Omit to pick interactively on TTY. |
| `--sim-model SLUG` | — | Model for an opencode SIM role. Omit to pick interactively (picker proposes `--agent-model` as default); in non-interactive opencode-agent runs it mirrors `--agent-model`. |
| `--codex-model NAME` | `gpt-5.5` | codex-native model for a codex role (namespaced `--agent/sim-model` are rejected by codex and fall back to this). |
| `--codex-effort minimal\|low\|medium\|high\|xhigh` | `high` | codex reasoning effort, pinned via `-c model_reasoning_effort` on every codex turn. |
| `--claude-model NAME` | _(empty)_ | claude model for a claude role (e.g. `opus`); empty uses the `~/.claude` account default. |
| `--claude-effort low\|medium\|high\|max` | `high` | claude effort, pinned via `--effort` on every claude turn. |
| `--cli-reviewer auto\|codex\|claude\|none` | `auto` | Post-publish CLI review loop tool. `auto` detects + prompts on TTY; `none` skips. |
| `--max-cli-review-rounds INT` | `3` | Max post-publish review + revision rounds before `PR_NEEDS_HUMAN`. |
| `--run-slug STR` | `run` | Identifier for `<runs-root>/<run-id>/` naming. |
| `--check-cmd CMD` | — | Executable check command, repeatable; blocks publication on failure. |
| `--publish-mode stub\|gh` | `stub` | `stub` dry-runs everything except `git push` / `gh pr create`. |
| `--keep-worktree` | False | Preserve the worktree after the run. |
| `--simulate-drift-after-approval` | False | Inject post-APPROVED diff drift to exercise the diff-hash gate. |
| `--container-user UID:GID` | — | Docker `--user` value. |
| `--agent-timeout-seconds INT` | `1800` | Per-agent-turn timeout. |
| `--sim-timeout-seconds INT` | `1500` | Per-SIM-turn timeout. |
| `--opencode-stdout-stall-seconds INT` | `300` | Kill opencode if its stdout hasn't grown for this many seconds. `0` to disable. |
| `--pr-title STR` / `--pr-body STR` | (derived from SETTLED) | Override PR title / body. |
| `--max-turns INT` | `30` | Per-actor turn budget. |
| `--max-wall-minutes INT` | `180` | Wall-clock budget. |
| `--no-progress-turns INT` | `5` | Stagnation threshold; aborts on no marker progress. |
| `--malformed-verdict-retries INT` | `2` | Retries for an unparseable SIM verdict. |
| `--max-review-rounds INT` | `3` | Max REVIEW → WORK loops before `NO_PR_CHANGES_REQUESTED`. |
| `--sim-scenario {approved,changes_requested,needs_human,malformed,malformed_then_approved}` | `approved` | Fake-SIM behavior (ignored when `--agent opencode`). |
| `--agent-scenario {normal,forbidden_path,no_impl_complete}` | `normal` | Fake-agent behavior. |

### `cleanup` flags

| Flag | Default | Purpose |
|---|---|---|
| `--runs-root PATH` | `.contremaitre/runs` | Runs root used to decide which container labels are still live. |
| `--dry-run` | False | Report what would be removed without touching. |
| `--skip-images` | False | Skip dangling-image prune. |
| `--deps` | False | Also remove lockhash-keyed pristine deps volumes. |
| `--repos` | False | Also remove `~/.cache/contremaitre/<slug>/` clones. |

### `image build` flags

| Flag | Default | Purpose |
|---|---|---|
| `--variant base\|rust\|go` | `base` | Image variant; `rust` / `go` chain `FROM contremaitre-agent:latest` and add their toolchain. |
| `--image-name NAME` | (derived) | Override output tag. |
| `--dockerfile PATH` | (derived) | Override Dockerfile path. |
| `--no-cache` | False | Force fresh layers. |

### `eval` flags (shared across `run` / `check` / `compare` / `promote` / `all` / `show`)

| Flag | Default | Purpose |
|---|---|---|
| `--config NAME` | `default` | Config name under `golden_cases/<case_id>/configs/`. |
| `--n INT` | `3` | Number of runs to aggregate (n=3 is the floor; lower values are forbidden by `promote`). |
| `--runs-root PATH` | `.contremaitre/runs` | Runs root. |
| `--json` (compare only) | False | Emit raw JSON instead of the pretty scorecard. |
