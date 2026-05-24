"""Typed event infrastructure for guardrail and recovery events.

Single source of truth for event kinds, their field shapes, and
serialization. Writers create typed instances; readers deserialise
via `load()`. The enum-like string constants remain as `.kind`
attribute values for backwards compat with TUI substring matching.

Usage:

    # Writer
    emit = events.TurnCap(turns=30)
    append_jsonl(path, dump(emit))

    # Reader
    loaded = [events.load(json.loads(l)) for l in lines if l.strip()]
    for e in loaded:
        if isinstance(e, events.RevisionRequested):
            print(e.required_changes)

Design: events.py#L1-L??  (architecture-review candidate C1 — typed events)
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, asdict
from typing import Any


# ── Registry ──────────────────────────────────────────────────────────
# Maps event class → kind string (the value that appears as "event" or
# "kind" in JSONL rows). Populated at class-definition time by the
# @_reg decorator.

_KIND: dict[type, str] = {}
_BY_KIND: dict[str, type] = {}


def _reg(kind: str):
    """Parameterised decorator that registers a dataclass as an event kind.

    Usage:
        @_reg("revision_requested")
        @dataclass(frozen=True)
        class RevisionRequested:
            round: int
            required_changes: list[str]
    """

    def _wrap(cls):
        cls.kind = kind
        _KIND[cls] = kind
        _BY_KIND[kind] = cls
        return cls

    return _wrap


def MARKER(name: str):
    """Shortcut for zero-field events.  Creates a frozen dataclass.

    Usage:
        Turn = MARKER("turn")
    """
    cls_name = "".join(p.title() for p in name.split("_"))
    ns = {"__annotations__": {}, "__repr__": lambda self: f"<{name}>"}
    cls = type(cls_name, (), ns)
    return _reg(name)(dataclass(frozen=True)(cls))


# ── Guardrail events (guardrail_events.jsonl, routing key "event") ──

Turn = MARKER("turn")


@_reg("progress")
@dataclass(frozen=True)
class Progress:
    label: str
    no_progress_streak: int


@_reg("no_progress")
@dataclass(frozen=True)
class NoProgress:
    label: str
    no_progress_streak: int


@_reg("work_session_end")
@dataclass(frozen=True)
class WorkSessionEnd:
    round: int
    outcome: str


@_reg("opencode_actor_start")
@dataclass(frozen=True)
class OpencodeActorStart:
    role: str
    mount_mode: str
    model: str
    timeout_seconds: int
    cmd_redacted: str


@_reg("subagent_step_finish_harvested")
@dataclass(frozen=True)
class SubagentStepFinishHarvested:
    role: str
    count: int


@_reg("malformed_verdict")
@dataclass(frozen=True)
class MalformedVerdict:
    round: int
    attempt: int
    error: str


@_reg("revision_requested")
@dataclass(frozen=True)
class RevisionRequested:
    round: int
    required_changes: list[str]


@_reg("review_verdict")
@dataclass(frozen=True)
class ReviewVerdict:
    round: int
    verdict: str
    confidence: float
    summary: str
    required_changes: int


@_reg("hard_gates_checked")
@dataclass(frozen=True)
class HardGatesChecked:
    passed: bool
    diff_hash_matched: bool
    diff_scan_passed: bool
    clean_worktree: bool
    changed_files: int


@_reg("turn_cap")
@dataclass(frozen=True)
class TurnCap:
    turns: int


@_reg("wall_cap")
@dataclass(frozen=True)
class WallCap:
    wall_minutes: float


@_reg("recorded_cost_cap")
@dataclass(frozen=True)
class RecordedCostCap:
    recorded_cost_usd: float
    max_cost_usd: float


@_reg("no_progress_cap")
@dataclass(frozen=True)
class NoProgressCap:
    no_progress_streak: int
    no_progress_turns: int


@_reg("check_started")
@dataclass(frozen=True)
class CheckStarted:
    cmd: str
    index: int
    in_container: bool


@_reg("check_completed")
@dataclass(frozen=True)
class CheckCompleted:
    cmd: str
    index: int
    returncode: int | None
    duration_seconds: float
    timed_out: bool
    stdout_head: str | None = None


@_reg("host_commit_created")
@dataclass(frozen=True)
class HostCommitCreated:
    reason: str
    title: str


@_reg("host_commit_skipped")
@dataclass(frozen=True)
class HostCommitSkipped:
    reason: str


SimulatedDiffDrift = MARKER("simulated_diff_drift")

ImplementationCompleteCleared = MARKER("implementation_complete_cleared")


@_reg("publication_blocked")
@dataclass(frozen=True)
class PublicationBlocked:
    reason: str
    hard_gates: dict
    forbidden_files: list


@_reg("published")
@dataclass(frozen=True)
class Published:
    publish_mode: str
    branch: str
    url: str
    dry_run: bool


@_reg("worktree_removed")
@dataclass(frozen=True)
class WorktreeRemoved:
    path: str


@_reg("infra_failure")
@dataclass(frozen=True)
class InfraFailure:
    error: str


GuardrailEvent = (
    Turn
    | Progress
    | NoProgress
    | WorkSessionEnd
    | OpencodeActorStart
    | SubagentStepFinishHarvested
    | MalformedVerdict
    | RevisionRequested
    | ReviewVerdict
    | HardGatesChecked
    | TurnCap
    | WallCap
    | RecordedCostCap
    | NoProgressCap
    | CheckStarted
    | CheckCompleted
    | HostCommitCreated
    | HostCommitSkipped
    | SimulatedDiffDrift
    | ImplementationCompleteCleared
    | PublicationBlocked
    | Published
    | WorktreeRemoved
    | InfraFailure
)

# ── Recovery events (recoveries.jsonl, routing key "kind") ──


@_reg("sqlite_recovery_silent_stall")
@dataclass(frozen=True)
class SqliteRecoverySilentStall:
    role: str
    recovered_chars: int
    message_id: str
    step_finish_completed: bool


@_reg("sigterm_emergency_write")
@dataclass(frozen=True)
class SigtermEmergencyWrite:
    turns: int
    signal: str


@_reg("extract_failed")
@dataclass(frozen=True)
class ExtractFailed:
    error: str


@_reg("viewer_build_failed")
@dataclass(frozen=True)
class ViewerBuildFailed:
    error: str


RecoveryEvent = SqliteRecoverySilentStall | SigtermEmergencyWrite | ExtractFailed | ViewerBuildFailed


# ── Serialization ──────────────────────────────────────────────────────


def dump(event: GuardrailEvent | RecoveryEvent) -> dict[str, Any]:
    """Serialize a typed event to a JSONL row dict (no ts — caller adds it).

    Sets the correct routing key per union branch:
    - GuardrailEvent → ``{"event": "<kind>", ...fields}``
    - RecoveryEvent → ``{"kind": "<kind>", ...fields}``
    """

    d = asdict(event)
    d = {k: v for k, v in d.items() if v is not None}
    if isinstance(event, GuardrailEvent):
        d["event"] = _KIND[type(event)]
    elif isinstance(event, RecoveryEvent):
        d["kind"] = _KIND[type(event)]
    else:
        assert False, f"dump: unexpected event type {type(event)}"
    return d


def load(d: dict[str, Any]) -> GuardrailEvent | RecoveryEvent | None:
    """Deserialise a JSONL row dict to a typed event.

    Unknown routing keys return ``None`` (lenient forward compat).
    Row metadata keys (``ts``, ``event``, ``kind``) are stripped before
    constructor dispatch.
    """

    key = d.get("event") or d.get("kind")
    if not key:
        return None
    cls = _BY_KIND.get(key)
    if cls is None:
        return None
    fields = {k: v for k, v in d.items() if k not in ("ts", "event", "kind")}
    return cls(**fields)


# ── Import-time drift assertion ────────────────────────────────────────


def _assert_no_drift() -> None:
    """Fail at import time if any registered event is missing from its union.

    A developer who adds a new ``@_reg`` dataclass but forgets to add it to
    the ``GuardrailEvent`` or ``RecoveryEvent`` union gets a loud crash on
    the very next ``import contremaitre.events``.
    """

    registered = set(_KIND.values())
    in_union = {getattr(t, "kind", "") for t in typing.get_args(GuardrailEvent)}
    in_union |= {getattr(t, "kind", "") for t in typing.get_args(RecoveryEvent)}
    missing = registered - in_union
    extra = in_union - registered
    if missing:
        raise RuntimeError(
            f"events registered but missing from union: {sorted(missing)}. "
            "Add them to GuardrailEvent or RecoveryEvent."
        )
    if extra:
        raise RuntimeError(
            f"events in union but not registered: {sorted(extra)}. "
            "Remove stale entries."
        )


_assert_no_drift()


