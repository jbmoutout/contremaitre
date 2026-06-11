"""Recorded-cost extraction from actor JSONL streams.

Provider-side limits remain the real spend guardrail. This module only reads
cost/usage values present in opencode-style JSON events so the orchestrator can
stop when recorded spend crosses the configured cap.
"""

from __future__ import annotations

from typing import Any


def sum_costs_in_events(*event_lists: list[dict[str, Any]]) -> float:
    """Sum recorded costs across one or more already-parsed event lists.

    The pure interpreter for recorded cost. Callers that hold paths read the
    streams through the `RunArtifacts` reader and pass the parsed events here.
    """

    total = 0.0
    for events in event_lists:
        for event in events:
            total += _sum_costs(event)
    return round(total, 6)


def sum_token_usage_in_events(*event_lists: list[dict[str, Any]]) -> dict[str, int]:
    """Sum token counts across one or more already-parsed event lists, all shapes.

    The pure interpreter for token usage (mirrors `sum_costs_in_events`). Handles
    opencode `step_finish` (`part.tokens`), codex `--json` `turn.completed`
    (`usage`), and claude `stream-json` `result` (`usage`). Returns
    input/output/reasoning/cache_read totals. A subscription CLI (codex or
    claude) has no metered USD — claude's `total_cost_usd` is a notional
    API-equivalent, deliberately ignored by `sum_costs_in_events` (see
    `_sum_costs`) — so this token rollup, not a dollar figure, is the usage signal.
    """

    totals = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    for events in event_lists:
        for event in events:
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
            elif etype == "result":
                # claude emits one `result` per turn (no separate reasoning token).
                usage = event.get("usage") or {}
                totals["input"] += int(usage.get("input_tokens", 0) or 0)
                totals["output"] += int(usage.get("output_tokens", 0) or 0)
                totals["cache_read"] += int(usage.get("cache_read_input_tokens", 0) or 0)
    return totals


def _sum_costs(value: Any) -> float:
    # NB: `total_cost_usd` (and `costUSD`) from a claude `result` event are
    # deliberately NOT summed. The CLI actor drives claude on the operator's
    # OAuth subscription (apiKeySource=none, ANTHROPIC_API_KEY scrubbed), so that
    # figure is a NOTIONAL API-equivalent, not metered spend — counting it would
    # show a misleading "$" in the TUI footer and could trip the cost cap on a
    # subscription run. claude's real usage signal is the token rollup
    # (`sum_token_usage_in_events`) + the footer's rate-limit window, mirroring codex.
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
