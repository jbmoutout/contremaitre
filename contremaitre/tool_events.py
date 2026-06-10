from __future__ import annotations

from datetime import datetime
from typing import Any
from dataclasses import dataclass


@dataclass
class ToolUseEvent:
    tool: str
    input: dict
    state: dict
    timestamp_ms: float | None
    runtime: str
    event_index: int = -1

    @property
    def file_path(self) -> str:
        return (
            self.input.get("filePath")
            or self.input.get("file_path")
            or self.input.get("path")
            or ""
        )

    @property
    def content(self) -> str:
        return self.input.get("content") or ""


_CLAUDE_TOOL_MAP: dict[str, str] = {
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Read": "read",
    "Bash": "bash",
    "Grep": "grep",
    "Glob": "glob",
    "Task": "task",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
}


def _normalise_tool_name(raw_name: str) -> str:
    return _CLAUDE_TOOL_MAP.get(raw_name, raw_name.lower())


def _extract_timestamp_ms(event: dict[str, Any]) -> float | None:
    raw = event.get("timestamp")
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            pass
    ts = event.get("ts")
    if isinstance(ts, int | float):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


def iter_tool_use_events(raw_events: list[dict[str, Any]]) -> list[ToolUseEvent]:
    result: list[ToolUseEvent] = []
    for idx, event in enumerate(raw_events):
        etype = event.get("type")
        if etype == "tool_use":
            part = event.get("part") or {}
            tool = part.get("tool")
            if not tool:
                continue
            state = part.get("state") or {}
            inp = state.get("input") or {}
            result.append(
                ToolUseEvent(
                    tool=tool,
                    input=inp,
                    state=state,
                    timestamp_ms=_extract_timestamp_ms(event),
                    runtime="opencode",
                    event_index=idx,
                )
            )
        elif etype == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                if not name:
                    continue
                tool = _normalise_tool_name(name)
                inp = block.get("input") or {}
                result.append(
                    ToolUseEvent(
                        tool=tool,
                        input=inp,
                        state={},
                        timestamp_ms=_extract_timestamp_ms(event),
                        runtime="claude",
                        event_index=idx,
                    )
                )
    return result
