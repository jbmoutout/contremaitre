"""Regression test for the lockfile-staleness bug.

Before the fix, `ensure_deps_volume` was called from the CLI with
`repo=cache_clone_path` BEFORE the worktree was checked out from
`origin/<base>`. When the cache clone was stale (typical: an operator
who hasn't run against this target in a while), the lockfile detection
ran against months-old state — a new `uv.lock` in main wouldn't be
seen, and the deps install step silently no-op'd. The agent then
discovered there was no runtime mid-run.

The fix moves the call into the orchestrator, after `_create_worktree`
has fetched fresh and laid down the per-run worktree. This test pins
that ordering: `ensure_deps_volume` must receive the worktree path.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre.models import ActorMode, Caps, DepsVolume, PublishMode, RunConfig
from contremaitre.orchestrator import Orchestrator


class DepsFreshnessTest(unittest.TestCase):
    def _make_orch(self, repo: Path, runs_root: Path) -> Orchestrator:
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=runs_root,
            run_slug="freshness",
            actor_mode=ActorMode.OPENCODE,
            docker_image="contremaitre-agent:latest",
            publish_mode=PublishMode.STUB,
            caps=Caps(),
        )
        orch = Orchestrator(config=config)
        orch.paths.run_dir.mkdir(parents=True, exist_ok=True)
        orch.paths.worktree.mkdir(parents=True, exist_ok=True)
        return orch

    def test_ensure_pristine_passes_worktree_path_not_cache_clone(self):
        """The whole bug in one assertion: ensure_deps_volume must read
        from the worktree, not from `config.repo` (the cache clone).
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "cache-clone"
            repo.mkdir()
            runs = root / "runs"
            orch = self._make_orch(repo=repo, runs_root=runs)
            with patch("contremaitre.orchestrator.ensure_deps_volume") as fake:
                fake.return_value = None
                orch._ensure_pristine_deps_volume()
            fake.assert_called_once()
            kwargs = fake.call_args.kwargs
            self.assertEqual(kwargs["repo"], orch.paths.worktree)
            self.assertNotEqual(kwargs["repo"], repo)  # NOT the cache clone

    def test_ensure_pristine_skipped_in_fake_mode(self):
        """Fake-actor tests (which never spin up docker) must not
        attempt to call out to the deps subsystem.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "cache-clone"
            repo.mkdir()
            runs = root / "runs"
            orch = self._make_orch(repo=repo, runs_root=runs)
            import dataclasses

            orch.config = dataclasses.replace(orch.config, actor_mode=ActorMode.FAKE)
            with patch("contremaitre.orchestrator.ensure_deps_volume") as fake:
                orch._ensure_pristine_deps_volume()
            fake.assert_not_called()

    def test_ensure_pristine_stores_handle_on_config(self):
        """Successful detection must update `config.deps_volume` so
        downstream actor/check mounts pick up the volume.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "cache-clone"
            repo.mkdir()
            runs = root / "runs"
            orch = self._make_orch(repo=repo, runs_root=runs)
            handle = DepsVolume(
                name="contremaitre-deps-uv-lock-deadbeef",
                mount_path=".venv",
                runtime_env=(("VIRTUAL_ENV", "/app/.venv"),),
            )
            with patch("contremaitre.orchestrator.ensure_deps_volume", return_value=handle):
                orch._ensure_pristine_deps_volume()
        self.assertEqual(orch.config.deps_volume, handle)

    def test_install_error_propagates_as_runtime_error(self):
        """DepsInstallError must surface as a run-aborting exception —
        continuing without deps would make L1 checks look like real
        failures when the underlying issue is install-side.
        """

        from contremaitre.runtime_image import DepsInstallError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "cache-clone"
            repo.mkdir()
            runs = root / "runs"
            log_path = runs / "_deps_install_x.log"
            runs.mkdir(parents=True)
            log_path.touch()
            orch = self._make_orch(repo=repo, runs_root=runs)
            with patch(
                "contremaitre.orchestrator.ensure_deps_volume",
                side_effect=DepsInstallError(lockfile="uv.lock", log_path=log_path, returncode=1),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    orch._ensure_pristine_deps_volume()
        self.assertIn("uv.lock", str(ctx.exception))


class OrderingTest(unittest.TestCase):
    """Pin the call order: worktree fetch BEFORE deps detection.

    If a refactor ever reverses these two steps, the cache will key on
    the cache clone's stale lockfile again — the exact bug we're
    closing here. This test breaks loudly when that happens.
    """

    def test_create_worktree_runs_before_ensure_pristine_deps(self):
        from contremaitre import orchestrator as orch_mod

        call_order: list[str] = []

        def fake_create_worktree(self, *args, **kwargs):
            call_order.append("create_worktree")

        def fake_ensure_pristine(self):
            call_order.append("ensure_pristine_deps")

        def fake_provision(self):
            call_order.append("provision_run_deps")
            # Bail out before any actor/runner machinery — we only care
            # about the first few steps' ordering.
            raise RuntimeError("test-bailout")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(orch_mod.Orchestrator, "_create_worktree", fake_create_worktree),
            patch.object(
                orch_mod.Orchestrator, "_ensure_pristine_deps_volume", fake_ensure_pristine
            ),
            patch.object(orch_mod.Orchestrator, "_provision_run_deps_volume", fake_provision),
            patch("contremaitre.orchestrator.enforce_preflight"),
            patch("contremaitre.orchestrator.GitRepo"),
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
            orch = orch_mod.Orchestrator(config=config)
            # run() catches and records the RuntimeError; we just need
            # the call sequence captured before the bailout.
            orch.run()

        self.assertEqual(
            call_order,
            ["create_worktree", "ensure_pristine_deps", "provision_run_deps"],
        )


if __name__ == "__main__":
    unittest.main()
