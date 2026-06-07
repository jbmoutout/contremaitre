"""Post-publish CLI reviewer.

After the Draft PR lands, optionally invoke a locally-installed frontier CLI
(`claude` or `codex`) on the operator's interactive subscription to read the
diff and post a code-review comment on the PR. The subscription path avoids
the API quota the SIM + extra reviewers consume.

Boundaries:
  - Detection is `shutil.which("claude" | "codex")`; nothing else.
  - The subprocess env clears `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` so the
    CLI cannot silently fall through to paid API usage if the operator has
    those vars set in their shell.
  - Output streams line-by-line into a per-tool `<tool>_review_raw_export.jsonl`
    sink matching the existing `*_raw_export.jsonl` pattern (agent, sim,
    extra). One sink per run.
  - Comment posting shells out to `gh pr comment <url> --body-file <path>`.
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
import sys
from dataclasses import dataclass
from pathlib import Path

from .jsonlog import append_jsonl, append_text_event


VALID_TOOLS = ("codex", "claude")
# `both` runs codex AND claude back-to-back on the same PR and posts two
# separate comments (one per tool). Only offered as a picker option when
# both binaries are installed; honored by `RunConfig.cli_reviewer="both"`
# in the orchestrator.
VALID_CHOICES = VALID_TOOLS + ("both", "none")
SUBPROCESS_TIMEOUT_S = 600

# Path the orchestrator stashes its per-run scaffolds under (SETTLED_DESIGN.md,
# IMPLEMENTATION_COMPLETE, architecture-review.html, etc.). The committed
# diff excludes them via a `:(exclude).contremaitre` pathspec; we additionally
# hide them from `git status` for the cli_review subprocess so codex/claude
# don't see phantom "untracked" files that aren't part of the PR.
_SCAFFOLD_EXCLUDE_PATTERN = ".contremaitre/"


@dataclass(frozen=True)
class ReviewResult:
    tool: str
    markdown: str
    exit_code: int
    error: str | None = None


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

    Honors an explicit `--cli-reviewer codex|claude|both|none`. When
    `auto` (the default), falls back to:
      - neither installed → "none" (silent)
      - one installed + TTY → confirm prompt
      - both installed + TTY → numbered picker incl. a `both` option
      - no TTY → "none" (we can't ask)
    Returns one of `"codex"`, `"claude"`, `"both"`, `"none"`.

    `both` requires BOTH binaries on PATH; an explicit `--cli-reviewer both`
    against a partial install degrades to whichever tool is available
    (and warns), matching the explicit-single-tool fallback behavior.

    `saved_default` is the operator's saved preference from
    defaults.toml (not a CLI flag). When set, the interactive picker's
    Enter behavior is "accept saved_default" instead of "skip". The
    operator can still override numerically. Ignored when `flag_value`
    is anything but `"auto"` (explicit always wins).
    """

    if flag_value in VALID_TOOLS:
        if flag_value not in available:
            print_fn(
                f"  cli-reviewer: '{flag_value}' is not installed on PATH — skipping",
                file=sys.stderr,
            )
            return "none"
        return flag_value
    if flag_value == "both":
        if len(available) == 2:
            return "both"
        if len(available) == 1:
            tool = next(iter(available))
            print_fn(
                f"  cli-reviewer: 'both' requested but only '{tool}' on PATH — using it alone",
                file=sys.stderr,
            )
            return tool
        return "none"
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
        # saved value (the tool itself, "both", or unset) keeps the
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
    # Both installed — numbered picker incl. a `both` option that runs
    # the two reviewers sequentially and posts two PR comments.
    # `saved_default` (when one of codex/claude/both) becomes the Enter
    # default; otherwise Enter still skips.
    enter_default: str | None = None
    if saved_default in VALID_TOOLS or saved_default == "both":
        enter_default = saved_default
    print_fn("  cli-review:")
    for i, tool in enumerate(VALID_TOOLS, start=1):
        if tool in available:
            marker = "  ← saved" if tool == enter_default else ""
            print_fn(f"    [{i}] {tool}{marker}")
    both_index = len(VALID_TOOLS) + 1
    both_marker = "  ← saved" if enter_default == "both" else ""
    print_fn(f"    [{both_index}] both  (run codex and claude, post 2 PR comments){both_marker}")
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
            if int(reply) == both_index:
                return "both"
        print_fn(f"  enter 1, 2, {both_index}, s, or Enter")


