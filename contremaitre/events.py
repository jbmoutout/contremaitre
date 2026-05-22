"""Run-event names emitted to guardrail_events.jsonl and recoveries.jsonl.

Single source of truth for event-name strings. Writers (orchestrator, actors,
cleanup paths) and readers (TUI, tests) import the constants here so a rename
breaks at import time instead of silently at runtime.

This is a registry, not a type system. We deliberately keep the heavier
"named RunEventLog with a tagged Event union" refactor on the shelf —
it's worth it only when a second reader appears (see candidate 4 in the
architecture review). The constants below close the hard-coded-key gap
without inventing a Writer/Reader seam that has only one adapter today.
"""

from __future__ import annotations


# ----- Per-turn / loop progression (guardrail_events.jsonl, `event` key) -----

TURN = "turn"
PROGRESS = "progress"
NO_PROGRESS = "no_progress"
WORK_SESSION_END = "work_session_end"

# ----- Actor lifecycle -----

OPENCODE_ACTOR_START = "opencode_actor_start"

# ----- Review loop -----

MALFORMED_VERDICT = "malformed_verdict"
REVISION_REQUESTED = "revision_requested"

# ----- Cap trips -----

TURN_CAP = "turn_cap"
WALL_CAP = "wall_cap"
RECORDED_COST_CAP = "recorded_cost_cap"
NO_PROGRESS_CAP = "no_progress_cap"

# ----- Host-side git -----

HOST_COMMIT_CREATED = "host_commit_created"
HOST_COMMIT_SKIPPED = "host_commit_skipped"
SIMULATED_DIFF_DRIFT = "simulated_diff_drift"
IMPLEMENTATION_COMPLETE_CLEARED = "implementation_complete_cleared"

# ----- Publication -----

PUBLICATION_BLOCKED = "publication_blocked"

# ----- Infrastructure -----

INFRA_FAILURE = "infra_failure"


# ----- Recovery events (recoveries.jsonl, `kind` key) -----
# Each is mirrored into guardrail_events.jsonl as `recovery_<kind>` by
# _record_recovery so a single tail catches both surfaces.

SQLITE_RECOVERY_SILENT_STALL = "sqlite_recovery_silent_stall"
ORPHAN_CONTAINER_KILL = "orphan_container_kill"
SIGTERM_EMERGENCY_WRITE = "sigterm_emergency_write"
VOLUME_REMOVED = "volume_removed"
EXTRACT_FAILED = "extract_failed"
