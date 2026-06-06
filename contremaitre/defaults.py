"""Operator defaults read from a hand-edited TOML file.

Holds the operator's preferred picker values (agent / sim / extra-reviewer
model strings + cli_reviewer choice) so `contremaitre run` doesn't re-ask
on every invocation. The launch screen reads these as the picker's
prefilled defaults; the operator confirms with Enter per run. Passing
`--no-prompt` to `run` skips the picker entirely and uses the saved
values verbatim.

Lookup order:
    1. `./.contremaitre/defaults.toml`  (cwd-local, next to `runs/`)
    2. `$XDG_CONFIG_HOME/contremaitre/defaults.toml`
       (or `~/.config/contremaitre/defaults.toml`)
The cwd-local file wins so an operator working from the contremaitre repo
sees and edits the file next to their code; the XDG path is the fallback
for invocations from arbitrary directories.

The file is hand-edited — there is no writer. Reads degrade silently: a
missing, malformed, or unreadable file returns an empty `Defaults`
without raising — a broken defaults file must not block a run that
would otherwise launch with hardcoded fallbacks.

Schema (all keys optional):
    actor = "claude"        # opencode | codex | claude | cli | fake — agent (and
                            # SIM unless sim_actor) runtime. "codex"/"claude" both
                            # alias the "cli" runtime and pin that CLI tool.
    sim_actor = "opencode"  # override the SIM runtime (mix: CLI agent + SIM)
    codex_model = "gpt-5.5" # codex-native model name used when a role is codex
    codex_effort = "high"   # minimal | low | medium | high | xhigh
    claude_model = "opus"   # claude model name used when a role is claude
                            # (empty → the ~/.claude account default)
    claude_effort = "high"  # low | medium | high | max
    agent_model = "opencode/big-pickle"   # used when a role is opencode
    sim_model = "opencode/big-pickle"
    extra_reviewer_model = "opencode/nemotron-3-super-free"  # or "skip"
    cli_reviewer = "both"   # auto | codex | claude | both | none

The `extra_reviewer_model = "skip"` sentinel disables the second-SIM
picker prompt entirely (equivalent to hitting `s` every run). Without
it, the picker still asks even when no slug is set.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


_FILENAME = "defaults.toml"
_VALID_CLI_REVIEWER = ("auto", "codex", "claude", "both", "none")
# Friendly actor aliases → the runtime value the CLI/`ActorMode` understands.
# "codex"/"claude" are the operator-facing names for the `cli` runtime (the CLI
# tool is carried separately as `cli_tool`), so the file can read `actor = "claude"`.
_ACTOR_ALIASES = {
    "opencode": "opencode",
    "codex": "cli",
    "claude": "cli",
    "cli": "cli",
    "fake": "fake",
}
# Operator-facing actor names that also pin a specific CLI tool.
_ACTOR_CLI_TOOLS = {"codex": "codex", "claude": "claude"}
_VALID_CODEX_EFFORT = ("minimal", "low", "medium", "high", "xhigh")
_VALID_CLAUDE_EFFORT = ("low", "medium", "high", "max")


@dataclass(frozen=True)
class Defaults:
    """Operator-level picker prefills. All fields optional.

    `extra_reviewer_skip` is the parsed form of `extra_reviewer_model =
    "skip"` in the file — the slug field stays `None` and this boolean
    signals "don't even ask in the picker."

    `actor` / `sim_actor` are normalized to runtime values ("codex" → "cli").
    """

    agent_model: str | None = None
    sim_model: str | None = None
    extra_reviewer_model: str | None = None
    extra_reviewer_skip: bool = False
    cli_reviewer: str | None = None
    actor: str | None = None
    sim_actor: str | None = None
    cli_tool: str | None = None
    codex_model: str | None = None
    codex_effort: str | None = None
    claude_model: str | None = None
    claude_effort: str | None = None


def defaults_path() -> Path:
    """Return the first existing defaults file in lookup order, else the XDG path.

    Caller-friendly: the returned path is what `load()` would actually
    read. When no file exists, returns the XDG path so error messages /
    docs can point at the conventional "where to put it" location.
    """

    for candidate in _candidate_paths():
        if candidate.exists():
            return candidate
    return _xdg_path()


def _candidate_paths() -> list[Path]:
    """Lookup order for the defaults file — cwd-local first, then XDG."""

    return [Path.cwd() / ".contremaitre" / _FILENAME, _xdg_path()]


def _xdg_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "contremaitre" / _FILENAME


def load(path: Path | None = None) -> Defaults:
    """Read defaults.toml, returning an empty `Defaults` on any failure.

    Without an explicit `path`, walks `_candidate_paths()` and reads the
    first one that exists. Treating missing/malformed files as "no
    defaults" is intentional: the file is a convenience layer, not a
    contract. The launch screen's hardcoded fallbacks always work.
    """

    target = path or defaults_path()
    try:
        raw = target.read_bytes()
    except OSError:
        return Defaults()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return Defaults()
    if not isinstance(data, dict):
        return Defaults()

    extra_raw = _clean_str(data.get("extra_reviewer_model"))
    extra_skip = extra_raw == "skip"
    return Defaults(
        agent_model=_clean_str(data.get("agent_model")),
        sim_model=_clean_str(data.get("sim_model")),
        extra_reviewer_model=None if extra_skip else extra_raw,
        extra_reviewer_skip=extra_skip,
        cli_reviewer=_clean_cli_reviewer(data.get("cli_reviewer")),
        actor=_clean_actor(data.get("actor")),
        sim_actor=_clean_actor(data.get("sim_actor")),
        # The CLI tool is carried by the operator-facing actor name ("codex" /
        # "claude"); both normalize to the "cli" runtime, so this preserves which.
        cli_tool=_actor_cli_tool(data.get("actor")),
        codex_model=_clean_str(data.get("codex_model")),
        codex_effort=_clean_codex_effort(data.get("codex_effort")),
        claude_model=_clean_str(data.get("claude_model")),
        claude_effort=_clean_claude_effort(data.get("claude_effort")),
    )


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _clean_cli_reviewer(value: object) -> str | None:
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    return cleaned if cleaned in _VALID_CLI_REVIEWER else None


def _clean_actor(value: object) -> str | None:
    """Normalize a friendly actor name to its runtime value, or None if invalid.

    "codex" → "cli" (the runtime the CLI understands); unknown values drop to
    None so a typo falls through to the picker default rather than crashing.
    """

    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    return _ACTOR_ALIASES.get(cleaned.lower())


def _clean_codex_effort(value: object) -> str | None:
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    return cleaned if cleaned.lower() in _VALID_CODEX_EFFORT else None


def _clean_claude_effort(value: object) -> str | None:
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    return cleaned if cleaned.lower() in _VALID_CLAUDE_EFFORT else None


def _actor_cli_tool(value: object) -> str | None:
    """Derive the CLI tool from the operator-facing actor name, or None.

    "codex" → "codex", "claude" → "claude"; "cli"/"opencode"/"fake"/unknown →
    None (no specific tool pinned, callers fall back to the codex default).
    """

    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    return _ACTOR_CLI_TOOLS.get(cleaned.lower())
