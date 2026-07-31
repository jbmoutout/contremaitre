"""Post-run pull-request outcomes.

`pr.json` records what the publisher did at run time. This Module owns the
later, mutable human outcome: whether the published PR was merged, rejected, or
is still pending. Refreshes run on the host through `gh`; readers stay entirely
local and degrade to an explicit UNKNOWN state when no refresh is available.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .jsonlog import write_json

PR_OUTCOME_FILENAME = "pr_outcome.json"
PR_OUTCOME_SCHEMA_VERSION = 1
_GH_FIELDS = "state,isDraft,mergedAt,closedAt,reviewDecision,url"


class PrOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"
    NO_PR = "NO_PR"
    DRY_RUN = "DRY_RUN"
    UNKNOWN = "UNKNOWN"


_DISPLAY_BY_OUTCOME = {
    PrOutcome.ACCEPTED: ("accepted PR", "tier-green"),
    PrOutcome.REJECTED: ("rejected PR", "tier-red"),
    PrOutcome.PENDING: ("pending PR", "tier-yellow"),
    PrOutcome.NO_PR: ("no PR", "tier-red"),
    PrOutcome.DRY_RUN: ("dry run", "tier-unknown"),
    PrOutcome.UNKNOWN: ("PR outcome unknown", "tier-unknown"),
}
_MAX_FAILURE_SUMMARIES = 5


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _values_for(outcome: PrOutcome) -> tuple[bool | None, float | None]:
    if outcome is PrOutcome.ACCEPTED:
        return True, 1.0
    if outcome in {PrOutcome.REJECTED, PrOutcome.NO_PR}:
        return False, 0.0
    return None, None


def _display_for(outcome: PrOutcome) -> tuple[str, str]:
    return _DISPLAY_BY_OUTCOME[outcome]


def _with_display_fields(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        outcome = PrOutcome(payload["outcome"])
    except (KeyError, ValueError):
        outcome = PrOutcome.UNKNOWN
    label, tier = _display_for(outcome)
    enriched = dict(payload)
    enriched["label"] = label
    enriched["tier"] = tier
    return enriched


def _record(
    outcome: PrOutcome,
    *,
    pr_url: str | None,
    checked_at: str | None,
    source: str,
    state: str | None = None,
    is_draft: bool | None = None,
    review_decision: str | None = None,
    merged_at: str | None = None,
    closed_at: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    accepted, score = _values_for(outcome)
    label, tier = _display_for(outcome)
    payload: dict[str, Any] = {
        "schema_version": PR_OUTCOME_SCHEMA_VERSION,
        "pr_url": pr_url,
        "outcome": outcome.value,
        "accepted": accepted,
        "score": score,
        "label": label,
        "tier": tier,
        "state": state,
        "is_draft": is_draft,
        "review_decision": review_decision or None,
        "merged_at": merged_at,
        "closed_at": closed_at,
        "checked_at": checked_at,
        "source": source,
    }
    if error:
        payload["error"] = error
    return payload


def outcome_from_github(
    pr_url: str,
    payload: dict[str, Any],
    *,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Normalize `gh pr view --json ...` output into the artifact contract."""

    merged_at = payload.get("mergedAt")
    state = str(payload.get("state") or "").upper() or None
    closed_at = payload.get("closedAt")
    if merged_at or state == "MERGED":
        outcome = PrOutcome.ACCEPTED
    elif state == "CLOSED" or closed_at:
        outcome = PrOutcome.REJECTED
    elif state == "OPEN":
        outcome = PrOutcome.PENDING
    else:
        outcome = PrOutcome.UNKNOWN
    return _record(
        outcome,
        pr_url=pr_url,
        checked_at=checked_at or _now_iso(),
        source="github",
        state=state,
        is_draft=payload.get("isDraft") if isinstance(payload.get("isDraft"), bool) else None,
        review_decision=payload.get("reviewDecision"),
        merged_at=merged_at,
        closed_at=closed_at,
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _last_known_github_outcome(run_dir: Path) -> dict[str, Any] | None:
    cached = _read_json(run_dir / PR_OUTCOME_FILENAME)
    candidate = cached
    if cached is not None and cached.get("outcome") == PrOutcome.UNKNOWN.value:
        nested = cached.get("last_known_outcome")
        candidate = nested if isinstance(nested, dict) else None
    if (
        candidate is None
        or candidate.get("source") != "github"
        or candidate.get("outcome")
        not in {
            PrOutcome.ACCEPTED.value,
            PrOutcome.REJECTED.value,
            PrOutcome.PENDING.value,
        }
    ):
        return None
    snapshot = _with_display_fields(candidate)
    snapshot.pop("last_known_outcome", None)
    return snapshot


def _is_github_pr_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    segments = [part for part in parts.path.split("/") if part]
    return (
        parts.scheme == "https"
        and parts.hostname == "github.com"
        and len(segments) == 4
        and segments[2] == "pull"
        and segments[3].isdigit()
    )


def _local_outcome(run_dir: Path) -> dict[str, Any]:
    pr = _read_json(run_dir / "pr.json")
    if pr is None:
        return _record(
            PrOutcome.UNKNOWN,
            pr_url=None,
            checked_at=None,
            source="local",
            error="pr.json missing or unreadable",
        )

    kind = pr.get("kind")
    url = pr.get("url")
    if kind != "PUBLISHED":
        return _record(
            PrOutcome.NO_PR,
            pr_url=url if isinstance(url, str) else None,
            checked_at=None,
            source="local",
        )
    if pr.get("dry_run") is True:
        return _record(
            PrOutcome.DRY_RUN,
            pr_url=url if isinstance(url, str) else None,
            checked_at=None,
            source="local",
        )
    if _is_github_pr_url(url):
        return _record(
            PrOutcome.PENDING,
            pr_url=url,
            checked_at=None,
            source="local",
            state="OPEN",
            is_draft=True,
        )
    return _record(
        PrOutcome.UNKNOWN,
        pr_url=url if isinstance(url, str) else None,
        checked_at=None,
        source="local",
        error="published run has no supported GitHub PR URL",
    )


def outcome_for_run(run_dir: Path) -> dict[str, Any]:
    """Read the cached outcome, falling back to publication-time local facts."""

    cached = _read_json(run_dir / PR_OUTCOME_FILENAME)
    if (
        cached is not None
        and cached.get("schema_version") == PR_OUTCOME_SCHEMA_VERSION
        and cached.get("outcome") in {item.value for item in PrOutcome}
    ):
        return _with_display_fields(cached)
    return _local_outcome(run_dir)


def _github_outcome(
    pr_url: str,
    *,
    runner: Callable[..., Any],
    checked_at: str,
) -> tuple[dict[str, Any] | None, str | None]:
    cmd = ["gh", "pr", "view", pr_url, "--json", _GH_FIELDS]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr or f"gh exited {proc.returncode}").strip()[-500:]
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return None, f"invalid gh JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "invalid gh JSON: expected object"
    return outcome_from_github(pr_url, payload, checked_at=checked_at), None


