"""Tests for tui.py pure helper functions.

All helpers tested here work on plain dicts (JSONL-deserialized events) and
do not require textual to be installed. They form the data-contract between
the JSONL files on disk and the TUI rendering layer.

The fixtures deliberately construct events using `events.<CONSTANT>` rather
than bare strings so that a rename of a constant (e.g. after PR #3's typed
event migration) breaks these tests instead of silently degrading the TUI.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from contremaitre import events
from contremaitre.jsonlog import read_jsonl
from contremaitre.tui import (
    _activity_state,
    _architecture_review_in,
    _build_event_row,
    _current_phase_label,
    _current_review_round,
    _derive_phase,
    _fmt_elapsed,
    _impl_complete_in,
    _is_free_model,
    _latest_pending_tool,
    _persistent_review_token,
    _phase_trail,
    _pr_number_from_url,
    _render_guardrail,
    _reviewer_glyph,
    _reviewer_status,
    _round_verdicts,
    _self_verified_in,
    _settled_in,
    _short_repo,
    _task_count,
    _terminal_badge,
    _text_event_count,
    _verdict_glyph,
    _warnings_token,
)


# ---------- helpers ----------


def _g(kind: str, **fields) -> dict:
    """Build a minimal guardrail event dict."""
    return {"ts": "2026-01-01T00:00:00.000Z", "event": kind, **fields}


def _actor_start(role: str) -> dict:
    return _g(events.OPENCODE_ACTOR_START, role=role)


# ===== _read_jsonl =====


def test_read_jsonl_missing_file(tmp_path):
    assert read_jsonl(tmp_path / "nonexistent.jsonl") == []


def test_read_jsonl_empty_file(tmp_path):
    (tmp_path / "f.jsonl").write_text("")
    assert read_jsonl(tmp_path / "f.jsonl") == []


def test_read_jsonl_skips_malformed_lines(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
    result = read_jsonl(p)
    assert result == [{"a": 1}, {"b": 2}]


def test_read_jsonl_skips_non_dict_values(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\n[1, 2]\n42\n')
    result = read_jsonl(p)
    assert result == [{"a": 1}]


# ===== _fmt_elapsed =====


def test_fmt_elapsed_none():
    assert _fmt_elapsed(None) == "—"


def test_fmt_elapsed_seconds():
    assert _fmt_elapsed(45) == "45s"


def test_fmt_elapsed_minutes():
    assert _fmt_elapsed(90) == "1m30s"


def test_fmt_elapsed_hours():
    assert _fmt_elapsed(3700) == "1h01m"


# ===== _activity_state =====


def test_activity_state_no_container_is_idle():
    assert _activity_state(container_present=False, file_age=0.5) == "idle"


def test_activity_state_container_recent_write_is_active():
    assert _activity_state(container_present=True, file_age=1.0) == "active"


def test_activity_state_container_stale_write_is_thinking():
    assert _activity_state(container_present=True, file_age=5.0) == "thinking"


def test_activity_state_container_no_file_is_thinking():
    assert _activity_state(container_present=True, file_age=None) == "thinking"


# ===== _is_free_model =====


def test_is_free_model_empty():
    assert not _is_free_model("")


def test_is_free_model_free_suffix():
    assert _is_free_model("opencode/claude-3-5-sonnet-free")


def test_is_free_model_big_pickle():
    assert _is_free_model("opencode/big-pickle")


def test_is_free_model_openrouter_free():
    assert _is_free_model("openrouter/google/gemini-flash:free")


def test_is_free_model_paid():
    assert not _is_free_model("openrouter/anthropic/claude-3-5-sonnet")


# ===== _derive_phase =====
# Verifies the 6-phase pipeline derivation: init → exploring → grilling →
# implementing → reviewing → done. Exploring → Grilling fires on EITHER
# architecture-review.html being written OR the SIM joining the conversation.
# color_state reflects live / ok / warn / error.


def test_derive_phase_init_when_nothing_started():
    phase, color = _derive_phase(
        terminal=False, terminal_verdict=None, settled=False, impl_complete=False, agent_started=False
    )
    assert phase == "init"
    assert color == "live"


def test_derive_phase_exploring_after_agent_start_pre_cards():
    # Agent started but no architecture-review.html yet and SIM hasn't joined —
    # this is the pre-cards reading-the-codebase phase.
    phase, color = _derive_phase(
        terminal=False,
        terminal_verdict=None,
        settled=False,
        impl_complete=False,
        agent_started=True,
        architecture_review_done=False,
        sim_started=False,
    )
    assert phase == "exploring"
    assert color == "live"


def test_derive_phase_grilling_after_architecture_review_written():
    phase, _ = _derive_phase(
        terminal=False,
        terminal_verdict=None,
        settled=False,
        impl_complete=False,
        agent_started=True,
        architecture_review_done=True,
        sim_started=False,
    )
    assert phase == "grilling"


def test_derive_phase_grilling_when_sim_joins_without_cards():
    # Fallback: agent skipped writing the cards file, but SIM has joined.
    # We still advance to grilling — SIM joining is the more robust signal.
    phase, _ = _derive_phase(
        terminal=False,
        terminal_verdict=None,
        settled=False,
        impl_complete=False,
        agent_started=True,
        architecture_review_done=False,
        sim_started=True,
    )
    assert phase == "grilling"


def test_derive_phase_implementing_after_settled():
    phase, _ = _derive_phase(
        terminal=False,
        terminal_verdict=None,
        settled=True,
        impl_complete=False,
        agent_started=True,
        architecture_review_done=True,
        sim_started=True,
    )
    assert phase == "implementing"


def test_derive_phase_reviewing_only_after_review_actor_start():
    # Gating Implementing → Reviewing on review_started (NOT impl_complete)
    # so the brief window where the agent has written IMPLEMENTATION_COMPLETE
    # but the review container hasn't started yet stays in implementing.
    phase, _ = _derive_phase(
        terminal=False,
        terminal_verdict=None,
        settled=True,
        impl_complete=True,
        agent_started=True,
        architecture_review_done=True,
        sim_started=True,
        review_started=True,
    )
    assert phase == "reviewing"


def test_derive_phase_stays_in_implementing_after_impl_complete_pre_review():
    # IMPL_COMPLETE marker written but `role=review` actor hasn't started.
    # Trail stays at implementing; the label surfaces "awaiting review".
    phase, _ = _derive_phase(
        terminal=False,
        terminal_verdict=None,
        settled=True,
        impl_complete=True,
        agent_started=True,
        architecture_review_done=True,
        sim_started=True,
        review_started=False,
    )
    assert phase == "implementing"


def test_derive_phase_done_ok_for_ready_for_draft_pr():
    phase, color = _derive_phase(
        terminal=True,
        terminal_verdict="READY_FOR_DRAFT_PR",
        settled=True,
        impl_complete=True,
        agent_started=True,
    )
    assert phase == "done"
    assert color == "ok"


def test_derive_phase_done_warn_for_no_pr_variants():
    for verdict in ("NO_PR_CHANGES_REQUESTED", "NO_PR_NEEDS_HUMAN"):
        phase, color = _derive_phase(
            terminal=True,
            terminal_verdict=verdict,
            settled=True,
            impl_complete=True,
            agent_started=True,
        )
        assert phase == "done", verdict
        assert color == "warn", verdict


def test_derive_phase_failed_freezes_at_implementing():
    # FAILED_INFRA during implementing — trail should stay at implementing
    # so the operator sees where the run died, not advance to "done".
    phase, color = _derive_phase(
        terminal=True,
        terminal_verdict="FAILED_INFRA",
        settled=True,
        impl_complete=False,
        agent_started=True,
        architecture_review_done=True,
        sim_started=True,
    )
    assert phase == "implementing"
    assert color == "error"


def test_derive_phase_failed_freezes_at_reviewing_when_review_started():
    phase, color = _derive_phase(
        terminal=True,
        terminal_verdict="FAILED_INFRA",
        settled=True,
        impl_complete=True,
        agent_started=True,
        architecture_review_done=True,
        sim_started=True,
        review_started=True,
    )
    assert phase == "reviewing"
    assert color == "error"


def test_derive_phase_failed_freezes_at_exploring():
    # Run died very early — before cards were written and before SIM joined.
    phase, color = _derive_phase(
        terminal=True,
        terminal_verdict="FAILED_INFRA",
        settled=False,
        impl_complete=False,
        agent_started=True,
        architecture_review_done=False,
        sim_started=False,
    )
    assert phase == "exploring"
    assert color == "error"


# ===== _phase_trail =====


def test_phase_trail_has_six_dots():
    text = _phase_trail("implementing", "live")
    dots = [c for c in text.plain if c in "●○"]
    assert len(dots) == 6


def test_phase_trail_marks_past_dots_filled():
    # implementing (index 3) means init + exploring + grilling are past.
    text = _phase_trail("implementing", "live")
    assert text.plain.startswith("●─●─●─")


def test_phase_trail_no_half_glyph_in_live_state():
    # Half-fill `◐` removed — most fonts render it at a different baseline
    # than `●`, which makes the trail look misaligned. Current dot is
    # distinguished by colour + bold, not by a different glyph.
    text = _phase_trail("grilling", "live")
    assert "◐" not in text.plain


def test_phase_trail_all_filled_at_terminal_done():
    text = _phase_trail("done", "ok")
    assert text.plain.count("●") == 6


# ===== _verdict_glyph =====


def test_verdict_glyph_approved():
    g, _ = _verdict_glyph("APPROVED")
    assert g == "✓"


def test_verdict_glyph_changes_requested():
    g, _ = _verdict_glyph("CHANGES_REQUESTED")
    assert g == "✗"


def test_verdict_glyph_needs_human():
    g, _ = _verdict_glyph("NEEDS_HUMAN")
    assert g == "?"


def test_verdict_glyph_unknown_or_none():
    g, _ = _verdict_glyph(None)
    assert g == "·"


# ===== _round_verdicts =====


def test_round_verdicts_returns_none_for_missing_round():
    sim, extra = _round_verdicts([{"round": 1, "verdict": "APPROVED"}], 2)
    assert sim is None
    assert extra is None


def test_round_verdicts_picks_sim_only():
    cycles = [{"round": 1, "verdict": "APPROVED"}]  # default reviewer = sim
    sim, extra = _round_verdicts(cycles, 1)
    assert sim == "APPROVED"
    assert extra is None


def test_round_verdicts_picks_both_reviewers():
    cycles = [
        {"round": 1, "verdict": "APPROVED", "reviewer": "sim"},
        {"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "extra"},
    ]
    sim, extra = _round_verdicts(cycles, 1)
    assert sim == "APPROVED"
    assert extra == "CHANGES_REQUESTED"


def test_round_verdicts_skips_unavailable_entries():
    cycles = [
        {"round": 1, "verdict": "APPROVED", "reviewer": "sim", "unavailable": True},
    ]
    sim, extra = _round_verdicts(cycles, 1)
    assert sim is None


# ===== _current_review_round =====


def test_current_review_round_zero_when_no_starts():
    assert _current_review_round([]) == 0


def test_current_review_round_counts_only_sim_review_starts():
    # Extra-reviewer actor_starts should NOT bump the round count —
    # they're the second reviewer within an already-opened round.
    guardrails = [
        _g(events.OPENCODE_ACTOR_START, role="agent"),
        _g(events.OPENCODE_ACTOR_START, role="sim"),
        _g(events.OPENCODE_ACTOR_START, role="review"),
        _g(events.OPENCODE_ACTOR_START, role="review", reviewer_id="extra"),
    ]
    assert _current_review_round(guardrails) == 1


def test_current_review_round_advances_per_loop_back():
    guardrails = [
        _g(events.OPENCODE_ACTOR_START, role="review"),
        _g(events.OPENCODE_ACTOR_START, role="review", reviewer_id="extra"),
        _g(events.OPENCODE_ACTOR_START, role="agent"),
        _g(events.OPENCODE_ACTOR_START, role="review"),
        _g(events.OPENCODE_ACTOR_START, role="review", reviewer_id="extra"),
        _g(events.OPENCODE_ACTOR_START, role="agent"),
        _g(events.OPENCODE_ACTOR_START, role="review"),  # round 3 just opened
    ]
    assert _current_review_round(guardrails) == 3


# ===== _reviewer_status =====


def test_reviewer_status_idle_when_no_start():
    assert (
        _reviewer_status(
            round_n=1, review_cycles=[], guardrails=[], is_extra=False
        )
        == "idle"
    )


def test_reviewer_status_streaming_when_started_no_verdict():
    guardrails = [_g(events.OPENCODE_ACTOR_START, role="review")]
    assert (
        _reviewer_status(
            round_n=1, review_cycles=[], guardrails=guardrails, is_extra=False
        )
        == "streaming"
    )


def test_reviewer_status_approved():
    cycles = [{"round": 1, "verdict": "APPROVED", "reviewer": "sim"}]
    guardrails = [_g(events.OPENCODE_ACTOR_START, role="review")]
    assert (
        _reviewer_status(round_n=1, review_cycles=cycles, guardrails=guardrails, is_extra=False)
        == "approved"
    )


def test_reviewer_status_changes_req():
    cycles = [{"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "sim"}]
    assert (
        _reviewer_status(round_n=1, review_cycles=cycles, guardrails=[], is_extra=False)
        == "changes_req"
    )


def test_reviewer_status_needs_human():
    cycles = [{"round": 1, "verdict": "NEEDS_HUMAN", "reviewer": "sim"}]
    assert (
        _reviewer_status(round_n=1, review_cycles=cycles, guardrails=[], is_extra=False)
        == "needs_human"
    )


def test_reviewer_status_unavailable():
    cycles = [{"round": 1, "reviewer": "sim", "unavailable": True}]
    assert (
        _reviewer_status(round_n=1, review_cycles=cycles, guardrails=[], is_extra=False)
        == "unavailable"
    )


def test_reviewer_status_extra_isolated_from_sim():
    # SIM has a verdict; extra hasn't started yet for this round.
    cycles = [{"round": 1, "verdict": "APPROVED", "reviewer": "sim"}]
    guardrails = [_g(events.OPENCODE_ACTOR_START, role="review")]
    assert (
        _reviewer_status(round_n=1, review_cycles=cycles, guardrails=guardrails, is_extra=True)
        == "idle"
    )


def test_reviewer_status_per_round_independence():
    # Round 1 fully done (both verdicts present), round 2 SIM streaming.
    cycles = [
        {"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "sim"},
        {"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "extra"},
    ]
    guardrails = [
        _g(events.OPENCODE_ACTOR_START, role="review"),  # round 1 sim
        _g(events.OPENCODE_ACTOR_START, role="review", reviewer_id="extra"),  # round 1 extra
        _g(events.OPENCODE_ACTOR_START, role="agent"),  # work loop
        _g(events.OPENCODE_ACTOR_START, role="review"),  # round 2 sim — streaming
    ]
    assert (
        _reviewer_status(round_n=2, review_cycles=cycles, guardrails=guardrails, is_extra=False)
        == "streaming"
    )
    assert (
        _reviewer_status(round_n=2, review_cycles=cycles, guardrails=guardrails, is_extra=True)
        == "idle"
    )
    # Round 1 verdicts are still recoverable for the persistent token /
    # warnings logic to inspect prior rounds.
    assert (
        _reviewer_status(round_n=1, review_cycles=cycles, guardrails=guardrails, is_extra=False)
        == "changes_req"
    )


# ===== _reviewer_glyph =====


def test_reviewer_glyph_streaming():
    g, _ = _reviewer_glyph("streaming")
    assert g == "⏵"


def test_reviewer_glyph_approved():
    g, _ = _reviewer_glyph("approved")
    assert g == "✓"


def test_reviewer_glyph_changes_req():
    g, _ = _reviewer_glyph("changes_req")
    assert g == "✗"


def test_reviewer_glyph_idle_is_dot():
    g, _ = _reviewer_glyph("idle")
    assert g == "·"


# ===== _current_phase_label =====


def _default_label_kwargs(**overrides):
    base = dict(
        phase="grilling",
        color_state="live",
        grilling_exchanges=0,
        impl_turns=0,
        self_verified=False,
        impl_complete=False,
        review_cycles=[],
        terminal_verdict=None,
        pr_number=None,
        current_review_round=0,
        sim_review_statuses=[],
        extra_review_statuses=[],
        extra_enabled=False,
    )
    base.update(overrides)
    return base


def test_phase_label_init():
    text = _current_phase_label(**_default_label_kwargs(phase="init"))
    assert "Init" in text.plain


def test_phase_label_exploring():
    text = _current_phase_label(**_default_label_kwargs(phase="exploring"))
    assert "Exploring" in text.plain


def test_phase_label_exploring_error_state_shows_infra_failure():
    text = _current_phase_label(
        **_default_label_kwargs(phase="exploring", color_state="error")
    )
    assert "infra failure" in text.plain


def test_phase_label_grilling_shows_exchange_count():
    text = _current_phase_label(**_default_label_kwargs(phase="grilling", grilling_exchanges=3))
    assert "Grilling" in text.plain
    assert "3" in text.plain


def test_phase_label_implementing_shows_turn_and_tested():
    text = _current_phase_label(
        **_default_label_kwargs(phase="implementing", impl_turns=5, self_verified=True)
    )
    assert "Implementing" in text.plain
    assert "5" in text.plain
    assert "tested" in text.plain
    assert "✓" in text.plain


def test_phase_label_implementing_no_tested_when_not_self_verified():
    text = _current_phase_label(
        **_default_label_kwargs(phase="implementing", impl_turns=5, self_verified=False)
    )
    assert "tested" not in text.plain


def test_phase_label_reviewing_shows_round_from_actor_count():
    # Round number now derives from `current_review_round` (actor-start
    # count), NOT from review_cycles — so it appears immediately when the
    # round opens, not only after the first verdict lands.
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="reviewing",
            current_review_round=1,
            sim_review_statuses=["streaming"],
        )
    )
    assert "Reviewing" in text.plain
    assert "round 1" in text.plain
    assert "Review" in text.plain
    assert "⏵" in text.plain  # SIM-as-reviewer is streaming


def test_phase_label_reviewing_shows_sim_verdict():
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="reviewing",
            current_review_round=1,
            sim_review_statuses=["approved"],
        )
    )
    assert "Review " in text.plain
    assert "✓" in text.plain
    assert "Extra Review" not in text.plain  # extra disabled, slot hidden


def test_phase_label_reviewing_extra_slot_hidden_when_idle():
    # extra_enabled=True but extra hasn't started this round — slot stays
    # hidden until the extra actor's first event, so the label grows
    # organically rather than showing a placeholder dot.
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="reviewing",
            current_review_round=1,
            sim_review_statuses=["streaming"],
            extra_review_statuses=["idle"],
            extra_enabled=True,
        )
    )
    assert "Extra Review" not in text.plain


def test_phase_label_reviewing_extra_slot_shown_when_streaming():
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="reviewing",
            current_review_round=1,
            sim_review_statuses=["approved"],
            extra_review_statuses=["streaming"],
            extra_enabled=True,
        )
    )
    assert "Review " in text.plain
    assert "Extra Review" in text.plain
    # Both glyphs present: ✓ for sim, ⏵ for extra streaming
    assert "✓" in text.plain
    assert "⏵" in text.plain


def test_phase_label_reviewing_multi_round():
    # Round 2 in flight — label shows round 2, not 1, even though
    # review_cycles still has round-1 verdicts recorded. Past rounds
    # stack their glyphs after "Review" / "Extra Review" so the
    # operator sees the full reviewing history without scrolling logs.
    cycles = [
        {"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "sim"},
        {"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "extra"},
    ]
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="reviewing",
            review_cycles=cycles,
            current_review_round=2,
            sim_review_statuses=["changes_req", "streaming"],
            extra_review_statuses=["changes_req", "streaming"],
            extra_enabled=True,
        )
    )
    assert "round 2" in text.plain
    assert "round 1" not in text.plain
    # Two glyphs per reviewer slot: ✗ (round 1) then ⏵ (round 2).
    assert text.plain.count("✗") == 2
    assert text.plain.count("⏵") == 2


def test_phase_label_done_pr_pushed_without_title_is_just_done():
    # No title in pr.json → label stays just `Done`. PR # is carried by
    # the verdict zone; putting it in the label too would be noise.
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="done", terminal_verdict="READY_FOR_DRAFT_PR", pr_number=1234
        )
    )
    assert text.plain.strip() == "Done"
    assert "1234" not in text.plain


def test_phase_label_done_pr_pushed_with_title_appended():
    # With a title in pr.json: label reads `Done · <title>` for context.
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="done",
            terminal_verdict="READY_FOR_DRAFT_PR",
            pr_number=1234,
            pr_title="fix: refactor user auth",
        )
    )
    assert "Done" in text.plain
    assert "fix: refactor user auth" in text.plain
    assert "1234" not in text.plain  # number still belongs to verdict zone


def test_phase_label_done_truncates_long_pr_title():
    long_title = "refactor: extract authentication subsystem into its own bounded context with explicit boundaries"
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="done",
            terminal_verdict="READY_FOR_DRAFT_PR",
            pr_title=long_title,
        )
    )
    # Truncated → ends with ellipsis, doesn't carry the whole title.
    assert "…" in text.plain
    assert long_title not in text.plain


def test_phase_label_implementing_awaiting_review():
    # IMPL_COMPLETE written but review actor hasn't started — surface the wait.
    text = _current_phase_label(
        **_default_label_kwargs(phase="implementing", impl_complete=True, impl_turns=5)
    )
    assert "Implementing" in text.plain
    assert "awaiting review" in text.plain


def test_phase_label_done_no_pr_changes_req():
    text = _current_phase_label(
        **_default_label_kwargs(phase="done", terminal_verdict="NO_PR_CHANGES_REQUESTED")
    )
    assert "exhausted" in text.plain


def test_phase_label_done_failed_infra():
    text = _current_phase_label(
        **_default_label_kwargs(phase="done", terminal_verdict="FAILED_INFRA")
    )
    assert "Failed" in text.plain


def test_phase_label_implementing_error_state_shows_infra_failure():
    # FAILED_INFRA frozen at implementing: label must say so, not just "Implementing".
    text = _current_phase_label(
        **_default_label_kwargs(phase="implementing", color_state="error")
    )
    assert "infra failure" in text.plain


# ===== _persistent_review_token =====


def test_persistent_review_token_none_when_no_cycles():
    assert _persistent_review_token(phase="implementing", review_cycles=[]) is None


def test_persistent_review_token_none_when_in_reviewing_phase():
    cycles = [{"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "sim"}]
    assert _persistent_review_token(phase="reviewing", review_cycles=cycles) is None


def test_persistent_review_token_shown_after_changes_requested_loopback():
    cycles = [{"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "sim"}]
    token = _persistent_review_token(phase="implementing", review_cycles=cycles)
    assert token is not None
    assert "R1" in token.plain
    assert "changes_req" in token.plain


def test_persistent_review_token_none_when_last_round_approved():
    # Approved → loop ends; token shouldn't show even if we're transiently
    # back in implementing-like state (shouldn't normally happen but safe).
    cycles = [{"round": 1, "verdict": "APPROVED", "reviewer": "sim"}]
    assert _persistent_review_token(phase="implementing", review_cycles=cycles) is None


# ===== _warnings_token =====


def test_warnings_token_none_when_quiet():
    assert (
        _warnings_token(recoveries=[], test_runs=[], review_cycles=[], extra_enabled=False)
        is None
    )


def test_warnings_token_emits_recovery_count():
    token = _warnings_token(
        recoveries=[{}, {}], test_runs=[], review_cycles=[], extra_enabled=False
    )
    assert token is not None
    assert "↻2" in token.plain


def test_warnings_token_emits_tests_failed():
    token = _warnings_token(
        recoveries=[], test_runs=[{"returncode": 1}], review_cycles=[], extra_enabled=False
    )
    assert token is not None
    assert "tests ✗" in token.plain


def test_warnings_token_tests_passing_stays_quiet():
    # All-pass tests SHOULD NOT surface — the warnings zone is loud signals only.
    assert (
        _warnings_token(
            recoveries=[],
            test_runs=[{"returncode": 0}, {"returncode": 0}],
            review_cycles=[],
            extra_enabled=False,
        )
        is None
    )


def test_warnings_token_extra_disagreed():
    cycles = [
        {"round": 1, "verdict": "APPROVED", "reviewer": "sim"},
        {"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "extra"},
    ]
    token = _warnings_token(recoveries=[], test_runs=[], review_cycles=cycles, extra_enabled=True)
    assert token is not None
    assert "disagreed" in token.plain


def test_warnings_token_extra_agreed_stays_quiet():
    # Agreement is the happy path; nothing should appear.
    cycles = [
        {"round": 1, "verdict": "APPROVED", "reviewer": "sim"},
        {"round": 1, "verdict": "APPROVED", "reviewer": "extra"},
    ]
    assert (
        _warnings_token(recoveries=[], test_runs=[], review_cycles=cycles, extra_enabled=True)
        is None
    )


# ===== _terminal_badge =====


def test_terminal_badge_pr_pushed_with_number():
    text, _ = _terminal_badge("READY_FOR_DRAFT_PR", 1234)
    assert "PR PUSHED" in text
    assert "1234" in text


def test_terminal_badge_pr_pushed_no_number():
    text, _ = _terminal_badge("READY_FOR_DRAFT_PR", None)
    assert text == "PR PUSHED"


def test_terminal_badge_no_pr_changes_req():
    text, _ = _terminal_badge("NO_PR_CHANGES_REQUESTED", None)
    assert "NO_PR" in text
    assert "changes_req" in text


def test_terminal_badge_no_pr_needs_human():
    text, _ = _terminal_badge("NO_PR_NEEDS_HUMAN", None)
    assert "needs human" in text


def test_terminal_badge_failed_infra():
    text, _ = _terminal_badge("FAILED_INFRA", None)
    assert "FAILED" in text


# ===== _pr_number_from_url =====


def test_pr_number_from_url_github():
    assert _pr_number_from_url("https://github.com/owner/repo/pull/42") == 42


def test_pr_number_from_url_with_trailing_segments():
    assert _pr_number_from_url("https://github.com/owner/repo/pull/42/files") == 42
    assert _pr_number_from_url("https://github.com/owner/repo/pull/42?tab=foo") == 42


def test_pr_number_from_url_none_for_garbage():
    assert _pr_number_from_url(None) is None
    assert _pr_number_from_url("") is None
    assert _pr_number_from_url("https://example.com/no-pull-here") is None


# ===== _text_event_count / _task_count =====


def test_text_event_count():
    evts = [
        {"type": "text"},
        {"type": "tool_use"},
        {"type": "text"},
    ]
    assert _text_event_count(evts) == 2


def test_task_count():
    evts = [
        {"type": "tool_use", "part": {"tool": "task"}},
        {"type": "tool_use", "part": {"tool": "bash"}},
        {"type": "tool_use", "part": {"tool": "task"}},
    ]
    assert _task_count(evts) == 2


# ===== _settled_in =====


def _write_tool_event(tool: str, file_path: str = "", status: str = "completed") -> dict:
    return {
        "type": "tool_use",
        "part": {
            "tool": tool,
            "state": {
                "status": status,
                "input": {"filePath": file_path},
            },
        },
    }


def _completed_bash(*, timestamp: int, command: str) -> dict:
    return {
        "type": "tool_use",
        "timestamp": timestamp,
        "part": {
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": command},
            },
        },
    }


def _completed_apply_patch(*, timestamp: int, path: str) -> dict:
    return {
        "type": "tool_use",
        "timestamp": timestamp,
        "part": {
            "tool": "apply_patch",
            "state": {
                "status": "completed",
                "input": {
                    "patchText": (
                        "*** Begin Patch\n"
                        f"*** Update File: {path}\n"
                        "@@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    )
                },
            },
        },
    }


def test_settled_in_false_when_empty():
    assert not _settled_in([])


def test_settled_in_true_on_settled_design_write():
    evts = [_write_tool_event("write", "/worktree/SETTLED_DESIGN.md")]
    assert _settled_in(evts)


def test_settled_in_false_when_not_completed():
    evts = [_write_tool_event("write", "/worktree/SETTLED_DESIGN.md", status="running")]
    assert not _settled_in(evts)


def test_impl_complete_in_true():
    evts = [_write_tool_event("write", "/worktree/IMPLEMENTATION_COMPLETE")]
    assert _impl_complete_in(evts)


def test_impl_complete_in_true_on_apply_patch():
    evts = [
        {
            "type": "tool_use",
            "part": {
                "tool": "apply_patch",
                "state": {
                    "status": "completed",
                    "input": {
                        "patchText": (
                            "*** Begin Patch\n"
                            "*** Add File: .contremaitre/IMPLEMENTATION_COMPLETE\n"
                            "+done\n"
                            "*** End Patch\n"
                        )
                    },
                },
            },
        }
    ]
    assert _impl_complete_in(evts)


# ===== _architecture_review_in =====


def test_architecture_review_in_false_when_empty():
    assert not _architecture_review_in([])


def test_architecture_review_in_true_on_write():
    evts = [_write_tool_event("write", "/worktree/.contremaitre/architecture-review.html")]
    assert _architecture_review_in(evts)


def test_architecture_review_in_true_on_apply_patch():
    evts = [
        {
            "type": "tool_use",
            "part": {
                "tool": "apply_patch",
                "state": {
                    "status": "completed",
                    "input": {
                        "patchText": (
                            "*** Begin Patch\n"
                            "*** Add File: .contremaitre/architecture-review.html\n"
                            "+<html/>\n"
                            "*** End Patch\n"
                        )
                    },
                },
            },
        }
    ]
    assert _architecture_review_in(evts)


def test_architecture_review_in_false_for_unrelated_write():
    evts = [_write_tool_event("write", "/worktree/src/foo.py")]
    assert not _architecture_review_in(evts)


def test_self_verified_counts_apply_patch_as_code_edit():
    evts = [
        _completed_bash(timestamp=1_000, command="pytest -q"),
        _completed_apply_patch(timestamp=2_000, path="app/foo.py"),
    ]
    assert not _self_verified_in(evts)

    evts.append(_completed_bash(timestamp=3_000, command="pytest -q"))
    assert _self_verified_in(evts)


# ===== _latest_pending_tool =====


def _pending_tool_event(tool: str, **inp) -> dict:
    return {
        "type": "tool_use",
        "part": {
            "tool": tool,
            "state": {"status": "running", "input": inp},
        },
    }


def test_latest_pending_tool_none_when_empty():
    assert _latest_pending_tool([]) is None


def test_latest_pending_tool_bash():
    evts = [_pending_tool_event("bash", command="pytest tests/")]
    result = _latest_pending_tool(evts)
    assert result is not None
    assert "bash" in result


def test_latest_pending_tool_read_shows_filename():
    evts = [_pending_tool_event("read", filePath="/src/foo.py")]
    result = _latest_pending_tool(evts)
    assert result is not None
    assert "foo.py" in result


def test_latest_pending_tool_completed_returns_none():
    evts = [
        {
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "state": {"status": "completed", "input": {"command": "ls"}},
            },
        }
    ]
    assert _latest_pending_tool(evts) is None


# ===== _render_guardrail =====
# Smoke-tests that each styled event kind produces output containing the kind
# string. The main guard here is that the style-dispatch branches don't raise.


@pytest.mark.parametrize(
    "kind",
    [
        events.PUBLISHED,
        events.PUBLICATION_BLOCKED,
        events.INFRA_FAILURE,
        events.REVISION_REQUESTED,
        events.REVIEW_VERDICT,
        events.CHECK_COMPLETED,
        events.HARD_GATES_CHECKED,
        events.OPENCODE_ACTOR_START,
        events.WORK_SESSION_END,
        events.TURN_CAP,
    ],
)
def test_render_guardrail_contains_kind(kind):
    ev = _g(kind, verdict="APPROVED", passed=True, returncode=0, role="agent")
    body = _render_guardrail(ev)
    assert kind in body.plain


def test_render_guardrail_recovery_kind_substring():
    ev = {"ts": "2026-01-01T00:00:00.000Z", "kind": events.SQLITE_RECOVERY_SILENT_STALL}
    body = _render_guardrail(ev)
    assert events.SQLITE_RECOVERY_SILENT_STALL in body.plain


# ===== _build_event_row =====


def test_build_event_row_text():
    ev = {"type": "text", "part": {"text": "hello world"}, "timestamp": None}
    marker, ts, typ, tool, body = _build_event_row(ev)
    assert "text" in typ.plain


def test_build_event_row_tool_use():
    ev = {
        "type": "tool_use",
        "part": {"tool": "bash", "state": {"status": "completed", "input": {"command": "ls"}}},
        "timestamp": None,
    }
    marker, ts, typ, tool, body = _build_event_row(ev)
    assert "bash" in tool.plain


def test_build_event_row_unknown_type():
    ev = {"type": "something_new", "timestamp": None}
    marker, ts, typ, tool, body = _build_event_row(ev)
    assert "something_new" in typ.plain


# ===== _short_repo =====


def test_short_repo_ssh_url():
    assert _short_repo("git@github.com:jbmoutout/itadakimasu.git") == "jbmoutout/itadakimasu"


def test_short_repo_ssh_url_no_dotgit():
    assert _short_repo("git@github.com:jbmoutout/itadakimasu") == "jbmoutout/itadakimasu"


def test_short_repo_https_url():
    assert _short_repo("https://github.com/jbmoutout/itadakimasu.git") == "jbmoutout/itadakimasu"


def test_short_repo_https_url_no_dotgit():
    assert _short_repo("https://github.com/jbmoutout/itadakimasu") == "jbmoutout/itadakimasu"


def test_short_repo_https_url_trailing_slash():
    assert _short_repo("https://github.com/jbmoutout/itadakimasu/") == "jbmoutout/itadakimasu"


def test_short_repo_local_path_falls_back_to_basename():
    # No owner/ tail to recover — show the last segment so the header
    # line isn't blank for local-only runs.
    assert _short_repo("/Users/jb/code/itadakimasu") == "itadakimasu"


def test_short_repo_empty_returns_placeholder():
    assert _short_repo(None) == "?"
    assert _short_repo("") == "?"
