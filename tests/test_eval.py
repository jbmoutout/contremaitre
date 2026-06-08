import tempfile
import unittest
from pathlib import Path

from contremaitre.eval import CaseDef, ConfigDef, check_run, load_config
from contremaitre.jsonlog import write_json


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

    def test_pr_needs_human_is_valid_published_terminal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name) / "run"
        (run_dir / "eval").mkdir(parents=True)

        write_json(
            run_dir / "run_config.json",
            {
                "base_sha": "abcdef",
                "contremaitre_git_dirty": False,
                "agent_model": "opencode/deepseek",
                "sim_model": "opencode/qwen",
                "cli_reviewer": "codex",
            },
        )
        write_json(
            run_dir / "stats.json",
            {
                "run_id": "run",
                "verdict": "PR_NEEDS_HUMAN",
                "recorded_cost_usd": 0.0,
                "duration_seconds": 12.0,
            },
        )
        write_json(run_dir / "pr.json", {"kind": "PUBLISHED"})
        (run_dir / "review_cycles.jsonl").write_text(
            '{"verdict":"APPROVED","confidence":0.9}\n',
            encoding="utf-8",
        )
        write_json(
            run_dir / "eval" / "pr_eval.json",
            {
                "verdict": "PR_NEEDS_HUMAN",
                "hard_gates": "PASS",
                "scorecard": {
                    "self_verified": True,
                    "settled_before_code": True,
                    "sim_review_confidence": 0.9,
                    "process_reliability": 0.5,
                },
            },
        )
        write_json(
            run_dir / "eval" / "flow_use.json",
            {
                "agent": {
                    "implementation_complete_written": {"value": True},
                    "exploration_convergence": {"value": "narrowed"},
                },
                "sim": {"sim_useful_call_ratio": {"value": 1.0}},
            },
        )
        (run_dir / "codex_review.md").write_text(
            "MUST_FIX - still blocked\n\n**issue:** README.md:1 fix it\n",
            encoding="utf-8",
        )

        report = check_run(
            CaseDef(
                case_id="case",
                description="case",
                target_url="https://github.com/x/y",
                base="main",
                expected_base_sha="abc",
            ),
            ConfigDef(
                name="default",
                agent_model="opencode/deepseek",
                sim_model="opencode/qwen",
                cli_reviewer="codex",
            ),
            run_dir,
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.headline["terminal_verdict"], "PR_NEEDS_HUMAN")
        self.assertEqual(report.headline["terminal_score"], 0.5)


if __name__ == "__main__":
    unittest.main()
