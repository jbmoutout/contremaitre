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

from .extract import parse_apply_patch
from .jsonlog import event_ms


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
_ZERO_TESTS_RE = re.compile(r"0 passed|no tests ran|collected 0 items|Ran 0 tests", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_flow_use(
    *,
    agent_events: list[dict],
    sim_events: list[dict],
    guardrails: list[dict],
    review_cycles: list[dict],
) -> dict[str, Any]:
    """Compute agent + SIM tool-use metrics for a completed run.

    Pure over already-parsed event lists — `flow_use` does no file I/O. The
    `RunArtifacts` Artifact reader owns reading and composes this interpreter
    over its memoized streams (`RunArtifacts.flow_use()`); this keeps the edge
    `run_artifacts → flow_use` acyclic (flow_use never imports the reader).
    """
    return {
        "schema": "flow_use v1",
        "agent": _agent_metrics(agent_events),
        "sim": _sim_metrics(sim_events, review_cycles),
        "phases": compute_phases(agent_events, guardrails, review_cycles),
    }


def compute_phases(
    agent_events: list[dict],
    guardrails: list[dict],
    cycles: list[dict],
    *,
    live: bool = False,
) -> dict[str, Any]:
    """Split the run into grilling / impl / review phases by counting actor turns.

    Pure: operates on already-read event lists, no I/O. Post-hoc readers reach it
    through `RunArtifacts.phases()`; the live TUI calls this directly with `live=True`.

    grilling = agent + SIM turns BEFORE SETTLED_DESIGN.md is written (design pass)
    impl     = agent turns from SETTLED write through IMPLEMENTATION_COMPLETE
    review   = SIM review rounds (from review_cycles, deduped over retries)

    Anchored to `actor_start` in `guardrails` (one start = one process
    invocation = one turn) and the SETTLED / IMPL_COMPLETE write timestamps in
    `agent_events`. Surfaced live in the TUI footer Zone 3 and rolled into the
    PR body lede.

    `live` controls the SETTLED-absent case (see below): a live run before the
    SETTLED write wants the accruing grilling counter; a post-hoc run that never
    settled wants an honest "unknown".
    """
    # Mode-agnostic write detection: opencode `tool_use` + claude `assistant`
    # tool_use blocks. (Codex has no per-event timestamp — see below.)
    settled_event = _find_write_event(agent_events, _SETTLED_RE)
    impl_event = _find_write_event(agent_events, _IMPL_COMPLETE_RE)
    settled_ms = event_ms(settled_event)
    impl_ms = event_ms(impl_event)

    starts: list[tuple[float, str]] = []
    for g in guardrails:
        if g.get("event") != "actor_start":
            continue
        ts = event_ms(g)
        role = g.get("role")
        if ts is None or role not in ("agent", "sim", "review"):
            continue
        starts.append((ts, role))
    starts.sort()

    # max(round), not len(): retry / unavailable rows can make
    # review_cycles longer than the number of logical review rounds.
    # The round number is the canonical counter. Always recoverable.
    review_rounds = max((e.get("round") or 0) for e in cycles) if cycles else 0

    # The grilling/impl split needs at least one agent turn boundary, plus
    # either a recoverable SETTLED write timestamp OR (in a live run) the
    # accruing pre-SETTLED counter.
    #
    # `not has_agent_turn` is genuinely unrecoverable — codex logs no
    # per-event ts, pre-`actor-start` CLI runs log no agent start — so it is
    # None in both modes.
    #
    # `settled_ms is None` with agent turns present is the fork: live → emit
    # the partial grilling counter (all starts are pre-SETTLED); post-hoc →
    # None, the honest "unknown" the readouts render as "—" rather than the
    # min-of-total-turns garbage the old code returned for a never-settled run.
    has_agent_turn = any(role == "agent" for _, role in starts)
    unrecoverable = not has_agent_turn or (settled_ms is None and not live)
    if unrecoverable:
        return {
            "pre_settled_agent_turns": None,
            "pre_settled_sim_turns": None,
            "grilling_exchanges": None,
            "impl_turns": None,
            "review_rounds": review_rounds,
        }

    # Identify the impl-start turn: the agent turn whose lifetime contains
    # the SETTLED write (start_ts ≤ settled_ms < next_start_ts). When
    # settled_ms is None (a live run still grilling), no turn qualifies, so
    # every start stays pre-SETTLED and impl is empty.
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

    event_times = [ts for e in events if (ts := event_ms(e)) is not None]
    wall_seconds = (
        round((event_times[-1] - event_times[0]) / 1000, 1) if len(event_times) > 1 else 0
    )

    settled_event = _find_write_to(tool_calls, _SETTLED_RE)
    impl_event = _find_write_to(tool_calls, _IMPL_COMPLETE_RE)
    first_code_edit = _find_first_code_edit(tool_calls)

    t0 = event_times[0] if event_times else None
    settled_ts = event_ms(settled_event)
    time_to_settled = (
        round((settled_ts - t0) / 1000, 1) if settled_ts is not None and t0 is not None else None
    )
    tokens_to_settled = _tokens_before(events, settled_event)

    settled_chars: int | None = None
    if settled_event:
        settled_chars = _write_chars(settled_event, _SETTLED_RE)

    settled_before_edit: bool | None = None
    if settled_event and first_code_edit:
        first_edit_ts = event_ms(first_code_edit)
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


def _sim_metrics(events: list[dict], review_cycles: list[dict]) -> dict[str, Any]:
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
    # Match against the SIM's own last verdict (not an `unavailable` marker
    # row) so the ratio reflects SIM tool-use → SIM verdict alignment.
    sim_useful_ratio: float | None = None
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
            inp.get("filePath") or inp.get("path") or inp.get("patchText") or inp.get("patch") or ""
        )
        if pattern.search(str(target)):
            return e
    return None


