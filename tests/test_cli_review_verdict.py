"""The `CliReviewVerdict` severity surface — the single home that the
commit-status projection, the TUI glyph, the eval canary score, and the viewer
tier all route through. The interface is the test surface."""

from __future__ import annotations

import json

from contremaitre.models import CliReviewVerdict


def test_rank_orders_severity_low_to_high():
    assert CliReviewVerdict.LOOKS_GOOD.rank == 0
    assert CliReviewVerdict.NEEDS_ATTENTION.rank == 1
    assert CliReviewVerdict.MUST_FIX.rank == 2


def test_quality_score_is_inverse_of_severity():
    # Derived, not tabled: must reproduce the old _VERDICT_KEY_TO_SCORE exactly.
    assert CliReviewVerdict.LOOKS_GOOD.quality_score == 1.0
    assert CliReviewVerdict.NEEDS_ATTENTION.quality_score == 0.5
    assert CliReviewVerdict.MUST_FIX.quality_score == 0.0


def test_quality_score_monotonic_with_severity():
    # The relation that makes the two axes one: worse severity → lower quality.
    members = [
        CliReviewVerdict.LOOKS_GOOD,
        CliReviewVerdict.NEEDS_ATTENTION,
        CliReviewVerdict.MUST_FIX,
    ]
    scores = [m.quality_score for m in members]
    assert scores == sorted(scores, reverse=True)


def test_blocks_merge_only_must_fix():
    assert CliReviewVerdict.MUST_FIX.blocks_merge
    assert not CliReviewVerdict.NEEDS_ATTENTION.blocks_merge
    assert not CliReviewVerdict.LOOKS_GOOD.blocks_merge


def test_coerce_accepts_str_enum_and_rejects_garbage():
    assert CliReviewVerdict.coerce("MUST_FIX") is CliReviewVerdict.MUST_FIX
    assert CliReviewVerdict.coerce(CliReviewVerdict.LOOKS_GOOD) is CliReviewVerdict.LOOKS_GOOD
    assert CliReviewVerdict.coerce(None) is None
    assert CliReviewVerdict.coerce("BIZARRE") is None


def test_serializes_to_plain_key_for_json():
    # str-Enum: members must round-trip through JSON as their bare key so the
    # artifact contract stays string-valued for old readers.
    assert json.dumps({"v": CliReviewVerdict.MUST_FIX}) == '{"v": "MUST_FIX"}'
