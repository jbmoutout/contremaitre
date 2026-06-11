from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre.actors import CompositeActorRunner, make_actor_runner
from contremaitre.cli_actor import (
    _CLAUDE_OAUTH_ENV,
    _CLAUDE_STATUSLINE_METER_SCRIPT_BODY,
    _CLAUDE_STATUSLINE_SCRIPT_BODY,
    CliActorRunner,
    _access_token_exp,
    _claude_effort_arg,
    _claude_meter_model,
    _claude_model_arg,
    _codex_effort_arg,
    _codex_model_arg,
    _parse_claude_events,
    _parse_claude_model,
    _parse_codex_events,
    _stamp_event_slice,
)
from contremaitre.costs import sum_costs_in_events, sum_token_usage_in_events
from contremaitre.jsonlog import read_jsonl
from contremaitre.models import ActorMode, RunConfig
from contremaitre.paths import build_run_paths
from contremaitre.preflight import (
    _active_cli_tools,
    _check_claude_auth,
    _check_cli_auth,
    _check_codex_auth,
)


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
    runner.driver.src_codex_home = src
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
            home = runner.driver.prepare_home(runner.agent_home)
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
            runner.driver.prepare_home(runner.agent_home)  # re-seed
            self.assertTrue((sess / "rollout.jsonl").exists())

    def _write_near_expiry(self, runner):
        (runner.driver.src_codex_home / "auth.json").write_text(
            json.dumps(
                {"tokens": {"access_token": _fake_jwt(int(time.time()) + 60), "refresh_token": "r"}}
            )
        )

    def test_near_expiry_refuses_when_host_refresh_does_not_renew(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp))
            self._write_near_expiry(runner)
            runner.driver._host_refresh_token = lambda: None  # stub: no renewal
            with self.assertRaises(Exception):
                runner.driver.prepare_home(runner.agent_home)

    def test_near_expiry_recovers_when_host_refresh_renews(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp))
            self._write_near_expiry(runner)

            def _renew():
                (runner.driver.src_codex_home / "auth.json").write_text(
                    json.dumps(
                        {
                            "tokens": {
                                "access_token": _fake_jwt(int(time.time()) + 9 * 24 * 3600),
                                "refresh_token": "REAL-SECRET-REFRESH-TOKEN",
                            }
                        }
                    )
                )

            runner.driver._host_refresh_token = _renew  # stub: refreshes the host token
            home = runner.driver.prepare_home(runner.agent_home)  # no raise
            self.assertEqual(
                json.loads((home / "auth.json").read_text())["tokens"]["refresh_token"], "x"
            )


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
        # The lock is the secure default, not mandatory: --allow-open-egress is
        # the explicit escape hatch, so the runner launches without both layers.
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp), allow_open_egress=True)
            runner._assert_egress_locked()  # no raise

    def test_claude_never_requires_lock(self):
        # claude carries no in-container credential (the host auth-inject proxy
        # holds it), so the lock requirement does not apply — even with no
        # docker_network / https_proxy configured.
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_claude_runner(Path(tmp))
            runner._assert_egress_locked()  # no raise

    def test_egress_docker_flags_are_per_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            # claude: OPEN egress + host.docker.internal, ignores any lock network.
            claude, _ = _make_claude_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )
            cflags = claude._egress_docker_flags()
            self.assertIn("--add-host", cflags)
            self.assertIn("host.docker.internal:host-gateway", cflags)
            self.assertNotIn("--network", cflags)
            # codex: locked internal network + forwarded proxy env.
            codex, _ = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )
            xflags = codex._egress_docker_flags()
            self.assertIn("--network", xflags)
            self.assertIn("cmtr-int", xflags)
            self.assertIn("HTTPS_PROXY", xflags)
            self.assertNotIn("--add-host", xflags)


class BuildCommandTest(unittest.TestCase):
    def test_first_turn_argv_mounts_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, paths = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://egress-proxy:3128"
            )
            cmd = runner._build_command(
                prompt="do it",
                home=runner.agent_home,
                session_id=None,
                model="m",
                mount_mode="rw",
                role="agent",
                extra_mounts=(),
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
            cmd = runner._build_command(
                prompt="again",
                home=runner.agent_home,
                session_id="SID-123",
                model="m",
                mount_mode="rw",
                role="agent",
                extra_mounts=(),
            )
            # exec-level opts must precede the `resume` subcommand (clap rejects
            # them otherwise), and the session id must follow `resume`.
            self.assertLess(cmd.index("-s"), cmd.index("resume"))
            self.assertEqual(cmd[cmd.index("resume") + 1], "SID-123")
            # -m model is omitted on resume (the session carries it).
            self.assertNotIn("-m", cmd[cmd.index("resume") :])

    def test_first_turn_falls_back_to_codex_model_for_namespaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(
                Path(tmp),
                docker_network="cmtr-int",
                https_proxy="http://p:3128",
                codex_model="gpt-5.5",
                codex_effort="high",
            )
            cmd = runner._build_command(
                prompt="do it",
                home=runner.agent_home,
                session_id=None,
                model="openrouter/deepseek/deepseek-v4-flash",
                mount_mode="rw",
                role="agent",
                extra_mounts=(),
            )
            # codex rejects opencode/openrouter names on a ChatGPT account → fall
            # back to the codex-native config.codex_model, not the namespaced name.
            self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.5")
            self.assertNotIn("openrouter/deepseek/deepseek-v4-flash", cmd)
            # Reasoning effort is pinned via an exec-level -c override.
            self.assertIn("model_reasoning_effort=high", cmd)

    def test_review_mounts_worktree_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )
            cmd = runner._build_command(
                prompt="review",
                home=runner.review_home,
                session_id=None,
                model="m",
                mount_mode="ro",
                role="review",
                extra_mounts=((Path("/tmp/rev"), "/review", "ro"),),
            )
            joined = " ".join(cmd)
            self.assertIn(f"{runner.worktree}:/app:ro", joined)
            self.assertIn("/tmp/rev:/review:ro", joined)

    def test_docker_env_scrubs_github_credentials(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_secret", "GH_TOKEN": "ghs_secret"}),
        ):
            runner, _ = _make_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )

            env = runner._docker_env()

            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("GH_TOKEN", env)


