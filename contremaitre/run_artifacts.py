"""Interpret JSONL event sequences to extract run-state facts.

This module reads already-loaded event lists and derives facts about the
Contremaitre run artifact contract: markers written, phases elapsed, marker
timestamps, and marker write sizes.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any

from .extract import parse_apply_patch
from .jsonlog import read_jsonl


class Marker(str, Enum):
    ARCHITECTURE_REVIEW = "architecture_review"
    SETTLED_DESIGN = "settled_design"
    IMPLEMENTATION_COMPLETE = "implementation_complete"


_MARKER_PATH_RE: dict[Marker, re.Pattern[str]] = {
    Marker.ARCHITECTURE_REVIEW: re.compile(
        r"(?:^|[/\\])architecture-review\.html?$", re.IGNORECASE
    ),
    Marker.SETTLED_DESIGN: re.compile(r"(?:^|[/\\])SETTLED_DESIGN\.md$", re.IGNORECASE),
    Marker.IMPLEMENTATION_COMPLETE: re.compile(r"(?:^|[/\\])IMPLEMENTATION_COMPLETE$"),
}
_CONTREMAITRE_DIR_RE = re.compile(r"[/\\]?\.contremaitre[/\\]")


def marker_written(events: list[dict[str, Any]], marker: Marker) -> bool:
    return _marker_event(events, marker) is not None


def marker_timestamp_ms(events: list[dict[str, Any]], marker: Marker) -> float | None:
    return _timestamp_ms(_marker_event(events, marker))


def marker_write_chars(events: list[dict[str, Any]], marker: Marker) -> int | None:
    event = _marker_event(events, marker)
    if event is None:
        return None
    return _write_chars(event, _MARKER_PATH_RE[marker])


def marker_tokens_before(events: list[dict[str, Any]], marker: Marker) -> int | None:
    event = _marker_event(events, marker)
    if event is None:
        return None
    total = 0
    for candidate in events:
        if candidate is event:
            break
        if candidate.get("type") == "step_finish":
            total += (candidate.get("part") or {}).get("tokens", {}).get("total", 0)
    return total


def compute_phases(paths: Any, agent_events: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """Disk-reading wrapper for path-based callers."""

    if agent_events is None:
        agent_events = read_jsonl(paths.raw_export)
    guardrails_path = getattr(paths, "guardrail_events", None)
    guardrails = read_jsonl(guardrails_path) if guardrails_path else []
    review_cycles_path = getattr(paths, "review_cycles", None)
    review_cycles = read_jsonl(review_cycles_path) if review_cycles_path else []
    return compute_phases_from_events(agent_events, guardrails, review_cycles)


def compute_phases_from_events(
    agent_events: list[dict[str, Any]],
    guardrails: list[dict[str, Any]],
    review_cycles: list[dict[str, Any]],
) -> dict[str, int]:
    """Split grilling, implementation, and review phases from loaded events."""

    settled_ms = marker_timestamp_ms(agent_events, Marker.SETTLED_DESIGN)
    impl_ms = marker_timestamp_ms(agent_events, Marker.IMPLEMENTATION_COMPLETE)

    starts: list[tuple[float, str]] = []
    for event in guardrails:
        if event.get("event") != "opencode_actor_start":
            continue
        ts = _timestamp_ms(event)
        role = event.get("role")
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
        post = []
    else:
        pre = starts[:impl_start_idx]
        post = starts[impl_start_idx:]

    pre_settled_agent = sum(1 for _, role in pre if role == "agent")
    pre_settled_sim = sum(1 for _, role in pre if role == "sim")
    impl_agent = sum(
        1 for ts, role in post if role == "agent" and (impl_ms is None or ts <= impl_ms)
    )
    review_rounds = (
        max((event.get("round") or 0) for event in review_cycles) if review_cycles else 0
    )

    return {
        "pre_settled_agent_turns": pre_settled_agent,
        "pre_settled_sim_turns": pre_settled_sim,
        "grilling_exchanges": min(pre_settled_agent, pre_settled_sim),
        "impl_turns": impl_agent,
        "review_rounds": review_rounds,
    }


def _timestamp_ms(event: dict[str, Any] | None) -> float | None:
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


def _tool_paths(event: dict[str, Any]) -> list[str]:
    inp = _inp(event)
    tool = _tool_name(event)
    if tool == "apply_patch":
        patch = inp.get("patchText") or inp.get("patch") or ""
        return [fp for _, fp, _ in parse_apply_patch(str(patch))]
    fp = inp.get("filePath") or inp.get("path") or ""
    return [str(fp)] if fp else []


def _marker_event(events: list[dict[str, Any]], marker: Marker) -> dict[str, Any] | None:
    pattern = _MARKER_PATH_RE[marker]
    for event in events:
        if event.get("type") != "tool_use":
            continue
        part = event.get("part") or {}
        if part.get("tool") not in ("write", "edit", "apply_patch"):
            continue
        if (part.get("state") or {}).get("status") != "completed":
            continue
        if any(pattern.search(path) for path in _tool_paths(event)):
            return event
    return None


def _write_chars(event: dict[str, Any], pattern: re.Pattern[str]) -> int:
    inp = _inp(event)
    tool = _tool_name(event)
    if tool == "write":
        return len(inp.get("content") or "")
    if tool == "edit":
        return len(inp.get("newString") or "")
    if tool == "apply_patch":
        patch = inp.get("patchText") or inp.get("patch") or ""
        return sum(len(body) for _, fp, body in parse_apply_patch(str(patch)) if pattern.search(fp))
    return 0


def _tool_name(event: dict[str, Any]) -> str:
    return (event.get("part") or {}).get("tool", "?")


def _inp(event: dict[str, Any]) -> dict[str, Any]:
    return ((event.get("part") or {}).get("state") or {}).get("input") or {}
