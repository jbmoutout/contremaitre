"""Single-file HTML viewer for a finished contremaitre run.

Reads the artifacts the orchestrator + extractor already produce
(`stats.json`, `raw_export.jsonl`, `sim_raw_export.jsonl`, `transcript.md`,
`subagents/*.md`, `extracted_files/*`, `pr.json`, `eval/*`) and renders a
self-contained `viewer.html` next to them. No external assets, no network
at view time.

Called from the orchestrator's `finally` so the viewer lands even on
infra failures, and from the `viewer` CLI subcommand for back-filling
old runs. Failures are swallowed and recorded in `recoveries.jsonl` —
the viewer is observability, not load-bearing.

The CSS lives in `_styles.css` (verbatim copy of the project's house
viewer style); the renderer JS lives in `_renderer.js`. Keeping them out
of the Python source keeps the rendering surface auditable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..jsonlog import ts_to_ms
from ..models import ModelSpec, RunPaths
from ..pr_outcomes import outcome_for_run
from ..run_artifacts import RunArtifacts

_HERE = Path(__file__).resolve().parent
_CSS_PATH = _HERE / "_styles.css"
_RENDERER_PATH = _HERE / "_renderer.js"

VIEWER_FILENAME = "viewer.html"


def build_viewer(paths: RunPaths, *, refresh_index: bool = True) -> Path:
    """Assemble DATA from run artifacts and write `<run_dir>/viewer.html`.

    Returns the written path. Raises if `stats.json` is missing — every
    finished run writes one in the orchestrator's `_write_final_stats`,
    so its absence means the run never reached a terminal state.

    Side effect: also refreshes `<runs_root>/index.html` so the index
    auto-updates whenever a run finalizes. Index failures are swallowed
    — the per-run viewer is the load-bearing artifact; the index is
    observability one level up.
    """

    data = _assemble_data(paths)
    html = _render_html(data, run_id=paths.run_id)
    out = paths.run_dir / VIEWER_FILENAME
    out.write_text(html, encoding="utf-8")

    # Lazy import — index.py imports VIEWER_FILENAME from this module, so a
    # top-level import here would be circular.
    try:
        if not refresh_index:
            return out

        from .index import build_index

        build_index(paths.run_dir.parent)
    except Exception:
        pass

    return out


# ----- DATA assembly -----


def _assemble_data(paths: RunPaths) -> dict[str, Any]:
    stats_raw = _read_json(paths.stats, default={})
    # Normalize every actor stream to opencode's event vocabulary up front so
    # codex agent/SIM/reviewer turns are visible to all consumers below. Then
    # back-fill synthetic timestamps on the clockless (codex) streams so their
    # turns interleave with timestamped opencode turns instead of piling at t=0.
    arts = RunArtifacts(paths)
    agent_events = _normalize_events(arts.raw_export())
    sim_events = _normalize_events(arts.sim_raw_export())
    _assign_synthetic_timestamps([agent_events, sim_events])

    agent_summary = _summarize_events(agent_events)
    sim_summary = _summarize_events(sim_events)

    timeline = _build_timeline(agent_events, "agent") + _build_timeline(sim_events, "sim")
    timeline.sort(key=lambda e: e.get("timestamp") or 0)

    chat = _build_chat(agent_events, sim_events)

    transcript = _parse_transcript(paths.transcript)

    sub_agents = _read_dir(paths.subagents_dir, suffix=".md")
    extracted_files = _read_dir(paths.extracted_files_dir)

    pr = _read_json(paths.pr_json, default=None)
    pr_outcome = outcome_for_run(paths.run_dir)

    eval_blob: dict[str, Any] = {}
    for src in (
        paths.cost_report,
        paths.preflight_report,
        paths.checks_report,
        paths.pr_eval,
        paths.settled_diff_report,
        paths.architecture_delta_report,
        paths.trajectory_report,
    ):
        loaded = _read_json(src, default=None)
        if loaded is not None:
            eval_blob[src.stem] = loaded

    guardrails = arts.guardrail_events()
    recoveries = arts.recoveries()
    cli_review = _assemble_cli_review(paths, guardrails)
    cli_review_extras = _assemble_cli_review_extras(paths)
    diffstat = arts.diffstat()

    stats = {
        **stats_raw,
        # Persisted model identity is a ModelSpec dict; the per-run renderer JS
        # must never see the raw record. This Python reader routes it through
        # ModelSpec and embeds the derived display string, so `_renderer.js`
        # consumes a string, never an object.
        "agent_model": ModelSpec.from_record(stats_raw.get("agent_model")).display(),
        "sim_model": ModelSpec.from_record(stats_raw.get("sim_model")).display(),
        "cost_usd": stats_raw.get("recorded_cost_usd"),
        "n_events": agent_summary["n_events"] + sim_summary["n_events"],
        "n_tool_uses": agent_summary["n_tool_uses"] + sim_summary["n_tool_uses"],
        "n_text_events": agent_summary["n_text_events"] + sim_summary["n_text_events"],
        "n_step_finishes": agent_summary["n_step_finishes"] + sim_summary["n_step_finishes"],
        "tool_counts": _merge_counts(agent_summary["tool_counts"], sim_summary["tool_counts"]),
        "tokens_in": agent_summary["tokens_in"] + sim_summary["tokens_in"],
        "tokens_out": agent_summary["tokens_out"] + sim_summary["tokens_out"],
        "agent_tool_counts": agent_summary["tool_counts"],
        "sim_tool_counts": sim_summary["tool_counts"],
        "files_written_count": len(extracted_files),
        "subagent_count": len(sub_agents),
        # Net diff over base — files touched + LoC. None fields when the run
        # produced no diff; the renderer omits the strip item in that case.
        "files_changed": (diffstat or {}).get("files"),
        "lines_added": (diffstat or {}).get("insertions"),
        "lines_removed": (diffstat or {}).get("deletions"),
    }

    return {
        "run_id": paths.run_id,
        "parent_label": "contremaitre",
        "rep_label": paths.run_id,
        "stats": stats,
        "initial_prompt": _read_text(paths.initial_prompt),
        "transcript": transcript,
        "chat": chat,
        "timeline": timeline,
        "sub_agents": sub_agents,
        "extracted_files": extracted_files,
        "pr": pr,
        "pr_outcome": pr_outcome,
        "eval": eval_blob or None,
        "guardrails": guardrails,
        "recoveries": recoveries,
        "cli_review": cli_review,
        "cli_review_extras": cli_review_extras,
    }


def _assemble_cli_review(
    paths: RunPaths,
    guardrails: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Collect the post-publish CLI-review artifacts into one viewer blob.

    Returns `None` when this run didn't enable a CLI reviewer (no
    `cli_review_started` event ever fired). Otherwise returns:

      {
        "tool":       "codex" | "claude",
        "status":     "completed" | "failed",
        "verdict":    "MUST_FIX" | "NEEDS_ATTENTION" | "LOOKS_GOOD" | None,
        "duration_s": float | None,                # cli_review_started → completed/failed
        "model":      str | None,                  # codex preamble only
        "url":        str | None,                  # PR URL the comment was posted to
        "markdown":   str | "",                    # body the orchestrator posted
        "fail_reason": str | None,                 # only when status == "failed"
      }

    Reads only what already exists on disk — no events emitted, no
    side effects.
    """

    started_evt = next(
        (g for g in reversed(guardrails) if g.get("event") == "cli_review_started"),
        None,
    )
    if started_evt is None:
        return None

    tool = started_evt.get("tool") or "?"
    round_n = started_evt.get("round")
    completed_evt = next(
        (
            g
            for g in reversed(guardrails)
            if g.get("event") == "cli_review_completed"
            and g.get("tool") == tool
            and (round_n is None or g.get("round") == round_n)
        ),
        None,
    )
    failed_evt = next(
        (
            g
            for g in reversed(guardrails)
            if g.get("event") == "cli_review_failed"
            and g.get("tool") == tool
            and (round_n is None or g.get("round") == round_n)
        ),
        None,
    )

    if failed_evt is not None and (completed_evt is None or completed_evt.get("verdict") is None):
        status = "failed"
    elif completed_evt is not None:
        status = "completed"
    else:
        # Started but no terminal event recorded — treat as failed for
        # the viewer (run was likely SIGKILL'd mid-review).
        status = "failed"

    review_md_path = paths.run_dir / f"{tool}_review.md"
    markdown = ""
    if review_md_path.is_file():
        try:
            markdown = review_md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            markdown = ""

    # Lazy import — `cli_reviewer` imports `jsonlog` etc.; keep the viewer
    # import-cycle clean by deferring to call time.
    from ..cli_reviewer import extract_model, parse_verdict

    sink_name = f"{tool}_review_raw_export.jsonl"
    sink = paths.run_dir / sink_name
    model = extract_model(tool, sink) if tool in ("codex", "claude") else None

    verdict: str | None = None
    if completed_evt is not None:
        verdict = completed_evt.get("verdict")
    if not verdict and markdown:
        verdict = parse_verdict(markdown)

    duration_s = _event_duration(started_evt, completed_evt or failed_evt)

    return {
        "tool": tool,
        "status": status,
        "verdict": verdict,
        "duration_s": duration_s,
        "model": model,
        "url": (completed_evt or started_evt or {}).get("url"),
        "markdown": markdown,
        "fail_reason": (failed_evt or {}).get("reason"),
    }


