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

    cli_review = _read_cli_review_summary(run_dir)

    return {
        "run_id": run_dir.name,
        "viewer_href": f"{run_dir.name}/{VIEWER_FILENAME}",
        "when": _format_when(run_dir.name),
        "agent_model": _short_model(stats.get("agent_model")),
        "sim_model": _short_model(stats.get("sim_model")),
        "extra_model": _short_model(stats.get("extra_reviewer_model")),
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
        "pr_kind": (pr or {}).get("kind") if isinstance(pr, dict) else None,
        "pr_url": (pr or {}).get("url") if isinstance(pr, dict) else None,
        "pr_title": (pr or {}).get("title") if isinstance(pr, dict) else None,
        "pr_branch": (pr or {}).get("branch") if isinstance(pr, dict) else None,
        "cli_review_tool": cli_review[0] if cli_review else None,
        "cli_review_verdict": cli_review[1] if cli_review else None,
        "cli_review_blocker": cli_review[2] if cli_review else None,
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


def _read_cli_review_summary(run_dir: Path) -> tuple[str, str | None, str | None] | None:
    """`(tool, verdict_key, blocker_class)` derived from `<tool>_review.md`.

    Skips the I/O round-trip into guardrails — the posted markdown file
    name carries the tool, and the agent's verdict key (MUST_FIX /
    NEEDS_ATTENTION / LOOKS_GOOD) lives on line 1 per the prompt spec.
    `blocker_class` is `_classify_blocker`'s read of the issue bullets,
    or `None` for non-MUST_FIX verdicts (where the classification is
    not actionable). Returns `None` when no cli_review.md is present.
    """

    for tool in ("codex", "claude"):
        path = run_dir / f"{tool}_review.md"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return (tool, None, None)
        # Verdict key is on line 1 after the H3 header the orchestrator
        # prepends. Scan a handful of non-blank lines for the first match.
        verdict: str | None = None
        for line in text.splitlines()[:8]:
            for key in ("MUST_FIX", "NEEDS_ATTENTION", "LOOKS_GOOD"):
                if key in line:
                    verdict = key
                    break
            if verdict:
                break
        blocker = _classify_blocker(text) if verdict == "MUST_FIX" else None
        return (tool, verdict, blocker)
    return None


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


def _short_model(model: str | None) -> str | None:
    if not model:
        return None
    # `provider/model-name` → `model-name`
    return model.split("/", 1)[-1]


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
</style>
</head>
<body>
<div class="page page-wide">
{body}
</div>
</body>
</html>
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
</div>
"""

    rendered_rows = "\n".join(_render_row(r) for r in rows)
    body_rows = f'<div class="group-block">\n{rendered_rows}\n</div>'

    legend = """
<div class="legend">
  <div class="legend-title">columns</div>
  <b>verdict</b> · the orchestrator's terminal verdict (READY_FOR_DRAFT_PR / NO_PR_* / FAILED_INFRA).
  <b>models</b> · agent / sim / extra-reviewer (provider prefix stripped).
  <b>PR</b> · published PR title + link, or the kind if no PR (NO_PR, DRY_RUN, …).
  Click any row to open that run's <code>viewer.html</code>.
</div>
"""

    footer = "<footer>generated by <code>contremaitre index</code></footer>"

    return header + body_rows + legend + footer


def _render_row(r: dict[str, Any]) -> str:
    tier = _verdict_tier(r["verdict"])

    # Title: repo + base + PR. Branch is demoted to the meta line below since
    # PR is the human-facing artifact and branch names tend to repeat the run id.
    repo_slug = r["repo"]
    repo_url = _github_url_from_slug(repo_slug)
    title_bits: list[str] = []
    if repo_slug:
        if repo_url:
            title_bits.append(f'<a href="{_escape(repo_url)}" target="_blank" rel="noopener">{_escape(repo_slug)}</a>')
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
    id_line = f'<div class="rep-meta">' f'<code>{_escape(r["run_id"])}</code> · {_escape(r["when"])}' f"</div>"

    models_bits: list[str] = []
    if r["agent_model"]:
        models_bits.append(f'<span style="color:var(--agent)">agent</span> <code>{_escape(r["agent_model"])}</code>')
    if r["sim_model"]:
        models_bits.append(f'<span style="color:var(--sim)">sim</span> <code>{_escape(r["sim_model"])}</code>')
    if r["extra_model"]:
        models_bits.append(f'<span style="color:var(--extra)">extra</span> <code>{_escape(r["extra_model"])}</code>')
    if r["cli_review_tool"]:
        # Colored sim-dot keyed on the verdict (MUST_FIX/NEEDS_ATTENTION/
        # LOOKS_GOOD) — keeps the house style consistent with the other
        # tier dots on the page.
        cli_tier = _cli_review_tier(r["cli_review_verdict"])
        # For MUST_FIX rows only, surface whether the blocker is mechanical
        # (formatter/lint — actor's pre-publish gate should catch) vs
        # judgement (real bug — what we want the reviewer for). Lets the
        # index reveal the 4-vs-9-vs-mixed split at a glance.
        blocker_suffix = ""
        blocker = r["cli_review_blocker"]
        if blocker:
            label = {"mechanical": "format", "mixed": "lint+bug", "semantic": "bug"}[blocker]
            blocker_suffix = f' <span style="color:var(--text-muted)">({label})</span>'
        models_bits.append(
            f'<span class="sim-dot {cli_tier}"></span>'
            f'<span style="color:var(--accent)">{_escape(r["cli_review_tool"])} review</span>'
            f"{blocker_suffix}"
        )
    models_line = " · ".join(models_bits) if models_bits else '<span class="no-eval">no model recorded</span>'

    branch_line = f'<div class="rep-meta">branch <code>{_escape(r["pr_branch"])}</code></div>' if r["pr_branch"] else ""

    blurb = r["impl_complete"] or r["settled_preamble"] or r["reason"]
    if blurb and len(blurb) > 280:
        blurb = blurb[:280].rstrip() + "…"
    blurb_line = f'<div class="rep-meta" style="color:var(--text-dim)">{_escape(blurb)}</div>' if blurb else ""

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
        return f'<a class="pr-link" href="{_escape(url)}" target="_blank" rel="noopener">' f"{label} ↗</a>"
    if kind:
        return f'<span class="score-pill pr-pill-no-pr"><b>{_escape(kind)}</b></span>'
    return ""


def _escape(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
