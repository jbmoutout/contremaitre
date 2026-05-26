from __future__ import annotations

from pathlib import Path

from . import events
from .actors import ActorError, ActorRunner
from .git_utils import GitRepo
from .jsonlog import append_jsonl
from .models import ParsedVerdict, RunConfig, RunPaths, State
from .verdicts import VerdictParseError, diff_hash, parse_sim_verdict, write_review_diff


def _merge_verdicts(sim: ParsedVerdict, extra: ParsedVerdict | None) -> ParsedVerdict:
    from .models import ReviewVerdict

    _VERDICT_SEVERITY = {
        ReviewVerdict.APPROVED: 0,
        ReviewVerdict.CHANGES_REQUESTED: 1,
        ReviewVerdict.NEEDS_HUMAN: 2,
    }

    if extra is None:
        return sim

    if _VERDICT_SEVERITY[extra.verdict] > _VERDICT_SEVERITY[sim.verdict]:
        merged_verdict = extra.verdict
    else:
        merged_verdict = sim.verdict

    def _norm(s: str) -> str:
        return s.strip().casefold()

    sim_norms = {_norm(c) for c in sim.required_changes}
    extra_norms = {_norm(c) for c in extra.required_changes}
    overlap_norms = sim_norms & extra_norms

    merged_required: list[str] = []
    seen: set[str] = set()
    for change in sim.required_changes:
        norm = _norm(change)
        if norm in seen:
            continue
        seen.add(norm)
        tag = "[SIM+EXTRA]" if norm in overlap_norms else "[SIM]"
        merged_required.append(f"{tag} {change}")
    for change in extra.required_changes:
        norm = _norm(change)
        if norm in seen:
            continue
        seen.add(norm)
        merged_required.append(f"[EXTRA] {change}")

    merged_checks = list(dict.fromkeys(sim.checks_performed + extra.checks_performed))
    merged_summary = f"{sim.summary}\n— EXTRA: {extra.summary}"
    merged_confidence = min(sim.confidence, extra.confidence)

    return ParsedVerdict(
        verdict=merged_verdict,
        confidence=merged_confidence,
        required_changes=merged_required,
        checks_performed=merged_checks,
        summary=merged_summary,
        raw=sim.raw,
    )


class ReviewManager:
    def __init__(self, config: RunConfig, paths: RunPaths, emit):
        self.config = config
        self.paths = paths
        self.emit = emit
        self.last_sim_parsed: ParsedVerdict | None = None
        self.last_extra_parsed: ParsedVerdict | None = None

    def run_review(
        self,
        *,
        actor: ActorRunner,
        worktree_git: GitRepo,
        settled_file: Path,
        review_round: int,
        diff_base: str,
    ) -> tuple[ParsedVerdict, str] | None:
        self._transition(State.REVIEW, f"SIM review round {review_round}")
        current_hash = diff_hash(worktree_git, diff_base)
        diff_file = self.paths.run_dir / f"review_diff_round{review_round}.diff"
        write_review_diff(worktree_git, diff_base, diff_file)

        sim_parsed = self._run_one_reviewer(
            actor=actor,
            diff_file=diff_file,
            settled_file=settled_file,
            review_round=review_round,
            reviewer_id="sim",
            model_override=None,
            scenario=self.config.sim_scenario,
        )
        if sim_parsed is None:
            return None
        self._record_review_cycle(review_round, current_hash, sim_parsed, reviewer="sim")
        self.last_sim_parsed = sim_parsed
        self.last_extra_parsed = None

        extra_parsed: ParsedVerdict | None = None
        if self.config.extra_reviewer_model:
            try:
                extra_parsed = self._run_one_reviewer(
                    actor=actor,
                    diff_file=diff_file,
                    settled_file=settled_file,
                    review_round=review_round,
                    reviewer_id="extra",
                    model_override=self.config.extra_reviewer_model,
                    scenario=self.config.extra_reviewer_scenario,
                )
                unavailable_reason = (
                    None if extra_parsed is not None else "malformed_verdict_exhausted"
                )
            except ActorError as exc:
                extra_parsed = None
                unavailable_reason = f"actor_error: {exc}"
            if extra_parsed is None:
                self._record_extra_reviewer_unavailable(
                    review_round=review_round,
                    reason=unavailable_reason or "unknown",
                )
            else:
                self._record_review_cycle(
                    review_round, current_hash, extra_parsed, reviewer="extra"
                )
                self.last_extra_parsed = extra_parsed

        merged = _merge_verdicts(sim_parsed, extra_parsed)
        return merged, current_hash

    def _run_one_reviewer(
        self,
        *,
        actor: ActorRunner,
        diff_file: Path,
        settled_file: Path,
        review_round: int,
        reviewer_id: str,
        model_override: str | None,
        scenario: str,
    ) -> ParsedVerdict | None:
        parsed: ParsedVerdict | None = None
        for attempt in range(1, self.config.caps.malformed_verdict_retries + 2):
            output = actor.sim_review(
                diff_file=diff_file,
                settled_file=settled_file,
                scenario=scenario,
                attempt=attempt,
                reviewer_id=reviewer_id,
                model_override=model_override,
            )
            try:
                parsed = parse_sim_verdict(output.text)
                break
            except VerdictParseError as exc:
                self.emit(
                    events.MALFORMED_VERDICT,
                    round=review_round,
                    attempt=attempt,
                    reviewer=reviewer_id,
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
        self.emit(
            events.REVIEW_VERDICT,
            round=review_round,
            reviewer=reviewer,
            verdict=parsed.verdict.value,
            confidence=parsed.confidence,
            summary=parsed.summary[:200] if parsed.summary else "",
            required_changes=len(parsed.required_changes),
        )

    def _record_extra_reviewer_unavailable(
        self,
        *,
        review_round: int,
        reason: str,
    ) -> None:
        append_jsonl(
            self.paths.review_cycles,
            {
                "round": review_round,
                "reviewer": "extra",
                "unavailable": True,
                "reason": reason,
            },
        )
        record = {
            "kind": events.EXTRA_REVIEWER_UNAVAILABLE,
            "round": review_round,
            "reason": reason,
        }
        append_jsonl(self.paths.recoveries, record)
        append_jsonl(
            self.paths.guardrail_events,
            {"event": f"recovery_{events.EXTRA_REVIEWER_UNAVAILABLE}", **record},
        )

    def _transition(self, state: State, note: str) -> None:
        from .jsonlog import append_jsonl
        append_jsonl(self.paths.timeline, {"state": state.value, "note": note, "turns": 0})
