Review this pull request: {pr_url}

Write the review as markdown to stdout (the caller posts it verbatim as one PR comment — be the comment, no preamble).

## Strict output format

**Line 1** is exactly: `<glyph> <KEY> — <one-sentence justification>`, where the glyph + KEY pair is one of:
- `🟢 LOOKS_GOOD` — no blocking issues
- `🟠 NEEDS_ATTENTION` — non-blocking concerns or open questions
- `🔴 MUST_FIX` — blocking issues

The KEY is the canonical machine-parseable token; the glyph and one-sentence justification are for the human reader.

**Line 3** is a one-sentence headline of WHAT this PR does. Active voice, concrete subject. Not a file-list, not a press release.
**Line 4** is one sentence on WHY it matters — the risk it carries, the win it unlocks, or the constraint it honors. If nothing matters, skip line 4 entirely.

**Then** itemised findings, one per line, each cited with `path/to/file:line`, prefixed with one of (Conventional Comments):
`**issue:**` (blocking) · `**suggestion:**` (non-blocking improvement) · `**nit:**` (minor / style) · `**question:**` (clarification)

Worst-first within the list. Skip categories that have no entries. Be terse — no headings beyond the verdict line, no closing summary.
