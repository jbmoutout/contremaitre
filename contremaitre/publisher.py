"""Publication boundary.

The orchestrator is the only component allowed to publish. Actor containers do
not receive GitHub credentials, and this module runs only after SIM approval,
diff-hash verification, executable checks, and deterministic diff-scan pass.

`PublishOutcome` is the single tagged result type for every terminal of the
state machine — PUBLISHED, BLOCKED, NO_PR — and `record_publication` is the
one writer of `pr.json`. Schema drift between the published and not-published
paths is structurally impossible.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .jsonlog import append_jsonl, write_json
from .models import PublishMode, RunConfig, RunPaths
from .scaffolds import (
    IMPLEMENTATION_COMPLETE_RELPATH,
    derive_commit_message,
)


class PublishOutcomeKind(str, Enum):
    PUBLISHED = "PUBLISHED"  # Publisher ran. May be dry-run (stub) or real (gh).
    BLOCKED = "BLOCKED"  # Hard gate or executable check refused publication.
    NO_PR = "NO_PR"  # Run ended before publication was attempted.


@dataclass(frozen=True)
class PublishOutcome:
    """Tagged result for every terminal of the publication path.

    Two hash fields by design:
      - `approved_diff_hash`: what the SIM signed off on. Stable across the
        BLOCKED-on-drift case so we can still see what was approved.
      - `current_diff_hash`: what the worktree contains right now. Differs from
        `approved_diff_hash` only on a drift block — that's the whole signal
        that drift happened, and it would be lost if we collapsed the two
        into one field.

    For PUBLISHED outcomes the two are equal by definition (drift check passed).
    For NO_PR outcomes both are None (no diff was reviewed).
    """

    kind: PublishOutcomeKind
    base: str
    publish_mode: PublishMode
    reason: str
    branch: str | None = None
    url: str | None = None
    approved_diff_hash: str | None = None
    current_diff_hash: str | None = None
    dry_run: bool = (
        True  # True for stub or for non-PUBLISHED kinds; False only when gh actually opened a PR.
    )
    # PR title as passed to `gh pr create --title` (or what would have been
    # passed in stub mode). None for non-PUBLISHED outcomes. Exposed in
    # pr.json so downstream readers (TUI footer, viewer) can render it
    # without re-parsing SETTLED_DESIGN.md.
    title: str | None = None


def record_publication(paths: RunPaths, outcome: PublishOutcome) -> None:
    """Write the single canonical pr.json row for this run."""

    write_json(
        paths.pr_json,
        {
            "kind": outcome.kind.value,
            "branch": outcome.branch,
            "base": outcome.base,
            "url": outcome.url,
            "approved_diff_hash": outcome.approved_diff_hash,
            "current_diff_hash": outcome.current_diff_hash,
            "reason": outcome.reason,
            "publish_mode": outcome.publish_mode.value,
            "dry_run": outcome.dry_run,
            "title": outcome.title,
        },
    )


class Publisher:
    def publish(
        self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str
    ) -> PublishOutcome:
        raise NotImplementedError


class StubPublisher(Publisher):
    def publish(
        self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str
    ) -> PublishOutcome:
        # PUBLISHED implies the drift check passed, so approved == current.
        # Derive title even in stub mode so pr.json carries the same shape
        # as real publishes (and the schema lock test holds).
        derived_title, _ = _derive_pr_metadata(paths, diff_hash)
        outcome = PublishOutcome(
            kind=PublishOutcomeKind.PUBLISHED,
            base=config.base,
            publish_mode=config.publish_mode,
            reason="publisher stub: would push branch and open a draft PR after approval",
            branch=branch,
            url=None,
            approved_diff_hash=diff_hash,
            current_diff_hash=diff_hash,
            dry_run=True,
            title=config.pr_title or derived_title,
        )
        record_publication(paths, outcome)
        return outcome


class GhPublisher(Publisher):
    """Host-side GitHub publisher using local git + GitHub CLI."""

    def publish(
        self, *, config: RunConfig, paths: RunPaths, branch: str, diff_hash: str
    ) -> PublishOutcome:
        if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
            raise RuntimeError("GITHUB_TOKEN or GH_TOKEN is required for --publish-mode gh")
        if not config.fork:
            raise RuntimeError("--fork is required for --publish-mode gh")

        env = os.environ.copy()
        derived_title, derived_body = _derive_pr_metadata(paths, diff_hash)
        pr_body = _write_pr_body(paths, config, derived_body)
        final_title = config.pr_title or derived_title
        self._run(
            ["git", "push", "origin", f"HEAD:{branch}"], cwd=paths.worktree, paths=paths, env=env
        )
        cmd = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            config.base,
            "--head",
            branch,
            "--title",
            final_title,
            "--body-file",
            str(pr_body),
        ]
        if config.gh_repo:
            cmd.extend(["--repo", config.gh_repo])
        proc = self._run(cmd, cwd=paths.worktree, paths=paths, env=env)
        outcome = PublishOutcome(
            kind=PublishOutcomeKind.PUBLISHED,
            base=config.base,
            publish_mode=config.publish_mode,
            reason="pushed branch and opened draft PR via gh",
            branch=branch,
            url=_extract_url(proc.stdout),
            approved_diff_hash=diff_hash,
            current_diff_hash=diff_hash,
            dry_run=False,
            title=final_title,
        )
        record_publication(paths, outcome)
        return outcome

    def _run(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        paths: RunPaths,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
        append_jsonl(
            paths.git_log,
            {
                "cmd": cmd,
                "cwd": str(cwd),
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
                "publisher": "gh",
            },
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"publisher command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}"
            )
        return proc


def make_publisher(config: RunConfig) -> Publisher:
    if config.publish_mode == PublishMode.STUB:
        return StubPublisher()
    if config.publish_mode == PublishMode.GH:
        return GhPublisher()
    raise RuntimeError(f"unknown publish mode: {config.publish_mode}")


def _write_pr_body(paths: RunPaths, config: RunConfig, derived_body: str) -> Path:
    body = paths.run_dir / "pr_body.md"
    text = config.pr_body or derived_body
    body.write_text(text, encoding="utf-8")
    return body


def _derive_pr_metadata(paths: RunPaths, diff_hash: str) -> tuple[str, str]:
    """Build PR title + body from SETTLED_DESIGN.md and the eval report.

    Title: same shape as the host commit title (first SETTLED line minus
    skill-emitted "Settled design — " prefix) so the PR, the commit, and
    `git log` all agree.
    Body: lede stamp + IMPLEMENTATION_COMPLETE one-liner + SETTLED design
    (headings demoted) + SIM review (summary + collapsible checklist) +
    revision callout if the SIM bounced + eval scorecard + footer seal.
    Self-contained so reviewers don't need to clone the run dir.
    """

    import json as _json
    import re as _re
    from .flow_use import compute_phases

    def _read_jsonl(p: Path) -> list[dict]:
        if not p.exists():
            return []
        try:
            return [
                _json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
        except (OSError, ValueError):
            return []

    def _read_json(p: Path) -> dict | None:
        if not p.exists():
            return None
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    title, settled_body = derive_commit_message(paths.worktree, paths.run_id)
    # `derive_commit_message` appends `\n\n---\nRun: <id>\n` for `git log`
    # readability. The PR body has its own footer with run_id + diff hash,
    # so strip the commit-only trailer to avoid a double separator.
    settled_body = _re.sub(r"\n+---\nRun: [^\n]+\n*$", "", settled_body)
    # Demote SETTLED headings 2 levels so the body's own H2 sections
    # (## Design, ## SIM review) stay the top of the visible hierarchy
    # and the SETTLED H1/H2 don't blow up GitHub's rendering.
    settled_body = _re.sub(r"^(#{1,4}) ", r"\1## ", settled_body, flags=_re.MULTILINE)

    impl_complete = _read_impl_complete(paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH)
    pr_eval = _read_json(paths.eval_dir / "pr_eval.json")

    review_cycles = _read_jsonl(paths.review_cycles)
    test_runs = _read_jsonl(paths.test_runs)

    # Per-reviewer split. Treat missing `reviewer` field as "sim" so old runs
    # written before the extra-reviewer feature land in the right bucket.
    sim_cycles = [
        r for r in review_cycles if r.get("reviewer", "sim") == "sim" and not r.get("unavailable")
    ]
    extra_attempted = any(r.get("reviewer") == "extra" for r in review_cycles)
    sim = sim_cycles[-1] if sim_cycles else {}
    last_round_value = max((r.get("round") or 0 for r in review_cycles), default=0)
    last_round_entries = [r for r in review_cycles if (r.get("round") or 0) == last_round_value]
    last_round_extra = next(
        (
            r
            for r in last_round_entries
            if r.get("reviewer") == "extra" and not r.get("unavailable")
        ),
        None,
    )
    last_round_extra_unavailable = any(
        r.get("reviewer") == "extra" and r.get("unavailable") for r in last_round_entries
    )

    # Phase split — surfaces "design pass actually happened" vs "agent shipped
    # on candidate selection alone". grill≤1 with impl=1 is the skipped-grilling
    # pattern; grill≥3 means real back-and-forth before SETTLED.
    try:
        phases = compute_phases(paths)
    except Exception:
        phases = {
            "grilling_exchanges": None,
            "impl_turns": None,
            "review_rounds": last_round_value,
        }

    # ----- lede -----
    verdict = (sim.get("verdict") or "?").upper()
    confidence = sim.get("confidence")
    n_rounds = last_round_value or len(sim_cycles)
    n_tests = len(test_runs)
    n_pass = sum(1 for t in test_runs if t.get("returncode") == 0)

    lede_parts = [f"֍ **{verdict}**"]
    if extra_attempted:
        if last_round_extra is not None:
            agreement = (sim.get("verdict") or "").upper() == (
                last_round_extra.get("verdict") or ""
            ).upper()
            lede_parts.append("SIM+EXTRA agreed" if agreement else "SIM+EXTRA disagreed")
        elif last_round_extra_unavailable:
            lede_parts.append("EXTRA unavailable")
    if confidence is not None:
        if last_round_extra is not None and last_round_extra.get("confidence") is not None:
            lede_parts.append(f"confidence {confidence:.2f}/{last_round_extra['confidence']:.2f}")
        else:
            lede_parts.append(f"confidence {confidence:.1f}")
    if phases.get("grilling_exchanges") is not None:
        lede_parts.append(f"grill {phases['grilling_exchanges']} · impl {phases['impl_turns']}")
    if n_rounds:
        lede_parts.append(f"{n_rounds} review round{'s' if n_rounds > 1 else ''}")
    if n_tests:
        mark = "✓" if n_pass == n_tests else "✗"
        lede_parts.append(f"tests {n_pass}/{n_tests} {mark}")

    # ----- revision callout (any reviewer bouncing in any round) -----
    bounced = [r for r in review_cycles if (r.get("verdict") or "").upper() == "CHANGES_REQUESTED"]
    revision_lines: list[str] = []
    for r in bounced:
        reqs = r.get("required_changes") or []
        if reqs:
            first = reqs[0]
            note = (first[:117] + "…") if len(first) > 120 else first
            if len(reqs) > 1:
                note += f" (+ {len(reqs) - 1} more)"
            who = r.get("reviewer", "sim").upper()
            revision_lines.append(f"> Round {r.get('round', '?')} {who} flagged: {note}")

    # ----- SIM checklist (collapsible) -----
    checks_performed = sim.get("checks_performed") or []
    checklist_block = ""
    if checks_performed:
        items = "\n".join(f"- {c}" for c in checks_performed)
        checklist_block = (
            f"\n<details>\n<summary>{len(checks_performed)} checks performed</summary>\n\n"
            f"{items}\n\n</details>"
        )

    scorecard_block = _build_scorecard_block(pr_eval)

    # ----- assemble -----
    parts: list[str] = []
    parts.append(" · ".join(lede_parts))
    if impl_complete:
        parts.append("\n" + "\n".join(f"> {ln}" for ln in impl_complete.splitlines()))
    parts.append("\n---\n")
    parts.append("## Design\n")
    parts.append(settled_body.rstrip())
    parts.append("\n---\n")
    parts.append("## SIM review\n")
    summary = (sim.get("summary") or "").rstrip()
    if summary:
        parts.append(summary)
    if revision_lines:
        parts.append("\n" + "\n".join(revision_lines))
    if checklist_block:
        parts.append(checklist_block)
    if scorecard_block:
        parts.append("\n---\n")
        parts.append(scorecard_block)
    parts.append(f"\n---\n\n`{paths.run_id}` · diff `{diff_hash[:16]}…`\n")

    return title, "\n".join(parts)


def _read_impl_complete(marker_path: Path) -> str:
    """Return the agent's one-line summary, or "" if the marker is missing.

    The marker is written by the agent as the last step of WORK (per
    initial_prompt.md). Content is free-form prose; we trim trailing
    whitespace but otherwise preserve what the agent wrote.
    """

    if not marker_path.exists():
        return ""
    try:
        return marker_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _build_scorecard_block(pr_eval: dict | None) -> str:
    """Collapsed `<details>` summarising hard gates + reviewer discipline.

    Adds signal not visible in the lede line: hard-gate pass/fail and the
    `self_verified` / `settled_before_code` booleans from the eval
    scorecard. Returns "" if eval data is missing (e.g. older runs).
    """

    if not isinstance(pr_eval, dict):
        return ""
    hard_gates = pr_eval.get("hard_gates")
    scorecard = pr_eval.get("scorecard") or {}
    if not hard_gates and not scorecard:
        return ""

    lines: list[str] = []
    if hard_gates:
        mark = "✓" if hard_gates == "PASS" else "✗"
        lines.append(f"- Hard gates: {mark} {hard_gates}")
    sim_conf = scorecard.get("sim_confidence")
    extra_conf = scorecard.get("extra_reviewer_confidence")
    cross_family = scorecard.get("cross_family_agreement")
    if sim_conf is not None or extra_conf is not None:
        bits = []
        if sim_conf is not None:
            bits.append(f"sim {sim_conf:.2f}")
        if extra_conf is not None:
            bits.append(f"extra {extra_conf:.2f}")
        if cross_family is True:
            bits.append("cross-family agreement")
        elif cross_family is False:
            bits.append("cross-family disagreement")
        lines.append(f"- Reviewer confidence: {' · '.join(bits)}")
    discipline_bits: list[str] = []
    if scorecard.get("self_verified") is not None:
        discipline_bits.append(f"self-verified {'✓' if scorecard['self_verified'] else '✗'}")
    if scorecard.get("settled_before_code") is not None:
        discipline_bits.append(
            f"settled-before-code {'✓' if scorecard['settled_before_code'] else '✗'}"
        )
    if discipline_bits:
        lines.append(f"- Agent discipline: {' · '.join(discipline_bits)}")
    if not lines:
        return ""
    items = "\n".join(lines)
    return f"<details>\n<summary>Eval scorecard</summary>\n\n{items}\n\n</details>"


def _extract_url(stdout: str) -> str | None:
    for token in stdout.split():
        if token.startswith("http://") or token.startswith("https://"):
            return token
    return stdout.strip() or None
