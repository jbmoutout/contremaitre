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

import re
from typing import Any

from .artifact_signals import (
    SETTLED_RE,
    compute_phase_counts,
    compute_self_verification,
    detect_artifact_writes,
    timestamp_ms,
    tokens_before,
    tool_paths,
)
from .jsonlog import read_jsonl


# ---------------------------------------------------------------------------
# Regex patterns — all anchored to harness contracts, not model prose style
# ---------------------------------------------------------------------------

_DIFF_RE = re.compile(r"review_diff_round|(?:^|[/\\])diff\.patch$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_flow_use(paths: Any) -> dict[str, Any]:
    """Compute agent + SIM tool-use metrics for a completed run.

    `paths` is a RunPaths instance; fields used:
      raw_export, sim_raw_export, review_cycles, guardrail_events.
    """
    agent_events = read_jsonl(paths.raw_export)
    sim_events = read_jsonl(paths.sim_raw_export)
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
        agent_events = read_jsonl(paths.raw_export)
    guardrails_path = getattr(paths, "guardrail_events", None)
    guardrails = read_jsonl(guardrails_path) if guardrails_path else []
    cycles = read_jsonl(paths.review_cycles)
    counts = compute_phase_counts(agent_events, guardrails, cycles)

    return {
        "grilling_exchanges": counts.grilling_exchanges,
        "impl_turns": counts.impl_turns,
        "review_rounds": counts.review_rounds,
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

    event_times = [ts for e in events if (ts := timestamp_ms(e)) is not None]
    wall_seconds = (
        round((event_times[-1] - event_times[0]) / 1000, 1) if len(event_times) > 1 else 0
    )

    writes = detect_artifact_writes(events)
    settled_event = writes.settled_design
    impl_event = writes.implementation_complete
    first_code_edit = writes.first_code_edit

    t0 = event_times[0] if event_times else None
    settled_ts = settled_event.timestamp_ms if settled_event else None
    time_to_settled = (
        round((settled_ts - t0) / 1000, 1) if settled_ts is not None and t0 is not None else None
    )
    tokens_to_settled = tokens_before(events, settled_event)

    settled_chars: int | None = None
    if settled_event:
        settled_chars = settled_event.chars

    settled_before_edit: bool | None = None
    if settled_event and first_code_edit:
        first_edit_ts = first_code_edit.timestamp_ms
        settled_before_edit = (
            settled_ts < first_edit_ts
            if settled_ts is not None and first_edit_ts is not None
            else None
        )
    elif settled_event:
        settled_before_edit = True

    verification = compute_self_verification(events)

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
            "value": verification.self_verified,
            "extraction": "automatic",
        },
        "self_verify_output_suggests_pass": {
            "value": verification.output_suggests_pass,
            "extraction": "heuristic",
            "note": "Absence of FAILED/error: in bash output. Manual ratification on False.",
        },
        "runtime_install_required": {
            "value": verification.runtime_install_required,
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
        SETTLED_RE.search(_inp(e).get("filePath", "") or "")
        for e in tool_calls
        if _tool_name(e) == "read"
    )

    diff_reads = [
        e
        for e in tool_calls
        if _tool_name(e) == "read" and _DIFF_RE.search(_inp(e).get("filePath", "") or "")
    ]
    sim_read_diff = len(diff_reads) > 0
    sim_read_diff_partial = any(_read_limit(e) < 200 for e in diff_reads)

    file_access = _count_file_accesses(tool_calls)
    convergence, breadth, _, _ = _convergence(file_access)

    # sim_useful_call_ratio: fraction of SIM grep calls whose ARGS (pattern
    # or path/include/glob) are referenced in the verdict text. SIMs
    # paraphrase output but mention the search terms they ran; matching
    # output verbatim floored the metric at 0 across every observed run.
    # Valid for SIM only (verdict prose is the deliverable, not for agents).
    # Match against the SIM's own last verdict (not the extra reviewer's or
    # an `unavailable` marker row) so the ratio reflects SIM tool-use → SIM
    # verdict alignment.
    sim_useful_ratio: float | None = None
    review_cycles = read_jsonl(paths.review_cycles)
    sim_cycles = [
        c for c in review_cycles if c.get("reviewer", "sim") == "sim" and not c.get("unavailable")
    ]
    if sim_cycles:
        last = sim_cycles[-1]
        verdict_text = last.get("summary", "") + " ".join(last.get("checks_performed", []))
        grep_calls = [e for e in tool_calls if _tool_name(e) == "grep"]
        if grep_calls:
            cited = sum(1 for e in grep_calls if _grep_args_cited_in(e, verdict_text))
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
            "note": "Fraction of SIM grep calls whose args (pattern/path/include/glob) appear in verdict summary+checks_performed.",
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
        for fp in tool_paths(e):
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


def _read_limit(e: dict) -> int:
    raw = _inp(e).get("limit")
    if raw is None:
        return 9999
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 9999


_REGEX_METACHARS = re.compile(r"[\\^$.|?*+(){}\[\]]")


def _grep_args_cited_in(
    grep_event: dict,
    verdict_text: str,
    *,
    min_pattern_len: int = 3,
    min_path_len: int = 4,
) -> bool:
    """Did the SIM's verdict reference what this grep call searched for?

    Matches grep ARGUMENTS (pattern + path/include/glob) against verdict
    prose, not grep output lines. SIMs paraphrase output but reliably
    mention the search terms they ran. Try the pattern as a literal first;
    if it contains regex metacharacters, fall back to the longest
    literal fragment between metachars so that a SIM saying
    "all `_compile_` helpers" still credits a grep for `_compile_\\w+`.
    """
    inp = _inp(grep_event)
    pat = str(inp.get("pattern") or "")
    if pat and len(pat) >= min_pattern_len:
        if pat in verdict_text:
            return True
        fragments = _REGEX_METACHARS.split(pat)
        longest = max(fragments, key=len, default="")
        if len(longest) >= min_pattern_len and longest != pat and longest in verdict_text:
            return True
    for k in ("path", "include", "glob"):
        v = str(inp.get(k) or "")
        if v and len(v) >= min_path_len and v in verdict_text:
            return True
    return False
