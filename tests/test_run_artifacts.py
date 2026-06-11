"""Tests for the Artifact reader (`RunArtifacts`).

The interface is the test surface: these exercise the read-side over a fixture
run dir with no Textual app and no container — the path the deepening unlocked.
"""

from __future__ import annotations

import json
from pathlib import Path

from contremaitre.paths import build_run_paths
from contremaitre.run_artifacts import RunArtifacts


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def _seed_run(tmp_path: Path) -> Path:
    """A run dir with the streams the reader interprets. Returns the run dir."""

    run_dir = tmp_path / "run_abc"
    paths = build_run_paths(tmp_path, "run_abc")

    # Agent stream: a SETTLED write (ts 3500) + a step_finish carrying cost+tokens.
    _write_jsonl(
        paths.raw_export,
        [
            {
                "type": "tool_use",
                "timestamp": 3500,
                "part": {
                    "tool": "write",
                    "state": {
                        "status": "completed",
                        "input": {
                            "filePath": "/app/.contremaitre/SETTLED_DESIGN.md",
                            "content": "x",
                        },
                    },
                },
            },
            {
                "type": "step_finish",
                "part": {
                    "cost": 0.10,
                    "tokens": {"input": 100, "output": 10, "cache": {"read": 5}},
                },
            },
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
        ],
    )
    # SIM stream: its own cost + tokens (so two-stream sums are exercised).
    _write_jsonl(
        paths.sim_raw_export,
        [
            {"type": "step_finish", "part": {"cost": 0.02, "tokens": {"input": 20, "output": 4}}},
            {"type": "system", "subtype": "init", "model": "deepseek-v4-flash-free"},
        ],
    )
    _write_jsonl(
        paths.guardrail_events,
        [
            {"event": "actor_start", "role": "agent", "ts": 1000},
            {"event": "actor_start", "role": "sim", "ts": 2000},
            {"event": "actor_start", "role": "agent", "ts": 3000},
            {"event": "actor_start", "role": "review", "ts": 4000},
        ],
    )
    _write_jsonl(paths.review_cycles, [{"reviewer": "sim", "round": 1, "verdict": "APPROVED"}])
    return run_dir


def test_from_run_dir_reads_streams(tmp_path):
    run_dir = _seed_run(tmp_path)
    arts = RunArtifacts.from_run_dir(run_dir)
    assert len(arts.raw_export()) == 3
    assert len(arts.sim_raw_export()) == 2
    assert len(arts.guardrail_events()) == 4
    assert len(arts.review_cycles()) == 1


def test_missing_streams_are_empty(tmp_path):
    arts = RunArtifacts.from_run_dir(tmp_path / "nonexistent")
    assert arts.raw_export() == []
    assert arts.recoveries() == []
    assert arts.test_runs() == []
    assert arts.phases() == {
        "pre_settled_agent_turns": None,
        "pre_settled_sim_turns": None,
        "grilling_exchanges": None,
        "impl_turns": None,
        "review_rounds": 0,
    }


def test_phases_delegates_to_compute_phases(tmp_path):
    arts = RunArtifacts.from_run_dir(_seed_run(tmp_path))
    phases = arts.phases()
    assert phases["grilling_exchanges"] == 1
    assert phases["impl_turns"] == 1
    assert phases["review_rounds"] == 1


def test_cost_sums_both_streams(tmp_path):
    arts = RunArtifacts.from_run_dir(_seed_run(tmp_path))
    # 0.10 (agent) + 0.02 (sim) — two-stream is the orchestrator's cap semantics.
    assert arts.cost() == 0.12


def test_token_usage_sums_both_streams(tmp_path):
    arts = RunArtifacts.from_run_dir(_seed_run(tmp_path))
    assert arts.token_usage() == {
        "input": 120,  # 100 agent + 20 sim
        "output": 14,  # 10 agent + 4 sim
        "reasoning": 0,
        "cache_read": 5,
    }


def test_resolved_model_per_stream(tmp_path):
    arts = RunArtifacts.from_run_dir(_seed_run(tmp_path))
    assert arts.resolved_model() == "claude-sonnet-4-6"
    assert arts.resolved_model(sim=True) == "deepseek-v4-flash-free"


def test_cli_review_raw_by_tool(tmp_path):
    paths = build_run_paths(tmp_path, "run_abc")
    _write_jsonl(paths.claude_review_raw_export, [{"type": "text", "part": {"text": "hi"}}])
    arts = RunArtifacts.from_run_dir(tmp_path / "run_abc")
    assert len(arts.cli_review_raw("claude")) == 1
    assert arts.cli_review_raw("codex") == []


def test_reads_are_memoized_per_instance(tmp_path):
    run_dir = _seed_run(tmp_path)
    arts = RunArtifacts.from_run_dir(run_dir)
    first = arts.guardrail_events()
    # Mutate the file on disk; a memoized reader must not see the change
    # (snapshot semantics — a fresh instance is how the TUI re-reads).
    (run_dir / "guardrail_events.jsonl").write_text("", encoding="utf-8")
    assert arts.guardrail_events() is first
    assert RunArtifacts.from_run_dir(run_dir).guardrail_events() == []


def test_flow_use_composes_over_memoized_streams(tmp_path):
    # The reader owns reading; `flow_use` is a pure interpreter it feeds.
    arts = RunArtifacts.from_run_dir(_seed_run(tmp_path))
    fu = arts.flow_use()
    assert fu["schema"] == "flow_use v1"
    assert fu["phases"]["review_rounds"] == 1
    assert fu["sim"]["available"] is True


def test_flow_use_reads_review_cycles_once(tmp_path, monkeypatch):
    # The old path-reader read review_cycles twice per call (compute_flow_use
    # at :80 + _sim_metrics at :342). Routing through the memoized reader
    # collapses it to one read — this locks that fix.
    import contremaitre.run_artifacts as ra

    run_dir = _seed_run(tmp_path)
    counts: dict[str, int] = {}
    real = ra.read_jsonl

    def counting(path):
        counts[path.name] = counts.get(path.name, 0) + 1
        return real(path)

    monkeypatch.setattr(ra, "read_jsonl", counting)
    ra.RunArtifacts.from_run_dir(run_dir).flow_use()
    assert counts["review_cycles.jsonl"] == 1


def test_flow_use_relocates_iso_timestamp_tolerance(tmp_path):
    # The wall-time-from-ISO-`ts` assertion that lived in test_flow_use.py's
    # path-reader lands here, where a file is actually read + coerced through
    # the seam (the pure-list version still lives in test_flow_use.py).
    paths = build_run_paths(tmp_path, "run_iso")
    _write_jsonl(
        paths.raw_export,
        [
            {"type": "text", "ts": "2026-01-01T00:00:00Z", "part": {"text": "one"}},
            {"type": "text", "ts": "2026-01-01T00:00:02Z", "part": {"text": "two"}},
        ],
    )
    fu = RunArtifacts.from_run_dir(tmp_path / "run_iso").flow_use()
    assert fu["agent"]["wall_seconds_total"]["value"] == 2.0


def test_worktree_state_accessor(tmp_path):
    paths = build_run_paths(tmp_path, "run_wt")
    _write_jsonl(paths.worktree_state, [{"diff_stat": "1 file changed, 3 insertions(+)"}])
    arts = RunArtifacts.from_run_dir(tmp_path / "run_wt")
    assert arts.worktree_state() == [{"diff_stat": "1 file changed, 3 insertions(+)"}]
    # Missing stream → [] (snapshot semantics, same as the other accessors).
    assert RunArtifacts.from_run_dir(tmp_path / "run_absent").worktree_state() == []
