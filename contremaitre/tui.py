"""Live TUI for Contremaitre runs.

Layout:
    header    run id, agent + SIM models, docker image
    panes     Agent (left) | SIM (right) — RichLog widgets with scrollback
    log       guardrail_events.jsonl + recoveries.jsonl tail (RichLog)
    footer    one line, info-hierarchy ordered left→right:
              [trail]  [phase label + sub-info]  [↶ R<N> changes_req]?
                [warnings]?  [elapsed · cost]  [verdict]
              Trail = 6 dots (Init → Exploring → Grilling → Implementing → Reviewing → Done).
              Warning tokens hidden when quiet.
    links     2nd-row plain-URL line (PR + viewer) shown only at terminal state.
              Plain URLs, not OSC 8 — Apple Terminal doesn't support OSC 8 but
              detects plain URL text for cmd+click in every modern terminal.

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
import threading
import time
from pathlib import Path
from typing import Any

from .cli_reviewer import expand_choice as _cli_reviewer_expand_choice
from .costs import sum_costs_in_events
from .extract import parse_apply_patch
from .models import ModelSpec, resolved_model_from_events

# Unknown identity placeholder for runs whose model dicts can't be read yet.
_UNKNOWN_SPEC = ModelSpec(runtime="opencode", requested="?")

try:
    from rich.table import Table
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Link, RichLog, Static

    _TEXTUAL_AVAILABLE = True
except ImportError:  # pragma: no cover — gated at CLI entry point
    _TEXTUAL_AVAILABLE = False


SETTLED_FILE_RE = re.compile(r"/SETTLED_DESIGN\.md$", re.IGNORECASE)
IMPL_COMPLETE_FILE_RE = re.compile(r"/IMPLEMENTATION_COMPLETE$")
ARCH_REVIEW_FILE_RE = re.compile(r"/architecture-review\.html?$", re.IGNORECASE)
APPLY_PATCH_SETTLED_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update)\s+File:\s*.*SETTLED_DESIGN\.md\s*$",
    re.IGNORECASE | re.MULTILINE,
)
APPLY_PATCH_IMPL_COMPLETE_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update)\s+File:\s*.*IMPLEMENTATION_COMPLETE\s*$",
    re.IGNORECASE | re.MULTILINE,
)
APPLY_PATCH_ARCH_REVIEW_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update)\s+File:\s*.*architecture-review\.html?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
DOCKER_REFRESH_S = 2.0
# codex writes its rate-limit snapshot into session-rollout files (not the
# `--json` stream we tail). Re-reading them every chrome tick (5Hz) is wasteful
# — the quota moves once per turn — so cache the parse for a few seconds, and
# tail only the rollout's tail to find the latest `token_count` event.
CODEX_USAGE_REFRESH_S = 3.0
_CODEX_USAGE_TAIL_BYTES = 256 * 1024

# Sentinel for the "no prior active pane recorded yet" state on the very
# first tick — distinct from `None` (a legitimate active-pane value meaning
# "every pane is idle right now") so the wink doesn't fire spuriously on
# the initial render.
_UNSET_ACTIVE = object()


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


def _short_repo(url_or_path: str | None) -> str:
    """Normalize a git URL or local path to `owner/repo` for display.

    Handles:
      git@github.com:owner/repo.git    → owner/repo
      https://github.com/owner/repo.git → owner/repo
      https://github.com/owner/repo    → owner/repo
      /Users/jb/code/repo              → repo  (basename fallback)

    Empty / unknown → `?` so the header line never collapses.

    Local paths use the basename only — extracting two path segments
    (e.g. `code/repo`) would falsely imply a GitHub-style owner.
    """

    if not url_or_path:
        return "?"
    s = str(url_or_path).strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    # `owner/repo` match only when the input is clearly a URL — SSH
    # (`user@host:…`) or any `scheme://…`. Without this guard, local
    # paths like `/Users/jb/code/repo` would yield `code/repo`, which
    # reads as a GitHub slug but isn't.
    looks_like_url = "://" in s or bool(re.match(r"^[^/\s]+@[^/\s]+:", s))
    if looks_like_url:
        m = re.search(r"[:/]([^/:]+/[^/:]+)$", s)
        if m:
            return m.group(1)
    # Plain path / single name — basename fallback.
    return s.rsplit("/", 1)[-1] or "?"


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


def _claude_family(name: str | None) -> str | None:
    """Claude footer label family (`opus`/`sonnet`/`haiku`) from a plain model
    name or id, or None. Operates on a name, never a role label — identity
    parsing lives in `ModelSpec`."""

    if not name:
        return None
    lowered = name.lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in lowered:
            return family
    return None


def _ts_to_ms(ts: object) -> int | None:
    """Coerce a heterogeneous event timestamp to epoch-ms, or None.

    Events arrive with int/float ms (opencode + our codex back-fill), a numeric
    string, or an ISO-8601 string (claude emits e.g. `2026-06-06T12:49:25.658Z`
    on user/assistant/result events). Anything unparseable → None (blank column).
    """

    if isinstance(ts, bool):  # bool is an int subclass; never a timestamp
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _fmt_ts(ts_ms: object) -> str:
    ms = _ts_to_ms(ts_ms)
    if not ms:
        return "        "
    try:
        return time.strftime("%H:%M:%S", time.localtime(ms / 1000))
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


_SPINNER_FRAMES = (" ⠋", " ⠙", " ⠹", " ⠸", " ⠼", " ⠴", " ⠦", " ⠧", " ⠇", " ⠏")


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
        sub.append(spinner, style=f"bold {_PAL_SUCCESS}")
        sub.append(" streaming", style=_PAL_SUCCESS)
    elif state == "thinking":
        sub.append(spinner, style="bold cyan")
        sub.append(" thinking…", style="cyan")
    else:
        sub.append("•", style="dim")
        sub.append(" idle", style="dim")
    sub.append(f" · turns {turns}", style="dim")
    if pending_tool:
        clipped = pending_tool if len(pending_tool) <= 60 else pending_tool[:57] + "…"
        sub.append(f" · {clipped}", style="dim")
    if container_id:
        sub.append(f" · {container_id} ({container_uptime})", style="dim")
    return sub


# ---------- Phase trail + footer renderers ----------
#
# The footer is one line organised as five zones:
#   [trail]  [phase label + sub-info]   [persistent review token + warnings]   [resources]   [verdict]
#
# Trail = 5 dots tracking the five canonical phases from docs/control-plane.md:
#   Init → Grilling → Implementing → Reviewing → Done.
# Past dots = green ●; current = bright ◐; future = dim ○. At terminal state
# the current dot's colour reflects the TerminalVerdict (green / yellow / red).
#
# A second-line widget (`#links-line`) renders plain URLs (PR + viewer) at
# terminal state — plain text so any terminal's URL-detection (Apple Terminal
# included) makes them cmd+clickable. OSC 8 hyperlinks are deliberately not
# used because Apple Terminal doesn't support them.

_PHASES = ("init", "exploring", "grilling", "implementing", "reviewing", "cli_review", "done")


def _derive_phase(
    *,
    terminal: bool,
    terminal_verdict: str | None,
    settled: bool,
    impl_complete: bool,
    agent_started: bool,
    architecture_review_done: bool = False,
    sim_started: bool = False,
    review_started: bool = False,
    cli_review_started: bool = False,
    cli_review_completed: bool = False,
    cli_review_failed: bool = False,
) -> tuple[str, str]:
    """Return `(phase, color_state)`.

    Phases: `init → exploring → grilling → implementing → reviewing → done`.

    Exploring → Grilling fires on EITHER `architecture-review.html` being
    written OR a WORK-SIM turn starting (whichever first). The OR fallback
    handles agents that skip writing the cards file.

    Implementing → Reviewing fires on the `role=review` actor start, NOT
    on `IMPLEMENTATION_COMPLETE` being written. Between the agent writing
    the marker and the orchestrator starting the review container, hard
    gates run — that brief window stays in `implementing` (the label
    surfaces "awaiting review" so it's clear the agent is done).

    `color_state` ∈ {"live", "ok", "warn", "error"} controls the current dot
    colour and reflects how the run is doing at the current phase:
      - live  — running; current dot bold-bright white
      - ok    — terminal success (READY_FOR_DRAFT_PR); current dot green
      - warn  — terminal PR_NEEDS_HUMAN / NO_PR_*; current dot yellow
      - error — terminal FAILED_INFRA; current dot red, trail frozen at
                the last active phase (not advanced to "done") so the
                operator can see where the run died.
    """

    grilling_reached = architecture_review_done or sim_started

    if terminal:
        if terminal_verdict == "READY_FOR_DRAFT_PR":
            # If the CLI reviewer ran but didn't finish cleanly, surface the
            # partial state — `done warn` rather than `done ok` so the
            # operator can see the comment didn't post even though the PR did.
            if cli_review_started and not cli_review_completed:
                return ("done", "warn") if cli_review_failed else ("done", "ok")
            return ("done", "ok")
        if terminal_verdict in (
            "PR_NEEDS_HUMAN",
            "NO_PR_CHANGES_REQUESTED",
            "NO_PR_NEEDS_HUMAN",
        ):
            return ("done", "warn")
        # FAILED_INFRA (or unknown) — freeze trail at last active phase.
        if cli_review_started:
            return ("cli_review", "error")
        if review_started:
            return ("reviewing", "error")
        if settled:
            return ("implementing", "error")
        if grilling_reached:
            return ("grilling", "error")
        if agent_started:
            return ("exploring", "error")
        return ("init", "error")
    # Running.
    if cli_review_started and not cli_review_completed:
        return ("cli_review", "warn" if cli_review_failed else "live")
    if review_started:
        return ("reviewing", "live")
    if settled:
        return ("implementing", "live")
    if grilling_reached:
        return ("grilling", "live")
    if agent_started:
        return ("exploring", "live")
    return ("init", "live")


def _phase_trail(phase: str, color_state: str) -> Text:
    """Render the trail with the current phase highlighted.

    All dots use the `●` / `○` pair (filled / hollow) — no half-fill `◐`,
    because most monospace fonts render `◐` at a different baseline than
    `●`, which made the trail look visually misaligned. Distinction
    between past / current / future is carried by colour and bold weight.
    """

    cur_idx = _PHASES.index(phase) if phase in _PHASES else 0
    text = Text()
    for i in range(len(_PHASES)):
        if i > 0:
            text.append("─", style=_PAL_VDIM)
        if i < cur_idx:
            text.append("●", style=_PAL_SUCCESS)
        elif i == cur_idx:
            if color_state == "ok":
                text.append("●", style=f"bold {_PAL_SUCCESS}")
            elif color_state == "warn":
                text.append("●", style=f"bold {_PAL_WARN}")
            elif color_state == "error":
                text.append("●", style=f"bold {_PAL_ERROR}")
            else:  # live
                text.append("●", style=f"bold {_PAL_BRIGHT}")
        else:
            text.append("○", style=_PAL_DIM)
    return text


def _verdict_glyph(verdict: str | None) -> tuple[str, str]:
    """(glyph, style) for a review verdict — used in phase label & tokens."""

    v = (verdict or "").upper()
    if v == "APPROVED":
        return "✓", _PAL_SUCCESS
    if v == "CHANGES_REQUESTED":
        return "✗", _PAL_WARN
    if v == "NEEDS_HUMAN":
        return "?", _PAL_WARN
    return "·", _PAL_DIM


def _round_verdict(review_cycles: list[dict[str, Any]], round_n: int) -> str | None:
    """SIM verdict for the given round, or None."""

    return next(
        (
            r.get("verdict")
            for r in review_cycles
            if (r.get("round") or 0) == round_n
            and r.get("reviewer", "sim") == "sim"
            and not r.get("unavailable")
        ),
        None,
    )


def _current_review_round(guardrails: list[dict[str, Any]]) -> int:
    """Round number derived from the count of REVIEW-SIM actor starts."""

    return sum(
        1
        for g in guardrails
        if g.get("event") == "opencode_actor_start" and g.get("role") == "review"
    )


def _reviewer_status(
    *,
    round_n: int,
    review_cycles: list[dict[str, Any]],
    guardrails: list[dict[str, Any]],
) -> str:
    """SIM reviewer status for the given round.

    Returns one of:
      - `idle`         — reviewer hasn't started for this round yet
      - `streaming`    — actor started but no verdict recorded yet
      - `approved`     — verdict APPROVED
      - `changes_req`  — verdict CHANGES_REQUESTED
      - `needs_human`  — verdict NEEDS_HUMAN
      - `unavailable`  — marked unavailable in review_cycles
    """

    for cycle in review_cycles:
        if (cycle.get("round") or 0) != round_n:
            continue
        if cycle.get("reviewer", "sim") != "sim":
            continue
        if cycle.get("unavailable"):
            return "unavailable"
        v = (cycle.get("verdict") or "").upper()
        if v == "APPROVED":
            return "approved"
        if v == "CHANGES_REQUESTED":
            return "changes_req"
        if v == "NEEDS_HUMAN":
            return "needs_human"
        return "streaming"

    matching_starts = [
        g
        for g in guardrails
        if g.get("event") == "opencode_actor_start" and g.get("role") == "review"
    ]
    if len(matching_starts) >= round_n:
        return "streaming"
    return "idle"


def _reviewer_glyph(status: str) -> tuple[str, str]:
    """`(glyph, style)` for a reviewer status (Reviewing phase label)."""

    if status == "approved":
        return "✓", _PAL_SUCCESS
    if status == "changes_req":
        return "✗", _PAL_WARN
    if status == "needs_human":
        return "?", _PAL_WARN
    if status == "streaming":
        return "⏵", f"bold {_PAL_BRIGHT}"
    if status == "unavailable":
        return "—", _PAL_DIM
    return "·", _PAL_DIM


def _cli_review_status_glyph(
    status: str | None,
    verdict: str | None = None,
) -> tuple[str, str]:
    """`(glyph, style)` for the post-publish CLI-review status.

    On `completed`, the glyph reflects the agent's verdict key (MUST_FIX /
    NEEDS_ATTENTION / LOOKS_GOOD from line 1 of the review) — NOT the
    subprocess exit code. The agent can successfully produce a `MUST_FIX`
    review, and the footer needs to show that as `✗`, not `✓`. Falls back
    to ✓ when the completion event didn't carry a verdict (parse miss).
    """

    if status == "completed":
        if verdict == "MUST_FIX":
            return "✗", _PAL_ERROR
        if verdict == "NEEDS_ATTENTION":
            return "!", _PAL_WARN
        return "✓", _PAL_SUCCESS
    if status == "failed":
        return "✗", _PAL_ERROR
    if status == "streaming":
        return "⏵", f"bold {_PAL_BRIGHT}"
    return "·", _PAL_DIM


def _review_status_tail(
    *,
    sim_review_statuses: list[str],
    cli_review_status: str | None = None,
    cli_review_tool: str = "",
    cli_review_verdict: str | None = None,
    cli_review_states: list[tuple[str, str, str | None]] | None = None,
) -> Text:
    """The `Review ✓ ✓  CODEX Review ⏵` footer tail.

    `cli_review_states` (a list of `(tool, status, verdict)`) is the
    multi-tool path — renders one glyph per active tool in execution
    order.
    """

    tail = Text()
    if sim_review_statuses:
        tail.append(" · Review ", style=_PAL_TEXT)
        for i, s in enumerate(sim_review_statuses):
            if i:
                tail.append(" ", style=_PAL_TEXT)
            g, st = _reviewer_glyph(s)
            tail.append(g, style=st)
    states = cli_review_states
    if states is None and cli_review_status is not None:
        states = [(cli_review_tool, cli_review_status, cli_review_verdict)]
    if states:
        for tool, status, verdict in states:
            label = (tool or "CLI").upper()
            tail.append(f"  {label} Review ", style=_PAL_TEXT)
            g, st = _cli_review_status_glyph(status, verdict)
            tail.append(g, style=st)
    return tail


def _current_phase_label(
    *,
    phase: str,
    color_state: str,
    grilling_exchanges: int,
    impl_turns: int,
    self_verified: bool,
    impl_complete: bool,
    review_cycles: list[dict[str, Any]],
    terminal_verdict: str | None,
    pr_number: int | None,
    pr_title: str | None = None,
    current_review_round: int = 0,
    sim_review_statuses: list[str] | None = None,
    cli_review_status: str | None = None,
    cli_review_tool: str = "",
    cli_review_verdict: str | None = None,
    cli_review_states: list[tuple[str, str, str | None]] | None = None,
    provider_failure: dict | None = None,
) -> Text:
    """Phase name + sub-info (right of the trail).

    Terminal labels for non-success states (PR_NEEDS_HUMAN, NO_PR_*,
    FAILED) explain *why* the run ended so the operator doesn't have to
    also read the verdict zone. For READY_FOR_DRAFT_PR the label is just `Done` —
    the verdict zone's bold `PR PUSHED #N` already carries that info,
    so duplicating it here would be noise.
    """

    text = Text()
    if phase == "init":
        text.append("Init", style=f"bold {_PAL_BRIGHT}")
        return text
    if phase == "exploring":
        if color_state == "error":
            text.append(
                _infra_failure_label("Exploring", provider_failure), style=f"bold {_PAL_ERROR}"
            )
            return text
        text.append("Exploring", style=f"bold {_PAL_BRIGHT}")
        return text
    if phase == "grilling":
        if color_state == "error":
            text.append(
                _infra_failure_label("Grilling", provider_failure), style=f"bold {_PAL_ERROR}"
            )
            return text
        text.append("Grilling", style=f"bold {_PAL_BRIGHT}")
        text.append(f" (exchange {grilling_exchanges})", style=_PAL_TEXT)
        return text
    if phase == "implementing":
        if color_state == "error":
            text.append(
                _infra_failure_label("Implementing", provider_failure), style=f"bold {_PAL_ERROR}"
            )
            return text
        text.append("Implementing", style=f"bold {_PAL_BRIGHT}")
        if impl_complete:
            # Agent wrote IMPLEMENTATION_COMPLETE but the review container
            # hasn't started yet (hard gates running). Signal the wait.
            text.append(" — awaiting review", style=_PAL_TEXT)
            return text
        text.append(f" (turn {impl_turns}", style=_PAL_TEXT)
        if self_verified:
            text.append(", tested ", style=_PAL_TEXT)
            text.append("✓", style=_PAL_SUCCESS)
        text.append(")", style=_PAL_TEXT)
        return text
    if phase == "reviewing":
        if color_state == "error":
            text.append(
                _infra_failure_label("Reviewing", provider_failure), style=f"bold {_PAL_ERROR}"
            )
            return text
        text.append("Reviewing", style=f"bold {_PAL_BRIGHT}")
        # Show the round number from the count of actor-starts, not from
        # `review_cycles` (which only lands once the FIRST verdict is
        # recorded — leaving a several-second gap where the round number
        # would be absent).
        if current_review_round > 0:
            text.append(f" (round {current_review_round})", style=_PAL_TEXT)
        text.append(
            _review_status_tail(
                sim_review_statuses=sim_review_statuses or [],
                cli_review_status=cli_review_status,
                cli_review_tool=cli_review_tool,
                cli_review_verdict=cli_review_verdict,
                cli_review_states=cli_review_states,
            )
        )
        return text
    if phase == "cli_review":
        if color_state == "error":
            text.append(
                _infra_failure_label("CLI review", provider_failure), style=f"bold {_PAL_ERROR}"
            )
            return text
        if color_state == "warn":
            text.append("CLI review — failed", style=f"bold {_PAL_WARN}")
        else:
            if cli_review_states and len(cli_review_states) > 1:
                # Multi-tool — name both reviewers in execution order so
                # the operator sees which two are running, not a generic
                # "CLI review".
                tools = " + ".join(t.upper() for t, _, _ in cli_review_states)
                label = f"{tools} review"
            else:
                label = f"{(cli_review_tool or 'CLI').upper()} review"
            text.append(label, style=f"bold {_PAL_BRIGHT}")
            text.append(" (post-publish)", style=_PAL_TEXT)
        text.append(
            _review_status_tail(
                sim_review_statuses=sim_review_statuses or [],
                cli_review_status=cli_review_status,
                cli_review_tool=cli_review_tool,
                cli_review_verdict=cli_review_verdict,
                cli_review_states=cli_review_states,
            )
        )
        return text
    # phase == "done"
    if terminal_verdict == "READY_FOR_DRAFT_PR":
        # Verdict zone carries `PR PUSHED #N` — don't duplicate the
        # number here. Show the PR title for context if available
        # (truncated to keep the footer on one line).
        text.append("Done", style=f"bold {_PAL_SUCCESS}")
        trimmed = _truncate_pr_title(pr_title)
        if trimmed:
            text.append(" · ", style=_PAL_VDIM)
            text.append(trimmed, style=_PAL_TEXT)
        # Keep the gatekeeper + cli-review verdict glyphs visible after
        # the run finishes — they're the most scannable record of "did
        # every reviewer agree" and disappearing them at terminal was
        # losing information right when the operator wants to verify.
        text.append(
            _review_status_tail(
                sim_review_statuses=sim_review_statuses or [],
                cli_review_status=cli_review_status,
                cli_review_tool=cli_review_tool,
                cli_review_verdict=cli_review_verdict,
                cli_review_states=cli_review_states,
            )
        )
    elif terminal_verdict == "PR_NEEDS_HUMAN":
        text.append("Done — PR needs human", style=f"bold {_PAL_WARN}")
        trimmed = _truncate_pr_title(pr_title)
        if trimmed:
            text.append(" · ", style=_PAL_VDIM)
            text.append(trimmed, style=_PAL_TEXT)
        text.append(
            _review_status_tail(
                sim_review_statuses=sim_review_statuses or [],
                cli_review_status=cli_review_status,
                cli_review_tool=cli_review_tool,
                cli_review_verdict=cli_review_verdict,
                cli_review_states=cli_review_states,
            )
        )
    elif terminal_verdict == "NO_PR_CHANGES_REQUESTED":
        text.append("Reviewing — changes_req (exhausted)", style=f"bold {_PAL_WARN}")
    elif terminal_verdict == "NO_PR_NEEDS_HUMAN":
        text.append("Reviewing — needs human", style=f"bold {_PAL_WARN}")
    elif terminal_verdict == "FAILED_INFRA":
        if provider_failure is not None:
            model = _short_quota_model(provider_failure.get("model"))
            if provider_failure.get("kind") == "transient":
                msg = f"Failed — transient provider error ({model})"
            else:
                msg = f"Failed — free-tier quota exhausted ({model})"
            text.append(msg, style=f"bold {_PAL_ERROR}")
        else:
            text.append("Failed — infrastructure error", style=f"bold {_PAL_ERROR}")
    else:
        text.append("Done", style=_PAL_TEXT)
    return text


def _persistent_review_token(
    *,
    phase: str,
    review_cycles: list[dict[str, Any]],
) -> Text | None:
    """`↶ R<N> changes_req` token shown after a CHANGES_REQUESTED loop-back.

    Renders only when we're back in a WORK phase (grilling/implementing)
    AND the latest completed review round's SIM verdict was
    CHANGES_REQUESTED. The token says "we're here *because* of the last
    review" and clears as soon as the next review round opens its own
    review_cycles entry.
    """

    if phase not in ("exploring", "grilling", "implementing"):
        return None
    if not review_cycles:
        return None
    last_round = max((r.get("round") or 0) for r in review_cycles)
    sim_verdict = _round_verdict(review_cycles, last_round)
    if not sim_verdict or sim_verdict.upper() != "CHANGES_REQUESTED":
        return None
    text = Text()
    text.append(f"↶ R{last_round} changes_req", style=f"bold {_PAL_WARN}")
    return text


def _warnings_token(
    *,
    recoveries: list[dict[str, Any]],
    test_runs: list[dict[str, Any]],
) -> Text | None:
    """Compact warning tokens. Returns None when there's nothing to surface."""

    parts: list[Text] = []
    rec_n = len(recoveries)
    if rec_n:
        t = Text()
        t.append(f"↻{rec_n}", style=f"bold {_PAL_WARN}")
        parts.append(t)
    if any(r.get("returncode") not in (0, None) for r in test_runs):
        t = Text()
        t.append("tests ✗", style=f"bold {_PAL_ERROR}")
        parts.append(t)
    if not parts:
        return None
    out = Text()
    for i, p in enumerate(parts):
        if i > 0:
            out.append("  ")
        out.append(p)
    return out


