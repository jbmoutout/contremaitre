"""Sharper tests that lock specific artifacts at the actor + publication boundary.

The pre-existing suite verifies that artifacts EXIST. These tests verify
their CONTENT — what role/phase the actor logged, what shape pr.json takes
per terminal kind, what transcript rows landed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from contremaitre import events
from contremaitre.actors import (
    FakeActorRunner,
    _harvest_step_finishes_from_sqlite,
    _recover_text_from_sqlite,
    _record_recovery,
)
from contremaitre.costs import estimate_recorded_cost_usd
from contremaitre.fixture import init_fixture
from contremaitre.models import Caps, RunConfig, TerminalVerdict
from contremaitre.orchestrator import run
from contremaitre.paths import build_run_paths, new_run_id


# ---------- actor-boundary tests (FakeActorRunner alone, no orchestrator) ----------


class FakeActorWritesItsOwnArtifactsTest(unittest.TestCase):
    """C1's invariant: the actor owns raw_export + transcript writes.

    These tests instantiate FakeActorRunner directly and check that each
    public method (agent_turn, sim_turn, sim_review) writes the right JSONL
    event and the right transcript row.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo = init_fixture(root / "repo")
        runs_root = root / "runs"
        run_id = new_run_id("actor-test")
        self.paths = build_run_paths(runs_root, run_id)
        self.paths.run_dir.mkdir(parents=True)
        # Worktree gets created by the orchestrator normally; for these tests
        # we point the fake actor at the fixture repo directly so its
        # subprocess can write inside a real directory.
        self.paths = self.paths.__class__(**{**self.paths.__dict__, "worktree": self.repo})
        self.actor = FakeActorRunner(
            paths=self.paths,
            agent_scenario="normal",
            sim_scenario="approved",
        )

    def _read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_agent_turn_writes_text_event_with_role_and_phase(self):
        out = self.actor.agent_turn("ignored by fake")

        self.assertTrue(out.text)
        events_list = self._read_jsonl(self.paths.raw_export)
        text_events = [e for e in events_list if e.get("type") == "text"]
        self.assertEqual(len(text_events), 1)
        self.assertEqual(text_events[0]["role"], "agent")
        self.assertEqual(text_events[0]["phase"], "WORK")
        self.assertEqual(text_events[0]["part"]["text"], out.text)

    def test_sim_turn_writes_text_event_to_sim_raw_export(self):
        out = self.actor.sim_turn("ignored")

        self.assertTrue(out.text)
        # No leakage into the agent stream.
        self.assertFalse(self.paths.raw_export.exists())
        events_list = self._read_jsonl(self.paths.sim_raw_export)
        text_events = [e for e in events_list if e.get("type") == "text"]
        self.assertEqual(len(text_events), 1)
        self.assertEqual(text_events[0]["role"], "sim")
        self.assertEqual(text_events[0]["phase"], "WORK")
        self.assertEqual(text_events[0]["part"]["text"], out.text)

    def test_sim_review_writes_strict_json_verdict(self):
        # sim_review needs the agent to have produced SETTLED + a diff.
        # Easier here to fake the inputs directly.
        diff_file = self.paths.run_dir / "diff.patch"
        settled_file = self.paths.run_dir / "SETTLED_DESIGN.md"
        diff_file.write_text("dummy diff", encoding="utf-8")
        settled_file.write_text("dummy settled", encoding="utf-8")

        out = self.actor.sim_review(
            diff_file=diff_file,
            settled_file=settled_file,
            scenario="approved",
            attempt=1,
        )

        verdict = json.loads(out.text)
        self.assertEqual(verdict["verdict"], "APPROVED")
        self.assertIn("confidence", verdict)
        # The actor's REVIEW-phase event lands in sim_raw_export.
        events_list = self._read_jsonl(self.paths.sim_raw_export)
        review_text_events = [
            e for e in events_list if e.get("type") == "text" and e.get("phase") == "REVIEW"
        ]
        self.assertEqual(len(review_text_events), 1)
        self.assertEqual(review_text_events[0]["role"], "sim")

    def test_actor_interleaves_transcript_rows(self):
        self.actor.agent_turn("first")
        self.actor.sim_turn("second")

        transcript = self.paths.transcript.read_text(encoding="utf-8")
        # Full-marker equality (not substring): the downstream viewer/
        # extractor parse on this exact `\n\n## {phase} - {speaker}\n\n`
        # shape — a regression that flipped the order to `## agent - WORK`
        # or dropped a newline would fail here.
        agent_marker = "\n\n## WORK - agent\n\n"
        sim_marker = "\n\n## WORK - sim\n\n"
        agent_idx = transcript.find(agent_marker)
        sim_idx = transcript.find(sim_marker)
        self.assertNotEqual(agent_idx, -1, f"agent marker not found in: {transcript!r}")
        self.assertNotEqual(sim_idx, -1, f"sim marker not found in: {transcript!r}")
        self.assertLess(agent_idx, sim_idx)


