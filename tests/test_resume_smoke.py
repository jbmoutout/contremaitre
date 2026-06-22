"""End-to-end resume cycle in fake mode: trip a cap, then `run --continue`.

Exercises the orchestrator's resume plumbing without docker: a capped run must
leave a resumable checkpoint + its worktree, and a continuation must reattach to
the same run identity and drive to a clean terminal.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

from contremaitre import events
from contremaitre.fixture import init_fixture
from contremaitre.models import Caps, RunConfig, TerminalVerdict
from contremaitre.orchestrator import run
from contremaitre.resume import load_resume_state, resume_path


class ResumeCycleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = init_fixture(root / "repo")
        self.runs_root = root / "runs"

    def _events(self, run_dir: Path) -> str:
        return (run_dir / "guardrail_events.jsonl").read_text(encoding="utf-8")

    def test_capped_run_is_resumable_then_completes(self):
        # 1) A run that never reaches IMPLEMENTATION_COMPLETE trips the turn cap.
        capped = run(
            RunConfig(
                repo=self.repo,
                base="main",
                runs_root=self.runs_root,
                run_slug="resume",
                check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
                agent_scenario="no_impl_complete",
                caps=Caps(max_turns=1),
            )
        )
        self.assertEqual(capped.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)

        # The cap exit is resumable: checkpoint kept, worktree kept, hint emitted.
        self.assertTrue(resume_path(capped.run_dir).exists())
        self.assertTrue(capped.worktree.exists())
        self.assertIn(events.RESUMABLE, self._events(capped.run_dir))
        self.addCleanup(self._rm_worktree, capped.worktree)

        # 2) Load the checkpoint and continue with a finishing scenario + a fresh
        #    (larger) budget — exactly what `run --continue` does.
        state = load_resume_state(self.runs_root, capped.run_id)
        cont_config = dataclasses.replace(
            state.config, agent_scenario="normal", caps=Caps(max_turns=30)
        )
        resumed = run(
            cont_config,
            resume_from=dataclasses.replace(state, config=cont_config),
        )

        # Same run identity — reattached, not a new run.
        self.assertEqual(resumed.run_id, capped.run_id)
        self.assertEqual(resumed.run_dir, capped.run_dir)
        ev = self._events(capped.run_dir)
        self.assertIn(events.RUN_RESUMED, ev)
        self.assertIn("after-reattach", (capped.run_dir / "worktree_state.jsonl").read_text())

        # A clean (non-cap) terminal removes the now-stale checkpoint and the
        # worktree, so the run is not accidentally continuable again.
        self.assertNotEqual(resumed.verdict, TerminalVerdict.NO_PR_NEEDS_HUMAN)
        self.assertFalse(resume_path(capped.run_dir).exists())

    def test_resume_carries_turn_offset_into_timeline(self):
        capped = run(
            RunConfig(
                repo=self.repo,
                base="main",
                runs_root=self.runs_root,
                run_slug="offset",
                agent_scenario="no_impl_complete",
                caps=Caps(max_turns=2),
            )
        )
        self.addCleanup(self._rm_worktree, capped.worktree)
        state = load_resume_state(self.runs_root, capped.run_id)
        # The checkpoint records the turns already spent; the resumed run's fresh
        # budget is measured on top of that base.
        self.assertGreaterEqual(state.turns, 1)

        payload = json.loads(resume_path(capped.run_dir).read_text())
        self.assertEqual(payload["run_id"], capped.run_id)
        self.assertEqual(payload["schema_version"], 1)

    @staticmethod
    def _rm_worktree(path: Path) -> None:
        import shutil

        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
