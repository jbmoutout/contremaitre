"""Resume checkpoint: persist enough of a run to continue it after a cap-trip.

A run that trips a budget cap (wall / turn / no-progress) leaves its worktree
and per-run session homes intact (see `Orchestrator._cleanup_worktree`, skipped
on a resumable exit). This module persists the *rest* of the state a fresh
process needs to re-enter `_review_rounds`: the full `RunConfig`, the pinned
diff base, the working branch, the current review round, and the agent/SIM
session ids. `contremaitre run --continue <run_id>` reads it back.

The on-disk session JSONL (codex `sessions/`, claude `projects/*.jsonl`) lives
under `<run_dir>/{prefix}-{role}-home`; carrying the session id here lets the
resumed actor reattach with `resume <id>` and recover the agent's full context.
The worktree carries the file edits. Together they hold the whole run — resume
hands the agent back its own session, it does not replay turns.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonlog import write_json
from .models import ActorMode, Caps, DepsVolume, PublishMode, RunConfig

RESUME_RELNAME = "resume.json"
# Bumped when the serialized shape changes incompatibly; a mismatch refuses to
# resume rather than reconstructing a stale RunConfig from a different schema.
RESUME_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ResumeState:
    """Everything `run --continue` needs that the worktree/session homes lack."""

    config: RunConfig
    run_id: str
    base_sha: str
    branch: str
    review_round: int
    required_changes: list[str]
    agent_session: str | None
    sim_session: str | None
    turns: int


def resume_path(run_dir: Path) -> Path:
    return run_dir / RESUME_RELNAME


def has_resume_state(runs_root: Path, run_id: str) -> bool:
    return resume_path(runs_root / run_id).exists()


# ----- RunConfig (de)serialization -----
#
# `dataclasses.asdict` recurses the nested dataclasses (Caps, DepsVolume) and
# turns tuples into lists; we only have to hand-coerce the field types JSON
# can't round-trip on its own (Path, the str-enums). Reconstruction is explicit
# for those same fields and forwards every remaining primitive by name, so a new
# RunConfig field rides along for free.


def config_to_jsonable(config: RunConfig) -> dict[str, Any]:
    d = dataclasses.asdict(config)
    d["repo"] = str(config.repo)
    d["runs_root"] = str(config.runs_root)
    d["opencode_config"] = str(config.opencode_config) if config.opencode_config else None
    d["actor_mode"] = config.actor_mode.value
    d["sim_actor_mode"] = config.sim_actor_mode.value if config.sim_actor_mode else None
    d["publish_mode"] = config.publish_mode.value
    return d


def config_from_jsonable(raw: dict[str, Any]) -> RunConfig:
    d = dict(raw)
    caps = Caps(**d.pop("caps"))
    deps_raw = d.pop("deps_volume")
    deps_volume = None
    if deps_raw:
        deps_raw = dict(deps_raw)
        deps_raw["runtime_env"] = tuple(tuple(pair) for pair in deps_raw.get("runtime_env", ()))
        deps_volume = DepsVolume(**deps_raw)
    sim_mode = d.pop("sim_actor_mode")
    opencode_config = d.pop("opencode_config")
    return RunConfig(
        repo=Path(d.pop("repo")),
        runs_root=Path(d.pop("runs_root")),
        opencode_config=Path(opencode_config) if opencode_config else None,
        actor_mode=ActorMode(d.pop("actor_mode")),
        sim_actor_mode=ActorMode(sim_mode) if sim_mode else None,
        publish_mode=PublishMode(d.pop("publish_mode")),
        check_cmds=tuple(d.pop("check_cmds")),
        caps=caps,
        deps_volume=deps_volume,
        **d,
    )


# ----- ResumeState (de)serialization -----


def write_resume_state(run_dir: Path, state: ResumeState) -> None:
    write_json(
        resume_path(run_dir),
        {
            "schema_version": RESUME_SCHEMA_VERSION,
            "run_id": state.run_id,
            "base_sha": state.base_sha,
            "branch": state.branch,
            "review_round": state.review_round,
            "required_changes": list(state.required_changes),
            "agent_session": state.agent_session,
            "sim_session": state.sim_session,
            "turns": state.turns,
            "config": config_to_jsonable(state.config),
        },
    )


def load_resume_state(runs_root: Path, run_id: str) -> ResumeState:
    import json

    path = resume_path(runs_root / run_id)
    if not path.exists():
        raise ResumeError(
            f"no resume checkpoint for run {run_id!r} "
            f"(expected {path}). Only runs that tripped a budget cap are resumable."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("schema_version")
    if version != RESUME_SCHEMA_VERSION:
        raise ResumeError(
            f"resume checkpoint for {run_id!r} is schema v{version}, "
            f"this build expects v{RESUME_SCHEMA_VERSION}; cannot continue."
        )
    return ResumeState(
        config=config_from_jsonable(raw["config"]),
        run_id=raw["run_id"],
        base_sha=raw["base_sha"],
        branch=raw["branch"],
        review_round=raw["review_round"],
        required_changes=list(raw.get("required_changes", [])),
        agent_session=raw.get("agent_session"),
        sim_session=raw.get("sim_session"),
        turns=raw.get("turns", 0),
    )


class ResumeError(Exception):
    """A resume checkpoint is missing, stale, or for an unsupported runtime."""