class CodexModelArgTest(unittest.TestCase):
    def test_omits_namespaced_and_empty(self):
        # Any provider-namespaced model is an opencode/openrouter name codex
        # rejects on a ChatGPT account — omit -m, fall back to the account
        # default. `opencode/...` is the one that slipped past an
        # `openrouter/`-only filter (default agent_model = opencode free model).
        self.assertEqual(_codex_model_arg("openrouter/deepseek/deepseek-v4-flash"), [])
        self.assertEqual(_codex_model_arg("opencode/deepseek-v4-flash-free"), [])
        self.assertEqual(_codex_model_arg(""), [])

    def test_passes_codex_native_model(self):
        self.assertEqual(_codex_model_arg("gpt-5.5"), ["-m", "gpt-5.5"])
        self.assertEqual(_codex_model_arg("gpt-5-codex"), ["-m", "gpt-5-codex"])

    def test_falls_back_to_codex_default_for_namespaced(self):
        # A namespaced/empty per-role model uses the configured codex default…
        self.assertEqual(_codex_model_arg("opencode/x", "gpt-5.5"), ["-m", "gpt-5.5"])
        self.assertEqual(_codex_model_arg("", "gpt-5.5"), ["-m", "gpt-5.5"])
        # …but a codex-native per-role model still wins over the default.
        self.assertEqual(_codex_model_arg("gpt-5-codex", "gpt-5.5"), ["-m", "gpt-5-codex"])

    def test_effort_arg(self):
        self.assertEqual(_codex_effort_arg("high"), ["-c", "model_reasoning_effort=high"])
        self.assertEqual(_codex_effort_arg("xhigh"), ["-c", "model_reasoning_effort=xhigh"])
        self.assertEqual(_codex_effort_arg(""), [])


class CodexModelSpecTest(unittest.TestCase):
    def test_codex_role_records_model_and_effort(self):
        from contremaitre.models import ModelSpec

        spec = ModelSpec.build(
            mode=ActorMode.CLI,
            opencode_model="opencode/deepseek-v4-flash-free",
            codex_model="gpt-5.5",
            codex_effort="high",
        )
        self.assertEqual((spec.runtime, spec.requested, spec.effort), ("codex", "gpt-5.5", "high"))
        self.assertEqual(spec.canonical(), ("gpt-5.5", "codex"))
        self.assertEqual(spec.display(), "codex/gpt-5.5 high")

    def test_opencode_role_records_slug_and_provider(self):
        from contremaitre.models import ModelSpec

        for mode in (ActorMode.OPENCODE, ActorMode.OPENCODE.value, "fake"):
            spec = ModelSpec.build(
                mode=mode,
                opencode_model="opencode/big-pickle",
                codex_model="gpt-5.5",
                codex_effort="high",
            )
            self.assertEqual(spec.requested, "opencode/big-pickle")
            self.assertEqual(spec.provider, "opencode")
            self.assertIsNone(spec.effort)
            self.assertEqual(spec.canonical(), ("big-pickle", spec.runtime))


def _cli_config(root: Path, **over) -> RunConfig:
    over.setdefault("cli_tool", "codex")
    actor_mode = over.pop("actor_mode", ActorMode.CLI)
    return RunConfig(
        repo=root,
        base="main",
        runs_root=root / "runs",
        run_slug="t",
        actor_mode=actor_mode,
        **over,
    )


class CodexAuthCheckTest(unittest.TestCase):
    def test_pass_when_token_valid(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            home = Path(tmp) / ".codex"
            home.mkdir(parents=True)
            (home / "auth.json").write_text(
                json.dumps(
                    {"tokens": {"access_token": _fake_jwt(int(time.time()) + 9 * 24 * 3600)}}
                )
            )
            self.assertEqual(_check_codex_auth(_cli_config(Path(tmp))).status, "PASS")

    def test_fail_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            self.assertEqual(_check_codex_auth(_cli_config(Path(tmp))).status, "FAIL")

    def test_near_expiry_warns_not_fails(self):
        # Near-expiry is RECOVERABLE: the runner host-refreshes in prepare_home
        # before each turn (and hard-fails there if it can't). Failing preflight
        # would abort before that refresh ever runs, so it must WARN, not FAIL.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            home = Path(tmp) / ".codex"
            home.mkdir(parents=True)
            (home / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt(int(time.time()) + 60)}})
            )
            self.assertEqual(_check_codex_auth(_cli_config(Path(tmp))).status, "WARN")


