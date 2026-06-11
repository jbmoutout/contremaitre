"""Tests for the pipeline-observability tab in the runs index.

Covers the grounded per-pairing extraction + aggregation: the pure
extractors (`_diffstat_loc`, `_pr_review_verdict`, `_review_signals`),
the live phase split (via `_pipeline_run_metrics`, which now calls the
fixed `flow_use.compute_phases`), and the coverage-honest aggregation in
`_collect_pipeline_pairings`. Every expected number is hand-computed in
the test so a silent extraction regression is caught.
"""

from __future__ import annotations

import json
from pathlib import Path

from contremaitre.models import ModelSpec
from contremaitre.run_artifacts import RunArtifacts
from contremaitre.viewer.index import (
    _collect_pipeline_pairings,
    _diffstat_loc,
    _pipeline_run_metrics,
    _pr_review_verdict,
    _review_signals,
    _verdict_tier,
)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _opencode_step_finish(output: int) -> dict:
    return {
        "type": "step_finish",
        "part": {"tokens": {"input": 10, "output": output, "reasoning": 0, "total": 10 + output}},
    }


def _opencode_settled_write(timestamp: int) -> dict:
    return {
        "type": "tool_use",
        "timestamp": timestamp,
        "part": {
            "tool": "write",
            "state": {
                "status": "completed",
                "input": {"filePath": "/app/.contremaitre/SETTLED_DESIGN.md", "content": "x"},
            },
        },
    }


def _make_run(
    runs_root: Path,
    run_id: str,
    *,
    agent: str,
    sim: str,
    actor_mode: str = "opencode",
    verdict: str = "READY_FOR_DRAFT_PR",
    turns: int = 10,
    duration: float = 600.0,
    diff_stat: str | None = " 2 files changed, 100 insertions(+), 40 deletions(-)\n",
    review_cycles: list[dict] | None = None,
    cli_review_verdict: str | None = None,
    output_tokens: int = 300,
    settled: bool = True,
) -> None:
    """Write a synthetic run dir exercising every pipeline extractor."""

    run_dir = runs_root / run_id
    _write_json(
        run_dir / "stats.json",
        {
            "agent_model": agent,
            "sim_model": sim,
            "actor_mode": actor_mode,
            "verdict": verdict,
            "turns": turns,
            "duration_seconds": duration,
        },
    )
    if diff_stat is not None:
        _write_jsonl(
            run_dir / "worktree_state.jsonl",
            [
                {"label": "after-worktree", "diff_stat": "", "status": ""},
                {"label": "after-checks-round1", "diff_stat": diff_stat, "status": ""},
            ],
        )
    if review_cycles is not None:
        _write_jsonl(run_dir / "review_cycles.jsonl", review_cycles)

    # raw_export: tokens (always) + a SETTLED write when `settled` so the
    # live phase split has an anchor (see guardrails below).
    raw = [_opencode_step_finish(output_tokens)]
    if settled:
        raw.append(_opencode_settled_write(3500))
    _write_jsonl(run_dir / "raw_export.jsonl", raw)

    # guardrails: agent/sim turn boundaries for the phase split + an
    # optional post-publish CLI review verdict.
    guardrails: list[dict] = [
        {"event": "actor_start", "role": "agent", "ts": 1000},
        {"event": "actor_start", "role": "sim", "ts": 2000},
        {"event": "actor_start", "role": "agent", "ts": 3000},
        {"event": "actor_start", "role": "review", "ts": 4000},
    ]
    if cli_review_verdict is not None:
        guardrails.append(
            {"event": "cli_review_completed", "tool": "codex", "verdict": cli_review_verdict}
        )
    _write_jsonl(run_dir / "guardrail_events.jsonl", guardrails)


# --------------------------------------------------------------------------
# Canonical model normalization
# --------------------------------------------------------------------------


