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

import pytest

from contremaitre import events
from contremaitre.models import ModelSpec
from contremaitre.tui import (
    _PAL_ERROR,
    _PAL_TEXT,
    _PAL_WARN,
    _PHASES,
    _codex_usage_from_payload,
    _codex_usage_token,
    _fmt_reset,
    _fmt_window_minutes,
    _read_codex_usage,
    _usage_pct_style,
    _activity_state,
    _architecture_review_in,
    _build_event_row,
    _claude_event_rows,
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
    _read_jsonl,
    _render_event,
    _render_guardrail,
    _reviewer_glyph,
    _reviewer_status,
    _round_verdict,
    _self_verified_in,
    _settled_in,
    _short_repo,
    _task_count,
    _terminal_badge,
    _text_event_count,
    _verdict_glyph,
    _warnings_token,
)
from contremaitre.tui import (
    _aggregate_cli_review_verdict,
    _cli_review_tool_header,
    _derive_cli_review_states,
    _review_status_tail,
)


# ---------- helpers ----------


def _g(kind: str, **fields) -> dict:
    """Build a minimal guardrail event dict."""
    return {"ts": "2026-01-01T00:00:00.000Z", "event": kind, **fields}


def _actor_start(role: str) -> dict:
    return _g(events.OPENCODE_ACTOR_START, role=role)


# ===== _read_jsonl =====


def test_read_jsonl_missing_file(tmp_path):
    assert _read_jsonl(tmp_path / "nonexistent.jsonl") == []


def test_read_jsonl_empty_file(tmp_path):
    (tmp_path / "f.jsonl").write_text("")
    assert _read_jsonl(tmp_path / "f.jsonl") == []


