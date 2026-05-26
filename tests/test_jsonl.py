"""Tests for jsonlog.py — JSONL reading and writing helpers."""

from __future__ import annotations

from pathlib import Path

from contremaitre.jsonlog import read_jsonl


def test_read_jsonl_missing_file(tmp_path: Path):
    assert read_jsonl(tmp_path / "nonexistent.jsonl") == []


def test_read_jsonl_empty_file(tmp_path: Path):
    (tmp_path / "f.jsonl").write_text("")
    assert read_jsonl(tmp_path / "f.jsonl") == []


def test_read_jsonl_skips_malformed_lines(tmp_path: Path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
    result = read_jsonl(p)
    assert result == [{"a": 1}, {"b": 2}]


def test_read_jsonl_skips_non_dict_values(tmp_path: Path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\n[1, 2]\n42\n')
    result = read_jsonl(p)
    assert result == [{"a": 1}]
