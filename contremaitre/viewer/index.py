"""Single-file HTML index across all run dirs under a runs root.

Scans `<runs_root>/<run_id>/` directories for `viewer.html` companions,
reads each run's `stats.json`, `pr.json`, `run_config.json`, and renders
a self-contained `<runs_root>/index.html` that links into each run's
viewer. Reuses `_styles.css` so the index inherits the viewer's look.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..costs import sum_token_usage_in_events
from ..models import ModelSpec
from ..pr_outcomes import PrOutcome, outcome_for_run
from ..run_artifacts import RunArtifacts
from . import VIEWER_FILENAME

_HERE = Path(__file__).resolve().parent
_CSS_PATH = _HERE / "_styles.css"

INDEX_FILENAME = "index.html"


def build_index(runs_root: Path) -> Path:
    """Scan `runs_root` for runs with a viewer and emit `index.html`.

    Returns the written path. Runs without `viewer.html` are skipped —
    the index is a viewer-of-viewers, not a run roster.
    """

    rows = _collect_rows(runs_root)
    html = _render_html(rows, runs_root=runs_root)
    out = runs_root / INDEX_FILENAME
    out.write_text(html, encoding="utf-8")
    return out


def _collect_rows(runs_root: Path) -> list[dict[str, Any]]:
    if not runs_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue
        viewer = entry / VIEWER_FILENAME
        if not viewer.is_file():
            continue
        rows.append(_summarize_run(entry))
    # Newest first — run_ids are timestamp-prefixed so reverse-sort is
    # chronologically correct without parsing the timestamp.
    rows.sort(key=lambda r: r["run_id"], reverse=True)
    return rows


def _summarize_run(run_dir: Path) -> dict[str, Any]:
    stats = _read_json(run_dir / "stats.json", default={}) or {}
    pr = _read_json(run_dir / "pr.json", default=None)
    config = _read_json(run_dir / "run_config.json", default=None)

    repo = None
    base = None
    if isinstance(config, dict):
        repo = _slug_from_git_url(config.get("target_url"))
        base = config.get("base")
    if not repo and isinstance(pr, dict):
        repo = _slug_from_pr_url(pr.get("url"))
    if not base and isinstance(pr, dict):
        base = pr.get("base")

    cli_reviews = _read_cli_reviews(run_dir)
    # One reader: diffstat + the whole-run token rollup share the memoized
    # stream reads (worktree_state for diffstat, the actor/reviewer exports
    # for tokens). token_usage_all spans agent + SIM + both CLI reviewers.
    arts = RunArtifacts.from_run_dir(run_dir)
    diffstat = arts.diffstat()
    tokens = arts.token_usage_all()
    pr_outcome = outcome_for_run(run_dir)

    return {
        "run_id": run_dir.name,
        "viewer_href": f"{run_dir.name}/{VIEWER_FILENAME}",
        "when": _format_when(run_dir.name),
        "agent_model": _spec_canonical(stats.get("agent_model"))[0],
        "sim_model": _spec_canonical(stats.get("sim_model"))[0],
        "repo": repo,
        "base": base,
        "verdict": stats.get("verdict") or "?",
        "terminal_state": stats.get("terminal_state") or "?",
        "duration_seconds": stats.get("duration_seconds"),
        "turns": stats.get("turns"),
        "cost_usd": stats.get("recorded_cost_usd"),
        "reason": (stats.get("reason") or "").strip(),
        "impl_complete": _read_impl_complete(run_dir),
        "settled_preamble": _read_settled_preamble(run_dir),
        "diffstat": diffstat,
        "tokens": tokens,
        "pr_kind": (pr or {}).get("kind") if isinstance(pr, dict) else None,
        "pr_url": (pr or {}).get("url") if isinstance(pr, dict) else None,
        "pr_title": (pr or {}).get("title") if isinstance(pr, dict) else None,
        "pr_branch": (pr or {}).get("branch") if isinstance(pr, dict) else None,
        "pr_outcome": pr_outcome,
        "cli_reviews": cli_reviews,
    }


_MECHANICAL_TOKENS = (
    "black",
    "flake8",
    "mypy",
    "format",  # "Black-formatted", "formatting check"
    "unused",  # F401 phrasing
    "f401",
    "e402",
    "e501",
)


def _classify_blocker(text: str) -> str | None:
    """Classify a MUST_FIX review's blocker as mechanical/semantic/mixed.

    Scans the `**issue:**` bullets emitted by the cli_reviewer prompt.
    A bullet is "mechanical" if its body mentions a CI-mechanical token
    (Black, flake8, mypy, etc.) — those failures are formatter/lint
    discoverable and addressed by the actor's pre-IMPLEMENTATION_COMPLETE
    gate (see initial_prompt.md). A bullet is "semantic" otherwise.

    Returns `"mechanical"` (all bullets mechanical), `"semantic"` (none
    mechanical), `"mixed"` (both kinds present), or `None` when no
    `**issue:**` bullets exist to classify.
    """

    mech = sem = 0
    for raw in text.splitlines():
        line = raw.lower()
        if "**issue:**" not in line:
            continue
        if any(tok in line for tok in _MECHANICAL_TOKENS):
            mech += 1
        else:
            sem += 1
    if mech == 0 and sem == 0:
        return None
    if mech and not sem:
        return "mechanical"
    if sem and not mech:
        return "semantic"
    return "mixed"


def _read_cli_reviews(run_dir: Path) -> list[dict[str, Any]]:
    """Every cli_review on disk for `run_dir`, in display order.

    Three provenance shapes are surfaced:
      - orchestrator-published: `<tool>_review.md` next to the run's other
        artifacts (the cli_reviewer post-publish step).
      - orchestrator loop rounds: `extras/cli_review_<NNN>/<tool>_review.md`.
      - legacy format: `extras/cli_review_<NNN>/review.md` with sibling
        `summary.json` carrying the tool name. Present in older runs;
        source labelled `extra-NNN` for backward compatibility.

    Each entry: `{"tool", "verdict", "blocker", "source"}`. Verdict / blocker
    come from `_classify_review_md`. The list is ordered original-first then
    extras by index so the side-by-side badge order matches batch order.
    """

    reviews: list[dict[str, Any]] = []

    for tool in ("codex", "claude"):
        path = run_dir / f"{tool}_review.md"
        if path.is_file():
            reviews.append(_classify_review_md(tool, path, source="orchestrator"))

    extras_root = run_dir / "extras"
    if extras_root.is_dir():
        for extra_dir in sorted(extras_root.iterdir()):
            if not extra_dir.is_dir():
                continue
            round_label = extra_dir.name.replace("cli_review_", "round-")
            for review_md in sorted(extra_dir.glob("*_review.md")):
                tool = review_md.name.removesuffix("_review.md")
                if tool in ("codex", "claude"):
                    reviews.append(
                        _classify_review_md(tool, review_md, source=f"{round_label}-{tool}")
                    )
            review_md = extra_dir / "review.md"
            if not review_md.is_file():
                continue
            summary_path = extra_dir / "summary.json"
            tool = None
            if summary_path.is_file():
                summary = _read_json(summary_path, default=None)
                if isinstance(summary, dict):
                    tool = summary.get("tool")
            if not tool:
                # Fall back to filename guess for legacy extras where the
                # review.md doesn't carry the tool name — sniff the sibling
                # raw export filename.
                for candidate in ("codex", "claude"):
                    if (extra_dir / f"{candidate}_review_raw_export.jsonl").exists():
                        tool = candidate
                        break
            if not tool:
                continue
            # `cli_review_001` → `extra-001`
            label = extra_dir.name.replace("cli_review_", "extra-")
            reviews.append(_classify_review_md(tool, review_md, source=label))

    return reviews


def _classify_review_md(tool: str, path: Path, *, source: str) -> dict[str, Any]:
    """Parse a review.md into the dict shape `_read_cli_reviews` returns."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"tool": tool, "verdict": None, "blocker": None, "source": source}
    verdict: str | None = None
    for line in text.splitlines()[:8]:
        for key in ("MUST_FIX", "NEEDS_ATTENTION", "LOOKS_GOOD"):
            if key in line:
                verdict = key
                break
        if verdict:
            break
    blocker = _classify_blocker(text) if verdict == "MUST_FIX" else None
    return {"tool": tool, "verdict": verdict, "blocker": blocker, "source": source}


