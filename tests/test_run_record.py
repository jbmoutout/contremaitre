"""Unit tests for the Run record — the typed read-side view of run artifacts.

The interface is the test surface: these exercise `RunStats` / `ReviewCycle` /
`sim_cycles` / `RunRecord.load` directly, plus a writer/reader round-trip that
nothing checked before this module existed.
"""

from __future__ import annotations

import json

from contremaitre.models import ActorMode, ModelSpec
from contremaitre.run_record import (
    ReviewCycle,
    RunRecord,
    RunStats,
    parse_review_cycles,
    sim_cycles,
)

# --------------------------------------------------------------------------
# RunStats
# --------------------------------------------------------------------------


def test_run_stats_reads_scalars():
    rec = RunStats.from_record(
        {
            "run_id": "r1",
            "verdict": "READY_FOR_DRAFT_PR",
            "terminal_state": "APPROVED",
            "actor_mode": "opencode",
            "turns": 7,
            "duration_seconds": 12.5,
            "recorded_cost_usd": 0.34,
            "reason": "  done  ",
        }
    )
    assert rec.run_id == "r1"
    assert rec.verdict == "READY_FOR_DRAFT_PR"
    assert rec.terminal_state == "APPROVED"
    assert rec.actor_mode == "opencode"
    assert rec.turns == 7
    assert rec.duration_seconds == 12.5
    assert rec.recorded_cost_usd == 0.34
    assert rec.reason == "done"  # stripped


def test_run_stats_run_id_falls_back_to_arg():
    rec = RunStats.from_record({}, run_id="dir-name")
    assert rec.run_id == "dir-name"


def test_run_stats_tolerates_non_dict_and_missing():
    rec = RunStats.from_record(None)
    assert rec.run_id == ""
    assert rec.verdict is None
    assert rec.reason == ""
    assert rec.agent_spec is None
    assert rec.sim_spec is None
    assert rec.agent_canonical() == (None, None)
    assert rec.sim_canonical() == (None, None)


def test_run_stats_parses_model_specs_when_present():
    rec = RunStats.from_record(
        {
            "agent_model": {"runtime": "codex", "requested": "gpt-5.5", "effort": "high"},
            "sim_model": "openrouter/deepseek/deepseek-v4-flash",
        }
    )
    assert isinstance(rec.agent_spec, ModelSpec)
    assert rec.agent_canonical() == ("gpt-5.5", "codex")
    assert rec.sim_canonical() == ("deepseek-v4-flash", "opencode")


def test_run_stats_absent_model_is_none_not_unknown_spec():
    # The viewer's "absent model → skip this run" guard depends on None here,
    # NOT a "?" placeholder spec.
    rec = RunStats.from_record({"verdict": "X"})
    assert rec.agent_spec is None
    assert rec.agent_canonical() == (None, None)


# --------------------------------------------------------------------------
# ReviewCycle + folds
# --------------------------------------------------------------------------


def test_review_cycle_defaults():
    c = ReviewCycle.from_row({})
    assert c.round == 0
    assert c.reviewer == "sim"
    assert c.is_sim is True
    assert c.unavailable is False
    assert c.verdict is None
    assert c.summary == ""
    assert c.checks_performed == ()


def test_review_cycle_reads_fields():
    c = ReviewCycle.from_row(
        {
            "round": 2,
            "reviewer": "sim",
            "verdict": "APPROVED",
            "summary": "looks good",
            "checks_performed": ["ran tests", "read diff"],
        }
    )
    assert c.round == 2
    assert c.verdict == "APPROVED"
    assert c.summary == "looks good"
    assert c.checks_performed == ("ran tests", "read diff")


def test_review_cycle_missing_reviewer_is_sim():
    # The chosen tolerant default — `or "sim"`.
    assert ReviewCycle.from_row({"round": 1}).is_sim is True
    assert ReviewCycle.from_row({"reviewer": ""}).is_sim is True


def test_sim_cycles_excludes_unavailable_and_non_sim():
    cycles = parse_review_cycles(
        [
            {"round": 1, "reviewer": "sim", "verdict": "APPROVED"},
            {"round": 2, "reviewer": "sim", "unavailable": True},
            {"round": 3, "reviewer": "cli", "verdict": "MUST_FIX"},
            {"round": 4, "verdict": "CHANGES_REQUESTED"},  # absent reviewer → sim
        ]
    )
    kept = sim_cycles(cycles)
    assert [c.round for c in kept] == [1, 4]


# --------------------------------------------------------------------------
# RunRecord façade
# --------------------------------------------------------------------------


def test_run_record_load(tmp_path):
    (tmp_path / "stats.json").write_text(
        json.dumps({"verdict": "READY_FOR_DRAFT_PR", "turns": 3}), encoding="utf-8"
    )
    (tmp_path / "review_cycles.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"round": 1, "reviewer": "sim", "verdict": "CHANGES_REQUESTED"},
                {"round": 2, "reviewer": "sim", "verdict": "APPROVED"},
            ]
        ),
        encoding="utf-8",
    )
    rec = RunRecord.load(tmp_path)
    assert rec.stats.verdict == "READY_FOR_DRAFT_PR"
    assert rec.stats.turns == 3
    assert rec.stats.run_id == tmp_path.name  # run_id defaults to dir name
    assert [c.round for c in rec.review_cycles] == [1, 2]
    assert [c.verdict for c in sim_cycles(rec.review_cycles)] == [
        "CHANGES_REQUESTED",
        "APPROVED",
    ]


def test_run_record_load_missing_files(tmp_path):
    rec = RunRecord.load(tmp_path)
    assert rec.stats.run_id == tmp_path.name
    assert rec.stats.verdict is None
    assert rec.review_cycles == []


# --------------------------------------------------------------------------
# Writer/reader round-trip — the contract test that was impossible before
# --------------------------------------------------------------------------


def test_round_trip_model_spec_through_stats():
    # The orchestrator persists `ModelSpec.to_dict()` under agent_model/sim_model;
    # RunStats.from_record must read it back to an equivalent canonical identity.
    spec = ModelSpec.build(
        mode=ActorMode.CLI, opencode_model="x", codex_model="gpt-5.5", codex_effort="high"
    )
    written = {"agent_model": spec.to_dict(), "sim_model": spec.to_dict()}
    rec = RunStats.from_record(written)
    assert rec.agent_canonical() == spec.canonical()
    assert rec.sim_canonical() == spec.canonical()
