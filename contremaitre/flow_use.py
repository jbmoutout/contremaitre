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
from pathlib import Path
from typing import Any

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
_DIFF_RE = re.compile(r"review_diff_round")
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
      raw_export, sim_raw_export, review_cycles.
    """
    agent_events = _read_jsonl(paths.raw_export)
    sim_events = _read_jsonl(paths.sim_raw_export)
    return {
        "schema": "flow_use v1",
        "agent": _agent_metrics(agent_events),
        "sim": _sim_metrics(sim_events, paths),
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

    wall_seconds = (
        round((events[-1]["timestamp"] - events[0]["timestamp"]) / 1000, 1)
        if len(events) > 1
        else 0
    )

    settled_event = _find_write_to(tool_calls, _SETTLED_RE)
    impl_event = _find_write_to(tool_calls, _IMPL_COMPLETE_RE)
    first_code_edit = _find_first_code_edit(tool_calls)

    t0 = events[0]["timestamp"] if events else 0
    time_to_settled = (
        round((settled_event["timestamp"] - t0) / 1000, 1) if settled_event else None
    )
    tokens_to_settled = _tokens_before(events, settled_event)

    settled_chars: int | None = None
    if settled_event:
        inp = _inp(settled_event)
        content = inp.get("content") or inp.get("newString") or ""
        settled_chars = len(content)

    settled_before_edit: bool | None = None
    if settled_event and first_code_edit:
        settled_before_edit = settled_event["timestamp"] < first_code_edit["timestamp"]
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
    sim_read_diff_partial = any(
        (_inp(e).get("limit") or 9999) < 200 for e in diff_reads
    )

    file_access = _count_file_accesses(tool_calls)
    convergence, breadth, _, _ = _convergence(file_access)

    # sim_useful_call_ratio: fraction of SIM grep outputs cited in verdict text.
    # Valid for SIM (verdict prose is the deliverable). Broken for agent side.
    sim_useful_ratio: float | None = None
    review_cycles = _read_jsonl(paths.review_cycles)
    if review_cycles:
        last = review_cycles[-1]
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
        inp = _inp(e)
        fp = inp.get("filePath") or inp.get("path") or ""
        if fp:
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
        target = inp.get("filePath") or inp.get("path") or inp.get("patchText") or ""
        if pattern.search(target):
            return e
    return None


def _find_first_code_edit(tool_calls: list[dict]) -> dict | None:
    """First write/edit to a path outside .contremaitre/."""
    for e in tool_calls:
        part = e.get("part") or {}
        if part.get("tool") not in ("write", "edit"):
            continue
        if (part.get("state") or {}).get("status") != "completed":
            continue
        fp = _inp(e).get("filePath") or _inp(e).get("path") or ""
        if fp and not _CONTREMAITRE_DIR_RE.search(fp):
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
    impl_ts = impl_event["timestamp"] if impl_event else float("inf")

    last_edit_ts = 0
    for e in tool_calls:
        part = e.get("part") or {}
        if part.get("tool") not in ("write", "edit"):
            continue
        fp = _inp(e).get("filePath") or _inp(e).get("path") or ""
        if fp and not _CONTREMAITRE_DIR_RE.search(fp):
            last_edit_ts = max(last_edit_ts, e["timestamp"])

    test_outputs: list[str] = []
    runtime_install = False

    for e in tool_calls:
        if _tool_name(e) != "bash":
            continue
        cmd = _inp(e).get("command") or ""
        if _RUNTIME_INSTALL_RE.search(cmd):
            runtime_install = True
        if (
            _TEST_CMD_RE.search(cmd)
            and last_edit_ts < e["timestamp"] < impl_ts
        ):
            test_outputs.append((e.get("part") or {}).get("state", {}).get("output") or "")

    if not test_outputs:
        return False, None, runtime_install

    all_pass = all(
        not _TEST_FAIL_RE.search(out) and not _ZERO_TESTS_RE.search(out)
        for out in test_outputs
    )
    return True, all_pass, runtime_install


def _grep_cited_in(grep_event: dict, verdict_text: str, min_len: int = 20) -> bool:
    output = str((grep_event.get("part") or {}).get("state", {}).get("output") or "")
    for line in output.split("\n"):
        line = line.strip()
        if len(line) >= min_len and line in verdict_text:
            return True
    return False
