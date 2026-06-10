"""Regression tests for the TUI launch path (`_tui_run_cmd` → subprocess).

The TUI runs the interactive prep (clone, Zen picker, preflight presence
check, Y/n confirm, image build) on the HOST, then spawns `contremaitre run`
as a subprocess that re-enters `_run_cmd`. Two failure modes — both surfacing
as "orchestrator did not create a run dir within 30s" — are guarded here:

  1. **Build-before-spawn** (commit d1c66e7): if the ~3-min image build runs
     inside the subprocess instead of on the host, the run dir is never
     created before the TUI's discover timeout fires.
  2. **Confirm-skip** (stdin=DEVNULL): the subprocess inherits no TTY, so its
     own `_run_cmd` confirm gate (`sys.stdin.isatty()`) must evaluate False —
     otherwise it blocks on `input()` forever and the run dir never appears.

Neither path had a test before; both shipped as live bugs.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import contremaitre.cli as cli_mod


_BASE_ARGS = [
    "--fork",
    "git@github.com:o/r.git",
    "--base",
    "main",
    "--agent",
    "opencode",
    "--agent-model",
    "opencode/x",
    "--sim-model",
    "opencode/x",
]


def _tui_ns(run_args):
    return argparse.Namespace(run_args=list(run_args), refresh_hz=4, discover_timeout=10)


class TuiRunOrderTest(unittest.TestCase):
    """The image must be built BEFORE the TUI subprocess is spawned."""

    def _run(self, run_args):
        calls: list[str] = []

        def rec_image(**_kw):
            calls.append("image")
            return 0

        def rec_spawn(**kw):
            calls.append("spawn")
            self._spawn_kwargs = kw
            return 0

        with (
            patch.object(cli_mod, "_ensure_local_clone"),
            patch.object(cli_mod, "_preflight_presence_check", return_value=0),
            patch.object(cli_mod, "_recap_and_confirm", return_value=True),
            patch.object(cli_mod, "_maybe_provision_cli_egress"),
            patch.object(cli_mod, "_ensure_image_for", side_effect=rec_image),
            patch("contremaitre.tui.spawn_and_attach", side_effect=rec_spawn),
        ):
            rc = cli_mod._tui_run_cmd(_tui_ns(run_args))
        self.assertEqual(rc, 0)
        return calls

    def test_image_built_before_spawn(self):
        # Regression for d1c66e7: building inside the spawned subprocess made
        # the run dir appear only after the build, past the 30s discover
        # timeout. The host must build first.
        self.assertEqual(self._run(_BASE_ARGS), ["image", "spawn"])

    def test_image_build_failure_aborts_before_spawn(self):
        with (
            patch.object(cli_mod, "_ensure_local_clone"),
            patch.object(cli_mod, "_preflight_presence_check", return_value=0),
            patch.object(cli_mod, "_recap_and_confirm", return_value=True),
            patch.object(cli_mod, "_maybe_provision_cli_egress"),
            patch.object(cli_mod, "_ensure_image_for", return_value=1),
            patch("contremaitre.tui.spawn_and_attach") as spawn,
        ):
            rc = cli_mod._tui_run_cmd(_tui_ns(_BASE_ARGS))
        self.assertEqual(rc, 1)
        spawn.assert_not_called()


class TuiRunForwardingTest(unittest.TestCase):
    """The host's resolved choices must reach the subprocess via run_cmd."""

    def _spawn_cmd(self, run_args):
        captured = {}

        def cap_spawn(**kw):
            captured["run_cmd"] = kw["run_cmd"]
            return 0

        with (
            patch.object(cli_mod, "_ensure_local_clone"),
            patch.object(cli_mod, "_preflight_presence_check", return_value=0),
            patch.object(cli_mod, "_recap_and_confirm", return_value=True),
            patch.object(cli_mod, "_maybe_provision_cli_egress"),
            patch.object(cli_mod, "_ensure_image_for", return_value=0),
            patch("contremaitre.tui.spawn_and_attach", side_effect=cap_spawn),
        ):
            rc = cli_mod._tui_run_cmd(_tui_ns(run_args))
        self.assertEqual(rc, 0)
        return captured["run_cmd"]

    def test_forwards_agent_and_models(self):
        cmd = self._spawn_cmd(_BASE_ARGS)
        self.assertEqual(cmd[cmd.index("--agent") + 1], "opencode")
        self.assertEqual(cmd[cmd.index("--agent-model") + 1], "opencode/x")
        self.assertEqual(cmd[cmd.index("--sim-model") + 1], "opencode/x")

    def test_forwards_claude_agent(self):
        cmd = self._spawn_cmd(
            ["--fork", "git@github.com:o/r.git", "--base", "main", "--agent", "claude"]
        )
        self.assertEqual(cmd[cmd.index("--agent") + 1], "claude")

    def test_repo_cache_appended(self):
        cmd = self._spawn_cmd(_BASE_ARGS)
        self.assertIn("--repo-cache", cmd)


