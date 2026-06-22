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

ACTOR_START = "actor_start"
SUBAGENT_STEP_FINISH_HARVESTED = "subagent_step_finish_harvested"

# ----- Review loop -----

MALFORMED_VERDICT = "malformed_verdict"
REVISION_REQUESTED = "revision_requested"
REVIEW_VERDICT = "review_verdict"
HARD_GATES_CHECKED = "hard_gates_checked"

# ----- Cap trips -----

TURN_CAP = "turn_cap"
WALL_CAP = "wall_cap"
RECORDED_COST_CAP = "recorded_cost_cap"
NO_PROGRESS_CAP = "no_progress_cap"

# Emitted when a budget-cap exit leaves a resumable checkpoint (resume.json +
# preserved worktree + session homes). Carries the `run --continue` hint.
RESUMABLE = "resumable"
# Emitted at the start of a resumed run, before re-entering the WORK loop.
RUN_RESUMED = "run_resumed"
# Recorded (in recoveries.jsonl) when a per-turn resume checkpoint write fails;
# best-effort, never fatal — only the in-flight turn's resumability is lost.
RESUME_CHECKPOINT_FAILED = "resume_checkpoint_failed"

# ----- Executable checks (L1) -----

CHECK_STARTED = "check_started"
CHECK_COMPLETED = "check_completed"

# ----- Deps-volume offline readiness (pre-agent) -----

# Emitted once after the per-run deps volume is provisioned: did the
# operator's check command (or the ecosystem canary) pass on the SAME
# network the agent will face? Catches the warm/run parity gap — deps
# cached with open egress at warm time, but a build backend the runtime
# `uv run` needs is missing and can't be fetched under the locked egress.
DEPS_OFFLINE_ASSERT = "deps_offline_assert"

# ----- Host-side git -----

HOST_COMMIT_CREATED = "host_commit_created"
HOST_COMMIT_SKIPPED = "host_commit_skipped"
SIMULATED_DIFF_DRIFT = "simulated_diff_drift"
IMPLEMENTATION_COMPLETE_CLEARED = "implementation_complete_cleared"

# ----- Publication -----

PUBLICATION_BLOCKED = "publication_blocked"
PUBLISHED = "published"

# ----- Post-publish CLI review loop (claude / codex in Docker) -----

CLI_REVIEW_STARTED = "cli_review_started"
CLI_REVIEW_COMPLETED = "cli_review_completed"
CLI_REVIEW_FAILED = "cli_review_failed"
# Worst-of-N verdict projected onto a GitHub commit status on the published HEAD.
CLI_REVIEW_STATUS = "cli_review_status"
# Loop events: revision triggered when any tool is not LOOKS_GOOD; done when all LOOKS_GOOD.
CLI_REVIEW_LOOP_REVISION = "cli_review_loop_revision"
CLI_REVIEW_LOOP_DONE = "cli_review_loop_done"
CLI_REVIEW_LOOP_EXHAUSTED = "cli_review_loop_exhausted"
CLI_REVIEW_LOOP_BLOCKED = "cli_review_loop_blocked"

# ----- Cleanup -----

WORKTREE_REMOVED = "worktree_removed"

# ----- Infrastructure -----

INFRA_FAILURE = "infra_failure"
PROVIDER_QUOTA_EXHAUSTED = "provider_quota_exhausted"
PROVIDER_TRANSIENT_ERROR = "provider_transient_error"
PROVIDER_TRANSIENT_ERROR_RETRY = "provider_transient_error_retry"
OPENCODE_STALL = "opencode_stall"


# ----- Recovery events (recoveries.jsonl, `kind` key) -----
# Each is mirrored into guardrail_events.jsonl as `recovery_<kind>` by
# _record_recovery so a single tail catches both surfaces.

SQLITE_RECOVERY_SILENT_STALL = "sqlite_recovery_silent_stall"
SIGTERM_EMERGENCY_WRITE = "sigterm_emergency_write"
EXTRACT_FAILED = "extract_failed"
VIEWER_BUILD_FAILED = "viewer_build_failed"
