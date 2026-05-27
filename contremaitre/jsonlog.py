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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts.

    Returns `[]` silently on missing/unreadable files. Skips blank lines,
    parses each non-blank line, and filters to dict-typed records. Callers
    navigate with `.get()` as they already do.
    """
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

