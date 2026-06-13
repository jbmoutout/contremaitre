"""L1 check runner routing: which runtimes run the gate in a sidecar vs host.

The check gate must execute in the same environment the agent did — a
hermetic sidecar with the worktree + deps volume — for every real runtime
(opencode AND cli). Only FAKE mode (no docker) runs on the host. Running a
CLI check on the host would miss the per-run deps volume and the agent's
locked egress; this pins the routing so a refactor can't silently regress
a codex run's gate back to the host.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from contremaitre.checks import run_checks
from contremaitre.models import ActorMode, RunConfig
from contremaitre.paths import build_run_paths


def _config(root: Path, mode: ActorMode) -> RunConfig:
    return RunConfig(
        repo=root,
        base="main",
        runs_root=root / "runs",
        run_slug="t",
        actor_mode=mode,
        docker_image="img",
        check_cmds=("uv run pytest -q",),
    )


class CheckRoutingTest(unittest.TestCase):
    def _route(self, mode: ActorMode) -> str:
        """Run run_checks under `mode` with both backends stubbed; return
        whichever of "sidecar"/"host" was invoked.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = build_run_paths(root / "runs", f"20260613-{root.name}")
            paths.run_dir.mkdir(parents=True)
            ok = MagicMock(returncode=0, stdout="", stderr="")
            with (
                patch("contremaitre.checks._run_sidecar", return_value=ok) as sidecar,
                patch("contremaitre.checks._run_host", return_value=ok) as host,
            ):
                run_checks(config=_config(root, mode), paths=paths)
        if sidecar.called and not host.called:
            return "sidecar"
        if host.called and not sidecar.called:
            return "host"
        return f"both/neither (sidecar={sidecar.called} host={host.called})"

    def test_opencode_runs_in_sidecar(self):
        self.assertEqual(self._route(ActorMode.OPENCODE), "sidecar")

    def test_cli_runs_in_sidecar(self):
        # The fix: a codex/claude run's gate is now hermetic, not host-side.
        self.assertEqual(self._route(ActorMode.CLI), "sidecar")

    def test_fake_runs_on_host(self):
        # FAKE never touches docker — must stay on the host.
        self.assertEqual(self._route(ActorMode.FAKE), "host")


if __name__ == "__main__":
    unittest.main()
