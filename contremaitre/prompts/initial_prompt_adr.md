Run the `/improve-codebase-architecture` skill against this repository as a turn-by-turn dialogue, **entering at its grilling loop** — the design candidate is already chosen and recorded in the ADR at `{adr_path}` in this worktree. Skip the skill's exploration and candidate-report phases; everything the grilling loop prescribes still applies, including the sub-skills it invokes — `/grilling` for the interview discipline, `/domain-modeling` inline as terms sharpen, `/codebase-design` if alternative interfaces need exploring. The ADR is the plan under grill; the SWE you're paired with is the person being interviewed — a reviewer who replies between your turns, not you. Per `/grilling`: one question per turn, with your recommended answer alongside — end your turn with it and wait; don't answer for them or start implementing alone. Otherwise follow the skill; the host scaffolds below override its defaults where they conflict.

**Fact-check the ADR before the interview starts.** `/grilling` tells you to answer from the codebase instead of asking whenever the tree can answer — apply that to the whole ADR up front. It was written against an earlier state of the tree and may have drifted. Read it, then verify every checkable claim against the code as it stands now — file paths, symbol names, line references, counts, "currently X does Y" statements, test citations — and classify each:

- **Confirmed** — matches the tree.
- **Drifted** — was true, but the code has moved (renamed symbol, shifted lines, changed counts). Update the ADR in place so its facts are current; where raw line numbers have rotted, prefer durable references (symbol names) in the correction.
- **Contested** — the tree contradicts it in a way that isn't mere drift, or you can't verify it. Do NOT rewrite these — carry them into grilling.

Correct only **facts** (Context / current-state sections). Never edit the Decision, its rationale, or its Consequences on your own authority — if the fact-check undermines the decision itself, that is a grilling question for the SWE, not an edit.

Open the grill with: (1) the ADR's decision restated in one paragraph, (2) the fact-check summary — confirmed / drifted-and-updated / contested — and (3) your first question with your recommended answer. Contested claims are the first branches of the design tree; start with the sharpest. Then walk the remaining branches, one question per turn, until the design settles.

**Host owns git.** Never run `git status`, `git add`, `git commit`, `git push`, or `gh`. You have no credentials. The host stages, commits, scans, pushes, and opens the draft PR after the review pass approves. Your job is file edits in the worktree.

**Write `.contremaitre/SETTLED_DESIGN.md` before implementing.** Capture the seam, what sits behind it, the PR sequence, and any load-bearing constraints from grilling. Also record that this run is seeded from the ADR at `{adr_path}` and list the factual corrections you made to it — your ADR edits are part of the diff, and the review pass checks the diff against this file.

**Write `.contremaitre/IMPLEMENTATION_COMPLETE` last, with a one-line summary.** This is the terminal signal — the host hands off to review when it appears. Before writing it, run both:

  1. **The repository's test suite** — verify it passes.
  2. **The formatter/lint gates the project's CI enforces** — discover them by reading `.github/workflows/`, `.pre-commit-config.yaml`, `Makefile`, the tool sections of `pyproject.toml` / `package.json`, or contributing docs. If the gate tooling isn't installed in this environment, install it the way the project declares (dev dep group, extras, devDependencies). Run each gate **in check-only mode, or scoped to the files you touched** — never mass-reformat or auto-fix across the worktree, as that pollutes the diff with unrelated changes.

Do not write `IMPLEMENTATION_COMPLETE` if tests fail, are skipped, or any gate reports violations against your changes.
