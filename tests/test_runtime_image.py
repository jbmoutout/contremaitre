from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from contremaitre.models import DepsVolume
from contremaitre.runtime_image import (
    _LOCKFILES,
    _detect,
    _offline_assert_cmd,
    assert_deps_offline,
    clone_deps_volume_for_run,
    ensure_deps_volume,
)


class LockfileDetectionTest(unittest.TestCase):
    """`_detect` returns the right cache_mount_path + runtime_env per ecosystem.

    Lives at the boundary that decides where the deps cache will be
    mounted in BOTH the install one-shot and the runtime container.
    Regressing the table silently breaks every Python/Rust/Go run.
    """

    def test_lockfile_table_covers_supported_ecosystems(self):
        names = {lock.name for lock in _LOCKFILES}
        # Sanity: any new lockfile addition shows up here, and any
        # removal forces a deliberate test change.
        self.assertEqual(
            names,
            {
                "package-lock.json",
                "pnpm-lock.yaml",
                "yarn.lock",
                "poetry.lock",
                "uv.lock",
                "requirements.lock",
                "Cargo.lock",
                "go.sum",
            },
        )

    def test_node_lockfiles_use_node_modules(self):
        for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / name).write_text("{}", encoding="utf-8")
                detected = _detect(Path(tmp))
                self.assertIsNotNone(detected)
                lock, _ = detected
                self.assertEqual(lock.name, name)
                self.assertEqual(lock.cache_mount_path, "node_modules")
                self.assertEqual(lock.runtime_env, ())

    def test_python_lockfiles_use_venv_with_virtual_env(self):
        # All three Python lockfiles share the same /app/.venv layout
        # and runtime env. Tested together so adding a future Python
        # lockfile means adding one name here.
        for name in ("uv.lock", "poetry.lock", "requirements.lock"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / name).write_text("", encoding="utf-8")
                detected = _detect(Path(tmp))
                self.assertIsNotNone(detected)
                lock, _ = detected
                self.assertEqual(lock.cache_mount_path, ".venv")
                env = dict(lock.runtime_env)
                self.assertEqual(env.get("VIRTUAL_ENV"), "/app/.venv")
                # PATH must put the venv bin first so `pytest`/`ruff`
                # resolve to the cached install, not whatever the image
                # ships at /usr/bin.
                self.assertTrue(env.get("PATH", "").startswith("/app/.venv/bin:"))

    def test_requirements_lock_lower_priority_than_uv_lock(self):
        """When a project has both `uv.lock` and `requirements.lock`
        (typical mid-migration state), `_detect` must pick uv.lock —
        it's the canonical source and requirements.lock is often a
        generated/legacy artifact. Pins the iteration order in
        `_LOCKFILES` against accidental reshuffling.
        """

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "uv.lock").write_text("", encoding="utf-8")
            (Path(tmp) / "requirements.lock").write_text("", encoding="utf-8")
            detected = _detect(Path(tmp))
            self.assertIsNotNone(detected)
            lock, _ = detected
            self.assertEqual(lock.name, "uv.lock")

    def test_cargo_uses_cargo_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Cargo.lock").write_text("", encoding="utf-8")
            detected = _detect(Path(tmp))
            self.assertIsNotNone(detected)
            lock, _ = detected
            self.assertEqual(lock.cache_mount_path, ".cargo-cache")
            self.assertEqual(dict(lock.runtime_env)["CARGO_HOME"], "/app/.cargo-cache")

    def test_go_uses_gopath(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "go.sum").write_text("", encoding="utf-8")
            detected = _detect(Path(tmp))
            self.assertIsNotNone(detected)
            lock, _ = detected
            self.assertEqual(lock.cache_mount_path, ".go-mod-cache")
            self.assertEqual(dict(lock.runtime_env)["GOPATH"], "/app/.go-mod-cache")

    def test_no_lockfile_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_detect(Path(tmp)))

    def test_uv_runtime_env_pins_no_sync_and_canary(self):
        """uv's runtime env must carry UV_NO_SYNC=1 so a runtime `uv run`
        uses the warmed venv as-is instead of rebuilding the project (which
        refetches the `setuptools` build backend — blocked under locked
        egress). poetry, which has no such rebuild-on-run, must NOT carry it.
        """

        uv = next(lock for lock in _LOCKFILES if lock.name == "uv.lock")
        self.assertEqual(dict(uv.runtime_env).get("UV_NO_SYNC"), "1")
        self.assertEqual(uv.canary_cmd, "uv run python -c ''")
        # requirements.lock shares the uv-run path, so shares the guard.
        req = next(lock for lock in _LOCKFILES if lock.name == "requirements.lock")
        self.assertEqual(dict(req.runtime_env).get("UV_NO_SYNC"), "1")
        self.assertEqual(req.canary_cmd, "uv run python -c ''")
        # poetry: no uv-run rebuild, so no UV_NO_SYNC and no uv canary.
        poetry = next(lock for lock in _LOCKFILES if lock.name == "poetry.lock")
        self.assertNotIn("UV_NO_SYNC", dict(poetry.runtime_env))
        self.assertEqual(poetry.canary_cmd, "")

    def test_recipe_tag_changes_with_install_cmd(self):
        """The volume key folds in a hash of the install command, so editing
        a recipe forces a rebuild instead of reusing a volume built by the
        old command. Same command → stable tag; changed command → new tag.
        """

        from contremaitre.runtime_image import _Lockfile, _recipe_tag

        base = _Lockfile("uv.lock", "uv sync --frozen", ".venv")
        same = _Lockfile("uv.lock", "uv sync --frozen", ".venv", canary_cmd="x")
        changed = _Lockfile("uv.lock", "uv sync --frozen --no-install-project", ".venv")
        # Stable across runtime_env / canary edits (they don't change contents).
        self.assertEqual(_recipe_tag(base), _recipe_tag(same))
        # Changes when the install command changes — the masked-fix bug.
        self.assertNotEqual(_recipe_tag(base), _recipe_tag(changed))

    def test_only_uv_family_has_a_canary(self):
        """Canaries exist only where they exercise the proven-broken
        offline-run path (uv). Faking one for an ecosystem we can't test
        would give false confidence — those rely on the operator's check.
        """

        with_canary = {lock.name for lock in _LOCKFILES if lock.canary_cmd}
        self.assertEqual(with_canary, {"uv.lock", "requirements.lock"})

    def test_install_commands_do_not_bootstrap_tools(self):
        """The base image ships uv + poetry. If the install_cmd ever
        regresses to `pip install --quiet uv && …`, the install
        container will reach out to PyPI on every cache miss —
        slow, noisy, and a footgun for air-gapped operators.
        """

        for lock in _LOCKFILES:
            with self.subTest(lock=lock.name):
                self.assertNotIn("pip install --quiet uv", lock.install_cmd)
                self.assertNotIn("pip install --quiet poetry", lock.install_cmd)