class TokenUsageRollupTest(unittest.TestCase):
    def test_rolls_up_codex_turn_completed_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            p.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 100,
                                    "output_tokens": 10,
                                    "cached_input_tokens": 80,
                                    "reasoning_output_tokens": 5,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 40,
                                    "output_tokens": 4,
                                    "cached_input_tokens": 30,
                                    "reasoning_output_tokens": 1,
                                },
                            }
                        ),
                    ]
                )
            )
            self.assertEqual(
                sum_token_usage_in_events(read_jsonl(p)),
                {"input": 140, "output": 14, "reasoning": 6, "cache_read": 110},
            )

    def test_still_rolls_up_opencode_step_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            p.write_text(
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {"tokens": {"input": 7, "output": 2, "cache": {"read": 3}}},
                    }
                )
            )
            self.assertEqual(
                sum_token_usage_in_events(read_jsonl(p)),
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


class ForwardedFlagHelpersTest(unittest.TestCase):
    """`_set_flag_value` / `_remove_flag` fold an interactive choice back into
    the passthrough flags handed to the `contremaitre run` subprocess."""

    def test_set_flag_appends_when_absent(self):
        from contremaitre.cli import _set_flag_value

        args = ["--base", "main"]
        _set_flag_value(args, "--agent", "codex")
        self.assertEqual(args, ["--base", "main", "--agent", "codex"])

    def test_set_flag_replaces_space_form(self):
        from contremaitre.cli import _set_flag_value

        args = ["--agent", "opencode", "--base", "main"]
        _set_flag_value(args, "--agent", "codex")
        self.assertEqual(args, ["--base", "main", "--agent", "codex"])

    def test_set_flag_replaces_equals_form(self):
        from contremaitre.cli import _set_flag_value

        args = ["--agent=opencode", "--base", "main"]
        _set_flag_value(args, "--agent", "codex")
        self.assertEqual(args, ["--base", "main", "--agent", "codex"])

    def test_remove_flag_drops_all_forms(self):
        from contremaitre.cli import _remove_flag

        args = ["--sim", "opencode", "--x", "1", "--sim=codex"]
        _remove_flag(args, "--sim")
        self.assertEqual(args, ["--x", "1"])


class CompositeRunnerTest(unittest.TestCase):
    def _runner(self, tmp, **over):
        paths = build_run_paths(Path(tmp) / "runs", f"20260605-{Path(tmp).name}")
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        cfg = RunConfig(
            repo=Path(tmp),
            base="main",
            runs_root=Path(tmp) / "runs",
            run_slug="t",
            actor_mode=ActorMode.FAKE,
            **over,
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
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "FINAL"},
                            }
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
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "FIRST"}}
                )
                + "\n"
            )
            off = p.stat().st_size  # boundary between turn 1 and turn 2
            with p.open("a") as f:
                f.write(json.dumps({"type": "thread.started", "thread_id": "S2"}) + "\n")
                f.write(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "SECOND"},
                        }
                    )
                    + "\n"
                )
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


class StampCodexSliceTest(unittest.TestCase):
    """codex events arrive clockless; we back-fill real per-turn timestamps so
    the viewer / TUI-attach show measured times instead of guesses."""

    def _turn(self):
        return [
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}}
            ),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}),
            json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}}
            ),
        ]

    def test_stamps_events_within_turn_window_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            p.write_text("\n".join(self._turn()) + "\n")
            _stamp_event_slice(p, start_offset=0, t_start=1000.0, t_end=1002.0)
            stamped = [json.loads(ln) for ln in p.read_text().splitlines()]
            ts = [e["timestamp"] for e in stamped]
            # Every event stamped, monotonic, inside [t_start, t_end] in ms.
            self.assertEqual(len(ts), 4)
            self.assertEqual(ts, sorted(ts))
            self.assertGreaterEqual(ts[0], 1_000_000)
            self.assertLessEqual(ts[-1], 1_002_000)
            # The final reply lands at ~turn-end (what the chat orders bubbles by).
            self.assertEqual(ts[-1], 1_002_000)
            # Content survives the rewrite — the turn still parses.
            text, _sid, usage, _err = _parse_codex_events(p)
            self.assertEqual(text, "hi")
            self.assertEqual(usage["input_tokens"], 5)

    def test_only_rewrites_the_offset_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            prior = json.dumps({"type": "turn.completed", "timestamp": 42}) + "\n"
            p.write_text(prior)
            off = len(prior.encode("utf-8"))
            with p.open("a") as fh:
                fh.write("\n".join(self._turn()) + "\n")
            _stamp_event_slice(p, start_offset=off, t_start=1000.0, t_end=1001.0)
            lines = [json.loads(ln) for ln in p.read_text().splitlines()]
            # Prior turn's existing timestamp is untouched; new slice is stamped.
            self.assertEqual(lines[0]["timestamp"], 42)
            self.assertTrue(all("timestamp" in e for e in lines[1:]))

    def test_preserves_existing_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            p.write_text(json.dumps({"type": "turn.started", "timestamp": 7}) + "\n")
            _stamp_event_slice(p, start_offset=0, t_start=1000.0, t_end=1001.0)
            self.assertEqual(json.loads(p.read_text())["timestamp"], 7)


