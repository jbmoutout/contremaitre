"""Unit tests for `ModelSpec` — the canonical model-identity record.

`ModelSpec` is the single home of model identity: atomic stored fields, derived
`display()` / `canonical()`, one factory (`build` / `for_role`), and one reader
(`from_record`) that tolerates legacy on-disk strings. These tests exercise the
record directly — the interface is the test surface, so identity logic is no
longer verified through each caller's private normalizer.
"""

from __future__ import annotations

from contremaitre.models import (
    ActorMode,
    CliReviewVerdict,
    ModelSpec,
    RunConfig,
    resolved_model_from_events,
)


# --------------------------------------------------------------------------
# build() — each runtime
# --------------------------------------------------------------------------


def test_build_codex_records_model_and_effort():
    spec = ModelSpec.build(
        mode=ActorMode.CLI, opencode_model="x", codex_model="gpt-5.5", codex_effort="xhigh"
    )
    assert spec == ModelSpec(runtime="codex", requested="gpt-5.5", effort="xhigh")
    assert spec.canonical() == ("gpt-5.5", "codex")
    assert spec.display() == "codex/gpt-5.5 xhigh"


def test_build_claude_account_default():
    spec = ModelSpec.build(
        mode=ActorMode.CLI,
        opencode_model="x",
        cli_tool="claude",
        claude_model="",
        claude_effort="max",
    )
    assert spec.runtime == "claude"
    assert spec.requested == ""
    assert spec.effort == "max"
    # Pre-resolution: name is unknown, so canonical collapses to "?".
    assert spec.canonical() == ("?", "claude")
    assert spec.display() == "claude/default max"


def test_build_opencode_zen_vs_openrouter_provider():
    zen = ModelSpec.build(mode=ActorMode.OPENCODE, opencode_model="opencode/big-pickle")
    assert zen.provider == "opencode"
    assert zen.display() == "opencode/big-pickle"

    router = ModelSpec.build(
        mode="opencode", opencode_model="openrouter/deepseek/deepseek-v4-flash"
    )
    assert router.provider == "openrouter"
    assert router.canonical() == ("deepseek-v4-flash", "opencode")
    assert router.display() == "openrouter/deepseek-v4-flash"


def test_build_fake_runtime():
    spec = ModelSpec.build(mode=ActorMode.FAKE, opencode_model="opencode/big-pickle")
    assert spec.runtime == "fake"
    assert spec.canonical() == ("big-pickle", "fake")


# --------------------------------------------------------------------------
# for_role() — applies SIM overrides
# --------------------------------------------------------------------------


def test_for_role_applies_sim_overrides(tmp_path):
    config = RunConfig(
        repo=tmp_path,
        base="main",
        runs_root=tmp_path,
        run_slug="s",
        actor_mode=ActorMode.CLI,
        cli_tool="codex",
        codex_model="gpt-5.5",
        codex_effort="high",
        sim_actor_mode=ActorMode.CLI,
        sim_cli_tool="claude",
        claude_model="opus",
        claude_effort="max",
    )
    assert ModelSpec.for_role(config, "agent").canonical() == ("gpt-5.5", "codex")
    sim = ModelSpec.for_role(config, "sim")
    assert sim.runtime == "claude"
    assert sim.requested == "opus"
    assert sim.effort == "max"


def test_for_role_sim_falls_back_to_agent_runtime(tmp_path):
    config = RunConfig(
        repo=tmp_path,
        base="main",
        runs_root=tmp_path,
        run_slug="s",
        actor_mode=ActorMode.OPENCODE,
        agent_model="z/a",
        sim_model="z/b",
    )
    # No sim_actor_mode override → SIM shares the agent's runtime.
    assert ModelSpec.for_role(config, "sim").canonical() == ("b", "opencode")


# --------------------------------------------------------------------------
# resolved — prefers stream-reported model
# --------------------------------------------------------------------------


def test_with_resolved_prefers_stream_model():
    spec = ModelSpec(runtime="claude", requested="", effort="high")
    sharp = spec.with_resolved("claude-sonnet-4-6")
    assert sharp.resolved == "claude-sonnet-4-6"
    assert sharp.canonical() == ("claude-sonnet-4-6", "claude")
    assert sharp.display() == "claude/claude-sonnet-4-6 high"


def test_with_resolved_noop_returns_self():
    spec = ModelSpec(runtime="codex", requested="gpt-5.5", effort="high")
    assert spec.with_resolved(None) is spec
    assert spec.with_resolved("") is spec


def test_resolved_model_from_events():
    assert (
        resolved_model_from_events(
            [{"type": "system", "subtype": "init", "model": "claude-opus-4-8"}]
        )
        == "claude-opus-4-8"
    )
    assert resolved_model_from_events([{"type": "turn.started"}]) is None


# --------------------------------------------------------------------------
# from_record — dict, legacy string, round-trip
# --------------------------------------------------------------------------


def test_from_record_reads_canonical_dict():
    spec = ModelSpec(
        runtime="codex", requested="gpt-5.5", effort="high", resolved=None, provider=None
    )
    assert ModelSpec.from_record(spec.to_dict()) == spec


