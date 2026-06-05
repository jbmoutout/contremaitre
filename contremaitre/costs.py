"""Recorded-cost extraction from actor JSONL streams.

Provider-side limits remain the real spend guardrail. This module only reads
cost/usage values present in opencode-style JSON events so the orchestrator can
stop when recorded spend crosses the configured cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def estimate_recorded_cost_usd(*paths: Path) -> float:
    total = 0.0
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += _sum_costs(event)
    return round(total, 6)


def sum_costs_in_events(*event_lists: list[dict[str, Any]]) -> float:
    """Sum recorded costs across one or more already-parsed event lists.

    Same semantics as `estimate_recorded_cost_usd` but avoids re-reading
    JSONL files when callers (the TUI) already have parsed events in hand.
    """

    total = 0.0
    for events in event_lists:
        for event in events:
            total += _sum_costs(event)
    return round(total, 6)


def sum_token_usage(*paths: Path) -> dict[str, int]:
    """Sum token counts across actor streams, both event shapes.

    Handles opencode `step_finish` (`part.tokens`) and codex `--json`
    `turn.completed` (`usage`). Returns input/output/reasoning/cache_read
    totals. Codex on a subscription has no metered USD, so this token rollup —
    not a dollar figure — is the usage signal surfaced for CLI runs.
    """

    totals = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "step_finish":
                tok = (event.get("part") or {}).get("tokens") or {}
                totals["input"] += int(tok.get("input", 0) or 0)
                totals["output"] += int(tok.get("output", 0) or 0)
                totals["reasoning"] += int(tok.get("reasoning", 0) or 0)
                cache = tok.get("cache") or {}
                totals["cache_read"] += int(cache.get("read", 0) or 0)
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
                totals["input"] += int(usage.get("input_tokens", 0) or 0)
                totals["output"] += int(usage.get("output_tokens", 0) or 0)
                totals["reasoning"] += int(usage.get("reasoning_output_tokens", 0) or 0)
                totals["cache_read"] += int(usage.get("cached_input_tokens", 0) or 0)
    return totals


def _sum_costs(value: Any) -> float:
    if isinstance(value, dict):
        subtotal = 0.0
        for key, child in value.items():
            if key.lower() in {"cost", "cost_usd", "usd", "total_cost"} and isinstance(
                child, (int, float)
            ):
                subtotal += float(child)
            else:
                subtotal += _sum_costs(child)
        return subtotal
    if isinstance(value, list):
        return sum(_sum_costs(item) for item in value)
    return 0.0
