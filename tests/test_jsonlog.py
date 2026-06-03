from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contremaitre.artifacts import read_jsonl


class JsonLogReadTest(unittest.TestCase):
    def test_read_jsonl_returns_all_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                json.dumps({"a": 1}) + "\n"
                + json.dumps({"b": 2}) + "\n"
                + json.dumps({"c": 3}) + "\n",
                encoding="utf-8",
            )
            result = read_jsonl(path)
            self.assertEqual(result, [{"a": 1}, {"b": 2}, {"c": 3}])

    def test_read_jsonl_skips_empty_lines_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.jsonl"
            path.write_text(
                json.dumps({"ok": 1}) + "\n"
                + "\n"
                + "not-json\n"
                + json.dumps({"ok": 2}) + "\n",
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
