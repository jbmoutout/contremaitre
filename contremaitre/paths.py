"""Path, slug, and run-id helpers.

All external path construction flows through this module so user-provided slugs
cannot escape the run directory or create surprising branch names.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .models import RunPaths

SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SLUG_CLEAN_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def validate_slug(value: str, label: str = "slug") -> str:
    if not SAFE_SLUG_RE.fullmatch(value):
        raise ValueError(
            f"{label} must start with a letter or number and contain only "
            "letters, numbers, '.', '_' or '-'"
        )
    return value


def slugify(value: str, fallback: str = "run") -> str:
    slug = SLUG_CLEAN_RE.sub("-", value.strip().lower()).strip(".-_")
    if not slug:
        slug = fallback
    if not slug[0].isalnum():
        slug = f"{fallback}-{slug}"
    return slug[:64]


def new_run_id(slug: str) -> str:
    safe = validate_slug(slugify(slug), "run slug")
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}"


def contained_path(root: Path, *parts: str) -> Path:
    resolved_root = root.resolve()
    path = resolved_root.joinpath(*parts).resolve()
    if path != resolved_root and resolved_root not in path.parents:
        raise ValueError(f"path escapes root: {path}")
    return path


def build_run_paths(runs_root: Path, run_id: str) -> RunPaths:
    run_dir = contained_path(runs_root, run_id)
    worktree = Path("/tmp") / f"contremaitre-{run_id}"
    eval_dir = run_dir / "eval"
    return RunPaths(
        run_id=run_id,
        run_dir=run_dir,
        worktree=worktree,
        initial_prompt=run_dir / "initial_prompt.txt",
        raw_export=run_dir / "raw_export.jsonl",
        sim_raw_export=run_dir / "sim_raw_export.jsonl",
        claude_review_raw_export=run_dir / "claude_review_raw_export.jsonl",
        codex_review_raw_export=run_dir / "codex_review_raw_export.jsonl",
        transcript=run_dir / "transcript.md",
        timeline=run_dir / "timeline.jsonl",
        trajectory=run_dir / "trajectory.json",
        stats=run_dir / "stats.json",
        git_log=run_dir / "git_log.jsonl",
        test_runs=run_dir / "test_runs.jsonl",
        review_cycles=run_dir / "review_cycles.jsonl",
        worktree_state=run_dir / "worktree_state.jsonl",
        guardrail_events=run_dir / "guardrail_events.jsonl",
        pr_json=run_dir / "pr.json",
        eval_dir=eval_dir,
        pr_eval=eval_dir / "pr_eval.json",
        pr_eval_md=eval_dir / "pr_eval.md",
        checks_report=eval_dir / "checks_report.json",
        settled_diff_report=eval_dir / "settled_diff_report.json",
        architecture_delta_report=eval_dir / "architecture_delta_report.json",
        trajectory_report=eval_dir / "trajectory_report.json",
        judge_attempts=eval_dir / "judge_attempts.jsonl",
        cost_report=eval_dir / "cost_report.json",
        preflight_report=eval_dir / "preflight_report.json",
        recoveries=run_dir / "recoveries.jsonl",
        subagents_dir=run_dir / "subagents",
        extracted_files_dir=run_dir / "extracted_files",
        flow_use_report=eval_dir / "flow_use.json",
    )
