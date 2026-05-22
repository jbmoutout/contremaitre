from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre import prompts
from contremaitre.actors import build_docker_command
from contremaitre.git_utils import GitRepo
from contremaitre.models import ActorMode, PublishMode, RunConfig
from contremaitre.orchestrator import Orchestrator
from contremaitre.paths import build_run_paths
from contremaitre.publisher import GhPublisher


class OpencodeBoundaryTest(unittest.TestCase):
    def test_initial_prompt_invokes_skill_and_keeps_host_owned_boundaries(self):
        prompt = prompts.INITIAL_PROMPT

        # Skill is now the framework, not a forbidden tool.
        self.assertIn("improve-codebase-architecture", prompt)
        # Host-owns-git rule remains the architectural invariant.
        self.assertIn("git", prompt)
        self.assertIn("gh", prompt)
        # Handoff scaffolds the skill doesn't prescribe.
        self.assertIn("SETTLED_DESIGN.md", prompt)
        self.assertIn("IMPLEMENTATION_COMPLETE", prompt)

    def test_sim_persona_locks_read_only_tooled_intent(self):
        persona = prompts.SIM_TOOLED_PERSONA

        # Tooled, read-only — not the pre-tooled persona shape.
        self.assertIn("read", persona)
        self.assertIn("glob", persona)
        self.assertIn("grep", persona)
        # Skill vocabulary is the SIM's language.
        for term in ("Module", "Interface", "Depth", "Seam", "Adapter"):
            self.assertIn(term, persona)

    def test_opencode_docker_command_whitelists_env_and_mounts_worktree_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "secret-key", "HTTP_PROXY": "ambient-proxy"},
            clear=False,
        ):
            root = Path(tmp)
            paths = build_run_paths(root / "runs", f"20260521-{root.name}")
            paths.run_dir.mkdir(parents=True)
            worktree = root / "worktree"
            state = root / "state"
            worktree.mkdir()
            state.mkdir()
            config = RunConfig(
                repo=root,
                base="main",
                runs_root=root / "runs",
                run_slug="test",
                actor_mode=ActorMode.OPENCODE,
                docker_image="test-image",
                http_proxy=None,
            )

            cmd, _ = build_docker_command(
                config=config,
                paths=paths,
                worktree=worktree,
                state_dir=state,
                mount_mode="ro",
                model="openrouter/test/model",
                prompt="hello",
                session_id="sess",
                extra_mounts=[],
                role="agent",
            )

            joined = " ".join(cmd)
            # Detached + label-driven supervision (Phase 3): docker daemon
            # owns the container lifecycle, signal handlers stop by label.
            self.assertEqual(cmd[:3], ["docker", "run", "-d"])
            self.assertIn(f"contremaitre.run-id={paths.run_id}", cmd)
            self.assertIn("contremaitre.role=agent", cmd)
            self.assertNotIn("--cidfile", cmd)
            self.assertNotIn("--rm", cmd)
            self.assertIn(f"{worktree}:/app:ro", joined)
            self.assertIn("OPENROUTER_API_KEY", cmd)
            self.assertNotIn("secret-key", joined)
            self.assertNotIn("HTTP_PROXY", cmd)
            self.assertIn("--session", cmd)
            self.assertIn("sess", cmd)

    def test_opencode_docker_command_passes_explicit_proxy_only_by_name(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret-key"}):
            root = Path(tmp)
            paths = build_run_paths(root / "runs", f"20260521-{root.name}")
            paths.run_dir.mkdir(parents=True)
            worktree = root / "worktree"
            state = root / "state"
            worktree.mkdir()
            state.mkdir()
            config = RunConfig(
                repo=root,
                base="main",
                runs_root=root / "runs",
                run_slug="test",
                actor_mode=ActorMode.OPENCODE,
                docker_image="test-image",
                http_proxy="http://proxy.local:8080",
            )

            cmd, env = build_docker_command(
                config=config,
                paths=paths,
                worktree=worktree,
                state_dir=state,
                mount_mode="rw",
                model="openrouter/test/model",
                prompt="hello",
                session_id=None,
                extra_mounts=[],
                role="agent",
            )

            self.assertIn("HTTP_PROXY", cmd)
            self.assertEqual(env["HTTP_PROXY"], "http://proxy.local:8080")
            self.assertNotIn("http://proxy.local:8080", " ".join(cmd))

    def test_orchestrator_commits_actor_left_changes_from_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-m",
                    "base",
                ],
                check=True,
                capture_output=True,
            )
            config = RunConfig(repo=repo, base="main", runs_root=root / "runs", run_slug="host-commit")
            orch = Orchestrator(config)
            orch._prepare_run_dir()
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", str(orch.paths.worktree), "-b", "work", "main"],
                check=True,
                capture_output=True,
            )
            (orch.paths.worktree / "README.md").write_text("changed\n", encoding="utf-8")
            # SETTLED_DESIGN.md is required by the orchestrator before commit
            # in normal flow; the commit message helper reads its first line
            # to derive the title.
            settled_dir = orch.paths.worktree / ".contremaitre"
            settled_dir.mkdir(exist_ok=True)
            (settled_dir / "SETTLED_DESIGN.md").write_text(
                "# Settled design — Consolidate Prisma seam\n\n"
                "## What\n\nDelete the duplicate singleton.\n",
                encoding="utf-8",
            )

            orch._commit_agent_changes(repo=GitRepo(orch.paths.worktree, orch.paths.git_log))

            status = subprocess.run(["git", "-C", str(orch.paths.worktree), "status", "--porcelain"], check=True, capture_output=True, text=True)
            log_title = subprocess.run(["git", "-C", str(orch.paths.worktree), "log", "-1", "--pretty=%s"], check=True, capture_output=True, text=True)
            log_body = subprocess.run(["git", "-C", str(orch.paths.worktree), "log", "-1", "--pretty=%b"], check=True, capture_output=True, text=True)
            self.assertEqual(status.stdout, "")
            # Title is derived from SETTLED_DESIGN.md first line, with the
            # "Settled design — " prefix stripped.
            self.assertEqual(log_title.stdout.strip(), "Consolidate Prisma seam")
            # Body carries the full SETTLED text + a run-id trailer.
            self.assertIn("Delete the duplicate singleton.", log_body.stdout)
            self.assertIn(f"Run: {orch.run_id}", log_body.stdout)
            orch._cleanup_worktree()

    def test_gh_publisher_pushes_and_creates_draft_pr_from_host(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GITHUB_TOKEN": "token"}):
            root = Path(tmp)
            paths = build_run_paths(root / "runs", f"20260521-{root.name}")
            paths.run_dir.mkdir(parents=True)
            paths.worktree.mkdir(parents=True)
            config = RunConfig(
                repo=root,
                base="main",
                runs_root=root / "runs",
                run_slug="test",
                fork="git@github.com:user/repo.git",
                publish_mode=PublishMode.GH,
                gh_repo="owner/repo",
                pr_title="Test PR",
            )
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                stdout = "https://github.com/owner/repo/pull/1\n" if cmd[:3] == ["gh", "pr", "create"] else ""
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

            with patch("contremaitre.publisher.subprocess.run", side_effect=fake_run):
                outcome = GhPublisher().publish(config=config, paths=paths, branch="refactor/x", diff_hash="abc")

            self.assertEqual(outcome.kind.value, "PUBLISHED")
            self.assertFalse(outcome.dry_run)
            self.assertEqual(outcome.url, "https://github.com/owner/repo/pull/1")
            self.assertEqual(calls[0], ["git", "push", "origin", "HEAD:refactor/x"])
            self.assertEqual(calls[1][:3], ["gh", "pr", "create"])
            self.assertIn("--draft", calls[1])
            self.assertIn("--repo", calls[1])
            pr = json.loads(paths.pr_json.read_text(encoding="utf-8"))
            self.assertEqual(pr["kind"], "PUBLISHED")
            self.assertEqual(pr["publish_mode"], "gh")


if __name__ == "__main__":
    unittest.main()
