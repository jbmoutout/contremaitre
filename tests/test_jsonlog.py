from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contremaitre.jsonlog import read_jsonl


class ReadJsonlTest(unittest.TestCase):
    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({"a": 1}),
                    json.dumps({"b": 2}),
                    json.dumps({"c": 3}),
                ]) + "\n",
                encoding="utf-8",
            )
            result = read_jsonl(path)
            self.assertEqual(result, [{"a": 1}, {"b": 2}, {"c": 3}])

    def test_empty_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.jsonl"
            path.write_text("", encoding="utf-8")
            self.assertEqual(read_jsonl(path), [])

    def test_missing_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nonexistent.jsonl"
            self.assertEqual(read_jsonl(path), [])

    def test_blank_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blanks.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({"a": 1}),
                    "",
                    "   ",
                    json.dumps({"b": 2}),
                ]) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(read_jsonl(path), [{"a": 1}, {"b": 2}])

    def test_malformed_json_line_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "malformed.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({"good": 1}),
                    "not json at all",
                    json.dumps({"good": 2}),
                ]) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(read_jsonl(path), [{"good": 1}, {"good": 2}])

    def test_non_dict_line_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "non_dict.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({"a": 1}),
                    json.dumps([1, 2, 3]),
                    json.dumps("string"),
                    json.dumps(42),
                    json.dumps({"b": 2}),
                ]) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(read_jsonl(path), [{"a": 1}, {"b": 2}])

    def test_oserror_on_read_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self.assertTrue(path.exists())
            result = read_jsonl(path)
            self.assertEqual(result, [])

    def test_trailing_newline_does_not_add_empty_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trailing.jsonl"
            path.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")
            self.assertEqual(read_jsonl(path), [{"a": 1}])

    def test_utf8_with_replace_on_binary_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "binary_junk.jsonl"
            path.write_bytes(b'{"a": 1}\n\xff\xfe\x00{"b": 2}\n')
            result = read_jsonl(path)
            self.assertEqual(result, [{"a": 1}])


if __name__ == "__main__":
    unittest.main()
