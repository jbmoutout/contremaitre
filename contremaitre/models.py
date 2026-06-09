"""Shared data contracts for the Contremaitre control plane.

These dataclasses are small on purpose. They are the stable seam between the
CLI, the orchestrator, and the actor adapters (fake and opencode).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any


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
    MUST_FIX = "MUST_FIX"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    LOOKS_GOOD = "LOOKS_GOOD"


class TerminalVerdict(str, Enum):
    READY_FOR_DRAFT_PR = "READY_FOR_DRAFT_PR"
    # PR created but CLI reviewer never reached LOOKS_GOOD in max_cli_review_rounds.
    # Yellow: the draft is published; a human should review before merging.
    PR_NEEDS_HUMAN = "PR_NEEDS_HUMAN"
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


# The fixed shape the *legacy* `role_model_label` emitted for a CLI role —
# `<model> (codex|claude, effort=<e>)`. Kept ONLY so `ModelSpec.from_record`
# can read run dirs written before model identity became a structured record.
# This is the one and only place that parse contract survives; no other module
# may decode it. (Removing it would make pre-migration run dirs unreadable.)
_CLI_LABEL_RE = re.compile(
    r"^(?P<model>.+?)\s*\((?P<runtime>codex|claude),\s*effort=(?P<effort>[^)]*)\)\s*$"
)


def resolved_model_from_events(events: list[dict[str, Any]]) -> str | None:
    """The model a claude run reports in its `system/init` stream event, or None.

    claude echoes the model it *actually* ran (even when the requested model was
    the ~/.claude account default) in `system/init`. codex carries no model in
    its stream and opencode's requested slug already *is* the resolved model, so
    this returns None for them and the spec falls back to `requested`.
    """

    for e in events:
        if e.get("type") == "system" and e.get("subtype") == "init":
            model = e.get("model")
            if isinstance(model, str) and model:
                return model
    return None


@dataclass(frozen=True)
class ModelSpec:
    """Canonical model identity for one role (agent / SIM).

    Stored fields are atomic and are never re-parsed; the human display string
    (`display`) and the grouping key (`canonical`) are *derived* and never read
    back as a source of truth. One factory (`build` / `for_role`) constructs it
    from config; one classmethod (`from_record`) reads it back and is the only
    place that tolerates a legacy on-disk label string.
    """

    runtime: str  # "opencode" | "codex" | "claude" | "fake" — how the role was driven
    requested: str  # exactly what config asked, verbatim (slug, codex model, or "")
    effort: str | None = None  # CLI roles only; None for opencode/fake
    resolved: str | None = None  # what the stream said it ran; None when unknown
    provider: str | None = None  # "opencode" | "openrouter" for a slug; None for CLI

    def canonical(self) -> tuple[str, str]:
        """Stable `(name, runtime)` grouping key. Prefers `resolved` so two
        account-default claude runs that ran different models don't collide."""

        base = self.resolved or self.requested or "?"
        name = base.rsplit("/", 1)[-1].replace(" ", "-") or "?"
        return name, self.runtime

    def display(self) -> str:
        """The one human string. No consumer ever parses it back."""

        base = self.resolved or self.requested or ""
        name = base.rsplit("/", 1)[-1] if base else ""
        if not name:
            if self.runtime in ("codex", "claude"):
                name = "default"  # claude ~/.claude account default, pre-resolution
            else:
                return "?"
        if name == "?":
            return "?"
        prefix = self.provider or self.runtime
        if self.effort:
            return f"{prefix}/{name} {self.effort}"
        return f"{prefix}/{name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "requested": self.requested,
            "effort": self.effort,
            "resolved": self.resolved,
            "provider": self.provider,
        }

    def with_resolved(self, resolved: str | None) -> "ModelSpec":
        """A copy with `resolved` filled from the stream, or self when there is
        nothing new to record."""

        if not resolved or resolved == self.resolved:
            return self
        return replace(self, resolved=resolved)

    @classmethod
    def build(
        cls,
        *,
        mode,
        opencode_model: str,
        codex_model: str = "gpt-5.5",
        codex_effort: str = "high",
        cli_tool: str = "codex",
        claude_model: str = "",
        claude_effort: str = "high",
    ) -> "ModelSpec":
        """The single atomic factory. A CLI role records its CLI-native model +
        effort; any other runtime records the opencode/OpenRouter slug + its
        provider. Accepts `mode` as an `ActorMode` or its string value."""

        m = mode.value if isinstance(mode, ActorMode) else mode
        if m == ActorMode.CLI.value:
            if cli_tool == "claude":
                return cls(runtime="claude", requested=claude_model, effort=claude_effort or None)
            return cls(runtime="codex", requested=codex_model, effort=codex_effort or None)
        runtime = "fake" if m == ActorMode.FAKE.value else "opencode"
        provider = "opencode" if is_zen_model(opencode_model) else "openrouter"
        return cls(runtime=runtime, requested=opencode_model, provider=provider)

    @classmethod
    def for_role(cls, config, role: str) -> "ModelSpec":
        """Build the spec for `role` ∈ {"agent","sim"} from a `RunConfig`,
        applying the SIM's per-role actor/tool overrides."""

        if role == "agent":
            return cls.build(
                mode=config.actor_mode,
                opencode_model=config.agent_model,
                codex_model=config.codex_model,
                codex_effort=config.codex_effort,
                cli_tool=config.cli_tool,
                claude_model=config.claude_model,
                claude_effort=config.claude_effort,
            )
        if role == "sim":
            return cls.build(
                mode=config.sim_actor_mode or config.actor_mode,
                opencode_model=config.sim_model,
                codex_model=config.codex_model,
                codex_effort=config.codex_effort,
                cli_tool=config.sim_cli_tool or config.cli_tool,
                claude_model=config.claude_model,
                claude_effort=config.claude_effort,
            )
        raise ValueError(f"unknown role: {role!r}")

    @classmethod
    def from_record(cls, obj) -> "ModelSpec":
        """Read identity back from disk. Accepts the canonical dict OR a legacy
        label/slug string — the only reader that tolerates the old shape, so no
        other module ever has to know it existed."""

        if isinstance(obj, dict):
            # Atomic record: read every stored field verbatim. `requested` is
            # exactly what config asked, including "" for a claude account
            # default — never coerce it (that would lose the canonical value and
            # break the round-trip). An empty dict still falls back to "?" via
            # the `or` (there is no persisted value to preserve).
            requested = obj.get("requested")
            return cls(
                runtime=obj.get("runtime") or "opencode",
                requested=requested if isinstance(requested, str) else "?",
                effort=obj.get("effort"),
                resolved=obj.get("resolved"),
                provider=obj.get("provider"),
            )
        if isinstance(obj, str) and obj and obj != "?":
            m = _CLI_LABEL_RE.match(obj)
            if m:
                return cls(
                    runtime=m.group("runtime"),
                    requested=m.group("model").strip(),
                    effort=m.group("effort") or None,
                )
            provider = "opencode" if is_zen_model(obj) else "openrouter"
            return cls(runtime="opencode", requested=obj, provider=provider)
        return cls(runtime="opencode", requested="?")


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
    # CLI reviewer drives a post-PR revision loop: reviews the PR, posts a
    # comment, and if MUST_FIX re-enters the agent (fresh Docker session) until
    # LOOKS_GOOD or max_cli_review_rounds exhausted. `"none"` skips entirely.
    cli_reviewer: str = "none"
    max_cli_review_rounds: int = 3
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