class TuiPresenceCheckArgsTest(unittest.TestCase):
    """The Namespace handed to the presence check must carry the model fields.

    Regression: `confirm_args` once omitted `agent_model`/`sim_model`/
    `openrouter_env_var`, so the opencode paid-model `OPENROUTER_API_KEY` check
    silently no-op'd on the TUI path — a keyless paid run sailed past pre-flight
    and only failed inside the container ("run dir not created within 30s").
    """

    def _captured_ns(self, run_args):
        seen = {}

        def cap(ns):
            seen["ns"] = ns
            return 0

        with (
            patch.object(cli_mod, "_ensure_local_clone"),
            patch.object(cli_mod, "_preflight_presence_check", side_effect=cap),
            patch.object(cli_mod, "_recap_and_confirm", return_value=True),
            patch.object(cli_mod, "_maybe_provision_cli_egress"),
            patch.object(cli_mod, "_ensure_image_for", return_value=0),
            patch("contremaitre.tui.spawn_and_attach", return_value=0),
        ):
            cli_mod._tui_run_cmd(_tui_ns(run_args))
        return seen["ns"]

    def test_presence_check_receives_models_and_key_var(self):
        ns = self._captured_ns(
            [
                "--fork",
                "git@github.com:o/r.git",
                "--base",
                "main",
                "--agent",
                "opencode",
                "--agent-model",
                "openrouter/qwen/qwen3.7-max",
                "--sim-model",
                "opencode/x",
            ]
        )
        self.assertEqual(ns.agent_model, "openrouter/qwen/qwen3.7-max")
        self.assertEqual(ns.sim_model, "opencode/x")
        self.assertEqual(ns.openrouter_env_var, "OPENROUTER_API_KEY")


class RunCmdConfirmGateTest(unittest.TestCase):
    """`_run_cmd` must NOT prompt when there is no controlling TTY.

    The TUI spawns the subprocess with stdin=DEVNULL precisely so this gate
    evaluates False — otherwise the subprocess blocks on input() and the run
    dir is never created.
    """

    def _run_with_stdin_tty(self, *, isatty: bool):
        ns = argparse.Namespace(
            upstream=None,
            fork="git@github.com:o/r.git",
            repo_cache=None,
            base="main",
            agent="opencode",
            sim=None,
            agent_model="opencode/x",
            sim_model="opencode/x",
        )
        result = MagicMock()
        result.verdict.value = "READY_FOR_DRAFT_PR"
        result.reason = "ok"
        result.run_dir = "/tmp/x"
        result.pr_created = True

        fake_stdin = MagicMock()
        fake_stdin.isatty.return_value = isatty
        with (
            patch.object(cli_mod, "_ensure_local_clone"),
            patch.object(cli_mod, "_preflight_presence_check", return_value=0),
            patch.object(cli_mod, "_maybe_provision_cli_egress"),
            patch.object(cli_mod, "_config_from_args", return_value=MagicMock()),
            patch.object(cli_mod, "_ensure_default_image_built", return_value=0),
            patch.object(cli_mod, "run", return_value=result),
            patch.object(cli_mod, "_recap_and_confirm", return_value=True) as confirm,
            patch("sys.stdin", fake_stdin),
        ):
            rc = cli_mod._run_cmd(ns)
        return rc, confirm

    def test_no_confirm_when_not_a_tty(self):
        # This is the subprocess case (stdin=DEVNULL). Must not prompt.
        rc, confirm = self._run_with_stdin_tty(isatty=False)
        self.assertEqual(rc, 0)
        confirm.assert_not_called()

    def test_confirms_when_interactive_tty(self):
        # Direct `contremaitre run` on a terminal still prompts.
        _rc, confirm = self._run_with_stdin_tty(isatty=True)
        confirm.assert_called_once()


class SpawnStdinTest(unittest.TestCase):
    """`spawn_and_attach` must launch the subprocess with a null stdin."""

    def test_subprocess_stdin_is_devnull(self):
        from contremaitre import tui as tui_mod

        with tempfile.TemporaryDirectory() as td:
            runs_root = Path(td)
            run_dir = runs_root / "20260101-000000-run"
            captured = {}
            fake_proc = MagicMock()
            fake_proc.poll.return_value = None

            def fake_popen(_cmd, **kwargs):
                captured.update(kwargs)
                run_dir.mkdir()  # appears "new" only after spawn
                return fake_proc

            fake_app = MagicMock()
            fake_app.run.return_value = 0
            with (
                patch.object(tui_mod, "_require_textual"),
                patch("subprocess.Popen", side_effect=fake_popen),
                # ContremaitreTUI only exists when the optional `textual` extra
                # is installed; create=True lets the patch stand in regardless.
                patch.object(tui_mod, "ContremaitreTUI", create=True, return_value=fake_app),
                patch.object(tui_mod, "_print_final_urls"),
            ):
                rc = tui_mod.spawn_and_attach(
                    runs_root=runs_root,
                    run_slug="run",
                    run_cmd=["echo"],
                    agent_model=MagicMock(),
                    sim_model=MagicMock(),
                    cli_reviewer="none",
                    docker_image="img",
                    target_url="u",
                    base="main",
                    refresh_hz=4,
                    discover_timeout_s=5,
                )
            self.assertEqual(rc, 0)
            self.assertEqual(captured.get("stdin"), subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