def test_read_jsonl_skips_malformed_lines(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
    result = _read_jsonl(p)
    assert result == [{"a": 1}, {"b": 2}]


def test_read_jsonl_skips_non_dict_values(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\n[1, 2]\n42\n')
    result = _read_jsonl(p)
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
        terminal=False,
        terminal_verdict=None,
        settled=False,
        impl_complete=False,
        agent_started=False,
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


def test_derive_phase_done_warn_for_published_human_and_no_pr_variants():
    for verdict in ("PR_NEEDS_HUMAN", "NO_PR_CHANGES_REQUESTED", "NO_PR_NEEDS_HUMAN"):
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


def test_phase_trail_has_one_dot_per_phase():
    text = _phase_trail("implementing", "live")
    dots = [c for c in text.plain if c in "●○"]
    assert len(dots) == len(_PHASES)


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
    assert text.plain.count("●") == len(_PHASES)


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


# ===== _round_verdict =====


def test_round_verdict_returns_none_for_missing_round():
    assert _round_verdict([{"round": 1, "verdict": "APPROVED"}], 2) is None


def test_round_verdict_picks_sim():
    cycles = [{"round": 1, "verdict": "APPROVED"}]  # default reviewer = sim
    assert _round_verdict(cycles, 1) == "APPROVED"


def test_round_verdict_ignores_non_sim_reviewer():
    # Only the sim reviewer row is considered; any other reviewer is ignored.
    cycles = [{"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "other"}]
    assert _round_verdict(cycles, 1) is None


def test_round_verdict_skips_unavailable_entries():
    cycles = [{"round": 1, "verdict": "APPROVED", "reviewer": "sim", "unavailable": True}]
    assert _round_verdict(cycles, 1) is None


# ===== _current_review_round =====


def test_current_review_round_zero_when_no_starts():
    assert _current_review_round([]) == 0


def test_current_review_round_counts_only_sim_review_starts():
    guardrails = [
        _g(events.OPENCODE_ACTOR_START, role="agent"),
        _g(events.OPENCODE_ACTOR_START, role="sim"),
        _g(events.OPENCODE_ACTOR_START, role="review"),
    ]
    assert _current_review_round(guardrails) == 1


def test_current_review_round_advances_per_loop_back():
    guardrails = [
        _g(events.OPENCODE_ACTOR_START, role="review"),
        _g(events.OPENCODE_ACTOR_START, role="agent"),
        _g(events.OPENCODE_ACTOR_START, role="review"),
        _g(events.OPENCODE_ACTOR_START, role="agent"),
        _g(events.OPENCODE_ACTOR_START, role="review"),  # round 3 just opened
    ]
    assert _current_review_round(guardrails) == 3


# ===== _reviewer_status =====


def test_reviewer_status_idle_when_no_start():
    assert _reviewer_status(round_n=1, review_cycles=[], guardrails=[]) == "idle"


def test_reviewer_status_streaming_when_started_no_verdict():
    guardrails = [_g(events.OPENCODE_ACTOR_START, role="review")]
    assert _reviewer_status(round_n=1, review_cycles=[], guardrails=guardrails) == "streaming"


def test_reviewer_status_approved():
    cycles = [{"round": 1, "verdict": "APPROVED", "reviewer": "sim"}]
    guardrails = [_g(events.OPENCODE_ACTOR_START, role="review")]
    assert _reviewer_status(round_n=1, review_cycles=cycles, guardrails=guardrails) == "approved"


def test_reviewer_status_changes_req():
    cycles = [{"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "sim"}]
    assert _reviewer_status(round_n=1, review_cycles=cycles, guardrails=[]) == "changes_req"


def test_reviewer_status_needs_human():
    cycles = [{"round": 1, "verdict": "NEEDS_HUMAN", "reviewer": "sim"}]
    assert _reviewer_status(round_n=1, review_cycles=cycles, guardrails=[]) == "needs_human"


def test_reviewer_status_unavailable():
    cycles = [{"round": 1, "reviewer": "sim", "unavailable": True}]
    assert _reviewer_status(round_n=1, review_cycles=cycles, guardrails=[]) == "unavailable"


def test_reviewer_status_per_round_independence():
    # Round 1 done, round 2 SIM streaming.
    cycles = [
        {"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "sim"},
    ]
    guardrails = [
        _g(events.OPENCODE_ACTOR_START, role="review"),  # round 1
        _g(events.OPENCODE_ACTOR_START, role="agent"),  # work loop
        _g(events.OPENCODE_ACTOR_START, role="review"),  # round 2 — streaming
    ]
    assert _reviewer_status(round_n=2, review_cycles=cycles, guardrails=guardrails) == "streaming"
    # Round 1 verdict is still recoverable for prior-round inspection.
    assert _reviewer_status(round_n=1, review_cycles=cycles, guardrails=guardrails) == "changes_req"


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
    text = _current_phase_label(**_default_label_kwargs(phase="exploring", color_state="error"))
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


def test_phase_label_reviewing_multi_round():
    # Round 2 in flight — label shows round 2, not 1, even though
    # review_cycles still has round-1 verdicts recorded. Past rounds
    # stack their glyphs after "Review" so the operator sees the full
    # reviewing history without scrolling logs.
    cycles = [
        {"round": 1, "verdict": "CHANGES_REQUESTED", "reviewer": "sim"},
    ]
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="reviewing",
            review_cycles=cycles,
            current_review_round=2,
            sim_review_statuses=["changes_req", "streaming"],
        )
    )
    assert "round 2" in text.plain
    assert "round 1" not in text.plain
    # Two glyphs for SIM: ✗ (round 1) then ⏵ (round 2).
    assert text.plain.count("✗") == 1
    assert text.plain.count("⏵") == 1


def test_phase_label_done_pr_pushed_without_title_is_just_done():
    # No title in pr.json → label stays just `Done`. PR # is carried by
    # the verdict zone; putting it in the label too would be noise.
    text = _current_phase_label(
        **_default_label_kwargs(phase="done", terminal_verdict="READY_FOR_DRAFT_PR", pr_number=1234)
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


def test_phase_label_done_pr_needs_human_keeps_title_and_review_tail():
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="done",
            terminal_verdict="PR_NEEDS_HUMAN",
            pr_title="fix: harden cli review loop",
            cli_review_states=[("codex", "completed", "MUST_FIX")],
        )
    )
    assert "PR needs human" in text.plain
    assert "fix: harden cli review loop" in text.plain
    assert "CODEX Review" in text.plain


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
    text = _current_phase_label(**_default_label_kwargs(phase="implementing", color_state="error"))
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
    assert _warnings_token(recoveries=[], test_runs=[]) is None


def test_warnings_token_emits_recovery_count():
    token = _warnings_token(recoveries=[{}, {}], test_runs=[])
    assert token is not None
    assert "↻2" in token.plain


def test_warnings_token_emits_tests_failed():
    token = _warnings_token(recoveries=[], test_runs=[{"returncode": 1}])
    assert token is not None
    assert "tests ✗" in token.plain


def test_warnings_token_tests_passing_stays_quiet():
    # All-pass tests SHOULD NOT surface — the warnings zone is loud signals only.
    assert _warnings_token(recoveries=[], test_runs=[{"returncode": 0}, {"returncode": 0}]) is None


# ===== _terminal_badge =====


def test_terminal_badge_pr_pushed_with_number():
    text, _ = _terminal_badge("READY_FOR_DRAFT_PR", 1234)
    assert "PR PUSHED" in text
    assert "1234" in text


def test_terminal_badge_pr_pushed_no_number():
    text, _ = _terminal_badge("READY_FOR_DRAFT_PR", None)
    assert text == "PR PUSHED"


def test_terminal_badge_pr_needs_human_with_number():
    text, _ = _terminal_badge("PR_NEEDS_HUMAN", 1234)
    assert "PR NEEDS HUMAN" in text
    assert "1234" in text


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


def test_terminal_badge_failed_infra_quota():
    quota = {"kind": "quota", "role": "agent", "model": "x/y", "marker": "FreeUsageLimitError"}
    text, _ = _terminal_badge("FAILED_INFRA", None, provider_failure=quota)
    assert "quota" in text
    assert "transient" not in text


def test_terminal_badge_failed_infra_transient():
    transient = {
        "kind": "transient",
        "role": "sim",
        "model": "x/y",
        "marker": "Provider returned error",
    }
    text, _ = _terminal_badge("FAILED_INFRA", None, provider_failure=transient)
    assert "transient" in text
    assert "quota" not in text


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
                        f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch\n"
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


# Each entry: (kind, event_fields, expected_substrings_in_body.plain).
# The substrings are the kind-specific signals the renderer is supposed to
# surface — NOT just the kind name. A test that asserted only "kind appears
# in body" passes against a renderer that returned `Text(kind)` and ignored
# every other field; locking the icon / payload field per kind catches that.
@pytest.mark.parametrize(
    "kind,fields,expected",
    [
        # ✓ icon — operator's "did it publish?" eye-anchor. The URL itself
        # is surfaced by the dedicated links-line widget, not by the
        # guardrail renderer, so we only lock the icon here.
        (events.PUBLISHED, {}, ["✓"]),
        # Block reason should surface so the operator knows why.
        (events.PUBLICATION_BLOCKED, {"reason": "diff_hash_drift"}, []),
        (events.INFRA_FAILURE, {"error": "docker died"}, ["docker died"]),
        # round= field signals which loop we're in.
        (events.REVISION_REQUESTED, {"round": 2}, ["round=2"]),
        # APPROVED → ✓; verdict field echoed.
        (
            events.REVIEW_VERDICT,
            {"verdict": "APPROVED", "round": 1},
            ["✓", "verdict=APPROVED", "round=1"],
        ),
        # check_completed surfaces the command + ✓ on rc=0.
        (
            events.CHECK_COMPLETED,
            {"returncode": 0, "cmd": "pytest -q", "duration_seconds": 1.5},
            ["✓", "pytest -q"],
        ),
        # hard_gates_checked passed=True → ✓.
        (events.HARD_GATES_CHECKED, {"passed": True}, ["✓"]),
        # actor_start surfaces the role so the operator sees agent/sim/review.
        (events.OPENCODE_ACTOR_START, {"role": "review"}, ["role=review"]),
        (events.WORK_SESSION_END, {"role": "agent"}, ["role=agent"]),
        (events.TURN_CAP, {"role": "agent"}, ["role=agent"]),
    ],
)
def test_render_guardrail_surfaces_kind_specific_fields(kind, fields, expected):
    ev = _g(kind, **fields)
    body = _render_guardrail(ev)
    assert kind in body.plain
    for needle in expected:
        assert needle in body.plain, f"{kind!r}: expected {needle!r} in {body.plain!r}"


def test_render_guardrail_check_completed_nonzero_shows_rc_and_x_icon():
    # Failure-path branch is separately asserted so a regression that
    # always prints `✓` regardless of returncode fails here.
    ev = _g(events.CHECK_COMPLETED, returncode=2, cmd="pytest -q")
    body = _render_guardrail(ev)
    assert "✗" in body.plain
    assert "rc=2" in body.plain


def test_render_guardrail_hard_gates_failed_shows_x_icon():
    ev = _g(events.HARD_GATES_CHECKED, passed=False)
    body = _render_guardrail(ev)
    assert "✗" in body.plain


def test_render_guardrail_review_verdict_changes_requested_shows_x_icon():
    ev = _g(events.REVIEW_VERDICT, verdict="CHANGES_REQUESTED")
    body = _render_guardrail(ev)
    assert "✗" in body.plain
    assert "verdict=CHANGES_REQUESTED" in body.plain


def test_render_guardrail_recovery_kind_substring():
    # Recoveries use `kind=` (not `event=`) and carry `recovered_chars`.
    ev = {
        "ts": "2026-01-01T00:00:00.000Z",
        "kind": events.SQLITE_RECOVERY_SILENT_STALL,
        "role": "agent",
        "recovered_chars": 256,
    }
    body = _render_guardrail(ev)
    assert events.SQLITE_RECOVERY_SILENT_STALL in body.plain
    assert "role=agent" in body.plain
    assert "recovered_chars=256" in body.plain


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


# ===== cli_reviewer multi-tool helpers =====


def test_aggregate_verdict_picks_worst():
    # MUST_FIX > NEEDS_ATTENTION > LOOKS_GOOD. If either reviewer
    # found a blocker the operator must see `MUST_FIX` in the footer,
    # not the rosier of the two.
    assert _aggregate_cli_review_verdict(["LOOKS_GOOD", "MUST_FIX"]) == "MUST_FIX"
    assert _aggregate_cli_review_verdict(["NEEDS_ATTENTION", "LOOKS_GOOD"]) == "NEEDS_ATTENTION"
    assert _aggregate_cli_review_verdict(["LOOKS_GOOD", "LOOKS_GOOD"]) == "LOOKS_GOOD"


def test_aggregate_verdict_empty_is_none():
    assert _aggregate_cli_review_verdict([]) is None
    assert _aggregate_cli_review_verdict([None, None]) is None


def test_aggregate_verdict_ignores_unknown_keys():
    # Defensive — a renamed verdict in a future review prompt shouldn't
    # silently outrank the known keys.
    assert _aggregate_cli_review_verdict(["BIZARRE", "MUST_FIX"]) == "MUST_FIX"


def test_derive_cli_review_states_streaming():
    # Tool started, not yet completed → surfaces as streaming.
    guardrails = [_g(events.CLI_REVIEW_STARTED, tool="claude")]
    states = _derive_cli_review_states(guardrails, "claude")
    assert states == [("claude", "streaming", None)]


def test_derive_cli_review_states_completed():
    # Tool finished with a verdict — state is "completed" with verdict attached.
    guardrails = [
        _g(events.CLI_REVIEW_STARTED, tool="claude"),
        _g(events.CLI_REVIEW_COMPLETED, tool="claude", verdict="LOOKS_GOOD"),
    ]
    states = _derive_cli_review_states(guardrails, "claude")
    assert states == [("claude", "completed", "LOOKS_GOOD")]


def test_derive_cli_review_states_failed_tool_surfaces_status():
    # Tool that failed surfaces as "failed", verdict is None.
    guardrails = [
        _g(events.CLI_REVIEW_STARTED, tool="codex"),
        _g(events.CLI_REVIEW_FAILED, tool="codex"),
    ]
    states = _derive_cli_review_states(guardrails, "codex")
    assert states == [("codex", "failed", None)]


def test_derive_cli_review_states_filters_unstarted_tools():
    # Tool not yet started is not pre-rendered as a dim placeholder —
    # only tools that have emitted cli_review_started are included.
    guardrails = []  # nothing started yet
    states = _derive_cli_review_states(guardrails, "claude")
    assert states == []


def test_derive_cli_review_states_none_choice_returns_empty():
    assert _derive_cli_review_states([], "none") == []


def test_derive_cli_review_states_single_tool_returns_single_entry():
    guardrails = [
        _g(events.CLI_REVIEW_STARTED, tool="claude"),
        _g(events.CLI_REVIEW_COMPLETED, tool="claude", verdict="NEEDS_ATTENTION"),
    ]
    assert _derive_cli_review_states(guardrails, "claude") == [
        ("claude", "completed", "NEEDS_ATTENTION")
    ]


def test_derive_cli_review_states_latest_round_wins():
    guardrails = [
        _g(events.CLI_REVIEW_STARTED, tool="codex", round=1),
        _g(events.CLI_REVIEW_COMPLETED, tool="codex", round=1, verdict="MUST_FIX"),
        _g(events.CLI_REVIEW_STARTED, tool="codex", round=2),
    ]

    assert _derive_cli_review_states(guardrails, "codex") == [("codex", "streaming", None)]


def test_review_status_tail_renders_two_tool_glyphs_for_both():
    # The multi-tool path renders one `<TOOL> Review <glyph>` segment
    # per tool — operator sees both verdicts at a glance.
    tail = _review_status_tail(
        sim_review_statuses=[],
        cli_review_states=[
            ("claude", "completed", "LOOKS_GOOD"),
            ("codex", "streaming", None),
        ],
    )
    assert "CLAUDE Review" in tail.plain
    assert "CODEX Review" in tail.plain
    assert "✓" in tail.plain
    assert "⏵" in tail.plain


def test_review_status_tail_falls_back_to_single_tool_params():
    # Back-compat — when no states list is provided, the historical
    # (status, tool, verdict) trio still drives a single segment.
    tail = _review_status_tail(
        sim_review_statuses=[],
        cli_review_status="completed",
        cli_review_tool="claude",
        cli_review_verdict="MUST_FIX",
    )
    assert "CLAUDE Review" in tail.plain
    assert "✗" in tail.plain
    assert "CODEX Review" not in tail.plain


def test_phase_label_cli_review_both_names_both_tools():
    # `cli_reviewer="both"` shows e.g. `CLAUDE + CODEX review` so the
    # operator sees both reviewers, not a generic `CLI review`.
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="cli_review",
            cli_review_states=[
                ("claude", "streaming", None),
                ("codex", "streaming", None),
            ],
        )
    )
    assert "CLAUDE" in text.plain
    assert "CODEX" in text.plain
    assert "review" in text.plain


def test_phase_label_cli_review_single_tool_unchanged():
    # Single-tool label still reads `CLAUDE review` (existing behavior).
    text = _current_phase_label(
        **_default_label_kwargs(
            phase="cli_review",
            cli_review_tool="claude",
            cli_review_status="streaming",
        )
    )
    assert "CLAUDE review" in text.plain
    assert "CODEX" not in text.plain


def test_cli_review_tool_header_renders_divider_with_tool_name():
    sep = _cli_review_tool_header("claude")
    assert "claude review" in sep.plain
    assert "──" in sep.plain
    sep_codex = _cli_review_tool_header("codex")
    assert "codex review" in sep_codex.plain


# ----- codex --json event rendering (CLI actor streams these into raw_export) -----


def _row_plain(ev):
    _marker, _ts, typ, tool, body = _build_event_row(ev)
    plain = lambda x: x.plain if hasattr(x, "plain") else str(x)  # noqa: E731
    return plain(typ), plain(tool), plain(body)


def test_codex_command_execution_renders_command_and_exit():
    typ, tool, body = _row_plain(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "sed -n app.py",
                "aggregated_output": "def f(): ...",
                "exit_code": 0,
            },
        }
    )
    assert typ == "command"
    assert tool == "bash"
    assert "sed -n app.py" in body
    assert "exit 0" in body
    # aggregated_output is intentionally NOT rendered (noise; still on disk).
    assert "def f(): ..." not in body


