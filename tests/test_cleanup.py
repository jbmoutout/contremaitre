from __future__ import annotations

import io
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre.cleanup import (
    prune_dangling_images,
    run_cleanup,
    scan_dangling_images,
    scan_stale_containers,
    scan_stale_worktrees,
)


class ScanStaleContainersTest(unittest.TestCase):
    def test_returns_stale_when_run_dir_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "runs"
            (runs_root / "missing-run").mkdir(parents=True)
            known_stale = runs_root / "20250101-120000-my-run"
            known_stale.mkdir()
            runs_root = Path(tmp) / "other-runs"
            runs_root.mkdir()
            fake_docker_output = (
                "abc123\t20250101-120000-my-run\n"
                "def456\t19990101-000000-old-run\n"
            )
            with patch(
                "contremaitre.cleanup.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["docker", "ps", "-aq"], 0,
                    stdout=fake_docker_output, stderr="",
                ),
            ):
                stale = scan_stale_containers(runs_root)

        self.assertEqual(stale, [("def456", "19990101-000000-old-run")])

    def test_docker_failure_returns_empty(self):
        with patch(
            "contremaitre.cleanup.subprocess.run",
            side_effect=OSError("no docker"),
        ):
            self.assertEqual(scan_stale_containers(Path("/tmp")), [])

    def test_nonzero_returncode_returns_empty(self):
        with patch(
            "contremaitre.cleanup.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["docker", "ps", "-aq"], 1, stdout="", stderr="error",
            ),
        ):
            self.assertEqual(scan_stale_containers(Path("/tmp")), [])


class ScanStaleWorktreesTest(unittest.TestCase):
    def test_returns_stale_worktree_when_run_dir_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktrees_root = Path(tmp) / "worktrees"
            worktrees_root.mkdir()
            stale_wt = worktrees_root / "contremaitre-20250101-120000-my-run"
            stale_wt.mkdir()
            # runs_root exists but has no subdir matching this worktree
            runs_root = worktrees_root / "runs"
            runs_root.mkdir()

            with patch(
                "contremaitre.cleanup.Path.glob",
                return_value=[stale_wt],
            ), patch(
                "contremaitre.cleanup.Path.exists",
                return_value=True,
            ):
                stale = scan_stale_worktrees(runs_root)

        self.assertEqual(stale, [stale_wt])

    def test_ignores_active_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "runs"
            run_id = "20250101-120000-my-run"
            (runs_root / run_id).mkdir(parents=True)
            wt = Path(tmp) / f"contremaitre-{run_id}"
            wt.mkdir()

            with patch(
                "contremaitre.cleanup.Path.glob",
                return_value=[wt],
            ), patch(
                "contremaitre.cleanup.Path.exists",
                return_value=True,
            ):
                stale = scan_stale_worktrees(runs_root)

        self.assertEqual(stale, [])


class ScanDanglingImagesTest(unittest.TestCase):
    def test_returns_image_ids(self):
        with patch(
            "contremaitre.cleanup.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["docker", "images", "-q"], 0,
                stdout="sha256:abc\nsha256:def\n", stderr="",
            ),
        ):
            self.assertEqual(
                scan_dangling_images(),
                ["sha256:abc", "sha256:def"],
            )

    def test_docker_failure_returns_empty(self):
        with patch(
            "contremaitre.cleanup.subprocess.run",
            side_effect=OSError("no docker"),
        ):
            self.assertEqual(scan_dangling_images(), [])


class PruneDanglingImagesTest(unittest.TestCase):
    def test_calls_docker_image_prune(self):
        mock = unittest.mock.MagicMock()
        with patch("contremaitre.cleanup.subprocess.run", mock):
            prune_dangling_images()
        mock.assert_called_once()
        args = mock.call_args[0][0]
        self.assertEqual(args[:3], ["docker", "image", "prune"])
        self.assertIn("-f", args)


