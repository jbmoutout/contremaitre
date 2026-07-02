# Exploration: pi.dev as an alternative coding-agent harness

**Status:** exploration / proposal. Describes work *not yet done*. Nothing in this
document reflects shipped behaviour — `pi` is not wired in. Read
[control-plane.md](control-plane.md) for what the system actually does today.

**Question asked:** can [pi](https://pi.dev/)
(`@earendil-works/pi-coding-agent`, repo `badlogic/pi-mono`) replace or augment
`opencode` as the agent/SIM harness, and where is the leverage?

**Evidence base:** the `CliDriver` / `ActorRunner` seams in
[cli_actor.py](../contremaitre/cli_actor.py) and [actors.py](../contremaitre/actors.py);
the auth/egress threat model in [cli_auth_proxy.py](../contremaitre/cli_auth_proxy.py)
and [cli_egress.py](../contremaitre/cli_egress.py); and **177 real runs** under
`.contremaitre/runs/` (3.9 GB) plus `LEARNINGS.md`, cross-referenced against pi's
published `docs/json.md` and `docs/rpc.md`.

---

## §0. Is it worth it? (critical verdict — read this first)

**Short answer: under the current constraints, little to nothing to gain — do not
build the integration on spec.** The rest of this doc designs *how* to integrate pi
cleanly; this section argues *whether* you should. The body oversells; this is the
correction.

The value case for pi rested on **consolidation** (one harness replacing three, one
observability surface, less recovery code). The operator explicitly ruled that out:
keep opencode + codex + claude, and scope pi to OpenRouter + Zen + Anthropic. Once
consolidation is off the table, the case collapses:

- **No new model reach.** opencode already reaches OpenRouter + free Zen; claude CLI
  already reaches Anthropic (and is the *preferred* Anthropic path). pi's three
  targets are a **strict subset** of what's already covered. Zero new models.
- **The observability win only pays if opencode is deleted.** The 70
  `recovery_sqlite_recovery_silent_stall` events are already *handled* by working
  code. As a kept-alongside harness, pi **adds** a driver + proxy generalization +
  a dependency on top of opencode's recovery code — net surface goes *up*, not down.
- **"Equal-footing A/B" is partly illusory.** pi is not a neutral substrate — it has
  its own agent loop, tools, prompt scaffold, and context management. Running "the
  same model" through pi vs opencode swaps the entire scaffold, not just the model.
- **pi's one true differentiator (RPC warm-context multi-turn) solves no current
  pain** and is the most expensive part (Phase 4). The SDK/embed angle is actively
  *wrong* here — it would breach the no-credentials-in-container threat model.

**The one testable exception:** is pi's agent loop simply *better than opencode's*
on the same cheap/free models? That's a fair fight (both are third-party loops) and
it's the only thing that could earn pi a place — as the preferred free/OpenRouter
harness, opencode kept as fallback. It hinges on **R8** (can pi even reach Zen
anonymously).

**Recommendation:** don't integrate on spec. Do the cheap probe first — (1) settle
R8 (~15 min); if Zen is gated to the opencode binary, shelve pi entirely. (2) One
head-to-head: pi vs opencode on the *same* free model against the golden case,
eyeball trajectory quality + failure rate, no driver/proxy work. (3) Only if pi
*visibly wins* do you pay for the driver. Otherwise this doc is a **design-on-file**
— cheap insurance for the day consolidation is back on the table, which is when pi
actually becomes interesting.

---

## TL;DR — recommendation (how to integrate, *if* §0 clears)

0. **pi is additive — every existing harness stays.** opencode, the claude CLI,
   and the codex CLI all remain, unchanged and selectable. claude CLI stays the
   *preferred* Anthropic harness (it's better-tuned for Anthropic); pi is not a
   replacement and not "central." pi's distinct role is **the universal model
   harness**: one agent loop that can run *any* of **three model sources** —
   OpenRouter, the free `opencode/*` Zen models, and Anthropic — through one clean
   event stream. OpenAI/ChatGPT-class models are **out of scope for pi**: the codex
   CLI keeps that role (decided — §6 R9). The value is reach + a uniform
   observability surface for cross-provider model comparison, **not** collapsing
   the others.

1. **Build it as a `CliDriver`, not a new runtime.** pi is a headless CLI in a
   container exactly like codex/claude. The whole `CliActorRunner` machinery
   (detached container per turn, per-turn event-slice parsing, session stash,
   egress lock, deps volume, label-driven supervision) is reused unchanged. The
   new code is one ~180-line driver class + a multi-provider auth registration.
   This is **v1**.

2. **The model-reach requirement drives the auth design (§3).** pi must hit
   multiple providers, and the threat model forbids real credentials in the
   container. The clean answer is to **generalize the existing host auth-inject
   proxy** ([cli_auth_proxy.py](../contremaitre/cli_auth_proxy.py)) — already
   built multi-provider (`PROVIDERS` dict + per-`Provider` `identity_headers()`)
   — to broker each provider pi uses. pi container holds **zero** real keys; each
   provider's base-URL env points at the host proxy. This is *more* secure than
   opencode's current posture (a real `OPENROUTER_API_KEY` in-container). **(R1:
   verify pi honours per-provider base-URL env vars.)**

3. **pi structurally retires our biggest harness scar.** Across the run history
   there are **70 `recovery_sqlite_recovery_silent_stall`** events — every one is
   opencode writing its reply to SQLite but never flushing the event to stdout,
   forcing a host-side DB scrape. pi's `--mode json` makes the final reply an
   explicit `message_end` / `turn_end` event, and its `--mode rpc` gives an
   explicit `agent_end` idle signal. That entire recovery class disappears — so
   for the free/OpenRouter model role, pi can be a cleaner path than opencode.

4. **pi has built-in what we hand-roll.** `auto_retry_start`/`auto_retry_end`
   events (transient-provider retry, which we built `provider_transient_error_retry`
   for) and `compaction_start`/`compaction_end` (context overflow, which our
   budget-cap resume machinery works around) are **first-class, observable pi
   events**. We get to *watch* them instead of reconstructing them.

**Recommended sequencing:** v1 `PiDriver` (print/json) reaching OpenRouter + Zen +
Anthropic via the generalized proxy → prove it on the golden case with a sibling
eval config → then consider v2 (RPC long-lived process). No existing harness is
removed at any phase.

---

## 1. What pi is

A minimal, extensible coding-agent harness. Four-tool core (`read`, `write`,
`edit`, `bash`, plus `grep`/`find`/`ls`); self-extends at runtime via TypeScript
Extensions, Skills, Prompt Templates, Themes. Provider-agnostic, bring-your-own-key.
Four run modes:

| Mode | Invocation | Shape | Fit for us |
|---|---|---|---|
| Interactive TUI | `pi` | full terminal UI | n/a |
| **Print** | `pi -p "…"` | run once, print, exit | maps to `codex exec` / `claude -p` |
| **JSON** | `pi --mode json …` | line-delimited event stream on stdout | **v1 parse target** |
| **RPC** | `pi --mode rpc` | long-lived process, LF-framed JSONL over stdin/stdout | **v2 — warm-context multi-turn** |

Install: `npm i -g --ignore-scripts @earendil-works/pi-coding-agent` (or
`curl -fsSL https://pi.dev/install.sh | sh`). Sessions persist as JSONL trees
under `~/.pi/agent/sessions/`, keyed by working dir; resume via `--continue` /
`--resume` / `--session <id|path>` / `--fork`; ephemeral via `--no-session`.
Per-role thinking via `--thinking off|minimal|low|medium|high|xhigh`. Tool
control via `--tools <list>` / `--exclude-tools <list>` / `--no-tools`.

---

## 2. The seam — where pi plugs in

The actor abstraction has two levels (see [actors.py](../contremaitre/actors.py#L65)
and [cli_actor.py](../contremaitre/cli_actor.py#L428)):

- `ActorRunner` Protocol — `agent_turn` / `sim_turn` / `sim_review`. The
  orchestrator owns the WORK loop and calls these.
- `CliDriver` Protocol — the per-tool seams `CliActorRunner` delegates to. **This
  is where pi goes.** codex and claude are both just drivers.

### `PiDriver` — mapping each `CliDriver` member

| Member | codex / claude today | `PiDriver` |
|---|---|---|
| `name` | `"codex"` / `"claude"` | `"pi"` |
| `home_mount_target` | `/root/.codex` / `/root/.claude/projects` | `/root/.pi` (sessions live in `~/.pi/agent/sessions/`) |
| `home_dir_prefix` | `codex` / `claude` | `pi` |
| `prepare_home(home)` | seed/refresh auth home | mkdir `agent/sessions`; write pi provider config pointing each provider's base URL at its proxy; **no credential file** (proxy holds every key) |
| `ensure_ready()` | JWT gate / start auth proxy | start the proxy for *each* provider the selected model needs (`ensure_auth_proxy("claude"|"openrouter"|"zen"|…)`) |
| `inner_argv(...)` | `codex exec …` / `claude -p …` | `["pi", "--mode", "json", "-p", "--provider", prov, "--model", model_id, "--thinking", effort, *session_args, *tool_args, prompt]` where `(prov, model_id)` come from the slug router (§3) |
| `container_env(base)` | token inject/scrub | per selected provider: `<PROVIDER>_BASE_URL=<that provider's proxy>`, `<PROVIDER>_API_KEY="contremaitre-injected"` (dummy) |
| `container_env_names()` | forwarded `-e` names | the base-URL + dummy-key names for the providers in play |
| `parse_events(path, start_offset)` | codex/claude JSONL parse | parse pi json events (§4) |

`session_args`: `["--session", session_id]` on resume, else `[]` (turn 1 captures
the id from the `session` event). `tool_args`: agent → full tools; **SIM →
`["--exclude-tools","write,edit"]`** — a *third* read-only layer on top of the
`/app:ro` mount and the SIM persona (codex/claude only have the first two).

### Registration (the blast radius of v1)

1. `cli_actor.py` `_make_driver()` (~L744): add `if tool == "pi": return PiDriver(config)`.
2. `models.py`: `pi_model` / `pi_effort` fields on `RunConfig`.
3. `cli.py`: `--cli-tool pi`, `--pi-model`, `--pi-effort`; add `"pi"` to the
   `--agent` / `--sim` choice lists.
4. `runtime_image.py` Dockerfile: `npm i -g --ignore-scripts @earendil-works/pi-coding-agent`.
5. `egress.py`: pi-via-proxy is credential-free in-container → **open-egress-safe**,
   same exemption as claude (no squid lock required).

No change to the orchestrator, the WORK loop, hard gates, the publisher, the TUI,
or `OpencodeActorRunner`. `CompositeActorRunner` already supports mixing, so
`--agent pi --sim opencode` (or any combo) works for free.

---

## 3. Auth & egress fit (the part that usually kills an integration)

The threat model is non-negotiable: **the agent/SIM container holds no outbound
credential** — git, GitHub, and the model token are all host-owned. Because pi
must reach *several* providers, this is the hard part of the integration. Today,
each model source is reached differently:

| Provider | Base URL | Auth today | Headers | Egress |
|---|---|---|---|---|
| OpenRouter (paid opencode) | `https://openrouter.ai/api/v1` | **real `OPENROUTER_API_KEY` in the opencode container** ([actors.py:567](../contremaitre/actors.py#L567)) | — | open |
| Zen (free `opencode/*`) | `https://opencode.ai/zen/v1` | **none** — anonymous, via the opencode binary's built-in access | `User-Agent: contremaitre` ([cli.py:1085](../contremaitre/cli.py#L1085)) | open |
| Anthropic (claude CLI) | host proxy → `api.anthropic.com` | host-resolved, injected per request | — | open |
| OpenAI (codex CLI) | `chatgpt.com` (validated) | neutered JWT in container | — | **locked** |

pi cannot borrow opencode's "built-in" Zen access (it isn't the opencode binary),
and putting a real `OPENROUTER_API_KEY` in pi's container would copy opencode's
weakest leg. **The clean design: generalize the auth-inject proxy into pi's single
front door for every provider.** The proxy is already shaped for it — `Provider`
carries `upstream_host`, a live `resolve_token`, and an `identity_headers()` hook
([cli_auth_proxy.py:124](../contremaitre/cli_auth_proxy.py#L124)) — keyed in a
`PROVIDERS` dict, one daemon thread per provider. Add entries:

| New `Provider` | `upstream_host` | `resolve_token` | `identity_headers()` |
|---|---|---|---|
| `openrouter` | `openrouter.ai` | host `OPENROUTER_API_KEY` | — |
| `zen` | `opencode.ai` | **none** (anonymous) | `{"User-Agent": "contremaitre"}` |
| `claude` *(exists)* | `api.anthropic.com` | claude subscription token | — |

(No `openai` provider — OpenAI/ChatGPT-class models stay with the codex CLI, §6
R9.) pi's container then holds **zero real keys**: for each provider it points
`<PROVIDER>_BASE_URL` at that provider's proxy and sends a dummy key, exactly like
`ClaudeDriver`. Open-egress-safe (nothing to steal). Net effect: **pi reaching
OpenRouter/Zen is *more* secure than opencode does today** — no real key ever
enters a container. Should pi ever need another provider (Gemini, Groq, a local
endpoint), it's one more `Provider` entry + a slug-router case.

Two small proxy changes this implies:
- **Token-less providers.** The relay currently always injects
  `Authorization: Bearer <resolve_token()>` ([cli_auth_proxy.py:156](../contremaitre/cli_auth_proxy.py#L156)).
  Zen is anonymous, so a `Provider` must be able to declare "no Authorization" and
  rely on `identity_headers()` alone. ~5-line change.
- **Slug router.** `PiDriver` maps a contremaitre model slug → `(pi provider,
  pi model id, proxy to start)`: `opencode/<id>` → `zen`; `openrouter/<id>` →
  `openrouter`; `anthropic/<id>` → `claude`. This is the concrete meaning of "pi
  accepts any model." (`openai/*` deliberately unrouted — that's the codex CLI.)

> **Hard dependency (R1):** pi must let us override each provider's base URL via env
> (`ANTHROPIC_BASE_URL`, the OpenRouter base, and a custom OpenAI-compatible base
> for Zen) and target the proxy. The unified `pi-ai` layer is
> SDK-style so this is expected, but **verify before building.** If a provider can't
> be base-URL-redirected, its fallback is the codex pattern (scoped key + squid
> lock) — strictly worse, and not the v1 plan.
>
> **Anthropic is the lowest-priority pi target.** claude CLI stays the preferred
> Anthropic harness; pi-on-Anthropic exists only for apples-to-apples comparison
> against pi's other providers.

---

## 4. Parsing pi's `--mode json` stream (`parse_events`)

`parse_events` reads the events file from `start_offset` (this turn's slice only —
the same offset trick codex/claude use) and returns
`(final_text, session_id, usage, error)`. Mapping from pi's documented events:

| We need | pi event |
|---|---|
| **session_id** | first line `{"type":"session","version":3,"id":"<uuid>",…}` |
| **final reply text** | `{"type":"turn_end","message":<AgentMessage>,…}` (or accumulate `message_end`) |
| **turn done** | `turn_end` / `agent_end` |
| **transient retry (observe, don't re-implement)** | `{"type":"auto_retry_start",…}` / `{"type":"auto_retry_end","success":false,"finalError":…}` |
| **context overflow handled** | `compaction_start` / `compaction_end` |
| **tool error** | `{"type":"tool_execution_end","isError":true,…}` |
| **token usage** | **R2 — not shown in `docs/json.md`.** Present via RPC `get_session_stats`; confirm json mode emits it (likely on `agent_end`) or derive from session JSONL. |

Two contract questions to confirm against a live binary (§6): **R3** does `-p`
compose with `--mode json` (or does `--mode json` already imply single-turn-and-exit
from the prompt arg, which is what we want); **R4** the exact field path to the
assistant text inside `AgentMessage`.

---

## 5. What the run history says pi fixes — and what it doesn't

Grounded in the 177-run corpus (`guardrail_events.jsonl` aggregated) and
`LEARNINGS.md`. Harness-mix for context: **934 `opencode_actor_start`** vs **277
`actor_start`** (CLI) — the corpus is opencode-dominant, so opencode's scars are
the ones with the most data.

### Structurally fixed by pi's protocol

| Friction (count / source) | Today | With pi |
|---|---|---|
| **Silent stall: text in SQLite, never on stdout — 70 `recovery_sqlite_recovery_silent_stall`** | host scrapes `opencode.db` to recover the reply (`f88bb82`) | reply is an explicit `turn_end`/`message_end` event; RPC `agent_end` is an explicit idle signal. Recovery class **retired**. |
| **Transient provider error mid-stream** (`provider_transient_error_retry`; LEARNINGS 2026-05-23) | dual-channel stdout+internal-log scan + hand-rolled bounded retry | pi emits `auto_retry_start/end`; we observe, not reconstruct |
| **Budget/context-cap exit** (`wall_cap`×4; resume.json machinery, `b045f2a`) | preserve worktree + homes, `run --continue` | pi `compaction_start/end` handles context overflow in-loop; resume still ours, but fewer hard caps hit |
| **Meta-only turn ending** (SIM emits "Let me verify…" then exits; LEARNINGS 2026-05-23, `833be2d`) | persona rule + "container-exit = last text" convention | `agent_end` is an explicit protocol boundary — the harness tells us the turn is genuinely done, not inferred from process exit |

### Inherited unchanged (orchestrator-side, harness-agnostic — pi does *not* fix these)

- **SIM verdict JSON brittleness** (LEARNINGS 2026-06-13, every verdict failed one
  run; `84ff424`) — lives in `verdicts.py`, not the harness. pi's clean
  `turn_end` text may *help*, but the defensive parser stays load-bearing.
- **Deps volume isolation** (`0633ac4`), **diff-base SHA pinning**, **lockfile
  churn on install** (`6d2e155`), **hard-gate L0 determinism** — all host-side,
  orthogonal to which harness runs.
- **Yield discipline / IMPLEMENTATION_COMPLETE marker** — a prompt+control-plane
  contract. pi *could* make it crisper as a first-class Extension tool (§5 below),
  but the filesystem-marker convention works as-is.

### Strategic leverage — universal model reach, not consolidation

The leverage is **not** collapsing harnesses — opencode, codex, and claude all
stay. It's that pi gives one agent loop reach across *every* provider behind *one*
event schema and *one* auth path (the generalized proxy). Concretely:

- **Cross-provider model comparison on equal footing.** Today, comparing a free
  Zen model against an OpenRouter model against an Anthropic model means three
  different harnesses with three different observability surfaces and failure
  modes — confounding the comparison. With pi, the *same loop* runs all three, so
  the only variable is the model. That sharpens the eval canary directly.
- **A cleaner path for the free/OpenRouter role.** opencode's SQLite-stall and
  hand-rolled retry (§ above) are the corpus's most frequent harness scars. pi can
  reach the *same* free Zen and OpenRouter models with explicit `turn_end` /
  `auto_retry` events instead — opencode stays the default, but pi is a less
  fragile alternative for that role when a run needs it.
- **New providers are a `Provider` entry, not a new harness.** Want Gemini, Groq,
  or a local model in the mix? Register one proxy `Provider` + a slug-router case.
  No new runtime, no new credential surface.

Bonus avenue: pi's **Extensions/Skills** are native TypeScript hooks. The
contremaitre coding skill could run as a pi Skill, and SIM-yield /
`IMPLEMENTATION_COMPLETE` could become a first-class Extension *tool* (a clean
protocol event) rather than a stdout/file-marker convention — directly attacking
the turn-boundary-honesty friction at its root. v3-grade; note and defer.

---

## 6. Risks & must-verify unknowns

| # | Unknown | Why it matters | How to close |
|---|---|---|---|
| **R1** | Can pi override **each provider's** base URL via env/config and target the proxy (Anthropic, OpenRouter, + a custom OpenAI-compatible base for Zen)? | The entire zero-new-credential, multi-provider story depends on it | one-shot each provider against a local proxy; or read the `pi-ai` provider sources |
| **R8** | Does Zen free access work from a **non-opencode** client — i.e. is `https://opencode.ai/zen/v1` truly anonymous given just `User-Agent: contremaitre`, or is the free tier gated to the opencode binary's identity? | "free opencode models" is an explicit requirement; if gated, pi can't serve them without a key | the host probe ([cli.py:1082](../contremaitre/cli.py#L1082)) already succeeds anonymously — confirm a *full chat completion* (not just the probe) does too, through the proxy |
| ~~**R9**~~ | **Resolved:** OpenAI/codex-class is **not** a pi target | codex CLI keeps that role; pi reaches OpenRouter + Zen + Anthropic only | decided — note below |
| **R2** | Does `--mode json` emit token usage? | TUI meter + stats.json + budget accounting | inspect a live event stream; fall back to session JSONL |
| **R3** | `-p` × `--mode json` composition / single-turn semantics | shapes `inner_argv` and per-turn container model | run `pi --mode json -p "hi"`, observe exit |
| **R4** | Exact `AgentMessage` text field path | `parse_events` correctness | inspect one `turn_end` payload |
| **R5** | Print/JSON mode autonomy (no confirmation prompt) | headless viability | **Largely resolved:** `docs/rpc.md` states "tool calls execute autonomously without confirmation"; confirm the same holds for `-p`/json |
| **R6** | `--ignore-scripts` global install runs correctly in the image | Dockerfile build | build the image, `pi --version` |
| **R7** | Any pi telemetry/phone-home that a locked egress would block | egress policy | run under squid allowlist, watch for blocked hosts |

R1–R4 plus R8 are answerable in **under an hour** with the binary installed
locally — they gate whether v1 is a clean afternoon or a yak-shave. R9 is a scope
decision (note below), not a probe.

**R9 note (resolved) — codex is not a pi target.** "pi.dev must accept … codex"
meant *keep the existing codex CLI harness*, not route pi to OpenAI/ChatGPT
models. So pi's model targets are exactly **OpenRouter + free Zen + Anthropic**.
OpenAI-class models remain the codex CLI's job (its ChatGPT-subscription auth —
neutered JWT, chatgpt.com, WebSocket transport — is codex-specific and stays put).
No `openai` proxy provider, no `openai/*` slug-router case.

---

## 7. Proposed plan

> Invariant across all phases: **no existing harness is removed.** opencode,
> codex CLI, and claude CLI stay selectable throughout.

**Phase 0 — de-risk (local, ~1h).** Install pi; resolve R1–R4 + R8 against a live
binary; capture a real `--mode json` event stream to a fixture. Gate: can pi route
**OpenRouter, Zen (anonymous + `User-Agent`), and Anthropic** through host base-URL
overrides? Zen is the riskiest leg (R8).

**Phase 1 — `PiDriver` v1 (print/json) + multi-provider proxy.** ~180-line driver
+ the 5 registration points (§2) + the proxy generalization (token-less
providers, `openrouter`/`zen` `Provider` entries) + the slug router (§3). Unit-test
`parse_events` against the Phase-0 fixture. Smoke needs a real-model run
(fake-actor scaffolds won't exercise pi).

**Phase 2 — eval-gate it.** Add **sibling configs** under
`golden_cases/case_01_sqlite_utils_8f0c06e/configs/` — e.g. `pi_zen.toml` (a free
Zen model through pi) and `pi_openrouter.toml` — alongside the existing
`default.toml` (do *not* edit it; each config owns its baseline, AGENTS.md eval
rule). Run n≥3, `compare`, `promote`. Because pi and opencode can run the *same*
model, this is a genuine harness A/B with the model held fixed.

**Phase 3 — position pi.** If pi matches/beats opencode on the canary and R1–R9
are green, decide pi's standing role: keep it opt-in (`--cli-tool pi`), or make it
the recommended path for the free/OpenRouter model role (opencode still available).
**No harness is retired** — pi earns share by being chosen, not by replacement.

**Phase 4 (optional) — RPC runtime.** A `PiRpcActorRunner` (an `ActorRunner`, not
a `CliDriver`) holding one long-lived container per WORK session, talking
`{"type":"prompt",…}` / `agent_end` over stdio. Wins: warm model context across
turns (no per-turn cold start), and `agent_end` as a first-class idle signal.
Cost: breaks the one-container-per-turn + label-SIGTERM supervision model; needs a
stdio pump and liveness handling. Structural bet — only after v1.

---

## Appendix — sources

- Run corpus: `.contremaitre/runs/` (177 runs); guardrail-event aggregation in this
  conversation. `LEARNINGS.md`, `docs/eval_systems.md` (gitignored).
- Code: [cli_actor.py](../contremaitre/cli_actor.py),
  [actors.py](../contremaitre/actors.py),
  [cli_auth_proxy.py](../contremaitre/cli_auth_proxy.py),
  [cli_egress.py](../contremaitre/cli_egress.py).
- pi: <https://pi.dev/>, repo `badlogic/pi-mono`
  (`packages/coding-agent/README.md`, `docs/json.md`, `docs/rpc.md`,
  `docs/session-format.md`). npm `@earendil-works/pi-coding-agent`.
