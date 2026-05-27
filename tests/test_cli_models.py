from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

from contremaitre.cli import (
    _fetch_free_models,
    _fetch_openrouter_catalog,
    _normalize_openrouter_slug,
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
    assert _normalize_openrouter_slug("openrouter/qwen/qwen3.7-max") == "openrouter/qwen/qwen3.7-max"
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