# ===== claude CLI actor =======================================================


def _make_claude_runner(root: Path, **config_overrides):
    """Build a claude CliActorRunner (auth is the env token, no fixture home)."""
    paths = build_run_paths(root / "runs", f"20260606-{root.name}")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    config = RunConfig(
        repo=root,
        base="main",
        runs_root=root / "runs",
        run_slug="test",
        actor_mode=ActorMode.CLI,
        cli_tool="claude",
        docker_image="test-image",
        **config_overrides,
    )
    return CliActorRunner(config=config, paths=paths, tool="claude"), paths


class ClaudeModelArgTest(unittest.TestCase):
    def test_omits_namespaced_and_empty(self):
        self.assertEqual(_claude_model_arg("openrouter/deepseek/deepseek-v4-flash"), [])
        self.assertEqual(_claude_model_arg(""), [])

    def test_passes_claude_native_model(self):
        self.assertEqual(_claude_model_arg("opus"), ["--model", "opus"])
        self.assertEqual(_claude_model_arg("claude-opus-4-8"), ["--model", "claude-opus-4-8"])

    def test_falls_back_to_claude_default_for_namespaced(self):
        self.assertEqual(_claude_model_arg("opencode/x", "opus"), ["--model", "opus"])
        self.assertEqual(_claude_model_arg("", "opus"), ["--model", "opus"])
        # …but a claude-native per-role model still wins over the default.
        self.assertEqual(_claude_model_arg("sonnet", "opus"), ["--model", "sonnet"])

    def test_effort_arg(self):
        self.assertEqual(_claude_effort_arg("high"), ["--effort", "high"])
        self.assertEqual(_claude_effort_arg("max"), ["--effort", "max"])
        self.assertEqual(_claude_effort_arg(""), [])

    def test_parse_claude_model_from_turn_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "claude.jsonl"
            p.write_text(
                json.dumps(
                    {"type": "system", "subtype": "init", "session_id": "OLD", "model": "old"}
                )
                + "\n"
            )
            off = p.stat().st_size
            with p.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "system",
                            "subtype": "init",
                            "session_id": "S",
                            "model": "claude-opus-4-8",
                        }
                    )
                    + "\n"
                )
                fh.write(json.dumps({"type": "result", "subtype": "success", "result": "ok"}))

            self.assertEqual(_parse_claude_model(p, start_offset=off), "claude-opus-4-8")

    def test_meter_model_prefers_known_or_configured_model_then_sonnet(self):
        self.assertEqual(_claude_meter_model("claude-opus-4-8", "sonnet"), "claude-opus-4-8")
        self.assertEqual(_claude_meter_model("openrouter/x", "opus"), "opus")
        self.assertEqual(_claude_meter_model("", ""), "sonnet")


