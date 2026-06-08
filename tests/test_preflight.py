from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre.fixture import init_fixture
from contremaitre.models import ActorMode, Caps, RunConfig
from contremaitre.preflight import _check_network_policy, run_preflight


class PreflightTest(unittest.TestCase):
    def test_opencode_requires_explicit_network_policy(self):
        config = self._config(skip_openrouter_key_check=True)

        report = run_preflight(config)

        self.assertFalse(report.passed)
        self.assertIn("network_policy", self._fail_names(report))

    def test_unlimited_openrouter_key_fails(self):
        config = self._config(http_proxy="http://proxy.local:8080")
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}),
            self._mock_docker_ok(),
            patch(
                "contremaitre.preflight._fetch_openrouter_key",
                return_value={"data": {"label": "test", "limit": None, "limit_remaining": None}},
            ),
        ):
            report = run_preflight(config)

        self.assertFalse(report.passed)
        self.assertIn("openrouter_key", self._fail_names(report))

    def test_openrouter_remaining_above_cap_warns_but_passes(self):
        # Provider-side limit and orchestrator cap enforce different things:
        # the orchestrator cap is the per-run budget, the provider limit is
        # the daily backstop. A looser daily limit warns, doesn't block.
        config = self._config(http_proxy="http://proxy.local:8080", caps=Caps(max_cost_usd=30))
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}),
            self._mock_docker_ok(),
            patch(
                "contremaitre.preflight._fetch_openrouter_key",
                return_value={
                    "data": {
                        "label": "test",
                        "limit": 100,
                        "limit_remaining": 80,
                        "include_byok_in_limit": True,
                    }
                },
            ),
        ):
            report = run_preflight(config)

        self.assertTrue(report.passed, report.failure_summary())
        self.assertEqual("WARN", self._status_by_name(report)["openrouter_key"])

    def test_bounded_openrouter_key_and_explicit_proxy_pass(self):
        config = self._config(http_proxy="http://proxy.local:8080", caps=Caps(max_cost_usd=30))
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}),
            self._mock_docker_ok(),
            patch(
                "contremaitre.preflight._fetch_openrouter_key",
                return_value={
                    "data": {
                        "label": "test",
                        "limit": 30,
                        "limit_remaining": 12,
                        "include_byok_in_limit": True,
                    }
                },
            ),
        ):
            report = run_preflight(config)

        self.assertTrue(report.passed, report.failure_summary())

    def test_missing_key_passes_with_only_free_models(self):
        config = self._config(
            http_proxy="http://proxy.local:8080",
            agent_model="opencode/grok-code",
            sim_model="opencode/grok-code",
        )
        with patch.dict(os.environ, {}, clear=False), self._mock_docker_ok():
            os.environ.pop("OPENROUTER_API_KEY", None)
            report = run_preflight(config)

        self.assertTrue(report.passed, report.failure_summary())
        self.assertEqual("PASS", self._status_by_name(report)["openrouter_key"])

    def test_missing_key_fails_with_paid_model(self):
        # Default RunConfig models point at `openrouter/...` — paid slugs
        # that require a key. Absent key + paid model must still FAIL.
        config = self._config(http_proxy="http://proxy.local:8080")
        with patch.dict(os.environ, {}, clear=False), self._mock_docker_ok():
            os.environ.pop("OPENROUTER_API_KEY", None)
            report = run_preflight(config)

        self.assertFalse(report.passed)
        self.assertIn("openrouter_key", self._fail_names(report))

    def test_non_byok_limited_openrouter_key_warns_but_passes(self):
        config = self._config(http_proxy="http://proxy.local:8080", caps=Caps(max_cost_usd=30))
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}),
            self._mock_docker_ok(),
            patch(
                "contremaitre.preflight._fetch_openrouter_key",
                return_value={
                    "data": {
                        "label": "test",
                        "limit": 30,
                        "limit_remaining": 12,
                        "include_byok_in_limit": False,
                    }
                },
            ),
        ):
            report = run_preflight(config)

        self.assertTrue(report.passed, report.failure_summary())
        self.assertEqual("WARN", self._status_by_name(report)["openrouter_key"])

    def test_codex_role_open_egress_warns(self):
        # --allow-open-egress is the explicit override (warned), not a hard fail.
        config = self._config(actor_mode=ActorMode.CLI, allow_open_egress=True)
        self.assertEqual(_check_network_policy(config).status, "WARN")

    def test_codex_role_no_policy_no_flag_fails(self):
        # Default is locked: a codex role with neither an explicit policy nor the
        # override (i.e. auto-provision failed) must refuse, not run open.
        config = self._config(actor_mode=ActorMode.CLI, allow_open_egress=False)
        self.assertEqual(_check_network_policy(config).status, "FAIL")

    def test_codex_sim_no_policy_no_flag_fails(self):
        # Reverse mix: opencode agent + codex SIM, no policy/flag → FAIL.
        config = self._config(
            actor_mode=ActorMode.OPENCODE,
            sim_actor_mode=ActorMode.CLI,
            allow_open_egress=False,
        )
        self.assertEqual(_check_network_policy(config).status, "FAIL")

    def test_cli_reviewer_no_policy_no_flag_fails(self):
        # Opencode agent + opencode SIM still needs the CLI egress lock when
        # a post-publish codex/claude reviewer is configured.
        config = self._config(
            actor_mode=ActorMode.OPENCODE,
            sim_actor_mode=ActorMode.OPENCODE,
            cli_reviewer="codex",
            allow_open_egress=False,
        )
        self.assertEqual(_check_network_policy(config).status, "FAIL")

    def test_codex_role_passes_with_explicit_lock(self):
        config = self._config(
            actor_mode=ActorMode.CLI,
            docker_network="contremaitre-cli-egress",
            https_proxy="http://contremaitre-egress-proxy:3128",
        )
        self.assertEqual(_check_network_policy(config).status, "PASS")

    def test_pure_opencode_still_allows_open_egress(self):
        # The flag remains a valid opt-in for a non-codex run (WARN, not FAIL).
        config = self._config(actor_mode=ActorMode.OPENCODE, allow_open_egress=True)
        self.assertEqual(_check_network_policy(config).status, "WARN")

    def _config(self, **overrides):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        opencode_config = root / "opencode.json"
        opencode_config.write_text("{}", encoding="utf-8")
        data = {
            "repo": repo,
            "base": "main",
            "runs_root": root / "runs",
            "run_slug": "preflight",
            "actor_mode": ActorMode.OPENCODE,
            "docker_image": "test-image",
            "opencode_config": opencode_config,
        }
        data.update(overrides)
        return RunConfig(**data)

    @staticmethod
    def _fail_names(report):
        return {check.name for check in report.checks if check.status == "FAIL"}

    @staticmethod
    def _status_by_name(report):
        return {check.name: check.status for check in report.checks}

    @staticmethod
    def _mock_docker_ok():
        def fake_run(cmd):
            if cmd[0] == "git":
                return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
            if cmd[:3] == ["docker", "run", "--rm"] and "touch /app/.contremaitre_ro_probe" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="Read-only file system"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

        return patch("contremaitre.preflight._run", side_effect=fake_run)


