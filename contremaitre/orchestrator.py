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

import dataclasses
import signal
import time
from pathlib import Path

from . import events, prompts
from .actors import ActorError, ActorRunner, make_actor_runner
from .cap_guard import CapGuard
from .checks import CheckResult, run_checks
from .deps_provisioner import DepsProvisioner
from .diffscan import DiffScanResult, scan_diff
from .evaluator import (
    combined_review_summary,
    hard_gate_payload,
    sim_review_summary,
    write_eval_reports,
)
from .extract import extract_run_artifacts
from .git_utils import GitRepo
from .jsonlog import append_jsonl, write_json
from .models import (
    ActorMode,
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
from .review_manager import ReviewManager
from .session_loop import SessionLoop
from .verdicts import VerdictParseError, diff_hash, parse_sim_verdict, write_review_diff
from .viewer import build_viewer
from .worktree_manager import (
    SETTLED_RELPATH,
    WorktreeManager,
)


class Orchestrator:
    def __init__(self, config: RunConfig):
        self.config = config
        self.run_id = new_run_id(config.run_slug)
        self.paths = build_run_paths(config.runs_root, self.run_id)
        self.started = time.monotonic()
        self.trajectory: list[dict[str, object]] = []
        self._diff_base: str = ""
        self.turns: int = 0
        self._last_sim_parsed: ParsedVerdict | None = None
        self._last_extra_parsed: ParsedVerdict | None = None

    def _emit(self, event: str, **fields) -> None:
        append_jsonl(self.paths.guardrail_events, {"event": event, **fields})

    def _transition(self, state: State, note: str) -> None:
        record = {"state": state.value, "note": note, "turns": self.turns}
        self.trajectory.append(record)
        append_jsonl(self.paths.timeline, record)

    # ----- top-level run -----

    def run(self) -> RunResult:
        self._prepare_run_dir()
        repo = GitRepo(self.config.repo, self.paths.git_log)
        branch = f"{validate_slug(self.config.branch_prefix, 'branch prefix')}/{self.run_id}"

        prior_term = signal.getsignal(signal.SIGTERM)

        def _on_sigterm(_signum, _frame):
            self._stop_run_containers()
            append_jsonl(
                self.paths.recoveries,
                {"kind": events.SIGTERM_EMERGENCY_WRITE, "turns": 0, "signal": "sigterm"},
            )
            self._write_final_stats(State.FAILED, TerminalVerdict.FAILED_INFRA, "killed_via_sigterm")
            self._extract_artifacts_safely()
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, _on_sigterm)

        wm: WorktreeManager | None = None
        try:
            enforce_preflight(self.config, self.paths)
            self._transition(State.INIT, "creating worktree")

            wm = WorktreeManager(self.config, self.paths, self._emit)
            base_sha = wm.create(repo, branch)
            self._diff_base = base_sha

            dp = DepsProvisioner(self.config, self.paths)
            pristine = dp.ensure_pristine(self.paths.worktree, self.config.repo.name)
            self.config = dataclasses.replace(self.config, deps_volume=dp.provision_run(pristine, self.run_id))

            cg = CapGuard(self.config.caps, self.started)
            sl = SessionLoop(self.config, self.paths, cg, wm, self._emit, self.trajectory)
            rm = ReviewManager(self.config, self.paths, self._emit)

            worktree_git = GitRepo(self.paths.worktree, self.paths.git_log)
            actor = make_actor_runner(config=self.config, paths=self.paths)

            return self._review_rounds(actor=actor, worktree_git=worktree_git, branch=branch, wm=wm, rm=rm, sl=sl, cg=cg)
        except Exception as exc:
            failure_kind = getattr(exc, "kind", None) if isinstance(exc, ActorError) else None
            self._emit(events.INFRA_FAILURE, error=repr(exc), kind=failure_kind)
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
            if not self.config.keep_worktree and wm is not None:
                source_repo = GitRepo(self.config.repo, self.paths.git_log)
                wm.cleanup(source_repo)
            signal.signal(signal.SIGTERM, prior_term)

    def _prepare_run_dir(self) -> None:
        self.paths.run_dir.mkdir(parents=True, exist_ok=False)
        self.paths.eval_dir.mkdir(parents=True, exist_ok=True)
        self.paths.initial_prompt.write_text(prompts.INITIAL_PROMPT, encoding="utf-8")
        self.paths.transcript.write_text(f"# Contremaitre transcript - {self.run_id}\n", encoding="utf-8")
        write_json(
            self.paths.run_dir / "run_config.json",
            {
                "agent_model": self.config.agent_model,
                "sim_model": self.config.sim_model,
                "extra_reviewer_model": self.config.extra_reviewer_model,
                "docker_image": self.config.docker_image,
                "target_url": self.config.upstream or self.config.fork or str(self.config.repo),
                "base": self.config.base,
            },
        )

    def _stop_run_containers(self) -> None:
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

    def _extract_artifacts_safely(self) -> None:
        try:
            extract_run_artifacts(self.paths)
        except Exception as exc:
            append_jsonl(self.paths.recoveries, {"kind": events.EXTRACT_FAILED, "error": repr(exc)})
        try:
            build_viewer(self.paths)
        except Exception as exc:
            append_jsonl(self.paths.recoveries, {"kind": events.VIEWER_BUILD_FAILED, "error": repr(exc)})

    def _review_rounds(
        self,
        *,
        actor: ActorRunner,
        worktree_git: GitRepo,
        branch: str,
        wm: WorktreeManager,
        rm: ReviewManager,
        sl: SessionLoop,
        cg: CapGuard,
    ) -> RunResult:
        last_required_changes: list[str] = []
        last_parsed: ParsedVerdict | None = None
        last_sim: ParsedVerdict | None = None
        last_extra: ParsedVerdict | None = None

        for review_round in range(1, self.config.caps.max_review_rounds + 1):
            self._transition(State.WORK, f"WORK session round {review_round}")
            outcome = sl.run(
                actor=actor,
                review_round=review_round,
                required_changes=last_required_changes,
                sim_parsed=last_sim,
                extra_parsed=last_extra,
                diff_base=self._diff_base,
            )
            self._emit(events.WORK_SESSION_END, round=review_round, outcome=outcome)
            self.turns = cg.turns

            if not self._implementation_complete():
                return self._terminal_no_pr(
                    TerminalVerdict.NO_PR_NEEDS_HUMAN,
                    f"WORK ended without IMPLEMENTATION_COMPLETE ({outcome})",
                    branch=branch,
                )
            if cg.tripped(self.paths.raw_export, self.paths.sim_raw_export, self.paths.cost_report, self._emit) is not None:
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

            wm.commit_agent_changes(worktree_git)
            checks = run_checks(
                config=self.config,
                paths=self.paths,
                emit_event=self._emit,
            )
            wm.record_worktree_state(worktree_git, f"after-checks-round{review_round}", self._diff_base)

            review_result = rm.run_review(
                actor=actor,
                worktree_git=worktree_git,
                settled_file=settled_file,
                review_round=review_round,
                diff_base=self._diff_base,
            )
            self._last_sim_parsed = rm.last_sim_parsed
            self._last_extra_parsed = rm.last_extra_parsed
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
                last_sim = rm.last_sim_parsed
                last_extra = rm.last_extra_parsed
                self._clear_implementation_complete()
                self._emit(
                    events.REVISION_REQUESTED,
                    round=review_round,
                    required_changes=last_required_changes,
                )
                continue

            return self._publish_or_block(
                worktree_git=worktree_git,
                branch=branch,
                checks=checks,
                parsed=parsed,
                approved_hash=current_hash,
            )

        summary = last_parsed.summary if last_parsed else "no SIM verdict captured"
        return self._terminal_no_pr(
            TerminalVerdict.NO_PR_CHANGES_REQUESTED,
            f"max review rounds exhausted: {summary}",
            branch=branch,
            sim_verdict=last_parsed,
        )

    def _implementation_complete(self) -> bool:
        from .worktree_manager import IMPLEMENTATION_COMPLETE_RELPATH
        return (self.paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH).exists()

    def _clear_implementation_complete(self) -> None:
        from .worktree_manager import IMPLEMENTATION_COMPLETE_RELPATH
        marker = self.paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH
        if marker.exists():
            marker.unlink()
            self._emit(events.IMPLEMENTATION_COMPLETE_CLEARED)

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
            wm = WorktreeManager(self.config, self.paths, self._emit)
            wm.commit_drift(worktree_git)

        recomputed_hash = diff_hash(worktree_git, self._diff_base)
        diff_hash_matched = recomputed_hash == approved_hash
        diff_scan = scan_diff(worktree_git, self._diff_base)
        from .worktree_manager import _only_contremaitre_changes
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
        extra_attempted = self.config.extra_reviewer_model is not None
        sim_parsed = self._last_sim_parsed
        extra_parsed = self._last_extra_parsed
        if sim_verdict is not None and sim_parsed is not None:
            sim_review = combined_review_summary(
                sim=sim_parsed,
                extra=extra_parsed,
                merged=sim_verdict,
                extra_attempted=extra_attempted,
            )
        elif sim_verdict is not None:
            sim_review = combined_review_summary(
                sim=sim_verdict,
                extra=extra_parsed,
                merged=sim_verdict,
                extra_attempted=extra_attempted,
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

    def _write_final_stats(self, terminal_state: State, verdict: TerminalVerdict, reason: str) -> None:
        from .costs import estimate_recorded_cost_usd
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
                "extra_reviewer_model": self.config.extra_reviewer_model,
                "actor_mode": self.config.actor_mode.value,
                "publish_mode": self.config.publish_mode.value,
                "recorded_cost_usd": estimate_recorded_cost_usd(self.paths.raw_export, self.paths.sim_raw_export),
            },
        )
        write_json(self.paths.trajectory, {"states": self.trajectory})


def run(config: RunConfig) -> RunResult:
    return Orchestrator(config).run()