_SETTLED_MAX_LINES = 3
_SETTLED_MAX_CHARS = 280


def _read_impl_complete(run_dir: Path) -> str:
    """Return the agent's IMPLEMENTATION_COMPLETE one-liner, or "".

    Cleanup tooling extracts the worktree marker to
    `extracted_files/.contremaitre__IMPLEMENTATION_COMPLETE`. Single-line
    file; trim to `_SETTLED_MAX_CHARS` so the index row stays compact.
    """

    src = run_dir / "extracted_files" / ".contremaitre__IMPLEMENTATION_COMPLETE"
    if not src.is_file():
        return ""
    try:
        text = src.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > _SETTLED_MAX_CHARS:
        text = text[:_SETTLED_MAX_CHARS].rstrip() + "…"
    return text


def _read_settled_preamble(run_dir: Path) -> str:
    """First few non-heading lines from `review_input/SETTLED_DESIGN.md`.

    SETTLED_DESIGN starts with an H1 title and usually a `## Seam` section,
    then prose. We skip headings (`#`-prefixed) and blank lines, then take
    the first `_SETTLED_MAX_LINES` lines (or up to `_SETTLED_MAX_CHARS`).
    Returns "" if the file is missing or has no prose.
    """

    src = run_dir / "review_input" / "SETTLED_DESIGN.md"
    if not src.is_file():
        return ""
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kept.append(line)
        if len(kept) >= _SETTLED_MAX_LINES:
            break
    joined = " ".join(kept)
    if len(joined) > _SETTLED_MAX_CHARS:
        joined = joined[:_SETTLED_MAX_CHARS].rstrip() + "…"
    return joined


def _format_when(run_id: str) -> str:
    # run_id starts with YYYYMMDD-HHMMSS — keep the rest as the slug
    stamp = run_id[:15]
    try:
        dt = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return run_id
    return dt.strftime("%Y-%m-%d %H:%M")


def _spec_canonical(model: object) -> tuple[str | None, str | None]:
    """Uniform `(name, runtime)` for a role's persisted model identity.

    Routes through `ModelSpec.from_record` (which absorbs the canonical dict
    *and* any legacy on-disk label/slug string), so the viewer never parses a
    model string itself. Returns `(None, None)` when identity is absent.
    """

    if not model:
        return None, None
    return ModelSpec.from_record(model).canonical()


def _slug_from_pr_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    # Path like /owner/repo/pull/8
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) >= 2:
        return f"{segs[0]}/{segs[1]}"
    return None


def _github_url_from_slug(slug: str | None) -> str | None:
    """`owner/repo` → `https://github.com/owner/repo`. None if not the right shape."""

    if not slug or "/" not in slug:
        return None
    parts = slug.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"https://github.com/{slug}"


def _slug_from_git_url(url: str | None) -> str | None:
    if not url:
        return None
    s = url.strip()
    if s.endswith(".git"):
        s = s[:-4]
    if s.startswith("git@") and ":" in s:
        path = s.partition(":")[2]
    else:
        path = urlsplit(s if "://" in s else f"https://{s}").path
    path = path.strip("/")
    segs = path.split("/")
    if len(segs) >= 2:
        return f"{segs[-2]}/{segs[-1]}"
    return path or None


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ----- HTML assembly -----


_TIER_BY_VERDICT = {
    "READY_FOR_DRAFT_PR": "tier-green",
    "PR_NEEDS_HUMAN": "tier-yellow",
    "FAILED_INFRA": "tier-red",
}

# Display aliases for verbose verdict strings.
_VERDICT_DISPLAY = {
    "READY_FOR_DRAFT_PR": "DRAFT_PR",
}


def _verdict_tier(verdict: str) -> str:
    if verdict in _TIER_BY_VERDICT:
        return _TIER_BY_VERDICT[verdict]
    if verdict.startswith("NO_PR"):
        return "tier-yellow"
    return "tier-unknown"


def _cli_review_tier(verdict: str | None) -> str:
    """Map the agent's verdict key to a sim-dot tier class."""

    if verdict == "LOOKS_GOOD":
        return "tier-green"
    if verdict == "NEEDS_ATTENTION":
        return "tier-yellow"
    if verdict == "MUST_FIX":
        return "tier-red"
    return "tier-unknown"


def _pr_outcome_pill(outcome: dict[str, Any]) -> str:
    key = str(outcome.get("outcome") or PrOutcome.UNKNOWN.value)
    label = str(outcome.get("label") or key)
    tier = str(outcome.get("tier") or "tier-unknown")
    return (
        f'<span class="score-pill"><span class="sim-dot {tier}"></span>'
        f"<b>{_escape(label)}</b></span>"
    )


