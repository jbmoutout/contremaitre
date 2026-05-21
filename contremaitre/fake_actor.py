"""Deterministic fake agent/SIM subprocess for fixture smoke runs.

Invoked as a subprocess by :class:`contremaitre.actors.FakeActorRunner` so the
fake-mode boundary mirrors the opencode boundary: a process receives the
worktree/run-dir context and emits text or a strict JSON verdict.

Subcommands match the multi-turn protocol the orchestrator drives:

  agent       -> single-turn end-to-end: writes SETTLED_DESIGN.md, a small
                 implementation, and IMPLEMENTATION_COMPLETE so the WORK loop
                 terminates immediately. The host orchestrator owns git; this
                 process never runs git commands.
  sim-turn    -> canned SWE-style reply (rarely reached because the WORK loop
                 exits after the agent's first turn).
  sim-review  -> strict JSON verdict based on --scenario.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Contremaitre fake actor")
    sub = ap.add_subparsers(dest="role", required=True)

    agent = sub.add_parser("agent")
    agent.add_argument("--worktree", required=True)
    agent.add_argument("--scenario", default="normal")

    sub.add_parser("sim-turn")

    sim_review = sub.add_parser("sim-review")
    sim_review.add_argument("--diff-file", required=True)
    sim_review.add_argument("--settled-file", required=True)
    sim_review.add_argument("--scenario", default="approved")
    sim_review.add_argument("--attempt", type=int, default=1)

    args = ap.parse_args(argv)
    if args.role == "agent":
        return _agent(args)
    if args.role == "sim-turn":
        return _sim_turn()
    return _sim_review(args)


def _agent(args: argparse.Namespace) -> int:
    worktree = Path(args.worktree)
    _write_settled_design(worktree)
    _write_implementation(worktree, args.scenario)
    if args.scenario == "no_impl_complete":
        print(
            "Fixture: SETTLED + implementation written but "
            "IMPLEMENTATION_COMPLETE intentionally withheld (cap/no-progress test)."
        )
        return 0
    _write_implementation_complete(worktree)
    print(
        "Fixture WORK complete: SETTLED_DESIGN.md locked, "
        "implementation written, IMPLEMENTATION_COMPLETE handoff."
    )
    return 0


def _write_settled_design(worktree: Path) -> None:
    design = worktree / ".contremaitre" / "SETTLED_DESIGN.md"
    design.parent.mkdir(parents=True, exist_ok=True)
    design.write_text(
        "# SETTLED DESIGN\n\n"
        "Add `src/contremaitre_demo.py` with a single `status()` boundary.\n"
        "Add `tests/test_contremaitre_demo.py` to verify the observable status.\n"
        "Do not touch forbidden paths. Keep the change intentionally small.\n",
        encoding="utf-8",
    )


def _write_implementation(worktree: Path, scenario: str) -> None:
    src = worktree / "src"
    tests = worktree / "tests"
    src.mkdir(exist_ok=True)
    tests.mkdir(exist_ok=True)
    ignore = worktree / ".gitignore"
    if not ignore.exists():
        ignore.write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (src / "contremaitre_demo.py").write_text(
        '"""Tiny fixture module written by the fake agent."""\n\n'
        "def status() -> str:\n"
        '    return "settled"\n',
        encoding="utf-8",
    )
    (tests / "test_contremaitre_demo.py").write_text(
        "import unittest\n\n"
        "from src.contremaitre_demo import status\n\n\n"
        "class DemoTest(unittest.TestCase):\n"
        "    def test_status(self):\n"
        '        self.assertEqual(status(), "settled")\n\n\n'
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    (src / "__init__.py").write_text("", encoding="utf-8")
    if scenario == "forbidden_path":
        forbidden = worktree / "prisma" / "migrations" / "0001_forbidden.sql"
        forbidden.parent.mkdir(parents=True, exist_ok=True)
        forbidden.write_text("-- forbidden-path fixture\n", encoding="utf-8")


def _write_implementation_complete(worktree: Path) -> None:
    marker = worktree / ".contremaitre" / "IMPLEMENTATION_COMPLETE"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("Fixture handoff: SETTLED implemented, checks attempted.\n", encoding="utf-8")


def _sim_turn() -> int:
    print(
        "Implementation looks aligned with SETTLED. No drift observed. "
        "Continue and hand off when ready."
    )
    return 0


def _sim_review(args: argparse.Namespace) -> int:
    scenario = args.scenario
    if scenario == "malformed":
        print("APPROVED but not JSON")
        return 0
    if scenario == "malformed_then_approved" and args.attempt == 1:
        print("{not-json")
        return 0

    diff = Path(args.diff_file).read_text(encoding="utf-8", errors="replace")
    settled = Path(args.settled_file).read_text(encoding="utf-8", errors="replace")
    if scenario == "changes_requested":
        payload = {
            "verdict": "CHANGES_REQUESTED",
            "confidence": 0.74,
            "required_changes": ["Add the missing boundary test before review."],
            "checks_performed": ["Read SETTLED design", "Read diff against base"],
            "summary": "The change is plausible but the review scenario requested revision.",
        }
    elif scenario == "needs_human":
        payload = {
            "verdict": "NEEDS_HUMAN",
            "confidence": 0.52,
            "required_changes": [],
            "checks_performed": ["Read SETTLED design", "Read diff against base"],
            "summary": "The fake SIM is configured to route this run to a human.",
        }
    else:
        payload = {
            "verdict": "APPROVED",
            "confidence": 0.9,
            "required_changes": [],
            "checks_performed": [
                "Read SETTLED design",
                "Read diff against base",
                f"Diff chars: {len(diff)}",
                f"SETTLED chars: {len(settled)}",
            ],
            "summary": "The implementation matches the settled fixture design.",
        }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
