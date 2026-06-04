"""flow_use.py — Agent + SIM tool-use observability for contremaitre runs.

Anchored to the four harness filesystem contracts in initial_prompt.md:
  1. .contremaitre/architecture-review.html   — agent writes HTML candidates
  2. .contremaitre/SETTLED_DESIGN.md          — write BEFORE first code edit
  3. bash test runner                          — must run BEFORE IMPLEMENTATION_COMPLETE
  4. .contremaitre/IMPLEMENTATION_COMPLETE     — written LAST

Replaces the evals-era tool_use.py for this harness.

Retired (broken for contremaitre):
  - time_to_first_candidate / tokens_to_first_candidate
    -> looked for "## N. " headers in prose; agent writes HTML cards + SETTLED file;
       always null regardless of model.
  - useful_call_ratio (agent side)
    -> checked tool output in agent final prose; refactor deliverable is the diff;
       terse models score 0, verbose models score high — model style artifact.

Retargeted:
  - time_to_first_candidate -> time_to_settled_design_seconds
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
from typing import Any, Callable

from .event_stream import (
    ParsedEvents,
    ParsedToolCall,
    ParsedStepFinish,
    parse_events,
    parse_guardrail_events,
)
from .extract import parse_apply_patch
from .harness import HarnessContracts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_flow_use(paths: Any) -> dict[str, Any]:
    """Compute agent + SIM tool-use metrics for a completed run.

    ``paths`` is a RunPaths instance; fields used:
      raw_export, sim_raw_export, review_cycles, guardrail_events.
    Signature unchanged so callers (evaluator.py, viewer/__init__.py) are
    unaffected by the internal refactor.
    """
    harness = HarnessContracts()
    agent_events = parse_events(paths.raw_export)
    sim_events = parse_events(paths.sim_raw_export)
    agent = _agent_metrics(agent_events, harness)
    return {
        "schema": "flow_use v1",
        "agent": agent,
        "sim": _sim_metrics(sim_events, paths, harness),
        "phases": compute_phases(paths, agent_events, harness),
    }


def compute_phases(
    paths: Any,
    agent_events: ParsedEvents | None = None,
    harness: HarnessContracts | None = None,
) -> dict[str, Any]:
    """Split the run into grilling / impl / review phases by counting actor turns.

    grilling = agent + SIM turns BEFORE SETTLED_DESIGN.md is written (design pass)
    impl     = agent turns from SETTLED write through IMPLEMENTATION_COMPLETE
    review   = SIM review rounds (from review_cycles.jsonl, deduped over retries)

    Anchored to ``opencode_actor_start`` in guardrail_events.jsonl (one start =
    one process invocation = one turn) and the SETTLED / IMPL_COMPLETE write
    timestamps in raw_export.jsonl. Surfaced live in the TUI footer Zone 3
    and rolled into the PR body lede.
    """
    if harness is None:
        harness = HarnessContracts()
    if agent_events is None:
        agent_events = parse_events(paths.raw_export)

    tool_calls = agent_events.tool_calls
    settled_event = _find_write_to(tool_calls, lambda t: harness.is_settled(t))
    impl_event = _find_write_to(tool_calls, lambda t: harness.is_impl_complete(t))
    settled_ms = settled_event.ts if settled_event else None
    impl_ms = impl_event.ts if impl_event else None

    guardrails_path = getattr(paths, "guardrail_events", None)
    guardrails = parse_guardrail_events(guardrails_path) if guardrails_path else []
    starts: list[tuple[float, str]] = []
    for g in guardrails:
        if g.event != "opencode_actor_start":
            continue
        ts = g.ts
        role = g.role
        if role not in ("agent", "sim", "review"):
            continue
        starts.append((ts, role))
    starts.sort()

    # Identify the impl-start turn: the agent turn whose lifetime contains
    # the SETTLED write (start_ts <= settled_ms < next_start_ts).
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
    impl_agent = sum(1 for ts, r in post if r == "agent" and (impl_ms is None or ts <= impl_ms))

    # max(round), not len(): with the extra reviewer enabled, review_cycles
    # carries two entries per round plus optional ``unavailable`` entries; the
    # round number is the canonical counter.
    cycles = _read_jsonl_safe(paths.review_cycles)
    review_rounds = max((e.get("round") or 0) for e in cycles) if cycles else 0

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


def _agent_metrics(events: ParsedEvents, harness: HarnessContracts) -> dict[str, Any]:
    tool_calls = events.tool_calls

    by_tool: dict[str, int] = {}
    for tc in tool_calls:
        by_tool[tc.tool] = by_tool.get(tc.tool, 0) + 1

    file_access = _count_file_accesses(tool_calls)
    re_reads = sum(max(0, n - 1) for n in file_access.values())
    convergence, breadth, distinct, total = _convergence(file_access)

    all_events_ts = sorted(
        [e.ts for e in events.tool_calls]
        + [e.ts for e in events.step_finishes]
        + [e.ts for e in events.text_events]
    )
    wall_seconds = (
        round((all_events_ts[-1] - all_events_ts[0]) / 1000, 1) if len(all_events_ts) > 1 else 0
    )

    settled_event = _find_write_to(tool_calls, lambda t: harness.is_settled(t))
    impl_event = _find_write_to(tool_calls, lambda t: harness.is_impl_complete(t))
    first_code_edit = _find_first_code_edit(tool_calls, harness)

    t0 = all_events_ts[0] if all_events_ts else None
    settled_ts = settled_event.ts if settled_event else None
    time_to_settled = (
        round((settled_ts - t0) / 1000, 1) if settled_ts is not None and t0 is not None else None
    )
    tokens_to_settled = _tokens_before(events.step_finishes, settled_event)

    settled_chars: int | None = None
    if settled_event:
        settled_chars = _settled_write_chars(settled_event)

    settled_before_edit: bool | None = None
    if settled_event and first_code_edit:
        settled_before_edit = (
            settled_event.ts < first_code_edit.ts
            if settled_event.ts is not None and first_code_edit.ts is not None
            else None
        )
    elif settled_event:
        settled_before_edit = True

    self_verified, self_verify_pass, runtime_install = _check_self_verification(
        tool_calls, impl_event, harness
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


def _sim_metrics(events: ParsedEvents, paths: Any, harness: HarnessContracts) -> dict[str, Any]:
    tool_calls = events.tool_calls

    if not tool_calls and not events.text_events:
        return {"available": False}

    by_tool: dict[str, int] = {}
    for tc in tool_calls:
        by_tool[tc.tool] = by_tool.get(tc.tool, 0) + 1

    sim_read_settled = any(
        harness.is_settled(tc.file_path or "")
        for tc in tool_calls
        if tc.tool == "read" and tc.file_path
    )

    diff_reads = [
        tc
        for tc in tool_calls
        if tc.tool == "read" and tc.file_path and harness.is_diff_path(tc.file_path)
    ]
    sim_read_diff = len(diff_reads) > 0
    sim_read_diff_partial = any(tc.limit is not None and tc.limit < 200 for tc in diff_reads)

    file_access = _count_file_accesses(tool_calls)
    convergence, breadth, _, _ = _convergence(file_access)

    sim_useful_ratio: float | None = None
    review_cycles = _read_jsonl_safe(paths.review_cycles)
    sim_cycles = [
        c for c in review_cycles if c.get("reviewer", "sim") == "sim" and not c.get("unavailable")
    ]
    if sim_cycles:
        last = sim_cycles[-1]
        verdict_text = last.get("summary", "") + " ".join(last.get("checks_performed", []))
        grep_calls = [tc for tc in tool_calls if tc.tool == "grep"]
        if grep_calls:
            cited = sum(1 for tc in grep_calls if _grep_args_cited_in(tc, verdict_text))
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


def _count_file_accesses(tool_calls: list[ParsedToolCall]) -> dict[str, int]:
    acc: dict[str, int] = {}
    for tc in tool_calls:
        for fp in _call_paths(tc):
            acc[fp] = acc.get(fp, 0) + 1
    return acc


def _call_paths(tc: ParsedToolCall) -> list[str]:
    """Extract all file paths referenced by a tool call.

    For ``apply_patch``, paths come from parsing the patch text. For all
    other tools, the single ``file_path`` is used.
    """
    if tc.tool == "apply_patch" and tc.patch_text:
        return [fp for _, fp, _ in parse_apply_patch(tc.patch_text)]
    if tc.file_path:
        return [tc.file_path]
    return []


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


def _find_write_to(
    tool_calls: list[ParsedToolCall], predicate: Callable[[str], bool]
) -> ParsedToolCall | None:
    """Find the first write/edit/apply_patch whose target satisfies *predicate*.

    The ``target`` checked is ``file_path`` for write/edit, or the raw patch
    text for apply_patch (matching existing behaviour where pattern is
    searched against the whole patch payload).
    """
    for tc in tool_calls:
        if tc.tool not in ("write", "edit", "apply_patch"):
            continue
        if tc.status != "completed":
            continue
        target = tc.file_path or tc.patch_text or ""
        if predicate(target):
            return tc
    return None


def _find_first_code_edit(
    tool_calls: list[ParsedToolCall], harness: HarnessContracts
) -> ParsedToolCall | None:
    """First write/edit/apply_patch to a path outside ``.contremaitre/``."""
    for tc in tool_calls:
        if tc.tool not in ("write", "edit", "apply_patch"):
            continue
        if tc.status != "completed":
            continue
        if any(not harness.is_contremaitre_dir(fp) for fp in _call_paths(tc)):
            return tc
    return None


def _tokens_before(
    step_finishes: list[ParsedStepFinish], target: ParsedToolCall | None
) -> int | None:
    if not target:
        return None
    total = 0
    for sf in step_finishes:
        if sf.ts >= target.ts:
            break
        total += sf.tokens
    return total


def _check_self_verification(
    tool_calls: list[ParsedToolCall],
    impl_event: ParsedToolCall | None,
    harness: HarnessContracts,
) -> tuple[bool, bool | None, bool]:
    """Return (self_verified, output_suggests_pass, runtime_install_required).

    self_verified: agent ran a test command between last code edit and
    IMPLEMENTATION_COMPLETE write.
    output_suggests_pass: heuristic — no FAILED/error: in any test output.
    runtime_install_required: agent had to install a runtime (container gap).
    """
    impl_ts = impl_event.ts if impl_event else float("inf")

    last_edit_ts: float | None = None
    for tc in tool_calls:
        if tc.tool not in ("write", "edit", "apply_patch"):
            continue
        if tc.ts is None:
            continue
        if any(not harness.is_contremaitre_dir(fp) for fp in _call_paths(tc)):
            last_edit_ts = tc.ts if last_edit_ts is None else max(last_edit_ts, tc.ts)

    test_outputs: list[str] = []
    runtime_install = False

    for tc in tool_calls:
        if tc.tool != "bash":
            continue
        cmd = tc.command or ""
        if harness.is_runtime_install(cmd):
            runtime_install = True
        if tc.ts is None:
            continue
        if (
            harness.is_test_command(cmd)
            and last_edit_ts is not None
            and last_edit_ts < tc.ts < impl_ts
        ):
            test_outputs.append(tc.output)

    if not test_outputs:
        return False, None, runtime_install

    all_pass = all(
        not harness.test_output_suggests_fail(out)
        and not harness.test_output_suggests_no_tests(out)
        for out in test_outputs
    )
    return True, all_pass, runtime_install


def _settled_write_chars(tc: ParsedToolCall) -> int:
    """Character count of the content written to SETTLED_DESIGN.md."""
    if tc.tool == "write":
        return len(tc.content or "")
    if tc.tool == "edit":
        return len(tc.new_string or "")
    if tc.tool == "apply_patch" and tc.patch_text:
        return sum(len(body) for _, _, body in parse_apply_patch(tc.patch_text))
    return 0


_REGEX_METACHARS = re.compile(r"[\\^$.|?*+(){}\[\]]")


def _grep_args_cited_in(
    grep_call: ParsedToolCall,
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
    "all ``_compile_`` helpers" still credits a grep for ``_compile_\\w+``.
    """
    pat = grep_call.pattern or ""
    if pat and len(pat) >= min_pattern_len:
        if pat in verdict_text:
            return True
        fragments = _REGEX_METACHARS.split(pat)
        longest = max(fragments, key=len, default="")
        if len(longest) >= min_pattern_len and longest != pat and longest in verdict_text:
            return True
    for k in ("path", "include", "glob"):
        v = str(grep_call.__dict__.get(k) or "")
        if v and len(v) >= min_path_len and v in verdict_text:
            return True
    return False


# ---------------------------------------------------------------------------
# Minimal JSONL reader (review_cycles only — shared via event_stream
# for raw_export)
# ---------------------------------------------------------------------------


def _read_jsonl_safe(path: Any) -> list[dict]:
    """Read a JSONL path, returning [] on missing file.

    Used for review_cycles.jsonl which may not exist in all test fixtures.
    """
    from pathlib import Path as _Path

    p = _Path(str(path)) if not isinstance(path, _Path) else path
    if not p.exists():
        return []
    import json

    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return []
    return [json.loads(line) for line in text.strip().split("\n") if line.strip()]
