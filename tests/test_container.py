"""Unit tests for container.py — build_argv (pure, no Docker needed)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre.container import DockerContainerLifecycle
from contremaitre.models import ActorMode, DepsVolume, RunConfig
from contremaitre.paths import build_run_paths


class BuildArgvTest(unittest.TestCase):
    """100% pure — no Docker, no subprocess."""

    def _lifecycle(self) -> DockerContainerLifecycle:
        return DockerContainerLifecycle()

    def _config(self, **overrides: object) -> RunConfig:
        kwargs: dict[str, object] = dict(
            repo=Path("/repo"),
            base="main",
            runs_root=Path("/runs"),
            run_slug="t",
            actor_mode=ActorMode.OPENCODE,
            docker_image="img",
        )
        kwargs.update(overrides)
        return RunConfig(**kwargs)  # type: ignore[arg-type]

    def _paths(self, root: Path) -> tuple:  # -> (paths, worktree)
        paths = build_run_paths(root / "runs", "test-run")
        paths.run_dir.mkdir(parents=True)
        wt = root / "wt"
        wt.mkdir()
        return paths, wt

    def test_zen_model_needs_no_key(self):
        """Free Zen model — no OPENROUTER_API_KEY required."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config()
            with patch.dict(os.environ, {}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="opencode/deepseek-v4-flash-free",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        self.assertEqual(cmd[:3], ["docker", "run", "-d"])

    def test_non_zen_model_requires_key(self):
        """Non-Zen model — must raise when OPENROUTER_API_KEY unset."""
        from contremaitre.actors import ActorError

        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config()
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ActorError):
                    lc.build_argv(
                        config=config,
                        paths=paths,
                        worktree=wt,
                        state_dir=Path(tmp),
                        mount_mode="rw",
                        model="openrouter/anthropic/claude-sonnet-4.6",
                        prompt="p",
                        session_id=None,
                        role="agent",
                    )

    def test_session_id_is_included(self):
        """Session id forwarded as --session flag."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id="sess-42",
                    role="agent",
                )
        self.assertIn("--session", cmd)
        self.assertIn("sess-42", cmd)

    def test_proxy_vars_forwarded(self):
        """HTTP_PROXY, HTTPS_PROXY, NO_PROXY passed as -e flags."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config(
                http_proxy="http://proxy:8080",
                https_proxy="https://proxy:8443",
                no_proxy="localhost",
            )
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, env = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        # Env dict should have proxy vars
        self.assertEqual(env["HTTP_PROXY"], "http://proxy:8080")
        self.assertEqual(env["HTTPS_PROXY"], "https://proxy:8443")
        self.assertEqual(env["NO_PROXY"], "localhost")
        # And they should be forwarded as -e flags too (Docker needs them)
        e_idx = [i for i, v in enumerate(cmd) if v == "-e"]
        e_args = [cmd[i + 1] for i in e_idx]
        self.assertIn("HTTP_PROXY", e_args)
        self.assertIn("HTTPS_PROXY", e_args)
        self.assertIn("NO_PROXY", e_args)

    def test_no_proxy_when_unset(self):
        """No proxy vars in cmd or env when config omits them."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, env = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        e_idx = [i for i, v in enumerate(cmd) if v == "-e"]
        e_args = [cmd[i + 1] for i in e_idx]
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            self.assertNotIn(var, e_args)
            self.assertNotIn(var, env)

    def test_labels_carry_run_id_and_role(self):
        """Container labels include run-id and role."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        self.assertIn("--label", cmd)
        label_idx = cmd.index("--label") + 1
        self.assertIn("contremaitre.run-id=test-run", cmd[label_idx])
        role_idx = cmd.index("--label", label_idx) + 1
        self.assertIn("contremaitre.role=agent", cmd[role_idx])

    def test_extra_mounts_appended(self):
        """Extra mounts passed through as -v flags."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config()
            extra = [(Path("/host/path"), "/container/path", "ro")]
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                    extra_mounts=extra,
                )
        v_idx = [i for i, v in enumerate(cmd) if v == "-v"]
        v_args = [cmd[i + 1] for i in v_idx]
        self.assertIn("/host/path:/container/path:ro", v_args)

    def test_container_user(self):
        """User flag forwarded when set."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config(container_user="1000:1000")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        self.assertIn("--user", cmd)
        self.assertIn("1000:1000", cmd)

    def test_docker_network(self):
        """Network flag forwarded when set."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config(docker_network="my-net")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        self.assertIn("--network", cmd)
        self.assertIn("my-net", cmd)

    def test_deps_volume(self):
        """Deps volume mounted when present."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            dv = DepsVolume(
                name="dv-hash", mount_path="venv", runtime_env=[("PIP_DIR", "/app/venv")]
            )
            config = self._config(deps_volume=dv)
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        v_idx = [i for i, v in enumerate(cmd) if v == "-e"]
        e_args = [cmd[i + 1] for i in v_idx]
        self.assertIn("PIP_DIR=/app/venv", e_args)

    def test_opencode_config_mounted(self):
        """opencode.json bind-mounted when set."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            cfg = Path(tmp) / "opencode.json"
            cfg.write_text("{}")
            config = self._config(opencode_config=cfg)
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        v_idx = [i for i, v in enumerate(cmd) if v == "-v"]
        v_args = [cmd[i + 1] for i in v_idx]
        self.assertTrue(any("/opencode.json:ro" in a for a in v_args))

    def test_prompt_in_opencode_cmd(self):
        """Prompt is the last argument."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="Hello world",
                    session_id=None,
                    role="agent",
                )
        self.assertEqual(cmd[-1], "Hello world")

    def test_mount_mode_reflected(self):
        """Worktree mount uses the requested mode."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="ro",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="sim",
                )
        v_idx = [i for i, v in enumerate(cmd) if v == "-v"]
        v_args = [cmd[i + 1] for i in v_idx]
        wt_mounts = [a for a in v_args if str(wt) in a]
        self.assertTrue(any(wt_mount.endswith(":ro") for wt_mount in wt_mounts))

    def test_openrouter_env_var_present(self):
        """OPENROUTER_API_KEY forwarded as -e flag."""
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            paths, wt = self._paths(Path(tmp))
            config = self._config(openrouter_env_var="OPENROUTER_API_KEY")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-xxx"}, clear=True):
                cmd, _ = lc.build_argv(
                    config=config,
                    paths=paths,
                    worktree=wt,
                    state_dir=Path(tmp),
                    mount_mode="rw",
                    model="openrouter/m",
                    prompt="p",
                    session_id=None,
                    role="agent",
                )
        e_idx = [i for i, v in enumerate(cmd) if v == "-e"]
        e_args = [cmd[i + 1] for i in e_idx]
        self.assertIn("OPENROUTER_API_KEY", e_args)