def _assemble_cli_review_extras(paths: RunPaths) -> list[dict[str, Any]]:
    """Collect CLI review round artifacts from `<run_dir>/extras/cli_review_NNN/`.

    Each orchestrator loop round writes `<tool>_review.md` +
    `<tool>_raw_export.jsonl` under its own numbered sub-dir. Returns one
    dict per artifact in index order. Shape mirrors `_assemble_cli_review`
    plus a `source` label so the renderer can distinguish rounds.
    """

    extras_root = paths.run_dir / "extras"
    if not extras_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for extra_dir in sorted(extras_root.iterdir()):
        if not extra_dir.is_dir() or not extra_dir.name.startswith("cli_review_"):
            continue
        round_label = extra_dir.name.replace("cli_review_", "round-")
        for review_md_path in sorted(extra_dir.glob("*_review.md")):
            tool = review_md_path.name.removesuffix("_review.md")
            if tool not in ("codex", "claude"):
                continue
            try:
                markdown = review_md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                markdown = ""
            from ..cli_reviewer import extract_model, parse_verdict

            sink = extra_dir / f"{tool}_raw_export.jsonl"
            out.append(
                {
                    "tool": tool,
                    "source": f"{round_label}-{tool}",
                    "status": "completed" if markdown else "failed",
                    "verdict": parse_verdict(markdown) if markdown else None,
                    "duration_s": None,
                    "model": extract_model(tool, sink) if sink.is_file() else None,
                    "url": None,
                    "markdown": markdown,
                    "fail_reason": None if markdown else "missing review body",
                    "posted_to_pr": None,
                }
            )
        summary_path = extra_dir / "summary.json"
        review_md_path = extra_dir / "review.md"
        if not summary_path.is_file():
            continue
        summary = _read_json(summary_path, default=None)
        if not isinstance(summary, dict):
            continue
        markdown = ""
        if review_md_path.is_file():
            try:
                markdown = review_md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                markdown = ""
        has_review = bool(summary.get("review_chars")) and not summary.get("error")
        out.append(
            {
                "tool": summary.get("tool"),
                "source": extra_dir.name.replace("cli_review_", "extra-"),
                "status": "completed" if has_review else "failed",
                "verdict": summary.get("verdict"),
                "duration_s": summary.get("duration_s"),
                "model": summary.get("model"),
                "url": summary.get("pr_url"),
                "markdown": markdown,
                "fail_reason": summary.get("error"),
                "posted_to_pr": bool(summary.get("posted_to_pr")),
            }
        )
    return out


