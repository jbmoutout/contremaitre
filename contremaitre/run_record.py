"""Typed, read-side view of one run's artifacts — the Run record.

The orchestrator and evaluator write the artifact contract (`stats.json`,
`review_cycles.jsonl`, …) as loose JSON; this module parses that shape *once* so
every reader (the TUI, the viewer index, flow-use, eval) crosses one interface
instead of re-deriving field names with `.get()` chains. Tolerant by
construction — defaults, never requirements — consistent with the additive
artifact contract (`docs/control-plane.md`) and mirroring
`ModelSpec.from_record`, which it composes for the model-identity fields.

The seam is **parsing, not loading**: `RunStats.from_record` / `ReviewCycle.from_row`
/ `parse_review_cycles` take dicts the caller already holds, so the live TUI keeps
its tail loop and owns *when* to read. `RunRecord.load` is a thin façade for
post-hoc readers (viewer index, eval) over a *finished* run dir — do NOT call it
per live poll; it would re-read the whole run on every refresh and fight the
tail-loop's read ownership (`tui.py` re-reads `review_cycles.jsonl` each frame).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonlog import read_jsonl
from .models import ModelSpec


@dataclass(frozen=True)
class RunStats:
    """The `stats.json` contract, read once.

    `agent_spec` / `sim_spec` are `None` when the run wrote no model identity
    for that role (matching the viewer's historic "absent → skip this run"
    guard); present values route through `ModelSpec.from_record`, the one
    reader that absorbs the canonical dict and the legacy on-disk string.
    """

    run_id: str
    verdict: str | None
    terminal_state: str | None
    actor_mode: str | None
    turns: int | None
    duration_seconds: float | None
    recorded_cost_usd: float | None
    reason: str
    agent_spec: ModelSpec | None
    sim_spec: ModelSpec | None

    @classmethod
    def from_record(cls, obj: Any, *, run_id: str = "") -> "RunStats":
        d = obj if isinstance(obj, dict) else {}
        return cls(
            run_id=d.get("run_id") or run_id,
            verdict=d.get("verdict"),
            terminal_state=d.get("terminal_state"),
            actor_mode=d.get("actor_mode"),
            turns=d.get("turns"),
            duration_seconds=d.get("duration_seconds"),
            recorded_cost_usd=d.get("recorded_cost_usd"),
            reason=(d.get("reason") or "").strip(),
            agent_spec=ModelSpec.from_record(d["agent_model"]) if d.get("agent_model") else None,
            sim_spec=ModelSpec.from_record(d["sim_model"]) if d.get("sim_model") else None,
        )

    def agent_canonical(self) -> tuple[str | None, str | None]:
        """`(name, runtime)` for the agent, or `(None, None)` when absent."""
        return self.agent_spec.canonical() if self.agent_spec else (None, None)

    def sim_canonical(self) -> tuple[str | None, str | None]:
        """`(name, runtime)` for the SIM, or `(None, None)` when absent."""
        return self.sim_spec.canonical() if self.sim_spec else (None, None)


@dataclass(frozen=True)
class ReviewCycle:
    """One `review_cycles.jsonl` row.

    `reviewer` is constant `"sim"` on disk today — both write sites pass it
    literally (`orchestrator._record_review_cycle`), so `is_sim` is always True
    and the SIM filter is forward-looking for cli-reviewer rows that don't exist
    yet. The `or "sim"` default normalises absent/empty to sim (the tolerant
    reading two of the three legacy callers already used); the divergence it
    resolves is unobservable while the field is constant.
    """

    round: int
    reviewer: str
    unavailable: bool
    verdict: str | None
    summary: str
    checks_performed: tuple[str, ...]

    @classmethod
    def from_row(cls, row: Any) -> "ReviewCycle":
        d = row if isinstance(row, dict) else {}
        checks = d.get("checks_performed")
        return cls(
            round=int(d.get("round") or 0),
            reviewer=d.get("reviewer") or "sim",
            unavailable=bool(d.get("unavailable")),
            verdict=d.get("verdict") or None,
            summary=d.get("summary") or "",
            checks_performed=tuple(checks) if isinstance(checks, list) else (),
        )

    @property
    def is_sim(self) -> bool:
        return self.reviewer == "sim"


def parse_review_cycles(rows: Iterable[dict]) -> list[ReviewCycle]:
    """Parse already-read review-cycle dicts into typed rows."""
    return [ReviewCycle.from_row(r) for r in rows]


def sim_cycles(cycles: Iterable[ReviewCycle]) -> list[ReviewCycle]:
    """SIM review rows, excluding `unavailable`.

    The one fold with two real adapters (the viewer's `_review_signals` and
    flow-use's sim-useful-ratio), so it lives behind the seam. Per-consumer
    rollups (`sim_rounds`, `sim_changes`, last-cycle text) and the per-round-N
    lookup (TUI only) stay caller-side — folding them in would manufacture
    hypothetical seams.
    """
    return [c for c in cycles if c.is_sim and not c.unavailable]


@dataclass(frozen=True)
class RunRecord:
    """Whole finished-run view for post-hoc readers (viewer index, eval).

    Composes the same per-artifact parsers `RunStats` / `parse_review_cycles`
    expose. The live TUI must NOT use this — see the module docstring.
    """

    run_dir: Path
    stats: RunStats
    review_cycles: list[ReviewCycle]

    @classmethod
    def load(cls, run_dir: Path) -> "RunRecord":
        stats_obj: Any = None
        stats_path = run_dir / "stats.json"
        if stats_path.exists():
            try:
                stats_obj = json.loads(stats_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stats_obj = None
        return cls(
            run_dir=run_dir,
            stats=RunStats.from_record(stats_obj, run_id=run_dir.name),
            review_cycles=parse_review_cycles(read_jsonl(run_dir / "review_cycles.jsonl")),
        )
