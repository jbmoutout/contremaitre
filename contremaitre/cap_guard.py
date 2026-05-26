from __future__ import annotations

import time

from . import events
from .costs import estimate_recorded_cost_usd
from .models import Caps
from .jsonlog import write_json


class CapGuard:
    def __init__(self, caps: Caps, started: float):
        self.caps = caps
        self.started = started
        self.turns = 0
        self.no_progress_streak = 0
        self._last_progress_key: tuple[str, str] | None = None
        self._cost: float = 0.0

    def before_turn(self) -> None:
        self.turns += 1

    def record_progress(self, status: str, diff_stat: str, label: str, text: str) -> str:
        key = (status + "\n" + diff_stat, str(len(text.strip())))
        if self._last_progress_key is None or key != self._last_progress_key:
            self.no_progress_streak = 0
            self._last_progress_key = key
            return events.PROGRESS
        self.no_progress_streak += 1
        return events.NO_PROGRESS

    def tripped(self, raw_export_path, sim_raw_export_path, cost_report_path, emit) -> str | None:
        wall_minutes = (time.monotonic() - self.started) / 60.0
        if self.turns >= self.caps.max_turns:
            emit(events.TURN_CAP, turns=self.turns)
            return events.TURN_CAP
        if wall_minutes >= self.caps.max_wall_minutes:
            emit(events.WALL_CAP, wall_minutes=wall_minutes)
            return events.WALL_CAP
        recorded_cost = estimate_recorded_cost_usd(raw_export_path, sim_raw_export_path)
        write_json(
            cost_report_path,
            {
                "recorded_cost_usd": recorded_cost,
                "max_cost_usd": self.caps.max_cost_usd,
                "note": "Recorded stream cost only; provider-side limit remains the primary spend guardrail.",
            },
        )
        if recorded_cost >= self.caps.max_cost_usd:
            emit(
                events.RECORDED_COST_CAP,
                recorded_cost_usd=recorded_cost,
                max_cost_usd=self.caps.max_cost_usd,
            )
            return events.RECORDED_COST_CAP
        if self.no_progress_streak >= self.caps.no_progress_turns:
            emit(
                events.NO_PROGRESS_CAP,
                no_progress_streak=self.no_progress_streak,
                no_progress_turns=self.caps.no_progress_turns,
            )
            return events.NO_PROGRESS_CAP
        return None

    def add_cost(self, delta: float) -> None:
        self._cost += delta