def _event_duration(
    start_evt: dict[str, Any] | None,
    end_evt: dict[str, Any] | None,
) -> float | None:
    """Seconds between two guardrail-event timestamps (ISO-8601 `ts` field)."""

    if not start_evt or not end_evt:
        return None
    from datetime import datetime

    def _parse(ts: Any) -> float | None:
        if not isinstance(ts, str):
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    t0 = _parse(start_evt.get("ts"))
    t1 = _parse(end_evt.get("ts"))
    if t0 is None or t1 is None:
        return None
    return round(t1 - t0, 3)


# ----- codex → opencode event normalization -----

_CODEX_EVENT_TYPES = frozenset(
    {"thread.started", "turn.started", "turn.completed", "item.started", "item.completed"}
)


def _is_codex_stream(events: list[dict[str, Any]]) -> bool:
    """True when this stream is codex `exec --json` output, not opencode's.

    codex speaks a thread/turn/item vocabulary and omits the `part`/`timestamp`
    envelope opencode uses; a single codex-shaped event is enough to tell.
    """

    return any(ev.get("type") in _CODEX_EVENT_TYPES for ev in events)


def _normalize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate a CLI-actor stream into opencode's event shape; pass others through.

    Every downstream consumer (`_summarize_events`, `_build_timeline`,
    `_build_chat`) understands only opencode's vocabulary (`tool_use` / `text` /
    `step_finish`, each wrapped in a `part`). codex and claude each emit a
    different one, so without this adapter their agent/SIM/reviewer turns are
    invisible in the viewer. Token counting mirrors `costs.sum_token_usage_in_events` (raw
    `input_tokens` / `output_tokens`, no cache subtraction) for consistency with
    the orchestrator's own rollup.
    """

    if _is_claude_stream(events):
        return _claude_to_opencode(events)
    if _is_codex_stream(events):
        return _codex_to_opencode(events)
    return events


def _codex_to_opencode(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten codex thread/turn/item events into opencode-shaped events.

    Per codex turn we emit, in order: every non-message item as a `tool_use`,
    one `step_finish` carrying the turn's token usage, then a closing `text`
    event holding the `agent_message`. That order matches what `_stream_turns`
    expects (tools + tokens accrue, then the text flushes the turn), so a codex
    turn renders as one chat bubble with its command trace attached.
    """

    out: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    text = ""
    text_ts: int | float | None = None
    usage: dict[str, Any] | None = None
    usage_ts: int | float | None = None
    open_turn = False

    def flush() -> None:
        nonlocal tools, text, text_ts, usage, usage_ts, open_turn
        if not open_turn:
            return
        out.extend(tools)
        if usage:
            out.append(
                {
                    "type": "step_finish",
                    "timestamp": usage_ts,
                    "part": {
                        "tokens": {
                            "input": int(usage.get("input_tokens") or 0),
                            "output": int(usage.get("output_tokens") or 0),
                        },
                        "cost": 0,
                    },
                }
            )
        # Always close with a text event so `_stream_turns` flushes the turn,
        # even when codex produced no agent_message (a tools-only turn). The
        # bubble's timestamp drives chat ordering, so fall back to the turn's
        # usage clock when the message itself carried none.
        out.append(
            {
                "type": "text",
                "timestamp": text_ts if text_ts is not None else usage_ts,
                "part": {"text": text},
            }
        )
        tools = []
        text = ""
        text_ts = None
        usage = None
        usage_ts = None
        open_turn = False

    for ev in events:
        etype = ev.get("type")
        # codex events are clockless on disk for legacy runs, but newer runs
        # carry a real `timestamp` (back-filled in cli_actor._stamp_event_slice);
        # propagate it so the chat shows measured times, not synthetic ones.
        ts = ev.get("timestamp")
        if etype in ("turn.started", "thread.started"):
            flush()  # close any turn left open by a missing turn.completed
            open_turn = etype == "turn.started"
        elif etype == "item.completed":
            open_turn = True
            item = ev.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message":
                t = item.get("text")
                if isinstance(t, str):
                    text = t
                    text_ts = ts
            else:
                tools.append(_codex_item_tool(item, ts=ts))
        elif etype == "turn.completed":
            u = ev.get("usage")
            if isinstance(u, dict):
                usage = u
                usage_ts = ts
            flush()
    flush()
    return out