def test_canonical_model_normalizes_across_runtimes():
    # The viewer now routes persisted identity through ModelSpec.from_record,
    # which absorbs both the canonical dict and any legacy on-disk string.
    # opencode/OpenRouter slugs → final path segment, runtime "opencode".
    assert ModelSpec.from_record("openrouter/deepseek/deepseek-v4-flash").canonical() == (
        "deepseek-v4-flash",
        "opencode",
    )
    assert ModelSpec.from_record("opencode/deepseek-v4-flash-free").canonical() == (
        "deepseek-v4-flash-free",
        "opencode",
    )
    # Legacy CLI label strings → model + runtime.
    assert ModelSpec.from_record("gpt-5.5 (codex, effort=high)").canonical() == ("gpt-5.5", "codex")
    assert ModelSpec.from_record("claude default (claude, effort=high)").canonical() == (
        "claude-default",
        "claude",
    )
    # The canonical dict shape (what new runs persist).
    assert ModelSpec.from_record(
        {"runtime": "codex", "requested": "gpt-5.5", "effort": "high"}
    ).canonical() == ("gpt-5.5", "codex")


def test_collect_pairings_merges_differently_spelled_same_model(tmp_path):
    # Two runs whose agent slug differs only by provider prefix must land in
    # ONE pairing — the old split("/", 1) left a stray vendor and split them.
    runs = tmp_path / "runs"
    _make_run(runs, "20260101-000200-run", agent="openrouter/deepseek/deepseek-v4-flash", sim="z/s")
    _make_run(runs, "20260101-000201-run", agent="opencode/deepseek-v4-flash", sim="z/s")
    pairings = _collect_pipeline_pairings(runs)
    assert len(pairings) == 1
    assert pairings[0]["agent"] == "deepseek-v4-flash"
    assert pairings[0]["agent_rt"] == "opencode"
    assert pairings[0]["n"] == 2


# --------------------------------------------------------------------------
# Reader-fed extractors (take a RunArtifacts; read through the single seam)
# --------------------------------------------------------------------------


def test_diffstat_loc_parses_last_snapshot(tmp_path):
    _write_jsonl(
        tmp_path / "worktree_state.jsonl",
        [
            {"diff_stat": " 1 file changed, 5 insertions(+), 1 deletion(-)\n"},
            {"diff_stat": " 3 files changed, 80 insertions(+), 12 deletions(-)\n"},
        ],
    )
    assert _diffstat_loc(RunArtifacts.from_run_dir(tmp_path)) == (80, 12)


def test_diffstat_loc_handles_insertions_only(tmp_path):
    _write_jsonl(
        tmp_path / "worktree_state.jsonl", [{"diff_stat": " 1 file changed, 7 insertions(+)\n"}]
    )
    assert _diffstat_loc(RunArtifacts.from_run_dir(tmp_path)) == (7, 0)


def test_diffstat_loc_none_without_diffstat(tmp_path):
    _write_jsonl(tmp_path / "worktree_state.jsonl", [{"diff_stat": "", "status": "?? x\n"}])
    assert _diffstat_loc(RunArtifacts.from_run_dir(tmp_path)) is None


def test_pr_review_verdict_keeps_worst(tmp_path):
    _write_jsonl(
        tmp_path / "guardrail_events.jsonl",
        [
            {"event": "cli_review_completed", "tool": "claude", "verdict": "NEEDS_ATTENTION"},
            {"event": "cli_review_completed", "tool": "codex", "verdict": "MUST_FIX"},
            {"event": "turn", "turn": 1},
        ],
    )
    assert _pr_review_verdict(RunArtifacts.from_run_dir(tmp_path)) == "MUST_FIX"


def test_pr_review_verdict_none_when_no_review(tmp_path):
    _write_jsonl(tmp_path / "guardrail_events.jsonl", [{"event": "turn", "turn": 1}])
    assert _pr_review_verdict(RunArtifacts.from_run_dir(tmp_path)) is None


