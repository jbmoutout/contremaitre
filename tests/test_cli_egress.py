from __future__ import annotations

import argparse
import unittest

from contremaitre.cli import _cli_egress_is_auto
from contremaitre.cli_egress import (
    NETWORK_NAME,
    EgressError,
    _squid_conf_hash,
    ensure_egress_proxy,
    proxy_url,
)


class _Resp:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _make_runner(plan: dict):
    """Fake subprocess.run dispatching on the docker subcommand. `plan` keys:
    network_inspect, network_create, ps, run, connect, logs."""
    calls: list[list[str]] = []

    def runner(cmd, capture_output=True, text=True):
        calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "network" and cmd[2] == "inspect":
            return plan.get("network_inspect", _Resp(1))
        if sub == "network" and cmd[2] == "create":
            return plan.get("network_create", _Resp(0))
        if sub == "network" and cmd[2] == "connect":
            return plan.get("connect", _Resp(0))
        if sub == "ps":
            return plan.get("ps", _Resp(0, ""))
        if sub == "inspect":
            return plan.get("inspect", _Resp(0, ""))
        if sub == "run":
            return plan.get("run", _Resp(0, "cid"))
        if sub == "logs":
            return plan.get("logs", _Resp(0, "Accepting HTTP Socket connections"))
        return _Resp(0)

    runner.calls = calls
    return runner


def _subs(calls):
    return [(c[1], c[2]) if len(c) > 2 else (c[1],) for c in calls]


class EnsureEgressProxyTest(unittest.TestCase):
    def test_idempotent_when_already_up_and_config_current(self):
        # Running AND the squid-conf hash label matches → no recreate.
        runner = _make_runner(
            {
                "network_inspect": _Resp(0),
                "ps": _Resp(0, "running-cid\n"),
                "inspect": _Resp(0, _squid_conf_hash() + "\n"),
            }
        )
        net, url = ensure_egress_proxy(runner=runner, sleep=lambda *_: None)
        self.assertEqual(net, NETWORK_NAME)
        self.assertEqual(url, proxy_url())
        # Already running with current config → must NOT (re)create.
        self.assertNotIn(("network", "create"), _subs(runner.calls))
        self.assertNotIn(("run",), [(c[1],) for c in runner.calls])

    def test_recreates_when_allowlist_changed(self):
        # Running but the label is a stale hash (allowlist edited) → recreate so
        # the new squid.conf takes effect (squid bakes config in at start).
        runner = _make_runner(
            {
                "network_inspect": _Resp(0),
                "ps": _Resp(0, "running-cid\n"),
                "inspect": _Resp(0, "stale-hash-from-prior-allowlist\n"),
                "run": _Resp(0, "cid"),
                "logs": _Resp(0, "Accepting HTTP Socket connections"),
            }
        )
        ensure_egress_proxy(runner=runner, sleep=lambda *_: None)
        self.assertIn(("rm",), [(c[1],) for c in runner.calls])
        self.assertIn(("run",), [(c[1],) for c in runner.calls])

    def test_provisions_when_absent(self):
        runner = _make_runner(
            {
                "network_inspect": _Resp(1),  # absent
                "network_create": _Resp(0),
                "ps": _Resp(0, ""),  # not running
                "run": _Resp(0, "cid"),
                "connect": _Resp(0),
                "logs": _Resp(0, "Accepting HTTP Socket connections"),
            }
        )
        net, url = ensure_egress_proxy(runner=runner, sleep=lambda *_: None)
        self.assertEqual((net, url), (NETWORK_NAME, proxy_url()))
        subs = _subs(runner.calls)
        self.assertIn(("network", "create"), subs)
        self.assertIn(("run",), [(c[1],) for c in runner.calls])
        self.assertIn(("network", "connect"), subs)

    def test_raises_when_network_create_fails(self):
        runner = _make_runner({"network_inspect": _Resp(1), "network_create": _Resp(1, err="boom")})
        with self.assertRaises(EgressError):
            ensure_egress_proxy(runner=runner, sleep=lambda *_: None)


class CliEgressIsAutoTest(unittest.TestCase):
    def _args(self, **kw):
        ns = argparse.Namespace(
            actor="cli",
            sim_actor=None,
            cli_reviewer="none",
            allow_open_egress=False,
            docker_network=None,
            https_proxy=None,
        )
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_auto_for_cli_without_egress(self):
        self.assertTrue(_cli_egress_is_auto(self._args()))

    def test_not_auto_for_pure_opencode(self):
        self.assertFalse(_cli_egress_is_auto(self._args(actor="opencode")))

    def test_auto_for_cli_reviewer_with_opencode_roles(self):
        self.assertTrue(_cli_egress_is_auto(self._args(actor="opencode", cli_reviewer="codex")))

    def test_auto_for_codex_sim_with_opencode_agent(self):
        # Reverse mix: opencode agent + codex SIM still needs the lock (the codex
        # token rides in the SIM container).
        self.assertTrue(_cli_egress_is_auto(self._args(actor="opencode", sim_actor="cli")))

    def test_not_auto_when_open_egress(self):
        # --allow-open-egress is the explicit override: no auto-lock, runs open.
        self.assertFalse(_cli_egress_is_auto(self._args(allow_open_egress=True)))

    def test_not_auto_when_explicit_network(self):
        # An explicit operator-supplied policy wins (their own locked egress).
        self.assertFalse(_cli_egress_is_auto(self._args(docker_network="x")))


class SquidAllowlistTest(unittest.TestCase):
    def test_allowlist_covers_codex_and_opencode_providers(self):
        # The locked proxy must reach codex (openai/chatgpt) AND an opencode SIM
        # on either OpenRouter (paid) or OpenCode Zen (free → opencode.ai +
        # models.dev catalog), so a mixed run works locked.
        from contremaitre.cli_egress import _SQUID_CONF

        conf = _SQUID_CONF.read_text(encoding="utf-8")
        for domain in (
            ".chatgpt.com",
            ".openai.com",
            ".anthropic.com",  # claude (the CLI actor) on a Claude subscription
            ".openrouter.ai",
            ".opencode.ai",
            ".models.dev",
        ):
            self.assertIn(domain, conf)
        self.assertIn("http_access deny all", conf)  # default-deny stays


if __name__ == "__main__":
    unittest.main()
