from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from contremaitre.worktree_metadata import (
    IMPLEMENTATION_COMPLETE_RELPATH,
    SETTLED_RELPATH,
    derive_commit_message,
    read_impl_complete,
)


class SettledRelpathTest(unittest.TestCase):
    def test_settled_relpath_is_dot_contremaitre_settled(self):
        self.assertEqual(SETTLED_RELPATH, Path(".contremaitre") / "SETTLED_DESIGN.md")

    def test_impl_complete_relpath_is_dot_contremaitre_marker(self):
        self.assertEqual(
            IMPLEMENTATION_COMPLETE_RELPATH,
            Path(".contremaitre") / "IMPLEMENTATION_COMPLETE",
        )


class DeriveCommitMessageTest(unittest.TestCase):
    def test_returns_title_and_body_from_settled(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / ".contremaitre").mkdir()
            (worktree / ".contremaitre" / "SETTLED_DESIGN.md").write_text(
                "# Extract foo into bar\n\nSome design text.\n", encoding="utf-8",
            )
            title, body = derive_commit_message(worktree, "20260603-test-run")
            self.assertEqual(title, "Extract foo into bar")
            self.assertIn("Some design text.", body)
            self.assertIn("Run: 20260603-test-run", body)

    def test_strips_settled_design_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / ".contremaitre").mkdir()
            (worktree / ".contremaitre" / "SETTLED_DESIGN.md").write_text(
                "# Settled design — Extract foo into bar\n", encoding="utf-8",
            )
            title, _ = derive_commit_message(worktree, "run-1")
            self.assertEqual(title, "Extract foo into bar")

    def test_falls_back_when_settled_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            title, body = derive_commit_message(Path(tmp), "run-missing")
            self.assertIn("Contremaitre refactor", title)
            self.assertIn("Run: run-missing", body)

    def test_falls_back_when_settled_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / ".contremaitre").mkdir()
            (worktree / ".contremaitre" / "SETTLED_DESIGN.md").write_text("", encoding="utf-8")
            title, _ = derive_commit_message(worktree, "run-empty")
            self.assertIn("Contremaitre refactor", title)


class ReadImplCompleteTest(unittest.TestCase):
    def test_returns_content_when_marker_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "IMPLEMENTATION_COMPLETE"
            marker.write_text("Extracted bar; 42/42 tests pass.\n", encoding="utf-8")
            self.assertEqual(
                read_impl_complete(marker), "Extracted bar; 42/42 tests pass.",
            )

    def test_returns_empty_when_marker_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "IMPLEMENTATION_COMPLETE"
            self.assertEqual(read_impl_complete(marker), "")




if __name__ == "__main__":
    unittest.main()