def _verdict_label(verdict: str) -> str:
    return _VERDICT_DISPLAY.get(verdict, verdict)


def _fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    s = int(round(float(seconds)))
    m, r = divmod(s, 60)
    if m == 0:
        return f"{r}s"
    return f"{m}m {r}s"


# Kaomoji for free runs — cheerful, ASCII, fits the monospace vibe.
_FREE_KAOMOJI = "(◕‿◕)"


def _fmt_cost(cost: float | int | None) -> tuple[str, bool]:
    """Return (label, is_free). Free runs show a kaomoji instead of $0.0000."""

    if cost is None:
        return ("—", False)
    c = float(cost)
    if c == 0:
        return (f"free {_FREE_KAOMOJI}", True)
    return (f"${c:.4f}", False)


def _fmt_turns(turns: int | None) -> str:
    return "—" if turns is None else str(turns)


def _fmt_token_count(n: int) -> str:
    """Compact token count: 1_523_868 → '1.5M', 81_548 → '82k', 940 → '940'."""

    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _tokens_pill(tokens: dict[str, int] | None) -> str:
    """`{input, output, reasoning, cache_read}` → an "in · out · cache" pill.

    `out` folds reasoning tokens in (they're billed as output). Empty string
    when the run recorded no token usage (e.g. an infra failure before any
    actor turn), so such rows show no pill rather than a misleading "0 in".
    """

    if not tokens:
        return ""
    inp = tokens.get("input") or 0
    out = (tokens.get("output") or 0) + (tokens.get("reasoning") or 0)
    cache = tokens.get("cache_read") or 0
    if not (inp or out or cache):
        return ""
    return (
        '<span class="score-pill" title="tokens across agent + SIM + CLI reviewers '
        '(out includes reasoning; cache = cache-read)">'
        f"<b>{_fmt_token_count(inp)}</b> in · "
        f"<b>{_fmt_token_count(out)}</b> out · "
        f"<b>{_fmt_token_count(cache)}</b> cache"
        "</span>"
    )


def _diffstat_pill(diffstat: dict[str, int] | None) -> str:
    """`{files, insertions, deletions}` → a "N files · +I −D" score pill.

    Empty string when the run produced no diff (so a clean/no-op run shows
    no pill rather than a misleading "0 files"). Colors mirror the pipeline
    tab's +LoC/−LoC accents (green insertions, red deletions).
    """

    if not diffstat:
        return ""
    files = diffstat.get("files") or 0
    ins = diffstat.get("insertions") or 0
    dele = diffstat.get("deletions") or 0
    return (
        '<span class="score-pill">'
        f"<b>{files}</b> file{'s' if files != 1 else ''} · "
        f'<span style="color:var(--success)">+{ins}</span> '
        f'<span style="color:#F87171">−{dele}</span>'
        "</span>"
    )


def _render_html(rows: list[dict[str, Any]], *, runs_root: Path) -> str:
    css = _CSS_PATH.read_text(encoding="utf-8")
    body = _render_body(rows, runs_root=runs_root)
    title = "contremaitre · runs"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
{css}
</style>
<style>
/* Index-only color accents — additive over _styles.css */
:root {{
  /* Dimmer green: viewer's --success (#4ADE80) reads as neon at index scale,
     where many rows light up at once. */
  --success: #3FA060;
}}
/* Stretch the headline column across the full row height and push the
   "view" link to the bottom so verdict sits at top-right, view at bottom-right. */
