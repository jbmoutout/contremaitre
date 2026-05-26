"""Regression test for the lockfile-staleness bug.

Before the fix, `ensure_deps_volume` was called from the CLI with
`repo=cache_clone_path` BEFORE the worktree was checked out from
`origin/<base>`. When the cache clone was stale (typical: an operator
who hasn't run against this target in a while), the lockfile detection
ran against months-old state. The fix moves the call into the
orchestrator, after worktree creation has fetched fresh.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre.deps_provisioner import DepsProvisioner
from contremaitre.models import ActorMode, Caps, DepsVolume, PublishMode, RunConfig
from contremaitre.orchestrator import Orchestrator


class DepsFreshnessTest(unittest.TestCase):
    def _make_prov(self, repo: Path, runs_root: Path, *, actor_mode=ActorMode.OPENCODE) -> DepsProvisioner:
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=runs_root,
            run_slug="freshness",
            actor_mode=actor_mode,
            docker_image="contremaitre-agent:latest",
            publish_mode=PublishMode.STUB,
            caps=Caps(),
        )
        orch = Orchestrator(config=config)
        orch.paths.run_dir.mkdir(parents=True, exist_ok=True)
        orch.paths.worktree.mkdir(parents=True, exist_ok=True)
        return DepsProvisioner(config, orch.paths), orch.paths.worktree

    def test_ensure_pristine_passes_worktree_path_not_cache_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "cache-clone"
            repo.mkdir()
            runs = root / "runs"
            prov, worktree = self._make_prov(repo=repo, runs_root=runs)
            with patch("contremaitre.deps_provisioner.ensure_deps_volume") as fake:
                fake.return_value = None
                prov.ensure_pristine(worktree, project_id="test")
            fake.assert_called_once()
            kwargs = fake.call_args.kwargs
            self.assertEqual(kwargs["repo"], worktree)
            self.assertNotEqual(kwargs["repo"], repo)

    def test_ensure_pristine_skipped_in_fake_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "cache-clone"
            repo.mkdir()
            runs = root / "runs"
            prov, worktree = self._make_prov(repo=repo, runs_root=runs, actor_mode=ActorMode.FAKE)
            with patch("contremaitre.deps_provisioner.ensure_deps_volume") as fake:
                prov.ensure_pristine(worktree, project_id="test")
            fake.assert_not_called()

    def test_ensure_pristine_returns_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "cache-clone"
            repo.mkdir()
            runs = root / "runs"
            prov, worktree = self._make_prov(repo=repo, runs_root=runs)
            handle = DepsVolume(
                name="contremaitre-deps-uv-lock-deadbeef",
                mount_path=".venv",
                runtime_env=(("VIRTUAL_ENV", "/app/.venv"),),
            )
            with patch("contremaitre.deps_provisioner.ensure_deps_volume", return_value=handle):
                result = prov.ensure_pristine(worktree, project_id="test")
        self.assertEqual(result, handle)

    def test_install_error_propagates_as_runtime_error(self):
        from contremaitre.runtime_image import DepsInstallError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "cache-clone"
            repo.mkdir()
            runs = root / "runs"
            log_path = runs / "_deps_install_x.log"
            runs.mkdir(parents=True)
            log_path.touch()
            prov, worktree = self._make_prov(repo=repo, runs_root=runs)
            with patch(
                "contremaitre.deps_provisioner.ensure_deps_volume",
                side_effect=DepsInstallError(
                    lockfile="uv.lock", log_path=log_path, returncode=1
                ),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    prov.ensure_pristine(worktree, project_id="test")
        self.assertIn("uv.lock", str(ctx.exception))


class OrderingTest(unittest.TestCase):
    """Pin the call order: worktree fetch BEFORE deps detection.

    The orchestrator's `run()` now delegates to extracted classes.
    This test verifies the delegation order: WorktreeManager.create
    runs before DepsProvisioner.ensure_pristine, which runs before
    DepsProvisioner.provision_run.
    """

    def test_create_worktree_runs_before_ensure_pristine_deps(self):
        from contremaitre import worktree_manager as wm_mod
        from contremaitre import deps_provisioner as dp_mod

        call_order: list[str] = []

        def fake_wm_create(self, repo, branch):
            call_order.append("create_worktree")
            return "deadbeef"  # fake base_sha

        def fake_dp_ensure(self, worktree, project_id):
            call_order.append("ensure_pristine_deps")
            return None

        def fake_dp_provision(self, pristine, run_id):
            call_order.append("provision_run_deps")
            raise RuntimeError("test-bailout")

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            wm_mod.WorktreeManager, "create", fake_wm_create
        ), patch.object(
            dp_mod.DepsProvisioner, "ensure_pristine", fake_dp_ensure
        ), patch.object(
            dp_mod.DepsProvisioner, "provision_run", fake_dp_provision
        ), patch(
            "contremaitre.orchestrator.enforce_preflight"
        ), patch(
            "contremaitre.orchestrator.GitRepo"
        ):
            root = Path(tmp)
            runs = root / "runs"
            config = RunConfig(
                repo=root,
                base="main",
                runs_root=runs,
                run_slug="order",
                actor_mode=ActorMode.OPENCODE,
                docker_image="img",
                publish_mode=PublishMode.STUB,
            )
            orch = Orchestrator(config=config)
            orch.run()

        self.assertEqual(
            call_order,
            ["create_worktree", "ensure_pristine_deps", "provision_run_deps"],
        )


if __name__ == "__main__":
    unittest.main()
