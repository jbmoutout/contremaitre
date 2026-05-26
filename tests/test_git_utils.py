from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from contremaitre.git_utils import SETTLED_RELPATH, derive_commit_message, only_contremaitre_changes


SETTLED = Path(".contremaitre") / "SETTLED_DESIGN.md"


class DeriveCommitMessageTest(unittest.TestCase):
    """Direct unit tests for derive_commit_message with a temp worktree.

    These tests pin the parsing edge cases — missing file, empty file,
    prefixed title, no prefix — without dragging in Docker or git repos.
    The same function is exercised indirectly through fixture runs in
    test_control_plane.py; these add 0.01s coverage for edge cases the
    fixture doesn't hit.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name)
        self.run_id = "20260526-120000-test"

    def _write_settled(self, content: str) -> None:
        path = self.worktree / SETTLED
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_missing_file_uses_fallback(self):
        title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertIn(self.run_id, title)
        self.assertIn(self.run_id, body)

    def test_empty_file_uses_fallback(self):
        self._write_settled("")
        title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertIn(self.run_id, title)
        self.assertIn(self.run_id, body)

    def test_whitespace_only_file_uses_fallback(self):
        self._write_settled("   \n  \n  ")
        title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertIn(self.run_id, title)
        self.assertIn(self.run_id, body)

    def test_normal_title_no_prefix(self):
        self._write_settled("# Extract shared git helpers\n\nMove three functions.")
        title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertEqual(title, "Extract shared git helpers")
        self.assertIn("Move three functions.", body)

    def test_skill_prefix_em_dash_is_stripped(self):
        self._write_settled("# Settled design — Resolve reverse dependency\n\nDetails.")
        title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertEqual(title, "Resolve reverse dependency")

    def test_skill_prefix_hyphen_is_stripped(self):
        self._write_settled("# Settled design - Resolve reverse dependency\n\nDetails.")
        title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertEqual(title, "Resolve reverse dependency")

    def test_skill_prefix_colon_is_stripped(self):
        self._write_settled("# Settled design: Resolve reverse dependency\n\nDetails.")
        title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertEqual(title, "Resolve reverse dependency")

    def test_case_insensitive_prefix_strip(self):
        self._write_settled("# SETTLED DESIGN — Resolve reverse dep\n\nBody.")
        title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertEqual(title, "Resolve reverse dep")

    def test_body_includes_settled_text_and_trailer(self):
        self._write_settled("# Title\n\nDesign body.")
        _title, body = derive_commit_message(self.worktree, self.run_id)
        self.assertIn("Design body.", body)
        self.assertIn(self.run_id, body)

    def test_no_hash_in_title(self):
        self._write_settled("First line is title\n\nDesign body.")
        title, _body = derive_commit_message(self.worktree, self.run_id)
        self.assertEqual("First line is title", title)


class OnlyContremaitreChangesTest(unittest.TestCase):
    def test_empty_porcelain_is_clean(self):
        self.assertTrue(only_contremaitre_changes(""))

    def test_only_contremaitre_dotfiles_is_clean(self):
        porcelain = "?? .contremaitre/SETTLED_DESIGN.md\n?? .contremaitre/IMPLEMENTATION_COMPLETE\n"
        self.assertTrue(only_contremaitre_changes(porcelain))

    def test_opencode_json_is_clean(self):
        self.assertTrue(only_contremaitre_changes("?? opencode.json\n"))

    def test_real_source_change_is_not_clean(self):
        self.assertFalse(only_contremaitre_changes(" M src/main.py\n"))

    def test_mixed_contremaitre_and_source_is_not_clean(self):
        porcelain = "?? .contremaitre/SETTLED_DESIGN.md\n M src/main.py\n"
        self.assertFalse(only_contremaitre_changes(porcelain))


if __name__ == "__main__":
    unittest.main()
