from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from contremaitre.cli import (
    _DEFAULT_ZEN_MODEL,
    _fetch_free_models,
    _fetch_openrouter_catalog,
    _index_cmd,
    _normalize_openrouter_slug,
    _pick_zen_model_interactive,
    _run_cmd,
    build_parser,
)


class _Response:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_fetch_free_models_uses_models_dev_and_excludes_deprecated():
    payload = {
        "opencode": {
            "models": {
                "minimax-m2.5-free": {
                    "id": "minimax-m2.5-free",
                    "status": "deprecated",
                },
                "deepseek-v4-flash-free": {"id": "deepseek-v4-flash-free"},
                "nemotron-3-super-free": {"id": "nemotron-3-super-free"},
                "claude-sonnet-4-5": {"id": "claude-sonnet-4-5"},
                "big-pickle": {"id": "big-pickle"},
            },
        },
    }
    requested_urls: list[str] = []

    def fake_urlopen(req, timeout: int):
        requested_urls.append(req.full_url)
        # Just guard that *some* timeout is set — the actual value is a
        # tuning knob, not a behavioural contract. The fail-fast-when-offline
        # invariant is covered by `test_..._returns_none_when_catalog_unavailable`.
        assert timeout > 0
        return _Response(payload)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert _fetch_free_models() == [
            {"id": "big-pickle"},
            {"id": "deepseek-v4-flash-free"},
            {"id": "nemotron-3-super-free"},
        ]

    assert requested_urls == ["https://models.dev/api.json"]


def test_fetch_free_models_returns_none_when_catalog_unavailable():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("offline"),
    ):
        assert _fetch_free_models() is None


def test_normalize_openrouter_slug_prepends_provider_for_raw_paste():
    # Copy-paste from openrouter.ai/models gives `<vendor>/<model>` — the
    # picker has to prepend `openrouter/` so opencode can dispatch.
    assert _normalize_openrouter_slug("qwen/qwen3.7-max") == "openrouter/qwen/qwen3.7-max"


def test_normalize_openrouter_slug_preserves_existing_provider_prefix():
    assert (
        _normalize_openrouter_slug("openrouter/qwen/qwen3.7-max") == "openrouter/qwen/qwen3.7-max"
    )
    assert _normalize_openrouter_slug("opencode/big-pickle") == "opencode/big-pickle"


def test_normalize_openrouter_slug_strips_whitespace():
    assert _normalize_openrouter_slug("  qwen/qwen3.7-max  ") == "openrouter/qwen/qwen3.7-max"


def test_normalize_openrouter_slug_passes_empty_through():
    assert _normalize_openrouter_slug("") == ""
    assert _normalize_openrouter_slug("   ") == ""


def test_fetch_openrouter_catalog_returns_id_set():
    payload = {
        "data": [
            {"id": "qwen/qwen3.7-max", "name": "Qwen 3.7 Max"},
            {"id": "anthropic/claude-opus-4.5"},
            {"id": ""},  # skipped — empty id
            "not-an-object",  # skipped — wrong shape
        ],
    }
    requested_urls: list[str] = []

    def fake_urlopen(req, timeout: int):
        requested_urls.append(req.full_url)
        assert timeout > 0
        return _Response(payload)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert _fetch_openrouter_catalog() == {
            "qwen/qwen3.7-max",
            "anthropic/claude-opus-4.5",
        }
    assert requested_urls == ["https://openrouter.ai/api/v1/models"]


def test_fetch_openrouter_catalog_returns_none_on_network_error():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("offline"),
    ):
        assert _fetch_openrouter_catalog() is None


def test_fetch_openrouter_catalog_returns_none_on_malformed_response():
    with patch(
        "urllib.request.urlopen",
        side_effect=lambda req, timeout: _Response({"not-data": "wrong shape"}),
    ):
        assert _fetch_openrouter_catalog() is None


