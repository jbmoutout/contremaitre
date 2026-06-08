"""Shared data contracts for the Contremaitre control plane.

These dataclasses are small on purpose. They are the stable seam between the
CLI, the orchestrator, and the actor adapters (fake and opencode).
"""

from __future__ import annotations

from collections.abc import Iterable
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


class CliReviewVerdict(str, Enum):
    """The post-publish CLI reviewer's verdict, and the single home for its
    severity ordering.

    Every consumer (the commit-status projection, the TUI footer glyph, the
    eval canary score, the viewer tier) routes through this enum rather than
    re-tabling the order. Severity runs ``LOOKS_GOOD < NEEDS_ATTENTION <
    MUST_FIX``; the eval **quality** score is the inverse axis, *derived* from
    the rank so it can never drift from the severity order.
    """

    MUST_FIX = "MUST_FIX"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    LOOKS_GOOD = "LOOKS_GOOD"

    @property
    def rank(self) -> int:
        """Severity rank, low → high. ``MUST_FIX`` is worst."""

        return _CLI_VERDICT_SEVERITY.index(self)

    @property
    def quality_score(self) -> float:
        """Quality score in ``[0, 1]``, high = good — the inverse of severity.

        Derived, never tabled: ``LOOKS_GOOD`` → 1.0, ``NEEDS_ATTENTION`` → 0.5,
        ``MUST_FIX`` → 0.0. Feeds the eval canary's ``cli_review_score``.
        """

        return 1.0 - self.rank / (len(_CLI_VERDICT_SEVERITY) - 1)

    @classmethod
    def coerce(cls, value: object) -> CliReviewVerdict | None:
        """Best-effort ``str | enum | None`` → member. Never raises.

        Returns ``None`` for anything that isn't one of the three keys, so
        callers can fold unparseable / missing verdicts uniformly.
        """

        if isinstance(value, cls):
            return value
        try:
            return cls(value)  # type: ignore[arg-type]
        except ValueError:
            return None

    @classmethod
    def worst_of(cls, verdicts: Iterable[object]) -> CliReviewVerdict | None:
        """Highest-severity verdict among ``verdicts``.

        Ignores unparseable / ``None`` entries (a reviewer that crashed or
        drifted from the format). Returns ``None`` only when nothing parseable
        is present. Used for worst-of-N across multiple reviewers
        (``cli_reviewer="both"``) — any ``MUST_FIX`` wins.
        """

        members = [m for m in (cls.coerce(v) for v in verdicts) if m is not None]
        if not members:
            return None
        return max(members, key=lambda m: m.rank)

    @classmethod
    def parse(cls, text: str | None) -> CliReviewVerdict | None:
        """Extract the verdict key from the head of a CLI-review markdown.

        The prompt pins ``<glyph> <KEY> — <justification>`` on line 1, but we
        scan the first 10 lines defensively: the three keys are mutually
        disjoint substrings, so a containment check is unambiguous even if the
        agent prepends stray lines. (This 10-line window is wider than the old
        ``cli_reviewer.parse_verdict`` 5-line scan — a deliberate, strictly-more-
        lenient widening; lines 6–10 only ever matter for malformed output.)

        Returns ``None`` for both *no text* and *text without a key* — callers
        that need to tell those apart keep that branch at their own edge.
        """

        if not text:
            return None
        for line in text.lstrip().splitlines()[:10]:
            for member in _CLI_VERDICT_SEVERITY:
                if member.value in line:
                    return member
        return None


# Severity order, low → high. The single source of truth that ``rank`` and
# ``quality_score`` derive from.
_CLI_VERDICT_SEVERITY: tuple[CliReviewVerdict, ...] = (
    CliReviewVerdict.LOOKS_GOOD,
    CliReviewVerdict.NEEDS_ATTENTION,
    CliReviewVerdict.MUST_FIX,
)


class TerminalVerdict(str, Enum):
    READY_FOR_DRAFT_PR = "READY_FOR_DRAFT_PR"
    NO_PR_CHANGES_REQUESTED = "NO_PR_CHANGES_REQUESTED"
    NO_PR_NEEDS_HUMAN = "NO_PR_NEEDS_HUMAN"
    FAILED_INFRA = "FAILED_INFRA"
    # Distinct from FAILED_INFRA: the run reached the provider's free-tier
    # quota (e.g. opencode-zen `FreeUsageLimitError`). Not an infra problem
    # the operator can fix locally — wait for the per-day/per-hour reset or
    # switch to a paid model. Eval canary aborts the n=3 batch on this
    # verdict since the remaining runs would hit the same limit.
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


