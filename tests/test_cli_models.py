from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

from contremaitre.cli import _fetch_free_models


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
