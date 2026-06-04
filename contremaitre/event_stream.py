"""event_stream.py — Shared raw-event reader for opencode JSONL streams.

Hides all knowledge of the opencode JSONL event schema
(``part.state.input.command``, ``event["timestamp"]``, etc.) behind typed
dataclasses. Both ``flow_use.py`` and ``extract.py`` consume this instead of
navigating raw dicts.

Usage::

    events = parse_events(paths.raw_export)
    for tc in events.tool_calls:
        ... tc.file_path  # not input.get("filePath") ...

    guardrails = parse_guardrail_events(paths.guardrail_events)
    for g in guardrails:
        ... g.role ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .jsonlog import read_jsonl


# ---------------------------------------------------------------------------
# Typed records  (stable interface for metric and extraction consumers)
# ---------------------------------------------------------------------------


@dataclass
class ParsedToolCall:
    """A single ``tool_use`` event from the opencode JSONL stream.

    Only non-null fields are set; the caller checks for ``None`` when a field
    may not apply to the tool type (e.g. ``command`` is set only for ``bash``
    calls). This avoids a deep type hierarchy while keeping the interface
    stable against raw schema changes.
    """

    ts: float
    tool: str
    status: str | None = None
    output: str = ""

    file_path: str | None = None
    command: str | None = None
    content: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    patch_text: str | None = None
    pattern: str | None = None
    include: str | None = None
    glob: str | None = None
    limit: int | None = None

    description: str | None = None
    prompt: str | None = None
    subagent_type: str | None = None


@dataclass
class ParsedStepFinish:
    ts: float
    tokens: int = 0


@dataclass
class ParsedTextEvent:
    ts: float
    text: str = ""


@dataclass
class ParsedGuardrailEvent:
    ts: float
    event: str
    role: str | None = None


@dataclass
class ParsedEvents:
    tool_calls: list[ParsedToolCall] = field(default_factory=list)
    step_finishes: list[ParsedStepFinish] = field(default_factory=list)
    text_events: list[ParsedTextEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers (private — the opendcode JSONL schema lives here)
# ---------------------------------------------------------------------------


def _input(event: dict) -> dict:
    return ((event.get("part") or {}).get("state") or {}).get("input") or {}


def _part(event: dict) -> dict:
    return event.get("part") or {}


def _state(event: dict) -> dict:
    return (_part(event)).get("state") or {}


def _timestamp_ms(event: dict) -> float | None:
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


def _parse_tool_call(event: dict) -> ParsedToolCall | None:
    inp = _input(event)
    p = _part(event)
    s = _state(event)
    ts = _timestamp_ms(event)
    if ts is None:
        return None

    return ParsedToolCall(
        ts=ts,
        tool=p.get("tool") or "?",
        status=s.get("status"),
        output=s.get("output") or "",
        file_path=inp.get("filePath") or inp.get("path") or None,
        command=inp.get("command") or None,
        content=inp.get("content") or None,
        old_string=inp.get("oldString") or None,
        new_string=inp.get("newString") or None,
        patch_text=inp.get("patchText") or inp.get("patch") or None,
        pattern=inp.get("pattern") or None,
        include=inp.get("include") or None,
        glob=inp.get("glob") or None,
        limit=_parse_limit(inp),
        description=inp.get("description") or None,
        prompt=inp.get("prompt") or None,
        subagent_type=inp.get("subagent_type") or None,
    )


def _parse_limit(inp: dict) -> int | None:
    raw = inp.get("limit")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_events(jsonl_path: str | Path) -> ParsedEvents:
    """Read *one* JSONL file and parse into typed records.

    Hides all knowledge of the opencode JSONL event schema from callers.
    """
    return _parse_event_list(read_jsonl(jsonl_path))


def _parse_event_list(events: list[dict]) -> ParsedEvents:
    tool_calls: list[ParsedToolCall] = []
    step_finishes: list[ParsedStepFinish] = []
    text_events: list[ParsedTextEvent] = []

    for event in events:
        event_type = event.get("type")
        if event_type == "tool_use":
            tc = _parse_tool_call(event)
            if tc is not None:
                tool_calls.append(tc)
        elif event_type == "step_finish":
            ts = _timestamp_ms(event)
            if ts is not None:
                tokens = (event.get("part") or {}).get("tokens", {}).get("total", 0)
                step_finishes.append(ParsedStepFinish(ts=ts, tokens=tokens))
        elif event_type == "text":
            ts = _timestamp_ms(event)
            if ts is not None:
                text = (event.get("part") or {}).get("text") or ""
                text_events.append(ParsedTextEvent(ts=ts, text=text))

    return ParsedEvents(
        tool_calls=tool_calls,
        step_finishes=step_finishes,
        text_events=text_events,
    )


def parse_guardrail_events(jsonl_path: str | Path) -> list[ParsedGuardrailEvent]:
    """Read a guardrail_events.jsonl file and return typed records."""
    events = read_jsonl(jsonl_path)
    result: list[ParsedGuardrailEvent] = []
    for event in events:
        ts = _timestamp_ms(event)
        if ts is None:
            continue
        result.append(
            ParsedGuardrailEvent(
                ts=ts,
                event=event.get("event") or "",
                role=event.get("role") or None,
            )
        )
    return result
