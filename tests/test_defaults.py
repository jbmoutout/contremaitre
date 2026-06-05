from __future__ import annotations

from pathlib import Path

from contremaitre import defaults


def test_load_returns_empty_when_file_missing(tmp_path: Path):
    assert defaults.load(tmp_path / "nope.toml") == defaults.Defaults()


def test_load_returns_empty_when_file_unreadable_dir(tmp_path: Path):
    # Pointing at a directory triggers OSError; load must degrade silently.
    assert defaults.load(tmp_path) == defaults.Defaults()


def test_load_returns_empty_when_toml_malformed(tmp_path: Path):
    bad = tmp_path / "defaults.toml"
    bad.write_text("not = a = valid = toml")
    assert defaults.load(bad) == defaults.Defaults()


def test_load_returns_empty_when_not_a_table(tmp_path: Path):
    # tomllib parses array-only TOML, but our schema expects a top-level
    # table. Anything else should degrade to "no defaults".
    bad = tmp_path / "defaults.toml"
    bad.write_text("# just a comment\n")
    # An empty TOML doc is a valid empty table → all fields None.
    assert defaults.load(bad) == defaults.Defaults()


def test_load_reads_all_fields(tmp_path: Path):
    path = tmp_path / "defaults.toml"
    path.write_text(
        "\n".join(
            [
                'agent_model = "opencode/big-pickle"',
                'sim_model = "openrouter/qwen/qwen3-max"',
                'extra_reviewer_model = "opencode/nemotron-3-super-free"',
                'cli_reviewer = "both"',
            ]
        )
        + "\n"
    )
    out = defaults.load(path)
    assert out == defaults.Defaults(
        agent_model="opencode/big-pickle",
        sim_model="openrouter/qwen/qwen3-max",
        extra_reviewer_model="opencode/nemotron-3-super-free",
        cli_reviewer="both",
    )


def test_load_normalizes_codex_actor_alias(tmp_path: Path):
    # "codex" is the operator-facing name; it normalizes to the "cli" runtime.
    path = tmp_path / "defaults.toml"
    path.write_text('actor = "codex"\nsim_actor = "opencode"\n')
    out = defaults.load(path)
    assert out.actor == "cli"
    assert out.sim_actor == "opencode"


def test_load_passes_through_opencode_and_fake_actor(tmp_path: Path):
    path = tmp_path / "defaults.toml"
    path.write_text('actor = "opencode"\n')
    assert defaults.load(path).actor == "opencode"


def test_load_drops_invalid_actor(tmp_path: Path):
    # A typo / future value drops to None → falls through to the picker default.
    path = tmp_path / "defaults.toml"
    path.write_text('actor = "claudette"\n')
    assert defaults.load(path).actor is None


def test_load_reads_codex_model_and_effort(tmp_path: Path):
    path = tmp_path / "defaults.toml"
    path.write_text('codex_model = "gpt-5.5"\ncodex_effort = "high"\n')
    out = defaults.load(path)
    assert out.codex_model == "gpt-5.5"
    assert out.codex_effort == "high"


def test_load_drops_invalid_codex_effort(tmp_path: Path):
    path = tmp_path / "defaults.toml"
    path.write_text('codex_effort = "ludicrous"\n')
    assert defaults.load(path).codex_effort is None


def test_load_strips_whitespace(tmp_path: Path):
    path = tmp_path / "defaults.toml"
    path.write_text('agent_model = "  opencode/big-pickle  "\n')
    out = defaults.load(path)
    assert out.agent_model == "opencode/big-pickle"


def test_load_drops_invalid_cli_reviewer_value(tmp_path: Path):
    # A defaults file from a future version (or a typo) must not break
    # the launch screen — we silently drop unknown enum values.
    path = tmp_path / "defaults.toml"
    path.write_text('agent_model = "opencode/big-pickle"\ncli_reviewer = "lefuturoide"\n')
    out = defaults.load(path)
    assert out.agent_model == "opencode/big-pickle"
    assert out.cli_reviewer is None


def test_load_drops_non_string_fields(tmp_path: Path):
    path = tmp_path / "defaults.toml"
    path.write_text("agent_model = 42\n")
    assert defaults.load(path).agent_model is None


def test_load_parses_extra_reviewer_skip_sentinel(tmp_path: Path):
    # `extra_reviewer_model = "skip"` is an explicit "don't ask in the
    # picker either" signal. Lifted into `extra_reviewer_skip=True` and
    # the slug field stays None so downstream config doesn't try to
    # launch an extra reviewer with the literal string "skip".
    path = tmp_path / "defaults.toml"
    path.write_text('extra_reviewer_model = "skip"\n')
    out = defaults.load(path)
    assert out.extra_reviewer_model is None
    assert out.extra_reviewer_skip is True


def test_load_extra_reviewer_skip_defaults_false_when_absent(tmp_path: Path):
    path = tmp_path / "defaults.toml"
    path.write_text('extra_reviewer_model = "opencode/big-pickle"\n')
    out = defaults.load(path)
    assert out.extra_reviewer_model == "opencode/big-pickle"
    assert out.extra_reviewer_skip is False


def test_defaults_path_returns_xdg_when_no_file_exists(monkeypatch, tmp_path: Path):
    # No file on disk → fall through to the XDG location so callers
    # have a stable "here is where to put it" pointer for docs / errors.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    out = defaults.defaults_path()
    assert out == tmp_path / "xdg" / "contremaitre" / "defaults.toml"


def test_defaults_path_prefers_cwd_local_over_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # Both exist; cwd-local wins. Makes the dev workflow (edit the file
    # next to the code) the dominant path without dropping XDG support.
    local = tmp_path / ".contremaitre" / "defaults.toml"
    local.parent.mkdir(parents=True)
    local.write_text('agent_model = "opencode/big-pickle"\n')
    xdg = tmp_path / "xdg" / "contremaitre" / "defaults.toml"
    xdg.parent.mkdir(parents=True)
    xdg.write_text('agent_model = "openrouter/qwen/qwen3-max"\n')
    assert defaults.defaults_path() == local


def test_defaults_path_falls_back_to_xdg_when_cwd_local_missing(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    xdg = tmp_path / "xdg" / "contremaitre" / "defaults.toml"
    xdg.parent.mkdir(parents=True)
    xdg.write_text("\n")
    assert defaults.defaults_path() == xdg


def test_xdg_path_falls_back_to_home(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)  # ensure no cwd-local file pollutes the test
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = defaults.defaults_path()
    assert out == tmp_path / "home" / ".config" / "contremaitre" / "defaults.toml"


def test_load_picks_cwd_local_over_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    local = tmp_path / ".contremaitre" / "defaults.toml"
    local.parent.mkdir(parents=True)
    local.write_text('agent_model = "opencode/big-pickle"\n')
    xdg = tmp_path / "xdg" / "contremaitre" / "defaults.toml"
    xdg.parent.mkdir(parents=True)
    xdg.write_text('agent_model = "openrouter/qwen/qwen3-max"\n')
    assert defaults.load().agent_model == "opencode/big-pickle"


def test_load_falls_through_to_xdg_when_no_cwd_local(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    xdg = tmp_path / "xdg" / "contremaitre" / "defaults.toml"
    xdg.parent.mkdir(parents=True)
    xdg.write_text('agent_model = "openrouter/qwen/qwen3-max"\n')
    assert defaults.load().agent_model == "openrouter/qwen/qwen3-max"
