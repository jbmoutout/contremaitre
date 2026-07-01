from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre import prompts
from contremaitre.container import DockerContainerLifecycle
from contremaitre.git_utils import GitRepo
from contremaitre.models import ActorMode, DepsVolume, PublishMode, RunConfig
from contremaitre.orchestrator import Orchestrator
from contremaitre.paths import build_run_paths
from contremaitre.publisher import GhPublisher

# Module-level alias so existing tests keep calling build_docker_command(...)
build_docker_command = DockerContainerLifecycle().build_argv


class OpencodeBoundaryTest(unittest.TestCase):
    def test_initial_prompt_invokes_skill_and_keeps_host_owned_boundaries(self):
        prompt = prompts.INITIAL_PROMPT

        # Skill is the framework. Specific token survives mutation that
        # swaps in a different orchestrator/skill name.
        self.assertIn("improve-codebase-architecture", prompt)
        # Host-owns-git policy. The phrases must be specific enough that
        # reversing the policy (e.g. "host does not own git") would break
        # the test — bare substring `assertIn("git", prompt)` does not.
        self.assertIn("Host owns git.", prompt)
        self.assertIn("Never run `git status`", prompt)
        self.assertIn("You have no credentials.", prompt)
        # Handoff scaffolds the skill doesn't prescribe.
        self.assertIn("`.contremaitre/SETTLED_DESIGN.md`", prompt)
        self.assertIn("`.contremaitre/IMPLEMENTATION_COMPLETE`", prompt)

    def test_sim_persona_locks_read_only_tooled_intent(self):
        persona = prompts.SIM_TOOLED_PERSONA

        # Lock the allow/deny lines against policy reversal. Wording is
        # runtime-agnostic (Codex exec_command + Claude/opencode Bash/Read)
        # so we check capabilities, not tool names.
        self.assertIn("**Allowed operations**:", persona)
        self.assertIn("**Forbidden operations**: write, edit, delete", persona)
        # Skill vocabulary is the SIM's language — lock the canonical line.
        self.assertIn(
            "**Module · Interface · Implementation · Depth · Seam · Adapter · "
            "Leverage · Locality.**",
            persona,
        )

    def test_opencode_docker_command_whitelists_env_and_mounts_worktree_read_only(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "secret-key", "HTTP_PROXY": "ambient-proxy"},
                clear=False,
            ),
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

    def _build_with_opencode_config(self, mount_mode: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = build_run_paths(root / "runs", f"20260605-{root.name}")
        paths.run_dir.mkdir(parents=True)
        worktree = root / "worktree"
        worktree.mkdir()
        state = root / "state"
        state.mkdir()
        cfg = root / "synth-opencode.json"
        cfg.write_text("{}", encoding="utf-8")
        config = RunConfig(
            repo=root,
            base="main",
            runs_root=root / "runs",
            run_slug="t",
            actor_mode=ActorMode.OPENCODE,
            docker_image="img",
            opencode_config=cfg,
        )
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}, clear=False):
            build_docker_command(
                config=config,
                paths=paths,
                worktree=worktree,
                state_dir=state,
                mount_mode=mount_mode,
                model="m",
                prompt="p",
                session_id=None,
                extra_mounts=[],
                role="sim",
            )
        return worktree

    def _deps_mount_for_role(self, role: str, mount_mode: str) -> str | None:
        """Return the `name:/app/path:MODE` deps-volume arg build_docker_command
        emits for `role`, or None if no deps mount was added.
        """

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}, clear=False),
        ):
            root = Path(tmp)
            paths = build_run_paths(root / "runs", f"20260613-{root.name}")
            paths.run_dir.mkdir(parents=True)
            worktree = root / "worktree"
            worktree.mkdir()
            state = root / "state"
            state.mkdir()
            config = RunConfig(
                repo=root,
                base="main",
                runs_root=root / "runs",
                run_slug="t",
                actor_mode=ActorMode.OPENCODE,
                docker_image="img",
                deps_volume=DepsVolume(
                    name="vol-x",
                    mount_path=".venv",
                    runtime_env=(("VIRTUAL_ENV", "/app/.venv"),),
                ),
            )
            cmd, _ = build_docker_command(
                config=config,
                paths=paths,
                worktree=worktree,
                state_dir=state,
                mount_mode=mount_mode,
                model="m",
                prompt="p",
                session_id=None,
                extra_mounts=[],
                role=role,
            )
        for token in cmd:
            if token.startswith("vol-x:/app/.venv:"):
                return token
        return None

    def test_deps_mount_agent_rw_sim_ro_review_none(self):
        """Deps follow execution: agent gets a writable venv (self-verify),
        the SIM gets read-only, the review role gets no deps mount at all
        (keeps the reviewer prompt's "no deps" true). Worktree mount mode is
        the agent's rw / the reviewers' ro, and the deps mode tracks it.
        """

        self.assertEqual(self._deps_mount_for_role("agent", "rw"), "vol-x:/app/.venv:rw")
        self.assertEqual(self._deps_mount_for_role("sim", "ro"), "vol-x:/app/.venv:ro")
        self.assertIsNone(self._deps_mount_for_role("review", "ro"))

    def test_ro_mount_precreates_opencode_json_mountpoint(self):
        # A codex-agent mix runs the opencode SIM with /app:ro and no opencode.json
        # in the worktree (codex never emitted it); docker can't create the
        # bind-mount target on a read-only /app, so build_docker_command does.
        worktree = self._build_with_opencode_config("ro")
        self.assertTrue((worktree / "opencode.json").exists())

    def test_rw_mount_does_not_precreate_opencode_json(self):
        # For the RW agent docker creates the mountpoint itself — don't pollute
        # the worktree pre-emptively.
        worktree = self._build_with_opencode_config("rw")
        self.assertFalse((worktree / "opencode.json").exists())

    def test_opencode_docker_command_passes_explicit_proxy_only_by_name(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret-key"}),
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
            subprocess.run(
                ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
            )
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
            config = RunConfig(
                repo=repo, base="main", runs_root=root / "runs", run_slug="host-commit"
            )
            orch = Orchestrator(config)
            orch._prepare_run_dir()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "worktree",
                    "add",
                    str(orch.paths.worktree),
                    "-b",
                    "work",
                    "main",
                ],
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

            status = subprocess.run(
                ["git", "-C", str(orch.paths.worktree), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            log_title = subprocess.run(
                ["git", "-C", str(orch.paths.worktree), "log", "-1", "--pretty=%s"],
                check=True,
                capture_output=True,
                text=True,
            )
            log_body = subprocess.run(
                ["git", "-C", str(orch.paths.worktree), "log", "-1", "--pretty=%b"],
                check=True,
                capture_output=True,
                text=True,
            )
            log_files = subprocess.run(
                ["git", "-C", str(orch.paths.worktree), "show", "--name-only", "--pretty="],
                check=True,
                capture_output=True,
                text=True,
            )
            # `.contremaitre/` is excluded from the commit by pathspec but
            # stays in the worktree (SIM reads it across WORK rounds), so
            # status shows it as untracked.
            self.assertEqual(status.stdout.strip(), "?? .contremaitre/")
            # README.md is staged + committed; .contremaitre/* is not.
            self.assertIn("README.md", log_files.stdout)
            self.assertNotIn(".contremaitre", log_files.stdout)
            # Title is derived from SETTLED_DESIGN.md first line, with the
            # "Settled design — " prefix stripped.
            self.assertEqual(log_title.stdout.strip(), "Consolidate Prisma seam")
            # Body carries the full SETTLED text + a run-id trailer.
            self.assertIn("Delete the duplicate singleton.", log_body.stdout)
            self.assertIn(f"Run: {orch.run_id}", log_body.stdout)
            orch._cleanup_worktree()

    def test_host_commit_succeeds_when_upstream_gitignore_covers_excluded_path(self):
        # Regression: agents sometimes produce build output (e.g. `.next/`)
        # while verifying their changes. If the target repo's .gitignore
        # covers that path, `:(exclude).next` used to abort the host commit
        # because git treats the exclude as an explicit mention and refuses
        # to add an explicitly-named ignored path. Drop the exclude when
        # gitignore already covers it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            subprocess.run(
                ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
            )
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            (repo / ".gitignore").write_text(".next/\n", encoding="utf-8")
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
            config = RunConfig(
                repo=repo, base="main", runs_root=root / "runs", run_slug="host-commit-ignored"
            )
            orch = Orchestrator(config)
            orch._prepare_run_dir()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "worktree",
                    "add",
                    str(orch.paths.worktree),
                    "-b",
                    "work",
                    "main",
                ],
                check=True,
                capture_output=True,
            )
            (orch.paths.worktree / "README.md").write_text("changed\n", encoding="utf-8")
            # Agent produced build output that the target repo gitignores.
            next_dir = orch.paths.worktree / ".next"
            next_dir.mkdir()
            (next_dir / "build.json").write_text("{}\n", encoding="utf-8")
            settled_dir = orch.paths.worktree / ".contremaitre"
            settled_dir.mkdir(exist_ok=True)
            (settled_dir / "SETTLED_DESIGN.md").write_text(
                "# Settled design — Consolidate Prisma seam\n"
                "\n## What\n\nDelete the duplicate singleton.\n",
                encoding="utf-8",
            )

            # Must not raise. Before the fix this raised GitError(1) on the
            # `git add` step.
            orch._commit_agent_changes(repo=GitRepo(orch.paths.worktree, orch.paths.git_log))

            log_files = subprocess.run(
                ["git", "-C", str(orch.paths.worktree), "show", "--name-only", "--pretty="],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("README.md", log_files.stdout)
            self.assertNotIn(".next", log_files.stdout)
            self.assertNotIn(".contremaitre", log_files.stdout)
            orch._cleanup_worktree()

    def test_gh_publisher_pushes_and_creates_draft_pr_from_host(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
        ):
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
                stdout = (
                    "https://github.com/owner/repo/pull/1\n"
                    if cmd[:3] == ["gh", "pr", "create"]
                    else ""
                )
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

            with patch("contremaitre.publisher.subprocess.run", side_effect=fake_run):
                outcome = GhPublisher().publish(
                    config=config, paths=paths, branch="refactor/x", diff_hash="abc"
                )

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


class GhPublisherPreconditionsTest(unittest.TestCase):
    """Lock the two RuntimeError gates in GhPublisher.publish — neither
    was covered before, so a regression that removed the auth or fork
    gate (publishing with no token / wrong remote) could ship silently."""

    def _config(self, *, fork: str | None) -> RunConfig:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            return RunConfig(
                repo=root,
                base="main",
                runs_root=root / "runs",
                run_slug="gh-fail",
                fork=fork,
                publish_mode=PublishMode.GH,
                gh_repo="owner/repo",
            )

    def _paths(self, root: Path):
        paths = build_run_paths(root / "runs", f"20260521-{root.name}")
        paths.run_dir.mkdir(parents=True)
        return paths

    def test_publish_without_github_token_raises(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            paths = self._paths(Path(tmp))
            config = self._config(fork="git@github.com:user/repo.git")
            with self.assertRaisesRegex(RuntimeError, r"GITHUB_TOKEN.*GH_TOKEN"):
                GhPublisher().publish(
                    config=config, paths=paths, branch="refactor/x", diff_hash="abc"
                )

    def test_publish_without_fork_raises(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"GITHUB_TOKEN": "token"}, clear=True),
        ):
            paths = self._paths(Path(tmp))
            config = self._config(fork=None)
            with self.assertRaisesRegex(RuntimeError, r"--fork"):
                GhPublisher().publish(
                    config=config, paths=paths, branch="refactor/x", diff_hash="abc"
                )


