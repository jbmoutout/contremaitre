"""Live TUI for Contremaitre runs.

Layout:
    header    run id, agent + SIM models, docker image
    panes     Agent (left) | SIM (right) — RichLog widgets with scrollback
    log       guardrail_events.jsonl + recoveries.jsonl tail (RichLog)
    footer    turn count, SETTLED + IMPL_COMPLETE flags, subagents,
              recoveries count, elapsed, status

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


def _state_breadcrumb(guardrails: list[dict[str, Any]], *, terminal_stats: Path | None) -> Text:
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
        current = "APPROVED" if (terminal_stats and "READY_FOR_DRAFT_PR" in terminal_stats.read_text(encoding="utf-8", errors="replace")) else current

    text = Text()
    for i, stage in enumerate(stages):
        if i > 0:
            text.append(" › ", style="dim")
        is_current = stage == current and blocked_or_failed is None
        if is_current:
            text.append(stage, style="bold cyan")
        elif stages.index(current) > i and blocked_or_failed is None:
            text.append(stage, style="green")
        else:
            text.append(stage, style="dim")
    if blocked_or_failed:
        text.append("  →  ", style="dim")
        text.append(blocked_or_failed, style="bold red")
    return text


# ---------- Event introspection ----------


def _text_event_count(events: list[dict[str, Any]]) -> int:
    return sum(1 for e in events if e.get("type") == "text")


def _task_count(events: list[dict[str, Any]]) -> int:
    return sum(
        1
        for e in events
        if e.get("type") == "tool_use" and (e.get("part") or {}).get("tool") == "task"
    )


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
                "docker", "ps", "--no-trunc",
                "--format", "{{.ID}}\t{{.Mounts}}\t{{.RunningFor}}",
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
            ["docker", "inspect", cid, "--format",
             "{{range .Mounts}}{{.Source}}|{{.Mode}};{{end}}"],
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
    "read":        "#6B8AFF",
    "grep":        "#6B8AFF",
    "glob":        "#6B8AFF",
    "edit":        "#FFB830",
    "write":       "#FFB830",
    "apply_patch": "#FFB830",
    "bash":        "#4ADE80",
    "task":        "#C792EA",
    "skill":       "#FF3B3B",
    "todowrite":   "#888888",
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
        return ("", ts, Text("step_finish", style="dim"), "",
                Text(" ".join(bits), style="dim"))

    if t == "tool_use":
        tool = p.get("tool") or "?"
        state = p.get("state") or {}
        inp = state.get("input") or {}
        body = _tool_body(tool, inp, state)
        return ("", ts, Text("tool_use", style="dim"),
                Text(tool, style=f"bold {_tool_style(tool)}"), body)

    if t == "text":
        txt = p.get("text") or ""
        body = Text()
        body.append(f"{len(txt):,} chars\n", style="dim")
        body.append(txt)
        marker_style = "magenta" if event.get("_recovered_from_sqlite") else "blue"
        return (Text("▍", style=marker_style), ts, Text("text", style="bold"),
                "", body)

    if t == "error":
        err = p.get("error") or event.get("error") or {}
        if isinstance(err, dict):
            data = err.get("data") or {}
            msg = data.get("message") or err.get("name") or str(err)[:200]
        else:
            msg = str(err)[:200]
        return (Text("▍", style="red"), ts, Text("error", style="bold red"),
                "", Text(msg, style="red"))

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
    """Visual break between turns within a pane's RichLog.

    Each opencode invocation ends with exactly one `text` event (the
    model's final reply); the next event in the file is the first step
    of the next turn. Rendering a separator right after each text event
    gives the operator a clear "the conversation just came back" cue
    instead of an undifferentiated stream of step_start/tool_use rows.
    """

    label = f"turn {turn_number} · {role} ✓"
    body = "── " + label + " " + "─" * max(0, 70 - len(label) - 4)
    sep = Text()
    sep.append(body, style="bold cyan")
    return sep


def _render_guardrail(event: dict[str, Any]):
    """Render a guardrail_events.jsonl or recoveries.jsonl line."""

    ts_iso = event.get("ts", "")
    ts = ts_iso[11:19] if len(ts_iso) > 19 else " " * 8
    kind = event.get("event") or event.get("kind") or "?"
    style = "dim"
    if "infra_failure" in kind or "blocked" in kind or "malformed" in kind:
        style = "bold red"
    elif "recovery" in kind or "orphan" in kind or "sqlite" in kind:
        style = "bold yellow"
    elif "work_session_end" in kind or "implementation_complete" in kind:
        style = "bold green"
    body = Text()
    body.append(f"{ts} ", style="dim")
    body.append(kind, style=style)
    # Append a few interesting fields for context.
    for field in ("role", "outcome", "round", "recovered_chars", "container_ids"):
        if field in event:
            body.append(f"  {field}={event[field]}", style="dim")
    if event.get("error"):
        body.append(f"  error={event['error'][:120]}", style="red")
    return body


# ---------- Textual app ----------


if _TEXTUAL_AVAILABLE:

    class ContremaitreTUI(App):
        CSS = """
        Screen { layout: vertical; }
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
            # Per-pane turn counters for the inter-turn separator. One
            # `text` event ends one opencode invocation, so these tick
            # exactly once per turn and the label stays in sync with the
            # footer's `turns A:N S:N`.
            self._agent_turn_separators = 0
            self._sim_turn_separators = 0

        @property
        def paths(self) -> dict[str, Path]:
            return {
                "raw_export": self.run_dir / "raw_export.jsonl",
                "sim_raw_export": self.run_dir / "sim_raw_export.jsonl",
                "guardrail_events": self.run_dir / "guardrail_events.jsonl",
                "recoveries": self.run_dir / "recoveries.jsonl",
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
                    yield Static("", classes="pane-sub", id="agent-sub")
                    yield RichLog(id="agent-log", auto_scroll=True, markup=False,
                                  wrap=True, highlight=False)
                with Vertical(classes="pane", id="sim-pane"):
                    yield Static("", classes="pane-sub", id="sim-sub")
                    yield RichLog(id="sim-log", auto_scroll=True, markup=False,
                                  wrap=True, highlight=False)
            with Vertical(id="activity-panel"):
                yield RichLog(id="activity-log", auto_scroll=True, markup=False,
                              wrap=True, highlight=False)
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
            self._update_agent_log()
            self._update_sim_log()
            self._update_activity_log()
            self._update_chrome()

        def _update_agent_log(self) -> None:
            events = _read_jsonl(self.paths["raw_export"])
            widget = self.query_one("#agent-log", RichLog)
            if not events and not self._showed_initial_prompt:
                ip = self.paths["initial_prompt"]
                if ip.exists():
                    try:
                        text = ip.read_text(encoding="utf-8")
                    except OSError:
                        text = ""
                    widget.write(Text("── initial prompt (sent to agent) ──", style="bold dim"))
                    widget.write(Text(text))
                    widget.write(Text(
                        f"[initial prompt · {len(text):,} chars · awaiting first event]",
                        style="dim",
                    ))
                    self._showed_initial_prompt = True
                return
            for e in events[self._agent_idx:]:
                widget.write(_render_event(e))
                if e.get("type") == "text":
                    self._agent_turn_separators += 1
                    widget.write(_turn_separator(self._agent_turn_separators, "agent"))
            self._agent_idx = len(events)

        def _update_sim_log(self) -> None:
            events = _read_jsonl(self.paths["sim_raw_export"])
            widget = self.query_one("#sim-log", RichLog)
            for e in events[self._sim_idx:]:
                widget.write(_render_event(e))
                if e.get("type") == "text":
                    self._sim_turn_separators += 1
                    widget.write(_turn_separator(self._sim_turn_separators, "sim"))
            self._sim_idx = len(events)

        def _update_activity_log(self) -> None:
            widget = self.query_one("#activity-log", RichLog)
            guardrails = _read_jsonl(self.paths["guardrail_events"])
            for e in guardrails[self._guardrail_idx:]:
                widget.write(_render_guardrail(e))
            self._guardrail_idx = len(guardrails)
            recoveries = _read_jsonl(self.paths["recoveries"])
            for e in recoveries[self._recoveries_idx:]:
                widget.write(_render_guardrail(e))
            self._recoveries_idx = len(recoveries)

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
            elapsed = self._frozen_elapsed if terminal and self._frozen_elapsed is not None else (time.time() - self.t_start)

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
            agent_pane.border_title = f"Agent ({_short_model(self.agent_model)})"
            sim_pane.border_title = f"SIM ({_short_model(self.sim_model)})"
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
            rc = self.proc.poll() if self.proc else None
            if self.proc is None:
                stats = self.paths["stats"]
                if stats.exists():
                    try:
                        d = json.loads(stats.read_text(encoding="utf-8"))
                        status = f"{d.get('terminal_state','?')} · {d.get('verdict','?')}"
                        style = "bold green" if d.get("verdict") == "READY_FOR_DRAFT_PR" else "bold yellow"
                    except (OSError, json.JSONDecodeError):
                        status = "attached"
                        style = "cyan"
                else:
                    status = "attached"
                    style = "cyan"
            elif rc is None:
                status = "running"
                style = "green"
            elif rc == 0:
                status = "exited 0"
                style = "bold green"
            else:
                status = f"exited {rc}"
                style = "bold red"

            crumb = _state_breadcrumb(guardrails, terminal_stats=stats_path if terminal else None)

            footer = Text()
            footer.append(crumb)
            footer.append("   ")
            footer.append(f"turns A:{agent_turns} S:{sim_turns}")
            footer.append(" · ")
            footer.append("SETTLED" if settled else "no settled", style="bold green" if settled else "dim")
            footer.append(" · ")
            footer.append("IMPL_COMPLETE" if impl_complete else "no impl_complete",
                          style="bold green" if impl_complete else "dim")
            footer.append(" · ")
            footer.append(f"subagents: {subagents}")
            footer.append(" · ")
            footer.append(f"recoveries: {len(recoveries)}",
                          style="yellow" if recoveries else "dim")
            footer.append(" · ")
            footer.append(f"elapsed {_fmt_elapsed(elapsed)}")
            footer.append(" · ")
            footer.append(status, style=style)
            footer.append("  ·  Ctrl-C ", style="dim")
            footer.append("kills" if self.proc else "exits", style="dim")
            footer.append("  ·  ↑↓/PgUp/PgDn scroll", style="dim")
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
        raise SystemExit(
            "contremaitre tui requires textual.\n"
            "Install with: python3 -m pip install --user textual"
        )


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
