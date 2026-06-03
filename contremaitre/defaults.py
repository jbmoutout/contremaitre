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
    agent_model = "opencode/big-pickle"
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


@dataclass(frozen=True)
class Defaults:
    """Operator-level picker prefills. All fields optional.

    `extra_reviewer_skip` is the parsed form of `extra_reviewer_model =
    "skip"` in the file — the slug field stays `None` and this boolean
    signals "don't even ask in the picker."
    """

    agent_model: str | None = None
    sim_model: str | None = None
    extra_reviewer_model: str | None = None
    extra_reviewer_skip: bool = False
    cli_reviewer: str | None = None


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
