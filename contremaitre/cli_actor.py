"""CLI-driven actor: run a frontier CLI (codex) headless in-container.

A third `ActorRunner` beside `OpencodeActorRunner` / `FakeActorRunner`. Where
opencode drives an OpenRouter model, this drives the `codex` CLI inside the
same per-run container, so the agent/SIM gets the CLI's own agentic loop
(tools, skills) — empirically a markedly stronger reviewer than the OpenRouter
models. Selected via `ActorMode.CLI` + `RunConfig.cli_tool`.

Every design choice below was pinned by in-container experiment, not guessed:

AUTH (subscription, hard-minimised — read before touching `prepare_codex_home`)
  The agent runs UNTRUSTED model-generated shell with codex's own sandbox off
  (`-s danger-full-access`): the container is the only boundary, and codex *is*
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

EGRESS (the load-bearing control — `_assert_egress_locked`)
  The access token is a ~10-DAY JWT, so the in-container credential outlives any
  single run. Exfiltration is prevented by a two-layer lock the runner REFUSES
  to launch without (unless `allow_open_egress`):
    1. an `--internal` `docker_network` — kills direct egress AND external DNS,
    2. an allowlisting `https_proxy` (provider domains only) as the sole exit.

MULTI-TURN: turn N writes a session rollout into the persisted home; turn N+1
  resumes it by id (`codex exec ... resume <id>`), carrying context across
  separate `docker run`s — the codex analog of opencode's `--session`.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

from .actors import ActorError, ActorOutput, _run_detached_container
from .jsonlog import append_text_event, append_transcript
from .models import RunConfig, RunPaths

# codex REQUIRES tokens.refresh_token present and non-empty (parser + refresh
# API both reject otherwise), so we neuter rather than drop it. Inert in
# practice: a valid access token in a writable home means codex never refreshes.
_NEUTERED_REFRESH_TOKEN = "x"

# Refuse to run when the access JWT has less than this remaining — codex may
# then try (and, with the neutered token, fail) to refresh mid-run. The token
# is a ~10-day JWT, so this rarely fires.
_REFRESH_MARGIN_SECONDS = 24 * 3600


def _codex_model_arg(model: str) -> list[str]:
    """`["-m", model]`, or `[]` when codex should use its subscription default.

    RunConfig.agent_model/sim_model are OpenRouter-namespaced (for opencode);
    codex on a ChatGPT account rejects those, so we omit -m and let codex pick
    its account default. A codex-native model name (e.g. "gpt-5.5") passes
    through.
    """

    if not model or model.startswith("openrouter/"):
        return []
    return ["-m", model]


def _append_usage_step_finish(raw_export: Path, usage: dict) -> None:
    """Append a codex turn's token usage as an opencode-shaped step_finish event.

    Routing usage through the SAME step_finish channel opencode uses means the
    TUI renders per-turn tokens (in/out/cache-r) with no CLI-specific code, and
    costs.* roll it up uniformly. No `cost` key: codex on a subscription is not
    metered per token, so recorded USD stays an honest $0 while tokens show.
    """

    tokens = {
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "reasoning": usage.get("reasoning_output_tokens", 0),
        "cache": {"read": usage.get("cached_input_tokens", 0)},
    }
    event = {
        "type": "step_finish",
        "timestamp": int(time.time() * 1000),
        "part": {"tokens": tokens},
    }
    raw_export.parent.mkdir(parents=True, exist_ok=True)
    with raw_export.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


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


def _parse_codex_events(events_path: Path) -> tuple[str, str | None, dict | None, str | None]:
    """Extract (final_text, session_id, usage, error) from codex `--json` stdout.

    codex emits JSONL: `thread.started` (carries thread_id = session id),
    `item.completed` (the `agent_message` item carries the final text),
    `turn.completed` (carries a `usage` token breakdown), and on failure
    `turn.failed` / `error`. We take the LAST agent_message as the final reply.
    """

    final_text = ""
    session_id: str | None = None
    usage: dict | None = None
    error: str | None = None
    if not events_path.exists():
        return final_text, session_id, usage, error
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
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


class CliActorRunner:
    """Drive codex headlessly in the per-run container as agent / SIM / reviewer.

    Implements the `ActorRunner` protocol (agent_turn / sim_turn / sim_review)
    so `make_actor_runner` returns it for `ActorMode.CLI`.
    """

    def __init__(self, *, config: RunConfig, paths: RunPaths, tool: str = "codex"):
        if tool != "codex":
            raise ActorError(
                f"cli_actor: only 'codex' is implemented (got {tool!r}); "
                "claude is pending a headless CLAUDE_CODE_OAUTH_TOKEN"
            )
        self.config = config
        self.paths = paths
        self.worktree = paths.worktree
        self.tool = tool
        # Source of truth for the operator's subscription token. Overridable so
        # tests can point at a fixture instead of a real ~/.codex.
        self._src_codex_home = Path.home() / ".codex"
        # Per-role homes persist across turns within a run, so session rollouts
        # accumulate and `resume` works; auth.json is re-seeded each turn.
        self.agent_home = paths.run_dir / "codex-agent-home"
        self.sim_home = paths.run_dir / "codex-sim-home"
        self.review_home = paths.run_dir / "codex-review-home"
        self._agent_session: str | None = None
        self._sim_session: str | None = None

    # ----- protocol surface -------------------------------------------------

    def agent_turn(self, message: str) -> ActorOutput:
        return self._codex_turn(
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
        return self._codex_turn(
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
        return self._codex_turn(
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
            events_basename=f"codex_review_{reviewer_id}_{attempt}_events.jsonl",
        )

    # ----- security-critical seams -----------------------------------------

    def _assert_egress_locked(self) -> None:
        """Refuse to launch unless BOTH containment layers are set.

        The in-container access token is a ~10-day JWT, so it outlives the run;
        the only thing standing between an injected `cat auth.json` and an
        attacker is the egress lock. Require an `--internal` docker_network
        (no route, no external DNS) AND an allowlisting https_proxy (the sole
        exit). `allow_open_egress` is the explicit, logged escape hatch.
        """

        if self.config.allow_open_egress:
            return
        if not (self.config.docker_network and self.config.https_proxy):
            raise ActorError(
                "CLI actor refuses to launch without BOTH an --internal "
                "docker_network and an allowlisting https_proxy (provider-only "
                "egress); pass allow_open_egress to override. The in-container "
                "subscription token is long-lived, so open egress is exfiltratable."
            )

    def _ensure_fresh_access_token(self) -> None:
        """Refresh the host token when the access JWT is near expiry.

        Common case (token is a ~10-day JWT): nothing to do. Near expiry: trigger
        a host-side refresh (the real refresh_token stays on the host, never in a
        container), then re-check and raise only if it didn't renew — codex would
        otherwise try, and with the neutered in-container token fail, to refresh
        mid-run.
        """

        auth = self._src_codex_home / "auth.json"
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
        env["CODEX_HOME"] = str(self._src_codex_home)
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

    def prepare_codex_home(self, home: Path) -> Path:
        """Seed/refresh a per-run CODEX_HOME whose refresh_token is neutered.

        Re-seeds auth.json EVERY turn from the host source (self-healing against
        codex deleting it on a failed refresh), preserving any sessions/ already
        in the home so `resume` keeps working. Returns the dir to mount RW.
        """

        self._ensure_fresh_access_token()
        auth = json.loads((self._src_codex_home / "auth.json").read_text(encoding="utf-8"))
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

    # ----- container invocation --------------------------------------------

    def _codex_turn(
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
        events_basename: str | None = None,
    ) -> ActorOutput:
        self._assert_egress_locked()
        codex_home = self.prepare_codex_home(home)
        session_id = getattr(self, session_attr) if session_attr else None

        # codex `--json` stdout IS the event stream; we point the detached
        # runner's stdout at a per-turn sidecar (keeps raw_export pure: we
        # append one opencode-shaped text event for the final reply, like the
        # fake actor, so downstream JSONL readers stay uniform).
        events_basename = events_basename or f"codex_{role}_events.jsonl"
        events_path = self.paths.run_dir / events_basename
        if events_path.exists():
            events_path.unlink()

        cmd = self._build_codex_command(
            prompt=prompt,
            codex_home=codex_home,
            session_id=session_id,
            model=model,
            mount_mode=mount_mode,
            role=role,
            extra_mounts=extra_mounts,
        )
        env = self._docker_env()
        returncode, stderr, _fast_fail = _run_detached_container(
            cmd=cmd,
            env=env,
            stdout_path=events_path,
            timeout_seconds=timeout_seconds,
            role=role,
            stdout_stall_seconds=self.config.opencode_stdout_stall_seconds or None,
        )

        final_text, parsed_session, usage, error = _parse_codex_events(events_path)
        if returncode != 0:
            raise ActorError(
                f"{role} codex exited {returncode}: {error or stderr[:500]}"
            )
        if error and not final_text:
            raise ActorError(f"{role} codex turn failed: {error}")

        if session_attr and parsed_session:
            setattr(self, session_attr, parsed_session)
        if usage is not None:
            _append_usage_step_finish(raw_export, usage)

        append_text_event(raw_export, role=role, phase=phase, text=final_text)
        append_transcript(self.paths.transcript, speaker=speaker, phase=phase, text=final_text)
        return ActorOutput(text=final_text, stderr=stderr, returncode=returncode)

    def _build_codex_command(
        self,
        *,
        prompt: str,
        codex_home: Path,
        session_id: str | None,
        model: str,
        mount_mode: str,
        role: str,
        extra_mounts: tuple[tuple[Path, str, str], ...] = (),
    ) -> list[str]:
        """`docker run -d` argv for one codex turn. The security-critical builder.

        Mounts: worktree -> /app:{rw|ro}; the per-run codex_home (neutered
        refresh_token) -> /root/.codex:rw (RW mandatory — codex writes
        PATH/app-server/sessions); any extra_mounts (e.g. the /review context).
        Egress proxy is forwarded by name only (value via -e), never inlined.
        """

        # PROVEN flag order: exec-level opts BEFORE the `resume` subcommand.
        inner = ["codex", "exec", "-s", "danger-full-access", "--skip-git-repo-check", "--json"]
        if session_id:
            inner += ["resume", session_id, prompt]
        else:
            # OpenRouter-namespaced models (the opencode default) are rejected
            # by codex on a ChatGPT account; omit -m so codex uses its
            # subscription default. A codex-native model name passes through.
            inner += _codex_model_arg(model) + [prompt]

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
            f"{codex_home}:/root/.codex:rw",
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
        cmd += ["-w", "/app", self.config.docker_image, *inner]
        return cmd

    def _docker_env(self) -> dict[str, str]:
        """Env for the `docker run` subprocess: ambient + explicit proxy values.

        Proxy vars are set here (the value) and referenced by name in the argv
        (`-e HTTPS_PROXY`), so docker forwards the value into the container
        without it appearing on the command line.
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