def test_run_parser_leaves_opencode_models_unset_for_picker():
    parser = build_parser()

    args = parser.parse_args(
        ["run", "--base", "main", "--fork", "git@github.com:o/r.git", "--agent", "opencode"]
    )

    assert args.agent_model == ""
    assert args.sim_model == ""


def test_run_cmd_headless_reuses_agent_model_for_opencode_sim():
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--base",
            "main",
            "--fork",
            "git@github.com:o/r.git",
            "--agent",
            "opencode",
            "--agent-model",
            "opencode/big-pickle",
        ]
    )
    captured = {}

    def fake_run(config):
        captured["config"] = config
        return SimpleNamespace(
            verdict=SimpleNamespace(value="READY_FOR_DRAFT_PR"),
            reason="ok",
            run_dir="/tmp/run",
            pr_created=True,
        )

    with (
        patch("contremaitre.cli._ensure_local_clone"),
        patch("sys.stdin.isatty", return_value=False),
        patch("contremaitre.cli._preflight_presence_check", return_value=0),
        patch("contremaitre.cli._maybe_provision_cli_egress"),
        patch("contremaitre.cli._ensure_default_image_built", return_value=0),
        patch("contremaitre.cli.run", side_effect=fake_run),
    ):
        assert _run_cmd(args) == 0

    assert captured["config"].agent_model == "opencode/big-pickle"
    assert captured["config"].sim_model == "opencode/big-pickle"


def test_pick_zen_model_accepts_raw_openrouter_slug():
    with (
        patch("contremaitre.cli._fetch_free_models", return_value=[{"id": "big-pickle"}]),
        patch("contremaitre.cli._fetch_openrouter_catalog", return_value={"qwen/qwen3.7-max"}),
        patch("builtins.input", return_value="qwen/qwen3.7-max"),
    ):
        assert _pick_zen_model_interactive("agent") == "openrouter/qwen/qwen3.7-max"


def test_pick_zen_model_accepts_openrouter_slug_when_catalog_unavailable():
    with (
        patch("contremaitre.cli._fetch_free_models", return_value=[{"id": "big-pickle"}]),
        patch("contremaitre.cli._fetch_openrouter_catalog", return_value=None),
        patch("builtins.input", return_value="openrouter/anthropic/claude-sonnet-4.6"),
    ):
        assert _pick_zen_model_interactive("agent") == "openrouter/anthropic/claude-sonnet-4.6"


def test_pick_zen_model_catalog_unavailable_uses_zen_fallback():
    with patch("contremaitre.cli._fetch_free_models", return_value=None):
        assert _pick_zen_model_interactive("agent") == _DEFAULT_ZEN_MODEL


def test_pick_zen_model_empty_catalog_uses_zen_fallback():
    with patch("contremaitre.cli._fetch_free_models", return_value=[]):
        assert _pick_zen_model_interactive("agent") == _DEFAULT_ZEN_MODEL


def test_index_accepts_pr_outcome_refresh_flag():
    args = build_parser().parse_args(["index", "--refresh-pr-outcomes"])

    assert args.refresh_pr_outcomes is True


def test_index_reports_pr_outcome_refresh_failure_details(tmp_path, capsys):
    args = SimpleNamespace(
        runs_root=tmp_path,
        refresh_pr_outcomes=True,
        open=False,
    )
    summary = {
        "accepted": 1,
        "rejected": 0,
        "pending": 0,
        "no_pr": 0,
        "dry_run": 0,
        "unknown": 1,
        "errors": 1,
        "failures": [
            {
                "pr_url": "https://github.com/acme/widgets/pull/7",
                "error": "gh auth required",
            }
        ],
    }

    with (
        patch("contremaitre.pr_outcomes.refresh_run_outcomes", return_value=summary),
        patch("contremaitre.cli.build_index", return_value=tmp_path / "index.html"),
    ):
        assert _index_cmd(args) == 0

    captured = capsys.readouterr()
    assert "errors=1" in captured.out
    assert "https://github.com/acme/widgets/pull/7: gh auth required" in captured.err
