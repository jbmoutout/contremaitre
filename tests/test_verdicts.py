"""Unit tests for SIM verdict parsing.

The interface under test is `parse_sim_verdict`: it must recover the verdict
object from a real LLM-judge message — reasoning prose followed by the JSON
object — without being fooled by inline braces in that prose. The field/type
checks stay strict; only the JSON-extraction layer is lenient.
"""

from __future__ import annotations

import json

import pytest

from contremaitre.models import ReviewVerdict
from contremaitre.verdicts import VerdictParseError, parse_sim_verdict

_VERDICT = {
    "verdict": "APPROVED",
    "confidence": 0.95,
    "required_changes": [],
    "checks_performed": ["read the diff"],
    "summary": "looks good",
}


def _emit(verdict: dict) -> str:
    return json.dumps(verdict)


def test_clean_json_object_parses():
    parsed = parse_sim_verdict(_emit(_VERDICT))
    assert parsed.verdict == ReviewVerdict.APPROVED
    assert parsed.confidence == 0.95


def test_prose_with_inline_braces_before_verdict():
    """Regression: prose mentioning `{agent, sim}` / `{"codex","auto"}` must
    not be mistaken for the verdict object. This is the exact shape that
    silently sank every SIM verdict in run 20260613-225744."""

    raw = (
        'The base fact is re-derived at `"codex" in {agent, sim} or reviewer '
        'in {"codex","auto"}`, and `active ⊆ {"codex","claude"}`. Emitting '
        "the verdict.\n\n" + _emit(_VERDICT)
    )
    parsed = parse_sim_verdict(raw)
    assert parsed.verdict == ReviewVerdict.APPROVED


def test_fenced_json_block_parses():
    raw = "Here is my verdict:\n\n```json\n" + _emit(_VERDICT) + "\n```\n"
    parsed = parse_sim_verdict(raw)
    assert parsed.verdict == ReviewVerdict.APPROVED


def test_prefers_verdict_keyed_object_over_other_json():
    """An earlier non-verdict JSON object in the prose must not win over the
    real verdict object."""

    raw = (
        'Config was `{"codex": 1, "claude": 2}` at the time. '
        "Final verdict:\n" + _emit(_VERDICT)
    )
    parsed = parse_sim_verdict(raw)
    assert parsed.verdict == ReviewVerdict.APPROVED
    assert parsed.summary == "looks good"


def test_prose_only_message_has_no_verdict():
    with pytest.raises(VerdictParseError):
        parse_sim_verdict("I checked `{agent, sim}` and it all looks fine.")


def test_unknown_verdict_string_still_rejected():
    bad = dict(_VERDICT, verdict="MAYBE")
    with pytest.raises(VerdictParseError):
        parse_sim_verdict(_emit(bad))


def test_missing_field_still_rejected():
    bad = {k: v for k, v in _VERDICT.items() if k != "confidence"}
    with pytest.raises(VerdictParseError):
        parse_sim_verdict(_emit(bad))


def test_out_of_range_confidence_rejected():
    bad = dict(_VERDICT, confidence=1.5)
    with pytest.raises(VerdictParseError):
        parse_sim_verdict(_emit(bad))


# --------------------------------------------------------------------------
# Guard still fails closed — a genuinely-bad final verdict must be rejected
# even when valid-looking JSON appears earlier in the prose. Lenient
# *extraction* must not weaken *validation*.
# --------------------------------------------------------------------------


def test_invalid_final_verdict_not_masked_by_decoy():
    """A decoy `{...}` in prose must not let an unknown final verdict slip
    through: we prefer the last verdict-keyed object, so the broken one is
    what gets validated — and rejected."""

    raw = (
        'config was `{"codex": 1, "claude": 2}`. Final:\n'
        + _emit(dict(_VERDICT, verdict="MAYBE"))
    )
    with pytest.raises(VerdictParseError):
        parse_sim_verdict(raw)


def test_truncated_final_verdict_rejected():
    """SIM hit the token limit mid-object: no complete verdict survives, so
    the gate fails closed and the orchestrator retries."""

    raw = 'All checks pass. {"verdict": "APPROVED", "confidence": 0.9, "required_changes": ['
    with pytest.raises(VerdictParseError):
        parse_sim_verdict(raw)