def test_review_signals_sim_rounds_and_changes(tmp_path):
    _write_jsonl(
        tmp_path / "review_cycles.jsonl",
        [
            {"reviewer": "sim", "round": 1, "verdict": "CHANGES_REQUESTED"},
            {"reviewer": "sim", "round": 2, "verdict": "APPROVED"},
        ],
    )
    sig = _review_signals(RunArtifacts.from_run_dir(tmp_path))
    assert sig["sim_rounds"] == 2
    assert sig["sim_changes"] is True  # round 1 bounced


def test_review_signals_all_none_without_cycles(tmp_path):
    sig = _review_signals(RunArtifacts.from_run_dir(tmp_path))
    assert sig["sim_rounds"] is None
    assert sig["sim_changes"] is None


# --------------------------------------------------------------------------
# Live phase split through the dashboard metric extractor
# --------------------------------------------------------------------------


def test_pipeline_run_metrics_computes_live_phases(tmp_path):
    runs = tmp_path / "runs"
    _make_run(
        runs,
        "20260101-000000-run",
        agent="prov/agentX",
        sim="prov/simY",
        review_cycles=[{"reviewer": "sim", "round": 1, "verdict": "APPROVED"}],
        settled=True,
    )
    m = _pipeline_run_metrics(runs / "20260101-000000-run")
    # SETTLED write at ts=3500 falls in the agent turn starting at 3000
    # (the 2nd agent turn) → 1 agent + 1 sim turn precede it.
    assert m["design_rounds"] == 1
    assert m["impl_turns"] == 1
    assert m["out_tokens"] == 300


def test_pipeline_run_metrics_phases_none_without_settled(tmp_path):
    runs = tmp_path / "runs"
    _make_run(runs, "20260101-000001-run", agent="prov/agentX", sim="prov/simY", settled=False)
    m = _pipeline_run_metrics(runs / "20260101-000001-run")
    assert m["design_rounds"] is None
    assert m["impl_turns"] is None


def test_pipeline_run_metrics_skips_fake_mode(tmp_path):
    runs = tmp_path / "runs"
    _make_run(runs, "20260101-000002-run", agent="prov/a", sim="prov/b", actor_mode="fake")
    assert _pipeline_run_metrics(runs / "20260101-000002-run") is None


def test_pipeline_run_metrics_marks_infra_failures(tmp_path):
    # Infra runs are kept as a lightweight marker (not None) so the
    # aggregator can count them, but flagged so they're excluded from rates.
    runs = tmp_path / "runs"
    _make_run(runs, "20260101-000003-run", agent="prov/a", sim="prov/b", verdict="FAILED_INFRA")
    _make_run(runs, "20260101-000004-run", agent="prov/a", sim="prov/b", verdict="QUOTA_EXHAUSTED")
    for rid in ("20260101-000003-run", "20260101-000004-run"):
        m = _pipeline_run_metrics(runs / rid)
        assert m["infra"] is True
        assert "turns" not in m  # metric extraction skipped


def test_pr_needs_human_is_published_warning_for_index_metrics(tmp_path):
    runs = tmp_path / "runs"
    _make_run(runs, "20260101-000005-run", agent="prov/a", sim="prov/b", verdict="PR_NEEDS_HUMAN")

    metrics = _pipeline_run_metrics(runs / "20260101-000005-run")

    assert _verdict_tier("PR_NEEDS_HUMAN") == "tier-yellow"
    assert metrics["infra"] is False
    assert metrics["lands_pr"] is True


def test_collect_pairings_excludes_infra_from_rates(tmp_path):
    # An infra failure must not count toward the metrics or drag PR-land:
    # 2 real runs (1 lands) + 1 infra → n=2, pr_land=0.5, infra_n=1.
    runs = tmp_path / "runs"
    _make_run(runs, "20260101-000300-run", agent="p/a", sim="p/b", verdict="READY_FOR_DRAFT_PR")
    _make_run(
        runs, "20260101-000301-run", agent="p/a", sim="p/b", verdict="NO_PR_CHANGES_REQUESTED"
    )
    _make_run(runs, "20260101-000302-run", agent="p/a", sim="p/b", verdict="FAILED_INFRA")
    pairings = _collect_pipeline_pairings(runs)
    assert len(pairings) == 1
    assert pairings[0]["n"] == 2
    assert pairings[0]["pr_land"] == 0.5
    assert pairings[0]["infra_n"] == 1


