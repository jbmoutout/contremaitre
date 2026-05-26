Review this pull request: {pr_url}

Write the review as markdown to stdout (the caller posts it verbatim as one PR comment — be the comment, no preamble).

## Strict output format

**Line 1** is exactly one of:
- `🟢 LGTM` — no blocking issues
- `🟠 Needs attention` — non-blocking concerns or open questions
- `🔴 Must fix` — blocking issues

**Line 3** is a one-sentence headline of WHAT this PR does. Active voice, concrete subject. Not a file-list, not a press release.
**Line 4** is one sentence on WHY it matters — the risk it carries, the win it unlocks, or the constraint it honors. If nothing matters, skip line 4 entirely.

**Then** itemised findings, one per line, each cited with `path/to/file:line`, prefixed with one of (Conventional Comments):
`**issue:**` (blocking) · `**suggestion:**` (non-blocking improvement) · `**nit:**` (minor / style) · `**question:**` (clarification)

Worst-first within the list. Skip categories that have no entries. Be terse — no headings beyond the verdict line, no closing summary.
