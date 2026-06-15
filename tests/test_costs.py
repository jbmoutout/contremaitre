"""Token-usage interpreter — the claude `result` accounting in particular.

The load-bearing case is sub-agents: a claude run's top-level `result.usage`
covers only the main model, while Task-tool sub-agents are accounted under
`modelUsage`. Dropping `modelUsage` silently undercounts every sub-agent token.
"""

from contremaitre.costs import sum_token_usage_in_events


def test_claude_result_sums_all_models_in_modelusage():
    # One `claude -p` invocation: an opus main agent that spawned haiku
    # Explore sub-agents. The haiku usage lives ONLY in modelUsage.
    events = [
        {
            "type": "result",
            "usage": {  # top-level = main model only
                "input_tokens": 3584,
                "output_tokens": 18753,
                "cache_read_input_tokens": 422097,
            },
            "modelUsage": {
                "claude-opus-4-8": {
                    "inputTokens": 5047,
                    "outputTokens": 18771,
                    "cacheReadInputTokens": 422097,
                },
                "claude-haiku-4-5-20251001": {  # the sub-agents
                    "inputTokens": 24177,
                    "outputTokens": 15985,
                    "cacheReadInputTokens": 1123190,
                },
            },
        }
    ]
    totals = sum_token_usage_in_events(events)
    assert totals == {
        "input": 5047 + 24177,
        "output": 18771 + 15985,
        "reasoning": 0,
        "cache_read": 422097 + 1123190,
    }


def test_codex_turn_completed_nets_cache_out_of_input():
    # codex `input_tokens` is the TOTAL prompt (cached included); the cached
    # subset must land in cache_read, not be double-counted in input.
    events = [
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 586105,  # total, includes the 513792 cached
                "cached_input_tokens": 513792,
                "output_tokens": 2539,
                "reasoning_output_tokens": 985,
            },
        }
    ]
    assert sum_token_usage_in_events(events) == {
        "input": 586105 - 513792,  # fresh input only
        "output": 2539,
        "reasoning": 985,
        "cache_read": 513792,
    }


def test_claude_result_falls_back_to_top_level_usage_without_modelusage():
    # Older claude streams (or a turn with no modelUsage) still count.
    events = [
        {
            "type": "result",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_input_tokens": 5,
            },
        }
    ]
    assert sum_token_usage_in_events(events) == {
        "input": 100,
        "output": 10,
        "reasoning": 0,
        "cache_read": 5,
    }
