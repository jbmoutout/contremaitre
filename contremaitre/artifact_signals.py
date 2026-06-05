"""Agent event interpretation for Contremaitre run artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import events
from .extract import parse_apply_patch


ARCHITECTURE_REVIEW_RE = re.compile(r"architecture-review\.html", re.IGNORECASE)
SETTLED_RE = re.compile(r"SETTLED_DESIGN", re.IGNORECASE)
IMPLEMENTATION_COMPLETE_RE = re.compile(r"IMPLEMENTATION_COMPLETE")
CONTREMAITRE_DIR_RE = re.compile(r"(^|[/\\])\.contremaitre([/\\]|$)")

TEST_CMD_RE = re.compile(
    r"\bunittest\b|\bpytest\b|\btsc\b|npm\s+test|make\s+test|\bmypy\b|\bjest\b|\bvitest\b"
)
RUNTIME_INSTALL_RE = re.compile(r"apt-?get\s+install|pip\s+install\b|npm\s+install\b")
TEST_FAIL_RE = re.compile(r"\bFAILED\b|\berror:\s|\bfailed\b", re.IGNORECASE)
ZERO_TESTS_RE = re.compile(r"0 passed|no tests ran|collected 0 items|Ran 0 tests", re.IGNORECASE)


@dataclass(frozen=True)
class WriteSignal:
    index: int
    timestamp_ms: float | None
    paths: tuple[str, ...]
    chars: int = 0


@dataclass(frozen=True)
class ArtifactWrites:
    architecture_review: WriteSignal | None
    settled_design: WriteSignal | None
    implementation_complete: WriteSignal | None
    first_code_edit: WriteSignal | None


@dataclass(frozen=True)
class PhaseCounts:
    grilling_exchanges: int
    impl_turns: int
    review_rounds: int


@dataclass(frozen=True)
class SelfVerification:
    self_verified: bool
    output_suggests_pass: bool | None
    runtime_install_required: bool


def detect_artifact_writes(agent_events: list[dict[str, Any]]) -> ArtifactWrites:
    return ArtifactWrites(
        architecture_review=_find_write_to(agent_events, ARCHITECTURE_REVIEW_RE),
        settled_design=_find_write_to(agent_events, SETTLED_RE),
        implementation_complete=_find_write_to(agent_events, IMPLEMENTATION_COMPLETE_RE),
        first_code_edit=_find_first_code_edit(agent_events),
    )


def compute_phase_counts(
    agent_events: list[dict[str, Any]],
    guardrails: list[dict[str, Any]],
    review_cycles: list[dict[str, Any]],
) -> PhaseCounts:
    writes = detect_artifact_writes(agent_events)
    settled_ms = writes.settled_design.timestamp_ms if writes.settled_design else None
    impl_ms = (
        writes.implementation_complete.timestamp_ms if writes.implementation_complete else None
    )

    starts: list[tuple[float, str]] = []
    for guardrail in guardrails:
        if guardrail.get("event") != events.OPENCODE_ACTOR_START:
            continue
        ts = timestamp_ms(guardrail)
        role = guardrail.get("role")
        if ts is None or role not in ("agent", "sim", "review"):
            continue
        starts.append((ts, role))
    starts.sort()

    impl_start_idx: int | None = None
    if settled_ms is not None:
        for i, (ts, role) in enumerate(starts):
            if role != "agent":
                continue
            next_ts = starts[i + 1][0] if i + 1 < len(starts) else float("inf")
            if ts <= settled_ms < next_ts:
                impl_start_idx = i
                break

    if impl_start_idx is None:
        pre = starts
        post: list[tuple[float, str]] = []
    else:
        pre = starts[:impl_start_idx]
        post = starts[impl_start_idx:]

    pre_agent = sum(1 for _, role in pre if role == "agent")
    pre_sim = sum(1 for _, role in pre if role == "sim")
    impl_turns = sum(
        1 for ts, role in post if role == "agent" and (impl_ms is None or ts <= impl_ms)
    )
    review_rounds = (
        max((entry.get("round") or 0) for entry in review_cycles) if review_cycles else 0
    )

    return PhaseCounts(
        grilling_exchanges=min(pre_agent, pre_sim),
        impl_turns=impl_turns,
        review_rounds=review_rounds,
    )


def compute_self_verification(agent_events: list[dict[str, Any]]) -> SelfVerification:
    writes = detect_artifact_writes(agent_events)
    impl_ts = (
        writes.implementation_complete.timestamp_ms
        if writes.implementation_complete
        and writes.implementation_complete.timestamp_ms is not None
        else float("inf")
    )

    last_edit_ts = writes.first_code_edit.timestamp_ms if writes.first_code_edit else None
    test_outputs: list[str] = []
    runtime_install = False

    for event in agent_events:
        if _tool_name(event) != "bash":
            continue
        cmd = _tool_input(event).get("command") or ""
        if RUNTIME_INSTALL_RE.search(cmd):
            runtime_install = True
        event_ts = timestamp_ms(event)
        if event_ts is None:
            continue
        if (
            TEST_CMD_RE.search(cmd)
            and last_edit_ts is not None
            and last_edit_ts < event_ts < impl_ts
        ):
            output = ((event.get("part") or {}).get("state") or {}).get("output") or ""
            test_outputs.append(output)

    if not test_outputs:
        return SelfVerification(False, None, runtime_install)

    output_suggests_pass = all(
        not TEST_FAIL_RE.search(output) and not ZERO_TESTS_RE.search(output)
        for output in test_outputs
    )
    return SelfVerification(True, output_suggests_pass, runtime_install)


def tool_paths(event: dict[str, Any]) -> tuple[str, ...]:
    inp = _tool_input(event)
    tool = _tool_name(event)
    if tool == "apply_patch":
        patch = inp.get("patchText") or inp.get("patch") or ""
        return tuple(path for _, path, _ in parse_apply_patch(str(patch)))
    path = inp.get("filePath") or inp.get("path") or ""
    return (str(path),) if path else ()


def timestamp_ms(event: dict[str, Any] | None) -> float | None:
    if not event:
        return None
    raw = event.get("timestamp")
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            pass
    ts = event.get("ts")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


def tokens_before(events_list: list[dict[str, Any]], signal: WriteSignal | None) -> int | None:
    if signal is None:
        return None
    total = 0
    for event in events_list[: signal.index]:
        if event.get("type") == "step_finish":
            total += ((event.get("part") or {}).get("tokens") or {}).get("total", 0)
    return total


def _find_write_to(
    events_list: list[dict[str, Any]], pattern: re.Pattern[str]
) -> WriteSignal | None:
    for index, event in enumerate(events_list):
        signal = _write_signal(index, event, pattern)
        if signal is not None:
            return signal
    return None


def _find_first_code_edit(events_list: list[dict[str, Any]]) -> WriteSignal | None:
    for index, event in enumerate(events_list):
        if not _completed_write_event(event):
            continue
        paths = tool_paths(event)
        if any(not CONTREMAITRE_DIR_RE.search(path) for path in paths):
            return WriteSignal(index=index, timestamp_ms=timestamp_ms(event), paths=paths)
    return None


def _write_signal(
    index: int, event: dict[str, Any], pattern: re.Pattern[str]
) -> WriteSignal | None:
    if not _completed_write_event(event):
        return None
    paths = tool_paths(event)
    if not any(pattern.search(path) for path in paths):
        return None
    return WriteSignal(
        index=index,
        timestamp_ms=timestamp_ms(event),
        paths=paths,
        chars=_write_chars(event, pattern),
    )


def _completed_write_event(event: dict[str, Any]) -> bool:
    if event.get("type") != "tool_use":
        return False
    part = event.get("part") or {}
    if part.get("tool") not in ("write", "edit", "apply_patch"):
        return False
    return ((part.get("state") or {}).get("status")) == "completed"


def _tool_name(event: dict[str, Any]) -> str:
    return (event.get("part") or {}).get("tool", "?")


def _tool_input(event: dict[str, Any]) -> dict[str, Any]:
    return ((event.get("part") or {}).get("state") or {}).get("input") or {}


def _write_chars(event: dict[str, Any], pattern: re.Pattern[str]) -> int:
    inp = _tool_input(event)
    tool = _tool_name(event)
    if tool == "write":
        return len(inp.get("content") or "")
    if tool == "edit":
        return len(inp.get("newString") or "")
    if tool == "apply_patch":
        patch = inp.get("patchText") or inp.get("patch") or ""
        return sum(
            len(body) for _, path, body in parse_apply_patch(str(patch)) if pattern.search(path)
        )
    return 0
