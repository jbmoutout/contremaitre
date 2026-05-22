"""Run-event names emitted to guardrail_events.jsonl and recoveries.jsonl.

Single source of truth for event-name strings on the **writer** side
(orchestrator, actors, cleanup paths) and **structural** readers (tests
asserting specific event presence). Rename a constant here, every writer
+ structural reader breaks at import time instead of silently at runtime.

The TUI (`tui.py`) deliberately does NOT import these constants — it
classifies events for **color** by substring pattern (`"recovery" in kind`)
so a new event named `*_recovery_*` gets a sensible default style without
explicit registration. That coupling is permissive on purpose: the TUI is
a best-effort observability tool, not a load-bearing reader. A rename that
removes the substring marker would silently fall back to the default style,
not crash. Acceptable tradeoff for now; revisit if a second non-TUI reader
appears.

We deliberately keep the heavier "named RunEventLog with a tagged Event
union" refactor on the shelf — it's worth it only when a structural reader
appears (eval consumer, programmatic dashboard). The constants below close
the writer-side hard-coded-key gap without inventing a Writer/Reader seam
that has only one adapter today.
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

# ----- Executable checks (L1) -----

CHECK_STARTED = "check_started"
CHECK_COMPLETED = "check_completed"

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
SIGTERM_EMERGENCY_WRITE = "sigterm_emergency_write"
EXTRACT_FAILED = "extract_failed"
VIEWER_BUILD_FAILED = "viewer_build_failed"
