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
SUBPROCESS_TIMEOUT_S = 600


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
    input_fn=input,
    print_fn=print,
) -> str:
    """Decide which tool the orchestrator should run.

    Honors an explicit `--cli-reviewer codex|claude|none`. When `auto`
    (the default), falls back to:
      - neither installed → "none" (silent)
      - one installed + TTY → confirm prompt
      - both installed + TTY → numbered picker
      - no TTY → "none" (we can't ask)
    Returns one of `"codex"`, `"claude"`, `"none"`.
    """

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
        try:
            reply = input_fn(f"  cli-review with {tool} after publish? [Y/n] ").strip().lower()
        except EOFError:
            return "none"
        return tool if reply in ("", "y", "yes") else "none"
    # Both installed — numbered picker.
    print_fn("  cli-review:")
    for i, tool in enumerate(VALID_TOOLS, start=1):
        if tool in available:
            print_fn(f"    [{i}] {tool}")
    print_fn("    [s] skip")
    while True:
        try:
            reply = input_fn("  pick (Enter=skip): ").strip().lower()
        except EOFError:
            return "none"
        if reply in ("", "s", "skip"):
            return "none"
        if reply.isdigit():
            idx = int(reply) - 1
            if 0 <= idx < len(VALID_TOOLS) and VALID_TOOLS[idx] in available:
                return VALID_TOOLS[idx]
        print_fn("  enter 1, 2, s, or Enter")


# ---------- prompt assembly ----------


def build_prompt(*, pr_url: str) -> str:
    """The prompt handed to `claude -p` / `codex exec`.

    Body lives in `contremaitre/prompts/cli_reviewer_prompt.md` so it can
    be tuned without touching Python (same convention as `initial_prompt.md`
    and the SIM prompts). The MD file has one `{pr_url}` placeholder that
    we substitute here.

    Pointing the agent at a PR URL (rather than pasting the diff) is the
    empirical sweet spot: it can fetch the full diff, surrounding files,
    CI status, and linked issues itself via the local `gh` CLI from
    `cwd=paths.worktree`.
    """

    from .prompts import CLI_REVIEWER_PROMPT

    return CLI_REVIEWER_PROMPT.format(pr_url=pr_url)


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


VERDICT_GLYPHS = ("🟢", "🟠", "🔴")


def parse_verdict(markdown: str) -> str | None:
    """Return the first verdict glyph (🟢 / 🟠 / 🔴) found in `markdown`.

    The prompt enforces "line 1 is exactly one of {🟢 LGTM, 🟠 Needs
    attention, 🔴 Must fix}", so the first non-blank line typically
    carries the verdict. We scan a few extra lines defensively in case
    the agent prepends stray whitespace or a stray blank — the verdict
    needs to be findable even if the agent isn't 100% compliant with
    the format spec.
    """

    for line in markdown.lstrip().splitlines()[:5]:
        stripped = line.strip()
        for glyph in VERDICT_GLYPHS:
            if stripped.startswith(glyph):
                return glyph
    return None


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


def format_header(*, tool: str, model: str | None, duration_s: float) -> str:
    """Tool + model + duration line prepended to the posted PR comment.

    H3 (`###`) — visible enough to give context (who reviewed this PR,
    with what model, how long it took) without competing with the H1/H2
    space. Sits at the top so the human gets the source of the review
    before they read the verdict line.
    """

    parts = [f"reviewed by `{tool}`"]
    if model:
        parts.append(f"`{model}`")
    parts.append(_fmt_duration_short(duration_s))
    return "### " + " · ".join(parts) + "\n\n"


def _fmt_duration_short(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m {s}s" if s else f"{m}m"


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