def _codex_item_tool(item: dict[str, Any], *, ts: int | float | None = None) -> dict[str, Any]:
    """One codex non-message item → an opencode `tool_use` event.

    `command_execution` maps to a bash card (command + aggregated output, status
    from the exit code); `reasoning` to a card whose body is the thought; any
    other item type dumps its remaining fields generically so it still renders.
    `ts` carries the source event's clock through when present.
    """

    itype = item.get("type") or "item"
    if itype == "command_execution":
        cmd = item.get("command") or ""
        exit_code = item.get("exit_code")
        failed = isinstance(exit_code, int) and exit_code != 0
        return _tool_event(
            "bash",
            status="error" if failed else "completed",
            title=_first_line(cmd),
            inp={"command": cmd},
            output=item.get("aggregated_output") or "",
            metadata={"exit_code": exit_code} if exit_code is not None else {},
            ts=ts,
        )
    if itype == "reasoning":
        return _tool_event(
            "reasoning",
            status="completed",
            title="reasoning",
            inp={},
            output=item.get("text") or "",
            ts=ts,
        )
    body = {k: v for k, v in item.items() if k not in ("id", "type")}
    return _tool_event(
        itype,
        status=item.get("status") or "completed",
        title=itype,
        inp=body,
        output="",
        ts=ts,
    )