def test_infra_only_pairing_is_dropped_but_surfaced(tmp_path):
    from contremaitre.viewer.index import _infra_only_pairings

    # A pairing whose only runs are infra failures must NOT appear in the
    # main table but MUST surface in the infra-only footnote list.
    runs = tmp_path / "runs"
    _make_run(runs, "20260101-000400-run", agent="x/gpt-5.4", sim="x/dsv4", verdict="FAILED_INFRA")
    _make_run(runs, "20260101-000401-run", agent="x/gpt-5.4", sim="x/dsv4", verdict="FAILED_INFRA")
    assert _collect_pipeline_pairings(runs) == []
    infra_only = _infra_only_pairings(runs)
    assert len(infra_only) == 1
    assert infra_only[0]["agent"] == "gpt-5.4"
    assert infra_only[0]["infra_n"] == 2


# --------------------------------------------------------------------------
# Aggregation + coverage honesty
# --------------------------------------------------------------------------


def test_collect_pairings_aggregates_with_coverage(tmp_path):
    runs = tmp_path / "runs"
    # Run A: lands a PR, sim bounced then approved, codex MUST_FIX, settled.
    _make_run(
        runs,
        "20260101-000100-run",
        agent="prov/agentX",
        sim="prov/simY",
        verdict="READY_FOR_DRAFT_PR",
        turns=10,
        duration=600.0,
        diff_stat=" 2 files changed, 100 insertions(+), 40 deletions(-)\n",
        review_cycles=[
            {"reviewer": "sim", "round": 1, "verdict": "CHANGES_REQUESTED"},
            {"reviewer": "sim", "round": 2, "verdict": "APPROVED"},
        ],
        cli_review_verdict="MUST_FIX",
        output_tokens=300,
        settled=True,
    )
    # Run B: no PR, sim approved first try, extra bounced, NO cli review,
    # NO settled write (phases unrecoverable).
    _make_run(
        runs,
        "20260101-000101-run",
        agent="prov/agentX",
        sim="prov/simY",
        verdict="NO_PR_CHANGES_REQUESTED",
        turns=6,
        duration=300.0,
        diff_stat=" 1 file changed, 20 insertions(+)\n",
        review_cycles=[
            {"reviewer": "sim", "round": 1, "verdict": "APPROVED"},
        ],
        cli_review_verdict=None,
        output_tokens=100,
        settled=False,
    )
    # A lone single-run pairing must be dropped (< _PAIRING_MIN_RUNS).
    _make_run(runs, "20260101-000102-run", agent="prov/solo", sim="prov/solo")

    pairings = _collect_pipeline_pairings(runs)
    assert [(p["agent"], p["sim"]) for p in pairings] == [("agentX", "simY")]
    p = pairings[0]

    assert p["n"] == 2
    assert p["pr_land"] == 0.5  # 1 of 2 landed
    assert p["turns"] == (8.0, 2)
    assert p["duration"] == (450.0, 2)
    assert p["ins"] == (60.0, 2)  # (100 + 20) / 2
    assert p["dele"] == (20.0, 2)  # (40 + 0) / 2
    assert p["out_tokens"] == (200.0, 2)  # (300 + 100) / 2
    assert p["sim_rounds"] == (1.5, 2)  # (2 + 1) / 2
    assert p["sim_changes"] == (0.5, 2)  # A bounced, B did not
    # cli review only in A → coverage 1, MUST_FIX → fail rate 1.0
    assert p["pr_fail"] == (1.0, 1)
    # phases only recoverable in A → coverage 1
    assert p["design"] == (1.0, 1)
    assert p["impl"] == (1.0, 1)
    # tokens-per-line: out_tokens 200 / net LoC (60 + 20) = 2.5
    assert p["tok_per_loc"] == 2.5
