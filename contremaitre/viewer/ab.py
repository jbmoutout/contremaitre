"""Head-to-head A/B comparison report for one golden case × two configs.

Renders a single self-contained `<runs_root>/ab--<case_id>--<a>-vs-<b>.html`
comparing two *arms* (configs) run against the same pinned task (target_url +
base + expected_base_sha). One arm differs from the other in the models it
runs — the single-variable rule applied to a model swap.

Everything displayed is grounded in artifacts the orchestrator already writes,
read through the same extractors the eval canary uses (`eval.check_run` for
the two-layer scorecard, `RunArtifacts` for streams/diffstat/phases). No LLM
judge is invoked at report time; the cli_review verdict IS the judge signal,
captured during the runs themselves.

Scientific-honesty rules baked into the layout:

- **Provenance first.** The report opens with the pinned input, both arm
  configs, and a validity checklist (base-SHA pinning, judge parity,
  environment uniformity, clean tree, artifact completeness). A comparison
  whose checklist fails is labelled as such, not silently rendered.
- **Infra failures are excluded from metrics but never hidden.** Runs whose
  terminal verdict is in `INFRA_VERDICTS` appear in the roster with a badge
  and in the per-arm attempt counts, but contribute nothing to the metric
  vectors — counting a docker failure as model behaviour would corrupt
  both arms.
- **No p-values at n=3.** Per-metric "signal" is a range-separation test:
  all values of one arm strictly beyond all values of the other (for n=3 vs
  n=3 this is the extreme rank ordering, one-sided rank-sum p ≈ 0.05).
  Overlapping ranges are labelled `overlap`, not spun into a winner.
- **Direction-aware, but only where direction is defensible.** Scope metrics
  (files changed, LoC, review rounds) have no "better" direction and are
  rendered as context, never as wins.

The per-run traces stay in each run's `viewer.html` (conversation, timeline,
tool cards); the report links into them and embeds only the head-to-head
essentials (final diff, cli_review body, SETTLED preamble) as collapsible
sections.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..costs import sum_token_usage_in_events
from ..eval import (
    CanaryReport,
    CaseDef,
    ConfigDef,
    case_dir_for,
    check_run,
    latest_n_runs_for_case,
    load_case,
    load_config,
)
from ..run_artifacts import RunArtifacts
from . import VIEWER_FILENAME
from .index import (
    _cli_review_tier,
    _format_when,
    _fmt_token_count,
    _github_url_from_slug,
    _read_cli_reviews,
    _read_impl_complete,
    _read_settled_preamble,
    _slug_from_git_url,
    _verdict_tier,
)

_HERE = Path(__file__).resolve().parent
_CSS_PATH = _HERE / "_styles.css"

# Same exclusion set as the index's pipeline tab: these runs died for reasons
# unrelated to model quality and would understate every per-arm metric.
INFRA_VERDICTS = frozenset({"FAILED_INFRA", "QUOTA_EXHAUSTED"})

_DIFF_EMBED_CAP = 120_000
_REVIEW_EMBED_CAP = 40_000


def ab_report_filename(case_id: str, config_a: str, config_b: str) -> str:
    return f"ab--{case_id}--{config_a}-vs-{config_b}.html"


# ---------------------------------------------------------------------------
# Per-run row assembly
# ---------------------------------------------------------------------------


def _read_json(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_text_capped(path: Path, cap: int) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > cap:
        text = text[:cap].rstrip() + f"\n… [truncated {len(text) - cap} chars]"
    return text


def _final_diff_text(run_dir: Path) -> str:
    """The diff that drove the terminal verdict, for head-to-head reading.

    Highest-numbered `review_diff_round_<N>.diff` (one per review round —
    same selection as `eval._diff_stats`), falling back to the last
    `review_input/diff.patch` snapshot.
    """

    diffs = sorted(run_dir.glob("review_diff_round*.diff"))
    src = diffs[-1] if diffs else run_dir / "review_input" / "diff.patch"
    return _read_text_capped(src, _DIFF_EMBED_CAP)


def collect_run_row(case: CaseDef, config: ConfigDef, run_dir: Path) -> dict[str, Any]:
    """One run → scorecard + roster extras. The unit both arms are built from."""

    report = check_run(case, config, run_dir)
    stats = _read_json(run_dir / "stats.json", default={}) or {}
    run_config = _read_json(run_dir / "run_config.json", default={}) or {}
    pr = _read_json(run_dir / "pr.json", default=None)

    arts = RunArtifacts.from_run_dir(run_dir)
    agent_tok = sum_token_usage_in_events(arts.raw_export())
    sim_tok = sum_token_usage_in_events(arts.sim_raw_export())
    all_tok = arts.token_usage_all()
    phases = arts.phases()

    verdict = report.headline.get("terminal_verdict")
    review_md = ""
    if config.cli_reviewer in ("codex", "claude"):
        review_md = _read_text_capped(
            run_dir / f"{config.cli_reviewer}_review.md", _REVIEW_EMBED_CAP
        )

    return {
        "run_id": run_dir.name,
        "run_dir": run_dir,
        "viewer_href": f"{run_dir.name}/{VIEWER_FILENAME}",
        "when": _format_when(run_dir.name),
        "report": report,
        "healthy": verdict not in INFRA_VERDICTS,
        "reason": (stats.get("reason") or "").strip(),
        "pr_url": (pr or {}).get("url") if isinstance(pr, dict) else None,
        "pr_title": (pr or {}).get("title") if isinstance(pr, dict) else None,
        "pr_kind": (pr or {}).get("kind") if isinstance(pr, dict) else None,
        "run_config": run_config,
        "resolved_agent_model": arts.resolved_model(),
        "resolved_sim_model": arts.resolved_model(sim=True),
        "cli_reviews": _read_cli_reviews(run_dir),
        "impl_complete": _read_impl_complete(run_dir),
        "settled_preamble": _read_settled_preamble(run_dir),
        "diff_text": _final_diff_text(run_dir),
        "review_md": review_md,
        "extras": {
            "pr_landed": verdict in {"READY_FOR_DRAFT_PR", "PR_NEEDS_HUMAN"},
            "agent_tokens_in": agent_tok.get("input") or None,
            "agent_tokens_out": (agent_tok.get("output") or 0)
            + (agent_tok.get("reasoning") or 0)
            or None,
            "sim_tokens_out": (sim_tok.get("output") or 0) + (sim_tok.get("reasoning") or 0)
            or None,
            "tokens_total": sum(all_tok.values()) or None,
            "design_rounds": phases.get("grilling_exchanges"),
            "impl_turns": phases.get("impl_turns"),
        },
    }


# ---------------------------------------------------------------------------
# Metric registry — the head-to-head table is driven entirely by this list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    section: str
    better: str  # "higher" | "lower" | "context"
    kind: str  # "num" | "bool" | "cat"
    fmt: str = "num2"  # num0 | num1 | num2 | usd | dur | tok
    note: str = ""
    hl: bool = False  # headline row — brighter label, feeds the scoreboard order
    get: Callable[[dict[str, Any]], Any] = lambda row: None


def _h(field: str) -> Callable[[dict[str, Any]], Any]:
    return lambda row: row["report"].headline.get(field)


def _d(group: str, field: str) -> Callable[[dict[str, Any]], Any]:
    return lambda row: row["report"].diagnostic.get(group, {}).get(field)


def _x(field: str) -> Callable[[dict[str, Any]], Any]:
    return lambda row: row["extras"].get(field)


METRICS: tuple[Metric, ...] = (
    # outcome ---------------------------------------------------------------
    Metric("terminal_verdict", "terminal verdict", "outcome", "context", "cat", hl=True,
           get=_h("terminal_verdict")),
    Metric("terminal_score", "terminal score", "outcome", "higher", "num", "num2",
           note="READY_FOR_DRAFT_PR=1.0 · PR_NEEDS_HUMAN=0.5 · NO_PR_*=0.0", get=_h("terminal_score")),
    Metric("pr_landed", "draft PR landed", "outcome", "higher", "bool", hl=True, get=_x("pr_landed")),
    Metric("hard_gates_passed", "hard gates passed", "outcome", "higher", "bool",
           get=_d("format_compliance", "hard_gates_passed")),
    # LLM judge (cli_review) --------------------------------------------------
    Metric("cli_review_verdict", "judge verdict", "LLM judge (cli_review)", "context", "cat", hl=True,
           get=_h("cli_review_verdict_key")),
    Metric("cli_review_score", "judge score", "LLM judge (cli_review)", "higher", "num", "num2",
           note="LOOKS_GOOD=1.0 · NEEDS_ATTENTION=0.5 · MUST_FIX=0.0", hl=True,
           get=_h("cli_review_score")),
    Metric("cli_findings_weighted", "findings (weighted)", "LLM judge (cli_review)", "lower", "num", "num1",
           note="issue×3 + suggestion×2 + nit×1", hl=True, get=_h("cli_findings_weighted")),
    Metric("cli_issue_count", "issue count", "LLM judge (cli_review)", "lower", "num", "num1",
           get=_h("cli_issue_count")),
    Metric("cli_suggestion_count", "suggestion count", "LLM judge (cli_review)", "lower", "num", "num1",
           get=_h("cli_suggestion_count")),
    Metric("cli_nit_count", "nit count", "LLM judge (cli_review)", "lower", "num", "num1",
           get=_h("cli_nit_count")),
    Metric("cli_citation_density", "citation density", "LLM judge (cli_review)", "higher", "num", "num2",
           note="fraction of findings citing path:line", get=_h("cli_citation_density")),
    Metric("cli_review_parse_ok", "judge output parseable", "LLM judge (cli_review)", "higher", "bool",
           get=_d("format_compliance", "cli_review_parse_ok")),
    # process / discipline ----------------------------------------------------
    Metric("agent_discipline_score", "agent discipline", "process / discipline", "higher", "num", "num2",
           note="exploration + sim_useful_call_ratio + self_verified", hl=True,
           get=_h("agent_discipline_score")),
    Metric("settled_before_code", "settled before code", "process / discipline", "higher", "bool",
           get=_d("discipline", "settled_before_code")),
    Metric("self_verified", "self-verified (ran tests)", "process / discipline", "higher", "bool",
           get=_d("discipline", "self_verified")),
    Metric("exploration_convergence", "exploration convergence", "process / discipline", "context", "cat",
           get=_d("discipline", "exploration_convergence")),
    Metric("sim_useful_call_ratio", "SIM useful-call ratio", "process / discipline", "higher", "num", "num2",
           note="fraction of SIM greps cited in its verdict", get=_d("discipline", "sim_useful_call_ratio")),
    Metric("context_pollution_events", "context pollution events", "process / discipline", "lower", "num", "num1",
           get=_d("discipline", "context_pollution_events")),
    Metric("runtime_install_required", "runtime install required", "process / discipline", "lower", "bool",
           get=_d("discipline", "runtime_install_required")),
    Metric("time_to_settled", "time to SETTLED (s)", "process / discipline", "context", "num", "num0",
           get=_d("discipline", "time_to_settled_design_seconds")),
    Metric("tokens_to_settled", "tokens to SETTLED", "process / discipline", "context", "num", "tok",
           get=_d("discipline", "tokens_to_settled_design")),
    Metric("implementation_complete_written", "IMPLEMENTATION_COMPLETE written", "process / discipline",
           "higher", "bool", get=_d("format_compliance", "implementation_complete_written")),
    # review gates -------------------------------------------------------------
    Metric("review_rounds", "SIM review rounds", "review gates", "context", "num", "num1",
           get=_h("review_rounds")),
    Metric("total_checks_performed", "SIM checks performed", "review gates", "context", "num", "num1",
           get=_d("review_depth", "total_checks_performed")),
    Metric("total_required_changes", "SIM required changes", "review gates", "context", "num", "num1",
           get=_d("review_depth", "total_required_changes")),
    Metric("sim_review_confidence", "SIM review confidence", "review gates", "higher", "num", "num2",
           get=_d("review_depth", "sim_review_confidence")),
    Metric("process_reliability", "process reliability", "review gates", "higher", "num", "num2",
           get=_d("review_depth", "process_reliability")),
    Metric("sim_verdicts_parse_ok", "SIM verdicts parseable", "review gates", "higher", "bool",
           get=_d("format_compliance", "sim_verdicts_parse_ok")),
    # code output ---------------------------------------------------------------
    Metric("files_changed", "files changed", "code output", "context", "num", "num1",
           get=_h("files_changed")),
    Metric("loc_added", "LoC added", "code output", "context", "num", "num0",
           get=_d("diff_detail", "loc_added")),
    Metric("loc_deleted", "LoC deleted", "code output", "context", "num", "num0",
           get=_d("diff_detail", "loc_deleted")),
    Metric("loc_net_delta", "LoC net delta", "code output", "context", "num", "num0", hl=True,
           get=_h("loc_net_delta")),
    Metric("design_rounds", "design/grilling exchanges", "code output", "context", "num", "num1",
           get=_x("design_rounds")),
    Metric("impl_turns", "implementation turns", "code output", "context", "num", "num1",
           get=_x("impl_turns")),
    # efficiency / spend ----------------------------------------------------------
    Metric("wall_seconds", "wall clock", "efficiency / spend", "lower", "num", "dur", hl=True,
           get=_h("wall_seconds")),
    Metric("turns", "total turns", "efficiency / spend", "lower", "num", "num0",
           get=_d("efficiency", "turns")),
    Metric("cost_usd", "recorded cost", "efficiency / spend", "lower", "num", "usd",
           note="$0 for subscription/free runs — tokens are the real spend", hl=True,
           get=_h("cost_usd")),
    Metric("agent_tokens_out", "agent output tokens", "efficiency / spend", "context", "num", "tok",
           note="the code-production signal", hl=True, get=_x("agent_tokens_out")),
    Metric("agent_tokens_in", "agent input tokens", "efficiency / spend", "context", "num", "tok",
           get=_x("agent_tokens_in")),
    Metric("sim_tokens_out", "SIM output tokens", "efficiency / spend", "context", "num", "tok",
           get=_x("sim_tokens_out")),
    Metric("tokens_total", "total tokens (all streams)", "efficiency / spend", "context", "num", "tok",
           note="agent + SIM + CLI reviewers", get=_x("tokens_total")),
    Metric("agent_tool_call_count", "agent tool calls", "efficiency / spend", "context", "num", "num0",
           get=_d("efficiency", "agent_tool_call_count")),
    Metric("sim_tool_call_count", "SIM tool calls", "efficiency / spend", "context", "num", "num0",
           get=_d("efficiency", "sim_tool_call_count")),
)


# ---------------------------------------------------------------------------
# Comparison math (pure — unit-tested directly)
# ---------------------------------------------------------------------------


def metric_vector(metric: Metric, rows: list[dict[str, Any]]) -> list[Any]:
    """Metric values over the *healthy* runs of one arm, absent values dropped."""

    out = []
    for row in rows:
        if not row["healthy"]:
            continue
        v = metric.get(row)
        if v is not None:
            out.append(v)
    return out


def summarize_num(values: list[Any]) -> dict[str, float | int | None]:
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return {"median": None, "min": None, "max": None, "n": 0}
    return {
        "median": statistics.median(nums),
        "min": min(nums),
        "max": max(nums),
        "n": len(nums),
    }


def separation(a: list[Any], b: list[Any]) -> str | None:
    """Range-separation verdict between two numeric vectors.

    Returns `"a"` / `"b"` when every value of that arm strictly exceeds every
    value of the other (the extreme rank ordering — at n=3 vs n=3 this is a
    one-sided rank-sum p ≈ 0.05), `"overlap"` when ranges intersect, and
    `None` when either side has fewer than 2 values (no range to compare).
    Direction here is raw "greater"; the caller maps it to better/worse via
    the metric's `better` field.
    """

    na = [v for v in a if isinstance(v, (int, float)) and not isinstance(v, bool)]
    nb = [v for v in b if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(na) < 2 or len(nb) < 2:
        return None
    if min(na) > max(nb):
        return "a"
    if min(nb) > max(na):
        return "b"
    return "overlap"


def rate(values: list[Any]) -> tuple[float | None, int]:
    """(fraction of present bools that are true, count present)."""

    bools = [v for v in values if isinstance(v, bool)]
    if not bools:
        return None, 0
    return sum(1 for v in bools if v) / len(bools), len(bools)


def mix(values: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        if v is None:
            continue
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def compare_metric(metric: Metric, rows_a: list[dict[str, Any]], rows_b: list[dict[str, Any]]) -> dict[str, Any]:
    """One registry entry → the full comparison record one table row needs."""

    va = metric_vector(metric, rows_a)
    vb = metric_vector(metric, rows_b)
    rec: dict[str, Any] = {"metric": metric, "values_a": va, "values_b": vb}

    if metric.kind == "num":
        rec["a"] = summarize_num(va)
        rec["b"] = summarize_num(vb)
        rec["sep"] = separation(va, vb)
        ma, mb = rec["a"]["median"], rec["b"]["median"]
        rec["delta"] = (mb - ma) if (ma is not None and mb is not None) else None
        rec["winner"] = _winner(metric.better, rec["sep"])
    elif metric.kind == "bool":
        ra, na = rate(va)
        rb, nb = rate(vb)
        rec["a"] = {"rate": ra, "n": na}
        rec["b"] = {"rate": rb, "n": nb}
        rec["delta"] = (rb - ra) if (ra is not None and rb is not None) else None
        rec["sep"] = None
        rec["winner"] = None
    else:  # cat
        rec["a"] = {"mix": mix(va)}
        rec["b"] = {"mix": mix(vb)}
        rec["delta"] = None
        rec["sep"] = None
        rec["winner"] = None
    return rec


def _winner(better: str, sep: str | None) -> str | None:
    """Map a raw range separation to a win attribution, direction-aware.

    Only `higher`/`lower` metrics can produce a winner; `context` metrics
    report separation without attribution (`"differs"` handled at render).
    """

    if sep not in ("a", "b") or better == "context":
        return None
    if better == "higher":
        return sep
    return "b" if sep == "a" else "a"


# ---------------------------------------------------------------------------
# Validity checks
# ---------------------------------------------------------------------------

# run_config.json fields that must be uniform across every run of BOTH arms
# for the comparison to isolate the model swap (system minus models).
_ENV_FIELDS = ("contremaitre_git_sha", "dockerfile_sha256", "skills_lock_sha256", "prompt_hashes")


def validity_checks(
    case: CaseDef,
    config_a: ConfigDef,
    config_b: ConfigDef,
    rows_a: list[dict[str, Any]],
    rows_b: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The checklist that decides whether the delta is attributable.

    Each entry: `{"name", "status" ("pass"|"warn"|"fail"), "detail"}`.
    """

    checks: list[dict[str, Any]] = []
    all_rows = rows_a + rows_b

    # 1. Input pinning — every run's captured base_sha matches the case pin.
    if case.expected_base_sha:
        bad = [
            r["run_id"]
            for r in all_rows
            if not (r["report"].base_sha or "").startswith(case.expected_base_sha)
        ]
        checks.append(
            {
                "name": f"base pinned @ {case.expected_base_sha[:12]}",
                "status": "fail" if bad else "pass",
                "detail": f"mismatched: {', '.join(bad)}" if bad else f"all {len(all_rows)} runs match",
            }
        )
    else:
        checks.append(
            {
                "name": "base pinned",
                "status": "warn",
                "detail": "case.toml has no expected_base_sha — input not pinned",
            }
        )

    # 2. Judge parity — a different cli_reviewer is a different measurement
    # instrument, not a different system; judge metrics stop being comparable.
    if config_a.cli_reviewer == config_b.cli_reviewer:
        checks.append(
            {
                "name": "same judge (cli_reviewer)",
                "status": "pass",
                "detail": f"both arms judged by {config_a.cli_reviewer}",
            }
        )
    else:
        checks.append(
            {
                "name": "same judge (cli_reviewer)",
                "status": "fail",
                "detail": (
                    f"arm A judged by {config_a.cli_reviewer}, arm B by "
                    f"{config_b.cli_reviewer} — LLM-judge metrics are NOT comparable"
                ),
            }
        )

    # 3. Single-variable — arms should differ only in model fields.
    if config_a.publish_mode != config_b.publish_mode:
        checks.append(
            {
                "name": "single variable (models only)",
                "status": "warn",
                "detail": f"publish_mode differs ({config_a.publish_mode} vs {config_b.publish_mode})",
            }
        )
    else:
        moved = [
            name
            for name, va, vb in (
                ("agent_model", config_a.agent_model, config_b.agent_model),
                ("sim_model", config_a.sim_model, config_b.sim_model),
            )
            if va != vb
        ]
        checks.append(
            {
                "name": "single variable (models only)",
                "status": "pass" if moved else "warn",
                "detail": f"moved: {', '.join(moved)}" if moved else "configs are identical — nothing to compare",
            }
        )

    # 4. Environment uniformity — contremaitre code / prompts / image / skills
    # identical across all 2n runs, so time doesn't confound the arm split.
    drifted: list[str] = []
    for field in _ENV_FIELDS:
        seen = {json.dumps(r["run_config"].get(field), sort_keys=True) for r in all_rows}
        if len(seen) > 1:
            drifted.append(field)
    checks.append(
        {
            "name": "environment uniform across runs",
            "status": "warn" if drifted else "pass",
            "detail": (
                f"drifted: {', '.join(drifted)} — contremaitre changed between runs; "
                "time may confound the comparison"
            )
            if drifted
            else "code, prompts, image, skills identical for all runs",
        }
    )

    # 5. Clean tree.
    dirty = [r["run_id"] for r in all_rows if r["run_config"].get("contremaitre_git_dirty")]
    checks.append(
        {
            "name": "clean contremaitre tree",
            "status": "warn" if dirty else "pass",
            "detail": f"dirty during: {', '.join(dirty)}" if dirty else "all runs on committed code",
        }
    )

    # 6. Artifact completeness / per-run health.
    not_ok = [r["run_id"] for r in all_rows if r["healthy"] and not r["report"].ok]
    infra = [r["run_id"] for r in all_rows if not r["healthy"]]
    status = "fail" if not_ok else ("warn" if infra else "pass")
    detail_bits = []
    if not_ok:
        detail_bits.append(f"check_run failed: {', '.join(not_ok)}")
    if infra:
        detail_bits.append(f"infra-failed (excluded from metrics): {', '.join(infra)}")
    checks.append(
        {
            "name": "all runs healthy + artifacts complete",
            "status": status,
            "detail": "; ".join(detail_bits) or "every run ok",
        }
    )

    # 7. Sample size floor.
    n_a = sum(1 for r in rows_a if r["healthy"])
    n_b = sum(1 for r in rows_b if r["healthy"])
    checks.append(
        {
            "name": "sample size (n≥3 per arm)",
            "status": "pass" if min(n_a, n_b) >= 3 else "warn",
            "detail": f"arm A n={n_a}, arm B n={n_b}"
            + ("" if min(n_a, n_b) >= 3 else " — below the n=3 floor; treat every delta as anecdote"),
        }
    )

    return checks