def test_render_event_stamps_codex_event_with_read_time():
    # codex events carry no timestamp; _render_event stamps first-render
    # wall-clock so the TUI shows a time column for them.
    ev = {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}
    _render_event(ev)
    assert isinstance(ev.get("timestamp"), int) and ev["timestamp"] > 0


def test_render_event_preserves_existing_timestamp():
    ev = {"type": "text", "part": {"text": "x"}, "timestamp": 123}
    _render_event(ev)
    assert ev["timestamp"] == 123


def test_codex_agent_message_renders_as_text():
    typ, _tool, body = _row_plain(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hello world"}}
    )
    assert typ == "text"
    assert "hello world" in body


def test_codex_turn_completed_shows_tokens():
    typ, _tool, body = _row_plain(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 5, "output_tokens": 2, "cached_input_tokens": 1},
        }
    )
    assert typ == "turn_done"
    assert "in 5" in body and "out 2" in body


def test_text_event_count_includes_codex_agent_message():
    events_list = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "x"}},
        {"type": "text"},
        {"type": "item.completed", "item": {"type": "command_execution"}},  # not a turn
    ]
    assert _text_event_count(events_list) == 2


# ===== codex subscription usage (footer rate-limit indicator) =====


def _token_count_event(*, primary=80.0, secondary=20.0, resets_at=9_999_999_999):
    """A codex `token_count` rollout event. `None` for a window omits it."""

    def _win(used, minutes):
        if used is None:
            return None
        return {"used_percent": used, "window_minutes": minutes, "resets_at": resets_at}

    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"model_context_window": 258400},
            "rate_limits": {
                "plan_type": "plus",
                "primary": _win(primary, 300),
                "secondary": _win(secondary, 10080),
            },
        },
    }


