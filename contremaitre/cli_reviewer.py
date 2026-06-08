"""Post-publish CLI reviewer.

After the Draft PR lands, optionally invoke a locally-installed frontier CLI
(`claude` or `codex`) on the operator's interactive subscription to read the
diff and post a code-review comment on the PR. The subscription path avoids
the API quota the SIM consumes.

Boundaries:
  - Detection is `shutil.which("claude" | "codex")`; nothing else.
  - Output streams line-by-line into a per-tool `<tool>_review_raw_export.jsonl`
    sink matching the existing `*_raw_export.jsonl` pattern (agent, sim).
    One sink per run.
  - The reviewer container receives host-built PR context under `/review:ro`;
    it does not receive GitHub credentials and does not fetch GitHub.
  - Host-side comment posting shells out to `gh pr comment <url> --body-file <path>`.
    Any failure is logged but does NOT raise — the PR is already published
    and a missed review comment is recoverable by the human reviewer.
  - The worst-of-N verdict is also projected onto a GitHub commit status on
    the published HEAD (`gh api .../statuses/{sha}`, context
    `contremaitre/cli-review`): MUST_FIX → failure, else success. This puts a
    ✓/✗ in the PR's merge box so a draft that earned MUST_FIX is glanceably
    dead instead of silently rotting. PAT-viable (unlike the App-only Checks
    API). Informational until the operator requires the context in branch
    protection, which then gates merge. Best-effort, same as comment posting.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .jsonlog import append_jsonl


VALID_TOOLS = ("codex", "claude")
VALID_CHOICES = VALID_TOOLS + ("none",)


# ---------- detection + resolution (CLI launch-screen helpers) ----------


def detect_available() -> dict[str, str]:
    """Return a mapping of installed CLI tool name → absolute binary path.

    Empty when neither is on PATH.
    """

    available: dict[str, str] = {}
    for name in VALID_TOOLS:
        found = shutil.which(name)
        if found:
            available[name] = found
    return available


def resolve_choice(
    *,
    flag_value: str,
    available: dict[str, str],
    tty: bool,
    saved_default: str | None = None,
    input_fn=input,
    print_fn=print,
) -> str:
    """Decide which tool the orchestrator should run.

    Honors an explicit `--cli-reviewer codex|claude|none`. When
    `auto` (the default), falls back to:
      - neither installed → "none" (silent)
      - one installed + TTY → confirm prompt
      - both installed + TTY → numbered picker
      - no TTY → "none" (we can't ask)
    Returns one of `"codex"`, `"claude"`, `"none"`.

    `saved_default` is the operator's saved preference from
    defaults.toml (not a CLI flag). When set, the interactive picker's
    Enter behavior is "accept saved_default" instead of "skip". The
    operator can still override numerically. Ignored when `flag_value`
    is anything but `"auto"` (explicit always wins).
    """

    import sys

    if flag_value in VALID_TOOLS:
        if flag_value not in available:
            print_fn(
                f"  cli-reviewer: '{flag_value}' is not installed on PATH — skipping",
                file=sys.stderr,
            )
            return "none"
        return flag_value
    if flag_value == "none":
        return "none"
    # flag_value == "auto" (or any unrecognised value treated as auto)
    if not available:
        return "none"
    if not tty:
        return "none"
    if len(available) == 1:
        tool = next(iter(available))
        # `saved_default == "none"` flips Enter from Y to N. Any other
        # saved value (the tool itself, or unset) keeps the
        # historical Y default — the saved-value semantics is "we WANT
        # cli-review," and Enter accepting that is the right behavior.
        if saved_default == "none":
            try:
                reply = input_fn(f"  cli-review with {tool} after publish? [y/N] ").strip().lower()
            except EOFError:
                return "none"
            return tool if reply in ("y", "yes") else "none"
        try:
            reply = input_fn(f"  cli-review with {tool} after publish? [Y/n] ").strip().lower()
        except EOFError:
            return "none"
        return tool if reply in ("", "y", "yes") else "none"
    # Both installed — numbered picker.
    # `saved_default` (when one of codex/claude) becomes the Enter
    # default; otherwise Enter still skips.
    enter_default: str | None = None
    if saved_default in VALID_TOOLS:
        enter_default = saved_default
    print_fn("  cli-review:")
    for i, tool in enumerate(VALID_TOOLS, start=1):
        if tool in available:
            marker = "  ← saved" if tool == enter_default else ""
            print_fn(f"    [{i}] {tool}{marker}")
    print_fn("    [s] skip")
    enter_label = enter_default if enter_default else "skip"
    while True:
        try:
            reply = input_fn(f"  pick (Enter={enter_label}): ").strip().lower()
        except EOFError:
            return "none"
        if reply == "":
            return enter_default if enter_default else "none"
        if reply in ("s", "skip"):
            return "none"
        if reply.isdigit():
            idx = int(reply) - 1
            if 0 <= idx < len(VALID_TOOLS) and VALID_TOOLS[idx] in available:
                return VALID_TOOLS[idx]
        print_fn(f"  enter 1, 2, s, or Enter")


# ---------- prompt assembly ----------


def build_prompt(
    *,
    pr_url: str,
    round_n: int = 1,
    round_of: int = 1,
    review_path: str = "/review",
) -> str:
    """The prompt handed to the CLI reviewer in Docker.

    Body lives in `contremaitre/prompts/cli_reviewer_prompt.md` so it can
    be tuned without touching Python. The MD file has `{round_n}`,
    `{round_of}`, and `{review_path}` placeholders. `pr_url` remains part of
    the function signature because the caller records it in guardrails and
    host-side posting still uses it, but it is not injected into the reviewer
    instructions: GitHub access is host-owned.
    """

    from .prompts import CLI_REVIEWER_PROMPT

    return CLI_REVIEWER_PROMPT.format(
        round_n=round_n,
        round_of=round_of,
        review_path=review_path,
    )


# ---------- comment posting ----------


def post_comment(
    *,
    pr_url: str,
    body_path: Path,
    git_log: Path,
) -> tuple[bool, str]:
    """Post the review body as a single comment on the PR via `gh pr comment`.

    Logs the command + outcome into `git_log` (the same file the publisher
    uses) so the activity is observable post-mortem. Returns
    `(success, message)`; the orchestrator records both into its event log.
    """

    cmd = ["gh", "pr", "comment", pr_url, "--body-file", str(body_path)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        append_jsonl(
            git_log,
            {
                "cmd": cmd,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
                "publisher": "cli_reviewer",
            },
        )
        return False, str(exc)
    append_jsonl(
        git_log,
        {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "publisher": "cli_reviewer",
        },
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f"exit {proc.returncode}"
    return True, proc.stdout.strip()


VERDICT_KEYS = ("MUST_FIX", "NEEDS_ATTENTION", "LOOKS_GOOD")


def extract_required_changes(markdown: str) -> list[str]:
    """Extract numbered items from the `## Required changes` section.

    The prompt specifies: numbered list, each item is `path:line — description`.
    Returns the text of each numbered item (number stripped). Empty when the
    section is absent or contains no numbered items.
    """

    in_section = False
    items: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## required changes"):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("## "):
                break
            if stripped and stripped[0].isdigit() and ". " in stripped[:4]:
                text = stripped.split(". ", 1)[1].strip()
                if text:
                    items.append(text)
    return items


def parse_verdict(markdown: str) -> str | None:
    """Return the verdict key (MUST_FIX / NEEDS_ATTENTION / LOOKS_GOOD).

    The prompt enforces `<glyph> <KEY> — <justification>` on line 1, so
    the first non-blank line carries one of the three SCREAMING_SNAKE_CASE
    keys. We scan a few extra lines defensively in case the agent prepends
    stray whitespace — the verdict needs to be findable even if the agent
    isn't 100% compliant with the format spec. The keys are mutually
    disjoint substrings, so a containment check is unambiguous.
    """

    for line in markdown.lstrip().splitlines()[:5]:
        for key in VERDICT_KEYS:
            if key in line:
                return key
    return None


# ---------- commit-status projection ----------
#
# The cli_review verdict is otherwise only visible as line 1 of a PR comment
# (easy to miss in a long conversation) and as a run-local TUI glyph. Projecting
# it onto a GitHub *commit status* puts a ✓/✗ in the PR's merge box, so a draft
# that earned MUST_FIX is glanceably dead instead of silently rotting.
#
# The status is purely informational UNTIL the operator requires the context in
# branch protection — at which point a MUST_FIX `failure` actually gates merge.
# That makes GitHub branch protection the on/off switch; contremaitre always
# reports, the repo decides whether the report blocks.

# Stable context string so branch protection can require exactly this check.
CLI_REVIEW_STATUS_CONTEXT = "contremaitre/cli-review"

# Severity order: a higher rank is a worse verdict. Used for worst-of-N across
# multiple review rounds — any MUST_FIX wins.
_VERDICT_RANK = {"LOOKS_GOOD": 0, "NEEDS_ATTENTION": 1, "MUST_FIX": 2}


def worst_verdict(verdicts: list[str | None]) -> str | None:
    """Return the highest-severity verdict among `verdicts`.

    Ignores `None` (a reviewer that failed or produced an unparseable
    verdict). Returns `None` only when nothing parseable is present.
    `MUST_FIX` > `NEEDS_ATTENTION` > `LOOKS_GOOD`.
    """

    ranked = [v for v in verdicts if v in _VERDICT_RANK]
    if not ranked:
        return None
    return max(ranked, key=lambda v: _VERDICT_RANK[v])


def verdict_commit_state(verdict: str | None) -> str:
    """Map a verdict to a GitHub commit-status `state`.

    Only `MUST_FIX` blocks (`failure`). `NEEDS_ATTENTION` is non-blocking by
    definition, `LOOKS_GOOD` is clean, and an unparseable/missing verdict
    (`None`) passes too — we never deadlock a required check on a reviewer
    that crashed or drifted from the format. The nuance for the non-failure
    cases lives in the status `description`, not the state.
    """

    return "failure" if verdict == "MUST_FIX" else "success"


def _owner_repo_from_url(pr_url: str) -> str | None:
    """Extract `owner/repo` from a GitHub PR URL.

    `https://github.com/OWNER/REPO/pull/N` → `"OWNER/REPO"`. Returns `None`
    if the URL doesn't have the expected shape (the caller then skips the
    status — best-effort, like every other cli_review side effect).
    """

    marker = "github.com/"
    idx = pr_url.find(marker)
    if idx == -1:
        return None
    tail = pr_url[idx + len(marker) :].strip("/")
    parts = tail.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}/{parts[1]}"


def post_commit_status(
    *,
    pr_url: str,
    sha: str,
    verdict: str | None,
    description: str,
    git_log: Path,
    target_url: str | None = None,
) -> tuple[bool, str]:
    """Publish a commit status reflecting the cli_review verdict.

    Uses `gh api .../statuses/{sha}` (POST is implied by the `-f` fields),
    which a plain PAT with repo scope can do — unlike the Checks API, which
    needs a GitHub App. Best-effort: logs the command + outcome into
    `git_log` and returns `(success, message)`; never raises, since the PR
    is already published and a missed status is recoverable.
    """

    owner_repo = _owner_repo_from_url(pr_url)
    if owner_repo is None:
        return False, f"could not derive owner/repo from {pr_url!r}"

    state = verdict_commit_state(verdict)
    cmd = [
        "gh",
        "api",
        f"repos/{owner_repo}/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        f"context={CLI_REVIEW_STATUS_CONTEXT}",
        # GitHub caps the status description at 140 chars.
        "-f",
        f"description={description[:140]}",
    ]
    if target_url:
        cmd.extend(["-f", f"target_url={target_url}"])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        append_jsonl(
            git_log,
            {
                "cmd": cmd,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
                "publisher": "cli_reviewer",
            },
        )
        return False, str(exc)
    append_jsonl(
        git_log,
        {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "publisher": "cli_reviewer",
        },
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f"exit {proc.returncode}"
    return True, state


def extract_model(tool: str, jsonl_path: Path) -> str | None:
    """Best-effort model-name extraction from the streamed JSONL.

    Codex emits `model: <name>` in its session preamble (lands as a `text`
    event in our sink); we grep for it. Claude doesn't print its model in
    `-p` mode, so for claude we just return None and the footer omits the
    model line — the operator's `~/.claude/.credentials.json` decides it.
    """

    if tool != "codex" or not jsonl_path.exists():
        return None
    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = (event.get("part") or {}).get("text", "")
            if isinstance(text, str) and text.startswith("model:"):
                return text.split(":", 1)[1].strip() or None
    except OSError:
        return None
    return None


def format_header(
    *, tool: str, model: str | None, duration_s: float, round_n: int = 1, round_of: int = 1
) -> str:
    """Tool + model + round + duration line prepended to the posted PR comment.

    H3 (`###`) — visible enough to give context (who reviewed, what model,
    which round) without competing with the H1/H2 space.
    """

    parts = [f"reviewed by `{tool}`"]
    if model:
        parts.append(f"`{model}`")
    parts.append(f"round {round_n}/{round_of}")
    parts.append(_fmt_duration_short(duration_s))
    return "### " + " · ".join(parts) + "\n\n"


def _fmt_duration_short(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m {s}s" if s else f"{m}m"


def expand_choice(choice: str) -> tuple[str, ...]:
    """Resolve a `cli_reviewer` config value to the list of tools to run.

    Single-tool choices return a 1-tuple; `"none"` / unknown return `()`.
    Used by the orchestrator to iterate over the right set of tools.
    """

    if choice in VALID_TOOLS:
        return (choice,)
    return ()


def jsonl_sink_for(paths, tool: str) -> Path:
    """Return the per-tool JSONL sink under the run dir.

    `paths` is a `RunPaths`; the dependency is unimported here to keep the
    module free of orchestrator cycles.
    """

    if tool == "claude":
        return paths.claude_review_raw_export
    if tool == "codex":
        return paths.codex_review_raw_export
    raise ValueError(f"unknown cli tool: {tool}")
