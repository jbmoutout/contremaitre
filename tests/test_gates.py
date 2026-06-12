"""Unit tests for the Hard gates (L0) Module — contremaitre/gates.py.

The L0 gate recipe used to be open-coded (and duplicated) inside the
orchestrator, reachable only through full FAKE-mode runs. With it behind
`gates.evaluate_l0` + the internal-path predicates, the gate logic has a direct
test surface: the cases below pin behaviour the orchestrator only exercised
end-to-end.
"""

from __future__ import annotations

import unittest

from contremaitre import gates


class _FakeRepo:
    """Minimal GitRepo stand-in: just the three reads `evaluate_l0` performs."""

    def __init__(self, *, diff: bytes, changed: list[str], porcelain: str):
        self._diff = diff
        self._changed = changed
        self._porcelain = porcelain

    def diff_bytes(self, base: str) -> bytes:  # diff_hash()
        return self._diff

    def output(self, *args: str) -> str:  # scan_diff() → diff --name-only
        return "\n".join(self._changed)

    def status_porcelain(self) -> str:
        return self._porcelain


def _hash_of(diff: bytes) -> str:
    import hashlib

    return hashlib.sha256(diff).hexdigest()


class InternalPathPolicyTest(unittest.TestCase):
    def test_single_source_tuple(self):
        self.assertEqual(
            gates.INTERNAL_PATHS,
            (".contremaitre", "opencode.json", "dist", "build", "out", ".next", "__pycache__"),
        )

    def test_exact_file_match(self):
        self.assertTrue(gates.is_internal_path("opencode.json"))
        self.assertTrue(gates.is_internal_path(".contremaitre"))

    def test_build_output_names_are_directory_only(self):
        self.assertFalse(gates.is_internal_path("dist"))
        self.assertFalse(gates.is_internal_path("build"))
        self.assertFalse(gates.is_internal_path("out"))
        self.assertFalse(gates.is_internal_path(".next"))
        self.assertFalse(gates.is_internal_path("__pycache__"))

    def test_under_directory_match(self):
        self.assertTrue(gates.is_internal_path(".contremaitre/runs/x/stats.json"))
        self.assertTrue(gates.is_internal_path("dist/"))
        self.assertTrue(gates.is_internal_path("dist/bundle.js"))
        self.assertTrue(gates.is_internal_path("__pycache__/mod.cpython-311.pyc"))

    def test_does_not_fall_into_prefix_trap(self):
        # The bug a naive `startswith("dist")` would carry: a real source path
        # that merely *begins with* a tolerated name must NOT be tolerated.
        self.assertFalse(gates.is_internal_path("distribution/setup.py"))
        self.assertFalse(gates.is_internal_path("outline.md"))
        self.assertFalse(gates.is_internal_path("builder.py"))

    def test_ordinary_source_is_not_internal(self):
        self.assertFalse(gates.is_internal_path("contremaitre/gates.py"))
        self.assertFalse(gates.is_internal_path("README.md"))


class OnlyInternalChangesTest(unittest.TestCase):
    def test_empty_porcelain_is_clean(self):
        self.assertTrue(gates.only_internal_changes(""))

    def test_only_internal_paths_is_clean(self):
        porcelain = "?? .contremaitre/runs/r1/stats.json\n M opencode.json\n?? dist/app.js\n"
        self.assertTrue(gates.only_internal_changes(porcelain))

    def test_root_files_named_like_build_output_are_dirty(self):
        self.assertFalse(gates.only_internal_changes("?? dist\n"))
        self.assertFalse(gates.only_internal_changes("?? build\n"))
        self.assertFalse(gates.only_internal_changes("?? out\n"))
        self.assertFalse(gates.only_internal_changes("?? .next\n"))
        self.assertFalse(gates.only_internal_changes("?? __pycache__\n"))

    def test_build_output_directories_are_clean(self):
        self.assertTrue(gates.only_internal_changes("?? dist/\n"))
        self.assertTrue(gates.only_internal_changes("?? build/\n"))
        self.assertTrue(gates.only_internal_changes("?? out/\n"))
        self.assertTrue(gates.only_internal_changes("?? .next/\n"))
        self.assertTrue(gates.only_internal_changes("?? __pycache__/\n"))

    def test_any_real_change_is_dirty(self):
        porcelain = "?? .contremaitre/runs/r1/x\n M contremaitre/gates.py\n"
        self.assertFalse(gates.only_internal_changes(porcelain))

    def test_quoted_path_with_spaces(self):
        # git quotes paths with unusual chars; the row parser strips quotes.
        porcelain = ' M "contremaitre/some file.py"\n'
        self.assertFalse(gates.only_internal_changes(porcelain))


class EvaluateL0Test(unittest.TestCase):
    def _eval(self, *, diff, changed, porcelain, expected_hash):
        repo = _FakeRepo(diff=diff, changed=changed, porcelain=porcelain)
        return gates.evaluate_l0(
            worktree_git=repo, diff_base="origin/main", expected_hash=expected_hash
        )

    def test_all_green(self):
        diff = b"diff --git a/contremaitre/gates.py ...\n"
        r = self._eval(
            diff=diff,
            changed=["contremaitre/gates.py"],
            porcelain="",
            expected_hash=_hash_of(diff),
        )
        self.assertTrue(r.passed)
        self.assertTrue(r.diff_hash_matched)
        self.assertTrue(r.clean_worktree)
        self.assertTrue(r.diff_scan.passed)
        self.assertEqual(r.recomputed_hash, _hash_of(diff))
        # payload schema is the unchanged gates.hard_gate_payload dict
        self.assertTrue(r.payload["passed"])
        self.assertEqual(
            set(r.payload["checks"]),
            {"diff_scan", "clean_worktree", "diff_hash_matched", "draft_only"},
        )

    def test_hash_drift_fails_l0(self):
        diff = b"some diff"
        r = self._eval(
            diff=diff,
            changed=["contremaitre/gates.py"],
            porcelain="",
            expected_hash="deadbeef",  # does not match recomputed
        )
        self.assertFalse(r.passed)
        self.assertFalse(r.diff_hash_matched)
        self.assertFalse(r.payload["passed"])

    def test_forbidden_path_fails_l0(self):
        diff = b"leak"
        r = self._eval(
            diff=diff,
            changed=["contremaitre/gates.py", ".env"],
            porcelain="",
            expected_hash=_hash_of(diff),
        )
        self.assertFalse(r.passed)
        self.assertFalse(r.diff_scan.passed)
        self.assertIn(".env", r.payload["forbidden_files"])

    def test_dirty_worktree_fails_l0(self):
        diff = b"d"
        r = self._eval(
            diff=diff,
            changed=["contremaitre/gates.py"],
            porcelain=" M contremaitre/orchestrator.py\n",
            expected_hash=_hash_of(diff),
        )
        self.assertFalse(r.passed)
        self.assertFalse(r.clean_worktree)

    def test_internal_only_worktree_is_clean(self):
        diff = b"d"
        r = self._eval(
            diff=diff,
            changed=["contremaitre/gates.py"],
            porcelain="?? .contremaitre/runs/r1/SETTLED_DESIGN.md\n",
            expected_hash=_hash_of(diff),
        )
        self.assertTrue(r.clean_worktree)
        self.assertTrue(r.passed)


if __name__ == "__main__":
    unittest.main()