class ClaudeBuildCommandTest(unittest.TestCase):
    def test_first_turn_no_session_flag(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {_CLAUDE_OAUTH_ENV: "SECRET-TOKEN"}),
        ):
            runner, _ = _make_claude_runner(
                Path(tmp),
                docker_network="cmtr-int",
                https_proxy="http://p:3128",
                claude_model="opus",
                claude_effort="high",
            )
            cmd = runner._build_command(
                prompt="do it",
                home=runner.agent_home,
                session_id=None,
                model="openrouter/x",  # namespaced → falls back to claude_model
                mount_mode="rw",
                role="agent",
            )
            joined = " ".join(cmd)
            self.assertIn("--output-format", cmd)
            self.assertIn("stream-json", cmd)
            self.assertIn("--verbose", cmd)
            self.assertIn("--settings", cmd)
            self.assertEqual(
                cmd[cmd.index("--settings") + 1],
                "/root/.claude/projects/.contremaitre/settings.json",
            )
            self.assertIn("bypassPermissions", cmd)
            # First turn: no session flag — claude mints its own id (we capture it).
            self.assertNotIn("--session-id", cmd)
            self.assertNotIn("--resume", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
            self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
            self.assertIn(f"{runner.agent_home}:/root/.claude/projects:rw", joined)
            self.assertIn(f"{runner.worktree}:/app:rw", joined)
            # No real credential is forwarded: the host auth-inject proxy holds it.
            # claude carries base-url + dummy CLAUDE_CODE_OAUTH_TOKEN (subscription
            # mode → rate limits) + empty ANTHROPIC_AUTH_TOKEN (no API-key override).
            self.assertIn("ANTHROPIC_BASE_URL", cmd)
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", cmd)
            self.assertIn("ANTHROPIC_AUTH_TOKEN", cmd)
            self.assertNotIn("SECRET-TOKEN", joined)
            # claude runs OPEN egress + reaches the host proxy via host.docker.internal;
            # it ignores the codex egress-lock network even when one is configured.
            self.assertIn("--add-host", cmd)
            self.assertIn("host.docker.internal:host-gateway", cmd)
            self.assertNotIn("--network", cmd)
            # prompt is the final positional arg.
            self.assertEqual(cmd[-1], "do it")

    def test_resume_turn_uses_resume_not_session_id(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {_CLAUDE_OAUTH_ENV: "t"}):
            runner, _ = _make_claude_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )
            cmd = runner._build_command(
                prompt="again",
                home=runner.agent_home,
                session_id="SID-123",
                model="opus",
                mount_mode="rw",
                role="agent",
            )
            self.assertEqual(cmd[cmd.index("--resume") + 1], "SID-123")
            self.assertNotIn("--session-id", cmd)

    def test_review_mounts_worktree_readonly(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {_CLAUDE_OAUTH_ENV: "t"}):
            runner, _ = _make_claude_runner(
                Path(tmp), docker_network="cmtr-int", https_proxy="http://p:3128"
            )
            cmd = runner._build_command(
                prompt="review",
                home=runner.review_home,
                session_id=None,
                model="opus",
                mount_mode="ro",
                role="review",
                extra_mounts=((Path("/tmp/rev"), "/review", "ro"),),
            )
            joined = " ".join(cmd)
            self.assertIn(f"{runner.worktree}:/app:ro", joined)
            self.assertIn("/tmp/rev:/review:ro", joined)

    def test_usage_meter_command_uses_interactive_statusline_without_bypass(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {_CLAUDE_OAUTH_ENV: "SECRET-TOKEN"}),
        ):
            runner, _ = _make_claude_runner(
                Path(tmp),
                docker_network="cmtr-int",
                https_proxy="http://p:3128",
            )
            home = runner.driver.prepare_home(runner.agent_home)
            cmd = runner._build_claude_statusline_meter_command(home=home, role="agent")
            joined = " ".join(cmd)

            self.assertIn(f"{home / '.contremaitre' / 'claude.json'}:/root/.claude.json:rw", joined)
            self.assertIn(f"{home}:/root/.claude/projects:rw", joined)
            self.assertIn(f"{runner.worktree}:/app:ro", joined)
            self.assertIn("python3", cmd)
            self.assertIn("/root/.claude/projects/.contremaitre/statusline_meter.py", cmd)
            # The meter authenticates through the same host proxy — base-url +
            # dummy CLAUDE_CODE_OAUTH_TOKEN (subscription mode so rate limits flow).
            self.assertIn("ANTHROPIC_BASE_URL", cmd)
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", cmd)
            self.assertIn("ANTHROPIC_AUTH_TOKEN", cmd)  # forwarded empty (no API-key override)
            self.assertIn("CONTREMAITRE_CLAUDE_METER_MODEL", cmd)
            self.assertIn("CONTREMAITRE_CLAUDE_METER_PROMPT", cmd)
            self.assertNotIn("SECRET-TOKEN", joined)
            self.assertNotIn("--permission-mode", cmd)
            self.assertNotIn("bypassPermissions", cmd)


class ClaudeContainerEnvTest(unittest.TestCase):
    def test_points_at_host_proxy_and_scrubs_keys(self):
        # No real credential enters the container: the host auth-inject proxy
        # holds it. container_env carries only the base-url + a dummy bearer.
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "paid"}),
            patch(
                "contremaitre.cli_auth_proxy.ensure_auth_proxy",
                return_value="http://host.docker.internal:9999",
            ),
        ):
            runner, _ = _make_claude_runner(Path(tmp))
            env = runner.driver.container_env({})
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://host.docker.internal:9999")
            # Dummy CLAUDE_CODE_OAUTH_TOKEN keeps claude in subscription/OAuth mode
            # so interactive sessions (usage meter) track rate_limits.five_hour/seven_day.
            self.assertEqual(env[_CLAUDE_OAUTH_ENV], "contremaitre-injected")
            # Force-emptied so ANTHROPIC_AUTH_TOKEN can't override to API-key mode.
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "")
            self.assertEqual(env["ANTHROPIC_API_KEY"], "")
            # IS_SANDBOX=1 lets claude bypass permissions as root (the container
            # is a sandbox); without it claude exits "cannot be used with root".
            self.assertEqual(env["IS_SANDBOX"], "1")
            self.assertEqual(
                set(runner.driver.container_env_names()),
                {
                    "ANTHROPIC_BASE_URL",
                    _CLAUDE_OAUTH_ENV,
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_API_KEY",
                    "IS_SANDBOX",
                },
            )


class ClaudeEnsureReadyTest(unittest.TestCase):
    def test_raises_without_credential(self):
        from contremaitre.cli_auth_proxy import AuthProxyError

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "contremaitre.cli_auth_proxy.ensure_auth_proxy",
                side_effect=AuthProxyError("no claude credential found"),
            ),
        ):
            runner, _ = _make_claude_runner(Path(tmp))
            with self.assertRaises(Exception):
                runner.driver.ensure_ready()

    def test_passes_with_credential(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "contremaitre.cli_auth_proxy.ensure_auth_proxy",
                return_value="http://host.docker.internal:1",
            ),
        ):
            runner, _ = _make_claude_runner(Path(tmp))
            runner.driver.ensure_ready()  # no raise


