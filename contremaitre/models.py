"""Shared data contracts for the Contremaitre control plane.

These dataclasses are small on purpose. They are the stable seam between the
CLI, the orchestrator, and the actor adapters (fake and opencode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class State(str, Enum):
    INIT = "INIT"
    WORK = "WORK"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    NO_PR = "NO_PR"
    FAILED = "FAILED"


class ReviewVerdict(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class TerminalVerdict(str, Enum):
    READY_FOR_DRAFT_PR = "READY_FOR_DRAFT_PR"
    NO_PR_CHANGES_REQUESTED = "NO_PR_CHANGES_REQUESTED"
    NO_PR_NEEDS_HUMAN = "NO_PR_NEEDS_HUMAN"
    FAILED_INFRA = "FAILED_INFRA"


class ActorMode(str, Enum):
    FAKE = "fake"
    OPENCODE = "opencode"


class PublishMode(str, Enum):
    STUB = "stub"
    GH = "gh"


@dataclass(frozen=True)
class DepsVolume:
    """Per-run handle to a populated docker volume holding cached deps.

    `name` is the volume name; `mount_path` is the relative path inside
    the repo where the ecosystem expects its deps (`node_modules`,
    `.venv`, `.cargo-cache`, `.go-mod-cache`). Mounted at `/app/{mount_path}`
    in agent/sim/check containers. `runtime_env` carries environment
    variables those containers need so the ecosystem tool finds the
    cache (e.g. `VIRTUAL_ENV=/app/.venv` for uv/poetry, `GOPATH=…` for
    go, `CARGO_HOME=…` for cargo).
    """

    name: str
    mount_path: str
    runtime_env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Caps:
    max_turns: int = 30
    max_wall_minutes: int = 180
    max_cost_usd: float = 30.0
    no_progress_turns: int = 5
    malformed_verdict_retries: int = 2
    max_review_rounds: int = 3


@dataclass(frozen=True)
class RunConfig:
    repo: Path
    base: str
    runs_root: Path
    run_slug: str
    branch_prefix: str = "refactor"
    fork: str | None = None
    upstream: str | None = None
    agent_model: str = "openrouter/deepseek/deepseek-v4-flash"
    sim_model: str = "openrouter/deepseek/deepseek-v4-flash"
    extra_reviewer_model: str | None = None
    # Local CLI reviewer run AFTER the Draft PR is published, posting a code
    # review comment on the PR using the operator's subscription-bound CLI
    # (`claude` or `codex`). Optional. `"none"` skips the step entirely;
    # the orchestrator's hook is a no-op in that case.
    cli_reviewer: str = "none"
    check_cmds: tuple[str, ...] = ()
    actor_mode: ActorMode = ActorMode.FAKE
    sim_scenario: str = "approved"
    extra_reviewer_scenario: str = "approved"
    agent_scenario: str = "normal"
    publish_mode: PublishMode = PublishMode.STUB
    keep_worktree: bool = False
    simulate_drift_after_approval: bool = False
    docker_image: str = "contremaitre-agent:latest"
    # Docker volume + mount metadata for the ecosystem's deps cache.
    # Keyed on lockfile hash; populated once per lockhash by
    # runtime_image.ensure_deps_volume, then cloned per-run. None when
    # the target has no recognized lockfile (deps unavailable to checks).
    deps_volume: DepsVolume | None = None
    opencode_config: Path | None = None
    openrouter_env_var: str = "OPENROUTER_API_KEY"
    container_user: str | None = None
    docker_network: str | None = None
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    skip_preflight: bool = False
    allow_open_egress: bool = False
    skip_openrouter_key_check: bool = False
    allow_unlimited_openrouter_key: bool = False
    openrouter_key_url: str = "https://openrouter.ai/api/v1/key"
    agent_timeout_seconds: int = 1800
    sim_timeout_seconds: int = 900
    gh_repo: str | None = None
    pr_title: str | None = None
    pr_body: str | None = None
    caps: Caps = field(default_factory=Caps)


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    run_dir: Path
    worktree: Path
    initial_prompt: Path
    raw_export: Path
    sim_raw_export: Path
    extra_reviewer_raw_export: Path
    # Post-publish CLI reviewer streams (`claude -p` or `codex exec`). Only
    # one of the two is written per run; the unused path is registered for
    # readers to probe.
    claude_review_raw_export: Path
    codex_review_raw_export: Path
    transcript: Path
    timeline: Path
    trajectory: Path
    stats: Path
    git_log: Path
    test_runs: Path
    review_cycles: Path
    worktree_state: Path
    guardrail_events: Path
    pr_json: Path
    eval_dir: Path
    pr_eval: Path
    pr_eval_md: Path
    checks_report: Path
    settled_diff_report: Path
    architecture_delta_report: Path
    trajectory_report: Path
    judge_attempts: Path
    cost_report: Path
    preflight_report: Path
    recoveries: Path
    subagents_dir: Path
    extracted_files_dir: Path
    flow_use_report: Path


@dataclass(frozen=True)
class ParsedVerdict:
    verdict: ReviewVerdict
    confidence: float
    required_changes: list[str]
    checks_performed: list[str]
    summary: str
    raw: str


@dataclass(frozen=True)
class RunResult:
    run_id: str
    terminal_state: State
    verdict: TerminalVerdict
    run_dir: Path
    worktree: Path
    pr_created: bool
    reason: str