if __name__ == "__main__":
    unittest.main()


class CliNetworkPolicyTest(unittest.TestCase):
    """F4: a CLI role's egress preflight must require BOTH an internal network AND
    an https proxy (matching cli_actor._assert_egress_locked) — a half-configured
    run that passes a generic 'either' check would be refused on the first turn."""

    def _cfg(self, **over):
        return RunConfig(
            repo=Path("/tmp"),
            base="main",
            runs_root=Path("/tmp/runs"),
            run_slug="t",
            actor_mode=ActorMode.CLI,
            **over,
        )

    def test_cli_only_network_fails(self):
        self.assertEqual(_check_network_policy(self._cfg(docker_network="net")).status, "FAIL")

    def test_cli_only_proxy_fails(self):
        self.assertEqual(
            _check_network_policy(self._cfg(https_proxy="http://p:3128")).status, "FAIL"
        )

    def test_cli_both_layers_pass(self):
        cfg = self._cfg(docker_network="net", https_proxy="http://p:3128")
        self.assertEqual(_check_network_policy(cfg).status, "PASS")

    def test_cli_allow_open_egress_warns(self):
        self.assertEqual(_check_network_policy(self._cfg(allow_open_egress=True)).status, "WARN")

    def test_opencode_still_accepts_either(self):
        # Unchanged for opencode: a single network OR proxy is sufficient.
        cfg = RunConfig(
            repo=Path("/tmp"),
            base="main",
            runs_root=Path("/tmp/runs"),
            run_slug="t",
            actor_mode=ActorMode.OPENCODE,
            docker_network="net",
        )
        self.assertEqual(_check_network_policy(cfg).status, "PASS")
