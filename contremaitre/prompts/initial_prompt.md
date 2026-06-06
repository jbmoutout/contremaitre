Run the `/improve-codebase-architecture` skill against this repository as a turn-by-turn dialogue. The SWE you're paired with is the person the skill addresses — a reviewer who replies between your turns, not you — so wherever the skill would ask them a question (which candidate to deepen, each grilling round), end your turn with it and wait; don't answer for them or start implementing alone. Otherwise follow the skill; the host scaffolds below override its defaults where they conflict.

**Host owns git.** Never run `git status`, `git add`, `git commit`, `git push`, or `gh`. You have no credentials. The host stages, commits, scans, pushes, and opens the draft PR after the review pass approves. Your job is file edits in the worktree.

**HTML architecture-review goes in the worktree.** If the skill emits an HTML report, write it via the `write` tool to `.contremaitre/architecture-review.html` (not `/tmp/...`, not `$TMPDIR`, not via a bash heredoc). The host preserves it as a run artifact; the SIM browses it via the read-only worktree mount.

**Write `.contremaitre/SETTLED_DESIGN.md` before implementing.** Capture the seam, what sits behind it, the PR sequence, and any load-bearing constraints from grilling. The review pass diffs the implementation against this file.

**Write `.contremaitre/IMPLEMENTATION_COMPLETE` last, with a one-line summary.** This is the terminal signal — the host hands off to review when it appears. Before writing it, run both:

  1. **The repository's test suite** — verify it passes.
  2. **The formatter/lint gates the project's CI enforces** — discover them by reading `.github/workflows/`, `.pre-commit-config.yaml`, `Makefile`, the tool sections of `pyproject.toml` / `package.json`, or contributing docs. If the gate tooling isn't installed in this environment, install it the way the project declares (dev dep group, extras, devDependencies). Run each gate **in check-only mode, or scoped to the files you touched** — never mass-reformat or auto-fix across the worktree, as that pollutes the diff with unrelated changes.

Do not write `IMPLEMENTATION_COMPLETE` if tests fail, are skipped, or any gate reports violations against your changes.
