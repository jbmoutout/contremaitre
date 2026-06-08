"""Unit tests for the post-publish CLI reviewer (claude/codex)."""

from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from contremaitre import cli_reviewer
from contremaitre.paths import build_run_paths
from contremaitre.tui import _derive_phase


SHIM_OK = """#!/bin/sh
echo "## Review"
echo ""
echo "Looks fine."
"""

SHIM_FAIL = """#!/bin/sh
echo "boom" >&2
exit 7
"""

SHIM_ENV_DUMP = """#!/bin/sh
echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY-unset}"
echo "ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN-unset}"
echo "OPENAI_API_KEY=${OPENAI_API_KEY-unset}"
"""


def _shim(tmp: Path, name: str, body: str) -> Path:
    """Drop an executable shim that masquerades as `claude` / `codex`."""

    path = tmp / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class DetectAvailableTest(unittest.TestCase):
    def test_neither_installed(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(cli_reviewer.detect_available(), {})

    def test_only_claude(self):
        def which(name):
            return "/usr/local/bin/claude" if name == "claude" else None

        with mock.patch("shutil.which", side_effect=which):
            self.assertEqual(
                cli_reviewer.detect_available(),
                {"claude": "/usr/local/bin/claude"},
            )

    def test_both_installed(self):
        def which(name):
            return f"/bin/{name}"

        with mock.patch("shutil.which", side_effect=which):
            self.assertEqual(
                cli_reviewer.detect_available(),
                {"codex": "/bin/codex", "claude": "/bin/claude"},
            )


class ResolveChoiceTest(unittest.TestCase):
    def test_explicit_codex_when_installed(self):
        out = cli_reviewer.resolve_choice(
            flag_value="codex",
            available={"codex": "/bin/codex"},
            tty=True,
        )
        self.assertEqual(out, "codex")

    def test_explicit_claude_when_not_installed_falls_to_none(self):
        out = cli_reviewer.resolve_choice(
            flag_value="claude",
            available={},
            tty=True,
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "none")

    def test_explicit_none_short_circuits(self):
        out = cli_reviewer.resolve_choice(
            flag_value="none",
            available={"claude": "/bin/claude"},
            tty=True,
        )
        self.assertEqual(out, "none")

    def test_auto_neither_returns_none(self):
        self.assertEqual(
            cli_reviewer.resolve_choice(flag_value="auto", available={}, tty=True),
            "none",
        )

    def test_auto_no_tty_returns_none(self):
        self.assertEqual(
            cli_reviewer.resolve_choice(
                flag_value="auto",
                available={"claude": "/bin/claude"},
                tty=False,
            ),
            "none",
        )

    def test_auto_one_installed_confirmed(self):
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"claude": "/bin/claude"},
            tty=True,
            input_fn=lambda _: "",  # Enter = accept
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "claude")

    def test_auto_one_installed_declined(self):
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex"},
            tty=True,
            input_fn=lambda _: "n",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "none")

    def test_auto_both_installed_pick_codex(self):
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            input_fn=lambda _: "1",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "codex")

    def test_auto_both_installed_skip(self):
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            input_fn=lambda _: "s",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "none")

    def test_auto_both_installed_pick_both(self):
        # The 3rd option ("both") runs codex and claude back-to-back and
        # posts two PR comments. Surfaced only when both binaries are on
        # PATH so we don't dangle an unselectable option.
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            input_fn=lambda _: "3",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "both")

    def test_explicit_both_when_both_installed(self):
        out = cli_reviewer.resolve_choice(
            flag_value="both",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
        )
        self.assertEqual(out, "both")

    def test_explicit_both_with_only_one_installed_degrades_to_that_one(self):
        # `--cli-reviewer both` should not silently skip when only one
        # tool is present — the operator opted in. Fall back to whichever
        # is available and warn.
        out = cli_reviewer.resolve_choice(
            flag_value="both",
            available={"claude": "/bin/claude"},
            tty=True,
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "claude")

    def test_explicit_both_with_neither_installed_returns_none(self):
        out = cli_reviewer.resolve_choice(
            flag_value="both",
            available={},
            tty=True,
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "none")