# ---------- prompt assembly ----------


def build_prompt(*, pr_url: str, round_n: int = 1, round_of: int = 1) -> str:
    """The prompt handed to the CLI reviewer in Docker.

    Body lives in `contremaitre/prompts/cli_reviewer_prompt.md` so it can
    be tuned without touching Python. The MD file has `{pr_url}`, `{round_n}`,
    and `{round_of}` placeholders.

    Pointing the reviewer at a PR URL lets it fetch the full diff, surrounding
    files, CI status, and linked issues itself via the local `gh` CLI.
    """

    from .prompts import CLI_REVIEWER_PROMPT

    return CLI_REVIEWER_PROMPT.format(pr_url=pr_url, round_n=round_n, round_of=round_of)


# ---------- worktree prep ----------


def hide_orchestrator_scaffolds(worktree: Path) -> None:
    """Suppress `.contremaitre/*` from `git status` in the cli_review cwd.

    The orchestrator's host commit excludes `.contremaitre/` via a
    `:(exclude).contremaitre` pathspec, so the scaffolds (SETTLED_DESIGN.md,
    IMPLEMENTATION_COMPLETE, …) sit in the worktree as uncommitted files.
    The cli_review subprocess runs with `cwd=worktree`, so without this
    hide step `git status` shows phantom "untracked" entries the agent
    might mistake for drift from the PR.

    Writes the pattern to `$GIT_DIR/info/exclude`. Resolves `$GIT_DIR`
    through the `.git` gitlink file when `worktree` was created by
    `git worktree add` (per-worktree exclude, doesn't pollute the shared
    main repo). Best-effort: failures are swallowed since this is purely
    cosmetic and the rest of cli_review still works without it.
    """

    try:
        git_dir = _resolve_git_dir(worktree)
        if git_dir is None:
            return
        exclude_path = git_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        # Line-by-line check, not substring, so a comment containing the
        # pattern doesn't make this falsely think it's already present.
        for line in existing.splitlines():
            if line.strip() == _SCAFFOLD_EXCLUDE_PATTERN:
                return
        with exclude_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"{_SCAFFOLD_EXCLUDE_PATTERN}\n")
    except OSError:
        return


def _resolve_git_dir(worktree: Path) -> Path | None:
    """Return the worktree's `$GIT_DIR`, honoring the gitlink file shape.

    For repos created via `git worktree add`, `worktree/.git` is a text
    file like `gitdir: /path/to/main/.git/worktrees/<name>` rather than a
    directory. We follow the pointer so we write to the PER-WORKTREE
    exclude, not the shared main one.
    """

    gitlink = worktree / ".git"
    if gitlink.is_file():
        try:
            content = gitlink.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not content.startswith("gitdir:"):
            return None
        target = content.split(":", 1)[1].strip()
        return Path(target)
    if gitlink.is_dir():
        return gitlink
    return None


# ---------- subprocess invocation ----------


def _scrubbed_env() -> dict[str, str]:
    """Copy of os.environ with API-key vars cleared.

    Keeps the CLI on its OAuth subscription. Setting to `""` rather than
    deleting is intentional — both CLIs treat empty as unset, and a few
    shell wrappers re-export keys on subprocess spawn unless the empty
    value is present to override.
    """

    env = dict(os.environ)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        if key in env:
            env[key] = ""
    return env