class ActorMode(str, Enum):
    FAKE = "fake"
    OPENCODE = "opencode"
    # Drive a frontier CLI (currently codex) headless inside the per-run
    # container AS the agent/SIM, on the operator's subscription. See
    # cli_actor.CliActorRunner for the auth + egress-lock handling.
    CLI = "cli"


def is_zen_model(model: str) -> bool:
    """True for a free OpenCode Zen model (`opencode/...`), which the opencode
    binary reaches via built-in access — no `OPENROUTER_API_KEY` needed.

    The single source of truth for "does this opencode model need the key", shared
    by preflight (`_check_openrouter_key`) and the runner (`build_docker_command`)
    so a green preflight can't turn into a runtime "key required" failure. Any
    other slug (OpenRouter, incl. `...:free`) goes through the keyed API.
    """

    return bool(model) and model.startswith("opencode/")


def role_model_label(
    *,
    actor_mode,
    opencode_model: str,
    codex_model: str,
    codex_effort: str,
    cli_tool: str = "codex",
    claude_model: str = "",
    claude_effort: str = "high",
) -> str:
    """Human label for a role's model. A CLI role shows its CLI-native model
    + effort (codex or claude, per `cli_tool`); any other runtime shows the
    opencode/OpenRouter slug.

    Used by the launch recap, the TUI header, and `stats.json` so a CLI role
    never displays the (ignored) opencode slug. Accepts `actor_mode` as either an
    `ActorMode` or its string value. `cli_tool` defaults to "codex" so existing
    callers (and the codex path) are unchanged.
    """

    mode = actor_mode.value if isinstance(actor_mode, ActorMode) else actor_mode
    if mode == ActorMode.CLI.value:
        if cli_tool == "claude":
            return f"{claude_model or 'claude default'} (claude, effort={claude_effort})"
        return f"{codex_model} (codex, effort={codex_effort})"
    return opencode_model


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
    # Which frontier CLI to drive AS the agent/SIM when actor_mode is CLI:
    # "codex" (ChatGPT subscription) or "claude" (Claude subscription, via a
    # headless CLAUDE_CODE_OAUTH_TOKEN). See cli_actor.CliDriver.
    cli_tool: str = "codex"
    # codex-native model + reasoning effort for a codex role. agent_model/
    # sim_model are opencode-namespaced and codex rejects them, so a codex role
    # uses `codex_model` (the per-role model wins only if it is codex-native).
    # `codex_effort` is pinned via `-c model_reasoning_effort=<effort>`.
    codex_model: str = "gpt-5.5"
    codex_effort: str = "high"
    # claude-native model + effort for a claude CLI role (cli_tool="claude").
    # `claude_model` empty → claude uses the operator's ~/.claude account default
    # (don't hardcode a model). `claude_effort` maps to claude's `--effort`
    # (low|medium|high|max), set every turn (resume-safe), mirroring codex_effort.
    claude_model: str = ""
    claude_effort: str = "high"
    # Per-role actor override: the agent uses `actor_mode`; when this is set the
    # SIM uses it instead, so a run can MIX runtimes (e.g. a codex agent with an
    # opencode SIM, or vice versa). None means the SIM shares `actor_mode`.
    sim_actor_mode: ActorMode | None = None
    # Per-role CLI tool override (the cli_tool analog of sim_actor_mode): when set
    # and the SIM runs the CLI runtime, the SIM drives this tool instead of
    # `cli_tool` — so a run can mix the two CLI tools (codex agent + claude SIM,
    # or the reverse). None means the SIM shares `cli_tool`.
    sim_cli_tool: str | None = None
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
    sim_timeout_seconds: int = 1500
    # Kill an opencode subprocess if neither its stdout (raw event
    # stream) nor opencode's internal log file has grown for this many
    # seconds. Catches the "silent agent" pathology where the model
    # goes dark mid-turn and otherwise sits until the full
    # {agent,sim}_timeout fires. Watching the internal log too avoids
    # false-positives when a Task subagent is grinding silently
    # (subagent events don't surface to the parent's stdout). Set to 0
    # to disable. Threshold sits above observed max inter-step gaps on
    # healthy free-endpoint runs (~190s) with headroom.
    opencode_stdout_stall_seconds: int = 300
    # Re-invoke opencode this many times when a turn raises a transient
    # provider error (e.g. upstream 5xx surfaced as `Provider returned
    # error`). Quota errors, stalls, and wall-clock timeouts are NOT
    # retried — those are terminal for the run. 0 disables retry.
    opencode_transient_retry_max: int = 1
    opencode_transient_retry_backoff_seconds: int = 30
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
