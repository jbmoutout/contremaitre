import tempfile
import unittest
from pathlib import Path

from contremaitre.eval import load_config


class EvalConfigTest(unittest.TestCase):
    def test_load_config_rejects_removed_extra_reviewer_model(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        case_dir = Path(tmp.name)
        configs = case_dir / "configs"
        configs.mkdir()
        (configs / "stale.toml").write_text(
            """
publish_mode = "gh"

[models]
agent_model = "opencode/deepseek"
sim_model = "opencode/qwen"
cli_reviewer = "codex"
extra_reviewer_model = "opencode/big-pickle"
""".lstrip(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "extra_reviewer_model was removed"):
            load_config(case_dir, "stale")


if __name__ == "__main__":
    unittest.main()
