#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time


OUT = os.environ.get(
    "CONTREMAITRE_CLAUDE_STATUSLINE_OUT",
    "/root/.claude/projects/.contremaitre/statusline.jsonl",
)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _pct(value):
    if isinstance(value, (int, float)):
        return f"{value:.0f}"
    return None


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    snapshot = {
        "recorded_at": time.time(),
        "session_id": data.get("session_id"),
        "version": data.get("version"),
        "model": _dict(data.get("model")),
        "rate_limits": _dict(data.get("rate_limits")),
        "context_window": _dict(data.get("context_window")),
        "cost": _dict(data.get("cost")),
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, separators=(",", ":")) + "\n")

    parts = []
    rate_limits = snapshot["rate_limits"]
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        used = _pct(_dict(rate_limits.get(key)).get("used_percentage"))
        if used is not None:
            parts.append(f"{label}:{used}% used")
    print("cmtr " + " ".join(parts) if parts else "cmtr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
