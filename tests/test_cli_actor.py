from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from contremaitre.actors import CompositeActorRunner, make_actor_runner
from contremaitre.cli_actor import (
    CliActorRunner,
    _access_token_exp,
    _codex_model_arg,
    _parse_codex_events,
)
from contremaitre.costs import sum_token_usage
from contremaitre.models import ActorMode, RunConfig
from contremaitre.paths import build_run_paths
from contremaitre.preflight import _check_codex_auth


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _fake_jwt(exp: int) -> str:
    """A 3-part JWT whose payload carries `exp` (signature is irrelevant — we
    never verify it, only decode the expiry)."""
    return f"{_b64url({'alg': 'none'})}.{_b64url({'exp': exp})}.sig"


def _make_runner(root: Path, **config_overrides):
    """Build a CliActorRunner against a fixture CODEX_HOME (no real ~/.codex)."""
    paths = build_run_paths(root / "runs", f"20260605-{root.name}")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    config = RunConfig(
        repo=root,
        base="main",
        runs_root=root / "runs",
        run_slug="test",
        actor_mode=ActorMode.CLI,
        docker_image="test-image",
        **config_overrides,
    )
    runner = CliActorRunner(config=config, paths=paths, tool="codex")
    # Point the auth source at a fixture home with a far-future valid token.
    src = root / "src-codex"
    src.mkdir(parents=True, exist_ok=True)
    (src / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _fake_jwt(int(time.time()) + 9 * 24 * 3600),
                    "refresh_token": "REAL-SECRET-REFRESH-TOKEN",
                    "id_token": "id",
                    "account_id": "acct",
                },
            }
        )
    )
    runner._src_codex_home = src
    return runner, paths


class AccessTokenExpTest(unittest.TestCase):
    def test_decodes_exp_from_jwt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.json"
            p.write_text(json.dumps({"tokens": {"access_token": _fake_jwt(1893456000)}}))
            self.assertEqual(_access_token_exp(p), 1893456000)

    def test_returns_none_for_opaque_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.json"
            p.write_text(json.dumps({"tokens": {"access_token": "opaque-not-a-jwt"}}))
            self.assertIsNone(_access_token_exp(p))


class PrepareCodexHomeTest(unittest.TestCase):
    def test_neuters_refresh_token_and_keeps_real_secret_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp))
            home = runner.prepare_codex_home(runner.agent_home)
            written = (home / "auth.json").read_text()
            self.assertEqual(json.loads(written)["tokens"]["refresh_token"], "x")
            # The real standing credential must never reach the mounted home.
            self.assertNotIn("REAL-SECRET-REFRESH-TOKEN", written)
            # Access token (short-lived, OK to mount) is preserved.
            self.assertIn("access_token", json.loads(written)["tokens"])

    def test_reseed_preserves_existing_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp))
            # Simulate a rollout written by an earlier turn.
            sess = runner.agent_home / "sessions" / "2026"
            sess.mkdir(parents=True, exist_ok=True)
            (sess / "rollout.jsonl").write_text("{}")
            runner.prepare_codex_home(runner.agent_home)  # re-seed
            self.assertTrue((sess / "rollout.jsonl").exists())

    def _write_near_expiry(self, runner):
        (runner._src_codex_home / "auth.json").write_text(
            json.dumps(
                {"tokens": {"access_token": _fake_jwt(int(time.time()) + 60), "refresh_token": "r"}}
            )
        )

    def test_near_expiry_refuses_when_host_refresh_does_not_renew(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp))
            self._write_near_expiry(runner)
            runner._host_refresh_token = lambda: None  # stub: no renewal
            with self.assertRaises(Exception):
                runner.prepare_codex_home(runner.agent_home)

    def test_near_expiry_recovers_when_host_refresh_renews(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp))
            self._write_near_expiry(runner)

            def _renew():
                (runner._src_codex_home / "auth.json").write_text(
                    json.dumps(
                        {"tokens": {"access_token": _fake_jwt(int(time.time()) + 9 * 24 * 3600),
                                    "refresh_token": "REAL-SECRET-REFRESH-TOKEN"}}
                    )
                )

            runner._host_refresh_token = _renew  # stub: refreshes the host token
            home = runner.prepare_codex_home(runner.agent_home)  # no raise
            self.assertEqual(json.loads((home / "auth.json").read_text())["tokens"]["refresh_token"], "x")


