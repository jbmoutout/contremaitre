from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..jsonlog import append_text_event, append_transcript
from ..models import RunPaths
from .base import ActorError, ActorOutput


class FakeActorRunner:
    def __init__(self, *, paths: RunPaths, agent_scenario: str, sim_scenario: str):
        self.paths = paths
        self.agent_scenario = agent_scenario
        self.sim_scenario = sim_scenario

    def agent_turn(self, message: str) -> ActorOutput:
        return self._fake(
            ["agent", "--worktree", str(self.paths.worktree), "--scenario", self.agent_scenario],
            role="agent",
            phase="WORK",
            raw_export=self.paths.raw_export,
        )

    def sim_turn(self, message: str) -> ActorOutput:
        return self._fake(
            ["sim-turn"],
            role="sim",
            phase="WORK",
            raw_export=self.paths.sim_raw_export,
        )

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
        reviewer_id: str = "sim",
        model_override: str | None = None,
    ) -> ActorOutput:
        export = (
            self.paths.extra_reviewer_raw_export
            if reviewer_id == "extra"
            else self.paths.sim_raw_export
        )
        return self._fake(
            [
                "sim-review",
                "--diff-file",
                str(diff_file),
                "--settled-file",
                str(settled_file),
                "--scenario",
                scenario,
                "--attempt",
                str(attempt),
            ],
            role="sim",
            phase="REVIEW",
            raw_export=export,
        )

    def _fake(self, args: list[str], *, role: str, phase: str, raw_export: Path) -> ActorOutput:
        package_root = Path(__file__).resolve().parents[2]
        env = {
            **os.environ,
            "PYTHONPATH": f"{package_root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        }
        cmd = [sys.executable, "-m", "contremaitre.fake_actor", *args]
        proc = subprocess.run(
            cmd,
            cwd=self.paths.worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise ActorError(
                f"fake actor failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}"
            )
        text = proc.stdout.strip()
        append_text_event(raw_export, role=role, phase=phase, text=text)
        append_transcript(self.paths.transcript, speaker=role, phase=phase, text=text)
        return ActorOutput(text=text, stderr=proc.stderr, returncode=proc.returncode)
