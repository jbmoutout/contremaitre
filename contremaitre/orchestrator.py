"""Orchestration state machine.

The orchestrator is deliberately boring Python: it owns worktree setup, caps,
logs, strict review parsing, hard gates, and the publisher boundary. Actor
processes can be clever later; this module should remain deterministic.

Shape:
    INIT  -> WORK  -> REVIEW  -> APPROVED   (terminal: PR opened)
                              -> WORK       (revision round, up to max_review_rounds)
                              -> NO_PR      (terminal: CHANGES_REQUESTED exhausted, NEEDS_HUMAN,
                                              malformed verdict, cap trip, no IMPLEMENTATION_COMPLETE)
                                FAILED      (terminal: infrastructure error)

WORK is one multi-turn opencode session: the agent runs the
`improve-codebase-architecture` skill end-to-end while the SIM (read-only
tooled SWE) responds turn by turn. The loop terminates when the agent writes
`.contremaitre/IMPLEMENTATION_COMPLETE` in the worktree.

The hand-rolled turn loop is a self-contained copy — Contremaitre does not
import any external orchestration substrate at runtime.
"""

from __future__ import annotations

import shutil
import signal
import time
import dataclasses
from dataclasses import dataclass
from pathlib import Path

from . import events, prompts
from .actors import ActorError, ActorRunner, make_actor_runner
from .checks import CheckResult, run_checks
from .runtime_image import DepsInstallError, clone_deps_volume_for_run, ensure_deps_volume
from .costs import estimate_recorded_cost_usd, sum_token_usage
from .diffscan import DiffScanResult, scan_diff
from .evaluator import (
    hard_gate_payload,
    sim_review_summary,
    write_eval_reports,
)
from .extract import extract_run_artifacts
from .viewer import build_viewer
from .git_utils import GitRepo
from .jsonlog import append_jsonl, write_json
from .manifest import build_manifest
from .models import (
    ActorMode,
    ParsedVerdict,
    ReviewVerdict,
    RunConfig,
    RunResult,
    State,
    TerminalVerdict,
    role_model_label,
)
from .paths import build_run_paths, new_run_id, validate_slug
from .preflight import enforce_preflight
from .publisher import (
    PublishOutcome,
    PublishOutcomeKind,
    make_publisher,
    record_publication,
)
from .scaffolds import (
    IMPLEMENTATION_COMPLETE_RELPATH,
    SETTLED_RELPATH,
    derive_commit_message,
)
from .verdicts import VerdictParseError, diff_hash, parse_sim_verdict, write_review_diff

# Paths held out of the host commit. `.contremaitre/` / `opencode.json` are
# orchestration-internal; the rest are conventionally-gitignored build output
# that some agents produce as a verification step (the worktree may not carry
# the upstream .gitignore for all of them, so we belt-and-suspenders).
_HOST_COMMIT_EXCLUDES = (
    ".contremaitre",
    "opencode.json",
    "dist",
    "build",
    "out",
    ".next",
    "__pycache__",
)


@dataclass(frozen=True)
class _WorktreeSnapshot:
    """One pair of git read-only queries shared across the two per-turn records.

    Without this, every turn ran `git status --porcelain` and `git diff --stat`
    twice — once for the worktree-state log, once for the no-progress key.
    """

    status: str
    diff_stat: str


