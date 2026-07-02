"""Tests for the head-to-head A/B comparison (`eval ab` + viewer/ab.py).

Two layers:

- Pure comparison math (`separation`, `_winner`, `compare_metric`,
  `validity_checks`) over fabricated rows — the statistical honesty rules
  (range separation, infra exclusion, direction attribution) are load-bearing
  and must not drift silently.
- `build_ab_report` / `cmd_ab --report-only` integration over a fabricated
  golden case + runs root, asserting the written HTML carries the arms, the
  per-run viewer links, and the exclusion badges.
"""

import tempfile
import unittest
from pathlib import Path

from contremaitre.eval import CaseDef, ConfigDef, cmd_ab
from contremaitre.jsonlog import write_json
from contremaitre.viewer.ab import (
    METRICS,
    Metric,
    _winner,
    assemble_ab_data,
    build_ab_report,
    collect_run_row,
    compare_metric,
    metric_vector,
    separation,
    validity_checks,
)

_CASE = CaseDef(
    case_id="case_x",
    description="fabricated",
    target_url="https://github.com/x/y",
    base="main",
    expected_base_sha="abc123",
)
_CFG_A = ConfigDef(
    name="arm_a", agent_model="opencode/alpha", sim_model="opencode/judgey", cli_reviewer="codex"
)
_CFG_B = ConfigDef(
    name="arm_b", agent_model="opencode/beta", sim_model="opencode/judgey", cli_reviewer="codex"
)


def _write_run_dir(
    runs_root: Path,
    *,
    ts: str,
    config_name: str,
    rep: int,
    agent_model: str,
    verdict: str = "READY_FOR_DRAFT_PR",
    cli_verdict_line: str = "LOOKS_GOOD - ship it",
    wall: float = 100.0,
    cost: float = 0.5,
    base_sha: str = "abc123def",
    git_sha: str = "cafe",
    dirty: bool = False,
) -> Path:
    run_dir = runs_root / f"{ts}-eval-case_x-{config_name}-{rep:02d}"
    (run_dir / "eval").mkdir(parents=True)

    write_json(
        run_dir / "run_config.json",
        {
            "base_sha": base_sha,
            "contremaitre_git_sha": git_sha,
            "contremaitre_git_dirty": dirty,
            "dockerfile_sha256": "dock",
            "skills_lock_sha256": "skill",
            "prompt_hashes": {"initial_prompt.md": "p1"},
            "agent_model": agent_model,
            "sim_model": "opencode/judgey",
            "cli_reviewer": "codex",
        },
    )
    write_json(
        run_dir / "stats.json",
        {
            "run_id": run_dir.name,
            "verdict": verdict,
            "recorded_cost_usd": cost,
            "duration_seconds": wall,
            "turns": 4,
            "agent_model": agent_model,
            "sim_model": "opencode/judgey",
        },
    )
    write_json(
        run_dir / "pr.json",
        {"kind": "PUBLISHED", "url": "https://github.com/x/y/pull/7", "title": "deepen a seam"},
    )
    (run_dir / "review_cycles.jsonl").write_text(
        '{"verdict":"APPROVED","confidence":0.9,"reviewer":"sim","round":1,'
        '"checks_performed":["ran tests"],"required_changes":[]}\n',
        encoding="utf-8",
    )
    write_json(
        run_dir / "eval" / "pr_eval.json",
        {
            "verdict": verdict,
            "hard_gates": "PASS",
            "scorecard": {
                "self_verified": True,
                "settled_before_code": True,
                "sim_review_confidence": 0.9,
                "process_reliability": 1.0,
            },
        },
    )
    write_json(
        run_dir / "eval" / "flow_use.json",
        {
            "agent": {
                "implementation_complete_written": {"value": True},
                "exploration_convergence": {"value": "narrowed"},
                "tool_call_count": {"value": 10},
            },
            "sim": {"sim_useful_call_ratio": {"value": 1.0}, "sim_tool_call_count": {"value": 5}},
        },
    )
    (run_dir / "codex_review.md").write_text(
        f"{cli_verdict_line}\n\n**nit:** src/foo.py:12 tiny thing\n", encoding="utf-8"
    )
    (run_dir / "worktree_state.jsonl").write_text(
        '{"diff_stat": " 3 files changed, 30 insertions(+), 10 deletions(-)"}\n',
        encoding="utf-8",
    )
    (run_dir / "raw_export.jsonl").write_text(
        '{"type":"step_finish","part":{"tokens":{"input":100,"output":50},"cost":0}}\n'
        '{"type":"text","part":{"text":"done"}}\n',
        encoding="utf-8",
    )
    (run_dir / "review_diff_round1.diff").write_text(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n+added\n-removed\n",
        encoding="utf-8",
    )
    return run_dir


