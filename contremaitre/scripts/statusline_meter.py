#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pty
import select
import signal
import subprocess
import time


BASE = "/root/.claude/projects/.contremaitre"
STATUSLINE = f"{BASE}/statusline.jsonl"
TTY_LOG = f"{BASE}/statusline_meter_tty.log"
SETTINGS = f"{BASE}/settings.json"
PROMPT = os.environ.get("CONTREMAITRE_CLAUDE_METER_PROMPT", "OK")
MODEL = os.environ.get("CONTREMAITRE_CLAUDE_METER_MODEL", "sonnet")
TIMEOUT = float(os.environ.get("CONTREMAITRE_CLAUDE_METER_TIMEOUT_SECONDS", "75"))
# Only count snapshots written after this probe started, so stale data from the
# preceding agent turn's statusLine hook doesn't cause an immediate false-positive.
START_TIME = time.time()


def _has_usage() -> bool:
    try:
        lines = open(STATUSLINE, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            continue
        if snap.get("recorded_at", 0) < START_TIME:
            continue
        rate_limits = snap.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        for window in rate_limits.values():
            if isinstance(window, dict) and isinstance(window.get("used_percentage"), (int, float)):
                return True
    return False


def main() -> int:
    cmd = ["claude", "--settings", SETTINGS]
    if MODEL:
        cmd += ["--model", MODEL]

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        cmd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=os.setsid,
    )
    os.close(slave)

    sent_at: float | None = None
    second_enter = False
    deadline = time.time() + TIMEOUT
    try:
        with open(TTY_LOG, "ab", buffering=0) as log:
            while time.time() < deadline:
                now = time.time()
                if sent_at is None:
                    os.write(master, PROMPT.encode("utf-8") + b"\r")
                    sent_at = now
                elif not second_enter and now - sent_at > 3:
                    # Some terminals require an extra Enter after the prompt is
                    # accepted into the input widget; harmless if already sent.
                    os.write(master, b"\r")
                    second_enter = True

                if _has_usage():
                    return 0

                readable, _, _ = select.select([master], [], [], 0.2)
                if readable:
                    try:
                        log.write(os.read(master, 4096))
                    except OSError:
                        break
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