class EgressLockTest(unittest.TestCase):
    def test_refuses_without_both_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp), docker_network="cmtr-int")  # proxy missing
            with self.assertRaises(Exception):
                runner._assert_egress_locked()

    def test_passes_with_both_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://egress-proxy:3128"
            )
            runner._assert_egress_locked()  # no raise

    def test_allow_open_egress_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp), allow_open_egress=True)
            runner._assert_egress_locked()  # no raise


class BuildCommandTest(unittest.TestCase):
    def test_first_turn_argv_mounts_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, paths = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://egress-proxy:3128"
            )
            cmd = runner._build_codex_command(
                prompt="do it", codex_home=runner.agent_home, session_id=None,
                model="m", mount_mode="rw", role="agent", extra_mounts=(),
            )
            joined = " ".join(cmd)
            self.assertEqual(cmd[:3], ["docker", "run", "-d"])
            self.assertIn("contremaitre.role=agent", cmd)
            self.assertIn("--json", cmd)
            self.assertIn("danger-full-access", cmd)
            self.assertIn(f"{runner.agent_home}:/root/.codex:rw", joined)
            self.assertIn(f"{runner.worktree}:/app:rw", joined)
            self.assertIn("--network", cmd)
            self.assertIn("cmtr-int", cmd)
            # Proxy forwarded by NAME only — the URL value must not be inlined.
            self.assertIn("HTTPS_PROXY", cmd)
            self.assertNotIn("http://egress-proxy:3128", joined)

    def test_resume_turn_places_opts_before_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )
            cmd = runner._build_codex_command(
                prompt="again", codex_home=runner.agent_home, session_id="SID-123",
                model="m", mount_mode="rw", role="agent", extra_mounts=(),
            )
            # exec-level opts must precede the `resume` subcommand (clap rejects
            # them otherwise), and the session id must follow `resume`.
            self.assertLess(cmd.index("-s"), cmd.index("resume"))
            self.assertEqual(cmd[cmd.index("resume") + 1], "SID-123")
            # -m model is omitted on resume (the session carries it).
            self.assertNotIn("-m", cmd[cmd.index("resume"):])

    def test_first_turn_omits_m_for_openrouter_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )
            cmd = runner._build_codex_command(
                prompt="do it", codex_home=runner.agent_home,
                session_id=None, model="openrouter/deepseek/deepseek-v4-flash",
                mount_mode="rw", role="agent", extra_mounts=(),
            )
            # codex rejects OpenRouter models on a ChatGPT account → no -m,
            # codex falls back to its subscription default.
            self.assertNotIn("-m", cmd)

    def test_review_mounts_worktree_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )
            cmd = runner._build_codex_command(
                prompt="review", codex_home=runner.review_home, session_id=None,
                model="m", mount_mode="ro", role="review",
                extra_mounts=((Path("/tmp/rev"), "/review", "ro"),),
            )
            joined = " ".join(cmd)
            self.assertIn(f"{runner.worktree}:/app:ro", joined)
            self.assertIn("/tmp/rev:/review:ro", joined)


class CodexModelArgTest(unittest.TestCase):
    def test_omits_openrouter_and_empty(self):
        self.assertEqual(_codex_model_arg("openrouter/deepseek/deepseek-v4-flash"), [])
        self.assertEqual(_codex_model_arg(""), [])

    def test_passes_codex_native_model(self):
        self.assertEqual(_codex_model_arg("gpt-5.5"), ["-m", "gpt-5.5"])


def _cli_config(root: Path, **over) -> RunConfig:
    return RunConfig(
        repo=root, base="main", runs_root=root / "runs", run_slug="t",
        actor_mode=ActorMode.CLI, cli_tool="codex", **over,
    )


class CodexAuthCheckTest(unittest.TestCase):
    def test_pass_when_token_valid(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            home = Path(tmp) / ".codex"
            home.mkdir(parents=True)
            (home / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt(int(time.time()) + 9 * 24 * 3600)}})
            )
            self.assertEqual(_check_codex_auth(_cli_config(Path(tmp))).status, "PASS")

    def test_fail_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            self.assertEqual(_check_codex_auth(_cli_config(Path(tmp))).status, "FAIL")

    def test_fail_when_near_expiry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            home = Path(tmp) / ".codex"
            home.mkdir(parents=True)
            (home / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt(int(time.time()) + 60)}})
            )
            self.assertEqual(_check_codex_auth(_cli_config(Path(tmp))).status, "FAIL")