class RunCleanupTest(unittest.TestCase):
    def test_nothing_to_do_when_all_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "runs"
            runs_root.mkdir()
            out = io.StringIO()
            with patch("contremaitre.cleanup.scan_stale_containers", return_value=[]), \
                 patch("contremaitre.cleanup.scan_stale_worktrees", return_value=[]), \
                 patch("contremaitre.cleanup.scan_dangling_images", return_value=[]), \
                 patch("contremaitre.cleanup.list_deps_volumes", return_value=[]), \
                 patch("contremaitre.cleanup._list_cache_clones", return_value=[]), \
                 patch("sys.stdout", out):
                rc = run_cleanup(runs_root=runs_root)

        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out.getvalue())

    def test_dry_run_lists_without_removing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "runs"
            runs_root.mkdir()
            out = io.StringIO()
            with patch("contremaitre.cleanup.scan_stale_containers",
                       return_value=[("c1", "run-1"), ("c2", "run-2")]), \
                 patch("contremaitre.cleanup.scan_stale_worktrees",
                       return_value=[Path("/tmp/contremaitre-stale")]), \
                 patch("contremaitre.cleanup.scan_dangling_images",
                       return_value=["img1"]), \
                 patch("contremaitre.cleanup.list_deps_volumes", return_value=[]), \
                 patch("contremaitre.cleanup._list_cache_clones", return_value=[]), \
                 patch("sys.stdout", out):
                rc = run_cleanup(runs_root=runs_root, dry_run=True)

        self.assertEqual(rc, 0)
        output = out.getvalue()
        self.assertIn("would remove (dry-run)", output)
        self.assertIn("container c1", output)
        self.assertIn("container c2", output)
        self.assertIn("worktree", output)
        self.assertIn("1 dangling image(s)", output)

    def test_executes_removal_when_not_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "runs"
            runs_root.mkdir()
            docker_rm = unittest.mock.MagicMock(
                return_value=subprocess.CompletedProcess(
                    ["docker", "rm", "-f", "c1"], 0, stdout="c1", stderr="",
                )
            )
            docker_vol = unittest.mock.MagicMock(
                return_value=subprocess.CompletedProcess(
                    ["docker", "volume", "rm", "-f", "vol1"], 0, stdout="vol1", stderr="",
                )
            )
            image_prune = unittest.mock.MagicMock(
                return_value=subprocess.CompletedProcess(
                    ["docker", "image", "prune", "-f"], 0, stdout="", stderr="",
                )
            )
            out = io.StringIO()
            with patch("contremaitre.cleanup.scan_stale_containers",
                       return_value=[("c1", "run-1")]), \
                 patch("contremaitre.cleanup.scan_stale_worktrees",
                       return_value=[]), \
                 patch("contremaitre.cleanup.scan_dangling_images",
                       return_value=["img1"]), \
                 patch("contremaitre.cleanup.list_deps_volumes",
                       return_value=["vol1"]), \
                 patch("contremaitre.cleanup._list_cache_clones",
                       return_value=[]), \
                 patch("contremaitre.cleanup.subprocess.run",
                       side_effect=[docker_rm(), docker_vol(), image_prune()]), \
                 patch("sys.stdout", out):
                rc = run_cleanup(runs_root=runs_root, dry_run=False, deps=True)

        self.assertEqual(rc, 0)
        output = out.getvalue()
        self.assertIn("removed", output)

    def test_deps_and_repos_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "runs"
            runs_root.mkdir()
            out = io.StringIO()
            with patch("contremaitre.cleanup.scan_stale_containers", return_value=[]), \
                 patch("contremaitre.cleanup.scan_stale_worktrees", return_value=[]), \
                 patch("contremaitre.cleanup.scan_dangling_images", return_value=[]), \
                 patch("contremaitre.cleanup.list_deps_volumes",
                       return_value=["vol1"]), \
                 patch("contremaitre.cleanup._list_cache_clones",
                       return_value=[Path("/tmp/clone1")]), \
                 patch("sys.stdout", out):
                rc = run_cleanup(
                    runs_root=runs_root, dry_run=True, deps=True, repos=True,
                )

        self.assertEqual(rc, 0)
        output = out.getvalue()
        self.assertIn("deps-vol", output)
        self.assertIn("clone", output)


if __name__ == "__main__":
    unittest.main()
