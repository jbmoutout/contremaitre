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
from .actors import ActorRunner, make_actor_runner
from .checks import CheckResult, run_checks
from .runtime_image import clone_deps_volume_for_run
from .costs import estimate_recorded_cost_usd
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
from .models import (
    ParsedVerdict,
    ReviewVerdict,
    RunConfig,
    RunResult,
    State,
    TerminalVerdict,
)
from .paths import build_run_paths, new_run_id, validate_slug
from .preflight import enforce_preflight
from .publisher import (
    PublishOutcome,
    PublishOutcomeKind,
    make_publisher,
    record_publication,
)
from .verdicts import VerdictParseError, diff_hash, parse_sim_verdict, write_review_diff


SETTLED_RELPATH = Path(".contremaitre") / "SETTLED_DESIGN.md"
IMPLEMENTATION_COMPLETE_RELPATH = Path(".contremaitre") / "IMPLEMENTATION_COMPLETE"


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
            self._write_final_stats(State.FAILED, TerminalVerdict.FAILED_INFRA, "killed_via_sigterm")
            self._extract_artifacts_safely()
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, _on_sigterm)
        try:
            enforce_preflight(self.config, self.paths)
            self._transition(State.INIT, "creating worktree")
            self._create_worktree(repo, branch)
            self._provision_run_deps_volume()
            worktree_git = GitRepo(self.paths.worktree, self.paths.git_log)
            actor = make_actor_runner(config=self.config, paths=self.paths)

            return self._review_rounds(actor=actor, worktree_git=worktree_git, branch=branch)
        except Exception as exc:
            self._emit(events.INFRA_FAILURE, error=repr(exc))
            record_publication(
                self.paths,
                PublishOutcome(
                    kind=PublishOutcomeKind.NO_PR,
                    base=self.config.base,
                    publish_mode=self.config.publish_mode,
                    reason=f"FAILED_INFRA: {exc}",
                    branch=branch,
                    dry_run=True,
                ),
            )
            self._write_final_stats(State.FAILED, TerminalVerdict.FAILED_INFRA, str(exc))
            return RunResult(
                run_id=self.run_id,
                terminal_state=State.FAILED,
                verdict=TerminalVerdict.FAILED_INFRA,
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
            append_jsonl(self.paths.recoveries, {"kind": events.VIEWER_BUILD_FAILED, "error": repr(exc)})

    def _review_rounds(self, *, actor: ActorRunner, worktree_git: GitRepo, branch: str) -> RunResult:
        last_required_changes: list[str] = []
        last_parsed: ParsedVerdict | None = None

        for review_round in range(1, self.config.caps.max_review_rounds + 1):
            self._transition(State.WORK, f"WORK session round {review_round}")
            outcome = self._run_work_session(
                actor=actor,
                review_round=review_round,
                required_changes=last_required_changes,
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
                self._clear_implementation_complete()
                self._emit(
                    events.REVISION_REQUESTED,
                    round=review_round,
                    required_changes=last_required_changes,
                )
                continue

            # APPROVED — drift check + hard gates + publish
            return self._publish_or_block(
                worktree_git=worktree_git,
                branch=branch,
                checks=checks,
                parsed=parsed,
                approved_hash=current_hash,
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
    ) -> str:
        """Run the multi-turn WORK session until terminal or cap.

        Returns a short reason string describing why the loop exited.
        """

        if review_round == 1:
            first_message = prompts.INITIAL_PROMPT
        else:
            first_message = prompts.revision_followup(required_changes)

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

        parsed: ParsedVerdict | None = None
        last_error: str | None = None
        for attempt in range(1, self.config.caps.malformed_verdict_retries + 2):
            self._before_turn()
            output = actor.sim_review(
                diff_file=diff_file,
                settled_file=settled_file,
                scenario=self.config.sim_scenario,
                attempt=attempt,
            )
            raw = output.text
            try:
                parsed = parse_sim_verdict(raw)
                break
            except VerdictParseError as exc:
                last_error = str(exc)
                self._emit(
                    events.MALFORMED_VERDICT,
                    round=review_round,
                    attempt=attempt,
                    error=last_error,
                )

        if parsed is None:
            return None

        append_jsonl(
            self.paths.review_cycles,
            {
                "round": review_round,
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
            verdict=parsed.verdict.value,
            confidence=parsed.confidence,
            summary=parsed.summary[:200] if parsed.summary else "",
            required_changes=len(parsed.required_changes),
        )
        return parsed, current_hash

    # ----- publication gate -----

    def _publish_or_block(
        self,
        *,
        worktree_git: GitRepo,
        branch: str,
        checks: list[CheckResult],
        parsed: ParsedVerdict,
        approved_hash: str,
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
        self._write_eval(
            verdict=TerminalVerdict.READY_FOR_DRAFT_PR,
            checks=checks,
            hard_gates=hard_gates,
            needs_human=[],
            sim_verdict=parsed,
            reason=outcome.reason,
        )
        self._write_final_stats(State.APPROVED, TerminalVerdict.READY_FOR_DRAFT_PR, outcome.reason)
        return RunResult(
            run_id=self.run_id,
            terminal_state=State.APPROVED,
            verdict=TerminalVerdict.READY_FOR_DRAFT_PR,
            run_dir=self.paths.run_dir,
            worktree=self.paths.worktree,
            pr_created=True,
            reason=outcome.reason,
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
        if sim_verdict is not None:
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
                "process_reliability": 1.0 if verdict == TerminalVerdict.READY_FOR_DRAFT_PR else 0.5,
            },
            needs_human=needs_human,
        )

    # ----- worktree + git helpers -----

    def _prepare_run_dir(self) -> None:
        self.paths.run_dir.mkdir(parents=True, exist_ok=False)
        self.paths.eval_dir.mkdir(parents=True, exist_ok=True)
        self.paths.initial_prompt.write_text(prompts.INITIAL_PROMPT, encoding="utf-8")
        self.paths.transcript.write_text(f"# Contremaitre transcript - {self.run_id}\n", encoding="utf-8")

    def _create_worktree(self, repo: GitRepo, branch: str) -> None:
        if self.paths.worktree.exists():
            if self.paths.worktree.name.startswith("contremaitre-"):
                shutil.rmtree(self.paths.worktree)
            else:
                raise RuntimeError(f"refusing to remove non-Contremaitre path: {self.paths.worktree}")
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
        title, body = _derive_commit_message(self.paths.worktree, self.run_id)
        # Pathspec excludes keep orchestration-internal files out of the
        # staged set even though the files stay in the worktree:
        #
        # `.contremaitre/*` — SETTLED_DESIGN.md, IMPLEMENTATION_COMPLETE,
        #   architecture-review.html. Must stay for WORK-phase SIM reads.
        #
        # `opencode.json` — opencode may create a local config file in the
        #   worktree root even though a synthesized config is mounted :ro.
        #   This is opencode internal state, not part of any design.
        repo.run("add", "--", ".", ":(exclude).contremaitre", ":(exclude)opencode.json")
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
        write_json(
            self.paths.cost_report,
            {
                "recorded_cost_usd": recorded_cost,
                "max_cost_usd": self.config.caps.max_cost_usd,
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

    def _write_final_stats(self, terminal_state: State, verdict: TerminalVerdict, reason: str) -> None:
        write_json(
            self.paths.stats,
            {
                "run_id": self.run_id,
                "terminal_state": terminal_state.value,
                "verdict": verdict.value,
                "reason": reason,
                "turns": self.turns,
                "duration_seconds": round(time.monotonic() - self.started, 3),
                "agent_model": self.config.agent_model,
                "sim_model": self.config.sim_model,
                "actor_mode": self.config.actor_mode.value,
                "publish_mode": self.config.publish_mode.value,
                "recorded_cost_usd": estimate_recorded_cost_usd(self.paths.raw_export, self.paths.sim_raw_export),
            },
        )
        write_json(self.paths.trajectory, {"states": self.trajectory})

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
                ["docker", "volume", "ls", "-q", "--filter", f"label=contremaitre.run-id={self.run_id}"],
                capture_output=True, text=True, timeout=5,
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
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, _sp.TimeoutExpired):
            return
        ids = [line for line in ps.stdout.split() if line]
        for cid in ids:
            try:
                _sp.run(["docker", "stop", "-t", "5", cid], capture_output=True, timeout=15)
            except (OSError, _sp.TimeoutExpired):
                continue


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

    _INTERNAL_PATHS = (".contremaitre/", ".contremaitre", "opencode.json")

    for line in porcelain.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if not any(path == p or path.startswith(p) for p in _INTERNAL_PATHS):
            return False
    return True


def _derive_commit_message(worktree: Path, run_id: str) -> tuple[str, str]:
    """Read SETTLED_DESIGN.md and turn it into (commit title, commit body).

    Title: first non-empty line, stripped of `# ` and any "Settled design — "
    prefix the skill tends to emit. Falls back to a run-id-tagged generic
    when SETTLED is missing or empty (shouldn't happen post-WORK since the
    orchestrator gates on it, but the host commit must never fail here).
    Body: the full SETTLED text + a trailer with the run id, so the commit
    is self-contained for anyone reading `git log` later.
    """

    settled = worktree / SETTLED_RELPATH
    fallback_title = f"Contremaitre refactor ({run_id})"
    if not settled.exists():
        return fallback_title, f"Run: {run_id}\n"
    text = settled.read_text(encoding="utf-8").strip()
    if not text:
        return fallback_title, f"Run: {run_id}\n"
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    title = first_line.lstrip("#").strip()
    for prefix in ("Settled design — ", "Settled design - ", "Settled design: "):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()
            break
    if not title:
        title = fallback_title
    body = f"{text}\n\n---\nRun: {run_id}\n"
    return title, body


def run(config: RunConfig) -> RunResult:
    return Orchestrator(config).run()
