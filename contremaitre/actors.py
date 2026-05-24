"""Actor process adapters.

The orchestrator depends on the small `ActorRunner` surface in this module.
`FakeActorRunner` uses deterministic subprocesses for tests and fixture
smoke runs. `OpencodeActorRunner` drives opencode-in-Docker for live runs.
Neither holds git, GitHub, diff-scan, or cap-enforcement responsibility —
those stay host-owned.

Protocol (multi-turn WORK session + single-shot REVIEW pass):

    agent_turn(message)        -> ActorOutput  # agent's reply, persistent session
    sim_turn(message)          -> ActorOutput  # SIM's reply, persistent session
    sim_review(...)            -> ActorOutput  # single-shot JSON verdict, fresh session

The hand-rolled multi-turn loop in the orchestrator drives these by
alternating agent_turn / sim_turn until `.contremaitre/IMPLEMENTATION_COMPLETE`
appears in the worktree.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import events
from .jsonlog import append_jsonl, append_text_event, append_transcript
from .models import ActorMode, RunConfig, RunPaths


class ActorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActorOutput:
    """One turn's text reply, after the actor has logged itself.

    Adapters own raw_export.jsonl + transcript.md writes for their own turns.
    The orchestrator just consumes `text` and moves on. No bool field telling
    the caller "did I log for you?" — that was a leaking-abstraction marker.
    """

    text: str
    stderr: str = ""
    returncode: int = 0


class ActorRunner(Protocol):
    def agent_turn(self, message: str) -> ActorOutput: ...

    def sim_turn(self, message: str) -> ActorOutput: ...

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
    ) -> ActorOutput: ...


# ------------------------------- Fake --------------------------------


class FakeActorRunner:
    """Deterministic subprocess actor for fixture smoke runs.

    The fake agent writes `.contremaitre/SETTLED_DESIGN.md`, a small
    implementation, and `.contremaitre/IMPLEMENTATION_COMPLETE` on its first
    turn, so the orchestrator's WORK loop terminates immediately. The fake
    SIM emits canned strings or strict JSON verdicts based on scenario.

    Owns its own raw_export + transcript writes. The orchestrator never
    reaches into either file on behalf of this adapter.
    """

    def __init__(self, *, paths: RunPaths, agent_scenario: str, sim_scenario: str):
        self.paths = paths
        self.agent_scenario = agent_scenario
        self.sim_scenario = sim_scenario

    def agent_turn(self, message: str) -> ActorOutput:
        return self._fake(
            ["agent", "--worktree", str(self.paths.worktree), "--scenario", self.agent_scenario],
            role="agent",
            phase="WORK",
            raw_export=self.paths.raw_export,
        )

    def sim_turn(self, message: str) -> ActorOutput:
        return self._fake(
            ["sim-turn"],
            role="sim",
            phase="WORK",
            raw_export=self.paths.sim_raw_export,
        )

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
    ) -> ActorOutput:
        return self._fake(
            [
                "sim-review",
                "--diff-file", str(diff_file),
                "--settled-file", str(settled_file),
                "--scenario", scenario,
                "--attempt", str(attempt),
            ],
            role="sim",
            phase="REVIEW",
            raw_export=self.paths.sim_raw_export,
        )

    def _fake(self, args: list[str], *, role: str, phase: str, raw_export: Path) -> ActorOutput:
        package_root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "PYTHONPATH": f"{package_root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        }
        cmd = [sys.executable, "-m", "contremaitre.fake_actor", *args]
        proc = subprocess.run(
            cmd,
            cwd=self.paths.worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise ActorError(f"fake actor failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
        text = proc.stdout.strip()
        # Wrap in opencode's text-event shape so downstream readers see uniform JSONL.
        append_text_event(raw_export, role=role, phase=phase, text=text)
        append_transcript(self.paths.transcript, speaker=role, phase=phase, text=text)
        return ActorOutput(text=text, stderr=proc.stderr, returncode=proc.returncode)


# ----------------------------- Opencode ------------------------------


class OpencodeActorRunner:
    """Run agent and SIM turns through opencode inside Docker.

    The agent gets a writable `/app` mount and one persistent session across
    WORK turns. The SIM gets the same worktree as a read-only mount and one
    persistent session across WORK turns. The REVIEW pass uses a fresh SIM
    session with an additional read-only `/review` mount containing the
    settled design and the diff.

    GitHub credentials are never passed into either container.
    """

    def __init__(self, *, config: RunConfig, paths: RunPaths):
        self.config = config
        self.paths = paths
        self.worktree = paths.worktree
        self.agent_state = paths.run_dir / "opencode-agent-state"
        self.sim_state = paths.run_dir / "opencode-sim-state"
        self.review_state = paths.run_dir / "opencode-review-state"
        self.agent_state.mkdir(parents=True, exist_ok=True)
        self.sim_state.mkdir(parents=True, exist_ok=True)
        self.review_state.mkdir(parents=True, exist_ok=True)
        self._agent_session: str | None = None
        self._sim_session: str | None = None

    def agent_turn(self, message: str) -> ActorOutput:
        return self._opencode_turn(
            role="agent",
            prompt=message,
            raw_export=self.paths.raw_export,
            state_dir=self.agent_state,
            mount_mode="rw",
            model=self.config.agent_model,
            timeout_seconds=self.config.agent_timeout_seconds,
            session_attr="_agent_session",
        )

    def sim_turn(self, message: str) -> ActorOutput:
        return self._opencode_turn(
            role="sim",
            prompt=message,
            raw_export=self.paths.sim_raw_export,
            state_dir=self.sim_state,
            mount_mode="ro",
            model=self.config.sim_model,
            timeout_seconds=self.config.sim_timeout_seconds,
            session_attr="_sim_session",
        )

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
    ) -> ActorOutput:
        from . import prompts

        review_dir = self.paths.run_dir / "review_input"
        review_dir.mkdir(exist_ok=True)
        (review_dir / "SETTLED_DESIGN.md").write_text(
            settled_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (review_dir / "diff.patch").write_text(diff_file.read_text(encoding="utf-8"), encoding="utf-8")
        # Fresh session every review attempt so the SIM has clean context.
        attempt_state = self.review_state / f"attempt-{attempt}"
        attempt_state.mkdir(parents=True, exist_ok=True)
        return self._opencode_turn(
            role="review",
            prompt=prompts.SIM_REVIEW_PROMPT,
            raw_export=self.paths.sim_raw_export,
            state_dir=attempt_state,
            mount_mode="ro",
            model=self.config.sim_model,
            timeout_seconds=self.config.sim_timeout_seconds,
            session_attr=None,
            extra_mounts=[(review_dir, "/review", "ro")],
        )

    def _opencode_turn(
        self,
        *,
        role: str,
        prompt: str,
        raw_export: Path,
        state_dir: Path,
        mount_mode: str,
        model: str,
        timeout_seconds: int,
        session_attr: str | None,
        extra_mounts: list[tuple[Path, str, str]] | None = None,
    ) -> ActorOutput:
        pre_text_count = _count_text_events(raw_export)
        session_id = getattr(self, session_attr) if session_attr else None
        cmd, env = build_docker_command(
            config=self.config,
            paths=self.paths,
            worktree=self.worktree,
            state_dir=state_dir,
            mount_mode=mount_mode,
            model=model,
            prompt=prompt,
            session_id=session_id,
            extra_mounts=extra_mounts or [],
            role=role,
        )
        append_jsonl(
            self.paths.guardrail_events,
            {
                "event": events.OPENCODE_ACTOR_START,
                "role": role,
                "mount_mode": mount_mode,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "cmd_redacted": redact_command(cmd),
            },
        )
        raw_export.parent.mkdir(parents=True, exist_ok=True)
        returncode, stderr = _run_detached_container(
            cmd=cmd,
            env=env,
            stdout_path=raw_export,
            timeout_seconds=timeout_seconds,
            role=role,
        )
        if returncode != 0:
            raise ActorError(f"{role} opencode exited {returncode}: {stderr[:500]}")
        new_session_id = _latest_session_id(raw_export)
        if new_session_id and session_attr:
            setattr(self, session_attr, new_session_id)
        post_text_count = _count_text_events(raw_export)
        if post_text_count == pre_text_count:
            # Provider/API error scoped to events emitted DURING this turn
            # (after pre_text_count). Avoids tripping on stale errors from
            # earlier turns left in the multi-turn stream.
            error = _latest_error_after_text_count(raw_export, pre_text_count)
            if error:
                raise ActorError(f"{role} opencode emitted error without text: {error[:500]}")
            # opencode silent-stall: the text part landed in opencode's sqlite
            # but the corresponding `text` event was never flushed to stdout
            # before docker exited. Read it back from the DB and synthesize
            # the missing event so downstream tooling sees a uniform stream.
            recovered, msg_id, completed = _recover_text_from_sqlite(
                state_dir, session_id or new_session_id
            )
            if recovered:
                _append_synthetic_text_event(
                    raw_export, recovered, msg_id, session_id or new_session_id
                )
                _record_recovery(
                    self.paths,
                    kind=events.SQLITE_RECOVERY_SILENT_STALL,
                    role=role,
                    recovered_chars=len(recovered),
                    message_id=msg_id,
                    step_finish_completed=completed,
                )
                self._append_transcript(role=role, text=recovered)
                self._harvest_step_finishes(role=role, state_dir=state_dir, raw_export=raw_export)
                return ActorOutput(text=recovered, stderr=stderr, returncode=returncode)
            raise ActorError(
                f"{role} opencode emitted no text and sqlite recovery found nothing"
            )
        latest = _latest_text(raw_export)
        self._append_transcript(role=role, text=latest)
        self._harvest_step_finishes(role=role, state_dir=state_dir, raw_export=raw_export)
        return ActorOutput(text=latest, stderr=stderr, returncode=returncode)

    def _harvest_step_finishes(self, *, role: str, state_dir: Path, raw_export: Path) -> None:
        """Backfill step_finish events for subagent sessions + stragglers.

        Mirrors the sqlite-recovery pattern: opencode's stdout is the
        source of truth when it's complete, the DB is the source of truth
        when it isn't. Subagent (child) sessions are *always* invisible to
        the parent's stdout, so harvesting is normal flow — not a recovery —
        and we use guardrail_events rather than recoveries.jsonl.
        """

        count = _harvest_step_finishes_from_sqlite(state_dir, raw_export)
        if count:
            append_jsonl(
                self.paths.guardrail_events,
                {
                    "event": events.SUBAGENT_STEP_FINISH_HARVESTED,
                    "role": role,
                    "count": count,
                },
            )

    def _append_transcript(self, *, role: str, text: str) -> None:
        # Roles: "agent" / "sim" / "review". The review role is the SIM doing
        # the review pass; transcript speaker stays "sim" so both WORK and
        # REVIEW SIM turns interleave under one identity.
        phase = "REVIEW" if role == "review" else "WORK"
        speaker = "sim" if role == "review" else role
        append_transcript(self.paths.transcript, speaker=speaker, phase=phase, text=text)


def make_actor_runner(*, config: RunConfig, paths: RunPaths) -> ActorRunner:
    if config.actor_mode == ActorMode.FAKE:
        return FakeActorRunner(
            paths=paths,
            agent_scenario=config.agent_scenario,
            sim_scenario=config.sim_scenario,
        )
    if config.actor_mode == ActorMode.OPENCODE:
        return OpencodeActorRunner(config=config, paths=paths)
    raise ActorError(f"unknown actor mode: {config.actor_mode}")


def build_docker_command(
    *,
    config: RunConfig,
    paths: RunPaths,
    worktree: Path,
    state_dir: Path,
    mount_mode: str,
    model: str,
    prompt: str,
    session_id: str | None,
    role: str,
    extra_mounts: list[tuple[Path, str, str]] | None = None,
) -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    env_var = config.openrouter_env_var
    if env_var not in env:
        raise ActorError(f"{env_var} is required for opencode actor mode")
    proxy_vars: list[str] = []
    if config.http_proxy:
        env["HTTP_PROXY"] = config.http_proxy
        proxy_vars.append("HTTP_PROXY")
    if config.https_proxy:
        env["HTTPS_PROXY"] = config.https_proxy
        proxy_vars.append("HTTPS_PROXY")
    if config.no_proxy:
        env["NO_PROXY"] = config.no_proxy
        proxy_vars.append("NO_PROXY")

    opencode_cmd = [
        "/root/.opencode/bin/opencode",
        "run",
        "--dangerously-skip-permissions",
        "--format",
        "json",
        "--model",
        model,
    ]
    if session_id:
        opencode_cmd.extend(["--session", session_id])
    opencode_cmd.append(prompt)

    # Detached so the container's lifecycle is owned by the docker daemon,
    # not by this python process: terminal close / SIGHUP no longer
    # orphans the run, signal handlers can `docker stop` by label, and
    # we get the container id back on stdout without a cidfile.
    cmd = ["docker", "run", "-d"]
    cmd.extend(["--label", f"contremaitre.run-id={paths.run_id}"])
    cmd.extend(["--label", f"contremaitre.role={role}"])
    if config.container_user:
        cmd.extend(["--user", config.container_user])
    if config.docker_network:
        cmd.extend(["--network", config.docker_network])
    cmd.extend(
        [
            "-v",
            f"{paths.run_dir}:/results",
            "-v",
            f"{state_dir}:/root/.local/share/opencode",
            "-v",
            f"{worktree}:/app:{mount_mode}",
        ]
    )
    if config.deps_volume:
        # Lockhash-keyed deps volume, RW so the agent can install
        # mid-run when the design genuinely needs a new dep (test
        # framework, lint plugin, etc.). The trade-off: parallel runs
        # against the same lockfile share the volume and can race on
        # writes. Acceptable for solo-operator sequential workflow;
        # revisit if multi-run-in-parallel becomes a real pattern.
        # Mounted over the worktree bind at /app/{mount_path}; the
        # worktree's own copy of that directory (if any) is shadowed.
        cmd.extend(["-v", f"{config.deps_volume.name}:/app/{config.deps_volume.mount_path}:rw"])
        for key, value in config.deps_volume.runtime_env:
            cmd.extend(["-e", f"{key}={value}"])
    if config.opencode_config:
        cmd.extend(["-v", f"{config.opencode_config}:/app/opencode.json:ro"])
    for host_path, container_path, mode in extra_mounts or []:
        cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
    cmd.extend(["-e", env_var])
    for proxy_var in proxy_vars:
        cmd.extend(["-e", proxy_var])
    cmd.extend(["-w", "/app", config.docker_image, *opencode_cmd])
    return cmd, env


def _run_detached_container(
    *,
    cmd: list[str],
    env: dict[str, str],
    stdout_path: Path,
    timeout_seconds: int,
    role: str,
) -> tuple[int, str]:
    """Start the detached container, stream its logs, wait for exit.

    Returns (returncode, stderr). On timeout: docker stop the container,
    then raise — same surface the previous Popen-based runner had so the
    orchestrator's caller doesn't need to know about the detached model.
    """

    create = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if create.returncode != 0:
        raise ActorError(f"{role} docker run -d failed: {create.stderr[:500]}")
    container_id = create.stdout.strip()
    if not container_id:
        raise ActorError(f"{role} docker run -d produced no container id")

    log_proc: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("ab") as stdout_f:
            log_proc = subprocess.Popen(
                ["docker", "logs", "-f", container_id],
                stdout=stdout_f,
                stderr=subprocess.PIPE,
            )
            try:
                wait = subprocess.run(
                    ["docker", "wait", container_id],
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                subprocess.run(
                    ["docker", "stop", "-t", "5", container_id],
                    capture_output=True,
                    timeout=15,
                )
                log_proc.kill()
                raise ActorError(f"{role} opencode timed out after {timeout_seconds}s") from exc
            try:
                log_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log_proc.kill()
            stderr_bytes = log_proc.stderr.read() if log_proc.stderr else b""
        returncode = int(wait.stdout.strip() or "1")
        return returncode, stderr_bytes.decode("utf-8", errors="replace")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            timeout=15,
        )


def redact_command(cmd: list[str]) -> list[str]:
    """Keep logs useful without exposing long prompts."""

    redacted = list(cmd)
    if redacted:
        redacted[-1] = f"<prompt {len(redacted[-1])} chars>"
    return redacted


def _read_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _count_text_events(path: Path) -> int:
    return sum(1 for event in _read_events(path) if event.get("type") == "text")


def _latest_text(path: Path) -> str:
    for event in reversed(_read_events(path)):
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    return text
    return ""


def _latest_session_id(path: Path) -> str | None:
    for event in reversed(_read_events(path)):
        session = event.get("sessionID") or event.get("session_id")
        if isinstance(session, str):
            return session
    return None


def _latest_error_after_text_count(path: Path, baseline_text_count: int) -> str | None:
    """Return the latest error event that arrived AFTER the Nth text event.

    Multi-turn streams accumulate errors from old turns. When checking
    whether the *current* turn failed, ignore errors from prior turns.
    """

    events = _read_events(path)
    seen_text = 0
    cutoff_idx = 0
    for i, event in enumerate(events):
        if event.get("type") == "text":
            seen_text += 1
            if seen_text > baseline_text_count:
                cutoff_idx = i
                break
    else:
        # No new text event landed; everything after `baseline_text_count` text events counts.
        cutoff_idx = 0
        seen_text = 0
        for i, event in enumerate(events):
            if event.get("type") == "text":
                seen_text += 1
            if seen_text >= baseline_text_count:
                cutoff_idx = i + 1
                break
    for event in reversed(events[cutoff_idx:]):
        if event.get("type") == "error":
            return json.dumps(event, sort_keys=True)
    return None


def _record_recovery(paths: RunPaths, *, kind: str, **fields) -> None:
    """Append a recovery event to both recoveries.jsonl and guardrail_events.

    `recoveries.jsonl` is the forensic capture for sqlite recoveries +
    SIGTERM emergency writes — events that aren't normal control-plane
    flow but matter for post-mortem analysis. Mirrored in guardrail_events
    so a single tail catches them too.
    """

    record = {"kind": kind, **fields}
    append_jsonl(paths.recoveries, record)
    append_jsonl(paths.guardrail_events, {"event": f"recovery_{kind}", **fields})


def _recover_text_from_sqlite(
    state_dir: Path, session_id: str | None
) -> tuple[str | None, str | None, bool]:
    """Recover the latest message's text from opencode's sqlite.

    Failure mode this addresses: opencode persists message parts to its
    SQLite (text + step-finish with reason='stop') but sometimes does NOT
    flush the corresponding `text` event to its --format=json stdout
    before the docker process exits. The data is intact in the DB; we
    read it back.

    Returns: (text, message_id, completed). `completed` is True if the
    message has a step-finish part with reason='stop' — i.e. genuinely
    finished, not mid-stream.
    """

    db_path = state_dir / "opencode.db"
    if not db_path.exists():
        return None, None, False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        cur = conn.cursor()
        sess_id = session_id
        if sess_id is None:
            row = cur.execute(
                "SELECT id FROM session ORDER BY time_created DESC LIMIT 1"
            ).fetchone()
            if not row:
                conn.close()
                return None, None, False
            sess_id = row[0]
        msg_row = cur.execute(
            "SELECT id FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 1",
            (sess_id,),
        ).fetchone()
        if not msg_row:
            conn.close()
            return None, None, False
        msg_id = msg_row[0]
        parts = cur.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY time_created ASC",
            (msg_id,),
        ).fetchall()
        conn.close()
        if not parts:
            return None, msg_id, False
        text_chunks: list[str] = []
        completed = False
        for (data_str,) in parts:
            try:
                part = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            ptype = part.get("type")
            if ptype == "text":
                chunk = part.get("text", "")
                if isinstance(chunk, str):
                    text_chunks.append(chunk)
            elif ptype == "step-finish" and part.get("reason") == "stop":
                completed = True
        if not text_chunks:
            return None, msg_id, completed
        return "".join(text_chunks), msg_id, completed
    except sqlite3.Error:
        return None, None, False


def _append_synthetic_text_event(
    raw_export: Path, text: str, message_id: str | None, session_id: str | None
) -> None:
    """Append a synthetic text event so downstream tooling sees a uniform stream."""

    event = {
        "type": "text",
        "timestamp": int(time.time() * 1000),
        "sessionID": session_id,
        "_recovered_from_sqlite": True,
        "_message_id": message_id,
        "part": {"text": text},
    }
    raw_export.parent.mkdir(parents=True, exist_ok=True)
    with raw_export.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _existing_step_finish_part_ids(path: Path) -> set[str]:
    """Collect `part.id` values for step_finish events already in raw_export.

    Used by `_harvest_step_finishes_from_sqlite` to dedupe — we never want
    to re-emit a step_finish opencode's stdout already streamed, nor one we
    synthesized on a previous turn.
    """

    ids: set[str] = set()
    for event in _read_events(path):
        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if isinstance(part, dict):
            pid = part.get("id")
            if isinstance(pid, str):
                ids.add(pid)
    return ids


def _harvest_step_finishes_from_sqlite(state_dir: Path, raw_export: Path) -> int:
    """Append synthetic `step_finish` events for parts opencode never streamed.

    Two undercounts this addresses:

    (a) Subagent (child) sessions spawned via the `task` tool log their
        step-finish parts to the same opencode.db as the parent, but their
        events never flow into the parent invocation's --format=json stdout.
        Each parent turn that uses subagents leaves their cost invisible to
        the recorded-cost estimator.

    (b) Even for the parent session, the final step-finish part sometimes
        lands in the DB after docker exits without flushing to stdout.

    We walk every `part` row in the DB whose JSON has `type == "step-finish"`
    and synthesize a `step_finish` event matching the stdout envelope shape
    for any whose `part.id` isn't already in raw_export. The cost estimator
    (`costs.estimate_recorded_cost_usd`) walks event JSON recursively for
    `cost`-like keys, so synthesized parts contribute identically to real
    ones without any estimator change.

    Idempotent: dedupe is by `part.id`, so calling this every turn is safe
    even when child sessions persist across turns.

    Returns: number of synthetic events appended.
    """

    db_path = state_dir / "opencode.db"
    if not db_path.exists():
        return 0
    existing_ids = _existing_step_finish_part_ids(raw_export)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, session_id, message_id, data, time_created "
            "FROM part ORDER BY time_created ASC"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return 0
    new_events: list[dict[str, object]] = []
    now_ms = int(time.time() * 1000)
    for part_id, session_id, message_id, data_str, time_created in rows:
        # The `id` lives in the table column, not the JSON blob — opencode
        # only injects it into the envelope when streaming to stdout. The
        # `message_id` is also in a column. The JSON has the type / cost /
        # tokens / reason fields; we reassemble the envelope shape that
        # stdout emits.
        if not isinstance(part_id, str) or part_id in existing_ids:
            continue
        try:
            part_data = json.loads(data_str)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(part_data, dict) or part_data.get("type") != "step-finish":
            continue
        part_envelope = {
            **part_data,
            "id": part_id,
            "messageID": message_id,
            "sessionID": session_id,
        }
        # opencode stores `time_created` as milliseconds since epoch; fall
        # back to wall clock if the column is missing or malformed.
        if isinstance(time_created, (int, float)) and time_created > 0:
            ts = int(time_created)
        else:
            ts = now_ms
        new_events.append(
            {
                "type": "step_finish",
                "timestamp": ts,
                "sessionID": session_id,
                "_synthesized_from_sqlite": True,
                "part": part_envelope,
            }
        )
        existing_ids.add(part_id)
    if not new_events:
        return 0
    raw_export.parent.mkdir(parents=True, exist_ok=True)
    with raw_export.open("a", encoding="utf-8") as f:
        for event in new_events:
            f.write(json.dumps(event) + "\n")
    return len(new_events)
