from __future__ import annotations

import time
from pathlib import Path

from .. import events
from ..jsonlog import append_jsonl, append_transcript
from ..models import ActorMode, RunConfig, RunPaths
from .base import ActorError, ActorOutput, ActorRunner
from .docker import (
    _FAST_FAIL_MARKERS,
    build_docker_command,
    _classify_fast_fail_marker,
    _count_jsonl_events,
    _count_text_events,
    _latest_error_after_text_count,
    _latest_session_id,
    _latest_text,
    _run_detached_container,
    redact_command,
)
from .fake import FakeActorRunner
from .recovery import (
    _append_synthetic_text_event,
    _harvest_step_finishes_from_sqlite,
    _record_recovery,
    _recover_text_from_sqlite,
)


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
        from .. import prompts

        review_dir = self.paths.run_dir / "review_input"
        review_dir.mkdir(exist_ok=True)
        (review_dir / "SETTLED_DESIGN.md").write_text(
            settled_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (review_dir / "diff.patch").write_text(
            diff_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
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
        returncode, stderr, fast_fail_reason = _run_detached_container(
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