class SavedDefaultTest(unittest.TestCase):
    """`saved_default` prefills the auto-picker without short-circuiting it."""

    def test_two_installed_enter_accepts_saved_both(self):
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            saved_default="both",
            input_fn=lambda _: "",  # Enter
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "both")

    def test_two_installed_enter_accepts_saved_codex(self):
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            saved_default="codex",
            input_fn=lambda _: "",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "codex")

    def test_two_installed_saved_default_can_be_overridden_numerically(self):
        # Saved was "both" but operator picks `1` (codex). Numeric pick
        # must still win over the Enter default.
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            saved_default="both",
            input_fn=lambda _: "1",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "codex")

    def test_two_installed_saved_default_can_still_skip(self):
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            saved_default="both",
            input_fn=lambda _: "s",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "none")

    def test_one_installed_saved_none_flips_default_to_n(self):
        # `saved_default="none"` flips Enter from Y to N. Y still works
        # explicitly, but Enter now skips.
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"claude": "/bin/claude"},
            tty=True,
            saved_default="none",
            input_fn=lambda _: "",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "none")

    def test_one_installed_saved_default_y_still_accepts(self):
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"claude": "/bin/claude"},
            tty=True,
            saved_default="claude",
            input_fn=lambda _: "",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "claude")


class ExpandChoiceTest(unittest.TestCase):
    def test_both_expands_to_claude_first_then_codex(self):
        # Claude first so its comment lands above codex's in the PR
        # conversation — purely cosmetic but consistent.
        self.assertEqual(cli_reviewer.expand_choice("both"), ("claude", "codex"))

    def test_single_tool_returns_one_tuple(self):
        self.assertEqual(cli_reviewer.expand_choice("codex"), ("codex",))
        self.assertEqual(cli_reviewer.expand_choice("claude"), ("claude",))

    def test_none_returns_empty(self):
        self.assertEqual(cli_reviewer.expand_choice("none"), ())

    def test_unknown_returns_empty(self):
        self.assertEqual(cli_reviewer.expand_choice("auto"), ())
        self.assertEqual(cli_reviewer.expand_choice("garbage"), ())


