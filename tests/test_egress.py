import unittest

from contremaitre import egress


class EgressRuleTest(unittest.TestCase):
    def test_codex_is_credential_bearing_claude_is_not(self):
        self.assertTrue(egress.is_credential_bearing("codex"))
        self.assertFalse(egress.is_credential_bearing("claude"))
        self.assertFalse(egress.is_credential_bearing("opencode"))

    def test_codex_locked_unless_open_override(self):
        self.assertTrue(egress.cli_tool_locked("codex", allow_open_egress=False))
        self.assertFalse(egress.cli_tool_locked("codex", allow_open_egress=True))

    def test_host_injected_tool_never_locked(self):
        # claude is host-injected — nothing to exfiltrate, so never locked.
        self.assertFalse(egress.cli_tool_locked("claude", allow_open_egress=False))
        self.assertFalse(egress.cli_tool_locked("claude", allow_open_egress=True))

    def test_any_locked_across_active_tools(self):
        self.assertTrue(egress.any_locked_cli_tool({"claude", "codex"}, allow_open_egress=False))
        self.assertFalse(egress.any_locked_cli_tool({"claude", "codex"}, allow_open_egress=True))
        self.assertFalse(egress.any_locked_cli_tool({"claude"}, allow_open_egress=False))
        self.assertFalse(egress.any_locked_cli_tool(set(), allow_open_egress=False))


if __name__ == "__main__":
    unittest.main()