def _command_for(
    tool: str,
    prompt: str,
    *,
    final_message_path: Path | None = None,
) -> list[str]:
    """Headless invocation for each supported CLI.

    Both tools default to interactive-trust sandboxes that block the
    review work we actually want done (running tests, exploring the
    worktree). The flags below relax that to what's needed for a code
    review on the operator's own machine:

    - **codex**: `--sandbox workspace-write` lets commands write to the
      worktree; `--add-dir ~/.cache` covers ecosystem caches (`uv`, `pip`,
      `npm`, …) so `uv run pytest` works. Anything outside `~/.cache` and
      the worktree stays read-only — matches the "verify the diff, don't
      reach into the rest of $HOME" intent. `-o <final_message_path>`
      writes only the agent's final message to disk; stdout still gets the
      full session transcript (useful for the TUI live view), but the
      posted PR comment reads from this file instead, sparing the human
      ~100 KB of tool-call dumps.
    - **claude**: `--permission-mode bypassPermissions` — required for
      headless `-p` mode, otherwise the Bash tool prompts for approval on
      stdin and the subprocess hangs invisibly. The risk profile mirrors
      the user's own interactive `claude` session. `-p` already writes
      ONLY the final response to stdout, so no extra final-message flag
      is needed.
    """

    if tool == "claude":
        return [
            "claude",
            "-p",
            "--permission-mode",
            "bypassPermissions",
            prompt,
        ]
    if tool == "codex":
        cache_dir = str(Path.home() / ".cache")
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--add-dir",
            cache_dir,
        ]
        if final_message_path is not None:
            cmd.extend(["-o", str(final_message_path)])
        cmd.append(prompt)
        return cmd
    raise ValueError(f"unknown cli tool: {tool}")


def run_review(
    *,
    tool: str,
    prompt: str,
    jsonl_path: Path,
    cwd: Path | None = None,
    timeout_s: int = SUBPROCESS_TIMEOUT_S,
) -> ReviewResult:
    """Spawn the CLI tool headlessly and stream stdout into `jsonl_path`.

    Each output line is appended as a text event so the TUI can render
    them progressively (same shape as `raw_export.jsonl`). Returns the
    collected markdown + exit code; the orchestrator decides whether to
    post it as a comment.

    `cwd` should be the freshly-published branch's worktree — both
    `claude -p` and `codex exec` then resolve their tools (Bash, file
    reads, `gh`) against the right checkout.
    """

    if tool not in VALID_TOOLS:
        return ReviewResult(tool=tool, markdown="", exit_code=-1, error=f"unknown tool: {tool}")

    # Codex's stdout is the full session transcript (every tool call, every
    # sed/cat/gh output). The actual review is the final agent message;
    # `-o <path>` writes only that to disk. We still stream stdout to the
    # JSONL so the TUI sees live progress, but the posted PR comment reads
    # from this file. Lives next to the JSONL for easy inspection.
    final_message_path: Path | None = None
    if tool == "codex":
        final_message_path = jsonl_path.parent / "codex_final_message.md"

    cmd = _command_for(tool, prompt, final_message_path=final_message_path)
    env = _scrubbed_env()
    collected: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(cwd) if cwd else None,
            text=True,
            bufsize=1,
        )
    except (OSError, FileNotFoundError) as exc:
        return ReviewResult(tool=tool, markdown="", exit_code=-1, error=str(exc))

    assert proc.stdout is not None
    timed_out = False
    try:
        with proc.stdout as stream:
            for line in stream:
                collected.append(line)
                append_text_event(
                    jsonl_path,
                    role=f"{tool}_review",
                    phase="post_publish",
                    text=line.rstrip("\n"),
                )
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        timed_out = True

    # Codex: prefer the `-o` file (clean final message) over the streamed
    # stdout (full transcript). Falls back to stdout if the file is missing,
    # which happens when codex crashes before writing it (auth error, etc.).
    if final_message_path is not None and final_message_path.exists():
        try:
            markdown = final_message_path.read_text(encoding="utf-8")
        except OSError:
            markdown = "".join(collected)
    else:
        markdown = "".join(collected)

    if timed_out:
        return ReviewResult(
            tool=tool,
            markdown=markdown,
            exit_code=-1,
            error=f"timeout after {timeout_s}s",
        )

    return ReviewResult(
        tool=tool,
        markdown=markdown,
        exit_code=proc.returncode,
        error=None if proc.returncode == 0 else f"exit code {proc.returncode}",
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
# multiple reviewers (`cli_reviewer="both"`) — any MUST_FIX wins.
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

    `"both"` expands to `("claude", "codex")` (claude first so its
    comment lands above codex's in the PR conversation — purely cosmetic
    ordering). Single-tool choices return a 1-tuple; `"none"` / unknown
    return `()`. Used by the orchestrator to iterate over the right set
    of subprocess invocations.
    """

    if choice == "both":
        return ("claude", "codex")
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
