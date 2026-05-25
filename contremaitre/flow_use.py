"""flow_use.py — Agent + SIM tool-use observability for contremaitre runs.

Anchored to the four harness filesystem contracts in initial_prompt.md:
  1. .contremaitre/architecture-review.html   — agent writes HTML candidates
  2. .contremaitre/SETTLED_DESIGN.md          — write BEFORE first code edit
  3. bash test runner                          — must run BEFORE IMPLEMENTATION_COMPLETE
  4. .contremaitre/IMPLEMENTATION_COMPLETE     — written LAST

Replaces the evals-era tool_use.py for this harness.

Retired (broken for contremaitre):
  - time_to_first_candidate / tokens_to_first_candidate
    → looked for "## N. " headers in prose; agent writes HTML cards + SETTLED file;
      always null regardless of model.
  - useful_call_ratio (agent side)
    → checked tool output in agent final prose; refactor deliverable is the diff;
      terse models score 0, verbose models score high — model style artifact.

Retargeted:
  - time_to_first_candidate → time_to_settled_design_seconds
    anchored to write of .contremaitre/SETTLED_DESIGN.md (harness contract,
    model-agnostic).

Added (agent): settled_write_before_first_code_edit, settled_design_chars,
  self_verified, self_verify_output_suggests_pass, runtime_install_required,
  implementation_complete_written.

Added (SIM): sim_read_settled, sim_read_diff, sim_read_diff_partial,
  sim_grep_count, sim_exploration_convergence, sim_useful_call_ratio.
  Note: sim_useful_call_ratio is valid for SIM (verdict prose is the
  deliverable) but broken for the agent side.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .extract import parse_apply_patch

# ---------------------------------------------------------------------------
# JSONL reader — inline until PR #1 (jsonlog.read_jsonl) lands on main
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
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


# ---------------------------------------------------------------------------
# Regex patterns — all anchored to harness contracts, not model prose style
# ---------------------------------------------------------------------------

_SETTLED_RE = re.compile(r"SETTLED_DESIGN", re.IGNORECASE)
_IMPL_COMPLETE_RE = re.compile(r"IMPLEMENTATION_COMPLETE")
_DIFF_RE = re.compile(r"review_diff_round|(?:^|[/\\])diff\.patch$", re.IGNORECASE)
_CONTREMAITRE_DIR_RE = re.compile(r"[/\\]?\.contremaitre[/\\]")

# Test runner patterns — what "self-verification" looks like in bash tool calls
_TEST_CMD_RE = re.compile(
    r"\bunittest\b|\bpytest\b|\btsc\b|npm\s+test|make\s+test|\bmypy\b|\bjest\b|\bvitest\b"
)
# Runtime install patterns — signal container config gap, not agent quality
_RUNTIME_INSTALL_RE = re.compile(r"apt-?get\s+install|pip\s+install\b|npm\s+install\b")

# Failure markers in test output (heuristic — manual ratification on fail)
_TEST_FAIL_RE = re.compile(r"\bFAILED\b|\berror:\s|\bfailed\b", re.IGNORECASE)
_ZERO_TESTS_RE = re.compile(
    r"0 passed|no tests ran|collected 0 items|Ran 0 tests", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_flow_use(paths: Any) -> dict[str, Any]:
    """Compute agent + SIM tool-use metrics for a completed run.

    `paths` is a RunPaths instance; fields used:
      raw_export, sim_raw_export, review_cycles, guardrail_events.
    """
    agent_events = _read_jsonl(paths.raw_export)
    sim_events = _read_jsonl(paths.sim_raw_export)
    agent = _agent_metrics(agent_events)
    return {
        "schema": "flow_use v1",
        "agent": agent,
        "sim": _sim_metrics(sim_events, paths),
        "phases": compute_phases(paths, agent_events),
    }


def compute_phases(paths: Any, agent_events: list[dict] | None = None) -> dict[str, Any]:
    """Split the run into grilling / impl / review phases by counting actor turns.

    grilling = agent + SIM turns BEFORE SETTLED_DESIGN.md is written (design pass)
    impl     = agent turns from SETTLED write through IMPLEMENTATION_COMPLETE
    review   = SIM review rounds (from review_cycles.jsonl, deduped over retries)

    Anchored to `opencode_actor_start` in guardrail_events.jsonl (one start =
    one process invocation = one turn) and the SETTLED / IMPL_COMPLETE write
    timestamps in raw_export.jsonl. Surfaced live in the TUI footer Zone 3
    and rolled into the PR body lede.
    """
    if agent_events is None:
        agent_events = _read_jsonl(paths.raw_export)
    tool_calls = [e for e in agent_events if e.get("type") == "tool_use"]
    settled_event = _find_write_to(tool_calls, _SETTLED_RE)
    impl_event = _find_write_to(tool_calls, _IMPL_COMPLETE_RE)
    settled_ms = _timestamp_ms(settled_event)
    impl_ms = _timestamp_ms(impl_event)

    guardrails_path = getattr(paths, "guardrail_events", None)
    guardrails = _read_jsonl(guardrails_path) if guardrails_path else []
    starts: list[tuple[float, str]] = []
    for g in guardrails:
        if g.get("event") != "opencode_actor_start":
            continue
        ts = _timestamp_ms(g)
        role = g.get("role")
        if ts is None or role not in ("agent", "sim", "review"):
            continue
        starts.append((ts, role))
    starts.sort()

    # Identify the impl-start turn: the agent turn whose lifetime contains
    # the SETTLED write (start_ts ≤ settled_ms < next_start_ts).
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
        pre = starts
        post = []
    else:
        pre = starts[:impl_start_idx]
        post = starts[impl_start_idx:]

    pre_settled_agent = sum(1 for _, r in pre if r == "agent")
    pre_settled_sim = sum(1 for _, r in pre if r == "sim")
    impl_agent = sum(
        1 for ts, r in post
        if r == "agent" and (impl_ms is None or ts <= impl_ms)
    )

    # max(round), not len(): with the extra reviewer enabled, review_cycles
    # carries two entries per round plus optional `unavailable` entries; the
    # round number is the canonical counter.
    cycles = _read_jsonl(paths.review_cycles)
    review_rounds = (
        max((e.get("round") or 0) for e in cycles) if cycles else 0
    )

    return {
        "pre_settled_agent_turns": pre_settled_agent,
        "pre_settled_sim_turns": pre_settled_sim,
        "grilling_exchanges": min(pre_settled_agent, pre_settled_sim),
        "impl_turns": impl_agent,
        "review_rounds": review_rounds,
    }


# ---------------------------------------------------------------------------
# Agent metrics
# ---------------------------------------------------------------------------


def _agent_metrics(events: list[dict]) -> dict[str, Any]:
    tool_calls = [e for e in events if e.get("type") == "tool_use"]

    by_tool: dict[str, int] = {}
    for e in tool_calls:
        t = _tool_name(e)
        by_tool[t] = by_tool.get(t, 0) + 1

    file_access = _count_file_accesses(tool_calls)
    re_reads = sum(max(0, n - 1) for n in file_access.values())
    convergence, breadth, distinct, total = _convergence(file_access)

    event_times = [ts for e in events if (ts := _timestamp_ms(e)) is not None]
    wall_seconds = (
        round((event_times[-1] - event_times[0]) / 1000, 1)
        if len(event_times) > 1
        else 0
    )

    settled_event = _find_write_to(tool_calls, _SETTLED_RE)
    impl_event = _find_write_to(tool_calls, _IMPL_COMPLETE_RE)
    first_code_edit = _find_first_code_edit(tool_calls)

    t0 = event_times[0] if event_times else None
    settled_ts = _timestamp_ms(settled_event)
    time_to_settled = (
        round((settled_ts - t0) / 1000, 1)
        if settled_ts is not None and t0 is not None
        else None
    )
    tokens_to_settled = _tokens_before(events, settled_event)

    settled_chars: int | None = None
    if settled_event:
        settled_chars = _write_chars(settled_event, _SETTLED_RE)

    settled_before_edit: bool | None = None
    if settled_event and first_code_edit:
        first_edit_ts = _timestamp_ms(first_code_edit)
        settled_before_edit = (
            settled_ts < first_edit_ts
            if settled_ts is not None and first_edit_ts is not None
            else None
        )
    elif settled_event:
        settled_before_edit = True

    self_verified, self_verify_pass, runtime_install = _check_self_verification(
        tool_calls, impl_event
    )

    return {
        "tool_call_count": {"value": len(tool_calls), "extraction": "automatic"},
        "by_tool": {"value": by_tool, "extraction": "automatic"},
        "file_re_reads": {"value": re_reads, "extraction": "automatic"},
        "context_pollution_events": {"value": re_reads, "extraction": "automatic"},
        "exploration_convergence": {
            "value": convergence,
            "distinct_files_touched": distinct,
            "total_file_accesses": total,
            "breadth_ratio": breadth,
            "extraction": "heuristic",
        },
        "wall_seconds_total": {"value": wall_seconds, "extraction": "automatic"},
        "time_to_settled_design_seconds": {
            "value": time_to_settled,
            "extraction": "automatic",
        },
        "tokens_to_settled_design": {
            "value": tokens_to_settled,
            "extraction": "automatic",
        },
        "settled_design_chars": {
            "value": settled_chars,
            "extraction": "automatic",
            "note": "< 200 may indicate placeholder write before actual design work",
        },
        "settled_write_before_first_code_edit": {
            "value": settled_before_edit,
            "extraction": "automatic",
        },
        "self_verified": {
            "value": self_verified,
            "extraction": "automatic",
        },
        "self_verify_output_suggests_pass": {
            "value": self_verify_pass,
            "extraction": "heuristic",
            "note": "Absence of FAILED/error: in bash output. Manual ratification on False.",
        },
        "runtime_install_required": {
            "value": runtime_install,
            "extraction": "automatic",
            "note": "apt-get/pip/npm install detected — container config gap, not agent quality.",
        },
        "implementation_complete_written": {
            "value": impl_event is not None,
            "extraction": "automatic",
        },
    }


# ---------------------------------------------------------------------------
# SIM metrics
# ---------------------------------------------------------------------------


def _sim_metrics(events: list[dict], paths: Any) -> dict[str, Any]:
    if not events:
        return {"available": False}

    tool_calls = [e for e in events if e.get("type") == "tool_use"]

    by_tool: dict[str, int] = {}
    for e in tool_calls:
        t = _tool_name(e)
        by_tool[t] = by_tool.get(t, 0) + 1

    sim_read_settled = any(
        _SETTLED_RE.search(_inp(e).get("filePath", "") or "")
        for e in tool_calls
        if _tool_name(e) == "read"
    )

    diff_reads = [
        e
        for e in tool_calls
        if _tool_name(e) == "read"
        and _DIFF_RE.search(_inp(e).get("filePath", "") or "")
    ]
    sim_read_diff = len(diff_reads) > 0
    sim_read_diff_partial = any(_read_limit(e) < 200 for e in diff_reads)

    file_access = _count_file_accesses(tool_calls)
    convergence, breadth, _, _ = _convergence(file_access)

    # sim_useful_call_ratio: fraction of SIM grep outputs cited in verdict text.
    # Valid for SIM (verdict prose is the deliverable). Broken for agent side.
    # Match against the SIM's own last verdict only (not the extra reviewer's
    # or an `unavailable` marker row) so the ratio reflects SIM tool-use → SIM
    # verdict alignment.
    sim_useful_ratio: float | None = None
    review_cycles = _read_jsonl(paths.review_cycles)
    sim_cycles = [
        c for c in review_cycles
        if c.get("reviewer", "sim") == "sim" and not c.get("unavailable")
    ]
    if sim_cycles:
        last = sim_cycles[-1]
        verdict_text = last.get("summary", "") + " ".join(last.get("checks_performed", []))
        grep_calls = [e for e in tool_calls if _tool_name(e) == "grep"]
        if grep_calls:
            cited = sum(1 for e in grep_calls if _grep_cited_in(e, verdict_text))
            sim_useful_ratio = round(cited / len(grep_calls), 3)

    return {
        "available": True,
        "sim_tool_call_count": {"value": len(tool_calls), "extraction": "automatic"},
        "sim_by_tool": {"value": by_tool, "extraction": "automatic"},
        "sim_read_settled": {"value": sim_read_settled, "extraction": "automatic"},
        "sim_read_diff": {"value": sim_read_diff, "extraction": "automatic"},
        "sim_read_diff_partial": {
            "value": sim_read_diff_partial,
            "extraction": "automatic",
            "note": "True if any diff read used limit < 200 — SIM may have reviewed partial diff",
        },
        "sim_grep_count": {"value": by_tool.get("grep", 0), "extraction": "automatic"},
        "sim_exploration_convergence": {
            "value": convergence,
            "breadth_ratio": breadth,
            "extraction": "heuristic",
        },
        "sim_useful_call_ratio": {
            "value": sim_useful_ratio,
            "extraction": "heuristic",
            "note": "Fraction of SIM grep outputs cited in verdict summary+checks_performed.",
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_name(e: dict) -> str:
    return (e.get("part") or {}).get("tool", "?")


def _inp(e: dict) -> dict:
    return ((e.get("part") or {}).get("state") or {}).get("input") or {}


def _count_file_accesses(tool_calls: list[dict]) -> dict[str, int]:
    acc: dict[str, int] = {}
    for e in tool_calls:
        for fp in _tool_paths(e):
            acc[fp] = acc.get(fp, 0) + 1
    return acc


def _convergence(file_access: dict[str, int]) -> tuple[str, float, int, int]:
    distinct = len(file_access)
    total = sum(file_access.values())
    breadth = round(distinct / total, 3) if total else 0
    if total == 0:
        label = "no_exploration"
    elif breadth >= 0.9:
        label = "narrowed"
    elif breadth >= 0.6:
        label = "mostly_narrowed"
    elif breadth >= 0.3:
        label = "mixed"
    else:
        label = "thrashed"
    return label, breadth, distinct, total


def _find_write_to(tool_calls: list[dict], pattern: re.Pattern) -> dict | None:
    for e in tool_calls:
        part = e.get("part") or {}
        if part.get("tool") not in ("write", "edit", "apply_patch"):
            continue
        if (part.get("state") or {}).get("status") != "completed":
            continue
        inp = _inp(e)
        target = (
            inp.get("filePath")
            or inp.get("path")
            or inp.get("patchText")
            or inp.get("patch")
            or ""
        )
        if pattern.search(str(target)):
            return e
    return None


def _find_first_code_edit(tool_calls: list[dict]) -> dict | None:
    """First write/edit/apply_patch to a path outside .contremaitre/."""
    for e in tool_calls:
        part = e.get("part") or {}
        if part.get("tool") not in ("write", "edit", "apply_patch"):
            continue
        if (part.get("state") or {}).get("status") != "completed":
            continue
        if any(not _CONTREMAITRE_DIR_RE.search(fp) for fp in _tool_paths(e)):
            return e
    return None


def _tokens_before(events: list[dict], target: dict | None) -> int | None:
    if not target:
        return None
    total = 0
    for e in events:
        if e is target:
            break
        if e.get("type") == "step_finish":
            total += (e.get("part") or {}).get("tokens", {}).get("total", 0)
    return total


def _check_self_verification(
    tool_calls: list[dict], impl_event: dict | None
) -> tuple[bool, bool | None, bool]:
    """Return (self_verified, output_suggests_pass, runtime_install_required).

    self_verified: agent ran a test command between last code edit and
    IMPLEMENTATION_COMPLETE write.
    output_suggests_pass: heuristic — no FAILED/error: in any test output.
    runtime_install_required: agent had to install a runtime (container gap).
    """
    impl_ts = _timestamp_ms(impl_event) if impl_event else float("inf")

    last_edit_ts: float | None = None
    for e in tool_calls:
        part = e.get("part") or {}
        if part.get("tool") not in ("write", "edit", "apply_patch"):
            continue
        event_ts = _timestamp_ms(e)
        if event_ts is None:
            continue
        if any(not _CONTREMAITRE_DIR_RE.search(fp) for fp in _tool_paths(e)):
            last_edit_ts = (
                event_ts if last_edit_ts is None else max(last_edit_ts, event_ts)
            )

    test_outputs: list[str] = []
    runtime_install = False

    for e in tool_calls:
        if _tool_name(e) != "bash":
            continue
        cmd = _inp(e).get("command") or ""
        if _RUNTIME_INSTALL_RE.search(cmd):
            runtime_install = True
        event_ts = _timestamp_ms(e)
        if event_ts is None:
            continue
        if (
            _TEST_CMD_RE.search(cmd)
            and last_edit_ts is not None
            and last_edit_ts < event_ts < impl_ts
        ):
            test_outputs.append((e.get("part") or {}).get("state", {}).get("output") or "")

    if not test_outputs:
        return False, None, runtime_install

    all_pass = all(
        not _TEST_FAIL_RE.search(out) and not _ZERO_TESTS_RE.search(out)
        for out in test_outputs
    )
    return True, all_pass, runtime_install


def _timestamp_ms(e: dict | None) -> float | None:
    if not e:
        return None
    raw = e.get("timestamp")
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            pass
    ts = e.get("ts")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


def _tool_paths(e: dict) -> list[str]:
    inp = _inp(e)
    tool = _tool_name(e)
    if tool == "apply_patch":
        patch = inp.get("patchText") or inp.get("patch") or ""
        return [fp for _, fp, _ in parse_apply_patch(str(patch))]
    fp = inp.get("filePath") or inp.get("path") or ""
    return [str(fp)] if fp else []


def _write_chars(e: dict, pattern: re.Pattern) -> int:
    inp = _inp(e)
    tool = _tool_name(e)
    if tool == "write":
        return len(inp.get("content") or "")
    if tool == "edit":
        return len(inp.get("newString") or "")
    if tool == "apply_patch":
        patch = inp.get("patchText") or inp.get("patch") or ""
        return sum(
            len(body)
            for _, fp, body in parse_apply_patch(str(patch))
            if pattern.search(fp)
        )
    return 0


def _read_limit(e: dict) -> int:
    raw = _inp(e).get("limit")
    if raw is None:
        return 9999
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 9999


def _grep_cited_in(grep_event: dict, verdict_text: str, min_len: int = 20) -> bool:
    output = str((grep_event.get("part") or {}).get("state", {}).get("output") or "")
    for line in output.split("\n"):
        line = line.strip()
        if len(line) >= min_len and line in verdict_text:
            return True
    return False
