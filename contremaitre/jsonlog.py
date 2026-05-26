"""Small JSONL and transcript helpers used by the orchestrator.

The log files are intended for both humans and later agents. Records therefore
use explicit names, ISO-like timestamps, and stable top-level fields.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    enriched = {"ts": utc_ts(), **record}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(enriched, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_text_event(path: Path, *, role: str, phase: str, text: str) -> None:
    append_jsonl(
        path,
        {
            "type": "text",
            "role": role,
            "phase": phase,
            "part": {"text": text},
        },
    )


def append_transcript(path: Path, *, speaker: str, phase: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## {phase} - {speaker}\n\n{text.strip()}\n")


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def count_text_events(path: Path) -> int:
    return sum(1 for event in read_events(path) if event.get("type") == "text")


def latest_text(path: Path) -> str:
    for event in reversed(read_events(path)):
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    return text
    return ""


def latest_session_id(path: Path) -> str | None:
    for event in reversed(read_events(path)):
        session = event.get("sessionID") or event.get("session_id")
        if isinstance(session, str):
            return session
    return None


def latest_error_after_text_count(path: Path, baseline_text_count: int) -> str | None:
    events = read_events(path)
    seen_text = 0
    cutoff_idx = 0
    for i, event in enumerate(events):
        if event.get("type") == "text":
            seen_text += 1
            if seen_text > baseline_text_count:
                cutoff_idx = i
                break
    else:
        cutoff_idx = 0
        seen_text = 0
        for i, event in enumerate(events):
            if event.get("type") == "text":
                seen_text += 1
            if seen_text >= baseline_text_count:
                cutoff_idx = i + 1
                break
    for event in reversed(events[cutoff_idx:]):
        if event.get("type") == "error":
            return json.dumps(event, sort_keys=True)
    return None