class EnsureDepsVolumeInstallShapeTest(unittest.TestCase):
    """End-to-end shape of the `docker run` invocation per ecosystem.

    Each ecosystem must (a) mount the cache volume at the right path,
    (b) pass the right env vars rewritten for the install context
    (/app → /install), and (c) run the install command via `sh -lc`.
    A regression here means deps install succeeds on the host's CI but
    produces an empty cache the agent never finds.
    """

    def _run_and_capture(self, lockfile_name: str, lockfile_content: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as runs:
            repo = Path(tmp)
            (repo / lockfile_name).write_text(lockfile_content, encoding="utf-8")
            with (
                patch("contremaitre.runtime_image._volume_exists", return_value=False),
                patch("contremaitre.runtime_image._prune_stale_deps_volumes"),
                patch("contremaitre.runtime_image.subprocess.run") as fake_run,
            ):
                fake_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                ensure_deps_volume(
                    repo=repo,
                    base_image="test-image",
                    runs_root=Path(runs),
                    project_id="test-project",
                )
            # Calls: volume create, docker run install. We want the install.
            install_call = fake_run.call_args_list[-1]
            return list(install_call.args[0])

    def test_uv_install_mounts_venv_and_sets_virtual_env(self):
        cmd = self._run_and_capture("uv.lock", "[]")
        joined = " ".join(cmd)
        # Cache mount path is .venv, not node_modules. /app (not /install)
        # so uv's embedded shebangs (`#!/app/.venv/bin/python`) resolve
        # at runtime, which also mounts at /app.
        self.assertRegex(joined, r"contremaitre-deps-test-project-uv-lock-[0-9a-f-]+:/app/\.venv\b")
        self.assertIn("VIRTUAL_ENV=/app/.venv", cmd)
        # Full `uv sync --frozen` — NOT `--no-install-project`. The project
        # must be installed at warm time (open egress can fetch the build
        # backend); deferring it to a runtime `uv run` under locked egress
        # was the setuptools wall. Guard the regression explicitly.
        self.assertIn("uv sync --frozen", cmd[-1])
        self.assertNotIn("--no-install-project", cmd[-1])
        self.assertNotIn("pip install --quiet uv", cmd[-1])

    def test_volume_name_includes_recipe_tag(self):
        """The volume name carries the recipe tag as a trailing segment
        (`...-<digest>-<recipe>`), so editing a recipe forces a fresh volume
        and the prefix-scoped prune still sweeps the superseded one. Without
        this, an install-cmd change reuses the stale volume — the bug that
        masked the uv parity fix on first test.
        """

        cmd = self._run_and_capture("uv.lock", "[]")
        joined = " ".join(cmd)
        self.assertRegex(
            joined,
            r"contremaitre-deps-test-project-uv-lock-[0-9a-f]+-[0-9a-f]+:/app/\.venv\b",
        )

    def test_poetry_install_mounts_venv_and_sets_virtual_env(self):
        cmd = self._run_and_capture("poetry.lock", "")
        joined = " ".join(cmd)
        self.assertRegex(
            joined, r"contremaitre-deps-test-project-poetry-lock-[0-9a-f-]+:/app/\.venv\b"
        )
        self.assertIn("VIRTUAL_ENV=/app/.venv", cmd)
        self.assertIn("POETRY_VIRTUALENVS_IN_PROJECT=true", cmd[-1])

    def test_node_install_mounts_node_modules_no_env(self):
        cmd = self._run_and_capture("package-lock.json", "{}")
        joined = " ".join(cmd)
        self.assertRegex(
            joined,
            r"contremaitre-deps-test-project-package-lock-json-[0-9a-f-]+:/app/node_modules\b",
        )
        # No VIRTUAL_ENV / CARGO_HOME / GOPATH for Node.
        for env_key in ("VIRTUAL_ENV", "CARGO_HOME", "GOPATH"):
            self.assertNotIn(f"{env_key}=", " ".join(cmd))

    def test_cargo_install_mounts_cargo_cache_and_sets_cargo_home(self):
        cmd = self._run_and_capture("Cargo.lock", "")
        joined = " ".join(cmd)
        self.assertRegex(
            joined, r"contremaitre-deps-test-project-Cargo-lock-[0-9a-f-]+:/app/\.cargo-cache\b"
        )
        self.assertIn("CARGO_HOME=/app/.cargo-cache", cmd)

    def test_go_install_mounts_go_cache_and_sets_gopath(self):
        cmd = self._run_and_capture("go.sum", "")
        joined = " ".join(cmd)
        self.assertRegex(
            joined, r"contremaitre-deps-test-project-go-sum-[0-9a-f-]+:/app/\.go-mod-cache\b"
        )
        self.assertIn("GOPATH=/app/.go-mod-cache", cmd)

    def test_install_and_runtime_paths_match(self):
        """Regression: uv embeds the venv path into installed scripts'
        shebangs at install time. If the install one-shot mounts the
        venv at /install/.venv but the runtime container mounts it at
        /app/.venv, every script ends up with shebang
        `#!/install/.venv/bin/python` that fails at runtime with
        "not found". The install context's mount path MUST equal the
        runtime mount path advertised via runtime_env.
        """

        cmd = self._run_and_capture("uv.lock", "[]")
        # No /install/ mount paths in the install one-shot — everything
        # is /app/, matching where actors.py and checks.py mount things.
        joined = " ".join(cmd)
        self.assertNotIn("/install/", joined)
        # Source mounted RW so docker can create the cache mountpoint
        # directory (worktrees from `git worktree add` lack untracked
        # dirs like `.venv/`, so RO fails with "read-only file system"
        # at container-create time).
        self.assertIn(":/app:rw", joined)
        self.assertNotIn(":/app:ro", joined)
        self.assertIn("VIRTUAL_ENV=/app/.venv", cmd)

    def test_returns_handle_with_runtime_paths(self):
        """`ensure_deps_volume` returns a handle whose runtime_env points
        at /app/* (not /install/*). The install-context rewrite is
        internal to the install one-shot — downstream actor/check
        containers see the runtime layout.
        """

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as runs:
            repo = Path(tmp)
            (repo / "uv.lock").write_text("[]", encoding="utf-8")
            with (
                patch("contremaitre.runtime_image._volume_exists", return_value=False),
                patch("contremaitre.runtime_image._prune_stale_deps_volumes"),
                patch("contremaitre.runtime_image.subprocess.run") as fake_run,
            ):
                fake_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                handle = ensure_deps_volume(
                    repo=repo,
                    base_image="test-image",
                    runs_root=Path(runs),
                    project_id="myproject",
                )
        self.assertIsNotNone(handle)
        self.assertEqual(handle.mount_path, ".venv")
        env = dict(handle.runtime_env)
        self.assertEqual(env["VIRTUAL_ENV"], "/app/.venv")
        # Project slug is in the volume name so two repos with the same
        # lockfile kind don't evict each other in _prune_stale_deps_volumes.
        self.assertIn("myproject", handle.name)


class PruneScopeTest(unittest.TestCase):
    """Pre-existing bug, fixed: pruning was scoped to lockfile-kind only,
    so running project A then project B (both with `package-lock.json`)
    evicted A's cache. Now scoped to project+kind.
    """

    def test_prune_filters_by_project_slug(self):
        from contremaitre import runtime_image as ri

        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("contremaitre.runtime_image.subprocess.run", side_effect=fake_run):
            ri._prune_stale_deps_volumes(
                project_slug="github-com-foo-bar",
                lockfile_name="package-lock.json",
                current_volume="contremaitre-deps-github-com-foo-bar-package-lock-json-abc",
            )

        # The `docker volume ls` filter must include the project slug —
        # otherwise it would match other projects' package-lock.json
        # volumes and queue them for deletion.
        ls_call = captured[0]
        joined = " ".join(ls_call)
        self.assertIn("name=contremaitre-deps-github-com-foo-bar-package-lock-json-", joined)
        # Sanity: a bare `package-lock-json-` filter would be too broad.
        self.assertNotIn("name=contremaitre-deps-package-lock-json-", joined)


class CloneDepsVolumeForRunTest(unittest.TestCase):
    """`clone_deps_volume_for_run` preserves mount_path + runtime_env.

    The per-run clone is the same bytes under a different name; if
    mount_path or runtime_env got dropped here, the per-run container
    would mount a populated volume at the wrong path or without the
    env vars the ecosystem needs.
    """

    def test_clone_preserves_mount_path_and_env(self):
        pristine = DepsVolume(
            name="contremaitre-deps-uv-lock-abc123",
            mount_path=".venv",
            runtime_env=(("VIRTUAL_ENV", "/app/.venv"),),
        )
        with patch("contremaitre.runtime_image.subprocess.run") as fake_run:
            fake_run.return_value = MagicMock(returncode=0)
            handle = clone_deps_volume_for_run(
                pristine=pristine, run_id="20260524-abcd", base_image="img"
            )
        self.assertEqual(handle.mount_path, ".venv")
        self.assertEqual(handle.runtime_env, pristine.runtime_env)
        self.assertEqual(handle.name, "contremaitre-run-20260524-abcd-deps")
        self.assertNotEqual(handle.name, pristine.name)

        # Two subprocess.run calls: `docker volume create` then the
        # `docker run … cp -a` copy. Locking these down so a future refactor
        # that drops either step (silently leaving the per-run volume empty)
        # fails the test instead of leaking volumes in production.
        self.assertEqual(fake_run.call_count, 2)
        create_cmd = fake_run.call_args_list[0].args[0]
        copy_cmd = fake_run.call_args_list[1].args[0]
        # Volume create with the run-id label — that label is how the
        # orchestrator's `finally` cleanup finds and removes per-run volumes.
        # Lose the label and we leak a volume per run forever.
        self.assertEqual(create_cmd[:3], ["docker", "volume", "create"])
        self.assertIn("contremaitre.run-id=20260524-abcd", create_cmd)
        self.assertIn("contremaitre-run-20260524-abcd-deps", create_cmd)
        # The clone itself: read-only mount of pristine, RW mount of per-run,
        # `cp -a` to preserve permissions/timestamps.
        self.assertEqual(copy_cmd[:3], ["docker", "run", "--rm"])
        self.assertIn(f"{pristine.name}:/src:ro", copy_cmd)
        self.assertIn("contremaitre-run-20260524-abcd-deps:/dst", copy_cmd)
        self.assertIn("contremaitre.run-id=20260524-abcd", copy_cmd)
        self.assertTrue(
            any("cp -a /src/. /dst/" in arg for arg in copy_cmd),
            f"expected cp -a in copy_cmd, got {copy_cmd!r}",
        )


class OfflineAssertCmdSelectionTest(unittest.TestCase):
    """`_offline_assert_cmd` picks the right probe: operator check > canary > none."""

    def test_check_cmds_win_joined_with_and(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # A uv.lock canary is available, but an explicit check command
            # is the stronger signal and must win.
            (repo / "uv.lock").write_text("", encoding="utf-8")
            sel = _offline_assert_cmd(worktree=repo, check_cmds=("ruff check .", "pytest -q"))
        self.assertEqual(sel, ("ruff check . && pytest -q", "check_cmd"))

    def test_canary_fallback_when_no_check_cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "uv.lock").write_text("", encoding="utf-8")
            sel = _offline_assert_cmd(worktree=repo, check_cmds=())
        self.assertEqual(sel, ("uv run python -c ''", "canary"))

    def test_none_when_no_check_cmd_and_no_canary(self):
        # Node has no canary today → nothing to assert without a check cmd.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package-lock.json").write_text("{}", encoding="utf-8")
            self.assertIsNone(_offline_assert_cmd(worktree=repo, check_cmds=()))

    def test_none_when_no_lockfile_and_no_check_cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_offline_assert_cmd(worktree=Path(tmp), check_cmds=()))


