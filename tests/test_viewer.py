"""Tests for the run-viewer assembler.

The viewer is observability — it must build at every run terminus, and it
must not let agent-written content break out of the DATA payload. Two
tests, one for each invariant.
"""

from __future__ import annotations

import json
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


def _extract_data_payload(html: str) -> dict:
    """Pull the JSON between `const DATA = ` and `;\\n</script>`.

    The viewer test relies on this exact framing — the renderer reads it.
    """

    marker = "const DATA = "
    start = html.index(marker) + len(marker)
    end = html.index(";\n</script>", start)
    return json.loads(html[start:end])


class ViewerTest(unittest.TestCase):
    def test_fake_run_writes_viewer_html(self):
        """End-to-end: orchestrator's `finally` produces viewer.html with
        structural anchors and a DATA payload tagged with this run's id.

        The previous version only asserted `size > 1000` — the embedded CSS
        alone is 20KB, so a viewer that wrote only the CSS (no DATA, no
        body) would still pass. These assertions catch that regression.
        """

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
        html = viewer.read_text(encoding="utf-8")
        # Structural anchors — these would all be absent in a viewer that
        # only dumped the CSS or only rendered an error page.
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("const DATA = ", html)
        self.assertIn("</html>", html)
        # The DATA payload is parseable JSON and identifies THIS run.
        data = _extract_data_payload(html)
        self.assertEqual(data.get("run_id"), result.run_id)

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
        # The escaped payload is still parseable as JSON — escaping must
        # not corrupt the data the renderer reads.
        _extract_data_payload(html)


if __name__ == "__main__":
    unittest.main()