def refresh_run_outcomes(
    runs_root: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Refresh every run under `runs_root`, querying each unique PR URL once."""

    timestamp = checked_at or _now_iso()
    run_dirs = [
        path
        for path in sorted(runs_root.iterdir())
        if path.is_dir()
        and not path.name.startswith("_")
        and any((path / name).is_file() for name in ("viewer.html", "stats.json", "pr.json"))
    ]
    by_url: dict[str, list[Path]] = {}
    summary: dict[str, Any] = {item.value.lower(): 0 for item in PrOutcome}
    summary["errors"] = 0
    summary["failures"] = []

    for run_dir in run_dirs:
        local = _local_outcome(run_dir)
        url = local.get("pr_url")
        if local["outcome"] == PrOutcome.PENDING.value and isinstance(url, str):
            by_url.setdefault(url, []).append(run_dir)
            continue
        write_json(run_dir / PR_OUTCOME_FILENAME, local)
        summary[local["outcome"].lower()] += 1

    for pr_url, matching_runs in by_url.items():
        refreshed, error = _github_outcome(pr_url, runner=runner, checked_at=timestamp)
        if refreshed is None:
            summary["errors"] += 1
            if len(summary["failures"]) < _MAX_FAILURE_SUMMARIES:
                summary["failures"].append(
                    {
                        "pr_url": pr_url,
                        "error": error or "GitHub lookup failed",
                    }
                )
            for run_dir in matching_runs:
                last_known = _last_known_github_outcome(run_dir)
                unknown = _record(
                    PrOutcome.UNKNOWN,
                    pr_url=pr_url,
                    checked_at=timestamp,
                    source="github",
                    error=error or "GitHub lookup failed",
                )
                if last_known is not None:
                    unknown["last_known_outcome"] = last_known
                write_json(run_dir / PR_OUTCOME_FILENAME, unknown)
                summary["unknown"] += 1
            continue
        for run_dir in matching_runs:
            write_json(run_dir / PR_OUTCOME_FILENAME, refreshed)
            summary[str(refreshed["outcome"]).lower()] += 1

    return summary