class CommandForTest(unittest.TestCase):
    """Lock down sandbox / permission flags so they don't silently regress."""

    def test_codex_runs_with_workspace_write_sandbox_and_cache_dir(self):
        # codex defaults to --sandbox=read-only, which blocks uv/pip/pnpm
        # from initialising their caches under ~/.cache and stops the
        # reviewer from running tests against the diff.
        cmd = cli_reviewer._command_for("codex", "x")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "workspace-write")
        self.assertIn("--add-dir", cmd)
        # cache dir entry should be a HOME-relative .cache path
        cache_entry = cmd[cmd.index("--add-dir") + 1]
        self.assertTrue(cache_entry.endswith("/.cache"))

    def test_codex_uses_output_last_message_when_path_provided(self):
        # Without -o, codex stdout is the full session transcript (every
        # sed/cat/gh tool call) — past runs posted 137 KB comments because
        # we captured stdout verbatim. -o writes only the final message.
        path = Path("/tmp/codex-final.md")
        cmd = cli_reviewer._command_for("codex", "x", final_message_path=path)
        self.assertIn("-o", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], str(path))

    def test_claude_runs_with_bypass_permissions(self):
        # Without bypassPermissions, claude -p blocks waiting for Bash
        # approval on stdin and the subprocess hangs forever.
        cmd = cli_reviewer._command_for("claude", "x")
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertIn("--permission-mode", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")


class BuildPromptTest(unittest.TestCase):
    def test_uses_host_review_bundle_not_pr_url_fetching(self):
        prompt = cli_reviewer.build_prompt(pr_url="https://github.com/x/y/pull/42")
        self.assertIn("/review/diff.patch", prompt)
        self.assertIn("/review/PR.md", prompt)
        self.assertIn("/app", prompt)
        self.assertIn("Do not call `gh`", prompt)
        self.assertNotIn("https://github.com/x/y/pull/42", prompt)

    def test_stays_compact(self):
        # The whole point of the host-mounted context is that we don't pay for
        # an inline diff. Guard the size so a future "helpful" addition
        # doesn't silently put us back in paste-the-diff territory.
        prompt = cli_reviewer.build_prompt(pr_url="https://github.com/x/y/pull/1")
        self.assertLess(len(prompt), 2600)

    def test_specifies_verdict_format(self):
        # Output format locks down the three SCREAMING_SNAKE_CASE keys so
        # machine parsing is unambiguous. No emoji glyphs — just the keys.
        prompt = cli_reviewer.build_prompt(pr_url="https://github.com/x/y/pull/1")
        self.assertNotIn("🟢", prompt)
        self.assertNotIn("🟠", prompt)
        self.assertNotIn("🔴", prompt)
        self.assertIn("LOOKS_GOOD", prompt)
        self.assertIn("NEEDS_ATTENTION", prompt)
        self.assertIn("MUST_FIX", prompt)
        # Conventional-comments labels for the body.
        self.assertIn("**issue:**", prompt)
        self.assertIn("**nit:**", prompt)
        # Required changes section is documented in the prompt.
        self.assertIn("## Required changes", prompt)

    def test_drops_praise_category(self):
        # Praise is dropped from the standard label list so the agent
        # doesn't reach for it by default. Not explicitly forbidden — if
        # the agent finds something genuinely worth noting positively, it
        # can fold it into the headline.
        prompt = cli_reviewer.build_prompt(pr_url="https://github.com/x/y/pull/1")
        self.assertNotIn("**praise:**", prompt)

    def test_summary_is_headline_plus_why(self):
        # The earlier "2-3 sentence read" instruction produced press-release
        # summaries. Headline + why-it-matters lands better with humans.
        prompt = cli_reviewer.build_prompt(pr_url="https://github.com/x/y/pull/1")
        self.assertIn("WHAT this PR does", prompt)
        self.assertIn("WHY it matters", prompt)


class RunReviewTest(unittest.TestCase):
    """Cover the subprocess streaming path with shim executables."""

    def test_streams_stdout_into_jsonl_and_returns_markdown(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            shim = _shim(tmp, "codex", SHIM_OK)
            # Make the shim accept `exec --skip-git-repo-check <prompt>` —
            # /bin/sh discards extra positional args, so the shim doesn't
            # need to parse them.
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ.get('PATH', '')}"}):
                sink = tmp / "out.jsonl"
                result = cli_reviewer.run_review(
                    tool="codex", prompt="please review", jsonl_path=sink
                )
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Looks fine.", result.markdown)
            lines = [json.loads(ln) for ln in sink.read_text().splitlines() if ln.strip()]
            self.assertGreaterEqual(len(lines), 3)
            self.assertEqual(lines[0]["role"], "codex_review")
            self.assertEqual(lines[0]["type"], "text")
            self.assertIn("## Review", lines[0]["part"]["text"])
            del shim  # silence unused

    def test_nonzero_exit_is_surfaced(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            _shim(tmp, "claude", SHIM_FAIL)
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ.get('PATH', '')}"}):
                sink = tmp / "out.jsonl"
                result = cli_reviewer.run_review(tool="claude", prompt="x", jsonl_path=sink)
            self.assertEqual(result.exit_code, 7)
            self.assertIsNotNone(result.error)

    def test_unknown_tool_returns_error(self):
        with TemporaryDirectory() as td:
            sink = Path(td) / "out.jsonl"
            result = cli_reviewer.run_review(tool="bogus", prompt="x", jsonl_path=sink)
            self.assertNotEqual(result.exit_code, 0)
            self.assertIsNotNone(result.error)

    def test_codex_final_message_file_overrides_stdout(self):
        # Codex's stdout is the full session transcript. The posted comment
        # must come from the -o final-message file, NOT from streamed
        # stdout, otherwise the PR comment is 100 KB of tool-call dumps.
        # Shim mimics codex: writes the final review to the path given
        # after `-o`, dumps noise to stdout.
        shim_body = """#!/bin/sh
# Locate the -o argument and write the "real" final message to it.
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then
    printf 'clean review markdown\\n' > "$arg"
    break
  fi
  prev="$arg"
done
echo "session noise line 1"
echo "session noise line 2"
"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            _shim(tmp, "codex", shim_body)
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ.get('PATH', '')}"}):
                sink = tmp / "out.jsonl"
                result = cli_reviewer.run_review(tool="codex", prompt="x", jsonl_path=sink)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.markdown.strip(), "clean review markdown")
            self.assertNotIn("session noise", result.markdown)
            # And the JSONL still got the noise lines for the TUI to render.
            lines = [json.loads(ln) for ln in sink.read_text().splitlines() if ln.strip()]
            joined = " ".join(e["part"]["text"] for e in lines)
            self.assertIn("session noise", joined)

    def test_cwd_is_passed_to_subprocess(self):
        # Worktree cwd is the whole point of switching to URL-only prompts:
        # the agent's Bash / file tools resolve against the published branch's
        # checkout rather than wherever contremaitre was launched from.
        shim_body = """#!/bin/sh
pwd
"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            _shim(tmp, "codex", shim_body)
            workdir = tmp / "worktree"
            workdir.mkdir()
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ.get('PATH', '')}"}):
                sink = tmp / "out.jsonl"
                result = cli_reviewer.run_review(
                    tool="codex", prompt="x", jsonl_path=sink, cwd=workdir
                )
            self.assertEqual(result.exit_code, 0)
            # On macOS /tmp is a symlink to /private/tmp; resolve both sides.
            self.assertEqual(Path(result.markdown.strip()).resolve(), workdir.resolve())

    def test_subscription_safety_blanks_api_keys(self):
        """Operator's ANTHROPIC_API_KEY must NOT leak into the subprocess.

        Otherwise the CLI silently falls through to paid API usage instead
        of the operator's interactive subscription — the whole reason this
        feature exists.
        """

        with TemporaryDirectory() as td:
            tmp = Path(td)
            _shim(tmp, "claude", SHIM_ENV_DUMP)
            env_patch = {
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
                "ANTHROPIC_API_KEY": "should-not-leak",
                # OAuth token: claude CLI prefers this over the API key,
                # so blanking only API_KEY but leaving AUTH_TOKEN set still
                # lands paid API calls instead of subscription usage.
                "ANTHROPIC_AUTH_TOKEN": "auth-token-must-not-leak",
                "OPENAI_API_KEY": "also-should-not-leak",
            }
            with mock.patch.dict(os.environ, env_patch):
                sink = tmp / "out.jsonl"
                result = cli_reviewer.run_review(tool="claude", prompt="x", jsonl_path=sink)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("ANTHROPIC_API_KEY=", result.markdown)
            self.assertIn("ANTHROPIC_AUTH_TOKEN=", result.markdown)
            self.assertNotIn("should-not-leak", result.markdown)
            self.assertNotIn("auth-token-must-not-leak", result.markdown)
            self.assertNotIn("also-should-not-leak", result.markdown)