# ---------- pr.json schema-by-kind tests ----------


class PrJsonSchemaPerKindTest(unittest.TestCase):
    """Lock C2's invariant: pr.json carries the same key set across every
    terminal, with the two hash fields populated by drift case.
    """

    REQUIRED_KEYS = {
        "kind",
        "branch",
        "base",
        "url",
        "approved_diff_hash",
        "current_diff_hash",
        "reason",
        "publish_mode",
        "dry_run",
        "title",
    }

    def test_published_has_equal_hashes(self):
        pr = self._run_and_read("published")
        self.assertEqual(set(pr.keys()), self.REQUIRED_KEYS)
        self.assertEqual(pr["kind"], "PUBLISHED")
        self.assertIsNotNone(pr["approved_diff_hash"])
        self.assertEqual(pr["approved_diff_hash"], pr["current_diff_hash"])

    def test_blocked_on_drift_has_diverging_hashes(self):
        pr = self._run_and_read("drift", simulate_drift_after_approval=True)
        self.assertEqual(set(pr.keys()), self.REQUIRED_KEYS)
        self.assertEqual(pr["kind"], "BLOCKED")
        self.assertIsNotNone(pr["approved_diff_hash"])
        self.assertIsNotNone(pr["current_diff_hash"])
        self.assertNotEqual(pr["approved_diff_hash"], pr["current_diff_hash"])

    def test_no_pr_has_null_hashes(self):
        pr = self._run_and_read("needs", sim_scenario="needs_human")
        self.assertEqual(set(pr.keys()), self.REQUIRED_KEYS)
        self.assertEqual(pr["kind"], "NO_PR")
        self.assertIsNone(pr["approved_diff_hash"])
        self.assertIsNone(pr["current_diff_hash"])

    def _run_and_read(self, slug: str, **overrides) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=root / "runs",
            run_slug=slug,
            check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
            caps=Caps(),
            **overrides,
        )
        result = run(config)
        return json.loads((result.run_dir / "pr.json").read_text(encoding="utf-8"))


# ---------- recoveries.jsonl content (sanity check the recovery surface) ----------