if __name__ == "__main__":
    unittest.main()


class ZenKeyClassificationTest(unittest.TestCase):
    """build_docker_command must require OPENROUTER_API_KEY only for keyed models,
    matching preflight's `_check_openrouter_key` (both via `is_zen_model`) — else a
    Zen-only run that passes preflight would fail here at launch (F1)."""

    def _build(self, model: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = build_run_paths(root / "runs", f"20260606-{root.name}")
            paths.run_dir.mkdir(parents=True)
            worktree = root / "wt"
            worktree.mkdir()
            config = RunConfig(
                repo=root,
                base="main",
                runs_root=root / "runs",
                run_slug="t",
                actor_mode=ActorMode.OPENCODE,
                docker_image="img",
            )
            return build_docker_command(
                config=config,
                paths=paths,
                worktree=worktree,
                state_dir=root,
                mount_mode="rw",
                model=model,
                prompt="p",
                session_id=None,
                role="agent",
            )

    def test_zen_model_needs_no_key(self):
        # No OPENROUTER_API_KEY in env, free Zen model → must NOT raise.
        with patch.dict(os.environ, {}, clear=True):
            cmd, _ = self._build("opencode/deepseek-v4-flash-free")
        self.assertEqual(cmd[:3], ["docker", "run", "-d"])

    def test_non_zen_model_requires_key(self):
        from contremaitre.actors import ActorError

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ActorError):
                self._build("openrouter/anthropic/claude-sonnet-4.6")