.rep.rep-eval .rep-headline {{
  justify-content: space-between;
  align-self: stretch;
}}
.rep.rep-eval .rep-headline .view-link {{ cursor: pointer; }}
.rep.rep-eval .rep-headline .view-link:hover {{ color: var(--text-bright); text-decoration: none; }}
.rep.rep-eval .rep-headline .view-link:hover::after {{ transform: translateX(2px); }}
.rep-label a {{ color: var(--text-bright); }}
.rep-label a:hover {{ color: var(--accent); text-decoration: none; }}
.rep-label code {{ color: var(--text-bright); background: var(--surface-2); padding: 1px 6px; border-radius: 2px; }}
.score-pill.pill-free {{
  color: var(--success); border-color: var(--success);
  background: rgba(74, 222, 128, 0.08);
}}
.score-pill.pill-free b {{ color: var(--success); }}
.pr-link {{ color: var(--accent); }}
.pr-link b {{ color: var(--text-bright); font-weight: 500; }}
.pr-pill-no-pr {{
  color: var(--warning); border-color: var(--warning);
  background: rgba(255, 184, 48, 0.08);
}}
.totals .item .sim-dot {{ vertical-align: 1px; }}
/* tab bar: runs | pipeline */
.tabbar {{ display: flex; gap: 2px; margin: 18px 0 22px; border-bottom: 1px solid var(--surface-2); }}
.tab {{ background: none; border: none; color: var(--text-muted); font-family: inherit; font-size: 13px; padding: 8px 18px; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; letter-spacing: 0.02em; }}
.tab:hover {{ color: var(--text-bright); }}
.tab.active {{ color: var(--text-bright); border-bottom-color: var(--accent); }}
.tabpane {{ display: none; }}
.tabpane.active {{ display: block; }}
/* pipeline observability table */
.pipeline-title {{ font-size: 13px; color: var(--text-bright); margin-bottom: 14px; letter-spacing: 0.02em; }}
.pipeline-scroll {{ overflow-x: auto; }}
.pipeline-table {{ border-collapse: collapse; font-size: 12px; white-space: nowrap; }}
.pipeline-table th {{ text-align: left; color: var(--text-muted); font-weight: 500; padding: 3px 12px 3px 0; }}
.pipeline-table th.num {{ text-align: right; padding-right: 16px; }}
.pipeline-table thead tr:last-child th {{ padding-bottom: 9px; border-bottom: 1px solid var(--surface-2); }}
.pipeline-table .grp-head {{ color: var(--text-dim); font-size: 10px; text-transform: uppercase; letter-spacing: 0.09em; padding-bottom: 1px; }}
.pipeline-table td {{ padding: 7px 12px; border-bottom: 1px solid rgba(255, 255, 255, 0.04); color: var(--text-dim); }}
.pipeline-table td.num {{ text-align: right; padding-right: 16px; font-variant-numeric: tabular-nums; }}
.pipeline-table .pair-cell {{ color: var(--text-bright); padding-left: 0; padding-right: 20px; }}
.pipeline-table .pair-cell .sep {{ color: var(--text-muted); margin: 0 8px; }}
.pipeline-table .rt-tag {{ margin-left: 5px; padding: 0 5px; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); border: 1px solid var(--surface-2); border-radius: 3px; vertical-align: 1px; }}
.pipeline-table .grp-start {{ border-left: 1px solid var(--surface-2); padding-left: 16px; }}
.pipeline-table .cell.partial {{ color: var(--text-muted); opacity: 0.7; }}
.pipeline-table .cell.sev-amber {{ color: var(--warning); }}
.pipeline-table .cell.sev-red {{ color: #F87171; }}
.pipeline-table .muted {{ color: var(--text-muted); }}
.pipeline-table tbody tr:hover td {{ background: rgba(255, 255, 255, 0.02); }}
.cov-note {{ margin-top: 16px; font-size: 11px; color: var(--text-muted); line-height: 1.7; max-width: 900px; }}
</style>
</head>
<body>
<div class="page page-wide">
{body}
</div>
<script>
document.querySelectorAll('.tab').forEach(function (tab) {{
  tab.addEventListener('click', function () {{
    document.querySelectorAll('.tab').forEach(function (t) {{ t.classList.remove('active'); }});
    document.querySelectorAll('.tabpane').forEach(function (p) {{ p.classList.remove('active'); }});
    tab.classList.add('active');
    var pane = document.getElementById(tab.dataset.pane);
    if (pane) {{ pane.classList.add('active'); }}
  }});
}});
</script>
</body>
</html>
"""


# ----- pipeline observability (the "pipeline" tab) -----
#
# A by-pairing aggregate of the run pipeline, grouped into four concerns —
# OUTCOME (does the duo land a PR), EXCHANGE (how long the design/impl
# turns run), REVIEW (sim/extra change-requested rate + post-publish CLI
# review failures), and CODE (LoC + output tokens produced). Every number
# comes from a structured field, never regex'd from log prose, and each
# derived metric carries an honest coverage count (k of n runs had the
# datum): flow_use phases go null for CLI-agent runs, review_cycles only
# exist once a run reaches review, and a CLI PR-review only runs when a PR
# is published. Fake-mode fixtures are excluded — this tab is about the
# real model+prompt path. The join key is the stats.json display label
# (the same short form the runs tab shows), so the two tabs reconcile.

_PAIRING_MIN_RUNS = 2

# Verdicts where the pipeline died for reasons unrelated to model quality
# (docker/clone/preflight, or provider credit exhaustion). These runs never
# exercised the model loop meaningfully, so they're excluded from the
# per-pairing metrics — counting them would understate PR-land rate and
# pollute turns/LoC. Infra reliability is a separate concern (the runs tab's
# "failed infra" total).
_INFRA_VERDICTS = frozenset({"FAILED_INFRA", "QUOTA_EXHAUSTED"})


def _pr_review_verdict(arts: RunArtifacts) -> str | None:
    """Worst post-publish CLI-review verdict across all rounds, or None if no review ran.

    Read from the structured `cli_review_completed` guardrail event rather
    than scraping `<tool>_review.md`. One event is emitted per completed round
    (only for parseable verdicts); we keep the worst across all rounds
    (MUST_FIX > NEEDS_ATTENTION > LOOKS_GOOD) so the pipeline column reflects
    the most severe finding. None verdicts (old-format runs from before the
    event-ordering fix) are skipped via the `in order` guard.
    """

    order = {"LOOKS_GOOD": 1, "NEEDS_ATTENTION": 2, "MUST_FIX": 3}
    worst: str | None = None
    for event in arts.guardrail_events():
        if event.get("event") != "cli_review_completed":
            continue
        verdict = event.get("verdict")
        if verdict in order and (worst is None or order[verdict] > order[worst]):
            worst = verdict
    return worst


def _review_signals(arts: RunArtifacts) -> dict[str, Any]:
    """SIM review signals from `review_cycles.jsonl` for one run.

    `sim_rounds` is the highest round the primary reviewer reached;
    `sim_changes` records whether the reviewer ever bounced the diff
    (returned a non-APPROVED verdict). It is None when the reviewer produced
    no verdict row, so coverage stays honest for runs that never reached
    review.
    """

    sim_verdicts: list[str] = []
    sim_rounds = 0
    for row in arts.review_cycles():
        if row.get("unavailable"):
            continue
        verdict = row.get("verdict")
        if not verdict:
            continue
        if row.get("reviewer") == "sim":
            sim_verdicts.append(verdict)
            sim_rounds = max(sim_rounds, int(row.get("round") or 0))
    return {
        "sim_rounds": sim_rounds or None,
        "sim_changes": (any(v != "APPROVED" for v in sim_verdicts) if sim_verdicts else None),
    }


def _pipeline_run_metrics(run_dir: Path) -> dict[str, Any] | None:
    """Grounded pipeline metrics for one run, or None if not aggregable.

    Returns None only for fake-mode fixtures and runs with no agent/sim
    label. For an infra failure it returns a MARKER dict (`infra=True` plus
    just the pairing identity): the run is kept so the aggregator can count
    how often a duo infra-failed, but its (meaningless, often empty)
    metrics are skipped and excluded from the rates. For a real run every
    metric field is None when its source datum is absent, so the aggregator
    can report honest per-metric coverage.
    """

    stats = _read_json(run_dir / "stats.json", default=None)
    if not isinstance(stats, dict) or stats.get("actor_mode") == "fake":
        return None
    agent, agent_rt = _spec_canonical(stats.get("agent_model"))
    sim, sim_rt = _spec_canonical(stats.get("sim_model"))
    if not agent or not sim:
        return None
    if stats.get("verdict") in _INFRA_VERDICTS:
        return {"agent": agent, "agent_rt": agent_rt, "sim": sim, "sim_rt": sim_rt, "infra": True}

    # Phases computed LIVE via the fixed flow_use.compute_phases, not the
    # persisted eval/flow_use.json (stale for runs scored before the CLI
    # fix). grilling/impl come back None when unrecoverable — codex streams
    # carry no timestamps, and pre-actor-start CLI runs logged no agent
    # turns — so the dashboard shows "—" rather than wrong numbers.
    # One reader per run: memoization collapses the streams these helpers share
    # (raw_export, review_cycles, guardrail_events) to one read each.
    arts = RunArtifacts.from_run_dir(run_dir)
    phases = arts.phases()
    loc = arts.diffstat()
    review = _review_signals(arts)
    pr_verdict = _pr_review_verdict(arts)

    # Agent output tokens — the code-production signal (cost is $0 for
    # subscription runs, so tokens are the real spend). Recomputed from the
    # raw stream via the canonical summer, which handles all three runtime
    # event shapes; eval/cost_report.json is too sparse to rely on.
    out_tokens: int | None = None
    if arts.raw_export():
        # Single-stream output tokens — deliberately NOT arts.token_usage(),
        # which sums agent+SIM. This is the agent's code-production signal.
        out_tokens = sum_token_usage_in_events(arts.raw_export()).get("output") or None

    return {
        "agent": agent,
        "agent_rt": agent_rt,
        "sim": sim,
        "sim_rt": sim_rt,
        "infra": False,
        "lands_pr": str(stats.get("verdict") or "") in {"READY_FOR_DRAFT_PR", "PR_NEEDS_HUMAN"},
        "accepted_pr": outcome_for_run(run_dir).get("accepted"),
        "turns": stats.get("turns"),
        "duration": stats.get("duration_seconds"),
        "design_rounds": phases.get("grilling_exchanges"),
        "impl_turns": phases.get("impl_turns"),
        "sim_rounds": review["sim_rounds"],
        "sim_changes": review["sim_changes"],
        "pr_review_fail": (pr_verdict == "MUST_FIX") if pr_verdict else None,
        "ins": loc["insertions"] if loc else None,
        "dele": loc["deletions"] if loc else None,
        "out_tokens": out_tokens,
    }


def _avg(runs: list[dict[str, Any]], key: str) -> tuple[float | None, int]:
    """(mean of present values, count present). (None, 0) when all absent."""

    vals = [r[key] for r in runs if r.get(key) is not None]
    return (sum(vals) / len(vals) if vals else None, len(vals))


def _rate(runs: list[dict[str, Any]], key: str) -> tuple[float | None, int]:
    """(fraction of present bool values that are truthy, count present)."""

    vals = [r[key] for r in runs if r.get(key) is not None]
    return (sum(1 for v in vals if v) / len(vals) if vals else None, len(vals))


def _collect_pipeline_pairings(runs_root: Path) -> list[dict[str, Any]]:
    """Aggregate grounded pipeline metrics by agent×sim pairing, busiest first.

    Pairings under `_PAIRING_MIN_RUNS` real runs are dropped. Each averaged
    or rated metric is a `(value, coverage)` tuple so the table can show
    how many of the pairing's runs actually carried the datum.
    """

    if not runs_root.is_dir():
        return []
    # Key on (name, runtime) per role so an opencode model never collides
    # with a same-named CLI one, and so the same model groups regardless of
    # provider-prefix spelling.
    buckets: dict[tuple[str, str | None, str, str | None], list[dict[str, Any]]] = {}
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        metrics = _pipeline_run_metrics(entry)
        if metrics is None:
            continue
        key = (metrics["agent"], metrics["agent_rt"], metrics["sim"], metrics["sim_rt"])
        buckets.setdefault(key, []).append(metrics)

    pairings: list[dict[str, Any]] = []
    for (agent, agent_rt, sim, sim_rt), runs in buckets.items():
        # Metrics are computed over the REAL runs only; infra failures are
        # kept solely as a per-pairing count (the `infra` column). A pairing
        # needs `_PAIRING_MIN_RUNS` real runs to characterize model behavior;
        # all-infra pairings (0 real) surface via `_infra_only_pairings`.
        real = [r for r in runs if not r.get("infra")]
        infra_n = len(runs) - len(real)
        if len(real) < _PAIRING_MIN_RUNS:
            continue
        ins = _avg(real, "ins")
        dele = _avg(real, "dele")
        out_tokens = _avg(real, "out_tokens")
        tok_per_loc = None
        net_loc = (ins[0] or 0) + (dele[0] or 0)
        if out_tokens[0] is not None and net_loc > 0:
            tok_per_loc = out_tokens[0] / net_loc
        pairings.append(
            {
                "agent": agent,
                "agent_rt": agent_rt,
                "sim": sim,
                "sim_rt": sim_rt,
                "n": len(real),
                "infra_n": infra_n,
                "pr_land": sum(1 for r in real if r["lands_pr"]) / len(real),
                "pr_accept": _rate(real, "accepted_pr"),
                "design": _avg(real, "design_rounds"),
                "impl": _avg(real, "impl_turns"),
                "turns": _avg(real, "turns"),
                "duration": _avg(real, "duration"),
                "sim_rounds": _avg(real, "sim_rounds"),
                "sim_changes": _rate(real, "sim_changes"),
                "pr_fail": _rate(real, "pr_review_fail"),
                "ins": ins,
                "dele": dele,
                "out_tokens": out_tokens,
                "tok_per_loc": tok_per_loc,
            }
        )
    # Sort by model identity so the same agent groups together and the
    # table reads in a stable, scannable order (runtime, then name, per
    # role); run-count breaks ties.
    pairings.sort(
        key=lambda p: (
            p["agent_rt"] or "",
            p["agent"],
            p["sim_rt"] or "",
            p["sim"],
            -p["n"],
        )
    )
    return pairings


# ----- pipeline table rendering -----


def _rate_tier(rate: float) -> str:
    return "tier-green" if rate >= 0.6 else "tier-yellow" if rate >= 0.35 else "tier-red"


def _model_html(name: str, runtime: str | None) -> str:
    """Canonical model name + a uniform dim runtime tag (opencode/codex/claude)."""

    tag = f'<span class="rt-tag">{_escape(runtime)}</span>' if runtime else ""
    return f"{_escape(name)}{tag}"


def _sev_class(metric: tuple[float | None, int], amber: float, red: float) -> str:
    """Severity class for a 'higher is worse' rate (sim-changes, PR-fail)."""

    value = metric[0]
    if value is None:
        return ""
    if value >= red:
        return " sev-red"
    if value >= amber:
        return " sev-amber"
    return ""


def _cov_attr(metric: tuple[float | None, int], n: int) -> tuple[str, str]:
    """(extra-class, title-attr) marking a cell whose datum covers < n runs."""

    _, cov = metric
    if 0 < cov < n:
        return " partial", f' title="{cov}/{n} runs"'
    return "", ""


def _cell(
    metric: tuple[float | None, int],
    text: str,
    n: int,
    *,
    group: bool = False,
    sev: str = "",
) -> str:
    """One right-aligned numeric cell, dimmed + titled when coverage < n."""

    extra, title = _cov_attr(metric, n)
    classes = "num cell" + extra + sev + (" grp-start" if group else "")
    return f'<td class="{classes}"{title}>{text}</td>'


def _fmt_num(metric: tuple[float | None, int], decimals: int = 1) -> str:
    value = metric[0]
    return "—" if value is None else f"{value:.{decimals}f}"


def _fmt_pct(metric: tuple[float | None, int]) -> str:
    value = metric[0]
    return "—" if value is None else f"{value * 100:.0f}%"


def _fmt_ktok(metric: tuple[float | None, int]) -> str:
    value = metric[0]
    if value is None:
        return "—"
    return f"{value / 1000:.1f}k" if value >= 1000 else f"{value:.0f}"


def _fmt_minutes(metric: tuple[float | None, int]) -> str:
    value = metric[0]
    return "—" if value is None else f"{value / 60:.0f}m"


def _count_infra_runs(runs_root: Path) -> int:
    """Real (non-fake) runs the dashboard drops as infra failures."""

    if not runs_root.is_dir():
        return 0
    count = 0
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        stats = _read_json(entry / "stats.json", default=None)
        if (
            isinstance(stats, dict)
            and stats.get("actor_mode") != "fake"
            and stats.get("verdict") in _INFRA_VERDICTS
        ):
            count += 1
    return count


def _infra_only_pairings(runs_root: Path) -> list[dict[str, Any]]:
    """Pairings that ONLY ever infra-failed (no real run), busiest-fail first.

    These have no row in the main table (no model data to compare), so the
    footnote names them — otherwise a duo that always dies in infra would
    silently disappear. Cheap: reads stats.json verdict only, no metric
    extraction. Singleton (one-off) infra pairings are omitted as noise;
    we list duos that failed ≥2 times.
    """

    if not runs_root.is_dir():
        return []
    counts: dict[tuple[str, str | None, str, str | None], list[int]] = {}
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        stats = _read_json(entry / "stats.json", default=None)
        if not isinstance(stats, dict) or stats.get("actor_mode") == "fake":
            continue
        agent, agent_rt = _spec_canonical(stats.get("agent_model"))
        sim, sim_rt = _spec_canonical(stats.get("sim_model"))
        if not agent or not sim:
            continue
        real_infra = counts.setdefault((agent, agent_rt, sim, sim_rt), [0, 0])
        real_infra[1 if stats.get("verdict") in _INFRA_VERDICTS else 0] += 1

    out = [
        {"agent": a, "agent_rt": a_rt, "sim": s, "sim_rt": s_rt, "infra_n": infra}
        for (a, a_rt, s, s_rt), (real, infra) in counts.items()
        if real == 0 and infra >= _PAIRING_MIN_RUNS
    ]
    out.sort(key=lambda d: -d["infra_n"])
    return out


def _render_pipeline_table(
    pairings: list[dict[str, Any]],
    *,
    excluded_infra: int = 0,
    infra_only: list[dict[str, Any]] | None = None,
) -> str:
    """The pipeline tab: a grouped comparison table, one row per duo.

    Returns an empty-state note when no pairing reached `_PAIRING_MIN_RUNS`.
    """

    if not pairings:
        return (
            f'<p class="tagline">no pairing has ≥{_PAIRING_MIN_RUNS} real runs yet '
            "— nothing to compare.</p>"
        )

    body_rows: list[str] = []
    for p in pairings:
        n = p["n"]
        pair_label = (
            f"{_model_html(p['agent'], p['agent_rt'])}"
            f'<span class="sep">×</span>'
            f"{_model_html(p['sim'], p['sim_rt'])}"
        )
        land_tier = _rate_tier(p["pr_land"])
        accept_rate = p["pr_accept"][0]
        accept_tier = _rate_tier(accept_rate) if accept_rate is not None else "tier-unknown"
        pr_fail_text = _fmt_pct(p["pr_fail"])
        if (p["pr_fail"][0] or 0) >= 0.5:
            pr_fail_text += " ⚠"
        tok_per_loc = "—" if p["tok_per_loc"] is None else f"{p['tok_per_loc']:.0f}"
        infra_n = p["infra_n"]
        attempts = n + infra_n
        if infra_n == 0:
            infra_cell = '<td class="num cell"><span class="muted">0</span></td>'
        else:
            infra_rate = infra_n / attempts
            infra_sev = (
                " sev-red" if infra_rate >= 0.5 else " sev-amber" if infra_rate >= 0.3 else ""
            )
            infra_cell = (
                f'<td class="num cell{infra_sev}" '
                f'title="{infra_n} of {attempts} attempts infra-failed (excluded from metrics)">'
                f"{infra_n}</td>"
            )

        body_rows.append(
            "<tr>"
            f'<td class="pair-cell">{pair_label}</td>'
            f'<td class="num cell">{n}</td>'
            # outcome
            f'<td class="num cell grp-start"><span class="sim-dot {land_tier}"></span>'
            f"{p['pr_land'] * 100:.0f}%</td>"
            + _cell(
                p["pr_accept"],
                _fmt_pct(p["pr_accept"]),
                n,
                sev="" if accept_rate is None else f" {accept_tier}",
            )
            + infra_cell
            # exchange
            + _cell(p["design"], _fmt_num(p["design"]), n, group=True)
            + _cell(p["impl"], _fmt_num(p["impl"]), n)
            + _cell(p["turns"], _fmt_num(p["turns"], 0), n)
            + _cell(p["duration"], _fmt_minutes(p["duration"]), n)
            # review gates
            + _cell(p["sim_rounds"], _fmt_num(p["sim_rounds"]), n, group=True)
            + _cell(
                p["sim_changes"],
                _fmt_pct(p["sim_changes"]),
                n,
                sev=_sev_class(p["sim_changes"], 0.3, 0.6),
            )
            + _cell(p["pr_fail"], pr_fail_text, n, sev=_sev_class(p["pr_fail"], 0.25, 0.5))
            # code output
            + _cell(p["ins"], "+" + _fmt_num(p["ins"], 0), n, group=True)
            + _cell(p["dele"], "−" + _fmt_num(p["dele"], 0), n)
            + _cell(p["out_tokens"], _fmt_ktok(p["out_tokens"]), n)
            + f'<td class="num cell">{tok_per_loc}</td>'
            + "</tr>"
        )

    total_n = sum(p["n"] for p in pairings)

    def _cov_pct(key: str) -> str:
        covered = sum(p[key][1] for p in pairings)
        return f"{covered / total_n * 100:.0f}%" if total_n else "—"

    excluded = "fake-mode fixtures"
    if excluded_infra:
        excluded += f" + {excluded_infra} infra-failed runs"
    cov_note = (
        f'<p class="cov-note">{total_n} real runs · {len(pairings)} pairings '
        f"({excluded} excluded). "
        f"coverage — exchange phases {_cov_pct('design')} · "
        f"accepted PR {_cov_pct('pr_accept')} · "
        f"sim review {_cov_pct('sim_changes')} · "
        f"PR review {_cov_pct('pr_fail')} · tokens {_cov_pct('out_tokens')}. "
        "Dimmed cells = partial coverage (hover for k/n). "
        "Δ = changes-requested rate; PR fail = post-publish CLI review MUST_FIX rate.</p>"
    )
    if infra_only:
        items = ", ".join(
            f"{_model_html(d['agent'], d['agent_rt'])}"
            f'<span class="sep">×</span>{_model_html(d["sim"], d["sim_rt"])} '
            f'<span class="muted">({d["infra_n"]})</span>'
            for d in infra_only
        )
        cov_note += (
            '<p class="cov-note">Only ever infra-failed, no model data '
            f"(see the runs tab): {items}.</p>"
        )

    return f"""
<div class="pipeline-title">pipeline observability · grounded per-pairing metrics</div>
<div class="pipeline-scroll">
  <table class="pipeline-table">
    <thead>
      <tr class="grp-row">
        <th colspan="2"></th>
        <th class="grp-head grp-start" colspan="3">outcome</th>
        <th class="grp-head grp-start" colspan="4">exchange</th>
        <th class="grp-head grp-start" colspan="4">review gates</th>
        <th class="grp-head grp-start" colspan="4">code output</th>
      </tr>
      <tr>
        <th>pairing (agent × sim)</th>
        <th class="num" title="real runs the metrics are computed over (infra failures excluded)">runs</th>
        <th class="num grp-start" title="% of real runs that reached a draft PR">PR%</th>
        <th class="num" title="% of scoreable runs whose PR was ultimately merged; open/draft/unknown excluded">accepted</th>
        <th class="num" title="runs that died in infra (docker/clone/preflight/quota) — counted here, excluded from every other column">infra</th>
        <th class="num grp-start" title="design/grilling exchanges before code (flow_use)">design</th>
        <th class="num" title="agent turns implementing after SETTLED_DESIGN">impl</th>
        <th class="num" title="total turns (stats.json)">turns</th>
        <th class="num" title="wall-clock duration">dur</th>
        <th class="num grp-start" title="highest SIM review round reached">sim r</th>
        <th class="num" title="% of reviewed runs SIM requested changes at least once">sim Δ</th>
        <th class="num" title="% of reviewed runs the post-publish CLI review said MUST_FIX">PR fail</th>
        <th class="num grp-start" title="avg lines inserted (net diff)">+LoC</th>
        <th class="num" title="avg lines deleted (net diff)">−LoC</th>
        <th class="num" title="avg agent output tokens">out-tok</th>
        <th class="num" title="output tokens per line of net diff">tok/L</th>
      </tr>
    </thead>
    <tbody>
{"".join(body_rows)}
    </tbody>
  </table>
</div>
{cov_note}
"""


def _render_body(rows: list[dict[str, Any]], *, runs_root: Path) -> str:
    if not rows:
        return (
            f'<div class="topbar"><span class="crumb">contremaitre</span>'
            f'<span class="dim">/</span><span class="crumb">runs</span></div>'
            f"<h1>no runs</h1>"
            f'<p class="tagline">no <code>{VIEWER_FILENAME}</code> found under '
            f"<code>{_escape(str(runs_root))}</code></p>"
        )

    total = len(rows)
    n_pr = sum(1 for r in rows if r["pr_kind"] == "PUBLISHED")
    n_failed = sum(1 for r in rows if r["verdict"] == "FAILED_INFRA")
    n_no_pr = sum(1 for r in rows if str(r["verdict"]).startswith("NO_PR"))
    outcome_counts = {
        outcome.value: sum(1 for row in rows if row["pr_outcome"].get("outcome") == outcome.value)
        for outcome in PrOutcome
    }

    header = f"""
<div class="topbar">
  <span class="crumb">contremaitre</span>
  <span class="dim">/</span>
  <span class="crumb">runs</span>
</div>

<h1>runs · {total}</h1>
<p class="tagline">index of <code>{VIEWER_FILENAME}</code> across <code>{_escape(str(runs_root))}</code></p>

<div class="totals">
  <span class="item"><b>{total}</b> total</span>
  <span class="item"><span class="sim-dot tier-green"></span><b>{n_pr}</b> PR published</span>
  <span class="item"><span class="sim-dot tier-yellow"></span><b>{n_no_pr}</b> no PR</span>
  <span class="item"><span class="sim-dot tier-red"></span><b>{n_failed}</b> failed infra</span>
  <span class="item"><span class="sim-dot tier-green"></span><b>{outcome_counts[PrOutcome.ACCEPTED.value]}</b> accepted</span>
  <span class="item"><span class="sim-dot tier-red"></span><b>{outcome_counts[PrOutcome.REJECTED.value]}</b> rejected</span>
  <span class="item"><span class="sim-dot tier-yellow"></span><b>{outcome_counts[PrOutcome.PENDING.value]}</b> pending</span>
</div>
"""

    rendered_rows = "\n".join(_render_row(r) for r in rows)
    body_rows = f'<div class="group-block">\n{rendered_rows}\n</div>'

    legend = """
<div class="legend">
  <div class="legend-title">columns</div>
  <b>verdict</b> · the orchestrator's terminal verdict (READY_FOR_DRAFT_PR / PR_NEEDS_HUMAN / NO_PR_* / FAILED_INFRA).
  <b>models</b> · agent / sim (provider prefix stripped).
  <b>PR</b> · published PR title + link, or the kind if no PR (NO_PR, DRY_RUN, …).
  <b>accepted PR</b> · eventual GitHub outcome: merged, rejected, pending, or unavailable.
  Click any row to open that run's <code>viewer.html</code>.
</div>
"""

    footer = "<footer>generated by <code>contremaitre index</code></footer>"

    # Two tabs over the shared header: the per-run roster (default) and the
    # by-pairing pipeline-observability dashboard. Both draw on the same
    # runs root; the runtime toggle is a few lines of vanilla JS in
    # `_render_html` so the file stays self-contained.
    pipeline_html = _render_pipeline_table(
        _collect_pipeline_pairings(runs_root),
        excluded_infra=_count_infra_runs(runs_root),
        infra_only=_infra_only_pairings(runs_root),
    )
    tabbar = (
        '<div class="tabbar">'
        f'<button class="tab active" data-pane="pane-runs">runs · {total}</button>'
        '<button class="tab" data-pane="pane-pipeline">pipeline</button>'
        "</div>"
    )
    runs_pane = f'<div id="pane-runs" class="tabpane active">{body_rows}{legend}</div>'
    pipeline_pane = f'<div id="pane-pipeline" class="tabpane">{pipeline_html}</div>'
    return header + tabbar + runs_pane + pipeline_pane + footer


def _render_row(r: dict[str, Any]) -> str:
    tier = _verdict_tier(r["verdict"])

    # Title: repo + base + PR. Branch is demoted to the meta line below since
    # PR is the human-facing artifact and branch names tend to repeat the run id.
    repo_slug = r["repo"]
    repo_url = _github_url_from_slug(repo_slug)
    title_bits: list[str] = []
    if repo_slug:
        if repo_url:
            title_bits.append(
                f'<a href="{_escape(repo_url)}" target="_blank" rel="noopener">{_escape(repo_slug)}</a>'
            )
        else:
            title_bits.append(_escape(repo_slug))
        if r["base"]:
            title_bits.append(f'<span class="sim">base <code>{_escape(r["base"])}</code></span>')
        pr_title_bit = _pr_title_bit(r)
        if pr_title_bit:
            title_bits.append(pr_title_bit)
    else:
        title_bits.append(_escape(r["run_id"]))
    title_line = '<span class="sep">·</span>'.join(f" {b} " for b in title_bits).strip()

    # Run id + when become meta (still scannable, just demoted from title).
    id_line = (
        f'<div class="rep-meta"><code>{_escape(r["run_id"])}</code> · {_escape(r["when"])}</div>'
    )

    models_bits: list[str] = []
    if r["agent_model"]:
        models_bits.append(
            f'<span style="color:var(--agent)">agent</span> <code>{_escape(r["agent_model"])}</code>'
        )
    if r["sim_model"]:
        models_bits.append(
            f'<span style="color:var(--sim)">sim</span> <code>{_escape(r["sim_model"])}</code>'
        )
    # One badge per cli_review on disk (orchestrator rounds).
    # Side-by-side display lets a cross-reviewer comparison read
    # off the index — codex MUST_FIX next to claude NEEDS_ATTENTION etc.
    for cr in r["cli_reviews"]:
        cli_tier = _cli_review_tier(cr["verdict"])
        # For MUST_FIX rows only, surface whether the blocker is mechanical
        # (formatter/lint — actor's pre-publish gate should catch) vs
        # judgement (real bug — what we want the reviewer for). Lets the
        # index reveal the 4-vs-9-vs-mixed split at a glance.
        suffix_bits: list[str] = []
        blocker = cr["blocker"]
        if blocker:
            label = {"mechanical": "format", "mixed": "lint+bug", "semantic": "bug"}[blocker]
            suffix_bits.append(label)
        if cr["source"] != "orchestrator":
            suffix_bits.append(cr["source"])
        suffix = (
            f' <span style="color:var(--text-muted)">({" · ".join(suffix_bits)})</span>'
            if suffix_bits
            else ""
        )
        models_bits.append(
            f'<span class="sim-dot {cli_tier}"></span>'
            f'<span style="color:var(--accent)">{_escape(cr["tool"])} review</span>'
            f"{suffix}"
        )
    models_line = (
        " · ".join(models_bits) if models_bits else '<span class="no-eval">no model recorded</span>'
    )

    branch_line = (
        f'<div class="rep-meta">branch <code>{_escape(r["pr_branch"])}</code></div>'
        if r["pr_branch"]
        else ""
    )

    blurb = r["impl_complete"] or r["settled_preamble"] or r["reason"]
    if blurb and len(blurb) > 280:
        blurb = blurb[:280].rstrip() + "…"
    blurb_line = (
        f'<div class="rep-meta" style="color:var(--text-dim)">{_escape(blurb)}</div>'
        if blurb
        else ""
    )

    cost_label, is_free = _fmt_cost(r["cost_usd"])
    cost_pill = (
        f'<span class="score-pill pill-free"><b>{cost_label}</b></span>'
        if is_free
        else f'<span class="score-pill"><b>{cost_label}</b> cost</span>'
    )
    score_pills = (
        f'<span class="score-pill"><b>{_fmt_duration(r["duration_seconds"])}</b> duration</span>'
        f'<span class="score-pill"><b>{_fmt_turns(r["turns"])}</b> turns</span>'
        f"{cost_pill}"
        f"{_diffstat_pill(r['diffstat'])}"
        f"{_tokens_pill(r.get('tokens'))}"
        f"{_pr_outcome_pill(r['pr_outcome'])}"
    )

    verdict_display = _verdict_label(r["verdict"])
    headline = f"""
<div class="rep-headline">
  <div class="composite" style="font-size:13px"><span class="{tier}">●</span> <span class="{tier}">{_escape(verdict_display)}</span></div>
  <a class="view-link" href="{_escape(r["viewer_href"])}">view</a>
</div>
""".strip()

    return f"""
<div class="rep rep-eval">
  <div class="rep-left">
    <div class="rep-label">{title_line}</div>
    {id_line}
    <div class="rep-meta">{models_line}</div>
    {branch_line}
    {blurb_line}
    <div class="rep-scores">{score_pills}</div>
  </div>
  {headline}
</div>
""".strip()


def _pr_title_bit(r: dict[str, Any]) -> str:
    """Inline PR fragment for the title row.

    Returns a linked `PR #N · <title>` when a PR was published, or a small
    `NO_PR`-style pill when the run terminated without one. Empty string
    means "don't add a bullet for this row".
    """

    kind = r["pr_kind"]
    url = r["pr_url"]
    title = r["pr_title"]
    if url:
        pr_no = url.rstrip("/").rsplit("/", 1)[-1]
        label = f"<b>PR #{_escape(pr_no)}</b>"
        if title:
            label += f" · {_escape(title)}"
        return (
            f'<a class="pr-link" href="{_escape(url)}" target="_blank" rel="noopener">{label} ↗</a>'
        )
    if kind:
        return f'<span class="score-pill pr-pill-no-pr"><b>{_escape(kind)}</b></span>'
    return ""


def _escape(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
