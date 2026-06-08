"""Unit tests for the post-publish CLI reviewer (claude/codex)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from contremaitre import cli_reviewer
from contremaitre.paths import build_run_paths
from contremaitre.tui import _derive_phase


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

    def test_two_installed_enter_picks_one(self):
        # When both are installed, Enter with a saved_default of "codex"
        # accepts the saved tool; no "both" option is offered.
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            saved_default="codex",
            input_fn=lambda _: "",  # Enter = accept saved
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "codex")

    def test_two_installed_numeric_pick_overrides_saved(self):
        # Operator saved "codex" but picks [2] (claude) numerically — numeric wins.
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            saved_default="codex",
            input_fn=lambda _: "2",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "claude")

    def test_two_installed_no_saved_enter_skips(self):
        # No saved default, Enter = skip.
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            saved_default=None,
            input_fn=lambda _: "",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "none")

    def test_two_installed_saved_skip_overrides_to_none(self):
        # saved_default="none" with both installed: Enter skips.
        out = cli_reviewer.resolve_choice(
            flag_value="auto",
            available={"codex": "/bin/codex", "claude": "/bin/claude"},
            tty=True,
            saved_default="none",
            input_fn=lambda _: "",
            print_fn=lambda *a, **k: None,
        )
        self.assertEqual(out, "none")


class SavedDefaultTest(unittest.TestCase):
    """`saved_default` prefills the auto-picker without short-circuiting it."""

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
    def test_single_tool_returns_one_tuple(self):
        self.assertEqual(cli_reviewer.expand_choice("codex"), ("codex",))
        self.assertEqual(cli_reviewer.expand_choice("claude"), ("claude",))

    def test_none_returns_empty(self):
        self.assertEqual(cli_reviewer.expand_choice("none"), ())

    def test_unknown_returns_empty(self):
        self.assertEqual(cli_reviewer.expand_choice("auto"), ())
        self.assertEqual(cli_reviewer.expand_choice("garbage"), ())


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
        # One reviewer clean, one blocking → the block wins.
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

    def test_format_includes_round_context(self):
        header = cli_reviewer.format_header(tool="codex", model=None, duration_s=5, round_n=2, round_of=3)
        self.assertIn("round 2/3", header)

    def test_format_defaults_to_round_1_of_1(self):
        header = cli_reviewer.format_header(tool="codex", model=None, duration_s=5)
        self.assertIn("round 1/1", header)

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


class ExtractRequiredChangesTest(unittest.TestCase):
    """extract_required_changes is on the critical revision path:
    MUST_FIX verdict → parsed items → cli_revision_followup prompt.
    Wrong parse = empty or garbled revision instruction.
    """

    def test_extracts_numbered_items_from_section(self):
        md = (
            "🔴 MUST_FIX — two problems\n\n"
            "## Summary\n\nStuff.\n\n"
            "## Required changes\n\n"
            "1. src/foo.py:42 — remove dead branch\n"
            "2. src/bar.py:10 — rename variable\n\n"
            "## Praise\n\nGood tests.\n"
        )
        self.assertEqual(
            cli_reviewer.extract_required_changes(md),
            ["src/foo.py:42 — remove dead branch", "src/bar.py:10 — rename variable"],
        )

    def test_returns_empty_when_section_absent(self):
        md = "🟢 LOOKS_GOOD — all fine\n\n## Summary\n\nGreat work.\n"
        self.assertEqual(cli_reviewer.extract_required_changes(md), [])

    def test_returns_empty_when_section_has_no_numbered_items(self):
        md = "🔴 MUST_FIX\n\n## Required changes\n\nSee inline comments.\n"
        self.assertEqual(cli_reviewer.extract_required_changes(md), [])

    def test_stops_at_next_heading(self):
        md = (
            "## Required changes\n\n"
            "1. fix this\n"
            "2. fix that\n"
            "## Praise\n\n"
            "3. this is praise not a required change\n"
        )
        result = cli_reviewer.extract_required_changes(md)
        self.assertEqual(result, ["fix this", "fix that"])
        self.assertNotIn("this is praise not a required change", result)

    def test_strips_leading_number_and_dot(self):
        md = "## Required changes\n\n1. foo:1 — do X\n"
        self.assertEqual(cli_reviewer.extract_required_changes(md), ["foo:1 — do X"])

    def test_case_insensitive_section_header(self):
        md = "## REQUIRED CHANGES\n\n1. bar:5 — fix Y\n"
        self.assertEqual(cli_reviewer.extract_required_changes(md), ["bar:5 — fix Y"])

    def test_empty_string_returns_empty(self):
        self.assertEqual(cli_reviewer.extract_required_changes(""), [])


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