def _terminal_badge(
    verdict: str | None,
    pr_number: int | None,
    provider_failure: dict | None = None,
) -> tuple[str, str]:
    """`(text, style)` for the rightmost verdict badge at terminal state.

    `provider_failure` upgrades the generic `FAILED · infra` badge to the
    more specific `FAILED · quota` so the operator can tell at-a-glance
    that the run died on a free-tier quota rather than e.g. a docker hiccup.
    """

    if verdict == "READY_FOR_DRAFT_PR":
        label = f"PR PUSHED #{pr_number}" if pr_number is not None else "PR PUSHED"
        return label, f"bold {_PAL_SUCCESS}"
    if verdict == "PR_NEEDS_HUMAN":
        label = f"PR NEEDS HUMAN #{pr_number}" if pr_number is not None else "PR NEEDS HUMAN"
        return label, f"bold {_PAL_WARN}"
    if verdict == "NO_PR_CHANGES_REQUESTED":
        return "NO_PR · changes_req", f"bold {_PAL_WARN}"
    if verdict == "NO_PR_NEEDS_HUMAN":
        return "NO_PR · needs human", f"bold {_PAL_WARN}"
    if verdict == "FAILED_INFRA":
        if provider_failure is not None:
            tag = "transient" if provider_failure.get("kind") == "transient" else "quota"
            return f"FAILED · {tag}", f"bold {_PAL_ERROR}"
        return "FAILED · infra", f"bold {_PAL_ERROR}"
    return verdict or "?", f"bold {_PAL_TEXT}"