def test_codex_usage_from_payload_extracts_windows():
    usage = _codex_usage_from_payload(_token_count_event()["payload"])
    assert usage["primary"]["used_percent"] == 80.0
    assert usage["primary"]["window_minutes"] == 300
    assert usage["secondary"]["used_percent"] == 20.0


def test_codex_usage_from_payload_none_when_windows_null():
    # codex emits token_count after every turn but only fills the windows on
    # turns that hit the metered endpoint — a null snapshot carries no signal.
    payload = _token_count_event(primary=None, secondary=None)["payload"]
    assert _codex_usage_from_payload(payload) is None


def test_read_codex_usage_skips_trailing_null_snapshot(tmp_path):
    roll = tmp_path / "codex-sim-home" / "sessions" / "2026" / "rollout-x.jsonl"
    roll.parent.mkdir(parents=True)
    roll.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                _token_count_event(primary=70.0),  # the real snapshot
                {"type": "response_item", "payload": {"type": "message"}},
                _token_count_event(primary=None, secondary=None),  # trailing null
            ]
        )
        + "\n"
    )
    usage = _read_codex_usage(tmp_path)
    assert usage is not None
    assert usage["primary"]["used_percent"] == 70.0


def test_read_codex_usage_none_without_codex_homes(tmp_path):
    (tmp_path / "raw_export.jsonl").write_text("{}\n")  # opencode-only run
    assert _read_codex_usage(tmp_path) is None


