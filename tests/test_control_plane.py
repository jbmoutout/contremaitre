from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from contremaitre import events
from contremaitre.fixture import init_fixture
from contremaitre.git_utils import GitRepo, GitResult
from contremaitre.models import (
    Caps,
    ParsedVerdict,
    PublishMode,
    ReviewVerdict,
    RunConfig,
    TerminalVerdict,
)
from contremaitre.orchestrator import Orchestrator, run
from contremaitre.publisher import PublishOutcome, PublishOutcomeKind
from contremaitre.scaffolds import SETTLED_RELPATH
from contremaitre.verdicts import diff_hash


class RecordingGitRepo(GitRepo):
    def __init__(self, cwd: Path, log_path: Path | None = None, *, push_returncode: int = 0):
        super().__init__(cwd, log_path)
        self.push_returncode = push_returncode
        self.pushes: list[list[str]] = []

    def run(self, *args: str, check: bool = True) -> GitResult:
        if args and args[0] == "push":
            self.pushes.append(list(args))
            return GitResult(
                args=["git", *args],
                cwd=self.cwd,
                returncode=self.push_returncode,
                stdout="",
                stderr="push failed" if self.push_returncode else "",
            )
        return super().run(*args, check=check)


class ControlPlaneTest(unittest.TestCase):
    def test_approved_run_writes_artifacts_and_stub_pr(self):
        result, runs_root = self._run_fixture(run_slug="approved")

        self.assertEqual(result.verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        pr = self._read_json(result.run_dir / "pr.json")
        self.assertEqual(pr["kind"], "PUBLISHED")
        self.assertTrue(pr["dry_run"])  # stub publisher
        self.assertTrue((result.run_dir / "raw_export.jsonl").exists())
        self.assertTrue((result.run_dir / "sim_raw_export.jsonl").exists())
        self.assertTrue((result.run_dir / "eval" / "pr_eval.json").exists())
        self.assertTrue(result.run_dir.exists())

    def test_changes_requested_is_safe_no_pr(self):
        # Scope to 2 review rounds: one CHANGES_REQUESTED triggers a real
        # revision-followup turn, the second exhausts the cap. The previous
        # version used the default (3 rounds) which ran the loop slower
        # without exercising the revision path more thoroughly.
        result, _ = self._run_fixture(
            run_slug="changes",
            sim_scenario="changes_requested",
            caps=Caps(max_review_rounds=2),
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_CHANGES_REQUESTED)
        pr = self._read_json(result.run_dir / "pr.json")
        self.assertEqual(pr["kind"], "NO_PR")
        # Revision flow fired at least once with the SIM's required_changes
        # surfacing in the guardrail event. A regression that dropped the
        # bullets from the agent's revision prompt would leave the field
        # empty here.
        guardrail_lines = [
            json.loads(line)
            for line in (result.run_dir / "guardrail_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        revisions = [g for g in guardrail_lines if g.get("event") == events.REVISION_REQUESTED]
        self.assertGreaterEqual(len(revisions), 1)
        self.assertEqual(
            revisions[0]["required_changes"],
            ["Add the missing boundary test before review."],
        )

    def test_malformed_verdict_retries_to_needs_human(self):
        result, _ = self._run_fixture(
            run_slug="malformed",
            sim_scenario="malformed",
            caps=Caps(malformed_verdict_retries=1),
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        guardrails = (result.run_dir / "guardrail_events.jsonl").read_text(encoding="utf-8")
        self.assertIn(events.MALFORMED_VERDICT, guardrails)

    def test_forbidden_path_blocks_approved_publication(self):
        result, _ = self._run_fixture(run_slug="forbidden", agent_scenario="forbidden_path")

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        pr_eval = self._read_json(result.run_dir / "eval" / "pr_eval.json")
        self.assertEqual(pr_eval["hard_gates"], "FAIL")
        self.assertIn(".env", pr_eval["hard_gate_details"]["forbidden_files"])

    def test_diff_hash_drift_blocks_publication(self):
        result, _ = self._run_fixture(run_slug="drift", simulate_drift_after_approval=True)

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        pr_eval = self._read_json(result.run_dir / "eval" / "pr_eval.json")
        self.assertFalse(pr_eval["hard_gate_details"]["checks"]["diff_hash_matched"])

    def test_turn_cap_stops_safely(self):
        # Without IMPLEMENTATION_COMPLETE the WORK loop runs until a cap fires.
        result, _ = self._run_fixture(
            run_slug="cap",
            agent_scenario="no_impl_complete",
            caps=Caps(max_turns=1),
        )

        self.assertEqual(result.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        guardrails = (result.run_dir / "guardrail_events.jsonl").read_text(encoding="utf-8")
        self.assertIn(events.TURN_CAP, guardrails)

    def _run_fixture(
        self,
        *,
        run_slug: str,
        sim_scenario: str = "approved",
        agent_scenario: str = "normal",
        simulate_drift_after_approval: bool = False,
        caps: Caps | None = None,
    ):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        runs_root = root / "runs"
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=runs_root,
            run_slug=run_slug,
            check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
            sim_scenario=sim_scenario,
            agent_scenario=agent_scenario,
            simulate_drift_after_approval=simulate_drift_after_approval,
            caps=caps or Caps(),
        )
        return run(config), runs_root

    @staticmethod
    def _read_json(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class CliReviewRevisionGateTest(unittest.TestCase):
    MUST_FIX_REVIEW = (
        "MUST_FIX - blocking issue\n\n"
        "The PR needs a follow-up edit.\n\n"
        "**issue:** README.md:1 needs an update.\n\n"
        "## Required changes\n\n"
        "1. README.md:1 - update the README before the next round\n"
    )
    LOOKS_GOOD_REVIEW = "LOOKS_GOOD - no blocking issues\n\nThe PR is clean.\n"

    def test_cli_revision_reruns_gates_before_push(self):
        orch, worktree_git, branch = self._prepared_orchestrator()
        outcome = self._published_outcome(orch, branch)
        reviews = iter([self.MUST_FIX_REVIEW, self.LOOKS_GOOD_REVIEW])

        def agent_revision(_actor, _message):
            (orch.paths.worktree / "README.md").write_text(
                "# Contremaitre fixture\n\nCLI revision applied.\n",
                encoding="utf-8",
            )
            self._write_marker(orch)
            return "revision complete"

        with (
            mock.patch.object(
                orch, "_run_one_cli_reviewer", side_effect=lambda **_kw: next(reviews)
            ),
            mock.patch.object(orch, "_agent_turn", side_effect=agent_revision),
            mock.patch.object(orch, "_post_cli_review_status"),
            mock.patch("contremaitre.cli_reviewer.post_comment", return_value=(True, "posted")),
        ):
            verdict = orch._run_cli_review_loop(
                worktree_git=worktree_git,
                branch=branch,
                outcome=outcome,
                actor=object(),
            )

        self.assertEqual(verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        self.assertEqual(worktree_git.pushes, [["push", "origin", f"HEAD:{branch}"]])
        guardrails = self._read_jsonl(orch.paths.guardrail_events)
        self.assertTrue(
            any(
                row.get("event") == events.HARD_GATES_CHECKED
                and row.get("context") == "cli_review_revision"
                and row.get("passed") is True
                for row in guardrails
            )
        )

    def test_cli_revision_check_failure_blocks_push(self):
        orch, worktree_git, branch = self._prepared_orchestrator(
            check_cmds=(f'{sys.executable} -c "import sys; sys.exit(1)"',)
        )
        outcome = self._published_outcome(orch, branch)

        def agent_revision(_actor, _message):
            (orch.paths.worktree / "README.md").write_text(
                "# Contremaitre fixture\n\nUnsafe CLI revision.\n",
                encoding="utf-8",
            )
            self._write_marker(orch)
            return "revision complete"

        with (
            mock.patch.object(orch, "_run_one_cli_reviewer", return_value=self.MUST_FIX_REVIEW),
            mock.patch.object(orch, "_agent_turn", side_effect=agent_revision),
            mock.patch.object(orch, "_post_cli_review_status"),
            mock.patch("contremaitre.cli_reviewer.post_comment", return_value=(True, "posted")),
        ):
            verdict = orch._run_cli_review_loop(
                worktree_git=worktree_git,
                branch=branch,
                outcome=outcome,
                actor=object(),
            )

        self.assertEqual(verdict, TerminalVerdict.PR_NEEDS_HUMAN)
        self.assertEqual(worktree_git.pushes, [])
        guardrails = self._read_jsonl(orch.paths.guardrail_events)
        blocked = [row for row in guardrails if row.get("event") == events.CLI_REVIEW_LOOP_BLOCKED]
        self.assertTrue(blocked)
        self.assertEqual(blocked[-1]["reason"], "executable checks failed")

    def test_blocked_cli_review_context_posts_commit_status_before_return(self):
        orch, worktree_git, branch = self._prepared_orchestrator()
        outcome = self._published_outcome(orch, branch)

        with (
            mock.patch.object(
                orch,
                "_write_cli_review_context",
                side_effect=OSError("disk full"),
            ),
            mock.patch.object(orch, "_agent_turn") as agent_turn,
            mock.patch.object(orch, "_post_cli_review_status") as post_status,
        ):
            verdict = orch._run_cli_review_loop(
                worktree_git=worktree_git,
                branch=branch,
                outcome=outcome,
                actor=object(),
            )

        self.assertEqual(verdict, TerminalVerdict.PR_NEEDS_HUMAN)
        agent_turn.assert_not_called()
        post_status.assert_called_once()
        self.assertIn("review context failed", orch._last_cli_review_reason)

    def test_empty_cli_reviewer_output_requires_human_without_revision(self):
        orch, worktree_git, branch = self._prepared_orchestrator()
        outcome = self._published_outcome(orch, branch)

        with (
            mock.patch.object(orch, "_run_one_cli_reviewer", return_value=""),
            mock.patch.object(orch, "_agent_turn") as agent_turn,
            mock.patch.object(orch, "_post_cli_review_status"),
        ):
            verdict = orch._run_cli_review_loop(
                worktree_git=worktree_git,
                branch=branch,
                outcome=outcome,
                actor=object(),
            )

        self.assertEqual(verdict, TerminalVerdict.PR_NEEDS_HUMAN)
        agent_turn.assert_not_called()
        self.assertEqual(worktree_git.pushes, [])
        guardrails = self._read_jsonl(orch.paths.guardrail_events)
        self.assertTrue(
            any(
                row.get("event") == events.CLI_REVIEW_FAILED and row.get("reason") == "empty_output"
                for row in guardrails
            )
        )

    def test_unparseable_cli_reviewer_output_requires_human_without_revision(self):
        orch, worktree_git, branch = self._prepared_orchestrator()
        outcome = self._published_outcome(orch, branch)

        with (
            mock.patch.object(orch, "_run_one_cli_reviewer", return_value="Looks fine to me"),
            mock.patch.object(orch, "_agent_turn") as agent_turn,
            mock.patch.object(orch, "_post_cli_review_status"),
            mock.patch("contremaitre.cli_reviewer.post_comment", return_value=(True, "posted")),
        ):
            verdict = orch._run_cli_review_loop(
                worktree_git=worktree_git,
                branch=branch,
                outcome=outcome,
                actor=object(),
            )

        self.assertEqual(verdict, TerminalVerdict.PR_NEEDS_HUMAN)
        agent_turn.assert_not_called()
        self.assertEqual(worktree_git.pushes, [])
        guardrails = self._read_jsonl(orch.paths.guardrail_events)
        self.assertTrue(
            any(
                row.get("event") == events.CLI_REVIEW_FAILED
                and row.get("reason") == "unparseable_verdict"
                for row in guardrails
            )
        )

    def test_publish_rewrites_eval_when_cli_review_needs_human(self):
        orch, worktree_git, branch = self._prepared_orchestrator()
        approved_hash = diff_hash(worktree_git, orch._diff_base)
        parsed = ParsedVerdict(
            verdict=ReviewVerdict.APPROVED,
            confidence=0.9,
            required_changes=[],
            checks_performed=["read diff"],
            summary="approved",
            raw="{}",
        )

        outcome = self._published_outcome(orch, branch)

        def cli_review_loop(**_kwargs):
            orch._last_cli_review_reason = "post-publish CLI review exhausted"
            return TerminalVerdict.PR_NEEDS_HUMAN

        publisher = SimpleNamespace(publish=mock.Mock(return_value=outcome))
        with (
            mock.patch("contremaitre.orchestrator.make_publisher", return_value=publisher),
            mock.patch.object(orch, "_run_cli_review_loop", side_effect=cli_review_loop),
        ):
            result = orch._publish_or_block(
                worktree_git=worktree_git,
                branch=branch,
                checks=[],
                parsed=parsed,
                approved_hash=approved_hash,
                actor=object(),
            )

        self.assertEqual(result.verdict, TerminalVerdict.PR_NEEDS_HUMAN)
        self.assertEqual(result.reason, "post-publish CLI review exhausted")
        stats = json.loads(orch.paths.stats.read_text(encoding="utf-8"))
        self.assertEqual(stats["verdict"], "PR_NEEDS_HUMAN")
        self.assertEqual(stats["reason"], "post-publish CLI review exhausted")
        pr_eval = json.loads(orch.paths.pr_eval.read_text(encoding="utf-8"))
        self.assertEqual(pr_eval["verdict"], "PR_NEEDS_HUMAN")
        self.assertEqual(pr_eval["hard_gates"], "PASS")
        self.assertEqual(pr_eval["needs_human"], ["post-publish CLI review exhausted"])

    def test_cli_review_context_is_host_built_for_docker_reviewer(self):
        orch, worktree_git, branch = self._prepared_orchestrator()
        outcome = self._published_outcome(orch, branch)
        (orch.paths.worktree / SETTLED_RELPATH).parent.mkdir(parents=True, exist_ok=True)
        (orch.paths.worktree / SETTLED_RELPATH).write_text("## Settled\nDo the thing.\n")
        (orch.paths.run_dir / "pr_body.md").write_text("PR body from host.\n", encoding="utf-8")
        (orch.paths.worktree / "README.md").write_text(
            "# Contremaitre fixture\n\nCLI review context diff.\n",
            encoding="utf-8",
        )
        worktree_git.run("add", "README.md")
        worktree_git.run("commit", "-m", "Prepare review context diff")

        review_dir = orch._write_cli_review_context(
            worktree_git=worktree_git,
            outcome=outcome,
            cli_round=1,
            max_rounds=3,
            extras_dir=orch.paths.run_dir / "extras" / "cli_review_001",
        )

        self.assertTrue((review_dir / "diff.patch").read_text(encoding="utf-8"))
        self.assertIn("README.md", (review_dir / "changed_files.txt").read_text())
        self.assertIn("Do the thing", (review_dir / "SETTLED_DESIGN.md").read_text())
        self.assertIn("PR body from host", (review_dir / "pr_body.md").read_text())
        pr_md = (review_dir / "PR.md").read_text(encoding="utf-8")
        self.assertIn("GitHub access is host-owned", pr_md)
        self.assertIn("`/review/diff.patch`", pr_md)

    def test_cli_reviewer_uses_locked_config_and_review_mount(self):
        orch, _worktree_git, _branch = self._prepared_orchestrator()
        review_dir = orch.paths.run_dir / "extras" / "cli_review_001" / "input"
        review_dir.mkdir(parents=True)

        with mock.patch("contremaitre.cli_actor.CliActorRunner") as runner_cls:
            runner = runner_cls.return_value
            runner.cli_reviewer_turn.return_value = SimpleNamespace(text="LOOKS_GOOD — clean")

            out = orch._run_one_cli_reviewer(
                tool="codex",
                prompt="review /review",
                sink=orch.paths.codex_review_raw_export,
                round_n=1,
                review_dir=review_dir,
            )

        self.assertEqual(out, "LOOKS_GOOD — clean")
        self.assertIs(runner_cls.call_args.kwargs["config"], orch.config)
        self.assertFalse(runner_cls.call_args.kwargs["config"].allow_open_egress)
        runner.cli_reviewer_turn.assert_called_once_with(
            prompt="review /review",
            raw_export=orch.paths.codex_review_raw_export,
            round_n=1,
            review_dir=review_dir,
        )

    def test_max_rounds_exhaustion_returns_pr_needs_human(self):
        orch, worktree_git, branch = self._prepared_orchestrator(max_cli_review_rounds=1)
        outcome = self._published_outcome(orch, branch)

        with (
            mock.patch.object(orch, "_run_one_cli_reviewer", return_value=self.MUST_FIX_REVIEW),
            mock.patch.object(orch, "_agent_turn") as agent_turn,
            mock.patch.object(orch, "_post_cli_review_status"),
            mock.patch("contremaitre.cli_reviewer.post_comment", return_value=(True, "posted")),
        ):
            verdict = orch._run_cli_review_loop(
                worktree_git=worktree_git,
                branch=branch,
                outcome=outcome,
                actor=object(),
            )

        self.assertEqual(verdict, TerminalVerdict.PR_NEEDS_HUMAN)
        agent_turn.assert_not_called()
        self.assertEqual(worktree_git.pushes, [])
        guardrails = self._read_jsonl(orch.paths.guardrail_events)
        self.assertTrue(
            any(row.get("event") == events.CLI_REVIEW_LOOP_EXHAUSTED for row in guardrails)
        )

    def test_multi_tool_round_both_must_fix_then_looks_good_pushes(self):
        orch, worktree_git, branch = self._prepared_orchestrator(max_cli_review_rounds=2)
        outcome = self._published_outcome(orch, branch)

        must_fix_claude = (
            "MUST_FIX - second tool also objects\n\n"
            "## Required changes\n\n"
            "1. src/foo.py:1 - add missing docstring\n"
        )
        reviews = iter(
            [
                self.MUST_FIX_REVIEW,  # round 1, codex
                must_fix_claude,  # round 1, claude
                self.LOOKS_GOOD_REVIEW,  # round 2, codex
                self.LOOKS_GOOD_REVIEW,  # round 2, claude
            ]
        )

        def agent_revision(_actor, _message):
            (orch.paths.worktree / "README.md").write_text(
                "# Contremaitre fixture\n\nCLI multi-tool revision.\n",
                encoding="utf-8",
            )
            self._write_marker(orch)
            return "revision complete"

        with (
            mock.patch("contremaitre.cli_reviewer.expand_choice", return_value=("codex", "claude")),
            mock.patch.object(
                orch, "_run_one_cli_reviewer", side_effect=lambda **_kw: next(reviews)
            ),
            mock.patch.object(orch, "_agent_turn", side_effect=agent_revision),
            mock.patch.object(orch, "_post_cli_review_status"),
            mock.patch("contremaitre.cli_reviewer.post_comment", return_value=(True, "posted")),
        ):
            verdict = orch._run_cli_review_loop(
                worktree_git=worktree_git,
                branch=branch,
                outcome=outcome,
                actor=object(),
            )

        self.assertEqual(verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        self.assertEqual(worktree_git.pushes, [["push", "origin", f"HEAD:{branch}"]])

    def _prepared_orchestrator(
        self,
        *,
        check_cmds: tuple[str, ...] = (),
        max_cli_review_rounds: int = 2,
        cli_reviewer: str = "codex",
    ):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=root / "runs",
            run_slug="cli-revision",
            check_cmds=check_cmds,
            publish_mode=PublishMode.GH,
            cli_reviewer=cli_reviewer,
            max_cli_review_rounds=max_cli_review_rounds,
        )
        orch = Orchestrator(config)
        orch._prepare_run_dir()
        branch = f"refactor/{orch.run_id}"
        orch._create_worktree(GitRepo(repo, orch.paths.git_log), branch)
        self.addCleanup(orch._cleanup_worktree)
        worktree_git = RecordingGitRepo(orch.paths.worktree, orch.paths.git_log)
        return orch, worktree_git, branch

    @staticmethod
    def _published_outcome(orch: Orchestrator, branch: str) -> PublishOutcome:
        return PublishOutcome(
            kind=PublishOutcomeKind.PUBLISHED,
            base="main",
            publish_mode=orch.config.publish_mode,
            reason="test published PR",
            branch=branch,
            url="https://github.com/example/repo/pull/1",
            dry_run=False,
        )

    @staticmethod
    def _write_marker(orch: Orchestrator) -> None:
        marker = orch.paths.worktree / ".contremaitre" / "IMPLEMENTATION_COMPLETE"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("CLI revision complete.\n", encoding="utf-8")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


if __name__ == "__main__":
    unittest.main()