class ClaudePrepareHomeTest(unittest.TestCase):
    def test_empty_home_no_credential_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_claude_runner(Path(tmp))
            home = runner.driver.prepare_home(runner.agent_home)
            self.assertTrue(home.is_dir())
            # No credential file is ever written (auth is the env token).
            self.assertFalse((home / "auth.json").exists())
            self.assertFalse((home / ".credentials.json").exists())

    def test_seeds_statusline_bridge_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_claude_runner(Path(tmp))
            home = runner.driver.prepare_home(runner.agent_home)
            statusline_dir = home / ".contremaitre"
            settings = json.loads((statusline_dir / "settings.json").read_text())
            script = (statusline_dir / "statusline.py").read_text()
            meter = (statusline_dir / "statusline_meter.py").read_text()
            global_config = json.loads((statusline_dir / "claude.json").read_text())

            self.assertEqual(settings["statusLine"]["type"], "command")
            self.assertIn(
                "/root/.claude/projects/.contremaitre/statusline.py",
                settings["statusLine"]["command"],
            )
            self.assertIn("/root/.claude/projects/.contremaitre/statusline.jsonl", script)
            self.assertIn("rate_limits", script)
            self.assertNotIn(_CLAUDE_OAUTH_ENV, script)
            self.assertIn("used_percentage", meter)
            self.assertNotIn(_CLAUDE_OAUTH_ENV, meter)
            self.assertTrue(global_config["hasCompletedOnboarding"])
            self.assertTrue(global_config["projects"]["/app"]["hasTrustDialogAccepted"])
            self.assertNotIn(_CLAUDE_OAUTH_ENV, json.dumps(global_config))

    def test_statusline_bridge_script_is_valid_python(self):
        compile(_CLAUDE_STATUSLINE_SCRIPT_BODY, "statusline.py", "exec")
        compile(_CLAUDE_STATUSLINE_METER_SCRIPT_BODY, "statusline_meter.py", "exec")

    def test_reseed_preserves_existing_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_claude_runner(Path(tmp))
            proj = runner.agent_home / "projects" / "app"
            proj.mkdir(parents=True, exist_ok=True)
            (proj / "sess.jsonl").write_text("{}")
            runner.driver.prepare_home(runner.agent_home)  # idempotent
            self.assertTrue((proj / "sess.jsonl").exists())


class ClaudeParseEventsTest(unittest.TestCase):
    def _stream(self):
        return [
            json.dumps(
                {"type": "system", "subtype": "init", "session_id": "abc", "model": "claude-opus"}
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "interim"},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "FINAL",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 80,
                    },
                    "total_cost_usd": 0.05,
                }
            ),
        ]

    def test_extracts_text_session_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text("\n".join(self._stream()))
            text, sid, usage, error = _parse_claude_events(p)
            self.assertEqual(text, "FINAL")  # result.result, not the interim block
            self.assertEqual(sid, "abc")
            self.assertEqual(usage["input_tokens"], 100)
            self.assertIsNone(error)

    def test_surfaces_error_subtype(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text(
                json.dumps(
                    {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""}
                )
            )
            text, _sid, _usage, error = _parse_claude_events(p)
            self.assertEqual(text, "")
            self.assertIn("error_max_turns", error)

    def test_start_offset_scopes_to_one_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text(
                json.dumps({"type": "result", "subtype": "success", "result": "FIRST"}) + "\n"
            )
            off = p.stat().st_size
            with p.open("a") as f:
                f.write(
                    json.dumps(
                        {"type": "system", "subtype": "init", "session_id": "S2", "model": "m"}
                    )
                    + "\n"
                )
                f.write(
                    json.dumps({"type": "result", "subtype": "success", "result": "SECOND"}) + "\n"
                )
            text, sid, _u, _e = _parse_claude_events(p, start_offset=off)
            self.assertEqual(text, "SECOND")
            self.assertEqual(sid, "S2")


class ClaudeAuthCheckTest(unittest.TestCase):
    def test_pass_when_token_set(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {_CLAUDE_OAUTH_ENV: "tok"}),
        ):
            cfg = _cli_config(Path(tmp), cli_tool="claude")
            self.assertEqual(_check_claude_auth(cfg).status, "PASS")
            self.assertEqual(_check_cli_auth(cfg).status, "PASS")  # dispatch

    def test_fail_when_no_credential_resolves(self):
        from contremaitre.cli_auth_proxy import AuthProxyError

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "contremaitre.cli_auth_proxy.resolve_claude_token",
                side_effect=AuthProxyError("no claude credential found"),
            ),
        ):
            cfg = _cli_config(Path(tmp), cli_tool="claude")
            self.assertEqual(_check_claude_auth(cfg).status, "FAIL")


