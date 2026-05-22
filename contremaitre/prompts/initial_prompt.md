Run the `/improve-codebase-architecture` skill end-to-end against this repository. The SWE you're paired with is the user the skill talks to.

Four scaffolding rules from the host Contremaitre orchestrator — everything else, follow the skill:

- **Host owns git and GitHub.** Do not run `git status`, `git add`, `git commit`, `git push`, or `gh`. You have no credentials. The host stages, commits, scans, pushes, and opens the draft PR after a separate review pass approves. Your job is file edits in the worktree.
- **Architecture review report goes in the worktree, not `$TMPDIR`.** If the skill emits an HTML architecture-review report, write it to `.contremaitre/architecture-review.html` in the repo (NOT `/tmp/...` or `$TMPDIR`). The host preserves it as a run artifact; the read-only SIM collaborator can browse it via the worktree mount. Use the `write` tool, not a bash heredoc.
- **Capture the settled design.** Once the grilling loop has produced a locked design, write it to `.contremaitre/SETTLED_DESIGN.md` before you start implementing — the seam, what sits behind it, the PR sequence, anything load-bearing. The review pass reads this file to check the diff against. (This isn't part of the skill; it's the handoff artifact Contremaitre needs.)
- **Hand off via marker file.** When implementation matches `.contremaitre/SETTLED_DESIGN.md` and the relevant local checks have been attempted, write `.contremaitre/IMPLEMENTATION_COMPLETE` containing a one-line summary, then stop. That file is the terminal signal handing the run to review. Don't write it early.
