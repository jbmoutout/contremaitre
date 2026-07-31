from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from contremaitre.pr_outcomes import (
    PR_OUTCOME_FILENAME,
    PrOutcome,
    outcome_for_run,
    outcome_from_github,
    refresh_run_outcomes,
)


def _write_pr(run_dir: Path, **overrides: object) -> None:
    run_dir.mkdir(parents=True)
    payload = {
        "kind": "PUBLISHED",
        "url": "https://github.com/acme/widgets/pull/7",
        "dry_run": False,
        "publish_mode": "gh",
    }
    payload.update(overrides)
    (run_dir / "pr.json").write_text(json.dumps(payload), encoding="utf-8")


def test_github_merged_pr_is_accepted() -> None:
    outcome = outcome_from_github(
        "https://github.com/acme/widgets/pull/7",
        {
            "state": "MERGED",
            "isDraft": False,
            "mergedAt": "2026-07-30T12:34:56Z",
            "closedAt": "2026-07-30T12:34:56Z",
            "reviewDecision": "APPROVED",
        },
        checked_at="2026-07-31T00:00:00Z",
    )

    assert outcome["outcome"] == PrOutcome.ACCEPTED.value
    assert outcome["accepted"] is True
    assert outcome["score"] == 1.0
    assert outcome["merged_at"] == "2026-07-30T12:34:56Z"


def test_github_closed_unmerged_pr_is_rejected() -> None:
    outcome = outcome_from_github(
        "https://github.com/acme/widgets/pull/7",
        {
            "state": "CLOSED",
            "isDraft": False,
            "mergedAt": None,
            "closedAt": "2026-07-30T12:34:56Z",
            "reviewDecision": "",
        },
        checked_at="2026-07-31T00:00:00Z",
    )

    assert outcome["outcome"] == PrOutcome.REJECTED.value
    assert outcome["accepted"] is False
    assert outcome["score"] == 0.0


def test_open_pr_is_pending_and_unscored() -> None:
    outcome = outcome_from_github(
        "https://github.com/acme/widgets/pull/7",
        {
            "state": "OPEN",
            "isDraft": True,
            "mergedAt": None,
            "closedAt": None,
            "reviewDecision": "",
        },
        checked_at="2026-07-31T00:00:00Z",
    )

    assert outcome["outcome"] == PrOutcome.PENDING.value
    assert outcome["accepted"] is None
    assert outcome["score"] is None


def test_local_no_pr_and_dry_run_are_distinct(tmp_path: Path) -> None:
    no_pr = tmp_path / "no-pr"
    _write_pr(no_pr, kind="NO_PR", url=None, dry_run=True)
    dry_run = tmp_path / "dry-run"
    _write_pr(dry_run, url=None, dry_run=True, publish_mode="stub")

    assert outcome_for_run(no_pr)["outcome"] == PrOutcome.NO_PR.value
    assert outcome_for_run(no_pr)["score"] == 0.0
    assert outcome_for_run(dry_run)["outcome"] == PrOutcome.DRY_RUN.value
    assert outcome_for_run(dry_run)["score"] is None


def test_local_fallback_is_derived_without_a_github_check_timestamp(tmp_path: Path) -> None:
    run_dir = tmp_path / "pending"
    _write_pr(run_dir)

    outcome = outcome_for_run(run_dir)

    assert outcome["source"] == "local"
    assert outcome["checked_at"] is None
    assert outcome["label"] == "pending PR"
    assert outcome["tier"] == "tier-yellow"