class AssertDepsOfflineTest(unittest.TestCase):
    """`assert_deps_offline` builds an L1-sidecar-shaped probe on the
    agent's network and reports pass/fail without deciding severity.
    """

    @staticmethod
    def _proc(returncode=0, stdout="", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_mirrors_locked_network_and_mounts_deps_rw(self):
        captured: dict[str, list[str]] = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return self._proc(0)

        vol = DepsVolume(
            name="vol-x",
            mount_path=".venv",
            runtime_env=(("VIRTUAL_ENV", "/app/.venv"), ("UV_NO_SYNC", "1")),
        )
        res = assert_deps_offline(
            worktree=Path("/wt"),
            base_image="img",
            docker_network="contremaitre-cli-egress",
            container_user=None,
            deps_volume=vol,
            check_cmds=("pytest -q",),
            runner=runner,
        )
        cmd = captured["cmd"]
        joined = " ".join(cmd)
        # Same network the agent faces — the whole point of the probe.
        self.assertIn("--network", cmd)
        self.assertIn("contremaitre-cli-egress", cmd)
        # Deps volume RW at the cache path, runtime env passed through.
        self.assertIn("vol-x:/app/.venv:rw", joined)
        self.assertIn("VIRTUAL_ENV=/app/.venv", cmd)
        self.assertIn("UV_NO_SYNC=1", cmd)
        # `sh -c`, not `-lc` (login shell would reset PATH and drop the venv).
        self.assertIn("sh", cmd)
        self.assertEqual(cmd[-2], "-c")
        self.assertNotIn("-lc", cmd)
        self.assertEqual(cmd[-1], "pytest -q")
        self.assertTrue(res.ok)
        self.assertEqual(res.source, "check_cmd")
        self.assertEqual(res.network, "contremaitre-cli-egress")

    def test_open_egress_omits_network_flag_and_uses_canary(self):
        captured: dict[str, list[str]] = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return self._proc(0)

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "uv.lock").write_text("", encoding="utf-8")
            res = assert_deps_offline(
                worktree=Path(tmp),
                base_image="img",
                docker_network=None,
                container_user=None,
                deps_volume=None,
                check_cmds=(),
                runner=runner,
            )
        self.assertNotIn("--network", captured["cmd"])
        self.assertEqual(res.source, "canary")
        self.assertEqual(res.cmd, "uv run python -c ''")
        self.assertIsNone(res.network)

    def test_failure_maps_returncode_and_tails_output(self):
        def runner(cmd, **kwargs):
            return self._proc(returncode=1, stdout="build-out", stderr="setuptools missing")

        res = assert_deps_offline(
            worktree=Path("/wt"),
            base_image="img",
            docker_network="net",
            container_user=None,
            deps_volume=None,
            check_cmds=("x",),
            runner=runner,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.returncode, 1)
        self.assertIn("setuptools missing", res.output)

    def test_returns_none_when_nothing_to_assert(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package-lock.json").write_text("{}", encoding="utf-8")
            res = assert_deps_offline(
                worktree=Path(tmp),
                base_image="img",
                docker_network="net",
                container_user=None,
                deps_volume=None,
                check_cmds=(),
                runner=lambda *a, **k: self._proc(0),
            )
        self.assertIsNone(res)

    def test_container_did_not_complete_is_not_ok(self):
        def runner(cmd, **kwargs):
            raise OSError("docker daemon gone")

        res = assert_deps_offline(
            worktree=Path("/wt"),
            base_image="img",
            docker_network="net",
            container_user=None,
            deps_volume=None,
            check_cmds=("x",),
            runner=runner,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.returncode, -1)
        self.assertIn("did not complete", res.output)


if __name__ == "__main__":
    unittest.main()
