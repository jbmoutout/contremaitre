"""SIM verdict parsing and diff hashing."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .git_utils import GitRepo
from .models import ParsedVerdict, ReviewVerdict


class VerdictParseError(ValueError):
    pass


_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def _extract_json_object(raw: str) -> str | None:
    """Return the JSON object embedded in `raw`.

    LLM outputs commonly wrap structured JSON in markdown fences or leading
    prose. We try, in order:

      1. raw stripped (strict path — preserves prior behavior when the SIM
         emits clean JSON);
      2. content of the first ```json / ``` fenced block;
      3. the substring from the first `{` to the matching last `}`.

    Return `None` if no candidate is found. The caller still runs the strict
    field/type checks on whatever we return.
    """

    stripped = raw.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    match = _FENCE_RE.search(raw)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        return raw[start : end + 1]

    return None


def parse_sim_verdict(raw: str) -> ParsedVerdict:
    """Parse the verdict contract.

    The field/type checks remain strict — unknown verdict strings, missing
    fields, malformed types all fail. Only the JSON-extraction layer is
    lenient: we accept fenced or prose-wrapped output as long as a single
    JSON object can be identified. This is the common LLM-as-judge output
    shape; demanding raw JSON-only triples the retry rate without changing
    semantics.
    """

    payload = _extract_json_object(raw)
    if payload is None:
        raise VerdictParseError("verdict did not contain a parseable JSON object")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise VerdictParseError(f"verdict was not valid JSON: {exc}") from exc
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


def diff_hash(repo: GitRepo, base: str) -> str:
    return hashlib.sha256(repo.diff_bytes(base)).hexdigest()


def write_review_diff(repo: GitRepo, base: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(repo.diff_bytes(base))