class ClaudeTokenUsageTest(unittest.TestCase):
    def test_rolls_up_result_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            p.write_text(
                json.dumps(
                    {
                        "type": "result",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "cache_read_input_tokens": 80,
                        },
                    }
                )
            )
            self.assertEqual(
                sum_token_usage_in_events(read_jsonl(p)),
                {"input": 100, "output": 10, "reasoning": 0, "cache_read": 80},
            )

    def test_total_cost_usd_not_counted_as_spend(self):
        # claude runs on the OAuth subscription (no metered USD), so its
        # `result.total_cost_usd` is a NOTIONAL API-equivalent and must NOT be
        # summed as real spend (else the footer shows a misleading $ and the cost
        # cap could trip on a subscription run). costUSD in modelUsage too.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.jsonl"
            p.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "result",
                                "total_cost_usd": 0.05,
                                "modelUsage": {"claude-sonnet-4-6": {"costUSD": 0.05}},
                            }
                        ),
                        json.dumps({"type": "result", "total_cost_usd": 0.02}),
                    ]
                )
            )
            self.assertEqual(sum_costs_in_events(read_jsonl(p)), 0.0)


class ClaudeModelSpecTest(unittest.TestCase):
    def test_claude_role_records_model_and_effort(self):
        from contremaitre.models import ModelSpec

        spec = ModelSpec.build(
            mode=ActorMode.CLI,
            opencode_model="opencode/x",
            codex_model="gpt-5.5",
            codex_effort="high",
            cli_tool="claude",
            claude_model="opus",
            claude_effort="high",
        )
        self.assertEqual((spec.runtime, spec.requested, spec.effort), ("claude", "opus", "high"))
        self.assertEqual(spec.display(), "claude/opus high")

    def test_empty_claude_model_is_account_default(self):
        from contremaitre.models import ModelSpec

        spec = ModelSpec.build(
            mode=ActorMode.CLI,
            opencode_model="opencode/x",
            codex_model="gpt-5.5",
            codex_effort="high",
            cli_tool="claude",
            claude_model="",
            claude_effort="max",
        )
        # Account default: requested is empty verbatim; display falls back to
        # "default" until the stream resolves the real model.
        self.assertEqual((spec.runtime, spec.requested, spec.effort), ("claude", "", "max"))
        self.assertEqual(spec.display(), "claude/default max")
        # Once resolved from system/init, identity sharpens to the real model.
        resolved = spec.with_resolved("claude-sonnet-4-6")
        self.assertEqual(resolved.canonical(), ("claude-sonnet-4-6", "claude"))
        self.assertEqual(resolved.display(), "claude/claude-sonnet-4-6 max")


