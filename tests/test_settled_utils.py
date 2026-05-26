"""Unit tests for settled_utils.read_settled_design."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from contremaitre.settled_utils import read_settled_design


class ReadSettledDesignTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name)
        self.settled = self.worktree / ".contremaitre"
        self.settled.mkdir(parents=True, exist_ok=True)
        self.run_id = "test-001"

    def test_missing_returns_fallback_title_and_empty_body(self):
        title, body = read_settled_design(self.worktree, self.run_id)
        self.assertEqual(title, f"Contremaitre refactor ({self.run_id})")
        self.assertEqual(body, "")

    def test_empty_returns_fallback_title_and_empty_body(self):
        (self.settled / "SETTLED_DESIGN.md").write_text("  \n  \n", encoding="utf-8")
        title, body = read_settled_design(self.worktree, self.run_id)
        self.assertEqual(title, f"Contremaitre refactor ({self.run_id})")
        self.assertEqual(body, "")

    def test_title_is_first_non_empty_line(self):
        (self.settled / "SETTLED_DESIGN.md").write_text(
            "# Deepen the publication boundary\n\nSome body text.",
            encoding="utf-8",
        )
        title, body = read_settled_design(self.worktree, self.run_id)
        self.assertEqual(title, "Deepen the publication boundary")
        self.assertEqual(body, "# Deepen the publication boundary\n\nSome body text.")

    def test_strips_settled_design_prefix(self):
        (self.settled / "SETTLED_DESIGN.md").write_text(
            "# Settled design — Deepen the publication boundary\n\nBody.",
            encoding="utf-8",
        )
        title, _ = read_settled_design(self.worktree, self.run_id)
        self.assertEqual(title, "Deepen the publication boundary")

    def test_strips_various_prefix_forms(self):
        for prefix in ("Settled design — ", "Settled design - ", "Settled design: "):
            (self.settled / "SETTLED_DESIGN.md").write_text(
                f"# {prefix}Title\n\nBody.", encoding="utf-8",
            )
            title, _ = read_settled_design(self.worktree, self.run_id)
            self.assertEqual(title, "Title")

    def test_body_is_raw_no_trailer_appended(self):
        (self.settled / "SETTLED_DESIGN.md").write_text(
            "# Title\n\nSome content.", encoding="utf-8",
        )
        _, body = read_settled_design(self.worktree, self.run_id)
        self.assertEqual(body, "# Title\n\nSome content.")
        self.assertNotIn("---", body)
        self.assertNotIn("Run:", body)

    def test_complex_body_preserved(self):
        content = (
            "# Title\n\n"
            "## Seam\n\n"
            "Some description.\n\n"
            "## Behind the seam\n\n"
            "- Item 1\n"
            "- Item 2\n"
        )
        (self.settled / "SETTLED_DESIGN.md").write_text(content, encoding="utf-8")
        _, body = read_settled_design(self.worktree, self.run_id)
        self.assertEqual(body, content.strip())
