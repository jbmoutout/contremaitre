from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre.cli_actor import (
    CliActorRunner,
    _access_token_exp,
    _codex_model_arg,
    _parse_codex_events,
)
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

    def test_near_expiry_token_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = _make_runner(Path(tmp))
            (runner._src_codex_home / "auth.json").write_text(
                json.dumps(
                    {"tokens": {"access_token": _fake_jwt(int(time.time()) + 60),
                                "refresh_token": "r"}}
                )
            )
            with self.assertRaises(Exception):
                runner.prepare_codex_home(runner.agent_home)


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
