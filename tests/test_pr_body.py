"""Tests for the PR body assembly and the runs-index blurb.

`_derive_pr_metadata` is the single producer of the PR body — it stitches
the IMPLEMENTATION_COMPLETE blockquote, the demoted SETTLED_DESIGN, the SIM
review, and the eval scorecard into one self-contained document. The
viewer index uses the same IMPLEMENTATION_COMPLETE marker as the row
blurb. These tests lock the shape so regressions surface in CI rather
than on a reviewer's PR.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from contremaitre.paths import build_run_paths
from contremaitre.pr_body import render_pr_body
from contremaitre.viewer.index import _read_impl_complete, _summarize_run


def _seed_worktree(worktree: Path, *, settled: str, impl_complete: str | None) -> None:
    (worktree / ".contremaitre").mkdir(parents=True, exist_ok=True)
    (worktree / ".contremaitre" / "SETTLED_DESIGN.md").write_text(settled, encoding="utf-8")
    if impl_complete is not None:
        (worktree / ".contremaitre" / "IMPLEMENTATION_COMPLETE").write_text(
            impl_complete,
            encoding="utf-8",
        )


def _seed_eval(eval_dir: Path, payload: dict) -> None:
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "pr_eval.json").write_text(json.dumps(payload), encoding="utf-8")


_SETTLED_FIXTURE = (
    "# SETTLED DESIGN — Extract foo into bar\n"
    "\n"
    "## Seam\n"
    "\n"
    "`foo` in `mod.py` does two things.\n"
    "\n"
    "## What sits behind the seam\n"
    "\n"
    "A pure `bar()` function.\n"
)


class DerivePrMetadataTest(unittest.TestCase):
    def _paths(self, tmp: Path):
        paths = build_run_paths(tmp / "runs", f"20260526-{tmp.name}")
        paths.run_dir.mkdir(parents=True)
        paths.worktree.mkdir(parents=True, exist_ok=True)
        return paths

    def test_impl_complete_renders_as_blockquote_under_lede(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            _seed_worktree(
                paths.worktree,
                settled=_SETTLED_FIXTURE,
                impl_complete="Extracted bar; 42/42 tests pass.",
            )
            _, body = render_pr_body(paths, diff_hash="deadbeef" * 8)
            self.assertIn("> Extracted bar; 42/42 tests pass.", body)
            # Blockquote sits between the lede and the `## Design` separator.
            blockquote_idx = body.index("> Extracted bar")
            design_idx = body.index("## Design")
            self.assertLess(blockquote_idx, design_idx)

    def test_missing_impl_complete_still_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            _seed_worktree(paths.worktree, settled=_SETTLED_FIXTURE, impl_complete=None)
            _, body = render_pr_body(paths, diff_hash="abc" * 16)
            self.assertNotIn("> ", body.split("## Design")[0])
            self.assertIn("## Design", body)

    def test_settled_headings_demoted_two_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            _seed_worktree(
                paths.worktree,
                settled=_SETTLED_FIXTURE,
                impl_complete="ok",
            )
            _, body = render_pr_body(paths, diff_hash="abc" * 16)
            # SETTLED's H1 must be gone; demoted to H3.
            self.assertNotIn("\n# SETTLED DESIGN", body)
            self.assertIn("### SETTLED DESIGN — Extract foo into bar", body)
            # H2 in SETTLED becomes H4. The body's own H2 (## Design, ##
            # SIM review) must remain — exactly two of them.
            h2_lines = [ln for ln in body.splitlines() if re.match(r"^## [^#]", ln)]
            self.assertEqual(
                {ln.strip() for ln in h2_lines},
                {"## Design", "## SIM review"},
            )
            self.assertIn("#### Seam", body)

    def test_scorecard_block_renders_from_pr_eval(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            _seed_worktree(
                paths.worktree,
                settled=_SETTLED_FIXTURE,
                impl_complete="ok",
            )
            _seed_eval(
                paths.eval_dir,
                {
                    "hard_gates": "PASS",
                    "scorecard": {
                        "sim_confidence": 0.95,
                        "extra_reviewer_confidence": 1.0,
                        "cross_family_agreement": True,
                        "self_verified": True,
                        "settled_before_code": True,
                    },
                },
            )
            _, body = render_pr_body(paths, diff_hash="abc" * 16)
            self.assertIn("<summary>Eval scorecard</summary>", body)
            self.assertIn("Hard gates: ✓ PASS", body)
            self.assertIn("cross-family agreement", body)
            self.assertIn("self-verified ✓", body)
            self.assertIn("settled-before-code ✓", body)

    def test_scorecard_omitted_when_pr_eval_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            _seed_worktree(
                paths.worktree,
                settled=_SETTLED_FIXTURE,
                impl_complete="ok",
            )
            _, body = render_pr_body(paths, diff_hash="abc" * 16)
            self.assertNotIn("Eval scorecard", body)

    def test_body_has_no_duplicate_run_footer(self):
        # `derive_commit_message` appends `---\nRun: <id>` for the commit
        # message. The PR body has its own footer with run id + diff hash,
        # so the commit-only trailer must be stripped.
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            _seed_worktree(
                paths.worktree,
                settled=_SETTLED_FIXTURE,
                impl_complete="ok",
            )
            _, body = render_pr_body(paths, diff_hash="abc" * 16)
            self.assertEqual(body.count(f"Run: {paths.run_id}"), 0)
            self.assertEqual(body.count(f"`{paths.run_id}`"), 1)


class IndexReadImplCompleteTest(unittest.TestCase):
    def test_reads_marker_from_extracted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260526-test-run"
            (run_dir / "extracted_files").mkdir(parents=True)
            (run_dir / "extracted_files" / ".contremaitre__IMPLEMENTATION_COMPLETE").write_text(
                "Did the thing.\n",
                encoding="utf-8",
            )
            self.assertEqual(_read_impl_complete(run_dir), "Did the thing.")

    def test_returns_empty_when_marker_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260526-no-marker"
            run_dir.mkdir()
            self.assertEqual(_read_impl_complete(run_dir), "")

    def test_summarize_run_exposes_impl_complete(self):
        # _summarize_run must surface impl_complete so the renderer can
        # prefer it over the SETTLED preamble — that's the whole point of
        # the index-desc change.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260526-summary"
            (run_dir / "extracted_files").mkdir(parents=True)
            (run_dir / "extracted_files" / ".contremaitre__IMPLEMENTATION_COMPLETE").write_text(
                "Refactored X into Y.",
                encoding="utf-8",
            )
            row = _summarize_run(run_dir)
            self.assertEqual(row["impl_complete"], "Refactored X into Y.")


if __name__ == "__main__":
    unittest.main()