def test_codex_usage_token_shows_remaining_and_reset():
    # 84% used at now → 16% left; resets 1h2m out → `codex 16% left (↻1h02m)`.
    usage = _codex_usage_from_payload(_token_count_event(primary=84.0, resets_at=3722)["payload"])
    assert _codex_usage_token(usage, now=0.0).plain == "codex 5h 16% left (↻1h02m)"


def test_codex_usage_token_never_appends_weekly_window():
    loose = _codex_usage_from_payload(_token_count_event(primary=84.0, secondary=20.0)["payload"])
    loose_plain = _codex_usage_token(loose, now=0.0).plain
    assert "wk" not in loose_plain and "7d" not in loose_plain
    assert loose_plain.count("left") == 1
    tight = _codex_usage_from_payload(
        _token_count_event(primary=10.0, secondary=95.0, resets_at=None)["payload"]
    )
    tok = _codex_usage_token(tight, now=0.0).plain
    assert tok == "codex 5h 90% left"
    assert "7d" not in tok


def test_codex_usage_token_none_when_no_data():
    assert _codex_usage_token(None) is None
    assert _codex_usage_token({"primary": None, "secondary": None}) is None


def test_usage_pct_style_severity():
    # Only low budgets draw the eye: red → amber → neutral (no green).
    assert _usage_pct_style(5) == _PAL_ERROR
    assert _usage_pct_style(30) == _PAL_WARN
    assert _usage_pct_style(80) == _PAL_TEXT


def test_fmt_window_minutes():
    assert _fmt_window_minutes(300) == "5h"
    assert _fmt_window_minutes(10080) == "7d"
    assert _fmt_window_minutes(90) == "90m"
    assert _fmt_window_minutes(None) == ""


def test_fmt_reset_countdown():
    assert _fmt_reset(3722, 0.0) == "↻1h02m"
    assert _fmt_reset(600, 0.0) == "↻10m"
    assert _fmt_reset(100, 200.0) == ""  # already elapsed
    assert _fmt_reset(None, 0.0) == ""


# ===== claude stream-json rendering =====


def _claude_rows_plain(ev):
    plain = lambda x: x.plain if hasattr(x, "plain") else str(x)  # noqa: E731
    return [
        (plain(typ), plain(tool), plain(body))
        for (_marker, _ts, typ, tool, body) in _claude_event_rows(ev, "12:00:00")
    ]


def test_claude_system_init_renders_session_row():
    rows = _claude_rows_plain(
        {"type": "system", "subtype": "init", "session_id": "abc-123", "model": "claude-opus-4-8"}
    )
    assert len(rows) == 1
    typ, _tool, body = rows[0]
    assert typ == "session"
    assert "abc-123" in body and "claude-opus-4-8" in body


