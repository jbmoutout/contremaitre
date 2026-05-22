"""Tests for the run-viewer assembler.

The viewer is observability — it must build at every run terminus, and it
must not let agent-written content break out of the DATA payload. Two
tests, one for each invariant.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from contremaitre.fixture import init_fixture
from contremaitre.jsonlog import write_json
from contremaitre.models import Caps, RunConfig
from contremaitre.orchestrator import run
from contremaitre.paths import build_run_paths, new_run_id
from contremaitre.viewer import VIEWER_FILENAME, build_viewer


class ViewerTest(unittest.TestCase):
    def test_fake_run_writes_viewer_html(self):
        """End-to-end: orchestrator's `finally` produces viewer.html."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=root / "runs",
            run_slug="viewer-e2e",
            check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
            caps=Caps(),
        )
        result = run(config)
        viewer = result.run_dir / VIEWER_FILENAME
        self.assertTrue(viewer.exists())
        self.assertGreater(viewer.stat().st_size, 1000)

    def test_escapes_closing_script_tag_in_payload(self):
        """`</script>` in extracted-file bodies must not close the page tag."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = build_run_paths(root / "runs", new_run_id("escape"))
        paths.run_dir.mkdir(parents=True)
        write_json(paths.stats, {"run_id": paths.run_id, "verdict": "FAILED_INFRA"})
        paths.extracted_files_dir.mkdir(parents=True)
        (paths.extracted_files_dir / "evil.html").write_text(
            "<script>alert(1)</script>", encoding="utf-8",
        )

        out = build_viewer(paths)
        html = out.read_text(encoding="utf-8")
        marker = "const DATA = "
        start = html.index(marker) + len(marker)
        end = html.index(";\n</script>", start)
        self.assertNotIn("</script>", html[start:end])


if __name__ == "__main__":
    unittest.main()