class TokenUsageRollupTest(unittest.TestCase):
    def test_rolls_up_codex_turn_completed_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            p.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "turn.completed", "usage": {
                            "input_tokens": 100, "output_tokens": 10,
                            "cached_input_tokens": 80, "reasoning_output_tokens": 5}}),
                        json.dumps({"type": "turn.completed", "usage": {
                            "input_tokens": 40, "output_tokens": 4,
                            "cached_input_tokens": 30, "reasoning_output_tokens": 1}}),
                    ]
                )
            )
            self.assertEqual(
                sum_token_usage(p),
                {"input": 140, "output": 14, "reasoning": 6, "cache_read": 110},
            )

    def test_still_rolls_up_opencode_step_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            p.write_text(
                json.dumps(
                    {"type": "step_finish",
                     "part": {"tokens": {"input": 7, "output": 2, "cache": {"read": 3}}}}
                )
            )
            self.assertEqual(
                sum_token_usage(p),
                {"input": 7, "output": 2, "reasoning": 0, "cache_read": 3},
            )


class _StubRunner:
    def __init__(self):
        self.calls: list[str] = []

    def agent_turn(self, message):
        self.calls.append("agent")

    def sim_turn(self, message):
        self.calls.append("sim")

    def sim_review(self, **kwargs):
        self.calls.append("review")


class RuntimeSelectorTest(unittest.TestCase):
    def _pick(self, actor, sim_actor, inputs):
        from contremaitre.cli import _pick_runtimes_interactive

        ns = argparse.Namespace(actor=actor, sim_actor=sim_actor)
        with patch("builtins.input", side_effect=inputs):
            _pick_runtimes_interactive(ns)
        return ns

    def test_picks_mixed_codex_agent_opencode_sim(self):
        ns = self._pick("opencode", None, ["2", "1"])  # agent=codex, SIM=opencode
        self.assertEqual(ns.actor, "cli")
        self.assertEqual(ns.sim_actor, "opencode")

    def test_same_runtime_records_no_sim_override(self):
        ns = self._pick("opencode", None, ["", ""])  # keep opencode for both
        self.assertEqual(ns.actor, "opencode")
        self.assertIsNone(ns.sim_actor)

    def test_fake_default_skips_picker(self):
        ns = self._pick("fake", None, [])  # no prompts consumed
        self.assertEqual(ns.actor, "fake")


class ForwardedFlagHelpersTest(unittest.TestCase):
    """`_set_flag_value` / `_remove_flag` fold an interactive choice back into
    the passthrough flags handed to the `contremaitre run` subprocess."""

    def test_set_flag_appends_when_absent(self):
        from contremaitre.cli import _set_flag_value

        args = ["--base", "main"]
        _set_flag_value(args, "--actor", "cli")
        self.assertEqual(args, ["--base", "main", "--actor", "cli"])

    def test_set_flag_replaces_space_form(self):
        from contremaitre.cli import _set_flag_value

        args = ["--actor", "opencode", "--base", "main"]
        _set_flag_value(args, "--actor", "cli")
        self.assertEqual(args, ["--base", "main", "--actor", "cli"])

    def test_set_flag_replaces_equals_form(self):
        from contremaitre.cli import _set_flag_value

        args = ["--actor=opencode", "--base", "main"]
        _set_flag_value(args, "--actor", "cli")
        self.assertEqual(args, ["--base", "main", "--actor", "cli"])

    def test_remove_flag_drops_all_forms(self):
        from contremaitre.cli import _remove_flag

        args = ["--sim-actor", "opencode", "--x", "1", "--sim-actor=cli"]
        _remove_flag(args, "--sim-actor")
        self.assertEqual(args, ["--x", "1"])


