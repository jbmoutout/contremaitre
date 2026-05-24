Run the `/improve-codebase-architecture` skill end-to-end against this repository. The SWE you're paired with is the user the skill talks to. Follow the skill for everything; the four host-side scaffolds below override skill defaults where they conflict.

**Host owns git.** Never run `git status`, `git add`, `git commit`, `git push`, or `gh`. You have no credentials. The host stages, commits, scans, pushes, and opens the draft PR after the review pass approves. Your job is file edits in the worktree.

**HTML architecture-review goes in the worktree.** If the skill emits an HTML report, write it via the `write` tool to `.contremaitre/architecture-review.html` (not `/tmp/...`, not `$TMPDIR`, not via a bash heredoc). The host preserves it as a run artifact; the SIM browses it via the read-only worktree mount.

**Write `.contremaitre/SETTLED_DESIGN.md` before implementing.** Capture the seam, what sits behind it, the PR sequence, and any load-bearing constraints from grilling. The review pass diffs the implementation against this file.

**Write `.contremaitre/IMPLEMENTATION_COMPLETE` last, with a one-line summary.** This is the terminal signal — the host hands off to review when it appears. Before writing it, run the repository's test suite and verify it passes. Do not write this file if tests are failing or were skipped.
