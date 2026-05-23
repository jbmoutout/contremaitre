"""Live TUI for Contremaitre runs.

Layout:
    header    run id, agent + SIM models, docker image
    panes     Agent (left) | SIM (right) — RichLog widgets with scrollback
    log       guardrail_events.jsonl + recoveries.jsonl tail (RichLog)
    footer    4 zones separated by ` │ ` —
              (1) pipeline breadcrumb,
              (2) gates: settled / impl + review rounds + test pass-rate,
              (3) metrics: A/S turns, sub-agents, recoveries, elapsed, cost,
              (4) process verdict (running / exited N / terminal state).

Scrollback: mouse wheel / PageUp / PageDown / Home / End inside any pane.
Auto-sticks to bottom when at bottom; stays put if you've scrolled up.

Two entry modes via the `contremaitre tui` subcommand:
  - `tui run [run-args]` — spawn `contremaitre run` and attach
  - `tui attach <run-dir>` — read-only watch of an in-progress or finished run

Requires `textual` (optional extra). Install with:
    pip install --user textual
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .costs import sum_costs_in_events

try:
    from rich.table import Table
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.widgets import RichLog, Static

    _TEXTUAL_AVAILABLE = True
except ImportError:  # pragma: no cover — gated at CLI entry point
    _TEXTUAL_AVAILABLE = False


SETTLED_FILE_RE = re.compile(r"/SETTLED_DESIGN\.md$", re.IGNORECASE)
IMPL_COMPLETE_FILE_RE = re.compile(r"/IMPLEMENTATION_COMPLETE$")
APPLY_PATCH_SETTLED_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update)\s+File:\s*.*SETTLED_DESIGN\.md\s*$",
    re.IGNORECASE | re.MULTILINE,
)
DOCKER_REFRESH_S = 2.0


# ---------- JSONL helpers ----------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _file_age(path: Path | None) -> float | None:
    if path is None or not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def _short_model(model: str) -> str:
    return model.split("/")[-1] if model else "?"


def _is_free_model(model: str) -> bool:
    """True for OpenCode Zen free tier or OpenRouter `:free` variants.

    Matches:
      - `opencode/<name>-free` (Zen's free tier convention)
      - `opencode/big-pickle` (stealth free model)
      - `openrouter/<name>:free` (legacy OpenRouter free routing)
    """

    if not model:
        return False
    bare = model.rsplit("/", 1)[-1]
    return bare.endswith("-free") or bare == "big-pickle" or bare.endswith(":free")


def _fmt_ts(ts_ms: int | None) -> str:
    if not ts_ms:
        return "        "
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts_ms / 1000))
    except (ValueError, OSError):
        return "        "


def _fmt_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


# Footer / chrome palette — mirrors viewer.html CSS vars so footer reads in
# the same language as agent/SIM panes (which already use these hexes via
# _TOOL_STYLES below). Kept as module constants so renderers stay terse.
_PAL_BRIGHT = "#FFFFFF"
_PAL_TEXT = "#C9C9C9"
_PAL_DIM = "#555555"
_PAL_VDIM = "#444444"
_PAL_SUCCESS = "#4ADE80"
_PAL_WARN = "#FFB830"
_PAL_ERROR = "#FF3B3B"
_PAL_ACCENT = "#6B8AFF"


_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _activity_state(*, container_present: bool, file_age: float | None) -> str:
    """Classify a pane's per-refresh state for the loader.

    `active`    — container running and stdout file written in the last 2s
                  (events landing in real time: tool_use, text deltas).
    `thinking`  — container running but no recent stdout writes (model is
                  mid-generation; no events to render yet).
    `idle`      — no container; either pre-launch or already finished.
    """

    if not container_present:
        return "idle"
    if file_age is not None and file_age < 2.0:
        return "active"
    return "thinking"


def _render_pane_subheader(
    *,
    state: str,
    spinner: str,
    turns: int,
    pending_tool: str | None,
    container_id: str | None,
    container_uptime: str | None,
) -> Text:
    """Per-pane subheader, color-coded by activity state.

    Format: `<spinner> <state>  ·  turns: N  ·  doing: <tool>  ·  container <id> (<uptime>)`
    The spinner ticks per refresh when state is `active` or `thinking`;
    it disappears (replaced by a static dot) when `idle` so a finished
    pane doesn't keep visually animating.
    """

    sub = Text()
    if state == "active":
        sub.append(spinner, style="bold green")
        sub.append(" streaming", style="green")
    elif state == "thinking":
        sub.append(spinner, style="bold cyan")
        sub.append(" thinking…", style="cyan")
    else:
        sub.append("•", style="dim")
        sub.append(" idle", style="dim")
    sub.append(f"  ·  turns: {turns}", style="dim")
    if pending_tool:
        clipped = pending_tool if len(pending_tool) <= 60 else pending_tool[:57] + "…"
        sub.append(f"  ·  doing: {clipped}", style="dim")
    if container_id:
        sub.append(f"  ·  container {container_id} ({container_uptime})", style="dim")
    else:
        sub.append("  ·  no container", style="dim")
    return sub


def _state_breadcrumb(guardrails: list[dict[str, Any]], *, terminal_stats: Path | None, failed: bool = False) -> Text:
    """Render INIT > WORK > REVIEW > APPROVED > PUBLISHED progression.

    Derived from guardrail_events (cheap inference, no orchestrator state
    leaked): WORK starts at the first opencode_actor_start with role=agent,
    REVIEW at role=review, APPROVED at work_session_end with outcome
    starting "approved", PUBLISHED at the `published` event,
    BLOCKED/FAILED at publication_blocked / infra_failure. Bold-cyan the
    current state, dim the rest.
    """

    stages = ["INIT", "WORK", "REVIEW", "APPROVED", "PUBLISHED"]
    current = "INIT"
    blocked_or_failed: str | None = None
    for ev in guardrails:
        kind = ev.get("event")
        if kind == "opencode_actor_start":
            role = ev.get("role")
            if role == "agent" and current == "INIT":
                current = "WORK"
            elif role == "review":
                current = "REVIEW"
        elif kind == "published":
            current = "PUBLISHED"
        elif kind == "publication_blocked":
            blocked_or_failed = "BLOCKED"
        elif kind == "infra_failure":
            blocked_or_failed = "FAILED"
    if terminal_stats is not None and current == "REVIEW" and blocked_or_failed is None:
        # Terminal reached but no PUBLISHED event — common when SIM
        # disapproved or hard gates failed before publication.
        current = (
            "APPROVED"
            if (terminal_stats and "READY_FOR_DRAFT_PR" in terminal_stats.read_text(encoding="utf-8", errors="replace"))
            else current
        )

    text = Text()
    for i, stage in enumerate(stages):
        if i > 0:
            text.append(" › ", style=_PAL_VDIM)
        is_current = stage == current and blocked_or_failed is None
        if is_current:
            if failed:
                stage_style = f"bold {_PAL_ERROR}"   # stuck here in failure
            elif stage == "PUBLISHED":
                stage_style = f"bold {_PAL_SUCCESS}"  # terminal success
            else:
                stage_style = f"bold {_PAL_BRIGHT}"   # active / in progress
            text.append(stage, style=stage_style)
        elif stages.index(current) > i and blocked_or_failed is None:
            text.append(stage, style=_PAL_SUCCESS)
        else:
            text.append(stage, style=_PAL_DIM)
    if blocked_or_failed:
        text.append("  →  ", style=_PAL_DIM)
        text.append(blocked_or_failed, style=f"bold {_PAL_ERROR}")
    return text


def _review_summary(review_cycles: list[dict[str, Any]]) -> Text | None:
    """Compact review-rounds indicator: `R N ✓` or `R N ✗`.

    Reads review_cycles.jsonl (one row per SIM review). Last row's verdict
    sets the icon; the number is the round count so a bounce shows as `R 2`.
    """

    if not review_cycles:
        return None
    last = review_cycles[-1]
    n = last.get("round") or len(review_cycles)
    verdict = (last.get("verdict") or "").upper()
    t = Text()
    t.append(f"R {n} ", style=_PAL_TEXT)
    if verdict == "APPROVED":
        t.append("✓", style=_PAL_SUCCESS)
    elif verdict == "CHANGES_REQUESTED":
        t.append("✗", style=_PAL_WARN)
    else:
        t.append("·", style=_PAL_DIM)
    return t


def _tests_summary(test_runs: list[dict[str, Any]]) -> Text | None:
    """Compact test-runs indicator: `tests P/T ✓` (or ✗ if any failed)."""

    if not test_runs:
        return None
    total = len(test_runs)
    passed = sum(1 for r in test_runs if r.get("returncode") == 0)
    t = Text()
    t.append(f"tests {passed}/{total} ", style=_PAL_TEXT)
    t.append("✓" if passed == total else "✗", style=_PAL_SUCCESS if passed == total else _PAL_ERROR)
    return t


# ---------- Event introspection ----------


def _text_event_count(events: list[dict[str, Any]]) -> int:
    return sum(1 for e in events if e.get("type") == "text")


def _task_count(events: list[dict[str, Any]]) -> int:
    return sum(1 for e in events if e.get("type") == "tool_use" and (e.get("part") or {}).get("tool") == "task")


def _settled_in(events: list[dict[str, Any]]) -> bool:
    for e in events:
        if e.get("type") != "tool_use":
            continue
        part = e.get("part") or {}
        state = part.get("state") or {}
        if state.get("status") != "completed":
            continue
        tool = part.get("tool")
        inp = state.get("input") or {}
        if tool in ("write", "edit"):
            fp = inp.get("filePath") or inp.get("path") or ""
            if SETTLED_FILE_RE.search(fp):
                return True
        elif tool == "apply_patch":
            patch = inp.get("patchText") or inp.get("patch") or ""
            if APPLY_PATCH_SETTLED_RE.search(patch):
                return True
    return False


def _impl_complete_in(events: list[dict[str, Any]]) -> bool:
    for e in events:
        if e.get("type") != "tool_use":
            continue
        part = e.get("part") or {}
        state = part.get("state") or {}
        if state.get("status") != "completed":
            continue
        tool = part.get("tool")
        inp = state.get("input") or {}
        if tool in ("write", "edit"):
            fp = inp.get("filePath") or inp.get("path") or ""
            if IMPL_COMPLETE_FILE_RE.search(fp):
                return True
    return False


def _latest_pending_tool(events: list[dict[str, Any]]) -> str | None:
    for e in reversed(events):
        if e.get("type") != "tool_use":
            continue
        part = e.get("part") or {}
        state = part.get("state") or {}
        if state.get("status") == "completed":
            return None
        tool = part.get("tool") or "?"
        inp = state.get("input") or {}
        if tool in ("read", "edit", "write"):
            target = inp.get("filePath") or inp.get("path") or ""
            return f"{tool} {Path(target).name}" if target else tool
        if tool == "task":
            desc = inp.get("description") or inp.get("subagent_type") or ""
            return f"task: {desc[:30]}" if desc else "task"
        if tool == "bash":
            cmd = (inp.get("command") or "")[:30]
            return f"bash `{cmd}`"
        if tool == "grep":
            pat = (inp.get("pattern") or "")[:30]
            return f"grep {pat!r}"
        if tool == "glob":
            pat = inp.get("pattern") or inp.get("globPattern") or ""
            return f"glob {pat[:30]!r}"
        return tool
    return None


# ---------- Docker introspection ----------


def _docker_info(image_name: str, worktree: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "image_created": None,
        "agent_container": None,
        "sim_container": None,
    }
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image_name, "--format", "{{.Created}}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            info["image_created"] = proc.stdout.strip()[:10]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    worktree_str = str(worktree)
    try:
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "--no-trunc",
                "--format",
                "{{.ID}}\t{{.Mounts}}\t{{.RunningFor}}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            cid = parts[0]
            mounts = parts[1]
            uptime = parts[2] if len(parts) > 2 else ""
            if worktree_str not in mounts:
                continue
            # Distinguish agent (:rw) from SIM (:ro) by checking the mount
            # mode. docker ps doesn't expose the mode directly in --format;
            # heuristic: scan `docker inspect <cid>` for ReadOnly.
            mode = _container_mount_mode(cid, worktree_str)
            if mode == "ro":
                info["sim_container"] = {"id": cid[:12], "uptime": uptime}
            else:
                info["agent_container"] = {"id": cid[:12], "uptime": uptime}
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return info


@functools.lru_cache(maxsize=256)
def _container_mount_mode(cid: str, worktree_str: str) -> str:
    """Return 'ro' or 'rw' for the worktree mount on the container.

    Mount mode is fixed for a container's lifetime; cache by cid+mount so the
    TUI's 2-second refresh loop doesn't shell out to `docker inspect` every
    tick. lru_cache lets stale cids age out automatically.
    """

    try:
        proc = subprocess.run(
            ["docker", "inspect", cid, "--format", "{{range .Mounts}}{{.Source}}|{{.Mode}};{{end}}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "rw"
    if proc.returncode != 0:
        return "rw"
    for entry in proc.stdout.strip().split(";"):
        if "|" not in entry:
            continue
        src, mode = entry.split("|", 1)
        if src == worktree_str:
            return "ro" if "ro" in mode else "rw"
    return "rw"


# ---------- Per-event Rich renderables (only used when textual is loaded) ----------


_TOOL_STYLES = {
    "read": "#6B8AFF",
    "grep": "#6B8AFF",
    "glob": "#6B8AFF",
    "edit": "#FFB830",
    "write": "#FFB830",
    "apply_patch": "#FFB830",
    "bash": "#4ADE80",
    "task": "#C792EA",
    "skill": "#FF3B3B",
    "todowrite": "#888888",
}


def _tool_style(tool: str) -> str:
    return _TOOL_STYLES.get(tool, "magenta")


def _truncate(value: str, limit: int = 200) -> str:
    """Cap unbounded tool args so a multi-KB body doesn't crash the RichLog."""

    if len(value) <= limit:
        return value
    return value[:limit] + f"… (+{len(value) - limit:,} chars)"


def _tool_body(tool: str, inp: dict[str, Any], state: dict[str, Any]) -> str:
    if tool == "task":
        desc = inp.get("description") or inp.get("subagent_type") or ""
        out = state.get("output") or ""
        status = state.get("status", "")
        bits = []
        if desc:
            bits.append(f"desc: {_truncate(desc)}")
        if status == "completed" and out:
            bits.append(f"[subagent output · {len(out):,} chars]")
        return "  ·  ".join(bits)
    if tool == "skill":
        return inp.get("name", "")
    if tool in ("read", "edit"):
        fp = inp.get("filePath") or inp.get("path") or ""
        return f"file: {fp}" if fp else ""
    if tool == "write":
        fp = inp.get("filePath", "")
        n = len(inp.get("content", "") or "")
        return f"file: {fp}  ({n:,}B)" if fp else f"({n:,}B)"
    if tool == "glob":
        pat = inp.get("pattern") or inp.get("globPattern") or ""
        return f"pattern: {pat}"
    if tool == "grep":
        pat = inp.get("pattern", "")
        inc = inp.get("path") or inp.get("include") or ""
        return f"pattern: {_truncate(pat)}" + (f"  in {inc}" if inc else "")
    if tool == "bash":
        cmd = inp.get("command") or ""
        return f"cmd: {_truncate(cmd)}"
    if tool == "apply_patch":
        patch = inp.get("patchText") or inp.get("patch") or ""
        m = re.search(r"\*\*\*\s+(Add|Update|Delete)\s+File:\s*(\S+)", patch[:200])
        if m:
            return f"{m.group(1).lower()}: {m.group(2)}"
        return "patch"
    return ""


def _build_event_row(event: dict[str, Any]):
    t = event.get("type", "")
    p = event.get("part") or {}
    ts = _fmt_ts(event.get("timestamp"))

    if t == "step_start":
        return ("", ts, Text("step_start", style="dim"), "", "")

    if t == "step_finish":
        tok = p.get("tokens") or {}
        cost = p.get("cost")
        cache = tok.get("cache") or {}
        bits = [
            f"in {tok.get('input', 0):,}",
            f"out {tok.get('output', 0):,}",
            f"cache-r {cache.get('read', 0)}",
        ]
        if cost is not None:
            bits.append(f"cost ${cost:.4f}")
        return ("", ts, Text("step_finish", style="dim"), "", Text(" ".join(bits), style="dim"))

    if t == "tool_use":
        tool = p.get("tool") or "?"
        state = p.get("state") or {}
        inp = state.get("input") or {}
        body = _tool_body(tool, inp, state)
        return ("", ts, Text("tool_use", style="dim"), Text(tool, style=f"bold {_tool_style(tool)}"), body)

    if t == "text":
        txt = p.get("text") or ""
        body = Text()
        body.append(f"{len(txt):,} chars\n", style="dim")
        body.append(txt)
        marker_style = "magenta" if event.get("_recovered_from_sqlite") else "blue"
        return (Text("▍", style=marker_style), ts, Text("text", style="bold"), "", body)

    if t == "error":
        err = p.get("error") or event.get("error") or {}
        if isinstance(err, dict):
            data = err.get("data") or {}
            msg = data.get("message") or err.get("name") or str(err)[:200]
        else:
            msg = str(err)[:200]
        return (Text("▍", style="red"), ts, Text("error", style="bold red"), "", Text(msg, style="red"))

    return ("", ts, Text(t or "?", style="dim"), "", "")


def _event_table() -> "Table":
    t = Table.grid(padding=(0, 1))
    t.add_column(width=1, no_wrap=True)
    t.add_column(width=8, no_wrap=True, style="dim")
    t.add_column(width=11, no_wrap=True)
    t.add_column(width=11, no_wrap=True)
    t.add_column(overflow="fold")
    return t


def _render_event(event: dict[str, Any]):
    marker, ts, typ, tool, body = _build_event_row(event)
    t = _event_table()
    t.add_row(marker, ts, typ, tool, body)
    return t


def _turn_separator(turn_number: int, role: str) -> Text:
    """Visual break at an orchestrator-driven handover boundary.

    Driven by `opencode_actor_start` events in guardrail_events.jsonl —
    each one marks the orchestrator handing the conversation to a new
    role (agent → sim, sim → agent, sim → reviewer). The separator is
    written into the pane that just finished a turn, so the operator
    sees a clear "this turn ended, handover" cue. A single opencode
    invocation can emit multiple `text` events, so we drive off the
    orchestrator's own boundary marker rather than text events.
    """

    label = f"turn {turn_number} · {role} ✓"
    body = "── " + label + " " + "─" * max(0, 70 - len(label) - 4)
    sep = Text()
    sep.append(body, style="bold cyan")
    return sep


def _role_label(role: str) -> str:
    # `review` is the SIM doing the review pass — surfaced as `reviewer`
    # so the operator can tell a review turn from a WORK-loop sim turn.
    return "reviewer" if role == "review" else role


def _render_guardrail(event: dict[str, Any]):
    """Render a guardrail_events.jsonl or recoveries.jsonl line."""

    ts_iso = event.get("ts", "")
    ts = ts_iso[11:19] if len(ts_iso) > 19 else " " * 8
    kind = event.get("event") or event.get("kind") or "?"

    # Semantic style — most events are dim scaffolding; only actionable
    # signals get color so the operator's eye goes straight to what matters.
    if any(k in kind for k in ("infra_failure", "blocked", "malformed")):
        style = f"bold {_PAL_ERROR}"
    elif any(k in kind for k in ("recovery", "orphan", "sqlite", "cap")):
        style = f"bold {_PAL_WARN}"
    elif kind == "published":
        style = f"bold {_PAL_SUCCESS}"
    elif kind == "work_session_end" or "implementation_complete" in kind:
        style = f"bold {_PAL_SUCCESS}"
    elif kind == "revision_requested":
        style = _PAL_WARN
    elif kind == "review_verdict":
        verdict = (event.get("verdict") or "").upper()
        style = f"bold {_PAL_SUCCESS}" if verdict == "APPROVED" else f"bold {_PAL_WARN}"
    elif kind == "check_completed":
        rc = event.get("returncode")
        style = f"bold {_PAL_ERROR}" if rc not in (0, None) else _PAL_SUCCESS
    elif kind == "hard_gates_checked":
        style = f"bold {_PAL_SUCCESS}" if event.get("passed") else f"bold {_PAL_ERROR}"
    else:
        style = "dim"

    body = Text()
    body.append(f"{ts} ", style="dim")

    # Prefix icon for the events operators care about most.
    if kind == "check_completed":
        rc = event.get("returncode")
        icon = "✓" if rc in (0, None) else "✗"
        body.append(f"{icon} ", style=style)
    elif kind == "review_verdict":
        verdict = (event.get("verdict") or "").upper()
        body.append("✓ " if verdict == "APPROVED" else "✗ ", style=style)
    elif kind == "published":
        body.append("✓ ", style=style)
    elif kind == "hard_gates_checked":
        body.append("✓ " if event.get("passed") else "✗ ", style=style)

    body.append(kind, style=style)

    for field in ("role", "outcome", "round", "verdict", "recovered_chars"):
        if field in event:
            body.append(f"  {field}={event[field]}", style="dim")
    if kind == "check_completed":
        cmd = event.get("cmd", "")
        if cmd:
            body.append(f"  {cmd}", style="dim")
        rc = event.get("returncode")
        if rc not in (0, None):
            body.append(f"  rc={rc}", style=f"bold {_PAL_ERROR}")
        dur = event.get("duration_seconds")
        if dur is not None:
            body.append(f"  {dur:.1f}s", style="dim")
    if event.get("error"):
        body.append(f"  error={event['error'][:120]}", style=f"bold {_PAL_ERROR}")
    head = event.get("stdout_head")
    if head:
        for line in str(head).splitlines():
            body.append("\n    ")
            body.append(line, style=_PAL_ERROR)
    return body


# ---------- Textual app ----------


if _TEXTUAL_AVAILABLE:

    class ContremaitreTUI(App):
        CSS = """
        Screen { layout: vertical; padding: 1 2; }
        #header { height: 1; padding: 0 1; }
        #panes { height: 1fr; }
        .pane { width: 1fr; border: heavy white; }
        .pane.active { border: heavy yellow; }
        .pane-sub { height: 1; padding: 0 1; color: $text-muted; }
        RichLog {
            background: $background;
            scrollbar-size: 1 1;
            scrollbar-color: white;
            scrollbar-color-active: white;
            scrollbar-color-hover: white;
            scrollbar-background: $background;
            scrollbar-background-active: $background;
            scrollbar-background-hover: $background;
        }
        #activity-panel { height: 10; border: heavy white; }
        #footer-line { height: 1; padding: 0 1; }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit", "Quit (kills orchestrator)", show=True),
        ]

        def __init__(
            self,
            run_dir: Path,
            *,
            agent_model: str = "?",
            sim_model: str = "?",
            docker_image: str = "?",
            proc: subprocess.Popen | None = None,
            refresh_hz: float = 5.0,
        ):
            super().__init__()
            self.run_dir = run_dir
            self.agent_model = agent_model
            self.sim_model = sim_model
            self.docker_image = docker_image
            self.proc = proc
            self.refresh_hz = refresh_hz
            self.t_start = time.time()
            self._agent_idx = 0
            self._sim_idx = 0
            self._guardrail_idx = 0
            self._recoveries_idx = 0
            self._docker_state: dict[str, Any] = {}
            self._docker_ts = 0.0
            self._showed_initial_prompt = False
            # Snapshot of elapsed / last-write age at terminal state — once
            # stats.json appears the run is over and these numbers should
            # stop incrementing (previous behavior had the clock running
            # forever, which made the "9m18s ago" of a finished run look
            # like a stall).
            self._frozen_elapsed: float | None = None
            self._frozen_gr_age: float | None = None
            # Animated Braille spinner. Cycled once per chrome refresh
            # (~5Hz default) so the rotation is visible to the operator
            # without eating CPU. Resets when both containers go idle.
            self._spin_tick = 0
            # Per-pane handover separator counters. Each orchestrator
            # turn handover emits one `opencode_actor_start` event with
            # role agent/sim/review; we render a separator into the
            # *just-finished* pane each time a new start event appears
            # for it, so the label corresponds to the turn that ended.
            self._agent_separators_rendered = 0
            self._sim_separators_rendered = 0

        @property
        def paths(self) -> dict[str, Path]:
            return {
                "raw_export": self.run_dir / "raw_export.jsonl",
                "sim_raw_export": self.run_dir / "sim_raw_export.jsonl",
                "guardrail_events": self.run_dir / "guardrail_events.jsonl",
                "recoveries": self.run_dir / "recoveries.jsonl",
                "review_cycles": self.run_dir / "review_cycles.jsonl",
                "test_runs": self.run_dir / "test_runs.jsonl",
                "initial_prompt": self.run_dir / "initial_prompt.txt",
                "stats": self.run_dir / "stats.json",
            }

        @property
        def worktree(self) -> Path:
            return Path("/tmp") / f"contremaitre-{self.run_dir.name}"

        def compose(self) -> ComposeResult:
            yield Static("", id="header")
            with Horizontal(id="panes"):
                with Vertical(classes="pane", id="agent-pane"):
                    yield RichLog(id="agent-log", auto_scroll=False, markup=False, wrap=True, highlight=False)
                    yield Static("", classes="pane-sub", id="agent-sub")
                with Vertical(classes="pane", id="sim-pane"):
                    yield RichLog(id="sim-log", auto_scroll=False, markup=False, wrap=True, highlight=False)
                    yield Static("", classes="pane-sub", id="sim-sub")
            with Vertical(id="activity-panel"):
                yield RichLog(id="activity-log", auto_scroll=False, markup=False, wrap=True, highlight=False)
            yield Static("", id="footer-line")

        def on_mount(self) -> None:
            self.title = f"contremaitre · {self.run_dir.name}"
            refresh_s = 1.0 / max(0.5, min(20.0, self.refresh_hz))
            self.set_interval(refresh_s, self._tick)

        def _tick(self) -> None:
            now = time.time()
            if now - self._docker_ts > DOCKER_REFRESH_S:
                self._docker_state = _docker_info(self.docker_image, self.worktree)
                self._docker_ts = now
            self._update_turn_separators()
            self._update_agent_log()
            self._update_sim_log()
            self._update_activity_log()
            self._update_chrome()

        @staticmethod
        def _at_bottom(widget: RichLog) -> bool:
            return widget.scroll_y >= widget.max_scroll_y

        def _update_agent_log(self) -> None:
            events = _read_jsonl(self.paths["raw_export"])
            widget = self.query_one("#agent-log", RichLog)
            at_bottom = self._at_bottom(widget)
            if not events and not self._showed_initial_prompt:
                ip = self.paths["initial_prompt"]
                if ip.exists():
                    try:
                        text = ip.read_text(encoding="utf-8")
                    except OSError:
                        text = ""
                    widget.write(Text("── initial prompt (sent to agent) ──", style="bold dim"))
                    widget.write(Text(text))
                    widget.write(
                        Text(
                            f"[initial prompt · {len(text):,} chars · awaiting first event]",
                            style="dim",
                        )
                    )
                    self._showed_initial_prompt = True
                return
            for e in events[self._agent_idx :]:
                widget.write(_render_event(e))
            self._agent_idx = len(events)
            if at_bottom:
                widget.scroll_end(animate=False)

        def _update_sim_log(self) -> None:
            events = _read_jsonl(self.paths["sim_raw_export"])
            widget = self.query_one("#sim-log", RichLog)
            at_bottom = self._at_bottom(widget)
            for e in events[self._sim_idx :]:
                widget.write(_render_event(e))
            self._sim_idx = len(events)
            if at_bottom:
                widget.scroll_end(animate=False)

        def _update_turn_separators(self) -> None:
            # Drive handover separators off `opencode_actor_start` in
            # guardrail_events. Runs BEFORE the per-pane log updates so
            # the separator lands between turn N and turn N+1: the
            # orchestrator guarantees all of turn N's events are in
            # raw_export before writing agent_start[N+1], so the previous
            # tick already flushed them; the separator then precedes any
            # turn N+1 events written in this tick.
            guardrails = _read_jsonl(self.paths["guardrail_events"])
            agent_starts = [
                e for e in guardrails
                if e.get("event") == "opencode_actor_start" and e.get("role") == "agent"
            ]
            sim_starts = [
                e for e in guardrails
                if e.get("event") == "opencode_actor_start"
                and e.get("role") in ("sim", "review")
            ]
            agent_widget = self.query_one("#agent-log", RichLog)
            sim_widget = self.query_one("#sim-log", RichLog)
            agent_at_bottom = self._at_bottom(agent_widget)
            sim_at_bottom = self._at_bottom(sim_widget)
            while self._agent_separators_rendered < len(agent_starts) - 1:
                n = self._agent_separators_rendered + 1
                role = agent_starts[n - 1].get("role", "agent")
                agent_widget.write(_turn_separator(n, _role_label(role)))
                self._agent_separators_rendered += 1
            while self._sim_separators_rendered < len(sim_starts) - 1:
                n = self._sim_separators_rendered + 1
                role = sim_starts[n - 1].get("role", "sim")
                sim_widget.write(_turn_separator(n, _role_label(role)))
                self._sim_separators_rendered += 1
            if agent_at_bottom:
                agent_widget.scroll_end(animate=False)
            if sim_at_bottom:
                sim_widget.scroll_end(animate=False)

        def _update_activity_log(self) -> None:
            widget = self.query_one("#activity-log", RichLog)
            at_bottom = self._at_bottom(widget)
            guardrails = _read_jsonl(self.paths["guardrail_events"])
            for e in guardrails[self._guardrail_idx :]:
                widget.write(_render_guardrail(e))
            self._guardrail_idx = len(guardrails)
            recoveries = _read_jsonl(self.paths["recoveries"])
            for e in recoveries[self._recoveries_idx :]:
                widget.write(_render_guardrail(e))
            self._recoveries_idx = len(recoveries)
            if at_bottom:
                widget.scroll_end(animate=False)

        def _determine_active(self) -> str | None:
            ag = self._docker_state.get("agent_container")
            sm = self._docker_state.get("sim_container")
            if ag:
                return "agent"
            if sm:
                return "sim"
            a = _file_age(self.paths["raw_export"])
            s = _file_age(self.paths["sim_raw_export"])
            if a is None and s is None:
                return None
            if s is None:
                return "agent"
            if a is None:
                return "sim"
            return "agent" if a <= s else "sim"

        def _update_chrome(self) -> None:
            agent_events = _read_jsonl(self.paths["raw_export"])
            sim_events = _read_jsonl(self.paths["sim_raw_export"])
            recoveries = _read_jsonl(self.paths["recoveries"])
            guardrails = _read_jsonl(self.paths["guardrail_events"])

            agent_turns = _text_event_count(agent_events)
            sim_turns = _text_event_count(sim_events)
            settled = _settled_in(agent_events)
            impl_complete = _impl_complete_in(agent_events)
            subagents = _task_count(agent_events)

            # Freeze elapsed + activity-age once the orchestrator has
            # written stats.json (terminal state) so a finished run stops
            # looking like it's still ticking.
            stats_path = self.paths["stats"]
            terminal = stats_path.exists()
            if terminal and self._frozen_elapsed is None:
                self._frozen_elapsed = time.time() - self.t_start
                gr_age_at_freeze = _file_age(self.paths["guardrail_events"])
                self._frozen_gr_age = gr_age_at_freeze if gr_age_at_freeze is not None else 0.0
            elapsed = (
                self._frozen_elapsed if terminal and self._frozen_elapsed is not None else (time.time() - self.t_start)
            )

            # ----- Header -----
            img = self._docker_state.get("image_created")
            header = Text()
            header.append("contremaitre · ", style="bold cyan")
            header.append(self.run_dir.name)
            header.append(
                f"  ·  agent={_short_model(self.agent_model)}  sim={_short_model(self.sim_model)}",
                style="dim",
            )
            if img:
                header.append(f"  ·  {self.docker_image} built {img}", style="dim")
            self.query_one("#header", Static).update(header)

            # ----- Pane subheaders with thinking loader -----
            ag = self._docker_state.get("agent_container")
            sm = self._docker_state.get("sim_container")
            agent_state = _activity_state(
                container_present=bool(ag),
                file_age=_file_age(self.paths["raw_export"]),
            )
            sim_state = _activity_state(
                container_present=bool(sm),
                file_age=_file_age(self.paths["sim_raw_export"]),
            )
            # Tick the spinner only while at least one pane is non-idle;
            # otherwise a finished run would keep visually animating
            # forever, which contradicts the elapsed-freeze policy below.
            if agent_state != "idle" or sim_state != "idle":
                self._spin_tick = (self._spin_tick + 1) % len(_SPINNER_FRAMES)
            spinner = _SPINNER_FRAMES[self._spin_tick]

            self.query_one("#agent-sub", Static).update(
                _render_pane_subheader(
                    state=agent_state,
                    spinner=spinner,
                    turns=agent_turns,
                    pending_tool=_latest_pending_tool(agent_events),
                    container_id=ag["id"] if ag else None,
                    container_uptime=ag["uptime"] if ag else None,
                )
            )
            self.query_one("#sim-sub", Static).update(
                _render_pane_subheader(
                    state=sim_state,
                    spinner=spinner,
                    turns=sim_turns,
                    pending_tool=_latest_pending_tool(sim_events),
                    container_id=sm["id"] if sm else None,
                    container_uptime=sm["uptime"] if sm else None,
                )
            )

            # ----- Pane titles + active highlight -----
            agent_pane = self.query_one("#agent-pane")
            sim_pane = self.query_one("#sim-pane")
            sim_starts = [
                e for e in guardrails
                if e.get("event") == "opencode_actor_start" and e.get("role") in ("sim", "review")
            ]
            sim_label = "Reviewer" if (sim_starts and sim_starts[-1].get("role") == "review") else "SIM"
            agent_pane.border_title = f"Agent ({_short_model(self.agent_model)})"
            sim_pane.border_title = f"{sim_label} ({_short_model(self.sim_model)})"
            active = self._determine_active()
            agent_pane.set_class(active == "agent", "active")
            sim_pane.set_class(active == "sim", "active")

            # ----- Activity panel title -----
            if terminal and self._frozen_gr_age is not None:
                gr_age: float | None = self._frozen_gr_age
            else:
                gr_age = _file_age(self.paths["guardrail_events"])
            age_str = f" (last write {_fmt_elapsed(gr_age)} ago)" if gr_age is not None else ""
            self.query_one("#activity-panel").border_title = f"orchestrator activity{age_str}"

            # ----- Footer -----
            # Read stats once; reused for verdict text, color, and breadcrumb
            # failure flag. Stats is written before the process exits so it is
            # available for both the proc=None (attach) and rc!=0 (run) paths.
            stats_data: dict = {}
            if terminal:
                try:
                    stats_data = json.loads(stats_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass

            run_failed = stats_data.get("terminal_state") in ("NO_PR", "FAILED")

            rc = self.proc.poll() if self.proc else None
            if self.proc is None:
                if stats_data:
                    status = f"{stats_data.get('terminal_state', '?')} · {stats_data.get('verdict', '?')}"
                    verdict_style = (
                        f"bold {_PAL_SUCCESS}" if stats_data.get("verdict") == "READY_FOR_DRAFT_PR"
                        else f"bold {_PAL_ERROR}" if run_failed
                        else f"bold {_PAL_WARN}"
                    )
                else:
                    status = "attached"
                    verdict_style = _PAL_TEXT
            elif rc is None:
                status = "running"
                verdict_style = f"bold {_PAL_BRIGHT}"
            elif rc == 0:
                status = "exited 0"
                verdict_style = f"bold {_PAL_SUCCESS}"
            else:
                if stats_data:
                    status = f"{stats_data.get('terminal_state', '?')} · {stats_data.get('verdict', '?')}"
                else:
                    status = f"exited {rc}"
                verdict_style = f"bold {_PAL_ERROR}"

            crumb = _state_breadcrumb(
                guardrails,
                terminal_stats=stats_path if terminal else None,
                failed=run_failed,
            )
            sep = Text(" │ ", style=_PAL_VDIM)

            footer = Text()

            # ----- Zone 1: pipeline breadcrumb -----
            footer.append(crumb)
            footer.append(sep)

            # ----- Zone 2: gates & quality (achievements vs not-yet) -----
            footer.append("● " if settled else "○ ", style=_PAL_SUCCESS if settled else _PAL_VDIM)
            footer.append("settled", style=_PAL_TEXT if settled else _PAL_DIM)
            footer.append("  ")
            footer.append("● " if impl_complete else "○ ", style=_PAL_SUCCESS if impl_complete else _PAL_VDIM)
            footer.append("impl", style=_PAL_TEXT if impl_complete else _PAL_DIM)
            review_cycles = _read_jsonl(self.paths["review_cycles"])
            rev_text = _review_summary(review_cycles)
            if rev_text is not None:
                footer.append("  ")
                footer.append(rev_text)
            test_runs = _read_jsonl(self.paths["test_runs"])
            tests_text = _tests_summary(test_runs)
            if tests_text is not None:
                footer.append("  ")
                footer.append(tests_text)
            footer.append(sep)

            # ----- Zone 3: work metrics (dim by default, color only when anomalous) -----
            footer.append(f"A{agent_turns} S{sim_turns}", style=_PAL_TEXT)
            footer.append("  ")
            footer.append(f"sub {subagents}", style=_PAL_DIM)
            footer.append("  ")
            rec_count = len(recoveries)
            footer.append(f"↻{rec_count}", style=_PAL_WARN if rec_count else _PAL_DIM)
            footer.append("  ")
            footer.append(_fmt_elapsed(elapsed), style=_PAL_DIM)
            footer.append("  ")
            if _is_free_model(self.agent_model) and _is_free_model(self.sim_model):
                footer.append("free (◕‿◕)", style=_PAL_SUCCESS)
            else:
                cost_usd = sum_costs_in_events(agent_events, sim_events)
                footer.append(f"${cost_usd:.4f}", style=_PAL_TEXT if cost_usd > 0 else _PAL_DIM)
            footer.append(sep)

            # ----- Zone 4: process verdict (rightmost, biggest contrast) -----
            footer.append(status, style=verdict_style)

            viewer = self.run_dir / "viewer.html"
            if terminal and viewer.exists():
                footer.append("  ")
                footer.append(
                    "viewer ↗",
                    style=f"{_PAL_ACCENT} link file://{viewer.resolve()}",
                )

            self.query_one("#footer-line", Static).update(footer)

        def action_quit(self) -> None:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            self.exit(self.proc.returncode if self.proc else 0)


# ---------- Entry points called from cli.py ----------


def _require_textual() -> None:
    if not _TEXTUAL_AVAILABLE:
        raise SystemExit("contremaitre tui requires textual.\n" "Install with: python3 -m pip install --user textual")


def attach(run_dir: Path, *, refresh_hz: float = 5.0) -> int:
    """Mount the TUI on an existing run directory (read-only)."""

    _require_textual()
    run_dir = run_dir.resolve()
    if not run_dir.exists():
        raise SystemExit(f"run dir does not exist: {run_dir}")
    agent_model, sim_model, docker_image = _read_run_models(run_dir)
    app = ContremaitreTUI(
        run_dir,
        agent_model=agent_model,
        sim_model=sim_model,
        docker_image=docker_image,
        proc=None,
        refresh_hz=refresh_hz,
    )
    return app.run() or 0


def spawn_and_attach(
    runs_root: Path,
    run_slug: str,
    run_cmd: list[str],
    *,
    refresh_hz: float = 5.0,
    discover_timeout_s: float = 30.0,
    agent_model: str = "?",
    sim_model: str = "?",
    docker_image: str = "?",
) -> int:
    """Spawn `contremaitre run …` and attach the TUI to its run dir."""

    _require_textual()
    runs_root = runs_root.resolve()
    runs_root.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in runs_root.glob(f"*-{run_slug}") if p.is_dir()}

    # Spawn the orchestrator detached from our stdout so the TUI owns the
    # terminal. Its output (initial prompt etc.) goes to a sidecar log
    # under the run dir once it exists; we capture it transiently here.
    proc = subprocess.Popen(
        run_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    deadline = time.time() + discover_timeout_s
    run_dir: Path | None = None
    while time.time() < deadline:
        candidates = [p for p in runs_root.glob(f"*-{run_slug}") if p.is_dir()]
        new = [p for p in candidates if p.name not in before]
        if new:
            run_dir = max(new, key=lambda p: p.stat().st_mtime)
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    if run_dir is None:
        # Orchestrator died before creating its run dir, or took too long.
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        raise SystemExit(
            f"orchestrator did not create a run dir under {runs_root} matching "
            f"*-{run_slug} within {discover_timeout_s}s\n"
            f"stderr: {stderr[:1000]}"
        )

    app = ContremaitreTUI(
        run_dir,
        agent_model=agent_model,
        sim_model=sim_model,
        docker_image=docker_image,
        proc=proc,
        refresh_hz=refresh_hz,
    )
    return app.run() or 0


def _read_run_models(run_dir: Path) -> tuple[str, str, str]:
    stats = run_dir / "stats.json"
    if stats.exists():
        try:
            d = json.loads(stats.read_text(encoding="utf-8"))
            return (d.get("agent_model", "?"), d.get("sim_model", "?"), "?")
        except (OSError, json.JSONDecodeError):
            pass
    return ("?", "?", "?")