def _provider_fast_fail(guardrails: list[dict[str, Any]]) -> dict | None:
    """Return the latest provider fast-fail event, or None.

    Covers both quota exhaustion (terminal: retry can't help) and
    transient upstream errors (retried then exhausted). Used to swap
    the generic "infra failure" labels for the more informative
    "free-tier quota exhausted ({model})" / "transient provider error
    ({model})" so the operator can tell which model to swap before
    retrying.
    """

    for event in reversed(guardrails):
        kind = event.get("event")
        if kind == "provider_quota_exhausted":
            return {
                "kind": "quota",
                "role": event.get("role"),
                "model": event.get("model"),
                "marker": event.get("marker"),
            }
        if kind == "provider_transient_error":
            return {
                "kind": "transient",
                "role": event.get("role"),
                "model": event.get("model"),
                "marker": event.get("marker"),
            }
    return None


def _short_quota_model(model: str | None) -> str:
    if not model:
        return "?"
    return model.rsplit("/", 1)[-1]


def _infra_failure_label(phase_name: str, provider_failure: dict | None) -> str:
    """Customize the `<phase> — infra failure` label when we know the cause.

    Provider fast-fail errors get a specific tag so the operator can
    tell "free-tier quota exhausted on {model}" or "transient provider
    error on {model}" from a generic docker hiccup without scanning the
    activity feed.
    """

    if provider_failure is not None:
        model = _short_quota_model(provider_failure.get("model"))
        if provider_failure.get("kind") == "transient":
            return f"{phase_name} — transient provider error ({model})"
        return f"{phase_name} — free-tier quota exhausted ({model})"
    return f"{phase_name} — infra failure"


def _read_pr_json(run_dir: Path) -> dict | None:
    p = run_dir / "pr.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pr_number_from_url(url: str | None) -> int | None:
    """Extract the trailing `/pull/<N>` integer from a GitHub PR URL."""

    if not url:
        return None
    m = re.search(r"/pull/(\d+)(?:[/?#]|$)", url)
    return int(m.group(1)) if m else None


def _truncate_pr_title(title: str | None, limit: int = 60) -> str | None:
    """Trim long PR titles for the `Done · <title>` phase label.

    The footer is one line wide; long titles would push the right segment
    off-screen on narrow terminals. Limit chosen to leave headroom for
    trail + label prefix + right-segment content at ~120 cols.
    """

    if not title:
        return None
    title = title.strip()
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


# ---------- Event introspection ----------


def _text_event_count(events: list[dict[str, Any]]) -> int:
    # opencode emits `text`; the codex CLI actor emits the reply as an
    # `item.completed` agent_message; the claude CLI actor emits one `result`
    # per turn — each counts as one actor turn.
    return sum(
        1
        for e in events
        if e.get("type") in ("text", "result")
        or (
            e.get("type") == "item.completed"
            and isinstance(e.get("item"), dict)
            and e["item"].get("type") == "agent_message"
        )
    )


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
        elif tool == "apply_patch":
            patch = inp.get("patchText") or inp.get("patch") or ""
            if APPLY_PATCH_IMPL_COMPLETE_RE.search(patch):
                return True
    return False


def _architecture_review_in(events: list[dict[str, Any]]) -> bool:
    """True if `.contremaitre/architecture-review.html` has been written.

    The skill's convention is for the agent to publish candidate cards there
    before grilling with SIM. Not a harness gate, but useful as the
    Exploring → Grilling boundary in the footer trail.
    """

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
            if ARCH_REVIEW_FILE_RE.search(fp):
                return True
        elif tool == "apply_patch":
            patch = inp.get("patchText") or inp.get("patch") or ""
            if APPLY_PATCH_ARCH_REVIEW_RE.search(patch):
                return True
    return False


def _compute_phases_for_tui(
    paths: dict[str, Path],
    agent_events: list[dict[str, Any]],
    guardrails: list[dict[str, Any]],
    review_cycles: list[dict[str, Any]],
) -> dict[str, int]:
    """In-TUI mirror of flow_use.compute_phases.

    The TUI ticks ~once a second and already holds the events it needs.
    Re-reading from disk here would just duplicate I/O. Same logic as
    flow_use.compute_phases — anchored to opencode_actor_start + the
    SETTLED / IMPLEMENTATION_COMPLETE write timestamps.
    """
    settled_event = None
    impl_event = None
    for e in agent_events:
        if e.get("type") != "tool_use":
            continue
        part = e.get("part") or {}
        if part.get("tool") not in ("write", "edit", "apply_patch"):
            continue
        if (part.get("state") or {}).get("status") != "completed":
            continue
        inp = (part.get("state") or {}).get("input") or {}
        target = inp.get("filePath") or inp.get("path") or inp.get("patchText") or ""
        if settled_event is None and "SETTLED_DESIGN" in target.upper():
            settled_event = e
        if impl_event is None and "IMPLEMENTATION_COMPLETE" in target:
            impl_event = e

    settled_ms = settled_event.get("timestamp") if settled_event else None
    impl_ms = impl_event.get("timestamp") if impl_event else None

    starts: list[tuple[float, str]] = []
    for g in guardrails:
        if g.get("event") != "opencode_actor_start":
            continue
        ts_str = g.get("ts", "")
        try:
            from datetime import datetime

            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() * 1000
        except (ValueError, AttributeError):
            continue
        role = g.get("role")
        if role in ("agent", "sim", "review"):
            starts.append((ts, role))
    starts.sort()

    impl_start_idx: int | None = None
    if settled_ms is not None:
        for i, (ts, role) in enumerate(starts):
            if role != "agent":
                continue
            next_ts = starts[i + 1][0] if i + 1 < len(starts) else float("inf")
            if ts <= settled_ms < next_ts:
                impl_start_idx = i
                break

    if impl_start_idx is None:
        pre, post = starts, []
    else:
        pre, post = starts[:impl_start_idx], starts[impl_start_idx:]

    pre_agent = sum(1 for _, r in pre if r == "agent")
    pre_sim = sum(1 for _, r in pre if r == "sim")
    impl = sum(1 for ts, r in post if r == "agent" and (impl_ms is None or ts <= impl_ms))

    return {
        "grilling_exchanges": min(pre_agent, pre_sim),
        "impl_turns": impl,
        "review_rounds": len(review_cycles),
    }


def _self_verified_in(events: list[dict[str, Any]]) -> bool:
    """True if agent ran a test command after its last code edit."""
    _test_cmd_re = re.compile(
        r"\bunittest\b|\bpytest\b|\btsc\b|npm\s+test|make\s+test|\bmypy\b|\bjest\b|\bvitest\b"
    )
    _contremaitre_re = re.compile(r"[/\\]?\.contremaitre[/\\]")
    last_edit_ts = 0
    for e in events:
        if e.get("type") != "tool_use":
            continue
        part = e.get("part") or {}
        if part.get("tool") not in ("write", "edit", "apply_patch"):
            continue
        inp = (part.get("state") or {}).get("input") or {}
        paths: list[str]
        if part.get("tool") == "apply_patch":
            patch = inp.get("patchText") or inp.get("patch") or ""
            paths = [fp for _, fp, _ in parse_apply_patch(patch)]
        else:
            fp = inp.get("filePath") or inp.get("path") or ""
            paths = [fp] if fp else []
        if any(not _contremaitre_re.search(fp) for fp in paths):
            last_edit_ts = max(last_edit_ts, e.get("timestamp", 0))
    if not last_edit_ts:
        return False
    for e in events:
        if e.get("type") != "tool_use":
            continue
        if (e.get("part") or {}).get("tool") != "bash":
            continue
        if e.get("timestamp", 0) <= last_edit_ts:
            continue
        cmd = ((e.get("part") or {}).get("state") or {}).get("input", {}).get("command") or ""
        if _test_cmd_re.search(cmd):
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
            [
                "docker",
                "inspect",
                cid,
                "--format",
                "{{range .Mounts}}{{.Source}}|{{.Mode}};{{end}}",
            ],
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


# ---------- codex subscription usage (rate limits) ----------
#
# codex `exec --json` (what we tail into raw_export) carries only per-turn token
# counts — NOT the ChatGPT plan's usage limits. Those land in codex's session
# rollout (`~/.codex/sessions/.../rollout-*.jsonl`) as `event_msg` /
# `token_count` payloads carrying a `rate_limits` block:
#   primary   — the short rolling window (e.g. 5h),  used_percent + resets_at
#   secondary — the long window (e.g. weekly),       used_percent + resets_at
# Every codex role (agent / SIM / review) authenticates against the operator's
# single subscription, so the freshest rollout — whatever role wrote it — holds
# the current account-wide quota. We surface it in the footer so the operator
# can see how close a run is to getting rate-limited.