def test_from_record_parses_legacy_cli_label():
    spec = ModelSpec.from_record("gpt-5.5 (codex, effort=high)")
    assert (spec.runtime, spec.requested, spec.effort) == ("codex", "gpt-5.5", "high")
    spec = ModelSpec.from_record("claude default (claude, effort=max)")
    assert (spec.runtime, spec.requested, spec.effort) == ("claude", "claude default", "max")
    assert spec.canonical() == ("claude-default", "claude")


def test_from_record_parses_legacy_slug():
    spec = ModelSpec.from_record("opencode/deepseek-v4-flash-free")
    assert spec.runtime == "opencode"
    assert spec.provider == "opencode"
    assert spec.canonical() == ("deepseek-v4-flash-free", "opencode")


def test_from_record_preserves_empty_requested_verbatim():
    # claude account-default persists requested="" — the dict reader must keep
    # it verbatim, not coerce to "?", or the canonical value is lost and the
    # round-trip breaks.
    spec = ModelSpec(runtime="claude", requested="", effort="max")
    back = ModelSpec.from_record(spec.to_dict())
    assert back.requested == ""
    assert back == spec
    # Once resolved is stored, the round-trip still sharpens correctly.
    sharp = spec.with_resolved("claude-sonnet-4-6")
    assert ModelSpec.from_record(sharp.to_dict()) == sharp


def test_from_record_handles_empty_and_none():
    for empty in (None, "", "?", {}):
        spec = ModelSpec.from_record(empty)
        assert spec.canonical() == ("?", "opencode")
        assert spec.display() == "?"


def test_round_trip_through_dict_and_back():
    original = ModelSpec.build(
        mode=ActorMode.CLI,
        opencode_model="x",
        cli_tool="claude",
        claude_model="opus",
        claude_effort="max",
    ).with_resolved("claude-opus-4-8")
    assert ModelSpec.from_record(original.to_dict()) == original


# --------------------------------------------------------------------------
# CliReviewVerdict — owns its own severity order, parse, and worst-of-N
# --------------------------------------------------------------------------


def test_severity_orders_looks_good_lt_needs_attention_lt_must_fix():
    assert (
        CliReviewVerdict.LOOKS_GOOD.severity
        < CliReviewVerdict.NEEDS_ATTENTION.severity
        < CliReviewVerdict.MUST_FIX.severity
    )


def test_is_blocking_only_must_fix():
    assert CliReviewVerdict.MUST_FIX.is_blocking is True
    assert CliReviewVerdict.NEEDS_ATTENTION.is_blocking is False
    assert CliReviewVerdict.LOOKS_GOOD.is_blocking is False


def test_parse_each_key_on_line_one():
    assert (
        CliReviewVerdict.parse("🟢 LOOKS_GOOD — no blocking issues\n\nfine\n")
        is CliReviewVerdict.LOOKS_GOOD
    )
    assert (
        CliReviewVerdict.parse("🟠 NEEDS_ATTENTION — non-blocking\n\n…")
        is CliReviewVerdict.NEEDS_ATTENTION
    )
    assert CliReviewVerdict.parse("🔴 MUST_FIX — blocking\n\n…") is CliReviewVerdict.MUST_FIX


def test_parse_is_worst_first_within_a_line():
    # The regression: a justification that *mentions* a milder key must not
    # outrank the stated verdict. eval's old best-first scan read this as
    # LOOKS_GOOD and scored it 1.0; worst-first reads MUST_FIX.
    assert (
        CliReviewVerdict.parse("✗ MUST_FIX — not LOOKS_GOOD yet\n\n…") is CliReviewVerdict.MUST_FIX
    )


def test_parse_key_without_glyph():
    assert CliReviewVerdict.parse("MUST_FIX — broken\n\n…") is CliReviewVerdict.MUST_FIX


def test_parse_tolerates_leading_blank_lines():
    assert CliReviewVerdict.parse("\n\n🟢 LOOKS_GOOD — fine\n\n…") is CliReviewVerdict.LOOKS_GOOD


def test_parse_none_when_no_key():
    assert CliReviewVerdict.parse("Looks good, nothing structured\n\n…") is None


def test_worst_empty_and_all_none_is_none():
    assert CliReviewVerdict.worst([]) is None
    assert CliReviewVerdict.worst([None, None]) is None


def test_worst_must_fix_wins_over_clean():
    assert CliReviewVerdict.worst(["LOOKS_GOOD", "MUST_FIX"]) is CliReviewVerdict.MUST_FIX


def test_worst_needs_attention_over_looks_good():
    assert (
        CliReviewVerdict.worst(["NEEDS_ATTENTION", "LOOKS_GOOD"])
        is CliReviewVerdict.NEEDS_ATTENTION
    )


def test_worst_ignores_none_and_unparseable_wire_junk():
    # Accepts wire strings, members, None, and junk — junk and None are skipped.
    assert (
        CliReviewVerdict.worst([None, "BIZARRE", CliReviewVerdict.MUST_FIX])
        is CliReviewVerdict.MUST_FIX
    )
