from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from contremaitre import cli
from contremaitre.models import ActorMode, RunConfig


def _make_config(image: str, dockerfile_dir: Path) -> RunConfig:
    return RunConfig(
        repo=dockerfile_dir,
        base="main",
        runs_root=dockerfile_dir / "runs",
        run_slug="rebuild-test",
        actor_mode=ActorMode.OPENCODE,
        docker_image=image,
    )


class DockerfileHashRebuildTest(unittest.TestCase):
    """`_ensure_default_image_built` rebuilds when the Dockerfile changes.

    This is the protection against the silent-staleness class of bug
    (Dockerfile edited, image not rebuilt — the live image still missing
    `python3`/`uv` even after the operator added them to the Dockerfile).
    """

    def _patch_dockerfile(self, dockerfile: Path):
        # Both production Dockerfiles get patched to point at the temp
        # file so _ensure_default_image_built routes through it for both
        # base and variant images.
        return patch.dict(
            cli._VARIANT_DOCKERFILES,
            {"base": dockerfile, "rust": dockerfile, "go": dockerfile},
        )

    def test_inspect_missing_triggers_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            df = Path(tmp) / "Dockerfile"
            df.write_text("FROM scratch\n", encoding="utf-8")
            config = _make_config(cli._DEFAULT_IMAGE, Path(tmp))
            with (
                self._patch_dockerfile(df),
                patch("contremaitre.cli.subprocess.run") as fake_run,
                patch("contremaitre.cli._build_image_inline", return_value=0) as fake_build,
            ):
                # inspect returns non-zero → image missing
                fake_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
                rc = cli._ensure_default_image_built(config)
        self.assertEqual(rc, 0)
        fake_build.assert_called_once()
        # Variant dispatch routed the build at the patched Dockerfile for
        # the configured image. A regression that built from the wrong
        # variant (e.g. always `base`, ignoring `rust`/`go`) would surface
        # here as a path mismatch.
        kwargs = fake_build.call_args.kwargs
        self.assertEqual(kwargs["dockerfile"], df)
        self.assertEqual(kwargs["image_name"], cli._DEFAULT_IMAGE)

    def test_hash_match_skips_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            df = Path(tmp) / "Dockerfile"
            content = b"FROM scratch\nRUN echo cached\n"
            df.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            config = _make_config(cli._DEFAULT_IMAGE, Path(tmp))
            with (
                self._patch_dockerfile(df),
                patch("contremaitre.cli.subprocess.run") as fake_run,
                patch("contremaitre.cli._build_image_inline", return_value=0) as fake_build,
            ):
                fake_run.return_value = MagicMock(returncode=0, stdout=digest + "\n", stderr="")
                rc = cli._ensure_default_image_built(config)
        self.assertEqual(rc, 0)
        fake_build.assert_not_called()

    def test_hash_mismatch_triggers_rebuild(self):
        """Image exists but its Dockerfile-hash label is from a previous
        revision — the canonical "stale image" path. Must rebuild.
        """

        with tempfile.TemporaryDirectory() as tmp:
            df = Path(tmp) / "Dockerfile"
            df.write_bytes(b"FROM scratch\nRUN echo new\n")
            config = _make_config(cli._DEFAULT_IMAGE, Path(tmp))
            with (
                self._patch_dockerfile(df),
                patch("contremaitre.cli.subprocess.run") as fake_run,
                patch("contremaitre.cli._build_image_inline", return_value=0) as fake_build,
            ):
                fake_run.return_value = MagicMock(
                    returncode=0, stdout="stale-hash-from-prior-build\n", stderr=""
                )
                rc = cli._ensure_default_image_built(config)
        self.assertEqual(rc, 0)
        fake_build.assert_called_once()

    def test_missing_label_triggers_rebuild(self):
        """Pre-fix images have no label at all. First post-fix run must
        rebuild rather than trust the unlabelled image.
        """

        with tempfile.TemporaryDirectory() as tmp:
            df = Path(tmp) / "Dockerfile"
            df.write_bytes(b"FROM scratch\n")
            config = _make_config(cli._DEFAULT_IMAGE, Path(tmp))
            with (
                self._patch_dockerfile(df),
                patch("contremaitre.cli.subprocess.run") as fake_run,
                patch("contremaitre.cli._build_image_inline", return_value=0) as fake_build,
            ):
                fake_run.return_value = MagicMock(returncode=0, stdout="\n", stderr="")
                cli._ensure_default_image_built(config)
        fake_build.assert_called_once()

    def test_cli_actor_mode_triggers_build(self):
        """Codex (CLI) agent shares the same container image as opencode, so
        a missing image must auto-build — the old gate bailed for any
        non-opencode mode and left `codex: command not found` in-container.
        """

        with tempfile.TemporaryDirectory() as tmp:
            df = Path(tmp) / "Dockerfile"
            df.write_text("FROM scratch\n", encoding="utf-8")
            config = RunConfig(
                repo=Path(tmp),
                base="main",
                runs_root=Path(tmp) / "runs",
                run_slug="cli-build",
                actor_mode=ActorMode.CLI,
                docker_image=cli._DEFAULT_IMAGE,
            )
            with (
                self._patch_dockerfile(df),
                patch("contremaitre.cli.subprocess.run") as fake_run,
                patch("contremaitre.cli._build_image_inline", return_value=0) as fake_build,
            ):
                fake_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
                rc = cli._ensure_default_image_built(config)
        self.assertEqual(rc, 0)
        fake_build.assert_called_once()

    def test_composite_mixed_modes_triggers_build(self):
        """A mixed pair (fake agent + codex SIM here) still shares one image.
        The build decision is the UNION of both roles, not just the agent.
        """

        with tempfile.TemporaryDirectory() as tmp:
            df = Path(tmp) / "Dockerfile"
            df.write_text("FROM scratch\n", encoding="utf-8")
            config = RunConfig(
                repo=Path(tmp),
                base="main",
                runs_root=Path(tmp) / "runs",
                run_slug="mix-build",
                actor_mode=ActorMode.FAKE,
                sim_actor_mode=ActorMode.CLI,
                docker_image=cli._DEFAULT_IMAGE,
            )
            with (
                self._patch_dockerfile(df),
                patch("contremaitre.cli.subprocess.run") as fake_run,
                patch("contremaitre.cli._build_image_inline", return_value=0) as fake_build,
            ):
                fake_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
                rc = cli._ensure_default_image_built(config)
        self.assertEqual(rc, 0)
        fake_build.assert_called_once()

    def test_fake_actor_mode_skips_entirely(self):
        """No docker work in fake-mode test paths."""

        with tempfile.TemporaryDirectory() as tmp:
            config = RunConfig(
                repo=Path(tmp),
                base="main",
                runs_root=Path(tmp) / "runs",
                run_slug="fake-test",
                actor_mode=ActorMode.FAKE,
                docker_image=cli._DEFAULT_IMAGE,
            )
            with patch("contremaitre.cli.subprocess.run") as fake_run:
                rc = cli._ensure_default_image_built(config)
        self.assertEqual(rc, 0)
        fake_run.assert_not_called()

    def test_unknown_image_name_skips(self):
        """Operator-supplied custom images aren't auto-rebuilt — preflight
        surfaces a clean missing-image error with the build hint.
        """

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config("my-org/custom-image:v3", Path(tmp))
            with patch("contremaitre.cli.subprocess.run") as fake_run:
                rc = cli._ensure_default_image_built(config)
        self.assertEqual(rc, 0)
        fake_run.assert_not_called()


class BuildImageStampsLabelTest(unittest.TestCase):
    def test_build_invocation_includes_dockerfile_hash_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            df = Path(tmp) / "Dockerfile"
            content = b"FROM scratch\nRUN echo hi\n"
            df.write_bytes(content)
            expected = hashlib.sha256(content).hexdigest()
            with (
                patch("contremaitre.cli.subprocess.run") as fake_run,
                patch("contremaitre.cli._prune_dangling_images"),
            ):
                fake_run.return_value = MagicMock(returncode=0)
                rc = cli._build_image_inline(
                    image_name="contremaitre-agent:latest",
                    dockerfile=df,
                    no_cache=False,
                )
        self.assertEqual(rc, 0)
        cmd = fake_run.call_args.args[0]
        # `--label contremaitre.dockerfile-sha256=<hex>` must be in the
        # build command — that's how the next staleness check finds it.
        self.assertIn("--label", cmd)
        idx = cmd.index("--label")
        self.assertEqual(cmd[idx + 1], f"{cli._DOCKERFILE_HASH_LABEL}={expected}")


if __name__ == "__main__":
    unittest.main()