def _latest_codex_rollout(run_dir: Path) -> Path | None:
    """Most-recently-modified codex session rollout under any codex home."""

    newest: Path | None = None
    newest_mtime = -1.0
    for home in run_dir.glob("codex-*-home"):
        for roll in home.rglob("rollout-*.jsonl"):
            try:
                mtime = roll.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest = roll
    return newest


def _read_codex_usage(run_dir: Path) -> dict[str, Any] | None:
    """Latest codex rate-limit snapshot for `run_dir`, or None.

    Tails the newest session rollout (only the last `_CODEX_USAGE_TAIL_BYTES`)
    for the most recent `token_count` event and returns its rate-limit windows.
    Returns None when this isn't a codex run, or no snapshot has landed yet.
    """

    roll = _latest_codex_rollout(run_dir)
    if roll is None:
        return None
    try:
        size = roll.stat().st_size
        with roll.open("rb") as fh:
            if size > _CODEX_USAGE_TAIL_BYTES:
                fh.seek(size - _CODEX_USAGE_TAIL_BYTES)
                fh.readline()  # drop the partial line the seek landed mid-way
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        line = line.strip()
        # Cheap pre-filter — skip the (large) response_item lines without a parse.
        if not line or '"token_count"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if payload.get("type") != "token_count":
            continue
        # codex emits a `token_count` after every turn, but only populates the
        # rate_limit windows on turns that hit the metered endpoint (the rest
        # carry `rate_limits.primary: null`). Keep walking back to the last
        # populated snapshot rather than stopping at an empty trailing one.
        usage = _codex_usage_from_payload(payload)
        if usage is not None:
            return usage
    return None


def _codex_usage_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the rate-limit windows out of a codex `token_count` payload."""

    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None

    def _window(w: Any) -> dict[str, Any] | None:
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            return None
        return {
            "used_percent": float(w["used_percent"]),
            "window_minutes": w.get("window_minutes"),
            "resets_at": w.get("resets_at"),
        }

    primary = _window(rate_limits.get("primary"))
    secondary = _window(rate_limits.get("secondary"))
    if primary is None and secondary is None:
        return None
    return {
        "primary": primary,
        "secondary": secondary,
        "plan_type": rate_limits.get("plan_type"),
    }


def _fmt_window_minutes(minutes: Any) -> str:
    """`300` → `5h`, `10080` → `7d`, else `{n}m`. Empty for unknown."""

    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return ""
    m = int(minutes)
    if m % 1440 == 0:
        return f"{m // 1440}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def _fmt_reset(resets_at: Any, now: float) -> str:
    """`↻1h02m` countdown to a reset epoch; empty when unknown or elapsed."""

    if not isinstance(resets_at, (int, float)):
        return ""
    secs = int(resets_at - now)
    if secs <= 0:
        return ""
    hours, rem = divmod(secs, 3600)
    mins = rem // 60
    return f"↻{hours}h{mins:02d}m" if hours else f"↻{mins}m"


def _usage_pct_style(remaining: float) -> str:
    """Color a remaining-budget percentage: red when nearly spent, amber when
    low, neutral otherwise — only a shrinking budget should draw the eye."""

    if remaining < 15:
        return _PAL_ERROR
    if remaining < 40:
        return _PAL_WARN
    return _PAL_TEXT


def _codex_usage_token(
    usage: dict[str, Any] | None,
    *,
    tool: str = "codex",
    now: float | None = None,
) -> "Text | None":
    """The `codex 5h 25% left (↻6m)` footer fragment, or None when no data.

    Codex sometimes emits both rollout windows, but the footer intentionally
    shows only the primary short window so the label is stable and comparable
    across turns.
    """

    if not usage:
        return None
    head = usage.get("primary")
    if not head:
        return None
    now = time.time() if now is None else now

    rem_p = max(0.0, 100.0 - head["used_percent"])
    win_p = _fmt_window_minutes(head.get("window_minutes")) or "5h"
    text = Text()
    text.append(f"{tool} ", style=_PAL_DIM)
    text.append(f"{win_p} ", style=_PAL_VDIM)
    text.append(f"{round(rem_p)}% left", style=_usage_pct_style(rem_p))
    reset = _fmt_reset(head.get("resets_at"), now)
    if reset:
        text.append(f" ({reset})", style=_PAL_VDIM)
    return text


# claude exposes Claude.ai subscription windows through its documented statusLine
# input (`rate_limits.five_hour` / `rate_limits.seven_day`), not through the
# `stream-json` stdout we tail for chat/tool events. ClaudeDriver installs a
# tiny per-run statusLine command that whitelists those fields into
# `claude-*-home/.contremaitre/statusline.jsonl`; the TUI tails that snapshot.


def _statusline_model_id(snapshot: dict[str, Any]) -> str | None:
    model = snapshot.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    if isinstance(model, dict):
        for key in ("id", "model", "name", "display_name"):
            value = model.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _claude_usage_from_statusline(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a Claude Code statusLine snapshot to codex-like windows."""

    rate_limits = snapshot.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None

    def _window(name: str, minutes: int) -> dict[str, Any] | None:
        raw = rate_limits.get(name)
        if not isinstance(raw, dict) or not isinstance(raw.get("used_percentage"), (int, float)):
            return None
        return {
            "used_percent": float(raw["used_percentage"]),
            "window_minutes": minutes,
            "resets_at": raw.get("resets_at"),
        }

    primary = _window("five_hour", 300)
    secondary = _window("seven_day", 10080)
    if primary is None and secondary is None:
        return None
    model = _statusline_model_id(snapshot)
    return {
        "primary": primary,
        "secondary": secondary,
        "model": model,
        "family": _claude_family(model),
        "recorded_at": snapshot.get("recorded_at"),
    }


def _last_claude_statusline_usages(path: Path) -> list[dict[str, Any]]:
    """Latest populated statusLine usage snapshots per model in one home."""

    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _CODEX_USAGE_TAIL_BYTES:
                fh.seek(size - _CODEX_USAGE_TAIL_BYTES)
                fh.readline()  # drop the partial line the seek landed mid-way
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line or '"rate_limits"' not in line:
            continue
        try:
            snapshot = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(snapshot, dict):
            usage = _claude_usage_from_statusline(snapshot)
            if usage is not None:
                key = usage.get("model") or "__unknown__"
                if key not in seen:
                    seen.add(key)
                    out.append(usage)
    return out


def _last_claude_statusline_usage(path: Path) -> dict[str, Any] | None:
    """Latest populated statusLine usage snapshot in one claude home."""

    usages = _last_claude_statusline_usages(path)
    return usages[0] if usages else None


def _read_claude_usages(run_dir: Path) -> list[dict[str, Any]]:
    """Latest claude statusLine snapshots for each model in `run_dir`."""

    snapshots: list[tuple[float, Path]] = []
    for home in run_dir.glob("claude-*-home"):
        p = home / ".contremaitre" / "statusline.jsonl"
        try:
            snapshots.append((p.stat().st_mtime, p))
        except OSError:
            continue
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _mtime, p in sorted(snapshots, reverse=True):
        for usage in _last_claude_statusline_usages(p):
            key = usage.get("model") or "__unknown__"
            if key in seen:
                continue
            seen.add(key)
            out.append(usage)
    return out


def _read_claude_usage(run_dir: Path) -> dict[str, Any] | None:
    """Latest claude statusLine rate-limit snapshot for `run_dir`, or None.

    Covers claude in any role (agent / SIM / review) by scanning all per-run
    claude homes newest first. Returns None until Claude Code has produced a
    statusLine snapshot with subscription rate_limits, or for non-claude runs.
    """

    usages = _read_claude_usages(run_dir)
    return usages[0] if usages else None


def _select_claude_usage(
    usages: list[dict[str, Any]],
    target: str | None,
) -> dict[str, Any] | None:
    """Pick the statusLine snapshot matching a role's active Claude model.

    `target` is the role's resolved model name (`ModelSpec.canonical()[0]`),
    not a label — never a parse contract."""

    if not usages:
        return None
    target = target or ""
    target_norm = target.lower()
    family = _claude_family(target)
    if target_norm and target_norm not in {"?", "claude-default", "default"}:
        for usage in usages:
            model = usage.get("model")
            if isinstance(model, str) and model.lower() == target_norm:
                return usage
        if family:
            for usage in usages:
                if usage.get("family") == family:
                    return usage
            return None
        return usages[0] if len(usages) == 1 else None
    return usages[0]


def _claude_usage_token(
    usage: dict[str, Any] | None,
    *,
    now: float | None = None,
    model_label: str | None = None,
) -> "Text | None":
    """The `claude 5h 25% left (↻1h02m)` footer fragment, or None."""

    if not usage:
        return None
    head = usage.get("primary") or usage.get("secondary")
    if not head:
        return None
    now = time.time() if now is None else now
    other = usage.get("secondary") if head is usage.get("primary") else None
    rem_p = max(0.0, 100.0 - head["used_percent"])
    win_p = _fmt_window_minutes(head.get("window_minutes")) or "limit"
    text = Text()
    text.append("claude ", style=_PAL_DIM)
    if model_label:
        text.append(f"{model_label} ", style=_PAL_DIM)
    text.append(f"{win_p} ", style=_PAL_VDIM)
    text.append(f"{round(rem_p)}% left", style=_usage_pct_style(rem_p))
    reset = _fmt_reset(head.get("resets_at"), now)
    if reset:
        text.append(f" ({reset})", style=_PAL_VDIM)
    if other:
        rem_s = max(0.0, 100.0 - other["used_percent"])
        if rem_s < rem_p:
            win_s = _fmt_window_minutes(other.get("window_minutes")) or "7d"
            text.append(f" · {win_s} ", style=_PAL_VDIM)
            text.append(f"{round(rem_s)}% left", style=_usage_pct_style(rem_s))
    return text


