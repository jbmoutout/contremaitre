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
import subprocess
import time
from pathlib import Path

from . import prompts
from .actors import ActorRunner, _kill_orphan_containers_by_mount, make_actor_runner
from .checks import CheckResult, run_checks
from .costs import estimate_recorded_cost_usd
from .diffscan import DiffScanResult, scan_diff
from .evaluator import (
    hard_gate_payload,
    sim_review_summary,
    write_eval_reports,
)
from .extract import extract_run_artifacts
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

    # ----- top-level run -----

    def run(self) -> RunResult:
        self._prepare_run_dir()
        repo = GitRepo(self.config.repo, self.paths.git_log)
        branch = f"{validate_slug(self.config.branch_prefix, 'branch prefix')}/{self.run_id}"

        # SIGTERM handler: when the operator kills us, dump whatever state we
        # have to disk before exiting so the run isn't a black hole.
        # (SIGKILL still can't be caught.)
        prior_handler = signal.getsignal(signal.SIGTERM)

        def _on_sigterm(_signum, _frame):
            append_jsonl(
                self.paths.recoveries,
                {"kind": "sigterm_emergency_write", "turns": self.turns},
            )
            self._write_final_stats(State.FAILED, TerminalVerdict.FAILED_INFRA, "killed_via_sigterm")
            self._extract_artifacts_safely()
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, _on_sigterm)
        try:
            enforce_preflight(self.config, self.paths)
            self._transition(State.INIT, "creating worktree")
            self._create_worktree(repo, branch)
            worktree_git = GitRepo(self.paths.worktree, self.paths.git_log)
            actor = make_actor_runner(config=self.config, paths=self.paths)

            return self._review_rounds(actor=actor, worktree_git=worktree_git, branch=branch)
        except Exception as exc:
            append_jsonl(self.paths.guardrail_events, {"event": "infra_failure", "error": repr(exc)})
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
            signal.signal(signal.SIGTERM, prior_handler)

    def _extract_artifacts_safely(self) -> None:
        """Run the subagent + files extractor; swallow extraction errors."""

        try:
            extract_run_artifacts(self.paths)
        except Exception as exc:
            append_jsonl(self.paths.recoveries, {"kind": "extract_failed", "error": repr(exc)})

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
            append_jsonl(
                self.paths.guardrail_events,
                {"event": "work_session_end", "round": review_round, "outcome": outcome},
            )

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
            checks = run_checks(self.paths.worktree, self.config.check_cmds, self.paths.test_runs)
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
                append_jsonl(
                    self.paths.guardrail_events,
                    {
                        "event": "revision_requested",
                        "round": review_round,
                        "required_changes": last_required_changes,
                    },
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
        self._record_worktree_state(worktree_git, label)
        self._record_progress(worktree_git, label, text)
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
        current_hash = diff_hash(worktree_git, self.config.base)
        diff_file = self.paths.run_dir / f"review_diff_round{review_round}.diff"
        write_review_diff(worktree_git, self.config.base, diff_file)

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
                append_jsonl(
                    self.paths.guardrail_events,
                    {
                        "event": "malformed_verdict",
                        "round": review_round,
                        "attempt": attempt,
                        "error": last_error,
                    },
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

        recomputed_hash = diff_hash(worktree_git, self.config.base)
        diff_hash_matched = recomputed_hash == approved_hash
        diff_scan = scan_diff(worktree_git, self.config.base)
        clean = worktree_git.status_porcelain() == ""
        hard_gates = hard_gate_payload(
            diff_scan=diff_scan,
            clean_worktree=clean,
            diff_hash_matched=diff_hash_matched,
        )
        checks_pass = bool(checks) and all(check.passed for check in checks)

        if not hard_gates["passed"]:
            return self._blocked_by_gates(
                branch=branch,
                approved_hash=approved_hash,
                checks=checks,
                diff_scan=diff_scan,
                hard_gates=hard_gates,
                reason="hard gate failed",
                sim_verdict=parsed,
            )
        if not checks_pass:
            return self._blocked_by_gates(
                branch=branch,
                approved_hash=approved_hash,
                checks=checks,
                diff_scan=diff_scan,
                hard_gates=hard_gates,
                reason="executable checks failed or were not configured",
                sim_verdict=parsed,
            )

        publisher = make_publisher(self.config)
        outcome = publisher.publish(
            config=self.config,
            paths=self.paths,
            branch=branch,
            diff_hash=approved_hash,
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
        checks: list[CheckResult],
        diff_scan: DiffScanResult,
        hard_gates: dict[str, object],
        reason: str,
        sim_verdict: ParsedVerdict | None,
    ) -> RunResult:
        append_jsonl(
            self.paths.guardrail_events,
            {
                "event": "publication_blocked",
                "reason": reason,
                "hard_gates": hard_gates,
                "forbidden_files": diff_scan.forbidden_files,
            },
        )
        record_publication(
            self.paths,
            PublishOutcome(
                kind=PublishOutcomeKind.BLOCKED,
                base=self.config.base,
                publish_mode=self.config.publish_mode,
                reason=reason,
                branch=branch,
                diff_hash=approved_hash,
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
        repo.run("worktree", "add", str(self.paths.worktree), "-b", branch, self.config.base)
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
        append_jsonl(self.paths.guardrail_events, {"event": "simulated_diff_drift"})

    def _commit_agent_changes(self, repo: GitRepo) -> None:
        status = repo.status_porcelain()
        if not status.strip():
            append_jsonl(self.paths.guardrail_events, {"event": "host_commit_skipped", "reason": "worktree clean"})
            return
        repo.run("add", ".")
        repo.run("commit", "-m", "Apply Contremaitre agent changes")
        append_jsonl(
            self.paths.guardrail_events,
            {
                "event": "host_commit_created",
                "reason": "actor left worktree changes for orchestrator-owned git boundary",
            },
        )

    # ----- terminal signal -----

    def _implementation_complete(self) -> bool:
        return (self.paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH).exists()

    def _clear_implementation_complete(self) -> None:
        marker = self.paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH
        if marker.exists():
            marker.unlink()
            append_jsonl(self.paths.guardrail_events, {"event": "implementation_complete_cleared"})

    # ----- bookkeeping -----

    def _transition(self, state: State, note: str) -> None:
        record = {"state": state.value, "note": note, "turns": self.turns}
        self.trajectory.append(record)
        append_jsonl(self.paths.timeline, record)

    def _before_turn(self) -> None:
        self.turns += 1
        append_jsonl(self.paths.timeline, {"event": "turn", "turn": self.turns})

    def _cap_tripped(self) -> bool:
        wall_minutes = (time.monotonic() - self.started) / 60.0
        if self.turns >= self.config.caps.max_turns:
            append_jsonl(self.paths.guardrail_events, {"event": "turn_cap", "turns": self.turns})
            return True
        if wall_minutes >= self.config.caps.max_wall_minutes:
            append_jsonl(self.paths.guardrail_events, {"event": "wall_cap", "wall_minutes": wall_minutes})
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
            append_jsonl(
                self.paths.guardrail_events,
                {
                    "event": "recorded_cost_cap",
                    "recorded_cost_usd": recorded_cost,
                    "max_cost_usd": self.config.caps.max_cost_usd,
                },
            )
            return True
        if self.no_progress_streak >= self.config.caps.no_progress_turns:
            append_jsonl(
                self.paths.guardrail_events,
                {
                    "event": "no_progress_cap",
                    "no_progress_streak": self.no_progress_streak,
                    "no_progress_turns": self.config.caps.no_progress_turns,
                },
            )
            return True
        return False

    def _record_progress(self, repo: GitRepo, label: str, text: str) -> None:
        status = repo.run("status", "--porcelain", check=False).stdout
        diff_stat = repo.run("diff", "--stat", f"{self.config.base}...HEAD", check=False).stdout
        key = (status + "\n" + diff_stat, str(len(text.strip())))
        if self._last_progress_key is None or key != self._last_progress_key:
            self.no_progress_streak = 0
            self._last_progress_key = key
            event = "progress"
        else:
            self.no_progress_streak += 1
            event = "no_progress"
        append_jsonl(
            self.paths.guardrail_events,
            {
                "event": event,
                "label": label,
                "no_progress_streak": self.no_progress_streak,
            },
        )

    def _record_worktree_state(self, repo: GitRepo, label: str) -> None:
        append_jsonl(
            self.paths.worktree_state,
            {
                "label": label,
                "status": repo.run("status", "--porcelain", check=False).stdout,
                "diff_stat": repo.run("diff", "--stat", f"{self.config.base}...HEAD", check=False).stdout,
            },
        )

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

    def _cleanup_worktree(self) -> None:
        if not self.paths.worktree.name.startswith("contremaitre-"):
            return
        # Kill any container still holding this worktree as a mount before we
        # try to remove it — docker --rm doesn't always tear down on parent
        # death, and a held mount makes `worktree remove` and rmtree fail.
        killed = _kill_orphan_containers_by_mount(self.paths.worktree)
        if killed:
            append_jsonl(
                self.paths.recoveries,
                {"kind": "orphan_container_kill", "reason": "cleanup", "container_ids": killed},
            )
        source_repo = GitRepo(self.config.repo, self.paths.git_log)
        if self.paths.worktree.exists():
            source_repo.run("worktree", "remove", "--force", str(self.paths.worktree), check=False)
        if self.paths.worktree.exists():
            shutil.rmtree(self.paths.worktree)
        source_repo.run("worktree", "prune", check=False)
        self._cleanup_docker_volume()

    def _cleanup_docker_volume(self) -> None:
        """Remove the per-run named volume backing /app/node_modules.

        Containers have already exited (--rm) and any orphans were killed in
        _cleanup_worktree, so the volume is no longer in use. Best-effort:
        swallow failures so cleanup doesn't mask the real verdict.
        """

        # Fake-mode runs never touched docker.
        from .models import ActorMode

        if self.config.actor_mode != ActorMode.OPENCODE:
            return
        try:
            proc = subprocess.run(
                ["docker", "volume", "rm", "-f", self.paths.docker_volume],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        # `docker volume rm -f` exits 0 even for nonexistent volumes; the
        # name appears in stdout only on actual removal.
        if proc.returncode == 0 and self.paths.docker_volume in proc.stdout:
            append_jsonl(
                self.paths.recoveries,
                {"kind": "volume_removed", "name": self.paths.docker_volume},
            )


def run(config: RunConfig) -> RunResult:
    return Orchestrator(config).run()
