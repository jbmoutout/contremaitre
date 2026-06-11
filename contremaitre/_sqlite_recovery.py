"""SQLite recovery for opencode silent-stall and step-finish harvesting.

Opencode persists message parts to its SQLite (text + step-finish) but
sometimes does NOT flush the corresponding events to --format=json stdout
before the Docker process exits. These readers recover that data from the DB.

Two failure modes:

  1. Silent stall — opencode exits with no `text` event on stdout. The DB
     has the text; we read it back and the caller synthesises a text event.

  2. Subagent invisibility — subagent (child) sessions spawned by the `task`
     tool log their step-finish parts to the same opencode.db as the parent,
     but their events never flow into the parent invocation's stdout. Each
     parent turn that uses subagents leaves their cost invisible to the
     recorded-cost estimator.

Both readers return data and let the caller own logging policy (guardrail
events, recoveries). Zero dependencies on contremaitre event constants,
RunPaths, or logging machinery.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


def _read_jsonl_lines(path: Path) -> list[dict]:
    """Inline JSONL reader. Zero contremaitre imports."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def recover_text(state_dir: Path, session_id: str | None) -> tuple[str | None, str | None, bool]:
    """Recover the latest message's text from opencode's sqlite.

    Returns: (text, message_id, completed).
    `completed` is True if the message has a step-finish part with
    reason='stop' — i.e. genuinely finished, not mid-stream.
    """

    db_path = state_dir / "opencode.db"
    if not db_path.exists():
        return None, None, False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        cur = conn.cursor()
        sess_id = session_id
        if sess_id is None:
            row = cur.execute(
                "SELECT id FROM session ORDER BY time_created DESC LIMIT 1"
            ).fetchone()
            if not row:
                conn.close()
                return None, None, False
            sess_id = row[0]
        msg_row = cur.execute(
            "SELECT id FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 1",
            (sess_id,),
        ).fetchone()
        if not msg_row:
            conn.close()
            return None, None, False
        msg_id = msg_row[0]
        parts = cur.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY time_created ASC",
            (msg_id,),
        ).fetchall()
        conn.close()
        if not parts:
            return None, msg_id, False
        text_chunks: list[str] = []
        completed = False
        for (data_str,) in parts:
            try:
                part = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            ptype = part.get("type")
            if ptype == "text":
                chunk = part.get("text", "")
                if isinstance(chunk, str):
                    text_chunks.append(chunk)
            elif ptype == "step-finish" and part.get("reason") == "stop":
                completed = True
        if not text_chunks:
            return None, msg_id, completed
        return "".join(text_chunks), msg_id, completed
    except sqlite3.Error:
        return None, None, False


def existing_step_finish_part_ids(path: Path) -> set[str]:
    """Collect `part.id` values for step_finish events already in raw_export.

    Used by `harvest_step_finishes` to dedupe — we never want to re-emit a
    step_finish opencode's stdout already streamed, nor one we synthesised
    on a previous turn.
    """

    ids: set[str] = set()
    for event in _read_jsonl_lines(path):
        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if isinstance(part, dict):
            pid = part.get("id")
            if isinstance(pid, str):
                ids.add(pid)
    return ids


def harvest_step_finishes(state_dir: Path, raw_export: Path) -> int:
    """Append synthetic `step_finish` events for parts opencode never streamed.

    Walks every `part` row in the DB whose JSON has `type == "step-finish"`
    and synthesises a `step_finish` event matching the stdout envelope shape
    for any whose `part.id` isn't already in raw_export. The cost estimator
    (`costs.sum_costs_in_events`) walks event JSON recursively for `cost`-like
    keys, so synthesised parts contribute identically to real ones without any
    estimator change.

    Idempotent: dedupe is by `part.id`, so calling this every turn is safe
    even when child sessions persist across turns.

    Returns: number of synthetic events appended.
    """

    db_path = state_dir / "opencode.db"
    if not db_path.exists():
        return 0
    existing_ids = existing_step_finish_part_ids(raw_export)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, session_id, message_id, data, time_created "
            "FROM part ORDER BY time_created ASC"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return 0
    new_events: list[dict[str, object]] = []
    now_ms = int(time.time() * 1000)
    for part_id, session_id, message_id, data_str, time_created in rows:
        if not isinstance(part_id, str) or part_id in existing_ids:
            continue
        try:
            part_data = json.loads(data_str)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(part_data, dict) or part_data.get("type") != "step-finish":
            continue
        part_envelope = {
            **part_data,
            "id": part_id,
            "messageID": message_id,
            "sessionID": session_id,
        }
        if isinstance(time_created, (int, float)) and time_created > 0:
            ts = int(time_created)
        else:
            ts = now_ms
        new_events.append(
            {
                "type": "step_finish",
                "timestamp": ts,
                "sessionID": session_id,
                "_synthesized_from_sqlite": True,
                "part": part_envelope,
            }
        )
        existing_ids.add(part_id)
    if not new_events:
        return 0
    raw_export.parent.mkdir(parents=True, exist_ok=True)
    with raw_export.open("a", encoding="utf-8") as f:
        for event in new_events:
            f.write(json.dumps(event) + "\n")
    return len(new_events)