def test_claude_assistant_multiblock_expands_to_n_rows():
    rows = _claude_rows_plain(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "thinking then acting"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "app.py"}},
                ]
            },
        }
    )
    assert len(rows) == 3  # one row per content block
    assert rows[0][0] == "text" and "thinking then acting" in rows[0][2]
    assert rows[1][0] == "tool_use" and rows[1][1] == "Bash" and "pytest -q" in rows[1][2]
    assert rows[2][0] == "tool_use" and rows[2][1] == "Read" and "app.py" in rows[2][2]


def test_claude_result_renders_turn_done_with_cost():
    rows = _claude_rows_plain(
        {
            "type": "result",
            "subtype": "success",
            "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 80},
            "total_cost_usd": 0.0512,
        }
    )
    assert len(rows) == 1
    typ, _tool, body = rows[0]
    assert typ == "turn_done"
    assert "in 100" in body and "out 10" in body and "cache-r 80" in body
    assert "$0.0512" in body


def test_claude_user_tool_result_renders_compact_row():
    rows = _claude_rows_plain(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "1 passed in 0.1s"}
                ]
            },
        }
    )
    assert len(rows) == 1
    assert rows[0][0] == "tool_result" and "1 passed" in rows[0][2]


def test_render_event_stamps_claude_event():
    ev = {"type": "result", "subtype": "success", "result": "done"}
    _render_event(ev)
    assert isinstance(ev.get("timestamp"), int) and ev["timestamp"] > 0


def test_text_event_count_includes_claude_result():
    evts = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
        {"type": "result", "subtype": "success", "result": "a"},
        {"type": "result", "subtype": "success", "result": "b"},
    ]
    assert _text_event_count(evts) == 2  # two turns (two results), not the assistant


def test_codex_usage_token_absent_for_claude_run():
    # claude stream-json carries no subscription-window data, so the footer
    # usage token is codex-only (None when there's no rate-limit snapshot).
    assert _codex_usage_token(None) is None


# ===== timestamp coercion (claude emits ISO-8601 strings) =====


def test_fmt_ts_handles_iso_string_and_int_and_none():
    from contremaitre.tui import _fmt_ts, _ts_to_ms

    # claude's user/assistant/result events carry ISO-8601 strings.
    assert _ts_to_ms("2026-06-06T12:49:25.658Z") == 1780750165658
    assert _ts_to_ms(1780750165658) == 1780750165658
    assert _ts_to_ms("1780750165658") == 1780750165658
    assert _ts_to_ms(None) is None
    assert _ts_to_ms("") is None
    assert _ts_to_ms("not-a-time") is None
    # _fmt_ts must never raise on any of these (regression: TypeError str/int).
    assert _fmt_ts("2026-06-06T12:49:25.658Z") != ""
    assert _fmt_ts(None) == "        "


def test_render_event_survives_claude_iso_string_timestamp():
    # Regression: a claude tool_result `user` event carries an ISO-8601 string
    # timestamp; _render_event must not crash dividing str by 1000.
    ev = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "content": "<tool_use_error>boom</tool_use_error>",
                    "is_error": True,
                    "tool_use_id": "t1",
                }
            ]
        },
        "timestamp": "2026-06-06T12:49:25.658Z",
    }
    _render_event(ev)  # must not raise