# ----- claude → opencode event normalization -----

# claude `stream-json` types, disjoint from opencode and codex vocabularies.
_CLAUDE_EVENT_TYPES = frozenset({"system", "assistant", "result"})


def _is_claude_stream(events: list[dict[str, Any]]) -> bool:
    """True when this stream is claude `-p --output-format stream-json` output.

    claude speaks `system`/`assistant`/`user`/`result`; a single such event is
    enough to tell it apart from opencode and codex.
    """

    return any(ev.get("type") in _CLAUDE_EVENT_TYPES for ev in events)


def _claude_to_opencode(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten claude stream-json into opencode-shaped events.

    Per claude turn (one `-p` invocation, `system/init` … `result`) we emit, in
    order: every `tool_use` block as a `tool_use` event (output stitched from the
    matching `tool_result` in a later `user` event), one `step_finish` with the
    turn's token usage + `total_cost_usd`, then a closing `text` event holding the
    authoritative `result.result`. That order matches what `_stream_turns`
    expects, so a claude turn renders as one chat bubble with its tool trace.
    """

    out: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    tools_by_id: dict[str, dict[str, Any]] = {}
    text = ""
    text_ts: int | float | None = None
    usage: dict[str, Any] | None = None
    usage_ts: int | float | None = None
    cost = 0.0
    open_turn = False

    def flush() -> None:
        nonlocal tools, tools_by_id, text, text_ts, usage, usage_ts, cost, open_turn
        if not open_turn:
            return
        out.extend(tools)
        out.append(
            {
                "type": "step_finish",
                "timestamp": usage_ts,
                "part": {
                    "tokens": {
                        "input": int((usage or {}).get("input_tokens") or 0),
                        "output": int((usage or {}).get("output_tokens") or 0),
                    },
                    "cost": cost,
                },
            }
        )
        out.append(
            {
                "type": "text",
                "timestamp": text_ts if text_ts is not None else usage_ts,
                "part": {"text": text},
            }
        )
        tools = []
        tools_by_id = {}
        text = ""
        text_ts = None
        usage = None
        usage_ts = None
        cost = 0.0
        open_turn = False

    for ev in events:
        etype = ev.get("type")
        # claude emits ISO-8601 string timestamps on user/assistant/result events;
        # coerce to epoch-ms so the chat preserves real times (and downstream
        # sorting never mixes str with int).
        ts = ts_to_ms(ev.get("timestamp"))
        if etype == "system" and ev.get("subtype") == "init":
            flush()  # close any turn left open by a missing result
            open_turn = True
        elif etype == "assistant":
            open_turn = True
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t:
                        text = t
                        text_ts = ts
                elif block.get("type") == "tool_use":
                    tool_ev = _claude_item_tool(block, ts=ts)
                    tools.append(tool_ev)
                    bid = block.get("id")
                    if bid:
                        tools_by_id[bid] = tool_ev
        elif etype == "user":
            open_turn = True
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                target = tools_by_id.get(block.get("tool_use_id"))
                if target is not None:
                    target["part"]["state"]["output"] = _claude_block_text(block.get("content"))
                    if block.get("is_error"):
                        target["part"]["state"]["status"] = "error"
        elif etype == "result":
            open_turn = True
            res = ev.get("result")
            if isinstance(res, str) and res:
                text = res
                text_ts = ts
            u = ev.get("usage")
            if isinstance(u, dict):
                usage = u
                usage_ts = ts
            c = ev.get("total_cost_usd")
            if isinstance(c, (int, float)):
                cost = float(c)
            flush()
    flush()
    return out


# claude tool name → opencode card tag (drives the viewer's per-tool styling).
_CLAUDE_TOOL_TAGS = {
    "Bash": "bash",
    "Read": "read",
    "Edit": "edit",
    "Write": "write",
    "Grep": "grep",
    "Glob": "glob",
    "Task": "task",
    "TodoWrite": "todowrite",
}


def _claude_item_tool(block: dict[str, Any], *, ts: int | float | None = None) -> dict[str, Any]:
    """One claude `tool_use` block → an opencode `tool_use` event (output empty).

    The output is stitched later from the matching `tool_result`. The title is a
    human-meaningful summary (command first line / file path / pattern) so the
    card reads well regardless of the raw input key names.
    """

    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    tag = _CLAUDE_TOOL_TAGS.get(name, name.lower())
    if name == "Bash":
        title = _first_line(str(inp.get("command") or ""))
    elif name in ("Read", "Edit", "Write"):
        title = str(inp.get("file_path") or "")
    elif name in ("Grep", "Glob"):
        title = str(inp.get("pattern") or inp.get("query") or inp.get("glob") or "")
    elif name == "Task":
        title = str(inp.get("description") or inp.get("subagent_type") or "")
    else:
        title = name
    return _tool_event(tag, status="completed", title=title, inp=inp, output="", ts=ts)


def _claude_block_text(content: Any) -> str:
    """Flatten a claude tool_result `content` (str or list of text blocks)."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _tool_event(
    tool: str,
    *,
    status: str,
    title: str,
    inp: dict[str, Any],
    output: str,
    metadata: dict[str, Any] | None = None,
    ts: int | float | None = None,
) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "timestamp": ts,
        "part": {
            "tool": tool,
            "state": {
                "status": status,
                "title": title,
                "input": inp,
                "output": output,
                "metadata": metadata or {},
            },
        },
    }


