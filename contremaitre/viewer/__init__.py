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

from ..models import RunPaths

_HERE = Path(__file__).resolve().parent
_CSS_PATH = _HERE / "_styles.css"
_RENDERER_PATH = _HERE / "_renderer.js"

VIEWER_FILENAME = "viewer.html"


def build_viewer(paths: RunPaths) -> Path:
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
        from .index import build_index
        build_index(paths.run_dir.parent)
    except Exception:
        pass

    return out


# ----- DATA assembly -----


def _assemble_data(paths: RunPaths) -> dict[str, Any]:
    stats_raw = _read_json(paths.stats, default={})
    agent_events = _read_jsonl(paths.raw_export)
    sim_events = _read_jsonl(paths.sim_raw_export)
    extra_reviewer_events = _read_jsonl(paths.extra_reviewer_raw_export)
    extra_reviewer_enabled = bool(extra_reviewer_events) or bool(stats_raw.get("extra_reviewer_model"))

    agent_summary = _summarize_events(agent_events)
    sim_summary = _summarize_events(sim_events)
    extra_summary = _summarize_events(extra_reviewer_events)

    timeline = (
        _build_timeline(agent_events, "agent")
        + _build_timeline(sim_events, "sim")
        + _build_timeline(extra_reviewer_events, "extra")
    )
    timeline.sort(key=lambda e: e.get("timestamp") or 0)

    chat = _build_chat(
        agent_events,
        sim_events,
        extra_reviewer_events=extra_reviewer_events if extra_reviewer_enabled else None,
    )

    transcript = _parse_transcript(paths.transcript)

    sub_agents = _read_dir(paths.subagents_dir, suffix=".md")
    extracted_files = _read_dir(paths.extracted_files_dir)

    pr = _read_json(paths.pr_json, default=None)

    eval_blob: dict[str, Any] = {}
    for src in (paths.cost_report, paths.preflight_report, paths.checks_report,
                paths.pr_eval, paths.settled_diff_report,
                paths.architecture_delta_report, paths.trajectory_report):
        loaded = _read_json(src, default=None)
        if loaded is not None:
            eval_blob[src.stem] = loaded

    guardrails = _read_jsonl(paths.guardrail_events)
    recoveries = _read_jsonl(paths.recoveries)
    cli_review = _assemble_cli_review(paths, guardrails)

    stats = {
        **stats_raw,
        "cost_usd": stats_raw.get("recorded_cost_usd"),
        "n_events": agent_summary["n_events"] + sim_summary["n_events"] + extra_summary["n_events"],
        "n_tool_uses": agent_summary["n_tool_uses"] + sim_summary["n_tool_uses"] + extra_summary["n_tool_uses"],
        "n_text_events": agent_summary["n_text_events"] + sim_summary["n_text_events"] + extra_summary["n_text_events"],
        "n_step_finishes": agent_summary["n_step_finishes"] + sim_summary["n_step_finishes"] + extra_summary["n_step_finishes"],
        "tool_counts": _merge_counts(
            _merge_counts(agent_summary["tool_counts"], sim_summary["tool_counts"]),
            extra_summary["tool_counts"],
        ),
        "tokens_in": agent_summary["tokens_in"] + sim_summary["tokens_in"] + extra_summary["tokens_in"],
        "tokens_out": agent_summary["tokens_out"] + sim_summary["tokens_out"] + extra_summary["tokens_out"],
        "agent_tool_counts": agent_summary["tool_counts"],
        "sim_tool_counts": sim_summary["tool_counts"],
        "extra_reviewer_tool_counts": extra_summary["tool_counts"] if extra_reviewer_enabled else None,
        "extra_reviewer_enabled": extra_reviewer_enabled,
        "files_written_count": len(extracted_files),
        "subagent_count": len(sub_agents),
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
        "eval": eval_blob or None,
        "guardrails": guardrails,
        "recoveries": recoveries,
        "cli_review": cli_review,
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
        "verdict":    "🟢"/"🟠"/"🔴" | None,     # parsed agent verdict
        "duration_s": float | None,                # cli_review_started → completed/failed
        "model":      str | None,                  # codex preamble only
        "url":        str | None,                  # PR URL the comment was posted to
        "markdown":   str | "",                    # body the orchestrator posted
        "fail_reason": str | None,                 # only when status == "failed"
      }

    Reads only what already exists on disk — no events emitted, no
    side effects.
    """

    started_evt = next((g for g in guardrails if g.get("event") == "cli_review_started"), None)
    if started_evt is None:
        return None
    completed_evt = next((g for g in guardrails if g.get("event") == "cli_review_completed"), None)
    failed_evt = next((g for g in guardrails if g.get("event") == "cli_review_failed"), None)

    tool = (completed_evt or failed_evt or started_evt).get("tool") or "?"
    if failed_evt is not None and completed_evt is None:
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
    *,
    extra_reviewer_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bucket events into agent/sim/extra turns and compute per-role totals.

    A "turn" is one text utterance plus every tool_use / step_finish that
    preceded it in that stream (since the agent's last text). This is the
    shape the chat-style renderer expects — bubbles with an attached tool
    trace. Mirrors the layout used in `agent_sim_conversation.html`.

    `extra_reviewer_events=None` (no extra reviewer configured) returns the
    original AGENT/SIM totals shape so the renderer stays back-compat.
    """

    agent_turns = _stream_turns(agent_events, "AGENT")
    sim_turns = _stream_turns(sim_events, "SIM")
    extra_turns = (
        _stream_turns(extra_reviewer_events, "EXTRA")
        if extra_reviewer_events is not None
        else []
    )
    turns = sorted(
        agent_turns + sim_turns + extra_turns,
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

    totals: dict[str, Any] = {"AGENT": _agg(agent_turns), "SIM": _agg(sim_turns)}
    if extra_reviewer_events is not None:
        totals["EXTRA"] = _agg(extra_turns)

    return {
        "turns": turns,
        "totals": totals,
        "duration": duration,
    }


def _stream_turns(events: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_tools: list[dict[str, Any]] = []
    pending_tokens = 0
    pending_cost = 0.0

    def _flush(ts: int | None, text: str) -> None:
        nonlocal pending_tools, pending_tokens, pending_cost
        if not pending_tools and not text.strip():
            return
        summary: dict[str, int] = {}
        for tool in pending_tools:
            name = tool["tool"]
            summary[name] = summary.get(name, 0) + 1
        turns.append({
            "who": role,
            "ts": ts,
            "text": text,
            "tools": pending_tools,
            "summary": summary,
            "cost": round(pending_cost, 6),
            "tokens": pending_tokens,
        })
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
                output = output[:_CHAT_OUTPUT_CAP] + f"\n\n… [truncated {len(output) - _CHAT_OUTPUT_CAP} chars]"
            pending_tools.append({
                "tool": part.get("tool") or "?",
                "ts": ts,
                "status": state.get("status"),
                "title": state.get("title") or "",
                "input": state.get("input") or {},
                "output": output,
                "metadata": state.get("metadata") or {},
            })
        elif t == "step_finish":
            tok = part.get("tokens") or {}
            pending_tokens += int(tok.get("input") or 0) + int(tok.get("output") or 0)
            pending_cost += float(part.get("cost") or 0)
        elif t == "text":
            text = part.get("text") or ev.get("text") or ""
            _flush(ts, text)

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
            text = (part.get("text") or ev.get("text") or "")
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
<section id="chat"></section>
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
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
