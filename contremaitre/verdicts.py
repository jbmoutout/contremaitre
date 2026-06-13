"""SIM verdict parsing and diff hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .git_utils import GitRepo
from .models import ParsedVerdict, ReviewVerdict


class VerdictParseError(ValueError):
    pass


def _balanced_json_objects(raw: str) -> list[str]:
    """Yield every top-level balanced JSON object substring in `raw`, in order.

    Uses the stdlib decoder's `raw_decode` at each `{`: a brace that doesn't
    begin a parseable object (e.g. prose like ``{agent, sim}`` or
    ``{"codex","auto"}``) is skipped rather than mistaken for the boundary of
    the real object. Brace-counting alone can't do this — it can't tell a JSON
    object from a set/dict literal mentioned in prose.
    """

    decoder = json.JSONDecoder()
    objects: list[str] = []
    idx = 0
    while True:
        start = raw.find("{", idx)
        if start == -1:
            return objects
        try:
            obj, end = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            objects.append(raw[start:end])
        idx = end if end > start else start + 1


def _extract_json_object(raw: str) -> str | None:
    """Return the JSON object embedded in `raw`.

    LLM judges commonly emit reasoning prose and then the verdict object. That
    prose routinely contains inline braces (set/dict literals like
    ``{agent, sim}``, ``{"codex","auto"}``), so spanning from the first `{` to
    the last `}` slices garbage. Instead we scan for every *parseable* balanced
    object and prefer the one that actually carries a ``verdict`` key, falling
    back to the last object emitted (the judge's final word).

    Return `None` if no candidate is found. The caller still runs the strict
    field/type checks on whatever we return.
    """

    objects = _balanced_json_objects(raw)
    if not objects:
        return None

    for candidate in reversed(objects):
        if '"verdict"' in candidate and "verdict" in json.loads(candidate):
            return candidate
    return objects[-1]


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