def _footer_meter_tokens(
    *,
    agent_model: ModelSpec,
    sim_model: ModelSpec,
    agent_events: list[dict[str, Any]],
    sim_events: list[dict[str, Any]],
    codex_usage: dict[str, Any] | None,
    claude_usages: list[dict[str, Any]],
    now: float | None = None,
    free_wink: bool = False,
) -> list["Text"]:
    """Per-role footer meter tokens for AGENT and SIM."""

    def _with_role(role: str, body: Text) -> Text:
        text = Text()
        text.append(f"{role} ", style=_PAL_DIM)
        text.append(body)
        return text

    def _unknown(role: str, tool: str) -> Text:
        text = Text()
        text.append(f"{role} ", style=_PAL_DIM)
        text.append(f"{tool} ?", style=_PAL_WARN)
        return text

    def _free(role: str) -> Text:
        face = "◕‿-" if free_wink else "◕‿◕"
        text = Text()
        text.append(f"{role} ", style=_PAL_DIM)
        text.append(f"free ({face})", style=_PAL_SUCCESS)
        return text

    def _paid(role: str, events: list[dict[str, Any]]) -> Text:
        cost = sum_costs_in_events(events)
        text = Text()
        text.append(f"{role} ", style=_PAL_DIM)
        text.append(f"${cost:.4f}", style=_PAL_TEXT if cost > 0 else _PAL_DIM)
        return text

    def _role(role: str, spec: ModelSpec, events: list[dict[str, Any]]) -> Text:
        if _is_free_model(spec.requested):
            return _free(role)
        name = spec.canonical()[0]
        if spec.runtime == "codex":
            token = _codex_usage_token(codex_usage, now=now)
            return _with_role(role, token) if token is not None else _unknown(role, "codex")
        if spec.runtime == "claude":
            usage = _select_claude_usage(claude_usages, name)
            label = _claude_family(name) or (usage.get("family") if usage else None)
            token = _claude_usage_token(usage, now=now, model_label=label)
            return _with_role(role, token) if token is not None else _unknown(role, "claude")
        return _paid(role, events)

    return [_role("A", agent_model, agent_events), _role("S", sim_model, sim_events)]


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
        return (
            "",
            ts,
            Text("tool_use", style="dim"),
            Text(tool, style=f"bold {_tool_style(tool)}"),
            body,
        )

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
        return (
            Text("▍", style="red"),
            ts,
            Text("error", style="bold red"),
            "",
            Text(msg, style="red"),
        )

    # ----- codex --json shapes (CLI actor streams these into raw_export) -----
    if t == "thread.started":
        return (
            "",
            ts,
            Text("session", style="dim"),
            "",
            Text(event.get("thread_id", ""), style="dim"),
        )

    if t == "turn.started":
        return ("", ts, Text("turn", style="dim"), "", "")

    if t == "turn.completed":
        u = event.get("usage") or {}
        bits = [
            f"in {u.get('input_tokens', 0):,}",
            f"out {u.get('output_tokens', 0):,}",
            f"cache-r {u.get('cached_input_tokens', 0):,}",
        ]
        return ("", ts, Text("turn_done", style="dim"), "", Text(" ".join(bits), style="dim"))

    if t in ("item.started", "item.completed"):
        return _codex_item_row(event, ts)

    return ("", ts, Text(t or "?", style="dim"), "", "")


def _codex_item_row(event: dict[str, Any], ts: str):
    """Row for a codex `item.started`/`item.completed` event.

    Renders the agent's live work: command executions (with output + exit code),
    the final agent_message (as a `text` row, like opencode), reasoning, and a
    generic fallback for any other item type.
    """

    item = event.get("item") or {}
    itype = item.get("type")
    completed = event.get("type") == "item.completed"

    if itype == "agent_message":
        txt = item.get("text") or ""
        body = Text()
        body.append(f"{len(txt):,} chars\n", style="dim")
        body.append(txt)
        return (Text("▍", style="blue"), ts, Text("text", style="bold"), "", body)

    if itype == "command_execution":
        # Just the command + exit status — the aggregated stdout/stderr is noise
        # in the live pane (and still on disk in raw_export for forensics).
        body = Text()
        body.append((item.get("command") or "").rstrip(), style="cyan")
        if completed:
            body.append(f"  exit {item.get('exit_code')}", style="dim")
        else:
            body.append("  running…", style="dim")
        return ("", ts, Text("command", style="bold"), Text("bash", style="bold cyan"), body)

    if itype == "reasoning":
        txt = item.get("text") or item.get("summary") or ""
        return ("", ts, Text("reasoning", style="dim"), "", Text(str(txt), style="dim italic"))

    summary = json.dumps({k: v for k, v in item.items() if k != "id"})[:200]
    return ("", ts, Text(itype or "item", style="dim"), "", Text(summary, style="dim"))


# claude `stream-json` event types — disjoint from opencode (text/tool_use/
# step_*/error) and codex (thread.*/turn.*/item.*), so a type check suffices.
_CLAUDE_EVENT_TYPES = frozenset({"system", "assistant", "user", "result"})


def _claude_tool_body(name: str, inp: dict[str, Any]) -> "Text":
    """Compact one-line body for a claude `tool_use` block (input only).

    The tool_result (output) arrives in a later `user` event; the live pane keeps
    just the call, like the codex command rows (full I/O stays on disk).
    """

    body = Text()
    if name == "Bash":
        body.append(_truncate(str(inp.get("command") or "")), style="cyan")
    elif name in ("Read", "Edit", "Write"):
        body.append(str(inp.get("file_path") or ""))
    elif name in ("Grep", "Glob"):
        pat = inp.get("pattern") or inp.get("query") or inp.get("glob") or ""
        body.append(_truncate(str(pat)))
    elif name == "Task":
        body.append(_truncate(str(inp.get("description") or inp.get("subagent_type") or "")))
    else:
        body.append(_truncate(json.dumps(inp)), style="dim")
    return body


def _claude_tool_result_text(content: Any) -> str:
    """Flatten a claude tool_result `content` (str or list of blocks) for display."""

    if isinstance(content, str):
        return _truncate(content.replace("\n", " "))
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict)]
        return _truncate(" ".join(parts).replace("\n", " "))
    return ""


def _claude_event_rows(event: dict[str, Any], ts: str) -> list:
    """Rows for one claude `stream-json` event — 0..N (claude bundles blocks).

    `system/init` → a session row (id + model); `assistant` → one row per
    content block (text → blue text row, tool_use → tool row); `user` →
    compact tool_result rows; `result` → a turn_done row with token rollup + cost
    ($total_cost_usd), reddened on a non-success result.
    """

    t = event.get("type")
    if t == "system":
        if event.get("subtype") == "init":
            label = f"{event.get('session_id', '')}  {event.get('model', '')}".strip()
            return [("", ts, Text("session", style="dim"), "", Text(label, style="dim"))]
        return [
            (
                "",
                ts,
                Text("system", style="dim"),
                "",
                Text(str(event.get("subtype") or ""), style="dim"),
            )
        ]

    if t == "assistant":
        rows: list = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                txt = block.get("text") or ""
                body = Text()
                body.append(f"{len(txt):,} chars\n", style="dim")
                body.append(txt)
                rows.append((Text("▍", style="blue"), ts, Text("text", style="bold"), "", body))
            elif block.get("type") == "tool_use":
                name = block.get("name") or "?"
                rows.append(
                    (
                        "",
                        ts,
                        Text("tool_use", style="dim"),
                        Text(name, style=f"bold {_tool_style(name.lower())}"),
                        _claude_tool_body(name, block.get("input") or {}),
                    )
                )
        return rows

    if t == "user":
        rows = []
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                rows.append(
                    (
                        "",
                        ts,
                        Text("tool_result", style="dim"),
                        "",
                        Text(_claude_tool_result_text(block.get("content")), style="dim"),
                    )
                )
        return rows

    if t == "result":
        u = event.get("usage") or {}
        bits = [
            f"in {u.get('input_tokens', 0):,}",
            f"out {u.get('output_tokens', 0):,}",
            f"cache-r {u.get('cache_read_input_tokens', 0):,}",
        ]
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            bits.append(f"cost ${cost:.4f}")
        ok = not event.get("is_error") and event.get("subtype") in (None, "success")
        style = "dim" if ok else "red"
        return [("", ts, Text("turn_done", style=style), "", Text(" ".join(bits), style=style))]

    return [("", ts, Text(str(t) or "?", style="dim"), "", "")]


def _event_table() -> "Table":
    t = Table.grid(padding=(0, 1))
    t.add_column(width=1, no_wrap=True)
    t.add_column(width=8, no_wrap=True, style="dim")
    t.add_column(width=11, no_wrap=True)
    t.add_column(width=11, no_wrap=True)
    t.add_column(overflow="fold")
    return t


def _render_event(event: dict[str, Any]):
    # codex `--json` / claude `stream-json` events carry no timestamp; stamp
    # first-render (≈ first-seen) wall-clock so CLI rows show a time column too.
    # opencode events already have `timestamp` and are left alone. Each event is
    # rendered once (the panes are index-gated), so for a live tail this is the
    # moment it arrived.
    if not event.get("timestamp"):
        event["timestamp"] = int(time.time() * 1000)
    ts = _fmt_ts(event.get("timestamp"))
    t = _event_table()
    if event.get("type") in _CLAUDE_EVENT_TYPES:
        # claude bundles several content blocks per event → one row each.
        for row in _claude_event_rows(event, ts):
            t.add_row(*row)
    else:
        t.add_row(*_build_event_row(event))
    return t


_CLI_REVIEW_VERDICT_RANK = {
    "MUST_FIX": 3,
    "NEEDS_ATTENTION": 2,
    "LOOKS_GOOD": 1,
}


def _aggregate_cli_review_verdict(verdicts: list[str | None]) -> str | None:
    """Worst-case verdict across a cli_review round.

    MUST_FIX > NEEDS_ATTENTION > LOOKS_GOOD. Drives the aggregate
    footer glyph — if any reviewer says MUST_FIX the operator should
    see `✗`, not the rosier result. Returns None when no verdict is in
    scope (tool not done or didn't surface a key).
    """

    best_rank = 0
    best_key: str | None = None
    for v in verdicts:
        rank = _CLI_REVIEW_VERDICT_RANK.get(v or "", 0)
        if rank > best_rank:
            best_rank = rank
            best_key = v
    return best_key


def _derive_cli_review_states(
    guardrails: list[dict[str, Any]], choice: str
) -> list[tuple[str, str, str | None]]:
    """Return `[(tool, status, verdict)]` in render order for `choice`.

    `status` is one of `"streaming"`, `"completed"`, `"failed"`. Tools
    that haven't emitted `cli_review_started` yet are dropped — they'd
    show as a dim `·` placeholder otherwise and the footer would start
    wider than it ends up. Render order matches `expand_choice` so
    `both` lists claude then codex (left-to-right glyph order in the
    footer mirrors execution order).
    """

    tools = _cli_reviewer_expand_choice(choice)
    if not tools:
        return []
    out: list[tuple[str, str, str | None]] = []
    for tool in tools:
        started_evt = next(
            (
                g
                for g in reversed(guardrails)
                if g.get("event") == "cli_review_started" and g.get("tool") == tool
            ),
            None,
        )
        if started_evt is None:
            continue
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
        failed = any(
            g.get("event") == "cli_review_failed"
            and g.get("tool") == tool
            and (round_n is None or g.get("round") == round_n)
            for g in guardrails
        )
        if failed:
            status = "failed"
        elif completed_evt is not None:
            status = "completed"
        else:
            status = "streaming"
        verdict = completed_evt.get("verdict") if completed_evt else None
        out.append((tool, status, verdict))
    return out


