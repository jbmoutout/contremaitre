from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolUseEvent:
    tool: str
    input: dict
    state: dict
    timestamp_ms: float | None
    runtime: str

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
    mapped = _CLAUDE_TOOL_MAP.get(raw_name)
    if mapped is not None:
        return mapped
    return raw_name[0].lower() + raw_name[1:] if raw_name else raw_name


def iter_tool_use_events(raw_events: list[dict[str, Any]]) -> list[ToolUseEvent]:
    result: list[ToolUseEvent] = []
    for event in raw_events:
        etype = event.get("type")
        if etype == "tool_use":
            part = event.get("part") or {}
            tool = part.get("tool")
            if not tool:
                continue
            state = part.get("state") or {}
            inp = state.get("input") or {}
            ts = event.get("timestamp")
            result.append(
                ToolUseEvent(
                    tool=tool,
                    input=inp,
                    state=state,
                    timestamp_ms=float(ts) if isinstance(ts, (int, float)) else None,
                    runtime="opencode",
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
                ts = event.get("ts")
                result.append(
                    ToolUseEvent(
                        tool=tool,
                        input=inp,
                        state={},
                        timestamp_ms=float(ts) if isinstance(ts, (int, float)) else None,
                        runtime="claude",
                    )
                )
    return result