class ClaudeMakeRunnerTest(unittest.TestCase):
    def test_make_actor_runner_returns_claude_cli_runner(self):
        from contremaitre.actors import make_actor_runner

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_run_paths(Path(tmp) / "runs", f"20260606-{Path(tmp).name}")
            paths.run_dir.mkdir(parents=True, exist_ok=True)
            cfg = _cli_config(Path(tmp), cli_tool="claude")
            runner = make_actor_runner(config=cfg, paths=paths)
            self.assertIsInstance(runner, CliActorRunner)
            self.assertEqual(runner.tool, "claude")
            self.assertEqual(runner.driver.name, "claude")
            # claude homes are namespaced so they never collide with codex.
            self.assertTrue(str(runner.agent_home).endswith("claude-agent-home"))

    def test_claude_agent_with_opencode_sim_is_composite(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_run_paths(Path(tmp) / "runs", f"20260606-{Path(tmp).name}")
            paths.run_dir.mkdir(parents=True, exist_ok=True)
            cfg = _cli_config(Path(tmp), cli_tool="claude", sim_actor_mode=ActorMode.OPENCODE)
            self.assertIsInstance(make_actor_runner(config=cfg, paths=paths), CompositeActorRunner)


# ===== cross-CLI: codex agent + claude SIM (and the reverse) =====


def _xcli_paths(tmp):
    paths = build_run_paths(Path(tmp) / "runs", f"20260606-{Path(tmp).name}")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    return paths


class CrossCliRunnerTest(unittest.TestCase):
    def test_codex_agent_claude_sim_is_composite_of_two_cli_runners(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cli_config(
                Path(tmp), cli_tool="codex", sim_actor_mode=ActorMode.CLI, sim_cli_tool="claude"
            )
            r = make_actor_runner(config=cfg, paths=_xcli_paths(tmp))
            self.assertIsInstance(r, CompositeActorRunner)
            self.assertEqual(r._agent.tool, "codex")
            self.assertEqual(r._sim.tool, "claude")
            # Tool-namespaced homes → the two runners never collide in one run dir.
            self.assertTrue(str(r._agent.agent_home).endswith("codex-agent-home"))
            self.assertTrue(str(r._sim.sim_home).endswith("claude-sim-home"))

    def test_claude_agent_codex_sim_reverse(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cli_config(
                Path(tmp), cli_tool="claude", sim_actor_mode=ActorMode.CLI, sim_cli_tool="codex"
            )
            r = make_actor_runner(config=cfg, paths=_xcli_paths(tmp))
            self.assertIsInstance(r, CompositeActorRunner)
            self.assertEqual(r._agent.tool, "claude")
            self.assertEqual(r._sim.tool, "codex")

    def test_same_tool_both_cli_stays_single_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            # sim_cli_tool None → SIM shares the agent's tool → one runner, not composite.
            cfg = _cli_config(Path(tmp), cli_tool="claude", sim_actor_mode=ActorMode.CLI)
            r = make_actor_runner(config=cfg, paths=_xcli_paths(tmp))
            self.assertNotIsInstance(r, CompositeActorRunner)
            self.assertEqual(r.tool, "claude")


class ActiveCliToolsTest(unittest.TestCase):
    def test_mixed_run_reports_both_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cli_config(
                Path(tmp), cli_tool="codex", sim_actor_mode=ActorMode.CLI, sim_cli_tool="claude"
            )
            self.assertEqual(_active_cli_tools(cfg), {"codex", "claude"})

    def test_single_tool_run_reports_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cli_config(Path(tmp), cli_tool="claude")  # sim shares actor (CLI/claude)
            self.assertEqual(_active_cli_tools(cfg), {"claude"})

    def test_cli_agent_opencode_sim_reports_agent_tool_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cli_config(Path(tmp), cli_tool="claude", sim_actor_mode=ActorMode.OPENCODE)
            self.assertEqual(_active_cli_tools(cfg), {"claude"})

    def test_opencode_agent_with_cli_reviewer_reports_reviewer_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cli_config(
                Path(tmp),
                actor_mode=ActorMode.OPENCODE,
                sim_actor_mode=ActorMode.OPENCODE,
                cli_reviewer="codex",
            )
            self.assertEqual(_active_cli_tools(cfg), {"codex"})

    def test_opencode_agent_with_claude_reviewer_reports_claude_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cli_config(
                Path(tmp),
                actor_mode=ActorMode.OPENCODE,
                sim_actor_mode=ActorMode.OPENCODE,
                cli_reviewer="claude",
            )
            self.assertEqual(_active_cli_tools(cfg), {"claude"})


# ===== F3: CLI turns emit the actor-start guardrail (telemetry parity) =====


class CliActorStartEventTest(unittest.TestCase):
    def _guardrails(self, paths):
        return [
            json.loads(line)
            for line in paths.guardrail_events.read_text().splitlines()
            if line.strip()
        ]

    def test_agent_turn_emits_actor_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, paths = _make_runner(Path(tmp), allow_open_egress=True)
            runner.driver.parse_events = lambda *a, **k: ("ok", None, None, None)
            with patch(
                "contremaitre.cli_actor._run_detached_container", return_value=(0, "", None)
            ):
                runner.agent_turn("hello")
            starts = [e for e in self._guardrails(paths) if e.get("event") == "actor_start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["role"], "agent")
            self.assertEqual(starts[0]["tool"], "codex")
            self.assertNotIn("reviewer_id", starts[0])  # only review turns tag it

    def test_sim_review_emits_actor_start_with_reviewer_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, paths = _make_runner(Path(tmp), allow_open_egress=True)
            d = Path(tmp) / "diff.patch"
            d.write_text("diff")
            sd = Path(tmp) / "settled.md"
            sd.write_text("design")
            runner.driver.parse_events = lambda *a, **k: ("LOOKS_GOOD", None, None, None)
            with patch(
                "contremaitre.cli_actor._run_detached_container", return_value=(0, "", None)
            ):
                runner.sim_review(
                    diff_file=d,
                    settled_file=sd,
                    scenario="approved",
                    attempt=1,
                    reviewer_id="extra",
                )
            starts = [
                e
                for e in self._guardrails(paths)
                if e.get("event") == "actor_start" and e.get("role") == "review"
            ]
            self.assertEqual(starts[0]["reviewer_id"], "extra")

    def test_claude_successful_turn_refreshes_statusline_meter(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {_CLAUDE_OAUTH_ENV: "tok"}),
        ):
            runner, _paths = _make_claude_runner(Path(tmp), allow_open_egress=True)
            runner.driver.parse_events = lambda *a, **k: ("ok", "SID", None, None)

            def _fake_run_container(**kwargs):
                if kwargs["role"] == "agent":
                    kwargs["stdout_path"].write_text(
                        json.dumps(
                            {
                                "type": "system",
                                "subtype": "init",
                                "session_id": "SID",
                                "model": "claude-opus-4-8",
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                return (0, "", None)

            with patch(
                "contremaitre.cli_actor._run_detached_container", side_effect=_fake_run_container
            ) as run_container:
                runner.agent_turn("hello")
                # Join the background initial-probe thread while the patch is still
                # active so it doesn't escape and call the real docker runner.
                for t in runner._initial_meter_threads.values():
                    t.join(timeout=5)

            # 3 calls: initial background probe + agent turn + post-turn meter
            self.assertEqual(run_container.call_count, 3)
            calls = run_container.call_args_list
            actor_cmd = next(c.kwargs["cmd"] for c in calls if "-p" in c.kwargs["cmd"])
            meter_calls = [c for c in calls if "statusline_meter.py" in " ".join(c.kwargs["cmd"])]
            self.assertEqual(len(meter_calls), 2)
            self.assertIn("--permission-mode", actor_cmd)
            post_turn_meter = meter_calls[-1]
            self.assertNotIn("--permission-mode", post_turn_meter.kwargs["cmd"])
            self.assertEqual(
                post_turn_meter.kwargs["env"]["CONTREMAITRE_CLAUDE_METER_MODEL"], "claude-opus-4-8"
            )
            self.assertEqual(
                post_turn_meter.kwargs["env"]["CONTREMAITRE_CLAUDE_METER_PROMPT"], "OK"
            )


if __name__ == "__main__":
    unittest.main()