def _write_case_dir(project_root: Path) -> None:
    case_dir = project_root / "golden_cases" / "case_x"
    (case_dir / "configs").mkdir(parents=True)
    (case_dir / "case.toml").write_text(
        """
id = "case_x"
description = "fabricated"
target_url = "https://github.com/x/y"
base = "main"
expected_base_sha = "abc123"
""".lstrip(),
        encoding="utf-8",
    )
    for name, agent in (("arm_a", "opencode/alpha"), ("arm_b", "opencode/beta")):
        (case_dir / "configs" / f"{name}.toml").write_text(
            f"""
publish_mode = "gh"

[models]
agent_model = "{agent}"
sim_model = "opencode/judgey"
cli_reviewer = "codex"
""".lstrip(),
            encoding="utf-8",
        )


class SeparationTest(unittest.TestCase):
    def test_disjoint_ranges_attribute_the_greater_arm(self):
        self.assertEqual(separation([5, 6, 7], [1, 2, 3]), "a")
        self.assertEqual(separation([1, 2, 3], [5, 6, 7]), "b")

    def test_overlapping_ranges_are_overlap(self):
        self.assertEqual(separation([1, 4], [3, 9]), "overlap")
        # Touching boundaries are NOT separation — strict inequality only.
        self.assertEqual(separation([1, 3], [3, 9]), "overlap")

    def test_insufficient_n_returns_none(self):
        self.assertIsNone(separation([1], [2, 3]))
        self.assertIsNone(separation([], []))

    def test_winner_maps_direction(self):
        self.assertEqual(_winner("higher", "a"), "a")
        self.assertEqual(_winner("lower", "a"), "b")
        self.assertEqual(_winner("context", "a"), None)
        self.assertIsNone(_winner("higher", "overlap"))
        self.assertIsNone(_winner("higher", None))