def _cli_review_tool_header(tool: str) -> Text:
    """Inline `── {tool} review ──` divider for the cli-review pane.

    Written once at the moment the tool's first stdout chunk lands.
    Single-tool runs only see their own divider — harmless visual
    context, and it keeps the renderer uniform.
    """

    label = f"── {tool} review "
    body = label + "─" * max(0, 44 - len(label))
    sep = Text()
    sep.append(body, style="bold cyan")
    return sep


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
    body = "── " + label + " " + "─" * max(0, 40 - len(label) - 4)
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
    if any(
        k in kind
        for k in (
            "infra_failure",
            "blocked",
            "malformed",
            "quota_exhausted",
            "provider_transient_error",
        )
    ) and not kind.endswith("_retry"):
        style = f"bold {_PAL_ERROR}"
    elif kind == "provider_transient_error_retry":
        # Each retry attempt is a "we're trying again" cue — warn-style
        # so the operator notices but knows the run hasn't given up.
        style = f"bold {_PAL_WARN}"
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

    class _NoFocusRichLog(RichLog):
        # can_focus is no longer accepted as a Widget.__init__ kwarg in
        # Textual 8.x; set it as a class attribute instead.
        can_focus = False

    class ContremaitreTUI(App):
        CSS = """
        Screen { layout: vertical; padding: 0 1 1 1; }
        #header { height: 1; padding: 0 1; }
        #header2 { height: 1; padding: 0 1; }
        #panes { height: 1fr; }
        .pane { width: 1fr; border: round white; }
        .pane.active { border: round yellow; }
        .pane-sub { height: 1; padding: 0 1; color: $text-muted; }
        #sim-column { width: 1fr; layout: vertical; }
        .sim-subpane { width: 1fr; height: 1fr; border: round white; }
        .sim-subpane.active { border: round yellow; }
        #cli-review-pane.active { border: round yellow; }
        #cli-review-pane.hidden { display: none; }
        RichLog {
            background: $background;
            scrollbar-size-vertical: 1;
            scrollbar-size-horizontal: 1;
            scrollbar-color: white;
            scrollbar-color-active: white;
            scrollbar-color-hover: white;
            scrollbar-background: $background;
            scrollbar-background-active: $background;
            scrollbar-background-hover: $background;
        }
        #activity-panel { height: 8; border: round white; }
        #footer-bar { layout: horizontal; height: 1; padding: 0 1; }
        #footer-left { width: 1fr; height: 1; }
        #footer-right { width: auto; height: 1; }
        #links-line { layout: horizontal; height: 1; padding: 0 1; }
        #links-line.hidden { display: none; }
        .link-token {
            width: auto;
            height: 1;
            margin-right: 4;
            color: #6B8AFF;
        }
        .link-token:hover { color: #FFFFFF; text-style: underline; }
        .link-token.hidden { display: none; }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit", "Quit (kills orchestrator)", show=True),
        ]

        def __init__(
            self,
            run_dir: Path,
            *,
            agent_model: ModelSpec | None = None,
            sim_model: ModelSpec | None = None,
            cli_reviewer: str = "none",
            docker_image: str = "?",
            target_url: str | None = None,
            base: str | None = None,
            proc: subprocess.Popen | None = None,
            refresh_hz: float = 5.0,
        ):
            super().__init__()
            self.run_dir = run_dir
            self.agent_model = agent_model or _UNKNOWN_SPEC
            self.sim_model = sim_model or _UNKNOWN_SPEC
            # `"codex"`, `"claude"`, `"auto"`, or `"none"`. When set
            # (other than `"none"`), the post-publish cli-review row
            # mounts and shows the subscription-bound CLI reviewer's
            # stream with `── <tool> review ──` dividers in time order.
            self.cli_reviewer = cli_reviewer
            self.docker_image = docker_image
            self.target_url = target_url
            self.base = base
            self.proc = proc
            self.refresh_hz = refresh_hz
            self.t_start = time.time()
            self._agent_idx = 0
            self._agent_model_resolved = False
            self._sim_idx = 0
            self._sim_model_resolved = False
            self._guardrail_idx = 0
            self._recoveries_idx = 0
            self._docker_state: dict[str, Any] = {}
            self._docker_ts = 0.0
            # codex subscription rate-limit snapshot, refreshed off the hot path
            # (see CODEX_USAGE_REFRESH_S). None until the first read / for
            # non-codex runs.
            self._codex_usage: dict[str, Any] | None = None
            self._codex_usage_ts = 0.0
            self._claude_usages: list[dict[str, Any]] = []
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
            # Active-pane tracker + wink countdown. When the highlighted
            # pane changes, the free-cost smiley winks (◕‿-) for a few
            # ticks before returning to (◕‿◕). Sentinel-init prevents a
            # spurious wink on the very first tick (no prior active to
            # diff against).
            self._prev_active: str | None | object = _UNSET_ACTIVE
            self._wink_ticks_remaining: int = 0
            # Per-pane handover separator counters. Each orchestrator
            # turn handover emits one `opencode_actor_start` event with
            # role agent/sim/review; we render a separator into the
            # *just-finished* pane each time a new start event appears
            # for it, so the label corresponds to the turn that ended.
            self._agent_separators_rendered = 0
            self._sim_separators_rendered = 0
            self._extra_separators_rendered = 0
            # Post-publish CLI review (claude/codex) chunk stream. Rendered
            # into its own pane (#cli-review-pane). The detected tool name
            # is cached so the chrome border-title can flip to e.g.
            # "CODEX REVIEW" the moment the first chunk lands.
            #
            # Each tool's stream is preceded by a
            # `── claude review ──` / `── codex review ──` divider
            # written once at first event arrival. Per-tool cursors keep
            # the divider's idempotence: each event renders exactly once
            # even though the file is re-read on every tick.
            self._cli_review_cursor: dict[str, int] = {"claude": 0, "codex": 0}
            self._cli_review_headers_written: set[str] = set()
            self._cli_review_tool: str = ""
            # Captured at terminal state for the post-exit URL summary
            # printed to stdout — gives Apple Terminal users (no OSC 8
            # support) cmd+clickable links in their scrollback.
            self._final_pr_url: str | None = None
            self._final_viewer_path: str | None = None

        @property
        def paths(self) -> dict[str, Path]:
            return {
                "raw_export": self.run_dir / "raw_export.jsonl",
                "sim_raw_export": self.run_dir / "sim_raw_export.jsonl",
                "claude_review_raw_export": self.run_dir / "claude_review_raw_export.jsonl",
                "codex_review_raw_export": self.run_dir / "codex_review_raw_export.jsonl",
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
            yield Static("", id="header2")
            with Horizontal(id="panes"):
                with Vertical(classes="pane", id="agent-pane"):
                    yield _NoFocusRichLog(
                        id="agent-log",
                        auto_scroll=False,
                        markup=False,
                        wrap=True,
                        highlight=False,
                    )
                    yield Static("", classes="pane-sub", id="agent-sub")
                with Vertical(id="sim-column"):
                    with Vertical(classes="sim-subpane", id="sim-pane"):
                        yield _NoFocusRichLog(
                            id="sim-log",
                            auto_scroll=False,
                            markup=False,
                            wrap=True,
                            highlight=False,
                        )
                        yield Static("", classes="pane-sub", id="sim-sub")
                    # Post-publish CLI reviewer — splits the SIM column in
                    # half vertically. Hidden until cli_review_started fires;
                    # reveals in-place so the SIM pane shrinks to share
                    # the column. Uses sim-subpane class for consistent
                    # border/sizing with the SIM pane above.
                    with Vertical(classes="sim-subpane", id="cli-review-pane"):
                        yield _NoFocusRichLog(
                            id="cli-review-log",
                            auto_scroll=False,
                            markup=False,
                            wrap=True,
                            highlight=False,
                        )
                        yield Static("", classes="pane-sub", id="cli-review-sub")
            with Vertical(id="activity-panel"):
                yield _NoFocusRichLog(
                    id="activity-log",
                    auto_scroll=False,
                    markup=False,
                    wrap=True,
                    highlight=False,
                )
            with Horizontal(id="footer-bar"):
                yield Static("", id="footer-left")
                yield Static("", id="footer-right")
            # Pre-mount Link widgets so we can show/hide them at terminal
            # state without re-mounting. URL + visible text are set at
            # runtime in _update_chrome. Textual's `Link` widget catches
            # the click itself and opens the URL via `webbrowser.open()` —
            # works in every terminal (Apple Terminal, Ghostty, iTerm2…)
            # because the terminal is not involved in the click handling.
            with Horizontal(id="links-line", classes="hidden"):
                yield Link("", url="", id="pr-link", classes="link-token hidden")
                yield Link("", url="", id="viewer-link", classes="link-token hidden")

        def on_mount(self) -> None:
            self.title = f"contremaitre · {self.run_dir.name}"
            refresh_s = 1.0 / max(0.5, min(20.0, self.refresh_hz))
            self.set_interval(refresh_s, self._tick)
            # CLI-review pane hidden on mount; reveals once the post-publish
            # reviewer starts (see `_update_cli_review_log`).
            self.query_one("#cli-review-pane").set_class(True, "hidden")

        def _tick(self) -> None:
            now = time.time()
            if now - self._docker_ts > DOCKER_REFRESH_S:
                self._docker_state = _docker_info(self.docker_image, self.worktree)
                self._docker_ts = now
            if now - self._codex_usage_ts > CODEX_USAGE_REFRESH_S:
                self._codex_usage = _read_codex_usage(self.run_dir)
                self._claude_usages = _read_claude_usages(self.run_dir)
                self._codex_usage_ts = now
            self._update_turn_separators()
            self._update_agent_log()
            self._update_sim_log()
            if self.cli_reviewer in ("codex", "claude", "auto"):
                self._update_cli_review_log()
            self._update_activity_log()
            self._update_chrome()

        @staticmethod
        def _at_bottom(widget: RichLog) -> bool:
            return widget.scroll_y >= widget.max_scroll_y

        def _update_agent_log(self) -> None:
            events = _read_jsonl(self.paths["raw_export"])
            # Back-fill the header's agent model with the real one the CLI reports
            # (the pre-run label shows the account default when claude_model is empty).
            if not self._agent_model_resolved:
                model = resolved_model_from_events(events)
                if model:
                    self.agent_model = self.agent_model.with_resolved(model)
                    self._agent_model_resolved = True
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

        def _update_cli_review_log(self) -> None:
            """Stream post-publish CLI reviewer chunks into the bottom row.

            The pane is hidden until the orchestrator emits the
            `cli_review_started` guardrail event — NOT the first stdout
            chunk. Codex spins up for several seconds before writing
            anything to stdout; waiting on the first chunk made the pane
            appear delayed (looked like "only shows when it's done"). The
            reveal now coincides with the visible `cli_review_started`
            line in the activity log directly below.

            Each tool's stream is preceded by a
            `── claude review ──` / `── codex review ──` divider written
            once at first event arrival. Single-tool runs only see their
            own divider.
            """

            # Unhide as soon as the orchestrator says the CLI reviewer has
            # started, even before any stdout has arrived.
            guardrails = _read_jsonl(self.paths["guardrail_events"])
            if any(g.get("event") == "cli_review_started" for g in guardrails):
                pane = self.query_one("#cli-review-pane")
                if pane.has_class("hidden"):
                    pane.set_class(False, "hidden")

            widget = self.query_one("#cli-review-log", RichLog)
            at_bottom = self._at_bottom(widget)
            # claude first, codex second — dividers land in execution order.
            for tool in ("claude", "codex"):
                events = _read_jsonl(self.paths[f"{tool}_review_raw_export"])
                if not events:
                    continue
                if tool not in self._cli_review_headers_written:
                    widget.write(_cli_review_tool_header(tool))
                    self._cli_review_headers_written.add(tool)
                # Cache the most recently-streaming tool for chrome
                # title flips (only meaningful in single-tool mode, but
                # cheap to maintain in `both` mode too).
                self._cli_review_tool = tool
                cursor = self._cli_review_cursor.get(tool, 0)
                for e in events[cursor:]:
                    widget.write(_render_event(e))
                self._cli_review_cursor[tool] = len(events)
            if at_bottom:
                widget.scroll_end(animate=False)

        def _update_sim_log(self) -> None:
            events = _read_jsonl(self.paths["sim_raw_export"])
            if not self._sim_model_resolved:
                model = resolved_model_from_events(events)
                if model:
                    self.sim_model = self.sim_model.with_resolved(model)
                    self._sim_model_resolved = True
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
                e
                for e in guardrails
                if e.get("event") == "opencode_actor_start" and e.get("role") == "agent"
            ]
            sim_starts = [
                e
                for e in guardrails
                if e.get("event") == "opencode_actor_start" and e.get("role") in ("sim", "review")
            ]
            agent_widget = self.query_one("#agent-log", RichLog)
            sim_widget = self.query_one("#sim-log", RichLog)
            agent_at_bottom = self._at_bottom(agent_widget)
            sim_at_bottom = self._at_bottom(sim_widget)
            while self._agent_separators_rendered < len(agent_starts) - 1:
                n = self._agent_separators_rendered + 2
                role = agent_starts[n - 1].get("role", "agent")
                agent_widget.write(_turn_separator(n, _role_label(role)))
                self._agent_separators_rendered += 1
            while self._sim_separators_rendered < len(sim_starts) - 1:
                n = self._sim_separators_rendered + 2
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
            """Whichever pane wrote most recently is "active" (yellow border).

            File freshness is the canonical signal because SIM-review and
            extra-review run sequentially (never concurrently) but share
            the same RO-mount container slot in docker_state — so
            container presence alone can't tell which pane owns it.
            Container presence is a fallback for the agent (which has its
            own RW slot) when no file has been written yet.
            """

            ages: dict[str, float] = {}
            for name, key in (
                ("agent", "raw_export"),
                ("sim", "sim_raw_export"),
                ("cli_review", "claude_review_raw_export"),
                ("cli_review", "codex_review_raw_export"),
            ):
                if name == "cli_review" and self.cli_reviewer not in ("codex", "claude"):
                    continue
                age = _file_age(self.paths[key])
                if age is not None:
                    # Two cli_review sinks share the same active key — keep
                    # the freshest one (lowest age) so whichever tool the
                    # orchestrator chose drives the highlight.
                    if name not in ages or age < ages[name]:
                        ages[name] = age
            if ages:
                return min(ages, key=ages.get)
            # No file has been written yet — fall back to container slots
            # so the active border still shows during startup.
            if self._docker_state.get("agent_container"):
                return "agent"
            if self._docker_state.get("sim_container"):
                return "sim"
            return None

        def _update_chrome(self) -> None:
            agent_events = _read_jsonl(self.paths["raw_export"])
            sim_events = _read_jsonl(self.paths["sim_raw_export"])
            recoveries = _read_jsonl(self.paths["recoveries"])
            guardrails = _read_jsonl(self.paths["guardrail_events"])

            # CLI-review event flags — derived early so they're available
            # to both the CLI pane chrome (mid-function) and the phase
            # machine (later). Single source of truth per tick.
            #
            # Per-tool states (`[(tool, status, verdict)]`) drive the
            # multi-tool path: the phase machine and footer treat the
            # cli_review step as one logical phase but render each
            # tool's verdict glyph separately. `cli_review_completed`
            # is the aggregate flag —
            # only true once every expected tool has settled — so the
            # phase doesn't transition to "done" while the second
            # reviewer is still streaming.
            cli_review_states = _derive_cli_review_states(guardrails, self.cli_reviewer)
            expected_tools = _cli_reviewer_expand_choice(self.cli_reviewer)
            cli_review_started = any(
                s[1] in ("streaming", "completed", "failed") for s in cli_review_states
            )
            cli_review_failed = any(s[1] == "failed" for s in cli_review_states)
            if expected_tools:
                settled_tools = {
                    t for (t, st, _v) in cli_review_states if st in ("completed", "failed")
                }
                cli_review_completed = bool(settled_tools) and all(
                    t in settled_tools for t in expected_tools
                )
            else:
                cli_review_completed = False
            # Verdict key (MUST_FIX / NEEDS_ATTENTION / LOOKS_GOOD) the
            # agent wrote on line 1 of the review, surfaced on the
            # completion event payload by the orchestrator. Drives the
            # footer glyph so `MUST_FIX` renders as `✗` instead of the
            # subprocess-exit-0 `✓`. Worst-case wins for `both`: if
            # either reviewer says MUST_FIX, the aggregate is MUST_FIX.
            cli_review_verdict: str | None = _aggregate_cli_review_verdict(
                [v for (_t, st, v) in cli_review_states if st == "completed"]
            )

            agent_turns = _text_event_count(agent_events)
            sim_turns = _text_event_count(sim_events)
            settled = _settled_in(agent_events)
            impl_complete = _impl_complete_in(agent_events)
            self_verified = _self_verified_in(agent_events)

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
                self._frozen_elapsed
                if terminal and self._frozen_elapsed is not None
                else (time.time() - self.t_start)
            )

            # ----- Header -----
            img = self._docker_state.get("image_created")
            header = Text()
            header.append("contremaitre · ", style="#6B8AFF")
            header.append(self.run_dir.name)
            header.append(
                f"  ·  agent={self.agent_model.display()}  sim={self.sim_model.display()}",
                style="dim",
            )
            if img:
                header.append(f"  ·  {self.docker_image} built {img}", style="dim")
            self.query_one("#header", Static).update(header)

            # ----- Header line 2: target repo + base branch -----
            # Static once the run is launched. `→ <owner/repo> • git:<base>`:
            # arrow = "targeting", bullet softens the separator, `git:` prefix
            # disambiguates the colon-suffixed branch name from a tag/SHA.
            header2 = Text()
            header2.append("→ ", style=_PAL_ACCENT)
            header2.append(_short_repo(self.target_url), style=_PAL_ACCENT)
            header2.append("  •  ", style=_PAL_VDIM)
            header2.append(f"git:{self.base or '?'}", style=_PAL_ACCENT)
            self.query_one("#header2", Static).update(header2)

            # ----- Pane subheaders with thinking loader -----
            ag = self._docker_state.get("agent_container")
            sm = self._docker_state.get("sim_container")
            sim_file_age = _file_age(self.paths["sim_raw_export"])
            agent_state = _activity_state(
                container_present=bool(ag),
                file_age=_file_age(self.paths["raw_export"]),
            )
            sim_state = _activity_state(
                container_present=bool(sm),
                file_age=sim_file_age,
            )
            # Tick the spinner only while at least one pane is non-idle;
            # otherwise a finished run would keep visually animating
            # forever, which contradicts the elapsed-freeze policy below.
            # The post-publish CLI review pane activates after every other
            # pane has gone idle, so it feeds the tick too; otherwise its
            # loader would never animate during the only window it runs in.
            cli_review_file_age: float | None = None
            if self.cli_reviewer in ("codex", "claude", "auto"):
                cli_review_file_age = _file_age(
                    self.paths["claude_review_raw_export"]
                ) or _file_age(self.paths["codex_review_raw_export"])
            cli_review_phase_live = cli_review_started and not (
                cli_review_completed or cli_review_failed
            )
            cli_review_running_state = _activity_state(
                container_present=cli_review_phase_live,
                file_age=cli_review_file_age,
            )
            if agent_state != "idle" or sim_state != "idle" or cli_review_running_state != "idle":
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
            agent_pane.border_title = f"CODING AGENT ({self.agent_model.display()})"
            sim_pane.border_title = f"SIM ({self.sim_model.display()})"
            active = None if terminal else self._determine_active()
            # During the cli_review phase, the post-publish reviewer owns
            # the highlight even before its first stdout chunk lands.
            # Otherwise `_determine_active` would keep returning the
            # previous freshest writer (the SIM reviewer that just APPROVED
            # the publish), so the yellow border would lag behind the
            # actual focus for the codex/claude spin-up window.
            if cli_review_started and not (cli_review_completed or cli_review_failed):
                active = "cli_review"
            # Wink on active-pane changes — small visual cue that focus
            # just shifted. 3 ticks ≈ 600ms at the default 5Hz refresh,
            # long enough to register without looking glitchy. Sentinel
            # init suppresses the wink on the very first render.
            if self._prev_active is not _UNSET_ACTIVE and active != self._prev_active:
                self._wink_ticks_remaining = 3
            self._prev_active = active
            agent_pane.set_class(active == "agent", "active")
            sim_pane.set_class(active == "sim", "active")

            # ----- CLI review column (only when configured) -----
            # `cli_reviewer` is the operator's choice from launch screen
            # ("codex" / "claude" / "auto"); `_cli_review_tool` is what
            # the JSONL actually shows once chunks land — used as the
            # title-flip hint once the active tool is known.
            if self.cli_reviewer in ("codex", "claude", "auto"):
                cli_events = _read_jsonl(self.paths["claude_review_raw_export"]) + _read_jsonl(
                    self.paths["codex_review_raw_export"]
                )
                tool_label = (self._cli_review_tool or self.cli_reviewer).upper()
                # Pane state mirrors the file-age semantics used elsewhere:
                # `active` while a chunk arrived in the last 2s, otherwise
                # `idle`. No container backs this — the CLI runs in Docker.
                cli_file_age = (
                    _file_age(self.paths["claude_review_raw_export"])
                    if self._cli_review_tool == "claude"
                    else _file_age(self.paths["codex_review_raw_export"])
                )
                cli_state = _activity_state(
                    container_present=cli_review_phase_live,
                    file_age=cli_file_age,
                )
                self.query_one("#cli-review-sub", Static).update(
                    _render_pane_subheader(
                        state=cli_state,
                        spinner=spinner,
                        turns=_text_event_count(cli_events),
                        pending_tool=None,
                        container_id=None,
                        container_uptime=None,
                    )
                )
                cli_pane = self.query_one("#cli-review-pane")
                cli_pane.border_title = f"{tool_label} PR REVIEW (post-publish)"
                cli_pane.set_class(cli_review_phase_live or active == "cli_review", "active")

            # ----- Activity panel title -----
            if terminal and self._frozen_gr_age is not None:
                gr_age: float | None = self._frozen_gr_age
            else:
                gr_age = _file_age(self.paths["guardrail_events"])
            age_str = f" (last write {_fmt_elapsed(gr_age)} ago)" if gr_age is not None else ""
            self.query_one("#activity-panel").border_title = f"orchestrator activity{age_str}"

            # ----- Footer -----
            # One-line layout:
            #   [trail]   [phase label + sub-info]   [↶ R<N> changes_req?]   [warnings?]   [time · cost]   [verdict]
            # Plus a 2nd-row #links-line that only appears at terminal state
            # and shows plain (cmd+clickable) URLs to the PR and viewer.

            stats_data: dict = {}
            if terminal:
                try:
                    stats_data = json.loads(stats_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass

            tv: str | None = stats_data.get("verdict") if terminal else None
            provider_failure = _provider_fast_fail(guardrails)
            pr_data = _read_pr_json(self.run_dir) or {}
            pr_url = pr_data.get("url") if pr_data.get("kind") == "PUBLISHED" else None
            pr_number = _pr_number_from_url(pr_url)
            pr_title = pr_data.get("title") if pr_data.get("kind") == "PUBLISHED" else None

            agent_started = any(
                g.get("event") == "opencode_actor_start" and g.get("role") == "agent"
                for g in guardrails
            )
            # WORK-loop SIM only — `role=sim`. Review-pass SIM is a separate
            # role and gates the Implementing → Reviewing transition below.
            sim_started = any(
                g.get("event") == "opencode_actor_start" and g.get("role") == "sim"
                for g in guardrails
            )
            review_started = any(
                g.get("event") == "opencode_actor_start" and g.get("role") == "review"
                for g in guardrails
            )
            architecture_review_done = _architecture_review_in(agent_events)
            # cli_review_started / _completed / _failed hoisted above.
            phase, color_state = _derive_phase(
                terminal=terminal,
                terminal_verdict=tv,
                settled=settled,
                impl_complete=impl_complete,
                agent_started=agent_started,
                architecture_review_done=architecture_review_done,
                sim_started=sim_started,
                review_started=review_started,
                cli_review_started=cli_review_started,
                cli_review_completed=cli_review_completed,
                cli_review_failed=cli_review_failed,
            )

            # Phase counters mirror flow_use.compute_phases so footer matches eval.
            review_cycles = _read_jsonl(self.paths["review_cycles"])
            test_runs = _read_jsonl(self.paths["test_runs"])
            phase_counts = _compute_phases_for_tui(
                self.paths, agent_events, guardrails, review_cycles
            )

            # Verdict zone text. `attached` covers read-only TUI on an
            # in-progress run; terminal badges come from TerminalVerdict
            # (models.py). exited-0 without stats means stats wasn't written
            # yet (race) — fall back to a neutral label.
            rc = self.proc.poll() if self.proc else None
            if terminal and tv:
                status_text, status_style = _terminal_badge(tv, pr_number, provider_failure)
            elif self.proc is None:
                status_text, status_style = "attached", _PAL_TEXT
            elif rc is None:
                status_text, status_style = "running", f"bold {_PAL_BRIGHT}"
            elif rc == 0:
                status_text, status_style = "exited 0", f"bold {_PAL_SUCCESS}"
            else:
                status_text, status_style = f"exited {rc}", f"bold {_PAL_ERROR}"

            # Per-reviewer status for the Reviewing / CLI-review / Done
            # phase labels. Computed once the review phase has started so
            # the verdict glyphs stay visible through cli_review and done
            # (used to disappear at terminal — losing the most scannable
            # "did every reviewer agree" signal right when the operator
            # wants to verify).
            if review_started:
                current_review_round = _current_review_round(guardrails)
                sim_review_statuses = [
                    _reviewer_status(
                        round_n=r,
                        review_cycles=review_cycles,
                        guardrails=guardrails,
                    )
                    for r in range(1, current_review_round + 1)
                ]
            else:
                current_review_round = 0
                sim_review_statuses = []

            # CLI-review aggregate status derived from per-tool states.
            # Only fed into the single-tool legacy path (`cli_review_status`)
            # for back-compat; `cli_review_states` is the multi-tool
            # source of truth fed to the footer renderer directly.
            cli_review_status: str | None = None
            if self.cli_reviewer in ("codex", "claude", "auto"):
                if cli_review_failed:
                    cli_review_status = "failed"
                elif cli_review_completed:
                    cli_review_status = "completed"
                elif cli_review_started:
                    cli_review_status = "streaming"

            # Left segment: trail + phase label + persistent review token + warnings.
            left = Text()
            left.append(_phase_trail(phase, color_state))
            left.append("   ")
            left.append(
                _current_phase_label(
                    phase=phase,
                    color_state=color_state,
                    grilling_exchanges=phase_counts["grilling_exchanges"],
                    impl_turns=phase_counts["impl_turns"],
                    self_verified=self_verified,
                    impl_complete=impl_complete,
                    review_cycles=review_cycles,
                    terminal_verdict=tv,
                    pr_number=pr_number,
                    pr_title=pr_title,
                    current_review_round=current_review_round,
                    sim_review_statuses=sim_review_statuses,
                    cli_review_status=cli_review_status,
                    cli_review_tool=self._cli_review_tool
                    or (self.cli_reviewer if self.cli_reviewer in ("codex", "claude") else ""),
                    cli_review_verdict=cli_review_verdict,
                    cli_review_states=cli_review_states if cli_review_states else None,
                    provider_failure=provider_failure,
                )
            )
            rev_token = _persistent_review_token(phase=phase, review_cycles=review_cycles)
            if rev_token is not None:
                left.append("     ")
                left.append(rev_token)
            warn_token = _warnings_token(
                recoveries=recoveries,
                test_runs=test_runs,
            )
            if warn_token is not None:
                left.append("     ")
                left.append(warn_token)

            # Right segment: elapsed · cost · verdict. Hosted in a separate
            # Static with `width: auto` so the horizontal layout pushes it
            # to the right edge regardless of left-segment length.
            right = Text()
            right.append(_fmt_elapsed(elapsed), style=_PAL_DIM)
            meter_tokens = _footer_meter_tokens(
                agent_model=self.agent_model,
                sim_model=self.sim_model,
                agent_events=agent_events,
                sim_events=sim_events,
                codex_usage=self._codex_usage,
                claude_usages=self._claude_usages,
                free_wink=self._wink_ticks_remaining > 0,
            )
            if self._wink_ticks_remaining > 0:
                self._wink_ticks_remaining -= 1
            for meter_token in meter_tokens:
                right.append(" · ", style=_PAL_VDIM)
                right.append(meter_token)
            right.append("     ")
            right.append(status_text, style=status_style)

            self.query_one("#footer-left", Static).update(left)
            self.query_one("#footer-right", Static).update(right)

            # ----- Links line (terminal state only) -----
            # Uses Textual's `Link` widget — it intercepts the click
            # itself and opens the URL via `webbrowser.open()`. Doesn't
            # depend on terminal OSC 8 support, so it works uniformly in
            # Apple Terminal, Ghostty, iTerm2, kitty, wezterm, etc.
            # (Previous OSC 8 approach was unreliable: Textual's mouse
            # capture in alt-screen mode can swallow clicks before the
            # terminal's OSC 8 handler sees them.)
            viewer = self.run_dir / "viewer.html"
            has_viewer = viewer.exists()
            self._final_pr_url = pr_url if terminal else None
            self._final_viewer_path = str(viewer.resolve()) if (terminal and has_viewer) else None
            pr_link = self.query_one("#pr-link", Link)
            viewer_link = self.query_one("#viewer-link", Link)
            if terminal and pr_url:
                pr_link.text = f"↗ PR  {pr_url}"
                pr_link.url = pr_url
                pr_link.set_class(False, "hidden")
            else:
                pr_link.set_class(True, "hidden")
            if terminal and has_viewer:
                viewer_url = f"file://{viewer.resolve()}"
                viewer_link.text = f"↗ viewer  {viewer_url}"
                viewer_link.url = viewer_url
                viewer_link.set_class(False, "hidden")
            else:
                viewer_link.set_class(True, "hidden")
            self.query_one("#links-line").set_class(
                not (terminal and (pr_url or has_viewer)), "hidden"
            )

        def action_quit(self) -> None:
            """Ctrl+C: ack visibly, then drain the orchestrator off-thread.

            Used to call `proc.wait(timeout=10)` directly here, which blocked
            the Textual event loop for up to 10 seconds — `_tick` couldn't
            fire, the screen froze, and the SIGTERM_EMERGENCY_WRITE +
            WORKTREE_REMOVED events the orchestrator emits during shutdown
            never rendered. From the operator's seat it looked bugged.

            Now: write an immediate ack to the activity log so the keystroke
            is acknowledged, then run the wait on a background thread.
            `_tick`'s set_interval keeps firing and renders shutdown events
            live (`SIGTERM_EMERGENCY_WRITE`, `worktree_removed`, etc.).
            """

            if self.proc is None or self.proc.poll() is not None:
                # Read-only attach, or proc already exited — close immediately.
                self.exit(self.proc.returncode if self.proc else 0)
                return
            self.query_one("#activity-log", RichLog).write(
                Text(
                    "⏹ ctrl+c — sending SIGTERM, draining containers + writing stats…",
                    style=f"bold {_PAL_WARN}",
                )
            )
            threading.Thread(target=self._shutdown_worker, daemon=True).start()

        def _shutdown_worker(self) -> None:
            """Drain the orchestrator subprocess; called on a background thread.

            `proc.wait` here is safe because we're off the Textual event
            loop. When the orchestrator exits, we marshal `self.exit(...)`
            back onto the main thread via `call_from_thread`.
            """

            proc = self.proc
            if proc is None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            self.call_from_thread(self.exit, proc.returncode)


# ---------- Entry points called from cli.py ----------


def _require_textual() -> None:
    if not _TEXTUAL_AVAILABLE:
        raise SystemExit(
            "contremaitre tui requires textual.\n"
            "Install with: python3 -m pip install --user textual"
        )


def _print_final_urls(app: "ContremaitreTUI") -> None:
    """Print PR + viewer URLs to stdout after TUI exit.

    Inside the TUI, Apple Terminal can't render OSC 8 hyperlinks (no
    support) and Textual's mouse capture eats clicks on plain URLs too.
    Printing the URLs after the TUI tears down puts them in the
    terminal's scrollback as plain text, where every modern terminal's
    URL-detection picks them up for cmd+click.
    """

    pr_url = app._final_pr_url
    viewer_path = app._final_viewer_path
    if not pr_url and not viewer_path:
        return
    print()
    if pr_url:
        print(f"  PR:     {pr_url}")
    if viewer_path:
        print(f"  Viewer: file://{viewer_path}")


def attach(run_dir: Path, *, refresh_hz: float = 5.0) -> int:
    """Mount the TUI on an existing run directory (read-only)."""

    _require_textual()
    run_dir = run_dir.resolve()
    if not run_dir.exists():
        raise SystemExit(f"run dir does not exist: {run_dir}")
    (
        agent_model,
        sim_model,
        cli_reviewer,
        docker_image,
        target_url,
        base,
    ) = _read_run_models(run_dir)
    app = ContremaitreTUI(
        run_dir,
        agent_model=agent_model,
        sim_model=sim_model,
        cli_reviewer=cli_reviewer,
        docker_image=docker_image,
        target_url=target_url,
        base=base,
        proc=None,
        refresh_hz=refresh_hz,
    )
    rc = app.run() or 0
    _print_final_urls(app)
    return rc


def spawn_and_attach(
    runs_root: Path,
    run_slug: str,
    run_cmd: list[str],
    *,
    refresh_hz: float = 5.0,
    discover_timeout_s: float = 30.0,
    agent_model: ModelSpec | None = None,
    sim_model: ModelSpec | None = None,
    cli_reviewer: str = "none",
    docker_image: str = "?",
    target_url: str | None = None,
    base: str | None = None,
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
        cli_reviewer=cli_reviewer,
        docker_image=docker_image,
        target_url=target_url,
        base=base,
        proc=proc,
        refresh_hz=refresh_hz,
    )
    rc = app.run() or 0
    _print_final_urls(app)
    return rc


def _read_run_models(
    run_dir: Path,
) -> tuple[ModelSpec, ModelSpec, str, str, str | None, str | None]:
    """Return (agent_model, sim_model, cli_reviewer, docker_image, target_url, base).

    Prefers `run_config.json` (written at orchestrator start — available
    immediately, even for attach against an in-flight run). Falls back to
    `stats.json` (terminal only) for older runs that pre-date the snapshot.
    Model identity routes through `ModelSpec.from_record`, which absorbs both
    the canonical dict and any legacy on-disk string — this reader never sees
    the old shape.
    """

    config = run_dir / "run_config.json"
    if config.exists():
        try:
            d = json.loads(config.read_text(encoding="utf-8"))
            return (
                ModelSpec.from_record(d.get("agent_model")),
                ModelSpec.from_record(d.get("sim_model")),
                d.get("cli_reviewer", "none"),
                d.get("docker_image", "?"),
                d.get("target_url"),
                d.get("base"),
            )
        except (OSError, json.JSONDecodeError):
            pass
    stats = run_dir / "stats.json"
    if stats.exists():
        try:
            d = json.loads(stats.read_text(encoding="utf-8"))
            return (
                ModelSpec.from_record(d.get("agent_model")),
                ModelSpec.from_record(d.get("sim_model")),
                d.get("cli_reviewer", "none"),
                "?",
                None,
                None,
            )
        except (OSError, json.JSONDecodeError):
            pass
    return (_UNKNOWN_SPEC, _UNKNOWN_SPEC, "none", "?", None, None)