class Orchestrator:
    def __init__(self, config: RunConfig):
        self.config = config
        self.run_id = new_run_id(config.run_slug)
        self.paths = build_run_paths(config.runs_root, self.run_id)
        self.started = time.monotonic()
        self.turns = 0
        self.trajectory: list[dict[str, object]] = []
        self.no_progress_streak = 0
        self._last_progress_key: tuple[str, str] | None = None
        # SHA of `origin/<base>` captured at worktree creation, right
        # after `git fetch origin <base>` and before any remote rewiring.
        # Used as the diff base by all later operations — pinning to a
        # commit instead of a ref name survives the `git remote remove
        # origin && git remote add origin <fork>` swap (which deletes
        # `refs/remotes/origin/<base>`).
        self._base_sha: str = ""
        # Last-round SIM verdict. Stashed by _run_review so _write_eval can
        # build the `sim_review` payload without threading it through the
        # publication-gate path.
        self._last_sim_parsed: ParsedVerdict | None = None
        self._last_cli_review_reason: str | None = None

    @property
    def _diff_base(self) -> str:
        """SHA against which all diff operations compute. Pinned at fetch time."""

        return self._base_sha or self.config.base

    # ----- top-level run -----

    def run(self) -> RunResult:
        self._prepare_run_dir()
        repo = GitRepo(self.config.repo, self.paths.git_log)
        branch = f"{validate_slug(self.config.branch_prefix, 'branch prefix')}/{self.run_id}"

        # Signal handler for operator-initiated death. SIGTERM is caught so
        # the in-flight container is `docker stop`'d (by label), the final
        # stats are written, and the artifact extractor still runs. SIGHUP
        # is intentionally not caught: detached + labeled containers are
        # recoverable on the next run via `cleanup --deps` / a label scan,
        # so we don't pay the complexity of two handlers for a rarer path.
        # SIGKILL remains uncatchable (intentional kernel-level kill).
        prior_term = signal.getsignal(signal.SIGTERM)

        def _on_sigterm(_signum, _frame):
            self._stop_run_containers()
            append_jsonl(
                self.paths.recoveries,
                {"kind": events.SIGTERM_EMERGENCY_WRITE, "turns": self.turns, "signal": "sigterm"},
            )
            self._write_final_stats(
                State.FAILED, TerminalVerdict.FAILED_INFRA, "killed_via_sigterm"
            )
            self._extract_artifacts_safely()
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, _on_sigterm)
        try:
            enforce_preflight(self.config, self.paths)
            self._transition(State.INIT, "creating worktree")
            self._create_worktree(repo, branch)
            self._ensure_pristine_deps_volume()
            self._provision_run_deps_volume()
            worktree_git = GitRepo(self.paths.worktree, self.paths.git_log)
            actor = make_actor_runner(config=self.config, paths=self.paths)

            return self._review_rounds(actor=actor, worktree_git=worktree_git, branch=branch)
        except Exception as exc:
            failure_kind = getattr(exc, "kind", None) if isinstance(exc, ActorError) else None
            self._emit(events.INFRA_FAILURE, error=repr(exc), kind=failure_kind)
            # Provider free-tier quota (opencode-zen `FreeUsageLimitError`) is
            # categorically not "infra" — the operator can't fix Docker /
            # network / etc. to make it work; they have to wait for the quota
            # reset or switch to a paid model. Distinct verdict so downstream
            # readers (TUI, eval canary) can act on it.
            if failure_kind == events.PROVIDER_QUOTA_EXHAUSTED:
                verdict = TerminalVerdict.QUOTA_EXHAUSTED
                reason_prefix = "QUOTA_EXHAUSTED"
            else:
                verdict = TerminalVerdict.FAILED_INFRA
                reason_prefix = "FAILED_INFRA"
            record_publication(
                self.paths,
                PublishOutcome(
                    kind=PublishOutcomeKind.NO_PR,
                    base=self.config.base,
                    publish_mode=self.config.publish_mode,
                    reason=f"{reason_prefix}: {exc}",
                    branch=branch,
                    dry_run=True,
                ),
            )
            self._write_final_stats(State.FAILED, verdict, str(exc))
            return RunResult(
                run_id=self.run_id,
                terminal_state=State.FAILED,
                verdict=verdict,
                run_dir=self.paths.run_dir,
                worktree=self.paths.worktree,
                pr_created=False,
                reason=str(exc),
            )
        finally:
            self._extract_artifacts_safely()
            if not self.config.keep_worktree:
                self._cleanup_worktree()
            signal.signal(signal.SIGTERM, prior_term)

    def _extract_artifacts_safely(self) -> None:
        """Run the subagent + files extractor, then build the viewer.

        Both are observability — failures are recorded and swallowed so a
        broken extractor or viewer can't mask the real run outcome. Viewer
        runs second because it reads the extracted files.
        """

        try:
            extract_run_artifacts(self.paths)
        except Exception as exc:
            append_jsonl(self.paths.recoveries, {"kind": events.EXTRACT_FAILED, "error": repr(exc)})
        try:
            build_viewer(self.paths)
        except Exception as exc:
            append_jsonl(
                self.paths.recoveries, {"kind": events.VIEWER_BUILD_FAILED, "error": repr(exc)}
            )

    def _review_rounds(
        self, *, actor: ActorRunner, worktree_git: GitRepo, branch: str
    ) -> RunResult:
        last_required_changes: list[str] = []
        last_parsed: ParsedVerdict | None = None
        last_sim: ParsedVerdict | None = None

        for review_round in range(1, self.config.caps.max_review_rounds + 1):
            self._transition(State.WORK, f"WORK session round {review_round}")
            outcome = self._run_work_session(
                actor=actor,
                review_round=review_round,
                required_changes=last_required_changes,
                sim_parsed=last_sim,
            )
            self._emit(events.WORK_SESSION_END, round=review_round, outcome=outcome)

            if not self._implementation_complete():
                return self._terminal_no_pr(
                    TerminalVerdict.NO_PR_NEEDS_HUMAN,
                    f"WORK ended without IMPLEMENTATION_COMPLETE ({outcome})",
                    branch=branch,
                )
            if self._cap_tripped():
                return self._terminal_no_pr(
                    TerminalVerdict.NO_PR_NEEDS_HUMAN,
                    "cap tripped during WORK",
                    branch=branch,
                )

            settled_file = self.paths.worktree / SETTLED_RELPATH
            if not settled_file.exists():
                return self._terminal_no_pr(
                    TerminalVerdict.NO_PR_NEEDS_HUMAN,
                    "SETTLED_DESIGN.md was not written",
                    branch=branch,
                )

            self._commit_agent_changes(worktree_git)
            checks = run_checks(
                config=self.config,
                paths=self.paths,
                emit_event=self._emit,
            )
            self._record_worktree_state(worktree_git, f"after-checks-round{review_round}")

            review_result = self._run_review(
                actor=actor,
                worktree_git=worktree_git,
                settled_file=settled_file,
                review_round=review_round,
            )
            if review_result is None:
                return self._terminal_no_pr(
                    TerminalVerdict.NO_PR_NEEDS_HUMAN,
                    "SIM verdict malformed after retries",
                    branch=branch,
                    checks=checks,
                )
            parsed, current_hash = review_result

            if parsed.verdict == ReviewVerdict.NEEDS_HUMAN:
                return self._terminal_no_pr(
                    TerminalVerdict.NO_PR_NEEDS_HUMAN,
                    parsed.summary,
                    branch=branch,
                    checks=checks,
                    sim_verdict=parsed,
                )

            if parsed.verdict == ReviewVerdict.CHANGES_REQUESTED:
                last_required_changes = list(parsed.required_changes)
                last_parsed = parsed
                last_sim = self._last_sim_parsed
                self._clear_implementation_complete()
                self._emit(
                    events.REVISION_REQUESTED,
                    round=review_round,
                    required_changes=last_required_changes,
                )
                continue

            # APPROVED — drift check + hard gates + publish + CLI review loop
            return self._publish_or_block(
                worktree_git=worktree_git,
                branch=branch,
                checks=checks,
                parsed=parsed,
                approved_hash=current_hash,
                actor=actor,
            )

        # Max review rounds exhausted while still CHANGES_REQUESTED.
        summary = last_parsed.summary if last_parsed else "no SIM verdict captured"
        return self._terminal_no_pr(
            TerminalVerdict.NO_PR_CHANGES_REQUESTED,
            f"max review rounds exhausted: {summary}",
            branch=branch,
            sim_verdict=last_parsed,
        )

    # ----- WORK multi-turn loop -----

    def _run_work_session(
        self,
        *,
        actor: ActorRunner,
        review_round: int,
        required_changes: list[str],
        sim_parsed: ParsedVerdict | None,
    ) -> str:
        """Run the multi-turn WORK session until terminal or cap.

        Returns a short reason string describing why the loop exited.
        """

        if review_round == 1:
            first_message = prompts.INITIAL_PROMPT
        else:
            first_message = prompts.revision_followup(
                required_changes,
                sim=sim_parsed,
            )

        agent_text = self._agent_turn(actor, first_message)
        if self._implementation_complete():
            return "implementation_complete_turn_1"
        if self._cap_tripped():
            return "cap_tripped_turn_1"

        sim_first = True
        for turn in range(2, self.config.caps.max_turns + 1):
            sim_message = (
                prompts.sim_first_turn(agent_text)
                if sim_first
                else prompts.sim_subsequent_turn(agent_text)
            )
            sim_text = self._sim_turn(actor, sim_message)
            sim_first = False
            if self._implementation_complete():
                return f"implementation_complete_after_sim_turn_{turn}"
            if self._cap_tripped():
                return f"cap_tripped_after_sim_turn_{turn}"

            agent_text = self._agent_turn(actor, sim_text)
            if self._implementation_complete():
                return f"implementation_complete_turn_{turn}"
            if self._cap_tripped():
                return f"cap_tripped_after_agent_turn_{turn}"

        return "max_turns"

    def _agent_turn(self, actor: ActorRunner, message: str) -> str:
        # Actor owns raw_export + transcript writes for its own turn.
        self._before_turn()
        output = actor.agent_turn(message)
        text = output.text
        worktree_git = GitRepo(self.paths.worktree, self.paths.git_log)
        label = f"after-agent-turn-{self.turns}"
        snapshot = self._record_worktree_state(worktree_git, label)
        self._record_progress(snapshot, label, text)
        return text

    def _sim_turn(self, actor: ActorRunner, message: str) -> str:
        # Actor owns raw_export + transcript writes for its own turn.
        self._before_turn()
        output = actor.sim_turn(message)
        return output.text

    # ----- review pass -----

    def _run_review(
        self,
        *,
        actor: ActorRunner,
        worktree_git: GitRepo,
        settled_file: Path,
        review_round: int,
    ) -> tuple[ParsedVerdict, str] | None:
        self._transition(State.REVIEW, f"SIM review round {review_round}")
        current_hash = diff_hash(worktree_git, self._diff_base)
        diff_file = self.paths.run_dir / f"review_diff_round{review_round}.diff"
        write_review_diff(worktree_git, self._diff_base, diff_file)

        sim_parsed = self._run_one_reviewer(
            actor=actor,
            diff_file=diff_file,
            settled_file=settled_file,
            review_round=review_round,
            scenario=self.config.sim_scenario,
        )
        if sim_parsed is None:
            return None
        self._record_review_cycle(review_round, current_hash, sim_parsed, reviewer="sim")
        self._last_sim_parsed = sim_parsed
        return sim_parsed, current_hash

    def _run_one_reviewer(
        self,
        *,
        actor: ActorRunner,
        diff_file: Path,
        settled_file: Path,
        review_round: int,
        scenario: str,
    ) -> ParsedVerdict | None:
        """Run the SIM through the malformed-verdict retry loop."""

        parsed: ParsedVerdict | None = None
        for attempt in range(1, self.config.caps.malformed_verdict_retries + 2):
            self._before_turn()
            output = actor.sim_review(
                diff_file=diff_file,
                settled_file=settled_file,
                scenario=scenario,
                attempt=attempt,
            )
            try:
                parsed = parse_sim_verdict(output.text)
                break
            except VerdictParseError as exc:
                self._emit(
                    events.MALFORMED_VERDICT,
                    round=review_round,
                    attempt=attempt,
                    reviewer="sim",
                    error=str(exc),
                )
        return parsed

    def _record_review_cycle(
        self,
        review_round: int,
        current_hash: str,
        parsed: ParsedVerdict,
        *,
        reviewer: str,
    ) -> None:
        append_jsonl(
            self.paths.review_cycles,
            {
                "round": review_round,
                "reviewer": reviewer,
                "diff_hash": current_hash,
                "verdict": parsed.verdict.value,
                "confidence": parsed.confidence,
                "required_changes": parsed.required_changes,
                "checks_performed": parsed.checks_performed,
                "summary": parsed.summary,
            },
        )
        self._emit(
            events.REVIEW_VERDICT,
            round=review_round,
            reviewer=reviewer,
            verdict=parsed.verdict.value,
            confidence=parsed.confidence,
            summary=parsed.summary[:200] if parsed.summary else "",
            required_changes=len(parsed.required_changes),
        )

    # ----- post-publish CLI review loop -----

    def _run_cli_review_loop(
        self,
        *,
        worktree_git: GitRepo,
        branch: str,
        outcome: PublishOutcome,
        actor,
    ) -> TerminalVerdict:
        """Post-PR CLI review loop: reviewer → revision → reviewer, up to max rounds.

        Each round runs every configured CLI tool and posts a PR comment. The
        loop exits with READY_FOR_DRAFT_PR when all tools return LOOKS_GOOD, or
        with PR_NEEDS_HUMAN when max_cli_review_rounds exhausts without LOOKS_GOOD.

        Any verdict other than LOOKS_GOOD (including NEEDS_ATTENTION) triggers
        an agent revision on the same branch before the next round. The reviewer
        always runs read-only in Docker; the agent revision uses the existing
        session (resumes from WORK context).

        GitHub access is host-owned: the host builds a read-only `/review`
        bundle for the container, then posts the returned markdown as a PR
        comment after the reviewer exits.
        """

        from . import cli_reviewer as _cli_reviewer

        max_rounds = self.config.max_cli_review_rounds
        tools = list(_cli_reviewer.expand_choice(self.config.cli_reviewer))

        if not tools or not outcome.url:
            self._last_cli_review_reason = None
            return TerminalVerdict.READY_FOR_DRAFT_PR

        last_round_verdicts: list[tuple[str, str | None]] = []
        self._last_cli_review_reason = "post-publish CLI review did not reach LOOKS_GOOD"

        for cli_round in range(1, max_rounds + 1):
            self._transition(State.APPROVED, f"CLI review round {cli_round}/{max_rounds}")
            round_verdicts: list[tuple[str, str | None]] = []
            round_needs_revision = False
            round_reviewer_failed = False
            all_required_changes: list[str] = []

            extras_dir = self.paths.run_dir / "extras" / f"cli_review_{cli_round:03d}"
            try:
                review_dir = self._write_cli_review_context(
                    worktree_git=worktree_git,
                    outcome=outcome,
                    cli_round=cli_round,
                    max_rounds=max_rounds,
                    extras_dir=extras_dir,
                )
            except Exception as exc:
                self._last_cli_review_reason = f"review context failed: {exc}"
                self._emit(
                    events.CLI_REVIEW_LOOP_BLOCKED,
                    round=cli_round,
                    reason=f"review context failed: {exc}",
                )
                return TerminalVerdict.PR_NEEDS_HUMAN

            for tool in tools:
                sink = _cli_reviewer.jsonl_sink_for(self.paths, tool)
                sink_start_offset = sink.stat().st_size if sink.exists() else 0

                self._emit(events.CLI_REVIEW_STARTED, tool=tool, url=outcome.url, round=cli_round)
                prompt = _cli_reviewer.build_prompt(
                    pr_url=outcome.url, round_n=cli_round, round_of=max_rounds
                )
                start = time.monotonic()
                markdown = self._run_one_cli_reviewer(
                    tool=tool,
                    prompt=prompt,
                    sink=sink,
                    round_n=cli_round,
                    review_dir=review_dir,
                )
                duration_s = time.monotonic() - start
                _copy_file_slice(
                    src=sink,
                    dst=extras_dir / f"{tool}_raw_export.jsonl",
                    start_offset=sink_start_offset,
                )

                if not markdown:
                    round_reviewer_failed = True
                    self._emit(
                        events.CLI_REVIEW_FAILED,
                        tool=tool,
                        reason="empty_output",
                        round=cli_round,
                    )
                    round_verdicts.append((tool, None))
                    continue

                model = _cli_reviewer.extract_model(tool=tool, jsonl_path=sink)
                header = _cli_reviewer.format_header(
                    tool=tool,
                    model=model,
                    duration_s=duration_s,
                    round_n=cli_round,
                    round_of=max_rounds,
                )
                final_markdown = header + markdown.lstrip()

                review_md = extras_dir / f"{tool}_review.md"
                try:
                    review_md.write_text(final_markdown, encoding="utf-8")
                    (self.paths.run_dir / f"{tool}_review.md").write_text(
                        final_markdown, encoding="utf-8"
                    )
                except OSError as exc:
                    self._emit(
                        events.CLI_REVIEW_FAILED,
                        tool=tool,
                        reason=f"write_error: {exc}",
                        round=cli_round,
                    )
                    round_reviewer_failed = True
                    round_verdicts.append((tool, None))
                    continue

                posted, post_message = _cli_reviewer.post_comment(
                    pr_url=outcome.url,
                    body_path=review_md,
                    git_log=self.paths.git_log,
                )
                if not posted:
                    self._emit(
                        events.CLI_REVIEW_FAILED,
                        tool=tool,
                        reason=f"post_failed: {post_message}",
                        round=cli_round,
                    )

                verdict = _cli_reviewer.parse_verdict(markdown)
                self._emit(
                    events.CLI_REVIEW_COMPLETED,
                    tool=tool,
                    url=outcome.url,
                    review_chars=len(markdown),
                    verdict=verdict,
                    round=cli_round,
                )
                round_verdicts.append((tool, verdict))

                if verdict is None:
                    round_reviewer_failed = True
                    self._emit(
                        events.CLI_REVIEW_FAILED,
                        tool=tool,
                        reason="unparseable_verdict",
                        round=cli_round,
                    )
                    continue
                if verdict != "LOOKS_GOOD":
                    round_needs_revision = True
                    all_required_changes.extend(_cli_reviewer.extract_required_changes(markdown))

            last_round_verdicts = round_verdicts

            if round_reviewer_failed:
                self._last_cli_review_reason = (
                    "post-publish CLI review failed or produced no parseable verdict"
                )
                self._post_cli_review_status(
                    worktree_git=worktree_git, outcome=outcome, verdicts=round_verdicts
                )
                return TerminalVerdict.PR_NEEDS_HUMAN

            if not round_needs_revision:
                self._emit(
                    events.CLI_REVIEW_LOOP_DONE,
                    round=cli_round,
                    verdicts={t: v for t, v in round_verdicts},
                )
                self._last_cli_review_reason = None
                self._post_cli_review_status(
                    worktree_git=worktree_git, outcome=outcome, verdicts=round_verdicts
                )
                return TerminalVerdict.READY_FOR_DRAFT_PR

            if cli_round == max_rounds:
                break

            # Revision round: agent addresses required changes, then commits, gates,
            # and pushes only if the revised HEAD is deterministic-safe.
            self._emit(
                events.CLI_REVIEW_LOOP_REVISION,
                round=cli_round,
                required_changes_count=len(all_required_changes),
                verdicts={t: v for t, v in round_verdicts},
            )
            revision_ready = self._run_cli_review_revision(
                actor=actor,
                worktree_git=worktree_git,
                branch=branch,
                required_changes=all_required_changes,
                cli_round=cli_round,
                max_rounds=max_rounds,
            )
            if not revision_ready:
                self._last_cli_review_reason = "post-publish CLI review revision was blocked"
                self._post_cli_review_status(
                    worktree_git=worktree_git, outcome=outcome, verdicts=round_verdicts
                )
                return TerminalVerdict.PR_NEEDS_HUMAN

        # Max rounds exhausted without reaching LOOKS_GOOD on all tools.
        self._emit(
            events.CLI_REVIEW_LOOP_EXHAUSTED,
            rounds=max_rounds,
            verdicts={t: v for t, v in last_round_verdicts},
        )
        self._post_cli_review_status(
            worktree_git=worktree_git, outcome=outcome, verdicts=last_round_verdicts
        )
        return TerminalVerdict.PR_NEEDS_HUMAN

    def _run_cli_review_revision(
        self,
        *,
        actor,
        worktree_git: GitRepo,
        branch: str,
        required_changes: list[str],
        cli_round: int,
        max_rounds: int,
    ) -> bool:
        """Apply one post-publish CLI-review revision and gate it before push."""

        revision_prompt = prompts.cli_revision_followup(
            required_changes, round_n=cli_round, round_of=max_rounds
        )
        self._clear_implementation_complete()
        self._agent_turn(actor, revision_prompt)
        if not self._implementation_complete():
            self._emit(
                events.CLI_REVIEW_LOOP_BLOCKED,
                round=cli_round,
                reason="revision ended without IMPLEMENTATION_COMPLETE",
            )
            return False
        if self._cap_tripped():
            self._emit(
                events.CLI_REVIEW_LOOP_BLOCKED,
                round=cli_round,
                reason="cap tripped during CLI revision",
            )
            return False

        self._commit_agent_changes(worktree_git)
        revision_hash = diff_hash(worktree_git, self._diff_base)
        try:
            checks = run_checks(
                config=self.config,
                paths=self.paths,
                emit_event=self._emit,
            )
        except Exception as exc:
            self._emit(
                events.CLI_REVIEW_LOOP_BLOCKED,
                round=cli_round,
                reason=f"executable checks raised: {exc}",
            )
            return False
        self._record_worktree_state(worktree_git, f"after-cli-revision-checks-round{cli_round}")
        if not self._cli_revision_gates_passed(
            worktree_git=worktree_git,
            revision_hash=revision_hash,
            checks=checks,
            cli_round=cli_round,
        ):
            return False

        push = worktree_git.run("push", "origin", f"HEAD:{branch}", check=False)
        if push.returncode != 0:
            self._emit(
                events.CLI_REVIEW_LOOP_BLOCKED,
                round=cli_round,
                reason="push failed",
                stderr=push.stderr[-1000:],
            )
            return False
        return True

    def _cli_revision_gates_passed(
        self,
        *,
        worktree_git: GitRepo,
        revision_hash: str,
        checks: list[CheckResult],
        cli_round: int,
    ) -> bool:
        """Run deterministic L0/L1 gates for a post-publish revision."""

        recomputed_hash = diff_hash(worktree_git, self._diff_base)
        diff_hash_matched = recomputed_hash == revision_hash
        diff_scan = scan_diff(worktree_git, self._diff_base)
        clean = _only_contremaitre_changes(worktree_git.status_porcelain())
        hard_gates = hard_gate_payload(
            diff_scan=diff_scan,
            clean_worktree=clean,
            diff_hash_matched=diff_hash_matched,
        )
        checks_failed = any(not check.passed for check in checks)
        self._emit(
            events.HARD_GATES_CHECKED,
            context="cli_review_revision",
            round=cli_round,
            passed=bool(hard_gates["passed"]) and not checks_failed,
            diff_hash_matched=diff_hash_matched,
            diff_scan_passed=diff_scan.passed if diff_scan else False,
            clean_worktree=clean,
            changed_files=len(diff_scan.changed_files) if diff_scan else 0,
            failed_checks=[check.cmd for check in checks if not check.passed],
        )
        if hard_gates["passed"] and not checks_failed:
            return True
        self._emit(
            events.CLI_REVIEW_LOOP_BLOCKED,
            round=cli_round,
            reason="hard gate failed" if not hard_gates["passed"] else "executable checks failed",
            hard_gates=hard_gates,
            failed_checks=[check.cmd for check in checks if not check.passed],
        )
        return False

    def _run_one_cli_reviewer(
        self,
        *,
        tool: str,
        prompt: str,
        sink: Path,
        round_n: int,
        review_dir: Path,
    ) -> str:
        """Run one CLI reviewer tool in Docker for one round. Returns markdown or "".

        The reviewer sees only `/app:ro` plus the host-built `/review:ro`
        bundle. GitHub credentials and PR side effects stay host-owned.
        Failures are caught and emitted as CLI_REVIEW_FAILED events; the loop
        continues without this tool's verdict.
        """

        from .cli_actor import CliActorRunner as _CliActorRunner

        runner = _CliActorRunner(config=self.config, paths=self.paths, tool=tool)
        try:
            output = runner.cli_reviewer_turn(
                prompt=prompt,
                raw_export=sink,
                round_n=round_n,
                review_dir=review_dir,
            )
            return output.text
        except Exception as exc:
            self._emit(
                events.CLI_REVIEW_FAILED,
                tool=tool,
                reason=f"docker_error: {exc}",
                round=round_n,
            )
            return ""

    def _write_cli_review_context(
        self,
        *,
        worktree_git: GitRepo,
        outcome: PublishOutcome,
        cli_round: int,
        max_rounds: int,
        extras_dir: Path,
    ) -> Path:
        """Write the host-owned PR context mounted into CLI reviewer Docker."""

        review_dir = extras_dir / "input"
        review_dir.mkdir(parents=True, exist_ok=True)
        head_sha = worktree_git.output("rev-parse", "HEAD").strip()
        current_hash = diff_hash(worktree_git, self._diff_base)
        write_review_diff(worktree_git, self._diff_base, review_dir / "diff.patch")
        changed = worktree_git.output("diff", "--name-only", f"{self._diff_base}...HEAD")
        (review_dir / "changed_files.txt").write_text(changed, encoding="utf-8")
        (review_dir / "head_sha.txt").write_text(f"{head_sha}\n", encoding="utf-8")

        settled = self.paths.worktree / SETTLED_RELPATH
        if settled.is_file():
            (review_dir / "SETTLED_DESIGN.md").write_text(
                settled.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
        else:
            (review_dir / "SETTLED_DESIGN.md").write_text("", encoding="utf-8")

        pr_body = self.paths.run_dir / "pr_body.md"
        if pr_body.is_file():
            (review_dir / "pr_body.md").write_text(
                pr_body.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
        else:
            (review_dir / "pr_body.md").write_text("", encoding="utf-8")

        payload = {
            "url": outcome.url,
            "title": outcome.title,
            "base": outcome.base,
            "branch": outcome.branch,
            "round": cli_round,
            "round_of": max_rounds,
            "head_sha": head_sha,
            "diff_base": self._diff_base,
            "approved_diff_hash": outcome.approved_diff_hash,
            "current_diff_hash": current_hash,
        }
        write_json(review_dir / "pr.json", payload)
        (review_dir / "PR.md").write_text(
            _render_cli_review_context_markdown(payload),
            encoding="utf-8",
        )
        _write_previous_cli_reviews(
            extras_root=self.paths.run_dir / "extras",
            current_round=cli_round,
            dst=review_dir / "previous_cli_reviews.md",
        )
        return review_dir

    def _post_cli_review_status(
        self,
        *,
        worktree_git: GitRepo,
        outcome: PublishOutcome,
        verdicts: list[tuple[str, str | None]],
    ) -> None:
        """Project the cli_review verdict onto a GitHub commit status.

        Posts exactly one status per run on the published HEAD, using the
        worst verdict across all reviewers (`both` → any MUST_FIX wins). The
        status is informational until the operator requires the
        `contremaitre/cli-review` context in branch protection, at which
        point a MUST_FIX `failure` gates merge. Best-effort: a failure here
        never disturbs the already-published PR.
        """

        from . import cli_reviewer as _cli_reviewer

        worst = _cli_reviewer.worst_verdict([v for _, v in verdicts])
        if worst is None:
            # No reviewer produced a parseable verdict (all failed/drifted).
            # Skip rather than post a misleading success — branch protection
            # treats a never-posted context as not-applicable for this SHA.
            return
        try:
            sha = worktree_git.output("rev-parse", "HEAD").strip()
        except Exception:
            return
        # e.g. "claude MUST_FIX · codex LOOKS_GOOD" — the per-tool split, so
        # the merge box shows which reviewer objected even though the state
        # is worst-of-N.
        description = " · ".join(f"{tool} {v or 'unparsed'}" for tool, v in verdicts)
        posted, message = _cli_reviewer.post_commit_status(
            pr_url=outcome.url,
            sha=sha,
            verdict=worst,
            description=description,
            git_log=self.paths.git_log,
            target_url=outcome.url,
        )
        self._emit(
            events.CLI_REVIEW_STATUS,
            url=outcome.url,
            sha=sha,
            verdict=worst,
            state=message if posted else None,
            posted=posted,
            reason=None if posted else message,
        )

    # ----- publication gate -----

    def _publish_or_block(
        self,
        *,
        worktree_git: GitRepo,
        branch: str,
        checks: list[CheckResult],
        parsed: ParsedVerdict,
        approved_hash: str,
        actor,
    ) -> RunResult:
        if self.config.simulate_drift_after_approval:
            self._commit_drift(worktree_git)

        recomputed_hash = diff_hash(worktree_git, self._diff_base)
        diff_hash_matched = recomputed_hash == approved_hash
        diff_scan = scan_diff(worktree_git, self._diff_base)
        # `.contremaitre/*` is excluded from staging by design and stays
        # untracked in the worktree for the SIM to read across rounds —
        # don't count it against clean-worktree.
        clean = _only_contremaitre_changes(worktree_git.status_porcelain())
        hard_gates = hard_gate_payload(
            diff_scan=diff_scan,
            clean_worktree=clean,
            diff_hash_matched=diff_hash_matched,
        )
        self._emit(
            events.HARD_GATES_CHECKED,
            passed=bool(hard_gates["passed"]),
            diff_hash_matched=diff_hash_matched,
            diff_scan_passed=diff_scan.passed if diff_scan else False,
            clean_worktree=clean,
            changed_files=len(diff_scan.changed_files) if diff_scan else 0,
        )
        # L1 executable-check gate: blocks only on a configured-and-failing
        # check. No --check-cmd → empty results → no-op (operator opted out;
        # SIM approval + L0 hard gates still apply).
        checks_failed = any(not check.passed for check in checks)

        if not hard_gates["passed"]:
            return self._blocked_by_gates(
                branch=branch,
                approved_hash=approved_hash,
                current_hash=recomputed_hash,
                checks=checks,
                diff_scan=diff_scan,
                hard_gates=hard_gates,
                reason="hard gate failed",
                sim_verdict=parsed,
            )
        if checks_failed:
            return self._blocked_by_gates(
                branch=branch,
                approved_hash=approved_hash,
                current_hash=recomputed_hash,
                checks=checks,
                diff_scan=diff_scan,
                hard_gates=hard_gates,
                reason="executable checks failed",
                sim_verdict=parsed,
            )

        # Write eval BEFORE publish so `_derive_pr_metadata` can read the
        # scorecard into the PR body. Safe because eval inputs (hard_gates,
        # checks, parsed) are all in scope here, and `reason` is unused
        # downstream when `sim_verdict` is set (always the case on this path).
        self._write_eval(
            verdict=TerminalVerdict.READY_FOR_DRAFT_PR,
            checks=checks,
            hard_gates=hard_gates,
            needs_human=[],
            sim_verdict=parsed,
            reason="approved",
        )
        publisher = make_publisher(self.config)
        outcome = publisher.publish(
            config=self.config,
            paths=self.paths,
            branch=branch,
            diff_hash=approved_hash,
        )
        self._emit(
            events.PUBLISHED,
            publish_mode=self.config.publish_mode.value,
            branch=branch,
            url=outcome.url,
            dry_run=outcome.dry_run,
        )
        # Post-publish CLI review loop. Runs reviewer rounds in Docker;
        # triggers agent revisions on the same branch on any non-LOOKS_GOOD
        # verdict. Exits READY_FOR_DRAFT_PR (all LOOKS_GOOD) or PR_NEEDS_HUMAN
        # (max rounds exhausted). Never raises — the PR is already published.
        terminal_verdict = self._run_cli_review_loop(
            worktree_git=worktree_git,
            branch=branch,
            outcome=outcome,
            actor=actor,
        )
        final_reason = outcome.reason
        if terminal_verdict != TerminalVerdict.READY_FOR_DRAFT_PR:
            final_reason = (
                self._last_cli_review_reason or "post-publish CLI review did not reach LOOKS_GOOD"
            )
            self._write_eval(
                verdict=terminal_verdict,
                checks=checks,
                hard_gates=hard_gates,
                needs_human=[final_reason],
                sim_verdict=parsed,
                reason=final_reason,
            )
        self._write_final_stats(State.APPROVED, terminal_verdict, final_reason)
        return RunResult(
            run_id=self.run_id,
            terminal_state=State.APPROVED,
            verdict=terminal_verdict,
            run_dir=self.paths.run_dir,
            worktree=self.paths.worktree,
            pr_created=True,
            reason=final_reason,
        )

    def _blocked_by_gates(
        self,
        *,
        branch: str,
        approved_hash: str,
        current_hash: str,
        checks: list[CheckResult],
        diff_scan: DiffScanResult,
        hard_gates: dict[str, object],
        reason: str,
        sim_verdict: ParsedVerdict | None,
    ) -> RunResult:
        self._emit(
            events.PUBLICATION_BLOCKED,
            reason=reason,
            hard_gates=hard_gates,
            forbidden_files=diff_scan.forbidden_files,
        )
        record_publication(
            self.paths,
            PublishOutcome(
                kind=PublishOutcomeKind.BLOCKED,
                base=self.config.base,
                publish_mode=self.config.publish_mode,
                reason=reason,
                branch=branch,
                approved_diff_hash=approved_hash,
                current_diff_hash=current_hash,
                dry_run=True,
            ),
        )
        self._write_eval(
            verdict=TerminalVerdict.NO_PR_NEEDS_HUMAN,
            checks=checks,
            hard_gates=hard_gates,
            needs_human=[reason],
            sim_verdict=sim_verdict,
            reason=reason,
        )
        self._write_final_stats(State.NO_PR, TerminalVerdict.NO_PR_NEEDS_HUMAN, reason)
        return RunResult(
            run_id=self.run_id,
            terminal_state=State.NO_PR,
            verdict=TerminalVerdict.NO_PR_NEEDS_HUMAN,
            run_dir=self.paths.run_dir,
            worktree=self.paths.worktree,
            pr_created=False,
            reason=reason,
        )

    def _terminal_no_pr(
        self,
        verdict: TerminalVerdict,
        reason: str,
        *,
        branch: str | None = None,
        checks: list[CheckResult] | None = None,
        sim_verdict: ParsedVerdict | None = None,
    ) -> RunResult:
        record_publication(
            self.paths,
            PublishOutcome(
                kind=PublishOutcomeKind.NO_PR,
                base=self.config.base,
                publish_mode=self.config.publish_mode,
                reason=reason,
                branch=branch,
                dry_run=True,
            ),
        )
        self._write_eval(
            verdict=verdict,
            checks=checks or [],
            hard_gates={
                "passed": False,
                "checks": {
                    "diff_scan": False,
                    "clean_worktree": False,
                    "diff_hash_matched": False,
                    "draft_only": True,
                },
                "forbidden_files": [],
                "changed_files": [],
            },
            needs_human=[reason] if verdict != TerminalVerdict.NO_PR_CHANGES_REQUESTED else [],
            sim_verdict=sim_verdict,
            reason=reason,
        )
        self._write_final_stats(State.NO_PR, verdict, reason)
        return RunResult(
            run_id=self.run_id,
            terminal_state=State.NO_PR,
            verdict=verdict,
            run_dir=self.paths.run_dir,
            worktree=self.paths.worktree,
            pr_created=False,
            reason=reason,
        )

    def _write_eval(
        self,
        *,
        verdict: TerminalVerdict,
        checks: list[CheckResult],
        hard_gates: dict[str, object],
        needs_human: list[str],
        sim_verdict: ParsedVerdict | None,
        reason: str,
    ) -> None:
        # `sim_verdict` is the verdict that drove publication (or the last seen
        # before a terminal-no-pr). `_last_sim_parsed` carries the per-round
        # breakdown set by `_run_review`.
        sim_parsed = self._last_sim_parsed
        if sim_verdict is not None and sim_parsed is not None:
            sim_review = sim_review_summary(
                verdict=sim_parsed.verdict.value,
                confidence=sim_parsed.confidence,
                summary=sim_parsed.summary,
                required_changes=sim_parsed.required_changes,
                checks_performed=sim_parsed.checks_performed,
            )
        elif sim_verdict is not None:
            sim_review = sim_review_summary(
                verdict=sim_verdict.verdict.value,
                confidence=sim_verdict.confidence,
                summary=sim_verdict.summary,
                required_changes=sim_verdict.required_changes,
                checks_performed=sim_verdict.checks_performed,
            )
        else:
            sim_review = sim_review_summary(
                verdict=None,
                confidence=None,
                summary=reason,
            )
        write_eval_reports(
            paths=self.paths,
            verdict=verdict,
            hard_gates=hard_gates,
            checks=checks,
            sim_review=sim_review,
            trajectory={
                "turns": self.turns,
                "states": self.trajectory,
                "process_reliability": 1.0
                if verdict == TerminalVerdict.READY_FOR_DRAFT_PR
                else 0.5,
            },
            needs_human=needs_human,
        )

    # ----- worktree + git helpers -----

    def _prepare_run_dir(self) -> None:
        self.paths.run_dir.mkdir(parents=True, exist_ok=False)
        self.paths.eval_dir.mkdir(parents=True, exist_ok=True)
        self.paths.initial_prompt.write_text(prompts.INITIAL_PROMPT, encoding="utf-8")
        self.paths.transcript.write_text(
            f"# Contremaitre transcript - {self.run_id}\n", encoding="utf-8"
        )
        # Provenance manifest. Records models, image, target, base, and the
        # version of the system under test (contremaitre SHA + prompt /
        # skills-lock / dockerfile hashes). Downstream readers (`tui attach`,
        # `viewer/index.py`, the eval canary) all read this file; new fields
        # are additive so existing readers stay compatible.
        write_json(
            self.paths.run_dir / "run_config.json",
            build_manifest(self.config),
        )

    def _create_worktree(self, repo: GitRepo, branch: str) -> None:
        if self.paths.worktree.exists():
            if self.paths.worktree.name.startswith("contremaitre-"):
                shutil.rmtree(self.paths.worktree)
            else:
                raise RuntimeError(
                    f"refusing to remove non-Contremaitre path: {self.paths.worktree}"
                )
        # Re-pin the cache's `origin` URL to the canonical source for
        # this run BEFORE fetching. The previous run's worktree rewired
        # `origin` to `--fork` (so the publisher could `git push origin
        # HEAD:<branch>`), and since worktrees share git config with the
        # cache, that URL persists. Without this step, run N+1's fetch
        # would target run N's `--fork` URL even if the operator passed
        # a different `--fork` (or set `--upstream`) on the new CLI.
        source_url = self.config.upstream or self.config.fork
        if source_url:
            repo.run("remote", "set-url", "origin", source_url, check=False)
        # Fetch the base branch fresh from `origin` and branch the
        # worktree from the remote-tracking ref — never from local refs.
        # Local refs are operator-mutable and were the root cause of
        # earlier run-to-run check flakiness.
        repo.run("fetch", "origin", self.config.base)
        base_ref = f"origin/{self.config.base}"
        self._base_sha = repo.run("rev-parse", base_ref).stdout.strip()
        repo.run("worktree", "add", str(self.paths.worktree), "-b", branch, base_ref)
        worktree_git = GitRepo(self.paths.worktree, self.paths.git_log)
        if self.config.fork:
            worktree_git.run("remote", "remove", "origin", check=False)
            worktree_git.run("remote", "add", "origin", self.config.fork)
        if self.config.upstream:
            worktree_git.run("remote", "remove", "upstream", check=False)
            worktree_git.run("remote", "add", "upstream", self.config.upstream)
        self._record_worktree_state(worktree_git, "after-worktree")

    def _commit_drift(self, repo: GitRepo) -> None:
        drift = self.paths.worktree / ".contremaitre" / "drift_after_approval.txt"
        drift.parent.mkdir(parents=True, exist_ok=True)
        drift.write_text("committed after approval to force diff-hash mismatch\n", encoding="utf-8")
        repo.run("add", str(drift.relative_to(self.paths.worktree)))
        repo.run("commit", "-m", "Simulate drift after approval")
        self._emit(events.SIMULATED_DIFF_DRIFT)

    def _commit_agent_changes(self, repo: GitRepo) -> None:
        if _only_contremaitre_changes(repo.status_porcelain()):
            self._emit(events.HOST_COMMIT_SKIPPED, reason="worktree clean")
            return
        title, body = derive_commit_message(self.paths.worktree, self.run_id)
        # `:(exclude)X` pathspecs hold orchestration-internal / build-output
        # files out of the staged set. They stay in the worktree (SIM reads
        # them; agent produces them) but must not land in the PR diff.
        # Drop excludes already covered by the worktree's .gitignore: git
        # treats `:(exclude)X` as an explicit mention of X, and the add
        # aborts when X is also gitignored ("paths are ignored").
        excludes = [
            f":(exclude){path}" for path in _HOST_COMMIT_EXCLUDES if not _is_gitignored(repo, path)
        ]
        repo.run("add", "--", ".", *excludes)
        repo.run("commit", "-m", title, "-m", body)
        self._emit(
            events.HOST_COMMIT_CREATED,
            reason="actor left worktree changes for orchestrator-owned git boundary",
            title=title,
        )

    # ----- terminal signal -----

    def _implementation_complete(self) -> bool:
        return (self.paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH).exists()

    def _clear_implementation_complete(self) -> None:
        marker = self.paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH
        if marker.exists():
            marker.unlink()
            self._emit(events.IMPLEMENTATION_COMPLETE_CLEARED)

    # ----- bookkeeping -----

    def _emit(self, event: str, **fields) -> None:
        """Append an event row to guardrail_events.jsonl.

        Wraps the `append_jsonl(self.paths.guardrail_events, {"event": …, …})`
        boilerplate so call sites read as one line for simple emits and stay
        flat for multi-field ones. Single emission point also lets us add a
        timestamp / turn-counter later in one place.
        """

        append_jsonl(self.paths.guardrail_events, {"event": event, **fields})

    def _transition(self, state: State, note: str) -> None:
        record = {"state": state.value, "note": note, "turns": self.turns}
        self.trajectory.append(record)
        append_jsonl(self.paths.timeline, record)

    def _before_turn(self) -> None:
        self.turns += 1
        append_jsonl(self.paths.timeline, {"event": events.TURN, "turn": self.turns})

    def _cap_tripped(self) -> bool:
        wall_minutes = (time.monotonic() - self.started) / 60.0
        if self.turns >= self.config.caps.max_turns:
            self._emit(events.TURN_CAP, turns=self.turns)
            return True
        if wall_minutes >= self.config.caps.max_wall_minutes:
            self._emit(events.WALL_CAP, wall_minutes=wall_minutes)
            return True
        recorded_cost = estimate_recorded_cost_usd(self.paths.raw_export, self.paths.sim_raw_export)
        token_rollup = sum_token_usage(self.paths.raw_export, self.paths.sim_raw_export)
        write_json(
            self.paths.cost_report,
            {
                "recorded_cost_usd": recorded_cost,
                "max_cost_usd": self.config.caps.max_cost_usd,
                "tokens": token_rollup,
                "note": "Recorded stream cost only; provider-side limit remains the primary spend guardrail.",
            },
        )
        if recorded_cost >= self.config.caps.max_cost_usd:
            self._emit(
                events.RECORDED_COST_CAP,
                recorded_cost_usd=recorded_cost,
                max_cost_usd=self.config.caps.max_cost_usd,
            )
            return True
        if self.no_progress_streak >= self.config.caps.no_progress_turns:
            self._emit(
                events.NO_PROGRESS_CAP,
                no_progress_streak=self.no_progress_streak,
                no_progress_turns=self.config.caps.no_progress_turns,
            )
            return True
        return False

    def _snapshot_worktree(self, repo: GitRepo) -> _WorktreeSnapshot:
        return _WorktreeSnapshot(
            status=repo.run("status", "--porcelain", check=False).stdout,
            diff_stat=repo.run("diff", "--stat", f"{self._diff_base}...HEAD", check=False).stdout,
        )

    def _record_progress(self, snapshot: _WorktreeSnapshot, label: str, text: str) -> None:
        key = (snapshot.status + "\n" + snapshot.diff_stat, str(len(text.strip())))
        if self._last_progress_key is None or key != self._last_progress_key:
            self.no_progress_streak = 0
            self._last_progress_key = key
            event = events.PROGRESS
        else:
            self.no_progress_streak += 1
            event = events.NO_PROGRESS
        self._emit(event, label=label, no_progress_streak=self.no_progress_streak)

    def _record_worktree_state(self, repo: GitRepo, label: str) -> _WorktreeSnapshot:
        """Snapshot + log + return for reuse by callers needing the same data."""

        snapshot = self._snapshot_worktree(repo)
        append_jsonl(
            self.paths.worktree_state,
            {"label": label, "status": snapshot.status, "diff_stat": snapshot.diff_stat},
        )
        return snapshot

    def _write_final_stats(
        self, terminal_state: State, verdict: TerminalVerdict, reason: str
    ) -> None:
        write_json(
            self.paths.stats,
            {
                "run_id": self.run_id,
                "terminal_state": terminal_state.value,
                "verdict": verdict.value,
                "reason": reason,
                "turns": self.turns,
                "duration_seconds": round(time.monotonic() - self.started, 3),
                "agent_model": role_model_label(
                    actor_mode=self.config.actor_mode,
                    opencode_model=self.config.agent_model,
                    codex_model=self.config.codex_model,
                    codex_effort=self.config.codex_effort,
                    cli_tool=self.config.cli_tool,
                    claude_model=self.config.claude_model,
                    claude_effort=self.config.claude_effort,
                ),
                "sim_model": role_model_label(
                    actor_mode=self.config.sim_actor_mode or self.config.actor_mode,
                    opencode_model=self.config.sim_model,
                    codex_model=self.config.codex_model,
                    codex_effort=self.config.codex_effort,
                    cli_tool=self.config.sim_cli_tool or self.config.cli_tool,
                    claude_model=self.config.claude_model,
                    claude_effort=self.config.claude_effort,
                ),
                "actor_mode": self.config.actor_mode.value,
                "publish_mode": self.config.publish_mode.value,
                "recorded_cost_usd": estimate_recorded_cost_usd(
                    self.paths.raw_export, self.paths.sim_raw_export
                ),
            },
        )
        write_json(self.paths.trajectory, {"states": self.trajectory})

    def _ensure_pristine_deps_volume(self) -> None:
        """Populate the lockhash-keyed deps cache against the fresh worktree.

        Runs AFTER `_create_worktree` (which fetched `origin/<base>`
        fresh), so the lockfile keying reflects the exact state the
        agent will see. Reading the lockfile from the cache clone — as
        the pre-fix CLI did — meant the cache could be keyed on a
        months-stale snapshot, silently no-op'ing when the lockfile was
        newly added upstream.

        Skipped in fake-mode tests (no docker involved). DepsInstallError
        propagates: continuing without deps makes L1 checks look like
        real failures when the underlying issue is an install gap.
        """

        if self.config.actor_mode != ActorMode.OPENCODE:
            return
        try:
            handle = ensure_deps_volume(
                repo=self.paths.worktree,
                base_image=self.config.docker_image,
                runs_root=self.config.runs_root,
                # Cache-clone dir name is the canonical project slug
                # (`github.com-jbmoutout-contremaitre`); used to scope
                # volume naming + prune so projects don't evict each
                # other's caches.
                project_id=self.config.repo.name,
            )
        except DepsInstallError as exc:
            raise RuntimeError(str(exc)) from exc
        if handle is not None:
            self.config = dataclasses.replace(self.config, deps_volume=handle)

    def _provision_run_deps_volume(self) -> None:
        """Clone the pristine deps cache into a per-run volume.

        Mutates `self.config.deps_volume` to point at the per-run
        clone so downstream actor + check container mounts use the
        ephemeral volume. The pristine cache is untouched (RO in the
        clone step) and survives for the next run. The per-run
        volume is labeled with this run's id; `_remove_run_volumes`
        in `finally` removes it.

        Skipped when no pristine volume was set (no lockfile detected
        for this target, or fake-mode tests).
        """

        pristine = self.config.deps_volume
        if not pristine:
            return
        per_run = clone_deps_volume_for_run(
            pristine=pristine,
            run_id=self.run_id,
            base_image=self.config.docker_image,
        )
        self.config = dataclasses.replace(self.config, deps_volume=per_run)

    def _cleanup_worktree(self) -> None:
        if not self.paths.worktree.name.startswith("contremaitre-"):
            return
        # Stop any container still labeled for this run before removing
        # the worktree — a container with the worktree mounted blocks
        # `worktree remove` / rmtree. Normal flow on the happy path is a
        # no-op (each turn's container is already removed in its
        # `finally`); this catches the timeout / signal paths.
        self._stop_run_containers()
        self._remove_run_volumes()
        source_repo = GitRepo(self.config.repo, self.paths.git_log)
        worktree_existed = self.paths.worktree.exists()
        if worktree_existed:
            source_repo.run("worktree", "remove", "--force", str(self.paths.worktree), check=False)
        if self.paths.worktree.exists():
            shutil.rmtree(self.paths.worktree)
        source_repo.run("worktree", "prune", check=False)
        if worktree_existed:
            self._emit(events.WORKTREE_REMOVED, path=str(self.paths.worktree))

    def _remove_run_volumes(self) -> None:
        """Remove docker volumes labeled with this run-id.

        Catches the per-run deps clone created by
        `_provision_run_deps_volume`. Best-effort: a volume still in
        use (e.g. by a container `_stop_run_containers` didn't fully
        stop) won't delete; we swallow that so cleanup doesn't mask
        the real run outcome.
        """

        import subprocess as _sp

        try:
            ls = _sp.run(
                [
                    "docker",
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=contremaitre.run-id={self.run_id}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, _sp.TimeoutExpired):
            return
        for name in (line for line in ls.stdout.split() if line):
            try:
                _sp.run(["docker", "volume", "rm", "-f", name], capture_output=True, timeout=15)
            except (OSError, _sp.TimeoutExpired):
                continue

    def _stop_run_containers(self) -> None:
        """`docker stop` every container labeled with this run-id. Best effort.

        Containers are launched detached + labeled `contremaitre.run-id=<id>`,
        so signal handlers / cleanup can find and stop them by label without
        tracking individual container ids.
        """

        import subprocess as _sp

        try:
            ps = _sp.run(
                ["docker", "ps", "-q", "--filter", f"label=contremaitre.run-id={self.run_id}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, _sp.TimeoutExpired):
            return
        ids = [line for line in ps.stdout.split() if line]
        for cid in ids:
            try:
                _sp.run(["docker", "stop", "-t", "5", cid], capture_output=True, timeout=15)
            except (OSError, _sp.TimeoutExpired):
                continue


def _is_gitignored(repo: GitRepo, path: str) -> bool:
    """True iff `path` is matched by a .gitignore rule in the worktree.

    Used to skip `:(exclude){path}` pathspecs that would otherwise cause
    `git add` to abort — git treats `:(exclude)` as explicit mention, and
    explicit + gitignored is an error.
    """

    return repo.run("check-ignore", "-q", "--", path, check=False).returncode == 0


def _only_contremaitre_changes(porcelain: str) -> bool:
    """True iff every `git status --porcelain` row is orchestration-internal.

    Files excluded from commits by pathspec (`.contremaitre/*`,
    `opencode.json`) are deliberately untracked in the worktree. The
    host-commit step and the clean-worktree hard gate both need to treat
    a worktree whose only changes are in these paths as "clean for our
    purposes":

    - host-commit: skip instead of producing an empty PR.
    - clean-worktree gate: pass.

    Empty porcelain (no changes at all) is also "clean".
    """

    _INTERNAL_PREFIXES = (
        ".contremaitre/",
        ".contremaitre",
        "opencode.json",
        "dist/",
        "build/",
        "out/",
        ".next/",
        "__pycache__/",
    )

    for line in porcelain.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if not any(path == p or path.startswith(p) for p in _INTERNAL_PREFIXES):
            return False
    return True


def _copy_file_slice(*, src: Path, dst: Path, start_offset: int) -> None:
    """Copy bytes appended to `src` since `start_offset` into `dst`."""

    if not src.exists():
        return
    try:
        with src.open("rb") as fh:
            fh.seek(start_offset)
            data = fh.read()
    except OSError:
        return
    if not data:
        return
    try:
        dst.write_bytes(data)
    except OSError:
        return


def _render_cli_review_context_markdown(payload: dict[str, object]) -> str:
    """Small manifest telling the reviewer what the host mounted."""

    lines = [
        "# Pull Request Context",
        "",
        "GitHub access is host-owned. The reviewer container must not fetch the PR,",
        "call `gh`, or use GitHub credentials. Review only the mounted files below",
        "and the read-only worktree at `/app`.",
        "",
        f"- PR URL: {payload.get('url') or '(unknown)'}",
        f"- Title: {payload.get('title') or '(unknown)'}",
        f"- Base: {payload.get('base') or '(unknown)'}",
        f"- Branch: {payload.get('branch') or '(unknown)'}",
        f"- Round: {payload.get('round')}/{payload.get('round_of')}",
        f"- Head SHA: {payload.get('head_sha') or '(unknown)'}",
        f"- Diff base: {payload.get('diff_base') or '(unknown)'}",
        f"- Current diff hash: {payload.get('current_diff_hash') or '(unknown)'}",
        "",
        "## Mounted Files",
        "",
        "- `/review/diff.patch` — current PR diff against the run base",
        "- `/review/changed_files.txt` — changed file list",
        "- `/review/SETTLED_DESIGN.md` — design the agent claimed to implement",
        "- `/review/pr_body.md` — PR body posted by the host",
        "- `/review/pr.json` — structured PR metadata",
        "- `/review/previous_cli_reviews.md` — previous CLI-review comments in this run",
    ]
    return "\n".join(lines) + "\n"


def _write_previous_cli_reviews(*, extras_root: Path, current_round: int, dst: Path) -> None:
    parts: list[str] = []
    for round_n in range(1, current_round):
        round_dir = extras_root / f"cli_review_{round_n:03d}"
        if not round_dir.is_dir():
            continue
        for review_md in sorted(round_dir.glob("*_review.md")):
            tool = review_md.name.removesuffix("_review.md")
            try:
                body = review_md.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if body:
                parts.append(f"## Round {round_n} — {tool}\n\n{body}\n")
    if parts:
        dst.write_text("\n".join(parts), encoding="utf-8")
    else:
        dst.write_text("No previous CLI-review comments in this run.\n", encoding="utf-8")


def run(config: RunConfig) -> RunResult:
    return Orchestrator(config).run()
