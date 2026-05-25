"""Unit tests for the git_message module (extracted commit-message formatting)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from contremaitre.git_message import SETTLED_RELPATH, derive_commit_message


class DeriveCommitMessageTest(unittest.TestCase):
    def test_happy_path_strips_settled_design_prefix(self):
        with TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            settled = worktree / SETTLED_RELPATH
            settled.parent.mkdir(parents=True, exist_ok=True)
            settled.write_text(
                "# Settled design — Collapse the Order intake pipeline\n\n"
                "Merge the three shallow wrappers into one deep module.\n",
                encoding="utf-8",
            )
            title, body = derive_commit_message(worktree, "run-123")
            self.assertEqual(title, "Collapse the Order intake pipeline")
            self.assertIn("Collapse the Order intake pipeline", body)
            self.assertIn("run-123", body)

    def test_happy_path_with_dash_prefix(self):
        with TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            settled = worktree / SETTLED_RELPATH
            settled.parent.mkdir(parents=True, exist_ok=True)
            settled.write_text(
                "# Settled design - Extract gate interface\n\nDetails.\n",
                encoding="utf-8",
            )
            title, body = derive_commit_message(worktree, "run-456")
            self.assertEqual(title, "Extract gate interface")

    def test_happy_path_with_colon_prefix(self):
        with TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            settled = worktree / SETTLED_RELPATH
            settled.parent.mkdir(parents=True, exist_ok=True)
            settled.write_text(
                "# Settled design: Unify JSONL reading\n\nDetails.\n",
                encoding="utf-8",
            )
            title, body = derive_commit_message(worktree, "run-789")
            self.assertEqual(title, "Unify JSONL reading")

    def test_no_prefix_uses_first_line_as_title(self):
        with TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            settled = worktree / SETTLED_RELPATH
            settled.parent.mkdir(parents=True, exist_ok=True)
            settled.write_text(
                "Some plain title\n\nBody content.\n",
                encoding="utf-8",
            )
            title, body = derive_commit_message(worktree, "run-abc")
            self.assertEqual(title, "Some plain title")

    def test_missing_file_falls_back(self):
        with TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            title, body = derive_commit_message(worktree, "run-missing")
            self.assertEqual(title, "Contremaitre refactor (run-missing)")
            self.assertIn("run-missing", body)

    def test_empty_file_falls_back(self):
        with TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            settled = worktree / SETTLED_RELPATH
            settled.parent.mkdir(parents=True, exist_ok=True)
            settled.write_text("", encoding="utf-8")
            title, body = derive_commit_message(worktree, "run-empty")
            self.assertEqual(title, "Contremaitre refactor (run-empty)")

    def test_body_includes_full_settled_text_and_run_trailer(self):
        with TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            settled = worktree / SETTLED_RELPATH
            settled.parent.mkdir(parents=True, exist_ok=True)
            settled_text = "# Settled design — My title\n\nDetailed design body."
            settled.write_text(settled_text, encoding="utf-8")
            title, body = derive_commit_message(worktree, "run-body-check")
            self.assertEqual(title, "My title")
            self.assertIn(settled_text, body)
            self.assertIn("---\nRun: run-body-check", body)

    def test_prefix_match_from_content_without_trailing_space(self):
        with TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            settled = worktree / SETTLED_RELPATH
            settled.parent.mkdir(parents=True, exist_ok=True)
            settled.write_text(
                "# Settled design —\n\nSome content.\n",
                encoding="utf-8",
            )
            title, body = derive_commit_message(worktree, "run-no-space")
            self.assertEqual(title, "Settled design —")


class SettledRelpathTest(unittest.TestCase):
    def test_settled_relpath_is_under_dot_contremaitre(self):
        self.assertEqual(str(SETTLED_RELPATH), ".contremaitre/SETTLED_DESIGN.md")


if __name__ == "__main__":
    unittest.main()
