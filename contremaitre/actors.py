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

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import events
from .jsonlog import (
    append_jsonl,
    append_text_event,
    append_transcript,
    count_text_events,
    latest_error_after_text_count,
    latest_session_id,
    latest_text,
)
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


# Import extracted modules after ActorError is defined so container.py
# (which imports ActorError from this module) can resolve it at load time.
from .container import QUOTA_ERROR_MARKERS, build_docker_command, redact_command, run_container
from .recovery import (
    append_synthetic_text_event,
    harvest_step_finishes_from_sqlite,
    record_recovery,
    recover_text_from_sqlite,
)


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
        reviewer_id: str = "sim",
        model_override: str | None = None,
    ) -> ActorOutput:
        export = (
            self.paths.extra_reviewer_raw_export
            if reviewer_id == "extra"
            else self.paths.sim_raw_export
        )
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
            raw_export=export,
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
        reviewer_id: str = "sim",
        model_override: str | None = None,
    ) -> ActorOutput:
        from . import prompts

        review_dir = self.paths.run_dir / "review_input"
        review_dir.mkdir(exist_ok=True)
        (review_dir / "SETTLED_DESIGN.md").write_text(
            settled_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (review_dir / "diff.patch").write_text(diff_file.read_text(encoding="utf-8"), encoding="utf-8")
        # Fresh session every review attempt so the SIM has clean context.
        # Separate state dirs per reviewer so SIM and extra reviewer can't
        # collide on opencode.db when called back-to-back in one round.
        attempt_state = self.review_state / f"{reviewer_id}-attempt-{attempt}"
        attempt_state.mkdir(parents=True, exist_ok=True)
        raw_export = (
            self.paths.extra_reviewer_raw_export
            if reviewer_id == "extra"
            else self.paths.sim_raw_export
        )
        return self._opencode_turn(
            role="review",
            prompt=prompts.SIM_REVIEW_PROMPT,
            raw_export=raw_export,
            state_dir=attempt_state,
            mount_mode="ro",
            model=model_override or self.config.sim_model,
            timeout_seconds=self.config.sim_timeout_seconds,
            session_attr=None,
            extra_mounts=[(review_dir, "/review", "ro")],
            reviewer_id=reviewer_id,
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
        pre_text_count = count_text_events(raw_export)
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
        start_event: dict[str, object] = {
            "event": events.OPENCODE_ACTOR_START,
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
        returncode, stderr, fast_fail_reason = run_container(
            cmd=cmd,
            env=env,
            stdout_path=raw_export,
            timeout_seconds=timeout_seconds,
            role=role,
            baseline_text_count=pre_text_count,
        )
        if fast_fail_reason is not None:
            append_jsonl(
                self.paths.guardrail_events,
                {
                    "event": events.PROVIDER_QUOTA_EXHAUSTED,
                    "role": role,
                    "model": model,
                    "marker": fast_fail_reason,
                },
            )
            raise ActorError(
                f"{role} opencode aborted early — provider quota exhausted "
                f"({fast_fail_reason}) on model {model}",
                kind=events.PROVIDER_QUOTA_EXHAUSTED,
            )
        if returncode != 0:
            raise ActorError(f"{role} opencode exited {returncode}: {stderr[:500]}")
        new_session_id = latest_session_id(raw_export)
        if new_session_id and session_attr:
            setattr(self, session_attr, new_session_id)
        post_text_count = count_text_events(raw_export)
        if post_text_count == pre_text_count:
            # Provider/API error scoped to events emitted DURING this turn
            # (after pre_text_count). Avoids tripping on stale errors from
            # earlier turns left in the multi-turn stream.
            error = latest_error_after_text_count(raw_export, pre_text_count)
            if error:
                if any(marker in error for marker in QUOTA_ERROR_MARKERS):
                    append_jsonl(
                        self.paths.guardrail_events,
                        {
                            "event": events.PROVIDER_QUOTA_EXHAUSTED,
                            "role": role,
                            "model": model,
                            "marker": "FreeUsageLimitError",
                        },
                    )
                    raise ActorError(
                        f"{role} opencode emitted free-tier quota error on {model}: "
                        f"{error[:300]}",
                        kind=events.PROVIDER_QUOTA_EXHAUSTED,
                    )
                raise ActorError(f"{role} opencode emitted error without text: {error[:500]}")
            # opencode silent-stall: the text part landed in opencode's sqlite
            # but the corresponding `text` event was never flushed to stdout
            # before docker exited. Read it back from the DB and synthesize
            # the missing event so downstream tooling sees a uniform stream.
            recovered, msg_id, completed = recover_text_from_sqlite(
                state_dir, session_id or new_session_id
            )
            if recovered:
                append_synthetic_text_event(
                    raw_export, recovered, msg_id, session_id or new_session_id
                )
                record_recovery(
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
        latest = latest_text(raw_export)
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

        count = harvest_step_finishes_from_sqlite(state_dir, raw_export)
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