# ---------------------------------------------------------------------------
# Assembly + entry point
# ---------------------------------------------------------------------------


def assemble_ab_data(
    *,
    project_root: Path,
    runs_root: Path,
    case_id: str,
    config_a: str,
    config_b: str,
    n: int,
) -> dict[str, Any]:
    """Collect both arms and every comparison record. Pure data; no HTML."""

    case_dir = case_dir_for(project_root, case_id)
    case = load_case(case_dir)
    cfg_a = load_config(case_dir, config_a)
    cfg_b = load_config(case_dir, config_b)

    dirs_a = latest_n_runs_for_case(runs_root, case_id, config_a, n)
    dirs_b = latest_n_runs_for_case(runs_root, case_id, config_b, n)
    rows_a = [collect_run_row(case, cfg_a, d) for d in dirs_a]
    rows_b = [collect_run_row(case, cfg_b, d) for d in dirs_b]

    return {
        "case": case,
        "config_a": cfg_a,
        "config_b": cfg_b,
        "n_requested": n,
        "rows_a": rows_a,
        "rows_b": rows_b,
        "checks": validity_checks(case, cfg_a, cfg_b, rows_a, rows_b),
        "comparisons": [compare_metric(m, rows_a, rows_b) for m in METRICS],
    }


def build_ab_report(
    *,
    project_root: Path,
    runs_root: Path,
    case_id: str,
    config_a: str,
    config_b: str,
    n: int,
) -> Path:
    """Assemble + render + write. Returns the written report path."""

    data = assemble_ab_data(
        project_root=project_root,
        runs_root=runs_root,
        case_id=case_id,
        config_a=config_a,
        config_b=config_b,
        n=n,
    )
    html = _render_html(data)
    out = runs_root / ab_report_filename(case_id, config_a, config_b)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _fmt_value(v: Any, fmt: str) -> str:
    if v is None:
        return "—"
    if fmt == "usd":
        return f"${float(v):.3f}"
    if fmt == "dur":
        s = int(round(float(v)))
        m, r = divmod(s, 60)
        return f"{m}m {r:02d}s" if m else f"{r}s"
    if fmt == "tok":
        return _fmt_token_count(int(v))
    if fmt == "num0":
        return f"{float(v):.0f}"
    if fmt == "num1":
        return f"{float(v):.1f}"
    return f"{float(v):.2f}"


