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
from .container import (
    ContainerLifecycle,
    DockerContainerLifecycle,
    _classify_fast_fail_marker,
    _FAST_FAIL_MARKERS,
    redact_command,
)
from .jsonlog import append_jsonl, append_text_event, append_transcript, read_jsonl
from .models import ActorMode, RunConfig, RunPaths


class ActorError(RuntimeError):
    """Generic actor failure surface.

    `kind` is a free-form tag that lets callers (and the TUI) distinguish
    failure modes worth a custom label without parsing the message string.
    Defaults to `None` for legacy errors that don't classify themselves.
    """

    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


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
        reviewer_id: str = "sim",
        model_override: str | None = None,
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
                "--diff-file",
                str(diff_file),
                "--settled-file",
                str(settled_file),
                "--scenario",
                scenario,
                "--attempt",
                str(attempt),
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
            raise ActorError(
                f"fake actor failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}"
            )
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

    def __init__(
        self, *, config: RunConfig, paths: RunPaths, container: ContainerLifecycle | None = None
    ):
        self.config = config
        self.paths = paths
        self.worktree = paths.worktree
        self.container = container or DockerContainerLifecycle()
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
        (review_dir / "diff.patch").write_text(
            diff_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        # Fresh session every review attempt so the SIM has clean context.
        attempt_state = self.review_state / f"sim-attempt-{attempt}"
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
            reviewer_id="sim",
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
        reviewer_id: str | None = None,
    ) -> ActorOutput:
        # Retry wrapper: on transient upstream symptoms (a provider error
        # surfaced into the stream, or a dual-signal stdout+log stall),
        # kill the failed attempt and re-run the turn. Quota errors,
        # wall-clock timeouts, and bare `exited N` failures bypass this
        # — those are terminal.
        retryable_kinds = (events.PROVIDER_TRANSIENT_ERROR, events.OPENCODE_STALL)
        max_retries = self.config.opencode_transient_retry_max
        backoff = self.config.opencode_transient_retry_backoff_seconds
        attempt = 1
        while True:
            events_offset = _count_jsonl_events(raw_export)
            try:
                return self._opencode_turn_attempt(
                    role=role,
                    prompt=prompt,
                    raw_export=raw_export,
                    state_dir=state_dir,
                    mount_mode=mount_mode,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    session_attr=session_attr,
                    extra_mounts=extra_mounts,
                    reviewer_id=reviewer_id,
                    events_offset=events_offset,
                )
            except ActorError as exc:
                if exc.kind not in retryable_kinds:
                    raise
                if attempt > max_retries:
                    raise ActorError(
                        f"{exc} (after {max_retries} retries)",
                        kind=exc.kind,
                    )
                append_jsonl(
                    self.paths.guardrail_events,
                    {
                        "event": events.PROVIDER_TRANSIENT_ERROR_RETRY,
                        "role": role,
                        "model": model,
                        "attempt": attempt,
                        "backoff_seconds": backoff,
                        "reason": str(exc),
                    },
                )
                time.sleep(backoff)
                attempt += 1

    def _opencode_turn_attempt(
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
        extra_mounts: list[tuple[Path, str, str]] | None,
        reviewer_id: str | None,
        events_offset: int,
    ) -> ActorOutput:
        pre_text_count = _count_text_events(raw_export)
        session_id = getattr(self, session_attr) if session_attr else None
        cmd, env = self.container.build_argv(
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
        start_event: dict[str, object] = {
            "event": events.ACTOR_START,
            "role": role,
            "mount_mode": mount_mode,
            "model": model,
            "timeout_seconds": timeout_seconds,
            "cmd_redacted": redact_command(cmd),
        }
        if reviewer_id is not None:
            # Tags review-pass starts so the TUI can route per-reviewer
            # turn separators to the right pane. role stays "review" for
            # transcript/breadcrumb back-compat; the field is additive.
            start_event["reviewer_id"] = reviewer_id
        append_jsonl(self.paths.guardrail_events, start_event)
        raw_export.parent.mkdir(parents=True, exist_ok=True)
        cr = self.container.run_detached(
            cmd=cmd,
            env=env,
            stdout_path=raw_export,
            timeout_seconds=timeout_seconds,
            role=role,
            baseline_text_count=pre_text_count,
            state_dir=state_dir,
            stdout_stall_seconds=self.config.opencode_stdout_stall_seconds or None,
            events_offset=events_offset,
        )
        returncode = cr.returncode
        stderr = cr.stderr
        fast_fail_reason = cr.fast_fail_reason
        if fast_fail_reason is not None:
            kind = _classify_fast_fail_marker(fast_fail_reason)
            append_jsonl(
                self.paths.guardrail_events,
                {
                    "event": kind,
                    "role": role,
                    "model": model,
                    "marker": fast_fail_reason,
                },
            )
            if kind == events.PROVIDER_QUOTA_EXHAUSTED:
                raise ActorError(
                    f"{role} opencode aborted early — provider quota exhausted "
                    f"({fast_fail_reason}) on model {model}",
                    kind=kind,
                )
            raise ActorError(
                f"{role} opencode aborted early — transient provider error "
                f"({fast_fail_reason}) on model {model}",
                kind=kind,
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
            error = _latest_error_after_text_count(
                raw_export, pre_text_count, events_offset=events_offset
            )
            if error:
                matched = next(
                    (m for m in _FAST_FAIL_MARKERS if m in error),
                    None,
                )
                if matched is not None:
                    kind = _classify_fast_fail_marker(matched)
                    append_jsonl(
                        self.paths.guardrail_events,
                        {
                            "event": kind,
                            "role": role,
                            "model": model,
                            "marker": matched,
                        },
                    )
                    if kind == events.PROVIDER_QUOTA_EXHAUSTED:
                        raise ActorError(
                            f"{role} opencode emitted free-tier quota error on {model}: "
                            f"{error[:300]}",
                            kind=kind,
                        )
                    raise ActorError(
                        f"{role} opencode emitted transient provider error on {model}: "
                        f"{error[:300]}",
                        kind=kind,
                    )
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
            raise ActorError(f"{role} opencode emitted no text and sqlite recovery found nothing")
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


class CompositeActorRunner:
    """Route agent and SIM turns to DIFFERENT backing runtimes.

    Lets a run mix actors per role — e.g. a codex agent paired with an opencode
    SIM. `agent_turn` goes to the agent runner; `sim_turn` / `sim_review` go to
    the SIM runner. Each sub-runner owns its own state (sessions, containers);
    they only share the role-separated `RunPaths` sinks.
    """

    def __init__(self, *, agent_runner: ActorRunner, sim_runner: ActorRunner):
        self._agent = agent_runner
        self._sim = sim_runner

    def agent_turn(self, message: str) -> ActorOutput:
        return self._agent.agent_turn(message)

    def sim_turn(self, message: str) -> ActorOutput:
        return self._sim.sim_turn(message)

    def sim_review(self, **kwargs) -> ActorOutput:
        return self._sim.sim_review(**kwargs)


def _make_single_runner(
    mode: ActorMode, *, config: RunConfig, paths: RunPaths, tool: str | None = None
) -> ActorRunner:
    if mode == ActorMode.FAKE:
        return FakeActorRunner(
            paths=paths,
            agent_scenario=config.agent_scenario,
            sim_scenario=config.sim_scenario,
        )
    if mode == ActorMode.OPENCODE:
        return OpencodeActorRunner(config=config, paths=paths, container=DockerContainerLifecycle())
    if mode == ActorMode.CLI:
        # Lazy import: cli_actor imports this module for the shared detached
        # runner, so a top-level import here would be a cycle.
        from .cli_actor import CliActorRunner

        return CliActorRunner(
            config=config,
            paths=paths,
            tool=tool or config.cli_tool,
            container=DockerContainerLifecycle(),
        )
    raise ActorError(f"unknown actor mode: {mode}")


def make_actor_runner(*, config: RunConfig, paths: RunPaths) -> ActorRunner:
    agent_mode = config.actor_mode
    sim_mode = config.sim_actor_mode or config.actor_mode
    agent_tool = config.cli_tool
    sim_tool = config.sim_cli_tool or config.cli_tool
    # A single runner only when the SIM matches the agent on BOTH runtime and (for
    # the CLI runtime) tool. Two CLI roles with different tools — codex agent +
    # claude SIM, or the reverse — need a composite of two CliActorRunners (their
    # per-run homes are tool-namespaced, so they never collide).
    same_tool = not (agent_mode == ActorMode.CLI and sim_tool != agent_tool)
    if sim_mode == agent_mode and same_tool:
        return _make_single_runner(agent_mode, config=config, paths=paths, tool=agent_tool)
    return CompositeActorRunner(
        agent_runner=_make_single_runner(agent_mode, config=config, paths=paths, tool=agent_tool),
        sim_runner=_make_single_runner(sim_mode, config=config, paths=paths, tool=sim_tool),
    )








def _count_text_events(path: Path) -> int:
    return sum(1 for event in read_jsonl(path) if event.get("type") == "text")


def _count_jsonl_events(path: Path) -> int:
    """Return the number of newline-delimited records in `path` (0 if absent)."""

    if not path.exists():
        return 0
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _latest_text(path: Path) -> str:
    for event in reversed(read_jsonl(path)):
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    return text
    return ""


def _latest_session_id(path: Path) -> str | None:
    for event in reversed(read_jsonl(path)):
        session = event.get("sessionID") or event.get("session_id")
        if isinstance(session, str):
            return session
    return None


def _latest_error_after_text_count(
    path: Path, baseline_text_count: int, *, events_offset: int = 0
) -> str | None:
    """Return the latest error event that arrived AFTER the Nth text event.

    Multi-turn streams accumulate errors from old turns. When checking
    whether the *current* turn failed, ignore errors from prior turns.
    `events_offset` further narrows the scan to events at or after a
    given index — used by the retry wrapper so attempt N+1 does not pick
    up attempt N's error from earlier in the same turn.
    """

    events = read_jsonl(path)
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
    cutoff_idx = max(cutoff_idx, events_offset)
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
    for event in read_jsonl(path):
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
    (`costs.sum_costs_in_events`) walks event JSON recursively for
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
