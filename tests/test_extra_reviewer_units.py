"""Unit tests for the extra-reviewer building blocks.

These are intentionally separate from the end-to-end control-plane tests:
they exercise the merge-verdict logic, the model-family classifier, and the
flow_use review_rounds switch directly, so a regression in one piece is
localised to a small failing assertion rather than a confusing fixture run.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from contremaitre.flow_use import compute_phases
from contremaitre.model_family import model_family
from contremaitre.models import ParsedVerdict, ReviewVerdict
from contremaitre.orchestrator import _merge_verdicts


def _verdict(
    *,
    verdict: ReviewVerdict,
    confidence: float = 0.9,
    required_changes: list[str] | None = None,
    summary: str = "ok",
) -> ParsedVerdict:
    return ParsedVerdict(
        verdict=verdict,
        confidence=confidence,
        required_changes=required_changes or [],
        checks_performed=["read settled", "read diff"],
        summary=summary,
        raw="{}",
    )


class MergeVerdictsTest(unittest.TestCase):
    def test_extra_none_returns_sim_unchanged(self):
        sim = _verdict(verdict=ReviewVerdict.APPROVED)
        merged = _merge_verdicts(sim, None)
        self.assertIs(merged, sim)

    def test_both_approved_returns_approved(self):
        sim = _verdict(verdict=ReviewVerdict.APPROVED, confidence=0.95)
        extra = _verdict(verdict=ReviewVerdict.APPROVED, confidence=0.80)
        merged = _merge_verdicts(sim, extra)
        self.assertEqual(merged.verdict, ReviewVerdict.APPROVED)
        # Confidence floor — merged surfaces the weakest of the two.
        self.assertEqual(merged.confidence, 0.80)

    def test_changes_requested_wins_over_approved(self):
        sim = _verdict(verdict=ReviewVerdict.APPROVED)
        extra = _verdict(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            required_changes=["add boundary test"],
        )
        merged = _merge_verdicts(sim, extra)
        self.assertEqual(merged.verdict, ReviewVerdict.CHANGES_REQUESTED)
        self.assertEqual(merged.required_changes, ["[EXTRA] add boundary test"])

    def test_needs_human_escalates_over_changes_requested(self):
        # The bullet order in the original DUMP draft had CHANGES_REQUESTED
        # before NEEDS_HUMAN — this test pins the strictest-wins choice.
        sim = _verdict(verdict=ReviewVerdict.NEEDS_HUMAN)
        extra = _verdict(verdict=ReviewVerdict.CHANGES_REQUESTED)
        merged = _merge_verdicts(sim, extra)
        self.assertEqual(merged.verdict, ReviewVerdict.NEEDS_HUMAN)

    def test_both_changes_requested_dedupes_overlap(self):
        sim = _verdict(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            required_changes=["Add test", "Remove dead code"],
        )
        extra = _verdict(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            required_changes=["  add test  ", "Document boundary"],
        )
        merged = _merge_verdicts(sim, extra)
        self.assertEqual(merged.verdict, ReviewVerdict.CHANGES_REQUESTED)
        # Overlap (case- and whitespace-insensitive) gets [SIM+EXTRA] tag.
        # SIM-only and EXTRA-only items get their respective tags.
        self.assertEqual(
            merged.required_changes,
            [
                "[SIM+EXTRA] Add test",
                "[SIM] Remove dead code",
                "[EXTRA] Document boundary",
            ],
        )

    def test_checks_performed_union_preserves_order(self):
        sim = _verdict(verdict=ReviewVerdict.APPROVED)
        extra = ParsedVerdict(
            verdict=ReviewVerdict.APPROVED,
            confidence=0.9,
            required_changes=[],
            checks_performed=["read diff", "ran tests"],
            summary="extra-ok",
            raw="{}",
        )
        merged = _merge_verdicts(sim, extra)
        # Dedupes "read diff" (in both) while keeping insertion order.
        self.assertEqual(
            merged.checks_performed,
            ["read settled", "read diff", "ran tests"],
        )

    def test_summary_concatenates_with_extra_prefix(self):
        sim = _verdict(verdict=ReviewVerdict.APPROVED, summary="sim says ok")
        extra = _verdict(verdict=ReviewVerdict.APPROVED, summary="extra concurs")
        merged = _merge_verdicts(sim, extra)
        self.assertEqual(merged.summary, "sim says ok\n— EXTRA: extra concurs")


class ModelFamilyTest(unittest.TestCase):
    def test_three_segment_provider_family_variant(self):
        self.assertEqual(
            model_family("openrouter/deepseek/deepseek-v4-flash"),
            "deepseek",
        )
        self.assertEqual(
            model_family("openrouter/qwen/qwen3-32b"),
            "qwen",
        )

    def test_two_segment_provider_bare_id(self):
        self.assertEqual(model_family("opencode/qwen-32b-free"), "qwen")
        self.assertEqual(model_family("opencode/deepseek-v4-free"), "deepseek")

    def test_anthropic_and_openai_families(self):
        self.assertEqual(model_family("openrouter/anthropic/claude-4"), "anthropic")
        self.assertEqual(model_family("openrouter/openai/gpt-5"), "openai")

    def test_unknown_falls_back_to_unknown(self):
        self.assertEqual(model_family(""), "unknown")
        self.assertEqual(model_family(None), "unknown")
        self.assertEqual(model_family("opencode/big-pickle"), "unknown")


class FlowUseReviewRoundsTest(unittest.TestCase):
    """Lock the `len(cycles) → max(round)` switch.

    With the extra reviewer enabled, review_cycles carries two entries per
    round. `len(cycles)` would double-count rounds; `max(round)` matches the
    intent (how many revision loops were exercised).
    """

    def test_review_rounds_uses_max_round_not_len(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_cycles = root / "review_cycles.jsonl"
            # Two rounds, each with sim + extra entries → 4 rows total.
            rows = [
                {"round": 1, "reviewer": "sim", "verdict": "CHANGES_REQUESTED"},
                {"round": 1, "reviewer": "extra", "verdict": "CHANGES_REQUESTED"},
                {"round": 2, "reviewer": "sim", "verdict": "APPROVED"},
                {"round": 2, "reviewer": "extra", "verdict": "APPROVED"},
            ]
            review_cycles.write_text(
                "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                encoding="utf-8",
            )
            paths = SimpleNamespace(
                raw_export=root / "missing.jsonl",
                sim_raw_export=root / "missing-sim.jsonl",
                review_cycles=review_cycles,
                guardrail_events=root / "missing-gr.jsonl",
            )
            phases = compute_phases(paths)
            self.assertEqual(phases["review_rounds"], 2)

    def test_review_rounds_empty(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = SimpleNamespace(
                raw_export=root / "missing.jsonl",
                sim_raw_export=root / "missing-sim.jsonl",
                review_cycles=root / "missing-cycles.jsonl",
                guardrail_events=root / "missing-gr.jsonl",
            )
            phases = compute_phases(paths)
            self.assertEqual(phases["review_rounds"], 0)


if __name__ == "__main__":
    unittest.main()