class RecoveryArtifactShapeTest(unittest.TestCase):
    def test_fake_run_produces_no_recoveries_file(self):
        """Fake mode never touches docker, so no recovery events should fire."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = init_fixture(root / "repo")
        config = RunConfig(
            repo=repo,
            base="main",
            runs_root=root / "runs",
            run_slug="no-recoveries",
            check_cmds=(f"{sys.executable} -m unittest discover -s tests",),
        )
        result = run(config)

        recoveries = result.run_dir / "recoveries.jsonl"
        self.assertEqual(result.verdict, TerminalVerdict.READY_FOR_DRAFT_PR)
        self.assertFalse(
            recoveries.exists(),
            f"fake mode shouldn't write recoveries.jsonl; got: "
            f"{recoveries.read_text(encoding='utf-8') if recoveries.exists() else ''}",
        )


# ---------- subagent step_finish harvest from opencode.db ----------


class HarvestStepFinishFromSqliteTest(unittest.TestCase):
    """Lock the invariant from the OpenRouter reconciliation: every
    step-finish part opencode persists to its sqlite — across parent AND
    child sessions — must show up in `recorded_cost_usd`.

    Cause: opencode's --format=json stdout streams events for the invoked
    session only, so subagent (child) sessions spawned via the `task`
    tool are invisible to the parent's raw_export. Also, the parent's
    final step-finish sometimes lands in the DB after docker exits
    without flushing. Both undercount cost by ~2x in subagent-heavy runs.

    Fix: `_harvest_step_finishes_from_sqlite` synthesizes the missing
    events back into raw_export from the DB after every turn. The cost
    estimator walks any JSON for `cost`-like keys, so synthesized parts
    contribute identically to real ones.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "opencode-state"
        self.state_dir.mkdir()
        self.raw_export = Path(self._tmp.name) / "raw_export.jsonl"

    def _make_db(self, *, parts: list[tuple[str, str, dict]]) -> None:
        """Build a minimal opencode.db with just the columns harvest reads.

        `parts` is a list of (session_id, part_id, part_data_dict). Real
        opencode keeps the `id` and `message_id` on the table columns and
        does NOT duplicate them inside the `data` JSON blob — only the
        envelope-streaming code on the read side reassembles them. Mirror
        that here, or the harvest function's table-column dedupe wouldn't
        be exercised faithfully.
        """

        db_path = self.state_dir / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE part (id TEXT, session_id TEXT, message_id TEXT, "
            "time_created INTEGER, data TEXT)"
        )
        for i, (session_id, part_id, data) in enumerate(parts):
            cur.execute(
                "INSERT INTO part (id, session_id, message_id, time_created, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    part_id,
                    session_id,
                    f"msg_{part_id}",
                    1779484395509 + i,
                    json.dumps(data),
                ),
            )
        conn.commit()
        conn.close()

    def _step_finish(self, cost: float) -> dict:
        return {
            "type": "step-finish",
            "reason": "tool-calls",
            "cost": cost,
            "tokens": {
                "input": 100,
                "output": 10,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
        }

    def _read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_harvest_appends_parent_and_subagent_step_finishes(self):
        # Parent already has one step_finish in raw_export (from stdout).
        self.raw_export.write_text(
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_parent",
                    "part": {"id": "prt_p1", "type": "step-finish", "cost": 0.01},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self._make_db(
            parts=[
                ("ses_parent", "prt_p1", self._step_finish(0.01)),  # already in raw_export
                ("ses_parent", "prt_p2", self._step_finish(0.02)),  # stdout missed it
                ("ses_child1", "prt_c1a", self._step_finish(0.03)),  # subagent
                ("ses_child1", "prt_c1b", self._step_finish(0.04)),
                ("ses_child2", "prt_c2", self._step_finish(0.05)),
            ]
        )

        appended = _harvest_step_finishes_from_sqlite(self.state_dir, self.raw_export)

        self.assertEqual(appended, 4, "should append all step_finishes except the duplicate")
        events_list = self._read_jsonl(self.raw_export)
        self.assertEqual(len(events_list), 5)
        synthesized = [e for e in events_list if e.get("_synthesized_from_sqlite")]
        self.assertEqual(len(synthesized), 4)
        # All synthesized events keep their part envelope intact.
        for e in synthesized:
            self.assertEqual(e["type"], "step_finish")
            self.assertIn("part", e)
            self.assertIn("cost", e["part"])
        # Cost estimator sees the full $0.15 (parent stdout + harvested).
        total = estimate_recorded_cost_usd(self.raw_export)
        self.assertAlmostEqual(total, 0.15, places=6)

    def test_harvest_is_idempotent(self):
        """Calling harvest twice doesn't double-count — part.id dedupes."""

        self._make_db(
            parts=[
                ("ses_child", "prt_c1", self._step_finish(0.03)),
                ("ses_child", "prt_c2", self._step_finish(0.04)),
            ]
        )

        first = _harvest_step_finishes_from_sqlite(self.state_dir, self.raw_export)
        second = _harvest_step_finishes_from_sqlite(self.state_dir, self.raw_export)

        self.assertEqual(first, 2)
        self.assertEqual(second, 0)
        total = estimate_recorded_cost_usd(self.raw_export)
        self.assertAlmostEqual(total, 0.07, places=6)

    def test_harvest_no_db_is_noop(self):
        """No opencode.db (e.g. fake actor mode) → harvest is silent no-op."""

        # No DB created.
        self.assertEqual(
            _harvest_step_finishes_from_sqlite(self.state_dir, self.raw_export),
            0,
        )
        self.assertFalse(self.raw_export.exists())

    def test_harvest_uses_table_id_column_not_json_blob(self):
        """Regression: opencode keeps `id` on the `part` table column, NOT
        inside the JSON `data` blob. An earlier draft of the harvest read
        `data["id"]` and silently emitted nothing on real DBs (found the
        bug only when running end-to-end against a recorded run).

        Lock the behavior by building rows whose JSON blob has no `id`
        field and confirming the harvest still emits — and that the
        synthesized envelope's `part.id` matches the table column.
        """

        self._make_db(
            parts=[
                ("ses_child", "prt_table_only", self._step_finish(0.02)),
            ]
        )
        # Sanity: the data blob on disk has no `id` field.
        db_path = self.state_dir / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        (blob,) = conn.execute("SELECT data FROM part").fetchone()
        conn.close()
        self.assertNotIn("id", json.loads(blob))

        appended = _harvest_step_finishes_from_sqlite(self.state_dir, self.raw_export)

        self.assertEqual(appended, 1)
        events_list = self._read_jsonl(self.raw_export)
        self.assertEqual(events_list[0]["part"]["id"], "prt_table_only")
        # messageID + sessionID get reassembled from columns too.
        self.assertEqual(events_list[0]["part"]["messageID"], "msg_prt_table_only")
        self.assertEqual(events_list[0]["part"]["sessionID"], "ses_child")

    def test_harvest_ignores_non_step_finish_parts(self):
        """Text/tool parts in `part` table must not be re-emitted as step_finish."""

        self._make_db(
            parts=[
                ("ses_child", "prt_text", {"type": "text", "text": "hello"}),
                ("ses_child", "prt_tool", {"type": "tool-call", "name": "bash"}),
                ("ses_child", "prt_sf", self._step_finish(0.02)),
            ]
        )

        appended = _harvest_step_finishes_from_sqlite(self.state_dir, self.raw_export)

        self.assertEqual(appended, 1)
        events_list = self._read_jsonl(self.raw_export)
        self.assertEqual(len(events_list), 1)
        self.assertEqual(events_list[0]["part"]["id"], "prt_sf")


# ---------- positive sqlite-recovery surface ----------


class RecoverTextFromSqliteTest(unittest.TestCase):
    """Lock the silent-stall recovery: when opencode persisted the message
    parts to sqlite but never flushed the corresponding `text` event to
    stdout, `_recover_text_from_sqlite` must read the parts back and
    return the concatenated text + a `completed` flag derived from the
    step-finish part's `reason`.

    Companion to the harvest tests: harvest covers the *cost* leak (the
    step-finish parts), this covers the *text* leak (the message body).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "opencode-state"
        self.state_dir.mkdir()

    def _make_db(
        self,
        *,
        sessions: list[tuple[str, int]],
        messages: list[tuple[str, str, int]],
        parts: list[tuple[str, str, dict, int]],
    ) -> None:
        """Build a minimal opencode.db with session/message/part tables.

        Mirrors real opencode: `id`, `session_id`, `message_id` are table
        columns, NOT inside the JSON `data` blob. Per-part `data` carries
        only the type/text/reason fields the recovery function reads.
        """

        db_path = self.state_dir / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("CREATE TABLE session (id TEXT, time_created INTEGER)")
        cur.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER)")
        cur.execute(
            "CREATE TABLE part (id TEXT, session_id TEXT, message_id TEXT, "
            "time_created INTEGER, data TEXT)"
        )
        for sid, ts in sessions:
            cur.execute("INSERT INTO session VALUES (?, ?)", (sid, ts))
        for mid, sid, ts in messages:
            cur.execute("INSERT INTO message VALUES (?, ?, ?)", (mid, sid, ts))
        for mid, pid, data, ts in parts:
            # session_id on `part` is populated by opencode; recovery reads
            # via message_id so the value is incidental but realistic.
            cur.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
                (pid, "ignored", mid, ts, json.dumps(data)),
            )
        conn.commit()
        conn.close()

    def test_recovers_concatenated_text_and_completed_flag(self):
        self._make_db(
            sessions=[("ses_a", 1)],
            messages=[("msg_1", "ses_a", 100)],
            parts=[
                ("msg_1", "prt_t1", {"type": "text", "text": "Hello "}, 101),
                ("msg_1", "prt_t2", {"type": "text", "text": "world."}, 102),
                ("msg_1", "prt_sf", {"type": "step-finish", "reason": "stop"}, 103),
            ],
        )

        text, msg_id, completed = _recover_text_from_sqlite(self.state_dir, "ses_a")

        self.assertEqual(text, "Hello world.")
        self.assertEqual(msg_id, "msg_1")
        self.assertTrue(completed)

    def test_step_finish_with_non_stop_reason_is_not_completed(self):
        """Mid-stream interrupt → completed=False so the caller knows
        the recovered text is partial."""

        self._make_db(
            sessions=[("ses_a", 1)],
            messages=[("msg_1", "ses_a", 100)],
            parts=[
                ("msg_1", "prt_t1", {"type": "text", "text": "partial"}, 101),
                ("msg_1", "prt_sf", {"type": "step-finish", "reason": "tool-calls"}, 102),
            ],
        )

        text, _msg_id, completed = _recover_text_from_sqlite(self.state_dir, "ses_a")

        self.assertEqual(text, "partial")
        self.assertFalse(completed)

    def test_no_session_id_picks_latest_session(self):
        """Passing `None` makes recovery fall back to the latest session
        by `time_created`. Locks the ORDER BY direction — a regression
        that flipped to ASC would return the oldest session instead."""

        self._make_db(
            sessions=[("ses_old", 1), ("ses_new", 100)],
            messages=[
                ("msg_old", "ses_old", 50),
                ("msg_new", "ses_new", 200),
            ],
            parts=[
                ("msg_old", "prt_o", {"type": "text", "text": "OLD"}, 51),
                ("msg_new", "prt_n", {"type": "text", "text": "NEW"}, 201),
            ],
        )

        text, msg_id, _completed = _recover_text_from_sqlite(self.state_dir, None)

        self.assertEqual(text, "NEW")
        self.assertEqual(msg_id, "msg_new")

    def test_no_db_returns_noop(self):
        text, msg_id, completed = _recover_text_from_sqlite(self.state_dir, "ses_a")

        self.assertIsNone(text)
        self.assertIsNone(msg_id)
        self.assertFalse(completed)


class RecordRecoveryTest(unittest.TestCase):
    """Lock the positive case of the recovery-surfacing path. The
    pre-existing test `test_fake_run_produces_no_recoveries_file` covers
    only the negative side — fake mode emits no recoveries. This locks
    that when a recovery IS recorded, it lands in both recoveries.jsonl
    AND guardrail_events.jsonl (mirrored for single-tail discovery).
    """

    def test_record_recovery_writes_both_files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = build_run_paths(root / "runs", new_run_id("rec"))
        paths.run_dir.mkdir(parents=True)

        _record_recovery(
            paths,
            kind=events.SQLITE_RECOVERY_SILENT_STALL,
            role="agent",
            recovered_chars=42,
            message_id="msg_x",
            step_finish_completed=True,
        )

        rec_lines = [
            json.loads(line)
            for line in paths.recoveries.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rec_lines), 1)
        self.assertEqual(rec_lines[0]["kind"], events.SQLITE_RECOVERY_SILENT_STALL)
        self.assertEqual(rec_lines[0]["role"], "agent")
        self.assertEqual(rec_lines[0]["recovered_chars"], 42)
        self.assertTrue(rec_lines[0]["step_finish_completed"])

        guard_lines = [
            json.loads(line)
            for line in paths.guardrail_events.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(guard_lines), 1)
        # Mirror naming convention: guardrail event is `recovery_<kind>`.
        self.assertEqual(
            guard_lines[0]["event"],
            f"recovery_{events.SQLITE_RECOVERY_SILENT_STALL}",
        )
        self.assertEqual(guard_lines[0]["role"], "agent")


if __name__ == "__main__":
    unittest.main()