def test_resolved_model_from_events_surfaces_claude_model():
    from contremaitre.models import resolved_model_from_events

    events = [
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 5},
        {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6", "session_id": "s"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    ]
    assert resolved_model_from_events(events) == "claude-sonnet-4-6"
    # No init event (e.g. codex stream) → None (spec keeps its requested model).
    assert resolved_model_from_events([{"type": "turn.started"}]) is None


def test_with_resolved_sharpens_identity_keeping_effort():
    from contremaitre.models import ModelSpec

    # The live relabel: back-filling the stream's real model must keep the
    # effort — the header would otherwise lose it the moment a claude run starts.
    spec = ModelSpec(runtime="claude", requested="", effort="high")
    sharp = spec.with_resolved("claude-opus-4-8")
    assert sharp.display() == "claude/claude-opus-4-8 high"
    assert sharp.canonical() == ("claude-opus-4-8", "claude")
    # No stream model (codex) → unchanged spec, by identity.
    assert spec.with_resolved(None) is spec


def test_model_spec_display_shows_runtime_prefix():
    from contremaitre.models import ModelSpec

    # CLI roles: `<runtime>/<short-model> <effort>`.
    assert ModelSpec.from_record("gpt-5.5 (codex, effort=high)").display() == "codex/gpt-5.5 high"
    assert (
        ModelSpec.from_record("claude-opus-4-8 (claude, effort=max)").display()
        == "claude/claude-opus-4-8 max"
    )
    # opencode role: keep `<provider>/<short-model>`, no effort.
    assert (
        ModelSpec.from_record("opencode/deepseek-v4-flash-free").display()
        == "opencode/deepseek-v4-flash-free"
    )
    # openrouter slug drops the middle org segment: `<provider>/<short-model>`.
    assert (
        ModelSpec.from_record("openrouter/deepseek/deepseek-v4-flash").display()
        == "openrouter/deepseek-v4-flash"
    )
    # Unknown/empty → null sentinel.
    assert ModelSpec.from_record("?").display() == "?"
    assert ModelSpec.from_record(None).display() == "?"
    assert ModelSpec.from_record("").display() == "?"


def test_read_run_models_reads_model_specs_from_run_config(tmp_path):
    from contremaitre.tui import _read_run_models

    # run_config.json now persists the canonical ModelSpec dict per role.
    (tmp_path / "run_config.json").write_text(
        json.dumps(
            {
                "agent_model": {
                    "runtime": "codex",
                    "requested": "gpt-5.5",
                    "effort": "high",
                    "resolved": None,
                    "provider": None,
                },
                "sim_model": {
                    "runtime": "claude",
                    "requested": "opus",
                    "effort": "max",
                    "resolved": None,
                    "provider": None,
                },
                "cli_reviewer": "none",
                "docker_image": "img",
            }
        ),
        encoding="utf-8",
    )

    agent, sim, _cli_review, _image, _target, _base = _read_run_models(tmp_path)
    assert agent.display() == "codex/gpt-5.5 high"
    assert sim.display() == "claude/opus max"


def test_read_run_models_tolerates_legacy_string_run_config(tmp_path):
    from contremaitre.tui import _read_run_models

    # Historical run dirs hold the legacy label/slug string; from_record absorbs
    # it so the reader still renders them.
    (tmp_path / "run_config.json").write_text(
        json.dumps(
            {
                "agent_model": "gpt-5.5 (codex, effort=high)",
                "sim_model": "opencode/deepseek-v4-flash-free",
                "cli_reviewer": "none",
                "docker_image": "img",
            }
        ),
        encoding="utf-8",
    )

    agent, sim, *_ = _read_run_models(tmp_path)
    assert agent.display() == "codex/gpt-5.5 high"
    assert sim.canonical() == ("deepseek-v4-flash-free", "opencode")


def test_read_run_models_reconstructs_legacy_cli_run_config(tmp_path):
    from contremaitre.tui import _read_run_models

    # Legacy run_config: agent_model/sim_model are the raw opencode slug a CLI
    # role ignores, with runtime/tool/effort in sibling fields. Reconstruct via
    # ModelSpec.build so the CLI roles aren't mislabeled opencode.
    (tmp_path / "run_config.json").write_text(
        json.dumps(
            {
                "agent_model": "opencode/deepseek-v4-flash-free",
                "sim_model": "openrouter/qwen/qwen3-max",
                "actor_mode": "cli",
                "sim_actor_mode": "cli",
                "cli_tool": "codex",
                "sim_cli_tool": "claude",
                "codex_model": "gpt-5.5",
                "codex_effort": "high",
                "claude_model": "opus",
                "claude_effort": "max",
                "cli_reviewer": "none",
                "docker_image": "img",
            }
        ),
        encoding="utf-8",
    )

    agent, sim, *_ = _read_run_models(tmp_path)
    assert agent.canonical() == ("gpt-5.5", "codex")
    assert agent.display() == "codex/gpt-5.5 high"
    assert sim.canonical() == ("opus", "claude")
    assert sim.display() == "claude/opus max"


def test_read_run_models_legacy_opencode_run_config_stays_opencode(tmp_path):
    from contremaitre.tui import _read_run_models

    # A legacy opencode run_config reconstructs to the slug identity, not a
    # spurious CLI label.
    (tmp_path / "run_config.json").write_text(
        json.dumps(
            {
                "agent_model": "openrouter/deepseek/deepseek-v4-flash",
                "sim_model": "opencode/big-pickle",
                "actor_mode": "opencode",
                "cli_reviewer": "none",
                "docker_image": "img",
            }
        ),
        encoding="utf-8",
    )

    agent, sim, *_ = _read_run_models(tmp_path)
    assert agent.canonical() == ("deepseek-v4-flash", "opencode")
    assert sim.canonical() == ("big-pickle", "opencode")


# ===== claude footer usage (statusLine rate-limit indicator) =====


def _claude_statusline_line(
    *,
    five_hour=80.0,
    seven_day=20.0,
    five_hour_reset=9_999_999_999,
    seven_day_reset=9_999_999_999,
    model="claude-sonnet-4-6",
):
    return json.dumps(
        {
            "recorded_at": 1,
            "session_id": "s",
            "model": {"id": model, "display_name": model},
            "rate_limits": {
                "five_hour": {
                    "used_percentage": five_hour,
                    "resets_at": five_hour_reset,
                }
                if five_hour is not None
                else None,
                "seven_day": {
                    "used_percentage": seven_day,
                    "resets_at": seven_day_reset,
                }
                if seven_day is not None
                else None,
            },
        }
    )


def test_claude_usage_from_statusline_extracts_windows():
    from contremaitre.tui import _claude_usage_from_statusline

    usage = _claude_usage_from_statusline(json.loads(_claude_statusline_line()))
    assert usage is not None
    assert usage["primary"]["used_percent"] == 80.0
    assert usage["primary"]["window_minutes"] == 300
    assert usage["secondary"]["used_percent"] == 20.0
    assert usage["secondary"]["window_minutes"] == 10080
    assert usage["model"] == "claude-sonnet-4-6"
    assert usage["family"] == "sonnet"


def test_read_claude_usage_picks_last_statusline_snapshot(tmp_path):
    from contremaitre.tui import _read_claude_usage

    snap = tmp_path / "claude-agent-home" / ".contremaitre" / "statusline.jsonl"
    snap.parent.mkdir(parents=True)
    snap.write_text(
        "\n".join(
            [
                _claude_statusline_line(five_hour_reset=111),
                _claude_statusline_line(five_hour_reset=222),  # last one wins
            ]
        )
    )
    usage = _read_claude_usage(tmp_path)
    assert usage is not None and usage["primary"]["resets_at"] == 222


def test_read_claude_usages_keeps_latest_per_model(tmp_path):
    from contremaitre.tui import _read_claude_usages

    snap = tmp_path / "claude-agent-home" / ".contremaitre" / "statusline.jsonl"
    snap.parent.mkdir(parents=True)
    snap.write_text(
        "\n".join(
            [
                _claude_statusline_line(model="claude-sonnet-4-6", five_hour=80.0),
                _claude_statusline_line(model="claude-opus-4-8", five_hour=60.0),
                _claude_statusline_line(model="claude-sonnet-4-6", five_hour=30.0),
            ]
        )
    )
    usages = _read_claude_usages(tmp_path)
    by_model = {u["model"]: u for u in usages}
    assert by_model["claude-sonnet-4-6"]["primary"]["used_percent"] == 30.0
    assert by_model["claude-opus-4-8"]["primary"]["used_percent"] == 60.0


def test_read_claude_usage_none_for_codex_run(tmp_path):
    from contremaitre.tui import _read_claude_usage

    # A codex stream has no claude statusLine snapshot → no claude footer.
    (tmp_path / "raw_export.jsonl").write_text(
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}})
    )
    assert _read_claude_usage(tmp_path) is None