def _first_line(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0][:200]


def _assign_synthetic_timestamps(streams: list[list[dict[str, Any]]]) -> None:
    """Back-fill `timestamp` on clockless (codex) events, in place.

    codex carries no per-event clock. Two regimes:

      * Some stream has a real clock (e.g. opencode agent + codex SIM): spread
        each clockless stream's events evenly across the real `[tmin, tmax]`
        window, preserving file order, so codex turns interleave through the run
        instead of piling up at t=0. Approximate — flagged `ts_synthetic` so the
        chat can render the offset as `~` rather than `+`.

      * No real clock anywhere (all-codex run): assign a per-stream running
        index, staggered by stream position, so round N of each stream sorts
        together (agent N before SIM N before EXTRA N).
    """

    real = [
        ev["timestamp"]
        for stream in streams
        for ev in stream
        if isinstance(ev.get("timestamp"), (int, float))
    ]
    if real:
        tmin, tmax = min(real), max(real)
        span = (tmax - tmin) or 1
        for stream in streams:
            missing = [ev for ev in stream if not isinstance(ev.get("timestamp"), (int, float))]
            n = len(missing)
            if not n:
                continue
            for i, ev in enumerate(missing):
                ev["timestamp"] = tmin + int((i + 0.5) / n * span)
                ev["ts_synthetic"] = True
    else:
        width = len(streams) or 1
        for offset, stream in enumerate(streams):
            for i, ev in enumerate(stream):
                if not isinstance(ev.get("timestamp"), (int, float)):
                    ev["timestamp"] = i * width + offset
                    ev["ts_synthetic"] = True


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    n_tool_uses = 0
    n_text_events = 0
    n_step_finishes = 0
    tokens_in = 0
    tokens_out = 0

    for ev in events:
        t = ev.get("type")
        if t == "tool_use":
            n_tool_uses += 1
            part = ev.get("part") or {}
            tool = part.get("tool") or "?"
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
        elif t == "text":
            n_text_events += 1
        elif t == "step_finish":
            n_step_finishes += 1
            part = ev.get("part") or {}
            tokens = part.get("tokens") or {}
            tokens_in += int(tokens.get("input") or 0)
            tokens_out += int(tokens.get("output") or 0)

    return {
        "n_events": len(events),
        "n_tool_uses": n_tool_uses,
        "n_text_events": n_text_events,
        "n_step_finishes": n_step_finishes,
        "tool_counts": tool_counts,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def _merge_counts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + v
    return out


_CHAT_OUTPUT_CAP = 32_000


def _build_chat(
    agent_events: list[dict[str, Any]],
    sim_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bucket events into agent/sim turns and compute per-role totals.

    A "turn" is one text utterance plus every tool_use / step_finish that
    preceded it in that stream (since the agent's last text). This is the
    shape the chat-style renderer expects — bubbles with an attached tool
    trace. Mirrors the layout used in `agent_sim_conversation.html`.
    """

    agent_turns = _stream_turns(agent_events, "AGENT")
    sim_turns = _stream_turns(sim_events, "SIM")
    turns = sorted(
        agent_turns + sim_turns,
        key=lambda t: t["ts"] or 0,
    )

    t0 = min((t["ts"] for t in turns if t["ts"]), default=0)
    for t in turns:
        t["rel"] = round(((t["ts"] or 0) - t0) / 1000, 3) if t0 else 0

    def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "msgs": len(rows),
            "tools": sum(len(r["tools"]) for r in rows),
            "cost": round(sum(r["cost"] for r in rows), 6),
            "tokens": sum(r["tokens"] for r in rows),
        }

    duration = 0.0
    if turns:
        last_ts = max((t["ts"] or 0) for t in turns)
        if last_ts and t0:
            duration = round((last_ts - t0) / 1000, 3)

    return {
        "turns": turns,
        "totals": {"AGENT": _agg(agent_turns), "SIM": _agg(sim_turns)},
        "duration": duration,
    }


def _stream_turns(events: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_tools: list[dict[str, Any]] = []
    pending_tokens = 0
    pending_cost = 0.0

    def _flush(ts: int | None, text: str, synthetic: bool = False) -> None:
        nonlocal pending_tools, pending_tokens, pending_cost
        if not pending_tools and not text.strip():
            return
        summary: dict[str, int] = {}
        for tool in pending_tools:
            name = tool["tool"]
            summary[name] = summary.get(name, 0) + 1
        turns.append(
            {
                "who": role,
                "ts": ts,
                "ts_synthetic": synthetic,
                "text": text,
                "tools": pending_tools,
                "summary": summary,
                "cost": round(pending_cost, 6),
                "tokens": pending_tokens,
            }
        )
        pending_tools = []
        pending_tokens = 0
        pending_cost = 0.0

    for ev in events:
        t = ev.get("type")
        part = ev.get("part") or {}
        ts = ev.get("timestamp")
        if t == "tool_use":
            state = part.get("state") or {}
            output = state.get("output") or ""
            if isinstance(output, str) and len(output) > _CHAT_OUTPUT_CAP:
                output = (
                    output[:_CHAT_OUTPUT_CAP]
                    + f"\n\n… [truncated {len(output) - _CHAT_OUTPUT_CAP} chars]"
                )
            pending_tools.append(
                {
                    "tool": part.get("tool") or "?",
                    "ts": ts,
                    "status": state.get("status"),
                    "title": state.get("title") or "",
                    "input": state.get("input") or {},
                    "output": output,
                    "metadata": state.get("metadata") or {},
                }
            )
        elif t == "step_finish":
            tok = part.get("tokens") or {}
            pending_tokens += int(tok.get("input") or 0) + int(tok.get("output") or 0)
            pending_cost += float(part.get("cost") or 0)
        elif t == "text":
            text = part.get("text") or ev.get("text") or ""
            _flush(ts, text, bool(ev.get("ts_synthetic")))

    # Trailing tools with no closing text — still worth surfacing.
    if pending_tools:
        _flush(pending_tools[-1]["ts"], "")

    return turns


def _build_timeline(events: list[dict[str, Any]], actor: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        t = ev.get("type")
        if t not in ("tool_use", "text", "step_finish"):
            continue
        rec: dict[str, Any] = {
            "type": t,
            "timestamp": ev.get("timestamp"),
            "actor": actor,
        }
        part = ev.get("part") or {}
        if t == "tool_use":
            rec["tool"] = part.get("tool") or "?"
            state = part.get("state") or {}
            rec["status"] = state.get("status")
            rec["title"] = state.get("title") or ""
            inp = state.get("input") or {}
            for key in ("filePath", "path", "command", "description", "pattern"):
                if isinstance(inp.get(key), str) and inp[key]:
                    rec[key] = inp[key][:240]
                    break
        elif t == "text":
            text = part.get("text") or ev.get("text") or ""
            rec["text_len"] = len(text)
            rec["text_preview"] = text[:200]
        elif t == "step_finish":
            rec["tokens"] = part.get("tokens") or {}
            rec["cost"] = part.get("cost") or 0
        out.append(rec)
    return out


def _parse_transcript(path: Path) -> list[dict[str, str]]:
    """Split `transcript.md` on `## <phase> - <speaker>` headers.

    The transcript writer (`jsonlog.append_transcript`) emits this exact
    header shape; mirroring it here avoids needing to thread structured
    transcript records back to the viewer.
    """

    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    turns: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    buf: list[str] = []

    def _flush() -> None:
        if current is not None:
            current["text"] = "\n".join(buf).strip()
            turns.append(current)

    for line in lines:
        if line.startswith("## ") and " - " in line[3:]:
            _flush()
            head = line[3:].strip()
            phase, _, speaker = head.partition(" - ")
            current = {"speaker": speaker.strip().lower(), "phase": phase.strip(), "text": ""}
            buf = []
        else:
            if current is not None:
                buf.append(line)
    _flush()
    return [t for t in turns if t.get("text")]


def _read_dir(dir_path: Path, *, suffix: str | None = None) -> list[dict[str, Any]]:
    if not dir_path.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(dir_path.iterdir()):
        if not entry.is_file():
            continue
        if suffix is not None and entry.suffix != suffix:
            continue
        try:
            body = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append({"name": entry.name, "body": body, "len": len(body)})
    return out


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ----- HTML assembly -----


def _render_html(data: dict[str, Any], *, run_id: str) -> str:
    css = _CSS_PATH.read_text(encoding="utf-8")
    renderer = _RENDERER_PATH.read_text(encoding="utf-8")
    # Embed DATA as JSON. `</script>` sequences inside strings are escaped
    # so the renderer's </script> tag is unambiguous to the HTML parser.
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    title = f"contremaitre · {run_id}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
{css}
</style>
</head>
<body>
<div class="page page-wide">

<div class="topbar">
  <span class="crumb">contremaitre</span>
  <span class="dim">/</span>
  <span class="crumb">{_escape(run_id)}</span>
</div>

<h1 id="rep-title">{_escape(run_id)}</h1>
<p class="tagline" id="rep-tagline"></p>

<div class="totals" id="stats-header"></div>

<nav class="viewer-nav" id="nav"></nav>

<section id="overview" class="active"></section>
<section id="conversation"></section>
<section id="timeline"></section>
<section id="subagents"></section>
<section id="files"></section>
<section id="events"></section>
<section id="eval"></section>

<footer>
  run: <code>{_escape(run_id)}</code> · viewer built from extracted artifacts
</footer>

</div>
<script>
const DATA = {payload};
</script>
<script>
{renderer}
</script>
</body>
</html>
"""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
