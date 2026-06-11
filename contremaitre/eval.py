"""v0 regression canary for Contremaitre.

Reads case definitions from `golden_cases/<case_id>/case.toml`, runs each case
n times via real opencode mode against the pinned target+SHA, extracts a
**multi-layer scorecard** from the artifacts the orchestrator already writes,
aggregates the n samples into a cell, and compares the cell against a per-case
baseline.

The scorecard has two layers:

- **Headline** (drives pass/fail): cli_review_score, terminal_score,
  files_changed, loc_net_delta, review_rounds, cost_usd, wall_seconds.
- **Diagnostic** (per-tier rollups, informational): format compliance,
  discipline, review depth, cli_review finding breakdown, efficiency.

Signal is sourced from artifacts the orchestrator already produces — no LLM
judge is invoked. L2/L3 focused judges stay PENDING (see `evaluator.py`).

Conventions:
- Real opencode mode only. Fake-mode cases live under `smoke_cases/` and are
  not picked up by this module.
- Cases pin `target_url` + `base` (a ref) + `expected_base_sha` (the SHA the
  ref must resolve to). The cell aggregator refuses runs whose captured
  `base_sha` doesn't match the case's `expected_base_sha`.
- Run dirs are tagged `eval-<case_id>-<rep_index>` so `latest_n_runs_for_case`
  can find them without a separate registry.
- A baseline is only promotable when all contributing runs have
  `contremaitre_git_dirty=false` AND `cli_review_parse_ok=true`. If the
  reviewer's parser broke on any run, that's a reviewer-prompt regression,
  not a baseline candidate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import manifest_digest


GOLDEN_CASES_DIRNAME = "golden_cases"
CANARY_FILENAME = "canary.json"
MIN_BASELINE_N = 3

# Drift envelopes. Width is per-metric — loose for noisy panels, tight for spend.
_DRIFT_ENVELOPES = {
    "cli_review_score": 0.30,  # ≥0.30 = one full grade (Future AGI ≥3pt)
    "terminal_score": 0.0,  # any drop is a regression
    "files_changed": 0.50,  # n=3 is noisy; tighten once we have more samples
    "loc_net_delta": 0.50,
    "cost_usd": 0.20,  # per roadmap §2
    "wall_seconds": 0.30,
    # agent_discipline_score: currently unused — gate is temporarily disabled
    # in _compare_to_baseline because the sim_useful_call_ratio input was
    # zero-pinned by a stale matcher and pre-fix baselines are not comparable.
    # Restore the envelope_check call-site when baselines are regenerated.
    "agent_discipline_score": 0.20,
    # Demoted to diagnostic (still recorded in canary.json, no longer gated)
    # because each correlates strongly with another headline metric on the
    # available run corpus, causing "regression on N axes" overcount. The
    # measured correlations:
    #   review_rounds        — r≈+0.73 with wall_seconds, +0.85 with turns
    #   cli_findings_weighted — r≈-0.63 with cli_review_score
    #   cli_citation_density  — moderate corr with reviewer family, thin n
}


# ---------------------------------------------------------------------------
# Case + Config loading
# ---------------------------------------------------------------------------
#
# Task and configuration are decoupled. A *case* pins the input
# (target_url + base_sha + intent); a *config* pins the system being
# evaluated against that input (agent/SIM/reviewer models, prompts,
# publish_mode). One case can have many configs; cells and baselines are
# keyed by (case_id, config_name) so model swaps don't destroy the task
# trail and cross-config comparison is a first-class operation.
#
# Layout:
#   golden_cases/<case_id>/
#     case.toml                  # task fields only
#     configs/<config_name>.toml # one per (agent, sim, reviewer) combo
#     baselines/<config_name>.json
#
# Default config is just `<case_dir>/configs/default.toml`; if the operator
# doesn't specify --config, this is what runs.


DEFAULT_CONFIG_NAME = "default"


@dataclass(frozen=True)
class CaseDef:
    """The task being evaluated against — immutable input pinning."""

    case_id: str
    description: str
    target_url: str
    base: str  # the ref name (e.g. "eval/case-1")
    expected_base_sha: str | None


@dataclass(frozen=True)
class ConfigDef:
    """The system being evaluated — what we swap to test hypotheses."""

    name: str
    agent_model: str
    sim_model: str
    cli_reviewer: str  # "codex" | "claude" | "none"
    publish_mode: str = "gh"  # "gh" required for cli_reviewer to fire


def load_case(case_dir: Path) -> CaseDef:
    case_toml = case_dir / "case.toml"
    raw = tomllib.loads(case_toml.read_text(encoding="utf-8"))
    return CaseDef(
        case_id=raw["id"],
        description=raw.get("description", ""),
        target_url=raw["target_url"],
        base=raw["base"],
        expected_base_sha=raw.get("expected_base_sha"),
    )


def load_config(case_dir: Path, config_name: str) -> ConfigDef:
    config_path = case_dir / "configs" / f"{config_name}.toml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"config {config_name!r} not found at {config_path}; "
            f"available: {[p.stem for p in (case_dir / 'configs').glob('*.toml')] if (case_dir / 'configs').exists() else []}"
        )
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    models = raw.get("models", {})
    if "extra_reviewer_model" in models:
        raise ValueError(
            "extra_reviewer_model was removed; add a sibling config with a different "
            "sim_model instead"
        )
    return ConfigDef(
        name=config_name,
        agent_model=models["agent_model"],
        sim_model=models["sim_model"],
        cli_reviewer=models.get("cli_reviewer", "codex"),
        publish_mode=raw.get("publish_mode", "gh"),
    )


def list_configs(case_dir: Path) -> list[str]:
    configs_dir = case_dir / "configs"
    if not configs_dir.exists():
        return []
    return sorted(p.stem for p in configs_dir.glob("*.toml"))


def list_cases(project_root: Path) -> list[Path]:
    cases_root = project_root / GOLDEN_CASES_DIRNAME
    if not cases_root.exists():
        return []
    return sorted(p for p in cases_root.iterdir() if p.is_dir() and (p / "case.toml").exists())


def case_dir_for(project_root: Path, case_id: str) -> Path:
    return project_root / GOLDEN_CASES_DIRNAME / case_id


# ---------------------------------------------------------------------------
# Run a case (subprocess into `contremaitre run`)
# ---------------------------------------------------------------------------


def _gh_token() -> str | None:
    """Resolve a GITHUB_TOKEN for the subprocess.

    Prefer ambient env; fall back to `gh auth token`. Returns None when no
    token is available — the run will fail at the publisher boundary, which
    is the right place for that failure to surface.
    """

    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        proc = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def run_case(case: CaseDef, config: ConfigDef, *, runs_root: Path, rep_index: int = 1) -> Path:
    """Launch one opencode run for the (case, config) pair. Returns the run dir.

    Shells out to `python -m contremaitre run` so the entire production launch
    sequence (clone-cache, preflight, image-rebuild check, opencode-config
    synthesis, publisher) runs exactly as in a normal user invocation. The
    canary's job is to canary the production path, not a parallel one.

    Run slug includes both case_id and config_name so cells from different
    configs of the same case don't aggregate together silently. While the
    subprocess runs, this function polls `guardrail_events.jsonl` in the
    new run dir and emits short status lines (one per phase transition)
    to stderr.
    """

    slug = f"eval-{case.case_id}-{config.name}-{rep_index:02d}"
    cmd = [
        sys.executable,
        "-m",
        "contremaitre",
        "run",
        "--agent",
        "opencode",
        "--base",
        case.base,
        "--fork",
        case.target_url,
        "--run-slug",
        slug,
        "--agent-model",
        config.agent_model,
        "--sim-model",
        config.sim_model,
        "--cli-reviewer",
        config.cli_reviewer,
        "--publish-mode",
        config.publish_mode,
        "--allow-open-egress",
    ]
    env = dict(os.environ)
    token = _gh_token()
    if token:
        env["GITHUB_TOKEN"] = token

    runs_root.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in runs_root.iterdir() if p.is_dir()}
    started = time.monotonic()

    print(f"contremaitre eval: launching {' '.join(cmd[2:])}", file=sys.stderr)
    proc = subprocess.Popen(cmd, env=env)
    _watch_progress(proc, runs_root=runs_root, slug=slug, before=before, started=started)
    rc = proc.wait()

    after = {p.name for p in runs_root.iterdir() if p.is_dir()}
    new_dirs = [p for p in (runs_root / n for n in (after - before)) if slug in p.name]
    if rc != 0 and not new_dirs:
        raise RuntimeError(
            f"contremaitre run failed (rc={rc}) and produced no run dir for slug={slug}"
        )
    if not new_dirs:
        raise RuntimeError(f"no new run dir produced for slug={slug}")
    if len(new_dirs) > 1:
        raise RuntimeError(f"multiple new run dirs for slug={slug}: {new_dirs}")
    return new_dirs[0]


# Events worth surfacing as a progress line. Anything not in this dict is
# silently ignored — `progress` / `no_progress` fire several times per turn
# and would drown out the phase signal.
_PROGRESS_EVENTS = {
    "actor_start",
    "turn",
    "review_verdict",
    "revision_requested",
    "malformed_verdict",
    "host_commit_created",
    "implementation_complete_cleared",
    "hard_gates_checked",
    "publication_blocked",
    "published",
    "cli_review_started",
    "cli_review_completed",
    "cli_review_failed",
    "turn_cap",
    "wall_cap",
    "recorded_cost_cap",
    "no_progress_cap",
    "infra_failure",
    "provider_quota_exhausted",
}


def _fmt_progress(obj: dict[str, Any], elapsed: float, state: dict[str, int]) -> str | None:
    """Map one guardrail event to a short progress line. None to skip.

    `state` is a per-watcher mutable dict — used to maintain per-role turn
    counters (the orchestrator emits one `actor_start` per agent or
    SIM container launch; we count them rather than expecting a discrete
    `turn` event, which only lands in `timeline.jsonl`).
    """

    e = obj.get("event")
    if e not in _PROGRESS_EVENTS:
        return None
    minutes = f"{elapsed / 60:5.1f}m"
    if e == "actor_start":
        role = obj.get("role", "?")
        if role == "agent":
            state["agent_turns"] = state.get("agent_turns", 0) + 1
            return f"      [{minutes}] agent turn {state['agent_turns']}"
        if role == "sim":
            state["sim_turns"] = state.get("sim_turns", 0) + 1
            return f"      [{minutes}] sim   turn {state['sim_turns']}"
        return f"      [{minutes}] {role} container started"
    if e == "host_commit_created":
        return f"      [{minutes}] host commit ({obj.get('files', '?')} files)"
    if e == "review_verdict":
        return (
            f"      [{minutes}] round {obj.get('round', '?')} verdict: "
            f"{obj.get('verdict', '?')} ({obj.get('reviewer', 'sim')})"
        )
    if e == "revision_requested":
        return f"      [{minutes}] revision round {obj.get('round', '?')} requested"
    if e == "malformed_verdict":
        return f"      [{minutes}] SIM verdict malformed (retry {obj.get('attempt', '?')})"
    if e == "implementation_complete_cleared":
        return f"      [{minutes}] IMPLEMENTATION_COMPLETE cleared (revision)"
    if e == "hard_gates_checked":
        return f"      [{minutes}] hard gates: {'PASS' if obj.get('passed') else 'FAIL'}"
    if e == "publication_blocked":
        return f"      [{minutes}] publication BLOCKED: {obj.get('reason', '')}"
    if e == "published":
        url = obj.get("url") or "(stub)"
        return f"      [{minutes}] PR opened: {url}"
    if e == "cli_review_started":
        return f"      [{minutes}] cli_review starting ({obj.get('tool', '?')})"
    if e == "cli_review_completed":
        return f"      [{minutes}] cli_review done"
    if e == "cli_review_failed":
        return f"      [{minutes}] cli_review FAILED: {obj.get('reason', '')}"
    if e in {"turn_cap", "wall_cap", "recorded_cost_cap", "no_progress_cap"}:
        return f"      [{minutes}] CAP TRIPPED: {e}"
    if e == "infra_failure":
        return f"      [{minutes}] INFRA FAILURE: {obj.get('reason', '')}"
    if e == "provider_quota_exhausted":
        return f"      [{minutes}] provider quota exhausted"
    return None


_SPINNER_FRAMES = "|/-\\"


def _watch_progress(
    proc: subprocess.Popen,
    *,
    runs_root: Path,
    slug: str,
    before: set[str],
    started: float,
    poll_interval: float = 0.2,
) -> None:
    """Single-threaded polling: find the run dir, then tail its guardrail_events.

    Runs to completion when the subprocess exits. Tolerates partial writes
    by tracking the last byte position; only complete lines are dispatched.
    On a TTY, draws a one-line spinner between events so the operator knows
    the run is still alive; on non-TTY, skips the spinner and slows the
    poll loop down to once every 2s.
    """

    run_dir: Path | None = None
    last_pos = 0
    announced_dir = False
    state: dict[str, int] = {}
    is_tty = sys.stderr.isatty()
    spinner_idx = 0
    last_event_at = time.monotonic()
    # On non-TTY (logs, CI), the spinner is just noise; skip it and poll
    # at the slower cadence we used before.
    if not is_tty:
        poll_interval = 2.0

    def _clear_spinner_line() -> None:
        if is_tty:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

    def _emit_new_events() -> bool:
        nonlocal last_pos
        if run_dir is None:
            return False
        events_path = run_dir / "guardrail_events.jsonl"
        if not events_path.exists():
            return False
        try:
            with events_path.open("r", encoding="utf-8") as fp:
                fp.seek(last_pos)
                chunk = fp.read()
        except OSError:
            return False
        if not chunk:
            return False
        if chunk.endswith("\n"):
            complete_chunk = chunk
            consumed = len(chunk)
        else:
            last_nl = chunk.rfind("\n")
            if last_nl < 0:
                return False
            complete_chunk = chunk[: last_nl + 1]
            consumed = last_nl + 1
        last_pos += consumed
        emitted_any = False
        for line in complete_chunk.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = _fmt_progress(obj, time.monotonic() - started, state)
            if msg:
                if not emitted_any:
                    _clear_spinner_line()
                print(msg, file=sys.stderr, flush=True)
                emitted_any = True
        return emitted_any

    while proc.poll() is None:
        if run_dir is None:
            try:
                for p in runs_root.iterdir():
                    if p.is_dir() and p.name not in before and slug in p.name:
                        run_dir = p
                        break
            except OSError:
                pass
            if run_dir is not None and not announced_dir:
                _clear_spinner_line()
                print(f"      [  0.0m] run dir: {run_dir}", file=sys.stderr, flush=True)
                announced_dir = True
        if _emit_new_events():
            last_event_at = time.monotonic()
        if is_tty:
            spinner_idx = (spinner_idx + 1) % len(_SPINNER_FRAMES)
            elapsed = time.monotonic() - started
            idle = int(time.monotonic() - last_event_at)
            sys.stderr.write(
                f"\r      [{elapsed / 60:5.1f}m] {_SPINNER_FRAMES[spinner_idx]} "
                f"running... ({idle}s since last event)\033[K"
            )
            sys.stderr.flush()
        time.sleep(poll_interval)

    # Drain any final events written after the last poll.
    if run_dir is None:
        try:
            for p in runs_root.iterdir():
                if p.is_dir() and p.name not in before and slug in p.name:
                    run_dir = p
                    break
        except OSError:
            pass
    _emit_new_events()
    _clear_spinner_line()


# ---------------------------------------------------------------------------
# Per-run metric extraction
# ---------------------------------------------------------------------------


_REQUIRED_ARTIFACTS = (
    "run_config.json",
    "stats.json",
    "pr.json",
    "review_cycles.jsonl",
    "eval/pr_eval.json",
    "eval/flow_use.json",
)


_VERDICT_KEY_TO_SCORE = {
    "LOOKS_GOOD": 1.0,
    "NEEDS_ATTENTION": 0.5,
    "MUST_FIX": 0.0,
}
_VERDICT_KEYS = tuple(_VERDICT_KEY_TO_SCORE.keys())

_TERMINAL_TO_SCORE = {
    "READY_FOR_DRAFT_PR": 1.0,
    "PR_NEEDS_HUMAN": 0.5,
    "NO_PR_CHANGES_REQUESTED": 0.0,
    "NO_PR_NEEDS_HUMAN": 0.0,
    "FAILED_INFRA": -1.0,
}

# Conventional Comments labels emitted by cli_reviewer_prompt.md.
_FINDING_LABELS = ("issue", "suggestion", "nit", "question", "praise", "thought")
_FINDING_RE = re.compile(rf"\*\*({'|'.join(_FINDING_LABELS)}):\*\*", re.IGNORECASE)
# path:line reference inside a finding line. Matches `lib/foo.ts:42`,
# `src/bar.py:123`, `Cargo.toml:5` etc. Tightened to require a path segment
# (slash or dot) so it doesn't match plain "n:m" patterns.
_CITATION_RE = re.compile(r"\b[\w./-]+\.[a-zA-Z]\w*:\d+")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_safe(path: Path) -> Any:
    return _read_json(path) if path.exists() else None


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as fp:
        return sum(1 for line in fp if line.strip())


def _parse_cli_review(run_dir: Path, cli_reviewer: str) -> dict[str, Any]:
    """Extract verdict key + finding counts from the cli_reviewer output.

    Reads the raw stream (`<tool>_review_raw_export.jsonl`) or the posted file
    (`<tool>_review.md`) — whichever is on disk. The roadmap §5 prefers the
    raw stream because the posted file has a metadata header that can foil
    naive line-1 regexes.
    """

    if cli_reviewer == "none":
        return {
            "ran": False,
            "verdict_key": None,
            "verdict_score": None,
            "finding_count": None,
            "citation_count": None,
            "by_label": {},
            "parse_ok": None,
        }

    # Prefer the `.rerun` artifacts when an operator manually re-ran the
    # cli_reviewer after a first-attempt failure (e.g. provider 400, stream
    # truncation). The plain files are kept as-is by the orchestrator, so
    # without this preference the parser anchors on the broken first attempt
    # and reports parse_ok=false even when a clean review exists on disk.
    raw_rerun = run_dir / f"{cli_reviewer}_review_raw_export.rerun.jsonl"
    posted_rerun = run_dir / f"{cli_reviewer}_review.rerun.md"
    raw = raw_rerun if raw_rerun.is_file() else run_dir / f"{cli_reviewer}_review_raw_export.jsonl"
    posted = posted_rerun if posted_rerun.is_file() else run_dir / f"{cli_reviewer}_review.md"

    # Distinguish three cases: reviewer didn't run (no PR to review →
    # absent files), reviewer ran but parser failed (file exists, no
    # verdict key found), reviewer ran and parsed cleanly. The first
    # case must return parse_ok=None so the rate metric doesn't conflate
    # "reviewer prompt regressed" with "there was no PR".
    review_files_exist = raw.exists() or posted.exists()

    if not review_files_exist:
        return {
            "ran": False,
            "verdict_key": None,
            "verdict_score": None,
            "finding_count": None,
            "citation_count": None,
            "by_label": {},
            "parse_ok": None,
        }

    text = ""
    if raw.exists():
        # Reassemble the streamed message: each JSONL line is a chunk; just
        # collect the `text` fields if present, else fall back to posted.
        try:
            chunks = []
            for line in raw.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("text") or obj.get("message") or obj.get("delta")
                if isinstance(t, str):
                    chunks.append(t)
            text = "".join(chunks)
        except OSError:
            text = ""

    if not text and posted.exists():
        text = posted.read_text(encoding="utf-8")
        # The orchestrator prepends an H3 metadata header to the posted
        # file ("### reviewed by `codex` · `gpt-5.5` · 5m 23s"). We look
        # at the first 10 lines for the verdict key, so the header line
        # shifts but doesn't blind us — citation/finding counts use the
        # whole body and the header is benign.

    verdict_key = None
    if text:
        head = "\n".join(text.splitlines()[:10])
        for key in _VERDICT_KEYS:
            if key in head:
                verdict_key = key
                break

    parse_ok = verdict_key is not None
    finding_count = len(_FINDING_RE.findall(text)) if text else 0
    citation_count = len(_CITATION_RE.findall(text)) if text else 0
    by_label: dict[str, int] = {label: 0 for label in _FINDING_LABELS}
    if text:
        for match in _FINDING_RE.finditer(text):
            by_label[match.group(1).lower()] += 1

    return {
        "ran": bool(text),
        "verdict_key": verdict_key if parse_ok else ("PARSE_FAIL" if text else None),
        "verdict_score": _VERDICT_KEY_TO_SCORE.get(verdict_key) if parse_ok else None,
        "finding_count": finding_count,
        "citation_count": citation_count,
        "by_label": by_label,
        "parse_ok": parse_ok,
    }


# Severity weights for Conventional Comments labels. `issue` is the
# blocking-quality bucket per the cli_reviewer prompt; `suggestion` and
# `nit` are lighter. `question` doesn't imply a defect. `praise` /
# `thought` are positive-or-neutral so they're zero-weight too — we're
# measuring the magnitude of negative signal, not net sentiment.
_FINDING_SEVERITY_WEIGHTS = {
    "issue": 3,
    "suggestion": 2,
    "nit": 1,
    "question": 0,
    "praise": 0,
    "thought": 0,
}


def _weighted_findings(by_label: dict[str, int]) -> int:
    """Severity-weighted sum of findings.

    A MUST_FIX run with one critical `**issue:**` scores 3; a MUST_FIX with
    four `**nit:**` items scores 4. Both look identical when collapsed to
    `cli_review_score=0.0`, but they're meaningfully different signals.
    """

    return sum(count * _FINDING_SEVERITY_WEIGHTS.get(label, 0) for label, count in by_label.items())


def _citation_density(cli: dict[str, Any]) -> float | None:
    """Fraction of findings with a `path:line` citation. None when no findings.

    Grounded reviews (high density) cite the offending line; ungrounded
    reviews (low density) hand-wave. A drop across runs would suggest the
    reviewer is producing less-trustworthy output, independent of the
    verdict mix.
    """

    findings = cli.get("finding_count")
    if not findings:
        return None
    citations = cli.get("citation_count") or 0
    return citations / findings


_EXPLORATION_TO_SCORE = {
    "mostly_narrowed": 1.0,
    "narrowed": 1.0,
    "mixed": 0.5,
    "scattered": 0.0,
    "thrashed": 0.0,
}


def _agent_discipline_score(
    *,
    exploration: str | None,
    sim_useful_ratio: float | None,
    self_verified: bool | None,
) -> float | None:
    """Continuous [0, 1] composite over three independent discipline signals.

    Components:
    - `exploration`: flow_use's `exploration_convergence.value` —
      narrowed → 1.0, mixed → 0.5, scattered/thrashed → 0.0.
    - `sim_useful_ratio`: flow_use's `sim_useful_call_ratio` — fraction of
      SIM grep outputs cited in the verdict (already 0..1).
    - `self_verified`: scorecard boolean — did the agent run tests itself.

    Mean of available components. Returns None when no component resolves.
    The signal complements `cli_review_score` because all three measure
    things the cli_reviewer cannot see (process, not artifact).
    """

    parts: list[float] = []
    if isinstance(exploration, str):
        score = _EXPLORATION_TO_SCORE.get(exploration)
        if score is not None:
            parts.append(score)
    if isinstance(sim_useful_ratio, (int, float)):
        parts.append(max(0.0, min(1.0, float(sim_useful_ratio))))
    if isinstance(self_verified, bool):
        parts.append(1.0 if self_verified else 0.0)
    if not parts:
        return None
    return sum(parts) / len(parts)


def _diff_stats(run_dir: Path) -> dict[str, int | None]:
    """LoC + files_changed by parsing the latest `review_diff_round_<N>.diff`.

    The orchestrator writes one diff per review round; the highest-numbered
    file is the diff that drove the terminal verdict. Counts `+`/`-` lines
    excluding the `+++ `/`--- ` headers; files_changed by counting `diff
    --git` headers.
    """

    diffs = sorted(run_dir.glob("review_diff_round*.diff"))
    if not diffs:
        return {
            "files_changed": None,
            "loc_added": None,
            "loc_deleted": None,
            "loc_net_delta": None,
        }
    latest = diffs[-1]
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {
            "files_changed": None,
            "loc_added": None,
            "loc_deleted": None,
            "loc_net_delta": None,
        }

    added = 0
    deleted = 0
    files = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return {
        "files_changed": files,
        "loc_added": added,
        "loc_deleted": deleted,
        "loc_net_delta": added - deleted,
    }


def _sim_verdicts_parse_ok(run_dir: Path) -> bool:
    path = run_dir / "review_cycles.jsonl"
    if not path.exists():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if "verdict" not in obj or "confidence" not in obj:
                return False
    except (json.JSONDecodeError, OSError):
        return False
    return True


def _review_depth(run_dir: Path) -> dict[str, int]:
    path = run_dir / "review_cycles.jsonl"
    if not path.exists():
        return {"total_checks_performed": 0, "total_required_changes": 0, "rounds": 0}
    total_checks = 0
    total_changes = 0
    rounds: set[int] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            total_checks += len(obj.get("checks_performed") or [])
            total_changes += len(obj.get("required_changes") or [])
            r = obj.get("round")
            if isinstance(r, int):
                rounds.add(r)
    except (json.JSONDecodeError, OSError):
        pass
    return {
        "total_checks_performed": total_checks,
        "total_required_changes": total_changes,
        "rounds": len(rounds),
    }


@dataclass
class CanaryReport:
    case_id: str
    run_id: str
    run_dir: str
    system_digest: str
    input_digest: str
    base_sha: str | None
    contremaitre_git_dirty: bool | None
    headline: dict[str, Any]
    diagnostic: dict[str, Any]
    missing_artifacts: list[str]
    ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "system_digest": self.system_digest,
            "input_digest": self.input_digest,
            "base_sha": self.base_sha,
            "contremaitre_git_dirty": self.contremaitre_git_dirty,
            "headline": self.headline,
            "diagnostic": self.diagnostic,
            "missing_artifacts": self.missing_artifacts,
            "ok": self.ok,
        }


def _input_digest(case: CaseDef, config: ConfigDef, base_sha: str | None) -> str:
    """Hash that identifies the *input + judge choice* for the cell.

    Two runs share an input_digest iff they ran against the same target+SHA
    AND used the same cli_reviewer (the judge choice — different judge =
    different measurement, not different system). If the case author bumps
    `expected_base_sha`, runs from before that bump are not aggregatable
    into the new cell.
    """

    import hashlib

    parts = [case.target_url, case.base, base_sha or "", config.cli_reviewer]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def check_run(case: CaseDef, config: ConfigDef, run_dir: Path) -> CanaryReport:
    missing: list[str] = [rel for rel in _REQUIRED_ARTIFACTS if not (run_dir / rel).exists()]

    run_config = _read_json_safe(run_dir / "run_config.json") or {}
    stats = _read_json_safe(run_dir / "stats.json") or {}
    pr_eval = _read_json_safe(run_dir / "eval" / "pr_eval.json") or {}
    flow_use = _read_json_safe(run_dir / "eval" / "flow_use.json") or {}

    scorecard = (pr_eval.get("scorecard") or {}) if isinstance(pr_eval, dict) else {}
    flow_agent = (flow_use.get("agent") or {}) if isinstance(flow_use, dict) else {}
    flow_sim = (flow_use.get("sim") or {}) if isinstance(flow_use, dict) else {}

    def _flow_value(d: dict[str, Any], key: str) -> Any:
        v = d.get(key)
        if isinstance(v, dict) and "value" in v:
            return v["value"]
        return v

    cli = _parse_cli_review(run_dir, config.cli_reviewer)
    diff = _diff_stats(run_dir)
    depth = _review_depth(run_dir)

    terminal = stats.get("verdict") or pr_eval.get("verdict")
    terminal_score = _TERMINAL_TO_SCORE.get(terminal) if terminal else None

    # Severity-weighted finding count: Conventional Comments labels already
    # encode severity (issue >> suggestion >> nit). Sum with weights so a
    # MUST_FIX with one critical issue scores differently from a MUST_FIX
    # with four trivial nits. Praise/thought ignored — usually 0 anyway.
    cli_findings_weighted = (
        _weighted_findings(cli.get("by_label") or {}) if cli.get("ran") else None
    )
    cli_citation_density = _citation_density(cli)

    # Continuous orchestrator-side discipline composite. Per-run [0, 1] score
    # derived from three independent signals the cli_reviewer cannot see:
    # how convergently the agent explored, whether it self-verified, and
    # whether the SIM grounded its verdict in actual greps. Sensitive to
    # the kind of regression a single 3-state verdict can't catch.
    agent_discipline = _agent_discipline_score(
        exploration=(flow_agent.get("exploration_convergence") or {}).get("value"),
        sim_useful_ratio=_flow_value(flow_sim, "sim_useful_call_ratio"),
        self_verified=scorecard.get("self_verified"),
    )

    headline = {
        "cli_review_score": cli["verdict_score"],
        "cli_review_verdict_key": cli["verdict_key"],
        "cli_findings_weighted": cli_findings_weighted,
        "cli_issue_count": (cli.get("by_label") or {}).get("issue") if cli.get("ran") else None,
        "cli_suggestion_count": (cli.get("by_label") or {}).get("suggestion")
        if cli.get("ran")
        else None,
        "cli_nit_count": (cli.get("by_label") or {}).get("nit") if cli.get("ran") else None,
        "cli_citation_density": cli_citation_density,
        "agent_discipline_score": agent_discipline,
        "terminal_score": terminal_score,
        "terminal_verdict": terminal,
        "files_changed": diff["files_changed"],
        "loc_net_delta": diff["loc_net_delta"],
        "review_rounds": depth["rounds"],
        "cost_usd": stats.get("recorded_cost_usd"),
        "wall_seconds": stats.get("duration_seconds"),
    }

    diagnostic = {
        "format_compliance": {
            "cli_review_parse_ok": cli["parse_ok"],
            "sim_verdicts_parse_ok": _sim_verdicts_parse_ok(run_dir),
            "hard_gates_passed": pr_eval.get("hard_gates") == "PASS" if pr_eval else None,
            "implementation_complete_written": _flow_value(
                flow_agent, "implementation_complete_written"
            ),
        },
        "discipline": {
            "settled_before_code": scorecard.get("settled_before_code"),
            "self_verified": scorecard.get("self_verified"),
            "runtime_install_required": _flow_value(flow_agent, "runtime_install_required"),
            "context_pollution_events": _flow_value(flow_agent, "context_pollution_events"),
            "exploration_convergence": (flow_agent.get("exploration_convergence") or {}).get(
                "value"
            ),
            "time_to_settled_design_seconds": _flow_value(
                flow_agent, "time_to_settled_design_seconds"
            ),
            "tokens_to_settled_design": _flow_value(flow_agent, "tokens_to_settled_design"),
            "sim_useful_call_ratio": _flow_value(flow_sim, "sim_useful_call_ratio"),
        },
        "review_depth": {
            "total_checks_performed": depth["total_checks_performed"],
            "total_required_changes": depth["total_required_changes"],
            "sim_review_confidence": scorecard.get("sim_review_confidence"),
            "process_reliability": scorecard.get("process_reliability"),
        },
        "cli_review_breakdown": {
            "ran": cli["ran"],
            "finding_count": cli["finding_count"],
            "citation_count": cli["citation_count"],
            "by_label": cli["by_label"],
        },
        "diff_detail": {
            "loc_added": diff["loc_added"],
            "loc_deleted": diff["loc_deleted"],
            "files_changed": diff["files_changed"],
        },
        "efficiency": {
            "turns": stats.get("turns"),
            "agent_tool_call_count": _flow_value(flow_agent, "tool_call_count"),
            "sim_tool_call_count": _flow_value(flow_sim, "sim_tool_call_count"),
        },
    }

    base_sha = run_config.get("base_sha")
    # `run_config.json` is written at run START, so its model specs carry no
    # `resolved` (a claude account-default run reads as "?" there). `stats.json`
    # is written at run END with `resolved` back-filled, so prefer its identity
    # records for the digest — otherwise two account-default runs that resolved
    # to different models would collide into one system_digest.
    digest_manifest = dict(run_config) if run_config else {}
    for key in ("agent_model", "sim_model"):
        if isinstance(stats.get(key), dict):
            digest_manifest[key] = stats[key]
    system_digest = manifest_digest(digest_manifest) if run_config else ""
    input_digest = _input_digest(case, config, base_sha)

    # `ok` distinguishes "system behaved correctly" from "system broke".
    # READY_FOR_DRAFT_PR and PR_NEEDS_HUMAN both published a draft PR; the
    # latter is the valid yellow terminal for an exhausted post-publish CLI
    # review loop. NO_PR_NEEDS_HUMAN / NO_PR_CHANGES_REQUESTED are also valid
    # eval outcomes (the SIM legitimately rejected the diff). Only infra
    # failures, missing artifacts, protocol parse failures, and base-SHA drift
    # mark a run as not-ok.
    #
    # `hard_gates_passed` is only enforced when terminal == READY_FOR_DRAFT_PR:
    # if we published a PR, gates *must* have passed (else the pipeline is
    # inconsistent). For NO_PR_* terminals, gates may never have run; that's
    # fine.
    fc = diagnostic["format_compliance"]
    base_sha_ok = case.expected_base_sha is None or (
        base_sha and base_sha.startswith(case.expected_base_sha)
    )
    terminal_healthy = terminal in {
        "READY_FOR_DRAFT_PR",
        "PR_NEEDS_HUMAN",
        "NO_PR_CHANGES_REQUESTED",
        "NO_PR_NEEDS_HUMAN",
    }
    gates_consistent = (
        terminal
        not in {
            "READY_FOR_DRAFT_PR",
            "PR_NEEDS_HUMAN",
        }
        or fc["hard_gates_passed"] is True
    )
    ok = (
        not missing
        and bool(fc["sim_verdicts_parse_ok"])
        and (fc["cli_review_parse_ok"] is None or fc["cli_review_parse_ok"])
        and base_sha_ok
        and terminal_healthy
        and gates_consistent
    )

    return CanaryReport(
        case_id=case.case_id,
        run_id=stats.get("run_id") or run_dir.name,
        run_dir=str(run_dir),
        system_digest=system_digest,
        input_digest=input_digest,
        base_sha=base_sha,
        contremaitre_git_dirty=run_config.get("contremaitre_git_dirty"),
        headline=headline,
        diagnostic=diagnostic,
        missing_artifacts=missing,
        ok=ok,
    )


def write_canary_report(report: CanaryReport, run_dir: Path) -> Path:
    out = run_dir / "eval" / CANARY_FILENAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Cell aggregation
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    case_id: str
    n: int
    system_digests: list[str]
    input_digests: list[str]
    base_shas: list[str]
    any_run_dirty: bool
    all_runs_ok: bool
    headline: dict[str, Any]
    diagnostic: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "n": self.n,
            "system_digests": self.system_digests,
            "input_digests": self.input_digests,
            "base_shas": self.base_shas,
            "any_run_dirty": self.any_run_dirty,
            "all_runs_ok": self.all_runs_ok,
            "headline": self.headline,
            "diagnostic": self.diagnostic,
        }


def _median_range(values: list[Any]) -> dict[str, Any]:
    numeric = [v for v in values if isinstance(v, (int, float))]
    if not numeric:
        return {"median": None, "min": None, "max": None}
    return {"median": statistics.median(numeric), "min": min(numeric), "max": max(numeric)}


def _rate(values: list[Any]) -> float | None:
    bools = [v for v in values if isinstance(v, bool)]
    if not bools:
        return None
    return sum(1 for v in bools if v) / len(bools)


def _mix(values: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        if v is None:
            continue
        key = str(v)
        out[key] = out.get(key, 0) + 1
    return out


def aggregate_cell(reports: list[CanaryReport]) -> Cell:
    """Collapse n per-run reports into a cell summary.

    Numeric panels → median + min + max. Boolean panels → rate (fraction true).
    Categorical panels (verdict keys, exploration_convergence) → mix.
    """

    case_id = reports[0].case_id if reports else ""
    n = len(reports)

    def headline_panel(field: str) -> Any:
        return [r.headline.get(field) for r in reports]

    headline = {
        "cli_review_score": _median_range(headline_panel("cli_review_score")),
        "cli_review_verdict_mix": _mix(headline_panel("cli_review_verdict_key")),
        "cli_findings_weighted": _median_range(headline_panel("cli_findings_weighted")),
        "cli_issue_count": _median_range(headline_panel("cli_issue_count")),
        "cli_suggestion_count": _median_range(headline_panel("cli_suggestion_count")),
        "cli_nit_count": _median_range(headline_panel("cli_nit_count")),
        "cli_citation_density": _median_range(headline_panel("cli_citation_density")),
        "agent_discipline_score": _median_range(headline_panel("agent_discipline_score")),
        "terminal_score": _median_range(headline_panel("terminal_score")),
        "terminal_verdict_mix": _mix(headline_panel("terminal_verdict")),
        "files_changed": _median_range(headline_panel("files_changed")),
        "loc_net_delta": _median_range(headline_panel("loc_net_delta")),
        "review_rounds": _median_range(headline_panel("review_rounds")),
        "cost_usd": _median_range(headline_panel("cost_usd")),
        "wall_seconds": _median_range(headline_panel("wall_seconds")),
    }

    def diag_panel(group: str, field: str) -> list[Any]:
        return [r.diagnostic.get(group, {}).get(field) for r in reports]

    diagnostic = {
        "format_compliance": {
            "cli_review_parse_ok_rate": _rate(
                diag_panel("format_compliance", "cli_review_parse_ok")
            ),
            "sim_verdicts_parse_ok_rate": _rate(
                diag_panel("format_compliance", "sim_verdicts_parse_ok")
            ),
            "hard_gates_passed_rate": _rate(diag_panel("format_compliance", "hard_gates_passed")),
            "implementation_complete_written_rate": _rate(
                diag_panel("format_compliance", "implementation_complete_written")
            ),
        },
        "discipline": {
            "settled_before_code_rate": _rate(diag_panel("discipline", "settled_before_code")),
            "self_verified_rate": _rate(diag_panel("discipline", "self_verified")),
            "runtime_install_required_rate": _rate(
                diag_panel("discipline", "runtime_install_required")
            ),
            "context_pollution_events": _median_range(
                diag_panel("discipline", "context_pollution_events")
            ),
            "exploration_convergence_mix": _mix(
                diag_panel("discipline", "exploration_convergence")
            ),
            "time_to_settled_design_seconds": _median_range(
                diag_panel("discipline", "time_to_settled_design_seconds")
            ),
            "tokens_to_settled_design": _median_range(
                diag_panel("discipline", "tokens_to_settled_design")
            ),
            "sim_useful_call_ratio": _median_range(
                diag_panel("discipline", "sim_useful_call_ratio")
            ),
        },
        "review_depth": {
            "total_checks_performed": _median_range(
                diag_panel("review_depth", "total_checks_performed")
            ),
            "total_required_changes": _median_range(
                diag_panel("review_depth", "total_required_changes")
            ),
            "sim_review_confidence": _median_range(
                diag_panel("review_depth", "sim_review_confidence")
            ),
            "process_reliability": _median_range(diag_panel("review_depth", "process_reliability")),
        },
        "cli_review_breakdown": {
            "finding_count": _median_range(diag_panel("cli_review_breakdown", "finding_count")),
            "citation_count": _median_range(diag_panel("cli_review_breakdown", "citation_count")),
        },
        "diff_detail": {
            "loc_added": _median_range(diag_panel("diff_detail", "loc_added")),
            "loc_deleted": _median_range(diag_panel("diff_detail", "loc_deleted")),
        },
        "efficiency": {
            "turns": _median_range(diag_panel("efficiency", "turns")),
            "agent_tool_call_count": _median_range(
                diag_panel("efficiency", "agent_tool_call_count")
            ),
            "sim_tool_call_count": _median_range(diag_panel("efficiency", "sim_tool_call_count")),
        },
    }

    return Cell(
        case_id=case_id,
        n=n,
        system_digests=[r.system_digest for r in reports],
        input_digests=[r.input_digest for r in reports],
        base_shas=[r.base_sha or "" for r in reports],
        any_run_dirty=any(bool(r.contremaitre_git_dirty) for r in reports),
        all_runs_ok=all(r.ok for r in reports),
        headline=headline,
        diagnostic=diagnostic,
    )


def cell_from_dict(d: dict[str, Any]) -> Cell:
    return Cell(
        case_id=d["case_id"],
        n=d["n"],
        system_digests=list(d.get("system_digests", [])),
        input_digests=list(d.get("input_digests", [])),
        base_shas=list(d.get("base_shas", [])),
        any_run_dirty=bool(d.get("any_run_dirty", False)),
        all_runs_ok=bool(d.get("all_runs_ok", False)),
        headline=dict(d.get("headline", {})),
        diagnostic=dict(d.get("diagnostic", {})),
    )


# ---------------------------------------------------------------------------
# Locate runs for a case
# ---------------------------------------------------------------------------


def latest_n_runs_for_case(runs_root: Path, case_id: str, config_name: str, n: int) -> list[Path]:
    """Most recent n eval runs for a (case, config) pair, newest last.

    Slug format: `<timestamp>-eval-<case_id>-<config>-<rep>`. Both
    case_id and config_name must be underscore-only (no dashes) so the
    dash-separated parse is unambiguous.
    """

    if not runs_root.exists():
        return []

    pattern = re.compile(rf"-eval-{re.escape(case_id)}-{re.escape(config_name)}-\d+$")
    candidates = sorted(
        (p for p in runs_root.iterdir() if p.is_dir() and pattern.search(p.name)),
        key=lambda p: p.name,
    )
    return candidates[-n:] if len(candidates) >= n else candidates


# ---------------------------------------------------------------------------
# Compare against baseline
# ---------------------------------------------------------------------------


@dataclass
class CompareResult:
    case_id: str
    has_baseline: bool
    regressions: list[str]
    drifts: list[str]
    improvements: list[str]
    two_variable_warning: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "has_baseline": self.has_baseline,
            "regressions": self.regressions,
            "drifts": self.drifts,
            "improvements": self.improvements,
            "two_variable_warning": self.two_variable_warning,
        }

    @property
    def is_regression(self) -> bool:
        return bool(self.regressions)


def _median(panel: Any) -> float | None:
    if isinstance(panel, dict):
        v = panel.get("median")
        return v if isinstance(v, (int, float)) else None
    return None


def _envelope_check(
    name: str,
    cur: float | None,
    base: float | None,
    *,
    envelope: float,
    direction: str = "down",
) -> str | None:
    """Return a regression / drift string if cur is outside the envelope.

    `direction="down"` flags a drop; `direction="symmetric"` flags any
    movement outside ±envelope; envelope is a fraction of `base`.
    """

    if cur is None or base is None:
        return None
    if base == 0:
        if cur == 0:
            return None
        return f"{name} {base} → {cur} (was zero)"
    delta = (cur - base) / abs(base)
    if direction == "down" and delta < -envelope:
        return f"{name} {base:.3g} → {cur:.3g} (Δ {delta * 100:+.0f}%)"
    if direction == "symmetric" and abs(delta) > envelope:
        return f"{name} {base:.3g} → {cur:.3g} (Δ {delta * 100:+.0f}%)"
    return None


def compare_cell(current: Cell, baseline: Cell | None) -> CompareResult:
    if baseline is None:
        return CompareResult(
            case_id=current.case_id,
            has_baseline=False,
            regressions=[],
            drifts=[],
            improvements=[],
            two_variable_warning=None,
        )

    regressions: list[str] = []
    drifts: list[str] = []
    improvements: list[str] = []

    # Headline panels.
    h_cur, h_base = current.headline, baseline.headline

    # cli_review_score: drop ≥ envelope = regression; rise = improvement.
    # Keep on headline because it IS the LLM-as-judge signal (codex reviewing
    # the produced PR), even though n=3 + 3-state encoding makes the
    # threshold coarse — see complementary panels below for finer signal.
    cur_score = _median(h_cur.get("cli_review_score"))
    base_score = _median(h_base.get("cli_review_score"))
    msg = _envelope_check(
        "cli_review_score",
        cur_score,
        base_score,
        envelope=_DRIFT_ENVELOPES["cli_review_score"],
        direction="down",
    )
    if msg:
        regressions.append(msg)
    elif cur_score is not None and base_score is not None and cur_score > base_score:
        improvements.append(f"cli_review_score {base_score:.2f} → {cur_score:.2f}")

    # agent_discipline_score: continuous composite (exploration_convergence +
    # sim_useful_call_ratio + self_verified). Captures process-quality drift
    # the cli_reviewer cannot see — independent regression signal.
    # TEMPORARILY DEMOTED to informational-only: the sim_useful_call_ratio
    # input was zero-pinned by a stale matcher and is now real; existing
    # baselines for this metric are pre-fix and not comparable. Re-enable
    # the gate once baselines are regenerated against the fixed matcher.
    cur_d = _median(h_cur.get("agent_discipline_score"))
    base_d = _median(h_base.get("agent_discipline_score"))
    if cur_d is not None and base_d is not None and cur_d > base_d + 0.10:
        improvements.append(f"agent_discipline_score {base_d:.2f} → {cur_d:.2f}")

    # terminal_score: any drop is a regression.
    cur_t = _median(h_cur.get("terminal_score"))
    base_t = _median(h_base.get("terminal_score"))
    if cur_t is not None and base_t is not None and cur_t < base_t:
        regressions.append(f"terminal_score {base_t:.2f} → {cur_t:.2f}")
    elif cur_t is not None and base_t is not None and cur_t > base_t:
        improvements.append(f"terminal_score {base_t:.2f} → {cur_t:.2f}")

    # Scope + efficiency drift envelopes (informational unless they widen
    # beyond the rule).
    for name, env, direction in (
        ("files_changed", _DRIFT_ENVELOPES["files_changed"], "symmetric"),
        ("loc_net_delta", _DRIFT_ENVELOPES["loc_net_delta"], "symmetric"),
        ("cost_usd", _DRIFT_ENVELOPES["cost_usd"], "symmetric"),
        ("wall_seconds", _DRIFT_ENVELOPES["wall_seconds"], "symmetric"),
    ):
        cur_v = _median(h_cur.get(name))
        base_v = _median(h_base.get(name))
        msg = _envelope_check(name, cur_v, base_v, envelope=env, direction=direction)
        if msg:
            drifts.append(msg)

    # Format-compliance: any drop counts as regression.
    fc_cur = current.diagnostic.get("format_compliance", {})
    fc_base = baseline.diagnostic.get("format_compliance", {})
    for name in (
        "cli_review_parse_ok_rate",
        "sim_verdicts_parse_ok_rate",
        "hard_gates_passed_rate",
    ):
        cur_v = fc_cur.get(name)
        base_v = fc_base.get(name)
        if isinstance(cur_v, (int, float)) and isinstance(base_v, (int, float)) and cur_v < base_v:
            regressions.append(f"{name} {base_v:.2f} → {cur_v:.2f}")

    # Two-variable warning: if the system_digest moved AND any model field in
    # the headline mix differs from baseline's first run.
    two_var = _two_variable_check(current, baseline)

    return CompareResult(
        case_id=current.case_id,
        has_baseline=True,
        regressions=regressions,
        drifts=drifts,
        improvements=improvements,
        two_variable_warning=two_var,
    )


def _two_variable_check(current: Cell, baseline: Cell) -> str | None:
    """Per the single-variable rule (see golden_cases/README.md): never bump two variables at once.

    `system_digest` differing means contremaitre code / prompts / image /
    skills changed since baseline. Models are case-pinned, so a model swap
    requires a `case.toml` edit, which would also change prompt_hashes (if
    `case.toml` is hashed) — but we capture the model swap directly by
    comparing baseline.input_digests vs current.input_digests when the case
    file hasn't been re-baselined.

    Heuristic: flag when system_digest set differs from baseline's AND any
    input_digest in current isn't in baseline.input_digests.
    """

    if not current.system_digests or not baseline.system_digests:
        return None
    sys_changed = set(current.system_digests) != set(baseline.system_digests)
    input_changed = set(current.input_digests) != set(baseline.input_digests)
    if sys_changed and input_changed:
        return (
            "Both `system_digest` (contremaitre code/prompts/image/skills) AND "
            "`input_digest` (target/base/cli_reviewer) changed since baseline. "
            "Change one variable at a time (the single-variable rule) — re-baseline "
            "with only one moved, then bump the other."
        )
    return None


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------


class PromoteError(RuntimeError):
    pass


def _baseline_path(case_dir: Path, config_name: str) -> Path:
    return case_dir / "baselines" / f"{config_name}.json"


def promote_baseline(case_dir: Path, config_name: str, cell: Cell) -> Path:
    """Snapshot a cell as the (case, config) baseline.

    Writes to `<case_dir>/baselines/<config_name>.json` so each config
    has its own pinned reference. Refuses to promote when:
    - n < MIN_BASELINE_N (single sample below floor).
    - Any contributing run had a dirty contremaitre tree.
    - Any contributing run had `cli_review_parse_ok=false` — a baseline
      captured with a broken reviewer parser would normalize *to* the bug.
    - Not all runs passed their `check_run` (3/3 required).
    """

    if cell.n < MIN_BASELINE_N:
        raise PromoteError(f"refusing to promote n={cell.n} < {MIN_BASELINE_N}")
    if cell.any_run_dirty:
        raise PromoteError(
            "refusing to promote: at least one contributing run had "
            "contremaitre_git_dirty=true. Commit your changes first."
        )
    if not cell.all_runs_ok:
        raise PromoteError(
            "refusing to promote: at least one contributing run failed "
            "check_run (missing artifacts, base_sha mismatch, or parse "
            "failure). A baseline must be 3/3."
        )
    fc = cell.diagnostic.get("format_compliance", {}) if cell.diagnostic else {}
    if fc.get("cli_review_parse_ok_rate") not in (None, 1.0):
        raise PromoteError(
            "refusing to promote: cli_review_parse_ok_rate < 1.0 — the "
            "reviewer parser broke on at least one run. Baseline would "
            "normalize to the bug."
        )

    out = _baseline_path(case_dir, config_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cell.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def load_baseline(case_dir: Path, config_name: str) -> Cell | None:
    path = _baseline_path(case_dir, config_name)
    if not path.exists():
        return None
    return cell_from_dict(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def cmd_run(*, project_root: Path, case_id: str, config_name: str, n: int, runs_root: Path) -> int:
    case_dir = case_dir_for(project_root, case_id)
    case = load_case(case_dir)
    config = load_config(case_dir, config_name)
    print(
        f"contremaitre eval: running case={case.case_id} config={config_name} n={n}",
        file=sys.stderr,
    )
    for i in range(n):
        try:
            run_dir = run_case(case, config, runs_root=runs_root, rep_index=i + 1)
        except RuntimeError as exc:
            print(f"  [{i + 1}/{n}] FAILED to launch: {exc}", file=sys.stderr)
            return 1
        report = check_run(case, config, run_dir)
        write_canary_report(report, run_dir)
        status = "OK" if report.ok else "FAIL"
        h = report.headline
        print(
            f"  [{i + 1}/{n}] {status} terminal={h.get('terminal_verdict')} "
            f"cli_review={h.get('cli_review_verdict_key')} "
            f"rounds={h.get('review_rounds')} files={h.get('files_changed')} "
            f"dir={run_dir}",
            file=sys.stderr,
        )
        # Abort the batch on provider quota — the remaining runs would hit
        # the same per-day/per-hour limit. Surface a clear actionable error
        # instead of grinding through n-1 wasted iterations.
        if h.get("terminal_verdict") == "QUOTA_EXHAUSTED":
            remaining = n - i - 1
            print(
                f"\ncontremaitre eval: ABORTED after {i + 1}/{n} runs — "
                f"provider quota exhausted. The remaining {remaining} run(s) "
                f"would hit the same limit on the same model.",
                file=sys.stderr,
            )
            print(
                f"  Options: (a) wait for the quota reset, "
                f"(b) edit golden_cases/{case_id}/configs/{config_name}.toml to switch to a "
                f"paid model, (c) re-run later with `eval run {case_id} --config {config_name} --n {remaining}`.",
                file=sys.stderr,
            )
            return 1
    return 0


def _infer_case_and_config(run_dir_name: str, project_root: Path) -> tuple[str, str] | None:
    """Recover (case_id, config_name) from a run dir name.

    Slug: `<ts>-eval-<case_id>-<config>-<NN>` → returns (case_id, config).
    Disambiguates by matching against known case dirs (longest case_id
    prefix wins) since both case_id and config_name can contain underscores.
    """

    parts = run_dir_name.split("-eval-", 1)
    if len(parts) != 2:
        return None
    tail = parts[1]
    if not (len(tail) >= 3 and tail[-3] == "-" and tail[-2:].isdigit()):
        return None
    tail_no_rep = tail[:-3]
    for case_dir in list_cases(project_root):
        cid = case_dir.name
        if tail_no_rep.startswith(cid + "-"):
            config = tail_no_rep[len(cid) + 1 :]
            return cid, config
    return None


def cmd_check(*, project_root: Path, run_dir: Path) -> int:
    inferred = _infer_case_and_config(run_dir.name, project_root)
    if inferred is None:
        print(
            f"contremaitre eval: cannot infer (case, config) from run dir name {run_dir.name}",
            file=sys.stderr,
        )
        return 2
    case_id, config_name = inferred
    case_dir = case_dir_for(project_root, case_id)
    case = load_case(case_dir)
    config = load_config(case_dir, config_name)
    report = check_run(case, config, run_dir)
    out = write_canary_report(report, run_dir)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    print(f"contremaitre eval: wrote {out}", file=sys.stderr)
    return 0 if report.ok else 1


def cmd_compare(
    *,
    project_root: Path,
    case_id: str,
    config_name: str,
    runs_root: Path,
    n: int,
    as_json: bool = False,
) -> int:
    case_dir = case_dir_for(project_root, case_id)
    case = load_case(case_dir)
    config = load_config(case_dir, config_name)
    run_dirs = latest_n_runs_for_case(runs_root, case_id, config_name, n)
    if len(run_dirs) < n:
        print(
            f"contremaitre eval: only {len(run_dirs)} runs for case={case_id} config={config_name} (need {n})",
            file=sys.stderr,
        )
        return 2
    reports = [check_run(case, config, rd) for rd in run_dirs]
    cell = aggregate_cell(reports)
    baseline = load_baseline(case_dir, config_name)
    result = compare_cell(cell, baseline)

    if as_json:
        print(
            json.dumps(
                {
                    "cell": cell.to_dict(),
                    "compare": result.to_dict(),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(format_cell_report(cell, baseline, result, config_name=config_name))

    if result.two_variable_warning:
        print(f"contremaitre eval: WARNING — {result.two_variable_warning}", file=sys.stderr)
    if not result.has_baseline:
        print(
            f"contremaitre eval: no baseline for case={case_id} config={config_name}; "
            f"run `eval promote {case_id} --config {config_name}` to create one.",
            file=sys.stderr,
        )
        return 0
    if result.is_regression:
        print(f"contremaitre eval: REGRESSION ({len(result.regressions)} item(s))", file=sys.stderr)
        return 1
    if result.improvements:
        print(
            f"contremaitre eval: improvement candidate ({len(result.improvements)} item(s)); consider `eval promote`.",
            file=sys.stderr,
        )
    return 0


def cmd_promote(
    *, project_root: Path, case_id: str, config_name: str, runs_root: Path, n: int
) -> int:
    case_dir = case_dir_for(project_root, case_id)
    case = load_case(case_dir)
    config = load_config(case_dir, config_name)
    run_dirs = latest_n_runs_for_case(runs_root, case_id, config_name, n)
    if len(run_dirs) < n:
        print(
            f"contremaitre eval: only {len(run_dirs)} runs for case={case_id} config={config_name} (need {n})",
            file=sys.stderr,
        )
        return 2
    reports = [check_run(case, config, rd) for rd in run_dirs]
    cell = aggregate_cell(reports)
    try:
        out = promote_baseline(case_dir, config_name, cell)
    except PromoteError as exc:
        print(f"contremaitre eval: {exc}", file=sys.stderr)
        return 1
    print(f"contremaitre eval: wrote {out}", file=sys.stderr)
    return 0


def cmd_all(*, project_root: Path, config_name: str, runs_root: Path, n: int) -> int:
    """Run every case × the named config. Each case must have a matching config file.

    `eval all` is a per-config sweep: one config across many tasks. Different
    configs of the same case are tested via separate `eval all --config X`
    invocations.
    """

    cases = list_cases(project_root)
    if not cases:
        print(
            f"contremaitre eval: no cases under {project_root / GOLDEN_CASES_DIRNAME}",
            file=sys.stderr,
        )
        return 2
    any_regression = False
    for case_dir in cases:
        case = load_case(case_dir)
        if not (case_dir / "configs" / f"{config_name}.toml").exists():
            print(
                f"contremaitre eval: skipping case={case.case_id} — no config "
                f"named {config_name!r} (available: {list_configs(case_dir)})",
                file=sys.stderr,
            )
            continue
        rc_run = cmd_run(
            project_root=project_root,
            case_id=case.case_id,
            config_name=config_name,
            n=n,
            runs_root=runs_root,
        )
        if rc_run != 0:
            print(
                f"contremaitre eval: stopping `eval all` after case={case.case_id} "
                f"config={config_name} failed at run-stage (rc={rc_run})",
                file=sys.stderr,
            )
            return 1
        rc_cmp = cmd_compare(
            project_root=project_root,
            case_id=case.case_id,
            config_name=config_name,
            runs_root=runs_root,
            n=n,
        )
        if rc_cmp == 1:
            any_regression = True
    return 1 if any_regression else 0


# ---------------------------------------------------------------------------
# Pretty-print scorecard (`eval show`)
# ---------------------------------------------------------------------------


def _fmt_range(panel: Any, *, prec: int = 2) -> str:
    """Render a `{median, min, max}` dict as `med X.XX  [min – max]`."""

    if not isinstance(panel, dict):
        return "—"
    med = panel.get("median")
    lo = panel.get("min")
    hi = panel.get("max")
    if med is None:
        return "—"
    if isinstance(med, float):
        med_s = f"{med:.{prec}f}"
        lo_s = f"{lo:.{prec}f}" if isinstance(lo, float) else str(lo)
        hi_s = f"{hi:.{prec}f}" if isinstance(hi, float) else str(hi)
    else:
        med_s = str(med)
        lo_s = str(lo)
        hi_s = str(hi)
    return f"{med_s}   [{lo_s} – {hi_s}]"


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "— (no data)"
    if isinstance(value, (int, float)):
        return f"{value:.2f}"
    return str(value)


def _fmt_mix(panel: Any) -> str:
    if not isinstance(panel, dict) or not panel:
        return "—"
    return ", ".join(f"{k}×{v}" for k, v in panel.items())


def _short(digest: str | None) -> str:
    if not digest:
        return "—"
    return digest[:12]


def format_cell_report(
    cell: Cell, baseline: Cell | None, compare: CompareResult, *, config_name: str | None = None
) -> str:
    """Compact human-readable scorecard. ~40 lines."""

    h = cell.headline
    d = cell.diagnostic
    lines: list[str] = []

    config_label = f"   config={config_name}" if config_name else ""
    lines.append(f"Case: {cell.case_id}{config_label}   n={cell.n}")
    lines.append(
        f"  system_digest: {_short(cell.system_digests[0] if cell.system_digests else None)}"
        f"   input_digest: {_short(cell.input_digests[0] if cell.input_digests else None)}"
        f"   dirty: {cell.any_run_dirty}   all_runs_ok: {cell.all_runs_ok}"
    )
    lines.append("")

    lines.append("Headline — LLM judge (cli_reviewer):")
    lines.append(f"  cli_review_score        {_fmt_range(h.get('cli_review_score'), prec=2)}")
    lines.append(f"    verdict_mix           {_fmt_mix(h.get('cli_review_verdict_mix'))}")
    lines.append(
        f"  cli_findings_weighted   {_fmt_range(h.get('cli_findings_weighted'), prec=1)}   (issue×3 + suggestion×2 + nit×1)"
    )
    lines.append(f"    issue_count           {_fmt_range(h.get('cli_issue_count'))}")
    lines.append(f"    suggestion_count      {_fmt_range(h.get('cli_suggestion_count'))}")
    lines.append(f"    nit_count             {_fmt_range(h.get('cli_nit_count'))}")
    lines.append(f"  cli_citation_density    {_fmt_range(h.get('cli_citation_density'), prec=2)}")
    lines.append("")
    lines.append("Headline — orchestrator-side (process, not artifact):")
    lines.append(
        f"  agent_discipline_score  {_fmt_range(h.get('agent_discipline_score'), prec=2)}   (exploration + sim_useful + self_verified)"
    )
    lines.append(f"  terminal_score          {_fmt_range(h.get('terminal_score'), prec=2)}")
    lines.append(f"    terminal_mix          {_fmt_mix(h.get('terminal_verdict_mix'))}")
    lines.append(f"  files_changed           {_fmt_range(h.get('files_changed'))}")
    lines.append(f"  loc_net_delta           {_fmt_range(h.get('loc_net_delta'))}")
    lines.append(f"  review_rounds           {_fmt_range(h.get('review_rounds'))}")
    lines.append(f"  cost_usd                {_fmt_range(h.get('cost_usd'), prec=3)}")
    lines.append(f"  wall_seconds            {_fmt_range(h.get('wall_seconds'), prec=0)}")
    lines.append("")

    fc = d.get("format_compliance", {})
    lines.append("Format compliance:")
    lines.append(f"  sim_verdicts_parse_ok       {_fmt_rate(fc.get('sim_verdicts_parse_ok_rate'))}")
    lines.append(f"  cli_review_parse_ok         {_fmt_rate(fc.get('cli_review_parse_ok_rate'))}")
    lines.append(f"  hard_gates_passed           {_fmt_rate(fc.get('hard_gates_passed_rate'))}")
    lines.append(
        f"  implementation_complete     {_fmt_rate(fc.get('implementation_complete_written_rate'))}"
    )
    lines.append("")

    disc = d.get("discipline", {})
    lines.append("Discipline:")
    lines.append(
        f"  settled_before_code         rate={_fmt_rate(disc.get('settled_before_code_rate'))}"
    )
    lines.append(f"  self_verified               rate={_fmt_rate(disc.get('self_verified_rate'))}")
    lines.append(
        f"  runtime_install_required    rate={_fmt_rate(disc.get('runtime_install_required_rate'))}"
    )
    lines.append(
        f"  context_pollution           {_fmt_range(disc.get('context_pollution_events'))}"
    )
    lines.append(
        f"  exploration_convergence     {_fmt_mix(disc.get('exploration_convergence_mix'))}"
    )
    lines.append(
        f"  time_to_settled (s)         {_fmt_range(disc.get('time_to_settled_design_seconds'), prec=0)}"
    )
    lines.append(
        f"  tokens_to_settled           {_fmt_range(disc.get('tokens_to_settled_design'))}"
    )
    lines.append(
        f"  sim_useful_call_ratio       {_fmt_range(disc.get('sim_useful_call_ratio'), prec=2)}"
    )
    lines.append("")

    rev = d.get("review_depth", {})
    lines.append("Review depth:")
    lines.append(f"  total_checks_performed      {_fmt_range(rev.get('total_checks_performed'))}")
    lines.append(f"  total_required_changes      {_fmt_range(rev.get('total_required_changes'))}")
    lines.append(
        f"  sim_review_confidence       {_fmt_range(rev.get('sim_review_confidence'), prec=2)}"
    )
    lines.append(
        f"  process_reliability         {_fmt_range(rev.get('process_reliability'), prec=2)}"
    )
    lines.append("")

    cli = d.get("cli_review_breakdown", {})
    diff = d.get("diff_detail", {})
    eff = d.get("efficiency", {})
    lines.append("cli_review breakdown / diff / efficiency:")
    lines.append(f"  finding_count               {_fmt_range(cli.get('finding_count'))}")
    lines.append(f"  citation_count              {_fmt_range(cli.get('citation_count'))}")
    lines.append(f"  loc_added                   {_fmt_range(diff.get('loc_added'))}")
    lines.append(f"  loc_deleted                 {_fmt_range(diff.get('loc_deleted'))}")
    lines.append(f"  turns                       {_fmt_range(eff.get('turns'))}")
    lines.append(f"  agent_tool_call_count       {_fmt_range(eff.get('agent_tool_call_count'))}")
    lines.append(f"  sim_tool_call_count         {_fmt_range(eff.get('sim_tool_call_count'))}")
    lines.append("")

    lines.append("Baseline:")
    if baseline is None:
        lines.append("  (no baseline yet — `eval promote` to create one)")
    else:
        lines.append(f"  has_baseline: yes (n={baseline.n})")
        if compare.two_variable_warning:
            lines.append(f"  ! WARNING: {compare.two_variable_warning}")
        if compare.regressions:
            lines.append(f"  REGRESSIONS ({len(compare.regressions)}):")
            for r in compare.regressions:
                lines.append(f"    - {r}")
        if compare.drifts:
            lines.append(f"  drifts ({len(compare.drifts)}):")
            for dft in compare.drifts:
                lines.append(f"    ~ {dft}")
        if compare.improvements:
            lines.append(f"  improvements ({len(compare.improvements)}):")
            for imp in compare.improvements:
                lines.append(f"    + {imp}")
        if not (compare.regressions or compare.drifts or compare.improvements):
            lines.append("  no change vs baseline")

    return "\n".join(lines)


def cmd_show(*, project_root: Path, case_id: str, config_name: str, runs_root: Path, n: int) -> int:
    case_dir = case_dir_for(project_root, case_id)
    case = load_case(case_dir)
    config = load_config(case_dir, config_name)
    run_dirs = latest_n_runs_for_case(runs_root, case_id, config_name, n)
    if not run_dirs:
        print(
            f"contremaitre eval: no runs found for case={case_id} config={config_name}",
            file=sys.stderr,
        )
        return 2
    if len(run_dirs) < n:
        print(
            f"contremaitre eval: only {len(run_dirs)} runs for case={case_id} config={config_name} "
            f"(asked for n={n}); rendering what we have",
            file=sys.stderr,
        )
    reports = [check_run(case, config, rd) for rd in run_dirs]
    cell = aggregate_cell(reports)
    baseline = load_baseline(case_dir, config_name)
    compare = compare_cell(cell, baseline)
    print(format_cell_report(cell, baseline, compare, config_name=config_name))
    return 0
