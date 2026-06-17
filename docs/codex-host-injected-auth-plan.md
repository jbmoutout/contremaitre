# Plan: codex host-injected auth + uniform open egress

**Status:** designed, spike-validated (2026-06-15). Not yet implemented.

## Objective

Move codex auth to host-injected (same model as claude), so the codex container
holds **zero usable credential**. Then **harmonize egress to open** for all CLI
roles and **delete the squid lock subsystem**. The win is uniformity + deletion,
not security (codex already only exposed a short-lived JWT). Gated on the spike
below — which **passed**.

## Spike results (decisive)

Run in `/tmp/cli-actor-test/auth-inject/` against the real chatgpt.com backend,
codex-cli 0.136.0.

1. **Attestation does NOT bite.** On the HTTP responses transport
   (`features.responses_websockets=false`), codex sends only `authorization` +
   `chatgpt-account-id` + `originator` on `/responses` — **no `x-oai-attestation`**.
   Isolating test: dummy `auth.json` in `CODEX_HOME` (well-formed JWT, far-future
   `exp`), host proxy swaps the dummy bearer for the real one → `/models` and
   `/responses` both `200 OK`, codex returned `PONG` with real usage.

2. **The redirect knob is NOT `chatgpt_base_url`.** That setting (the one with the
   chatgpt.com/localhost validation that motivated the prior's socat/MITM idea)
   only steers telemetry — pointing it at a dead port still returned `PONG`. The
   model traffic is redirected by a **custom `model_providers` entry**:
   ```
   -c features.responses_websockets=false
   -c 'model_providers.codex_proxy={ name="codex_proxy", base_url="<proxy>/backend-api/codex", wire_api="responses" }'
   -c model_provider="codex_proxy"
   ```
   Custom-provider base URLs are **not** localhost-validated (accepted
   `http://192.168.1.86:8923`), so the container points straight at
   `host.docker.internal` — claude-style. **No socat, no DNS/CA MITM, no WS relay.**

3. **Verbatim forward.** With `base_url=<proxy>/backend-api/codex`, codex sends the
   full path; the proxy relays unchanged — identical to claude's `_relay`.

## Implementation

### 1. `cli_auth_proxy.py` — add codex provider
- `resolve_codex_token()`: host-side refresh-if-near-expiry, then read
  `~/.codex/auth.json` `tokens.access_token`. Real `refresh_token` never leaves host.
- `resolve_codex_identity()`: `{"chatgpt-account-id": tokens.account_id}`.
- `Provider` gains `resolve_identity: Callable[[], dict] = lambda: {}`.
- `PROVIDERS["codex"] = Provider("chatgpt.com", resolve_codex_token, resolve_codex_identity)`.
- `_relay`: strip identity headers too before injecting; `out.update(identity)`.
- Move `_ensure_fresh_access_token` + `_host_refresh_token` here from `CodexDriver`.

### 2. `cli_actor.py` `CodexDriver` — dummy home + proxy
- `_DUMMY_CODEX_AUTH`: `auth_mode=chatgpt`, well-formed dummy JWT (far-future exp),
  dummy refresh/account_id.
- `ensure_ready()`: `cli_auth_proxy.ensure_auth_proxy("codex")`, fail fast.
- `prepare_home()`: write only the dummy `auth.json`. No real token ever written.
- `inner_argv()`: add the three `-c` flags; `base = ensure_auth_proxy("codex") + "/backend-api/codex"`.
- `container_env`/`container_env_names`: no secret env.
- **Delete:** `src_codex_home`, `_ensure_fresh_access_token`, `_host_refresh_token`,
  `_NEUTERED_REFRESH_TOKEN`, re-seed + hard-guard logic.

### 3. Collapse egress (one path)
- `_assert_egress_locked()` → `return` (no CLI role carries a usable credential).
- `_egress_docker_flags()` → `["--add-host", "host.docker.internal:host-gateway"]` for both tools.

### 4. Delete squid subsystem
- Remove `cli_egress.py` + `cli_egress_squid.conf`.
- `cli.py`: delete `_maybe_provision_cli_egress`, `_cli_egress_is_auto`,
  `ensure_egress_proxy` import + call sites. codex no longer sets
  `docker_network`/`https_proxy`.
- `checks.py` / `runtime_image.py`: sidecars run on default bridge like claude's
  already do; update "locked egress" docstrings.
- `models.py`: `docker_network`/`https_proxy`/`allow_open_egress` become dead for CLI.
  ⚠️ **Verify opencode (`actors.py`) doesn't read them before deleting fields.**

### 5. Docs (same commit)
- `AGENTS.md` "What NOT to do" — **reverse** the "don't drop codex's neutered-token
  + locked egress" note (justified: container now holds zero usable credential).
- `cli_actor.py` codex AUTH docstring, `docs/control-plane.md`, `README.md` egress.

## Security assessment & must-fix-before-ship

Adversarial audit of the end-state (independent agent + verification). The design
eliminates credential **theft** (no portable secret in any container) but
substitutes credential **abuse** (the proxy signs requests with the real secret)
and **deletes a defense-in-depth layer** (squid). Findings, calibrated:

1. **[HIGH] Proxy is an unscoped signing oracle.** `_relay` forwards ANY path to
   the pinned upstream with the real credential injected — host is pinned, path is
   not. Verified: with the operator's OpenRouter **inference** key, `/api/v1/keys`
   → 401 (can't mint an uncapped key; cap holds), but `/api/v1/credits` → 200
   (account reads pass). For a **provisioning** key, key-minting WOULD work →
   CRITICAL. codex/claude expose account-scoped backend endpoints similarly.
   **Fix:** per-provider (method, path-prefix) allowlist in `_relay`, checked
   BEFORE injecting — claude `POST /v1/messages` (+count-tokens); codex `POST
   /backend-api/codex/responses` + `GET …/models`; openrouter `POST
   /api/v1/chat/completions` + `GET /api/v1/models`. Reject all else 403.
2. **[HIGH] Cross-provider abuse on shared open egress (NEW).** All containers
   reach ALL proxy ports via `host.docker.internal`. A codex container (no
   OpenRouter/Anthropic cred of its own) can drive the openrouter + claude
   proxies. Squid made this structurally impossible (no route, host-scoped
   allowlist). **Fix:** per-(run,role,provider) random nonce header the container
   must echo; each role is handed only its own provider's nonce.
3. **[HIGH] Loopback bind unreachable on Linux + exposure if widened (NEW/correctness).**
   `_BIND_HOST=127.0.0.1` (cli_auth_proxy.py:44). On native Linux,
   `host.docker.internal:host-gateway` is the bridge gateway IP, which can't reach
   a loopback-bound server → all CLI turns fail. Spike was macOS/Docker Desktop
   only. Naive fix (`0.0.0.0`) exposes the oracle to every co-tenant container.
   **Fix:** bind the docker-bridge-gateway address on Linux, keep loopback on
   Desktop; add an in-container reachability integration test.
4. **[DECISION — ACCEPTED] Open egress exposes PRIVATE target source. Risk
   accepted by operator (2026-06-15).** Squid was anti-exfil, not just anti-theft:
   it denied CONNECT to all but provider domains, blocking exfil of the worktree/
   diff/source (cli_egress_squid.conf:16-19). The TARGET repo (cloned via
   `--fork`/`--upstream` gh auth, mounted at `/app`) **can be private** — AGENTS.md
   "Repo is treated as public" scopes only the *contremaitre* repo's committed
   files, NOT the target. So open egress makes private target source + git history
   exfiltratable by a compromised/ injected agent. NEW for codex agent/SIM and
   opencode-SIM-in-mixed (were locked); pre-existing for claude + pure-opencode
   (already open today). **Operator decision: accept this.** The agent runs a
   frontier model the operator selected; private-source exfil via prompt-injection
   is accepted as out of scope. **MUST document loudly** in control-plane.md +
   README threat-model sections: "open egress ⇒ private target source is NOT
   confidentiality-protected; do not run untrusted targets you can't afford to
   leak." Host-inject (anti-theft) and egress-lock (anti-exfil) are orthogonal;
   this plan deliberately keeps only anti-theft.
5. **[MEDIUM] `resolve_token` per-request → keychain/file thrash + DoS.** claude
   shells `security find-generic-password` per request when env unset; a container
   can hammer the proxy. **Fix:** cache resolved token with short TTL + inbound
   rate-limit.
6. **[MEDIUM] Verify zero real-secret residue on dummy paths.** Keep a guard
   assert (replacing the deleted cli_actor.py:497-498) that the real
   access/refresh token AND real `chatgpt-account-id` are absent from the dummy
   codex `auth.json` (the real account-id is injected host-side via
   `resolve_codex_identity`, so the container's dummy must use a FAKE one).
   Confirm actors.py:645 `-e OPENROUTER_API_KEY` is removed and the synthesized
   opencode.json carries the literal dummy, never `{env:}` nor the real key.
7. **[LOW] No proxy audit log.** `log_message` silenced. Log method+path+provider+role
   (never the token) to JSONL so abuse (findings 1-2) is visible.

**Ship order:** (1)+(2) close the oracle; (3) is required for Linux to work at
all; (4) is a decision to make explicitly; (5)-(7) are hardening. Do NOT reverse
the AGENTS.md "locked egress" note until (1)+(2) land, or the documented threat
model overstates protection.

## Hardening design for #1 / #2 / #3 (to implement)

### #1 — Per-provider path+method allowlist (server-side, no tool cooperation)
- Add `allowed: frozenset[tuple[str, str]]` (method, path-prefix) to `Provider`.
- In `_relay`, BEFORE injecting the credential: match `(self.command, self.path)`
  against `allowed` (prefix match, ignore query string). No match → `403`, no
  injection, no upstream forward.
- Allowlists:
  - **claude**: `POST /v1/messages` (+ `POST /v1/messages/count_tokens`).
    ⚠️ SPIKE — confirm claude's exact paths through `ANTHROPIC_BASE_URL` before
    pinning, or a too-strict list breaks it.
  - **codex**: `POST /backend-api/codex/responses`, `GET /backend-api/codex/models`
    (from spike). Telemetry paths (`/apps`, `/analytics-events`) deliberately
    NOT listed → they 403 at the proxy instead of 401 at upstream, which also
    silences finding §A noise.
  - **openrouter**: `POST /api/v1/chat/completions`, `GET /api/v1/models` (from spike).
- Bonus: blocks the OpenRouter `/api/v1/keys` + `/api/v1/credits` paths (verified
  reachable today), so even a provisioning key can't mint/read through the proxy.

### #2 — Per-provider nonce (container can only reach its own proxy)
- Proxy mints a random nonce per provider at start; requires inbound header
  `X-Contremaitre-Proxy-Auth: <nonce>`; rejects `403` otherwise; STRIPS it before
  forwarding upstream.
- Each driver hands the CLI only ITS provider's nonce as a custom header:
  - **claude**: `ANTHROPIC_CUSTOM_HEADERS`. ⚠️ SPIKE — confirm claude forwards it.
  - **codex**: `model_providers.<p>.http_headers`. ⚠️ SPIKE.
  - **opencode**: provider `options.headers` in opencode.json. ⚠️ SPIKE.
- A codex container holds only the codex nonce → can't authenticate to the
  openrouter/claude proxy ports. Restores squid's role-isolation property on
  shared open egress. Recommend per-provider (not per-role) nonce — simplest,
  sufficient for cross-provider isolation.

### #3 — Platform-aware bind (server-side; also a functional fix)
- Docker Desktop (darwin/windows): keep `127.0.0.1` (Desktop proxies
  `host.docker.internal` → loopback).
- Native Linux: bind the **docker bridge-gateway address** (resolve it; default
  `172.17.0.1`), NOT `0.0.0.0` (which exposes every host interface). Pair with #2
  so reachability ≠ authorization regardless of bind.
- Add an integration test that curls the proxy FROM INSIDE a container (the spike
  never exercised the Linux path).

### Spikes still required before coding
1. claude exact request paths through `ANTHROPIC_BASE_URL` (for #1).
2. Custom-header pass-through per tool — claude `ANTHROPIC_CUSTOM_HEADERS`, codex
   `http_headers`, opencode `options.headers` (for #2).
3. Linux bridge-gateway bind reachability from a container (for #3).

No code written yet — implement on a fresh branch (was `feat/auth-proxy-hardening`, dropped while deferred).

## Open decisions
- **A. Telemetry 401s.** codex plugin/app-server calls (`/apps`, `rmcp` worker) hit
  real chatgpt.com with the dummy token → `401`, **harmless** (turn completes).
  Recommend: leave for v1 + a code comment; chase a `features.*=false` disable only
  if noisy. `chatgpt_base_url` can't fix it (localhost validation).
- **B. Dummy-JWT minting helper** shape.
- **C. `_access_token_exp` home** (stays in `cli_actor.py` for `cli.py` status line, or moves).

## Security finding: OPENROUTER_API_KEY (RESOLVED → option 2)

Audit result:
- **CLI containers (codex/claude): key never enters.** Forwarded `-e` names are
  driver-specific (claude→Anthropic; codex→none) + deps `runtime_env`
  (`VIRTUAL_ENV`/`UV_NO_SYNC` only) + proxy var *names*. `opencode.json` (only a
  `{env:}` *reference*, never the literal) is not mounted in CLI containers. No
  `--env-file`, no bulk `-e`. ✅
- **opencode containers: key IS present and IS at risk.** `actors.py:645`
  unconditionally forwards `-e OPENROUTER_API_KEY` (real value from `.env`) into
  every opencode container; the agent runs untrusted shell
  (`--dangerously-skip-permissions`). Pure opencode = open egress today → already
  exfiltratable (mitigated only by the mandatory credit-limit in preflight).
  Mixed codex+opencode = squid-locked today → protected.
- **The original "delete squid" plan REGRESSES this:** `cli_egress_squid.conf`
  states the lock contains *"codex's token (and any opencode SIM)"*. Going open
  egress flips the mixed-run opencode SIM from locked→open, newly exposing the key.

**Resolution: host-inject the OpenRouter key too (option 2)** — then no container
holds any model credential and uniform open egress is genuinely safe.

### Spike (passed, 2026-06-15, real openrouter.ai + host opencode)
- Gate A: dummy `Authorization` + proxy injects real key → `200` + completion.
- Gate B: opencode with **dummy** `OPENROUTER_API_KEY` and `baseURL`→proxy routes
  ALL model traffic through it (`cli_auth=Bearer DUMMY-…`, proxy `<- 200 OK`,
  agentic loop ran). `baseURL` is a real redirect knob (unlike codex's
  `chatgpt_base_url`); no validation; verbatim path forward. Vanilla OpenAI-compatible
  HTTP — no transport flag, no `model_providers` gymnastics.

### Option 2 implementation
- **`cli_auth_proxy.py`**: `PROVIDERS["openrouter"] = Provider("openrouter.ai",
  resolve_openrouter_token)` (token from `os.environ[OPENROUTER_API_KEY]`, loaded
  from `.env`). No identity headers. Relay forwards `/api/v1/...` verbatim.
- **`cli.py _synthesize_opencode_config`**: `baseURL` →
  `ensure_auth_proxy("openrouter") + "/api/v1"`; `apiKey` → literal dummy
  `"contremaitre-injected"` (drop the `{env:}` reference). Calling
  `ensure_auth_proxy` fails fast if the key is missing.
- **`actors.py build_docker_command`**: DROP `-e OPENROUTER_API_KEY` (line 645);
  ADD `--add-host host.docker.internal:host-gateway`. The `env_var not in env`
  raise moves host-side (the proxy resolver).
- **Egress**: now ALL containers (codex, claude, opencode) are tokenless →
  uniform open egress → squid subsystem fully deleted, no regression. Zen
  (`opencode/...`) models still reach opencode.ai/models.dev over open egress;
  they carry no operator secret.

### Zen = no auth (verified 2026-06-15)
Free OpenCode Zen (`opencode/...`) uses NO operator credential — confirmed three ways:
- Image bakes nothing: Dockerfile only `curl …/install | bash`, no `auth login` (Dockerfile:34).
- Container state mount is a fresh per-run empty dir (`run_dir/"opencode-*-state"` +
  mkdir, actors.py:188-192) — NEVER seeded from the host store, so the host
  `auth.json` (which DOES hold the literal 73-char OpenRouter key) never reaches it.
- Keyless run reaches the Zen server: with `OPENROUTER_API_KEY` unset + empty
  `XDG_DATA_HOME` + isolated HOME, a bad slug got a server-side "Model not found"
  and a valid slug reached `step_start` — no 401/403, no login, no `auth.json` written.
Zen's free tier is rate-limited (`FreeUsageLimitError`, models.py:46) — a shared
quota, not a secret. So open egress is safe for Zen; nothing exfiltratable.

## Config & build files to update (full inventory)
Every artifact that encodes the current "codex locked by default" posture:
- **`Makefile`** (lines 34, 59-63, 83, 97, 120): the `ALLOW_OPEN_EGRESS` block +
  comments describe codex as locked-by-default. Rewrite the comments; `--allow-open-egress`
  / `_egress_flag` becomes a no-op (open is the only mode) — drop it or keep as inert.
- **`README.md`** (5, 30, 40, 42, 73, 76, 103): codex "locked by default", the
  `--allow-open-egress`/`--docker-network`/`--http(s)-proxy` egress paragraphs, the
  startup check-on-locked-network note. Rewrite to "all roles run open; host proxy
  injects every provider credential." `OPENROUTER_API_KEY` in `.env` stays (host-side).
- **`docs/control-plane.md`** (103-107, 236, 329-343, 356, 365): the egress decision
  table (`_maybe_provision_cli_egress`/`_check_network_policy`/`_assert_egress_locked`),
  the "Codex containers hold a neutered token / locked egress" + "Opencode containers
  see OPENROUTER_API_KEY" paragraphs, the cli_review "provider-only CLI egress" note,
  the failure-mode list. Major rewrite → single open-egress posture, three-provider proxy.
- **`AGENTS.md`** (23, 68): "Where to edit" (drop `cli_egress.py` squid ref) + "What
  NOT to do" (REVERSE the "don't drop codex's neutered-token + locked egress" rule;
  add "don't reintroduce any real model credential into ANY container — claude, codex,
  AND openrouter are all host-injected").
- **`.env.example`** (`OPENROUTER_API_KEY=`): keep — now host-only (the proxy resolves
  it; it never enters a container). Add a one-line comment saying so.
- **`preflight.py`**: `_check_network_policy` (codex-locked enforcement) + the
  `_check_openrouter_key` presence row move to "proxy can resolve the key" — no longer
  a per-container concern. `_egress_*` CLI flags in `cli.py` argparse become inert.
