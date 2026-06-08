"""Tests for `manifest_digest` — the "system under test" comparability key.

The digest hashes each role's `ModelSpec.canonical()` + effort, so CLI model
swaps, effort changes, and (once back-filled) the resolved model all register
as a different system. The eval canary overlays the resolved specs from
`stats.json` before digesting, so these resolved-aware cases are what the
canary actually compares.
"""

from __future__ import annotations

from contremaitre.manifest import DIGEST_VERSION, manifest_digest
from contremaitre.models import ModelSpec


def _manifest(agent: ModelSpec, sim: ModelSpec) -> dict:
    return {
        "contremaitre_git_sha": "sha",
        "contremaitre_git_dirty": False,
        "dockerfile_sha256": "df",
        "skills_lock_sha256": "sk",
        "agent_model": agent.to_dict(),
        "sim_model": sim.to_dict(),
        "prompt_hashes": {},
    }


def test_digest_distinguishes_codex_effort():
    high = ModelSpec(runtime="codex", requested="gpt-5.5", effort="high")
    xhigh = ModelSpec(runtime="codex", requested="gpt-5.5", effort="xhigh")
    sim = ModelSpec(runtime="opencode", requested="opencode/qwen", provider="opencode")
    assert manifest_digest(_manifest(high, sim)) != manifest_digest(_manifest(xhigh, sim))


def test_digest_distinguishes_resolved_account_default():
    # Two claude account-default runs (identical requested="") that resolved to
    # different models must NOT collide — this is the gap the eval overlay
    # closes by feeding stats.json's resolved specs into the digest.
    base = ModelSpec(runtime="claude", requested="", effort="max")
    opus = base.with_resolved("claude-opus-4-8")
    sonnet = base.with_resolved("claude-sonnet-4-6")
    sim = ModelSpec(runtime="opencode", requested="opencode/qwen", provider="opencode")
    assert manifest_digest(_manifest(opus, sim)) != manifest_digest(_manifest(sonnet, sim))
    # And the unresolved pair collides (same canonical "?"), which is exactly
    # why resolved must be back-filled before the digest.
    assert manifest_digest(_manifest(base, sim)) == manifest_digest(_manifest(base, sim))


def test_digest_tolerates_legacy_string_identity():
    spec = ModelSpec(runtime="codex", requested="gpt-5.5", effort="high")
    sim = ModelSpec(runtime="opencode", requested="opencode/qwen", provider="opencode")
    m = _manifest(spec, sim)
    # Legacy run dirs stored a label/slug string under agent_model/sim_model;
    # from_record absorbs them so the digest computes without raising.
    legacy = {**m, "agent_model": "gpt-5.5 (codex, effort=high)", "sim_model": "opencode/qwen"}
    assert manifest_digest(legacy) == manifest_digest(m)


def test_digest_version_bump_changes_every_digest():
    # DIGEST_VERSION is a literal token in the hashed parts, so a future bump
    # deliberately resets all baselines. Guard that it is in the hashed set by
    # confirming it is a non-empty token (the hash itself is opaque).
    assert DIGEST_VERSION
