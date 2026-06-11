from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contremaitre.jsonlog import event_ms, read_jsonl, ts_to_ms


class JsonLogReadTest(unittest.TestCase):
    def test_read_jsonl_returns_all_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                json.dumps({"a": 1})
                + "\n"
                + json.dumps({"b": 2})
                + "\n"
                + json.dumps({"c": 3})
                + "\n",
                encoding="utf-8",
            )
            result = read_jsonl(path)
            self.assertEqual(result, [{"a": 1}, {"b": 2}, {"c": 3}])

    def test_read_jsonl_skips_empty_lines_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.jsonl"
            path.write_text(
                json.dumps({"ok": 1}) + "\n" + "\n" + "not-json\n" + json.dumps({"ok": 2}) + "\n",
                encoding="utf-8",
            )
            result = read_jsonl(path)
            self.assertEqual(result, [{"ok": 1}, {"ok": 2}])

    def test_read_jsonl_skips_non_dict_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scalars.jsonl"
            path.write_text('"just a string"\n' + "42\n" + "null\n", encoding="utf-8")
            result = read_jsonl(path)
            self.assertEqual(result, [])

    def test_read_jsonl_missing_file_returns_empty_list(self):
        result = read_jsonl(Path("/nonexistent/path.jsonl"))
        self.assertEqual(result, [])

    def test_read_jsonl_empty_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.jsonl"
            path.write_text("", encoding="utf-8")
            result = read_jsonl(path)
            self.assertEqual(result, [])

    def test_read_jsonl_directory_path_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = read_jsonl(Path(tmp))
            self.assertEqual(result, [])


class TimestampCoerceTest(unittest.TestCase):
    """`ts_to_ms` / `event_ms` are the read-side timestamp tolerance the
    flow-use path-reader used to exercise indirectly. Now that `RunArtifacts`
    owns reading, the coercer gets its own home here."""

    def test_ts_to_ms_iso_string(self):
        # ISO-8601 with a trailing Z (claude stamps these). Two stamps 2s apart
        # must coerce to ms that differ by exactly 2000.
        a = ts_to_ms("2026-01-01T00:00:00Z")
        b = ts_to_ms("2026-01-01T00:00:02Z")
        self.assertIsInstance(a, int)
        self.assertEqual(b - a, 2000)

    def test_ts_to_ms_numeric_string_and_int(self):
        self.assertEqual(ts_to_ms("1500"), 1500)
        self.assertEqual(ts_to_ms("-1500"), -1500)
        self.assertEqual(ts_to_ms(1500), 1500)
        self.assertEqual(ts_to_ms(1500.9), 1500)

    def test_ts_to_ms_unparseable_is_none(self):
        self.assertIsNone(ts_to_ms("not-a-timestamp"))
        self.assertIsNone(ts_to_ms(""))
        self.assertIsNone(ts_to_ms("   "))
        self.assertIsNone(ts_to_ms(None))

    def test_ts_to_ms_bool_is_never_a_timestamp(self):
        # bool is an int subclass; it must not coerce to 0/1 ms.
        self.assertIsNone(ts_to_ms(True))
        self.assertIsNone(ts_to_ms(False))

    def test_event_ms_prefers_timestamp_then_falls_back_to_ts(self):
        self.assertEqual(event_ms({"timestamp": 1000, "ts": "2026-01-01T00:00:00Z"}), 1000)
        self.assertEqual(event_ms({"ts": 2000}), 2000)

    def test_event_ms_missing_or_empty_is_none(self):
        self.assertIsNone(event_ms({}))
        self.assertIsNone(event_ms(None))
        self.assertIsNone(event_ms({"timestamp": "garbage"}))
