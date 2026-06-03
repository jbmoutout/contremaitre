"""Defaults overlay: hand-edited defaults.toml seeds `contremaitre run` args.

Run-screen happy path is covered by the picker tests; here we lock in
the precedence rules around saved defaults so a future refactor cannot
silently let saved values override explicit CLI flags.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from unittest.mock import patch

from contremaitre import defaults
from contremaitre.cli import _apply_saved_defaults, _pick_models_interactive


def _make_args(**overrides) -> argparse.Namespace:
    base = dict(
        agent_model="openrouter/deepseek/deepseek-v4-flash",
        sim_model="openrouter/deepseek/deepseek-v4-flash",
        extra_reviewer_model=None,
        cli_reviewer="auto",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _write_defaults(tmp_path: Path, body: str) -> None:
    """Seed the XDG fallback file under `tmp_path`.

    Integration tests rely on the XDG location so `monkeypatch.chdir`
    isolates them from any real `./.contremaitre/defaults.toml` that
    might exist in the test runner's cwd. The cwd-local lookup order
    is exercised in `test_defaults.py`.
    """

    target = tmp_path / "contremaitre" / "defaults.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_saved_defaults_fill_in_when_no_explicit_flags(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_defaults(
        tmp_path,
        "\n".join(
            [
                'agent_model = "opencode/big-pickle"',
                'sim_model = "opencode/big-pickle"',
                'cli_reviewer = "both"',
            ]
        )
        + "\n",
    )
    args = _make_args()
    _apply_saved_defaults(args, argv=["contremaitre", "run", "--base", "main"])
    assert args.agent_model == "opencode/big-pickle"
    assert args.sim_model == "opencode/big-pickle"
    assert args.cli_reviewer == "both"


def test_explicit_flag_beats_saved_default(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_defaults(
        tmp_path,
        'agent_model = "opencode/big-pickle"\ncli_reviewer = "both"\n',
    )
    args = _make_args(agent_model="openrouter/qwen/qwen3-max")
    _apply_saved_defaults(
        args,
        argv=[
            "contremaitre",
            "run",
            "--base",
            "main",
            "--agent-model",
            "openrouter/qwen/qwen3-max",
        ],
    )
    # Explicit flag wins for agent_model …
    assert args.agent_model == "openrouter/qwen/qwen3-max"
    # … but saved cli_reviewer still applies because the operator didn't
    # pass --cli-reviewer.
    assert args.cli_reviewer == "both"


def test_apply_is_a_no_op_when_defaults_file_missing(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    args = _make_args()
    _apply_saved_defaults(args, argv=["contremaitre", "run", "--base", "main"])
    # Values unchanged.
    assert args.agent_model == "openrouter/deepseek/deepseek-v4-flash"
    assert args.cli_reviewer == "auto"


def test_extra_reviewer_skip_sentinel_raises_picker_flag(
    monkeypatch, tmp_path: Path
):
    # `extra_reviewer_model = "skip"` raises the `_defaults_skip_extra`
    # sentinel so the launch-screen picker still PROMPTS for extra
    # reviewer, but Enter skips instead of accepting the suggestion.
    # The slug field stays None so downstream config doesn't carry the
    # literal "skip" string.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_defaults(tmp_path, 'extra_reviewer_model = "skip"\n')
    args = _make_args()
    _apply_saved_defaults(args, argv=["contremaitre", "run", "--base", "main"])
    assert args.extra_reviewer_model is None
    assert getattr(args, "_defaults_skip_extra", False) is True


def test_explicit_extra_flag_suppresses_skip_sentinel(monkeypatch, tmp_path: Path):
    # An explicit --extra-reviewer-model on the CLI overrides the file's
    # skip sentinel — the operator may want to one-shot a real model
    # without editing the file. `_defaults_skip_extra` stays unset so the
    # picker (if it runs at all) treats Enter as accept.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_defaults(tmp_path, 'extra_reviewer_model = "skip"\n')
    args = _make_args(extra_reviewer_model="opencode/big-pickle")
    _apply_saved_defaults(
        args,
        argv=[
            "contremaitre",
            "run",
            "--base",
            "main",
            "--extra-reviewer-model",
            "opencode/big-pickle",
        ],
    )
    assert args.extra_reviewer_model == "opencode/big-pickle"
    assert getattr(args, "_defaults_skip_extra", False) is False


def test_apply_handles_equals_form_of_explicit_flag(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_defaults(tmp_path, 'agent_model = "opencode/big-pickle"\n')
    args = _make_args(agent_model="openrouter/qwen/qwen3-max")
    _apply_saved_defaults(
        args,
        argv=[
            "contremaitre",
            "run",
            "--agent-model=openrouter/qwen/qwen3-max",
        ],
    )
    assert args.agent_model == "openrouter/qwen/qwen3-max"


_FAKE_FREE = [
    {"id": "deepseek-v4-flash-free"},
    {"id": "nemotron-3-super-free"},
    {"id": "qwen3-mini-free"},
]


def _run_picker_with_inputs(
    replies,
    *,
    extra_default_skip,
    current_agent="opencode/deepseek-v4-flash-free",
    current_sim="opencode/deepseek-v4-flash-free",
    current_extra=None,
):
    """Drive `_pick_models_interactive` with scripted Enter responses.

    Mocks the free-catalog fetch + `input()`. Each prompt grabs the next
    item from `replies`; running out raises StopIteration, which the
    picker doesn't catch — making accidental over-prompting visible in
    test failures.
    """

    it = iter(replies)
    with (
        patch("contremaitre.cli._fetch_free_models", return_value=_FAKE_FREE),
        patch("builtins.input", side_effect=lambda *_a, **_kw: next(it)),
    ):
        return _pick_models_interactive(
            current_agent=current_agent,
            current_sim=current_sim,
            current_extra=current_extra,
            pick_agent=True,
            pick_sim=True,
            pick_extra=True,
            allow_custom=False,
            extra_default_skip=extra_default_skip,
        )


def test_picker_default_skip_makes_enter_skip_extra_reviewer():
    # All three prompts get Enter. With extra_default_skip=True, the
    # extra slot stays unset — Enter is the skip path.
    agent, sim, extra, picker_args = _run_picker_with_inputs(
        ["", "", ""], extra_default_skip=True
    )
    assert agent.startswith("opencode/")
    assert sim.startswith("opencode/")
    assert extra is None
    # picker_args must not contain --extra-reviewer-model — that's what
    # the orchestrator + tui-passthrough rely on for "skipped".
    flags = [flag for flag, _ in picker_args]
    assert "--extra-reviewer-model" not in flags


def test_picker_sim_default_uses_saved_sim_not_agent_index():
    # Regression: before this fix, hitting Enter on the sim prompt
    # produced whatever the operator just picked for agent, ignoring
    # `sim_model` in defaults.toml. Sim must default to its OWN saved
    # value when set.
    _, sim, _, picker_args = _run_picker_with_inputs(
        ["", "", "s"],
        extra_default_skip=False,
        current_agent="opencode/deepseek-v4-flash-free",
        current_sim="opencode/nemotron-3-super-free",
    )
    assert sim == "opencode/nemotron-3-super-free"
    assert ("--sim-model", "opencode/nemotron-3-super-free") in picker_args


def test_picker_extra_default_uses_saved_extra_not_cross_family():
    # Regression: before this fix, Enter on the extra prompt accepted
    # whatever cross-family model the picker suggested, ignoring
    # `extra_reviewer_model` in defaults.toml. Saved value wins.
    _, _, extra, picker_args = _run_picker_with_inputs(
        ["", "", ""],
        extra_default_skip=False,
        current_agent="opencode/deepseek-v4-flash-free",
        current_sim="opencode/deepseek-v4-flash-free",
        current_extra="opencode/nemotron-3-super-free",
    )
    assert extra == "opencode/nemotron-3-super-free"
    assert ("--extra-reviewer-model", "opencode/nemotron-3-super-free") in picker_args


def test_picker_falls_back_to_cross_family_when_no_saved_extra():
    # When current_extra is None, the cross-family heuristic still
    # supplies a suggestion (preserves the historical UX for first-time
    # operators with no defaults.toml).
    _, _, extra, _ = _run_picker_with_inputs(
        ["", "", ""],
        extra_default_skip=False,
        current_agent="opencode/deepseek-v4-flash-free",
        current_sim="opencode/deepseek-v4-flash-free",
        current_extra=None,
    )
    assert extra is not None
    assert extra.startswith("opencode/")


def test_picker_without_default_skip_accepts_suggested_extra_on_enter():
    # Sanity check the inverse — same three Enters, but with the
    # historical default behavior. Enter on the extra slot should accept
    # the suggested model.
    agent, sim, extra, picker_args = _run_picker_with_inputs(
        ["", "", ""], extra_default_skip=False
    )
    assert extra is not None
    assert extra.startswith("opencode/")
    flags = [flag for flag, _ in picker_args]
    assert "--extra-reviewer-model" in flags


# Sanity-check that the documented schema in defaults.py module docstring
# actually round-trips through load(): copy it byte-for-byte, parse,
# assert each field. Catches schema drift where the docstring example
# stops matching the parser.
def test_documented_schema_example_loads_cleanly(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_defaults(
        tmp_path,
        "\n".join(
            [
                'agent_model = "opencode/big-pickle"',
                'sim_model = "opencode/big-pickle"',
                'extra_reviewer_model = "opencode/nemotron-3-super-free"',
                'cli_reviewer = "both"',
            ]
        )
        + "\n",
    )
    out = defaults.load()
    assert out == defaults.Defaults(
        agent_model="opencode/big-pickle",
        sim_model="opencode/big-pickle",
        extra_reviewer_model="opencode/nemotron-3-super-free",
        cli_reviewer="both",
    )