class CompareMetricTest(unittest.TestCase):
    """compare_metric over real collect_run_row rows, including infra exclusion."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs_root = Path(tmp.name)

    def test_infra_runs_are_excluded_from_vectors(self):
        healthy = _write_run_dir(
            self.runs_root, ts="20260701-010000", config_name="arm_a", rep=1,
            agent_model="opencode/alpha", wall=100.0,
        )
        infra = _write_run_dir(
            self.runs_root, ts="20260701-020000", config_name="arm_a", rep=2,
            agent_model="opencode/alpha", wall=9999.0, verdict="FAILED_INFRA",
        )
        rows = [
            collect_run_row(_CASE, _CFG_A, healthy),
            collect_run_row(_CASE, _CFG_A, infra),
        ]
        wall_metric = next(m for m in METRICS if m.key == "wall_seconds")
        self.assertEqual(metric_vector(wall_metric, rows), [100.0])
        self.assertFalse(rows[1]["healthy"])

    def test_num_metric_produces_summary_delta_and_separation(self):
        rows_a = [
            collect_run_row(
                _CASE, _CFG_A,
                _write_run_dir(self.runs_root, ts=f"20260701-0{i}0000", config_name="arm_a",
                               rep=i, agent_model="opencode/alpha", wall=100.0 + i),
            )
            for i in (1, 2)
        ]
        rows_b = [
            collect_run_row(
                _CASE, _CFG_B,
                _write_run_dir(self.runs_root, ts=f"20260701-1{i}0000", config_name="arm_b",
                               rep=i, agent_model="opencode/beta", wall=300.0 + i),
            )
            for i in (1, 2)
        ]
        wall_metric = next(m for m in METRICS if m.key == "wall_seconds")
        rec = compare_metric(wall_metric, rows_a, rows_b)
        self.assertEqual(rec["a"]["n"], 2)
        self.assertEqual(rec["sep"], "b")  # B's walls are all greater...
        self.assertEqual(rec["winner"], "a")  # ...and lower is better.
        self.assertAlmostEqual(rec["delta"], 200.0)  # median(301,302) − median(101,102)

    def test_bool_metric_produces_rates(self):
        row = collect_run_row(
            _CASE, _CFG_A,
            _write_run_dir(self.runs_root, ts="20260701-050000", config_name="arm_a",
                           rep=1, agent_model="opencode/alpha"),
        )
        pr_metric = next(m for m in METRICS if m.key == "pr_landed")
        rec = compare_metric(pr_metric, [row], [row])
        self.assertEqual(rec["a"], {"rate": 1.0, "n": 1})


class ValidityChecksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs_root = Path(tmp.name)

    def _row(self, **kwargs):
        defaults = dict(config_name="arm_a", rep=1, agent_model="opencode/alpha")
        defaults.update(kwargs)
        return collect_run_row(_CASE, _CFG_A, _write_run_dir(self.runs_root, **defaults))

    def _check(self, checks, name_prefix):
        return next(c for c in checks if c["name"].startswith(name_prefix))

    def test_all_green_when_uniform_and_pinned(self):
        rows_a = [self._row(ts=f"20260701-0{i}0000", rep=i) for i in (1, 2, 3)]
        rows_b = [
            collect_run_row(
                _CASE, _CFG_B,
                _write_run_dir(self.runs_root, ts=f"20260701-1{i}0000", config_name="arm_b",
                               rep=i, agent_model="opencode/beta"),
            )
            for i in (1, 2, 3)
        ]
        checks = validity_checks(_CASE, _CFG_A, _CFG_B, rows_a, rows_b)
        for prefix in ("base pinned", "same judge", "single variable",
                       "environment uniform", "clean contremaitre", "all runs healthy",
                       "sample size"):
            self.assertEqual(self._check(checks, prefix)["status"], "pass", prefix)

    def test_judge_mismatch_fails(self):
        cfg_b = ConfigDef(name="arm_b", agent_model="opencode/beta",
                          sim_model="opencode/judgey", cli_reviewer="claude")
        checks = validity_checks(_CASE, _CFG_A, cfg_b, [], [])
        self.assertEqual(self._check(checks, "same judge")["status"], "fail")

    def test_env_drift_and_dirty_tree_warn(self):
        rows_a = [self._row(ts="20260701-010000", rep=1, git_sha="cafe")]
        rows_b = [
            collect_run_row(
                _CASE, _CFG_B,
                _write_run_dir(self.runs_root, ts="20260701-110000", config_name="arm_b",
                               rep=1, agent_model="opencode/beta", git_sha="beef", dirty=True),
            )
        ]
        checks = validity_checks(_CASE, _CFG_A, _CFG_B, rows_a, rows_b)
        env = self._check(checks, "environment uniform")
        self.assertEqual(env["status"], "warn")
        self.assertIn("contremaitre_git_sha", env["detail"])
        self.assertEqual(self._check(checks, "clean contremaitre")["status"], "warn")
        self.assertEqual(self._check(checks, "sample size")["status"], "warn")

    def test_base_sha_mismatch_fails(self):
        rows = [self._row(ts="20260701-010000", rep=1, base_sha="fff000")]
        checks = validity_checks(_CASE, _CFG_A, _CFG_B, rows, [])
        self.assertEqual(self._check(checks, "base pinned")["status"], "fail")

    def test_identical_configs_warn_on_single_variable(self):
        checks = validity_checks(_CASE, _CFG_A, _CFG_A, [], [])
        self.assertEqual(self._check(checks, "single variable")["status"], "warn")


class BuildAbReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project_root = Path(tmp.name)
        self.runs_root = self.project_root / "runs"
        self.runs_root.mkdir()
        _write_case_dir(self.project_root)
        for i in (1, 2, 3):
            _write_run_dir(self.runs_root, ts=f"20260701-0{i}0000", config_name="arm_a",
                           rep=i, agent_model="opencode/alpha", wall=100.0 + i)
            _write_run_dir(self.runs_root, ts=f"20260701-1{i}0000", config_name="arm_b",
                           rep=i, agent_model="opencode/beta", wall=300.0 + i,
                           cli_verdict_line="MUST_FIX - broken")
        # A fourth arm_b attempt that infra-failed — must appear in the roster
        # badge'd, and stay out of the metric vectors.
        _write_run_dir(self.runs_root, ts="20260701-140000", config_name="arm_b",
                       rep=4, agent_model="opencode/beta", verdict="FAILED_INFRA")

    def test_report_is_written_with_arms_links_and_badges(self):
        out = build_ab_report(
            project_root=self.project_root, runs_root=self.runs_root,
            case_id="case_x", config_a="arm_a", config_b="arm_b", n=3,
        )
        self.assertEqual(out.name, "ab--case_x--arm_a-vs-arm_b.html")
        html = out.read_text(encoding="utf-8")
        self.assertIn("arm_a", html)
        self.assertIn("opencode/beta", html)
        # Per-run trace links into the existing viewers.
        self.assertIn("20260701-010000-eval-case_x-arm_a-01/viewer.html", html)
        # n=3 window over arm_b picks the LATEST 3 (reps 2,3 + the infra 4).
        self.assertIn("20260701-140000-eval-case_x-arm_b-04/viewer.html", html)
        self.assertIn("INFRA — excluded from metrics", html)
        # wall_seconds separates (A strictly faster) → attributed to A.
        self.assertIn("A separated", html)
        # Scoreboard shows real diff size (+added −deleted), not the net delta,
        # and the roster pill reads added/deleted from diff_detail (the fixture
        # diff is +1/−1 over 1 file — a net delta of 0 must not render "+0 −0").
        self.assertIn("diff size (median)", html)
        self.assertIn("<b>1</b> files · " '<span style="color:var(--success)">+1</span>', html)
        self.assertIn("generated by <code>contremaitre eval ab</code>", html)

    def test_assemble_excludes_infra_from_vectors_but_keeps_roster(self):
        data = assemble_ab_data(
            project_root=self.project_root, runs_root=self.runs_root,
            case_id="case_x", config_a="arm_a", config_b="arm_b", n=3,
        )
        self.assertEqual(len(data["rows_b"]), 3)
        self.assertEqual(sum(1 for r in data["rows_b"] if r["healthy"]), 2)
        judge = next(r for r in data["comparisons"] if r["metric"].key == "cli_review_score")
        self.assertEqual(judge["values_a"], [1.0, 1.0, 1.0])
        self.assertEqual(judge["values_b"], [0.0, 0.0])

    def test_cmd_ab_report_only(self):
        rc = cmd_ab(
            project_root=self.project_root, case_id="case_x",
            config_a="arm_a", config_b="arm_b", n=3,
            runs_root=self.runs_root, report_only=True,
        )
        self.assertEqual(rc, 0)
        self.assertTrue((self.runs_root / "ab--case_x--arm_a-vs-arm_b.html").is_file())

    def test_cmd_ab_rejects_same_config(self):
        rc = cmd_ab(
            project_root=self.project_root, case_id="case_x",
            config_a="arm_a", config_b="arm_a", n=3,
            runs_root=self.runs_root, report_only=True,
        )
        self.assertEqual(rc, 2)

    def test_cmd_ab_report_only_without_runs_returns_2(self):
        empty = self.project_root / "empty_runs"
        empty.mkdir()
        rc = cmd_ab(
            project_root=self.project_root, case_id="case_x",
            config_a="arm_a", config_b="arm_b", n=3,
            runs_root=empty, report_only=True,
        )
        self.assertEqual(rc, 2)


class MetricRegistryTest(unittest.TestCase):
    def test_keys_are_unique_and_directions_valid(self):
        keys = [m.key for m in METRICS]
        self.assertEqual(len(keys), len(set(keys)))
        for m in METRICS:
            self.assertIn(m.better, ("higher", "lower", "context"), m.key)
            self.assertIn(m.kind, ("num", "bool", "cat"), m.key)

    def test_default_metric_getter_returns_none(self):
        self.assertIsNone(Metric("x", "x", "s", "context", "num").get({}))


if __name__ == "__main__":
    unittest.main()
