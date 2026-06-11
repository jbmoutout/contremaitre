"""Small JSONL and transcript helpers used by the orchestrator.

The log files are intended for both humans and later agents. Records therefore
use explicit names, ISO-like timestamps, and stable top-level fields.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ts_to_ms(value: object) -> int | None:
    """Coerce a single timestamp value to epoch-ms, or None.

    Accepts an epoch-ms int/float (opencode + our codex back-fill), a numeric
    string, or an ISO-8601 string (claude stamps e.g. `2026-06-06T12:49:25.658Z`).
    Anything unparseable → None. The one coercer shared by the TUI, the viewer,
    and flow_use (each previously kept its own copy).
    """

    if isinstance(value, bool):  # bool is an int subclass; never a timestamp
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.lstrip("-").isdigit():
            return int(s)
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def event_ms(event: dict | None) -> int | None:
    """Epoch-ms for an event record, or None.

    Prefers the `timestamp` field, falling back to `ts` (claude stream-json and
    our own `append_jsonl` stamp records there). Both go through `ts_to_ms`.
    """

    if not event:
        return None
    for key in ("timestamp", "ts"):
        if key in event:
            ms = ts_to_ms(event.get(key))
            if ms is not None:
                return ms
    return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Return all JSON objects in `path`. Returns `[]` on missing/unreadable."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


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
