"""CLI-driven actor: run a frontier CLI (codex or claude) headless in-container.

A third `ActorRunner` beside `OpencodeActorRunner` / `FakeActorRunner`. Where
opencode drives an OpenRouter model, this drives a frontier CLI inside the same
per-run container, so the agent/SIM gets the CLI's own agentic loop (tools,
skills) — empirically a markedly stronger reviewer than the OpenRouter models.
Selected via `ActorMode.CLI` + `RunConfig.cli_tool`.

The runner owns the SHARED orchestration (egress lock, per-run home lifecycle,
detached container run + stdout->raw_export, timestamp back-fill, session-attr
management, transcript append, docker wrapper). Tool-specific seams live behind
`CliDriver`: `CodexDriver` and `ClaudeDriver`. Adding a tool = a new driver, not
a fork of the runner.

Every design choice below was pinned by in-container experiment, not guessed.

AUTH — codex (subscription, hard-minimised — read before touching CodexDriver)
  The agent runs UNTRUSTED model-generated shell with codex's own sandbox off
  (`-s danger-full-access`): the container is the only boundary, and the CLI *is*
  the agent, so anything readable by that uid is readable by injected code.
  Therefore the mounted credential is minimised and the home is per-run:
    - NEUTER `tokens.refresh_token` to a dummy non-empty value. codex's parser
      REQUIRES the field and the refresh API REJECTS an empty string, so it
      can't be dropped or blanked — but with a valid access token in a writable
      home codex never calls refresh, so the dummy is inert. A leaked copy buys
      <=10 days of access (the JWT's own life) and CANNOT mint more; the real
      refresh_token is a standing key to the operator's account and never
      enters the container.
    - RE-SEED auth.json EVERY turn from the host. codex can delete auth.json on
      a failed proactive refresh; re-seeding makes the home self-healing.
    - The home is mounted RW (codex writes PATH/app-server/sessions; an RO home
      hard-fails). Writes land on the per-run copy, never the operator's
      `~/.codex`.

AUTH — claude (subscription via a headless OAuth token)
  On macOS the operator's interactive creds live in Keychain (no readable
  credentials file), so the in-container path uses `claude setup-token`'s
  long-lived `CLAUDE_CODE_OAUTH_TOKEN`, forwarded into the container by NAME
  (`-e CLAUDE_CODE_OAUTH_TOKEN`, value in the docker-run env, never on argv —
  the proxy-var pattern). The per-run state mounts at /root/.claude/PROJECTS
  (claude's session store) — NOT all of /root/.claude, which would shadow the
  image's baked /root/.claude/skills (→ "Unknown skill"); a projects-only mount
  keeps skills visible AND persists sessions so `--resume` works across turns.
  No credential file is ever written. `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`
  are forwarded empty to override any image-baked key and keep claude on the
  OAuth subscription. Unlike codex's refresh token, this OAuth token is NOT
  neuterable — it is the credential, and it is long-lived (~1yr), so the egress
  lock is doubly load-bearing for a claude run.

EGRESS (the load-bearing control — `_assert_egress_locked`)
  The in-container credential outlives any single run (codex: a ~10-day JWT;
  claude: a ~1yr OAuth token). By default exfiltration is prevented by a
  two-layer lock the runner refuses to launch without (auto-provisioned for any
  CLI role; `allow_open_egress` is the explicit, warned escape hatch):
    1. an `--internal` `docker_network` — kills direct egress AND external DNS,
    2. an allowlisting `https_proxy` (provider domains only) as the sole exit.

MULTI-TURN: turn N writes session state into the persisted per-run home; turn
  N+1 resumes it by id (codex: `codex exec ... resume <id>`; claude:
  `claude ... --resume <id>`), carrying context across separate `docker run`s —
  the CLI analog of opencode's `--session`.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .actors import ActorError, ActorOutput, _run_detached_container
from .jsonlog import append_transcript
from .models import RunConfig, RunPaths

# codex REQUIRES tokens.refresh_token present and non-empty (parser + refresh
# API both reject otherwise), so we neuter rather than drop it. Inert in
# practice: a valid access token in a writable home means codex never refreshes.
_NEUTERED_REFRESH_TOKEN = "x"

# Refuse to run when the access JWT has less than this remaining — codex may
# then try (and, with the neutered token, fail) to refresh mid-run. The token
# is a ~10-day JWT, so this rarely fires.
_REFRESH_MARGIN_SECONDS = 24 * 3600

# Env var carrying claude's headless OAuth token (from `claude setup-token`).
_CLAUDE_OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


# ===== codex helpers (module-level; imported by cli.py/preflight/tests) =======


def _codex_model_arg(model: str, default: str = "") -> list[str]:
    """`-m` for a codex turn: the per-role model if codex-native, else `default`.

    RunConfig.agent_model/sim_model are provider-namespaced for opencode
    (`openrouter/...`, `opencode/...`); codex on a ChatGPT account rejects those
    ("model is not supported when using Codex with a ChatGPT account"). So a
    namespaced (or empty) per-role model falls back to `default` — a codex-native
    name like "gpt-5.5" (config.codex_model). A bare per-role name (no `/`,
    e.g. "gpt-5-codex") is itself codex-native and is honored as-is. Returns []
    only when neither yields a name (codex then uses its account default).
    """

    chosen = model if (model and "/" not in model) else default
    return ["-m", chosen] if chosen else []


def _codex_effort_arg(effort: str) -> list[str]:
    """`-c model_reasoning_effort=<effort>`, or [] when unset.

    Exec-level config override (applied before the `resume` subcommand), so it
    pins reasoning effort on every turn, not just the first.
    """

    return ["-c", f"model_reasoning_effort={effort}"] if effort else []


def _access_token_exp(auth_path: Path) -> int | None:
    """Return the access_token JWT's `exp` (unix seconds), or None.

    The codex access_token is a 3-part JWT; we base64url-decode the payload
    (no signature check — we only read the expiry) and pull `exp`. None for an
    opaque/non-JWT token or any parse failure.
    """

    try:
        token = json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]["access_token"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    return exp if isinstance(exp, int) else None


def _parse_codex_events(
    events_path: Path, *, start_offset: int = 0
) -> tuple[str, str | None, dict | None, str | None]:
    """Extract (final_text, session_id, usage, error) from codex `--json` stdout.

    codex emits JSONL: `thread.started` (carries thread_id = session id),
    `item.completed` (the `agent_message` item carries the final text),
    `turn.completed` (carries a `usage` token breakdown), and on failure
    `turn.failed` / `error`. We take the LAST agent_message as the final reply.

    `start_offset` (a byte offset) scopes parsing to a single turn's slice of a
    multi-turn raw_export — the runner streams every turn into the same file.
    """

    final_text = ""
    session_id: str | None = None
    usage: dict | None = None
    error: str | None = None
    if not events_path.exists():
        return final_text, session_id, usage, error
    with events_path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(start_offset)
        slice_text = fh.read()
    for line in slice_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "thread.started":
            session_id = ev.get("thread_id") or session_id
        elif etype == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final_text = text
        elif etype == "turn.completed":
            u = ev.get("usage")
            if isinstance(u, dict):
                usage = u
        elif etype == "turn.failed":
            err = ev.get("error")
            if isinstance(err, dict):
                error = err.get("message") or json.dumps(err)
        elif etype == "error" and error is None:
            error = ev.get("message")
    return final_text, session_id, usage, error


# ===== claude helpers =========================================================


def _claude_model_arg(model: str, default: str = "") -> list[str]:
    """`--model` for a claude turn: the per-role model if claude-native, else `default`.

    Mirrors `_codex_model_arg`: agent_model/sim_model are opencode-namespaced
    (`openrouter/...`), which aren't claude model names; a namespaced (or empty)
    per-role model falls back to `default` (config.claude_model). A bare name
    (no `/`, e.g. "opus" or "claude-opus-4-8") is claude-native and honored.
    Returns [] when neither yields a name (claude then uses the ~/.claude
    account default).
    """

    chosen = model if (model and "/" not in model) else default
    return ["--model", chosen] if chosen else []


def _claude_effort_arg(effort: str) -> list[str]:
    """`--effort <level>` for a claude turn, or [] when unset.

    A per-turn flag (low|medium|high|max), so it pins effort on every turn
    including `--resume`. The codex analog is the `-c model_reasoning_effort`
    override.
    """

    return ["--effort", effort] if effort else []


def _parse_claude_events(
    events_path: Path, *, start_offset: int = 0
) -> tuple[str, str | None, dict | None, str | None]:
    """Extract (final_text, session_id, usage, error) from claude `stream-json` stdout.

    claude `-p --output-format stream-json --verbose` emits JSONL:
      - `{"type":"system","subtype":"init","session_id",...}` — session id,
      - `{"type":"assistant","message":{"content":[...]},...}` — interim turns,
      - `{"type":"result","subtype":"success"|...,"result":"<final text>",
         "usage":{...},"total_cost_usd",...}` — the authoritative final reply.
    The final text is `result.result` (not concatenated assistant blocks). An
    error is a non-`success` subtype or `is_error`. `start_offset` scopes parsing
    to one turn's slice of the multi-turn raw_export.
    """

    final_text = ""
    session_id: str | None = None
    usage: dict | None = None
    error: str | None = None
    if not events_path.exists():
        return final_text, session_id, usage, error
    with events_path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(start_offset)
        slice_text = fh.read()
    for line in slice_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            session_id = ev.get("session_id") or session_id
        elif etype == "result":
            res = ev.get("result")
            if isinstance(res, str):
                final_text = res
            u = ev.get("usage")
            if isinstance(u, dict):
                usage = u
            subtype = ev.get("subtype")
            if ev.get("is_error") or (subtype is not None and subtype != "success"):
                error = (res if isinstance(res, str) and res else None) or (
                    subtype or "claude turn failed"
                )
            session_id = session_id or ev.get("session_id")
        elif session_id is None and ev.get("session_id"):
            session_id = ev.get("session_id")
    return final_text, session_id, usage, error


# ===== shared timestamp back-fill =============================================


def _stamp_event_slice(
    events_path: Path,
    *,
    start_offset: int,
    t_start: float,
    t_end: float,
) -> None:
    """Back-fill real per-event `timestamp`s onto this turn's CLI event slice.

    Both codex `exec --json` and claude `stream-json` emit clockless events, so
    without this every CLI event lands timestamp-less on disk and any post-hoc
    reader (the viewer, a TUI attach) can only guess at when things happened. We
    can't observe each line's arrival (the runner pipes the CLI's stdout straight
    to the file), but we DO know the turn's real wall-clock window — so we
    interpolate evenly across `[t_start, t_end]` in stream order. The final
    message / result thus lands at ~turn-end, which is what the chat orders by.

    Only this turn's slice (`start_offset` -> EOF) is rewritten; earlier turns
    were stamped by their own call. Events that already carry a `timestamp` are
    left untouched, and the line COUNT is preserved so the TUI's line-index tail
    is unaffected. Best effort: any I/O or decode failure leaves the slice as-is.
    """

    try:
        with events_path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(start_offset)
            slice_text = fh.read()
    except OSError:
        return
    if not slice_text.strip():
        return

    raw_lines = slice_text.splitlines()
    content_positions = [i for i, ln in enumerate(raw_lines) if ln.strip()]
    n = len(content_positions)
    rank = {line_idx: k for k, line_idx in enumerate(content_positions)}

    start_ms = int(t_start * 1000)
    span_ms = max(int(t_end * 1000) - start_ms, 0)

    out_lines: list[str] = []
    changed = False
    for i, ln in enumerate(raw_lines):
        if not ln.strip():
            out_lines.append(ln)
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            out_lines.append(ln)
            continue
        if isinstance(ev, dict) and "timestamp" not in ev:
            k = rank[i]
            offset = int(k / (n - 1) * span_ms) if n > 1 else span_ms
            ev["timestamp"] = start_ms + offset
            out_lines.append(json.dumps(ev, ensure_ascii=False))
            changed = True
        else:
            out_lines.append(ln)

    if not changed:
        return
    try:
        with events_path.open("r+", encoding="utf-8") as fh:
            fh.seek(start_offset)
            fh.truncate()
            fh.write("\n".join(out_lines) + "\n")
    except OSError:
        return


# ===== tool drivers ===========================================================


class CliDriver(Protocol):
    """The tool-specific seams of a CLI actor turn.

    `CliActorRunner` owns everything generic; a driver supplies only what differs
    between codex and claude: where the home mounts, how the home is prepared,
    the in-container CLI argv, the docker-run env (token inject/scrub), event
    parsing, the pre-launch auth gate, and (claude) a fresh session id to set.
    """

    name: str
    home_mount_target: str  # container path the per-run home mounts at
    home_dir_prefix: str  # per-run home dir names: f"{prefix}-{role}-home"

    def prepare_home(self, home: Path) -> Path: ...

    def ensure_ready(self) -> None: ...

    def inner_argv(
        self,
        *,
        prompt: str,
        session_id: str | None,
        model: str,
        config: RunConfig,
    ) -> list[str]: ...

    def container_env(self, base: dict[str, str]) -> dict[str, str]: ...

    def container_env_names(self) -> list[str]: ...

    def parse_events(
        self, events_path: Path, *, start_offset: int
    ) -> tuple[str, str | None, dict | None, str | None]: ...


class CodexDriver:
    """codex seams: subscription auth (neutered, per-run, self-healing) + `codex exec`."""

    name = "codex"
    home_mount_target = "/root/.codex"
    home_dir_prefix = "codex"

    def __init__(self, config: RunConfig):
        self.config = config
        # Source of truth for the operator's subscription token. Overridable so
        # tests can point at a fixture instead of a real ~/.codex.
        self.src_codex_home = Path.home() / ".codex"

    def ensure_ready(self) -> None:
        # The JWT-expiry gate runs inside prepare_home (it needs the home), so
        # there is nothing extra to assert before launch.
        return None

    def prepare_home(self, home: Path) -> Path:
        """Seed/refresh a per-run CODEX_HOME whose refresh_token is neutered.

        Re-seeds auth.json EVERY turn from the host source (self-healing against
        codex deleting it on a failed refresh), preserving any sessions/ already
        in the home so `resume` keeps working. Returns the dir to mount RW.
        """

        self._ensure_fresh_access_token()
        auth = json.loads((self.src_codex_home / "auth.json").read_text(encoding="utf-8"))
        tokens = auth.setdefault("tokens", {})
        real_refresh = tokens.get("refresh_token", "")
        tokens["refresh_token"] = _NEUTERED_REFRESH_TOKEN
        home.mkdir(parents=True, exist_ok=True)
        auth_path = home / "auth.json"
        auth_path.write_text(json.dumps(auth), encoding="utf-8")
        # Hard guard: the real standing credential must NOT reach the mount.
        if real_refresh and real_refresh != _NEUTERED_REFRESH_TOKEN:
            assert real_refresh not in auth_path.read_text(encoding="utf-8")
        return home

    def _ensure_fresh_access_token(self) -> None:
        """Refresh the host token when the access JWT is near expiry.

        Common case (token is a ~10-day JWT): nothing to do. Near expiry: trigger
        a host-side refresh (the real refresh_token stays on the host, never in a
        container), then re-check and raise only if it didn't renew — codex would
        otherwise try, and with the neutered in-container token fail, to refresh
        mid-run.
        """

        auth = self.src_codex_home / "auth.json"
        exp = _access_token_exp(auth)
        if exp is None or exp - time.time() > _REFRESH_MARGIN_SECONDS:
            return
        self._host_refresh_token()
        exp = _access_token_exp(auth)
        if exp is None or exp - time.time() <= _REFRESH_MARGIN_SECONDS:
            raise ActorError(
                "codex access token is near expiry and host auto-refresh did not "
                "renew it; run `codex login` on the host to re-authenticate."
            )

    def _host_refresh_token(self) -> None:
        """Refresh the host codex token via its auth manager (no model call).

        `codex login status` initialises codex's auth manager, which refreshes a
        near-expiry access token in place using the real refresh_token — which
        stays on the host and never enters any container. Best-effort: failures
        are swallowed; the caller re-checks expiry and raises if still stale.
        """

        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.src_codex_home)
        try:
            subprocess.run(
                ["codex", "login", "status"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def inner_argv(
        self,
        *,
        prompt: str,
        session_id: str | None,
        model: str,
        config: RunConfig,
    ) -> list[str]:
        # PROVEN flag order: exec-level opts BEFORE the `resume` subcommand.
        inner = ["codex", "exec", "-s", "danger-full-access", "--skip-git-repo-check", "--json"]
        # Reasoning effort is an exec-level `-c` override → pin it on EVERY turn
        # (resume included). Model is set only on the first turn; a resumed
        # session carries its own.
        inner += _codex_effort_arg(config.codex_effort)
        if session_id:
            inner += ["resume", session_id, prompt]
        else:
            inner += _codex_model_arg(model, config.codex_model) + [prompt]
        return inner

    def container_env(self, base: dict[str, str]) -> dict[str, str]:
        # codex reads its token from the mounted home, not the env.
        return base

    def container_env_names(self) -> list[str]:
        return []

    def parse_events(
        self, events_path: Path, *, start_offset: int
    ) -> tuple[str, str | None, dict | None, str | None]:
        return _parse_codex_events(events_path, start_offset=start_offset)


class ClaudeDriver:
    """claude seams: headless OAuth token via env + `claude -p --output-format stream-json`."""

    name = "claude"
    # Mount the per-run state at /root/.claude/PROJECTS, not all of /root/.claude:
    # the image bakes globally-installed skills into /root/.claude/skills, and
    # mounting over the whole dir would shadow them (→ "Unknown skill"). claude
    # stores resumable sessions under projects/<cwd-slug>/<id>.jsonl, so a
    # projects-only mount preserves the image's skills AND keeps multi-turn resume.
    home_mount_target = "/root/.claude/projects"
    home_dir_prefix = "claude"

    def __init__(self, config: RunConfig):
        self.config = config

    def ensure_ready(self) -> None:
        """Refuse to launch without the headless OAuth token.

        The token is opaque and long-lived (~1yr), so we check presence, not
        expiry. It must be set on the HOST (`claude setup-token`) and is
        forwarded into the container by name.
        """

        if not os.environ.get(_CLAUDE_OAUTH_ENV):
            raise ActorError(
                f"{_CLAUDE_OAUTH_ENV} not set; run `claude setup-token` on the host "
                f"and export {_CLAUDE_OAUTH_ENV} (the claude CLI actor's subscription auth)."
            )

    def prepare_home(self, home: Path) -> Path:
        """Per-run claude session store — empty, mounted at /root/.claude/projects.

        No credential file is written (auth comes from the env token). The dir
        persists across turns so `projects/<cwd-slug>/<id>.jsonl` survives for
        `--resume`; mounting only projects/ (not all of /root/.claude) leaves the
        image's baked /root/.claude/skills visible. Mounted RW (claude writes the
        session JSONL here); everything else claude writes under /root/.claude
        lands on the container's ephemeral layer, which is fine.
        """

        home.mkdir(parents=True, exist_ok=True)
        return home

    def inner_argv(
        self,
        *,
        prompt: str,
        session_id: str | None,
        model: str,
        config: RunConfig,
    ) -> list[str]:
        inner = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
        ]
        inner += _claude_model_arg(model, config.claude_model)
        inner += _claude_effort_arg(config.claude_effort)
        # claude mints its own session id (it ignores a supplied --session-id in
        # `-p` mode), so — like codex — we capture it from the stream and resume
        # by it on the next turn rather than trying to set it.
        if session_id:
            inner += ["--resume", session_id]
        inner += [prompt]
        return inner

    def container_env(self, base: dict[str, str]) -> dict[str, str]:
        # Inject the OAuth token (value stays in the docker-run env, off argv) and
        # force-empty any API key so claude can't fall through to paid API usage.
        base[_CLAUDE_OAUTH_ENV] = os.environ.get(_CLAUDE_OAUTH_ENV, "")
        base["ANTHROPIC_API_KEY"] = ""
        base["ANTHROPIC_AUTH_TOKEN"] = ""
        # claude refuses `--permission-mode bypassPermissions` when running as
        # root unless it's told it's in a sandbox (it exits with "cannot be used
        # with root/sudo privileges"). The per-run container runs as root and IS
        # a sandbox (egress-locked, isolated, ephemeral), so set the documented
        # escape — exactly the guard claude checks: `IS_SANDBOX === "1"`.
        base["IS_SANDBOX"] = "1"
        return base

    def container_env_names(self) -> list[str]:
        # Forwarded `-e NAME` (value from the docker-run env, never inlined on argv).
        return [_CLAUDE_OAUTH_ENV, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "IS_SANDBOX"]

    def parse_events(
        self, events_path: Path, *, start_offset: int
    ) -> tuple[str, str | None, dict | None, str | None]:
        return _parse_claude_events(events_path, start_offset=start_offset)


def _make_driver(tool: str, config: RunConfig) -> CliDriver:
    if tool == "codex":
        return CodexDriver(config)
    if tool == "claude":
        return ClaudeDriver(config)
    raise ActorError(f"cli_actor: unknown cli_tool {tool!r} (expected 'codex' or 'claude')")


# ===== the runner =============================================================


class CliActorRunner:
    """Drive a frontier CLI headless in the per-run container as agent / SIM / reviewer.

    Implements the `ActorRunner` protocol (agent_turn / sim_turn / sim_review) so
    `make_actor_runner` returns it for `ActorMode.CLI`. Tool-specific behavior is
    delegated to `self.driver` (`CodexDriver` / `ClaudeDriver`).
    """

    def __init__(self, *, config: RunConfig, paths: RunPaths, tool: str = "codex"):
        self.config = config
        self.paths = paths
        self.worktree = paths.worktree
        self.tool = tool
        self.driver = _make_driver(tool, config)
        # Per-role homes persist across turns within a run, so session state
        # accumulates and `resume` works. Named by the driver so codex and claude
        # never collide in a mixed run.
        prefix = self.driver.home_dir_prefix
        self.agent_home = paths.run_dir / f"{prefix}-agent-home"
        self.sim_home = paths.run_dir / f"{prefix}-sim-home"
        self.review_home = paths.run_dir / f"{prefix}-review-home"
        self._agent_session: str | None = None
        self._sim_session: str | None = None

    # ----- protocol surface -------------------------------------------------

    def agent_turn(self, message: str) -> ActorOutput:
        return self._cli_turn(
            role="agent",
            prompt=message,
            raw_export=self.paths.raw_export,
            home=self.agent_home,
            mount_mode="rw",
            model=self.config.agent_model,
            session_attr="_agent_session",
            timeout_seconds=self.config.agent_timeout_seconds,
            phase="WORK",
            speaker="agent",
        )

    def sim_turn(self, message: str) -> ActorOutput:
        return self._cli_turn(
            role="sim",
            prompt=message,
            raw_export=self.paths.sim_raw_export,
            home=self.sim_home,
            mount_mode="ro",
            model=self.config.sim_model,
            session_attr="_sim_session",
            timeout_seconds=self.config.sim_timeout_seconds,
            phase="WORK",
            speaker="sim",
        )

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
        reviewer_id: str = "sim",
        model_override: str | None = None,
    ) -> ActorOutput:
        from .prompts import SIM_REVIEW_PROMPT

        # Fresh, read-only review context: the settled design + the diff under
        # /review, the worktree read-only at /app. Single-shot (no resume) so
        # each review attempt has clean context, mirroring OpencodeActorRunner.
        review_dir = self.paths.run_dir / f"cli_review_input_{reviewer_id}_{attempt}"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "SETTLED_DESIGN.md").write_text(
            settled_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (review_dir / "diff.patch").write_text(
            diff_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        raw_export = (
            self.paths.extra_reviewer_raw_export
            if reviewer_id == "extra"
            else self.paths.sim_raw_export
        )
        return self._cli_turn(
            role="review",
            prompt=SIM_REVIEW_PROMPT,
            raw_export=raw_export,
            home=self.review_home / f"{reviewer_id}-attempt-{attempt}",
            mount_mode="ro",
            model=model_override or self.config.sim_model,
            session_attr=None,
            timeout_seconds=self.config.sim_timeout_seconds,
            phase="REVIEW",
            speaker="sim",
            extra_mounts=((review_dir, "/review", "ro"),),
        )

    # ----- security-critical seam ------------------------------------------

    def _assert_egress_locked(self) -> None:
        """Refuse to launch unless egress is locked OR explicitly opened.

        The in-container subscription credential outlives the run, so the only
        thing standing between an injected `cat`/`printenv` and an attacker is the
        egress lock. By default require an `--internal` docker_network (no route,
        no external DNS) AND an allowlisting https_proxy (the sole exit).
        `allow_open_egress` is the explicit, warned escape hatch.
        """

        if self.config.allow_open_egress:
            return
        if not (self.config.docker_network and self.config.https_proxy):
            raise ActorError(
                "CLI actor refuses to launch without BOTH an --internal "
                "docker_network and an allowlisting https_proxy (provider-only "
                "egress); pass --allow-open-egress to override. The in-container "
                "subscription token is long-lived, so open egress is exfiltratable."
            )

    # ----- container invocation --------------------------------------------

    def _cli_turn(
        self,
        *,
        role: str,
        prompt: str,
        raw_export: Path,
        home: Path,
        mount_mode: str,
        model: str,
        session_attr: str | None,
        timeout_seconds: int,
        phase: str,
        speaker: str,
        extra_mounts: tuple[tuple[Path, str, str], ...] = (),
    ) -> ActorOutput:
        self._assert_egress_locked()
        self.driver.ensure_ready()
        home = self.driver.prepare_home(home)
        existing_session = getattr(self, session_attr) if session_attr else None

        # The CLI's JSON stdout IS the event stream; we point the detached
        # runner's stdout straight at raw_export so the TUI tails the CLI's
        # reasoning/command/message items LIVE as the turn runs. raw_export
        # accumulates across turns; we parse only THIS turn's slice via the
        # byte offset.
        start_offset = raw_export.stat().st_size if raw_export.exists() else 0
        raw_export.parent.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command(
            prompt=prompt,
            home=home,
            session_id=existing_session,
            model=model,
            mount_mode=mount_mode,
            role=role,
            extra_mounts=extra_mounts,
        )
        env = self.driver.container_env(self._docker_env())
        # Bracket the container with wall-clock so we can back-fill real
        # timestamps onto the (clockless) CLI event slice it appends.
        t_start = time.time()
        returncode, stderr, _fast_fail = _run_detached_container(
            cmd=cmd,
            env=env,
            stdout_path=raw_export,
            timeout_seconds=timeout_seconds,
            role=role,
            stdout_stall_seconds=self.config.opencode_stdout_stall_seconds or None,
        )
        _stamp_event_slice(
            raw_export, start_offset=start_offset, t_start=t_start, t_end=time.time()
        )

        final_text, parsed_session, _usage, error = self.driver.parse_events(
            raw_export, start_offset=start_offset
        )
        if returncode != 0:
            raise ActorError(f"{role} {self.tool} exited {returncode}: {error or stderr[:500]}")
        if error and not final_text:
            raise ActorError(f"{role} {self.tool} turn failed: {error}")

        # Stash the session id (the one the CLI reported) ONLY after a successful
        # turn, so a failed turn 1 starts fresh next time rather than resuming a
        # session that was never written. Both tools mint their own id; we keep
        # the first turn's and `--resume`/`resume` it on every later turn.
        if session_attr:
            new_session = existing_session or parsed_session
            if new_session:
                setattr(self, session_attr, new_session)
        # No synthetic events: the CLI stream in raw_export already carries the
        # final reply (TUI renders it) and usage (costs roll it up). Transcript
        # stays the curated, human-readable record of the final reply per turn.
        append_transcript(self.paths.transcript, speaker=speaker, phase=phase, text=final_text)
        return ActorOutput(text=final_text, stderr=stderr, returncode=returncode)

    def _build_command(
        self,
        *,
        prompt: str,
        home: Path,
        session_id: str | None,
        model: str,
        mount_mode: str,
        role: str,
        extra_mounts: tuple[tuple[Path, str, str], ...] = (),
    ) -> list[str]:
        """`docker run -d` argv for one CLI turn. The security-critical builder.

        Mounts: worktree -> /app:{rw|ro}; the per-run home -> the driver's mount
        target:rw (RW mandatory — the CLI writes session/state); any extra_mounts
        (e.g. the /review context). Proxy + driver env vars are forwarded by NAME
        only (`-e NAME`, value via the docker-run env), never inlined on argv.
        """

        inner = self.driver.inner_argv(
            prompt=prompt,
            session_id=session_id,
            model=model,
            config=self.config,
        )
        cmd = [
            "docker",
            "run",
            "-d",
            "--label",
            f"contremaitre.run-id={self.paths.run_id}",
            "--label",
            f"contremaitre.role={role}",
        ]
        if self.config.docker_network:
            cmd += ["--network", self.config.docker_network]
        if self.config.container_user:
            cmd += ["--user", self.config.container_user]
        cmd += [
            "-v",
            f"{home}:{self.driver.home_mount_target}:rw",
            "-v",
            f"{self.worktree}:/app:{mount_mode}",
        ]
        for host_path, container_path, mode in extra_mounts:
            cmd += ["-v", f"{host_path}:{container_path}:{mode}"]
        for var, val in (
            ("HTTP_PROXY", self.config.http_proxy),
            ("HTTPS_PROXY", self.config.https_proxy),
            ("NO_PROXY", self.config.no_proxy),
        ):
            if val:
                cmd += ["-e", var]
        for name in self.driver.container_env_names():
            cmd += ["-e", name]
        cmd += ["-w", "/app", self.config.docker_image, *inner]
        return cmd

    def _docker_env(self) -> dict[str, str]:
        """Env for the `docker run` subprocess: ambient + explicit proxy values.

        Proxy vars are set here (the value) and referenced by name in the argv
        (`-e HTTPS_PROXY`), so docker forwards the value into the container
        without it appearing on the command line. Driver-specific vars (e.g.
        claude's OAuth token) are layered on by `driver.container_env`.
        """

        env = os.environ.copy()
        for var, val in (
            ("HTTP_PROXY", self.config.http_proxy),
            ("HTTPS_PROXY", self.config.https_proxy),
            ("NO_PROXY", self.config.no_proxy),
        ):
            if val:
                env[var] = val
        return env