# claude stream-json names its file-writing tools with leading caps.
_CLAUDE_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _find_write_event(events: list[dict], pattern: re.Pattern) -> dict | None:
    """First event that WRITES a path matching `pattern`, across runtimes.

    Handles opencode `tool_use` (write/edit/apply_patch) and claude
    `assistant` events whose `message.content[]` carries a `tool_use` block
    (Write/Edit/…). Both shapes carry an extractable timestamp, so the
    caller can time-anchor the phase split.

    Codex is intentionally NOT handled: its `--json` stream carries no
    per-event timestamp (and writes files via opaque `command_execution`
    bash), so even a detected write can't anchor a time-based split. The
    caller treats a missing settled event as "unknown" and emits None
    rather than a wrong number — see `compute_phases`.
    """

    for e in events:
        etype = e.get("type")
        if etype == "tool_use":  # opencode
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
        elif etype == "assistant":  # claude stream-json
            for block in (e.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") not in _CLAUDE_WRITE_TOOLS:
                    continue
                inp = block.get("input") or {}
                target = inp.get("file_path") or inp.get("path") or ""
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
    impl_ts = event_ms(impl_event) if impl_event else float("inf")

    last_edit_ts: float | None = None
    for e in tool_calls:
        part = e.get("part") or {}
        if part.get("tool") not in ("write", "edit", "apply_patch"):
            continue
        event_ts = event_ms(e)
        if event_ts is None:
            continue
        if any(not _CONTREMAITRE_DIR_RE.search(fp) for fp in _tool_paths(e)):
            last_edit_ts = event_ts if last_edit_ts is None else max(last_edit_ts, event_ts)

    test_outputs: list[str] = []
    runtime_install = False

    for e in tool_calls:
        if _tool_name(e) != "bash":
            continue
        cmd = _inp(e).get("command") or ""
        if _RUNTIME_INSTALL_RE.search(cmd):
            runtime_install = True
        event_ts = event_ms(e)
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
        not _TEST_FAIL_RE.search(out) and not _ZERO_TESTS_RE.search(out) for out in test_outputs
    )
    return True, all_pass, runtime_install


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
        return sum(len(body) for _, fp, body in parse_apply_patch(str(patch)) if pattern.search(fp))
    return 0


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