def test_refresh_queries_duplicate_url_once_and_writes_every_run(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    first = runs_root / "20260101-000000-first"
    second = runs_root / "20260101-000001-second"
    _write_pr(first)
    _write_pr(second)
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "state": "MERGED",
                    "isDraft": False,
                    "mergedAt": "2026-07-30T12:34:56Z",
                    "closedAt": "2026-07-30T12:34:56Z",
                    "reviewDecision": "APPROVED",
                }
            ),
            stderr="",
        )

    summary = refresh_run_outcomes(
        runs_root,
        runner=runner,
        checked_at="2026-07-31T00:00:00Z",
    )

    assert len(calls) == 1
    assert summary["accepted"] == 2
    for run_dir in (first, second):
        saved = json.loads((run_dir / PR_OUTCOME_FILENAME).read_text(encoding="utf-8"))
        assert saved["outcome"] == PrOutcome.ACCEPTED.value


def test_refresh_failure_does_not_prevent_other_runs(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    failed = runs_root / "20260101-000000-failed"
    healthy = runs_root / "20260101-000001-healthy"
    _write_pr(failed, url="https://github.com/acme/widgets/pull/1")
    _write_pr(healthy, url="https://github.com/acme/widgets/pull/2")
    (failed / PR_OUTCOME_FILENAME).write_text(
        json.dumps(
            outcome_from_github(
                "https://github.com/acme/widgets/pull/1",
                {
                    "state": "MERGED",
                    "isDraft": False,
                    "mergedAt": "2026-07-30T12:34:56Z",
                    "closedAt": "2026-07-30T12:34:56Z",
                    "reviewDecision": "APPROVED",
                },
                checked_at="2026-07-30T13:00:00Z",
            )
        ),
        encoding="utf-8",
    )

    def runner(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if cmd[3].endswith("/1"):
            raise subprocess.TimeoutExpired(cmd, 30)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "state": "OPEN",
                    "isDraft": True,
                    "mergedAt": None,
                    "closedAt": None,
                    "reviewDecision": "",
                }
            ),
            stderr="",
        )

    summary = refresh_run_outcomes(
        runs_root,
        runner=runner,
        checked_at="2026-07-31T00:00:00Z",
    )

    assert summary["errors"] == 1
    assert summary["failures"] == [
        {
            "pr_url": "https://github.com/acme/widgets/pull/1",
            "error": "TimeoutExpired: Command '['gh', 'pr', 'view', "
            "'https://github.com/acme/widgets/pull/1', '--json', "
            "'state,isDraft,mergedAt,closedAt,reviewDecision,url']' timed out after 30 seconds",
        }
    ]
    failed_outcome = outcome_for_run(failed)
    assert failed_outcome["outcome"] == PrOutcome.UNKNOWN.value
    assert failed_outcome["checked_at"] == "2026-07-31T00:00:00Z"
    assert failed_outcome["last_known_outcome"]["outcome"] == PrOutcome.ACCEPTED.value
    assert failed_outcome["last_known_outcome"]["checked_at"] == "2026-07-30T13:00:00Z"
    assert outcome_for_run(healthy)["outcome"] == PrOutcome.PENDING.value

    refresh_run_outcomes(
        runs_root,
        runner=runner,
        checked_at="2026-08-01T00:00:00Z",
    )

    failed_again = outcome_for_run(failed)
    assert failed_again["outcome"] == PrOutcome.UNKNOWN.value
    assert failed_again["checked_at"] == "2026-08-01T00:00:00Z"
    assert failed_again["last_known_outcome"]["outcome"] == PrOutcome.ACCEPTED.value
    assert failed_again["last_known_outcome"]["checked_at"] == "2026-07-30T13:00:00Z"


def test_refresh_persists_unknown_for_legacy_viewer_without_pr_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "20260101-000000-legacy"
    run_dir.mkdir(parents=True)
    (run_dir / "viewer.html").write_text("<html></html>", encoding="utf-8")
    (run_dir / "stats.json").write_text("{}", encoding="utf-8")

    summary = refresh_run_outcomes(
        run_dir.parent,
        checked_at="2026-07-31T00:00:00Z",
    )

    assert summary["unknown"] == 1
    assert (run_dir / PR_OUTCOME_FILENAME).is_file()
    assert outcome_for_run(run_dir)["outcome"] == PrOutcome.UNKNOWN.value
