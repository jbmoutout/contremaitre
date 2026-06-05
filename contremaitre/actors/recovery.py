from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..jsonlog import append_jsonl, read_jsonl
from ..models import RunPaths


def _record_recovery(paths: RunPaths, *, kind: str, **fields) -> None:
    """Append a recovery event to both recoveries.jsonl and guardrail_events.

    `recoveries.jsonl` is the forensic capture for sqlite recoveries +
    SIGTERM emergency writes — events that aren't normal control-plane
    flow but matter for post-mortem analysis. Mirrored in guardrail_events
    so a single tail catches them too.
    """

    record = {"kind": kind, **fields}
    append_jsonl(paths.recoveries, record)
    append_jsonl(paths.guardrail_events, {"event": f"recovery_{kind}", **fields})


def _recover_text_from_sqlite(
    state_dir: Path, session_id: str | None
) -> tuple[str | None, str | None, bool]:
    """Recover the latest message's text from opencode's sqlite.

    Failure mode this addresses: opencode persists message parts to its
    SQLite (text + step-finish with reason='stop') but sometimes does NOT
    flush the corresponding `text` event to its --format=json stdout
    before the docker process exits. The data is intact in the DB; we
    read it back.

    Returns: (text, message_id, completed). `completed` is True if the
    message has a step-finish part with reason='stop' — i.e. genuinely
    finished, not mid-stream.
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


def _append_synthetic_text_event(
    raw_export: Path, text: str, message_id: str | None, session_id: str | None
) -> None:
    """Append a synthetic text event so downstream tooling sees a uniform stream."""

    event = {
        "type": "text",
        "timestamp": int(time.time() * 1000),
        "sessionID": session_id,
        "_recovered_from_sqlite": True,
        "_message_id": message_id,
        "part": {"text": text},
    }
    raw_export.parent.mkdir(parents=True, exist_ok=True)
    with raw_export.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _existing_step_finish_part_ids(path: Path) -> set[str]:
    """Collect `part.id` values for step_finish events already in raw_export.

    Used by `_harvest_step_finishes_from_sqlite` to dedupe — we never want
    to re-emit a step_finish opencode's stdout already streamed, nor one we
    synthesized on a previous turn.
    """

    ids: set[str] = set()
    for event in read_jsonl(path):
        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if isinstance(part, dict):
            pid = part.get("id")
            if isinstance(pid, str):
                ids.add(pid)
    return ids


def _harvest_step_finishes_from_sqlite(state_dir: Path, raw_export: Path) -> int:
    """Append synthetic `step_finish` events for parts opencode never streamed.

    Two undercounts this addresses:

    (a) Subagent (child) sessions spawned via the `task` tool log their
        step-finish parts to the same opencode.db as the parent, but their
        events never flow into the parent invocation's --format=json stdout.
        Each parent turn that uses subagents leaves their cost invisible to
        the recorded-cost estimator.

    (b) Even for the parent session, the final step-finish part sometimes
        lands in the DB after docker exits without flushing to stdout.

    We walk every `part` row in the DB whose JSON has `type == "step-finish"`
    and synthesize a `step_finish` event matching the stdout envelope shape
    for any whose `part.id` isn't already in raw_export. The cost estimator
    (`costs.estimate_recorded_cost_usd`) walks event JSON recursively for
    `cost`-like keys, so synthesized parts contribute identically to real
    ones without any estimator change.

    Idempotent: dedupe is by `part.id`, so calling this every turn is safe
    even when child sessions persist across turns.

    Returns: number of synthetic events appended.
    """

    db_path = state_dir / "opencode.db"
    if not db_path.exists():
        return 0
    existing_ids = _existing_step_finish_part_ids(raw_export)
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
        # The `id` lives in the table column, not the JSON blob — opencode
        # only injects it into the envelope when streaming to stdout. The
        # `message_id` is also in a column. The JSON has the type / cost /
        # tokens / reason fields; we reassemble the envelope shape that
        # stdout emits.
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
        # opencode stores `time_created` as milliseconds since epoch; fall
        # back to wall clock if the column is missing or malformed.
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
