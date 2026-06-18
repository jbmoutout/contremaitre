---
status: rejected
date: 2026-06-18
---

# Codex host-injected auth + uniform open egress — rejected

> **Rejected, not implemented.** The asymmetric egress posture (codex/opencode
> locked, claude open) is **deliberate**, not an accident to be "harmonized."
> What actually prompted this investigation — letting the `cli_review` role run
> tests — was orthogonal to auth and shipped on its own (deps mount + throwaway
> worktree copy; see `runtime_image._NON_EXECUTING_ROLES`, `cli_reviewer_turn`).
> This ADR records why the auth/egress rework was dropped and preserves the spike
> findings so feasibility isn't re-derived if it ever resurfaces.

## Context

The idea: move codex auth to host-injected (like claude), so no CLI container
holds a usable credential, then run **all** containers on open egress and delete
the squid lock subsystem. Stated goal was uniformity + deletion. It was
spike-validated as *feasible* before being rejected on *desirability*.

## Decision

**Do not pursue it.** Keep the current posture:

- claude — host-injected (no in-container credential), runs **open** (it must
  reach the host `cli_auth_proxy`, and its ~1-year OAuth token is too dangerous
  to ever place in a container).
- codex — short-lived (~10-day, refresh-neutered) token in its home, runs
  **locked** behind the squid allowlist.
- opencode — OpenRouter key (paid) or none (Zen); inherits the run's network.

The egress asymmetry is **principled**: posture follows credential blast-radius.
A tool whose credential is bounded (codex 10-day, OpenRouter capped) can sit in a
locked container; a tool whose credential is dangerous (claude 1-year) must be
host-injected, which forces open egress to reach the proxy. The CLI-egress rule
now lives in one place — [`egress.py`](../../contremaitre/egress.py) — so the
asymmetry is at least readable.

## Why rejected

1. **The two goals cancel.** Removing codex's in-container token only matters if
   egress goes open; but the squid lock already *contains* that token (short-lived,
   refresh-neutered, no exfil route). Keep the lock → removing the token buys
   almost nothing.
2. **Open egress exfiltrates PRIVATE target source.** The target repo (cloned via
   `--fork`/`--upstream` gh auth, mounted at `/app`) can be private. squid was
   anti-exfil, not only anti-theft (`cli_egress_squid.conf` denied CONNECT to all
   but provider domains). "Uniform open" makes private source + git history
   exfiltratable for roles that are locked today — a regression, not a cleanup.
   (AGENTS.md "repo is treated as public" scopes the *contremaitre* repo's
   committed files, not the target.)
3. **It trades kernel isolation for bespoke security code.** Deleting squid
   (battle-tested `--internal` network) and making "open" safe again requires a
   per-provider path allowlist, a per-provider nonce, and a platform-aware bind in
   the proxy — hand-rolled security code that must be perfect. Net complexity is
   not lower, and the posture is arguably weaker.
4. **The proxy becomes an unscoped signing oracle** on shared open egress: it
   injects the real credential into any path to the pinned host, and every
   container can reach every provider's proxy port. Closing that needs the
   allowlist + nonce from (3). For codex specifically the security gain over the
   status quo is lateral at best.

## Considered options

- **Uniform *open* egress + host-inject all (the proposal).** Rejected — items
  1–4 above.
- **Uniform *locked* egress + host-inject all.** The strictly-better direction
  (would also close claude's pre-existing open-egress private-repo exposure), but
  it hinges on an unproven unknown: whether an `--internal`-network container can
  reach a host proxy bound to the docker bridge-gateway IP. Deferred — only worth
  the spike if private-target confidentiality becomes a hard requirement.
- **Do nothing.** **Chosen.** Current posture contains every durable credential
  in the locked roles; claude carries no durable secret; the OpenRouter key is
  spend-capped. The asymmetry is principled. Churning auth here would be code for
  its own sake.

## Preserved spike findings (2026-06-15)

If host-inject is ever revisited, these were established empirically (against the
real backends) and need not be re-derived:

- **Codex host-inject is feasible.** The model-traffic redirect knob is a custom
  `model_providers` entry (`wire_api=responses`, arbitrary `base_url`) +
  `model_provider` override + `features.responses_websockets=false` — **not**
  `chatgpt_base_url` (which only steers telemetry and is localhost-validated).
  On the HTTP responses transport codex sends no `x-oai-attestation`, so a host
  proxy can swap a dummy bearer for the real token (`/responses` → `200`, real
  `PONG`). No socat, no DNS/CA MITM, no WebSocket relay.
- **OpenRouter host-inject is trivial** (vanilla OpenAI-compatible): point
  opencode's provider `baseURL` at a host proxy, dummy `apiKey`; the proxy injects
  the real key. The operator's key is an *inference* key — `/api/v1/keys` → 401
  (cannot mint an uncapped key), but `/api/v1/credits` → 200.
- **Zen (`opencode/...`) uses no operator credential** — image bakes none, the
  per-run state mount is empty, a keyless run reaches the Zen server. Free tier is
  rate-limited (a shared quota, not a secret).
- **OpenRouter key containment today:** it never enters CLI containers; it enters
  *opencode* containers via `-e OPENROUTER_API_KEY` (actors.py), exfiltratable on
  a pure-opencode open run (bounded by the mandatory preflight credit cap).

## Consequences

- The egress asymmetry stays, now documented as deliberate and centralized in
  `egress.py`. AGENTS.md's "don't drop codex's neutered-token + locked egress"
  rule stands.
- The `cli_review`-runs-tests capability (the real driver) shipped independently.
- If private-target confidentiality is ever required, revisit the *uniform locked*
  option above, starting with the bridge-gateway-proxy reachability spike.
