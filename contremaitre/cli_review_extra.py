"""Re-run cli_review against an existing run dir, without re-running agent/SIM.

A thin driver over `cli_reviewer.run_review` that materializes a fresh git
worktree of the run's PR branch from the local cache clone and drives the
same review pipeline the orchestrator uses post-publish. Each pass lands
under `<run_dir>/extras/cli_review_<NNN>/`, leaving the original
`<tool>_review_raw_export.jsonl` and the original `cli_review_completed` /
`cli_review_failed` event in place.

Used by EVAL_ROADMAP/A1 to cross-judge completed runs (codex vs claude on
identical diffs) without re-spending agent/SIM tokens. Posting back to the
PR is opt-in (`post=True`) so iterating across many runs doesn't spam
comments on unrelated PRs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import cli_reviewer
from .jsonlog import append_jsonl, utc_ts


# Duplicated from `cli._slug_from_url` / `cli._default_cache_path` to keep
# this module importable without pulling the CLI argparse stack. Drift risk
# is low — the slug shape is the on-disk contract for the cache layout.
_CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "contremaitre"
_URL_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug_from_url(source_url: str) -> str:
    url = source_url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@") and ":" in url:
        host_part, _, path_part = url.partition(":")
        host = host_part.partition("@")[2]
        path = path_part.lstrip("/")
    else:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = parts.hostname or ""
        path = parts.path.lstrip("/")
    raw = f"{host}-{path}".strip("-/")
    safe = _URL_SAFE_RE.sub("-", raw).strip("-")
    return safe or "contremaitre-target"


def _cache_path_for(target_url: str) -> Path:
    return _CACHE_ROOT / _slug_from_url(target_url)


def _next_extra_index(run_dir: Path) -> int:
    """Lowest unused 1-based index under `<run_dir>/extras/cli_review_<NNN>`."""

    extras = run_dir / "extras"
    if not extras.is_dir():
        return 1
    used = []
    for child in extras.iterdir():
        m = re.match(r"cli_review_(\d+)$", child.name)
        if m:
            used.append(int(m.group(1)))
    return (max(used) + 1) if used else 1


def _git_worktree_add(cache_repo: Path, worktree: Path, branch: str) -> None:
    subprocess.run(
        ["git", "-C", str(cache_repo), "worktree", "add", str(worktree), branch],
        check=True,
        capture_output=True,
    )


def _git_worktree_remove(cache_repo: Path, worktree: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cache_repo), "worktree", "remove", "--force", str(worktree)],
        check=False,
        capture_output=True,
    )


def run_extra(
    run_dir: Path,
    *,
    tool: str,
    post: bool = False,
    timeout_s: int = cli_reviewer.SUBPROCESS_TIMEOUT_S,
) -> dict:
    """Run an extra cli_review pass against the PR for an existing run.

    Reads `run_config.json` + `pr.json`, sets up a fresh worktree of the
    PR branch from the local cache clone, drives `cli_reviewer.run_review`
    with `tool`, writes everything under `<run_dir>/extras/cli_review_<NNN>/`,
    and appends a `cli_review_extra_{completed,failed}` row to the run's
    `guardrail_events.jsonl`. Returns the same summary that gets written to
    `summary.json` so callers can aggregate without re-reading disk.
    """

    if tool not in cli_reviewer.VALID_TOOLS:
        raise ValueError(
            f"unknown tool: {tool!r} (expected one of {cli_reviewer.VALID_TOOLS})"
        )

    run_dir = run_dir.resolve()
    run_config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    pr_info = json.loads((run_dir / "pr.json").read_text(encoding="utf-8"))

    target_url = run_config["target_url"]
    branch = pr_info["branch"]
    pr_url = pr_info["url"]

    cache_repo = _cache_path_for(target_url)
    if not (cache_repo / ".git").exists():
        raise FileNotFoundError(
            f"cache clone missing for {target_url} — expected at {cache_repo}"
        )

    extra_index = _next_extra_index(run_dir)
    extra_dir = run_dir / "extras" / f"cli_review_{extra_index:03d}"
    extra_dir.mkdir(parents=True, exist_ok=True)

    worktree = (
        Path("/tmp")
        / f"contremaitre-cli-review-extra-{run_dir.name}-{extra_index:03d}"
    )
    if worktree.exists():
        _git_worktree_remove(cache_repo, worktree)

    summary: dict = {
        "tool": tool,
        "original_tool": run_config.get("cli_reviewer"),
        "pr_url": pr_url,
        "extra_index": extra_index,
        "extra_dir": str(extra_dir.relative_to(run_dir)),
        "started_at": utc_ts(),
    }

    try:
        _git_worktree_add(cache_repo, worktree, branch)
        cli_reviewer.hide_orchestrator_scaffolds(worktree)
        prompt = cli_reviewer.build_prompt(pr_url=pr_url)
        sink = extra_dir / f"{tool}_review_raw_export.jsonl"

        start = time.monotonic()
        result = cli_reviewer.run_review(
            tool=tool,
            prompt=prompt,
            jsonl_path=sink,
            cwd=worktree,
            timeout_s=timeout_s,
        )
        duration_s = time.monotonic() - start

        summary.update(
            {
                "exit_code": result.exit_code,
                "duration_s": round(duration_s, 1),
                "review_chars": len(result.markdown),
                "verdict": cli_reviewer.parse_verdict(result.markdown),
                "error": result.error,
            }
        )

        review_md_path = extra_dir / "review.md"
        if result.markdown.strip():
            model = cli_reviewer.extract_model(tool=tool, jsonl_path=sink)
            header = cli_reviewer.format_header(
                tool=tool, model=model, duration_s=duration_s
            )
            review_md_path.write_text(
                header + result.markdown.lstrip(), encoding="utf-8"
            )
            summary["model"] = model

        posted = False
        if post and result.markdown.strip() and not result.error:
            posted, message = cli_reviewer.post_comment(
                pr_url=pr_url,
                body_path=review_md_path,
                git_log=run_dir / "git_log.jsonl",
            )
            summary["post_message"] = message
        summary["posted_to_pr"] = posted
        summary["finished_at"] = utc_ts()
    finally:
        _git_worktree_remove(cache_repo, worktree)

    (extra_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Refresh the per-run viewer.html AND the runs-root index so the new
    # claude/codex review block + side-by-side header chip show up without
    # waiting for the operator to rerun `contremaitre index`. `build_viewer`
    # already triggers `build_index` internally, so one call regenerates
    # both surfaces. Same best-effort pattern build_viewer uses elsewhere —
    # the extra artifact on disk is the load-bearing output; viewer
    # refresh is observability and shouldn't break a successful rerun.
    try:
        from .paths import build_run_paths
        from .viewer import build_viewer

        build_viewer(build_run_paths(run_dir.parent, run_dir.name))
    except Exception:
        pass

    failed = bool(summary.get("error")) or not summary.get("review_chars")
    event_name = "cli_review_extra_failed" if failed else "cli_review_extra_completed"
    append_jsonl(
        run_dir / "guardrail_events.jsonl",
        {
            "event": event_name,
            "tool": tool,
            "original_tool": summary["original_tool"],
            "extra_index": extra_index,
            "extra_dir": summary["extra_dir"],
            "verdict": summary.get("verdict"),
            "review_chars": summary.get("review_chars"),
            "duration_s": summary.get("duration_s"),
            "posted_to_pr": summary.get("posted_to_pr", False),
            "url": pr_url,
            "manual_rerun": True,
        },
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cli_review_extra",
        description="Re-run cli_review against an existing run dir.",
    )
    p.add_argument("run_dir", type=Path, help="Path to .contremaitre/runs/<id>/")
    p.add_argument(
        "--tool",
        choices=cli_reviewer.VALID_TOOLS,
        required=True,
        help="Reviewer to invoke (codex or claude).",
    )
    p.add_argument(
        "--post",
        action="store_true",
        help="Also gh pr comment the result. Off by default — A1-style iteration "
        "across many runs should NOT post.",
    )
    p.add_argument(
        "--timeout-s",
        type=int,
        default=cli_reviewer.SUBPROCESS_TIMEOUT_S,
    )
    args = p.parse_args(argv)
    summary = run_extra(
        args.run_dir,
        tool=args.tool,
        post=args.post,
        timeout_s=args.timeout_s,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not summary.get("error") and summary.get("review_chars") else 1


if __name__ == "__main__":
    sys.exit(main())
