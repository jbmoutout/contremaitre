"""PR body rendering.

Assembles the draft-PR title and markdown body from a completed run's
artifacts. Pure read: reads JSONL and files from disk; no git, no subprocess,
no disk writes.

The single public entry point is `render_pr_body`. `publisher.py` calls it and
then handles the side-effecting publication steps (disk write + `gh pr create`).
"""

from __future__ import annotations

import re
from pathlib import Path

from .flow_use import compute_phases
from .jsonlog import read_json, read_jsonl
from .models import RunPaths
from .scaffolds import IMPLEMENTATION_COMPLETE_RELPATH, derive_commit_message


def render_pr_body(paths: RunPaths, diff_hash: str) -> tuple[str, str]:
    """Return (title, markdown_body) for the draft PR.

    Title: same shape as the host commit title (first SETTLED line minus
    skill-emitted "Settled design — " prefix) so the PR, the commit, and
    `git log` all agree.
    Body: lede stamp + IMPLEMENTATION_COMPLETE one-liner + SETTLED design
    (headings demoted) + SIM review (summary + collapsible checklist) +
    revision callout if the SIM bounced + eval scorecard + footer seal.
    Self-contained so reviewers don't need to clone the run dir.
    """

    title, settled_body = derive_commit_message(paths.worktree, paths.run_id)
    # `derive_commit_message` appends `\n\n---\nRun: <id>\n` for `git log`
    # readability. The PR body has its own footer with run_id + diff hash,
    # so strip the commit-only trailer to avoid a double separator.
    settled_body = re.sub(r"\n+---\nRun: [^\n]+\n*$", "", settled_body)
    # Demote SETTLED headings 2 levels so the body's own H2 sections
    # (## Design, ## SIM review) stay the top of the visible hierarchy
    # and the SETTLED H1/H2 don't blow up GitHub's rendering.
    settled_body = re.sub(r"^(#{1,4}) ", r"\1## ", settled_body, flags=re.MULTILINE)

    impl_complete = _read_impl_complete(paths.worktree / IMPLEMENTATION_COMPLETE_RELPATH)
    pr_eval = read_json(paths.eval_dir / "pr_eval.json")

    review_cycles = read_jsonl(paths.review_cycles)
    test_runs = read_jsonl(paths.test_runs)

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