class ScrubbedEnvTest(unittest.TestCase):
    """Lock the deny set on `_scrubbed_env` so a future refactor that
    removes an entry from the list (e.g. drops `ANTHROPIC_AUTH_TOKEN` by
    mistake) fails loudly instead of silently shipping a leak."""

    def test_blanks_all_known_provider_keys(self):
        env_patch = {
            "ANTHROPIC_API_KEY": "a",
            "ANTHROPIC_AUTH_TOKEN": "b",
            "OPENAI_API_KEY": "c",
            "UNRELATED_KEY": "keep-me",
        }
        with mock.patch.dict(os.environ, env_patch, clear=False):
            env = cli_reviewer._scrubbed_env()
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(env["OPENAI_API_KEY"], "")
        # Unrelated env vars pass through — operator's PATH / HOME / shell
        # plumbing must not get clobbered.
        self.assertEqual(env["UNRELATED_KEY"], "keep-me")

    def test_blanks_present_keys_even_if_some_unset(self):
        # Partial operator env (only one of the three set): the present
        # key still gets blanked, missing ones don't crash.
        env_patch = {"OPENAI_API_KEY": "only-this-one"}
        with mock.patch.dict(os.environ, env_patch, clear=True):
            env = cli_reviewer._scrubbed_env()
        self.assertEqual(env.get("OPENAI_API_KEY"), "")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)


