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

    @property
    def old_string(self) -> str:
        return self.input.get("oldString") or self.input.get("old_string") or ""

    @property
    def new_string(self) -> str:
        return self.input.get("newString") or self.input.get("new_string") or ""


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


def extract_timestamp_ms(event: dict[str, Any]) -> float | None:
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
                    timestamp_ms=extract_timestamp_ms(event),
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
                inp = block.get("input") or {}
                ts = extract_timestamp_ms(event)
                if name == "MultiEdit":
                    for edit in inp.get("edits") or []:
                        if not isinstance(edit, dict):
                            continue
                        fp = edit.get("file_path") or edit.get("path") or ""
                        if not fp:
                            continue
                        outer = {
                            "file_path": fp,
                            "old_string": edit.get("old_string", ""),
                            "new_string": edit.get("new_string", ""),
                        }
                        result.append(
                            ToolUseEvent(
                                tool="edit",
                                input=outer,
                                state={},
                                timestamp_ms=ts,
                                runtime="claude",
                                event_index=idx,
                            )
                        )
                else:
                    tool = _normalise_tool_name(name)
                    result.append(
                        ToolUseEvent(
                            tool=tool,
                            input=inp,
                            state={},
                            timestamp_ms=ts,
                            runtime="claude",
                            event_index=idx,
                        )
                    )
    return result
