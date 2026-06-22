"""Round-trip + guard tests for the resume checkpoint serializer."""

import tempfile
import unittest
from pathlib import Path

from contremaitre import resume
from contremaitre.models import (
    ActorMode,
    Caps,
    DepsVolume,
    PublishMode,
    RunConfig,
)
from contremaitre.resume import ResumeError, ResumeState


def _rich_config(runs_root: Path) -> RunConfig:
    """A config exercising every type JSON can't round-trip on its own:
    Paths, both str-enums, the nested Caps + DepsVolume (tuple-of-tuples),
    and a populated check_cmds tuple."""
    return RunConfig(
        repo=Path("/some/repo"),
        base="main",
        runs_root=runs_root,
        run_slug="demo",
        actor_mode=ActorMode.CLI,
        sim_actor_mode=ActorMode.OPENCODE,
        cli_tool="claude",
        sim_cli_tool="codex",
        publish_mode=PublishMode.GH,
        check_cmds=("pytest -q", "ruff check"),
        opencode_config=Path("/tmp/oc.json"),
        deps_volume=DepsVolume(
            name="vol-abc",
            mount_path=".venv",
            runtime_env=(("VIRTUAL_ENV", "/app/.venv"), ("PATH", "/app/.venv/bin")),
        ),
        caps=Caps(max_turns=12, max_wall_minutes=240, max_cost_usd=7.5),
    )


class ConfigRoundTripTest(unittest.TestCase):
    def test_config_survives_jsonable_round_trip(self):
        cfg = _rich_config(Path("/runs"))
        restored = resume.config_from_jsonable(resume.config_to_jsonable(cfg))
        self.assertEqual(cfg, restored)

    def test_minimal_config_round_trips(self):
        cfg = RunConfig(repo=Path("/r"), base="main", runs_root=Path("/runs"), run_slug="x")
        restored = resume.config_from_jsonable(resume.config_to_jsonable(cfg))
        self.assertEqual(cfg, restored)

    def test_jsonable_is_pure_json(self):
        import json

        cfg = _rich_config(Path("/runs"))
        # Must serialize with no custom encoder (Path/enum already coerced).
        json.dumps(resume.config_to_jsonable(cfg))


class ResumeStateFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs_root = Path(tmp.name)
        self.run_id = "20260101-000000-demo"
        self.run_dir = self.runs_root / self.run_id
        self.run_dir.mkdir(parents=True)

    def _state(self) -> ResumeState:
        return ResumeState(
            config=_rich_config(self.runs_root),
            run_id=self.run_id,
            base_sha="deadbeef",
            branch="refactor/20260101-000000-demo",
            review_round=2,
            required_changes=["fix the thing", "and the other thing"],
            agent_session="sess-agent-1",
            sim_session="sess-sim-1",
            turns=17,
        )

    def test_write_then_load_round_trips(self):
        original = self._state()
        resume.write_resume_state(self.run_dir, original)
        self.assertTrue(resume.has_resume_state(self.runs_root, self.run_id))
        loaded = resume.load_resume_state(self.runs_root, self.run_id)
        self.assertEqual(loaded, original)

    def test_load_missing_raises(self):
        with self.assertRaises(ResumeError):
            resume.load_resume_state(self.runs_root, "nope-no-such-run")

    def test_load_rejects_schema_mismatch(self):
        import json

        resume.write_resume_state(self.run_dir, self._state())
        path = resume.resume_path(self.run_dir)
        payload = json.loads(path.read_text())
        payload["schema_version"] = 999
        path.write_text(json.dumps(payload))
        with self.assertRaises(ResumeError):
            resume.load_resume_state(self.runs_root, self.run_id)


class ContinueCmdGuardTest(unittest.TestCase):
    """The `run --continue` guards that return before any docker work."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs_root = Path(tmp.name)

    def _args(self, run_id: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            continue_run=run_id,
            runs_root=self.runs_root,
            max_turns=30,
            max_wall_minutes=180,
            max_cost_usd=30.0,
            no_progress_turns=5,
            malformed_verdict_retries=2,
            max_review_rounds=3,
        )

    def _write_checkpoint(self, run_id: str, config: RunConfig):
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True)
        resume.write_resume_state(
            run_dir,
            ResumeState(
                config=config,
                run_id=run_id,
                base_sha="abc",
                branch="refactor/x",
                review_round=1,
                required_changes=[],
                agent_session="s",
                sim_session=None,
                turns=3,
            ),
        )

    def test_missing_checkpoint_returns_error(self):
        from contremaitre.cli import _continue_cmd

        self.assertEqual(_continue_cmd(self._args("no-such-run")), 1)

    def test_opencode_run_is_refused(self):
        from contremaitre.cli import _continue_cmd

        run_id = "20260101-000000-oc"
        self._write_checkpoint(
            run_id,
            RunConfig(
                repo=Path("/r"),
                base="main",
                runs_root=self.runs_root,
                run_slug="oc",
                actor_mode=ActorMode.OPENCODE,
            ),
        )
        self.assertEqual(_continue_cmd(self._args(run_id)), 1)


if __name__ == "__main__":
    unittest.main()
