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
    agent_model: str = "fake-agent"
    sim_model: str = "fake-sim"
    check_cmds: tuple[str, ...] = ()
    actor_mode: ActorMode = ActorMode.FAKE
    sim_scenario: str = "approved"
    agent_scenario: str = "normal"
    publish_mode: PublishMode = PublishMode.STUB
    keep_worktree: bool = False
    simulate_drift_after_approval: bool = False
    docker_image: str = "arch001-eval-app:latest"
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
    allow_openrouter_limit_above_cap: bool = False
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