def test_claude_usage_token_formats_window_and_reset():
    from contremaitre.tui import _claude_usage_token

    tok = _claude_usage_token(
        {
            "primary": {
                "used_percent": 84.0,
                "window_minutes": 300,
                "resets_at": 3722,
            },
            "secondary": {
                "used_percent": 20.0,
                "window_minutes": 10080,
                "resets_at": 9999,
            },
        },
        now=0.0,
        model_label="sonnet",
    )
    assert tok is not None
    plain = tok.plain
    assert plain == "claude sonnet 5h 16% left (↻1h02m)"


def test_claude_usage_token_appends_weekly_only_when_tighter():
    from contremaitre.tui import _claude_usage_token

    tok = _claude_usage_token(
        {
            "primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": 0},
            "secondary": {"used_percent": 95.0, "window_minutes": 10080, "resets_at": 0},
        },
        now=0.0,
        model_label="opus",
    )
    assert tok is not None
    assert "claude opus 5h 90% left" in tok.plain
    assert "7d 5% left" in tok.plain


def test_claude_usage_token_none_when_empty():
    from contremaitre.tui import _claude_usage_token

    assert _claude_usage_token(None) is None
    assert _claude_usage_token({"primary": None, "secondary": None}) is None


def test_footer_meter_tokens_are_per_role_and_show_unknown_counters():
    from contremaitre.tui import _footer_meter_tokens

    tokens = _footer_meter_tokens(
        agent_model=ModelSpec.from_record("gpt-5.5 (codex, effort=high)"),
        sim_model=ModelSpec.from_record("claude-sonnet-4-6 (claude, effort=high)"),
        agent_events=[],
        sim_events=[],
        codex_usage=None,
        claude_usages=[],
        now=0.0,
    )
    assert [t.plain for t in tokens] == ["A codex ?", "S claude ?"]


def test_footer_meter_tokens_mix_free_paid_codex_and_model_specific_claude():
    from contremaitre.tui import _claude_usage_from_statusline, _footer_meter_tokens

    codex_usage = _codex_usage_from_payload(
        _token_count_event(primary=84.0, resets_at=None)["payload"]
    )
    claude_usages = [
        _claude_usage_from_statusline(
            json.loads(
                _claude_statusline_line(
                    model="claude-sonnet-4-6",
                    five_hour=3.0,
                    five_hour_reset=None,
                    seven_day_reset=None,
                )
            )
        ),
        _claude_usage_from_statusline(
            json.loads(
                _claude_statusline_line(
                    model="claude-opus-4-8",
                    five_hour=75.0,
                    five_hour_reset=None,
                    seven_day_reset=None,
                )
            )
        ),
    ]

    assert [
        t.plain
        for t in _footer_meter_tokens(
            agent_model=ModelSpec.from_record("opencode/deepseek-v4-flash-free"),
            sim_model=ModelSpec.from_record("openrouter/qwen/qwen3-max"),
            agent_events=[],
            sim_events=[{"type": "step_finish", "part": {"cost_usd": 0.1234}}],
            codex_usage=codex_usage,
            claude_usages=[u for u in claude_usages if u],
            now=0.0,
        )
    ] == ["A free (◕‿◕)", "S $0.1234"]

    assert [
        t.plain
        for t in _footer_meter_tokens(
            agent_model=ModelSpec.from_record("gpt-5.5 (codex, effort=high)"),
            sim_model=ModelSpec.from_record("claude-opus-4-8 (claude, effort=max)"),
            agent_events=[],
            sim_events=[],
            codex_usage=codex_usage,
            claude_usages=[u for u in claude_usages if u],
            now=0.0,
        )
    ] == ["A codex 5h 16% left", "S claude opus 5h 25% left"]
