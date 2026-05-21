"""Strict SIM verdict parsing and diff hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .git_utils import GitRepo
from .models import ParsedVerdict, ReviewVerdict


class VerdictParseError(ValueError):
    pass


def parse_sim_verdict(raw: str) -> ParsedVerdict:
    """Parse the exact JSON verdict contract.

    The parser intentionally rejects markdown fences and extra prose. The
    orchestrator can retry a malformed SIM answer, but it must not guess what
    the SIM meant.
    """

    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise VerdictParseError(f"verdict was not strict JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise VerdictParseError("verdict JSON must be an object")

    required = ("verdict", "confidence", "required_changes", "checks_performed", "summary")
    missing = [key for key in required if key not in data]
    if missing:
        raise VerdictParseError(f"verdict missing required field(s): {', '.join(missing)}")

    try:
        verdict = ReviewVerdict(data["verdict"])
    except ValueError as exc:
        raise VerdictParseError(f"unknown verdict: {data['verdict']!r}") from exc

    confidence = data["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise VerdictParseError("confidence must be a number")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise VerdictParseError("confidence must be between 0.0 and 1.0")

    required_changes = _string_list(data["required_changes"], "required_changes")
    checks_performed = _string_list(data["checks_performed"], "checks_performed")
    summary = data["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise VerdictParseError("summary must be a non-empty string")

    return ParsedVerdict(
        verdict=verdict,
        confidence=confidence,
        required_changes=required_changes,
        checks_performed=checks_performed,
        summary=summary,
        raw=raw,
    )


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise VerdictParseError(f"{label} must be a list of strings")
    return value


def diff_bytes(repo: GitRepo, base: str) -> bytes:
    return repo.bytes_output("diff", f"{base}...HEAD")


def diff_hash(repo: GitRepo, base: str) -> str:
    return hashlib.sha256(diff_bytes(repo, base)).hexdigest()


def write_review_diff(repo: GitRepo, base: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(diff_bytes(repo, base))

