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


def _sum_costs(value: Any) -> float:
    if isinstance(value, dict):
        subtotal = 0.0
        for key, child in value.items():
            if key.lower() in {"cost", "cost_usd", "usd", "total_cost"} and isinstance(child, (int, float)):
                subtotal += float(child)
            else:
                subtotal += _sum_costs(child)
        return subtotal
    if isinstance(value, list):
        return sum(_sum_costs(item) for item in value)
    return 0.0