def _fmt_summary(summary: dict[str, Any], fmt: str) -> str:
    med = summary.get("median")
    if med is None:
        return "—"
    lo, hi = summary.get("min"), summary.get("max")
    if lo == hi:
        return _fmt_value(med, fmt)
    return f"{_fmt_value(med, fmt)} <span class='rng'>[{_fmt_value(lo, fmt)} – {_fmt_value(hi, fmt)}]</span>"


def _fmt_delta(delta: Any, fmt: str, kind: str) -> str:
    if delta is None:
        return "—"
    if kind == "bool":
        return f"{delta * 100:+.0f}pp"
    sign = "+" if delta >= 0 else "−"
    return f"{sign}{_fmt_value(abs(delta), fmt)}"


def _escape(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_AB_CSS = """
/* A/B report additive styles over _styles.css.
   Reading hierarchy, brightest first: scoreboard values → table medians
   (winner tinted green) → labels → Δ → per-run points/ranges. The house
   --text-dim (#444) is reserved for genuinely secondary text; measured
   values never use it. */
.arm-a { color: var(--agent); }
.arm-b { color: var(--sim); }
.exp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 18px 0; }
.exp-card { border: 1px solid var(--surface-2); border-radius: 4px; padding: 14px 16px; font-size: 12px; }
.exp-card h3 { margin: 0 0 10px; font-size: 12px; letter-spacing: 0.04em; }
.exp-card .kv { display: grid; grid-template-columns: 130px 1fr; gap: 3px 12px; color: #9AA0A8; }
.exp-card .kv b { color: var(--text-bright); font-weight: 500; word-break: break-all; }
.checklist { margin: 10px 0 26px; font-size: 12px; }
.checklist li { list-style: none; padding: 4px 0; color: #9AA0A8; }
.checklist .st { display: inline-block; width: 52px; font-weight: 600; }
.checklist .st.pass { color: var(--success); }
.checklist .st.warn { color: var(--warning); }
.checklist .st.fail { color: #F87171; }
.checklist .nm { color: var(--text-bright); margin-right: 8px; }
/* at-a-glance scoreboard */
.sb-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 14px 0 8px; }
.sb-card { border: 1px solid var(--surface-2); border-radius: 4px; padding: 12px 14px; }
.sb-title { font-size: 10.5px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 9px; }
.sb-row { display: flex; align-items: baseline; gap: 9px; padding: 2px 0; }
.sb-arm { font-size: 10px; font-weight: 700; width: 12px; }
.sb-val { font-size: 15px; font-weight: 500; color: var(--text-bright); font-variant-numeric: tabular-nums; }
.sb-val.small { font-size: 12px; }
.sb-val.win { color: var(--success); }
.sb-sub { font-size: 10px; color: var(--text-muted); margin-left: 2px; }
/* head-to-head table */
.ab-table { border-collapse: collapse; font-size: 12.5px; width: 100%; }
.ab-table th { text-align: left; color: var(--text-muted); font-weight: 500; padding: 6px 10px; border-bottom: 1px solid var(--surface-2); font-size: 11px; }
.ab-table th.num, .ab-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.ab-table td { padding: 7px 10px; border-bottom: 1px solid rgba(255,255,255,0.05); color: #9AA0A8; }
.ab-table td.metric { color: #C7CBD1; white-space: nowrap; }
.ab-table tr.hl td.metric { color: var(--text-bright); font-weight: 500; }
.ab-table td.metric .note { display: block; color: var(--text-muted); font-size: 10px; white-space: normal; font-weight: 400; }
.ab-table td.med { color: #E8EAED; font-weight: 500; white-space: nowrap; }
.ab-table td.med.win { color: var(--success); }
.ab-table td.med.win .rng { color: rgba(74, 222, 128, 0.55); }
.ab-table td.delta { color: #9AA0A8; white-space: nowrap; }
.ab-table tr.section td { padding-top: 20px; color: var(--text-muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.09em; border-bottom: 1px solid var(--surface-2); }
.ab-table .rng { color: var(--text-muted); font-size: 10.5px; font-weight: 400; }
.ab-table .pts { color: var(--text-muted); font-size: 10px; white-space: nowrap; }
.ab-table td.sig { white-space: nowrap; }
.sig-badge { padding: 1px 7px; border-radius: 3px; border: 1px solid var(--surface-2); color: var(--text-muted); font-size: 10.5px; }
.sig-badge.win-a { color: var(--agent); border-color: var(--agent); }
.sig-badge.win-b { color: var(--sim); border-color: var(--sim); }
.sig-badge.differs { color: var(--warning); border-color: var(--warning); }
.roster-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: start; }
.roster-grid .rep { margin-bottom: 12px; }
.run-details { margin: 8px 0 0; font-size: 11px; }
.run-details summary { cursor: pointer; color: var(--text-muted); padding: 2px 0; }
.run-details summary:hover { color: var(--text-bright); }
.run-details pre { max-height: 420px; overflow: auto; font-size: 10.5px; line-height: 1.5; background: var(--surface-1, rgba(255,255,255,0.03)); padding: 10px; border-radius: 3px; white-space: pre-wrap; word-break: break-word; }
.badge-infra { color: #F87171; border: 1px solid #F87171; border-radius: 3px; padding: 0 6px; font-size: 10px; letter-spacing: 0.05em; }
.method-note { margin-top: 26px; font-size: 11px; color: var(--text-muted); line-height: 1.7; max-width: 920px; }
h2.ab { font-size: 13px; color: var(--text-bright); letter-spacing: 0.03em; margin: 30px 0 10px; }
"""


def _render_html(data: dict[str, Any]) -> str:
    css = _CSS_PATH.read_text(encoding="utf-8")
    case: CaseDef = data["case"]
    a: ConfigDef = data["config_a"]
    b: ConfigDef = data["config_b"]
    title = f"contremaitre · A/B · {case.case_id} · {a.name} vs {b.name}"
    body = _render_body(data)
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
{_AB_CSS}
</style>
</head>
<body>
<div class="page page-wide">
{body}
</div>
</body>
</html>
"""


def _arm_header(label: str, cls: str, config: ConfigDef, rows: list[dict[str, Any]]) -> str:
    n_healthy = sum(1 for r in rows if r["healthy"])
    n_infra = len(rows) - n_healthy
    resolved_agent = next((r["resolved_agent_model"] for r in rows if r["resolved_agent_model"]), None)
    resolved_sim = next((r["resolved_sim_model"] for r in rows if r["resolved_sim_model"]), None)
    kv = [
        ("config", f"<b><code>{_escape(config.name)}</code></b>"),
        ("agent model", f"<b>{_escape(config.agent_model)}</b>"),
        ("sim model", f"<b>{_escape(config.sim_model)}</b>"),
        ("cli reviewer", f"<b>{_escape(config.cli_reviewer)}</b>"),
        ("runs", f"<b>{n_healthy}</b> healthy" + (f" · {n_infra} infra-failed" if n_infra else "")),
    ]
    if resolved_agent:
        kv.insert(2, ("agent resolved", f"<b>{_escape(resolved_agent)}</b>"))
    if resolved_sim:
        kv.insert(4 if resolved_agent else 3, ("sim resolved", f"<b>{_escape(resolved_sim)}</b>"))
    rows_html = "".join(f"<span>{k}</span><span>{v}</span>" for k, v in kv)
    return (
        f'<div class="exp-card"><h3 class="{cls}">arm {label}</h3>'
        f'<div class="kv">{rows_html}</div></div>'
    )


def _render_checklist(checks: list[dict[str, Any]]) -> str:
    items = "".join(
        f'<li><span class="st {c["status"]}">{c["status"].upper()}</span>'
        f'<span class="nm">{_escape(c["name"])}</span>'
        f'<span>{_escape(c["detail"])}</span></li>'
        for c in checks
    )
    return f'<ul class="checklist">{items}</ul>'


def _sig_cell(rec: dict[str, Any]) -> str:
    metric: Metric = rec["metric"]
    sep = rec.get("sep")
    if metric.kind != "num":
        return '<td class="sig"></td>'
    if sep is None:
        return '<td class="sig"><span class="sig-badge">n/a</span></td>'
    if sep == "overlap":
        return '<td class="sig"><span class="sig-badge">overlap</span></td>'
    winner = rec.get("winner")
    if winner == "a":
        return '<td class="sig"><span class="sig-badge win-a">A separated ✓</span></td>'
    if winner == "b":
        return '<td class="sig"><span class="sig-badge win-b">B separated ✓</span></td>'
    # Context metric with disjoint ranges — a real difference, no attribution.
    return '<td class="sig"><span class="sig-badge differs">differs</span></td>'


def _points(values: list[Any], fmt: str) -> str:
    if not values:
        return "—"
    return " · ".join(_fmt_value(v, fmt) for v in values)


def _row_lean(rec: dict[str, Any]) -> str | None:
    """Which arm looks better on this row — PRESENTATIONAL only.

    Compares medians (num) or rates (bool) on directional metrics so the
    winner cell can be tinted. This is a reading aid for direction of
    difference; the `signal` column is the only significance claim the
    report makes (see the methodology note).
    """

    metric: Metric = rec["metric"]
    if metric.better == "context" or metric.kind == "cat":
        return None
    if metric.kind == "num":
        va, vb = rec["a"].get("median"), rec["b"].get("median")
    else:
        va, vb = rec["a"].get("rate"), rec["b"].get("rate")
    if va is None or vb is None or va == vb:
        return None
    greater = "a" if va > vb else "b"
    if metric.better == "higher":
        return greater
    return "b" if greater == "a" else "a"


def _render_metric_row(rec: dict[str, Any]) -> str:
    metric: Metric = rec["metric"]
    lean = _row_lean(rec)
    note = f'<span class="note">{_escape(metric.note)}</span>' if metric.note else ""
    label = f'<td class="metric">{_escape(metric.label)}{note}</td>'
    med_a_cls = "num med" + (" win" if lean == "a" else "")
    med_b_cls = "num med" + (" win" if lean == "b" else "")

    if metric.kind == "num":
        cells = (
            f'<td class="{med_a_cls}">{_fmt_summary(rec["a"], metric.fmt)}</td>'
            f'<td class="{med_b_cls}">{_fmt_summary(rec["b"], metric.fmt)}</td>'
            f'<td class="num delta">{_fmt_delta(rec["delta"], metric.fmt, metric.kind)}</td>'
            f"{_sig_cell(rec)}"
            f'<td class="num pts">{_points(rec["values_a"], metric.fmt)}</td>'
            f'<td class="num pts">{_points(rec["values_b"], metric.fmt)}</td>'
        )
    elif metric.kind == "bool":
        def _rate_cell(side: dict[str, Any], cls: str) -> str:
            if side["rate"] is None:
                return f'<td class="{cls}">—</td>'
            k = round(side["rate"] * side["n"])
            return (
                f'<td class="{cls}">{side["rate"] * 100:.0f}% '
                f'<span class="rng">({k}/{side["n"]})</span></td>'
            )

        def _bool_points(values: list[Any]) -> str:
            marks = ["✓" if v else "✗" for v in values if isinstance(v, bool)]
            return " ".join(marks) if marks else "—"

        cells = (
            f"{_rate_cell(rec['a'], med_a_cls)}"
            f"{_rate_cell(rec['b'], med_b_cls)}"
            f'<td class="num delta">{_fmt_delta(rec["delta"], metric.fmt, metric.kind)}</td>'
            f"{_sig_cell(rec)}"
            f'<td class="num pts">{_bool_points(rec["values_a"])}</td>'
            f'<td class="num pts">{_bool_points(rec["values_b"])}</td>'
        )
    else:  # cat
        def _mix_cell(side: dict[str, Any]) -> str:
            m = side.get("mix") or {}
            if not m:
                return '<td class="num med">—</td>'
            body = ", ".join(f"{_escape(k)}×{v}" for k, v in m.items())
            return f'<td class="num med">{body}</td>'

        cells = (
            f"{_mix_cell(rec['a'])}"
            f"{_mix_cell(rec['b'])}"
            '<td class="num delta">—</td>'
            f"{_sig_cell(rec)}"
            '<td class="num pts"></td>'
            '<td class="num pts"></td>'
        )

    return f'<tr{" class=\"hl\"" if metric.hl else ""}>{label}{cells}</tr>'


# Scoreboard rows, in display order — the ~8 questions an operator asks first.
# `__diff__` is the synthetic diff-size card (median +added −deleted · files):
# net delta is deliberately NOT a scoreboard value — a balanced +79/−78
# rewrite and a +3/−6 no-op both read "≈0 net", which buries the difference
# that matters. Net delta stays in the table, next to added/deleted.
_SCOREBOARD_KEYS = (
    ("terminal_verdict", "outcome"),
    ("cli_review_verdict", "judge verdict"),
    ("cli_review_score", "judge score"),
    ("cli_findings_weighted", "findings (weighted)"),
    ("cost_usd", "cost / run"),
    ("wall_seconds", "wall clock"),
    ("__diff__", "diff size (median)"),
    ("agent_tokens_out", "agent out-tokens"),
)


def _diff_size_card(by_key: dict[str, dict[str, Any]]) -> str:
    """The scoreboard's diff-size card: `+added −deleted · files` per arm."""

    added = by_key.get("loc_added")
    deleted = by_key.get("loc_deleted")
    files = by_key.get("files_changed")

    rows = []
    for side, arm_cls in (("a", "arm-a"), ("b", "arm-b")):
        def _med(rec: dict[str, Any] | None) -> float | None:
            return rec[side].get("median") if rec else None

        ins, dele, nf = _med(added), _med(deleted), _med(files)
        if ins is None and dele is None:
            body = '<span class="sb-val small">—</span>'
        else:
            files_bit = f'<span class="sb-sub">· {nf:.0f} files</span>' if nf is not None else ""
            body = (
                f'<span class="sb-val small"><span style="color:var(--success)">+{(ins or 0):.0f}</span> '
                f'<span style="color:#F87171">−{(dele or 0):.0f}</span></span>{files_bit}'
            )
        rows.append(
            f'<div class="sb-row"><span class="sb-arm {arm_cls}">{side.upper()}</span>{body}</div>'
        )
    return (
        '<div class="sb-card"><div class="sb-title">diff size (median)</div>'
        f'{"".join(rows)}</div>'
    )


def _render_scoreboard(data: dict[str, Any]) -> str:
    """The at-a-glance answer strip: one card per headline metric, A over B,
    the better value (directional metrics only) in green."""

    by_key = {rec["metric"].key: rec for rec in data["comparisons"]}
    cards: list[str] = []
    for key, title in _SCOREBOARD_KEYS:
        if key == "__diff__":
            cards.append(_diff_size_card(by_key))
            continue
        rec = by_key.get(key)
        if rec is None:
            continue
        metric: Metric = rec["metric"]
        lean = _row_lean(rec)

        def _side_val(side: str) -> str:
            if metric.kind == "num":
                v = rec[side].get("median")
                return _fmt_value(v, metric.fmt)
            if metric.kind == "bool":
                r = rec[side].get("rate")
                return "—" if r is None else f"{r * 100:.0f}%"
            m = rec[side].get("mix") or {}
            return ", ".join(f"{k}×{v}" for k, v in m.items()) or "—"

        rows = []
        for side, arm_cls in (("a", "arm-a"), ("b", "arm-b")):
            val = _side_val(side)
            cls = "sb-val"
            if metric.kind == "cat" or len(val) > 12:
                cls += " small"
            if lean == side:
                cls += " win"
            rows.append(
                f'<div class="sb-row"><span class="sb-arm {arm_cls}">{side.upper()}</span>'
                f'<span class="{cls}">{_escape(val)}</span></div>'
            )
        cards.append(
            f'<div class="sb-card"><div class="sb-title">{_escape(title)}</div>{"".join(rows)}</div>'
        )
    return f'<div class="sb-grid">{"".join(cards)}</div>'


def _render_metric_table(data: dict[str, Any]) -> str:
    a: ConfigDef = data["config_a"]
    b: ConfigDef = data["config_b"]
    rows: list[str] = []
    current_section = None
    for rec in data["comparisons"]:
        metric: Metric = rec["metric"]
        if metric.section != current_section:
            current_section = metric.section
            rows.append(f'<tr class="section"><td colspan="7">{_escape(current_section)}</td></tr>')
        rows.append(_render_metric_row(rec))
    return f"""
<table class="ab-table">
  <thead>
    <tr>
      <th>metric</th>
      <th class="num arm-a">A · {_escape(a.name)} <span class="rng">med [min–max]</span></th>
      <th class="num arm-b">B · {_escape(b.name)} <span class="rng">med [min–max]</span></th>
      <th class="num">Δ (B−A)</th>
      <th>signal</th>
      <th class="num arm-a">A per-run</th>
      <th class="num arm-b">B per-run</th>
    </tr>
  </thead>
  <tbody>
{"".join(rows)}
  </tbody>
</table>
"""


def _render_run_card(row: dict[str, Any]) -> str:
    report: CanaryReport = row["report"]
    verdict = report.headline.get("terminal_verdict") or "?"
    tier = _verdict_tier(verdict)
    h = report.headline

    pr_bit = ""
    if row["pr_url"]:
        pr_no = row["pr_url"].rstrip("/").rsplit("/", 1)[-1]
        title = f" · {_escape(row['pr_title'])}" if row["pr_title"] else ""
        pr_bit = (
            f'<a class="pr-link" href="{_escape(row["pr_url"])}" target="_blank" '
            f'rel="noopener"><b>PR #{_escape(pr_no)}</b>{title} ↗</a>'
        )
    elif row["pr_kind"]:
        pr_bit = f'<span class="score-pill pr-pill-no-pr"><b>{_escape(row["pr_kind"])}</b></span>'

    infra_bit = "" if row["healthy"] else ' <span class="badge-infra">INFRA — excluded from metrics</span>'

    review_badges = ""
    for cr in row["cli_reviews"]:
        cli_tier = _cli_review_tier(cr["verdict"])
        src = f' <span style="color:var(--text-muted)">({_escape(cr["source"])})</span>' if cr["source"] != "orchestrator" else ""
        review_badges += (
            f' <span class="sim-dot {cli_tier}"></span>'
            f'<span style="color:var(--accent)">{_escape(cr["tool"])} {_escape(cr["verdict"] or "?")}</span>{src}'
        )

    blurb = row["impl_complete"] or row["settled_preamble"] or row["reason"]
    blurb_html = (
        f'<div class="rep-meta" style="color:var(--text-dim)">{_escape(blurb[:280])}</div>' if blurb else ""
    )

    def _pill(label: str, value: str) -> str:
        return f'<span class="score-pill"><b>{value}</b> {label}</span>'

    pills = [
        _pill("duration", _fmt_value(h.get("wall_seconds"), "dur")),
        _pill("cost", _fmt_value(h.get("cost_usd"), "usd")),
        _pill("rounds", str(h.get("review_rounds") if h.get("review_rounds") is not None else "—")),
    ]
    # added/deleted live in the diagnostic diff_detail panel, not the headline
    # (the headline carries only files_changed + the net delta).
    diff_detail = report.diagnostic.get("diff_detail", {})
    if h.get("files_changed") is not None:
        pills.append(
            f'<span class="score-pill"><b>{h["files_changed"]}</b> files · '
            f'<span style="color:var(--success)">+{diff_detail.get("loc_added") or 0}</span> '
            f'<span style="color:#F87171">−{diff_detail.get("loc_deleted") or 0}</span></span>'
        )
    tok_out = row["extras"].get("agent_tokens_out")
    if tok_out:
        pills.append(_pill("agent out-tok", _fmt_token_count(tok_out)))

    details = []
    if row["settled_preamble"]:
        details.append(
            f'<details class="run-details"><summary>SETTLED design (preamble)</summary>'
            f"<pre>{_escape(row['settled_preamble'])}</pre></details>"
        )
    if row["diff_text"]:
        details.append(
            f'<details class="run-details"><summary>final diff</summary>'
            f"<pre>{_escape(row['diff_text'])}</pre></details>"
        )
    if row["review_md"]:
        details.append(
            f'<details class="run-details"><summary>cli_review body</summary>'
            f"<pre>{_escape(row['review_md'])}</pre></details>"
        )

    return f"""
<div class="rep rep-eval">
  <div class="rep-left">
    <div class="rep-label"><span class="{tier}">●</span> <span class="{tier}">{_escape(verdict)}</span>{infra_bit}{" · " + pr_bit if pr_bit else ""}</div>
    <div class="rep-meta"><code>{_escape(row["run_id"])}</code> · {_escape(row["when"])} ·
      <a href="{_escape(row["viewer_href"])}">full trace (viewer) →</a></div>
    <div class="rep-meta">{review_badges or '<span class="no-eval">no cli review on disk</span>'}</div>
    {blurb_html}
    <div class="rep-scores">{"".join(pills)}</div>
    {"".join(details)}
  </div>
</div>
""".strip()


def _render_body(data: dict[str, Any]) -> str:
    case: CaseDef = data["case"]
    a: ConfigDef = data["config_a"]
    b: ConfigDef = data["config_b"]
    rows_a: list[dict[str, Any]] = data["rows_a"]
    rows_b: list[dict[str, Any]] = data["rows_b"]

    repo_slug = _slug_from_git_url(case.target_url)
    repo_url = _github_url_from_slug(repo_slug)
    repo_bit = (
        f'<a href="{_escape(repo_url)}" target="_blank" rel="noopener">{_escape(repo_slug)}</a>'
        if repo_url
        else _escape(repo_slug or case.target_url)
    )

    task_card = f"""
<div class="exp-card" style="grid-column: span 2">
  <h3>pinned task</h3>
  <div class="kv">
    <span>case</span><span><b><code>{_escape(case.case_id)}</code></b></span>
    <span>target</span><span><b>{repo_bit}</b></span>
    <span>base ref</span><span><b><code>{_escape(case.base)}</code></b></span>
    <span>pinned SHA</span><span><b><code>{_escape(case.expected_base_sha or "— (unpinned)")}</code></b></span>
    <span>description</span><span>{_escape(case.description)}</span>
  </div>
</div>
"""

    n_sep = sum(1 for rec in data["comparisons"] if rec.get("sep") in ("a", "b"))
    n_num = sum(1 for rec in data["comparisons"] if rec["metric"].kind == "num")

    method_note = f"""
<div class="method-note">
<b>methodology.</b> Each arm is the latest n={data["n_requested"]} runs of its config against the pinned
task; arms were launched interleaved (A,B,A,B,…) so provider load and time-of-day drift spread across
both. Numeric cells show median [min–max] over the arm's healthy runs plus every per-run value —
no aggregate hides an outlier. Infra-failed runs ({", ".join(sorted(INFRA_VERDICTS))}) stay in the
roster but are excluded from metric vectors. <b>signal</b> is a range-separation test: an arm "separates"
only when all its values lie strictly beyond all of the other arm's (at n=3 vs n=3 that extreme ranking
has one-sided rank-sum p ≈ 0.05 — suggestive, not conclusive); anything else is honest <i>overlap</i>.
Direction (which arm "wins" a separation) is only attributed on metrics with a defensible better-direction;
scope metrics (LoC, files, rounds) render as <i>differs</i>. {n_sep} of {n_num} numeric metrics separated.
Green values (scoreboard + table) mark the arm whose median points the better way on a directional
metric — a reading aid for the direction of difference, <b>not</b> a significance claim; only the
signal column claims separation.
Metric definitions come from the eval canary's scorecard (<code>eval.check_run</code>) — the same
extraction that gates baselines, so this report and <code>eval compare</code> cannot disagree about a run.
</div>
"""

    roster = f"""
<h2 class="ab">runs · head to head</h2>
<div class="roster-grid">
  <div>
    <p class="tagline arm-a">arm A · <code>{_escape(a.name)}</code></p>
    {"".join(_render_run_card(r) for r in rows_a) or '<p class="tagline">no runs on disk</p>'}
  </div>
  <div>
    <p class="tagline arm-b">arm B · <code>{_escape(b.name)}</code></p>
    {"".join(_render_run_card(r) for r in rows_b) or '<p class="tagline">no runs on disk</p>'}
  </div>
</div>
"""

    return f"""
<div class="topbar">
  <span class="crumb">contremaitre</span>
  <span class="dim">/</span>
  <span class="crumb">eval</span>
  <span class="dim">/</span>
  <span class="crumb">A/B</span>
</div>

<h1><span class="arm-a">{_escape(a.name)}</span> vs <span class="arm-b">{_escape(b.name)}</span></h1>
<p class="tagline">head-to-head on <code>{_escape(case.case_id)}</code> ·
<a href="index.html">runs index</a></p>

<h2 class="ab">at a glance</h2>
{_render_scoreboard(data)}

<div class="exp-grid">
{task_card}
{_arm_header("A", "arm-a", a, rows_a)}
{_arm_header("B", "arm-b", b, rows_b)}
</div>

<h2 class="ab">validity</h2>
{_render_checklist(data["checks"])}

<h2 class="ab">metrics · head to head</h2>
{_render_metric_table(data)}

{roster}

{method_note}

<footer>generated by <code>contremaitre eval ab</code></footer>
"""
