Review this pull request (round {round_n} of {round_of}). Do not call `gh` — no GitHub credentials exist in this container. You may run the project's tests to ground your findings: they run offline against the warmed dependency cache, in a throwaway copy of the worktree (your changes there are discarded — this review emits markdown only).

```diff
{diff}
```

Write the review as markdown to stdout (the caller posts it verbatim as one PR comment — be the comment, no preamble).

## Strict output format

**Line 1** is exactly: `<KEY> — <one-sentence justification>`, where KEY is one of:
- `LOOKS_GOOD` — no issues found
- `NEEDS_ATTENTION` — non-blocking concerns or open questions worth addressing
- `MUST_FIX` — blocking issues that must be resolved

The KEY is the canonical machine-parseable token; the one-sentence justification is for the human reader.

**Line 3** is a one-sentence headline of WHAT this PR does. Active voice, concrete subject. Not a file-list, not a press release.
**Line 4** is one sentence on WHY it matters — the risk it carries, the win it unlocks, or the constraint it honors. If nothing matters, skip line 4 entirely.

**Then** itemised findings, one per line, each cited with `path/to/file:line`, prefixed with one of (Conventional Comments):
`**issue:**` (blocking) · `**suggestion:**` (non-blocking improvement) · `**nit:**` (minor / style) · `**question:**` (clarification)

Worst-first within the list. Skip categories that have no entries. Be terse — no headings beyond the verdict line and Required changes section, no closing summary.

**If KEY is MUST_FIX or NEEDS_ATTENTION**, add a final section after the findings:

```
## Required changes

1. path/to/file:line — imperative description of what to fix
2. path/to/file:line — imperative description of what to fix
```

List every actionable item. Each item must be self-contained (file + line + verb phrase). The agent receiving this list acts on it without seeing the rest of your review.
