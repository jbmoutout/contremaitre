"""The Artifact reader — one Module that reads a run's artifact contract.

Every consumer of a run's on-disk streams (the live TUI, the post-hoc viewer,
the eval canary, the PR-body builder) used to re-derive the same reads: a local
`read_jsonl` copy, a local timestamp coercer, and a path-taking phase/cost/token
wrapper. `RunArtifacts` is the single owner of that I/O. It reads each stream at
most once per instance (lazy + memoized) and **composes** the pure interpreters
that already live elsewhere — it does not re-home them:

  - phases  → `flow_use.compute_phases`
  - cost    → `costs.sum_costs_in_events`
  - tokens  → `costs.sum_token_usage_in_events`
  - model   → `models.resolved_model_from_events`

Snapshot semantics: an instance is a point-in-time read. To re-read (the live
TUI on each refresh tick) construct a fresh one — memoization then collapses the
repeated reads within that tick to one per file.

Dependency direction is `run_artifacts → flow_use` (the *pure* `compute_phases`),
never the reverse: `flow_use` stays a leaf so this import is acyclic. The shared
timestamp coercers (`ts_to_ms` / `event_ms`) live in the `jsonlog` leaf for the
same reason — `flow_use` needs them too and cannot import this module.
"""

from __future__ import annotations

import re
from pathlib import Path

from .costs import sum_costs_in_events, sum_token_usage_in_events
from .flow_use import compute_flow_use, compute_phases
from .jsonlog import read_jsonl
from .models import RunPaths, resolved_model_from_events
from .paths import build_run_paths

# Parsers for the `git diff --stat base...HEAD` summary line recorded per
# snapshot in `worktree_state.jsonl` — "N files changed, M insertions(+),
# K deletions(-)". Any clause may be absent (single-file / all-add /
# all-delete diffs), so each is matched independently.
_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed")
_INSERTIONS_RE = re.compile(r"(\d+)\s+insertions?\(\+\)")
_DELETIONS_RE = re.compile(r"(\d+)\s+deletions?\(-\)")


class RunArtifacts:
    """Reader over one run's artifact contract. See module docstring."""

    def __init__(self, paths: RunPaths):
        self.paths = paths
        self._cache: dict[Path, list[dict]] = {}

    @classmethod
    def from_run_dir(cls, run_dir: Path) -> "RunArtifacts":
        """Build from a bare run dir (the TUI holds `run_dir`; the viewer index
        walks a runs root). Derives the file paths via `build_run_paths` so the
        reader never holds a second copy of the artifact filenames — `RunPaths`
        stays the single path registry."""

        return cls(build_run_paths(run_dir.parent, run_dir.name))

    # ---- raw streams (parsed once, memoized per instance) ----

    def _read(self, path: Path) -> list[dict]:
        cached = self._cache.get(path)
        if cached is None:
            cached = read_jsonl(path)
            self._cache[path] = cached
        return cached

    def raw_export(self) -> list[dict]:
        return self._read(self.paths.raw_export)

    def sim_raw_export(self) -> list[dict]:
        return self._read(self.paths.sim_raw_export)

    def guardrail_events(self) -> list[dict]:
        return self._read(self.paths.guardrail_events)

    def review_cycles(self) -> list[dict]:
        return self._read(self.paths.review_cycles)

    def recoveries(self) -> list[dict]:
        return self._read(self.paths.recoveries)

    def test_runs(self) -> list[dict]:
        return self._read(self.paths.test_runs)

    def worktree_state(self) -> list[dict]:
        """Per-snapshot `git diff --stat base...HEAD` rows (`worktree_state.jsonl`)."""

        return self._read(self.paths.worktree_state)

    def cli_review_raw(self, tool: str) -> list[dict]:
        """The post-publish CLI reviewer stream for `tool` (`claude` / `codex`)."""

        return self._read(getattr(self.paths, f"{tool}_review_raw_export"))

    def diffstat(self) -> dict[str, int] | None:
        """Net diff as `{"files", "insertions", "deletions"}`, or None.

        Reads the last `worktree_state.jsonl` snapshot that carries a
        `git diff --stat` summary line and pulls the three counts out of it
        (any clause may be absent — see the module-level regexes). None when
        no snapshot recorded a diffstat (e.g. a run that never produced a
        diff). The single owner of this parse — both viewer render paths
        (per-run overview, index roster + pipeline tab) read through here.
        """

        result: dict[str, int] | None = None
        for row in self.worktree_state():
            diff_stat = row.get("diff_stat") or ""
            files_m = _FILES_RE.search(diff_stat)
            ins_m = _INSERTIONS_RE.search(diff_stat)
            del_m = _DELETIONS_RE.search(diff_stat)
            if files_m or ins_m or del_m:
                result = {
                    "files": int(files_m.group(1)) if files_m else 0,
                    "insertions": int(ins_m.group(1)) if ins_m else 0,
                    "deletions": int(del_m.group(1)) if del_m else 0,
                }
        return result

    # ---- interpretations (compose the pure interpreters over the streams) ----

    def phases(self, *, live: bool = False) -> dict:
        """Grilling / impl / review phase counts. Subsumes the old
        `compute_phases_from_paths`; delegates to the pure `compute_phases`."""

        return compute_phases(
            self.raw_export(), self.guardrail_events(), self.review_cycles(), live=live
        )

    def flow_use(self) -> dict:
        """Agent + SIM tool-use metrics for the run. Composes the pure
        `flow_use.compute_flow_use` over the reader's memoized streams — so the
        `review_cycles` read it shares with `phases()` happens once per
        instance, not the twice the old path-reader incurred."""

        return compute_flow_use(
            agent_events=self.raw_export(),
            sim_events=self.sim_raw_export(),
            guardrails=self.guardrail_events(),
            review_cycles=self.review_cycles(),
        )

    def cost(self) -> float:
        """Recorded cost across both actor streams (agent + SIM). Two-stream by
        design — the orchestrator's cap check must not drop SIM spend."""

        return sum_costs_in_events(self.raw_export(), self.sim_raw_export())

    def token_usage(self) -> dict[str, int]:
        """Token rollup across both actor streams (agent + SIM). Two-stream, as
        the orchestrator's cost report expects."""

        return sum_token_usage_in_events(self.raw_export(), self.sim_raw_export())

    def token_usage_all(self) -> dict[str, int]:
        """Token rollup across EVERY actor stream — agent, SIM, and both
        post-publish CLI reviewers (claude + codex). Distinct from
        `token_usage()` (agent + SIM only), which feeds the orchestrator's
        in-run cost cap *before* any reviewer runs: that gate must not count
        post-publish work. This is the whole-run figure the index roster
        shows. Absent reviewer streams read as empty, so it equals
        `token_usage()` for runs that never published or were never reviewed."""

        return sum_token_usage_in_events(
            self.raw_export(),
            self.sim_raw_export(),
            self.cli_review_raw("claude"),
            self.cli_review_raw("codex"),
        )

    def resolved_model(self, *, sim: bool = False) -> str | None:
        """The model the stream reports it actually ran (claude `system/init`),
        for the agent stream by default or the SIM stream when `sim=True`. None
        when the runtime is silent (codex) or the stream is absent."""

        stream = self.sim_raw_export() if sim else self.raw_export()
        return resolved_model_from_events(stream)
