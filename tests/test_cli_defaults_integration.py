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
    current_agent="opencode/deepseek-v4-flash-free",
    current_sim="opencode/deepseek-v4-flash-free",
):
    """Drive `_pick_models_interactive` with scripted Enter responses."""

    it = iter(replies)
    with (
        patch("contremaitre.cli._fetch_free_models", return_value=_FAKE_FREE),
        patch("builtins.input", side_effect=lambda *_a, **_kw: next(it)),
    ):
        return _pick_models_interactive(
            current_agent=current_agent,
            current_sim=current_sim,
            pick_agent=True,
            pick_sim=True,
            allow_custom=False,
        )


def test_picker_sim_default_uses_saved_sim_not_agent_index():
    # Regression: hitting Enter on the sim prompt must use the saved sim
    # model, not whatever the operator picked for agent.
    _, sim, picker_args = _run_picker_with_inputs(
        ["", ""],
        current_agent="opencode/deepseek-v4-flash-free",
        current_sim="opencode/nemotron-3-super-free",
    )
    assert sim == "opencode/nemotron-3-super-free"
    assert ("--sim-model", "opencode/nemotron-3-super-free") in picker_args


# Sanity-check that the documented schema in defaults.py module docstring
# actually round-trips through load().
def test_documented_schema_example_loads_cleanly(monkeypatch, tmp_path: Path):
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
    out = defaults.load()
    assert out == defaults.Defaults(
        agent_model="opencode/big-pickle",
        sim_model="opencode/big-pickle",
        cli_reviewer="both",
    )
