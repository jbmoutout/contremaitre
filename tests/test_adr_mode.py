"""ADR-seeded runs (`--adr`): prompt selection, SIM seed note, INIT validation.

The mode has no state-machine surface — it is a templated first message, a
host note in the SIM's first turn, and a fail-fast existence check against
the `origin/<base>` checkout. These tests lock each of those seams.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from contremaitre import prompts
from contremaitre.cli import _config_from_args, build_parser
from contremaitre.fixture import init_fixture
from contremaitre.git_utils import GitRepo
from contremaitre.manifest import build_manifest, manifest_digest
from contremaitre.models import RunConfig
from contremaitre.orchestrator import Orchestrator

ADR = "docs/adr/0001-example.md"


class AdrPromptTest(unittest.TestCase):
    def test_default_mode_is_byte_identical_to_classic_prompt(self):
        self.assertIs(prompts.initial_prompt(None), prompts.INITIAL_PROMPT)

    def test_adr_prompt_substitutes_path_and_keeps_host_owned_boundaries(self):
        prompt = prompts.initial_prompt(ADR)

        self.assertIn(ADR, prompt)
        self.assertNotIn("{adr_path}", prompt)
        # Same skill, entered at the grilling loop — with the sub-skill the
        # upstream skill delegates to named explicitly.
        self.assertIn("improve-codebase-architecture", prompt)
        self.assertIn("`/grilling`", prompt)
        # Host-owned boundaries are identical to the classic prompt; the
        # phrases are specific enough that reversing the policy breaks here.
        self.assertIn("Host owns git.", prompt)
        self.assertIn("Never run `git status`", prompt)
        self.assertIn("You have no credentials.", prompt)
        self.assertIn("`.contremaitre/SETTLED_DESIGN.md`", prompt)
        self.assertIn("`.contremaitre/IMPLEMENTATION_COMPLETE`", prompt)

    def test_adr_prompt_locks_fact_check_triage_and_authority_boundary(self):
        prompt = prompts.initial_prompt(ADR)

        for klass in ("**Confirmed**", "**Drifted**", "**Contested**"):
            self.assertIn(klass, prompt)
        # Facts get fixed silently; the decision is never the agent's to edit.
        self.assertIn("Correct only **facts**", prompt)
        self.assertIn("Never edit the Decision", prompt)
        # SETTLED carries the ADR context so the review pass sees the ADR
        # edits as sanctioned rather than drift.
        self.assertIn("list the factual corrections", prompt)

    def test_sim_first_turn_injects_host_note_only_when_seeded(self):
        seeded = prompts.sim_first_turn("agent says hi", adr_path=ADR)
        unseeded = prompts.sim_first_turn("agent says hi")

        self.assertIn("─── HOST NOTE ───", seeded)
        self.assertIn(ADR, seeded)
        # Primary-source rule: the SIM reads the file, not the agent's
        # restatement — and polices the facts-only authority boundary.
        self.assertIn("it is the primary source", seeded)
        self.assertIn("never the Decision", seeded)
        # The note sits between the persona and the agent's message.
        self.assertLess(seeded.index("─── END PERSONA ───"), seeded.index("─── HOST NOTE ───"))
        self.assertLess(seeded.index("─── HOST NOTE ───"), seeded.index("AGENT:"))

        self.assertNotIn("HOST NOTE", unseeded)
        self.assertNotIn("{adr_path}", seeded)

    def test_review_prompt_carries_adr_boundary_criterion(self):
        self.assertIn("pre-existing ADR", prompts.SIM_REVIEW_PROMPT)
        self.assertIn("Decision, rationale, or Consequences", prompts.SIM_REVIEW_PROMPT)


class AdrCliTest(unittest.TestCase):
    def _parse(self, *extra: str):
        return build_parser().parse_args(["run", "--base", "main", *extra])

    def test_adr_flag_threads_into_config(self):
        args = self._parse("--adr", ADR)
        config = _config_from_args(args, repo=Path("."))
        self.assertEqual(config.adr_path, ADR)

    def test_adr_flag_defaults_to_none(self):
        config = _config_from_args(self._parse(), repo=Path("."))
        self.assertIsNone(config.adr_path)

    def test_adr_flag_rejects_absolute_and_traversal_paths(self):
        for bad in ("/etc/adr.md", "docs/../../evil.md", ".."):
            with (
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self._parse("--adr", bad)


class AdrInitValidationTest(unittest.TestCase):
    def _orchestrator(self, adr_path: str | None) -> Orchestrator:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        # Commit the ADR on main and refresh the remote-tracking ref — the
        # worktree branches from `origin/main`, so an ADR that is merely
        # sitting uncommitted in the source repo must NOT count.
        adr_file = repo / ADR
        adr_file.parent.mkdir(parents=True)
        adr_file.write_text("# ADR 0001\n\nDecision: example.\n", encoding="utf-8")
        git = GitRepo(repo)
        git.run("add", ".")
        git.run("commit", "-m", "adr: example")
        git.run("fetch", "origin", "main")
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=root / "runs",
            run_slug="adr-mode",
            adr_path=adr_path,
        )
        orch = Orchestrator(config)
        orch._prepare_run_dir()
        orch._create_worktree(GitRepo(repo, orch.paths.git_log), f"refactor/{orch.run_id}")
        self.addCleanup(orch._cleanup_worktree)
        return orch

    def test_committed_adr_passes_and_prompt_artifact_is_templated(self):
        orch = self._orchestrator(ADR)
        orch._validate_adr_seed()  # must not raise
        written = orch.paths.initial_prompt.read_text(encoding="utf-8")
        self.assertIn(ADR, written)
        self.assertNotIn("{adr_path}", written)

    def test_missing_adr_fails_fast_with_base_branch_hint(self):
        orch = self._orchestrator("docs/adr/9999-nowhere.md")
        with self.assertRaises(RuntimeError) as ctx:
            orch._validate_adr_seed()
        self.assertIn("origin/main", str(ctx.exception))
        self.assertIn("committed", str(ctx.exception))

    def test_worktree_escape_is_refused_even_past_argparse(self):
        # `--adr` blocks `..` at parse time; the orchestrator re-checks so a
        # programmatic RunConfig can't smuggle a traversal through.
        orch = self._orchestrator("../outside.md")
        with self.assertRaises(RuntimeError) as ctx:
            orch._validate_adr_seed()
        self.assertIn("escapes the worktree", str(ctx.exception))

    def test_unseeded_run_skips_validation(self):
        orch = self._orchestrator(None)
        orch._validate_adr_seed()  # no-op, must not raise
        written = orch.paths.initial_prompt.read_text(encoding="utf-8")
        self.assertEqual(written, prompts.INITIAL_PROMPT)


class AdrManifestTest(unittest.TestCase):
    def test_manifest_records_adr_path_outside_the_system_digest(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        config = RunConfig(repo=repo, base="main", runs_root=root / "runs", run_slug="m")
        manifest = build_manifest(config)
        self.assertIsNone(manifest["adr_path"])

        seeded = dict(manifest, adr_path=ADR)
        self.assertEqual(manifest_digest(manifest), manifest_digest(seeded))


if __name__ == "__main__":
    unittest.main()