class PostCommentTest(unittest.TestCase):
    def test_logs_command_and_returns_success(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            body = tmp / "review.md"
            body.write_text("# review\n")
            log = tmp / "git_log.jsonl"
            fake_proc = mock.Mock(
                returncode=0, stdout="https://github.com/x/y/pull/1#issuecomment-1", stderr=""
            )
            with mock.patch("subprocess.run", return_value=fake_proc) as srun:
                ok, msg = cli_reviewer.post_comment(
                    pr_url="https://github.com/x/y/pull/1",
                    body_path=body,
                    git_log=log,
                )
            self.assertTrue(ok)
            # Lock the full gh invocation shape: target PR URL is positional,
            # body is passed via `--body-file` (NOT `--body`, which has a
            # length limit + shell-escaping headaches). A regression that
            # swapped to `--body` or dropped the body path entirely would
            # ship empty/truncated comments.
            cmd = srun.call_args.args[0]
            self.assertEqual(cmd[:3], ["gh", "pr", "comment"])
            self.assertIn("https://github.com/x/y/pull/1", cmd)
            self.assertIn("--body-file", cmd)
            self.assertEqual(cmd[cmd.index("--body-file") + 1], str(body))
            self.assertNotIn("--body", [c for c in cmd if c == "--body"])
            entries = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
            self.assertEqual(entries[0]["returncode"], 0)
            self.assertEqual(entries[0]["publisher"], "cli_reviewer")

    def test_failure_logs_and_returns_false(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            body = tmp / "review.md"
            body.write_text("body")
            log = tmp / "git_log.jsonl"
            fake_proc = mock.Mock(returncode=1, stdout="", stderr="auth failed")
            with mock.patch("subprocess.run", return_value=fake_proc):
                ok, msg = cli_reviewer.post_comment(
                    pr_url="https://github.com/x/y/pull/1",
                    body_path=body,
                    git_log=log,
                )
            self.assertFalse(ok)
            self.assertIn("auth failed", msg)


class WorstVerdictTest(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(cli_reviewer.worst_verdict([]))

    def test_all_none_is_none(self):
        # Every reviewer failed/drifted — nothing to project.
        self.assertIsNone(cli_reviewer.worst_verdict([None, None]))

    def test_single(self):
        self.assertEqual(cli_reviewer.worst_verdict(["LOOKS_GOOD"]), "LOOKS_GOOD")

    def test_must_fix_beats_looks_good(self):
        # `both`: one reviewer clean, one blocking → the block wins.
        self.assertEqual(cli_reviewer.worst_verdict(["LOOKS_GOOD", "MUST_FIX"]), "MUST_FIX")

    def test_needs_attention_beats_looks_good(self):
        self.assertEqual(
            cli_reviewer.worst_verdict(["NEEDS_ATTENTION", "LOOKS_GOOD"]),
            "NEEDS_ATTENTION",
        )

    def test_ignores_none_among_real(self):
        self.assertEqual(cli_reviewer.worst_verdict([None, "MUST_FIX"]), "MUST_FIX")


class VerdictCommitStateTest(unittest.TestCase):
    def test_must_fix_blocks(self):
        self.assertEqual(cli_reviewer.verdict_commit_state("MUST_FIX"), "failure")

    def test_needs_attention_passes(self):
        # Non-blocking by definition — must not gate merge.
        self.assertEqual(cli_reviewer.verdict_commit_state("NEEDS_ATTENTION"), "success")

    def test_looks_good_passes(self):
        self.assertEqual(cli_reviewer.verdict_commit_state("LOOKS_GOOD"), "success")

    def test_none_passes(self):
        # An unparseable verdict must never deadlock a required check.
        self.assertEqual(cli_reviewer.verdict_commit_state(None), "success")


class OwnerRepoFromUrlTest(unittest.TestCase):
    def test_extracts_owner_repo(self):
        self.assertEqual(
            cli_reviewer._owner_repo_from_url("https://github.com/octo/widget/pull/42"),
            "octo/widget",
        )

    def test_rejects_non_github(self):
        self.assertIsNone(cli_reviewer._owner_repo_from_url("https://example.com/x/y"))

    def test_rejects_incomplete(self):
        self.assertIsNone(cli_reviewer._owner_repo_from_url("https://github.com/octo"))


class PostCommitStatusTest(unittest.TestCase):
    def test_must_fix_posts_failure_state(self):
        with TemporaryDirectory() as td:
            log = Path(td) / "git_log.jsonl"
            fake_proc = mock.Mock(returncode=0, stdout="{}", stderr="")
            with mock.patch("subprocess.run", return_value=fake_proc) as srun:
                ok, state = cli_reviewer.post_commit_status(
                    pr_url="https://github.com/x/y/pull/1",
                    sha="deadbeef",
                    verdict="MUST_FIX",
                    description="claude MUST_FIX",
                    git_log=log,
                    target_url="https://github.com/x/y/pull/1",
                )
            self.assertTrue(ok)
            self.assertEqual(state, "failure")
            # Lock the gh-api shape: a PAT-viable statuses POST on the right
            # SHA, with the stable context branch protection keys on.
            cmd = srun.call_args.args[0]
            self.assertEqual(cmd[:2], ["gh", "api"])
            self.assertIn("repos/x/y/statuses/deadbeef", cmd)
            self.assertIn("state=failure", cmd)
            self.assertIn(f"context={cli_reviewer.CLI_REVIEW_STATUS_CONTEXT}", cmd)
            self.assertIn("target_url=https://github.com/x/y/pull/1", cmd)
            entries = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
            self.assertEqual(entries[0]["publisher"], "cli_reviewer")

    def test_looks_good_posts_success_state(self):
        with TemporaryDirectory() as td:
            log = Path(td) / "git_log.jsonl"
            fake_proc = mock.Mock(returncode=0, stdout="{}", stderr="")
            with mock.patch("subprocess.run", return_value=fake_proc) as srun:
                ok, state = cli_reviewer.post_commit_status(
                    pr_url="https://github.com/x/y/pull/1",
                    sha="cafe",
                    verdict="LOOKS_GOOD",
                    description="claude LOOKS_GOOD",
                    git_log=log,
                )
            self.assertTrue(ok)
            self.assertEqual(state, "success")
            self.assertIn("state=success", srun.call_args.args[0])

    def test_bad_url_returns_false_without_calling_gh(self):
        with TemporaryDirectory() as td:
            log = Path(td) / "git_log.jsonl"
            with mock.patch("subprocess.run") as srun:
                ok, _ = cli_reviewer.post_commit_status(
                    pr_url="https://example.com/nope",
                    sha="cafe",
                    verdict="MUST_FIX",
                    description="x",
                    git_log=log,
                )
            self.assertFalse(ok)
            srun.assert_not_called()

    def test_description_truncated_to_github_limit(self):
        with TemporaryDirectory() as td:
            log = Path(td) / "git_log.jsonl"
            fake_proc = mock.Mock(returncode=0, stdout="{}", stderr="")
            with mock.patch("subprocess.run", return_value=fake_proc) as srun:
                cli_reviewer.post_commit_status(
                    pr_url="https://github.com/x/y/pull/1",
                    sha="cafe",
                    verdict="LOOKS_GOOD",
                    description="z" * 500,
                    git_log=log,
                )
            cmd = srun.call_args.args[0]
            desc_arg = next(c for c in cmd if c.startswith("description="))
            self.assertLessEqual(len(desc_arg[len("description=") :]), 140)


class ParseVerdictTest(unittest.TestCase):
    """Verdict key drives the TUI footer color — must reflect what the
    agent wrote (line 1 per the prompt), not the subprocess exit code."""

    def test_looks_good(self):
        self.assertEqual(
            cli_reviewer.parse_verdict("🟢 LOOKS_GOOD — no blocking issues\n\nlooks fine\n"),
            "LOOKS_GOOD",
        )

    def test_needs_attention(self):
        self.assertEqual(
            cli_reviewer.parse_verdict("🟠 NEEDS_ATTENTION — non-blocking concerns\n\n…"),
            "NEEDS_ATTENTION",
        )

    def test_must_fix(self):
        self.assertEqual(
            cli_reviewer.parse_verdict(
                "🔴 MUST_FIX — blocking issues found\n\nblocking issue at …"
            ),
            "MUST_FIX",
        )

    def test_returns_none_when_no_key(self):
        # Agent didn't follow format; better to fall back gracefully than
        # crash. TUI maps None → ✓ as a permissive default.
        self.assertIsNone(cli_reviewer.parse_verdict("Looks good\n\n…"))

    def test_tolerates_leading_blanks(self):
        # Agent sometimes emits a stray blank line before the verdict.
        # Scan a few lines defensively per the parser comment.
        self.assertEqual(
            cli_reviewer.parse_verdict("\n\n🟢 LOOKS_GOOD — fine\n\n…"),
            "LOOKS_GOOD",
        )

    def test_key_works_without_glyph(self):
        # The KEY is the canonical machine-parseable token. If the agent
        # drops the glyph but still emits the key, we still classify.
        self.assertEqual(
            cli_reviewer.parse_verdict("MUST_FIX — broken\n\n…"),
            "MUST_FIX",
        )


class HeaderTest(unittest.TestCase):
    """Lock down the metadata header shape (H3 only, includes model when known)."""

    def test_format_includes_tool_and_duration(self):
        header = cli_reviewer.format_header(tool="codex", model=None, duration_s=14)
        self.assertIn("codex", header)
        self.assertIn("14s", header)
        self.assertTrue(header.startswith("### "))
        self.assertNotIn("# reviewed", header.replace("### ", ""))  # not H1
        self.assertNotIn("## reviewed", header.replace("### ", ""))  # not H2

    def test_format_includes_model_when_known(self):
        header = cli_reviewer.format_header(tool="codex", model="gpt-5.5", duration_s=134)
        self.assertIn("gpt-5.5", header)
        self.assertIn("2m 14s", header)

    def test_format_skips_model_when_none(self):
        header = cli_reviewer.format_header(tool="claude", model=None, duration_s=90)
        # No spurious "`None`" or " · `` ·" sneaking into the string.
        self.assertNotIn("None", header)
        self.assertNotIn("``", header)

    def test_extract_model_finds_codex_preamble_line(self):
        with TemporaryDirectory() as td:
            sink = Path(td) / "codex_review_raw_export.jsonl"
            sink.write_text(
                '{"part": {"text": "OpenAI Codex v0.128"}, "type": "text"}\n'
                '{"part": {"text": "model: gpt-5.5"}, "type": "text"}\n'
                '{"part": {"text": "🟢 LOOKS_GOOD — fine"}, "type": "text"}\n'
            )
            self.assertEqual(cli_reviewer.extract_model("codex", sink), "gpt-5.5")

    def test_extract_model_returns_none_for_claude(self):
        # Claude doesn't print its model in -p mode; the helper bails fast.
        with TemporaryDirectory() as td:
            sink = Path(td) / "claude_review_raw_export.jsonl"
            sink.write_text('{"part": {"text": "model: not-extracted"}, "type": "text"}\n')
            self.assertIsNone(cli_reviewer.extract_model("claude", sink))


class HideOrchestratorScaffoldsTest(unittest.TestCase):
    """Suppress .contremaitre/* from `git status` in the cli_review cwd.

    Without this, codex/claude see the orchestrator's per-run scaffolds
    (SETTLED_DESIGN.md, IMPLEMENTATION_COMPLETE, …) as uncommitted files
    and may flag them as drift from the PR.
    """

    def test_appends_to_dir_style_git_info_exclude(self):
        with TemporaryDirectory() as td:
            worktree = Path(td)
            (worktree / ".git" / "info").mkdir(parents=True)
            cli_reviewer.hide_orchestrator_scaffolds(worktree)
            exclude = (worktree / ".git" / "info" / "exclude").read_text()
            self.assertIn(".contremaitre/", exclude)

    def test_idempotent(self):
        # Running cli_review on the same cached repo across runs must not
        # grow the exclude file with duplicates.
        with TemporaryDirectory() as td:
            worktree = Path(td)
            (worktree / ".git" / "info").mkdir(parents=True)
            cli_reviewer.hide_orchestrator_scaffolds(worktree)
            cli_reviewer.hide_orchestrator_scaffolds(worktree)
            cli_reviewer.hide_orchestrator_scaffolds(worktree)
            exclude = (worktree / ".git" / "info" / "exclude").read_text()
            self.assertEqual(exclude.count(".contremaitre/"), 1)

    def test_follows_gitlink_to_per_worktree_exclude(self):
        # `git worktree add` makes `<worktree>/.git` a file pointing to the
        # main repo's worktrees/<name> dir. Writing to that PER-WORKTREE
        # exclude keeps other concurrent runs sharing the cache clean.
        with TemporaryDirectory() as td:
            root = Path(td)
            main_git = root / "main" / ".git"
            per_worktree = main_git / "worktrees" / "run-x"
            per_worktree.mkdir(parents=True)
            worktree = root / "wt"
            worktree.mkdir()
            (worktree / ".git").write_text(f"gitdir: {per_worktree}\n")

            cli_reviewer.hide_orchestrator_scaffolds(worktree)

            self.assertTrue((per_worktree / "info" / "exclude").exists())
            self.assertIn(
                ".contremaitre/",
                (per_worktree / "info" / "exclude").read_text(),
            )
            # Main repo's exclude must NOT have been touched.
            self.assertFalse((main_git / "info" / "exclude").exists())

    def test_preserves_existing_excludes(self):
        # Don't clobber other patterns the operator already had in there.
        with TemporaryDirectory() as td:
            worktree = Path(td)
            info = worktree / ".git" / "info"
            info.mkdir(parents=True)
            (info / "exclude").write_text("# operator's patterns\n*.log\nbuild/\n")
            cli_reviewer.hide_orchestrator_scaffolds(worktree)
            content = (info / "exclude").read_text()
            self.assertIn("*.log", content)
            self.assertIn("build/", content)
            self.assertIn(".contremaitre/", content)

    def test_no_git_dir_is_a_noop(self):
        # Best-effort: cli_review still works without this if the cwd
        # somehow isn't a git checkout. Two invariants: (a) doesn't raise,
        # (b) doesn't fabricate a .git directory or any other files —
        # otherwise we'd silently turn a non-git directory into one.
        with TemporaryDirectory() as td:
            tmp = Path(td)
            before = sorted(tmp.iterdir())
            cli_reviewer.hide_orchestrator_scaffolds(tmp)
            self.assertFalse((tmp / ".git").exists())
            self.assertEqual(sorted(tmp.iterdir()), before)


class JsonlSinkForTest(unittest.TestCase):
    def test_picks_right_sink(self):
        with TemporaryDirectory() as td:
            paths = build_run_paths(Path(td), "20260101-000000-test")
            self.assertEqual(
                cli_reviewer.jsonl_sink_for(paths, "claude").name,
                "claude_review_raw_export.jsonl",
            )
            self.assertEqual(
                cli_reviewer.jsonl_sink_for(paths, "codex").name,
                "codex_review_raw_export.jsonl",
            )
            with self.assertRaises(ValueError):
                cli_reviewer.jsonl_sink_for(paths, "bogus")


class DerivePhaseCliReviewTest(unittest.TestCase):
    """Verify the TUI's phase machine includes the new cli_review state."""

    def test_running_in_cli_review(self):
        phase, color = _derive_phase(
            terminal=False,
            terminal_verdict=None,
            settled=True,
            impl_complete=True,
            agent_started=True,
            architecture_review_done=True,
            sim_started=True,
            review_started=True,
            cli_review_started=True,
            cli_review_completed=False,
        )
        self.assertEqual(phase, "cli_review")
        self.assertEqual(color, "live")

    def test_failed_cli_review_stays_until_completion(self):
        phase, color = _derive_phase(
            terminal=False,
            terminal_verdict=None,
            settled=True,
            impl_complete=True,
            agent_started=True,
            review_started=True,
            cli_review_started=True,
            cli_review_completed=False,
            cli_review_failed=True,
        )
        self.assertEqual(phase, "cli_review")
        self.assertEqual(color, "warn")

    def test_terminal_with_cli_review_failed_downgrades_done_color(self):
        phase, color = _derive_phase(
            terminal=True,
            terminal_verdict="READY_FOR_DRAFT_PR",
            settled=True,
            impl_complete=True,
            agent_started=True,
            review_started=True,
            cli_review_started=True,
            cli_review_completed=False,
            cli_review_failed=True,
        )
        self.assertEqual(phase, "done")
        self.assertEqual(color, "warn")

    def test_terminal_with_cli_review_completed_is_done_ok(self):
        phase, color = _derive_phase(
            terminal=True,
            terminal_verdict="READY_FOR_DRAFT_PR",
            settled=True,
            impl_complete=True,
            agent_started=True,
            review_started=True,
            cli_review_started=True,
            cli_review_completed=True,
        )
        self.assertEqual(phase, "done")
        self.assertEqual(color, "ok")

    def test_terminal_without_cli_review_is_done_ok(self):
        # Backward-compat: a run that didn't enable cli_review still resolves
        # to "done ok" on a successful publish.
        phase, color = _derive_phase(
            terminal=True,
            terminal_verdict="READY_FOR_DRAFT_PR",
            settled=True,
            impl_complete=True,
            agent_started=True,
            review_started=True,
        )
        self.assertEqual(phase, "done")
        self.assertEqual(color, "ok")


if __name__ == "__main__":
    unittest.main()