class TuiRunForwardsRuntimeTest(unittest.TestCase):
    """`tui run` must forward the resolved per-role runtime to the subprocess.

    The TUI builds a throwaway `confirm_args` namespace and the real run
    happens in a `contremaitre run` subprocess, so the picker's choice only
    takes effect if it is written back into the forwarded flags.
    """

    def _spawn_cmd(self, run_args, launch_side_effect=None):
        import contremaitre.cli as cli_mod

        captured = {}

        def fake_spawn(**kwargs):
            captured["run_cmd"] = kwargs["run_cmd"]
            return 0

        saved = MagicMock(
            agent_model=None,
            sim_model=None,
            extra_reviewer_model=None,
            cli_reviewer=None,
            extra_reviewer_skip=False,
        )
        ns = argparse.Namespace(run_args=run_args, refresh_hz=4, discover_timeout=10)
        with (
            patch.object(cli_mod, "_ensure_local_clone"),
            patch.object(cli_mod._defaults, "load", return_value=saved),
            patch.object(
                cli_mod,
                "_launch_screen",
                side_effect=launch_side_effect or (lambda **k: True),
            ),
            patch("contremaitre.tui.spawn_and_attach", side_effect=fake_spawn),
        ):
            rc = cli_mod._tui_run_cmd(ns)
        self.assertEqual(rc, 0)
        return captured["run_cmd"]

    def test_bare_tui_run_forwards_opencode_default(self):
        # No --actor: the TUI defaults the agent runtime to a real actor so the
        # subprocess runs opencode (and the image auto-builds), not `fake`.
        cmd = self._spawn_cmd(["--fork", "git@github.com:o/r.git", "--base", "main"])
        self.assertIn("--actor", cmd)
        self.assertEqual(cmd[cmd.index("--actor") + 1], "opencode")
        self.assertNotIn("--sim-actor", cmd)

    def test_picker_change_propagates_mixed_runtimes(self):
        def picked(**kwargs):
            args = kwargs["args"]
            args.actor = "cli"  # codex agent
            args.sim_actor = "opencode"  # opencode SIM
            return True

        cmd = self._spawn_cmd(
            ["--fork", "git@github.com:o/r.git", "--base", "main"], picked
        )
        self.assertEqual(cmd[cmd.index("--actor") + 1], "cli")
        self.assertEqual(cmd[cmd.index("--sim-actor") + 1], "opencode")


class CompositeRunnerTest(unittest.TestCase):
    def _runner(self, tmp, **over):
        paths = build_run_paths(Path(tmp) / "runs", f"20260605-{Path(tmp).name}")
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        cfg = RunConfig(
            repo=Path(tmp), base="main", runs_root=Path(tmp) / "runs", run_slug="t",
            actor_mode=ActorMode.FAKE, **over,
        )
        return make_actor_runner(config=cfg, paths=paths)

    def test_single_runner_when_modes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotIsInstance(self._runner(tmp), CompositeActorRunner)

    def test_composite_when_modes_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._runner(tmp, sim_actor_mode=ActorMode.CLI)  # fake agent + codex SIM
            self.assertIsInstance(r, CompositeActorRunner)

    def test_routes_agent_and_sim_to_distinct_runners(self):
        agent, sim = _StubRunner(), _StubRunner()
        comp = CompositeActorRunner(agent_runner=agent, sim_runner=sim)
        comp.agent_turn("x")
        comp.sim_turn("y")
        comp.sim_review(diff_file=None, settled_file=None, scenario="", attempt=1)
        self.assertEqual(agent.calls, ["agent"])  # agent runner gets only the agent turn
        self.assertEqual(sim.calls, ["sim", "review"])  # sim runner gets sim + review


class ParseEventsTest(unittest.TestCase):
    def test_extracts_text_session_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "SID-9"}),
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {"type": "item.completed",
                             "item": {"type": "agent_message", "text": "FINAL"}}
                        ),
                        json.dumps({"type": "turn.completed", "usage": {"output_tokens": 6}}),
                    ]
                )
            )
            text, sid, usage, error = _parse_codex_events(p)
            self.assertEqual(text, "FINAL")
            self.assertEqual(sid, "SID-9")
            self.assertEqual(usage, {"output_tokens": 6})
            self.assertIsNone(error)

    def test_start_offset_scopes_to_one_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text(
                json.dumps({"type": "item.completed",
                            "item": {"type": "agent_message", "text": "FIRST"}}) + "\n"
            )
            off = p.stat().st_size  # boundary between turn 1 and turn 2
            with p.open("a") as f:
                f.write(json.dumps({"type": "thread.started", "thread_id": "S2"}) + "\n")
                f.write(json.dumps({"type": "item.completed",
                                    "item": {"type": "agent_message", "text": "SECOND"}}) + "\n")
            text, sid, _u, _e = _parse_codex_events(p, start_offset=off)
            self.assertEqual(text, "SECOND")  # not "FIRST" from the prior turn
            self.assertEqual(sid, "S2")

    def test_surfaces_turn_failed_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text(
                json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})
            )
            text, sid, usage, error = _parse_codex_events(p)
            self.assertEqual(text, "")
            self.assertIn("401", error)


if __name__ == "__main__":
    unittest.main()
